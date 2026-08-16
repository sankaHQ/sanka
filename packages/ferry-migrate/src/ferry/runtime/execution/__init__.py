# SPDX-License-Identifier: AGPL-3.0-only
"""Execution seam: the working-state model and protocol family for transfer.

Faithful port of the production execution mechanics' contracts. The package
is a sibling of the lifecycle :class:`ferry.runtime.state.StateStore` (which
keeps its synchronous document store role): here live the async, scope-free
protocols one transfer execution runs against — durable per-record results
(:class:`ExecutionLedger`), the fenced journal (:class:`ExecutionJournal`),
the attempt claim (:class:`AttemptFence`), the observer side-channel — plus
the working-state dataclasses and the execution error taxonomy with its
production ``FERRY_*`` codes preserved verbatim.

Hosts bundle their adapters into an :class:`ExecutionHost`, closing each one
over their pinned execution scope; upstream code never sees a workspace,
run, program, or channel id.
"""

from ferry.runtime.execution.errors import EXECUTION_FAULT_CODES, ExecutionFault
from ferry.runtime.execution.local import SqliteExecutionState
from ferry.runtime.execution.model import (
    AttemptIdentity,
    BatchPage,
    ExecutionRoute,
    ExecutionScope,
    ExecutionSnapshot,
    ExecutionStatus,
    JournalEntry,
    PairFailedIds,
    PairStatusTotal,
    RecordWriteOutcome,
    WriteStatus,
)
from ferry.runtime.execution.report_codec import (
    HEARTBEAT_FRESHNESS,
    canonical_route_manifest,
    dump_journal,
    execution_heartbeat_is_recent,
    execution_is_cancelled,
    execution_route_high_water_marks,
    execution_route_manifest,
    filter_mapping_groups,
    load_journal,
    parse_datetime,
    prepare_route_state,
    report_checkpoints,
    report_progress_marker,
    report_route_counts,
    report_route_failed_record_ids,
    report_route_pending_record_ids,
    require_matching_route_manifest,
    route_keys_by_pair,
    selected_route_keys,
)
from ferry.runtime.execution.scope import (
    execution_routes,
    freeze_route_high_water_marks,
    freeze_scope,
    frozen_route_totals,
)
from ferry.runtime.execution.state import (
    NULL_OBSERVER,
    AttemptFence,
    ClaimOutcome,
    ExecutionHost,
    ExecutionJournal,
    ExecutionLedger,
    ExecutionObserver,
    SaveOutcome,
)

__all__ = [
    "EXECUTION_FAULT_CODES",
    "HEARTBEAT_FRESHNESS",
    "NULL_OBSERVER",
    "AttemptFence",
    "AttemptIdentity",
    "BatchPage",
    "ClaimOutcome",
    "ExecutionFault",
    "ExecutionHost",
    "ExecutionJournal",
    "ExecutionLedger",
    "ExecutionObserver",
    "ExecutionRoute",
    "ExecutionScope",
    "ExecutionSnapshot",
    "ExecutionStatus",
    "JournalEntry",
    "PairFailedIds",
    "PairStatusTotal",
    "RecordWriteOutcome",
    "SaveOutcome",
    "SqliteExecutionState",
    "WriteStatus",
    "canonical_route_manifest",
    "dump_journal",
    "execution_heartbeat_is_recent",
    "execution_is_cancelled",
    "execution_route_high_water_marks",
    "execution_route_manifest",
    "execution_routes",
    "filter_mapping_groups",
    "freeze_route_high_water_marks",
    "freeze_scope",
    "frozen_route_totals",
    "load_journal",
    "parse_datetime",
    "prepare_route_state",
    "report_checkpoints",
    "report_progress_marker",
    "report_route_counts",
    "report_route_failed_record_ids",
    "report_route_pending_record_ids",
    "require_matching_route_manifest",
    "route_keys_by_pair",
    "selected_route_keys",
]
