# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from typing import Any

import httpx

from sanka_cli import __version__


class APIError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        ctx_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.ctx_id = ctx_id
        self.payload = payload or {}

    def display_message(self) -> str:
        if self.ctx_id:
            return f"{self.message} (ctx_id={self.ctx_id})"
        return self.message


class SankaApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def _headers(self, *, include_auth: bool = True) -> dict[str, str]:
        headers = {
            "User-Agent": f"sanka-cli/{__version__}",
            "X-Sanka-CLI-Version": __version__,
        }
        if include_auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _raise_for_response(self, response: httpx.Response) -> None:
        try:
            payload = response.json()
        except Exception:
            payload = {}

        message = ""
        ctx_id = None
        if isinstance(payload, dict):
            error = payload.get("error")
            error = error if isinstance(error, dict) else {}
            meta = payload.get("meta")
            meta = meta if isinstance(meta, dict) else {}
            message = str(
                payload.get("message")
                or payload.get("detail")
                or error.get("message")
                or ""
            ).strip()
            ctx_id = payload.get("ctx_id") or meta.get("ctx_id")
        if not message:
            message = response.text.strip() or f"HTTP {response.status_code}"

        raise APIError(
            status_code=response.status_code,
            message=message,
            ctx_id=ctx_id,
            payload=payload if isinstance(payload, dict) else {},
        )

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        """Fetch a non-JSON body (currently only the Custom Code bundle download).

        Returns headers alongside the payload so the caller can verify the digest the
        server reports without a second request.
        """
        response = self.client.request(
            method.upper(),
            path,
            headers=self._headers(),
            params=params,
        )
        if response.status_code >= 400:
            self._raise_for_response(response)
        return response.content, dict(response.headers)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        request_headers = self._headers()
        request_headers.update(headers or {})
        response = self.client.request(
            method.upper(),
            path,
            headers=request_headers,
            params=params,
            json=json_body,
        )
        _ = allow_refresh
        if response.status_code >= 400:
            self._raise_for_response(response)

        if not response.content:
            return {}
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise APIError(
                status_code=response.status_code,
                message="API returned invalid JSON",
            ) from exc
