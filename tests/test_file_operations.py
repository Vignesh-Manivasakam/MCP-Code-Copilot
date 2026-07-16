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
    expected_tools = [
        "read_file",
        "batch_read_files",
        "write_file",
        "create_file",
        "modify_file",
        "undo_edit",
        "list_files",
        "get_file_structure",
        "search_in_files",
        "grep_search",
        "find_function",
        "find_references",
        "get_file_info",
        "analyze_file",
        "get_diagnostics",
        "execute_command",
        "set_project_root",
    ]
    for tool_name in expected_tools:
        assert tool_name in tools_dict

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

def test_create_file_is_empty_only(setup_test_project):
    create_file = tools_dict["create_file"]
    res = create_file(file_path="src/empty.py")
    assert res["success"] is True
    assert res["action"] == "created"
    
    # Verify file is empty on disk
    path = setup_test_project / "src" / "empty.py"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == ""

def test_write_file_creates_with_content(setup_test_project):
    write_file = tools_dict["write_file"]
    res = write_file(file_path="src/new_file.py", content="print('hello')", create_backup=False)
    assert res["success"] is True
    assert res["action"] == "created"
    
    # Verify content on disk
    path = setup_test_project / "src" / "new_file.py"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "print('hello')"

def test_read_file_line_range(setup_test_project):
    read_file = tools_dict["read_file"]
    
    # Write a multi-line file
    (setup_test_project / "src" / "multi.py").write_text("line1\nline2\nline3\nline4\nline5", encoding="utf-8")
    
    # Read range 2 to 4
    res = read_file(file_path="src/multi.py", start_line=2, end_line=4)
    assert res["success"] is True
    assert res["content"] == "line2\nline3\nline4"
    assert res["total_lines"] == 5
    assert res["returned_lines"] == 3
    assert res["returned_range"] == "2-4"
    assert res["truncated"] is True

def test_batch_read_files(setup_test_project):
    batch_read_files = tools_dict["batch_read_files"]
    res = batch_read_files(file_paths=["src/app.py", "src/utils.py"])
    assert res["success"] is True
    assert res["files_read"] == 2
    assert "src/app.py" in res["files"]
    assert "src/utils.py" in res["files"]
    assert "Hello, world!" in res["files"]["src/app.py"]["content"]
    assert "def add" in res["files"]["src/utils.py"]["content"]

def test_grep_search_with_context(setup_test_project):
    grep_search = tools_dict["grep_search"]
    res = grep_search(pattern="add", path="src")
    assert res["success"] is True
    assert res["total_matches"] > 0
    assert len(res["results"]) > 0
    match = res["results"][0]
    assert match["file"] == "src/utils.py"
    assert "def add(a, b):" in match["content"]
    assert "> 1: def add(a, b):" in match["context"]

def test_grep_search_exclude_filter(setup_test_project):
    grep_search = tools_dict["grep_search"]
    
    # Create test file to exclude
    (setup_test_project / "src" / "exclude.test.py").write_text("def test_exclude(): pass", encoding="utf-8")
    
    res = grep_search(pattern="def", path="src", exclude="*.test.py")
    assert res["success"] is True
    for match in res["results"]:
        assert "exclude.test.py" not in match["file"]

def test_undo_edit_reverts_modify(setup_test_project):
    read_file = tools_dict["read_file"]
    modify_file = tools_dict["modify_file"]
    undo_edit = tools_dict["undo_edit"]
    
    # Read to establish history context
    read_file("src/app.py")
    
    # Edit the file
    res = modify_file(
        file_path="src/app.py",
        target_content='print("Hello, world!")',
        replacement_content='print("Changed!")'
    )
    assert res["success"] is True
    
    # Verify change
    path = setup_test_project / "src" / "app.py"
    assert 'print("Changed!")' in path.read_text(encoding="utf-8")
    
    # Undo
    res = undo_edit(file_path="src/app.py")
    assert res["success"] is True
    assert res["action"] == "reverted"
    
    # Verify revert on disk
    assert 'print("Hello, world!")' in path.read_text(encoding="utf-8")
    assert 'print("Changed!")' not in path.read_text(encoding="utf-8")

def test_undo_edit_no_history(setup_test_project):
    undo_edit = tools_dict["undo_edit"]
    res = undo_edit(file_path="src/app.py")
    assert res["success"] is False
    assert "No edit history found" in res["error"]

def test_get_diagnostics_python_valid(setup_test_project):
    get_diagnostics = tools_dict["get_diagnostics"]
    res = get_diagnostics(file_path="src/app.py")
    assert res["success"] is True
    assert res["status"] == "ok"
    assert res["total_errors"] == 0

def test_get_diagnostics_python_syntax_error(setup_test_project):
    get_diagnostics = tools_dict["get_diagnostics"]
    
    # Write invalid python file
    (setup_test_project / "src" / "invalid.py").write_text("def main(\n    print('error')", encoding="utf-8")
    
    res = get_diagnostics(file_path="src/invalid.py")
    assert res["success"] is True
    assert res["status"] == "error"
    assert res["total_errors"] > 0
    assert res["diagnostics"][0]["line"] in (1, 2)

def test_get_diagnostics_json_valid(setup_test_project):
    get_diagnostics = tools_dict["get_diagnostics"]
    (setup_test_project / "src" / "data.json").write_text('{"name": "test"}', encoding="utf-8")
    res = get_diagnostics(file_path="src/data.json")
    assert res["success"] is True
    assert res["status"] == "ok"
    assert res["total_errors"] == 0

def test_get_diagnostics_json_invalid(setup_test_project):
    get_diagnostics = tools_dict["get_diagnostics"]
    (setup_test_project / "src" / "invalid.json").write_text('{"name": "test"', encoding="utf-8")
    res = get_diagnostics(file_path="src/invalid.json")
    assert res["success"] is True
    assert res["status"] == "error"
    assert res["total_errors"] > 0

def test_execute_command_custom_timeout(setup_test_project):
    execute_command = tools_dict["execute_command"]
    res = execute_command("python", ["-c", "import time; time.sleep(1)"], timeout=5)
    assert res["success"] is True
    assert res["exit_code"] == 0

def test_execute_command_working_directory(setup_test_project):
    execute_command = tools_dict["execute_command"]
    
    # Create file in subdirectory
    (setup_test_project / "src" / "sub_test.py").write_text("print('sub')", encoding="utf-8")
    
    res = execute_command("python", ["sub_test.py"], working_directory="src")
    assert res["success"] is True
    assert "sub" in res["stdout"].strip()
