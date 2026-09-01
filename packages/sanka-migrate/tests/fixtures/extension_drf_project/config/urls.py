# SPDX-License-Identifier: AGPL-3.0-only
from api.views import HealthViewSet
from django.urls import include, path
from rest_framework.routers import SimpleRouter

router = SimpleRouter()
router.register("health", HealthViewSet, basename="health")

urlpatterns = [path("api/", include(router.urls))]
