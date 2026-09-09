import json
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from orders.models import Order
from orders.services import transition_order_status
from payments.models import Payment
from products.models import Product

User = get_user_model()


class OrderLifecycleAndInvoiceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="orderuser@example.com",
            password="password123",
            name="Order User",
            phone="9876543210",
        )
        self.other_user = User.objects.create_user(
            email="otherorderuser@example.com",
            password="password123",
            name="Other Order User",
            phone="9876543211",
        )
        self.client.force_authenticate(user=self.user)

        self.product = Product.objects.create(
            name="Smart Watch 2",
            brand="OnePlus",
            category="Smart Watches",
            description="Smart Watch",
            original_price=100.0,
            offer_price=73.0,
            stock=15,
        )

    def test_online_order_creation_is_awaiting_payment(self):
        payload = {
            "orderItems": [{"product": self.product._id, "quantity": 1}],
            "shippingAddress": {"name": "Order User", "city": "Chennai", "postalCode": "600001"},
            "paymentInfo": {"method": "Razorpay", "status": "Pending"},
        }
        res = self.client.post("/api/orders", data=payload, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()["order"]

        # Fulfilment status MUST be AwaitingPayment, not Placed
        self.assertEqual(data["orderStatus"], "AwaitingPayment")
        self.assertEqual(data["paymentInfo"]["status"], "Pending")

        # Initial history MUST contain AwaitingPayment
        hist = data["statusHistory"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "AwaitingPayment")

        # Stock must NOT be reduced
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 15)

    def test_cod_order_creation_is_placed_and_reduces_stock(self):
        payload = {
            "orderItems": [{"product": self.product._id, "quantity": 1}],
            "shippingAddress": {"name": "Order User", "city": "Chennai", "postalCode": "600001"},
            "paymentInfo": {"method": "COD", "status": "Pending"},
        }
        res = self.client.post("/api/orders", data=payload, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()["order"]

        self.assertEqual(data["orderStatus"], "Placed")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 14)

    def test_transition_order_status_terminal_protection(self):
        order = Order.objects.create(
            user=self.user,
            order_items=[],
            total_price=100.0,
            order_status="AwaitingPayment",
        )

        transition_order_status(order, "Placed")
        order.refresh_from_db()
        self.assertEqual(order.order_status, "Placed")

        transition_order_status(order, "Delivered")
        order.refresh_from_db()
        self.assertEqual(order.order_status, "Delivered")
        self.assertIsNotNone(order.delivered_at)

        # Attempt to downgrade Delivered order back to Placed or Processing
        transition_order_status(order, "Processing")
        order.refresh_from_db()
        self.assertEqual(order.order_status, "Delivered")

    def test_invoice_endpoint_guards(self):
        # 1. Unpaid order -> 409 Conflict
        order = Order.objects.create(
            user=self.user,
            order_items=[{"name": "Watch", "price": 73.0, "quantity": 1}],
            total_price=73.0,
            order_status="AwaitingPayment",
            payment_info={"status": "Pending"},
        )
        res = self.client.get(f"/api/orders/{order._id}/invoice")
        self.assertEqual(res.status_code, 409)
        self.assertIn("only after payment is completed", res.json()["message"])

        # 2. Other user tries to access -> 403 Forbidden
        self.client.force_authenticate(user=self.other_user)
        res = self.client.get(f"/api/orders/{order._id}/invoice")
        self.assertEqual(res.status_code, 403)

        # 3. Completed paid order -> 200 OK with PDF
        self.client.force_authenticate(user=self.user)
        order.order_status = "Placed"
        order.save()
        Payment.objects.create(
            order=order,
            user=self.user,
            amount=Decimal("73.00"),
            amount_paise=7300,
            status="Completed",
        )

        res = self.client.get(f"/api/orders/{order._id}/invoice")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertIn(f"attachment; filename=\"Invoice_{order._id}.pdf\"", res["Content-Disposition"])


class AdminOrdersFilteringTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="adminpassword123",
            name="Admin User",
            phone="9000000001",
            role="admin",
        )
        self.customer = User.objects.create_user(
            email="customer@example.com",
            password="customerpassword123",
            name="Customer User",
            phone="9000000002",
        )
        self.client.force_authenticate(user=self.admin)

    def test_admin_orders_filtering_and_stats(self):
        # 1. COD order (Placed) -> Must be INCLUDED
        cod_order = Order.objects.create(
            user=self.customer,
            order_items=[{"name": "Item 1", "price": 500.0, "quantity": 1}],
            total_price=500.0,
            order_status="Placed",
            payment_info={"method": "COD", "status": "Pending"},
        )

        # 2. Razorpay completed order (Placed) -> Must be INCLUDED
        rzp_paid_order = Order.objects.create(
            user=self.customer,
            order_items=[{"name": "Item 2", "price": 1000.0, "quantity": 1}],
            total_price=1000.0,
            order_status="Placed",
            payment_info={"method": "Razorpay", "status": "Completed"},
        )
        Payment.objects.create(
            order=rzp_paid_order,
            user=self.customer,
            amount=Decimal("1000.00"),
            amount_paise=100000,
            status="Completed",
            stock_reduced=True,
            paid_at=timezone.now(),
        )

        # 3. Post-payment cancelled order (Cancelled after capture) -> Must be INCLUDED for audit/refund
        post_cancel_order = Order.objects.create(
            user=self.customer,
            order_items=[{"name": "Item 3", "price": 300.0, "quantity": 1}],
            total_price=300.0,
            order_status="Cancelled",
            payment_info={"method": "Razorpay", "status": "Completed"},
        )
        Payment.objects.create(
            order=post_cancel_order,
            user=self.customer,
            amount=Decimal("300.00"),
            amount_paise=30000,
            status="Completed",
            stock_reduced=True,
            paid_at=timezone.now(),
        )

        # 4. Pre-payment cancelled checkout order (AwaitingPayment + Cancelled) -> Must be EXCLUDED
        pre_cancel_order = Order.objects.create(
            user=self.customer,
            order_items=[{"name": "Item 4", "price": 73.0, "quantity": 1}],
            total_price=73.0,
            order_status="AwaitingPayment",
            payment_info={"method": "Razorpay", "status": "Cancelled"},
        )
        Payment.objects.create(
            order=pre_cancel_order,
            user=self.customer,
            amount=Decimal("73.00"),
            amount_paise=7300,
            status="Cancelled",
            stock_reduced=False,
            paid_at=None,
        )

        # 5. Pre-payment pending checkout order (AwaitingPayment + Pending) -> Must be EXCLUDED
        pre_pending_order = Order.objects.create(
            user=self.customer,
            order_items=[{"name": "Item 5", "price": 200.0, "quantity": 1}],
            total_price=200.0,
            order_status="AwaitingPayment",
            payment_info={"method": "Razorpay", "status": "Pending"},
        )
        Payment.objects.create(
            order=pre_pending_order,
            user=self.customer,
            amount=Decimal("200.00"),
            amount_paise=20000,
            status="Pending",
            stock_reduced=False,
            paid_at=None,
        )

        # Query GET /api/orders (Admin list orders)
        res = self.client.get("/api/orders")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        order_ids = [o["_id"] for o in data["orders"]]
        self.assertEqual(data["total"], 3)
        self.assertIn(cod_order._id, order_ids)
        self.assertIn(rzp_paid_order._id, order_ids)
        self.assertIn(post_cancel_order._id, order_ids)
        self.assertNotIn(pre_cancel_order._id, order_ids)
        self.assertNotIn(pre_pending_order._id, order_ids)

        # Query GET /api/orders/stats (Admin order stats)
        res_stats = self.client.get("/api/orders/stats")
        self.assertEqual(res_stats.status_code, 200)
        stats = res_stats.json()["stats"]
        self.assertEqual(stats["totalOrders"], 3)

        # Query GET /api/admin/dashboard (Dashboard stats)
        res_dash = self.client.get("/api/admin/dashboard")
        self.assertEqual(res_dash.status_code, 200)
        dash_stats = res_dash.json()["stats"]
        self.assertEqual(dash_stats["totalOrders"], 3)
