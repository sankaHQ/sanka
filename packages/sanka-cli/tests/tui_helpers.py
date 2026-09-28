# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from sanka.cli.tui.model import (
    ExtensionChoice,
    HistoryEntry,
    JobRef,
    MarketplaceView,
    Project,
    StageOutcome,
    StageRun,
)
from sanka.cli.tui.services import preferred_extension


class FakeServices:
    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []
        self.installed: list[str] = []
        self.installed_marketplaces: list[str | None] = []
        self.disabled: list[str] = []
        self.catalog: tuple[ExtensionChoice, ...] = (
            _choice("sanka/drf-to-fastapi", "1.2.0", installed=True, targets=("fastapi",)),
            _choice("sanka/drf-to-flask", "1.0.0", installed=True, targets=("flask",)),
            _choice("sanka/python-to-go", "0.1.0", installed=False, targets=("go",)),
        )
        self.endpoint_rows: tuple[Any, ...] = ()
        self.hashes: tuple[str, ...] = ()
        self.target_names = tuple(
            sorted({target for choice in self.catalog for target in choice.targets})
        )
        self.project_view = Project(
            root="/work/demo", frameworks=("django",), languages=("python",)
        )
        self.history_rows = (
            HistoryEntry(
                stage="verify",
                outcome="succeeded",
                target="fastapi",
                plan_hash="sha256:done",
                at="2026-09-22T00:00:00+00:00",
            ),
        )
        self.job = JobRef(
            run_id="run_01K",
            workspace="78165495",
            route="cloud-run",
            stage_group="fix",
            status="running",
            command="fix",
            title="Fix",
            plan_sha="a82f18c",
            extension_id="sanka/python-to-go",
        )

    def cloud_command(self, args: list[str]) -> dict[str, Any]:
        self.calls.append({"cloud_args": args})
        return {"held": 10, "charged": 3, "released": 7}

    def cloud_identity(self, workspace: str) -> dict[str, Any]:
        return {"email": "test@example.invalid", "selected_workspace": workspace}

    def cloud_login(self, token: str) -> dict[str, Any]:
        return {"email": "test@example.invalid"}

    def fix_eligibility(self, job: JobRef) -> dict[str, Any]:
        return {
            "eligible": True,
            "parent_run_id": job.run_id,
            "workspace_code": job.workspace,
            "min_credits": 1001,
            "max_credits": 6000,
            "verification_scope": "test and verify",
        }

    def project(self) -> Project:
        return self.project_view

    def extensions(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        status: str | None = None,
        target: str | None = None,
    ) -> tuple[ExtensionChoice, ...]:
        needle = query.strip().lower()
        rows = []
        for choice in self.catalog:
            if kind and choice.kind != kind:
                continue
            label = choice.status_label.lower()
            if status and status not in choice.status and label != status.lower():
                continue
            if target and target not in choice.targets:
                continue
            haystack = " ".join((choice.id, choice.kind, choice.label, *choice.targets)).lower()
            if needle and needle not in haystack:
                continue
            rows.append(choice)
        return tuple(rows)

    def extension_settings(self, extension_id: str) -> Any:
        return self.settings.get(extension_id)

    def extension(self, extension_id: str) -> ExtensionChoice | None:
        return preferred_extension(self.catalog, extension_id)

    def marketplaces(self) -> tuple[MarketplaceView, ...]:
        return (
            MarketplaceView(
                name="sanka",
                identity="github.com/sankaHQ/extensions",
                revision="",
                commit="abc",
                trusted=True,
            ),
        )

    def used_extension_id(self, target: str | None = None) -> str | None:
        return "sanka/drf-to-fastapi" if target in {None, "fastapi"} else None

    def targets(self) -> tuple[str, ...]:
        return self.target_names

    def plan_hashes(self) -> tuple[str, ...]:
        return self.hashes

    def saved_endpoints(self) -> tuple[Any, ...]:
        return self.endpoint_rows

    def install(self, extension_id: str, marketplace: str | None = None) -> str:
        self.installed.append(extension_id)
        self.installed_marketplaces.append(marketplace)
        self.catalog = tuple(
            replace(choice, status=frozenset({"available", "installed", "locked"}))
            if choice.id == extension_id
            else choice
            for choice in self.catalog
        )
        return f"Installed {extension_id}"

    def remove(self, extension_id: str) -> str:
        return f"Removed {extension_id}"

    def disable(self, extension_id: str) -> str:
        self.disabled.append(extension_id)
        return f"Disabled {extension_id}"

    def add_marketplace(
        self,
        source: str,
        *,
        name: str | None,
        trust: bool,
        revision: str | None,
    ) -> str:
        return f"Added {source}"

    def upgrade_marketplace(self, name: str | None) -> str:
        return "Upgraded"

    def remove_marketplace(self, name: str) -> str:
        return f"Removed {name}"

    def run_local(
        self,
        command: str,
        *,
        target: str | None,
        plan_hash: str | None,
        configuration: Mapping[str, Any],
        on_activity: Any,
        endpoints: tuple[str, ...] = (),
        explicit_env_names: tuple[str, ...] = (),
    ) -> StageOutcome:
        on_activity(f"Waiting on the {command} extension")
        self.calls.append(
            {
                "command": command,
                "target": target,
                "plan_hash": plan_hash,
                "configuration": dict(configuration),
                "endpoints": endpoints,
                "explicit_env_names": explicit_env_names,
            }
        )
        data: dict[str, Any] = {"plan_hash": "sha256:planned"} if command == "plan" else {}
        if command == "verify":
            data["routes"] = [{"route_key": "GET /users", "ok": False, "failed": 1}]
        run = StageRun(
            command=command,
            phase="succeeded" if command != "verify" else "failed",
            plan_hash=str(data.get("plan_hash") or plan_hash or ""),
            message=f"{command} complete",
            result_data=data,
            extension_id="sanka/drf-to-fastapi",
        )
        if command == "verify":
            from sanka.cli.tui.model import parse_routes

            run.routes = parse_routes(data)
        return StageOutcome(run=run, exit_code=0 if command != "verify" else 1)

    def history(self) -> tuple[HistoryEntry, ...]:
        return self.history_rows

    def ledger(self) -> tuple[str, ...]:
        return ("sanka.yaml is present. The spec-file ledger has no state database yet.",)

    def cloud_jobs(self) -> tuple[JobRef, ...]:
        return ()

    def cloud_poll(self, job: JobRef) -> JobRef:
        job.status = "succeeded"
        job.outcome = "checks_passed"
        job.current_task = "checks_passed"
        return job

    def cloud_events(self, job: JobRef, cursor: int) -> tuple[int, tuple[str, ...]]:
        return cursor + 1, ("worker started",)

    def cloud_cancel(self, job: JobRef) -> str:
        return f"Cancel requested for {job.run_id}"

    def record(self, entry: HistoryEntry) -> None:
        return None


