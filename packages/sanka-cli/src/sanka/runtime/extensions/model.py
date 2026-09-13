# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable extension discovery records and stable errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

LIFECYCLE_COMMANDS = frozenset({"apply", "plan", "scan", "test", "verify"})


class ExtensionError(RuntimeError):
    """An extension boundary failed with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details: dict[str, Any] = dict(details) if details else {}


@dataclass(frozen=True)
class MatchedEvidence:
    kind: str
    value: str
    path: str


@dataclass(frozen=True)
class Matcher:
    kind: str
    value: str


@dataclass(frozen=True)
class Wheel:
    name: str
    url: str
    sha256: str


@dataclass(frozen=True)
class EndpointSupport:
    name: str
    roles: tuple[Literal["source", "destination"], ...]


@dataclass(frozen=True)
class FlowReferenceRole:
    id: str
    kind: str
    parent_id: str | None
    related_object_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "parent_id": self.parent_id,
            "related_object_id": self.related_object_id,
        }


@dataclass(frozen=True)
class FlowValueRole:
    id: str
    types: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "types": list(self.types)}


@dataclass(frozen=True)
class FlowTemplateIdentity:
    id: str
    revision: str
    digest: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "revision": self.revision, "digest": self.digest}


@dataclass(frozen=True)
class FlowCapability:
    type: str
    references: tuple[FlowReferenceRole, ...]
    values: tuple[FlowValueRole, ...]
    template: FlowTemplateIdentity
    output_schema: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "references": [item.to_dict() for item in self.references],
            "values": [item.to_dict() for item in self.values],
            "template": self.template.to_dict(),
            "output_schema": self.output_schema,
        }


@dataclass(frozen=True)
class Fingerprint:
    languages: tuple[str, ...]
    frameworks: tuple[str, ...]
    dependencies: tuple[str, ...]
    evidence: tuple[MatchedEvidence, ...]
    hash: str


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    marketplace: str
    kind: Literal["migration", "connector", "flow"]
    protocol_version: str
    distribution: str
    distribution_version: str
    executable: str | None
    entry_point: str | None
    providers: tuple[EndpointSupport, ...]
    commands: tuple[str, ...]
    match_all: tuple[Matcher, ...]
    match_any: tuple[Matcher, ...]
    targets: tuple[str, ...]
    runtime_sanka_cli: str
    wheels: tuple[Wheel, ...]
    digest: str
    capabilities: tuple[FlowCapability, ...] = ()


@dataclass(frozen=True)
class Recommendation:
    id: str
    version: str
    marketplace: str
    marketplace_identity: str
    snapshot_digest: str
    manifest_digest: str
    commands: tuple[str, ...]
    targets: tuple[str, ...]
    evidence: tuple[MatchedEvidence, ...]
    status: tuple[str, ...]
    add_command: str


# Compatibility import for the published manifest model.
Provider = EndpointSupport

# Compatibility names from the earlier systems terminology.
SystemSupport = EndpointSupport
