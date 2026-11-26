from django.urls import path
from .views import (
    create_shipment_view,
    shipment_status_view,
    shiprocket_connection_status_view,
    tracking_updates_webhook,
)

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
    # NOTE: Shiprocket rejects webhook URLs containing "shiprocket", "sr", "kr",
    # so the webhook path is deliberately neutral. Both forms are routed because
    # a POST can't survive an APPEND_SLASH redirect.
    path("tracking-updates/", tracking_updates_webhook, name="tracking_updates_webhook"),
    path("tracking-updates", tracking_updates_webhook),
    path("orders/<str:order_id>/create-shipment/", create_shipment_view, name="create_shipment"),
    path("orders/<str:order_id>/shipment/", shipment_status_view, name="shipment_status"),
]
