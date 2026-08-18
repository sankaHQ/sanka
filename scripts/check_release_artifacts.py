# SPDX-License-Identifier: Apache-2.0
"""Validate the complete wheel/sdist set before any package publication."""

from __future__ import annotations

import email.policy
import sys
import tarfile
import zipfile
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

EXPECTED_LICENSES = {
    "sanka-migrate": "AGPL-3.0-only",
    "sanka-migrate-connector-sdk": "Apache-2.0",
    "sanka-migrate-connector-clickhouse": "Apache-2.0",
    "sanka-migrate-connector-csv": "Apache-2.0",
    "sanka-migrate-connector-hubspot": "Apache-2.0",
    "sanka-migrate-connector-markdown": "Apache-2.0",
    "sanka-migrate-connector-postgres": "Apache-2.0",
    "sanka-migrate-connector-salesforce": "Apache-2.0",
    "sanka-migrate-connector-sqlite": "Apache-2.0",
}
REPOSITORY_URL = "https://github.com/sankaHQ/sanka-migrate"


def _wheel_prefix(name: str) -> str:
    return name.replace("-", "_")


def _metadata_from_wheel(path: Path) -> tuple[EmailMessage, str]:
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
        if not any(name.endswith("/LICENSE") for name in license_files):
            raise ValueError(f"{path.name}: wheel does not contain a LICENSE file")
        return metadata, entry_points


def _sdist_has_license(path: Path) -> bool:
    with tarfile.open(path, mode="r:gz") as archive:
        return any(member.name.endswith("/LICENSE") for member in archive.getmembers())


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
            metadata, entry_points = _metadata_from_wheel(wheels[0])
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
        if any(str(requirement).lower().startswith("ferry-") for requirement in requirements):
            errors.append(f"{project_name}: artifact still depends on a legacy Ferry distribution")
        if not _sdist_has_license(sdists[0]):
            errors.append(f"{sdists[0].name}: sdist does not contain a LICENSE file")

        if project_name == "sanka-migrate":
            if "sanka-migrate = ferry.cli:main" not in entry_points:
                errors.append("sanka-migrate: primary CLI entry point is missing")
            if "ferry = ferry.cli:main" not in entry_points:
                errors.append("sanka-migrate: Ferry compatibility CLI alias is missing")
            if not any(
                str(requirement).lower().startswith("sanka-migrate-connector-sdk")
                for requirement in requirements
            ):
                errors.append("sanka-migrate: public SDK distribution dependency is missing")
        elif project_name != "sanka-migrate-connector-sdk":
            if "[ferry.connectors]" not in entry_points:
                errors.append(f"{project_name}: stable ferry.connectors entry-point group moved")

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
