# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the NetSuite source — every request is served by an
``httpx.MockTransport``; no account, no network."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from sanka.connector import (
    AuthenticationError,
    ConfigurationError,
    ConnectorError,
    Credentials,
    DataError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    SourceConnector,
    SourceFilter,
    SupportsRecordCounts,
    SupportsSnapshotBounds,
    TransientProviderError,
    UnsupportedFeatureError,
    ValidationFailedError,
)
from sanka_connector_netsuite import CONNECTOR, HttpNetSuiteGateway, NetSuiteSource

BASE_URL = "https://sankademo1.suitetalk.api.netsuite.com"
SUITEQL_PATH = "/services/rest/query/v1/suiteql"
TOKEN_PATH = "/services/rest/auth/oauth2/v1/token"


def _credentials(**overrides: Any) -> Credentials:
    values: dict[str, Any] = {
        "provider": "netsuite",
        "connection_id": "conn-1",
        "access_token": "token-1",
        "settings": {"api_base_url": BASE_URL},
    }
    values.update(overrides)
    return Credentials(**values)


Handler = Any  # Callable[[httpx.Request], httpx.Response]


def _source(handler: Handler) -> NetSuiteSource:
    return NetSuiteSource(gateway=HttpNetSuiteGateway(transport=httpx.MockTransport(handler)))


def _page(
    items: list[dict[str, Any]],
    *,
    has_more: bool = False,
    total_results: int | None = None,
) -> dict[str, Any]:
    return {
        "links": [],
        "count": len(items),
        "hasMore": has_more,
        "items": [{"links": [], **item} for item in items],
        "offset": 0,
        "totalResults": len(items) if total_results is None else total_results,
    }


def _count_payload(count: int) -> dict[str, Any]:
    return _page([{"expr1": count}], total_results=1)


def _query(request: httpx.Request) -> str:
    return str(json.loads(request.content.decode("utf-8"))["q"])


def _vendor_error(status: int, code: str, detail: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "type": "https://www.rfc-editor.org/rfc/rfc7231#section-6.5.1",
            "title": "Error",
            "status": status,
            "o:errorDetails": [{"detail": detail, "o:errorCode": code}],
        },
    )


# -- credential validation ---------------------------------------------------


async def test_missing_api_base_url_is_a_configuration_error() -> None:
    source = _source(lambda request: httpx.Response(200, json=_count_payload(0)))
    with pytest.raises(ConfigurationError, match="api_base_url"):
        await source.count_records(_credentials(settings={}), object_type="customer")


async def test_invalid_api_base_url_is_a_configuration_error() -> None:
    source = _source(lambda request: httpx.Response(200, json=_count_payload(0)))
    with pytest.raises(ConfigurationError, match="HTTP"):
        await source.count_records(
            _credentials(settings={"api_base_url": "not a url"}),
            object_type="customer",
        )


async def test_missing_token_without_grant_credentials_is_a_configuration_error() -> None:
    source = _source(lambda request: httpx.Response(200, json=_count_payload(0)))
    with pytest.raises(ConfigurationError, match="access token"):
        await source.count_records(_credentials(access_token=None), object_type="customer")


# -- request shape -----------------------------------------------------------


async def test_suiteql_requests_carry_the_transient_prefer_header() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["prefer"] = request.headers.get("Prefer")
        seen["authorization"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_count_payload(3))

    count = await _source(handler).count_records(_credentials(), object_type="customer")
    assert count == 3
    assert seen["prefer"] == "transient"
    assert seen["authorization"] == "Bearer token-1"
    assert seen["path"] == SUITEQL_PATH
    assert seen["params"] == {"limit": "1", "offset": "0"}


async def test_transaction_objects_add_the_type_discriminator() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(_query(request))
        return httpx.Response(200, json=_count_payload(80))

    source = _source(handler)
    assert await source.count_records(_credentials(), object_type="salesOrder") == 80
    assert await source.count_records(_credentials(), object_type="invoice") == 80
    assert queries == [
        "SELECT COUNT(*) FROM transaction WHERE type = 'SalesOrd'",
        "SELECT COUNT(*) FROM transaction WHERE type = 'CustInvc'",
    ]


async def test_unknown_object_type_never_reaches_the_account() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(ValidationFailedError, match="Unknown NetSuite object type"):
        await _source(handler).count_records(_credentials(), object_type="employee")


# -- discovery and inventory -------------------------------------------------


