# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sanka.connector import (
    ConfigurationError,
    Credentials,
    SupportsRecordCounts,
    UnsupportedFeatureError,
)
from sanka_connector_sqlite import CONNECTOR, SqliteSource


def _credentials(path: Path) -> Credentials:
    return Credentials(provider="sqlite", settings={"connection": str(path)})


def _seed(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL,
            in_stock BOOLEAN,
            notes
        );
        CREATE TABLE logs (message TEXT);
        CREATE TABLE pairs (a TEXT, b TEXT, PRIMARY KEY (a, b)) WITHOUT ROWID;
        """
    )
    connection.executemany(
        "INSERT INTO items (id, name, price, in_stock, notes) VALUES (?, ?, ?, ?, ?)",
        [(index, f"item-{index}", 1.5 * index, index % 2, None) for index in range(1, 6)],
    )
    connection.executemany("INSERT INTO logs (message) VALUES (?)", [("m1",), ("m2",), ("m3",)])
    connection.execute("INSERT INTO pairs (a, b) VALUES ('x', 'y')")
    connection.commit()
    connection.close()


async def test_discover_objects_lists_user_tables_only(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    objects = await SqliteSource().discover_objects(_credentials(db))
    # AUTOINCREMENT creates sqlite_sequence, which must be skipped as an internal.
    assert [o.key for o in objects] == ["items", "logs", "pairs"]
    assert all(o.default_selected and o.canonical_type == o.key for o in objects)


async def test_inventory_primary_key_table(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    inventory = await SqliteSource().inventory(_credentials(db), object_types=["items"])
    assert len(inventory.objects) == 1
    items = inventory.objects[0]
    assert items.record_count == 5
    assert items.identity_fields == ["id"]
    assert {f.key: f.data_type for f in items.fields} == {
        "id": "number",
        "name": "string",
        "price": "number",
        "in_stock": "boolean",
        "notes": "string",
    }


async def test_inventory_rowid_and_without_rowid_paths(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    inventory = await SqliteSource().inventory(_credentials(db))
    by_key = {o.key: o for o in inventory.objects}

    logs = by_key["logs"]
    assert logs.identity_fields == ["rowid"]
    assert [f.key for f in logs.fields] == ["message", "rowid"]
    assert logs.fields[-1].data_type == "number"

    pairs = by_key["pairs"]
    assert pairs.identity_fields == []
    assert any("pairs" in w for w in inventory.warnings)


async def test_read_records_keyset_paginates_across_pages(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    source = SqliteSource()
    credentials = _credentials(db)

    first = await source.read_records(
        credentials, object_type="items", field_keys=["id", "name"], limit=2
    )
    assert [r["id"] for r in first.records] == [1, 2]
    assert first.has_more and first.next_cursor == "2"

    second = await source.read_records(
        credentials, object_type="items", field_keys=["id", "name"], limit=2, cursor="2"
    )
    assert [r["id"] for r in second.records] == [3, 4]
    assert second.has_more and second.next_cursor == "4"

    third = await source.read_records(
        credentials, object_type="items", field_keys=["id", "name"], limit=2, cursor="4"
    )
    assert [r["id"] for r in third.records] == [5]
    assert third.records[0]["name"] == "item-5"
    assert not third.has_more and third.next_cursor is None


async def test_read_records_rowid_path_and_missing_fields(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    source = SqliteSource()
    credentials = _credentials(db)

    first = await source.read_records(
        credentials, object_type="logs", field_keys=["message", "rowid", "absent"], limit=2
    )
    assert first.records == [
        {"message": "m1", "rowid": 1, "absent": None},
        {"message": "m2", "rowid": 2, "absent": None},
    ]
    assert first.has_more and first.next_cursor == "2"

    rest = await source.read_records(
        credentials,
        object_type="logs",
        field_keys=["message", "rowid", "absent"],
        limit=2,
        cursor=first.next_cursor,
    )
    assert rest.records == [{"message": "m3", "rowid": 3, "absent": None}]
    assert not rest.has_more and rest.next_cursor is None

    # The cursor advances even when the identity is not among the requested fields.
    unrequested = await source.read_records(
        credentials, object_type="logs", field_keys=["message"], limit=1
    )
    assert unrequested.records == [{"message": "m1"}]
    assert unrequested.has_more and unrequested.next_cursor == "1"


async def test_without_rowid_composite_key_reads_are_unsupported(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    with pytest.raises(UnsupportedFeatureError):
        await SqliteSource().read_records(
            _credentials(db), object_type="pairs", field_keys=["a", "b"], limit=10
        )


async def test_count_capability_and_registration_has_both_roles(tmp_path: Path) -> None:
    db = tmp_path / "in.db"
    _seed(db)
    source = SqliteSource()
    assert await source.count_records(_credentials(db), object_type="items") == 5
    assert await source.count_records(_credentials(db), object_type="logs") == 3
    assert isinstance(source, SupportsRecordCounts)
    assert CONNECTOR.name == "sqlite"
    assert CONNECTOR.source is not None
    assert CONNECTOR.destination is not None


async def test_missing_database_is_configuration_error(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    with pytest.raises(ConfigurationError):
        await SqliteSource().discover_objects(_credentials(missing))
    # Reading must never create an empty database file as a side effect.
    assert not missing.exists()
