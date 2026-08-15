# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Salesforce source — every request is served by an
``httpx.MockTransport``; no org, no network."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from ferry_connector_salesforce import CONNECTOR, HttpSalesforceGateway, SalesforceSource

from ferry.connector import (
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
    SupportsOwnerDirectory,
    SupportsRecordCounts,
    SupportsSnapshotBounds,
    TransientProviderError,
    UnsupportedFeatureError,
    ValidationFailedError,
)

INSTANCE = "https://acme.my.salesforce.com"
DATA_PREFIX = "/services/data/v60.0"


def _credentials(**overrides: Any) -> Credentials:
    values: dict[str, Any] = {
        "provider": "salesforce",
        "connection_id": "conn-1",
        "access_token": "token-1",
        "settings": {"instance_url": INSTANCE},
    }
    values.update(overrides)
    return Credentials(**values)


Handler = Any  # Callable[[httpx.Request], httpx.Response]


def _source(handler: Handler) -> SalesforceSource:
    return SalesforceSource(gateway=HttpSalesforceGateway(transport=httpx.MockTransport(handler)))


def _page(
    records: list[dict[str, Any]],
    *,
    done: bool = True,
    next_records_url: str | None = None,
    total_size: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "totalSize": len(records) if total_size is None else total_size,
        "done": done,
        "records": records,
    }
    if next_records_url is not None:
        payload["nextRecordsUrl"] = next_records_url
    return payload


def _count_payload(count: int) -> dict[str, Any]:
    return {"totalSize": 1, "done": True, "records": [{"expr0": count}]}


def _soql(request: httpx.Request) -> str:
    return request.url.params["q"]


# -- credential validation ---------------------------------------------------


async def test_missing_access_token_is_a_configuration_error() -> None:
    source = _source(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ConfigurationError, match="access token"):
        await source.count_records(_credentials(access_token=None), object_type="Account")


async def test_missing_instance_url_is_a_configuration_error() -> None:
    source = _source(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ConfigurationError, match="instance_url"):
        await source.count_records(_credentials(settings={}), object_type="Account")


async def test_requests_carry_bearer_token_and_hit_query_all() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_count_payload(0))

    # A trailing slash on the configured instance URL must not double up.
    credentials = _credentials(settings={"instance_url": INSTANCE + "/"})
    await _source(handler).count_records(credentials, object_type="Account")
    assert seen[0].url.host == "acme.my.salesforce.com"
    assert seen[0].url.path == f"{DATA_PREFIX}/queryAll/"
    assert seen[0].headers["Authorization"] == "Bearer token-1"


async def test_api_version_override_and_validation() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_count_payload(0))

    source = _source(handler)
    await source.count_records(
        _credentials(settings={"instance_url": INSTANCE, "api_version": "v61.0"}),
        object_type="Account",
    )
    assert seen[0].url.path == "/services/data/v61.0/queryAll/"

    with pytest.raises(ConfigurationError, match="api_version"):
        await source.count_records(
            _credentials(settings={"instance_url": INSTANCE, "api_version": "60"}),
            object_type="Account",
        )


# -- discover_objects --------------------------------------------------------


async def test_discover_objects_filters_maps_and_sorts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"{DATA_PREFIX}/sobjects/"
        return httpx.Response(
            200,
            json={
                "sobjects": [
                    {"name": "Zebra__c", "label": "Zebra", "queryable": True, "custom": True},
                    {"name": "Case", "label": "Case", "queryable": True},
                    {"name": "Account", "label": "Account", "queryable": True},
                    {"name": "AccountHistory", "label": "History", "queryable": False},
                    {
                        "name": "OldThing",
                        "label": "Old",
                        "queryable": True,
                        "deprecatedAndHidden": True,
                    },
                    {"name": "", "label": "Nameless", "queryable": True},
                    {"name": "Contact", "label": "Contact", "queryable": True},
                    "not-a-dict-is-dropped-by-the-gateway",
                ]
            },
        )

    options = await _source(handler).discover_objects(_credentials())
    assert [option.key for option in options] == ["Account", "Contact", "Case", "Zebra__c"]
    account, contact, case, zebra = options
    assert account.default_selected and contact.default_selected
    assert not case.default_selected and not zebra.default_selected
    assert account.canonical_type == "company"
    assert contact.canonical_type == "contact"
    assert case.canonical_type == "ticket"
    assert zebra.canonical_type == "zebra__c"
    assert zebra.custom and not account.custom


