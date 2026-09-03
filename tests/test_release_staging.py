# SPDX-License-Identifier: Apache-2.0
"""Release staging keeps privileged publishing jobs free of build-time code."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_NAMES = (
    "sanka_cli-0.2.3-py3-none-any.whl",
    "sanka_cli-0.2.3.tar.gz",
)


def _run(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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


def test_stage_release_artifacts_builds_one_flat_verified_set(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    destination = tmp_path / "release"
    source.mkdir()
    for filename in ARTIFACT_NAMES:
        (source / filename).write_bytes(filename.encode())

    result = _run(source, destination)

    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(path.name for path in destination.iterdir()) == [
        "SHA256SUMS",
        "SOURCE_COMMIT",
        *ARTIFACT_NAMES,
    ]
    source_commit = (destination / "SOURCE_COMMIT").read_text().strip()
    assert len(source_commit) == 40
    assert all(character in "0123456789abcdef" for character in source_commit)
    assert (destination / "SHA256SUMS").read_text().splitlines() == [
        f"{hashlib.sha256((source / name).read_bytes()).hexdigest()}  {name}"
        for name in ARTIFACT_NAMES
    ]


def test_stage_release_artifacts_rejects_an_incomplete_set(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    destination = tmp_path / "release"
    source.mkdir()
    (source / ARTIFACT_NAMES[0]).write_bytes(b"wheel only")

    result = _run(source, destination)

    assert result.returncode == 1
    assert "expected one wheel and one sdist" in result.stdout
    assert not destination.exists()


def test_stage_release_artifacts_rejects_an_ancestor_destination(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    source.mkdir()

    result = _run(source, tmp_path)

    assert result.returncode == 1
    assert "must be separate sibling directories" in result.stdout
    assert source.exists()
