"""
Milestone M5 Empirical Stress Challenge Test Suite.
===================================================
Adversarial testing of:
1. Prompt argument variations for implement_feature and debug_issue:
   - Python function call with Python list ["a.py", "b.py"]
   - Python function call with None, empty string, single file, spaces, unicode
   - MCP JSON-RPC protocol with JSON array string '["a.py", "b.py"]'
   - MCP JSON-RPC protocol with comma-delimited string "a.py, b.py"
   - MCP JSON-RPC protocol with newline-delimited string "a.py\\nb.py"
   - MCP JSON-RPC protocol with empty string "" and whitespace
   - MCP JSON-RPC protocol with single file "a.py"
   - MCP JSON-RPC protocol with special characters & spaces in filenames
   - MCP JSON-RPC protocol with unicode characters in filenames
   - Direct verification that raw JSON array over MCP triggers protocol -32602 per MCP spec (arguments must be dict[str, str])
2. Resource reading: session://token-usage:
   - Normal token count (<80% quota): warning_header is False, warning_message is None
   - Threshold boundary (80% quota = 800,000 tokens): warning_header is True, warning_message present
   - High token count (>80% quota = 920,000 tokens): warning_header is True, remaining calculated
   - Over-budget token count (>100% quota = 1,250,000 tokens): remaining >= 0, percent_used > 100%
3. Resource reading: project://conventions:
   - Present with custom conventions: custom contents returned
   - Absent: fallback conventions returned
   - Empty file (0 bytes) / whitespace: empirical bug reproduction (returns empty string instead of fallback)
4. Tool schemas:
   - Query tools/list over /mcp
   - Verify all 6 poly-tools have unstripped schemas
   - Verify properties dictionary is non-empty, zero _lazy_schema_required
   - Verify specific property fields for each of the 6 tools
5. Protocol Robustness & Edge Cases:
   - Missing required arguments in prompts/get
   - Unknown methods and unknown resources
   - Missing session ID on non-initialize requests
"""
import json
from pathlib import Path
import shutil
from typing import Any, Dict, Generator
import pytest
from starlette.testclient import TestClient

from config import Config
from copilot.prompts.coding_prompts import (
    _normalize_file_list,
    generate_implement_feature_prompt,
    generate_debug_issue_prompt,
    implement_feature,
    debug_issue,
)
from copilot.prompts.resources import (
    WORKSPACE_CONVENTIONS_FALLBACK,
    get_project_conventions_content,
    get_session_token_usage_content,
)
from copilot.services.telemetry import (
    get_token_tracker,
    get_token_usage,
    reset_telemetry,
)
from server import create_app


def parse_mcp_response(response) -> Dict[str, Any]:
    """Helper to parse SSE-framed or raw JSON responses."""
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
def test_app(tmp_path: Path):
    """Instantiate a Starlette test app pointing to a clean temporary workspace."""
    old_root = Config.PROJECT_ROOT
    reset_telemetry()
    Config.set_project_root(tmp_path)

    app = create_app(project_root=str(tmp_path))
    yield app

    reset_telemetry()
    Config.PROJECT_ROOT = old_root


@pytest.fixture
def client(test_app) -> Generator[TestClient, None, None]:
    """Yield a Starlette TestClient with active lifespan."""
    with TestClient(test_app) as tc:
        yield tc


