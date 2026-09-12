import logging
import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


class ShiprocketException(Exception):
    """Base exception for Shiprocket integration errors."""
    pass


class ShiprocketConfigError(ShiprocketException):
    """Raised when Shiprocket configuration or environment variables are missing."""
    pass


class ShiprocketAuthError(ShiprocketException):
    """Raised when authentication with Shiprocket fails."""
    pass


class ShiprocketTimeoutError(ShiprocketException):
    """Raised when Shiprocket API requests time out."""
    pass


class ShiprocketConnectionError(ShiprocketException):
    """Raised when network/connectivity issues occur with Shiprocket API."""
    pass


class ShiprocketClient:
    """
    Secure client service for interacting with Shiprocket APIs.
    Caches authentication tokens and prevents sensitive credential leakage.
    """

    CACHE_KEY = "shiprocket_auth_token"
    CACHE_TTL = 86400  # 24 hours (Shiprocket token is valid for 10 days / 240 hrs)
    DEFAULT_TIMEOUT = 10.0  # seconds

    def __init__(self, email=None, password=None, base_url=None, pickup_location=None, timeout=None):
        self.email = email if email is not None else getattr(settings, "SHIPROCKET_API_EMAIL", "")
        self.password = password if password is not None else getattr(settings, "SHIPROCKET_API_PASSWORD", "")
        self.base_url = (base_url if base_url is not None else getattr(settings, "SHIPROCKET_API_BASE_URL", "https://apiv2.shiprocket.in")).rstrip("/")
        self.pickup_location = pickup_location if pickup_location is not None else getattr(settings, "SHIPROCKET_PICKUP_LOCATION", "work")
        self.timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT

    def authenticate(self):
        """
        Authenticate against Shiprocket API and obtain an auth token.
        Never logs or returns raw credentials.
        """
        if not self.email or not self.password:
            raise ShiprocketConfigError("Shiprocket API credentials are not configured.")

        url = f"{self.base_url}/v1/external/auth/login"
        payload = {
            "email": self.email,
            "password": self.password,
        }
        headers = {
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            logger.warning("Shiprocket login request timed out.")
            raise ShiprocketTimeoutError("Shiprocket API connection timed out.")
        except requests.exceptions.RequestException:
            logger.warning("Shiprocket login request failed due to a network error.")
            raise ShiprocketConnectionError("Unable to connect to Shiprocket API.")

        if response.status_code == 200:
            try:
                data = response.json()
            except Exception:
                raise ShiprocketConnectionError("Invalid JSON response received from Shiprocket API.")

            token = data.get("token")
            if not token:
                raise ShiprocketAuthError("Authentication succeeded but token was not returned.")

            cache.set(self.CACHE_KEY, token, timeout=self.CACHE_TTL)
            return token

        if response.status_code in (401, 403, 422):
            logger.warning("Shiprocket authentication failed with status %s", response.status_code)
            raise ShiprocketAuthError("Shiprocket authentication failed: Invalid credentials.")

        logger.warning("Shiprocket returned unexpected status %s", response.status_code)
        raise ShiprocketConnectionError(f"Shiprocket API returned unexpected status {response.status_code}.")

    def get_token(self, force_refresh=False):
        """
        Retrieve cached token or obtain a new one if expired / force_refresh is True.
        """
        if not force_refresh:
            cached_token = cache.get(self.CACHE_KEY)
            if cached_token:
                return cached_token

        return self.authenticate()

    def invalidate_token(self):
        """Clear cached token when 401 Unauthorized occurs on subsequent API requests."""
        cache.delete(self.CACHE_KEY)

    def test_connection(self):
        """
        Perform a safe connection check against Shiprocket API.
        Guarantees that sensitive tokens and passwords are never returned.
        """
        try:
            self.get_token(force_refresh=True)
            return {
                "connected": True,
                "pickupLocation": self.pickup_location,
                "message": "Shiprocket connection successful",
            }
        except ShiprocketConfigError as exc:
            return {
                "connected": False,
                "pickupLocation": self.pickup_location,
                "message": str(exc),
            }
        except ShiprocketAuthError as exc:
            return {
                "connected": False,
                "pickupLocation": self.pickup_location,
                "message": str(exc),
            }
        except ShiprocketTimeoutError as exc:
            return {
                "connected": False,
                "pickupLocation": self.pickup_location,
                "message": str(exc),
            }
        except ShiprocketConnectionError as exc:
            return {
                "connected": False,
                "pickupLocation": self.pickup_location,
                "message": str(exc),
            }
        except Exception:
            return {
                "connected": False,
                "pickupLocation": self.pickup_location,
                "message": "Shiprocket connection failed due to an unexpected error.",
            }
