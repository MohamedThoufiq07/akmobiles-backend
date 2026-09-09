from decimal import Decimal
from django.conf import settings
from django.db import models

from common.utils import generate_object_id

PAYMENT_STATUS_CHOICES = (
    ("Pending", "Pending"),
    ("Authorized", "Authorized"),
    ("Processing", "Processing"),
    ("Completed", "Completed"),
    ("Failed", "Failed"),
    ("Cancelled", "Cancelled"),
    ("Expired", "Expired"),
    ("Refunded", "Refunded"),
)


class Payment(models.Model):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    order = models.OneToOneField("orders.Order", related_name="payment", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="payments", on_delete=models.CASCADE)

    razorpay_order_id = models.CharField(max_length=100, unique=True, null=True, blank=True, db_index=True)
    razorpay_payment_id = models.CharField(max_length=100, unique=True, null=True, blank=True, db_index=True)
    razorpay_signature = models.CharField(max_length=255, blank=True, default="")

    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    amount_paise = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=10, default="INR")
    status = models.CharField(max_length=30, choices=PAYMENT_STATUS_CHOICES, default="Pending")

    receipt = models.CharField(max_length=100, blank=True, default="")
    error_code = models.CharField(max_length=100, blank=True, default="")
    error_description = models.TextField(blank=True, default="")

    stock_reduced = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payments"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Payment {self._id} (Order {self.order_id}) - {self.status}"


class PaymentAttempt(models.Model):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    payment = models.ForeignKey(Payment, related_name="attempts", on_delete=models.CASCADE)

    razorpay_payment_id = models.CharField(max_length=100, unique=True, null=True, blank=True, db_index=True)
    razorpay_order_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    status = models.CharField(max_length=30, choices=PAYMENT_STATUS_CHOICES, default="Pending")

    amount_paise = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=10, default="INR")

    error_code = models.CharField(max_length=100, blank=True, default="")
    error_description = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payment_attempts"
        ordering = ["-created_at"]

    def __str__(self):
        return f"PaymentAttempt {self._id} - {self.razorpay_payment_id or 'No ID'} ({self.status})"


class RazorpayWebhookEvent(models.Model):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    event_id = models.CharField(max_length=100, unique=True, db_index=True)
    event_type = models.CharField(max_length=100)
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.SET_NULL, related_name="webhook_events")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "razorpay_webhook_events"
        ordering = ["-created_at"]

    def __str__(self):
        return f"WebhookEvent {self.event_id} ({self.event_type})"
