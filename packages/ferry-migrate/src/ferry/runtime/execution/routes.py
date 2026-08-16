# SPDX-License-Identifier: AGPL-3.0-only
"""Batch route executor: one pass over the selected routes.

Faithful port of the production single-batch route loop. For every selected
route that is not yet complete, one source page is read (bounded when the
route carries a frozen high-water mark), parked pending relationships are
retried, terminal records are skipped without a rewrite, records are mapped
to destination properties (scalar transforms, scalar-reference resolution
through the identity ledger, owner mapping through the destination owner
directory), written one at a time or as a destination batch, and the
per-record results are persisted through the execution ledger with a
count-verified fail-closed check. Failed writes that a concurrent attempt
already committed repair to ``skipped``. Counters count each record once,
failed ids are tracked as a deduped set, and the route checkpoint advances
only past terminal records — a batch partial failure never advances the
checkpoint beyond the first failed record.

Scope-free by construction: the host closes its :class:`ExecutionHost`
adapters over whatever run scope it pins; ``run_batch`` itself has no
vocabulary for a workspace, program, or channel.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ferry.connector import (
    BatchWriteInput,
    Credentials,
    DestinationConnector,
    RecordPage,
    RelationshipWrite,
    SourceConnector,
    SupportsBatchRelationshipWrites,
    SupportsBatchWrites,
    SupportsBoundedReads,
    SupportsOwnerDirectory,
    WriteOptions,
)
from ferry.connector.records import BatchWriteStatus, ConflictPolicy, InvalidEmailPolicy
from ferry.runtime.execution.errors import ExecutionFault
from ferry.runtime.execution.model import (
    BatchPage,
    ExecutionRoute,
    ExecutionSnapshot,
    RecordWriteOutcome,
)
from ferry.runtime.execution.state import ExecutionHost
from ferry.runtime.mapping.owner_mapping import (
    MissingOwnerPolicy,
    OwnerDirectory,
    SourceOwnerDirectory,
    map_owner_properties,
)
from ferry.runtime.mapping.pending_relationships import (
    PendingRelationship,
    PendingRelationships,
    pending_relationship,
    pending_relationship_key,
    pending_relationship_source_ids,
    resolve_destination_record_ids,
    retry_pending_relationships,
)
from ferry.runtime.mapping.record_mapping import (
    destination_identity_fields,
    destination_properties,
    relationship_source_ids,
    source_field_keys,
)

OnMissingIdentity = Literal["drop", "fail"]
"""What a record without a source identity does to the batch.

