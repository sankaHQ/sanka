# SPDX-License-Identifier: AGPL-3.0-only
"""Small public facade over the stable migration runtime."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

from ferry.runtime.engine import InspectionResult, MigrationEngine, VerifyReport
from ferry.runtime.execution import DEFAULT_VALIDATION_SAMPLE_SIZE
from ferry.runtime.planner import MigrationPlan
from ferry.runtime.registry import ConnectorRegistry
from ferry.runtime.spec import EndpointSpec, MigrationSpec, SpecError
from ferry.runtime.state import RunStatus, SqliteStateStore

EndpointInput = str | Path | EndpointSpec


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
    """Entry point for local Sanka Migrate lifecycles.

    ``Sanka`` owns a local SQLite state store. Use it as a context manager for
    deterministic cleanup in long-running processes; short scripts may rely on
    process teardown.
    """

    def __init__(
        self,
        *,
        state: str | Path = ".ferry/state.db",
        batch_size: int = 100,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._store = SqliteStateStore(state)
        self._engine = MigrationEngine(
            store=self._store,
            registry=ConnectorRegistry.discover(),
            batch_size=batch_size,
            env=None if env is None else dict(env),
        )
        self._closed = False

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
            self._store.close()
            self._closed = True

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
        f"cannot infer the {role} connector from {raw!r}; pass an EndpointSpec, "
        "a URL-style endpoint, or a supported local path"
    )
