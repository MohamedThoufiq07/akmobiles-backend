from decimal import Decimal, ROUND_HALF_UP

from common.utils import sanitize_image_url
from products.models import Product

TAX_RATE = Decimal("0.18")


def calculate_order_pricing(order_items):
    """
    Shared server-authoritative calculation for order pricing.
    - Product prices and images snapshot from trusted database values.
    - Per-product delivery charge applies ONCE per distinct product/order line (NOT multiplied by quantity).
    - Merges identical product IDs into a single distinct line item.
    - Tax-inclusive pricing (GST is broken down informationally, not added on top).
    - Quantities validated as positive integers.
    - Strict Decimal-based arithmetic converted to paise.
    """
    # 1. Deduplicate/merge items by product ID
    merged_items_map = {}
    for item in order_items:
        product_id = item.get("product") or item.get("_id")
        if not product_id:
            continue

        raw_qty = item.get("quantity", 1)
        try:
            qty = max(1, int(raw_qty))
        except (ValueError, TypeError):
            qty = 1

        if product_id in merged_items_map:
            merged_items_map[product_id] += qty
        else:
            merged_items_map[product_id] = qty

    sanitized_items = []
    items_subtotal_dec = Decimal("0.00")
    shipping_total_dec = Decimal("0.00")

    for product_id, qty in merged_items_map.items():
        product = Product.objects.filter(_id=product_id).prefetch_related("product_images").first()
        if not product:
            continue

        # Use trusted database price
        price_dec = Decimal(str(product.offer_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        item_subtotal = price_dec * qty
        items_subtotal_dec += item_subtotal

        # Delivery charge applied ONCE per distinct product line
        raw_delivery = getattr(product, "delivery_charge", Decimal("49.00"))
        try:
            delivery_dec = Decimal(str(raw_delivery)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if delivery_dec < Decimal("0.00"):
                delivery_dec = Decimal("0.00")
        except Exception:
            delivery_dec = Decimal("49.00")

        line_delivery_total_dec = delivery_dec  # Exactly once per line
        shipping_total_dec += line_delivery_total_dec

        # Primary image resolution for snapshot
        primary_img = product.primary_image
        if primary_img and primary_img.url:
            raw_image_url = primary_img.url
        elif product.images and len(product.images) > 0:
            first_img = product.images[0]
            if isinstance(first_img, dict):
                raw_image_url = first_img.get("url", "")
            elif isinstance(first_img, str):
                raw_image_url = first_img
            else:
                raw_image_url = ""
        else:
            raw_image_url = ""

        image_url = sanitize_image_url(raw_image_url, product.brand or product.name)

        sanitized_items.append({
            "product": product._id,
            "name": product.name,
            "brand": product.brand,
            "image": image_url,
            "price": float(price_dec),
            "quantity": qty,
            "delivery_charge": float(delivery_dec),
            "line_delivery_total": float(line_delivery_total_dec),
            "stock": product.stock,
        })

    # Tax calculation: GST 18% included
    tax_dec = (items_subtotal_dec * TAX_RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    total_dec = (items_subtotal_dec + shipping_total_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    amount_paise = int(total_dec * 100)

    return {
        "items": sanitized_items,
        "items_price": float(items_subtotal_dec),
        "tax_price": float(tax_dec),
        "shipping_price": float(shipping_total_dec),
        "total_price": float(total_dec),
        "amount_paise": amount_paise,
        "items_price_dec": items_subtotal_dec,
        "tax_price_dec": tax_dec,
        "shipping_price_dec": shipping_total_dec,
        "total_price_dec": total_dec,
    }
