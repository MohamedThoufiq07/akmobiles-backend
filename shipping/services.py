"""
Shipment orchestration: turns an Order into a Shiprocket shipment and keeps the
order in sync with courier status updates.
"""

import logging
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from orders.models import Order
from orders.services import transition_order_status

from .client import ShiprocketClient, ShiprocketException

logger = logging.getLogger(__name__)


class ShipmentError(Exception):
    """A user-presentable reason a shipment could not be created."""


# Shiprocket status text (upper-cased) -> our fulfilment status.
# Anything not listed only updates `shipping_status`.
_STATUS_MAP = (
    ("OUT FOR DELIVERY", "OutForDelivery"),
    ("DELIVERED", "Delivered"),
    ("PICKED UP", "Shipped"),
    ("IN TRANSIT", "Shipped"),
    ("SHIPPED", "Shipped"),
)


def _digits(value, length=None):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-length:] if length else digits


def _first(d, *keys, default=""):
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return default


def build_order_payload(order, pickup_location):
    """Translate our Order into Shiprocket's `orders/create/adhoc` body."""
    addr = order.shipping_address or {}
    full_name = _first(addr, "name", "fullName", default=getattr(order.user, "name", "Customer"))
    first, _, last = full_name.partition(" ")

    line1 = _first(addr, "addressLine1", "address", "street", "line1")
    line2 = _first(addr, "addressLine2", "landmark", "line2")
    pincode = _digits(_first(addr, "postalCode", "pincode", "zip"), 6)
    phone = _digits(_first(addr, "phone", "mobile", default=getattr(order.user, "phone", "")), 10)

    problems = []
    if not line1:
        problems.append("address line")
    if not _first(addr, "city"):
        problems.append("city")
    if not _first(addr, "state"):
        problems.append("state")
    if len(pincode) != 6:
        problems.append("6-digit pincode")
    if len(phone) != 10:
        problems.append("10-digit phone")
    if problems:
        raise ShipmentError("Shipping address is missing: " + ", ".join(problems) + ".")

    items = []
    for item in order.order_items or []:
        items.append(
            {
                "name": str(item.get("name") or "Product")[:100],
                "sku": str(item.get("product") or item.get("name") or "SKU")[:50],
                "units": int(item.get("quantity") or 1),
                "selling_price": float(item.get("price") or 0),
            }
        )
    if not items:
        raise ShipmentError("Order has no items.")

    method = (order.payment_info or {}).get("method", "Razorpay")
    payment_method = "Prepaid" if method == "Razorpay" else "COD"

    created = order.created_at or timezone.now()
    return {
        "order_id": str(order._id),
        "order_date": timezone.localtime(created).strftime("%Y-%m-%d %H:%M"),
        "pickup_location": pickup_location,
        "billing_customer_name": first or "Customer",
        "billing_last_name": last,
        "billing_address": line1,
        "billing_address_2": line2,
        "billing_city": _first(addr, "city"),
        "billing_pincode": pincode,
        "billing_state": _first(addr, "state"),
        "billing_country": _first(addr, "country", default="India"),
        "billing_email": _first(addr, "email", default=getattr(order.user, "email", "")),
        "billing_phone": phone,
        "shipping_is_billing": True,
        "order_items": items,
        "payment_method": payment_method,
        "shipping_charges": float(order.shipping_price or 0),
        "sub_total": float(order.items_price or order.total_price or 0),
        "length": settings.SHIPROCKET_DEFAULT_LENGTH,
        "breadth": settings.SHIPROCKET_DEFAULT_BREADTH,
        "height": settings.SHIPROCKET_DEFAULT_HEIGHT,
        "weight": settings.SHIPROCKET_DEFAULT_WEIGHT,
    }


def create_shipment(order_id, client=None):
    """
    Create the Shiprocket order, assign a courier/AWB and request pickup.

    Idempotent and retry-safe: an order that already has an AWB is returned
    untouched; one that has a Shiprocket shipment but no AWB (a previous AWB
    attempt failed) only retries the AWB step, so no duplicate order is created.
    """
    client = client or ShiprocketClient()
    awb_error = None

    with transaction.atomic():
        order = Order.objects.select_for_update().select_related("user").filter(_id=order_id).first()
        if not order:
            raise ShipmentError("Order not found.")
        if order.awb_code:
            return order
        if order.order_status in ("AwaitingPayment", "Cancelled", "Returned", "Delivered"):
            raise ShipmentError(f"Cannot ship an order that is {order.order_status}.")

        if not order.shiprocket_shipment_id:
            payload = build_order_payload(order, client.pickup_location)
            try:
                created = client.create_order(payload)
            except ShiprocketException as exc:
                raise ShipmentError(str(exc))
            shipment_id = created.get("shipment_id")
            if not shipment_id:
                raise ShipmentError(
                    created.get("message") or "Shiprocket did not return a shipment id."
                )
            order.shiprocket_order_id = str(created.get("order_id") or "")
            order.shiprocket_shipment_id = str(shipment_id)
            order.shipping_status = "NEW"
            order.shipment_created_at = timezone.now()
            order.save(
                update_fields=[
                    "shiprocket_order_id",
                    "shiprocket_shipment_id",
                    "shipping_status",
                    "shipment_created_at",
                    "updated_at",
                ]
            )

        try:
            _assign_awb_and_pickup(order, client)
        except ShiprocketException as exc:
            awb_error = str(exc)

    if awb_error:
        # The Shiprocket order exists (ids are saved); calling this again retries AWB only.
        raise ShipmentError(f"Shipment created but courier assignment failed: {awb_error}")
    return order


def _assign_awb_and_pickup(order, client):
    data = client.assign_awb(order.shiprocket_shipment_id)
    response = (data.get("response") or {}).get("data") or {}
    awb = response.get("awb_code")
    if not awb:
        raise ShiprocketException(
            data.get("message")
            or "Courier could not be assigned. Check wallet balance and pincode serviceability."
        )
    order.awb_code = str(awb)
    order.courier_name = str(response.get("courier_name") or "")
    order.tracking_url = f"https://shiprocket.co/tracking/{awb}"
    order.shipping_status = "AWB ASSIGNED"
    order.save(
        update_fields=["awb_code", "courier_name", "tracking_url", "shipping_status", "updated_at"]
    )

    try:
        client.schedule_pickup(order.shiprocket_shipment_id)
        order.shipping_status = "PICKUP SCHEDULED"
        order.save(update_fields=["shipping_status", "updated_at"])
    except ShiprocketException:
        # Pickup can be scheduled from the Shiprocket panel; don't fail the shipment.
        logger.warning("Pickup scheduling failed for order %s", order._id)


def apply_tracking_update(payload):
    """
    Apply a Shiprocket webhook payload. Returns the Order or None when the
    payload doesn't match any order (e.g. Shiprocket's "Test Webhook" sample).
    """
    awb = str(payload.get("awb") or "").strip()
    sr_order_id = str(payload.get("order_id") or "").strip()
    status_text = str(payload.get("current_status") or payload.get("shipment_status") or "").strip()

    order = None
    if awb:
        order = Order.objects.filter(awb_code=awb).first()
    if not order and sr_order_id:
        order = Order.objects.filter(_id=sr_order_id).first()
    if not order:
        return None

    if status_text:
        order.shipping_status = status_text[:60]
        order.save(update_fields=["shipping_status", "updated_at"])

        upper = status_text.upper()
        for needle, new_status in _STATUS_MAP:
            if upper == needle or upper.startswith(needle):
                transition_order_status(
                    order._id,
                    new_status,
                    description=f"Courier update: {status_text}",
                )
                break
        order.refresh_from_db()
    return order
