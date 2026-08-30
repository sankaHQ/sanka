# SPDX-License-Identifier: AGPL-3.0-only
"""Django REST Framework to FastAPI migration recipes.

Two strategies share one scan artifact:

- ``native`` (default): generate a genuinely native async FastAPI request
  layer for the supported DRF envelope (ModelViewSet CRUD over
  ModelSerializer fields whose semantics the scan captured, plus the router
  API root). Persistence is async SQL (Tortoise by default; SQLAlchemy or
  psycopg on request) against the existing Django tables. Django is not
  imported at serve time. Format-suffix alias routes are dropped as a
  disclosed contract change, and routes outside the envelope are reported
  as needing manual adaptation.
- ``compatibility``: the strangler bridge from v0.1. It creates a real FastAPI
  route graph while dispatching each route into the existing Django
  application in-process, preserving observable behavior for the whole route
  surface at the cost of still serving through DRF.

A compatibility bridge must never support the claim that DRF was replaced;
``sanka verify`` and Sanka Migration Bench treat only the native strategy as a
completed migration.
"""

from __future__ import annotations

import ast
import base64
import importlib
import importlib.util
import inspect
import json
import keyword
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import replace as replace_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from sanka.runtime.frameworks.generated_environment import (
    GeneratedEnvironment,
    ensure_generated_environment,
)
from sanka.runtime.frameworks.generated_integrity import (
    GeneratedIntegrityError,
    attest_generated_bundle,
    freeze_generated_bundle,
)
from sanka.runtime.frameworks.model import (
    ApiRootIR,
    DatabaseIR,
    FrameworkPlan,
    FrameworkRisk,
    FrameworkScan,
    PlannedRoute,
    RouteAdaptationReason,
    RouteIR,
    SerializerFieldIR,
    SerializerIR,
    SkippedRoute,
    ViewAuthIR,
    ViewIR,
)
from sanka.runtime.frameworks.native_async import (
    SQL_ENGINE_LABELS,
    render_async_sql_files,
    render_generated_pyproject,
    resolve_sql_engine,
)
from sanka.runtime.frameworks.untrusted_framework import (
    UntrustedFrameworkError,
    run_untrusted_framework_worker,
)
from sanka.runtime.safe_local_io import (
    absolute_path,
    ensure_safe_directory,
    safe_read_text,
    safe_write_text,
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

_KNOWN_SAFE_MIDDLEWARE = frozenset(
    {
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.middleware.security.SecurityMiddleware",
    }
)


class FrameworkMigrationError(RuntimeError):
    """Raised when a framework migration cannot proceed safely."""


def _capture_http_security(settings: Any, middleware: tuple[str, ...]) -> dict[str, Any]:
    security_middleware = "django.middleware.security.SecurityMiddleware" in middleware
    frame_middleware = "django.middleware.clickjacking.XFrameOptionsMiddleware" in middleware
    return {
        "allowed_hosts": [str(host) for host in settings.ALLOWED_HOSTS],
        "ssl_redirect": bool(security_middleware and settings.SECURE_SSL_REDIRECT),
        "content_type_nosniff": bool(security_middleware and settings.SECURE_CONTENT_TYPE_NOSNIFF),
        "referrer_policy": (
            str(settings.SECURE_REFERRER_POLICY)
            if security_middleware and settings.SECURE_REFERRER_POLICY
            else None
        ),
        "cross_origin_opener_policy": (
            str(settings.SECURE_CROSS_ORIGIN_OPENER_POLICY)
            if security_middleware and settings.SECURE_CROSS_ORIGIN_OPENER_POLICY
            else None
        ),
        "hsts_seconds": int(settings.SECURE_HSTS_SECONDS) if security_middleware else 0,
        "hsts_include_subdomains": bool(
            security_middleware and settings.SECURE_HSTS_INCLUDE_SUBDOMAINS
        ),
        "hsts_preload": bool(security_middleware and settings.SECURE_HSTS_PRELOAD),
        "x_frame_options": str(settings.X_FRAME_OPTIONS) if frame_middleware else None,
    }


def scan_django(
    root: str | Path = ".",
    *,
    settings_module: str | None = None,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    allow_unsafe_source_execution: bool = False,
) -> FrameworkScan:
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise FrameworkMigrationError(f"source root is not a directory: {root_path}")
    selected_settings = settings_module or _infer_settings_module(root_path)
    try:
        payload = run_untrusted_framework_worker(
            {
                "operation": "scan",
                "root": str(root_path),
                "settings": selected_settings,
            },
            readable_roots=[root_path],
            allow_unsafe=allow_unsafe_source_execution,
        )
        scan_payload = payload["scan"]
        if not isinstance(scan_payload, dict):
            raise TypeError("scan payload is not an object")
        scan = FrameworkScan.from_dict(scan_payload)
        _validate_untrusted_scan(scan, expected_settings=selected_settings)
    except (KeyError, TypeError, ValueError, UntrustedFrameworkError) as error:
        raise FrameworkMigrationError(str(error)) from error
    # A dynamic source process is not an authenticity boundary. Discard its
    # claimed hash and any source-bearing serializer fields before the trusted
    # parent persists the IR or permits it to reach code generation.
    scan = _strip_executable_scan_fields(scan)
    _write_json(_artifact_path(root_path, artifact_dir, SCAN_FILE), scan.to_dict())
    return scan


_PYTHON_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTTED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_MAX_SCAN_ITEMS = 100_000
_MAX_SCAN_STRING = 65_536


def _validate_untrusted_scan(scan: FrameworkScan, *, expected_settings: str) -> None:
    """Validate worker data as untrusted IR before it can reach code generation."""

    if scan.settings_module != expected_settings:
        raise ValueError("isolated Django scan returned a different settings module")
    if scan.source != "." or scan.framework != "django-rest-framework":
        raise ValueError("isolated Django scan returned an unexpected framework identity")
    collections = (
        scan.routes,
        scan.serializers,
        scan.models,
        scan.permissions,
        scan.authentication,
        scan.serializer_details,
        scan.api_roots,
        scan.view_details,
        scan.middleware,
        scan.skipped_routes,
    )
    if any(len(items) > _MAX_SCAN_ITEMS for items in collections):
        raise ValueError("isolated Django scan exceeds the item-count safety limit")

    def bounded(value: Any, label: str) -> str:
        text = str(value)
        if len(text.encode("utf-8")) > _MAX_SCAN_STRING or "\x00" in text:
            raise ValueError(f"isolated Django scan contains an unsafe {label}")
        return text

    def identifier(value: Any, label: str, *, dotted: bool = False) -> str:
        text = bounded(value, label)
        pattern = _DOTTED_IDENTIFIER if dotted else _PYTHON_IDENTIFIER
        if not pattern.fullmatch(text) or any(keyword.iskeyword(part) for part in text.split(".")):
            raise ValueError(f"isolated Django scan contains an unsafe {label}: {text!r}")
        return text

    identifier(scan.settings_module, "settings module", dotted=True)
    identifier(scan.root_urlconf, "root URL configuration", dotted=True)
    for route in scan.routes:
        if route.method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("isolated Django scan contains an unsupported HTTP method")
        if not bounded(route.path, "route path").startswith("/"):
            raise ValueError("isolated Django scan contains an unsafe route path")
        operation = identifier(route.operation, "route operation")
        identifier(route.view, "route view", dotted=True)
        if route.native and operation not in {*_SUPPORTED_VIEWSET_ACTIONS, "get"}:
            raise ValueError("isolated Django scan contains an unsupported native operation")
    for serializer in scan.serializer_details:
        if serializer.create_style not in {"default", "carryover"}:
            raise ValueError("isolated Django scan contains an unsupported create style")
        if serializer.create_source is not None or serializer.create_imports:
            raise ValueError("isolated Django scan returned executable serializer source")
        identifier(serializer.model_module, "model module", dotted=True)
        identifier(serializer.model_class, "model class", dotted=True)
        identifier(serializer.pk_attname, "primary-key attribute")
        identifier(serializer.lookup, "lookup attribute")
        names: set[str] = set()
        for field in serializer.fields:
            identifier(field.name, "serializer field")
            column = identifier(field.attname or field.name, "model field attribute")
            if column in names:
                raise ValueError("isolated Django scan contains colliding model field attributes")
            names.add(column)
            if field.child is not None:
                _validate_untrusted_scan(
                    replace_dataclass(
                        scan,
                        serializer_details=(field.child,),
                        routes=(),
                        api_roots=(),
                        view_details=(),
                        skipped_routes=(),
                    ),
                    expected_settings=expected_settings,
                )
        for alias, module, attribute in serializer.create_imports:
            identifier(alias, "create import alias")
            identifier(module, "create import module", dotted=True)
            if attribute is not None:
                identifier(attribute, "create import attribute")
    for view in scan.view_details:
        bounded(view.name, "view name")
        auth = view.auth
        if auth is None:
            continue
        for value, label in (
            (auth.token_key_column, "token key column"),
            (auth.token_user_column, "token user column"),
            (auth.owner_field, "owner field"),
            (auth.owner_attname, "owner attribute"),
            (auth.inject_owner, "injected owner field"),
            (auth.inject_owner_attname, "injected owner attribute"),
        ):
            if value is not None:
                identifier(value, label)


def _strip_executable_scan_fields(scan: FrameworkScan) -> FrameworkScan:
    """Return parent-owned IR that cannot contain executable source fragments."""

    def sanitize_serializer(serializer: SerializerIR) -> SerializerIR:
        fields = tuple(
            replace_dataclass(
                field,
                child=sanitize_serializer(field.child) if field.child is not None else None,
            )
            for field in serializer.fields
        )
        return replace_dataclass(
            serializer,
            fields=fields,
            create_source=None,
            create_imports=(),
        )

    return replace_dataclass(
        scan,
        serializer_details=tuple(sanitize_serializer(item) for item in scan.serializer_details),
        scan_hash="",
    ).with_hash()


def _scan_django_in_process(root_path: Path, selected_settings: str) -> FrameworkScan:
    """Worker-only implementation. Call :func:`scan_django` from trusted code."""

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
        schema_version=4,
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
        view_details=tuple(walk.view_details[name] for name in sorted(walk.view_details)),
        middleware=middleware,
        http_security=_capture_http_security(django_conf.settings, middleware),
        generic_messages=_generic_messages(),
        database=_capture_database(root_path),
        skipped_routes=tuple(
            sorted(walk.skipped_routes, key=lambda item: (item.pattern, item.view))
        ),
    ).with_hash()
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
    sql_engine: str | None = None,
) -> FrameworkPlan:
    if strategy not in (NATIVE_STRATEGY, COMPATIBILITY_STRATEGY):
        raise FrameworkMigrationError(f"unknown plan strategy: {strategy}")
    try:
        engine = resolve_sql_engine(sql_engine)
    except ValueError as error:
        raise FrameworkMigrationError(str(error)) from error
    root_path = Path(root).resolve()
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    if strategy == NATIVE_STRATEGY:
        if engine == "psycopg" and scan.database.vendor != "postgresql":
            raise FrameworkMigrationError(
                "psycopg requires PostgreSQL; this project's database is "
                + (scan.database.vendor or "unknown")
            )
        routes = tuple(
            _plan_native_route(route, middleware=scan.middleware) for route in scan.routes
        )
        retained = (
            "Existing Django tables (schema reused, not rewritten)",
            f"Async SQL via {SQL_ENGINE_LABELS[engine]}",
            "DRF removed; FastAPI async request layer (Django is not imported at serve time)",
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
        schema_version=2,
        source_framework=scan.framework,
        target_framework="fastapi",
        mode=strategy,
        source_scan_hash=scan.scan_hash,
        settings_module=scan.settings_module,
        routes=routes,
        risks=scan.risks,
        retained=retained,
        default_output=output,
        sql_engine=engine,
    ).with_hash()
    _write_json(_artifact_path(root_path, artifact_dir, PLAN_FILE), plan.to_dict())
    return plan


