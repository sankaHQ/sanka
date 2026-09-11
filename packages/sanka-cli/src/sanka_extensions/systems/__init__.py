# SPDX-License-Identifier: Apache-2.0
"""Canonical extension interfaces over the shared compatibility implementation."""

from sanka_connector import (
    ENTRY_POINT_GROUP as ENTRY_POINT_GROUP,
)
from sanka_connector import (
    AuthenticationError as AuthenticationError,
)
from sanka_connector import (
    BatchRelationshipWriteResult as BatchRelationshipWriteResult,
)
from sanka_connector import (
    BatchWriteInput as BatchWriteInput,
)
from sanka_connector import (
    BatchWriteResult as BatchWriteResult,
)
from sanka_connector import (
    ConfigurationError as ConfigurationError,
)
from sanka_connector import (
    ConflictError as ConflictError,
)
from sanka_connector import (
    ConflictPolicy as ConflictPolicy,
)
from sanka_connector import (
    ConnectorError as SystemAccessError,
)
from sanka_connector import (
    ConnectorRegistration as ExtensionRegistration,
)
from sanka_connector import (
    CredentialProvider as CredentialProvider,
)
from sanka_connector import (
    Credentials as Credentials,
)
from sanka_connector import (
    CustomObjectDefinition as CustomObjectDefinition,
)
from sanka_connector import (
    CustomObjectProperty as CustomObjectProperty,
)
from sanka_connector import (
    DataError as DataError,
)
from sanka_connector import (
    DestinationConnector as SystemWriter,
)
from sanka_connector import (
    ErrorCategory as ErrorCategory,
)
from sanka_connector import (
    FieldSchema as FieldSchema,
)
from sanka_connector import (
    InvalidEmailPolicy as InvalidEmailPolicy,
)
from sanka_connector import (
    Inventory as Inventory,
)
from sanka_connector import (
    Limits as Limits,
)
from sanka_connector import (
    NotFoundError as NotFoundError,
)
from sanka_connector import (
    ObjectSchema as ObjectSchema,
)
from sanka_connector import (
    OwnerProfile as OwnerProfile,
)
from sanka_connector import (
    PermissionDeniedError as PermissionDeniedError,
)
from sanka_connector import (
    PipelineDefinition as PipelineDefinition,
)
from sanka_connector import (
    PipelineStage as PipelineStage,
)
from sanka_connector import (
    PropertyDefinition as PropertyDefinition,
)
from sanka_connector import (
    PropertyResult as PropertyResult,
)
from sanka_connector import (
    ProviderIdentity as SystemIdentity,
)
from sanka_connector import (
    ProviderTimeoutError as SystemTimeoutError,
)
from sanka_connector import (
    RateLimitError as RateLimitError,
)
from sanka_connector import (
    RecordPage as RecordPage,
)
from sanka_connector import (
    RelationshipWrite as RelationshipWrite,
)
from sanka_connector import (
    RelationshipWriteResult as RelationshipWriteResult,
)
from sanka_connector import (
    ResourceResult as ResourceResult,
)
from sanka_connector import (
    SchemaMismatchError as SchemaMismatchError,
)
from sanka_connector import (
    SourceConnector as SystemReader,
)
from sanka_connector import (
    SourceFilter as SourceFilter,
)
from sanka_connector import (
    SourceObject as SourceObject,
)
from sanka_connector import (
    SupportsBatchRelationshipWrites as SupportsBatchRelationshipWrites,
)
from sanka_connector import (
    SupportsBatchWrites as SupportsBatchWrites,
)
from sanka_connector import (
    SupportsBoundedCounts as SupportsBoundedCounts,
)
from sanka_connector import (
    SupportsBoundedReads as SupportsBoundedReads,
)
from sanka_connector import (
    SupportsConfigValidation as SupportsConfigValidation,
)
from sanka_connector import (
    SupportsCredentialRefresh as SupportsCredentialRefresh,
)
from sanka_connector import (
    SupportsHighWaterMark as SupportsHighWaterMark,
)
from sanka_connector import (
    SupportsIdentityInspection as SupportsIdentityInspection,
)
from sanka_connector import (
    SupportsLimits as SupportsLimits,
)
from sanka_connector import (
    SupportsOwnerDirectory as SupportsOwnerDirectory,
)
from sanka_connector import (
    SupportsPropertyProvisioning as SupportsPropertyProvisioning,
)
from sanka_connector import (
    SupportsRecordCounts as SupportsRecordCounts,
)
from sanka_connector import (
    SupportsResourceProvisioning as SupportsResourceProvisioning,
)
from sanka_connector import (
    SupportsRetryMetrics as SupportsRetryMetrics,
)
from sanka_connector import (
    SupportsSchemaProvisioning as SupportsSchemaProvisioning,
)
from sanka_connector import (
    SupportsSnapshotBounds as SupportsSnapshotBounds,
)
from sanka_connector import (
    TransientProviderError as TransientSystemError,
)
from sanka_connector import (
    UnsupportedFeatureError as UnsupportedFeatureError,
)
from sanka_connector import (
    ValidationFailedError as ValidationFailedError,
)
from sanka_connector import (
    WriteOptions as WriteOptions,
)
from sanka_connector import (
    WriteResult as WriteResult,
)
from sanka_connector import (
    __version__ as __version__,
)
from sanka_extensions.systems.records import require_identity_values as require_identity_values

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
