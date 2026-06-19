"""
Product + Review models — port of models/Product.js.

Embedded Mongo sub-objects are kept as JSONField so the JSON shape the frontend
receives is identical (no relational flattening that would change the response):
  highlights      -> JSONField (list[str])
  specifications  -> JSONField (dict: processor, ram, storage, display, ...)
  images          -> JSONField (list[{ url, alt }])

Reviews are a real related table (they reference a user) but are serialized back
*embedded* inside the product, exactly like the Mongo `reviews` array.
"""

from django.db import models

from common.utils import generate_object_id

CATEGORY_CHOICES = (
    ("Smartphones", "Smartphones"),
    ("Accessories", "Accessories"),
    ("Smart Watches", "Smart Watches"),
    ("Earbuds", "Earbuds"),
    ("Chargers", "Chargers"),
    ("Power Banks", "Power Banks"),
)


class Product(models.Model):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)

    name = models.CharField(max_length=255)
    brand = models.CharField(max_length=120)
    category = models.CharField(max_length=40, choices=CATEGORY_CHOICES)
    description = models.TextField()

    highlights = models.JSONField(default=list, blank=True)
    specifications = models.JSONField(default=dict, blank=True)
    images = models.JSONField(default=list, blank=True)

    original_price = models.FloatField()
    offer_price = models.FloatField()
    discount = models.IntegerField(default=0)
    stock = models.IntegerField(default=0)
    rating = models.FloatField(default=0)
    num_reviews = models.IntegerField(default=0)

    is_featured = models.BooleanField(default=False)
    flash_sale = models.BooleanField(default=False)
    num_sold = models.IntegerField(default=0)

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
        ]

    def save(self, *args, **kwargs):
        # Mirror the Mongoose pre-save: compute discount % from prices.
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

    def __str__(self):
        return self.name


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
