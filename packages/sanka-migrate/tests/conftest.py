# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--extension-release",
        help="absolute or working-directory-relative path to built extension wheels",
    )


@pytest.fixture
def extension_release(request: pytest.FixtureRequest) -> Path:
    value = request.config.getoption("--extension-release")
    if not value:
        pytest.fail("--extension-release is required and must name a wheel directory")
    release = Path(value).expanduser().resolve()
    if not release.is_dir():
        pytest.fail(f"--extension-release is not a directory: {release}")
    return release


@pytest.fixture
def drf_extension_fixture(tmp_path: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "extension_drf_project"
    project = tmp_path / "drf-project"
    shutil.copytree(source, project)
    (project / "requirements.txt").write_text(
        "Django>=5.2\ndjangorestframework>=3.16\n",
        encoding="utf-8",
    )
    with sqlite3.connect(project / "db.sqlite3") as database:
        database.execute(
            "CREATE TABLE api_health (id INTEGER PRIMARY KEY AUTOINCREMENT, status VARCHAR(32))"
        )
    return project
