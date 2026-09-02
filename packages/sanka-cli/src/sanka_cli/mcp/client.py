# SPDX-License-Identifier: Apache-2.0
"""Async HTTP client for the public Sanka research surface."""

from __future__ import annotations

import os
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self, cast
from urllib.parse import quote

import httpx

DEFAULT_API_BASE = "https://api.sanka.com/v2/migrate"
DEFAULT_TIMEOUT_SECONDS = 10.0


class SankaMigrateApiError(Exception):
    """A transport, protocol, or standard-envelope API failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        ctx_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.ctx_id = ctx_id


class SankaMigrateApiClient:
    """Small, credential-free client used only by the CLI MCP server."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        configured_base = base_url or os.environ.get("SANKA_MIGRATE_API_BASE") or DEFAULT_API_BASE
        self._client = httpx.AsyncClient(
            base_url=configured_base.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "sanka-cli/0.2.0"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def research_eol(
        self,
        *,
        product: str | None = None,
        category: str | None = None,
        event_type: str | None = None,
        after: str | None = None,
        before: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        path = "/research/eol"
        if product:
            path += f"/{quote(product, safe='')}"
        return await self._request(
            "GET",
            path,
            params={
                "category": category,
                "type": event_type,
                "after": after,
                "before": before,
                "locale": locale,
            },
        )

    async def research_tco(
        self,
        *,
        product: str | None = None,
        category: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        path = "/research/tco"
        if product:
            path += f"/{quote(product, safe='')}"
        return await self._request(
            "GET",
            path,
            params={"category": category, "locale": locale},
        )

    async def research_compare(
        self,
        *,
        category: str,
        platforms: list[str] | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/research/compare/{quote(category, safe='')}",
            params={
                "platforms": ",".join(platforms) if platforms else None,
                "locale": locale,
            },
        )

    async def assess(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/assessments", json=dict(payload))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | None] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        filtered_params = {
            key: value for key, value in (params or {}).items() if value not in (None, "")
        }
        try:
            response = await self._client.request(
                method,
                path,
                params=filtered_params or None,
                json=json,
            )
        except httpx.TimeoutException as error:
            raise SankaMigrateApiError(
                "SANKA_MIGRATE_API_TIMEOUT",
                "Sanka API request timed out.",
            ) from error
        except httpx.TransportError as error:
            raise SankaMigrateApiError(
                "SANKA_MIGRATE_API_UNAVAILABLE",
                f"Sanka API request failed: {error}",
            ) from error

        envelope = _json_object(response)
        if response.is_error:
            code, message, ctx_id = _error_details(envelope)
            raise SankaMigrateApiError(
                code,
                message,
                status_code=response.status_code,
                retry_after_seconds=_retry_after_seconds(response.headers),
                ctx_id=(
                    ctx_id
                    or response.headers.get("X-Ctx-Id")
                    or response.headers.get("X-Request-ID")
                ),
            )
        if envelope.get("success") is not True:
            code, message, ctx_id = _error_details(envelope)
            raise SankaMigrateApiError(code, message, ctx_id=ctx_id)
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise SankaMigrateApiError(
                "SANKA_MIGRATE_API_INVALID_RESPONSE",
                "Sanka API returned an invalid response.",
            )
        return cast(dict[str, Any], data)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {}
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _error_details(envelope: Mapping[str, Any]) -> tuple[str, str, str | None]:
    error = envelope.get("error")
    details = error if isinstance(error, dict) else {}
    meta = envelope.get("meta")
    metadata = meta if isinstance(meta, dict) else {}
    return (
        str(details.get("code") or "SANKA_MIGRATE_API_ERROR"),
        str(details.get("message") or "Sanka API request failed."),
        str(metadata.get("ctx_id")) if metadata.get("ctx_id") else None,
    )


def _retry_after_seconds(headers: httpx.Headers) -> int | None:
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_TIMEOUT_SECONDS",
    "SankaMigrateApiClient",
    "SankaMigrateApiError",
]
