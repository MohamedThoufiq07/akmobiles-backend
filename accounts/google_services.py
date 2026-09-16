import logging
from django.conf import settings
from django.db import transaction, IntegrityError
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from .models import User, SocialAccount

logger = logging.getLogger(__name__)

VALID_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleAuthException(Exception):
    """Base exception for Google authentication."""
    code = "GOOGLE_AUTH_ERROR"
    status_code = 400

    def __init__(self, message=None, code=None, status_code=None):
        self.message = message or "Authentication error"
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        super().__init__(self.message)


class GoogleAuthConfigError(GoogleAuthException):
    code = "GOOGLE_AUTH_NOT_CONFIGURED"
    status_code = 503


class GoogleAuthCredentialRequiredError(GoogleAuthException):
    code = "GOOGLE_CREDENTIAL_REQUIRED"
    status_code = 400


class GoogleAuthInvalidTokenError(GoogleAuthException):
    code = "GOOGLE_TOKEN_INVALID"
    status_code = 401


class GoogleAuthExpiredTokenError(GoogleAuthException):
    code = "GOOGLE_TOKEN_EXPIRED"
    status_code = 401


class GoogleAuthUnverifiedEmailError(GoogleAuthException):
    code = "GOOGLE_EMAIL_UNVERIFIED"
    status_code = 401


class AccountInactiveError(GoogleAuthException):
    code = "ACCOUNT_INACTIVE"
    status_code = 403


class CustomerAccountRequiredError(GoogleAuthException):
    code = "CUSTOMER_ACCOUNT_REQUIRED"
    status_code = 403


class GoogleAccountConflictError(GoogleAuthException):
    code = "GOOGLE_ACCOUNT_CONFLICT"
    status_code = 409


class GoogleEmailNotAuthoritativeError(GoogleAuthException):
    code = "GOOGLE_EMAIL_NOT_AUTHORITATIVE"
    status_code = 400


def _is_email_authoritative(google_payload: dict, normalized_email: str) -> bool:
    """
    Checks if Google is authoritative for the verified email.
    Google is authoritative if:
      - email ends with @gmail.com and email_verified is true, OR
      - email_verified is true and Google hd (hosted domain / Google Workspace) claim is present.
    """
    email_verified = google_payload.get("email_verified")
    is_verified = email_verified is True or str(email_verified).lower() == "true"
    if not is_verified:
        return False

    if normalized_email.endswith("@gmail.com"):
        return True

    hd = google_payload.get("hd")
    if hd and str(hd).strip():
        return True

    return False


def verify_google_id_token(credential: str, request_adapter=None) -> dict:
    """
    Verify Google ID token server-side using Google's official auth library.
    Never trusts client-supplied name, email, or verification flags.
    """
    if not credential or not isinstance(credential, str) or not credential.strip():
        raise GoogleAuthCredentialRequiredError("Google credential is required.")

    client_id = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    if not client_id or not client_id.strip():
        logger.error("GOOGLE_OAUTH_CLIENT_ID is not configured in settings.")
        raise GoogleAuthConfigError("Google authentication is not configured on this server.")

    adapter = request_adapter or google_requests.Request()

    try:
        payload = id_token.verify_oauth2_token(
            credential.strip(),
            adapter,
            audience=client_id.strip(),
        )
    except ValueError as exc:
        msg = str(exc).lower()
        if "expired" in msg:
            logger.warning("Google token verification failed: Token is expired.")
            raise GoogleAuthExpiredTokenError("Google token has expired. Please sign in again.")
        logger.warning("Google token verification failed: %s", exc)
        raise GoogleAuthInvalidTokenError("Invalid Google credential.")
    except Exception as exc:
        logger.warning("Google token verification error: %s", exc)
        raise GoogleAuthInvalidTokenError("Invalid Google credential.")

    # Validate Audience
    aud = payload.get("aud")
    if aud and aud != client_id.strip():
        logger.warning("Google token verification failed: Audience mismatch %s", aud)
        raise GoogleAuthInvalidTokenError("Invalid Google credential audience.")

    # Validate Issuer
    iss = payload.get("iss")
    if iss not in VALID_GOOGLE_ISSUERS:
        logger.warning("Google token verification failed: Invalid issuer %s", iss)
        raise GoogleAuthInvalidTokenError("Invalid Google credential issuer.")

    # Validate Subject
    sub = payload.get("sub")
    if not sub or not str(sub).strip():
        logger.warning("Google token verification failed: Missing sub claim.")
        raise GoogleAuthInvalidTokenError("Google token is missing required subject claim.")

    # Validate Email
    email = payload.get("email")
    if not email or not str(email).strip():
        logger.warning("Google token verification failed: Missing email claim.")
        raise GoogleAuthInvalidTokenError("Google token is missing required email claim.")

    # Validate Email Verification
    email_verified = payload.get("email_verified")
    if email_verified is not True and email_verified != "true":
        logger.warning("Google token verification failed: Email is not verified by Google.")
        raise GoogleAuthUnverifiedEmailError("Your Google email is not verified. Please verify your Google email.")

    return payload


