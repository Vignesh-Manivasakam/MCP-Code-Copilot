"""
Copilot Prompts and Resources Subsystem.
========================================
Registers standard MCP prompts and workspace resources for FastMCP:
- Prompts:
  - `implement_feature`: Guides LLM through spec analysis, AST inspection, code search, atomic patching, and verification.
  - `debug_issue`: Guides LLM through reproducing defects, running diagnostics, isolating defects, and verifying fixes.
- Resources:
  - `session://token-usage`: Live JSON telemetry token metrics against 1M budget quota.
  - `project://conventions`: Workspace conventions and architecture guidelines (.copilot/context.md with default fallback).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Tuple

from fastmcp import FastMCP

from copilot.prompts.coding_prompts import (
    debug_issue,
    generate_debug_issue_prompt,
    generate_implement_feature_prompt,
    implement_feature,
)
from copilot.prompts.resources import (
    WORKSPACE_CONVENTIONS_FALLBACK,
    get_project_conventions_content,
    get_session_token_usage_content,
    project_conventions,
    session_token_usage,
)

logger = logging.getLogger(__name__)

ALL_PROMPTS: Tuple[Callable[..., Any], ...] = (
    implement_feature,
    debug_issue,
)

ALL_RESOURCES: Tuple[Callable[..., Any], ...] = (
    session_token_usage,
    project_conventions,
)


def register_copilot_resources(mcp: FastMCP) -> List[str]:
    """Register all standard MCP resources onto the provided FastMCP instance.

    Args:
        mcp: FastMCP server instance.

    Returns:
        List of registered resource function names / identifiers.
    """
    registered: List[str] = []
    for res_fn in ALL_RESOURCES:
        mcp.add_resource(res_fn)
        name = getattr(res_fn, "__name__", str(res_fn))
        registered.append(name)
        logger.debug("Registered MCP resource '%s' with FastMCP", name)
    return registered


def register_copilot_prompts(mcp: FastMCP, include_resources: bool = True) -> List[str]:
    """Register standard MCP prompts (and optionally resources) onto the provided FastMCP instance.

    Args:
        mcp: FastMCP server instance.
        include_resources: Whether to also register standard MCP resources (default True).

    Returns:
        List of registered prompt function names.
    """
    registered: List[str] = []
    for prompt_fn in ALL_PROMPTS:
        mcp.add_prompt(prompt_fn)
        name = getattr(prompt_fn, "__name__", str(prompt_fn))
        registered.append(name)
        logger.debug("Registered MCP prompt '%s' with FastMCP", name)

    if include_resources:
        register_copilot_resources(mcp)

    return registered


__all__ = [
    "implement_feature",
    "debug_issue",
    "generate_implement_feature_prompt",
    "generate_debug_issue_prompt",
    "session_token_usage",
    "project_conventions",
    "get_session_token_usage_content",
    "get_project_conventions_content",
    "WORKSPACE_CONVENTIONS_FALLBACK",
    "ALL_PROMPTS",
    "ALL_RESOURCES",
    "register_copilot_prompts",
    "register_copilot_resources",
]
