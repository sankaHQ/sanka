# SPDX-License-Identifier: AGPL-3.0-only
"""Small public facade over the stable migration runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from sanka.runtime.engine import InspectionResult, MigrationEngine, VerifyReport
from sanka.runtime.execution import DEFAULT_VALIDATION_SAMPLE_SIZE
from sanka.runtime.planner import MigrationPlan
from sanka.runtime.registry import DataExtensionRegistry
from sanka.runtime.spec import EndpointSpec, MigrationSpec, SpecError
from sanka.runtime.state import RunStatus, SqliteStateStore
from sanka_data import CredentialProvider


@dataclass(frozen=True, slots=True, kw_only=True, init=False)
class SystemConfig:
    """A named system endpoint and independent, non-secret configuration.

    Creating this descriptor never authenticates a system. Secret values stay
    in environment variables or a managed credential store.
    """

    system_type: str
    roles: tuple[str, ...]
    endpoint_reference: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        *,
        system_type: str | None = None,
        roles: tuple[str, ...],
        endpoint_reference: str | None = None,
        options: Mapping[str, Any] | None = None,
        provider: str | None = None,
        connection: str | None = None,
    ) -> None:
        # Old Connection keyword arguments remain accepted without silently
        # choosing one endpoint when a caller supplies conflicting identities.
        if system_type is not None and provider is not None and system_type != provider:
            raise ValueError("system_type and compatibility provider disagree")
        if (
            endpoint_reference is not None
            and connection is not None
            and endpoint_reference != connection
        ):
            raise ValueError("endpoint_reference and compatibility connection disagree")
        selected_type = system_type if system_type is not None else provider
        if selected_type is None or not selected_type.strip():
            raise ValueError("system_type is required")
        object.__setattr__(self, "system_type", selected_type)
        object.__setattr__(self, "roles", roles)
        object.__setattr__(
            self,
            "endpoint_reference",
            endpoint_reference if endpoint_reference is not None else connection,
        )
        object.__setattr__(self, "options", dict(options or {}))

    @property
    def provider(self) -> str:
        """Compatibility spelling for system_type."""
        return self.system_type

    @property
    def connection(self) -> str | None:
        """Compatibility spelling for endpoint_reference."""
        return self.endpoint_reference

    def endpoint(self) -> EndpointSpec:
        return EndpointSpec(
            type=self.system_type,
            connection=self.endpoint_reference,
            options=dict(self.options),
        )


EndpointInput = str | Path | EndpointSpec | SystemConfig


class Migration:
    """One resumable, plan-bound migration lifecycle."""

    def __init__(self, *, engine: MigrationEngine, run_id: str) -> None:
        self._engine = engine
        self.run_id = run_id

    @property
    def status(self) -> RunStatus:
        """Return the latest persisted lifecycle status."""
        return self._engine.store.get_run(self.run_id).status

    @property
    def plan_hash(self) -> str | None:
        """Return the persisted plan hash, if this migration has been planned."""
        return self._engine.store.get_run(self.run_id).plan_hash

    async def inspect(self) -> InspectionResult:
        return await self._engine.inspect(self.run_id)

    async def plan(self) -> MigrationPlan:
        """Inspect both endpoints and persist a reviewable, write-free plan."""
        return await self._engine.plan(self.run_id)

    async def validate(
        self,
        *,
        sample_size: int = DEFAULT_VALIDATION_SAMPLE_SIZE,
        full: bool = False,
    ) -> dict[str, Any]:
        """Validate live source records without resolving a destination writer."""
        return await self._engine.validate(
            self.run_id,
            sample_size=sample_size,
            full=full,
        )

    async def apply(self, *, plan_hash: str) -> None:
        """Execute only the exact plan hash the caller reviewed."""
        await self._engine.apply(self.run_id, plan_hash=plan_hash)

    async def verify(self) -> VerifyReport:
        return await self._engine.verify(self.run_id)


class Sanka:
    """Entry point for local Sanka lifecycles.

    ``Sanka`` owns a local SQLite state store. Use it as a context manager for
    deterministic cleanup in long-running processes; short scripts may rely on
    process teardown.
    """

    def __init__(
        self,
        *,
        state: str | Path = ".sanka/migrate/state.db",
        batch_size: int = 100,
        env: Mapping[str, str] | None = None,
        credential_provider: CredentialProvider | None = None,
    ) -> None:
        self._store = SqliteStateStore(state)
        self._registry = DataExtensionRegistry.discover()
        self._engine = MigrationEngine(
            store=self._store,
            registry=self._registry,
            credential_provider=credential_provider,
            batch_size=batch_size,
            env=None if env is None else dict(env),
        )
        self._closed = False

    def configure_system(
        self,
        system_type: str,
        endpoint_reference: str | Path | None = None,
        *,
        options: Mapping[str, Any] | None = None,
    ) -> SystemConfig:
        """Configure a system supported by an installed data extension.

        The endpoint reference is a path, URL, or named system reference.
        Authentication and reachability are checked by the migration lifecycle.
        """
        self._ensure_open()
        normalized = system_type.strip().lower()
        if normalized == "postgresql":
            normalized = "postgres"
        roles = self._registry.roles(normalized)
        return SystemConfig(
            system_type=normalized,
            roles=roles,
            endpoint_reference=None if endpoint_reference is None else str(endpoint_reference),
            options=options,
        )

    def connect(
        self,
        provider: str,
        connection: str | Path | None = None,
        *,
        options: Mapping[str, Any] | None = None,
    ) -> SystemConfig:
        """Compatibility method for configure_system; does not authenticate."""
        return self.configure_system(provider, connection, options=options)

    def migrate(
        self,
        source: EndpointInput,
        target: EndpointInput,
        *,
        name: str | None = None,
        strategy: Mapping[str, Any] | None = None,
        verify: Mapping[str, Any] | None = None,
        reuse: bool = True,
    ) -> Migration:
        """Create or resume a migration without writing to the destination.

        Strings accept the same path and URL shorthand as the CLI. For more
        control, pass :class:`EndpointSpec` objects.
        """
        self._ensure_open()
        spec = MigrationSpec(
            source=_endpoint(source, role="source"),
            target=_endpoint(target, role="target"),
            strategy=dict(strategy or {}),
            verify=dict(verify or {}),
            name=name,
        )
        run_id = self._engine.create(spec, name=name, reuse=reuse)
        return Migration(engine=self._engine, run_id=run_id)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._registry.close()
            finally:
                self._store.close()

    def __enter__(self) -> Sanka:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("this Sanka client is closed")


def _endpoint(value: EndpointInput, *, role: str) -> EndpointSpec:
    if isinstance(value, SystemConfig):
        return value.endpoint()
    if isinstance(value, EndpointSpec):
        return value

    raw = str(value)
    if "://" in raw:
        scheme, _, connection = raw.partition("://")
        connector = scheme.lower()
        if connector == "postgresql":
            connector = "postgres"
        if connector == "sqlite":
            return EndpointSpec(type=connector, connection=connection)
        return EndpointSpec(type=connector, connection=raw)

    path = Path(raw)
    if role == "source" and path.is_dir():
        return EndpointSpec(type="markdown", connection=str(path))
    if role == "source" and path.suffix.lower() == ".csv":
        return EndpointSpec(type="csv", connection=str(path))
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return EndpointSpec(type="sqlite", connection=str(path))

    raise SpecError(
        f"cannot infer the {role} system type from {raw!r}; pass an EndpointSpec, "
        "a URL-style endpoint, or a supported local path"
    )


# Compatibility import for clients using the original descriptor name.
Connection = SystemConfig
