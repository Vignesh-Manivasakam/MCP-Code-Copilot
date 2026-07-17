"""
Tier 1 File Operation Tools – Code Copilot MCP Server (Phase 1)

All 10 tools are defined inside register_file_tools() so that they can be
bound to the FastMCP instance that is created in server.py at startup.
"""
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config
from middleware.security import SecurityValidator
from utils.encoding_detector import detect_encoding, read_file_with_encoding
from utils.error_handler import ErrorCode, create_error_response, create_success_response
from utils.file_validator import (
    detect_language_from_extension,
    get_file_size_human,
    should_warn_file_size,
    walk_safe_paths,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _build_tree(
    path: Path,
    current_depth: int,
    max_depth: int,
    include_hidden: bool,
    stats: Dict[str, int],
) -> Dict[str, Any]:
    """Recursively build a nested directory-tree dict."""
    node: Dict[str, Any] = {
        "name": path.name or str(path),
        "type": "directory" if path.is_dir() else "file",
    }

    if path.is_dir():
        stats["total_directories"] += 1
        stats["depth_reached"] = max(stats["depth_reached"], current_depth)
        children: List[Dict[str, Any]] = []

        if current_depth < max_depth:
            try:
                # Directories first, then files, both sorted alphabetically
                entries = sorted(
                    path.iterdir(),
                    key=lambda p: (p.is_file(), p.name.lower()),
                )
                for child in entries:
                    if not include_hidden and (child.name.startswith(".") or child.name in Config.IGNORED_DIRS):
                        continue
                    # Skip symlink loops
                    try:
                        if child.is_symlink() and child.resolve() == path.resolve():
                            continue
                    except OSError:
                        continue
                    children.append(
                        _build_tree(child, current_depth + 1, max_depth, include_hidden, stats)
                    )
            except PermissionError:
                logger.warning("Permission denied accessing directory: %s", path)

        node["children"] = children
    else:
        stats["total_files"] += 1
        try:
            node["size"] = path.stat().st_size
        except OSError:
            node["size"] = 0

    return node


def _line_comment_pattern(language: str) -> Optional[re.Pattern]:
    """Return a compiled regex that matches single-line comment starters."""
    patterns = {
        "Python": re.compile(r"^\s*#"),
        "Ruby": re.compile(r"^\s*#"),
        "Shell": re.compile(r"^\s*#"),
        "PowerShell": re.compile(r"^\s*#"),
        "JavaScript": re.compile(r"^\s*//"),
        "TypeScript": re.compile(r"^\s*//"),
        "Java": re.compile(r"^\s*//"),
        "C++": re.compile(r"^\s*//"),
        "C": re.compile(r"^\s*//"),
        "Go": re.compile(r"^\s*//"),
        "Rust": re.compile(r"^\s*//"),
        "C#": re.compile(r"^\s*//"),
        "Kotlin": re.compile(r"^\s*//"),
        "Swift": re.compile(r"^\s*//"),
        "PHP": re.compile(r"^\s*(//|#)"),
        "SQL": re.compile(r"^\s*--"),
    }
    return patterns.get(language)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register_file_tools(mcp) -> List[str]:
    """
    Register all 10 Tier-1 file-operation tools with *mcp*.

    Call this once during server initialisation::

        from tools.file_operations import register_file_tools
        register_file_tools(mcp)

    Args:
        mcp: A ``FastMCP`` instance.

    Returns:
        List of registered tool names (for logging).
    """

    # ------------------------------------------------------------------
    # Tool 1 – read_file
    # ------------------------------------------------------------------

    @mcp.tool()
    def read_file(
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        max_lines: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Read a file from the project. Supports partial reads for token efficiency.

        USE THIS WHEN:
        - You need to see file content before editing
        - You know the approximate line range of interest (use start_line/end_line)
        TOKEN SAVING: Use start_line and end_line to read only the lines you need.
        Reading 30 lines instead of 2000 saves massive tokens.

        Args:
            file_path:  Relative path to the file (e.g. "src/main.py").
            start_line: 1-based start line (inclusive). Omit to start from line 1.
            end_line:   1-based end line (inclusive). Omit to read to end of file.
            max_lines:  Maximum lines to return (applied after line range filtering).
        """
        is_valid, resolved, error_msg = SecurityValidator.validate_path(file_path)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"file_path": file_path}
            )

        is_readable, read_err = SecurityValidator.check_file_readable(resolved)
        if not is_readable:
            code = (
                ErrorCode.FILE_NOT_FOUND
                if not resolved.exists()
                else ErrorCode.PERMISSION_DENIED
            )
            return create_error_response(code, read_err, {"file_path": file_path})

        if SecurityValidator.is_binary_file(resolved):
            return create_error_response(
                ErrorCode.BINARY_FILE_ERROR,
                "Cannot read binary file as text",
                {"file_path": file_path},
            )

        file_size = resolved.stat().st_size
        if file_size > Config.FILE_SIZE_MAX_READ:
            return create_error_response(
                ErrorCode.CONTENT_TOO_LARGE,
                (
                    f"File too large to read: {get_file_size_human(file_size)} "
                    f"(max {get_file_size_human(Config.FILE_SIZE_MAX_READ)})"
                ),
                {"file_path": file_path, "size": file_size},
            )

        try:
            content, encoding = read_file_with_encoding(resolved)
        except Exception as exc:  # noqa: BLE001
            return create_error_response(
                ErrorCode.ENCODING_ERROR,
                f"Failed to read file: {exc}",
                {"file_path": file_path},
            )

        # Track file read state for staleness validation
        try:
            Config.FILE_READ_REGISTRY[resolved] = {
                "content": content,
                "timestamp": resolved.stat().st_mtime
            }
        except OSError:
            pass

        lines = content.splitlines()
        total_lines = len(lines)
        truncated = False
        returned_start = 1
        returned_end = total_lines

        # Apply line range filtering
        if start_line is not None or end_line is not None:
            s = (start_line or 1) - 1  # Convert to 0-based
            e = end_line or total_lines
            if s < 0:
                s = 0
            if e > total_lines:
                e = total_lines
            if s >= e:
                return create_error_response(
                    ErrorCode.INVALID_INPUT,
                    f"Invalid line range: start_line={start_line}, end_line={end_line} (file has {total_lines} lines)",
                    {"file_path": file_path},
                )
            lines = lines[s:e]
            content = "\n".join(lines)
            truncated = True
            returned_start = s + 1
            returned_end = e

        # Apply max_lines limit
        if max_lines is not None and max_lines < len(lines):
            content = "\n".join(lines[:max_lines])
            truncated = True
            returned_end = returned_start + max_lines - 1

        return create_success_response(
            {
                "file_path": file_path,
                "content": content,
                "encoding": encoding,
                "total_lines": total_lines,
                "returned_lines": len(content.splitlines()),
                "returned_range": f"{returned_start}-{returned_end}",
                "size": file_size,
                "size_human": get_file_size_human(file_size),
                "size_warning": should_warn_file_size(file_size),
                "truncated": truncated,
            }
        )

    # ------------------------------------------------------------------
    # Tool 1B – batch_read_files
    # ------------------------------------------------------------------

    @mcp.tool()
    def batch_read_files(
        file_paths: List[str],
        max_lines_per_file: Optional[int] = 200,
    ) -> Dict[str, Any]:
        """Read multiple files in a single tool call. Returns content for all files.

        USE THIS WHEN: You need context from several related files at once
        (e.g., a module and its tests, or a function and its callers).
        TOKEN SAVING: Reading 5 files in one call instead of 5 separate read_file calls
        reduces tool call overhead significantly.

        Args:
            file_paths:         List of relative paths to read (max 10 files).
            max_lines_per_file: Maximum lines per file (default: 200). Set to None for full content.
        """
        if not file_paths:
            return create_error_response(
                ErrorCode.INVALID_INPUT, "No file paths provided", {}
            )

        if len(file_paths) > Config.MAX_BATCH_READ_FILES:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                f"Too many files: {len(file_paths)} (max {Config.MAX_BATCH_READ_FILES})",
                {"count": len(file_paths)},
            )

        results = {}
        errors = {}

        for fp in file_paths:
            is_valid, resolved, error_msg = SecurityValidator.validate_path(fp)
            if not is_valid:
                errors[fp] = error_msg
                continue

            is_readable, read_err = SecurityValidator.check_file_readable(resolved)
            if not is_readable:
                errors[fp] = read_err
                continue

            if SecurityValidator.is_binary_file(resolved):
                errors[fp] = "Binary file — cannot read as text"
                continue

            try:
                content, encoding = read_file_with_encoding(resolved)
            except Exception as exc:
                errors[fp] = f"Read error: {exc}"
                continue

            lines = content.splitlines()
            truncated = False
            if max_lines_per_file is not None and len(lines) > max_lines_per_file:
                content = "\n".join(lines[:max_lines_per_file])
                truncated = True

            results[fp] = {
                "content": content,
                "encoding": encoding,
                "total_lines": len(lines),
                "truncated": truncated,
            }

            # Track in registry
            try:
                Config.FILE_READ_REGISTRY[resolved] = {
                    "content": "\n".join(lines),
                    "timestamp": resolved.stat().st_mtime,
                }
            except OSError:
                pass

        return create_success_response(
            {
                "files": results,
                "errors": errors,
                "files_read": len(results),
                "files_failed": len(errors),
            }
        )

    # ------------------------------------------------------------------
    # Tool 2 – write_file
    # ------------------------------------------------------------------

    @mcp.tool()
    def write_file(
        file_path: str, content: str, create_backup: bool = False
    ) -> Dict[str, Any]:
        """Create a new file with content, or completely overwrite an existing file.

        USE THIS WHEN:
        - Creating a NEW file with content (replaces create_file + write_file pattern)
        - Completely replacing ALL content in an existing file
        DO NOT USE WHEN:
        - Making small edits to an existing file — use modify_file instead (saves tokens)
        TOKEN SAVING: Call this ONCE to create a file with content. Never call create_file first.

        Args:
            file_path:     Relative path to the target file (e.g. "src/app.py").
            content:       Complete UTF-8 text content for the file.
            create_backup: When True and file exists, creates a .bak backup before overwriting.
        """
        is_valid, resolved, error_msg = SecurityValidator.validate_path(file_path)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"file_path": file_path}
            )

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > Config.MAX_WRITE_SIZE:
            return create_error_response(
                ErrorCode.CONTENT_TOO_LARGE,
                (
                    f"Content too large: {get_file_size_human(len(content_bytes))} "
                    f"(max {get_file_size_human(Config.MAX_WRITE_SIZE)})"
                ),
                {"file_path": file_path},
            )

        is_writable, write_err = SecurityValidator.check_file_writable(resolved)
        if not is_writable:
            return create_error_response(
                ErrorCode.PERMISSION_DENIED, write_err, {"file_path": file_path}
            )

        file_existed = resolved.exists()
        backup_created = False
        old_line_count = 0

        # Save pre-write state for undo (only if file existed)
        if file_existed:
            try:
                old_content, old_enc = read_file_with_encoding(resolved)
                Config.EDIT_HISTORY[resolved] = {
                    "content": old_content,
                    "encoding": old_enc,
                }
            except Exception:
                pass

            if create_backup:
                backup_path = resolved.with_suffix(resolved.suffix + ".bak")
                try:
                    shutil.copy2(resolved, backup_path)
                    backup_created = True
                except OSError as exc:
                    logger.warning("Could not create backup for %s: %s", resolved, exc)

            try:
                with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                    old_line_count = sum(1 for _ in fh)
            except OSError:
                old_line_count = 0

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            return create_error_response(
                ErrorCode.WRITE_ERROR,
                f"Failed to write file: {exc}",
                {"file_path": file_path},
            )

        new_line_count = len(content.splitlines())
        action = "updated" if file_existed else "created"

        return create_success_response(
            {
                "file_path": file_path,
                "action": action,
                "bytes_written": len(content_bytes),
                "lines": new_line_count,
                "lines_changed": abs(new_line_count - old_line_count),
                "backup_created": backup_created,
            }
        )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Tool 3 – create_file (lightweight — creates empty file only)
    # ------------------------------------------------------------------

    @mcp.tool()
    def create_file(
        file_path: str, overwrite: bool = False
    ) -> Dict[str, Any]:
        """Create a new EMPTY file (and parent directories if needed).

        IMPORTANT: This tool creates an EMPTY file only. It does NOT accept content.
        After creating the file, use write_file to add content to it.

        USE THIS WHEN: You need to create a new empty file or ensure a file exists.
        DO NOT USE THIS WHEN: You want to create a file WITH content — use write_file directly instead.
        TOKEN SAVING: To create a file with content, call write_file ONCE (not create_file + write_file).

        Args:
            file_path: Relative path for the new file (e.g. "src/utils/helpers.py").
            overwrite: When False (default), returns error if file already exists.
        """
        is_valid, resolved, error_msg = SecurityValidator.validate_path(file_path)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"file_path": file_path}
            )

        if resolved.exists() and not overwrite:
            return create_error_response(
                ErrorCode.FILE_EXISTS,
                f"File already exists: {file_path}. Use write_file to update it, or set overwrite=True.",
                {"file_path": file_path},
            )

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return create_error_response(
                ErrorCode.PERMISSION_DENIED,
                f"Cannot create parent directories: {exc}",
                {"file_path": file_path},
            )

        try:
            resolved.touch(exist_ok=overwrite)
        except OSError as exc:
            return create_error_response(
                ErrorCode.WRITE_ERROR,
                f"Failed to create file: {exc}",
                {"file_path": file_path},
            )

        return create_success_response(
            {
                "file_path": file_path,
                "action": "created",
                "bytes_written": 0,
                "lines": 0,
                "hint": "File created empty. Use write_file to add content.",
            }
        )

    # ------------------------------------------------------------------
    # Tool 4 – list_files
    # ------------------------------------------------------------------

    @mcp.tool()
    def list_files(
        directory: str = ".", pattern: str = "*", recursive: bool = False
    ) -> Dict[str, Any]:
        """List files and directories inside a directory.

        USE THIS WHEN: Exploring project structure, finding files in a specific directory.
        DO NOT USE WHEN: Searching for files by name pattern across the project — use glob_search instead.
        DO NOT USE WHEN: Searching for content inside files — use grep_search instead.

        Args:
            directory: Path relative to project root (default: project root).
            pattern:   Glob filter, e.g. ``"*.py"`` or ``"src/**"`` (default: ``"*"``).
            recursive: When ``True`` the search descends into subdirectories.
        """
        is_valid, resolved_dir, error_msg = SecurityValidator.validate_path(directory)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"directory": directory}
            )

        if not resolved_dir.exists():
            return create_error_response(
                ErrorCode.DIRECTORY_NOT_FOUND,
                f"Directory not found: {directory}",
                {"directory": directory},
            )

        if not resolved_dir.is_dir():
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                f"Path is not a directory: {directory}",
                {"directory": directory},
            )

        try:
            items = list(
                resolved_dir.rglob(pattern) if recursive else resolved_dir.glob(pattern)
            )
        except Exception as exc:  # noqa: BLE001
            return create_error_response(
                ErrorCode.INVALID_PATTERN,
                f"Invalid glob pattern '{pattern}': {exc}",
                {"directory": directory, "pattern": pattern},
            )

        entries: List[Dict[str, Any]] = []
        for item in sorted(items, key=lambda p: (p.is_file(), p.name.lower())):
            # Safety: confirm item is still within project root after glob
            try:
                item.relative_to(Config.PROJECT_ROOT)
            except ValueError:
                continue

            try:
                stat = item.stat()
                rel = str(item.relative_to(Config.PROJECT_ROOT))
                entries.append(
                    {
                        "path": rel,
                        "name": item.name,
                        "size": stat.st_size if item.is_file() else 0,
                        "size_human": get_file_size_human(stat.st_size) if item.is_file() else "-",
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "type": "file" if item.is_file() else "directory",
                    }
                )
            except OSError:
                continue

        return create_success_response(
            {
                "directory": directory,
                "pattern": pattern,
                "recursive": recursive,
                "files": entries,
                "total_count": len(entries),
            }
        )

    # ------------------------------------------------------------------
    # Tool 5 – get_file_structure
    # ------------------------------------------------------------------

    @mcp.tool()
    def get_file_structure(
        max_depth: int = 5, include_hidden: bool = False
    ) -> Dict[str, Any]:
        """Return a nested tree of the entire project.

        USE THIS WHEN: Getting an overview of the entire project layout.
        DO NOT USE WHEN: Looking for a specific file — use glob_search or list_files instead.

        Args:
            max_depth:      Maximum directory depth to traverse (default: 5).
            include_hidden: Include entries whose names begin with ``.``
                            (default: ``False``).
        """
        if Config.PROJECT_ROOT is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "Server not configured: PROJECT_ROOT is not set",
            )

        stats: Dict[str, int] = {
            "total_files": 0,
            "total_directories": 0,
            "depth_reached": 0,
        }

        tree = _build_tree(Config.PROJECT_ROOT, 0, max_depth, include_hidden, stats)

        return create_success_response(
            {
                "project_root": str(Config.PROJECT_ROOT),
                "tree": tree,
                "summary": stats,
            }
        )

    # ------------------------------------------------------------------
    # Tool 6 – search_in_files
    # ------------------------------------------------------------------

    @mcp.tool()
    def search_in_files(
        query: str,
        file_pattern: str = "*",
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> Dict[str, Any]:
        """Search for *query* text across all matching files in the project.

        USE THIS WHEN: Finding where a function is called, locating imports, finding error messages.
        DO NOT USE WHEN: Finding files by name — use list_files with a pattern instead.

        Args:
            query:          Text to search for.
            file_pattern:   Glob that restricts which files are searched
                            (e.g. ``"*.py"``).
            case_sensitive: Perform a case-sensitive match (default: ``False``).
            max_results:    Upper bound on returned match rows (default: 100).
        """
        if not query:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                "Search query cannot be empty",
                {"query": query},
            )

        if Config.PROJECT_ROOT is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR, "Server not configured"
            )

        max_results = min(max_results, Config.MAX_SEARCH_RESULTS)
        flags = 0 if case_sensitive else re.IGNORECASE

        try:
            pattern = re.compile(re.escape(query), flags)
        except re.error as exc:
            return create_error_response(
                ErrorCode.INVALID_INPUT, f"Invalid search query: {exc}"
            )

        results: List[Dict[str, Any]] = []
        files_searched = 0
        total_matches = 0
        truncated = False
        deadline = time.monotonic() + Config.SEARCH_TIMEOUT_SECONDS

        for file_path in walk_safe_paths(Config.PROJECT_ROOT, file_pattern, recursive=True):
            if time.monotonic() > deadline:
                truncated = True
                break

            if not file_path.is_file():
                continue

            if SecurityValidator.is_binary_file(file_path):
                continue

            try:
                if file_path.stat().st_size > 10 * 1024 * 1024:
                    logger.debug("Skipping large file during search: %s", file_path)
                    continue
            except OSError:
                continue

            files_searched += 1
            rel = str(file_path.relative_to(Config.PROJECT_ROOT))

            try:
                content, _ = read_file_with_encoding(file_path)
            except Exception:  # noqa: BLE001
                continue

            for line_no, line in enumerate(content.splitlines(), start=1):
                if pattern.search(line):
                    total_matches += 1
                    results.append(
                        {
                            "file_path": rel,
                            "line_number": line_no,
                            "line_content": line.strip(),
                        }
                    )
                    if len(results) >= max_results:
                        truncated = True
                        break

            if truncated:
                break

        return create_success_response(
            {
                "query": query,
                "results": results,
                "total_matches": total_matches,
                "files_searched": files_searched,
                "file_pattern": file_pattern,
                "case_sensitive": case_sensitive,
                "truncated": truncated,
            }
        )

    # ------------------------------------------------------------------
    # Tool – grep_search (advanced content search)
    # ------------------------------------------------------------------

    @mcp.tool()
    def grep_search(
        pattern: str,
        path: str = ".",
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        is_regex: bool = True,
        case_sensitive: bool = True,
        max_results: int = 50,
        context_lines: int = 2,
    ) -> Dict[str, Any]:
        """Search file contents for a text or regex pattern. Returns matches with surrounding context.

        USE THIS WHEN: Finding function callers, locating imports, searching for error messages,
        finding TODO/FIXME comments, locating configuration values.
        DO NOT USE WHEN: Finding files by name — use list_files or find_function instead.
        TOKEN SAVING: context_lines shows surrounding code so you don't need a follow-up read_file call.

        Args:
            pattern:        Text or regex pattern to search for.
            path:           Directory to search in, relative to project root (default: entire project).
            include:        Glob filter for file names (e.g. "*.py", "*.ts"). Only searches matching files.
            exclude:        Glob filter to exclude files (e.g. "*.test.*", "*.min.js").
            is_regex:       If True (default), treat pattern as regex. If False, treat as literal text.
            case_sensitive: If True (default), search is case-sensitive.
            max_results:    Maximum number of matches to return (default: 50).
            context_lines:  Number of lines to show before and after each match (default: 2).
        """
        is_valid, resolved_dir, error_msg = SecurityValidator.validate_path(path)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"path": path}
            )

        if not resolved_dir.is_dir():
            return create_error_response(
                ErrorCode.DIRECTORY_NOT_FOUND,
                f"Path is not a directory: {path}",
                {"path": path},
            )

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            if is_regex:
                compiled = re.compile(pattern, flags)
            else:
                compiled = re.compile(re.escape(pattern), flags)
        except re.error as exc:
            return create_error_response(
                ErrorCode.INVALID_PATTERN,
                f"Invalid regex pattern: {exc}",
                {"pattern": pattern},
            )

        matches = []
        files_searched = 0

        for file_path in walk_safe_paths(resolved_dir, include or "*", recursive=True):
            if not file_path.is_file():
                continue

            # Apply exclude filter
            if exclude and file_path.match(exclude):
                continue

            if SecurityValidator.is_binary_file(file_path):
                continue

            files_searched += 1
            try:
                content, _ = read_file_with_encoding(file_path)
            except Exception:
                continue

            lines = content.splitlines()
            rel_path = file_path.relative_to(Config.PROJECT_ROOT).as_posix()

            for i, line in enumerate(lines):
                if compiled.search(line):
                    # Get context lines
                    ctx_start = max(0, i - context_lines)
                    ctx_end = min(len(lines), i + context_lines + 1)
                    context_block = "\n".join(
                        f"{'>' if j == i else ' '} {j + 1}: {lines[j]}"
                        for j in range(ctx_start, ctx_end)
                    )

                    matches.append({
                        "file": rel_path,
                        "line": i + 1,
                        "content": line.strip(),
                        "context": context_block,
                    })

                    if len(matches) >= max_results:
                        break

            if len(matches) >= max_results:
                break

        return create_success_response(
            {
                "pattern": pattern,
                "total_matches": len(matches),
                "files_searched": files_searched,
                "truncated": len(matches) >= max_results,
                "results": matches,
            }
        )

    # ------------------------------------------------------------------
    # Tool 7 – find_function
    # ------------------------------------------------------------------

    @mcp.tool()
    def find_function(
        function_name: str, file_pattern: str = "*.py"
    ) -> Dict[str, Any]:
        """Locate function / method definitions matching *function_name*.

        USE THIS WHEN: Locating where a specific function or class is defined.
        DO NOT USE WHEN: Finding where a function is called/used — use search_in_files instead.

        Args:
            function_name: Exact name of the function to find.
            file_pattern:  Glob filter for which files to search (default: ``"*.py"``).
        """
        if not function_name or not re.match(r"^[A-Za-z_]\w*$", function_name):
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                "Invalid function name – must be a valid identifier",
                {"function_name": function_name},
            )

        if Config.PROJECT_ROOT is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR, "Server not configured"
            )

        fn = re.escape(function_name)
        search_patterns = [
            re.compile(rf"^\s*(?:async\s+)?def\s+{fn}\s*\("),               # Python
            re.compile(rf"\bfunction\s+{fn}\s*\("),                          # JS/TS named fn
            re.compile(rf"\basync\s+function\s+{fn}\s*\("),                  # JS/TS async
            re.compile(rf"\b(?:const|let|var)\s+{fn}\s*=\s*(?:async\s*)?(?:function|\()"),  # arrow
            re.compile(rf"\bfunc\s+{fn}\s*\("),                              # Go / Swift
            re.compile(rf"\bfn\s+{fn}\s*\("),                                # Rust
            re.compile(rf"\bdef\s+{fn}\s*\("),                               # Ruby
        ]

        results: List[Dict[str, Any]] = []

        for file_path in walk_safe_paths(Config.PROJECT_ROOT, file_pattern, recursive=True):
            if not file_path.is_file() or SecurityValidator.is_binary_file(file_path):
                continue

            rel = str(file_path.relative_to(Config.PROJECT_ROOT))

            try:
                content, _ = read_file_with_encoding(file_path)
            except Exception:  # noqa: BLE001
                continue

            lines = content.splitlines()

            for line_no, line in enumerate(lines, start=1):
                for pat in search_patterns:
                    if pat.search(line):
                        ctx_start = max(0, line_no - 3)
                        ctx_end = min(len(lines), line_no + 3)
                        results.append(
                            {
                                "file_path": rel,
                                "line_number": line_no,
                                "definition": line.strip(),
                                "context": lines[ctx_start:ctx_end],
                            }
                        )
                        break  # only match once per line

        return create_success_response(
            {
                "function_name": function_name,
                "results": results,
                "total_found": len(results),
            }
        )

    # ------------------------------------------------------------------
    # Tool 8 – find_references
    # ------------------------------------------------------------------

    @mcp.tool()
    def find_references(symbol: str, file_pattern: str = "*") -> Dict[str, Any]:
        """
        Find all usages of *symbol* (whole-word matches) across the project.

        Args:
            symbol:       Identifier to search for.
            file_pattern: Glob filter (default: all files).
        """
        if not symbol:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                "Symbol cannot be empty",
                {"symbol": symbol},
            )

        if Config.PROJECT_ROOT is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR, "Server not configured"
            )

        try:
            pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        except re.error as exc:
            return create_error_response(
                ErrorCode.INVALID_INPUT, f"Invalid symbol: {exc}"
            )

        references: List[Dict[str, Any]] = []

        for file_path in walk_safe_paths(Config.PROJECT_ROOT, file_pattern, recursive=True):
            if not file_path.is_file() or SecurityValidator.is_binary_file(file_path):
                continue

            rel = str(file_path.relative_to(Config.PROJECT_ROOT))

            try:
                content, _ = read_file_with_encoding(file_path)
            except Exception:  # noqa: BLE001
                continue

            lines = content.splitlines()

            for line_no, line in enumerate(lines, start=1):
                if pattern.search(line):
                    ctx_start = max(0, line_no - 2)
                    ctx_end = min(len(lines), line_no + 2)
                    references.append(
                        {
                            "file_path": rel,
                            "line_number": line_no,
                            "line_content": line.strip(),
                            "context": "\n".join(lines[ctx_start:ctx_end]),
                        }
                    )

        return create_success_response(
            {
                "symbol": symbol,
                "references": references,
                "total_references": len(references),
            }
        )

    # ------------------------------------------------------------------
    # Tool 9 – get_file_info
    # ------------------------------------------------------------------

    @mcp.tool()
    def get_file_info(file_path: str) -> Dict[str, Any]:
        """Return detailed metadata about a file or directory.

        USE THIS WHEN: Checking if a file exists, how large it is, or what language it is.
        DO NOT USE WHEN: You need to read the file content — use read_file instead.

        Args:
            file_path: Relative path to the file to inspect.
        """
        is_valid, resolved, error_msg = SecurityValidator.validate_path(file_path)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"file_path": file_path}
            )

        if not resolved.exists():
            return create_success_response({"file_path": file_path, "exists": False})

        try:
            stat = resolved.stat()
        except OSError as exc:
            return create_error_response(
                ErrorCode.PERMISSION_DENIED,
                f"Cannot stat file: {exc}",
                {"file_path": file_path},
            )

        is_binary = SecurityValidator.is_binary_file(resolved)
        is_readable = os.access(resolved, os.R_OK)
        language = detect_language_from_extension(resolved)

        result: Dict[str, Any] = {
            "file_path": file_path,
            "exists": True,
            "size": stat.st_size,
            "size_human": get_file_size_human(stat.st_size),
            "extension": resolved.suffix,
            "language": language,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
            "is_binary": is_binary,
            "is_readable": is_readable,
            "is_directory": resolved.is_dir(),
        }

        if not is_binary and is_readable and resolved.is_file():
            try:
                enc = detect_encoding(resolved)
                result["encoding"] = enc
                with open(resolved, "r", encoding=enc, errors="replace") as fh:
                    result["lines"] = sum(1 for _ in fh)
            except Exception:  # noqa: BLE001
                result["encoding"] = "unknown"
                result["lines"] = None

        return create_success_response(result)

    # ------------------------------------------------------------------
    # Tool 10 – analyze_file
    # ------------------------------------------------------------------

    @mcp.tool()
    def analyze_file(file_path: str) -> Dict[str, Any]:
        """Analyse a source-code file and return metrics.

        USE THIS WHEN: Understanding the complexity and structure of a file before editing.
        DO NOT USE WHEN: You just need to read the content — use read_file instead.

        Args:
            file_path: Relative path to the file to analyse.
        """
        is_valid, resolved, error_msg = SecurityValidator.validate_path(file_path)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"file_path": file_path}
            )

        is_readable, read_err = SecurityValidator.check_file_readable(resolved)
        if not is_readable:
            code = (
                ErrorCode.FILE_NOT_FOUND
                if not resolved.exists()
                else ErrorCode.PERMISSION_DENIED
            )
            return create_error_response(code, read_err, {"file_path": file_path})

        if SecurityValidator.is_binary_file(resolved):
            return create_error_response(
                ErrorCode.BINARY_FILE_ERROR,
                "Cannot analyse binary file",
                {"file_path": file_path},
            )

        try:
            content, _ = read_file_with_encoding(resolved)
        except Exception as exc:  # noqa: BLE001
            return create_error_response(
                ErrorCode.READ_ERROR,
                f"Failed to read file: {exc}",
                {"file_path": file_path},
            )

        language = detect_language_from_extension(resolved)
        comment_pat = _line_comment_pattern(language)

        lines = content.splitlines()
        total_lines = len(lines)
        blank_lines = sum(1 for ln in lines if not ln.strip())
        comment_lines = (
            sum(1 for ln in lines if comment_pat.match(ln)) if comment_pat else 0
        )
        code_lines = max(0, total_lines - blank_lines - comment_lines)

        # Language-specific symbol extraction patterns
        if language == "Python":
            fn_pat = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
            cls_pat = re.compile(r"^\s*class\s+(\w+)\s*[:\(]")
            imp_pat = re.compile(r"^\s*(?:import|from)\s+([\w.]+)")
        elif language in ("JavaScript", "TypeScript"):
            fn_pat = re.compile(
                r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function|\())"
            )
            cls_pat = re.compile(r"^\s*class\s+(\w+)")
            imp_pat = re.compile(
                r'(?:import\s+.*from\s+[\'"](.+?)[\'"]|require\s*\(\s*[\'"](.+?)[\'"])'
            )
        elif language == "Java":
            fn_pat = re.compile(
                r"(?:public|private|protected|static|\s)+\w[\w<>\[\]]*\s+(\w+)\s*\("
            )
            cls_pat = re.compile(
                r"^\s*(?:public\s+|private\s+|protected\s+|abstract\s+|final\s+)*class\s+(\w+)"
            )
            imp_pat = re.compile(r"^\s*import\s+([\w.]+)\s*;")
        elif language == "Go":
            fn_pat = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(")
            cls_pat = re.compile(r"^\s*type\s+(\w+)\s+struct")
            imp_pat = re.compile(r'^\s*(?:import\s+)?"([\w./]+)"')
        elif language == "Rust":
            fn_pat = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*[<\(]")
            cls_pat = re.compile(r"^\s*(?:pub\s+)?struct\s+(\w+)")
            imp_pat = re.compile(r"^\s*use\s+([\w:]+)")
        else:
            fn_pat = re.compile(r"(?:function|def|func)\s+(\w+)\s*\(")
            cls_pat = re.compile(r"^\s*class\s+(\w+)")
            imp_pat = re.compile(r"^\s*(?:import|require|include|using)\s+([\w.\"']+)")

        functions: List[str] = []
        classes: List[str] = []
        imports: List[str] = []

        for ln in lines:
            m = fn_pat.search(ln)
            if m:
                name = next((g for g in m.groups() if g), None)
                if name and name not in functions:
                    functions.append(name)

            m = cls_pat.search(ln)
            if m:
                name = next((g for g in m.groups() if g), None)
                if name and name not in classes:
                    classes.append(name)

            m = imp_pat.search(ln)
            if m:
                name = next((g for g in m.groups() if g), None)
                if name and name not in imports:
                    imports.append(name)

        if total_lines < 100:
            complexity = "low"
        elif total_lines < 500:
            complexity = "medium"
        else:
            complexity = "high"

        return create_success_response(
            {
                "file_path": file_path,
                "language": language,
                "metrics": {
                    "total_lines": total_lines,
                    "code_lines": code_lines,
                    "comment_lines": comment_lines,
                    "blank_lines": blank_lines,
                    "function_count": len(functions),
                    "class_count": len(classes),
                    "import_count": len(imports),
                },
                "complexity": complexity,
                "imports": imports,
                "functions": functions,
                "classes": classes,
            }
        )

    # ------------------------------------------------------------------
    # Tool – get_diagnostics (lightweight syntax checking)
    # ------------------------------------------------------------------

    @mcp.tool()
    def get_diagnostics(file_path: str) -> Dict[str, Any]:
        """Check a file for syntax errors without running it.

        USE THIS WHEN: After making edits with modify_file or write_file, verify the file is valid.
        Supports: Python (.py), JSON (.json), JavaScript (.js — requires node).
        DO NOT USE WHEN: You want to run tests — use execute_command with pytest instead.

        Args:
            file_path: Relative path to the file to check.
        """
        import ast
        import json as json_module

        is_valid, resolved, error_msg = SecurityValidator.validate_path(file_path)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"file_path": file_path}
            )

        is_readable, read_err = SecurityValidator.check_file_readable(resolved)
        if not is_readable:
            return create_error_response(
                ErrorCode.FILE_NOT_FOUND if not resolved.exists() else ErrorCode.PERMISSION_DENIED,
                read_err,
                {"file_path": file_path},
            )

        try:
            content, _ = read_file_with_encoding(resolved)
        except Exception as exc:
            return create_error_response(
                ErrorCode.READ_ERROR, f"Failed to read: {exc}", {"file_path": file_path}
            )

        ext = resolved.suffix.lower()
        diagnostics = []

        if ext == ".py":
            try:
                ast.parse(content, filename=file_path)
            except SyntaxError as e:
                diagnostics.append({
                    "severity": "error",
                    "line": e.lineno,
                    "column": e.offset,
                    "message": str(e.msg),
                })

            # Also check with compile for additional errors
            try:
                compile(content, file_path, "exec")
            except SyntaxError as e:
                if not diagnostics:  # Avoid duplicate
                    diagnostics.append({
                        "severity": "error",
                        "line": e.lineno,
                        "column": e.offset,
                        "message": str(e.msg),
                    })

        elif ext == ".json":
            try:
                json_module.loads(content)
            except json_module.JSONDecodeError as e:
                diagnostics.append({
                    "severity": "error",
                    "line": e.lineno,
                    "column": e.colno,
                    "message": str(e.msg),
                })

        elif ext in (".js", ".ts", ".jsx", ".tsx"):
            import subprocess as _sp
            try:
                res = _sp.run(
                    ["node", "--check", str(resolved)],
                    capture_output=True, text=True, timeout=10,
                    cwd=Config.PROJECT_ROOT,
                )
                if res.returncode != 0:
                    diagnostics.append({
                        "severity": "error",
                        "line": None,
                        "column": None,
                        "message": res.stderr.strip(),
                    })
            except (FileNotFoundError, _sp.TimeoutExpired):
                diagnostics.append({
                    "severity": "warning",
                    "line": None,
                    "column": None,
                    "message": "Node.js not available for JS/TS syntax checking.",
                })
        else:
            return create_success_response({
                "file_path": file_path,
                "language": ext,
                "status": "unsupported",
                "message": f"No syntax checker available for {ext} files.",
                "diagnostics": [],
            })

        status = "error" if diagnostics else "ok"
        return create_success_response({
            "file_path": file_path,
            "language": ext,
            "status": status,
            "diagnostics": diagnostics,
            "total_errors": len(diagnostics),
        })

    # ------------------------------------------------------------------
    # Tool 11 – set_project_root
    # ------------------------------------------------------------------

    @mcp.tool()
    def set_project_root(path: str) -> Dict[str, Any]:
        """
        Set (or change) the project folder the server works on.

        Call this tool when the user has not yet specified a project, or wants
        to switch to a different folder or freshly-cloned repository.

        **Ask the user** for a local folder path or a path to a cloned repo
        before calling this tool.  Do NOT guess or default to any folder.

        Args:
            path: Absolute path to the project folder on the local machine.
                  Example: ``C:/Users/alice/repos/my-project``
                           ``/home/alice/repos/my-project``
        """
        if not path or not path.strip():
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                "No path provided. Please supply an absolute path to the project folder.",
                {"path": path},
            )

        try:
            resolved = Path(path.strip()).resolve()
        except (TypeError, ValueError) as exc:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                f"Invalid path: {exc}",
                {"path": path},
            )

        if not resolved.exists():
            return create_error_response(
                ErrorCode.FILE_NOT_FOUND,
                f"Path does not exist: {resolved}",
                {"path": str(resolved)},
            )

        if not resolved.is_dir():
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                f"Path is not a directory: {resolved}",
                {"path": str(resolved)},
            )

        previous = str(Config.PROJECT_ROOT) if Config.PROJECT_ROOT else None
        Config.PROJECT_ROOT = resolved
        logger.info("Project root changed to: %s", resolved)

        # Clear stale state from any previous project
        Config.FILE_READ_REGISTRY.clear()
        Config.EDIT_HISTORY.clear()

        # Re-initialize FTS5 search index for the new project
        try:
            from services.search_index import initialize_index, index_all_files
            initialize_index(resolved)
            index_all_files(resolved)
        except Exception:  # noqa: BLE001
            logger.warning("Could not build search index for %s", resolved)

        try:
            file_count = sum(1 for p in walk_safe_paths(resolved, "*", recursive=True) if p.is_file())
        except Exception:  # noqa: BLE001
            file_count = -1

        return create_success_response(
            {
                "project_root": str(resolved),
                "project_name": resolved.name,
                "file_count": file_count,
                "previous_root": previous,
                "message": f"Project root set to: {resolved}  ({file_count} files found)",
            }
        )

    # ------------------------------------------------------------------
    # Tool 11 – modify_file
    # ------------------------------------------------------------------

    @mcp.tool()
    def modify_file(
        file_path: str,
        target_content: str,
        replacement_content: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        allow_multiple: bool = False,
        create_backup: bool = False,
    ) -> Dict[str, Any]:
        """Make a surgical edit to an existing file by replacing a specific block of text.

        USE THIS WHEN:
        - Changing specific lines or blocks in an existing file
        - Fixing bugs, updating function logic, adding imports to existing files
        - Any edit where you are NOT replacing the entire file content
        DO NOT USE WHEN:
        - Creating a brand new file — use write_file instead
        - Replacing ALL content of a file — use write_file instead
        TOKEN SAVING: Send ONLY the exact target block and its replacement.
        Do NOT send the entire file content. This saves significant tokens.

        Args:
            file_path:           Relative path to the target file.
            target_content:      The exact text block to find. Must match the file content exactly.
                                 Include enough surrounding context to make the match unique.
            replacement_content: The new text to replace the target with.
            start_line:          Optional 1-based start line to restrict search area (improves accuracy).
            end_line:            Optional 1-based end line to restrict search area.
            allow_multiple:      If True, replaces ALL occurrences. If False (default), fails if
                                 target matches more than once (safety guard).
            create_backup:       When True, creates a .bak backup before modifying.
        """
        if not target_content:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                "target_content cannot be empty",
                {"file_path": file_path},
            )

        is_valid, resolved, error_msg = SecurityValidator.validate_path(file_path)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"file_path": file_path}
            )

        is_readable, read_err = SecurityValidator.check_file_readable(resolved)
        if not is_readable:
            return create_error_response(
                ErrorCode.FILE_NOT_FOUND if not resolved.exists() else ErrorCode.PERMISSION_DENIED,
                read_err,
                {"file_path": file_path},
            )

        is_writable, write_err = SecurityValidator.check_file_writable(resolved)
        if not is_writable:
            return create_error_response(
                ErrorCode.PERMISSION_DENIED, write_err, {"file_path": file_path}
            )

        if SecurityValidator.is_binary_file(resolved):
            return create_error_response(
                ErrorCode.BINARY_FILE_ERROR, "Cannot edit binary file", {"file_path": file_path}
            )

        # Read current content and encoding
        try:
            content, encoding = read_file_with_encoding(resolved)
        except Exception as exc:
            return create_error_response(
                ErrorCode.READ_ERROR, f"Failed to read file: {exc}", {"file_path": file_path}
            )

        # Check Read-Staleness
        mtime = resolved.stat().st_mtime
        read_state = Config.FILE_READ_REGISTRY.get(resolved)
        if read_state:
            # If modification time is newer than read time, check if content has actually changed
            if mtime > read_state["timestamp"] and content != read_state["content"]:
                return create_error_response(
                    ErrorCode.WRITE_ERROR,
                    "File has been modified since it was last read. Please read the file again to refresh your context.",
                    {"file_path": file_path},
                )

        # Split content into lines preserving newlines
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)

        # Validate line bounds
        start_idx = 0
        end_idx = total_lines

        if start_line is not None:
            if start_line < 1 or start_line > total_lines:
                return create_error_response(
                    ErrorCode.INVALID_INPUT,
                    f"start_line ({start_line}) is out of bounds (file has {total_lines} lines)",
                    {"file_path": file_path},
                )
            start_idx = start_line - 1

        if end_line is not None:
            if end_line < 1 or end_line > total_lines:
                return create_error_response(
                    ErrorCode.INVALID_INPUT,
                    f"end_line ({end_line}) is out of bounds (file has {total_lines} lines)",
                    {"file_path": file_path},
                )
            if start_line is not None and end_line < start_line:
                return create_error_response(
                    ErrorCode.INVALID_INPUT,
                    f"end_line ({end_line}) cannot be less than start_line ({start_line})",
                    {"file_path": file_path},
                )
            end_idx = end_line

        # Extract search area
        search_block = "".join(lines[start_idx:end_idx])

        # Normalize quotes and newlines helper
        def normalize_quotes(s: str) -> str:
            return s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")

        # Normalize line endings
        target_norm = target_content.replace("\r\n", "\n")
        replacement_norm = replacement_content.replace("\r\n", "\n")

        # Attempt exact match first
        actual_target = target_norm
        match_index = search_block.find(target_norm)
        normalization_applied = False

        # Fallback to normalized quotes comparison
        if match_index == -1:
            sb_norm_quotes = normalize_quotes(search_block)
            target_norm_quotes = normalize_quotes(target_norm)
            match_index = sb_norm_quotes.find(target_norm_quotes)
            if match_index != -1:
                # Retrieve actual string matching in original search_block
                actual_target = search_block[match_index:match_index + len(target_norm)]
                normalization_applied = True

        if match_index == -1:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                f"The target block was not found in the file{' (within the specified lines)' if (start_line or end_line) else ''}.",
                {"file_path": file_path, "target_content": target_content},
            )

        # Preserve quote style if normalized comparison was used
        if normalization_applied:
            def preserve_quote_style(old_str: str, actual_old_str: str, new_str: str) -> str:
                has_double = "“" in actual_old_str or "”" in actual_old_str
                has_single = "‘" in actual_old_str or "’" in actual_old_str
                if not has_double and not has_single:
                    return new_str
                res = new_str
                if has_double:
                    chars = list(res)
                    res_chars = []
                    for i, c in enumerate(chars):
                        if c == '"':
                            is_opening = (i == 0) or (chars[i-1] in (" ", "\t", "\n", "\r", "(", "[", "{"))
                            res_chars.append("“" if is_opening else "”")
                        else:
                            res_chars.append(c)
                    res = "".join(res_chars)
                if has_single:
                    chars = list(res)
                    res_chars = []
                    for i, c in enumerate(chars):
                        if c == "'":
                            is_contraction = (i > 0 and i < len(chars) - 1 and chars[i-1].isalpha() and chars[i+1].isalpha())
                            if is_contraction:
                                res_chars.append("’")
                            else:
                                is_opening = (i == 0) or (chars[i-1] in (" ", "\t", "\n", "\r", "(", "[", "{"))
                                res_chars.append("‘" if is_opening else "’")
                        else:
                            res_chars.append(c)
                    res = "".join(res_chars)
                return res

            replacement_norm = preserve_quote_style(target_norm, actual_target, replacement_norm)

        # Check uniqueness in the search area
        if allow_multiple:
            matches_count = search_block.count(actual_target)
        else:
            # Check normalized quotes count to prevent duplicate matches under either representation
            sb_quotes_norm = normalize_quotes(search_block)
            target_quotes_norm = normalize_quotes(target_norm)
            matches_count = sb_quotes_norm.count(target_quotes_norm)

        if matches_count > 1 and not allow_multiple:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                f"Found {matches_count} matches for the target block. Please provide more surrounding context to uniquely identify the match.",
                {"file_path": file_path, "matches_found": matches_count},
            )

        # Perform replacement
        if allow_multiple:
            new_search_block = search_block.replace(actual_target, replacement_norm)
        else:
            new_search_block = search_block.replace(actual_target, replacement_norm, 1)

        # Reconstruct updated content
        new_content = "".join(lines[:start_idx]) + new_search_block + "".join(lines[end_idx:])

        # Save pre-edit state for undo
        Config.EDIT_HISTORY[resolved] = {
            "content": content,
            "encoding": encoding,
        }

        # Create backup if requested
        backup_created = False
        if create_backup:
            backup_path = resolved.with_suffix(resolved.suffix + ".bak")
            try:
                shutil.copy2(resolved, backup_path)
                backup_created = True
            except OSError as exc:
                logger.warning("Could not create backup for %s: %s", resolved, exc)

        # Write content back
        try:
            with open(resolved, "w", encoding=encoding) as fh:
                fh.write(new_content)
        except OSError as exc:
            return create_error_response(
                ErrorCode.WRITE_ERROR, f"Failed to write file: {exc}", {"file_path": file_path}
            )

        # Update read registry
        try:
            mtime_new = resolved.stat().st_mtime
            Config.FILE_READ_REGISTRY[resolved] = {
                "content": new_content,
                "timestamp": mtime_new,
            }
        except OSError:
            pass

        lines_changed = abs(len(new_content.splitlines()) - total_lines)
        return create_success_response(
            {
                "file_path": file_path,
                "action": "modified",
                "lines_changed": lines_changed,
                "backup_created": backup_created,
            }
        )

    # ------------------------------------------------------------------
    # Tool – undo_edit
    # ------------------------------------------------------------------

    @mcp.tool()
    def undo_edit(file_path: str) -> Dict[str, Any]:
        """Revert the last edit made to a file, restoring its previous content.

        USE THIS WHEN: A modify_file or write_file introduced a bug and you need to revert.
        Only reverts the MOST RECENT edit (single-level undo).
        After undo, you can read_file to see the restored content.

        Args:
            file_path: Relative path to the file to revert.
        """
        is_valid, resolved, error_msg = SecurityValidator.validate_path(file_path)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"file_path": file_path}
            )

        history = Config.EDIT_HISTORY.get(resolved)
        if not history:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                f"No edit history found for {file_path}. Cannot undo.",
                {"file_path": file_path},
            )

        try:
            with open(resolved, "w", encoding=history["encoding"]) as fh:
                fh.write(history["content"])
        except OSError as exc:
            return create_error_response(
                ErrorCode.WRITE_ERROR,
                f"Failed to restore file: {exc}",
                {"file_path": file_path},
            )

        # Update read registry with restored content
        try:
            Config.FILE_READ_REGISTRY[resolved] = {
                "content": history["content"],
                "timestamp": resolved.stat().st_mtime,
            }
        except OSError:
            pass

        # Remove from history (single-level undo)
        del Config.EDIT_HISTORY[resolved]

        restored_lines = len(history["content"].splitlines())
        return create_success_response(
            {
                "file_path": file_path,
                "action": "reverted",
                "restored_lines": restored_lines,
            }
        )

    # ------------------------------------------------------------------
    # Tool 12 – execute_command
    # ------------------------------------------------------------------

    @mcp.tool()
    def execute_command(
        command: str,
        arguments: List[str] = [],
        working_directory: Optional[str] = None,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """Execute a whitelisted development command in the project sandbox.

        USE THIS WHEN: Running tests (pytest), linting (ruff, flake8), git operations,
        building projects (npm run build), or any development task.
        DO NOT USE WHEN: Reading or writing files — use file tools instead.
        SECURITY: Only whitelisted commands are allowed. Commands run sandboxed in the project directory.

        Args:
            command:           The executable to run (must be whitelisted: python, pytest, npm, git, ruff, etc.).
            arguments:         List of arguments for the command.
            working_directory: Subdirectory within project to run in (relative path, default: project root).
            timeout:           Max execution time in seconds (default: 30, max: 300).
        """
        import subprocess

        if Config.PROJECT_ROOT is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR, "Server not configured: PROJECT_ROOT is not set"
            )

        # Verify whitelisted command
        allowed = getattr(Config, "ALLOWED_COMMANDS", set())
        if command not in allowed:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION,
                f"Command '{command}' is not whitelisted. Whitelisted commands: {sorted(list(allowed))}",
                {"command": command},
            )

        full_cmd = [command] + arguments
        logger.info("Executing command: %s", " ".join(full_cmd))

        # Determine working directory
        cwd = Config.PROJECT_ROOT
        if working_directory:
            wd_valid, wd_resolved, wd_err = SecurityValidator.validate_path(working_directory)
            if not wd_valid:
                return create_error_response(
                    ErrorCode.SECURITY_VIOLATION, wd_err, {"working_directory": working_directory}
                )
            if not wd_resolved.is_dir():
                return create_error_response(
                    ErrorCode.DIRECTORY_NOT_FOUND,
                    f"Working directory not found: {working_directory}",
                    {"working_directory": working_directory},
                )
            cwd = wd_resolved

        # Clamp timeout
        actual_timeout = min(max(timeout, 5), Config.COMMAND_TIMEOUT_MAX)

        start_time = time.perf_counter()
        try:
            res = subprocess.run(
                full_cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=actual_timeout,
            )
            elapsed = (time.perf_counter() - start_time) * 1000

            return create_success_response(
                {
                    "command": " ".join(full_cmd),
                    "exit_code": res.returncode,
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "execution_time_ms": round(elapsed, 1),
                }
            )
        except subprocess.TimeoutExpired:
            return create_error_response(
                ErrorCode.TIMEOUT_ERROR,
                f"Command timed out after {actual_timeout} seconds: {' '.join(full_cmd)}",
                {"command": command},
            )
        except Exception as exc:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Failed to execute command: {exc}",
                {"command": command},
            )

    # ------------------------------------------------------------------
    # Tool 18 – search_codebase
    # ------------------------------------------------------------------
    @mcp.tool()
    def search_codebase(
        query: str,
        limit: int = 25,
    ) -> Dict[str, Any]:
        """Search the codebase for keywords or prefix terms using local SQLite FTS5 index.
        
        USE THIS WHEN:
        - You need to search for variables, class names, or text patterns across all files.
        - You want fast full-text search without scanning directories file-by-file.
        """
        if Config.PROJECT_ROOT is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "Server not configured: PROJECT_ROOT is not set. Call set_project_root first.",
            )

        from middleware.gating import gating_registry
        gating_registry.transition("search_codebase")
        
        from services.search_index import search_code
        try:
            results = search_code(Config.PROJECT_ROOT, query, limit)
            return create_success_response({
                "query": query,
                "results": results,
                "count": len(results)
            })
        except Exception as e:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Failed to search codebase index: {e}",
                {"query": query}
            )

    # ------------------------------------------------------------------
    # Tool 19 – lsp_find_definition
    # ------------------------------------------------------------------
    @mcp.tool()
    def lsp_find_definition(
        file_path: str,
        line: int,
        character: int,
        language: str = "python",
    ) -> Dict[str, Any]:
        """Navigate to the definition of a symbol at the specified line and character.
        
        USE THIS WHEN:
        - You want to find where a function, class, or variable is defined.
        """
        if Config.PROJECT_ROOT is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "Server not configured: PROJECT_ROOT is not set. Call set_project_root first.",
            )

        from middleware.gating import gating_registry
        gating_registry.transition("lsp_find_definition")
        
        valid, resolved, err = SecurityValidator.validate_path(file_path)
        if not valid:
            return create_error_response(ErrorCode.SECURITY_VIOLATION, err, {"file_path": file_path})
            
        from services.lsp_client import LSPClient
        client = LSPClient(Config.PROJECT_ROOT, language)
        if not client.start():
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"LSP server not available for language: {language}",
                {"file_path": file_path}
            )
            
        try:
            results = client.find_definition(file_path, line, character)
            return create_success_response({
                "file_path": file_path,
                "line": line,
                "character": character,
                "results": results
            })
        finally:
            client.stop()

    # ------------------------------------------------------------------
    # Tool 20 – lsp_find_references
    # ------------------------------------------------------------------
    @mcp.tool()
    def lsp_find_references(
        file_path: str,
        line: int,
        character: int,
        language: str = "python",
    ) -> Dict[str, Any]:
        """Find all references (usages) of a symbol across the project.
        
        USE THIS WHEN:
        - You want to find where a variable, function, or class is used or called.
        """
        if Config.PROJECT_ROOT is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "Server not configured: PROJECT_ROOT is not set. Call set_project_root first.",
            )

        from middleware.gating import gating_registry
        gating_registry.transition("lsp_find_references")
        
        valid, resolved, err = SecurityValidator.validate_path(file_path)
        if not valid:
            return create_error_response(ErrorCode.SECURITY_VIOLATION, err, {"file_path": file_path})
            
        from services.lsp_client import LSPClient
        client = LSPClient(Config.PROJECT_ROOT, language)
        if not client.start():
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"LSP server not available for language: {language}",
                {"file_path": file_path}
            )
            
        try:
            results = client.find_references(file_path, line, character)
            return create_success_response({
                "file_path": file_path,
                "line": line,
                "character": character,
                "results": results
            })
        finally:
            client.stop()

    # ------------------------------------------------------------------
    # Tool 21 – get_tool_schema
    # ------------------------------------------------------------------
    @mcp.tool()
    def get_tool_schema(tool_name: str) -> Dict[str, Any]:
        """Retrieve the complete argument blueprints (JSON schema) for a lazy-loaded tool.
        
        USE THIS WHEN:
        - You want to invoke a tool (like modify_file or execute_command) but its schema is lazy-loaded (hidden).
        """
        from middleware.gating import gating_registry
        gating_registry.transition("get_tool_schema")
        
        schema = gating_registry.original_schemas.get(tool_name)
        if not schema:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                f"Tool '{tool_name}' schema not found or not registered as lazy.",
                {"tool_name": tool_name}
            )
        return create_success_response({
            "tool_name": tool_name,
            "parameters": schema
        })

    # ------------------------------------------------------------------
    # Tool 22 – git_checkpoint
    # ------------------------------------------------------------------
    @mcp.tool()
    def git_checkpoint(message: str) -> Dict[str, Any]:
        """Create a local Git checkpoint commit of the current workspace state.
        
        USE THIS WHEN:
        - You are about to make significant edits and want to save a rollback checkpoint.
        """
        import subprocess as _git_sp

        if Config.PROJECT_ROOT is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "Server not configured: PROJECT_ROOT is not set. Call set_project_root first.",
            )

        from middleware.gating import gating_registry
        gating_registry.transition("git_checkpoint")
        
        try:
            # Check if repo is git repository
            _git_sp.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=Config.PROJECT_ROOT, capture_output=True, check=True)
            # Stage all changes
            _git_sp.run(["git", "add", "-A"], cwd=Config.PROJECT_ROOT, check=True)
            # Commit
            res = _git_sp.run(["git", "commit", "-m", f"checkpoint: {message}"], cwd=Config.PROJECT_ROOT, capture_output=True, text=True)
            return create_success_response({
                "status": "checkpoint created",
                "message": message,
                "stdout": res.stdout
            })
        except Exception as e:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Failed to create Git checkpoint: {e}",
                {}
            )

    # ------------------------------------------------------------------
    # Tool 23 – git_rollback
    # ------------------------------------------------------------------
    @mcp.tool()
    def git_rollback() -> Dict[str, Any]:
        """Roll back the workspace to the last Git checkpoint.
        
        USE THIS WHEN:
        - Code modifications or test executions broke the codebase and you want to start over.
        """
        import subprocess as _git_sp

        if Config.PROJECT_ROOT is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "Server not configured: PROJECT_ROOT is not set. Call set_project_root first.",
            )

        from middleware.gating import gating_registry
        gating_registry.transition("git_rollback")
        
        try:
            _git_sp.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=Config.PROJECT_ROOT, capture_output=True, check=True)
            # Hard reset to HEAD
            _git_sp.run(["git", "reset", "--hard", "HEAD"], cwd=Config.PROJECT_ROOT, check=True)
            # Clean untracked files
            _git_sp.run(["git", "clean", "-fd"], cwd=Config.PROJECT_ROOT, check=True)
            return create_success_response({
                "status": "rolled back successfully"
            })
        except Exception as e:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Failed to roll back: {e}",
                {}
            )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    tool_names = [
        "read_file",
        "batch_read_files",
        "write_file",
        "create_file",
        "modify_file",
        "undo_edit",
        "list_files",
        "get_file_structure",
        "search_in_files",
        "grep_search",
        "find_function",
        "find_references",
        "get_file_info",
        "analyze_file",
        "get_diagnostics",
        "execute_command",
        "set_project_root",
        "search_codebase",
        "lsp_find_definition",
        "lsp_find_references",
        "get_tool_schema",
        "git_checkpoint",
        "git_rollback",
    ]
    logger.info("Registered %d file-operation tools", len(tool_names))
    return tool_names
