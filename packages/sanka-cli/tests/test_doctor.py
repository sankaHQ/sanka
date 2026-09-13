# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from sanka_cli import __version__
from sanka_cli.main import cli


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
