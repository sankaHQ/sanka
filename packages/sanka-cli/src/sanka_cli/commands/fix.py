# SPDX-License-Identifier: Apache-2.0
"""Authenticated Sanka Fix client. Execution remains in Sanka Cloud."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

import click
import httpx

import sanka_cli.runtime as runtime
from sanka_cli.commands.cloud import ROOT, WORKSPACE, _data, _digest, _headers, _intent_key, _read
from sanka_cli.config import config_path
from sanka_cli.state import CLIState


def _interactive() -> bool:
    return sys.stdin.isatty()


def _intent_path(binding: dict[str, Any], key: str | None) -> Path:
    fingerprint = hashlib.sha256(json.dumps([binding, key], sort_keys=True).encode()).hexdigest()
    return config_path().parent / "fix-intents" / f"{fingerprint}.json"


def _saved_intent(binding: dict[str, Any], key: str | None) -> dict[str, Any] | None:
    path = _intent_path(binding, key)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise click.ClickException(
            "Cannot read saved Fix intent; do not submit a new intent"
        ) from exc
    if (
        not isinstance(saved, dict)
        or not isinstance(saved.get("request"), dict)
        or not isinstance(saved.get("idempotency_key"), str)
        or not isinstance(saved["request"].get("candidate_sha256"), str)
        or saved.get("binding") != binding
        or type(saved["request"].get("max_credits")) is not int
        or saved["request"]["max_credits"] != binding["max_credits"]
    ):
        raise click.ClickException("Invalid saved Fix intent; do not submit a new intent")
    _intent_key(saved["idempotency_key"])
    _digest(saved["request"]["candidate_sha256"])
    return saved


def _save_intent(binding: dict[str, Any], body: dict[str, Any], key: str | None) -> dict[str, Any]:
    """Persist consent and exact body before sending, atomically even for concurrent clients."""
    path = _intent_path(binding, key)
    actual_key = key or str(uuid4())
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = directory / f".{uuid4()}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump({"idempotency_key": actual_key, "binding": binding, "request": body}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        with suppress(FileExistsError):
            os.link(temporary, path)
        saved = _saved_intent(binding, key)
        assert saved is not None
        # An explicit retry of an automatically generated key finds the same durable request.
        with suppress(FileExistsError):
            os.link(path, _intent_path(binding, saved["idempotency_key"]))
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return saved
    except OSError as exc:
        raise click.ClickException("Cannot durably save Fix intent; nothing was submitted") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _exit_code(run: dict[str, Any]) -> int | None:
    status = run.get("status")
    if status not in {"succeeded", "failed", "cancelled"}:
        return None
    outcome = (run.get("fix_result") or {}).get("outcome")
    if status == "cancelled" or outcome == "cancelled":
        return 5
    if status == "succeeded" and outcome == "checks_passed":
        return 0
    if outcome in {"setup_required", "service_error"}:
        return 4
    if outcome in {"needs_review", "budget_reached"} or status == "succeeded":
        return 3
    return 4


def _follow(state: CLIState, workspace: str, run: dict[str, Any], seconds: int) -> None:
    deadline = time.monotonic() + seconds
    code = _exit_code(run)
    previous_status = run.get("status")
    if _interactive():
        click.echo(f"Fix status: {previous_status}", err=True)
    try:
        while code is None and time.monotonic() < deadline:
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            run = _data(_read(state, workspace, f"{ROOT}/{run['id']}"))
            code = _exit_code(run)
            if _interactive() and run.get("status") != previous_status:
                previous_status = run.get("status")
                click.echo(f"Fix status: {previous_status}", err=True)
    except KeyboardInterrupt:
        run["client_state"] = "detached"
        runtime.emit_payload(run, state)
        raise SystemExit(130) from None
    run["client_state"] = "settled" if code is not None else "wait_timeout"
    run["receipt_command"] = f"sanka cloud receipt --workspace {workspace} {run['id']}"
    runtime.emit_payload(run, state)
    raise SystemExit(code if code is not None else 6)


@click.command()
@click.option("--cloud", "use_cloud", is_flag=True, help="Execute in Sanka Cloud (required).")
@WORKSPACE
@click.option(
    "--run", "parent_id", required=True, type=click.UUID, help="Existing Code migration run."
)
@click.option(
    "--max-credits", required=True, type=click.IntRange(min=1), help="Maximum total credits."
)
@click.option(
    "--yes", is_flag=True, help="Consent to cloud repository excerpts and the credit hold."
)
@click.option(
    "--idempotency-key", default=None, help="Required with --yes; reuse after uncertain responses."
)
@click.option(
    "--wait", is_flag=True, help="Follow the accepted run; Ctrl-C detaches without cancelling."
)
@click.option("--wait-timeout", default=900, show_default=True, type=click.IntRange(1, 3600))
@click.pass_obj
def fix(
    state: CLIState,
    use_cloud: bool,
    workspace: str,
    parent_id: Any,
    max_credits: int,
    yes: bool,
    idempotency_key: str | None,
    wait: bool,
    wait_timeout: int,
) -> None:
    """Run optional paid Sanka Fix on an eligible retained Sanka Code migration."""
    if not use_cloud:
        raise click.UsageError(
            "Sanka Fix executes in Sanka Cloud. Add --cloud; run sanka login first."
        )
    headers = _headers(workspace)
    if not yes and not _interactive():
        raise click.UsageError("Noninteractive Fix requires --yes and --idempotency-key.")
    if yes and not idempotency_key:
        raise click.UsageError("Automation requires --yes, --max-credits and --idempotency-key.")
    if idempotency_key:
        _intent_key(idempotency_key)
    path = f"{ROOT}/{parent_id}/fix"
    try:
        resolved = runtime.resolve_runtime(
            profile_name=state.profile, base_url_override=state.base_url
        )
    except runtime.CredentialStoreError as exc:
        raise click.ClickException(str(exc)) from exc
    binding = {
        "profile": resolved["profile_name"],
        "base_url": resolved["base_url"],
        "workspace": workspace,
        "parent_run_id": str(parent_id),
        "max_credits": max_credits,
    }
    saved = _saved_intent(binding, idempotency_key)
    if saved is None:
        try:
            eligibility = _data(_read(state, workspace, path))
        except click.ClickException as exc:
            raise click.ClickException(
                f"{exc}. Check your workspace and run sanka login if authentication expired."
            ) from exc
        if (
            eligibility.get("parent_run_id") != str(parent_id)
            or str(eligibility.get("workspace_code")) != workspace
        ):
            raise click.ClickException(
                "Fix eligibility does not match the selected workspace and run"
            )
        if not eligibility.get("eligible"):
            raise click.ClickException(
                f"Sanka Fix unavailable: {eligibility.get('reason')}. "
                f"{eligibility.get('action') or ''}"
            )
        candidate = _digest(str(eligibility.get("candidate_sha256") or ""))
        if not eligibility["min_credits"] <= max_credits <= eligibility["max_credits"]:
            raise click.BadParameter(
                f"must be {eligibility['min_credits']}-{eligibility['max_credits']} for this run",
                param_hint="--max-credits",
            )
        body = {"candidate_sha256": candidate, "max_credits": max_credits}
        if not yes:
            click.echo(
                f"Sanka Fix: {eligibility.get('workspace_name')} ({workspace}), run {parent_id}\n"
                f"Source: {eligibility.get('source_sha256')}; candidate: {candidate}\n"
                f"Recipe: {eligibility.get('recipe')} ({eligibility.get('recipe_sha256')})\n"
                f"Scope: {eligibility.get('verification_scope')}; provider/model: "
                f"{eligibility.get('provider')}/{eligibility.get('model')}\n"
                f"Worker limits: {json.dumps(eligibility.get('limits'), sort_keys=True)}",
                err=True,
            )
            click.confirm(
                f"Consent to processing repository excerpts in Sanka Cloud and reserving up to "
                f"{max_credits} total credits? This cap is not a fixed price or a repair guarantee",
                abort=True,
                err=True,
            )
        saved = _save_intent(binding, body, idempotency_key)
    else:
        click.echo("Recovering the previously approved Fix intent and exact candidate.", err=True)
    body = saved["request"]
    idempotency_key = saved["idempotency_key"]
    headers["Idempotency-Key"] = idempotency_key
    click.echo(
        f"Fix intent: {idempotency_key}. "
        "Reuse this key and the same inputs after an uncertain response.",
        err=True,
    )
    try:
        run = _data(runtime.request_json(state, "POST", path, headers=headers, json_body=body))
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Response uncertain. Retry identical inputs with --idempotency-key {idempotency_key}; "
            "do not use a new key."
        ) from exc
    run["idempotency_key"] = idempotency_key
    run["receipt_command"] = f"sanka cloud receipt --workspace {workspace} {run['id']}"
    if wait:
        click.echo(f"Fix run: {run['id']} {run.get('ui_url', '')}", err=True)
        _follow(state, workspace, run, wait_timeout)
    else:
        runtime.emit_payload(run, state)
