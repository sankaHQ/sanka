# SPDX-License-Identifier: Apache-2.0
"""Check the public quickstart with isolated CLI and source environments.

Use a built wheel before publication or sanka-cli==VERSION after publication.
Only extension installation, scan and plan run; no application migration runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

EXAMPLES_REVISION = "5d27fbcb6759c1fc0bfd0cc556e570851a62ab6d"


def check_quickstart(
    requirement: str, root: Path, report: dict[str, Any], upgrade_from: str | None = None
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Install uv before running quickstart acceptance")
    # Do not inherit development imports, active environments, credentials,
    # package indexes or a user's existing Sanka marketplace configuration.
    environment = {
        key: os.environ[key]
        for key in ("PATH", "TMPDIR", "LANG", "SYSTEMROOT")
        if key in os.environ
    }
    environment.update(
        UV_TOOL_DIR=str(root / "tools"),
        UV_TOOL_BIN_DIR=str(root / "bin"),
        UV_CACHE_DIR=str(root / "uv-cache"),
        UV_PYTHON_INSTALL_DIR=str(root / "python"),
        UV_PYTHON_PREFERENCE="only-managed",
        UV_NO_CONFIG="1",
        UV_NO_PROGRESS="1",
        SANKA_HOME=str(root / "sanka-home"),
        XDG_CONFIG_HOME=str(root / "config"),
        XDG_CACHE_HOME=str(root / "cache"),
        PYTHONNOUSERSITE="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )

    def run(name: str, command: list[str], cwd: Path = root, timeout: int = 300) -> str:
        print(f"Quickstart: {name}", flush=True)
        result = subprocess.run(
            command, cwd=cwd, env=environment, capture_output=True, text=True, timeout=timeout
        )
        report["steps"].append({"name": name, "exit_code": result.returncode})
        if result.returncode:
            raise RuntimeError(f"{name} failed\n{result.stdout[-8000:]}\n{result.stderr[-8000:]}")
        return result.stdout.strip()

    examples = root / "examples"
    run(
        "clone public example",
        ["git", "clone", "https://github.com/sankaHQ/sanka-examples", str(examples)],
    )
    run("select reviewed example", ["git", "checkout", "--detach", EXAMPLES_REVISION], examples)
    report["example_revision"] = run(
        "verify example revision", ["git", "rev-parse", "HEAD"], examples
    )
    assert report["example_revision"] == EXAMPLES_REVISION
    project = examples / "django/order-tracker"
    run("create source environment", [uv, "venv", "--python", "3.12", ".venv"], project)
    source_python = project / ".venv/bin/python"
    run(
        "install source requirements",
        [uv, "pip", "install", "--python", str(source_python), "-r", "requirements.txt"],
        project,
    )
    run(
        "verify source isolation",
        [
            str(source_python),
            "-I",
            "-c",
            "import importlib.util,sys; import django,rest_framework; "
            "assert importlib.util.find_spec('sanka') is None; "
            "assert sys.version_info[:2] == (3,12)",
        ],
        project,
    )
    cli = root / "bin/sanka"
    cli_python = root / "tools/sanka-cli/bin/python"
    lock_path = project / ".sanka/extensions.lock"
    previous_lock = None
    if upgrade_from:
        run(
            "install previous CLI",
            [uv, "tool", "install", "--python", "3.12", f"sanka-cli=={upgrade_from}"],
            project,
        )
        run(
            "lock previous public extension",
            [str(cli), "extension", "add", "sanka/drf-to-fastapi"],
            project,
        )
        previous_lock = lock_path.read_bytes()
        report["previous_extensions"] = json.loads(previous_lock)["extensions"]
    run("install isolated CLI", [uv, "tool", "install", "--python", "3.12", requirement], project)
    report["cli_version"] = run("CLI version", [str(cli), "--version"], project)
    report["runtime"] = json.loads(
        run(
            "verify CLI isolation",
            [
                str(cli_python),
                "-I",
                "-c",
                "import importlib.util,json,sys; "
                "assert importlib.util.find_spec('django') is None; "
                "assert importlib.util.find_spec('rest_framework') is None; "
                "assert sys.version_info[:2] == (3,12); "
                "print(json.dumps({'python':sys.version.split()[0], 'django_in_cli':False}))",
            ],
            project,
        )
    )
    # Model `source .venv/bin/activate`: the CLI still resolves from uv's bin.
    inactive_path = f"{root / 'bin'}:{environment['PATH']}"
    environment["VIRTUAL_ENV"] = str(project / ".venv")
    environment["PATH"] = f"{project / '.venv/bin'}:{inactive_path}"
    assert shutil.which("sanka", path=environment["PATH"]) == str(cli)
    run("doctor", ["sanka", "doctor"], project)
    if previous_lock is not None:
        assert lock_path.read_bytes() == previous_lock, "A CLI upgrade must preserve project pins"
        run(
            "refresh the official catalog",
            ["sanka", "extension", "marketplace", "upgrade", "official"],
            project,
        )
        assert lock_path.read_bytes() == previous_lock, (
            "A catalog refresh must preserve project pins"
        )
    run("install public extension", ["sanka", "extension", "add", "sanka/drf-to-fastapi"], project)
    lock_bytes = lock_path.read_bytes()
    locked = json.loads(lock_bytes)["extensions"]
    extension = next(item for item in locked if item["id"] == "sanka/drf-to-fastapi")
    assert extension["marketplace_identity"] == "github.com/sankaHQ/extensions", extension
    report["extension"] = extension
    if previous_lock is not None:
        assert extension["version"] != report["previous_extensions"][0]["version"]
        report["explicit_extension_upgrade"] = "passed"
    scans = []
    for name in ("scan with source activated", "scan without activation"):
        scans.append(json.loads(run(name, ["sanka", "scan", ".", "--json"], project)))
        environment.pop("VIRTUAL_ENV", None)
        environment["PATH"] = inactive_path
    assert scans[0]["scan_hash"] == scans[1]["scan_hash"]
    assert scans[0]["routes"], "The public example must expose routes"
    report["scan_hash"] = scans[0]["scan_hash"]
    report["route_count"] = len(scans[0]["routes"])
    plan = json.loads(
        run(
            "plan minimal native FastAPI",
            [
                "sanka",
                "plan",
                ".",
                "--to",
                "fastapi",
                "--generation",
                "minimal",
                "--output",
                ".sanka/output/fastapi",
                "--strategy",
                "native",
                "--package-manager",
                "uv",
                "--json",
            ],
            project,
        )
    )
    assert plan["source_scan_hash"] == report["scan_hash"]
    assert plan["plan_hash"].startswith("sha256:")
    assert lock_path.read_bytes() == lock_bytes, "Scan and plan must preserve the extension lock"
    assert not (project / ".sanka/output/fastapi").exists(), "Planning must not generate the app"
    report["plan_hash"] = plan["plan_hash"]
    report["extension_lock_sha256"] = hashlib.sha256(lock_bytes).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True, help="Built CLI wheel or sanka-cli==VERSION")
    parser.add_argument("--upgrade-from", help="Also check adoption from a published CLI version")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    requirement = str(Path(args.cli).resolve()) if Path(args.cli).is_file() else args.cli
    report: dict[str, Any] = {
        "cli_requirement": requirement,
        "status": "failed",
        "steps": [],
        "not_run": ["apply", "test", "verify"],
    }
    if Path(requirement).is_file():
        report["cli_wheel_sha256"] = hashlib.sha256(Path(requirement).read_bytes()).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="sanka-quickstart-") as temporary:
            check_quickstart(requirement, Path(temporary).resolve(), report)
        if args.upgrade_from:
            upgrade: dict[str, Any] = {"from": args.upgrade_from, "steps": []}
            report["upgrade"] = upgrade
            with tempfile.TemporaryDirectory(prefix="sanka-quickstart-upgrade-") as temporary:
                check_quickstart(requirement, Path(temporary).resolve(), upgrade, args.upgrade_from)
        report["status"] = "passed"
    except Exception as error:
        report["failure_reason"] = str(error)
        raise
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Quickstart acceptance passed: {args.report}")


if __name__ == "__main__":
    main()
