# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sanka.runtime.frameworks.generated_environment import (
    GeneratedEnvironmentError,
    ensure_generated_environment,
)


def test_generated_environment_is_synced_inside_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "generated"
    output.mkdir()
    pyproject = output / "pyproject.toml"
    pyproject.write_text("[project]\nname='target'\nversion='0.0.0'\n", encoding="utf-8")
    source_python = tmp_path / "source-python"
    source_python.write_text("", encoding="utf-8")
    recorded: dict[str, object] = {}

    monkeypatch.setattr(
        "sanka.runtime.frameworks.generated_environment.shutil.which",
        lambda name: "/usr/local/bin/uv" if name == "uv" else None,
    )

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded["environment"] = kwargs["env"]
        environment_root = output / ".venv"
        (environment_root / "bin").mkdir(parents=True)
        (environment_root / "bin" / "python").write_text("", encoding="utf-8")
        (output / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("sanka.runtime.frameworks.generated_environment.subprocess.run", fake_run)

    environment = ensure_generated_environment(output, python=source_python)

    assert environment.root == output / ".venv"
    assert environment.python == output / ".venv" / "bin" / "python"
    assert environment.pyproject == pyproject
    assert environment.lockfile == output / "uv.lock"
    assert recorded["command"] == [
        "/usr/local/bin/uv",
        "sync",
        "--project",
        str(output),
        "--python",
        str(source_python),
        "--extra",
        "test",
        "--no-dev",
        "--no-install-project",
    ]
    assert recorded["environment"]["UV_PROJECT_ENVIRONMENT"] == str(output / ".venv")  # type: ignore[index]


def test_generated_environment_requires_generated_pyproject(tmp_path: Path) -> None:
    with pytest.raises(GeneratedEnvironmentError, match="rerun `sanka apply`"):
        ensure_generated_environment(tmp_path)


def test_generated_environment_reports_missing_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(
        "sanka.runtime.frameworks.generated_environment.shutil.which", lambda _name: None
    )

    with pytest.raises(GeneratedEnvironmentError, match="uv is required"):
        ensure_generated_environment(tmp_path)


def test_generated_environment_reports_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(
        "sanka.runtime.frameworks.generated_environment.shutil.which", lambda _name: "uv"
    )
    monkeypatch.setattr(
        "sanka.runtime.frameworks.generated_environment.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "bad lock"),
    )

    with pytest.raises(GeneratedEnvironmentError, match="bad lock"):
        ensure_generated_environment(tmp_path)
