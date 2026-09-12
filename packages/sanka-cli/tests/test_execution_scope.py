# SPDX-License-Identifier: AGPL-3.0-only
"""Scope-freeze tests: high-water marks, frozen totals, scope composition.

Ports the pinned production behaviors: marks freeze for every reviewed route
(not only the selected ones), totals count only the selected routes bounded
by the frozen marks, a ``None`` mark counts as zero, sources that cannot
count yield unknown (``None``) totals, and a frozen mark without bounded
counting refuses rather than counting outside the frozen set.
"""

from __future__ import annotations

from typing import Any

import pytest

from sanka.runtime.execution import (
    ExecutionFault,
    ExecutionScope,
    execution_routes,
    freeze_route_high_water_marks,
    freeze_scope,
    frozen_route_totals,
)
from sanka.runtime.mapping import MigrationMappingField, mapping_groups, mapping_route_manifest
from sanka_extensions.data import Credentials, RecordPage, SourceFilter, SourceObject

_CREDENTIALS = Credentials(provider="fake")


def _account_contact_fields() -> list[MigrationMappingField]:
    return [
        MigrationMappingField(
            source_field="Account.Name", target_object="companies", target_field="name"
        ),
        MigrationMappingField(
            source_field="Contact.Name", target_object="contacts", target_field="lastname"
        ),
    ]


def _filtered_account_fields() -> list[MigrationMappingField]:
    return [
        MigrationMappingField(
            source_field="Account.Name",
            target_object="companies",
            target_field="name",
            source_filter=SourceFilter(field="IsPersonAccount", value=False),
        ),
        MigrationMappingField(
            source_field="Account.Name",
            target_object="contacts",
            target_field="firstname",
            source_filter=SourceFilter(field="IsPersonAccount", value=True),
        ),
    ]


class FakeSource:
    """Minimum DataReader; capability subclasses add scope methods."""

    provider = "fake-source"
    binding_kind = "api_token"

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
        return RecordPage(object_key=object_type)


