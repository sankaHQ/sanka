# SPDX-License-Identifier: AGPL-3.0-only
"""The ``sanka`` CLI: scan / plan / validate / apply / test / verify / status.

Spec-driven flow (migration-as-code)::

    sanka plan     -f sanka-migrate.yaml
    sanka validate -f sanka-migrate.yaml
    sanka apply --plan-hash sha256:... -f sanka-migrate.yaml
    sanka verify   -f sanka-migrate.yaml

``validate`` is write-free by construction: it samples live source records
through the reviewed plan and reports rejects without ever resolving the
destination connector, exiting non-zero when invalid records exist.

Shorthand and provider selection::

    sanka connect markdown
    sanka migrate ./content sqlite://content.db

Django REST Framework to FastAPI compatibility flow::

    sanka scan
    sanka plan --to fastapi
    sanka apply --plan-hash sha256:...
    sanka test
    sanka verify

Run state lives in a local SQLite file (default ``.sanka/migrate/state.db``), so
``apply`` resumes where an interrupted run stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from sanka.cli._research import (
    SankaMigrateApiClient,
    SankaMigrateApiError,
    print_research,
    signup_url,
)
from sanka.runtime.__about__ import __version__
from sanka.runtime.engine import ExecutionError, MigrationEngine, VerifyReport
from sanka.runtime.execution import DEFAULT_VALIDATION_SAMPLE_SIZE
from sanka.runtime.frameworks import (
    COMPATIBILITY_STRATEGY,
    DEFAULT_ARTIFACT_DIR,
    DEFAULT_FASTAPI_OUTPUT,
    NATIVE_STRATEGY,
    FrameworkMigrationError,
    GeneratedEnvironmentError,
    apply_fastapi_plan,
    load_fastapi_plan,
    plan_fastapi,
    scan_django,
    test_fastapi_app,
    verify_fastapi_migration,
    write_bench_candidate,
    write_gap_report,
)
from sanka.runtime.frameworks.model import FrameworkPlan, FrameworkScan
from sanka.runtime.frameworks.native_async import (
    DEFAULT_SQL_ENGINE,
    SQL_ENGINE_LABELS,
    SQL_ENGINES,
)
from sanka.runtime.planner import MigrationPlan
from sanka.runtime.registry import ConnectorRegistry, UnknownConnectorError
from sanka.runtime.spec import EndpointSpec, MigrationSpec, SpecError
from sanka.runtime.state import SqliteStateStore

DEFAULT_SPEC_FILE = "sanka-migrate.yaml"
DEFAULT_STATE_FILE = ".sanka/migrate/state.db"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return int(asyncio.run(args.handler(args)))
    except (
        SpecError,
        UnknownConnectorError,
        ExecutionError,
        FrameworkMigrationError,
        GeneratedEnvironmentError,
        FileNotFoundError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except SankaMigrateApiError as error:
        retry = f"; retry after {error.retry_after}s" if error.retry_after else ""
        print(f"error: {error.code}: {error.message}{retry}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sanka",
        description="Sanka — inspect, plan, execute, and verify migrations with a finish line.",
    )
    parser.add_argument("--version", action="version", version=f"sanka {__version__}")
    parser.set_defaults(command=None)
    commands = parser.add_subparsers(dest="command")

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("-f", "--file", default=DEFAULT_SPEC_FILE, help="migration spec YAML")
        sub.add_argument("--state", default=DEFAULT_STATE_FILE, help="run-state SQLite file")

    scan = commands.add_parser(
        "scan", help="inspect a source application and write its semantic scan artifact"
    )
    scan.add_argument("root", nargs="?", default=".", help="Django repository root")
    scan.add_argument("--settings", help="Django settings module (auto-detected by default)")
    scan.add_argument("--artifact-dir", default=DEFAULT_ARTIFACT_DIR)
    scan.add_argument(
        "--trust-source-code",
        action="store_true",
        help="allow unsandboxed Django startup only when no OS sandbox is available",
    )
    scan.add_argument("--json", action="store_true", help="print the scan artifact as JSON")
    scan.set_defaults(handler=_cmd_scan)

    plan = commands.add_parser("plan", help="inspect source/target and produce a reviewable plan")
    common(plan)
    plan.add_argument("root", nargs="?", default=".", help="application repository root")
    plan.add_argument("--to", choices=("fastapi",), help="target application framework")
    plan.add_argument(
        "--strategy",
        choices=(NATIVE_STRATEGY, COMPATIBILITY_STRATEGY),
        default=NATIVE_STRATEGY,
        help="native FastAPI generation (default) or the in-process compatibility bridge",
    )
    plan.add_argument("--artifact-dir", default=DEFAULT_ARTIFACT_DIR)
    plan.add_argument("--output", default=DEFAULT_FASTAPI_OUTPUT)
    plan.add_argument(
        "--orm",
        choices=SQL_ENGINES,
        default=None,
        help="async SQL engine for native FastAPI (default: tortoise, closest to Django)",
    )
    plan.add_argument("--json", action="store_true", help="print the framework plan as JSON")
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
    apply_.add_argument(
        "--plan-hash",
        required=True,
        help="exact reviewed plan hash printed by `sanka plan`",
    )
    apply_.add_argument("--to", choices=("fastapi",), help="select an application plan")
    apply_.add_argument("--root", default=".", help="application repository root")
    apply_.add_argument("--artifact-dir", default=DEFAULT_ARTIFACT_DIR)
    apply_.add_argument("--output", default=None, help="generated FastAPI output directory")
    apply_.add_argument("--force", action="store_true", help="replace existing generated files")
    apply_.add_argument(
        "--orm",
        choices=SQL_ENGINES,
        default=None,
        help="async SQL engine (prompts when interactive; tortoise is recommended)",
    )
    apply_.add_argument(
        "--min-readiness",
        type=float,
        default=50.0,
        metavar="PCT",
        help="minimum native readiness percentage for generation (default: 50; "
        "use 0 to explicitly allow a partial scaffold)",
    )
    apply_.add_argument(
        "--gap-report-only",
        action="store_true",
        help="write GAP-REPORT.md and the reviewed plan instead of generating an app",
    )
    apply_.add_argument(
        "--bench-candidate",
        default=None,
        help="also emit a Sanka Migration Bench candidate (overlay + candidate.yaml) here",
    )
    apply_.set_defaults(handler=_cmd_apply)

    test = commands.add_parser(
        "test", help="generate and run unit tests for the created FastAPI app"
    )
    common(test)
    test.add_argument("--root", default=".", help="application repository root")
    test.add_argument("--to", choices=("fastapi",), help="select an application plan")
    test.add_argument("--artifact-dir", default=DEFAULT_ARTIFACT_DIR)
    test.add_argument("--output", default=None, help="generated FastAPI output directory")
    test.add_argument("--json", action="store_true", help="print the test report as JSON")
    test.set_defaults(handler=_cmd_test)

    verify = commands.add_parser("verify", help="verify the target against the source and ledger")
    common(verify)
    verify.add_argument("--root", default=".", help="application repository root")
    verify.add_argument("--to", choices=("fastapi",), help="select an application plan")
    verify.add_argument("--artifact-dir", default=DEFAULT_ARTIFACT_DIR)
    verify.add_argument("--output", default=None, help="generated FastAPI output directory")
    verify.add_argument(
        "--cases",
        default=None,
        help="JSON file with additional read-only HTTP verification cases",
    )
    verify.add_argument("--no-http", action="store_true", help="skip safe read-only HTTP probes")
    verify.add_argument(
        "--trust-source-code",
        action="store_true",
        help="allow unsandboxed source probes only when no OS sandbox is available",
    )
    verify.add_argument("--json", action="store_true", help="print framework verification as JSON")
    verify.set_defaults(handler=_cmd_verify)

    status = commands.add_parser("status", help="show run status and ledger counts")
    common(status)
    status.set_defaults(handler=_cmd_status)

    migrate = commands.add_parser("migrate", help="plan + apply + verify in one go")
    migrate.add_argument("source", help="source (directory, file, or URL-style endpoint)")
    migrate.add_argument("target", help="target (URL-style endpoint, e.g. sqlite://out.db)")
    migrate.add_argument("--state", default=DEFAULT_STATE_FILE, help="run-state SQLite file")
    migrate.set_defaults(handler=_cmd_migrate)

    connect = commands.add_parser(
        "connect",
        help="select an installed connector and show its supported migration roles",
    )
    connect.add_argument("provider", help="local provider slug, e.g. markdown or postgres")
    connect.add_argument("--json", action="store_true", help="print provider details as JSON")
    connect.set_defaults(handler=_cmd_connect)

    research = commands.add_parser(
        "research",
        help="query cited Sanka lifecycle, cost, and comparison research",
    )
    research_commands = research.add_subparsers(dest="research_command", required=True)

    eol = research_commands.add_parser("eol", help="query software lifecycle events")
    eol.add_argument("product", nargs="?")
    eol.add_argument("--category")
    eol.add_argument(
        "--type",
        choices=("shutdown", "end_of_support", "maintenance_end", "version_lifecycle"),
    )
    eol.add_argument("--after", help="inclusive YYYY-MM lower bound")
    eol.add_argument("--before", help="inclusive YYYY-MM upper bound")
    _research_output_options(eol)
    eol.set_defaults(handler=_cmd_research_eol)

    tco = research_commands.add_parser("tco", help="query published cost benchmarks")
    tco.add_argument("product", nargs="?")
    tco.add_argument("--category")
    _research_output_options(tco)
    tco.set_defaults(handler=_cmd_research_tco)

    compare = research_commands.add_parser("compare", help="compare migration capabilities")
    compare.add_argument("category")
    compare.add_argument("--platforms", help="comma-separated platform slugs (maximum 10)")
    _research_output_options(compare)
    compare.set_defaults(handler=_cmd_research_compare)

    assess = commands.add_parser("assess", help="submit a free migration assessment")
    assess.add_argument("--source")
    assess.add_argument("--destination")
    assess.add_argument("--volume")
    assess.add_argument("--timing")
    assess.add_argument("--concerns")
    assess.add_argument("--category")
    assess.add_argument("--lang", choices=("en", "ja"), default="en")
    assess.add_argument("--json", action="store_true", help="print the API data payload as JSON")
    assess.set_defaults(handler=_cmd_assess, form_started_at_ms=int(time.time() * 1000))

    return parser


def _research_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lang", choices=("en", "ja"), dest="locale")
    parser.add_argument("--json", action="store_true", help="print the API data payload as JSON")


def _research_client() -> SankaMigrateApiClient:
    return SankaMigrateApiClient()


def _engine(state_path: str) -> MigrationEngine:
    return MigrationEngine(
        store=SqliteStateStore(state_path), registry=ConnectorRegistry.discover()
    )


async def _cmd_connect(args: argparse.Namespace) -> int:
    registry = ConnectorRegistry.discover()
    provider = str(args.provider).strip().lower()
    if provider == "postgresql":
        provider = "postgres"
    roles = list(registry.roles(provider))
    payload = {
        "provider": provider,
        "roles": roles,
        "installed": True,
        "package": f"sanka-connector-{provider}",
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{provider}: ready ({', '.join(roles)})")
        print(f"provided by installed package sanka-connector-{provider}")
    return 0


def _load_spec(path: str) -> MigrationSpec:
    spec_path = Path(path)
    if not spec_path.is_file():
        raise FileNotFoundError(f"spec file not found: {spec_path}")
    return MigrationSpec.from_yaml(spec_path.read_text(encoding="utf-8"))


async def _cmd_plan(args: argparse.Namespace) -> int:
    if args.to == "fastapi":
        framework_plan = plan_fastapi(
            args.root,
            artifact_dir=args.artifact_dir,
            output=args.output,
            strategy=args.strategy,
            sql_engine=args.orm,
        )
        if args.json:
            print(json.dumps(framework_plan.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_framework_plan(framework_plan)
        return 0
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    migration_plan = await engine.plan(run_id)
    _print_plan(run_id, migration_plan)
    return 0


async def _cmd_validate(args: argparse.Namespace) -> int:
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    run = engine.store.get_run(run_id)
    if run.plan_json is None:
        raise ExecutionError("no plan for this spec yet; run `sanka plan` first")
    payload = await engine.validate(run_id, sample_size=args.sample, full=args.full)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_validation(run_id, payload)
    return 1 if _validation_invalid(payload) else 0


async def _cmd_apply(args: argparse.Namespace) -> int:
    if _use_framework_lifecycle(args.root, args.file, args.artifact_dir, args.to):
        sql_engine = args.orm
        plan = load_fastapi_plan(args.root, artifact_dir=args.artifact_dir)
        if plan.mode == NATIVE_STRATEGY:
            if not 0 <= args.min_readiness <= 100:
                raise FrameworkMigrationError("--min-readiness must be between 0 and 100")
            print(
                f"native readiness: {plan.readiness:.0%} "
                f"({plan.native_routes}/{plan.native_eligible_routes} non-alias routes); "
                f"{plan.needs_adaptation_routes} route(s) need manual adaptation"
            )
            below_min = plan.readiness * 100.0 < args.min_readiness
            if args.gap_report_only or below_min or plan.native_routes == 0:
                destination = args.bench_candidate or args.output or "gap-report"
                report_path = write_gap_report(
                    args.root, destination, artifact_dir=args.artifact_dir
                )
                if args.gap_report_only:
                    print(f"gap report written to {report_path}")
                    return 0
                reason = (
                    "the native plan contains no generatable routes"
                    if plan.native_routes == 0
                    else f"readiness is below --min-readiness {args.min_readiness:g}%"
                )
                print(f"not applying: {reason}; gap report written to {report_path}")
                return 1
        if plan.mode != NATIVE_STRATEGY and args.gap_report_only:
            raise FrameworkMigrationError(
                "gap reports describe native plans; run `sanka plan --to fastapi`"
            )
        if plan.mode == NATIVE_STRATEGY and sql_engine is None and sys.stdin.isatty():
            sql_engine = _prompt_sql_engine(plan.sql_engine)
        output, routes = apply_fastapi_plan(
            args.root,
            artifact_dir=args.artifact_dir,
            output=args.output,
            plan_hash=args.plan_hash,
            force=args.force,
            sql_engine=sql_engine,
        )
        if plan.mode == NATIVE_STRATEGY:
            engine = sql_engine or plan.sql_engine
            print(f"generated {routes} native FastAPI routes in {output}")
            print(f"SQL engine: {engine}")
        else:
            print(f"generated {routes} FastAPI routes in {output}")
        print(f"applied plan: {plan.plan_hash}")
        if args.bench_candidate:
            candidate = write_bench_candidate(
                args.root,
                args.bench_candidate,
                artifact_dir=args.artifact_dir,
            )
            print(f"benchmark candidate written to {candidate}")
            print(
                "candidate discloses its gaps: GAP-REPORT.md, plan-fastapi.json, verify-report.json"
            )
        print("next: sanka test")
        return 0
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    run = engine.store.get_run(run_id)
    if run.plan_json is None:
        raise ExecutionError("no plan for this spec yet; run `sanka plan` first")
    await engine.apply(run_id, plan_hash=args.plan_hash)
    print(f"run {run_id}: applied")
    return 0


async def _cmd_test(args: argparse.Namespace) -> int:
    if not _use_framework_lifecycle(args.root, args.file, args.artifact_dir, args.to):
        raise FrameworkMigrationError(
            "`sanka test` runs unit tests for a FastAPI apply; run `sanka apply` first"
        )
    report = await asyncio.to_thread(
        lambda: test_fastapi_app(
            args.root,
            artifact_dir=args.artifact_dir,
            output=args.output,
        )
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_framework_test(report)
    return 0 if report["ok"] else 1


async def _cmd_verify(args: argparse.Namespace) -> int:
    if _use_framework_lifecycle(args.root, args.file, args.artifact_dir, args.to):
        # The framework verifier still boots Django for the source Client;
        # run it off the CLI event loop so Django's async-context guard stays quiet.
        framework_report = await asyncio.to_thread(
            lambda: verify_fastapi_migration(
                args.root,
                artifact_dir=args.artifact_dir,
                output=args.output,
                probe_http=not args.no_http,
                cases=args.cases,
                allow_unsafe_source_execution=args.trust_source_code,
            )
        )
        if args.json:
            print(json.dumps(framework_report, ensure_ascii=False, indent=2))
        else:
            _print_framework_verify(framework_report)
        return 0 if framework_report["ok"] else 1
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    verify_report = await engine.verify(run_id)
    _print_verify(verify_report)
    return 0 if verify_report.ok else 1


async def _cmd_scan(args: argparse.Namespace) -> int:
    scan = scan_django(
        args.root,
        settings_module=args.settings,
        artifact_dir=args.artifact_dir,
        allow_unsafe_source_execution=args.trust_source_code,
    )
    if args.json:
        print(json.dumps(scan.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_framework_scan(scan)
    return 0


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
    answer = input("Apply this exact plan? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        print("aborted; nothing was written")
        return 1
    await engine.apply(run_id, plan_hash=plan.plan_hash)
    report = await engine.verify(run_id)
    _print_verify(report)
    return 0 if report.ok else 1


async def _cmd_research_eol(args: argparse.Namespace) -> int:
    data = await asyncio.to_thread(
        _research_client().research_eol,
        product=args.product,
        category=args.category,
        event_type=args.type,
        after=args.after,
        before=args.before,
        locale=args.locale,
    )
    return _print_research_result(data, kind="eol", locale=args.locale, as_json=args.json)


async def _cmd_research_tco(args: argparse.Namespace) -> int:
    data = await asyncio.to_thread(
        _research_client().research_tco,
        product=args.product,
        category=args.category,
        locale=args.locale,
    )
    return _print_research_result(data, kind="tco", locale=args.locale, as_json=args.json)


async def _cmd_research_compare(args: argparse.Namespace) -> int:
    if args.platforms and len([item for item in args.platforms.split(",") if item.strip()]) > 10:
        raise SankaMigrateApiError(
            "SANKA_MIGRATE_INPUT_INVALID",
            "--platforms accepts at most 10 comma-separated values.",
        )
    data = await asyncio.to_thread(
        _research_client().research_compare,
        category=args.category,
        platforms=args.platforms,
        locale=args.locale,
    )
    return _print_research_result(data, kind="compare", locale=args.locale, as_json=args.json)


async def _cmd_assess(args: argparse.Namespace) -> int:
    answers = [
        args.source,
        args.destination,
        args.volume,
        args.timing,
        args.concerns,
        args.category,
    ]
    if not any(value is not None for value in answers):
        args.source = input("Source system: ").strip()
        args.destination = input("Destination system: ").strip()
        args.volume = input("Data volume: ").strip()
        args.timing = input("Target timing: ").strip()
        args.concerns = input("Main concerns: ").strip()
    if not str(args.source or "").strip():
        raise SankaMigrateApiError(
            "SANKA_MIGRATE_INPUT_INVALID",
            "--source is required outside interactive mode.",
        )
    elapsed_ms = int(time.time() * 1000) - int(args.form_started_at_ms)
    if elapsed_ms < 2000:
        await asyncio.sleep((2000 - elapsed_ms) / 1000)
    data = await asyncio.to_thread(
        _research_client().assess,
        {
            "source": args.source,
            "destination": args.destination or "",
            "volume": args.volume or "",
            "timing": args.timing or "",
            "concerns": args.concerns or "",
            "category": args.category or "",
            "lang": args.lang,
            "attribution": {"channel": "cli"},
            "website": "",
            "form_started_at": args.form_started_at_ms,
        },
    )
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    assessment_id = str(data.get("assessment_id") or "")
    if not assessment_id:
        raise SankaMigrateApiError(
            "SANKA_MIGRATE_API_INVALID_RESPONSE",
            "Assessment response did not include an assessment_id.",
        )
    print(f"assessment submitted: {assessment_id}")
    print("next: create your workspace and Sakura will build the grounded report")
    print(f"  {signup_url(assessment_id, lang=args.lang)}")
    return 0


def _print_research_result(
    data: dict[str, Any],
    *,
    kind: str,
    locale: str | None,
    as_json: bool,
) -> int:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if _research_has_results(data, kind=kind) else 2
    return print_research(data, kind=kind, locale=locale)


def _research_has_results(data: dict[str, Any], *, kind: str) -> bool:
    if kind == "eol":
        if isinstance(data.get("events"), list):
            return bool(data["events"])
        product = data.get("product")
        return isinstance(product, dict) and bool(product.get("events"))
    if kind == "tco":
        if isinstance(data.get("benchmarks"), list):
            return bool(data["benchmarks"])
        product = data.get("product")
        return isinstance(product, dict) and bool(product.get("plans"))
    return bool(data.get("platforms"))


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


def _use_framework_lifecycle(
    root: str, spec_file: str, artifact_dir: str, target: str | None
) -> bool:
    """Select the application-migration lifecycle only when no data spec exists."""
    if target == "fastapi":
        return True
    if spec_file != DEFAULT_SPEC_FILE or Path(spec_file).is_file():
        return False
    artifact = Path(artifact_dir)
    if not artifact.is_absolute():
        artifact = Path(root) / artifact
    return (artifact / "plan-fastapi.json").is_file()


def _prompt_sql_engine(default: str | None) -> str:
    chosen = default if default in SQL_ENGINES else DEFAULT_SQL_ENGINE
    print("SQL engine for the generated FastAPI app:")
    for index, name in enumerate(SQL_ENGINES, start=1):
        marker = " [default]" if name == chosen else ""
        print(f"  {index}) {SQL_ENGINE_LABELS[name]}{marker}")
    raw = input(f"Choice [{SQL_ENGINES.index(chosen) + 1}]: ").strip().lower()
    if not raw:
        return chosen
    if raw in SQL_ENGINES:
        return raw
    if raw.isdigit() and 1 <= int(raw) <= len(SQL_ENGINES):
        return SQL_ENGINES[int(raw) - 1]
    raise FrameworkMigrationError(f"unknown SQL engine: {raw}; choose {', '.join(SQL_ENGINES)}")


def _print_framework_scan(scan: FrameworkScan) -> None:
    custom_actions = {
        route.operation
        for route in scan.routes
        if route.operation
        not in {
            "get",
            "post",
            "put",
            "patch",
            "delete",
            "list",
            "retrieve",
            "create",
            "update",
            "partial_update",
            "destroy",
        }
    }
    print("Sanka")
    print()
    print("Scanning repository...")
    print()
    print("Detected")
    print(f"  Python       {scan.python_version}")
    print(f"  Django       {scan.django_version}")
    print(f"  DRF          {scan.drf_version}")
    db = scan.database
    db_label = db.vendor if not db.name else f"{db.vendor} ({db.name})"
    print(f"  Database     {db_label}")
    print()
    print("Application")
    print(f"  {len(scan.routes)} endpoints")
    print(f"  {len(scan.serializers)} serializers")
    print(f"  {len(scan.models)} models")
    print(f"  {len(scan.permissions)} permissions")
    if scan.skipped_routes:
        print()
        print("Not scanned (non-DRF views — port these by hand)")
        for skipped in scan.skipped_routes:
            print(f"  {skipped.pattern} -> {skipped.view}")
    print(f"  {len(custom_actions)} custom actions")
    print(f"  {scan.test_files} test files")
    print()
    print("Migration candidates")
    print("  → FastAPI     Supported (native async SQL + compatibility strategies)")
    if scan.risks:
        print()
        print(f"Risks: {len(scan.risks)} route(s) need adaptation")
    print()
    print(f"scan hash: {scan.scan_hash}")
    print("Scan complete.")
    print()
    print("Run:")
    print("  sanka plan --to fastapi")


def _print_framework_plan(plan: FrameworkPlan) -> None:
    native = plan.mode == NATIVE_STRATEGY
    dropped = sum(route.strategy == "dropped-format-suffix-alias" for route in plan.routes)
    print("DRF → FastAPI Migration Plan" + (" (native)" if native else " (compatibility)"))
    print()
    print(f"  {len(plan.routes)} endpoints")
    print()
    if native:
        print("Native FastAPI generation")
        print(f"  {plan.native_routes} endpoints")
        if dropped:
            print()
            print("Dropped format-suffix aliases (disclosed contract change)")
            print(f"  {dropped} endpoints")
    else:
        print("Automatic compatibility bridge")
        print(f"  {plan.automatic_routes} endpoints")
    print()
    print("Needs adaptation")
    print(f"  {plan.needs_adaptation_routes} endpoints")
    if native and plan.needs_adaptation_routes:
        reason_counts = Counter(
            (reason.code, reason.feature, reason.message)
            for route in plan.routes
            for reason in route.adaptation_reasons
        )
        if reason_counts:
            print()
            print("Why routes need adaptation")
            for (code, feature, message), count in reason_counts.most_common(10):
                print(f"  {count} endpoints — {code} ({feature})")
                print(f"    {message}")
            if len(reason_counts) > 10:
                remaining = sum(count for _, count in reason_counts.most_common()[10:])
                print(
                    f"  {remaining} additional reason occurrences; "
                    "run `sanka plan --to fastapi --json` for per-route details"
                )
    print()
    print("Retained in native mode" if native else "Retained in compatibility mode")
    for item in plan.retained:
        print(f"  - {item}")
    if native:
        print()
        print("SQL engine")
        print(f"  {plan.sql_engine} — {SQL_ENGINE_LABELS.get(plan.sql_engine, plan.sql_engine)}")
        print("  change with `sanka plan --to fastapi --orm tortoise|sqlalchemy|psycopg`")
    if plan.risks:
        print()
        print("Potential issues")
        for risk in plan.risks:
            location = f" ({risk.file}:{risk.line})" if risk.file and risk.line else ""
            print(f"  {risk.severity.upper()} {risk.code}{location}")
            print(f"    {risk.message}")
    print()
    if native:
        print(
            f"Native migration readiness: {plan.readiness:.0%} "
            f"({plan.native_routes}/{plan.native_eligible_routes} non-alias routes generated)"
        )
        if dropped:
            print(
                f"Format-suffix aliases dropped: {dropped} "
                f"({plan.alias_drop_rate:.0%} of scanned routes)"
            )
    else:
        print(f"Bridge generation readiness: {plan.readiness:.0%}")
    print(f"plan hash: {plan.plan_hash}")
    print("Review the plan, then run `sanka apply --plan-hash <hash>`.")


def _print_framework_test(report: dict[str, Any]) -> None:
    verdict = "OK" if report["ok"] else "FAILED"
    print("Testing the generated FastAPI app...")
    print()
    print(f"Wrote {report['file']}")
    if report.get("environment"):
        print(f"Attested dependency metadata: {report['pyproject']}")
        print(f"Disposable environment: {report['environment']}")
        print(f"Disposable Python: {report['python']}")
        print(f"Disposable project: {report['environment_pyproject']}")
        print(f"Resolved lockfile: {report['lockfile']}")
    print(f"Ran {report['tests']} tests")
    if report.get("allow_writes"):
        print("Writes ran against an isolated SQLite copy")
    print(f"Generated API tests: {verdict}")
    if not report["ok"] and report.get("log"):
        print()
        print(report["log"])
    print()
    if report["ok"]:
        print("next: sanka verify")
        return
    missing = report.get("missing_dependency")
    if missing:
        print(
            f"Generated app dependency is missing: {missing['module']} "
            f"(package `{missing['package']}`)."
        )
        print("The generated dependency metadata may be incomplete; rerun:")
        print("  sanka apply --plan-hash <hash> --force")
        print("  sanka test")
        return
    print("Fix the test failure above, then rerun:")
    print("  sanka test")


def _print_framework_verify(report: dict[str, Any]) -> None:
    routes = report["routes"]
    http = report["http"]
    native = report.get("mode") == NATIVE_STRATEGY
    verdict = "complete" if report["ok"] else "FAILED"
    if native:
        print("Verifying the native DRF → FastAPI migration...")
    else:
        print("Verifying the DRF → FastAPI compatibility bridge...")
    print()
    paths = report["paths"]
    print("Verified paths")
    print(f"  Source app:    {paths['source']}")
    print(f"  Scan:          {paths['scan']}")
    print(f"  Plan:          {paths['plan']}")
    print(f"  Generated app: {paths['generated']}")
    print(f"  Manifest:      {paths['manifest']}")
    print(f"  Dependencies:  {paths['pyproject']}")
    if paths.get("environment"):
        print(f"  Environment:   {paths['environment']}")
        print(f"  Python:        {paths['python']}")
        print(f"  Lockfile:      {paths['lockfile']}")
    generated_files = report.get("generated_files") or []
    if generated_files:
        print("Generated Python checked")
        for path in generated_files:
            print(f"  - {path}")
    print()
    print("Routes")
    expected_total = routes["planned"] - len(routes.get("dropped", []))
    print(f"  {routes['generated']} / {expected_total} generated")
    if not routes["missing"] and not routes.get("extra"):
        print("  generated route declarations match the reviewed plan ✓")
    if routes.get("dropped"):
        print(f"  {len(routes['dropped'])} format-suffix aliases dropped by design")
    print("Generated code")
    print("  machine-local bundle integrity attestation verified ✓")
    print("  generated Python files exist and compile ✓")
    print("  manifest matches the current scan and reviewed plan ✓")
    if native:
        print("  generated serving path has no Django imports ✓")
    print("Safe HTTP behavior")
    if not http.get("enabled", True):
        print("  skipped with --no-http")
    else:
        print(f"  {http['passed']} / {http['probed']} source-vs-generated probes matched")
        print("  automatic coverage is limited to parameter-free GET/HEAD routes")
        print("  compared status, content type, body, Allow, Location, and WWW-Authenticate")
        if http["safe_routes"] == 0:
            print("  no parameter-free GET/HEAD routes were available for automatic probing")
    if routes["missing"]:
        print("Missing routes")
        for route in routes["missing"]:
            print(f"  - {route}")
    if routes.get("extra"):
        print("Unexpected routes")
        for route in routes["extra"]:
            print(f"  - {route}")
    if routes["needs_adaptation"]:
        print("Needs adaptation")
        for route in routes["needs_adaptation"]:
            print(f"  - {route}")
    if http["failed"]:
        print("HTTP mismatches")
        for probe in http["failed"]:
            print(
                f"  - {probe['method']} {probe['path']}:"
                f" source={probe['source_status']} target={probe['target_status']}"
            )
    print()
    if native:
        print(f"Native migration verification: {verdict}")
    else:
        print(f"Compatibility bridge verification: {verdict}")
    print(f"plan hash: {report['plan_hash']}")


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
    print(f"approved source records: {sum(len(route.candidate_ids) for route in plan.routes)}")
    if plan.candidate_hash:
        print(f"candidate hash: {plan.candidate_hash}")
    print(f"plan hash: {plan.plan_hash}")
    print("Review the plan, then run `sanka apply --plan-hash <hash>`.")


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
