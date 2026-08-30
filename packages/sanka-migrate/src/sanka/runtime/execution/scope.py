# SPDX-License-Identifier: AGPL-3.0-only
"""Scope freeze: per-route high-water marks, frozen totals, route resolution.

Faithful port of the production scope mechanics: the queue-time high-water
mark freeze probes :class:`~sanka_connector.SupportsHighWaterMark` and
freezes a mark for **every** reviewed route (not only the selected ones); the
claim-time total count probes :class:`~sanka_connector.SupportsRecordCounts`
and counts **selected** routes only, bounded by the frozen marks through
:class:`~sanka_connector.SupportsBoundedCounts`. A route whose frozen mark is
``None`` (the source held no records at freeze time) counts as ``0`` and is
completed without a read. Sources that cannot count report ``None`` totals —
unknown, never zero.

Scope validation against a saved manifest reuses the codec contracts
(:func:`~sanka.runtime.execution.report_codec.require_matching_route_manifest`
and friends); :func:`freeze_scope` composes manifest, selection, marks, and
totals into one :class:`~sanka.runtime.execution.model.ExecutionScope`, whose
``scope_hash`` is the stable handle approvals bind to.

:class:`ExactIdScope` is the tagged variant for the safety runbook's exact-ID
pilot sets: instead of a frozen high-water mark, the scope enumerates the
exact candidate ids per route (and their canonical ``candidate_hash``), and
``run_batch`` intersects every source page with that set, refusing route
completion until the execution ledger covers every candidate id.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sanka.runtime.execution.errors import ExecutionFault
from sanka.runtime.execution.model import ExecutionRoute, ExecutionScope
from sanka.runtime.execution.report_codec import (
    canonical_route_manifest,
    execution_route_high_water_marks,
    filter_mapping_groups,
    require_matching_route_manifest,
    selected_route_keys,
)
from sanka.runtime.execution.state import NULL_OBSERVER, ExecutionObserver
from sanka.runtime.hashing import content_hash
from sanka.runtime.mapping.record_mapping import (
    MappingGroup,
    mapping_group_key,
    mapping_route_manifest,
)
from sanka_connector import (
    Credentials,
    SourceConnector,
    SupportsBoundedCounts,
    SupportsHighWaterMark,
    SupportsRecordCounts,
)

EXACT_CANDIDATE_HASH_MISMATCH_CODE = "SANKA_MIGRATE_EXACT_SCOPE_CANDIDATE_HASH_MISMATCH"
"""Refusal code when an exact candidate set no longer matches its hash.

