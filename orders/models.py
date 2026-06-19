"""
Order model — port of models/Order.js.

Embedded Mongo arrays/objects -> JSONField, preserving the exact response shape:
  orderItems[]    -> order_items   (list of { product, name, image, price, quantity })
  shippingAddress -> shipping_address
  paymentInfo     -> payment_info  ({ razorpayOrderId, razorpayPaymentId, razorpaySignature, status })
  statusHistory[] -> status_history (list of { status, date, description })
"""

from datetime import timedelta

from django.db import models
from django.utils import timezone

from common.utils import generate_object_id

ORDER_STATUS_CHOICES = (
    ("Placed", "Placed"),
    ("Processing", "Processing"),
    ("Shipped", "Shipped"),
    ("Delivered", "Delivered"),
    ("Cancelled", "Cancelled"),
)


class Order(models.Model):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    user = models.ForeignKey("accounts.User", related_name="orders", on_delete=models.CASCADE)

    order_items = models.JSONField(default=list)
    shipping_address = models.JSONField(default=dict)
    payment_info = models.JSONField(default=dict, blank=True)

    items_price = models.FloatField(default=0)
    tax_price = models.FloatField(default=0)
    shipping_price = models.FloatField(default=0)
    total_price = models.FloatField(default=0)

    order_status = models.CharField(max_length=20, choices=ORDER_STATUS_CHOICES, default="Placed")
    status_history = models.JSONField(default=list, blank=True)

    estimated_delivery = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "orders"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["order_status"]),
            models.Index(fields=["-created_at"]),
        ]

    def save(self, *args, **kwargs):
        # Mirror the Mongoose pre-save on first insert.
        is_new = self._state.adding
        if is_new:
            now = timezone.now()
            self.estimated_delivery = now + timedelta(days=5)
            if not self.status_history:
                self.status_history = [{
                    "status": "Placed",
                    "date": now.isoformat(),
                    "description": "Order has been placed successfully",
                }]
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order {self._id}"
