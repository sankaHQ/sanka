# SPDX-License-Identifier: Apache-2.0
"""Validate the complete wheel/sdist set before any package publication."""

from __future__ import annotations

import configparser
import email.policy
import re
import sys
import tarfile
import zipfile
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

EXPECTED_LICENSES = {
    "sanka-cli": "Apache-2.0 AND AGPL-3.0-only",
}
REPOSITORY_URL = "https://github.com/sankaHQ/sanka"
FORBIDDEN_RUNTIME_DEPENDENCIES = frozenset(
    {
        "mcp",
        "aiosqlite",
        "asyncpg",
        "django",
        "djangorestframework",
        "fastapi",
        "psycopg",
        "sqlalchemy",
        "tortoise-orm",
        "uvicorn",
    }
)


def _wheel_prefix(name: str) -> str:
    return name.replace("-", "_")


def _requirement_name(value: str) -> str:
    return re.split(r"[<>=!~;\s\[]", value, maxsplit=1)[0].lower().replace("_", "-")


def _metadata_from_wheel(path: Path) -> tuple[EmailMessage, str, set[str]]:
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError(f"{path.name}: expected one METADATA file, found {metadata_names}")
        metadata = BytesParser(policy=email.policy.default).parsebytes(
            archive.read(metadata_names[0])
        )
        entry_point_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
        ]
        entry_points = archive.read(entry_point_names[0]).decode() if entry_point_names else ""
        license_files = [name for name in archive.namelist() if ".dist-info/licenses/" in name]
        required_licenses = {
            "LICENSES/Apache-2.0.txt",
            "LICENSES/AGPL-3.0-only.txt",
            "NOTICE",
        }
        missing_licenses = sorted(
            name
            for name in required_licenses
            if not any(path.endswith("/" + name) for path in license_files)
        )
        if missing_licenses:
            raise ValueError(f"{path.name}: wheel is missing license files: {missing_licenses}")
        return metadata, entry_points, set(archive.namelist())


def _sdist_has_license_files(path: Path) -> bool:
    with tarfile.open(path, mode="r:gz") as archive:
        names = {member.name for member in archive.getmembers()}
    return all(
        any(name.endswith("/" + required) for name in names)
        for required in (
            "LICENSES/Apache-2.0.txt",
            "LICENSES/AGPL-3.0-only.txt",
            "NOTICE",
        )
    )


def _runtime_boundary_errors(requirement_names: set[str], wheel_members: set[str]) -> list[str]:
    errors = [
        f"sanka-cli: target dependency {dependency} leaked into core"
        for dependency in sorted(requirement_names & FORBIDDEN_RUNTIME_DEPENDENCIES)
    ]
    if any(name.startswith("sanka/runtime/frameworks/") for name in wheel_members):
        errors.append("sanka-cli: wheel must not ship sanka/runtime/frameworks/")
    if any(name.startswith("sanka_cli/mcp/") for name in wheel_members):
        errors.append("sanka-cli: wheel must not ship the retired local MCP server")
    return errors


