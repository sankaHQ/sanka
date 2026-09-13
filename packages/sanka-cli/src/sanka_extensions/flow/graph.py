# SPDX-License-Identifier: Apache-2.0
"""Executable semantics for the first bounded Flow graph shape.

This declares meaning, not an executor. Unknown operations and extra fields are
rejected rather than treated as an approximation of a supported action.
"""

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
    identifiers,
    instances,
    object_fields,
    unique,
)
from sanka_extensions.flow.definition import JsonValue
from sanka_extensions.flow.identity import Reference, require_reference


@dataclass(frozen=True, slots=True, init=False)
class ValueBinding(WireRecord):
    """A literal, the event record, an exact reference, or a before/after field."""

    _payload: FrozenJson = field(repr=False)

    def __init__(
        self,
        kind: Literal["literal", "event_record", "event_field", "reference"],
        *,
        value: JsonValue = None,
        field_ref: str | None = None,
        phase: Literal["before", "after"] | None = None,
        reference_id: str | None = None,
    ) -> None:
        choice(kind, ("literal", "event_record", "event_field", "reference"), "value binding")
        payload: dict[str, JsonValue] = {"kind": kind}
        if kind == "literal":
            if any(item is not None for item in (field_ref, phase, reference_id)):
                raise ValueError("literal binding cannot have field or reference arguments")
            payload["value"] = value
        else:
            if value is not None:
                raise ValueError("only literal bindings accept a value")
            if kind == "event_field":
                identifier(field_ref, "event field_ref")
                choice(phase, ("before", "after"), "event phase")
                if reference_id is not None:
                    raise ValueError("event field binding cannot have reference_id")
                payload.update(field_ref=field_ref, phase=phase)
            elif kind == "reference":
                identifier(reference_id, "binding reference_id")
                if field_ref is not None or phase is not None:
                    raise ValueError("reference binding cannot have event arguments")
                payload["reference_id"] = reference_id
            elif field_ref is not None or phase is not None or reference_id is not None:
                raise ValueError("event_record binding cannot have extra arguments")
        object.__setattr__(self, "_payload", FrozenJson(payload))

    @property
    def kind(self) -> str:
        return cast(str, self.to_dict()["kind"])

    def to_dict(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self._payload.value)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        if type(value) is not dict:
            raise ValueError("binding must be an object")
        kind = value.get("kind")
        fields = {
            "literal": {"kind", "value"},
            "event_record": {"kind"},
            "event_field": {"kind", "field_ref", "phase"},
            "reference": {"kind", "reference_id"},
        }
        if type(kind) is not str or kind not in fields:
            raise ValueError("unsupported value binding kind")
        return cls(**object_fields(value, fields[kind], "value binding"))


@dataclass(frozen=True, slots=True)
class FieldMapping(WireRecord):
    field_ref: str
    value: ValueBinding

    def __post_init__(self) -> None:
        identifier(self.field_ref, "field mapping reference")
        if type(self.value) is not ValueBinding:
            raise ValueError("field mapping value must be a ValueBinding")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"field_ref": self.field_ref, "value": self.value.to_dict()}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"field_ref", "value"}, "field mapping")
        return cls(payload["field_ref"], ValueBinding.from_dict(payload["value"]))


@dataclass(frozen=True, slots=True)
class AssociationMapping(WireRecord):
    relationship_ref: str
    record: ValueBinding
    required: bool = True

    def __post_init__(self) -> None:
        identifier(self.relationship_ref, "association reference")
        if type(self.record) is not ValueBinding or self.record.kind not in (
            "event_record",
            "reference",
        ):
            raise ValueError("association record must bind the event or an exact record reference")
        boolean(self.required, "association required")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "relationship_ref": self.relationship_ref,
            "record": self.record.to_dict(),
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"relationship_ref", "record", "required"}, "association")
        return cls(
            payload["relationship_ref"],
            ValueBinding.from_dict(payload["record"]),
            payload["required"],
        )


@dataclass(frozen=True, slots=True)
class RecordIdentity(WireRecord):
    """One bound target per installation/workflow/action/source record.

    Repeated deliveries and leaving/reentering a stage preserve the same bound
    record. A missing bound target requires conflict resolution, never recreation
    by name or silently issuing another record.
    """

    scope: Literal["installation_workflow_action_event_record"] = (
        "installation_workflow_action_event_record"
    )
    on_existing: Literal["preserve"] = "preserve"
    on_missing: Literal["conflict"] = "conflict"

    def __post_init__(self) -> None:
        choice(self.scope, ("installation_workflow_action_event_record",), "record identity scope")
        choice(self.on_existing, ("preserve",), "existing-record policy")
        choice(self.on_missing, ("conflict",), "missing-record policy")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"scope": self.scope, "on_existing": self.on_existing, "on_missing": self.on_missing}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(
            **object_fields(value, {"scope", "on_existing", "on_missing"}, "record identity")
        )


