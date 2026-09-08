import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal

from django.conf import settings
from django.db import transaction, IntegrityError
from django.db.models import F
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from common.pricing import calculate_order_pricing
from orders.models import Order
from products.models import Product
from .models import Payment, PaymentAttempt, RazorpayWebhookEvent

logger = logging.getLogger(__name__)


def _razorpay_client():
    import razorpay
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def _reduce_stock_once(payment, order):
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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_razorpay_order(request):
    """
    POST /api/payments/razorpay/create-order/
    Creates or reuses an active Razorpay order for an authenticated internal order.
    Prices, taxes, discounts, and shipping are server-recalculated and never trusted from the client.
    """
    order_id = request.data.get("orderId") or request.data.get("order_id")
    if not order_id:
        return Response({"success": False, "message": "orderId is required."}, status=400)

    with transaction.atomic():
        order = Order.objects.select_for_update().filter(_id=order_id).first()
        if not order:
            return Response({"success": False, "message": "Order not found."}, status=404)

        if order.user_id != request.user._id and request.user.role != "admin":
            return Response({"success": False, "message": "Not authorized to pay for this order."}, status=403)

        if order.order_status in ["Delivered", "Cancelled"]:
            return Response({"success": False, "message": f"Cannot initiate payment for {order.order_status.lower()} order."}, status=400)

        # Server-authoritative recalculation
        pricing = calculate_order_pricing(order.order_items or [])
        if not pricing["items"]:
            return Response({"success": False, "message": "Order has no valid items."}, status=400)

        # Recheck current stock
        for item in pricing["items"]:
            pid, qty = item["product"], item["quantity"]
            prod = Product.objects.filter(_id=pid).first()
            if not prod or prod.stock < qty:
                prod_name = prod.name if prod else "Product"
                return Response({"success": False, "message": f"Insufficient stock for {prod_name}."}, status=400)

        amount_paise = pricing["amount_paise"]
        if amount_paise < 100:
            return Response({"success": False, "message": "Payable amount must be at least ₹1.00 (100 paise)."}, status=400)

        # Retrieve or create Payment aggregate record
        payment, _ = Payment.objects.select_for_update().get_or_create(
            order=order,
            defaults={
                "user": request.user,
                "amount": pricing["total_price_dec"],
                "amount_paise": amount_paise,
                "currency": "INR",
                "status": "Pending",
            },
        )

        if payment.status == "Completed":
            return Response({"success": False, "message": "Order is already paid."}, status=400)

        # Update latest calculated amount
        payment.amount = pricing["total_price_dec"]
        payment.amount_paise = amount_paise

        # Idempotency / rapid double-click protection:
        # If an active Razorpay order ID exists on the pending payment, reuse it
        if payment.razorpay_order_id and payment.status == "Pending":
            return Response({
                "success": True,
                "key": settings.RAZORPAY_KEY_ID,
                "orderId": payment.razorpay_order_id,
                "amount": payment.amount_paise,
                "currency": payment.currency,
                "internalOrderId": order._id,
                "name": "AK Mobiles",
                "description": f"Order #{order._id}",
            })

        if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
            return Response({"success": False, "message": "Payment gateway credentials are not configured."}, status=500)

        receipt = f"rcpt_{order._id[:14]}_{int(time.time())}"
        try:
            client = _razorpay_client()
            rzp_order = client.order.create({
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
            })
        except Exception as err:
            logger.exception("Razorpay SDK order creation failed: %s", err)
            return Response({"success": False, "message": "Failed to create payment order with gateway."}, status=502)

        payment.razorpay_order_id = rzp_order["id"]
        payment.receipt = receipt
        payment.status = "Pending"
        payment.save()

        order.payment_info = {
            "method": "Razorpay",
            "status": "Pending",
            "razorpayOrderId": rzp_order["id"],
            "amount": pricing["total_price"],
            "currency": "INR",
        }
        order.save(update_fields=["payment_info", "updated_at"])

        return Response({
            "success": True,
            "key": settings.RAZORPAY_KEY_ID,
            "orderId": rzp_order["id"],
            "amount": amount_paise,
            "currency": "INR",
            "internalOrderId": order._id,
            "name": "AK Mobiles",
            "description": f"Order #{order._id}",
        })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify_payment(request):
    """
    POST /api/payments/razorpay/verify-payment/
    Verifies the checkout HMAC signature and confirms payment capture status.
    """
    order_id = request.data.get("orderId") or request.data.get("order_id")
    rzp_order_id = request.data.get("razorpay_order_id")
    rzp_payment_id = request.data.get("razorpay_payment_id")
    rzp_signature = request.data.get("razorpay_signature")

    if not all([order_id, rzp_order_id, rzp_payment_id, rzp_signature]):
        return Response({"success": False, "message": "Missing required verification fields."}, status=400)

    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update()
            .select_related("order")
            .filter(order___id=order_id)
            .first()
        )
        if not payment:
            return Response({"success": False, "message": "Payment record not found for this order."}, status=404)

        if payment.user_id != request.user._id and request.user.role != "admin":
            return Response({"success": False, "message": "Not authorized."}, status=403)

        # Idempotent return if already completed
        if payment.status == "Completed":
            return Response({
                "success": True,
                "status": "captured",
                "message": "Payment already verified successfully.",
                "orderId": order_id,
            }, status=200)

        # Confirm Razorpay order ID matches database record
        if rzp_order_id != payment.razorpay_order_id:
            return Response({"success": False, "message": "Razorpay order ID mismatch."}, status=400)

        # Verify HMAC-SHA256 signature using constant-time comparison
        body = f"{payment.razorpay_order_id}|{rzp_payment_id}".encode("utf-8")
        secret = settings.RAZORPAY_KEY_SECRET or ""
        expected_sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected_sig, rzp_signature):
            PaymentAttempt.objects.create(
                payment=payment,
                razorpay_payment_id=rzp_payment_id,
                razorpay_order_id=rzp_order_id,
                status="Failed",
                amount_paise=payment.amount_paise,
                currency=payment.currency,
                error_code="SIGNATURE_MISMATCH",
                error_description="Signature verification failed.",
            )
            return Response({"success": False, "message": "Payment signature verification failed."}, status=400)

        # Confirm payment uniqueness
        if Payment.objects.exclude(_id=payment._id).filter(razorpay_payment_id=rzp_payment_id).exists():
            return Response({"success": False, "message": "Payment ID already utilized by another order."}, status=400)

        # Fetch payment details from Razorpay SDK to confirm capture status, amount, and currency
        try:
            client = _razorpay_client()
            rzp_payment = client.payment.fetch(rzp_payment_id)
        except Exception as err:
            logger.exception("Failed to fetch Razorpay payment %s: %s", rzp_payment_id, err)
            return Response({"success": False, "message": "Unable to verify payment status with gateway."}, status=502)

        if rzp_payment.get("order_id") != payment.razorpay_order_id:
            return Response({"success": False, "message": "Payment order ID mismatch."}, status=400)

        if int(rzp_payment.get("amount", 0)) != payment.amount_paise:
            return Response({"success": False, "message": "Payment amount mismatch."}, status=400)

        if rzp_payment.get("currency") != payment.currency:
            return Response({"success": False, "message": "Payment currency mismatch."}, status=400)

        rzp_status = rzp_payment.get("status")

        if rzp_status == "captured":
            payment.status = "Completed"
            payment.razorpay_payment_id = rzp_payment_id
            payment.razorpay_signature = rzp_signature
            payment.paid_at = timezone.now()
            payment.save()

            PaymentAttempt.objects.create(
                payment=payment,
                razorpay_payment_id=rzp_payment_id,
                razorpay_order_id=rzp_order_id,
                status="Completed",
                amount_paise=payment.amount_paise,
                currency=payment.currency,
            )

            # Reduce stock exactly once
            _reduce_stock_once(payment, payment.order)

            # Update Order payment info and history
            order = payment.order
            order.payment_info = {
                "method": "Razorpay",
                "status": "Completed",
                "razorpayOrderId": payment.razorpay_order_id,
                "razorpayPaymentId": rzp_payment_id,
                "paidAt": payment.paid_at.isoformat(),
            }
            order.save(update_fields=["payment_info", "updated_at"])

            return Response({
                "success": True,
                "status": "captured",
                "message": "Payment verified successfully.",
                "orderId": order._id,
            })

        elif rzp_status in ["authorized", "created"]:
            payment.status = "Authorized"
            payment.razorpay_payment_id = rzp_payment_id
            payment.save(update_fields=["status", "razorpay_payment_id", "updated_at"])

            PaymentAttempt.objects.create(
                payment=payment,
                razorpay_payment_id=rzp_payment_id,
                razorpay_order_id=rzp_order_id,
                status="Authorized",
                amount_paise=payment.amount_paise,
                currency=payment.currency,
            )

            return Response({
                "success": True,
                "status": "authorized",
                "message": "Payment authorized, awaiting capture.",
                "orderId": payment.order_id,
            })

        else:
            PaymentAttempt.objects.create(
                payment=payment,
                razorpay_payment_id=rzp_payment_id,
                razorpay_order_id=rzp_order_id,
                status="Failed",
                amount_paise=payment.amount_paise,
                currency=payment.currency,
                error_code="NOT_CAPTURED",
                error_description=f"Payment status is {rzp_status}.",
            )
            return Response({"success": False, "message": f"Payment is not captured (status: {rzp_status})."}, status=400)


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def razorpay_webhook(request):
    """
    POST /api/payments/razorpay/webhook/
    Public webhook receiver with header signature verification and durable duplicate protection.
    """
    event_id = request.headers.get("x-razorpay-event-id") or request.META.get("HTTP_X_RAZORPAY_EVENT_ID")
    if not event_id:
        return Response({"error": "Missing X-Razorpay-Event-Id header."}, status=400)

    raw_body = request.body
    signature = request.headers.get("x-razorpay-signature") or request.META.get("HTTP_X_RAZORPAY_SIGNATURE")
    webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET

    if not signature or not webhook_secret:
        return Response({"error": "Missing webhook signature or webhook secret not configured."}, status=400)

    expected_sig = hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, signature):
        return Response({"error": "Invalid webhook signature."}, status=400)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return Response({"error": "Invalid JSON payload."}, status=400)

    event_type = payload.get("event", "")

    # Durable duplicate event deduplication using database unique constraint
    try:
        with transaction.atomic():
            webhook_event = RazorpayWebhookEvent.objects.create(
                event_id=event_id,
                event_type=event_type,
            )
    except IntegrityError:
        # Duplicate event received, ignore and return 200 OK
        return Response({"status": "ok", "message": "Duplicate event already processed."}, status=200)

    # Process events
    if event_type in ["payment.captured", "order.paid"]:
        entity = (
            payload.get("payload", {}).get("payment", {}).get("entity", {})
            if event_type == "payment.captured"
            else payload.get("payload", {}).get("order", {}).get("entity", {})
        )
        rzp_order_id = entity.get("order_id") or (entity.get("id") if event_type == "order.paid" else None)
        rzp_payment_id = entity.get("id") if event_type == "payment.captured" else None
        rzp_amount = entity.get("amount") or entity.get("amount_paid")
        rzp_currency = entity.get("currency", "INR")

        if rzp_order_id:
            with transaction.atomic():
                payment = (
                    Payment.objects.select_for_update()
                    .select_related("order")
                    .filter(razorpay_order_id=rzp_order_id)
                    .first()
                )
                if payment:
                    webhook_event.payment = payment
                    webhook_event.save(update_fields=["payment"])

                    # Complete order if not already completed
                    if payment.status != "Completed":
                        if rzp_amount and int(rzp_amount) != payment.amount_paise:
                            logger.warning("Webhook amount mismatch for order %s", rzp_order_id)
                        elif rzp_currency and rzp_currency != payment.currency:
                            logger.warning("Webhook currency mismatch for order %s", rzp_order_id)
                        else:
                            payment.status = "Completed"
                            if rzp_payment_id:
                                payment.razorpay_payment_id = rzp_payment_id
                            payment.paid_at = timezone.now()
                            payment.save()

                            if rzp_payment_id:
                                PaymentAttempt.objects.get_or_create(
                                    razorpay_payment_id=rzp_payment_id,
                                    defaults={
                                        "payment": payment,
                                        "razorpay_order_id": rzp_order_id,
                                        "status": "Completed",
                                        "amount_paise": payment.amount_paise,
                                        "currency": payment.currency,
                                    },
                                )

                            _reduce_stock_once(payment, payment.order)

                            order = payment.order
                            order.payment_info = {
                                "method": "Razorpay",
                                "status": "Completed",
                                "razorpayOrderId": payment.razorpay_order_id,
                                "razorpayPaymentId": payment.razorpay_payment_id,
                                "paidAt": payment.paid_at.isoformat(),
                            }
                            order.save(update_fields=["payment_info", "updated_at"])

    elif event_type == "payment.failed":
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        rzp_order_id = entity.get("order_id")
        rzp_payment_id = entity.get("id")
        error_code = entity.get("error_code", "")
        error_desc = entity.get("error_description", "Payment failed")

        if rzp_order_id:
            with transaction.atomic():
                payment = (
                    Payment.objects.select_for_update()
                    .filter(razorpay_order_id=rzp_order_id)
                    .first()
                )
                if payment:
                    webhook_event.payment = payment
                    webhook_event.save(update_fields=["payment"])

                    if rzp_payment_id:
                        PaymentAttempt.objects.get_or_create(
                            razorpay_payment_id=rzp_payment_id,
                            defaults={
                                "payment": payment,
                                "razorpay_order_id": rzp_order_id,
                                "status": "Failed",
                                "amount_paise": payment.amount_paise,
                                "currency": payment.currency,
                                "error_code": error_code,
                                "error_description": error_desc,
                            },
                        )

                    # Out-of-order protection: never downgrade a Completed payment
                    if payment.status != "Completed":
                        payment.status = "Failed"
                        payment.error_code = error_code
                        payment.error_description = error_desc
                        payment.save(update_fields=["status", "error_code", "error_description", "updated_at"])

    return Response({"status": "ok", "message": f"Webhook {event_type} processed."}, status=200)
