# SPDX-License-Identifier: Apache-2.0
"""Portable source inventory. Native configuration is evidence, never executable DSL."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Self, cast

from sanka_extensions.flow._wire import (
    FrozenJson,
    WireRecord,
    array,
    artifact_digest,
    choice,
    identifier,
    instances,
    object_fields,
    text,
    unique,
)
from sanka_extensions.flow.definition import JsonValue
from sanka_extensions.flow.identity import (
    ArtifactIdentity,
    Reference,
    UnsupportedFinding,
    reference_index,
)

SOURCE_SCHEMA_VERSION = "sanka-flow-source/v1"


@dataclass(frozen=True, slots=True, init=False)
class SourceNode(WireRecord):
    id: str
    kind: Literal["trigger", "condition", "action", "unsupported"]
    native_type: str
    _configuration: FrozenJson = field(repr=False)

    def __init__(
        self,
        id: str,
        kind: Literal["trigger", "condition", "action", "unsupported"],
        native_type: str,
        configuration: dict[str, JsonValue],
    ) -> None:
        identifier(id, "source node id")
        choice(kind, ("trigger", "condition", "action", "unsupported"), "source node kind")
        text(native_type, "source native_type")
        if type(configuration) is not dict:
            raise ValueError("source configuration must be an object")
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "native_type", native_type)
        object.__setattr__(self, "_configuration", FrozenJson(configuration))

    @property
    def configuration(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self._configuration.value)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "kind": self.kind,
            "native_type": self.native_type,
            "configuration": self.configuration,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(
            **object_fields(value, {"id", "kind", "native_type", "configuration"}, "source node")
        )


@dataclass(frozen=True, slots=True)
class SourceEdge(WireRecord):
    source: str
    target: str
    label: str

    def __post_init__(self) -> None:
        identifier(self.source, "source edge source")
        identifier(self.target, "source edge target")
        text(self.label, "native edge label")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"source": self.source, "target": self.target, "label": self.label}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(**object_fields(value, {"source", "target", "label"}, "source edge"))


@dataclass(frozen=True, slots=True)
class SourceSnapshot(WireRecord):
    """An immutable captured graph tied to one source endpoint and workspace.

    Source cycles and native branch labels can be retained as evidence. Only an
    explicitly reviewed normalizer can produce a supported desired Blueprint.
    """

    identity: ArtifactIdentity
    endpoint: str
    workspace: str
    nodes: tuple[SourceNode, ...]
    edges: tuple[SourceEdge, ...]
    references: tuple[Reference, ...] = ()
    unsupported: tuple[UnsupportedFinding, ...] = ()

    def __post_init__(self) -> None:
        if type(self.identity) is not ArtifactIdentity:
            raise ValueError("source identity must be an ArtifactIdentity")
        text(self.endpoint, "source endpoint")
        text(self.workspace, "source workspace")
        nodes = unique(
            instances(self.nodes, SourceNode, "source nodes"), lambda n: n.id, "source nodes"
        )
        if not nodes:
            raise ValueError("source snapshot must contain nodes")
        instances(self.edges, SourceEdge, "source edges")
        if len(set(self.edges)) != len(self.edges):
            raise ValueError("source edges must be unique")
        node_ids = {node.id for node in nodes}
        if any(edge.source not in node_ids or edge.target not in node_ids for edge in self.edges):
            raise ValueError("source edge must reference captured nodes")
        reference_index(self.references)
        if any(reference.scope != "source" for reference in self.references):
            raise ValueError("source snapshots must use source-scoped references")
        findings = instances(self.unsupported, UnsupportedFinding, "unsupported findings")
        if any(f.node_id is not None and f.node_id not in node_ids for f in findings):
            raise ValueError("unsupported finding must reference a captured node")
        reported = {finding.node_id for finding in findings}
        if any(node.kind == "unsupported" and node.id not in reported for node in nodes):
            raise ValueError("unsupported source nodes require explicit findings")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(
            self, "edges", tuple(sorted(self.edges, key=lambda e: (e.source, e.target, e.label)))
        )
        object.__setattr__(self, "references", tuple(sorted(self.references, key=lambda r: r.id)))
        object.__setattr__(
            self,
            "unsupported",
            tuple(sorted(findings, key=lambda f: (f.code, f.node_id or "", f.message))),
        )

    @property
    def digest(self) -> str:
        return artifact_digest(self.to_dict())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": SOURCE_SCHEMA_VERSION,
            "identity": self.identity.to_dict(),
            "endpoint": self.endpoint,
            "workspace": self.workspace,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "references": [reference.to_dict() for reference in self.references],
            "unsupported": [finding.to_dict() for finding in self.unsupported],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        payload = object_fields(
            value,
            {
                "schema_version",
                "identity",
                "endpoint",
                "workspace",
                "nodes",
                "edges",
                "references",
                "unsupported",
            },
            "source snapshot",
        )
        if payload["schema_version"] != SOURCE_SCHEMA_VERSION:
            raise ValueError(f"source schema_version must be {SOURCE_SCHEMA_VERSION}")
        return cls(
            identity=ArtifactIdentity.from_dict(payload["identity"]),
            endpoint=payload["endpoint"],
            workspace=payload["workspace"],
            nodes=array(payload["nodes"], SourceNode.from_dict, "nodes"),
            edges=array(payload["edges"], SourceEdge.from_dict, "edges"),
            references=array(payload["references"], Reference.from_dict, "references"),
            unsupported=array(payload["unsupported"], UnsupportedFinding.from_dict, "unsupported"),
        )
