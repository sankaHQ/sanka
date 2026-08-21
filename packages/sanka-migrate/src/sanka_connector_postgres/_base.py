# SPDX-License-Identifier: Apache-2.0
"""Shared internals for the PostgreSQL connector.

Connection management (one cached ``psycopg.AsyncConnection`` per DSN per
event loop, autocommit), identifier sanitization, source type-family mapping,
JSON-safe value conversion, cursor handling, and the psycopg → Sanka error
mapping. Both roles subclass :class:`PostgresConnectorBase`.
"""

from __future__ import annotations

import asyncio
import datetime
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Final

import psycopg
from psycopg.conninfo import conninfo_to_dict

from sanka.connector import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    ConnectorError,
    Credentials,
    DataError,
    ErrorCategory,
    PermissionDeniedError,
    ProviderTimeoutError,
    TransientProviderError,
)

_IDENTIFIER = re.compile(r"[^a-z0-9_]+")
_MAX_IDENTIFIER_LENGTH = 63

# information_schema ``data_type`` groupings (already lowercase for built-ins).
_NUMBER_TYPES: Final = frozenset(
    {"smallint", "integer", "bigint", "numeric", "decimal", "real", "double precision"}
)
_INTEGER_TYPES: Final = frozenset({"smallint", "integer", "bigint"})
_TEMPORAL_TYPES: Final = frozenset(
    {
        "date",
        "time without time zone",
        "time with time zone",
        "timestamp without time zone",
        "timestamp with time zone",
    }
)
_JSON_TYPES: Final = frozenset({"json", "jsonb"})
_BINARY_TYPES: Final = frozenset({"bytea"})

#: Sentinel returned by :func:`json_safe` for binary values the connector skips.
SKIPPED_BINARY: Final = object()


def identifier(value: str, *, kind: str) -> str:
    """Sanitize ``value`` into a lowercase PostgreSQL identifier."""
    normalized = _IDENTIFIER.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        raise DataError(f"cannot derive a PostgreSQL {kind} name from {value!r}")
    if normalized[0].isdigit():
        normalized = f"t_{normalized}"
    if len(normalized) > _MAX_IDENTIFIER_LENGTH:
        normalized = normalized[:_MAX_IDENTIFIER_LENGTH].rstrip("_")
    return normalized


def field_family(data_type: str) -> str:
    """Map an information_schema ``data_type`` to a Sanka field type family."""
    lowered = data_type.lower()
    if lowered in _NUMBER_TYPES:
        return "number"
    if lowered == "boolean":
        return "boolean"
    if lowered in _JSON_TYPES:
        return "object"
    return "string"


def is_temporal(data_type: str) -> bool:
    return data_type.lower() in _TEMPORAL_TYPES


def is_binary(data_type: str) -> bool:
    return data_type.lower() in _BINARY_TYPES


def is_integer(data_type: str) -> bool:
    return data_type.lower() in _INTEGER_TYPES


def json_safe(value: Any) -> Any:
    """Convert a fetched value into a JSON-safe Python value.

    datetime/date/time → ISO 8601 string, ``Decimal`` → string (no precision
    loss), ``UUID`` → string, containers convert recursively (binary elements
    become ``None``), binary scalars return :data:`SKIPPED_BINARY` so callers
    can drop the column, and any other exotic scalar becomes its ``str`` form.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return SKIPPED_BINARY
    if isinstance(value, list | tuple):
        return [_element(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _element(item) for key, item in value.items()}
    return str(value)


def _element(item: Any) -> Any:
    converted = json_safe(item)
    return None if converted is SKIPPED_BINARY else converted


def cursor_text(value: Any) -> str:
    """Render a key value as an opaque string cursor / snapshot bound."""
    converted = json_safe(value)
    if converted is SKIPPED_BINARY or converted is None:
        raise DataError("cannot build a pagination cursor from a binary or NULL key value")
    return converted if isinstance(converted, str) else str(converted)


def cursor_param(data_type: str, text: str) -> Any:
    """Turn a string cursor back into a query parameter for the key column.

    Integer-family keys are cast client-side; every other key type is sent as
    a string, which psycopg dumps with unknown OID so the server casts it to
    the column's native type — comparisons always run in that native type.
    """
    if is_integer(data_type):
        try:
            return int(text)
        except ValueError as error:
            raise DataError(f"cursor {text!r} is not valid for a {data_type} key") from error
    return text


def mapped_error(error: psycopg.Error, *, context: str) -> ConnectorError:
    """Map a psycopg exception onto the Sanka error taxonomy."""
    message = f"{context}: {error}"
    sqlstate = error.sqlstate or ""
    if sqlstate.startswith("28") or "authentication failed" in str(error).lower():
        return AuthenticationError(message)
    if sqlstate == "42501":
        return PermissionDeniedError(message)
    if sqlstate in ("42P01", "42703"):
        return DataError(message)
    if sqlstate == "23505":
        return ConflictError(message)
    if sqlstate == "57014":
        return ProviderTimeoutError(message)
    if sqlstate.startswith(("08", "53", "57", "58")):
        return TransientProviderError(message)
    if isinstance(error, psycopg.OperationalError):
        # Connection refused / DNS / timeouts / broken connections.
        return TransientProviderError(message)
    return ConnectorError(message, category=ErrorCategory.UNKNOWN)


@asynccontextmanager
async def pg_errors(context: str) -> AsyncIterator[None]:
    """Re-raise psycopg failures as Sanka connector errors."""
    try:
        yield
    except psycopg.Error as error:
        raise mapped_error(error, context=context) from error


_TABLES_SQL = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = %s AND table_type = 'BASE TABLE'
ORDER BY table_name
"""

