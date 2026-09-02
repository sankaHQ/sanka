# SPDX-License-Identifier: Apache-2.0
"""Fail when the embedded Connector SDK differs from its canonical source."""

from __future__ import annotations

import os
from pathlib import Path


def _python_tree(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed")
    }


def compare_python_trees(canonical: Path, embedded: Path) -> list[str]:
    canonical_files = _python_tree(canonical)
    embedded_files = _python_tree(embedded)
    return [
        path.as_posix()
        for path in sorted(canonical_files.keys() | embedded_files.keys())
        if canonical_files.get(path) != embedded_files.get(path)
    ]


def main() -> int:
    canonical = Path(os.environ["SANKA_CONNECTOR_SDK_SOURCE"]).resolve()
    embedded = Path("packages/sanka-cli/src/sanka_connector").resolve()
    mismatches = compare_python_trees(canonical, embedded)
    if mismatches:
        raise SystemExit("embedded connector SDK drift: " + ", ".join(mismatches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
