# SPDX-License-Identifier: Apache-2.0
"""Exercise the real shell installer with a controlled uv transport.

Public downloads and Python provisioning are separately covered by the release
smoke workflow. These tests never install into the developer's home directory.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "runtime with spaces"
    bindir = tmp_path / "bin with spaces"
    bootstrap = root / "bootstrap"
    bootstrap.mkdir(parents=True)
    stub = bootstrap / "uv"
    cli_source = '#!/bin/sh\nif [ "$1" = "--version" ]; then\n  echo "sanka, version VERSION"\nfi\n'
    python_source = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
    stub.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[:2] == ['tool', 'install']\n"
        "assert '--managed-python' in args and '--no-config' in args\n"
        "assert args[args.index('--python')+1] == '3.12'\n"
        "assert args[args.index('--default-index')+1] == 'https://pypi.org/simple'\n"
        "assert 'PYTHONPATH' not in os.environ and 'VIRTUAL_ENV' not in os.environ\n"
        "version = args[-1].split('==')[1]\n"
        "bin = pathlib.Path(os.environ['UV_TOOL_BIN_DIR'])\n"
        "bin.mkdir(parents=True, exist_ok=True)\n"
        "tool = pathlib.Path(os.environ['UV_TOOL_DIR'])/'sanka-cli/bin'\n"
        "tool.mkdir(parents=True, exist_ok=True)\n"
        f"python = tool/'python'; python.write_text({python_source!r}); python.chmod(0o755)\n"
        "cli = bin/'sanka'\n"
        f"cli.write_text({cli_source!r}.replace('VERSION', version))\n"
        "cli.chmod(0o755)\n"
    )
    stub.chmod(0o755)
    environment = dict(os.environ)
    environment.update(
        SANKA_INSTALL_DIR=str(root),
        SANKA_BIN_DIR=str(bindir),
        PATH=f"{bindir}:/usr/bin:/bin",
        VIRTUAL_ENV="/unsupported/project/venv",
        PYTHONPATH="/unsupported/project/python",
    )
    return root, bindir, environment


def invoke(environment: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(INSTALLER), *args],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("system_python", [None, "3.9.1"])
def test_install_does_not_use_system_python(
    sandbox: tuple[Path, Path, dict[str, str]], system_python: str | None
) -> None:
    root, bindir, environment = sandbox
    if system_python:
        bindir.mkdir()
        python = bindir / "python3"
        python.write_text(f"#!/bin/sh\necho Python {system_python}; exit 99\n")
        python.chmod(0o755)
    result = invoke(environment)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (bindir / "sanka").resolve() == root / "versions/0.2.15/bin/sanka"
    assert "sanka, version 0.2.15" in result.stdout
    assert not (root / "install.lock").exists()


def test_existing_uv_target_is_not_overwritten(sandbox: tuple[Path, Path, dict[str, str]]) -> None:
    root, bindir, environment = sandbox
    bindir.mkdir()
    original = bindir / "sanka"
    original.write_text("user-owned executable")
    result = invoke(environment, "--coexist")
    assert result.returncode != 0
    assert "Nothing was replaced" in result.stderr
    assert original.read_text() == "user-owned executable"
    assert not (root / "versions").exists()


def test_old_homebrew_is_preserved_and_coexist_reports_path(
    sandbox: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    root, bindir, environment = sandbox
    brew = tmp_path / "homebrew/bin"
    brew.mkdir(parents=True)
    original = brew / "sanka"
    original.write_text("#!/bin/sh\necho 'sanka, version 0.2.3'\n")
    original.chmod(0o755)
    environment["PATH"] = f"{brew}:{bindir}:/usr/bin:/bin"
    rejected = invoke(environment)
    assert rejected.returncode != 0
    assert "brew upgrade sankaHQ/cli/sanka" in rejected.stderr
    assert not (root / "versions").exists()
    accepted = invoke(environment, "--coexist")
    assert accepted.returncode == 0, accepted.stderr
    assert f"PATH currently selects: {original}" in accepted.stdout
    assert "export PATH=" in accepted.stdout
    assert "0.2.3" in original.read_text()


def test_upgrade_and_repeat_install_switch_only_the_owned_link(
    sandbox: tuple[Path, Path, dict[str, str]],
) -> None:
    root, bindir, environment = sandbox
    old = root / "versions/0.2.10"
    (old / "bin").mkdir(parents=True)
    (old / "bin/sanka").write_text("old version")
    (root / "current").symlink_to(old)
    bindir.mkdir()
    (bindir / "sanka").symlink_to(root / "current/bin/sanka")
    for _ in range(2):
        result = invoke(environment)
        assert result.returncode == 0, result.stderr + result.stdout
        assert (root / "current").resolve() == root / "versions/0.2.15"
        assert (old / "bin/sanka").read_text() == "old version"
        assert not (old / "current").exists()


def test_failed_download_does_not_switch_existing_version(
    sandbox: tuple[Path, Path, dict[str, str]],
) -> None:
    root, _, environment = sandbox
    old = root / "versions/0.2.10"
    old.mkdir(parents=True)
    (root / "current").symlink_to(old)
    (root / "bootstrap/uv").write_text("#!/bin/sh\nexit 23\n")
    result = invoke(environment)
    assert result.returncode == 23
    assert (root / "current").resolve() == old
    assert not (root / "install.lock").exists()


def test_concurrent_installer_preserves_lock(sandbox: tuple[Path, Path, dict[str, str]]) -> None:
    root, _, environment = sandbox
    lock = root / "install.lock"
    lock.mkdir()
    result = invoke(environment)
    assert result.returncode != 0
    assert "Another installation is active" in result.stderr
    assert lock.exists()


def test_bootstrap_checksum_mismatch_never_executes_download(
    sandbox: tuple[Path, Path, dict[str, str]],
) -> None:
    root, bindir, environment = sandbox
    (root / "bootstrap/uv").unlink()
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(
        '#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\nshift\nprintf "exit 99\\n" > "$1"\n'
    )
    curl.chmod(0o755)
    result = invoke(environment)
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (root / "versions").exists()
    assert not (root / "install.lock").exists()


def test_target_changed_during_install_is_not_overwritten(
    sandbox: tuple[Path, Path, dict[str, str]],
) -> None:
    root, bindir, environment = sandbox
    uv = root / "bootstrap/uv"
    with uv.open("a") as stream:
        stream.write(
            "(pathlib.Path(os.environ['SANKA_BIN_DIR'])/'sanka').write_text('other installer')\n"
        )
    result = invoke(environment)
    assert result.returncode != 0
    assert "changed" in result.stderr
    assert (bindir / "sanka").read_text() == "other installer"
    assert not (root / "current").exists()


@pytest.mark.parametrize("version", ["../x", "1;echo bad", "", "-e", "0.2.15/evil"])
def test_invalid_version_fails_before_mutation(
    sandbox: tuple[Path, Path, dict[str, str]],
    version: str,
) -> None:
    root, _, environment = sandbox
    result = invoke(environment, "--version", version)
    assert result.returncode != 0
    assert not (root / "versions").exists()
