# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import time

import click

import sanka_cli.runtime as runtime
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
@click.option("--data", required=True, help="JSON string or @path/to/file.json")
@click.pass_obj
def workflows_create(state: CLIState, data: str) -> None:
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
        if (
            str(status_data.get("status") or "").lower()
            in runtime.TERMINAL_WORKFLOW_RUN_STATUSES
        ):
            runtime.emit_payload(status_payload, state)
            return
        time.sleep(max(poll_interval, 0.1))

    runtime.emit_payload(last_payload, state)
    raise click.ClickException("Workflow run timed out while waiting for completion")
