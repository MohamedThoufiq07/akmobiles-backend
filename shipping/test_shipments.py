from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from orders.models import Order
from shipping.services import ShipmentError, create_shipment, build_order_payload
from shipping.client import ShiprocketException

User = get_user_model()

ADDRESS = {
    "name": "Ravi Kumar",
    "addressLine1": "12 Main Road",
    "city": "Chennai",
    "state": "Tamil Nadu",
    "postalCode": "600001",
    "phone": "+91 98765 43210",
}


def make_order(user, **kw):
    defaults = dict(
        user=user,
        order_items=[{"product": "p1", "name": "Phone", "price": 20000, "quantity": 1}],
        shipping_address=ADDRESS,
        payment_info={"method": "Razorpay", "status": "Completed"},
        items_price=20000,
        total_price=20000,
        order_status="Placed",
    )
    defaults.update(kw)
    return Order.objects.create(**defaults)


def fake_client(awb="AWB123"):
    client = MagicMock()
    client.pickup_location = "work"
    client.create_order.return_value = {"order_id": 555, "shipment_id": 777}
    client.assign_awb.return_value = {
        "response": {"data": {"awb_code": awb, "courier_name": "Delhivery"}}
    }
    return client


class ShipmentServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="c@x.com", password="pw12345678", name="Cust")
        self.order = make_order(self.user)

    def test_payload_prepaid_and_cleaned_fields(self):
        p = build_order_payload(self.order, "work")
        self.assertEqual(p["payment_method"], "Prepaid")
        self.assertEqual(p["billing_phone"], "9876543210")
        self.assertEqual(p["billing_pincode"], "600001")
        self.assertEqual(p["order_id"], str(self.order._id))

    def test_cod_payload(self):
        order = make_order(self.user, payment_info={"method": "COD"})
        self.assertEqual(build_order_payload(order, "work")["payment_method"], "COD")

    def test_missing_address_fields_rejected(self):
        order = make_order(self.user, shipping_address={"name": "A", "city": "X"})
        with self.assertRaises(ShipmentError):
            build_order_payload(order, "work")

    def test_create_shipment_saves_awb(self):
        client = fake_client()
        order = create_shipment(self.order._id, client=client)
        self.assertEqual(order.awb_code, "AWB123")
        self.assertEqual(order.shiprocket_shipment_id, "777")
        self.assertEqual(order.courier_name, "Delhivery")
        client.schedule_pickup.assert_called_once_with("777")

    def test_create_shipment_is_idempotent(self):
        client = fake_client()
        create_shipment(self.order._id, client=client)
        create_shipment(self.order._id, client=client)
        client.create_order.assert_called_once()

    def test_awb_failure_keeps_ids_and_retry_does_not_duplicate(self):
        client = fake_client()
        client.assign_awb.side_effect = ShiprocketException("Low wallet balance")
        with self.assertRaises(ShipmentError):
            create_shipment(self.order._id, client=client)
        self.order.refresh_from_db()
        self.assertEqual(self.order.shiprocket_shipment_id, "777")
        self.assertEqual(self.order.awb_code, "")

        client.assign_awb.side_effect = None
        client.assign_awb.return_value = {"response": {"data": {"awb_code": "A9", "courier_name": "X"}}}
        order = create_shipment(self.order._id, client=client)
        self.assertEqual(order.awb_code, "A9")
        client.create_order.assert_called_once()

    def test_unpaid_order_cannot_ship(self):
        order = make_order(self.user, order_status="AwaitingPayment")
        with self.assertRaises(ShipmentError):
            create_shipment(order._id, client=fake_client())


@override_settings(SHIPROCKET_WEBHOOK_TOKEN="secret-token")
class WebhookTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = User.objects.create_user(email="c@x.com", password="pw12345678", name="Cust")
        self.order = make_order(self.user, awb_code="AWB1", shiprocket_shipment_id="1")
        self.url = "/api/shipping/tracking-updates/"

    def test_rejects_missing_or_wrong_key(self):
        self.assertEqual(self.api.post(self.url, {}, format="json").status_code, 401)
        r = self.api.post(self.url, {}, format="json", HTTP_X_API_KEY="nope")
        self.assertEqual(r.status_code, 401)

    @override_settings(SHIPROCKET_WEBHOOK_TOKEN="")
    def test_unconfigured_token_refuses(self):
        r = self.api.post(self.url, {}, format="json", HTTP_X_API_KEY="")
        self.assertEqual(r.status_code, 503)

    def test_unknown_order_returns_200_for_test_webhook(self):
        r = self.api.post(self.url, {"awb": "ZZZ"}, format="json", HTTP_X_API_KEY="secret-token")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["matched"])

    def test_status_updates_order(self):
        r = self.api.post(
            self.url,
            {"awb": "AWB1", "current_status": "IN TRANSIT"},
            format="json",
            HTTP_X_API_KEY="secret-token",
        )
        self.assertEqual(r.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.shipping_status, "IN TRANSIT")
        self.assertEqual(self.order.order_status, "Shipped")

        self.api.post(
            self.url,
            {"awb": "AWB1", "current_status": "DELIVERED"},
            format="json",
            HTTP_X_API_KEY="secret-token",
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.order_status, "Delivered")


class ShipmentEndpointPermissionTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = User.objects.create_user(email="c@x.com", password="pw12345678", name="Cust")
        self.other = User.objects.create_user(email="o@x.com", password="pw12345678", name="Other")
        self.order = make_order(self.user, awb_code="A1")

    def test_create_requires_admin(self):
        self.api.force_authenticate(self.user)
        r = self.api.post(f"/api/shipping/orders/{self.order._id}/create-shipment/")
        self.assertEqual(r.status_code, 403)

    def test_shipment_visible_to_owner_only(self):
        self.api.force_authenticate(self.user)
        self.assertEqual(self.api.get(f"/api/shipping/orders/{self.order._id}/shipment/").status_code, 200)
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(f"/api/shipping/orders/{self.order._id}/shipment/").status_code, 403)
