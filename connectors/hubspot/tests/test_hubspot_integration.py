# SPDX-License-Identifier: Apache-2.0
"""Integration tests against a live HubSpot portal.

Set ``SANKA_MIGRATE_TEST_HUBSPOT_ACCESS_TOKEN`` (a private-app token for a
**disposable/test portal** with CRM object + schema read scopes and contact
write scope) to run these; without it the whole module is skipped so
``make check`` stays green offline.

The tests restrict themselves to clearly marked test contacts
(``sanka-migrate-connector-test-…@example.com`` with firstname ``Sanka Migrate Connector
Test``), poll for search-index visibility instead of assuming immediate
consistency, and archive every record they create.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from sanka_connector_hubspot import HubSpotDestination, HubSpotSource
from sanka_connector_hubspot._base import HUBSPOT_CRM_OBJECTS_URL, HubSpotGateway

from sanka.connector import Credentials, WriteOptions

ACCESS_TOKEN = os.environ.get("SANKA_MIGRATE_TEST_HUBSPOT_ACCESS_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not ACCESS_TOKEN, reason="SANKA_MIGRATE_TEST_HUBSPOT_ACCESS_TOKEN is not set"
)

TEST_FIRSTNAME = "Sanka Migrate Connector Test"
SEARCH_VISIBILITY_TIMEOUT_SECONDS = 90.0


def _credentials() -> Credentials:
    return Credentials(
        provider="hubspot",
        connection_id="integration-test",
        access_token=ACCESS_TOKEN,
    )


def _test_email() -> str:
    return f"sanka-migrate-connector-test-{uuid.uuid4().hex[:12]}@example.com"


async def _archive_contact(record_id: str) -> None:
    url = f"{HUBSPOT_CRM_OBJECTS_URL.format(object_type='contacts')}/{record_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        # 204 archived; 404 means it is already gone — both are fine.
        assert response.status_code in {204, 404}, response.text


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float = SEARCH_VISIBILITY_TIMEOUT_SECONDS,
    interval: float = 3.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _search_contact_by_email(gateway: HubSpotGateway, email: str) -> str | None:
    payload = await gateway.search_crm_objects(
        _credentials(),
        object_type="contacts",
        payload={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "limit": 1,
            "properties": ["email"],
        },
    )
    results = payload.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        record_id = str(results[0].get("id") or "").strip()
        return record_id or None
    return None


@pytest.fixture
async def created_contacts() -> AsyncIterator[list[str]]:
    record_ids: list[str] = []
    yield record_ids
    for record_id in record_ids:
        await _archive_contact(record_id)


async def test_discover_objects_lists_standard_types() -> None:
    options = await HubSpotSource().discover_objects(_credentials())
    keys = {option.key for option in options}
    assert {"companies", "contacts", "deals", "tickets"} <= keys
    defaults = [option for option in options if option.default_selected]
    assert {option.key for option in defaults} == {"companies", "contacts", "deals", "tickets"}


async def test_contacts_inventory_has_email_identity() -> None:
    inventory = await HubSpotSource().inventory(_credentials(), object_types=["contacts"])
    (contacts,) = inventory.objects
    assert contacts.key == "contacts"
    assert "email" in contacts.identity_fields
    field_keys = {field.key for field in contacts.fields}
    assert "email" in field_keys
    assert contacts.record_count >= 0


async def test_contact_write_lifecycle(created_contacts: list[str]) -> None:
    gateway = HubSpotGateway()
    destination = HubSpotDestination(gateway=gateway)
    email = _test_email()

    created = await destination.write_record(
        _credentials(),
        object_type="contacts",
        properties={
            "email": email,
            "firstname": TEST_FIRSTNAME,
            "lastname": "Delete Me",
        },
        options=WriteOptions(conflict_policy="create"),
    )
    assert created.status == "created"
    assert created.destination_record_id
    created_contacts.append(created.destination_record_id)

    # HubSpot's search index is eventually consistent; wait for the record to
    # become visible before exercising the identity-based conflict policies.
    visible = await _wait_until(
        lambda: _visible(gateway, email, created.destination_record_id or "")
    )
    assert visible, "created contact never became visible in HubSpot search"

    skipped = await destination.write_record(
        _credentials(),
        object_type="contacts",
        properties={"email": email, "firstname": TEST_FIRSTNAME},
        options=WriteOptions(conflict_policy="skip_existing"),
    )
    assert skipped.status == "skipped"
    assert skipped.destination_record_id == created.destination_record_id

    updated = await destination.write_record(
        _credentials(),
        object_type="contacts",
        properties={"email": email, "lastname": "Delete Me Updated"},
        options=WriteOptions(conflict_policy="update_existing"),
    )
    assert updated.status == "updated"
    assert updated.destination_record_id == created.destination_record_id

    metrics = destination.retry_metrics()
    assert metrics["requests"] >= 3
    assert set(metrics) == {
        "requests",
        "retries",
        "rateLimitRetries",
        "throttleWaitMs",
        "lastRetryAt",
    }


async def _visible(gateway: HubSpotGateway, email: str, expected_id: str) -> bool:
    return await _search_contact_by_email(gateway, email) == expected_id


async def test_list_owners_smoke() -> None:
    owners = await HubSpotDestination().list_owners(_credentials())
    assert isinstance(owners, list)
    for owner in owners:
        assert owner.id
        assert owner.email
