# SPDX-License-Identifier: AGPL-3.0-only
"""Django REST Framework to FastAPI migration recipes.

Two strategies share one scan artifact:

- ``native`` (default): generate a genuinely native FastAPI request layer for
  the supported DRF envelope (ModelViewSet CRUD over ModelSerializer fields
  whose semantics the scan captured, plus the router API root). The generated
  serving process keeps Django only for the ORM; DRF never loads into it.
  Format-suffix alias routes are dropped as a disclosed contract change, and
  routes outside the envelope are reported as needing manual adaptation.
- ``compatibility``: the strangler bridge from v0.1. It creates a real FastAPI
  route graph while dispatching each route into the existing Django
  application in-process, preserving observable behavior for the whole route
  surface at the cost of still serving through DRF.

A compatibility bridge must never support the claim that DRF was replaced;
``sanka verify`` and Sanka Migration Bench treat only the native strategy as a
completed migration.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from sanka.runtime.frameworks.model import (
    ApiRootIR,
    FrameworkPlan,
    FrameworkRisk,
    FrameworkScan,
    PlannedRoute,
    RouteIR,
    SerializerFieldIR,
    SerializerIR,
)

DEFAULT_ARTIFACT_DIR = ".sanka"
DEFAULT_FASTAPI_OUTPUT = ".sanka/output/fastapi"
SCAN_FILE = "scan.json"
PLAN_FILE = "plan-fastapi.json"
GENERATED_MANIFEST = "sanka-manifest.json"

NATIVE_STRATEGY = "native"
COMPATIBILITY_STRATEGY = "compatibility"
ROUTE_STRATEGY_NATIVE_CRUD = "native-fastapi-crud"
ROUTE_STRATEGY_NATIVE_API_ROOT = "native-fastapi-api-root"
ROUTE_STRATEGY_DROPPED_ALIAS = "dropped-format-suffix-alias"
ROUTE_STRATEGY_BRIDGE = "django-in-process-compatibility-bridge"
ROUTE_STRATEGY_MANUAL = "needs-manual-adaptation"

_SUPPORTED_VIEWSET_ACTIONS = {
    "list",
    "create",
    "retrieve",
    "update",
    "partial_update",
    "destroy",
}


class FrameworkMigrationError(RuntimeError):
    """Raised when a framework migration cannot proceed safely."""


def scan_django(
    root: str | Path = ".",
    *,
    settings_module: str | None = None,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
) -> FrameworkScan:
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise FrameworkMigrationError(f"source root is not a directory: {root_path}")
    selected_settings = settings_module or _infer_settings_module(root_path)
    django, rest_framework = _bootstrap_django(root_path, selected_settings)
    django_conf = importlib.import_module("django.conf")
    middleware = tuple(str(item) for item in django_conf.settings.MIDDLEWARE)
    django_urls = importlib.import_module("django.urls")
    resolver = django_urls.get_resolver()
    walk = _walk_patterns(resolver.url_patterns, root_path=root_path, middleware=middleware)
    routes, risks = walk.routes, walk.risks
    if not routes:
        raise FrameworkMigrationError(
            "no Django REST Framework routes were detected; check DJANGO_SETTINGS_MODULE"
        )
    serializers = sorted({value for route in routes if (value := route.serializer)})
    models = sorted({value for route in routes if (value := route.model)})
    permissions = sorted({value for route in routes for value in route.permissions})
    authentication = sorted({value for route in routes for value in route.authentication})
    scan = FrameworkScan(
        schema_version=2,
        source=".",
        language="python",
        framework="django-rest-framework",
        python_version=".".join(str(value) for value in sys.version_info[:3]),
        django_version=str(django.get_version()),
        drf_version=str(rest_framework.VERSION),
        settings_module=selected_settings,
        root_urlconf=str(resolver.urlconf_name),
        routes=tuple(sorted(routes, key=lambda route: (route.path, route.method))),
        serializers=tuple(serializers),
        models=tuple(models),
        permissions=tuple(permissions),
        authentication=tuple(authentication),
        test_files=_count_test_files(root_path),
        risks=tuple(risks),
        serializer_details=tuple(
            walk.serializer_details[name] for name in sorted(walk.serializer_details)
        ),
        api_roots=tuple(sorted(walk.api_roots, key=lambda item: item.path)),
        middleware=middleware,
        generic_messages=_generic_messages(),
    ).with_hash()
    _write_json(_artifact_path(root_path, artifact_dir, SCAN_FILE), scan.to_dict())
    return scan


def load_framework_scan(
    root: str | Path = ".", *, artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR
) -> FrameworkScan:
    path = _artifact_path(Path(root).resolve(), artifact_dir, SCAN_FILE)
    payload = _read_json(path, label="scan")
    scan = FrameworkScan.from_dict(payload)
    expected = scan.with_hash().scan_hash
    if not scan.scan_hash or scan.scan_hash != expected:
        raise FrameworkMigrationError(
            "scan artifact hash does not match its contents; run `sanka scan`"
        )
    return scan


def plan_fastapi(
    root: str | Path = ".",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    output: str = DEFAULT_FASTAPI_OUTPUT,
    strategy: str = NATIVE_STRATEGY,
) -> FrameworkPlan:
    if strategy not in (NATIVE_STRATEGY, COMPATIBILITY_STRATEGY):
        raise FrameworkMigrationError(f"unknown plan strategy: {strategy}")
    root_path = Path(root).resolve()
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    if strategy == NATIVE_STRATEGY:
        routes = tuple(_plan_native_route(route) for route in scan.routes)
        retained = (
            "Django models and migrations",
            "Django ORM and synchronous transactions",
            "DRF removed from the serving path; native FastAPI request layer",
            "Format-suffix alias routes dropped (disclosed contract change)",
        )
    else:
        routes = tuple(
            PlannedRoute(
                method=route.method,
                path=route.path,
                operation=route.operation,
                source_view=route.view,
                strategy=ROUTE_STRATEGY_BRIDGE,
                automatic=route.supported,
            )
            for route in scan.routes
        )
        retained = (
            "Django models and migrations",
            "Django ORM and synchronous transactions",
            "Django authentication and permissions",
            "DRF handlers behind the generated compatibility bridge",
        )
    plan = FrameworkPlan(
        schema_version=1,
        source_framework=scan.framework,
        target_framework="fastapi",
        mode=strategy,
        source_scan_hash=scan.scan_hash,
        settings_module=scan.settings_module,
        routes=routes,
        risks=scan.risks,
        retained=retained,
        default_output=output,
    ).with_hash()
    _write_json(_artifact_path(root_path, artifact_dir, PLAN_FILE), plan.to_dict())
    return plan


def _plan_native_route(route: RouteIR) -> PlannedRoute:
    if _is_format_alias_path(route.path):
        strategy = ROUTE_STRATEGY_DROPPED_ALIAS
        automatic = True
    elif route.native and route.serializer is None:
        strategy = ROUTE_STRATEGY_NATIVE_API_ROOT
        automatic = True
    elif route.native:
        strategy = ROUTE_STRATEGY_NATIVE_CRUD
        automatic = True
    else:
        strategy = ROUTE_STRATEGY_MANUAL
        automatic = False
    return PlannedRoute(
        method=route.method,
        path=route.path,
        operation=route.operation,
        source_view=route.view,
        strategy=strategy,
        automatic=automatic,
    )


def load_fastapi_plan(
    root: str | Path = ".", *, artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR
) -> FrameworkPlan:
    root_path = Path(root).resolve()
    path = _artifact_path(root_path, artifact_dir, PLAN_FILE)
    payload = _read_json(path, label="FastAPI plan")
    plan = FrameworkPlan.from_dict(payload)
    expected = plan.with_hash().plan_hash
    if not plan.plan_hash or plan.plan_hash != expected:
        raise FrameworkMigrationError(
            "FastAPI plan hash does not match its contents; run `sanka plan --to fastapi`"
        )
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    if scan.scan_hash != plan.source_scan_hash:
        raise FrameworkMigrationError(
            "the scan changed after this plan was reviewed; run `sanka plan --to fastapi` again"
        )
    return plan


def apply_fastapi_plan(
    root: str | Path = ".",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    output: str | Path | None = None,
    plan_hash: str | None = None,
    force: bool = False,
) -> tuple[Path, int]:
    root_path = Path(root).resolve()
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    if plan_hash is not None and plan_hash != plan.plan_hash:
        raise FrameworkMigrationError(
            f"reviewed plan hash {plan_hash!r} does not match current plan {plan.plan_hash!r}"
        )
    output_value = str(output) if output is not None else plan.default_output
    output_path = Path(output_value)
    if not output_path.is_absolute():
        output_path = root_path / output_path
    output_path = output_path.resolve()
    if output_path == root_path:
        raise FrameworkMigrationError("generated output cannot overwrite the source root")
    if output_path.exists() and any(output_path.iterdir()) and not force:
        raise FrameworkMigrationError(
            f"output is not empty: {output_path}; pass --force to replace generated files"
        )
    output_path.mkdir(parents=True, exist_ok=True)
    relative_source = os.path.relpath(root_path, output_path)
    if plan.mode == NATIVE_STRATEGY:
        scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
        count = _render_native_output(
            plan,
            scan,
            output_path,
            entrypoint="app.py",
            source_root=relative_source,
        )
    else:
        count = _render_bridge_output(plan, output_path, source_root=relative_source)
    return output_path, count


def write_bench_candidate(
    root: str | Path = ".",
    destination: str | Path = "bench-candidate",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
) -> Path:
    """Emit a Sanka Migration Bench candidate from the reviewed native plan.

    The overlay merges into the benchmark's copy of the source repository, so
    the entrypoint is the bench's fixed ``target_app.py`` and ``source_root``
    is the workspace root itself.
    """
    root_path = Path(root).resolve()
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    if plan.mode != NATIVE_STRATEGY:
        raise FrameworkMigrationError(
            "benchmark candidates require a native plan; run `sanka plan --to fastapi`"
        )
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    destination_path = Path(destination)
    if not destination_path.is_absolute():
        destination_path = root_path / destination_path
    destination_path = destination_path.resolve()
    if destination_path == root_path:
        raise FrameworkMigrationError("benchmark candidate cannot overwrite the source root")
    overlay = destination_path / "overlay"
    _render_native_output(plan, scan, overlay, entrypoint="target_app.py", source_root=".")
    _write_text(
        destination_path / "candidate.yaml",
        (
            "schema_version: sanka-bench/candidate/v0.1\n"
            "id: sanka-native\n"
            "kind: overlay\n"
            "overlay: overlay\n"
            "provenance:\n"
            "  producer: sanka\n"
            f"  revision: {plan.plan_hash}\n"
            "  command: sanka scan && sanka plan --to fastapi && sanka apply"
            " --bench-candidate <dir>\n"
        ),
    )
    return destination_path


def _allow_headers(generated: list[PlannedRoute]) -> dict[str, str]:
    """Per-path Allow header values matching DRF's method advertisement."""
    methods_by_path: dict[str, set[str]] = {}
    for route in generated:
        methods_by_path.setdefault(route.path, set()).add(route.method.upper())
    order = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
    allow: dict[str, str] = {}
    for path, methods in methods_by_path.items():
        if "GET" in methods:
            methods.add("HEAD")
        methods.add("OPTIONS")
        allow[path] = ", ".join(method for method in order if method in methods)
    return allow


