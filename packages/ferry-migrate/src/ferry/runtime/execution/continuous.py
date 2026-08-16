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

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from ferry.runtime.execution.errors import ExecutionFault
from ferry.runtime.execution.model import (
    AttemptIdentity,
    ExecutionSnapshot,
    JournalEntry,
)
from ferry.runtime.execution.report_codec import (
    canonical_route_manifest,
    execution_is_cancelled,
)
from ferry.runtime.execution.state import ExecutionLedger

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
_JOB_SUPERSEDED_MESSAGE = "Ferry execution job was superseded."

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
