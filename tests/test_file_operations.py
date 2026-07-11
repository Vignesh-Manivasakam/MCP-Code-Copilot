import os
import pytest
from pathlib import Path
from config import Config
from tools.file_operations import register_file_tools
from fastmcp import FastMCP

import asyncio

# Instantiate test FastMCP
mcp = FastMCP("test-server")
register_file_tools(mcp)

# Retrieve registered tools asynchronously
tools = asyncio.run(mcp.list_tools())
tools_dict = {t.name: t.fn for t in tools}

def test_mcp_tool_registration():
    assert "read_file" in tools_dict
    assert "write_file" in tools_dict
    assert "modify_file" in tools_dict
    assert "execute_command" in tools_dict
    assert "search_in_files" in tools_dict

def test_read_file_registry(setup_test_project):
    read_file = tools_dict["read_file"]
    res = read_file("src/app.py")
    assert res["success"] is True
    assert "Hello, world!" in res["content"]

    # Verify tracking database registered the read
    resolved = setup_test_project / "src" / "app.py"
    assert resolved in Config.FILE_READ_REGISTRY
    assert Config.FILE_READ_REGISTRY[resolved]["content"] == res["content"]

def test_modify_file_success(setup_test_project):
    read_file = tools_dict["read_file"]
    modify_file = tools_dict["modify_file"]

    # Initial read to populate registry
    read_file("src/app.py")

    # Modify a single line
    res = modify_file(
        file_path="src/app.py",
        target_content='print("Hello, world!")',
        replacement_content='print("Hello, Super Copilot!")',
    )
    assert res["success"] is True
    assert res["action"] == "modified"

    # Verify contents on disk
    app_py_content = (setup_test_project / "src" / "app.py").read_text(encoding="utf-8")
    assert 'print("Hello, Super Copilot!")' in app_py_content
    assert 'print("Hello, world!")' not in app_py_content

def test_modify_file_staleness_error(setup_test_project):
    read_file = tools_dict["read_file"]
    modify_file = tools_dict["modify_file"]

    # Read to register
    read_file("src/app.py")

    # Modify outside server (e.g. by user/git)
    resolved = setup_test_project / "src" / "app.py"
    resolved.write_text('def main():\n    print("Modified by user")\n', encoding="utf-8")

    # Try modifying via server - should raise staleness error
    res = modify_file(
        file_path="src/app.py",
        target_content='print("Hello, world!")',
        replacement_content='print("Hello, Copilot!")',
    )
    assert res["success"] is False
    assert "modified since it was last read" in res["error"]

def test_modify_file_quote_normalization(setup_test_project):
    read_file = tools_dict["read_file"]
    modify_file = tools_dict["modify_file"]

    # Create file with smart quotes
    file_with_quotes = setup_test_project / "src" / "quotes.py"
    file_with_quotes.write_text('title = “My Smart Title”\n', encoding="utf-8")

    # Read it
    read_file("src/quotes.py")

    # Request modification with straight quotes - should match using normalization
    res = modify_file(
        file_path="src/quotes.py",
        target_content='title = "My Smart Title"',
        replacement_content='title = "My New Title"',
    )
    assert res["success"] is True

    # Verify content
    content = file_with_quotes.read_text(encoding="utf-8")
    # Quote style should be preserved back to smart quotes!
    assert 'title = “My New Title”' in content

def test_modify_file_uniqueness_guard(setup_test_project):
    read_file = tools_dict["read_file"]
    modify_file = tools_dict["modify_file"]

    # Create file with duplicate blocks
    utils_py = setup_test_project / "src" / "utils.py"
    utils_py.write_text("a = 1\na = 1\n", encoding="utf-8")

    # Read
    read_file("src/utils.py")

    # Try modifying without allow_multiple - should fail
    res = modify_file(
        file_path="src/utils.py",
        target_content="a = 1",
        replacement_content="a = 2",
        allow_multiple=False,
    )
    assert res["success"] is False
    assert "Found 2 matches" in res["error"]

    # Modify with allow_multiple - should succeed
    res = modify_file(
        file_path="src/utils.py",
        target_content="a = 1",
        replacement_content="a = 2",
        allow_multiple=True,
    )
    assert res["success"] is True
    assert utils_py.read_text(encoding="utf-8") == "a = 2\na = 2\n"

def test_modify_file_with_line_bounds(setup_test_project):
    read_file = tools_dict["read_file"]
    modify_file = tools_dict["modify_file"]

    # Create file with identical lines in different sections
    file_path = setup_test_project / "src" / "bounds.py"
    file_path.write_text("line_val = 10\n# comment section\nline_val = 10\n", encoding="utf-8")

    # Read
    read_file("src/bounds.py")

    # Edit targeting line_val = 10 restricted to line 3 to 3 - should work uniquely
    res = modify_file(
        file_path="src/bounds.py",
        target_content="line_val = 10",
        replacement_content="line_val = 20",
        start_line=3,
        end_line=3,
    )
    assert res["success"] is True
    assert file_path.read_text(encoding="utf-8") == "line_val = 10\n# comment section\nline_val = 20\n"

def test_execute_command_whitelisting(setup_test_project):
    execute_command = tools_dict["execute_command"]

    # Safe command
    res = execute_command("python", ["-c", "print('hello from python')"])
    assert res["success"] is True
    assert res["exit_code"] == 0
    assert "hello from python" in res["stdout"]

    # Unsafe command (not whitelisted)
    res = execute_command("curl", ["https://google.com"])
    assert res["success"] is False
    assert "SECURITY_VIOLATION" in res["error_code"]

def test_safe_walk_traversal_filtering(setup_test_project):
    search_in_files = tools_dict["search_in_files"]

    # Search for "ignored" - should yield 0 because node_modules is ignored
    res = search_in_files("ignored")
    assert res["success"] is True
    assert res["total_matches"] == 0

    # Search for "def" - should find main in src/app.py and add/subtract in src/utils.py
    res = search_in_files("def")
    assert res["success"] is True
    assert res["total_matches"] > 0
    # None of the matches should belong to node_modules or .git
    for match in res["results"]:
        assert "node_modules" not in match["file_path"]
        assert ".git" not in match["file_path"]
