# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sqlite3
from pathlib import Path

from ferry_connector_sqlite import CONNECTOR, SqliteDestination

from ferry.connector import Credentials, WriteOptions


def _credentials(path: Path) -> Credentials:
    return Credentials(provider="sqlite", settings={"connection": str(path)})


OPTIONS = WriteOptions(conflict_policy="update_existing", identity_fields=["path"])


async def test_create_update_skip_and_schema_evolution(tmp_path: Path) -> None:
    db = tmp_path / "out.db"
    destination = SqliteDestination()
    credentials = _credentials(db)

    created = await destination.write_record(
        credentials,
        object_type="documents",
        properties={"path": "a.md", "title": "A", "published": True, "tags": ["x", "y"]},
        options=OPTIONS,
    )
    assert created.status == "created"

    updated = await destination.write_record(
        credentials,
        object_type="documents",
        properties={"path": "a.md", "title": "A2", "views": 10},
        options=OPTIONS,
    )
    assert updated.status == "updated"
    assert updated.destination_record_id == created.destination_record_id

    skipped = await destination.write_record(
        credentials,
        object_type="documents",
        properties={"path": "a.md", "title": "ignored"},
        options=WriteOptions(conflict_policy="skip_existing", identity_fields=["path"]),
    )
    assert skipped.status == "skipped"

    rows = (
        sqlite3.connect(db)
        .execute("SELECT path, title, published, tags, views FROM documents")
        .fetchall()
    )
    assert rows == [("a.md", "A2", 1, '["x", "y"]', 10)]


async def test_inventory_readback_and_target_suggestion(tmp_path: Path) -> None:
    db = tmp_path / "out.db"
    destination = SqliteDestination()
    credentials = _credentials(db)
    for index in range(3):
        await destination.write_record(
            credentials,
            object_type="documents",
            properties={"path": f"{index}.md"},
            options=OPTIONS,
        )

    inventory = await destination.inventory(credentials, canonical_types={"documents", "absent"})
    assert len(inventory.objects) == 1
    assert inventory.objects[0].record_count == 3
    assert destination.automatic_target_object("My Docs!") == "my_docs"
    assert CONNECTOR.name == "sqlite"
    assert CONNECTOR.destination is not None and CONNECTOR.source is None


async def test_inventory_on_missing_database_is_empty_and_creates_nothing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "never-created.db"
    inventory = await SqliteDestination().inventory(_credentials(db), canonical_types={"documents"})
    assert inventory.objects == []
    assert not db.exists()  # planning/inspection must leave no artifacts


async def test_sqlite_url_prefix_is_accepted(tmp_path: Path) -> None:
    db = tmp_path / "url.db"
    destination = SqliteDestination()
    result = await destination.write_record(
        Credentials(provider="sqlite", settings={"connection": f"sqlite:///{db}"}),
        object_type="items",
        properties={"path": "x", "n": 1},
        options=OPTIONS,
    )
    assert result.status == "created"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
