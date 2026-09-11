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

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from django.conf import settings
from common.permissions import IsAdmin, paginate_queryset
from common.storage import (
    generate_staging_key,
    finalize_staged_upload_to_permanent,
    delete_from_storage_safe,
    get_blob_token,
    validate_and_decode_image,
    put_to_vercel_blob,
    generate_permanent_key,
    StorageBaseException,
    StorageValidationError,
    StorageConfigError,
    StorageUpstreamError,
    StorageTimeoutError,
)
from .constants import (
    MIN_PRODUCT_IMAGES,
    MAX_PRODUCT_IMAGES,
    MAX_PRODUCT_IMAGE_BYTES,
    ACCEPTED_IMAGE_FORMATS,
)
from .models import Product, ProductImage, ProductUploadSession, ProductUploadItem, Review
from .serializers import ProductSerializer, AdminProductSerializer, ProductImageSerializer
from .services import (
    archive_single_product,
    bulk_archive_products,
    bulk_archive_all_filtered,
    restore_single_product,
    bulk_restore_products,
    bulk_restore_all_filtered,
)

logger = logging.getLogger(__name__)


def generate_upload_grant(item_id, session_token, user_id, is_staff, pathname, expires_in=300):
    grant_payload = {
        "staged_item_id": str(item_id),
        "session_token": str(session_token),
        "user_id": str(user_id) if user_id else "",
        "is_staff": bool(is_staff),
        "pathname": str(pathname),
        "exp": int(time.time()) + expires_in,
        "allowed_types": ["image/jpeg", "image/png", "image/webp"],
        "max_size": MAX_PRODUCT_IMAGE_BYTES,
    }
    blob_token = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
    secret = os.environ.get("UPLOAD_GRANT_SECRET", "").strip() or blob_token or settings.SECRET_KEY
    payload_json = json.dumps(grant_payload, separators=(',', ':'), sort_keys=True)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode('utf-8')).decode('utf-8').rstrip('=')
    sig = hmac.new(secret.encode('utf-8'), payload_b64.encode('utf-8'), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


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
    Ensures between MIN_PRODUCT_IMAGES and MAX_PRODUCT_IMAGES images, distinct IDs, correct sort_order,
    and exactly one primary image (lowest sort order ready image by default).
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

    # Enforce maximum images limit
    valid_images = images_data[:MAX_PRODUCT_IMAGES]

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

            if not url or "staging/" in url or url.endswith(".upload"):
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
    GET:  paginated list with filters, sort, and search (defaults to active products).
          Admin requests support status=active|archived|all and return AdminProductSerializer.
    POST: admin-only product create.
    """
    if request.method == "POST":
        if not _is_admin(request):
            return Response({"success": False, "message": "Admin only"}, status=403)

        images_data = request.data.get("images")
        if images_data is not None and isinstance(images_data, list):
            if len(images_data) > MAX_PRODUCT_IMAGES:
                return Response({
                    "code": "MAX_IMAGES_EXCEEDED",
                    "message": f"A product can contain a maximum of {MAX_PRODUCT_IMAGES} images.",
                    "file_name": "",
                    "request_id": uuid.uuid4().hex,
                }, status=400)

        with transaction.atomic():
            serializer = ProductSerializer(data=request.data)
            if serializer.is_valid():
                p = serializer.save()
                # Sync ProductImage rows if images supplied
                session_token = request.data.get("uploadSessionToken")
                if images_data is not None:
                    _sync_product_images(p, images_data, session_token=session_token)

                p_refreshed = Product.objects.prefetch_related("product_images", "reviews").filter(_id=p._id).first()
                return Response({"success": True, "product": AdminProductSerializer(p_refreshed).data}, status=201)
            return Response({"success": False, "errors": serializer.errors}, status=400)

    q = request.GET
    is_admin_req = _is_admin(request)
    status_filter = (q.get("status") or "active").lower()

    if is_admin_req:
        if status_filter == "archived":
            qs = Product.objects.filter(is_active=False)
        elif status_filter == "all":
            qs = Product.objects.all()
        else:
            qs = Product.objects.filter(is_active=True)
    else:
        qs = Product.objects.filter(is_active=True)

    qs = qs.prefetch_related("product_images", "reviews")

    brand = q.get("brand")
    if brand:
        qs = qs.filter(reduce(or_, (Q(brand__iexact=b) for b in brand.split(","))))

    category = q.get("category")
    if category:
        qs = qs.filter(reduce(or_, (Q(category__iexact=c) for c in category.split(","))))

    min_p, max_p = q.get("minPrice"), q.get("maxPrice")
    if min_p is not None and min_p != "":
        try:
            qs = qs.filter(offer_price__gte=float(min_p))
        except (ValueError, TypeError):
            pass
    if max_p is not None and max_p != "":
        try:
            qs = qs.filter(offer_price__lte=float(max_p))
        except (ValueError, TypeError):
            pass

    ram = q.get("ram")
    if ram:
        qs = qs.filter(reduce(or_, (Q(specifications__ram=v) for v in ram.split(","))))

    storage = q.get("storage")
    if storage:
        qs = qs.filter(reduce(or_, (Q(specifications__storage=v) for v in storage.split(","))))

    rating = q.get("rating")
    if rating:
        try:
            qs = qs.filter(rating__gte=float(rating))
        except (ValueError, TypeError):
            pass

    discount = q.get("discount")
    if discount:
        try:
            qs = qs.filter(discount__gte=float(discount))
        except (ValueError, TypeError):
            pass

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
    serializer_class = AdminProductSerializer if is_admin_req else ProductSerializer
    return Response({
        "success": True,
        "products": serializer_class(items, many=True).data,
        "page": page,
        "pages": pages,
        "total": total,
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def get_featured(request):
    products = Product.objects.filter(is_active=True, is_featured=True).prefetch_related("product_images", "reviews")[:8]
    return Response({"success": True, "products": ProductSerializer(products, many=True).data})


@api_view(["GET"])
@permission_classes([AllowAny])
def get_top(request):
    products = Product.objects.filter(is_active=True).order_by("-rating").prefetch_related("product_images", "reviews")[:8]
    return Response({"success": True, "products": ProductSerializer(products, many=True).data})


@api_view(["GET"])
@permission_classes([AllowAny])
def get_related(request, product_id):
    product = Product.objects.filter(_id=product_id, is_active=True).first()
    if not product:
        return Response({"success": False, "message": "Product not found"}, status=404)
    related = (
        Product.objects.filter(is_active=True)
        .filter(Q(brand=product.brand) | Q(category=product.category))
        .exclude(_id=product._id)
        .prefetch_related("product_images", "reviews")[:5]
    )
    return Response({"success": True, "products": ProductSerializer(related, many=True).data})


@api_view(["GET", "PUT", "DELETE"])
def product_detail(request, product_id):
    product = Product.objects.filter(_id=product_id).prefetch_related("product_images", "reviews").first()
    is_admin_req = _is_admin(request)

    if request.method == "GET":
        if not product or (not product.is_active and not is_admin_req):
            return Response({"success": False, "message": "Product not found"}, status=404)
        serializer_class = AdminProductSerializer if is_admin_req else ProductSerializer
        return Response({"success": True, "product": serializer_class(product).data})

    # PUT / DELETE require admin
    if not is_admin_req:
        return Response(
            {"success": False, "message": "Access denied. Admin privileges required."}, status=403
        )
    if not product:
        return Response({"success": False, "message": "Product not found"}, status=404)

    if request.method == "DELETE":
        archive_single_product(product, user=request.user)
        return Response({
            "success": True,
            "code": "PRODUCT_ARCHIVED",
            "message": f'"{product.name}" was archived successfully.',
        })

    images_data = request.data.get("images")
    if images_data is not None and isinstance(images_data, list):
        if len(images_data) > MAX_PRODUCT_IMAGES:
            return Response({
                "code": "MAX_IMAGES_EXCEEDED",
                "message": f"A product can contain a maximum of {MAX_PRODUCT_IMAGES} images.",
                "file_name": "",
                "request_id": uuid.uuid4().hex,
            }, status=400)

    with transaction.atomic():
        serializer = ProductSerializer(product, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        session_token = request.data.get("uploadSessionToken")
        if images_data is not None:
            _sync_product_images(product, images_data, session_token=session_token)

    product.refresh_from_db()
    product_refreshed = Product.objects.prefetch_related("product_images", "reviews").filter(_id=product._id).first()
    return Response({"success": True, "product": AdminProductSerializer(product_refreshed).data})


@api_view(["PUT"])
@permission_classes([IsAuthenticated, IsAdmin])
def restore_product(request, product_id):
    """
    PUT /api/products/<product_id>/restore/
    Admin-only endpoint to restore a single archived product from Trash.
    """
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found"}, status=404)

    restore_single_product(product)
    product.refresh_from_db()
    product_refreshed = Product.objects.prefetch_related("product_images", "reviews").filter(_id=product._id).first()
    return Response({
        "success": True,
        "code": "PRODUCT_RESTORED",
        "message": f'"{product.name}" was restored successfully.',
        "product": AdminProductSerializer(product_refreshed).data,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def bulk_archive_products_view(request):
    """
    POST /api/products/bulk-archive/
    Admin-only endpoint to bulk-archive products safely by IDs or filtered selection.
    """
    selection_mode = request.data.get("selection_mode") or request.data.get("selectionMode")
    filters = request.data.get("filters")
    excluded_ids = request.data.get("excluded_ids") or request.data.get("excludedIds") or []
    product_ids = request.data.get("product_ids") or request.data.get("productIds")

    if selection_mode == "all_filtered":
        count = bulk_archive_all_filtered(filters=filters, excluded_ids=excluded_ids, user=request.user)
    else:
        if not product_ids or not isinstance(product_ids, list):
            return Response({
                "code": "EMPTY_SELECTION",
                "message": "Please select at least one product to archive.",
            }, status=400)
        count = bulk_archive_products(product_ids, user=request.user)

    return Response({
        "success": True,
        "code": "PRODUCTS_ARCHIVED",
        "archived_count": count,
        "message": f"{count} product{'s' if count != 1 else ''} {'were' if count != 1 else 'was'} archived successfully.",
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def bulk_restore_products_view(request):
    """
    POST /api/products/bulk-restore/
    Admin-only endpoint to bulk-restore products from Trash.
    """
    selection_mode = request.data.get("selection_mode") or request.data.get("selectionMode")
    filters = request.data.get("filters")
    excluded_ids = request.data.get("excluded_ids") or request.data.get("excludedIds") or []
    product_ids = request.data.get("product_ids") or request.data.get("productIds")

    if selection_mode == "all_filtered":
        count = bulk_restore_all_filtered(filters=filters, excluded_ids=excluded_ids)
    else:
        if not product_ids or not isinstance(product_ids, list):
            return Response({
                "code": "EMPTY_SELECTION",
                "message": "Please select at least one product to restore.",
            }, status=400)
        count = bulk_restore_products(product_ids)

    return Response({
        "success": True,
        "code": "PRODUCTS_RESTORED",
        "restored_count": count,
        "message": f"{count} product{'s' if count != 1 else ''} {'were' if count != 1 else 'was'} restored successfully.",
    })


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
def authorize_upload_item(request, token):
    """
    POST /api/products/upload-session/<token>/authorize-upload/
    Issues an authorized direct-upload staging URL and registers a staged item.
    Enforces maximum 5 product images under concurrency locking.
    """
    req_id = uuid.uuid4().hex
    try:
        with transaction.atomic():
            session = ProductUploadSession.objects.select_for_update().filter(token=token, status="active").first()
            if not session or not session.is_valid():
                return Response({
                    "code": "SESSION_EXPIRED",
                    "message": "Upload session expired or invalid.",
                    "request_id": req_id,
                }, status=409)

            raw_filename = (
                request.data.get("filename")
                or request.data.get("fileName")
                or "image.jpg"
            ).strip()
            safe_filename = raw_filename[:255]

            # Calculate total active staged items + target product images
            staged_count = session.items.select_for_update().filter(is_committed=False).count()
            product_images_count = 0
            if session.product:
                product_images_count = session.product.product_images.select_for_update().count()

            if staged_count + product_images_count >= MAX_PRODUCT_IMAGES:
                return Response({
                    "code": "MAX_IMAGES_EXCEEDED",
                    "message": f"A product can contain a maximum of {MAX_PRODUCT_IMAGES} images.",
                    "file_name": safe_filename,
                    "request_id": req_id,
                }, status=400)

            # Determine canonical extension and MIME from filename or contentType
            content_type_req = (
                request.data.get("contentType")
                or request.data.get("content_type")
                or ""
            ).lower().strip()
            _, raw_ext = os.path.splitext(safe_filename)
            raw_ext = raw_ext.lower()

            if content_type_req == "image/png" or raw_ext == ".png":
                canonical_ext = ".png"
                canonical_mime = "image/png"
            elif content_type_req == "image/webp" or raw_ext == ".webp":
                canonical_ext = ".webp"
                canonical_mime = "image/webp"
            else:
                canonical_ext = ".jpg"
                canonical_mime = "image/jpeg"

            item_id = uuid.uuid4().hex[:24]
            staging_key = generate_staging_key(session.token, item_id=item_id, canonical_ext=canonical_ext)
            blob_token = get_blob_token()
            user_id = str(getattr(request.user, "_id", None) or getattr(request.user, "pk", "") or "")
            is_staff = bool(request.user and request.user.is_staff)
            upload_grant = generate_upload_grant(
                item_id=item_id,
                session_token=session.token,
                user_id=user_id,
                is_staff=is_staff,
                pathname=staging_key,
            )

            upload_mode = "blob" if blob_token else "local"
            local_stage_url = (
                request.build_absolute_uri(f"/api/products/upload-session/{token}/stage-local/{item_id}")
                if not blob_token
                else None
            )

            item = ProductUploadItem.objects.create(
                _id=item_id,
                session=session,
                url=local_stage_url or "",
                storage_key=staging_key,
                filename=safe_filename,
                content_type="application/octet-stream",
                is_committed=False,
            )

            return Response({
                "success": True,
                "mode": upload_mode,
                "pathname": staging_key,
                "stagedItemId": item._id,
                "stagingKey": staging_key,
                "uploadGrant": upload_grant,
                "uploadUrl": local_stage_url or staging_key,
                "handleUploadUrl": "/api/product-image-upload",
                "item": {
                    "id": item._id,
                    "storageKey": staging_key,
                    "uploadUrl": local_stage_url or staging_key,
                    "filename": safe_filename,
                    "status": "uploading",
                },
            }, status=201)
    except Exception as exc:
        logger.exception("[%s] Unexpected error in authorize_upload_item: %s", req_id, exc)
        return Response({
            "code": "INTERNAL_ERROR",
            "message": str(exc),
            "request_id": req_id,
        }, status=500)


@api_view(["PUT", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def stage_local_upload_item(request, token, item_id):
    """
    PUT /api/products/upload-session/<token>/stage-local/<item_id>/
    Development/test fallback handler for direct binary PUT streams.
    """
    session = ProductUploadSession.objects.filter(token=token, status="active").first()
    if not session or not session.is_valid():
        return Response({"code": "SESSION_EXPIRED", "message": "Upload session expired or invalid."}, status=409)

    item = ProductUploadItem.objects.filter(session=session, _id=item_id, is_committed=False).first()
    if not item:
        return Response({"code": "INVALID_IMAGE", "message": "Staged item not found."}, status=404)

    raw_data = request.body
    if not raw_data:
        return Response({"code": "INVALID_IMAGE", "message": "No data received."}, status=400)

    saved_name = default_storage.save(item.storage_key, ContentFile(raw_data))
    item.url = default_storage.url(saved_name)
    if item.url.startswith("/"):
        item.url = request.build_absolute_uri(item.url)
    item.file_size = len(raw_data)
    item.save(update_fields=["url", "file_size"])

    return Response({"success": True, "url": item.url})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def finalize_upload_item(request, token):
    """
    POST /api/products/upload-session/<token>/finalize-upload/
    Authoritatively verifies the staged image and promotes it to permanent storage.
    """
    req_id = uuid.uuid4().hex
    session = ProductUploadSession.objects.filter(token=token, status="active").first()
    if not session or not session.is_valid():
        return Response({
            "code": "SESSION_EXPIRED",
            "message": "Upload session expired or invalid.",
            "request_id": req_id,
        }, status=409)

    staged_item_id = (
        request.data.get("stagedItemId")
        or request.data.get("itemId")
        or request.data.get("item_id")
    )
    if not staged_item_id:
        return Response({
            "code": "INVALID_IMAGE",
            "message": "stagedItemId or itemId is required.",
            "request_id": req_id,
        }, status=400)

    item = ProductUploadItem.objects.filter(session=session, _id=staged_item_id).first()
    if not item:
        return Response({
            "code": "INVALID_IMAGE",
            "message": "Staged upload item not found in session.",
            "request_id": req_id,
        }, status=400)

    blob_url = (
        request.data.get("blobUrl")
        or request.data.get("blob_url")
        or request.data.get("url")
        or ""
    ).strip()
    blob_pathname = (
        request.data.get("blobPathname")
        or request.data.get("blob_pathname")
        or request.data.get("pathname")
        or ""
    ).strip()

    if blob_url:
        item.url = blob_url
    if blob_pathname and blob_pathname.startswith("products/"):
        item.storage_key = blob_pathname
    if blob_url or blob_pathname:
        item.save(update_fields=["url", "storage_key"])

    # Idempotent replay
    if not item.storage_key.startswith("products/staging/"):
        return Response({
            "success": True,
            "item": {
                "id": item._id,
                "url": item.url,
                "storageKey": item.storage_key,
                "filename": item.filename,
                "fileSize": item.file_size,
                "contentType": item.content_type,
            }
        }, status=200)

    try:
        final_info = finalize_staged_upload_to_permanent(item, request=request)
        item.url = final_info["url"]
        item.storage_key = final_info["storage_key"]
        item.content_type = final_info["content_type"]
        item.file_size = final_info["file_size"]
        item.save()

        return Response({
            "success": True,
            "item": {
                "id": item._id,
                "url": item.url,
                "storageKey": item.storage_key,
                "filename": item.filename,
                "fileSize": item.file_size,
                "width": final_info["width"],
                "height": final_info["height"],
                "contentType": item.content_type,
            }
        }, status=200)

    except StorageValidationError as err:
        logger.warning("[%s] Finalize validation error for %s: %s", req_id, item.filename, err.message)
        delete_from_storage_safe(item.storage_key, item.url)
        item.delete()
        return Response({
            "code": err.code,
            "message": err.message,
            "file_name": item.filename,
            "request_id": req_id,
        }, status=400)
    except StorageConfigError as err:
        logger.error("[%s] Finalize storage config error: %s", req_id, err.message)
        return Response({
            "code": err.code,
            "message": err.message,
            "file_name": item.filename,
            "request_id": req_id,
        }, status=503)
    except StorageUpstreamError as err:
        logger.error("[%s] Finalize storage upstream error: %s", req_id, err.message)
        return Response({
            "code": err.code,
            "message": err.message,
            "file_name": item.filename,
            "request_id": req_id,
        }, status=502)
    except StorageTimeoutError as err:
        logger.error("[%s] Finalize storage timeout error: %s", req_id, err.message)
        return Response({
            "code": err.code,
            "message": err.message,
            "file_name": item.filename,
            "request_id": req_id,
        }, status=504)
    except Exception as exc:
        logger.exception("[%s] Unexpected finalize error: %s", req_id, exc)
        return Response({
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred while processing image.",
            "file_name": item.filename,
            "request_id": req_id,
        }, status=500)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
@parser_classes([MultiPartParser, FormParser])
def stage_upload_item(request, token):
    """
    POST /api/products/upload-session/<token>/stage/
    Direct multipart stage fallback with authoritative Pillow validation and canonical permanent storage.
    """
    req_id = uuid.uuid4().hex
    session = ProductUploadSession.objects.filter(token=token, status="active").first()
    if not session or not session.is_valid():
        return Response({
            "code": "SESSION_EXPIRED",
            "message": "Invalid or expired upload session.",
            "request_id": req_id,
        }, status=409)

    file_obj = request.FILES.get("image") or request.FILES.get("file")
    if not file_obj:
        return Response({
            "code": "INVALID_IMAGE",
            "message": "No file provided.",
            "file_name": "",
            "request_id": req_id,
        }, status=400)

    try:
        val_info = validate_and_decode_image(file_obj)
        canonical_mime = val_info["content_type"]
        canonical_ext = val_info["extension"]
        permanent_key = generate_permanent_key(canonical_ext=canonical_ext)

        file_obj.seek(0)
        verified_bytes = file_obj.read()
        put_result = put_to_vercel_blob(permanent_key, verified_bytes, content_type=canonical_mime)
        url = put_result["url"]
        if url.startswith("/") and request:
            url = request.build_absolute_uri(url)

        item = ProductUploadItem.objects.create(
            session=session,
            url=url,
            storage_key=permanent_key,
            filename=file_obj.name[:255],
            file_size=len(verified_bytes),
            content_type=canonical_mime,
            is_committed=False,
        )
        return Response({
            "success": True,
            "item": {
                "id": item._id,
                "url": item.url,
                "storageKey": item.storage_key,
                "filename": item.filename,
                "fileSize": item.file_size,
                "width": val_info["width"],
                "height": val_info["height"],
            }
        }, status=201)
    except StorageValidationError as val_err:
        return Response({
            "code": val_err.code,
            "message": val_err.message,
            "file_name": getattr(file_obj, "name", ""),
            "request_id": req_id,
        }, status=400)
    except StorageConfigError as cfg_err:
        return Response({
            "code": cfg_err.code,
            "message": cfg_err.message,
            "file_name": getattr(file_obj, "name", ""),
            "request_id": req_id,
        }, status=503)
    except StorageUpstreamError as up_err:
        return Response({
            "code": up_err.code,
            "message": up_err.message,
            "file_name": getattr(file_obj, "name", ""),
            "request_id": req_id,
        }, status=502)
    except StorageTimeoutError as to_err:
        return Response({
            "code": to_err.code,
            "message": to_err.message,
            "file_name": getattr(file_obj, "name", ""),
            "request_id": req_id,
        }, status=504)
    except Exception as exc:
        logger.exception("[%s] Stage upload failed: %s", req_id, exc)
        return Response({
            "code": "INTERNAL_ERROR",
            "message": "Failed to upload image.",
            "file_name": getattr(file_obj, "name", ""),
            "request_id": req_id,
        }, status=500)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def remove_staged_item(request, token, item_id):
    """
    DELETE /api/products/upload-session/<token>/items/<item_id>/
    Removes a staged item from the session and deletes its uncommitted storage object.
    """
    session = ProductUploadSession.objects.filter(token=token, status="active").first()
    if not session or not session.is_valid():
        return Response({
            "code": "SESSION_EXPIRED",
            "message": "Invalid or expired upload session.",
            "request_id": uuid.uuid4().hex,
        }, status=409)

    item = ProductUploadItem.objects.filter(session=session, _id=item_id, is_committed=False).first()
    if not item:
        return Response({"code": "NOT_FOUND", "message": "Item not found."}, status=404)

    delete_from_storage_safe(item.storage_key, item.url)
    item.delete()
    return Response({"success": True, "message": "Staged item removed."})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def add_product_image(request, product_id):
    """
    POST /api/products/<product_id>/images/
    Directly attaches an uploaded image metadata to an existing product.
    Enforces MAX_PRODUCT_IMAGES limit under concurrency lock.
    """
    req_id = uuid.uuid4().hex
    with transaction.atomic():
        locked_product = Product.objects.select_for_update().filter(_id=product_id).first()
        if not locked_product:
            return Response({"success": False, "message": "Product not found."}, status=404)

        if locked_product.product_images.count() >= MAX_PRODUCT_IMAGES:
            return Response({
                "code": "MAX_IMAGES_EXCEEDED",
                "message": f"Maximum {MAX_PRODUCT_IMAGES} images allowed per product.",
                "request_id": req_id,
            }, status=400)

        url = request.data.get("url")
        if not url:
            return Response({"success": False, "message": "Image URL is required."}, status=400)

        storage_key = request.data.get("storageKey") or request.data.get("storage_key") or ""
        alt_text = request.data.get("altText") or request.data.get("alt") or locked_product.name

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
    req_id = uuid.uuid4().hex
    product = Product.objects.filter(_id=product_id).first()
    if not product:
        return Response({"success": False, "message": "Product not found."}, status=404)

    image_ids = request.data.get("imageIds") or request.data.get("image_ids") or []
    if not isinstance(image_ids, list):
        return Response({"success": False, "message": "imageIds array is required."}, status=400)

    if len(image_ids) > MAX_PRODUCT_IMAGES:
        return Response({
            "code": "MAX_IMAGES_EXCEEDED",
            "message": f"Maximum {MAX_PRODUCT_IMAGES} images allowed per product.",
            "request_id": req_id,
        }, status=400)

    if len(image_ids) != len(set(image_ids)):
        return Response({"success": False, "message": "Duplicate image IDs provided in reorder."}, status=400)

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
