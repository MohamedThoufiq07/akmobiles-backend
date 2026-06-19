from rest_framework import serializers

from .models import Contact, Settings


class ContactSerializer(serializers.ModelSerializer):
    isRead = serializers.BooleanField(source="is_read", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Contact
        fields = ["_id", "name", "email", "subject", "message", "isRead", "createdAt", "updatedAt"]


class SettingsSerializer(serializers.ModelSerializer):
    flashSaleActive = serializers.BooleanField(source="flash_sale_active")
    flashSaleTitle = serializers.CharField(source="flash_sale_title")
    flashSaleSubtitle = serializers.CharField(source="flash_sale_subtitle")
    flashSaleEndsAt = serializers.DateTimeField(source="flash_sale_ends_at", allow_null=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Settings
        fields = [
            "_id", "flashSaleActive", "flashSaleTitle", "flashSaleSubtitle",
            "flashSaleEndsAt", "createdAt", "updatedAt",
        ]
