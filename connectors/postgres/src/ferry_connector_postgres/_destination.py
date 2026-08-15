# SPDX-License-Identifier: Apache-2.0
"""PostgreSQL destination connector.

Tables are created lazily from the records written to them, with column types
taken from the first-seen Python value (bool → boolean, int → bigint, float →
double precision, dict/list → jsonb, everything else → text) and widened with
``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` as new fields appear. When a later
value cannot be represented in a column this destination minted (mixed-type
source fields), the column is promoted up the boolean → bigint → double
precision → text ladder with ``ALTER COLUMN … TYPE … USING`` instead of
failing the run — SQLite's everything-fits semantics on a typed store, without
losing rows already written. Identity columns get a
``CREATE UNIQUE INDEX IF NOT EXISTS``. Writes honor the identity fields and
conflict policy from :class:`ferry.connector.WriteOptions` via an identity
pre-SELECT, mirroring the sqlite connector.

``destination_record_id`` is the identity value as a string when exactly one
identity field is present in the record, else ``None`` — PostgreSQL has no
stable rowid (``ctid`` moves on update/vacuum), and the identity value is what
the run's ledger can re-find later.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Final

import psycopg
from psycopg import sql
from psycopg.types.json import Json

from ferry.connector import (
    Credentials,
    FieldSchema,
    Inventory,
    ObjectSchema,
    RelationshipWrite,
    RelationshipWriteResult,
    WriteOptions,
    WriteResult,
)
from ferry_connector_postgres._base import (
    PostgresConnectorBase,
    identifier,
    pg_errors,
)

_NUMERIC_COLUMN_TYPES: Final = frozenset(
    {"smallint", "integer", "bigint", "real", "double precision", "numeric", "decimal"}
)
_JSON_COLUMN_TYPES: Final = frozenset({"json", "jsonb"})
_MAX_INDEX_NAME_LENGTH = 63

#: Literals PostgreSQL accepts for boolean input (lowercased).
_BOOLEAN_LITERALS: Final = frozenset(
    {"true", "false", "t", "f", "yes", "no", "y", "n", "on", "off", "1", "0"}
)


def _column_type(value: Any) -> str:
    """Column type for a first-seen Python value (``None`` → text)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "bigint"
    if isinstance(value, float):
        return "double precision"
    if isinstance(value, dict | list):
        return "jsonb"
    return "text"