@dataclass(frozen=True, slots=True)
class Trigger(WireRecord):
    object_ref: str
    changed_fields: tuple[str, ...] = ()
    operation: Literal["record.updated", "record.created"] = "record.updated"

    def __post_init__(self) -> None:
        choice(self.operation, ("record.updated", "record.created"), "trigger operation")
        identifier(self.object_ref, "trigger object_ref")
        fields = identifiers(self.changed_fields, "trigger changed_fields")
        if self.operation == "record.updated" and not fields:
            raise ValueError("record.updated requires at least one changed field")
        if self.operation == "record.created" and fields:
            raise ValueError("record.created cannot watch changed fields")
        object.__setattr__(self, "changed_fields", fields)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "operation": self.operation,
            "object_ref": self.object_ref,
            "changed_fields": list(self.changed_fields),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"operation", "object_ref", "changed_fields"}, "trigger")
        return cls(
            payload["object_ref"],
            array(payload["changed_fields"], lambda v: identifier(v, "field"), "changed_fields"),
            payload["operation"],
        )


@dataclass(frozen=True, slots=True)
class Condition(WireRecord):
    left: ValueBinding
    right: ValueBinding
    operator: Literal["equals"] = "equals"

    def __post_init__(self) -> None:
        choice(self.operator, ("equals",), "condition operator")
        if type(self.left) is not ValueBinding or type(self.right) is not ValueBinding:
            raise ValueError("condition operands must be ValueBindings")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "operator": self.operator,
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"operator", "left", "right"}, "condition")
        return cls(
            ValueBinding.from_dict(payload["left"]),
            ValueBinding.from_dict(payload["right"]),
            payload["operator"],
        )


@dataclass(frozen=True, slots=True)
class Action(WireRecord):
    object_ref: str
    fields: tuple[FieldMapping, ...]
    associations: tuple[AssociationMapping, ...] = ()
    identity: RecordIdentity = field(default_factory=RecordIdentity)
    operation: Literal["record.create"] = "record.create"

    def __post_init__(self) -> None:
        choice(self.operation, ("record.create",), "action operation")
        identifier(self.object_ref, "action object_ref")
        fields = unique(
            instances(self.fields, FieldMapping, "fields"), lambda f: f.field_ref, "fields"
        )
        if not fields:
            raise ValueError("record.create requires explicit field mappings")
        associations = unique(
            instances(self.associations, AssociationMapping, "associations"),
            lambda a: a.relationship_ref,
            "associations",
        )
        if type(self.identity) is not RecordIdentity:
            raise ValueError("record.create requires an explicit supported record identity")
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "associations", associations)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "operation": self.operation,
            "object_ref": self.object_ref,
            "fields": [item.to_dict() for item in self.fields],
            "associations": [item.to_dict() for item in self.associations],
            "identity": self.identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(
            value, {"operation", "object_ref", "fields", "associations", "identity"}, "action"
        )
        return cls(
            object_ref=payload["object_ref"],
            operation=payload["operation"],
            fields=array(payload["fields"], FieldMapping.from_dict, "fields"),
            associations=array(
                payload["associations"], AssociationMapping.from_dict, "associations"
            ),
            identity=RecordIdentity.from_dict(payload["identity"]),
        )


@dataclass(frozen=True, slots=True)
class WorkflowNode(WireRecord):
    id: str
    spec: Trigger | Condition | Action

    def __post_init__(self) -> None:
        identifier(self.id, "node id")
        if type(self.spec) not in (Trigger, Condition, Action):
            raise ValueError("unsupported workflow node spec")

    @property
    def kind(self) -> str:
        return {Trigger: "trigger", Condition: "condition", Action: "action"}[type(self.spec)]

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, "kind": self.kind, "spec": self.spec.to_dict()}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"id", "kind", "spec"}, "workflow node")
        kind = choice(payload["kind"], ("trigger", "condition", "action"), "node kind")
        parser: type[Trigger] | type[Condition] | type[Action]
        parser = {"trigger": Trigger, "condition": Condition, "action": Action}[kind]
        return cls(payload["id"], parser.from_dict(payload["spec"]))


@dataclass(frozen=True, slots=True)
class WorkflowEdge(WireRecord):
    source: str
    target: str
    when: Literal["always", "true"] = "always"

    def __post_init__(self) -> None:
        identifier(self.source, "edge source")
        identifier(self.target, "edge target")
        choice(self.when, ("always", "true"), "edge condition")
        if self.source == self.target:
            raise ValueError("workflow edges cannot point to themselves")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"source": self.source, "target": self.target, "when": self.when}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(**object_fields(value, {"source", "target", "when"}, "workflow edge"))