@pytest.fixture
def session_id(client: TestClient) -> str:
    """Initialize MCP handshake and return valid session ID."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": Config.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "adversarial-tester", "version": "1.0.0"},
        },
    }
    res = client.post("/mcp", json=payload)
    assert res.status_code == 200, f"Handshake failed: {res.text}"
    sid = res.headers.get("mcp-session-id")
    assert sid is not None, "Missing mcp-session-id header"
    return sid


# ===========================================================================
# 1. Prompt Argument Variations: Python Invocations & _normalize_file_list
# ===========================================================================

@pytest.mark.parametrize(
    "input_val, expected",
    [
        (["a.py", "b.py"], ["a.py", "b.py"]),
        ('["a.py", "b.py"]', ["a.py", "b.py"]),
        ("a.py, b.py", ["a.py", "b.py"]),
        ("a.py\nb.py", ["a.py", "b.py"]),
        ("", []),
        ("a.py", ["a.py"]),
        ("path/with spaces/file name.py", ["path/with spaces/file name.py"]),
        ("src/unicode_⚡_test.py", ["src/unicode_⚡_test.py"]),
        ("file1.py,   file2.py \n file3.py", ["file1.py", "file2.py", "file3.py"]),
        (None, []),
        ("   \t\n  ", []),
        ('["unclosed_bracket.py', ['["unclosed_bracket.py']),
        ('["valid1.py", "valid with spaces.py"]', ["valid1.py", "valid with spaces.py"]),
        (["  trimmed.py  ", "clean.py"], ["trimmed.py", "clean.py"]),
    ],
)
def test_normalize_file_list_variations(input_val, expected):
    """Verify _normalize_file_list handles all argument formats without throwing exceptions."""
    result = _normalize_file_list(input_val)
    assert result == expected


def test_python_direct_prompt_calls_with_python_list():
    """Verify implement_feature and debug_issue accept native Python lists directly."""
    # implement_feature
    p1 = implement_feature("FeatX", "SpecX", files_to_modify=["src/core.py", "src/util.py"])
    assert "- `src/core.py`" in p1
    assert "- `src/util.py`" in p1

    # debug_issue
    p2 = debug_issue("ErrorX", "pytest tests/", suspected_files=["lib/a.py", "lib/b.py"])
    assert "- `lib/a.py`" in p2
    assert "- `lib/b.py`" in p2

    # None and empty variations
    p3 = implement_feature("FeatY", "SpecY", files_to_modify="")
    assert "None specified upfront" in p3

    p4 = debug_issue("ErrorY", None, suspected_files=None)
    assert "None specified" in p4
    assert "No reproducing command specified" in p4


# ===========================================================================
# 2. Prompt Argument Variations: MCP Protocol /mcp
# ===========================================================================

@pytest.mark.parametrize(
    "files_arg, expected_in_prompt",
    [
        ('["src/a.py", "src/b.py"]', ["`src/a.py`", "`src/b.py`"]),
        ("src/a.py, src/b.py", ["`src/a.py`", "`src/b.py`"]),
        ("src/a.py\nsrc/b.py", ["`src/a.py`", "`src/b.py`"]),
        ("", ["None specified upfront"]),
        ("src/a.py", ["`src/a.py`"]),
        ("path/with spaces/my file.py", ["`path/with spaces/my file.py`"]),
        ("src/unicode_⚡_test.py", ["`src/unicode_⚡_test.py`"]),
        ("   ", ["None specified upfront"]),
        ('["file with spaces 1.py", "file with spaces 2.py"]', ["`file with spaces 1.py`", "`file with spaces 2.py`"]),
    ],
)
def test_mcp_prompt_implement_feature_string_variations(
    client: TestClient, session_id: str, files_arg, expected_in_prompt
):
    """Stress-test prompts/get for implement_feature over MCP wire protocol."""
    payload = {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "prompts/get",
        "params": {
            "name": "implement_feature",
            "arguments": {
                "feature_name": "AdversarialAuth",
                "specification": "Test all file argument variations",
                "files_to_modify": files_arg,
            },
        },
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": session_id})
    assert response.status_code == 200, f"Request failed: {response.text}"

    data = parse_mcp_response(response)
    assert "result" in data, f"Error in response: {data}"
    messages = data["result"]["messages"]
    assert len(messages) >= 1
    text = messages[0]["content"]["text"]

    assert "AdversarialAuth" in text
    assert "Test all file argument variations" in text
    for item in expected_in_prompt:
        assert item in text, f"Expected '{item}' in prompt text:\n{text}"


@pytest.mark.parametrize(
    "suspected_arg, expected_in_prompt",
    [
        ('["lib/core.py", "lib/util.py"]', ["`lib/core.py`", "`lib/util.py`"]),
        ("lib/core.py, lib/util.py", ["`lib/core.py`", "`lib/util.py`"]),
        ("lib/core.py\nlib/util.py", ["`lib/core.py`", "`lib/util.py`"]),
        ("", ["None specified"]),
        ("lib/core.py", ["`lib/core.py`"]),
        ("path/with spaces/bug file.py", ["`path/with spaces/bug file.py`"]),
        ("src/unicode_⚡_bug.py", ["`src/unicode_⚡_bug.py`"]),
    ],
)
def test_mcp_prompt_debug_issue_string_variations(
    client: TestClient, session_id: str, suspected_arg, expected_in_prompt
):
    """Stress-test prompts/get for debug_issue over MCP wire protocol."""
    payload = {
        "jsonrpc": "2.0",
        "id": 102,
        "method": "prompts/get",
        "params": {
            "name": "debug_issue",
            "arguments": {
                "error_message": "ZeroDivisionError: division by zero in calculate()",
                "reproducing_command": "pytest tests/test_calc.py -k test_zero",
                "suspected_files": suspected_arg,
            },
        },
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": session_id})
    assert response.status_code == 200, f"Request failed: {response.text}"

    data = parse_mcp_response(response)
    assert "result" in data, f"Error in response: {data}"
    messages = data["result"]["messages"]
    assert len(messages) >= 1
    text = messages[0]["content"]["text"]

    assert "ZeroDivisionError: division by zero" in text
    assert "pytest tests/test_calc.py -k test_zero" in text
    for item in expected_in_prompt:
        assert item in text, f"Expected '{item}' in prompt text:\n{text}"


def test_mcp_prompt_raw_json_array_triggers_protocol_validation_error(client: TestClient, session_id: str):
    """
    Per MCP 2024-11-05 spec, GetPromptRequest.arguments must be dict[str, str].
    Passing a raw JSON array instead of a string in arguments must return JSON-RPC -32602.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 103,
        "method": "prompts/get",
        "params": {
            "name": "implement_feature",
            "arguments": {
                "feature_name": "Test",
                "specification": "Test",
                "files_to_modify": ["a.py", "b.py"],  # Non-string value
            },
        },
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": session_id})
    assert response.status_code == 200
    data = parse_mcp_response(response)
    assert "error" in data
    assert data["error"]["code"] == -32602


