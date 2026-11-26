from decimal import Decimal
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from orders.cancellation import CancellationError, cancel_order, issue_refund
from orders.models import Order
from payments.models import Payment
from products.models import Product
from shipping.client import ShiprocketException

User = get_user_model()


def make_product(stock=10, num_sold=0):
    return Product.objects.create(
        name="Phone X",
        brand="Brand",
        category="Smartphones",
        description="d",
        original_price=200.0,
        offer_price=100.0,
        stock=stock,
        num_sold=num_sold,
    )


def make_order(user, product, method="Razorpay", status="Placed", qty=2, **kw):
    order = Order.objects.create(
        user=user,
        order_items=[{"product": product._id, "name": "Phone X", "price": 100, "quantity": qty}],
        shipping_address={"name": "Buyer", "city": "Chennai", "state": "TN", "postalCode": "600001"},
        payment_info={"method": method, "status": "Completed" if method == "Razorpay" else "Pending"},
        items_price=200,
        total_price=200,
        order_status=status,
        **kw,
    )
    return order


def make_payment(order, user, status="Completed", stock_reduced=True):
    return Payment.objects.create(
        order=order,
        user=user,
        amount=Decimal("200.00"),
        amount_paise=20000,
        status=status,
        razorpay_payment_id="pay_ABC123",
        razorpay_order_id="order_XYZ",
        stock_reduced=stock_reduced,
    )


def rz_ok():
    rz = MagicMock()
    rz.payment.refund.return_value = {"id": "rfnd_1", "status": "processed"}
    return rz


def sr_ok():
    return MagicMock()


class CancelOrderServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="b@x.com", password="pw12345678", name="Buyer")
        self.other = User.objects.create_user(email="o@x.com", password="pw12345678", name="Other")
        self.admin = User.objects.create_user(
            email="a@x.com", password="pw12345678", name="Admin", role="admin"
        )
        self.product = make_product(stock=8, num_sold=2)  # as if 2 were just sold

    def test_paid_online_order_is_refunded_and_restocked(self):
        order = make_order(self.user, self.product)
        make_payment(order, self.user)
        rz = rz_ok()

        done, refund = cancel_order(order._id, self.user, rz_client=rz, client=sr_ok())

        self.assertEqual(done.order_status, "Cancelled")
        self.assertEqual(refund["status"], "Refunded")
        rz.payment.refund.assert_called_once()
        args = rz.payment.refund.call_args.args
        self.assertEqual(args[0], "pay_ABC123")
        self.assertEqual(args[1]["amount"], 20000)  # paise, full amount
        self.assertEqual(Payment.objects.get(order=order).status, "Refunded")
        self.assertEqual(done.payment_info["refundId"], "rfnd_1")
        self.product.refresh_from_db()
        self.assertEqual((self.product.stock, self.product.num_sold), (10, 0))

    def test_cod_order_cancel_restocks_without_refund(self):
        order = make_order(self.user, self.product, method="COD")
        rz = rz_ok()
        done, refund = cancel_order(order._id, self.user, rz_client=rz, client=sr_ok())
        self.assertEqual(done.order_status, "Cancelled")
        self.assertEqual(refund["status"], "NotRequired")
        rz.payment.refund.assert_not_called()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_unpaid_order_cancel_does_not_touch_stock(self):
        order = make_order(self.user, self.product, status="AwaitingPayment")
        make_payment(order, self.user, status="Pending", stock_reduced=False)
        done, refund = cancel_order(order._id, self.user, rz_client=rz_ok(), client=sr_ok())
        self.assertEqual(done.order_status, "Cancelled")
        self.assertEqual(refund["status"], "NotRequired")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 8)

    def test_double_cancel_refunds_and_restocks_only_once(self):
        order = make_order(self.user, self.product)
        make_payment(order, self.user)
        rz = rz_ok()
        cancel_order(order._id, self.user, rz_client=rz, client=sr_ok())
        _, second = cancel_order(order._id, self.user, rz_client=rz, client=sr_ok())
        self.assertEqual(second["status"], "AlreadyCancelled")
        self.assertEqual(rz.payment.refund.call_count, 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_refund_failure_keeps_cancellation_and_can_be_retried(self):
        order = make_order(self.user, self.product)
        make_payment(order, self.user)
        bad = MagicMock()
        bad.payment.refund.side_effect = RuntimeError("gateway down")

        done, refund = cancel_order(order._id, self.user, rz_client=bad, client=sr_ok())
        self.assertEqual(done.order_status, "Cancelled")
        self.assertEqual(refund["status"], "Failed")
        self.assertEqual(done.payment_info["refundStatus"], "Failed")
        self.assertEqual(Payment.objects.get(order=order).status, "Completed")

        retry = issue_refund(order._id, rz_client=rz_ok())
        self.assertEqual(retry["status"], "Refunded")
        self.assertEqual(Payment.objects.get(order=order).status, "Refunded")
        self.assertNotIn("refundError", Order.objects.get(_id=order._id).payment_info)

    def test_shipped_order_cannot_be_cancelled(self):
        order = make_order(self.user, self.product, status="Shipped")
        with self.assertRaises(CancellationError):
            cancel_order(order._id, self.user, rz_client=rz_ok(), client=sr_ok())

    def test_picked_up_parcel_cannot_be_cancelled_even_if_status_lags(self):
        order = make_order(
            self.user, self.product, awb_code="A1", shiprocket_order_id="9", shipping_status="PICKED UP"
        )
        with self.assertRaises(CancellationError):
            cancel_order(order._id, self.user, rz_client=rz_ok(), client=sr_ok())

    def test_other_customer_cannot_cancel(self):
        order = make_order(self.user, self.product)
        with self.assertRaises(CancellationError) as ctx:
            cancel_order(order._id, self.other, rz_client=rz_ok(), client=sr_ok())
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(Order.objects.get(_id=order._id).order_status, "Placed")

    def test_admin_can_cancel_any_order(self):
        order = make_order(self.user, self.product, method="COD")
        done, _ = cancel_order(order._id, self.admin, rz_client=rz_ok(), client=sr_ok())
        self.assertEqual(done.order_status, "Cancelled")

    def test_shiprocket_shipment_is_cancelled_first(self):
        order = make_order(
            self.user, self.product, awb_code="AWB9", shiprocket_order_id="555", shipping_status="PICKUP SCHEDULED"
        )
        make_payment(order, self.user)
        sr = sr_ok()
        done, _ = cancel_order(order._id, self.user, rz_client=rz_ok(), client=sr)
        sr.cancel_awb.assert_called_once_with("AWB9")
        sr.cancel_order.assert_called_once_with("555")
        self.assertEqual(done.shipping_status, "CANCELED")

    def test_shiprocket_failure_aborts_cancel_and_refund(self):
        order = make_order(
            self.user, self.product, awb_code="AWB9", shiprocket_order_id="555", shipping_status="PICKUP SCHEDULED"
        )
        make_payment(order, self.user)
        sr = sr_ok()
        sr.cancel_awb.side_effect = ShiprocketException("courier busy")
        rz = rz_ok()
        with self.assertRaises(CancellationError) as ctx:
            cancel_order(order._id, self.user, rz_client=rz, client=sr)
        self.assertEqual(ctx.exception.status, 502)
        order.refresh_from_db()
        self.assertEqual(order.order_status, "Placed")
        rz.payment.refund.assert_not_called()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 8)

    def test_cancel_sends_email_once(self):
        order = make_order(self.user, self.product, method="COD")
        with self.captureOnCommitCallbacks(execute=True):
            cancel_order(order._id, self.user, rz_client=rz_ok(), client=sr_ok())
        subjects = [m.subject for m in mail.outbox]
        self.assertEqual(sum("cancelled" in s.lower() for s in subjects), 1)


class CancelEndpointTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = User.objects.create_user(email="b@x.com", password="pw12345678", name="Buyer")
        self.admin = User.objects.create_user(
            email="a@x.com", password="pw12345678", name="Admin", role="admin"
        )
        self.product = make_product(stock=8, num_sold=2)

    def test_customer_cancel_endpoint(self):
        order = make_order(self.user, self.product, method="COD")
        self.api.force_authenticate(self.user)
        res = self.api.post(f"/api/orders/{order._id}/cancel", {"reason": "changed my mind"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["order"]["orderStatus"], "Cancelled")
        self.assertIn("changed my mind", res.json()["order"]["statusHistory"][-1]["description"])

    def test_cancel_endpoint_requires_login(self):
        order = make_order(self.user, self.product)
        self.assertIn(self.api.post(f"/api/orders/{order._id}/cancel").status_code, (401, 403))

    def test_cannot_cancel_shipped_via_endpoint(self):
        order = make_order(self.user, self.product, status="Shipped")
        self.api.force_authenticate(self.user)
        res = self.api.post(f"/api/orders/{order._id}/cancel")
        self.assertEqual(res.status_code, 400)

    def test_admin_status_cancelled_uses_full_cancel_flow(self):
        order = make_order(self.user, self.product, method="COD")
        self.api.force_authenticate(self.admin)
        res = self.api.put(f"/api/orders/{order._id}/status", {"status": "Cancelled"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)  # restocked, not just a status flip

    def test_refund_endpoint_is_admin_only_and_cancelled_only(self):
        order = make_order(self.user, self.product)
        self.api.force_authenticate(self.user)
        self.assertEqual(self.api.post(f"/api/orders/{order._id}/refund").status_code, 403)
        self.api.force_authenticate(self.admin)
        self.assertEqual(self.api.post(f"/api/orders/{order._id}/refund").status_code, 400)
