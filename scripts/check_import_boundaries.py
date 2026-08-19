# SPDX-License-Identifier: Apache-2.0
"""Enforce Sanka Migrate's license/import boundaries.

Rules (see docs/ARCHITECTURE.md):

- ``packages/sanka-migrate-connector-sdk`` (Apache-2.0) must not import any ``sanka.*``
  module outside ``sanka.connector`` — Apache code must never depend on the
  AGPL runtime.
- ``connectors/*`` (Apache-2.0) may import ``sanka.connector`` only.
- ``packages/sanka-migrate`` (AGPL-3.0-only) may import anything.

Usage: ``python scripts/check_import_boundaries.py [repo_root]``
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ALLOWED_PREFIX = "sanka.connector"
RESTRICTED_ZONES = ("packages/sanka-migrate-connector-sdk", "connectors")


def _imported_modules(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def _violates(module: str) -> bool:
    if module == ALLOWED_PREFIX or module.startswith(ALLOWED_PREFIX + "."):
        return False
    return module == "sanka" or module.startswith("sanka.")


def check(root: Path) -> list[str]:
    violations: list[str] = []
    for zone in RESTRICTED_ZONES:
        zone_path = root / zone
        if not zone_path.is_dir():
            continue
        for path in sorted(zone_path.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                if _violates(module):
                    violations.append(
                        f"{path.relative_to(root)}: imports {module!r} "
                        f"(only {ALLOWED_PREFIX!r} is allowed in this zone)"
                    )
    return violations


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    violations = check(root)
    for line in violations:
        print(f"BOUNDARY VIOLATION: {line}")
    if violations:
        return 1
    print("import boundaries OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
