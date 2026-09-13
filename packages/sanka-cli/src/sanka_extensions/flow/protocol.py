# SPDX-License-Identifier: Apache-2.0
"""Typed one-document protocol for isolated, side-effect-free Flow generators.

Version 1 accepts template definitions only. Source import capabilities need an
explicit future contract; a SourceSnapshot is never a template request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Self, cast

from sanka_extensions.flow._wire import (
    FrozenJson,
    WireRecord,
    array,
    artifact_digest,
    canonical_json,
    choice,
    identifier,
    instances,
    object_fields,
    text,
    unique,
)
from sanka_extensions.flow.blueprint import (
    BLUEPRINT_SCHEMA_VERSION,
    BLUEPRINT_V2_SCHEMA_VERSION,
    Blueprint,
)
from sanka_extensions.flow.definition import (
    FlowDefinition,
    JsonValue,
    decode_definition,
    encode_definition,
)
from sanka_extensions.flow.identity import ArtifactIdentity, Reference, reference_index

PROTOCOL_VERSION = "sanka-flow-extension/v1"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
type JsonScalar = str | int | float | bool | None
type ScalarType = Literal["string", "number", "boolean", "null"]
type OutputSchema = Literal["sanka-flow-blueprint/v1", "sanka-flow-blueprint/v2"]
OUTPUT_SCHEMAS = (BLUEPRINT_SCHEMA_VERSION, BLUEPRINT_V2_SCHEMA_VERSION)


def _scalar_type(value: object) -> ScalarType:
    if value is None:
        return "null"
    if type(value) is str:
        return "string"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float):
        # Reject NaN/Infinity rather than accepting a non-JSON number.
        FrozenJson(value)
        return "number"
    raise ValueError("selected values must be finite JSON scalars")


def _digest(value: object, label: str) -> str:
    checked = text(value, label)
    ArtifactIdentity(label, "1", checked)
    return checked


@dataclass(frozen=True, slots=True)
class ReferenceRequirement(WireRecord):
    """One logical input role; the host selects its exact native key."""

    id: str
    kind: Literal["object", "property", "relationship", "record"]
    parent_id: str | None = None
    related_object_id: str | None = None

    def __post_init__(self) -> None:
        self._reference()

    def _reference(self) -> Reference:
        return Reference(
            self.id, self.kind, self.id, self.parent_id, related_object_id=self.related_object_id
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "kind": self.kind,
            "parent_id": self.parent_id,
            "related_object_id": self.related_object_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(
            **object_fields(
                value, {"id", "kind", "parent_id", "related_object_id"}, "reference role"
            )
        )


@dataclass(frozen=True, slots=True)
class ValueRequirement(WireRecord):
    id: str
    types: tuple[ScalarType, ...]

    def __post_init__(self) -> None:
        identifier(self.id, "selected value id")
        if type(self.types) is not tuple or not self.types:
            raise ValueError("selected value requires an explicit tuple of scalar types")
        for kind in self.types:
            choice(kind, ("string", "number", "boolean", "null"), "selected value type")
        object.__setattr__(
            self, "types", unique(self.types, lambda item: item, "selected value types")
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, "types": list(self.types)}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"id", "types"}, "selected value requirement")
        kinds = array(
            payload["types"],
            lambda item: cast(
                ScalarType, choice(item, ("string", "number", "boolean", "null"), "scalar type")
            ),
            "scalar types",
        )
        return cls(payload["id"], kinds)


@dataclass(frozen=True, slots=True)
class FlowCapability(WireRecord):
    """Static manifest capability; reading it does not import extension code."""

    type: str
    references: tuple[ReferenceRequirement, ...]
    values: tuple[ValueRequirement, ...]
    template: ArtifactIdentity
    output_schema: OutputSchema

    def __post_init__(self) -> None:
        FlowDefinition(type=self.type)
        choice(self.output_schema, OUTPUT_SCHEMAS, "Flow output schema")
        if type(self.template) is not ArtifactIdentity or self.template.id != self.type:
            raise ValueError("Flow capability requires the exact typed template identity")
        references = unique(
            instances(self.references, ReferenceRequirement, "reference requirements"),
            lambda item: item.id,
            "reference requirements",
        )
        values = unique(
            instances(self.values, ValueRequirement, "value requirements"),
            lambda item: item.id,
            "value requirements",
        )
        reference_index(tuple(item._reference() for item in references))
        object.__setattr__(self, "references", references)
        object.__setattr__(self, "values", values)

    def validate_request(self, request: BlueprintRequest) -> None:
        if type(request) is not BlueprintRequest or request.definition.type != self.type:
            raise ValueError("Flow request type is not declared by this capability")
        if request.template != self.template or request.output_schema != self.output_schema:
            raise ValueError("Flow request differs from the declared template or output schema")
        required = {item.id: item for item in self.references}
        supplied = {item.id: item for item in request.references}
        if set(required) != set(supplied):
            raise ValueError("Flow request must supply exactly the declared reference roles")
        for role, reference in supplied.items():
            expected = required[role]
            if (reference.kind, reference.parent_id, reference.related_object_id) != (
                expected.kind,
                expected.parent_id,
                expected.related_object_id,
            ):
                raise ValueError(f"Flow reference role has the wrong type: {role}")
        required_values = {item.id: item.types for item in self.values}
        supplied_values = request.values
        if set(required_values) != set(supplied_values):
            raise ValueError("Flow request must supply exactly the declared selected values")
        for name, value in supplied_values.items():
            if _scalar_type(value) not in required_values[name]:
                raise ValueError(f"Flow selected value has the wrong type: {name}")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "type": self.type,
            "references": [item.to_dict() for item in self.references],
            "values": [item.to_dict() for item in self.values],
            "template": self.template.to_dict(),
            "output_schema": self.output_schema,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(
            value, {"type", "references", "values", "template", "output_schema"}, "Flow capability"
        )
        return cls(
            payload["type"],
            array(payload["references"], ReferenceRequirement.from_dict, "reference requirements"),
            array(payload["values"], ValueRequirement.from_dict, "value requirements"),
            ArtifactIdentity.from_dict(payload["template"]),
            payload["output_schema"],
        )


@dataclass(frozen=True, slots=True)
class TargetIdentity(WireRecord):
    id: str
    revision: str
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        text(self.id, "target id")
        text(self.revision, "target revision")
        if type(self.capabilities) is not tuple:
            raise ValueError("target capabilities must be an explicit tuple")
        for capability in self.capabilities:
            text(capability, "target capability")
        object.__setattr__(
            self,
            "capabilities",
            unique(self.capabilities, lambda item: item, "target capabilities"),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, "revision": self.revision, "capabilities": list(self.capabilities)}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"id", "revision", "capabilities"}, "target identity")
        return cls(
            payload["id"],
            payload["revision"],
            array(
                payload["capabilities"],
                lambda item: text(item, "target capability"),
                "capabilities",
            ),
        )


@dataclass(frozen=True, slots=True, init=False)
class BlueprintRequest(WireRecord):
    request_id: str
    extension: ArtifactIdentity
    definition: FlowDefinition
    target: TargetIdentity
    references: tuple[Reference, ...]
    template: ArtifactIdentity
    output_schema: OutputSchema
    _values: FrozenJson = field(repr=False)

    def __init__(
        self,
        request_id: str,
        extension: ArtifactIdentity,
        definition: FlowDefinition,
        target: TargetIdentity,
        references: tuple[Reference, ...],
        values: dict[str, JsonScalar],
        *,
        template: ArtifactIdentity,
        output_schema: OutputSchema,
    ) -> None:
        identifier(request_id, "request_id")
        if type(extension) is not ArtifactIdentity or type(definition) is not FlowDefinition:
            raise ValueError("Flow requests require typed extension and definition identities")
        if type(target) is not TargetIdentity:
            raise ValueError("Flow requests require a typed target identity")
        if type(template) is not ArtifactIdentity or template.id != definition.type:
            raise ValueError("Flow requests require the exact typed template identity")
        choice(output_schema, OUTPUT_SCHEMAS, "Flow output schema")
        references = unique(
            instances(references, Reference, "request references"),
            lambda item: item.id,
            "request references",
        )
        reference_index(references)
        if any(item.scope != "target" or item.binding != "existing" for item in references):
            raise ValueError("generation inputs must be exact existing target references")
        if type(values) is not dict:
            raise ValueError("selected values must be an object")
        for name, value in values.items():
            identifier(name, "selected value name")
            _scalar_type(value)
        for attribute, assigned in (
            ("request_id", request_id),
            ("extension", extension),
            ("definition", definition),
            ("target", target),
            ("references", references),
            ("template", template),
            ("output_schema", output_schema),
            ("_values", FrozenJson(values)),
        ):
            object.__setattr__(self, attribute, assigned)

    @property
    def operation(self) -> str:
        return "blueprint"

    @property
    def values(self) -> dict[str, JsonScalar]:
        return cast(dict[str, JsonScalar], self._values.value)

    @property
    def digest(self) -> str:
        return artifact_digest(self.to_dict())

    @property
    def blueprint_parameters(self) -> dict[str, JsonValue]:
        """Carry every explicit choice into the reviewed output without credentials."""
        return {
            "target": self.target.to_dict(),
            "values": cast(dict[str, JsonValue], self.values),
            "definition_parameters": self.definition.parameters,
        }

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": PROTOCOL_VERSION,
            "request_id": self.request_id,
            "operation": self.operation,
            "extension": self.extension.to_dict(),
            "definition": encode_definition(self.definition),
            "target": self.target.to_dict(),
            "references": [item.to_dict() for item in self.references],
            "values": cast(dict[str, JsonValue], self.values),
            "template": self.template.to_dict(),
            "output_schema": self.output_schema,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(
            value,
            {
                "schema_version",
                "request_id",
                "operation",
                "extension",
                "definition",
                "target",
                "references",
                "values",
                "template",
                "output_schema",
            },
            "Flow extension request",
        )
        choice(payload["schema_version"], (PROTOCOL_VERSION,), "Flow protocol version")
        choice(payload["operation"], ("blueprint",), "Flow operation")
        return cls(
            payload["request_id"],
            ArtifactIdentity.from_dict(payload["extension"]),
            decode_definition(payload["definition"]),
            TargetIdentity.from_dict(payload["target"]),
            array(payload["references"], Reference.from_dict, "request references"),
            payload["values"],
            template=ArtifactIdentity.from_dict(payload["template"]),
            output_schema=payload["output_schema"],
        )


@dataclass(frozen=True, slots=True)
class FlowExtensionFailure(WireRecord):
    code: str
    message: str

    def __post_init__(self) -> None:
        identifier(self.code, "Flow error code")
        text(self.message, "Flow error message")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(**object_fields(value, {"code", "message"}, "Flow extension error"))


@dataclass(frozen=True, slots=True)
class BlueprintResponse(WireRecord):
    request_id: str
    request_digest: str
    extension: ArtifactIdentity
    outcome: Literal["success", "error"]
    blueprint: Blueprint | None = None
    error: FlowExtensionFailure | None = None

    def __post_init__(self) -> None:
        identifier(self.request_id, "response request_id")
        _digest(self.request_digest, "request digest")
        if type(self.extension) is not ArtifactIdentity:
            raise ValueError("response requires a typed extension identity")
        choice(self.outcome, ("success", "error"), "Flow outcome")
        if self.outcome == "success":
            if type(self.blueprint) is not Blueprint or self.error is not None:
                raise ValueError("success requires a Blueprint and no error")
            if self.blueprint.extension != self.extension:
                raise ValueError("Blueprint extension provenance differs from the response")
        elif self.blueprint is not None or type(self.error) is not FlowExtensionFailure:
            raise ValueError("error requires a failure and no Blueprint")

    @property
    def operation(self) -> str:
        return "blueprint"

    def validate_for(self, request: BlueprintRequest) -> None:
        if type(request) is not BlueprintRequest:
            raise ValueError("response validation requires a BlueprintRequest")
        if (self.request_id, self.request_digest, self.extension) != (
            request.request_id,
            request.digest,
            request.extension,
        ):
            raise ValueError("Flow response belongs to different requested input")
        if self.blueprint is None:
            return
        if (
            self.blueprint.origin.kind != "template"
            or self.blueprint.origin.identity != request.template
        ):
            raise ValueError("Flow output does not identify the requested template")
        if self.blueprint.schema_version != request.output_schema:
            raise ValueError("Flow output changed the requested output schema")
        if self.blueprint.references != request.references or self.blueprint.mappings:
            raise ValueError("Flow output changed the requested target references")
        if FrozenJson(self.blueprint.parameters) != FrozenJson(request.blueprint_parameters):
            raise ValueError("Flow output changed the requested target, values or parameters")
        self.blueprint.require_supported(capabilities=frozenset(request.target.capabilities))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": PROTOCOL_VERSION,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "operation": self.operation,
            "extension": self.extension.to_dict(),
            "outcome": self.outcome,
            "blueprint": self.blueprint.to_dict() if self.blueprint is not None else None,
            "error": self.error.to_dict() if self.error is not None else None,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(
            value,
            {
                "schema_version",
                "request_id",
                "request_digest",
                "operation",
                "extension",
                "outcome",
                "blueprint",
                "error",
            },
            "Flow extension response",
        )
        choice(payload["schema_version"], (PROTOCOL_VERSION,), "Flow protocol version")
        choice(payload["operation"], ("blueprint",), "Flow operation")
        return cls(
            payload["request_id"],
            payload["request_digest"],
            ArtifactIdentity.from_dict(payload["extension"]),
            payload["outcome"],
            Blueprint.from_dict(payload["blueprint"]) if payload["blueprint"] is not None else None,
            FlowExtensionFailure.from_dict(payload["error"])
            if payload["error"] is not None
            else None,
        )

    @classmethod
    def success(cls, request: BlueprintRequest, blueprint: Blueprint) -> Self:
        response = cls(request.request_id, request.digest, request.extension, "success", blueprint)
        response.validate_for(request)
        return response

    @classmethod
    def failure(cls, request: BlueprintRequest, *, code: str, message: str) -> Self:
        return cls(
            request.request_id,
            request.digest,
            request.extension,
            "error",
            error=FlowExtensionFailure(code, message),
        )


def decode_message(data: bytes) -> dict[str, JsonValue]:
    """Reject oversized, duplicate-key or non-finite JSON before typed decoding."""
    if type(data) is not bytes or len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("Flow protocol message exceeds its byte limit")

    def unique_fields(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Flow protocol JSON contains duplicate fields")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_fields)
        if type(value) is not dict:
            raise ValueError("Flow protocol message must be an object")
        canonical_json(value).encode("utf-8")
    except (UnicodeError, RecursionError) as error:
        raise ValueError("Flow protocol message is not bounded UTF-8 JSON") from error
    return cast(dict[str, JsonValue], value)


def encode_message(value: BlueprintRequest | BlueprintResponse) -> bytes:
    if type(value) not in (BlueprintRequest, BlueprintResponse):
        raise ValueError("Flow protocol requires a typed request or response")
    rendered = (canonical_json(value.to_dict()) + "\n").encode("utf-8")
    if len(rendered) > MAX_MESSAGE_BYTES:
        raise ValueError("Flow protocol message exceeds its byte limit")
    return rendered
