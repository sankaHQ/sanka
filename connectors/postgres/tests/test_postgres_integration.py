# SPDX-License-Identifier: Apache-2.0
"""Integration tests against a real PostgreSQL server.

Gated on ``SANKA_MIGRATE_TEST_POSTGRES_DSN``; the whole module skips when it is not
set. Every test works inside a scratch schema with a random suffix and drops
it on teardown, so any database the DSN user can create schemas in works
(CI uses a postgres:16 service container).
"""

from __future__ import annotations

import datetime
import os
import secrets
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Json
from sanka_connector_postgres import PostgresDestination, PostgresSource

from sanka.connector import (
    Credentials,
    DataError,
    SourceFilter,
    UnsupportedFeatureError,
    WriteOptions,
)

_DSN = os.environ.get("SANKA_MIGRATE_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(not _DSN, reason="SANKA_MIGRATE_TEST_POSTGRES_DSN is not set")


def _credentials(schema: str) -> Credentials:
    return Credentials(provider="postgres", settings={"connection": _DSN, "schema": schema})


@pytest.fixture
async def admin() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    assert _DSN is not None
    connection = await psycopg.AsyncConnection.connect(_DSN, autocommit=True)
    yield connection
    await connection.close()


@pytest.fixture
async def schema(admin: psycopg.AsyncConnection[Any]) -> AsyncIterator[str]:
    name = f"sanka_migrate_it_{secrets.token_hex(4)}"
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    yield name
    await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
async def source() -> AsyncIterator[PostgresSource]:
    connector = PostgresSource()
    yield connector
    await connector.close()


@pytest.fixture
async def destination() -> AsyncIterator[PostgresDestination]:
    connector = PostgresDestination()
    yield connector
    await connector.close()


async def _seed_books(admin: psycopg.AsyncConnection[Any], schema: str, count: int = 3) -> None:
    await admin.execute(
        sql.SQL(
            """
            CREATE TABLE {table} (
              id bigint PRIMARY KEY,
              title text NOT NULL,
              price numeric(8, 2),
              published boolean,
              attrs jsonb,
              created_at timestamptz,
              cover bytea
            )
            """
        ).format(table=sql.Identifier(schema, "books"))
    )
    for index in range(1, count + 1):
        await admin.execute(
            sql.SQL(
                "INSERT INTO {table} (id, title, price, published, attrs, created_at, cover)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            ).format(table=sql.Identifier(schema, "books")),
            (
                index,
                f"Book {index}",
                Decimal("12.50") + index if index != 2 else None,
                index % 2 == 1,
                Json({"tags": ["x", str(index)]}),
                datetime.datetime(2026, 8, index, 9, 30, tzinfo=datetime.UTC),
                b"\x00\x01" if index == 1 else None,
            ),
        )


async def test_discover_and_inventory(
    admin: psycopg.AsyncConnection[Any], schema: str, source: PostgresSource
) -> None:
    await _seed_books(admin, schema)
    await admin.execute(
        sql.SQL(
            "CREATE TABLE {table} (book_id bigint, page_no int, body text,"
            " PRIMARY KEY (book_id, page_no))"
        ).format(table=sql.Identifier(schema, "pages"))
    )
    await admin.execute(
        sql.SQL("CREATE TABLE {table} (body text)").format(table=sql.Identifier(schema, "notes"))
    )

    objects = await source.discover_objects(_credentials(schema))
    assert [o.key for o in objects] == ["books", "notes", "pages"]
    assert all(o.default_selected for o in objects)

    inventory = await source.inventory(_credentials(schema))
    by_key = {o.key: o for o in inventory.objects}
    books = by_key["books"]
    assert books.record_count == 3
    assert books.identity_fields == ["id"]
    families = {f.key: f.data_type for f in books.fields}
    assert families == {
        "id": "number",
        "title": "string",
        "price": "number",
        "published": "boolean",
        "attrs": "object",
        "created_at": "string",
    }
    assert "cover" not in families  # bytea excluded
    created_at = next(f for f in books.fields if f.key == "created_at")
    assert created_at.metadata == {"pg_type": "timestamp with time zone"}
    assert next(f for f in books.fields if f.key == "id").unique

    assert by_key["pages"].identity_fields == []
    assert by_key["notes"].identity_fields == []
    assert any("composite primary key" in w for w in inventory.warnings)
    assert any("no primary key" in w for w in inventory.warnings)
    assert any("binary column books.cover" in w for w in inventory.warnings)

    filtered = await source.inventory(_credentials(schema), object_types=["books", "missing"])
    assert [o.key for o in filtered.objects] == ["books"]
    assert any("'missing' not found" in w for w in filtered.warnings)


async def test_read_records_keyset_pagination_and_value_safety(
    admin: psycopg.AsyncConnection[Any], schema: str, source: PostgresSource
) -> None:
    await _seed_books(admin, schema, count=5)
    credentials = _credentials(schema)
    field_keys = ["id", "title", "price", "published", "attrs", "created_at", "cover"]

    with pytest.warns(UserWarning, match="binary value"):
        first = await source.read_records(
            credentials, object_type="books", field_keys=field_keys, limit=2
        )
    assert [r["id"] for r in first.records] == [1, 2]
    assert first.has_more and first.next_cursor == "2"
    record = first.records[0]
    assert record["title"] == "Book 1"
    assert record["price"] == "13.50"  # Decimal -> string
    assert record["published"] is True
    assert record["attrs"] == {"tags": ["x", "1"]}
    assert record["created_at"] == "2026-08-01T09:30:00+00:00"
    assert "cover" not in record  # binary value dropped
    assert first.records[1]["price"] is None  # NULL passes through

    second = await source.read_records(
        credentials, object_type="books", field_keys=field_keys, limit=2, cursor="2"
    )
    assert [r["id"] for r in second.records] == [3, 4]
    third = await source.read_records(
        credentials, object_type="books", field_keys=field_keys, limit=2, cursor="4"
    )
    assert [r["id"] for r in third.records] == [5]
    assert not third.has_more and third.next_cursor is None

    with pytest.raises(DataError):
        await source.read_records(credentials, object_type="books", field_keys=["nope"], limit=2)
    with pytest.raises(DataError):
        await source.read_records(
            credentials, object_type="notes_missing", field_keys=["body"], limit=2
        )


async def test_timestamp_primary_key_pagination(
    admin: psycopg.AsyncConnection[Any], schema: str, source: PostgresSource
) -> None:
    await admin.execute(
        sql.SQL("CREATE TABLE {table} (at timestamptz PRIMARY KEY, label text)").format(
            table=sql.Identifier(schema, "events")
        )
    )
    for day in (1, 2, 3):
        await admin.execute(
            sql.SQL("INSERT INTO {table} VALUES (%s, %s)").format(
                table=sql.Identifier(schema, "events")
            ),
            (datetime.datetime(2026, 8, day, tzinfo=datetime.UTC), f"day {day}"),
        )
    credentials = _credentials(schema)

    first = await source.read_records(
        credentials, object_type="events", field_keys=["at", "label"], limit=2
    )
    assert [r["label"] for r in first.records] == ["day 1", "day 2"]
    assert first.next_cursor == "2026-08-02T00:00:00+00:00"
    second = await source.read_records(
        credentials,
        object_type="events",
        field_keys=["at", "label"],
        limit=2,
        cursor=first.next_cursor,
    )
    assert [r["label"] for r in second.records] == ["day 3"]
    assert not second.has_more

    mark = await source.high_water_mark(credentials, object_type="events")
    assert mark == "2026-08-03T00:00:00+00:00"


async def test_count_and_snapshot_bounds(
    admin: psycopg.AsyncConnection[Any], schema: str, source: PostgresSource
) -> None:
    await _seed_books(admin, schema, count=5)
    credentials = _credentials(schema)

    assert await source.count_records(credentials, object_type="books") == 5
    mark = await source.high_water_mark(credentials, object_type="books")
    assert mark == "5"

    # Rows arriving after the mark stay outside the frozen scope.
    for late in (6, 7):
        await admin.execute(
            sql.SQL("INSERT INTO {table} (id, title) VALUES (%s, %s)").format(
                table=sql.Identifier(schema, "books")
            ),
            (late, f"Late {late}"),
        )
    assert await source.count_records(credentials, object_type="books") == 7
    assert (
        await source.count_records_bounded(credentials, object_type="books", upper_bound=mark) == 5
    )
    bounded = await source.read_records_bounded(
        credentials, object_type="books", field_keys=["id"], limit=10, upper_bound=mark
    )
    assert [r["id"] for r in bounded.records] == [1, 2, 3, 4, 5]
    assert not bounded.has_more

    with pytest.raises(UnsupportedFeatureError):
        await source.count_records(
            credentials, object_type="books", source_filter=SourceFilter(field="published")
        )


async def test_source_requires_single_column_primary_key(
    admin: psycopg.AsyncConnection[Any], schema: str, source: PostgresSource
) -> None:
    await admin.execute(
        sql.SQL("CREATE TABLE {table} (body text)").format(table=sql.Identifier(schema, "notes"))
    )
    await admin.execute(
        sql.SQL("INSERT INTO {table} VALUES ('x'), ('y')").format(
            table=sql.Identifier(schema, "notes")
        )
    )
    credentials = _credentials(schema)
    assert await source.count_records(credentials, object_type="notes") == 2
    with pytest.raises(DataError):
        await source.read_records(credentials, object_type="notes", field_keys=["body"], limit=1)
    with pytest.raises(DataError):
        await source.high_water_mark(credentials, object_type="notes")


OPTIONS = WriteOptions(conflict_policy="update_existing", identity_fields=["path"])


async def test_destination_create_update_skip_and_evolution(
    admin: psycopg.AsyncConnection[Any], schema: str, destination: PostgresDestination
) -> None:
    credentials = _credentials(schema)

    created = await destination.write_record(
        credentials,
        object_type="documents",
        properties={
            "path": "a.md",
            "title": "A",
            "published": True,
            "views": 10,
            "score": 1.5,
            "tags": ["x", "y"],
        },
        options=OPTIONS,
    )
    assert created.status == "created"
    assert created.destination_record_id == "a.md"

    updated = await destination.write_record(
        credentials,
        object_type="documents",
        properties={"path": "a.md", "title": "A2", "extra": "later"},  # schema widening
        options=OPTIONS,
    )
    assert updated.status == "updated"
    assert updated.destination_record_id == "a.md"

    skipped = await destination.write_record(
        credentials,
        object_type="documents",
        properties={"path": "a.md", "title": "ignored"},
        options=WriteOptions(conflict_policy="skip_existing", identity_fields=["path"]),
    )
    assert skipped.status == "skipped"

    types_cursor = await admin.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = 'documents'",
        (schema,),
    )
    types = dict(await types_cursor.fetchall())
    assert types == {
        "path": "text",
        "title": "text",
        "published": "boolean",
        "views": "bigint",
        "score": "double precision",
        "tags": "jsonb",
        "extra": "text",
    }

    index_cursor = await admin.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = 'documents'",
        (schema,),
    )
    assert ("documents_path_sanka_uq",) in await index_cursor.fetchall()

    rows_cursor = await admin.execute(
        sql.SQL("SELECT path, title, published, views, score, tags, extra FROM {table}").format(
            table=sql.Identifier(schema, "documents")
        )
    )
    assert await rows_cursor.fetchall() == [("a.md", "A2", True, 10, 1.5, ["x", "y"], "later")]


