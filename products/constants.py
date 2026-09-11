"""
Product domain constants for AK Mobiles.
Enforces business rules for product image limits, file sizes, and formats.
"""

MIN_PRODUCT_IMAGES = 1
MAX_PRODUCT_IMAGES = 5
MAX_PRODUCT_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB

ACCEPTED_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP")
