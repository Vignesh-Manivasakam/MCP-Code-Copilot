"""
Empirical Adversarial Stress Test Suite for File Operations Poly-Tool (tests/test_adversarial_file_ops.py)
========================================================================================================
Authored by: Challenger M4-Fix-1
Poly-Tool Target: copilot/tools/file_ops.py

Adversarial Stress Test Matrix:
1. CRLF Preservation:
   - Single-line target with multi-line LF replacement
   - Multi-line target with single-line replacement
   - Multi-line target with multi-line mixed CRLF/LF replacement
   - Multi-byte UTF-8, CJK, Cyrillic, RTL, mathematical symbols, and emojis in CRLF files
   - Pure LF file preservation when patched with CRLF input
   - Indentation-insensitive Strategy 3 matching with CRLF and unicode
2. Multi-File Atomic Transactions & Failure Injection:
   - Injection at File 1 of N in Step 2a: 100% disk rollback, 0 temp files, 0 HistoryDB records
   - Injection at File N/2 of N in Step 2a: 100% disk rollback, 0 temp files, 0 HistoryDB records
   - Injection at File N of N in Step 2a: 100% disk rollback, 0 temp files, 0 HistoryDB records
   - Injection in Step 2b (HistoryDB record_edit): Rollback restores disk and purges SQLite records
3. Stale Read Detection & False Positive Immunity:
   - Equal mtime with modified content -> ERR_STALE_READ
   - Earlier mtime (clock skew / backdated) with modified content -> ERR_STALE_READ
   - Sub-second mtime change with modified content -> ERR_STALE_READ
   - Identical content with changed mtime (touch/utime) -> SUCCEEDS (NO false positive)
   - Multi-file transaction pre-flight stale read detection under equal mtime
   - Multi-file transaction pre-flight untouched content with bumped mtime -> SUCCEEDS
4. Disk Hygiene & Invariant Verification:
   - Zero .bak files created under any conditions
   - Zero orphaned .tmp_tx_* temporary files
"""
from __future__ import annotations

import hashlib
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
# Helper Verification Functions
# ==============================================================================

def assert_strict_crlf(file_path: Path):
    """Verify that every line in the file ends with \\r\\n and zero lone \\n or \\r exist."""
    data = file_path.read_bytes()
    assert b"\r\n" in data, f"File {file_path} is missing \\r\\n line endings"
    assert b"\r\r\n" not in data, f"File {file_path} contains corrupted \\r\\r\\n line endings"
    lines = data.split(b"\r\n")
    # All segments except the possible trailing empty one after the final \r\n
    for idx, seg in enumerate(lines[:-1]):
        assert b"\n" not in seg, f"Found lone \\n in line {idx} of {file_path}: {seg}"
        assert b"\r" not in seg, f"Found lone \\r in line {idx} of {file_path}: {seg}"


def assert_strict_lf(file_path: Path):
    """Verify that the file uses pure \\n without any \\r or \\r\\n."""
    data = file_path.read_bytes()
    assert b"\r\n" not in data, f"File {file_path} contains unexpected \\r\\n line endings"
    assert b"\r" not in data, f"File {file_path} contains carriage return \\r"
    assert b"\n" in data, f"File {file_path} does not contain expected \\n line endings"


def assert_zero_bak_and_tmp(root_dir: Path):
    """Verify zero .bak and zero .tmp_tx_* files exist anywhere in the directory."""
    bak_files = list(root_dir.rglob("*.bak"))
    assert bak_files == [], f"Found orphaned .bak files: {bak_files}"
    tmp_files = list(root_dir.rglob(".tmp_tx_*"))
    assert tmp_files == [], f"Found orphaned .tmp_tx_* files: {tmp_files}"


# ==============================================================================
# GROUP 1: CRLF Preservation Adversarial Tests
# ==============================================================================

def test_crlf_preservation_single_line_target_multiline_lf_replacement(setup_test_project: Path):
    """
    Empirical test: CRLF file where target is a single line, and replacement has 4 lines with LF.
    Verify that all inserted replacement lines adopt CRLF, and zero lone LF remains.
    """
    f = setup_test_project / "src" / "crlf_target1.py"
    f.write_bytes(b"def compute():\r\n    val = 1\r\n    return val\r\n")

    res = file_ops(
        action="patch",
        file_path="src/crlf_target1.py",
        target="val = 1",
        replacement="a = 10\n    b = 20\n    c = 30\n    val = a + b + c",
    )

    assert res["success"] is True, f"Patch failed: {res}"
    assert_strict_crlf(f)
    content = f.read_text(encoding="utf-8")
    assert "val = a + b + c" in content
    assert_zero_bak_and_tmp(setup_test_project)


