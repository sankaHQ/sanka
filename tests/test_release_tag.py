# SPDX-License-Identifier: Apache-2.0
"""Release tags must match the immutable CLI package version."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_release_tag_guard_accepts_the_022_tag() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_release_tag.py", "v0.2.2", "tag"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "release tag OK: v0.2.2\n"