async def test_discover_objects_flags_custom_suffix_without_custom_marker() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sobjects": [{"name": "Thing__c", "label": "Thing", "queryable": True}]},
        )

    options = await _source(handler).discover_objects(_credentials())
    assert options[0].custom is True


# -- list_owners -------------------------------------------------------------


async def test_list_owners_skips_incomplete_users_and_maps_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "FROM User" in _soql(request)
        return httpx.Response(
            200,
            json=_page(
                [
                    {"Id": "005A", "Name": "Alice", "Email": "alice@example.com"},
                    {"Id": "005B", "Name": "", "Email": "bob@example.com", "IsActive": False},
                    {"Id": "", "Name": "NoId", "Email": "noid@example.com"},
                    {"Id": "005C", "Name": "NoEmail", "Email": ""},
                ]
            ),
        )

    owners = await _source(handler).list_owners(_credentials())
    assert [(owner.id, owner.email) for owner in owners] == [
        ("005A", "alice@example.com"),
        ("005B", "bob@example.com"),
    ]
    assert owners[0].active is True and owners[0].name == "Alice"
    assert owners[1].active is False and owners[1].name is None


async def test_list_owners_follows_next_records_url() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/queryAll/"):
            return httpx.Response(
                200,
                json=_page(
                    [{"Id": "005A", "Email": "a@example.com"}],
                    done=False,
                    next_records_url=f"{DATA_PREFIX}/queryAll/01g-2000",
                ),
            )
        assert request.url.path == f"{DATA_PREFIX}/queryAll/01g-2000"
        return httpx.Response(200, json=_page([{"Id": "005B", "Email": "b@example.com"}]))

    owners = await _source(handler).list_owners(_credentials())
    assert [owner.id for owner in owners] == ["005A", "005B"]
    assert len(paths) == 2


# -- inventory ---------------------------------------------------------------


def _describe_payload() -> dict[str, Any]:
    return {
        "label": "Account",
        "fields": [
            {
                "name": "Id",
                "label": "Account ID",
                "type": "id",
                "nillable": False,
                "createable": False,
            },
            {
                "name": "Name",
                "label": "Account Name",
                "type": "string",
                "nillable": False,
                "createable": True,
                "unique": False,
            },
            {
                "name": "OwnerId",
                "label": "Owner",
                "type": "reference",
                "nillable": True,
                "createable": True,
                "referenceTo": ["User"],
            },
            {
                "name": "Score__c",
                "label": "Score",
                "type": "double",
                "nillable": True,
                "createable": True,
                "calculated": True,
                "unique": True,
            },
            {"name": "", "label": "dropped: empty name"},
            "dropped: not a dict",
        ],
    }


async def test_inventory_maps_describe_and_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{DATA_PREFIX}/sobjects/Account/describe":
            return httpx.Response(200, json=_describe_payload())
        assert _soql(request) == "SELECT COUNT() FROM Account"
        return httpx.Response(200, json=_count_payload(42))

    inventory = await _source(handler).inventory(_credentials(), object_types=["Account"])
    assert inventory.provider == "salesforce"
    assert inventory.connection_id == "conn-1"
    assert inventory.warnings == []
    (account,) = inventory.objects
    assert account.key == "Account"
    assert account.label == "Account"
    assert account.canonical_type == "company"
    assert account.record_count == 42
    assert account.identity_fields == ["Id"]

    by_key = {field.key: field for field in account.fields}
    assert set(by_key) == {"Id", "Name", "OwnerId", "Score__c"}
    assert by_key["Id"].required is True and by_key["Id"].writable is False
    assert by_key["Name"].required is True and by_key["Name"].writable is True
    assert by_key["OwnerId"].required is False
    assert by_key["OwnerId"].metadata == {"calculated": False, "referenceTo": ["User"]}
    assert by_key["Score__c"].unique is True
    assert by_key["Score__c"].metadata["calculated"] is True
    assert by_key["Score__c"].data_type == "double"


