# SPDX-License-Identifier: Apache-2.0
"""Canonical extension interfaces over the shared compatibility implementation."""

from sanka_connector.protocols import (
    DestinationConnector as DataWriter,
)
from sanka_connector.protocols import (
    Limits as Limits,
)
from sanka_connector.protocols import (
    SourceConnector as DataReader,
)
from sanka_connector.protocols import (
    SupportsBatchRelationshipWrites as SupportsBatchRelationshipWrites,
)
from sanka_connector.protocols import (
    SupportsBatchWrites as SupportsBatchWrites,
)
from sanka_connector.protocols import (
    SupportsBoundedCounts as SupportsBoundedCounts,
)
from sanka_connector.protocols import (
    SupportsBoundedReads as SupportsBoundedReads,
)
from sanka_connector.protocols import (
    SupportsConfigValidation as SupportsConfigValidation,
)
from sanka_connector.protocols import (
    SupportsHighWaterMark as SupportsHighWaterMark,
)
from sanka_connector.protocols import (
    SupportsIdentityInspection as SupportsIdentityInspection,
)
from sanka_connector.protocols import (
    SupportsLimits as SupportsLimits,
)
from sanka_connector.protocols import (
    SupportsOwnerDirectory as SupportsOwnerDirectory,
)
from sanka_connector.protocols import (
    SupportsPropertyProvisioning as SupportsPropertyProvisioning,
)
from sanka_connector.protocols import (
    SupportsRecordCounts as SupportsRecordCounts,
)
from sanka_connector.protocols import (
    SupportsResourceProvisioning as SupportsResourceProvisioning,
)
from sanka_connector.protocols import (
    SupportsRetryMetrics as SupportsRetryMetrics,
)
from sanka_connector.protocols import (
    SupportsSchemaProvisioning as SupportsSchemaProvisioning,
)
from sanka_connector.protocols import (
    SupportsSnapshotBounds as SupportsSnapshotBounds,
)

__all__ = [
    "DataReader",
    "DataWriter",
    "Limits",
    "SupportsBatchRelationshipWrites",
    "SupportsBatchWrites",
    "SupportsBoundedCounts",
    "SupportsBoundedReads",
    "SupportsConfigValidation",
    "SupportsHighWaterMark",
    "SupportsIdentityInspection",
    "SupportsLimits",
    "SupportsOwnerDirectory",
    "SupportsPropertyProvisioning",
    "SupportsRecordCounts",
    "SupportsResourceProvisioning",
    "SupportsRetryMetrics",
    "SupportsSchemaProvisioning",
    "SupportsSnapshotBounds",
]
