# SPDX-License-Identifier: AGPL-3.0-only
"""Generate and run basic unit tests for a Sanka-created FastAPI app."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from sanka.runtime.frameworks.django_fastapi import (
    DEFAULT_ARTIFACT_DIR,
    GENERATED_MANIFEST,
    NATIVE_STRATEGY,
    FrameworkMigrationError,
    load_fastapi_plan,
    load_framework_scan,
)

GENERATED_TEST_FILE = "test_generated.py"


def test_fastapi_app(
    root: str | Path = ".",
    *,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Write ``test_generated.py`` next to the applied app and run it."""
    root_path = Path(root).resolve()
    scan = load_framework_scan(root_path, artifact_dir=artifact_dir)
    plan = load_fastapi_plan(root_path, artifact_dir=artifact_dir)
    output_value = str(output) if output is not None else plan.default_output
    output_path = Path(output_value)
    if not output_path.is_absolute():
        output_path = root_path / output_path
    output_path = output_path.resolve()
    manifest_path = output_path / GENERATED_MANIFEST
    if not manifest_path.is_file():
        raise FrameworkMigrationError("generated output is missing; run `sanka apply` first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("source_scan_hash") != scan.scan_hash:
        raise FrameworkMigrationError("generated output does not match the current scan")
    if manifest.get("plan_hash") != plan.plan_hash:
        raise FrameworkMigrationError("generated output does not match the reviewed plan")
    env, allow_writes = _isolated_env(output_path, manifest)
    source = _render_generated_tests(manifest, allow_writes=allow_writes)
    test_path = output_path / GENERATED_TEST_FILE
    test_path.write_text(source, encoding="utf-8")
    compile(source, str(test_path), "exec")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "test_generated", "-v"],
        cwd=output_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    log = (result.stdout or "") + (result.stderr or "")
    ran = _ran_count(log)
    ok = result.returncode == 0
    return {
        "ok": ok,
        "file": str(test_path),
        "tests": ran,
        "allow_writes": allow_writes,
        "mode": plan.mode,
        "scan_hash": scan.scan_hash,
        "plan_hash": plan.plan_hash,
        "output": str(output_path),
        "log": log.strip(),
    }


def _isolated_env(output: Path, manifest: dict[str, Any]) -> tuple[dict[str, str], bool]:
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    database = manifest.get("database") or {}
    if str(database.get("vendor") or "") != "sqlite":
        return env, False
    name = str(database.get("name") or "")
    if not name or name == ":memory:":
        return env, False
    source_root = (output / str(manifest.get("source_root") or ".")).resolve()
    src = Path(name) if Path(name).is_absolute() else source_root / name
    if not src.is_file():
        return env, False
    tmp = Path(tempfile.mkdtemp(prefix="sanka-test-"))
    dest = tmp / src.name
    shutil.copy2(src, dest)
    env["SANKA_TEST_DB"] = str(dest)
    return env, True


