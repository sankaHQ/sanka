# SPDX-License-Identifier: Apache-2.0
"""Review and construct a hosted template through the authenticated public API."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from uuid import UUID, uuid4

import click

import sanka_cli.runtime as runtime
from sanka_cli.output import resolve_output_format
from sanka_cli.state import CLIState


def create_from_template(
    state: CLIState,
    *,
    template_id: str,
    config: Path,
    template_version: int,
    request_id: UUID | None,
    approve_plan: str | None,
    plan_only: bool,
) -> None:
    if approve_plan is not None and (request_id is None or plan_only):
        raise click.UsageError("--approve-plan requires --request-id and cannot use --plan-only")
    if approve_plan is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", approve_plan):
        raise click.BadParameter(
            "Expected sha256: followed by 64 lowercase hex characters", param_hint="--approve-plan"
        )
    try:
        parameters = runtime.parse_json_input("@" + str(config))
    except (OSError, ValueError) as error:
        raise click.ClickException(f"Cannot read template JSON config: {error}") from error
    identity = str(request_id or uuid4())
    response = runtime.request_json(
        state,
        "POST",
        "/v2/public/workflows/templates/plan",
        json_body={
            "request_id": identity,
            "template_id": template_id,
            "template_version": template_version,
            "parameters": parameters,
        },
    )
    plan = response.get("data")
    if not isinstance(plan, dict) or (
        plan.get("request_id") != identity
        or plan.get("template_id") != template_id
        or plan.get("template_version") != template_version
        or plan.get("operation") != "create"
        or plan.get("workflow_id") is not None
        or plan.get("construction") != "inactive"
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(plan.get("plan_digest", "")))
    ):
        raise click.ClickException("Server returned a plan for a different or unsupported request")
    if approve_plan is not None and approve_plan != plan["plan_digest"]:
        raise click.ClickException(
            "Approved digest does not match the saved plan; review the new plan first"
        )
    if plan.get("applicable") is not True:
        runtime.emit_payload(response, state)
        raise click.ClickException("Resolve the plan blockers before construction")
    interactive = sys.stdin.isatty() and resolve_output_format(state.output) != "json"
    if plan_only or (approve_plan is None and not interactive):
        runtime.emit_payload(response, state)
        if not plan_only:
            click.echo(
                f"Review only. To construct, repeat with --request-id {identity} "
                f"--approve-plan {plan['plan_digest']}",
                err=True,
            )
        return
    if approve_plan is None:
        # Keep machine stdout to one final result while showing every reviewed
        # field before the human chooses construction in an interactive terminal.
        click.echo(json.dumps(plan, ensure_ascii=False, indent=2), err=True)
        if not click.confirm("Construct this inactive workflow?", default=False, err=True):
            runtime.emit_payload(response, state)
            return
    result = runtime.request_json(
        state,
        "POST",
        "/v2/public/workflows/templates/use",
        json_body={"request_id": identity, "plan_digest": plan["plan_digest"]},
    )
    runtime.emit_payload(result, state)
