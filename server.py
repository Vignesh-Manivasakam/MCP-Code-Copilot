#!/usr/bin/env python3
"""
Code Copilot MCP Server – Phase 1
==================================
A secure, single-folder code-assistance MCP server built on FastMCP v2.

Features
--------
* 10 Tier-1 file-operation tools
* Path-traversal protection (hard sandbox at project root)
* Automatic encoding detection
* CORS + request-logging middleware
* Health check and project-info HTTP routes

Usage
-----
    python server.py /path/to/your/project [--port 8000] [--host 127.0.0.1]
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from config import Config
from tools.file_operations import register_file_tools

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP(Config.SERVER_NAME)

# ---------------------------------------------------------------------------
# Custom HTTP routes
# ---------------------------------------------------------------------------


@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
def health_check(request: Request) -> PlainTextResponse:
    """Liveness / readiness probe."""
    if Config.PROJECT_ROOT is None:
        return PlainTextResponse(
            "⚠️  Code Copilot MCP Server – no project configured\n",
            status_code=503,
        )

    try:
        file_count = sum(1 for p in Config.PROJECT_ROOT.rglob("*") if p.is_file())
    except Exception:  # noqa: BLE001
        file_count = -1

    body = (
        f"✅ Code Copilot MCP Server v{Config.SERVER_VERSION}\n\n"
        f"Status  : Running\n"
        f"Project : {Config.PROJECT_ROOT.name}\n"
        f"Path    : {Config.PROJECT_ROOT}\n"
        f"Files   : {file_count}\n"
        f"Tools   : 10  (Tier 1 – file operations)\n\n"
        f"MCP endpoint : /mcp\n"
    )
    return PlainTextResponse(body)


@mcp.custom_route("/project-info", methods=["GET"])
def project_info(request: Request) -> JSONResponse:
    """Return a JSON summary of the current project."""
    if Config.PROJECT_ROOT is None:
        return JSONResponse({"error": "No project configured"}, status_code=503)

    root = Config.PROJECT_ROOT
    all_entries = list(root.rglob("*"))

    extension_counts: dict = {}
    total_size = 0

    for entry in all_entries:
        if entry.is_file():
            ext = entry.suffix or ".no_extension"
            extension_counts[ext] = extension_counts.get(ext, 0) + 1
            try:
                total_size += entry.stat().st_size
            except OSError:
                pass

    stats = {
        "project_name": root.name,
        "project_path": str(root),
        "total_files": sum(1 for e in all_entries if e.is_file()),
        "total_directories": sum(1 for e in all_entries if e.is_dir()),
        "total_size_bytes": total_size,
        "extensions": extension_counts,
    }
    return JSONResponse(stats)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def _build_middleware() -> list:
    """Compose the middleware stack."""
    stack = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["mcp-session-id", "Mcp-Session-Id", "*"],
        )
    ]

    if Config.ENABLE_REQUEST_LOGGING:
        from middleware.logging import LoggingMiddleware  # noqa: PLC0415

        stack.append(Middleware(LoggingMiddleware))

    return stack


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def initialize_server(project_root: Optional[str] = None) -> None:
    """Register all tools and optionally configure the project root.

    When *project_root* is supplied (CLI argument or env-var), the sandbox is
    set immediately.  When it is omitted the server starts without a project
    configured; the client can then call the ``set_project_root`` MCP tool at
    any time to point the server at a local folder or cloned repository.
    """
    logger.info("Initialising Code Copilot MCP Server…")

    if project_root:
        try:
            Config.set_project_root(project_root)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to configure project root: %s", exc)
            sys.exit(1)
        logger.info("✅ Project root: %s", Config.PROJECT_ROOT)
    else:
        logger.info(
            "ℹ️  No project root supplied – call the 'set_project_root' tool "
            "(or restart with a path argument) to configure one."
        )

    logger.info("Registering tools…")
    registered = register_file_tools(mcp)
    logger.info("✅ Registered tools: %s", registered)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import uvicorn  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        description="Code Copilot MCP Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        default=None,
        help=(
            "Absolute or relative path to the project folder to expose. "
            "If omitted the server starts without a project configured – "
            "use the 'set_project_root' MCP tool to set one at runtime."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=Config.DEFAULT_PORT,
        help="TCP port to listen on",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Network interface to bind (use 0.0.0.0 for all interfaces)",
    )

    args = parser.parse_args()

    initialize_server(args.project_root)

    http_app = mcp.http_app(path="/mcp", middleware=_build_middleware())

    separator = "=" * 60
    print(separator)
    print(f"🚀 Code Copilot MCP Server  v{Config.SERVER_VERSION}")
    print(separator)
    if Config.PROJECT_ROOT:
        print(f"  Project : {Config.PROJECT_ROOT}")
    else:
        print("  Project : ⚠️  NOT CONFIGURED")
        print("            → Ask the assistant to call 'set_project_root'")
        print("              or restart with:  python server.py <folder>")
    print(f"  MCP     : http://{args.host}:{args.port}/mcp")
    print(f"  Health  : http://{args.host}:{args.port}/health")
    print(separator)
    if not Config.PROJECT_ROOT:
        print("ℹ️  Tell the assistant which project folder or repo you want to work on.\n")
    else:
        print("Ready – press Ctrl+C to stop.\n")

    try:
        uvicorn.run(http_app, host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\n✅ Server stopped.")
