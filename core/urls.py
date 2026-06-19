from django.urls import path
from . import views

# Mounted under /api (see akmobiles/urls.py)
urlpatterns = [
    path("/contact", views.contact),
    path("/newsletter/subscribe", views.newsletter_subscribe),
    path("/settings", views.store_settings),
    path("/users", views.all_users),
    path("/users/<str:user_id>", views.user_by_id),
    path("/admin/dashboard", views.dashboard_stats),
    path("/admin/reports/sales", views.sales_report),
    path("/admin/reports/top-products", views.top_products),
    path("/upload", views.upload_image),
]
