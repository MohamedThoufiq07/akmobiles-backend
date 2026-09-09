import logging
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from products.models import Product

logger = logging.getLogger(__name__)

# Canonical Fulfilment Statuses
FULFILMENT_STATUSES = [
    "AwaitingPayment",
    "Placed",
    "Processing",
    "Packed",
    "Shipped",
    "OutForDelivery",
    "Delivered",
    "Cancelled",
    "Returned",
]

# Canonical Status Descriptions
STATUS_DESCRIPTIONS = {
    "AwaitingPayment": "Order created, awaiting payment confirmation",
    "Placed": "Order has been placed successfully",
    "Processing": "Order is being processed and packed",
    "Packed": "Order has been packed and ready for dispatch",
    "Shipped": "Order has been shipped and is on the way",
    "OutForDelivery": "Order is out for delivery",
    "Delivered": "Order has been delivered successfully",
    "Cancelled": "Order has been cancelled",
    "Returned": "Order has been returned",
}

# Terminal states that cannot be transitioned out of
TERMINAL_STATUSES = {"Delivered", "Cancelled", "Returned"}


def get_admin_orders_queryset(base_qs=None):
    """
    Returns the queryset of confirmed/placed orders visible in Admin Orders and Analytics.
    Excludes pre-payment Razorpay attempts (AwaitingPayment with uncaptured/cancelled payment).
    Includes:
    - COD orders that were successfully placed (Placed, Processing, Delivered, Cancelled, etc.)
    - Completed/captured Razorpay orders (even if subsequently cancelled/refunded).
    """
    from orders.models import Order

    qs = base_qs if base_qs is not None else Order.objects.all()
    return qs.exclude(order_status="AwaitingPayment").exclude(
        Q(payment__isnull=False) &
        Q(payment__paid_at__isnull=True) &
        Q(payment__stock_reduced=False) &
        ~Q(payment__status="Completed")
    )


def transition_order_status(order_or_id, new_status: str, description: str = "", update_delivered_at: bool = False):
    """
    Central transactional service to update order status.
    Uses transaction.atomic() and select_for_update() with terminal-state and duplicate-history protection.
    """
    from orders.models import Order

    if new_status not in FULFILMENT_STATUSES:
        raise ValueError(f"Invalid fulfilment status: {new_status}")

    with transaction.atomic():
        if isinstance(order_or_id, str):
            order = Order.objects.select_for_update().filter(_id=order_or_id).first()
        else:
            order = Order.objects.select_for_update().filter(_id=order_or_id._id).first()

        if not order:
            raise ValueError(f"Order not found: {order_or_id}")

        current_status = order.order_status

        # If current status is terminal and attempting to change to a different status, disallow
        if current_status in TERMINAL_STATUSES and current_status != new_status:
            logger.warning(f"Cannot transition order {order._id} from terminal status {current_status} to {new_status}")
            return order

        order.order_status = new_status

        # Update status_history list
        history = list(order.status_history or [])
        now_iso = timezone.now().isoformat()
        desc = description or STATUS_DESCRIPTIONS.get(new_status, f"Order status updated to {new_status}")

        # Avoid consecutive duplicate status entries in history
        if not history or history[-1].get("status") != new_status:
            history.append({
                "status": new_status,
                "date": now_iso,
                "description": desc,
            })
            order.status_history = history

        if new_status == "Delivered" or update_delivered_at:
            if not order.delivered_at:
                order.delivered_at = timezone.now()

        order.save(update_fields=["order_status", "status_history", "delivered_at", "updated_at"])
        return order


def reduce_stock_once(payment, order):
    """
    Safely decrements product stock and increments num_sold exactly once
    inside an atomic transaction upon confirmed captured payment.
    """
    if not payment.stock_reduced and order:
        for item in (order.order_items or []):
            pid = item.get("product")
            qty = item.get("quantity", 1)
            if pid:
                Product.objects.filter(_id=pid).update(
                    stock=F("stock") - qty, num_sold=F("num_sold") + qty
                )
        Product.objects.filter(stock__lt=0).update(stock=0)
        payment.stock_reduced = True
        payment.save(update_fields=["stock_reduced", "updated_at"])


def complete_payment_once(payment, order, rzp_payment_id: str = "", rzp_signature: str = "", paid_at=None):
    """
    Single atomic service that completes payment and order fulfilment in ONE transaction:
    - Payment = Completed
    - Order = Placed
    - PaymentAttempt = Completed
    - Status history appended
    - Stock reduction exactly once
    - Order.payment_info updated
    """
    from payments.models import PaymentAttempt

    with transaction.atomic():
        now = paid_at or timezone.now()

        # 1. Update Payment aggregate
        payment.status = "Completed"
        if rzp_payment_id:
            payment.razorpay_payment_id = rzp_payment_id
        if rzp_signature:
            payment.razorpay_signature = rzp_signature
        payment.paid_at = now
        payment.error_code = ""
        payment.error_description = ""
        payment.save()

        # 2. Update/Create PaymentAttempt
        if rzp_payment_id:
            attempt = PaymentAttempt.objects.filter(razorpay_payment_id=rzp_payment_id).first()
            if attempt:
                attempt.status = "Completed"
                attempt.save(update_fields=["status", "updated_at"])
            else:
                PaymentAttempt.objects.create(
                    payment=payment,
                    razorpay_payment_id=rzp_payment_id,
                    razorpay_order_id=payment.razorpay_order_id or "",
                    status="Completed",
                    amount_paise=payment.amount_paise,
                    currency=payment.currency,
                )

        # 3. Transition Order to Placed
        transition_order_status(
            order,
            new_status="Placed",
            description="Order has been placed successfully after payment capture",
        )

        # 4. Reduce stock once
        reduce_stock_once(payment, order)

        # 5. Update Order payment_info snapshot for compatibility
        order.payment_info = {
            "method": "Razorpay",
            "status": "Completed",
            "razorpayOrderId": payment.razorpay_order_id or "",
            "razorpayPaymentId": payment.razorpay_payment_id or "",
            "paidAt": now.isoformat(),
        }
        order.save(update_fields=["payment_info", "updated_at"])

        return payment, order
