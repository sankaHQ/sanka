# SPDX-License-Identifier: AGPL-3.0-only
from rest_framework import serializers

from bulletins.models import Bulletin


class BulletinSerializer(serializers.ModelSerializer):
    class Meta:
        model = Bulletin
        fields = ("id", "author", "title", "body")
        read_only_fields = ("id", "author")
