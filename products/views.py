"""
Product views — with multi-image support, upload sessions, and atomic image management.

Endpoints (under /api/products/):
  GET    /                                         getProducts (filters, sort, search, pagination)
  GET    /featured                                 getFeaturedProducts
  GET    /top                                      getTopProducts
  GET    /<id>                                     getProductById
  GET    /<id>/related                             getRelatedProducts
  POST   /<id>/reviews                             createReview            (auth)
  POST   /                                         createProduct           (admin)
  PUT    /<id>                                     updateProduct           (admin)
  DELETE /<id>                                     deleteProduct           (admin)

  Image Management Endpoints (admin):
  POST   /upload-session                           createUploadSession
  POST   /upload-session/<token>/stage             stageUploadItem
  DELETE /upload-session/<token>/items/<item_id>   removeStagedItem
  POST   /<id>/images                              addProductImage
  PUT    /<id>/images/reorder                      reorderProductImages
  PUT    /<id>/images/<image_id>/primary           setPrimaryProductImage
  DELETE /<id>/images/<image_id>                   deleteProductImage
"""

from datetime import timedelta
from functools import reduce
from operator import or_

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from common.permissions import IsAdmin, paginate_queryset
from common.storage import upload_to_blob_or_storage, delete_from_storage_safe
from .models import Product, ProductImage, ProductUploadSession, ProductUploadItem, Review
from .serializers import ProductSerializer, ProductImageSerializer

SORT_MAP = {
    "price_low": "offer_price",
    "price_high": "-offer_price",
    "popular": "-num_sold",
    "rating": "-rating",
    "newest": "-created_at",
}


def _is_admin(request):
    return request.user and request.user.is_authenticated and request.user.role == "admin"


