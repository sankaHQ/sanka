# SPDX-License-Identifier: Apache-2.0
"""MCP tool schemas, annotations, and error behavior."""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, ClassVar, Self

import pytest
from mcp import McpError

import sanka_migrate_mcp.server as server
from sanka_migrate_mcp.client import SankaMigrateApiError


@pytest.mark.asyncio
async def test_server_registers_exact_tools_and_write_annotations() -> None:
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    assert set(tools) == {
        "sanka_migrate_research_eol",
        "sanka_migrate_research_tco",
        "sanka_migrate_research_compare",
        "sanka_migrate_assess",
    }
    for name in (
        "sanka_migrate_research_eol",
        "sanka_migrate_research_tco",
        "sanka_migrate_research_compare",
    ):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is True
        assert "rate-limited" in (tools[name].description or "")
    assess = tools["sanka_migrate_assess"]
    assert assess.annotations is not None
    assert assess.annotations.readOnlyHint is False
    assert assess.annotations.destructiveHint is False
    assert "Create one" in (assess.description or "")
    compare_platforms = tools["sanka_migrate_research_compare"].inputSchema["properties"][
        "platforms"
    ]
    assert compare_platforms["anyOf"][0]["maxItems"] == 10


class _FakeClient:
    result: ClassVar[dict[str, Any]] = {}
    error: ClassVar[SankaMigrateApiError | None] = None
    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def research_eol(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("eol", kwargs))
        if self.error:
            raise self.error
        return self.result

    async def assess(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("assess", payload))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    _FakeClient.result = {}
    _FakeClient.error = None
    _FakeClient.calls = []


@pytest.mark.asyncio
async def test_read_tool_returns_structured_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.error = SankaMigrateApiError(
        "SANKA_MIGRATE_RATE_LIMITED",
        "Slow down.",
        status_code=429,
        retry_after_seconds=45,
    )
    monkeypatch.setattr(server, "SankaMigrateApiClient", _FakeClient)

    result = await server.research_eol(category="erp-migration")

    assert result == {
        "rate_limited": True,
        "retry_after_seconds": 45,
        "message": "Slow down.",
    }


@pytest.mark.asyncio
async def test_server_failure_is_mcp_error_with_api_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.error = SankaMigrateApiError(
        "SANKA_MIGRATE_API_UNAVAILABLE",
        "Temporarily unavailable.",
        status_code=503,
        ctx_id="ctx-503",
    )
    monkeypatch.setattr(server, "SankaMigrateApiClient", _FakeClient)

    with pytest.raises(McpError) as exc_info:
        await server.research_eol()

    assert "SANKA_MIGRATE_API_UNAVAILABLE" in str(exc_info.value)
    assert "ctx-503" in str(exc_info.value)


@pytest.mark.asyncio
async def test_assessment_waits_and_marks_mcp_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.result = {"assessment_id": "assessment-123"}
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(server, "SankaMigrateApiClient", _FakeClient)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await server.assess(source="Salesforce", destination="HubSpot", lang="en")

    assert waits and 1.9 < waits[0] <= 2.0
    assert result["assessment_id"] == "assessment-123"
    method, payload = _FakeClient.calls[0]
    assert method == "assess"
    assert payload["attribution"] == {"channel": "mcp"}
    assert payload["website"] == ""
    assert isinstance(payload["form_started_at"], int)
