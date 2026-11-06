"""
Test Suite for copilot/core/history.py
Tests SQLite reverse-patch undo history, LRU ring buffer, conflict detection,
crash resilience across restarts, and zero .bak files.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from config import Config
from copilot.core.history import HistoryDB


@pytest.fixture
def history_db(setup_test_project: Path):
    db_file = setup_test_project / ".copilot" / "history.db"
    db = HistoryDB(db_path=db_file)
    db.clear()
    return db


def test_history_single_file_undo(history_db: HistoryDB, setup_test_project: Path):
    """Modifies a file, calls undo, and asserts the file content is 100% restored."""
    file_path = setup_test_project / "src" / "test.txt"
    v0 = "Initial content\nline 2\n"
    v1 = "Modified content\nline 2\nline 3\n"

    file_path.write_text(v0, encoding="utf-8")
    edit_id = history_db.record_edit(str(file_path), v0, v1, patch_summary="modify test.txt")
    assert edit_id is not None
    file_path.write_text(v1, encoding="utf-8")

    success, msg, restored = history_db.undo(str(file_path))
    assert success is True
    assert "Successfully restored" in msg
    assert file_path.read_text(encoding="utf-8") == v0
    assert history_db.count() == 0

    # Invariant: Zero .bak files created anywhere
    bak_files = list(setup_test_project.rglob("*.bak"))
    assert bak_files == []


def test_history_multi_step_undo(history_db: HistoryDB, setup_test_project: Path):
    """Performs 3 successive edits; undoes them in reverse chronological order."""
    file_path = setup_test_project / "src" / "stepped.txt"
    v0 = "Version 0"
    v1 = "Version 1"
    v2 = "Version 2"
    v3 = "Version 3"

    file_path.write_text(v0, encoding="utf-8")

    history_db.record_edit(str(file_path), v0, v1, "v0 -> v1")
    file_path.write_text(v1, encoding="utf-8")

    history_db.record_edit(str(file_path), v1, v2, "v1 -> v2")
    file_path.write_text(v2, encoding="utf-8")

    history_db.record_edit(str(file_path), v2, v3, "v2 -> v3")
    file_path.write_text(v3, encoding="utf-8")

    assert history_db.count() == 3

    # Undo 1: v3 -> v2
    s1, _, _ = history_db.undo(str(file_path))
    assert s1 is True
    assert file_path.read_text(encoding="utf-8") == v2

    # Undo 2: v2 -> v1
    s2, _, _ = history_db.undo(str(file_path))
    assert s2 is True
    assert file_path.read_text(encoding="utf-8") == v1

    # Undo 3: v1 -> v0
    s3, _, _ = history_db.undo(str(file_path))
    assert s3 is True
    assert file_path.read_text(encoding="utf-8") == v0

    # Undo 4: Stack is empty
    s4, msg, _ = history_db.undo(str(file_path))
    assert s4 is False
    assert "No edits available to undo" in msg


def test_history_lru_ring_buffer_eviction(history_db: HistoryDB, setup_test_project: Path):
    """Exceeding MAX_ENTRIES=20 evicts the oldest patch cleanly from .copilot/history.db."""
    for i in range(1, 26):
        history_db.record_edit(
            f"src/file_{i}.txt",
            f"pre_{i}",
            f"post_{i}",
            patch_summary=f"edit {i}",
        )

    assert history_db.count() == 20
    history = history_db.get_history(limit=50)
    assert len(history) == 20

    # Most recent entry is id 25
    assert history[0]["id"] == 25
    # Oldest retained entry is id 6 (entries 1-5 evicted)
    assert history[-1]["id"] == 6


def test_history_undo_corrupted_file_state(history_db: HistoryDB, setup_test_project: Path):
    """If a file was manually modified outside copilot, undo alerts the user of conflict."""
    file_path = setup_test_project / "src" / "conflict.txt"
    v0 = "Initial"
    v1 = "Copilot Edit"
    v_external = "External Modification by User"

    file_path.write_text(v0, encoding="utf-8")
    history_db.record_edit(str(file_path), v0, v1)
    file_path.write_text(v1, encoding="utf-8")

    # Simulate external edit
    file_path.write_text(v_external, encoding="utf-8")

    # Attempt undo
    success, msg, _ = history_db.undo(str(file_path))
    assert success is False
    assert "conflict" in msg.lower()
    # Ensure external modifications were NOT overwritten
    assert file_path.read_text(encoding="utf-8") == v_external
    # Ensure entry remains for inspection / manual resolution
    assert history_db.count() == 1


def test_history_external_deletion_conflict(history_db: HistoryDB, setup_test_project: Path):
    """If a file was deleted externally after an edit, undo reports conflict."""
    file_path = setup_test_project / "src" / "deleted_externally.txt"
    v0 = "Start"
    v1 = "Edit"

    file_path.write_text(v0, encoding="utf-8")
    history_db.record_edit(str(file_path), v0, v1)
    file_path.write_text(v1, encoding="utf-8")

    # Externally delete file
    file_path.unlink()

    success, msg, _ = history_db.undo(str(file_path))
    assert success is False
    assert "deleted externally" in msg.lower()


def test_history_undo_empty_stack(history_db: HistoryDB):
    """Calling undo when no edits exist returns a clean notice."""
    success, msg, path = history_db.undo()
    assert success is False
    assert "No edits available to undo" in msg
    assert path is None


def test_history_survives_server_restart(setup_test_project: Path):
    """History stored in .copilot/history.db persists across server process restarts."""
    db_file = setup_test_project / ".copilot" / "history.db"

    # Server session 1
    db1 = HistoryDB(db_path=db_file)
    db1.record_edit("src/restart.txt", "v0", "v1", "session 1 edit")
    assert db1.count() == 1

    # Simulate server restart (new instance of HistoryDB pointing to same SQLite file)
    db2 = HistoryDB(db_path=db_file)
    assert db2.count() == 1
    history = db2.get_history()
    assert len(history) == 1
    assert history[0]["file_path"] == "src/restart.txt"
    assert history[0]["patch_summary"] == "session 1 edit"


def test_history_file_specific_undo(history_db: HistoryDB, setup_test_project: Path):
    """Verifies file-targeted undo when multiple files have history entries."""
    f1 = setup_test_project / "src" / "f1.txt"
    f2 = setup_test_project / "src" / "f2.txt"

    f1.write_text("f1_v0", encoding="utf-8")
    f2.write_text("f2_v0", encoding="utf-8")

    history_db.record_edit(str(f1), "f1_v0", "f1_v1", "edit f1")
    f1.write_text("f1_v1", encoding="utf-8")

    history_db.record_edit(str(f2), "f2_v0", "f2_v1", "edit f2")
    f2.write_text("f2_v1", encoding="utf-8")

    # Undo only f1
    s1, _, _ = history_db.undo(str(f1))
    assert s1 is True
    assert f1.read_text(encoding="utf-8") == "f1_v0"
    assert f2.read_text(encoding="utf-8") == "f2_v1"
    assert history_db.count() == 1

    # Undo f2
    s2, _, _ = history_db.undo(str(f2))
    assert s2 is True
    assert f2.read_text(encoding="utf-8") == "f2_v0"
    assert history_db.count() == 0


def test_history_crlf_preservation(history_db: HistoryDB, setup_test_project: Path):
    """Verifies Windows CRLF line endings are preserved through record and undo."""
    file_path = setup_test_project / "src" / "crlf.txt"
    v0 = "line1\r\nline2\r\nline3\r\n"
    v1 = "line1\r\nline2_modified\r\nline3\r\n"

    with open(file_path, "w", encoding="utf-8", newline="") as f:
        f.write(v0)

    history_db.record_edit(str(file_path), v0, v1, "crlf edit")

    with open(file_path, "w", encoding="utf-8", newline="") as f:
        f.write(v1)

    success, msg, _ = history_db.undo(str(file_path))
    assert success is True

    with open(file_path, "rb") as f:
        restored_bytes = f.read()

    assert restored_bytes == v0.encode("utf-8")
    assert b"\r\n" in restored_bytes
