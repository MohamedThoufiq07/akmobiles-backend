"""
Order views — port of controllers/orderController.js.

Endpoints (under /api/orders/, all require auth):
  POST   /              createOrder
  GET    /myorders      getMyOrders
  GET    /stats         getOrderStats        (admin)
  GET    /<id>          getOrderById          (owner or admin)
  GET    /<id>/invoice  getOrderInvoice       (owner or admin, requires Completed payment)
  GET    /              getAllOrders          (admin)
  PUT    /<id>/status   updateOrderStatus     (admin)
"""

from datetime import timedelta

from django.db.models import F, Sum, Count
from django.db.models.functions import ExtractMonth, ExtractYear, ExtractDay
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.pdf import generate_invoice_pdf
from common.permissions import paginate_queryset
from common.pricing import calculate_order_pricing
from products.models import Product
from .models import Order
from .serializers import OrderSerializer
from .services import transition_order_status, get_admin_orders_queryset


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
    qs = get_admin_orders_queryset()
    if q.get("status"):
        qs = qs.filter(order_status=q.get("status"))
    items, page, pages, total = paginate_queryset(qs, q.get("page", 1), q.get("limit", 20))
    data = OrderSerializer(items, many=True, context={"populate_user": True}).data
    return Response({"success": True, "orders": data, "page": page, "pages": pages, "total": total})


def _create_order(request):
    try:
        data = request.data
        order_items = data.get("orderItems") or []
        if not order_items:
            return Response({"success": False, "message": "No order items provided."}, status=400)

        pricing = calculate_order_pricing(order_items)
        if not pricing["items"]:
            return Response({"success": False, "message": "No valid order items found."}, status=400)

        # Validate stock
        for item in pricing["items"]:
            pid, qty = item["product"], item["quantity"]
            prod = Product.objects.filter(_id=pid).first()
            if not prod or prod.stock < qty:
                prod_name = prod.name if prod else "Product"
                return Response({"success": False, "message": f"{prod_name} is out of stock or insufficient quantity."}, status=400)

        payment_info = data.get("paymentInfo") or {}
        payment_method = payment_info.get("method", "Razorpay")
        is_online_payment = payment_method == "Razorpay"

        now_iso = timezone.now().isoformat()

        if is_online_payment:
            order_status = "AwaitingPayment"
            initial_status_history = [{
                "status": "AwaitingPayment",
                "date": now_iso,
                "description": "Order created, awaiting payment confirmation",
            }]
            order_payment_info = {
                "method": "Razorpay",
                "status": "Pending",
            }
        else:
            order_status = "Placed"
            initial_status_history = [{
                "status": "Placed",
                "date": now_iso,
                "description": "Order has been placed successfully",
            }]
            order_payment_info = {
                "method": payment_method,
                "status": "Pending",
            }

        order = Order.objects.create(
            user=request.user,
            order_items=pricing["items"],
            shipping_address=data.get("shippingAddress") or {},
            payment_info=order_payment_info,
            items_price=pricing["items_price"],
            tax_price=pricing["tax_price"],
            shipping_price=pricing["shipping_price"],
            total_price=pricing["total_price"],
            order_status=order_status,
            status_history=initial_status_history,
        )

        if not is_online_payment:
            for item in pricing["items"]:
                pid, qty = item["product"], item["quantity"]
                if pid:
                    Product.objects.filter(_id=pid).update(
                        stock=F("stock") - qty, num_sold=F("num_sold") + qty
                    )
            Product.objects.filter(stock__lt=0).update(stock=0)

        serialized = OrderSerializer(order).data
        return Response({"success": True, "order": serialized}, status=201)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return Response({"success": False, "message": str(exc)}, status=500)


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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_invoice(request, order_id):
    order = Order.objects.filter(_id=order_id).first()
    if not order:
        return Response({"success": False, "message": "Order not found"}, status=404)
    if order.user_id != request.user._id and request.user.role != "admin":
        return Response(
            {"success": False, "message": "Not authorized to view invoice for this order."}, status=403
        )

    try:
        payment = order.payment
    except Exception:
        payment = None
    # Enforce invoice restriction: only Completed payments & non-AwaitingPayment orders can download invoice
    is_completed = (payment and payment.status == "Completed") or (
        order.payment_info and order.payment_info.get("status") == "Completed"
    )
    if not is_completed or order.order_status == "AwaitingPayment":
        return Response(
            {"success": False, "message": "Invoice is available only after payment is completed."},
            status=409,
        )

    pdf_bytes = generate_invoice_pdf(order)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="Invoice_{order._id}.pdf"'
    return response


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
    if not status:
        return Response({"success": False, "message": "Status is required."}, status=400)

    try:
        updated_order = transition_order_status(order, status)
    except ValueError as e:
        return Response({"success": False, "message": str(e)}, status=400)

    return Response({"success": True, "order": OrderSerializer(updated_order).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_stats(request):
    if not _is_admin(request):
        return Response(
            {"success": False, "message": "Access denied. Admin privileges required."}, status=403
        )

    admin_qs = get_admin_orders_queryset()
    total_orders = admin_qs.count()
    delivered_orders = admin_qs.filter(order_status="Delivered").count()
    total_revenue = admin_qs.aggregate(s=Sum("total_price"))["s"] or 0

    six_months = timezone.now() - timedelta(days=182)
    monthly = (
        admin_qs.filter(created_at__gte=six_months)
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
        admin_qs.filter(created_at__gte=seven_days)
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
