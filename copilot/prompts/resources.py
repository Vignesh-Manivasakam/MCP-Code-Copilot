"""
Resources Subsystem for Code Copilot MCP Server.
================================================
Defines standardized MCP resources exposing runtime state and workspace context:
- `session://token-usage`: Live session token consumption metrics against 1M budget quota.
- `project://conventions`: Workspace conventions and architecture guidelines (.copilot/context.md with default fallback).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from fastmcp.resources import resource

from config import Config

logger = logging.getLogger(__name__)

WORKSPACE_CONVENTIONS_FALLBACK = """# Workspace Conventions & Architecture Guidelines

## Architecture & Tool Usage Rules
1. **File Operations**:
   - Always route code modifications through `file_ops`.
   - Prefer surgical atomic patching (`action='patch'`) over whole-file overwrites.
   - For multi-file coordinated refactorings, use `action='transaction'` to guarantee atomic rollback on failure.
   - Use `inspect_ast` (<200 tokens) before reading whole files to preserve context window.
   - Use `file_ops(action='read', start_line=..., end_line=...)` for windowed reading.

2. **Security & Sandbox Safeguards**:
   - Path jail: Never access, read, or modify files outside `Config.PROJECT_ROOT`.
   - Sensitive file block: Never access `.env`, `.git/config`, `id_rsa`, `.secrets`.
   - Command whitelist: Only execute approved tools (`pytest`, `python`, `npm`, `git`, `uv`, etc.) through `run_command`.
   - Output compaction: Subprocess outputs over 40 lines are automatically compressed with summary + error traces.

3. **History & Rollback**:
   - Zero backup (`.bak`) files are created on disk.
   - All file modifications are recorded as SQLite reverse patches in `.copilot/history.db` (LRU 20 ring buffer).
   - Use `file_ops(action='undo')` or `git_ops(action='rollback')` to revert changes safely.

4. **Line Endings & Formatting**:
   - Preserve existing newline format (`\\r\\n` on Windows, `\\n` on Unix).
   - Maintain file indentation (spaces vs tabs) without alteration.

5. **Token Quota Awareness**:
   - Track session consumption via `session://token-usage`.
   - If usage exceeds 80% (800,000 tokens), streamline responses and scope tool invocations.
"""


def get_session_token_usage_content() -> str:
    """Retrieve live session token consumption metrics formatted as a JSON string.

    Returns:
        JSON string containing total_estimated_tokens, budget_limit, remaining, percent_used, and warning.
    """
    try:
        from copilot.services.telemetry import get_token_usage
        usage = get_token_usage()
    except Exception as exc:
        logger.error("Failed to retrieve token usage from telemetry: %s", exc)
        usage = {
            "total_estimated_tokens": 0,
            "budget_limit": getattr(Config, "TOKEN_BUDGET_LIMIT", 1_000_000),
            "remaining": getattr(Config, "TOKEN_BUDGET_LIMIT", 1_000_000),
            "percent_used": 0.0,
            "warning": False,
            "warning_header": False,
            "warning_message": None,
        }
    return json.dumps(usage, indent=2)


@resource(
    "session://token-usage",
    mime_type="application/json",
    description="Live session token consumption metrics against 1M budget quota.",
)
def session_token_usage() -> str:
    """MCP Resource: session://token-usage."""
    return get_session_token_usage_content()


def get_project_conventions_content() -> str:
    """Read .copilot/context.md from workspace, or return fallback conventions if missing.

    Returns:
        Markdown string containing workspace coding conventions.
    """
    context_file = Config.CONTEXT_FILE_PATH
    if context_file is None and Config.COPILOT_DIR is not None:
        context_file = Config.COPILOT_DIR / "context.md"

    if context_file is not None:
        try:
            target_path = Path(context_file)
            if target_path.is_file():
                return target_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning(
                "Failed to read context file '%s': %s; using fallback conventions",
                context_file,
                exc,
            )

    return WORKSPACE_CONVENTIONS_FALLBACK


@resource(
    "project://conventions",
    mime_type="text/markdown",
    description="Workspace coding conventions, architecture principles, and tool usage guidelines.",
)
def project_conventions() -> str:
    """MCP Resource: project://conventions."""
    return get_project_conventions_content()