async def test_inventory_count_falls_back_to_total_size_then_zero() -> None:
    def handler_total_size(request: httpx.Request) -> httpx.Response:
        if "describe" in request.url.path:
            return httpx.Response(200, json={"label": "Lead", "fields": []})
        return httpx.Response(200, json={"totalSize": 7, "done": True, "records": []})

    inventory = await _source(handler_total_size).inventory(_credentials(), object_types=["Lead"])
    assert inventory.objects[0].record_count == 7

    def handler_junk(request: httpx.Request) -> httpx.Response:
        if "describe" in request.url.path:
            return httpx.Response(200, json={"label": "Lead", "fields": []})
        return httpx.Response(200, json={"records": [{"expr0": "junk"}], "totalSize": "junk"})

    inventory = await _source(handler_junk).inventory(_credentials(), object_types=["Lead"])
    assert inventory.objects[0].record_count == 0


async def test_inventory_turns_per_object_failures_into_warnings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{DATA_PREFIX}/sobjects/Broken/describe":
            return httpx.Response(404, text="NOT_FOUND: no such object")
        if "describe" in request.url.path:
            return httpx.Response(200, json={"label": "Account", "fields": []})
        return httpx.Response(200, json=_count_payload(1))

    inventory = await _source(handler).inventory(_credentials(), object_types=["Account", "Broken"])
    assert [item.key for item in inventory.objects] == ["Account"]
    assert len(inventory.warnings) == 1
    assert inventory.warnings[0].startswith("Broken: ")
    assert "404" in inventory.warnings[0]


async def test_inventory_rejects_invalid_object_names_outright() -> None:
    source = _source(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ValidationFailedError, match="object type"):
        await source.inventory(_credentials(), object_types=["Account; DROP"])


async def test_inventory_defaults_to_core_crm_objects() -> None:
    described: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "describe" in request.url.path:
            described.append(request.url.path.split("/")[-2])
            return httpx.Response(200, json={"label": "x", "fields": []})
        return httpx.Response(200, json=_count_payload(0))

    await _source(handler).inventory(_credentials())
    assert sorted(described) == ["Account", "Contact", "Lead", "Opportunity"]


# -- read_records ------------------------------------------------------------


async def test_read_records_builds_keyset_soql_and_paginates() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(_soql(request))
        assert request.url.path == f"{DATA_PREFIX}/queryAll/"
        return httpx.Response(200, json=_page([{"Id": "001A"}, {"Id": "001B"}]))

    page = await _source(handler).read_records(
        _credentials(),
        object_type="Account",
        field_keys=["Name", "Website", "Name", "Id", "  "],
        limit=2,
        cursor="001AAA",
    )
    assert queries == [
        "SELECT Id, Name, Website FROM Account WHERE Id > '001AAA' ORDER BY Id ASC LIMIT 2"
    ]
    assert page.object_key == "Account"
    assert [record["Id"] for record in page.records] == ["001A", "001B"]
    assert page.has_more is True  # a full page means more may exist
    assert page.next_cursor == "001B"


async def test_read_records_short_page_ends_pagination() -> None:
    page = await _source(
        lambda request: httpx.Response(200, json=_page([{"Id": "001A"}]))
    ).read_records(_credentials(), object_type="Account", field_keys=[], limit=50)
    assert page.has_more is False
    assert page.next_cursor is None


async def test_read_records_handles_response_split_before_soql_limit() -> None:
    # Salesforce split the response: fewer rows than LIMIT, done=false, a
    # locator present. The connector must keep paginating from the last Id.
    page = await _source(
        lambda request: httpx.Response(
            200,
            json=_page(
                [{"Id": "001A"}, {"Id": "001B"}],
                done=False,
                next_records_url=f"{DATA_PREFIX}/queryAll/01g-2000",
            ),
        )
    ).read_records(_credentials(), object_type="Account", field_keys=["Name"], limit=200)
    assert page.has_more is True
    assert page.next_cursor == "001B"


