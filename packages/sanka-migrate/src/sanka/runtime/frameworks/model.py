# SPDX-License-Identifier: AGPL-3.0-only
"""Stable artifacts for source-framework scans and target-framework plans."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from sanka.runtime.hashing import content_hash


@dataclass(frozen=True, slots=True)
class FrameworkRisk:
    severity: str
    code: str
    message: str
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class RouteIR:
    method: str
    path: str
    operation: str
    view: str
    serializer: str | None = None
    model: str | None = None
    authentication: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    transactional: bool = False
    source_file: str | None = None
    source_line: int | None = None
    supported: bool = True

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True, slots=True)
class FrameworkScan:
    schema_version: int
    source: str
    language: str
    framework: str
    python_version: str
    django_version: str
    drf_version: str
    settings_module: str
    root_urlconf: str
    routes: tuple[RouteIR, ...]
    serializers: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    authentication: tuple[str, ...] = ()
    test_files: int = 0
    risks: tuple[FrameworkRisk, ...] = ()
    scan_hash: str = field(default="")

    def hash_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("scan_hash", None)
        return payload

    def with_hash(self) -> FrameworkScan:
        return replace(self, scan_hash=content_hash(self.hash_payload()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FrameworkScan:
        return cls(
            schema_version=int(payload["schema_version"]),
            source=str(payload["source"]),
            language=str(payload["language"]),
            framework=str(payload["framework"]),
            python_version=str(payload["python_version"]),
            django_version=str(payload["django_version"]),
            drf_version=str(payload["drf_version"]),
            settings_module=str(payload["settings_module"]),
            root_urlconf=str(payload["root_urlconf"]),
            routes=tuple(RouteIR(**item) for item in payload.get("routes", [])),
            serializers=tuple(payload.get("serializers", [])),
            models=tuple(payload.get("models", [])),
            permissions=tuple(payload.get("permissions", [])),
            authentication=tuple(payload.get("authentication", [])),
            test_files=int(payload.get("test_files", 0)),
            risks=tuple(FrameworkRisk(**item) for item in payload.get("risks", [])),
            scan_hash=str(payload.get("scan_hash", "")),
        )


@dataclass(frozen=True, slots=True)
class PlannedRoute:
    method: str
    path: str
    operation: str
    source_view: str
    strategy: str
    automatic: bool

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True, slots=True)
class FrameworkPlan:
    schema_version: int
    source_framework: str
    target_framework: str
    mode: str
    source_scan_hash: str
    settings_module: str
    routes: tuple[PlannedRoute, ...]
    risks: tuple[FrameworkRisk, ...]
    retained: tuple[str, ...]
    default_output: str
    plan_hash: str = field(default="")

    @property
    def automatic_routes(self) -> int:
        return sum(route.automatic for route in self.routes)

    @property
    def readiness(self) -> float:
        if not self.routes:
            return 0.0
        return self.automatic_routes / len(self.routes)

    def hash_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("plan_hash", None)
        return payload

    def with_hash(self) -> FrameworkPlan:
        return replace(self, plan_hash=content_hash(self.hash_payload()))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["automatic_routes"] = self.automatic_routes
        payload["readiness"] = self.readiness
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FrameworkPlan:
        return cls(
            schema_version=int(payload["schema_version"]),
            source_framework=str(payload["source_framework"]),
            target_framework=str(payload["target_framework"]),
            mode=str(payload["mode"]),
            source_scan_hash=str(payload["source_scan_hash"]),
            settings_module=str(payload["settings_module"]),
            routes=tuple(PlannedRoute(**item) for item in payload.get("routes", [])),
            risks=tuple(FrameworkRisk(**item) for item in payload.get("risks", [])),
            retained=tuple(payload.get("retained", [])),
            default_output=str(payload["default_output"]),
            plan_hash=str(payload.get("plan_hash", "")),
        )
