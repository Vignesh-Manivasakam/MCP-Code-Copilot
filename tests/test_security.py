"""
Test Suite 1: Security & Path Jail
Tests strict path jail sandboxing, sensitive file protection, symlinks, and directory hygiene.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
import pytest

from config import Config
from copilot.core.security import (
    validate_path,
    is_sensitive_file,
    filter_excluded_dirs,
    is_binary_file,
    check_file_readable,
    check_file_writable,
    SecurityValidator,
)


# ==============================================================================
# Positive Cases
# ==============================================================================

def test_security_valid_relative_path(setup_test_project: Path):
    """Accepts src/main.py located inside PROJECT_ROOT."""
    # Ensure src/main.py exists
    main_py = setup_test_project / "src" / "main.py"
    main_py.write_text("print('hello')", encoding="utf-8")

    is_valid, resolved, err = validate_path("src/main.py")
    assert is_valid is True
    assert resolved == main_py.resolve()
    assert err == ""


def test_security_valid_nested_path(setup_test_project: Path):
    """Accepts deep/nested/folder/config.json."""
    nested_dir = setup_test_project / "deep" / "nested" / "folder"
    nested_dir.mkdir(parents=True, exist_ok=True)
    config_json = nested_dir / "config.json"
    config_json.write_text("{}", encoding="utf-8")

    is_valid, resolved, err = validate_path("deep/nested/folder/config.json")
    assert is_valid is True
    assert resolved == config_json.resolve()
    assert err == ""


def test_security_valid_absolute_path_inside_root(setup_test_project: Path):
    """Accepts an absolute path when it physically resolves within PROJECT_ROOT."""
    app_py = setup_test_project / "src" / "app.py"
    abs_path_str = str(app_py.resolve())

    is_valid, resolved, err = validate_path(abs_path_str)
    assert is_valid is True
    assert resolved == app_py.resolve()
    assert err == ""


# ==============================================================================
# Edge Cases
# ==============================================================================

def test_security_symlink_inside_root(setup_test_project: Path):
    """Accepts a symlink whose target is also inside PROJECT_ROOT."""
    target_file = setup_test_project / "src" / "app.py"
    link_path = setup_test_project / "src" / "link_to_app.py"

    try:
        os.symlink(target_file, link_path)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported or permitted on this platform/run")

    is_valid, resolved, err = validate_path("src/link_to_app.py")
    assert is_valid is True
    assert resolved == target_file.resolve()
    assert err == ""


def test_security_case_insensitivity_on_windows(setup_test_project: Path):
    """Correctly matches paths on Windows NTFS regardless of casing (Src/Main.PY vs src/main.py)."""
    main_py = setup_test_project / "src" / "main.py"
    main_py.write_text("print('case test')", encoding="utf-8")

    is_valid, resolved, err = validate_path("Src/Main.PY")
    assert is_valid is True
    if sys.platform == "win32":
        # On Windows NTFS, case is normalized or matches disk file
        assert resolved == main_py.resolve()
    assert err == ""


def test_security_unicode_and_spaces_in_path(setup_test_project: Path):
    """Handles filenames with spaces, accents, and non-ASCII characters (docs/résumé v1.pdf)."""
    docs_dir = setup_test_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    resume_file = docs_dir / "résumé v1.pdf"
    resume_file.write_bytes(b"%PDF-1.4 dummy content")

    is_valid, resolved, err = validate_path("docs/résumé v1.pdf")
    assert is_valid is True
    assert resolved == resume_file.resolve()
    assert err == ""


def test_security_path_with_redundant_dots(setup_test_project: Path):
    """Normalizes src/./utils/../utils/helper.py to src/utils/helper.py."""
    utils_dir = setup_test_project / "src" / "utils"
    utils_dir.mkdir(exist_ok=True)
    helper_py = utils_dir / "helper.py"
    helper_py.write_text("def helper(): pass", encoding="utf-8")

    is_valid, resolved, err = validate_path("src/./utils/../utils/helper.py")
    assert is_valid is True
    assert resolved == helper_py.resolve()
    assert err == ""


# ==============================================================================
# Negative & Adversarial Cases
# ==============================================================================

def test_security_reject_parent_traversal(setup_test_project: Path):
    """Blocks ../../etc/passwd or ..\\..\\Windows\\System32."""
    for bad_path in ["../../etc/passwd", "..\\..\\Windows\\System32", "src/../../outside.py"]:
        is_valid, resolved, err = validate_path(bad_path)
        assert is_valid is False
        assert resolved is None
        assert "ERR_PATH_TRAVERSAL" in err or "outside the project root" in err


def test_security_reject_symlink_escaping_root(setup_test_project: Path):
    """Blocks a symlink inside the workspace pointing to an external folder."""
    outside_dir = setup_test_project.parent / "outside_dir"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")

    link_path = setup_test_project / "src" / "link_to_outside"
    try:
        os.symlink(outside_file, link_path)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported or permitted on this platform/run")

    is_valid, resolved, err = validate_path("src/link_to_outside")
    assert is_valid is False
    assert resolved is None
    assert "outside" in err.lower()


def test_security_reject_sensitive_files(setup_test_project: Path):
    """Rejects direct access to .env, .git/config, id_rsa, or .secrets."""
    for sensitive in [".env", ".git/config", "id_rsa", "id_ed25519", ".secrets", "nested/.env", "sub/.secrets"]:
        is_valid, resolved, err = validate_path(sensitive)
        assert is_valid is False
        assert resolved is None
        assert "Sensitive file access is denied" in err


def test_security_reject_null_byte_injection(setup_test_project: Path):
    """Rejects paths containing null bytes (main.py\\0.txt)."""
    is_valid, resolved, err = validate_path("main.py\x00.txt")
    assert is_valid is False
    assert resolved is None
    assert "Null bytes" in err or "ERR_PATH_TRAVERSAL" in err


# ==============================================================================
# Utility & Hygiene Tests
# ==============================================================================

def test_filter_excluded_dirs():
    """Verifies filter_excluded_dirs prunes in-place matching Config.EXCLUDE_DIRS."""
    dirs = ["src", ".git", "node_modules", "tests", "__pycache__", ".venv"]
    res = filter_excluded_dirs(dirs)
    assert dirs == ["src", "tests"]
    assert res == ["src", "tests"]


def test_is_sensitive_file_normalization():
    """Verifies is_sensitive_file handles casing and backslashes."""
    assert is_sensitive_file(".env") is True
    assert is_sensitive_file(".ENV") is True
    assert is_sensitive_file(r".git\config") is True
    assert is_sensitive_file(".git/config") is True
    assert is_sensitive_file("src/id_rsa") is True
    assert is_sensitive_file("src/app.py") is False


def test_is_binary_file(setup_test_project: Path):
    """Verifies binary detection via null byte inspection."""
    text_file = setup_test_project / "src" / "app.py"
    assert is_binary_file(text_file) is False

    bin_file = setup_test_project / "dummy.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03hello")
    assert is_binary_file(bin_file) is True


def test_security_validator_backward_compatibility(setup_test_project: Path):
    """Ensures SecurityValidator class provides backward compatibility."""
    app_py = setup_test_project / "src" / "app.py"
    is_valid, resolved, err = SecurityValidator.validate_path("src/app.py")
    assert is_valid is True
    assert resolved == app_py.resolve()

    readable, r_err = SecurityValidator.check_file_readable(app_py)
    assert readable is True
    assert r_err == ""

    writable, w_err = SecurityValidator.check_file_writable(app_py)
    assert writable is True
    assert w_err == ""
