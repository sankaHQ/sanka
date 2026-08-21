# SPDX-License-Identifier: AGPL-3.0-only
"""Django REST Framework to FastAPI compatibility migration recipe.

The v0.1 recipe is intentionally a strangler bridge. It creates a real FastAPI
route graph while dispatching each route into the existing Django application
in-process. That preserves observable behavior first; teams can then replace
individual bridge handlers with native FastAPI implementations without a
flag-day rewrite.
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
    FrameworkPlan,
    FrameworkRisk,
    FrameworkScan,
    PlannedRoute,
    RouteIR,
)

DEFAULT_ARTIFACT_DIR = ".sanka"
DEFAULT_FASTAPI_OUTPUT = ".sanka/output/fastapi"
SCAN_FILE = "scan.json"
PLAN_FILE = "plan-fastapi.json"
GENERATED_MANIFEST = "sanka-manifest.json"


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
    django_urls = importlib.import_module("django.urls")
    resolver = django_urls.get_resolver()
    routes, risks = _walk_patterns(resolver.url_patterns, root_path=root_path)
    if not routes:
        raise FrameworkMigrationError(
            "no Django REST Framework routes were detected; check DJANGO_SETTINGS_MODULE"
        )
    serializers = sorted({value for route in routes if (value := route.serializer)})
    models = sorted({value for route in routes if (value := route.model)})
    permissions = sorted({value for route in routes for value in route.permissions})
    authentication = sorted({value for route in routes for value in route.authentication})
    scan = FrameworkScan(
        schema_version=1,
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
) -> FrameworkPlan:
    root_path = Path(root).resolve()
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    routes = tuple(
        PlannedRoute(
            method=route.method,
            path=route.path,
            operation=route.operation,
            source_view=route.view,
            strategy="django-in-process-compatibility-bridge",
            automatic=route.supported,
        )
        for route in scan.routes
    )
    plan = FrameworkPlan(
        schema_version=1,
        source_framework=scan.framework,
        target_framework="fastapi",
        mode="compatibility",
        source_scan_hash=scan.scan_hash,
        settings_module=scan.settings_module,
        routes=routes,
        risks=scan.risks,
        retained=(
            "Django models and migrations",
            "Django ORM and synchronous transactions",
            "Django authentication and permissions",
            "DRF handlers behind the generated compatibility bridge",
        ),
        default_output=output,
    ).with_hash()
    _write_json(_artifact_path(root_path, artifact_dir, PLAN_FILE), plan.to_dict())
    return plan


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
    automatic = [route for route in plan.routes if route.automatic]
    manifest = {
        "schema_version": 1,
        "generator": "sanka",
        "mode": plan.mode,
        "source_scan_hash": plan.source_scan_hash,
        "plan_hash": plan.plan_hash,
        "settings_module": plan.settings_module,
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
    }
    relative_source = os.path.relpath(root_path, output_path)
    _write_text(output_path / "app.py", _render_app())
    _write_text(output_path / "sanka_compat.py", _render_compatibility_runtime())
    _write_text(output_path / "README.md", _render_generated_readme(plan))
    _write_text(
        output_path / "requirements.txt",
        "fastapi>=0.115,<1\nuvicorn[standard]>=0.30,<1\n",
    )
    manifest["source_root"] = relative_source
    _write_json(output_path / GENERATED_MANIFEST, manifest)
    return output_path, len(automatic)


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
    expected = {route.key for route in plan.routes if route.automatic}
    needs_adaptation = sorted(route.key for route in plan.routes if not route.automatic)
    actual = {
        f"{str(route['method']).upper()} {route['path']}" for route in manifest.get("routes", [])
    }
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    _compile_generated_files(output_path)
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
        "routes": {
            "scanned": len(scan.routes),
            "planned": len(plan.routes),
            "generated": len(actual),
            "missing": missing,
            "extra": extra,
            "needs_adaptation": needs_adaptation,
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


def _walk_patterns(
    patterns: Iterable[Any], *, root_path: Path, prefix: str = ""
) -> tuple[list[RouteIR], list[FrameworkRisk]]:
    routes: list[RouteIR] = []
    risks: list[FrameworkRisk] = []
    for pattern in patterns:
        raw = str(pattern.pattern)
        combined = f"{prefix}{raw}"
        nested = getattr(pattern, "url_patterns", None)
        if nested is not None:
            nested_routes, nested_risks = _walk_patterns(
                nested, root_path=root_path, prefix=combined
            )
            routes.extend(nested_routes)
            risks.extend(nested_risks)
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
            risks.append(
                FrameworkRisk(
                    severity="high",
                    code="SANKA_DRF_DYNAMIC_ROUTE",
                    message=f"Route pattern requires manual adaptation: {combined}",
                    file=source_file,
                    line=source_line,
                )
            )
        for method, operation in methods:
            operation_source = _safe_source(getattr(view_class, operation, None))
            transactional = (
                "transaction.atomic" in operation_source or "@atomic" in operation_source
            )
            routes.append(
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
                )
            )
    deduplicated = {route.key: route for route in routes}
    return list(deduplicated.values()), risks


def _is_drf_view(view_class: type[Any]) -> bool:
    return any(base.__module__.startswith("rest_framework.") for base in inspect.getmro(view_class))


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


def _compile_generated_files(output: Path) -> None:
    for name in ("app.py", "sanka_compat.py"):
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
