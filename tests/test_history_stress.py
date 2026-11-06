"""
Adversarial empirical stress tests for M2 SQLite History Engine (HistoryDB).
Validates:
1. Ring buffer bound: 25 consecutive edits across multiple files stay <= 20 entries with LRU eviction.
2. Zero .bak files: Disk scanning confirms zero .bak / .backup / .tmp files exist.
3. External edit conflict detection: Protects external changes from being overwritten.
4. Windows CRLF hash integrity: Verifies bit-for-bit CRLF preservation, no \\r\\r\\n corruption,
   and benign newline translation handling.
5. Windows path handling: Mixed slashes, case-insensitivity (COLLATE NOCASE), and relative/absolute resolution.
6. File creation undo: Clean deletion on undoing initial creation.
7. High-volume stress: Rapid sequential operations without database lock errors.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import pytest

from config import Config
from copilot.core.history import HistoryDB


@pytest.fixture
def stress_workspace(tmp_path: Path):
    """Provides a fresh isolated workspace with dynamic Config.PROJECT_ROOT configured."""
    workspace = tmp_path / "stress_history_ws"
    workspace.mkdir(parents=True, exist_ok=True)
    orig_root = Config.PROJECT_ROOT
    orig_db = Config.HISTORY_DB_PATH
    Config.PROJECT_ROOT = workspace
    Config.HISTORY_DB_PATH = workspace / ".copilot" / "history.db"

    db = HistoryDB(db_path=Config.HISTORY_DB_PATH)
    db.clear()

    yield workspace, db

    Config.PROJECT_ROOT = orig_root
    Config.HISTORY_DB_PATH = orig_db


class TestRingBufferBound:
    """Challenge 1: Ring buffer bound and LRU eviction across multiple files."""

    def test_25_consecutive_edits_across_multiple_files(self, stress_workspace):
        workspace, db = stress_workspace
        files = [workspace / f"module_{k}.py" for k in range(5)]

        # Initialize 5 files
        for idx, f in enumerate(files):
            f.write_text(f"# Initial module {idx}\n", encoding="utf-8")

        # Perform 25 consecutive edits across the 5 files in round-robin
        for edit_num in range(1, 26):
            target_file = files[(edit_num - 1) % len(files)]
            pre_content = target_file.read_text(encoding="utf-8")
            post_content = pre_content + f"def step_{edit_num}(): return {edit_num}\n"

            edit_id = db.record_edit(
                str(target_file),
                pre_content,
                post_content,
                patch_summary=f"edit_{edit_num}",
            )
            assert edit_id is not None
            target_file.write_text(post_content, encoding="utf-8")

            # Invariant: Record count must never exceed 20
            current_count = db.count()
            expected_count = min(edit_num, 20)
            assert current_count == expected_count, (
                f"At edit {edit_num}, expected count {expected_count} but got {current_count}"
            )

        # Final count check after 25 edits
        assert db.count() == 20

        # Verify LRU eviction: edits 1-5 must be evicted; edits 6-25 must survive
        records = db.get_history(limit=50)
        assert len(records) == 20
        retained_summaries = [r["patch_summary"] for r in records]

        # Most recent should be edit_25
        assert retained_summaries[0] == "edit_25"
        # Oldest retained should be edit_6
        assert retained_summaries[-1] == "edit_6"

        # Edits 1..5 must NOT exist
        for evicted_num in range(1, 6):
            assert f"edit_{evicted_num}" not in retained_summaries

        # Now perform 20 successive undos
        for undo_idx in range(20):
            expected_edit_num = 25 - undo_idx
            success, msg, restored_path = db.undo()
            assert success is True, f"Undo {undo_idx + 1} failed: {msg}"
            assert db.count() == 20 - (undo_idx + 1)

        # 21st undo must report no edits available
        s21, msg21, _ = db.undo()
        assert s21 is False
        assert "No edits available to undo" in msg21
        assert db.count() == 0


class TestZeroBakFilesInvariant:
    """Challenge 2: Verify zero .bak or temporary undo files are created or left on disk."""

    def test_zero_bak_files_throughout_lifecycle(self, stress_workspace):
        workspace, db = stress_workspace
        files = [workspace / f"doc_{k}.txt" for k in range(5)]

        for f in files:
            f.write_text("v0\n", encoding="utf-8")

        # Perform 25 edits
        for i in range(1, 26):
            f = files[i % len(files)]
            pre = f.read_text(encoding="utf-8")
            post = pre + f"append {i}\n"
            db.record_edit(str(f), pre, post, f"append_{i}")
            f.write_text(post, encoding="utf-8")

        # Scan for .bak files after edits
        bak_files_after_edits = list(workspace.rglob("*.bak"))
        backup_files = list(workspace.rglob("*.backup"))
        tmp_undo_files = list(workspace.rglob(".tmp_undo*"))
        assert bak_files_after_edits == [], f"Found .bak files after edits: {bak_files_after_edits}"
        assert backup_files == [], f"Found .backup files after edits: {backup_files}"
        assert tmp_undo_files == [], f"Found temporary undo files: {tmp_undo_files}"

        # Perform 15 undos
        for _ in range(15):
            success, _, _ = db.undo()
            assert success is True

        # Scan for .bak files after undos
        bak_files_after_undos = list(workspace.rglob("*.bak"))
        tmp_undo_after_undos = list(workspace.rglob(".tmp_undo*"))
        assert bak_files_after_undos == [], f"Found .bak files after undos: {bak_files_after_undos}"
        assert tmp_undo_after_undos == [], f"Found temporary files after undos: {tmp_undo_after_undos}"


class TestExternalEditConflictDetection:
    """Challenge 3: External modification conflict detection and protection."""

    def test_external_modification_aborts_undo_and_preserves_disk(self, stress_workspace):
        workspace, db = stress_workspace
        target = workspace / "protected.txt"
        v0 = "Initial version 0\n"
        v1 = "Copilot version 1\n"
        v_external = "External manual change by human developer\n"

        target.write_text(v0, encoding="utf-8")
        db.record_edit(str(target), v0, v1, "edit to v1")
        target.write_text(v1, encoding="utf-8")

        # External modification occurs
        target.write_text(v_external, encoding="utf-8")

        # Attempt targeted undo
        success, msg, restored = db.undo(str(target))
        assert success is False
        assert "conflict" in msg.lower()
        assert "modified outside copilot" in msg.lower()

        # Empirical assertion: The external change MUST NOT be overwritten
        assert target.read_text(encoding="utf-8") == v_external

        # The history entry must NOT be deleted so the user can inspect/resolve it
        assert db.count() == 1

        # Attempt global undo without file_path argument: must also detect conflict
        success_global, msg_global, _ = db.undo()
        assert success_global is False
        assert "conflict" in msg_global.lower()
        assert target.read_text(encoding="utf-8") == v_external
        assert db.count() == 1

    def test_external_deletion_aborts_undo(self, stress_workspace):
        workspace, db = stress_workspace
        target = workspace / "to_delete.txt"
        v0 = "Pre content\n"
        v1 = "Post content\n"

        target.write_text(v0, encoding="utf-8")
        db.record_edit(str(target), v0, v1, "edit to v1")
        target.write_text(v1, encoding="utf-8")

        # External process deletes the file
        target.unlink()
        assert not target.exists()

        # Attempt undo
        success, msg, _ = db.undo(str(target))
        assert success is False
        assert "deleted externally" in msg.lower()

        # File should NOT be recreated
        assert not target.exists()
        assert db.count() == 1


class TestWindowsCRLFHashIntegrity:
    """Challenge 4: Windows CRLF (\\r\\n) hash integrity, round-trip restoration, and normalization."""

    def test_pure_crlf_roundtrip_byte_exactness(self, stress_workspace):
        workspace, db = stress_workspace
        crlf_file = workspace / "windows_lines.txt"

        v0_crlf = "Line 1\r\nLine 2\r\nLine 3\r\n"
        v1_crlf = "Line 1\r\nLine 2 Modified\r\nLine 3\r\nLine 4 Added\r\n"

        with open(crlf_file, "w", encoding="utf-8", newline="") as f:
            f.write(v0_crlf)

        db.record_edit(str(crlf_file), v0_crlf, v1_crlf, "crlf edit")

        with open(crlf_file, "w", encoding="utf-8", newline="") as f:
            f.write(v1_crlf)

        # Verify recorded hashes match exact raw utf-8 encoding
        history = db.get_history(limit=1)
        expected_pre_hash = hashlib.sha256(v0_crlf.encode("utf-8")).hexdigest()
        expected_post_hash = hashlib.sha256(v1_crlf.encode("utf-8")).hexdigest()
        assert history[0]["pre_edit_hash"] == expected_pre_hash
        assert history[0]["post_edit_hash"] == expected_post_hash

        # Perform undo
        success, msg, _ = db.undo(str(crlf_file))
        assert success is True

        # Binary byte-exactness verification: must contain \r\n and NOT \r\r\n
        with open(crlf_file, "rb") as f:
            restored_bytes = f.read()

        assert restored_bytes == v0_crlf.encode("utf-8")
        assert b"\r\n" in restored_bytes
        assert b"\r\r\n" not in restored_bytes

    def test_benign_newline_conversion_does_not_false_conflict(self, stress_workspace):
        workspace, db = stress_workspace
        target = workspace / "git_checkout_file.py"

        v0 = "def foo():\r\n    return 'hello'\r\n"
        v1 = "def foo():\r\n    return 'world'\r\n"

        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(v0)

        db.record_edit(str(target), v0, v1, "crlf edit")

        # Simulate git checkout or editor converting \r\n to \n without changing code semantics
        v1_lf = "def foo():\n    return 'world'\n"
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(v1_lf)

        # Undo should detect this as a benign line ending difference and restore successfully
        success, msg, _ = db.undo(str(target))
        assert success is True
        assert "Successfully restored" in msg

        # File is restored to v0
        with open(target, "r", encoding="utf-8", newline="") as f:
            content = f.read()
        assert content == v0

    def test_crlf_with_genuine_external_edit_triggers_conflict(self, stress_workspace):
        workspace, db = stress_workspace
        target = workspace / "crlf_modified.py"

        v0 = "def foo():\r\n    return 1\r\n"
        v1 = "def foo():\r\n    return 2\r\n"

        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(v0)

        db.record_edit(str(target), v0, v1, "crlf edit")

        # External edit modifies code and retains CRLF
        v_ext = "def foo():\r\n    return 999\r\n"
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(v_ext)

        success, msg, _ = db.undo(str(target))
        assert success is False
        assert "conflict" in msg.lower()


class TestWindowsPathNormalization:
    """Challenge 5: Mixed slashes, relative vs absolute paths, and case-insensitivity."""

    def test_backslash_and_case_insensitivity_lookup(self, stress_workspace):
        workspace, db = stress_workspace
        sub_dir = workspace / "src" / "Services"
        sub_dir.mkdir(parents=True, exist_ok=True)
        file_path = sub_dir / "worker.py"
        file_path.write_text("v0", encoding="utf-8")

        # Record with standard POSIX posix path
        db.record_edit(str(file_path), "v0", "v1", "edit worker")
        file_path.write_text("v1", encoding="utf-8")

        # Undo using Windows backslashes and different casing
        windows_path_upper = str(file_path).replace("/", "\\").upper()
        success, msg, restored = db.undo(windows_path_upper)
        assert success is True
        assert file_path.read_text(encoding="utf-8") == "v0"

    def test_relative_path_resolution(self, stress_workspace):
        workspace, db = stress_workspace
        rel_file = workspace / "relative_test.txt"
        rel_file.write_text("orig", encoding="utf-8")

        # Record with relative path string
        db.record_edit("relative_test.txt", "orig", "mod", "relative edit")
        rel_file.write_text("mod", encoding="utf-8")

        success, msg, restored = db.undo("relative_test.txt")
        assert success is True
        assert rel_file.read_text(encoding="utf-8") == "orig"


class TestFileCreationUndo:
    """Challenge 6: Undo creation of a new file deletes it from disk."""

    def test_undo_created_file_deletes_it(self, stress_workspace):
        workspace, db = stress_workspace
        new_file = workspace / "brand_new.txt"
        new_file.write_text("created content", encoding="utf-8")

        db.record_edit(
            str(new_file),
            pre_edit_content="",
            post_edit_content="created content",
            patch_summary="create_file",
        )

        assert new_file.exists()
        success, msg, restored = db.undo(str(new_file))
        assert success is True
        assert not new_file.exists(), "New file was not deleted upon undoing creation"
        assert db.count() == 0


class TestHighVolumeStress:
    """Challenge 7: 100 rapid sequential operations without SQLite database lock."""

    def test_rapid_insert_and_undo_loop(self, stress_workspace):
        workspace, db = stress_workspace
        f = workspace / "rapid.txt"
        f.write_text("base", encoding="utf-8")

        for i in range(100):
            pre = f"state_{i}"
            post = f"state_{i+1}"
            db.record_edit(str(f), pre, post, f"op_{i}")
            # Keep buffer bounded
            assert db.count() <= 20

        assert db.count() == 20


class TestTargetedInterleavedUndo:
    """Challenge 8: Targeted out-of-order undo across multiple files."""

    def test_targeted_undo_leaves_unrelated_history_intact(self, stress_workspace):
        workspace, db = stress_workspace
        file_a = workspace / "file_a.txt"
        file_b = workspace / "file_b.txt"

        file_a.write_text("a0", encoding="utf-8")
        file_b.write_text("b0", encoding="utf-8")

        # Edit A, then Edit B, then Edit A again
        db.record_edit(str(file_a), "a0", "a1", "edit a1")
        file_a.write_text("a1", encoding="utf-8")

        db.record_edit(str(file_b), "b0", "b1", "edit b1")
        file_b.write_text("b1", encoding="utf-8")

        db.record_edit(str(file_a), "a1", "a2", "edit a2")
        file_a.write_text("a2", encoding="utf-8")

        assert db.count() == 3

        # Undo targeted to file_b (which is NOT the top of stack)
        success_b, msg_b, _ = db.undo(str(file_b))
        assert success_b is True
        assert file_b.read_text(encoding="utf-8") == "b0"
        assert file_a.read_text(encoding="utf-8") == "a2"  # A untouched!
        assert db.count() == 2

        # Now undo file_a: should roll a2 -> a1
        success_a1, _, _ = db.undo(str(file_a))
        assert success_a1 is True
        assert file_a.read_text(encoding="utf-8") == "a1"
        assert db.count() == 1

        # Second undo on file_a: rolls a1 -> a0
        success_a0, _, _ = db.undo(str(file_a))
        assert success_a0 is True
        assert file_a.read_text(encoding="utf-8") == "a0"
        assert db.count() == 0


class TestUnicodeAndEmojiIntegrity:
    """Challenge 9: Unicode, multi-byte UTF-8, and emoji preservation with CRLF."""

    def test_unicode_emoji_crlf_roundtrip(self, stress_workspace):
        workspace, db = stress_workspace
        target = workspace / "unicode_test.txt"

        v0 = "Header 🚀\r\n你好世界 🌏\r\nGrüße über die Donau ÄÖÜß\r\n"
        v1 = "Header 🚀 modified\r\n你好世界 🌏 2026\r\nGrüße über die Donau ÄÖÜß\r\n"

        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(v0)

        db.record_edit(str(target), v0, v1, "unicode edit")

        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(v1)

        success, msg, _ = db.undo(str(target))
        assert success is True

        with open(target, "r", encoding="utf-8", newline="") as f:
            restored = f.read()

        assert restored == v0
        with open(target, "rb") as f:
            raw = f.read()
        assert raw == v0.encode("utf-8")


class TestCustomRingBufferCapacity:
    """Challenge 10: Explicit custom max_entries parameterization."""

    def test_custom_max_entries_bound_5(self, stress_workspace):
        workspace, _ = stress_workspace
        custom_db_path = workspace / ".copilot" / "custom_5.db"
        db = HistoryDB(db_path=custom_db_path, max_entries=5)

        for i in range(1, 11):
            db.record_edit(f"file_{i}.txt", f"v{i}", f"v{i+1}", f"summary_{i}")
            assert db.count() <= 5

        assert db.count() == 5
        records = db.get_history(limit=10)
        assert len(records) == 5
        # Oldest should be summary_6, newest summary_10
        assert records[0]["patch_summary"] == "summary_10"
        assert records[-1]["patch_summary"] == "summary_6"


class TestLargePayloadResilience:
    """Challenge 11: 1MB+ content payload resilience."""

    def test_large_file_undo_and_hashing(self, stress_workspace):
        workspace, db = stress_workspace
        large_file = workspace / "large_file.txt"

        # Generate ~1MB text with CRLF
        chunk = "Line of text with some padding data 1234567890\r\n" * 10
        v0 = chunk * 2000  # ~1 MB
        v1 = v0 + "EXTRA LINE AT THE END\r\n"

        with open(large_file, "w", encoding="utf-8", newline="") as f:
            f.write(v0)

        db.record_edit(str(large_file), v0, v1, "large edit")

        with open(large_file, "w", encoding="utf-8", newline="") as f:
            f.write(v1)

        success, msg, _ = db.undo(str(large_file))
        assert success is True

        with open(large_file, "r", encoding="utf-8", newline="") as f:
            restored = f.read()
        assert restored == v0
