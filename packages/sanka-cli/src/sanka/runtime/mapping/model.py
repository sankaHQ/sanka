# SPDX-License-Identifier: AGPL-3.0-only
"""Reviewed mapping domain model: mapping fields, value maps, candidate sets.

Faithful port of the production migration-mapping model. The pydantic wire
aliases are gone — the open runtime speaks snake_case Python only — but the
validation rules, defaults, and semantics are unchanged, so a mapping that
was valid in production is valid here and vice versa.

``association_category`` values and the ``HUBSPOT_DEFINED`` family are
HubSpot destination-format names, not Sanka coupling.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from sanka_data import SourceFilter
from sanka_data.records import RelationshipMode

AssociationCategory = Literal["HUBSPOT_DEFINED", "USER_DEFINED", "INTEGRATOR_DEFINED"]
MappingKind = Literal["scalar", "owner", "relationship", "reference"]
UnmappedValuePolicy = Literal["error", "preserve", "omit"]
ValueMapScalar = str | int | float | bool

_MAX_VALUE_MAP_ENTRIES = 2_000
_MAX_VALUE_MAP_PREDICATES = 20


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueMapEntry:
    """One reviewed source-record predicate and its destination scalar value."""

    when: dict[str, ValueMapScalar]
    value: ValueMapScalar

    def __post_init__(self) -> None:
        if not self.when:
            raise ValueError("value_map.when requires at least one predicate field")
        if len(self.when) > _MAX_VALUE_MAP_PREDICATES:
            raise ValueError(
                f"value_map.when supports at most {_MAX_VALUE_MAP_PREDICATES} predicate fields"
            )
        normalized: dict[str, ValueMapScalar] = {}
        for raw_field, expected in self.when.items():
            key = str(raw_field or "").strip()
            if not key:
                raise ValueError("value_map.when field names must not be empty")
            if key in normalized:
                raise ValueError("value_map.when field names must be unique")
            normalized[key] = expected
        object.__setattr__(self, "when", normalized)


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationMappingField:
    """One reviewed source-field-to-destination-field mapping rule."""

    source_field: str
    target_object: str
    target_field: str
    source_type: str | None = None
    target_type: str | None = None
    transform_rule: str | None = None
    required: bool = False
    identity: bool | None = None
    mapping_kind: MappingKind = "scalar"
    source_reference_object: str | None = None
    target_reference_object: str | None = None
    relationship_mode: RelationshipMode | None = None
    association_category: AssociationCategory | None = None
    association_type_id: int | None = None
    source_filter: SourceFilter | None = None
    value_map: list[ValueMapEntry] = field(default_factory=list)
    unmapped_value_policy: UnmappedValuePolicy = "error"

    def __post_init__(self) -> None:
        if self.association_type_id is not None and self.association_type_id <= 0:
            raise ValueError("association_type_id must be greater than zero")
        if len(self.value_map) > _MAX_VALUE_MAP_ENTRIES:
            raise ValueError(f"value_map supports at most {_MAX_VALUE_MAP_ENTRIES} entries")
        if self.value_map and self.mapping_kind != "scalar":
            raise ValueError("value_map is only valid for scalar mappings")
        predicates: set[tuple[tuple[str, str], ...]] = set()
        for entry in self.value_map:
            predicate = tuple(
                sorted(
                    (key, json.dumps(value, sort_keys=True)) for key, value in entry.when.items()
                )
            )
            if predicate in predicates:
                raise ValueError("value_map predicates must be unique")
            predicates.add(predicate)
        if self.mapping_kind not in {"relationship", "reference"}:
            if self.association_category is not None or self.association_type_id is not None:
                raise ValueError(
                    "association_category and association_type_id are only valid for "
                    "relationship mappings"
                )
            return
        if not str(self.source_reference_object or "").strip():
            raise ValueError(
                f"source_reference_object is required for {self.mapping_kind} mappings"
            )
        if not str(self.target_reference_object or "").strip():
            raise ValueError(
                f"target_reference_object is required for {self.mapping_kind} mappings"
            )
        if self.mapping_kind == "reference":
            if self.association_category is not None or self.association_type_id is not None:
                raise ValueError(
                    "association_category and association_type_id are not valid for "
                    "reference mappings"
                )
            return
        if (self.association_category is None) != (self.association_type_id is None):
            raise ValueError(
                "association_category and association_type_id must be provided together"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class MappingCandidateSet:
    """Heuristic auto-mapping output: candidate fields plus coverage counters.

    ``coverage`` keys (``sourceFields``/``mapped``/``required``/``incompatible``)
    are the persisted report spellings shared with the production runtime.
    """

    fields: list[MigrationMappingField] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
