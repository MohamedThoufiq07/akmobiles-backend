"""
Serializers for the accounts app.

The frontend expects the user object shaped exactly like the Mongo response:
  { _id, name, email, role, phone, addresses, wishlist }
and on /profile additionally: createdAt (and a *populated* wishlist).
"""

from rest_framework import serializers

from .models import User


class RegisterSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=50)
    email = serializers.EmailField()
    password = serializers.CharField(min_length=6, write_only=True)

    def validate_email(self, value):
        value = value.lower()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def create(self, validated):
        return User.objects.create_user(
            email=validated["email"],
            password=validated["password"],
            name=validated["name"],
        )


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class UserSerializer(serializers.ModelSerializer):
    """Standard user object (wishlist = list of product _id strings, like raw Mongo)."""

    wishlist = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["_id", "name", "email", "role", "phone", "addresses", "wishlist"]

    def get_wishlist(self, obj):
        return list(obj.wishlist.values_list("_id", flat=True))


class UserProfileSerializer(serializers.ModelSerializer):
    """/profile response: wishlist populated with full product objects + createdAt."""

    wishlist = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = User
        fields = ["_id", "name", "email", "role", "phone", "addresses", "wishlist", "createdAt"]

    def get_wishlist(self, obj):
        from products.serializers import ProductSerializer  # lazy import avoids cycle
        return ProductSerializer(obj.wishlist.all(), many=True).data


class UserAdminSerializer(serializers.ModelSerializer):
    """Used by the admin user-list endpoints (no password, with timestamps)."""

    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = User
        fields = ["_id", "name", "email", "role", "phone", "addresses", "createdAt", "updatedAt"]
