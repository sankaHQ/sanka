# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import click

import sanka_cli.runtime as runtime
from sanka_cli.state import CLIState


def _resource_list_command(path: str):
    @click.command("list")
    @click.option("--page", default=1, show_default=True, type=int)
    @click.option("--limit", default=50, show_default=True, type=int)
    @click.pass_obj
    def command(state: CLIState, page: int, limit: int) -> None:
        payload = runtime.request_json(
            state,
            "GET",
            path,
            params={"page": page, "limit": limit},
        )
        runtime.emit_payload(payload, state)

    return command


def _resource_get_command(path: str):
    @click.command("get")
    @click.argument("record_id")
    @click.option("--external-id", default=None)
    @click.pass_obj
    def command(state: CLIState, record_id: str, external_id: str | None) -> None:
        params = {"external_id": external_id} if external_id else None
        payload = runtime.request_json(
            state,
            "GET",
            f"{path}/{record_id}",
            params=params,
        )
        runtime.emit_payload(payload, state)

    return command


def _resource_create_command(path: str):
    @click.command("create")
    @click.option("--data", required=True, help="JSON string or @path/to/file.json")
    @click.pass_obj
    def command(state: CLIState, data: str) -> None:
        payload = runtime.request_json(
            state,
            "POST",
            path,
            json_body=runtime.parse_json_input(data),
        )
        runtime.emit_payload(payload, state)

    return command


def _resource_update_command(path: str):
    @click.command("update")
    @click.argument("record_id")
    @click.option("--data", required=True, help="JSON string or @path/to/file.json")
    @click.option("--external-id", default=None)
    @click.pass_obj
    def command(
        state: CLIState,
        record_id: str,
        data: str,
        external_id: str | None,
    ) -> None:
        params = {"external_id": external_id} if external_id else None
        payload = runtime.request_json(
            state,
            "PUT",
            f"{path}/{record_id}",
            params=params,
            json_body=runtime.parse_json_input(data),
        )
        runtime.emit_payload(payload, state)

    return command


def _resource_delete_command(path: str):
    @click.command("delete")
    @click.argument("record_id")
    @click.option("--external-id", default=None)
    @click.pass_obj
    def command(state: CLIState, record_id: str, external_id: str | None) -> None:
        params = {"external_id": external_id} if external_id else None
        payload = runtime.request_json(
            state,
            "DELETE",
            f"{path}/{record_id}",
            params=params,
        )
        runtime.emit_payload(payload, state)

    return command


def attach_resource_group(root: click.Group, name: str, path: str) -> None:
    @root.group(name=name)
    def resource_group() -> None:
        """Resource commands."""

    resource_group.add_command(_resource_list_command(path))
    resource_group.add_command(_resource_get_command(path))
    resource_group.add_command(_resource_create_command(path))
    resource_group.add_command(_resource_update_command(path))
    resource_group.add_command(_resource_delete_command(path))
