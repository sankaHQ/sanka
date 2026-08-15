# SPDX-License-Identifier: AGPL-3.0-only
"""Email-to-owner-id resolution against destination and source directories.

Faithful port of the production owner-mapping module. Emails are trimmed and
lower-cased before lookup; duplicated or inactive destination owners always
block (regardless of policy), and the missing-owner policy decides between
blocking, leaving the property empty, and a reviewed fallback owner.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from ferry.connector import OwnerProfile
from ferry.runtime.mapping.errors import MappingError
from ferry.runtime.mapping.model import MigrationMappingField

MissingOwnerPolicy = Literal["block", "leave_empty", "fallback"]


def normalize_owner_email(value: Any) -> str:
    return str(value or "").strip().lower()


class OwnerDirectory:
    """Destination-side owner lookup keyed by normalized email."""

    def __init__(self, profiles: list[OwnerProfile]) -> None:
        by_email: dict[str, list[OwnerProfile]] = defaultdict(list)
        for profile in profiles:
            email = normalize_owner_email(profile.email)
            if email:
                by_email[email].append(profile)
        self._by_email = dict(by_email)

    def resolve(
        self,
        email: Any,
        *,
        policy: MissingOwnerPolicy,
        fallback_email: str | None,
    ) -> str | None:
        normalized = normalize_owner_email(email)
        matches = self._by_email.get(normalized, [])
        active = [profile for profile in matches if profile.active]
        if len(active) == 1 and len(matches) == 1:
            return active[0].id
        if len(matches) > 1:
            raise MappingError(
                f"Destination owner email is duplicated: {normalized}",
                code="FERRY_OWNER_EMAIL_DUPLICATE",
            )
        if matches and not active:
            raise MappingError(
                f"Destination owner is inactive: {normalized}",
                code="FERRY_OWNER_INACTIVE",
            )
        if policy == "leave_empty":
            return None
        if policy == "fallback":
            fallback = normalize_owner_email(fallback_email)
            if not fallback:
                raise MappingError(
                    "A fallback owner email is required.",
                    code="FERRY_FALLBACK_OWNER_REQUIRED",
                )
            if fallback == normalized:
                raise MappingError(
                    f"Destination owner email was not found: {normalized}",
                    code="FERRY_OWNER_NOT_FOUND",
                )
            return self.resolve(fallback, policy="block", fallback_email=None)
        raise MappingError(
            f"Destination owner email was not found: {normalized}",
            code="FERRY_OWNER_NOT_FOUND",
        )


class SourceOwnerDirectory:
    """Source-side owner lookup: provider user id to normalized email."""

    def __init__(self, profiles: list[OwnerProfile]) -> None:
        self._by_id = {
            str(profile.id or "").strip(): profile
            for profile in profiles
            if str(profile.id or "").strip()
        }

    def email_for_id(self, owner_id: Any) -> str | None:
        profile = self._by_id.get(str(owner_id or "").strip())
        if profile is None or not profile.active:
            return None
        return normalize_owner_email(profile.email) or None


def map_owner_properties(
    properties: dict[str, Any],
    fields: list[MigrationMappingField],
    *,
    directory: OwnerDirectory,
    source_directory: SourceOwnerDirectory | None = None,
    policy: MissingOwnerPolicy,
    fallback_email: str | None,
) -> dict[str, Any]:
    mapped = dict(properties)
    for field in fields:
        if field.mapping_kind != "owner":
            continue
        source_value = mapped.get(field.target_field)
        source_email = normalize_owner_email(source_value)
        if source_directory is not None and "@" not in source_email:
            source_email = source_directory.email_for_id(source_value) or ""
        owner_id = directory.resolve(
            source_email,
            policy=policy,
            fallback_email=fallback_email,
        )
        if owner_id is None:
            mapped.pop(field.target_field, None)
        else:
            mapped[field.target_field] = owner_id
    return mapped