async def test_read_records_empty_page_never_has_more() -> None:
    page = await _source(
        lambda request: httpx.Response(200, json=_page([], done=False))
    ).read_records(_credentials(), object_type="Account", field_keys=[], limit=10)
    assert page.records == []
    assert page.has_more is False
    assert page.next_cursor is None


async def test_read_records_drops_malformed_records() -> None:
    page = await _source(
        lambda request: httpx.Response(
            200,
            json=_page([{"Id": "001A"}, {"Name": "no id"}, "not-a-dict", {"Id": ""}]),
        )
    ).read_records(_credentials(), object_type="Account", field_keys=[], limit=10)
    assert [record["Id"] for record in page.records] == ["001A"]


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(0, 100), (-5, 1), (1, 1), (200, 200), (999, 200)],
)
async def test_read_records_clamps_limit(limit: int, expected: int) -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(_soql(request))
        return httpx.Response(200, json=_page([]))

    await _source(handler).read_records(
        _credentials(), object_type="Account", field_keys=[], limit=limit
    )
    assert queries[0].endswith(f"LIMIT {expected}")


async def test_read_records_bounded_adds_upper_bound_predicate() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(_soql(request))
        return httpx.Response(200, json=_page([]))

    await _source(handler).read_records_bounded(
        _credentials(),
        object_type="Account",
        field_keys=["Name"],
        limit=100,
        cursor="001AAA",
        upper_bound="001ZZZ",
    )
    assert queries == [
        "SELECT Id, Name FROM Account WHERE Id > '001AAA' AND Id <= '001ZZZ'"
        " ORDER BY Id ASC LIMIT 100"
    ]


async def test_read_records_source_filter_adds_field_and_predicate() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(_soql(request))
        return httpx.Response(200, json=_page([]))

    await _source(handler).read_records(
        _credentials(),
        object_type="Contact",
        field_keys=["Email"],
        limit=10,
        source_filter=SourceFilter(field="Migrate__c", value=False),
    )
    assert queries == [
        "SELECT Id, Email, Migrate__c FROM Contact WHERE Migrate__c = false"
        " ORDER BY Id ASC LIMIT 10"
    ]


async def test_read_records_rejects_bad_input() -> None:
    source = _source(lambda request: httpx.Response(200, json=_page([])))
    credentials = _credentials()
    with pytest.raises(ValidationFailedError, match="object type"):
        await source.read_records(
            credentials, object_type="Account Account", field_keys=[], limit=1
        )
    with pytest.raises(ValidationFailedError, match="field"):
        await source.read_records(
            credentials, object_type="Account", field_keys=["Name,Owner"], limit=1
        )
    with pytest.raises(ValidationFailedError, match="cursor"):
        await source.read_records(
            credentials, object_type="Account", field_keys=[], limit=1, cursor="0' OR 1=1"
        )
    with pytest.raises(ValidationFailedError, match="upper bound"):
        await source.read_records_bounded(
            credentials, object_type="Account", field_keys=[], limit=1, upper_bound="not ok!"
        )
    with pytest.raises(ValidationFailedError, match="cannot target Id"):
        await source.read_records(
            credentials,
            object_type="Account",
            field_keys=[],
            limit=1,
            source_filter=SourceFilter(field="Id"),
        )


# -- counts and snapshot bounds ----------------------------------------------


async def test_count_records_with_and_without_filter() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(_soql(request))
        return httpx.Response(200, json=_count_payload(9))

    source = _source(handler)
    assert await source.count_records(_credentials(), object_type="Account") == 9
    assert (
        await source.count_records(
            _credentials(),
            object_type="Account",
            source_filter=SourceFilter(field="Migrate__c", value=True),
        )
        == 9
    )
    assert queries == [
        "SELECT COUNT() FROM Account",
        "SELECT COUNT() FROM Account WHERE Migrate__c = true",
    ]


