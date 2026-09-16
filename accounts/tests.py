from unittest.mock import patch
from django.conf import settings
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import SocialAccount, User


class GoogleAuthTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.endpoint = "/api/accounts/google/"
        self.client_id = "test-google-client-id.apps.googleusercontent.com"
        self.patcher = patch.object(settings, "GOOGLE_OAUTH_CLIENT_ID", self.client_id)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_01_new_google_customer_creation(self, mock_verify):
        mock_verify.return_value = {
            "sub": "google-sub-12345",
            "email": "customer1@gmail.com",
            "email_verified": True,
            "name": "Customer One",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "valid_token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data.get("success"))
        self.assertIn("token", response.data)
        self.assertEqual(response.data["user"]["email"], "customer1@gmail.com")
        self.assertEqual(response.data["user"]["name"], "Customer One")
        self.assertEqual(response.data["user"]["role"], User.ROLE_USER)

        user = User.objects.get(email="customer1@gmail.com")
        self.assertEqual(user.role, User.ROLE_USER)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())

        social = SocialAccount.objects.get(provider="google", provider_user_id="google-sub-12345")
        self.assertEqual(social.user, user)

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_02_existing_gmail_customer_automatically_linked_without_password(self, mock_verify):
        existing_user = User.objects.create_user(
            email="existing_customer@gmail.com",
            password="mysecretpassword123",
            name="Existing Customer",
            role=User.ROLE_USER,
        )
        mock_verify.return_value = {
            "sub": "google-sub-gmail-link",
            "email": "EXISTING_CUSTOMER@GMAIL.COM",
            "email_verified": True,
            "name": "Existing Customer",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        # Notice: request payload only contains credential, no password
        response = self.client.post(self.endpoint, {"credential": "valid_token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data.get("success"))
        self.assertEqual(response.data["user"]["email"], "existing_customer@gmail.com")

        # Confirm social account was automatically created and linked to existing user
        social = SocialAccount.objects.get(user=existing_user, provider="google")
        self.assertEqual(social.provider_user_id, "google-sub-gmail-link")
        # Password remains intact on existing account
        self.assertTrue(existing_user.check_password("mysecretpassword123"))

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_03_existing_workspace_customer_with_hd_automatically_linked(self, mock_verify):
        existing_user = User.objects.create_user(
            email="alex@company.com",
            password="workpassword123",
            name="Alex Workspace",
            role=User.ROLE_USER,
        )
        mock_verify.return_value = {
            "sub": "google-sub-workspace-1",
            "email": "alex@company.com",
            "email_verified": True,
            "hd": "company.com",
            "name": "Alex Workspace",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "valid_token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data.get("success"))
        self.assertEqual(response.data["user"]["email"], "alex@company.com")

        social = SocialAccount.objects.get(user=existing_user, provider="google")
        self.assertEqual(social.provider_user_id, "google-sub-workspace-1")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_04_repeat_google_login_uses_provider_sub(self, mock_verify):
        user = User.objects.create_user(
            email="repeat@gmail.com",
            name="Repeat User",
            role=User.ROLE_USER,
        )
        SocialAccount.objects.create(
            user=user,
            provider="google",
            provider_user_id="google-sub-repeat",
            email="repeat@gmail.com",
        )
        mock_verify.return_value = {
            "sub": "google-sub-repeat",
            "email": "repeat_changed_in_token@gmail.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "valid_token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Returns user resolved via sub
        self.assertEqual(response.data["user"]["email"], "repeat@gmail.com")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_05_existing_non_authoritative_third_party_email_not_automatically_linked(self, mock_verify):
        User.objects.create_user(
            email="someone@yahoo.com",
            password="yahoopassword123",
            role=User.ROLE_USER,
        )
        mock_verify.return_value = {
            "sub": "google-sub-yahoo",
            "email": "someone@yahoo.com",
            "email_verified": True,
            # No hd claim and not @gmail.com
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "valid_token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get("code"), "GOOGLE_EMAIL_NOT_AUTHORITATIVE")
        self.assertIn("cannot be automatically linked", response.data.get("message", ""))
        self.assertFalse(SocialAccount.objects.filter(provider="google", provider_user_id="google-sub-yahoo").exists())

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_06_inactive_admin_staff_superuser_rejection(self, mock_verify):
        # 1. Inactive account
        inactive_user = User.objects.create_user(
            email="inactive@gmail.com",
            is_active=False,
            role=User.ROLE_USER,
        )
        SocialAccount.objects.create(
            user=inactive_user,
            provider="google",
            provider_user_id="sub-inactive",
            email="inactive@gmail.com",
        )
        mock_verify.return_value = {
            "sub": "sub-inactive",
            "email": "inactive@gmail.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        res1 = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(res1.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res1.data.get("code"), "ACCOUNT_INACTIVE")

        # 2. Admin account
        User.objects.create_user(email="admin@gmail.com", password="pw", role=User.ROLE_ADMIN)
        mock_verify.return_value = {
            "sub": "sub-admin",
            "email": "admin@gmail.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        res2 = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(res2.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res2.data.get("code"), "CUSTOMER_ACCOUNT_REQUIRED")

        # 3. Staff account
        User.objects.create_user(email="staff@gmail.com", password="pw", is_staff=True, role=User.ROLE_USER)
        mock_verify.return_value = {
            "sub": "sub-staff",
            "email": "staff@gmail.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        res3 = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(res3.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res3.data.get("code"), "CUSTOMER_ACCOUNT_REQUIRED")

        # 4. Superuser account
        User.objects.create_superuser(email="super@gmail.com", password="pw")
        mock_verify.return_value = {
            "sub": "sub-super",
            "email": "super@gmail.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        res4 = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(res4.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res4.data.get("code"), "CUSTOMER_ACCOUNT_REQUIRED")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_07_conflicting_google_sub_rejection(self, mock_verify):
        user = User.objects.create_user(
            email="conflict@gmail.com",
            name="Conflict User",
            role=User.ROLE_USER,
        )
        SocialAccount.objects.create(
            user=user,
            provider="google",
            provider_user_id="google-sub-first",
            email="conflict@gmail.com",
        )
        # Attempt to login with same email but DIFFERENT Google sub
        mock_verify.return_value = {
            "sub": "google-sub-second-different",
            "email": "conflict@gmail.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data.get("code"), "GOOGLE_ACCOUNT_CONFLICT")

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_08_concurrent_first_login_race_handling(self, mock_verify):
        mock_verify.return_value = {
            "sub": "google-sub-race",
            "email": "race@gmail.com",
            "email_verified": True,
            "name": "Race User",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        res1 = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        res2 = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

        self.assertEqual(User.objects.filter(email="race@gmail.com").count(), 1)
        self.assertEqual(SocialAccount.objects.filter(provider="google", provider_user_id="google-sub-race").count(), 1)

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_09_google_request_no_longer_requires_linking_password(self, mock_verify):
        User.objects.create_user(
            email="nopwreq@gmail.com",
            password="secretpassword",
            role=User.ROLE_USER,
        )
        mock_verify.return_value = {
            "sub": "google-sub-nopwreq",
            "email": "nopwreq@gmail.com",
            "email_verified": True,
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        # Passing extra password does not affect resolution or trigger old 409
        response = self.client.post(self.endpoint, {"credential": "token", "password": "ignored"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data.get("success"))

    @patch("accounts.google_services.id_token.verify_oauth2_token")
    def test_10_jwt_response_format_remains_correct(self, mock_verify):
        # Create normal password user to compare format
        User.objects.create_user(
            email="compare_format@gmail.com",
            password="password123",
            role=User.ROLE_USER,
        )
        pw_response = self.client.post(
            "/api/auth/login",
            {"email": "compare_format@gmail.com", "password": "password123"},
            format="json",
        )
        self.assertEqual(pw_response.status_code, status.HTTP_200_OK)

        # Google login format
        mock_verify.return_value = {
            "sub": "google-sub-jwt-test",
            "email": "jwt_test@gmail.com",
            "email_verified": True,
            "name": "JWT Test",
            "aud": self.client_id,
            "iss": "https://accounts.google.com",
        }
        g_response = self.client.post(self.endpoint, {"credential": "token"}, format="json")
        self.assertEqual(g_response.status_code, status.HTTP_200_OK)
        self.assertIn("token", g_response.data)
        self.assertIn("user", g_response.data)
        self.assertEqual(set(g_response.data["user"].keys()), set(pw_response.data["user"].keys()))
