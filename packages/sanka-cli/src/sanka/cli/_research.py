# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP helpers for the credential-free Sanka assessment endpoint."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sanka_cli import __version__

DEFAULT_API_BASE = "https://api.sanka.com/v2/migrate"
DEFAULT_SIGNUP_BASE = "https://app.sanka.com"
DEFAULT_TIMEOUT_SECONDS = 10.0


class _HttpResponse(Protocol):
    headers: Mapping[str, str]

    def read(self) -> bytes: ...


_Opener = Callable[..., Any]


class SankaMigrateApiError(Exception):
    """A transport or standard-envelope error returned by the public API."""

    def __init__(self, code: str, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


class SankaMigrateApiClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: _Opener = urlopen,
    ) -> None:
        configured_base = base_url or os.environ.get("SANKA_MIGRATE_API_BASE") or DEFAULT_API_BASE
        self._base_url = configured_base.rstrip("/")
        self._timeout = timeout
        self._opener = opener

    def assess(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/assessments", payload=payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str | None] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = {key: value for key, value in (query or {}).items() if value not in (None, "")}
        suffix = f"?{urlencode(params)}" if params else ""
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "User-Agent": f"sanka-cli/{__version__}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}{suffix}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                envelope = _decode_json(response.read())
        except HTTPError as error:
            envelope = _decode_json(error.read(), fallback={})
            details = _error_details(envelope)
            retry_after = error.headers.get("Retry-After") if error.headers else None
            raise SankaMigrateApiError(
                details["code"],
                details["message"],
                retry_after=retry_after,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise SankaMigrateApiError(
                "SANKA_MIGRATE_API_UNAVAILABLE",
                f"Sanka API request failed: {error}",
            ) from error
        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            details = _error_details(envelope)
            raise SankaMigrateApiError(details["code"], details["message"])
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise SankaMigrateApiError(
                "SANKA_MIGRATE_API_INVALID_RESPONSE",
                "Sanka API returned an invalid response.",
            )
        return cast(dict[str, Any], data)


def signup_url(assessment_id: str, *, lang: str) -> str:
    base = (os.environ.get("SANKA_MIGRATE_SIGNUP_BASE") or DEFAULT_SIGNUP_BASE).rstrip("/")
    signup_path = "/ja/signup" if lang == "ja" else "/register"
    handoff = urlencode({"lang": lang, "assessment_id": assessment_id})
    return f"{base}{signup_path}?{urlencode({'next': f'/migrate-onboarding?{handoff}'})}"


def _decode_json(raw: bytes, *, fallback: Any = None) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback


def _error_details(envelope: Any) -> dict[str, str]:
    error = envelope.get("error") if isinstance(envelope, dict) else None
    if not isinstance(error, dict):
        return {
            "code": "SANKA_MIGRATE_API_ERROR",
            "message": "Sanka API request failed.",
        }
    return {
        "code": str(error.get("code") or "SANKA_MIGRATE_API_ERROR"),
        "message": str(error.get("message") or "Sanka API request failed."),
    }


__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_SIGNUP_BASE",
    "SankaMigrateApiClient",
    "SankaMigrateApiError",
    "signup_url",
]
