# SPDX-License-Identifier: Apache-2.0
"""Compatibility imports; new extensions use sanka_extensions.app."""

from sanka_extensions.app import ENTRY_POINT_GROUP as ENTRY_POINT_GROUP
from sanka_extensions.app import AuthenticationError as AuthenticationError
from sanka_extensions.app import BatchRelationshipWriteResult as BatchRelationshipWriteResult
from sanka_extensions.app import BatchWriteInput as BatchWriteInput
from sanka_extensions.app import BatchWriteResult as BatchWriteResult
from sanka_extensions.app import ConfigurationError as ConfigurationError
from sanka_extensions.app import ConflictError as ConflictError
from sanka_extensions.app import ConflictPolicy as ConflictPolicy
from sanka_extensions.app import CredentialProvider as CredentialProvider
from sanka_extensions.app import Credentials as Credentials
from sanka_extensions.app import CustomObjectDefinition as CustomObjectDefinition
from sanka_extensions.app import CustomObjectProperty as CustomObjectProperty
from sanka_extensions.app import DataAccessError as SystemAccessError
from sanka_extensions.app import DataError as DataError
from sanka_extensions.app import DataIdentity as SystemIdentity
from sanka_extensions.app import DataReader as SystemReader
from sanka_extensions.app import DataTimeoutError as SystemTimeoutError
from sanka_extensions.app import DataWriter as SystemWriter
from sanka_extensions.app import ErrorCategory as ErrorCategory
from sanka_extensions.app import ExtensionRegistration as ExtensionRegistration
from sanka_extensions.app import FieldSchema as FieldSchema
from sanka_extensions.app import InvalidEmailPolicy as InvalidEmailPolicy
from sanka_extensions.app import Inventory as Inventory
from sanka_extensions.app import Limits as Limits
from sanka_extensions.app import NotFoundError as NotFoundError
from sanka_extensions.app import ObjectSchema as ObjectSchema
from sanka_extensions.app import OwnerProfile as OwnerProfile
from sanka_extensions.app import PermissionDeniedError as PermissionDeniedError
from sanka_extensions.app import PipelineDefinition as PipelineDefinition
from sanka_extensions.app import PipelineStage as PipelineStage
from sanka_extensions.app import PropertyDefinition as PropertyDefinition
from sanka_extensions.app import PropertyResult as PropertyResult
from sanka_extensions.app import RateLimitError as RateLimitError
from sanka_extensions.app import RecordPage as RecordPage
from sanka_extensions.app import RelationshipWrite as RelationshipWrite
from sanka_extensions.app import RelationshipWriteResult as RelationshipWriteResult
from sanka_extensions.app import ResourceResult as ResourceResult
from sanka_extensions.app import SchemaMismatchError as SchemaMismatchError
from sanka_extensions.app import SourceFilter as SourceFilter
from sanka_extensions.app import SourceObject as SourceObject
from sanka_extensions.app import SupportsBatchRelationshipWrites as SupportsBatchRelationshipWrites
from sanka_extensions.app import SupportsBatchWrites as SupportsBatchWrites
from sanka_extensions.app import SupportsBoundedCounts as SupportsBoundedCounts
from sanka_extensions.app import SupportsBoundedReads as SupportsBoundedReads
from sanka_extensions.app import SupportsConfigValidation as SupportsConfigValidation
from sanka_extensions.app import SupportsCredentialRefresh as SupportsCredentialRefresh
from sanka_extensions.app import SupportsHighWaterMark as SupportsHighWaterMark
from sanka_extensions.app import SupportsIdentityInspection as SupportsIdentityInspection
from sanka_extensions.app import SupportsLimits as SupportsLimits
from sanka_extensions.app import SupportsOwnerDirectory as SupportsOwnerDirectory
from sanka_extensions.app import SupportsPropertyProvisioning as SupportsPropertyProvisioning
from sanka_extensions.app import SupportsRecordCounts as SupportsRecordCounts
from sanka_extensions.app import SupportsResourceProvisioning as SupportsResourceProvisioning
from sanka_extensions.app import SupportsRetryMetrics as SupportsRetryMetrics
from sanka_extensions.app import SupportsSchemaProvisioning as SupportsSchemaProvisioning
from sanka_extensions.app import SupportsSnapshotBounds as SupportsSnapshotBounds
from sanka_extensions.app import TransientDataError as TransientSystemError
from sanka_extensions.app import UnsupportedFeatureError as UnsupportedFeatureError
from sanka_extensions.app import ValidationFailedError as ValidationFailedError
from sanka_extensions.app import WriteOptions as WriteOptions
from sanka_extensions.app import WriteResult as WriteResult
from sanka_extensions.app import __version__ as __version__
from sanka_extensions.app import require_identity_values as require_identity_values

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