def _plan_native_route(route: RouteIR, *, middleware: tuple[str, ...] = ()) -> PlannedRoute:
    adaptation_reasons: tuple[RouteAdaptationReason, ...] = ()
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
        adaptation_reasons = route.adaptation_reasons or _legacy_adaptation_reasons(
            route, middleware=middleware
        )
    return PlannedRoute(
        method=route.method,
        path=route.path,
        operation=route.operation,
        source_view=route.view,
        strategy=strategy,
        automatic=automatic,
        adaptation_reasons=adaptation_reasons,
    )


def _legacy_adaptation_reasons(
    route: RouteIR, *, middleware: tuple[str, ...]
) -> tuple[RouteAdaptationReason, ...]:
    """Explain older scan artifacts that predate per-route reasons."""
    if not route.supported:
        return (
            _adaptation_reason(
                "SANKA_DRF_ROUTE_PATTERN_UNSUPPORTED",
                "route-pattern",
                "The route pattern cannot be represented safely as a FastAPI path.",
            ),
        )
    unsupported_middleware = _unsupported_middleware(middleware)
    if unsupported_middleware:
        return (_middleware_adaptation_reason(unsupported_middleware),)
    return (
        _adaptation_reason(
            "SANKA_DRF_NATIVE_DETAIL_RESCAN_REQUIRED",
            "scan-artifact",
            "This older scan did not record the native disqualifier; run `sanka scan` again.",
        ),
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
    plan_hash: str,
    force: bool = False,
    sql_engine: str | None = None,
) -> tuple[Path, int]:
    root_path = Path(root).resolve()
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    if not plan_hash:
        raise FrameworkMigrationError("a nonempty reviewed FastAPI plan hash is required")
    if plan_hash != plan.plan_hash:
        raise FrameworkMigrationError(
            f"reviewed plan hash {plan_hash!r} does not match current plan {plan.plan_hash!r}"
        )
    try:
        engine = resolve_sql_engine(sql_engine or plan.sql_engine)
    except ValueError as error:
        raise FrameworkMigrationError(str(error)) from error
    output_value = str(output) if output is not None else plan.default_output
    output_path = Path(output_value)
    if not output_path.is_absolute():
        output_path = root_path / output_path
    output_path = absolute_path(output_path)
    if output_path == root_path:
        raise FrameworkMigrationError("generated output cannot overwrite the source root")
    if output_path.exists() and any(output_path.iterdir()) and not force:
        raise FrameworkMigrationError(
            f"output is not empty: {output_path}; pass --force to replace generated files"
        )
    ensure_safe_directory(output_path)
    relative_source = os.path.relpath(root_path, output_path)
    if plan.mode == NATIVE_STRATEGY:
        scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
        count = _render_native_output(
            plan,
            scan,
            output_path,
            entrypoint="app.py",
            source_root=relative_source,
            sql_engine=engine,
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
    is the workspace root itself. The bench fixture intentionally retains
    Django for ORM access, so this projection uses its installed Django rather
    than the normal plan's independently installed async SQL engine. Source
    function bodies are never copied into the candidate.
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
    destination_path = absolute_path(destination_path)
    if destination_path == root_path:
        raise FrameworkMigrationError("benchmark candidate cannot overwrite the source root")
    overlay = destination_path / "overlay"
    _render_native_output(
        plan,
        scan,
        overlay,
        entrypoint="target_app.py",
        source_root=".",
        sql_engine="django",
    )
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
            " --plan-hash <hash> --bench-candidate <dir>\n"
        ),
    )
    # The candidate carries its own gap disclosure: the reviewed plan, a
    # human-readable gap report, and a fast structural verify. Whoever holds
    # the candidate directory sees exactly what was and was not generated
    # without digging into the source checkout's .sanka/ artifacts.
    _write_json(destination_path / "plan-fastapi.json", plan.to_dict())
    _write_text(destination_path / "GAP-REPORT.md", _render_gap_report(plan, scan))
    _write_json(destination_path / "gap-report.json", _gap_report_payload(plan, scan))
    try:
        verify_report = verify_fastapi_migration(
            root_path, artifact_dir=artifact_dir, output=overlay, probe_http=False
        )
    except FrameworkMigrationError as error:
        verify_report = {"ok": False, "error": str(error)}
    _write_json(destination_path / "verify-report.json", verify_report)
    return destination_path


def write_gap_report(
    root: str | Path = ".",
    destination: str | Path = "gap-report",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
) -> Path:
    """Emit the unsupported-route inventory without generating an application.

    This is the low-readiness deliverable: instead of a mostly-empty scaffold
    that silently 404s, the caller gets the reviewed plan plus a structured
    checklist of every route that still needs a hand-written handler."""
    root_path = Path(root).resolve()
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    if plan.mode != NATIVE_STRATEGY:
        raise FrameworkMigrationError(
            "gap reports describe native plans; run `sanka plan --to fastapi`"
        )
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    destination_path = Path(destination)
    if not destination_path.is_absolute():
        destination_path = root_path / destination_path
    destination_path = destination_path.resolve()
    if destination_path == root_path:
        raise FrameworkMigrationError("gap report cannot overwrite the source root")
    if destination_path.exists() and not destination_path.is_dir():
        raise FrameworkMigrationError(
            f"gap report destination is not a directory: {destination_path}"
        )
    allowed_existing = {"GAP-REPORT.md", "gap-report.json", "plan-fastapi.json"}
    if destination_path.is_dir():
        unexpected = sorted(
            path.name for path in destination_path.iterdir() if path.name not in allowed_existing
        )
        if unexpected:
            raise FrameworkMigrationError(
                "gap report destination contains non-report files; refusing to leave a stale "
                f"scaffold in place: {destination_path} ({', '.join(unexpected)})"
            )
    destination_path.mkdir(parents=True, exist_ok=True)
    _write_json(destination_path / "plan-fastapi.json", plan.to_dict())
    _write_text(destination_path / "GAP-REPORT.md", _render_gap_report(plan, scan))
    _write_json(destination_path / "gap-report.json", _gap_report_payload(plan, scan))
    return destination_path


def _field_payload(field: SerializerFieldIR) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
        "max_digits": field.max_digits,
        "decimal_places": field.decimal_places,
        "choices": list(field.choices),
        "unique": field.unique,
        "unique_message": field.unique_message,
        "has_default": field.has_default,
        "default": field.default,
        "attname": field.attname,
        "messages": dict(field.messages),
    }
    if field.child is not None:
        payload["child"] = {
            "model_module": field.child.model_module,
            "model_class": field.child.model_class,
            "object_name": field.child.object_name,
            "db_table": field.child.db_table,
            "pk_attname": field.child.pk_attname,
            "ordering": list(field.child.ordering),
            "fields": [_field_payload(item) for item in field.child.fields],
        }
    return payload


