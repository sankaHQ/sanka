# SPDX-License-Identifier: AGPL-3.0-only
from django.http import HttpResponseRedirect
from django.urls import include, path
from reference_api.views import ErrorView, HealthView, ProjectViewSet, SearchView
from rest_framework.routers import SimpleRouter

router = SimpleRouter()
router.register("projects", ProjectViewSet, basename="project")


def legacy_redirect(request):
    # Plain Django view: outside DRF, so the scanner must disclose it as
    # skipped instead of silently pretending the URL space ends at DRF.
    return HttpResponseRedirect("/api/health/")


urlpatterns = [
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/error/", ErrorView.as_view(), name="error"),
    path("api/search/", SearchView.as_view(), name="search"),
    path("api/", include(router.urls)),
    path("legacy/redirect/", legacy_redirect, name="legacy-redirect"),
]