def test_crlf_preservation_multiline_target_single_line_replacement(setup_test_project: Path):
    """
    Empirical test: CRLF file where target spans multiple lines, replaced with a single line.
    Verify that surrounding lines and the modified block strictly preserve CRLF.
    """
    f = setup_test_project / "src" / "crlf_target_multi.py"
    f.write_bytes(
        b"header = 'init'\r\n"
        b"line_to_remove_1 = True\r\n"
        b"line_to_remove_2 = False\r\n"
        b"line_to_remove_3 = None\r\n"
        b"footer = 'done'\r\n"
    )

    res = file_ops(
        action="patch",
        file_path="src/crlf_target_multi.py",
        target="line_to_remove_1 = True\nline_to_remove_2 = False\nline_to_remove_3 = None",
        replacement="consolidated_flag = True",
    )

    assert res["success"] is True, f"Patch failed: {res}"
    assert_strict_crlf(f)
    content = f.read_text(encoding="utf-8")
    assert "consolidated_flag = True" in content
    assert "line_to_remove_2" not in content
    assert_zero_bak_and_tmp(setup_test_project)


def test_crlf_preservation_multiline_target_multiline_replacement_mixed_endings(setup_test_project: Path):
    """
    Empirical test: Multi-line target and multi-line replacement containing mixed \\r\\n and \\n.
    Verify all replaced lines strictly normalize to CRLF matching the file's newline format.
    """
    f = setup_test_project / "src" / "crlf_mixed_rep.py"
    f.write_bytes(
        b"def setup_config():\r\n"
        b"    cfg_host = 'localhost'\r\n"
        b"    cfg_port = 8080\r\n"
        b"    return cfg_host, cfg_port\r\n"
    )

    target_block = "cfg_host = 'localhost'\r\n    cfg_port = 8080"
    # Replacement has mixed CRLF and LF lines
    replacement_block = "cfg_host = '0.0.0.0'\n    cfg_port = 9000\r\n    cfg_tls = True"

    res = file_ops(
        action="patch",
        file_path="src/crlf_mixed_rep.py",
        target=target_block,
        replacement=replacement_block,
    )

    assert res["success"] is True, f"Patch failed: {res}"
    assert_strict_crlf(f)
    content = f.read_text(encoding="utf-8")
    assert "cfg_tls = True" in content
    assert_zero_bak_and_tmp(setup_test_project)


def test_crlf_preservation_unicode_and_emojis(setup_test_project: Path):
    """
    Empirical test: CRLF file containing multi-byte UTF-8, Chinese, Cyrillic, Arabic,
    mathematical symbols, and emojis (🚀, 🔥, 🐍).
    Verify encoding integrity and strict CRLF preservation across replacement.
    """
    f = setup_test_project / "src" / "unicode_crlf.py"
    initial_text = (
        "# -*- coding: utf-8 -*-\r\n"
        "# 欢迎使用 Code Copilot 🚀\r\n"
        "# Привет мир! שלום עולם\r\n"
        "class UnicodeAgent:\r\n"
        "    formula = '∑(x_i) = ∫ f(t) dt'\r\n"
        "    greeting = 'Hello 🌍'\r\n"
        "    status = 'PENDING ⏳'\r\n"
    )
    f.write_bytes(initial_text.encode("utf-8"))

    # Replace with emojis and CJK using LF newlines
    replacement_text = (
        "    greeting = '你好世界 🌏'\n"
        "    status = 'ACTIVE 🚀🔥✨'\n"
        "    notes = 'Empirical testing: 🦀 + 🐍 = ⚡'"
    )

    res = file_ops(
        action="patch",
        file_path="src/unicode_crlf.py",
        target="    greeting = 'Hello 🌍'\r\n    status = 'PENDING ⏳'",
        replacement=replacement_text,
    )

    assert res["success"] is True, f"Patch failed: {res}"
    assert_strict_crlf(f)

    # Re-read and verify unicode representation bit-for-bit
    content = f.read_text(encoding="utf-8")
    assert "你好世界 🌏" in content
    assert "ACTIVE 🚀🔥✨" in content
    assert "🦀 + 🐍 = ⚡" in content
    assert "∑(x_i) = ∫ f(t) dt" in content
    assert_zero_bak_and_tmp(setup_test_project)


