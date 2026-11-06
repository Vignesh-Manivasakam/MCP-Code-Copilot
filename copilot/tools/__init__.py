"""
Copilot Tools Subsystem.
========================
Exports the 6 consolidated FastMCP poly-tools and registration utilities:
- `file_ops`: Unified file operations (read, write, patch, delete, info, batch_read, transaction)
- `code_search`: High-speed codebase search (grep, symbol, fulltext FTS5, files glob)
- `run_command`: Sandboxed CLI execution with output compaction and credential scrubbing
- `git_ops`: Native git plumbing and shadow checkpoint/rollback
- `inspect_ast`: Structural symbol outline generator (<200 tokens)
- `diagnostics`: File syntax verification, session telemetry metrics, and token usage
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Tuple

from fastmcp import FastMCP

from copilot.tools.code_search import code_search
from copilot.tools.diagnostics import diagnostics
from copilot.tools.file_ops import file_ops
from copilot.tools.git_ops import git_ops
from copilot.tools.inspect_ast import inspect_ast
from copilot.tools.run_command import run_command

logger = logging.getLogger(__name__)

# Canonical tuple of all 6 consolidated poly-tools
ALL_TOOLS: Tuple[Callable[..., Any], ...] = (
    file_ops,
    code_search,
    run_command,
    git_ops,
    inspect_ast,
    diagnostics,
)


def register_copilot_tools(mcp: FastMCP) -> List[str]:
    """Register all 6 consolidated poly-tools onto the provided FastMCP instance.

    Args:
        mcp: FastMCP server instance.

    Returns:
        List of registered tool function names.
    """
    registered: List[str] = []
    for tool_fn in ALL_TOOLS:
        mcp.add_tool(tool_fn)
        registered.append(tool_fn.__name__)
        logger.debug("Registered poly-tool '%s' with FastMCP", tool_fn.__name__)
    return registered


# Backward-compatibility alias for transitional server/test callers
register_file_tools = register_copilot_tools

__all__ = [
    "file_ops",
    "code_search",
    "run_command",
    "git_ops",
    "inspect_ast",
    "diagnostics",
    "ALL_TOOLS",
    "register_copilot_tools",
    "register_file_tools",
]
