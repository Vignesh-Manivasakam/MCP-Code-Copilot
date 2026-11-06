"""
Authoritative Test Suite 4: Code Search & Indexing.
===================================================
Tests SearchService for FTS5 snippets, grep with context, file globbing,
directory hygiene, truncation, and invalid regex handling as specified in Part 4 Suite 4.
"""
from pathlib import Path
import pytest

from config import Config
from copilot.services.search_service import SearchService, ERR_INVALID_REGEX, ERR_PATH_TRAVERSAL


def test_search_grep_regex_match(tmp_path):
    """Finds all occurrences of regex pattern with 2 lines of before/after context."""
    src = tmp_path / "controllers"
    src.mkdir(parents=True, exist_ok=True)
    ctrl_file = src / "auth_controller.py"
    ctrl_file.write_text("""import os
import sys

class AuthController:
    def login(self):
        pass

    def logout(self):
        pass

class UserController:
    def profile(self):
        pass
""", encoding="utf-8")

    service = SearchService(project_root=tmp_path)
    res = service.grep_search(r"class\s+.*Controller", context_lines=2)

    assert res["total_matches"] == 2
    assert len(res["results"]) == 2
    assert res["truncated"] is False

    match1 = res["results"][0]
    assert "AuthController" in match1["content"]
    assert match1["line_number"] == 4
    # Context should contain line numbers with > for match and space for context
    assert "> 4:" in match1["context"]
    assert "  2: import sys" in match1["context"]
    assert "  5:     def login(self):" in match1["context"]


def test_search_fts5_keyword(tmp_path):
    """Instant keyword retrieval via SQLite FTS5 table with match snippet highlighting."""
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "service.py").write_text("""def compute_payload():
    result = perform_calculation()
    return result
""", encoding="utf-8")

    (app_dir / "helper.py").write_text("""def perform_calculation():
    # complex arithmetic calculation
    return 100
""", encoding="utf-8")

    service = SearchService(project_root=tmp_path)
    count = service.index_all(force=True)
    assert count >= 2

    # Query for "calculation"
    search_res = service.fts_search("calculation")
    assert search_res["total_matches"] >= 2
    assert len(search_res["results"]) >= 2

    # Confirm snippet highlighting <<<match>>>
    snippets = [r["snippet"] for r in search_res["results"]]
    assert any("<<<calculation>>>" in snip for snip in snippets)


def test_search_files_glob(tmp_path):
    """Finds all *.test.ts files across the workspace."""
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)

    (tmp_path / "tests" / "unit.test.ts").write_text("// test", encoding="utf-8")
    (tmp_path / "tests" / "e2e.test.ts").write_text("// test", encoding="utf-8")
    (tmp_path / "src" / "main.ts").write_text("// main", encoding="utf-8")

    service = SearchService(project_root=tmp_path)
    res = service.glob_files("*.test.ts")

    assert res["total_matches"] == 2
    paths = [r["file_path"] for r in res["results"]]
    assert any("unit.test.ts" in p for p in paths)
    assert any("e2e.test.ts" in p for p in paths)
    assert not any("main.ts" in p for p in paths)


def test_search_ignore_directories(tmp_path):
    """Strictly skips .git/, .venv/, node_modules/, __pycache__/, .copilot/."""
    # Create files in excluded dirs
    for d in [".git", ".venv", "node_modules", "__pycache__", ".copilot"]:
        excluded_dir = tmp_path / d
        excluded_dir.mkdir(parents=True, exist_ok=True)
        (excluded_dir / "secret.py").write_text("SECRET_TOKEN = 'forbidden_in_search'", encoding="utf-8")

    # Create one valid file
    valid_dir = tmp_path / "src"
    valid_dir.mkdir(parents=True, exist_ok=True)
    (valid_dir / "valid.py").write_text("VALID_TOKEN = 'allowed_in_search'", encoding="utf-8")

    service = SearchService(project_root=tmp_path)

    # 1. Grep search check
    grep_res = service.grep_search("forbidden_in_search")
    assert grep_res["total_matches"] == 0
    assert len(grep_res["results"]) == 0

    # 2. Glob files check
    glob_res = service.glob_files("secret.py")
    assert glob_res["total_matches"] == 0
    assert len(glob_res["results"]) == 0

    # 3. FTS search check
    service.index_all(force=True)
    fts_res = service.fts_search("forbidden_in_search")
    assert fts_res["total_matches"] == 0
    assert len(fts_res["results"]) == 0


