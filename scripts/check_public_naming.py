# SPDX-License-Identifier: Apache-2.0
"""Enforce the public Sanka naming contract."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_URL = "https://github.com/sankaHQ/sanka"
RETIRED_TOKEN = "fer" + "ry"
LEGACY_DISPLAY_TOKEN = "Sanka " + "Migrate"
IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}

EXPECTED_PROJECTS = {
    Path("packages/sanka-migrate/pyproject.toml"): "sanka-migrate",
    Path("packages/sanka-migrate-mcp/pyproject.toml"): "sanka-migrate-mcp",
}
HOSTED_CONNECTOR_PACKAGES = {
    "sanka-connector-hubspot",
    "sanka-connector-salesforce",
    "sanka-connector-sendgrid",
}


def _load(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _retired_name_references() -> list[str]:
    references: list[str] = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if RETIRED_TOKEN in str(relative).lower():
            references.append(f"path: {relative}")
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if RETIRED_TOKEN in content.lower():
            references.append(f"content: {relative}")
    return references


def _legacy_display_name_references() -> list[str]:
    references: list[str] = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_PARTS for part in relative.parts) or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if LEGACY_DISPLAY_TOKEN in content:
            references.append(f"content: {relative}")
    return references


def main() -> int:
    errors: list[str] = []
    versions: set[str] = set()

    retired_references = _retired_name_references()
    if retired_references:
        errors.append(
            "retired project name remains in the public source tree: "
            + ", ".join(retired_references)
        )

    legacy_display_references = _legacy_display_name_references()
    if legacy_display_references:
        errors.append(
            "legacy display name remains in the public source tree: "
            + ", ".join(legacy_display_references)
        )

    root_project = _load(ROOT / "pyproject.toml")["project"]
    if root_project["name"] != "sanka-migrate-workspace":
        errors.append("workspace project name must be sanka-migrate-workspace")

    workspace = _load(ROOT / "pyproject.toml")
    development_dependencies = {
        str(value).split("=", 1)[0].lower()
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

    for relative_path, expected_name in EXPECTED_PROJECTS.items():
        document = _load(ROOT / relative_path)
        project = document["project"]
        actual_name = project.get("name")
        if actual_name != expected_name:
            errors.append(
                f"{relative_path}: expected project name {expected_name!r}, got {actual_name!r}"
            )
        if actual_name == "sanka":
            errors.append(f"{relative_path}: bare 'sanka' is a reserved umbrella/colliding name")
        versions.add(str(project.get("version", "")))
        repository = project.get("urls", {}).get("Repository")
        if repository != REPOSITORY_URL:
            errors.append(f"{relative_path}: Repository must be {REPOSITORY_URL}")

    if len(versions) != 1:
        errors.append(
            f"all published distributions must share one version, found {sorted(versions)}"
        )

    runtime = _load(ROOT / "packages/sanka-migrate/pyproject.toml")
    scripts = runtime["project"].get("scripts", {})
    expected_scripts = {
        "sanka-migrate": "sanka.cli:main",
    }
    if scripts != expected_scripts:
        errors.append(f"runtime scripts must be exactly: {expected_scripts}")
    runtime_packages = (
        runtime.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages", [])
    )
    expected_runtime_packages = {"src/sanka"}
    if set(runtime_packages) != expected_runtime_packages:
        errors.append(
            "runtime wheel must ship the public sanka namespace: "
            f"{sorted(expected_runtime_packages)}"
        )

    mcp_package = _load(ROOT / "packages/sanka-migrate-mcp/pyproject.toml")
    mcp_project = mcp_package["project"]
    expected_mcp_scripts = {"sanka-migrate-mcp": "sanka_migrate_mcp.server:main"}
    if mcp_project.get("scripts", {}) != expected_mcp_scripts:
        errors.append(f"MCP scripts must be exactly: {expected_mcp_scripts}")
    mcp_dependencies = [str(value).lower() for value in mcp_project.get("dependencies", [])]
    for required in ("httpx", "mcp", "pydantic", "pydantic-settings"):
        if not any(
            value == required or value.startswith(required + ">") for value in mcp_dependencies
        ):
            errors.append(f"MCP package must depend on {required!r}")
    mcp_packages = (
        mcp_package.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages", [])
    )
    if set(mcp_packages) != {"src/sanka_migrate_mcp"}:
        errors.append("MCP wheel must ship only the standalone sanka_migrate_mcp package")

    connector_entry_points = runtime["project"].get("entry-points", {}).get("sanka.connectors", {})
    if connector_entry_points:
        errors.append("runtime must not bundle provider entry points; use sankaHQ/sanka-connectors")

    package_paths = (
        ROOT / "packages/sanka-migrate/src/sanka/runtime",
        ROOT / "packages/sanka-migrate/src/sanka/cli",
        ROOT / "packages/sanka-migrate/src/sanka/connector",
        ROOT / "packages/sanka-migrate-mcp/src/sanka_migrate_mcp",
    )
    for path in package_paths:
        if not path.is_dir():
            errors.append(f"package namespace path is missing: {path.relative_to(ROOT)}")

    public_facade = ROOT / "packages/sanka-migrate/src/sanka/__init__.py"
    if not public_facade.is_file():
        errors.append("public sanka.Sanka facade is missing from the runtime distribution")

    if errors:
        print("public naming check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    version = versions.pop()
    print(f"public naming OK: {len(EXPECTED_PROJECTS)} distributions at {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
