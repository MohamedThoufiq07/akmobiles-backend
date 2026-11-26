import hmac
import logging

from django.conf import settings
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from common.permissions import IsAdmin
from orders.models import Order

from .client import ShiprocketClient, ShiprocketException
from .services import ShipmentError, apply_tracking_update, create_shipment

logger = logging.getLogger(__name__)


def _shipment_dict(order):
    return {
        "shiprocketOrderId": order.shiprocket_order_id,
        "shipmentId": order.shiprocket_shipment_id,
        "awbCode": order.awb_code,
        "courierName": order.courier_name,
        "trackingUrl": order.tracking_url,
        "shippingStatus": order.shipping_status,
        "orderStatus": order.order_status,
    }


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


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def create_shipment_view(request, order_id):
    """Admin: create the Shiprocket shipment (order + AWB + pickup) for an order."""
    try:
        order = create_shipment(order_id)
    except ShipmentError as exc:
        return Response({"success": False, "message": str(exc)}, status=400)
    except ShiprocketException as exc:
        return Response({"success": False, "message": str(exc)}, status=502)
    return Response({"success": True, "shipment": _shipment_dict(order)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def shipment_status_view(request, order_id):
    """Order owner or admin: stored shipment details for an order."""
    order = Order.objects.filter(_id=order_id).first()
    if not order:
        return Response({"success": False, "message": "Order not found."}, status=404)
    if request.user.role != "admin" and order.user_id != request.user._id:
        return Response({"success": False, "message": "Not authorized."}, status=403)
    return Response({"success": True, "shipment": _shipment_dict(order)})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def tracking_updates_webhook(request):
    """
    Courier status webhook. Authenticated by the shared `x-api-key` header.
    Always answers 200 for authenticated calls (even for unknown orders) so the
    sender doesn't keep retrying and its "Test Webhook" check passes.
    """
    expected = getattr(settings, "SHIPROCKET_WEBHOOK_TOKEN", "")
    if not expected:
        return Response({"success": False, "message": "Webhook not configured."}, status=503)

    provided = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        return Response({"success": False, "message": "Unauthorized."}, status=401)

    payload = request.data if isinstance(request.data, dict) else {}
    try:
        order = apply_tracking_update(payload)
    except Exception:
        logger.exception("Failed to apply tracking update")
        return Response({"success": False}, status=500)

    return Response({"success": True, "matched": order is not None})
