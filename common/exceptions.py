"""
Make DRF errors look like the Express errorHandler output:
    { "success": false, "message": "..." }
so the frontend's error handling keeps working unchanged.
"""

from rest_framework.views import exception_handler
from rest_framework.response import Response


def _extract_message(data, fallback="Something went wrong."):
    if isinstance(data, dict):
        if "message" in data:
            return data["message"]
        if "detail" in data:
            return str(data["detail"])
        # validation errors -> first field's first error
        for value in data.values():
            if isinstance(value, (list, tuple)) and value:
                return str(value[0])
            return str(value)
    if isinstance(data, (list, tuple)) and data:
        return str(data[0])
    return fallback


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        response.data = {
            "success": False,
            "message": _extract_message(response.data),
        }
    return response
