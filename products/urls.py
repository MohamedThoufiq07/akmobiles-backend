from django.urls import path
from . import views

# Specific routes before the <id> catch-all (same precedence as Express).
urlpatterns = [
    path("", views.products_root),                                        # GET list / POST create
    path("/bulk-archive", views.bulk_archive_products_view),              # POST bulk archive
    path("/bulk-restore", views.bulk_restore_products_view),              # POST bulk restore
    path("/upload-session", views.create_upload_session),                 # POST create upload session
    path("/upload-session/<str:token>/authorize-upload", views.authorize_upload_item), # POST authorize direct upload
    path("/upload-session/<str:token>/stage-local/<str:item_id>", views.stage_local_upload_item), # PUT local stage
    path("/upload-session/<str:token>/finalize-upload", views.finalize_upload_item), # POST finalize & verify upload
    path("/upload-session/<str:token>/stage", views.stage_upload_item),   # POST stage uploaded file
    path("/upload-session/<str:token>/items/<str:item_id>", views.remove_staged_item), # DELETE staged file
    path("/featured", views.get_featured),
    path("/top", views.get_top),
    path("/<str:product_id>/images/reorder", views.reorder_product_images), # PUT reorder images
    path("/<str:product_id>/images/<str:image_id>/primary", views.set_primary_product_image), # PUT set primary
    path("/<str:product_id>/images/<str:image_id>", views.delete_product_image), # DELETE product image
    path("/<str:product_id>/images", views.add_product_image),            # POST add product image
    path("/<str:product_id>/restore", views.restore_product),             # PUT restore product
    path("/<str:product_id>/related", views.get_related),
    path("/<str:product_id>/reviews/eligibility", views.get_review_eligibility),
    path("/<str:product_id>/reviews/eligibility/", views.get_review_eligibility),
    path("/<str:product_id>/reviews/<str:review_id>", views.product_review_detail),
    path("/<str:product_id>/reviews/<str:review_id>/", views.product_review_detail),
    path("/<str:product_id>/reviews", views.product_reviews),
    path("/<str:product_id>/reviews/", views.product_reviews),
    path("/<str:product_id>", views.product_detail),                      # GET / PUT / DELETE
]