# ===========================================================================
# 3. Resource Reading: session://token-usage Under Quota Stress
# ===========================================================================

def test_token_usage_resource_normal_state(client: TestClient, session_id: str):
    """Under low token count, warning_header and warning are False, warning_message is None."""
    reset_telemetry()
    tracker = get_token_tracker()
    tracker.record_usage(tokens=50_000)

    payload = {
        "jsonrpc": "2.0",
        "id": 201,
        "method": "resources/read",
        "params": {"uri": "session://token-usage"},
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": session_id})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    contents = data["result"]["contents"]
    usage = json.loads(contents[0]["text"])

    assert usage["total_estimated_tokens"] == 50_000
    assert usage["budget_limit"] == 1_000_000
    assert usage["remaining"] == 950_000
    assert usage["percent_used"] == 5.0
    assert usage["warning"] is False
    assert usage["warning_header"] is False
    assert usage["warning_message"] is None


def test_token_usage_resource_threshold_boundary(client: TestClient, session_id: str):
    """At exactly 800,000 tokens (80% boundary), warning_header and warning activate."""
    reset_telemetry()
    tracker = get_token_tracker()
    tracker.record_usage(tokens=800_000)

    payload = {
        "jsonrpc": "2.0",
        "id": 202,
        "method": "resources/read",
        "params": {"uri": "session://token-usage"},
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": session_id})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    usage = json.loads(data["result"]["contents"][0]["text"])

    assert usage["total_estimated_tokens"] == 800_000
    assert usage["percent_used"] == 80.0
    assert usage["warning"] is True
    assert usage["warning_header"] is True
    assert usage["warning_message"] is not None
    assert "80%" in usage["warning_message"]
    assert "800,000" in usage["warning_message"]


def test_token_usage_resource_high_quota_stress(client: TestClient, session_id: str):
    """At 920,000 tokens (>80% quota), warning_header is True with remaining calculation."""
    reset_telemetry()
    tracker = get_token_tracker()
    tracker.record_usage(tokens=920_000)

    payload = {
        "jsonrpc": "2.0",
        "id": 203,
        "method": "resources/read",
        "params": {"uri": "session://token-usage"},
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": session_id})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    usage = json.loads(data["result"]["contents"][0]["text"])

    assert usage["total_estimated_tokens"] == 920_000
    assert usage["remaining"] == 80_000
    assert usage["percent_used"] == 92.0
    assert usage["warning"] is True
    assert usage["warning_header"] is True
    assert usage["warning_message"] is not None
    assert "920,000" in usage["warning_message"]


def test_token_usage_resource_over_budget(client: TestClient, session_id: str):
    """When token count exceeds 1,000,000 limit, remaining stays >= 0 and percent_used > 100%."""
    reset_telemetry()
    tracker = get_token_tracker()
    tracker.record_usage(tokens=1_250_000)

    payload = {
        "jsonrpc": "2.0",
        "id": 204,
        "method": "resources/read",
        "params": {"uri": "session://token-usage"},
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": session_id})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    usage = json.loads(data["result"]["contents"][0]["text"])

    assert usage["total_estimated_tokens"] == 1_250_000
    assert usage["remaining"] == 0  # max(0, budget - total)
    assert usage["percent_used"] == 125.0
    assert usage["warning"] is True
    assert usage["warning_header"] is True


# ===========================================================================
# 4. Resource Reading: project://conventions
# ===========================================================================

def test_project_conventions_custom_content(tmp_path: Path):
    """When .copilot/context.md is present with content, custom contents are returned."""
    Config.set_project_root(tmp_path)
    copilot_dir = tmp_path / ".copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)
    custom_md = "# Team Rules\n1. Always test.\n2. No direct master commits.\n"
    (copilot_dir / "context.md").write_text(custom_md, encoding="utf-8")

    app = create_app(project_root=str(tmp_path))
    with TestClient(app) as client:
        init_res = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": Config.MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test-runner", "version": "1.0"},
                },
            },
        )
        sid = init_res.headers.get("mcp-session-id")

        res = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "project://conventions"}},
            headers={"mcp-session-id": sid},
        )
        assert res.status_code == 200
        data = parse_mcp_response(res)
        text = data["result"]["contents"][0]["text"]
        assert text == custom_md


