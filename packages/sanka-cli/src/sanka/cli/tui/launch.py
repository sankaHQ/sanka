# SPDX-License-Identifier: AGPL-3.0-only
"""Open Textual only for an explicit TUI request."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from sanka.cli.tui.app import SankaApp
from sanka.cli.tui.model import Session, StageRun
from sanka.cli.tui.services import HostServices, _job_from_code, job_from_fix
from sanka.runtime.extensions.model import ExtensionError
from sanka_cli.state import CLIState

_LIFECYCLE = frozenset({"scan", "plan", "apply", "test", "verify"})


def use_human_tui(state: Any) -> bool:
    from sanka_cli.state import validate_tui

    return validate_tui(state, terminal=sys.stdin.isatty() and sys.stdout.isatty())


def launch_dashboard(
    root: Path | None = None,
    *,
    state: CLIState | None = None,
    explicit_env_names: tuple[str, ...] = (),
) -> int:
    project = (root or Path(".")).expanduser().resolve()
    services = HostServices(project, cli_state=state)
    configuration = services.saved_configuration()
    app = SankaApp(
        services,
        Session(
            project_root=str(project),
            configuration=configuration,
            target=configuration.get("target"),
            explicit_env_names=explicit_env_names,
            profile=state.profile if state else None,
            base_url=state.base_url if state else None,
        ),
        start="status",
        ask_trust=True,
    )
    return _run(app)


def launch_doctor(state: CLIState, expected_version: str | None = None) -> int:
    root = Path.cwd()
    return _run(
        SankaApp(
            HostServices(root, cli_state=state),
            Session(project_root=str(root), command="doctor", direct=True),
            start="doctor",
            ask_trust=True,
            doctor_expected_version=expected_version,
        )
    )


def launch_local(args: Any) -> int:
    root = _root(args)
    artifact_dir = str(getattr(args, "artifact_dir", None) or ".sanka")
    import click

    context = click.get_current_context(silent=True)
    state = context.obj if context and isinstance(context.obj, CLIState) else None
    services = HostServices(root, artifact_dir=artifact_dir, cli_state=state)
    command = str(args.command)
    session = Session(
        project_root=str(root),
        command=command,
        direct=True,
        artifact_dir=artifact_dir,
        profile=services.cli_state.profile,
        base_url=services.cli_state.base_url,
    )
    start = "status"
    autostart = False
    if command in _LIFECYCLE:
        from sanka.cli import _extension_configuration
        from sanka.runtime.extensions.lifecycle import resolve_plan_configuration

        session.target = getattr(args, "to", None)
        session.plan_hash = getattr(args, "plan_hash", None)
        saved = services.saved_configuration()
        if session.target is not None and session.target != saved.get("target"):
            saved = {}
        session.configuration = resolve_plan_configuration(saved, _extension_configuration(args))
        if session.target is None:
            session.target = session.configuration.get("target")
        session.explicit_env_names = tuple(getattr(args, "extension_env", ()) or ())
        session.endpoint_ids = tuple(getattr(args, "endpoint", ()) or ())
        session.all_endpoints = bool(getattr(args, "all_endpoints", False))
        if session.endpoint_ids or session.all_endpoints:
            session.configuration.pop("selected_endpoints", None)
        session.stage = StageRun(command=command)
        session.endpoints = list(services.saved_endpoints())
        start = command
        autostart = command in {"scan", "test", "verify"}
    elif command == "extension":
        extension_command = getattr(args, "extension_command", None)
        if extension_command == "marketplace":
            start = "marketplace"
            session.command = "marketplace"
            session.notice = _marketplace_action(services, args)
        else:
            start = "extensions"
            session.command = "extension"
            session.pending_extension_id = getattr(args, "extension_id", None)
            session.pending_action = (
                extension_command if extension_command in {"add", "remove"} else None
            )
    app = SankaApp(services, session, start=start, autostart=autostart, ask_trust=True)
    return _run(app)


def monitor_code(
    state: Any,
    workspace: str,
    stage: str,
    operation: dict[str, Any],
    wait_timeout: int,
) -> int:
    job = _job_from_code(stage, operation, workspace, title="Cloud")
    root = Path(".").resolve()
    session = Session(
        project_root=str(root),
        command=stage,
        direct=True,
        job=job,
        workspace=workspace,
        profile=state.profile,
        base_url=state.base_url,
        plan_hash=job.plan_sha or None,
        stage=StageRun(command=stage, execution="cloud", phase=job.status),
    )
    app = SankaApp(
        HostServices(root, cli_state=state, workspace=workspace),
        session,
        start="monitor",
        monitor_timeout=wait_timeout,
        ask_trust=True,
    )
    return _run(app)


def monitor_fix(state: Any, workspace: str, run: dict[str, Any], wait_timeout: int) -> int:
    job = job_from_fix(run, workspace)
    root = Path(".").resolve()
    session = Session(
        project_root=str(root),
        command="fix",
        profile=state.profile,
        base_url=state.base_url,
        direct=True,
        job=job,
        workspace=workspace,
        stage=StageRun(command="fix", execution="cloud", phase=job.status),
    )
    app = SankaApp(
        HostServices(root, cli_state=state, workspace=workspace),
        session,
        start="monitor",
        monitor_timeout=wait_timeout,
        ask_trust=True,
    )
    return _run(app)


def _marketplace_action(services: HostServices, args: Any) -> str:
    command = getattr(args, "marketplace_command", None)
    try:
        if command == "add":
            return services.add_marketplace(
                str(args.source),
                name=getattr(args, "name", None),
                trust=bool(getattr(args, "trust", False)),
                revision=getattr(args, "revision", None),
            )
        if command == "upgrade":
            return services.upgrade_marketplace(getattr(args, "name", None))
        if command == "remove":
            return services.remove_marketplace(str(args.name))
    except ExtensionError as error:
        return f"{error.code}: {error}"
    return ""


def _root(args: Any) -> Path:
    raw = getattr(args, "root", None) or "."
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return path.resolve()
    except OSError:
        return path


def _run(app: SankaApp) -> int:
    result = app.run()
    return result if isinstance(result, int) else 0
