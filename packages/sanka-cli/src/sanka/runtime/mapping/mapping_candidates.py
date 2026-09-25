# SPDX-License-Identifier: AGPL-3.0-only
"""Heuristic auto-mapping between a source and a destination inventory.

Faithful port of the production candidate generator: token-overlap plus exact
key/label scoring, a type-family bonus, an email-to-owner bonus, a minimum
score of 50, and greedy one-to-one assignment with deterministic tie-breaking
by destination field key. The weights and threshold are production semantics;
do not tune them here.
"""

from __future__ import annotations

import re

from sanka.runtime.mapping.model import (
    MappingCandidateSet,
    MappingKind,
    MigrationMappingField,
)
from sanka_extensions.app import FieldSchema, Inventory

_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _tokens(value: str) -> set[str]:
    return {token for token in _TOKEN_RE.split(str(value or "").lower()) if token}


def _normalized(value: str) -> str:
    return "".join(sorted(_tokens(value)))


def _type_family(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"currency", "double", "float", "int", "integer", "number", "percent"}:
        return "number"
    if normalized in {"bool", "boolean"}:
        return "boolean"
    if normalized in {"date", "datetime", "timestamp"}:
        return "date"
    if normalized in {"email"}:
        return "email"
    if normalized in {"url"}:
        return "url"
    return "string"


def _score(source: FieldSchema, target: FieldSchema) -> int:
    source_key = _normalized(source.key)
    target_key = _normalized(target.key)
    source_label = _normalized(source.label)
    target_label = _normalized(target.label)
    score = 0
    if source_key and source_key == target_key:
        score += 100
    elif source_label and source_label == target_label:
        score += 80
    else:
        overlap = _tokens(source.key) | _tokens(source.label)
        target_tokens = _tokens(target.key) | _tokens(target.label)
        score += 15 * len(overlap & target_tokens)
    if _type_family(source.data_type) == _type_family(target.data_type):
        score += 20
    source_text = f"{source.key} {source.label}".lower()
    target_text = f"{target.key} {target.label}".lower()
    if "email" in source_text and any(
        marker in target_text for marker in ("owner", "salesperson", "assignee")
    ):
        score += 60
    return score


def _transform_rule(source: FieldSchema, target: FieldSchema) -> str:
    source_family = _type_family(source.data_type)
    target_family = _type_family(target.data_type)
    if source_family == target_family:
        return ""
    if target_family == "number":
        return "number_parse"
    if target_family == "date":
        return "date_parse"
    if target_family == "boolean":
        return "boolean_map"
    return "trim"


def _mapping_kind(
    source: FieldSchema,
    target: FieldSchema,
) -> MappingKind:
    source_tokens = _tokens(source.key) | _tokens(source.label)
    target_tokens = _tokens(target.key) | _tokens(target.label)
    target_text = f"{target.key} {target.label}".lower()
    owner_target = bool(target_tokens & {"owner", "salesperson", "assignee"}) or any(
        marker in target_text for marker in ("owner", "salesperson", "assignee")
    )
    source_is_email = "email" in source_tokens or _type_family(source.data_type) == "email"
    return "owner" if owner_target and source_is_email else "scalar"


def generate_mapping_candidates(
    source: Inventory,
    destination: Inventory,
) -> MappingCandidateSet:
    fields: list[MigrationMappingField] = []
    source_field_count = 0
    destination_by_canonical = {item.canonical_type: item for item in destination.objects}
    for source_object in source.objects:
        source_field_count += len(source_object.fields)
        target_object = destination_by_canonical.get(source_object.canonical_type)
        if target_object is None:
            continue
        available_targets = [field for field in target_object.fields if field.writable]
        used_targets: set[str] = set()
        for source_field in source_object.fields:
            ranked = sorted(
                (
                    (_score(source_field, target_field), target_field)
                    for target_field in available_targets
                    if target_field.key not in used_targets
                ),
                key=lambda item: (-item[0], item[1].key),
            )
            if not ranked or ranked[0][0] < 50:
                continue
            target_field = ranked[0][1]
            used_targets.add(target_field.key)
            fields.append(
                MigrationMappingField(
                    source_field=f"{source_object.key}.{source_field.key}",
                    target_object=target_object.key,
                    target_field=target_field.key,
                    source_type=source_field.data_type,
                    target_type=target_field.data_type,
                    transform_rule=_transform_rule(source_field, target_field),
                    required=target_field.required,
                    identity=(
                        True
                        if target_field.unique or target_field.key in target_object.identity_fields
                        else None
                    ),
                    mapping_kind=_mapping_kind(source_field, target_field),
                )
            )

    fields.sort(key=lambda row: (row.target_object, row.source_field, row.target_field))
    required = sum(1 for field in fields if field.required)
    return MappingCandidateSet(
        fields=fields,
        coverage={
            "sourceFields": source_field_count,
            "mapped": len(fields),
            "required": required,
            "incompatible": sum(1 for field in fields if field.transform_rule),
        },
    )
