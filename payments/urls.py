from django.urls import path
from . import views

urlpatterns = [
    path("razorpay/create-order/", views.create_razorpay_order),
    path("razorpay/verify-payment/", views.verify_payment),
    path("razorpay/webhook/", views.razorpay_webhook),
]
