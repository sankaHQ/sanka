# SPDX-License-Identifier: Apache-2.0
"""Unit tests that need no HubSpot portal: everything runs against
``httpx.MockTransport``. Covers read pagination and association merging,
batch write chunking and trace-id reconciliation, the contacts upsert path,
association batches, provisioning dry-run vs confirm, the provider-control
throttle/retry loop (with an injected fake clock), invalid-email policies,
error mapping, and the registration shape."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from sanka_connector_hubspot import CONNECTOR, HubSpotDestination, HubSpotSource
from sanka_connector_hubspot._base import (
    HubSpotGateway,
    HubSpotRequestError,
    custom_object_match_token,
    hubspot_property_type,
    mapped_error,
)
from sanka_connector_hubspot._destination import (
    _custom_object_schema_is_compatible,
    _custom_object_schema_payload,
    _pipeline_is_compatible,
    _pipeline_payload,
    _stage_probability,
)

from sanka.connector import (
    AuthenticationError,
    BatchWriteInput,
    ConflictError,
    Credentials,
    CustomObjectDefinition,
    CustomObjectProperty,
    DestinationConnector,
    NotFoundError,
    PermissionDeniedError,
    PipelineDefinition,
    PipelineStage,
    PropertyDefinition,
    RateLimitError,
    RelationshipWrite,
    SourceConnector,
    SupportsBatchRelationshipWrites,
    SupportsBatchWrites,
    SupportsOwnerDirectory,
    SupportsRetryMetrics,
    SupportsSchemaProvisioning,
    TransientProviderError,
    UnsupportedFeatureError,
    ValidationFailedError,
    WriteOptions,
)

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def credentials() -> Credentials:
    return Credentials(provider="hubspot", connection_id="conn-1", access_token="unit-test-token")


class Recorder:
    """Wraps a responder so tests can assert on the captured requests."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


