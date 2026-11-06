"""
Authoritative Test Suite 6: Undo History & Git Plumbing.
========================================================
Tests SQLite reverse-patch undo history, LRU ring buffer, server restart persistence,
conflict detection, and native Git plumbing (shadow checkpoints without dirtying
working tree, diff stat with compaction, status parsing, and rollback) as specified in Part 4 Suite 6.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import pytest

from config import Config
from copilot.core.history import HistoryDB
from copilot.tools.git_ops import (
    git_ops,
    ERR_NOT_GIT_REPO,
    ERR_INVALID_ACTION,
)


@pytest.fixture
def history_db(setup_test_project: Path):
    db_file = setup_test_project / ".copilot" / "history.db"
    db = HistoryDB(db_path=db_file)
    db.clear()
    return db


@pytest.fixture
def git_repo(tmp_path: Path):
    """Initialize a real Git repository in tmp_path with configured user."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Copilot Test"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "copilot@test.local"], cwd=tmp_path, check=True, capture_output=True)

    # Initial root commit
    readme = tmp_path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".copilot/\n.venv/\nnode_modules/\n__pycache__/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True, capture_output=True)

    old_root = Config.PROJECT_ROOT
    Config.PROJECT_ROOT = tmp_path
    yield tmp_path
    Config.PROJECT_ROOT = old_root


# ==============================================================================
# Positive Cases
# ==============================================================================

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


def test_git_shadow_checkpoint(git_repo: Path):
    """Creates a shadow checkpoint; restores it without dirtying Git working tree."""
    target_file = git_repo / "src_app.py"
    target_file.write_text("def work(): pass\n", encoding="utf-8")
    untracked_file = git_repo / "scratch.py"
    untracked_file.write_text("# scratch\n", encoding="utf-8")

    status_before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    ).stdout

    res = git_ops(action="checkpoint", message="save work before refactor")
    assert res["success"] is True
    assert "checkpoint_id" in res
    checkpoint_id = res["checkpoint_id"]

    # Verify working tree status is IDENTICAL before and after checkpoint
    status_after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    ).stdout
    assert status_before == status_after

    # Verify active HEAD commit was NOT altered
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert checkpoint_id != head_sha


def test_git_rollback_to_checkpoint(git_repo: Path):
    """Creates shadow checkpoint, makes breaking edits and extra files, and rolls back."""
    target_file = git_repo / "module.py"
    target_file.write_text("STABLE_V1 = True\n", encoding="utf-8")

    chk_res = git_ops(action="checkpoint", message="checkpoint v1")
    assert chk_res["success"] is True
    chk_id = chk_res["checkpoint_id"]

    # Introduce breaking edit and unwanted extra file
    target_file.write_text("BROKEN_SYNTAX_ERROR = ???\n", encoding="utf-8")
    bad_file = git_repo / "unwanted_junk.tmp"
    bad_file.write_text("junk", encoding="utf-8")

    # Roll back to checkpoint
    rb_res = git_ops(action="rollback", target_ref=chk_id)
    assert rb_res["success"] is True
    assert rb_res["restored_to"] == chk_id

    # Verify target file restored and unwanted file removed
    assert target_file.read_text(encoding="utf-8") == "STABLE_V1 = True\n"
    assert not bad_file.exists()


def test_git_status_clean_and_dirty(git_repo: Path):
    """Verifies git status reporting clean status and tracking modified/untracked files."""
    # 1. Clean status
    res1 = git_ops(action="status")
    assert res1["success"] is True
    assert res1["is_clean"] is True
    assert res1["total_changes"] == 0

    # 2. Dirty status
    (git_repo / "new_file.txt").write_text("hello", encoding="utf-8")
    res2 = git_ops(action="status")
    assert res2["success"] is True
    assert res2["is_clean"] is False
    assert "new_file.txt" in res2["untracked"]


def test_git_diff_with_compaction(git_repo: Path):
    """Generates git diff with stat summary and 40-line compaction for large diffs."""
    readme = git_repo / "README.md"
    long_content = "\n".join(f"Line {i}: test content" for i in range(100))
    readme.write_text(long_content, encoding="utf-8")

    res = git_ops(action="diff")
    assert res["success"] is True
    assert "README.md" in res["stat"]
    assert res["truncated"] is True
    assert res["total_lines"] > 40
    assert "[Output compacted:" in res["diff"]


# ==============================================================================
# Edge Cases (X, Y, Z)
# ==============================================================================

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
    assert history[0]["id"] == 25
    assert history[-1]["id"] == 6


def test_history_survives_server_restart(setup_test_project: Path):
    """History stored in .copilot/history.db persists across server process restarts."""
    db_file = setup_test_project / ".copilot" / "history.db"

    db1 = HistoryDB(db_path=db_file)
    db1.record_edit("src/restart.txt", "v0", "v1", "session 1 edit")
    assert db1.count() == 1

    db2 = HistoryDB(db_path=db_file)
    assert db2.count() == 1
    history = db2.get_history()
    assert history[0]["file_path"] == "src/restart.txt"


# ==============================================================================
# Negative & Failure Cases
# ==============================================================================

def test_history_undo_empty_stack(history_db: HistoryDB):
    """Calling undo when no edits exist returns a clean notice."""
    success, msg, path = history_db.undo()
    assert success is False
    assert "No edits available to undo" in msg
    assert path is None


def test_history_undo_corrupted_file_state(history_db: HistoryDB, setup_test_project: Path):
    """If a file was manually modified outside copilot, undo alerts the user of conflict."""
    file_path = setup_test_project / "src" / "conflict.txt"
    v0 = "Initial"
    v1 = "Copilot Edit"
    v_external = "External Modification by User"

    file_path.write_text(v0, encoding="utf-8")
    history_db.record_edit(str(file_path), v0, v1)
    file_path.write_text(v1, encoding="utf-8")

    file_path.write_text(v_external, encoding="utf-8")

    success, msg, _ = history_db.undo(str(file_path))
    assert success is False
    assert "conflict" in msg.lower()
    assert file_path.read_text(encoding="utf-8") == v_external


def test_git_ops_not_a_repo(tmp_path: Path):
    """Calling git_ops on a non-git directory returns ERR_NOT_GIT_REPO."""
    non_git = tmp_path / "plain_dir"
    non_git.mkdir()
    old_root = Config.PROJECT_ROOT
    Config.PROJECT_ROOT = non_git
    try:
        res = git_ops(action="status")
        assert res["success"] is False
        assert res["error_code"] == ERR_NOT_GIT_REPO
        assert "not a git repository" in res["message"].lower()
    finally:
        Config.PROJECT_ROOT = old_root


def test_git_ops_invalid_action(git_repo: Path):
    """Calling git_ops with unsupported action returns ERR_INVALID_ACTION."""
    res = git_ops(action="rebase")
    assert res["success"] is False
    assert res["error_code"] == ERR_INVALID_ACTION
    assert "unsupported git action" in res["message"].lower()
