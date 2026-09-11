# SPDX-License-Identifier: AGPL-3.0-only
"""Write-free validation: sampled source reads projected through the plan.

Faithful port of the production dry-run route sampling. For every reviewed
route, a sampled (optionally full) set of source pages is read — bounded by a
frozen high-water mark when the caller passes one — and every record is
projected to destination properties through the reviewed mapping. Failures
become reject rows with the production spellings, deduplicated into
per-reason counts, and the global reject list is capped exactly as the
production dry run caps it.

Write-freedom is visible in the type, not by convention (ARCHITECTURE
tenet 2): no signature or import in this module admits a destination
connector, an execution ledger, or a journal — there is nothing here that
*can* write. The only connector methods reachable from this module are
source reads.

Beyond the production sampling core, validation surfaces the two write-free
failures the batch executor would otherwise hit mid-transfer, behind typed
knobs whose parity values are explicit:

- records without a source identity reject with
  :data:`MISSING_IDENTITY_CODE` under ``on_missing_identity="fail"`` (the
  open runtime's default, matching ``run_batch``); production dry-run parity
  is ``"drop"`` — identity is not examined and the record is validated for
  mapping alone.
- ledger-free reference-mapping violations (required reference empty,
  ambiguous scalar reference id) reject under ``check_references=True``;
  production dry-run parity is ``False``. Reference *targets* need an
  identity ledger and are deliberately out of reach here — a route mapping
  only references still rejects its records as
  ``SANKA_MIGRATE_EMPTY_DESTINATION_RECORD``, exactly as the production dry run
  does.

:func:`validation_rejection` and :func:`validation_reason` are the public
ports of the production reject shaping (host shims delegate here). The
rejection reads ``code``/``message``/``details`` structurally from the
error, so the production host's application errors, the mapping family's
:class:`~sanka.runtime.mapping.errors.MappingError`, and
:class:`~sanka.runtime.execution.errors.ExecutionFault` all shape
identically; anything else falls back to the production
``SANKA_MIGRATE_MAPPING_VALUE_INVALID`` rejection.

The private imports from :mod:`sanka.runtime.execution.routes` are
deliberate: validation must read pages and key record identities *exactly*
as batch execution does, or its verdicts describe a different migration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sanka.runtime.execution.errors import ExecutionFault
from sanka.runtime.execution.model import ExecutionRoute
from sanka.runtime.execution.report_codec import _source_filter_payload
from sanka.runtime.execution.routes import (
    _MISSING_IDENTITY_MESSAGE,
    OnMissingIdentity,
    _read_route_page,
    _record_identity,
)
from sanka.runtime.mapping.errors import MappingError
from sanka.runtime.mapping.record_mapping import destination_properties, relationship_source_ids
from sanka_extensions.systems import Credentials, SystemReader

DEFAULT_VALIDATION_SAMPLE_SIZE = 10
"""Records sampled per route — the production dry-run ``maxRecords`` default."""

MAX_VALIDATION_REJECTS = 100
"""Global reject-row cap — the production ``MAX_DRY_RUN_REJECTS``."""

MISSING_IDENTITY_CODE = "SANKA_MIGRATE_SOURCE_RECORD_IDENTITY_MISSING"
"""Reject code for a record the execution ledger could never key.

