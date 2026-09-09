from django.urls import path
from . import views

urlpatterns = [
    path("", views.orders_root),                  # POST create / GET all (admin)
    path("/myorders", views.my_orders),
    path("/stats", views.order_stats),
    path("/<str:order_id>/invoice", views.order_invoice),
    path("/<str:order_id>", views.order_detail),
    path("/<str:order_id>/status", views.update_status),
]
