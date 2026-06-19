"""
Order views — port of controllers/orderController.js.

Endpoints (under /api/orders/, all require auth):
  POST   /              createOrder
  GET    /myorders      getMyOrders
  GET    /stats         getOrderStats        (admin)
  GET    /<id>          getOrderById          (owner or admin)
  GET    /              getAllOrders          (admin)
  PUT    /<id>/status   updateOrderStatus     (admin)
"""

from datetime import timedelta

from django.db.models import F, Sum, Count
from django.db.models.functions import ExtractMonth, ExtractYear, ExtractDay
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import paginate_queryset
from products.models import Product
from .models import Order
from .serializers import OrderSerializer

STATUS_DESCRIPTIONS = {
    "Placed": "Order has been placed successfully",
    "Processing": "Order is being processed and packed",
    "Shipped": "Order has been shipped and is on the way",
    "Delivered": "Order has been delivered successfully",
    "Cancelled": "Order has been cancelled",
}


def _is_admin(request):
    return request.user.is_authenticated and request.user.role == "admin"


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def orders_root(request):
    if request.method == "POST":
        return _create_order(request)
    # GET -> admin: list all orders
    if not _is_admin(request):
        return Response(
            {"success": False, "message": "Access denied. Admin privileges required."}, status=403
        )
    q = request.query_params
    qs = Order.objects.all()
    if q.get("status"):
        qs = qs.filter(order_status=q.get("status"))
    items, page, pages, total = paginate_queryset(qs, q.get("page", 1), q.get("limit", 20))
    data = OrderSerializer(items, many=True, context={"populate_user": True}).data
    return Response({"success": True, "orders": data, "page": page, "pages": pages, "total": total})


def _create_order(request):
    data = request.data
    order_items = data.get("orderItems") or []
    if not order_items:
        return Response({"success": False, "message": "No order items provided."}, status=400)

    order = Order.objects.create(
        user=request.user,
        order_items=order_items,
        shipping_address=data.get("shippingAddress") or {},
        payment_info=data.get("paymentInfo") or {},
        items_price=data.get("itemsPrice", 0),
        tax_price=data.get("taxPrice", 0),
        shipping_price=data.get("shippingPrice", 0),
        total_price=data.get("totalPrice", 0),
    )

    # Decrement stock + bump numSold for each product (like the bulkWrite $inc).
    for item in order_items:
        pid, qty = item.get("product"), item.get("quantity", 1)
        if pid:
            Product.objects.filter(_id=pid).update(
                stock=F("stock") - qty, num_sold=F("num_sold") + qty
            )
    Product.objects.filter(stock__lt=0).update(stock=0)  # clamp negatives

    return Response({"success": True, "order": OrderSerializer(order).data}, status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_orders(request):
    qs = Order.objects.filter(user=request.user).order_by("-created_at")
    data = OrderSerializer(qs, many=True, context={"populate_item_fields": ["name", "images"]}).data
    return Response({"success": True, "orders": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_detail(request, order_id):
    order = Order.objects.filter(_id=order_id).first()
    if not order:
        return Response({"success": False, "message": "Order not found"}, status=404)
    if order.user_id != request.user._id and request.user.role != "admin":
        return Response(
            {"success": False, "message": "Not authorized to view this order"}, status=403
        )
    data = OrderSerializer(
        order,
        context={"populate_user": True, "populate_item_fields": ["name", "images", "brand"]},
    ).data
    return Response({"success": True, "order": data})


@api_view(["PUT"])
@permission_classes([IsAuthenticated])
def update_status(request, order_id):
    if not _is_admin(request):
        return Response(
            {"success": False, "message": "Access denied. Admin privileges required."}, status=403
        )
    order = Order.objects.filter(_id=order_id).first()
    if not order:
        return Response({"success": False, "message": "Order not found"}, status=404)

    status = request.data.get("status")
    order.order_status = status
    history = list(order.status_history or [])
    history.append({
        "status": status,
        "date": timezone.now().isoformat(),
        "description": STATUS_DESCRIPTIONS.get(status, f"Order status updated to {status}"),
    })
    order.status_history = history

    if status == "Delivered":
        order.delivered_at = timezone.now()
        payment = dict(order.payment_info or {})
        payment["status"] = "Completed"
        order.payment_info = payment

    order.save()
    return Response({"success": True, "order": OrderSerializer(order).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_stats(request):
    if not _is_admin(request):
        return Response(
            {"success": False, "message": "Access denied. Admin privileges required."}, status=403
        )

    total_orders = Order.objects.count()
    delivered_orders = Order.objects.filter(order_status="Delivered").count()
    total_revenue = Order.objects.aggregate(s=Sum("total_price"))["s"] or 0

    six_months = timezone.now() - timedelta(days=182)
    monthly = (
        Order.objects.filter(created_at__gte=six_months)
        .annotate(month=ExtractMonth("created_at"), year=ExtractYear("created_at"))
        .values("month", "year")
        .annotate(revenue=Sum("total_price"), orders=Count("_id"))
        .order_by("year", "month")
    )
    monthly_revenue = [
        {"_id": {"month": r["month"], "year": r["year"]}, "revenue": r["revenue"], "orders": r["orders"]}
        for r in monthly
    ]

    seven_days = timezone.now() - timedelta(days=7)
    daily = (
        Order.objects.filter(created_at__gte=seven_days)
        .annotate(day=ExtractDay("created_at"), month=ExtractMonth("created_at"))
        .values("day", "month")
        .annotate(revenue=Sum("total_price"), orders=Count("_id"))
        .order_by("month", "day")
    )
    daily_revenue = [
        {"_id": {"day": r["day"], "month": r["month"]}, "revenue": r["revenue"], "orders": r["orders"]}
        for r in daily
    ]

    return Response({
        "success": True,
        "stats": {
            "totalOrders": total_orders,
            "deliveredOrders": delivered_orders,
            "totalRevenue": total_revenue,
            "monthlyRevenue": monthly_revenue,
            "dailyRevenue": daily_revenue,
        },
    })
