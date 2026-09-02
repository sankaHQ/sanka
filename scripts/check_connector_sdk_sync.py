# SPDX-License-Identifier: Apache-2.0
"""Fail when the embedded Connector SDK drifts from its canonical snapshot."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

EMBEDDED_SHA256 = "728ea85636d191d3bd2a13bfeee48cc8e60d89ea6c5e39e887ef6a40f55e19c9"
EXTENSIONS_REVISION = "0852273fbddd614c03486b3d834f69b10331ecab"


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


def python_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path, contents in sorted(_python_tree(root).items()):
        digest.update(path.as_posix().encode())
        digest.update(b"\0")
        digest.update(contents)
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    embedded = Path("packages/sanka-cli/src/sanka_connector").resolve()
    source = os.environ.get("SANKA_CONNECTOR_SDK_SOURCE")
    if source:
        mismatches = compare_python_trees(Path(source).resolve(), embedded)
        if mismatches:
            raise SystemExit("embedded connector SDK drift: " + ", ".join(mismatches))
    elif python_tree_sha256(embedded) != EMBEDDED_SHA256:
        raise SystemExit(
            "embedded connector SDK snapshot drift; sync from extensions revision "
            + EXTENSIONS_REVISION
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
