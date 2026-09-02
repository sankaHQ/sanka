# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sanka.runtime.extensions import ExtensionError, Recommendation, fingerprint_repository
from sanka.runtime.extensions.lifecycle import ApplicationLifecycle
from sanka.runtime.extensions.runner import ExtensionResult, ExtensionRunner
from sanka.runtime.extensions.store import ExtensionStore, LockEntry


def _lock(*, digest: str = "3" * 64) -> LockEntry:
    return LockEntry(
        id="sanka/drf-to-fastapi",
        version="0.1.0a1",
        marketplace_identity="github.com/sankaHQ/extensions",
        snapshot_digest="1" * 40,
        manifest_digest="sha256:" + "2" * 64,
        distribution="sanka-extension-drf-to-fastapi",
        artifact_digest=digest,
        protocol_version="sanka-extension/v1",
        executable="sanka-extension-drf-to-fastapi",
        commands=("apply", "plan", "scan", "test", "verify"),
        enabled=True,
        configuration_digest="sha256:" + "4" * 64,
    )


def _recommendation(
    lock: LockEntry,
    *,
    marketplace: str,
    targets: tuple[str, ...],
    status: tuple[str, ...] = ("available", "installed", "locked"),
) -> Recommendation:
    return Recommendation(
        id=lock.id,
        version=lock.version,
        marketplace=marketplace,
        marketplace_identity=lock.marketplace_identity,
        snapshot_digest=lock.snapshot_digest,
        manifest_digest=lock.manifest_digest,
        commands=lock.commands,
        targets=targets,
        evidence=(),
        status=status,
        add_command=f"sanka extension add {lock.id}",
    )


class FakeStore:
    def __init__(self, *, installed: bool = False, default_available: bool = False) -> None:
        self.installed = installed
        self.disabled = False
        self.default_available = default_available
        self.lock = _lock()
        self.added = 0

    def recommendations(self, _fingerprint: object) -> tuple[Recommendation, ...]:
        status = (
            ("available", "disabled")
            if self.disabled
            else (("available", "installed", "locked") if self.installed else ("available",))
        )
        return (
            Recommendation(
                id=self.lock.id,
                version=self.lock.version,
                marketplace="official",
                marketplace_identity=self.lock.marketplace_identity,
                snapshot_digest=self.lock.snapshot_digest,
                manifest_digest=self.lock.manifest_digest,
                commands=self.lock.commands,
                targets=("fastapi",),
                evidence=(),
                status=status,
                add_command="sanka extension add sanka/drf-to-fastapi",
            ),
        )

    def resolve_locked(self, extension_id: str) -> LockEntry:
        if not self.installed or extension_id != self.lock.id:
            raise ExtensionError("SANKA_EXTENSION_REQUIRED", "not installed")
        return self.lock

    def add_extension(self, extension_id: str, **_kwargs: object) -> LockEntry:
        self.added += 1
        if not self.default_available:
            raise ExtensionError("SANKA_EXTENSION_NOT_CACHED", "not installed")
        assert extension_id == self.lock.id
        self.installed = True
        self.disabled = False
        return self.lock


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[LockEntry, dict[str, Any]]] = []
        self.explicit_env_names: list[tuple[str, ...]] = []
        self.required_inputs: list[str] = []

    def run(
        self,
        lock: LockEntry,
        request: dict[str, Any],
        *,
        allowed_roots: tuple[Path, ...],
        explicit_env_names: tuple[str, ...] = (),
        executable_fd: int | None = None,
    ) -> ExtensionResult:
        del executable_fd
        self.calls.append((lock, request))
        self.explicit_env_names.append(explicit_env_names)
        if request["command"] == "plan" and self.required_inputs:
            missing = [
                name for name in self.required_inputs if name not in request["configuration"]
            ]
            if missing:
                return ExtensionResult(
                    outcome="error",
                    data={},
                    artifacts=(),
                    limitations=(),
                    next_actions=(),
                    error={
                        "code": "SANKA_EXTENSION_INPUT_REQUIRED",
                        "message": "need input",
                        "details": {"inputs": missing},
                    },
                )
        artifact_root = Path(request["artifact_root"])
        artifact_root.mkdir(parents=True, exist_ok=True)
        command = request["command"]
        artifact = artifact_root / f"{command}.json"
        artifact.write_text(json.dumps({"command": command}), encoding="utf-8")
        data: dict[str, Any] = {"extension_command": command}
        if command == "plan":
            data["plan_hash"] = "sha256:extension-plan"
        return ExtensionResult(
            outcome="success",
            data=data,
            artifacts=(str(artifact.resolve()),),
            limitations=(),
            next_actions=(),
            error=None,
        )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "manage.py").write_text("import rest_framework\n", encoding="utf-8")
    (project / "requirements.txt").write_text("djangorestframework==3.16\n", encoding="utf-8")
    return project


