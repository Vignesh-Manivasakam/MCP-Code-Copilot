"""
Consolidated Code Search Poly-Tool for Code Copilot MCP Server.
==============================================================
Provides high-speed search across grep (regex with context), symbol outlines,
SQLite FTS5 fulltext search with highlighted snippets, and file-tree globbing.
Instruments telemetry tracing and token budget quota warnings.
"""
from __future__ import annotations

from enum import Enum
import json
import logging
import time
from typing import Any, Dict, List, Optional

from config import Config
from copilot.services.search_service import (
    SearchService,
    ERR_INVALID_REGEX,
    ERR_PATH_TRAVERSAL,
    ERR_NOT_FOUND,
)
from copilot.services.telemetry import (
    estimate_tokens,
    get_token_tracker,
    record_trace,
)

logger = logging.getLogger(__name__)

ERR_INVALID_MODE = "ERR_INVALID_MODE"


class SearchMode(str, Enum):
    """Supported search modes matching authoritative specification."""
    GREP = "grep"           # Regex content search with context lines
    SYMBOL = "symbol"       # Function/class definitions across languages
    FULLTEXT = "fulltext"   # SQLite FTS5 indexed keyword search
    FILES = "files"         # Glob/pattern file tree navigation


def code_search(
    mode: str,
    query: str,
    path: Optional[str] = None,
    file_pattern: Optional[str] = None,
    max_results: int = 25,
) -> Dict[str, Any]:
    """High-speed code search with automatic ignore-pruning (.git, .venv, node_modules).

    Parameters:
        mode: Search mode ('grep', 'symbol', 'fulltext', 'files').
        query: Search string, regex, symbol name, or glob pattern.
        path: Optional relative or inside-root directory path to constrain search.
        file_pattern: Optional glob to filter filenames (e.g. '*.py').
        max_results: Maximum results to return (default: 25).

    Returns:
        Structured search results dictionary with matches, truncation flags, and telemetry.
    """
    start_time = time.perf_counter()
    clean_mode = str(mode).strip().lower()

    valid_modes = {
        SearchMode.GREP.value,
        SearchMode.SYMBOL.value,
        SearchMode.FULLTEXT.value,
        SearchMode.FILES.value,
    }

    if clean_mode not in valid_modes:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        res: Dict[str, Any] = {
            "error": ERR_INVALID_MODE,
            "error_code": ERR_INVALID_MODE,
            "message": f"Unsupported search mode '{mode}'. Supported modes: {', '.join(sorted(valid_modes))}",
            "results": [],
            "total_matches": 0,
            "truncated": False,
            "success": False,
        }
        record_trace(
            tool_name="code_search",
            duration_ms=elapsed_ms,
            status="error",
            error_code=ERR_INVALID_MODE,
            estimated_tokens=estimate_tokens(json.dumps(res)),
            metadata={"mode": mode, "query": query},
        )
        return res

    service = SearchService()

    try:
        if clean_mode == SearchMode.GREP.value:
            res = service.grep_search(
                query=query,
                path=path,
                file_pattern=file_pattern,
                max_results=max_results,
                context_lines=getattr(Config, "GREP_CONTEXT_LINES", 2),
            )
        elif clean_mode == SearchMode.SYMBOL.value:
            res = service.search_symbols(
                query=query,
                path=path,
                file_pattern=file_pattern,
                max_results=max_results,
            )
        elif clean_mode == SearchMode.FULLTEXT.value:
            res = service.fts_search(
                query=query,
                max_results=max_results,
            )
        elif clean_mode == SearchMode.FILES.value:
            pattern = query.strip() if query and query.strip() else (file_pattern or "*")
            res = service.glob_files(
                pattern=pattern,
                root_dir=path,
                max_results=max_results,
            )
        else:
            raise ValueError(f"Unhandled mode {clean_mode}")

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        res = {
            "error": "ERR_INTERNAL",
            "error_code": "ERR_INTERNAL",
            "message": f"Search failed: {exc}",
            "results": [],
            "total_matches": 0,
            "truncated": False,
            "success": False,
        }
        record_trace(
            tool_name="code_search",
            duration_ms=elapsed_ms,
            status="error",
            error_code="ERR_INTERNAL",
            estimated_tokens=estimate_tokens(json.dumps(res)),
            metadata={"mode": mode, "query": query},
        )
        return res

    elapsed_ms = (time.perf_counter() - start_time) * 1000
    res["execution_time_ms"] = round(elapsed_ms, 2)
    res["success"] = "error" not in res and res.get("error_code") is None

    # Telemetry and token estimation
    token_count = estimate_tokens(json.dumps(res, default=str))
    record_trace(
        tool_name="code_search",
        duration_ms=elapsed_ms,
        status="success" if res["success"] else "error",
        error_code=res.get("error_code"),
        estimated_tokens=token_count,
        metadata={"mode": clean_mode, "query": query, "total_matches": res.get("total_matches", 0)},
    )

    # Token quota warning injection
    tracker = get_token_tracker()
    if tracker.is_warning_active():
        res["warning"] = tracker.get_warning_header()

    return res


def register_search_tools(mcp: Any) -> List[str]:
    """Register code_search tool on FastMCP instance."""
    mcp.tool()(code_search)
    return ["code_search"]


__all__ = [
    "code_search",
    "register_search_tools",
    "SearchMode",
    "ERR_INVALID_MODE",
]
