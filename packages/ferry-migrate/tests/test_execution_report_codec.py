# SPDX-License-Identifier: AGPL-3.0-only
"""Round-trip and helper tests for the stage-report codec.

The fixture corpus models the persisted production report shapes (fresh
queued, running batch, heartbeat, completed, cancelled, manual single-batch,
legacy source-keyed) with neutral synthetic values. Modern fixtures are laid
out in the codec's canonical key order and must round-trip byte-identically
(``json.dumps`` equality); legacy layouts load through the fallback and dump
in the modern format.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ferry.runtime.execution import (
    ExecutionFault,
    ExecutionScope,
    ExecutionSnapshot,
    JournalEntry,
    canonical_route_manifest,
    dump_journal,
    execution_heartbeat_is_recent,
    execution_is_cancelled,
    execution_route_high_water_marks,
    execution_route_manifest,
    filter_mapping_groups,
    load_journal,
    parse_datetime,
    prepare_route_state,
    report_checkpoints,
    report_progress_marker,
    report_route_counts,
    report_route_failed_record_ids,
    report_route_pending_record_ids,
    require_matching_route_manifest,
    route_keys_by_pair,
    selected_route_keys,
)
from ferry.runtime.mapping import MappingGroup, MigrationMappingField

ROUTE = "accounts|companies"
FILTERED_ROUTE = "accounts|companies|is_active=equals:false"


def _manifest() -> list[dict[str, Any]]:
    return [
        {
            "routeKey": ROUTE,
            "sourceObject": "accounts",
            "destinationObject": "companies",
            "sourceFilter": None,
        }
    ]


def _filtered_manifest() -> list[dict[str, Any]]:
    return [
        {
            "routeKey": FILTERED_ROUTE,
            "sourceObject": "accounts",
            "destinationObject": "companies",
            "sourceFilter": {"field": "is_active", "operator": "equals", "value": False},
        },
        {
            "routeKey": "contacts|contacts",
            "sourceObject": "contacts",
            "destinationObject": "contacts",
            "sourceFilter": None,
        },
    ]


def _pending_relationship() -> dict[str, Any]:
    return {
        "sourceRecordId": "s-1",
        "destinationObject": "companies",
        "destinationRecordId": "d-1",
        "relationshipField": "parent",
        "relatedSourceObject": "accounts",
        "relatedSourceRecordId": "s-9",
        "relatedDestinationObject": "companies",
        "relationshipMode": "default",
        "associationCategory": "USER_DEFINED",
        "associationTypeId": 7,
    }


def _queued_report() -> dict[str, Any]:
    """Fresh queued continuous report, canonical key order."""

    return {
        "stage": "transfer",
        "slug": "transfer",
        "status": "queued",
        "summary": "Continuous migration execution is queued.",
        "items": [],
        "warnings": [],
        "generatedAt": "2026-08-16T00:00:00+00:00",
        "selectedRouteKeys": [ROUTE],
        "checkpoints": {},
        "completedRoutes": [],
        "hasMore": True,
        "execution": {
            "mode": "continuous",
            "state": "queued",
            "jobId": "job-1",
            "batchSize": 200,
            "batchesCompleted": 0,
            "startedAt": "2026-08-16T00:00:00+00:00",
            "lastHeartbeatAt": "2026-08-16T00:00:00+00:00",
            "resumed": False,
            "conflictPolicy": "skip",
            "missingOwnerPolicy": "block",
            "invalidEmailPolicy": "block",
            "invalidEmailAuditField": None,
            "routeManifest": _manifest(),
            "selectedRouteKeys": [ROUTE],
            "routeHighWaterMarks": {ROUTE: None},
        },
    }


def _running_batch_report() -> dict[str, Any]:
    """Post-batch report for a claimed continuous attempt, canonical order."""

    return {
        "stage": "transfer",
        "slug": "transfer",
        "status": "running",
        "summary": "Migration batch completed; more source records remain.",
        "items": [],
        "warnings": ["Owner mapping is unsupported for one field."],
        "generatedAt": "2026-08-16T00:10:00+00:00",
        "sourceProvider": "salesforce",
        "destinationProvider": "hubspot",
        "conflictPolicy": "skip",
        "missingOwnerPolicy": "block",
        "invalidEmailPolicy": "block",
        "invalidEmailAuditField": None,
        "selectedRouteKeys": [ROUTE],
        "counts": {"created": 2, "failed": 1},
        "checkpoints": {ROUTE: "cursor-2"},
        "completedRoutes": [],
        "routeCounts": {ROUTE: {"created": 2, "updated": 0, "skipped": 0, "failed": 1}},
        "routePendingRecordIds": {ROUTE: ["s-1"]},
        "routePendingRelationships": {ROUTE: [_pending_relationship()]},
        "routeFailedRecordIds": {ROUTE: ["s-3"]},
        "batchPages": {
            ROUTE: {
                "sourceRecordIds": ["s-1", "s-2", "s-3"],
                "nextCursor": "cursor-2",
                "hasMore": True,
            }
        },
        "hasMore": True,
        "execution": {
            "mode": "continuous",
            "state": "running",
            "jobId": "job-1",
            "attemptId": "task-1:1",
            "attemptNumber": 1,
            "taskRunId": "task-1",
            "batchSize": 200,
            "batchesCompleted": 3,
            "startedAt": "2026-08-16T00:00:00+00:00",
            "lastHeartbeatAt": "2026-08-16T00:10:00+00:00",
            "resumed": False,
            "conflictPolicy": "skip",
            "missingOwnerPolicy": "block",
            "invalidEmailPolicy": "block",
            "invalidEmailAuditField": None,
            "routeManifest": _manifest(),
            "selectedRouteKeys": [ROUTE],
            "routeHighWaterMarks": {ROUTE: None},
            "durableResultsAttemptId": "task-1:1",
        },
    }


def _heartbeat_report() -> dict[str, Any]:
    """Continuous progress report: routeProgress/progress/providerControl."""

    report = _running_batch_report()
    report["routeProgress"] = [
        {
            "routeKey": ROUTE,
            "sourceObject": "accounts",
            "destinationObject": "companies",
            "processed": 2,
            "created": 2,
            "updated": 0,
            "skipped": 0,
            "failed": 1,
            "associationPending": 1,
            "total": 5,
            "remaining": 3,
            "percent": 40.0,
            "etaSeconds": 12,
            "checkpoint": "cursor-2",
            "status": "running",
        }
    ]
    report["progress"] = {
        "processed": 2,
        "total": 5,
        "remaining": 3,
        "percent": 40.0,
        "etaSeconds": 12,
    }
    report["providerControl"] = {"retryAfterSeconds": 0, "throttledRequests": 0}
    return report


def _completed_report() -> dict[str, Any]:
    return {
        "stage": "transfer",
        "slug": "transfer",
        "status": "completed",
        "summary": "Migration execution completed.",
        "items": [],
        "warnings": [],
        "generatedAt": "2026-08-16T01:00:00+00:00",
        "sourceProvider": "salesforce",
        "destinationProvider": "hubspot",
        "conflictPolicy": "skip",
        "missingOwnerPolicy": "block",
        "invalidEmailPolicy": "block",
        "invalidEmailAuditField": None,
        "selectedRouteKeys": [ROUTE],
        "counts": {"created": 3, "skipped": 2},
        "checkpoints": {},
        "completedRoutes": [ROUTE],
        "routeCounts": {ROUTE: {"created": 3, "updated": 0, "skipped": 2, "failed": 0}},
        "routeFailedRecordIds": {},
        "batchPages": {
            ROUTE: {"sourceRecordIds": ["s-4", "s-5"], "nextCursor": None, "hasMore": False}
        },
        "hasMore": False,
        "execution": {
            "mode": "continuous",
            "state": "completed",
            "jobId": "job-1",
            "attemptId": "task-1:2",
            "attemptNumber": 2,
            "taskRunId": "task-1",
            "batchSize": 200,
            "batchesCompleted": 5,
            "startedAt": "2026-08-16T00:00:00+00:00",
            "lastHeartbeatAt": "2026-08-16T01:00:00+00:00",
            "resumed": True,
            "routeManifest": _manifest(),
            "selectedRouteKeys": [ROUTE],
            "routeHighWaterMarks": {ROUTE: "2026-08-15T00:00:00+00:00"},
            "durableResultsAttemptId": "task-1:2",
        },
    }


def _cancelled_report() -> dict[str, Any]:
    report = _running_batch_report()
    report["status"] = "cancelled"
    report["summary"] = (
        "Continuous migration execution was cancelled after the in-flight batch finished."
    )
    execution = dict(report["execution"])
    execution["state"] = "cancelled"
    report["execution"] = execution
    return report


def _manual_batch_report() -> dict[str, Any]:
    """Manual single-batch execution: no execution envelope at all."""

    return {
        "stage": "transfer",
        "slug": "transfer",
        "status": "completed",
        "summary": "Migration execution completed.",
        "items": [],
        "warnings": [],
        "generatedAt": "2026-08-16T02:00:00+00:00",
        "sourceProvider": "salesforce",
        "destinationProvider": "hubspot",
        "conflictPolicy": "skip",
        "missingOwnerPolicy": "block",
        "invalidEmailPolicy": "block",
        "invalidEmailAuditField": None,
        "selectedRouteKeys": [ROUTE],
        "counts": {"created": 1},
        "checkpoints": {},
        "completedRoutes": [ROUTE],
        "routeCounts": {ROUTE: {"created": 1, "updated": 0, "skipped": 0, "failed": 0}},
        "routeFailedRecordIds": {},
        "batchPages": {ROUTE: {"sourceRecordIds": ["s-1"], "nextCursor": None, "hasMore": False}},
        "hasMore": False,
    }


_CORPUS: list[Callable[[], dict[str, Any]]] = [
    _queued_report,
    _running_batch_report,
    _heartbeat_report,
    _completed_report,
    _cancelled_report,
    _manual_batch_report,
]


def _round_trip(report: dict[str, Any]) -> dict[str, Any]:
    return dump_journal(load_journal(report))


# -- byte-identical round trips ------------------------------------------------


@pytest.mark.parametrize("factory", _CORPUS, ids=lambda factory: factory.__name__)
def test_corpus_round_trips_byte_identically(factory: Callable[[], dict[str, Any]]) -> None:
    report = factory()
    assert json.dumps(_round_trip(report)) == json.dumps(report)


@pytest.mark.parametrize("factory", _CORPUS, ids=lambda factory: factory.__name__)
def test_second_round_trip_is_idempotent(factory: Callable[[], dict[str, Any]]) -> None:
    once = _round_trip(factory())
    assert json.dumps(_round_trip(once)) == json.dumps(once)


def test_unknown_host_keys_round_trip_verbatim() -> None:
    report = _running_batch_report()
    report["hostOnlyKey"] = {"nested": [1, 2, {"deep": None}]}
    report["anotherEnvelope"] = "opaque"
    execution = dict(report["execution"])
    execution["hostExecutionKey"] = {"queue": "primary"}
    report["execution"] = execution

    assert json.dumps(_round_trip(report)) == json.dumps(report)


def test_semantically_empty_values_ride_extras_verbatim() -> None:
    # A pre-execution default report: every mechanics key present but empty,
    # plus the host-owned "pending" status. Nothing may be dropped, invented,
    # or reordered.
    report = {
        "stage": "transfer",
        "slug": "transfer",
        "status": "pending",
        "summary": "",
        "items": [],
        "warnings": [],
        "selectedRouteKeys": [],
        "counts": {},
        "checkpoints": {},
        "completedRoutes": [],
        "routeCounts": {},
        "routePendingRecordIds": {},
        "routePendingRelationships": {},
        "routeFailedRecordIds": {},
        "batchPages": {},
        "hasMore": False,
        "execution": {},
    }
    entry = load_journal(report)
    assert entry.status == "queued"  # model default; "pending" stays host-owned
    assert entry.extras["status"] == "pending"
    assert json.dumps(dump_journal(entry)) == json.dumps(report)


def test_production_queued_key_order_normalizes_to_canonical() -> None:
    canonical = _queued_report()
    production_order = {
        key: canonical[key]
        for key in (
            "stage",
            "slug",
            "status",
            "summary",
            "items",
            "warnings",
            "generatedAt",
            "checkpoints",
            "completedRoutes",
            "selectedRouteKeys",  # the queue path appends this after the route state
            "hasMore",
            "execution",
        )
    }
    normalized = _round_trip(production_order)
    assert normalized == production_order  # values and key set survive
    assert json.dumps(normalized) == json.dumps(canonical)  # order is canonical


# -- model direction -----------------------------------------------------------


def test_model_to_report_to_model_preserves_state() -> None:
    scope = ExecutionScope(
        route_manifest=tuple(canonical_route_manifest(_manifest())),
        selected_route_keys=(ROUTE,),
        route_high_water_marks={ROUTE: "2026-08-15T00:00:00+00:00"},
        route_totals={ROUTE: 5},
    )
    snapshot = ExecutionSnapshot(
        checkpoints={ROUTE: "cursor-9"},
        completed_routes=set(),
        route_counts={ROUTE: {"created": 1, "updated": 0, "skipped": 0, "failed": 0}},
        route_pending_record_ids={ROUTE: {"s-1"}},
        route_failed_record_ids={ROUTE: {"s-2"}},
        warnings=["needs review"],
        has_more=True,
    )
    entry = JournalEntry(
        status="running",
        snapshot=snapshot,
        job_id="job-1",
        attempt_id="task-1:1",
        batch_size=100,
        batches_completed=2,
        started_at="2026-08-16T00:00:00+00:00",
        last_heartbeat_at="2026-08-16T00:05:00+00:00",
        resumed=True,
        scope=scope,
        durable_results_attempt_id="task-1:1",
        aggregate_counts={"created": 1, "failed": 1},
    )

    report = dump_journal(entry)
    assert report["execution"]["routeTotals"] == {ROUTE: 5}
    reloaded = load_journal(report)

    assert reloaded.status == entry.status
    assert reloaded.job_id == entry.job_id
    assert reloaded.attempt_id == entry.attempt_id
    assert reloaded.batch_size == entry.batch_size
    assert reloaded.batches_completed == entry.batches_completed
    assert reloaded.started_at == entry.started_at
    assert reloaded.last_heartbeat_at == entry.last_heartbeat_at
    assert reloaded.resumed is True
    assert reloaded.durable_results_attempt_id == entry.durable_results_attempt_id
    assert reloaded.aggregate_counts == entry.aggregate_counts
    assert reloaded.snapshot.checkpoints == snapshot.checkpoints
    assert reloaded.snapshot.route_counts == snapshot.route_counts
    assert reloaded.snapshot.route_pending_record_ids == snapshot.route_pending_record_ids
    assert reloaded.snapshot.route_failed_record_ids == snapshot.route_failed_record_ids
    assert reloaded.snapshot.warnings == snapshot.warnings
    assert reloaded.snapshot.has_more is True
    assert reloaded.scope is not None
    assert reloaded.scope.route_manifest == scope.route_manifest
    assert reloaded.scope.selected_route_keys == scope.selected_route_keys
    assert dict(reloaded.scope.route_high_water_marks) == dict(scope.route_high_water_marks)
    assert dict(reloaded.scope.route_totals) == dict(scope.route_totals)
    assert reloaded.scope.scope_hash == scope.scope_hash


def test_batch_pages_load_into_typed_pages() -> None:
    entry = load_journal(_running_batch_report())
    page = entry.snapshot.batch_pages[ROUTE]
    assert page.source_record_ids == ("s-1", "s-2", "s-3")
    assert page.next_cursor == "cursor-2"
    assert page.has_more is True


def test_pending_relationships_rebuild_the_canonical_entry() -> None:
    report = _running_batch_report()
    # A legacy entry without relationshipMode/associationCategory/typeId:
    # the reader rebuilds the canonical ten-key spelling.
    report["routePendingRelationships"] = {
        ROUTE: [
            {
                "sourceRecordId": "s-1",
                "destinationObject": "companies",
                "relationshipField": "parent",
                "relatedSourceObject": "accounts",
                "relatedSourceRecordId": "s-9",
                "relatedDestinationObject": "companies",
            }
        ]
    }
    dumped = _round_trip(report)
    assert dumped["routePendingRelationships"] == {
        ROUTE: [
            {
                "sourceRecordId": "s-1",
                "destinationObject": "companies",
                "destinationRecordId": None,
                "relationshipField": "parent",
                "relatedSourceObject": "accounts",
                "relatedSourceRecordId": "s-9",
                "relatedDestinationObject": "companies",
                "relationshipMode": "default",
                "associationCategory": None,
                "associationTypeId": None,
            }
        ]
    }


def test_partial_route_counts_normalize_to_four_statuses() -> None:
    report = _running_batch_report()
    report["routeCounts"] = {ROUTE: {"created": 0, "failed": 1}}
    entry = load_journal(report)
    assert entry.snapshot.route_counts == {
        ROUTE: {"created": 0, "updated": 0, "skipped": 0, "failed": 1}
    }


def test_partial_execution_scope_dumps_complete_and_canonical() -> None:
    # Hand-authored state (production tests do this) may omit the selection
    # and high-water marks; the codec completes the canonical scope on save.
    report = {
        "status": "running",
        "execution": {"mode": "continuous", "state": "running", "routeManifest": _manifest()},
    }
    dumped = _round_trip(report)
    assert dumped["execution"]["selectedRouteKeys"] == [ROUTE]
    assert dumped["execution"]["routeHighWaterMarks"] == {}
    assert list(dumped["execution"]) == [
        "mode",
        "state",
        "routeManifest",
        "selectedRouteKeys",
        "routeHighWaterMarks",
    ]


# -- legacy source-keyed checkpoints ------------------------------------------


def test_legacy_source_keyed_state_loads_and_saves_modern() -> None:
    legacy = {
        "status": "running",
        "checkpoints": {"accounts": "cursor-1"},
        "completedRoutes": ["accounts"],
        "routeCounts": {"accounts": {"created": 1, "updated": 0, "skipped": 0, "failed": 0}},
        "routeFailedRecordIds": {"accounts": ["s-3"]},
        "routePendingRecordIds": {"accounts": ["s-4"]},
    }
    entry = load_journal(legacy, route_manifest=_manifest())
    assert entry.snapshot.checkpoints == {ROUTE: "cursor-1"}
    assert entry.snapshot.completed_routes == {ROUTE}
    assert entry.snapshot.route_failed_record_ids == {ROUTE: {"s-3"}}
    assert entry.snapshot.route_pending_record_ids == {ROUTE: {"s-4"}}

    dumped = dump_journal(entry)
    assert dumped["checkpoints"] == {ROUTE: "cursor-1"}
    assert dumped["completedRoutes"] == [ROUTE]
    assert dumped["routeCounts"] == {ROUTE: {"created": 1, "updated": 0, "skipped": 0, "failed": 0}}
    assert "accounts" not in dumped["checkpoints"]  # the bare legacy key is gone


def test_modern_report_passes_the_remap_untouched() -> None:
    report = _running_batch_report()
    assert json.dumps(prepare_route_state(report, _manifest())) == json.dumps(report)


def test_legacy_key_is_rejected_when_the_source_has_filtered_routes() -> None:
    legacy = {"checkpoints": {"accounts": "cursor-1"}}
    with pytest.raises(ExecutionFault) as excinfo:
        prepare_route_state(legacy, _filtered_manifest())
    assert excinfo.value.code == "FERRY_ROUTE_CHECKPOINT_MISMATCH"


def test_legacy_key_collision_with_modern_key_is_rejected() -> None:
    # Production's collision check is order-dependent: a legacy key that
    # resolves onto an already-remapped modern key raises...
    legacy = {"checkpoints": {ROUTE: "cursor-2", "accounts": "cursor-1"}}
    with pytest.raises(ExecutionFault) as excinfo:
        prepare_route_state(legacy, _manifest())
    assert excinfo.value.code == "FERRY_ROUTE_CHECKPOINT_MISMATCH"

    # ...while a modern key that lands after the legacy one overwrites it
    # silently (last key wins), exactly as the production remap does.
    reordered = {"checkpoints": {"accounts": "cursor-1", ROUTE: "cursor-2"}}
    assert prepare_route_state(reordered, _manifest())["checkpoints"] == {ROUTE: "cursor-2"}


def test_unknown_checkpoint_key_is_rejected() -> None:
    with pytest.raises(ExecutionFault) as excinfo:
        prepare_route_state({"checkpoints": {"tickets": "cursor-1"}}, _manifest())
    assert excinfo.value.code == "FERRY_ROUTE_CHECKPOINT_MISMATCH"


def test_saved_manifest_mismatch_is_rejected_before_the_remap() -> None:
    report = {"execution": {"routeManifest": _filtered_manifest()}}
    with pytest.raises(ExecutionFault) as excinfo:
        prepare_route_state(report, _manifest())
    assert excinfo.value.code == "FERRY_ROUTE_MANIFEST_CHANGED"


def test_route_progress_keys_are_validated_by_the_remap() -> None:
    report = {"routeProgress": [{"routeKey": "tickets|tickets", "processed": 1}]}
    with pytest.raises(ExecutionFault) as excinfo:
        prepare_route_state(report, _manifest())
    assert excinfo.value.code == "FERRY_ROUTE_CHECKPOINT_MISMATCH"


# -- canonical manifests -------------------------------------------------------


def test_canonical_route_manifest_sorts_rows_and_drops_extra_keys() -> None:
    rows = list(reversed(_filtered_manifest()))
    rows[0] = {**rows[0], "identityFields": ["email"]}  # v0 manifest extra key
    normalized = canonical_route_manifest(rows)
    assert [row["routeKey"] for row in normalized] == [FILTERED_ROUTE, "contacts|contacts"]
    assert all(
        set(row) == {"routeKey", "sourceObject", "destinationObject", "sourceFilter"}
        for row in normalized
    )


def test_canonical_route_manifest_strips_the_filter_field() -> None:
    rows = [
        {
            "routeKey": FILTERED_ROUTE,
            "sourceObject": "accounts",
            "destinationObject": "companies",
            "sourceFilter": {"field": " is_active ", "operator": "equals", "value": False},
        }
    ]
    normalized = canonical_route_manifest(rows)
    assert normalized[0]["sourceFilter"] == {
        "field": "is_active",
        "operator": "equals",
        "value": False,
    }


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (None, "FERRY_ROUTE_MANIFEST_MISSING"),
        ([], "FERRY_ROUTE_MANIFEST_MISSING"),
        (["route"], "FERRY_ROUTE_MANIFEST_INVALID"),
        ([{"sourceObject": "", "destinationObject": "companies"}], "FERRY_ROUTE_MANIFEST_INVALID"),
        (
            [
                {
                    "routeKey": ROUTE,
                    "sourceObject": "accounts",
                    "destinationObject": "companies",
                    "sourceFilter": {"field": "is_active", "operator": "gt", "value": True},
                }
            ],
            "FERRY_ROUTE_MANIFEST_INVALID",
        ),
        (
            [
                {
                    "routeKey": ROUTE,
                    "sourceObject": "accounts",
                    "destinationObject": "companies",
                    "sourceFilter": {"field": "is_active", "value": 1},
                }
            ],
            "FERRY_ROUTE_MANIFEST_INVALID",
        ),
        (
            [
                {
                    "routeKey": "wrong|key",
                    "sourceObject": "accounts",
                    "destinationObject": "companies",
                    "sourceFilter": None,
                }
            ],
            "FERRY_ROUTE_MANIFEST_INVALID",
        ),
        (_manifest() + _manifest(), "FERRY_ROUTE_MANIFEST_INVALID"),
    ],
    ids=[
        "missing",
        "empty",
        "non-dict-row",
        "blank-object",
        "bad-operator",
        "non-bool-value",
        "mismatched-route-key",
        "duplicate-route-key",
    ],
)
def test_canonical_route_manifest_faults(raw: Any, code: str) -> None:
    with pytest.raises(ExecutionFault) as excinfo:
        canonical_route_manifest(raw)
    assert excinfo.value.code == code


def test_require_matching_route_manifest_accepts_and_rejects() -> None:
    field = MigrationMappingField(
        source_field="name", target_object="companies", target_field="name"
    )
    groups: list[MappingGroup] = [("accounts", "companies", None, [field])]
    assert require_matching_route_manifest(groups, _manifest()) == canonical_route_manifest(
        _manifest()
    )
    with pytest.raises(ExecutionFault) as excinfo:
        require_matching_route_manifest(groups, _filtered_manifest())
    assert excinfo.value.code == "FERRY_ROUTE_MANIFEST_CHANGED"


def test_execution_route_manifest_reads_the_saved_manifest() -> None:
    execution = {"routeManifest": _manifest()}
    assert execution_route_manifest(execution) == canonical_route_manifest(_manifest())
    with pytest.raises(ExecutionFault) as excinfo:
        execution_route_manifest({})
    assert excinfo.value.code == "FERRY_ROUTE_MANIFEST_MISSING"


# -- route selection -----------------------------------------------------------


def test_selected_route_keys_defaults_and_orders_by_manifest() -> None:
    manifest = canonical_route_manifest(_filtered_manifest())
    all_keys = [FILTERED_ROUTE, "contacts|contacts"]
    assert selected_route_keys(manifest, None) == all_keys
    assert selected_route_keys(manifest, "") == all_keys
    assert selected_route_keys(manifest, []) == all_keys
    assert selected_route_keys(manifest, ["", "  "]) == all_keys
    # Selection order follows the manifest, not the request.
    assert selected_route_keys(manifest, ["contacts|contacts", FILTERED_ROUTE]) == all_keys
    assert selected_route_keys(manifest, ["contacts|contacts"]) == ["contacts|contacts"]


def test_selected_route_keys_faults() -> None:
    manifest = canonical_route_manifest(_manifest())
    with pytest.raises(ExecutionFault) as excinfo:
        selected_route_keys(manifest, "accounts|companies")
    assert excinfo.value.code == "FERRY_EXECUTION_ROUTE_INVALID"
    with pytest.raises(ExecutionFault) as excinfo:
        selected_route_keys(manifest, ["tickets|tickets", "aaa|bbb"])
    assert excinfo.value.code == "FERRY_EXECUTION_ROUTE_INVALID"
    assert excinfo.value.details == {"unknownRouteKeys": ["aaa|bbb", "tickets|tickets"]}


def test_route_keys_by_pair_groups_filtered_routes() -> None:
    manifest = [
        *_filtered_manifest(),
        {
            "routeKey": "accounts|companies|is_active=equals:true",
            "sourceObject": "accounts",
            "destinationObject": "companies",
            "sourceFilter": {"field": "is_active", "operator": "equals", "value": True},
        },
    ]
    assert route_keys_by_pair(manifest) == {
        ("accounts", "companies"): [
            FILTERED_ROUTE,
            "accounts|companies|is_active=equals:true",
        ],
        ("contacts", "contacts"): ["contacts|contacts"],
    }


def test_filter_mapping_groups_selects_by_route_key() -> None:
    field = MigrationMappingField(
        source_field="name", target_object="companies", target_field="name"
    )
    accounts: MappingGroup = ("accounts", "companies", None, [field])
    contacts: MappingGroup = ("contacts", "contacts", None, [field])
    assert filter_mapping_groups([accounts, contacts], [ROUTE]) == [accounts]
    assert filter_mapping_groups([accounts, contacts], []) == []


# -- high-water marks ----------------------------------------------------------


def test_high_water_marks_normalize_and_sort() -> None:
    manifest = canonical_route_manifest(_filtered_manifest())
    raw = {"contacts|contacts": " 2026-08-15 ", FILTERED_ROUTE: None}
    normalized = execution_route_high_water_marks({"routeHighWaterMarks": raw}, manifest)
    assert list(normalized) == [FILTERED_ROUTE, "contacts|contacts"]
    assert normalized == {FILTERED_ROUTE: None, "contacts|contacts": "2026-08-15"}
    assert execution_route_high_water_marks({}, manifest) == {}
    assert execution_route_high_water_marks({"routeHighWaterMarks": {}}, manifest) == {}


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-dict",
        {"other|route": "cursor"},
        {ROUTE: "cursor", "extra|route": "cursor"},
        {ROUTE: "   "},
    ],
    ids=["non-dict", "wrong-key-set", "extra-key", "blank-value"],
)
def test_high_water_mark_faults(raw: Any) -> None:
    manifest = canonical_route_manifest(_manifest())
    with pytest.raises(ExecutionFault) as excinfo:
        execution_route_high_water_marks({"routeHighWaterMarks": raw}, manifest)
    assert excinfo.value.code == "FERRY_EXECUTION_HIGH_WATER_MARK_INVALID"


def test_load_rejects_a_saved_selection_outside_the_manifest() -> None:
    report = _running_batch_report()
    execution = dict(report["execution"])
    execution["selectedRouteKeys"] = ["tickets|tickets"]
    report["execution"] = execution
    with pytest.raises(ExecutionFault) as excinfo:
        load_journal(report)
    assert excinfo.value.code == "FERRY_EXECUTION_ROUTE_INVALID"


# -- cancellation, heartbeat, readers -----------------------------------------


def test_execution_is_cancelled_checks_status_and_state() -> None:
    assert execution_is_cancelled("cancelled", {}) is True
    assert execution_is_cancelled("running", {"state": "cancelled"}) is True
    assert execution_is_cancelled("running", {"state": "running"}) is False
    assert execution_is_cancelled(None, "not-a-dict") is False


def test_parse_datetime_accepts_iso_z_naive_and_datetimes() -> None:
    assert parse_datetime("2026-08-16T00:00:00Z") == datetime(2026, 8, 16, tzinfo=UTC)
    assert parse_datetime("2026-08-16T09:00:00+09:00") == datetime(2026, 8, 16, tzinfo=UTC)
    naive = parse_datetime("2026-08-16T00:00:00")
    assert naive is not None and naive.tzinfo is UTC
    aware = datetime(2026, 8, 16, tzinfo=UTC)
    assert parse_datetime(aware) == aware
    assert parse_datetime("") is None
    assert parse_datetime(None) is None
    assert parse_datetime("not a timestamp") is None


def test_heartbeat_freshness_prefers_the_execution_heartbeat() -> None:
    fresh = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    assert execution_heartbeat_is_recent({"lastHeartbeatAt": fresh}, None) is True
    assert execution_heartbeat_is_recent({"lastHeartbeatAt": stale}, None) is False
    # Falls back to the stage row's updated_at when no heartbeat was written.
    assert execution_heartbeat_is_recent({}, fresh) is True
    assert execution_heartbeat_is_recent({}, stale) is False
    assert execution_heartbeat_is_recent({}, None) is False


def test_report_readers_filter_blank_keys_and_values() -> None:
    report = {
        "checkpoints": {ROUTE: "cursor-1", "": "cursor-2", "other|route": "  "},
        "routeCounts": {ROUTE: {"created": "2"}, "": {"created": 1}, "bad|route": []},
        "routePendingRecordIds": {ROUTE: ["s-1", " "], "": ["s-2"], "x|y": "not-a-list"},
        "routeFailedRecordIds": {ROUTE: ["s-3", ""]},
        "completedRoutes": [ROUTE, "", 7],
    }
    assert report_checkpoints(report) == {ROUTE: "cursor-1"}
    assert report_route_counts(report) == {
        ROUTE: {"created": 2, "updated": 0, "skipped": 0, "failed": 0}
    }
    assert report_route_pending_record_ids(report) == {ROUTE: {"s-1"}}
    assert report_route_failed_record_ids(report) == {ROUTE: {"s-3"}}
    assert report_progress_marker(report) == (
        ((ROUTE, "cursor-1"),),
        ("7", ROUTE),
    )
