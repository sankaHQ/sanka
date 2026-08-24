# SPDX-License-Identifier: AGPL-3.0-only
from django.urls import include, path
from listings.views import ListingViewSet
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register("listings", ListingViewSet, basename="listing")

urlpatterns = [path("api/", include(router.urls))]