@dataclass(frozen=True, slots=True)
class WorkflowGraph(WireRecord):
    """A trigger, optional equality condition, and one record creation action."""

    id: str
    nodes: tuple[WorkflowNode, ...]
    edges: tuple[WorkflowEdge, ...]
    schema_version: str = "sanka-flow-graph/v1"

    def __post_init__(self) -> None:
        identifier(self.id, "workflow id")
        choice(
            self.schema_version,
            ("sanka-flow-graph/v1", "sanka-flow-graph/v2"),
            "graph schema_version",
        )
        nodes = unique(instances(self.nodes, WorkflowNode, "nodes"), lambda n: n.id, "nodes")
        instances(self.edges, WorkflowEdge, "edges")
        kinds = {node.kind for node in nodes}
        conditional = kinds == {"trigger", "condition", "action"} and len(nodes) == 3
        direct = kinds == {"trigger", "action"} and len(nodes) == 2
        if not conditional and not (direct and self.schema_version == "sanka-flow-graph/v2"):
            raise ValueError("supported graph requires exactly one trigger, condition and action")
        by_kind = {node.kind: node for node in nodes}
        trigger = by_kind["trigger"].spec
        assert isinstance(trigger, Trigger)
        if self.schema_version == "sanka-flow-graph/v1" and trigger.operation != "record.updated":
            raise ValueError("record.created requires graph schema_version v2")
        expected = (
            {
                (by_kind["trigger"].id, by_kind["condition"].id, "always"),
                (by_kind["condition"].id, by_kind["action"].id, "true"),
            }
            if conditional
            else {(by_kind["trigger"].id, by_kind["action"].id, "always")}
        )
        actual = {(edge.source, edge.target, edge.when) for edge in self.edges}
        if len(self.edges) != len(expected) or actual != expected:
            raise ValueError(
                "unsupported graph edges; require trigger→condition→action without branches"
            )
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(
            self, "edges", tuple(sorted(self.edges, key=lambda e: (e.source, e.target)))
        )

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "id": self.id,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }
        if self.schema_version != "sanka-flow-graph/v1":
            result["schema_version"] = self.schema_version
        return result

    @classmethod
    def from_dict(cls, value: object) -> Self:
        versioned = type(value) is dict and "schema_version" in value
        fields = {"id", "nodes", "edges"} | ({"schema_version"} if versioned else set())
        payload = object_fields(value, fields, "workflow graph")
        if versioned and payload["schema_version"] != "sanka-flow-graph/v2":
            raise ValueError("explicit graph schema_version must be sanka-flow-graph/v2")
        return cls(
            payload["id"],
            array(payload["nodes"], WorkflowNode.from_dict, "nodes"),
            array(payload["edges"], WorkflowEdge.from_dict, "edges"),
            payload.get("schema_version", "sanka-flow-graph/v1"),
        )

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        """Exact semantic features a native compiler must implement, never infer."""
        capabilities = {self.schema_version, "flow.record-identity/v1"}
        for node in self.nodes:
            operation = (
                node.spec.operator if isinstance(node.spec, Condition) else node.spec.operation
            )
            capabilities.add(f"flow.{node.kind}.{operation}/v1")
            if isinstance(node.spec, Action) and node.spec.associations:
                capabilities.add("flow.associations/v1")
        return tuple(sorted(capabilities))

    def validate_references(self, references: dict[str, Reference]) -> None:
        trigger = next(node.spec for node in self.nodes if isinstance(node.spec, Trigger))
        require_reference(references, trigger.object_ref, "object")
        for field_ref in trigger.changed_fields:
            require_reference(references, field_ref, "property", parent=trigger.object_ref)
        for node in self.nodes:
            if trigger.operation == "record.created":
                bindings = (
                    (node.spec.left, node.spec.right)
                    if isinstance(node.spec, Condition)
                    else tuple(mapping.value for mapping in node.spec.fields)
                    if isinstance(node.spec, Action)
                    else ()
                )
                if any(binding.to_dict().get("phase") == "before" for binding in bindings):
                    raise ValueError("record.created cannot read before values")
            if isinstance(node.spec, Condition):
                for binding in (node.spec.left, node.spec.right):
                    validate_binding(binding, references, trigger.object_ref)
            if isinstance(node.spec, Action):
                require_reference(references, node.spec.object_ref, "object")
                for field_mapping in node.spec.fields:
                    require_reference(
                        references, field_mapping.field_ref, "property", parent=node.spec.object_ref
                    )
                    validate_binding(field_mapping.value, references, trigger.object_ref)
                for association in node.spec.associations:
                    require_reference(
                        references,
                        association.relationship_ref,
                        "relationship",
                        parent=node.spec.object_ref,
                    )
                    validate_association(association, references, trigger.object_ref)


def validate_binding(
    binding: ValueBinding, references: dict[str, Reference], event_object: str
) -> None:
    value = binding.to_dict()
    if value["kind"] == "event_field":
        require_reference(
            references, cast(str, value["field_ref"]), "property", parent=event_object
        )
    elif value["kind"] == "reference":
        require_reference(references, cast(str, value["reference_id"]), "record")


def validate_association(
    association: AssociationMapping, references: dict[str, Reference], event_object: str
) -> None:
    validate_binding(association.record, references, event_object)
    related_object = references[association.relationship_ref].related_object_id
    binding = association.record.to_dict()
    actual_object: str | None
    if association.record.kind == "event_record":
        actual_object = event_object
    else:
        actual_object = references[cast(str, binding["reference_id"])].parent_id
    if actual_object != related_object:
        raise ValueError("association record belongs to a different related object")
