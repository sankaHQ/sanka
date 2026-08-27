# SPDX-License-Identifier: Apache-2.0
"""NetSuite SuiteTalk REST gateway — the HTTP surface the source connector needs.

Only the SuiteQL query service is used: every discovery, count, and read the
source performs is a ``POST {api_base_url}/services/rest/query/v1/suiteql``
request carrying the documented ``Prefer: transient`` header, a JSON body of
``{"q": "SELECT ..."}``, and ``limit``/``offset`` paging parameters.

Authentication is a bearer token (``credentials.access_token``) against the
account's API base URL (``settings["api_base_url"]``, for example
``https://<account>.suitetalk.api.netsuite.com``); embedders may point the
connector at a contract-compatible isolated environment through the same
setting. When the credentials also carry ``client_id`` + ``client_secret``,
the OAuth 2.0 client-credentials grant recovers an expired or absent token
automatically: the token endpoint (``settings["token_url"]``, default
``{api_base_url}/services/rest/auth/oauth2/v1/token``) is called once, the
request is retried with the fresh token, and the token is cached in-process
per client credential pair. NetSuite's production client-credentials flow
signs a JWT client assertion; environments that require the assertion flow
should mint the access token outside the connector and supply it directly.

HTTP failures map onto the Sanka error taxonomy: 401 →
:class:`AuthenticationError` (after any grant attempt), 403 →
:class:`PermissionDeniedError`, 404 → :class:`NotFoundError`, 429 →
:class:`RateLimitError` (carrying ``Retry-After`` when the account sends it),
5xx and transport failures → :class:`TransientProviderError`, anything else →
:class:`ConnectorError`. NetSuite's RFC 7807-style error body is read for its
``o:errorDetails[].o:errorCode`` and detail text, which are folded into the
raised message.
"""

from __future__ import annotations

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

SUITEQL_PATH = "/services/rest/query/v1/suiteql"
DEFAULT_TOKEN_PATH = "/services/rest/auth/oauth2/v1/token"
MAX_PAGE_SIZE = 1000


