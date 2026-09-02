# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import shutil
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sanka.runtime.registry import ConnectorRegistry


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


@pytest.fixture
def trusted_connector_discovery(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use explicit test registrations without production entry-point discovery."""
    module_names = (
        "sanka_connector_markdown",
        "sanka_connector_postgres",
        "sanka_connector_sqlite",
    )
    prior_modules = {
        name: module
        for name, module in sys.modules.items()
        if any(name == root or name.startswith(f"{root}.") for root in module_names)
    }
    from sanka_connector_markdown import CONNECTOR as markdown
    from sanka_connector_postgres import CONNECTOR as postgres
    from sanka_connector_sqlite import CONNECTOR as sqlite

    registrations = {
        registration.name: registration for registration in (markdown, postgres, sqlite)
    }
    monkeypatch.setattr(
        ConnectorRegistry,
        "discover",
        classmethod(lambda _cls, *_args, **_kwargs: ConnectorRegistry(dict(registrations))),
    )
    yield
    for name in tuple(sys.modules):
        if any(name == root or name.startswith(f"{root}.") for root in module_names):
            sys.modules.pop(name, None)
    sys.modules.update(prior_modules)
