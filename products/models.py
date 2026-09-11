"""
Product + Review + ProductImage models.

Embedded Mongo sub-objects are kept as JSONField for backward compatibility:
  highlights      -> JSONField (list[str])
  specifications  -> JSONField (dict: processor, ram, storage, display, ...)
  images          -> JSONField (list[{ url, alt }]) (legacy)

ProductImage:
  Normalized relational table for multiple product images with sort order,
  primary flag constraint, dimensions, and storage metadata.
"""

from decimal import Decimal
import secrets
from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from common.utils import generate_object_id

CATEGORY_CHOICES = (
    ("Smartphones", "Smartphones"),
    ("Accessories", "Accessories"),
    ("Smart Watches", "Smart Watches"),
    ("Earbuds", "Earbuds"),
    ("Chargers", "Chargers"),
    ("Power Banks", "Power Banks"),
)


def generate_session_token():
    return secrets.token_urlsafe(32)


class Product(models.Model):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)

    name = models.CharField(max_length=255)
    brand = models.CharField(max_length=120)
    category = models.CharField(max_length=40, choices=CATEGORY_CHOICES)
    description = models.TextField()

    highlights = models.JSONField(default=list, blank=True)
    specifications = models.JSONField(default=dict, blank=True)
    images = models.JSONField(default=list, blank=True)  # Legacy JSON field retained for backward compatibility

    original_price = models.FloatField()
    offer_price = models.FloatField()
    delivery_charge = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("49.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    discount = models.IntegerField(default=0)
    stock = models.IntegerField(default=0)
    rating = models.FloatField(default=0)
    num_reviews = models.IntegerField(default=0)

    is_featured = models.BooleanField(default=False)
    flash_sale = models.BooleanField(default=False)
    num_sold = models.IntegerField(default=0)

    is_active = models.BooleanField(default=True, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="archived_products",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "products"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["brand"]),
            models.Index(fields=["category"]),
            models.Index(fields=["offer_price"]),
            models.Index(fields=["-rating"]),
            models.Index(fields=["-num_sold"]),
            models.Index(fields=["-discount"]),
            models.Index(fields=["-created_at"]),
            models.Index(fields=["is_featured"]),
            models.Index(fields=["flash_sale"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["archived_at"]),
        ]

    def save(self, *args, **kwargs):
        # Compute discount % from prices
        if self.original_price and self.offer_price:
            self.discount = round(
                ((self.original_price - self.offer_price) / self.original_price) * 100
            )
        super().save(*args, **kwargs)

    def recalculate_rating(self):
        reviews = list(self.reviews.all())
        self.num_reviews = len(reviews)
        self.rating = (sum(r.rating for r in reviews) / len(reviews)) if reviews else 0
        self.save(update_fields=["num_reviews", "rating"])

    @property
    def primary_image(self):
        """Returns the primary ProductImage or the first ordered ProductImage."""
        primary = self.product_images.filter(is_primary=True).first()
        if primary:
            return primary
        return self.product_images.order_by("sort_order", "created_at").first()

    def __str__(self):
        return self.name


class ProductImage(models.Model):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    product = models.ForeignKey(Product, related_name="product_images", on_delete=models.CASCADE)
    url = models.TextField()
    storage_key = models.CharField(max_length=500, blank=True, default="")
    alt_text = models.CharField(max_length=255, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)
    is_primary = models.BooleanField(default=False)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    file_size = models.IntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=100, blank=True, default="image/jpeg")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "product_images"
        ordering = ["sort_order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["product"],
                condition=models.Q(is_primary=True),
                name="unique_primary_image_per_product",
            )
        ]
        indexes = [
            models.Index(fields=["product", "sort_order"]),
            models.Index(fields=["product", "is_primary"]),
        ]

    def __str__(self):
        return f"Image {self._id} for Product {self.product_id}"


class Review(models.Model):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    product = models.ForeignKey(Product, related_name="reviews", on_delete=models.CASCADE)
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    rating = models.IntegerField()
    comment = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "product_reviews"
        ordering = ["-created_at"]
        unique_together = ("product", "user")  # one review per user per product

    def __str__(self):
        return f"{self.name} -> {self.product_id}"


class ProductUploadSession(models.Model):
    """
    Short-lived admin upload session for secure multi-image staging prior to product creation/editing.
    """
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
    token = models.CharField(max_length=64, unique=True, default=generate_session_token)
    status = models.CharField(max_length=20, default="active")  # active, committed, expired
    expires_at = models.DateTimeField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "product_upload_sessions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["user", "status"]),
        ]

    def is_valid(self):
        return self.status == "active" and self.expires_at > timezone.now()


class ProductUploadItem(models.Model):
    """
    Individual staged image item tied to an upload session.
    """
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    session = models.ForeignKey(ProductUploadSession, related_name="items", on_delete=models.CASCADE)
    url = models.TextField()
    storage_key = models.CharField(max_length=500)
    filename = models.CharField(max_length=255, blank=True, default="")
    file_size = models.IntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=100, blank=True, default="image/jpeg")
    is_committed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "product_upload_items"
        ordering = ["created_at"]
