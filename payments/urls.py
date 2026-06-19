from django.urls import path
from . import views

urlpatterns = [
    path("/key", views.razorpay_key),
    path("/create-order", views.create_order),
    path("/verify", views.verify_payment),
]
