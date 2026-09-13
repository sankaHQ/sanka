# SPDX-License-Identifier: Apache-2.0
"""Resolved desired configuration, shared by imports and reusable templates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Self, cast

from sanka_extensions.flow._wire import (
    FrozenJson,
    WireRecord,
    array,
    artifact_digest,
    boolean,
    choice,
    identifier,
    identifiers,
    instances,
    object_fields,
    text,
    unique,
)
from sanka_extensions.flow.definition import JsonValue, _policies
from sanka_extensions.flow.graph import (
    Action,
    Condition,
    Trigger,
    WorkflowGraph,
    validate_association,
    validate_binding,
)
from sanka_extensions.flow.identity import (
    ArtifactIdentity,
    BlueprintOrigin,
    Mapping,
    Reference,
    UnsupportedFinding,
    reference_index,
    require_reference,
)
from sanka_extensions.flow.scenario import Scenario

BLUEPRINT_SCHEMA_VERSION = "sanka-flow-blueprint/v1"
BLUEPRINT_V2_SCHEMA_VERSION = "sanka-flow-blueprint/v2"


@dataclass(frozen=True, slots=True, init=False)
class Resource(WireRecord):
    """A stable logical resource with immutable canonical JSON configuration."""

    id: str
    kind: Literal["workflow", "object", "property"]
    depends_on: tuple[str, ...]
    _spec: FrozenJson = field(repr=False)

    def __init__(
        self,
        id: str,
        kind: Literal["workflow", "object", "property"],
        spec: dict[str, JsonValue],
        depends_on: tuple[str, ...] = (),
    ) -> None:
        identifier(id, "resource id")
        choice(kind, ("workflow", "object", "property"), "resource kind")
        depends_on = identifiers(depends_on, "resource dependencies")
        if id in depends_on:
            raise ValueError("resource cannot depend on itself")
        if kind == "workflow":
            graph = WorkflowGraph.from_dict(spec)
            if graph.id != id:
                raise ValueError("workflow graph identity must match its resource identity")
            spec = graph.to_dict()
        elif kind == "object":
            payload = object_fields(spec, {"name", "slug"}, "object spec")
            text(payload["name"], "object name")
            identifier(payload["slug"], "object slug")
        else:
            payload = object_fields(
                spec, {"object_ref", "name", "key", "value_type", "required"}, "property spec"
            )
            identifier(payload["object_ref"], "property object_ref")
            text(payload["name"], "property name")
            identifier(payload["key"], "property key")
            choice(
                payload["value_type"],
                ("text", "number", "boolean", "date", "datetime"),
                "property value_type",
            )
            boolean(payload["required"], "property required")
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "depends_on", depends_on)
        object.__setattr__(self, "_spec", FrozenJson(spec))

    @property
    def spec(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self._spec.value)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "kind": self.kind,
            "spec": self.spec,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"id", "kind", "spec", "depends_on"}, "resource")
        return cls(
            payload["id"],
            payload["kind"],
            payload["spec"],
            array(payload["depends_on"], lambda v: identifier(v, "dependency"), "depends_on"),
        )


def _dependencies(resources: tuple[Resource, ...]) -> None:
    by_id = {resource.id: resource for resource in resources}
    remaining = set(by_id)
    for resource in resources:
        if set(resource.depends_on) - by_id.keys():
            raise ValueError("resource dependency must resolve within the Blueprint")
    while remaining:
        ready = {key for key in remaining if not (set(by_id[key].depends_on) & remaining)}
        if not ready:
            raise ValueError("resource dependencies contain a cycle")
        remaining -= ready


def _graph_references(graph: WorkflowGraph) -> set[str]:
    refs: set[str] = set()
    for node in graph.nodes:
        spec = node.spec
        if isinstance(spec, Trigger):
            refs.add(spec.object_ref)
            refs.update(spec.changed_fields)
            continue
        if isinstance(spec, Condition):
            bindings = [spec.left, spec.right]
        else:
            refs.add(spec.object_ref)
            refs.update(mapping.field_ref for mapping in spec.fields)
            refs.update(mapping.relationship_ref for mapping in spec.associations)
            bindings = [mapping.value for mapping in spec.fields]
            bindings += [mapping.record for mapping in spec.associations]
        for binding in bindings:
            value = binding.to_dict()
            if binding.kind == "event_field":
                refs.add(cast(str, value["field_ref"]))
            elif binding.kind == "reference":
                refs.add(cast(str, value["reference_id"]))
    return refs


@dataclass(frozen=True, slots=True, init=False)
class Blueprint(WireRecord):
    """A reviewed candidate's desired meaning, not a plan or constructed workflow.

    Artifact identity pins provenance. The runtime must bind the resulting plan
    to target revisions and enforce the unchanged definition policies.
    """

    id: str
    revision: str
    origin: BlueprintOrigin
    extension: ArtifactIdentity
    resources: tuple[Resource, ...]
    references: tuple[Reference, ...]
    mappings: tuple[Mapping, ...]
    scenarios: tuple[Scenario, ...]
    unsupported: tuple[UnsupportedFinding, ...]
    _parameters: FrozenJson = field(repr=False)
    schema_version: str

    def __init__(
        self,
        id: str,
        revision: str,
        origin: BlueprintOrigin,
        extension: ArtifactIdentity,
        resources: tuple[Resource, ...],
        references: tuple[Reference, ...] = (),
        mappings: tuple[Mapping, ...] = (),
        scenarios: tuple[Scenario, ...] = (),
        unsupported: tuple[UnsupportedFinding, ...] = (),
        parameters: dict[str, JsonValue] | None = None,
        schema_version: str = BLUEPRINT_SCHEMA_VERSION,
    ) -> None:
        choice(
            schema_version,
            (BLUEPRINT_SCHEMA_VERSION, BLUEPRINT_V2_SCHEMA_VERSION),
            "Blueprint schema_version",
        )
        identifier(id, "blueprint id")
        text(revision, "blueprint revision")
        if type(origin) is not BlueprintOrigin or type(extension) is not ArtifactIdentity:
            raise ValueError("Blueprint requires typed origin and extension identities")
        resources = unique(instances(resources, Resource, "resources"), lambda r: r.id, "resources")
        references = unique(
            instances(references, Reference, "references"), lambda r: r.id, "references"
        )
        mappings = unique(
            instances(mappings, Mapping, "mappings"), lambda m: m.source_ref, "mapping sources"
        )
        scenarios = unique(instances(scenarios, Scenario, "scenarios"), lambda s: s.id, "scenarios")
        instances(unsupported, UnsupportedFinding, "unsupported findings")
        if parameters is not None and type(parameters) is not dict:
            raise ValueError("Blueprint parameters must be an object")
        for name, value in (
            ("schema_version", schema_version),
            ("id", id),
            ("revision", revision),
            ("origin", origin),
            ("extension", extension),
            ("resources", resources),
            ("references", references),
            ("mappings", mappings),
            ("scenarios", scenarios),
            (
                "unsupported",
                tuple(sorted(unsupported, key=lambda f: (f.code, f.node_id or "", f.message))),
            ),
            ("_parameters", FrozenJson(parameters if parameters is not None else {})),
        ):
            object.__setattr__(self, name, value)
        self._validate()

    @property
    def parameters(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self._parameters.value)

    @property
    def policies(self) -> dict[str, JsonValue]:
        return _policies()

    @property
    def digest(self) -> str:
        return artifact_digest(self.to_dict())

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        capabilities = {self.schema_version}
        for resource in self.resources:
            capabilities.add(f"flow.resource.{resource.kind}/v1")
            if resource.kind == "workflow":
                capabilities.update(WorkflowGraph.from_dict(resource.spec).required_capabilities)
        return tuple(sorted(capabilities))

    def require_supported(self, *, capabilities: frozenset[str] | None = None) -> None:
        if self.unsupported:
            raise ValueError(
                "Blueprint contains unsupported semantics: "
                + ", ".join(f.code for f in self.unsupported)
            )
        if self.schema_version == BLUEPRINT_V2_SCHEMA_VERSION or capabilities is not None:
            missing = set(self.required_capabilities) - (capabilities or frozenset())
            if missing:
                raise ValueError(
                    "Target lacks required Flow capabilities: " + ", ".join(sorted(missing))
                )

    def _validate(self) -> None:
        _dependencies(self.resources)
        references = reference_index(self.references)
        resources = {resource.id: resource for resource in self.resources}
        for reference in self.references:
            if reference.binding == "resource":
                resource = resources.get(reference.key)
                if resource is None or resource.kind != reference.kind:
                    raise ValueError(
                        "planned reference must resolve to a resource of the same kind"
                    )
                if (
                    resource.kind == "property"
                    and resource.spec["object_ref"] != reference.parent_id
                ):
                    raise ValueError("planned property reference must match its resource object")
            elif (
                reference.parent_id is not None
                and references[reference.parent_id].binding == "resource"
            ):
                raise ValueError("existing reference cannot belong to an unconstructed object")
        mapping_targets = {mapping.target_ref for mapping in self.mappings}
        if len(mapping_targets) != len(self.mappings):
            raise ValueError("mapping targets must be unique")
        mapped = {mapping.source_ref: mapping.target_ref for mapping in self.mappings}
        for mapping in self.mappings:
            source = references.get(mapping.source_ref)
            target = references.get(mapping.target_ref)
            if (
                source is None
                or target is None
                or source.scope != "source"
                or target.scope != "target"
                or source.kind != target.kind
            ):
                raise ValueError("mapping must join same-kind source and target references")
            for source_object, target_object in (
                (source.parent_id, target.parent_id),
                (source.related_object_id, target.related_object_id),
            ):
                if source_object is not None and mapped.get(source_object) != target_object:
                    raise ValueError("child mapping requires a matching object mapping")
        if self.origin.kind == "template" and (
            self.mappings or any(r.scope == "source" for r in self.references)
        ):
            raise ValueError("template Blueprints cannot claim source mappings")
        graphs: dict[str, WorkflowGraph] = {}
        for resource in self.resources:
            used: set[str] = set()
            if resource.kind == "workflow":
                graph = WorkflowGraph.from_dict(resource.spec)
                expected_graph = (
                    "sanka-flow-graph/v2"
                    if self.schema_version == BLUEPRINT_V2_SCHEMA_VERSION
                    else "sanka-flow-graph/v1"
                )
                if graph.schema_version != expected_graph:
                    raise ValueError(
                        "workflow graph schema_version must match the Blueprint version"
                    )
                graph.validate_references(references)
                graphs[resource.id] = graph
                used = _graph_references(graph)
            elif resource.kind == "property":
                object_ref = cast(str, resource.spec["object_ref"])
                require_reference(references, object_ref, "object")
                used.add(object_ref)
            for reference_id in tuple(used):
                reference = references[reference_id]
                used.update(
                    item
                    for item in (reference.parent_id, reference.related_object_id)
                    if item is not None
                )
            for reference_id in used:
                reference = references[reference_id]
                if reference.binding == "resource" and reference.key not in resource.depends_on:
                    raise ValueError("planned resource references require an explicit dependency")
        for scenario in self.scenarios:
            if self.schema_version == BLUEPRINT_SCHEMA_VERSION and any(
                event.operation != "record.updated" for event in scenario.events
            ):
                raise ValueError("record.created scenarios require Blueprint schema_version v2")
            scenario_graph = graphs.get(scenario.workflow_id)
            if scenario_graph is None:
                raise ValueError("scenario must reference a workflow resource")
            self._validate_scenario(scenario, scenario_graph, references)
        if not self.unsupported:
            for workflow_id in graphs:
                coverage = {
                    s.case for s in self.scenarios if s.workflow_id == workflow_id and s.required
                }
                if coverage != {"no_match", "match", "retry"}:
                    raise ValueError("every workflow requires no_match, match and retry scenarios")

    @staticmethod
    def _validate_scenario(
        scenario: Scenario, graph: WorkflowGraph, references: dict[str, Reference]
    ) -> None:
        trigger = next(node.spec for node in graph.nodes if isinstance(node.spec, Trigger))
        action = next(node.spec for node in graph.nodes if isinstance(node.spec, Action))
        condition = next(
            (node.spec for node in graph.nodes if isinstance(node.spec, Condition)), None
        )
        bindings = [condition.left, condition.right] if condition else []
        if scenario.case != "no_match":
            bindings += [mapping.value for mapping in action.fields]
            bindings += [mapping.value for mapping in scenario.expected_fields]
        for event in scenario.events:
            if scenario.case != "no_match" and event.operation != trigger.operation:
                raise ValueError("match/retry events must match the trigger operation")
            if (
                scenario.case == "no_match"
                and condition is None
                and trigger.operation == "record.created"
                and event.operation == trigger.operation
            ):
                raise ValueError(
                    "unconditional creation requires a different operation for no_match"
                )
            for field_ref in event.before.keys() | event.after.keys():
                require_reference(references, field_ref, "property", parent=trigger.object_ref)
            for field_ref in trigger.changed_fields if event.operation == trigger.operation else ():
                if field_ref not in event.before or field_ref not in event.after:
                    raise ValueError(
                        "scenario must supply before and after values for watched fields"
                    )
            for binding in bindings if event.operation == trigger.operation else ():
                value = binding.to_dict()
                if binding.kind == "event_field":
                    phase = event.before if value["phase"] == "before" else event.after
                    if value["field_ref"] not in phase:
                        raise ValueError("scenario must supply every evaluated event field")
        if scenario.case != "no_match":
            if {f.field_ref for f in scenario.expected_fields} != {
                f.field_ref for f in action.fields
            }:
                raise ValueError("scenario must assert every created field")
            if {a.relationship_ref for a in scenario.expected_associations} != {
                a.relationship_ref for a in action.associations
            }:
                raise ValueError("scenario must assert every created association")
        for mapping in scenario.expected_fields:
            require_reference(references, mapping.field_ref, "property", parent=action.object_ref)
            validate_binding(mapping.value, references, trigger.object_ref)
        for association in scenario.expected_associations:
            require_reference(
                references, association.relationship_ref, "relationship", parent=action.object_ref
            )
            validate_association(association, references, trigger.object_ref)

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "revision": self.revision,
            "origin": self.origin.to_dict(),
            "extension": self.extension.to_dict(),
            "parameters": self.parameters,
            "policies": self.policies,
            "resources": [resource.to_dict() for resource in self.resources],
            "references": [reference.to_dict() for reference in self.references],
            "mappings": [mapping.to_dict() for mapping in self.mappings],
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
            "unsupported": [finding.to_dict() for finding in self.unsupported],
        }
        if self.schema_version == BLUEPRINT_V2_SCHEMA_VERSION:
            result["required_capabilities"] = list(self.required_capabilities)
        return result

    @classmethod
    def from_dict(cls, value: object) -> Self:
        if type(value) is dict and "schema_version" not in value:
            raise ValueError("Blueprint has unexpected or missing fields: schema_version")
        version = value.get("schema_version") if type(value) is dict else None
        choice(
            version,
            (BLUEPRINT_SCHEMA_VERSION, BLUEPRINT_V2_SCHEMA_VERSION),
            "Blueprint schema_version",
        )
        payload = object_fields(
            value,
            {
                "schema_version",
                "id",
                "revision",
                "origin",
                "extension",
                "parameters",
                "policies",
                "resources",
                "references",
                "mappings",
                "scenarios",
                "unsupported",
            }
            | ({"required_capabilities"} if version == BLUEPRINT_V2_SCHEMA_VERSION else set()),
            "Blueprint",
        )
        if payload["policies"] != _policies():
            raise ValueError(
                "Blueprint policies must preserve changes and require verified activation"
            )
        result = cls(
            schema_version=payload["schema_version"],
            id=payload["id"],
            revision=payload["revision"],
            origin=BlueprintOrigin.from_dict(payload["origin"]),
            extension=ArtifactIdentity.from_dict(payload["extension"]),
            parameters=payload["parameters"],
            resources=array(payload["resources"], Resource.from_dict, "resources"),
            references=array(payload["references"], Reference.from_dict, "references"),
            mappings=array(payload["mappings"], Mapping.from_dict, "mappings"),
            scenarios=array(payload["scenarios"], Scenario.from_dict, "scenarios"),
            unsupported=array(payload["unsupported"], UnsupportedFinding.from_dict, "unsupported"),
        )
        if version == BLUEPRINT_V2_SCHEMA_VERSION and payload["required_capabilities"] != list(
            result.required_capabilities
        ):
            raise ValueError("required_capabilities must exactly describe the Blueprint semantics")
        return result
