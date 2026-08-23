# SPDX-License-Identifier: AGPL-3.0-only
from bulletins.views import BulletinViewSet
from django.urls import include, path
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register("bulletins", BulletinViewSet, basename="bulletin")

urlpatterns = [path("api/", include(router.urls))]
