from django.urls import path
from .views import shiprocket_connection_status_view

urlpatterns = [
    path(
        "shiprocket/connection-status/",
        shiprocket_connection_status_view,
        name="shiprocket_connection_status",
    ),
    path(
        "shiprocket/connection-status",
        shiprocket_connection_status_view,
    ),
]
