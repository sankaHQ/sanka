# SPDX-License-Identifier: AGPL-3.0-only
"""Flagship demo e2e: PostgreSQL → ClickHouse through create → inspect →
plan → apply → verify. Runs only when both integration backends are
configured (in CI both service containers are)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

from ferry.runtime.engine import MigrationEngine
from ferry.runtime.registry import ConnectorRegistry
from ferry.runtime.spec import EndpointSpec, MigrationSpec
from ferry.runtime.state import SqliteStateStore

POSTGRES_DSN = os.environ.get("FERRY_TEST_POSTGRES_DSN")
CLICKHOUSE_URL = os.environ.get("FERRY_TEST_CLICKHOUSE_URL")

pytestmark = pytest.mark.skipif(
    not (POSTGRES_DSN and CLICKHOUSE_URL),
    reason="set FERRY_TEST_POSTGRES_DSN and FERRY_TEST_CLICKHOUSE_URL to run",
)

ROWS = 40


async def test_flagship_postgres_to_clickhouse(tmp_path: Path) -> None:
    import psycopg

    assert POSTGRES_DSN is not None and CLICKHOUSE_URL is not None
    suffix = uuid.uuid4().hex[:8]
    schema = f"ferry_e2e_{suffix}"
    target_table = f"events_{suffix}"

    connection = await psycopg.AsyncConnection.connect(POSTGRES_DSN)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(
            f'CREATE TABLE "{schema}"."{target_table}" ('
            " id BIGINT PRIMARY KEY, tenant TEXT, amount DOUBLE PRECISION,"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        async with connection.cursor() as cursor:
            await cursor.executemany(
                f'INSERT INTO "{schema}"."{target_table}" (id, tenant, amount) VALUES (%s, %s, %s)',
                [(i, f"tenant-{i % 3}", i * 1.5) for i in range(1, ROWS + 1)],
            )
        await connection.commit()

        spec = MigrationSpec(
            source=EndpointSpec(
                type="postgres", connection=POSTGRES_DSN, options={"schema": schema}
            ),
            target=EndpointSpec(type="clickhouse", connection=CLICKHOUSE_URL),
        )
        engine = MigrationEngine(
            store=SqliteStateStore(tmp_path / "state.db"),
            registry=ConnectorRegistry.discover(),
            batch_size=16,  # force multiple pages/checkpoints
        )
        run_id = engine.create(spec)
        plan = await engine.plan(run_id)
        assert [r.estimated_count for r in plan.routes] == [ROWS]
        assert plan.routes[0].identity_field == "id"

        await engine.apply(run_id, plan_hash=plan.plan_hash)
        report = await engine.verify(run_id)
        route = report.routes[0]
        assert report.ok, report
        assert route.migrated == ROWS
        assert route.failed == 0
        assert route.destination_count == ROWS  # ClickHouse FINAL count readback
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.commit()
        await connection.close()
        _drop_clickhouse_table(CLICKHOUSE_URL, target_table)


def _drop_clickhouse_table(url: str, table: str) -> None:
    import clickhouse_connect

    parsed = urlparse(url)
    client = clickhouse_connect.get_client(
        host=parsed.hostname or "localhost",
        port=parsed.port or 8123,
        database=(parsed.path.lstrip("/") or "default"),
        username=parsed.username or "default",
        password=parsed.password or "",
    )
    try:
        client.command(f"DROP TABLE IF EXISTS `{table}`")
    finally:
        client.close()
