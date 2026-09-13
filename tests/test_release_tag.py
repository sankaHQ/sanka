# SPDX-License-Identifier: Apache-2.0
"""Release tags must match the immutable CLI package version."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_release_tag_guard_accepts_current_release_tag() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_release_tag.py", "v0.2.11", "tag"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "release tag OK: v0.2.11\n"


def test_installer_default_matches_release_package() -> None:
    project = tomllib.loads((ROOT / "packages/sanka-cli/pyproject.toml").read_text())["project"]
    selected = re.search(r"^    version=([0-9.]+)$", (ROOT / "install.sh").read_text(), re.M)
    assert selected is not None
    assert selected.group(1) == project["version"]
