# SPDX-License-Identifier: Apache-2.0
"""Read-only HTTP gateway for Twilio SendGrid Marketing Contacts exports."""

from __future__ import annotations

import gzip
import json
from typing import Any, Protocol

import httpx

from sanka.connector import (
    AuthenticationError,
    ConfigurationError,
    ConnectorError,
    Credentials,
    DataError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    TransientProviderError,
)

DEFAULT_API_BASE_URL = "https://api.sendgrid.com"


class SendGridGateway(Protocol):
    async def get_contact_summary(self, credentials: Credentials) -> dict[str, Any]: ...

    async def create_contact_export(self, credentials: Credentials) -> str: ...

    async def get_contact_export(
        self,
        credentials: Credentials,
        *,
        export_id: str,
    ) -> dict[str, Any]: ...

    async def download_contact_export(
        self,
        credentials: Credentials,
        *,
        urls: list[str],
    ) -> list[dict[str, Any]]: ...


class HttpSendGridGateway:
    """SendGrid v3 implementation with injectable transport for contract tests."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def get_contact_summary(self, credentials: Credentials) -> dict[str, Any]:
        return await self._request_object(
            credentials,
            "GET",
            "/v3/marketing/contacts",
        )

    async def create_contact_export(self, credentials: Credentials) -> str:
        payload = await self._request_object(
            credentials,
            "POST",
            "/v3/marketing/contacts/exports",
            json_body={
                "file_type": "json",
                "notifications": {"email": False},
            },
        )
        export_id = str(payload.get("id") or "").strip()
        if not export_id:
            raise DataError("SendGrid export response is missing id")
        return export_id

    async def get_contact_export(
        self,
        credentials: Credentials,
        *,
        export_id: str,
    ) -> dict[str, Any]:
        return await self._request_object(
            credentials,
            "GET",
            f"/v3/marketing/contacts/exports/{export_id}",
        )

    async def download_contact_export(
        self,
        credentials: Credentials,
        *,
        urls: list[str],
    ) -> list[dict[str, Any]]:
        # Signed export URLs are bearer credentials in their own right. Never
        # forward the SendGrid API key to the object-storage host.
        records: list[dict[str, Any]] = []
        for url in urls:
            response = await self._send(credentials, "GET", url, authenticated=False)
            if response.status_code >= 400:
                raise _mapped_http_error(response)
            records.extend(_json_records(response))
        return records

    async def _request_object(
        self,
        credentials: Credentials,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._send(
            credentials,
            method,
            f"{_api_base_url(credentials)}{path}",
            authenticated=True,
            json_body=json_body,
        )
        if response.status_code >= 400:
            raise _mapped_http_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataError(
                f"SendGrid returned HTTP {response.status_code} with a non-JSON body"
            ) from exc
        if not isinstance(payload, dict):
            raise DataError("SendGrid returned an unexpected JSON shape")
        return payload

    async def _send(
        self,
        credentials: Credentials,
        method: str,
        url: str,
        *,
        authenticated: bool,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {_api_key(credentials)}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                return await client.request(method, url, headers=headers, json=json_body)
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"SendGrid request failed: {exc}") from exc


def _api_key(credentials: Credentials) -> str:
    value = str(credentials.access_token or "").strip()
    if not value:
        raise ConfigurationError("sendgrid connector needs an API key in Credentials.access_token")
    return value


def _api_base_url(credentials: Credentials) -> str:
    raw = str(credentials.settings.get("api_base_url") or DEFAULT_API_BASE_URL).strip()
    try:
        parsed = httpx.URL(raw)
    except ValueError as exc:
        raise ConfigurationError("SendGrid api_base_url is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ConfigurationError("SendGrid api_base_url must be an absolute HTTP(S) URL")
    return raw.rstrip("/")


def _json_records(response: httpx.Response) -> list[dict[str, Any]]:
    body = response.content
    if body.startswith(b"\x1f\x8b"):
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            raise DataError("SendGrid export contains invalid gzip data") from exc
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            payload = [json.loads(line) for line in body.decode("utf-8").splitlines() if line]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataError("SendGrid export is not valid JSON") from exc
    if isinstance(payload, dict):
        payload = payload.get("result")
    if not isinstance(payload, list):
        raise DataError("SendGrid export JSON must contain a list of contacts")
    return [row for row in payload if isinstance(row, dict)]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = str(response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _mapped_http_error(response: httpx.Response) -> ConnectorError:
    text = response.text.strip()
    message = f"SendGrid returned HTTP {response.status_code}"
    if text:
        message = f"{message}: {text[:500]}"
    status = response.status_code
    if status == 401:
        return AuthenticationError(message)
    if status == 403:
        return PermissionDeniedError(message)
    if status == 404:
        return NotFoundError(message)
    if status == 429:
        return RateLimitError(message, retry_after_seconds=_retry_after_seconds(response))
    if status >= 500:
        return TransientProviderError(message)
    return ConnectorError(message)
