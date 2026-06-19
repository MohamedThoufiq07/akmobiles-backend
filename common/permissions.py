"""
Reusable DRF permission classes and a pagination helper.

The Node backend returned paginated lists as:
    { success, products|orders|..., page, pages, total }
DRF's built-in paginator wraps results differently, so we paginate manually with
`paginate_queryset` to reproduce that exact envelope.
"""

from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    """Mirror of the Node `admin` middleware: user must be authenticated + role == 'admin'."""

    message = "Access denied. Admin privileges required."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role == "admin"
        )


def paginate_queryset(queryset, page=1, limit=12):
    """Return (items_page, page_num, total_pages, total_count) like the Node controllers."""
    try:
        page_num = int(page) or 1
    except (TypeError, ValueError):
        page_num = 1
    try:
        limit_num = int(limit) or 12
    except (TypeError, ValueError):
        limit_num = 12

    total = queryset.count()
    start = (page_num - 1) * limit_num
    end = start + limit_num
    items = queryset[start:end]
    pages = (total + limit_num - 1) // limit_num  # ceil
    return items, page_num, pages, total
