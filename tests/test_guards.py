# SPDX-License-Identifier: Apache-2.0
"""The repo guard scripts must pass on the real tree and fail on violations."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from scripts import check_public_naming
from scripts.check_license_headers import expected_license

ROOT = Path(__file__).resolve().parent.parent


def test_unified_project_metadata() -> None:
    project = tomllib.loads((ROOT / "packages" / "sanka-cli" / "pyproject.toml").read_text())[
        "project"
    ]
    assert project["name"] == "sanka-cli"
    assert project["version"] == "0.2.4"
    assert project["license"] == "Apache-2.0 AND AGPL-3.0-only"
    assert project["scripts"] == {"sanka": "sanka_cli.main:main"}


def test_workspace_has_no_legacy_package_member() -> None:
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert workspace["project"]["name"] == "sanka-cli-workspace"
    assert workspace["tool"]["uv"]["workspace"]["members"] == ["packages/sanka-cli"]


def test_unified_package_license_zones() -> None:
    assert expected_license(Path("packages/sanka-cli/src/sanka/runtime/spec.py")) == (
        "AGPL-3.0-only"
    )
    assert expected_license(Path("packages/sanka-cli/src/sanka_cli/main.py")) == "Apache-2.0"
    assert expected_license(Path("packages/sanka-cli/src/sanka_connector/schema.py")) == (
        "Apache-2.0"
    )


def test_base_cli_import_registers_mcp_without_loading_extra() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from sanka_cli.main import cli; "
                "assert 'mcp' in cli.commands; "
                "assert 'mcp' not in sys.modules; "
                "assert 'sanka_cli.mcp.server' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_connector_sdk_sync_accepts_identical_python_trees(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    embedded = tmp_path / "packages" / "sanka-cli" / "src" / "sanka_connector"
    canonical.mkdir()
    embedded.mkdir(parents=True)
    for root in (canonical, embedded):
        (root / "__init__.py").write_text("# SPDX-License-Identifier: Apache-2.0\n")
        (root / "py.typed").touch()
    env = os.environ | {"SANKA_CONNECTOR_SDK_SOURCE": str(canonical)}

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_connector_sdk_sync.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_connector_sdk_sync_make_target_uses_recorded_snapshot() -> None:
    env = os.environ.copy()
    env.pop("SANKA_CONNECTOR_SDK_SOURCE", None)

    result = subprocess.run(
        ["make", "connector-sdk-sync"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_connector_sdk_sync_reports_byte_drift(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    embedded = tmp_path / "packages" / "sanka-cli" / "src" / "sanka_connector"
    canonical.mkdir()
    embedded.mkdir(parents=True)
    (canonical / "schema.py").write_text("canonical\n")
    (embedded / "schema.py").write_text("embedded\n")
    env = os.environ | {"SANKA_CONNECTOR_SDK_SOURCE": str(canonical)}

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_connector_sdk_sync.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "embedded connector SDK drift: schema.py" in result.stderr


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


def test_boundaries_catch_mcp_integration_importing_runtime_namespace() -> None:
    fixture = ROOT / "tests" / "fixtures" / "mcp_boundary_violation"
    result = _run("check_import_boundaries.py", str(fixture))
    assert result.returncode == 1
    assert "sanka.runtime" in result.stdout
    assert "MCP integration cannot import the AGPL runtime" in result.stdout


def test_boundaries_catch_connector_sdk_importing_runtime_namespace(tmp_path: Path) -> None:
    connector = tmp_path / "packages" / "sanka-cli" / "src" / "sanka_connector"
    connector.mkdir(parents=True)
    (connector / "bad.py").write_text("from sanka.runtime import engine\n", encoding="utf-8")

    result = _run("check_import_boundaries.py", str(tmp_path))

    assert result.returncode == 1
    assert "sanka.runtime" in result.stdout
    assert "Connector SDK cannot import the AGPL runtime" in result.stdout


def test_boundaries_catch_target_logic_in_the_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "packages" / "sanka-cli" / "src" / "sanka" / "runtime"
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


def test_public_naming_passes_on_repo() -> None:
    result = _run("check_public_naming.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_public_naming_finds_mixed_case_retired_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    active_file = tmp_path / "active.md"
    active_file.write_text("Retired name: " + "FER" + "RY" + "\n", encoding="utf-8")
    monkeypatch.setattr(check_public_naming, "ROOT", tmp_path)

    assert check_public_naming._tree_references(check_public_naming.RETIRED_TOKEN) == [
        "content: active.md"
    ]


def test_dependency_licenses_pass_on_workspace() -> None:
    result = _run("check_dependency_licenses.py")
    assert result.returncode == 0, result.stdout + result.stderr
