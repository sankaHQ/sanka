# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the ClickHouse destination — no server required."""

from __future__ import annotations

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError
from sanka_connector_clickhouse import (
    CONNECTOR,
    ClickHouseDestination,
    _add_column_ddl,
    _create_table_ddl,
    _encode,
    _encode_row,
    _identifier,
    _infer_columns,
    _map_error,
    _parse_connection,
    _supports_final,
)

from sanka.connector import (
    AuthenticationError,
    ConfigurationError,
    ConnectorError,
    Credentials,
    DataError,
    DestinationConnector,
    RelationshipWrite,
    SupportsBatchWrites,
    TransientProviderError,
    WriteOptions,
)


def _credentials(url: str, **extra: str) -> Credentials:
    return Credentials(provider="clickhouse", settings={"connection": url, **extra})


# -- URL parsing ------------------------------------------------------------


def test_parse_full_http_url_with_userinfo() -> None:
    target = _parse_connection(_credentials("http://alice:s%40cret@ch.example.com:9010/analytics"))
    assert target.interface == "http"
    assert target.host == "ch.example.com"
    assert target.port == 9010
    assert target.database == "analytics"
    assert target.username == "alice"
    assert target.password == "s@cret"


def test_parse_http_defaults() -> None:
    target = _parse_connection(_credentials("http://ch.example.com"))
    assert target.port == 8123
    assert target.database == "default"
    assert target.username == "default"
    assert target.password == ""
    assert target == _parse_connection(_credentials("http://ch.example.com/"))


def test_parse_https_defaults_to_tls_port() -> None:
    target = _parse_connection(_credentials("https://ch.example.com/warehouse"))
    assert target.interface == "https"
    assert target.port == 8443
    assert target.database == "warehouse"


def test_parse_clickhouse_scheme_normalizes_to_http() -> None:
    target = _parse_connection(_credentials("clickhouse://ch.example.com/warehouse"))
    assert target.interface == "http"
    assert target.port == 8123
    # Normalization makes both spellings the same client-cache key.
    assert target == _parse_connection(_credentials("http://ch.example.com:8123/warehouse"))


def test_parse_settings_credentials_and_url_precedence() -> None:
    from_settings = _parse_connection(
        _credentials("clickhouse://ch.example.com/db", username="svc", password="pw")
    )
    assert from_settings.username == "svc"
    assert from_settings.password == "pw"

    url_wins = _parse_connection(
        _credentials("clickhouse://alice:urlpw@ch.example.com/db", username="svc", password="pw")
    )
    assert url_wins.username == "alice"
    assert url_wins.password == "urlpw"


@pytest.mark.parametrize(
    "url",
    [
        "postgres://ch.example.com/db",
        "http:///db",
        "http://ch.example.com/db?secure=1",
        "http://ch.example.com/db#frag",
        "http://ch.example.com/db/extra",
        "http://ch.example.com:notaport/db",
    ],
)
def test_parse_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(ConfigurationError):
        _parse_connection(_credentials(url))


def test_parse_requires_connection_and_never_echoes_secrets() -> None:
    with pytest.raises(ConfigurationError):
        _parse_connection(Credentials(provider="clickhouse", settings={}))
    with pytest.raises(ConfigurationError) as excinfo:
        _parse_connection(_credentials("clickhouse://u:supersecret@ch.example.com/a/b"))
    assert "supersecret" not in str(excinfo.value)


# -- identifiers, typing, encoding ------------------------------------------


def test_identifier_sanitization() -> None:
    assert ClickHouseDestination().automatic_target_object("My Docs!") == "my_docs"
    assert _identifier("42nd Street", kind="table") == "t_42nd_street"
    with pytest.raises(DataError):
        _identifier("!!!", kind="table")


def test_infer_columns_types_first_seen_order_and_first_non_none() -> None:
    rows = [
        {
            "OK?": True,
            "Count": 1,
            "Score": 1.5,
            "Name": "x",
            "Tags": ["a"],
            "Meta": {"k": 1},
            "Empty": None,
        },
        {"Empty": 7, "Count": 2.5},
    ]
    assert _infer_columns(rows) == {
        "ok": "Bool",  # bool checked before int
        "count": "Int64",  # first non-None value wins; the later 2.5 is ignored
        "score": "Float64",
        "name": "String",
        "tags": "String",
        "meta": "String",
        "empty": "Int64",  # None rows do not pin a type
    }
    assert _infer_columns([{"x": None}]) == {"x": "String"}