async def test_count_records_bounded_combines_predicates() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(_soql(request))
        return httpx.Response(200, json=_count_payload(3))

    count = await _source(handler).count_records_bounded(
        _credentials(),
        object_type="Account",
        source_filter=SourceFilter(field="Migrate__c", value=True),
        upper_bound="001ZZZ",
    )
    assert count == 3
    assert queries == ["SELECT COUNT() FROM Account WHERE Migrate__c = true AND Id <= '001ZZZ'"]

    with pytest.raises(ValidationFailedError, match="upper bound"):
        await _source(handler).count_records_bounded(
            _credentials(), object_type="Account", upper_bound=""
        )


async def test_count_filter_cannot_target_id() -> None:
    source = _source(lambda request: httpx.Response(200, json=_count_payload(0)))
    with pytest.raises(ValidationFailedError, match="cannot target Id"):
        await source.count_records(
            _credentials(), object_type="Account", source_filter=SourceFilter(field="Id")
        )


async def test_high_water_mark_returns_max_id_or_none() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(_soql(request))
        return httpx.Response(200, json=_page([{"Id": "001ZZZ"}]))

    mark = await _source(handler).high_water_mark(
        _credentials(),
        object_type="Account",
        source_filter=SourceFilter(field="Migrate__c", value=True),
    )
    assert mark == "001ZZZ"
    assert queries == ["SELECT Id FROM Account WHERE Migrate__c = true ORDER BY Id DESC LIMIT 1"]

    empty = await _source(lambda request: httpx.Response(200, json=_page([]))).high_water_mark(
        _credentials(), object_type="Account"
    )
    assert empty is None


# -- error mapping -----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (429, RateLimitError),
        (500, TransientProviderError),
        (503, TransientProviderError),
    ],
)
async def test_http_status_maps_to_taxonomy(status: int, expected: type[Exception]) -> None:
    source = _source(lambda request: httpx.Response(status, text="boom"))
    with pytest.raises(expected, match=f"HTTP {status}"):
        await source.count_records(_credentials(), object_type="Account")


async def test_plain_400_is_a_generic_connector_error() -> None:
    source = _source(lambda request: httpx.Response(400, text="MALFORMED_QUERY: unexpected token"))
    with pytest.raises(ConnectorError) as excinfo:
        await source.count_records(_credentials(), object_type="Account")
    assert type(excinfo.value) is ConnectorError
    assert "MALFORMED_QUERY" in str(excinfo.value)


async def test_rate_limit_carries_retry_after() -> None:
    source = _source(
        lambda request: httpx.Response(429, headers={"Retry-After": "30"}, text="limit")
    )
    with pytest.raises(RateLimitError) as excinfo:
        await source.count_records(_credentials(), object_type="Account")
    assert excinfo.value.retry_after_seconds == 30.0
    assert excinfo.value.retryable


async def test_transport_failures_are_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(TransientProviderError, match="connection refused"):
        await _source(handler).count_records(_credentials(), object_type="Account")


async def test_non_json_body_is_a_data_error() -> None:
    source = _source(lambda request: httpx.Response(200, text="<html>proxy page</html>"))
    with pytest.raises(DataError, match="non-JSON"):
        await source.count_records(_credentials(), object_type="Account")


# -- token refresh -----------------------------------------------------------


def _refresh_credentials(**overrides: Any) -> Credentials:
    return _credentials(
        refresh_token="refresh-1", client_id="client-1", client_secret="secret-1", **overrides
    )