def test_search_max_results_truncation(tmp_path):
    """When a search matches many lines, truncates at max_results=25 and reports total matches."""
    repeat_file = tmp_path / "repeated.py"
    # Create 100 matching lines
    lines = [f"item_{i} = MATCH_TARGET_TOKEN_{i}" for i in range(100)]
    repeat_file.write_text("\n".join(lines), encoding="utf-8")

    service = SearchService(project_root=tmp_path)
    res = service.grep_search("MATCH_TARGET_TOKEN", max_results=25)

    assert res["total_matches"] == 100
    assert len(res["results"]) == 25
    assert res["truncated"] is True
    assert "Showing 25 of 100 matches" in res["truncation_notice"]


def test_search_special_characters(tmp_path):
    """Searches for strings with quotes, backslashes, and regex characters ($value = arr[\"key\"])."""
    code_file = tmp_path / "complex.php"
    content = """<?php
$value = arr["key"];
$escaped = "path\\to\\dir";
"""
    code_file.write_text(content, encoding="utf-8")

    service = SearchService(project_root=tmp_path)

    # Grep with special characters escaped or raw literal
    grep_res = service.grep_search(r'\$value = arr\["key"\];')
    assert grep_res["total_matches"] == 1
    assert "$value = arr" in grep_res["results"][0]["content"]

    # FTS search with quotes, brackets, dollars should not raise SQLite syntax error
    service.index_all(force=True)
    fts_res = service.fts_search('$value = arr["key"]')
    assert fts_res["total_matches"] >= 1
    assert any("<<<key>>>" in r["snippet"] or "<<<value>>>" in r["snippet"] or "<<<arr>>>" in r["snippet"] for r in fts_res["results"])


def test_search_invalid_regex(tmp_path):
    """Returns a structured error explaining the invalid regex syntax rather than a server crash."""
    service = SearchService(project_root=tmp_path)
    res = service.grep_search("[unclosed-group-regex(")

    assert res["error"] == ERR_INVALID_REGEX
    assert res["error_code"] == ERR_INVALID_REGEX
    assert "Invalid regex" in res["message"]
    assert res["results"] == []
    assert res["total_matches"] == 0


def test_search_zero_matches(tmp_path):
    """Returns an empty result set with a helpful suggestion message."""
    service = SearchService(project_root=tmp_path)
    res = service.grep_search("nonexistent_pattern_987654321")

    assert res["total_matches"] == 0
    assert res["results"] == []
    assert res["truncated"] is False
    assert "No matches found" in res["message"]


def test_search_path_traversal_blocked(tmp_path):
    """Grep search blocks attempts to search outside the project root."""
    service = SearchService(project_root=tmp_path)
    res = service.grep_search("test", path="../../outside")

    assert res["error"] == ERR_PATH_TRAVERSAL
    assert res["error_code"] == ERR_PATH_TRAVERSAL
    assert res["results"] == []


def test_symbol_search(tmp_path):
    """Symbol search extracts definitions across supported programming languages."""
    code_dir = tmp_path / "src"
    code_dir.mkdir(parents=True, exist_ok=True)

    py_code = """class UserManager:
    def get_user(self):
        pass

async def authenticate():
    pass
"""
    (code_dir / "user.py").write_text(py_code, encoding="utf-8")

    service = SearchService(project_root=tmp_path)
    res = service.search_symbols("User")
    assert res["total_matches"] >= 1
    symbols = [r["symbol"] for r in res["results"]]
    assert "UserManager" in symbols

    # Also test method alias symbol_search
    alias_res = service.symbol_search("authenticate")
    assert alias_res["total_matches"] >= 1
    assert any("authenticate" in r["symbol"] for r in alias_res["results"])


def test_search_reindex_consecutive_force(tmp_path):
    """Calling index_all(force=True) twice consecutively drops views/tables without OperationalError."""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "service.py").write_text("def run_task(): return 42", encoding="utf-8")

    service = SearchService(project_root=tmp_path)
    # First indexing pass creates FTS table and code_search VIEW
    count1 = service.index_all(force=True)
    assert count1 >= 1

    # Second indexing pass must drop existing VIEW and table cleanly without OperationalError
    count2 = service.index_all(force=True)
    assert count2 == count1

    # Verify search operates normally after re-indexing
    res = service.fts_search("run_task")
    assert res["total_matches"] >= 1
    assert any("<<<run_task>>>" in r["snippet"] for r in res["results"])


def test_search_connection_closed_on_windows(tmp_path):
    """Database connections are closed immediately, preventing [WinError 32] file lock collisions."""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "data.py").write_text("API_KEY_DATA = 'test_token'", encoding="utf-8")

    db_file = tmp_path / ".copilot" / "index.db"
    service = SearchService(index_path=db_file, project_root=tmp_path)
    service.index_all(force=True)
    res = service.fts_search("API_KEY_DATA")
    assert res["total_matches"] == 1

    # With connections closed immediately, we should be able to unlink the db file without WinError 32
    assert db_file.exists()
    db_file.unlink()
    assert not db_file.exists()