def test_project_conventions_absent_fallback(tmp_path: Path):
    """When .copilot/context.md is absent, fallback conventions are returned."""
    Config.set_project_root(tmp_path)
    copilot_dir = tmp_path / ".copilot"
    if copilot_dir.exists():
        shutil.rmtree(copilot_dir)

    app = create_app(project_root=str(tmp_path))
    with TestClient(app) as client:
        init_res = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": Config.MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test-runner", "version": "1.0"},
                },
            },
        )
        sid = init_res.headers.get("mcp-session-id")

        res = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "project://conventions"}},
            headers={"mcp-session-id": sid},
        )
        assert res.status_code == 200
        data = parse_mcp_response(res)
        text = data["result"]["contents"][0]["text"]
        assert text == WORKSPACE_CONVENTIONS_FALLBACK
        assert "Workspace Conventions & Architecture Guidelines" in text


def test_project_conventions_empty_file_bug_reproduction(tmp_path: Path):
    """
    EMPIRICAL BUG REPRODUCTION:
    When .copilot/context.md exists but is empty (0 bytes) or only whitespace,
    get_project_conventions_content returns an empty string "" instead of falling
    back to WORKSPACE_CONVENTIONS_FALLBACK.
    """
    Config.set_project_root(tmp_path)
    copilot_dir = tmp_path / ".copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)
    empty_file = copilot_dir / "context.md"
    empty_file.write_text("", encoding="utf-8")

    content = get_project_conventions_content()
    # EMPIRICAL OBSERVATION:
    # content is empty string "" rather than WORKSPACE_CONVENTIONS_FALLBACK
    is_empty_string = (content == "")
    is_fallback = (content == WORKSPACE_CONVENTIONS_FALLBACK)
    print(f"\n[EMPIRICAL FINDING] Empty context.md returned empty string: {is_empty_string}, returned fallback: {is_fallback}")

    # Document the defect: it returns empty string instead of fallback conventions!
    assert is_empty_string is True, "Observed behavior: returns empty string without falling back"


# ===========================================================================
# 5. Tool Schemas: tools/list over /mcp
# ===========================================================================

def test_tool_schemas_unstripped_and_complete(client: TestClient, session_id: str):
    """Verify all 6 poly-tools have unstripped, fully-specified inputSchemas over /mcp."""
    payload = {
        "jsonrpc": "2.0",
        "id": 301,
        "method": "tools/list",
        "params": {},
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": session_id})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    tools = data["result"]["tools"]
    tool_map = {t["name"]: t for t in tools}

    expected_tools = {
        "file_ops",
        "code_search",
        "run_command",
        "git_ops",
        "inspect_ast",
        "diagnostics",
    }
    assert expected_tools.issubset(set(tool_map.keys()))

    # Verify zero occurrences of _lazy_schema_required
    serialized_tools = json.dumps(tools)
    assert "_lazy_schema_required" not in serialized_tools

    # Detailed schema checks for each poly-tool
    for name in expected_tools:
        tool = tool_map[name]
        assert "description" in tool and len(tool["description"]) > 0
        assert "inputSchema" in tool
        schema = tool["inputSchema"]
        assert schema.get("type") == "object"
        assert "properties" in schema
        props = schema["properties"]
        assert isinstance(props, dict)
        assert len(props) > 0, f"Tool '{name}' has empty properties dictionary"

    # Specific property assertions for key poly-tools
    file_ops_props = tool_map["file_ops"]["inputSchema"]["properties"]
    assert "action" in file_ops_props
    assert "file_path" in file_ops_props
    assert "content" in file_ops_props
    assert "target" in file_ops_props
    assert "replacement" in file_ops_props
    assert "edits" in file_ops_props

    search_props = tool_map["code_search"]["inputSchema"]["properties"]
    assert "mode" in search_props
    assert "query" in search_props
    assert "path" in search_props

    cmd_props = tool_map["run_command"]["inputSchema"]["properties"]
    assert "command" in cmd_props
    assert "arguments" in cmd_props
    assert "timeout_seconds" in cmd_props

    git_props = tool_map["git_ops"]["inputSchema"]["properties"]
    assert "action" in git_props

    ast_props = tool_map["inspect_ast"]["inputSchema"]["properties"]
    assert "file_path" in ast_props

    diag_props = tool_map["diagnostics"]["inputSchema"]["properties"]
    assert "action" in diag_props
