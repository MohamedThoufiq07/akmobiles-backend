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


class TimezoneAndTimestampIntegrityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="tzuser@example.com",
            password="password123",
            name="TZ User",
            phone="9000000003",
        )

    def test_timestamp_integrity_and_timezone_handling(self):
        import time

        # 1. Create order
        order = Order.objects.create(
            user=self.user,
            order_items=[],
            total_price=100.0,
            order_status="AwaitingPayment",
        )
        initial_created_at = order.created_at
        initial_updated_at = order.updated_at
        self.assertTrue(timezone.is_aware(initial_created_at))
        self.assertTrue(timezone.is_aware(initial_updated_at))

        # 2. Create Payment in Pending/Cancelled state
        payment = Payment.objects.create(
            order=order,
            user=self.user,
            amount=Decimal("100.00"),
            amount_paise=10000,
            status="Cancelled",
        )
        # paid_at must remain null for cancelled payments
        self.assertIsNone(payment.paid_at)

        # 3. Transition order status
        time.sleep(0.01)
        transition_order_status(order, "Placed")
        order.refresh_from_db()

        # created_at remains unchanged after updates
        self.assertEqual(order.created_at, initial_created_at)
        # updated_at changes after status update
        self.assertGreaterEqual(order.updated_at, initial_updated_at)

        # status_history entries have valid ISO timestamp
        self.assertGreater(len(order.status_history), 0)
        latest_hist = order.status_history[-1]
        self.assertIn("date", latest_hist)
        parsed_dt = timezone.datetime.fromisoformat(latest_hist["date"])
        self.assertTrue(timezone.is_aware(parsed_dt) or parsed_dt.tzinfo is not None)


class PerProductDeliveryCalculationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="calcuser@example.com",
            password="password123",
            name="Calc User",
            phone="9000000020",
        )
        self.client.force_authenticate(user=self.user)

        self.prod_a = Product.objects.create(
            name="Product A",
            brand="Brand A",
            category="Smartphones",
            original_price=1200.0,
            offer_price=1000.0,
            delivery_charge=Decimal("49.00"),
            stock=20,
        )
        self.prod_b = Product.objects.create(
            name="Product B",
            brand="Brand B",
            category="Accessories",
            original_price=600.0,
            offer_price=500.0,
            delivery_charge=Decimal("30.00"),
            stock=20,
        )
        self.prod_free = Product.objects.create(
            name="Product Free",
            brand="Brand C",
            category="Earbuds",
            original_price=2000.0,
            offer_price=1500.0,
            delivery_charge=Decimal("0.00"),
            stock=20,
        )

    def test_single_item_qty1_delivery_charge(self):
        # 1 item, qty 1, delivery ₹49 -> Subtotal ₹1,000, Shipping ₹49, Total ₹1,049
        payload = {
            "orderItems": [{"product": self.prod_a._id, "quantity": 1}],
            "shippingAddress": {"name": "Calc User", "city": "Chennai", "postalCode": "600001"},
            "paymentInfo": {"method": "Razorpay", "status": "Pending"},
        }
        res = self.client.post("/api/orders", data=payload, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()["order"]

        self.assertEqual(data["itemsPrice"], 1000.0)
        self.assertEqual(data["shippingPrice"], 49.0)
        self.assertEqual(data["totalPrice"], 1049.0)

        # Verify item snapshot
        item_snap = data["orderItems"][0]
        self.assertEqual(item_snap["delivery_charge"], 49.0)
        self.assertEqual(item_snap["line_delivery_total"], 49.0)

    def test_single_item_qty2_delivery_charge_not_multiplied(self):
        # 1 item, qty 2, delivery ₹49 -> Subtotal ₹2,000, Shipping ₹49, Total ₹2,049
        payload = {
            "orderItems": [{"product": self.prod_a._id, "quantity": 2}],
            "shippingAddress": {"name": "Calc User", "city": "Chennai", "postalCode": "600001"},
            "paymentInfo": {"method": "Razorpay", "status": "Pending"},
        }
        res = self.client.post("/api/orders", data=payload, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()["order"]

        self.assertEqual(data["itemsPrice"], 2000.0)
        self.assertEqual(data["shippingPrice"], 49.0)  # Must remain ₹49, not ₹98
        self.assertEqual(data["totalPrice"], 2049.0)

    def test_single_item_qty10_delivery_charge_not_multiplied(self):
        # 1 item, qty 10, delivery ₹49 -> Subtotal ₹10,000, Shipping ₹49, Total ₹10,049
        payload = {
            "orderItems": [{"product": self.prod_a._id, "quantity": 10}],
            "shippingAddress": {"name": "Calc User", "city": "Chennai", "postalCode": "600001"},
            "paymentInfo": {"method": "Razorpay", "status": "Pending"},
        }
        res = self.client.post("/api/orders", data=payload, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()["order"]

        self.assertEqual(data["itemsPrice"], 10000.0)
        self.assertEqual(data["shippingPrice"], 49.0)
        self.assertEqual(data["totalPrice"], 10049.0)

    def test_multi_product_delivery_charge_sum(self):
        # Prod A (qty 2, del ₹49) + Prod B (qty 3, del ₹30) + Prod Free (qty 1, del ₹0)
        # Subtotal: (1000*2) + (500*3) + (1500*1) = 2000 + 1500 + 1500 = 5000
        # Shipping: 49 + 30 + 0 = 79
        # Total: 5079
        payload = {
            "orderItems": [
                {"product": self.prod_a._id, "quantity": 2},
                {"product": self.prod_b._id, "quantity": 3},
                {"product": self.prod_free._id, "quantity": 1},
            ],
            "shippingAddress": {"name": "Calc User", "city": "Chennai", "postalCode": "600001"},
            "paymentInfo": {"method": "Razorpay", "status": "Pending"},
        }
        res = self.client.post("/api/orders", data=payload, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()["order"]

        self.assertEqual(data["itemsPrice"], 5000.0)
        self.assertEqual(data["shippingPrice"], 79.0)
        self.assertEqual(data["totalPrice"], 5079.0)

    def test_duplicate_product_id_merged_and_charged_once(self):
        # Duplicate payload entries for Prod A (qty 1 + qty 2) -> merged qty 3, delivery charged once
        payload = {
            "orderItems": [
                {"product": self.prod_a._id, "quantity": 1},
                {"product": self.prod_a._id, "quantity": 2},
            ],
            "shippingAddress": {"name": "Calc User", "city": "Chennai", "postalCode": "600001"},
            "paymentInfo": {"method": "Razorpay", "status": "Pending"},
        }
        res = self.client.post("/api/orders", data=payload, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()["order"]

        self.assertEqual(len(data["orderItems"]), 1)
        self.assertEqual(data["orderItems"][0]["quantity"], 3)
        self.assertEqual(data["itemsPrice"], 3000.0)
        self.assertEqual(data["shippingPrice"], 49.0)
        self.assertEqual(data["totalPrice"], 3049.0)

    def test_historical_orders_retain_saved_totals(self):
        # Order created in the past with ₹0 shipping
        past_order = Order.objects.create(
            user=self.user,
            order_items=[{"name": "Old Item", "price": 1000.0, "quantity": 1, "delivery_charge": 0.0}],
            items_price=1000.0,
            shipping_price=0.0,
            total_price=1000.0,
            order_status="Delivered",
        )

        # Later change to product delivery charge
        self.prod_a.delivery_charge = Decimal("99.00")
        self.prod_a.save()

        # Query past order
        res = self.client.get(f"/api/orders/{past_order._id}")
        self.assertEqual(res.status_code, 200)
        data = res.json()["order"]
        self.assertEqual(data["shippingPrice"], 0.0)
        self.assertEqual(data["totalPrice"], 1000.0)
