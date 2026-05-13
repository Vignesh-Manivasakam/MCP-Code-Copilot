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
                    if not include_hidden and child.name.startswith("."):
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
    def read_file(file_path: str, max_lines: Optional[int] = None) -> Dict[str, Any]:
        """
        Read the contents of a file from the project.

        Args:
            file_path: Relative path to the file from the project root
                       (e.g. "src/main.py").
            max_lines: When set, only the first *max_lines* lines are returned
                       and ``truncated`` will be ``True`` in the response.
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

        lines = content.splitlines()
        total_lines = len(lines)
        truncated = False

        if max_lines is not None and max_lines < total_lines:
            content = "\n".join(lines[:max_lines])
            truncated = True

        return create_success_response(
            {
                "file_path": file_path,
                "content": content,
                "encoding": encoding,
                "lines": total_lines,
                "size": file_size,
                "size_human": get_file_size_human(file_size),
                "size_warning": should_warn_file_size(file_size),
                "truncated": truncated,
            }
        )

    # ------------------------------------------------------------------
    # Tool 2 – write_file
    # ------------------------------------------------------------------

    @mcp.tool()
    def write_file(
        file_path: str, content: str, create_backup: bool = False
    ) -> Dict[str, Any]:
        """
        Write or overwrite a file with *content*.

        Args:
            file_path:     Relative path to the target file.
            content:       UTF-8 text to write.
            create_backup: When ``True`` and the file already exists, a ``.bak``
                           copy is created before overwriting.
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

        if file_existed:
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
    # Tool 3 – create_file
    # ------------------------------------------------------------------

    @mcp.tool()
    def create_file(
        file_path: str, content: str = "", overwrite: bool = False
    ) -> Dict[str, Any]:
        """
        Create a new file, optionally pre-populated with *content*.

        Args:
            file_path: Relative path for the new file.
            content:   Initial file content (default: empty).
            overwrite: When ``False`` (default) the tool returns an error if the
                       file already exists.  Set to ``True`` to allow overwriting.
        """
        is_valid, resolved, error_msg = SecurityValidator.validate_path(file_path)
        if not is_valid:
            return create_error_response(
                ErrorCode.SECURITY_VIOLATION, error_msg, {"file_path": file_path}
            )

        if resolved.exists() and not overwrite:
            return create_error_response(
                ErrorCode.FILE_EXISTS,
                f"File already exists: {file_path}",
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
            with open(resolved, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            return create_error_response(
                ErrorCode.WRITE_ERROR,
                f"Failed to create file: {exc}",
                {"file_path": file_path},
            )

        bytes_written = len(content.encode("utf-8"))
        return create_success_response(
            {
                "file_path": file_path,
                "action": "created",
                "bytes_written": bytes_written,
                "lines": len(content.splitlines()),
            }
        )

    # ------------------------------------------------------------------
    # Tool 4 – list_files
    # ------------------------------------------------------------------

    @mcp.tool()
    def list_files(
        directory: str = ".", pattern: str = "*", recursive: bool = False
    ) -> Dict[str, Any]:
        """
        List files / directories inside *directory*.

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
        """
        Return a nested tree of the entire project.

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
        """
        Search for *query* text across all matching files in the project.

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

        for file_path in Config.PROJECT_ROOT.rglob(file_pattern):
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
    # Tool 7 – find_function
    # ------------------------------------------------------------------

    @mcp.tool()
    def find_function(
        function_name: str, file_pattern: str = "*.py"
    ) -> Dict[str, Any]:
        """
        Locate function / method definitions matching *function_name*.

        Uses regex-based pattern matching – adequate for Phase 1.

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

        for file_path in Config.PROJECT_ROOT.rglob(file_pattern):
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

        for file_path in Config.PROJECT_ROOT.rglob(file_pattern):
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
        """
        Return detailed metadata about a file or directory.

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
        """
        Analyse a source-code file and return metrics.

        Metrics include line counts by type, detected functions/classes/imports,
        and a simple complexity rating (low / medium / high).

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

        try:
            file_count = sum(1 for p in resolved.rglob("*") if p.is_file())
        except Exception:  # noqa: BLE001
            file_count = -1

        return create_success_response(
            {
                "project_root": str(resolved),
                "project_name": resolved.name,
                "file_count": file_count,
                "previous_root": previous,
            },
            f"✅ Project root set to: {resolved}  ({file_count} files found)",
        )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    tool_names = [
        "read_file",
        "write_file",
        "create_file",
        "list_files",
        "get_file_structure",
        "search_in_files",
        "find_function",
        "find_references",
        "get_file_info",
        "analyze_file",
        "set_project_root",
    ]
    logger.info("Registered %d file-operation tools", len(tool_names))
    return tool_names
