"""
Serializers for products. Field names are emitted in the camelCase format
the frontend uses (originalPrice, offerPrice, deliveryCharge, primaryImage, images, ...).
"""

from decimal import Decimal
from rest_framework import serializers

from common.utils import sanitize_image_url
from .models import Product, ProductImage, Review


class ReviewSerializer(serializers.ModelSerializer):
    user = serializers.CharField(source="user_id", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Review
        fields = ["_id", "user", "name", "rating", "comment", "createdAt", "updatedAt"]


class ProductImageSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="_id", read_only=True)
    altText = serializers.CharField(source="alt_text", default="")
    sortOrder = serializers.IntegerField(source="sort_order", default=0)
    isPrimary = serializers.BooleanField(source="is_primary", default=False)
    contentType = serializers.CharField(source="content_type", read_only=True)
    fileSize = serializers.IntegerField(source="file_size", read_only=True)

    class Meta:
        model = ProductImage
        fields = ["id", "url", "altText", "sortOrder", "isPrimary", "contentType", "fileSize"]


class ProductSerializer(serializers.ModelSerializer):
    reviews = ReviewSerializer(many=True, read_only=True)
    primaryImage = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()
    originalPrice = serializers.FloatField(source="original_price")
    offerPrice = serializers.FloatField(source="offer_price")
    deliveryCharge = serializers.SerializerMethodField()
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
            "highlights", "specifications", "primaryImage", "images",
            "originalPrice", "offerPrice", "deliveryCharge", "discount", "stock",
            "rating", "numReviews", "reviews",
            "isFeatured", "flashSale", "numSold",
            "createdAt", "updatedAt",
        ]
        read_only_fields = ["discount", "rating", "primaryImage"]

    def get_deliveryCharge(self, obj):
        charge = getattr(obj, "delivery_charge", Decimal("49.00"))
        try:
            return f"{Decimal(str(charge)):.2f}"
        except Exception:
            return "49.00"

    def get_primaryImage(self, obj):
        # 1. Try relational ProductImage prefetch
        if hasattr(obj, "_prefetched_objects_cache") and "product_images" in obj._prefetched_objects_cache:
            p_images = list(obj.product_images.all())
        else:
            p_images = list(obj.product_images.order_by("sort_order", "created_at"))

        if p_images:
            primary = next((img for img in p_images if img.is_primary), p_images[0])
            sanitized_url = sanitize_image_url(primary.url, obj.brand or obj.name)
            return {
                "id": primary._id,
                "url": sanitized_url,
                "altText": primary.alt_text or obj.name,
                "sortOrder": primary.sort_order,
                "isPrimary": primary.is_primary,
            }

        # 2. Fallback to legacy images JSON
        legacy = obj.images or []
        if legacy:
            first = legacy[0]
            raw_url = first.get("url", "") if isinstance(first, dict) else (first if isinstance(first, str) else "")
            alt = first.get("alt", obj.name) if isinstance(first, dict) else obj.name
            sanitized_url = sanitize_image_url(raw_url, obj.brand or obj.name)
            return {
                "id": "legacy-0",
                "url": sanitized_url,
                "altText": alt,
                "sortOrder": 0,
                "isPrimary": True,
            }

        fallback_url = sanitize_image_url("", obj.brand or obj.name)
        return {
            "id": "placeholder",
            "url": fallback_url,
            "altText": obj.name,
            "sortOrder": 0,
            "isPrimary": True,
        }

    def get_images(self, obj):
        # 1. Try relational ProductImage
        if hasattr(obj, "_prefetched_objects_cache") and "product_images" in obj._prefetched_objects_cache:
            p_images = list(obj.product_images.all())
        else:
            p_images = list(obj.product_images.order_by("sort_order", "created_at"))

        if p_images:
            result = []
            for img in p_images:
                sanitized_url = sanitize_image_url(img.url, obj.brand or obj.name)
                result.append({
                    "id": img._id,
                    "url": sanitized_url,
                    "altText": img.alt_text or obj.name,
                    "sortOrder": img.sort_order,
                    "isPrimary": img.is_primary,
                })
            return result

        # 2. Fallback to legacy images
        images = obj.images or []
        cleaned_images = []
        for idx, img in enumerate(images):
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
            cleaned_images.append({
                "id": f"legacy-{idx}",
                "url": url,
                "altText": alt,
                "sortOrder": idx,
                "isPrimary": idx == 0,
            })
        return cleaned_images

    def to_internal_value(self, data):
        ret = super().to_internal_value(data)
        if "deliveryCharge" in data:
            try:
                ret["delivery_charge"] = Decimal(str(data["deliveryCharge"]))
            except Exception:
                ret["delivery_charge"] = Decimal("49.00")
        return ret


class TopProductSerializer(serializers.ModelSerializer):
    """Trimmed shape for /admin/reports/top-products."""
    primaryImage = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()
    offerPrice = serializers.FloatField(source="offer_price")
    deliveryCharge = serializers.SerializerMethodField()
    numSold = serializers.IntegerField(source="num_sold")

    class Meta:
        model = Product
        fields = ["_id", "name", "brand", "offerPrice", "deliveryCharge", "numSold", "primaryImage", "images"]

    def get_deliveryCharge(self, obj):
        charge = getattr(obj, "delivery_charge", Decimal("49.00"))
        try:
            return f"{Decimal(str(charge)):.2f}"
        except Exception:
            return "49.00"

    def get_primaryImage(self, obj):
        p_images = list(obj.product_images.all()) if hasattr(obj, "product_images") else []
        if p_images:
            primary = next((img for img in p_images if img.is_primary), p_images[0])
            return {
                "id": primary._id,
                "url": sanitize_image_url(primary.url, obj.brand or obj.name),
                "altText": primary.alt_text or obj.name,
                "sortOrder": primary.sort_order,
                "isPrimary": primary.is_primary,
            }
        legacy = obj.images or []
        if legacy:
            first = legacy[0]
            raw_url = first.get("url", "") if isinstance(first, dict) else (first if isinstance(first, str) else "")
            return {
                "id": "legacy-0",
                "url": sanitize_image_url(raw_url, obj.brand or obj.name),
                "altText": obj.name,
                "sortOrder": 0,
                "isPrimary": True,
            }
        return {
            "id": "placeholder",
            "url": sanitize_image_url("", obj.brand or obj.name),
            "altText": obj.name,
            "sortOrder": 0,
            "isPrimary": True,
        }

    def get_images(self, obj):
        p_images = list(obj.product_images.all()) if hasattr(obj, "product_images") else []
        if p_images:
            return [
                {
                    "id": img._id,
                    "url": sanitize_image_url(img.url, obj.brand or obj.name),
                    "altText": img.alt_text or obj.name,
                    "sortOrder": img.sort_order,
                    "isPrimary": img.is_primary,
                }
                for img in p_images
            ]
        images = obj.images or []
        cleaned_images = []
        for idx, img in enumerate(images):
            raw_url = img.get("url", "") if isinstance(img, dict) else (img if isinstance(img, str) else "")
            alt = img.get("alt", obj.name) if isinstance(img, dict) else obj.name
            cleaned_images.append({
                "id": f"legacy-{idx}",
                "url": sanitize_image_url(raw_url, obj.brand or obj.name),
                "altText": alt,
                "sortOrder": idx,
                "isPrimary": idx == 0,
            })
        return cleaned_images
