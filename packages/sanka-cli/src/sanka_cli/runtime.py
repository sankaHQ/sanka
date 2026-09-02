# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import click
import httpx

from sanka_cli.client import APIError, SankaApiClient
from sanka_cli.config import CredentialStoreError as CredentialStoreError
from sanka_cli.config import (
    clear_tokens,
    list_profiles,
    resolve_runtime,
    set_active_profile,
    store_tokens,
    upsert_profile,
)
from sanka_cli.output import print_payload, resolve_output_format
from sanka_cli.state import CLIState

TERMINAL_WORKFLOW_RUN_STATUSES = {
    "success",
    "failed",
    "canceled",
    "skipped",
    "stop_trigger",
}


class TokenVerificationError(Exception):
    """The API explicitly rejected the supplied access token."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def verify_access_token(
    *,
    base_url: str,
    access_token: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Confirm an access token against whoami before it is persisted.

    Returns (identity, None) when the token is verified, or (None, warning)
    when the API could not be reached or errored without rejecting the token —
    so login still works offline. Raises TokenVerificationError when the API
    rejects the token (401/403).
    """
    client = SankaApiClient(base_url=base_url, access_token=access_token)
    try:
        payload = client.request_json("GET", "/v2/public/auth/whoami")
    except APIError as exc:
        if exc.status_code in (401, 403):
            raise TokenVerificationError(
                f"{base_url} rejected this access token "
                f"(HTTP {exc.status_code}: {exc.display_message()}).",
                status_code=exc.status_code,
            ) from exc
        return (
            None,
            f"token saved without verification: {base_url} responded HTTP {exc.status_code}",
        )
    except httpx.HTTPError as exc:
        return (
            None,
            f"token saved without verification: could not reach {base_url} "
            f"({exc.__class__.__name__})",
        )
    finally:
        client.close()

    data = payload.get("data", payload)
    return (data if isinstance(data, dict) else {}), None


def parse_json_input(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    raw_value = str(raw).strip()
    if not raw_value:
        return {}
    if raw_value.startswith("@"):
        file_path = Path(raw_value[1:]).expanduser()
        raw_value = file_path.read_text(encoding="utf-8")
    parsed = json.loads(raw_value)
    if not isinstance(parsed, dict):
        raise click.ClickException("JSON input must be an object")
    return parsed


def build_client(state: CLIState) -> SankaApiClient:
    try:
        runtime = resolve_runtime(
            profile_name=state.profile,
            base_url_override=state.base_url,
        )
    except CredentialStoreError as exc:
        raise click.ClickException(str(exc)) from exc
    access_token = runtime["access_token"]
    if not access_token:
        raise click.ClickException("No access token configured. Run `sanka login` first.")

    return SankaApiClient(
        base_url=runtime["base_url"],
        access_token=access_token,
    )


def handle_api_error(exc: APIError) -> NoReturn:
    raise click.ClickException(exc.display_message()) from exc


def emit_payload(payload: Any, state: CLIState) -> None:
    print_payload(payload, output_format=resolve_output_format(state.output))


def request_json(
    state: CLIState,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    client = build_client(state)
    try:
        return client.request_json(
            method,
            path,
            params=params,
            json_body=json_body,
            headers=headers,
        )
    except APIError as exc:
        handle_api_error(exc)
    finally:
        client.close()


def request_bytes(
    state: CLIState,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> tuple[bytes, dict[str, str]]:
    client = build_client(state)
    try:
        return client.request_bytes(method, path, params=params)
    except APIError as exc:
        handle_api_error(exc)
        raise
    finally:
        client.close()


__all__ = [
    "TERMINAL_WORKFLOW_RUN_STATUSES",
    "CLIState",
    "TokenVerificationError",
    "clear_tokens",
    "emit_payload",
    "list_profiles",
    "parse_json_input",
    "request_bytes",
    "request_json",
    "resolve_runtime",
    "set_active_profile",
    "store_tokens",
    "upsert_profile",
    "verify_access_token",
]
