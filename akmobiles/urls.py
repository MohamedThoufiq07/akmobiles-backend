from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path, include
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"status": "OK", "message": "AK Mobiles API is running"})


# Mounted WITHOUT trailing slashes to mirror the Express routes exactly.
urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api/health", health),
    path("api/auth", include("accounts.urls")),
    path("api/products", include("products.urls")),
    path("api/orders", include("orders.urls")),
    path("api/payment", include("payments.urls")),
    path("api", include("core.urls")),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
