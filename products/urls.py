from django.urls import path
from . import views

# Specific routes before the <id> catch-all (same precedence as Express).
urlpatterns = [
    path("", views.products_root),                        # GET list / POST create
    path("/featured", views.get_featured),
    path("/top", views.get_top),
    path("/<str:product_id>", views.product_detail),      # GET / PUT / DELETE
    path("/<str:product_id>/related", views.get_related),
    path("/<str:product_id>/reviews", views.create_review),
]
