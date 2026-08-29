# SPDX-License-Identifier: AGPL-3.0-only
"""Create an isolated Python environment for a generated migration target."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class GeneratedEnvironmentError(RuntimeError):
    """Raised when a generated target environment cannot be prepared."""


@dataclass(frozen=True)
class GeneratedEnvironment:
    root: Path
    python: Path
    pyproject: Path
    lockfile: Path


def ensure_generated_environment(
    output: Path,
    *,
    python: str | Path = sys.executable,
) -> GeneratedEnvironment:
    """Sync ``output/.venv`` from the generated target's ``pyproject.toml``."""
    output = output.resolve()
    pyproject = output / "pyproject.toml"
    if not pyproject.is_file():
        raise GeneratedEnvironmentError(
            f"generated dependency metadata is missing: {pyproject}; rerun `sanka apply`"
        )
    uv = shutil.which("uv")
    if uv is None:
        raise GeneratedEnvironmentError(
            "uv is required to prepare the generated app environment; "
            "install uv from https://docs.astral.sh/uv/ and rerun the command"
        )
    environment_root = output / ".venv"
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    environment["UV_PROJECT_ENVIRONMENT"] = str(environment_root)
    environment["UV_NO_PROGRESS"] = "1"
    try:
        result = subprocess.run(
            [
                uv,
                "sync",
                "--project",
                str(output),
                "--python",
                str(Path(python).resolve()),
                "--extra",
                "test",
                "--no-dev",
                "--no-install-project",
            ],
            cwd=output,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GeneratedEnvironmentError(
            f"could not prepare the generated app environment at {environment_root}: {error}"
        ) from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "uv sync failed").strip()
        raise GeneratedEnvironmentError(
            f"could not install the generated app dependencies in {environment_root}:\n{detail}"
        )
    python_path = _environment_python(environment_root)
    lockfile = output / "uv.lock"
    if not python_path.is_file() or not lockfile.is_file():
        raise GeneratedEnvironmentError(
            f"uv did not create the expected generated environment at {environment_root}"
        )
    return GeneratedEnvironment(
        root=environment_root,
        python=python_path,
        pyproject=pyproject,
        lockfile=lockfile,
    )


def _environment_python(environment_root: Path) -> Path:
    if os.name == "nt":
        return environment_root / "Scripts" / "python.exe"
    return environment_root / "bin" / "python"
