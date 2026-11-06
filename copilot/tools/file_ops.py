"""
Consolidated File Operations Poly-Tool (copilot/tools/file_ops.py)
================================================================
Consolidates read, write, patch, delete, info, batch_read, and multi-file atomic transactions.
Enforces zero .bak files, SQLite reverse-patch undo history, CRLF tolerance,
indentation-insensitive patching, and strict path jail validation.
"""
from __future__ import annotations

import difflib
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import textwrap
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from config import Config
from copilot.core.history import HistoryDB
from copilot.core.security import (
    check_file_readable,
    check_file_writable,
    is_binary_file,
    validate_path,
)
from copilot.services.telemetry import (
    ERR_DIFF_MISMATCH,
    ERR_NOT_FOUND,
    ERR_PATH_TRAVERSAL,
    ERR_STALE_READ,
    SECURITY_VIOLATION,
    estimate_tokens,
    get_token_tracker,
    record_trace,
)

logger = logging.getLogger(__name__)

ERR_INVALID_ACTION = "INVALID_INPUT"
ERR_INVALID_INPUT = "INVALID_INPUT"
ERR_BINARY_FILE = "ERR_BINARY_FILE"
ERR_FILE_TOO_LARGE = "ERR_FILE_TOO_LARGE"
ERR_AMBIGUOUS_MATCH = "ERR_AMBIGUOUS_MATCH"
ERR_INTERNAL = "ERR_INTERNAL"


class FileAction(str, Enum):
    READ = "read"
    WRITE = "write"
    PATCH = "patch"
    DELETE = "delete"
    INFO = "info"
    BATCH_READ = "batch_read"
    TRANSACTION = "transaction"


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def _detect_language(ext: str) -> str:
    ext_lower = ext.lower().lstrip(".")
    lang_map = {
        "py": "python",
        "pyw": "python",
        "pyi": "python",
        "js": "javascript",
        "jsx": "javascript",
        "mjs": "javascript",
        "cjs": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "json": "json",
        "md": "markdown",
        "markdown": "markdown",
        "html": "html",
        "htm": "html",
        "css": "css",
        "scss": "scss",
        "yaml": "yaml",
        "yml": "yaml",
        "toml": "toml",
        "xml": "xml",
        "sql": "sql",
        "sh": "shell",
        "bash": "shell",
        "bat": "batch",
        "cmd": "batch",
        "ps1": "powershell",
        "c": "c",
        "h": "c",
        "cpp": "cpp",
        "hpp": "cpp",
        "rs": "rust",
        "go": "go",
        "java": "java",
        "txt": "text",
        "log": "text",
    }
    return lang_map.get(ext_lower, "text")


def _read_disk_file(path: Path) -> Tuple[str, str]:
    """Read file content trying utf-8 first, then fallback encodings."""
    encodings = ["utf-8"] + [enc for enc in getattr(Config, "FALLBACK_ENCODINGS", ["latin-1", "cp1252", "iso-8859-1"]) if enc != "utf-8"]
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return f.read(), enc
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read(), "utf-8"


