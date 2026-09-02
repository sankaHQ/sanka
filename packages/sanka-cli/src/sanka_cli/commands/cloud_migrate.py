# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import click

import sanka_cli.runtime as runtime
from sanka_cli.state import CLIState

MIGRATE_API_PREFIX = "/v2/migrate"

_PLAN_TERMINAL_STATUSES = {"requires_approval", "failed", "cancelled"}
_EXECUTION_TERMINAL_STATUSES = {
    "applied",
    "verified",
    "failed",
    "paused",
    "cancelled",
}

_CLOUD_HELP: dict[str, str] = {
    "plan": """Cloud usage: sanka plan --program ID [--migration ID] [--new]
       [--sample-size N] [--force] [--no-wait] [--timeout SECONDS]

Without --program or --migration, this command runs locally.
When a Program has no migration, plan creates one. --new always creates one.
""",
    "apply": """Cloud usage: sanka apply --program ID [--migration ID] [--yes]
       [--plan-hash HASH] [--route KEY ...] [--wait] [--timeout SECONDS]

Without --program or --migration, this command runs locally.
Cloud apply fetches the reviewable plan and binds execution to its exact hash.
""",
    "status": """Cloud usage: sanka status --program ID [--migration ID]
       sanka status --migration ID

Without --program or --migration, this command runs locally.
""",
    "verify": """Cloud usage: sanka verify --program ID [--migration ID]
       sanka verify --migration ID

Without --program or --migration, this command runs locally.
""",
    "repair": """Usage: sanka repair --program ID [--migration ID] [--yes]
       [--plan-hash HASH] [--wait] [--timeout SECONDS]
""",
    "review": """Usage: sanka review --program ID [--migration ID]
       sanka review --migration ID
""",
    "pause": """Usage: sanka pause --program ID [--migration ID]
       sanka pause --migration ID
""",
    "resume": """Usage: sanka resume --program ID [--migration ID]
       [--plan-hash HASH] [--wait] [--timeout SECONDS]
""",
    "cancel": """Usage: sanka cancel --program ID [--migration ID] [--yes]
       sanka cancel --migration ID [--yes]
""",
}


@dataclass
class CloudOptions:
    yes: bool = False
    wait: bool = False
    force: bool = False
    new: bool = False
    sample_size: int = 10
    timeout: float = 600.0
    plan_hash: str | None = None
    routes: list[str] = field(default_factory=list)


def run_cloud_command(
    command: str,
    state: CLIState,
    *,
    program_id: str | None,
    migration_id: str | None,
    args: tuple[str, ...],
) -> None:
    if "--help" in args or "-h" in args:
        click.echo(_CLOUD_HELP[command].rstrip())
        return
    options = _parse_options(command, args)
    if command == "status":
        _status(state, program_id=program_id, migration_id=migration_id)
        return

    migration, resolved_program_id = _resolve_migration(
        state,
        program_id=program_id,
        migration_id=migration_id,
        create_when_missing=command == "plan",
        always_create=command == "plan" and options.new,
    )
    resolved_migration_id = str(migration["id"])

    if command == "plan":
        _plan(
            state,
            migration_id=resolved_migration_id,
            program_id=resolved_program_id,
            options=options,
        )
    elif command == "apply":
        _hash_bound_action(
            state,
            action="apply",
            migration=migration,
            program_id=resolved_program_id,
            options=options,
            confirmation="Apply this cloud migration?",
        )
    elif command == "repair":
        _hash_bound_action(
            state,
            action="repair",
            migration=migration,
            program_id=resolved_program_id,
            options=options,
            confirmation="Retry failed records for this migration?",
        )
    elif command == "resume":
        _hash_bound_action(
            state,
            action="resume",
            migration=migration,
            program_id=resolved_program_id,
            options=options,
        )
    elif command in {"verify", "review", "pause", "cancel"}:
        _simple_action(
            state,
            action=command,
            migration=migration,
            program_id=resolved_program_id,
            options=options,
        )
    else:  # pragma: no cover - registration owns the command set
        raise click.ClickException(f"Unsupported cloud migration command: {command}")