def _render_bridge_output(plan: FrameworkPlan, output_path: Path, *, source_root: str) -> int:
    automatic = [route for route in plan.routes if route.automatic]
    manifest = {
        "schema_version": 1,
        "generator": "sanka",
        "mode": plan.mode,
        "source_scan_hash": plan.source_scan_hash,
        "plan_hash": plan.plan_hash,
        "settings_module": plan.settings_module,
        "generated_files": ["app.py", "sanka_compat.py"],
        "routes": [
            {
                "method": route.method,
                "path": route.path,
                "operation": route.operation,
                "source_view": route.source_view,
                "strategy": route.strategy,
            }
            for route in automatic
        ],
        "source_root": source_root,
    }
    _write_text(output_path / "app.py", _render_app())
    _write_text(output_path / "sanka_compat.py", _render_compatibility_runtime())
    _write_text(output_path / "README.md", _render_generated_readme(plan))
    _write_text(
        output_path / "requirements.txt",
        "fastapi>=0.115,<1\nuvicorn[standard]>=0.30,<1\n",
    )
    _write_json(output_path / GENERATED_MANIFEST, manifest)
    return len(automatic)


def _render_native_output(
    plan: FrameworkPlan,
    scan: FrameworkScan,
    output_path: Path,
    *,
    entrypoint: str,
    source_root: str,
) -> int:
    generated = [
        route
        for route in plan.routes
        if route.strategy in (ROUTE_STRATEGY_NATIVE_CRUD, ROUTE_STRATEGY_NATIVE_API_ROOT)
    ]
    if not generated:
        raise FrameworkMigrationError(
            "the native plan contains no generatable routes; nothing to apply"
        )
    dropped = [route for route in plan.routes if route.strategy == ROUTE_STRATEGY_DROPPED_ALIAS]
    scan_routes = {route.key: route for route in scan.routes}
    serializer_by_name = {item.name: item for item in scan.serializer_details}
    resources: dict[str, dict[str, Any]] = {}
    api_root_paths: set[str] = set()
    for planned in sorted(generated, key=lambda route: (route.path, route.method)):
        if planned.strategy == ROUTE_STRATEGY_NATIVE_API_ROOT:
            api_root_paths.add(planned.path)
            continue
        route = scan_routes[planned.key]
        if route.serializer is None or route.serializer not in serializer_by_name:
            raise FrameworkMigrationError(
                f"native route {planned.key} has no captured serializer; run `sanka scan`"
            )
        ir = serializer_by_name[route.serializer]
        resource = resources.setdefault(
            ir.name,
            {
                "serializer": ir.name,
                "model_module": ir.model_module,
                "model_class": ir.model_class,
                "object_name": ir.object_name,
                "ordering": list(ir.ordering),
                "lookup": ir.lookup,
                "fields": [
                    {
                        "name": field.name,
                        "kind": field.kind,
                        "required": field.required,
                        "read_only": field.read_only,
                        "allow_null": field.allow_null,
                        "allow_blank": field.allow_blank,
                        "trim_whitespace": field.trim_whitespace,
                        "max_length": field.max_length,
                        "min_length": field.min_length,
                        "min_value": field.min_value,
                        "max_value": field.max_value,
                        "has_default": field.has_default,
                        "default": field.default,
                        "messages": dict(field.messages),
                    }
                    for field in ir.fields
                ],
                "routes": [],
            },
        )
        resource["routes"].append(
            {"method": planned.method, "path": planned.path, "operation": planned.operation}
        )
    manifest = {
        "schema_version": 1,
        "generator": "sanka",
        "mode": plan.mode,
        "source_scan_hash": plan.source_scan_hash,
        "plan_hash": plan.plan_hash,
        "settings_module": plan.settings_module,
        "serving_settings": "sanka_settings",
        "entrypoint": entrypoint,
        "allow": _allow_headers(generated),
        "generic_messages": dict(scan.generic_messages),
        "generated_files": [entrypoint, "sanka_native.py", "sanka_settings.py"],
        "resources": [resources[name] for name in sorted(resources)],
        "api_roots": [
            {"path": root.path, "links": [list(link) for link in root.links]}
            for root in scan.api_roots
            if root.path in api_root_paths
        ],
        "routes": [
            {
                "method": route.method,
                "path": route.path,
                "operation": route.operation,
                "source_view": route.source_view,
                "strategy": route.strategy,
            }
            for route in sorted(generated, key=lambda route: (route.path, route.method))
        ],
        "dropped_routes": [
            {"method": route.method, "path": route.path, "reason": "format-suffix alias"}
            for route in sorted(dropped, key=lambda route: (route.path, route.method))
        ],
        "source_root": source_root,
    }
    _write_text(output_path / entrypoint, _render_native_app())
    _write_text(output_path / "sanka_native.py", _render_native_runtime())
    _write_text(output_path / "sanka_settings.py", _render_native_settings(plan.settings_module))
    _write_text(output_path / "README.md", _render_native_readme(plan))
    _write_text(
        output_path / "requirements.txt",
        "# Django is provided by the source application's own environment.\n"
        "fastapi>=0.115,<1\nuvicorn[standard]>=0.30,<1\n",
    )
    _write_json(output_path / GENERATED_MANIFEST, manifest)
    return len(generated)


