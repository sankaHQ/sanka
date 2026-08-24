# SPDX-License-Identifier: AGPL-3.0-only
from django.db import transaction
from rest_framework import serializers

from listings.models import Listing, ListingItem


class ListingItemSerializer(serializers.ModelSerializer):
    quantity = serializers.IntegerField(min_value=1)

    class Meta:
        model = ListingItem
        fields = ("id", "sku", "quantity", "price")
        read_only_fields = ("id",)


class ListingSerializer(serializers.ModelSerializer):
    entries = ListingItemSerializer(many=True)

    class Meta:
        model = Listing
        fields = ("id", "code", "state", "note", "entries")
        read_only_fields = ("id",)

    def create(self, validated_data):
        entries_data = validated_data.pop("entries")
        with transaction.atomic():
            listing = Listing.objects.create(**validated_data)
            for entry_data in entries_data:
                ListingItem.objects.create(listing=listing, **entry_data)
            total = sum(entry.quantity for entry in listing.entries.all())
            if total > 50:
                raise serializers.ValidationError({"entries": ["Listing exceeds 50 total units."]})
        return listing

    def update(self, instance, validated_data):
        validated_data.pop("entries", None)
        return super().update(instance, validated_data)
