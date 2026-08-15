# SPDX-License-Identifier: Apache-2.0
"""SQLite source and destination connector.

As a source, every user table is a migratable object: fields come from
``PRAGMA table_info``, the identity is the table's single-column primary key
(else ``rowid``, exposed as an extra field), and reads use keyset pagination
(``WHERE <pk> > ? ORDER BY <pk>``) so cursors are deterministic and resumable.

As a destination, tables are created lazily from the records written to them
(TEXT-affinity columns; complex values JSON-encoded), and widened with
``ALTER TABLE`` when new fields appear. Writes honor the identity fields and
conflict policy from :class:`ferry.connector.WriteOptions`.
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
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    SourceFilter,
    SourceObject,
    UnsupportedFeatureError,
    WriteOptions,
    WriteResult,
)

_IDENTIFIER = re.compile(r"[^a-z0-9_]+")
_ROWID = "rowid"


def _identifier(value: str, *, kind: str) -> str:
    normalized = _IDENTIFIER.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        raise DataError(f"cannot derive a SQLite {kind} name from {value!r}")
    if normalized[0].isdigit():
        normalized = f"t_{normalized}"
    return normalized


def _quote(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _database_path(credentials: Credentials, *, role: str) -> str:
    raw = credentials.settings.get("connection") or credentials.settings.get("path")
    if not raw:
        raise ConfigurationError(f"sqlite {role} needs a database path (connection)")
    path_text = str(raw)
    for prefix in ("sqlite:///", "sqlite://"):
        if path_text.startswith(prefix):
            path_text = path_text[len(prefix) :]
            break
    if not path_text:
        raise ConfigurationError(f"sqlite {role} path is empty")
    return str(Path(path_text).expanduser())


def _column_type(declared: str) -> str:
    upper = declared.upper()
    if "BOOL" in upper:
        return "boolean"
    if "INT" in upper or any(marker in upper for marker in ("REAL", "FLOA", "DOUB")):
        return "number"
    return "string"


class _SqliteConnectionCache:
    """Per-instance connection cache shared by the source and destination roles."""

    def __init__(self) -> None:
        self._connections: dict[str, sqlite3.Connection] = {}

    def _cached_connection(self, key: str) -> sqlite3.Connection:
        connection = self._connections.get(key)
        if connection is None:
            connection = sqlite3.connect(key)
            self._connections[key] = connection
        return connection


class SqliteSource(_SqliteConnectionCache):
    provider = "sqlite"
    binding_kind = "database"

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        connection = self._connect(credentials)
        return [
            SourceObject(key=table, label=table, canonical_type=table, default_selected=True)
            for table in self._tables(connection)
        ]

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        connection = self._connect(credentials)
        tables = self._tables(connection)
        if object_types is not None:
            requested = set(object_types)
            tables = [table for table in tables if table in requested]

        objects: list[ObjectSchema] = []
        warnings: list[str] = []
        for table in tables:
            columns = self._columns(connection, table)
            fields = [
                FieldSchema(
                    key=str(column[1]),
                    label=str(column[1]),
                    data_type=_column_type(str(column[2])),
                )
                for column in columns
            ]
            identity = self._identity_column(connection, table, columns)
            identity_fields: list[str] = []
            if identity == _ROWID:
                fields.append(FieldSchema(key=_ROWID, label=_ROWID, data_type="number"))
                identity_fields = [_ROWID]
            elif identity is not None:
                identity_fields = [identity]
            else:
                warnings.append(
                    f"table {table!r} has neither a single-column primary key nor a rowid;"
                    " no identity field is available"
                )
            count = connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
            objects.append(
                ObjectSchema(
                    key=table,
                    label=table,
                    canonical_type=table,
                    record_count=int(count),
                    fields=fields,
                    identity_fields=identity_fields,
                )
            )
        return Inventory(
            provider=self.provider,
            connection_id=credentials.connection_id,
            objects=objects,
            warnings=warnings,
        )

    async def read_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None = None,
        source_filter: SourceFilter | None = None,
    ) -> RecordPage:
        connection = self._connect(credentials)
        self._require_table(connection, object_type)
        columns = self._columns(connection, object_type)
        declared = {str(column[1]) for column in columns}
        key_column = self._identity_column(connection, object_type, columns)
        if key_column is None:
            raise UnsupportedFeatureError(
                f"table {object_type!r} has neither a single-column primary key nor a rowid;"
                " keyset pagination is unavailable",
                remediation="add a single-column primary key to the table",
            )

        select_columns = [key for key in field_keys if key in declared or key == key_column]
        if key_column not in select_columns:
            select_columns.append(key_column)
        rendered = ", ".join(
            f"{_ROWID} AS {_ROWID}" if name == _ROWID and name not in declared else _quote(name)
            for name in select_columns
        )
        key_reference = (
            _ROWID if key_column == _ROWID and key_column not in declared else _quote(key_column)
        )

        page_size = max(1, limit)
        sql = f"SELECT {rendered} FROM {_quote(object_type)}"
        parameters: list[Any] = []
        if cursor is not None:
            sql += f" WHERE {key_reference} > ?"
            parameters.append(cursor)
        sql += f" ORDER BY {key_reference} LIMIT ?"
        parameters.append(page_size + 1)

        rows = connection.execute(sql, parameters).fetchall()
        has_more = len(rows) > page_size
        records: list[dict[str, Any]] = []
        last_key: Any = None
        for row in rows[:page_size]:
            values = dict(zip(select_columns, row, strict=True))
            last_key = values[key_column]
            if last_key is None:
                raise DataError(
                    f"table {object_type!r} has a NULL value in identity column "
                    f"{key_column!r}; keyset pagination requires non-NULL keys"
                )
            records.append({key: values.get(key) for key in field_keys})
        return RecordPage(
            object_key=object_type,
            records=records,
            next_cursor=str(last_key) if has_more else None,
            has_more=has_more,
        )

    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int:
        connection = self._connect(credentials)
        self._require_table(connection, object_type)
        row = connection.execute(f"SELECT COUNT(*) FROM {_quote(object_type)}").fetchone()
        return int(row[0])

    # -- internals ----------------------------------------------------------

    def _connect(self, credentials: Credentials) -> sqlite3.Connection:
        key = _database_path(credentials, role="source")
        if key not in self._connections and not Path(key).is_file():
            raise ConfigurationError(f"sqlite source database not found: {key}")
        return self._cached_connection(key)

    def _tables(self, connection: sqlite3.Connection) -> list[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _require_table(self, connection: sqlite3.Connection, object_type: str) -> None:
        if object_type not in self._tables(connection):
            raise DataError(f"sqlite source has no table {object_type!r}")

    def _columns(self, connection: sqlite3.Connection, table: str) -> list[Any]:
        return connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()

    def _identity_column(
        self, connection: sqlite3.Connection, table: str, columns: list[Any]
    ) -> str | None:
        primary_key = [str(column[1]) for column in columns if int(column[5]) > 0]
        if len(primary_key) == 1:
            return primary_key[0]
        if self._has_rowid(connection, table):
            return _ROWID
        return None

    def _has_rowid(self, connection: sqlite3.Connection, table: str) -> bool:
        try:
            connection.execute(f"SELECT {_ROWID} FROM {_quote(table)} LIMIT 1")
        except sqlite3.OperationalError:
            return False
        return True


class SqliteDestination(_SqliteConnectionCache):
    provider = "sqlite"
    binding_kind = "database"

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
        if not Path(_database_path(credentials, role="destination")).exists():
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

    def _connect(self, credentials: Credentials) -> sqlite3.Connection:
        key = _database_path(credentials, role="destination")
        if key not in self._connections:
            Path(key).parent.mkdir(parents=True, exist_ok=True)
        return self._cached_connection(key)

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


CONNECTOR = ConnectorRegistration(
    name="sqlite", source=SqliteSource(), destination=SqliteDestination()
)
