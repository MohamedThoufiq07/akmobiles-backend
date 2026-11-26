"""
Order cancellation and refunds.

cancel_order():  validates -> cancels the Shiprocket shipment (if any) -> marks the
                 order Cancelled (history + customer e-mail) -> returns stock -> refunds.
issue_refund():  full Razorpay refund for a paid online order; safe to retry.

Ordering matters: the courier shipment is cancelled FIRST so that a failure there
leaves the order untouched instead of cancelled-but-still-being-picked-up. A refund
failure never undoes the cancellation; it is recorded so an admin can retry.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from orders.models import Order
from orders.services import transition_order_status
from products.models import Product

logger = logging.getLogger(__name__)

# Only before the courier has the parcel.
CANCELLABLE_STATUSES = {"AwaitingPayment", "Placed", "Processing", "Packed"}
_IN_COURIER_HANDS = ("PICKED UP", "IN TRANSIT", "OUT FOR", "DELIVERED", "SHIPPED", "RTO")


class CancellationError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _payment_of(order):
    try:
        return order.payment
    except Exception:
        return None


def _cancel_shipment(order, client=None):
    if not (order.shiprocket_order_id or order.awb_code):
        return
    if (order.shipping_status or "").upper().startswith("CANCEL"):
        return

    from shipping.client import ShiprocketClient, ShiprocketException

    client = client or ShiprocketClient()
    try:
        if order.awb_code:
            client.cancel_awb(order.awb_code)
        if order.shiprocket_order_id:
            client.cancel_order(order.shiprocket_order_id)
    except ShiprocketException as exc:
        raise CancellationError(
            f"Could not cancel the shipment with the courier ({exc}). "
            "Please try again in a minute, or cancel it in the Shiprocket panel first.",
            status=502,
        )
    order.shipping_status = "CANCELED"
    order.save(update_fields=["shipping_status", "updated_at"])


def _restock_once(order):
    """Give the items back to stock, exactly once per order."""
    method = (order.payment_info or {}).get("method", "Razorpay")
    if method == "Razorpay":
        payment = _payment_of(order)
        if not payment or not payment.stock_reduced:
            return  # stock was never taken (unpaid order)
        payment.stock_reduced = False
        payment.save(update_fields=["stock_reduced", "updated_at"])

    for item in order.order_items or []:
        pid, qty = item.get("product"), int(item.get("quantity") or 1)
        if pid:
            Product.objects.filter(_id=pid).update(
                stock=F("stock") + qty, num_sold=F("num_sold") - qty
            )
    Product.objects.filter(num_sold__lt=0).update(num_sold=0)


def _razorpay_client():
    import razorpay

    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def issue_refund(order_id, rz_client=None):
    """
    Refund the full amount of a paid online order. Never raises.
    Returns {"status": "Refunded" | "AlreadyRefunded" | "NotRequired" | "Failed", ...}.
    """
    from payments.models import Payment

    try:
        order = Order.objects.filter(_id=order_id).first()
        if not order:
            return {"status": "Failed", "message": "Order not found."}

        with transaction.atomic():
            payment = Payment.objects.select_for_update().filter(order_id=order_id).first()
            if not payment:
                return {"status": "NotRequired"}
            if payment.status == "Refunded":
                return {"status": "AlreadyRefunded"}
            if payment.status != "Completed" or not payment.razorpay_payment_id:
                return {"status": "NotRequired"}

            amount = int(payment.amount_paise or round(float(order.total_price) * 100))
            try:
                client = rz_client or _razorpay_client()
                refund = client.payment.refund(
                    payment.razorpay_payment_id,
                    {
                        "amount": amount,
                        "speed": "normal",
                        "notes": {"order_id": str(order._id), "reason": "order_cancelled"},
                    },
                )
            except Exception as exc:
                message = str(exc)[:300] or "Razorpay refund failed."
                logger.warning("Refund failed for order %s: %s", order_id, message)
                info = dict(order.payment_info or {})
                info.update({"refundStatus": "Failed", "refundError": message})
                order.payment_info = info
                order.save(update_fields=["payment_info", "updated_at"])
                return {"status": "Failed", "message": message}

            payment.status = "Refunded"
            payment.save(update_fields=["status", "updated_at"])
            info = dict(order.payment_info or {})
            info.update(
                {
                    "status": "Refunded",
                    "refundStatus": "Refunded",
                    "refundId": (refund or {}).get("id", ""),
                    "refundAmount": amount / 100,
                    "refundedAt": timezone.now().isoformat(),
                }
            )
            info.pop("refundError", None)
            order.payment_info = info
            order.save(update_fields=["payment_info", "updated_at"])
            return {"status": "Refunded", "refundId": info["refundId"], "amount": amount / 100}
    except Exception:
        logger.exception("Unexpected refund error for order %s", order_id)
        return {"status": "Failed", "message": "Unexpected error while refunding."}


def cancel_order(order_id, actor, reason="", client=None, rz_client=None):
    """
    Cancel an order on behalf of `actor` (the owner or an admin).
    Returns (order, refund_info). Raises CancellationError with a user-safe message.
    """
    is_admin = getattr(actor, "role", "") == "admin"

    with transaction.atomic():
        order = Order.objects.select_for_update().select_related("user").filter(_id=order_id).first()
        if not order or (not is_admin and order.user_id != actor._id):
            raise CancellationError("Order not found.", status=404)

        if order.order_status == "Cancelled":
            return order, {"status": "AlreadyCancelled"}
        if order.order_status not in CANCELLABLE_STATUSES:
            raise CancellationError(
                f"This order is already {order.order_status} and can no longer be cancelled."
            )
        shipping_state = (order.shipping_status or "").upper()
        if order.awb_code and any(shipping_state.startswith(s) for s in _IN_COURIER_HANDS):
            raise CancellationError(
                "The courier has already picked up this parcel, so it can't be cancelled now."
            )

        _cancel_shipment(order, client)

        who = "admin" if is_admin else "customer"
        note = f"Order cancelled by {who}" + (f": {reason.strip()[:200]}" if reason and reason.strip() else "")
        transition_order_status(order, "Cancelled", description=note)
        order.refresh_from_db()
        _restock_once(order)

    refund = issue_refund(order_id, rz_client=rz_client)
    order.refresh_from_db()
    return order, refund