def _text_form(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _write_param(column_type: str, value: Any) -> Any:
    """Adapt a value to the actual column type.

    json/jsonb columns take every non-NULL value JSON-encoded (``Json`` wraps
    scalars too, so a plain string becomes a valid JSON string). Strings are
    sent with unknown OID and cast by the server into any column type. Other
    Python types are passed natively when the column matches and stringified
    client-side otherwise — PostgreSQL has no implicit casts into text.
    """
    if value is None:
        return None
    lowered = column_type.lower()
    if lowered in _JSON_COLUMN_TYPES:
        return Json(value)
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        if lowered == "boolean":
            return value
        if lowered in _NUMERIC_COLUMN_TYPES:
            return int(value)
        return _text_form(value)
    if isinstance(value, int | float):
        if lowered in _NUMERIC_COLUMN_TYPES:
            return value
        return _text_form(value)
    return _text_form(value)


def _index_name(table: str, identity_columns: list[str]) -> str:
    base = f"{table}_{'_'.join(identity_columns)}_ferry_uq"
    if len(base) <= _MAX_INDEX_NAME_LENGTH:
        return base
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    return f"{base[: _MAX_INDEX_NAME_LENGTH - 9]}_{digest}"


def _parses(text: str, kind: type[int] | type[float]) -> bool:
    try:
        kind(text)
    except ValueError:
        return False
    return True


def _required_type(current: str, value: Any) -> str | None:
    """The minted type a column must be promoted to so ``value`` fits.

    ``None`` means the value is representable in the current type. Promotion
    walks the ladder boolean → bigint → double precision → text (jsonb and
    text absorb everything), mirroring SQLite's everything-fits semantics on a
    typed store without losing data already written. Only the five types this
    destination mints are ever promoted; values headed for other pre-existing
    column types are server-cast and surface an error when they do not fit.
    """
    lowered = current.lower()
    if value is None or lowered in _JSON_COLUMN_TYPES or lowered == "text":
        return None
    if lowered == "boolean":
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            return None if value.strip().lower() in _BOOLEAN_LITERALS else "text"
        if isinstance(value, int):
            return "bigint"
        if isinstance(value, float):
            return "double precision"
        return "text"
    if lowered == "bigint":
        if isinstance(value, bool | int):
            return None
        if isinstance(value, float):
            return "double precision"
        if isinstance(value, str):
            if _parses(value, int):
                return None
            return "double precision" if _parses(value, float) else "text"
        return "text"
    if lowered == "double precision":
        if isinstance(value, bool | int | float):
            return None
        if isinstance(value, str):
            return None if _parses(value, float) else "text"
        return "text"
    return None  # not a type this destination minted; leave it alone


def _promotion_cast(current: str, target: str, column: sql.Identifier) -> sql.Composable:
    if current.lower() == "boolean" and target != "text":
        # No direct boolean → numeric cast; go through int.
        return sql.SQL("{column}::int::{type}").format(column=column, type=sql.SQL(target))
    return sql.SQL("{column}::{type}").format(column=column, type=sql.SQL(target))


class PostgresDestination(PostgresConnectorBase):
    def __init__(self) -> None:
        super().__init__()
        self._schemas_ensured: set[tuple[str, str]] = set()
        self._table_columns: dict[tuple[str, str, str], dict[str, str]] = {}
        self._identity_indexed: set[tuple[str, str, str]] = set()

    def automatic_target_object(self, canonical_type: str) -> str | None:
        return identifier(canonical_type, kind="table")

    async def inventory(
        self,
        credentials: Credentials,
        *,
        canonical_types: set[str],
    ) -> Inventory:
        async with pg_errors("postgres destination: inventory"):
            schema = self._schema_name(credentials)
            connection = await self._connection(credentials)
            existing = set(await self._tables(connection, schema))
            objects: list[ObjectSchema] = []
            for canonical_type in sorted(canonical_types):
                table = identifier(canonical_type, kind="table")
                if table not in existing:
                    continue
                count_cursor = await connection.execute(
                    sql.SQL("SELECT COUNT(*) FROM {table}").format(
                        table=sql.Identifier(schema, table)
                    )
                )
                count_row = await count_cursor.fetchone()
                columns = await self._columns(connection, schema, table)
                objects.append(
                    ObjectSchema(
                        key=table,
                        label=table,
                        canonical_type=canonical_type,
                        record_count=int(count_row[0]) if count_row is not None else 0,
                        fields=[FieldSchema(key=name, label=name) for name, _ in columns],
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
        async with pg_errors(f"postgres destination: write into {object_type!r}"):
            dsn = self._dsn(credentials)
            schema = self._schema_name(credentials)
            table = identifier(object_type, kind="table")
            connection = await self._connection(credentials)
            try:
                return await self._write(
                    connection,
                    dsn=dsn,
                    schema=schema,
                    table=table,
                    properties=properties,
                    options=options,
                )
            except psycopg.Error as error:
                if error.sqlstate in ("42P01", "42703"):
                    # Undefined table/column race: another actor changed the
                    # schema under us. Drop the cache so a retry re-ensures.
                    self._invalidate(dsn, schema, table)
                raise

    async def _write(
        self,
        connection: psycopg.AsyncConnection[Any],
        *,
        dsn: str,
        schema: str,
        table: str,
        properties: dict[str, Any],
        options: WriteOptions,
    ) -> WriteResult:
        values = {identifier(key, kind="column"): value for key, value in properties.items()}
        identity_columns = [
            identifier(field, kind="column") for field in (options.identity_fields or [])
        ]
        column_types = await self._ensure_table(
            connection, dsn, schema, table, values, identity_columns
        )
        for name, value in values.items():
            current = column_types.get(name, "text")
            target = _required_type(current, value)
            if target is not None:
                await connection.execute(
                    sql.SQL(
                        "ALTER TABLE {table} ALTER COLUMN {column} TYPE {type} USING {cast}"
                    ).format(
                        table=sql.Identifier(schema, table),
                        column=sql.Identifier(name),
                        type=sql.SQL(target),  # closed set, not user input
                        cast=_promotion_cast(current, target, sql.Identifier(name)),
                    )
                )
                column_types[name] = target  # mutates the cached map
        params = {
            name: _write_param(column_types.get(name, "text"), value)
            for name, value in values.items()
        }
        usable = [name for name in identity_columns if name in values]
        identity_text = str(values[usable[0]]) if len(usable) == 1 else None
        table_ident = sql.Identifier(schema, table)

        exists = (
            bool(usable)
            and options.conflict_policy != "create"
            and await self._exists(connection, table_ident, usable, params)
        )
        if exists and options.conflict_policy == "skip_existing":
            return WriteResult(status="skipped", destination_record_id=identity_text)
        if exists and options.conflict_policy == "update_existing":
            assignments = sql.SQL(", ").join(
                sql.SQL("{column} = %s").format(column=sql.Identifier(name)) for name in values
            )
            predicate = sql.SQL(" AND ").join(
                sql.SQL("{column} = %s").format(column=sql.Identifier(name)) for name in usable
            )
            await connection.execute(
                sql.SQL("UPDATE {table} SET {assignments} WHERE {predicate}").format(
                    table=table_ident, assignments=assignments, predicate=predicate
                ),
                [*(params[name] for name in values), *(params[name] for name in usable)],
            )
            return WriteResult(status="updated", destination_record_id=identity_text)

        await connection.execute(
            sql.SQL("INSERT INTO {table} ({columns}) VALUES ({placeholders})").format(
                table=table_ident,
                columns=sql.SQL(", ").join(sql.Identifier(name) for name in values),
                placeholders=sql.SQL(", ").join(sql.Placeholder() for _ in values),
            ),
            [params[name] for name in values],
        )
        return WriteResult(status="created", destination_record_id=identity_text)

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        return RelationshipWriteResult(
            status="skipped", message="postgres destination does not model relationships yet"
        )

    # -- internals ----------------------------------------------------------

    async def _ensure_table(
        self,
        connection: psycopg.AsyncConnection[Any],
        dsn: str,
        schema: str,
        table: str,
        values: dict[str, Any],
        identity_columns: list[str],
    ) -> dict[str, str]:
        """Create/widen the table as needed; return the column → type map.

        The map is cached per (DSN, schema, table) to keep the per-record cost
        at plain DML. If another actor drops the table or a column mid-run,
        the resulting UndefinedTable/UndefinedColumn surfaces as a DataError
        and the cache entry is invalidated for the next attempt.
        """
        schema_key = (dsn, schema)
        if schema_key not in self._schemas_ensured:
            await connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(
                    schema=sql.Identifier(schema)
                )
            )
            self._schemas_ensured.add(schema_key)

        table_key = (dsn, schema, table)
        known = self._table_columns.get(table_key)
        if known is None:
            known = dict(await self._columns(connection, schema, table))
            if not known:
                await connection.execute(
                    sql.SQL("CREATE TABLE IF NOT EXISTS {table} ({columns})").format(
                        table=sql.Identifier(schema, table),
                        columns=sql.SQL(", ").join(
                            sql.SQL("{column} {type}").format(
                                column=sql.Identifier(name),
                                type=sql.SQL(_column_type(value)),  # closed set, not user input
                            )
                            for name, value in values.items()
                        ),
                    )
                )
                known = {name: _column_type(value) for name, value in values.items()}
            self._table_columns[table_key] = known

        for name, value in values.items():
            if name in known:
                continue
            column_type = _column_type(value)
            await connection.execute(
                sql.SQL("ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {type}").format(
                    table=sql.Identifier(schema, table),
                    column=sql.Identifier(name),
                    type=sql.SQL(column_type),
                )
            )
            known[name] = column_type

        if (
            identity_columns
            and table_key not in self._identity_indexed
            and all(name in known for name in identity_columns)
        ):
            await connection.execute(
                sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {table} ({columns})").format(
                    index=sql.Identifier(_index_name(table, identity_columns)),
                    table=sql.Identifier(schema, table),
                    columns=sql.SQL(", ").join(sql.Identifier(name) for name in identity_columns),
                )
            )
            self._identity_indexed.add(table_key)
        return known

    def _invalidate(self, dsn: str, schema: str, table: str) -> None:
        table_key = (dsn, schema, table)
        self._table_columns.pop(table_key, None)
        self._identity_indexed.discard(table_key)
        self._schemas_ensured.discard((dsn, schema))

    async def _exists(
        self,
        connection: psycopg.AsyncConnection[Any],
        table_ident: sql.Identifier,
        usable: list[str],
        params: dict[str, Any],
    ) -> bool:
        predicate = sql.SQL(" AND ").join(
            sql.SQL("{column} = %s").format(column=sql.Identifier(name)) for name in usable
        )
        cursor = await connection.execute(
            sql.SQL("SELECT 1 FROM {table} WHERE {predicate} LIMIT 1").format(
                table=table_ident, predicate=predicate
            ),
            [params[name] for name in usable],
        )
        return await cursor.fetchone() is not None


if TYPE_CHECKING:
    from ferry.connector import DestinationConnector

    _protocol_destination: DestinationConnector = PostgresDestination()