Production drops such records silently (``"drop"``, behavior preservation);
the open runtime defaults to ``"fail"`` — a synthetic failed result that
holds the checkpoint, because a record the ledger cannot key can never be
reconciled.
"""

_MESSAGE_LIMIT = 500
_MISSING_IDENTITY_MESSAGE = "Source record is missing an identity value."
_ALREADY_WRITTEN_MESSAGE = "Destination record was already written."
_REPAIRED_BY_ACTIVE_ATTEMPT_MESSAGE = (
    "Destination result was already committed by an active execution attempt."
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WritePolicies:
    """Reviewed write behavior applied uniformly across one batch.

    Per-route :class:`~ferry.connector.WriteOptions` derive from these plus
    each route's identity fields; the owner policies feed the owner-mapping
    phase.
    """

    conflict_policy: ConflictPolicy
    missing_owner_policy: MissingOwnerPolicy = "block"
    fallback_owner_email: str | None = None
    invalid_email_policy: InvalidEmailPolicy = "block"
    invalid_email_audit_field: str | None = None


@dataclass(slots=True)
class _RecordState:
    """Per-record working state across the write/relationship/repair phases."""

    record: dict[str, Any]
    source_record_id: str
    already_terminal: bool
    destination_record_id: str | None
    status: BatchWriteStatus | None = None
    message: str | None = None
    relationship_failed: bool = False
    pending_relationships: dict[str, PendingRelationship] = field(default_factory=dict)


@dataclass(slots=True)
class _OwnerDirectories:
    """Owner directories, acquired lazily once per batch."""

    directory: OwnerDirectory | None = None
    source_directory: SourceOwnerDirectory | None = None


def _record_identity(record: Mapping[str, Any]) -> str:
    return str(record.get("Id") or record.get("id") or "").strip()


def _failure_message(error: Exception) -> str:
    return str(error)[:_MESSAGE_LIMIT]


async def _retry_parked_relationships(
    *,
    host: ExecutionHost,
    destination: DestinationConnector,
    destination_credentials: Credentials,
    snapshot: ExecutionSnapshot,
    route_key: str,
    pending_relationships: PendingRelationships,
    excluded_source_record_ids: set[str] | None = None,
) -> PendingRelationships:
    """Retry parked pending relationships; sync the pending-id set."""

    previous_pending_ids = pending_relationship_source_ids(pending_relationships)
    remaining = await retry_pending_relationships(
        ledger=host.identity_ledger,
        shared_ledger=host.shared_identity_ledger,
        destination=destination,
        credentials=destination_credentials,
        pending_relationships=pending_relationships,
        excluded_source_record_ids=excluded_source_record_ids,
    )
    snapshot.route_pending_relationships[route_key] = remaining
    pending_ids = snapshot.route_pending_record_ids.setdefault(route_key, set())
    pending_ids.difference_update(previous_pending_ids)
    pending_ids.update(pending_relationship_source_ids(remaining))
    return remaining


async def _read_route_page(
    *,
    source: SourceConnector,
    source_credentials: Credentials,
    route: ExecutionRoute,
    batch_size: int,
    cursor: str | None,
    upper_bound: str | None,
) -> RecordPage:
    """Read one route page — bounded by the frozen mark when the scope has one."""

    if upper_bound is not None:
        if not isinstance(source, SupportsBoundedReads):
            raise ExecutionFault(
                "Source provider cannot resume the frozen Ferry record set.",
                code="FERRY_SOURCE_HIGH_WATER_MARK_UNSUPPORTED",
            )
        return await source.read_records_bounded(
            source_credentials,
            object_type=route.source_object,
            field_keys=source_field_keys(route.fields),
            limit=batch_size,
            cursor=cursor,
            source_filter=route.source_filter,
            upper_bound=upper_bound,
        )
    return await source.read_records(
        source_credentials,
        object_type=route.source_object,
        field_keys=source_field_keys(route.fields),
        limit=batch_size,
        cursor=cursor,
        source_filter=route.source_filter,
    )


async def _related_destination_ids(
    *,
    host: ExecutionHost,
    route: ExecutionRoute,
    records: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, str]]:
    """Resolve every referenced source id on the page, one lookup per pair.

    Ambiguous cross-run candidates block the whole batch (the shared-ledger
    resolution raises) before any destination write happens.
    """

    related_source_ids_by_pair: dict[tuple[str, str], set[str]] = {}
    for relationship in (
        mapping_field
        for mapping_field in route.fields
        if mapping_field.mapping_kind in {"reference", "relationship"}
    ):
        pair = (
            str(relationship.source_reference_object),
            str(relationship.target_reference_object),
        )
        related_ids = related_source_ids_by_pair.setdefault(pair, set())
        for record in records:
            related_ids.update(relationship_source_ids(record, relationship))
    resolved: dict[tuple[str, str], dict[str, str]] = {}
    for pair, related_source_ids in related_source_ids_by_pair.items():
        resolved[pair] = await resolve_destination_record_ids(
            ledger=host.identity_ledger,
            shared_ledger=host.shared_identity_ledger,
            source_object=pair[0],
            source_record_ids=sorted(related_source_ids),
            destination_object=pair[1],
        )
    return resolved


def _destination_payload(
    record: dict[str, Any],
    route: ExecutionRoute,
    *,
    related_destination_ids: Mapping[tuple[str, str], Mapping[str, str]],
    has_owner_mapping: bool,
    owners: _OwnerDirectories,
    policies: WritePolicies,
) -> dict[str, Any]:
    """Map one record: scalar transforms, scalar references, owner mapping."""

    properties = destination_properties(record, route.fields)
    for reference in (
        mapping_field for mapping_field in route.fields if mapping_field.mapping_kind == "reference"
    ):
        related_source_ids = relationship_source_ids(record, reference)
        if not related_source_ids:
            if reference.required:
                raise ExecutionFault(
                    "Required reference source field is empty.",
                    code="FERRY_REQUIRED_REFERENCE_SOURCE_FIELD_EMPTY",
                    details={
                        "sourceField": reference.source_field,
                        "targetField": reference.target_field,
                    },
                )
            continue
        if len(related_source_ids) != 1:
            raise ExecutionFault(
                "A scalar reference mapping must resolve one source id.",
                code="FERRY_REFERENCE_SOURCE_ID_AMBIGUOUS",
                details={
                    "sourceField": reference.source_field,
                    "targetField": reference.target_field,
                    "sourceIdCount": len(related_source_ids),
                },
            )
        related_destination_id = related_destination_ids.get(
            (
                str(reference.source_reference_object),
                str(reference.target_reference_object),
            ),
            {},
        ).get(related_source_ids[0])
        if not related_destination_id:
            raise ExecutionFault(
                "Referenced destination record has not been written yet.",
                code="FERRY_REFERENCE_TARGET_PENDING",
                details={
                    "sourceField": reference.source_field,
                    "targetField": reference.target_field,
                    "sourceReferenceObject": reference.source_reference_object,
                    "targetReferenceObject": reference.target_reference_object,
                },
            )
        properties[reference.target_field] = related_destination_id
    if has_owner_mapping and owners.directory is not None:
        properties = map_owner_properties(
            properties,
            route.fields,
            directory=owners.directory,
            source_directory=owners.source_directory,
            policy=policies.missing_owner_policy,
            fallback_email=policies.fallback_owner_email,
        )
    if not properties:
        raise ExecutionFault(
            "No destination properties were produced.",
            code="FERRY_EMPTY_DESTINATION_RECORD",
        )
    return properties


async def run_batch(
    *,
    routes: Sequence[ExecutionRoute],
    source: SourceConnector,
    source_credentials: Credentials,
    destination: DestinationConnector,
    destination_credentials: Credentials,
    policies: WritePolicies,
    batch_size: int,
    snapshot: ExecutionSnapshot,
    host: ExecutionHost,
    route_high_water_marks: Mapping[str, str | None] | None = None,
    on_missing_identity: OnMissingIdentity = "fail",
) -> bool:
    """Run one batch pass over the selected routes; mutate ``snapshot``.

    Returns ``has_more`` — whether any route still holds unprocessed records
    (or held a failed record that keeps the batch retryable). The snapshot's
    ``checkpoints`` and ``batch_pages`` are replaced with this batch's
    in-progress routes; counters, pending state, failed-id sets, and
    completed routes are updated in place.
    """

    next_checkpoints: dict[str, str] = {}
    batch_pages: dict[str, BatchPage] = {}
    has_more = False
    owners = _OwnerDirectories()
    batch_writer = destination if isinstance(destination, SupportsBatchWrites) else None
    for route in routes:
        has_owner_mapping = any(
            mapping_field.mapping_kind == "owner" for mapping_field in route.fields
        )
        if has_owner_mapping and owners.directory is None:
            if not isinstance(destination, SupportsOwnerDirectory):
                raise ExecutionFault(
                    "Destination provider does not support owner mapping.",
                    code="FERRY_OWNER_MAPPING_UNSUPPORTED",
                )
            owners.directory = OwnerDirectory(
                await destination.list_owners(destination_credentials)
            )
            if isinstance(source, SupportsOwnerDirectory):
                owners.source_directory = SourceOwnerDirectory(
                    await source.list_owners(source_credentials)
                )
        route_key = route.route_key
        if route_key in snapshot.completed_routes:
            parked = snapshot.route_pending_relationships.get(route_key, {})
            if parked:
                await _retry_parked_relationships(
                    host=host,
                    destination=destination,
                    destination_credentials=destination_credentials,
                    snapshot=snapshot,
                    route_key=route_key,
                    pending_relationships=parked,
                )
            continue
        if (
            route_high_water_marks is not None
            and route_key in route_high_water_marks
            and route_high_water_marks[route_key] is None
        ):
            # The source held no records for this route when the scope froze.
            snapshot.completed_routes.add(route_key)
            continue
        current_route_counts = snapshot.route_counts.setdefault(
            route_key,
            dict.fromkeys(("created", "updated", "skipped"), 0),
        )
        current_route_pending = snapshot.route_pending_record_ids.setdefault(route_key, set())
        current_route_pending_relationships = snapshot.route_pending_relationships.setdefault(
            route_key,
            {},
        )
        current_route_failed = snapshot.route_failed_record_ids.setdefault(route_key, set())
        legacy_cursor = (
            snapshot.checkpoints.get(route.source_object) if route.source_filter is None else None
        )
        current_cursor = (
            str(snapshot.checkpoints.get(route_key) or legacy_cursor or "").strip() or None
        )
        upper_bound = (
            route_high_water_marks.get(route_key) if route_high_water_marks is not None else None
        )
        page = await _read_route_page(
            source=source,
            source_credentials=source_credentials,
            route=route,
            batch_size=batch_size,
            cursor=current_cursor,
            upper_bound=upper_bound,
        )
        page_source_ids = [
            source_record_id
            for record in page.records
            if (source_record_id := _record_identity(record))
        ]
        batch_pages[route_key] = BatchPage(
            source_record_ids=tuple(page_source_ids),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )
        current_route_pending_relationships = await _retry_parked_relationships(
            host=host,
            destination=destination,
            destination_credentials=destination_credentials,
            snapshot=snapshot,
            route_key=route_key,
            pending_relationships=current_route_pending_relationships,
            excluded_source_record_ids=set(page_source_ids),
        )
        terminal_destination_ids = await host.ledger.terminal_destination_ids(
            source_object=route.source_object,
            source_record_ids=page_source_ids,
            destination_object=route.destination_object,
        )
        related_destination_ids = await _related_destination_ids(
            host=host,
            route=route,
            records=page.records,
        )

        # -- write phase ----------------------------------------------------
        last_terminal_cursor = current_cursor
        batch_failed = False
        record_states: list[_RecordState] = []
        batch_write_inputs: list[BatchWriteInput] = []
        write_options = WriteOptions(
            conflict_policy=policies.conflict_policy,
            identity_fields=destination_identity_fields(route.fields),
            invalid_email_policy=policies.invalid_email_policy,
            invalid_email_audit_field=policies.invalid_email_audit_field,
        )
        missing_identity_count = 0
        for record in page.records:
            source_record_id = _record_identity(record)
            if not source_record_id:
                if on_missing_identity == "drop":
                    continue
                record_states.append(
                    _RecordState(
                        record=record,
                        source_record_id=f"missing-identity:{missing_identity_count}",
                        already_terminal=False,
                        destination_record_id=None,
                        status="failed",
                        message=_MISSING_IDENTITY_MESSAGE,
                    )
                )
                missing_identity_count += 1
                continue
            destination_record_id = terminal_destination_ids.get(source_record_id)
            already_terminal = source_record_id in terminal_destination_ids
            state = _RecordState(
                record=record,
                source_record_id=source_record_id,
                already_terminal=already_terminal,
                destination_record_id=destination_record_id,
            )
            try:
                if not already_terminal:
                    properties = _destination_payload(
                        record,
                        route,
                        related_destination_ids=related_destination_ids,
                        has_owner_mapping=has_owner_mapping,
                        owners=owners,
                        policies=policies,
                    )
                    if batch_writer is not None:
                        batch_write_inputs.append(
                            BatchWriteInput(
                                trace_id=source_record_id,
                                properties=properties,
                            )
                        )
                    else:
                        result = await destination.write_record(
                            destination_credentials,
                            object_type=route.destination_object,
                            properties=properties,
                            options=write_options,
                        )
                        state.status = result.status
                        state.destination_record_id = result.destination_record_id
                        state.message = result.message
                else:
                    state.status = "skipped"
                    state.message = _ALREADY_WRITTEN_MESSAGE
            except Exception as error:
                state.status = "failed"
                state.destination_record_id = None
                state.message = _failure_message(error)
            record_states.append(state)

        if batch_writer is not None and batch_write_inputs:
            try:
                batch_results = await batch_writer.write_records(
                    destination_credentials,
                    object_type=route.destination_object,
                    records=batch_write_inputs,
                    options=write_options,
                )
                batch_results_by_trace = {result.trace_id: result for result in batch_results}
            except Exception as error:
                batch_results_by_trace = {}
                batch_error = _failure_message(error)
            else:
                batch_error = "Destination batch did not return a result for this record."
            for state in record_states:
                if state.status is not None:
                    continue
                batch_result = batch_results_by_trace.get(state.source_record_id)
                if batch_result is None:
                    state.status = "failed"
                    state.message = batch_error
                    continue
                state.status = batch_result.status
                state.destination_record_id = batch_result.destination_record_id
                state.message = batch_result.message

        # -- relationship phase ---------------------------------------------
        relationship_batch_writer = (
            destination if isinstance(destination, SupportsBatchRelationshipWrites) else None
        )
        relationship_inputs: list[RelationshipWrite] = []
        relationship_source_by_trace: dict[str, str] = {}
        relationship_pending_by_trace: dict[str, PendingRelationship] = {}
        for state in record_states:
            if state.status == "failed":
                continue
            destination_record_id = state.destination_record_id
            for relationship_index, relationship in enumerate(
                mapping_field
                for mapping_field in route.fields
                if mapping_field.mapping_kind == "relationship"
            ):
                for related_index, related_source_id in enumerate(
                    relationship_source_ids(state.record, relationship)
                ):
                    parked_relationship = pending_relationship(
                        source_record_id=state.source_record_id,
                        destination_object=route.destination_object,
                        destination_record_id=destination_record_id,
                        relationship_field=relationship.target_field,
                        related_source_object=str(relationship.source_reference_object),
                        related_source_record_id=related_source_id,
                        related_destination_object=str(relationship.target_reference_object),
                        relationship_mode=(relationship.relationship_mode or "default"),
                        association_category=relationship.association_category,
                        association_type_id=relationship.association_type_id,
                    )
                    pending_key = pending_relationship_key(parked_relationship)
                    try:
                        if not destination_record_id:
                            raise ExecutionFault(
                                "Destination record id is missing.",
                                code="FERRY_RELATIONSHIP_DESTINATION_PENDING",
                            )
                        related_destination_id = related_destination_ids.get(
                            (
                                str(relationship.source_reference_object),
                                str(relationship.target_reference_object),
                            ),
                            {},
                        ).get(related_source_id)
                        if not related_destination_id:
                            raise ExecutionFault(
                                "Referenced destination record has not been written yet.",
                                code="FERRY_RELATIONSHIP_TARGET_PENDING",
                            )
                        trace_id = f"{state.source_record_id}:{relationship_index}:{related_index}"
                        relationship_write = RelationshipWrite(
                            trace_id=trace_id,
                            object_type=route.destination_object,
                            record_id=destination_record_id,
                            relationship_field=relationship.target_field,
                            related_object_type=str(relationship.target_reference_object),
                            related_record_id=related_destination_id,
                            relationship_mode=(relationship.relationship_mode or "default"),
                            association_category=relationship.association_category,
                            association_type_id=relationship.association_type_id,
                        )
                        if relationship_batch_writer is not None:
                            relationship_inputs.append(relationship_write)
                            relationship_source_by_trace[trace_id] = state.source_record_id
                            relationship_pending_by_trace[trace_id] = parked_relationship
                        else:
                            await destination.write_relationship(
                                destination_credentials,
                                relationship=relationship_write,
                            )
                    except Exception as error:
                        state.relationship_failed = True
                        state.pending_relationships[pending_key] = parked_relationship
                        state.message = (
                            f"{state.message or ''} Relationship pending: {error}"
                        ).strip()[:_MESSAGE_LIMIT]

        if relationship_batch_writer is not None and relationship_inputs:
            try:
                relationship_results = await relationship_batch_writer.write_relationships(
                    destination_credentials,
                    relationships=relationship_inputs,
                )
            except Exception as error:
                relationship_results = []
                relationship_batch_error = _failure_message(error)
            else:
                relationship_batch_error = "Destination association batch did not return a result."
            relationship_results_by_trace = {
                result.trace_id: result for result in relationship_results
            }
            states_by_source_id = {state.source_record_id: state for state in record_states}
            for relationship_input in relationship_inputs:
                relationship_result = relationship_results_by_trace.get(relationship_input.trace_id)
                if relationship_result is not None and relationship_result.status != "failed":
                    continue
                state = states_by_source_id[
                    relationship_source_by_trace[relationship_input.trace_id]
                ]
                state.relationship_failed = True
                parked_relationship = relationship_pending_by_trace[relationship_input.trace_id]
                state.pending_relationships[pending_relationship_key(parked_relationship)] = (
                    parked_relationship
                )
                error_message = (
                    relationship_result.message
                    if relationship_result is not None
                    else relationship_batch_error
                )
                state.message = (
                    f"{state.message or ''} Relationship pending: {error_message}"
                ).strip()[:_MESSAGE_LIMIT]

        # -- pending parking ------------------------------------------------
        page_source_id_set = set(page_source_ids)
        for pending_key, parked_relationship in list(current_route_pending_relationships.items()):
            if parked_relationship["sourceRecordId"] in page_source_id_set:
                current_route_pending_relationships.pop(pending_key, None)
        current_route_pending.difference_update(page_source_id_set)
        for state in record_states:
            current_route_pending_relationships.update(state.pending_relationships)
            if state.pending_relationships:
                current_route_pending.add(state.source_record_id)

        # -- count-verified fail-closed persistence -------------------------
        new_outcomes = [
            RecordWriteOutcome(
                source_object=route.source_object,
                source_record_id=state.source_record_id,
                destination_object=route.destination_object,
                destination_record_id=state.destination_record_id,
                status=state.status or "failed",
                message=state.message,
            )
            for state in record_states
            if not state.already_terminal
        ]
        saved_result_count = await host.ledger.upsert_results(new_outcomes)
        if saved_result_count != len(new_outcomes):
            raise ExecutionFault(
                "Ferry did not persist every destination result in the current batch.",
                code="FERRY_RECORD_RESULT_BULK_SAVE_INCOMPLETE",
                details={
                    "expectedCount": len(new_outcomes),
                    "savedCount": saved_result_count,
                },
            )

        # -- counters, failed-id dedupe, checkpoint advance -----------------
        for state in record_states:
            source_record_id = state.source_record_id
            status: BatchWriteStatus = state.status or "failed"
            if status == "failed":
                host.observer.record_failed(
                    route_key=route_key,
                    source_object=route.source_object,
                    source_record_id=source_record_id,
                )
            if not state.already_terminal and status in {"created", "updated", "skipped"}:
                current_route_counts[status] = current_route_counts.get(status, 0) + 1
            if status == "failed":
                if source_record_id not in current_route_failed:
                    current_route_counts["failed"] = current_route_counts.get("failed", 0) + 1
                current_route_failed.add(source_record_id)
            else:
                if source_record_id in current_route_failed:
                    current_route_counts["failed"] = max(
                        0,
                        current_route_counts.get("failed", 0) - 1,
                    )
                current_route_failed.discard(source_record_id)
                if state.relationship_failed:
                    current_route_pending.add(source_record_id)
                else:
                    current_route_pending.discard(source_record_id)
            if status == "failed":
                batch_failed = True
            elif not batch_failed:
                last_terminal_cursor = source_record_id

        # -- end-of-route pending retry -------------------------------------
        if not page.has_more and current_route_pending_relationships:
            current_route_pending_relationships = await _retry_parked_relationships(
                host=host,
                destination=destination,
                destination_credentials=destination_credentials,
                snapshot=snapshot,
                route_key=route_key,
                pending_relationships=current_route_pending_relationships,
            )

        if batch_failed:
            if last_terminal_cursor:
                next_checkpoints[route_key] = last_terminal_cursor
            has_more = True
        elif page.has_more and page.next_cursor:
            next_checkpoints[route_key] = page.next_cursor
            has_more = True
        else:
            snapshot.completed_routes.add(route_key)

    snapshot.checkpoints = next_checkpoints
    snapshot.batch_pages = batch_pages
    snapshot.has_more = has_more
    return has_more