async def test_destination_type_promotion_round_trip(
    admin: psycopg.AsyncConnection[Any], schema: str, destination: PostgresDestination
) -> None:
    credentials = _credentials(schema)
    # First-seen typing: views -> bigint, flag -> boolean.
    await destination.write_record(
        credentials,
        object_type="posts",
        properties={"path": "a", "views": 10, "flag": True},
        options=OPTIONS,
    )
    # Mixed types arrive later: views must degrade to text, flag to double.
    await destination.write_record(
        credentials,
        object_type="posts",
        properties={"path": "b", "views": "many", "flag": 2.5},
        options=OPTIONS,
    )
    types_cursor = await admin.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = 'posts'",
        (schema,),
    )
    types = dict(await types_cursor.fetchall())
    assert types["views"] == "text"
    assert types["flag"] == "double precision"
    rows_cursor = await admin.execute(
        sql.SQL("SELECT path, views, flag FROM {table} ORDER BY path").format(
            table=sql.Identifier(schema, "posts")
        )
    )
    assert await rows_cursor.fetchall() == [("a", "10", 1.0), ("b", "many", 2.5)]


async def test_destination_inventory_and_create_policy(
    admin: psycopg.AsyncConnection[Any], schema: str, destination: PostgresDestination
) -> None:
    credentials = _credentials(schema)
    for index in range(3):
        await destination.write_record(
            credentials,
            object_type="documents",
            properties={"path": f"{index}.md"},
            options=OPTIONS,
        )

    inventory = await destination.inventory(credentials, canonical_types={"documents", "absent"})
    assert len(inventory.objects) == 1
    assert inventory.objects[0].key == "documents"
    assert inventory.objects[0].record_count == 3
    assert destination.automatic_target_object("My Docs!") == "my_docs"

    # create policy without identity: plain inserts, no destination id.
    for _ in range(2):
        result = await destination.write_record(
            credentials,
            object_type="loglines",
            properties={"message": "hello"},
            options=WriteOptions(conflict_policy="create"),
        )
        assert result.status == "created"
        assert result.destination_record_id is None
    count_cursor = await admin.execute(
        sql.SQL("SELECT COUNT(*) FROM {table}").format(table=sql.Identifier(schema, "loglines"))
    )
    row = await count_cursor.fetchone()
    assert row is not None and row[0] == 2


