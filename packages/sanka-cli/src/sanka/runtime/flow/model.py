# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable Flow host observations and runtime plans.

The Blueprint port is structural so host adapters can supply a separately
released SDK contract. SDK definitions and business templates remain upstream;
these types describe observed state, installation ownership and runtime evidence.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Protocol, cast

from sanka.runtime.hashing import canonical_json, content_hash


class FlowError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ResourceInput(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def spec(self) -> dict[str, Any]: ...

    @property
    def depends_on(self) -> tuple[str, ...]: ...


class BlueprintInput(Protocol):
    @property
    def resources(self) -> tuple[ResourceInput, ...]: ...

    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True, init=False)
class Document:
    """Frozen JSON object. Every read returns an independent decoded copy."""

    text: str

    def __init__(self, value: dict[str, Any]) -> None:
        if type(value) is not dict:
            raise FlowError("FLOW_DOCUMENT_INVALID", "A Flow document must be a JSON object")
        pending: list[tuple[object, int]] = [(value, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 64 or count > 100_000:
                raise FlowError("FLOW_DOCUMENT_LIMIT", "Flow document exceeds structural limits")
            if type(item) is dict:
                if not all(type(key) is str for key in item):
                    raise FlowError("FLOW_DOCUMENT_INVALID", "JSON object keys must be strings")
                pending.extend((child, depth + 1) for child in item.values())
            elif type(item) is list:
                pending.extend((child, depth + 1) for child in item)
            elif item is not None and type(item) not in {str, bool, int, float}:
                raise FlowError(
                    "FLOW_DOCUMENT_INVALID", "Flow documents require JSON-native values"
                )
            elif type(item) is float and not math.isfinite(item):
                raise FlowError("FLOW_DOCUMENT_INVALID", "Flow documents require finite numbers")
        rendered = canonical_json(value)
        if len(rendered.encode("utf-8")) > 4 * 1024 * 1024:
            raise FlowError("FLOW_DOCUMENT_LIMIT", "Flow document exceeds its byte limit")
        object.__setattr__(self, "text", rendered)

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.text))

    @property
    def digest(self) -> str:
        return content_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class ObservedResource:
    target_id: str
    kind: str
    revision: str
    configuration: Document
    active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "kind": self.kind,
            "revision": self.revision,
            "configuration": self.configuration.to_dict(),
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ObservedResource:
        return cls(
            value["target_id"],
            value["kind"],
            value["revision"],
            Document(value["configuration"]),
            value["active"],
        )


@dataclass(frozen=True, slots=True)
class OwnedResource:
    logical_id: str
    target_id: str
    kind: str
    revision: str
    desired: Document
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_id": self.logical_id,
            "target_id": self.target_id,
            "kind": self.kind,
            "revision": self.revision,
            "desired": self.desired.to_dict(),
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OwnedResource:
        return cls(
            value["logical_id"],
            value["target_id"],
            value["kind"],
            value["revision"],
            Document(value["desired"]),
            tuple(value["depends_on"]),
        )


@dataclass(frozen=True, slots=True)
class Installation:
    id: str
    target: str
    revision: int = 0
    resources: tuple[OwnedResource, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "revision": self.revision,
            "resources": [r.to_dict() for r in sorted(self.resources, key=lambda r: r.logical_id)],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Installation:
        return cls(
            value["id"],
            value["target"],
            value["revision"],
            tuple(OwnedResource.from_dict(r) for r in value["resources"]),
        )


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    target: str
    revision: str
    resources: tuple[ObservedResource, ...]
    supported_kinds: frozenset[str]
    # Host validation binds resolved fields/stages/relationships and semantic
    # capabilities to this exact observation, rather than guessing by name.
    context: Document
    blockers: tuple[Document, ...] = ()
    supports_staging: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "revision": self.revision,
            "resources": [r.to_dict() for r in sorted(self.resources, key=lambda r: r.target_id)],
            "supported_kinds": sorted(self.supported_kinds),
            "context": self.context.to_dict(),
            "blockers": [b.to_dict() for b in self.blockers],
            "supports_staging": self.supports_staging,
        }


@dataclass(frozen=True, slots=True)
class FlowPlan:
    document: Document

    @property
    def digest(self) -> str:
        return self.document.digest

    @property
    def applicable(self) -> bool:
        return not self.to_dict()["blockers"]

    def to_dict(self) -> dict[str, Any]:
        return self.document.to_dict()


@dataclass(frozen=True, slots=True)
class Claim:
    plan_digest: str
    attempt_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class OperationReceipt:
    operation_id: str
    request_digest: str
    resource: ObservedResource | None
    target_revision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "request_digest": self.request_digest,
            "resource": self.resource.to_dict() if self.resource else None,
            "target_revision": self.target_revision,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OperationReceipt:
        return cls(
            value["operation_id"],
            value["request_digest"],
            ObservedResource.from_dict(value["resource"]) if value["resource"] else None,
            value["target_revision"],
        )