async def test_discover_objects_returns_the_registry_customer_first() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("discovery makes no provider calls")

    options = await _source(handler).discover_objects(_credentials())
    assert [option.key for option in options][:1] == ["customer"]
    assert {option.key for option in options} == {
        "customer",
        "vendor",
        "inventoryItem",
        "salesOrder",
        "invoice",
    }
    customer = options[0]
    assert customer.canonical_type == "company"
    assert customer.default_selected is True


async def test_inventory_counts_objects_and_collects_warnings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = _query(request)
        if "FROM customer" in query:
            return httpx.Response(200, json=_count_payload(60))
        return _vendor_error(403, "INSUFFICIENT_PERMISSION", "no access")

    inventory = await _source(handler).inventory(
        _credentials(),
        object_types=["customer", "vendor"],
    )
    assert [schema.key for schema in inventory.objects] == ["customer"]
    customer = inventory.objects[0]
    assert customer.record_count == 60
    assert customer.canonical_type == "company"
    assert customer.identity_fields == ["id"]
    assert {field.key for field in customer.fields} >= {"id", "entityid", "companyname"}
    assert len(inventory.warnings) == 1
    assert "vendor" in inventory.warnings[0]
    assert "INSUFFICIENT_PERMISSION" in inventory.warnings[0]


# -- reads -------------------------------------------------------------------


async def test_read_records_keyset_paginates_on_id() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = _query(request)
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=_page(
                [
                    {"id": "1001", "companyname": "Northstar", "email": "a@example.test"},
                    {"id": "1002", "companyname": "Harbor", "email": "b@example.test"},
                ],
                has_more=True,
                total_results=60,
            ),
        )

    page = await _source(handler).read_records(
        _credentials(),
        object_type="customer",
        field_keys=["companyname", "email", "id"],
        limit=2,
        cursor="1000",
    )
    assert seen["query"] == (
        "SELECT id, companyname, email FROM customer WHERE id > 1000 ORDER BY id"
    )
    assert seen["params"] == {"limit": "2", "offset": "0"}
    assert page.object_key == "customer"
    assert [record["id"] for record in page.records] == ["1001", "1002"]
    assert all("links" not in record for record in page.records)
    assert page.has_more is True
    assert page.next_cursor == "1002"


async def test_read_records_final_page_has_no_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([{"id": "1060"}], has_more=False))

    page = await _source(handler).read_records(
        _credentials(),
        object_type="customer",
        field_keys=[],
        limit=100,
    )
    assert page.has_more is False
    assert page.next_cursor is None


async def test_read_records_bounded_composes_both_predicates() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(_query(request))
        return httpx.Response(200, json=_page([{"id": "40002", "tranid": "INV-9002"}]))

    page = await _source(handler).read_records_bounded(
        _credentials(),
        object_type="invoice",
        field_keys=["tranid"],
        limit=50,
        cursor="40001",
        upper_bound="40080",
    )
    assert queries == [
        "SELECT id, tranid FROM transaction"
        " WHERE type = 'CustInvc' AND id > 40001 AND id <= 40080 ORDER BY id"
    ]
    assert page.records == [{"id": "40002", "tranid": "INV-9002"}]


async def test_invalid_cursor_and_unknown_fields_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    source = _source(handler)
    with pytest.raises(ValidationFailedError, match="cursor"):
        await source.read_records(
            _credentials(),
            object_type="customer",
            field_keys=[],
            limit=10,
            cursor="1000; DROP",
        )
    with pytest.raises(ValidationFailedError, match="field"):
        await source.read_records(
            _credentials(),
            object_type="customer",
            field_keys=["favoritecolor"],
            limit=10,
        )


async def test_source_filters_are_unsupported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(UnsupportedFeatureError):
        await _source(handler).read_records(
            _credentials(),
            object_type="customer",
            field_keys=[],
            limit=10,
            source_filter=SourceFilter(field="isinactive", operator="equals", value=False),
        )


async def test_high_water_mark_orders_descending_with_limit_one() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = _query(request)
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_page([{"id": "20080"}]))

    mark = await _source(handler).high_water_mark(_credentials(), object_type="salesOrder")
    assert mark == "20080"
    assert seen["query"] == ("SELECT id FROM transaction WHERE type = 'SalesOrd' ORDER BY id DESC")
    assert seen["params"]["limit"] == "1"


