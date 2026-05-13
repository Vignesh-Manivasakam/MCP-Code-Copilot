"""
Standardized error handling for Code Copilot MCP Server
"""
import functools
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ErrorCode:
    """Centralised error code constants."""

    # Security errors
    SECURITY_VIOLATION = "SECURITY_VIOLATION"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    PERMISSION_DENIED = "PERMISSION_DENIED"

    # File errors
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_EXISTS = "FILE_EXISTS"
    DIRECTORY_NOT_FOUND = "DIRECTORY_NOT_FOUND"

    # Operation errors
    READ_ERROR = "READ_ERROR"
    WRITE_ERROR = "WRITE_ERROR"
    ENCODING_ERROR = "ENCODING_ERROR"
    BINARY_FILE_ERROR = "BINARY_FILE_ERROR"

    # Validation errors
    INVALID_INPUT = "INVALID_INPUT"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
    INVALID_PATTERN = "INVALID_PATTERN"

    # System errors
    INTERNAL_ERROR = "INTERNAL_ERROR"
    TIMEOUT_ERROR = "TIMEOUT_ERROR"


def create_error_response(
    error_code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create a standardised error response dictionary.

    Args:
        error_code: One of the ErrorCode constants.
        message:    Human-readable description of the problem.
        details:    Optional extra key/value pairs merged into the response.
    """
    response: Dict[str, Any] = {
        "success": False,
        "error": message,
        "error_code": error_code,
    }
    if details:
        response.update(details)
    return response


def create_success_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a standardised success response dictionary.

    Args:
        data: Response payload.
    """
    return {"success": True, **data}


def handle_tool_errors(func):
    """
    Decorator that catches common exceptions and converts them to structured
    error responses so individual tool functions do not need try/except blocks
    for predictable error types.

    Usage::

        @handle_tool_errors
        def my_tool(arg: str) -> dict:
            ...
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except FileNotFoundError as exc:
            return create_error_response(ErrorCode.FILE_NOT_FOUND, str(exc))
        except PermissionError as exc:
            return create_error_response(ErrorCode.PERMISSION_DENIED, str(exc))
        except ValueError as exc:
            return create_error_response(ErrorCode.INVALID_INPUT, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Unexpected error in %s: %s", func.__name__, exc, exc_info=True
            )
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"An unexpected error occurred: {exc}",
            )

    return wrapper