def test_lf_preservation_when_patched_with_crlf(setup_test_project: Path):
    """
    Empirical test: Unix LF file patched with CRLF target and replacement.
    Verify file remains pure LF without any carriage return \\r introduced.
    """
    f = setup_test_project / "src" / "unix_lf.py"
    f.write_bytes(b"def unix_func():\n    version = 1\n    return version\n")

    res = file_ops(
        action="patch",
        file_path="src/unix_lf.py",
        target="version = 1\r\n    return version",
        replacement="version = 2\r\n    updated = True\r\n    return version",
    )

    assert res["success"] is True, f"Patch failed: {res}"
    assert_strict_lf(f)
    content = f.read_text(encoding="utf-8")
    assert "version = 2" in content
    assert "updated = True" in content
    assert_zero_bak_and_tmp(setup_test_project)


def test_crlf_strategy3_indentation_insensitive_preserves_crlf(setup_test_project: Path):
    """
    Empirical test: CRLF file matching via Strategy 3 (indentation insensitive).
    Verify that re-indented lines correctly preserve CRLF and zero lone LF exists.
    """
    f = setup_test_project / "src" / "crlf_indent.py"
    f.write_bytes(
        b"class DeepWorker:\r\n"
        b"    def execute(self):\r\n"
        b"        if True:\r\n"
        b"            flag = 'start'\r\n"
        b"            score = 100\r\n"
        b"            return score\r\n"
    )

    # Search block has 2 spaces indent and LF endings
    search_block = "if True:\n  flag = 'start'\n  score = 100\n  return score"
    replacement_block = "if True:\n  flag = 'updated'\n  score = 200\n  return score"

    res = file_ops(
        action="patch",
        file_path="src/crlf_indent.py",
        target=search_block,
        replacement=replacement_block,
    )

    assert res["success"] is True, f"Patch failed: {res}"
    assert_strict_crlf(f)
    content = f.read_text(encoding="utf-8")
    assert "            flag = 'updated'" in content
    assert "            score = 200" in content
    assert_zero_bak_and_tmp(setup_test_project)


# ==============================================================================
# GROUP 2: Transaction Failure Injection at File 1, File N/2, File N
# ==============================================================================

def test_stress_transaction_failure_injection_at_file_1(setup_test_project: Path):
    """
    Stress test: Transaction touching 6 files (writes, deletes, patches, creates).
    Failure injected at File 1 in Phase 2 during atomic replace.
    Verify:
    1. 100% rollback of files on disk
    2. ZERO orphaned .tmp_tx_* files
    3. ZERO phantom records in SQLite HistoryDB
    """
    db = HistoryDB(setup_test_project / ".copilot" / "history.db")
    initial_db_count = db.count()

    files = {}
    for i in range(1, 7):
        p = setup_test_project / "src" / f"tx_inj1_file{i}.txt"
        if i != 2 and i != 6:  # files 2 and 6 will be new creations
            p.write_text(f"initial content {i}\n", encoding="utf-8")
            files[i] = (p, f"initial content {i}\n")
        else:
            files[i] = (p, None)

    edits = [
        {"action": "write", "file_path": f"src/tx_inj1_file1.txt", "content": "mod 1\n"},
        {"action": "write", "file_path": f"src/tx_inj1_file2.txt", "content": "created 2\n"},
        {"action": "patch", "file_path": f"src/tx_inj1_file3.txt", "target": "initial content 3", "replacement": "mod 3"},
        {"action": "delete", "file_path": f"src/tx_inj1_file4.txt"},
        {"action": "write", "file_path": f"src/tx_inj1_file5.txt", "content": "mod 5\n"},
        {"action": "write", "file_path": f"src/tx_inj1_file6.txt", "content": "created 6\n"},
    ]

    original_replace = os.replace

    def mock_replace(src, dst):
        if "tx_inj1_file1" in str(dst):
            raise OSError("Injected I/O error on File 1 (first file)")
        return original_replace(src, dst)

    with patch("os.replace", side_effect=mock_replace):
        res = file_ops(action="transaction", edits=edits)

    assert res["success"] is False
    assert res["rolled_back"] is True
    assert res["error_code"] == ERR_INTERNAL

    # 1. Verify 100% rollback on disk
    assert files[1][0].read_text(encoding="utf-8") == "initial content 1\n"
    assert not files[2][0].exists(), "Created file 2 was not rolled back!"
    assert files[3][0].read_text(encoding="utf-8") == "initial content 3\n"
    assert files[4][0].exists() and files[4][0].read_text(encoding="utf-8") == "initial content 4\n"
    assert files[5][0].read_text(encoding="utf-8") == "initial content 5\n"
    assert not files[6][0].exists(), "Created file 6 was not rolled back!"

    # 2. ZERO orphaned .tmp_tx_* files
    assert_zero_bak_and_tmp(setup_test_project)

    # 3. ZERO phantom records in SQLite HistoryDB
    final_db_count = db.count()
    assert final_db_count == initial_db_count, f"History DB leaked records: {final_db_count - initial_db_count}"


