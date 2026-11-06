"""
Authoritative Test Suite 2: File Operations & Surgical Patching.
================================================================
Tests all 7 actions of the file_ops poly-tool: read, write, patch, delete,
info, batch_read, and multi-file atomic transactions with rollback.
Enforces CRLF line ending preservation, indentation-insensitivity, stale read
detection, zero .bak files, and telemetry instrumentation.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
import pytest

from config import Config
from copilot.core.history import HistoryDB
from copilot.services.telemetry import (
    ERR_DIFF_MISMATCH,
    ERR_NOT_FOUND,
    ERR_PATH_TRAVERSAL,
    ERR_STALE_READ,
    SECURITY_VIOLATION,
)
from copilot.tools.file_ops import (
    file_ops,
    ERR_AMBIGUOUS_MATCH,
    ERR_BINARY_FILE,
    ERR_INVALID_ACTION,
    ERR_INVALID_INPUT,
)


# ==============================================================================
# Positive Cases (Happy Path)
# ==============================================================================

def test_file_ops_read_full(setup_test_project: Path):
    """Reads an entire file cleanly with auto-detected UTF-8 encoding."""
    res = file_ops(action="read", file_path="src/app.py")
    assert res["success"] is True
    assert res["returned_lines"] == res["total_lines"]
    assert "def main():" in res["content"]
    assert "1: def main():" in res["numbered_content"]
    assert res["truncated"] is False


def test_file_ops_read_line_window(setup_test_project: Path):
    """Reads lines 10 to 25 and includes correct 1-based line number prefixes."""
    long_file = setup_test_project / "src" / "long.txt"
    lines = [f"line {i}" for i in range(1, 31)]
    long_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = file_ops(action="read", file_path="src/long.txt", start_line=10, end_line=25)
    assert res["success"] is True
    assert res["returned_lines"] == 16
    assert res["returned_range"] == "10-25"
    assert res["truncated"] is True
    assert "10: line 10" in res["numbered_content"]
    assert "25: line 25" in res["numbered_content"]


def test_file_ops_write_new_file(setup_test_project: Path):
    """Creates a new file atomically, updates read registry, with zero .bak files."""
    new_path = "src/brand_new.py"
    content = "print('brand new')\n"
    res = file_ops(action="write", file_path=new_path, content=content)

    assert res["success"] is True
    assert res["status"] == "created"
    assert (setup_test_project / new_path).read_text(encoding="utf-8") == content

    # Check read registry
    resolved = setup_test_project / new_path
    assert resolved in Config.FILE_READ_REGISTRY or str(resolved) in Config.FILE_READ_REGISTRY

    # Invariant: Zero .bak files created anywhere in workspace
    bak_files = list(setup_test_project.rglob("*.bak"))
    assert bak_files == []


def test_file_ops_write_overwrite_existing(setup_test_project: Path):
    """Overwrites an existing file atomically and records history for undo."""
    target = "src/app.py"
    new_content = "print('overwritten')\n"
    res = file_ops(action="write", file_path=target, content=new_content)

    assert res["success"] is True
    assert res["status"] == "updated"
    assert (setup_test_project / target).read_text(encoding="utf-8") == new_content

    # Invariant: HistoryDB recorded the edit
    db = HistoryDB(setup_test_project / ".copilot" / "history.db")
    assert db.count() >= 1


def test_file_ops_patch_exact_block(setup_test_project: Path):
    """Locates a multi-line block and replaces it surgically with new lines."""
    patch_target = setup_test_project / "src" / "patch_target.py"
    initial_content = (
        "def setup():\n"
        "    pass\n"
        "\n"
        "def compute():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    return a + b\n"
        "\n"
        "def teardown():\n"
        "    pass\n"
    )
    patch_target.write_text(initial_content, encoding="utf-8")

    target_block = "def compute():\n    a = 1\n    b = 2\n    return a + b"
    replacement_block = "def compute():\n    a = 10\n    b = 20\n    c = 30\n    return a + b + c"

    res = file_ops(
        action="patch",
        file_path="src/patch_target.py",
        target=target_block,
        replacement=replacement_block,
    )

    assert res["success"] is True
    updated = patch_target.read_text(encoding="utf-8")
    assert "def compute():\n    a = 10\n    b = 20\n    c = 30\n    return a + b + c" in updated
    assert "def setup():" in updated
    assert "def teardown():" in updated


def test_file_ops_delete_file(setup_test_project: Path):
    """Safely deletes a file and records its content in undo history."""
    to_delete = setup_test_project / "src" / "to_delete.txt"
    to_delete.write_text("pre-delete content", encoding="utf-8")

    res = file_ops(action="delete", file_path="src/to_delete.txt")
    assert res["success"] is True
    assert res["deleted"] is True
    assert not to_delete.exists()

    # Verify HistoryDB undo can restore it
    db = HistoryDB(setup_test_project / ".copilot" / "history.db")
    success, _, restored_path = db.undo(str(to_delete))
    assert success is True
    assert to_delete.exists()
    assert to_delete.read_text(encoding="utf-8") == "pre-delete content"


def test_file_ops_info_file_metadata(setup_test_project: Path):
    """Inspects file metadata: existence, size, language, permissions, lines."""
    res = file_ops(action="info", file_path="src/app.py")
    assert res["success"] is True
    assert res["exists"] is True
    assert res["size"] > 0
    assert res["lines"] > 0
    assert res["language"] == "python"
    assert res["is_binary"] is False
    assert res["is_readable"] is True
    assert res["is_writable"] is True


def test_file_ops_batch_read_multiple_files(setup_test_project: Path):
    """Reads multiple files in a single call (up to 10 files) with per-file status."""
    paths = ["src/app.py", "src/utils.py"]
    res = file_ops(action="batch_read", paths=paths)

    assert res["success"] is True
    assert res["files_read"] == 2
    assert res["failed_count"] == 0
    assert "src/app.py" in res["files"]
    assert "src/utils.py" in res["files"]
    assert res["files"]["src/app.py"]["success"] is True
    assert res["files"]["src/utils.py"]["success"] is True


def test_file_ops_atomic_transaction_success(setup_test_project: Path):
    """Executes multi-file atomic transaction with multiple file edits successfully."""
    edits = [
        {"action": "write", "file_path": "src/tx1.txt", "content": "tx1 content\n"},
        {"action": "write", "file_path": "src/tx2.txt", "content": "tx2 content\n"},
        {
            "action": "patch",
            "file_path": "src/app.py",
            "target": 'print("Hello, world!")',
            "replacement": 'print("Hello, transaction!")',
        },
    ]

    res = file_ops(action="transaction", edits=edits)
    assert res["success"] is True
    assert res["total_edits"] == 3
    assert (setup_test_project / "src" / "tx1.txt").read_text(encoding="utf-8") == "tx1 content\n"
    assert (setup_test_project / "src" / "tx2.txt").read_text(encoding="utf-8") == "tx2 content\n"
    assert 'print("Hello, transaction!")' in (setup_test_project / "src" / "app.py").read_text(encoding="utf-8")


# ==============================================================================
# Edge Cases (X, Y, Z)
# ==============================================================================

def test_file_ops_patch_crlf_windows_file(setup_test_project: Path):
    """File saved on disk with Windows CRLF; LLM sends LF target. Line endings remain CRLF."""
    crlf_file = setup_test_project / "src" / "crlf.py"
    crlf_file.write_bytes(b"def foo():\r\n    return 1\r\n")

    res = file_ops(
        action="patch",
        file_path="src/crlf.py",
        target="def foo():\n    return 1",
        replacement="def foo():\n    return 2",
    )

    assert res["success"] is True
    data = crlf_file.read_bytes()
    assert b"\r\n" in data
    assert b"\r\r\n" not in data
    assert b"def foo():\r\n    return 2\r\n" == data


def test_file_ops_patch_indentation_insensitivity(setup_test_project: Path):
    """Target in file has 4 spaces; LLM sends 2 spaces. Server normalizes to 4 spaces."""
    target_file = setup_test_project / "src" / "indented.py"
    target_file.write_text(
        "def compute():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    return a + b\n",
        encoding="utf-8",
    )

    search_block = "def compute():\n  a = 1\n  b = 2\n  return a + b"
    replacement_block = "def compute():\n  a = 10\n  b = 20\n  return a + b"

    res = file_ops(
        action="patch",
        file_path="src/indented.py",
        target=search_block,
        replacement=replacement_block,
    )

    assert res["success"] is True
    updated = target_file.read_text(encoding="utf-8")
    assert "def compute():\n    a = 10\n    b = 20\n    return a + b\n" == updated


def test_file_ops_patch_unique_match_selection(setup_test_project: Path):
    """Target occurs in two places; start_line and end_line isolate the intended occurrence."""
    duplicate_file = setup_test_project / "src" / "dup.py"
    content = (
        "line 1\n"
        "val = 1\n"
        "line 3\n"
        "line 4\n"
        "val = 1\n"
        "line 6\n"
    )
    duplicate_file.write_text(content, encoding="utf-8")

    res = file_ops(
        action="patch",
        file_path="src/dup.py",
        target="val = 1",
        replacement="val = 99",
        start_line=1,
        end_line=3,
    )

    assert res["success"] is True
    lines = duplicate_file.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "val = 99"
    assert lines[4] == "val = 1"


def test_file_ops_empty_file_handling(setup_test_project: Path):
    """Reading an empty (0-byte) file returns total_lines=0 and content='' without crashing."""
    empty_file = setup_test_project / "src" / "empty.txt"
    empty_file.write_bytes(b"")

    res = file_ops(action="read", file_path="src/empty.txt")
    assert res["success"] is True
    assert res["total_lines"] == 0
    assert res["returned_lines"] == 0
    assert res["content"] == ""
    assert res["size"] == 0


def test_file_ops_binary_file_detection(setup_test_project: Path):
    """Reading a binary file (containing null bytes) returns descriptive error."""
    bin_file = setup_test_project / "src" / "test.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03\x04\x05")

    res = file_ops(action="read", file_path="src/test.bin")
    assert res["success"] is False
    assert res["error_code"] == ERR_BINARY_FILE
    assert "binary file" in res["error"].lower()


def test_file_ops_unicode_and_spaces_in_path(setup_test_project: Path):
    """Handles paths with spaces, unicode characters, and emojis cleanly."""
    file_rel = "src/résumé draft v1 🚀.txt"
    content = "Résumé of John Doe 🚀 — 100% verified\n"

    # Write
    w_res = file_ops(action="write", file_path=file_rel, content=content)
    assert w_res["success"] is True

    # Read
    r_res = file_ops(action="read", file_path=file_rel)
    assert r_res["success"] is True
    assert content in r_res["content"]

    # Patch
    p_res = file_ops(
        action="patch",
        file_path=file_rel,
        target="100% verified",
        replacement="Senior Staff Engineer",
    )
    assert p_res["success"] is True
    assert "Senior Staff Engineer" in (setup_test_project / file_rel).read_text(encoding="utf-8")


def test_file_ops_batch_read_partial_missing(setup_test_project: Path):
    """Batch read handles mix of existing and missing files without crashing."""
    paths = ["src/app.py", "src/nonexistent_xyz.py"]
    res = file_ops(action="batch_read", paths=paths)

    assert res["success"] is True
    assert res["files_read"] == 1
    assert res["failed_count"] == 1
    assert res["files"]["src/app.py"]["success"] is True
    assert res["files"]["src/nonexistent_xyz.py"]["success"] is False
    assert res["files"]["src/nonexistent_xyz.py"]["error_code"] == ERR_NOT_FOUND


def test_file_ops_transaction_dry_run(setup_test_project: Path):
    """Transaction with dry_run=True returns preview diffs without modifying files on disk."""
    edits = [
        {"action": "write", "file_path": "src/staged.txt", "content": "staged text\n"},
        {"action": "patch", "file_path": "src/app.py", "target": "def main():", "replacement": "def main_staged():"},
    ]

    res = file_ops(action="transaction", edits=edits, dry_run=True)
    assert res["success"] is True
    assert res["dry_run"] is True
    assert not (setup_test_project / "src" / "staged.txt").exists()
    assert "def main():" in (setup_test_project / "src" / "app.py").read_text(encoding="utf-8")


# ==============================================================================
# Negative & Failure Cases
# ==============================================================================

def test_file_ops_patch_target_not_found(setup_test_project: Path):
    """Patching non-existent target fails with ERR_DIFF_MISMATCH and similarity hint."""
    res = file_ops(
        action="patch",
        file_path="src/app.py",
        target="def nonexistent_function():\n    return False",
        replacement="def new_func(): pass",
    )

    assert res["success"] is False
    assert res["error_code"] == ERR_DIFF_MISMATCH
    assert "not found" in res["error"].lower()


def test_file_ops_stale_read_conflict(setup_test_project: Path):
    """File read at T0, modified externally at T1, patch at T2 fails with ERR_STALE_READ."""
    target_path = setup_test_project / "src" / "app.py"

    # Step 1: Read file at T0 (registered in Config.FILE_READ_REGISTRY)
    r_res = file_ops(action="read", file_path="src/app.py")
    assert r_res["success"] is True

    # Step 2: External edit at T1 with newer timestamp
    time.sleep(0.02)
    target_path.write_text("# External modification\ndef main():\n    pass\n", encoding="utf-8")

    # Step 3: Attempt patch at T2
    p_res = file_ops(
        action="patch",
        file_path="src/app.py",
        target="def main():",
        replacement="def main_patched():",
    )

    assert p_res["success"] is False
    assert p_res["error_code"] == ERR_STALE_READ
    assert "externally" in p_res["error"].lower()
    assert "file_hash" in p_res


def test_file_ops_read_nonexistent_file(setup_test_project: Path):
    """Reading a non-existent file returns structured ERR_NOT_FOUND."""
    res = file_ops(action="read", file_path="src/does_not_exist.txt")
    assert res["success"] is False
    assert res["error_code"] == ERR_NOT_FOUND


def test_file_ops_reject_directory_traversal(setup_test_project: Path):
    """Path traversal outside project root is rejected with ERR_PATH_TRAVERSAL."""
    res = file_ops(action="read", file_path="../../etc/passwd")
    assert res["success"] is False
    assert res["error_code"] in (ERR_PATH_TRAVERSAL, SECURITY_VIOLATION)


def test_file_ops_reject_sensitive_files(setup_test_project: Path):
    """Accessing protected sensitive files (.git/config, .env) is blocked."""
    res = file_ops(action="read", file_path=".git/config")
    assert res["success"] is False
    assert res["error_code"] in (ERR_PATH_TRAVERSAL, SECURITY_VIOLATION)


def test_file_ops_patch_ambiguous_multiple_matches(setup_test_project: Path):
    """Target occurring multiple times without line bounds fails with ambiguous match error."""
    dup_file = setup_test_project / "src" / "multiple.txt"
    dup_file.write_text("item = 1\nitem = 1\n", encoding="utf-8")

    res = file_ops(
        action="patch",
        file_path="src/multiple.txt",
        target="item = 1",
        replacement="item = 2",
    )

    assert res["success"] is False
    assert res["error_code"] == ERR_AMBIGUOUS_MATCH
    assert "matches" in res["error"].lower()


def test_file_ops_atomic_transaction_rollback_on_failure(setup_test_project: Path):
    """Multi-file transaction rolls back all files if any single file edit fails."""
    file1 = setup_test_project / "src" / "tx_file1.txt"
    file1.write_text("original file1\n", encoding="utf-8")

    edits = [
        {"action": "write", "file_path": "src/tx_file1.txt", "content": "modified file1\n"},
        {
            "action": "patch",
            "file_path": "src/app.py",
            "target": "def totally_missing_target_abc_xyz():",
            "replacement": "def replaced():",
        },
    ]

    res = file_ops(action="transaction", edits=edits)
    assert res["success"] is False
    assert res["rolled_back"] is True

    # Invariant: file1 is 100% restored to its original state on disk
    assert file1.read_text(encoding="utf-8") == "original file1\n"

    # Invariant: Zero .bak files created anywhere
    bak_files = list(setup_test_project.rglob("*.bak"))
    assert bak_files == []


def test_file_ops_batch_read_limit_exceeded(setup_test_project: Path):
    """Batch read with more than MAX_BATCH_READ_FILES (10) fails with INVALID_INPUT."""
    paths = [f"src/file_{i}.txt" for i in range(12)]
    res = file_ops(action="batch_read", paths=paths)

    assert res["success"] is False
    assert res["error_code"] == ERR_INVALID_INPUT
    assert "limit exceeded" in res["error"].lower()


def test_file_ops_invalid_action(setup_test_project: Path):
    """Calling file_ops with unsupported action returns INVALID_INPUT."""
    res = file_ops(action="invalid_action_xyz", file_path="src/app.py")
    assert res["success"] is False
    assert res["error_code"] == ERR_INVALID_ACTION
    assert "unsupported" in res["error"].lower()


def test_file_ops_telemetry_trace_recorded(setup_test_project: Path):
    """Executing file_ops appends a structured JSON record in telemetry trace log."""
    log_file = setup_test_project / ".copilot" / "logs" / "trace.jsonl"
    if log_file.exists():
        log_file.unlink()

    res = file_ops(action="read", file_path="src/app.py")
    assert res["success"] is True

    assert log_file.exists()
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    tool_traces = [entry for entry in lines if entry.get("tool_name") == "file_ops"]
    assert len(tool_traces) >= 1
    assert tool_traces[-1]["status"] == "success"
    assert "duration_ms" in tool_traces[-1]