async def test_count_records_bounded_appends_the_bound() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = _query(request)
        return httpx.Response(200, json=_count_payload(41))

    count = await _source(handler).count_records_bounded(
        _credentials(),
        object_type="customer",
        upper_bound="1041",
    )
    assert count == 41
    assert seen["query"] == "SELECT COUNT(*) FROM customer WHERE id <= 1041"


# -- auth --------------------------------------------------------------------


async def test_static_token_401_is_terminal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _vendor_error(401, "INVALID_LOGIN", "Invalid login attempt.")

    with pytest.raises(AuthenticationError, match="INVALID_LOGIN"):
        await _source(handler).count_records(_credentials(), object_type="customer")


async def test_grant_credentials_recover_a_rejected_token_once() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == TOKEN_PATH:
            form = dict(parse_qsl(request.content.decode("utf-8")))
            assert form == {
                "grant_type": "client_credentials",
                "client_id": "integration-1",
                "client_secret": "secret-1",
            }
            return httpx.Response(
                200,
                json={"access_token": "token-2", "expires_in": 3600, "token_type": "Bearer"},
            )
        if request.headers.get("Authorization") == "Bearer token-2":
            return httpx.Response(200, json=_count_payload(60))
        return _vendor_error(401, "INVALID_LOGIN", "expired")

    credentials = _credentials(client_id="integration-1", client_secret="secret-1")
    source = _source(handler)
    assert await source.count_records(credentials, object_type="customer") == 60
    assert calls == [SUITEQL_PATH, TOKEN_PATH, SUITEQL_PATH]
    # The granted token is cached: the next call skips the token endpoint.
    assert await source.count_records(credentials, object_type="customer") == 60
    assert calls == [SUITEQL_PATH, TOKEN_PATH, SUITEQL_PATH, SUITEQL_PATH]


async def test_grant_mints_a_token_when_none_is_supplied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "token-3"})
        assert request.headers.get("Authorization") == "Bearer token-3"
        return httpx.Response(200, json=_count_payload(25))

    credentials = _credentials(
        access_token=None,
        client_id="integration-1",
        client_secret="secret-1",
    )
    assert await _source(handler).count_records(credentials, object_type="vendor") == 25


async def test_rejected_grant_is_an_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return _vendor_error(401, "invalid_client", "unknown client")
        return _vendor_error(401, "INVALID_LOGIN", "expired")

    credentials = _credentials(client_id="integration-1", client_secret="wrong")
    with pytest.raises(AuthenticationError, match="invalid_client"):
        await _source(handler).count_records(credentials, object_type="customer")


async def test_token_url_setting_overrides_the_grant_endpoint() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "auth.example.test":
            return httpx.Response(200, json={"access_token": "token-4"})
        return httpx.Response(200, json=_count_payload(1))

    credentials = _credentials(
        access_token=None,
        client_id="integration-1",
        client_secret="secret-1",
        settings={
            "api_base_url": BASE_URL,
            "token_url": "https://auth.example.test/oauth2/token",
        },
    )
    assert await _source(handler).count_records(credentials, object_type="customer") == 1
    assert hosts == ["auth.example.test", "sankademo1.suitetalk.api.netsuite.com"]


# -- error mapping -----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (500, TransientProviderError),
        (400, ConnectorError),
    ],
)
async def test_http_failures_map_onto_the_error_taxonomy(
    status: int,
    expected: type[ConnectorError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _vendor_error(status, "SSS_ERROR", "provider detail")

    with pytest.raises(expected, match="SSS_ERROR: provider detail"):
        await _source(handler).count_records(_credentials(), object_type="customer")


async def test_rate_limits_carry_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"}, json={})

    with pytest.raises(RateLimitError) as excinfo:
        await _source(handler).count_records(_credentials(), object_type="customer")
    assert excinfo.value.retry_after_seconds == 12.0


async def test_transport_failures_are_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(TransientProviderError):
        await _source(handler).count_records(_credentials(), object_type="customer")


async def test_non_json_bodies_are_data_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(DataError):
        await _source(handler).count_records(_credentials(), object_type="customer")


# -- registration ------------------------------------------------------------


def test_connector_registers_a_source_role_only() -> None:
    assert CONNECTOR.name == "netsuite"
    assert isinstance(CONNECTOR.source, NetSuiteSource)
    assert CONNECTOR.destination is None


def test_source_satisfies_the_spi_protocols() -> None:
    source = NetSuiteSource()
    assert isinstance(source, SourceConnector)
    assert isinstance(source, SupportsRecordCounts)
    assert isinstance(source, SupportsSnapshotBounds)
