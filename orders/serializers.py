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
        if not fields:
            return obj.order_items

        ids = [it.get("product") for it in obj.order_items if it.get("product")]
        products = {p._id: p for p in Product.objects.filter(_id__in=ids)}

        result = []
        for item in obj.order_items:
            item = dict(item)
            prod = products.get(item.get("product"))
            if prod:
                populated = {"_id": prod._id}
                if "name" in fields:
                    populated["name"] = prod.name
                if "images" in fields:
                    populated["images"] = prod.images
                if "brand" in fields:
                    populated["brand"] = prod.brand
                item["product"] = populated
            result.append(item)
        return result
