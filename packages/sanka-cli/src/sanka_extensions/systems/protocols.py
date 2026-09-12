# SPDX-License-Identifier: Apache-2.0
"""Compatibility imports; new extensions use sanka_extensions.data."""

from sanka_extensions.data.protocols import DataReader as SystemReader
from sanka_extensions.data.protocols import DataWriter as SystemWriter
from sanka_extensions.data.protocols import Limits as Limits
from sanka_extensions.data.protocols import (
    SupportsBatchRelationshipWrites as SupportsBatchRelationshipWrites,
)
from sanka_extensions.data.protocols import SupportsBatchWrites as SupportsBatchWrites
from sanka_extensions.data.protocols import SupportsBoundedCounts as SupportsBoundedCounts
from sanka_extensions.data.protocols import SupportsBoundedReads as SupportsBoundedReads
from sanka_extensions.data.protocols import SupportsConfigValidation as SupportsConfigValidation
from sanka_extensions.data.protocols import SupportsHighWaterMark as SupportsHighWaterMark
from sanka_extensions.data.protocols import SupportsIdentityInspection as SupportsIdentityInspection
from sanka_extensions.data.protocols import SupportsLimits as SupportsLimits
from sanka_extensions.data.protocols import SupportsOwnerDirectory as SupportsOwnerDirectory
from sanka_extensions.data.protocols import (
    SupportsPropertyProvisioning as SupportsPropertyProvisioning,
)
from sanka_extensions.data.protocols import SupportsRecordCounts as SupportsRecordCounts
from sanka_extensions.data.protocols import (
    SupportsResourceProvisioning as SupportsResourceProvisioning,
)
from sanka_extensions.data.protocols import SupportsRetryMetrics as SupportsRetryMetrics
from sanka_extensions.data.protocols import SupportsSchemaProvisioning as SupportsSchemaProvisioning
from sanka_extensions.data.protocols import SupportsSnapshotBounds as SupportsSnapshotBounds

__all__ = [
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
    "SystemReader",
    "SystemWriter",
]
