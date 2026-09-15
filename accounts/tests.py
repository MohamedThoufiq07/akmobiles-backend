import threading
from unittest.mock import patch
from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import SocialAccount, User
from accounts.views import make_token
from accounts.google_services import (
    GoogleAuthConfigError,
    GoogleAuthInvalidTokenError,
    GoogleAuthExpiredTokenError,
    GoogleAuthUnverifiedEmailError,
)


class GoogleAuthTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.endpoint = "/api/accounts/google/"
        self.client_id = "test-google-client-id.apps.googleusercontent.com"
        # Set settings.GOOGLE_OAUTH_CLIENT_ID for tests
        self.patcher = patch.object(settings, "GOOGLE_OAUTH_CLIENT_ID", self.client_id)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_01_new_verified_google_customer_creation(self, mock_verify):
        mock_verify.return_value = {
            "sub": "google-sub-12345",
            "email": "customer1@example.com",
            "email_verified": True,
            "name": "Customer One",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "valid_token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data.get("success"))
        self.assertIn("token", response.data)
        self.assertEqual(response.data["user"]["email"], "customer1@example.com")
        self.assertEqual(response.data["user"]["name"], "Customer One")
        self.assertEqual(response.data["user"]["role"], User.ROLE_USER)

        user = User.objects.get(email="customer1@example.com")
        self.assertEqual(user.role, User.ROLE_USER)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_02_social_account_created_using_google_sub(self, mock_verify):
        mock_verify.return_value = {
            "sub": "google-sub-unique-999",
            "email": "customer2@example.com",
            "email_verified": True,
            "name": "Customer Two",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "valid_token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        social_acc = SocialAccount.objects.get(provider="google", provider_user_id="google-sub-unique-999")
        self.assertEqual(social_acc.user.email, "customer2@example.com")
        self.assertEqual(social_acc.email, "customer2@example.com")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_03_new_google_only_user_has_unusable_password(self, mock_verify):
        mock_verify.return_value = {
            "sub": "google-sub-pw-test",
            "email": "googleonly@example.com",
            "email_verified": True,
            "name": "Google Only",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        self.client.post(self.endpoint, {"credential": "valid_token"}, format="json")
        user = User.objects.get(email="googleonly@example.com")
        self.assertFalse(user.has_usable_password())

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_04_existing_social_account_returns_same_user(self, mock_verify):
        user = User.objects.create_user(
            email="existing_social@example.com",
            name="Existing Social",
            role=User.ROLE_USER,
        )
        SocialAccount.objects.create(
            user=user,
            provider="google",
            provider_user_id="google-sub-existing",
            email="existing_social@example.com",
        )
        mock_verify.return_value = {
            "sub": "google-sub-existing",
            "email": "existing_social@example.com",
            "email_verified": True,
            "name": "Existing Social",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "valid_token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["email"], "existing_social@example.com")
        self.assertEqual(User.objects.filter(email="existing_social@example.com").count(), 1)

    def test_05_missing_credential_returns_400(self):
        response = self.client.post(self.endpoint, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get("code"), "GOOGLE_CREDENTIAL_REQUIRED")

        response_empty = self.client.post(self.endpoint, {"credential": "   "}, format="json")
        self.assertEqual(response_empty.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response_empty.data.get("code"), "GOOGLE_CREDENTIAL_REQUIRED")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_06_invalid_token_returns_401(self, mock_verify):
        mock_verify.side_effect = ValueError("Invalid token signature")
        response = self.client.post(self.endpoint, {"credential": "bad_token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get("code"), "GOOGLE_TOKEN_INVALID")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_07_expired_token_returns_401(self, mock_verify):
        mock_verify.side_effect = ValueError("Token used too late: expired token")
        response = self.client.post(self.endpoint, {"credential": "expired_token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get("code"), "GOOGLE_TOKEN_EXPIRED")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_08_wrong_audience_returns_401(self, mock_verify):
        mock_verify.return_value = {
            "sub": "sub-wrong-aud",
            "email": "wrongaud@example.com",
            "email_verified": True,
            "aud": "wrong-client-id.apps.googleusercontent.com",
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get("code"), "GOOGLE_TOKEN_INVALID")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_09_invalid_issuer_returns_401(self, mock_verify):
        mock_verify.return_value = {
            "sub": "sub-wrong-iss",
            "email": "wrongiss@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://malicious-issuer.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get("code"), "GOOGLE_TOKEN_INVALID")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_10_unverified_email_returns_401(self, mock_verify):
        mock_verify.return_value = {
            "sub": "sub-unverified",
            "email": "unverified@example.com",
            "email_verified": False,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get("code"), "GOOGLE_EMAIL_UNVERIFIED")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_11_missing_sub_or_email_returns_safe_error(self, mock_verify):
        mock_verify.return_value = {
            "email": "missing_sub@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get("code"), "GOOGLE_TOKEN_INVALID")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_12_inactive_customer_rejected(self, mock_verify):
        user = User.objects.create_user(
            email="inactive@example.com",
            name="Inactive User",
            is_active=False,
            role=User.ROLE_USER,
        )
        SocialAccount.objects.create(
            user=user,
            provider="google",
            provider_user_id="google-sub-inactive",
            email="inactive@example.com",
        )
        mock_verify.return_value = {
            "sub": "google-sub-inactive",
            "email": "inactive@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("code"), "ACCOUNT_INACTIVE")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_13_admin_rejected(self, mock_verify):
        User.objects.create_user(
            email="admin@example.com",
            password="adminpassword123",
            role=User.ROLE_ADMIN,
        )
        mock_verify.return_value = {
            "sub": "google-sub-admin",
            "email": "admin@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("code"), "CUSTOMER_ACCOUNT_REQUIRED")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_14_staff_rejected(self, mock_verify):
        User.objects.create_user(
            email="staff@example.com",
            password="staffpassword123",
            is_staff=True,
            role=User.ROLE_USER,
        )
        mock_verify.return_value = {
            "sub": "google-sub-staff",
            "email": "staff@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("code"), "CUSTOMER_ACCOUNT_REQUIRED")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_15_superuser_rejected(self, mock_verify):
        User.objects.create_superuser(
            email="superuser@example.com",
            password="superpassword123",
        )
        mock_verify.return_value = {
            "sub": "google-sub-super",
            "email": "superuser@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("code"), "CUSTOMER_ACCOUNT_REQUIRED")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_16_every_non_customer_role_rejected(self, mock_verify):
        user = User.objects.create_user(
            email="customrole@example.com",
            role="manager",
        )
        SocialAccount.objects.create(
            user=user,
            provider="google",
            provider_user_id="google-sub-custom-role",
            email="customrole@example.com",
        )
        mock_verify.return_value = {
            "sub": "google-sub-custom-role",
            "email": "customrole@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("code"), "CUSTOMER_ACCOUNT_REQUIRED")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_17_existing_password_customer_returns_409_account_link_required(self, mock_verify):
        User.objects.create_user(
            email="localcust@example.com",
            password="mypassword123",
            role=User.ROLE_USER,
        )
        mock_verify.return_value = {
            "sub": "google-sub-localcust",
            "email": "localcust@example.com",
            "email_verified": True,
            "name": "Local Cust",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data.get("code"), "ACCOUNT_LINK_REQUIRED")
        self.assertFalse(response.data.get("success"))

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_18_wrong_existing_password_cannot_link(self, mock_verify):
        User.objects.create_user(
            email="localcust2@example.com",
            password="correctpassword123",
            role=User.ROLE_USER,
        )
        mock_verify.return_value = {
            "sub": "google-sub-localcust2",
            "email": "localcust2@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(
            self.endpoint,
            {"credential": "token", "password": "wrongpassword"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data.get("code"), "ACCOUNT_LINK_PASSWORD_INVALID")
        self.assertFalse(SocialAccount.objects.filter(provider="google", email="localcust2@example.com").exists())

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_19_correct_password_securely_links_and_signs_in(self, mock_verify):
        user = User.objects.create_user(
            email="localcust3@example.com",
            password="correctpassword123",
            role=User.ROLE_USER,
        )
        mock_verify.return_value = {
            "sub": "google-sub-localcust3",
            "email": "localcust3@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(
            self.endpoint,
            {"credential": "token", "password": "correctpassword123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data.get("success"))
        self.assertIn("token", response.data)
        self.assertTrue(SocialAccount.objects.filter(user=user, provider="google", provider_user_id="google-sub-localcust3").exists())

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_20_existing_unusable_password_account_is_not_duplicated(self, mock_verify):
        user = User.objects.create_user(
            email="unusable@example.com",
            role=User.ROLE_USER,
        )
        user.set_unusable_password()
        user.save()

        mock_verify.return_value = {
            "sub": "google-sub-unusable",
            "email": "unusable@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("code"), "ACCOUNT_LINK_NOT_ALLOWED")
        self.assertEqual(User.objects.filter(email="unusable@example.com").count(), 1)

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_21_concurrent_first_login_does_not_create_duplicate_users(self, mock_verify):
        mock_verify.return_value = {
            "sub": "google-sub-concurrent",
            "email": "concurrent@example.com",
            "email_verified": True,
            "name": "Concurrent User",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        res1 = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        res2 = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

        self.assertEqual(User.objects.filter(email="concurrent@example.com").count(), 1)
        self.assertEqual(SocialAccount.objects.filter(provider="google", provider_user_id="google-sub-concurrent").count(), 1)

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_22_response_matches_existing_password_login_jwt_structure(self, mock_verify):
        # Create normal user and test normal login response structure
        User.objects.create_user(
            email="format_test@example.com",
            password="password123",
            role=User.ROLE_USER,
        )
        pw_response = self.client.post(
            "/api/auth/login",
            {"email": "format_test@example.com", "password": "password123"},
            format="json",
        )
        self.assertEqual(pw_response.status_code, status.HTTP_200_OK)
        expected_keys = set(pw_response.data.keys())

        # Test Google login response structure
        mock_verify.return_value = {
            "sub": "google-sub-format-new",
            "email": "format_google@example.com",
            "email_verified": True,
            "name": "Format Google",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        g_response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(g_response.status_code, status.HTTP_200_OK)
        self.assertTrue(set(g_response.data.keys()).issuperset(expected_keys))
        self.assertEqual(set(g_response.data["user"].keys()), set(pw_response.data["user"].keys()))

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_23_google_token_is_not_persisted(self, mock_verify):
        mock_verify.return_value = {
            "sub": "google-sub-persist-check",
            "email": "persistcheck@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        raw_credential = "super_secret_id_token_12345"
        self.client.post(self.endpoint, {"credential": raw_credential}, format="json")
        # Check that token does not exist in any database field
        user = User.objects.get(email="persistcheck@example.com")
        self.assertNotIn(raw_credential, str(user.__dict__))
        social = SocialAccount.objects.get(user=user)
        self.assertNotIn(raw_credential, str(social.__dict__))

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_24_password_and_credential_are_never_returned(self, mock_verify):
        mock_verify.return_value = {
            "sub": "google-sub-leak-check",
            "email": "leakcheck@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token_val"}, format="json")
        self.assertNotIn("password", response.data)
        self.assertNotIn("password", response.data["user"])
        self.assertNotIn("credential", response.data)

    def test_25_only_canonical_endpoint_registered(self):
        from django.urls import resolve, Resolver404
        from accounts.views import google_login

        # /api/accounts/google/ resolves directly to google_login
        resolved = resolve("/api/accounts/google/")
        self.assertEqual(resolved.func, google_login)

        # /api/auth/google/ must NOT exist (Resolver404)
        with self.assertRaises(Resolver404):
            resolve("/api/auth/google/")

        # POST /api/accounts/google/ with empty body returns GOOGLE_CREDENTIAL_REQUIRED
        res_canon = self.client.post("/api/accounts/google/", {}, format="json")
        self.assertEqual(res_canon.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_canon.data.get("code"), "GOOGLE_CREDENTIAL_REQUIRED")

        # Existing password login URL continues working
        User.objects.create_user(email="urltest@example.com", password="password123", role=User.ROLE_USER)
        res_login = self.client.post(
            "/api/auth/login",
            {"email": "urltest@example.com", "password": "password123"},
            format="json",
        )
        self.assertEqual(res_login.status_code, status.HTTP_200_OK)

        # Existing registration URL continues working
        res_reg = self.client.post(
            "/api/auth/register",
            {"name": "New Reg", "email": "newreg@example.com", "password": "password123"},
            format="json",
        )
        self.assertEqual(res_reg.status_code, status.HTTP_201_CREATED)

    def test_26_get_request_is_rejected(self):
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_27_throttling_behaviour_configured(self):
        from accounts.views import GoogleAuthRateThrottle
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        throttle = GoogleAuthRateThrottle()

        class DummyRequest:
            method = "POST"
            user = None
            META = {"REMOTE_ADDR": "127.0.0.1"}
            def __init__(self, data):
                self.data = data

        req_normal = DummyRequest({"credential": "test"})
        throttle.allow_request(req_normal, None)
        self.assertEqual(throttle.scope, "google_auth")

        req_pw = DummyRequest({"credential": "test", "password": "pw"})
        throttle.allow_request(req_pw, None)
        self.assertEqual(throttle.scope, "google_link_password")

        self.assertIn("google_auth", settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}))
        self.assertIn("google_link_password", settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}))

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_28_password_link_rate_limit_exceeded_returns_429(self, mock_verify):
        from django.core.cache import cache
        cache.clear()

        User.objects.create_user(
            email="throttle_link@example.com",
            password="correctpassword123",
            role=User.ROLE_USER,
        )
        mock_verify.return_value = {
            "sub": "google-sub-throttle",
            "email": "throttle_link@example.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }

        # 5 allowed attempts
        for _ in range(5):
            res = self.client.post(
                self.endpoint,
                {"credential": "token", "password": "wrongpassword"},
                format="json",
            )
            self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

        # 6th attempt is throttled with HTTP 429
        res_throttled = self.client.post(
            self.endpoint,
            {"credential": "token", "password": "wrongpassword"},
            format="json",
        )
        self.assertEqual(res_throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        cache.clear()


