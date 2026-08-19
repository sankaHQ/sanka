# SPDX-License-Identifier: Apache-2.0
"""HTTP contract tests for the standalone MCP API client."""

from __future__ import annotations

import json

import httpx
import pytest

from sanka_migrate_mcp.client import SankaMigrateApiClient, SankaMigrateApiError


@pytest.mark.asyncio
async def test_client_uses_branded_public_base_and_unwraps_data() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"success": True, "data": {"events": [], "dataset": {}}},
        )

    async with SankaMigrateApiClient(transport=httpx.MockTransport(handler)) as client:
        result = await client.research_eol(
            category="erp-migration",
            event_type="shutdown",
            after="2027-01",
            locale="ja",
        )

    assert result == {"events": [], "dataset": {}}
    request = requests[0]
    assert str(request.url.copy_with(query=None)) == (
        "https://api.sanka.com/v2/migrate/research/eol"
    )
    assert dict(request.url.params) == {
        "category": "erp-migration",
        "type": "shutdown",
        "after": "2027-01",
        "locale": "ja",
    }
    assert request.headers["user-agent"] == "sanka-migrate-mcp"


@pytest.mark.asyncio
async def test_compare_escapes_category_and_serializes_platforms() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True, "data": {"platforms": []}})

    async with SankaMigrateApiClient(transport=httpx.MockTransport(handler)) as client:
        await client.research_compare(
            category="crm/migration",
            platforms=["salesforce", "hubspot"],
            locale="en",
        )

    assert "/research/compare/crm%2Fmigration?" in str(requests[0].url)
    assert dict(requests[0].url.params) == {
        "platforms": "salesforce,hubspot",
        "locale": "en",
    }


@pytest.mark.asyncio
async def test_client_preserves_rate_limit_and_support_context() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json={
                "success": False,
                "error": {"code": "SANKA_MIGRATE_RATE_LIMITED", "message": "Slow down."},
                "meta": {"ctx_id": "ctx-test"},
            },
        )

    async with SankaMigrateApiClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SankaMigrateApiError) as exc_info:
            await client.research_tco()

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 60
    assert exc_info.value.ctx_id == "ctx-test"
    assert exc_info.value.code == "SANKA_MIGRATE_RATE_LIMITED"


@pytest.mark.asyncio
async def test_client_uses_support_context_header_when_envelope_omits_meta() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            503,
            headers={"X-Ctx-Id": "ctx-header"},
            json={
                "success": False,
                "error": {"code": "SERVICE_UNAVAILABLE", "message": "Unavailable."},
            },
        )

    async with SankaMigrateApiClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SankaMigrateApiError) as exc_info:
            await client.research_tco()

    assert exc_info.value.ctx_id == "ctx-header"


@pytest.mark.asyncio
async def test_client_rejects_non_object_success_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=json.dumps({"success": True, "data": []}).encode(),
        )

    async with SankaMigrateApiClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SankaMigrateApiError, match="invalid response") as exc_info:
            await client.research_tco()

    assert exc_info.value.code == "SANKA_MIGRATE_API_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_client_maps_transport_failure_to_tool_safe_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with SankaMigrateApiClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SankaMigrateApiError) as exc_info:
            await client.research_tco()

    assert exc_info.value.code == "SANKA_MIGRATE_API_UNAVAILABLE"
    assert "offline" in exc_info.value.message
