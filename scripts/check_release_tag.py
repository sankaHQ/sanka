# SPDX-License-Identifier: Apache-2.0
"""Require a release tag to match the unified package version."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_FILE = ROOT / "packages" / "sanka-cli" / "pyproject.toml"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check_release_tag.py <ref-name> <ref-type>")
        return 2
    if sys.argv[2] != "tag":
        print(f"release ref type must be 'tag', got {sys.argv[2]!r}")
        return 1
    with PACKAGE_FILE.open("rb") as handle:
        version = str(tomllib.load(handle)["project"]["version"])
    expected_tag = f"v{version}"
    if sys.argv[1] != expected_tag:
        print(f"release ref must be {expected_tag!r}, got {sys.argv[1]!r}")
        return 1
    print(f"release tag OK: {expected_tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
