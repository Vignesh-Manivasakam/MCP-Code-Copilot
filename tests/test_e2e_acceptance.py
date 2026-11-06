"""
End-to-End Acceptance Test Suite for FastMCP Code Copilot.
==========================================================
Empirical validation by Challenger Final-1.
Verifies:
1. GET /health status 200 and JSON structure.
2. /mcp initialize and session handshake.
3. tools/list returning all 6 tools with unstripped schemas.
4. Multi-tool sequence: inspect_ast -> code_search -> file_ops -> run_command -> git_ops.
5. Disk hygiene: zero .bak files created during operations.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Dict
import pytest
from starlette.testclient import TestClient

from config import Config
from copilot.services.telemetry import reset_telemetry
from server import create_app
from copilot.tools.inspect_ast import inspect_ast
from copilot.tools.code_search import code_search
from copilot.tools.file_ops import file_ops
from copilot.tools.run_command import run_command
from copilot.tools.git_ops import git_ops


def parse_mcp_response(response) -> Dict[str, Any]:
    text = response.text.strip()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            return json.loads(line[6:].strip())
        elif line.startswith("data:"):
            return json.loads(line[5:].strip())
    try:
        return response.json()
    except Exception:
        raise ValueError(f"Unable to parse response as MCP JSON-RPC: {text}")


@pytest.fixture
def test_environment(tmp_path: Path):
    """Set up a fully initialized git test repository with dummy files."""
    old_root = Config.PROJECT_ROOT
    reset_telemetry()
    Config.set_project_root(tmp_path)

    # Initialize git repo for git_ops tests
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Challenger Final-1"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "challenger@test.local"], cwd=tmp_path, check=True, capture_output=True)

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    app_py = src_dir / "app.py"
    app_py.write_text('def main():\n    print("Hello, world!")\n\nif __name__ == "__main__":\n    main()\n', encoding="utf-8")

    readme = tmp_path / "README.md"
    readme.write_text("# Test Workspace\n", encoding="utf-8")

    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True, capture_output=True)

    # Populate conventions file
    copilot_dir = tmp_path / ".copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)
    conventions_file = copilot_dir / "context.md"
    conventions_file.write_text("# Test Conventions\nAlways verify empirically.\n", encoding="utf-8")

    app = create_app(project_root=str(tmp_path))
    with TestClient(app) as tc:
        yield {"client": tc, "root": tmp_path}

    reset_telemetry()
    Config.PROJECT_ROOT = old_root


def test_e2e_health_endpoint(test_environment):
    """E2E Step 1: Call /health and verify 200 JSON status."""
    client: TestClient = test_environment["client"]
    res = client.get("/health")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert data.get("status") == "ok", f"Expected status 'ok', got {data}"
    assert data.get("server") == "FastMCP Code Copilot"
    assert "version" in data


def test_e2e_mcp_initialize_handshake(test_environment):
    """E2E Step 2: Call /mcp initialize and verify session handshake."""
    client: TestClient = test_environment["client"]
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": Config.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "challenger-final-1", "version": "1.0"},
        },
    }
    res = client.post("/mcp", json=payload)
    assert res.status_code == 200, f"Handshake failed: {res.text}"
    session_id = res.headers.get("mcp-session-id")
    assert session_id is not None, "mcp-session-id header missing in initialize response"
    assert len(session_id) > 0

    data = parse_mcp_response(res)
    assert "result" in data
    assert data["result"]["protocolVersion"] == Config.MCP_PROTOCOL_VERSION
    assert "serverInfo" in data["result"]
    assert "capabilities" in data["result"]
    assert "tools" in data["result"]["capabilities"]


def test_e2e_tools_list_schema_completeness(test_environment):
    """E2E Step 3: List tools and verify all 6 tools exist with unstripped properties."""
    client: TestClient = test_environment["client"]
    # Handshake
    init_res = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "initialize",
            "params": {
                "protocolVersion": Config.MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "schema-validator", "version": "1.0"},
            },
        },
    )
    assert init_res.status_code == 200
    session_id = init_res.headers.get("mcp-session-id")

    # Call tools/list
    list_res = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}},
        headers={"mcp-session-id": session_id},
    )
    assert list_res.status_code == 200
    data = parse_mcp_response(list_res)
    assert "result" in data
    tools = data["result"]["tools"]
    tool_names = {t["name"] for t in tools}

    expected_tools = {
        "file_ops",
        "code_search",
        "run_command",
        "git_ops",
        "inspect_ast",
        "diagnostics",
    }
    assert expected_tools.issubset(tool_names), f"Missing tools: {expected_tools - tool_names}"
    assert len(tools) == 6, f"Expected exactly 6 tools, got {len(tools)}: {tool_names}"

    # Verify unstripped schemas
    for t in tools:
        assert "inputSchema" in t, f"Tool {t['name']} has no inputSchema"
        schema = t["inputSchema"]
        assert schema.get("type") == "object", f"Tool {t['name']} inputSchema is not object"
        assert "properties" in schema, f"Tool {t['name']} missing properties in inputSchema"
        assert len(schema["properties"]) > 0, f"Tool {t['name']} has empty properties dict"
        raw_schema = json.dumps(schema)
        assert "_lazy_schema_required" not in raw_schema, f"Tool {t['name']} has lazy schema marker"


def test_e2e_mcp_multi_tool_sequence_over_mcp(test_environment):
    """E2E Step 4 (MCP Protocol): Execute inspect_ast -> code_search -> file_ops -> run_command -> git_ops via /mcp."""
    client: TestClient = test_environment["client"]
    root: Path = test_environment["root"]

    init_res = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 100,
            "method": "initialize",
            "params": {
                "protocolVersion": Config.MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "sequence-tester", "version": "1.0"},
            },
        },
    )
    assert init_res.status_code == 200
    session_id = init_res.headers.get("mcp-session-id")
    headers = {"mcp-session-id": session_id}

    # 1. inspect_ast
    req_ast = {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "tools/call",
        "params": {
            "name": "inspect_ast",
            "arguments": {"file_path": "src/app.py"},
        },
    }
    res_ast = client.post("/mcp", json=req_ast, headers=headers)
    assert res_ast.status_code == 200
    data_ast = parse_mcp_response(res_ast)
    assert "result" in data_ast
    content_ast = data_ast["result"].get("content", [])
    assert len(content_ast) > 0
    text_ast = content_ast[0]["text"]
    assert "main" in text_ast

    # 2. code_search
    req_search = {
        "jsonrpc": "2.0",
        "id": 102,
        "method": "tools/call",
        "params": {
            "name": "code_search",
            "arguments": {
                "mode": "grep",
                "query": "def main",
                "path": "src",
            },
        },
    }
    res_search = client.post("/mcp", json=req_search, headers=headers)
    assert res_search.status_code == 200
    data_search = parse_mcp_response(res_search)
    assert "result" in data_search
    content_search = data_search["result"].get("content", [])
    assert len(content_search) > 0
    text_search = content_search[0]["text"]
    assert "app.py" in text_search

    # 3. file_ops (write -> read -> patch -> delete)
    test_rel_path = "src/e2e_scratch_test_mcp.txt"

    # 3a. Write
    req_write = {
        "jsonrpc": "2.0",
        "id": 103,
        "method": "tools/call",
        "params": {
            "name": "file_ops",
            "arguments": {
                "action": "write",
                "file_path": test_rel_path,
                "content": "line1: alpha\nline2: beta\nline3: gamma\n",
            },
        },
    }
    res_write = client.post("/mcp", json=req_write, headers=headers)
    assert res_write.status_code == 200

    # 3b. Read
    req_read = {
        "jsonrpc": "2.0",
        "id": 104,
        "method": "tools/call",
        "params": {
            "name": "file_ops",
            "arguments": {"action": "read", "file_path": test_rel_path},
        },
    }
    res_read = client.post("/mcp", json=req_read, headers=headers)
    assert res_read.status_code == 200
    data_read = parse_mcp_response(res_read)
    text_read = data_read["result"]["content"][0]["text"]
    assert "line2: beta" in text_read

    # 3c. Patch
    req_patch = {
        "jsonrpc": "2.0",
        "id": 105,
        "method": "tools/call",
        "params": {
            "name": "file_ops",
            "arguments": {
                "action": "patch",
                "file_path": test_rel_path,
                "target": "line2: beta",
                "replacement": "line2: beta_modified",
            },
        },
    }
    res_patch = client.post("/mcp", json=req_patch, headers=headers)
    assert res_patch.status_code == 200

    # 3d. Verify Patch
    res_read2 = client.post("/mcp", json=req_read, headers=headers)
    data_read2 = parse_mcp_response(res_read2)
    assert "beta_modified" in data_read2["result"]["content"][0]["text"]

    # 3e. Delete
    req_del = {
        "jsonrpc": "2.0",
        "id": 106,
        "method": "tools/call",
        "params": {
            "name": "file_ops",
            "arguments": {"action": "delete", "file_path": test_rel_path},
        },
    }
    res_del = client.post("/mcp", json=req_del, headers=headers)
    assert res_del.status_code == 200

    # Verify file is deleted on disk
    assert not (root / test_rel_path).exists()

    # 4. run_command
    req_cmd = {
        "jsonrpc": "2.0",
        "id": 107,
        "method": "tools/call",
        "params": {
            "name": "run_command",
            "arguments": {
                "command": "python",
                "arguments": ["-c", "print('MCP_E2E_VERIFIED')"],
            },
        },
    }
    res_cmd = client.post("/mcp", json=req_cmd, headers=headers)
    assert res_cmd.status_code == 200
    data_cmd = parse_mcp_response(res_cmd)
    text_cmd = data_cmd["result"]["content"][0]["text"]
    assert "MCP_E2E_VERIFIED" in text_cmd

    # 5. git_ops
    req_git = {
        "jsonrpc": "2.0",
        "id": 108,
        "method": "tools/call",
        "params": {
            "name": "git_ops",
            "arguments": {"action": "status"},
        },
    }
    res_git = client.post("/mcp", json=req_git, headers=headers)
    assert res_git.status_code == 200
    data_git = parse_mcp_response(res_git)
    text_git = data_git["result"]["content"][0]["text"]
    assert ("branch" in text_git.lower()) or ("status" in text_git.lower()) or ("commit" in text_git.lower())


def test_e2e_direct_poly_tools_sequence(test_environment):
    """E2E Step 4 (Direct In-Memory Poly-Tools): inspect_ast -> code_search -> file_ops -> run_command -> git_ops."""
    root: Path = test_environment["root"]

    # 1. inspect_ast
    ast_res = inspect_ast(file_path="src/app.py")
    assert ast_res.get("success") is True
    assert "outline" in ast_res
    assert any(item.get("name") == "main" for item in ast_res["outline"])

    # 2. code_search
    search_res = code_search(mode="grep", query="def main", path="src")
    assert search_res.get("success") is True
    assert search_res.get("total_matches", 0) > 0

    # 3. file_ops
    scratch_file = "src/e2e_direct_scratch.txt"
    # write
    w_res = file_ops(action="write", file_path=scratch_file, content="Alpha\nBeta\nGamma\n")
    assert w_res.get("success") is True

    # read
    r_res = file_ops(action="read", file_path=scratch_file)
    assert r_res.get("success") is True
    assert "Beta" in r_res.get("content", "")

    # patch
    p_res = file_ops(action="patch", file_path=scratch_file, target="Beta", replacement="Beta_Patched")
    assert p_res.get("success") is True

    # read updated
    r_res2 = file_ops(action="read", file_path=scratch_file)
    assert "Beta_Patched" in r_res2.get("content", "")

    # delete
    d_res = file_ops(action="delete", file_path=scratch_file)
    assert d_res.get("success") is True
    assert not (root / scratch_file).exists()

    # 4. run_command
    cmd_res = run_command(command="python", arguments=["-c", "print('POLY_TOOL_SUCCESS')"])
    assert cmd_res.get("exit_code") == 0
    assert "POLY_TOOL_SUCCESS" in cmd_res.get("stdout", "")

    # 5. git_ops
    git_res = git_ops(action="status")
    assert git_res.get("success") is True
    assert "branch" in git_res
    assert len(git_res["branch"]) > 0


def test_e2e_zero_bak_files_audit():
    """E2E Step 5: Verify zero .bak backup files on disk across workspace."""
    bak_files = list(Config.PROJECT_ROOT.rglob("*.bak"))
    assert len(bak_files) == 0, f"Found .bak files on disk: {[str(p) for p in bak_files]}"
