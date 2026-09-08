"""
Serializers for products. Field names are emitted in the exact camelCase the
frontend already uses (originalPrice, offerPrice, numReviews, isFeatured, ...).
"""

from rest_framework import serializers

from common.utils import sanitize_image_url
from .models import Product, Review


class ReviewSerializer(serializers.ModelSerializer):
    user = serializers.CharField(source="user_id", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Review
        fields = ["_id", "user", "name", "rating", "comment", "createdAt", "updatedAt"]


class ProductSerializer(serializers.ModelSerializer):
    reviews = ReviewSerializer(many=True, read_only=True)
    images = serializers.SerializerMethodField()
    originalPrice = serializers.FloatField(source="original_price")
    offerPrice = serializers.FloatField(source="offer_price")
    numReviews = serializers.IntegerField(source="num_reviews", read_only=True)
    isFeatured = serializers.BooleanField(source="is_featured", required=False)
    flashSale = serializers.BooleanField(source="flash_sale", required=False)
    numSold = serializers.IntegerField(source="num_sold", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Product
        fields = [
            "_id", "name", "brand", "category", "description",
            "highlights", "specifications", "images",
            "originalPrice", "offerPrice", "discount", "stock",
            "rating", "numReviews", "reviews",
            "isFeatured", "flashSale", "numSold",
            "createdAt", "updatedAt",
        ]
        read_only_fields = ["discount", "rating"]

    def get_images(self, obj):
        images = obj.images or []
        cleaned_images = []
        for img in images:
            if isinstance(img, dict):
                raw_url = img.get("url", "")
                alt = img.get("alt", obj.name)
            elif isinstance(img, str):
                raw_url = img
                alt = obj.name
            else:
                raw_url = ""
                alt = obj.name
            url = sanitize_image_url(raw_url, obj.brand or obj.name)
            cleaned_images.append({"url": url, "alt": alt})
        return cleaned_images


class TopProductSerializer(serializers.ModelSerializer):
    """Trimmed shape for /admin/reports/top-products (select: name brand offerPrice numSold images)."""

    images = serializers.SerializerMethodField()
    offerPrice = serializers.FloatField(source="offer_price")
    numSold = serializers.IntegerField(source="num_sold")

    class Meta:
        model = Product
        fields = ["_id", "name", "brand", "offerPrice", "numSold", "images"]

    def get_images(self, obj):
        images = obj.images or []
        cleaned_images = []
        for img in images:
            if isinstance(img, dict):
                raw_url = img.get("url", "")
                alt = img.get("alt", obj.name)
            elif isinstance(img, str):
                raw_url = img
                alt = obj.name
            else:
                raw_url = ""
                alt = obj.name
            url = sanitize_image_url(raw_url, obj.brand or obj.name)
            cleaned_images.append({"url": url, "alt": alt})
        return cleaned_images