def test_encode_json_containers_and_fallback() -> None:
    assert _encode({"k": "テスト"}) == '{"k": "テスト"}'  # ensure_ascii=False
    assert _encode([1, 2]) == "[1, 2]"
    assert _encode(True) is True
    assert _encode(None) is None
    assert _encode("text") == "text"

    class Odd:
        def __str__(self) -> str:
            return "odd!"

    assert _encode(Odd()) == "odd!"


def test_encode_row_fills_missing_keys_with_none() -> None:
    assert _encode_row({"Path": "a.md", "n": 1}, ["path", "n", "missing"]) == ["a.md", 1, None]


# -- DDL generation ---------------------------------------------------------


def test_create_table_ddl_with_identity_uses_replacing_merge_tree() -> None:
    ddl = _create_table_ddl(
        "documents", {"path": "String", "title": "String", "views": "Int64"}, ["path"]
    )
    assert ddl == (
        "CREATE TABLE IF NOT EXISTS `documents` "
        "(`path` String, `title` Nullable(String), `views` Nullable(Int64)) "
        "ENGINE = ReplacingMergeTree ORDER BY (`path`)"
    )


def test_create_table_ddl_without_identity_uses_merge_tree() -> None:
    ddl = _create_table_ddl("events", {"kind": "String"}, [])
    assert ddl == (
        "CREATE TABLE IF NOT EXISTS `events` (`kind` Nullable(String)) "
        "ENGINE = MergeTree ORDER BY tuple()"
    )
    # Identity fields absent from the record cannot be ORDER BY keys.
    assert "MergeTree ORDER BY tuple()" in _create_table_ddl("events", {"kind": "String"}, ["id"])


def test_add_column_ddl_is_nullable_and_idempotent() -> None:
    assert _add_column_ddl("documents", "comment", "String") == (
        "ALTER TABLE `documents` ADD COLUMN IF NOT EXISTS `comment` Nullable(String)"
    )


def test_supports_final_only_for_collapsing_engines() -> None:
    # ClickHouse rejects FINAL on plain MergeTree (ILLEGAL_FINAL, seen on 24.8),
    # so inventory guards it by engine, including replicated/cloud prefixes.
    assert _supports_final("ReplacingMergeTree")
    assert _supports_final("ReplicatedReplacingMergeTree")
    assert _supports_final("SharedReplacingMergeTree")
    assert _supports_final("AggregatingMergeTree")
    assert not _supports_final("MergeTree")
    assert not _supports_final("ReplicatedMergeTree")
    assert not _supports_final("SharedMergeTree")
    assert not _supports_final("Memory")


# -- error mapping ----------------------------------------------------------


def test_map_error_categories() -> None:
    auth = _map_error(
        DatabaseError("Code: 516. DB::Exception: AUTHENTICATION_FAILED", code=516),
        action="write",
    )
    assert isinstance(auth, AuthenticationError)

    missing = _map_error(
        DatabaseError("Code: 60. DB::Exception: UNKNOWN_TABLE", code=60), action="write"
    )
    assert isinstance(missing, DataError)

    transient = _map_error(OperationalError("Error executing HTTP request"), action="write")
    assert isinstance(transient, TransientProviderError)
    assert transient.retryable

    unknown = _map_error(DatabaseError("Code: 999. DB::Exception: odd"), action="write")
    assert type(unknown) is ConnectorError

    fallback = _map_error(RuntimeError("server said Code: 516 somewhere"), action="write")
    assert isinstance(fallback, AuthenticationError)


# -- registration and serverless SPI behavior --------------------------------


def test_registration_shape() -> None:
    assert CONNECTOR.name == "clickhouse"
    assert CONNECTOR.source is None
    assert CONNECTOR.destination is not None
    assert isinstance(CONNECTOR.destination, DestinationConnector)
    assert isinstance(CONNECTOR.destination, SupportsBatchWrites)


async def test_empty_writes_skip_without_server() -> None:
    destination = ClickHouseDestination()
    options = WriteOptions(conflict_policy="create")
    single = await destination.write_record(
        Credentials(provider="clickhouse", settings={}),
        object_type="documents",
        properties={},
        options=options,
    )
    assert single.status == "skipped"

    credentials = _credentials("http://localhost:8123/default")
    assert (
        await destination.write_records(
            credentials, object_type="documents", records=[], options=options
        )
        == []
    )


async def test_write_relationship_is_skipped() -> None:
    result = await ClickHouseDestination().write_relationship(
        Credentials(provider="clickhouse", settings={}),
        relationship=RelationshipWrite(
            trace_id="t1",
            object_type="documents",
            record_id="1",
            relationship_field="parent",
            related_object_type="documents",
            related_record_id="2",
        ),
    )
    assert result.status == "skipped"
    assert result.message is not None
