# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import time
from pathlib import Path
from uuid import UUID

import click

import sanka_cli.runtime as runtime
from sanka_cli.commands.workflow_templates import create_from_template
from sanka_cli.state import CLIState


@click.group()
def workflows() -> None:
    """Workflow commands."""


@workflows.command("list")
@click.option("--page", default=1, show_default=True, type=int)
@click.option("--limit", default=50, show_default=True, type=int)
@click.pass_obj
def workflows_list(state: CLIState, page: int, limit: int) -> None:
    payload = runtime.request_json(
        state,
        "GET",
        "/v2/public/workflows",
        params={"page": page, "limit": limit},
    )
    runtime.emit_payload(payload, state)


@workflows.command("get")
@click.argument("workflow_ref")
@click.pass_obj
def workflows_get(state: CLIState, workflow_ref: str) -> None:
    payload = runtime.request_json(
        state,
        "GET",
        f"/v2/public/workflows/{workflow_ref}",
    )
    runtime.emit_payload(payload, state)


@workflows.command("create")
@click.option("--data", help="Existing raw workflow JSON string or @path/to/file.json")
@click.option("--template", "template_id", help="Shared business template ID")
@click.option(
    "--config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Template parameters as a JSON file",
)
@click.option("--template-version", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--request-id", type=click.UUID, help="Keep this UUID for plan and create retries")
@click.option("--approve-plan", help="Exact approved plan digest; requires --request-id")
@click.option("--plan-only", is_flag=True, help="Review without creating a workflow")
@click.pass_obj
def workflows_create(
    state: CLIState,
    data: str | None,
    template_id: str | None,
    config: Path | None,
    template_version: int,
    request_id: UUID | None,
    approve_plan: str | None,
    plan_only: bool,
) -> None:
    if template_id is not None:
        if data is not None:
            raise click.UsageError("Use either --data or --template with --config")
        if config is None:
            raise click.UsageError("--template requires --config")
        create_from_template(
            state,
            template_id=template_id,
            config=config,
            template_version=template_version,
            request_id=request_id,
            approve_plan=approve_plan,
            plan_only=plan_only,
        )
        return
    if config is not None or request_id is not None or approve_plan is not None or plan_only:
        raise click.UsageError("Template options require --template")
    if data is None:
        raise click.UsageError("Provide --data or --template with --config")
    payload = runtime.request_json(
        state,
        "POST",
        "/v2/public/workflows",
        json_body=runtime.parse_json_input(data),
    )
    runtime.emit_payload(payload, state)


@workflows.command("update")
@click.option("--data", required=True, help="JSON string or @path/to/file.json")
@click.pass_obj
def workflows_update(state: CLIState, data: str) -> None:
    payload = runtime.request_json(
        state,
        "POST",
        "/v2/public/workflows",
        json_body=runtime.parse_json_input(data),
    )
    runtime.emit_payload(payload, state)


@workflows.command("run")
@click.argument("workflow_ref")
@click.option("--wait/--no-wait", default=False, show_default=True)
@click.option("--poll-interval", default=2.0, show_default=True, type=float)
@click.option("--timeout", default=60.0, show_default=True, type=float)
@click.pass_obj
def workflows_run(
    state: CLIState,
    workflow_ref: str,
    wait: bool,
    poll_interval: float,
    timeout: float,
) -> None:
    payload = runtime.request_json(
        state,
        "POST",
        f"/v2/public/workflows/{workflow_ref}/run",
    )
    data = payload.get("data", payload)
    if not wait:
        runtime.emit_payload(payload, state)
        return

    deadline = time.time() + max(timeout, 1.0)
    run_id = str(data["run_id"])
    last_payload = payload
    while time.time() < deadline:
        status_payload = runtime.request_json(
            state,
            "GET",
            f"/v2/public/workflow-runs/{run_id}",
        )
        last_payload = status_payload
        status_data = status_payload.get("data", status_payload)
        if str(status_data.get("status") or "").lower() in runtime.TERMINAL_WORKFLOW_RUN_STATUSES:
            runtime.emit_payload(status_payload, state)
            return
        time.sleep(max(poll_interval, 0.1))

    runtime.emit_payload(last_payload, state)
    raise click.ClickException("Workflow run timed out while waiting for completion")
