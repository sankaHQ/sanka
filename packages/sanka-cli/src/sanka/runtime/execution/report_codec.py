# SPDX-License-Identifier: AGPL-3.0-only
"""Stage-report codec: persisted transfer-report dicts <-> the execution model.

Faithful port of the production pure state helpers for the transfer stage
report: canonical route manifests (with equality checks), route selection,
saved high-water-mark validation, the legacy source-keyed checkpoint remap,
heartbeat freshness, and the :class:`JournalEntry` <->
report-payload conversion. The camelCase spellings (``checkpoints``,
``completedRoutes``, ``routeCounts``, ``routePendingRecordIds``,
``routePendingRelationships``, ``routeFailedRecordIds``, ``batchPages``,
``routeHighWaterMarks``, ``durableResultsAttemptId``, …) are the persisted
production contract — do not rename them.

Round-trip contract
===================

``dump_journal(load_journal(report))`` is byte-identical (``json.dumps``
equality) for reports laid out in the codec's canonical key order, which is
the production *batch-construction* order with the heartbeat report's
appended keys (``execution`` before ``routeProgress``/``progress``/
``providerControl``, matching the heartbeat write). Two production
construction sites order a handful of keys differently (the fresh queued
report places ``selectedRouteKeys`` after ``completedRoutes``; a claimed
attempt appends ``attemptId``/``attemptNumber``/``taskRunId`` at the end of
``execution``); the codec normalizes those to the canonical order. Key order
is not observable through the production store (the stage report is
persisted as JSONB, which re-orders keys) — presence, spellings, and values
are the binding contract, and those round-trip exactly.

Host-envelope keys the codec does not own (``summary``, ``generatedAt``,
provider echoes, policy echoes, ``progress``, …) survive a load -> save
round trip verbatim through :attr:`JournalEntry.extras`; unknown keys keep
their values untouched and are emitted after the canonical block in their
original order. A key whose value is present but semantically empty (``{}``,
``[]``, ``0``, ``false``, ``null``, ``""``) also rides ``extras`` verbatim,
so the codec never invents or drops a key. Keys the codec owns must not be
placed in ``extras`` by hand; on conflict the ``extras`` value wins (that is
what preserves e.g. a legacy ``"status": "pending"`` byte-for-byte).

Value normalizations the production reader itself performs are preserved:
``routeCounts`` rows normalize to the four-status shape, blank checkpoint
keys/values drop, pending-relationship entries are rebuilt with the
canonical ten-key spelling, and saved high-water-mark maps re-sort by route
key. A parsed scope always dumps complete — a hand-authored execution dict
carrying only ``routeManifest`` gains its canonical ``selectedRouteKeys``
and ``routeHighWaterMarks`` on save, exactly what the production writer
would next persist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast, get_args

from sanka.runtime.execution.errors import ExecutionFault
from sanka.runtime.execution.model import (
    BatchPage,
    ExecutionScope,
    ExecutionSnapshot,
    ExecutionStatus,
    JournalEntry,
)
from sanka.runtime.mapping.pending_relationships import (
    PendingRelationshipsByRoute,
    report_pending_relationships,
)
from sanka.runtime.mapping.record_mapping import (
    MappingGroup,
    MappingRouteManifest,
    mapping_group_key,
    mapping_route_manifest,
)
from sanka_connector import SourceFilter

HEARTBEAT_FRESHNESS = timedelta(minutes=5)
"""An execution job counts as active while its heartbeat is this recent."""

_EXECUTION_STATUS_VALUES: frozenset[str] = frozenset(get_args(ExecutionStatus))

_ROUTE_COUNT_STATUSES = ("created", "updated", "skipped", "failed")

_INVALID_MANIFEST_MESSAGE = "The saved Sanka route manifest is invalid."

_TOP_LEVEL_KEY_ORDER = (
    "stage",
    "slug",
    "status",
    "summary",
    "items",
    "warnings",
    "generatedAt",
    "sourceProvider",
    "destinationProvider",
    "conflictPolicy",
    "missingOwnerPolicy",
    "invalidEmailPolicy",
    "invalidEmailAuditField",
    "selectedRouteKeys",
    "counts",
    "checkpoints",
    "completedRoutes",
    "routeCounts",
    "routePendingRecordIds",
    "routePendingRelationships",
    "routeFailedRecordIds",
    "batchPages",
    "hasMore",
    "execution",
    "routeProgress",
    "progress",
    "providerControl",
)

_EXECUTION_KEY_ORDER = (
    "mode",
    "state",
    "jobId",
    "attemptId",
    "attemptNumber",
    "taskRunId",
    "batchSize",
    "batchesCompleted",
    "startedAt",
    "lastHeartbeatAt",
    "resumed",
    "conflictPolicy",
    "missingOwnerPolicy",
    "invalidEmailPolicy",
    "invalidEmailAuditField",
    "routeManifest",
    "selectedRouteKeys",
    "routeHighWaterMarks",
    "routeTotals",
    "durableResultsAttemptId",
)

_SCOPE_EXECUTION_KEYS = frozenset(
    {"routeManifest", "selectedRouteKeys", "routeHighWaterMarks", "routeTotals"}
)


# -- route manifests ----------------------------------------------------------


def _source_filter_from_payload(raw: Any) -> SourceFilter:
    if not isinstance(raw, dict):
        raise ValueError("sourceFilter must be an object")
    unknown = set(raw) - {"field", "operator", "value"}
    if unknown:
        raise ValueError(f"sourceFilter has unsupported keys: {sorted(unknown)}")
    field = raw.get("field")
    if not isinstance(field, str) or not field.strip():
        raise ValueError("sourceFilter.field is required")
    if raw.get("operator", "equals") != "equals":
        raise ValueError("sourceFilter.operator must be 'equals'")
    if "value" not in raw or not isinstance(raw["value"], bool):
        raise ValueError("sourceFilter.value must be a boolean")
    # Production's model validator strips the field before the route key is
    # derived; keep that so saved route identities keep matching.
    return SourceFilter(field=field.strip(), operator="equals", value=raw["value"])


def _source_filter_payload(source_filter: SourceFilter) -> dict[str, Any]:
    return {
        "field": source_filter.field,
        "operator": source_filter.operator,
        "value": source_filter.value,
    }


def canonical_route_manifest(raw: Any) -> MappingRouteManifest:
    """Normalize + verify a saved route manifest; sorted by route key."""

    if not isinstance(raw, list) or not raw:
        raise ExecutionFault(
            "A complete Sanka route manifest is required before continuous execution.",
            code="SANKA_MIGRATE_ROUTE_MANIFEST_MISSING",
        )
    normalized: MappingRouteManifest = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ExecutionFault(
                _INVALID_MANIFEST_MESSAGE, code="SANKA_MIGRATE_ROUTE_MANIFEST_INVALID"
            )
        source_object = str(item.get("sourceObject") or "").strip()
        destination_object = str(item.get("destinationObject") or "").strip()
        if not source_object or not destination_object:
            raise ExecutionFault(
                _INVALID_MANIFEST_MESSAGE, code="SANKA_MIGRATE_ROUTE_MANIFEST_INVALID"
            )
        raw_filter = item.get("sourceFilter")
        try:
            source_filter = (
                _source_filter_from_payload(raw_filter) if raw_filter is not None else None
            )
        except ValueError as exc:
            raise ExecutionFault(
                "The saved Sanka route manifest contains an invalid source filter.",
                code="SANKA_MIGRATE_ROUTE_MANIFEST_INVALID",
            ) from exc
        route_key = mapping_group_key(source_object, destination_object, source_filter)
        saved_route_key = str(item.get("routeKey") or route_key).strip()
        if saved_route_key != route_key or route_key in seen:
            raise ExecutionFault(
                "The saved Sanka route identity does not match its source filter.",
                code="SANKA_MIGRATE_ROUTE_MANIFEST_INVALID",
            )
        seen.add(route_key)
        normalized.append(
            {
                "routeKey": route_key,
                "sourceObject": source_object,
                "destinationObject": destination_object,
                "sourceFilter": (
                    _source_filter_payload(source_filter) if source_filter is not None else None
                ),
            }
        )
    return sorted(normalized, key=lambda row: str(row["routeKey"]))


def require_matching_route_manifest(
    groups: list[MappingGroup],
    expected_route_manifest: Any,
) -> MappingRouteManifest:
    """The current mapping groups must still describe the expected manifest."""

    current = canonical_route_manifest(mapping_route_manifest(groups))
    expected = canonical_route_manifest(expected_route_manifest)
    if current != expected:
        raise ExecutionFault(
            "The Sanka mapping routes changed after continuous execution was queued. "
            "Review the source filters before starting a new job.",
            code="SANKA_MIGRATE_ROUTE_MANIFEST_CHANGED",
        )
    return expected


def execution_route_manifest(execution: dict[str, Any]) -> MappingRouteManifest:
    """The canonical manifest a queued execution dict is bound to."""

    return canonical_route_manifest(execution.get("routeManifest"))


def selected_route_keys(
    route_manifest: MappingRouteManifest,
    requested_route_keys: Any,
) -> list[str]:
    """Resolve a route selection against the manifest, in manifest order."""

    known_route_keys = [str(row["routeKey"]) for row in route_manifest]
    if requested_route_keys is None or requested_route_keys == "":
        return known_route_keys
    if not isinstance(requested_route_keys, list | tuple):
        raise ExecutionFault(
            "Sanka execution route keys must be a list.",
            code="SANKA_MIGRATE_EXECUTION_ROUTE_INVALID",
        )
    requested = {str(route_key or "").strip() for route_key in requested_route_keys}
    requested.discard("")
    if not requested:
        return known_route_keys
    unknown = sorted(requested.difference(known_route_keys))
    if unknown:
        raise ExecutionFault(
            "Sanka execution includes routes outside the reviewed mapping.",
            code="SANKA_MIGRATE_EXECUTION_ROUTE_INVALID",
            details={"unknownRouteKeys": unknown},
        )
    return [route_key for route_key in known_route_keys if route_key in requested]


def route_keys_by_pair(
    route_manifest: MappingRouteManifest,
) -> dict[tuple[str, str], list[str]]:
    """Route keys grouped by (source object, destination object) pair."""

    routes: dict[tuple[str, str], list[str]] = {}
    for row in canonical_route_manifest(route_manifest):
        pair = (str(row["sourceObject"]), str(row["destinationObject"]))
        routes.setdefault(pair, []).append(str(row["routeKey"]))
    return routes


def filter_mapping_groups(
    groups: list[MappingGroup],
    selected_route_keys: list[str],
) -> list[MappingGroup]:
    """The mapping groups whose route keys are in the selection, in order."""

    selected = set(selected_route_keys)
    return [
        group for group in groups if mapping_group_key(group[0], group[1], group[2]) in selected
    ]


def execution_route_high_water_marks(
    execution: dict[str, Any],
    route_manifest: MappingRouteManifest,
) -> dict[str, str | None]:
    """Validate a saved high-water-mark map against the reviewed routes."""

    raw = execution.get("routeHighWaterMarks")
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        raise ExecutionFault(
            "The saved Sanka route high-water marks are invalid.",
            code="SANKA_MIGRATE_EXECUTION_HIGH_WATER_MARK_INVALID",
        )
    known_route_keys = {str(row["routeKey"]) for row in route_manifest}
    if set(raw) != known_route_keys:
        raise ExecutionFault(
            "The saved Sanka route high-water marks do not match the reviewed routes.",
            code="SANKA_MIGRATE_EXECUTION_HIGH_WATER_MARK_INVALID",
        )
    normalized: dict[str, str | None] = {}
    for route_key in sorted(known_route_keys):
        value = raw.get(route_key)
        if value is None:
            normalized[route_key] = None
            continue
        normalized_value = str(value).strip()
        if not normalized_value:
            raise ExecutionFault(
                "The saved Sanka route high-water mark is blank.",
                code="SANKA_MIGRATE_EXECUTION_HIGH_WATER_MARK_INVALID",
            )
        normalized[route_key] = normalized_value
    return normalized


# -- legacy checkpoint remap --------------------------------------------------


def prepare_route_state(
    report: dict[str, Any],
    route_manifest: Any,
) -> dict[str, Any]:
    """Remap legacy source-keyed route state onto the reviewed manifest.

    Legacy reports keyed checkpoints (and the sibling per-route dicts) by
    the bare source object name. Each such key resolves to the manifest's
    single filter-less route for that source object; ambiguity or an unknown
    key raises ``SANKA_MIGRATE_ROUTE_CHECKPOINT_MISMATCH``. A saved manifest that no
    longer matches raises ``SANKA_MIGRATE_ROUTE_MANIFEST_CHANGED``.
    """

    normalized_manifest = canonical_route_manifest(route_manifest)
    normalized = dict(report)
    execution = normalized.get("execution")
    execution = dict(execution) if isinstance(execution, dict) else {}
    saved_manifest = execution.get("routeManifest")
    if (
        saved_manifest is not None
        and canonical_route_manifest(saved_manifest) != normalized_manifest
    ):
        raise ExecutionFault(
            "The saved Sanka checkpoints belong to different mapping routes.",
            code="SANKA_MIGRATE_ROUTE_MANIFEST_CHANGED",
        )

    known_keys = {str(row["routeKey"]) for row in normalized_manifest}
    legacy_source_keys: dict[str, str] = {}
    for row in normalized_manifest:
        if row["sourceFilter"] is None:
            source_object = str(row["sourceObject"])
            if source_object in legacy_source_keys:
                legacy_source_keys[source_object] = ""
            else:
                legacy_source_keys[source_object] = str(row["routeKey"])

    def resolve_route_key(raw_key: Any) -> str:
        route_key = str(raw_key or "").strip()
        if route_key in known_keys:
            return route_key
        if "|" not in route_key and legacy_source_keys.get(route_key):
            return legacy_source_keys[route_key]
        raise ExecutionFault(
            "A saved Sanka checkpoint does not match the reviewed source-filter route.",
            code="SANKA_MIGRATE_ROUTE_CHECKPOINT_MISMATCH",
        )

    for field_name in (
        "checkpoints",
        "routeCounts",
        "routePendingRecordIds",
        "routePendingRelationships",
        "routeFailedRecordIds",
    ):
        value = normalized.get(field_name)
        if not isinstance(value, dict):
            continue
        remapped: dict[str, Any] = {}
        for raw_key, field_value in value.items():
            route_key = resolve_route_key(raw_key)
            if route_key in remapped and str(raw_key) != route_key:
                raise ExecutionFault(
                    "Multiple saved Sanka checkpoints resolve to the same route.",
                    code="SANKA_MIGRATE_ROUTE_CHECKPOINT_MISMATCH",
                )
            remapped[route_key] = field_value
        normalized[field_name] = remapped

    completed_routes = normalized.get("completedRoutes")
    if isinstance(completed_routes, list):
        normalized["completedRoutes"] = sorted(
            {resolve_route_key(route_key) for route_key in completed_routes}
        )
    route_progress = normalized.get("routeProgress")
    if isinstance(route_progress, list):
        for row in route_progress:
            if isinstance(row, dict) and str(row.get("routeKey") or "").strip():
                resolve_route_key(row["routeKey"])
    return normalized


# -- report readers -----------------------------------------------------------


def _checkpoints_value(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }


def report_checkpoints(report: dict[str, Any]) -> dict[str, str]:
    return _checkpoints_value(report.get("checkpoints"))


def _route_counts_value(raw: Any) -> dict[str, dict[str, int]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, int]] = {}
    for route_key, counts in raw.items():
        if not str(route_key).strip() or not isinstance(counts, dict):
            continue
        normalized[str(route_key)] = {
            status: max(0, int(counts.get(status) or 0)) for status in _ROUTE_COUNT_STATUSES
        }
    return normalized


def report_route_counts(report: dict[str, Any]) -> dict[str, dict[str, int]]:
    return _route_counts_value(report.get("routeCounts"))


def _route_record_ids_value(raw: Any) -> dict[str, set[str]]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(route_key): {str(record_id) for record_id in record_ids if str(record_id).strip()}
        for route_key, record_ids in raw.items()
        if str(route_key).strip() and isinstance(record_ids, list)
    }


def report_route_pending_record_ids(report: dict[str, Any]) -> dict[str, set[str]]:
    return _route_record_ids_value(report.get("routePendingRecordIds"))


def report_route_failed_record_ids(report: dict[str, Any]) -> dict[str, set[str]]:
    return _route_record_ids_value(report.get("routeFailedRecordIds"))


def report_progress_marker(
    report: dict[str, Any],
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """Stall detector over a raw report: (sorted checkpoints, completed)."""

    raw_completed = report.get("completedRoutes")
    completed = tuple(
        sorted(
            str(item)
            for item in (raw_completed if isinstance(raw_completed, list) else [])
            if str(item).strip()
        )
    )
    return tuple(sorted(report_checkpoints(report).items())), completed


# -- cancellation + heartbeat -------------------------------------------------


def execution_is_cancelled(stage_status: str | None, execution: Any) -> bool:
    """A cancellation is recorded on the stage status or the execution state."""

    return bool(
        stage_status == "cancelled"
        or (isinstance(execution, dict) and execution.get("state") == "cancelled")
    )


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def execution_heartbeat_is_recent(
    execution: dict[str, Any],
    stage_updated_at: datetime | str | None,
) -> bool:
    heartbeat = parse_datetime(execution.get("lastHeartbeatAt")) or parse_datetime(stage_updated_at)
    return heartbeat is not None and datetime.now(UTC) - heartbeat < HEARTBEAT_FRESHNESS


# -- journal load -------------------------------------------------------------


def _batch_pages_value(raw: Any) -> dict[str, BatchPage]:
    if not isinstance(raw, dict):
        return {}
    pages: dict[str, BatchPage] = {}
    for route_key, raw_page in raw.items():
        if not str(route_key).strip() or not isinstance(raw_page, dict):
            continue
        raw_ids = raw_page.get("sourceRecordIds")
        source_record_ids = tuple(
            str(record_id)
            for record_id in (raw_ids if isinstance(raw_ids, list) else [])
            if str(record_id).strip()
        )
        cursor = raw_page.get("nextCursor")
        next_cursor = cursor if isinstance(cursor, str) or cursor is None else str(cursor)
        pages[str(route_key)] = BatchPage(
            source_record_ids=source_record_ids,
            next_cursor=next_cursor,
            has_more=bool(raw_page.get("hasMore")),
        )
    return pages


def _aggregate_counts_value(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for status, value in raw.items():
        if not isinstance(value, int) or isinstance(value, bool):
            return {}
        counts[str(status)] = value
    return counts


def _route_totals_value(raw: Any) -> dict[str, int | None]:
    if not isinstance(raw, dict) or not raw:
        return {}
    totals: dict[str, int | None] = {}
    for route_key, value in raw.items():
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            return {}
        totals[str(route_key)] = value
    return totals


def _parse_execution(
    execution: dict[str, Any],
    entry: JournalEntry,
) -> dict[str, Any]:
    """Consume owned execution keys into ``entry``; return leftover extras."""

    scope: ExecutionScope | None = None
    consumed_totals = False
    if execution.get("routeManifest") is not None:
        manifest = execution_route_manifest(execution)
        selected = selected_route_keys(manifest, execution.get("selectedRouteKeys"))
        high_water_marks = execution_route_high_water_marks(execution, manifest)
        totals = _route_totals_value(execution.get("routeTotals"))
        consumed_totals = bool(totals) or execution.get("routeTotals") is None
        scope = ExecutionScope(
            route_manifest=tuple(manifest),
            selected_route_keys=tuple(selected),
            route_high_water_marks=high_water_marks,
            route_totals=totals,
        )
    entry.scope = scope

    extras: dict[str, Any] = {}
    for key, value in execution.items():
        if key in _SCOPE_EXECUTION_KEYS:
            if scope is not None and (key != "routeTotals" or consumed_totals):
                continue
            extras[key] = value
        elif key == "jobId" and isinstance(value, str) and value.strip():
            entry.job_id = value
        elif key == "attemptId" and isinstance(value, str) and value.strip():
            entry.attempt_id = value
        elif key == "durableResultsAttemptId" and isinstance(value, str) and value.strip():
            entry.durable_results_attempt_id = value
        elif key == "startedAt" and isinstance(value, str) and value.strip():
            entry.started_at = value
        elif key == "lastHeartbeatAt" and isinstance(value, str) and value.strip():
            entry.last_heartbeat_at = value
        elif (
            key == "batchSize" and isinstance(value, int) and not isinstance(value, bool)
        ) and value > 0:
            entry.batch_size = value
        elif (
            key == "batchesCompleted" and isinstance(value, int) and not isinstance(value, bool)
        ) and value > 0:
            entry.batches_completed = value
        elif key == "resumed" and value is True:
            entry.resumed = True
        else:
            extras[key] = value
    return extras


def load_journal(
    report: dict[str, Any],
    *,
    route_manifest: Any | None = None,
) -> JournalEntry:
    """Parse a persisted transfer-stage report into a :class:`JournalEntry`.

    When ``route_manifest`` is provided the report first goes through the
    legacy source-keyed checkpoint remap (:func:`prepare_route_state`),
    exactly as the production queue path does before touching saved state.
    """

    normalized = (
        prepare_route_state(report, route_manifest) if route_manifest is not None else dict(report)
    )

    entry = JournalEntry(
        status="queued",
        snapshot=ExecutionSnapshot(),
        job_id=None,
        attempt_id=None,
        batch_size=0,
        batches_completed=0,
        started_at=None,
        last_heartbeat_at=None,
        resumed=False,
        scope=None,
        durable_results_attempt_id=None,
    )
    snapshot = entry.snapshot
    extras = entry.extras

    for key, value in normalized.items():
        if key == "status":
            if isinstance(value, str) and value in _EXECUTION_STATUS_VALUES:
                entry.status = cast("ExecutionStatus", value)
            else:
                extras[key] = value
        elif key == "counts":
            counts = _aggregate_counts_value(value)
            if counts:
                entry.aggregate_counts = counts
            else:
                extras[key] = value
        elif key == "checkpoints":
            checkpoints = _checkpoints_value(value)
            if checkpoints:
                snapshot.checkpoints = checkpoints
            else:
                extras[key] = value
        elif key == "completedRoutes":
            completed = (
                {str(item) for item in value if str(item).strip()}
                if isinstance(value, list)
                else set()
            )
            if completed:
                snapshot.completed_routes = completed
            else:
                extras[key] = value
        elif key == "routeCounts":
            route_counts = _route_counts_value(value)
            if route_counts:
                snapshot.route_counts = route_counts
            else:
                extras[key] = value
        elif key == "routePendingRecordIds":
            pending_ids = _route_record_ids_value(value)
            if any(pending_ids.values()):
                snapshot.route_pending_record_ids = pending_ids
            else:
                extras[key] = value
        elif key == "routePendingRelationships":
            pending: PendingRelationshipsByRoute = report_pending_relationships(normalized)
            if pending:
                snapshot.route_pending_relationships = pending
            else:
                extras[key] = value
        elif key == "routeFailedRecordIds":
            failed_ids = _route_record_ids_value(value)
            if any(failed_ids.values()):
                snapshot.route_failed_record_ids = failed_ids
            else:
                extras[key] = value
        elif key == "batchPages":
            pages = _batch_pages_value(value)
            if pages:
                snapshot.batch_pages = pages
            else:
                extras[key] = value
        elif key == "warnings":
            if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
                snapshot.warnings = list(value)
            else:
                extras[key] = value
        elif key == "hasMore":
            if value is True:
                snapshot.has_more = True
            else:
                extras[key] = value
        elif key == "routeProgress":
            if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
                entry.route_progress = [dict(row) for row in value]
            else:
                extras[key] = value
        elif key == "providerControl":
            if isinstance(value, dict) and value:
                entry.provider_control = dict(value)
            else:
                extras[key] = value
        elif key == "execution":
            if isinstance(value, dict):
                execution_extras = _parse_execution(value, entry)
                if execution_extras or not value:
                    # Leftover host keys — or a literally empty execution
                    # dict, which must survive the round trip as ``{}``.
                    extras["execution"] = execution_extras
            else:
                extras[key] = value
        else:
            extras[key] = value
    return entry


# -- journal dump -------------------------------------------------------------


def _dump_execution(entry: JournalEntry) -> Any | None:
    execution_extras = entry.extras.get("execution")
    if execution_extras is not None and not isinstance(execution_extras, dict):
        return execution_extras
    has_extras_execution = "execution" in entry.extras
    sub_extras: dict[str, Any] = dict(execution_extras) if execution_extras else {}

    owned: dict[str, Any] = {}
    if entry.job_id:
        owned["jobId"] = entry.job_id
    if entry.attempt_id:
        owned["attemptId"] = entry.attempt_id
    if entry.batch_size > 0:
        owned["batchSize"] = entry.batch_size
    if entry.batches_completed > 0:
        owned["batchesCompleted"] = entry.batches_completed
    if entry.started_at:
        owned["startedAt"] = entry.started_at
    if entry.last_heartbeat_at:
        owned["lastHeartbeatAt"] = entry.last_heartbeat_at
    if entry.resumed:
        owned["resumed"] = True
    scope = entry.scope
    if scope is not None:
        owned["routeManifest"] = [dict(row) for row in scope.route_manifest]
        owned["selectedRouteKeys"] = list(scope.selected_route_keys)
        owned["routeHighWaterMarks"] = dict(scope.route_high_water_marks)
        if scope.route_totals:
            owned["routeTotals"] = dict(scope.route_totals)
    if entry.durable_results_attempt_id:
        owned["durableResultsAttemptId"] = entry.durable_results_attempt_id

    if not owned and not sub_extras and not has_extras_execution:
        return None
    payload: dict[str, Any] = {}
    for key in _EXECUTION_KEY_ORDER:
        if key in sub_extras:
            payload[key] = sub_extras[key]
        elif key in owned:
            payload[key] = owned[key]
    for key, value in sub_extras.items():
        if key not in payload:
            payload[key] = value
    return payload


def dump_journal(entry: JournalEntry) -> dict[str, Any]:
    """Render a :class:`JournalEntry` as the persisted stage-report dict."""

    snapshot = entry.snapshot
    owned: dict[str, Any] = {"status": entry.status}
    if snapshot.warnings:
        owned["warnings"] = list(snapshot.warnings)
    if entry.aggregate_counts:
        owned["counts"] = dict(entry.aggregate_counts)
    if snapshot.checkpoints:
        owned["checkpoints"] = {str(key): str(value) for key, value in snapshot.checkpoints.items()}
    if snapshot.completed_routes:
        owned["completedRoutes"] = sorted(snapshot.completed_routes)
    if snapshot.route_counts:
        owned["routeCounts"] = {
            route_key: {
                status: max(0, int(counts.get(status) or 0)) for status in _ROUTE_COUNT_STATUSES
            }
            for route_key, counts in snapshot.route_counts.items()
        }
    if any(snapshot.route_pending_record_ids.values()):
        owned["routePendingRecordIds"] = {
            route_key: sorted(record_ids)
            for route_key, record_ids in snapshot.route_pending_record_ids.items()
            if record_ids
        }
    if any(snapshot.route_pending_relationships.values()):
        owned["routePendingRelationships"] = {
            route_key: list(pending.values())
            for route_key, pending in snapshot.route_pending_relationships.items()
            if pending
        }
    if any(snapshot.route_failed_record_ids.values()):
        owned["routeFailedRecordIds"] = {
            route_key: sorted(record_ids)
            for route_key, record_ids in snapshot.route_failed_record_ids.items()
            if record_ids
        }
    if snapshot.batch_pages:
        owned["batchPages"] = {
            route_key: {
                "sourceRecordIds": list(page.source_record_ids),
                "nextCursor": page.next_cursor,
                "hasMore": page.has_more,
            }
            for route_key, page in snapshot.batch_pages.items()
        }
    if snapshot.has_more:
        owned["hasMore"] = True
    if entry.route_progress:
        owned["routeProgress"] = [dict(row) for row in entry.route_progress]
    if entry.provider_control:
        owned["providerControl"] = dict(entry.provider_control)
    execution_payload = _dump_execution(entry)
    if execution_payload is not None:
        owned["execution"] = execution_payload

    extras = entry.extras
    report: dict[str, Any] = {}
    for key in _TOP_LEVEL_KEY_ORDER:
        if key == "execution":
            if "execution" in owned:
                report[key] = owned[key]
        elif key in extras:
            report[key] = extras[key]
        elif key in owned:
            report[key] = owned[key]
    for key, value in extras.items():
        if key not in report and key != "execution":
            report[key] = value
    return report
