"""
Secure storage handler for AK Mobiles.
Supports Vercel Blob (production) with safe local fallback (dev/tests).
Includes deep Pillow validation, magic byte inspection, filename sanitization,
collision-safe keys, and order-snapshot retention safety.
"""

import io
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from decimal import Decimal
from django.conf import settings
from django.core.files.storage import default_storage
from django.utils.text import slugify
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = {
    "image/jpeg": [".jpg", ".jpeg"],
    "image/png": [".png"],
    "image/webp": [".webp"],
}

ALLOWED_PIL_FORMATS = {"JPEG", "PNG", "WEBP"}

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
MAX_DIMENSION = 5000  # 5000x5000 px max
MIN_DIMENSION = 10    # 10x10 px min
Image.MAX_IMAGE_PIXELS = 25_000_000  # Defend against decompression bombs


def validate_image_file(file_obj):
    """
    Validates file size, MIME type, magic bytes, and performs deep Pillow verification.
    Catches truncated, corrupted, and malformed files as well as decompression bombs.
    Returns: dict with { 'content_type', 'width', 'height', 'format' }
    """
    if not file_obj:
        raise ValueError("No file provided.")

    file_size = getattr(file_obj, "size", None)
    if file_size is None and hasattr(file_obj, "getbuffer"):
        file_size = file_obj.getbuffer().nbytes
    elif file_size is None and hasattr(file_obj, "seek") and hasattr(file_obj, "tell"):
        curr = file_obj.tell()
        file_obj.seek(0, os.SEEK_END)
        file_size = file_obj.tell()
        file_obj.seek(curr)

    if file_size is not None and file_size > MAX_FILE_SIZE:
        raise ValueError("File size exceeds 5MB limit.")

    if file_size is not None and file_size == 0:
        raise ValueError("File is empty.")

    content_type = getattr(file_obj, "content_type", "")
    if content_type and content_type not in ALLOWED_MIME_TYPES:
        raise ValueError("Invalid file format. Allowed formats: JPEG, PNG, WebP.")

    # Read first 32 bytes for magic byte validation
    initial_pos = file_obj.tell() if hasattr(file_obj, "tell") else 0
    header = file_obj.read(32)
    if hasattr(file_obj, "seek"):
        file_obj.seek(initial_pos)

    # Magic byte verification
    is_valid_magic = False
    detected_type = None

    if header.startswith(b"\xff\xd8\xff"):
        is_valid_magic = True
        detected_type = "image/jpeg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        is_valid_magic = True
        detected_type = "image/png"
    elif len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        is_valid_magic = True
        detected_type = "image/webp"

    if not is_valid_magic or (content_type and detected_type and detected_type != content_type):
        raise ValueError("File content does not match allowed image formats (JPEG, PNG, WebP).")

    final_content_type = content_type or detected_type

    # Deep Pillow Decoding & Integrity Verification
    try:
        # 1. Verification pass
        file_obj.seek(initial_pos)
        pil_img = Image.open(file_obj)
        
        # Verify file structure
        pil_img.verify()
        detected_format = pil_img.format
        if detected_format not in ALLOWED_PIL_FORMATS:
            raise ValueError(f"Unsupported image format: {detected_format}. Allowed: JPEG, PNG, WebP.")

        # 2. Decoding pass to check full raster data and dimensions
        file_obj.seek(initial_pos)
        pil_img = Image.open(file_obj)
        pil_img.load()  # Forces full image decoding to catch truncated data
        
        width, height = pil_img.size
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            raise ValueError(f"Image dimensions ({width}x{height}) exceed maximum allowed {MAX_DIMENSION}x{MAX_DIMENSION}px.")
        if width < MIN_DIMENSION or height < MIN_DIMENSION:
            raise ValueError(f"Image dimensions ({width}x{height}) are too small (min {MIN_DIMENSION}x{MIN_DIMENSION}px).")

        file_obj.seek(initial_pos)
        return {
            "content_type": final_content_type,
            "width": width,
            "height": height,
            "format": detected_format,
            "file_size": file_size,
        }
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        logger.warning("Decompression bomb detected: %s", e)
        raise ValueError("Image file is too complex or poses a decompression risk.")
    except (ValueError, SyntaxError) as e:
        logger.warning("Image verification error: %s", e)
        raise ValueError(str(e) if "dimensions" in str(e) or "format" in str(e) else "Truncated, corrupted, or malformed image file.")
    except Exception as e:
        logger.warning("Image open failed: %s", e)
        raise ValueError("Truncated, corrupted, or malformed image file.")


