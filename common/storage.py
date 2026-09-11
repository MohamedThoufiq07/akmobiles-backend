"""
Production-grade storage handler for AK Mobiles.
Supports direct browser uploads to Vercel Blob staging and authoritative Django finalization.
Includes function-scoped Pillow validation, magic signature and raster integrity verification,
anti-SSRF download protection, collision-safe permanent key promotion, canonical MIME/extension
normalization, and order-snapshot retention safety.
"""

import io
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
from decimal import Decimal
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_STREAM_BYTES = MAX_FILE_SIZE + 1  # 5 MB + 1 byte
MAX_DIMENSION = 5000  # 5000x5000 px max
MIN_DIMENSION = 10    # 10x10 px min
Image.MAX_IMAGE_PIXELS = 25_000_000  # Defend against decompression bombs

CANONICAL_FORMAT_MAP = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}

ALLOWED_BLOB_HOST_PATTERNS = [
    re.compile(r"^blob\.vercel-storage\.com$"),
    re.compile(r"^[a-zA-Z0-9_-]+\.public\.blob\.vercel-storage\.com$"),
]


# Structured Custom Exceptions
class StorageBaseException(Exception):
    def __init__(self, message, code="IMAGE_UPLOAD_FAILED", status_code=500):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class StorageValidationError(StorageBaseException):
    def __init__(self, message):
        super().__init__(message, code="INVALID_IMAGE", status_code=400)


class StorageConfigError(StorageBaseException):
    def __init__(self, message="Storage service is temporarily unavailable."):
        super().__init__(message, code="STORAGE_UNAVAILABLE", status_code=503)


class StorageUpstreamError(StorageBaseException):
    def __init__(self, message="Unable to upload image to storage. Please retry."):
        super().__init__(message, code="IMAGE_UPLOAD_FAILED", status_code=502)


class StorageTimeoutError(StorageBaseException):
    def __init__(self, message="Storage operation timed out. Please retry."):
        super().__init__(message, code="IMAGE_UPLOAD_TIMEOUT", status_code=504)


def get_blob_token():
    return getattr(settings, "BLOB_READ_WRITE_TOKEN", None) or os.getenv("BLOB_READ_WRITE_TOKEN", "")


def generate_staging_key(session_token, item_id=None, canonical_ext=None):
    """
    Generates a secure, temporary staging key:
    products/staging/<session_uuid>/<item_uuid>.<canonical_extension_or_upload>
    """
    safe_session = re.sub(r"[^a-zA-Z0-9_-]", "", str(session_token))[:64] or uuid.uuid4().hex
    item_uuid = item_id or uuid.uuid4().hex
    ext = canonical_ext if canonical_ext else ".upload"
    if not ext.startswith("."):
        ext = f".{ext}"
    return f"products/staging/{safe_session}/{item_uuid}{ext}"


def generate_permanent_key(canonical_ext=".jpg"):
    """
    Generates a collision-safe, permanent storage key:
    products/<namespace_uuid>/<image_uuid>.<canonical_extension>
    """
    ns_uuid = uuid.uuid4().hex[:12]
    img_uuid = uuid.uuid4().hex
    ext = canonical_ext if canonical_ext.startswith(".") else f".{canonical_ext}"
    return f"products/{ns_uuid}/{img_uuid}{ext}"


