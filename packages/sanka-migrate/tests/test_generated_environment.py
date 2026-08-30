# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sanka.runtime.frameworks.generated_environment import (
    GeneratedEnvironmentError,
    ensure_generated_environment,
)
from sanka.runtime.frameworks.generated_integrity import attest_generated_bundle


def _bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SANKA_INTEGRITY_KEY_PATH", str(tmp_path / "integrity.key"))
    output = tmp_path / "generated"
    output.mkdir()
    (output / "app.py").write_text("app = object()\n", encoding="utf-8")
    (output / "pyproject.toml").write_text(
        "[project]\nname='target'\nversion='0.0.0'\n", encoding="utf-8"
    )
    manifest = attest_generated_bundle(
        output,
        {"schema_version": 1, "generated_files": ["app.py"]},
        protected_names=["app.py", "pyproject.toml"],
    )
    (output / "sanka-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return output


def test_generated_environment_is_disposable_and_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _bundle(tmp_path, monkeypatch)
    source_python = tmp_path / "source-python"
    source_python.write_text("", encoding="utf-8")
    recorded: list[tuple[list[str], dict[str, str]]] = []

    monkeypatch.setattr(
        "sanka.runtime.frameworks.generated_environment.shutil.which",
        lambda name: "/usr/local/bin/uv" if name == "uv" else None,
    )

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        recorded.append((command, environment))
        project = Path(command[command.index("--project") + 1])
        if command[1] == "lock":
            (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        else:
            environment_root = Path(environment["UV_PROJECT_ENVIRONMENT"])
            (environment_root / "bin").mkdir(parents=True)
            (environment_root / "bin" / "python").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("sanka.runtime.frameworks.generated_environment.subprocess.run", fake_run)

    environment = ensure_generated_environment(output, python=source_python)

    assert environment.root.parent == environment.pyproject.parent
    assert environment.root != output / ".venv"
    assert environment.python == environment.root / "bin" / "python"
    assert environment.lockfile == environment.pyproject.parent / "uv.lock"
    assert [command[1] for command, _ in recorded] == ["lock", "sync"]
    assert "--locked" in recorded[1][0]
    assert "--no-config" in recorded[0][0]
    assert "--no-config" in recorded[1][0]
    assert "VIRTUAL_ENV" not in recorded[1][1]


def test_generated_environment_requires_attested_bundle(tmp_path: Path) -> None:
    with pytest.raises(GeneratedEnvironmentError, match="manifest"):
        ensure_generated_environment(tmp_path)


def test_generated_environment_reports_missing_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _bundle(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "sanka.runtime.frameworks.generated_environment.shutil.which", lambda _name: None
    )

    with pytest.raises(GeneratedEnvironmentError, match="uv is required"):
        ensure_generated_environment(output)


def test_generated_environment_reports_lock_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _bundle(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "sanka.runtime.frameworks.generated_environment.shutil.which", lambda _name: "uv"
    )
    monkeypatch.setattr(
        "sanka.runtime.frameworks.generated_environment.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "bad lock"),
    )

    with pytest.raises(GeneratedEnvironmentError, match="bad lock"):
        ensure_generated_environment(output)
