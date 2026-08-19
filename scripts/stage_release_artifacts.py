# SPDX-License-Identifier: Apache-2.0
"""Stage checked release artifacts for all-project and one-project publishing jobs."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts.check_release_artifacts import EXPECTED_LICENSES, _wheel_prefix


def _project_artifacts(source: Path, project_name: str) -> tuple[Path, Path]:
    prefix = _wheel_prefix(project_name)
    wheels = sorted(source.glob(f"{prefix}-*.whl"))
    sdists = sorted(source.glob(f"{prefix}-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            f"{project_name}: expected one wheel and one sdist, "
            f"found wheels={[path.name for path in wheels]} "
            f"sdists={[path.name for path in sdists]}"
        )
    return wheels[0], sdists[0]


def _source_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_release_artifacts(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise ValueError(f"missing release artifact directory: {source}")
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError(
            "release staging source and destination must be separate sibling directories"
        )

    project_files = {
        project_name: _project_artifacts(source, project_name) for project_name in EXPECTED_LICENSES
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=destination.parent,
        prefix=f".{destination.name}-",
    ) as temporary:
        staged = Path(temporary) / destination.name
        all_projects = staged / "all"
        all_projects.mkdir(parents=True)

        for project_name, paths in project_files.items():
            project_directory = staged / "packages" / project_name
            project_directory.mkdir(parents=True)
            for path in paths:
                shutil.copy2(path, all_projects / path.name)
                shutil.copy2(path, project_directory / path.name)

        expected_artifacts = len(EXPECTED_LICENSES) * 2
        if len(list(all_projects.iterdir())) != expected_artifacts:
            raise ValueError(
                f"staged all-project release must contain {expected_artifacts} artifacts"
            )

        checksum_lines = [
            f"{_sha256(path)}  all/{path.name}"
            for path in sorted(all_projects.iterdir(), key=lambda item: item.name)
        ]
        (staged / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n")
        (staged / "SOURCE_COMMIT").write_text(_source_commit() + "\n")

        if destination.exists():
            shutil.rmtree(destination)
        staged.replace(destination)


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist")
    destination = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("release")
    try:
        stage_release_artifacts(source, destination)
    except ValueError as error:
        print(f"release staging failed: {error}")
        return 1
    print(
        f"release staging OK: {len(EXPECTED_LICENSES)} projects, "
        f"{len(EXPECTED_LICENSES) * 2} artifacts"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
