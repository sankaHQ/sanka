# SPDX-License-Identifier: AGPL-3.0-only
from django.db import models


class Listing(models.Model):
    STATE_CHOICES = (
        ("draft", "Draft"),
        ("active", "Active"),
        ("closed", "Closed"),
    )

    code = models.CharField(max_length=20, unique=True)
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default="draft")
    note = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        ordering = ("id",)


class ListingItem(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="entries")
    sku = models.CharField(max_length=30)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=6, decimal_places=2)

    class Meta:
        ordering = ("id",)
