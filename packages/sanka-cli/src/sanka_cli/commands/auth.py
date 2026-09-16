# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import click

import sanka_cli.runtime as runtime
from sanka_cli.config import DEFAULT_BASE_URL
from sanka_cli.state import CLIState


@click.group()
def auth() -> None:
    """Authentication commands."""


@auth.command("login")
@click.option(
    "--access-token",
    prompt="Developer API access token",
    hide_input=True,
    help="Developer API access token (hidden prompt when omitted).",
)
@click.option(
    "--refresh-token",
    default=None,
    help="Deprecated legacy refresh token. V2 public API uses bearer tokens.",
)
@click.option("--profile", "profile_name", default=None, help="Profile name to save.")
@click.option("--base-url", default=None, help="API base URL to store for the profile.")
@click.pass_obj
def auth_login(
    state: CLIState,
    access_token: str,
    refresh_token: str | None,
    profile_name: str | None,
    base_url: str | None,
) -> None:
    """Verify an access token against the API and store it for the profile."""
    resolved_profile_name = profile_name or state.profile or "default"
    resolved_base_url = (base_url or state.base_url or DEFAULT_BASE_URL).rstrip("/")
    try:
        identity, verify_warning = runtime.verify_access_token(
            base_url=resolved_base_url,
            access_token=access_token,
        )
    except runtime.TokenVerificationError as exc:
        raise click.ClickException(
            f"{exc} Nothing was saved. Create a token under Developers → API in Sanka and retry."
        ) from exc
    runtime.upsert_profile(resolved_profile_name, base_url=resolved_base_url)
    try:
        runtime.store_tokens(
            resolved_profile_name,
            access_token=access_token,
            refresh_token=refresh_token,
        )
    except runtime.CredentialStoreError as exc:
        raise click.ClickException(str(exc)) from exc
    payload: dict[str, object] = {
        "message": "logged_in" if identity is not None else "saved",
    }
    if identity:
        payload.update(_identity_fields(identity))
    payload["profile"] = resolved_profile_name
    payload["base_url"] = resolved_base_url
    if verify_warning:
        payload["warning"] = verify_warning
    runtime.emit_payload(payload, state)


def _identity_fields(identity: dict[str, object]) -> dict[str, str]:
    workspace_name = str(identity.get("workspace_name") or "").strip()
    workspace_code = str(identity.get("workspace_code") or "").strip()
    if workspace_name and workspace_code:
        workspace = f"{workspace_name} ({workspace_code})"
    else:
        workspace = workspace_name or workspace_code
    fields = {
        "workspace": workspace,
        "user": str(identity.get("email") or identity.get("username") or "").strip(),
        "token": str(identity.get("token_name") or "").strip(),
        "permission_level": str(identity.get("permission_level") or "").strip(),
    }
    return {key: value for key, value in fields.items() if value}


@auth.command("status")
@click.pass_obj
def auth_status(state: CLIState) -> None:
    try:
        resolved = runtime.resolve_runtime(
            profile_name=state.profile,
            base_url_override=state.base_url,
        )
    except runtime.CredentialStoreError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = runtime.request_json(state, "GET", "/v2/public/auth/whoami")
    data = payload.get("data", payload)
    data["profile"] = resolved["profile_name"]
    data["base_url"] = resolved["base_url"]
    runtime.emit_payload(data, state)


@auth.command("logout")
@click.option("--profile", "profile_name", default=None, help="Profile name to clear.")
@click.pass_obj
def auth_logout(state: CLIState, profile_name: str | None) -> None:
    resolved_profile_name = profile_name or state.profile or "default"
    runtime.clear_tokens(resolved_profile_name)
    runtime.emit_payload(
        {
            "message": "logged_out",
            "profile": resolved_profile_name,
        },
        state,
    )
