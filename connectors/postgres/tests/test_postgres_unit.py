# SPDX-License-Identifier: Apache-2.0
"""Unit tests that need no PostgreSQL server: sanitization, type-family
mapping, JSON-safety conversion, cursor handling, error mapping, DSN
validation, and registration shape."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import psycopg
import pytest
from sanka_connector_postgres import CONNECTOR, PostgresDestination, PostgresSource
from sanka_connector_postgres._base import (
    SKIPPED_BINARY,
    cursor_param,
    cursor_text,
    field_family,
    identifier,
    json_safe,
    mapped_error,
)
from sanka_connector_postgres._destination import (
    _column_type,
    _index_name,
    _required_type,
    _write_param,
)

from sanka.connector import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    ConnectorError,
    Credentials,
    DataError,
    DestinationConnector,
    ErrorCategory,
    SourceConnector,
    SupportsRecordCounts,
    SupportsSnapshotBounds,
    TransientProviderError,
    UnsupportedFeatureError,
)


def test_identifier_sanitization() -> None:
    assert identifier("My Docs!", kind="table") == "my_docs"
    assert identifier("  Ärger--Straße  ", kind="table") == "rger_stra_e"
    assert identifier("42nd_street", kind="column") == "t_42nd_street"
    assert identifier("a" * 80, kind="table") == "a" * 63
    with pytest.raises(DataError):
        identifier("!!!", kind="table")


def test_field_family_mapping() -> None:
    for data_type in ("smallint", "integer", "bigint", "numeric", "real", "double precision"):
        assert field_family(data_type) == "number"
    assert field_family("boolean") == "boolean"
    assert field_family("json") == "object"
    assert field_family("jsonb") == "object"
    for data_type in ("timestamp with time zone", "date", "time without time zone"):
        assert field_family(data_type) == "string"
    assert field_family("text") == "string"
    assert field_family("uuid") == "string"
    assert field_family("USER-DEFINED") == "string"


def test_json_safe_conversion() -> None:
    moment = datetime.datetime(2026, 8, 16, 9, 30, tzinfo=datetime.UTC)
    unique = uuid.uuid4()
    assert json_safe(moment) == "2026-08-16T09:30:00+00:00"
    assert json_safe(moment.date()) == "2026-08-16"
    assert json_safe(moment.time()) == "09:30:00"
    assert json_safe(Decimal("12.50")) == "12.50"
    assert json_safe(unique) == str(unique)
    assert json_safe(b"\x00\x01") is SKIPPED_BINARY
    assert json_safe(memoryview(b"\x00")) is SKIPPED_BINARY
    assert json_safe(None) is None
    assert json_safe(True) is True
    assert json_safe(3) == 3
    assert json_safe("x") == "x"
    # Containers convert recursively; binary elements become None.
    assert json_safe([moment.date(), Decimal("1"), b"\x00"]) == ["2026-08-16", "1", None]
    assert json_safe({"when": moment.date(), 1: "x"}) == {"when": "2026-08-16", "1": "x"}
    # Exotic scalars fall back to their string form.
    assert json_safe(datetime.timedelta(days=1)) == "1 day, 0:00:00"


def test_cursor_text_and_param_round_trip() -> None:
    assert cursor_text(41) == "41"
    assert cursor_text(datetime.date(2026, 8, 16)) == "2026-08-16"
    assert cursor_param("bigint", "41") == 41
    assert cursor_param("uuid", "0" * 8) == "0" * 8
    assert cursor_param("timestamp with time zone", "2026-08-16T09:30:00+00:00") == (
        "2026-08-16T09:30:00+00:00"
    )
    with pytest.raises(DataError):
        cursor_param("bigint", "not-a-number")
    with pytest.raises(DataError):
        cursor_text(None)
    with pytest.raises(DataError):
        cursor_text(b"\x00")


def test_error_mapping() -> None:
    assert isinstance(
        mapped_error(psycopg.errors.InvalidPassword("no"), context="c"), AuthenticationError
    )
    assert isinstance(
        mapped_error(psycopg.OperationalError("connection refused"), context="c"),
        TransientProviderError,
    )
    assert isinstance(mapped_error(psycopg.errors.UndefinedTable("gone"), context="c"), DataError)
    assert isinstance(mapped_error(psycopg.errors.UndefinedColumn("gone"), context="c"), DataError)
    assert isinstance(
        mapped_error(psycopg.errors.UniqueViolation("dup"), context="c"), ConflictError
    )
    unknown = mapped_error(psycopg.errors.SyntaxError("bad"), context="ctx")
    assert type(unknown) is ConnectorError
    assert unknown.category is ErrorCategory.UNKNOWN
    assert "ctx" in str(unknown)


async def test_dsn_validation_errors() -> None:
    source = PostgresSource()
    with pytest.raises(ConfigurationError):
        await source.discover_objects(Credentials(provider="postgres"))
    with pytest.raises(ConfigurationError):
        await source.discover_objects(
            Credentials(provider="postgres", settings={"connection": "   "})
        )
    with pytest.raises(ConfigurationError):
        await source.discover_objects(
            Credentials(provider="postgres", settings={"connection": "definitely not a dsn"})
        )
    with pytest.raises(ConfigurationError):
        await source.discover_objects(
            Credentials(
                provider="postgres",
                settings={"connection": "postgresql://localhost/db", "schema": "  "},
            )
        )


async def test_source_filter_is_rejected_not_ignored() -> None:
    from sanka.connector import SourceFilter

    source = PostgresSource()
    with pytest.raises(UnsupportedFeatureError):
        await source.count_records(
            Credentials(provider="postgres", settings={"connection": "postgresql://x/y"}),
            object_type="t",
            source_filter=SourceFilter(field="active"),
        )


def test_destination_column_typing_and_params() -> None:
    assert _column_type(True) == "boolean"
    assert _column_type(3) == "bigint"
    assert _column_type(1.5) == "double precision"
    assert _column_type({"a": 1}) == "jsonb"
    assert _column_type(["a"]) == "jsonb"
    assert _column_type("x") == "text"
    assert _column_type(None) == "text"

    json_param = _write_param("jsonb", {"a": 1})
    assert json_param.obj == {"a": 1}
    assert _write_param("jsonb", "plain").obj == "plain"
    assert _write_param("boolean", True) is True
    assert _write_param("bigint", True) == 1
    assert _write_param("text", True) == "true"
    assert _write_param("bigint", 3) == 3
    assert _write_param("text", 3) == "3"
    assert _write_param("double precision", 1.5) == 1.5
    assert _write_param("text", {"a": 1}) == '{"a": 1}'
    assert _write_param("boolean", None) is None
    assert _write_param("boolean", "true") == "true"  # server casts the literal


def test_type_promotion_ladder() -> None:
    # Fits: no promotion.
    assert _required_type("text", 42) is None
    assert _required_type("jsonb", "anything") is None
    assert _required_type("boolean", True) is None
    assert _required_type("boolean", "TRUE") is None
    assert _required_type("bigint", 7) is None
    assert _required_type("bigint", "7") is None
    assert _required_type("double precision", 1.5) is None
    assert _required_type("double precision", "1e3") is None
    assert _required_type("bigint", None) is None
    # Promotions up the ladder.
    assert _required_type("boolean", 5) == "bigint"
    assert _required_type("boolean", 1.5) == "double precision"
    assert _required_type("boolean", "many") == "text"
    assert _required_type("bigint", 1.5) == "double precision"
    assert _required_type("bigint", "1.5") == "double precision"
    assert _required_type("bigint", "many") == "text"
    assert _required_type("bigint", {"a": 1}) == "text"
    assert _required_type("double precision", "many") == "text"
    # Pre-existing (non-minted) column types are never promoted.
    assert _required_type("integer", "many") is None
    assert _required_type("timestamp with time zone", "not a date") is None


def test_index_name_is_deterministic_and_bounded() -> None:
    assert _index_name("documents", ["path"]) == "documents_path_sanka_uq"
    long = _index_name("t" * 40, ["c" * 40])
    assert len(long) <= 63
    assert long == _index_name("t" * 40, ["c" * 40])
    assert long != _index_name("t" * 40, ["d" * 40])


def test_registration_shape_and_capabilities() -> None:
    assert CONNECTOR.name == "postgres"
    assert CONNECTOR.source is not None and CONNECTOR.destination is not None
    source, destination = PostgresSource(), PostgresDestination()
    assert source.provider == destination.provider == "postgres"
    assert source.binding_kind == destination.binding_kind == "database"
    assert isinstance(source, SourceConnector)
    assert isinstance(source, SupportsRecordCounts)
    assert isinstance(source, SupportsSnapshotBounds)
    assert isinstance(destination, DestinationConnector)
    assert destination.automatic_target_object("Sales Orders (2026)") == "sales_orders_2026"
