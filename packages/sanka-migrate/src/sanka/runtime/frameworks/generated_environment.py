# SPDX-License-Identifier: AGPL-3.0-only
"""Create an isolated Python environment for a generated migration target."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from dataclasses import dataclass
from pathlib import Path


class GeneratedEnvironmentError(RuntimeError):
    """Raised when a generated target environment cannot be prepared."""


@dataclass(frozen=True)
class GeneratedEnvironment:
    root: Path
    python: Path
    pyproject: Path | None
    lockfile: Path | None


def ensure_generated_environment(
    output: Path,
    *,
    python: str | Path = sys.executable,
) -> GeneratedEnvironment:
    """Prepare ``output/.venv`` with the package manager selected in the plan."""
    output = output.resolve()
    pyproject = output / "pyproject.toml"
    manifest = output / "sanka-manifest.json"
    package_manager = "uv"
    if manifest.is_file():
        try:
            package_manager = str(
                json.loads(manifest.read_text(encoding="utf-8")).get("package_manager") or "uv"
            )
        except (json.JSONDecodeError, OSError) as error:
            raise GeneratedEnvironmentError(
                f"could not read generated manifest: {manifest}"
            ) from error
    if package_manager not in {"uv", "pip"}:
        raise GeneratedEnvironmentError(
            f"unsupported generated package manager {package_manager!r}"
        )
    if package_manager == "pip":
        return _ensure_pip_environment(output, pyproject if pyproject.is_file() else None)
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
    except (OSError, subprocess.SubprocessError) as error:
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


def _ensure_pip_environment(output: Path, pyproject: Path | None) -> GeneratedEnvironment:
    requirements = [
        path
        for path in (output / "requirements.txt", output / "requirements-test.txt")
        if path.is_file()
    ]
    if not requirements:
        raise GeneratedEnvironmentError(
            f"generated dependency metadata is missing in {output}; rerun `sanka apply`"
        )
    environment_root = output / ".venv"
    try:
        venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(environment_root)
        python_path = _environment_python(environment_root)
        command = [str(python_path), "-m", "pip", "install"]
        for path in requirements:
            command.extend(("-r", str(path)))
        result = subprocess.run(
            command,
            cwd=output,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GeneratedEnvironmentError(
            f"could not prepare the generated app environment at {environment_root}: {error}"
        ) from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pip install failed").strip()
        raise GeneratedEnvironmentError(
            f"could not install the generated app dependencies in {environment_root}:\n{detail}"
        )
    if not python_path.is_file():
        raise GeneratedEnvironmentError(
            f"pip did not create the expected generated environment at {environment_root}"
        )
    return GeneratedEnvironment(
        root=environment_root,
        python=python_path,
        pyproject=pyproject,
        lockfile=None,
    )


def _environment_python(environment_root: Path) -> Path:
    if os.name == "nt":
        return environment_root / "Scripts" / "python.exe"
    return environment_root / "bin" / "python"
