# SPDX-License-Identifier: Apache-2.0
"""Gate a candidate CLI wheel using an immutable independent extension consumer."""

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

EXAMPLES_REVISION = "18560f787ee6b723ddfa983e67ae310188ffd9f8"
BASELINE_VERSION = "0.2.12"


def validate_report(report: dict[str, Any], wheel_hash: str) -> None:
    if report.get("status") != "passed":
        raise ValueError(
            f"Extension acceptance failed at {report.get('stage')}: {report.get('error')}"
        )
    if report.get("candidate", {}).get("sha256") != wheel_hash:
        raise ValueError("Acceptance report does not identify the candidate wheel")
    before = report.get("lock_sha256_before")
    if (
        not isinstance(before, str)
        or len(before) != 64
        or any(character not in "0123456789abcdef" for character in before)
        or before != report.get("lock_sha256_after")
    ):
        raise ValueError("Acceptance report does not prove unchanged extension lock bytes")
    if report.get("upgrade_from") != BASELINE_VERSION:
        raise ValueError("Acceptance report uses the wrong upgrade baseline")
    version = report.get("candidate", {}).get("version")
    if not isinstance(version, str) or not version or report.get("installed_cli") != version:
        raise ValueError("Acceptance report does not identify the installed candidate version")


def check(wheel: Path, report: dict[str, Any], root: Path) -> None:
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    report.update(
        candidate_wheel=wheel.name,
        candidate_sha256=digest,
        examples_revision=EXAMPLES_REVISION,
        upgrade_from=BASELINE_VERSION,
    )
    environment = {
        key: os.environ[key]
        for key in ("PATH", "TMPDIR", "LANG", "SYSTEMROOT")
        if key in os.environ
    }
    environment.update(
        HOME=str(root),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        UV_NO_CONFIG="1",
    )
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Install uv before running extension acceptance")

    def run(stage: str, command: list[str], cwd: Path = root) -> str:
        report["stage"] = stage
        result = subprocess.run(
            command, cwd=cwd, env=environment, capture_output=True, text=True, timeout=600
        )
        if result.returncode:
            raise RuntimeError(
                f"{stage} failed ({result.returncode})\n"
                f"{result.stdout[-12000:]}\n{result.stderr[-12000:]}"
            )
        return result.stdout.strip()

    examples = root / "examples"
    run(
        "fetch independent example",
        ["git", "clone", "https://github.com/sankaHQ/sanka-examples", str(examples)],
    )
    run("fetch immutable example", ["git", "fetch", "origin", EXAMPLES_REVISION], examples)
    run("select immutable example", ["git", "checkout", "--detach", EXAMPLES_REVISION], examples)
    if run("verify example revision", ["git", "rev-parse", "HEAD"], examples) != EXAMPLES_REVISION:
        raise ValueError("Unexpected examples revision")
    evidence = root / "consumer.json"
    try:
        run(
            "candidate extension acceptance",
            [
                uv,
                "run",
                "--no-project",
                "--python",
                "3.12",
                "python",
                str(examples / "extensions/config-upgrade/check.py"),
                "--cli-wheel",
                str(wheel),
                "--upgrade-from",
                BASELINE_VERSION,
                "--report",
                str(evidence),
            ],
        )
    finally:
        if evidence.is_file():
            report["consumer"] = json.loads(evidence.read_text())
    validate_report(report.get("consumer", {}), digest)
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != digest:
        raise ValueError("Candidate wheel changed during acceptance")
    report["status"] = "passed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli-wheel", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report: dict[str, Any] = {"status": "failed", "stage": "setup"}
    try:
        wheel = args.cli_wheel.resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="sanka-extension-release-") as directory:
            check(wheel, report, Path(directory))
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
