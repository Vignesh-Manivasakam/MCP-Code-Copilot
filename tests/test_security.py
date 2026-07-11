import os
import pytest
from pathlib import Path
from middleware.security import SecurityValidator

def test_validate_path_valid(setup_test_project):
    is_valid, resolved, err = SecurityValidator.validate_path("src/app.py")
    assert is_valid is True
    assert resolved == setup_test_project / "src" / "app.py"
    assert err == ""

def test_validate_path_absolute_rejected(setup_test_project):
    # Try an absolute path string (rejected to enforce relative paths to project root)
    abs_path_str = str(setup_test_project / "src" / "app.py")
    is_valid, resolved, err = SecurityValidator.validate_path(abs_path_str)
    assert is_valid is False
    assert "Absolute paths are not allowed" in err

def test_validate_path_traversal_rejected(setup_test_project):
    is_valid, resolved, err = SecurityValidator.validate_path("src/../../outside.py")
    assert is_valid is False
    assert "Path traversal ('..') is not permitted" in err

def test_validate_path_resolved_outside_rejected(setup_test_project):
    outside_dir = setup_test_project.parent / "outside_dir"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")

    link_path = setup_test_project / "src" / "link_to_outside"
    try:
        os.symlink(outside_file, link_path)
    except (OSError, NotImplementedError):
        # On Windows, symlink creation can fail without elevated privileges.
        pytest.skip("Symlink creation not supported or permitted on this platform/run")

    is_valid, resolved, err = SecurityValidator.validate_path("src/link_to_outside")
    assert is_valid is False
    assert "outside the project root" in err