def _choice(
    extension_id: str,
    version: str,
    *,
    installed: bool,
    targets: tuple[str, ...],
) -> ExtensionChoice:
    status = frozenset({"installed", "locked"}) if installed else frozenset({"available"})
    return ExtensionChoice(
        id=extension_id,
        version=version,
        marketplace="sanka",
        marketplace_identity="github.com/sankaHQ/extensions",
        kind="migration",
        targets=targets,
        status=status,
        commands=("scan", "plan", "apply", "test", "verify"),
        runtime_specifier=">=0.2",
    )


# A settings declaration as an extension wheel ships it.
DECLARATION = {
    "schema_version": "sanka-extension-settings/v1",
    "display": {"name": {"en": "DRF to Flask", "ja": "DRF から Flask"}},
    "settings": [
        {
            "id": "orm",
            "stage": "plan",
            "type": "choice",
            "default": "django",
            "label": {"en": "ORM", "ja": "ORM"},
            "choices": [
                {"value": "django", "label": {"en": "Django", "ja": "Django"}},
                {"value": "sqlalchemy", "label": {"en": "SQLAlchemy", "ja": "SQLAlchemy"}},
            ],
        },
        {
            "id": "generation",
            "stage": "plan",
            "type": "choice",
            "default": "minimal",
            "label": {"en": "Generation layout", "ja": "生成レイアウト"},
            "when": {"orm": "sqlalchemy"},
            "choices": [
                {"value": "minimal", "label": {"en": "Minimal", "ja": "最小"}},
                {"value": "full", "label": {"en": "Full", "ja": "フル"}},
            ],
        },
        {
            "id": "min_readiness",
            "stage": "plan",
            "type": "integer",
            "default": 0,
            "minimum": 0,
            "maximum": 100,
            "label": {"en": "Minimum readiness (%)", "ja": "最低移行準備度 (%)"},
        },
    ],
}