def test_stress_transaction_failure_injection_at_file_n_half(setup_test_project: Path):
    """
    Stress test: Transaction touching 6 files.
    Files 1 and 2 succeed in Phase 2 on disk.
    Failure injected at File 3 (index N/2) in Phase 2.
    Verify:
    1. 100% rollback on disk: File 1 restored, File 2 unlinked, Files 3-6 intact
    2. ZERO orphaned .tmp_tx_* files
    3. ZERO phantom records in SQLite HistoryDB
    """
    db = HistoryDB(setup_test_project / ".copilot" / "history.db")
    initial_db_count = db.count()

    f1 = setup_test_project / "src" / "tx_inj3_f1.txt"
    f1.write_text("orig f1\n", encoding="utf-8")

    f2 = setup_test_project / "src" / "tx_inj3_f2.txt"  # New file

    f3 = setup_test_project / "src" / "tx_inj3_f3.txt"
    f3.write_text("orig f3\n", encoding="utf-8")

    f4 = setup_test_project / "src" / "tx_inj3_f4.txt"
    f4.write_text("orig f4\n", encoding="utf-8")

    f5 = setup_test_project / "src" / "tx_inj3_f5.txt"
    f5.write_text("orig f5\n", encoding="utf-8")

    f6 = setup_test_project / "src" / "tx_inj3_f6.txt"  # New file

    edits = [
        {"action": "write", "file_path": "src/tx_inj3_f1.txt", "content": "modified f1\n"},
        {"action": "write", "file_path": "src/tx_inj3_f2.txt", "content": "created f2\n"},
        {"action": "write", "file_path": "src/tx_inj3_f3.txt", "content": "modified f3\n"},
        {"action": "delete", "file_path": "src/tx_inj3_f4.txt"},
        {"action": "patch", "file_path": "src/tx_inj3_f5.txt", "target": "orig f5", "replacement": "new f5"},
        {"action": "write", "file_path": "src/tx_inj3_f6.txt", "content": "created f6\n"},
    ]

    original_replace = os.replace

    def mock_replace(src, dst):
        if "tx_inj3_f3" in str(dst):
            raise OSError("Injected I/O error on File 3 (N/2)")
        return original_replace(src, dst)

    with patch("os.replace", side_effect=mock_replace):
        res = file_ops(action="transaction", edits=edits)

    assert res["success"] is False
    assert res["rolled_back"] is True
    assert res["error_code"] == ERR_INTERNAL

    # 1. 100% rollback on disk
    assert f1.read_text(encoding="utf-8") == "orig f1\n"
    assert not f2.exists()
    assert f3.read_text(encoding="utf-8") == "orig f3\n"
    assert f4.exists() and f4.read_text(encoding="utf-8") == "orig f4\n"
    assert f5.read_text(encoding="utf-8") == "orig f5\n"
    assert not f6.exists()

    # 2. ZERO orphaned .tmp_tx_* files
    assert_zero_bak_and_tmp(setup_test_project)

    # 3. ZERO phantom records in SQLite HistoryDB
    final_db_count = db.count()
    assert final_db_count == initial_db_count


