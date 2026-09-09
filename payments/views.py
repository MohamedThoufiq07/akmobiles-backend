import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal

from django.conf import settings
from django.db import transaction, IntegrityError
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from common.pricing import calculate_order_pricing
from orders.models import Order
from orders.services import complete_payment_once, transition_order_status
from products.models import Product
from .models import Payment, PaymentAttempt, RazorpayWebhookEvent

logger = logging.getLogger(__name__)


def _razorpay_client():
    import razorpay
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def _resolve_payment_for_rzp_order(rzp_order_id):
    """
    Locates the internal Payment record using current or historical Razorpay order IDs.
    """
    if not rzp_order_id:
        return None
    payment = Payment.objects.filter(razorpay_order_id=rzp_order_id).first()
    if payment:
        return payment
    attempt = PaymentAttempt.objects.filter(razorpay_order_id=rzp_order_id).select_related("payment").first()
    if attempt:
        return attempt.payment
    return None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_razorpay_order(request):
    """
    POST /api/payments/razorpay/create-order/
    Creates or reuses an active Razorpay order for an authenticated internal order.
    Prices, taxes, discounts, and shipping are server-recalculated and never trusted from the client.
    Supports retry payments while preserving historical attempts.
    """
    order_id = request.data.get("orderId") or request.data.get("order_id")
    if not order_id:
        return Response({"success": False, "message": "orderId is required."}, status=400)

    # 1. Read-only validation outside db lock
    order = Order.objects.filter(_id=order_id).first()
    if not order:
        return Response({"success": False, "message": "Order not found."}, status=404)

    if order.user_id != request.user._id and request.user.role != "admin":
        return Response({"success": False, "message": "Not authorized to pay for this order."}, status=403)

    if order.order_status in ["Delivered", "Cancelled", "Returned"]:
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

    payment = Payment.objects.filter(order=order).first()
    if payment and payment.status == "Completed":
        return Response({"success": False, "message": "Order is already paid."}, status=400)

    # Idempotency / rapid double-click protection:
    # If an active Razorpay order ID exists on the pending payment, reuse it
    if payment and payment.razorpay_order_id and payment.status == "Pending":
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

    # 2. Call Razorpay API outside database lock
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

    rzp_order_id = rzp_order["id"]

    # 3. Open atomic transaction to save Payment and PaymentAttempt
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().filter(_id=order._id).first()
        payment, _ = Payment.objects.select_for_update().get_or_create(
            order=locked_order,
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

        # Store historical attempt
        PaymentAttempt.objects.create(
            payment=payment,
            razorpay_order_id=rzp_order_id,
            status="Pending",
            amount_paise=amount_paise,
            currency="INR",
        )

        payment.razorpay_order_id = rzp_order_id
        payment.receipt = receipt
        payment.amount = pricing["total_price_dec"]
        payment.amount_paise = amount_paise
        payment.status = "Pending"
        payment.save()

        locked_order.payment_info = {
            "method": "Razorpay",
            "status": "Pending",
            "razorpayOrderId": rzp_order_id,
            "amount": pricing["total_price"],
            "currency": "INR",
        }
        locked_order.save(update_fields=["payment_info", "updated_at"])

    return Response({
        "success": True,
        "key": settings.RAZORPAY_KEY_ID,
        "orderId": rzp_order_id,
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
    Uses complete_payment_once() to atomically update all order and payment states.
    """
    order_id = request.data.get("orderId") or request.data.get("order_id")
    rzp_order_id = request.data.get("razorpay_order_id")
    rzp_payment_id = request.data.get("razorpay_payment_id")
    rzp_signature = request.data.get("razorpay_signature")

    if not all([order_id, rzp_order_id, rzp_payment_id, rzp_signature]):
        return Response({"success": False, "message": "Missing required verification fields."}, status=400)

    # 1. Read-only validation
    payment = Payment.objects.select_related("order").filter(order___id=order_id).first()
    if not payment:
        return Response({"success": False, "message": "Payment record not found for this order."}, status=404)

    if payment.user_id != request.user._id and request.user.role != "admin":
        return Response({"success": False, "message": "Not authorized."}, status=403)

    if payment.status == "Completed":
        return Response({
            "success": True,
            "status": "captured",
            "message": "Payment already verified successfully.",
            "orderId": order_id,
        }, status=200)

    # Verify HMAC-SHA256 signature
    body = f"{rzp_order_id}|{rzp_payment_id}".encode("utf-8")
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

    # 2. Call Razorpay API outside database lock
    try:
        client = _razorpay_client()
        rzp_payment = client.payment.fetch(rzp_payment_id)
    except Exception as err:
        logger.exception("Failed to fetch Razorpay payment %s: %s", rzp_payment_id, err)
        return Response({"success": False, "message": "Unable to verify payment status with gateway."}, status=502)

    if rzp_payment.get("order_id") != rzp_order_id:
        return Response({"success": False, "message": "Payment order ID mismatch."}, status=400)

    if int(rzp_payment.get("amount", 0)) != payment.amount_paise:
        return Response({"success": False, "message": "Payment amount mismatch."}, status=400)

    if rzp_payment.get("currency") != payment.currency:
        return Response({"success": False, "message": "Payment currency mismatch."}, status=400)

    rzp_status = rzp_payment.get("status")

    # 3. Apply verified state in atomic transaction
    with transaction.atomic():
        locked_payment = Payment.objects.select_for_update().select_related("order").filter(_id=payment._id).first()
        locked_order = Order.objects.select_for_update().filter(_id=locked_payment.order_id).first()

        if locked_payment.status == "Completed":
            return Response({"success": True, "status": "captured", "message": "Payment already completed.", "orderId": locked_order._id})

        if rzp_status == "captured":
            complete_payment_once(
                locked_payment,
                locked_order,
                rzp_payment_id=rzp_payment_id,
                rzp_signature=rzp_signature,
            )
            return Response({
                "success": True,
                "status": "captured",
                "message": "Payment verified successfully.",
                "orderId": locked_order._id,
            })

        elif rzp_status in ["authorized", "processing"]:
            locked_payment.status = "Authorized" if rzp_status == "authorized" else "Processing"
            locked_payment.razorpay_payment_id = rzp_payment_id
            locked_payment.save(update_fields=["status", "razorpay_payment_id", "updated_at"])

            PaymentAttempt.objects.create(
                payment=locked_payment,
                razorpay_payment_id=rzp_payment_id,
                razorpay_order_id=rzp_order_id,
                status=locked_payment.status,
                amount_paise=locked_payment.amount_paise,
                currency=locked_payment.currency,
            )

            return Response({
                "success": True,
                "status": rzp_status,
                "message": f"Payment {rzp_status}, awaiting capture.",
                "orderId": locked_order._id,
            })

        else:
            PaymentAttempt.objects.create(
                payment=locked_payment,
                razorpay_payment_id=rzp_payment_id,
                razorpay_order_id=rzp_order_id,
                status="Failed",
                amount_paise=locked_payment.amount_paise,
                currency=locked_payment.currency,
                error_code="NOT_CAPTURED",
                error_description=f"Payment status is {rzp_status}.",
            )
            return Response({"success": False, "message": f"Payment is not captured (status: {rzp_status})."}, status=400)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def checkout_dismissed(request):
    """
    POST /api/payments/razorpay/checkout-dismissed/
    Handles checkout dismissal safely by querying the gateway for true payment state.
    Calls Razorpay API outside locks; updates database inside atomic transaction.
    """
    order_id = request.data.get("internalOrderId") or request.data.get("orderId") or request.data.get("order_id")
    if not order_id:
        return Response({"success": False, "message": "orderId is required."}, status=400)

    # 1. Read-only query
    order = Order.objects.filter(_id=order_id).first()
    if not order:
        return Response({"success": False, "message": "Order not found."}, status=404)

    if order.user_id != request.user._id and request.user.role != "admin":
        return Response({"success": False, "message": "Not authorized."}, status=403)

    payment = Payment.objects.filter(order=order).first()
    if payment and payment.status == "Completed":
        return Response({"success": True, "status": "completed", "message": "Payment is already completed."}, status=200)

    rzp_order_id = request.data.get("razorpayOrderId") or request.data.get("razorpay_order_id") or (payment.razorpay_order_id if payment else None)
    rzp_payments = []
    api_failed = False

    # 2. Call Razorpay API outside lock
    if rzp_order_id:
        try:
            client = _razorpay_client()
            rzp_payments_res = client.order.payments(rzp_order_id)
            rzp_payments = rzp_payments_res.get("items", []) if isinstance(rzp_payments_res, dict) else []
        except Exception as err:
            logger.warning("Failed to query Razorpay payments for order %s: %s", rzp_order_id, err)
            api_failed = True

    if api_failed:
        # If gateway API fails or times out, do NOT mark Cancelled based on API failure
        return Response({
            "success": True,
            "status": "pending",
            "message": "Gateway check temporarily unavailable. Order remains awaiting payment.",
            "orderId": order._id,
        }, status=200)

    # 3. Check for any captured / authorized attempt on gateway
    captured_attempt = next((p for p in rzp_payments if p.get("status") == "captured"), None)
    auth_attempt = next((p for p in rzp_payments if p.get("status") in ["authorized", "processing"]), None)

    # 4. Open atomic transaction to apply verified result
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().filter(_id=order._id).first()
        locked_payment = Payment.objects.select_for_update().filter(order=locked_order).first() if payment else None

        if locked_payment and locked_payment.status == "Completed":
            return Response({"success": True, "status": "completed", "message": "Payment is already completed."}, status=200)

        if captured_attempt:
            # Race condition: payment was captured on gateway right as modal was dismissed
            complete_payment_once(
                locked_payment,
                locked_order,
                rzp_payment_id=captured_attempt.get("id", ""),
            )
            return Response({
                "success": True,
                "status": "captured",
                "message": "Payment captured successfully.",
                "orderId": locked_order._id,
            })

        elif auth_attempt:
            new_status = "Authorized" if auth_attempt.get("status") == "authorized" else "Processing"
            if locked_payment:
                locked_payment.status = new_status
                locked_payment.razorpay_payment_id = auth_attempt.get("id", "")
                locked_payment.save(update_fields=["status", "razorpay_payment_id", "updated_at"])
            return Response({
                "success": True,
                "status": auth_attempt.get("status"),
                "message": "Payment authorized on gateway.",
                "orderId": locked_order._id,
            })

        else:
            # Payment was genuinely cancelled / dismissed without capture
            if locked_payment and locked_payment.status != "Completed":
                locked_payment.status = "Cancelled"
                locked_payment.error_code = "CUSTOMER_CANCELLED"
                locked_payment.error_description = "Customer dismissed checkout modal."
                locked_payment.save(update_fields=["status", "error_code", "error_description", "updated_at"])

                # Idempotent PaymentAttempt without duplicating rows
                if rzp_order_id and not PaymentAttempt.objects.filter(
                    payment=locked_payment,
                    razorpay_order_id=rzp_order_id,
                    status="Cancelled",
                ).exists():
                    PaymentAttempt.objects.create(
                        payment=locked_payment,
                        razorpay_order_id=rzp_order_id,
                        status="Cancelled",
                        amount_paise=locked_payment.amount_paise,
                        currency=locked_payment.currency,
                        error_code="CUSTOMER_CANCELLED",
                        error_description="Customer dismissed checkout modal.",
                    )

            if locked_order.order_status != "AwaitingPayment" and locked_order.order_status not in ["Delivered", "Cancelled"]:
                locked_order.order_status = "AwaitingPayment"
                locked_order.save(update_fields=["order_status", "updated_at"])

            return Response({
                "success": True,
                "status": "cancelled",
                "message": "Payment cancelled. Your order has not been confirmed.",
                "orderId": locked_order._id,
            })


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def razorpay_webhook(request):
    """
    POST /api/payments/razorpay/webhook/
    Public webhook receiver with header signature verification, duplicate deduplication,
    and out-of-order protection. Uses complete_payment_once() for captured orders.
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

        payment = _resolve_payment_for_rzp_order(rzp_order_id)
        if payment:
            webhook_event.payment = payment
            webhook_event.save(update_fields=["payment"])

            with transaction.atomic():
                locked_payment = Payment.objects.select_for_update().select_related("order").filter(_id=payment._id).first()
                locked_order = Order.objects.select_for_update().filter(_id=locked_payment.order_id).first()

                if locked_payment.status != "Completed":
                    if rzp_amount and int(rzp_amount) != locked_payment.amount_paise:
                        logger.warning("Webhook amount mismatch for order %s", rzp_order_id)
                    elif rzp_currency and rzp_currency != locked_payment.currency:
                        logger.warning("Webhook currency mismatch for order %s", rzp_order_id)
                    else:
                        complete_payment_once(
                            locked_payment,
                            locked_order,
                            rzp_payment_id=rzp_payment_id or locked_payment.razorpay_payment_id or "",
                        )

    elif event_type == "payment.failed":
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        rzp_order_id = entity.get("order_id")
        rzp_payment_id = entity.get("id")
        error_code = entity.get("error_code", "")
        error_desc = entity.get("error_description", "Payment failed")

        payment = _resolve_payment_for_rzp_order(rzp_order_id)
        if payment:
            webhook_event.payment = payment
            webhook_event.save(update_fields=["payment"])

            with transaction.atomic():
                locked_payment = Payment.objects.select_for_update().filter(_id=payment._id).first()

                if rzp_payment_id:
                    PaymentAttempt.objects.get_or_create(
                        razorpay_payment_id=rzp_payment_id,
                        defaults={
                            "payment": locked_payment,
                            "razorpay_order_id": rzp_order_id or "",
                            "status": "Failed",
                            "amount_paise": locked_payment.amount_paise,
                            "currency": locked_payment.currency,
                            "error_code": error_code,
                            "error_description": error_desc,
                        },
                    )

                # Derive aggregate status safely from all attempts (Completed is terminal)
                if locked_payment.status != "Completed":
                    attempts = PaymentAttempt.objects.filter(payment=locked_payment)
                    has_auth = attempts.filter(status__in=["Authorized", "Processing"]).exists()
                    has_pending = attempts.filter(status="Pending").exists()

                    if has_auth:
                        locked_payment.status = "Authorized"
                    elif has_pending:
                        locked_payment.status = "Pending"
                    else:
                        locked_payment.status = "Failed"
                        locked_payment.error_code = error_code
                        locked_payment.error_description = error_desc

                    locked_payment.save(update_fields=["status", "error_code", "error_description", "updated_at"])

    return Response({"status": "ok", "message": f"Webhook {event_type} processed."}, status=200)


@api_view(["POST", "GET"])
@permission_classes([AllowAny])
def expire_stale_payments(request):
    """
    POST/GET /api/payments/razorpay/expire-stale/
    Invoked by Vercel Cron or scheduled runners with Bearer CRON_SECRET.
    Expires pending online orders older than 30 minutes if no payment captured on Gateway.
    """
    import os
    from datetime import timedelta

    auth_header = request.headers.get("Authorization") or request.META.get("HTTP_AUTHORIZATION", "")
    expected_secret = getattr(settings, "CRON_SECRET", None) or os.environ.get("CRON_SECRET", "")

    if not expected_secret or auth_header != f"Bearer {expected_secret}":
        return Response({"success": False, "message": "Unauthorized cron request."}, status=401)

    threshold = timezone.now() - timedelta(minutes=30)
    stale_payments = Payment.objects.filter(status="Pending", created_at__lt=threshold)
    expired_count = 0

    for payment in stale_payments:
        rzp_payments = []
        if payment.razorpay_order_id:
            try:
                client = _razorpay_client()
                res = client.order.payments(payment.razorpay_order_id)
                rzp_payments = res.get("items", []) if isinstance(res, dict) else []
            except Exception:
                continue

        captured = any(p.get("status") == "captured" for p in rzp_payments)
        if not captured:
            with transaction.atomic():
                locked_payment = Payment.objects.select_for_update().filter(_id=payment._id, status="Pending").first()
                if locked_payment:
                    locked_payment.status = "Expired"
                    locked_payment.save(update_fields=["status", "updated_at"])
                    expired_count += 1

    return Response({"success": True, "expiredCount": expired_count, "timestamp": timezone.now().isoformat()})
