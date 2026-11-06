"""
Inspect AST Poly-Tool for Code Copilot MCP Server.
=================================================
Extracts structural code outlines (classes, methods, signatures, line numbers)
in ~60-200 tokens. Backed by `copilot.services.ast_service.generate_ast_outline`.
"""
from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any, Dict, Optional

from config import Config
from copilot.services.ast_service import generate_ast_outline
from copilot.services.telemetry import (
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    SECURITY_VIOLATION,
    estimate_tokens,
    get_token_tracker,
    normalize_error_code,
    record_trace,
)

logger = logging.getLogger(__name__)


def inspect_ast(file_path: str) -> Dict[str, Any]:
    """Extract structural code outlines (classes, methods, signatures, line numbers) in ~60-200 tokens.

    USE THIS WHEN:
    - You need to understand file structure before editing
    - You want to discover class, method, or function definitions and line numbers
    - You need to conserve tokens compared to reading the entire file

    Args:
        file_path: Relative or inside-root absolute path to the target source file.

    Returns:
        Structured outline dictionary:
        {
            "success": bool,
            "file_path": str,
            "language": str,
            "outline": list[dict],
            "outline_text": str,
            "token_count": int,
            "total_lines": int,
            "total_symbols": int,
            "syntax_error": bool,
            "fallback": bool,
            "truncated": bool,
            "error": str | None,
            "warning_header": bool (optional)
        }
    """
    start_time = time.perf_counter()
    status = "success"
    error_code: Optional[str] = None
    estimated_tokens = 0

    try:
        if not file_path or not isinstance(file_path, str) or not file_path.strip():
            error_code = "ERR_INVALID_INPUT"
            duration_ms = (time.perf_counter() - start_time) * 1000
            res = {
                "success": False,
                "file_path": str(file_path or ""),
                "language": "unknown",
                "outline": [],
                "outline_text": "",
                "token_count": 0,
                "total_lines": 0,
                "total_symbols": 0,
                "syntax_error": False,
                "fallback": True,
                "truncated": False,
                "error": "ERR_INVALID_INPUT: 'file_path' must be a non-empty string.",
            }
            record_trace(
                tool_name="inspect_ast",
                duration_ms=duration_ms,
                status="error",
                error_code=error_code,
                estimated_tokens=0,
                metadata={"file_path": str(file_path)},
            )
            return res

        # Delegate to ast_service.generate_ast_outline
        result = generate_ast_outline(file_path)

        if not result.get("success", False):
            status = "error"
            raw_err = result.get("error") or ""
            if "SECURITY_VIOLATION" in raw_err or "traversal" in raw_err.lower() or "sensitive" in raw_err.lower():
                error_code = SECURITY_VIOLATION
            elif "ERR_NOT_FOUND" in raw_err or "not found" in raw_err.lower():
                error_code = ERR_NOT_FOUND
            else:
                error_code = ERR_INTERNAL
        else:
            status = "success"
            error_code = None

        estimated_tokens = result.get("token_count", 0)
        if estimated_tokens == 0 and result.get("outline_text"):
            estimated_tokens = estimate_tokens(result.get("outline_text", ""))

        # Check token quota warning (>80%)
        tracker = get_token_tracker()
        if tracker.is_warning_active():
            result["warning_header"] = True
            result["token_warning"] = "Warning: Session token usage exceeds 80% quota."

        duration_ms = (time.perf_counter() - start_time) * 1000
        record_trace(
            tool_name="inspect_ast",
            duration_ms=duration_ms,
            status=status,
            error_code=error_code,
            estimated_tokens=estimated_tokens,
            metadata={
                "file_path": str(file_path),
                "language": result.get("language"),
                "total_symbols": result.get("total_symbols", 0),
                "fallback": result.get("fallback", False),
            },
        )
        return result

    except Exception as exc:
        logger.exception("Unexpected exception in inspect_ast: %s", exc)
        duration_ms = (time.perf_counter() - start_time) * 1000
        error_code = ERR_INTERNAL
        record_trace(
            tool_name="inspect_ast",
            duration_ms=duration_ms,
            status="error",
            error_code=error_code,
            estimated_tokens=0,
            metadata={"file_path": str(file_path), "exception": str(exc)},
        )
        return {
            "success": False,
            "file_path": str(file_path),
            "language": "unknown",
            "outline": [],
            "outline_text": "",
            "token_count": 0,
            "total_lines": 0,
            "total_symbols": 0,
            "syntax_error": False,
            "fallback": True,
            "truncated": False,
            "error": f"ERR_INTERNAL: Unexpected error: {exc}",
        }
