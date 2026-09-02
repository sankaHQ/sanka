# SPDX-License-Identifier: Apache-2.0
"""stdio MCP server exposing the Sanka public research API."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, Literal

from mcp import McpError
from mcp.server.fastmcp import FastMCP
from mcp.types import INTERNAL_ERROR, ErrorData, ToolAnnotations
from pydantic import Field

from sanka_cli.mcp.client import SankaMigrateApiClient, SankaMigrateApiError
from sanka_cli.mcp.render import (
    rate_limited_result,
    render_assessment,
    render_compare,
    render_eol,
    render_tco,
)

Locale = Literal["en", "ja"]
EventType = Literal["shutdown", "end_of_support", "maintenance_end", "version_lifecycle"]
Platforms = Annotated[list[str] | None, Field(default=None, max_length=10)]
AssessmentSource = Annotated[str, Field(min_length=1)]

RESEARCH_CONTRACT = (
    "Keyless and rate-limited per IP. Every claim includes vendor source URLs and "
    "verified dates; cite them when using the data. Results contain at most 50 items "
    "and report when a narrower filter is needed."
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
ASSESSMENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

mcp = FastMCP(
    "Sanka Research",
    instructions=(
        "Use the research tools for cited software lifecycle and migration evidence. "
        "The assessment tool creates one assessment only when the user asks for it."
    ),
)


@mcp.tool(
    name="sanka_research_eol",
    description=f"Read software end-of-life and support events. {RESEARCH_CONTRACT}",
    annotations=READ_ONLY,
    structured_output=True,
)
async def research_eol(
    product: str | None = None,
    category: str | None = None,
    type: EventType | None = None,
    after: str | None = None,
    before: str | None = None,
    locale: Locale | None = None,
) -> dict[str, Any]:
    try:
        async with SankaMigrateApiClient() as client:
            data = await client.research_eol(
                product=product,
                category=category,
                event_type=type,
                after=after,
                before=before,
                locale=locale,
            )
    except SankaMigrateApiError as error:
        return _handle_api_error(error)
    return render_eol(data, locale=locale)


@mcp.tool(
    name="sanka_research_tco",
    description=f"Read cited software pricing and total-cost benchmarks. {RESEARCH_CONTRACT}",
    annotations=READ_ONLY,
    structured_output=True,
)
async def research_tco(
    product: str | None = None,
    category: str | None = None,
    locale: Locale | None = None,
) -> dict[str, Any]:
    try:
        async with SankaMigrateApiClient() as client:
            data = await client.research_tco(
                product=product,
                category=category,
                locale=locale,
            )
    except SankaMigrateApiError as error:
        return _handle_api_error(error)
    return render_tco(data, locale=locale)


@mcp.tool(
    name="sanka_research_compare",
    description=(
        "Compare cited export, import, and operational facts in one migration category. "
        "Accepts at most ten platform slugs. " + RESEARCH_CONTRACT
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
async def research_compare(
    category: str,
    platforms: Platforms = None,
    locale: Locale | None = None,
) -> dict[str, Any]:
    try:
        async with SankaMigrateApiClient() as client:
            data = await client.research_compare(
                category=category,
                platforms=platforms,
                locale=locale,
            )
    except SankaMigrateApiError as error:
        return _handle_api_error(error)
    return render_compare(data, locale=locale)


@mcp.tool(
    name="sanka_assess",
    description=(
        "Create one free Sanka assessment from the supplied migration answers. "
        "This records an assessment but does not create an account or start a migration."
    ),
    annotations=ASSESSMENT_WRITE,
    structured_output=True,
)
async def assess(
    source: AssessmentSource,
    destination: str | None = None,
    volume: str | None = None,
    timing: str | None = None,
    concerns: str | None = None,
    category: str | None = None,
    lang: Locale = "en",
) -> dict[str, Any]:
    started_at_ms = int(time.time() * 1000)
    started_at = time.monotonic()
    remaining = 2.0 - (time.monotonic() - started_at)
    if remaining > 0:
        await asyncio.sleep(remaining)
    try:
        async with SankaMigrateApiClient() as client:
            data = await client.assess(
                {
                    "source": source,
                    "destination": destination or "",
                    "volume": volume or "",
                    "timing": timing or "",
                    "concerns": concerns or "",
                    "category": category or "",
                    "lang": lang,
                    "attribution": {"channel": "mcp"},
                    "website": "",
                    "form_started_at": started_at_ms,
                }
            )
    except SankaMigrateApiError as error:
        return _handle_api_error(error)
    try:
        return render_assessment(data, lang=lang)
    except ValueError as error:
        raise _tool_error(
            "SANKA_MIGRATE_API_INVALID_RESPONSE",
            str(error),
            ctx_id=None,
        ) from error


def _handle_api_error(error: SankaMigrateApiError) -> dict[str, Any]:
    if error.status_code == 429:
        return rate_limited_result(error)
    raise _tool_error(error.code, error.message, ctx_id=error.ctx_id) from error


def _tool_error(code: str, message: str, *, ctx_id: str | None) -> McpError:
    data: dict[str, str] = {"code": code}
    if ctx_id:
        data["ctx_id"] = ctx_id
    suffix = f" (ctx_id: {ctx_id})" if ctx_id else ""
    return McpError(
        ErrorData(
            code=INTERNAL_ERROR,
            message=f"{code}: {message}{suffix}",
            data=data,
        )
    )


def main() -> None:
    mcp.run(transport="stdio")


__all__ = ["assess", "main", "mcp", "research_compare", "research_eol", "research_tco"]