def _lifecycle(
    project: Path,
    store: FakeStore,
    runner: FakeRunner,
    *,
    interactive: bool = False,
    prompt: Callable[[str, tuple[str, ...] | None], str | None] | None = None,
) -> ApplicationLifecycle:
    if not hasattr(store, "execution_guard"):
        store.execution_guard = nullcontext  # type: ignore[attr-defined]
    if not hasattr(store, "execution_lease"):
        store.execution_lease = lambda _lock: nullcontext(None)  # type: ignore[attr-defined]
    return ApplicationLifecycle(
        project,
        store=cast(ExtensionStore, store),
        runner=cast(ExtensionRunner, runner),
        interactive=interactive,
        prompt=prompt,
    )


def test_json_scan_fails_closed_with_structured_recommendations(tmp_path: Path) -> None:
    project = _project(tmp_path)
    lifecycle = _lifecycle(project, FakeStore(), FakeRunner())

    with pytest.raises(ExtensionError) as raised:
        lifecycle.scan()

    assert raised.value.code == "SANKA_EXTENSION_REQUIRED"
    assert raised.value.details["fingerprint"]["frameworks"] == ["django-rest-framework"]
    assert raised.value.details["recommendations"][0]["add_command"] == (
        "sanka extension add sanka/drf-to-fastapi"
    )
    assert not (project / ".sanka" / "scan.json").exists()


