# SPDX-License-Identifier: AGPL-3.0-only
"""Human-readable summaries for local lifecycle results.

The JSON document (``--json``) is the contract; these lines only restate the
fields a person needs after each step. Every accessor tolerates missing keys so
an older or newer extension response never breaks the text output.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

_FRAMEWORK_LABELS = {
    "django-rest-framework": "DRF",
    "drf": "DRF",
    "fastapi": "FastAPI",
    "flask": "Flask",
}
_ENGINE_LABELS = {
    "tortoise": "Tortoise ORM",
    "sqlalchemy": "SQLAlchemy",
    "psycopg": "psycopg",
}


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    return None


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def _label(value: Any, table: Mapping[str, str]) -> str:
    text = str(value) if value is not None else ""
    return table.get(text.lower(), text)


def _relative(path: Any, root: str | Path) -> str:
    text = str(path) if path is not None else ""
    try:
        return str(Path(text).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return text


def _scan_lines(data: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    detected: list[tuple[str, str]] = []
    for key, name in (
        ("python_version", "Python"),
        ("django_version", "Django"),
        ("drf_version", "DRF"),
    ):
        value = data.get(key)
        if isinstance(value, str) and value:
            detected.append((name, value))
    database = data.get("database")
    if isinstance(database, Mapping):
        vendor = database.get("vendor")
        db_name = database.get("name")
        if vendor:
            detected.append(("Database", f"{vendor} ({db_name})" if db_name else str(vendor)))
    if detected:
        lines.append("Detected")
        lines.extend(f"  {name:<10} {value}" for name, value in detected)
    counts = [
        (_count(data.get("routes")), "route", "routes"),
        (_count(data.get("serializers")), "serializer", "serializers"),
        (_count(data.get("models")), "model", "models"),
        (_count(data.get("permissions")), "permission class", "permission classes"),
        (_count(data.get("test_files")), "test file", "test files"),
    ]
    present = [(n, s, p) for n, s, p in counts if n is not None]
    if present:
        if lines:
            lines.append("")
        lines.append("Application")
        lines.extend(f"  {_plural(n, s, p)}" for n, s, p in present)
    skipped = _count(data.get("skipped_routes"))
    if skipped:
        lines.append(f"  {_plural(skipped, 'skipped route')}")
    scan_hash = data.get("scan_hash")
    if isinstance(scan_hash, str) and scan_hash:
        if lines:
            lines.append("")
        lines.append(f"scan hash: {scan_hash}")
    return lines


def _target_from_extension_id(extension_id: Any) -> str | None:
    """``sanka/drf-to-fastapi`` names its target after ``-to-``; other ids give nothing."""
    if not isinstance(extension_id, str):
        return None
    name = extension_id.rsplit("/", 1)[-1]
    if "-to-" not in name:
        return None
    target = name.rsplit("-to-", 1)[-1].strip()
    return target or None


def _scan_targets(data: Mapping[str, Any]) -> list[str]:
    targets: list[str] = []

    def add(candidate: Any) -> None:
        if isinstance(candidate, str) and candidate and candidate not in targets:
            targets.append(candidate)

    extensions = data.get("extensions")
    if isinstance(extensions, list):
        for entry in extensions:
            if not isinstance(entry, Mapping):
                continue
            explicit = entry.get("targets")
            record = entry.get("extension")
            if not explicit and isinstance(record, Mapping):
                explicit = record.get("targets")
            for candidate in explicit or ():
                add(candidate)
            if not explicit:
                identifier = entry.get("id")
                if identifier is None and isinstance(record, Mapping):
                    identifier = record.get("id")
                add(_target_from_extension_id(identifier))
    return targets


def _plan_lines(data: Mapping[str, Any], root: str | Path) -> list[str]:
    lines: list[str] = []
    source = _label(data.get("source_framework"), _FRAMEWORK_LABELS)
    target = _label(data.get("target_framework"), _FRAMEWORK_LABELS)
    title = f"{source} → {target} plan" if source and target else "Migration plan"
    lines.append(title)
    strategy: list[str] = []
    if data.get("mode"):
        strategy.append(str(data["mode"]))
    if data.get("sql_engine"):
        strategy.append(_label(data["sql_engine"], _ENGINE_LABELS))
    if data.get("generation_mode"):
        strategy.append(f"{data['generation_mode']} generation")
    if data.get("package_manager"):
        strategy.append(str(data["package_manager"]))
    if strategy:
        lines.append(f"  {'Strategy':<10} {' · '.join(strategy)}")
    output = data.get("default_output") or data.get("output")
    if output:
        lines.append(f"  {'Output':<10} {_relative(output, root)}")
    swagger_ui = data.get("swagger_ui")
    if target == "FastAPI" and isinstance(swagger_ui, bool):
        lines.append(f"  {'Swagger UI':<10} {'enabled (/docs)' if swagger_ui else 'disabled'}")
    native = _count(data.get("native_routes"))
    dropped = _count(data.get("dropped_alias_routes"))
    manual = _count(data.get("needs_adaptation_routes"))
    total = _count(data.get("routes"))
    if total is None and None not in (native, dropped, manual):
        total = (native or 0) + (dropped or 0) + (manual or 0)
    if any(value is not None for value in (native, dropped, manual)):
        lines.append("")
        lines.append(f"Routes ({total})" if total is not None else "Routes")
        if native is not None:
            lines.append(f"  {native} generated natively")
        if dropped is not None:
            lines.append(f"  {dropped} format-suffix aliases dropped (disclosed)")
        if manual is not None:
            lines.append(f"  {manual} need manual adaptation")
        eligible = _count(data.get("native_eligible_routes"))
        readiness = data.get("readiness")
        if isinstance(readiness, (int, float)) and not isinstance(readiness, bool):
            percent = round(float(readiness) * 100) if readiness <= 1 else round(float(readiness))
            detail = (
                f" ({native}/{eligible} non-alias routes)" if None not in (native, eligible) else ""
            )
            lines.append(f"  readiness {percent}%{detail}")
    return lines


def _apply_lines(data: Mapping[str, Any], root: str | Path) -> list[str]:
    lines: list[str] = ["Generated"]
    generated = _count(data.get("routes_generated"))
    target = _label(data.get("target_framework"), _FRAMEWORK_LABELS)
    if generated is not None:
        noun = f"native {target} route" if target else "native route"
        lines.append(f"  {_plural(generated, noun)}")
    output = data.get("output")
    if output:
        lines.append(f"  {'Output':<10} {_relative(output, root)}")
    if data.get("sql_engine"):
        lines.append(f"  {'Engine':<10} {_label(data['sql_engine'], _ENGINE_LABELS)}")
    return lines if len(lines) > 1 else []


def _test_lines(data: Mapping[str, Any], root: str | Path) -> list[str]:
    lines: list[str] = ["Generated app tests"]
    ran = _count(data.get("tests"))
    if ran is None:
        log = data.get("log")
        if isinstance(log, str):
            import re

            match = re.search(r"Ran (\d+) tests?", log)
            if match:
                ran = int(match.group(1))
    if ran is not None:
        lines.append(f"  Ran {_plural(ran, 'test')}")
    environment = data.get("environment")
    if environment:
        lines.append(f"  {'Env':<10} {_relative(environment, root)}")
    return lines if len(lines) > 1 else []


def _verify_lines(data: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    mode = data.get("mode")
    lines.append(f"Verified ({mode})" if mode else "Verified")
    routes = data.get("routes")
    if isinstance(routes, Mapping):
        parts: list[str] = []
        generated = _count(routes.get("generated"))
        if generated is not None:
            parts.append(f"{generated} generated")
        for key, text in (
            ("dropped", "aliases dropped"),
            ("needs_adaptation", "need adaptation"),
            ("missing", "missing"),
            ("extra", "extra"),
        ):
            count = _count(routes.get(key))
            if count is not None:
                parts.append(f"{count} {text}")
        if parts:
            lines.append(f"  {'routes':<8} " + " · ".join(parts))
    http = data.get("http")
    if isinstance(http, Mapping) and http.get("enabled", True):
        passed = _count(http.get("passed"))
        probed = _count(http.get("probed"))
        if passed is not None and probed is not None:
            lines.append(f"  {'http':<8} {passed}/{probed} probes passed")
    return lines if len(lines) > 1 else []


def application_summary(
    command: str, data: Mapping[str, Any], *, root: str | Path = "."
) -> tuple[list[str], str | None]:
    """Return (summary lines, next-command hint) for a successful lifecycle step."""
    if not isinstance(data, Mapping):
        return [], None
    if command == "scan":
        targets = _scan_targets(data)
        target = targets[0] if len(targets) == 1 else "<target>"
        return _scan_lines(data), f"sanka plan . --to {target}"
    if command == "plan":
        plan_hash = data.get("plan_hash")
        hint = (
            f"sanka apply --root . --plan-hash {plan_hash}"
            if isinstance(plan_hash, str) and plan_hash
            else "sanka apply --root . --plan-hash <hash printed above>"
        )
        return _plan_lines(data, root), hint
    if command == "apply":
        return _apply_lines(data, root), "sanka test ."
    if command == "test":
        return _test_lines(data, root), "sanka verify ."
    if command == "verify":
        return _verify_lines(data), None
    return [], None
