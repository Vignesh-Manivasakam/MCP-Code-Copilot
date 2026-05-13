"""
Security middleware – path validation and permission checks.

All file operation tools MUST call SecurityValidator.validate_path() before
touching the filesystem.  The project root acts as a hard sandbox boundary;
any path that resolves outside of it is rejected immediately.
"""
import os
import logging
from pathlib import Path
from typing import Tuple

from config import Config

logger = logging.getLogger(__name__)


class SecurityValidator:
    """Validates file-system paths against the configured project root."""

    @staticmethod
    def validate_path(file_path: str) -> Tuple[bool, Path, str]:
        """
        Validate that *file_path* is safe and lies within the project root.

        Checks performed (in order):
        1. PROJECT_ROOT is configured.
        2. The supplied path is not absolute.
        3. The path does not contain ``..`` components before resolution.
        4. The resolved (symlink-free) path starts with PROJECT_ROOT.

        Args:
            file_path: Path supplied by the caller – may be relative or use
                       forward/back slashes.

        Returns:
            ``(is_valid, resolved_path, error_message)``

            - *is_valid*       – ``True`` iff the path passed all checks.
            - *resolved_path*  – The absolute, resolved Path when valid;
                                 ``Path(".")`` when invalid.
            - *error_message*  – Human-readable reason for rejection (empty
                                 string when valid).
        """
        _SENTINEL = Path(".")

        if Config.PROJECT_ROOT is None:
            return False, _SENTINEL, "Server not configured: PROJECT_ROOT is not set"

        try:
            path = Path(file_path)
        except (TypeError, ValueError) as exc:
            return False, _SENTINEL, f"Invalid path: {exc}"

        # Reject absolute paths outright – they bypass root containment
        if path.is_absolute():
            logger.warning("Rejected absolute path attempt: %s", file_path)
            return False, _SENTINEL, "Absolute paths are not allowed; use a path relative to the project root"

        # Eagerly reject any path that contains ".." components
        try:
            parts = path.parts
        except Exception:  # noqa: BLE001
            parts = ()

        if ".." in parts:
            logger.warning("Rejected path-traversal attempt: %s", file_path)
            return False, _SENTINEL, "Path traversal ('..') is not permitted"

        # Resolve symlinks and normalise
        try:
            resolved = (Config.PROJECT_ROOT / path).resolve()
        except OSError as exc:
            return False, _SENTINEL, f"Cannot resolve path: {exc}"

        # Ensure the resolved path stays within the project root
        try:
            resolved.relative_to(Config.PROJECT_ROOT)
        except ValueError:
            logger.warning(
                "Rejected out-of-root path: %s resolved to %s", file_path, resolved
            )
            return False, _SENTINEL, "Access denied: path is outside the project root"

        return True, resolved, ""

    @staticmethod
    def check_file_readable(file_path: Path) -> Tuple[bool, str]:
        """
        Check that *file_path* exists and is readable as a file.

        Args:
            file_path: Absolute, already-validated path.

        Returns:
            ``(is_readable, error_message)``
        """
        if not file_path.exists():
            return False, f"File does not exist: {file_path}"
        if file_path.is_dir():
            return False, f"Path is a directory, not a file: {file_path}"
        if not os.access(file_path, os.R_OK):
            return False, f"Permission denied (read): {file_path}"
        return True, ""

    @staticmethod
    def check_file_writable(file_path: Path) -> Tuple[bool, str]:
        """
        Check that the directory containing *file_path* is writable and, if
        the file already exists, that the file itself is writable.

        Args:
            file_path: Absolute, already-validated path.

        Returns:
            ``(is_writable, error_message)``
        """
        parent = file_path.parent
        if not parent.exists():
            # The caller may create parent dirs, so only report if the root
            # ancestor within the project is also missing.
            return True, ""  # mkdir will be attempted by the tool

        if not os.access(parent, os.W_OK):
            return False, f"Parent directory is not writable: {parent}"

        if file_path.exists() and not os.access(file_path, os.W_OK):
            return False, f"File is not writable: {file_path}"

        return True, ""

    @staticmethod
    def is_binary_file(file_path: Path) -> bool:
        """
        Detect whether *file_path* is a binary file.

        Reads up to 8 KB and checks for null bytes, which almost never appear
        in text content but are common in binary formats.

        Args:
            file_path: Absolute path to inspect.

        Returns:
            ``True`` if the file is (likely) binary.
        """
        try:
            with open(file_path, "rb") as fh:
                chunk = fh.read(8_192)
            return b"\x00" in chunk
        except OSError:
            # If we can't read it we treat it as binary to be safe
            return True