def test_stress_transaction_failure_injection_at_file_n(setup_test_project: Path):
    """
    Stress test: Transaction touching 6 files.
    Files 1 through 5 succeed in Phase 2 on disk.
    Failure injected at File 6 (File N, the very last file).
    Verify:
    1. 100% rollback on disk: ALL 5 preceding modifications reversed
    2. ZERO orphaned .tmp_tx_* files
    3. ZERO phantom records in SQLite HistoryDB
    """
    db = HistoryDB(setup_test_project / ".copilot" / "history.db")
    initial_db_count = db.count()

    f1 = setup_test_project / "src" / "tx_injN_f1.txt"
    f1.write_text("orig f1\n", encoding="utf-8")

    f2 = setup_test_project / "src" / "tx_injN_f2.txt"  # New file

    f3 = setup_test_project / "src" / "tx_injN_f3.txt"
    f3.write_text("orig f3\n", encoding="utf-8")

    f4 = setup_test_project / "src" / "tx_injN_f4.txt"
    f4.write_text("orig f4\n", encoding="utf-8")

    f5 = setup_test_project / "src" / "tx_injN_f5.txt"
    f5.write_text("orig f5\n", encoding="utf-8")

    f6 = setup_test_project / "src" / "tx_injN_f6.txt"
    f6.write_text("orig f6\n", encoding="utf-8")

    edits = [
        {"action": "write", "file_path": "src/tx_injN_f1.txt", "content": "modified f1\n"},
        {"action": "write", "file_path": "src/tx_injN_f2.txt", "content": "created f2\n"},
        {"action": "delete", "file_path": "src/tx_injN_f3.txt"},
        {"action": "patch", "file_path": "src/tx_injN_f4.txt", "target": "orig f4", "replacement": "new f4"},
        {"action": "write", "file_path": "src/tx_injN_f5.txt", "content": "modified f5\n"},
        {"action": "write", "file_path": "src/tx_injN_f6.txt", "content": "modified f6\n"},
    ]

    original_replace = os.replace

    def mock_replace(src, dst):
        if "tx_injN_f6" in str(dst):
            raise OSError("Injected I/O error on File N (last file)")
        return original_replace(src, dst)

    with patch("os.replace", side_effect=mock_replace):
        res = file_ops(action="transaction", edits=edits)

    assert res["success"] is False
    assert res["rolled_back"] is True
    assert res["error_code"] == ERR_INTERNAL

    # 1. 100% rollback on disk
    assert f1.read_text(encoding="utf-8") == "orig f1\n"
    assert not f2.exists(), "f2 was not removed during rollback!"
    assert f3.exists() and f3.read_text(encoding="utf-8") == "orig f3\n", "f3 was not restored after deletion!"
    assert f4.read_text(encoding="utf-8") == "orig f4\n"
    assert f5.read_text(encoding="utf-8") == "orig f5\n"
    assert f6.read_text(encoding="utf-8") == "orig f6\n"

    # 2. ZERO orphaned .tmp_tx_* files
    assert_zero_bak_and_tmp(setup_test_project)

    # 3. ZERO phantom records in SQLite HistoryDB
    final_db_count = db.count()
    assert final_db_count == initial_db_count


