# SPDX-License-Identifier: Apache-2.0
"""``sanka functions`` -- manage Custom Code functions from the repository that owns them.

The shape of this group follows from spec D3: the customer's git repo is the source of
truth, and Sanka stores immutable versions pushed from it. So the commands mirror a
deploy tool rather than a CRUD client -- ``push`` uploads what is on disk, ``pull``
writes what is deployed back to disk, ``diff`` tells you whether those two agree, and
``deploy`` and ``rollback`` are both just moves of the ``live`` alias.

``dev``, ``run``, ``test`` and ``logs`` need the execution runtime and land with it.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

import click

import sanka_cli.runtime as runtime
from sanka_cli.bundle import (
    MANIFEST_FILENAME,
    BuiltBundle,
    BundleError,
    build_bundle,
    extract_bundle,
)
from sanka_cli.state import CLIState

CODE_ROOT = "/v2/public/code"

RUNTIME_CHOICES = {"node": "node22", "python": "python312"}

_NODE_TEMPLATE = """\
/**
 * Sanka Custom Code function.
 *
 * Return an object with `outputFields`; those are the values downstream workflow
 * actions can reference, and they must match the `outputs` declared in sanka.json.
 */
export const main = async (event) => {
  const { companyName } = event.inputFields;

  return {
    outputFields: {
      greeting: `Hello, ${companyName ?? "world"}`,
    },
  };
};
"""

_PYTHON_TEMPLATE = '''\
"""Sanka Custom Code function.