def _sync_product_images(product, images_data, session_token=None):
    """
    Synchronizes ProductImage records for a product within a transaction.
    images_data can be a list of dicts: [{ url, alt, altText, storage_key, isPrimary, ... }]
    or strings.
    Ensures at most 10 images, correct sort_order, and exactly one primary image.
    """
    if images_data is None:
        return

    # If session token provided, commit staged items
    if session_token:
        session = ProductUploadSession.objects.filter(token=session_token, status="active").first()
        if session and session.is_valid():
            session.items.filter(is_committed=False).update(is_committed=True)
            session.status = "committed"
            session.product = product
            session.save(update_fields=["status", "product", "updated_at"])

    # Limit to maximum 10 images
    valid_images = images_data[:10]

    # If new images provided, sync them
    if valid_images:
        # Reset existing primary flags to avoid unique constraint collision during sync
        product.product_images.filter(is_primary=True).update(is_primary=False)
        existing_images = {img._id: img for img in product.product_images.all()}
        new_ids = []

        has_explicit_primary = any(
            isinstance(it, dict) and (it.get("isPrimary") or it.get("is_primary"))
            for it in valid_images
        )

        for index, item in enumerate(valid_images):
            if isinstance(item, dict):
                img_id = item.get("id") or item.get("_id")
                url = (item.get("url") or "").strip()
                alt = (item.get("altText") or item.get("alt") or product.name or "").strip()
                storage_key = (item.get("storage_key") or item.get("storageKey") or "").strip()
                is_pri = bool(item.get("isPrimary") or item.get("is_primary"))
            elif isinstance(item, str):
                img_id = None
                url = item.strip()
                alt = product.name or ""
                storage_key = ""
                is_pri = False
            else:
                continue

            if not url:
                continue

            # First image is default primary unless an explicit primary is set
            is_primary = is_pri if has_explicit_primary else (index == 0)

            if img_id and img_id in existing_images:
                img_obj = existing_images[img_id]
                img_obj.url = url
                img_obj.alt_text = alt[:255]
                img_obj.sort_order = index
                img_obj.is_primary = is_primary
                if storage_key:
                    img_obj.storage_key = storage_key
                img_obj.save()
                new_ids.append(img_obj._id)
            else:
                new_img = ProductImage.objects.create(
                    product=product,
                    url=url,
                    storage_key=storage_key,
                    alt_text=alt[:255],
                    sort_order=index,
                    is_primary=is_primary,
                )
                new_ids.append(new_img._id)

        # Remove deleted images
        for old_id, old_img in existing_images.items():
            if old_id not in new_ids:
                old_img.delete()

        # Guarantee exactly one primary image
        p_images = list(product.product_images.order_by("sort_order", "created_at"))
        if p_images:
            primary_count = sum(1 for img in p_images if img.is_primary)
            if primary_count != 1:
                # Set only the first as primary
                for idx, img in enumerate(p_images):
                    img.is_primary = (idx == 0)
                    img.sort_order = idx
                    img.save(update_fields=["is_primary", "sort_order", "updated_at"])


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def products_root(request):
    """
    GET:  paginated list with filters, sort, and search.
    POST: admin-only product create.
    """
    if request.method == "POST":
        if not _is_admin(request):
            return Response({"success": False, "message": "Admin only"}, status=403)

        with transaction.atomic():
            serializer = ProductSerializer(data=request.data)
            if serializer.is_valid():
                p = serializer.save()
                # Sync ProductImage rows if images supplied
                images_data = request.data.get("images")
                session_token = request.data.get("uploadSessionToken")
                if images_data is not None:
                    _sync_product_images(p, images_data, session_token=session_token)

                p_refreshed = Product.objects.prefetch_related("product_images", "reviews").filter(_id=p._id).first()
                return Response({"success": True, "product": ProductSerializer(p_refreshed).data}, status=201)
            return Response({"success": False, "errors": serializer.errors}, status=400)

    q = request.GET
    qs = Product.objects.all().prefetch_related("product_images", "reviews")

    brand = q.get("brand")
    if brand:
        qs = qs.filter(reduce(or_, (Q(brand__iexact=b) for b in brand.split(","))))

    category = q.get("category")
    if category:
        qs = qs.filter(reduce(or_, (Q(category__iexact=c) for c in category.split(","))))

    min_p, max_p = q.get("minPrice"), q.get("maxPrice")
    if min_p is not None and min_p != "":
        qs = qs.filter(offer_price__gte=float(min_p))
    if max_p is not None and max_p != "":
        qs = qs.filter(offer_price__lte=float(max_p))

    ram = q.get("ram")
    if ram:
        qs = qs.filter(reduce(or_, (Q(specifications__ram=v) for v in ram.split(","))))

    storage = q.get("storage")
    if storage:
        qs = qs.filter(reduce(or_, (Q(specifications__storage=v) for v in storage.split(","))))

    rating = q.get("rating")
    if rating:
        qs = qs.filter(rating__gte=float(rating))

    discount = q.get("discount")
    if discount:
        qs = qs.filter(discount__gte=float(discount))

    if q.get("flashSale") == "true":
        qs = qs.filter(flash_sale=True)

    search = (q.get("search") or q.get("keyword") or "").strip()
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(brand__icontains=search)
            | Q(category__icontains=search)
            | Q(description__icontains=search)
        )
        if not q.get("sort"):
            qs = qs.annotate(
                search_rank=Case(
                    When(name__iexact=search, then=Value(1)),
                    When(name__istartswith=search, then=Value(2)),
                    When(brand__iexact=search, then=Value(3)),
                    When(brand__istartswith=search, then=Value(4)),
                    When(name__icontains=search, then=Value(5)),
                    When(brand__icontains=search, then=Value(6)),
                    When(category__icontains=search, then=Value(7)),
                    default=Value(8),
                    output_field=IntegerField(),
                )
            ).order_by("search_rank", "-rating", "-created_at")
        else:
            qs = qs.order_by(SORT_MAP.get(q.get("sort"), "-created_at"))
    else:
        qs = qs.order_by(SORT_MAP.get(q.get("sort"), "-created_at"))

    items, page, pages, total = paginate_queryset(qs, q.get("page", 1), q.get("limit", 12))
    return Response({
        "success": True,
        "products": ProductSerializer(items, many=True).data,
        "page": page,
        "pages": pages,
        "total": total,
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def get_featured(request):
    products = Product.objects.filter(is_featured=True).prefetch_related("product_images", "reviews")[:8]
    return Response({"success": True, "products": ProductSerializer(products, many=True).data})


@api_view(["GET"])
@permission_classes([AllowAny])
def get_top(request):
    products = Product.objects.order_by("-rating").prefetch_related("product_images", "reviews")[:8]
    return Response({"success": True, "products": ProductSerializer(products, many=True).data})


@api_view(["GET"])
@permission_classes([AllowAny])
def get_related(request, product_id):
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found"}, status=404)
    related = (
        Product.objects.filter(Q(brand=product.brand) | Q(category=product.category))
        .exclude(_id=product._id)
        .prefetch_related("product_images", "reviews")[:5]
    )
    return Response({"success": True, "products": ProductSerializer(related, many=True).data})


@api_view(["GET", "PUT", "DELETE"])
def product_detail(request, product_id):
    product = Product.objects.filter(_id=product_id).prefetch_related("product_images", "reviews").first()

    if request.method == "GET":
        if not product:
            return Response({"success": False, "message": "Product not found"}, status=404)
        return Response({"success": True, "product": ProductSerializer(product).data})

    # PUT / DELETE require admin
    if not _is_admin(request):
        return Response(
            {"success": False, "message": "Access denied. Admin privileges required."}, status=403
        )
    if not product:
        return Response({"success": False, "message": "Product not found"}, status=404)

    if request.method == "DELETE":
        with transaction.atomic():
            product.delete()
        return Response({"success": True, "message": "Product deleted"})

    with transaction.atomic():
        serializer = ProductSerializer(product, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        images_data = request.data.get("images")
        session_token = request.data.get("uploadSessionToken")
        if images_data is not None:
            _sync_product_images(product, images_data, session_token=session_token)

    product.refresh_from_db()
    product_refreshed = Product.objects.prefetch_related("product_images", "reviews").filter(_id=product._id).first()
    return Response({"success": True, "product": ProductSerializer(product_refreshed).data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_review(request, product_id):
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found"}, status=404)

    if Review.objects.filter(product=product, user=request.user).exists():
        return Response(
            {"success": False, "message": "You have already reviewed this product."}, status=400
        )

    Review.objects.create(
        product=product,
        user=request.user,
        name=request.user.name,
        rating=int(request.data.get("rating")),
        comment=request.data.get("comment", ""),
    )
    product.recalculate_rating()
    return Response({"success": True, "message": "Review added"}, status=201)


# ------------------------------------------------------------------ Admin Image Management Endpoints

@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def create_upload_session(request):
    """
    POST /api/products/upload-session/
    Authorizes a temporary admin upload session valid for 30 minutes.
    """
    product_id = request.data.get("productId")
    product = Product.objects.filter(_id=product_id).first() if product_id else None

    session = ProductUploadSession.objects.create(
        user=request.user,
        product=product,
        expires_at=timezone.now() + timedelta(minutes=30),
    )
    return Response({
        "success": True,
        "token": session.token,
        "expiresAt": session.expires_at.isoformat(),
    }, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
@parser_classes([MultiPartParser, FormParser])
def stage_upload_item(request, token):
    """
    POST /api/products/upload-session/<token>/stage/
    Uploads and stages an image file inside the authorized session.
    """
    session = ProductUploadSession.objects.filter(token=token, status="active").first()
    if not session or not session.is_valid():
        return Response({"success": False, "message": "Invalid or expired upload session."}, status=401)

    file_obj = request.FILES.get("image") or request.FILES.get("file")
    if not file_obj:
        return Response({"success": False, "message": "No file provided."}, status=400)

    try:
        result = upload_to_blob_or_storage(file_obj, request=request)
        item = ProductUploadItem.objects.create(
            session=session,
            url=result["url"],
            storage_key=result["storage_key"],
            filename=file_obj.name,
            file_size=result.get("file_size"),
            content_type=result.get("content_type", "image/jpeg"),
        )
        return Response({
            "success": True,
            "item": {
                "id": item._id,
                "url": item.url,
                "storageKey": item.storage_key,
                "filename": item.filename,
                "fileSize": item.file_size,
            }
        }, status=201)
    except ValueError as val_err:
        return Response({"success": False, "message": str(val_err)}, status=400)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        logger.exception("Stage upload failed: %s", exc)
        return Response({"success": False, "message": str(exc)}, status=500)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def remove_staged_item(request, token, item_id):
    """
    DELETE /api/products/upload-session/<token>/items/<item_id>/
    Removes a staged item from the session.
    """
    session = ProductUploadSession.objects.filter(token=token, status="active").first()
    if not session or not session.is_valid():
        return Response({"success": False, "message": "Invalid or expired upload session."}, status=401)

    item = ProductUploadItem.objects.filter(session=session, _id=item_id, is_committed=False).first()
    if not item:
        return Response({"success": False, "message": "Item not found."}, status=404)

    delete_from_storage_safe(item.storage_key, item.url)
    item.delete()
    return Response({"success": True, "message": "Staged item removed."})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def add_product_image(request, product_id):
    """
    POST /api/products/<product_id>/images/
    Directly attaches an uploaded image metadata to an existing product.
    """
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found."}, status=404)

    if product.product_images.count() >= 10:
        return Response({"success": False, "message": "Maximum 10 images allowed per product."}, status=400)

    url = request.data.get("url")
    if not url:
        return Response({"success": False, "message": "Image URL is required."}, status=400)

    storage_key = request.data.get("storageKey") or request.data.get("storage_key") or ""
    alt_text = request.data.get("altText") or request.data.get("alt") or product.name

    with transaction.atomic():
        locked_product = Product.objects.select_for_update().filter(_id=product_id).first()
        existing_count = locked_product.product_images.count()
        is_first = existing_count == 0

        img = ProductImage.objects.create(
            product=locked_product,
            url=url.strip(),
            storage_key=storage_key.strip(),
            alt_text=alt_text.strip()[:255],
            sort_order=existing_count,
            is_primary=is_first,
        )

    return Response({"success": True, "image": ProductImageSerializer(img).data}, status=201)


@api_view(["PUT"])
@permission_classes([IsAuthenticated, IsAdmin])
def reorder_product_images(request, product_id):
    """
    PUT /api/products/<product_id>/images/reorder/
    Reorders images according to the array of image IDs provided.
    The first image in the reordered list becomes primary.
    """
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found."}, status=404)

    image_ids = request.data.get("imageIds") or request.data.get("image_ids") or []
    if not isinstance(image_ids, list):
        return Response({"success": False, "message": "imageIds array is required."}, status=400)

    with transaction.atomic():
        locked_product = Product.objects.select_for_update().filter(_id=product_id).first()
        # Reset primary flag first to avoid constraint collisions
        locked_product.product_images.filter(is_primary=True).update(is_primary=False)
        images = {img._id: img for img in locked_product.product_images.select_for_update().all()}

        for order, img_id in enumerate(image_ids):
            if img_id in images:
                img = images[img_id]
                img.sort_order = order
                img.is_primary = (order == 0)
                img.save(update_fields=["sort_order", "is_primary", "updated_at"])

    product_refreshed = Product.objects.prefetch_related("product_images").filter(_id=product_id).first()
    return Response({
        "success": True,
        "images": ProductImageSerializer(product_refreshed.product_images.all(), many=True).data,
    })


@api_view(["PUT"])
@permission_classes([IsAuthenticated, IsAdmin])
def set_primary_product_image(request, product_id, image_id):
    """
    PUT /api/products/<product_id>/images/<image_id>/primary/
    Sets the specified image as primary, promotes it to position 0, and updates subsequent order.
    """
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found."}, status=404)

    with transaction.atomic():
        locked_product = Product.objects.select_for_update().filter(_id=product_id).first()
        target_img = locked_product.product_images.select_for_update().filter(_id=image_id).first()
        if not target_img:
            return Response({"success": False, "message": "Image not found on this product."}, status=404)

        other_images = list(
            locked_product.product_images.select_for_update()
            .exclude(_id=image_id)
            .order_by("sort_order", "created_at")
        )

        # Reset primary on all images for this product first
        locked_product.product_images.filter(is_primary=True).update(is_primary=False)

        target_img.sort_order = 0
        target_img.is_primary = True
        target_img.save(update_fields=["sort_order", "is_primary", "updated_at"])

        for idx, img in enumerate(other_images, start=1):
            img.sort_order = idx
            img.is_primary = False
            img.save(update_fields=["sort_order", "is_primary", "updated_at"])

    product_refreshed = Product.objects.prefetch_related("product_images").filter(_id=product_id).first()
    return Response({
        "success": True,
        "primaryImage": ProductSerializer(product_refreshed).data["primaryImage"],
        "images": ProductImageSerializer(product_refreshed.product_images.all(), many=True).data,
    })


@api_view(["DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def delete_product_image(request, product_id, image_id):
    """
    DELETE /api/products/<product_id>/images/<image_id>/
    Deletes the ProductImage. If deleted image was primary, auto-promotes the next image to primary.
    """
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found."}, status=404)

    with transaction.atomic():
        locked_product = Product.objects.select_for_update().filter(_id=product_id).first()
        target_img = locked_product.product_images.select_for_update().filter(_id=image_id).first()
        if not target_img:
            return Response({"success": False, "message": "Image not found."}, status=404)

        was_primary = target_img.is_primary
        storage_key = target_img.storage_key
        img_url = target_img.url
        target_img.delete()

        # Re-normalize remaining images
        remaining = list(locked_product.product_images.select_for_update().order_by("sort_order", "created_at"))
        for idx, img in enumerate(remaining):
            img.sort_order = idx
            if was_primary and idx == 0:
                img.is_primary = True
            img.save(update_fields=["sort_order", "is_primary", "updated_at"])

        # Attempt safe storage delete if not in historical order
        delete_from_storage_safe(storage_key, img_url)

    product_refreshed = Product.objects.prefetch_related("product_images").filter(_id=product_id).first()
    return Response({
        "success": True,
        "message": "Image deleted.",
        "images": ProductImageSerializer(product_refreshed.product_images.all(), many=True).data,
    })
