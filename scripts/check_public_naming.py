# SPDX-License-Identifier: Apache-2.0
"""Enforce the public Sanka naming contract."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_URL = "https://github.com/sankaHQ/sanka"
RETIRED_TOKEN = "fer" + "ry"
LEGACY_DISPLAY_TOKEN = "Sanka " + "Migrate"
PACKAGE_FILE = Path("packages/sanka-cli/pyproject.toml")
HOSTED_CONNECTOR_PACKAGES = {
    "sanka-connector-hubspot",
    "sanka-connector-salesforce",
    "sanka-connector-sendgrid",
}
IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "release",
}


def _load(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _tree_references(token: str) -> list[str]:
    references: list[str] = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if token.lower() in str(relative).lower():
            references.append(f"path: {relative}")
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if token in content:
            references.append(f"content: {relative}")
    return references


def _requirement_name(value: str) -> str:
    return re.split(r"[<>=!~;\s\[]", value, maxsplit=1)[0].lower().replace("_", "-")


def main() -> int:
    errors: list[str] = []

    retired_references = _tree_references(RETIRED_TOKEN)
    if retired_references:
        errors.append(
            "retired project name remains in the public source tree: "
            + ", ".join(retired_references)
        )
    legacy_display_references = _tree_references(LEGACY_DISPLAY_TOKEN)
    if legacy_display_references:
        errors.append(
            "legacy display name remains in the public source tree: "
            + ", ".join(legacy_display_references)
        )

    workspace = _load(ROOT / "pyproject.toml")
    if workspace["project"]["name"] != "sanka-cli-workspace":
        errors.append("workspace project name must be sanka-cli-workspace")
    development_dependencies = {
        _requirement_name(str(value))
        for value in workspace.get("dependency-groups", {}).get("dev", [])
    }
    unexpected_hosted_packages = sorted(
        development_dependencies.intersection(HOSTED_CONNECTOR_PACKAGES)
    )
    if unexpected_hosted_packages:
        errors.append(
            "hosted SaaS providers must not be local development dependencies: "
            f"{unexpected_hosted_packages}"
        )

    project_document = _load(ROOT / PACKAGE_FILE)
    project = project_document["project"]
    if project.get("name") != "sanka-cli":
        errors.append(f"{PACKAGE_FILE}: project name must be 'sanka-cli'")
    if project.get("urls", {}).get("Repository") != REPOSITORY_URL:
        errors.append(f"{PACKAGE_FILE}: Repository must be {REPOSITORY_URL}")
    expected_scripts = {"sanka": "sanka_cli.main:main"}
    if project.get("scripts", {}) != expected_scripts:
        errors.append(f"console scripts must be exactly: {expected_scripts}")

    mcp_dependencies = {
        _requirement_name(str(value))
        for value in project.get("optional-dependencies", {}).get("mcp", [])
    }
    expected_mcp_dependencies = {"mcp", "pydantic", "pydantic-settings"}
    if mcp_dependencies != expected_mcp_dependencies:
        errors.append(f"the mcp extra must contain exactly: {sorted(expected_mcp_dependencies)}")

    wheel_packages = (
        project_document.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages", [])
    )
    expected_wheel_packages = {"src/sanka", "src/sanka_cli", "src/sanka_connector"}
    if set(wheel_packages) != expected_wheel_packages:
        errors.append(f"wheel packages must be exactly: {sorted(expected_wheel_packages)}")
    if project.get("entry-points", {}).get("sanka.connectors", {}):
        errors.append("sanka-cli must not bundle provider entry points; use sankaHQ/extensions")

    for relative in (
        "packages/sanka-cli/src/sanka",
        "packages/sanka-cli/src/sanka_cli",
        "packages/sanka-cli/src/sanka_cli/mcp",
        "packages/sanka-cli/src/sanka_connector",
    ):
        if not (ROOT / relative).is_dir():
            errors.append(f"package namespace path is missing: {relative}")
    for legacy in ("packages/sanka-migrate", "packages/sanka-migrate-mcp"):
        if (ROOT / legacy).exists():
            errors.append(f"retired package path remains: {legacy}")

    if errors:
        print("public naming check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"public naming OK: sanka-cli {project['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
