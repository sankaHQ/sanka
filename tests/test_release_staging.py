# SPDX-License-Identifier: Apache-2.0
"""Release staging keeps privileged publishing jobs free of build-time code."""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _expected_projects() -> tuple[str, ...]:
    module = ast.parse((ROOT / "scripts" / "check_release_artifacts.py").read_text())
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "EXPECTED_LICENSES"
            for target in statement.targets
        ):
            licenses = ast.literal_eval(statement.value)
            return tuple(licenses)
    raise AssertionError("EXPECTED_LICENSES is missing")


EXPECTED_PROJECTS = _expected_projects()


def _artifact_names(project_name: str) -> tuple[str, str]:
    prefix = project_name.replace("-", "_")
    return (
        f"{prefix}-0.1.0.dev0-py3-none-any.whl",
        f"{prefix}-0.1.0.dev0.tar.gz",
    )


def test_stage_release_artifacts_builds_all_and_per_project_sets(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    destination = tmp_path / "release"
    source.mkdir()
    for project_name in EXPECTED_PROJECTS:
        for filename in _artifact_names(project_name):
            (source / filename).write_bytes(filename.encode())

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.stage_release_artifacts",
            str(source),
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(list((destination / "all").iterdir())) == len(EXPECTED_PROJECTS) * 2
    source_commit = (destination / "SOURCE_COMMIT").read_text().strip()
    assert len(source_commit) == 40
    assert all(character in "0123456789abcdef" for character in source_commit)

    checksum_lines = (destination / "SHA256SUMS").read_text().splitlines()
    assert len(checksum_lines) == len(EXPECTED_PROJECTS) * 2
    expected_checksums = []
    for artifact in sorted((destination / "all").iterdir(), key=lambda item: item.name):
        expected_checksums.append(
            f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  all/{artifact.name}"
        )
    assert checksum_lines == expected_checksums

    for project_name in EXPECTED_PROJECTS:
        assert sorted(
            path.name for path in (destination / "packages" / project_name).iterdir()
        ) == [*_artifact_names(project_name)]


def test_stage_release_artifacts_rejects_an_incomplete_set(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    destination = tmp_path / "release"
    source.mkdir()
    first_project = EXPECTED_PROJECTS[0]
    (source / _artifact_names(first_project)[0]).write_bytes(b"wheel only")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.stage_release_artifacts",
            str(source),
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )

    assert result.returncode == 1
    assert "expected one wheel and one sdist" in result.stdout
    assert not destination.exists()


def test_stage_release_artifacts_rejects_an_ancestor_destination(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    source.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.stage_release_artifacts",
            str(source),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )

    assert result.returncode == 1
    assert "must be separate sibling directories" in result.stdout
    assert source.exists()
