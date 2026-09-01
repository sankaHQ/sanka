# SPDX-License-Identifier: Apache-2.0
"""Enforce Sanka's license/import boundaries.

Rules (see docs/ARCHITECTURE.md):

- the Apache-2.0 Connector SDK and providers live in the separate
  ``sankaHQ/extensions`` repository, whose CI owns their boundary checks.
- ``packages/sanka-migrate-mcp`` (Apache-2.0) must not import ``sanka`` at all;
  it is a standalone REST shim outside the runtime namespace.
- ``packages/sanka-migrate`` must not import target framework implementations or
  generated destination dependencies; those belong in extensions.

Usage: ``python scripts/check_import_boundaries.py [repo_root]``
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

RESTRICTED_ZONES: tuple[tuple[str, str | None], ...] = (("packages/sanka-migrate-mcp", None),)
TARGET_SPECIFIC_MODULES = frozenset(
    {
        "aiosqlite",
        "asyncpg",
        "django",
        "fastapi",
        "httpx",
        "psycopg",
        "rest_framework",
        "sanka.runtime.frameworks",
        "sqlalchemy",
        "tortoise",
        "uvicorn",
    }
)


def _imported_modules(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def _violates(module: str, allowed_prefix: str | None) -> bool:
    if allowed_prefix and (module == allowed_prefix or module.startswith(allowed_prefix + ".")):
        return False
    return module == "sanka" or module.startswith("sanka.")


def _target_specific(module: str) -> bool:
    return any(module == name or module.startswith(name + ".") for name in TARGET_SPECIFIC_MODULES)


def check(root: Path) -> list[str]:
    violations: list[str] = []
    for zone, allowed_prefix in RESTRICTED_ZONES:
        zone_path = root / zone
        if not zone_path.is_dir():
            continue
        for path in sorted(zone_path.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                if _violates(module, allowed_prefix):
                    allowed = (
                        f"only {allowed_prefix!r} is allowed"
                        if allowed_prefix
                        else "the standalone MCP package cannot import the sanka namespace"
                    )
                    violations.append(f"{path.relative_to(root)}: imports {module!r} ({allowed})")
    runtime = root / "packages" / "sanka-migrate" / "src" / "sanka"
    if runtime.is_dir():
        for path in sorted(runtime.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                if _target_specific(module):
                    violations.append(
                        f"{path.relative_to(root)}: imports {module!r} "
                        "(target-specific migration logic belongs in an extension)"
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