Minted by the open runtime: the workspace safety runbook's exact-ID pilot
("an exact saved ID set and its hash") is operator procedure in production,
so there is no production code to preserve. Deliberately absent from
:data:`~sanka.runtime.execution.errors.EXECUTION_FAULT_CODES`, which pins the
production taxonomy verbatim.
"""


def execution_routes(
    groups: list[MappingGroup],
    selected_route_keys: Sequence[str] | None = None,
) -> list[ExecutionRoute]:
    """Executable routes for the given mapping groups, in group order.

    When ``selected_route_keys`` is provided, only groups whose route key is
    in the selection are returned (still in group order), exactly as the
    production route filter behaves.
    """

    if selected_route_keys is not None:
        groups = filter_mapping_groups(groups, list(selected_route_keys))
    return [
        ExecutionRoute(
            route_key=mapping_group_key(source_object, destination_object, source_filter),
            source_object=source_object,
            destination_object=destination_object,
            source_filter=source_filter,
            fields=fields,
        )
        for source_object, destination_object, source_filter, fields in groups
    ]


async def freeze_route_high_water_marks(
    *,
    source: SourceConnector,
    source_credentials: Credentials,
    routes: Sequence[ExecutionRoute],
) -> dict[str, str | None]:
    """Freeze one high-water mark per route; ``{}`` when unsupported.

    Production freezes marks for every reviewed route at queue time so the
    whole run is bound to one candidate set; a source without
    :class:`SupportsHighWaterMark` yields an empty map (unbounded reads).
    """

    if not isinstance(source, SupportsHighWaterMark):
        return {}
    high_water_marks: dict[str, str | None] = {}
    for route in routes:
        high_water_marks[route.route_key] = await source.high_water_mark(
            source_credentials,
            object_type=route.source_object,
            source_filter=route.source_filter,
        )
    return high_water_marks


async def frozen_route_totals(
    *,
    source: SourceConnector,
    source_credentials: Credentials,
    routes: Sequence[ExecutionRoute],
    route_high_water_marks: Mapping[str, str | None],
    observer: ExecutionObserver = NULL_OBSERVER,
) -> dict[str, int | None]:
    """Count the frozen candidate set per selected route.

    ``None`` totals mean the source cannot count (reconciliation treats them
    as unknown, never zero). A frozen ``None`` mark counts as ``0``. A frozen
    mark on a source without :class:`SupportsBoundedCounts` fails with
    ``SANKA_MIGRATE_SOURCE_HIGH_WATER_MARK_UNSUPPORTED`` — counting outside the
    frozen set would break the reconciliation identity. Any other counting
    error reports the route to the observer and yields ``None``.
    """

    if not isinstance(source, SupportsRecordCounts):
        return {route.route_key: None for route in routes}
    totals: dict[str, int | None] = {}
    for route in routes:
        route_key = route.route_key
        if route_key in route_high_water_marks and route_high_water_marks[route_key] is None:
            totals[route_key] = 0
            continue
        try:
            upper_bound = route_high_water_marks.get(route_key)
            if upper_bound is not None:
                if not isinstance(source, SupportsBoundedCounts):
                    raise ExecutionFault(
                        "Source provider cannot count the frozen Sanka record set.",
                        code="SANKA_MIGRATE_SOURCE_HIGH_WATER_MARK_UNSUPPORTED",
                    )
                count = await source.count_records_bounded(
                    source_credentials,
                    object_type=route.source_object,
                    source_filter=route.source_filter,
                    upper_bound=upper_bound,
                )
            else:
                count = await source.count_records(
                    source_credentials,
                    object_type=route.source_object,
                    source_filter=route.source_filter,
                )
            totals[route_key] = max(0, int(count))
        except ExecutionFault:
            raise
        except Exception:  # a failed count degrades to unknown, never to zero
            observer.route_count_failed(route_key=route_key)
            totals[route_key] = None
    return totals


async def freeze_scope(
    *,
    groups: list[MappingGroup],
    source: SourceConnector,
    source_credentials: Credentials,
    requested_route_keys: Any | None = None,
    expected_route_manifest: Any | None = None,
    route_high_water_marks: Mapping[str, str | None] | None = None,
    include_route_totals: bool = True,
    observer: ExecutionObserver = NULL_OBSERVER,
) -> ExecutionScope:
    """Freeze the approved scope one execution is bound to.

    The manifest is canonicalized from the current mapping groups; when
    ``expected_route_manifest`` is provided (the saved manifest a queued job
    was approved against) the groups must still describe it —
    ``SANKA_MIGRATE_ROUTE_MANIFEST_CHANGED`` otherwise. A provided
    ``route_high_water_marks`` map (saved marks from a previous freeze) is
    validated against the manifest and reused; an absent or empty one is
    frozen fresh from the source, for every manifest route. Totals cover the
    selected routes only.

    Production freezes marks at queue time and counts totals at claim time;
    a queue-time freeze passes ``include_route_totals=False`` and completes
    the scope later via :func:`frozen_route_totals`. The default freezes the
    complete scope — a scope without totals cannot reconcile.
    """

    route_manifest = (
        require_matching_route_manifest(groups, expected_route_manifest)
        if expected_route_manifest is not None
        else canonical_route_manifest(mapping_route_manifest(groups))
    )
    selected = selected_route_keys(route_manifest, requested_route_keys)
    saved_high_water_marks = (
        execution_route_high_water_marks(
            {"routeHighWaterMarks": dict(route_high_water_marks)},
            route_manifest,
        )
        if route_high_water_marks is not None
        else {}
    )
    high_water_marks = saved_high_water_marks or await freeze_route_high_water_marks(
        source=source,
        source_credentials=source_credentials,
        routes=execution_routes(groups),
    )
    route_totals: dict[str, int | None] = {}
    if include_route_totals:
        route_totals = await frozen_route_totals(
            source=source,
            source_credentials=source_credentials,
            routes=execution_routes(groups, selected),
            route_high_water_marks=high_water_marks,
            observer=observer,
        )
    return ExecutionScope(
        route_manifest=tuple(route_manifest),
        selected_route_keys=tuple(selected),
        route_high_water_marks=high_water_marks,
        route_totals=route_totals,
    )


def exact_candidate_hash(candidate_ids_by_route: Mapping[str, Iterable[str]]) -> str:
    """Canonical content hash of an exact candidate-id set.

    Ids are string-coerced, stripped, deduplicated, and sorted per route, and
    route keys sort inside the canonical JSON form, so the same logical
    candidate set always yields the same hash regardless of construction
    order — the runbook's saved-ID-set hash, the stable handle an approval
    binds to.
    """

    return content_hash(
        {
            "candidateIdsByRoute": {
                str(route_key): sorted(
                    {
                        str(candidate_id).strip()
                        for candidate_id in candidate_ids
                        if str(candidate_id).strip()
                    }
                )
                for route_key, candidate_ids in candidate_ids_by_route.items()
            }
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExactIdScope(ExecutionScope):
    """The safety runbook's exact-ID pilot scope — a tagged scope variant.

    ``candidate_ids_by_route`` enumerates, per selected route, exactly the
    source record ids the execution may touch (canonical deduplicated sorted
    tuples); ``candidate_hash`` is their canonical content hash — the
    read-back gate an approval pins, verified again before any batch runs.
    ``route_totals`` carries ``len(candidate_ids)`` per route, so anything
    reconciling against ``scope.route_totals`` checks the runbook identity
    (created + updated + skipped + failed = candidate count) against the
    exact candidate count with no extra wiring. High-water marks stay empty:
    the candidate set itself is the bound.

    Under an exact scope, :func:`~sanka.runtime.execution.routes.run_batch`
    intersects every source page with the candidate set and refuses to
    complete a route until the execution ledger holds a terminal result for
    every candidate id.
    """

    candidate_ids_by_route: Mapping[str, tuple[str, ...]]
    candidate_hash: str

    @property
    def scope_hash(self) -> str:
        """Approval hash covering both the route envelope and exact candidates."""
        return content_hash(
            {
                "routeManifest": list(self.route_manifest),
                "selectedRouteKeys": list(self.selected_route_keys),
                "candidateHash": self.candidate_hash,
            }
        )

    def verify_candidate_hash(self) -> None:
        """Refuse when the candidate set drifted from its approved hash."""

        computed = exact_candidate_hash(self.candidate_ids_by_route)
        if computed != self.candidate_hash:
            raise ExecutionFault(
                "The exact-ID candidate set does not match its approved candidate hash.",
                code=EXACT_CANDIDATE_HASH_MISMATCH_CODE,
                details={
                    "candidateHash": self.candidate_hash,
                    "computedCandidateHash": computed,
                },
            )


def exact_id_scope(
    *,
    groups: list[MappingGroup],
    candidate_ids_by_route: Mapping[str, Iterable[str]],
    requested_route_keys: Any | None = None,
    expected_route_manifest: Any | None = None,
    expected_candidate_hash: str | None = None,
) -> ExactIdScope:
    """Freeze the exact-ID pilot scope one execution is bound to.

    The manifest comes from the current mapping groups, validated against
    ``expected_route_manifest`` when a saved one is provided — exactly as
    :func:`freeze_scope` validates it. The selection defaults to the
    candidate routes and must name exactly them: a selected route without
    candidate ids, or candidate ids for an unselected route, refuses with
    ``SANKA_MIGRATE_EXECUTION_ROUTE_INVALID`` — an exact scope enumerates everything
    it may touch, nothing less and nothing more. Candidate ids are
    canonicalized (string-coerced, stripped, deduplicated, sorted; an empty
    set is legal and completes its route without a read), and the canonical
    hash must equal ``expected_candidate_hash`` when the caller reads one
    back from an approval record.
    """

    route_manifest = (
        require_matching_route_manifest(groups, expected_route_manifest)
        if expected_route_manifest is not None
        else canonical_route_manifest(mapping_route_manifest(groups))
    )
    candidates: dict[str, tuple[str, ...]] = {}
    for route_key, candidate_ids in candidate_ids_by_route.items():
        if isinstance(candidate_ids, str | bytes):
            raise ExecutionFault(
                "Exact-ID candidates must be a collection of ids, not a single string.",
                code="SANKA_MIGRATE_EXECUTION_ROUTE_INVALID",
                details={"routeKey": str(route_key)},
            )
        candidates[str(route_key)] = tuple(
            sorted(
                {
                    str(candidate_id).strip()
                    for candidate_id in candidate_ids
                    if str(candidate_id).strip()
                }
            )
        )
    if not candidates:
        raise ExecutionFault(
            "An exact-ID scope requires at least one candidate route.",
            code="SANKA_MIGRATE_EXECUTION_ROUTE_INVALID",
        )
    selected = selected_route_keys(
        route_manifest,
        list(candidates) if requested_route_keys is None else requested_route_keys,
    )
    routes_without_candidates = [route_key for route_key in selected if route_key not in candidates]
    unselected_candidates = sorted(set(candidates).difference(selected))
    if routes_without_candidates or unselected_candidates:
        details: dict[str, Any] = {}
        if routes_without_candidates:
            details["routeKeysWithoutCandidates"] = routes_without_candidates
        if unselected_candidates:
            details["unselectedCandidateRouteKeys"] = unselected_candidates
        raise ExecutionFault(
            "An exact-ID scope must enumerate candidate ids for exactly its selected routes.",
            code="SANKA_MIGRATE_EXECUTION_ROUTE_INVALID",
            details=details,
        )
    candidate_hash = exact_candidate_hash(candidates)
    if expected_candidate_hash is not None and expected_candidate_hash != candidate_hash:
        raise ExecutionFault(
            "The exact-ID candidate set does not match its approved candidate hash.",
            code=EXACT_CANDIDATE_HASH_MISMATCH_CODE,
            details={
                "expectedCandidateHash": expected_candidate_hash,
                "candidateHash": candidate_hash,
            },
        )
    return ExactIdScope(
        route_manifest=tuple(route_manifest),
        selected_route_keys=tuple(selected),
        route_high_water_marks={},
        route_totals={route_key: len(candidates[route_key]) for route_key in selected},
        candidate_ids_by_route=candidates,
        candidate_hash=candidate_hash,
    )
