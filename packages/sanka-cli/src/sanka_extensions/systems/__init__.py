# SPDX-License-Identifier: Apache-2.0
"""Compatibility imports; new extensions use sanka_extensions.data."""

from sanka_extensions.data import ENTRY_POINT_GROUP as ENTRY_POINT_GROUP
from sanka_extensions.data import AuthenticationError as AuthenticationError
from sanka_extensions.data import BatchRelationshipWriteResult as BatchRelationshipWriteResult
from sanka_extensions.data import BatchWriteInput as BatchWriteInput
from sanka_extensions.data import BatchWriteResult as BatchWriteResult
from sanka_extensions.data import ConfigurationError as ConfigurationError
from sanka_extensions.data import ConflictError as ConflictError
from sanka_extensions.data import ConflictPolicy as ConflictPolicy
from sanka_extensions.data import CredentialProvider as CredentialProvider
from sanka_extensions.data import Credentials as Credentials
from sanka_extensions.data import CustomObjectDefinition as CustomObjectDefinition
from sanka_extensions.data import CustomObjectProperty as CustomObjectProperty
from sanka_extensions.data import DataAccessError as SystemAccessError
from sanka_extensions.data import DataError as DataError
from sanka_extensions.data import DataIdentity as SystemIdentity
from sanka_extensions.data import DataReader as SystemReader
from sanka_extensions.data import DataTimeoutError as SystemTimeoutError
from sanka_extensions.data import DataWriter as SystemWriter
from sanka_extensions.data import ErrorCategory as ErrorCategory
from sanka_extensions.data import ExtensionRegistration as ExtensionRegistration
from sanka_extensions.data import FieldSchema as FieldSchema
from sanka_extensions.data import InvalidEmailPolicy as InvalidEmailPolicy
from sanka_extensions.data import Inventory as Inventory
from sanka_extensions.data import Limits as Limits
from sanka_extensions.data import NotFoundError as NotFoundError
from sanka_extensions.data import ObjectSchema as ObjectSchema
from sanka_extensions.data import OwnerProfile as OwnerProfile
from sanka_extensions.data import PermissionDeniedError as PermissionDeniedError
from sanka_extensions.data import PipelineDefinition as PipelineDefinition
from sanka_extensions.data import PipelineStage as PipelineStage
from sanka_extensions.data import PropertyDefinition as PropertyDefinition
from sanka_extensions.data import PropertyResult as PropertyResult
from sanka_extensions.data import RateLimitError as RateLimitError
from sanka_extensions.data import RecordPage as RecordPage
from sanka_extensions.data import RelationshipWrite as RelationshipWrite
from sanka_extensions.data import RelationshipWriteResult as RelationshipWriteResult
from sanka_extensions.data import ResourceResult as ResourceResult
from sanka_extensions.data import SchemaMismatchError as SchemaMismatchError
from sanka_extensions.data import SourceFilter as SourceFilter
from sanka_extensions.data import SourceObject as SourceObject
from sanka_extensions.data import SupportsBatchRelationshipWrites as SupportsBatchRelationshipWrites
from sanka_extensions.data import SupportsBatchWrites as SupportsBatchWrites
from sanka_extensions.data import SupportsBoundedCounts as SupportsBoundedCounts
from sanka_extensions.data import SupportsBoundedReads as SupportsBoundedReads
from sanka_extensions.data import SupportsConfigValidation as SupportsConfigValidation
from sanka_extensions.data import SupportsCredentialRefresh as SupportsCredentialRefresh
from sanka_extensions.data import SupportsHighWaterMark as SupportsHighWaterMark
from sanka_extensions.data import SupportsIdentityInspection as SupportsIdentityInspection
from sanka_extensions.data import SupportsLimits as SupportsLimits
from sanka_extensions.data import SupportsOwnerDirectory as SupportsOwnerDirectory
from sanka_extensions.data import SupportsPropertyProvisioning as SupportsPropertyProvisioning
from sanka_extensions.data import SupportsRecordCounts as SupportsRecordCounts
from sanka_extensions.data import SupportsResourceProvisioning as SupportsResourceProvisioning
from sanka_extensions.data import SupportsRetryMetrics as SupportsRetryMetrics
from sanka_extensions.data import SupportsSchemaProvisioning as SupportsSchemaProvisioning
from sanka_extensions.data import SupportsSnapshotBounds as SupportsSnapshotBounds
from sanka_extensions.data import TransientDataError as TransientSystemError
from sanka_extensions.data import UnsupportedFeatureError as UnsupportedFeatureError
from sanka_extensions.data import ValidationFailedError as ValidationFailedError
from sanka_extensions.data import WriteOptions as WriteOptions
from sanka_extensions.data import WriteResult as WriteResult
from sanka_extensions.data import __version__ as __version__
from sanka_extensions.data import require_identity_values as require_identity_values

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
