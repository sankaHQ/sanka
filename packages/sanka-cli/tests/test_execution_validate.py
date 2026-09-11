# SPDX-License-Identifier: AGPL-3.0-only
"""Write-free validation tests.

Write-freedom is pinned structurally (the module's imports and signatures
admit no destination connector and no execution ledger), the reject shaping
is pinned against the production dry-run spellings (codes, messages, capped
rejects, deduplicated reason counts), and sampling is pinned deterministic —
same source, same payload, byte for byte.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from sanka.runtime.engine import ExecutionError, MigrationEngine
from sanka.runtime.execution import (
    DEFAULT_VALIDATION_SAMPLE_SIZE,
    MAX_VALIDATION_REJECTS,
    MISSING_IDENTITY_CODE,
    ExecutionFault,
    ExecutionRoute,
    execution_routes,
    validate_routes,
    validation_reason,
    validation_rejection,
    validation_rejects_truncated_warning,
)
from sanka.runtime.mapping import MappingError, MigrationMappingField, mapping_groups
from sanka.runtime.registry import DataExtensionRegistry
from sanka.runtime.spec import EndpointSpec, MigrationSpec
from sanka.runtime.state import SqliteStateStore
from sanka_data import Credentials, RecordPage, SourceFilter, SourceObject
from sanka_data.protocols import SystemReader, SystemWriter

pytestmark = pytest.mark.usefixtures("trusted_connector_discovery")

_CREDENTIALS = Credentials(provider="fake")


# -- fakes --------------------------------------------------------------------


class FakeSource:
    provider = "fake-source"
    binding_kind = "api_token"

    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self.records = records if records is not None else [{"Id": "001A", "Name": "Acme"}]
        self.read_requests: list[dict[str, Any]] = []

    async def discover_objects(self, credentials: Credentials) -> list[SourceObject]:
        return []

    async def inventory(
        self, credentials: Credentials, *, object_types: list[str] | None = None
    ) -> Any:
        raise NotImplementedError

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
        self.read_requests.append(
            {
                "object_type": object_type,
                "field_keys": field_keys,
                "limit": limit,
                "cursor": cursor,
                "source_filter": source_filter,
            }
        )
        return RecordPage(object_key=object_type, records=[dict(r) for r in self.records])


class PagedSource(FakeSource):
    """Serves fixed pages keyed by cursor, for full-read pagination tests."""

    def __init__(self, pages: dict[str | None, tuple[list[dict[str, Any]], str | None]]) -> None:
        super().__init__(records=[])
        self.pages = pages

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
        self.read_requests.append({"cursor": cursor, "limit": limit})
        records, next_cursor = self.pages[cursor]
        return RecordPage(
            object_key=object_type,
            records=[dict(r) for r in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )


class BoundedSource(FakeSource):
    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        super().__init__(records)
        self.bounded_requests: list[dict[str, Any]] = []

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
        self.bounded_requests.append(
            {"object_type": object_type, "cursor": cursor, "upper_bound": upper_bound}
        )
        return RecordPage(object_key=object_type, records=[dict(r) for r in self.records])


# -- helpers ------------------------------------------------------------------


def _routes(fields: list[MigrationMappingField]) -> list[ExecutionRoute]:
    return execution_routes(mapping_groups(fields))


def _name_route() -> list[ExecutionRoute]:
    return _routes(
        [
            MigrationMappingField(
                source_field="Account.Name", target_object="companies", target_field="name"
            )
        ]
    )


async def _validate(
    routes: list[ExecutionRoute],
    source: FakeSource,
    **overrides: Any,
) -> dict[str, Any]:
    return await validate_routes(
        routes=routes,
        source=source,
        source_credentials=_CREDENTIALS,
        **overrides,
    )


# -- write-freedom, pinned structurally ---------------------------------------


def test_validate_module_surface_admits_no_destination_and_no_ledger() -> None:
    from sanka.runtime.execution import validate as validate_module

    source = inspect.getsource(validate_module)
    assert "SystemWriter" not in source
    assert "ExecutionLedger" not in source
    assert "ExecutionHost" not in source
    assert "ExecutionJournal" not in source

    imported_names = [
        alias.asname or alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    ]
    assert not any("Destination" in name for name in imported_names)
    assert not any("Ledger" in name for name in imported_names)

    parameters = inspect.signature(validate_routes).parameters
    assert set(parameters) == {
        "routes",
        "source",
        "source_credentials",
        "sample_size",
        "full",
        "max_rejects",
        "route_high_water_marks",
        "on_missing_identity",
        "check_references",
    }


# -- payload shape and determinism --------------------------------------------


async def test_validate_routes_payload_shape_is_the_production_dry_run_shape() -> None:
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme"}, {"Id": "001B", "Name": "Beta"}])

    payload = await _validate(_name_route(), source)

    assert payload == {
        "objects": [
            {
                "sourceObject": "Account",
                "destinationObject": "companies",
                "sourceFilter": None,
                "sampled": 2,
                "valid": 2,
                "invalid": 0,
                "mappedFields": 1,
                "invalidReasons": [],
            }
        ],
        "rejects": [],
        "warnings": [],
    }
    assert source.read_requests == [
        {
            "object_type": "Account",
            "field_keys": ["Name"],
            "limit": DEFAULT_VALIDATION_SAMPLE_SIZE,
            "cursor": None,
            "source_filter": None,
        }
    ]


async def test_validate_routes_is_deterministic_byte_for_byte() -> None:
    def records() -> list[dict[str, Any]]:
        return [
            {"Id": "001A", "Name": "Acme"},
            {"Id": "001B"},
            {"Name": "NoIdentity"},
        ]

    first = await _validate(_name_route(), FakeSource(records=records()))
    second = await _validate(_name_route(), FakeSource(records=records()))

    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)


async def test_validate_routes_reports_source_filter_and_route_key() -> None:
    routes = _routes(
        [
            MigrationMappingField(
                source_field="Account.Name",
                target_object="companies",
                target_field="name",
                source_filter=SourceFilter(field="IsPersonAccount", value=False),
            )
        ]
    )
    source = FakeSource(records=[{"Id": "001A"}])

    payload = await _validate(routes, source)

    assert payload["objects"][0]["sourceFilter"] == {
        "field": "IsPersonAccount",
        "operator": "equals",
        "value": False,
    }
    (reject,) = payload["rejects"]
    assert reject["routeKey"] == "Account|companies|IsPersonAccount=equals:false"
    assert reject["code"] == "SANKA_MIGRATE_EMPTY_DESTINATION_RECORD"


# -- production rejection semantics -------------------------------------------


async def test_required_field_failures_reject_with_production_spellings() -> None:
    fields = [
        MigrationMappingField(
            source_field="Account.Name", target_object="companies", target_field="name"
        ),
        MigrationMappingField(
            source_field="Account.Industry",
            target_object="companies",
            target_field="industry",
            required=True,
        ),
    ]
    source = FakeSource(
        records=[
            {"Id": "001A", "Name": "Acme"},
            {"Id": "001B", "Name": "Beta"},
            {"Id": "001C", "Name": "Gamma", "Industry": "software"},
        ]
    )

    payload = await _validate(_routes(fields), source)

    row = payload["objects"][0]
    assert (row["sampled"], row["valid"], row["invalid"]) == (3, 1, 2)
    assert row["invalidReasons"] == [
        {
            "code": "SANKA_MIGRATE_REQUIRED_SOURCE_FIELD_EMPTY",
            "message": "Required source field is empty.",
            "sourceField": "Account.Industry",
            "targetField": "industry",
            "count": 2,
        }
    ]
    assert payload["rejects"][0] == {
        "sourceObject": "Account",
        "destinationObject": "companies",
        "routeKey": "Account|companies",
        "sourceRecordId": "001A",
        "code": "SANKA_MIGRATE_REQUIRED_SOURCE_FIELD_EMPTY",
        "message": "Required source field is empty.",
        "sourceField": "Account.Industry",
        "targetField": "industry",
    }
    assert [reject["sourceRecordId"] for reject in payload["rejects"]] == ["001A", "001B"]


async def test_empty_destination_record_rejects_like_production() -> None:
    source = FakeSource(records=[{"Id": "001A"}])

    payload = await _validate(_name_route(), source)

    (reject,) = payload["rejects"]
    assert reject["code"] == "SANKA_MIGRATE_EMPTY_DESTINATION_RECORD"
    assert reject["message"] == "No destination properties were produced."
    assert reject["sourceRecordId"] == "001A"
    assert payload["objects"][0]["invalid"] == 1


async def test_rejects_are_capped_globally_with_the_production_warning() -> None:
    source = FakeSource(records=[{"Id": f"00{n}"} for n in range(3)])

    payload = await _validate(_name_route(), source, max_rejects=1)

    assert len(payload["rejects"]) == 1
    assert payload["rejects"][0]["sourceRecordId"] == "000"
    assert payload["warnings"] == ["Dry-run rejects were truncated to the first 1 records."]
    assert payload["objects"][0]["invalid"] == 3
    assert payload["objects"][0]["invalidReasons"][0]["count"] == 3


def test_truncation_warning_matches_production_copy_at_the_production_cap() -> None:
    assert MAX_VALIDATION_REJECTS == 100
    assert (
        validation_rejects_truncated_warning(MAX_VALIDATION_REJECTS)
        == "Dry-run rejects were truncated to the first 100 records."
    )


def test_validation_rejection_falls_back_to_the_production_mapping_code() -> None:
    rejection = validation_rejection(
        source_object="Account",
        destination_object="companies",
        route_key="Account|companies",
        record={"external_id": "ext-9"},
        error=ValueError("boom"),
    )

    assert rejection == {
        "sourceObject": "Account",
        "destinationObject": "companies",
        "routeKey": "Account|companies",
        "sourceRecordId": "ext-9",
        "code": "SANKA_MIGRATE_MAPPING_VALUE_INVALID",
        "message": "Mapped source value could not be transformed.",
    }


def test_validation_rejection_keeps_structured_codes_and_filters_details() -> None:
    error = MappingError(
        "No reviewed Sanka value-map predicate matched the source record.",
        code="SANKA_MIGRATE_VALUE_MAP_UNMATCHED",
        details={
            "sourceField": "Account.Stage",
            "targetField": "stage",
            "sourceValue": "secret",
        },
    )

    rejection = validation_rejection(
        source_object="Account",
        destination_object="companies",
        route_key="Account|companies",
        record={"Id": "001A"},
        error=error,
    )

    assert rejection["code"] == "SANKA_MIGRATE_VALUE_MAP_UNMATCHED"
    assert rejection["sourceField"] == "Account.Stage"
    assert rejection["targetField"] == "stage"
    assert "sourceValue" not in rejection
    assert validation_reason(rejection) == {
        "code": "SANKA_MIGRATE_VALUE_MAP_UNMATCHED",
        "message": "No reviewed Sanka value-map predicate matched the source record.",
        "sourceField": "Account.Stage",
        "targetField": "stage",
    }


# -- missing identity ---------------------------------------------------------


async def test_missing_identity_rejects_by_default_and_drop_matches_production() -> None:
    def records() -> list[dict[str, Any]]:
        return [{"Name": "NoIdentity"}, {"Id": "001A", "Name": "Acme"}]

    strict = await _validate(_name_route(), FakeSource(records=records()))
    row = strict["objects"][0]
    assert (row["sampled"], row["valid"], row["invalid"]) == (2, 1, 1)
    (reject,) = strict["rejects"]
    assert reject["code"] == MISSING_IDENTITY_CODE
    assert reject["message"] == "Source record is missing an identity value."
    assert reject["sourceRecordId"] is None

    parity = await _validate(
        _name_route(), FakeSource(records=records()), on_missing_identity="drop"
    )
    parity_row = parity["objects"][0]
    assert (parity_row["sampled"], parity_row["valid"], parity_row["invalid"]) == (2, 2, 0)
    assert parity["rejects"] == []


async def test_missing_identity_honors_the_route_identity_field() -> None:
    routes = [
        ExecutionRoute(
            route_key="Account|companies",
            source_object="Account",
            destination_object="companies",
            source_filter=None,
            fields=[
                MigrationMappingField(
                    source_field="Account.Name", target_object="companies", target_field="name"
                )
            ],
            source_identity_field="slug",
        )
    ]
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme"}])

    payload = await _validate(routes, source)

    (reject,) = payload["rejects"]
    assert reject["code"] == MISSING_IDENTITY_CODE
    # The reject row still names the record by the production id chain.
    assert reject["sourceRecordId"] == "001A"


# -- reference checks ---------------------------------------------------------


def _reference_fields(*, required: bool) -> list[MigrationMappingField]:
    return [
        MigrationMappingField(
            source_field="Contact.LastName", target_object="contacts", target_field="lastname"
        ),
        MigrationMappingField(
            source_field="Contact.AccountId",
            target_object="contacts",
            target_field="company_id",
            mapping_kind="reference",
            source_reference_object="Account",
            target_reference_object="companies",
            required=required,
        ),
    ]


async def test_required_reference_empty_rejects_and_parity_mode_skips_it() -> None:
    source = FakeSource(records=[{"Id": "003A", "LastName": "Lovelace"}])

    payload = await _validate(_routes(_reference_fields(required=True)), source)
    (reject,) = payload["rejects"]
    assert reject["code"] == "SANKA_MIGRATE_REQUIRED_REFERENCE_SOURCE_FIELD_EMPTY"
    assert reject["message"] == "Required reference source field is empty."
    assert reject["sourceField"] == "Contact.AccountId"
    assert reject["targetField"] == "company_id"

    parity = await _validate(
        _routes(_reference_fields(required=True)),
        FakeSource(records=[{"Id": "003A", "LastName": "Lovelace"}]),
        check_references=False,
    )
    assert parity["rejects"] == []
    assert parity["objects"][0]["valid"] == 1


async def test_ambiguous_scalar_reference_rejects() -> None:
    source = FakeSource(
        records=[{"Id": "003A", "LastName": "Lovelace", "AccountId": ["001A", "001B"]}]
    )

    payload = await _validate(_routes(_reference_fields(required=False)), source)

    (reject,) = payload["rejects"]
    assert reject["code"] == "SANKA_MIGRATE_REFERENCE_SOURCE_ID_AMBIGUOUS"
    assert reject["message"] == "A scalar reference mapping must resolve one source id."


# -- sampling and scope -------------------------------------------------------


async def test_sample_reads_one_page_and_full_reads_them_all() -> None:
    def pages() -> dict[str | None, tuple[list[dict[str, Any]], str | None]]:
        return {
            None: ([{"Id": "1", "Name": "A"}, {"Id": "2", "Name": "B"}], "c1"),
            "c1": ([{"Id": "3", "Name": "C"}, {"Id": "4", "Name": "D"}], "c2"),
            "c2": ([{"Id": "5", "Name": "E"}], None),
        }

    sampled_source = PagedSource(pages())
    sampled = await _validate(_name_route(), sampled_source, sample_size=2)
    assert sampled["objects"][0]["sampled"] == 2
    assert [request["cursor"] for request in sampled_source.read_requests] == [None]

    full_source = PagedSource(pages())
    full = await _validate(_name_route(), full_source, sample_size=2, full=True)
    assert full["objects"][0]["sampled"] == 5
    assert full["objects"][0]["valid"] == 5
    assert [request["cursor"] for request in full_source.read_requests] == [None, "c1", "c2"]


async def test_sample_size_must_be_a_positive_integer() -> None:
    with pytest.raises(ExecutionFault) as excinfo:
        await _validate(_name_route(), FakeSource(), sample_size=0)

    assert excinfo.value.code == "SANKA_MIGRATE_DRY_RUN_LIMIT_INVALID"
    assert str(excinfo.value) == "Dry-run maxRecords must be a positive integer."


async def test_frozen_none_mark_samples_nothing() -> None:
    source = FakeSource()

    payload = await _validate(
        _name_route(), source, route_high_water_marks={"Account|companies": None}
    )

    assert source.read_requests == []
    assert payload["objects"][0] == {
        "sourceObject": "Account",
        "destinationObject": "companies",
        "sourceFilter": None,
        "sampled": 0,
        "valid": 0,
        "invalid": 0,
        "mappedFields": 1,
        "invalidReasons": [],
    }


async def test_frozen_mark_uses_bounded_reads_and_refuses_unbounded_sources() -> None:
    bounded = BoundedSource(records=[{"Id": "001A", "Name": "Acme"}])
    payload = await _validate(
        _name_route(), bounded, route_high_water_marks={"Account|companies": "hwm-5"}
    )
    assert bounded.bounded_requests == [
        {"object_type": "Account", "cursor": None, "upper_bound": "hwm-5"}
    ]
    assert payload["objects"][0]["valid"] == 1

    with pytest.raises(ExecutionFault) as excinfo:
        await _validate(
            _name_route(), FakeSource(), route_high_water_marks={"Account|companies": "hwm-5"}
        )
    assert excinfo.value.code == "SANKA_MIGRATE_SOURCE_HIGH_WATER_MARK_UNSUPPORTED"


async def test_routes_validate_independently_with_route_keyed_rejects() -> None:
    fields = [
        MigrationMappingField(
            source_field="Account.Name", target_object="companies", target_field="name"
        ),
        MigrationMappingField(
            source_field="Account.Name",
            target_object="contacts",
            target_field="firstname",
            required=True,
        ),
        MigrationMappingField(
            source_field="Account.Industry",
            target_object="contacts",
            target_field="industry",
            required=True,
        ),
    ]
    source = FakeSource(records=[{"Id": "001A", "Name": "Acme"}])

    payload = await _validate(_routes(fields), source)

    by_pair = {(row["sourceObject"], row["destinationObject"]): row for row in payload["objects"]}
    assert by_pair[("Account", "companies")]["valid"] == 1
    assert by_pair[("Account", "contacts")]["invalid"] == 1
    (reject,) = payload["rejects"]
    assert reject["routeKey"] == "Account|contacts"
    assert reject["code"] == "SANKA_MIGRATE_REQUIRED_SOURCE_FIELD_EMPTY"
    assert reject["sourceField"] == "Account.Industry"


# -- engine surface -----------------------------------------------------------


class PoisonedRegistry(DataExtensionRegistry):
    """A registry whose destination role must never be resolved."""

    def __init__(self, inner: DataExtensionRegistry) -> None:
        self._inner = inner

    def names(self) -> list[str]:
        return self._inner.names()

    def source(self, type_name: str) -> SystemReader:
        return self._inner.source(type_name)

    def destination(self, type_name: str) -> SystemWriter:
        raise AssertionError("write-free validation must never resolve a destination connector")


def _write_markdown_content(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text("---\ntitle: A\n---\nAlpha body\n", encoding="utf-8")
    (root / "b.md").write_text("---\ntitle: B\n---\nBeta body\n", encoding="utf-8")


def _markdown_spec(content: Path, db: Path) -> MigrationSpec:
    return MigrationSpec(
        source=EndpointSpec(type="markdown", connection=str(content)),
        target=EndpointSpec(type="sqlite", connection=str(db)),
    )


async def test_engine_validate_never_resolves_the_destination(tmp_path: Path) -> None:
    content, db = tmp_path / "content", tmp_path / "out.db"
    _write_markdown_content(content)
    store_path = tmp_path / "state" / "state.db"
    engine = MigrationEngine(
        store=SqliteStateStore(store_path), registry=DataExtensionRegistry.discover()
    )
    run_id = engine.create(_markdown_spec(content, db))
    await engine.plan(run_id)
    status_before = engine.store.get_run(run_id).status

    validating_engine = MigrationEngine(
        store=SqliteStateStore(store_path),
        registry=PoisonedRegistry(DataExtensionRegistry.discover()),
    )
    payload = await validating_engine.validate(run_id)

    (row,) = payload["objects"]
    assert (row["sourceObject"], row["destinationObject"]) == ("documents", "documents")
    assert row["sampled"] == 2
    assert row["valid"] == 2
    assert row["invalid"] == 0
    assert payload["rejects"] == []
    assert not db.exists()  # nothing was written, nothing was even created
    assert engine.store.get_run(run_id).status is status_before  # no lifecycle transition


async def test_engine_validate_requires_a_plan(tmp_path: Path) -> None:
    content, db = tmp_path / "content", tmp_path / "out.db"
    _write_markdown_content(content)
    engine = MigrationEngine(
        store=SqliteStateStore(tmp_path / "state.db"), registry=DataExtensionRegistry.discover()
    )
    run_id = engine.create(_markdown_spec(content, db))

    with pytest.raises(ExecutionError, match="has no plan"):
        await engine.validate(run_id)


async def test_engine_validate_wraps_execution_faults(tmp_path: Path) -> None:
    content, db = tmp_path / "content", tmp_path / "out.db"
    _write_markdown_content(content)
    engine = MigrationEngine(
        store=SqliteStateStore(tmp_path / "state.db"), registry=DataExtensionRegistry.discover()
    )
    run_id = engine.create(_markdown_spec(content, db))
    await engine.plan(run_id)

    with pytest.raises(ExecutionError, match="SANKA_MIGRATE_DRY_RUN_LIMIT_INVALID"):
        await engine.validate(run_id, sample_size=0)
