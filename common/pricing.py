from decimal import Decimal, ROUND_HALF_UP

from common.utils import sanitize_image_url
from products.models import Product

SHIPPING_THRESHOLD = Decimal("999.00")
SHIPPING_CHARGE = Decimal("49.00")
TAX_RATE = Decimal("0.18")


def calculate_order_pricing(order_items):
    """
    Shared server-authoritative calculation for order pricing.
    - Product prices and images snapshot from trusted database values.
    - Tax-inclusive pricing (GST is broken down informationally, not added on top).
    - Free shipping for subtotal >= ₹999, else ₹49.
    - Quantities validated as positive integers.
    - Decimal-based arithmetic converted to paise.
    """
    sanitized_items = []
    items_subtotal_dec = Decimal("0.00")

    for item in order_items:
        product_id = item.get("product") or item.get("_id")
        if not product_id:
            continue

        product = Product.objects.filter(_id=product_id).first()
        if not product:
            continue

        raw_qty = item.get("quantity", 1)
        try:
            qty = max(1, int(raw_qty))
        except (ValueError, TypeError):
            qty = 1

        # Use trusted database price
        price_dec = Decimal(str(product.offer_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        item_total = price_dec * qty
        items_subtotal_dec += item_total

        # Snapshot image
        raw_image_url = ""
        if product.images and len(product.images) > 0:
            first_img = product.images[0]
            if isinstance(first_img, dict):
                raw_image_url = first_img.get("url", "")
            elif isinstance(first_img, str):
                raw_image_url = first_img

        image_url = sanitize_image_url(raw_image_url, product.brand or product.name)

        sanitized_items.append({
            "product": product._id,
            "name": product.name,
            "image": image_url,
            "price": float(price_dec),
            "quantity": qty,
            "stock": product.stock,
        })

    # Shipping calculation: free if >= 999 or empty, else 49
    if items_subtotal_dec == Decimal("0.00") or items_subtotal_dec >= SHIPPING_THRESHOLD:
        shipping_dec = Decimal("0.00")
    else:
        shipping_dec = SHIPPING_CHARGE

    # Tax calculation: GST 18% included
    tax_dec = (items_subtotal_dec * TAX_RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    total_dec = (items_subtotal_dec + shipping_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    amount_paise = int(total_dec * 100)

    return {
        "items": sanitized_items,
        "items_price": float(items_subtotal_dec),
        "tax_price": float(tax_dec),
        "shipping_price": float(shipping_dec),
        "total_price": float(total_dec),
        "amount_paise": amount_paise,
        "items_price_dec": items_subtotal_dec,
        "tax_price_dec": tax_dec,
        "shipping_price_dec": shipping_dec,
        "total_price_dec": total_dec,
    }
