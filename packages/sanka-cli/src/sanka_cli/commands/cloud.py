# SPDX-License-Identifier: Apache-2.0
"""Workspace-pinned client for hosted repository runs; no hosted implementation."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from uuid import UUID

import click

import sanka_cli.runtime as runtime
from sanka_cli.certificates import load_scenarios, read_json, verify_certificate
from sanka_cli.state import CLIState

ROOT = "/v2/migrate/cloud-runs"
SOURCE_BYTES = 8 * 1024 * 1024
ARTIFACT_BYTES = 16 * 1024 * 1024
WORKSPACE = click.option("--workspace", required=True, help="Exact workspace code (8 digits).")
RUN = click.argument("run_id", type=click.UUID)


def _headers(workspace: str) -> dict[str, str]:
    if not re.fullmatch(r"\d{8}", workspace):
        raise click.BadParameter("must be an 8-digit workspace code", param_hint="--workspace")
    return {"X-Workspace-Code": workspace}


def _digest(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise click.BadParameter("must be a lowercase SHA-256 digest", param_hint="--sha256")
    return value


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise click.ClickException("Cloud API returned an invalid response")
    return data


def _intent_key(value: str) -> str:
    if (
        not 8 <= len(value) <= 200
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise click.BadParameter(
            "must be 8-200 visible ASCII characters", param_hint="--idempotency-key"
        )
    return value


def _read(state: CLIState, workspace: str, path: str, **params: Any) -> dict[str, Any]:
    return runtime.request_json(state, "GET", path, headers=_headers(workspace), params=params)


@click.group()
def cloud() -> None:
    """Hosted DRF-to-FastAPI runs, credit receipts, and artifacts (when enabled)."""


@cloud.command("upload")
@WORKSPACE
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--yes", is_flag=True, help="Confirm uploading this ZIP to the selected workspace.")
@click.pass_obj
def upload(state: CLIState, workspace: str, archive: Path, yes: bool) -> None:
    """Upload an explicit ZIP, returning its source ID and SHA-256; no compute charge."""
    headers = _headers(workspace)
    with archive.open("rb") as stream:
        content = stream.read(SOURCE_BYTES + 1)
    if not content or len(content) > SOURCE_BYTES:
        raise click.ClickException("Source ZIP must contain between 1 byte and 8 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            if not bundle.infolist():
                raise ValueError("empty ZIP")
    except (ValueError, zipfile.BadZipFile) as exc:
        raise click.ClickException("Source must be a nonempty ZIP archive") from exc
    sha256 = hashlib.sha256(content).hexdigest()
    if not yes:
        click.confirm(
            f"Upload {archive.name} ({len(content)} bytes, SHA-256 {sha256}) "
            f"to workspace {workspace}? Source retention: 7 days",
            abort=True,
        )
    payload = runtime.request_json(
        state,
        "POST",
        "/v2/migrate/cloud-sources",
        headers=headers,
        json_body={"sha256": sha256, "archive_base64": base64.b64encode(content).decode("ascii")},
    )
    if _data(payload).get("sha256") != sha256:
        raise click.ClickException("Uploaded source digest does not match the selected ZIP")
    runtime.emit_payload(payload, state)


@cloud.command("run")
@WORKSPACE
@click.option("--source-id", required=True, type=click.UUID)
@click.option("--sha256", required=True)
@click.option("--max-credits", required=True, type=click.IntRange(1, 6000))
@click.option("--timeout-seconds", default=600, show_default=True, type=click.IntRange(1, 3600))
@click.option(
    "--settings-module", default=None, help="Django settings module, e.g. config.settings."
)
@click.option(
    "--idempotency-key",
    required=True,
    help="Unique intent key (8-200 characters). Reuse with identical inputs after a network error.",
)
@click.option("--yes", is_flag=True, help="Confirm the credit hold and paid execution.")
@click.pass_obj
def run(
    state: CLIState,
    workspace: str,
    source_id: Any,
    sha256: str,
    max_credits: int,
    timeout_seconds: int,
    settings_module: str | None,
    idempotency_key: str,
    yes: bool,
) -> None:
    """Reserve the credit limit and queue one run. Retries keep the same source and key."""
    headers = _headers(workspace)
    _digest(sha256)
    _intent_key(idempotency_key)
    if settings_module is not None and (
        len(settings_module) > 200
        or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", settings_module)
    ):
        raise click.BadParameter("must be a Python module name", param_hint="--settings-module")
    if not yes:
        click.confirm(
            f"Run source {source_id} in workspace {workspace}? "
            f"Reserve up to {max_credits} credits; "
            "100 credits per active worker-minute, unused credits released",
            abort=True,
        )
    headers["Idempotency-Key"] = idempotency_key
    payload = runtime.request_json(
        state,
        "POST",
        ROOT,
        headers=headers,
        json_body={
            "source_id": str(source_id),
            "source_sha256": sha256,
            "max_credits": max_credits,
            "timeout_seconds": timeout_seconds,
            "settings_module": settings_module,
        },
    )
    runtime.emit_payload(payload, state)


@cloud.command("repair")
@WORKSPACE
@RUN
@click.option("--candidate-sha256", required=True, help="Exact saved output.zip digest.")
@click.option(
    "--path",
    "paths",
    multiple=True,
    required=True,
    help="Existing application Python file to allow; repeat for multiple paths.",
)
@click.option("--target-gate", required=True, type=click.Choice(["test", "verify"]))
@click.option("--max-credits", required=True, type=click.IntRange(1001, 7000))
@click.option("--timeout-seconds", default=600, show_default=True, type=click.IntRange(1, 3600))
@click.option(
    "--idempotency-key",
    required=True,
    help="Reuse this key with identical inputs after an uncertain response.",
)
@click.option(
    "--yes", is_flag=True, help="Confirm the full credit hold, including the success premium."
)
@click.pass_obj
def repair(
    state: CLIState,
    workspace: str,
    run_id: Any,
    candidate_sha256: str,
    paths: tuple[str, ...],
    target_gate: str,
    max_credits: int,
    timeout_seconds: int,
    idempotency_key: str,
    yes: bool,
) -> None:
    """Attempt one bounded code repair of a failed run's pinned output."""
    headers = _headers(workspace)
    _digest(candidate_sha256)
    _intent_key(idempotency_key)
    if (
        not 1 <= len(paths) <= 20
        or len(set(paths)) != len(paths)
        or any(
            len(path) > 240
            or not re.fullmatch(
                r"app/(?:[A-Za-z_][A-Za-z0-9_-]*/)*[A-Za-z_][A-Za-z0-9_-]*\.py", path
            )
            or "__pycache__" in path.split("/")
            for path in paths
        )
    ):
        raise click.BadParameter(
            "select 1-20 unique existing Python paths under app/", param_hint="--path"
        )
    parent = _data(_read(state, workspace, f"{ROOT}/{run_id}"))
    if parent.get("id") != str(run_id) or parent.get("status") != "failed":
        raise click.ClickException("Repair requires the selected failed run")
    artifacts = _data(_read(state, workspace, f"{ROOT}/{run_id}/artifacts"))
    if not any(
        item.get("name") == "output.zip" and item.get("sha256") == candidate_sha256
        for item in artifacts.get("artifacts", [])
    ):
        raise click.ClickException("Candidate digest does not match the retained output.zip")
    request = parent.get("request") or {}
    if not request.get("source_id") or not request.get("source_sha256"):
        raise click.ClickException("The failed run has no pinned source")
    if not yes:
        click.confirm(
            f"Repair run {run_id} in workspace {workspace}, check {target_gate}, "
            f"paths {', '.join(sorted(paths))}? Reserve up to {max_credits} credits, including "
            "1,000 only on success plus 100 per active worker-minute; one model attempt",
            abort=True,
        )
    headers["Idempotency-Key"] = idempotency_key
    runtime.emit_payload(
        runtime.request_json(
            state,
            "POST",
            ROOT,
            headers=headers,
            json_body={
                "source_id": request["source_id"],
                "source_sha256": request["source_sha256"],
                "settings_module": request.get("settings_module"),
                "max_credits": max_credits,
                "timeout_seconds": timeout_seconds,
                "repair": {
                    "parent_run_id": str(run_id),
                    "candidate_sha256": candidate_sha256,
                    "target_gate": target_gate,
                    "allowed_paths": sorted(paths),
                    "model_policy": "bounded-patch-v1",
                    "max_attempts": 1,
                },
            },
        ),
        state,
    )


