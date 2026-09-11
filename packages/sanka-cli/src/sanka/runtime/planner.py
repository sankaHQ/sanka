# SPDX-License-Identifier: AGPL-3.0-only
"""The planner: turn an inspection into a reviewable, hashable plan.

Planning is write-free by construction: this module receives inspection data
only and never touches a connector. The produced plan is a plain payload with
a canonical hash — the thing an operator (or agent) reviews and the thing
``apply`` is bound to.

Mappings are real :class:`~sanka.runtime.mapping.model.MigrationMappingField`
rules. When the destination already has a schema for a source object's
canonical type, the production heuristic auto-mapper proposes the mappings
(``mapping_origin="auto"``); otherwise the planner falls back to identity
mappings that mirror the source schema (``mapping_origin="identity"`` — the
fresh-database case). Route keys use the production
:func:`~sanka.runtime.mapping.record_mapping.mapping_group_key` derivation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from sanka.runtime.hashing import content_hash
from sanka.runtime.mapping.mapping_candidates import generate_mapping_candidates
from sanka.runtime.mapping.model import (
    MappingKind,
    MigrationMappingField,
    UnmappedValuePolicy,
    ValueMapEntry,
)
from sanka.runtime.mapping.record_mapping import (
    destination_identity_fields,
    mapping_group_key,
    mapping_groups,
)
from sanka_data import SourceFilter
from sanka_data.schema import Inventory, ObjectSchema, SourceObject

MappingOrigin = Literal["auto", "identity"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RoutePlan:
    route_key: str
    source_object: str
    target_object: str
    canonical_type: str
    identity_field: str
    identity_target_fields: list[str]
    field_mappings: list[MigrationMappingField]
    estimated_count: int
    mapping_origin: MappingOrigin
    candidate_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationPlan:
    source_provider: str
    target_provider: str
    routes: list[RoutePlan]
    warnings: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    candidate_hash: str | None = None

    @property
    def ready(self) -> float:
        """Crude readiness heuristic: warnings knock ~3% each, floor 50%."""
        return max(0.5, 1.0 - 0.03 * len(self.warnings)) if self.routes else 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "sourceProvider": self.source_provider,
            "targetProvider": self.target_provider,
            "routes": [
                {
                    "routeKey": r.route_key,
                    "sourceObject": r.source_object,
                    "targetObject": r.target_object,
                    "canonicalType": r.canonical_type,
                    "identityField": r.identity_field,
                    "identityTargetFields": list(r.identity_target_fields),
                    "fieldMappings": [_mapping_field_payload(m) for m in r.field_mappings],
                    "estimatedCount": r.estimated_count,
                    "mappingOrigin": r.mapping_origin,
                    "candidateIds": list(r.candidate_ids),
                }
                for r in self.routes
            ],
            "warnings": list(self.warnings),
            "coverage": dict(self.coverage),
            "candidateHash": self.candidate_hash,
        }

    @property
    def plan_hash(self) -> str:
        return content_hash(self.to_payload())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MigrationPlan:
        return cls(
            source_provider=payload["sourceProvider"],
            target_provider=payload["targetProvider"],
            routes=[
                RoutePlan(
                    route_key=r["routeKey"],
                    source_object=r["sourceObject"],
                    target_object=r["targetObject"],
                    canonical_type=r["canonicalType"],
                    identity_field=r["identityField"],
                    identity_target_fields=list(r["identityTargetFields"]),
                    field_mappings=[_mapping_field_from_payload(m) for m in r["fieldMappings"]],
                    estimated_count=r["estimatedCount"],
                    mapping_origin=cast(MappingOrigin, r["mappingOrigin"]),
                    candidate_ids=[str(value) for value in r.get("candidateIds", [])],
                )
                for r in payload["routes"]
            ],
            warnings=list(payload.get("warnings", [])),
            coverage=dict(payload.get("coverage", {})),
            candidate_hash=(
                str(payload["candidateHash"]) if payload.get("candidateHash") else None
            ),
        )


def build_plan(
    *,
    source_provider: str,
    target_provider: str,
    inventory: Inventory,
    source_objects: list[SourceObject],
    suggest_target_object: dict[str, str | None],
    destination_inventory: Inventory | None = None,
) -> MigrationPlan:
    """Build the plan: auto-mapped routes where the destination has schema,
    identity-mapped routes where it does not."""
    warnings = list(inventory.warnings)
    schemas: dict[str, ObjectSchema] = {obj.key: obj for obj in inventory.objects}
    selected = [o for o in source_objects if o.default_selected] or list(source_objects)

    auto_routes: dict[str, tuple[str, list[MigrationMappingField]]] = {}
    coverage: dict[str, Any] = {}
    if destination_inventory is not None and any(
        obj.fields for obj in destination_inventory.objects
    ):
        candidates = generate_mapping_candidates(inventory, destination_inventory)
        coverage = dict(candidates.coverage)
        for source_object_key, target_object, _source_filter, fields in mapping_groups(
            candidates.fields
        ):
            auto_routes[source_object_key] = (target_object, fields)

    routes: list[RoutePlan] = []
    for source_object in selected:
        schema = schemas.get(source_object.key)
        if schema is None:
            warnings.append(f"source object {source_object.key!r} missing from inventory; skipped")
            continue
        identity_field = _identity_field(schema)
        if identity_field is None:
            warnings.append(f"source object {source_object.key!r} has no identity field; skipped")
            continue

        auto = auto_routes.get(source_object.key)
        if auto is not None:
            target_object, mappings = auto
            identity_targets = destination_identity_fields(mappings)
            origin: MappingOrigin = "auto"
            if not identity_targets:
                warnings.append(
                    f"route {source_object.key!r}: no destination identity field was mapped;"
                    " writes cannot upsert and will rely on the run ledger only"
                )
        else:
            target_object = (
                source_object.automatic_target_object
                or suggest_target_object.get(source_object.canonical_type)
                or source_object.key
            )
            mappings = [
                MigrationMappingField(
                    source_field=f"{source_object.key}.{f.key}",
                    target_object=target_object,
                    target_field=f.key,
                    source_type=f.data_type,
                    identity=True if f.key == identity_field else None,
                )
                for f in schema.fields
            ]
            identity_targets = [identity_field]
            origin = "identity"

        routes.append(
            RoutePlan(
                route_key=mapping_group_key(source_object.key, target_object, None),
                source_object=source_object.key,
                target_object=target_object,
                canonical_type=source_object.canonical_type,
                identity_field=identity_field,
                identity_target_fields=identity_targets,
                field_mappings=mappings,
                estimated_count=schema.record_count,
                mapping_origin=origin,
            )
        )

    return MigrationPlan(
        source_provider=source_provider,
        target_provider=target_provider,
        routes=routes,
        warnings=warnings,
        coverage=coverage,
    )


def _identity_field(schema: ObjectSchema) -> str | None:
    if schema.identity_fields:
        return schema.identity_fields[0]
    field_keys = {f.key for f in schema.fields}
    if "id" in field_keys:
        return "id"
    return None


def _mapping_field_payload(mapping: MigrationMappingField) -> dict[str, Any]:
    return {
        "sourceField": mapping.source_field,
        "targetObject": mapping.target_object,
        "targetField": mapping.target_field,
        "sourceType": mapping.source_type,
        "targetType": mapping.target_type,
        "transformRule": mapping.transform_rule,
        "required": mapping.required,
        "identity": mapping.identity,
        "mappingKind": mapping.mapping_kind,
        "sourceReferenceObject": mapping.source_reference_object,
        "targetReferenceObject": mapping.target_reference_object,
        "relationshipMode": mapping.relationship_mode,
        "associationCategory": mapping.association_category,
        "associationTypeId": mapping.association_type_id,
        "sourceFilter": (
            None
            if mapping.source_filter is None
            else {
                "field": mapping.source_filter.field,
                "operator": mapping.source_filter.operator,
                "value": mapping.source_filter.value,
            }
        ),
        "valueMap": [
            {"when": dict(entry.when), "value": entry.value} for entry in mapping.value_map
        ],
        "unmappedValuePolicy": mapping.unmapped_value_policy,
    }


def _mapping_field_from_payload(payload: dict[str, Any]) -> MigrationMappingField:
    raw_filter = payload.get("sourceFilter")
    return MigrationMappingField(
        source_field=payload["sourceField"],
        target_object=payload["targetObject"],
        target_field=payload["targetField"],
        source_type=payload.get("sourceType"),
        target_type=payload.get("targetType"),
        transform_rule=payload.get("transformRule"),
        required=bool(payload.get("required", False)),
        identity=payload.get("identity"),
        mapping_kind=cast(MappingKind, payload.get("mappingKind", "scalar")),
        source_reference_object=payload.get("sourceReferenceObject"),
        target_reference_object=payload.get("targetReferenceObject"),
        relationship_mode=payload.get("relationshipMode"),
        association_category=payload.get("associationCategory"),
        association_type_id=payload.get("associationTypeId"),
        source_filter=(
            None
            if raw_filter is None
            else SourceFilter(
                field=raw_filter["field"],
                operator=raw_filter["operator"],
                value=raw_filter["value"],
            )
        ),
        value_map=[
            ValueMapEntry(when=dict(entry["when"]), value=entry["value"])
            for entry in payload.get("valueMap", [])
        ],
        unmapped_value_policy=cast(
            UnmappedValuePolicy, payload.get("unmappedValuePolicy", "error")
        ),
    )