def generate_storage_key(filename, prefix="products"):
    """
    Generates collision-safe, path-traversal-proof storage key.
    """
    base_name, ext = os.path.splitext(filename)
    safe_ext = ext.lower() if ext.lower() in [".jpg", ".jpeg", ".png", ".webp"] else ".jpg"
    safe_base = slugify(base_name)[:30] or "img"
    unique_id = uuid.uuid4().hex[:12]
    return f"{prefix}/{unique_id}_{safe_base}{safe_ext}"


def upload_to_blob_or_storage(file_obj, filename=None, request=None):
    """
    Uploads file to Vercel Blob (if BLOB_READ_WRITE_TOKEN is configured)
    or falls back to Django default_storage in local development.
    """
    validation_info = validate_image_file(file_obj)
    content_type = validation_info["content_type"]
    original_name = filename or getattr(file_obj, "name", "upload.jpg")
    storage_key = generate_storage_key(original_name)

    blob_token = getattr(settings, "BLOB_READ_WRITE_TOKEN", None) or os.getenv("BLOB_READ_WRITE_TOKEN")

    if blob_token:
        # Vercel Blob REST upload (Direct HTTP PUT to Blob Store)
        blob_endpoint = f"https://blob.vercel-storage.com/{storage_key}"
        file_obj.seek(0)
        file_bytes = file_obj.read()

        req = urllib.request.Request(
            blob_endpoint,
            data=file_bytes,
            headers={
                "Authorization": f"Bearer {blob_token}",
                "x-api-version": "7",
                "x-content-type": content_type,
                "x-add-random-suffix": "0",
            },
            method="PUT",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                return {
                    "url": res_data.get("url", blob_endpoint),
                    "storage_key": storage_key,
                    "content_type": content_type,
                    "file_size": len(file_bytes),
                    "width": validation_info.get("width"),
                    "height": validation_info.get("height"),
                }
        except Exception as err:
            logger.exception("Vercel Blob upload failed: %s", err)
            if not settings.DEBUG:
                raise RuntimeError("Failed to store image in persistent storage.")

    # In production without token, reject silent local storage fallback
    import sys
    is_testing = getattr(settings, "TESTING", False) or any("test" in arg for arg in sys.argv)
    if not settings.DEBUG and not is_testing and not blob_token:
        raise RuntimeError("Vercel Blob storage is not configured (missing BLOB_READ_WRITE_TOKEN).")

    # Local development / test fallback
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    saved_name = default_storage.save(storage_key, file_obj)
    url = default_storage.url(saved_name)
    if url.startswith("/") and request:
        url = request.build_absolute_uri(url)

    file_size = getattr(file_obj, "size", 0)
    return {
        "url": url,
        "storage_key": saved_name,
        "content_type": content_type,
        "file_size": file_size,
        "width": validation_info.get("width"),
        "height": validation_info.get("height"),
    }


def delete_from_storage_safe(storage_key, url=""):
    """
    Safely deletes storage object only if NOT referenced by any historical orders.
    Prevents deletion outside 'products/' prefix.
    """
    if not storage_key or not storage_key.startswith("products/"):
        logger.warning("Refused storage delete for non-product key: %s", storage_key)
        return False

    # Check order snapshots
    from orders.models import Order
    if url:
        orders_with_url = Order.objects.filter(order_items__icontains=url).exists()
        if orders_with_url:
            logger.info("Retaining image %s as it is referenced in historical order snapshot", storage_key)
            return False

    blob_token = getattr(settings, "BLOB_READ_WRITE_TOKEN", None) or os.getenv("BLOB_READ_WRITE_TOKEN")
    if blob_token:
        try:
            req = urllib.request.Request(
                "https://blob.vercel-storage.com/delete",
                data=json.dumps({"urls": [url or f"https://blob.vercel-storage.com/{storage_key}"]}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {blob_token}",
                    "x-api-version": "7",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return True
        except Exception as err:
            logger.warning("Vercel Blob deletion failed for %s: %s", storage_key, err)
            return False

    if default_storage.exists(storage_key):
        default_storage.delete(storage_key)
        return True

    return False