Minted by the open runtime (production dry-run never examines identity);
never raised, only reported in reject rows, so it is deliberately not part
of :data:`~sanka.runtime.execution.errors.EXECUTION_FAULT_CODES`.
"""

_LIMIT_INVALID_MESSAGE = "Dry-run maxRecords must be a positive integer."
_FALLBACK_REJECTION_CODE = "SANKA_MIGRATE_MAPPING_VALUE_INVALID"
_FALLBACK_REJECTION_MESSAGE = "Mapped source value could not be transformed."
_EMPTY_DESTINATION_MESSAGE = "No destination properties were produced."
_REQUIRED_REFERENCE_EMPTY_MESSAGE = "Required reference source field is empty."
_REFERENCE_AMBIGUOUS_MESSAGE = "A scalar reference mapping must resolve one source id."
_REASON_KEYS = ("code", "message", "sourceField", "targetField")


def validation_rejects_truncated_warning(max_rejects: int) -> str:
    """The production truncation warning, parameterized by the reject cap."""

    return f"Dry-run rejects were truncated to the first {max_rejects} records."


def validation_record_id(
    record: Mapping[str, Any],
    identity_field: str | None = None,
) -> str | None:
    """The reject row's source record id.

    Production spells this ``Id`` → ``id`` → ``external_id``; a route whose
    reviewed plan names an explicit identity field consults it first.
    """

    if identity_field:
        identity = str(record.get(identity_field) or "").strip()
        if identity:
            return identity
    raw = record.get("Id") or record.get("id") or record.get("external_id") or ""
    return str(raw).strip() or None


def validation_rejection(
    *,
    source_object: str,
    destination_object: str,
    route_key: str,
    record: Mapping[str, Any],
    error: Exception,
    identity_field: str | None = None,
) -> dict[str, Any]:
    """One reject row, shaped exactly as the production dry-run rejection.

    Structured errors (a string ``code`` and ``message``, an optional
    ``details`` dict) keep their code and message verbatim, with only the
    ``sourceField``/``targetField`` details surfaced; anything else becomes
    the production ``SANKA_MIGRATE_MAPPING_VALUE_INVALID`` fallback.
    """

    details: dict[str, Any] = {}
    code = getattr(error, "code", None)
    message = getattr(error, "message", None)
    if isinstance(code, str) and code and isinstance(message, str) and message:
        error_details = getattr(error, "details", None)
        if isinstance(error_details, dict):
            details = {
                key: error_details[key]
                for key in ("sourceField", "targetField")
                if error_details.get(key)
            }
    else:
        code = _FALLBACK_REJECTION_CODE
        message = _FALLBACK_REJECTION_MESSAGE
    return {
        "sourceObject": source_object,
        "destinationObject": destination_object,
        "routeKey": route_key,
        "sourceRecordId": validation_record_id(record, identity_field),
        "code": code,
        "message": message,
        **details,
    }


def validation_reason(rejection: Mapping[str, Any]) -> dict[str, Any]:
    """One rejection's deduplication payload (the production reason shape)."""

    return {key: rejection[key] for key in _REASON_KEYS if rejection.get(key)}


async def validate_routes(
    *,
    routes: Sequence[ExecutionRoute],
    source: SystemReader,
    source_credentials: Credentials,
    sample_size: int = DEFAULT_VALIDATION_SAMPLE_SIZE,
    full: bool = False,
    max_rejects: int = MAX_VALIDATION_REJECTS,
    route_high_water_marks: Mapping[str, str | None] | None = None,
    on_missing_identity: OnMissingIdentity = "fail",
    check_references: bool = True,
) -> dict[str, Any]:
    """Validate the reviewed routes against live source records, write-free.

    Reads one sampled page of ``sample_size`` records per route (all pages
    when ``full``; bounded reads when ``route_high_water_marks`` carries a
    frozen mark, and a frozen ``None`` mark samples nothing — the source held
    no records for the route when the scope froze) and projects every record
    through the reviewed mapping. Each record counts exactly once: ``valid``,
    or ``invalid`` with its first write-free failure as the reject row, so
    ``sampled == valid + invalid`` on every route row.

    Returns the deterministic mechanics payload the production dry-run report
    wraps — ``objects`` rows (``sourceObject``, ``destinationObject``,
    ``sourceFilter``, ``sampled``, ``valid``, ``invalid``, ``mappedFields``,
    ``invalidReasons``), the capped ``rejects``, and ``warnings`` — leaving
    the host envelope (``summary``, ``generatedAt``, ``dryRun``,
    ``routeManifest``) to the caller.
    """

    if sample_size < 1:
        raise ExecutionFault(_LIMIT_INVALID_MESSAGE, code="SANKA_MIGRATE_DRY_RUN_LIMIT_INVALID")
    objects: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    rejects_truncated = False
    for route in routes:
        sampled = 0
        valid = 0
        invalid = 0
        invalid_reasons: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
        upper_bound: str | None = None
        frozen_empty = False
        if route_high_water_marks is not None and route.route_key in route_high_water_marks:
            upper_bound = route_high_water_marks[route.route_key]
            frozen_empty = upper_bound is None
        cursor: str | None = None
        while not frozen_empty:
            page = await _read_route_page(
                source=source,
                source_credentials=source_credentials,
                route=route,
                batch_size=sample_size,
                cursor=cursor,
                upper_bound=upper_bound,
            )
            for record in page.records:
                sampled += 1
                rejection = _validate_record(
                    record,
                    route,
                    on_missing_identity=on_missing_identity,
                    check_references=check_references,
                )
                if rejection is None:
                    valid += 1
                    continue
                invalid += 1
                rejects_truncated = (
                    _collect_rejection(
                        rejection,
                        rejects=rejects,
                        invalid_reasons=invalid_reasons,
                        max_rejects=max_rejects,
                    )
                    or rejects_truncated
                )
            if not (full and page.has_more and page.next_cursor):
                break
            cursor = page.next_cursor
        objects.append(
            {
                "sourceObject": route.source_object,
                "destinationObject": route.destination_object,
                "sourceFilter": (
                    _source_filter_payload(route.source_filter)
                    if route.source_filter is not None
                    else None
                ),
                "sampled": sampled,
                "valid": valid,
                "invalid": invalid,
                "mappedFields": len(route.fields),
                "invalidReasons": list(invalid_reasons.values()),
            }
        )
    warnings = [validation_rejects_truncated_warning(max_rejects)] if rejects_truncated else []
    return {"objects": objects, "rejects": rejects, "warnings": warnings}


