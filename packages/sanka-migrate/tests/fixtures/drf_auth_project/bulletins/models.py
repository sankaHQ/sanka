# SPDX-License-Identifier: AGPL-3.0-only
from django.conf import settings
from django.db import models


class Bulletin(models.Model):
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bulletins",
    )
    title = models.CharField(max_length=90)
    body = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ("id",)
