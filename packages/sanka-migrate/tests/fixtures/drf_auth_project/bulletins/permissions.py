# SPDX-License-Identifier: AGPL-3.0-only
from rest_framework.permissions import SAFE_METHODS, BasePermission


class OwnerOrReadOnly(BasePermission):
    """If-form of the canonical owner idiom (the or-form lives in the bench)."""

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        return obj.author_id == request.user.id
