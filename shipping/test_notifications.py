from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from orders.models import Order
from orders.services import transition_order_status
from shipping.test_shipments import make_order

User = get_user_model()


class OrderEmailTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="buyer@x.com", password="pw12345678", name="Buyer")
        self.order = make_order(
            self.user,
            order_status="AwaitingPayment",
            awb_code="AWB77",
            courier_name="Blue Dart",
            tracking_url="https://track.example/AWB77",
        )

    def move(self, status):
        with self.captureOnCommitCallbacks(execute=True):
            transition_order_status(self.order, status)

    def test_placed_email_sent_once(self):
        self.move("Placed")
        self.move("Placed")  # no status change -> no second mail
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["buyer@x.com"])
        self.assertIn("Order confirmed", mail.outbox[0].subject)

    def test_shipped_email_has_tracking(self):
        self.move("Placed")
        self.move("Shipped")
        shipped = mail.outbox[-1]
        self.assertIn("AWB77", shipped.body)
        self.assertIn("https://track.example/AWB77", shipped.body)
        self.assertIn("Blue Dart", shipped.body)

    def test_full_lifecycle_sends_each_milestone(self):
        for status in ("Placed", "Shipped", "OutForDelivery", "Delivered"):
            self.move(status)
        self.assertEqual(len(mail.outbox), 4)

    def test_mail_failure_does_not_break_status_change(self):
        with patch("orders.notifications.EmailMultiAlternatives.send", side_effect=RuntimeError("smtp down")):
            self.move("Placed")
        self.order.refresh_from_db()
        self.assertEqual(self.order.order_status, "Placed")

    def test_processing_sends_no_email(self):
        self.move("Processing")
        self.assertEqual(len(mail.outbox), 0)


class AutoShipTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="b@x.com", password="pw12345678", name="B")
        self.order = make_order(self.user, order_status="AwaitingPayment")

    @override_settings(SHIPROCKET_AUTO_SHIP=True)
    def test_auto_ship_runs_when_enabled(self):
        with patch("shipping.services.auto_ship_order") as auto:
            with self.captureOnCommitCallbacks(execute=True):
                transition_order_status(self.order, "Placed")
        auto.assert_called_once_with(self.order._id)

    @override_settings(SHIPROCKET_AUTO_SHIP=False)
    def test_auto_ship_off_by_default(self):
        with patch("shipping.services.auto_ship_order") as auto:
            with self.captureOnCommitCallbacks(execute=True):
                transition_order_status(self.order, "Placed")
        auto.assert_not_called()

    @override_settings(SHIPROCKET_AUTO_SHIP=True)
    def test_auto_ship_failure_never_breaks_order(self):
        with patch("shipping.services.create_shipment", side_effect=RuntimeError("boom")):
            with self.captureOnCommitCallbacks(execute=True):
                transition_order_status(self.order, "Placed")
        self.order.refresh_from_db()
        self.assertEqual(self.order.order_status, "Placed")


class SerializerShipmentTests(TestCase):
    def test_shipment_field_present_only_after_awb(self):
        user = User.objects.create_user(email="s@x.com", password="pw12345678", name="S")
        order = make_order(user)
        api = APIClient()
        api.force_authenticate(user)
        self.assertIsNone(api.get(f"/api/orders/{order._id}").json()["order"]["shipment"])
        order.awb_code, order.courier_name = "A1", "DTDC"
        order.save()
        shipment = api.get(f"/api/orders/{order._id}").json()["order"]["shipment"]
        self.assertEqual(shipment["awbCode"], "A1")
        self.assertEqual(shipment["courierName"], "DTDC")
