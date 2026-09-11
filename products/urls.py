from django.urls import path
from . import views

# Specific routes before the <id> catch-all (same precedence as Express).
urlpatterns = [
    path("", views.products_root),                                        # GET list / POST create
    path("/upload-session", views.create_upload_session),                 # POST create upload session
    path("/upload-session/<str:token>/stage", views.stage_upload_item),   # POST stage uploaded file
    path("/upload-session/<str:token>/items/<str:item_id>", views.remove_staged_item), # DELETE staged file
    path("/featured", views.get_featured),
    path("/top", views.get_top),
    path("/<str:product_id>/images/reorder", views.reorder_product_images), # PUT reorder images
    path("/<str:product_id>/images/<str:image_id>/primary", views.set_primary_product_image), # PUT set primary
    path("/<str:product_id>/images/<str:image_id>", views.delete_product_image), # DELETE product image
    path("/<str:product_id>/images", views.add_product_image),            # POST add product image
    path("/<str:product_id>/related", views.get_related),
    path("/<str:product_id>/reviews", views.create_review),
    path("/<str:product_id>", views.product_detail),                      # GET / PUT / DELETE
]