def netsuite_api_base_url(credentials: Credentials) -> str:
    """Return the configured account API origin.

    NetSuite API hosts are account-scoped, so there is no production default;
    ``settings["api_base_url"]`` is required. The override is kept in
    credentials rather than module state so concurrent tenants never share a
    mutable target.
    """

    raw = str(credentials.settings.get("api_base_url") or "").strip()
    if not raw:
        raise ConfigurationError(
            "netsuite connector needs the account API base URL in settings['api_base_url']"
            " (for example https://<account>.suitetalk.api.netsuite.com)"
        )
    try:
        parsed = httpx.URL(raw)
    except ValueError as exc:
        raise ConfigurationError("NetSuite api_base_url is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ConfigurationError("NetSuite api_base_url must be an absolute HTTP(S) URL")
    return raw.rstrip("/")


class NetSuiteGateway(Protocol):
    """The gateway surface :class:`NetSuiteSource` depends on."""

    async def suiteql(
        self,
        credentials: Credentials,
        *,
        query: str,
        limit: int,
        offset: int = 0,
    ) -> dict[str, Any]: ...


class HttpNetSuiteGateway:
    """SuiteTalk REST implementation of :class:`NetSuiteGateway` on httpx."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._granted: dict[tuple[str, str], str] = {}

    async def suiteql(
        self,
        credentials: Credentials,
        *,
        query: str,
        limit: int,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        safe_offset = max(0, int(offset))
        return await self._post_json(
            credentials,
            path=SUITEQL_PATH,
            params={"limit": str(safe_limit), "offset": str(safe_offset)},
            body={"q": query},
        )

    async def request_access_token(self, credentials: Credentials) -> str:
        """Run one client-credentials grant and cache the returned token."""
        grant_key = _grant_key(credentials)
        if grant_key is None:
            raise ConfigurationError(
                "NetSuite token grant needs client_id and client_secret in the credentials"
            )
        token_url = _token_url(credentials)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": grant_key[0],
                        "client_secret": grant_key[1],
                    },
                )
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"NetSuite token request failed: {exc}") from exc
        if response.status_code >= 400:
            raise _mapped_grant_error(response)
        data = _json_object(response)
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise AuthenticationError("NetSuite token response is missing access_token")
        self._granted[grant_key] = access_token
        return access_token

    # -- internals ----------------------------------------------------------

    async def _resolved_token(self, credentials: Credentials) -> str:
        grant_key = _grant_key(credentials)
        if grant_key is not None:
            cached = self._granted.get(grant_key)
            if cached is not None:
                return cached
        access_token = str(credentials.access_token or "").strip()
        if access_token:
            return access_token
        if grant_key is not None:
            return await self.request_access_token(credentials)
        raise ConfigurationError(
            "netsuite connector needs an access token in credentials.access_token, or"
            " client_id + client_secret for the client-credentials grant"
        )

    async def _post_json(
        self,
        credentials: Credentials,
        *,
        path: str,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        base_url = netsuite_api_base_url(credentials)
        token = await self._resolved_token(credentials)
        response = await self._send(base_url, token, path=path, params=params, body=body)
        if response.status_code == 401 and _grant_key(credentials) is not None:
            token = await self.request_access_token(credentials)
            response = await self._send(base_url, token, path=path, params=params, body=body)
        if response.status_code >= 400:
            raise _mapped_http_error(response)
        return _json_object(response)

    async def _send(
        self,
        base_url: str,
        access_token: str,
        *,
        path: str,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                return await client.post(
                    f"{base_url}{path}",
                    params=params,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                        # Required by the SuiteQL service; requests without it
                        # are rejected outright.
                        "Prefer": "transient",
                    },
                )
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"NetSuite request failed: {exc}") from exc


def _grant_key(credentials: Credentials) -> tuple[str, str] | None:
    """The grant-cache key, when the credentials can run a token grant."""
    client_id = str(credentials.client_id or "").strip()
    client_secret = str(credentials.client_secret or "").strip()
    if client_id and client_secret:
        return (client_id, client_secret)
    return None


def _token_url(credentials: Credentials) -> str:
    configured = str(credentials.settings.get("token_url") or "").strip()
    if configured:
        try:
            parsed = httpx.URL(configured)
        except ValueError as exc:
            raise ConfigurationError("NetSuite token_url is not a valid URL") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ConfigurationError("NetSuite token_url must be an absolute HTTP(S) URL")
        return configured
    return f"{netsuite_api_base_url(credentials)}{DEFAULT_TOKEN_PATH}"


def _error_message(response: httpx.Response) -> str:
    detail = _error_detail(response)
    if detail:
        return f"NetSuite returned HTTP {response.status_code}: {detail}"
    text = response.text.strip()
    if text:
        return f"NetSuite returned HTTP {response.status_code}: {text[:500]}"
    return f"NetSuite returned HTTP {response.status_code}"


def _error_detail(response: httpx.Response) -> str | None:
    """Fold NetSuite's ``o:errorDetails`` entries into one readable line."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    details = payload.get("o:errorDetails")
    if not isinstance(details, list):
        return None
    parts: list[str] = []
    for entry in details:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("o:errorCode") or "").strip()
        text = str(entry.get("detail") or "").strip()
        if code and text:
            parts.append(f"{code}: {text}")
        elif code or text:
            parts.append(code or text)
    return "; ".join(parts) or None


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
                " client_id + client_secret so the connector can run the"
                " client-credentials grant"
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


def _mapped_grant_error(response: httpx.Response) -> ConnectorError:
    message = _error_message(response)
    status = response.status_code
    if status == 429:
        return RateLimitError(message, retry_after_seconds=_retry_after_seconds(response))
    if status >= 500:
        return TransientProviderError(message)
    return AuthenticationError(
        message,
        remediation="the client-credentials grant was rejected; check client_id and client_secret",
    )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DataError(
            f"NetSuite returned HTTP {response.status_code} with a non-JSON body"
        ) from exc
    if not isinstance(payload, dict):
        raise DataError(
            f"NetSuite returned HTTP {response.status_code} with an unexpected JSON shape"
        )
    return payload
