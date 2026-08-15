# SPDX-License-Identifier: Apache-2.0
"""SQLite destination connector.

Tables are created lazily from the records written to them (TEXT-affinity
columns; complex values JSON-encoded), and widened with ``ALTER TABLE`` when
new fields appear. Writes honor the identity fields and conflict policy from
:class:`ferry.connector.WriteOptions`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ferry.connector import (
    ConfigurationError,
    ConnectorRegistration,
    Credentials,
    DataError,
    FieldSchema,
    Inventory,
    ObjectSchema,
    RelationshipWrite,
    RelationshipWriteResult,
    WriteOptions,
    WriteResult,
)

_IDENTIFIER = re.compile(r"[^a-z0-9_]+")


def _identifier(value: str, *, kind: str) -> str:
    normalized = _IDENTIFIER.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        raise DataError(f"cannot derive a SQLite {kind} name from {value!r}")
    if normalized[0].isdigit():
        normalized = f"t_{normalized}"
    return normalized


class SqliteDestination:
    provider = "sqlite"
    binding_kind = "database"

    def __init__(self) -> None:
        self._connections: dict[str, sqlite3.Connection] = {}

    def automatic_target_object(self, canonical_type: str) -> str | None:
        return _identifier(canonical_type, kind="table")

    async def inventory(
        self,
        credentials: Credentials,
        *,
        canonical_types: set[str],
    ) -> Inventory:
        # Inventory is a read: a database that does not exist yet is simply
        # empty — never create the file from an inspection/planning path.
        if not self._db_path(credentials).exists():
            return Inventory(provider=self.provider, connection_id=credentials.connection_id)
        connection = self._connect(credentials)
        objects: list[ObjectSchema] = []
        for canonical_type in sorted(canonical_types):
            table = _identifier(canonical_type, kind="table")
            if not self._table_exists(connection, table):
                continue
            count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            objects.append(
                ObjectSchema(
                    key=table,
                    label=table,
                    canonical_type=canonical_type,
                    record_count=int(count),
                    fields=[
                        FieldSchema(key=str(column[1]), label=str(column[1])) for column in columns
                    ],
                )
            )
        return Inventory(
            provider=self.provider, connection_id=credentials.connection_id, objects=objects
        )

    async def write_record(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        if not properties:
            return WriteResult(status="skipped", message="empty record")
        connection = self._connect(credentials)
        table = _identifier(object_type, kind="table")
        columns = {
            _identifier(key, kind="column"): _to_sql(value) for key, value in properties.items()
        }
        self._ensure_table(connection, table, columns.keys())

        identity_columns = [
            _identifier(field, kind="column") for field in (options.identity_fields or [])
        ]
        existing_rowid = self._find_existing(connection, table, identity_columns, columns)

        if existing_rowid is not None and options.conflict_policy == "skip_existing":
            return WriteResult(status="skipped", destination_record_id=str(existing_rowid))
        if existing_rowid is not None and options.conflict_policy == "update_existing":
            assignments = ", ".join(f'"{name}" = ?' for name in columns)
            connection.execute(
                f'UPDATE "{table}" SET {assignments} WHERE rowid = ?',
                (*columns.values(), existing_rowid),
            )
            connection.commit()
            return WriteResult(status="updated", destination_record_id=str(existing_rowid))

        names = ", ".join(f'"{name}"' for name in columns)
        placeholders = ", ".join("?" for _ in columns)
        inserted = connection.execute(
            f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
            tuple(columns.values()),
        )
        connection.commit()
        return WriteResult(status="created", destination_record_id=str(inserted.lastrowid))

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        return RelationshipWriteResult(
            status="skipped", message="sqlite destination does not model relationships yet"
        )

    # -- internals ----------------------------------------------------------

    def _db_path(self, credentials: Credentials) -> Path:
        raw = credentials.settings.get("connection") or credentials.settings.get("path")
        if not raw:
            raise ConfigurationError("sqlite destination needs a database path (connection)")
        path_text = str(raw)
        for prefix in ("sqlite:///", "sqlite://"):
            if path_text.startswith(prefix):
                path_text = path_text[len(prefix) :]
                break
        if not path_text:
            raise ConfigurationError("sqlite destination path is empty")
        return Path(path_text).expanduser()

    def _connect(self, credentials: Credentials) -> sqlite3.Connection:
        key = str(self._db_path(credentials))
        connection = self._connections.get(key)
        if connection is None:
            Path(key).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(key)
            self._connections[key] = connection
        return connection

    def _table_exists(self, connection: sqlite3.Connection, table: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    def _ensure_table(self, connection: sqlite3.Connection, table: str, columns: Any) -> None:
        column_list = list(columns)
        if not self._table_exists(connection, table):
            rendered = ", ".join(f'"{name}"' for name in column_list)
            connection.execute(f'CREATE TABLE "{table}" ({rendered})')
            connection.commit()
            return
        existing = {
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        for name in column_list:
            if name not in existing:
                connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}"')
        connection.commit()

    def _find_existing(
        self,
        connection: sqlite3.Connection,
        table: str,
        identity_columns: list[str],
        columns: dict[str, Any],
    ) -> int | None:
        usable = [name for name in identity_columns if name in columns]
        if not usable:
            return None
        predicate = " AND ".join(f'"{name}" = ?' for name in usable)
        row = connection.execute(
            f'SELECT rowid FROM "{table}" WHERE {predicate} LIMIT 1',
            tuple(columns[name] for name in usable),
        ).fetchone()
        return None if row is None else int(row[0])


def _to_sql(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if value is None or isinstance(value, int | float | str):
        return value
    return json.dumps(value, ensure_ascii=False)


CONNECTOR = ConnectorRegistration(name="sqlite", destination=SqliteDestination())
