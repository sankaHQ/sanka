# SPDX-License-Identifier: Apache-2.0
"""Record movement types: pages, filters, write inputs/results, owners.

Faithful port of the production adapter DTOs. ``trace_id`` threads the
engine's per-record identity through batch calls so results reconcile back to
the identity ledger regardless of provider reordering.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from sanka_connector.errors import DataError

ConflictPolicy = Literal["create", "skip_existing", "update_existing"]
InvalidEmailPolicy = Literal["block", "leave_empty"]
WriteStatus = Literal["created", "updated", "skipped"]
BatchWriteStatus = Literal["created", "updated", "skipped", "failed"]
RelationshipMode = Literal["default", "single", "collection"]
RelationshipStatus = Literal["linked", "skipped"]
BatchRelationshipStatus = Literal["linked", "skipped", "failed"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceFilter:
    """A reviewed source-side predicate that defines one migration route."""

    field: str
    operator: Literal["equals"] = "equals"
    value: bool = True

    def __post_init__(self) -> None:
        if not self.field.strip():
            raise ValueError("SourceFilter.field is required")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordPage:
    """One page of source records with an opaque resume cursor."""

    object_key: str
    records: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class WriteOptions:
    """Reviewed write behavior for one route, applied uniformly to a batch."""

    conflict_policy: ConflictPolicy
    identity_fields: list[str] | None = None
    invalid_email_policy: InvalidEmailPolicy = "block"
    invalid_email_audit_field: str | None = None


def require_identity_values(
    properties: Mapping[str, Any], identity_fields: list[str] | None
) -> list[tuple[str, Any]]:
    """Return one complete, non-NULL reviewed identity tuple.

    A destination must never weaken a composite identity to whichever fields
    happen to be present in one untrusted record.  An empty identity remains a
    supported create-only route; once fields are declared, every field is
    required exactly once and must carry a non-NULL value.
    """

    if not identity_fields:
        return []
    fields = [str(field) for field in identity_fields]
    if any(not field.strip() for field in fields):
        raise DataError("identity field names must not be empty")
    if len(set(fields)) != len(fields):
        raise DataError("identity field names must be unique")
    missing = [field for field in fields if field not in properties]
    if missing:
        raise DataError(
            "record is missing required identity field(s): " + ", ".join(sorted(missing))
        )
    null_fields = [field for field in fields if properties[field] is None]
    if null_fields:
        raise DataError(
            "record has NULL required identity field(s): " + ", ".join(sorted(null_fields))
        )
    return [(field, properties[field]) for field in fields]


@dataclass(frozen=True, slots=True, kw_only=True)
class WriteResult:
    status: WriteStatus
    destination_record_id: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BatchWriteInput:
    trace_id: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class BatchWriteResult:
    trace_id: str
    status: BatchWriteStatus
    destination_record_id: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RelationshipWrite:
    trace_id: str
    object_type: str
    record_id: str
    relationship_field: str
    related_object_type: str
    related_record_id: str
    relationship_mode: RelationshipMode = "default"
    association_category: str | None = None
    association_type_id: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RelationshipWriteResult:
    status: RelationshipStatus
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BatchRelationshipWriteResult:
    trace_id: str
    status: BatchRelationshipStatus
    message: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OwnerProfile:
    """A destination- or source-side user for owner/assignee mapping."""

    id: str
    email: str
    active: bool = True
    name: str | None = None