def main() -> int:
    dist = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist")
    errors: list[str] = []
    versions: set[str] = set()

    if not dist.is_dir():
        print(f"release artifact check failed: missing directory {dist}")
        return 1

    for project_name, expected_license in EXPECTED_LICENSES.items():
        prefix = _wheel_prefix(project_name)
        wheels = sorted(dist.glob(f"{prefix}-*.whl"))
        sdists = sorted(dist.glob(f"{prefix}-*.tar.gz"))
        if len(wheels) != 1:
            errors.append(
                f"{project_name}: expected one wheel, found {[path.name for path in wheels]}"
            )
            continue
        if len(sdists) != 1:
            errors.append(
                f"{project_name}: expected one sdist, found {[path.name for path in sdists]}"
            )
            continue

        try:
            metadata, entry_points, wheel_members = _metadata_from_wheel(wheels[0])
        except ValueError as exc:
            errors.append(str(exc))
            continue

        actual_name = str(metadata["Name"])
        version = str(metadata["Version"])
        versions.add(version)
        if actual_name != project_name:
            errors.append(f"{wheels[0].name}: metadata name is {actual_name!r}")
        if metadata["License-Expression"] != expected_license:
            actual_license = metadata["License-Expression"]
            errors.append(f"{project_name}: expected {expected_license}, got {actual_license!r}")
        project_urls = metadata.get_all("Project-URL", []) or []
        if f"Repository, {REPOSITORY_URL}" not in project_urls:
            errors.append(f"{project_name}: missing canonical Repository project URL")
        requirements = metadata.get_all("Requires-Dist", []) or []
        requirement_names = {_requirement_name(str(requirement)) for requirement in requirements}
        core_requirement_names = {
            _requirement_name(str(requirement))
            for requirement in requirements
            if ";" not in str(requirement)
        }
        if not _sdist_has_license_files(sdists[0]):
            errors.append(f"{sdists[0].name}: sdist is missing required license files")

        if project_name == "sanka-cli":
            expected_core_dependencies = {
                "click",
                "cryptography",
                "httpx",
                "keyring",
                "packaging",
                "platformdirs",
                "pyyaml",
                "rich",
            }
            if core_requirement_names != expected_core_dependencies:
                errors.append(
                    "sanka-cli: core dependencies must be exactly "
                    f"{sorted(expected_core_dependencies)}, found {sorted(core_requirement_names)}"
                )
            if "sanka-connector-sdk" in requirement_names:
                errors.append("sanka-cli: must embed the connector SDK instead of depending on it")
            errors.extend(_runtime_boundary_errors(requirement_names, wheel_members))
            parser = configparser.ConfigParser()
            parser.read_string(entry_points)
            console_scripts = (
                dict(parser["console_scripts"]) if parser.has_section("console_scripts") else {}
            )
            if console_scripts != {"sanka": "sanka_cli.main:main"}:
                errors.append(
                    "sanka-cli: console scripts must be exactly "
                    f"{{'sanka': 'sanka_cli.main:main'}}, found {console_scripts}"
                )
            if "[sanka.connectors]" in entry_points:
                errors.append("sanka-cli: runtime wheel must not bundle connector entry points")
            required_imports = {
                "sanka/__init__.py",
                "sanka/_client.py",
                "sanka/connector/__init__.py",
                "sanka/runtime/__init__.py",
                "sanka/runtime/extensions/stage.py",
                "sanka/runtime/extensions/code_lifecycle.py",
                "sanka/runtime/extensions/code_repair.py",
                "sanka/runtime/extensions/artifacts.py",
                "sanka_cli/__init__.py",
                "sanka_cli/commands/skill.py",
                "sanka_cli/main.py",
                "sanka_cli/skills/sanka-cli/SKILL.md",
                "sanka_extensions/__init__.py",
                "sanka_extensions/py.typed",
                "sanka_extensions/systems/__init__.py",
                "sanka_extensions/data/__init__.py",
                "sanka_extensions/data/protocols.py",
                "sanka_extensions/code/__init__.py",
                "sanka_extensions/flow/__init__.py",
                "sanka_extensions/flow/definition.py",
                "sanka_extension_sdk/contract.py",
                "sanka_connector/__init__.py",
                "sanka_connector/py.typed",
            }
            missing_imports = sorted(required_imports - wheel_members)
            if missing_imports:
                errors.append(f"sanka-cli: wheel is missing public imports: {missing_imports}")
            if any(name.startswith("sanka_connector_") for name in wheel_members):
                errors.append("sanka-cli: wheel must not ship provider packages")
    expected_files = len(EXPECTED_LICENSES) * 2
    release_files = list(dist.glob("*.whl")) + list(dist.glob("*.tar.gz"))
    if len(release_files) != expected_files:
        errors.append(
            f"release set must contain exactly {expected_files} wheel/sdist files, found "
            f"{len(release_files)}"
        )
    if len(versions) != 1:
        errors.append(f"release artifacts do not share one version: {sorted(versions)}")

    if errors:
        print("release artifact check failed:")
        for issue in errors:
            print(f"- {issue}")
        return 1

    version = versions.pop()
    print(f"release artifacts OK: {len(EXPECTED_LICENSES)} projects at {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
