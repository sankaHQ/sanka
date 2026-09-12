# SPDX-License-Identifier: Apache-2.0
"""Enforce source-license and migration-extension import boundaries.

The embedded Apache Connector SDK and MCP integration cannot import the AGPL
runtime. Target-framework implementations and generated destination
dependencies belong in GitHub marketplace extensions, not the local runtime.

Usage: ``python scripts/check_import_boundaries.py [repo_root]``
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

RESTRICTED_ZONES = {
    "packages/sanka-cli/src/sanka_extension_sdk": "Extension SDK cannot import the AGPL runtime",
    "packages/sanka-cli/src/sanka_extensions": "Sanka Extension SDK cannot import the AGPL runtime",
    "packages/sanka-cli/src/sanka_cli/mcp": ("MCP integration cannot import the AGPL runtime"),
    "packages/sanka-cli/src/sanka_connector": ("Connector SDK cannot import the AGPL runtime"),
}
TARGET_SPECIFIC_MODULES = frozenset(
    {
        "aiosqlite",
        "asyncpg",
        "django",
        "fastapi",
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


def _imports_runtime(module: str) -> bool:
    return module == "sanka" or module.startswith("sanka.")


def _target_specific(module: str) -> bool:
    return any(module == name or module.startswith(name + ".") for name in TARGET_SPECIFIC_MODULES)


def check(root: Path) -> list[str]:
    violations: list[str] = []
    for zone, explanation in RESTRICTED_ZONES.items():
        zone_path = root / zone
        if not zone_path.is_dir():
            continue
        for path in sorted(zone_path.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                if _imports_runtime(module):
                    violations.append(
                        f"{path.relative_to(root)}: imports {module!r} ({explanation})"
                    )

    runtime = root / "packages" / "sanka-cli" / "src" / "sanka" / "runtime"
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
