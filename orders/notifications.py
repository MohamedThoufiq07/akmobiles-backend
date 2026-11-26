"""
Customer e-mail notifications for order / shipping milestones.

Sending never raises: a mail problem must not break payment, checkout or the
courier webhook. Each milestone mail is sent at most once per status change,
because callers only invoke this when the order status actually changed.
"""

import logging
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)

# status -> (subject suffix, headline, body line)
_EVENTS = {
    "Placed": (
        "Order confirmed",
        "Thank you! Your order is confirmed.",
        "We have received your order and will pack it shortly.",
    ),
    "Shipped": (
        "Your order has shipped",
        "Your order is on its way!",
        "It has been handed over to the courier.",
    ),
    "OutForDelivery": (
        "Out for delivery",
        "Your order is out for delivery.",
        "The courier will reach you today. Please keep your phone handy.",
    ),
    "Delivered": (
        "Order delivered",
        "Your order has been delivered.",
        "We hope you love it. Thank you for shopping with us!",
    ),
    "Cancelled": (
        "Order cancelled",
        "Your order has been cancelled.",
        "If you already paid, the refund will reach your account as per your bank's timeline.",
    ),
}


def _money(value):
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _recipient(order):
    addr = order.shipping_address or {}
    return (getattr(order.user, "email", "") or addr.get("email") or "").strip()


def build_email(order, event):
    """Return (subject, text, html) for a milestone, or None if unknown."""
    if event not in _EVENTS:
        return None
    subject_tail, headline, line = _EVENTS[event]
    site = settings.SITE_NAME
    order_ref = str(order._id)[-8:].upper()
    name = (order.shipping_address or {}).get("name") or getattr(order.user, "name", "") or "Customer"
    order_url = f"{settings.SITE_URL}/orders/{order._id}"

    tracking_lines = []
    if order.awb_code and event in ("Shipped", "OutForDelivery", "Delivered"):
        tracking_lines.append(f"Courier: {order.courier_name or 'Courier partner'}")
        tracking_lines.append(f"Tracking number (AWB): {order.awb_code}")
        if order.tracking_url:
            tracking_lines.append(f"Track your parcel: {order.tracking_url}")

    items = "\n".join(
        f"  - {i.get('name', 'Item')} x {i.get('quantity', 1)}" for i in (order.order_items or [])
    )

    text = (
        f"Hi {name},\n\n{headline}\n{line}\n\n"
        f"Order: #{order_ref}\nTotal: {_money(order.total_price)}\n"
        + (f"Items:\n{items}\n" if items else "")
        + ("\n" + "\n".join(tracking_lines) + "\n" if tracking_lines else "")
        + f"\nView your order: {order_url}\n\n— {site}"
    )

    track_html = ""
    if tracking_lines:
        link = (
            f'<p><a href="{escape(order.tracking_url)}">Track your parcel</a></p>'
            if order.tracking_url
            else ""
        )
        track_html = (
            f"<p><b>Courier:</b> {escape(order.courier_name or 'Courier partner')}<br>"
            f"<b>Tracking number (AWB):</b> {escape(order.awb_code)}</p>{link}"
        )
    items_html = "".join(
        f"<li>{escape(str(i.get('name', 'Item')))} × {escape(str(i.get('quantity', 1)))}</li>"
        for i in (order.order_items or [])
    )
    html = (
        f"<div style='font-family:Arial,sans-serif;max-width:560px'>"
        f"<h2>{escape(headline)}</h2><p>Hi {escape(name)},</p><p>{escape(line)}</p>"
        f"<p><b>Order:</b> #{escape(order_ref)}<br><b>Total:</b> {escape(_money(order.total_price))}</p>"
        f"{('<ul>' + items_html + '</ul>') if items_html else ''}{track_html}"
        f"<p><a href='{escape(order_url)}'>View your order</a></p>"
        f"<p style='color:#64748b'>— {escape(site)}</p></div>"
    )
    return f"{site}: {subject_tail} (#{order_ref})", text, html


def notify_order_event(order, event):
    """Send the milestone e-mail. Returns True if a message was handed to the mail backend."""
    try:
        built = build_email(order, event)
        to = _recipient(order)
        if not built or not to:
            return False
        subject, text, html = built
        msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [to])
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Failed to send %s e-mail for order %s", event, getattr(order, "_id", "?"))
        return False
