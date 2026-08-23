# SPDX-License-Identifier: AGPL-3.0-only
from rest_framework.viewsets import ModelViewSet

from inventory.models import Gadget
from inventory.serializers import GadgetSerializer


class GadgetViewSet(ModelViewSet):
    queryset = Gadget.objects.all()
    serializer_class = GadgetSerializer
