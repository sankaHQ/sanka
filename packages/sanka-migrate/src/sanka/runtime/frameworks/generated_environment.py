# SPDX-License-Identifier: AGPL-3.0-only
"""Create an isolated Python environment for a generated migration target."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sanka.runtime.frameworks.generated_integrity import (
    GeneratedIntegrityError,
    read_generated_manifest,
    verify_generated_bundle,
)
from sanka.runtime.safe_local_io import absolute_path, safe_read_text, safe_write_text


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
    """Create a disposable environment from attested generated metadata."""
    output = absolute_path(output)
    try:
        manifest = read_generated_manifest(output / "sanka-manifest.json")
        verify_generated_bundle(output, manifest)
    except GeneratedIntegrityError as error:
        raise GeneratedEnvironmentError(str(error)) from error
    source_pyproject = output / "pyproject.toml"
    uv = shutil.which("uv")
    if uv is None:
        raise GeneratedEnvironmentError(
            "uv is required to prepare the generated app environment; "
            "install uv from https://docs.astral.sh/uv/ and rerun the command"
        )
    temporary = Path(
        tempfile.mkdtemp(
            prefix="sanka-generated-environment-",
            dir=os.path.realpath(tempfile.gettempdir()),
        )
    )
    atexit.register(shutil.rmtree, temporary, ignore_errors=True)
    project = temporary / "project"
    project.mkdir(mode=0o700)
    pyproject = safe_write_text(project / "pyproject.toml", safe_read_text(source_pyproject))
    environment_root = project / ".venv"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "LANG",
            "LC_ALL",
            "PATH",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "WINDIR",
        }
    }
    home = temporary / "home"
    cache = temporary / "uv-cache"
    home.mkdir(mode=0o700)
    cache.mkdir(mode=0o700)
    environment["HOME"] = str(home)
    environment["UV_CACHE_DIR"] = str(cache)
    environment["UV_PROJECT_ENVIRONMENT"] = str(environment_root)
    environment["UV_NO_PROGRESS"] = "1"
    try:
        lock = subprocess.run(
            [
                uv,
                "lock",
                "--project",
                str(project),
                "--python",
                str(Path(python).resolve()),
                "--no-config",
            ],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if lock.returncode != 0:
            detail = (lock.stderr or lock.stdout or "uv lock failed").strip()
            raise GeneratedEnvironmentError(
                f"could not lock generated app dependencies in {project}:\n{detail}"
            )
        result = subprocess.run(
            [
                uv,
                "sync",
                "--project",
                str(project),
                "--python",
                str(Path(python).resolve()),
                "--extra",
                "test",
                "--no-dev",
                "--no-install-project",
                "--locked",
                "--no-config",
            ],
            cwd=project,
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
    lockfile = project / "uv.lock"
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
