# SPDX-License-Identifier: Apache-2.0
"""ClickHouse destination connector.

Tables are created lazily on first write. Column types are inferred from the
first non-``None`` value seen per field (``bool`` → ``Bool``, ``int`` →
``Int64``, ``float`` → ``Float64``, ``dict``/``list`` → ``String`` storing
JSON text, everything else → ``String``; all-``None`` columns default to
``String``). Every column is ``Nullable`` **except** identity columns —
ClickHouse forbids ``Nullable`` ORDER BY columns. Fields that appear later are
added with ``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` as ``Nullable``.

Engine choice is deliberate: when the run's
:class:`sanka.connector.WriteOptions` declare identity fields, tables are
created as ``ReplacingMergeTree ORDER BY (<identity columns>)``. Sanka Migrate's
engine guarantees at-least-once writes reconciled against an identity ledger,
so a re-applied migration inserts a fresh *version* of each row rather than
mutating in place — exactly the contract ReplacingMergeTree implements by
deduplicating rows that share an ORDER BY key at merge time. Without identity
fields, tables fall back to ``MergeTree ORDER BY tuple()``.

ClickHouse has no per-row upsert, so ``conflict_policy`` is effectively
ignored beyond that merge-time dedup: every successfully inserted row honestly
reports status ``created`` (never ``updated``/``skipped``), and duplicate
versions collapse later inside ClickHouse. For the same reason ``inventory``
counts with ``SELECT count() … FINAL``: a raw ``count()`` overcounts unmerged
versions and is not valid parity evidence. ``FINAL`` is guarded by an engine
readback because ClickHouse (observed on 24.8) rejects it on engines without
merge-time collapse — plain ``MergeTree`` included — with ``ILLEGAL_FINAL``.

Connections come from ``settings["connection"]`` as ``http://host:8123/db``,
``https://host:8443/db``, or ``clickhouse://host:8123/db`` (treated as HTTP).
The database defaults to ``default`` and must already exist. Credentials come
from the URL userinfo or ``settings["username"]``/``settings["password"]``
(URL wins per field). One client is cached per normalized connection target;
the synchronous ``clickhouse-connect`` client runs via ``asyncio.to_thread``
so the async SPI never blocks the event loop, and clients are created without
a server session so concurrent worker threads never contend on a session lock.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import unquote, urlsplit

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import InterfaceError, OperationalError

from sanka.connector import (
    AuthenticationError,
    BatchWriteInput,
    BatchWriteResult,
    ConfigurationError,
    ConnectorError,
    ConnectorRegistration,
    Credentials,
    DataError,
    FieldSchema,
    Inventory,
    ObjectSchema,
    RelationshipWrite,
    RelationshipWriteResult,
    TransientProviderError,
    WriteOptions,
    WriteResult,
)

_T = TypeVar("_T")

_IDENTIFIER = re.compile(r"[^a-z0-9_]+")
_ERROR_CODE = re.compile(r"Code:\s*(\d+)")

# scheme → clickhouse-connect interface; clickhouse:// is the HTTP protocol.
_SCHEME_INTERFACES = {"http": "http", "https": "https", "clickhouse": "http"}

# ClickHouse server error codes, by how the engine should react.
_AUTH_ERROR_CODES = frozenset({192, 193, 194, 497, 516})  # unknown user … authentication failed
_MISSING_SCHEMA_ERROR_CODES = frozenset({16, 47, 60})  # no such column / identifier / table
_TRANSIENT_ERROR_CODES = frozenset({159, 202, 209})  # timeouts and too-many-queries

# Engines whose FINAL modifier collapses row versions. Everything else — plain
# MergeTree included — rejects FINAL with ILLEGAL_FINAL (observed on 24.8), so
# inventory must not apply it there.
_FINAL_ENGINES = frozenset(
    {
        "AggregatingMergeTree",
        "CollapsingMergeTree",
        "GraphiteMergeTree",
        "ReplacingMergeTree",
        "SummingMergeTree",
        "VersionedCollapsingMergeTree",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _ConnectionTarget:
    """Normalized connection parameters; doubles as the client cache key."""

    interface: str
    host: str
    port: int
    database: str
    username: str
    password: str


def _parse_connection(credentials: Credentials) -> _ConnectionTarget:
    """Normalize ``settings["connection"]`` into a :class:`_ConnectionTarget`.

    Error messages never echo the URL — its userinfo may carry a password.
    """
    raw = credentials.settings.get("connection")
    if not raw:
        raise ConfigurationError(
            "clickhouse destination needs a connection URL "
            "(http://host:8123/db, https://host:8443/db, or clickhouse://host:8123/db)"
        )
    parts = urlsplit(str(raw))
    interface = _SCHEME_INTERFACES.get(parts.scheme.lower())
    if interface is None:
        raise ConfigurationError(
            "clickhouse connection URL must use the http, https, or clickhouse scheme"
        )
    if parts.query or parts.fragment:
        raise ConfigurationError(
            "clickhouse connection URL must not carry query parameters or fragments"
        )
    host = parts.hostname
    if not host:
        raise ConfigurationError("clickhouse connection URL is missing a host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ConfigurationError("clickhouse connection URL has an invalid port") from exc
    if port is None:
        port = 8443 if interface == "https" else 8123
    path = parts.path.strip("/")
    if "/" in path:
        raise ConfigurationError(
            "clickhouse connection URL path must be a single database name at most"
        )
    username = unquote(parts.username) if parts.username else None
    if username is None and credentials.settings.get("username") is not None:
        username = str(credentials.settings["username"])
    password = unquote(parts.password) if parts.password else None
    if password is None and credentials.settings.get("password") is not None:
        password = str(credentials.settings["password"])
    return _ConnectionTarget(
        interface=interface,
        host=host,
        port=port,
        database=unquote(path) if path else "default",
        username=username or "default",
        password=password or "",
    )


def _identifier(value: str, *, kind: str) -> str:
    normalized = _IDENTIFIER.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        raise DataError(f"cannot derive a ClickHouse {kind} name from {value!r}")
    if normalized[0].isdigit():
        normalized = f"t_{normalized}"
    return normalized


def _column_type(value: Any) -> str:
    """ClickHouse base type for one Python value (``bool`` before ``int``!)."""
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Int64"
    if isinstance(value, float):
        return "Float64"
    return "String"


def _encode(value: Any) -> Any:
    """Insert value for one Python value: JSON text for containers, else as-is."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _infer_columns(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Sanitized column name → base type, in first-seen order.

    The first non-``None`` value pins a column's type; columns that never see
    a value fall back to ``String``.
    """
    inferred: dict[str, str | None] = {}
    for row in rows:
        for key, value in row.items():
            name = _identifier(key, kind="column")
            if inferred.get(name) is None:
                inferred[name] = None if value is None else _column_type(value)
    return {name: column_type or "String" for name, column_type in inferred.items()}


def _encode_row(properties: Mapping[str, Any], column_names: Sequence[str]) -> list[Any]:
    """One insert row aligned to ``column_names``; missing keys become None."""
    encoded = {_identifier(key, kind="column"): _encode(value) for key, value in properties.items()}
    return [encoded.get(name) for name in column_names]


def _identity_columns(options: WriteOptions) -> list[str]:
    columns: list[str] = []
    for identity_field in options.identity_fields or []:
        name = _identifier(identity_field, kind="column")
        if name not in columns:
            columns.append(name)
    return columns


def _create_table_ddl(
    table: str, columns: Mapping[str, str], identity_columns: Sequence[str]
) -> str:
    """DDL for a table's first write.

    Identity columns stay non-Nullable and become the ReplacingMergeTree
    ORDER BY key (see the module docstring for why); everything else is
    Nullable. Without identity columns the table is a plain MergeTree.
    """
    order_by = [name for name in identity_columns if name in columns]
    rendered = ", ".join(
        f"`{name}` {column_type}" if name in order_by else f"`{name}` Nullable({column_type})"
        for name, column_type in columns.items()
    )
    if order_by:
        keys = ", ".join(f"`{name}`" for name in order_by)
        engine = f"ReplacingMergeTree ORDER BY ({keys})"
    else:
        engine = "MergeTree ORDER BY tuple()"
    return f"CREATE TABLE IF NOT EXISTS `{table}` ({rendered}) ENGINE = {engine}"


def _add_column_ddl(table: str, column: str, column_type: str) -> str:
    return f"ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS `{column}` Nullable({column_type})"


def _field_data_type(clickhouse_type: str) -> str:
    base = clickhouse_type
    if base.startswith("Nullable(") and base.endswith(")"):
        base = base[len("Nullable(") : -1]
    if base == "Bool":
        return "boolean"
    if base.startswith(("Int", "UInt", "Float", "Decimal")):
        return "number"
    return "string"


def _error_codes(exc: Exception) -> set[int]:
    codes: set[int] = set()
    structured = getattr(exc, "code", None)  # clickhouse-connect ≥1.7 sets Error.code
    if isinstance(structured, int):
        codes.add(structured)
    codes.update(int(match) for match in _ERROR_CODE.findall(str(exc)))
    return codes


def _map_error(exc: Exception, *, action: str) -> ConnectorError:
    """Map a clickhouse-connect exception onto the Sanka Migrate error taxonomy."""
    message = f"clickhouse {action} failed: {exc}"
    codes = _error_codes(exc)
    text = str(exc)
    if codes & _AUTH_ERROR_CODES or "AUTHENTICATION_FAILED" in text or "ACCESS_DENIED" in text:
        return AuthenticationError(
            message,
            remediation="verify the ClickHouse username/password and the user's access grants",
        )
    if codes & _MISSING_SCHEMA_ERROR_CODES:
        # Unknown table/column mid-write is a schema race with another writer.
        return DataError(message)
    if codes & _TRANSIENT_ERROR_CODES or isinstance(exc, OperationalError | InterfaceError):
        # Network failures, timeouts, and server overload are retryable.
        return TransientProviderError(message)
    return ConnectorError(message)


def _supports_final(engine: str) -> bool:
    """Whether ``SELECT … FINAL`` is legal for a table engine.

    Cloud/replicated variants prefix the base engine name (for example
    ``ReplicatedReplacingMergeTree``, ``SharedReplacingMergeTree``).
    """
    return engine.removeprefix("Shared").removeprefix("Replicated") in _FINAL_ENGINES


def _table_engine(client: Client, table: str) -> str | None:
    """The table's engine name, or ``None`` when the table does not exist."""
    result = client.query(
        "SELECT engine FROM system.tables WHERE database = currentDatabase() "
        "AND name = {name:String}",
        parameters={"name": table},
    )
    rows = result.result_rows
    return str(rows[0][0]) if rows else None


