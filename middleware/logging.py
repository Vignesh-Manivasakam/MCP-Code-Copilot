"""
Request / response logging middleware for Code Copilot MCP Server.
"""
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Simple Starlette middleware that logs each incoming request and the status
    code / latency of the outgoing response.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        logger.info("→ %s %s", request.method, request.url.path)

        response: Response = await call_next(request)

        elapsed_ms = (time.perf_counter() - start) * 1_000
        logger.info(
            "← %s %s  %d  (%.1f ms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response
