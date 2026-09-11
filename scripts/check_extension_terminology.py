# SPDX-License-Identifier: Apache-2.0
"""Enforce canonical types while retaining documented compatibility imports."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_TYPES = {
    "ConnectorRegistry",
    "ConnectorRegistration",
    "SourceConnector",
    "DestinationConnector",
    "ConnectorHostClient",
    "UnknownConnectorError",
    "ConnectorError",
    "ProviderIdentity",
    "ProviderTimeoutError",
    "TransientProviderError",
    "Connection",
}


def check(root: Path) -> list[str]:
    errors = []
    source_root = root / "packages/sanka-cli/src"
    for source in sorted(source_root.rglob("*.py")):
        relative = source.relative_to(source_root)
        if relative.parts[0] in {"sanka_connector", "sanka_data"}:
            continue  # One shared SDK compatibility implementation and canonical facade.
        if relative.parts[:2] == ("sanka", "connector"):
            continue  # Published historical SDK alias modules.
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in LEGACY_TYPES:
                errors.append(f"{relative}:{node.lineno}: legacy type definition {node.name}")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and (node.module == "sanka_connector" or node.module.startswith("sanka_connector."))
            ):
                errors.append(f"{relative}:{node.lineno}: use the sanka_data SDK interface")
    return errors


def main() -> int:
    errors = check(ROOT)
    if errors:
        raise SystemExit("\n".join(errors))
    print("Extension/system terminology: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
