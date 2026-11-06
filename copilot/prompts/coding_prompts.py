"""
Coding Prompts Subsystem for Code Copilot MCP Server.
=====================================================
Defines standardized MCP prompts guiding LLMs to act as senior software engineers:
- `implement_feature`: Guides LLM through AST inspection, targeted search, atomic patching with file_ops, and verification with run_command.
- `debug_issue`: Guides LLM through failure reproduction, AST/search localization, atomic fix application, and regression verification.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Union

from fastmcp.prompts import prompt

logger = logging.getLogger(__name__)


def _normalize_file_list(files: Union[List[str], str, None]) -> List[str]:
    """Normalize a list, JSON array string, comma/newline-delimited string, or single string into a clean list of file paths.

    Args:
        files: File list or string representation.

    Returns:
        List of cleaned, non-empty file path strings.
    """
    if not files:
        return []
    if isinstance(files, list):
        return [str(f).strip() for f in files if str(f).strip()]
    if isinstance(files, str):
        cleaned = files.strip()
        if not cleaned:
            return []
        if cleaned.startswith("[") and cleaned.endswith("]"):
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, list):
                    return [str(f).strip() for f in parsed if str(f).strip()]
            except Exception:
                pass
        if "," in cleaned or "\n" in cleaned:
            parts = re.split(r"[,\n]+", cleaned)
            return [p.strip() for p in parts if p.strip()]
        return [cleaned]
    return []


def generate_implement_feature_prompt(
    feature_name: str,
    specification: str,
    files_to_modify: Union[List[str], str, None] = None,
) -> str:
    """Generate structured Markdown prompt instructions for feature implementation.

    Args:
        feature_name: Name of the feature to implement.
        specification: Detailed requirements or acceptance criteria.
        files_to_modify: Target files expected to be created or modified.

    Returns:
        Formatted markdown prompt string.
    """
    norm_files = _normalize_file_list(files_to_modify)
    if norm_files:
        files_block = "\n".join(f"- `{f}`" for f in norm_files)
    else:
        files_block = "None specified upfront. Use `code_search` (`mode='files'`) to discover candidate files."

    return f"""# Feature Implementation Directive: {feature_name}

## Objective
Implement the requested feature strictly adhering to project conventions, safety sandboxing, and atomic patch workflows.

## Specification & Requirements
{specification.strip()}

## Target Files
{files_block}

## Structured Execution Protocol
Follow this 4-phase engineering workflow:

### Phase 1: Exploration & Context Discovery
1. **Structural AST Outlines**: Before reading whole files, inspect candidate source files using `inspect_ast(file_path=...)` to retrieve class/method structures and line spans in <200 tokens.
2. **Targeted Code Search**: Use `code_search` (`mode="symbol"` for function/class definitions, `mode="grep"` for regex patterns with context lines, or `mode="fulltext"` for keyword searches) to map dependencies.
3. **Contextual Read**: Read only the necessary line spans using `file_ops(action="read", file_path=..., start_line=..., end_line=...)`.

### Phase 2: Implementation & Atomic Patching
1. **Atomic Patching**:
   - For single-file updates: Use `file_ops(action="patch", file_path=..., target=..., replacement=...)`. Ensure `target` contains 2-3 lines of unique surrounding context to match unambiguously.
   - For multi-file changes: Execute an atomic transaction using `file_ops(action="transaction", edits=[...])` so all files succeed or roll back together automatically on failure.
   - For new files: Use `file_ops(action="write", file_path=..., content=...)`.
2. **Formatting & Endings**:
   - Preserve existing newline conventions (CRLF `\\r\\n` on Windows, LF `\\n` on POSIX).
   - Match existing indentation (spaces vs tabs) exactly.
3. **Safety Guarantee**:
   - Never edit sensitive files (`.env`, `.git/config`, `id_rsa`, `.secrets`).
   - Every file modification is automatically backed by an undo reverse-patch in `.copilot/history.db` with zero `.bak` files.

### Phase 3: Verification & Diagnostics
1. **Run Tests**: Execute the relevant test suite using `run_command(command="pytest", arguments=[...])`.
2. **Linter & Syntax Verification**: Run `diagnostics(action="file", file_path=...)` on all modified files to ensure zero syntax or linting regressions.
3. **Working Tree Review**: Run `git_ops(action="diff")` to inspect git diff stat and changes.

