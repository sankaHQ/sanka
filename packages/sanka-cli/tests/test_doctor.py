# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from sanka.runtime.extensions.store import INSTALLATION_SCHEMA
from sanka_cli import __version__
from sanka_cli.main import cli


@pytest.fixture(autouse=True)
def isolated_extension_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Doctor must read the test's store, never this machine's ~/.sanka."""
    monkeypatch.setenv("SANKA_HOME", str(tmp_path / "sanka-home"))
    return tmp_path / "sanka-home" / "extensions"


def _cached_environment(user_root: Path, interpreter: Path | None) -> Path:
    digest = "a" * 64
    root = user_root / "environments" / digest
    if interpreter is not None:
        (root / "bin").mkdir(parents=True)
        launchers = (
            "python",
            "python3",
            f"python{sys.version_info.major}.{sys.version_info.minor}",
        )
        for name in launchers:
            (root / "bin" / name).symlink_to(interpreter)
    user_root.mkdir(parents=True, exist_ok=True)
    wheel = f"cache/wheels/{'e' * 64}/sanka_extension_drf_to_fastapi-0.1.0a10-py3-none-any.whl"
    (user_root / "installations.json").write_text(
        json.dumps(
            {
                "schema_version": INSTALLATION_SCHEMA,
                "installations": [
                    {
                        "artifact_digest": digest,
                        "environment": f"environments/{digest}",
                        "environment_digest": "sha256:" + "b" * 64,
                        "id": "sanka/drf-to-fastapi",
                        "manifest_digest": "sha256:" + "c" * 64,
                        "marketplace_identity": "github.com/sankaHQ/extensions",
                        "snapshot_digest": "d" * 64,
                        "version": "0.1.0a10",
                        "wheels": [{"path": wheel, "sha256": "e" * 64}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    executable = tmp_path / "bin" / "sanka"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 99\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable.parent))
    monkeypatch.setattr(sys, "argv", [str(executable), "doctor"])
    return executable


def test_doctor_is_local_and_does_not_execute_candidates(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("doctor must not execute commands or load credentials")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("sanka_cli.config.resolve_runtime", forbidden)
    result = CliRunner().invoke(cli, ["doctor", "--json", "--expected-version", __version__])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["status"] == "ok"
    assert report["active_executable"] == str(installed)
    assert report["path_executable"] == str(installed)
    assert report["python"]["executable"] == sys.executable


def test_doctor_reports_shadowing_without_running_old_homebrew(
    installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = tmp_path / "homebrew" / "bin" / "sanka"
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/sh\nexit 99\n")
    old.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(old.parent), str(installed.parent)]))
    result = CliRunner().invoke(cli, ["doctor", "--json"])
    report = json.loads(result.output)
    assert result.exit_code == 0
    assert report["status"] == "warning"
    assert report["path_executable"] == str(old)
    assert {item["code"] for item in report["checks"]} == {
        "path_selection",
        "duplicate_installations",
    }
    assert "brew upgrade sankaHQ/cli/sanka" in result.output


def test_path_aliases_to_same_installation_are_not_duplicates(
    installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = tmp_path / "aliases" / "sanka"
    alias.parent.mkdir()
    alias.symlink_to(installed)
    monkeypatch.setenv("PATH", os.pathsep.join([str(alias.parent), str(installed.parent)]))
    result = CliRunner().invoke(cli, ["doctor", "--json"])
    assert json.loads(result.output)["status"] == "ok"


def test_missing_path_has_copyable_recovery(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 0
    assert f"{installed} --help" in result.output
    assert "rehash" in result.output


def test_expected_version_mismatch_fails(installed: Path) -> None:
    result = CliRunner().invoke(cli, ["doctor", "--json", "--expected-version", "0.0.0"])
    assert result.exit_code == 1
    assert json.loads(result.output)["checks"][0]["code"] == "cli_version"


def test_runtime_requirement_is_diagnosed(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.python_version", lambda: "3.9.1")
    result = CliRunner().invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["checks"][0]["code"] == "python_version"
    assert report["checks"][0]["recovery"] == "uv tool install --python 3.12 sanka-cli"


def test_doctor_reports_extension_environment_built_on_another_interpreter(
    installed: Path,
    isolated_extension_store: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("doctor must not execute commands")

    monkeypatch.setattr("subprocess.run", forbidden)
    stale = tmp_path / "old-python"
    stale.write_text("")
    _cached_environment(isolated_extension_store, stale)
    result = CliRunner().invoke(cli, ["doctor", "--json"])
    report = json.loads(result.output)
    assert result.exit_code == 1
    assert report["status"] == "error"
    [record] = report["extensions"]["environments"]
    assert record["status"] == "interpreter_mismatch"
    assert record["launchers"]["bin/python"] == str(stale)
    check = next(item for item in report["checks"] if item["code"] == "extension_environment")
    assert check["severity"] == "error"
    assert str(stale) in check["message"]
    assert check["recovery"] == (
        "sanka extension remove sanka/drf-to-fastapi && sanka extension add sanka/drf-to-fastapi"
    )


def test_doctor_accepts_extension_environment_on_this_interpreter(
    installed: Path, isolated_extension_store: Path
) -> None:
    _cached_environment(isolated_extension_store, Path(sys.executable).resolve())
    result = CliRunner().invoke(cli, ["doctor", "--json"])
    report = json.loads(result.output)
    assert result.exit_code == 0, result.output
    assert report["status"] == "ok"
    assert [item["status"] for item in report["extensions"]["environments"]] == ["ok"]
    assert report["extensions"]["error"] is None


def test_doctor_reports_missing_extension_environment(
    installed: Path, isolated_extension_store: Path
) -> None:
    _cached_environment(isolated_extension_store, None)
    result = CliRunner().invoke(cli, ["doctor", "--json"])
    report = json.loads(result.output)
    assert result.exit_code == 1
    check = next(item for item in report["checks"] if item["code"] == "extension_environment")
    assert "missing" in check["message"]
    assert check["recovery"] == "sanka extension add sanka/drf-to-fastapi"


def test_doctor_text_output_lists_extension_environments(
    installed: Path, isolated_extension_store: Path
) -> None:
    _cached_environment(isolated_extension_store, Path(sys.executable).resolve())
    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Extension sanka/drf-to-fastapi 0.1.0a10: ok" in result.output
