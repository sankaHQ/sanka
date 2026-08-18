# SPDX-License-Identifier: Apache-2.0
"""Enforce public Sanka Migrate names without moving stable Ferry internals."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_URL = "https://github.com/sankaHQ/sanka-migrate"

EXPECTED_PROJECTS = {
    Path("packages/ferry-connector-sdk/pyproject.toml"): "sanka-migrate-connector-sdk",
    Path("packages/ferry-migrate/pyproject.toml"): "sanka-migrate",
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


def main() -> int:
    errors: list[str] = []
    versions: set[str] = set()

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

    runtime = _load(ROOT / "packages/ferry-migrate/pyproject.toml")
    scripts = runtime["project"].get("scripts", {})
    expected_scripts = {"sanka-migrate": "ferry.cli:main", "ferry": "ferry.cli:main"}
    if scripts != expected_scripts:
        errors.append(f"runtime scripts must preserve the compatibility alias: {expected_scripts}")
    if "sanka-migrate-connector-sdk" not in runtime["project"].get("dependencies", []):
        errors.append("runtime must depend on the public SDK distribution name")

    for relative_path in EXPECTED_PROJECTS:
        if not str(relative_path).startswith("connectors/"):
            continue
        document = _load(ROOT / relative_path)
        entry_points = document["project"].get("entry-points", {})
        if "ferry.connectors" not in entry_points:
            errors.append(f"{relative_path}: stable ferry.connectors entry-point group moved")

    stable_paths = (
        ROOT / "packages/ferry-migrate/src/ferry/runtime",
        ROOT / "packages/ferry-migrate/src/ferry/cli",
        ROOT / "packages/ferry-connector-sdk/src/ferry/connector",
    )
    for path in stable_paths:
        if not path.is_dir():
            errors.append(f"stable internal namespace path is missing: {path.relative_to(ROOT)}")

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
