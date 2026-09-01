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


def test_boundaries_catch_mcp_importing_runtime_namespace() -> None:
    fixture = ROOT / "tests" / "fixtures" / "mcp_boundary_violation"
    result = _run("check_import_boundaries.py", str(fixture))
    assert result.returncode == 1
    assert "sanka.runtime" in result.stdout
    assert "standalone MCP package" in result.stdout


def test_boundaries_catch_target_logic_in_the_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "packages" / "sanka-migrate" / "src" / "sanka" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "leak.py").write_text(
        "from sanka.runtime.frameworks import scan_django\nimport asyncpg\nimport fastapi\n",
        encoding="utf-8",
    )

    result = _run("check_import_boundaries.py", str(tmp_path))

    assert result.returncode == 1
    assert "asyncpg" in result.stdout
    assert "sanka.runtime.frameworks" in result.stdout
    assert "fastapi" in result.stdout
    assert "target-specific" in result.stdout


def test_license_headers_pass_on_repo() -> None:
    result = _run("check_license_headers.py")
    assert result.returncode == 0, result.stdout + result.stderr
