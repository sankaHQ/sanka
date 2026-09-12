# SPDX-License-Identifier: Apache-2.0
"""Canonical extension interfaces over the shared compatibility implementation."""

from collections.abc import Mapping
from typing import Any

from sanka_connector.errors import DataError
from sanka_connector.records import (
    BatchRelationshipStatus as BatchRelationshipStatus,
)
from sanka_connector.records import (
    BatchRelationshipWriteResult as BatchRelationshipWriteResult,
)
from sanka_connector.records import (
    BatchWriteInput as BatchWriteInput,
)
from sanka_connector.records import (
    BatchWriteResult as BatchWriteResult,
)
from sanka_connector.records import (
    BatchWriteStatus as BatchWriteStatus,
)
from sanka_connector.records import (
    ConflictPolicy as ConflictPolicy,
)
from sanka_connector.records import (
    InvalidEmailPolicy as InvalidEmailPolicy,
)
from sanka_connector.records import (
    OwnerProfile as OwnerProfile,
)
from sanka_connector.records import (
    RecordPage as RecordPage,
)
from sanka_connector.records import (
    RelationshipMode as RelationshipMode,
)
from sanka_connector.records import (
    RelationshipStatus as RelationshipStatus,
)
from sanka_connector.records import (
    RelationshipWrite as RelationshipWrite,
)
from sanka_connector.records import (
    RelationshipWriteResult as RelationshipWriteResult,
)
from sanka_connector.records import (
    SourceFilter as SourceFilter,
)
from sanka_connector.records import (
    WriteOptions as WriteOptions,
)
from sanka_connector.records import (
    WriteResult as WriteResult,
)
from sanka_connector.records import (
    WriteStatus as WriteStatus,
)


def require_identity_values(
    properties: Mapping[str, Any], identity_fields: list[str] | None
) -> list[tuple[str, Any]]:
    """Return one complete, non-NULL reviewed identity tuple.

    A destination must never weaken a composite identity to whichever fields
    happen to be present in one untrusted record.  An empty identity remains a
    supported create-only route; once fields are declared, every field is
    required exactly once and must carry a non-NULL value.
    """

    if not identity_fields:
        return []
    fields = [str(field) for field in identity_fields]
    if any(not field.strip() for field in fields):
        raise DataError("identity field names must not be empty")
    if len(set(fields)) != len(fields):
        raise DataError("identity field names must be unique")
    missing = [field for field in fields if field not in properties]
    if missing:
        raise DataError(
            "record is missing required identity field(s): " + ", ".join(sorted(missing))
        )
    null_fields = [field for field in fields if properties[field] is None]
    if null_fields:
        raise DataError(
            "record has NULL required identity field(s): " + ", ".join(sorted(null_fields))
        )
    return [(field, properties[field]) for field in fields]


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