async def test_401_refreshes_once_retries_and_caches_the_new_token() -> None:
    token_posts: list[dict[str, str]] = []
    data_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            assert request.method == "POST"
            assert request.url.host == "login.salesforce.com"
            token_posts.append(dict(parse_qsl(request.content.decode())))
            return httpx.Response(200, json={"access_token": "token-2", "instance_url": INSTANCE})
        data_tokens.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer token-1":
            return httpx.Response(401, text="INVALID_SESSION_ID")
        return httpx.Response(200, json=_count_payload(5))

    source = _source(handler)
    credentials = _refresh_credentials()
    assert await source.count_records(credentials, object_type="Account") == 5
    assert token_posts == [
        {
            "grant_type": "refresh_token",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "refresh_token": "refresh-1",
        }
    ]
    assert data_tokens == ["Bearer token-1", "Bearer token-2"]

    # The refreshed token is cached: the next call goes straight through.
    assert await source.count_records(credentials, object_type="Account") == 5
    assert len(token_posts) == 1
    assert data_tokens[-1] == "Bearer token-2"


async def test_refresh_can_move_the_instance_url() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "token-2",
                    "instance_url": "https://acme.lightning.force.com",
                },
            )
        hosts.append(request.url.host)
        if request.headers["Authorization"] == "Bearer token-1":
            return httpx.Response(401, text="expired")
        return httpx.Response(200, json=_count_payload(0))

    await _source(handler).count_records(_refresh_credentials(), object_type="Account")
    assert hosts == ["acme.my.salesforce.com", "acme.lightning.force.com"]


async def test_auth_base_url_setting_targets_sandbox_login() -> None:
    token_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            token_hosts.append(request.url.host)
            return httpx.Response(200, json={"access_token": "token-2"})
        if request.headers["Authorization"] == "Bearer token-1":
            return httpx.Response(401, text="expired")
        return httpx.Response(200, json=_count_payload(0))

    credentials = _refresh_credentials(
        settings={"instance_url": INSTANCE, "auth_base_url": "https://test.salesforce.com"}
    )
    await _source(handler).count_records(credentials, object_type="Account")
    assert token_hosts == ["test.salesforce.com"]


async def test_401_without_refresh_material_fails_immediately() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, text="INVALID_SESSION_ID")

    with pytest.raises(AuthenticationError, match="HTTP 401"):
        await _source(handler).count_records(_credentials(), object_type="Account")
    assert calls == [f"{DATA_PREFIX}/queryAll/"]  # no token-endpoint attempt


async def test_rejected_refresh_grant_is_an_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "expired"}
            )
        return httpx.Response(401, text="expired")

    with pytest.raises(AuthenticationError, match="invalid_grant"):
        await _source(handler).count_records(_refresh_credentials(), object_type="Account")


async def test_refresh_response_without_access_token_is_an_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            return httpx.Response(200, json={"issued_at": "1723800000000"})
        return httpx.Response(401, text="expired")

    with pytest.raises(AuthenticationError, match="missing access_token"):
        await _source(handler).count_records(_refresh_credentials(), object_type="Account")


async def test_still_401_after_refresh_is_an_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            return httpx.Response(200, json={"access_token": "token-2"})
        return httpx.Response(401, text="still no")

    with pytest.raises(AuthenticationError, match="HTTP 401"):
        await _source(handler).count_records(_refresh_credentials(), object_type="Account")


# -- registration ------------------------------------------------------------


def test_registration_shape() -> None:
    assert CONNECTOR.name == "salesforce"
    assert CONNECTOR.destination is None  # the production adapter is source-only
    assert CONNECTOR.source is not None
    assert isinstance(CONNECTOR.source, SourceConnector)
    assert isinstance(CONNECTOR.source, SupportsRecordCounts)
    assert isinstance(CONNECTOR.source, SupportsSnapshotBounds)
    assert isinstance(CONNECTOR.source, SupportsOwnerDirectory)
    assert CONNECTOR.source.provider == "salesforce"
    assert CONNECTOR.source.binding_kind == "channel"


async def test_source_filter_requires_a_field() -> None:
    with pytest.raises(ValueError, match="field"):
        SourceFilter(field="   ")


def test_unsupported_feature_error_is_exported_for_filters() -> None:
    # Guard the public contract: filter misuse surfaces as the taxonomy's
    # UnsupportedFeatureError / ValidationFailedError, both importable from
    # ferry.connector.
    assert issubclass(UnsupportedFeatureError, ConnectorError)
    assert issubclass(ValidationFailedError, ConnectorError)
