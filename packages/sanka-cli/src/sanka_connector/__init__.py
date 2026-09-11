# SPDX-License-Identifier: Apache-2.0
"""Sanka Extension SDK — Apache-2.0 system access interfaces.

Extensions implement the base protocols (:class:`SystemReader`,
:class:`SystemWriter`) plus any optional capability protocols, and
must not import the AGPL-licensed runtime (``sanka.runtime``); CI enforces
that boundary so an extension is never a derivative work of the runtime.
"""

from sanka_connector.__about__ import __version__
from sanka_connector.credentials import (
    CredentialProvider,
    Credentials,
    SupportsCredentialRefresh,
)
from sanka_connector.errors import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    DataError,
    ErrorCategory,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    SchemaMismatchError,
    SystemAccessError,
    SystemTimeoutError,
    TransientSystemError,
    UnsupportedFeatureError,
    ValidationFailedError,
)
from sanka_connector.protocols import (
    Limits,
    SupportsBatchRelationshipWrites,
    SupportsBatchWrites,
    SupportsBoundedCounts,
    SupportsBoundedReads,
    SupportsConfigValidation,
    SupportsHighWaterMark,
    SupportsIdentityInspection,
    SupportsLimits,
    SupportsOwnerDirectory,
    SupportsPropertyProvisioning,
    SupportsRecordCounts,
    SupportsResourceProvisioning,
    SupportsRetryMetrics,
    SupportsSchemaProvisioning,
    SupportsSnapshotBounds,
    SystemReader,
    SystemWriter,
)
from sanka_connector.provisioning import (
    CustomObjectDefinition,
    CustomObjectProperty,
    PipelineDefinition,
    PipelineStage,
    PropertyDefinition,
    PropertyResult,
    ResourceResult,
)
from sanka_connector.records import (
    BatchRelationshipWriteResult,
    BatchWriteInput,
    BatchWriteResult,
    ConflictPolicy,
    InvalidEmailPolicy,
    OwnerProfile,
    RecordPage,
    RelationshipWrite,
    RelationshipWriteResult,
    SourceFilter,
    WriteOptions,
    WriteResult,
    require_identity_values,
)
from sanka_connector.registration import ENTRY_POINT_GROUP, ExtensionRegistration
from sanka_connector.schema import (
    FieldSchema,
    Inventory,
    ObjectSchema,
    SourceObject,
    SystemIdentity,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "AuthenticationError",
    "BatchRelationshipWriteResult",
    "BatchWriteInput",
    "BatchWriteResult",
    "ConfigurationError",
    "ConflictError",
    "ConflictPolicy",
    "CredentialProvider",
    "Credentials",
    "CustomObjectDefinition",
    "CustomObjectProperty",
    "DataError",
    "ErrorCategory",
    "ExtensionRegistration",
    "FieldSchema",
    "InvalidEmailPolicy",
    "Inventory",
    "Limits",
    "NotFoundError",
    "ObjectSchema",
    "OwnerProfile",
    "PermissionDeniedError",
    "PipelineDefinition",
    "PipelineStage",
    "PropertyDefinition",
    "PropertyResult",
    "RateLimitError",
    "RecordPage",
    "RelationshipWrite",
    "RelationshipWriteResult",
    "ResourceResult",
    "SchemaMismatchError",
    "SourceFilter",
    "SourceObject",
    "SupportsBatchRelationshipWrites",
    "SupportsBatchWrites",
    "SupportsBoundedCounts",
    "SupportsBoundedReads",
    "SupportsConfigValidation",
    "SupportsCredentialRefresh",
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
    "SystemAccessError",
    "SystemIdentity",
    "SystemReader",
    "SystemTimeoutError",
    "SystemWriter",
    "TransientSystemError",
    "UnsupportedFeatureError",
    "ValidationFailedError",
    "WriteOptions",
    "WriteResult",
    "__version__",
    "require_identity_values",
]

# Published compatibility names; both spellings identify the same classes.
SourceConnector = SystemReader
DestinationConnector = SystemWriter
ConnectorRegistration = ExtensionRegistration
ConnectorError = SystemAccessError
ProviderIdentity = SystemIdentity
ProviderTimeoutError = SystemTimeoutError
TransientProviderError = TransientSystemError

__all__ += [
    "ConnectorError",
    "ConnectorRegistration",
    "DestinationConnector",
    "ProviderIdentity",
    "ProviderTimeoutError",
    "SourceConnector",
    "TransientProviderError",
]
