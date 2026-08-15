# SPDX-License-Identifier: AGPL-3.0-only
"""Mapping and relationship semantics for the migration planner and engine.

Faithful port of the production Ferry mapping modules: reviewed field
mappings and scalar transforms, heuristic auto-mapping candidates, owner
(assignee) resolution, and deferred relationship linking. Behavior —
transform rules, scoring weights, thresholds, tie-breaking, and route-key
derivation — is preserved exactly; only the host-service plumbing (HTTP
envelope, workspace scoping) is gone.
"""

from ferry.runtime.mapping.errors import MappingError
from ferry.runtime.mapping.mapping_candidates import generate_mapping_candidates
from ferry.runtime.mapping.model import (
    AssociationCategory,
    MappingCandidateSet,
    MappingKind,
    MigrationMappingField,
    UnmappedValuePolicy,
    ValueMapEntry,
    ValueMapScalar,
)
from ferry.runtime.mapping.owner_mapping import (
    MissingOwnerPolicy,
    OwnerDirectory,
    SourceOwnerDirectory,
    map_owner_properties,
    normalize_owner_email,
)
from ferry.runtime.mapping.pending_relationships import (
    IdentityLedger,
    PendingRelationship,
    PendingRelationships,
    PendingRelationshipsByRoute,
    SharedIdentityLedger,
    pending_relationship,
    pending_relationship_key,
    pending_relationship_source_ids,
    report_pending_relationships,
    resolve_destination_record_ids,
    retry_pending_relationships,
)
from ferry.runtime.mapping.record_mapping import (
    MappingGroup,
    MappingRouteManifest,
    apply_transform,
    destination_identity_fields,
    destination_properties,
    mapping_group_key,
    mapping_groups,
    mapping_route_manifest,
    relationship_source_ids,
    source_field_keys,
)

__all__ = [
    "AssociationCategory",
    "IdentityLedger",
    "MappingCandidateSet",
    "MappingError",
    "MappingGroup",
    "MappingKind",
    "MappingRouteManifest",
    "MigrationMappingField",
    "MissingOwnerPolicy",
    "OwnerDirectory",
    "PendingRelationship",
    "PendingRelationships",
    "PendingRelationshipsByRoute",
    "SharedIdentityLedger",
    "SourceOwnerDirectory",
    "UnmappedValuePolicy",
    "ValueMapEntry",
    "ValueMapScalar",
    "apply_transform",
    "destination_identity_fields",
    "destination_properties",
    "generate_mapping_candidates",
    "map_owner_properties",
    "mapping_group_key",
    "mapping_groups",
    "mapping_route_manifest",
    "normalize_owner_email",
    "pending_relationship",
    "pending_relationship_key",
    "pending_relationship_source_ids",
    "relationship_source_ids",
    "report_pending_relationships",
    "resolve_destination_record_ids",
    "retry_pending_relationships",
    "source_field_keys",
]
