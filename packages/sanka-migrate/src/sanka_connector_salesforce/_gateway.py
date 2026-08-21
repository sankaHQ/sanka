# SPDX-License-Identifier: Apache-2.0
"""Salesforce REST gateway — the HTTP surface the source connector needs.

A trimmed port of the production Salesforce gateway: only the read-only calls
the Sanka source adapter makes (SOQL queries, object describes, the sObject
catalog, and the active-user directory), rewritten onto
:class:`sanka.connector.Credentials`.

Authentication is an OAuth access token (``credentials.access_token``) against
the org's instance URL (``settings["instance_url"]``); both are required.
When the credentials also carry ``refresh_token`` + ``client_id`` +
``client_secret``, an expired token recovers automatically: the first 401
triggers one refresh-token grant against ``settings["auth_base_url"]``
(default ``https://login.salesforce.com``; sandboxes use
``https://test.salesforce.com``) and the request is retried with the fresh
token, which is then cached in-process per refresh token. Salesforce does not
rotate refresh tokens on this grant by default; if your connected app does,
persist :class:`TokenRefreshResult.refresh_token` yourself — the gateway has
no credential store to write back to.

HTTP failures map onto the Sanka error taxonomy: 401 →
:class:`AuthenticationError` (after any refresh attempt), 403 →
:class:`PermissionDeniedError`, 404 → :class:`NotFoundError`, 429 →
:class:`RateLimitError` (carrying ``Retry-After`` when the org sends it),
5xx and transport failures → :class:`TransientProviderError`, anything else →
:class:`ConnectorError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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

SALESFORCE_API_VERSION = "v60.0"
DEFAULT_AUTH_BASE_URL = "https://login.salesforce.com"
TOKEN_PATH = "/services/oauth2/token"
ACTIVE_USERS_QUERY = (
    "SELECT Id, Name, Email, IsActive FROM User WHERE IsActive = true ORDER BY Name"
)

_API_VERSION_RE = re.compile(r"^v\d+\.\d+$")


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenRefreshResult:
    """Result of a refresh-token grant; mirrors the token endpoint response."""

    access_token: str
    refresh_token: str | None = None
    instance_url: str | None = None
    issued_at: str | None = None
    scope: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class _Session:
    """Resolved request target: bearer token plus instance base URL."""

    access_token: str
    instance_url: str


class SalesforceGateway(Protocol):
    """The gateway surface :class:`SalesforceSource` depends on."""

    async def query(
        self,
        credentials: Credentials,
        *,
        soql: str,
        query_all: bool = True,
    ) -> dict[str, Any]: ...

    async def describe_object(
        self,
        credentials: Credentials,
        *,
        object_type: str,
    ) -> dict[str, Any]: ...

    async def list_sobjects(
        self,
        credentials: Credentials,
    ) -> list[dict[str, Any]]: ...

    async def list_active_users(
        self,
        credentials: Credentials,
    ) -> list[dict[str, Any]]: ...


class HttpSalesforceGateway:
    """Salesforce REST implementation of :class:`SalesforceGateway` on httpx."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        api_version: str = SALESFORCE_API_VERSION,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._api_version = api_version
        self._refreshed: dict[str, _Session] = {}

    async def query(
        self,
        credentials: Credentials,
        *,
        soql: str,
        query_all: bool = True,
    ) -> dict[str, Any]:
        endpoint = "queryAll" if query_all else "query"
        return await self._get_json(
            credentials,
            path=self._data_path(credentials, f"{endpoint}/"),
            params={"q": soql},
        )

    async def describe_object(
        self,
        credentials: Credentials,
        *,
        object_type: str,
    ) -> dict[str, Any]:
        return await self._get_json(
            credentials,
            path=self._data_path(credentials, f"sobjects/{object_type}/describe"),
        )

    async def list_sobjects(self, credentials: Credentials) -> list[dict[str, Any]]:
        payload = await self._get_json(credentials, path=self._data_path(credentials, "sobjects/"))
        rows = payload.get("sobjects")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    async def list_active_users(self, credentials: Credentials) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        path = self._data_path(credentials, "queryAll/")
        params: dict[str, str] | None = {"q": ACTIVE_USERS_QUERY}
        while path:
            payload = await self._get_json(credentials, path=path, params=params)
            records = payload.get("records", [])
            if isinstance(records, list):
                users.extend(row for row in records if isinstance(row, dict))
            # nextRecordsUrl is server-relative (/services/data/…), ready to
            # request against the same instance URL.
            path = str(payload.get("nextRecordsUrl") or "").strip()
            params = None
        return users

    async def refresh_access_token(self, credentials: Credentials) -> TokenRefreshResult:
        """Exchange the refresh token for a fresh access token and cache it."""
        refresh_token = str(credentials.refresh_token or "").strip()
        client_id = str(credentials.client_id or "").strip()
        client_secret = str(credentials.client_secret or "").strip()
        if not refresh_token or not client_id or not client_secret:
            raise ConfigurationError(
                "Salesforce token refresh needs refresh_token, client_id, and client_secret"
            )
        auth_base_url = (
            str(credentials.settings.get("auth_base_url") or DEFAULT_AUTH_BASE_URL)
            .strip()
            .rstrip("/")
        )
        if not auth_base_url:
            raise ConfigurationError("Salesforce auth base URL must not be empty")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{auth_base_url}{TOKEN_PATH}",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                    },
                )
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"Salesforce token refresh failed: {exc}") from exc
        if response.status_code >= 400:
            raise _mapped_refresh_error(response)
        data = _json_object(response)
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise AuthenticationError("Salesforce token response is missing access_token")
        result = TokenRefreshResult(
            access_token=access_token,
            refresh_token=str(data.get("refresh_token") or "").strip() or None,
            instance_url=str(data.get("instance_url") or "").strip() or None,
            issued_at=str(data.get("issued_at") or "").strip() or None,
            scope=str(data.get("scope") or "").strip() or None,
        )
        configured = str(credentials.settings.get("instance_url") or "").strip().rstrip("/")
        instance_url = (result.instance_url or configured).rstrip("/")
        if instance_url:
            self._refreshed[refresh_token] = _Session(
                access_token=result.access_token,
                instance_url=instance_url,
            )
        return result

    # -- internals ----------------------------------------------------------

    def _resolved_api_version(self, credentials: Credentials) -> str:
        raw = credentials.settings.get("api_version")
        if raw is None:
            return self._api_version
        version = str(raw).strip()
        if not _API_VERSION_RE.fullmatch(version):
            raise ConfigurationError(
                "settings['api_version'] must look like 'v60.0'"
                f" (got {version or 'an empty value'!r})"
            )
        return version

    def _data_path(self, credentials: Credentials, suffix: str) -> str:
        return f"/services/data/{self._resolved_api_version(credentials)}/{suffix}"

    def _session(self, credentials: Credentials) -> _Session:
        access_token = str(credentials.access_token or "").strip()
        instance_url = str(credentials.settings.get("instance_url") or "").strip().rstrip("/")
        if not access_token:
            raise ConfigurationError(
                "salesforce connector needs an OAuth access token in credentials.access_token"
            )
        if not instance_url:
            raise ConfigurationError(
                "salesforce connector needs the org instance URL in settings['instance_url']"
                " (for example https://yourcompany.my.salesforce.com)"
            )
        refresh_key = _refresh_key(credentials)
        if refresh_key is not None:
            cached = self._refreshed.get(refresh_key)
            if cached is not None:
                return cached
        return _Session(access_token=access_token, instance_url=instance_url)

    async def _get_json(
        self,
        credentials: Credentials,
        *,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        session = self._session(credentials)
        response = await self._send(session, path=path, params=params)
        if response.status_code == 401 and _refresh_key(credentials) is not None:
            await self.refresh_access_token(credentials)
            session = self._session(credentials)
            response = await self._send(session, path=path, params=params)
        if response.status_code >= 400:
            raise _mapped_http_error(response)
        return _json_object(response)

    async def _send(
        self,
        session: _Session,
        *,
        path: str,
        params: dict[str, str] | None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                return await client.get(
                    f"{session.instance_url}{path}",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {session.access_token}",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"Salesforce request failed: {exc}") from exc


def _refresh_key(credentials: Credentials) -> str | None:
    """The refresh-cache key, when the credentials can run a refresh grant."""
    refresh_token = str(credentials.refresh_token or "").strip()
    client_id = str(credentials.client_id or "").strip()
    client_secret = str(credentials.client_secret or "").strip()
    if refresh_token and client_id and client_secret:
        return refresh_token
    return None


def _error_message(response: httpx.Response) -> str:
    text = response.text.strip()
    if text:
        return f"Salesforce returned HTTP {response.status_code}: {text[:500]}"
    return f"Salesforce returned HTTP {response.status_code}"


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
    message = _error_message(response)
    status = response.status_code
    if status == 401:
        return AuthenticationError(
            message,
            remediation=(
                "the access token was rejected; provide a fresh token, or supply"
                " refresh_token + client_id + client_secret so the connector can refresh it"
            ),
        )
    if status == 403:
        return PermissionDeniedError(message)
    if status == 404:
        return NotFoundError(message)
    if status == 429:
        return RateLimitError(message, retry_after_seconds=_retry_after_seconds(response))
    if status >= 500:
        return TransientProviderError(message)
    return ConnectorError(message)


def _mapped_refresh_error(response: httpx.Response) -> ConnectorError:
    message = _error_message(response)
    status = response.status_code
    if status == 429:
        return RateLimitError(message, retry_after_seconds=_retry_after_seconds(response))
    if status >= 500:
        return TransientProviderError(message)
    return AuthenticationError(
        message,
        remediation="the refresh-token grant was rejected; reauthorize the connected app",
    )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DataError(
            f"Salesforce returned HTTP {response.status_code} with a non-JSON body"
        ) from exc
    if not isinstance(payload, dict):
        raise DataError(
            f"Salesforce returned HTTP {response.status_code} with an unexpected JSON shape"
        )
    return payload
