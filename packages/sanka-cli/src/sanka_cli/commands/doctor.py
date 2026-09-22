# SPDX-License-Identifier: Apache-2.0
"""Read-only installation diagnostics. Never execute other PATH candidates."""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import sys
from importlib.metadata import metadata
from pathlib import Path
from typing import Any

import click
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from sanka_cli import __version__
from sanka_cli.state import CLIState


def executables_on_path() -> list[dict[str, str]]:
    names = ["sanka"] if os.name != "nt" else ["sanka.exe", "sanka.cmd", "sanka.bat"]
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for directory in os.get_exec_path():
        for name in names:
            path = Path(directory or os.curdir).absolute() / name
            if str(path) in seen or not path.is_file() or not os.access(path, os.X_OK):
                continue
            seen.add(str(path))
            records.append({"path": str(path), "resolved_path": str(path.resolve())})
    return records


def extension_environments() -> dict[str, Any]:
    """Cached extension environments and whether this interpreter can still run them.

    The store is read, never repaired, and nothing is executed. A missing store
    means no extension has been installed on this machine.
    """
    from sanka.runtime.extensions.model import ExtensionError
    from sanka.runtime.extensions.store import ExtensionStore, user_extension_root

    if not user_extension_root().is_dir():
        return {"environments": [], "error": None}
    try:
        store = ExtensionStore(Path.cwd())
        try:
            records = [dict(record) for record in store.installation_health()]
        finally:
            store.close()
    except (ExtensionError, OSError) as error:
        return {"environments": [], "error": str(error)}
    return {"environments": records, "error": None}


def _extension_checks(extensions: dict[str, Any]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    if extensions["error"]:
        checks.append(
            {
                "code": "extension_store",
                "severity": "warning",
                "message": f"Extension store could not be read: {extensions['error']}",
                "recovery": "Run sanka extension list for the full error.",
            }
        )
    for record in extensions["environments"]:
        identifier = f"{record['id']} {record['version']}"
        if record["status"] == "missing":
            checks.append(
                {
                    "code": "extension_environment",
                    "severity": "error",
                    "message": f"Extension environment for {identifier} is missing.",
                    "recovery": f"sanka extension add {record['id']}",
                }
            )
        elif record["status"] == "interpreter_mismatch":
            launcher = record["launchers"].get("bin/python") or "missing"
            checks.append(
                {
                    "code": "extension_environment",
                    "severity": "error",
                    "message": (
                        f"Extension environment for {identifier} was built with a different "
                        f"Python interpreter (bin/python -> {launcher}; this CLI runs "
                        f"{record['expected_interpreter']}). Lifecycle commands will fail "
                        "until it is rebuilt."
                    ),
                    "recovery": (
                        f"sanka extension remove {record['id']} && "
                        f"sanka extension add {record['id']}"
                    ),
                }
            )
    return checks


def installation_report(expected_version: str | None = None) -> dict[str, Any]:
    candidates = executables_on_path()
    active = Path(shutil.which(sys.argv[0]) or sys.argv[0]).absolute()
    winner = shutil.which("sanka")
    required = metadata("sanka-cli").get("Requires-Python", ">=3.12")
    runtime = platform.python_version()
    checks: list[dict[str, str]] = []
    if Version(runtime) not in SpecifierSet(required):
        checks.append(
            {
                "code": "python_version",
                "severity": "error",
                "message": f"Python {runtime} does not satisfy {required}.",
                "recovery": "uv tool install --python 3.12 sanka-cli",
            }
        )
    if expected_version and __version__ != expected_version:
        checks.append(
            {
                "code": "cli_version",
                "severity": "error",
                "message": f"Expected Sanka {expected_version}; running {__version__}.",
                "recovery": "Upgrade using the installation method that owns this executable.",
            }
        )
    if not winner or Path(winner).resolve() != active.resolve():
        checks.append(
            {
                "code": "path_selection",
                "severity": "warning",
                "message": f"PATH selects {winner or 'no sanka executable'}.",
                "recovery": (
                    f"Run {shlex.quote(str(active))} --help directly, "
                    "or put its directory first on PATH."
                ),
            }
        )
    if len({item["resolved_path"] for item in candidates}) > 1:
        checks.append(
            {
                "code": "duplicate_installations",
                "severity": "warning",
                "message": "Multiple Sanka installations are available on PATH.",
                "recovery": (
                    "Keep one installation method. Homebrew: brew upgrade sankaHQ/cli/sanka. "
                    "uv: uv tool upgrade sanka-cli --python 3.12. "
                    "Remove an old copy only through its owning package manager."
                ),
            }
        )
    extensions = extension_environments()
    checks.extend(_extension_checks(extensions))
    status = (
        "error"
        if any(item["severity"] == "error" for item in checks)
        else "warning"
        if checks
        else "ok"
    )
    return {
        "schema": "sanka-doctor/v1",
        "status": status,
        "cli_version": __version__,
        "expected_version": expected_version,
        "active_executable": str(active),
        "resolved_executable": str(active.resolve()),
        "path_executable": winner,
        "executables": candidates,
        "python": {
            "version": runtime,
            "required": required,
            "executable": sys.executable,
            "environment": sys.prefix,
        },
        "extensions": extensions,
        "checks": checks,
        "shell_note": (
            "PATH lookup cannot inspect your parent shell's aliases or command cache. "
            "In zsh run rehash; in bash run hash -r, then sanka --version."
        ),
    }


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Emit the sanka-doctor/v1 diagnostic report.")
@click.option(
    "--expected-version", help="Fail if the running CLI does not match this exact version."
)
@click.pass_obj
def doctor(state: CLIState, as_json: bool, expected_version: str | None) -> None:
    """Check installation, Python, PATH and cached extension environments locally."""
    report = installation_report(expected_version)
    if as_json or state.output == "json":
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        click.echo(f"Sanka {report['cli_version']} — {report['status']}")
        click.echo(f"Executable: {report['active_executable']}")
        click.echo(f"Python: {report['python']['version']} ({sys.executable})")
        click.echo(f"PATH selects: {report['path_executable'] or 'none'}")
        for item in report["executables"]:
            click.echo(f"  {item['path']} -> {item['resolved_path']}")
        for record in report["extensions"]["environments"]:
            click.echo(f"Extension {record['id']} {record['version']}: {record['status']}")
        for check in report["checks"]:
            click.echo(f"{check['severity'].upper()}: {check['message']}\n  {check['recovery']}")
        click.echo(report["shell_note"])
    if report["status"] == "error":
        # The console entrypoint uses standalone_mode=False, which turns Click's
        # Exit into a return value. Match the other local commands' process exit.
        raise SystemExit(1)
