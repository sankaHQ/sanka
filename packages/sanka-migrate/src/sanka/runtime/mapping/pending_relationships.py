# SPDX-License-Identifier: AGPL-3.0-only
"""Deferred relationship linking across migration routes.

A relationship can only be written once both ends exist in the destination.
Records whose related record has not been transferred yet park a *pending
relationship*; later passes resolve the related destination ids through the
identity ledger and retry the link.

Pending-relationship entries are plain dicts with camelCase keys
(``sourceRecordId``, ``relatedSourceObject``, …). That is the persisted
run-report/checkpoint format shared with the production runtime, and
:func:`pending_relationship_key` derives dedupe keys from those exact
spellings — do not rename them.

Production scoped ledger lookups by workspace/run/program/channel ids. The
open runtime keeps that scoping out of this module: callers pass an
:class:`IdentityLedger` (run-scoped) and optionally a
:class:`SharedIdentityLedger` (candidates from other compatible runs) that
close over whatever scope the host runtime uses. The shared lookup is
consulted only for ids the run-scoped ledger cannot resolve, and ambiguous
shared candidates always block.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol

from sanka.runtime.mapping.errors import MappingError
from sanka_connector import (
    Credentials,
    DestinationConnector,
    RelationshipWrite,
    SupportsBatchRelationshipWrites,
)

PendingRelationship = dict[str, Any]
PendingRelationships = dict[str, PendingRelationship]
PendingRelationshipsByRoute = dict[str, PendingRelationships]


class IdentityLedger(Protocol):
    """Run-scoped identity lookups: source record id to destination record id."""

    async def get_destination_record_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, str]: ...


class SharedIdentityLedger(Protocol):
    """Cross-run candidates from compatible runs; multiple ids may match."""

    async def get_shared_destination_record_ids(
        self,
        *,
        source_object: str,
        source_record_ids: Sequence[str],
        destination_object: str,
    ) -> dict[str, list[str]]: ...


async def resolve_destination_record_ids(
    *,
    ledger: IdentityLedger,
    shared_ledger: SharedIdentityLedger | None = None,
    source_object: str,
    source_record_ids: Sequence[str],
    destination_object: str,
) -> dict[str, str]:
    normalized_ids = list(dict.fromkeys(str(item) for item in source_record_ids if str(item)))
    destination_ids = await ledger.get_destination_record_ids(
        source_object=source_object,
        source_record_ids=normalized_ids,
        destination_object=destination_object,
    )
    missing_source_ids = sorted(set(normalized_ids) - destination_ids.keys())
    if not (missing_source_ids and shared_ledger is not None):
        return destination_ids

    shared_destination_ids = await shared_ledger.get_shared_destination_record_ids(
        source_object=source_object,
        source_record_ids=missing_source_ids,
        destination_object=destination_object,
    )
    ambiguous_count = sum(
        1 for candidate_ids in shared_destination_ids.values() if len(candidate_ids) > 1
    )
    if ambiguous_count:
        raise MappingError(
            "Referenced destination records are ambiguous across compatible Sanka runs.",
            code="SANKA_MIGRATE_RELATIONSHIP_TARGET_AMBIGUOUS",
            details={
                "sourceReferenceObject": source_object,
                "targetReferenceObject": destination_object,
                "ambiguousSourceCount": ambiguous_count,
            },
        )
    destination_ids.update(
        {
            source_id: candidate_ids[0]
            for source_id, candidate_ids in shared_destination_ids.items()
            if len(candidate_ids) == 1
        }
    )
    return destination_ids


def pending_relationship(
    *,
    source_record_id: str,
    destination_object: str,
    destination_record_id: str | None,
    relationship_field: str,
    related_source_object: str,
    related_source_record_id: str,
    related_destination_object: str,
    relationship_mode: str,
    association_category: str | None,
    association_type_id: int | None,
) -> PendingRelationship:
    return {
        "sourceRecordId": str(source_record_id),
        "destinationObject": str(destination_object),
        "destinationRecordId": (
            str(destination_record_id) if destination_record_id is not None else None
        ),
        "relationshipField": str(relationship_field),
        "relatedSourceObject": str(related_source_object),
        "relatedSourceRecordId": str(related_source_record_id),
        "relatedDestinationObject": str(related_destination_object),
        "relationshipMode": str(relationship_mode or "default"),
        "associationCategory": (
            str(association_category) if association_category is not None else None
        ),
        "associationTypeId": (
            int(association_type_id) if association_type_id is not None else None
        ),
    }


def pending_relationship_key(relationship: PendingRelationship) -> str:
    return json.dumps(
        {
            key: relationship.get(key)
            for key in (
                "sourceRecordId",
                "destinationObject",
                "relationshipField",
                "relatedSourceObject",
                "relatedSourceRecordId",
                "relatedDestinationObject",
                "relationshipMode",
                "associationCategory",
                "associationTypeId",
            )
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def report_pending_relationships(report: dict[str, Any]) -> PendingRelationshipsByRoute:
    raw = report.get("routePendingRelationships")
    if not isinstance(raw, dict):
        return {}
    normalized: PendingRelationshipsByRoute = {}
    required_keys = (
        "sourceRecordId",
        "destinationObject",
        "relationshipField",
        "relatedSourceObject",
        "relatedSourceRecordId",
        "relatedDestinationObject",
    )
    for route_key, relationships in raw.items():
        if not str(route_key).strip() or not isinstance(relationships, list):
            continue
        route_relationships: PendingRelationships = {}
        for raw_relationship in relationships:
            if not isinstance(raw_relationship, dict) or any(
                not str(raw_relationship.get(key) or "").strip() for key in required_keys
            ):
                continue
            relationship = pending_relationship(
                source_record_id=str(raw_relationship["sourceRecordId"]),
                destination_object=str(raw_relationship["destinationObject"]),
                destination_record_id=(
                    str(raw_relationship["destinationRecordId"])
                    if raw_relationship.get("destinationRecordId") is not None
                    else None
                ),
                relationship_field=str(raw_relationship["relationshipField"]),
                related_source_object=str(raw_relationship["relatedSourceObject"]),
                related_source_record_id=str(raw_relationship["relatedSourceRecordId"]),
                related_destination_object=str(raw_relationship["relatedDestinationObject"]),
                relationship_mode=str(raw_relationship.get("relationshipMode") or "default"),
                association_category=(
                    str(raw_relationship["associationCategory"])
                    if raw_relationship.get("associationCategory") is not None
                    else None
                ),
                association_type_id=(
                    int(raw_relationship["associationTypeId"])
                    if raw_relationship.get("associationTypeId") is not None
                    else None
                ),
            )
            route_relationships[pending_relationship_key(relationship)] = relationship
        if route_relationships:
            normalized[str(route_key)] = route_relationships
    return normalized


def pending_relationship_source_ids(
    pending_relationships: PendingRelationships,
) -> set[str]:
    return {
        str(relationship["sourceRecordId"])
        for relationship in pending_relationships.values()
        if str(relationship.get("sourceRecordId") or "").strip()
    }


async def retry_pending_relationships(
    *,
    ledger: IdentityLedger,
    shared_ledger: SharedIdentityLedger | None = None,
    destination: DestinationConnector,
    credentials: Credentials,
    pending_relationships: PendingRelationships,
    excluded_source_record_ids: set[str] | None = None,
) -> PendingRelationships:
    remaining = dict(pending_relationships)
    excluded = excluded_source_record_ids or set()
    retry_items = [
        (pending_key, relationship)
        for pending_key, relationship in pending_relationships.items()
        if relationship["sourceRecordId"] not in excluded
    ]
    if not retry_items:
        return remaining

    related_source_ids_by_pair: dict[tuple[str, str], set[str]] = {}
    for _pending_key, relationship in retry_items:
        pair = (
            relationship["relatedSourceObject"],
            relationship["relatedDestinationObject"],
        )
        related_source_ids_by_pair.setdefault(pair, set()).add(
            relationship["relatedSourceRecordId"]
        )
    related_destination_ids_by_pair: dict[tuple[str, str], dict[str, str]] = {}
    for pair, related_source_ids in related_source_ids_by_pair.items():
        related_destination_ids_by_pair[pair] = await resolve_destination_record_ids(
            ledger=ledger,
            shared_ledger=shared_ledger,
            source_object=pair[0],
            source_record_ids=sorted(related_source_ids),
            destination_object=pair[1],
        )

    batch_destination = (
        destination if isinstance(destination, SupportsBatchRelationshipWrites) else None
    )
    relationship_inputs: list[RelationshipWrite] = []
    pending_key_by_trace: dict[str, str] = {}
    for index, (pending_key, relationship) in enumerate(retry_items):
        destination_record_id = str(relationship["destinationRecordId"] or "").strip()
        related_pair = (
            relationship["relatedSourceObject"],
            relationship["relatedDestinationObject"],
        )
        related_destination_id = related_destination_ids_by_pair.get(related_pair, {}).get(
            relationship["relatedSourceRecordId"]
        )
        if not destination_record_id or not related_destination_id:
            continue
        write = RelationshipWrite(
            trace_id=f"pending:{index}",
            object_type=relationship["destinationObject"],
            record_id=destination_record_id,
            relationship_field=relationship["relationshipField"],
            related_object_type=relationship["relatedDestinationObject"],
            related_record_id=related_destination_id,
            relationship_mode=relationship["relationshipMode"],
            association_category=relationship.get("associationCategory"),
            association_type_id=relationship.get("associationTypeId"),
        )
        if batch_destination is not None:
            relationship_inputs.append(write)
            pending_key_by_trace[write.trace_id] = pending_key
            continue
        try:
            await destination.write_relationship(credentials, relationship=write)
        except Exception:
            continue
        remaining.pop(pending_key, None)

    if batch_destination is not None and relationship_inputs:
        try:
            relationship_results = await batch_destination.write_relationships(
                credentials,
                relationships=relationship_inputs,
            )
        except Exception:
            relationship_results = []
        relationship_results_by_trace = {result.trace_id: result for result in relationship_results}
        for trace_id, pending_key in pending_key_by_trace.items():
            result = relationship_results_by_trace.get(trace_id)
            if result is not None and result.status != "failed":
                remaining.pop(pending_key, None)
    return remaining
