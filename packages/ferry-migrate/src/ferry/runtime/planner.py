# SPDX-License-Identifier: AGPL-3.0-only
"""The planner: turn an inspection into a reviewable, hashable plan.

Planning is write-free by construction: this module receives inspection data
and a destination's *suggestion* function, and never touches a write method.
The produced plan is a plain payload with a canonical hash — the thing an
operator (or agent) reviews and the thing ``apply`` is bound to.

v0 planning is deliberately simple: one route per selected source object,
identity field from the object schema, 1:1 field mappings. Heuristic
auto-mapping and target-native schema rules layer in as connectors that need
them arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ferry.connector.schema import Inventory, ObjectSchema, SourceObject
from ferry.runtime.hashing import content_hash


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldMapping:
    source_field: str
    target_field: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RoutePlan:
    route_key: str
    source_object: str
    target_object: str
    canonical_type: str
    identity_field: str
    identity_target_field: str
    field_mappings: list[FieldMapping]
    estimated_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationPlan:
    source_provider: str
    target_provider: str
    routes: list[RoutePlan]
    warnings: list[str] = field(default_factory=list)

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
                    "identityTargetField": r.identity_target_field,
                    "fieldMappings": [
                        {"sourceField": m.source_field, "targetField": m.target_field}
                        for m in r.field_mappings
                    ],
                    "estimatedCount": r.estimated_count,
                }
                for r in self.routes
            ],
            "warnings": list(self.warnings),
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
                    identity_target_field=r["identityTargetField"],
                    field_mappings=[
                        FieldMapping(source_field=m["sourceField"], target_field=m["targetField"])
                        for m in r["fieldMappings"]
                    ],
                    estimated_count=r["estimatedCount"],
                )
                for r in payload["routes"]
            ],
            warnings=list(payload.get("warnings", [])),
        )


def build_plan(
    *,
    source_provider: str,
    target_provider: str,
    inventory: Inventory,
    source_objects: list[SourceObject],
    suggest_target_object: dict[str, str | None],
) -> MigrationPlan:
    """Build the v0 plan: selected objects → identity-mapped routes.

    ``suggest_target_object`` maps canonical type → the destination's
    suggested object (from ``automatic_target_object``), pre-computed by the
    caller so this function stays free of connector objects entirely.
    """
    warnings = list(inventory.warnings)
    schemas: dict[str, ObjectSchema] = {obj.key: obj for obj in inventory.objects}
    selected = [o for o in source_objects if o.default_selected] or list(source_objects)

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
        target_object = (
            source_object.automatic_target_object
            or suggest_target_object.get(source_object.canonical_type)
            or source_object.key
        )
        mappings = [FieldMapping(source_field=f.key, target_field=f.key) for f in schema.fields]
        routes.append(
            RoutePlan(
                route_key=f"{source_object.key}->{target_object}",
                source_object=source_object.key,
                target_object=target_object,
                canonical_type=source_object.canonical_type,
                identity_field=identity_field,
                identity_target_field=identity_field,
                field_mappings=mappings,
                estimated_count=schema.record_count,
            )
        )

    return MigrationPlan(
        source_provider=source_provider,
        target_provider=target_provider,
        routes=routes,
        warnings=warnings,
    )


def _identity_field(schema: ObjectSchema) -> str | None:
    if schema.identity_fields:
        return schema.identity_fields[0]
    field_keys = {f.key for f in schema.fields}
    if "id" in field_keys:
        return "id"
    return None
