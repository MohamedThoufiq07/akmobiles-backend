from django.urls import path
from . import views

urlpatterns = [
    path("/register", views.register),
    path("/login", views.login),
    path("/profile", views.profile),
    path("/forgot-password", views.forgot_password),
    path("/reset-password", views.reset_password),
    path("/wishlist/<str:product_id>", views.toggle_wishlist),
]
