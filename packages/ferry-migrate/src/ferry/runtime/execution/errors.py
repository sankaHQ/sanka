# SPDX-License-Identifier: AGPL-3.0-only
"""Structured execution errors with stable machine-readable codes.

Same pattern as :mod:`ferry.runtime.mapping.errors`: the production runtime
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

    ``code`` is a stable machine-readable identifier (``FERRY_*``) preserved
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
        "FERRY_EXECUTION_JOB_SUPERSEDED",
        "FERRY_EXECUTION_ATTEMPT_SUPERSEDED",
        "FERRY_ROUTE_MANIFEST_MISSING",
        "FERRY_ROUTE_MANIFEST_INVALID",
        "FERRY_ROUTE_MANIFEST_CHANGED",
        "FERRY_ROUTE_CHECKPOINT_MISMATCH",
        "FERRY_EXECUTION_HIGH_WATER_MARK_INVALID",
        "FERRY_SOURCE_CHECKPOINT_MISSING",
        "FERRY_SOURCE_HIGH_WATER_MARK_UNSUPPORTED",
        "FERRY_EXECUTION_ROUTE_INVALID",
        "FERRY_EXECUTION_ROUTE_CHANGED",
        "FERRY_REQUIRED_REFERENCE_SOURCE_FIELD_EMPTY",
        "FERRY_REFERENCE_SOURCE_ID_AMBIGUOUS",
        "FERRY_REFERENCE_TARGET_PENDING",
        "FERRY_EMPTY_DESTINATION_RECORD",
        "FERRY_OWNER_MAPPING_UNSUPPORTED",
        "FERRY_RECORD_RESULT_BULK_SAVE_INCOMPLETE",
    }
)
