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


@pytest.mark.parametrize(
    "args", [["doctor", "--json"], ["--output", "json", "doctor"], ["doctor", "-h"]]
)
def test_machine_output_and_help_never_launch_tui(
    installed: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setattr(
        "sanka.cli.tui.launch.launch_doctor", lambda *args: pytest.fail("unexpected TUI")
    )
    assert CliRunner().invoke(cli, args).exit_code == 0


def test_doctor_routes_human_tty_and_preserves_exit_code(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("sanka.cli.tui.launch.use_human_tui", lambda state: True)
    seen: list[str | None] = []

    def launch(state: object, expected: str | None) -> int:
        seen.append(expected)
        return 1

    monkeypatch.setattr("sanka.cli.tui.launch.launch_doctor", launch)
    result = CliRunner().invoke(cli, ["doctor", "--expected-version", "0.0.0"])
    assert result.exit_code == 1
    assert seen == ["0.0.0"]


@pytest.mark.asyncio
async def test_doctor_screen_refresh_copy_and_trust_boundary(
    installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widgets import Button, Static

    from sanka.cli.tui.app import DoctorScreen, SankaApp, TrustFolderScreen
    from sanka.cli.tui.model import Session
    from sanka.cli.tui.services import HostServices

    monkeypatch.setattr("sanka.cli.tui.app.is_folder_trusted", lambda root: False)
    monkeypatch.setattr(HostServices, "project", lambda self: pytest.fail("doctor read project"))
    app = SankaApp(
        HostServices(tmp_path),
        Session(project_root=str(tmp_path), direct=True),
        start="doctor",
        ask_trust=True,
        doctor_expected_version="0.0.0",
    )
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DoctorScreen)
        assert "ERROR" in str(app.screen.query_one("#doctor-status", Static).content)
        assert "Expected Sanka 0.0.0" in str(app.screen.query_one("#doctor-report", Static).content)
        assert app.session.exit_code == 1
        assert app.screen.query_one("#doctor-refresh", Button).region.bottom <= 24
        await pilot.click("#doctor-copy")
        assert json.loads(copied[-1])["schema"] == "sanka-doctor/v1"
        app.doctor_expected_version = __version__
        await pilot.press("r")
        assert app.session.exit_code == 0
        assert "No installation issues" in str(
            app.screen.query_one("#doctor-report", Static).content
        )
        app.action_stage("scan")
        await pilot.pause()
        assert isinstance(app.screen, TrustFolderScreen)


@pytest.mark.asyncio
async def test_direct_doctor_escape_exits(installed: Path, tmp_path: Path) -> None:
    from sanka.cli.tui.app import SankaApp
    from sanka.cli.tui.model import Session
    from sanka.cli.tui.services import HostServices

    app = SankaApp(
        HostServices(tmp_path),
        Session(project_root=str(tmp_path), direct=True),
        start="doctor",
        doctor_expected_version="0.0.0",
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("escape")
    assert app.return_value == 1
