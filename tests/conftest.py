import pytest
from pathlib import Path
from config import Config

@pytest.fixture(autouse=True)
def setup_test_project(tmp_path):
    # Set Config project root to the temporary directory
    old_root = Config.PROJECT_ROOT
    Config.PROJECT_ROOT = tmp_path

    # Create dummy file structure
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".git").mkdir()

    # Create dummy files
    app_py = tmp_path / "src" / "app.py"
    app_py.write_text('def main():\n    print("Hello, world!")\n\nif __name__ == "__main__":\n    main()\n', encoding="utf-8")

    utils_py = tmp_path / "src" / "utils.py"
    utils_py.write_text('def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n', encoding="utf-8")

    test_js = tmp_path / "node_modules" / "test.js"
    test_js.write_text('console.log("ignored");\n', encoding="utf-8")

    git_file = tmp_path / ".git" / "config"
    git_file.write_text('[core]\n\trepositoryformatversion = 0\n', encoding="utf-8")

    # Clear registry
    Config.FILE_READ_REGISTRY.clear()

    yield tmp_path

    # Restore old project root
    Config.PROJECT_ROOT = old_root