@cloud.command("certify")
@WORKSPACE
@RUN
@click.option("--candidate-sha256", required=True, help="Exact saved output.zip digest.")
@click.option(
    "--cases", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--max-credits", required=True, type=click.IntRange(2001, 8000))
@click.option("--timeout-seconds", default=600, show_default=True, type=click.IntRange(1, 3600))
@click.option("--idempotency-key", required=True, help="Reuse only with the same reviewed inputs.")
@click.option("--yes", is_flag=True, help="Confirm the full credit hold and selected HTTP scope.")
@click.pass_obj
def certify(
    state: CLIState,
    workspace: str,
    run_id: Any,
    candidate_sha256: str,
    cases: Path,
    max_credits: int,
    timeout_seconds: int,
    idempotency_key: str,
    yes: bool,
) -> None:
    """Compare reviewed HTTP cases independently and issue a certificate on success."""
    headers = _headers(workspace)
    _digest(candidate_sha256)
    _intent_key(idempotency_key)
    try:
        scenarios = load_scenarios(cases)
    except (ValueError, OSError, RecursionError) as error:
        raise click.ClickException(str(error)) from error
    parent = _data(_read(state, workspace, f"{ROOT}/{run_id}"))
    if parent.get("id") != str(run_id) or parent.get("status") not in {
        "succeeded",
        "failed",
        "cancelled",
    }:
        raise click.ClickException("Certification requires the selected settled run")
    artifacts = _data(_read(state, workspace, f"{ROOT}/{run_id}/artifacts"))
    if not any(
        item.get("name") == "output.zip" and item.get("sha256") == candidate_sha256
        for item in artifacts.get("artifacts", [])
    ):
        raise click.ClickException("Candidate digest does not match the retained output.zip")
    request = parent.get("request") or {}
    if not all(request.get(name) for name in ("source_id", "source_sha256", "settings_module")):
        raise click.ClickException(
            "The original run needs a pinned source and Django settings module"
        )
    if not yes:
        click.confirm(
            f"Certify candidate {candidate_sha256} in workspace {workspace} using {len(scenarios)} "
            f"reviewed HTTP cases? Reserve up to {max_credits} credits, including 2,000 only on "
            "successful certificate issuance plus 100 per active worker-minute. "
            "Untested behavior is outside this certificate",
            abort=True,
        )
    headers["Idempotency-Key"] = idempotency_key
    runtime.emit_payload(
        runtime.request_json(
            state,
            "POST",
            ROOT,
            headers=headers,
            json_body={
                "source_id": request["source_id"],
                "source_sha256": request["source_sha256"],
                "settings_module": request["settings_module"],
                "max_credits": max_credits,
                "timeout_seconds": timeout_seconds,
                "verification_profile": "independent-http-replay-v1",
                "certification": {
                    "parent_run_id": str(run_id),
                    "candidate_sha256": candidate_sha256,
                    "scenarios": scenarios,
                },
            },
        ),
        state,
    )


@cloud.command("certificate")
@WORKSPACE
@RUN
@click.option("--to", "destination", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.pass_obj
def certificate(state: CLIState, workspace: str, run_id: Any, destination: Path | None) -> None:
    """Read the certificate and revocation status; optionally save a new JSON file."""
    if destination and (destination.exists() or destination.is_symlink()):
        raise click.ClickException("Destination already exists; choose a new file")
    document = _data(_read(state, workspace, f"{ROOT}/{run_id}/certificate"))
    if destination:
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
        runtime.emit_payload(
            {"path": str(destination), "revoked_at": document.get("revoked_at")}, state
        )
    else:
        runtime.emit_payload(document, state)


@cloud.command("certificate-verify")
@click.argument("document", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--workspace", default=None, help="Verify online using this exact workspace code.")
@click.option(
    "--trusted-keys",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Explicit trusted issuer key ring for offline signature verification.",
)
@click.pass_obj
def certificate_verify(
    state: CLIState, document: Path, workspace: str | None, trusted_keys: Path | None
) -> None:
    """Verify a signature and, in online mode, check current revocation status."""
    if bool(workspace) == bool(trusted_keys):
        raise click.UsageError(
            "Choose --workspace for online verification or --trusted-keys for offline verification"
        )
    try:
        data = read_json(document)
        keys = (
            read_json(trusted_keys)
            if trusted_keys
            else _data(_read(state, workspace or "", f"{ROOT}/certificate-keys"))
        )
        payload = verify_certificate(data, keys)
        run_id = str(UUID(payload["run_id"]))
        if workspace:
            current = _data(_read(state, workspace, f"{ROOT}/{run_id}/certificate"))
            if current.get("certificate") != data.get("certificate", data):
                raise ValueError("The API certificate does not match the supplied signed record")
            if current.get("revoked_at"):
                raise ValueError("This certificate has been revoked by its owner")
    except (ValueError, KeyError, TypeError, AttributeError, OSError, RecursionError) as error:
        raise click.ClickException(str(error)) from error
    runtime.emit_payload(
        {
            "signature_valid": True,
            "run_id": run_id,
            "revocation_status": "active_at_check" if workspace else "not_checked_offline",
            "verification_scope": payload["evidence"]["profile"],
            "tested_routes": payload["evidence"]["tested_routes"],
            "untested_routes": payload["evidence"]["untested_routes"],
            "limitations": payload["limitations"],
        },
        state,
    )


@cloud.command("certificate-revoke")
@WORKSPACE
@RUN
@click.option(
    "--reason", required=True, help="Reason for withdrawing this certificate (1-500 characters)."
)
@click.option("--yes", is_flag=True, help="Confirm permanent certificate revocation.")
@click.pass_obj
def certificate_revoke(
    state: CLIState, workspace: str, run_id: Any, reason: str, yes: bool
) -> None:
    """Revoke this certificate while preserving its original signed evidence."""
    headers = _headers(workspace)
    reason = reason.strip()
    if not 1 <= len(reason) <= 500:
        raise click.BadParameter("must be 1-500 nonblank characters", param_hint="--reason")
    if not yes:
        click.confirm(f"Revoke certificate for run {run_id} in workspace {workspace}?", abort=True)
    runtime.emit_payload(
        runtime.request_json(
            state,
            "POST",
            f"{ROOT}/{run_id}/certificate/revoke",
            headers=headers,
            json_body={"reason": reason},
        ),
        state,
    )


@cloud.command("list")
@WORKSPACE
@click.option("--cursor", type=click.UUID, default=None)
@click.pass_obj
def list_runs(state: CLIState, workspace: str, cursor: Any) -> None:
    params = {"cursor": str(cursor)} if cursor else {}
    runtime.emit_payload(_read(state, workspace, ROOT, limit=20, **params), state)


@cloud.command("status")
@WORKSPACE
@RUN
@click.pass_obj
def status(state: CLIState, workspace: str, run_id: Any) -> None:
    runtime.emit_payload(_read(state, workspace, f"{ROOT}/{run_id}"), state)


@cloud.command("receipt")
@WORKSPACE
@RUN
@click.pass_obj
def receipt(state: CLIState, workspace: str, run_id: Any) -> None:
    """Read the immutable receipt, including held, charged, and released credits."""
    runtime.emit_payload(_read(state, workspace, f"{ROOT}/{run_id}/receipt"), state)


@cloud.command("events")
@WORKSPACE
@RUN
@click.option("--cursor", default=0, type=click.IntRange(min=0))
@click.pass_obj
def events(state: CLIState, workspace: str, run_id: Any, cursor: int) -> None:
    runtime.emit_payload(_read(state, workspace, f"{ROOT}/{run_id}/events", cursor=cursor), state)


@cloud.command("cancel")
@WORKSPACE
@RUN
@click.pass_obj
def cancel(state: CLIState, workspace: str, run_id: Any) -> None:
    """Cancel this run; completed active compute can still be charged."""
    runtime.emit_payload(
        runtime.request_json(
            state,
            "POST",
            f"{ROOT}/{run_id}/cancel",
            headers=_headers(workspace),
        ),
        state,
    )


@cloud.command("download")
@WORKSPACE
@RUN
@click.option(
    "--artifact",
    type=click.Choice(["output.zip", "logs.txt", "repair-response.json"]),
    default="output.zip",
)
@click.option("--to", "destination", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.pass_obj
def download(
    state: CLIState, workspace: str, run_id: Any, artifact: str, destination: Path
) -> None:
    """Verify a retained artifact's digest and save a new file; existing files are preserved."""
    if destination.exists() or destination.is_symlink():
        raise click.ClickException("Destination already exists; choose a new file")
    manifest = _data(_read(state, workspace, f"{ROOT}/{run_id}/artifacts"))
    entries = manifest.get("artifacts", [])
    metadata = next((item for item in entries if item.get("name") == artifact), None)
    if metadata is None:
        raise click.ClickException("Artifact is not available (it may have expired)")
    content, _ = runtime.request_bytes(
        state,
        "GET",
        f"{ROOT}/{run_id}/artifacts/{artifact}",
        headers=_headers(workspace),
        max_bytes=ARTIFACT_BYTES,
    )
    if len(content) != metadata.get("size_bytes") or hashlib.sha256(
        content
    ).hexdigest() != metadata.get("sha256"):
        raise click.ClickException("Artifact size or digest does not match; no file was written")
    with destination.open("xb") as stream:
        stream.write(content)
    runtime.emit_payload({"path": str(destination), "sha256": metadata["sha256"]}, state)
