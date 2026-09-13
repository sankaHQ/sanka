# SPDX-License-Identifier: AGPL-3.0-only
"""The ``sanka`` CLI: scan / plan / validate / apply / test / verify / status.

Spec-driven flow (migration-as-code)::

    sanka plan     -f sanka.yaml
    sanka validate -f sanka.yaml
    sanka apply --plan-hash sha256:... -f sanka.yaml
    sanka verify   -f sanka.yaml

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
import shlex
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

from sanka.cli._output import TerminalOutput
from sanka.cli._research import (
    SankaMigrateApiClient,
    SankaMigrateApiError,
    print_research,
    signup_url,
)
from sanka.runtime.__about__ import __version__
from sanka.runtime.engine import ExecutionError, MigrationEngine, VerifyReport
from sanka.runtime.execution import DEFAULT_VALIDATION_SAMPLE_SIZE
from sanka.runtime.extensions import ExtensionError
from sanka.runtime.extensions.lifecycle import ApplicationLifecycle
from sanka.runtime.extensions.runner import ExtensionResult
from sanka.runtime.extensions.store import ExtensionStore
from sanka.runtime.planner import MigrationPlan
from sanka.runtime.registry import ExtensionRegistry, UnknownEndpointError
from sanka.runtime.spec import EndpointSpec, MigrationSpec, SpecError
from sanka.runtime.state import SqliteStateStore

DEFAULT_SPEC_FILE = "sanka.yaml"
DEFAULT_STATE_FILE = ".sanka/migrate/state.db"
DEFAULT_ARTIFACT_DIR = ".sanka"
# Stable subprocess protocol consumed by sanka-sdk. Success is exit 0; migration
# failures are exit 1; usage failures are exit 2. Each emits one JSON document.
CLI_SCHEMA_VERSION = "sanka-cli/v1"
SDK_COMMANDS = frozenset({"scan", "plan", "apply", "test", "verify", "extension"})


class CliUsageError(ValueError):
    """A deterministic usage failure raised during or after argument parsing."""


class _CliArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, json_errors: bool = False, **kwargs: Any) -> None:
        self.json_errors = json_errors
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        if self.json_errors:
            raise CliUsageError(message)
        super().error(message)


def main(argv: list[str] | None = None, *, api_base: str | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_errors = "--json" in arguments or "--compact-dsl" in arguments
    parser = _build_parser(json_errors=json_errors)
    try:
        args = parser.parse_args(arguments)
        args.api_base = api_base
    except CliUsageError as error:
        command = arguments[0] if arguments and arguments[0] in SDK_COMMANDS else "sanka"
        return _print_cli_error(
            argparse.Namespace(
                command=command, json=json_errors, compact_dsl="--compact-dsl" in arguments
            ),
            error,
            exit_code=2,
        )
    if getattr(args, "compact_dsl", False):
        args.json = True
    if args.command is None:
        parser.print_help()
        return 0
    if hasattr(args, "root_option"):
        args.root = args.root_option or args.root or "."
    try:
        return int(asyncio.run(args.handler(args)))
    except CliUsageError as error:
        return _print_cli_error(args, error, exit_code=2)
    except ExtensionError as error:
        return _print_cli_error(
            args,
            error,
            exit_code=(
                2
                if args.command == "plan" and error.code == "SANKA_EXTENSION_TARGET_REQUIRED"
                else 1
            ),
        )
    except (
        SpecError,
        UnknownEndpointError,
        ExecutionError,
        FileNotFoundError,
    ) as error:
        return _print_cli_error(args, error, exit_code=1)
    except SankaMigrateApiError as error:
        retry = f"; retry after {error.retry_after}s" if error.retry_after else ""
        return _print_cli_error(
            args,
            RuntimeError(f"{error.code}: {error.message}{retry}"),
            exit_code=1,
        )
    except BrokenPipeError:
        return 0


def _terminal(args: argparse.Namespace) -> TerminalOutput:
    return TerminalOutput(
        json_mode=bool(getattr(args, "json", False)),
        no_color=bool(getattr(args, "no_color", False)),
        quiet=bool(getattr(args, "quiet", False)),
        verbose=bool(getattr(args, "verbose", False)),
    )


def _json_result(
    command: str,
    data: dict[str, Any],
    *,
    outcome: str = "success",
    migration_state: str,
    artifacts: list[str] | None = None,
    limitations: list[str] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    if outcome == "error" and "error" not in data:
        data = {
            **data,
            "error": {"code": "SANKA_FAILED", "message": f"{command} failed"},
        }
    envelope: dict[str, Any] = {
        "schema_version": CLI_SCHEMA_VERSION,
        "command": command,
        "outcome": outcome,
        "migration_state": migration_state,
        "data": data,
        "artifacts": sorted(artifacts or []),
        "limitations": sorted(limitations or []),
        "next_actions": next_actions or [],
    }
    # Keep v0 callers working while the versioned envelope becomes the stable contract.
    for key, value in data.items():
        envelope.setdefault(key, value)
    return envelope


def _print_result(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    if getattr(args, "compact_dsl", False):
        from sanka.cli._compact import render_compact

        print(render_compact(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _print_cli_error(args: argparse.Namespace, error: Exception, *, exit_code: int) -> int:
    if getattr(args, "json", False) or getattr(args, "compact_dsl", False):
        code = getattr(error, "code", None)
        details = getattr(error, "details", None)
        structured_error: dict[str, Any] = {
            "code": code or ("SANKA_USAGE" if exit_code == 2 else "SANKA_FAILED"),
            "message": str(error),
        }
        if isinstance(details, dict) and details:
            structured_error["details"] = details
        _print_result(
            args,
            _json_result(
                str(getattr(args, "command", "sanka") or "sanka"),
                {"error": structured_error},
                outcome="error",
                migration_state="not_started" if exit_code == 2 else "failed",
            ),
        )
    else:
        _terminal(args).failure(str(error))
    return exit_code


def _build_parser(*, json_errors: bool = False) -> argparse.ArgumentParser:
    parser = _CliArgumentParser(
        prog="sanka",
        description="Sanka — inspect, plan, execute, and verify migrations with a finish line.",
        epilog=(
            "Migration lifecycle: scan → plan → apply → test → verify.\n"
            "Run `sanka <command> --help` for choices, safety, artifacts, and examples."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        json_errors=json_errors,
    )
    parser.add_argument("--version", action="version", version=f"sanka {__version__}")
    parser.set_defaults(command=None)
    commands = parser.add_subparsers(dest="command")

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("-f", "--file", default=DEFAULT_SPEC_FILE, help="migration spec YAML")
        sub.add_argument("--state", default=DEFAULT_STATE_FILE, help="run-state SQLite file")

    def presentation(sub: argparse.ArgumentParser, *, json_option: bool = True) -> None:
        if json_option:
            formats = sub.add_mutually_exclusive_group()
            formats.add_argument(
                "--json", action="store_true", help="print one sanka-cli/v1 JSON document"
            )
            formats.add_argument(
                "--compact-dsl",
                action="store_true",
                help="print a compact result; full generated code stays in artifacts",
            )
        sub.add_argument("--no-color", action="store_true", help="disable ANSI color")
        detail = sub.add_mutually_exclusive_group()
        detail.add_argument("--quiet", action="store_true", help="print only outcome and errors")
        detail.add_argument("--verbose", action="store_true", help="print diagnostic details")

    def extension_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--extension-config",
            action="append",
            default=[],
            metavar="JSON_OBJECT",
            help="merge a JSON object into extension configuration",
        )
        sub.add_argument(
            "--extension-env",
            action="append",
            default=[],
            metavar="NAME",
            help="forward one explicitly named environment variable",
        )

    scan = commands.add_parser(
        "scan",
        help="inspect a source application and write its semantic scan artifact",
        description=(
            "Inspect a Django/DRF source without changing it. Writes only the canonical "
            "scan artifact under --artifact-dir."
        ),
        epilog="Example: sanka scan .\nNext: sanka plan .",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scan.add_argument("root", nargs="?", default=".", help="Django repository root")
    scan.add_argument("--settings", help="Django settings module (auto-detected by default)")
    scan.add_argument("--artifact-dir", default=DEFAULT_ARTIFACT_DIR)
    extension_options(scan)
    presentation(scan)
    scan.set_defaults(handler=_cmd_scan)

    plan = commands.add_parser(
        "plan",
        help="inspect source/target and produce a reviewable plan",
        description=(
            "Build a hash-bound migration plan. The target is inspected for update mode but "
            "is never modified; only --artifact-dir is written."
        ),
        epilog=(
            "Interactive: sanka plan .\n"
            "Non-interactive: sanka plan . --to fastapi --generation full "
            "--output ./fastapi-app --strategy native --package-manager uv\n"
            "Next: sanka apply --plan-hash <printed-hash>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common(plan)
    plan.add_argument("root", nargs="?", default=".", help="application repository root")
    plan.add_argument("--to", help="target application framework")
    plan.add_argument(
        "--strategy",
        choices=("native", "compatibility"),
        default=None,
        help="native FastAPI generation or the in-process compatibility bridge",
    )
    plan.add_argument("--artifact-dir", default=DEFAULT_ARTIFACT_DIR)
    plan.add_argument("--output", default=None, help="generated FastAPI target directory")
    plan.add_argument(
        "--generation",
        choices=("full", "update", "minimal"),
        default=None,
        help="full standalone project, safe update, or minimal runnable output",
    )
    plan.add_argument(
        "--package-manager",
        choices=("uv", "pip"),
        default=None,
        help="dependency setup used by the generated project",
    )
    plan.add_argument(
        "--orm",
        choices=("tortoise", "sqlalchemy", "psycopg"),
        default=None,
        help="async SQL engine for native FastAPI (default: tortoise, closest to Django)",
    )
    extension_options(plan)
    presentation(plan)
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

    apply_ = commands.add_parser(
        "apply",
        help="execute the reviewed plan (resumable)",
        description=(
            "Generate into the exact target reviewed by plan. Requires the exact plan hash; "
            "update mode refuses target drift and user-file conflicts."
        ),
        epilog=(
            "Mutates: the reviewed target only.\n"
            "Example: sanka apply --root . --plan-hash sha256:...\n"
            "Next: sanka test"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common(apply_)
    apply_.add_argument(
        "--plan-hash",
        required=True,
        help="exact reviewed plan hash printed by `sanka plan`",
    )
    apply_.add_argument("--to", help="select an application plan")
    apply_.add_argument("--root", default=".", help="application repository root")
    apply_.add_argument("--artifact-dir", default=DEFAULT_ARTIFACT_DIR)
    apply_.add_argument("--output", default=None, help="generated FastAPI output directory")
    apply_.add_argument("--force", action="store_true", help="replace existing generated files")
    apply_.add_argument(
        "--orm",
        choices=("tortoise", "sqlalchemy", "psycopg"),
        default=None,
        help="assert the reviewed ORM selection; cannot change it during apply",
    )
    apply_.add_argument(
        "--min-readiness",
        type=float,
        default=None,
        metavar="PCT",
        help="minimum native readiness percentage for generation",
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
    extension_options(apply_)
    presentation(apply_)
    apply_.set_defaults(handler=_cmd_apply)

    test = commands.add_parser(
        "test",
        help="generate and run tests for the converted application",
        description=(
            "Prepare the generated uv or pip environment, write generated-app tests, and run "
            "them. This proves generated scope, not source parity."
        ),
        epilog="Example: sanka test .\nNext: sanka verify .",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common(test)
    test.add_argument("root", nargs="?", default=None, help="application repository root")
    test.add_argument(
        "--root",
        dest="root_option",
        metavar="ROOT",
        default=None,
        help="application repository root",
    )
    test.add_argument("--to", help="select an application plan")
    test.add_argument("--artifact-dir", default=DEFAULT_ARTIFACT_DIR)
    test.add_argument("--output", default=None, help="generated FastAPI output directory")
    extension_options(test)
    presentation(test)
    test.set_defaults(handler=_cmd_test)

    verify = commands.add_parser(
        "verify",
        help="verify the target against the source and ledger",
        description=(
            "Check hashes, generated files, route coverage, and configured read-only HTTP "
            "comparisons. With --scenarios, replay a scenario file against the source "
            "application and a candidate FastAPI or Flask app and diff status, body, declared "
            "headers, and database state; that mode needs no plan or generated manifest."
        ),
        epilog=(
            "Examples:\n"
            "  sanka verify .\n"
            "  sanka verify . --scenarios public-tests/scenarios.json --candidate . "
            "--entrypoint target_app.py --db-env BENCH_DB_PATH --edge-probes\n"
            "Use --no-http only when structural verification is sufficient."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common(verify)
    verify.add_argument("root", nargs="?", default=None, help="application repository root")
    verify.add_argument(
        "--root",
        dest="root_option",
        metavar="ROOT",
        default=None,
        help="application repository root",
    )
    verify.add_argument("--to", help="select an application plan")
    verify.add_argument("--settings", help="source Django settings module for scenario replay")
    verify.add_argument("--artifact-dir", default=DEFAULT_ARTIFACT_DIR)
    verify.add_argument("--output", default=None, help="generated FastAPI output directory")
    verify.add_argument(
        "--cases",
        default=None,
        help="JSON file with additional read-only HTTP verification cases",
    )
    verify.add_argument("--no-http", action="store_true", help="skip safe read-only HTTP probes")
    verify.add_argument(
        "--scenarios",
        default=None,
        help=(
            "JSON scenario file to replay against the source application and the "
            "candidate (differential mode; any HTTP method; no plan required)"
        ),
    )
    verify.add_argument(
        "--candidate",
        default=None,
        help="candidate application root (default: the project root)",
    )
    verify.add_argument(
        "--entrypoint",
        default=None,
        help="candidate module exposing `app` (default: target_app.py)",
    )
    verify.add_argument(
        "--db-env",
        dest="db_env",
        default=None,
        help=(
            "environment variable both applications read for the SQLite path; each "
            "scenario runs from an identical fresh database (default: SANKA_TEST_DB)"
        ),
    )
    verify.add_argument(
        "--seed",
        default=None,
        help="Python file run after migrate to seed the fresh database (Django is configured)",
    )
    verify.add_argument(
        "--ignore-table",
        dest="ignore_tables",
        action="append",
        default=None,
        metavar="TABLE",
        help="exclude a table from the database comparison (repeatable)",
    )
    verify.add_argument(
        "--all-headers",
        dest="all_headers",
        action="store_true",
        help="compare every response header, not only the scenario's declared headers",
    )
    verify.add_argument(
        "--edge-probes",
        dest="edge_probes",
        action="store_true",
        help=(
            "add scan-derived edge probes per route: OPTIONS/Allow, an unsupported "
            "method, the slash variant, and a missing-object detail request"
        ),
    )
    verify.add_argument(
        "--source-python",
        dest="python",
        metavar="PATH",
        help=(
            "interpreter that can import the source application (default: the project's "
            ".venv when present, otherwise the extension's own interpreter)"
        ),
    )
    verify.add_argument(
        "--candidate-python",
        dest="candidate_python",
        metavar="PATH",
        help=(
            "interpreter that can import the candidate application (default: the "
            "candidate's .venv when present, otherwise the source interpreter)"
        ),
    )
    extension_options(verify)
    presentation(verify)
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
        help="inspect installed extension support; does not authenticate a system",
    )
    connect.add_argument("provider", help="system type, e.g. markdown or postgres")
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

    extension = commands.add_parser(
        "extension",
        help="manage migration extensions",
        description="manage migration extensions and trusted marketplace snapshots.",
    )
    extension_commands = extension.add_subparsers(dest="extension_command", required=True)

    extension_add = extension_commands.add_parser("add", help="install and lock an extension")
    extension_add.add_argument("extension_id")
    extension_add.add_argument("--marketplace")
    presentation(extension_add)
    extension_add.set_defaults(handler=_cmd_extension_add)

    extension_list = extension_commands.add_parser("list", help="list available extensions")
    presentation(extension_list)
    extension_list.set_defaults(handler=_cmd_extension_list)

    extension_remove = extension_commands.add_parser("remove", help="unpin or disable an extension")
    extension_remove.add_argument("extension_id")
    presentation(extension_remove)
    extension_remove.set_defaults(handler=_cmd_extension_remove)

    marketplace = extension_commands.add_parser(
        "marketplace", help="manage trusted marketplace sources"
    )
    marketplace_commands = marketplace.add_subparsers(dest="marketplace_command", required=True)
    marketplace_add = marketplace_commands.add_parser("add", help="add a trusted snapshot")
    marketplace_add.add_argument("source")
    marketplace_add.add_argument("--name")
    marketplace_add.add_argument(
        "--revision", help="pin an explicit full Git commit instead of the default catalog"
    )
    marketplace_add.add_argument(
        "--trust", action="store_true", help="explicitly trust a third-party source"
    )
    presentation(marketplace_add)
    marketplace_add.set_defaults(handler=_cmd_extension_marketplace_add)

    marketplace_list = marketplace_commands.add_parser("list", help="list trusted snapshots")
    presentation(marketplace_list)
    marketplace_list.set_defaults(handler=_cmd_extension_marketplace_list)

    marketplace_upgrade = marketplace_commands.add_parser(
        "upgrade", help="refresh marketplace snapshots without changing project pins"
    )
    marketplace_upgrade.add_argument("name", nargs="?")
    presentation(marketplace_upgrade)
    marketplace_upgrade.set_defaults(handler=_cmd_extension_marketplace_upgrade)

    marketplace_remove = marketplace_commands.add_parser(
        "remove", help="remove an unused marketplace"
    )
    marketplace_remove.add_argument("name")
    presentation(marketplace_remove)
    marketplace_remove.set_defaults(handler=_cmd_extension_marketplace_remove)

    _set_json_errors(parser, json_errors)
    return parser


def _set_json_errors(parser: argparse.ArgumentParser, enabled: bool) -> None:
    if isinstance(parser, _CliArgumentParser):
        parser.json_errors = enabled
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _set_json_errors(child, enabled)


def _research_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lang", choices=("en", "ja"), dest="locale")
    parser.add_argument("--json", action="store_true", help="print the API data payload as JSON")


def _research_client(api_base: str | None = None) -> SankaMigrateApiClient:
    # The shared CLI option names an API origin (optionally with a path prefix).
    # The older SANKA_MIGRATE_API_BASE setting continues to name the full service root.
    if api_base:
        base = api_base.rstrip("/")
        if not base.endswith("/v2/migrate"):
            base += "/v2/migrate"
        return SankaMigrateApiClient(base_url=base)
    return SankaMigrateApiClient()


def _engine(state_path: str) -> MigrationEngine:
    return MigrationEngine(
        store=SqliteStateStore(state_path), registry=ExtensionRegistry.discover()
    )


def _extension_result(
    args: argparse.Namespace,
    operation: str,
    records: list[dict[str, Any]],
) -> int:
    data = {"operation": operation, "records": records}
    if args.json or getattr(args, "compact_dsl", False):
        _print_result(args, _json_result("extension", data, migration_state="not_started"))
        return 0

    terminal = _terminal(args)
    if operation == "list":
        terminal.heading("Extensions")
        terminal.table(
            ("Extension", "Kind", "Version", "Status", "Marketplace"),
            [
                (
                    str(record.get("id", "")),
                    {"connector": "System access", "migration": "Code conversion"}.get(
                        str(record.get("kind", "")), str(record.get("kind", ""))
                    ),
                    str(record.get("version", "")),
                    ", ".join(map(str, record.get("status", []))),
                    str(record.get("marketplace", "")),
                )
                for record in records
            ],
        )
    elif operation == "marketplace_list":
        terminal.heading("Marketplaces")
        terminal.table(
            ("Marketplace", "Source", "Commit", "Trusted"),
            [
                (
                    str(record.get("name", "")),
                    str(record.get("source", "")),
                    str(record.get("resolved_commit") or "-")[:12],
                    "yes" if record.get("trusted") else "no",
                )
                for record in records
            ],
        )
    elif operation == "add":
        record = records[0]
        terminal.success(f"Installed {record['id']} {record['version']}")
    elif operation == "remove":
        terminal.success(f"Removed {records[0]['id']}")
    elif operation == "marketplace_add":
        terminal.success(f"Added marketplace {records[0]['name']}")
    elif operation == "marketplace_upgrade":
        terminal.success(
            "Updated marketplace " + ", ".join(str(record["name"]) for record in records)
        )
    elif operation == "marketplace_remove":
        terminal.success(f"Removed marketplace {records[0]['name']}")
    return 0


def _extension_store() -> ExtensionStore:
    return ExtensionStore(Path.cwd())


async def _cmd_extension_list(args: argparse.Namespace) -> int:
    return _extension_result(
        args,
        "list",
        [record.to_dict() for record in _extension_store().list_extensions()],
    )


async def _cmd_extension_add(args: argparse.Namespace) -> int:
    record = _extension_store().add_extension(
        args.extension_id,
        marketplace=args.marketplace,
    )
    return _extension_result(args, "add", [record.to_dict()])


async def _cmd_extension_remove(args: argparse.Namespace) -> int:
    _extension_store().remove_extension(args.extension_id)
    return _extension_result(args, "remove", [{"id": args.extension_id}])


async def _cmd_extension_marketplace_list(args: argparse.Namespace) -> int:
    return _extension_result(
        args,
        "marketplace_list",
        [record.to_dict() for record in _extension_store().marketplaces()],
    )


async def _cmd_extension_marketplace_add(args: argparse.Namespace) -> int:
    record = _extension_store().add_marketplace(
        args.source,
        name=args.name,
        trust=args.trust,
        revision=args.revision,
    )
    return _extension_result(args, "marketplace_add", [record.to_dict()])


async def _cmd_extension_marketplace_upgrade(args: argparse.Namespace) -> int:
    records = _extension_store().upgrade_marketplace(args.name)
    return _extension_result(
        args,
        "marketplace_upgrade",
        [record.to_dict() for record in records],
    )


async def _cmd_extension_marketplace_remove(args: argparse.Namespace) -> int:
    record = _extension_store().remove_marketplace(args.name)
    return _extension_result(args, "marketplace_remove", [record.to_dict()])


async def _cmd_connect(args: argparse.Namespace) -> int:
    registry = ExtensionRegistry.discover()
    system_type = str(args.provider).strip().lower()
    if system_type == "postgresql":
        system_type = "postgres"
    try:
        roles = list(registry.roles(system_type))
        payload = {
            "provider": system_type,  # Compatibility JSON key.
            "system_type": system_type,
            "roles": roles,
            "installed": True,
            "connection_status": "not_checked",
            **registry.extension_metadata(system_type),
        }
    finally:
        registry.close()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{system_type}: extension installed ({', '.join(roles)})")
        if payload.get("package"):
            print(f"extension {payload['extension_id']}; package {payload['package']}")
        print("System authentication and reachability have not been checked.")
    return 0


def _load_spec(path: str) -> MigrationSpec:
    spec_path = Path(path)
    if not spec_path.is_file():
        raise FileNotFoundError(f"spec file not found: {spec_path}")
    return MigrationSpec.from_yaml(spec_path.read_text(encoding="utf-8"))


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt_choice(
    label: str,
    choices: tuple[tuple[str, str], ...],
    *,
    default: str,
) -> str:
    print(label)
    for index, (value, description) in enumerate(choices, start=1):
        marker = " (recommended)" if value == default else ""
        print(f"  {index}. {description}{marker}")
    raw = (
        input(f"Select [{next(i for i, item in enumerate(choices, 1) if item[0] == default)}]: ")
        .strip()
        .lower()
    )
    if not raw:
        return default
    for index, (value, _description) in enumerate(choices, start=1):
        if raw in {str(index), value}:
            return value
    raise CliUsageError(f"invalid choice {raw!r}; choose {', '.join(item[0] for item in choices)}")


def _extension_configuration(args: argparse.Namespace) -> dict[str, Any]:
    configuration: dict[str, Any] = {}
    for document in getattr(args, "extension_config", []):
        try:
            value = json.loads(document)
        except json.JSONDecodeError as error:
            raise CliUsageError(f"--extension-config must be a JSON object: {error}") from error
        if not isinstance(value, dict):
            raise CliUsageError("--extension-config must be a JSON object")
        configuration.update(value)
    for source, target in (
        ("settings", "settings_module"),
        ("strategy", "strategy"),
        ("generation", "generation"),
        ("package_manager", "package_manager"),
        ("orm", "orm"),
        ("output", "output"),
        ("min_readiness", "min_readiness"),
        ("bench_candidate", "bench_candidate"),
        ("cases", "cases"),
        ("scenarios", "scenarios"),
        ("candidate", "candidate"),
        ("entrypoint", "entrypoint"),
        ("db_env", "db_env"),
        ("seed", "seed"),
        ("ignore_tables", "ignore_tables"),
        ("python", "python"),
        ("candidate_python", "candidate_python"),
    ):
        value = getattr(args, source, None)
        if value is not None:
            configuration[target] = value
    for name in ("force", "gap_report_only", "no_http", "all_headers", "edge_probes"):
        if getattr(args, name, False):
            configuration[name] = True
    return configuration


def _lifecycle_prompt(label: str, choices: tuple[str, ...] | None = None) -> str | None:
    if choices:
        return _prompt_choice(
            f"{label}:",
            tuple((choice, choice) for choice in choices),
            default=choices[0],
        )
    return input(f"{label}: ").strip() or None


def _application_lifecycle(args: argparse.Namespace) -> ApplicationLifecycle:
    return ApplicationLifecycle(
        Path(args.root),
        artifact_dir=args.artifact_dir,
        interactive=_interactive_terminal() and not args.json,
        prompt=_lifecycle_prompt,
    )


def _application_lifecycle_requested(args: argparse.Namespace) -> bool:
    return args.file == DEFAULT_SPEC_FILE and not Path(args.file).is_file()


def _print_application_result(
    args: argparse.Namespace,
    command: str,
    result: ExtensionResult,
    *,
    migration_state: str,
) -> int:
    if args.json or getattr(args, "compact_dsl", False):
        _print_result(
            args,
            _json_result(
                command,
                result.data,
                migration_state=migration_state,
                artifacts=list(result.artifacts),
                limitations=list(result.limitations),
                next_actions=list(result.next_actions),
            ),
        )
    else:
        terminal = _terminal(args)
        terminal.success(f"{command} complete")
        if command == "plan" and isinstance(result.data.get("plan_hash"), str):
            print(f"plan {result.data['plan_hash']}")
    return 0


async def _cmd_plan(args: argparse.Namespace) -> int:
    if _application_lifecycle_requested(args):
        result = _application_lifecycle(args).plan(
            target=args.to,
            configuration=_extension_configuration(args),
            explicit_env_names=tuple(args.extension_env),
        )
        return _print_application_result(args, "plan", result, migration_state="planned")
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    migration_plan = await engine.plan(run_id)
    if args.json or getattr(args, "compact_dsl", False):
        _print_result(
            args,
            _json_result(
                "plan",
                {
                    **migration_plan.to_payload(),
                    "run_id": run_id,
                    "plan_hash": migration_plan.plan_hash,
                    "ready": migration_plan.ready,
                },
                migration_state="planned",
                artifacts=[str(Path(args.state).resolve())],
                limitations=migration_plan.warnings,
                next_actions=[
                    shlex.join(
                        [
                            "sanka",
                            "apply",
                            "--file",
                            str(args.file),
                            "--state",
                            str(args.state),
                            "--plan-hash",
                            migration_plan.plan_hash,
                        ]
                    )
                ],
            ),
        )
    else:
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
    if _application_lifecycle_requested(args):
        result = _application_lifecycle(args).apply(
            reviewed_plan_hash=args.plan_hash,
            configuration=_extension_configuration(args),
            explicit_env_names=tuple(args.extension_env),
        )
        return _print_application_result(
            args,
            "apply",
            result,
            migration_state="generated_not_verified",
        )
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    run = engine.store.get_run(run_id)
    if run.plan_json is None:
        raise ExecutionError("no plan for this spec yet; run `sanka plan` first")
    await engine.apply(run_id, plan_hash=args.plan_hash)
    if args.json or getattr(args, "compact_dsl", False):
        _print_result(
            args,
            _json_result(
                "apply",
                {
                    "run_id": run_id,
                    "plan_hash": args.plan_hash,
                },
                migration_state="applied_not_verified",
                artifacts=[str(Path(args.state).resolve())],
                next_actions=[
                    shlex.join(
                        [
                            "sanka",
                            "verify",
                            "--file",
                            str(args.file),
                            "--state",
                            str(args.state),
                        ]
                    )
                ],
            ),
        )
    else:
        print(f"run {run_id}: applied")
    return 0


async def _cmd_test(args: argparse.Namespace) -> int:
    result = _application_lifecycle(args).test(
        configuration=_extension_configuration(args),
        explicit_env_names=tuple(args.extension_env),
    )
    return _print_application_result(
        args,
        "test",
        result,
        migration_state="generated_app_tested",
    )


async def _cmd_verify(args: argparse.Namespace) -> int:
    if _application_lifecycle_requested(args):
        result = _application_lifecycle(args).verify(
            configuration=_extension_configuration(args),
            explicit_env_names=tuple(args.extension_env),
        )
        return _print_application_result(
            args,
            "verify",
            result,
            migration_state="verified_within_scope",
        )
    spec = _load_spec(args.file)
    engine = _engine(args.state)
    run_id = engine.create(spec)
    verify_report = await engine.verify(run_id)
    if args.json or getattr(args, "compact_dsl", False):
        _print_result(
            args,
            _json_result(
                "verify",
                {
                    "run_id": verify_report.run_id,
                    "ok": verify_report.ok,
                    "routes": [
                        {
                            "route_key": route.route_key,
                            "source_count": route.source_count,
                            "migrated": route.migrated,
                            "failed": route.failed,
                            "destination_count": route.destination_count,
                            "ok": route.ok,
                        }
                        for route in verify_report.routes
                    ],
                },
                outcome="success" if verify_report.ok else "error",
                migration_state=(
                    "verified_within_scope" if verify_report.ok else "verification_failed"
                ),
                artifacts=[str(Path(args.state).resolve())],
            ),
        )
    else:
        _print_verify(verify_report)
    return 0 if verify_report.ok else 1


async def _cmd_scan(args: argparse.Namespace) -> int:
    result = _application_lifecycle(args).scan(
        configuration=_extension_configuration(args),
        explicit_env_names=tuple(args.extension_env),
    )
    return _print_application_result(args, "scan", result, migration_state="scanned")


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
        _research_client(getattr(args, "api_base", None)).research_eol,
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
        _research_client(getattr(args, "api_base", None)).research_tco,
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
        _research_client(getattr(args, "api_base", None)).research_compare,
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
        _research_client(getattr(args, "api_base", None)).assess,
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
