"""
Core models — ports of Contact.js, Newsletter.js, Settings.js.
"""

from django.db import models

from common.utils import generate_object_id


class Contact(models.Model):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    name = models.CharField(max_length=120)
    email = models.EmailField()
    subject = models.CharField(max_length=255)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "contacts"
        ordering = ["-created_at"]


class Newsletter(models.Model):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "newsletter_subscribers"
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.lower()
        super().save(*args, **kwargs)


class Settings(models.Model):
    """Single global settings row (store-wide config like the flash sale)."""

    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)
    flash_sale_active = models.BooleanField(default=False)
    flash_sale_title = models.CharField(max_length=255, default="Flash Sale")
    flash_sale_subtitle = models.CharField(
        max_length=255, default="Deals ending soon! Lowest prices of the month."
    )
    flash_sale_ends_at = models.DateTimeField(null=True, blank=True, default=None)
    banners = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "settings"

    @classmethod
    def get_singleton(cls):
        doc = cls.objects.first()
        if not doc:
            doc = cls.objects.create()
        return doc
