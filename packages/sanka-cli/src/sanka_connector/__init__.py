# SPDX-License-Identifier: Apache-2.0
"""Sanka Extension SDK — Apache-2.0 data access interfaces.

Extensions implement the base protocols (:class:`DataReader`,
:class:`DataWriter`) plus any optional capability protocols, and
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
    DataAccessError,
    DataError,
    DataTimeoutError,
    ErrorCategory,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    SchemaMismatchError,
    TransientDataError,
    UnsupportedFeatureError,
    ValidationFailedError,
)
from sanka_connector.protocols import (
    DataReader,
    DataWriter,
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
    DataIdentity,
    FieldSchema,
    Inventory,
    ObjectSchema,
    SourceObject,
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
    "DataAccessError",
    "DataError",
    "DataIdentity",
    "DataReader",
    "DataTimeoutError",
    "DataWriter",
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
    "TransientDataError",
    "UnsupportedFeatureError",
    "ValidationFailedError",
    "WriteOptions",
    "WriteResult",
    "__version__",
    "require_identity_values",
]

# Published compatibility names; both spellings identify the same classes.
SourceConnector = DataReader
DestinationConnector = DataWriter
ConnectorRegistration = ExtensionRegistration
ConnectorError = DataAccessError
ProviderIdentity = DataIdentity
ProviderTimeoutError = DataTimeoutError
TransientProviderError = TransientDataError

__all__ += [
    "ConnectorError",
    "ConnectorRegistration",
    "DestinationConnector",
    "ProviderIdentity",
    "ProviderTimeoutError",
    "SourceConnector",
    "TransientProviderError",
]

# Compatibility names from the earlier systems facade.
SystemReader = DataReader
SystemWriter = DataWriter
SystemAccessError = DataAccessError
SystemIdentity = DataIdentity
SystemTimeoutError = DataTimeoutError
TransientSystemError = TransientDataError

__all__ += [
    "SystemAccessError",
    "SystemIdentity",
    "SystemReader",
    "SystemTimeoutError",
    "SystemWriter",
    "TransientSystemError",
]
