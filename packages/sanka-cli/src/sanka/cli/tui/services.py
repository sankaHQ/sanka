# SPDX-License-Identifier: AGPL-3.0-only
"""Host adapters behind the Textual screens. Tests substitute a fake."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sanka.cli.tui.history import append_history, load_history
from sanka.cli.tui.model import (
    EndpointChoice,
    ExtensionChoice,
    HistoryEntry,
    JobRef,
    MarketplaceView,
    Project,
    StageOutcome,
    StageRun,
    extension_from_recommendation,
    parse_endpoints,
    parse_routes,
    parse_tests,
    progress_fraction,
)
from sanka.runtime.extensions.model import ExtensionError
from sanka.runtime.hashing import content_hash

Activity = Callable[[str], None]


class TuiServices(Protocol):
    def project(self) -> Project: ...

    def extensions(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        status: str | None = None,
        target: str | None = None,
    ) -> tuple[ExtensionChoice, ...]: ...

    def extension(self, extension_id: str) -> ExtensionChoice | None: ...

    def marketplaces(self) -> tuple[MarketplaceView, ...]: ...

    def used_extension_id(self, target: str | None = None) -> str | None: ...

    def targets(self) -> tuple[str, ...]: ...

    def plan_hashes(self) -> tuple[str, ...]: ...

    def saved_endpoints(self) -> tuple[EndpointChoice, ...]: ...

    def install(self, extension_id: str, marketplace: str | None = None) -> str: ...

    def remove(self, extension_id: str) -> str: ...

    def disable(self, extension_id: str) -> str: ...

    def add_marketplace(
        self,
        source: str,
        *,
        name: str | None,
        trust: bool,
        revision: str | None,
    ) -> str: ...

    def upgrade_marketplace(self, name: str | None) -> str: ...

    def remove_marketplace(self, name: str) -> str: ...

    def run_local(
        self,
        command: str,
        *,
        target: str | None,
        plan_hash: str | None,
        configuration: Mapping[str, Any],
        on_activity: Activity,
        endpoints: tuple[str, ...] = (),
        explicit_env_names: tuple[str, ...] = (),
    ) -> StageOutcome: ...

    def history(self) -> tuple[HistoryEntry, ...]: ...

    def ledger(self) -> tuple[str, ...]: ...

    def cloud_jobs(self) -> tuple[JobRef, ...]: ...

    def cloud_poll(self, job: JobRef) -> JobRef: ...

    def cloud_events(self, job: JobRef, cursor: int) -> tuple[int, tuple[str, ...]]: ...

    def cloud_cancel(self, job: JobRef) -> str: ...

    def cloud_command(self, args: list[str]) -> dict[str, Any]: ...

    def cloud_identity(self, workspace: str) -> dict[str, Any]: ...

    def cloud_login(self, token: str) -> dict[str, Any]: ...

    def fix_eligibility(self, job: JobRef) -> dict[str, Any]: ...

    def record(self, entry: HistoryEntry) -> None: ...


def _choice_from_record(
    record: Any, *, runtime_specifier: str, commands: tuple[str, ...]
) -> ExtensionChoice:
    return ExtensionChoice(
        id=record.id,
        version=record.version,
        marketplace=record.marketplace,
        marketplace_identity=record.marketplace_identity,
        kind=record.kind,
        targets=tuple(record.targets),
        status=frozenset(record.status),
        commands=commands,
        runtime_specifier=runtime_specifier,
        manifest_digest=record.manifest_digest,
        wheels=tuple(wheel.name for wheel in record.wheels),
    )


def read_plan_hash(root: Path, artifact_dir: str) -> str | None:
    path = root / artifact_dir / "plan.json"
    try:
        payload = json_loads(path)
    except (OSError, ValueError):
        return None
    reviewed = payload.get("plan_hash")
    if not isinstance(reviewed, str):
        return None
    unsigned = {key: value for key, value in payload.items() if key != "plan_hash"}
    if content_hash(unsigned) != reviewed:
        return None
    return reviewed


def json_loads(path: Path) -> dict[str, Any]:
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("expected an object")
    return payload


class HostServices:
    """Calls the lifecycle and extension store the CLI already uses."""

    def __init__(
        self,
        root: Path,
        *,
        artifact_dir: str = ".sanka",
        cli_state: Any | None = None,
        workspace: str | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.artifact_dir = artifact_dir
        from sanka_cli.state import CLIState

        self.cli_state = cli_state or CLIState(None, None, "json")
        self.workspace = workspace
        self._stderr = ""

    def _store(self) -> Any:
        from sanka.runtime.extensions.store import ExtensionStore

        return ExtensionStore(self.root)

    def project(self) -> Project:
        spec = (self.root / "sanka.yaml").is_file()
        try:
            from sanka.runtime.extensions.discovery import fingerprint_repository

            fingerprint = fingerprint_repository(self.root)
        except Exception as error:
            return Project(root=str(self.root), has_spec=spec, note=str(error))
        return Project(
            root=str(self.root),
            languages=tuple(fingerprint.languages),
            frameworks=tuple(fingerprint.frameworks),
            has_spec=spec,
            fingerprint_hash=fingerprint.hash,
        )

    def _manifest_facts(self, store: Any) -> dict[str, tuple[str, tuple[str, ...]]]:
        facts: dict[str, tuple[str, tuple[str, ...]]] = {}
        try:
            catalog = store._catalog()
        except ExtensionError:
            return facts
        for _marketplace, manifest in catalog:
            facts[manifest.id] = (manifest.runtime_sanka_cli, tuple(manifest.commands))
        return facts

    def extensions(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        status: str | None = None,
        target: str | None = None,
    ) -> tuple[ExtensionChoice, ...]:
        store = self._store()
        try:
            records = store.list_extensions()
            facts = self._manifest_facts(store)
        except ExtensionError:
            return ()
        finally:
            store.close()
        needle = query.strip().lower()
        choices: list[ExtensionChoice] = []
        for record in records:
            runtime, commands = facts.get(record.id, ("", ()))
            choice = _choice_from_record(record, runtime_specifier=runtime, commands=commands)
            if kind and choice.kind != kind:
                continue
            if status and status not in choice.status and choice.status_label != status:
                continue
            if target and target not in choice.targets:
                continue
            haystack = " ".join((choice.id, choice.kind, *choice.targets, choice.label)).lower()
            if needle and needle not in haystack:
                continue
            choices.append(choice)
        return tuple(choices)

    def extension(self, extension_id: str) -> ExtensionChoice | None:
        return next((item for item in self.extensions() if item.id == extension_id), None)

    def marketplaces(self) -> tuple[MarketplaceView, ...]:
        store = self._store()
        try:
            records = store.marketplaces()
        except ExtensionError:
            return ()
        finally:
            store.close()
        return tuple(
            MarketplaceView(
                name=record.name,
                identity=record.identity,
                revision=record.revision or "",
                commit=record.resolved_commit or "",
                trusted=record.trusted,
            )
            for record in records
        )

    def used_extension_id(self, target: str | None = None) -> str | None:
        try:
            from sanka.runtime.extensions.discovery import fingerprint_repository

            fingerprint = fingerprint_repository(self.root)
            store = self._store()
            try:
                recommendations = store.recommendations(fingerprint)
            finally:
                store.close()
        except Exception:
            return None
        enabled = [
            item
            for item in recommendations
            if "locked" in item.status
            and "disabled" not in item.status
            and "incompatible" not in item.status
        ]
        if target:
            matched = [item for item in enabled if target in item.targets]
            if len(matched) == 1:
                return str(matched[0].id)
        if len(enabled) == 1:
            return str(enabled[0].id)
        return None

    def targets(self) -> tuple[str, ...]:
        found = {
            target
            for choice in self.extensions()
            if "incompatible" not in choice.status
            for target in choice.targets
        }
        return tuple(sorted(found))

    def plan_hashes(self) -> tuple[str, ...]:
        reviewed = read_plan_hash(self.root, self.artifact_dir)
        return (reviewed,) if reviewed else ()

    def saved_endpoints(self) -> tuple[EndpointChoice, ...]:
        path = self.root / self.artifact_dir / "scan.json"
        try:
            payload = json_loads(path)
        except (OSError, ValueError):
            return ()
        return parse_endpoints(payload)

    def install(self, extension_id: str, marketplace: str | None = None) -> str:
        store = self._store()
        try:
            entry = store.add_extension(extension_id, marketplace=marketplace)
        finally:
            store.close()
        return f"Installed {entry.id} {entry.version}"

    def remove(self, extension_id: str) -> str:
        store = self._store()
        try:
            store.remove_extension(extension_id)
        finally:
            store.close()
        from sanka.runtime.extensions.store import DEFAULT_EXTENSION_ID

        note = ""
        if extension_id == DEFAULT_EXTENSION_ID:
            note = " The default extension is also recorded as disabled."
        return f"Removed {extension_id}.{note}"

    def disable(self, extension_id: str) -> str:
        store = self._store()
        try:
            store.set_extension_enabled(extension_id, enabled=False)
        finally:
            store.close()
        return f"Disabled {extension_id}"

    def add_marketplace(
        self,
        source: str,
        *,
        name: str | None,
        trust: bool,
        revision: str | None,
    ) -> str:
        store = self._store()
        try:
            record = store.add_marketplace(source, name=name, trust=trust, revision=revision)
        finally:
            store.close()
        return f"Added marketplace {record.name}"

    def upgrade_marketplace(self, name: str | None) -> str:
        store = self._store()
        try:
            records = store.upgrade_marketplace(name)
        finally:
            store.close()
        return f"Upgraded {len(records)} marketplace snapshot(s)"

    def remove_marketplace(self, name: str) -> str:
        store = self._store()
        try:
            record = store.remove_marketplace(name)
        finally:
            store.close()
        return f"Removed marketplace {record.name}"

    def run_local(
        self,
        command: str,
        *,
        target: str | None,
        plan_hash: str | None,
        configuration: Mapping[str, Any],
        on_activity: Activity,
        endpoints: tuple[str, ...] = (),
        explicit_env_names: tuple[str, ...] = (),
    ) -> StageOutcome:
        from sanka.runtime.extensions.lifecycle import ApplicationLifecycle
        from sanka.runtime.extensions.runner import ExtensionRunner

        cleaned = {
            key: value for key, value in dict(configuration).items() if key != "selected_endpoints"
        }
        on_activity("Checking the project fingerprint")
        on_activity("Checking the extension lock")
        self._stderr = ""

        def on_stderr(chunk: bytes) -> None:
            self._stderr += chunk.decode("utf-8", errors="replace")
            while "\n" in self._stderr:
                line, self._stderr = self._stderr.split("\n", 1)
                if line.strip():
                    on_activity(line.rstrip())

        runner = ExtensionRunner(on_stderr=on_stderr)
        lifecycle = ApplicationLifecycle(
            self.root,
            artifact_dir=self.artifact_dir,
            interactive=False,
            runner=runner,
        )
        on_activity(f"Waiting on the {command} extension")
        started = datetime.now(UTC)
        try:
            result = _dispatch_lifecycle(
                lifecycle,
                command,
                target=target,
                plan_hash=plan_hash,
                configuration=cleaned,
                explicit_env_names=explicit_env_names,
            )
        except ExtensionError as error:
            outcome = _outcome_from_error(command, error, started)
            return self._record_outcome(outcome, target, cleaned, endpoints)
        except Exception as error:
            outcome = _outcome_from_error(
                command,
                ExtensionError("SANKA_FAILED", str(error)),
                started,
            )
            return self._record_outcome(outcome, target, cleaned, endpoints)
        data = dict(result.data)
        plan_value = data.get("plan_hash")
        extension_id = ""
        extension = data.get("extension")
        if isinstance(extension, dict):
            raw_id = extension.get("id")
            if isinstance(raw_id, str):
                extension_id = raw_id
        run = StageRun(
            command=command,
            phase="succeeded" if result.outcome == "success" else "failed",
            started_at=started,
            extension_id=extension_id,
            plan_hash=plan_value if isinstance(plan_value, str) else "",
            message=f"{command} {result.outcome}",
            result_data=data,
            limitations=tuple(result.limitations),
            artifacts=tuple(result.artifacts),
            next_actions=tuple(result.next_actions),
            progress=progress_fraction(data.get("progress")),
            tests=parse_tests(data),
            routes=parse_routes(data),
        )
        return self._record_outcome(
            StageOutcome(run=run, exit_code=0 if run.phase == "succeeded" else 1),
            target,
            cleaned,
            endpoints,
        )

    def _record_outcome(
        self,
        outcome: StageOutcome,
        target: str | None,
        configuration: dict[str, Any],
        endpoints: tuple[str, ...],
    ) -> StageOutcome:
        run = outcome.run
        run.finished_at = datetime.now(UTC)
        # Credentials belong in the credential store, never in local attempt history.
        safe = {
            key: value
            for key, value in configuration.items()
            if key
            in {
                "output",
                "generation",
                "strategy",
                "package_manager",
                "orm",
                "settings_module",
                "swagger_ui",
            }
        }
        self.record(
            HistoryEntry(
                stage=run.command,
                outcome=run.phase,
                target=target or "",
                plan_hash=run.plan_hash,
                at=(run.started_at or run.finished_at).isoformat(),
                endpoints=endpoints,
                error_code=run.error_code,
                message=run.error_message or run.message,
                duration=run.elapsed,
                artifacts=run.artifacts,
                configuration=safe,
            )
        )
        return outcome

    def history(self) -> tuple[HistoryEntry, ...]:
        return load_history(self.root, self.artifact_dir)

    def ledger(self) -> tuple[str, ...]:
        spec = self.root / "sanka.yaml"
        database = self.root / ".sanka" / "migrate" / "state.db"
        if not spec.is_file() and not database.is_file():
            return ()
        if not database.is_file():
            return ("sanka.yaml is present. The spec-file ledger has no state database yet.",)
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT status, COUNT(*) FROM runs GROUP BY status ORDER BY status"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as error:
            return (f"sanka.yaml ledger could not be read: {error}",)
        if not rows:
            return ("sanka.yaml is present. The spec-file ledger has no runs yet.",)
        return tuple(f"{status}: {count}" for status, count in rows)

    def cloud_jobs(self) -> tuple[JobRef, ...]:
        if self.cli_state is None or not self.workspace:
            return ()
        try:
            from sanka_cli.commands.cloud import ROOT, _read
        except Exception:
            return ()
        try:
            envelope = _read(self.cli_state, self.workspace, ROOT, limit=20)
        except Exception:
            return ()
        payload = envelope.get("data", envelope) if isinstance(envelope, dict) else []
        rows: list[Any] = payload if isinstance(payload, list) else []
        if isinstance(payload, dict):
            for key in ("runs", "items", "results"):
                if isinstance(payload.get(key), list):
                    rows = payload[key]
                    break
        jobs: list[JobRef] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            run_id = item.get("id") or item.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                continue
            jobs.append(
                JobRef(
                    run_id=run_id,
                    workspace=self.workspace,
                    route="discover",
                    stage_group=str(item.get("kind") or item.get("stage") or "cloud"),
                    status=str(item.get("status") or "unknown"),
                    command=str(item.get("stage") or "verify"),
                    plan_sha=str(item.get("plan_sha256") or item.get("plan_hash") or ""),
                    extension_id=str(item.get("recipe") or item.get("extension_id") or ""),
                    started_at=str(item.get("created_at") or item.get("started_at") or ""),
                    outcome=str(item.get("status") or ""),
                    progress=progress_fraction(item.get("progress")),
                    title="Cloud",
                    receipt_command=f"sanka cloud receipt --workspace {self.workspace} {run_id}",
                    raw=item,
                )
            )
        return tuple(jobs)

    def cloud_poll(self, job: JobRef) -> JobRef:
        from sanka_cli.commands.cloud import _data, _read
        from sanka_cli.commands.code_cloud import ROOT as CODE_ROOT

        if self.cli_state is None:
            return job
        if job.route == "discover":
            from dataclasses import replace

            import click

            try:
                operation = _data(
                    _read(self.cli_state, job.workspace, f"{CODE_ROOT}/runs/{job.run_id}")
                )
            except click.ClickException:
                job = replace(job, route="cloud-run")
            else:
                if str((operation.get("run") or {}).get("id")) != job.run_id:
                    raise ValueError("Cloud returned a different run.")
                stage = "plan" if operation.get("operation") == "prepare" else "verify"
                return _job_from_code(stage, operation, job.workspace, title="Cloud")
        path = (
            f"{CODE_ROOT}/runs/{job.run_id}"
            if job.route == "code-plan"
            else f"/v2/migrate/cloud-runs/{job.run_id}"
        )
        payload = _data(_read(self.cli_state, job.workspace, path))
        returned_id = (
            (payload.get("run") or {}).get("id") if job.route == "code-plan" else payload.get("id")
        )
        if str(returned_id) != job.run_id:
            raise ValueError("Cloud returned a different run; the selected run was not changed.")
        if job.route == "code-plan":
            return _job_from_code(job.command, payload, job.workspace, title=job.title)
        return _job_from_cloud_run(payload, job)

    def cloud_events(self, job: JobRef, cursor: int) -> tuple[int, tuple[str, ...]]:
        if self.cli_state is None or job.route != "cloud-run":
            return cursor, ()
        from sanka_cli.commands.cloud import _data, _read

        try:
            payload = _data(
                _read(
                    self.cli_state,
                    job.workspace,
                    f"/v2/migrate/cloud-runs/{job.run_id}/events",
                    cursor=cursor,
                )
            )
        except Exception as error:
            return cursor, (str(error),)
        events = payload.get("events") or payload.get("items") or []
        lines: list[str] = []
        if isinstance(events, list):
            for event in events:
                if isinstance(event, str):
                    lines.append(event)
                elif isinstance(event, dict):
                    summary = event.get("message") or event.get("type") or event.get("status")
                    lines.append(str(summary or event))
        next_cursor = payload.get("next_cursor", payload.get("cursor", cursor))
        return (int(next_cursor) if isinstance(next_cursor, int) else cursor), tuple(lines)

    def cloud_cancel(self, job: JobRef) -> str:
        if job.route != "cloud-run":
            return "Detach leaves this hosted run in place. This screen does not cancel it."
        if self.cli_state is None:
            return "No signed-in profile is available to cancel this run."
        import sanka_cli.runtime as runtime
        from sanka_cli.commands.cloud import ROOT

        runtime.request_json(
            self.cli_state,
            "POST",
            f"{ROOT}/{job.run_id}/cancel",
            headers={"X-Workspace-Code": job.workspace},
        )
        return f"Cancel requested for {job.run_id}"

    def cloud_command(self, args: list[str]) -> dict[str, Any]:
        """Reuse CLI validation, intent persistence and verified artifact downloads."""
        import json
        import subprocess
        import sys

        command = [sys.executable, "-m", "sanka_cli", "--output", "json"]
        if self.cli_state is not None:
            if self.cli_state.profile:
                command += ["--profile", self.cli_state.profile]
            if self.cli_state.base_url:
                command += ["--base-url", self.cli_state.base_url]
        process = subprocess.run(command + args, cwd=self.root, capture_output=True, text=True)
        if process.returncode:
            raise ValueError(process.stderr.strip() or "Cloud command failed")
        payload = json.loads(process.stdout)
        data = payload.get("data", payload) if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError("Cloud command returned an invalid response object.")
        return data

    def cloud_identity(self, workspace: str) -> dict[str, Any]:
        from sanka_cli import runtime
        from sanka_cli.commands.cloud import ROOT, _read

        resolved = runtime.resolve_runtime(
            profile_name=self.cli_state.profile, base_url_override=self.cli_state.base_url
        )
        self.cli_state.profile = resolved["profile_name"]
        payload = runtime.request_json(self.cli_state, "GET", "/v2/public/auth/whoami")
        identity = payload.get("data", payload)
        # Pin and verify workspace access before exposing any paid actions.
        _read(self.cli_state, workspace, ROOT, limit=1)
        self.workspace = workspace
        return {
            **identity,
            "selected_workspace": workspace,
            "profile": self.cli_state.profile or "default",
        }

    def cloud_login(self, token: str) -> dict[str, Any]:
        from sanka_cli import runtime
        from sanka_cli.config import DEFAULT_BASE_URL

        profile = self.cli_state.profile or "default"
        base = self.cli_state.base_url or DEFAULT_BASE_URL
        identity, warning = runtime.verify_access_token(base_url=base, access_token=token)
        if identity is None:
            raise ValueError(warning or "Could not verify token; nothing was saved.")
        runtime.upsert_profile(profile, base_url=base)
        runtime.store_tokens(profile, access_token=token, refresh_token=None)
        self.cli_state.profile = profile
        return identity

    def fix_eligibility(self, job: JobRef) -> dict[str, Any]:
        from sanka_cli.commands.cloud import ROOT, _data, _read

        result = _data(_read(self.cli_state, job.workspace, f"{ROOT}/{job.run_id}/fix"))
        if (
            str(result.get("parent_run_id")) != job.run_id
            or str(result.get("workspace_code")) != job.workspace
        ):
            raise ValueError("Fix eligibility returned a different workspace or run.")
        return result

    def record(self, entry: HistoryEntry) -> None:
        append_history(self.root, entry, self.artifact_dir)


def _dispatch_lifecycle(
    lifecycle: Any,
    command: str,
    *,
    target: str | None,
    plan_hash: str | None,
    configuration: Mapping[str, Any],
    explicit_env_names: tuple[str, ...] = (),
) -> Any:
    if command == "scan":
        return lifecycle.scan(configuration=configuration, explicit_env_names=explicit_env_names)
    if command == "plan":
        return lifecycle.plan(
            target=target, configuration=configuration, explicit_env_names=explicit_env_names
        )
    if command == "apply":
        if not plan_hash:
            raise ExtensionError("SANKA_USAGE", "A reviewed plan hash is required")
        return lifecycle.apply(
            reviewed_plan_hash=plan_hash,
            configuration=configuration,
            explicit_env_names=explicit_env_names,
        )
    if command == "test":
        return lifecycle.test(configuration=configuration, explicit_env_names=explicit_env_names)
    if command == "verify":
        return lifecycle.verify(configuration=configuration, explicit_env_names=explicit_env_names)
    raise ExtensionError("SANKA_USAGE", f"Unsupported stage {command}")


def _details_text(details: object) -> str:
    if not isinstance(details, dict) or not details:
        return ""
    lines: list[str] = []
    for key in sorted(details):
        value = details[key]
        if isinstance(value, list):
            lines.append(f"{key}: {len(value)}")
        elif isinstance(value, dict):
            lines.append(key)
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _outcome_from_error(command: str, error: ExtensionError, started: datetime) -> StageOutcome:
    recommendations = error.details.get("recommendations")
    missing: tuple[ExtensionChoice, ...] = ()
    if error.code == "SANKA_EXTENSION_REQUIRED" and isinstance(recommendations, list):
        missing = tuple(
            item
            for item in (extension_from_recommendation(value) for value in recommendations)
            if item is not None
        )
    inputs: tuple[str, ...] = ()
    raw_inputs = error.details.get("inputs")
    if error.code == "SANKA_EXTENSION_INPUT_REQUIRED" and isinstance(raw_inputs, list):
        inputs = tuple(item for item in raw_inputs if isinstance(item, str) and item)
    run = StageRun(
        command=command,
        phase="failed",
        started_at=started,
        message=str(error),
        error_code=error.code,
        error_message=str(error),
        error_details=_details_text(error.details),
        result_data=dict(error.details),
        tests=parse_tests(error.details),
        routes=parse_routes(error.details),
    )
    exit_code = 2 if error.code in {"SANKA_USAGE", "SANKA_EXTENSION_TARGET_REQUIRED"} else 1
    return StageOutcome(run=run, missing=missing, inputs=inputs, exit_code=exit_code)


def _job_from_code(stage: str, operation: dict[str, Any], workspace: str, *, title: str) -> JobRef:
    raw_run = operation.get("run")
    raw_plan = operation.get("plan")
    run = raw_run if isinstance(raw_run, dict) else {}
    plan = raw_plan if isinstance(raw_plan, dict) else {}
    status = str(run.get("status") or "unknown")
    group = "scan+plan" if stage in {"scan", "plan"} else "apply+test+verify"
    run_id = str(run.get("id") or "")
    outcome = str(operation.get("outcome") or "")
    if operation.get("operation") in {"prepare", "execute"}:
        from sanka_cli.commands.code_cloud import stage_result

        outcome, _exit = stage_result(stage, operation)
    error = ""
    if status == "failed" or outcome in {"failed", "needs_review", "evidence_unavailable"}:
        error = str(operation.get("error") or run.get("error") or outcome or "failed")
    return JobRef(
        run_id=run_id,
        workspace=workspace,
        route="code-plan",
        stage_group=group,
        status=status,
        command=stage,
        plan_sha=str(plan.get("plan_sha256") or plan.get("plan_hash") or ""),
        extension_id=str(operation.get("recipe") or (run.get("request") or {}).get("recipe") or ""),
        started_at=str(run.get("created_at") or run.get("started_at") or ""),
        current_task=str(operation.get("stage_status") or status),
        last_error=error,
        outcome=outcome or status,
        progress=progress_fraction(run.get("progress") or operation.get("progress")),
        title=title,
        receipt_command=f"sanka cloud receipt --workspace {workspace} {run_id}",
        download_command=f"sanka cloud download {run_id} --workspace {workspace} --to output.zip",
        raw=operation,
    )


def _job_from_cloud_run(payload: dict[str, Any], previous: JobRef) -> JobRef:
    status = str(payload.get("status") or previous.status)
    raw_fix = payload.get("fix_result")
    fix_result = raw_fix if isinstance(raw_fix, dict) else {}
    outcome = str(fix_result.get("outcome") or status)
    error = ""
    if status in {"failed", "cancelled"} or outcome in {
        "setup_required",
        "service_error",
        "failed",
        "needs_review",
        "evidence_unavailable",
    }:
        error = str(payload.get("error") or outcome)
    return JobRef(
        run_id=str(payload.get("id") or previous.run_id),
        workspace=previous.workspace,
        route="cloud-run",
        stage_group=previous.stage_group,
        status=status,
        command="fix" if fix_result else previous.command,
        plan_sha=str(payload.get("candidate_sha256") or previous.plan_sha),
        extension_id=str(payload.get("recipe") or previous.extension_id),
        endpoint_ids=previous.endpoint_ids,
        started_at=str(payload.get("created_at") or previous.started_at),
        current_task=outcome,
        last_error=error,
        outcome=outcome,
        progress=progress_fraction(payload.get("progress")),
        title=previous.title,
        receipt_command=str(
            payload.get("receipt_command")
            or previous.receipt_command
            or f"sanka cloud receipt --workspace {previous.workspace} {previous.run_id}"
        ),
        ui_url=str(payload.get("ui_url") or previous.ui_url),
        download_command=(
            "sanka cloud download "
            f"{previous.run_id} --workspace {previous.workspace} --to output.zip"
        ),
        raw=payload,
    )


def cloud_exit_code(job: JobRef) -> int:
    """Map a hosted payload onto the exit codes the CLI already uses."""
    try:
        if job.command == "fix" or job.stage_group == "fix":
            from sanka_cli.commands.fix import _exit_code

            code = _exit_code(job.raw or {"status": job.status})
            return 6 if code is None else code
        from sanka_cli.commands.code_cloud import stage_result

        operation = (
            job.raw
            if isinstance(job.raw.get("run"), dict)
            else {
                "run": {"id": job.run_id, "status": job.status},
                "operation": "prepare" if job.command in {"scan", "plan"} else "execute",
            }
        )
        known = {"scan", "plan", "apply", "test", "verify"}
        stage = job.command if job.command in known else "scan"
        _outcome, code = stage_result(stage, operation)
        return code
    except Exception:
        if job.status == "succeeded":
            return 0
        if job.status == "cancelled":
            return 5
        if job.status == "failed":
            return 4
        return 1


def job_from_fix(run: dict[str, Any], workspace: str) -> JobRef:
    run_id = str(run.get("id") or "")
    return JobRef(
        run_id=run_id,
        workspace=workspace,
        route="cloud-run",
        stage_group="fix",
        status=str(run.get("status") or "unknown"),
        command="fix",
        plan_sha=str(run.get("candidate_sha256") or ""),
        extension_id=str(run.get("recipe") or ""),
        started_at=str(run.get("created_at") or ""),
        current_task=str(run.get("status") or ""),
        title="Fix",
        receipt_command=str(
            run.get("receipt_command") or f"sanka cloud receipt --workspace {workspace} {run_id}"
        ),
        ui_url=str(run.get("ui_url") or ""),
        download_command=(
            f"sanka cloud download {run_id} --workspace {workspace} --to fixed-output.zip"
        ),
        raw=run,
    )
