"""
Payment views — port of controllers/paymentController.js.

Endpoints (under /api/payment/):
  GET   key            getRazorpayKey      (public)
  POST  create-order   createRazorpayOrder (auth)
  POST  verify         verifyPayment       (auth)

If Razorpay keys aren't configured, falls back to a demo order/verify, exactly
like the Node version.
"""

import hashlib
import hmac
import logging
import time

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

logger = logging.getLogger(__name__)


def _razorpay_client():
    import razorpay  # imported lazily so the project runs without the SDK installed
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


@api_view(["GET"])
@permission_classes([AllowAny])
def razorpay_key(request):
    return Response({"success": True, "key": settings.RAZORPAY_KEY_ID})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_order(request):
    amount = request.data.get("amount", 0)
    amount_paise = round(float(amount) * 100)  # Razorpay expects paise
    try:
        if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
            raise RuntimeError("keys not configured")
        client = _razorpay_client()
        # razorpay's Client sets `.order` dynamically at init, so the static
        # checker can't see it — silence the false "no attribute" report.
        order = client.order.create({  # type: ignore
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"receipt_{int(time.time() * 1000)}",
        })
        return Response({"success": True, "order": order, "key": settings.RAZORPAY_KEY_ID})
    except Exception:
        # Demo fallback — identical to the Node controller's catch branch.
        # Log the real cause: a silent fallback here returns an "order_demo_" id
        # WITH the real key, which the live Razorpay modal then rejects with
        # "Oops! Something went wrong". Logging surfaces the actual failure
        # (bad keys, missing SDK dep, network) instead of hiding it.
        logger.exception("Razorpay create-order failed; returning demo fallback order")
        return Response({
            "success": True,
            "order": {
                "id": f"order_demo_{int(time.time() * 1000)}",
                "amount": amount_paise,
                "currency": "INR",
                "status": "created",
            },
            "key": settings.RAZORPAY_KEY_ID,
            "demo": True,
        })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify_payment(request):
    order_id = request.data.get("razorpay_order_id", "")
    payment_id = request.data.get("razorpay_payment_id", "")
    signature = request.data.get("razorpay_signature", "")

    if order_id.startswith("order_demo_"):
        return Response({"success": True, "message": "Payment verified (demo mode)"})

    body = f"{order_id}|{payment_id}".encode()
    # RAZORPAY_KEY_SECRET is always a str (os.getenv(..., "")); the checker
    # widens Django settings attrs to Optional, hence the spurious None warning.
    secret = settings.RAZORPAY_KEY_SECRET or ""
    expected = hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()

    if hmac.compare_digest(expected, signature):
        return Response({"success": True, "message": "Payment verified successfully"})
    return Response({"success": False, "message": "Payment verification failed"}, status=400)
