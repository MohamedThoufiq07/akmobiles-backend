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
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import AccessToken

from products.models import Product
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
