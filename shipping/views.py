from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from common.permissions import IsAdmin
from .client import ShiprocketClient


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def shiprocket_connection_status_view(request):
    """
    Admin-only endpoint to test and verify Shiprocket API connectivity.
    Never exposes API secrets, tokens, or raw credentials in responses.
    """
    client = ShiprocketClient()
    result = client.test_connection()
    return Response(result, status=200)
