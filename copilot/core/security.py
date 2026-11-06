"""
Security Module for Code Copilot.
=================================
Enforces strict path jail sandboxing, sensitive file protection,
directory scanner hygiene, and file integrity validation.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional, Set, Tuple

from config import Config

logger = logging.getLogger(__name__)


def is_sensitive_file(
    path: str | Path | None,
    sensitive_files: Optional[Set[str]] = None,
) -> bool:
    """
    Check if a path points to a sensitive protected file.

    Normalizes backslashes to forward slashes and lowercases before comparison.
    Matches exact relative paths, nested subpaths, and base filenames against
    Config.SENSITIVE_FILES (e.g. .env, .git/config, id_rsa, id_ed25519, .secrets).
    """
    if path is None:
        return False

    if sensitive_files is None:
        sensitive_files = getattr(Config, "SENSITIVE_FILES", {
            ".env",
            ".git/config",
            "id_rsa",
            "id_ed25519",
            ".secrets",
        })

    # Normalize backslashes to forward slashes and lowercase
    path_str = str(path).replace("\\", "/").lower().strip()
    if path_str.startswith("./"):
        path_str = path_str[2:]

    file_name = Path(path_str).name.lower()

    for s in sensitive_files:
        s_norm = s.replace("\\", "/").lower().strip()
        if "/" in s_norm:
            # Entry has directory component like '.git/config'
            if path_str == s_norm or path_str.endswith("/" + s_norm):
                return True
        else:
            # Entry is a basename like '.env', 'id_rsa'
            if file_name == s_norm:
                return True

    return False


def validate_path(
    path: str | Path | None,
    base_dir: Optional[Path | str] = None,
    check_sensitive: bool = True,
) -> Tuple[bool, Optional[Path], str]:
    """
    Validate that *path* is safe and resolves strictly within *base_dir*.

    Features:
    - Accepts valid relative paths inside base_dir.
    - Accepts valid absolute paths that physically resolve inside base_dir.
    - Blocks parent directory traversals ('../', '..\\').
    - Blocks symlinks escaping base_dir.
    - Accepts symlinks resolving inside base_dir.
    - Rejects null bytes in path with ERR_PATH_TRAVERSAL.
    - Normalizes redundant dots ('src/./utils/../utils/helper.py').
    - Normalizes NTFS case-insensitivity on Windows.
    - Rejects sensitive files (Config.SENSITIVE_FILES) by default.

    Returns:
        (is_valid, resolved_path_or_none, error_message)
    """
    if path is None:
        return False, None, "ERR_PATH_TRAVERSAL: Path cannot be None"

    path_str = str(path)
    if not path_str.strip():
        return False, None, "ERR_PATH_TRAVERSAL: Path cannot be empty"

    if "\x00" in path_str:
        return False, None, "ERR_PATH_TRAVERSAL: Null bytes are not permitted in path (Null bytes not allowed)"

    if base_dir is None:
        base_dir = Config.PROJECT_ROOT

    if base_dir is None:
        return False, None, "ERR_CONFIG: Server not configured: PROJECT_ROOT is not set"

    try:
        base_path = Path(base_dir).resolve()
    except Exception as exc:
        return False, None, f"ERR_CONFIG: Cannot resolve project root: {exc}"

    try:
        p = Path(path)
    except Exception as exc:
        return False, None, f"ERR_PATH_TRAVERSAL: Invalid path format: {exc}"

    # Determine candidate path
    if p.is_absolute():
        candidate = p
    elif p.root:
        # Drive-relative on Windows (e.g. \src\main.py)
        candidate = Path(base_path.drive + str(p)) if base_path.drive else p
    else:
        candidate = base_path / p

    # Check for escaping symlink or resolution errors
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return False, None, f"ERR_PATH_TRAVERSAL: Cannot resolve path: {exc}"

    # Verify containment within base_path
    try:
        if not resolved.is_relative_to(base_path):
            is_symlink = False
            try:
                if candidate.is_symlink():
                    is_symlink = True
                else:
                    for parent in candidate.parents:
                        if parent.is_symlink():
                            is_symlink = True
                            break
            except Exception:
                pass

            if is_symlink:
                logger.warning(
                    "Rejected symlink escaping root: %s resolved to %s outside %s",
                    path,
                    resolved,
                    base_path,
                )
                return (
                    False,
                    None,
                    f"ERR_PATH_TRAVERSAL: Symlink target is outside project root: {resolved}",
                )

            logger.warning(
                "Rejected out-of-root path: %s resolved to %s outside %s",
                path,
                resolved,
                base_path,
            )
            return (
                False,
                None,
                f"ERR_PATH_TRAVERSAL: Access denied: path is outside the project root ({resolved})",
            )
    except (ValueError, TypeError):
        return (
            False,
            None,
            "ERR_PATH_TRAVERSAL: Access denied: path is outside the project root",
        )

    # Check sensitive files protection
    if check_sensitive and (is_sensitive_file(path) or is_sensitive_file(resolved)):
        logger.warning("Rejected sensitive file access attempt: %s", path)
        return (
            False,
            None,
            f"ERR_PATH_TRAVERSAL: Sensitive file access is denied: {path}",
        )

    return True, resolved, ""


def filter_excluded_dirs(
    dirs: list[str],
    exclude_dirs: Optional[Set[str]] = None,
) -> list[str]:
    """
    Prune excluded directories in-place (for os.walk) and return the filtered list.

    Matches directory names case-insensitively against Config.EXCLUDE_DIRS
    (.git, .venv, node_modules, __pycache__, etc.).
    """
    if exclude_dirs is None:
        exclude_dirs = getattr(Config, "EXCLUDE_DIRS", set())

    exclude_lower = {x.lower() for x in exclude_dirs}
    dirs[:] = [d for d in dirs if d.lower() not in exclude_lower]
    return dirs


def is_binary_file(file_path: Path | str) -> bool:
    """
    Detect whether *file_path* is a binary file.

    Reads up to 8 KB and checks for null bytes. Returns False for empty files.
    """
    p = Path(file_path)
    try:
        with open(p, "rb") as fh:
            chunk = fh.read(8_192)
        return b"\x00" in chunk
    except OSError:
        return True


def check_file_readable(file_path: Path | str) -> Tuple[bool, str]:
    """
    Check that *file_path* exists and is readable as a regular file.
    """
    p = Path(file_path)
    if not p.exists():
        return False, f"File does not exist: {p}"
    if p.is_dir():
        return False, f"Path is a directory, not a file: {p}"
    if not os.access(p, os.R_OK):
        return False, f"Permission denied (read): {p}"
    return True, ""


def check_file_writable(file_path: Path | str) -> Tuple[bool, str]:
    """
    Check that *file_path* is writable or its parent directory can be created.
    """
    p = Path(file_path)
    parent = p.parent
    if not parent.exists():
        return True, ""

    if not os.access(parent, os.W_OK):
        return False, f"Parent directory is not writable: {parent}"

    if p.exists() and not os.access(p, os.W_OK):
        return False, f"File is not writable: {p}"

    return True, ""


class SecurityValidator:
    """Backward-compatible class wrapper around functional security APIs."""

    validate_path = staticmethod(validate_path)
    is_sensitive_file = staticmethod(is_sensitive_file)
    filter_excluded_dirs = staticmethod(filter_excluded_dirs)
    is_binary_file = staticmethod(is_binary_file)
    check_file_readable = staticmethod(check_file_readable)
    check_file_writable = staticmethod(check_file_writable)


__all__ = [
    "validate_path",
    "is_sensitive_file",
    "filter_excluded_dirs",
    "is_binary_file",
    "check_file_readable",
    "check_file_writable",
    "SecurityValidator",
]
