# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

import httpx
import pytest

from sanka.connector import (
    ConfigurationError,
    Credentials,
    SourceConnector,
    SupportsConfigValidation,
    SupportsLimits,
    SupportsRecordCounts,
    SupportsSnapshotBounds,
    ValidationFailedError,
)
from sanka_connector_sendgrid import CONNECTOR, HttpSendGridGateway, SendGridSource


def credentials(**overrides: Any) -> Credentials:
    values: dict[str, Any] = {
        "provider": "sendgrid",
        "connection_id": "sendgrid-demo",
        "access_token": "SG.demo",
    }
    values.update(overrides)
    return Credentials(**values)


class FakeGateway:
    def __init__(self) -> None:
        self.statuses = ["pending", "ready"]
        self.downloads = 0

    async def get_contact_summary(self, _credentials: Credentials) -> dict[str, Any]:
        return {"contact_count": 3, "result": []}

    async def create_contact_export(self, _credentials: Credentials) -> str:
        return "export-1"

    async def get_contact_export(
        self,
        _credentials: Credentials,
        *,
        export_id: str,
    ) -> dict[str, Any]:
        assert export_id == "export-1"
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {
            "id": export_id,
            "status": status,
            "urls": ["https://files.example/export.json"] if status == "ready" else [],
        }

    async def download_contact_export(
        self,
        _credentials: Credentials,
        *,
        urls: list[str],
    ) -> list[dict[str, Any]]:
        assert urls == ["https://files.example/export.json"]
        self.downloads += 1
        return [
            {"id": "c-3", "email": "c@example.com", "first_name": "C"},
            {"id": "c-1", "email": "a@example.com", "first_name": "A"},
            {"id": "c-2", "email": "b@example.com", "first_name": "B"},
        ]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def source(gateway: FakeGateway, clock: Clock | None = None) -> SendGridSource:
    active = clock or Clock()
    return SendGridSource(
        gateway=gateway,
        sleep=active.sleep,
        monotonic=active.monotonic,
        export_timeout_seconds=10,
        poll_interval_seconds=0.25,
    )


def test_registration_exposes_source_capabilities() -> None:
    assert CONNECTOR.name == "sendgrid"
    assert isinstance(CONNECTOR.source, SourceConnector)
    assert isinstance(CONNECTOR.source, SupportsConfigValidation)
    assert isinstance(CONNECTOR.source, SupportsLimits)
    assert isinstance(CONNECTOR.source, SupportsRecordCounts)
    assert isinstance(CONNECTOR.source, SupportsSnapshotBounds)
    assert CONNECTOR.destination is None


async def test_inventory_models_one_complete_contact_export() -> None:
    gateway = FakeGateway()
    connector = source(gateway)

    objects = await connector.discover_objects(credentials())
    assert [(item.key, item.canonical_type, item.default_selected) for item in objects] == [
        ("contacts", "contact", True)
    ]

    inventory = await connector.inventory(credentials())
    (contacts,) = inventory.objects
    assert contacts.record_count == 3
    assert contacts.identity_fields == ["id"]
    assert {field.key for field in contacts.fields} >= {
        "id",
        "email",
        "first_name",
        "last_name",
        "custom_fields",
    }


async def test_snapshot_bound_pages_reuse_the_same_export() -> None:
    gateway = FakeGateway()
    clock = Clock()
    connector = source(gateway, clock)

    bound = await connector.high_water_mark(credentials(), object_type="contacts")
    assert bound == "export-1"
    assert clock.sleeps == [0.25]

    first = await connector.read_records_bounded(
        credentials(),
        object_type="contacts",
        field_keys=["email", "first_name"],
        limit=2,
        upper_bound=bound,
    )
    assert [row["id"] for row in first.records] == ["c-1", "c-2"]
    assert first.next_cursor == "2"
    assert first.has_more is True

    second = await connector.read_records_bounded(
        credentials(),
        object_type="contacts",
        field_keys=["email", "first_name"],
        limit=2,
        cursor=first.next_cursor,
        upper_bound=bound,
    )
    assert second.records == [{"id": "c-3", "email": "c@example.com", "first_name": "C"}]
    assert second.next_cursor is None
    assert second.has_more is False
    assert (
        await connector.count_records_bounded(
            credentials(), object_type="contacts", upper_bound=bound
        )
        == 3
    )


async def test_unbounded_cursor_carries_export_identity() -> None:
    gateway = FakeGateway()
    gateway.statuses = ["ready"]
    connector = source(gateway)
    first = await connector.read_records(
        credentials(),
        object_type="contacts",
        field_keys=["email"],
        limit=1,
    )
    assert first.next_cursor
    second = await connector.read_records(
        credentials(),
        object_type="contacts",
        field_keys=["email"],
        limit=2,
        cursor=first.next_cursor,
    )
    assert [row["id"] for row in second.records] == ["c-2", "c-3"]


async def test_invalid_object_and_cursor_fail_closed() -> None:
    connector = source(FakeGateway())
    with pytest.raises(ValidationFailedError, match="object type"):
        await connector.count_records(credentials(), object_type="lists")
    with pytest.raises(ValidationFailedError, match="cursor"):
        await connector.read_records(
            credentials(),
            object_type="contacts",
            field_keys=[],
            limit=1,
            cursor="not-a-cursor",
        )


async def test_http_gateway_uses_override_and_never_forwards_key_to_download() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/sendgrid/v3/marketing/contacts":
            return httpx.Response(200, json={"contact_count": 1, "result": []})
        if request.url.host == "files.example":
            return httpx.Response(200, json=[{"id": "c-1", "email": "a@example.com"}])
        raise AssertionError(f"unexpected request: {request.url}")

    gateway = HttpSendGridGateway(transport=httpx.MockTransport(respond))
    configured = credentials(settings={"api_base_url": "https://demo.local/sendgrid"})
    await gateway.get_contact_summary(configured)
    records = await gateway.download_contact_export(
        configured,
        urls=["https://files.example/export.json"],
    )
    assert records[0]["id"] == "c-1"
    assert seen[0].headers["Authorization"] == "Bearer SG.demo"
    assert "Authorization" not in seen[1].headers


async def test_http_gateway_requires_api_key_and_absolute_base_url() -> None:
    gateway = HttpSendGridGateway(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    with pytest.raises(ConfigurationError, match="API key"):
        await gateway.get_contact_summary(credentials(access_token=None))
    with pytest.raises(ConfigurationError, match="absolute"):
        await gateway.get_contact_summary(credentials(settings={"api_base_url": "/sendgrid"}))
