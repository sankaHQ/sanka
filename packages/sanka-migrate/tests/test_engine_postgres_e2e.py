# SPDX-License-Identifier: AGPL-3.0-only
"""End-to-end: Markdown directory → PostgreSQL through the engine lifecycle.

Gated on ``SANKA_MIGRATE_TEST_POSTGRES_DSN`` like the connector integration tests.
Asserts engine-level outcomes only (row counts and the verify report) — plan
payload internals are deliberately not inspected here.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from sanka.runtime.engine import MigrationEngine
from sanka.runtime.registry import ConnectorRegistry
from sanka.runtime.spec import EndpointSpec, MigrationSpec
from sanka.runtime.state import SqliteStateStore
from sanka_connector_postgres import CONNECTOR, PostgresDestination

_DSN = os.environ.get("SANKA_MIGRATE_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(not _DSN, reason="SANKA_MIGRATE_TEST_POSTGRES_DSN is not set")


@pytest.fixture
async def schema() -> AsyncIterator[str]:
    assert _DSN is not None
    name = f"sanka_migrate_e2e_{secrets.token_hex(4)}"
    connection = await psycopg.AsyncConnection.connect(_DSN, autocommit=True)
    try:
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        yield name
        await connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    finally:
        await connection.close()
    destination = CONNECTOR.destination
    assert isinstance(destination, PostgresDestination)
    await destination.close()


def _write_content(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text(
        "---\ntitle: A\npublished: true\nviews: 10\n---\nAlpha body\n", encoding="utf-8"
    )
    (root / "b.md").write_text("---\ntitle: B\nviews: many\n---\nBeta body\n", encoding="utf-8")
    (root / "c.md").write_text("Plain body\n", encoding="utf-8")


async def _table_count(schema: str, table: str) -> int:
    assert _DSN is not None
    async with await psycopg.AsyncConnection.connect(_DSN) as connection:
        cursor = await connection.execute(
            sql.SQL("SELECT COUNT(*) FROM {table}").format(table=sql.Identifier(schema, table))
        )
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


async def test_markdown_to_postgres_lifecycle(tmp_path: Path, schema: str) -> None:
    content = tmp_path / "content"
    _write_content(content)
    spec = MigrationSpec(
        source=EndpointSpec(type="markdown", connection=str(content)),
        target=EndpointSpec(
            type="postgres",
            connection="$SANKA_MIGRATE_TEST_POSTGRES_DSN",
            options={"schema": schema},
        ),
    )
    engine = MigrationEngine(
        store=SqliteStateStore(tmp_path / "state" / "state.db"),
        registry=ConnectorRegistry.discover(),
        batch_size=2,  # force pagination + multiple checkpoints
    )

    run_id = engine.create(spec)
    plan = await engine.plan(run_id)
    await engine.apply(run_id, plan_hash=plan.plan_hash)

    report = await engine.verify(run_id)
    assert report.ok
    assert len(report.routes) == 1
    assert report.routes[0].source_count == 3
    assert report.routes[0].migrated == 3
    assert report.routes[0].failed == 0
    assert report.routes[0].destination_count == 3
    assert await _table_count(schema, "documents") == 3

    # Re-applying converges instead of duplicating (identity ledger).
    await engine.apply(run_id, plan_hash=plan.plan_hash)
    assert await _table_count(schema, "documents") == 3
