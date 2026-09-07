# SPDX-License-Identifier: Apache-2.0
"""Workspace-pinned client for hosted repository runs; no hosted implementation."""

from __future__ import annotations

import base64
import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Any

import click

import sanka_cli.runtime as runtime
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
    if (
        not 8 <= len(idempotency_key) <= 200
        or not idempotency_key.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in idempotency_key)
    ):
        raise click.BadParameter(
            "must be 8-200 visible ASCII characters", param_hint="--idempotency-key"
        )
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
@click.option("--artifact", type=click.Choice(["output.zip", "logs.txt"]), default="output.zip")
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
