"""
Core views — ports of contactController, newsletterController, settingsController,
adminController, userController, and the upload route.
"""

import os
import time
import random
from datetime import timedelta

from django.core.files.storage import default_storage
from django.db.models import Sum, Count
from django.db.models.functions import ExtractMonth, ExtractYear, ExtractDay
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from common.permissions import IsAdmin, paginate_queryset
from accounts.models import User
from accounts.serializers import UserAdminSerializer
from orders.models import Order
from products.models import Product
from products.serializers import TopProductSerializer
from .models import Contact, Newsletter, Settings
from .serializers import ContactSerializer, SettingsSerializer


# ------------------------------------------------------------------ contact
@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def contact(request):
    if request.method == "POST":
        data = request.data
        if not all(data.get(f) for f in ("name", "email", "subject", "message")):
            return Response({"success": False, "message": "Please fill in all fields."}, status=400)
        Contact.objects.create(
            name=data["name"], email=data["email"],
            subject=data["subject"], message=data["message"],
        )
        return Response(
            {"success": True,
             "message": "Your message has been sent successfully. We will get back to you soon!"},
            status=201,
        )

    # GET -> admin only
    if not (request.user.is_authenticated and request.user.role == "admin"):
        return Response(
            {"success": False, "message": "Access denied. Admin privileges required."}, status=403
        )
    qs = Contact.objects.all()
    items, page, pages, total = paginate_queryset(
        qs, request.query_params.get("page", 1), request.query_params.get("limit", 20)
    )
    return Response({
        "success": True,
        "messages": ContactSerializer(items, many=True).data,
        "page": page, "pages": pages, "total": total,
    })


# ------------------------------------------------------------------ newsletter
@api_view(["POST"])
@permission_classes([AllowAny])
def newsletter_subscribe(request):
    email = (request.data.get("email") or "").lower()
    if not email:
        return Response(
            {"success": False, "message": "Please provide your email address."}, status=400
        )
    if Newsletter.objects.filter(email=email).exists():
        return Response(
            {"success": False, "message": "This email is already subscribed."}, status=400
        )
    Newsletter.objects.create(email=email)
    return Response(
        {"success": True, "message": "Thank you for subscribing to our newsletter!"}, status=201
    )


# ------------------------------------------------------------------ settings
@api_view(["GET", "PUT"])
@permission_classes([AllowAny])
def store_settings(request):
    doc = Settings.get_singleton()
    if request.method == "GET":
        return Response({"success": True, "settings": SettingsSerializer(doc).data})

    if not (request.user.is_authenticated and request.user.role == "admin"):
        return Response(
            {"success": False, "message": "Access denied. Admin privileges required."}, status=403
        )
    data = request.data
    if "flashSaleActive" in data:
        doc.flash_sale_active = data["flashSaleActive"]
    if "flashSaleTitle" in data:
        doc.flash_sale_title = data["flashSaleTitle"]
    if "flashSaleSubtitle" in data:
        doc.flash_sale_subtitle = data["flashSaleSubtitle"]
    if "flashSaleEndsAt" in data:
        doc.flash_sale_ends_at = data["flashSaleEndsAt"] or None
    if "banners" in data:
        doc.banners = data["banners"]
    doc.save()
    return Response({"success": True, "settings": SettingsSerializer(doc).data})


