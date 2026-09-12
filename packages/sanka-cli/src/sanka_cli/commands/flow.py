# SPDX-License-Identifier: Apache-2.0
"""Generate native Sanka workflows from the hosted Studio template catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import click

import sanka_cli.runtime as runtime
from sanka_cli.state import CLIState

TEMPLATES = "/v2/workflows/templates"


@dataclass(frozen=True)
class FlowState:
    cli: CLIState
    workspace: str


def _request(
    state: FlowState, method: str, path: str, *, body: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = runtime.request_json(
        state.cli,
        method,
        path,
        json_body=body,
        headers={
            "X-Workspace-Code": state.workspace,
            "X-Sanka-Expected-Workspace-ID": state.workspace,
        },
    )
    data = payload.get("data", payload)
    if not isinstance(data, dict) or data.get("workspace_id") != state.workspace:
        raise click.ClickException("Flow response does not match the requested workspace.")
    return payload, data


@click.group()
@click.option("--workspace", required=True, type=click.UUID, help="Exact workspace UUID.")
@click.pass_context
def flow(ctx: click.Context, workspace: UUID) -> None:
    """Choose a category and template to generate a native workflow."""
    state = ctx.find_object(CLIState)
    assert state is not None
    ctx.obj = FlowState(state, str(workspace))


@flow.command("categories")
@click.pass_obj
def categories(state: FlowState) -> None:
    """List workflow categories available in this workspace."""
    payload, data = _request(state, "GET", TEMPLATES)
    runtime.emit_payload(
        {
            **payload,
            "data": {"workspace_id": state.workspace, "categories": data.get("categories", [])},
        },
        state.cli,
    )


@flow.command("templates")
@click.option("--category", help="Category ID returned by categories, for example sales.")
@click.pass_obj
def templates(state: FlowState, category: str | None) -> None:
    """List templates, optionally within one workflow category."""
    payload, data = _request(state, "GET", TEMPLATES)
    entries = data.get("templates", [])
    if category is not None:
        if not any(row.get("id") == category for row in data.get("categories", [])):
            raise click.BadParameter("Unknown workflow category.", param_hint="--category")
        entries = [row for row in entries if row.get("category_id") == category]
    runtime.emit_payload(
        {**payload, "data": {"workspace_id": state.workspace, "templates": entries}}, state.cli
    )


@flow.command("generate")
@click.argument("template_id")
@click.option("--request-id", type=click.UUID, help="Reuse this UUID to recover a lost response.")
@click.option("--language", type=click.Choice(["en", "ja"]), default="en", show_default=True)
@click.pass_obj
def generate(state: FlowState, template_id: str, request_id: UUID | None, language: str) -> None:
    """Generate a configured, inactive workflow; manage it in Workflows."""
    if not template_id.strip():
        raise click.BadParameter("Template ID cannot be empty.", param_hint="TEMPLATE_ID")
    identity = str(request_id or uuid4())
    # Print before the write so a disconnected caller can retry with the same identity.
    click.echo(f"Generation request ID: {identity} (reuse --request-id to retry)", err=True)
    payload, data = _request(
        state,
        "POST",
        f"{TEMPLATES}/use",
        body={
            "template_id": template_id,
            "request_id": identity,
            "language": language,
        },
    )
    try:
        UUID(str(data["workflow_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise click.ClickException(
            "Generation response did not identify a native workflow."
        ) from exc
    runtime.emit_payload({**payload, "data": {**data, "request_id": identity}}, state.cli)
