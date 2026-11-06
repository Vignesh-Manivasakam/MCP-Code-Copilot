#!/usr/bin/env python3
"""
Code Copilot MCP Server – Refactored ASGI Server
================================================
Starlette ASGI application mounting FastMCP dual transports (Streamable-HTTP /mcp
and Server-Sent Events /sse) with Private Network Access (PNA) CORS support and
OriginGuard bypass.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
import time
from typing import Optional

from fastmcp import FastMCP
import fastmcp.settings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from config import Config
from copilot.prompts import register_copilot_prompts
from copilot.tools import register_copilot_tools

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("copilot.server")

# ---------------------------------------------------------------------------
# FastMCP Singleton & Component Registration
# ---------------------------------------------------------------------------
mcp = FastMCP(Config.SERVER_NAME)
_tools_registered = False


def register_server_tools() -> None:
    """Idempotently register all consolidated poly-tools, prompts, and resources onto FastMCP."""
    global _tools_registered
    if not _tools_registered:
        register_copilot_tools(mcp)
        register_copilot_prompts(mcp, include_resources=True)
        _tools_registered = True
        logger.info("Registered consolidated poly-tools, prompts, and resources with FastMCP")


def initialize_server(project_root: Optional[str | Path] = None) -> None:
    """Initialize server configuration and register components (backward-compatibility helper)."""
    if project_root:
        Config.set_project_root(project_root)
    register_server_tools()


# ---------------------------------------------------------------------------
# Inlined Starlette Middleware
# ---------------------------------------------------------------------------
class LoggingMiddleware(BaseHTTPMiddleware):
    """Logs incoming HTTP requests and response latency in milliseconds."""

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        logger.info("→ %s %s", request.method, request.url.path)
        try:
            response: Response = await call_next(request)
            elapsed_ms = (time.perf_counter() - start) * 1_000
            logger.info(
                "← %s %s %d (%.1f ms)",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
            return response
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1_000
            logger.exception("← %s %s ERROR (%.1f ms)", request.method, request.url.path, elapsed_ms)
            raise


def _build_middleware() -> list[Middleware]:
    """Compose Starlette middleware stack with CORS PNA and request logging."""
    stack = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            allow_private_network=Config.CORS_ALLOW_PRIVATE_NETWORK,
            expose_headers=["mcp-session-id", "Mcp-Session-Id", "*"],
        )
    ]
    if getattr(Config, "ENABLE_REQUEST_LOGGING", True):
        stack.append(Middleware(LoggingMiddleware))
    return stack


# ---------------------------------------------------------------------------
# Health Check Endpoint
# ---------------------------------------------------------------------------
async def health_check(request: Request) -> JSONResponse:
    """Liveness probe returning standardized server status JSON."""
    return JSONResponse({
        "status": "ok",
        "server": "FastMCP Code Copilot",
        "version": "0.1.0",
    })


# ---------------------------------------------------------------------------
# ASGI Application Factory
# ---------------------------------------------------------------------------
def create_app(project_root: Optional[str | Path] = None) -> Starlette:
    """
    Construct the Starlette ASGI application with dual-transport FastMCP mounting.

    Mounts:
      - GET /health: Standardized JSON liveness status
      - GET /: Standardized JSON liveness status
      - POST/GET/DELETE /mcp: Streamable-HTTP transport
      - GET /sse: Server-Sent Events stream
      - POST /messages: SSE client message receiver
    """
    if project_root:
        Config.set_project_root(project_root)
    elif Config.PROJECT_ROOT is None:
        Config.PROJECT_ROOT = Path.cwd().resolve()
    Config.ensure_directories()

    # Configure FastMCP origin protection settings
    fastmcp.settings.http_host_origin_protection = Config.HOST_ORIGIN_PROTECTION

    # Register tools, prompts, and resources
    register_server_tools()

    # Generate transport sub-apps with OriginGuard bypassed
    http_sub = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
        host_origin_protection=Config.HOST_ORIGIN_PROTECTION,
    )
    sse_sub = mcp.http_app(
        path="/sse",
        transport="sse",
        host_origin_protection=Config.HOST_ORIGIN_PROTECTION,
    )

    # Combine top-level routes
    routes = [
        Route("/health", endpoint=health_check, methods=["GET"]),
        Route("/", endpoint=health_check, methods=["GET"]),
        *http_sub.routes,
        *sse_sub.routes,
    ]

    return Starlette(
        routes=routes,
        middleware=_build_middleware(),
        lifespan=http_sub.lifespan,
    )


# Module-level ASGI app for standard ASGI runners (e.g. uvicorn server:app)
app = create_app()


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Code Copilot MCP Server")
    parser.add_argument("project_root", nargs="?", default=None, help="Path to project root directory")
    parser.add_argument("--port", type=int, default=Config.DEFAULT_PORT, help="Port to listen on")
    parser.add_argument("--host", default=Config.DEFAULT_HOST, help="Host to bind to")
    args = parser.parse_args()

    application = create_app(args.project_root)
    print("=" * 60)
    print(f"🚀 Code Copilot MCP Server v{Config.SERVER_VERSION}")
    print(f"  Project : {Config.PROJECT_ROOT}")
    print(f"  MCP     : http://{args.host}:{args.port}/mcp")
    print(f"  SSE     : http://{args.host}:{args.port}/sse")
    print(f"  Health  : http://{args.host}:{args.port}/health")
    print("=" * 60)

    uvicorn.run(application, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
