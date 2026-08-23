# SPDX-License-Identifier: AGPL-3.0-only
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet

from bulletins.models import Bulletin
from bulletins.permissions import OwnerOrReadOnly
from bulletins.serializers import BulletinSerializer


class BulletinViewSet(ModelViewSet):
    queryset = Bulletin.objects.all()
    serializer_class = BulletinSerializer
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated, OwnerOrReadOnly)

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)
