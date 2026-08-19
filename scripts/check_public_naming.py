# SPDX-License-Identifier: Apache-2.0
"""Enforce the public Sanka Migrate naming contract."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_URL = "https://github.com/sankaHQ/sanka-migrate"
RETIRED_TOKEN = "fer" + "ry"
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
    Path("packages/sanka-migrate-connector-sdk/pyproject.toml"): "sanka-migrate-connector-sdk",
    Path("packages/sanka-migrate/pyproject.toml"): "sanka-migrate",
    Path("connectors/clickhouse/pyproject.toml"): "sanka-migrate-connector-clickhouse",
    Path("connectors/csv/pyproject.toml"): "sanka-migrate-connector-csv",
    Path("connectors/hubspot/pyproject.toml"): "sanka-migrate-connector-hubspot",
    Path("connectors/markdown/pyproject.toml"): "sanka-migrate-connector-markdown",
    Path("connectors/postgres/pyproject.toml"): "sanka-migrate-connector-postgres",
    Path("connectors/salesforce/pyproject.toml"): "sanka-migrate-connector-salesforce",
    Path("connectors/sqlite/pyproject.toml"): "sanka-migrate-connector-sqlite",
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


def main() -> int:
    errors: list[str] = []
    versions: set[str] = set()

    retired_references = _retired_name_references()
    if retired_references:
        errors.append(
            "retired project name remains in the public source tree: "
            + ", ".join(retired_references)
        )

    root_project = _load(ROOT / "pyproject.toml")["project"]
    if root_project["name"] != "sanka-migrate-workspace":
        errors.append("workspace project name must be sanka-migrate-workspace")

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
    expected_scripts = {"sanka-migrate": "sanka.cli:main"}
    if scripts != expected_scripts:
        errors.append(f"runtime scripts must be exactly: {expected_scripts}")
    if "sanka-migrate-connector-sdk" not in runtime["project"].get("dependencies", []):
        errors.append("runtime must depend on the public SDK distribution name")
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

    for relative_path in EXPECTED_PROJECTS:
        if not str(relative_path).startswith("connectors/"):
            continue
        document = _load(ROOT / relative_path)
        entry_points = document["project"].get("entry-points", {})
        if "sanka.connectors" not in entry_points:
            errors.append(f"{relative_path}: canonical sanka.connectors entry-point group moved")

    package_paths = (
        ROOT / "packages/sanka-migrate/src/sanka/runtime",
        ROOT / "packages/sanka-migrate/src/sanka/cli",
        ROOT / "packages/sanka-migrate-connector-sdk/src/sanka/connector",
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