def _ran_count(log: str) -> int:
    for line in reversed(log.splitlines()):
        if line.startswith("Ran "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return 0
    return 0


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return slug or "item"


def _route(resource: dict[str, Any], operation: str) -> str | None:
    for item in resource.get("routes") or []:
        if item.get("operation") == operation:
            return str(item["path"])
    return None


def _missing_path(path: str, lookup: str) -> str:
    return path.replace("{" + lookup + "}", "999999")


def _int_sample(spec: dict[str, Any]) -> int:
    value = 1
    if spec.get("min_value") is not None:
        value = max(value, int(spec["min_value"]))
    if spec.get("max_value") is not None:
        value = min(value, int(spec["max_value"]))
    return value


def _value_expr(spec: dict[str, Any]) -> str:
    kind = str(spec.get("kind") or "")
    if kind == "integer" or kind == "related_pk":
        return repr(_int_sample(spec))
    if kind == "decimal":
        places = int(spec.get("decimal_places") or 2)
        return repr(f"{1:.{places}f}")
    if kind == "choice":
        choices = list(spec.get("choices") or [])
        return repr(choices[0] if choices else "sanka")
    if spec.get("unique") or (kind == "char" and not spec.get("has_default")):
        if spec.get("max_length"):
            return f"_unique({int(spec['max_length'])})"
        return "_unique()"
    return repr("sanka-test")


def _payload_expr(fields: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for spec in fields:
        if spec.get("read_only"):
            continue
        if spec.get("kind") == "nested_many":
            child = spec.get("child") or {}
            parts.append(f"{spec['name']!r}: [{_payload_expr(list(child.get('fields') or []))}]")
            continue
        if not spec.get("required") and spec.get("has_default"):
            continue
        parts.append(f"{spec['name']!r}: {_value_expr(spec)}")
    return "{" + ", ".join(parts) + "}"


def _has_required_create_field(fields: list[dict[str, Any]]) -> bool:
    for spec in fields:
        if spec.get("read_only"):
            continue
        if spec.get("kind") == "nested_many" and spec.get("required"):
            return True
        if spec.get("required") and not spec.get("has_default"):
            return True
    return False


def _render_generated_tests(manifest: dict[str, Any], *, allow_writes: bool) -> str:
    entrypoint = str(manifest.get("entrypoint") or "app.py")
    module = Path(entrypoint).stem
    native = manifest.get("mode") == NATIVE_STRATEGY
    methods: list[str] = [
        "    def test_openapi_ok(self) -> None:",
        "        response = self.client.get('/openapi.json')",
        "        self.assertEqual(response.status_code, 200, response.text)",
        "        spec = response.json()",
        "        self.assertIn('paths', spec)",
        "",
        "    def test_openapi_declares_generated_routes(self) -> None:",
        "        paths = self.client.get('/openapi.json').json().get('paths') or {}",
        "        for path in ROUTES:",
        "            self.assertIn(path, paths, path)",
        "",
    ]
    routes = [str(item["path"]) for item in manifest.get("routes") or []]
    if native:
        for root in manifest.get("api_roots") or []:
            path = str(root["path"])
            name = _slug(f"api_root_{path}")
            methods.append(f"    def test_{name}(self) -> None:")
            methods.append(f"        response = self.client.get({path!r})")
            methods.append("        self.assertEqual(response.status_code, 200, response.text)")
            methods.append("        self.assertIsInstance(response.json(), dict)")
            methods.append("")
        for resource in manifest.get("resources") or []:
            view = _slug(str(resource.get("view") or "resource").rsplit(".", 1)[-1])
            auth = resource.get("auth") is not None
            fields = list(resource.get("fields") or [])
            lookup = str(resource.get("lookup") or "pk")
            listed = _route(resource, "list")
            retrieve = _route(resource, "retrieve")
            create = _route(resource, "create")
            destroy = _route(resource, "destroy")
            if listed:
                expected = 401 if auth else 200
                methods.append(f"    def test_{view}_list(self) -> None:")
                methods.append(f"        response = self.client.get({listed!r})")
                methods.append(
                    f"        self.assertEqual(response.status_code, {expected}, response.text)"
                )
                if not auth:
                    methods.append("        self.assertIsInstance(response.json(), list)")
                methods.append("")
            if retrieve:
                missing = _missing_path(retrieve, lookup)
                expected = 401 if auth else 404
                methods.append(f"    def test_{view}_missing_pk(self) -> None:")
                methods.append(f"        response = self.client.get({missing!r})")
                methods.append(
                    f"        self.assertEqual(response.status_code, {expected}, response.text)"
                )
                methods.append("")
            if create and _has_required_create_field(fields):
                expected = 401 if auth else 400
                methods.append(f"    def test_{view}_create_rejects_empty(self) -> None:")
                methods.append(f"        response = self.client.post({create!r}, json={{}})")
                methods.append(
                    f"        self.assertEqual(response.status_code, {expected}, response.text)"
                )
                methods.append("")
            if allow_writes and not auth and create and retrieve:
                body = _payload_expr(fields)
                detail = retrieve
                methods.append(f"    def test_{view}_create_roundtrip(self) -> None:")
                methods.append(f"        created = self.client.post({create!r}, json={body})")
                methods.append("        self.assertEqual(created.status_code, 201, created.text)")
                methods.append('        pk = created.json()["id"]')
                methods.append(f"        detail = {detail!r}.replace('{{{lookup}}}', str(pk))")
                methods.append("        fetched = self.client.get(detail)")
                methods.append("        self.assertEqual(fetched.status_code, 200, fetched.text)")
                if destroy:
                    methods.append("        deleted = self.client.delete(detail)")
                    methods.append(
                        "        self.assertEqual(deleted.status_code, 204, deleted.text)"
                    )
                    methods.append("        missing = self.client.get(detail)")
                    methods.append(
                        "        self.assertEqual(missing.status_code, 404, missing.text)"
                    )
                methods.append("")
    body = "\n".join(methods).rstrip() + "\n"
    return f"""# Generated by Sanka. Basic unit tests for the migrated FastAPI app.
from __future__ import annotations

import unittest
import uuid

from fastapi.testclient import TestClient

from {module} import app

ROUTES = {json.dumps(routes)}


def _unique(max_length: int | None = None) -> str:
    value = "sanka-" + uuid.uuid4().hex[:8]
    if max_length is not None:
        return value[:max_length]
    return value


class GeneratedAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._stack = TestClient(app, follow_redirects=False)
        cls.client = cls._stack.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stack.__exit__(None, None, None)

{body}

if __name__ == "__main__":
    unittest.main()
"""