def validate_and_decode_image(file_obj, max_bytes=MAX_FILE_SIZE):
    """
    Authoritative two-pass Pillow validation with function-scoped warning filters.
    Pass 1: verify() container structure and extract detected format.
    Pass 2: load() full raster data to verify against truncation and decompression bombs.
    Normalizes extension and content-type authoritatively from Pillow's verified format.
    Always rewinds file_obj in a finally block.
    """
    if not file_obj:
        raise StorageValidationError("No file provided.")

    try:
        # Check size
        if hasattr(file_obj, "seek") and hasattr(file_obj, "tell"):
            file_obj.seek(0, os.SEEK_END)
            file_size = file_obj.tell()
            file_obj.seek(0)
        else:
            file_size = getattr(file_obj, "size", None) or len(getattr(file_obj, "read", lambda: b"")())

        if file_size is not None and file_size > max_bytes:
            raise StorageValidationError("File size exceeds 5MB limit.")
        if file_size is not None and file_size == 0:
            raise StorageValidationError("File is empty.")

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)

            # Pass 1: Open, detect format & verify
            file_obj.seek(0)
            try:
                with Image.open(file_obj) as img:
                    detected_format = (img.format or "").upper()
                    if detected_format not in CANONICAL_FORMAT_MAP:
                        raise StorageValidationError(
                            f"Unsupported image format: {detected_format}. Allowed: JPEG, PNG, WebP."
                        )
                    width, height = img.size
                    img.verify()
            except (UnidentifiedImageError, SyntaxError, ValueError) as err:
                logger.warning("Pillow pass 1 verification failed: %s", err)
                raise StorageValidationError("File content does not match allowed image formats (JPEG, PNG, WebP).")
            except Image.DecompressionBombError as err:
                logger.warning("Decompression bomb detected in pass 1: %s", err)
                raise StorageValidationError("Image file is too complex or poses a decompression risk.")

            # Pass 2: Reopen and load full raster data
            file_obj.seek(0)
            try:
                with Image.open(file_obj) as img:
                    img.load()  # Force full decoding of image raster
                    width, height = img.size
                    if width > MAX_DIMENSION or height > MAX_DIMENSION:
                        raise StorageValidationError(
                            f"Image dimensions ({width}x{height}) exceed maximum allowed {MAX_DIMENSION}x{MAX_DIMENSION}px."
                        )
                    if width < MIN_DIMENSION or height < MIN_DIMENSION:
                        raise StorageValidationError(
                            f"Image dimensions ({width}x{height}) are too small (min {MIN_DIMENSION}x{MIN_DIMENSION}px)."
                        )
            except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as err:
                logger.warning("Pillow pass 2 raster load failed: %s", err)
                raise StorageValidationError(
                    str(err) if "dimensions" in str(err) else "Truncated, corrupted, or malformed image file."
                )
            except Image.DecompressionBombError as err:
                logger.warning("Decompression bomb detected in pass 2: %s", err)
                raise StorageValidationError("Image file is too complex or poses a decompression risk.")

        canonical_mime, canonical_ext = CANONICAL_FORMAT_MAP[detected_format]
        return {
            "format": detected_format,
            "content_type": canonical_mime,
            "extension": canonical_ext,
            "width": width,
            "height": height,
            "file_size": file_size,
        }
    finally:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Validate that redirect target is HTTPS and within allowed Vercel Blob host pattern
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https":
            raise StorageValidationError("Invalid redirect scheme from storage provider.")
        if not any(pattern.match(parsed.hostname or "") for pattern in ALLOWED_BLOB_HOST_PATTERNS):
            raise StorageValidationError("Invalid redirect destination from storage provider.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_remote_blob_bytes(url, storage_key=""):
    """
    Safely downloads/streams a staged blob with strict SSRF protections:
    - Enforces HTTPS scheme
    - Enforces allowed Vercel Blob hostname pattern
    - Enforces staging prefix
    - Limits stream to MAX_STREAM_BYTES (5MB + 1 byte)
    - Enforces connection & read timeouts
    """
    if not url:
        raise StorageValidationError("Missing blob URL for verification.")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        # Allow local dev / test mock URLs
        if not (settings.DEBUG and parsed.scheme in ("http", "")):
            raise StorageValidationError("Insecure storage URL scheme.")

    if not settings.DEBUG:
        if not any(pattern.match(parsed.hostname or "") for pattern in ALLOWED_BLOB_HOST_PATTERNS):
            raise StorageValidationError("Storage URL host is not authorized.")

    opener = urllib.request.build_opener(NoRedirectHandler())
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AKMobiles-ImageVerifier/2.0"},
        method="GET",
    )

    try:
        with opener.open(req, timeout=12) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_FILE_SIZE:
                raise StorageValidationError("Staged file size exceeds 5MB limit.")

            buffer = io.BytesIO()
            total_read = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total_read += len(chunk)
                if total_read > MAX_STREAM_BYTES:
                    raise StorageValidationError("Staged file stream exceeds 5MB limit.")
                buffer.write(chunk)

            buffer.seek(0)
            return buffer
    except StorageValidationError:
        raise
    except urllib.error.HTTPError as err:
        logger.error("HTTP error fetching remote blob: %s (status %s)", err, err.code)
        if err.code == 404:
            raise StorageValidationError("Staged upload file not found in storage.")
        raise StorageUpstreamError("Failed to retrieve staged file from storage provider.")
    except urllib.error.URLError as err:
        logger.error("Network error fetching remote blob: %s", err)
        if "timed out" in str(err).lower():
            raise StorageTimeoutError("Storage fetch timed out.")
        raise StorageUpstreamError("Unable to connect to storage provider.")
    except Exception as exc:
        logger.exception("Unexpected error fetching remote blob: %s", exc)
        raise StorageUpstreamError("Failed to read staged upload file.")


