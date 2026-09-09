from django.urls import path
from . import views

urlpatterns = [
    path("razorpay/create-order/", views.create_razorpay_order),
    path("razorpay/verify-payment/", views.verify_payment),
    path("razorpay/checkout-dismissed/", views.checkout_dismissed),
    path("razorpay/expire-stale/", views.expire_stale_payments),
    path("razorpay/webhook/", views.razorpay_webhook),
]
