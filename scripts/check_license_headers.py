# SPDX-License-Identifier: Apache-2.0
"""Verify every Python file carries the SPDX header its license zone requires.

Every ``*.py`` file must live in a declared license zone and start with the
matching ``SPDX-License-Identifier`` header within its first five lines.
A file outside every zone is an error: extend ``LICENSE_ZONES`` deliberately
rather than leaving a file's license implicit.

Usage: ``python scripts/check_license_headers.py [repo_root]``
"""

from __future__ import annotations

import sys
from pathlib import Path

LICENSE_ZONES: list[tuple[str, str]] = [
    ("packages/ferry-connector-sdk", "Apache-2.0"),
    ("packages/ferry-migrate", "AGPL-3.0-only"),
    ("connectors", "Apache-2.0"),
    ("scripts", "Apache-2.0"),
    ("tests", "Apache-2.0"),
    ("docs", "Apache-2.0"),
]

SKIP_PARTS = {".git", ".venv", "__pycache__", "build", "dist", ".mypy_cache", ".ruff_cache"}


def expected_license(rel: Path) -> str | None:
    posix = rel.as_posix()
    for prefix, spdx in LICENSE_ZONES:
        if posix == prefix or posix.startswith(prefix + "/"):
            return spdx
    return None


def check(root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if SKIP_PARTS.intersection(rel.parts):
            continue
        spdx = expected_license(rel)
        if spdx is None:
            problems.append(f"{rel}: not covered by any license zone — add it to LICENSE_ZONES")
            continue
        head = "".join(path.read_text(encoding="utf-8").splitlines(keepends=True)[:5])
        marker = f"SPDX-License-Identifier: {spdx}"
        if marker not in head:
            problems.append(f"{rel}: missing or wrong header (expected '{marker}')")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    problems = check(root)
    for line in problems:
        print(f"LICENSE HEADER: {line}")
    if problems:
        return 1
    print("license headers OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