Return a dict with "outputFields"; those are the values downstream workflow actions can
reference, and they must match the `outputs` declared in sanka.json.
"""


def main(event):
    company_name = event["inputFields"].get("companyName") or "world"

    return {"outputFields": {"greeting": f"Hello, {company_name}"}}
'''

_SANKAIGNORE_TEMPLATE = """\
# Files here are never uploaded. Defaults already cover .git, node_modules,
# __pycache__, .venv, dist/build and dotfiles like .env -- add your own below.
tests/
*.test.js
"""


@click.group()
def functions() -> None:
    """Custom Code functions."""


# ---- scaffolding ---------------------------------------------------------


@functions.command("init")
@click.option("--slug", required=True, help="Function slug (lowercase, hyphens).")
@click.option("--name", default=None, help="Human-readable name. Defaults to the slug.")
@click.option(
    "--runtime",
    "runtime_name",
    type=click.Choice(sorted(RUNTIME_CHOICES)),
    default="node",
    show_default=True,
)
@click.option(
    "--dir",
    "directory",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
)
def function_init(slug: str, name: str | None, runtime_name: str, directory: Path) -> None:
    """Scaffold sanka.json, an entry file, and .sankaignore."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / MANIFEST_FILENAME
    if manifest_path.exists():
        raise click.ClickException(f"{manifest_path} already exists.")

    resolved_runtime = RUNTIME_CHOICES[runtime_name]
    entry = "index.js" if runtime_name == "node" else "main.py"
    manifest = {
        "schemaVersion": 1,
        "slug": slug,
        "name": name or slug.replace("-", " ").title(),
        "runtime": resolved_runtime,
        "entry": entry,
        "timeoutSeconds": 20,
        "memoryMb": 128,
        "inputs": [{"name": "companyName", "type": "string", "required": True}],
        "outputs": [{"name": "greeting", "type": "string"}],
        "secrets": [],
        "permissions": [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    entry_path = directory / entry
    if not entry_path.exists():
        template = _NODE_TEMPLATE if runtime_name == "node" else _PYTHON_TEMPLATE
        entry_path.write_text(template, encoding="utf-8")

    ignore_path = directory / ".sankaignore"
    if not ignore_path.exists():
        ignore_path.write_text(_SANKAIGNORE_TEMPLATE, encoding="utf-8")

    click.echo(f"Created {manifest_path}, {entry_path}, and {ignore_path}")
    click.echo(
        f"Next: sanka functions create --dir {directory} && sanka functions push --dir {directory}"
    )


# ---- reading -------------------------------------------------------------


@functions.command("list")
@click.option("--limit", default=100, show_default=True, type=int)
@click.option("--offset", default=0, show_default=True, type=int)
@click.pass_obj
def function_list(state: CLIState, limit: int, offset: int) -> None:
    """List Custom Code functions in the workspace."""
    payload = runtime.request_json(
        state,
        "GET",
        f"{CODE_ROOT}/functions",
        params={"limit": limit, "offset": offset},
    )
    runtime.emit_payload(_data(payload).get("functions", []), state)


@functions.command("get")
@click.argument("slug")
@click.pass_obj
def function_get(state: CLIState, slug: str) -> None:
    """Show a function with its aliases and latest version."""
    payload = runtime.request_json(state, "GET", f"{CODE_ROOT}/functions/{slug}")
    runtime.emit_payload(_data(payload), state)


@functions.command("versions")
@click.argument("slug")
@click.option("--limit", default=50, show_default=True, type=int)
@click.pass_obj
def function_versions(state: CLIState, slug: str, limit: int) -> None:
    """List a function's versions, newest first."""
    payload = runtime.request_json(
        state,
        "GET",
        f"{CODE_ROOT}/functions/{slug}/versions",
        params={"limit": limit},
    )
    runtime.emit_payload(_data(payload).get("versions", []), state)


# ---- lifecycle -----------------------------------------------------------


@functions.command("create")
@click.option(
    "--dir",
    "directory",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--source-mode", type=click.Choice(["git", "ui"]), default="git", show_default=True)
@click.pass_obj
def function_create(state: CLIState, directory: Path, source_mode: str) -> None:
    """Register the function described by ./sanka.json.

    Defaults to source-mode git: creating from a repo means the repo owns the code.
    """
    manifest = _load_manifest(directory)
    payload = runtime.request_json(
        state,
        "POST",
        f"{CODE_ROOT}/functions",
        json_body={
            "slug": manifest["slug"],
            "name": manifest.get("name") or manifest["slug"],
            "runtime": manifest["runtime"],
            "description": manifest.get("description"),
            "source_mode": source_mode,
        },
    )
    runtime.emit_payload(_data(payload), state)


@functions.command("push")
@click.option(
    "--dir",
    "directory",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--activate", is_flag=True, help="Point the `live` alias at this version.")
@click.option("-m", "--message", default=None, help="Change summary.")
@click.option("--dry-run", is_flag=True, help="Build and report locally; upload nothing.")
@click.pass_obj
def function_push(
    state: CLIState,
    directory: Path,
    activate: bool,
    message: str | None,
    dry_run: bool,
) -> None:
    """Upload the working directory as a new immutable version.

    Bundling is deterministic, so re-pushing unchanged code returns the version that
    already exists rather than minting a duplicate -- safe to run on every CI merge.
    """
    manifest = _load_manifest(directory)
    built = _build(directory)

    if dry_run:
        runtime.emit_payload(
            {
                "slug": manifest["slug"],
                "content_sha256": built.sha256,
                "size_bytes": built.size_bytes,
                "file_count": len(built.paths),
                "files": list(built.paths),
                "uploaded": False,
            },
            state,
        )
        return

    body: dict[str, Any] = {
        "bundle_base64": base64.b64encode(built.raw).decode("ascii"),
        "content_sha256": built.sha256,
        "change_summary": message,
        "activate": activate,
    }
    body.update(_git_provenance(directory))

    payload = runtime.request_json(
        state,
        "POST",
        f"{CODE_ROOT}/functions/{manifest['slug']}/versions",
        json_body=body,
    )
    runtime.emit_payload(_data(payload), state)


@functions.command("pull")
@click.argument("slug")
@click.option("--version", type=int, default=None, help="Defaults to the live version.")
@click.option(
    "--dir",
    "directory",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@click.pass_obj
def function_pull(
    state: CLIState,
    slug: str,
    version: int | None,
    directory: Path,
    force: bool,
) -> None:
    """Write a deployed version's source back to disk."""
    resolved = _resolve_version(state, slug, version)
    raw, headers = _download(state, slug, resolved)
    _verify_digest(raw, headers)
    try:
        written = extract_bundle(raw, directory, overwrite=force)
    except BundleError as exc:
        raise click.ClickException(str(exc)) from exc
    runtime.emit_payload(
        {
            "slug": slug,
            "version": resolved,
            "files": written,
            "directory": str(directory),
        },
        state,
    )


@functions.command("diff")
@click.option(
    "--dir",
    "directory",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--version", type=int, default=None, help="Defaults to the live version.")
@click.pass_obj
def function_diff(state: CLIState, directory: Path, version: int | None) -> None:
    """Report whether the working directory matches a deployed version.

    Compares digests rather than file contents. Bundling is deterministic, so equal
    digests mean the trees are identical -- and an unequal digest is the signal to
    pull or push, which is the question this command exists to answer.
    """
    manifest = _load_manifest(directory)
    slug = manifest["slug"]
    built = _build(directory)
    resolved = _resolve_version(state, slug, version)
    raw, headers = _download(state, slug, resolved)
    _verify_digest(raw, headers)

    remote_digest = headers.get("x-sanka-content-sha256") or ""
    in_sync = remote_digest == built.sha256
    runtime.emit_payload(
        {
            "slug": slug,
            "version": resolved,
            "local_sha256": built.sha256,
            "remote_sha256": remote_digest,
            "in_sync": in_sync,
        },
        state,
    )
    if not in_sync:
        raise SystemExit(1)


@functions.command("deploy")
@click.argument("slug")
@click.option("--version", required=True, type=int)
@click.option("--alias", default="live", show_default=True)
@click.pass_obj
def function_deploy(state: CLIState, slug: str, version: int, alias: str) -> None:
    """Point an alias at a version."""
    payload = runtime.request_json(
        state,
        "PUT",
        f"{CODE_ROOT}/functions/{slug}/aliases/{alias}",
        json_body={"version": version},
    )
    runtime.emit_payload(_data(payload), state)


@functions.command("rollback")
@click.argument("slug")
@click.option(
    "--to",
    "to_version",
    type=int,
    default=None,
    help="Defaults to the previous version.",
)
@click.pass_obj
def function_rollback(state: CLIState, slug: str, to_version: int | None) -> None:
    """Move `live` back to an earlier version."""
    if to_version is None:
        to_version = _previous_version(state, slug)
    payload = runtime.request_json(
        state,
        "PUT",
        f"{CODE_ROOT}/functions/{slug}/aliases/live",
        json_body={"version": to_version},
    )
    runtime.emit_payload(_data(payload), state)


@functions.command("unlock")
@click.argument("slug")
@click.confirmation_option(
    prompt="This hands ownership of the code back to the Sanka UI. Continue?"
)
@click.pass_obj
def function_unlock(state: CLIState, slug: str) -> None:
    """Release the git lock so the function becomes UI-editable.

    Only this surface can do it. The UI cannot unlock itself, because then refusing UI
    pushes would be a formality rather than a control.
    """
    payload = runtime.request_json(
        state,
        "PATCH",
        f"{CODE_ROOT}/functions/{slug}",
        json_body={"source_mode": "ui"},
    )
    runtime.emit_payload(_data(payload), state)


@functions.command("lock")
@click.argument("slug")
@click.pass_obj
def function_lock(state: CLIState, slug: str) -> None:
    """Make this repo the source of truth; the UI editor becomes read-only."""
    payload = runtime.request_json(
        state,
        "PATCH",
        f"{CODE_ROOT}/functions/{slug}",
        json_body={"source_mode": "git"},
    )
    runtime.emit_payload(_data(payload), state)


# ---- secrets -------------------------------------------------------------


@functions.group("secrets")
def function_secrets() -> None:
    """Per-function secrets. Values are never readable back."""


@function_secrets.command("list")
@click.argument("slug")
@click.pass_obj
def function_secrets_list(state: CLIState, slug: str) -> None:
    payload = runtime.request_json(state, "GET", f"{CODE_ROOT}/functions/{slug}/secrets")
    runtime.emit_payload(_data(payload).get("secrets", []), state)


@function_secrets.command("set")
@click.argument("slug")
@click.argument("name")
@click.option(
    "--value",
    default=None,
    help="Omit to be prompted. Inline values land in your shell history.",
)
@click.pass_obj
def function_secrets_set(state: CLIState, slug: str, name: str, value: str | None) -> None:
    """Set a secret. Prompts without echo unless --value is given."""
    if value is None:
        value = click.prompt("Value", hide_input=True)
    payload = runtime.request_json(
        state,
        "PUT",
        f"{CODE_ROOT}/functions/{slug}/secrets/{name}",
        json_body={"name": name, "value": value},
    )
    runtime.emit_payload(_data(payload), state)


@function_secrets.command("rm")
@click.argument("slug")
@click.argument("name")
@click.pass_obj
def function_secrets_rm(state: CLIState, slug: str, name: str) -> None:
    runtime.request_json(state, "DELETE", f"{CODE_ROOT}/functions/{slug}/secrets/{name}")
    click.echo(f"Deleted {name}")


# ---- helpers -------------------------------------------------------------


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("data") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else (payload or {})


def _load_manifest(directory: Path) -> dict[str, Any]:
    path = directory / MANIFEST_FILENAME
    if not path.is_file():
        raise click.ClickException(
            f"{path} not found. Run `sanka functions init --slug <slug>` first."
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or not manifest.get("slug"):
        raise click.ClickException(f"{path} must be an object with a 'slug'.")
    return manifest


def _build(directory: Path) -> BuiltBundle:
    try:
        return build_bundle(directory)
    except BundleError as exc:
        raise click.ClickException(str(exc)) from exc


def _resolve_version(state: CLIState, slug: str, version: int | None) -> int:
    if version is not None:
        return version
    detail = _data(runtime.request_json(state, "GET", f"{CODE_ROOT}/functions/{slug}"))
    for alias in detail.get("aliases", []) or []:
        if alias.get("alias") == "live" and alias.get("version"):
            return int(alias["version"])
    latest = (detail.get("function") or {}).get("latest_version")
    if latest:
        return int(latest)
    raise click.ClickException(f"{slug} has no versions yet.")


def _previous_version(state: CLIState, slug: str) -> int:
    payload = runtime.request_json(
        state,
        "GET",
        f"{CODE_ROOT}/functions/{slug}/versions",
        params={"limit": 2},
    )
    versions = _data(payload).get("versions", []) or []
    if len(versions) < 2:
        raise click.ClickException(
            f"{slug} has no earlier version to roll back to. Pass --to explicitly."
        )
    return int(versions[1]["version"])


def _download(state: CLIState, slug: str, version: int) -> tuple[bytes, dict[str, str]]:
    raw, headers = runtime.request_bytes(
        state,
        "GET",
        f"{CODE_ROOT}/functions/{slug}/versions/{version}/bundle",
    )
    return raw, {key.lower(): value for key, value in headers.items()}


def _verify_digest(raw: bytes, headers: dict[str, str]) -> None:
    import hashlib

    declared = headers.get("x-sanka-content-sha256")
    if not declared:
        return
    actual = hashlib.sha256(raw).hexdigest()
    if actual != declared:
        raise click.ClickException(
            "Downloaded bundle does not match the digest the server reported "
            f"(expected {declared}, got {actual})."
        )


def _git_provenance(directory: Path) -> dict[str, str]:
    """Best-effort commit metadata, so a deployed version points back at its source.

    Never fatal: pushing from a tarball or a detached CI checkout without git metadata
    is legitimate, and refusing to deploy over missing provenance would be obstructive.
    """
    provenance: dict[str, str] = {}
    for key, args in (
        ("git_commit_sha", ["rev-parse", "HEAD"]),
        ("git_ref", ["rev-parse", "--abbrev-ref", "HEAD"]),
        ("git_repo_url", ["config", "--get", "remote.origin.url"]),
    ):
        try:
            result = subprocess.run(
                ["git", "-C", str(directory), *args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            provenance[key] = value
    return provenance


__all__ = ["functions"]