def resolve_google_customer(google_payload: dict) -> User:
    """
    Safely resolves, auto-links, or provisions an AK Mobiles customer account associated with
    the verified Google payload. Enforces role restrictions, email authoritativeness, and atomic transactions.
    """
    sub = str(google_payload["sub"]).strip()
    raw_email = str(google_payload["email"]).strip()
    normalized_email = raw_email.lower()

    with transaction.atomic():
        # A. Check if SocialAccount already exists for this Google sub
        social_account = (
            SocialAccount.objects.select_for_update()
            .filter(provider="google", provider_user_id=sub)
            .select_related("user")
            .first()
        )

        if social_account:
            user = social_account.user
            _validate_customer_access(user)
            return user

        # B. Check if a local User exists with the same email
        existing_user = (
            User.objects.select_for_update()
            .filter(email__iexact=normalized_email)
            .first()
        )

        if existing_user:
            # Strictly validate customer access first — reject admin, staff, superuser, inactive
            _validate_customer_access(existing_user)

            # Check if this existing user is already linked to another Google identity
            existing_social = SocialAccount.objects.filter(user=existing_user, provider="google").first()
            if existing_social:
                if existing_social.provider_user_id != sub:
                    raise GoogleAccountConflictError(
                        "This account is linked to a different Google identity."
                    )

            # Validate whether Google is authoritative for this email before auto-linking
            if not _is_email_authoritative(google_payload, normalized_email):
                raise GoogleEmailNotAuthoritativeError(
                    "This Google account cannot be automatically linked. Please sign in using your email and password, or reset your password."
                )

            # Auto-link the existing customer account with Google SocialAccount
            try:
                SocialAccount.objects.create(
                    user=existing_user,
                    provider="google",
                    provider_user_id=sub,
                    email=normalized_email,
                )
            except IntegrityError:
                # Concurrent link race condition
                pass

            return existing_user

        # C. New customer provisioning
        name_claim = google_payload.get("name") or google_payload.get("given_name") or normalized_email.split("@")[0]
        name = str(name_claim).strip()[:50] or "Customer"

        try:
            user = User.objects.create_user(
                email=normalized_email,
                name=name,
                password=None,
                role=User.ROLE_USER,
            )
            user.set_unusable_password()
            user.is_staff = False
            user.is_superuser = False
            user.save()

            SocialAccount.objects.create(
                user=user,
                provider="google",
                provider_user_id=sub,
                email=normalized_email,
            )
            return user
        except IntegrityError:
            # Handle concurrent first-login attempt
            social_account = (
                SocialAccount.objects.filter(provider="google", provider_user_id=sub)
                .select_related("user")
                .first()
            )
            if social_account:
                _validate_customer_access(social_account.user)
                return social_account.user

            user = User.objects.filter(email__iexact=normalized_email).first()
            if user:
                _validate_customer_access(user)
                if _is_email_authoritative(google_payload, normalized_email):
                    SocialAccount.objects.get_or_create(
                        provider="google",
                        provider_user_id=sub,
                        defaults={"user": user, "email": normalized_email}
                    )
                    return user
                raise GoogleEmailNotAuthoritativeError(
                    "This Google account cannot be automatically linked. Please sign in using your email and password, or reset your password."
                )
            raise GoogleAccountConflictError("Account creation conflict occurred. Please try signing in again.")


def _validate_customer_access(user: User):
    """
    Strictly verifies that the user is an active normal customer.
    Rejects admin, staff, superuser, and inactive accounts.
    """
    if not user.is_active:
        raise AccountInactiveError("This account is inactive. Please contact support.")

    # Strictly require normal customer role
    if user.role != User.ROLE_USER or user.is_staff or user.is_superuser:
        logger.warning("Google login rejected for administrative user %s", user.email)
        raise CustomerAccountRequiredError("Google sign-in is only permitted for customer accounts.")
