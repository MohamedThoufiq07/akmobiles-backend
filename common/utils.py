"""
Shared helpers.

generate_object_id() produces a 24-character hex string that looks exactly like a
MongoDB ObjectId. We use it as the primary key for every model so that the JSON
the React frontend receives has the same `_id` shape it had on the MERN stack
(e.g. "65f1c2a4e8b3a91d4c7f0021"). That means existing frontend URLs like
/products/:id and any cached data keep working with zero changes.
"""

import binascii
import os
import time
from urllib.parse import quote, urlparse


_counter = int.from_bytes(os.urandom(3), "big")


def generate_object_id() -> str:
    """Return a 24-char hex string mimicking a MongoDB ObjectId."""
    global _counter
    timestamp = int(time.time()).to_bytes(4, "big")
    random_part = os.urandom(5)
    _counter = (_counter + 1) & 0xFFFFFF
    counter = _counter.to_bytes(3, "big")
    return binascii.hexlify(timestamp + random_part + counter).decode()


def sanitize_image_url(url: str, fallback_label: str = "Product") -> str:
    """
    Sanitizes image URLs to prevent Mixed Content, CORS, and net::ERR_CONNECTION_REFUSED
    errors for localhost, 127.0.0.1, private-network, or invalid mock URLs in production.
    """
    fallback = f"https://placehold.co/600x600/f1f5f9/64748b?text={quote(str(fallback_label or 'Product'))}"
    if not url or not isinstance(url, str):
        return fallback

    trimmed = url.strip()
    if not trimmed or "img/1.jpg" in trimmed or "http://img" in trimmed or trimmed.startswith("//img"):
        return fallback

    if trimmed.startswith("data:image/") or trimmed.startswith("/") or trimmed.startswith("./"):
        return trimmed

    try:
        parsed = urlparse(trimmed)
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return fallback

        # Check local / loopback / private network hostnames
        if (
            hostname in ["localhost", "127.0.0.1", "::1", "0.0.0.0"]
            or hostname.endswith(".localhost")
            or hostname.startswith("192.168.")
            or hostname.startswith("10.")
            or (
                hostname.startswith("172.")
                and len(hostname.split(".")) > 1
                and hostname.split(".")[1].isdigit()
                and 16 <= int(hostname.split(".")[1]) <= 31
            )
        ):
            return fallback

        # If protocol is http, upgrade to https to avoid mixed content in production
        if parsed.scheme == "http":
            return parsed._replace(scheme="https").geturl()

        if parsed.scheme == "https":
            return trimmed

        return fallback
    except Exception:
        return fallback

