# SPDX-License-Identifier: AGPL-3.0-only
"""Session records the Textual views render. Widgets do not parse Click output."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

LIFECYCLE_COMMANDS = frozenset({"scan", "plan", "apply", "test", "verify"})
IMPLEMENTATIONS = frozenset({"not_migrated", "already_implemented", "skip", "unknown"})
_DISPLAY_WORDS = {
    "api": "API",
    "django": "Django",
    "drf": "DRF",
    "fastapi": "FastAPI",
    "flask": "Flask",
    "go": "Go",
    "python": "Python",
}


def display_name(extension_id: str) -> str:
    slug = extension_id.rsplit("/", 1)[-1]
    words = [_DISPLAY_WORDS.get(token, token.capitalize()) for token in slug.split("-") if token]
    return " ".join(words).replace(" To ", " to ") or extension_id


def command_line(
    command: str,
    *,
    target: str | None = None,
    plan_hash: str | None = None,
    cloud: bool = False,
) -> str:
    parts = ["sanka", command]
    if cloud:
        parts.append("--cloud")
    if target and command in {"scan", "plan"}:
        parts.extend(["--to", target])
    if plan_hash:
        parts.extend(["--plan-hash", plan_hash])
    parts.append("--json")
    return " ".join(parts)


def progress_fraction(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if 0 <= number <= 1:
        return number
    if 1 < number <= 100:
        return number / 100
    return None


def format_elapsed(started_at: datetime | None, *, now: datetime | None = None) -> str:
    if started_at is None:
        return ""
    current = now or datetime.now(UTC)
    seconds = max(0, int((current - started_at).total_seconds()))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


@dataclass
class Project:
    root: str
    languages: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    has_spec: bool = False
    fingerprint_hash: str = ""
    note: str = ""


@dataclass(frozen=True)
class ExtensionChoice:
    id: str
    version: str
    marketplace: str
    marketplace_identity: str
    kind: str
    targets: tuple[str, ...]
    status: frozenset[str]
    commands: tuple[str, ...] = ()
    runtime_specifier: str = ""
    manifest_digest: str = ""
    wheels: tuple[str, ...] = ()
    add_command: str = ""

    @property
    def label(self) -> str:
        return display_name(self.id)

    @property
    def status_label(self) -> str:
        if "disabled" in self.status:
            return "Disabled"
        if "incompatible" in self.status:
            return "Incompatible"
        if "update_available" in self.status:
            return "Update available"
        if "installed" in self.status or "locked" in self.status:
            return "Installed"
        return "Available"

    @property
    def version_label(self) -> str:
        if self.status.intersection({"installed", "locked", "update_available", "disabled"}):
            return self.version
        return "-"


@dataclass(frozen=True)
class MarketplaceView:
    name: str
    identity: str
    revision: str
    commit: str
    trusted: bool


@dataclass
class EndpointChoice:
    id: str
    method: str
    path: str
    implementation: str
    selected: bool

    @property
    def implementation_label(self) -> str:
        return {
            "not_migrated": "Not migrated",
            "already_implemented": "Already implemented",
            "skip": "Skip",
            "unknown": "Unknown",
        }.get(self.implementation, "Unknown")


def parse_endpoints(data: object) -> tuple[EndpointChoice, ...]:
    """Read a future extension payload. Unknown shapes yield no rows."""
    if not isinstance(data, dict):
        return ()
    raw = data.get("endpoints")
    if not isinstance(raw, list):
        return ()
    choices: list[EndpointChoice] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        method = item.get("method")
        path = item.get("path")
        if not isinstance(method, str) or not method or not isinstance(path, str) or not path:
            continue
        implementation = item.get("implementation")
        if implementation not in IMPLEMENTATIONS:
            implementation = "unknown"
        identity = item.get("id")
        choices.append(
            EndpointChoice(
                id=identity if isinstance(identity, str) and identity else f"{method} {path}",
                method=method,
                path=path,
                implementation=str(implementation),
                selected=implementation == "not_migrated",
            )
        )
    return tuple(choices)


@dataclass(frozen=True)
class TestRow:
    name: str
    status: str
    duration: str


@dataclass(frozen=True)
class RouteRow:
    name: str
    status: str
    detail: str


def parse_tests(data: object) -> tuple[TestRow, ...]:
    if not isinstance(data, dict) or not isinstance(data.get("tests"), list):
        return ()
    rows: list[TestRow] = []
    for item in data["tests"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        duration = item.get("duration")
        rows.append(
            TestRow(
                name=item["name"],
                status=str(item.get("status") or ""),
                duration="" if duration is None else str(duration),
            )
        )
    return tuple(rows)


def parse_routes(data: object) -> tuple[RouteRow, ...]:
    if not isinstance(data, dict):
        return ()
    raw = data.get("routes") if isinstance(data.get("routes"), list) else data.get("endpoints")
    if not isinstance(raw, list):
        return ()
    rows: list[RouteRow] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("route_key") or item.get("path") or item.get("name")
        if not isinstance(name, str) or not name:
            continue
        ok = item.get("ok")
        status = item.get("status")
        rendered = ("passed" if ok else "failed") if isinstance(ok, bool) else str(status or "")
        detail_bits = []
        for key in ("source_count", "migrated", "failed", "destination_count"):
            if key in item:
                detail_bits.append(f"{key}={item[key]}")
        rows.append(RouteRow(name=name, status=rendered, detail=" ".join(detail_bits)))
    return tuple(rows)


@dataclass
class StageRun:
    command: str
    execution: str = "local"
    phase: str = "idle"
    started_at: datetime | None = None
    extension_id: str = ""
    plan_hash: str = ""
    message: str = ""
    result_data: dict[str, Any] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    error_code: str = ""
    error_message: str = ""
    error_details: str = ""
    progress: float | None = None
    finished_at: datetime | None = None
    tests: tuple[TestRow, ...] = ()
    routes: tuple[RouteRow, ...] = ()

    @property
    def elapsed(self) -> str:
        return format_elapsed(self.started_at, now=self.finished_at)


@dataclass
class StageOutcome:
    run: StageRun
    missing: tuple[ExtensionChoice, ...] = ()
    inputs: tuple[str, ...] = ()
    exit_code: int = 0


@dataclass
class JobRef:
    run_id: str
    workspace: str
    route: str
    stage_group: str
    status: str
    command: str = "scan"
    plan_sha: str = ""
    extension_id: str = ""
    endpoint_ids: tuple[str, ...] = ()
    started_at: str = ""
    current_task: str = ""
    last_error: str = ""
    outcome: str = ""
    progress: float | None = None
    title: str = "Cloud"
    receipt_command: str = ""
    ui_url: str = ""
    download_command: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def execution(self) -> str:
        return "cloud"


@dataclass(frozen=True)
class HistoryEntry:
    stage: str
    outcome: str
    target: str
    plan_hash: str
    at: str
    execution: str = "local"
    endpoints: tuple[str, ...] = ()

    error_code: str = ""
    message: str = ""
    duration: str = ""
    artifacts: tuple[str, ...] = ()
    configuration: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "at": self.at,
            "execution": self.execution,
            "outcome": self.outcome,
            "plan_hash": self.plan_hash,
            "stage": self.stage,
            "target": self.target,
            "error_code": self.error_code,
            "message": self.message,
            "duration": self.duration,
            "artifacts": list(self.artifacts),
            "configuration": self.configuration,
        }
        if self.endpoints:
            payload["endpoints"] = list(self.endpoints)
        return payload

    @classmethod
    def from_dict(cls, value: object) -> HistoryEntry | None:
        if not isinstance(value, dict):
            return None
        stage = value.get("stage")
        outcome = value.get("outcome")
        if not isinstance(stage, str) or not isinstance(outcome, str):
            return None
        raw_endpoints = value.get("endpoints")
        endpoints = (
            tuple(item for item in raw_endpoints if isinstance(item, str))
            if isinstance(raw_endpoints, list)
            else ()
        )
        return cls(
            stage=stage,
            outcome=outcome,
            target=str(value.get("target") or ""),
            plan_hash=str(value.get("plan_hash") or ""),
            at=str(value.get("at") or ""),
            execution=str(value.get("execution") or "local"),
            endpoints=endpoints,
            error_code=str(value.get("error_code") or ""),
            message=str(value.get("message") or ""),
            duration=str(value.get("duration") or ""),
            artifacts=tuple(
                str(item)
                for item in (value["artifacts"] if isinstance(value.get("artifacts"), list) else [])
                if isinstance(item, str)
            ),
            configuration=dict(value["configuration"])
            if isinstance(value.get("configuration"), dict)
            else {},
        )


@dataclass
class Session:
    project_root: str
    command: str = "status"
    target: str | None = None
    plan_hash: str | None = None
    configuration: dict[str, Any] = field(default_factory=dict)
    explicit_env_names: tuple[str, ...] = ()
    endpoints: list[EndpointChoice] = field(default_factory=list)
    stage: StageRun = field(default_factory=lambda: StageRun("status"))
    job: JobRef | None = None
    direct: bool = False
    exit_code: int = 0
    busy: bool = False
    workspace: str | None = None
    pending_extension_id: str | None = None
    pending_action: str | None = None
    notice: str = ""
    autostart: bool = False
    profile: str | None = None
    base_url: str | None = None
    artifact_dir: str = ".sanka"
    stages: dict[str, StageRun] = field(default_factory=dict)

    def footer_line(self) -> str:
        if self.command == "marketplace":
            return "sanka extension marketplace list --json"
        if self.command == "extension":
            return "sanka extension list --json"
        parts = ["sanka"]
        if self.profile:
            parts += ["--profile", self.profile]
        if self.base_url:
            parts += ["--base-url", self.base_url]
        if self.job is not None and self.command in LIFECYCLE_COMMANDS | {"fix"}:
            parts += [
                "--output",
                "json",
                "cloud",
                "status",
                self.job.run_id,
                "--workspace",
                self.job.workspace,
            ]
            return shlex.join(parts)
        else:
            command = self.command if self.command in LIFECYCLE_COMMANDS else "status"
            parts += [command]
            if command in {"scan", "plan"}:
                parts += [self.project_root]
            elif command in LIFECYCLE_COMMANDS:
                parts += ["--root", self.project_root]
            if command in LIFECYCLE_COMMANDS:
                parts += ["--artifact-dir", self.artifact_dir]
                if self.target and command == "plan":
                    parts += ["--to", self.target]
                if self.plan_hash and command == "apply":
                    parts += ["--plan-hash", self.plan_hash]
                for name in self.explicit_env_names:
                    parts += ["--extension-env", name]
                if self.configuration:
                    parts += ["--extension-config", json.dumps(self.configuration)]
        return shlex.join([*parts, "--json"])


def extension_from_recommendation(value: object) -> ExtensionChoice | None:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        return None
    status = value.get("status")
    targets = value.get("targets")
    commands = value.get("commands")
    return ExtensionChoice(
        id=value["id"],
        version=str(value.get("version") or ""),
        marketplace=str(value.get("marketplace") or ""),
        marketplace_identity=str(value.get("marketplace_identity") or ""),
        kind="migration",
        targets=tuple(item for item in targets if isinstance(item, str))
        if isinstance(targets, list)
        else (),
        status=frozenset(item for item in status if isinstance(item, str))
        if isinstance(status, list)
        else frozenset(),
        commands=tuple(item for item in commands if isinstance(item, str))
        if isinstance(commands, list)
        else (),
        manifest_digest=str(value.get("manifest_digest") or ""),
        add_command=str(value.get("add_command") or ""),
    )
