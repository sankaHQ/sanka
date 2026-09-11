# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from sanka.runtime.mapping import (
    MappingError,
    MigrationMappingField,
    OwnerDirectory,
    SourceOwnerDirectory,
    map_owner_properties,
)
from sanka_extensions.systems import OwnerProfile


def _directory(*profiles: OwnerProfile) -> OwnerDirectory:
    return OwnerDirectory(list(profiles))


def test_owner_directory_matches_trimmed_case_insensitive_email() -> None:
    directory = _directory(OwnerProfile(id="owner-1", email="Ada@Example.com", active=True))

    assert (
        directory.resolve(
            "  ada@example.com ",
            policy="block",
            fallback_email=None,
        )
        == "owner-1"
    )


@pytest.mark.parametrize(
    ("profiles", "code"),
    [
        (
            [
                OwnerProfile(id="owner-1", email="ada@example.com"),
                OwnerProfile(id="owner-2", email="ADA@example.com"),
            ],
            "SANKA_MIGRATE_OWNER_EMAIL_DUPLICATE",
        ),
        (
            [OwnerProfile(id="owner-1", email="ada@example.com", active=False)],
            "SANKA_MIGRATE_OWNER_INACTIVE",
        ),
    ],
)
def test_owner_directory_blocks_ambiguous_or_inactive_profiles(
    profiles: list[OwnerProfile],
    code: str,
) -> None:
    with pytest.raises(MappingError) as exc_info:
        _directory(*profiles).resolve(
            "ada@example.com",
            policy="block",
            fallback_email=None,
        )

    assert exc_info.value.code == code


def test_owner_directory_missing_email_honors_policy() -> None:
    directory = _directory(OwnerProfile(id="owner-1", email="ada@example.com"))

    assert (
        directory.resolve("missing@example.com", policy="leave_empty", fallback_email=None) is None
    )
    with pytest.raises(MappingError) as exc_info:
        directory.resolve("missing@example.com", policy="block", fallback_email=None)
    assert exc_info.value.code == "SANKA_MIGRATE_OWNER_NOT_FOUND"


def test_owner_directory_applies_explicit_fallback() -> None:
    directory = _directory(OwnerProfile(id="fallback-1", email="fallback@example.com"))

    assert (
        directory.resolve(
            "missing@example.com",
            policy="fallback",
            fallback_email="fallback@example.com",
        )
        == "fallback-1"
    )


def test_owner_directory_fallback_requires_fallback_email() -> None:
    directory = _directory(OwnerProfile(id="owner-1", email="ada@example.com"))

    with pytest.raises(MappingError) as exc_info:
        directory.resolve("missing@example.com", policy="fallback", fallback_email=" ")

    assert exc_info.value.code == "SANKA_MIGRATE_FALLBACK_OWNER_REQUIRED"


def test_owner_directory_fallback_must_differ_from_missing_email() -> None:
    directory = _directory(OwnerProfile(id="owner-1", email="ada@example.com"))

    with pytest.raises(MappingError) as exc_info:
        directory.resolve(
            "missing@example.com",
            policy="fallback",
            fallback_email="Missing@example.com",
        )

    assert exc_info.value.code == "SANKA_MIGRATE_OWNER_NOT_FOUND"


def test_owner_property_mapping_replaces_email_with_provider_id() -> None:
    fields = [
        MigrationMappingField(
            source_field="Contact.OwnerEmail",
            target_object="contacts",
            target_field="hubspot_owner_id",
            mapping_kind="owner",
        )
    ]

    mapped = map_owner_properties(
        {"hubspot_owner_id": "ada@example.com", "firstname": "Ada"},
        fields,
        directory=_directory(OwnerProfile(id="42", email="ada@example.com")),
        policy="block",
        fallback_email=None,
    )

    assert mapped == {"hubspot_owner_id": "42", "firstname": "Ada"}


def test_owner_property_mapping_resolves_source_owner_id_before_destination() -> None:
    fields = [
        MigrationMappingField(
            source_field="Account.OwnerId",
            target_object="companies",
            target_field="hubspot_owner_id",
            mapping_kind="owner",
        )
    ]

    mapped = map_owner_properties(
        {"hubspot_owner_id": "005A", "name": "Acme"},
        fields,
        directory=_directory(OwnerProfile(id="42", email="ada@example.com")),
        source_directory=SourceOwnerDirectory([OwnerProfile(id="005A", email="ADA@example.com")]),
        policy="block",
        fallback_email=None,
    )

    assert mapped == {"hubspot_owner_id": "42", "name": "Acme"}


def test_owner_property_mapping_removes_unresolved_source_owner_when_configured() -> None:
    fields = [
        MigrationMappingField(
            source_field="Account.OwnerId",
            target_object="companies",
            target_field="hubspot_owner_id",
            mapping_kind="owner",
        )
    ]

    mapped = map_owner_properties(
        {"hubspot_owner_id": "005-INACTIVE", "name": "Acme"},
        fields,
        directory=_directory(OwnerProfile(id="42", email="ada@example.com")),
        source_directory=SourceOwnerDirectory([]),
        policy="leave_empty",
        fallback_email=None,
    )

    assert mapped == {"name": "Acme"}


def test_source_owner_directory_ignores_inactive_profiles() -> None:
    directory = SourceOwnerDirectory(
        [
            OwnerProfile(id="005A", email="ada@example.com"),
            OwnerProfile(id="005B", email="grace@example.com", active=False),
        ]
    )

    assert directory.email_for_id(" 005A ") == "ada@example.com"
    assert directory.email_for_id("005B") is None
    assert directory.email_for_id("missing") is None