class FakeClock:
    """Deterministic monotonic clock + sleep for the provider-control loop."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def gateway(handler: Callable[[httpx.Request], httpx.Response]) -> HubSpotGateway:
    return HubSpotGateway(transport=httpx.MockTransport(handler))


def destination(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: FakeClock | None = None,
    min_interval_seconds: float = 0.0,
) -> HubSpotDestination:
    if clock is None:
        return HubSpotDestination(
            gateway=gateway(handler),
            min_interval_seconds=min_interval_seconds,
        )
    return HubSpotDestination(
        gateway=gateway(handler),
        min_interval_seconds=min_interval_seconds,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def source(handler: Callable[[httpx.Request], httpx.Response]) -> HubSpotSource:
    return HubSpotSource(gateway=gateway(handler))


def body(request: httpx.Request) -> dict[str, object]:
    parsed = json.loads(request.content.decode())
    assert isinstance(parsed, dict)
    return parsed


def write_options(**overrides: object) -> WriteOptions:
    base: dict[str, object] = {"conflict_policy": "create"}
    base.update(overrides)
    return WriteOptions(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Registration shape
# --------------------------------------------------------------------------


def test_registration_shape() -> None:
    assert CONNECTOR.name == "hubspot"
    assert isinstance(CONNECTOR.source, SourceConnector)
    assert isinstance(CONNECTOR.destination, DestinationConnector)
    assert isinstance(CONNECTOR.destination, SupportsBatchWrites)
    assert isinstance(CONNECTOR.destination, SupportsBatchRelationshipWrites)
    assert isinstance(CONNECTOR.destination, SupportsSchemaProvisioning)
    assert isinstance(CONNECTOR.destination, SupportsOwnerDirectory)
    assert isinstance(CONNECTOR.destination, SupportsRetryMetrics)
    assert CONNECTOR.source is not None
    assert CONNECTOR.destination is not None
    assert CONNECTOR.source.provider == "hubspot"
    assert CONNECTOR.destination.provider == "hubspot"
    assert CONNECTOR.source.binding_kind == "channel"


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        ("bool", ("bool", "booleancheckbox")),
        ("Boolean", ("bool", "booleancheckbox")),
        ("checkbox", ("bool", "booleancheckbox")),
        ("currency", ("number", "number")),
        ("int", ("number", "number")),
        ("double", ("number", "number")),
        ("percent", ("number", "number")),
        ("date", ("date", "date")),
        ("datetime", ("datetime", "date")),
        ("timestamp", ("datetime", "date")),
        ("textarea", ("string", "textarea")),
        ("long_text", ("string", "textarea")),
        ("phone", ("string", "phonenumber")),
        ("picklist", ("string", "text")),
        (None, ("string", "text")),
    ],
)
def test_hubspot_property_type(source_type: str | None, expected: tuple[str, str]) -> None:
    assert hubspot_property_type(source_type) == expected


def test_custom_object_match_token() -> None:
    assert custom_object_match_token("Order__c") == "order"
    assert custom_object_match_token("  Purchase-Orders ") == "purchaseorders"
    assert custom_object_match_token(None) == ""


def test_stage_probability_ramp() -> None:
    assert _stage_probability(0, 1) == 1.0
    assert _stage_probability(0, 3) == 0.0
    assert _stage_probability(1, 3) == 0.5
    assert _stage_probability(2, 3) == 1.0


def test_automatic_target_object() -> None:
    dest = HubSpotDestination(gateway=gateway(lambda request: httpx.Response(500)))
    assert dest.automatic_target_object("company") == "companies"
    assert dest.automatic_target_object("contact") == "contacts"
    assert dest.automatic_target_object("deal") == "deals"
    assert dest.automatic_target_object("ticket") == "tickets"
    assert dest.automatic_target_object("order") is None


# --------------------------------------------------------------------------
# Source: discovery + inventory
# --------------------------------------------------------------------------


def _schemas_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "objectTypeId": "2-123",
                "name": "orders",
                "labels": {"singular": "Order", "plural": "Orders"},
            },
            {
                "objectTypeId": "2-999",
                "name": "retired",
                "labels": {"singular": "Retired", "plural": "Retired"},
                "archived": True,
            },
        ]
    }


async def test_discover_objects_standard_plus_custom() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/schemas"
        return httpx.Response(200, json=_schemas_payload())

    options = await source(respond).discover_objects(credentials())
    keys = [item.key for item in options]
    assert keys == ["companies", "contacts", "deals", "tickets", "2-123"]
    standard = options[0]
    assert standard.default_selected is True
    assert standard.canonical_type == "company"
    custom = options[-1]
    assert custom.custom is True
    assert custom.label == "Orders"
    assert custom.canonical_type == "orders"


async def test_source_inventory_fields_and_identity() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/schemas":
            return httpx.Response(200, json={"results": []})
        if request.url.path == "/crm/v3/properties/companies":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"name": "name", "label": "Name", "type": "string"},
                        {"name": "domain", "label": "Domain", "type": "string"},
                        {
                            "name": "customer_code",
                            "label": "Customer code",
                            "type": "string",
                            "hasUniqueValue": True,
                        },
                        {
                            "name": "locked",
                            "label": "Locked",
                            "type": "string",
                            "modificationMetadata": {"readOnlyValue": True},
                        },
                        {"name": "score", "label": "Score", "type": "number", "calculated": True},
                    ]
                },
            )
        if request.url.path == "/crm/v3/objects/companies/search":
            return httpx.Response(200, json={"total": 42, "results": []})
        raise AssertionError(f"unexpected request {request.url.path}")

    inventory = await source(respond).inventory(credentials(), object_types=["companies"])
    assert inventory.provider == "hubspot"
    assert inventory.connection_id == "conn-1"
    (companies,) = inventory.objects
    assert companies.key == "companies"
    assert companies.canonical_type == "company"
    assert companies.record_count == 42
    assert companies.identity_fields == ["customer_code", "domain"]
    by_key = {field.key: field for field in companies.fields}
    assert by_key["name"].writable is True
    assert by_key["locked"].writable is False
    assert by_key["score"].writable is False
    assert by_key["customer_code"].unique is True


# --------------------------------------------------------------------------
# Source: reads
# --------------------------------------------------------------------------


async def test_read_records_paginates_and_merges_associations() -> None:
    search_calls: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/objects/contacts/search":
            payload = body(request)
            search_calls.append(payload)
            if payload.get("after"):
                return httpx.Response(
                    200,
                    json={
                        "results": [{"id": "3", "properties": {"email": "c@example.com"}}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "1", "properties": {"email": "a@example.com"}},
                        {"id": "2", "properties": {"email": "b@example.com"}},
                    ],
                    "paging": {"next": {"after": "cursor-2"}},
                },
            )
        if request.url.path == "/crm/v4/associations/contacts/companies/batch/read":
            payload = body(request)
            inputs = payload["inputs"]
            assert isinstance(inputs, list)
            if {"id": "1"} in inputs:
                assert payload == {"inputs": [{"id": "1"}, {"id": "2"}]}
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "from": {"id": "1"},
                                "to": [{"toObjectId": 901}, {"toObjectId": 902}],
                            },
                        ]
                    },
                )
            return httpx.Response(200, json={"results": []})
        raise AssertionError(f"unexpected request {request.url.path}")

    reader = source(respond)
    page = await reader.read_records(
        credentials(),
        object_type="contacts",
        field_keys=["email", "associations.companies"],
        limit=500,
    )
    assert search_calls[0]["limit"] == 200
    assert search_calls[0]["properties"] == ["email"]
    assert page.has_more is True
    assert page.next_cursor == "cursor-2"
    assert page.records[0]["associations.companies"] == ["901", "902"]
    # The batch read pre-seeds every requested id, so records without
    # associations still carry an explicit empty list (production behavior).
    assert page.records[1]["associations.companies"] == []

    page_two = await reader.read_records(
        credentials(),
        object_type="contacts",
        field_keys=["email", "associations.companies"],
        limit=100,
        cursor="cursor-2",
    )
    assert search_calls[-1]["after"] == "cursor-2"
    assert page_two.has_more is False
    assert page_two.next_cursor is None


async def test_read_records_rejects_source_filters() -> None:
    from sanka.connector import SourceFilter

    with pytest.raises(UnsupportedFeatureError):
        await source(lambda request: httpx.Response(200, json={})).read_records(
            credentials(),
            object_type="contacts",
            field_keys=["email"],
            limit=10,
            source_filter=SourceFilter(field="active"),
        )


# --------------------------------------------------------------------------
# Error mapping (through the unthrottled source path)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (500, TransientProviderError),
        (503, TransientProviderError),
        (400, ValidationFailedError),
    ],
)
async def test_error_mapping_by_status(status: int, expected: type[Exception]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "nope"})

    with pytest.raises(expected) as excinfo:
        await source(respond).read_records(
            credentials(), object_type="contacts", field_keys=["email"], limit=10
        )
    assert excinfo.value.details["statusCode"] == status  # type: ignore[attr-defined]


async def test_error_mapping_rate_limit_retry_after() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3.5"}, json={"message": "slow down"})

    with pytest.raises(RateLimitError) as excinfo:
        await source(respond).read_records(
            credentials(), object_type="contacts", field_keys=["email"], limit=10
        )
    assert excinfo.value.retry_after_seconds == 3.5
    assert excinfo.value.retryable is True


async def test_error_mapping_transport_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(TransientProviderError):
        await source(respond).read_records(
            credentials(), object_type="contacts", field_keys=["email"], limit=10
        )


async def test_missing_access_token_is_authentication_error() -> None:
    with pytest.raises(AuthenticationError):
        await source(lambda request: httpx.Response(200, json={})).read_records(
            Credentials(provider="hubspot"),
            object_type="contacts",
            field_keys=["email"],
            limit=10,
        )


def test_scope_error_message() -> None:
    error = mapped_error(
        HubSpotRequestError(
            "Missing HubSpot scope. Reconnect HubSpot with the required automation, CRM, "
            "or marketing scope for this operation.",
            status_code=403,
        )
    )
    assert isinstance(error, PermissionDeniedError)
    assert "Missing HubSpot scope" in str(error)


# --------------------------------------------------------------------------
# Destination: single-record writes and conflict policies
# --------------------------------------------------------------------------


async def test_write_record_create_searches_identity_then_creates() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/objects/contacts/search":
            payload = body(request)
            assert payload["filterGroups"] == [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": "a@example.com"}]}
            ]
            return httpx.Response(200, json={"results": []})
        if request.url.path == "/crm/v3/objects/contacts":
            assert body(request) == {"properties": {"email": "a@example.com"}}
            return httpx.Response(201, json={"id": "101"})
        raise AssertionError(f"unexpected request {request.url.path}")

    recorder = Recorder(respond)
    result = await destination(recorder).write_record(
        credentials(),
        object_type="contacts",
        properties={"email": "a@example.com"},
        options=write_options(),
    )
    assert result.status == "created"
    assert result.destination_record_id == "101"
    assert [request.url.path for request in recorder.requests] == [
        "/crm/v3/objects/contacts/search",
        "/crm/v3/objects/contacts",
    ]


async def test_write_record_skip_existing_and_update_existing() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/objects/contacts/search":
            return httpx.Response(
                200, json={"results": [{"id": "900", "properties": {"email": "a@example.com"}}]}
            )
        if request.url.path == "/crm/v3/objects/contacts/900" and request.method == "PATCH":
            return httpx.Response(200, json={"id": "900"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    skip = await destination(respond).write_record(
        credentials(),
        object_type="contacts",
        properties={"email": "a@example.com"},
        options=write_options(conflict_policy="skip_existing"),
    )
    assert skip.status == "skipped"
    assert skip.destination_record_id == "900"
    assert skip.message == "Existing destination record matched an identity field."

    update = await destination(respond).write_record(
        credentials(),
        object_type="contacts",
        properties={"email": "a@example.com", "firstname": "Ada"},
        options=write_options(conflict_policy="update_existing"),
    )
    assert update.status == "updated"
    assert update.destination_record_id == "900"


async def test_write_record_custom_object_requires_identity() -> None:
    dest = destination(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ValidationFailedError) as excinfo:
        await dest.write_record(
            credentials(),
            object_type="2-123",
            properties={"order_number": "42"},
            options=write_options(conflict_policy="skip_existing"),
        )
    assert excinfo.value.details["code"] == "SANKA_MIGRATE_DESTINATION_IDENTITY_REQUIRED"

    with pytest.raises(ValidationFailedError) as excinfo:
        await dest.write_record(
            credentials(),
            object_type="2-123",
            properties={"order_number": ""},
            options=write_options(
                conflict_policy="skip_existing", identity_fields=["order_number"]
            ),
        )
    assert excinfo.value.details["code"] == "SANKA_MIGRATE_DESTINATION_IDENTITY_VALUE_REQUIRED"


async def test_write_record_missing_created_id_is_data_error() -> None:
    from sanka.connector import DataError

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(201, json={"status": "ok"})

    with pytest.raises(DataError):
        await destination(respond).write_record(
            credentials(),
            object_type="contacts",
            properties={"email": "a@example.com"},
            options=write_options(),
        )


# --------------------------------------------------------------------------
# Destination: invalid-email policies
# --------------------------------------------------------------------------


def _invalid_email_response() -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "status": "error",
            "category": "VALIDATION_ERROR",
            "message": 'Property values were not valid: [{"error":"INVALID_EMAIL"}]',
        },
    )


async def test_invalid_email_block_policy_surfaces_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        return _invalid_email_response()

    with pytest.raises(ValidationFailedError):
        await destination(respond).write_record(
            credentials(),
            object_type="contacts",
            properties={"email": "broken@@example.com"},
            options=write_options(),
        )


async def test_invalid_email_leave_empty_moves_email_to_audit_field() -> None:
    create_bodies: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        if request.url.path == "/crm/v3/objects/contacts":
            payload = body(request)
            create_bodies.append(payload)
            properties = payload["properties"]
            assert isinstance(properties, dict)
            if "email" in properties:
                return _invalid_email_response()
            return httpx.Response(201, json={"id": "310"})
        raise AssertionError(f"unexpected request {request.url.path}")

    result = await destination(respond).write_record(
        credentials(),
        object_type="contacts",
        properties={"email": "broken@@example.com", "customer_code": "C-9"},
        options=write_options(
            identity_fields=["email", "customer_code"],
            invalid_email_policy="leave_empty",
            invalid_email_audit_field="legacy_email",
        ),
    )
    assert result.status == "created"
    assert result.destination_record_id == "310"
    assert result.message is not None
    assert "HubSpot rejected the standard email value" in result.message
    fallback_properties = create_bodies[-1]["properties"]
    assert isinstance(fallback_properties, dict)
    assert "email" not in fallback_properties
    assert fallback_properties["legacy_email"] == "broken@@example.com"
    assert fallback_properties["customer_code"] == "C-9"


async def test_invalid_email_duplicate_conflict_message() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        payload = body(request)
        properties = payload["properties"]
        assert isinstance(properties, dict)
        if "email" in properties:
            return httpx.Response(409, json={"message": "Contact already exists"})
        return httpx.Response(201, json={"id": "311"})

    result = await destination(respond).write_record(
        credentials(),
        object_type="contacts",
        properties={"email": "dup@example.com", "customer_code": "C-10"},
        options=write_options(
            identity_fields=["email", "customer_code"],
            invalid_email_policy="leave_empty",
            invalid_email_audit_field="legacy_email",
        ),
    )
    assert result.status == "created"
    assert result.message is not None
    assert result.message.startswith("HubSpot email uniqueness conflict")


@pytest.mark.parametrize(
    ("audit_field", "identity_fields", "properties", "code"),
    [
        (
            None,
            ["email", "customer_code"],
            {"email": "x@@example.com", "customer_code": "C-1"},
            "SANKA_MIGRATE_INVALID_EMAIL_AUDIT_FIELD_REQUIRED",
        ),
        (
            "customer_code",
            ["email", "customer_code"],
            {"email": "x@@example.com", "customer_code": "C-1"},
            "SANKA_MIGRATE_INVALID_EMAIL_AUDIT_FIELD_IDENTITY_CONFLICT",
        ),
        (
            "legacy_email",
            ["email"],
            {"email": "x@@example.com"},
            "SANKA_MIGRATE_INVALID_EMAIL_ALTERNATE_IDENTITY_REQUIRED",
        ),
    ],
)
async def test_invalid_email_leave_empty_guardrails(
    audit_field: str | None,
    identity_fields: list[str],
    properties: dict[str, object],
    code: str,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        return _invalid_email_response()

    with pytest.raises(ValidationFailedError) as excinfo:
        await destination(respond).write_record(
            credentials(),
            object_type="contacts",
            properties=dict(properties),
            options=write_options(
                identity_fields=identity_fields,
                invalid_email_policy="leave_empty",
                invalid_email_audit_field=audit_field,
            ),
        )
    assert excinfo.value.details["code"] == code


# --------------------------------------------------------------------------
# Destination: batch writes
# --------------------------------------------------------------------------


async def test_write_records_chunks_at_100_and_reconciles_traces() -> None:
    create_calls: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/objects/deals/batch/create"
        payload = body(request)
        create_calls.append(payload)
        inputs = payload["inputs"]
        assert isinstance(inputs, list)
        results = []
        errors = []
        for row in inputs:
            assert isinstance(row, dict)
            trace_id = row["objectWriteTraceId"]
            if trace_id == "trace-5":
                errors.append(
                    {
                        "status": "error",
                        "message": "boom",
                        "context": {"objectWriteTraceId": ["trace-5"]},
                    }
                )
                continue
            results.append(
                {"id": f"dst-{trace_id}", "objectWriteTraceId": trace_id, "properties": {}}
            )
        payload_out: dict[str, object] = {"results": results}
        if errors:
            payload_out["errors"] = errors
        return httpx.Response(207 if errors else 201, json=payload_out)

    records = [
        BatchWriteInput(trace_id=f"trace-{index}", properties={"dealname": f"Deal {index}"})
        for index in range(250)
    ]
    results = await destination(respond).write_records(
        credentials(),
        object_type="deals",
        records=records,
        options=write_options(),
    )
    assert [len(call["inputs"]) for call in create_calls] == [100, 100, 50]  # type: ignore[arg-type]
    assert len(results) == 250
    by_trace = {result.trace_id: result for result in results}
    assert by_trace["trace-0"].status == "created"
    assert by_trace["trace-0"].destination_record_id == "dst-trace-0"
    assert by_trace["trace-5"].status == "failed"
    assert by_trace["trace-5"].message == "boom"
    assert [result.trace_id for result in results] == [f"trace-{index}" for index in range(250)]


async def test_write_records_rejects_duplicate_trace_ids() -> None:
    with pytest.raises(ValidationFailedError):
        await destination(lambda request: httpx.Response(200, json={})).write_records(
            credentials(),
            object_type="deals",
            records=[
                BatchWriteInput(trace_id="same", properties={}),
                BatchWriteInput(trace_id="same", properties={}),
            ],
            options=write_options(),
        )


async def test_write_records_skip_existing_uses_identity_search() -> None:
    search_bodies: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/objects/contacts/search":
            payload = body(request)
            search_bodies.append(payload)
            return httpx.Response(
                200,
                json={"results": [{"id": "900", "properties": {"email": "a@example.com"}}]},
            )
        if request.url.path == "/crm/v3/objects/contacts/batch/create":
            payload = body(request)
            inputs = payload["inputs"]
            assert isinstance(inputs, list)
            assert len(inputs) == 1
            return httpx.Response(
                201,
                json={
                    "results": [
                        {"id": "901", "objectWriteTraceId": "t-b", "properties": {}},
                    ]
                },
            )
        raise AssertionError(f"unexpected request {request.url.path}")

    results = await destination(respond).write_records(
        credentials(),
        object_type="contacts",
        records=[
            BatchWriteInput(trace_id="t-a", properties={"email": "a@example.com"}),
            BatchWriteInput(trace_id="t-b", properties={"email": "b@example.com"}),
        ],
        options=write_options(conflict_policy="skip_existing"),
    )
    filters = search_bodies[0]["filterGroups"][0]["filters"][0]  # type: ignore[index]
    assert filters["operator"] == "IN"
    assert sorted(filters["values"]) == ["a@example.com", "b@example.com"]
    assert results[0].status == "skipped"
    assert results[0].destination_record_id == "900"
    assert results[1].status == "created"
    assert results[1].destination_record_id == "901"


async def test_write_records_update_existing_contacts_uses_native_upsert() -> None:
    upsert_bodies: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/objects/contacts/batch/upsert"
        payload = body(request)
        upsert_bodies.append(payload)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "500", "objectWriteTraceId": "t-1", "new": True},
                    {"id": "501", "objectWriteTraceId": "t-2", "new": False},
                ]
            },
        )

    results = await destination(respond).write_records(
        credentials(),
        object_type="contacts",
        records=[
            BatchWriteInput(
                trace_id="t-1", properties={"email": "a@example.com", "firstname": "Ada"}
            ),
            BatchWriteInput(
                trace_id="t-2", properties={"email": "b@example.com", "firstname": "Bo"}
            ),
        ],
        options=write_options(conflict_policy="update_existing"),
    )
    inputs = upsert_bodies[0]["inputs"]
    assert isinstance(inputs, list)
    first = inputs[0]
    assert isinstance(first, dict)
    assert first["id"] == "a@example.com"
    assert first["idProperty"] == "email"
    assert first["properties"] == {"firstname": "Ada"}
    assert results[0].status == "created"
    assert results[0].destination_record_id == "500"
    assert results[1].status == "updated"
    assert results[1].destination_record_id == "501"


async def test_write_records_batch_409_reconciles_by_identity() -> None:
    batch_attempted = False
    in_searches = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal batch_attempted, in_searches
        if request.url.path == "/crm/v3/objects/companies/batch/create":
            batch_attempted = True
            return httpx.Response(409, json={"message": "duplicate domains"})
        if request.url.path == "/crm/v3/objects/companies/search":
            payload = body(request)
            filters = payload["filterGroups"][0]["filters"][0]  # type: ignore[index]
            if filters["operator"] == "IN":
                in_searches += 1
                if in_searches == 1:
                    # The pre-batch sweep sees nothing; a concurrent create
                    # lands before the batch call, which then 409s.
                    return httpx.Response(200, json={"results": []})
                return httpx.Response(
                    200,
                    json={"results": [{"id": "700", "properties": {"domain": "a.example"}}]},
                )
            return httpx.Response(200, json={"results": []})
        if request.url.path == "/crm/v3/objects/companies":
            return httpx.Response(201, json={"id": "701"})
        raise AssertionError(f"unexpected request {request.url.path}")

    results = await destination(respond).write_records(
        credentials(),
        object_type="companies",
        records=[
            BatchWriteInput(trace_id="t-a", properties={"domain": "a.example"}),
            BatchWriteInput(trace_id="t-b", properties={"domain": "b.example"}),
        ],
        options=write_options(conflict_policy="skip_existing"),
    )
    assert batch_attempted is True
    assert in_searches == 2
    assert results[0].status == "skipped"
    assert results[0].destination_record_id == "700"
    assert results[0].message == "A concurrent HubSpot create was reconciled by identity."
    assert results[1].status == "created"
    assert results[1].destination_record_id == "701"


# --------------------------------------------------------------------------
# Destination: relationship writes
# --------------------------------------------------------------------------


def _relationship(
    trace_id: str,
    *,
    category: str | None = None,
    type_id: int | None = None,
    record_id: str = "1",
    related_record_id: str = "2",
) -> RelationshipWrite:
    return RelationshipWrite(
        trace_id=trace_id,
        object_type="deals",
        record_id=record_id,
        relationship_field="company",
        related_object_type="companies",
        related_record_id=related_record_id,
        association_category=category,
        association_type_id=type_id,
    )


async def test_write_relationships_groups_by_association_type() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, body(request)))
        return httpx.Response(201, json={"status": "COMPLETE"})

    results = await destination(respond).write_relationships(
        credentials(),
        relationships=[
            _relationship("r-1", category="HUBSPOT_DEFINED", type_id=5),
            _relationship("r-2", category="HUBSPOT_DEFINED", type_id=5, record_id="9"),
            _relationship("r-3"),
        ],
    )
    assert all(result.status == "linked" for result in results)
    paths = sorted(path for path, _payload in calls)
    assert paths == [
        "/crm/v4/associations/deals/companies/batch/associate/default",
        "/crm/v4/associations/deals/companies/batch/create",
    ]
    typed_payload = next(p for path, p in calls if path.endswith("batch/create"))
    typed_inputs = typed_payload["inputs"]
    assert isinstance(typed_inputs, list)
    assert len(typed_inputs) == 2
    first = typed_inputs[0]
    assert isinstance(first, dict)
    assert first["types"] == [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 5}]
    default_payload = next(p for path, p in calls if path.endswith("associate/default"))
    default_inputs = default_payload["inputs"]
    assert isinstance(default_inputs, list)
    assert "types" not in default_inputs[0]


async def test_write_relationships_invalid_pairing_fails_only_that_row() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"status": "COMPLETE"})

    results = await destination(respond).write_relationships(
        credentials(),
        relationships=[
            _relationship("bad", category="HUBSPOT_DEFINED", type_id=None),
            _relationship("good"),
        ],
    )
    assert results[0].status == "failed"
    assert results[0].message is not None
    assert "must be provided together" in results[0].message
    assert results[1].status == "linked"


async def test_write_relationships_batch_errors_fail_chunk() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            207,
            json={"errors": [{"message": "association denied"}], "results": []},
        )

    results = await destination(respond).write_relationships(
        credentials(),
        relationships=[_relationship("r-1"), _relationship("r-2")],
    )
    assert [result.status for result in results] == ["failed", "failed"]
    assert results[0].message == "association denied"


async def test_write_relationships_chunks_at_100() -> None:
    calls: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(body(request))
        return httpx.Response(201, json={"status": "COMPLETE"})

    relationships = [_relationship(f"r-{index}", record_id=str(index)) for index in range(150)]
    results = await destination(respond).write_relationships(
        credentials(), relationships=relationships
    )
    assert [len(call["inputs"]) for call in calls] == [100, 50]  # type: ignore[arg-type]
    assert all(result.status == "linked" for result in results)


async def test_write_relationship_single_requires_paired_type() -> None:
    with pytest.raises(ValidationFailedError):
        await destination(lambda request: httpx.Response(200, json={})).write_relationship(
            credentials(),
            relationship=_relationship("solo", category="USER_DEFINED", type_id=None),
        )


async def test_write_relationship_single_links() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v4/associations/deals/companies/batch/associate/default"
        return httpx.Response(201, json={"status": "COMPLETE"})

    result = await destination(respond).write_relationship(
        credentials(), relationship=_relationship("solo")
    )
    assert result.status == "linked"


# --------------------------------------------------------------------------
# Destination: provisioning
# --------------------------------------------------------------------------


def _property_definitions() -> list[PropertyDefinition]:
    return [
        PropertyDefinition(
            source_field="Industry",
            target_object="companies",
            internal_name="industry_segment",
            label="Industry segment",
            source_type="picklist",
        ),
        PropertyDefinition(
            source_field="AnnualRevenue",
            target_object="companies",
            internal_name="annual_revenue_source",
            label="Annual revenue (source)",
            source_type="currency",
        ),
        PropertyDefinition(
            source_field="LegacyFlag",
            target_object="2-123",
            internal_name="legacy_flag",
            label="Legacy flag",
            source_type="bool",
        ),
    ]


async def test_reconcile_properties_dry_run_is_pure() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/properties/companies"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "industry_segment",
                        "label": "Industry segment",
                        "type": "string",
                        "fieldType": "text",
                    }
                ]
            },
        )

    recorder = Recorder(respond)
    results = await destination(recorder).reconcile_properties(
        credentials(),
        definitions=_property_definitions(),
        confirm=False,
    )
    assert all(request.method == "GET" for request in recorder.requests)
    by_name = {result.internal_name: result for result in results}
    assert by_name["industry_segment"].status == "existing"
    assert by_name["annual_revenue_source"].status == "would_create"
    assert by_name["annual_revenue_source"].target_type == "number"
    assert by_name["legacy_flag"].status == "unsupported"


async def test_reconcile_properties_conflicts() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "industry_segment",
                        "label": "Industry segment",
                        "type": "enumeration",
                        "fieldType": "select",
                    }
                ]
            },
        )

    duplicate = PropertyDefinition(
        source_field="Industry2",
        target_object="companies",
        internal_name="industry_segment",
        label="Industry segment (copy)",
        source_type="picklist",
    )
    results = await destination(respond).reconcile_properties(
        credentials(),
        definitions=[_property_definitions()[0], duplicate],
        confirm=False,
    )
    assert all(result.status == "conflict" for result in results)
    assert results[0].message is not None
    assert "same HubSpot internal name" in results[0].message


async def test_reconcile_properties_confirm_creates_missing() -> None:
    created_payloads: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"results": []})
        payload = body(request)
        created_payloads.append(payload)
        return httpx.Response(
            201,
            json={
                "name": payload["name"],
                "label": payload["label"],
                "type": payload["type"],
                "fieldType": payload["fieldType"],
            },
        )

    bool_definition = PropertyDefinition(
        source_field="IsActive",
        target_object="contacts",
        internal_name="is_active_source",
        label="Is active (source)",
        source_type="bool",
    )
    results = await destination(respond).reconcile_properties(
        credentials(),
        definitions=[bool_definition],
        confirm=True,
    )
    assert results[0].status == "created"
    payload = created_payloads[0]
    assert payload["groupName"] == "contactinformation"
    assert payload["type"] == "bool"
    assert payload["fieldType"] == "booleancheckbox"
    options = payload["options"]
    assert isinstance(options, list)
    assert [option["value"] for option in options] == ["true", "false"]  # type: ignore[index]
    assert payload["description"] == "Migrated from IsActive with Sanka Migrate."


def _pipeline_definition() -> PipelineDefinition:
    return PipelineDefinition(
        key="sales",
        label="Sales Pipeline",
        object_type="deals",
        stages=[
            PipelineStage(key="new", label="New", display_order=0),
            PipelineStage(key="review", label="Review", display_order=1),
            PipelineStage(key="won", label="Won", display_order=2),
        ],
    )


def _custom_object_definition() -> CustomObjectDefinition:
    return CustomObjectDefinition(
        key="orders",
        source_object="Order",
        internal_name="orders",
        singular_label="Order",
        plural_label="Orders",
        primary_display_property="order_number",
        associated_objects=["CONTACT"],
    )


async def test_reconcile_resources_dry_run_is_pure() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/pipelines/deals":
            return httpx.Response(200, json={"results": []})
        if request.url.path == "/crm/v3/schemas":
            return httpx.Response(200, json={"results": []})
        raise AssertionError(f"unexpected request {request.url.path}")

    recorder = Recorder(respond)
    results = await destination(recorder).reconcile_resources(
        credentials(),
        pipelines=[_pipeline_definition()],
        custom_objects=[_custom_object_definition()],
        confirm=False,
    )
    assert all(request.method == "GET" for request in recorder.requests)
    assert [result.status for result in results] == ["would_create", "would_create"]
    assert results[0].resource_type == "pipeline"
    assert results[1].resource_type == "custom_object"


async def test_reconcile_resources_existing_pipeline_reports_stage_ids() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/pipelines/deals"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "p-1",
                        "label": "sales pipeline",
                        "stages": [
                            {"id": "s-1", "label": "New", "displayOrder": 0},
                            {"id": "s-2", "label": "Review", "displayOrder": 1},
                            {"id": "s-3", "label": "Won", "displayOrder": 2},
                        ],
                    }
                ]
            },
        )

    (result,) = await destination(respond).reconcile_resources(
        credentials(),
        pipelines=[_pipeline_definition()],
        custom_objects=[],
        confirm=False,
    )
    assert result.status == "existing"
    assert result.provider_id == "p-1"
    assert result.stage_ids == {"new": "s-1", "review": "s-2", "won": "s-3"}


async def test_reconcile_resources_confirm_creates_pipeline_with_probability_ramp() -> None:
    created: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"results": []})
        payload = body(request)
        created.append(payload)
        return httpx.Response(
            201,
            json={
                "id": "p-9",
                "label": payload["label"],
                "stages": [
                    {"id": f"s-{index}", "label": stage["label"]}  # type: ignore[index]
                    for index, stage in enumerate(payload["stages"])  # type: ignore[arg-type]
                ],
            },
        )

    (result,) = await destination(respond).reconcile_resources(
        credentials(),
        pipelines=[_pipeline_definition()],
        custom_objects=[],
        confirm=True,
    )
    assert result.status == "created"
    assert result.provider_id == "p-9"
    assert set(result.stage_ids) == {"new", "review", "won"}
    stages = created[0]["stages"]
    assert isinstance(stages, list)
    assert [stage["metadata"]["probability"] for stage in stages] == ["0.0", "0.5", "1.0"]  # type: ignore[index]


async def test_reconcile_resources_existing_custom_object_compatibility() -> None:
    schema = {
        "objectTypeId": "2-77",
        "name": "orders",
        "labels": {"singular": "Order", "plural": "Orders"},
        "primaryDisplayProperty": "order_number",
        "properties": [{"name": "order_number"}],
        "associatedObjects": ["CONTACT"],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/schemas":
            return httpx.Response(200, json={"results": [schema]})
        if request.url.path == "/crm/v3/schemas/2-77":
            return httpx.Response(200, json=schema)
        raise AssertionError(f"unexpected request {request.url.path}")

    (result,) = await destination(respond).reconcile_resources(
        credentials(),
        pipelines=[],
        custom_objects=[_custom_object_definition()],
        confirm=False,
    )
    assert result.status == "existing"
    assert result.provider_id == "2-77"

    incompatible = dict(schema)
    incompatible["labels"] = {"singular": "Shipment", "plural": "Shipments"}

    def respond_conflict(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/schemas":
            return httpx.Response(200, json={"results": [incompatible]})
        return httpx.Response(200, json=incompatible)

    (conflict,) = await destination(respond_conflict).reconcile_resources(
        credentials(),
        pipelines=[],
        custom_objects=[_custom_object_definition()],
        confirm=False,
    )
    assert conflict.status == "conflict"


async def test_reconcile_resources_confirm_creates_custom_object_schema() -> None:
    created: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"results": []})
        payload = body(request)
        created.append(payload)
        return httpx.Response(201, json={"objectTypeId": "2-88"})

    (result,) = await destination(respond).reconcile_resources(
        credentials(),
        pipelines=[],
        custom_objects=[_custom_object_definition()],
        confirm=True,
    )
    assert result.status == "created"
    assert result.provider_id == "2-88"
    payload = created[0]
    assert payload["name"] == "orders"
    assert payload["primaryDisplayProperty"] == "order_number"
    properties = payload["properties"]
    assert isinstance(properties, list)
    assert len(properties) == 1
    primary = properties[0]
    assert isinstance(primary, dict)
    assert primary["name"] == "order_number"
    assert primary["type"] == "string"


def test_custom_object_schema_payload_shape() -> None:
    payload = _custom_object_schema_payload(_custom_object_definition())
    assert payload["labels"] == {"singular": "Order", "plural": "Orders"}
    assert payload["associatedObjects"] == ["CONTACT"]
    assert payload["requiredProperties"] == []


def test_pipeline_payload_probability_strings() -> None:
    payload = _pipeline_payload(_pipeline_definition())
    stages = payload["stages"]
    assert [stage["metadata"]["probability"] for stage in stages] == ["0.0", "0.5", "1.0"]


def _pipeline_definition_with_probabilities() -> PipelineDefinition:
    return PipelineDefinition(
        key="sales",
        label="Sales Pipeline",
        object_type="deals",
        stages=[
            PipelineStage(key="new", label="New", display_order=0, probability=0.1),
            PipelineStage(key="review", label="Review", display_order=1, probability=0.4),
            PipelineStage(key="won", label="Won", display_order=2, probability=0.9),
        ],
    )


def _provider_pipeline(probabilities: list[str | None]) -> dict[str, object]:
    stages: list[dict[str, object]] = []
    for index, (label, probability) in enumerate(
        zip(["New", "Review", "Won"], probabilities, strict=True)
    ):
        stage: dict[str, object] = {"id": f"s-{index}", "label": label, "displayOrder": index}
        if probability is not None:
            stage["metadata"] = {"probability": probability}
        stages.append(stage)
    return {"id": "p-1", "label": "sales pipeline", "stages": stages}


def test_pipeline_payload_uses_provided_probabilities_with_ramp_fallback() -> None:
    definition = PipelineDefinition(
        key="sales",
        label="Sales Pipeline",
        object_type="deals",
        stages=[
            PipelineStage(key="new", label="New", display_order=0, probability=0.2),
            PipelineStage(key="review", label="Review", display_order=1),
            PipelineStage(key="won", label="Won", display_order=2, probability=1.0),
        ],
    )
    stages = _pipeline_payload(definition)["stages"]
    # The reviewed 0.2 and 1.0 pass through; the middle stage without a
    # reviewed probability falls back to the linear ramp (index 1 of 3 = 0.5).
    assert [stage["metadata"]["probability"] for stage in stages] == ["0.2", "0.5", "1.0"]


def test_pipeline_compatibility_compares_probability_only_when_reviewed() -> None:
    reviewed = _pipeline_definition_with_probabilities()
    assert _pipeline_is_compatible(reviewed, _provider_pipeline(["0.1", "0.4", "0.9"]))
    # Float comparison, not string comparison.
    assert _pipeline_is_compatible(reviewed, _provider_pipeline(["0.10", "0.40", "0.90"]))
    assert not _pipeline_is_compatible(reviewed, _provider_pipeline(["0.1", "0.5", "0.9"]))
    # A reviewed probability requires a parseable provider value (production
    # semantics): a stage without metadata cannot be verified.
    assert not _pipeline_is_compatible(reviewed, _provider_pipeline(["0.1", None, "0.9"]))
    assert not _pipeline_is_compatible(reviewed, _provider_pipeline(["0.1", "oops", "0.9"]))

    unreviewed = _pipeline_definition()
    # Without reviewed probabilities the check ignores provider metadata
    # entirely, whether present or absent.
    assert _pipeline_is_compatible(unreviewed, _provider_pipeline(["0.7", "0.8", "0.9"]))
    assert _pipeline_is_compatible(unreviewed, _provider_pipeline([None, None, None]))


async def test_reconcile_resources_existing_pipeline_verifies_reviewed_probabilities() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/pipelines/deals"
        return httpx.Response(200, json={"results": [_provider_pipeline(["0.1", "0.4", "0.9"])]})

    (result,) = await destination(respond).reconcile_resources(
        credentials(),
        pipelines=[_pipeline_definition_with_probabilities()],
        custom_objects=[],
        confirm=False,
    )
    assert result.status == "existing"
    assert result.stage_ids == {"new": "s-0", "review": "s-1", "won": "s-2"}

    def respond_conflict(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_provider_pipeline(["0.1", "0.5", "0.9"])]})

    (conflict,) = await destination(respond_conflict).reconcile_resources(
        credentials(),
        pipelines=[_pipeline_definition_with_probabilities()],
        custom_objects=[],
        confirm=False,
    )
    assert conflict.status == "conflict"


async def test_reconcile_resources_confirm_creates_pipeline_with_provided_probabilities() -> None:
    created: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"results": []})
        payload = body(request)
        created.append(payload)
        return httpx.Response(
            201,
            json={
                "id": "p-9",
                "label": payload["label"],
                "stages": [
                    {"id": f"s-{index}", "label": stage["label"]}  # type: ignore[index]
                    for index, stage in enumerate(payload["stages"])  # type: ignore[arg-type]
                ],
            },
        )

    (result,) = await destination(respond).reconcile_resources(
        credentials(),
        pipelines=[_pipeline_definition_with_probabilities()],
        custom_objects=[],
        confirm=True,
    )
    assert result.status == "created"
    stages = created[0]["stages"]
    assert isinstance(stages, list)
    assert [stage["metadata"]["probability"] for stage in stages] == ["0.1", "0.4", "0.9"]  # type: ignore[index]


def _custom_object_properties() -> list[CustomObjectProperty]:
    return [
        CustomObjectProperty(
            source_field="OrderNumber",
            internal_name="order_number",
            label="Order number",
            required=True,
            unique=True,
            searchable=True,
        ),
        CustomObjectProperty(
            source_field="IsPriority",
            internal_name="is_priority",
            label="Priority",
            source_type="bool",
        ),
    ]


def _custom_object_definition_with_properties() -> CustomObjectDefinition:
    return CustomObjectDefinition(
        key="orders",
        source_object="Order",
        internal_name="orders",
        singular_label="Order",
        plural_label="Orders",
        primary_display_property="order_number",
        associated_objects=["CONTACT"],
        properties=_custom_object_properties(),
    )


def test_custom_object_schema_payload_includes_provided_properties() -> None:
    payload = _custom_object_schema_payload(_custom_object_definition_with_properties())
    properties = payload["properties"]
    assert isinstance(properties, list)
    # The provided list already carries the primary display property, so no
    # synthetic duplicate is added.
    assert [prop["name"] for prop in properties] == ["order_number", "is_priority"]
    primary, priority = properties
    assert primary["label"] == "Order number"
    assert primary["type"] == "string"
    assert primary["fieldType"] == "text"
    assert primary["hasUniqueValue"] is True
    assert primary["displayOrder"] == 0
    assert primary["description"] == "Migrated from OrderNumber with Sanka Migrate."
    assert priority["type"] == "bool"
    assert priority["fieldType"] == "booleancheckbox"
    assert priority["hasUniqueValue"] is False
    assert priority["displayOrder"] == 1
    assert [option["value"] for option in priority["options"]] == ["true", "false"]
    assert payload["requiredProperties"] == ["order_number"]
    assert payload["searchableProperties"] == ["order_number"]


def test_custom_object_schema_payload_synthesizes_missing_primary_display_property() -> None:
    definition = CustomObjectDefinition(
        key="orders",
        source_object="Order",
        internal_name="orders",
        singular_label="Order",
        plural_label="Orders",
        primary_display_property="order_number",
        properties=[
            CustomObjectProperty(source_field="Status", internal_name="status", label="Status"),
        ],
    )
    payload = _custom_object_schema_payload(definition)
    properties = payload["properties"]
    assert isinstance(properties, list)
    assert [prop["name"] for prop in properties] == ["order_number", "status"]
    synthetic, status = properties
    assert synthetic["label"] == "Order Number"
    assert synthetic["type"] == "string"
    assert synthetic["hasUniqueValue"] is False
    assert synthetic["displayOrder"] == 0
    assert status["displayOrder"] == 1
    assert payload["requiredProperties"] == []
    assert payload["searchableProperties"] == []


def _provider_custom_schema() -> dict[str, object]:
    return {
        "objectTypeId": "2-77",
        "name": "orders",
        "labels": {"singular": "Order", "plural": "Orders"},
        "primaryDisplayProperty": "order_number",
        "properties": [
            {"name": "order_number", "type": "string", "hasUniqueValue": True},
            {"name": "is_priority", "type": "bool"},
        ],
        "associatedObjects": ["CONTACT"],
    }


def test_custom_object_compatibility_checks_provided_properties() -> None:
    definition = _custom_object_definition_with_properties()
    assert _custom_object_schema_is_compatible(definition, _provider_custom_schema())

    missing = _provider_custom_schema()
    missing["properties"] = [{"name": "order_number", "type": "string", "hasUniqueValue": True}]
    assert not _custom_object_schema_is_compatible(definition, missing)

    type_mismatch = _provider_custom_schema()
    type_mismatch["properties"] = [
        {"name": "order_number", "type": "string", "hasUniqueValue": True},
        {"name": "is_priority", "type": "string"},
    ]
    assert not _custom_object_schema_is_compatible(definition, type_mismatch)

    not_unique = _provider_custom_schema()
    not_unique["properties"] = [
        {"name": "order_number", "type": "string"},
        {"name": "is_priority", "type": "bool"},
    ]
    assert not _custom_object_schema_is_compatible(definition, not_unique)


def test_custom_object_compatibility_without_provided_properties_checks_primary_name() -> None:
    # Without provided properties only the primary display property's presence
    # is verified (previous behavior preserved) — no type requirements.
    definition = _custom_object_definition()
    minimal = {
        "labels": {"singular": "Order", "plural": "Orders"},
        "primaryDisplayProperty": "order_number",
        "properties": [{"name": "order_number"}],
        "associatedObjects": ["CONTACT"],
    }
    assert _custom_object_schema_is_compatible(definition, minimal)
    absent = dict(minimal)
    absent["properties"] = [{"name": "something_else"}]
    assert not _custom_object_schema_is_compatible(definition, absent)


async def test_reconcile_resources_existing_custom_object_with_properties() -> None:
    schema = _provider_custom_schema()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/schemas":
            return httpx.Response(200, json={"results": [schema]})
        if request.url.path == "/crm/v3/schemas/2-77":
            return httpx.Response(200, json=schema)
        raise AssertionError(f"unexpected request {request.url.path}")

    (result,) = await destination(respond).reconcile_resources(
        credentials(),
        pipelines=[],
        custom_objects=[_custom_object_definition_with_properties()],
        confirm=False,
    )
    assert result.status == "existing"

    incompatible = _provider_custom_schema()
    incompatible["properties"] = [
        {"name": "order_number", "type": "string", "hasUniqueValue": True}
    ]

    def respond_conflict(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/schemas":
            return httpx.Response(200, json={"results": [incompatible]})
        return httpx.Response(200, json=incompatible)

    (conflict,) = await destination(respond_conflict).reconcile_resources(
        credentials(),
        pipelines=[],
        custom_objects=[_custom_object_definition_with_properties()],
        confirm=False,
    )
    assert conflict.status == "conflict"


async def test_reconcile_resources_confirm_creates_custom_object_with_properties() -> None:
    created: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"results": []})
        payload = body(request)
        created.append(payload)
        return httpx.Response(201, json={"objectTypeId": "2-88"})

    (result,) = await destination(respond).reconcile_resources(
        credentials(),
        pipelines=[],
        custom_objects=[_custom_object_definition_with_properties()],
        confirm=True,
    )
    assert result.status == "created"
    payload = created[0]
    properties = payload["properties"]
    assert isinstance(properties, list)
    assert [prop["name"] for prop in properties] == ["order_number", "is_priority"]  # type: ignore[index]
    assert payload["requiredProperties"] == ["order_number"]
    assert payload["searchableProperties"] == ["order_number"]


# --------------------------------------------------------------------------
# Destination: inventory + owners
# --------------------------------------------------------------------------


async def test_destination_inventory_resolves_custom_types_with_warnings() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/schemas":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "objectTypeId": "2-123",
                            "name": "orders",
                            "labels": {"singular": "Order", "plural": "Orders"},
                        },
                        {
                            "objectTypeId": "2-124",
                            "name": "shipment",
                            "labels": {"singular": "Parcel", "plural": "Parcels"},
                        },
                        {
                            "objectTypeId": "2-125",
                            "name": "parcel",
                            "labels": {"singular": "Parcel", "plural": "Parcels"},
                        },
                    ]
                },
            )
        if request.url.path.startswith("/crm/v3/properties/"):
            return httpx.Response(200, json={"results": [{"name": "order_number"}]})
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"total": 7, "results": []})
        raise AssertionError(f"unexpected request {request.url.path}")

    inventory = await destination(respond).inventory(
        credentials(),
        canonical_types={"contact", "order", "parcel", "warehouse"},
    )
    keys = {schema.key for schema in inventory.objects}
    assert keys == {"contacts", "2-123"}
    assert "HubSpot custom object mapping for parcel is ambiguous." in inventory.warnings
    assert "HubSpot does not have a default warehouse object mapping." in inventory.warnings


async def test_list_owners_maps_profiles_and_follows_paging() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/owners/"
        if request.url.params.get("after") == "2":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "31", "email": "archived@example.com", "archived": True},
                        {"id": "32", "email": ""},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "30", "email": "owner@example.com", "firstName": "Ada", "lastName": "L"}
                ],
                "paging": {"next": {"link": "https://api.hubapi.com/crm/v3/owners/?after=2"}},
            },
        )

    owners = await destination(respond).list_owners(credentials())
    assert [(owner.id, owner.active, owner.name) for owner in owners] == [
        ("30", True, "Ada L"),
        ("31", False, None),
    ]


# --------------------------------------------------------------------------
# Destination: provider control (throttle + retry)
# --------------------------------------------------------------------------


async def test_retry_on_429_honors_retry_after_and_counts_metrics() -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "limit"})
        return httpx.Response(201, json={"id": "42"})

    clock = FakeClock()
    dest = destination(respond, clock=clock, min_interval_seconds=0.25)
    result = await dest.write_record(
        credentials(),
        object_type="deals",
        properties={"dealname": "Sanka Migrate"},
        options=write_options(),
    )
    assert result.status == "created"
    assert clock.sleeps == [7.0]
    metrics = dest.retry_metrics()
    assert metrics["requests"] == 2
    assert metrics["retries"] == 1
    assert metrics["rateLimitRetries"] == 1
    assert metrics["throttleWaitMs"] == 7000
    assert metrics["lastRetryAt"] is not None


async def test_429_exhausts_after_five_attempts() -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"message": "limit"})

    clock = FakeClock()
    dest = destination(respond, clock=clock)
    with pytest.raises(RateLimitError) as excinfo:
        await dest.write_record(
            credentials(),
            object_type="deals",
            properties={"dealname": "Sanka Migrate"},
            options=write_options(),
        )
    assert attempts == 5
    assert excinfo.value.retry_after_seconds == 1.0
    metrics = dest.retry_metrics()
    assert metrics["requests"] == 5
    assert metrics["retries"] == 4
    assert metrics["rateLimitRetries"] == 4


async def test_client_error_is_not_retried() -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, json={"message": "bad payload"})

    dest = destination(respond, clock=FakeClock())
    with pytest.raises(ValidationFailedError):
        await dest.write_record(
            credentials(),
            object_type="deals",
            properties={"dealname": "Sanka Migrate"},
            options=write_options(),
        )
    assert attempts == 1
    assert dest.retry_metrics()["retries"] == 0


async def test_transient_5xx_retried_only_for_identity_bearing_writes() -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("/search"):
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, json={"message": "flaky"})
            return httpx.Response(200, json={"results": []})
        return httpx.Response(201, json={"id": "77"})

    clock = FakeClock()
    dest = destination(respond, clock=clock)
    result = await dest.write_record(
        credentials(),
        object_type="contacts",
        properties={"email": "a@example.com"},
        options=write_options(conflict_policy="skip_existing"),
    )
    assert result.status == "created"
    assert dest.retry_metrics()["retries"] == 1
    assert clock.sleeps == [1.0]  # 2**0 backoff

    # Without an identity-bearing conflict policy the same 503 must surface.
    attempts = 0

    def respond_create(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"message": "flaky"})

    with pytest.raises(TransientProviderError):
        await destination(respond_create, clock=FakeClock()).write_record(
            credentials(),
            object_type="deals",
            properties={"dealname": "Sanka Migrate"},
            options=write_options(),
        )
    assert attempts == 1


async def test_transport_error_retried_for_identity_bearing_writes() -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("/search"):
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("reset", request=request)
            return httpx.Response(200, json={"results": []})
        return httpx.Response(201, json={"id": "78"})

    dest = destination(respond, clock=FakeClock())
    result = await dest.write_record(
        credentials(),
        object_type="contacts",
        properties={"email": "a@example.com"},
        options=write_options(conflict_policy="update_existing"),
    )
    assert result.status == "created"
    assert dest.retry_metrics()["retries"] == 1


async def test_423_uses_fixed_two_second_backoff() -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(423, json={"message": "portal busy"})
        return httpx.Response(201, json={"id": "43"})

    clock = FakeClock()
    dest = destination(respond, clock=clock)
    result = await dest.write_record(
        credentials(),
        object_type="deals",
        properties={"dealname": "Sanka Migrate"},
        options=write_options(),
    )
    assert result.status == "created"
    assert clock.sleeps == [2.0]
    metrics = dest.retry_metrics()
    assert metrics["retries"] == 1
    assert metrics["rateLimitRetries"] == 0


async def test_min_interval_paces_consecutive_requests() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "1"})

    clock = FakeClock()
    dest = destination(respond, clock=clock, min_interval_seconds=0.25)
    for _ in range(2):
        await dest.write_record(
            credentials(),
            object_type="deals",
            properties={"dealname": "Sanka Migrate"},
            options=write_options(),
        )
    assert clock.sleeps == [0.25]
    assert dest.retry_metrics()["throttleWaitMs"] == 250


def test_retry_metrics_returns_a_copy() -> None:
    dest = HubSpotDestination(gateway=gateway(lambda request: httpx.Response(200, json={})))
    metrics = dest.retry_metrics()
    metrics["requests"] = 999
    assert dest.retry_metrics()["requests"] == 0
