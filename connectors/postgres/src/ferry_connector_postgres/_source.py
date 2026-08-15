# SPDX-License-Identifier: Apache-2.0
"""PostgreSQL source connector.

Reads BASE TABLEs from one schema. Identity is the single-column primary key;
tables with a composite, missing, or bytea primary key are inventoried with a
warning and no identity fields so the planner skips them. ``read_records``
keyset-paginates on the primary key (``WHERE pk > cursor ORDER BY pk``), and
:class:`ferry.connector.SupportsSnapshotBounds` freezes a run's scope at
``MAX(pk)``. Cursors and bounds travel as JSON-safe strings; see
``_base.cursor_param`` for how they are cast back.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg import sql

from ferry.connector import (
    Credentials,
    DataError,
    FieldSchema,
    Inventory,
    ObjectSchema,
    RecordPage,
    SourceFilter,
    SourceObject,
    UnsupportedFeatureError,
)
from ferry_connector_postgres._base import (
    SKIPPED_BINARY,
    PostgresConnectorBase,
    cursor_param,
    cursor_text,
    field_family,
    is_binary,
    is_temporal,
    json_safe,
    pg_errors,
)


class PostgresSource(PostgresConnectorBase):
    def __init__(self) -> None:
        super().__init__()
        self._binary_warned: set[tuple[str, str]] = set()

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        async with pg_errors("postgres source: discover objects"):
            schema = self._schema_name(credentials)
            connection = await self._connection(credentials)
            tables = await self._tables(connection, schema)
        return [
            SourceObject(key=table, label=table, canonical_type=table, default_selected=True)
            for table in tables
        ]

    async def inventory(
        self,
        credentials: Credentials,
        *,
        object_types: list[str] | None = None,
    ) -> Inventory:
        async with pg_errors("postgres source: inventory"):
            schema = self._schema_name(credentials)
            connection = await self._connection(credentials)
            tables = await self._tables(connection, schema)
            inventory_warnings: list[str] = []
            if object_types is not None:
                requested = list(dict.fromkeys(object_types))
                available = set(tables)
                inventory_warnings.extend(
                    f"table {name!r} not found in schema {schema!r}; skipped"
                    for name in requested
                    if name not in available
                )
                tables = [name for name in requested if name in available]

            objects: list[ObjectSchema] = []
            for table in tables:
                columns = await self._columns(connection, schema, table)
                primary_key = await self._primary_key_columns(connection, schema, table)
                identity = self._identity_fields(table, primary_key, inventory_warnings)
                fields: list[FieldSchema] = []
                for name, data_type in columns:
                    if is_binary(data_type):
                        inventory_warnings.append(
                            f"binary column {table}.{name} ({data_type}) is excluded from migration"
                        )
                        continue
                    fields.append(
                        FieldSchema(
                            key=name,
                            label=name,
                            data_type=field_family(data_type),
                            unique=identity == [name],
                            metadata={"pg_type": data_type} if is_temporal(data_type) else {},
                        )
                    )
                count_cursor = await connection.execute(
                    sql.SQL("SELECT COUNT(*) FROM {table}").format(
                        table=sql.Identifier(schema, table)
                    )
                )
                count_row = await count_cursor.fetchone()
                objects.append(
                    ObjectSchema(
                        key=table,
                        label=table,
                        canonical_type=table,
                        record_count=int(count_row[0]) if count_row is not None else 0,
                        fields=fields,
                        identity_fields=identity,
                    )
                )
        return Inventory(
            provider=self.provider,
            connection_id=credentials.connection_id,
            objects=objects,
            warnings=inventory_warnings,
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
        _reject_filter(source_filter)
        return await self._page(
            credentials,
            object_type=object_type,
            field_keys=field_keys,
            limit=limit,
            cursor=cursor,
            upper_bound=None,
        )

    # -- capabilities -------------------------------------------------------

    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int:
        _reject_filter(source_filter)
        return await self._count(credentials, object_type=object_type, upper_bound=None)

    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None:
        _reject_filter(source_filter)
        async with pg_errors(f"postgres source: high-water mark for {object_type!r}"):
            schema = self._schema_name(credentials)
            connection = await self._connection(credentials)
            key_column, _ = await self._pagination_key(connection, schema, object_type)
            cursor = await connection.execute(
                sql.SQL("SELECT MAX({key}) FROM {table}").format(
                    key=sql.Identifier(key_column),
                    table=sql.Identifier(schema, object_type),
                )
            )
            row = await cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return cursor_text(row[0])

    async def read_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None = None,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> RecordPage:
        _reject_filter(source_filter)
        return await self._page(
            credentials,
            object_type=object_type,
            field_keys=field_keys,
            limit=limit,
            cursor=cursor,
            upper_bound=upper_bound,
        )

    async def count_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> int:
        _reject_filter(source_filter)
        return await self._count(credentials, object_type=object_type, upper_bound=upper_bound)

    # -- internals ----------------------------------------------------------

    def _identity_fields(
        self,
        table: str,
        primary_key: list[tuple[str, str]],
        inventory_warnings: list[str],
    ) -> list[str]:
        if not primary_key:
            inventory_warnings.append(
                f"table {table!r} has no primary key; records cannot be identified"
                " and the planner will skip it"
            )
            return []
        if len(primary_key) > 1:
            rendered = ", ".join(name for name, _ in primary_key)
            inventory_warnings.append(
                f"table {table!r} has a composite primary key ({rendered}); records"
                " cannot be identified and the planner will skip it"
            )
            return []
        name, data_type = primary_key[0]
        if is_binary(data_type):
            inventory_warnings.append(
                f"table {table!r} has a binary ({data_type}) primary key; records"
                " cannot be identified and the planner will skip it"
            )
            return []
        return [name]

    async def _pagination_key(
        self, connection: psycopg.AsyncConnection[Any], schema: str, table: str
    ) -> tuple[str, str]:
        primary_key = await self._primary_key_columns(connection, schema, table)
        if len(primary_key) != 1 or is_binary(primary_key[0][1]):
            raise DataError(
                f"table {table!r} in schema {schema!r} has no usable single-column"
                " primary key (or does not exist); cannot keyset-paginate"
            )
        return primary_key[0]

    async def _page(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        field_keys: list[str],
        limit: int,
        cursor: str | None,
        upper_bound: str | None,
    ) -> RecordPage:
        async with pg_errors(f"postgres source: read {object_type!r}"):
            schema = self._schema_name(credentials)
            connection = await self._connection(credentials)
            key_column, key_type = await self._pagination_key(connection, schema, object_type)

            select_keys = list(dict.fromkeys(field_keys))
            if key_column not in select_keys:
                select_keys.append(key_column)
            key_index = select_keys.index(key_column)
            page_size = max(1, limit)

            query = sql.SQL("SELECT {columns} FROM {table}").format(
                columns=sql.SQL(", ").join(sql.Identifier(key) for key in select_keys),
                table=sql.Identifier(schema, object_type),
            )
            clauses: list[sql.Composable] = []
            params: list[Any] = []
            if cursor is not None:
                clauses.append(sql.SQL("{key} > %s").format(key=sql.Identifier(key_column)))
                params.append(cursor_param(key_type, cursor))
            if upper_bound is not None:
                clauses.append(sql.SQL("{key} <= %s").format(key=sql.Identifier(key_column)))
                params.append(cursor_param(key_type, upper_bound))
            if clauses:
                query = query + sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
            query = query + sql.SQL(" ORDER BY {key} LIMIT %s").format(
                key=sql.Identifier(key_column)
            )
            params.append(page_size)

            result = await connection.execute(query, params)
            rows = await result.fetchall()

        records: list[dict[str, Any]] = []
        for row in rows:
            full: dict[str, Any] = {}
            for key, value in zip(select_keys, row, strict=True):
                converted = json_safe(value)
                if converted is SKIPPED_BINARY:
                    self._warn_binary(object_type, key)
                    continue
                full[key] = converted
            records.append({key: full[key] for key in field_keys if key in full})

        has_more = len(rows) == page_size
        next_cursor = cursor_text(rows[-1][key_index]) if has_more and rows else None
        return RecordPage(
            object_key=object_type,
            records=records,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def _count(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        upper_bound: str | None,
    ) -> int:
        async with pg_errors(f"postgres source: count {object_type!r}"):
            schema = self._schema_name(credentials)
            connection = await self._connection(credentials)
            query = sql.SQL("SELECT COUNT(*) FROM {table}").format(
                table=sql.Identifier(schema, object_type)
            )
            params: list[Any] = []
            if upper_bound is not None:
                key_column, key_type = await self._pagination_key(connection, schema, object_type)
                query = query + sql.SQL(" WHERE {key} <= %s").format(key=sql.Identifier(key_column))
                params.append(cursor_param(key_type, upper_bound))
            result = await connection.execute(query, params)
            row = await result.fetchone()
        return int(row[0]) if row is not None else 0

    def _warn_binary(self, table: str, column: str) -> None:
        key = (table, column)
        if key in self._binary_warned:
            return
        self._binary_warned.add(key)
        warnings.warn(
            f"postgres source: skipping binary value(s) in {table}.{column}"
            " (bytea does not migrate)",
            stacklevel=4,
        )


def _reject_filter(source_filter: SourceFilter | None) -> None:
    if source_filter is not None:
        raise UnsupportedFeatureError(
            "postgres source does not support source filters yet; narrow the"
            " source with a view instead"
        )


if TYPE_CHECKING:
    from ferry.connector import SourceConnector, SupportsRecordCounts, SupportsSnapshotBounds

    _protocol_source: SourceConnector = PostgresSource()
    _protocol_counts: SupportsRecordCounts = PostgresSource()
    _protocol_bounds: SupportsSnapshotBounds = PostgresSource()
