# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import sanka
import sanka.runtime
from sanka import Connection, EndpointSpec, PlanMismatchError, RunStatus, Sanka


def _write_content(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text("---\ntitle: A\n---\nAlpha body\n", encoding="utf-8")
    (root / "b.md").write_text("---\ntitle: B\n---\nBeta body\n", encoding="utf-8")


def _status(migration: sanka.Migration) -> RunStatus:
    return migration.status


def test_public_facade_exports_runtime_types() -> None:
    assert sanka.__version__ == sanka.runtime.__version__
    assert sanka.MigrationPlan.__module__ == "sanka.runtime.planner"
    assert sanka.MigrationSpec.__module__ == "sanka.runtime.spec"
    assert sanka.ExecutionError.__module__ == "sanka.runtime.engine"


def test_sanka_connect_selects_a_bundled_provider(tmp_path: Path) -> None:
    with Sanka(state=tmp_path / "state.db") as client:
        hubspot = client.connect("hubspot")
        postgres = client.connect("postgresql", "postgresql://localhost/example")

    assert hubspot == Connection(
        provider="hubspot",
        roles=("source", "destination"),
        connection=None,
        options={},
    )
    assert postgres.provider == "postgres"
    assert postgres.roles == ("source", "destination")
    assert postgres.endpoint() == EndpointSpec(
        type="postgres",
        connection="postgresql://localhost/example",
    )


def test_sanka_connect_rejects_an_unknown_provider(tmp_path: Path) -> None:
    with (
        Sanka(state=tmp_path / "state.db") as client,
        pytest.raises(sanka.UnknownConnectorError, match="not-a-provider"),
    ):
        client.connect("not-a-provider")


async def test_sanka_facade_runs_the_hash_bound_lifecycle(tmp_path: Path) -> None:
    content = tmp_path / "content"
    destination = tmp_path / "out.db"
    _write_content(content)

    with Sanka(state=tmp_path / "state.db", batch_size=1) as client:
        migration = client.migrate(content, f"sqlite://{destination}")
        assert _status(migration) == RunStatus.CREATED
        assert not destination.exists()

        plan = await migration.plan()
        assert migration.plan_hash == plan.plan_hash
        assert _status(migration) == RunStatus.PLANNED
        assert not destination.exists()

        validation = await migration.validate()
        assert validation["rejects"] == []
        assert not destination.exists()

        with pytest.raises(PlanMismatchError):
            await migration.apply(plan_hash="sha256:not-the-reviewed-plan")
        assert not destination.exists()

        await migration.apply(plan_hash=plan.plan_hash)
        report = await migration.verify()

        assert report.ok
        assert _status(migration) == RunStatus.VERIFIED
        with sqlite3.connect(destination) as connection:
            count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 2


def test_sanka_facade_reuses_runs_and_accepts_explicit_endpoints(tmp_path: Path) -> None:
    content = tmp_path / "content"
    _write_content(content)

    with Sanka(state=tmp_path / "state.db") as client:
        source = EndpointSpec(type="markdown", connection=str(content))
        target = client.connect("sqlite", tmp_path / "out.db")

        first = client.migrate(source, target)
        second = client.migrate(source, target)
        fresh = client.migrate(source, target, reuse=False)

        assert first.run_id == second.run_id
        assert fresh.run_id != first.run_id


def test_closed_sanka_facade_rejects_new_migrations(tmp_path: Path) -> None:
    client = Sanka(state=tmp_path / "state.db")
    client.close()

    with pytest.raises(RuntimeError, match="closed"):
        client.migrate("postgres://source", "postgres://target")


def test_sanka_facade_uses_public_default_state_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with Sanka():
        pass

    assert (tmp_path / ".sanka" / "migrate" / "state.db").is_file()