def test_stress_transaction_failure_in_step2b_history_db(setup_test_project: Path):
    """
    Stress test: Step 2a succeeds completely on disk for all files, but Step 2b raises
    an exception while recording to HistoryDB on the second file.
    Verify:
    1. Disk is 100% rolled back to original states
    2. Any records inserted into HistoryDB before the crash are cleanly deleted
    3. ZERO orphaned .tmp_tx_* files
    """
    db = HistoryDB(setup_test_project / ".copilot" / "history.db")
    initial_db_count = db.count()

    f1 = setup_test_project / "src" / "tx_db_f1.txt"
    f1.write_text("orig db f1\n", encoding="utf-8")

    f2 = setup_test_project / "src" / "tx_db_f2.txt"
    f2.write_text("orig db f2\n", encoding="utf-8")

    edits = [
        {"action": "write", "file_path": "src/tx_db_f1.txt", "content": "mod db f1\n"},
        {"action": "write", "file_path": "src/tx_db_f2.txt", "content": "mod db f2\n"},
    ]

    original_record = HistoryDB.record_edit
    call_count = 0

    def mock_record_edit(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise sqlite3.OperationalError("Simulated SQLite lock error during tx commit")
        return original_record(self, *args, **kwargs)

    import sqlite3
    with patch.object(HistoryDB, "record_edit", mock_record_edit):
        res = file_ops(action="transaction", edits=edits)

    assert res["success"] is False
    assert res["rolled_back"] is True

    # 1. Disk restored
    assert f1.read_text(encoding="utf-8") == "orig db f1\n"
    assert f2.read_text(encoding="utf-8") == "orig db f2\n"

    # 2. History DB purged of partial entries
    final_db_count = db.count()
    assert final_db_count == initial_db_count, f"HistoryDB has {final_db_count - initial_db_count} leaked entries"

    # 3. Clean workspace
    assert_zero_bak_and_tmp(setup_test_project)


# ==============================================================================
# GROUP 3: Stale Read Detection & False Positive Immunity
# ==============================================================================

def test_stale_read_detection_under_equal_mtime(setup_test_project: Path):
    """
    Empirical test: Content modified on disk, but mtime set exactly equal to read timestamp.
    Verify stale read detection triggers ERR_STALE_READ based on content hash.
    """
    f = setup_test_project / "src" / "stale_equal_mtime.txt"
    f.write_text("def run():\n    return 'alpha'\n", encoding="utf-8")

    r_res = file_ops(action="read", file_path="src/stale_equal_mtime.txt")
    assert r_res["success"] is True
    orig_mtime = f.stat().st_mtime

    # External modification: change content, force mtime to match original exactly
    f.write_text("def run():\n    return 'external_modification'\n", encoding="utf-8")
    os.utime(f, (orig_mtime, orig_mtime))
    assert f.stat().st_mtime == orig_mtime

    # Attempt patch
    p_res = file_ops(
        action="patch",
        file_path="src/stale_equal_mtime.txt",
        target="return 'alpha'",
        replacement="return 'beta'",
    )

    assert p_res["success"] is False
    assert p_res["error_code"] == ERR_STALE_READ
    assert "file_hash" in p_res
    assert p_res["modified_mtime"] == orig_mtime


def test_stale_read_detection_under_earlier_mtime(setup_test_project: Path):
    """
    Empirical test: Content modified on disk, and mtime backdated to 60 seconds earlier
    (simulating clock skew or restore from older archive).
    Verify stale read detection triggers ERR_STALE_READ.
    """
    f = setup_test_project / "src" / "stale_earlier_mtime.txt"
    f.write_text("config_version = 1\n", encoding="utf-8")

    r_res = file_ops(action="read", file_path="src/stale_earlier_mtime.txt")
    assert r_res["success"] is True
    orig_mtime = f.stat().st_mtime

    # External modification with backdated mtime
    earlier_mtime = orig_mtime - 60.0
    f.write_text("config_version = 99\n", encoding="utf-8")
    os.utime(f, (earlier_mtime, earlier_mtime))
    assert f.stat().st_mtime < orig_mtime

    p_res = file_ops(
        action="patch",
        file_path="src/stale_earlier_mtime.txt",
        target="config_version = 1",
        replacement="config_version = 2",
    )

    assert p_res["success"] is False
    assert p_res["error_code"] == ERR_STALE_READ


def test_stale_read_detection_under_sub_second_mtime(setup_test_project: Path):
    """
    Empirical test: Rapid sub-second external modification where disk mtime changes
    by less than 1 millisecond.
    Verify stale read detection catches the modification.
    """
    f = setup_test_project / "src" / "stale_subsecond.txt"
    f.write_text("threshold = 100\n", encoding="utf-8")

    r_res = file_ops(action="read", file_path="src/stale_subsecond.txt")
    assert r_res["success"] is True
    orig_mtime = f.stat().st_mtime

    # Tiny sub-second bump (0.00001 sec)
    subsecond_mtime = orig_mtime + 0.00001
    f.write_text("threshold = 999\n", encoding="utf-8")
    os.utime(f, (subsecond_mtime, subsecond_mtime))

    p_res = file_ops(
        action="patch",
        file_path="src/stale_subsecond.txt",
        target="threshold = 100",
        replacement="threshold = 200",
    )

    assert p_res["success"] is False
    assert p_res["error_code"] == ERR_STALE_READ


def test_stale_read_no_false_positive_when_content_identical(setup_test_project: Path):
    """
    Empirical test: File was read, external tool touched the file (bumping mtime by 10s),
    but content was UNCHANGED.
    Verify that patch does NOT raise ERR_STALE_READ and successfully applies!
    """
    f = setup_test_project / "src" / "identical_touch.txt"
    original_text = "def calculate():\n    x = 10\n    return x\n"
    f.write_text(original_text, encoding="utf-8")

    r_res = file_ops(action="read", file_path="src/identical_touch.txt")
    assert r_res["success"] is True
    orig_mtime = f.stat().st_mtime

    # Touch file: Bump mtime by 15 seconds without altering content
    new_mtime = orig_mtime + 15.0
    os.utime(f, (new_mtime, new_mtime))
    assert f.stat().st_mtime > orig_mtime
    assert f.read_text(encoding="utf-8") == original_text

    # Attempt patch
    p_res = file_ops(
        action="patch",
        file_path="src/identical_touch.txt",
        target="x = 10",
        replacement="x = 42",
    )

    assert p_res["success"] is True, f"Unexpected false positive stale read: {p_res}"
    assert "x = 42" in f.read_text(encoding="utf-8")
    assert_zero_bak_and_tmp(setup_test_project)


def test_transaction_stale_read_under_equal_mtime(setup_test_project: Path):
    """
    Empirical test: Multi-file transaction pre-flight stale read detection
    when an external process changed content of one file but preserved its mtime.
    Verify transaction is aborted in pre-flight without touching any disk files.
    """
    f1 = setup_test_project / "src" / "tx_stale1.txt"
    f1.write_text("service_a = 'ready'\n", encoding="utf-8")

    f2 = setup_test_project / "src" / "tx_stale2.txt"
    f2.write_text("service_b = 'ready'\n", encoding="utf-8")

    file_ops(action="read", file_path="src/tx_stale1.txt")
    file_ops(action="read", file_path="src/tx_stale2.txt")
    orig_mtime2 = f2.stat().st_mtime

    # Alter f2 content externally, keep mtime identical
    f2.write_text("service_b = 'externally_stopped'\n", encoding="utf-8")
    os.utime(f2, (orig_mtime2, orig_mtime2))

    edits = [
        {"action": "patch", "file_path": "src/tx_stale1.txt", "target": "service_a = 'ready'", "replacement": "service_a = 'busy'"},
        {"action": "patch", "file_path": "src/tx_stale2.txt", "target": "service_b = 'ready'", "replacement": "service_b = 'busy'"},
    ]

    res = file_ops(action="transaction", edits=edits)

    assert res["success"] is False
    assert res["rolled_back"] is True
    assert res["error_code"] == ERR_STALE_READ

    # Verify f1 was not modified
    assert f1.read_text(encoding="utf-8") == "service_a = 'ready'\n"
    assert_zero_bak_and_tmp(setup_test_project)


def test_transaction_succeeds_when_mtime_changed_but_content_identical(setup_test_project: Path):
    """
    Empirical test: Multi-file transaction where files were read, and external touch
    bumped mtime, but content is bit-for-bit identical.
    Verify transaction SUCCEEDS and applies changes without false positive rejection.
    """
    f1 = setup_test_project / "src" / "tx_touch1.txt"
    f1.write_text("module_a = 1\n", encoding="utf-8")

    f2 = setup_test_project / "src" / "tx_touch2.txt"
    f2.write_text("module_b = 2\n", encoding="utf-8")

    file_ops(action="read", file_path="src/tx_touch1.txt")
    file_ops(action="read", file_path="src/tx_touch2.txt")

    # Touch both files with +20s mtime
    t = time.time() + 20.0
    os.utime(f1, (t, t))
    os.utime(f2, (t, t))

    edits = [
        {"action": "patch", "file_path": "src/tx_touch1.txt", "target": "module_a = 1", "replacement": "module_a = 10"},
        {"action": "patch", "file_path": "src/tx_touch2.txt", "target": "module_b = 2", "replacement": "module_b = 20"},
    ]

    res = file_ops(action="transaction", edits=edits)

    assert res["success"] is True, f"Transaction unexpectedly failed: {res}"
    assert f1.read_text(encoding="utf-8") == "module_a = 10\n"
    assert f2.read_text(encoding="utf-8") == "module_b = 20\n"
    assert_zero_bak_and_tmp(setup_test_project)
