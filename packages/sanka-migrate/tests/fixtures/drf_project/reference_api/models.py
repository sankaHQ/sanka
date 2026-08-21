# SPDX-License-Identifier: AGPL-3.0-only
from django.db import models


class Project(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        app_label = "reference_api"
