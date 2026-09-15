"""
Auth views — port of controllers/authController.js + middleware/auth.js.

Endpoints (mounted under /api/auth/):
  POST   register
  POST   login
  GET    profile            (auth)
  PUT    profile            (auth)
  POST   forgot-password
  POST   reset-password
  PUT    wishlist/<id>      (auth)
"""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import AccessToken

from products.models import Product
from .google_services import (
    GoogleAuthException,
    resolve_google_customer,
    verify_google_id_token,
)
from .models import User
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    UserSerializer,
    UserProfileSerializer,
)


def make_token(user):
    """Single access token carrying { id, role } — matches the Node generateToken()."""
    token = AccessToken.for_user(user)
    token["role"] = user.role
    return str(token)


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.save()
    return Response(
        {"success": True, "token": make_token(user), "user": UserSerializer(user).data},
        status=201,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    serializer = LoginSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {"success": False, "message": "Please provide email and password."}, status=400
        )
    email = serializer.validated_data["email"].lower()
    password = serializer.validated_data["password"]

    user = User.objects.filter(email=email).first()
    if not user or not user.check_password(password):
        return Response({"success": False, "message": "Invalid email or password."}, status=401)

    return Response(
        {"success": True, "token": make_token(user), "user": UserSerializer(user).data}
    )


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def profile(request):
    user = request.user
    if request.method == "GET":
        return Response({"success": True, "user": UserProfileSerializer(user).data})

    # PUT — update name / phone / addresses (only if provided, like the Node version)
    data = request.data
    if data.get("name"):
        user.name = data["name"]
    if data.get("phone") is not None:
        user.phone = data["phone"]
    if data.get("addresses") is not None:
        user.addresses = data["addresses"]
    user.save()
    return Response({"success": True, "user": UserSerializer(user).data})


@api_view(["POST"])
@permission_classes([AllowAny])
def forgot_password(request):
    email = (request.data.get("email") or "").lower()
    user = User.objects.filter(email=email).first()
    if not user:
        return Response(
            {"success": False, "message": "No account found with this email."}, status=404
        )

    reset_token = secrets.token_hex(32)
    user.reset_password_token = hashlib.sha256(reset_token.encode()).hexdigest()
    user.reset_password_expire = timezone.now() + timedelta(minutes=30)
    user.save(update_fields=["reset_password_token", "reset_password_expire"])

    payload = {
        "success": True,
        "message": "Password reset instructions sent to your email.",
    }
    # Demo only: expose the token in development, exactly like the Node controller.
    if settings.NODE_ENV == "development":
        payload["resetToken"] = reset_token
    return Response(payload)


@api_view(["POST"])
@permission_classes([AllowAny])
def reset_password(request):
    token = request.data.get("token") or ""
    password = request.data.get("password") or ""
    hashed = hashlib.sha256(token.encode()).hexdigest()

    user = User.objects.filter(
        reset_password_token=hashed,
        reset_password_expire__gt=timezone.now(),
    ).first()
    if not user:
        return Response(
            {"success": False, "message": "Invalid or expired reset token."}, status=400
        )

    user.set_password(password)
    user.reset_password_token = None
    user.reset_password_expire = None
    user.save()

    return Response(
        {"success": True, "message": "Password reset successful.", "token": make_token(user)}
    )


@api_view(["PUT"])
@permission_classes([IsAuthenticated])
def toggle_wishlist(request, product_id):
    user = request.user
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found"}, status=404)

    if user.wishlist.filter(_id=product_id).exists():
        user.wishlist.remove(product)
    else:
        user.wishlist.add(product)

    wishlist_ids = list(user.wishlist.values_list("_id", flat=True))
    return Response({"success": True, "wishlist": wishlist_ids})


import json


class GoogleAuthRateThrottle(ScopedRateThrottle):
    """
    Applies 'google_link_password' (5/min) when 'password' is present in request payload,
    and 'google_auth' (30/min) for normal credential-only verification requests.
    """

    def allow_request(self, request, view):
        if request.method != "POST":
            return True

        has_password = False
        try:
            # First check request.data if already parsed
            if hasattr(request, "data"):
                try:
                    data = request.data
                    if isinstance(data, dict):
                        has_password = bool(data.get("password"))
                except Exception:
                    pass

            # Fallback to underlying request POST or body
            if not has_password:
                raw_req = getattr(request, "_request", request)
                if hasattr(raw_req, "POST") and raw_req.POST:
                    has_password = bool(raw_req.POST.get("password"))
                elif hasattr(raw_req, "body") and raw_req.body:
                    raw_body = raw_req.body
                    if isinstance(raw_body, bytes):
                        raw_body = raw_body.decode("utf-8")
                    body_json = json.loads(raw_body)
                    if isinstance(body_json, dict):
                        has_password = bool(body_json.get("password"))
        except Exception:
            has_password = False

        self.scope = "google_link_password" if has_password else "google_auth"
        self.rate = self.get_rate()
        if self.rate is None:
            return True
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super(ScopedRateThrottle, self).allow_request(request, view)



@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([GoogleAuthRateThrottle])
def google_login(request):
    """
    Canonical endpoint for Google customer authentication: POST /api/accounts/google/
    Verifies the Google ID token, resolves/links customer account atomically,
    and returns standard AK Mobiles JWT token & user payload.
    """
    credential = request.data.get("credential")
    if not credential or not isinstance(credential, str) or not credential.strip():
        return Response(
            {
                "success": False,
                "code": "GOOGLE_CREDENTIAL_REQUIRED",
                "message": "Google credential token is required.",
            },
            status=400,
        )

    password = request.data.get("password")
    if password is not None and not isinstance(password, str):
        password = str(password)

    try:
        payload = verify_google_id_token(credential)
        user = resolve_google_customer(payload, password=password)
        token = make_token(user)
        return Response(
            {
                "success": True,
                "message": "Signed in with Google successfully.",
                "token": token,
                "user": UserSerializer(user).data,
            },
            status=200,
        )
    except GoogleAuthException as exc:
        return Response(
            {
                "success": False,
                "code": exc.code,
                "message": exc.message,
            },
            status=exc.status_code,
        )
    except Exception:
        return Response(
            {
                "success": False,
                "code": "GOOGLE_AUTH_FAILED",
                "message": "Unable to complete Google authentication. Please try again.",
            },
            status=500,
        )
