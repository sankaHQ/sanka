# SPDX-License-Identifier: Apache-2.0
"""Integration tests against a live ClickHouse server.

Set ``SANKA_MIGRATE_TEST_CLICKHOUSE_URL`` (e.g. ``http://localhost:8123/default``) to
run these; without it the whole module is skipped so ``make check`` stays
green offline. A disposable server works fine::

    docker run --rm -d -p 127.0.0.1:18123:8123 \
        -e CLICKHOUSE_SKIP_USER_SETUP=1 clickhouse/clickhouse-server:24.8
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import clickhouse_connect
import pytest
from clickhouse_connect.driver.client import Client
from sanka_connector_clickhouse import ClickHouseDestination, _parse_connection

from sanka.connector import (
    AuthenticationError,
    BatchWriteInput,
    Credentials,
    WriteOptions,
)

CLICKHOUSE_URL = os.environ.get("SANKA_MIGRATE_TEST_CLICKHOUSE_URL", "")

pytestmark = pytest.mark.skipif(
    not CLICKHOUSE_URL, reason="SANKA_MIGRATE_TEST_CLICKHOUSE_URL is not set"
)


def _credentials() -> Credentials:
    return Credentials(provider="clickhouse", settings={"connection": CLICKHOUSE_URL})


def _raw_client() -> Client:
    target = _parse_connection(_credentials())
    return clickhouse_connect.get_client(
        interface=target.interface,
        host=target.host,
        port=target.port,
        username=target.username,
        password=target.password,
        database=target.database,
    )


@pytest.fixture
def credentials() -> Credentials:
    return _credentials()


@pytest.fixture
def scratch_table() -> Iterator[Callable[[], str]]:
    created: list[str] = []

    def make() -> str:
        name = f"sanka_migrate_it_{uuid.uuid4().hex[:12]}"
        created.append(name)
        return name

    yield make
    client = _raw_client()
    for name in created:
        client.command(f"DROP TABLE IF EXISTS `{name}`")
    client.close()


async def test_batch_write_dedup_final_count_and_inventory(
    credentials: Credentials, scratch_table: Callable[[], str]
) -> None:
    destination = ClickHouseDestination()
    table = scratch_table()
    options = WriteOptions(conflict_policy="update_existing", identity_fields=["path"])

    first = [
        BatchWriteInput(
            trace_id=f"t{index}",
            properties={
                "path": f"doc-{index}.md",
                "title": f"Title {index}",
                "published": index % 2 == 0,
                "views": index * 10,
                "score": index + 0.5,
                "tags": ["a", "b"],
            },
        )
        for index in range(3)
    ]
    results = await destination.write_records(
        credentials, object_type=table, records=first, options=options
    )
    assert [result.trace_id for result in results] == ["t0", "t1", "t2"]
    assert all(result.status == "created" for result in results)

    # Re-apply the same identities: at-least-once semantics insert new
    # versions, and ReplacingMergeTree dedups them by ORDER BY key at merge
    # time — the FINAL count must stay stable.
    second = [
        BatchWriteInput(
            trace_id=f"r{index}",
            properties={"path": f"doc-{index}.md", "title": f"Rewritten {index}"},
        )
        for index in range(3)
    ]
    results = await destination.write_records(
        credentials, object_type=table, records=second, options=options
    )
    assert all(result.status == "created" for result in results)

    inventory = await destination.inventory(credentials, canonical_types={table, "absent_object"})
    assert len(inventory.objects) == 1
    schema = inventory.objects[0]
    assert schema.key == table
    assert schema.record_count == 3  # FINAL count, stable across the re-write
    field_keys = {field.key for field in schema.fields}
    assert {"path", "title", "published", "views", "score", "tags"} <= field_keys

    client = _raw_client()
    try:
        engine, sorting_key = client.query(
            "SELECT engine, sorting_key FROM system.tables "
            "WHERE database = currentDatabase() AND name = {name:String}",
            parameters={"name": table},
        ).result_rows[0]
        assert engine == "ReplacingMergeTree"
        assert sorting_key == "path"

        final_rows = client.query(
            f"SELECT title, views FROM `{table}` FINAL WHERE path = 'doc-1.md'"
        ).result_rows
        assert len(final_rows) == 1
        assert final_rows[0][0] == "Rewritten 1"  # FINAL reads the latest version
        assert final_rows[0][1] is None  # keys missing from the re-write became None

        raw_count = int(client.query(f"SELECT count() FROM `{table}`").result_rows[0][0])
        assert raw_count >= 3  # unmerged versions may linger — why inventory counts FINAL
    finally:
        client.close()


async def test_single_write_schema_evolution_and_empty_skip(
    credentials: Credentials, scratch_table: Callable[[], str]
) -> None:
    destination = ClickHouseDestination()
    table = scratch_table()
    options = WriteOptions(conflict_policy="create", identity_fields=["id"])

    created = await destination.write_record(
        credentials, object_type=table, properties={"id": "a", "n": 1}, options=options
    )
    assert created.status == "created"

    evolved = await destination.write_record(
        credentials, object_type=table, properties={"id": "b", "comment": "追記"}, options=options
    )
    assert evolved.status == "created"

    skipped = await destination.write_record(
        credentials, object_type=table, properties={}, options=options
    )
    assert skipped.status == "skipped"

    inventory = await destination.inventory(credentials, canonical_types={table})
    schema = inventory.objects[0]
    assert schema.record_count == 2
    by_key = {field.key: field for field in schema.fields}
    assert by_key["comment"].metadata["clickhouse_type"] == "Nullable(String)"
    assert by_key["n"].metadata["clickhouse_type"] == "Nullable(Int64)"
    assert by_key["id"].metadata["clickhouse_type"] == "String"  # identity: non-Nullable


async def test_no_identity_falls_back_to_merge_tree_and_inventory_still_counts(
    credentials: Credentials, scratch_table: Callable[[], str]
) -> None:
    destination = ClickHouseDestination()
    table = scratch_table()
    options = WriteOptions(conflict_policy="create")

    await destination.write_records(
        credentials,
        object_type=table,
        records=[
            BatchWriteInput(trace_id="t0", properties={"event": "x"}),
            BatchWriteInput(trace_id="t1", properties={"event": "y"}),
        ],
        options=options,
    )

    client = _raw_client()
    try:
        engine = client.query(
            "SELECT engine FROM system.tables "
            "WHERE database = currentDatabase() AND name = {name:String}",
            parameters={"name": table},
        ).result_rows[0][0]
        assert engine == "MergeTree"
    finally:
        client.close()

    # Plain MergeTree rejects FINAL with ILLEGAL_FINAL (observed on 24.8), so
    # inventory's engine guard must skip FINAL here and still count correctly.
    inventory = await destination.inventory(credentials, canonical_types={table})
    assert inventory.objects[0].record_count == 2


async def test_wrong_credentials_map_to_authentication_error(
    scratch_table: Callable[[], str],
) -> None:
    target = _parse_connection(_credentials())
    scheme = "https" if target.interface == "https" else "http"
    bad_url = (
        f"{scheme}://sanka_migrate_no_such_user:wrong@{target.host}:{target.port}/{target.database}"
    )
    destination = ClickHouseDestination()
    with pytest.raises(AuthenticationError):
        await destination.write_record(
            Credentials(provider="clickhouse", settings={"connection": bad_url}),
            object_type=scratch_table(),
            properties={"id": "a"},
            options=WriteOptions(conflict_policy="create", identity_fields=["id"]),
        )
