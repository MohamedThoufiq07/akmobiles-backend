"""
Explicit transactional services for product archive and restore (soft-delete).
Preserves historical orders, payments, invoices, snapshots, and image assets.
Does not delete Vercel Blob images or remove database rows.
"""

from functools import reduce
from operator import or_
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import Product


def build_filtered_product_queryset(filters=None, is_active=True):
    """
    Applies search and attribute filters identically to the product list view.
    """
    qs = Product.objects.filter(is_active=is_active)
    if not filters or not isinstance(filters, dict):
        return qs

    brand = filters.get("brand")
    if brand:
        qs = qs.filter(reduce(or_, (Q(brand__iexact=b) for b in brand.split(","))))

    category = filters.get("category")
    if category:
        qs = qs.filter(reduce(or_, (Q(category__iexact=c) for c in category.split(","))))

    min_p = filters.get("minPrice")
    if min_p is not None and min_p != "":
        try:
            qs = qs.filter(offer_price__gte=float(min_p))
        except (ValueError, TypeError):
            pass

    max_p = filters.get("maxPrice")
    if max_p is not None and max_p != "":
        try:
            qs = qs.filter(offer_price__lte=float(max_p))
        except (ValueError, TypeError):
            pass

    ram = filters.get("ram")
    if ram:
        qs = qs.filter(reduce(or_, (Q(specifications__ram=v) for v in ram.split(","))))

    storage = filters.get("storage")
    if storage:
        qs = qs.filter(reduce(or_, (Q(specifications__storage=v) for v in storage.split(","))))

    rating = filters.get("rating")
    if rating:
        try:
            qs = qs.filter(rating__gte=float(rating))
        except (ValueError, TypeError):
            pass

    discount = filters.get("discount")
    if discount:
        try:
            qs = qs.filter(discount__gte=float(discount))
        except (ValueError, TypeError):
            pass

    if str(filters.get("flashSale", "")).lower() == "true":
        qs = qs.filter(flash_sale=True)

    search = (filters.get("search") or filters.get("keyword") or "").strip()
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(brand__icontains=search)
            | Q(category__icontains=search)
            | Q(description__icontains=search)
        )

    return qs


def archive_single_product(product, user=None):
    """
    Soft-archives an individual product atomically.
    Removes it from storefront, featured, and flash-sale associations.
    """
    with transaction.atomic():
        locked = Product.objects.select_for_update().filter(_id=product._id).first()
        if not locked:
            return False
        locked.is_active = False
        locked.archived_at = timezone.now()
        locked.archived_by = user if user and user.is_authenticated else None
        locked.is_featured = False
        locked.flash_sale = False
        locked.save(update_fields=["is_active", "archived_at", "archived_by", "is_featured", "flash_sale", "updated_at"])
        return True


def bulk_archive_products(product_ids, user=None):
    """
    Soft-archives an explicit list of product IDs atomically.
    """
    if not product_ids or not isinstance(product_ids, list):
        return 0

    clean_ids = [str(pid).strip() for pid in product_ids if str(pid).strip()]
    if not clean_ids:
        return 0

    now = timezone.now()
    archived_by = user if user and user.is_authenticated else None

    with transaction.atomic():
        locked_qs = Product.objects.select_for_update().filter(_id__in=clean_ids, is_active=True)
        count = locked_qs.count()
        if count > 0:
            locked_qs.update(
                is_active=False,
                archived_at=now,
                archived_by=archived_by,
                is_featured=False,
                flash_sale=False,
                updated_at=now,
            )
        return count


def bulk_archive_all_filtered(filters=None, excluded_ids=None, user=None):
    """
    Soft-archives all products matching the given filter query, excluding excluded_ids.
    """
    now = timezone.now()
    archived_by = user if user and user.is_authenticated else None

    with transaction.atomic():
        qs = build_filtered_product_queryset(filters=filters, is_active=True)
        if excluded_ids and isinstance(excluded_ids, list):
            clean_excluded = [str(pid).strip() for pid in excluded_ids if str(pid).strip()]
            if clean_excluded:
                qs = qs.exclude(_id__in=clean_excluded)

        locked_qs = qs.select_for_update()
        count = locked_qs.count()
        if count > 0:
            locked_qs.update(
                is_active=False,
                archived_at=now,
                archived_by=archived_by,
                is_featured=False,
                flash_sale=False,
                updated_at=now,
            )
        return count


def restore_single_product(product):
    """
    Restores an archived product to active status atomically.
    """
    with transaction.atomic():
        locked = Product.objects.select_for_update().filter(_id=product._id).first()
        if not locked:
            return False
        locked.is_active = True
        locked.archived_at = None
        locked.archived_by = None
        locked.save(update_fields=["is_active", "archived_at", "archived_by", "updated_at"])
        return True


def bulk_restore_products(product_ids):
    """
    Restores an explicit list of product IDs from trash atomically.
    """
    if not product_ids or not isinstance(product_ids, list):
        return 0

    clean_ids = [str(pid).strip() for pid in product_ids if str(pid).strip()]
    if not clean_ids:
        return 0

    now = timezone.now()

    with transaction.atomic():
        locked_qs = Product.objects.select_for_update().filter(_id__in=clean_ids, is_active=False)
        count = locked_qs.count()
        if count > 0:
            locked_qs.update(
                is_active=True,
                archived_at=None,
                archived_by=None,
                updated_at=now,
            )
        return count


def bulk_restore_all_filtered(filters=None, excluded_ids=None):
    """
    Restores all products matching the given filter query from trash atomically.
    """
    now = timezone.now()

    with transaction.atomic():
        qs = build_filtered_product_queryset(filters=filters, is_active=False)
        if excluded_ids and isinstance(excluded_ids, list):
            clean_excluded = [str(pid).strip() for pid in excluded_ids if str(pid).strip()]
            if clean_excluded:
                qs = qs.exclude(_id__in=clean_excluded)

        locked_qs = qs.select_for_update()
        count = locked_qs.count()
        if count > 0:
            locked_qs.update(
                is_active=True,
                archived_at=None,
                archived_by=None,
                updated_at=now,
            )
        return count
