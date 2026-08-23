# SPDX-License-Identifier: AGPL-3.0-only
"""Structural matchers for the auth envelope: accept the canonical idioms,
reject anything whose semantics cannot be regenerated honestly."""

from __future__ import annotations

from typing import Any

from rest_framework import permissions as drf_permissions  # type: ignore[import-untyped]
from rest_framework.permissions import (  # type: ignore[import-untyped]
    SAFE_METHODS,
    BasePermission,
)

from sanka.runtime.frameworks.django_fastapi import (
    _match_owner_permission,
    _match_perform_create,
)


class OrFormOwner(BasePermission):  # type: ignore[misc]
    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        return request.method in SAFE_METHODS or obj.author_id == request.user.id


class IfFormOwner(BasePermission):  # type: ignore[misc]
    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        if request.method in SAFE_METHODS:
            return True
        return obj.owner_id == request.user.id  # type: ignore[no-any-return]


class ReversedComparison(BasePermission):  # type: ignore[misc]
    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        return request.method in SAFE_METHODS or request.user == obj.created_by


class ModuleQualifiedSafeMethods(BasePermission):  # type: ignore[misc]
    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        return request.method in drf_permissions.SAFE_METHODS or obj.author.pk == request.user.pk


class RoleCheck(BasePermission):  # type: ignore[misc]
    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        return bool(request.user.is_staff)


class ExtraStatement(BasePermission):  # type: ignore[misc]
    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        allowed = bool(obj.author_id == request.user.id)
        return request.method in SAFE_METHODS or allowed


class OverridesHasPermission(BasePermission):  # type: ignore[misc]
    def has_permission(self, request: Any, view: Any) -> bool:
        return bool(request.user and request.user.is_staff)

    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        return request.method in SAFE_METHODS or obj.author_id == request.user.id


def test_owner_idiom_shapes_are_recognized() -> None:
    assert _match_owner_permission(OrFormOwner) == "author"
    assert _match_owner_permission(IfFormOwner) == "owner"
    assert _match_owner_permission(ReversedComparison) == "created_by"
    assert _match_owner_permission(ModuleQualifiedSafeMethods) == "author"


def test_non_owner_logic_is_rejected() -> None:
    assert _match_owner_permission(RoleCheck) is None
    assert _match_owner_permission(ExtraStatement) is None
    assert _match_owner_permission(OverridesHasPermission) is None
    assert _match_owner_permission(object) is None


class _CanonicalInjection:
    """Hosts perform_create shapes; only the method source is inspected."""

    request: Any

    def perform_create(self, serializer: Any) -> None:
        serializer.save(author=self.request.user)


class _RenamedArgs:
    request: Any

    def perform_create(self, item: Any) -> None:
        item.save(owner=self.request.user)


class _ExtraKwarg:
    request: Any

    def perform_create(self, serializer: Any) -> None:
        serializer.save(author=self.request.user, moderated=True)


class _NotUser:
    request: Any

    def perform_create(self, serializer: Any) -> None:
        serializer.save(author=self.request.user.profile)


class _TwoStatements:
    request: Any

    def perform_create(self, serializer: Any) -> None:
        instance = serializer.save(author=self.request.user)
        instance.notify()


def test_perform_create_injection_is_recognized() -> None:
    assert _match_perform_create(_CanonicalInjection) == "author"
    assert _match_perform_create(_RenamedArgs) == "owner"


def test_other_perform_create_bodies_are_rejected() -> None:
    assert _match_perform_create(_ExtraKwarg) is None
    assert _match_perform_create(_NotUser) is None
    assert _match_perform_create(_TwoStatements) is None
