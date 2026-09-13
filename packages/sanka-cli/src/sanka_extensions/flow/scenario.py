# SPDX-License-Identifier: Apache-2.0
"""Required, deterministic scenarios; executing them must use isolated adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Self, cast

from sanka_extensions.flow._wire import (
    FrozenJson,
    WireRecord,
    array,
    boolean,
    choice,
    identifier,
    instances,
    object_fields,
    unique,
)
from sanka_extensions.flow.definition import JsonValue
from sanka_extensions.flow.graph import AssociationMapping, FieldMapping


@dataclass(frozen=True, slots=True, init=False)
class ScenarioEvent(WireRecord):
    id: str
    record_id: str
    operation: Literal["record.updated", "record.created"]
    _before: FrozenJson = field(repr=False)
    _after: FrozenJson = field(repr=False)

    def __init__(
        self,
        id: str,
        record_id: str,
        before: dict[str, JsonValue],
        after: dict[str, JsonValue],
        operation: Literal["record.updated", "record.created"] = "record.updated",
    ) -> None:
        identifier(id, "scenario event id")
        identifier(record_id, "scenario record_id")
        choice(operation, ("record.updated", "record.created"), "event operation")
        if type(before) is not dict or type(after) is not dict:
            raise ValueError("event before and after must be property-reference objects")
        for key in before.keys() | after.keys():
            identifier(key, "scenario property reference")
        if operation == "record.created" and before:
            raise ValueError("record.created event cannot have before values")
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "_before", FrozenJson(before))
        object.__setattr__(self, "_after", FrozenJson(after))

    @property
    def before(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self._before.value)

    @property
    def after(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self._after.value)

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "id": self.id,
            "record_id": self.record_id,
            "before": self.before,
            "after": self.after,
        }
        if self.operation != "record.updated":
            result["operation"] = self.operation
        return result

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = {"id", "record_id", "before", "after"}
        if type(value) is dict and "operation" in value:
            fields.add("operation")
        return cls(**object_fields(value, fields, "scenario event"))


@dataclass(frozen=True, slots=True)
class Scenario(WireRecord):
    id: str
    workflow_id: str
    case: Literal["no_match", "match", "retry"]
    events: tuple[ScenarioEvent, ...]
    expected_created_count: int
    expected_fields: tuple[FieldMapping, ...] = ()
    expected_associations: tuple[AssociationMapping, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        identifier(self.id, "scenario id")
        identifier(self.workflow_id, "scenario workflow_id")
        choice(self.case, ("no_match", "match", "retry"), "scenario case")
        boolean(self.required, "scenario required")
        instances(self.events, ScenarioEvent, "scenario events")
        if not self.events:
            raise ValueError("scenario must contain events")
        if type(self.expected_created_count) is not int:
            raise ValueError("expected_created_count must be an integer")
        expected = 0 if self.case == "no_match" else 1
        if self.expected_created_count != expected:
            raise ValueError("scenario count must verify zero on no-match and one on match/retry")
        if self.case == "retry":
            if len(self.events) < 2 or any(event != self.events[0] for event in self.events[1:]):
                raise ValueError("retry scenario must repeat the identical event at least twice")
        elif len(self.events) != 1:
            raise ValueError("match and no_match scenarios must contain exactly one event")
        fields = unique(
            instances(self.expected_fields, FieldMapping, "expected fields"),
            lambda f: f.field_ref,
            "expected fields",
        )
        associations = unique(
            instances(self.expected_associations, AssociationMapping, "expected associations"),
            lambda a: a.relationship_ref,
            "expected associations",
        )
        if self.case == "no_match" and (fields or associations):
            raise ValueError("no-match cannot expect a created record")
        object.__setattr__(self, "expected_fields", fields)
        object.__setattr__(self, "expected_associations", associations)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "case": self.case,
            "events": [event.to_dict() for event in self.events],
            "expected_created_count": self.expected_created_count,
            "expected_fields": [field.to_dict() for field in self.expected_fields],
            "expected_associations": [item.to_dict() for item in self.expected_associations],
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(
            value,
            {
                "id",
                "workflow_id",
                "case",
                "events",
                "expected_created_count",
                "expected_fields",
                "expected_associations",
                "required",
            },
            "scenario",
        )
        return cls(
            id=payload["id"],
            workflow_id=payload["workflow_id"],
            case=payload["case"],
            events=array(payload["events"], ScenarioEvent.from_dict, "events"),
            expected_created_count=payload["expected_created_count"],
            required=payload["required"],
            expected_fields=array(
                payload["expected_fields"], FieldMapping.from_dict, "expected_fields"
            ),
            expected_associations=array(
                payload["expected_associations"],
                AssociationMapping.from_dict,
                "expected_associations",
            ),
        )
