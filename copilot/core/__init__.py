"""
Core subsystem for Code Copilot:
- Strict path jail security and sensitive file protection (security.py)
- SQLite reverse-patch undo history (history.py)
- Sandboxed subprocess execution with process timeouts (sandbox.py)
"""
from __future__ import annotations

from copilot.core.history import HistoryDB
from copilot.core.sandbox import (
    ERR_COMMAND_FAILED,
    ERR_COMMAND_TIMEOUT,
    ERR_NOT_FOUND,
    ERR_PATH_TRAVERSAL,
    ERR_SECURITY_VIOLATION,
    is_command_allowed,
    kill_process_tree,
    normalize_command_name,
    resolve_binary_path,
    run_sandboxed_command,
    scrub_environment,
    truncate_output,
)
from copilot.core.security import (
    SecurityValidator,
    check_file_readable,
    check_file_writable,
    filter_excluded_dirs,
    is_binary_file,
    is_sensitive_file,
    validate_path,
)

__all__ = [
    # Security
    "validate_path",
    "is_sensitive_file",
    "filter_excluded_dirs",
    "is_binary_file",
    "check_file_readable",
    "check_file_writable",
    "SecurityValidator",
    # History
    "HistoryDB",
    # Sandbox
    "run_sandboxed_command",
    "is_command_allowed",
    "resolve_binary_path",
    "scrub_environment",
    "kill_process_tree",
    "truncate_output",
    "normalize_command_name",
    "ERR_SECURITY_VIOLATION",
    "ERR_COMMAND_TIMEOUT",
    "ERR_NOT_FOUND",
    "ERR_PATH_TRAVERSAL",
    "ERR_COMMAND_FAILED",
]