# ------------------------------------------------------------------ users (admin)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def all_users(request):
    qs = User.objects.filter(role="user").order_by("-created_at")
    items, page, pages, total = paginate_queryset(
        qs, request.query_params.get("page", 1), request.query_params.get("limit", 20)
    )
    return Response({
        "success": True,
        "users": UserAdminSerializer(items, many=True).data,
        "page": page, "pages": pages, "total": total,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def user_by_id(request, user_id):
    user = User.objects.filter(_id=user_id).first()
    if not user:
        return Response({"success": False, "message": "User not found"}, status=404)
    return Response({"success": True, "user": UserAdminSerializer(user).data})


# ------------------------------------------------------------------ admin dashboard / reports
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def dashboard_stats(request):
    from orders.services import get_admin_orders_queryset
    from orders.serializers import OrderSerializer

    admin_orders_qs = get_admin_orders_queryset()

    total_customers = User.objects.filter(role="user").count()
    total_orders = admin_orders_qs.count()
    total_products = Product.objects.count()
    total_revenue = admin_orders_qs.aggregate(s=Sum("total_price"))["s"] or 0

    recent = admin_orders_qs.order_by("-created_at")[:5]
    recent_orders = OrderSerializer(recent, many=True, context={"populate_user": True}).data

    status_breakdown = [
        {"_id": r["order_status"], "count": r["count"]}
        for r in admin_orders_qs.values("order_status").annotate(count=Count("_id"))
    ]

    twelve_months = timezone.now() - timedelta(days=365)
    monthly = (
        admin_orders_qs.filter(created_at__gte=twelve_months)
        .annotate(month=ExtractMonth("created_at"), year=ExtractYear("created_at"))
        .values("month", "year")
        .annotate(revenue=Sum("total_price"), orders=Count("_id"))
        .order_by("year", "month")
    )
    monthly_revenue = [
        {"_id": {"month": r["month"], "year": r["year"]}, "revenue": r["revenue"], "orders": r["orders"]}
        for r in monthly
    ]

    return Response({
        "success": True,
        "stats": {
            "totalCustomers": total_customers,
            "totalOrders": total_orders,
            "totalProducts": total_products,
            "totalRevenue": total_revenue,
            "recentOrders": recent_orders,
            "statusBreakdown": status_breakdown,
            "monthlyRevenue": monthly_revenue,
        },
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def sales_report(request):
    from orders.services import get_admin_orders_queryset

    admin_orders_qs = get_admin_orders_queryset()
    period = request.query_params.get("period", "monthly")
    if period == "daily":
        start = timezone.now() - timedelta(days=30)
        rows = (
            admin_orders_qs.filter(created_at__gte=start)
            .annotate(day=ExtractDay("created_at"), month=ExtractMonth("created_at"),
                      year=ExtractYear("created_at"))
            .values("day", "month", "year")
            .annotate(revenue=Sum("total_price"), orders=Count("_id"))
            .order_by("year", "month", "day")
        )
        sales = [
            {"_id": {"day": r["day"], "month": r["month"], "year": r["year"]},
             "revenue": r["revenue"], "orders": r["orders"], "items": 0}
            for r in rows
        ]
    else:
        start = timezone.now() - timedelta(days=365)
        rows = (
            admin_orders_qs.filter(created_at__gte=start)
            .annotate(month=ExtractMonth("created_at"), year=ExtractYear("created_at"))
            .values("month", "year")
            .annotate(revenue=Sum("total_price"), orders=Count("_id"))
            .order_by("year", "month")
        )
        sales = [
            {"_id": {"month": r["month"], "year": r["year"]},
             "revenue": r["revenue"], "orders": r["orders"], "items": 0}
            for r in rows
        ]
    return Response({"success": True, "salesData": sales})


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def top_products(request):
    products = Product.objects.order_by("-num_sold")[:10]
    return Response({"success": True, "products": TopProductSerializer(products, many=True).data})


# ------------------------------------------------------------------ upload (admin)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
@parser_classes([MultiPartParser])
def upload_image(request):
    import uuid
    req_id = uuid.uuid4().hex
    file = request.FILES.get("image") or request.FILES.get("file")
    if not file:
        return Response({
            "code": "INVALID_IMAGE",
            "message": "No image file provided.",
            "file_name": "",
            "request_id": req_id,
        }, status=400)

    from common.storage import (
        validate_and_decode_image,
        put_to_vercel_blob,
        generate_permanent_key,
        StorageValidationError,
        StorageConfigError,
        StorageUpstreamError,
        StorageTimeoutError,
    )
    try:
        val_info = validate_and_decode_image(file)
        canonical_mime = val_info["content_type"]
        canonical_ext = val_info["extension"]
        permanent_key = generate_permanent_key(canonical_ext=canonical_ext)

        file.seek(0)
        verified_bytes = file.read()
        put_result = put_to_vercel_blob(permanent_key, verified_bytes, content_type=canonical_mime)
        url = put_result["url"]
        if url.startswith("/") and request:
            url = request.build_absolute_uri(url)

        return Response({
            "success": True,
            "url": url,
            "storage_key": permanent_key,
            "content_type": canonical_mime,
            "file_size": len(verified_bytes),
            "width": val_info["width"],
            "height": val_info["height"],
        }, status=201)
    except StorageValidationError as val_err:
        return Response({
            "code": val_err.code,
            "message": val_err.message,
            "file_name": getattr(file, "name", ""),
            "request_id": req_id,
        }, status=400)
    except StorageConfigError as cfg_err:
        return Response({
            "code": cfg_err.code,
            "message": cfg_err.message,
            "file_name": getattr(file, "name", ""),
            "request_id": req_id,
        }, status=503)
    except StorageUpstreamError as up_err:
        return Response({
            "code": up_err.code,
            "message": up_err.message,
            "file_name": getattr(file, "name", ""),
            "request_id": req_id,
        }, status=502)
    except StorageTimeoutError as to_err:
        return Response({
            "code": to_err.code,
            "message": to_err.message,
            "file_name": getattr(file, "name", ""),
            "request_id": req_id,
        }, status=504)
    except Exception:
        return Response({
            "code": "INTERNAL_ERROR",
            "message": "Failed to upload image.",
            "file_name": getattr(file, "name", ""),
            "request_id": req_id,
        }, status=500)
