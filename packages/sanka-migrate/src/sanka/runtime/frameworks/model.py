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
    native: bool = False

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True, slots=True)
class SerializerFieldIR:
    """One serializer field with enough captured semantics to regenerate its
    validation natively, including the exact rendered DRF error strings."""

    name: str
    kind: str
    required: bool = False
    read_only: bool = False
    allow_null: bool = False
    allow_blank: bool = False
    trim_whitespace: bool = True
    max_length: int | None = None
    min_length: int | None = None
    min_value: int | None = None
    max_value: int | None = None
    has_default: bool = False
    default: Any = None
    attname: str | None = None
    max_digits: int | None = None
    decimal_places: int | None = None
    choices: tuple[Any, ...] = ()
    unique: bool = False
    unique_message: str | None = None
    child: SerializerIR | None = None
    messages: tuple[tuple[str, str], ...] = ()
    supported: bool = True

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SerializerFieldIR:
        data = dict(payload)
        data["messages"] = tuple(
            (str(key), str(value)) for key, value in payload.get("messages", ())
        )
        data["choices"] = tuple(payload.get("choices", ()))
        child = payload.get("child")
        data["child"] = SerializerIR.from_dict(child) if isinstance(child, dict) else None
        return cls(**data)


@dataclass(frozen=True, slots=True)
class SerializerIR:
    name: str
    model: str
    model_module: str
    model_class: str
    object_name: str
    ordering: tuple[str, ...] = ()
    lookup: str = "pk"
    fields: tuple[SerializerFieldIR, ...] = ()
    create_style: str = "default"
    create_source: str | None = None
    create_imports: tuple[tuple[str, str, str | None], ...] = ()
    update_drops: tuple[str, ...] | None = None
    supported: bool = True

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SerializerIR:
        data = dict(payload)
        data["ordering"] = tuple(payload.get("ordering", ()))
        data["fields"] = tuple(
            SerializerFieldIR.from_dict(item) for item in payload.get("fields", ())
        )
        data["create_imports"] = tuple(
            (str(alias), str(module), None if attr is None else str(attr))
            for alias, module, attr in payload.get("create_imports", ())
        )
        drops = payload.get("update_drops")
        data["update_drops"] = None if drops is None else tuple(str(item) for item in drops)
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ViewAuthIR:
    """Authentication and permission semantics captured for one view.

    Only exactly-recognized configurations are captured: DRF
    TokenAuthentication, IsAuthenticated, the owner-or-read-only object
    permission idiom, and the ``serializer.save(field=self.request.user)``
    perform_create injection. Anything else keeps the view outside the
    native envelope."""

    require_authenticated: bool = False
    token_keyword: str | None = None
    token_db_table: str | None = None
    token_key_column: str = "key"
    token_key_max_length: int = 40
    token_user_column: str = "user_id"
    owner_field: str | None = None
    owner_attname: str | None = None
    inject_owner: str | None = None
    inject_owner_attname: str | None = None
    messages: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ViewAuthIR:
        data = dict(payload)
        data["messages"] = tuple(
            (str(key), str(value)) for key, value in payload.get("messages", ())
        )
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ViewIR:
    name: str
    auth: ViewAuthIR | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ViewIR:
        auth = payload.get("auth")
        return cls(
            name=str(payload["name"]),
            auth=ViewAuthIR.from_dict(auth) if isinstance(auth, dict) else None,
        )


@dataclass(frozen=True, slots=True)
class ApiRootIR:
    path: str
    links: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ApiRootIR:
        return cls(
            path=str(payload["path"]),
            links=tuple((str(key), str(value)) for key, value in payload.get("links", ())),
        )


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
    serializer_details: tuple[SerializerIR, ...] = ()
    api_roots: tuple[ApiRootIR, ...] = ()
    view_details: tuple[ViewIR, ...] = ()
    middleware: tuple[str, ...] = ()
    generic_messages: tuple[tuple[str, str], ...] = ()
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
            serializer_details=tuple(
                SerializerIR.from_dict(item) for item in payload.get("serializer_details", [])
            ),
            api_roots=tuple(ApiRootIR.from_dict(item) for item in payload.get("api_roots", [])),
            view_details=tuple(ViewIR.from_dict(item) for item in payload.get("view_details", [])),
            middleware=tuple(payload.get("middleware", [])),
            generic_messages=tuple(
                (str(key), str(value)) for key, value in payload.get("generic_messages", ())
            ),
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