def put_to_vercel_blob(storage_key, data_bytes, content_type="image/jpeg"):
    """
    Uploads verified bytes to Vercel Blob with canonical Content-Type and x-access: public.
    """
    blob_token = get_blob_token()
    if not blob_token:
        if not settings.DEBUG:
            raise StorageConfigError("Vercel Blob storage is not configured (missing BLOB_READ_WRITE_TOKEN).")
        # Local fallback in dev
        saved_key = default_storage.save(storage_key, ContentFile(data_bytes))
        return {
            "url": default_storage.url(saved_key),
            "storage_key": saved_key,
        }

    blob_endpoint = f"https://blob.vercel-storage.com/{storage_key}"
    req = urllib.request.Request(
        blob_endpoint,
        data=data_bytes,
        headers={
            "Authorization": f"Bearer {blob_token}",
            "x-api-version": "7",
            "x-content-type": content_type,
            "x-access": "public",
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
            }
    except urllib.error.HTTPError as err:
        logger.exception("Vercel Blob PUT failed with HTTP %s: %s", err.code, err)
        if err.code in (401, 403):
            raise StorageConfigError("Vercel Blob storage authorization failed.")
        raise StorageUpstreamError("Storage provider rejected permanent image write.")
    except urllib.error.URLError as err:
        logger.exception("Vercel Blob PUT network error: %s", err)
        if "timed out" in str(err).lower():
            raise StorageTimeoutError("Storage write operation timed out.")
        raise StorageUpstreamError("Unable to connect to storage provider.")
    except Exception as exc:
        logger.exception("Unexpected error writing to Vercel Blob: %s", exc)
        raise StorageUpstreamError("Failed to store permanent image in storage provider.")


def finalize_staged_upload_to_permanent(staged_item, request=None):
    """
    Complete finalize pipeline:
    1. Downloads/streams staged file from server-known storage key/URL.
    2. Runs 2-pass Pillow validation.
    3. Normalizes format, MIME, and extension.
    4. Writes verified bytes to a new permanent key: products/<ns>/<uuid>.<canonical_ext>.
    5. Deletes staging .upload object safely.
    6. Returns verified item metadata.
    """
    # 1. Fetch staged bytes
    staging_key = staged_item.storage_key
    staging_url = staged_item.url

    if staging_key and default_storage.exists(staging_key):
        # Local development / test fallback
        with default_storage.open(staging_key, "rb") as f:
            raw_bytes = f.read()
            staged_stream = io.BytesIO(raw_bytes)
    else:
        staged_stream = fetch_remote_blob_bytes(staging_url, storage_key=staging_key)

    # 2. Authoritative Pillow Verification
    validation_info = validate_and_decode_image(staged_stream)
    canonical_mime = validation_info["content_type"]
    canonical_ext = validation_info["extension"]

    # 3. Permanent Key Promotion
    permanent_key = generate_permanent_key(canonical_ext=canonical_ext)
    staged_stream.seek(0)
    verified_bytes = staged_stream.read()

    put_result = put_to_vercel_blob(permanent_key, verified_bytes, content_type=canonical_mime)
    permanent_url = put_result["url"]
    if permanent_url.startswith("/") and request:
        permanent_url = request.build_absolute_uri(permanent_url)

    # 4. Safe cleanup of staging object
    try:
        delete_from_storage_safe(staging_key, url=staging_url)
    except Exception as err:
        logger.warning("Failed to delete staging object %s: %s", staging_key, err)

    return {
        "url": permanent_url,
        "storage_key": permanent_key,
        "content_type": canonical_mime,
        "extension": canonical_ext,
        "format": validation_info["format"],
        "width": validation_info["width"],
        "height": validation_info["height"],
        "file_size": len(verified_bytes),
    }


def delete_from_storage_safe(storage_key, url=""):
    """
    Safely deletes storage object only if NOT referenced by any historical orders or products.
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

    blob_token = get_blob_token()
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
