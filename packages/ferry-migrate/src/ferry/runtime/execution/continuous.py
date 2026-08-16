# SPDX-License-Identifier: AGPL-3.0-only
"""Continuous executor: the fenced batch loop over a frozen execution scope.

Faithful port of the production continuous-execution mechanics. One
:class:`ContinuousExecutor` claims an attempt through the
:class:`~ferry.runtime.execution.state.AttemptFence`, re-derives the frozen
scope from the claimed journal entry (refusing on any mismatch), then loops:
supersede/cancel checks before every batch, one delegated
:class:`BatchStep`, stall detection via the snapshot progress marker,
terminal-batch reconciliation against the durable ledger, frozen-total route
reopening from safe keyset checkpoints, progress/ETA/heartbeat assembly, a
fenced journal save, and a pause between batches — bounded by the batch
safety limit. A stall or a frozen-total shortfall marks the run ``failed``
for review rather than silently closing it; the durable ledger, not the
in-memory counters, is the completion authority.

Production defaults ride as constructor parameters
(``max_batches=1_000_000``, ``pause_seconds=0.25``) with an injectable
``sleep`` and ``clock`` — no ambient time or real sleeping is required to
test the loop.

Envelope copy (``summary``, ``generatedAt``, provider echoes) stays
host-side: this module writes only the mechanics keys and preserves every
unknown report key through :attr:`JournalEntry.extras` untouched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from ferry.runtime.execution.errors import ExecutionFault
from ferry.runtime.execution.model import (
    AttemptIdentity,
    ExecutionScope,
    ExecutionSnapshot,
    ExecutionStatus,
    JournalEntry,
)
from ferry.runtime.execution.report_codec import (
    canonical_route_manifest,
    execution_is_cancelled,
    parse_datetime,
)
from ferry.runtime.execution.report_codec import (
    selected_route_keys as resolve_selected_route_keys,
)
from ferry.runtime.execution.state import ExecutionHost, ExecutionJournal, ExecutionLedger

DEFAULT_MAX_BATCHES = 1_000_000
"""Production batch safety limit for one continuous job."""

DEFAULT_PAUSE_SECONDS = 0.25
"""Production pause between batches."""

STALLED_NO_PROGRESS_WARNING = (
    "Execution stopped because a batch did not advance any checkpoint. "
    "Review failed records before resuming."
)

FROZEN_TOTAL_SHORTFALL_WARNING = (
    "Execution stopped because the source page ended before the frozen "
    "route total was reached. Resume from the saved keyset checkpoint."
)

BATCH_SAFETY_LIMIT_MESSAGE = "Continuous migration stopped after the batch safety limit."

_ATTEMPT_SUPERSEDED_MESSAGE = "Ferry execution attempt was superseded."
_ATTEMPT_SUPERSEDED_CODE = "FERRY_EXECUTION_ATTEMPT_SUPERSEDED"
_JOB_SUPERSEDED_MESSAGE = "Ferry execution job was superseded."
_JOB_SUPERSEDED_CODE = "FERRY_EXECUTION_JOB_SUPERSEDED"

_FAILURE_MESSAGE_LIMIT = 500

_ROUTE_COUNT_STATUSES = ("created", "updated", "skipped", "failed")
_PROCESSED_STATUSES = ("created", "updated", "skipped")

RouteManifestRows = Sequence[Mapping[str, Any]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class BatchStep(Protocol):
    """Run one batch and persist it; return the saved state.

    In a queue-driven host this is the delegated single-batch execution
    (which saves through the journal itself, preserving the production
    two-saves-per-batch cadence); in the local engine it is ``run_batch``
    followed by ``journal.save``. Returns the saved entry and ``has_more``.
    """

    async def __call__(self) -> tuple[JournalEntry, bool]: ...


def normalized_attempt_identity(
    *,
    job_id: str,
    attempt_id: str | None = None,
    attempt_number: int = 1,
    task_run_id: str | None = None,
) -> AttemptIdentity:
    """The production attempt-identity fallback normalization."""

    return AttemptIdentity(
        attempt_id=str(attempt_id or f"{job_id}:{attempt_number}"),
        attempt_number=attempt_number,
        task_run_id=str(task_run_id or job_id),
    )


def _entry_is_cancelled(entry: JournalEntry) -> bool:
    """A cancellation is recorded on the entry status or its execution state."""

    return execution_is_cancelled(entry.status, entry.extras.get("execution"))


def _execution_extras(entry: JournalEntry) -> dict[str, Any]:
    """The mutable execution sub-extras dict, created when absent."""

    sub = entry.extras.get("execution")
    if not isinstance(sub, dict):
        sub = {}
        entry.extras["execution"] = sub
    return sub


def _own_report_key(entry: JournalEntry, key: str, *, empty: Any, is_empty: bool) -> None:
    """Take ownership of a report key the executor just wrote.

    The stale extras copy is dropped (extras win over owned values on dump);
    when the new owned value is semantically empty — which the codec would
    omit — the empty spelling rides extras so the key stays present, exactly
    as the production writer always emits it.
    """

    entry.extras.pop(key, None)
    if is_empty:
        entry.extras[key] = empty


# -- frozen-total route reopening ---------------------------------------------


def known_incomplete_route_keys(
    entry: JournalEntry,
    *,
    selected_route_keys: Sequence[str],
    route_totals: Mapping[str, int | None] | None = None,
) -> set[str]:
    """Selected routes whose terminal progress is short of the frozen total.

    With ``route_totals`` (the frozen candidate counts) a route is incomplete
    while ``created + updated + skipped`` is below its known total — unknown
    (``None``) totals never reopen. Without totals, the previously reported
    per-route progress rows are consulted (``remaining > 0``), the resume
    path's evidence.
    """

    selected = {str(route_key) for route_key in selected_route_keys}
    if route_totals is not None:
        route_counts = entry.snapshot.route_counts
        return {
            route_key
            for route_key in selected
            if route_totals.get(route_key) is not None
            and sum(
                int(route_counts.get(route_key, {}).get(status, 0))
                for status in _PROCESSED_STATUSES
            )
            < int(route_totals[route_key] or 0)
        }
    incomplete: set[str] = set()
    for row in entry.route_progress:
        route_key = str(row.get("routeKey") or "").strip()
        if route_key not in selected or row.get("remaining") is None:
            continue
        try:
            if int(row.get("remaining") or 0) > 0:
                incomplete.add(route_key)
        except (TypeError, ValueError):
            continue
    return incomplete


def reopen_incomplete_routes(
    snapshot: ExecutionSnapshot,
    *,
    route_keys: set[str],
    require_checkpoint: bool,
) -> None:
    """Reopen routes from their last safe keyset checkpoint.

    The checkpoint comes from the route's last batch page — its next cursor,
    or the last source record id the page held. A route with neither has no
    safe resume point: with ``require_checkpoint`` the reopen refuses
    (``FERRY_SOURCE_CHECKPOINT_MISSING``) before mutating anything, because
    resuming from nowhere would re-read outside the frozen scope.
    """

    if not route_keys:
        return
    reopened_checkpoints: dict[str, str] = {}
    for route_key in sorted(route_keys):
        page = snapshot.batch_pages.get(route_key)
        cursor = str(page.next_cursor or "").strip() if page is not None else ""
        if not cursor and page is not None and page.source_record_ids:
            cursor = page.source_record_ids[-1]
        if not cursor and require_checkpoint:
            raise ExecutionFault(
                "The incomplete Ferry route has no safe keyset checkpoint.",
                code="FERRY_SOURCE_CHECKPOINT_MISSING",
            )
        if cursor:
            reopened_checkpoints[route_key] = cursor
    snapshot.checkpoints.update(reopened_checkpoints)
    snapshot.completed_routes.difference_update(route_keys)
    snapshot.has_more = True


# -- durable result rebuild ---------------------------------------------------


async def durable_route_result_state(
    *,
    ledger: ExecutionLedger,
    route_keys_by_pair: Mapping[tuple[str, str], Sequence[str]],
) -> tuple[dict[str, dict[str, int]], dict[str, set[str]]]:
    """Rebuild route counts and failed-id sets from the durable ledger.

    Durable results are pair-keyed, so the rebuild is restricted to routes
    that are unique for their (source, destination) pair — a shared pair's
    rows cannot be attributed to one filtered route.
    """

    unique_routes = {
        pair: str(route_keys[0])
        for pair, route_keys in route_keys_by_pair.items()
        if len(route_keys) == 1
    }
    route_counts: dict[str, dict[str, int]] = {
        route_key: dict.fromkeys(_ROUTE_COUNT_STATUSES, 0) for route_key in unique_routes.values()
    }
    failed_record_ids: dict[str, set[str]] = {
        route_key: set() for route_key in unique_routes.values()
    }
    if not unique_routes:
        return route_counts, failed_record_ids
    for total in await ledger.pair_status_totals():
        route_key = unique_routes.get((total.source_object, total.destination_object))
        if route_key and total.status in route_counts[route_key]:
            route_counts[route_key][total.status] = int(total.count or 0)
    for failed in await ledger.failed_source_ids_by_pair():
        route_key = unique_routes.get((failed.source_object, failed.destination_object))
        if route_key:
            failed_record_ids[route_key] = {
                str(record_id) for record_id in failed.source_record_ids if str(record_id).strip()
            }
    return route_counts, failed_record_ids


async def apply_durable_route_results(
    *,
    ledger: ExecutionLedger,
    entry: JournalEntry,
    route_keys_by_pair: Mapping[tuple[str, str], Sequence[str]],
    attempt_id: str | None = None,
) -> bool:
    """Refresh the snapshot from the durable ledger when the rebuild is due.

    The production trigger discipline: a unique-pair route missing from the
    in-memory counts, or an attempt that has not rebuilt yet
    (``durable_results_attempt_id`` differs from the claimed attempt). The
    rebuild lands in the snapshot and stamps the attempt marker, so it runs
    once per attempt — the durable ledger is the authority the in-memory
    counters resynchronize from.
    """

    should_rebuild = any(
        route_key not in entry.snapshot.route_counts
        for route_keys in route_keys_by_pair.values()
        if len(route_keys) == 1
        for route_key in route_keys
    ) or (attempt_id is not None and entry.durable_results_attempt_id != attempt_id)
    if not should_rebuild:
        return False
    durable_counts, durable_failed_ids = await durable_route_result_state(
        ledger=ledger,
        route_keys_by_pair=route_keys_by_pair,
    )
    entry.snapshot.route_counts.update(durable_counts)
    entry.snapshot.route_failed_record_ids.update(durable_failed_ids)
    if attempt_id is not None:
        entry.durable_results_attempt_id = attempt_id
    return True


# -- terminal-batch reconciliation --------------------------------------------


async def reconcile_terminal_batch_pages(
    *,
    ledger: ExecutionLedger,
    entry: JournalEntry,
    route_manifest: RouteManifestRows,
    selected_route_keys: Sequence[str],
) -> bool:
    """Advance a stalled batch whose pages the durable ledger proves terminal.

    Every selected route's last batch page must consist entirely of records
    with terminal durable results, and no checked route may hold failed
    durable rows — otherwise nothing is touched and the stall stands for
    review. On success the checkpoints advance (or complete) from the page
    evidence, the unique-pair route counts and failed sets rebuild from the
    ledger, and the aggregate counts refresh from durable state.
    """

    snapshot = entry.snapshot
    if not snapshot.batch_pages:
        return False
    selected = {str(route_key) for route_key in selected_route_keys}
    manifest_by_key = {
        str(row["routeKey"]): row for row in canonical_route_manifest(list(route_manifest))
    }
    checked_route_keys: list[str] = []
    for route_key, page in snapshot.batch_pages.items():
        if route_key not in selected:
            continue
        manifest_row = manifest_by_key.get(route_key)
        source_record_ids = [
            record_id for record_id in page.source_record_ids if str(record_id).strip()
        ]
        if manifest_row is None or not source_record_ids:
            return False
        terminal = await ledger.terminal_destination_ids(
            source_object=str(manifest_row["sourceObject"]),
            source_record_ids=source_record_ids,
            destination_object=str(manifest_row["destinationObject"]),
        )
        if set(terminal) != set(source_record_ids):
            return False
        checked_route_keys.append(route_key)
    if not checked_route_keys:
        return False

    route_keys_by_pair: dict[tuple[str, str], list[str]] = {}
    for route_key in selected:
        row = manifest_by_key.get(route_key)
        if row is None:
            continue
        route_keys_by_pair.setdefault(
            (str(row["sourceObject"]), str(row["destinationObject"])),
            [],
        ).append(route_key)
    durable_counts, durable_failed_ids = await durable_route_result_state(
        ledger=ledger,
        route_keys_by_pair=route_keys_by_pair,
    )
    if any(durable_failed_ids.get(route_key) for route_key in checked_route_keys):
        return False

    checkpoints = dict(snapshot.checkpoints)
    completed_routes = set(snapshot.completed_routes)
    for route_key in checked_route_keys:
        page = snapshot.batch_pages[route_key]
        if page.has_more:
            next_cursor = str(page.next_cursor or "").strip()
            if not next_cursor:
                return False
            checkpoints[route_key] = next_cursor
        else:
            checkpoints.pop(route_key, None)
            completed_routes.add(route_key)

    snapshot.checkpoints = checkpoints
    snapshot.completed_routes = completed_routes
    snapshot.route_counts.update(durable_counts)
    snapshot.route_failed_record_ids.update(durable_failed_ids)
    entry.aggregate_counts = await ledger.status_totals()
    snapshot.has_more = any(
        snapshot.batch_pages[route_key].has_more for route_key in checked_route_keys
    )
    _own_report_key(entry, "counts", empty={}, is_empty=not entry.aggregate_counts)
    _own_report_key(entry, "checkpoints", empty={}, is_empty=not snapshot.checkpoints)
    _own_report_key(entry, "completedRoutes", empty=[], is_empty=not snapshot.completed_routes)
    _own_report_key(entry, "routeCounts", empty={}, is_empty=not snapshot.route_counts)
    _own_report_key(
        entry,
        "routeFailedRecordIds",
        empty={},
        is_empty=not any(snapshot.route_failed_record_ids.values()),
    )
    _own_report_key(entry, "hasMore", empty=False, is_empty=not snapshot.has_more)
    return True


# -- progress / heartbeat assembly --------------------------------------------


async def assemble_continuous_report(
    *,
    ledger: ExecutionLedger,
    entry: JournalEntry,
    state: ExecutionStatus,
    scope: ExecutionScope,
    job_id: str | None,
    started_at: str,
    batches_completed: int,
    batch_size: int | None,
    resumed: bool,
    stalled: bool,
    now: datetime,
    stall_warning: str | None = None,
    provider_control: Callable[[], dict[str, Any] | None] | None = None,
) -> None:
    """Assemble the heartbeat mechanics onto ``entry`` in place.

    Per-route processed/remaining/percent/ETA rows, the overall progress
    block, stall warnings, the execution bookkeeping keys, and
    ``lastHeartbeatAt`` — the production heartbeat mechanics. Routes missing
    from the in-memory counts fall back to the durable pair summaries when
    they are unique for their pair. Envelope copy (``summary``,
    ``generatedAt``) is deliberately not written here — it stays host-side;
    ``providerControl`` is written only when the host supplies a provider.
    The persisted scope keeps whatever route totals the entry already
    carried: the frozen totals used for reconciliation are claim-time state,
    not part of the persisted report contract.
    """

    snapshot = entry.snapshot
    manifest = canonical_route_manifest([dict(row) for row in scope.route_manifest])
    selected = set(resolve_selected_route_keys(manifest, list(scope.selected_route_keys)))
    routes = [row for row in manifest if str(row["routeKey"]) in selected]
    route_totals = scope.route_totals

    persisted_route_counts = {
        str(route_key): {
            status: max(0, int(counts.get(status) or 0)) for status in _ROUTE_COUNT_STATUSES
        }
        for route_key, counts in snapshot.route_counts.items()
    }
    pending_record_ids = snapshot.route_pending_record_ids
    route_pair_counts: dict[tuple[str, str], int] = {}
    for row in routes:
        pair = (str(row["sourceObject"]), str(row["destinationObject"]))
        route_pair_counts[pair] = route_pair_counts.get(pair, 0) + 1
    missing_unique_pairs = {
        (str(row["sourceObject"]), str(row["destinationObject"]))
        for row in routes
        if str(row["routeKey"]) not in persisted_route_counts
        if route_pair_counts[(str(row["sourceObject"]), str(row["destinationObject"]))] == 1
    }
    status_by_pair: dict[tuple[str, str], dict[str, int]] = {}
    if missing_unique_pairs:
        for pair_total in await ledger.pair_status_totals():
            summary_pair = (pair_total.source_object, pair_total.destination_object)
            if summary_pair in missing_unique_pairs and pair_total.status:
                status_by_pair.setdefault(summary_pair, {})[pair_total.status] = int(
                    pair_total.count or 0
                )

    start = parse_datetime(started_at) or now
    elapsed_seconds = max(0.001, (now - start).total_seconds())
    progress: list[dict[str, Any]] = []
    known_remaining = 0
    all_totals_known = True
    total_processed = 0
    total_records = 0
    completed_routes = snapshot.completed_routes
    for row in routes:
        source_object = str(row["sourceObject"])
        destination_object = str(row["destinationObject"])
        route_key = str(row["routeKey"])
        pair = (source_object, destination_object)
        route_counts = persisted_route_counts.get(route_key)
        if route_counts is None:
            route_counts = status_by_pair.get(pair, {}) if route_pair_counts[pair] == 1 else {}
        route_pending = pending_record_ids.get(route_key, set())
        processed = sum(int(route_counts.get(status, 0)) for status in _PROCESSED_STATUSES)
        failed = int(route_counts.get("failed", 0))
        total = route_totals.get(route_key)
        remaining = max(0, total - processed) if total is not None else None
        route_completed = route_key in completed_routes or remaining == 0
        rate = processed / elapsed_seconds
        eta_seconds = int(remaining / rate) if remaining is not None and rate > 0 else None
        if total is None:
            all_totals_known = False
        else:
            total_records += total
            known_remaining += int(remaining or 0)
        total_processed += processed
        progress.append(
            {
                "routeKey": route_key,
                "sourceObject": source_object,
                "sourceFilter": (
                    dict(row["sourceFilter"]) if row.get("sourceFilter") is not None else None
                ),
                "destinationObject": destination_object,
                "processed": processed,
                "created": int(route_counts.get("created", 0)),
                "updated": int(route_counts.get("updated", 0)),
                "skipped": int(route_counts.get("skipped", 0)),
                "failed": failed,
                "associationPending": len(route_pending),
                "total": total,
                "remaining": remaining,
                "percent": (
                    min(100, round(processed * 100 / total, 1))
                    if total not in {None, 0}
                    else (100 if total == 0 else None)
                ),
                "etaSeconds": eta_seconds,
                "checkpoint": snapshot.checkpoints.get(route_key),
                "status": (
                    "failed" if failed and stalled else ("completed" if route_completed else state)
                ),
            }
        )
    overall_rate = total_processed / elapsed_seconds
    overall_remaining = known_remaining if all_totals_known else None
    overall_eta = (
        int(overall_remaining / overall_rate)
        if overall_remaining is not None and overall_rate > 0
        else None
    )

    warnings = list(snapshot.warnings)
    if stalled:
        warning = stall_warning or STALLED_NO_PROGRESS_WARNING
        if warning not in warnings:
            warnings.append(warning)
    snapshot.warnings = warnings
    _own_report_key(entry, "warnings", empty=[], is_empty=not warnings)

    entry.status = state
    entry.extras.pop("status", None)
    entry.route_progress = progress
    _own_report_key(entry, "routeProgress", empty=[], is_empty=not progress)
    entry.extras["progress"] = {
        "processed": total_processed,
        "total": total_records if all_totals_known else None,
        "remaining": overall_remaining,
        "percent": (
            min(100, round(total_processed * 100 / total_records, 1))
            if all_totals_known and total_records > 0
            else (100 if all_totals_known and total_records == 0 else None)
        ),
        "etaSeconds": overall_eta,
    }
    if provider_control is not None:
        control = provider_control()
        entry.provider_control = dict(control) if control else None
        _own_report_key(entry, "providerControl", empty=control, is_empty=not control)

    persisted_route_totals = dict(entry.scope.route_totals) if entry.scope is not None else {}
    entry.scope = ExecutionScope(
        route_manifest=tuple(manifest),
        selected_route_keys=tuple(str(route_key) for route_key in scope.selected_route_keys),
        route_high_water_marks=dict(scope.route_high_water_marks),
        route_totals=persisted_route_totals,
    )
    if job_id is not None:
        entry.job_id = job_id
    if batch_size is not None:
        entry.batch_size = batch_size
    entry.batches_completed = batches_completed
    entry.started_at = started_at
    entry.last_heartbeat_at = now.isoformat()
    entry.resumed = resumed
    execution_extras = _execution_extras(entry)
    execution_extras["mode"] = "continuous"
    execution_extras["state"] = state
    for stale_key in (
        "jobId",
        "batchSize",
        "batchesCompleted",
        "startedAt",
        "lastHeartbeatAt",
        "routeManifest",
        "selectedRouteKeys",
        "routeHighWaterMarks",
        "routeTotals",
        "resumed",
    ):
        execution_extras.pop(stale_key, None)
    if not resumed:
        execution_extras["resumed"] = False
    if entry.batch_size <= 0:
        execution_extras["batchSize"] = entry.batch_size


# -- failure marking ----------------------------------------------------------


async def mark_execution_failed(
    *,
    journal: ExecutionJournal,
    message: str,
    job_id: str | None = None,
    attempt_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> JournalEntry | None:
    """Mark the journal entry ``failed`` for review; fenced, never clobbering.

    The current entry (or a fresh one when nothing was persisted yet) gains
    the failure message as a warning, the ``failed`` status, and a heartbeat.
    A journal owned by another job is left untouched, and the fenced save
    means an older attempt can never overwrite a newer attempt's state — the
    entry is returned only when the failure marking was actually persisted.
    """

    now = (clock or _utc_now)().isoformat()
    try:
        entry = await journal.load()
    except ExecutionFault:
        return None
    if entry is None:
        entry = JournalEntry(
            status="failed",
            snapshot=ExecutionSnapshot(),
            job_id=job_id,
            attempt_id=attempt_id,
            batch_size=0,
            batches_completed=0,
            started_at=None,
            last_heartbeat_at=None,
            resumed=False,
            scope=None,
            durable_results_attempt_id=None,
        )
    if job_id is not None and entry.job_id not in (None, job_id):
        return None
    warnings = list(entry.snapshot.warnings)
    if message and message not in warnings:
        warnings.append(message)
    entry.snapshot.warnings = warnings
    _own_report_key(entry, "warnings", empty=[], is_empty=not warnings)
    entry.status = "failed"
    entry.extras.pop("status", None)
    if job_id is not None:
        entry.job_id = job_id
    if attempt_id is not None:
        entry.attempt_id = attempt_id
    entry.last_heartbeat_at = now
    execution_extras = _execution_extras(entry)
    execution_extras["mode"] = "continuous"
    execution_extras["state"] = "failed"
    execution_extras.pop("jobId", None)
    execution_extras.pop("lastHeartbeatAt", None)
    outcome = await journal.save(entry)
    return entry if outcome == "saved" else None


# -- the continuous executor --------------------------------------------------


def _validated_scope(scope: ExecutionScope, entry: JournalEntry | None) -> ExecutionScope:
    """The claimed entry's saved scope must still be the scope being run.

    A queued entry without a manifest cannot execute continuously; a manifest,
    selection, or high-water-mark mismatch refuses — never resume superseded
    work against a different frozen scope. The caller's scope wins for route
    totals (claim-time counts); everything else is byte-compared.
    """

    saved = entry.scope if entry is not None else None
    if entry is not None and saved is None:
        raise ExecutionFault(
            "A complete Ferry route manifest is required before continuous execution.",
            code="FERRY_ROUTE_MANIFEST_MISSING",
        )
    if saved is None:
        return scope
    current_manifest = canonical_route_manifest([dict(row) for row in scope.route_manifest])
    saved_manifest = canonical_route_manifest([dict(row) for row in saved.route_manifest])
    if current_manifest != saved_manifest:
        raise ExecutionFault(
            "The saved Ferry mapping route manifest does not match the queued job.",
            code="FERRY_ROUTE_MANIFEST_CHANGED",
        )
    if list(scope.selected_route_keys) != list(saved.selected_route_keys):
        raise ExecutionFault(
            "The Ferry execution route selection changed after the job was queued.",
            code="FERRY_EXECUTION_ROUTE_CHANGED",
        )
    if dict(scope.route_high_water_marks) != dict(saved.route_high_water_marks):
        raise ExecutionFault(
            "The saved Ferry route high-water marks do not match the reviewed routes.",
            code="FERRY_EXECUTION_HIGH_WATER_MARK_INVALID",
        )
    return scope


class ContinuousExecutor:
    """The fenced continuous batch loop over one frozen execution scope.

    Construction pins the production defaults; ``sleep`` and ``clock`` are
    injectable so the loop is testable without real time. ``provider_control``
    optionally supplies the destination's retry-metrics block for the
    heartbeat report (host concern; the local runtime passes nothing).
    """

    def __init__(
        self,
        *,
        host: ExecutionHost,
        max_batches: int = DEFAULT_MAX_BATCHES,
        pause_seconds: float = DEFAULT_PAUSE_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
        provider_control: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self._host = host
        self._max_batches = int(max_batches)
        self._pause_seconds = float(pause_seconds)
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._clock = clock if clock is not None else _utc_now
        self._provider_control = provider_control

    async def run(
        self,
        *,
        scope: ExecutionScope,
        attempt: AttemptIdentity,
        step: BatchStep,
        job_id: str | None = None,
        batch_size: int | None = None,
    ) -> JournalEntry:
        """Claim the attempt, then loop batches to a terminal journal entry.

        Returns the terminal entry: ``completed`` when every selected route
        reconciles against its frozen total, ``failed`` (for review) on a
        stall, a frozen-total shortfall, or the batch safety limit, and
        ``cancelled`` when a cancellation wins a race. Supersession — of the
        job or of this attempt — raises :class:`ExecutionFault` after a
        fenced failure mark that can never clobber the newer owner's state.
        """

        journal = self._host.journal
        current = await journal.load()
        if current is not None:
            if _entry_is_cancelled(current):
                return current
            if current.status == "completed":
                return current
        claim_outcome, claimed = await self._host.fence.claim(attempt)
        if claim_outcome == "cancelled":
            resolved = claimed if claimed is not None else current
            if resolved is not None:
                return resolved
            raise ExecutionFault(_ATTEMPT_SUPERSEDED_MESSAGE, code=_ATTEMPT_SUPERSEDED_CODE)
        if claim_outcome == "superseded":
            self._host.observer.attempt_fenced(attempt_id=attempt.attempt_id)
            raise ExecutionFault(_ATTEMPT_SUPERSEDED_MESSAGE, code=_ATTEMPT_SUPERSEDED_CODE)
        entry = claimed if claimed is not None else current
        try:
            effective_scope = _validated_scope(scope, entry)
        except Exception as fault:
            await mark_execution_failed(
                journal=journal,
                message=str(fault)[:_FAILURE_MESSAGE_LIMIT],
                job_id=job_id,
                attempt_id=attempt.attempt_id,
                clock=self._clock,
            )
            raise
        manifest = [dict(row) for row in effective_scope.route_manifest]
        selected = list(effective_scope.selected_route_keys)
        route_totals = dict(effective_scope.route_totals)
        started_at = (entry.started_at if entry is not None else None) or self._clock().isoformat()
        batches_completed = entry.batches_completed if entry is not None else 0
        resumed = entry.resumed if entry is not None else False
        previous_progress = (
            entry.snapshot.progress_marker()
            if entry is not None
            else ExecutionSnapshot().progress_marker()
        )

        try:
            while batches_completed < self._max_batches:
                latest = await journal.load()
                if latest is not None:
                    if _entry_is_cancelled(latest):
                        return latest
                    if latest.attempt_id != attempt.attempt_id:
                        self._host.observer.attempt_fenced(attempt_id=attempt.attempt_id)
                        raise ExecutionFault(
                            _ATTEMPT_SUPERSEDED_MESSAGE,
                            code=_ATTEMPT_SUPERSEDED_CODE,
                        )
                batch_entry, batch_has_more = await step()
                if batch_entry.status == "cancelled":
                    return batch_entry
                batches_completed += 1
                progress_marker = batch_entry.snapshot.progress_marker()
                stalled = batch_has_more and progress_marker == previous_progress
                if stalled:
                    reconciled = await reconcile_terminal_batch_pages(
                        ledger=self._host.ledger,
                        entry=batch_entry,
                        route_manifest=manifest,
                        selected_route_keys=selected,
                    )
                    if reconciled:
                        batch_has_more = batch_entry.snapshot.has_more
                        progress_marker = batch_entry.snapshot.progress_marker()
                        stalled = batch_has_more and progress_marker == previous_progress
                stall_warning: str | None = None
                incomplete_routes = (
                    known_incomplete_route_keys(
                        batch_entry,
                        selected_route_keys=selected,
                        route_totals=route_totals,
                    )
                    if not batch_has_more
                    else set()
                )
                if incomplete_routes:
                    reopen_incomplete_routes(
                        batch_entry.snapshot,
                        route_keys=incomplete_routes,
                        require_checkpoint=True,
                    )
                    batch_has_more = True
                    stalled = True
                    stall_warning = FROZEN_TOTAL_SHORTFALL_WARNING
                state: ExecutionStatus = (
                    "failed" if stalled else ("running" if batch_has_more else "completed")
                )
                await assemble_continuous_report(
                    ledger=self._host.ledger,
                    entry=batch_entry,
                    state=state,
                    scope=effective_scope,
                    job_id=job_id,
                    started_at=started_at,
                    batches_completed=batches_completed,
                    batch_size=batch_size,
                    resumed=resumed,
                    stalled=stalled,
                    now=self._clock(),
                    stall_warning=stall_warning,
                    provider_control=self._provider_control,
                )
                save_outcome = await journal.save(batch_entry)
                if save_outcome == "cancelled":
                    batch_entry.status = "cancelled"
                    return batch_entry
                if save_outcome == "superseded":
                    raise ExecutionFault(_JOB_SUPERSEDED_MESSAGE, code=_JOB_SUPERSEDED_CODE)
                self._host.observer.batch_completed(
                    batches_completed=batches_completed,
                    has_more=batch_has_more,
                    stalled=stalled,
                )
                if stalled or not batch_has_more:
                    return batch_entry
                previous_progress = progress_marker
                await self._sleep(self._pause_seconds)
        except Exception as fault:
            try:
                latest = await journal.load()
            except ExecutionFault:
                latest = None
            if latest is not None and _entry_is_cancelled(latest):
                return latest
            await mark_execution_failed(
                journal=journal,
                message=str(fault)[:_FAILURE_MESSAGE_LIMIT],
                job_id=job_id,
                attempt_id=attempt.attempt_id,
                clock=self._clock,
            )
            raise

        marked = await mark_execution_failed(
            journal=journal,
            message=BATCH_SAFETY_LIMIT_MESSAGE,
            job_id=job_id,
            attempt_id=attempt.attempt_id,
            clock=self._clock,
        )
        if marked is not None:
            return marked
        final = await journal.load()
        if final is not None:
            return final
        raise ExecutionFault(_ATTEMPT_SUPERSEDED_MESSAGE, code=_ATTEMPT_SUPERSEDED_CODE)