def _collect_rejection(
    rejection: dict[str, Any],
    *,
    rejects: list[dict[str, Any]],
    invalid_reasons: dict[tuple[tuple[str, Any], ...], dict[str, Any]],
    max_rejects: int,
) -> bool:
    """Count the deduped reason, append under the cap; ``True`` when truncated."""

    reason = validation_reason(rejection)
    reason_key = tuple(reason.items())
    current_reason = invalid_reasons.setdefault(reason_key, {**reason, "count": 0})
    current_reason["count"] += 1
    if len(rejects) < max_rejects:
        rejects.append(rejection)
        return False
    return True


def _validate_record(
    record: dict[str, Any],
    route: ExecutionRoute,
    *,
    on_missing_identity: OnMissingIdentity,
    check_references: bool,
) -> dict[str, Any] | None:
    """The record's first write-free failure as a reject row; ``None`` = valid."""

    def rejection(error: Exception) -> dict[str, Any]:
        return validation_rejection(
            source_object=route.source_object,
            destination_object=route.destination_object,
            route_key=route.route_key,
            record=record,
            error=error,
            identity_field=route.source_identity_field,
        )

    if on_missing_identity == "fail" and not _record_identity(record, route.source_identity_field):
        return rejection(ExecutionFault(_MISSING_IDENTITY_MESSAGE, code=MISSING_IDENTITY_CODE))
    try:
        properties = destination_properties(record, route.fields)
    except (MappingError, TypeError, ValueError) as error:
        return rejection(error)
    if check_references:
        for reference in (
            mapping_field
            for mapping_field in route.fields
            if mapping_field.mapping_kind == "reference"
        ):
            related_source_ids = relationship_source_ids(record, reference)
            reference_details = {
                "sourceField": reference.source_field,
                "targetField": reference.target_field,
            }
            if not related_source_ids:
                if reference.required:
                    return rejection(
                        ExecutionFault(
                            _REQUIRED_REFERENCE_EMPTY_MESSAGE,
                            code="SANKA_MIGRATE_REQUIRED_REFERENCE_SOURCE_FIELD_EMPTY",
                            details=reference_details,
                        )
                    )
                continue
            if len(related_source_ids) != 1:
                return rejection(
                    ExecutionFault(
                        _REFERENCE_AMBIGUOUS_MESSAGE,
                        code="SANKA_MIGRATE_REFERENCE_SOURCE_ID_AMBIGUOUS",
                        details=reference_details,
                    )
                )
    if not properties:
        return rejection(
            ExecutionFault(
                _EMPTY_DESTINATION_MESSAGE, code="SANKA_MIGRATE_EMPTY_DESTINATION_RECORD"
            )
        )
    return None
