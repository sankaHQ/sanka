# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sqlite3
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest

import sanka
import sanka.runtime
import sanka_cli
from sanka import Connection, EndpointSpec, PlanMismatchError, RunStatus, Sanka
from sanka.runtime.extensions.store import ExtensionStore
from sanka.runtime.registry import ExtensionRegistry

pytestmark = pytest.mark.usefixtures("trusted_connector_discovery")


def _write_content(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text("---\ntitle: A\n---\nAlpha body\n", encoding="utf-8")
    (root / "b.md").write_text("---\ntitle: B\n---\nBeta body\n", encoding="utf-8")


def _status(migration: sanka.Migration) -> RunStatus:
    return migration.status


def test_public_facade_version_matches_unified_distribution() -> None:
    assert (
        sanka.__version__
        == sanka.runtime.__version__
        == sanka_cli.__version__
        == distribution_version("sanka-cli")
        == "0.2.16"
    )


def test_public_facade_exports_runtime_types() -> None:
    assert sanka.MigrationPlan.__module__ == "sanka.runtime.planner"
    assert sanka.MigrationSpec.__module__ == "sanka.runtime.spec"
    assert sanka.ExecutionError.__module__ == "sanka.runtime.engine"


def test_sanka_connect_selects_an_installed_provider(tmp_path: Path) -> None:
    with Sanka(state=tmp_path / "state.db") as client:
        markdown = client.connect("markdown", "./content")
        postgres = client.connect("postgresql", "postgresql://localhost/example")

    assert markdown == Connection(
        provider="markdown",
        roles=("source",),
        connection="./content",
        options={},
    )
    assert postgres.provider == "postgres"
    assert postgres.roles == ("source", "destination")
    assert postgres.endpoint() == EndpointSpec(
        type="postgres",
        connection="postgresql://localhost/example",
    )


def test_sanka_connect_rejects_an_unknown_provider(tmp_path: Path) -> None:
    with (
        Sanka(state=tmp_path / "state.db") as client,
        pytest.raises(sanka.UnknownEndpointError, match="not-a-provider"),
    ):
        client.connect("not-a-provider")


def test_sanka_connect_rejects_hosted_systems_without_cloud_dispatch(tmp_path: Path) -> None:
    with (
        Sanka(state=tmp_path / "state.db") as client,
        pytest.raises(sanka.UnknownEndpointError, match="hosted Data Migration API"),
    ):
        client.connect("hubspot")


async def test_sanka_facade_runs_the_hash_bound_lifecycle(tmp_path: Path) -> None:
    content = tmp_path / "content"
    destination = tmp_path / "out.db"
    _write_content(content)

    with Sanka(state=tmp_path / "state.db", batch_size=1) as client:
        migration = client.migrate(content, f"sqlite://{destination}")
        assert _status(migration) == RunStatus.CREATED
        assert not destination.exists()

        plan = await migration.plan()
        assert migration.plan_hash == plan.plan_hash
        assert _status(migration) == RunStatus.PLANNED
        assert not destination.exists()

        validation = await migration.validate()
        assert validation["rejects"] == []
        assert not destination.exists()

        with pytest.raises(PlanMismatchError):
            await migration.apply(plan_hash="sha256:not-the-reviewed-plan")
        assert not destination.exists()

        await migration.apply(plan_hash=plan.plan_hash)
        report = await migration.verify()

        assert report.ok
        assert _status(migration) == RunStatus.VERIFIED
        with sqlite3.connect(destination) as connection:
            count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 2


def test_sanka_facade_reuses_runs_and_accepts_explicit_endpoints(tmp_path: Path) -> None:
    content = tmp_path / "content"
    _write_content(content)

    with Sanka(state=tmp_path / "state.db") as client:
        source = EndpointSpec(type="markdown", connection=str(content))
        target = client.connect("sqlite", tmp_path / "out.db")

        first = client.migrate(source, target)
        second = client.migrate(source, target)
        fresh = client.migrate(source, target, reuse=False)

        assert first.run_id == second.run_id
        assert fresh.run_id != first.run_id


def test_closed_sanka_facade_rejects_new_migrations(tmp_path: Path) -> None:
    client = Sanka(state=tmp_path / "state.db")
    client.close()

    with pytest.raises(RuntimeError, match="closed"):
        client.migrate("postgres://source", "postgres://target")


def test_sanka_facade_uses_public_default_state_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with Sanka():
        pass

    assert (tmp_path / ".sanka" / "migrate" / "state.db").is_file()


def test_sanka_context_closes_owned_connector_store_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Host:
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class Store(ExtensionStore):
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    host = Host()
    store = Store(tmp_path / "project", user_root=tmp_path / "user")
    store._connector_clients["artifact"] = host  # type: ignore[assignment]
    registry = ExtensionRegistry({}, owner=store)
    monkeypatch.setattr(
        ExtensionRegistry,
        "discover",
        classmethod(lambda _cls: registry),
    )

    with Sanka(state=tmp_path / "state.db") as client:
        pass

    assert store.close_calls == 1
    assert host.close_calls == 1
    assert store._connector_clients == {}
    client.close()
    assert store.close_calls == 1
    assert host.close_calls == 1


def test_configured_systems_keep_independent_endpoints_and_options(tmp_path: Path) -> None:
    options = {"schema": "public"}
    with Sanka(state=tmp_path / "state.db") as client:
        production = client.configure_system("postgres", "production-db", options=options)
        staging = client.configure_system("postgres", "staging-db", options={"schema": "test"})
        legacy = client.connect(provider="postgres", connection="production-db", options=options)
    options["schema"] = "changed-after-configuration"
    assert isinstance(production, sanka.DataEndpoint)
    assert production == legacy
    assert production.endpoint_reference == "production-db"
    assert staging.endpoint_reference == "staging-db"
    assert production.endpoint().options == {"schema": "public"}
    assert staging.endpoint().options == {"schema": "test"}


def test_data_endpoint_preserves_both_earlier_facades(tmp_path: Path) -> None:
    with Sanka(state=tmp_path / "state.db") as client:
        endpoint = client.configure_endpoint("postgres", "production-db")
        system = client.configure_system("postgres", "production-db")
        connection = client.connect("postgres", "production-db")
    assert endpoint == system == connection
    assert sanka.DataEndpoint is sanka.SystemConfig is sanka.Connection
    assert endpoint.endpoint_type == endpoint.system_type == endpoint.provider == "postgres"
    assert endpoint.endpoint().connection == "production-db"


@pytest.mark.parametrize("legacy", ["system_type", "provider"])
def test_data_endpoint_rejects_conflicting_type_aliases(legacy: str) -> None:
    with pytest.raises(ValueError, match="disagree"):
        if legacy == "system_type":
            sanka.DataEndpoint(endpoint_type="postgres", roles=("source",), system_type="sqlite")
        else:
            sanka.DataEndpoint(endpoint_type="postgres", roles=("source",), provider="sqlite")


def test_compatibility_system_keywords_cannot_override_a_different_endpoint() -> None:
    with pytest.raises(ValueError, match="disagree"):
        sanka.DataEndpoint(
            system_type="postgres",
            roles=("source",),
            endpoint_reference="production-db",
            connection="staging-db",
        )
