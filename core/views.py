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
    total_customers = User.objects.filter(role="user").count()
    total_orders = Order.objects.count()
    total_products = Product.objects.count()
    total_revenue = Order.objects.aggregate(s=Sum("total_price"))["s"] or 0

    from orders.serializers import OrderSerializer
    recent = Order.objects.order_by("-created_at")[:5]
    recent_orders = OrderSerializer(recent, many=True, context={"populate_user": True}).data

    status_breakdown = [
        {"_id": r["order_status"], "count": r["count"]}
        for r in Order.objects.values("order_status").annotate(count=Count("_id"))
    ]

    twelve_months = timezone.now() - timedelta(days=365)
    monthly = (
        Order.objects.filter(created_at__gte=twelve_months)
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
    period = request.query_params.get("period", "monthly")
    if period == "daily":
        start = timezone.now() - timedelta(days=30)
        rows = (
            Order.objects.filter(created_at__gte=start)
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
            Order.objects.filter(created_at__gte=start)
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
    file = request.FILES.get("image")
    if not file:
        return Response({"success": False, "message": "No image file provided."}, status=400)
    if not file.content_type.startswith("image/"):
        return Response({"success": False, "message": "Only image files are allowed."}, status=400)
    if file.size > 5 * 1024 * 1024:
        return Response({"success": False, "message": "File too large (max 5MB)."}, status=400)

    # Save via Django's default storage: Cloudinary in prod (returns an absolute
    # https URL), local FileSystemStorage in dev (returns a relative /uploads/...
    # path). Same response contract { success, url } either way.
    ext = os.path.splitext(file.name)[1].lower()
    filename = f"product-{int(time.time() * 1000)}-{random.randint(0, 10**9)}{ext}"
    saved_name = default_storage.save(filename, file)
    url = default_storage.url(saved_name)
    # Dev (local FS) returns a relative path — keep the previous absolute-URL shape.
    if url.startswith("/"):
        url = request.build_absolute_uri(url)
    return Response({"success": True, "url": url}, status=201)
