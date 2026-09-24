# SPDX-License-Identifier: Apache-2.0
"""CLI adapter for the same two cloud operations used by Sanka Code."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import click
import httpx

from sanka_cli import runtime
from sanka_cli.cloud_source import package_source
from sanka_cli.commands.cloud import WORKSPACE, _data, _digest, _headers, _intent_key, _read
from sanka_cli.commands.cloud_github import SOURCE_ROOT, select_repository
from sanka_cli.config import config_path
from sanka_cli.state import CLIState

ROOT = "/v2/migrate/code-plans"
STAGES = {"scan", "plan", "apply", "test", "verify"}


def intent_file(state: CLIState, workspace: str, key: str) -> Path:
    resolved = runtime.resolve_runtime(profile_name=state.profile, base_url_override=state.base_url)
    identity = [resolved["base_url"], resolved["profile_name"], workspace, key]
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return config_path().parent / "code-cloud-intents" / f"{digest}.json"


def saved_request(path: Path, binding: dict[str, Any]) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as stream:
            saved = json.loads(stream.read(65537))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise click.ClickException("Cannot read cloud intent; do not retry with a new key") from exc
    if (
        not isinstance(saved, dict)
        or saved.get("binding") != binding
        or not isinstance(saved.get("body"), dict)
        or not isinstance(saved.get("path"), str)
    ):
        raise click.ClickException("Idempotency key is bound to different or invalid inputs")
    return saved


def save_request(
    path: Path, binding: dict[str, Any], endpoint: str, body: dict[str, Any]
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump({"binding": binding, "path": endpoint, "body": body}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        with suppress(FileExistsError):
            os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        saved = saved_request(path, binding)
        assert saved is not None
        return saved
    finally:
        temporary.unlink(missing_ok=True)


def stage_result(stage: str, operation: dict[str, Any]) -> tuple[str, int]:
    run = operation.get("run") or {}
    status = run.get("status")
    kind = operation.get("operation")
    if kind != ("prepare" if stage in {"scan", "plan"} else "execute"):
        raise click.ClickException(
            f"{stage} requires a {'scan/plan' if stage in {'scan', 'plan'} else 'execution'} run ID"
        )
    if status in {"queued", "running"}:
        return status, 6
    if status == "cancelled":
        return "cancelled", 5
    if status == "failed":
        return "failed", 4
    if status != "succeeded":
        raise click.ClickException("Unknown cloud run status")
    if stage in {"scan", "plan"}:
        return ("ready_for_review", 0) if operation.get("plan") else ("evidence_unavailable", 3)
    if stage == "verify":
        verification = operation.get("http_verification") or {}
        if (
            verification.get("status") == "passed"
            and verification.get("total", 0) > 0
            and verification.get("matched") == verification.get("total")
        ):
            return "passed", 0
        return "needs_review", 3
    return "complete", 0


def report(
    state: CLIState,
    workspace: str,
    stage: str,
    operation: dict[str, Any],
    wait: bool,
    wait_timeout: int,
) -> None:
    from sanka.cli.tui.launch import monitor_code, use_human_tui

    if use_human_tui(state):
        raise SystemExit(monitor_code(state, workspace, stage, operation, wait_timeout))
    run_id = str(operation["run"]["id"])
    deadline = time.monotonic() + wait_timeout
    outcome, code = stage_result(stage, operation)
    try:
        while wait and code == 6 and time.monotonic() < deadline:
            click.echo(f"{stage}: {outcome}", err=True)
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            operation = _data(_read(state, workspace, f"{ROOT}/runs/{run_id}"))
            if str(operation.get("run", {}).get("id")) != run_id:
                raise click.ClickException("Cloud returned a different run")
            outcome, code = stage_result(stage, operation)
    except KeyboardInterrupt:
        runtime.emit_payload({"run_id": run_id, "client_state": "detached"}, state)
        raise SystemExit(130) from None
    result = {
        **operation,
        "stage": stage,
        "stage_status": outcome,
        "execution_group": "scan+plan" if stage in {"scan", "plan"} else "apply+test+verify",
        "receipt_command": f"sanka cloud receipt --workspace {workspace} {run_id}",
    }
    if stage in {"scan", "plan"}:
        result["next_command"] = f"sanka plan --cloud --workspace {workspace} --run {run_id}"
        if operation.get("plan"):
            result["approved_plan_sha256"] = operation["plan"]["plan_sha256"]
    else:
        result["next_command"] = f"sanka verify --cloud --workspace {workspace} --run {run_id}"
    runtime.emit_payload(result, state)
    # A newly accepted background job is not a successful stage, but admission succeeded.
    raise SystemExit(0 if code == 6 and not wait and stage in {"scan", "apply"} else code)


def make_command(stage: str) -> click.Command:
    @click.command(
        stage,
        help=(
            "Start cloud Scan + Plan; no conversion."
            if stage == "scan"
            else "Approve the plan and run cloud Apply + Test + Verify."
            if stage == "apply"
            else f"Review the cloud {stage} result; does not start another paid run."
        ),
    )
    @WORKSPACE
    @click.argument("source", required=False, type=click.Path(path_type=Path))
    @click.option(
        "--run", type=click.UUID, help="Plan run for apply; stage operation run for review."
    )
    @click.option("--source-id", type=click.UUID)
    @click.option("--sha256", help="Digest required with --source-id.")
    @click.option("--github", help="Source repository as owner/repository.")
    @click.option("--ref", "branch", help="GitHub branch (default: repository default branch).")
    @click.option("--to", "recipe", type=click.Choice(["fastapi", "flask"]), default="fastapi")
    @click.option("--settings-module")
    @click.option("--plan-hash", help="Exact reviewed hosted plan SHA-256, required for apply.")
    @click.option("--max-credits", type=click.IntRange(1, 6000))
    @click.option("--timeout-seconds", type=click.IntRange(1, 3600), default=600)
    @click.option(
        "--idempotency-key", help="Required for scan/apply; reuse identical inputs after a timeout."
    )
    @click.option(
        "--yes", is_flag=True, help="Consent to source upload and this operation's credit cap."
    )
    @click.option("--wait", is_flag=True)
    @click.option("--wait-timeout", type=click.IntRange(1, 3600), default=900)
    @click.pass_obj
    def command(
        state: CLIState,
        workspace: str,
        source: Path | None,
        run: Any,
        source_id: Any,
        sha256: str | None,
        github: str | None,
        branch: str | None,
        recipe: str,
        settings_module: str | None,
        plan_hash: str | None,
        max_credits: int | None,
        timeout_seconds: int,
        idempotency_key: str | None,
        yes: bool,
        wait: bool,
        wait_timeout: int,
    ) -> None:
        headers = _headers(workspace)
        if stage in {"plan", "test", "verify"}:
            if (
                not run
                or source
                or source_id
                or github
                or max_credits
                or idempotency_key
                or plan_hash
                or sha256
                or branch
                or settings_module
            ):
                raise click.UsageError(
                    "Review requires --run; source and paid-operation options are not accepted"
                )
            operation = _data(_read(state, workspace, f"{ROOT}/runs/{run}"))
            if str(operation.get("run", {}).get("id")) != str(run):
                raise click.ClickException("Cloud returned a different run")
            report(state, workspace, stage, operation, wait, wait_timeout)
            return
        if max_credits is None or not idempotency_key:
            raise click.UsageError("Cloud scan/apply requires --max-credits and --idempotency-key")
        _intent_key(idempotency_key)
        if not yes and not sys.stdin.isatty():
            raise click.UsageError("Noninteractive execution requires --yes")
        if stage == "apply":
            if (
                not run
                or not plan_hash
                or source
                or source_id
                or github
                or sha256
                or branch
                or settings_module
            ):
                raise click.UsageError(
                    "Apply requires --run PLAN_RUN and --plan-hash; source comes from the "
                    "reviewed plan"
                )
            _digest(plan_hash)
        elif (
            run
            or plan_hash
            or sum(bool(v) for v in (source, source_id, github)) > 1
            or bool(source_id) != bool(sha256)
            or (branch and not github)
        ):
            raise click.UsageError(
                "Scan accepts one directory/ZIP, --github, or --source-id with --sha256"
            )
        content = None
        if stage == "scan" and not github and not source_id:
            source = source or Path(".")
            content = package_source(source)
            sha256 = hashlib.sha256(content).hexdigest()
        if sha256:
            _digest(sha256)
        binding = {
            "stage": stage,
            "run": str(run) if run else None,
            "source": str(source.resolve()) if source else None,
            "source_id": str(source_id) if source_id else None,
            "sha256": sha256,
            "github": github,
            "branch": branch,
            "recipe": recipe,
            "settings_module": settings_module,
            "plan_hash": plan_hash,
            "max_credits": max_credits,
            "timeout_seconds": timeout_seconds,
        }
        path = intent_file(state, workspace, idempotency_key)
        saved = saved_request(path, binding)
        if saved is None:
            if not yes:
                click.confirm(
                    f"{stage} in workspace {workspace}: {github or source or source_id or run}. "
                    f"Source/plan digest: {sha256 or plan_hash or 'pinned GitHub import'}. "
                    f"Upload source (7-day retention) and reserve up to {max_credits} credits?",
                    abort=True,
                    err=True,
                )
            body: dict[str, Any] = {"max_credits": max_credits, "timeout_seconds": timeout_seconds}
            endpoint = ROOT
            if stage == "apply":
                body["approved_plan_sha256"] = plan_hash
                endpoint = f"{ROOT}/{run}/runs"
            else:
                if github:
                    selection = select_repository(state, workspace, github, branch)
                    data = _data(
                        runtime.request_json(
                            state,
                            "POST",
                            f"{SOURCE_ROOT}/github/import",
                            headers=headers,
                            json_body=selection,
                        )
                    )["source"]
                    source_id, sha256 = data["id"], data["sha256"]
                elif content is not None:
                    assert source is not None
                    data = _data(
                        runtime.request_json(
                            state,
                            "POST",
                            f"{SOURCE_ROOT}/uploads",
                            headers=headers,
                            json_body={
                                "filename": (source.name or "source")[:190]
                                + ("" if source.suffix == ".zip" else ".zip"),
                                "sha256": sha256,
                                "archive_base64": base64.b64encode(content).decode(),
                            },
                        )
                    )["source"]
                    if data.get("sha256") != sha256:
                        raise click.ClickException("Uploaded source digest mismatch")
                    source_id = data["id"]
                recipes = _data(_read(state, workspace, f"{ROOT}/recipes"))
                selected = next(
                    (r for r in recipes["recipes"] if r["id"] == f"drf-to-{recipe}"), None
                )
                if not selected or not selected.get("enabled"):
                    raise click.ClickException(
                        "Selected cloud recipe is unavailable; no compute was started"
                    )
                body.update(
                    source_id=str(source_id),
                    source_sha256=sha256,
                    recipe=selected["id"],
                    recipe_sha256=selected["recipe_sha256"],
                    settings_module=settings_module,
                )
            saved = save_request(path, binding, endpoint, body)
        headers["Idempotency-Key"] = idempotency_key
        try:
            operation = _data(
                runtime.request_json(
                    state, "POST", saved["path"], headers=headers, json_body=saved["body"]
                )
            )
        except httpx.HTTPError as exc:
            raise click.ClickException(
                "Response uncertain. Retry identical inputs with "
                f"--idempotency-key {idempotency_key}; do not use a new key"
            ) from exc
        report(state, workspace, stage, operation, wait, wait_timeout)

    return command


def invoke(stage: str, state: CLIState, args: tuple[str, ...]) -> None:
    make_command(stage).main(
        args=list(args), prog_name=f"sanka {stage} --cloud", obj=state, standalone_mode=False
    )