def _parse_options(command: str, args: tuple[str, ...]) -> CloudOptions:
    allowed_flags: dict[str, set[str]] = {
        "plan": {"--force", "--new", "--wait", "--no-wait"},
        "apply": {"--yes", "--wait", "--no-wait"},
        "status": set(),
        "verify": set(),
        "repair": {"--yes", "--wait", "--no-wait"},
        "review": set(),
        "pause": set(),
        "resume": {"--wait", "--no-wait"},
        "cancel": {"--yes"},
    }
    allowed_values: dict[str, set[str]] = {
        "plan": {"--sample-size", "--timeout"},
        "apply": {"--plan-hash", "--route", "--timeout"},
        "status": set(),
        "verify": set(),
        "repair": {"--plan-hash", "--timeout"},
        "review": set(),
        "pause": set(),
        "resume": {"--plan-hash", "--timeout"},
        "cancel": set(),
    }
    options = CloudOptions(wait=command == "plan")
    index = 0
    while index < len(args):
        raw = args[index]
        key, separator, inline_value = raw.partition("=")
        if key in allowed_flags[command]:
            if separator:
                raise click.ClickException(f"{key} does not accept a value")
            if key == "--no-wait":
                options.wait = False
            else:
                setattr(options, key.removeprefix("--").replace("-", "_"), True)
            index += 1
            continue
        if key in allowed_values[command]:
            if separator:
                value = inline_value
            else:
                index += 1
                if index >= len(args):
                    raise click.ClickException(f"{key} requires a value")
                value = args[index]
            _set_value_option(options, key, value)
            index += 1
            continue
        raise click.ClickException(
            f"Unknown cloud option or argument: {raw}. "
            f"Run `sanka {command} --program ID --help` for cloud usage."
        )
    return options


def _set_value_option(options: CloudOptions, key: str, value: str) -> None:
    if key == "--sample-size":
        try:
            options.sample_size = int(value)
        except ValueError as exc:
            raise click.ClickException("--sample-size must be an integer") from exc
        if options.sample_size < 1:
            raise click.ClickException("--sample-size must be at least 1")
    elif key == "--timeout":
        try:
            options.timeout = float(value)
        except ValueError as exc:
            raise click.ClickException("--timeout must be a number") from exc
        if options.timeout <= 0:
            raise click.ClickException("--timeout must be greater than zero")
    elif key == "--plan-hash":
        options.plan_hash = value
    elif key == "--route":
        options.routes.append(value)


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise click.ClickException("Sanka API returned an unexpected response")
    return data


def _get_program(state: CLIState, program_id: str) -> dict[str, Any]:
    return _data(
        runtime.request_json(
            state,
            "GET",
            f"{MIGRATE_API_PREFIX}/programs/{program_id}",
        )
    )


def _get_migration(state: CLIState, migration_id: str) -> dict[str, Any]:
    return _data(
        runtime.request_json(
            state,
            "GET",
            f"{MIGRATE_API_PREFIX}/migrations/{migration_id}",
        )
    )


def _resolve_migration(
    state: CLIState,
    *,
    program_id: str | None,
    migration_id: str | None,
    create_when_missing: bool,
    always_create: bool = False,
) -> tuple[dict[str, Any], str | None]:
    program: dict[str, Any] | None = None
    migration_ids: list[str] = []
    if program_id:
        program = _get_program(state, program_id)
        migration_ids = [str(value) for value in program.get("migration_ids", [])]

    if migration_id:
        if program is not None and migration_id not in migration_ids:
            raise click.ClickException(
                f"Migration {migration_id} does not belong to Program {program_id}."
            )
        return _get_migration(state, migration_id), program_id

    if program is None:
        raise click.ClickException("Cloud migration commands require --program or --migration.")

    if always_create or (not migration_ids and create_when_missing):
        created = _data(
            runtime.request_json(
                state,
                "POST",
                f"{MIGRATE_API_PREFIX}/programs/{program_id}/migrations",
                json_body={},
            )
        )
        return created, program_id

    if not migration_ids:
        raise click.ClickException(
            f"Program {program_id} has no migration. Run `sanka plan --program {program_id}` first."
        )
    if len(migration_ids) > 1:
        choices = ", ".join(migration_ids)
        raise click.ClickException(
            f"Program {program_id} has multiple migrations ({choices}). "
            "Choose one with --migration."
        )
    return _get_migration(state, migration_ids[0]), program_id


def _plan(
    state: CLIState,
    *,
    migration_id: str,
    program_id: str | None,
    options: CloudOptions,
) -> None:
    migration = _data(
        runtime.request_json(
            state,
            "POST",
            f"{MIGRATE_API_PREFIX}/migrations/{migration_id}/plan",
            json_body={"sample_size": options.sample_size, "force": options.force},
        )
    )
    if not options.wait:
        runtime.emit_payload(
            {
                "program_id": program_id,
                "migration": migration,
                "next": f"sanka status --migration {migration_id}",
            },
            state,
        )
        return

    migration = _poll_migration(
        state,
        migration_id,
        terminal_statuses=_PLAN_TERMINAL_STATUSES,
        timeout=options.timeout,
    )
    if migration.get("status") != "requires_approval":
        raise click.ClickException(
            f"Cloud planning ended with status {migration.get('status')}. "
            f"Inspect it with `sanka status --migration {migration_id}`."
        )
    plan = _get_plan(state, migration_id)
    runtime.emit_payload(
        {
            "program_id": program_id,
            "migration": migration,
            "plan": plan,
            "next": _next_apply(program_id, migration_id),
        },
        state,
    )


