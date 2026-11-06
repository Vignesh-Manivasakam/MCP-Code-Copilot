"""
Consolidated Run Command Poly-Tool for Code Copilot MCP Server.
==============================================================
Sandboxed subprocess execution enforcing binary whitelisting, Windows .cmd
resolution, credential scrubbing, process tree timeout killing, and output compaction.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from config import Config
from copilot.core.sandbox import (
    run_sandboxed_command,
    ERR_SECURITY_VIOLATION,
    ERR_COMMAND_TIMEOUT,
    ERR_NOT_FOUND,
    ERR_PATH_TRAVERSAL,
    ERR_COMMAND_FAILED,
)
from copilot.services.telemetry import (
    estimate_tokens,
    get_token_tracker,
    record_trace,
)

logger = logging.getLogger(__name__)


def run_command(
    command: str,
    arguments: Optional[List[str]] = None,
    timeout_seconds: int = 30,
    working_directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Sandboxed execution of whitelisted developer binaries with sanitized environment.

    Parameters:
        command: Binary to execute (e.g., 'pytest', 'python', 'npm', 'git').
        arguments: Command-line arguments passed to the executable.
        timeout_seconds: Process execution timeout in seconds (default: 30, max: 300).
        working_directory: Optional directory path relative to project root.

    Returns:
        {
            "exit_code": int,
            "stdout": str,
            "stderr": str,
            "truncated": bool,
            "success": bool,
            "error_code": str | None,
            "error": str | None,
            "execution_time_ms": float,
            "command": str,
        }
    """
    args = list(arguments) if arguments is not None else []
    timeout = timeout_seconds if timeout_seconds is not None else getattr(Config, "DEFAULT_TIMEOUT_SECONDS", 30)

    res = run_sandboxed_command(
        command=command,
        arguments=args,
        timeout_seconds=timeout,
        cwd=working_directory,
    )

    # Estimate output tokens and record trace
    combined_output = (res.get("stdout") or "") + (res.get("stderr") or "")
    tokens = estimate_tokens(combined_output)

    record_trace(
        tool_name="run_command",
        duration_ms=res.get("execution_time_ms", 0.0),
        status="success" if res.get("success") else "error",
        error_code=res.get("error_code"),
        estimated_tokens=tokens,
        metadata={"command": command, "arguments": args, "exit_code": res.get("exit_code")},
    )

    # Inject token quota warning if active
    tracker = get_token_tracker()
    if tracker.is_warning_active():
        res["warning"] = tracker.get_warning_header()

    return res


def register_command_tools(mcp: Any) -> List[str]:
    """Register run_command tool on FastMCP instance."""
    mcp.tool()(run_command)
    return ["run_command"]


__all__ = [
    "run_command",
    "register_command_tools",
    "ERR_SECURITY_VIOLATION",
    "ERR_COMMAND_TIMEOUT",
    "ERR_NOT_FOUND",
    "ERR_PATH_TRAVERSAL",
    "ERR_COMMAND_FAILED",
]