async def test_destination_writes_into_pre_existing_table(
    admin: psycopg.AsyncConnection[Any], schema: str, destination: PostgresDestination
) -> None:
    await admin.execute(
        sql.SQL(
            "CREATE TABLE {table} (code varchar(20) NOT NULL, qty integer,"
            " meta json, seen timestamptz)"
        ).format(table=sql.Identifier(schema, "stock"))
    )
    credentials = _credentials(schema)
    result = await destination.write_record(
        credentials,
        object_type="stock",
        properties={
            "code": "A-1",
            "qty": 7,  # native int into integer column
            "meta": {"a": 1},  # Json into a json (not jsonb) column
            "seen": "2026-08-16T09:30:00+00:00",  # server casts the literal
        },
        options=WriteOptions(conflict_policy="update_existing", identity_fields=["code"]),
    )
    assert result.status == "created"
    updated = await destination.write_record(
        credentials,
        object_type="stock",
        properties={"code": "A-1", "qty": 8},
        options=WriteOptions(conflict_policy="update_existing", identity_fields=["code"]),
    )
    assert updated.status == "updated"
    rows_cursor = await admin.execute(
        sql.SQL("SELECT code, qty, meta, seen FROM {table}").format(
            table=sql.Identifier(schema, "stock")
        )
    )
    rows = await rows_cursor.fetchall()
    assert len(rows) == 1
    code, qty, meta, seen = rows[0]
    assert (code, qty, meta) == ("A-1", 8, {"a": 1})
    assert seen == datetime.datetime(2026, 8, 16, 9, 30, tzinfo=datetime.UTC)
