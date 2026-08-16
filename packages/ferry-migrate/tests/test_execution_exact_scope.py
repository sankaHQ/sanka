# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-ID pilot scope tests.

The safety runbook's exact-ID pilot ("an exact saved ID set and its hash")
as engine capability: the scope builder canonicalizes and hashes candidate
sets and refuses drifted or mis-enumerated ones; ``run_batch`` verifies the
candidate hash before touching anything, intersects every source page with
the candidate set, and refuses route completion until the execution ledger
covers every candidate id.
"""

from __future__ import annotations

import pytest

from ferry.runtime.execution import (
    EXACT_CANDIDATE_HASH_MISMATCH_CODE,
    ExactIdScope,
    ExecutionFault,
    ExecutionScope,
    exact_candidate_hash,
    exact_id_scope,
)
from ferry.runtime.mapping import MigrationMappingField, mapping_groups
from ferry.runtime.mapping.record_mapping import MappingGroup


def _two_route_groups() -> list[MappingGroup]:
    return mapping_groups(
        [
            MigrationMappingField(
                source_field="Account.Name", target_object="companies", target_field="name"
            ),
            MigrationMappingField(
                source_field="Contact.LastName", target_object="contacts", target_field="lastname"
            ),
        ]
    )


# -- builder canonicalization -------------------------------------------------


def test_exact_id_scope_freezes_canonical_candidates_and_totals() -> None:
    scope = exact_id_scope(
        groups=_two_route_groups(),
        candidate_ids_by_route={
            "Contact|contacts": ["003B", " 003A ", "003B", ""],
            "Account|companies": ["001A"],
        },
    )

    assert isinstance(scope, ExecutionScope)  # a tagged variant, not a sibling
    assert scope.selected_route_keys == ("Account|companies", "Contact|contacts")
    assert scope.candidate_ids_by_route == {
        "Account|companies": ("001A",),
        "Contact|contacts": ("003A", "003B"),
    }
    assert scope.route_totals == {"Account|companies": 1, "Contact|contacts": 2}
    assert scope.route_high_water_marks == {}
    assert scope.candidate_hash.startswith("sha256:")
    assert scope.scope_hash.startswith("sha256:")
    scope.verify_candidate_hash()  # a builder-made scope always verifies


def test_exact_candidate_hash_is_order_insensitive_and_content_sensitive() -> None:
    baseline = exact_candidate_hash({"a|b": ["2", "1"], "c|d": ["9"]})

    assert baseline == exact_candidate_hash({"c|d": ["9"], "a|b": ["1", "2", " 1 "]})
    assert baseline != exact_candidate_hash({"a|b": ["2", "1"], "c|d": ["8"]})
    assert baseline != exact_candidate_hash({"a|b": ["2", "1"]})


def test_exact_id_scope_verifies_an_expected_candidate_hash() -> None:
    approved = exact_candidate_hash({"Account|companies": ["001A", "001B"]})

    scope = exact_id_scope(
        groups=_two_route_groups(),
        candidate_ids_by_route={"Account|companies": ["001B", "001A"]},
        expected_candidate_hash=approved,
    )
    assert scope.candidate_hash == approved
    assert scope.selected_route_keys == ("Account|companies",)

    with pytest.raises(ExecutionFault) as excinfo:
        exact_id_scope(
            groups=_two_route_groups(),
            candidate_ids_by_route={"Account|companies": ["001A", "001C"]},
            expected_candidate_hash=approved,
        )
    assert excinfo.value.code == EXACT_CANDIDATE_HASH_MISMATCH_CODE
    assert excinfo.value.details["expectedCandidateHash"] == approved
    assert excinfo.value.details["candidateHash"] != approved


# -- builder refusals ---------------------------------------------------------


def test_exact_id_scope_requires_candidates_for_exactly_the_selected_routes() -> None:
    with pytest.raises(ExecutionFault) as missing:
        exact_id_scope(
            groups=_two_route_groups(),
            candidate_ids_by_route={"Account|companies": ["001A"]},
            requested_route_keys=["Account|companies", "Contact|contacts"],
        )
    assert missing.value.code == "FERRY_EXECUTION_ROUTE_INVALID"
    assert missing.value.details == {"routeKeysWithoutCandidates": ["Contact|contacts"]}

    with pytest.raises(ExecutionFault) as extra:
        exact_id_scope(
            groups=_two_route_groups(),
            candidate_ids_by_route={
                "Account|companies": ["001A"],
                "Contact|contacts": ["003A"],
            },
            requested_route_keys=["Account|companies"],
        )
    assert extra.value.code == "FERRY_EXECUTION_ROUTE_INVALID"
    assert extra.value.details == {"unselectedCandidateRouteKeys": ["Contact|contacts"]}


def test_exact_id_scope_refuses_routes_outside_the_reviewed_mapping() -> None:
    with pytest.raises(ExecutionFault) as excinfo:
        exact_id_scope(
            groups=_two_route_groups(),
            candidate_ids_by_route={"Lead|leads": ["00QA"]},
        )
    assert excinfo.value.code == "FERRY_EXECUTION_ROUTE_INVALID"
    assert excinfo.value.details == {"unknownRouteKeys": ["Lead|leads"]}


def test_exact_id_scope_refuses_empty_maps_and_string_candidates() -> None:
    with pytest.raises(ExecutionFault) as empty:
        exact_id_scope(groups=_two_route_groups(), candidate_ids_by_route={})
    assert empty.value.code == "FERRY_EXECUTION_ROUTE_INVALID"

    with pytest.raises(ExecutionFault) as stringly:
        exact_id_scope(
            groups=_two_route_groups(),
            candidate_ids_by_route={"Account|companies": "001A"},
        )
    assert stringly.value.code == "FERRY_EXECUTION_ROUTE_INVALID"
    assert stringly.value.details == {"routeKey": "Account|companies"}


def test_verify_candidate_hash_refuses_a_drifted_hand_built_scope() -> None:
    built = exact_id_scope(
        groups=_two_route_groups(),
        candidate_ids_by_route={"Account|companies": ["001A"]},
    )
    tampered = ExactIdScope(
        route_manifest=built.route_manifest,
        selected_route_keys=built.selected_route_keys,
        route_high_water_marks=built.route_high_water_marks,
        route_totals=built.route_totals,
        candidate_ids_by_route={"Account|companies": ("001A", "001Z")},
        candidate_hash=built.candidate_hash,
    )

    with pytest.raises(ExecutionFault) as excinfo:
        tampered.verify_candidate_hash()

    assert excinfo.value.code == EXACT_CANDIDATE_HASH_MISMATCH_CODE
    assert excinfo.value.details["candidateHash"] == built.candidate_hash
