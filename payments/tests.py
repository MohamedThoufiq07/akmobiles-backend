import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from orders.models import Order
from payments.models import Payment, PaymentAttempt, RazorpayWebhookEvent
from products.models import Product

User = get_user_model()


class RazorpayPaymentIntegrationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="testuser@example.com",
            password="testpassword123",
            name="Test User",
            phone="9876543210",
        )
        self.other_user = User.objects.create_user(
            email="otheruser@example.com",
            password="testpassword123",
            name="Other User",
            phone="9876543211",
        )
        self.client.force_authenticate(user=self.user)

        self.product1 = Product.objects.create(
            name="Test Phone",
            brand="AK Brand",
            category="Smartphones",
            description="A premium smartphone",
            original_price=1200.0,
            offer_price=1000.0,
            stock=10,
        )
        self.product2 = Product.objects.create(
            name="Test Charger",
            brand="AK Brand",
            category="Chargers",
            description="Fast charger",
            original_price=600.0,
            offer_price=500.0,
            stock=5,
        )

        settings.RAZORPAY_KEY_ID = "rzp_test_mock_key_123"
        settings.RAZORPAY_KEY_SECRET = "mock_secret_abc456"
        settings.RAZORPAY_WEBHOOK_SECRET = "mock_webhook_secret_789"

    def _create_test_order(self, user=None, items=None, payment_status="Pending", order_status="AwaitingPayment"):
        target_user = user or self.user
        order_items = items or [
            {
                "product": self.product1._id,
                "name": self.product1.name,
                "price": self.product1.offer_price,
                "quantity": 1,
            }
        ]
        return Order.objects.create(
            user=target_user,
            order_items=order_items,
            shipping_address={"name": target_user.name, "city": "Chennai", "postalCode": "600001"},
            payment_info={"method": "Razorpay", "status": payment_status},
            items_price=1000.0,
            tax_price=180.0,
            shipping_price=0.0,
            total_price=1000.0,
            order_status=order_status,
            status_history=[{"status": order_status, "date": "2026-09-09T10:00:00Z", "description": "Created"}],
        )

    # 1. Canonical endpoint paths and unauthenticated rejection
    def test_unauthenticated_create_order_rejected(self):
        self.client.force_authenticate(user=None)
        res = self.client.post("/api/payments/razorpay/create-order/", {"orderId": "nonexistent"})
        self.assertEqual(res.status_code, 401)

    def test_order_ownership_validation(self):
        other_order = self._create_test_order(user=self.other_user)
        res = self.client.post("/api/payments/razorpay/create-order/", {"orderId": other_order._id})
        self.assertEqual(res.status_code, 403)

    # 2. Server-side recalculation and client amount tampering ignored
    @patch("payments.views._razorpay_client")
    def test_create_order_recalculates_pricing_server_authoritative(self, mock_rzp_client_func):
        mock_client = MagicMock()
        mock_client.order.create.return_value = {"id": "order_rzp_test_100"}
        mock_rzp_client_func.return_value = mock_client

        order = self._create_test_order(
            items=[{"product": self.product2._id, "quantity": 1, "price": 1.0}]
        )

        res = self.client.post("/api/payments/razorpay/create-order/", {
            "orderId": order._id,
            "amount": 100,
        })

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["amount"], 54900)
        self.assertEqual(data["orderId"], "order_rzp_test_100")
        self.assertEqual(data["key"], settings.RAZORPAY_KEY_ID)

        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.amount_paise, 54900)
        self.assertEqual(payment.amount, Decimal("549.00"))
        self.assertEqual(payment.status, "Pending")
        self.assertEqual(payment.razorpay_order_id, "order_rzp_test_100")

    # 3. Already-paid order rejection
    def test_already_paid_order_rejection(self):
        order = self._create_test_order(payment_status="Completed", order_status="Placed")
        Payment.objects.create(
            order=order,
            user=self.user,
            amount=Decimal("1000.00"),
            amount_paise=100000,
            status="Completed",
            razorpay_order_id="order_already_paid",
        )
        res = self.client.post("/api/payments/razorpay/create-order/", {"orderId": order._id})
        self.assertEqual(res.status_code, 400)
        self.assertIn("already paid", res.json()["message"].lower())

    # 4. Checkout dismissal marks payment cancelled and retains awaiting payment
    @patch("payments.views._razorpay_client")
    def test_checkout_dismissed_marks_cancelled_and_idempotent(self, mock_rzp_client_func):
        mock_client = MagicMock()
        mock_client.order.payments.return_value = {"items": []}  # No captured payment
        mock_rzp_client_func.return_value = mock_client

        order = self._create_test_order()
        payment = Payment.objects.create(
            order=order,
            user=self.user,
            amount=Decimal("1000.00"),
            amount_paise=100000,
            status="Pending",
            razorpay_order_id="order_rzp_dismiss_test",
        )

        # First dismissal call with internalOrderId and razorpayOrderId
        res1 = self.client.post("/api/payments/razorpay/checkout-dismissed/", {
            "internalOrderId": order._id,
            "razorpayOrderId": "order_rzp_dismiss_test",
        })
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json()["status"], "cancelled")

        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, "Cancelled")
        self.assertEqual(order.order_status, "AwaitingPayment")
        self.assertFalse(payment.stock_reduced)
        self.assertEqual(self.product1.stock, 10)

        # Ensure PaymentAttempt created
        self.assertEqual(PaymentAttempt.objects.filter(payment=payment, status="Cancelled").count(), 1)

        # Second dismissal call (Idempotency test)
        res2 = self.client.post("/api/payments/razorpay/checkout-dismissed/", {
            "internalOrderId": order._id,
            "razorpayOrderId": "order_rzp_dismiss_test",
        })
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(PaymentAttempt.objects.filter(payment=payment, status="Cancelled").count(), 1)

    # 5. Checkout dismissal race condition: if captured on gateway, complete order
    @patch("payments.views._razorpay_client")
    def test_checkout_dismissed_captured_race_condition(self, mock_rzp_client_func):
        mock_client = MagicMock()
        mock_client.order.payments.return_value = {
            "items": [{"id": "pay_race_captured", "status": "captured"}]
        }
        mock_rzp_client_func.return_value = mock_client

        order = self._create_test_order()
        payment = Payment.objects.create(
            order=order,
            user=self.user,
            amount=Decimal("1000.00"),
            amount_paise=100000,
            status="Pending",
            razorpay_order_id="order_rzp_race_test",
        )

        res = self.client.post("/api/payments/razorpay/checkout-dismissed/", {"orderId": order._id})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "captured")

        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, "Completed")
        self.assertEqual(order.order_status, "Placed")
        self.assertTrue(payment.stock_reduced)

    # 6. Signature verification and complete_payment_once
    @patch("payments.views._razorpay_client")
    def test_verify_payment_signature_and_captured_success(self, mock_rzp_client_func):
        mock_client = MagicMock()
        mock_client.payment.fetch.return_value = {
            "id": "pay_test_999",
            "order_id": "order_rzp_verify_1",
            "amount": 100000,
            "currency": "INR",
            "status": "captured",
        }
        mock_rzp_client_func.return_value = mock_client

        order = self._create_test_order()
        payment = Payment.objects.create(
            order=order,
            user=self.user,
            amount=Decimal("1000.00"),
            amount_paise=100000,
            currency="INR",
            status="Pending",
            razorpay_order_id="order_rzp_verify_1",
        )

        initial_stock = self.product1.stock

        body = f"{payment.razorpay_order_id}|pay_test_999".encode("utf-8")
        valid_signature = hmac.new(
            settings.RAZORPAY_KEY_SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()

        res = self.client.post("/api/payments/razorpay/verify-payment/", {
            "orderId": order._id,
            "razorpay_order_id": "order_rzp_verify_1",
            "razorpay_payment_id": "pay_test_999",
            "razorpay_signature": valid_signature,
        })

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "captured")

        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, "Completed")
        self.assertEqual(order.order_status, "Placed")
        self.assertTrue(payment.stock_reduced)
        self.assertIsNotNone(payment.paid_at)

        self.product1.refresh_from_db()
        self.assertEqual(self.product1.stock, initial_stock - 1)

    # 7. Webhook payment.captured transitions order to Placed
    def test_webhook_payment_captured_success(self):
        order = self._create_test_order()
        payment = Payment.objects.create(
            order=order,
            user=self.user,
            amount=Decimal("1000.00"),
            amount_paise=100000,
            currency="INR",
            status="Pending",
            razorpay_order_id="order_rzp_wh_1",
        )

        payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_wh_captured_123",
                        "order_id": "order_rzp_wh_1",
                        "amount": 100000,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

        res = self.client.post(
            "/api/payments/razorpay/webhook/",
            data=raw_body,
            content_type="application/json",
            HTTP_X_RAZORPAY_EVENT_ID="evt_test_unique_001",
            HTTP_X_RAZORPAY_SIGNATURE=sig,
        )

        self.assertEqual(res.status_code, 200)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, "Completed")
        self.assertEqual(order.order_status, "Placed")
        self.assertTrue(payment.stock_reduced)

    # 8. Webhook duplicate idempotency
    def test_webhook_duplicate_event_idempotency(self):
        RazorpayWebhookEvent.objects.create(
            event_id="evt_duplicate_999",
            event_type="payment.captured",
        )

        payload = {"event": "payment.captured"}
        raw_body = json.dumps(payload).encode("utf-8")
        sig = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

        res = self.client.post(
            "/api/payments/razorpay/webhook/",
            data=raw_body,
            content_type="application/json",
            HTTP_X_RAZORPAY_EVENT_ID="evt_duplicate_999",
            HTTP_X_RAZORPAY_SIGNATURE=sig,
        )

        self.assertEqual(res.status_code, 200)
        self.assertIn("already processed", res.json()["message"].lower())

    # 9. Out-of-order failed webhook does not downgrade Completed payment
    def test_webhook_failed_does_not_downgrade_completed_payment(self):
        order = self._create_test_order(order_status="Placed", payment_status="Completed")
        payment = Payment.objects.create(
            order=order,
            user=self.user,
            amount=Decimal("1000.00"),
            amount_paise=100000,
            currency="INR",
            status="Completed",
            razorpay_order_id="order_rzp_completed_stay",
            razorpay_payment_id="pay_completed_prior",
        )

        payload = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_failed_later",
                        "order_id": "order_rzp_completed_stay",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Card declined",
                    }
                }
            },
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

        res = self.client.post(
            "/api/payments/razorpay/webhook/",
            data=raw_body,
            content_type="application/json",
            HTTP_X_RAZORPAY_EVENT_ID="evt_failed_out_of_order",
            HTTP_X_RAZORPAY_SIGNATURE=sig,
        )

        self.assertEqual(res.status_code, 200)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, "Completed")
        self.assertEqual(order.order_status, "Placed")

    # 10. Reconciliation management command dry run
    @patch("razorpay.Client")
    def test_reconcile_command_dry_run_zero_writes(self, mock_rzp_client_class):
        mock_instance = MagicMock()
        mock_instance.order.fetch.return_value = {"id": "order_rzp_recon_1"}
        mock_instance.order.payments.return_value = {"items": []}  # uncaptured
        mock_rzp_client_class.return_value = mock_instance

        order = self._create_test_order(order_status="Placed", payment_status="Pending")
        Payment.objects.create(
            order=order,
            user=self.user,
            amount=Decimal("1000.00"),
            amount_paise=100000,
            status="Pending",
            razorpay_order_id="order_rzp_recon_1",
        )

        call_command("reconcile_razorpay_orders", dry_run=True)

        # In dry run mode, database records MUST NOT be modified
        order.refresh_from_db()
        self.assertEqual(order.order_status, "Placed")

    # 11. Stale payments cron endpoint authentication
    def test_expire_stale_endpoint_requires_cron_secret(self):
        settings.CRON_SECRET = "super_secret_cron_token_123"

        # Unauthorized request without Bearer header
        res = self.client.post("/api/payments/razorpay/expire-stale/")
        self.assertEqual(res.status_code, 401)

        # Authorized request with Bearer header
        res = self.client.post(
            "/api/payments/razorpay/expire-stale/",
            HTTP_AUTHORIZATION="Bearer super_secret_cron_token_123",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
