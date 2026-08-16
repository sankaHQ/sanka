# SPDX-License-Identifier: AGPL-3.0-only
"""The ``ferry`` CLI: plan / validate / apply / verify / status, plus the
``ferry migrate SRC DST`` shorthand.

Spec-driven flow (migration-as-code)::

    ferry plan     -f ferry.yaml
    ferry validate -f ferry.yaml
    ferry apply    -f ferry.yaml
    ferry verify   -f ferry.yaml

``validate`` is write-free by construction: it samples live source records
through the reviewed plan and reports rejects without ever resolving the
destination connector, exiting non-zero when invalid records exist.

Shorthand::

    ferry migrate ./content sqlite://content.db

Run state lives in a local SQLite file (default ``.ferry/state.db``), so
``apply`` resumes where an interrupted run stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from ferry.runtime.__about__ import __version__
from ferry.runtime.engine import ExecutionError, MigrationEngine, VerifyReport
from ferry.runtime.execution import DEFAULT_VALIDATION_SAMPLE_SIZE
from ferry.runtime.planner import MigrationPlan
from ferry.runtime.registry import ConnectorRegistry, UnknownConnectorError
from ferry.runtime.spec import EndpointSpec, MigrationSpec, SpecError
from ferry.runtime.state import SqliteStateStore

DEFAULT_SPEC_FILE = "ferry.yaml"
DEFAULT_STATE_FILE = ".ferry/state.db"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return int(asyncio.run(args.handler(args)))
    except (SpecError, UnknownConnectorError, ExecutionError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ferry",
        description="Ferry — plan, execute, and verify finite migrations.",
    )
    parser.add_argument("--version", action="version", version=f"ferry {__version__}")
    parser.set_defaults(command=None)
    commands = parser.add_subparsers(dest="command")

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("-f", "--file", default=DEFAULT_SPEC_FILE, help="migration spec YAML")
        sub.add_argument("--state", default=DEFAULT_STATE_FILE, help="run-state SQLite file")

    plan = commands.add_parser("plan", help="inspect source/target and produce a reviewable plan")
    common(plan)
    plan.set_defaults(handler=_cmd_plan)

    validate = commands.add_parser(
        "validate",
        help="validate sampled source records against the plan without writing",
        description="Validate sampled source records against the plan without writing.",
    )
    common(validate)
    validate.add_argument(
        "--sample",
        type=int,
        default=DEFAULT_VALIDATION_SAMPLE_SIZE,
        help="records sampled per route",
    )
    validate.add_argument(
        "--full", action="store_true", help="validate every source record, not a sample"
    )
    validate.add_argument(
        "--json", action="store_true", help="print the raw validation payload as JSON"
    )
    validate.set_defaults(handler=_cmd_validate)

    apply_ = commands.add_parser("apply", help="execute the reviewed plan (resumable)")
    common(apply_)
    apply_.add_argument("--plan-hash", default=None, help="require this exact plan hash")
    apply_.set_defaults(handler=_cmd_apply)

    verify = commands.add_parser("verify", help="verify the target against the source and ledger")
    common(verify)
    verify.set_defaults(handler=_cmd_verify)

    status = commands.add_parser("status", help="show run status and ledger counts")
    common(status)
    status.set_defaults(handler=_cmd_status)

    migrate = commands.add_parser("migrate", help="plan + apply + verify in one go")
    migrate.add_argument("source", help="source (directory, file, or URL-style endpoint)")
    migrate.add_argument("target", help="target (URL-style endpoint, e.g. sqlite://out.db)")
    migrate.add_argument("--state", default=DEFAULT_STATE_FILE, help="run-state SQLite file")
    migrate.add_argument("-y", "--yes", action="store_true", help="apply without confirmation")
    migrate.set_defaults(handler=_cmd_migrate)

    return parser


def _engine(state_path: str) -> MigrationEngine:
    return MigrationEngine(
        store=SqliteStateStore(state_path), registry=ConnectorRegistry.discover()
    )


def _load_spec(path: str) -> MigrationSpec:
    spec_path = Path(path)
    if not spec_path.is_file():
        raise FileNotFoundError(f"spec file not found: {spec_path}")
    return MigrationSpec.from_yaml(spec_path.read_text(encoding="utf-8"))


async def _cmd_plan(args: argparse.Namespace) -> int:
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    plan = await engine.plan(run_id)
    _print_plan(run_id, plan)
    return 0


async def _cmd_validate(args: argparse.Namespace) -> int:
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    run = engine.store.get_run(run_id)
    if run.plan_json is None:
        raise ExecutionError("no plan for this spec yet; run `ferry plan` first")
    payload = await engine.validate(run_id, sample_size=args.sample, full=args.full)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_validation(run_id, payload)
    return 1 if _validation_invalid(payload) else 0


async def _cmd_apply(args: argparse.Namespace) -> int:
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    run = engine.store.get_run(run_id)
    if run.plan_json is None:
        raise ExecutionError("no plan for this spec yet; run `ferry plan` first")
    await engine.apply(run_id, plan_hash=args.plan_hash)
    print(f"run {run_id}: applied")
    return 0


async def _cmd_verify(args: argparse.Namespace) -> int:
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    report = await engine.verify(run_id)
    _print_verify(report)
    return 0 if report.ok else 1


async def _cmd_status(args: argparse.Namespace) -> int:
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    run = engine.store.get_run(run_id)
    print(f"run {run.id}  status={run.status}  spec={run.spec_hash[:19]}…")
    if run.plan_hash:
        print(f"plan {run.plan_hash}")
    summary = engine.store.ledger_summary(run.id)
    for route_key, counts in sorted(summary.items()):
        rendered = "  ".join(f"{status}={n}" for status, n in sorted(counts.items()))
        print(f"  {route_key}: {rendered}")
    return 0


async def _cmd_migrate(args: argparse.Namespace) -> int:
    spec = MigrationSpec(
        source=_infer_endpoint(args.source, role="source"),
        target=_infer_endpoint(args.target, role="target"),
    )
    engine = _engine(args.state)
    run_id = engine.create(spec)
    plan = await engine.plan(run_id)
    _print_plan(run_id, plan)
    if not args.yes:
        answer = input("Apply this plan? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("aborted; nothing was written")
            return 1
    await engine.apply(run_id, plan_hash=plan.plan_hash)
    report = await engine.verify(run_id)
    _print_verify(report)
    return 0 if report.ok else 1


def _infer_endpoint(value: str, *, role: str) -> EndpointSpec:
    """Map a CLI shorthand to an endpoint: paths → file connectors, URL
    schemes → the connector named by the scheme."""
    if "://" in value:
        scheme, _, rest = value.partition("://")
        scheme = scheme.lower()
        if scheme == "sqlite":
            return EndpointSpec(type="sqlite", connection=rest)
        if scheme in {"postgres", "postgresql"}:
            return EndpointSpec(type="postgres", connection=value)
        if scheme == "clickhouse":
            return EndpointSpec(type="clickhouse", connection=value)
        return EndpointSpec(type=scheme, connection=value)
    path = Path(value)
    if path.is_dir():
        return EndpointSpec(type="markdown", connection=str(path))
    if path.suffix in {".db", ".sqlite", ".sqlite3"}:
        return EndpointSpec(type="sqlite", connection=str(path))
    raise SpecError(
        f"cannot infer the {role} connector from {value!r}; use a URL-style endpoint"
        " (e.g. sqlite://out.db) or a directory path"
    )


def _print_plan(run_id: str, plan: MigrationPlan) -> None:
    print(f"run {run_id}")
    print(f"{plan.source_provider} -> {plan.target_provider}")
    print()
    for route in plan.routes:
        print(
            f"  {route.source_object} -> {route.target_object}"
            f"  ({route.estimated_count} records, {len(route.field_mappings)} fields,"
            f" identity: {route.identity_field})"
        )
    if plan.warnings:
        print()
        print("warnings:")
        for warning in plan.warnings:
            print(f"  - {warning}")
    print()
    print(f"ready: {plan.ready:.0%}")
    print(f"plan hash: {plan.plan_hash}")


def _validation_invalid(payload: dict[str, Any]) -> bool:
    return any(int(row.get("invalid") or 0) > 0 for row in payload.get("objects") or [])


def _print_validation(run_id: str, payload: dict[str, Any]) -> None:
    rows = payload.get("objects") or []
    verdict = "FAILED" if _validation_invalid(payload) else "OK"
    print(f"run {run_id}: validation {verdict} (write-free)")
    for row in rows:
        marker = "INVALID" if int(row.get("invalid") or 0) > 0 else "ok"
        print(
            f"  {row['sourceObject']} -> {row['destinationObject']}:"
            f" sampled={row['sampled']} valid={row['valid']} invalid={row['invalid']}  [{marker}]"
        )
        for reason in row.get("invalidReasons") or []:
            fields = (
                f"  ({reason['sourceField']} -> {reason['targetField']})"
                if reason.get("sourceField") and reason.get("targetField")
                else ""
            )
            print(f"    - {reason['count']}x {reason['code']}: {reason['message']}{fields}")
    warnings = payload.get("warnings") or []
    if warnings:
        print("warnings:")
        for warning in warnings:
            print(f"  - {warning}")


def _print_verify(report: VerifyReport) -> None:
    print(f"run {report.run_id}: verification {'OK' if report.ok else 'FAILED'}")
    for route in report.routes:
        source = "?" if route.source_count is None else str(route.source_count)
        target = "?" if route.destination_count is None else str(route.destination_count)
        marker = "ok" if route.ok else "MISMATCH"
        print(
            f"  {route.route_key}: source={source} migrated={route.migrated}"
            f" failed={route.failed} destination={target}  [{marker}]"
        )
