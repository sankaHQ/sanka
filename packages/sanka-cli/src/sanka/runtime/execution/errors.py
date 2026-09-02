# SPDX-License-Identifier: AGPL-3.0-only
"""Structured execution errors with stable machine-readable codes.

Same pattern as :mod:`sanka.runtime.mapping.errors`: the production runtime
raised HTTP-shaped application errors; the open runtime keeps the exact
error ``code`` values verbatim (they are production contracts that appear in
run reports and operator tooling) while dropping the HTTP envelope. Hosts
that need an HTTP surface map codes back to statuses in their own bridge.

:data:`EXECUTION_FAULT_CODES` is the catalog of execution-invariant codes —
the execution-mechanics subset of the production taxonomy. Control-plane
codes (confirmation, entitlement, scan orchestration, credential vetting)
stay host-side and are deliberately absent.
"""

from __future__ import annotations

from typing import Any


class ExecutionFault(RuntimeError):
    """An execution invariant failed; carries the production error code.

    ``code`` is a stable machine-readable identifier (``SANKA_MIGRATE_*``) preserved
    verbatim from the production runtime. ``details`` carries the error's
    structured payload using the same key spellings the persisted run-report
    format uses (camelCase); do not rename them.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details: dict[str, Any] = dict(details) if details else {}


EXECUTION_FAULT_CODES: frozenset[str] = frozenset(
    {
        "SANKA_MIGRATE_EXECUTION_JOB_SUPERSEDED",
        "SANKA_MIGRATE_EXECUTION_ATTEMPT_SUPERSEDED",
        "SANKA_MIGRATE_ROUTE_MANIFEST_MISSING",
        "SANKA_MIGRATE_ROUTE_MANIFEST_INVALID",
        "SANKA_MIGRATE_ROUTE_MANIFEST_CHANGED",
        "SANKA_MIGRATE_ROUTE_CHECKPOINT_MISMATCH",
        "SANKA_MIGRATE_EXECUTION_HIGH_WATER_MARK_INVALID",
        "SANKA_MIGRATE_SOURCE_CHECKPOINT_MISSING",
        "SANKA_MIGRATE_SOURCE_HIGH_WATER_MARK_UNSUPPORTED",
        "SANKA_MIGRATE_EXECUTION_ROUTE_INVALID",
        "SANKA_MIGRATE_EXECUTION_ROUTE_CHANGED",
        "SANKA_MIGRATE_REQUIRED_REFERENCE_SOURCE_FIELD_EMPTY",
        "SANKA_MIGRATE_REFERENCE_SOURCE_ID_AMBIGUOUS",
        "SANKA_MIGRATE_REFERENCE_TARGET_PENDING",
        "SANKA_MIGRATE_EMPTY_DESTINATION_RECORD",
        "SANKA_MIGRATE_OWNER_MAPPING_UNSUPPORTED",
        "SANKA_MIGRATE_RECORD_RESULT_BULK_SAVE_INCOMPLETE",
    }
)