def verify_fastapi_migration(
    root: str | Path = ".",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    output: str | Path | None = None,
    probe_http: bool = True,
    cases: str | Path | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    output_value = str(output) if output is not None else plan.default_output
    output_path = Path(output_value)
    if not output_path.is_absolute():
        output_path = root_path / output_path
    output_path = output_path.resolve()
    manifest = _read_json(output_path / GENERATED_MANIFEST, label="generated manifest")
    if manifest.get("source_scan_hash") != scan.scan_hash:
        raise FrameworkMigrationError("generated output does not match the current scan")
    if manifest.get("plan_hash") != plan.plan_hash:
        raise FrameworkMigrationError("generated output does not match the reviewed plan")
    expected = {
        route.key
        for route in plan.routes
        if route.automatic and route.strategy != ROUTE_STRATEGY_DROPPED_ALIAS
    }
    needs_adaptation = sorted(route.key for route in plan.routes if not route.automatic)
    dropped = sorted(
        route.key for route in plan.routes if route.strategy == ROUTE_STRATEGY_DROPPED_ALIAS
    )
    actual = {
        f"{str(route['method']).upper()} {route['path']}" for route in manifest.get("routes", [])
    }
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    _compile_generated_files(output_path, manifest)
    probes: list[dict[str, Any]] = []
    if probe_http and not missing and not extra:
        probes = _probe_read_only_routes(
            root_path,
            output_path,
            manifest,
            cases=_load_verification_cases(root_path, cases),
        )
    failed_probes = [probe for probe in probes if not probe["ok"]]
    return {
        "ok": not missing and not extra and not failed_probes and not needs_adaptation,
        "mode": plan.mode,
        "routes": {
            "scanned": len(scan.routes),
            "planned": len(plan.routes),
            "generated": len(actual),
            "missing": missing,
            "extra": extra,
            "needs_adaptation": needs_adaptation,
            "dropped": dropped,
        },
        "http": {
            "safe_routes": len(
                [
                    route
                    for route in manifest.get("routes", [])
                    if route.get("method") in {"GET", "HEAD"} and "{" not in route.get("path", "")
                ]
            ),
            "probed": len(probes),
            "passed": len(probes) - len(failed_probes),
            "failed": failed_probes,
        },
        "scan_hash": scan.scan_hash,
        "plan_hash": plan.plan_hash,
        "output": str(output_path),
    }


def _infer_settings_module(root: Path) -> str:
    manage = root / "manage.py"
    candidates = [manage] if manage.is_file() else []
    candidates.extend(path for path in root.glob("*/wsgi.py") if path.is_file())
    candidates.extend(path for path in root.glob("*/asgi.py") if path.is_file())
    for path in candidates:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "DJANGO_SETTINGS_MODULE" not in line:
                continue
            strings = re.findall(r"['\"]([^'\"]+)['\"]", line)
            values = [value for value in strings if value != "DJANGO_SETTINGS_MODULE"]
            if values:
                return str(values[-1])
    raise FrameworkMigrationError(
        "could not infer DJANGO_SETTINGS_MODULE; pass `sanka scan --settings your_project.settings`"
    )


def _bootstrap_django(root: Path, settings_module: str) -> tuple[ModuleType, ModuleType]:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    try:
        django = importlib.import_module("django")
        rest_framework = importlib.import_module("rest_framework")
    except ModuleNotFoundError as error:
        raise FrameworkMigrationError(
            "Django and djangorestframework must be installed in the project environment"
        ) from error
    django.setup()
    return django, rest_framework


class _WalkResult:
    def __init__(self) -> None:
        self.routes: list[RouteIR] = []
        self.risks: list[FrameworkRisk] = []
        self.serializer_details: dict[str, SerializerIR] = {}
        self.api_roots: list[ApiRootIR] = []


def _walk_patterns(
    patterns: Iterable[Any],
    *,
    root_path: Path,
    middleware: tuple[str, ...],
    prefix: str = "",
    collector: _WalkResult | None = None,
) -> _WalkResult:
    result = collector if collector is not None else _WalkResult()
    for pattern in patterns:
        raw = str(pattern.pattern)
        combined = f"{prefix}{raw}"
        nested = getattr(pattern, "url_patterns", None)
        if nested is not None:
            _walk_patterns(
                nested,
                root_path=root_path,
                middleware=middleware,
                prefix=combined,
                collector=result,
            )
            continue
        callback = getattr(pattern, "callback", None)
        view_class = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
        if callback is None or view_class is None or not _is_drf_view(view_class):
            continue
        path, supported = _to_fastapi_path(combined)
        source_file, source_line = _source_location(view_class, root_path)
        view_name = f"{view_class.__module__}.{view_class.__qualname__}"
        serializer = _qualified_name(getattr(view_class, "serializer_class", None))
        queryset = getattr(view_class, "queryset", None)
        model = _qualified_name(getattr(queryset, "model", None))
        permissions = tuple(
            value
            for item in getattr(view_class, "permission_classes", ())
            if (value := _qualified_name(item))
        )
        authentication = tuple(
            value
            for item in getattr(view_class, "authentication_classes", ())
            if (value := _qualified_name(item))
        )
        actions = getattr(callback, "actions", None)
        methods = _route_methods(view_class, actions)
        if not supported:
            result.risks.append(
                FrameworkRisk(
                    severity="high",
                    code="SANKA_DRF_DYNAMIC_ROUTE",
                    message=f"Route pattern requires manual adaptation: {combined}",
                    file=source_file,
                    line=source_line,
                )
            )
        native = _native_route_support(
            result,
            view_class=view_class,
            callback=callback,
            actions=actions,
            path=path,
            supported=supported,
            serializer_name=serializer,
            middleware=middleware,
        )
        for method, operation in methods:
            operation_source = _safe_source(getattr(view_class, operation, None))
            transactional = (
                "transaction.atomic" in operation_source or "@atomic" in operation_source
            )
            result.routes.append(
                RouteIR(
                    method=method,
                    path=path,
                    operation=operation,
                    view=view_name,
                    serializer=serializer,
                    model=model,
                    authentication=authentication,
                    permissions=permissions,
                    transactional=transactional,
                    source_file=source_file,
                    source_line=source_line,
                    supported=supported,
                    native=native,
                )
            )
    result.routes = list({route.key: route for route in result.routes}.values())
    return result


def _is_drf_view(view_class: type[Any]) -> bool:
    return any(base.__module__.startswith("rest_framework.") for base in inspect.getmro(view_class))


def _is_format_alias_path(path: str) -> bool:
    return "{format}" in path or "drf_format_suffix" in path


def _native_route_support(
    result: _WalkResult,
    *,
    view_class: type[Any],
    callback: Any,
    actions: dict[str, str] | None,
    path: str,
    supported: bool,
    serializer_name: str | None,
    middleware: tuple[str, ...],
) -> bool:
    """Decide whether a route sits inside the native-generation envelope.

    The envelope is deliberately narrow and checked against the live classes,
    not names: default-behavior ModelViewSet CRUD over a captured
    ModelSerializer, or the router API root. Anything else must be adapted by
    a human, never silently bridged in a native plan.
    """
    if not supported or middleware or _is_format_alias_path(path):
        return False
    routers = importlib.import_module("rest_framework.routers")
    if inspect.isclass(view_class) and issubclass(view_class, routers.APIRootView):
        links = _api_root_links(callback)
        if links is None:
            return False
        if all(root.path != path for root in result.api_roots):
            result.api_roots.append(ApiRootIR(path=path, links=links))
        return True
    if actions is None or not set(actions.values()) <= _SUPPORTED_VIEWSET_ACTIONS:
        return False
    viewsets = importlib.import_module("rest_framework.viewsets")
    if not issubclass(view_class, viewsets.ModelViewSet):
        return False
    if _has_viewset_overrides(view_class):
        return False
    if getattr(view_class, "lookup_field", "pk") != "pk":
        return False
    permissions_module = importlib.import_module("rest_framework.permissions")
    if any(item is not permissions_module.AllowAny for item in view_class.permission_classes):
        return False
    if getattr(view_class, "pagination_class", None) is not None:
        return False
    if tuple(getattr(view_class, "filter_backends", ())) != ():
        return False
    if tuple(getattr(view_class, "throttle_classes", ())) != ():
        return False
    if getattr(view_class, "versioning_class", None) is not None:
        return False
    if serializer_name is None:
        return False
    ir = result.serializer_details.get(serializer_name)
    if ir is None:
        ir = _serializer_ir(view_class, serializer_name)
        if ir is None:
            return False
        result.serializer_details[serializer_name] = ir
    return ir.supported


def _has_viewset_overrides(view_class: type[Any]) -> bool:
    generics = importlib.import_module("rest_framework.generics")
    mixins = importlib.import_module("rest_framework.mixins")
    expected = {
        "list": mixins.ListModelMixin.list,
        "create": mixins.CreateModelMixin.create,
        "perform_create": mixins.CreateModelMixin.perform_create,
        "retrieve": mixins.RetrieveModelMixin.retrieve,
        "update": mixins.UpdateModelMixin.update,
        "partial_update": mixins.UpdateModelMixin.partial_update,
        "perform_update": mixins.UpdateModelMixin.perform_update,
        "destroy": mixins.DestroyModelMixin.destroy,
        "perform_destroy": mixins.DestroyModelMixin.perform_destroy,
        "get_queryset": generics.GenericAPIView.get_queryset,
        "get_object": generics.GenericAPIView.get_object,
        "get_serializer": generics.GenericAPIView.get_serializer,
        "get_serializer_class": generics.GenericAPIView.get_serializer_class,
        "filter_queryset": generics.GenericAPIView.filter_queryset,
    }
    return any(getattr(view_class, name, None) is not func for name, func in expected.items())


def _api_root_links(callback: Any) -> tuple[tuple[str, str], ...] | None:
    initkwargs = getattr(callback, "view_initkwargs", None) or {}
    root_dict = initkwargs.get("api_root_dict") or {}
    if not root_dict:
        return None
    django_urls = importlib.import_module("django.urls")
    links: list[tuple[str, str]] = []
    for key, url_name in root_dict.items():
        try:
            links.append((str(key), str(django_urls.reverse(url_name))))
        except django_urls.NoReverseMatch:
            return None
    return tuple(sorted(links))


def _serializer_ir(view_class: type[Any], serializer_name: str) -> SerializerIR | None:
    serializers_module = importlib.import_module("rest_framework.serializers")
    serializer_class = view_class.serializer_class
    if not issubclass(serializer_class, serializers_module.ModelSerializer):
        return None
    queryset = view_class.queryset
    if queryset is None:
        return None
    model = queryset.model
    supported = not queryset.query.where.children
    lookup = str(
        getattr(view_class, "lookup_url_kwarg", None) or getattr(view_class, "lookup_field", "pk")
    )
    if _defines_custom_validation(serializer_class):
        supported = False
    fields: list[SerializerFieldIR] = []
    for name, field in serializer_class().fields.items():
        field_ir = _serializer_field_ir(str(name), field)
        fields.append(field_ir)
        supported = supported and field_ir.supported
    ordering = tuple(str(item) for item in (queryset.query.order_by or model._meta.ordering or ()))
    return SerializerIR(
        name=serializer_name,
        model=f"{model.__module__}.{model.__qualname__}",
        model_module=str(model.__module__),
        model_class=str(model.__qualname__),
        object_name=str(model._meta.object_name),
        ordering=ordering,
        lookup=lookup,
        fields=tuple(fields),
        supported=supported,
    )


def _generic_messages() -> tuple[tuple[str, str], ...]:
    """Capture framework-level error strings from the live DRF installation."""
    exceptions_module = importlib.import_module("rest_framework.exceptions")
    return (("not_found", str(exceptions_module.NotFound.default_detail)),)


def _defines_custom_validation(cls: type[Any]) -> bool:
    for klass in cls.__mro__:
        if klass.__module__.startswith("rest_framework."):
            continue
        for name in vars(klass):
            if name == "validate" or name.startswith("validate_"):
                return True
    return False


def _serializer_field_ir(name: str, field: Any) -> SerializerFieldIR:
    fields_module = importlib.import_module("rest_framework.fields")
    validators_module = importlib.import_module("django.core.validators")
    kind: str | None = None
    if type(field) is fields_module.IntegerField:
        kind = "integer"
    elif type(field) is fields_module.CharField:
        kind = "char"
    if kind is None:
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    drf_validators = importlib.import_module("rest_framework.validators")
    allowed_validators = (
        validators_module.MaxLengthValidator,
        validators_module.MinLengthValidator,
        validators_module.MaxValueValidator,
        validators_module.MinValueValidator,
        validators_module.ProhibitNullCharactersValidator,
        drf_validators.ProhibitSurrogateCharactersValidator,
    )
    supported = all(isinstance(item, allowed_validators) for item in field.validators)
    default = getattr(field, "default", fields_module.empty)
    has_default = default is not fields_module.empty
    if has_default and not isinstance(default, str | int | float | bool | type(None)):
        supported = False
        default = None
    return SerializerFieldIR(
        name=name,
        kind=kind,
        required=bool(field.required),
        read_only=bool(field.read_only),
        allow_null=bool(field.allow_null),
        allow_blank=bool(getattr(field, "allow_blank", False)),
        trim_whitespace=bool(getattr(field, "trim_whitespace", True)),
        max_length=_maybe_int(getattr(field, "max_length", None)),
        min_length=_maybe_int(getattr(field, "min_length", None)),
        min_value=_maybe_int(getattr(field, "min_value", None)),
        max_value=_maybe_int(getattr(field, "max_value", None)),
        has_default=has_default,
        default=default if has_default else None,
        messages=_field_messages(field, kind),
        supported=supported,
    )


def _maybe_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


_MESSAGE_KEYS = {
    "integer": ("required", "null", "invalid", "min_value", "max_value", "max_string_length"),
    "char": (
        "required",
        "null",
        "invalid",
        "blank",
        "max_length",
        "min_length",
        "null_characters",
        "surrogate_characters",
    ),
}


def _field_messages(field: Any, kind: str) -> tuple[tuple[str, str], ...]:
    """Render the exact error strings DRF would emit for this field."""
    validators_module = importlib.import_module("django.core.validators")
    params = {
        key: value
        for key in ("min_value", "max_value", "max_length", "min_length")
        if (value := getattr(field, key, None)) is not None
    }
    rendered: dict[str, str] = {}
    for key in _MESSAGE_KEYS[kind]:
        template = str(field.error_messages.get(key, ""))
        if not template:
            continue
        try:
            rendered[key] = template.format(**params)
        except (IndexError, KeyError):
            rendered[key] = template
    drf_validators = importlib.import_module("rest_framework.validators")
    for validator in getattr(field, "validators", ()):
        if isinstance(validator, validators_module.ProhibitNullCharactersValidator):
            rendered["null_characters"] = str(validator.message)
        elif isinstance(validator, drf_validators.ProhibitSurrogateCharactersValidator):
            rendered["surrogate_characters"] = str(validator.message)
    return tuple(sorted(rendered.items()))


def _route_methods(view_class: type[Any], actions: dict[str, str] | None) -> list[tuple[str, str]]:
    if actions:
        return sorted((method.upper(), action) for method, action in actions.items())
    methods: list[tuple[str, str]] = []
    for method in getattr(view_class, "http_method_names", ()):
        if method in {"options", "head", "trace"}:
            continue
        if callable(getattr(view_class, method, None)):
            methods.append((method.upper(), method))
    return methods


def _to_fastapi_path(raw: str) -> tuple[str, bool]:
    value = raw.strip()
    value = value.removesuffix("$").removesuffix(r"\Z").replace("^", "")
    value = re.sub(r"\(\?P<([A-Za-z_][A-Za-z0-9_]*)>[^)]+\)", r"{\1}", value)
    value = re.sub(
        r"<(?:(?:str|int|slug|uuid|path):)?([A-Za-z_][A-Za-z0-9_]*)>",
        r"{\1}",
        value,
    )
    value = value.replace(r"\/", "/").replace(r"\.", ".")
    value = value.replace("/?", "/")
    value = re.sub(r"\(\?:([^()]+)\)", r"\1", value)
    supported = re.search(r"[\[\]()+*?|\\^$]", value) is None
    path = "/" + value.lstrip("/")
    path = re.sub(r"/{2,}", "/", path)
    return path, supported


def _source_location(view_class: type[Any], root: Path) -> tuple[str | None, int | None]:
    try:
        path = Path(inspect.getsourcefile(view_class) or "").resolve()
        relative = str(path.relative_to(root))
        _, line = inspect.getsourcelines(view_class)
        return relative, line
    except (OSError, TypeError, ValueError):
        return None, None


def _qualified_name(value: Any) -> str | None:
    if value is None:
        return None
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    if module and name:
        return f"{module}.{name}"
    return str(value)


def _safe_source(value: Any) -> str:
    try:
        return inspect.getsource(value)
    except (OSError, TypeError):
        return ""


def _count_test_files(root: Path) -> int:
    ignored = {".git", ".sanka", ".tox", ".venv", "node_modules", "site-packages", "venv"}
    return sum(
        1
        for path in root.rglob("*.py")
        if not ignored.intersection(path.relative_to(root).parts)
        if path.name.startswith("test_") or path.name.endswith("_test.py")
    )


def _artifact_path(root: Path, artifact_dir: str | Path, name: str) -> Path:
    directory = Path(artifact_dir)
    if not directory.is_absolute():
        directory = root / directory
    return directory / name


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FrameworkMigrationError(f"{label} artifact not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise FrameworkMigrationError(f"could not read {label} artifact: {path}") from error
    if not isinstance(value, dict):
        raise FrameworkMigrationError(f"{label} artifact must be a JSON object: {path}")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _compile_generated_files(output: Path, manifest: dict[str, Any]) -> None:
    names = [
        str(name)
        for name in manifest.get("generated_files", ["app.py", "sanka_compat.py"])
        if str(name).endswith(".py")
    ]
    for name in names:
        path = output / name
        if not path.is_file():
            raise FrameworkMigrationError(f"generated file is missing: {path}")
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as error:
            raise FrameworkMigrationError(
                f"generated Python is invalid: {path}: {error}"
            ) from error


def _load_generated_app(output: Path) -> Any:
    module_name = f"_sanka_generated_{abs(hash(output))}"
    spec = importlib.util.spec_from_file_location(module_name, output / "app.py")
    if spec is None or spec.loader is None:
        raise FrameworkMigrationError("could not load the generated FastAPI application")
    if str(output) not in sys.path:
        sys.path.insert(0, str(output))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def _probe_read_only_routes(
    root: Path,
    output: Path,
    manifest: dict[str, Any],
    *,
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    _bootstrap_django(root, str(manifest["settings_module"]))
    try:
        django_test = importlib.import_module("django.test")
        fastapi_testclient = importlib.import_module("fastapi.testclient")
    except ModuleNotFoundError as error:
        raise FrameworkMigrationError(
            "FastAPI must be installed to run HTTP verification (`pip install sanka-migrate`)"
        ) from error
    source = django_test.Client()
    target = fastapi_testclient.TestClient(_load_generated_app(output))
    results: list[dict[str, Any]] = []
    automatic = [
        {"method": route.get("method"), "path": route.get("path"), "headers": {}}
        for route in manifest.get("routes", [])
        if route.get("method") in {"GET", "HEAD"} and "{" not in route.get("path", "")
    ]
    probes = automatic + cases
    seen: set[tuple[str, str, str]] = set()
    for case in probes:
        method = str(case.get("method", "GET")).upper()
        path = str(case.get("path", ""))
        headers = {str(key): str(value) for key, value in dict(case.get("headers", {})).items()}
        identity = (method, path, json.dumps(headers, sort_keys=True))
        if identity in seen:
            continue
        seen.add(identity)
        django_headers = {
            "HTTP_" + key.upper().replace("-", "_"): value
            for key, value in headers.items()
            if key.lower() not in {"content-type", "content-length"}
        }
        source_response = source.generic(method, path, **django_headers)
        target_response = target.request(method, path, headers=headers)
        source_type = str(source_response.get("Content-Type", "")).split(";", 1)[0]
        target_type = str(target_response.headers.get("content-type", "")).split(";", 1)[0]
        source_body = bytes(source_response.content)
        target_body = target_response.content
        bodies_match = _response_bodies_match(source_body, target_body, source_type, target_type)
        compared_headers = ("allow", "location", "www-authenticate")
        headers_match = all(
            str(source_response.get(header, "")) == str(target_response.headers.get(header, ""))
            for header in compared_headers
        )
        ok = (
            source_response.status_code == target_response.status_code
            and source_type == target_type
            and bodies_match
            and headers_match
        )
        results.append(
            {
                "method": method,
                "path": path,
                "ok": ok,
                "source_status": source_response.status_code,
                "target_status": target_response.status_code,
                "source_content_type": source_type,
                "target_content_type": target_type,
                "headers_match": headers_match,
            }
        )
    return results


def _load_verification_cases(root: Path, value: str | Path | None) -> list[dict[str, Any]]:
    path = Path(value) if value is not None else root / DEFAULT_ARTIFACT_DIR / "verify-cases.json"
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return []
    payload = _read_json(path, label="verification cases")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise FrameworkMigrationError("verification cases must contain a `cases` array")
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            raise FrameworkMigrationError(f"verification case {index} must be an object")
        method = str(item.get("method", "GET")).upper()
        path_value = str(item.get("path", ""))
        if method not in {"GET", "HEAD", "OPTIONS"}:
            raise FrameworkMigrationError(
                f"verification case {index} uses mutating method {method};"
                " automatic differential verification is read-only"
            )
        if not path_value.startswith("/") or "{" in path_value:
            raise FrameworkMigrationError(
                f"verification case {index} must use a concrete absolute path"
            )
        headers = item.get("headers", {})
        if not isinstance(headers, dict):
            raise FrameworkMigrationError(f"verification case {index} headers must be an object")
        cases.append({"method": method, "path": path_value, "headers": headers})
    return cases


def _response_bodies_match(
    source: bytes, target: bytes, source_type: str, target_type: str
) -> bool:
    if source_type == target_type == "application/json":
        try:
            return bool(json.loads(source) == json.loads(target))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return source == target
    return source == target


def _render_app() -> str:
    return """# Generated by Sanka. Replace bridge routes with native FastAPI handlers.
from sanka_compat import create_app

app = create_app()
"""


def _render_native_app() -> str:
    return """# Generated by Sanka. Native FastAPI request layer over the retained Django ORM.
from sanka_native import create_app

app = create_app()
"""


def _render_native_settings(settings_module: str) -> str:
    return f'''# Generated by Sanka under the license selected for this generated application.
"""Serving settings: the original settings without the DRF request layer."""

from {settings_module} import *  # noqa: F401,F403

INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "rest_framework"]  # noqa: F405
'''


def _render_native_runtime() -> str:
    return '''# Generated by Sanka under the license selected for this generated application.
"""Native FastAPI request layer generated from a reviewed Sanka plan.

HTTP is served by FastAPI alone. Django is configured with generated DRF-free
settings and retained for the ORM only. Validation below is a native
reimplementation of the serializer semantics captured at scan time, using the
exact error strings the source application produced.
"""
from __future__ import annotations

import json
import os
import re
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "sanka-manifest.json").read_text(encoding="utf-8"))
SOURCE_ROOT = (HERE / MANIFEST["source_root"]).resolve()
for _entry in (str(HERE), str(SOURCE_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
os.environ["DJANGO_SETTINGS_MODULE"] = MANIFEST["serving_settings"]

import django
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

django.setup()

# Handlers are synchronous on purpose: FastAPI runs them in a worker thread,
# which keeps the retained Django ORM outside the event loop.
_DECIMAL_TAIL = re.compile(r"\\.0*\\s*$")
_MAX_STRING_LENGTH = 1000
_MODEL_CACHE: dict[tuple[str, str], Any] = {}


def _model(resource: dict[str, Any]) -> Any:
    key = (resource["model_module"], resource["model_class"])
    if key not in _MODEL_CACHE:
        module = import_module(resource["model_module"])
        _MODEL_CACHE[key] = getattr(module, resource["model_class"])
    return _MODEL_CACHE[key]


def _serialize(resource: dict[str, Any], instance: Any) -> dict[str, Any]:
    return {spec["name"]: getattr(instance, spec["name"]) for spec in resource["fields"]}


def _clean_integer(spec: dict[str, Any], value: Any) -> tuple[Any, list[str]]:
    messages = spec["messages"]
    if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
        return None, [messages["max_string_length"]]
    try:
        cleaned = int(_DECIMAL_TAIL.sub("", str(value).strip()))
    except (TypeError, ValueError):
        return None, [messages["invalid"]]
    errors = []
    if spec.get("min_value") is not None and cleaned < spec["min_value"]:
        errors.append(messages["min_value"])
    if spec.get("max_value") is not None and cleaned > spec["max_value"]:
        errors.append(messages["max_value"])
    return cleaned, errors


def _clean_char(spec: dict[str, Any], value: Any) -> tuple[Any, list[str]]:
    messages = spec["messages"]
    if value == "" or (spec["trim_whitespace"] and isinstance(value, str) and not value.strip()):
        if spec["allow_blank"]:
            return "", []
        return None, [messages["blank"]]
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None, [messages["invalid"]]
    cleaned = str(value)
    if spec["trim_whitespace"]:
        cleaned = cleaned.strip()
    errors = []
    if spec.get("max_length") is not None and len(cleaned) > spec["max_length"]:
        errors.append(messages["max_length"])
    if spec.get("min_length") is not None and len(cleaned) < spec["min_length"]:
        errors.append(messages["min_length"])
    if "\\x00" in cleaned and messages.get("null_characters"):
        errors.append(messages["null_characters"])
    if messages.get("surrogate_characters") and any(
        0xD800 <= ord(char) <= 0xDFFF for char in cleaned
    ):
        errors.append(messages["surrogate_characters"])
    return cleaned, errors


_CLEANERS = {"integer": _clean_integer, "char": _clean_char}


def _validate(
    fields: list[dict[str, Any]], payload: Any, *, partial: bool
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    if not isinstance(payload, dict):
        message = f"Invalid data. Expected a dictionary, but got {type(payload).__name__}."
        return {}, {"non_field_errors": [message]}
    errors: dict[str, list[str]] = {}
    validated: dict[str, Any] = {}
    for spec in fields:
        if spec["read_only"]:
            continue
        name = spec["name"]
        if name not in payload:
            if partial:
                continue
            if spec["has_default"]:
                validated[name] = spec["default"]
            elif spec["required"]:
                errors[name] = [spec["messages"]["required"]]
            continue
        raw = payload[name]
        if raw is None:
            if spec["allow_null"]:
                validated[name] = None
            else:
                errors[name] = [spec["messages"]["null"]]
            continue
        cleaned, field_errors = _CLEANERS[spec["kind"]](spec, raw)
        if field_errors:
            errors[name] = field_errors
        else:
            validated[name] = cleaned
    return validated, errors


async def _read_raw_body(request: Request) -> bytes:
    return await request.body()


def _parse_json(raw: bytes) -> tuple[Any, Response | None]:
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, JSONResponse({"detail": f"JSON parse error - {exc}"}, status_code=400)


def _not_found(resource: dict[str, Any], allow: str, cause: str) -> Response:
    # DRF distinguishes a real miss (Http404 carries the model's message) from
    # an invalid lookup value (bare Http404 -> the generic NotFound detail).
    if cause == "missing":
        detail = f"No {resource['object_name']} matches the given query."
    else:
        detail = MANIFEST["generic_messages"]["not_found"]
    return JSONResponse({"detail": detail}, status_code=404, headers={"Allow": allow})


def _get_instance(resource: dict[str, Any], request: Request) -> tuple[Any, str]:
    model = _model(resource)
    raw = request.path_params.get(resource["lookup"])
    try:
        return model.objects.get(pk=raw), ""
    except model.DoesNotExist:
        return None, "missing"
    except (ValueError, TypeError, OverflowError):
        return None, "invalid"


def _crud_handler(resource: dict[str, Any], operation: str, allow: str) -> Any:
    if operation == "list":

        def handler(request: Request) -> Response:
            queryset = _model(resource).objects.all()
            if resource["ordering"]:
                queryset = queryset.order_by(*resource["ordering"])
            payload = [_serialize(resource, item) for item in queryset]
            return JSONResponse(payload, headers={"Allow": allow})

    elif operation == "create":

        def handler(request: Request, raw_body: bytes = Depends(_read_raw_body)) -> Response:
            payload, parse_error = _parse_json(raw_body)
            if parse_error is not None:
                parse_error.headers["Allow"] = allow
                return parse_error
            validated, errors = _validate(resource["fields"], payload, partial=False)
            if errors:
                return JSONResponse(errors, status_code=400, headers={"Allow": allow})
            instance = _model(resource).objects.create(**validated)
            return JSONResponse(
                _serialize(resource, instance), status_code=201, headers={"Allow": allow}
            )

    elif operation == "retrieve":

        def handler(request: Request) -> Response:
            instance, miss = _get_instance(resource, request)
            if instance is None:
                return _not_found(resource, allow, miss)
            return JSONResponse(_serialize(resource, instance), headers={"Allow": allow})

    elif operation in ("update", "partial_update"):
        partial = operation == "partial_update"

        def handler(request: Request, raw_body: bytes = Depends(_read_raw_body)) -> Response:
            instance, miss = _get_instance(resource, request)
            if instance is None:
                return _not_found(resource, allow, miss)
            payload, parse_error = _parse_json(raw_body)
            if parse_error is not None:
                parse_error.headers["Allow"] = allow
                return parse_error
            validated, errors = _validate(resource["fields"], payload, partial=partial)
            if errors:
                return JSONResponse(errors, status_code=400, headers={"Allow": allow})
            for name, value in validated.items():
                setattr(instance, name, value)
            instance.save()
            return JSONResponse(_serialize(resource, instance), headers={"Allow": allow})

    elif operation == "destroy":

        def handler(request: Request) -> Response:
            instance, miss = _get_instance(resource, request)
            if instance is None:
                return _not_found(resource, allow, miss)
            instance.delete()
            return Response(status_code=204, headers={"Allow": allow})

    else:
        raise RuntimeError(f"unsupported generated operation: {operation}")
    return handler


def _api_root_handler(links: list[list[str]], allow: str) -> Any:
    def handler(request: Request) -> Response:
        base = str(request.base_url).rstrip("/")
        payload = {key: f"{base}{path}" for key, path in links}
        return JSONResponse(payload, headers={"Allow": allow})

    return handler


def create_app() -> FastAPI:
    app = FastAPI(title="Sanka native FastAPI application")
    index = 0
    for resource in MANIFEST["resources"]:
        for route in resource["routes"]:
            allow = MANIFEST["allow"][route["path"]]
            handler = _crud_handler(resource, route["operation"], allow)
            handler.__name__ = f"sanka_native_{route['operation']}_{index}"
            app.add_api_route(
                route["path"],
                handler,
                methods=[route["method"]],
                operation_id=handler.__name__,
            )
            index += 1
    for root in MANIFEST["api_roots"]:
        handler = _api_root_handler(root["links"], MANIFEST["allow"][root["path"]])
        handler.__name__ = f"sanka_native_api_root_{index}"
        app.add_api_route(root["path"], handler, methods=["GET"], operation_id=handler.__name__)
        index += 1
    return app
'''


def _render_native_readme(plan: FrameworkPlan) -> str:
    return f"""# Generated native FastAPI application

Sanka generated this application from plan `{plan.plan_hash}`.

FastAPI owns the request layer. Django is configured through the generated
`sanka_settings` module, which removes the DRF request layer and keeps the
original models, migrations, ORM, and synchronous transactions. Validation is
a native reimplementation of the serializer semantics captured at scan time,
including the exact error strings.

Format-suffix alias routes from the source router are dropped as a disclosed
contract change; clients negotiate content types with headers instead.

Run locally from the Django repository root:

```bash
python -m pip install -r {plan.default_output}/requirements.txt
uvicorn --app-dir {plan.default_output} app:app --reload
```
"""


def _render_compatibility_runtime() -> str:
    return """# Generated by Sanka under the license selected for this generated application.
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import django
from django.core.asgi import get_asgi_application
from fastapi import FastAPI, Request
from fastapi.responses import Response

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "sanka-manifest.json").read_text(encoding="utf-8"))
SOURCE_ROOT = (HERE / MANIFEST["source_root"]).resolve()
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
os.environ["DJANGO_SETTINGS_MODULE"] = MANIFEST["settings_module"]
django.setup()
DJANGO_APP = get_asgi_application()

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


async def _dispatch(request: Request, method: str) -> Response:
    body = await request.body()
    scope = dict(request.scope)
    scope["method"] = method
    scope["root_path"] = ""
    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await DJANGO_APP(scope, receive, send)
    start = next(
        (message for message in messages if message["type"] == "http.response.start"),
        None,
    )
    if start is None:
        return Response(content=b"", status_code=500)
    content = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response = Response(content=content, status_code=start["status"])
    response.raw_headers = [
        (key, value)
        for key, value in start.get("headers", [])
        if key.decode("latin-1").lower() not in HOP_BY_HOP
        and key.decode("latin-1").lower() != "content-length"
    ]
    return response


def create_app() -> FastAPI:
    app = FastAPI(title="Sanka DRF to FastAPI compatibility application")
    for index, route in enumerate(MANIFEST["routes"]):
        method = route["method"]

        def make_handler(route_method: str):
            async def handler(request: Request) -> Response:
                return await _dispatch(request, route_method)

            return handler

        handler = make_handler(method)
        handler.__name__ = "sanka_" + route["operation"] + "_" + str(index)
        app.add_api_route(
            route["path"],
            handler,
            methods=[method],
            operation_id=handler.__name__,
            tags=["Sanka compatibility bridge"],
        )
    return app
"""


def _render_generated_readme(plan: FrameworkPlan) -> str:
    return f"""# Generated FastAPI compatibility application

Sanka generated this application from plan `{plan.plan_hash}`.

It is a **compatibility bridge**, not a claim that Django REST Framework has
already been removed. FastAPI owns the generated route graph and forwards each
request into the existing Django application in-process so observable behavior
stays stable. Replace bridge routes with native FastAPI handlers incrementally,
keeping `sanka verify` green after each replacement.

Run locally from the Django repository root:

```bash
python -m pip install -r {plan.default_output}/requirements.txt
uvicorn --app-dir {plan.default_output} app:app --reload
```

The generated application retains Django models, migrations, ORM,
authentication, permissions, and synchronous transaction handlers.
"""
