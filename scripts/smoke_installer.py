# SPDX-License-Identifier: Apache-2.0
"""Real public installer acceptance with no Python or uv commands on child PATH."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.2.10", help="An already published PyPI version")
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
                "output": outputs,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Installer fresh/repeat acceptance passed: {args.report}")


if __name__ == "__main__":
    main()
