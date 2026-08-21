# SPDX-License-Identifier: AGPL-3.0-only
from rest_framework import serializers

from reference_api.models import Project


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ("id", "name")