def test_scan_auto_pins_the_installed_default_and_namespaces_its_data(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = FakeStore(default_available=True)
    runner = FakeRunner()

    result = _lifecycle(project, store, runner).scan()

    assert store.added == 1
    assert result.data["extension"] == {"extension_command": "scan"}
    assert result.data["extension_command"] == "scan"
    assert result.data["recommendations"][0]["status"] == [
        "available",
        "installed",
        "locked",
    ]
    assert (
        json.loads((project / ".sanka" / "scan.json").read_text())["fingerprint"]["hash"]
        == result.data["fingerprint"]["hash"]
    )


def test_interactive_scan_decline_keeps_the_project_unpinned(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = FakeStore()

    with pytest.raises(ExtensionError) as raised:
        _lifecycle(
            project,
            store,
            FakeRunner(),
            interactive=True,
            prompt=lambda *_args: None,
        ).scan()

    assert raised.value.code == "SANKA_EXTENSION_REQUIRED"
    assert store.installed is False


def test_plan_selects_target_and_binds_a_generic_core_plan(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = FakeStore(installed=True)
    runner = FakeRunner()
    lifecycle = _lifecycle(project, store, runner)
    lifecycle.scan()

    result = lifecycle.plan(
        target="fastapi",
        configuration={"generation": "minimal", "output": ".sanka/output"},
    )

    plan = json.loads((project / ".sanka" / "plan.json").read_text())
    assert plan["schema_version"] == "sanka-application-plan/v1"
    assert plan["extension"] == store.lock.to_dict()
    assert plan["extension_plan"] == {
        "extension_command": "plan",
        "plan_hash": "sha256:extension-plan",
    }
    assert result.data["plan_hash"] == plan["plan_hash"]
    assert plan["plan_hash"].startswith("sha256:")


def test_plan_rejects_resolved_lock_identity_drift_before_dispatch(tmp_path: Path) -> None:
    project = _project(tmp_path)
    expected = _lock()
    recommendation = SimpleNamespace(
        id=expected.id,
        version=expected.version,
        marketplace="official",
        marketplace_identity=expected.marketplace_identity,
        snapshot_digest=expected.snapshot_digest,
        manifest_digest=expected.manifest_digest,
        commands=("plan",),
        targets=("fastapi",),
        evidence=(),
        status=("available", "installed", "locked"),
        add_command=f"sanka extension add {expected.id}",
    )

    class Store:
        def recommendations(self, _fingerprint: object) -> tuple[Recommendation, ...]:
            return cast(tuple[Recommendation, ...], (recommendation,))

        def resolve_locked(self, _extension_id: str) -> LockEntry:
            return replace(
                expected,
                version="0.2.0",
                snapshot_digest="9" * 40,
                manifest_digest="sha256:" + "8" * 64,
            )

    runner = FakeRunner()

    with pytest.raises(ExtensionError) as raised:
        _lifecycle(project, cast(FakeStore, Store()), runner).plan(target="fastapi")

    assert raised.value.code == "SANKA_EXTENSION_IDENTITY"
    assert runner.calls == []


def test_plan_auto_scan_receives_the_normalized_configuration_and_explicit_environment(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    runner = FakeRunner()

    _lifecycle(project, FakeStore(installed=True), runner).plan(
        target="fastapi",
        configuration={"settings_module": "config.settings"},
        explicit_env_names=("DJANGO_SECRET_KEY",),
    )

    assert [call[1]["command"] for call in runner.calls[:2]] == ["scan", "plan"]
    assert runner.calls[0][1]["configuration"] == {"settings_module": "config.settings"}
    assert runner.explicit_env_names[0] == ("DJANGO_SECRET_KEY",)


def test_scan_persists_only_sorted_explicit_environment_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("FIRST_SECRET", "first-secret-value")
    monkeypatch.setenv("SECOND_SECRET", "second-secret-value")

    _lifecycle(project, FakeStore(installed=True), FakeRunner()).scan(
        explicit_env_names=("SECOND_SECRET", "FIRST_SECRET"),
    )

    serialized = (project / ".sanka" / "scan.json").read_text()
    scan = json.loads(serialized)
    assert scan["explicit_env_names"] == ["FIRST_SECRET", "SECOND_SECRET"]
    assert "first-secret-value" not in serialized
    assert "second-secret-value" not in serialized


@pytest.mark.parametrize(
    ("saved_names", "current_names"),
    [
        (("FIRST_SECRET",), ("SECOND_SECRET",)),
        (("FIRST_SECRET",), ()),
    ],
    ids=("changed-names", "removed-names"),
)
def test_plan_refreshes_scan_when_explicit_environment_names_change(
    tmp_path: Path,
    saved_names: tuple[str, ...],
    current_names: tuple[str, ...],
) -> None:
    project = _project(tmp_path)
    runner = FakeRunner()
    lifecycle = _lifecycle(project, FakeStore(installed=True), runner)
    lifecycle.scan(explicit_env_names=saved_names)

    lifecycle.plan(target="fastapi", explicit_env_names=current_names)

    assert [request["command"] for _lock, request in runner.calls] == [
        "scan",
        "scan",
        "plan",
    ]
    scan = json.loads((project / ".sanka" / "scan.json").read_text())
    assert scan["explicit_env_names"] == sorted(current_names)


def test_plan_refreshes_scan_when_an_explicit_environment_value_may_have_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    runner = FakeRunner()
    lifecycle = _lifecycle(project, FakeStore(installed=True), runner)
    monkeypatch.setenv("SANKA_CONTEXT", "first-value")
    lifecycle.scan(explicit_env_names=("SANKA_CONTEXT",))
    monkeypatch.setenv("SANKA_CONTEXT", "second-value")

    lifecycle.plan(target="fastapi", explicit_env_names=("SANKA_CONTEXT",))

    assert [request["command"] for _lock, request in runner.calls] == [
        "scan",
        "scan",
        "plan",
    ]
    serialized = (project / ".sanka" / "scan.json").read_text()
    assert json.loads(serialized)["explicit_env_names"] == ["SANKA_CONTEXT"]
    assert "first-value" not in serialized
    assert "second-value" not in serialized


def test_plan_refreshes_an_existing_scan_for_changed_configuration(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runner = FakeRunner()
    lifecycle = _lifecycle(project, FakeStore(installed=True), runner)
    lifecycle.scan(configuration={"settings_module": "config.dev"})

    lifecycle.plan(
        target="fastapi",
        configuration={"settings_module": "config.prod"},
    )

    assert [request["command"] for _lock, request in runner.calls] == [
        "scan",
        "scan",
        "plan",
    ]
    assert runner.calls[1][1]["configuration"] == {"settings_module": "config.prod"}
    scan = json.loads((project / ".sanka" / "scan.json").read_text())
    assert scan["configuration"] == {"settings_module": "config.prod"}


def test_plan_refreshes_an_existing_scan_after_a_legitimate_repin(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = FakeStore(installed=True)
    runner = FakeRunner()
    lifecycle = _lifecycle(project, store, runner)
    lifecycle.scan()
    store.lock = replace(
        store.lock,
        version="0.2.0",
        snapshot_digest="9" * 40,
        manifest_digest="sha256:" + "8" * 64,
        artifact_digest="7" * 64,
    )

    lifecycle.plan(target="fastapi")

    assert [(request["command"], lock.version) for lock, request in runner.calls] == [
        ("scan", "0.1.0a1"),
        ("scan", "0.2.0"),
        ("plan", "0.2.0"),
    ]
    scan = json.loads((project / ".sanka" / "scan.json").read_text())
    assert scan["extensions"][0]["extension"] == store.lock.to_dict()


def test_plan_only_repin_refreshes_the_persisted_scan_recommendation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = FakeStore(installed=True)
    store.lock = replace(store.lock, commands=("plan",))
    runner = FakeRunner()
    lifecycle = _lifecycle(project, store, runner)
    lifecycle.scan()
    store.lock = replace(
        store.lock,
        version="0.2.0",
        snapshot_digest="9" * 40,
        manifest_digest="sha256:" + "8" * 64,
        artifact_digest="7" * 64,
    )

    lifecycle.plan(target="fastapi")

    assert [(request["command"], lock.version) for lock, request in runner.calls] == [
        ("plan", "0.2.0")
    ]
    scan = json.loads((project / ".sanka" / "scan.json").read_text())
    assert scan["recommendations"][0]["version"] == "0.2.0"
    assert scan["recommendations"][0]["marketplace_identity"] == ("github.com/sankaHQ/extensions")


@pytest.mark.parametrize(
    ("field", "value", "serialized"),
    [
        ("version", "0.2.0", "0.2.0"),
        ("marketplace", "renamed-marketplace", "renamed-marketplace"),
        ("marketplace_identity", "marketplace-b-new", "marketplace-b-new"),
        ("commands", ("plan", "verify"), ["plan", "verify"]),
        ("targets", ("flask", "starlette"), ["flask", "starlette"]),
        (
            "status",
            ("available", "installed", "locked", "update_available"),
            ["available", "installed", "locked", "update_available"],
        ),
        ("status", ("available", "disabled"), ["available", "disabled"]),
    ],
)
def test_plan_refreshes_the_full_plan_only_recommendation_envelope(
    tmp_path: Path,
    field: str,
    value: object,
    serialized: object,
) -> None:
    project = _project(tmp_path)
    active_lock = replace(
        _lock(),
        id="a/active",
        marketplace_identity="marketplace-a",
        commands=("plan",),
    )
    observed_lock = replace(
        _lock(),
        id="b/observed",
        marketplace_identity="marketplace-b",
        manifest_digest="sha256:" + "b" * 64,
        commands=("plan",),
    )
    active = _recommendation(active_lock, marketplace="a", targets=("fastapi",))
    observed = _recommendation(observed_lock, marketplace="b", targets=("flask",))

    class Store:
        recommendations_value = (observed, active)

        def recommendations(self, _fingerprint: object) -> tuple[Recommendation, ...]:
            return self.recommendations_value

        def resolve_locked(self, extension_id: str) -> LockEntry:
            assert extension_id == active_lock.id
            return active_lock

    store = Store()
    runner = FakeRunner()
    lifecycle = _lifecycle(project, cast(FakeStore, store), runner)
    lifecycle.scan()
    changed = replace(observed, **cast(Any, {field: value}))
    store.recommendations_value = (changed, active)

    lifecycle.plan(target="fastapi")

    scan = json.loads((project / ".sanka" / "scan.json").read_text())
    saved = next(item for item in scan["recommendations"] if item["id"] == changed.id)
    assert saved[field] == serialized
    assert [item["id"] for item in scan["recommendations"]] == ["a/active", "b/observed"]


def test_scan_dispatches_every_scan_capable_lock_under_its_exact_identity(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    locks = {
        "a/one": replace(
            _lock(),
            id="a/one",
            marketplace_identity="marketplace-a",
            manifest_digest="sha256:" + "a" * 64,
            commands=("plan", "scan"),
        ),
        "b/two": replace(
            _lock(),
            id="b/two",
            marketplace_identity="marketplace-b",
            manifest_digest="sha256:" + "b" * 64,
            commands=("plan", "scan"),
        ),
        "c/plan-only": replace(
            _lock(),
            id="c/plan-only",
            marketplace_identity="marketplace-c",
            manifest_digest="sha256:" + "c" * 64,
            commands=("plan",),
        ),
    }
    recommendations = tuple(
        SimpleNamespace(
            id=extension_id,
            version=lock.version,
            marketplace=lock.marketplace_identity,
            marketplace_identity=lock.marketplace_identity,
            snapshot_digest=lock.snapshot_digest,
            manifest_digest=lock.manifest_digest,
            commands=("plan",) if extension_id == "c/plan-only" else ("plan", "scan"),
            targets=("fastapi",),
            evidence=(),
            status=("available", "installed", "locked"),
            add_command=f"sanka extension add {extension_id}",
        )
        for extension_id, lock in locks.items()
    )

    class Store:
        def recommendations(self, _fingerprint: object) -> tuple[Recommendation, ...]:
            return cast(tuple[Recommendation, ...], recommendations)

        def resolve_locked(self, extension_id: str) -> LockEntry:
            return locks[extension_id]

    runner = FakeRunner()
    result = _lifecycle(project, cast(FakeStore, Store()), runner).scan()

    assert [lock.id for lock, _request in runner.calls] == ["a/one", "b/two"]
    assert result.data["extensions"] == [
        {"data": {"extension_command": "scan"}, "extension": locks["a/one"].to_dict()},
        {"data": {"extension_command": "scan"}, "extension": locks["b/two"].to_dict()},
    ]
    assert "extension" not in result.data
    assert "extension_command" not in result.data


def test_interactive_install_selects_the_exact_colliding_marketplace_recommendation(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    class Store:
        installed_marketplace: str | None = None

        def recommendations(self, _fingerprint: object) -> tuple[Recommendation, ...]:
            values = []
            for name, identity in (
                ("one", "marketplace-one"),
                ("two", "marketplace-two"),
            ):
                status = (
                    ("available", "installed", "locked")
                    if identity == self.installed_marketplace
                    else ("available",)
                )
                values.append(
                    SimpleNamespace(
                        id="vendor/demo",
                        version="1.0.0",
                        marketplace=name,
                        marketplace_identity=identity,
                        snapshot_digest=_lock().snapshot_digest,
                        manifest_digest=_lock().manifest_digest,
                        commands=("scan",),
                        targets=("fastapi",),
                        evidence=(),
                        status=status,
                        add_command="sanka extension add vendor/demo",
                    )
                )
            return cast(tuple[Recommendation, ...], tuple(values))

        def add_extension(self, extension_id: str, *, marketplace: str) -> LockEntry:
            assert extension_id == "vendor/demo"
            self.installed_marketplace = marketplace
            return replace(
                _lock(),
                id=extension_id,
                version="1.0.0",
                marketplace_identity=marketplace,
                commands=("scan",),
            )

        def resolve_locked(self, extension_id: str) -> LockEntry:
            assert self.installed_marketplace is not None
            return replace(
                _lock(),
                id=extension_id,
                version="1.0.0",
                marketplace_identity=self.installed_marketplace,
                commands=("scan",),
            )

    store = Store()

    def choose(_label: str, choices: tuple[str, ...] | None = None) -> str:
        assert choices == (
            "decline",
            "vendor/demo (marketplace-one)",
            "vendor/demo (marketplace-two)",
        )
        return choices[2]

    result = _lifecycle(
        project,
        cast(FakeStore, store),
        FakeRunner(),
        interactive=True,
        prompt=choose,
    ).scan()

    assert result.outcome == "success"
    assert store.installed_marketplace == "marketplace-two"


def test_plan_requires_a_target_outside_a_tty(tmp_path: Path) -> None:
    project = _project(tmp_path)
    lifecycle = _lifecycle(project, FakeStore(installed=True), FakeRunner())
    lifecycle.scan()

    with pytest.raises(ExtensionError) as raised:
        lifecycle.plan(target=None)

    assert raised.value.code == "SANKA_EXTENSION_TARGET_REQUIRED"
    assert raised.value.details == {"targets": ["fastapi"]}


def test_interactive_plan_retries_only_requested_structured_inputs(tmp_path: Path) -> None:
    project = _project(tmp_path)
    runner = FakeRunner()
    runner.required_inputs = ["output"]
    answers = iter(["fastapi", "generated"])

    def answer(_label: str, _choices: tuple[str, ...] | None = None) -> str:
        return next(answers)

    lifecycle = _lifecycle(
        project,
        FakeStore(installed=True),
        runner,
        interactive=True,
        prompt=answer,
    )
    lifecycle.scan()

    result = lifecycle.plan(target=None)

    assert result.outcome == "success"
    assert runner.calls[-1][1]["configuration"]["output"] == "generated"


def test_apply_rejects_stale_fingerprint_and_wrong_core_hash(tmp_path: Path) -> None:
    project = _project(tmp_path)
    lifecycle = _lifecycle(project, FakeStore(installed=True), FakeRunner())
    lifecycle.scan()
    planned = lifecycle.plan(target="fastapi")

    with pytest.raises(ExtensionError) as raised:
        lifecycle.apply(reviewed_plan_hash="sha256:wrong")
    assert raised.value.code == "SANKA_EXTENSION_PLAN_HASH_MISMATCH"

    (project / "new.py").write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(ExtensionError) as raised:
        lifecycle.apply(reviewed_plan_hash=str(planned.data["plan_hash"]))
    assert raised.value.code == "SANKA_FINGERPRINT_STALE"


def test_apply_rejects_configuration_not_bound_by_the_core_plan(tmp_path: Path) -> None:
    project = _project(tmp_path)
    lifecycle = _lifecycle(project, FakeStore(installed=True), FakeRunner())
    lifecycle.scan()
    planned = lifecycle.plan(target="fastapi")

    with pytest.raises(ExtensionError) as raised:
        lifecycle.apply(
            reviewed_plan_hash=str(planned.data["plan_hash"]),
            configuration={"output": "unreviewed"},
        )

    assert raised.value.code == "SANKA_EXTENSION_PLAN_HASH_MISMATCH"


@pytest.mark.parametrize("change", ["lock", "artifact"])
def test_later_phases_reject_lock_or_artifact_drift(tmp_path: Path, change: str) -> None:
    project = _project(tmp_path)
    store = FakeStore(installed=True)
    lifecycle = _lifecycle(project, store, FakeRunner())
    lifecycle.scan()
    planned = lifecycle.plan(target="fastapi")
    plan = json.loads((project / ".sanka" / "plan.json").read_text())
    if change == "lock":
        store.lock = replace(store.lock, artifact_digest="9" * 64)
    else:
        Path(plan["artifacts"][0]).write_text("changed", encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        lifecycle.apply(reviewed_plan_hash=str(planned.data["plan_hash"]))

    assert raised.value.code == "SANKA_EXTENSION_IDENTITY"


def test_disable_add_and_retry_restores_scan(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = FakeStore(installed=True, default_available=True)
    runner = FakeRunner()
    lifecycle = _lifecycle(project, store, runner)
    assert lifecycle.scan().outcome == "success"

    store.installed = False
    store.disabled = True
    with pytest.raises(ExtensionError) as raised:
        lifecycle.scan()
    assert raised.value.code == "SANKA_EXTENSION_REQUIRED"

    store.add_extension(store.lock.id)
    assert lifecycle.scan().outcome == "success"


def test_store_recommendations_keep_verified_marketplace_identity(tmp_path: Path) -> None:
    from sanka.runtime.extensions.store import ExtensionStore

    project = _project(tmp_path)
    market = Path(__file__).parent / "fixtures" / "extension_marketplace"
    store = ExtensionStore(project, user_root=tmp_path / "home")
    store.add_marketplace(market, name="fixtures", trust=True)

    recommendations = store.recommendations(fingerprint_repository(project))

    assert [(item.id, item.marketplace, item.commands) for item in recommendations] == [
        (
            "sanka/drf-to-fastapi",
            "fixtures",
            ("apply", "plan", "scan", "test", "verify"),
        )
    ]
    assert recommendations[0].marketplace_identity.startswith("local:")


def test_verify_replay_runs_without_a_reviewed_plan_and_after_source_changes(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    runner = FakeRunner()
    lifecycle = _lifecycle(project, FakeStore(installed=True), runner)

    # No scan and no plan yet: replay only needs the enabled extension.
    replayed = lifecycle.verify(configuration={"scenarios": "scenarios.json", "edge_probes": True})
    assert replayed.outcome == "success"
    request = runner.calls[-1][1]
    assert request["command"] == "verify"
    assert request["configuration"] == {"edge_probes": True, "scenarios": "scenarios.json"}
    assert request["reviewed_plan_hash"] is None

    # With a reviewed plan and a source tree the agent has since edited, apply still
    # refuses while replay keeps working against the live source.
    lifecycle.scan()
    planned = lifecycle.plan(target="fastapi")
    (project / "target_app.py").write_text("app = None\n", encoding="utf-8")
    with pytest.raises(ExtensionError) as raised:
        lifecycle.apply(reviewed_plan_hash=str(planned.data["plan_hash"]))
    assert raised.value.code == "SANKA_FINGERPRINT_STALE"
    again = lifecycle.verify(configuration={"scenarios": "scenarios.json"})
    assert again.outcome == "success"
    assert again.data["extension_command"] == "verify"


def test_verify_replay_requires_an_enabled_extension(tmp_path: Path) -> None:
    project = _project(tmp_path)
    lifecycle = _lifecycle(project, FakeStore(installed=False), FakeRunner())

    with pytest.raises(ExtensionError) as raised:
        lifecycle.verify(configuration={"scenarios": "scenarios.json"})

    assert raised.value.code == "SANKA_EXTENSION_REQUIRED"


def test_verify_without_scenarios_still_requires_the_reviewed_plan(tmp_path: Path) -> None:
    project = _project(tmp_path)
    lifecycle = _lifecycle(project, FakeStore(installed=True), FakeRunner())

    with pytest.raises(ExtensionError) as raised:
        lifecycle.verify()

    assert raised.value.code == "SANKA_EXTENSION_IDENTITY"
