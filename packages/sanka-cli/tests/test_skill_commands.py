# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from sanka_cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_install_claude_skill_in_project(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "skill",
            "install",
            "claude",
            "--scope",
            "project",
            "--project-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    skill_file = tmp_path / ".claude" / "skills" / "sanka-cli" / "SKILL.md"
    assert skill_file.is_file()
    bundled = Path(__file__).parents[1] / "src/sanka_cli/skills/sanka-cli/SKILL.md"
    assert skill_file.read_bytes() == bundled.read_bytes()
    assert (
        json.loads(result.output)["content_sha256"]
        == hashlib.sha256(skill_file.read_bytes()).hexdigest()
    )
    assert json.loads(result.output)["installations"] == [
        {
            "harness": "claude",
            "path": str(skill_file.parent),
            "status": "installed",
        }
    ]


@pytest.mark.parametrize(
    ("harness", "environment_name", "config_directory"),
    [
        ("claude", "CLAUDE_CONFIG_DIR", ".claude-test"),
        ("codex", "CODEX_HOME", ".codex-test"),
    ],
)
def test_install_skill_globally(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness: str,
    environment_name: str,
    config_directory: str,
) -> None:
    config_root = tmp_path / config_directory
    monkeypatch.setenv(environment_name, str(config_root))

    result = runner.invoke(
        cli,
        ["--output", "json", "skill", "install", harness, "--scope", "global"],
    )

    assert result.exit_code == 0, result.output
    assert (config_root / "skills" / "sanka-cli" / "SKILL.md").is_file()


def test_install_prompts_for_scope(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli,
        ["--output", "json", "skill", "install", "codex", "--project-dir", str(tmp_path)],
        input="project\n",
    )

    assert result.exit_code == 0, result.output
    assert "Install scope (project, global) [project]:" in result.stderr
    assert json.loads(result.stdout)["scope"] == "project"
    assert (tmp_path / ".codex" / "skills" / "sanka-cli" / "SKILL.md").is_file()


def test_install_without_harness_detects_available_harnesses(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for executable_name in ("claude", "codex"):
        executable = bin_directory / executable_name
        executable.touch(mode=0o755)
    monkeypatch.setenv("PATH", str(bin_directory))

    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "skill",
            "install",
            "--scope",
            "project",
            "--project-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [item["harness"] for item in payload["installations"]] == ["claude", "codex"]


def test_install_is_idempotent_and_refuses_conflicts_without_force(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    arguments = [
        "--output",
        "json",
        "skill",
        "install",
        "claude",
        "--scope",
        "project",
        "--project-dir",
        str(tmp_path),
    ]
    assert runner.invoke(cli, arguments).exit_code == 0

    unchanged = runner.invoke(cli, arguments)
    assert unchanged.exit_code == 0
    assert json.loads(unchanged.output)["installations"][0]["status"] == "unchanged"

    skill_file = tmp_path / ".claude" / "skills" / "sanka-cli" / "SKILL.md"
    skill_file.write_text("local changes\n")
    conflict = runner.invoke(cli, arguments)
    assert conflict.exit_code == 1
    assert "already exists with different content" in conflict.output
    assert skill_file.read_text() == "local changes\n"

    updated = runner.invoke(cli, [*arguments, "--force"])
    assert updated.exit_code == 0
    assert json.loads(updated.output)["installations"][0]["status"] == "updated"
    assert skill_file.read_text().startswith("---\nname: sanka-cli\n")


def test_detected_harness_conflict_does_not_partially_install(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for executable_name in ("claude", "codex"):
        executable = bin_directory / executable_name
        executable.touch(mode=0o755)
    monkeypatch.setenv("PATH", str(bin_directory))
    codex_skill = tmp_path / ".codex" / "skills" / "sanka-cli" / "SKILL.md"
    codex_skill.parent.mkdir(parents=True)
    codex_skill.write_text("local changes\n")

    result = runner.invoke(
        cli,
        [
            "skill",
            "install",
            "--scope",
            "project",
            "--project-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert not (tmp_path / ".claude" / "skills" / "sanka-cli").exists()
    assert codex_skill.read_text() == "local changes\n"


@pytest.mark.parametrize("symlink_target", ["directory", "file"])
def test_project_install_refuses_symlinked_destination(
    runner: CliRunner,
    tmp_path: Path,
    symlink_target: str,
) -> None:
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    outside_file = outside_directory / "outside.md"
    outside_file.write_text("keep me\n")
    project = tmp_path / "project"
    skill_file = project / ".claude" / "skills" / "sanka-cli" / "SKILL.md"
    if symlink_target == "directory":
        skill_file.parent.parent.mkdir(parents=True)
        skill_file.parent.symlink_to(outside_directory, target_is_directory=True)
    else:
        skill_file.parent.mkdir(parents=True)
        skill_file.symlink_to(outside_file)

    result = runner.invoke(
        cli,
        [
            "skill",
            "install",
            "claude",
            "--scope",
            "project",
            "--project-dir",
            str(project),
            "--force",
        ],
    )

    assert result.exit_code == 1
    assert "symbolic link" in result.output
    assert outside_file.read_text() == "keep me\n"
