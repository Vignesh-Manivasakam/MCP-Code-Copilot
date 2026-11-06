"""
Diagnostics Poly-Tool for Code Copilot MCP Server.
==================================================
Provides syntax checking, session telemetry summaries, and token quota inspection.
Consolidates file validation, session performance diagnostics, and token tracking.
"""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
import subprocess
import time
from typing import Any, Dict, List, Optional

from config import Config
from copilot.core.security import validate_path
from copilot.services.ast_service import read_file_source
from copilot.services.telemetry import (
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_SYNTAX_ERROR,
    SECURITY_VIOLATION,
    estimate_tokens,
    get_session_diagnostics,
    get_token_tracker,
    get_token_usage,
    normalize_error_code,
    record_trace,
)

logger = logging.getLogger(__name__)


def diagnostics(
    action: str = "file",
    file_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Perform self-diagnostics: syntax validation, session telemetry summary, or token budget usage.

    USE THIS WHEN:
    - action='file': Verify syntax of a modified file (Python, JSON, JS/TS) without executing it
    - action='session': Review tool call statistics, error rates, and latency across current session
    - action='tokens': Check current token usage against the 1,000,000 token session budget

    Args:
        action: Diagnostic mode: 'file', 'session', or 'tokens'. Default is 'file'.
        file_path: Target file path (required when action='file').

    Returns:
        Structured diagnostic dictionary with status, errors/metrics, and telemetry info.
    """
    start_time = time.perf_counter()
    action_clean = (action or "file").strip().lower()

    try:
        if action_clean == "file":
            res = _diagnose_file(file_path)
        elif action_clean == "session":
            res = _diagnose_session()
        elif action_clean == "tokens":
            res = _diagnose_tokens()
        else:
            res = {
                "success": False,
                "status": "error",
                "error": f"ERR_INVALID_ACTION: Unsupported action '{action}'. Valid actions are 'file', 'session', 'tokens'.",
            }

        # Check session token warning (>80%)
        tracker = get_token_tracker()
        if tracker.is_warning_active():
            res["warning_header"] = True
            res["token_warning"] = "Warning: Session token usage exceeds 80% quota."

        duration_ms = (time.perf_counter() - start_time) * 1000
        status_str = "success" if res.get("success", False) else "error"
        err_code = None
        if not res.get("success", False):
            err_code = normalize_error_code(res.get("error"))
        elif res.get("status") == "error":
            err_code = ERR_SYNTAX_ERROR

        est_tokens = estimate_tokens(json.dumps(res, default=str))

        record_trace(
            tool_name="diagnostics",
            duration_ms=duration_ms,
            status=status_str,
            error_code=err_code,
            estimated_tokens=est_tokens,
            metadata={"action": action_clean, "file_path": str(file_path or "")},
        )
        return res

    except Exception as exc:
        logger.exception("Unexpected exception in diagnostics: %s", exc)
        duration_ms = (time.perf_counter() - start_time) * 1000
        record_trace(
            tool_name="diagnostics",
            duration_ms=duration_ms,
            status="error",
            error_code=ERR_INTERNAL,
            estimated_tokens=0,
            metadata={"action": action_clean, "file_path": str(file_path or ""), "exception": str(exc)},
        )
        return {
            "success": False,
            "status": "error",
            "error": f"ERR_INTERNAL: Unexpected diagnostics failure: {exc}",
        }


def _diagnose_file(file_path: Optional[str]) -> Dict[str, Any]:
    """Perform syntax checking and static validation on a single file."""
    if not file_path or not str(file_path).strip():
        return {
            "success": False,
            "status": "error",
            "error": "ERR_INVALID_INPUT: 'file_path' is required when action='file'.",
        }

    is_valid, resolved, err_msg = validate_path(file_path, check_sensitive=True)
    if not is_valid or resolved is None:
        return {
            "success": False,
            "file_path": str(file_path),
            "status": "error",
            "error": f"ERR_SECURITY_VIOLATION: {err_msg}",
        }

    if not resolved.exists():
        return {
            "success": False,
            "file_path": str(file_path),
            "status": "error",
            "error": f"ERR_NOT_FOUND: File not found: {resolved}",
        }

    if not resolved.is_file():
        return {
            "success": False,
            "file_path": str(file_path),
            "status": "error",
            "error": f"ERR_NOT_FOUND: Target path is not a file: {resolved}",
        }

    content, total_lines = read_file_source(resolved)
    ext = resolved.suffix.lower()
    diag_list: List[Dict[str, Any]] = []

    if ext in (".py", ".pyw", ".pyi"):
        # Python syntax validation using ast.parse and compile()
        try:
            ast.parse(content, filename=str(resolved))
        except SyntaxError as e:
            diag_list.append({
                "severity": "error",
                "line": e.lineno,
                "column": e.offset,
                "message": str(e.msg),
                "text": (e.text or "").strip(),
                "code": ERR_SYNTAX_ERROR,
            })

        try:
            compile(content, str(resolved), "exec")
        except SyntaxError as e:
            if not diag_list:
                diag_list.append({
                    "severity": "error",
                    "line": e.lineno,
                    "column": e.offset,
                    "message": str(e.msg),
                    "text": (e.text or "").strip(),
                    "code": ERR_SYNTAX_ERROR,
                })

    elif ext == ".json":
        # JSON syntax validation
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            diag_list.append({
                "severity": "error",
                "line": e.lineno,
                "column": e.colno,
                "message": str(e.msg),
                "text": "",
                "code": ERR_SYNTAX_ERROR,
            })

    elif ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        # JavaScript / TypeScript syntax validation via node --check if available
        try:
            res = subprocess.run(
                ["node", "--check", str(resolved)],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=Config.PROJECT_ROOT,
            )
            if res.returncode != 0:
                diag_list.append({
                    "severity": "error",
                    "line": None,
                    "column": None,
                    "message": res.stderr.strip(),
                    "text": "",
                    "code": ERR_SYNTAX_ERROR,
                })
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            diag_list.append({
                "severity": "warning",
                "line": None,
                "column": None,
                "message": "Node.js not available for JS/TS runtime syntax validation.",
                "text": "",
                "code": "WARN_LINTER_UNAVAILABLE",
            })

    else:
        return {
            "success": True,
            "action": "file",
            "file_path": str(file_path),
            "language": ext,
            "status": "unsupported",
            "message": f"No syntax checker available for {ext} files.",
            "diagnostics": [],
            "total_errors": 0,
            "total_lines": total_lines,
        }

    status_result = "error" if any(d["severity"] == "error" for d in diag_list) else "ok"
    error_count = sum(1 for d in diag_list if d["severity"] == "error")

    return {
        "success": True,
        "action": "file",
        "file_path": str(file_path),
        "language": ext,
        "status": status_result,
        "syntax_valid": (status_result == "ok"),
        "diagnostics": diag_list,
        "total_errors": error_count,
        "total_lines": total_lines,
    }


def _diagnose_session() -> Dict[str, Any]:
    """Retrieve aggregate session performance metrics from telemetry service."""
    session_metrics = get_session_diagnostics()
    return {
        "success": True,
        "action": "session",
        "status": "ok",
        "session": session_metrics,
        "total_tool_calls": session_metrics.get("total_tool_calls", 0),
        "successful_calls": session_metrics.get("successful_calls", 0),
        "failed_calls": session_metrics.get("failed_calls", 0),
        "error_rate": session_metrics.get("error_rate", 0.0),
        "token_usage": session_metrics.get("token_usage", {}),
    }


def _diagnose_tokens() -> Dict[str, Any]:
    """Retrieve live token usage metrics against the 1M session quota."""
    token_metrics = get_token_usage()
    return {
        "success": True,
        "action": "tokens",
        "status": "ok",
        "token_usage": token_metrics,
        "warning_active": token_metrics.get("warning_active", False),
    }