def _describe_table(client: Client, table: str) -> list[tuple[str, str]]:
    result = client.query(f"DESCRIBE TABLE `{table}`")
    return [(str(row[0]), str(row[1])) for row in result.result_rows]


def _ensure_table(
    client: Client,
    table: str,
    columns: Mapping[str, str],
    identity_columns: Sequence[str],
) -> None:
    """Create the table on first write, then widen it for any new columns.

    ``IF NOT EXISTS`` on both statements keeps concurrent writers benign; the
    unconditional describe-and-widen pass after creation also heals the race
    where another writer created the table with a narrower column set.
    """
    if _table_engine(client, table) is None:
        client.command(_create_table_ddl(table, columns, identity_columns))
    existing = {name for name, _ in _describe_table(client, table)}
    for name, column_type in columns.items():
        if name not in existing:
            client.command(_add_column_ddl(table, name, column_type))


class ClickHouseDestination:
    provider = "clickhouse"
    binding_kind = "database"

    def __init__(self) -> None:
        self._clients: dict[_ConnectionTarget, Client] = {}
        self._clients_lock = threading.Lock()

    def automatic_target_object(self, canonical_type: str) -> str | None:
        return _identifier(canonical_type, kind="table")

    async def inventory(
        self,
        credentials: Credentials,
        *,
        canonical_types: set[str],
    ) -> Inventory:
        target = _parse_connection(credentials)

        def _work() -> list[ObjectSchema]:
            client = self._client_for(target)
            objects: list[ObjectSchema] = []
            for canonical_type in sorted(canonical_types):
                table = _identifier(canonical_type, kind="table")
                engine = _table_engine(client, table)
                if engine is None:
                    continue
                # FINAL is load-bearing: with ReplacingMergeTree a raw count()
                # overcounts unmerged row versions, which is not valid parity
                # evidence for verification. It is guarded by engine because
                # ClickHouse rejects FINAL on plain MergeTree (ILLEGAL_FINAL).
                final = " FINAL" if _supports_final(engine) else ""
                count = int(client.query(f"SELECT count() FROM `{table}`{final}").result_rows[0][0])
                fields = [
                    FieldSchema(
                        key=name,
                        label=name,
                        data_type=_field_data_type(type_name),
                        metadata={"clickhouse_type": type_name},
                    )
                    for name, type_name in _describe_table(client, table)
                ]
                objects.append(
                    ObjectSchema(
                        key=table,
                        label=table,
                        canonical_type=canonical_type,
                        record_count=count,
                        fields=fields,
                    )
                )
            return objects

        objects = await self._run("inventory", _work)
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
        target = _parse_connection(credentials)
        table = _identifier(object_type, kind="table")

        def _work() -> None:
            self._insert_rows(target, table, [properties], options)

        await self._run("write", _work)
        # ClickHouse has no row ids to report; dedup happens at merge time.
        return WriteResult(status="created")

    async def write_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        records: list[BatchWriteInput],
        options: WriteOptions,
    ) -> list[BatchWriteResult]:
        if not records:
            return []
        target = _parse_connection(credentials)
        table = _identifier(object_type, kind="table")
        writable = [record.properties for record in records if record.properties]

        def _work() -> None:
            self._insert_rows(target, table, writable, options)

        if writable:
            await self._run("write", _work)
        return [
            BatchWriteResult(trace_id=record.trace_id, status="created")
            if record.properties
            else BatchWriteResult(
                trace_id=record.trace_id, status="skipped", message="empty record"
            )
            for record in records
        ]

    async def write_relationship(
        self,
        credentials: Credentials,
        *,
        relationship: RelationshipWrite,
    ) -> RelationshipWriteResult:
        return RelationshipWriteResult(
            status="skipped", message="clickhouse destination does not model relationships yet"
        )

    # -- internals (synchronous; run on asyncio.to_thread workers) ----------

    def _insert_rows(
        self,
        target: _ConnectionTarget,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        options: WriteOptions,
    ) -> None:
        client = self._client_for(target)
        columns = _infer_columns(rows)
        _ensure_table(client, table, columns, _identity_columns(options))
        names = list(columns)
        client.insert(table, [_encode_row(row, names) for row in rows], column_names=names)

    def _client_for(self, target: _ConnectionTarget) -> Client:
        with self._clients_lock:
            client = self._clients.get(target)
            if client is None:
                # No server session: the cached client is shared across worker
                # threads, and sessionless queries cannot contend on ClickHouse's
                # per-session lock.
                client = clickhouse_connect.get_client(
                    interface=target.interface,
                    host=target.host,
                    port=target.port,
                    username=target.username,
                    password=target.password,
                    database=target.database,
                    autogenerate_session_id=False,
                )
                self._clients[target] = client
            return client

    async def _run(self, action: str, fn: Callable[[], _T]) -> _T:
        try:
            return await asyncio.to_thread(fn)
        except ConnectorError:
            raise
        except Exception as exc:
            raise _map_error(exc, action=action) from exc


CONNECTOR = ConnectorRegistration(name="clickhouse", destination=ClickHouseDestination())
