# SPDX-License-Identifier: Apache-2.0
"""Real public installer acceptance with no Python or uv commands on child PATH."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def check_shell_selection(cli: Path, old_cli: Path, version: str) -> dict[str, Any]:
    """Reproduce profile shadowing, then prove explicit PATH/cache recovery."""
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="sanka-shell-") as directory:
        home = Path(directory)
        current_bin = home / "uv bin"
        old_bin = home / "homebrew bin"
        current_bin.mkdir()
        old_bin.mkdir()
        (current_bin / "sanka").symlink_to(cli.resolve())
        (old_bin / "sanka").symlink_to(old_cli.resolve())
        # Match the reported failure: .zshenv prefers uv; .zshrc later prepends brew.
        first = f'export PATH={shlex.quote(str(current_bin))}:"$PATH"\n'
        shadow = f'export PATH={shlex.quote(str(old_bin))}:"$PATH"\n'
        (home / ".zshenv").write_text(first)
        (home / ".zshrc").write_text(shadow)
        (home / ".bashrc").write_text(first + shadow)
        environment = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "BASH_ENV", "ENV"):
            environment.pop(name, None)
        environment.update(HOME=str(home), ZDOTDIR=str(home), PATH="/usr/bin:/bin")
        for shell_name in ("zsh", "bash"):
            shell = shutil.which(shell_name)
            if not shell:
                raise RuntimeError(f"{shell_name} is required for interactive shell acceptance")
            prefix = (
                [shell, "-ic"]
                if shell_name == "zsh"
                else [shell, "--noprofile", "--rcfile", str(home / ".bashrc"), "-ic"]
            )

            def run(command: str, prefix: list[str] = prefix) -> str:
                result = subprocess.run(
                    [*prefix, command],
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                if result.returncode:
                    raise RuntimeError(result.stdout + result.stderr)
                return result.stdout

            selected = run("command -v sanka; sanka --version").splitlines()
            assert selected[0] == str(old_bin / "sanka"), selected
            assert selected[1] != f"sanka, version {version}", selected
            recovery = first + ("rehash\n" if shell_name == "zsh" else "hash -r\n")
            # Prime the command cache with the old executable before repairing PATH.
            details = json.loads(
                run(
                    "sanka --version >/dev/null\n"
                    + recovery
                    + f"sanka doctor --expected-version {shlex.quote(version)} --json"
                )
            )
            assert details["cli_version"] == version
            assert Path(details["path_executable"]).resolve() == cli.resolve()
            assert details["status"] == "warning"
            assert {check["code"] for check in details["checks"]} == {"duplicate_installations"}
            # Persist the same priority in the isolated profile, then start a new shell.
            profile = home / (".zshrc" if shell_name == "zsh" else ".bashrc")
            profile.write_text(shadow + first)
            fresh = run("command -v sanka; sanka --version").splitlines()
            assert fresh == [str(current_bin / "sanka"), f"sanka, version {version}"], fresh
            results[shell_name] = {
                "old_version": selected[1],
                "shadowing_reproduced": True,
                "path_and_cache_recovery": "passed",
                "fresh_shell": "passed",
                "duplicate_warning": "passed",
            }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.2.12", help="An already published PyPI version")
    parser.add_argument("--upgrade-from", help="Also prove an upgrade from this published version")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="sanka-install-smoke-"))
    commands = root / "system-bin"
    commands.mkdir()
    # uv's bootstrap needs ordinary POSIX utilities, but never a system Python.
    for name in (
        "sh",
        "bash",
        "uname",
        "curl",
        "cat",
        "mkdir",
        "chmod",
        "cp",
        "mv",
        "rm",
        "rmdir",
        "readlink",
        "ln",
        "mktemp",
        "tar",
        "gzip",
        "sed",
        "grep",
        "awk",
        "cut",
        "tr",
        "head",
        "tail",
        "sort",
        "uniq",
        "wc",
        "basename",
        "dirname",
        "pwd",
        "expr",
        "env",
        "find",
        "touch",
        "which",
        "getconf",
        "id",
        "install",
        "sha256sum",
        "shasum",
        "file",
        "ldd",
        "ps",
        "sysctl",
        "true",
        "false",
    ):
        source = shutil.which(name)
        if source:
            (commands / name).symlink_to(source)
    runtime = root / "runtime with spaces"
    bindir = root / "bin with spaces"
    environment = dict(os.environ)
    environment.update(
        SANKA_INSTALL_DIR=str(runtime), SANKA_BIN_DIR=str(bindir), PATH=f"{bindir}:{commands}"
    )
    installer = Path(__file__).resolve().parents[1] / "install.sh"
    outputs = []
    versions = ([args.upgrade_from] if args.upgrade_from else []) + [args.version, args.version]
    for run, selected_version in enumerate(versions):
        # The second install also sees a misleading old system Python command.
        if run == 1:
            old_python = commands / "python3"
            old_python.write_text("#!/bin/sh\necho Python 3.9.1 >&2\nexit 99\n")
            old_python.chmod(0o755)
        result = subprocess.run(
            ["/bin/sh", str(installer), "--version", selected_version],
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        outputs.append(result.stdout)
    if args.upgrade_from:
        previous = runtime / "versions" / args.upgrade_from / "bin/sanka"
        assert (
            subprocess.check_output(
                [str(previous), "--version"], env=environment, text=True
            ).strip()
            == f"sanka, version {args.upgrade_from}"
        )
    version = subprocess.check_output(
        [str(bindir / "sanka"), "--version"], env=environment, text=True
    )
    assert version.strip() == f"sanka, version {args.version}"
    python = runtime / "current/tools/sanka-cli/bin/python"
    details = json.loads(
        subprocess.check_output(
            [
                str(python),
                "-c",
                "import json,sys; from importlib.metadata import version; "
                "print(json.dumps({'python':sys.version.split()[0], 'cli':version('sanka-cli'), "
                "'python_executable':sys.executable}))",
            ],
            env=environment,
            text=True,
        )
    )
    shell_checks = (
        check_shell_selection(bindir / "sanka", previous, args.version) if args.upgrade_from else {}
    )
    args.report.write_text(
        json.dumps(
            {
                "root": str(root),
                "version": args.version,
                "fresh_no_python_no_uv": "passed",
                "repeat_with_python39": "passed",
                "upgrade_from": args.upgrade_from,
                "previous_version_preserved": bool(args.upgrade_from),
                "runtime": details,
                "interactive_shells": shell_checks,
                "output": outputs,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Installer fresh/repeat acceptance passed: {args.report}")


if __name__ == "__main__":
    main()