def _create_payload(ir: SerializerIR) -> dict[str, Any]:
    if ir.create_style == "carryover":
        return {"style": "nested"}
    return {"style": "default"}


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
    requirements = "fastapi>=0.115,<1\nuvicorn[standard]>=0.30,<1\n"
    _write_text(output_path / "requirements.txt", requirements)
    _write_text(output_path / "pyproject.toml", render_generated_pyproject(requirements))
    manifest = attest_generated_bundle(
        output_path,
        manifest,
        protected_names=[
            "README.md",
            "app.py",
            "pyproject.toml",
            "requirements.txt",
            "sanka_compat.py",
        ],
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
    sql_engine: str,
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
    manual = sorted(
        (route for route in plan.routes if route.strategy == ROUTE_STRATEGY_MANUAL),
        key=lambda route: (route.path, route.method),
    )
    scan_routes = {route.key: route for route in scan.routes}
    serializer_by_name = {item.name: item for item in scan.serializer_details}
    views_by_name = {item.name: item for item in scan.view_details}
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
        view_ir = views_by_name.get(route.view)
        auth_payload: dict[str, Any] | None = None
        if view_ir is not None and view_ir.auth is not None and view_ir.auth.require_authenticated:
            auth = view_ir.auth
            auth_payload = {
                "token_keyword": auth.token_keyword,
                "token_db_table": auth.token_db_table,
                "token_key_column": auth.token_key_column,
                "token_key_max_length": auth.token_key_max_length,
                "token_user_column": auth.token_user_column,
                "owner_attname": auth.owner_attname,
                "inject_owner_attname": auth.inject_owner_attname,
                "messages": dict(auth.messages),
            }
        resource = resources.setdefault(
            route.view,
            {
                "view": route.view,
                "auth": auth_payload,
                "serializer": ir.name,
                "model_module": ir.model_module,
                "model_class": ir.model_class,
                "object_name": ir.object_name,
                "db_table": ir.db_table,
                "pk_attname": ir.pk_attname,
                "ordering": list(ir.ordering),
                "lookup": ir.lookup,
                "fields": [_field_payload(field) for field in ir.fields],
                "create": _create_payload(ir),
                "update_drops": None if ir.update_drops is None else list(ir.update_drops),
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
        "sql_engine": sql_engine,
        "database": {
            "vendor": scan.database.vendor,
            "name": scan.database.name,
            "host": scan.database.host,
            "port": scan.database.port,
            "user": scan.database.user,
        },
        "entrypoint": entrypoint,
        "allow": _allow_headers(generated),
        "generic_messages": dict(scan.generic_messages),
        "http_security": dict(scan.http_security),
        "generated_files": [entrypoint],
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
        # The gap inventory travels WITH the overlay: whoever holds the
        # generated app also holds the machine-readable list of everything it
        # does not cover, instead of that truth living only in .sanka/ files
        # that never leave the source checkout.
        "readiness": plan.readiness,
        "native_eligible_routes": plan.native_eligible_routes,
        "needs_adaptation_routes": plan.needs_adaptation_routes,
        "unsupported_routes": [
            {
                "method": route.method,
                "path": route.path,
                "operation": route.operation,
                "source_view": route.source_view,
                "reasons": [
                    {"code": reason.code, "feature": reason.feature, "message": reason.message}
                    for reason in route.adaptation_reasons
                ],
                "stubbed": _stub_safe_path(route.path),
            }
            for route in manual
        ],
        "skipped_routes": [
            {"pattern": item.pattern, "view": item.view, "reason": item.reason}
            for item in scan.skipped_routes
        ],
        "source_root": source_root,
    }
    try:
        generated_names = render_async_sql_files(
            lambda name, text: _write_text(output_path / name, text),
            entrypoint=entrypoint,
            manifest=manifest,
            sql_engine=sql_engine,
        )
    except ValueError as error:
        raise FrameworkMigrationError(str(error)) from error
    generated_files = [entrypoint, *generated_names]
    manifest["generated_files"] = generated_files
    _write_text(output_path / entrypoint, _render_native_app(manifest))
    _write_text(
        output_path / "README.md",
        _render_native_readme(plan, sql_engine, entrypoint=entrypoint),
    )
    protected_names = [
        *generated_files,
        "README.md",
        "pyproject.toml",
        "requirements.txt",
    ]
    manifest = attest_generated_bundle(
        output_path,
        manifest,
        protected_names=list(dict.fromkeys(protected_names)),
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
    allow_unsafe_source_execution: bool = False,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    output_value = str(output) if output is not None else plan.default_output
    output_path = Path(output_value)
    if not output_path.is_absolute():
        output_path = root_path / output_path
    output_path = absolute_path(output_path)
    scan_path = _artifact_path(root_path, artifact_dir, SCAN_FILE).resolve()
    plan_path = _artifact_path(root_path, artifact_dir, PLAN_FILE).resolve()
    manifest_path = output_path / GENERATED_MANIFEST
    try:
        frozen_output, manifest = freeze_generated_bundle(output_path)
    except GeneratedIntegrityError as error:
        raise FrameworkMigrationError(str(error)) from error
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
    _compile_generated_files(frozen_output, manifest)
    probes: list[dict[str, Any]] = []
    generated_environment: GeneratedEnvironment | None = None
    if probe_http and not missing and not extra:
        if plan.mode == NATIVE_STRATEGY:
            generated_environment = ensure_generated_environment(output_path)
        execution_output = (
            generated_environment.bundle_root
            if generated_environment is not None
            else frozen_output
        )
        probes = _probe_read_only_routes(
            root_path,
            execution_output,
            manifest,
            cases=_load_verification_cases(root_path, cases),
            target_python=(
                generated_environment.python if generated_environment is not None else None
            ),
            allow_unsafe_source_execution=allow_unsafe_source_execution,
        )
    failed_probes = [probe for probe in probes if not probe["ok"]]
    generated_files = [
        str(absolute_path(output_path / str(name)))
        for name in manifest.get("generated_files", [])
        if str(name).endswith(".py")
    ]
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
            "enabled": probe_http,
            "evidence": "observational",
            "source_authenticated": False,
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
        "paths": {
            "source": str(root_path),
            "scan": str(scan_path),
            "plan": str(plan_path),
            "generated": str(output_path),
            "manifest": str(manifest_path),
            "pyproject": str((output_path / "pyproject.toml").resolve()),
            "environment": (
                str(generated_environment.root) if generated_environment is not None else None
            ),
            "python": (
                str(generated_environment.python) if generated_environment is not None else None
            ),
            "lockfile": (
                str(generated_environment.lockfile) if generated_environment is not None else None
            ),
        },
        "generated_files": generated_files,
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


def _capture_database(root: Path) -> DatabaseIR:
    django_conf = importlib.import_module("django.conf")
    db = django_conf.settings.DATABASES.get("default") or {}
    engine = str(db.get("ENGINE") or "")
    if "sqlite" in engine:
        vendor = "sqlite"
    elif "postgresql" in engine or "postgis" in engine:
        vendor = "postgresql"
    elif "mysql" in engine:
        vendor = "mysql"
    else:
        vendor = "other"
    name = str(db.get("NAME") or "")
    if vendor == "sqlite" and name:
        path = Path(name)
        path = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            name = str(path.relative_to(root.resolve()))
        except ValueError:
            name = str(path)
    return DatabaseIR(
        vendor=vendor,
        name=name,
        host=str(db.get("HOST") or ""),
        port=str(db.get("PORT") or ""),
        user=str(db.get("USER") or ""),
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
        self.view_details: dict[str, ViewIR] = {}
        self.skipped_routes: list[SkippedRoute] = []


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
            # A non-DRF callback is outside the scan's vocabulary, but silence
            # here hides a real route from every downstream disclosure. Record
            # it so plans, manifests, and gap reports can say "this exists and
            # was never scanned" instead of pretending the URL space ends at
            # DRF's edge.
            if callback is not None:
                view_name = _qualified_name(view_class or callback) or repr(callback)
                result.skipped_routes.append(
                    SkippedRoute(pattern=combined, view=view_name, reason="non-drf-view")
                )
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
        native, adaptation_reasons = _native_route_support(
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
                    adaptation_reasons=adaptation_reasons,
                )
            )
    result.routes = list({route.key: route for route in result.routes}.values())
    result.skipped_routes = list(
        {(item.pattern, item.view): item for item in result.skipped_routes}.values()
    )
    return result


def _is_drf_view(view_class: type[Any]) -> bool:
    return any(base.__module__.startswith("rest_framework.") for base in inspect.getmro(view_class))


def _is_format_alias_path(path: str) -> bool:
    return "{format}" in path or "drf_format_suffix" in path


def _stub_safe_path(path: str) -> bool:
    """True when an unsupported route's path can still be mounted for a stub.

    Paths that keep regex metacharacters after conversion cannot be expressed
    as a FastAPI path; those routes stay absent and are disclosed as such."""
    return re.search(r"[\[\]()+*?|\\^$]", path) is None


def _adaptation_reason(code: str, feature: str, message: str) -> RouteAdaptationReason:
    return RouteAdaptationReason(code=code, feature=feature, message=message)


def _middleware_adaptation_reason(
    middleware: tuple[str, ...],
) -> RouteAdaptationReason:
    count = len(middleware)
    noun = "class is" if count == 1 else "classes are"
    return _adaptation_reason(
        "SANKA_DRF_MIDDLEWARE_UNSUPPORTED",
        "middleware",
        f"{count} Django middleware {noun} outside the native allowlist: " + ", ".join(middleware),
    )


def _unsupported_middleware(middleware: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for item in middleware if item not in _KNOWN_SAFE_MIDDLEWARE)


def _qualified_items(values: Iterable[Any]) -> str:
    names = [name for item in values if (name := _qualified_name(item))]
    return ", ".join(names) if names else "none"


def _not_native(
    code: str, feature: str, message: str
) -> tuple[bool, tuple[RouteAdaptationReason, ...]]:
    return False, (_adaptation_reason(code, feature, message),)


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
) -> tuple[bool, tuple[RouteAdaptationReason, ...]]:
    """Decide whether a route sits inside the native-generation envelope.

    The envelope is deliberately narrow and checked against the live classes,
    not names: default-behavior ModelViewSet CRUD over a captured
    ModelSerializer, or the router API root. Anything else must be adapted by
    a human, never silently bridged in a native plan.
    """
    if _is_format_alias_path(path):
        return False, ()
    if not supported:
        return _not_native(
            "SANKA_DRF_ROUTE_PATTERN_UNSUPPORTED",
            "route-pattern",
            "The route pattern cannot be represented safely as a FastAPI path.",
        )
    unsupported_middleware = _unsupported_middleware(middleware)
    middleware_reasons = (
        (_middleware_adaptation_reason(unsupported_middleware),) if unsupported_middleware else ()
    )

    def disqualify(
        code: str, feature: str, message: str
    ) -> tuple[bool, tuple[RouteAdaptationReason, ...]]:
        return False, (*middleware_reasons, _adaptation_reason(code, feature, message))

    routers = importlib.import_module("rest_framework.routers")
    if inspect.isclass(view_class) and issubclass(view_class, routers.APIRootView):
        permissions_module = importlib.import_module("rest_framework.permissions")
        root_permissions = getattr(view_class, "permission_classes", ())
        if any(item is not permissions_module.AllowAny for item in root_permissions):
            return disqualify(
                "SANKA_DRF_API_ROOT_PERMISSIONS_UNSUPPORTED",
                "permissions",
                "Router API root permissions are outside AllowAny: "
                + _qualified_items(root_permissions),
            )
        links = _api_root_links(callback)
        if links is None:
            return disqualify(
                "SANKA_DRF_API_ROOT_LINKS_UNRESOLVED",
                "router-registration",
                "Router API root links could not be resolved from the registered routes.",
            )
        if middleware_reasons:
            return False, middleware_reasons
        if all(root.path != path for root in result.api_roots):
            result.api_roots.append(ApiRootIR(path=path, links=links))
        return True, ()
    if actions is None:
        return disqualify(
            "SANKA_DRF_VIEW_KIND_UNSUPPORTED",
            "view-kind",
            f"{view_class.__module__}.{view_class.__qualname__} is not router-bound "
            "ModelViewSet CRUD.",
        )
    unsupported_actions = sorted(set(actions.values()) - _SUPPORTED_VIEWSET_ACTIONS)
    if unsupported_actions:
        return disqualify(
            "SANKA_DRF_CUSTOM_ACTION_UNSUPPORTED",
            "viewset-actions",
            "Custom or unsupported viewset actions are present: " + ", ".join(unsupported_actions),
        )
    viewsets = importlib.import_module("rest_framework.viewsets")
    if not issubclass(view_class, viewsets.ModelViewSet):
        return disqualify(
            "SANKA_DRF_VIEWSET_KIND_UNSUPPORTED",
            "view-kind",
            f"{view_class.__module__}.{view_class.__qualname__} is not a ModelViewSet.",
        )
    overrides = _viewset_overrides(view_class)
    if overrides:
        return disqualify(
            "SANKA_DRF_VIEWSET_OVERRIDES_UNSUPPORTED",
            "viewset-overrides",
            "Viewset overrides require manual carryover: " + ", ".join(overrides),
        )
    if getattr(view_class, "lookup_field", "pk") != "pk":
        return disqualify(
            "SANKA_DRF_LOOKUP_FIELD_UNSUPPORTED",
            "lookup-field",
            f"Custom lookup_field is configured: {view_class.lookup_field}",
        )
    view_name = f"{view_class.__module__}.{view_class.__qualname__}"
    if view_name not in result.view_details:
        queryset = getattr(view_class, "queryset", None)
        model = getattr(queryset, "model", None)
        auth_ir = _view_auth_support(view_class, model)
        result.view_details[view_name] = ViewIR(name=view_name, auth=auth_ir)
    if result.view_details[view_name].auth is None:
        return disqualify(
            "SANKA_DRF_AUTH_PERMISSIONS_UNSUPPORTED",
            "authentication-permissions",
            "Authentication/permission classes are outside AllowAny or the supported "
            "TokenAuthentication + IsAuthenticated owner pattern; authentication: "
            + _qualified_items(getattr(view_class, "authentication_classes", ()))
            + "; permissions: "
            + _qualified_items(getattr(view_class, "permission_classes", ())),
        )
    if getattr(view_class, "pagination_class", None) is not None:
        return disqualify(
            "SANKA_DRF_PAGINATION_UNSUPPORTED",
            "pagination",
            "Pagination class requires manual adaptation: "
            + _qualified_items((view_class.pagination_class,)),
        )
    filter_backends = tuple(getattr(view_class, "filter_backends", ()))
    if filter_backends:
        return disqualify(
            "SANKA_DRF_FILTER_BACKENDS_UNSUPPORTED",
            "filter-backends",
            "Filter backends require manual adaptation: " + _qualified_items(filter_backends),
        )
    throttle_classes = tuple(getattr(view_class, "throttle_classes", ()))
    if throttle_classes:
        return disqualify(
            "SANKA_DRF_THROTTLING_UNSUPPORTED",
            "throttling",
            "Throttle classes require manual adaptation: " + _qualified_items(throttle_classes),
        )
    if getattr(view_class, "versioning_class", None) is not None:
        return disqualify(
            "SANKA_DRF_VERSIONING_UNSUPPORTED",
            "versioning",
            "Versioning class requires manual adaptation: "
            + _qualified_items((view_class.versioning_class,)),
        )
    if serializer_name is None:
        return disqualify(
            "SANKA_DRF_SERIALIZER_MISSING",
            "serializer",
            "No serializer_class was captured for this ModelViewSet.",
        )
    ir = result.serializer_details.get(serializer_name)
    if ir is None:
        ir = _serializer_ir(view_class, serializer_name)
        if ir is None:
            return disqualify(
                "SANKA_DRF_SERIALIZER_UNSUPPORTED",
                "serializer",
                f"Serializer cannot be captured as ModelSerializer CRUD: {serializer_name}",
            )
        result.serializer_details[serializer_name] = ir
    if not ir.supported:
        return disqualify(
            "SANKA_DRF_SERIALIZER_SEMANTICS_UNSUPPORTED",
            "serializer",
            f"Serializer fields, validation, queryset, or write overrides require manual "
            f"adaptation: {serializer_name}",
        )
    if middleware_reasons:
        return False, middleware_reasons
    return True, ()


def _viewset_overrides(view_class: type[Any]) -> tuple[str, ...]:
    generics = importlib.import_module("rest_framework.generics")
    mixins = importlib.import_module("rest_framework.mixins")
    expected = {
        "list": mixins.ListModelMixin.list,
        "create": mixins.CreateModelMixin.create,
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
    return tuple(
        name for name, func in expected.items() if getattr(view_class, name, None) is not func
    )


def _view_auth_support(view_class: type[Any], model: Any) -> ViewAuthIR | None:
    """Capture the view's auth semantics, or None when outside the envelope.

    Recognized exactly: AllowAny (no enforcement, no perform_create override);
    or IsAuthenticated with DRF TokenAuthentication, optionally one
    owner-or-read-only object permission (matched structurally) and the
    ``serializer.save(field=self.request.user)`` perform_create idiom.
    """
    permissions_module = importlib.import_module("rest_framework.permissions")
    mixins = importlib.import_module("rest_framework.mixins")
    permissions = list(view_class.permission_classes)
    create_overridden = view_class.perform_create is not mixins.CreateModelMixin.perform_create
    if all(item is permissions_module.AllowAny for item in permissions):
        if create_overridden:
            return None
        return ViewAuthIR(require_authenticated=False)
    if permissions_module.IsAuthenticated not in permissions:
        return None
    extras = [item for item in permissions if item is not permissions_module.IsAuthenticated]
    owner_field: str | None = None
    if len(extras) == 1:
        owner_field = _match_owner_permission(extras[0])
        if owner_field is None:
            return None
    elif extras:
        return None
    authentication_module = importlib.import_module("rest_framework.authentication")
    authenticators = list(view_class.authentication_classes)
    if len(authenticators) != 1:
        return None
    if authenticators[0] is not authentication_module.TokenAuthentication:
        return None
    inject_owner: str | None = None
    if create_overridden:
        inject_owner = _match_perform_create(view_class)
        if inject_owner is None:
            return None
    owner_attname = _user_fk_attname(model, owner_field) if owner_field else None
    if owner_field is not None and owner_attname is None:
        return None
    inject_attname = _user_fk_attname(model, inject_owner) if inject_owner else None
    if inject_owner is not None and inject_attname is None:
        return None
    token_model = authenticators[0]().get_model()
    key_field = token_model._meta.pk
    return ViewAuthIR(
        require_authenticated=True,
        token_keyword=str(authenticators[0].keyword),
        token_db_table=str(token_model._meta.db_table),
        token_key_column=str(key_field.column),
        token_key_max_length=int(getattr(key_field, "max_length", None) or 40),
        token_user_column=str(token_model._meta.get_field("user").attname),
        owner_field=owner_field,
        owner_attname=owner_attname,
        inject_owner=inject_owner,
        inject_owner_attname=inject_attname,
        messages=_probe_auth_messages(authenticators[0]),
    )


def _user_fk_attname(model: Any, field_name: str | None) -> str | None:
    if model is None or field_name is None:
        return None
    exceptions_module = importlib.import_module("django.core.exceptions")
    try:
        field = model._meta.get_field(field_name)
    except exceptions_module.FieldDoesNotExist:
        return None
    auth_module = importlib.import_module("django.contrib.auth")
    if not getattr(field, "is_relation", False):
        return None
    if field.related_model is not auth_module.get_user_model():
        return None
    return str(field.attname)


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _attr_chain(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None


def _match_owner_permission(perm_class: Any) -> str | None:
    """Return the owner field of an owner-or-read-only permission, or None.

    Matched structurally from the AST: ``has_object_permission`` must be the
    canonical safe-methods short-circuit plus an ownership comparison between
    an attribute of the object and the requesting user. Arbitrary permission
    logic cannot be regenerated honestly and keeps the view out of the
    envelope.
    """
    permissions_module = importlib.import_module("rest_framework.permissions")
    if not (
        inspect.isclass(perm_class) and issubclass(perm_class, permissions_module.BasePermission)
    ):
        return None
    permission_type: Any = perm_class
    if permission_type.has_permission is not permissions_module.BasePermission.has_permission:
        return None
    if (
        permission_type.has_object_permission
        is permissions_module.BasePermission.has_object_permission
    ):
        return None
    import textwrap

    try:
        source = textwrap.dedent(inspect.getsource(permission_type.has_object_permission))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    func = tree.body[0]
    arg_names = [item.arg for item in func.args.args]
    if len(arg_names) != 4:
        return None
    _, request_name, _, obj_name = arg_names
    body = [node for node in func.body if not _is_docstring(node)]
    if len(body) == 1 and isinstance(body[0], ast.Return):
        value = body[0].value
        if (
            isinstance(value, ast.BoolOp)
            and isinstance(value.op, ast.Or)
            and len(value.values) == 2
            and _is_safe_method_check(value.values[0], request_name)
        ):
            return _ownership_field(value.values[1], request_name, obj_name)
        return None
    if (
        len(body) == 2
        and isinstance(body[0], ast.If)
        and _is_safe_method_check(body[0].test, request_name)
        and not body[0].orelse
        and len(body[0].body) == 1
        and isinstance(body[0].body[0], ast.Return)
        and isinstance(body[0].body[0].value, ast.Constant)
        and body[0].body[0].value.value is True
        and isinstance(body[1], ast.Return)
        and body[1].value is not None
    ):
        return _ownership_field(body[1].value, request_name, obj_name)
    return None


def _is_safe_method_check(node: ast.expr, request_name: str) -> bool:
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
        return False
    if not isinstance(node.ops[0], ast.In):
        return False
    left = _attr_chain(node.left)
    if left != [request_name, "method"]:
        return False
    target = _attr_chain(node.comparators[0])
    return target is not None and target[-1] == "SAFE_METHODS"


def _ownership_field(node: ast.expr | None, request_name: str, obj_name: str) -> str | None:
    if node is None:
        return None
    if not (
        isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
    ):
        return None
    left, right = node.left, node.comparators[0]
    for obj_side, user_side in ((left, right), (right, left)):
        field = _obj_owner_attr(obj_side, obj_name)
        if field is not None and _is_request_user(user_side, request_name):
            return field
    return None


def _obj_owner_attr(node: ast.expr, obj_name: str) -> str | None:
    chain = _attr_chain(node)
    if not chain or chain[0] != obj_name:
        return None
    if len(chain) == 2:
        name = chain[1]
        if name.endswith("_id") and len(name) > 3:
            return name[:-3]
        return name
    if len(chain) == 3 and chain[2] in ("id", "pk"):
        return chain[1]
    return None


def _is_request_user(node: ast.expr, request_name: str) -> bool:
    chain = _attr_chain(node)
    if not chain or len(chain) < 2 or chain[0] != request_name or chain[1] != "user":
        return False
    if len(chain) == 2:
        return True
    return len(chain) == 3 and chain[2] in ("id", "pk")


def _match_perform_create(view_class: type[Any]) -> str | None:
    """Return the injected kwarg of ``serializer.save(field=self.request.user)``."""
    import textwrap

    try:
        source = textwrap.dedent(inspect.getsource(view_class.perform_create))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    func = tree.body[0]
    arg_names = [item.arg for item in func.args.args]
    if len(arg_names) != 2:
        return None
    self_name, serializer_name = arg_names
    body = [node for node in func.body if not _is_docstring(node)]
    if len(body) != 1 or not isinstance(body[0], ast.Expr):
        return None
    call = body[0].value
    if not (isinstance(call, ast.Call) and not call.args and len(call.keywords) == 1):
        return None
    if _attr_chain(call.func) != [serializer_name, "save"]:
        return None
    keyword = call.keywords[0]
    if keyword.arg is None:
        return None
    if _attr_chain(keyword.value) != [self_name, "request", "user"]:
        return None
    return str(keyword.arg)


def _match_nested_create(
    serializer_class: type[Any],
) -> tuple[str, tuple[tuple[str, str, str | None], ...]] | None:
    """Match exactly the nested parent-plus-children create idiom we generate."""

    import textwrap

    models_module = importlib.import_module("django.db.models")
    try:
        source = textwrap.dedent(inspect.getsource(serializer_class.create))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    func = tree.body[0]
    arg_names = [item.arg for item in func.args.args]
    if len(arg_names) != 2 or func.args.kwonlyargs or func.args.vararg or func.args.kwarg:
        return None
    self_name, data_name = arg_names
    if any(
        isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == self_name
        for node in ast.walk(func)
    ):
        return None
    writable_nested = [
        (str(name), field)
        for name, field in serializer_class().fields.items()
        if getattr(field, "many", False) and not getattr(field, "read_only", False)
    ]
    if len(writable_nested) != 1:
        return None
    nested_name, nested_field = writable_nested[0]
    parent_model = serializer_class.Meta.model
    child_model = nested_field.child.Meta.model
    if not (
        inspect.isclass(parent_model)
        and inspect.isclass(child_model)
        and issubclass(parent_model, models_module.Model)
        and issubclass(child_model, models_module.Model)
    ):
        return None
    parent_model_class = cast(type[Any], parent_model)
    child_model_class = cast(type[Any], child_model)
    relation_names = {
        str(field.name)
        for field in child_model_class._meta.fields
        if getattr(getattr(field, "remote_field", None), "model", None) is parent_model
    }
    if len(relation_names) != 1:
        return None
    relation_name = next(iter(relation_names))
    body = [node for node in func.body if not _is_docstring(node)]
    if len(body) != 3 or not isinstance(body[0], ast.Assign):
        return None
    pop_assignment = body[0]
    if len(pop_assignment.targets) != 1 or not isinstance(pop_assignment.targets[0], ast.Name):
        return None
    children_name = pop_assignment.targets[0].id
    pop_call = pop_assignment.value
    if not (
        isinstance(pop_call, ast.Call)
        and _attr_chain(pop_call.func) == [data_name, "pop"]
        and not pop_call.keywords
        and len(pop_call.args) == 1
        and isinstance(pop_call.args[0], ast.Constant)
        and pop_call.args[0].value == nested_name
    ):
        return None
    transaction_alias: str | None = None
    transaction_body: list[ast.stmt]
    if not isinstance(body[1], ast.With) or len(body[1].items) != 1:
        return None
    context = body[1].items[0]
    if context.optional_vars is not None or not isinstance(context.context_expr, ast.Call):
        return None
    atomic_call = context.context_expr
    chain = _attr_chain(atomic_call.func)
    if (
        chain is None
        or len(chain) != 2
        or chain[1] != "atomic"
        or atomic_call.args
        or atomic_call.keywords
    ):
        return None
    transaction_alias = chain[0]
    transaction_body = body[1].body
    if len(transaction_body) != 2 or not isinstance(transaction_body[0], ast.Assign):
        return None
    parent_assignment = transaction_body[0]
    if len(parent_assignment.targets) != 1 or not isinstance(
        parent_assignment.targets[0], ast.Name
    ):
        return None
    parent_name = parent_assignment.targets[0].id
    parent_call = parent_assignment.value
    if not isinstance(parent_call, ast.Call):
        return None
    parent_chain = _attr_chain(parent_call.func)
    if parent_chain is None or len(parent_chain) != 3 or parent_chain[1:] != ["objects", "create"]:
        return None
    parent_alias = parent_chain[0]
    if parent_call.args or len(parent_call.keywords) != 1:
        return None
    parent_splat = parent_call.keywords[0]
    if not (
        parent_splat.arg is None
        and isinstance(parent_splat.value, ast.Name)
        and parent_splat.value.id == data_name
    ):
        return None
    loop = transaction_body[1]
    if not (
        isinstance(loop, ast.For)
        and isinstance(loop.target, ast.Name)
        and isinstance(loop.iter, ast.Name)
        and loop.iter.id == children_name
        and not loop.orelse
        and len(loop.body) == 1
        and isinstance(loop.body[0], ast.Expr)
        and isinstance(loop.body[0].value, ast.Call)
    ):
        return None
    child_name = loop.target.id
    child_call = loop.body[0].value
    child_chain = _attr_chain(child_call.func)
    if (
        child_chain is None
        or len(child_chain) != 3
        or child_chain[1:] != ["objects", "create"]
        or child_call.args
    ):
        return None
    child_alias = child_chain[0]
    if len(child_call.keywords) != 2:
        return None
    relation_keywords = [keyword for keyword in child_call.keywords if keyword.arg is not None]
    splats = [keyword for keyword in child_call.keywords if keyword.arg is None]
    if not (
        len(relation_keywords) == 1
        and relation_keywords[0].arg == relation_name
        and isinstance(relation_keywords[0].value, ast.Name)
        and relation_keywords[0].value.id == parent_name
        and len(splats) == 1
        and isinstance(splats[0].value, ast.Name)
        and splats[0].value.id == child_name
    ):
        return None
    tail = body[2]
    if not (
        isinstance(tail, ast.Return)
        and isinstance(tail.value, ast.Name)
        and tail.value.id == parent_name
    ):
        return None
    module_globals = vars(importlib.import_module(serializer_class.__module__))
    if module_globals.get(parent_alias) is not parent_model:
        return None
    if module_globals.get(child_alias) is not child_model:
        return None
    transaction_value = module_globals.get(transaction_alias)
    if not (
        inspect.ismodule(transaction_value)
        and transaction_value.__name__ == "django.db.transaction"
    ):
        return None
    return source, (
        (
            parent_alias,
            str(parent_model_class.__module__),
            str(parent_model_class.__qualname__),
        ),
        (
            child_alias,
            str(child_model_class.__module__),
            str(child_model_class.__qualname__),
        ),
        (transaction_alias, "django.db.transaction", None),
    )


def _match_update_drop(serializer_class: type[Any]) -> tuple[str, ...] | None:
    """Match the drop-children update idiom, returning the dropped fields.

    Accepted shape: zero or more ``validated_data.pop("<field>"[, None])``
    statements followed by ``return super().update(instance, validated_data)``.
    """
    import textwrap

    try:
        source = textwrap.dedent(inspect.getsource(serializer_class.update))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    func = tree.body[0]
    arg_names = [item.arg for item in func.args.args]
    if len(arg_names) != 3:
        return None
    _, instance_name, data_name = arg_names
    body = [node for node in func.body if not _is_docstring(node)]
    if not body or not isinstance(body[-1], ast.Return):
        return None
    dropped: list[str] = []
    for statement in body[:-1]:
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
            return None
        call = statement.value
        if _attr_chain(call.func) != [data_name, "pop"] or call.keywords:
            return None
        if not call.args or not isinstance(call.args[0], ast.Constant):
            return None
        if not isinstance(call.args[0].value, str):
            return None
        if len(call.args) == 2:
            if not (isinstance(call.args[1], ast.Constant) and call.args[1].value is None):
                return None
        elif len(call.args) != 1:
            return None
        dropped.append(call.args[0].value)
    tail = body[-1].value
    if not (
        isinstance(tail, ast.Call)
        and not tail.keywords
        and isinstance(tail.func, ast.Attribute)
        and tail.func.attr == "update"
        and isinstance(tail.func.value, ast.Call)
        and isinstance(tail.func.value.func, ast.Name)
        and tail.func.value.func.id == "super"
        and not tail.func.value.args
        and len(tail.args) == 2
        and isinstance(tail.args[0], ast.Name)
        and tail.args[0].id == instance_name
        and isinstance(tail.args[1], ast.Name)
        and tail.args[1].id == data_name
    ):
        return None
    return tuple(dropped)


def _probe_auth_messages(auth_class: type[Any]) -> tuple[tuple[str, str], ...]:
    """Capture the auth error strings the live DRF installation produces.

    The header-parsing failures are probed with fake requests (no database
    access). The invalid-key and inactive-user paths require database rows a
    scan must never create, so those two strings are DRF's stable inline
    defaults.
    """
    exceptions_module = importlib.import_module("rest_framework.exceptions")
    authenticator = auth_class()
    keyword = str(auth_class.keyword)
    messages = {
        "no_credentials": str(exceptions_module.NotAuthenticated.default_detail),
        "forbidden": str(exceptions_module.PermissionDenied.default_detail),
        "invalid_token": "Invalid token.",
        "inactive_user": "User inactive or deleted.",
        "www_authenticate": str(authenticator.authenticate_header(None)),
    }

    class _ProbeRequest:
        def __init__(self, header: str) -> None:
            self.META = {"HTTP_AUTHORIZATION": header}
            self.headers = {"authorization": header}

    for key, header in (("empty_header", keyword), ("spaced_header", f"{keyword} a b")):
        try:
            authenticator.authenticate(_ProbeRequest(header))
        except exceptions_module.AuthenticationFailed as error:
            messages[key] = str(error.detail)
    return tuple(sorted(messages.items()))


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
    lookup = str(
        getattr(view_class, "lookup_url_kwarg", None) or getattr(view_class, "lookup_field", "pk")
    )
    ir = _build_serializer_ir(
        serializer_class,
        model,
        name=serializer_name,
        ordering=tuple(
            str(item) for item in (queryset.query.order_by or model._meta.ordering or ())
        ),
        lookup=lookup,
        analyze_writes=True,
    )
    if queryset.query.where.children:
        ir = replace_dataclass(ir, supported=False)
    return ir


def _build_serializer_ir(
    serializer_class: type[Any],
    model: Any,
    *,
    name: str,
    ordering: tuple[str, ...],
    lookup: str = "pk",
    analyze_writes: bool,
) -> SerializerIR:
    """Build the IR for one ModelSerializer (top-level or nested child)."""
    serializers_module = importlib.import_module("rest_framework.serializers")
    supported = True
    if _defines_custom_validation(serializer_class):
        supported = False
    fields: list[SerializerFieldIR] = []
    has_writable_nested = False
    for field_name, field in serializer_class().fields.items():
        field_ir = _serializer_field_ir(str(field_name), field, model)
        fields.append(field_ir)
        supported = supported and field_ir.supported
        if field_ir.kind == "nested_many" and not field_ir.read_only:
            has_writable_nested = True

    create_style = "default"
    update_drops: tuple[str, ...] | None = None
    if analyze_writes:
        create_overridden = serializer_class.create is not serializers_module.ModelSerializer.create
        update_overridden = serializer_class.update is not serializers_module.ModelSerializer.update
        if create_overridden:
            carryover = _match_nested_create(serializer_class)
            if carryover is None:
                supported = False
            else:
                create_style = "carryover"
        elif has_writable_nested:
            # DRF's default create() raises on writable nested fields; an
            # honest native migration needs the author's own create logic.
            supported = False
        if update_overridden:
            update_drops = _match_update_drop(serializer_class)
            if update_drops is None:
                supported = False
        elif has_writable_nested:
            supported = False
    elif serializer_class.create is not serializers_module.ModelSerializer.create or (
        serializer_class.update is not serializers_module.ModelSerializer.update
    ):
        # Nested children are written by the parent's create path; their own
        # overrides would be silently skipped, so they are unsupported.
        supported = False

    return SerializerIR(
        name=name,
        model=f"{model.__module__}.{model.__qualname__}",
        model_module=str(model.__module__),
        model_class=str(model.__qualname__),
        object_name=str(model._meta.object_name),
        db_table=str(model._meta.db_table),
        pk_attname=str(model._meta.pk.attname),
        ordering=ordering,
        lookup=lookup,
        fields=tuple(fields),
        create_style=create_style,
        update_drops=update_drops,
        supported=supported,
    )


def _nested_many_field_ir(name: str, field: Any, parent_model: Any) -> SerializerFieldIR:
    """IR for a ``many=True`` nested ModelSerializer child."""
    serializers_module = importlib.import_module("rest_framework.serializers")
    child = field.child
    if not isinstance(child, serializers_module.ModelSerializer):
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    if getattr(field, "source", name) != name or not getattr(field, "allow_empty", True):
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    child_class = type(child)
    child_model = getattr(getattr(child_class, "Meta", None), "model", None)
    if child_model is None or parent_model is None:
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    try:
        relation = parent_model._meta.get_field(name)
    except Exception:
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    if getattr(relation, "related_model", None) is not child_model:
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    child_model_type: Any = child_model
    child_ir = _build_serializer_ir(
        child_class,
        child_model_type,
        name=f"{child_class.__module__}.{child_class.__qualname__}",
        ordering=tuple(str(item) for item in (child_model_type._meta.ordering or ())),
        analyze_writes=False,
    )
    parent_fk = relation.field.attname if hasattr(relation, "field") else None
    if parent_fk is None:
        return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    messages = {
        "required": str(field.error_messages.get("required", "")),
        "null": str(field.error_messages.get("null", "")),
        "not_a_list": str(field.error_messages.get("not_a_list", "")),
    }
    # The generated runtime only enforces uniqueness on top-level fields.
    child_supported = child_ir.supported and not any(item.unique for item in child_ir.fields)
    return SerializerFieldIR(
        name=name,
        kind="nested_many",
        required=bool(field.required),
        read_only=bool(field.read_only),
        allow_null=bool(field.allow_null),
        attname=str(parent_fk),
        child=child_ir,
        messages=tuple(sorted((key, value) for key, value in messages.items() if value)),
        supported=child_supported,
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


def _serializer_field_ir(name: str, field: Any, model: Any) -> SerializerFieldIR:
    fields_module = importlib.import_module("rest_framework.fields")
    relations_module = importlib.import_module("rest_framework.relations")
    validators_module = importlib.import_module("django.core.validators")
    if type(field) is relations_module.PrimaryKeyRelatedField and field.read_only:
        attname = _related_attname(model, name)
        return SerializerFieldIR(
            name=name,
            kind="related_pk",
            read_only=True,
            attname=attname,
            supported=attname is not None,
        )
    serializers_module = importlib.import_module("rest_framework.serializers")
    if type(field) is serializers_module.ListSerializer:
        return _nested_many_field_ir(name, field, model)
    kind: str | None = None
    if type(field) is fields_module.IntegerField:
        kind = "integer"
    elif type(field) is fields_module.CharField:
        kind = "char"
    elif type(field) is fields_module.DecimalField:
        kind = "decimal"
        if not getattr(field, "coerce_to_string", True):
            return SerializerFieldIR(name=name, kind="unsupported", supported=False)
    elif type(field) is fields_module.ChoiceField:
        kind = "choice"
        values = tuple(field.choices)
        if not all(isinstance(value, str | int) for value in values):
            return SerializerFieldIR(name=name, kind="unsupported", supported=False)
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
        drf_validators.UniqueValidator,
    )
    supported = all(isinstance(item, allowed_validators) for item in field.validators)
    unique = False
    unique_message: str | None = None
    for item in field.validators:
        if isinstance(item, drf_validators.UniqueValidator):
            unique = True
            unique_message = str(item.message)
    default = getattr(field, "default", fields_module.empty)
    has_default = default is not fields_module.empty
    if not has_default:
        model_default = _django_field_default(model, name)
        if model_default is not fields_module.empty:
            default = model_default
            has_default = True
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
        max_digits=_maybe_int(getattr(field, "max_digits", None)),
        decimal_places=_maybe_int(getattr(field, "decimal_places", None)),
        choices=tuple(field.choices) if kind == "choice" else (),
        has_default=has_default,
        default=default if has_default else None,
        unique=unique,
        unique_message=unique_message,
        messages=_field_messages(field, kind),
        supported=supported,
    )


def _django_field_default(model: Any, name: str) -> Any:
    fields_module = importlib.import_module("rest_framework.fields")
    if model is None:
        return fields_module.empty
    exceptions_module = importlib.import_module("django.core.exceptions")
    try:
        field = model._meta.get_field(name)
    except exceptions_module.FieldDoesNotExist:
        return fields_module.empty
    if not field.has_default() or callable(field.default):
        return fields_module.empty
    return field.default


def _related_attname(model: Any, field_name: str) -> str | None:
    if model is None:
        return None
    exceptions_module = importlib.import_module("django.core.exceptions")
    try:
        field = model._meta.get_field(field_name)
    except exceptions_module.FieldDoesNotExist:
        return None
    if not getattr(field, "is_relation", False):
        return None
    return str(field.attname)


def _maybe_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


_MESSAGE_KEYS = {
    "integer": ("required", "null", "invalid", "min_value", "max_value", "max_string_length"),
    "decimal": (
        "required",
        "null",
        "invalid",
        "max_digits",
        "max_decimal_places",
        "max_whole_digits",
        "max_string_length",
    ),
    "choice": ("required", "null", "invalid_choice"),
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
        for key in ("min_value", "max_value", "max_length", "min_length", "max_digits")
        if (value := getattr(field, key, None)) is not None
    }
    if getattr(field, "decimal_places", None) is not None:
        params["max_decimal_places"] = field.decimal_places
        if getattr(field, "max_digits", None) is not None:
            params["max_whole_digits"] = field.max_digits - field.decimal_places
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
    try:
        value = json.loads(safe_read_text(path, max_bytes=96 * 1024 * 1024))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        raise FrameworkMigrationError(f"could not read {label} artifact: {path}") from error
    if not isinstance(value, dict):
        raise FrameworkMigrationError(f"{label} artifact must be a JSON object: {path}")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, content: str) -> None:
    safe_write_text(path, content)


def _compile_generated_files(output: Path, manifest: dict[str, Any]) -> None:
    names = [
        str(name)
        for name in manifest.get("generated_files", ["app.py", "sanka_compat.py"])
        if str(name).endswith(".py")
    ]
    for name in names:
        path = output / name
        try:
            text = safe_read_text(path, max_bytes=16 * 1024 * 1024)
        except (OSError, UnicodeDecodeError) as error:
            raise FrameworkMigrationError(f"generated file is missing or unsafe: {path}") from error
        try:
            compile(text, str(path), "exec")
        except SyntaxError as error:
            raise FrameworkMigrationError(
                f"generated Python is invalid: {path}: {error}"
            ) from error
        if manifest.get("mode") == NATIVE_STRATEGY:
            if manifest.get("sql_engine") == "django":
                # The retained-ORM projection imports Django by design; what
                # must never appear is the request-serving machinery.
                serving_machinery = (
                    "django.core.asgi",
                    "django.core.wsgi",
                    "django.core.handlers",
                    "django.test",
                )
                if any(item in text for item in serving_machinery):
                    raise FrameworkMigrationError(
                        f"native output imports Django serving machinery: {path}"
                    )
            elif "django.setup" in text or "import django" in text:
                raise FrameworkMigrationError(f"native output still imports Django: {path}")


def _load_generated_app(output: Path) -> Any:
    module_name = f"_sanka_generated_{abs(hash(output))}"
    spec = importlib.util.spec_from_file_location(module_name, output / "app.py")
    if spec is None or spec.loader is None:
        raise FrameworkMigrationError("could not load the generated FastAPI application")
    if str(output) not in sys.path:
        sys.path.insert(0, str(output))
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as error:
        raise FrameworkMigrationError(
            f"generated app is missing a dependency ({error.name}); "
            f"install {output / 'requirements.txt'}"
        ) from error
    return module.app


def _bind_source_database() -> None:
    django_conf = importlib.import_module("django.conf")
    name = django_conf.settings.DATABASES.get("default", {}).get("NAME")
    if name and not os.environ.get("SANKA_DATABASE_URL") and not os.environ.get("SANKA_TEST_DB"):
        os.environ["SANKA_TEST_DB"] = str(name)
    connections = importlib.import_module("django.db").connections
    connections.close_all()


def _probe_read_only_routes(
    root: Path,
    output: Path,
    manifest: dict[str, Any],
    *,
    cases: list[dict[str, Any]],
    target_python: Path | None,
    allow_unsafe_source_execution: bool,
) -> list[dict[str, Any]]:
    automatic = [
        {"method": route.get("method"), "path": route.get("path"), "headers": {}}
        for route in manifest.get("routes", [])
        if route.get("method") in {"GET", "HEAD"} and "{" not in route.get("path", "")
    ]
    probes = _deduplicate_probes(automatic + cases)
    settings = str(manifest["settings_module"])
    try:
        source_payload = run_untrusted_framework_worker(
            {
                "operation": "source-probes",
                "root": str(root),
                "settings": settings,
                "probes": probes,
            },
            readable_roots=[root],
            allow_unsafe=allow_unsafe_source_execution,
        )
        source_responses = _decode_worker_responses(source_payload, expected=len(probes))
    except UntrustedFrameworkError as error:
        raise FrameworkMigrationError(str(error)) from error
    if target_python is not None:
        target_responses = _native_target_probe_responses(
            output, target_python, probes, source_root=root
        )
    else:
        try:
            target_payload = run_untrusted_framework_worker(
                {
                    "operation": "compatibility-probes",
                    "root": str(root),
                    "settings": settings,
                    "output": str(output),
                    "probes": probes,
                },
                readable_roots=[root, output],
                allow_unsafe=allow_unsafe_source_execution,
            )
            target_responses = _decode_worker_responses(target_payload, expected=len(probes))
        except UntrustedFrameworkError as error:
            raise FrameworkMigrationError(str(error)) from error
    results: list[dict[str, Any]] = []
    for case, source_response, target_response in zip(
        probes, source_responses, target_responses, strict=True
    ):
        source_body = source_response["body"]
        target_body = target_response["body"]
        source_type = source_response["content_type"]
        target_type = target_response["content_type"]
        bodies_match = _response_bodies_match(source_body, target_body, source_type, target_type)
        compared_headers = ("allow", "location", "www-authenticate")
        headers_match = all(
            source_response["headers"].get(header, "") == target_response["headers"].get(header, "")
            for header in compared_headers
        )
        ok = (
            source_response["status"] == target_response["status"]
            and source_type == target_type
            and bodies_match
            and headers_match
        )
        results.append(
            {
                "method": case["method"],
                "path": case["path"],
                "ok": ok,
                "source_status": source_response["status"],
                "target_status": target_response["status"],
                "source_content_type": source_type,
                "target_content_type": target_type,
                "headers_match": headers_match,
            }
        )
    return results


def _source_probe_responses_in_process(
    root: Path, settings_module: str, probes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Worker-only source probe implementation."""

    _bootstrap_django(root, settings_module)
    _bind_source_database()
    django_test = importlib.import_module("django.test")
    source = django_test.Client()
    return [_source_probe_response(source, case) for case in probes]


def _decode_worker_responses(payload: dict[str, Any], *, expected: int) -> list[dict[str, Any]]:
    raw = payload.get("responses")
    if not isinstance(raw, list) or len(raw) != expected:
        raise FrameworkMigrationError("isolated framework worker returned incomplete responses")
    responses: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise FrameworkMigrationError("isolated framework worker returned invalid responses")
        try:
            status = int(item["status"])
            content_type = str(item["content_type"])
            body = base64.b64decode(str(item["body"]), validate=True)
            raw_headers = item["headers"]
            if status < 100 or status > 599:
                raise ValueError("invalid HTTP status")
            if len(content_type.encode("utf-8")) > 4_096 or "\x00" in content_type:
                raise ValueError("invalid content type")
            if len(body) > 16 * 1024 * 1024:
                raise ValueError("response body exceeds the worker boundary limit")
            if not isinstance(raw_headers, dict) or len(raw_headers) > 64:
                raise ValueError("invalid response headers")
            headers = {str(key).lower(): str(value) for key, value in raw_headers.items()}
            if any(
                len(key.encode("utf-8")) > 256
                or len(value.encode("utf-8")) > 8_192
                or "\x00" in key
                or "\x00" in value
                for key, value in headers.items()
            ):
                raise ValueError("invalid response headers")
            responses.append(
                {
                    "status": status,
                    "content_type": content_type,
                    "body": body,
                    "headers": headers,
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FrameworkMigrationError(
                "isolated framework worker returned invalid response data"
            ) from error
    return responses


def _deduplicate_probes(probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for case in probes:
        method = str(case.get("method", "GET")).upper()
        path = str(case.get("path", ""))
        headers = {str(key): str(value) for key, value in dict(case.get("headers", {})).items()}
        identity = (method, path, json.dumps(headers, sort_keys=True))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append({"method": method, "path": path, "headers": headers})
    return unique


def _source_probe_response(source: Any, case: dict[str, Any]) -> dict[str, Any]:
    headers = case["headers"]
    django_headers = {
        "HTTP_" + key.upper().replace("-", "_"): value
        for key, value in headers.items()
        if key.lower() not in {"content-type", "content-length"}
    }
    response = source.generic(case["method"], case["path"], **django_headers)
    importlib.import_module("django.db").connections.close_all()
    return {
        "status": response.status_code,
        "content_type": str(response.get("Content-Type", "")).split(";", 1)[0],
        "body": bytes(response.content),
        "headers": {
            name: str(response.get(name, "")) for name in ("allow", "location", "www-authenticate")
        },
    }


def _compatibility_target_probe_responses(
    output: Path, probes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    try:
        fastapi_testclient = importlib.import_module("fastapi.testclient")
    except ModuleNotFoundError as error:
        raise FrameworkMigrationError(
            "FastAPI is required to verify a compatibility bridge; install the generated "
            f"dependencies from {output / 'requirements.txt'} into the source environment"
        ) from error
    responses: list[dict[str, Any]] = []
    with fastapi_testclient.TestClient(_load_generated_app(output)) as target:
        for case in probes:
            response = target.request(case["method"], case["path"], headers=case["headers"])
            responses.append(_target_response_payload(response))
    return responses


def _target_response_payload(response: Any) -> dict[str, Any]:
    return {
        "status": response.status_code,
        "content_type": str(response.headers.get("content-type", "")).split(";", 1)[0],
        "body": response.content,
        "headers": {
            name: str(response.headers.get(name, ""))
            for name in ("allow", "location", "www-authenticate")
        },
    }


def _native_target_probe_responses(
    output: Path,
    target_python: Path,
    probes: list[dict[str, Any]],
    *,
    source_root: Path,
) -> list[dict[str, Any]]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "LANG",
            "LC_ALL",
            "PATH",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "WINDIR",
        }
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["SANKA_SOURCE_ROOT"] = str(source_root)
    result = subprocess.run(
        [str(target_python), "-I", "-B", "-c", _TARGET_PROBE_SCRIPT, str(output)],
        cwd=output,
        env=environment,
        input=json.dumps(probes),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "generated target probe failed").strip()
        raise FrameworkMigrationError(
            f"could not verify the generated app with {target_python}:\n{detail}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise FrameworkMigrationError(
            "generated target verification returned invalid output"
        ) from error
    if not isinstance(payload, list) or len(payload) != len(probes):
        raise FrameworkMigrationError("generated target verification returned incomplete results")
    responses: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise FrameworkMigrationError("generated target verification returned invalid results")
        try:
            responses.append(
                {
                    "status": int(item["status"]),
                    "content_type": str(item["content_type"]),
                    "body": base64.b64decode(str(item["body"]), validate=True),
                    "headers": dict(item["headers"]),
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FrameworkMigrationError(
                "generated target verification returned invalid response data"
            ) from error
    return responses


_TARGET_PROBE_SCRIPT = r"""import base64
import json
import sys

sys.path.insert(0, sys.argv[1])

from fastapi.testclient import TestClient
from app import app

probes = json.load(sys.stdin)
results = []
with TestClient(app) as client:
    for case in probes:
        response = client.request(case["method"], case["path"], headers=case["headers"])
        results.append({
            "status": response.status_code,
            "content_type": response.headers.get("content-type", "").split(";", 1)[0],
            "body": base64.b64encode(response.content).decode("ascii"),
            "headers": {
                name: response.headers.get(name, "")
                for name in ("allow", "location", "www-authenticate")
            },
        })
json.dump(results, sys.stdout)
"""


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


_FASTAPI_DECORATOR = {
    "GET": "get",
    "POST": "post",
    "PUT": "put",
    "PATCH": "patch",
    "DELETE": "delete",
}

_OPERATION_FUNCS = {
    "list": "list",
    "create": "create",
    "retrieve": "get",
    "update": "replace",
    "partial_update": "update",
    "destroy": "delete",
}


def _python_ident(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "item"


def _py_str(value: str) -> str:
    return json.dumps(value)


def _unique_ident(base: str, used: set[str]) -> str:
    name = base
    index = 2
    while name in used:
        name = f"{base}_{index}"
        index += 1
    used.add(name)
    return name


def _render_native_app(manifest: dict[str, Any]) -> str:
    """Emit decorator-style async FastAPI routes that call the shared native helpers."""
    lines = [
        "# Generated by Sanka. Async FastAPI over the existing SQL tables.",
        "from contextlib import asynccontextmanager",
        "",
        "from fastapi import FastAPI, Request",
        "from fastapi.responses import Response",
        "from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware",
        "from starlette.middleware.trustedhost import TrustedHostMiddleware",
        "",
        "import sanka_native as native",
        "import sanka_store as store",
        "",
        "",
        "@asynccontextmanager",
        "async def lifespan(_app: FastAPI):",
        "    await store.init_db()",
        "    try:",
        "        yield",
        "    finally:",
        "        await store.close_db()",
        "",
        "",
        'app = FastAPI(title="Sanka native FastAPI application", lifespan=lifespan)',
        '_HTTP_SECURITY = native.MANIFEST.get("http_security", {})',
        '_ALLOWED_HOSTS = _HTTP_SECURITY.get("allowed_hosts", [])',
        'if _HTTP_SECURITY.get("ssl_redirect"):',
        "    app.add_middleware(HTTPSRedirectMiddleware)",
        'if _ALLOWED_HOSTS and "*" not in _ALLOWED_HOSTS:',
        "    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)",
        "",
        '@app.middleware("http")',
        "async def add_security_headers(request: Request, call_next):",
        "    response = await call_next(request)",
        "    native.apply_security_headers(response, request, _HTTP_SECURITY)",
        "    return response",
        "",
    ]
    used_vars: set[str] = set()
    used_funcs: set[str] = set()
    resource_var: dict[str, str] = {}
    object_names: dict[str, str] = {}
    for resource in manifest["resources"]:
        view = str(resource["view"])
        ident = _python_ident(str(resource["object_name"]))
        var = _unique_ident(f"_{ident.upper()}", used_vars)
        resource_var[view] = var
        object_names[view] = ident
        lines.append(f"{var} = native.resource({_py_str(view)})")
    if resource_var:
        lines.append("")
    for route in manifest["routes"]:
        method = str(route["method"]).upper()
        path = str(route["path"])
        operation = str(route["operation"])
        decorator = _FASTAPI_DECORATOR.get(method)
        if decorator is None:
            raise FrameworkMigrationError(f"unsupported native HTTP method: {method}")
        if str(route.get("strategy")) == ROUTE_STRATEGY_NATIVE_API_ROOT:
            func = _unique_ident("api_root", used_funcs)
            lines.extend(
                [
                    f"@app.{decorator}({_py_str(path)})",
                    f"async def {func}(request: Request) -> Response:",
                    f"    return await native.api_root(request, {_py_str(path)})",
                    "",
                ]
            )
            continue
        view = str(route["source_view"])
        var = resource_var[view]
        func = _unique_ident(
            f"{_python_ident(_OPERATION_FUNCS.get(operation, operation))}_{object_names[view]}",
            used_funcs,
        )
        if "{pk}" in path:
            signature = "request: Request, pk: str"
            call = f"    return await native.handle({var}, {_py_str(operation)}, request)"
        else:
            signature = "request: Request"
            call = f"    return await native.handle({var}, {_py_str(operation)}, request)"
        lines.extend(
            [
                f"@app.{decorator}({_py_str(path)})",
                f"async def {func}({signature}) -> Response:",
                call,
                "",
            ]
        )
    stubbed = [entry for entry in manifest.get("unsupported_routes", []) if entry.get("stubbed")]
    if stubbed:
        lines.extend(
            [
                "# Routes outside Sanka's native envelope answer 501 with their",
                "# adaptation codes: an unmigrated route fails loudly instead of",
                "# silently 404ing. Replace each stub with a real handler; the",
                "# inventory lives in sanka-manifest.json under unsupported_routes.",
                "",
            ]
        )
        for entry in stubbed:
            method = str(entry["method"]).upper()
            path = str(entry["path"])
            body = json.dumps(
                {
                    "detail": (
                        "This route is outside Sanka's native generation envelope "
                        "and has not been migrated."
                    ),
                    "sanka": {
                        "route": f"{method} {path}",
                        "adaptation_codes": [
                            str(reason["code"]) for reason in entry.get("reasons", [])
                        ],
                        "see": "sanka-manifest.json#unsupported_routes",
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            func = _unique_ident("sanka_unsupported", used_funcs)
            lines.extend(
                [
                    f"@app.api_route({_py_str(path)}, methods=[{_py_str(method)}])",
                    f"async def {func}() -> Response:",
                    "    return Response(",
                    f"        content={_py_str(body)},",
                    "        status_code=501,",
                    '        media_type="application/json",',
                    "    )",
                    "",
                ]
            )
    lines.extend(
        [
            'if __name__ == "__main__":',
            "    import uvicorn",
            '    uvicorn.run(app, host="127.0.0.1", port=8000)',
            "",
        ]
    )
    return "\n".join(lines)


_PARITY_CHECKLIST = """\
## DRF parity checklist for hand-written handlers

Behavior that most often breaks exact parity when porting DRF by hand — every
item below has cost a real migration its last few percent:

- DRF stamps an `Allow` header on every response, including 400/404 (HEAD is
  added for GET; OPTIONS is always present).
- 404 has two flavors: a missing object renders the model's "No X matches the
  given query." while an invalid pk type renders the generic "Not found."
- Field-level null checks run before type checks: `{"items": null}` must yield
  `["This field may not be null."]`, not a list-type error.
- "may not be blank" (blank string) and "This field is required." (absent or
  null-file field) are different validations with different wording.
- Redirect responses carry an absolute `Location` URI
  (`request.build_absolute_uri`), never a relative path — and a framework's
  implicit trailing-slash redirect is not equivalent to the source's redirect
  view.
- Auth failures have exact strings and a `WWW-Authenticate` header; session
  authentication enforces CSRF even for API clients (Django's test client
  skips that only until `enforce_csrf_checks=True`).
- Unique-constraint violations surface as the model's own message (e.g.
  "order with this reference already exists.") as a 400 response — an
  unhandled database IntegrityError that kills the serving process is not
  parity.
- Django's test client omits `Content-Length`; adding or keeping the header
  where the source has none is a visible difference."""


def _gap_report_payload(plan: FrameworkPlan, scan: FrameworkScan) -> dict[str, Any]:
    manual = sorted(
        (route for route in plan.routes if route.strategy == ROUTE_STRATEGY_MANUAL),
        key=lambda route: (route.path, route.method),
    )
    return {
        "schema": "sanka/native-gap-report/v1",
        "plan_hash": plan.plan_hash,
        "readiness": plan.readiness,
        "threshold_unit": "ratio",
        "native_routes": plan.native_routes,
        "native_eligible_routes": plan.native_eligible_routes,
        "needs_adaptation_routes": plan.needs_adaptation_routes,
        "unsupported_routes": [
            {
                "method": route.method,
                "path": route.path,
                "operation": route.operation,
                "source_view": route.source_view,
                "reasons": [
                    {"code": reason.code, "feature": reason.feature, "message": reason.message}
                    for reason in route.adaptation_reasons
                ],
            }
            for route in manual
        ],
        "skipped_routes": [
            {"pattern": item.pattern, "view": item.view, "reason": item.reason}
            for item in scan.skipped_routes
        ],
        "critic_checks": {
            "route_coverage": "required",
            "redirect_and_header_parity": "required",
            "native_serving_evidence": "required",
            "database_parity": "required",
        },
    }


def _render_gap_report(plan: FrameworkPlan, scan: FrameworkScan) -> str:
    manual = sorted(
        (route for route in plan.routes if route.strategy == ROUTE_STRATEGY_MANUAL),
        key=lambda route: (route.path, route.method),
    )
    lines = [
        "# Sanka native migration gap report",
        "",
        f"Plan `{plan.plan_hash}` — native readiness {plan.readiness:.0%} "
        f"({plan.native_routes}/{plan.native_eligible_routes} non-alias routes generatable).",
        "",
        "The source application remains the specification. Every route below",
        "still needs a hand-written handler whose behavior is verified against",
        "the source application, not assumed from generated code.",
        "",
    ]
    if manual:
        lines.append(f"## Routes needing manual adaptation ({len(manual)})")
        lines.append("")
        for route in manual:
            mounted = (
                "stubbed to answer 501 in the generated app"
                if _stub_safe_path(route.path)
                else "NOT mounted — the path is not representable as a FastAPI route"
            )
            lines.append(f"- `{route.method} {route.path}` — {mounted}")
            for reason in route.adaptation_reasons:
                lines.append(f"  - `{reason.code}`: {reason.message}")
        lines.append("")
    else:
        lines.extend(
            [
                "## Routes needing manual adaptation (0)",
                "",
                "Every non-alias scanned route was generated natively.",
                "",
            ]
        )
    if scan.skipped_routes:
        lines.extend(
            [
                f"## URL patterns the scanner did not scan ({len(scan.skipped_routes)})",
                "",
                "Non-DRF Django views: they serve real traffic but are invisible to",
                "the DRF scan, so no readiness number accounts for them. Port them by",
                "hand.",
                "",
            ]
        )
        for item in scan.skipped_routes:
            lines.append(f"- `{item.pattern}` → `{item.view}` ({item.reason})")
        lines.append("")
    lines.append(_PARITY_CHECKLIST)
    lines.extend(
        [
            "",
            "## Machine-readable detail",
            "",
            "`plan-fastapi.json` beside this file carries per-route strategies and",
            "adaptation codes; a generated app's `sanka-manifest.json` repeats the",
            "inventory under `unsupported_routes` and `skipped_routes`.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_native_readme(
    plan: FrameworkPlan, sql_engine: str = "tortoise", *, entrypoint: str = "app.py"
) -> str:
    engine = sql_engine or "tortoise"
    if engine == "django":
        persistence = (
            "Persistence uses the retained Django ORM through the async facade in "
            "`sanka_store.py`. Generated `sanka_settings.py` removes DRF apps; Django is "
            "loaded for ORM access only, never as the request server."
        )
    else:
        persistence = (
            f"Persistence is async SQL (`{engine}`) in `sanka_store.py`, mapped onto the "
            "existing Django tables. Django is not imported at serve time."
        )
    gaps = ""
    if plan.needs_adaptation_routes:
        gaps = (
            f"\n**{plan.needs_adaptation_routes} route(s) are outside the native envelope "
            f"and were NOT migrated** (native readiness {plan.readiness:.0%}). Mountable "
            "ones are stubbed to answer 501 with their adaptation codes; the full "
            "inventory is `unsupported_routes` in `sanka-manifest.json`. For those "
            "routes the source application remains the specification.\n"
        )
    return f"""# Generated native FastAPI application

Sanka generated this application from plan `{plan.plan_hash}`.

Routes are declared with FastAPI decorators in `{entrypoint}` (`@app.get`,
`@app.post`, ...). Shared DRF-parity validation lives in `sanka_native.py`.
{persistence}
{gaps}

Set `SANKA_DATABASE_URL` for PostgreSQL (the scan never stores a password).
SQLite uses the captured database path, overridable with `SANKA_DATABASE_URL`
or `SANKA_TEST_DB`.

`sanka test` writes `test_generated.py` here and runs it. SQLite write tests
use an isolated copy of the database.

Format-suffix alias routes from the source router are dropped as a disclosed
contract change; clients negotiate content types with headers instead.

```bash
uv sync
uv run python {entrypoint}
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
from fastapi.responses import Response, StreamingResponse

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "sanka-manifest.json").read_text(encoding="utf-8"))
SOURCE_ROOT = Path(
    os.environ.get("SANKA_SOURCE_ROOT")
    or (HERE / MANIFEST["source_root"]).resolve()
).resolve()
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
    scope = dict(request.scope)
    scope["method"] = method
    scope["root_path"] = ""
    start = {}
    started = asyncio.Event()
    body_chunks = asyncio.Queue(maxsize=1)

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
            started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                await body_chunks.put(body)

    async def run_app():
        try:
            await DJANGO_APP(scope, request.receive, send)
        finally:
            await body_chunks.put(None)

    app_task = asyncio.create_task(run_app())
    start_task = asyncio.create_task(started.wait())
    done, _pending = await asyncio.wait(
        {app_task, start_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if app_task in done and not started.is_set():
        await app_task
        start_task.cancel()
        return Response(content=b"", status_code=500)
    start_task.cancel()

    async def stream_body():
        try:
            while True:
                chunk = await body_chunks.get()
                if chunk is None:
                    break
                yield chunk
            await app_task
        finally:
            if not app_task.done():
                app_task.cancel()

    response = StreamingResponse(stream_body(), status_code=start["status"])
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
