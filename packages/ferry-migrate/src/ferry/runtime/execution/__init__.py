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
    "WriteStatus",
]
