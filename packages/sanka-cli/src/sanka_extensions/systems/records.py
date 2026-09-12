# SPDX-License-Identifier: Apache-2.0
"""Compatibility imports; new extensions use sanka_extensions.data."""

from sanka_extensions.data.records import BatchRelationshipStatus as BatchRelationshipStatus
from sanka_extensions.data.records import (
    BatchRelationshipWriteResult as BatchRelationshipWriteResult,
)
from sanka_extensions.data.records import BatchWriteInput as BatchWriteInput
from sanka_extensions.data.records import BatchWriteResult as BatchWriteResult
from sanka_extensions.data.records import BatchWriteStatus as BatchWriteStatus
from sanka_extensions.data.records import ConflictPolicy as ConflictPolicy
from sanka_extensions.data.records import InvalidEmailPolicy as InvalidEmailPolicy
from sanka_extensions.data.records import OwnerProfile as OwnerProfile
from sanka_extensions.data.records import RecordPage as RecordPage
from sanka_extensions.data.records import RelationshipMode as RelationshipMode
from sanka_extensions.data.records import RelationshipStatus as RelationshipStatus
from sanka_extensions.data.records import RelationshipWrite as RelationshipWrite
from sanka_extensions.data.records import RelationshipWriteResult as RelationshipWriteResult
from sanka_extensions.data.records import SourceFilter as SourceFilter
from sanka_extensions.data.records import WriteOptions as WriteOptions
from sanka_extensions.data.records import WriteResult as WriteResult
from sanka_extensions.data.records import WriteStatus as WriteStatus
from sanka_extensions.data.records import require_identity_values as require_identity_values

__all__ = [
    "BatchRelationshipStatus",
    "BatchRelationshipWriteResult",
    "BatchWriteInput",
    "BatchWriteResult",
    "BatchWriteStatus",
    "ConflictPolicy",
    "InvalidEmailPolicy",
    "OwnerProfile",
    "RecordPage",
    "RelationshipMode",
    "RelationshipStatus",
    "RelationshipWrite",
    "RelationshipWriteResult",
    "SourceFilter",
    "WriteOptions",
    "WriteResult",
    "WriteStatus",
    "require_identity_values",
]
