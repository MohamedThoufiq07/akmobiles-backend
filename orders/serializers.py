"""
Order serializer.

The Node controllers `.populate()` different fields per endpoint:
  getMyOrders   -> orderItems.product (name, images)
  getOrderById  -> user (name,email) + orderItems.product (name, images, brand)
  getAllOrders  -> user (name, email)
We reproduce this via serializer context flags so each endpoint returns the same
shape it did on the MERN stack.
"""

from rest_framework import serializers

from common.utils import sanitize_image_url
from products.models import Product
from .models import Order


class OrderSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    orderItems = serializers.SerializerMethodField()
    shippingAddress = serializers.JSONField(source="shipping_address")
    paymentInfo = serializers.JSONField(source="payment_info")
    itemsPrice = serializers.FloatField(source="items_price")
    taxPrice = serializers.FloatField(source="tax_price")
    shippingPrice = serializers.FloatField(source="shipping_price")
    totalPrice = serializers.FloatField(source="total_price")
    orderStatus = serializers.CharField(source="order_status")
    statusHistory = serializers.JSONField(source="status_history")
    estimatedDelivery = serializers.DateTimeField(source="estimated_delivery")
    deliveredAt = serializers.DateTimeField(source="delivered_at")
    createdAt = serializers.DateTimeField(source="created_at")
    updatedAt = serializers.DateTimeField(source="updated_at")

    class Meta:
        model = Order
        fields = [
            "_id", "user", "orderItems", "shippingAddress", "paymentInfo",
            "itemsPrice", "taxPrice", "shippingPrice", "totalPrice",
            "orderStatus", "statusHistory", "estimatedDelivery", "deliveredAt",
            "createdAt", "updatedAt",
        ]

    def get_user(self, obj):
        if self.context.get("populate_user"):
            return {"_id": obj.user_id, "name": obj.user.name, "email": obj.user.email}
        return obj.user_id

    def get_orderItems(self, obj):
        fields = self.context.get("populate_item_fields")
        
        result = []
        ids = [it.get("product") for it in obj.order_items if it.get("product")]
        products = {p._id: p for p in Product.objects.filter(_id__in=ids)} if fields else {}

        for item in obj.order_items:
            item = dict(item)
            # Clean up the image field
            img_url = item.get("image", "")
            item["image"] = sanitize_image_url(img_url, item.get("name", "Product"))
            
            if fields:
                prod = products.get(item.get("product"))
                if prod:
                    populated = {"_id": prod._id}
                    if "name" in fields:
                        populated["name"] = prod.name
                    if "images" in fields:
                        cleaned_images = []
                        for p_img in (prod.images or []):
                            if isinstance(p_img, dict):
                                p_url = p_img.get("url", "")
                                p_alt = p_img.get("alt", prod.name)
                            elif isinstance(p_img, str):
                                p_url = p_img
                                p_alt = prod.name
                            else:
                                p_url = ""
                                p_alt = prod.name
                            cleaned_images.append({
                                "url": sanitize_image_url(p_url, prod.brand or prod.name),
                                "alt": p_alt,
                            })
                        populated["images"] = cleaned_images
                    if "brand" in fields:
                        populated["brand"] = prod.brand
                    item["product"] = populated
            result.append(item)
        return result

    def to_representation(self, instance):
        import json

        data = super().to_representation(instance)
        # Ensure shippingAddress is always a valid dict
        ship = data.get("shippingAddress")
        if isinstance(ship, str):
            try:
                data["shippingAddress"] = json.loads(ship)
            except Exception:
                data["shippingAddress"] = {}
        elif not isinstance(ship, dict) or ship is None:
            data["shippingAddress"] = {}

        # Canonical Payment source of truth from Payment model
        payment_dict = data.get("paymentInfo") or {}
        if isinstance(payment_dict, str):
            try:
                payment_dict = json.loads(payment_dict)
            except Exception:
                payment_dict = {}
        if not isinstance(payment_dict, dict) or payment_dict is None:
            payment_dict = {}

        # Use related Payment model if available
        try:
            payment_obj = instance.payment
        except Exception:
            payment_obj = None

        if payment_obj:
            payment_dict["status"] = payment_obj.status
            payment_dict["method"] = payment_dict.get("method") or "Razorpay"
            if payment_obj.razorpay_order_id:
                payment_dict["razorpayOrderId"] = payment_obj.razorpay_order_id
            if payment_obj.razorpay_payment_id:
                payment_dict["razorpayPaymentId"] = payment_obj.razorpay_payment_id
            if payment_obj.paid_at:
                payment_dict["paidAt"] = payment_obj.paid_at.isoformat()

        data["paymentInfo"] = payment_dict

        # Ensure statusHistory is always a list
        hist = data.get("statusHistory")
        if isinstance(hist, str):
            try:
                data["statusHistory"] = json.loads(hist)
            except Exception:
                data["statusHistory"] = []
        elif not isinstance(hist, list) or hist is None:
            data["statusHistory"] = []

        return data