def _handle_read(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> Dict[str, Any]:
    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        return {
            "success": False,
            "error": "ERR_INVALID_INPUT: 'file_path' must be a non-empty string.",
            "error_code": ERR_INVALID_INPUT,
        }

    is_valid, resolved, err_msg = validate_path(file_path, check_sensitive=True)
    if not is_valid or resolved is None:
        err_code = ERR_PATH_TRAVERSAL if ("traversal" in err_msg.lower() or "sensitive" in err_msg.lower() or "null" in err_msg.lower()) else SECURITY_VIOLATION
        return {
            "success": False,
            "error": f"{err_code}: {err_msg}",
            "error_code": err_code,
        }

    if not resolved.exists():
        return {
            "success": False,
            "error": f"ERR_NOT_FOUND: File not found: {file_path}",
            "error_code": ERR_NOT_FOUND,
        }

    if not resolved.is_file():
        return {
            "success": False,
            "error": f"ERR_NOT_FOUND: Target path is not a file: {file_path}",
            "error_code": ERR_NOT_FOUND,
        }

    if is_binary_file(resolved):
        return {
            "success": False,
            "error": f"Cannot read binary file as text: {file_path}",
            "error_code": ERR_BINARY_FILE,
        }

    file_size = resolved.stat().st_size
    max_read = getattr(Config, "FILE_SIZE_MAX_READ", 50 * 1024 * 1024)
    if file_size > max_read:
        return {
            "success": False,
            "error": f"ERR_FILE_TOO_LARGE: File size ({file_size} bytes) exceeds limit ({max_read} bytes)",
            "error_code": ERR_FILE_TOO_LARGE,
        }

    raw_content, encoding = _read_disk_file(resolved)

    # Empty file handling
    if len(raw_content) == 0:
        res = {
            "success": True,
            "file_path": str(file_path),
            "content": "",
            "numbered_content": "",
            "total_lines": 0,
            "returned_lines": 0,
            "returned_range": "0-0",
            "size": 0,
            "truncated": False,
            "encoding": encoding,
        }
        Config.FILE_READ_REGISTRY[resolved] = {
            "content": "",
            "timestamp": resolved.stat().st_mtime,
            "hash": hashlib.sha256(b"").hexdigest(),
        }
        Config.FILE_READ_REGISTRY[str(resolved)] = Config.FILE_READ_REGISTRY[resolved]
        return res

    lines = raw_content.splitlines(keepends=True)
    total_lines = len(lines)

    if start_line is not None or end_line is not None:
        s = 1 if start_line is None else max(1, start_line)
        e = total_lines if end_line is None else min(total_lines, end_line)
        if s > total_lines or s > e:
            sliced = []
        else:
            sliced = lines[s - 1 : e]

        returned_lines = len(sliced)
        returned_range = f"{s}-{e}"
        truncated = returned_lines < total_lines
        content = "".join(sliced)
        numbered_content = "\n".join(
            f"{i}: {line.rstrip('\r\n')}" for i, line in enumerate(sliced, start=s)
        )
    else:
        returned_lines = total_lines
        returned_range = f"1-{total_lines}"
        truncated = False
        content = raw_content
        numbered_content = "\n".join(
            f"{i}: {line.rstrip('\r\n')}" for i, line in enumerate(lines, start=1)
        )

    # Register in Config.FILE_READ_REGISTRY
    stat = resolved.stat()
    reg_entry = {
        "content": raw_content,
        "timestamp": stat.st_mtime,
        "hash": hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
    }
    Config.FILE_READ_REGISTRY[resolved] = reg_entry
    Config.FILE_READ_REGISTRY[str(resolved)] = reg_entry
    Config.FILE_READ_REGISTRY[resolved.as_posix()] = reg_entry

    return {
        "success": True,
        "file_path": str(file_path),
        "content": content,
        "numbered_content": numbered_content,
        "total_lines": total_lines,
        "returned_lines": returned_lines,
        "returned_range": returned_range,
        "truncated": truncated,
        "size": file_size,
        "encoding": encoding,
    }


def _handle_write(
    file_path: str,
    content: Optional[str],
    dry_run: bool = False,
) -> Dict[str, Any]:
    if content is None:
        return {
            "success": False,
            "error": "ERR_INVALID_INPUT: 'content' parameter is required for write action.",
            "error_code": ERR_INVALID_INPUT,
        }

    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        return {
            "success": False,
            "error": "ERR_INVALID_INPUT: 'file_path' must be a non-empty string.",
            "error_code": ERR_INVALID_INPUT,
        }

    is_valid, resolved, err_msg = validate_path(file_path, check_sensitive=True)
    if not is_valid or resolved is None:
        err_code = ERR_PATH_TRAVERSAL if ("traversal" in err_msg.lower() or "sensitive" in err_msg.lower() or "null" in err_msg.lower()) else SECURITY_VIOLATION
        return {
            "success": False,
            "error": f"{err_code}: {err_msg}",
            "error_code": err_code,
        }

    max_write = getattr(Config, "MAX_WRITE_SIZE", 10 * 1024 * 1024)
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > max_write:
        return {
            "success": False,
            "error": f"ERR_FILE_TOO_LARGE: Content size ({len(content_bytes)} bytes) exceeds limit ({max_write} bytes)",
            "error_code": ERR_FILE_TOO_LARGE,
        }

    file_existed = resolved.exists()
    pre_edit_content = ""
    if file_existed:
        pre_edit_content, _ = _read_disk_file(resolved)

    if dry_run:
        diff = "".join(
            difflib.unified_diff(
                pre_edit_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
            )
        )
        return {
            "success": True,
            "dry_run": True,
            "file_path": str(file_path),
            "status": "staged",
            "diff": diff,
            "bytes_to_write": len(content_bytes),
        }

    # Record in HistoryDB (Zero .bak files created)
    history_db = HistoryDB()
    patch_summary = "create_file" if not file_existed else f"write {file_path}"
    edit_id = history_db.record_edit(
        file_path=str(resolved),
        pre_edit_content=pre_edit_content,
        post_edit_content=content,
        patch_summary=patch_summary,
    )

    # Atomic write via temp file + os.replace
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temp_target = resolved.with_name(f".tmp_write_{resolved.name}_{uuid4().hex[:8]}")
    try:
        with open(temp_target, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_target, resolved)
    finally:
        if temp_target.exists():
            try:
                temp_target.unlink()
            except OSError:
                pass

    # Update FILE_READ_REGISTRY
    stat = resolved.stat()
    reg_entry = {
        "content": content,
        "timestamp": stat.st_mtime,
        "hash": hashlib.sha256(content_bytes).hexdigest(),
    }
    Config.FILE_READ_REGISTRY[resolved] = reg_entry
    Config.FILE_READ_REGISTRY[str(resolved)] = reg_entry
    Config.FILE_READ_REGISTRY[resolved.as_posix()] = reg_entry

    status = "created" if not file_existed else "updated"
    return {
        "success": True,
        "file_path": str(file_path),
        "status": status,
        "bytes_written": len(content_bytes),
        "edit_id": edit_id,
    }


def _match_and_replace_block(
    original_text: str,
    target: str,
    replacement: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> Tuple[bool, str, Optional[str], Optional[str]]:
    """
    Applies multi-strategy patching:
    1. Exact match
    2. Newline-normalized match
    3. Indentation-insensitive match

    Returns:
        (success: bool, new_text_or_err: str, error_code: str | None, similarity_hint: str | None)
    """
    has_crlf = "\r\n" in original_text
    line_sep = "\r\n" if has_crlf else "\n"
    lines = original_text.splitlines(keepends=True)
    total_lines = len(lines)

    s_idx = (start_line - 1) if (start_line is not None and start_line > 0) else 0
    e_idx = end_line if (end_line is not None and end_line > 0) else total_lines
    s_idx = max(0, min(s_idx, total_lines))
    e_idx = max(s_idx, min(e_idx, total_lines))

    prefix = "".join(lines[:s_idx])
    search_block = "".join(lines[s_idx:e_idx])
    suffix = "".join(lines[e_idx:])

    # Strategy 1: Exact match in search block
    exact_count = search_block.count(target)
    if exact_count == 1:
        rep_clean = replacement
        if has_crlf:
            rep_clean = rep_clean.replace("\r\n", "\n").replace("\n", "\r\n")
        else:
            rep_clean = rep_clean.replace("\r\n", "\n")
        new_search = search_block.replace(target, rep_clean, 1)
        return True, prefix + new_search + suffix, None, None
    elif exact_count > 1:
        return False, f"Target matches {exact_count} times in the specified range. Provide line numbers or more context.", ERR_AMBIGUOUS_MATCH, None

    # Strategy 2: Newline-normalized match (\r\n vs \n)
    sb_norm = search_block.replace("\r\n", "\n")
    tgt_norm = target.replace("\r\n", "\n")
    norm_count = sb_norm.count(tgt_norm)

    if norm_count == 1:
        rep_norm = replacement.replace("\r\n", "\n")
        if has_crlf:
            rep_norm = rep_norm.replace("\n", "\r\n")
        # Replace in normalized search block
        new_sb_norm = sb_norm.replace(tgt_norm, rep_norm.replace("\r\n", "\n"), 1)
        if has_crlf:
            new_sb_norm = new_sb_norm.replace("\n", "\r\n")
        return True, prefix + new_sb_norm + suffix, None, None
    elif norm_count > 1:
        return False, f"Target matches {norm_count} times (normalized newlines). Provide line numbers or more context.", ERR_AMBIGUOUS_MATCH, None

    # Strategy 3: Indentation-insensitive match
    tgt_lines = [l.rstrip("\r\n") for l in tgt_norm.splitlines()]
    rep_lines = [l.rstrip("\r\n") for l in replacement.splitlines()]
    sb_lines = [l.rstrip("\r\n") for l in sb_norm.splitlines()]

    def get_indent(s: str) -> int:
        expanded = s.expandtabs(4)
        return len(expanded) - len(expanded.lstrip())

    tgt_stripped = [l.strip() for l in tgt_lines if l.strip()]
    if tgt_stripped and len(sb_lines) >= len(tgt_lines):
        tgt_len = len(tgt_lines)
        matches: List[Tuple[int, List[str]]] = []
        for i in range(len(sb_lines) - tgt_len + 1):
            window = sb_lines[i : i + tgt_len]
            window_stripped = [l.strip() for l in window if l.strip()]
            if window_stripped == tgt_stripped:
                matches.append((i, window))

        if len(matches) == 1:
            match_idx, window = matches[0]
            w_non_empty = [(get_indent(l), l.strip()) for l in window if l.strip()]
            t_non_empty = [(get_indent(l), l.strip()) for l in tgt_lines if l.strip()]

            w_base = w_non_empty[0][0]
            t_base = t_non_empty[0][0]
            w_steps = [x[0] - w_base for x in w_non_empty if x[0] > w_base]
            t_steps = [x[0] - t_base for x in t_non_empty if x[0] > t_base]

            scale = (w_steps[0] / t_steps[0]) if (w_steps and t_steps and t_steps[0] > 0) else 1.0

            reindented_rep: List[str] = []
            for line in rep_lines:
                if not line.strip():
                    reindented_rep.append("")
                    continue
                cur_indent = get_indent(line)
                scaled_indent = max(0, int(round(w_base + (cur_indent - t_base) * scale)))
                reindented_rep.append(" " * scaled_indent + line.strip())

            new_sb_lines = sb_lines[:match_idx] + reindented_rep + sb_lines[match_idx + tgt_len :]
            new_search = line_sep.join(new_sb_lines)
            if search_block.endswith(line_sep):
                new_search += line_sep
            return True, prefix + new_search + suffix, None, None
        elif len(matches) > 1:
            return False, f"Target matches {len(matches)} times with normalized indentation. Provide line numbers or more context.", ERR_AMBIGUOUS_MATCH, None

    # No match found - compute similarity hint using difflib
    first_tgt_line = tgt_lines[0].strip() if tgt_lines else target.strip()
    candidate_lines = [l.strip() for l in sb_lines if l.strip()]
    hint = difflib.get_close_matches(first_tgt_line, candidate_lines, n=1, cutoff=0.3)
    similarity_hint = hint[0] if hint else None

    return False, "Target block not found in file", ERR_DIFF_MISMATCH, similarity_hint


def _handle_patch(
    file_path: str,
    target: Optional[str],
    replacement: Optional[str] = "",
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if target is None or target == "":
        return {
            "success": False,
            "error": "ERR_INVALID_INPUT: 'target' block to replace cannot be empty.",
            "error_code": ERR_INVALID_INPUT,
        }

    rep_content = replacement if replacement is not None else ""

    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        return {
            "success": False,
            "error": "ERR_INVALID_INPUT: 'file_path' must be a non-empty string.",
            "error_code": ERR_INVALID_INPUT,
        }

    is_valid, resolved, err_msg = validate_path(file_path, check_sensitive=True)
    if not is_valid or resolved is None:
        err_code = ERR_PATH_TRAVERSAL if ("traversal" in err_msg.lower() or "sensitive" in err_msg.lower() or "null" in err_msg.lower()) else SECURITY_VIOLATION
        return {
            "success": False,
            "error": f"{err_code}: {err_msg}",
            "error_code": err_code,
        }

    if not resolved.exists():
        return {
            "success": False,
            "error": f"ERR_NOT_FOUND: File not found: {file_path}",
            "error_code": ERR_NOT_FOUND,
        }

    if is_binary_file(resolved):
        return {
            "success": False,
            "error": f"Cannot patch binary file: {file_path}",
            "error_code": ERR_BINARY_FILE,
        }

    disk_content, encoding = _read_disk_file(resolved)

    # Stale read check
    reg = Config.FILE_READ_REGISTRY.get(resolved) or Config.FILE_READ_REGISTRY.get(str(resolved)) or Config.FILE_READ_REGISTRY.get(resolved.as_posix())
    if reg is not None:
        disk_mtime = resolved.stat().st_mtime
        if disk_content != reg.get("content", ""):
            new_hash = hashlib.sha256(disk_content.encode("utf-8")).hexdigest()
            return {
                "success": False,
                "error": f"File {file_path} was modified externally since last read.",
                "error_code": ERR_STALE_READ,
                "file_hash": new_hash,
                "modified_mtime": disk_mtime,
            }

    success, new_content_or_err, err_code, similarity_hint = _match_and_replace_block(
        original_text=disk_content,
        target=target,
        replacement=rep_content,
        start_line=start_line,
        end_line=end_line,
    )

    if not success:
        res = {
            "success": False,
            "error": new_content_or_err,
            "error_code": err_code or ERR_DIFF_MISMATCH,
        }
        if similarity_hint:
            res["similarity_hint"] = similarity_hint
        return res

    new_content = new_content_or_err

    # Compute lines changed
    orig_lines = disk_content.splitlines()
    new_lines = new_content.splitlines()
    diff_lines = list(difflib.unified_diff(orig_lines, new_lines))
    lines_changed = max(1, sum(1 for l in diff_lines if l.startswith("+") or l.startswith("-")) - 2)

    if dry_run:
        diff = "".join(
            difflib.unified_diff(
                disk_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
            )
        )
        return {
            "success": True,
            "dry_run": True,
            "file_path": str(file_path),
            "status": "staged",
            "diff": diff,
            "lines_changed": lines_changed,
        }

    # Record in HistoryDB (Zero .bak files)
    history_db = HistoryDB()
    edit_id = history_db.record_edit(
        file_path=str(resolved),
        pre_edit_content=disk_content,
        post_edit_content=new_content,
        patch_summary=f"patch {file_path}",
    )

    # Atomic write to disk via temporary file + os.replace
    temp_target = resolved.with_name(f".tmp_patch_{resolved.name}_{uuid4().hex[:8]}")
    try:
        with open(temp_target, "w", encoding=encoding, newline="") as f:
            f.write(new_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_target, resolved)
    finally:
        if temp_target.exists():
            try:
                temp_target.unlink()
            except OSError:
                pass

    # Update FILE_READ_REGISTRY
    stat = resolved.stat()
    reg_entry = {
        "content": new_content,
        "timestamp": stat.st_mtime,
        "hash": hashlib.sha256(new_content.encode("utf-8")).hexdigest(),
    }
    Config.FILE_READ_REGISTRY[resolved] = reg_entry
    Config.FILE_READ_REGISTRY[str(resolved)] = reg_entry
    Config.FILE_READ_REGISTRY[resolved.as_posix()] = reg_entry

    return {
        "success": True,
        "file_path": str(file_path),
        "lines_changed": lines_changed,
        "has_crlf": "\r\n" in disk_content,
        "edit_id": edit_id,
    }


def _handle_delete(
    file_path: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        return {
            "success": False,
            "error": "ERR_INVALID_INPUT: 'file_path' must be a non-empty string.",
            "error_code": ERR_INVALID_INPUT,
        }

    is_valid, resolved, err_msg = validate_path(file_path, check_sensitive=True)
    if not is_valid or resolved is None:
        err_code = ERR_PATH_TRAVERSAL if ("traversal" in err_msg.lower() or "sensitive" in err_msg.lower() or "null" in err_msg.lower()) else SECURITY_VIOLATION
        return {
            "success": False,
            "error": f"{err_code}: {err_msg}",
            "error_code": err_code,
        }

    if not resolved.exists():
        return {
            "success": False,
            "error": f"ERR_NOT_FOUND: File not found: {file_path}",
            "error_code": ERR_NOT_FOUND,
        }

    if resolved.is_dir():
        return {
            "success": False,
            "error": f"ERR_INVALID_INPUT: Cannot delete directory with file delete: {file_path}",
            "error_code": ERR_INVALID_INPUT,
        }

    pre_edit_content, _ = _read_disk_file(resolved)

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "file_path": str(file_path),
            "status": "staged_delete",
            "lines_to_remove": len(pre_edit_content.splitlines()),
        }

    # Record in HistoryDB (post_edit_content="" enables clean undo restoration)
    history_db = HistoryDB()
    edit_id = history_db.record_edit(
        file_path=str(resolved),
        pre_edit_content=pre_edit_content,
        post_edit_content="",
        patch_summary=f"delete {file_path}",
    )

    resolved.unlink()

    # Clean from read registry
    Config.FILE_READ_REGISTRY.pop(resolved, None)
    Config.FILE_READ_REGISTRY.pop(str(resolved), None)
    Config.FILE_READ_REGISTRY.pop(resolved.as_posix(), None)

    return {
        "success": True,
        "file_path": str(file_path),
        "deleted": True,
        "edit_id": edit_id,
    }


def _handle_info(file_path: str) -> Dict[str, Any]:
    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        return {
            "success": False,
            "error": "ERR_INVALID_INPUT: 'file_path' must be a non-empty string.",
            "error_code": ERR_INVALID_INPUT,
        }

    is_valid, resolved, err_msg = validate_path(file_path, check_sensitive=True)
    if not is_valid or resolved is None:
        err_code = ERR_PATH_TRAVERSAL if ("traversal" in err_msg.lower() or "sensitive" in err_msg.lower() or "null" in err_msg.lower()) else SECURITY_VIOLATION
        return {
            "success": False,
            "error": f"{err_code}: {err_msg}",
            "error_code": err_code,
        }

    if not resolved.exists():
        return {
            "success": True,
            "exists": False,
            "file_path": str(file_path),
        }

    stat = resolved.stat()
    is_bin = is_binary_file(resolved) if resolved.is_file() else False
    is_dir = resolved.is_dir()

    total_lines = 0
    if not is_bin and resolved.is_file():
        try:
            content, _ = _read_disk_file(resolved)
            total_lines = len(content.splitlines())
        except Exception:
            total_lines = 0

    return {
        "success": True,
        "exists": True,
        "file_path": str(file_path),
        "is_dir": is_dir,
        "is_file": resolved.is_file(),
        "is_binary": is_bin,
        "size": stat.st_size,
        "size_human": _format_size(stat.st_size),
        "extension": resolved.suffix,
        "language": _detect_language(resolved.suffix),
        "lines": total_lines,
        "is_readable": os.access(resolved, os.R_OK),
        "is_writable": os.access(resolved, os.W_OK),
        "modified": stat.st_mtime,
    }


def _handle_batch_read(paths: Optional[List[str]]) -> Dict[str, Any]:
    if not paths or not isinstance(paths, list):
        return {
            "success": False,
            "error": "ERR_INVALID_INPUT: 'paths' must be a non-empty list of file paths.",
            "error_code": ERR_INVALID_INPUT,
        }

    max_batch = getattr(Config, "MAX_BATCH_READ_FILES", 10)
    if len(paths) > max_batch:
        return {
            "success": False,
            "error": f"INVALID_INPUT: Batch read limit exceeded. Requested {len(paths)} files, maximum allowed is {max_batch}.",
            "error_code": ERR_INVALID_INPUT,
        }

    results: Dict[str, Any] = {}
    files_read = 0
    failed_count = 0

    for path_str in paths:
        read_res = _handle_read(file_path=path_str)
        results[path_str] = read_res
        if read_res.get("success"):
            files_read += 1
        else:
            failed_count += 1

    return {
        "success": True,
        "files_read": files_read,
        "failed_count": failed_count,
        "total_requested": len(paths),
        "files": results,
    }


def _handle_transaction(
    edits: Optional[List[Dict[str, Any]]],
    dry_run: bool = False,
) -> Dict[str, Any]:
    if not edits or not isinstance(edits, list):
        return {
            "success": False,
            "rolled_back": True,
            "error": "INVALID_INPUT: 'edits' must be a non-empty list of file edit operations.",
            "error_code": ERR_INVALID_INPUT,
        }

    # Phase 1: Pre-flight in-memory simulation
    virtual_files: Dict[Path, str] = {}
    virtual_exists: Dict[Path, bool] = {}
    original_states: Dict[Path, Tuple[bool, str]] = {}

    for idx, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return {
                "success": False,
                "rolled_back": True,
                "error": f"INVALID_INPUT: Edit at index {idx} must be a dictionary.",
                "error_code": ERR_INVALID_INPUT,
            }

        file_path = edit.get("file_path")
        action = edit.get("action", "patch")

        if not file_path or not isinstance(file_path, str) or not file_path.strip():
            return {
                "success": False,
                "rolled_back": True,
                "error": f"ERR_INVALID_INPUT: Edit at index {idx} missing 'file_path'.",
                "error_code": ERR_INVALID_INPUT,
            }

        is_valid, resolved, err_msg = validate_path(file_path, check_sensitive=True)
        if not is_valid or resolved is None:
            err_code = ERR_PATH_TRAVERSAL if ("traversal" in err_msg.lower() or "sensitive" in err_msg.lower() or "null" in err_msg.lower()) else SECURITY_VIOLATION
            return {
                "success": False,
                "rolled_back": True,
                "error": f"{err_code}: {err_msg}",
                "error_code": err_code,
            }

        # Load file state into simulation buffer
        if resolved not in original_states:
            if resolved.exists():
                orig_content, _ = _read_disk_file(resolved)
                original_states[resolved] = (True, orig_content)
                virtual_files[resolved] = orig_content
                virtual_exists[resolved] = True
            else:
                original_states[resolved] = (False, "")
                virtual_files[resolved] = ""
                virtual_exists[resolved] = False

        # Apply edit to simulation buffer
        if action == "write":
            content = edit.get("content")
            if content is None:
                return {
                    "success": False,
                    "rolled_back": True,
                    "error": f"INVALID_INPUT: Edit at index {idx} ('write') missing 'content'.",
                    "error_code": ERR_INVALID_INPUT,
                }
            virtual_files[resolved] = content
            virtual_exists[resolved] = True

        elif action == "delete":
            if not virtual_exists[resolved]:
                return {
                    "success": False,
                    "rolled_back": True,
                    "error": f"ERR_NOT_FOUND: Cannot delete non-existent file {file_path}",
                    "error_code": ERR_NOT_FOUND,
                }
            virtual_exists[resolved] = False

        elif action == "patch":
            if not virtual_exists[resolved]:
                return {
                    "success": False,
                    "rolled_back": True,
                    "error": f"ERR_NOT_FOUND: Cannot patch non-existent file {file_path}",
                    "error_code": ERR_NOT_FOUND,
                }
            target = edit.get("target")
            if target is None or target == "":
                return {
                    "success": False,
                    "rolled_back": True,
                    "error": f"INVALID_INPUT: Edit at index {idx} ('patch') missing 'target'.",
                    "error_code": ERR_INVALID_INPUT,
                }
            replacement = edit.get("replacement", "")

            # Stale read verification during pre-flight
            reg = Config.FILE_READ_REGISTRY.get(resolved) or Config.FILE_READ_REGISTRY.get(str(resolved)) or Config.FILE_READ_REGISTRY.get(resolved.as_posix())
            if reg is not None and original_states[resolved][0]:
                if original_states[resolved][1] != reg.get("content", ""):
                    return {
                        "success": False,
                        "rolled_back": True,
                        "error": f"ERR_STALE_READ: File {file_path} was modified externally.",
                        "error_code": ERR_STALE_READ,
                    }

            success, new_content_or_err, err_code, _ = _match_and_replace_block(
                original_text=virtual_files[resolved],
                target=target,
                replacement=replacement,
                start_line=edit.get("start_line"),
                end_line=edit.get("end_line"),
            )
            if not success:
                return {
                    "success": False,
                    "rolled_back": True,
                    "error": f"Transaction patch failed on {file_path}: {new_content_or_err}",
                    "error_code": err_code or ERR_DIFF_MISMATCH,
                }
            virtual_files[resolved] = new_content_or_err
        else:
            return {
                "success": False,
                "rolled_back": True,
                "error": f"INVALID_INPUT: Unsupported transaction action '{action}'.",
                "error_code": ERR_INVALID_INPUT,
            }

    if dry_run:
        previews = {}
        for res_path, (orig_existed, orig_content) in original_states.items():
            if not virtual_exists[res_path]:
                previews[str(res_path)] = "[Deleted]"
            else:
                previews[str(res_path)] = "".join(
                    difflib.unified_diff(
                        orig_content.splitlines(keepends=True),
                        virtual_files[res_path].splitlines(keepends=True),
                        fromfile=f"a/{res_path.name}",
                        tofile=f"b/{res_path.name}",
                    )
                )
        return {
            "success": True,
            "dry_run": True,
            "total_edits": len(edits),
            "files_affected": [str(p) for p in original_states.keys()],
            "previews": previews,
        }

    # Phase 2: Atomic application & rollback guard (Zero .bak files)
    applied_paths: List[Path] = []
    recorded_ids: List[str] = []
    history_db = HistoryDB()

    try:
        # Step 2a: Apply all filesystem modifications atomically
        for res_path, (orig_existed, orig_content) in original_states.items():
            new_existed = virtual_exists[res_path]
            new_content = virtual_files[res_path]

            if not new_existed:
                if orig_existed:
                    if res_path.exists():
                        res_path.unlink()
                    applied_paths.append(res_path)
            else:
                res_path.parent.mkdir(parents=True, exist_ok=True)
                temp_target = res_path.with_name(f".tmp_tx_{res_path.name}_{uuid4().hex[:8]}")
                try:
                    with open(temp_target, "w", encoding="utf-8", newline="") as f:
                        f.write(new_content)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_target, res_path)
                    applied_paths.append(res_path)
                finally:
                    if temp_target.exists():
                        try:
                            temp_target.unlink()
                        except OSError:
                            pass

        # Step 2b: Record edits in HistoryDB and update read registry ONLY upon complete disk success
        for res_path, (orig_existed, orig_content) in original_states.items():
            new_existed = virtual_exists[res_path]
            new_content = virtual_files[res_path]

            if not new_existed:
                if orig_existed:
                    eid = history_db.record_edit(
                        file_path=str(res_path),
                        pre_edit_content=orig_content,
                        post_edit_content="",
                        patch_summary=f"tx delete {res_path.name}",
                    )
                    recorded_ids.append(eid)
            else:
                summary = "create_file" if not orig_existed else f"tx edit {res_path.name}"
                eid = history_db.record_edit(
                    file_path=str(res_path),
                    pre_edit_content=orig_content,
                    post_edit_content=new_content,
                    patch_summary=summary,
                )
                recorded_ids.append(eid)

                # Update read registry
                stat = res_path.stat()
                reg_entry = {
                    "content": new_content,
                    "timestamp": stat.st_mtime,
                    "hash": hashlib.sha256(new_content.encode("utf-8")).hexdigest(),
                }
                Config.FILE_READ_REGISTRY[res_path] = reg_entry
                Config.FILE_READ_REGISTRY[str(res_path)] = reg_entry
                Config.FILE_READ_REGISTRY[res_path.as_posix()] = reg_entry

        return {
            "success": True,
            "total_edits": len(edits),
            "files_modified": [str(p) for p in original_states.keys()],
            "edit_ids": recorded_ids,
        }

    except Exception as exc:
        logger.exception("Transaction application failed mid-execution: %s", exc)
        # Rollback all applied files on disk
        for p in applied_paths:
            orig_existed, orig_content = original_states[p]
            try:
                if orig_existed:
                    with open(p, "w", encoding="utf-8", newline="") as f:
                        f.write(orig_content)
                else:
                    if p.exists():
                        p.unlink()
            except Exception as rollback_err:
                logger.error("Failed rollback on %s: %s", p, rollback_err)

        # Clean up any leftover temporary transaction files across affected directories
        for res_path in original_states.keys():
            try:
                for tmp_file in res_path.parent.glob(f".tmp_tx_{res_path.name}_*"):
                    try:
                        tmp_file.unlink()
                    except OSError:
                        pass
            except Exception:
                pass

        # Rollback any HistoryDB records that may have been created
        if recorded_ids:
            try:
                with history_db._get_connection() as conn:
                    conn.executemany("DELETE FROM edits WHERE id = ?", [(eid,) for eid in recorded_ids])
            except Exception as rollback_db_err:
                logger.error("Failed to rollback history DB records: %s", rollback_db_err)

        return {
            "success": False,
            "rolled_back": True,
            "error": f"Transaction aborted: {exc}",
            "error_code": ERR_INTERNAL,
        }


def file_ops(
    action: str,
    file_path: str = "",
    content: Optional[str] = None,
    target: Optional[str] = None,
    replacement: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    paths: Optional[List[str]] = None,
    edits: Optional[List[Dict[str, Any]]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Unified file operations with atomic writes, reverse-patch history, and CRLF tolerance.

    Actions:
    - 'read': Read full file or line slice [start_line, end_line]
    - 'write': Atomically create or overwrite a file
    - 'patch': Indentation-insensitive surgical text replacement with CRLF preservation
    - 'delete': Safely delete a file with pre-deletion state recorded in undo history
    - 'info': Inspect file metadata, permissions, language, and size
    - 'batch_read': Read multiple files (up to 10) in a single invocation
    - 'transaction': Execute multi-file atomic edits with full rollback simulation
    """
    start_time = time.perf_counter()
    action_clean = str(action).strip().lower()

    valid_actions = {
        FileAction.READ.value,
        FileAction.WRITE.value,
        FileAction.PATCH.value,
        FileAction.DELETE.value,
        FileAction.INFO.value,
        FileAction.BATCH_READ.value,
        FileAction.TRANSACTION.value,
    }

    if action_clean not in valid_actions:
        duration_ms = (time.perf_counter() - start_time) * 1000
        res = {
            "success": False,
            "error": f"INVALID_INPUT: Unsupported file action '{action}'. Supported actions: {', '.join(sorted(valid_actions))}",
            "error_code": ERR_INVALID_ACTION,
        }
        record_trace(
            tool_name="file_ops",
            duration_ms=duration_ms,
            status="error",
            error_code=ERR_INVALID_ACTION,
            estimated_tokens=estimate_tokens(json.dumps(res)),
            metadata={"action": action},
        )
        return res

    try:
        if action_clean == FileAction.READ.value:
            res = _handle_read(file_path=file_path, start_line=start_line, end_line=end_line)
        elif action_clean == FileAction.WRITE.value:
            res = _handle_write(file_path=file_path, content=content, dry_run=dry_run)
        elif action_clean == FileAction.PATCH.value:
            res = _handle_patch(
                file_path=file_path,
                target=target,
                replacement=replacement,
                start_line=start_line,
                end_line=end_line,
                dry_run=dry_run,
            )
        elif action_clean == FileAction.DELETE.value:
            res = _handle_delete(file_path=file_path, dry_run=dry_run)
        elif action_clean == FileAction.INFO.value:
            res = _handle_info(file_path=file_path)
        elif action_clean == FileAction.BATCH_READ.value:
            res = _handle_batch_read(paths=paths)
        elif action_clean == FileAction.TRANSACTION.value:
            res = _handle_transaction(edits=edits, dry_run=dry_run)
        else:
            res = {"success": False, "error": f"Unhandled action '{action}'", "error_code": ERR_INVALID_ACTION}

    except Exception as exc:
        logger.exception("Unexpected error in file_ops action '%s': %s", action, exc)
        res = {
            "success": False,
            "error": f"ERR_INTERNAL: Unexpected error: {exc}",
            "error_code": ERR_INTERNAL,
        }

    duration_ms = (time.perf_counter() - start_time) * 1000
    res["execution_time_ms"] = round(duration_ms, 2)

    status_str = "success" if res.get("success", False) else "error"
    err_code = res.get("error_code")
    tokens = estimate_tokens(json.dumps(res, default=str))

    record_trace(
        tool_name="file_ops",
        duration_ms=duration_ms,
        status=status_str,
        error_code=err_code,
        estimated_tokens=tokens,
        metadata={"action": action_clean, "file_path": str(file_path)},
    )

    tracker = get_token_tracker()
    if tracker.is_warning_active():
        res["warning"] = tracker.get_warning_header()

    return res


def register_file_ops_tool(mcp: Any) -> Any:
    """Register file_ops poly-tool on the FastMCP server instance."""
    return mcp.tool()(file_ops)


__all__ = [
    "file_ops",
    "FileAction",
    "register_file_ops_tool",
    "ERR_INVALID_ACTION",
    "ERR_INVALID_INPUT",
    "ERR_BINARY_FILE",
    "ERR_FILE_TOO_LARGE",
    "ERR_AMBIGUOUS_MATCH",
]