_COLUMNS_SQL = """
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = %s AND table_name = %s
ORDER BY ordinal_position
"""

_PRIMARY_KEY_SQL = """
SELECT kcu.column_name, col.data_type
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_schema = tc.constraint_schema
 AND kcu.constraint_name = tc.constraint_name
 AND kcu.table_schema = tc.table_schema
 AND kcu.table_name = tc.table_name
JOIN information_schema.columns col
  ON col.table_schema = kcu.table_schema
 AND col.table_name = kcu.table_name
 AND col.column_name = kcu.column_name
WHERE tc.constraint_type = 'PRIMARY KEY'
  AND tc.table_schema = %s
  AND tc.table_name = %s
ORDER BY kcu.ordinal_position
"""


class PostgresConnectorBase:
    """Connection and information_schema plumbing shared by both roles."""

    provider = "postgres"
    binding_kind = "database"

    def __init__(self) -> None:
        self._connections: dict[tuple[int, str], psycopg.AsyncConnection[Any]] = {}

    async def close(self) -> None:
        """Close connections cached for the running event loop (test hygiene;
        the runtime does not require it)."""
        loop_id = id(asyncio.get_running_loop())
        for key in [key for key in self._connections if key[0] == loop_id]:
            connection = self._connections.pop(key)
            if not connection.closed:
                await connection.close()

    # -- internals ----------------------------------------------------------

    def _dsn(self, credentials: Credentials) -> str:
        raw = credentials.settings.get("connection")
        if raw is None or not str(raw).strip():
            raise ConfigurationError(
                "postgres connector needs a DSN in settings['connection']"
                " (postgres://…, postgresql://…, or a libpq keyword string)"
            )
        dsn = str(raw).strip()
        try:
            conninfo_to_dict(dsn)
        except psycopg.ProgrammingError as error:
            raise ConfigurationError(f"invalid postgres DSN: {error}") from error
        return dsn

    def _schema_name(self, credentials: Credentials) -> str:
        raw = credentials.settings.get("schema", "public")
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigurationError("settings['schema'] must be a non-empty schema name")
        return raw.strip()

    async def _connection(self, credentials: Credentials) -> psycopg.AsyncConnection[Any]:
        """One cached connection per (event loop, DSN).

        Keying by loop matters because the registration object is a process
        singleton: successive ``asyncio.run`` calls (CLI invocations, test
        functions) must not share a loop-bound connection. Connections from
        finished loops are simply dropped; broken or closed connections are
        replaced on the next call.
        """
        dsn = self._dsn(credentials)
        key = (id(asyncio.get_running_loop()), dsn)
        cached = self._connections.get(key)
        if cached is not None and not cached.closed and not cached.broken:
            return cached
        connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        self._connections[key] = connection
        return connection

    async def _tables(self, connection: psycopg.AsyncConnection[Any], schema: str) -> list[str]:
        cursor = await connection.execute(_TABLES_SQL, (schema,))
        return [str(row[0]) for row in await cursor.fetchall()]

    async def _columns(
        self, connection: psycopg.AsyncConnection[Any], schema: str, table: str
    ) -> list[tuple[str, str]]:
        cursor = await connection.execute(_COLUMNS_SQL, (schema, table))
        return [(str(row[0]), str(row[1])) for row in await cursor.fetchall()]

    async def _primary_key_columns(
        self, connection: psycopg.AsyncConnection[Any], schema: str, table: str
    ) -> list[tuple[str, str]]:
        cursor = await connection.execute(_PRIMARY_KEY_SQL, (schema, table))
        return [(str(row[0]), str(row[1])) for row in await cursor.fetchall()]
