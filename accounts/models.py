"""
User model — port of models/User.js.

Mongo fields -> Django:
  _id            -> CharField primary key (24-hex, ObjectId-style)
  name           -> name
  email          -> email (unique, lowercase, login field)
  password       -> Django password (set_password / check_password)
  phone          -> phone
  role           -> role  (user | admin)
  addresses[]    -> JSONField (embedded list, same shape as Mongo subdocs)
  wishlist[]     -> ManyToMany(Product); serialized as ObjectId list or populated objects
  resetPassword* -> reset_password_token / reset_password_expire
  timestamps     -> created_at / updated_at  (output as createdAt / updatedAt)
"""

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from common.utils import generate_object_id
from .managers import UserManager

ROLE_CHOICES = (("user", "user"), ("admin", "admin"))


class User(AbstractBaseUser, PermissionsMixin):
    _id = models.CharField(primary_key=True, max_length=24, default=generate_object_id, editable=False)

    name = models.CharField(max_length=50)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True, default="")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="user")

    # Embedded address subdocuments stored as JSON (list of dicts):
    # { addressLine1, addressLine2, city, state, postalCode, isDefault }
    addresses = models.JSONField(default=list, blank=True)

    wishlist = models.ManyToManyField("products.Product", blank=True, related_name="wishlisted_by")

    reset_password_token = models.CharField(max_length=128, null=True, blank=True)
    reset_password_expire = models.DateTimeField(null=True, blank=True)

    # Django admin plumbing
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        db_table = "users"
        ordering = ["-created_at"]

    def __str__(self):
        return self.email
