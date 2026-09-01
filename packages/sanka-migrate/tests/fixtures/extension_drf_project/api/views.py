# SPDX-License-Identifier: AGPL-3.0-only
from rest_framework.viewsets import ModelViewSet

from api.serializers import Health, HealthSerializer


class HealthViewSet(ModelViewSet):
    queryset = Health.objects.all()
    serializer_class = HealthSerializer
    authentication_classes = ()
    permission_classes = ()
