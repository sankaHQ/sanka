# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import click

import sanka_cli.runtime as runtime
from sanka_cli.state import CLIState


@click.group()
def profiles() -> None:
    """Profile commands."""


@profiles.command("list")
@click.pass_obj
def profiles_list(state: CLIState) -> None:
    try:
        payload = runtime.list_profiles()
    except runtime.CredentialStoreError as exc:
        raise click.ClickException(str(exc)) from exc
    runtime.emit_payload({"data": payload}, state)


@profiles.command("use")
@click.argument("profile_name")
@click.pass_obj
def profiles_use(state: CLIState, profile_name: str) -> None:
    config = runtime.set_active_profile(profile_name)
    runtime.emit_payload(
        {
            "message": "active_profile_updated",
            "active_profile": config["active_profile"],
        },
        state,
    )
