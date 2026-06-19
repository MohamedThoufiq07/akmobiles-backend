"""
Django settings for the AK Mobiles backend.

This is a 1:1 port of the Express/MongoDB backend to Django + DRF + PostgreSQL.
Reads config from environment variables (see .env.example).
"""

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(key, default=False):
    return os.getenv(key, str(default)).lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env_bool("DEBUG", False)
NODE_ENV = os.getenv("NODE_ENV", "development")  # kept for parity (forgot-password demo token)

# ALLOWED_HOSTS from env (comma-separated), always including Vercel + localhost.
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
for _host in ("localhost", "127.0.0.1", ".vercel.app"):
    if _host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_host)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third party
    "rest_framework",
    "corsheaders",
    # local apps
    "accounts",
    "products",
    "orders",
    "payments",
    "core",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",          # must be high up
    "django.middleware.gzip.GZipMiddleware",          # = Express `compression()`
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",     # serve static in serverless prod
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "akmobiles.urls"
APPEND_SLASH = False  # Express routes have no trailing slash; keep URLs identical

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "akmobiles.wsgi.application"

# ---- Database: PostgreSQL ----
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "akmobiles"),
        "USER": os.getenv("DB_USER", "postgres"),
        "PASSWORD": os.getenv("DB_PASSWORD", "postgres"),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}

# Allow running quick checks on SQLite without Postgres (USE_SQLITE=true)
if env_bool("USE_SQLITE", False):
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }

# Production / managed Postgres (Neon): if DATABASE_URL is set it wins over the
# discrete DB_* vars and SQLite, with SSL required (Neon mandates sslmode=require).
_database_url = os.getenv("DATABASE_URL")
if _database_url:
    import dj_database_url

    DATABASES["default"] = dj_database_url.parse(
        _database_url, conn_max_age=0, ssl_require=True
    )

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 6}},  # matches the Mongoose minlength: 6
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"   # collectstatic target (a no-op-friendly dir)
MEDIA_URL = "/uploads/"          # = Express app.use('/uploads', ...)
MEDIA_ROOT = BASE_DIR / "uploads"

# WhiteNoise serves static straight from the app's finders (no collectstatic /
# populated STATIC_ROOT required at runtime), which suits Vercel's CDN model.
WHITENOISE_USE_FINDERS = True

# Storage backends (Django 4.2+ STORAGES API).
#   default     -> local FileSystemStorage for dev; swapped to Cloudinary below
#                  when CLOUDINARY env is present (Vercel's FS is ephemeral/RO).
#   staticfiles -> WhiteNoise compressed (non-manifest, works with finders).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# Cloudinary media storage in production. Activated only when credentials exist
# (CLOUDINARY_URL, or the cloud-name/key/secret trio); otherwise dev keeps local
# disk so uploads + the /api/upload contract are unchanged locally.
_cloudinary_configured = bool(
    os.getenv("CLOUDINARY_URL")
    or (
        os.getenv("CLOUDINARY_CLOUD_NAME")
        and os.getenv("CLOUDINARY_API_KEY")
        and os.getenv("CLOUDINARY_API_SECRET")
    )
)
if _cloudinary_configured:
    INSTALLED_APPS += ["cloudinary", "cloudinary_storage"]
    STORAGES["default"] = {"BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage"}
    if not os.getenv("CLOUDINARY_URL"):
        CLOUDINARY_STORAGE = {
            "CLOUD_NAME": os.getenv("CLOUDINARY_CLOUD_NAME"),
            "API_KEY": os.getenv("CLOUDINARY_API_KEY"),
            "API_SECRET": os.getenv("CLOUDINARY_API_SECRET"),
        }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024   # 10mb, like express.json limit

# ---- DRF ----
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.AllowAny",
    ),
    # We craft every response envelope by hand, so no default pagination renderer.
    "EXCEPTION_HANDLER": "common.exceptions.api_exception_handler",
}

# ---- simplejwt: single 7-day token, returned to the client as `token` ----
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=int(os.getenv("JWT_EXPIRE_DAYS", "7"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "_id",
    "USER_ID_CLAIM": "id",      # token payload carries { id, role } like the Node version
    "SIGNING_KEY": os.getenv("JWT_SECRET", SECRET_KEY),
}

# ---- CORS (= Express cors()) ----
CORS_ALLOW_CREDENTIALS = True
# Allowed origins = FRONTEND_URL (the Vercel frontend, comma-separated) + dev.
_frontend = os.getenv("FRONTEND_URL", "http://localhost:5173")
CORS_ALLOWED_ORIGINS = [o.strip() for o in _frontend.split(",") if o.strip()]
for _dev in ("http://localhost:5173", "http://127.0.0.1:5173"):
    if _dev not in CORS_ALLOWED_ORIGINS:
        CORS_ALLOWED_ORIGINS.append(_dev)
# Any Vercel deploy (preview + prod) for the frontend.
CORS_ALLOWED_ORIGIN_REGEXES = [r"^https://.*\.vercel\.app$"]
if env_bool("CORS_ALLOW_ALL", False):
    CORS_ALLOW_ALL_ORIGINS = True

# ---- CSRF (Django admin / any session-cookie POSTs over HTTPS on Vercel) ----
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]
if "https://*.vercel.app" not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append("https://*.vercel.app")

# ---- Razorpay ----
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
