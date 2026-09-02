# SPDX-License-Identifier: AGPL-3.0-only
from django.db import models
from rest_framework import serializers


class Health(models.Model):
    id = models.AutoField(primary_key=True)
    status = models.CharField(max_length=32)

    class Meta:
        app_label = "api"


class HealthSerializer(serializers.ModelSerializer):
    class Meta:
        model = Health
        fields = ("id", "status")
