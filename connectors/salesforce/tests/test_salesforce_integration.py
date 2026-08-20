# SPDX-License-Identifier: Apache-2.0
"""Integration tests against a live Salesforce org — strictly read-only.

Set both ``SANKA_MIGRATE_TEST_SALESFORCE_INSTANCE_URL`` (for example
``https://yourcompany.my.salesforce.com``) and
``SANKA_MIGRATE_TEST_SALESFORCE_ACCESS_TOKEN`` (an OAuth access token with API scope)
to run these; without them the whole module is skipped so ``make check``
stays green offline. A Developer Edition or sandbox org works fine. Every
call these tests make is a query, a describe, or a catalog read — nothing in
the org is created, changed, or deleted.
"""

from __future__ import annotations

import os

import pytest

from sanka.connector import Credentials
from sanka_connector_salesforce import SalesforceSource

INSTANCE_URL = os.environ.get("SANKA_MIGRATE_TEST_SALESFORCE_INSTANCE_URL", "")
ACCESS_TOKEN = os.environ.get("SANKA_MIGRATE_TEST_SALESFORCE_ACCESS_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not (INSTANCE_URL and ACCESS_TOKEN),
    reason=(
        "SANKA_MIGRATE_TEST_SALESFORCE_INSTANCE_URL and "
        "SANKA_MIGRATE_TEST_SALESFORCE_ACCESS_TOKEN are not set"
    ),
)


@pytest.fixture
def credentials() -> Credentials:
    return Credentials(
        provider="salesforce",
        connection_id="integration-test",
        access_token=ACCESS_TOKEN,
        settings={"instance_url": INSTANCE_URL},
    )


@pytest.fixture
def source() -> SalesforceSource:
    return SalesforceSource()


async def test_discover_objects_includes_core_crm_types(
    source: SalesforceSource, credentials: Credentials
) -> None:
    options = await source.discover_objects(credentials)
    keys = {option.key for option in options}
    assert "Account" in keys
    account = next(option for option in options if option.key == "Account")
    assert account.canonical_type == "company"
    assert account.default_selected is True
    # Default-selected objects sort ahead of everything else.
    defaults = [option.default_selected for option in options]
    assert defaults == sorted(defaults, reverse=True)


async def test_inventory_account_has_identity_fields_and_count(
    source: SalesforceSource, credentials: Credentials
) -> None:
    inventory = await source.inventory(credentials, object_types=["Account"])
    assert inventory.warnings == []
    (account,) = inventory.objects
    assert account.identity_fields == ["Id"]
    assert account.record_count >= 0
    field_keys = {field.key for field in account.fields}
    assert {"Id", "Name"} <= field_keys


async def test_read_records_paginates_with_strictly_increasing_ids(
    source: SalesforceSource, credentials: Credentials
) -> None:
    first = await source.read_records(
        credentials, object_type="Account", field_keys=["Name"], limit=2
    )
    ids = [str(record["Id"]) for record in first.records]
    assert ids == sorted(ids)
    for record in first.records:
        assert record["Id"]
    if not first.has_more:
        return
    assert first.next_cursor == ids[-1]
    second = await source.read_records(
        credentials,
        object_type="Account",
        field_keys=["Name"],
        limit=2,
        cursor=first.next_cursor,
    )
    for record in second.records:
        assert str(record["Id"]) > first.next_cursor


async def test_snapshot_bounds_are_consistent(
    source: SalesforceSource, credentials: Credentials
) -> None:
    mark = await source.high_water_mark(credentials, object_type="Account")
    total = await source.count_records(credentials, object_type="Account")
    assert total >= 0
    if mark is None:
        assert total == 0
        return
    bounded = await source.count_records_bounded(
        credentials, object_type="Account", upper_bound=mark
    )
    assert 0 <= bounded <= total
    page = await source.read_records_bounded(
        credentials, object_type="Account", field_keys=["Name"], limit=5, upper_bound=mark
    )
    for record in page.records:
        assert str(record["Id"]) <= mark


async def test_list_owners_returns_active_users(
    source: SalesforceSource, credentials: Credentials
) -> None:
    owners = await source.list_owners(credentials)
    # Every org has at least its admin user, but the directory only includes
    # users with an email, so allow empty and validate shape instead.
    for owner in owners:
        assert owner.id
        assert owner.email
