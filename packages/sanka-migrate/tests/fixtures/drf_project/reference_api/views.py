# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import dataclass

from django.db import transaction
from rest_framework.authentication import BaseAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from reference_api.models import Project
from reference_api.serializers import ProjectSerializer


@dataclass(frozen=True)
class ReferenceUser:
    username: str
    is_authenticated: bool = True


class ReferenceHeaderAuthentication(BaseAuthentication):
    def authenticate(self, request):
        username = request.headers.get("X-Reference-User")
        if not username:
            return None
        return ReferenceUser(username), None

    def authenticate_header(self, request):
        return "Reference"


class HealthView(APIView):
    authentication_classes = ()
    permission_classes = ()

    def get(self, request):
        return Response({"status": "ok"})


class ErrorView(APIView):
    authentication_classes = ()
    permission_classes = ()

    def get(self, request):
        raise ValidationError({"query": ["invalid"]})


class SearchView(APIView):
    authentication_classes = ()
    permission_classes = ()

    def get(self, request):
        page = int(request.query_params.get("page", "1"))
        return Response({"page": page, "results": [{"id": f"p-{page}"}]})


class ProjectViewSet(ModelViewSet):
    queryset = Project.objects.none()
    serializer_class = ProjectSerializer
    authentication_classes = (ReferenceHeaderAuthentication,)
    permission_classes = (IsAuthenticated,)

    def list(self, request):
        return Response([{"id": "p-1", "name": "Reference"}])

    def retrieve(self, request, pk=None):
        return Response({"id": pk, "name": "Reference"})

    @action(detail=False, methods=["get"])
    @transaction.atomic
    def featured(self, request):
        return Response({"id": "p-1", "featured": True})
