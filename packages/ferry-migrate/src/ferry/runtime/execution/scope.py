# SPDX-License-Identifier: AGPL-3.0-only
"""Scope freeze: per-route high-water marks, frozen totals, route resolution.

Faithful port of the production scope mechanics: the queue-time high-water
mark freeze probes :class:`~ferry.connector.SupportsHighWaterMark` and
freezes a mark for **every** reviewed route (not only the selected ones); the
claim-time total count probes :class:`~ferry.connector.SupportsRecordCounts`
and counts **selected** routes only, bounded by the frozen marks through
:class:`~ferry.connector.SupportsBoundedCounts`. A route whose frozen mark is
``None`` (the source held no records at freeze time) counts as ``0`` and is
completed without a read. Sources that cannot count report ``None`` totals —
unknown, never zero.

Scope validation against a saved manifest reuses the codec contracts
(:func:`~ferry.runtime.execution.report_codec.require_matching_route_manifest`
and friends); :func:`freeze_scope` composes manifest, selection, marks, and
totals into one :class:`~ferry.runtime.execution.model.ExecutionScope`, whose
``scope_hash`` is the stable handle approvals bind to.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ferry.connector import (
    Credentials,
    SourceConnector,
    SupportsBoundedCounts,
    SupportsHighWaterMark,
    SupportsRecordCounts,
)
from ferry.runtime.execution.errors import ExecutionFault
from ferry.runtime.execution.model import ExecutionRoute, ExecutionScope
from ferry.runtime.execution.report_codec import (
    canonical_route_manifest,
    execution_route_high_water_marks,
    filter_mapping_groups,
    require_matching_route_manifest,
    selected_route_keys,
)
from ferry.runtime.execution.state import NULL_OBSERVER, ExecutionObserver
from ferry.runtime.mapping.record_mapping import (
    MappingGroup,
    mapping_group_key,
    mapping_route_manifest,
)


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
    ``FERRY_SOURCE_HIGH_WATER_MARK_UNSUPPORTED`` — counting outside the
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
                        "Source provider cannot count the frozen Ferry record set.",
                        code="FERRY_SOURCE_HIGH_WATER_MARK_UNSUPPORTED",
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
    ``FERRY_ROUTE_MANIFEST_CHANGED`` otherwise. A provided
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
