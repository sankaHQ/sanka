# SPDX-License-Identifier: Apache-2.0
"""Provenance, reference bindings and explicit unsupported-semantic findings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Self

from sanka_extensions.flow._wire import (
    WireRecord,
    choice,
    identifier,
    instances,
    object_fields,
    text,
    unique,
)
from sanka_extensions.flow.definition import JsonValue


@dataclass(frozen=True, slots=True)
class ArtifactIdentity(WireRecord):
    id: str
    revision: str
    digest: str

    def __post_init__(self) -> None:
        text(self.id, "artifact id")
        text(self.revision, "artifact revision")
        if type(self.digest) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.digest):
            raise ValueError("artifact digest must be sha256:<64 lowercase hexadecimal characters>")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, "revision": self.revision, "digest": self.digest}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(**object_fields(value, {"id", "revision", "digest"}, "artifact identity"))


@dataclass(frozen=True, slots=True)
class BlueprintOrigin(WireRecord):
    kind: Literal["source", "template"]
    identity: ArtifactIdentity

    def __post_init__(self) -> None:
        choice(self.kind, ("source", "template"), "blueprint origin")
        if type(self.identity) is not ArtifactIdentity:
            raise ValueError("origin identity must be an ArtifactIdentity")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"kind": self.kind, "identity": self.identity.to_dict()}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(value, {"kind", "identity"}, "blueprint origin")
        return cls(kind=payload["kind"], identity=ArtifactIdentity.from_dict(payload["identity"]))


@dataclass(frozen=True, slots=True)
class Reference(WireRecord):
    """An exact scoped key, never a display-name adoption instruction."""

    id: str
    kind: Literal["object", "property", "relationship", "record"]
    key: str
    parent_id: str | None = None
    scope: Literal["source", "target"] = "target"
    binding: Literal["existing", "resource"] = "existing"
    related_object_id: str | None = None

    def __post_init__(self) -> None:
        identifier(self.id, "reference id")
        choice(self.kind, ("object", "property", "relationship", "record"), "reference kind")
        choice(self.scope, ("source", "target"), "reference scope")
        choice(self.binding, ("existing", "resource"), "reference binding")
        if self.scope == "source" and self.binding != "existing":
            raise ValueError("source references cannot bind planned resources")
        text(self.key, "reference key")
        if self.kind == "object":
            if self.parent_id is not None:
                raise ValueError("object references cannot have a parent")
        else:
            identifier(self.parent_id, "reference parent_id")
        if self.kind == "relationship":
            identifier(self.related_object_id, "relationship related_object_id")
        elif self.related_object_id is not None:
            raise ValueError("only relationship references can have a related object")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "kind": self.kind,
            "key": self.key,
            "parent_id": self.parent_id,
            "scope": self.scope,
            "binding": self.binding,
            "related_object_id": self.related_object_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(
            **object_fields(
                value,
                {"id", "kind", "key", "parent_id", "scope", "binding", "related_object_id"},
                "reference",
            )
        )


@dataclass(frozen=True, slots=True)
class Mapping(WireRecord):
    source_ref: str
    target_ref: str

    def __post_init__(self) -> None:
        identifier(self.source_ref, "mapping source_ref")
        identifier(self.target_ref, "mapping target_ref")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"source_ref": self.source_ref, "target_ref": self.target_ref}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(**object_fields(value, {"source_ref", "target_ref"}, "mapping"))


@dataclass(frozen=True, slots=True)
class UnsupportedFinding(WireRecord):
    """Every finding blocks construction; there is no ignore/warning switch."""

    code: str
    message: str
    node_id: str | None = None

    def __post_init__(self) -> None:
        identifier(self.code, "unsupported code")
        text(self.message, "unsupported message")
        if self.node_id is not None:
            identifier(self.node_id, "unsupported node_id")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"code": self.code, "message": self.message, "node_id": self.node_id}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(**object_fields(value, {"code", "message", "node_id"}, "unsupported finding"))


def reference_index(references: tuple[Reference, ...]) -> dict[str, Reference]:
    references = unique(
        instances(references, Reference, "references"), lambda r: r.id, "references"
    )
    exact_keys = {(r.scope, r.kind, r.parent_id, r.binding, r.key) for r in references}
    if len(exact_keys) != len(references):
        raise ValueError("references cannot alias the same exact scoped key")
    result = {reference.id: reference for reference in references}
    for reference in references:
        for object_id in (reference.parent_id, reference.related_object_id):
            if object_id is None:
                continue
            parent = result.get(object_id)
            if parent is None or parent.kind != "object" or parent.scope != reference.scope:
                raise ValueError("reference parent must resolve to an object in the same scope")
    return result


def require_reference(
    references: dict[str, Reference], reference_id: str, kind: str, *, parent: str | None = None
) -> None:
    reference = references.get(reference_id)
    if reference is None or reference.kind != kind or reference.scope != "target":
        raise ValueError(f"{reference_id} must resolve to a target {kind} reference")
    if parent is not None and reference.parent_id != parent:
        raise ValueError(f"{reference_id} belongs to a different object")
