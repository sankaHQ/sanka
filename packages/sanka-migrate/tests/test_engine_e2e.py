# SPDX-License-Identifier: AGPL-3.0-only
"""End-to-end: the PRD's first demo — Markdown directory → SQLite — through
the full create → inspect → plan → apply → verify lifecycle, via both the
engine API and the CLI shorthand."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sanka.cli import main
from sanka.runtime.engine import MigrationEngine, PlanMismatchError
from sanka.runtime.registry import ConnectorRegistry
from sanka.runtime.spec import EndpointSpec, MigrationSpec
from sanka.runtime.state import RunStatus, SqliteStateStore


def _write_content(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text(
        "---\ntitle: A\npublished: true\nviews: 10\n---\nAlpha body\n", encoding="utf-8"
    )
    (root / "b.md").write_text("---\ntitle: B\nviews: many\n---\nBeta body\n", encoding="utf-8")
    (root / "c.md").write_text("Plain body\n", encoding="utf-8")


def _spec(content: Path, db: Path) -> MigrationSpec:
    return MigrationSpec(
        source=EndpointSpec(type="markdown", connection=str(content)),
        target=EndpointSpec(type="sqlite", connection=str(db)),
    )


def _engine(tmp_path: Path) -> MigrationEngine:
    return MigrationEngine(
        store=SqliteStateStore(tmp_path / "state" / "state.db"),
        registry=ConnectorRegistry.discover(),
        batch_size=2,  # force pagination + multiple checkpoints
    )


async def test_full_lifecycle_markdown_to_sqlite(tmp_path: Path) -> None:
    content, db = tmp_path / "content", tmp_path / "out.db"
    _write_content(content)
    engine = _engine(tmp_path)

    run_id = engine.create(_spec(content, db))
    plan = await engine.plan(run_id)

    assert [r.route_key for r in plan.routes] == ["documents|documents"]
    route = plan.routes[0]
    assert route.mapping_origin == "identity"  # fresh target database: no schema to map onto
    assert route.identity_field == "path"
    assert route.identity_target_fields == ["path"]
    assert route.estimated_count == 3
    assert any("mixed types" in w for w in plan.warnings)
    assert 0.5 <= plan.ready < 1.0
    assert plan.plan_hash.startswith("sha256:")

    await engine.apply(run_id, plan_hash=plan.plan_hash)
    rows = (
        sqlite3.connect(db)
        .execute("SELECT path, title, content FROM documents ORDER BY path")
        .fetchall()
    )
    assert [row[0] for row in rows] == ["a.md", "b.md", "c.md"]
    assert rows[0][1] == "A"
    assert "Plain body" in rows[2][2]

    report = await engine.verify(run_id)
    assert report.ok
    assert report.routes[0].source_count == 3
    assert report.routes[0].migrated == 3
    assert report.routes[0].destination_count == 3
    assert engine.store.get_run(run_id).status is RunStatus.VERIFIED


async def test_reapply_is_idempotent_via_identity_ledger(tmp_path: Path) -> None:
    content, db = tmp_path / "content", tmp_path / "out.db"
    _write_content(content)
    engine = _engine(tmp_path)

    run_id = engine.create(_spec(content, db))
    await engine.plan(run_id)
    await engine.apply(run_id)
    first_summary = engine.store.ledger_summary(run_id)

    # Re-running the same run must not duplicate rows or ledger entries —
    # terminal records are filtered, checkpoints already say done.
    await engine.apply(run_id)
    assert engine.store.ledger_summary(run_id) == first_summary
    count = sqlite3.connect(db).execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert count == 3

    # And a second engine.create for the identical spec reuses the run.
    assert engine.create(_spec(content, db)) == run_id


async def test_apply_is_plan_hash_bound(tmp_path: Path) -> None:
    content, db = tmp_path / "content", tmp_path / "out.db"
    _write_content(content)
    engine = _engine(tmp_path)
    run_id = engine.create(_spec(content, db))
    await engine.plan(run_id)

    with pytest.raises(PlanMismatchError):
        await engine.apply(run_id, plan_hash="sha256:not-the-reviewed-plan")
    assert not db.exists()  # nothing was written


def test_cli_migrate_shorthand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    content, db = tmp_path / "content", tmp_path / "out.db"
    _write_content(content)
    state = tmp_path / "state.db"

    exit_code = main(["migrate", str(content), f"sqlite://{db}", "--yes", "--state", str(state)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "documents -> documents" in output
    assert "plan hash: sha256:" in output
    assert "verification OK" in output
    count = sqlite3.connect(db).execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert count == 3


def test_cli_spec_flow_plan_apply_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    content, db = tmp_path / "content", tmp_path / "out.db"
    _write_content(content)
    spec_file = tmp_path / "sanka-migrate.yaml"
    spec_file.write_text(
        f"source:\n  type: markdown\n  connection: {content}\n"
        f"target:\n  type: sqlite\n  connection: {db}\n",
        encoding="utf-8",
    )
    state = tmp_path / "state.db"
    base = ["-f", str(spec_file), "--state", str(state)]

    assert main(["plan", *base]) == 0
    assert main(["apply", *base]) == 0
    assert main(["verify", *base]) == 0
    assert main(["status", *base]) == 0
    output = capsys.readouterr().out
    assert "status=verified" in output
    assert "created=3" in output
