# SPDX-License-Identifier: Apache-2.0
"""Install the built CLI artifact and exercise doctor without source imports."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with zipfile.ZipFile(wheel) as package:
        name = next(name for name in package.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(package.read(name).decode())
        assert metadata["Name"] == "sanka-cli"
        version = metadata["Version"]
    root = Path(tempfile.mkdtemp(prefix="sanka-package-smoke-"))
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    subprocess.run(
        ["uv", "venv", "--python", "3.12", str(root / "venv")],
        env=environment,
        check=True,
        cwd=root,
    )
    python = root / "venv/bin/python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        env=environment,
        check=True,
        cwd=root,
    )
    cli = root / "venv/bin/sanka"
    environment["PATH"] = str(cli.parent) + os.pathsep + environment.get("PATH", "")
    report = json.loads(
        subprocess.check_output(
            [str(cli), "doctor", "--json", "--expected-version", version],
            env=environment,
            cwd=root,
            text=True,
        )
    )
    assert report["cli_version"] == version
    assert report["active_executable"] == str(cli)
    assert report["path_executable"] == str(cli)
    assert report["status"] != "error"
    assert all(check["code"] == "duplicate_installations" for check in report["checks"])
    missing = subprocess.run(
        [str(cli), "doctor", "--json", "--expected-version", "0.0.0"],
        env=environment,
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert missing.returncode == 1
    assert json.loads(missing.stdout)["status"] == "error"
    assert f"sanka, version {version}" in subprocess.check_output(
        [str(cli), "--version"],
        env=environment,
        cwd=root,
        text=True,
    )
    args.report.write_text(
        json.dumps(
            {
                "root": str(root),
                "wheel": str(wheel),
                "doctor": report,
                "mismatch_exit": missing.returncode,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Built package installation and doctor checks passed: {args.report}")


if __name__ == "__main__":
    main()
