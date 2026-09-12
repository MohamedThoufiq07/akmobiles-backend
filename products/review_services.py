"""
Authoritative backend services for Verified-Purchase Review Eligibility,
creation, update, deletion, and rating aggregation.
"""

from django.db import transaction, IntegrityError
from django.core.exceptions import ValidationError
from orders.models import Order
from payments.models import Payment
from .models import Product, Review


def check_review_eligibility(user, product):
    """
    Authoritative evaluation of whether a user can review a product.
    Returns:
        dict with {can_review, reason, is_verified_purchase, qualifying_order, existing_review}
    """
    if not user or not user.is_authenticated:
        return {
            "can_review": False,
            "reason": "AUTHENTICATION_REQUIRED",
            "is_verified_purchase": False,
            "qualifying_order": None,
            "existing_review": None,
        }

    if not product:
        return {
            "can_review": False,
            "reason": "PRODUCT_NOT_FOUND",
            "is_verified_purchase": False,
            "qualifying_order": None,
            "existing_review": None,
        }

    if not getattr(product, "is_active", True):
        return {
            "can_review": False,
            "reason": "PRODUCT_INACTIVE",
            "is_verified_purchase": False,
            "qualifying_order": None,
            "existing_review": None,
        }

    # Check for existing review
    existing_review = Review.objects.filter(product=product, user=user).first()
    if existing_review:
        return {
            "can_review": False,
            "reason": "REVIEW_ALREADY_EXISTS",
            "is_verified_purchase": existing_review.is_verified_purchase,
            "qualifying_order": existing_review.qualifying_order,
            "existing_review": existing_review,
        }

    # Match user orders for this product
    target_pid = str(product._id)
    user_orders = Order.objects.filter(user=user).order_by("-created_at")

    matching_orders = []
    for order in user_orders:
        items = order.order_items or []
        for item in items:
            item_pid = str(
                item.get("product")
                or item.get("product_id")
                or item.get("_id")
                or item.get("id")
                or ""
            )
            if item_pid and item_pid == target_pid:
                matching_orders.append(order)
                break

    if not matching_orders:
        return {
            "can_review": False,
            "reason": "PURCHASE_REQUIRED",
            "is_verified_purchase": False,
            "qualifying_order": None,
            "existing_review": None,
        }

    has_undelivered = False
    has_unverified_payment = False
    qualifying_order = None

    for order in matching_orders:
        if order.order_status != "Delivered":
            has_undelivered = True
            continue

        payment_method = (order.payment_info or {}).get("method", "Razorpay")
        is_razorpay = payment_method == "Razorpay"

        if is_razorpay:
            payment = getattr(order, "payment", None) or Payment.objects.filter(order=order).first()
            payment_status = payment.status if payment else (order.payment_info or {}).get("status")
            if payment_status == "Completed":
                qualifying_order = order
                break
            else:
                has_unverified_payment = True
        else:
            # Valid delivered COD or alternate non-Razorpay order
            qualifying_order = order
            break

    if qualifying_order:
        return {
            "can_review": True,
            "reason": "ELIGIBLE",
            "is_verified_purchase": True,
            "qualifying_order": qualifying_order,
            "existing_review": None,
        }

    if has_unverified_payment and not has_undelivered:
        return {
            "can_review": False,
            "reason": "PAYMENT_NOT_VERIFIED",
            "is_verified_purchase": False,
            "qualifying_order": None,
            "existing_review": None,
        }

    return {
        "can_review": False,
        "reason": "ORDER_NOT_DELIVERED",
        "is_verified_purchase": False,
        "qualifying_order": None,
        "existing_review": None,
    }


def create_product_review(user, product, rating, comment, title=""):
    """
    Creates a verified product review with atomic concurrency protection.
    """
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        raise ValidationError({"rating": "Rating must be an integer between 1 and 5."})

    trimmed_comment = (comment or "").strip()
    if not trimmed_comment:
        raise ValidationError({"comment": "Review comment cannot be blank."})
    if len(trimmed_comment) > 2000:
        raise ValidationError({"comment": "Review comment cannot exceed 2000 characters."})

    trimmed_title = (title or "").strip()[:200]

    with transaction.atomic():
        # Lock product for rating recalculation
        locked_product = Product.objects.select_for_update().filter(_id=product._id).first()
        if not locked_product:
            raise ValidationError({"product": "Product not found."})

        eligibility = check_review_eligibility(user, locked_product)
        if not eligibility["can_review"]:
            reason = eligibility["reason"]
            if reason == "REVIEW_ALREADY_EXISTS":
                raise IntegrityError("REVIEW_ALREADY_EXISTS")
            raise ValidationError({"eligibility": reason})

        display_name = user.name.strip() if getattr(user, "name", None) else user.email.split("@")[0]

        review = Review.objects.create(
            product=locked_product,
            user=user,
            qualifying_order=eligibility["qualifying_order"],
            name=display_name,
            rating=rating,
            title=trimmed_title,
            comment=trimmed_comment,
            is_verified_purchase=True,
            is_published=True,
        )

        locked_product.recalculate_rating()

    return review


def update_product_review(user, review_id, rating, comment, title=""):
    """
    Updates an existing customer review safely.
    """
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        raise ValidationError({"rating": "Rating must be an integer between 1 and 5."})

    trimmed_comment = (comment or "").strip()
    if not trimmed_comment:
        raise ValidationError({"comment": "Review comment cannot be blank."})
    if len(trimmed_comment) > 2000:
        raise ValidationError({"comment": "Review comment cannot exceed 2000 characters."})

    trimmed_title = (title or "").strip()[:200]

    with transaction.atomic():
        review = Review.objects.select_for_update().filter(_id=review_id).first()
        if not review:
            raise ValidationError({"review": "Review not found."})

        if review.user_id != user._id and getattr(user, "role", None) != "admin":
            raise ValidationError({"permission": "REVIEW_PERMISSION_DENIED"})

        review.rating = rating
        review.comment = trimmed_comment
        review.title = trimmed_title
        review.save(update_fields=["rating", "comment", "title", "updated_at"])

        review.product.recalculate_rating()

    return review


def delete_product_review(user, review_id):
    """
    Deletes a review and recalculates rating.
    """
    with transaction.atomic():
        review = Review.objects.select_for_update().filter(_id=review_id).first()
        if not review:
            raise ValidationError({"review": "Review not found."})

        if review.user_id != user._id and getattr(user, "role", None) != "admin":
            raise ValidationError({"permission": "REVIEW_PERMISSION_DENIED"})

        product = review.product
        review.delete()
        product.recalculate_rating()

    return True


def get_product_reviews_summary(product):
    """
    Computes distribution, count, and average for published reviews.
    """
    reviews = list(Review.objects.filter(product=product, is_published=True).order_by("-created_at"))
    count = len(reviews)
    average = round(sum(r.rating for r in reviews) / count, 2) if count > 0 else 0.0

    distribution = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    for r in reviews:
        if r.rating in distribution:
            distribution[r.rating] += 1

    return {
        "averageRating": average,
        "reviewCount": count,
        "distribution": distribution,
        "reviews": reviews,
    }