def _get_plan(state: CLIState, migration_id: str) -> dict[str, Any]:
    return _data(
        runtime.request_json(
            state,
            "GET",
            f"{MIGRATE_API_PREFIX}/migrations/{migration_id}/plan",
        )
    )


def _hash_bound_action(
    state: CLIState,
    *,
    action: str,
    migration: dict[str, Any],
    program_id: str | None,
    options: CloudOptions,
    confirmation: str | None = None,
) -> None:
    migration_id = str(migration["id"])
    plan = _get_plan(state, migration_id)
    current_plan_hash = str(plan.get("plan_hash") or "")
    if not current_plan_hash:
        raise click.ClickException("The cloud migration does not have a reviewable plan hash.")
    if options.plan_hash and options.plan_hash != current_plan_hash:
        raise click.ClickException(
            "--plan-hash does not match the current cloud plan. Re-run plan/status and review it."
        )
    plan_hash = options.plan_hash or current_plan_hash

    if confirmation and not options.yes:
        if state.output == "json":
            raise click.ClickException(
                "Interactive confirmation is disabled for JSON output; "
                f"review plan {plan_hash} "
                "and retry with --yes."
            )
        _print_plan_preview(migration, plan)
        click.confirm(confirmation, abort=True)

    body: dict[str, Any] = {"plan_hash": plan_hash}
    if action == "apply" and options.routes:
        body["routes"] = options.routes
    result = _data(
        runtime.request_json(
            state,
            "POST",
            f"{MIGRATE_API_PREFIX}/migrations/{migration_id}/{action}",
            json_body=body,
            headers=_idempotency_headers() if action in {"apply", "repair"} else None,
        )
    )
    if options.wait:
        result = _poll_migration(
            state,
            migration_id,
            terminal_statuses=_EXECUTION_TERMINAL_STATUSES,
            timeout=options.timeout,
        )
    runtime.emit_payload(
        {
            "program_id": program_id,
            "migration": result,
            "next": f"sanka status --migration {migration_id}",
        },
        state,
    )


def _simple_action(
    state: CLIState,
    *,
    action: str,
    migration: dict[str, Any],
    program_id: str | None,
    options: CloudOptions,
) -> None:
    migration_id = str(migration["id"])
    if action == "cancel" and not options.yes:
        if state.output == "json":
            raise click.ClickException("Retry with --yes to cancel when using JSON output.")
        click.confirm(
            "Cancel this migration permanently? Destination writes are not rolled back.",
            abort=True,
        )
    result = _data(
        runtime.request_json(
            state,
            "POST",
            f"{MIGRATE_API_PREFIX}/migrations/{migration_id}/{action}",
            headers=_idempotency_headers() if action in {"verify", "review"} else None,
        )
    )
    runtime.emit_payload(
        {"program_id": program_id, action: result},
        state,
    )


def _status(
    state: CLIState,
    *,
    program_id: str | None,
    migration_id: str | None,
) -> None:
    if migration_id:
        migration = _get_migration(state, migration_id)
        if program_id:
            program = _get_program(state, program_id)
            linked = [str(value) for value in program.get("migration_ids", [])]
            if migration_id not in linked:
                raise click.ClickException(
                    f"Migration {migration_id} does not belong to Program {program_id}."
                )
            runtime.emit_payload({"program": program, "migration": migration}, state)
            return
        runtime.emit_payload(migration, state)
        return
    if not program_id:
        raise click.ClickException("Cloud status requires --program or --migration.")
    program = _get_program(state, program_id)
    migrations = [
        _get_migration(state, str(linked_id)) for linked_id in program.get("migration_ids", [])
    ]
    runtime.emit_payload({"program": program, "migrations": migrations}, state)


def _poll_migration(
    state: CLIState,
    migration_id: str,
    *,
    terminal_statuses: set[str],
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        migration = _get_migration(state, migration_id)
        if migration.get("status") in terminal_statuses:
            return migration
        if time.monotonic() >= deadline:
            raise click.ClickException(
                f"Timed out waiting for migration {migration_id}. "
                f"Continue with `sanka status --migration {migration_id}`."
            )
        time.sleep(2)


def _print_plan_preview(migration: dict[str, Any], plan: dict[str, Any]) -> None:
    summary_value = plan.get("summary")
    summary = summary_value if isinstance(summary_value, dict) else {}
    click.echo(f"Cloud migration: {migration.get('id')}")
    click.echo(f"Status: {migration.get('status')}")
    click.echo(f"Plan hash: {plan.get('plan_hash')}")
    click.echo(f"Risk: {plan.get('risk_level')}")
    click.echo(f"Estimated records: {summary.get('records_estimated', 0)}")


def _idempotency_headers() -> dict[str, str]:
    return {"Idempotency-Key": f"sanka-cli-{uuid.uuid4()}"}


def _next_apply(program_id: str | None, migration_id: str) -> str:
    if program_id:
        return f"sanka apply --program {program_id} --migration {migration_id}"
    return f"sanka apply --migration {migration_id}"
