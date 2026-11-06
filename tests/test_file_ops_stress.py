"""
Stress Test Suite for File Operations Poly-Tool (tests/test_file_ops_stress.py)
==============================================================================
Empirical adversarial tests challenging:
1. Multi-file atomic transactions (Phase 1 & Phase 2 rollbacks, zero .bak files)
2. CRLF boundaries & mixed tab/space indentation on Windows files
3. Stale read conflict detection with concurrent external disk modification
4. Large batch reads (boundary limits) & binary file rejections
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
import pytest

from config import Config
from copilot.core.history import HistoryDB
from copilot.services.telemetry import (
    ERR_DIFF_MISMATCH,
    ERR_NOT_FOUND,
    ERR_STALE_READ,
)
from copilot.tools.file_ops import (
    file_ops,
    ERR_AMBIGUOUS_MATCH,
    ERR_BINARY_FILE,
    ERR_FILE_TOO_LARGE,
    ERR_INTERNAL,
    ERR_INVALID_ACTION,
    ERR_INVALID_INPUT,
)


# ==============================================================================
# Area 1: Multi-File Atomic Transactions & Rollback Stress Tests
# ==============================================================================

def test_stress_transaction_5_files_staged_5th_fails_phase1(setup_test_project: Path):
    """
    Stress test 1.1: 5 files staged in transaction; 5th file fails in Phase 1 (pre-flight).
    Verify files 1-4 are completely untouched, file 2 (staged new) was never created,
    file 3 (staged delete) still exists, zero .bak files created.
    """
    f1 = setup_test_project / "src" / "file1.txt"
    f1.write_text("original file 1\n", encoding="utf-8")

    f2 = setup_test_project / "src" / "file2_created.txt"
    assert not f2.exists()

    f3 = setup_test_project / "src" / "file3_deleted.txt"
    f3.write_text("file 3 to be deleted\n", encoding="utf-8")

    f4 = setup_test_project / "src" / "file4_patched.txt"
    f4.write_text("def target_function():\n    return 'old'\n", encoding="utf-8")

    f5 = setup_test_project / "src" / "file5_failing.txt"
    f5.write_text("file 5 content\n", encoding="utf-8")

    # Staged 5 operations where 5th operation fails (missing patch target)
    edits = [
        {"action": "write", "file_path": "src/file1.txt", "content": "modified file 1\n"},
        {"action": "write", "file_path": "src/file2_created.txt", "content": "staged new file 2\n"},
        {"action": "delete", "file_path": "src/file3_deleted.txt"},
        {
            "action": "patch",
            "file_path": "src/file4_patched.txt",
            "target": "return 'old'",
            "replacement": "return 'new'",
        },
        {
            "action": "patch",
            "file_path": "src/file5_failing.txt",
            "target": "NON_EXISTENT_TARGET_STRING_XYZ",
            "replacement": "replacement",
        },
    ]

    res = file_ops(action="transaction", edits=edits)

    assert res["success"] is False
    assert res["rolled_back"] is True
    assert res["error_code"] == ERR_DIFF_MISMATCH

    assert f1.read_text(encoding="utf-8") == "original file 1\n"
    assert not f2.exists()
    assert f3.read_text(encoding="utf-8") == "file 3 to be deleted\n"
    assert f4.read_text(encoding="utf-8") == "def target_function():\n    return 'old'\n"
    assert f5.read_text(encoding="utf-8") == "file 5 content\n"

    # Invariant: Zero .bak files created anywhere in workspace
    bak_files = list(setup_test_project.rglob("*.bak"))
    assert bak_files == [], f"Unexpected .bak files: {bak_files}"

    # Invariant: Zero temp files left behind
    tmp_files = list(setup_test_project.rglob(".tmp_tx_*"))
    assert tmp_files == [], f"Unexpected temp transaction files: {tmp_files}"


def test_stress_transaction_5_files_staged_5th_fails_phase2_disk_restored(setup_test_project: Path):
    """
    Stress test 1.2: 5 files pass Phase 1 simulation; during Phase 2 disk write,
    5th file raises an OSError/IOError.
    Verify Phase 2 rollback restores files 1-4 to their pre-transaction disk state,
    file 2 is unlinked, file 3 is restored, zero .bak files created.
    """
    f1 = setup_test_project / "src" / "f1.txt"
    f1.write_text("original f1\n", encoding="utf-8")

    f2 = setup_test_project / "src" / "f2_new.txt"

    f3 = setup_test_project / "src" / "f3_del.txt"
    f3.write_text("original f3\n", encoding="utf-8")

    f4 = setup_test_project / "src" / "f4_patch.txt"
    f4.write_text("original f4 code\n", encoding="utf-8")

    f5 = setup_test_project / "src" / "f5_fail.txt"
    f5.write_text("original f5\n", encoding="utf-8")

    edits = [
        {"action": "write", "file_path": "src/f1.txt", "content": "modified f1\n"},
        {"action": "write", "file_path": "src/f2_new.txt", "content": "modified f2\n"},
        {"action": "delete", "file_path": "src/f3_del.txt"},
        {"action": "patch", "file_path": "src/f4_patch.txt", "target": "original f4 code", "replacement": "new f4 code"},
        {"action": "write", "file_path": "src/f5_fail.txt", "content": "modified f5\n"},
    ]

    original_replace = os.replace

    def mock_replace(src, dst):
        if "f5_fail" in str(dst):
            raise OSError("Simulated I/O failure on file 5 during atomic rename")
        return original_replace(src, dst)

    with patch("os.replace", side_effect=mock_replace):
        res = file_ops(action="transaction", edits=edits)

    assert res["success"] is False
    assert res["rolled_back"] is True
    assert res["error_code"] == ERR_INTERNAL

    assert f1.read_text(encoding="utf-8") == "original f1\n"
    assert not f2.exists()
    assert f3.read_text(encoding="utf-8") == "original f3\n"
    assert f4.read_text(encoding="utf-8") == "original f4 code\n"
    assert f5.read_text(encoding="utf-8") == "original f5\n"

    # Zero .bak files
    bak_files = list(setup_test_project.rglob("*.bak"))
    assert bak_files == []


def test_stress_transaction_phase2_failure_leaves_orphaned_temp_and_history(setup_test_project: Path):
    """
    Stress test 1.3: Phase 2 failure edge case:
    Verifies whether orphaned temp files (.tmp_tx_*) or uncommitted history records remain.
    """
    db = HistoryDB(setup_test_project / ".copilot" / "history.db")
    initial_history_count = db.count()

    f1 = setup_test_project / "src" / "tf1.txt"
    f1.write_text("orig tf1\n", encoding="utf-8")
    f2 = setup_test_project / "src" / "tf2.txt"
    f2.write_text("orig tf2\n", encoding="utf-8")

    original_replace = os.replace

    def mock_replace(src, dst):
        if "tf2" in str(dst):
            raise OSError("Disk write failed on file 2")
        return original_replace(src, dst)

    with patch("os.replace", side_effect=mock_replace):
        res = file_ops(
            action="transaction",
            edits=[
                {"action": "write", "file_path": "src/tf1.txt", "content": "mod tf1\n"},
                {"action": "write", "file_path": "src/tf2.txt", "content": "mod tf2\n"},
            ],
        )

    assert res["success"] is False
    assert res["rolled_back"] is True

    # Check for orphaned temp files
    orphans = list(setup_test_project.rglob(".tmp_tx_*"))
    assert orphans == [], f"Found orphaned temp files after aborted transaction: {orphans}"

    # Check history db count was not incremented for aborted transaction
    final_history_count = db.count()
    assert final_history_count == initial_history_count, f"History DB retained {final_history_count - initial_history_count} phantom records from aborted transaction"


def test_stress_transaction_multiple_ops_same_file(setup_test_project: Path):
    """
    Stress test 1.4: Transaction executing sequential modifications on the SAME file.
    """
    file_path = "src/multi_op.py"
    edits = [
        {
            "action": "write",
            "file_path": file_path,
            "content": "def func():\n    step = 1\n    return step\n",
        },
        {
            "action": "patch",
            "file_path": file_path,
            "target": "step = 1",
            "replacement": "step = 2",
        },
    ]

    res = file_ops(action="transaction", edits=edits)
    assert res["success"] is True
    disk_content = (setup_test_project / file_path).read_text(encoding="utf-8")
    assert "step = 2" in disk_content
    assert "step = 1" not in disk_content


def test_stress_transaction_invalid_inputs(setup_test_project: Path):
    """Stress test 1.5: Transaction robustness against malformed/invalid inputs."""
    r1 = file_ops(action="transaction", edits=None)
    assert r1["success"] is False
    assert r1["error_code"] == ERR_INVALID_INPUT

    r2 = file_ops(action="transaction", edits=[])
    assert r2["success"] is False
    assert r2["error_code"] == ERR_INVALID_INPUT

    r3 = file_ops(action="transaction", edits=["not a dict"])
    assert r3["success"] is False
    assert r3["error_code"] == ERR_INVALID_INPUT

    r4 = file_ops(action="transaction", edits=[{"action": "write", "content": "abc"}])
    assert r4["success"] is False
    assert r4["error_code"] == ERR_INVALID_INPUT

    r5 = file_ops(action="transaction", edits=[{"action": "unsupported_action", "file_path": "src/app.py"}])
    assert r5["success"] is False
    assert r5["error_code"] == ERR_INVALID_INPUT


# ==============================================================================
# Area 2: CRLF Boundaries & Mixed Tab/Space Indentation Stress Tests
# ==============================================================================

def test_stress_crlf_file_patched_with_lf_multiline(setup_test_project: Path):
    """
    Stress test 2.1: CRLF file on Windows patched with multi-line target/replacement
    using Unix LF line endings.
    Invariant: Output on disk must maintain clean \r\n endings without \r\r\n or lone \n.
    """
    crlf_file = setup_test_project / "src" / "crlf_multi.py"
    original_bytes = b"def calculate():\r\n    x = 10\r\n    y = 20\r\n    return x + y\r\n"
    crlf_file.write_bytes(original_bytes)

    lf_target = "def calculate():\n    x = 10\n    y = 20\n    return x + y"
    lf_replacement = "def calculate():\n    x = 100\n    y = 200\n    return x + y"

    res = file_ops(
        action="patch",
        file_path="src/crlf_multi.py",
        target=lf_target,
        replacement=lf_replacement,
    )

    assert res["success"] is True
    data = crlf_file.read_bytes()

    assert b"\r\r\n" not in data, "Corrupted \\r\\r\\n line endings found!"
    assert b"\r\n" in data, "Expected \\r\\n line endings missing!"

    lines_raw = data.split(b"\r\n")
    for idx, segment in enumerate(lines_raw[:-1]):
        assert b"\n" not in segment, f"Lone \\n found in CRLF file line {idx}: {segment}"
        assert b"\r" not in segment, f"Lone \\r found in CRLF file line {idx}: {segment}"


def test_stress_crlf_file_single_line_target_multiline_lf_replacement(setup_test_project: Path):
    """
    Stress test 2.2: CRLF file where target is a SINGLE LINE without newlines,
    and replacement has MULTIPLE LINES with LF line endings.
    Tests Strategy 1 vs CRLF preservation: replacement lines must adopt CRLF.
    """
    crlf_file = setup_test_project / "src" / "crlf_single.py"
    crlf_file.write_bytes(b"def process():\r\n    result = 0\r\n    return result\r\n")

    res = file_ops(
        action="patch",
        file_path="src/crlf_single.py",
        target="result = 0",
        replacement="a = 10\n    b = 20\n    result = a + b",
    )

    assert res["success"] is True
    data = crlf_file.read_bytes()

    assert b"\r\r\n" not in data
    # Verify whether lines within replacement also have \r\n
    lines = data.split(b"\r\n")
    for idx, seg in enumerate(lines[:-1]):
        assert b"\n" not in seg, f"Found lone \\n in line {idx}: {seg}"


def test_stress_tab_indented_file_patched_with_space_indentation(setup_test_project: Path):
    """
    Stress test 2.3: File uses tabs (\t) for indentation; caller sends spaces.
    Verification of indentation scaling and syntactic validity.
    """
    tab_file = setup_test_project / "src" / "tabbed.py"
    tab_content = (
        "def compute_tab():\n"
        "\ta = 1\n"
        "\tb = 2\n"
        "\treturn a + b\n"
    )
    tab_file.write_text(tab_content, encoding="utf-8")

    search_block = "def compute_tab():\n    a = 1\n    b = 2\n    return a + b"
    replacement_block = "def compute_tab():\n    a = 10\n    b = 20\n    return a + b"

    res = file_ops(
        action="patch",
        file_path="src/tabbed.py",
        target=search_block,
        replacement=replacement_block,
    )

    assert res["success"] is True
    updated = tab_file.read_text(encoding="utf-8")
    assert "a = 10" in updated
    assert "b = 20" in updated


def test_stress_nested_indentation_scaling(setup_test_project: Path):
    """
    Stress test 2.4: Deeply nested blocks (e.g. 8 spaces base) scaled appropriately.
    """
    nested_file = setup_test_project / "src" / "nested.py"
    nested_content = (
        "class Outer:\n"
        "    def method(self):\n"
        "        if True:\n"
        "            x = 1\n"
        "            y = 2\n"
        "            return x + y\n"
    )
    nested_file.write_text(nested_content, encoding="utf-8")

    search_block = "if True:\n  x = 1\n  y = 2\n  return x + y"
    replacement_block = "if True:\n  x = 5\n  y = 10\n  return x + y"

    res = file_ops(
        action="patch",
        file_path="src/nested.py",
        target=search_block,
        replacement=replacement_block,
    )

    assert res["success"] is True
    updated = nested_file.read_text(encoding="utf-8")
    assert "            x = 5" in updated
    assert "            y = 10" in updated


# ==============================================================================
# Area 3: Stale Read Conflict Detection with Concurrent Disk Modification
# ==============================================================================

def test_stress_stale_read_concurrent_disk_modification(setup_test_project: Path):
    """
    Stress test 3.1: Sequential read registers mtime/hash; external process modifies file;
    patch must detect stale read and reject with ERR_STALE_READ.
    """
    f = setup_test_project / "src" / "concurrent.txt"
    f.write_text("line 1\nline 2\n", encoding="utf-8")

    r_res = file_ops(action="read", file_path="src/concurrent.txt")
    assert r_res["success"] is True

    time.sleep(0.02)
    f.write_text("line 1\nline 2 MODIFIED BY EXTERNAL PROCESS\n", encoding="utf-8")

    p_res = file_ops(
        action="patch",
        file_path="src/concurrent.txt",
        target="line 2",
        replacement="line 2 tool edit",
    )

    assert p_res["success"] is False
    assert p_res["error_code"] == ERR_STALE_READ
    assert "file_hash" in p_res
    assert "modified_mtime" in p_res


def test_stress_stale_read_inside_transaction(setup_test_project: Path):
    """
    Stress test 3.2: Stale read detection during a multi-file transaction pre-flight.
    """
    f1 = setup_test_project / "src" / "tx_stale.txt"
    f1.write_text("initial tx stale\n", encoding="utf-8")

    file_ops(action="read", file_path="src/tx_stale.txt")

    time.sleep(0.02)
    f1.write_text("externally altered tx stale\n", encoding="utf-8")

    edits = [
        {
            "action": "patch",
            "file_path": "src/tx_stale.txt",
            "target": "initial tx stale",
            "replacement": "new tx text",
        }
    ]

    res = file_ops(action="transaction", edits=edits)
    assert res["success"] is False
    assert res["rolled_back"] is True
    assert res["error_code"] == ERR_STALE_READ


def test_stress_stale_read_content_changed_same_or_lower_mtime(setup_test_project: Path):
    """
    Stress test 3.3: Content was changed on disk, but mtime was preserved or sub-millisecond.
    Verifies whether stale read detection is triggered when content hash differs even if mtime == reg_mtime.
    """
    f = setup_test_project / "src" / "mtime_tricky.txt"
    f.write_text("header = 1\ncontent v1\n", encoding="utf-8")

    r_res = file_ops(action="read", file_path="src/mtime_tricky.txt")
    assert r_res["success"] is True
    reg_mtime = f.stat().st_mtime

    # External modification: header changes, content v1 remains, mtime preserved
    f.write_text("header = 999\ncontent v1\n", encoding="utf-8")
    os.utime(f, (reg_mtime, reg_mtime))

    p_res = file_ops(
        action="patch",
        file_path="src/mtime_tricky.txt",
        target="content v1",
        replacement="content v1 patched",
    )

    # Must detect stale read because content changed on disk since read
    assert p_res["success"] is False
    assert p_res["error_code"] == ERR_STALE_READ, f"Expected ERR_STALE_READ but got {p_res}"


def test_stress_no_false_positive_stale_read(setup_test_project: Path):
    """
    Stress test 3.4: File read at T0, un-modified externally, patched at T1.
    Must succeed cleanly without false positive ERR_STALE_READ.
    """
    f = setup_test_project / "src" / "clean_edit.txt"
    f.write_text("initial text\n", encoding="utf-8")

    r_res = file_ops(action="read", file_path="src/clean_edit.txt")
    assert r_res["success"] is True

    time.sleep(0.01)
    p_res = file_ops(
        action="patch",
        file_path="src/clean_edit.txt",
        target="initial text",
        replacement="updated text",
    )
    assert p_res["success"] is True
    assert f.read_text(encoding="utf-8") == "updated text\n"


# ==============================================================================
# Area 4: Batch Reads & Binary File Rejection Stress Tests
# ==============================================================================

def test_stress_batch_read_exact_limit_10(setup_test_project: Path):
    """
    Stress test 4.1: Batch read with exactly 10 files (maximum boundary).
    Must return success=True, files_read=10, failed_count=0.
    """
    paths = []
    for i in range(10):
        p = setup_test_project / "src" / f"batch_{i}.txt"
        p.write_text(f"content of file {i}\n", encoding="utf-8")
        paths.append(f"src/batch_{i}.txt")

    res = file_ops(action="batch_read", paths=paths)
    assert res["success"] is True
    assert res["total_requested"] == 10
    assert res["files_read"] == 10
    assert res["failed_count"] == 0
    assert len(res["files"]) == 10


def test_stress_batch_read_exceeding_limit_11(setup_test_project: Path):
    """
    Stress test 4.2: Batch read with 11 files (1 over limit).
    Must return success=False, error_code=ERR_INVALID_INPUT.
    """
    paths = [f"src/file_{i}.txt" for i in range(11)]
    res = file_ops(action="batch_read", paths=paths)
    assert res["success"] is False
    assert res["error_code"] == ERR_INVALID_INPUT
    assert "limit exceeded" in res["error"].lower()


def test_stress_batch_read_mixed_binary_and_text(setup_test_project: Path):
    """
    Stress test 4.3: Batch read containing a mix of valid text, binary, and missing files.
    Batch read overall succeeds with per-file status correctly categorized.
    """
    t_file = setup_test_project / "src" / "valid_text.txt"
    t_file.write_text("clean text\n", encoding="utf-8")

    b_file = setup_test_project / "src" / "data.dat"
    b_file.write_bytes(b"\x00\x00\x01\x02\x03\xff")

    paths = ["src/valid_text.txt", "src/data.dat", "src/non_existent.txt"]
    res = file_ops(action="batch_read", paths=paths)

    assert res["success"] is True
    assert res["total_requested"] == 3
    assert res["files_read"] == 1
    assert res["failed_count"] == 2

    assert res["files"]["src/valid_text.txt"]["success"] is True
    assert res["files"]["src/data.dat"]["success"] is False
    assert res["files"]["src/data.dat"]["error_code"] == ERR_BINARY_FILE
    assert res["files"]["src/non_existent.txt"]["success"] is False
    assert res["files"]["src/non_existent.txt"]["error_code"] == ERR_NOT_FOUND


def test_stress_binary_file_rejections_across_actions(setup_test_project: Path):
    """
    Stress test 4.4: Ensure binary files are rejected for read and patch,
    and accurately described in info.
    """
    bin_path = setup_test_project / "src" / "image.png"
    bin_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    r = file_ops(action="read", file_path="src/image.png")
    assert r["success"] is False
    assert r["error_code"] == ERR_BINARY_FILE

    p = file_ops(action="patch", file_path="src/image.png", target="PNG", replacement="JPG")
    assert p["success"] is False
    assert p["error_code"] == ERR_BINARY_FILE

    inf = file_ops(action="info", file_path="src/image.png")
    assert inf["success"] is True
    assert inf["is_binary"] is True
    assert inf["lines"] == 0


# ==============================================================================
# Area 5: Input Validation & Boundary Condition Stress Tests
# ==============================================================================

def test_stress_empty_and_whitespace_paths(setup_test_project: Path):
    """Stress test 5.1: Rejection of empty, None, and whitespace-only file paths."""
    for action in ["read", "write", "patch", "delete", "info"]:
        r_empty = file_ops(action=action, file_path="")
        assert r_empty["success"] is False
        assert r_empty["error_code"] == ERR_INVALID_INPUT

        r_space = file_ops(action=action, file_path="   \t\n  ")
        assert r_space["success"] is False
        assert r_space["error_code"] == ERR_INVALID_INPUT


def test_stress_delete_directory_rejected(setup_test_project: Path):
    """Stress test 5.2: Attempting to delete a directory with file delete is safely rejected."""
    dir_to_del = setup_test_project / "src" / "subfolder"
    dir_to_del.mkdir()

    res = file_ops(action="delete", file_path="src/subfolder")
    assert res["success"] is False
    assert res["error_code"] == ERR_INVALID_INPUT
    assert "directory" in res["error"].lower()
    assert dir_to_del.exists()


def test_stress_file_size_limit_read(setup_test_project: Path):
    """Stress test 5.3: Reading a file exceeding Config.FILE_SIZE_MAX_READ returns ERR_FILE_TOO_LARGE."""
    f = setup_test_project / "src" / "large.txt"
    f.write_text("a" * 1024, encoding="utf-8")

    with patch.object(Config, "FILE_SIZE_MAX_READ", 500):
        res = file_ops(action="read", file_path="src/large.txt")
        assert res["success"] is False
        assert res["error_code"] == ERR_FILE_TOO_LARGE
        assert "exceeds limit" in res["error"].lower()


def test_stress_file_size_limit_write(setup_test_project: Path):
    """Stress test 5.4: Writing content exceeding Config.MAX_WRITE_SIZE returns ERR_FILE_TOO_LARGE."""
    with patch.object(Config, "MAX_WRITE_SIZE", 500):
        res = file_ops(action="write", file_path="src/oversize.txt", content="b" * 1000)
        assert res["success"] is False
        assert res["error_code"] == ERR_FILE_TOO_LARGE
        assert "exceeds limit" in res["error"].lower()