### Phase 4: Rollback & Recovery (If Needed)
- If unexpected errors or test failures cannot be resolved cleanly, restore modified files using `file_ops(action="undo")` or `git_ops(action="rollback")`.
"""


@prompt(
    name="implement_feature",
    description="Structured prompt template guiding the LLM through spec analysis, AST inspection, code search, atomic patching with file_ops, and verification with run_command.",
)
def implement_feature(
    feature_name: str,
    specification: str,
    files_to_modify: list[str] | str = "",
) -> str:
    """MCP Prompt: implement_feature."""
    return generate_implement_feature_prompt(
        feature_name=feature_name,
        specification=specification,
        files_to_modify=files_to_modify,
    )


def generate_debug_issue_prompt(
    error_message: str,
    reproducing_command: Optional[str] = None,
    suspected_files: Union[List[str], str, None] = None,
) -> str:
    """Generate structured Markdown prompt instructions for defect diagnosis and resolution.

    Args:
        error_message: Verbatim error message, stack trace, or defect description.
        reproducing_command: CLI command or test command reproducing the failure.
        suspected_files: Candidate files where the defect might reside.

    Returns:
        Formatted markdown prompt string.
    """
    norm_files = _normalize_file_list(suspected_files)
    if norm_files:
        files_block = "\n".join(f"- `{f}`" for f in norm_files)
    else:
        files_block = "None specified. Use traceback analysis and `code_search` to locate defect origin."

    reproduce_block = f"`{reproducing_command}`" if reproducing_command else "No reproducing command specified."

    return f"""# Diagnostic & Defect Resolution Directive

## Problem Description / Error Message
```
{error_message.strip()}
```

## Reproducing Command
{reproduce_block}

## Suspected Files
{files_block}

## Structured Diagnostic Protocol
Follow this 4-phase triage and resolution process:

### Phase 1: Reproduction & Triage
1. **Reproduce Failure**: If a reproducing command is provided, run `run_command(command=..., arguments=...)` to observe the raw exit code, stdout, and stderr.
2. **Syntax & Linter Check**: If suspected files are known, run `diagnostics(action="file", file_path=...)` to identify any immediate syntax or type errors.

### Phase 2: Defect Localization & Root Cause Analysis
1. **Traceback Analysis**: Map stack trace frames to source files and line numbers.
2. **Codebase Navigation**:
   - Use `code_search(mode="grep", query=...)` to search for exception messages, error constants, or symbol usages.
   - Use `inspect_ast(file_path=...)` to understand the class and method structure of suspect files without consuming large token budgets.
   - Read localized code windows with `file_ops(action="read", file_path=..., start_line=..., end_line=...)`.
3. **Hypothesis Formation**: Distinguish root cause from symptoms (check for null/None handling, boundary conditions, CRLF/encoding differences, type mismatches, or state mutations).

### Phase 3: Minimal Surgical Fix
1. **Surgical Patch**: Apply a minimal, focused correction using `file_ops(action="patch", ...)`. Avoid unnecessary refactoring or out-of-scope edits.
2. **Atomic Multi-File Changes**: If the fix requires modifying both implementation and tests, use `file_ops(action="transaction", edits=[...])`.
3. **History Protection**: Changes are safely tracked in SQLite history (`.copilot/history.db`) for instant rollback if needed.

### Phase 4: Verification & Regression Check
1. **Confirm Fix**: Re-execute the reproducing command via `run_command` and verify exit code is 0 and output is clean.
2. **Subsystem Regression Test**: Run the test suite for all related modules (`run_command(command="pytest", arguments=[...])`).
3. **Inspect Diff**: Review all changes using `git_ops(action="diff")`.
"""


@prompt(
    name="debug_issue",
    description="Structured prompt template guiding the LLM to reproduce defects, run diagnostics, locate defect origin, apply surgical fixes, and verify resolution.",
)
def debug_issue(
    error_message: str,
    reproducing_command: str | None = None,
    suspected_files: list[str] | str | None = None,
) -> str:
    """MCP Prompt: debug_issue."""
    return generate_debug_issue_prompt(
        error_message=error_message,
        reproducing_command=reproducing_command,
        suspected_files=suspected_files,
    )
