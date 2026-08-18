# SPDX-License-Identifier: Apache-2.0
"""The repo guard scripts must pass on the real tree and fail on violations."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_boundaries_pass_on_repo() -> None:
    result = _run("check_import_boundaries.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_boundaries_catch_connector_importing_runtime() -> None:
    fixture = ROOT / "tests" / "fixtures" / "boundary_violation"
    result = _run("check_import_boundaries.py", str(fixture))
    assert result.returncode == 1
    assert "sanka.runtime" in result.stdout


def test_license_headers_pass_on_repo() -> None:
    result = _run("check_license_headers.py")
    assert result.returncode == 0, result.stdout + result.stderr