class HighWaterSource(FakeSource):
    def __init__(self, marks: dict[str, str | None]) -> None:
        self.marks = marks
        self.high_water_requests: list[str] = []

    async def high_water_mark(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> str | None:
        self.high_water_requests.append(object_type)
        return self.marks[object_type]


class CountingSource(FakeSource):
    def __init__(self, counts: dict[str, int | Exception]) -> None:
        self.counts = counts
        self.count_requests: list[tuple[str, str | None]] = []

    async def count_records(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
    ) -> int:
        self.count_requests.append((object_type, None))
        value = self.counts[object_type]
        if isinstance(value, Exception):
            raise value
        return value


class BoundedCountingSource(CountingSource):
    async def count_records_bounded(
        self,
        credentials: Credentials,
        *,
        object_type: str,
        source_filter: SourceFilter | None = None,
        upper_bound: str,
    ) -> int:
        self.count_requests.append((object_type, upper_bound))
        value = self.counts[object_type]
        if isinstance(value, Exception):
            raise value
        return value


class RecordingObserver:
    def __init__(self) -> None:
        self.failed_route_counts: list[str] = []

    def record_failed(self, *, route_key: str, source_object: str, source_record_id: str) -> None:
        return None

    def batch_completed(self, *, batches_completed: int, has_more: bool, stalled: bool) -> None:
        return None

    def route_count_failed(self, *, route_key: str) -> None:
        self.failed_route_counts.append(route_key)

    def attempt_fenced(self, *, attempt_id: str) -> None:
        return None


# -- execution_routes ---------------------------------------------------------


def test_execution_routes_derive_route_keys_from_groups() -> None:
    routes = execution_routes(mapping_groups(_account_contact_fields()))

    assert [route.route_key for route in routes] == ["Account|companies", "Contact|contacts"]
    assert routes[0].source_object == "Account"
    assert routes[0].destination_object == "companies"
    assert routes[0].source_filter is None
    assert [field.target_field for field in routes[0].fields] == ["name"]


def test_execution_routes_keep_filtered_routes_separate() -> None:
    routes = execution_routes(mapping_groups(_filtered_account_fields()))

    assert [route.route_key for route in routes] == [
        "Account|companies|IsPersonAccount=equals:false",
        "Account|contacts|IsPersonAccount=equals:true",
    ]


def test_execution_routes_filter_to_the_selection_in_group_order() -> None:
    groups = mapping_groups(_account_contact_fields())

    routes = execution_routes(groups, ["Contact|contacts"])

    assert [route.route_key for route in routes] == ["Contact|contacts"]


# -- freeze_route_high_water_marks --------------------------------------------


async def test_high_water_marks_freeze_for_every_reviewed_route() -> None:
    source = HighWaterSource({"Account": "001Z", "Contact": "003Z"})
    routes = execution_routes(mapping_groups(_account_contact_fields()))

    marks = await freeze_route_high_water_marks(
        source=source, source_credentials=_CREDENTIALS, routes=routes
    )

    assert source.high_water_requests == ["Account", "Contact"]
    assert marks == {"Account|companies": "001Z", "Contact|contacts": "003Z"}


async def test_high_water_marks_are_empty_without_the_capability() -> None:
    routes = execution_routes(mapping_groups(_account_contact_fields()))

    marks = await freeze_route_high_water_marks(
        source=FakeSource(), source_credentials=_CREDENTIALS, routes=routes
    )

    assert marks == {}


async def test_high_water_marks_keep_an_empty_source_route_as_none() -> None:
    source = HighWaterSource({"Account": "001Z", "Contact": None})
    routes = execution_routes(mapping_groups(_account_contact_fields()))

    marks = await freeze_route_high_water_marks(
        source=source, source_credentials=_CREDENTIALS, routes=routes
    )

    assert marks == {"Account|companies": "001Z", "Contact|contacts": None}


# -- frozen_route_totals ------------------------------------------------------


async def test_totals_are_unknown_when_the_source_cannot_count() -> None:
    routes = execution_routes(mapping_groups(_account_contact_fields()))

    totals = await frozen_route_totals(
        source=FakeSource(),
        source_credentials=_CREDENTIALS,
        routes=routes,
        route_high_water_marks={},
    )

    assert totals == {"Account|companies": None, "Contact|contacts": None}


async def test_totals_use_bounded_counts_inside_the_frozen_marks() -> None:
    source = BoundedCountingSource({"Account": 12, "Contact": 3})
    routes = execution_routes(mapping_groups(_account_contact_fields()))

    totals = await frozen_route_totals(
        source=source,
        source_credentials=_CREDENTIALS,
        routes=routes,
        route_high_water_marks={"Account|companies": "001Z", "Contact|contacts": "003Z"},
    )

    assert totals == {"Account|companies": 12, "Contact|contacts": 3}
    assert source.count_requests == [("Account", "001Z"), ("Contact", "003Z")]


async def test_totals_count_unbounded_without_frozen_marks() -> None:
    source = CountingSource({"Account": 7, "Contact": 0})
    routes = execution_routes(mapping_groups(_account_contact_fields()))

    totals = await frozen_route_totals(
        source=source,
        source_credentials=_CREDENTIALS,
        routes=routes,
        route_high_water_marks={},
    )

    assert totals == {"Account|companies": 7, "Contact|contacts": 0}
    assert source.count_requests == [("Account", None), ("Contact", None)]


async def test_totals_count_a_none_mark_as_zero_without_reading() -> None:
    source = BoundedCountingSource({"Account": 12})
    routes = execution_routes(mapping_groups(_account_contact_fields()))

    totals = await frozen_route_totals(
        source=source,
        source_credentials=_CREDENTIALS,
        routes=routes,
        route_high_water_marks={"Account|companies": "001Z", "Contact|contacts": None},
    )

    assert totals == {"Account|companies": 12, "Contact|contacts": 0}
    assert source.count_requests == [("Account", "001Z")]


async def test_totals_refuse_a_frozen_mark_without_bounded_counting() -> None:
    source = CountingSource({"Account": 7, "Contact": 3})
    routes = execution_routes(mapping_groups(_account_contact_fields()))

    with pytest.raises(ExecutionFault) as exc:
        await frozen_route_totals(
            source=source,
            source_credentials=_CREDENTIALS,
            routes=routes,
            route_high_water_marks={"Account|companies": "001Z", "Contact|contacts": "003Z"},
        )

    assert exc.value.code == "SANKA_MIGRATE_SOURCE_HIGH_WATER_MARK_UNSUPPORTED"


async def test_totals_degrade_to_unknown_and_report_a_failed_count() -> None:
    source = BoundedCountingSource({"Account": RuntimeError("api down"), "Contact": 3})
    routes = execution_routes(mapping_groups(_account_contact_fields()))
    observer = RecordingObserver()

    totals = await frozen_route_totals(
        source=source,
        source_credentials=_CREDENTIALS,
        routes=routes,
        route_high_water_marks={},
        observer=observer,
    )

    assert totals == {"Account|companies": None, "Contact|contacts": 3}
    assert observer.failed_route_counts == ["Account|companies"]


async def test_totals_clamp_negative_counts_to_zero() -> None:
    source = CountingSource({"Account": -4, "Contact": 3})
    routes = execution_routes(mapping_groups(_account_contact_fields()))

    totals = await frozen_route_totals(
        source=source,
        source_credentials=_CREDENTIALS,
        routes=routes,
        route_high_water_marks={},
    )

    assert totals == {"Account|companies": 0, "Contact|contacts": 3}


# -- freeze_scope -------------------------------------------------------------


async def test_freeze_scope_freezes_marks_for_all_routes_and_totals_for_selected() -> None:
    class Source(HighWaterSource, BoundedCountingSource):
        def __init__(self) -> None:
            HighWaterSource.__init__(self, {"Account": "001Z", "Contact": "003Z"})
            self.counts = {"Account": 12, "Contact": 3}
            self.count_requests = []

    source = Source()
    groups = mapping_groups(_account_contact_fields())

    scope = await freeze_scope(
        groups=groups,
        source=source,
        source_credentials=_CREDENTIALS,
        requested_route_keys=["Account|companies"],
    )

    assert isinstance(scope, ExecutionScope)
    assert [row["routeKey"] for row in scope.route_manifest] == [
        "Account|companies",
        "Contact|contacts",
    ]
    assert scope.selected_route_keys == ("Account|companies",)
    assert source.high_water_requests == ["Account", "Contact"]
    assert scope.route_high_water_marks == {
        "Account|companies": "001Z",
        "Contact|contacts": "003Z",
    }
    assert scope.route_totals == {"Account|companies": 12}
    assert source.count_requests == [("Account", "001Z")]
    assert scope.scope_hash


async def test_freeze_scope_reuses_validated_saved_marks() -> None:
    source = HighWaterSource({"Account": "999", "Contact": "999"})
    groups = mapping_groups(_account_contact_fields())

    scope = await freeze_scope(
        groups=groups,
        source=source,
        source_credentials=_CREDENTIALS,
        route_high_water_marks={"Account|companies": "001Z", "Contact|contacts": None},
        include_route_totals=False,
    )

    assert source.high_water_requests == []
    assert scope.route_high_water_marks == {
        "Account|companies": "001Z",
        "Contact|contacts": None,
    }
    assert scope.route_totals == {}


async def test_freeze_scope_freezes_fresh_marks_when_saved_marks_are_empty() -> None:
    source = HighWaterSource({"Account": "001Z", "Contact": "003Z"})
    groups = mapping_groups(_account_contact_fields())

    scope = await freeze_scope(
        groups=groups,
        source=source,
        source_credentials=_CREDENTIALS,
        route_high_water_marks={},
        include_route_totals=False,
    )

    assert source.high_water_requests == ["Account", "Contact"]
    assert scope.route_high_water_marks == {
        "Account|companies": "001Z",
        "Contact|contacts": "003Z",
    }


async def test_freeze_scope_rejects_saved_marks_that_do_not_match_the_routes() -> None:
    groups = mapping_groups(_account_contact_fields())

    with pytest.raises(ExecutionFault) as exc:
        await freeze_scope(
            groups=groups,
            source=FakeSource(),
            source_credentials=_CREDENTIALS,
            route_high_water_marks={"Account|companies": "001Z"},
        )

    assert exc.value.code == "SANKA_MIGRATE_EXECUTION_HIGH_WATER_MARK_INVALID"


async def test_freeze_scope_rejects_a_changed_manifest() -> None:
    groups = mapping_groups(_account_contact_fields())
    expected = mapping_route_manifest(mapping_groups(_filtered_account_fields()))

    with pytest.raises(ExecutionFault) as exc:
        await freeze_scope(
            groups=groups,
            source=FakeSource(),
            source_credentials=_CREDENTIALS,
            expected_route_manifest=expected,
        )

    assert exc.value.code == "SANKA_MIGRATE_ROUTE_MANIFEST_CHANGED"


async def test_freeze_scope_rejects_a_selection_outside_the_manifest() -> None:
    groups = mapping_groups(_account_contact_fields())

    with pytest.raises(ExecutionFault) as exc:
        await freeze_scope(
            groups=groups,
            source=FakeSource(),
            source_credentials=_CREDENTIALS,
            requested_route_keys=["Account|unknown"],
        )

    assert exc.value.code == "SANKA_MIGRATE_EXECUTION_ROUTE_INVALID"


async def test_freeze_scope_hash_is_stable_over_totals() -> None:
    source = HighWaterSource({"Account": "001Z", "Contact": "003Z"})
    groups = mapping_groups(_account_contact_fields())

    queued = await freeze_scope(
        groups=groups,
        source=source,
        source_credentials=_CREDENTIALS,
        include_route_totals=False,
    )
    claimed = await freeze_scope(
        groups=groups,
        source=source,
        source_credentials=_CREDENTIALS,
        route_high_water_marks=dict(queued.route_high_water_marks),
        include_route_totals=False,
    )

    assert queued.scope_hash == claimed.scope_hash
