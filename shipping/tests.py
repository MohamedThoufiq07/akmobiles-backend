from unittest.mock import patch, MagicMock
import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from shipping.client import (
    ShiprocketClient,
    ShiprocketConfigError,
    ShiprocketAuthError,
    ShiprocketTimeoutError,
    ShiprocketConnectionError,
)

User = get_user_model()


class ShiprocketClientUnitTests(TestCase):
    """Unit tests for the ShiprocketClient service."""

    def setUp(self):
        cache.clear()
        self.email = "test@akmobiles.com"
        self.password = "secret_pass123"
        self.base_url = "https://apiv2.shiprocket.in"
        self.pickup_location = "work"

    def tearDown(self):
        cache.clear()

    @patch("shipping.client.requests.post")
    def test_successful_authentication_and_caching(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": 12345,
            "first_name": "AK",
            "last_name": "Mobiles",
            "email": self.email,
            "company_id": 999,
            "created_at": "2026-09-12 10:00:00",
            "token": "mocked_jwt_token_abcdef123456",
        }
        mock_post.return_value = mock_response

        client = ShiprocketClient(
            email=self.email,
            password=self.password,
            base_url=self.base_url,
            pickup_location=self.pickup_location,
        )

        token = client.authenticate()
        self.assertEqual(token, "mocked_jwt_token_abcdef123456")

        # Test token caching in get_token
        cached_token = client.get_token()
        self.assertEqual(cached_token, "mocked_jwt_token_abcdef123456")
        # Ensure it was cached and not fetched again
        self.assertEqual(mock_post.call_count, 1)

        # Test force_refresh
        refreshed_token = client.get_token(force_refresh=True)
        self.assertEqual(refreshed_token, "mocked_jwt_token_abcdef123456")
        self.assertEqual(mock_post.call_count, 2)

        # Invalidate cache
        client.invalidate_token()
        self.assertIsNone(cache.get(client.CACHE_KEY))

    @patch("shipping.client.requests.post")
    def test_invalid_credentials_raises_auth_error(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"message": "Invalid email or password."}
        mock_post.return_value = mock_response

        client = ShiprocketClient(
            email=self.email,
            password="wrong_password",
            base_url=self.base_url,
        )

        with self.assertRaises(ShiprocketAuthError) as ctx:
            client.authenticate()

        self.assertIn("Invalid credentials", str(ctx.exception))
        # Ensure credentials/tokens are not in exception string
        self.assertNotIn("wrong_password", str(ctx.exception))

    def test_missing_credentials_raises_config_error(self):
        client = ShiprocketClient(email="", password="")
        with self.assertRaises(ShiprocketConfigError) as ctx:
            client.authenticate()
        self.assertIn("credentials are not configured", str(ctx.exception))

    @patch("shipping.client.requests.post")
    def test_shiprocket_timeout_raises_timeout_error(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout("Connection timed out")

        client = ShiprocketClient(
            email=self.email,
            password=self.password,
            base_url=self.base_url,
        )

        with self.assertRaises(ShiprocketTimeoutError) as ctx:
            client.authenticate()
        self.assertIn("timed out", str(ctx.exception))

    @patch("shipping.client.requests.post")
    def test_shiprocket_connection_error(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("DNS failure")

        client = ShiprocketClient(
            email=self.email,
            password=self.password,
            base_url=self.base_url,
        )

        with self.assertRaises(ShiprocketConnectionError) as ctx:
            client.authenticate()
        self.assertIn("Unable to connect", str(ctx.exception))

    @patch("shipping.client.requests.post")
    def test_test_connection_returns_structured_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"token": "valid_token_xyz"}
        mock_post.return_value = mock_response

        client = ShiprocketClient(
            email=self.email,
            password=self.password,
            pickup_location="warehouse_primary",
        )

        result = client.test_connection()
        self.assertEqual(
            result,
            {
                "connected": True,
                "pickupLocation": "warehouse_primary",
                "message": "Shiprocket connection successful",
            },
        )
        self.assertNotIn("token", result)
        self.assertNotIn("valid_token_xyz", str(result))

    @patch("shipping.client.requests.post")
    def test_test_connection_returns_structured_failure_on_error(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_post.return_value = mock_response

        client = ShiprocketClient(
            email=self.email,
            password="bad",
            pickup_location="warehouse_primary",
        )

        result = client.test_connection()
        self.assertFalse(result["connected"])
        self.assertEqual(result["pickupLocation"], "warehouse_primary")
        self.assertIn("Invalid credentials", result["message"])
        self.assertNotIn("bad", str(result))


class ShiprocketConnectionStatusAPITests(TestCase):
    """API endpoint tests for /api/shipping/shiprocket/connection-status/."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()

        # Admin User
        self.admin = User.objects.create_user(
            email="admin@akmobiles.com",
            password="AdminPassword123!",
            name="Admin User",
            role="admin",
        )

        # Regular Customer
        self.customer = User.objects.create_user(
            email="customer@akmobiles.com",
            password="CustomerPassword123!",
            name="Regular Customer",
            role="customer",
        )

    def tearDown(self):
        cache.clear()

    def test_unauthenticated_user_rejected_401(self):
        res = self.client.get("/api/shipping/shiprocket/connection-status/")
        self.assertEqual(res.status_code, 401)

    def test_regular_customer_rejected_403(self):
        self.client.force_authenticate(user=self.customer)
        res = self.client.get("/api/shipping/shiprocket/connection-status/")
        self.assertEqual(res.status_code, 403)

    @patch("shipping.client.requests.post")
    @override_settings(
        SHIPROCKET_API_EMAIL="test@akmobiles.com",
        SHIPROCKET_API_PASSWORD="valid_password",
        SHIPROCKET_PICKUP_LOCATION="work",
        SHIPROCKET_API_BASE_URL="https://apiv2.shiprocket.in",
    )
    def test_admin_successful_connection_check(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"token": "super_secret_token_12345"}
        mock_post.return_value = mock_response

        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/shipping/shiprocket/connection-status/")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.json(),
            {
                "connected": True,
                "pickupLocation": "work",
                "message": "Shiprocket connection successful",
            },
        )
        # Verify secret token is never in response body
        self.assertNotIn("super_secret_token_12345", res.content.decode("utf-8"))
        self.assertNotIn("valid_password", res.content.decode("utf-8"))

    @patch("shipping.client.requests.post")
    @override_settings(
        SHIPROCKET_API_EMAIL="test@akmobiles.com",
        SHIPROCKET_API_PASSWORD="invalid_password",
        SHIPROCKET_PICKUP_LOCATION="work",
    )
    def test_admin_connection_check_invalid_credentials(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"message": "Unauthorized"}
        mock_post.return_value = mock_response

        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/shipping/shiprocket/connection-status/")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["connected"])
        self.assertEqual(data["pickupLocation"], "work")
        self.assertIn("Invalid credentials", data["message"])
        self.assertNotIn("invalid_password", res.content.decode("utf-8"))

    @override_settings(
        SHIPROCKET_API_EMAIL="",
        SHIPROCKET_API_PASSWORD="",
        SHIPROCKET_PICKUP_LOCATION="work",
    )
    def test_admin_connection_check_missing_env_vars(self):
        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/shipping/shiprocket/connection-status/")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["connected"])
        self.assertEqual(data["pickupLocation"], "work")
        self.assertIn("credentials are not configured", data["message"])

    @patch("shipping.client.requests.post")
    @override_settings(
        SHIPROCKET_API_EMAIL="test@akmobiles.com",
        SHIPROCKET_API_PASSWORD="valid_password",
        SHIPROCKET_PICKUP_LOCATION="work",
    )
    def test_admin_connection_check_shiprocket_timeout(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout("Read timeout")

        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/shipping/shiprocket/connection-status/")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["connected"])
        self.assertEqual(data["pickupLocation"], "work")
        self.assertIn("timed out", data["message"])

    @patch("shipping.client.requests.post")
    @override_settings(
        SHIPROCKET_API_EMAIL="test@akmobiles.com",
        SHIPROCKET_API_PASSWORD="valid_password",
        SHIPROCKET_PICKUP_LOCATION="work",
    )
    def test_no_trailing_slash_endpoint_works(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"token": "token_123"}
        mock_post.return_value = mock_response

        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/shipping/shiprocket/connection-status")

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["connected"])
