"""
Authoritative Test Suite 7: Server Protocol, Network, Dual-Transport & CORS PNA.
================================================================================
Tests Starlette ASGI application mounting:
- GET /health JSON endpoint
- CORS preflight OPTIONS request with Access-Control-Request-Private-Network: true
- FastMCP OriginGuard bypass (host_origin_protection=False)
- MCP Streamable-HTTP (/mcp) and Server-Sent Events (/sse) dual transports
- MCP initialize handshake & session ID lifecycle
- MCP prompt listing and resolution (implement_feature, debug_issue)
- MCP resource reading (session://token-usage, project://conventions)
- Unstripped tool schemas in tools/list
- In-process execution on Windows without port conflicts
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Generator
import pytest

from starlette.testclient import TestClient

from config import Config
from copilot.services.telemetry import reset_telemetry
from server import create_app

try:
    import httpx
except ImportError:
    import httpx2 as httpx


def parse_mcp_response(response) -> Dict[str, Any]:
    """Helper to parse either SSE-framed ('data: {...}') or direct JSON responses."""
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
def app_instance(tmp_path: Path):
    """Instantiate a test Starlette application pointing to a sandbox workspace."""
    old_root = Config.PROJECT_ROOT
    reset_telemetry()
    Config.set_project_root(tmp_path)

    # Populate conventions file in workspace
    copilot_dir = tmp_path / ".copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)
    conventions_file = copilot_dir / "context.md"
    conventions_file.write_text("# Test Conventions\nAlways write tests first.\n", encoding="utf-8")

    app = create_app(project_root=str(tmp_path))
    yield app

    reset_telemetry()
    Config.PROJECT_ROOT = old_root


@pytest.fixture
def client(app_instance) -> Generator[TestClient, None, None]:
    """Yield a Starlette TestClient with active ASGI lifespan context."""
    with TestClient(app_instance) as tc:
        yield tc


@pytest.fixture
def mcp_session(client: TestClient) -> str:
    """Perform MCP handshake and return active session ID."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": Config.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-runner", "version": "1.0.0"},
        },
    }
    response = client.post("/mcp", json=payload)
    assert response.status_code == 200, f"Handshake failed: {response.text}"
    session_id = response.headers.get("mcp-session-id")
    assert session_id is not None, "MCP initialize must return mcp-session-id header"
    return session_id


# ---------------------------------------------------------------------------
# Test Cases 1 - 19
# ---------------------------------------------------------------------------

def test_server_http_health_check(client: TestClient):
    """1. GET /health returns standardized server status JSON."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["server"] == "FastMCP Code Copilot"
    assert data["version"] == "0.1.0"

    # Also verify root GET / aliases health check
    root_res = client.get("/")
    assert root_res.status_code == 200
    assert root_res.json() == data


def test_server_http_health_unconfigured(client: TestClient):
    """2. GET /health when unconfigured returns graceful JSON."""
    old_root = Config.PROJECT_ROOT
    try:
        Config.PROJECT_ROOT = None
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
    finally:
        Config.PROJECT_ROOT = old_root


def test_server_cors_private_network_access(client: TestClient):
    """3. CORS preflight OPTIONS request returns Access-Control-Allow-Private-Network."""
    headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type, mcp-session-id",
        "Access-Control-Request-Private-Network": "true",
    }
    response = client.options("/mcp", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-private-network") == "true"
    assert response.headers.get("access-control-allow-origin") == "*"


def test_server_cors_standard_options(client: TestClient):
    """4. Standard CORS OPTIONS request returns allowed origins and methods."""
    headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "GET",
    }
    response = client.options("/health", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"
    assert "GET" in response.headers.get("access-control-allow-methods", "")


def test_server_fastmcp_origin_guard_bypass(client: TestClient):
    """5. FastMCP OriginGuard bypass allows enterprise intranet origins without 403."""
    headers = {
        "Origin": "https://chatbot.internal.corp",
        "Accept": "application/json, text/event-stream",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 100,
        "method": "initialize",
        "params": {
            "protocolVersion": Config.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "intranet-bot", "version": "2.0"},
        },
    }
    response = client.post("/mcp", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.headers.get("mcp-session-id") is not None


def test_server_mcp_initialize(client: TestClient):
    """6. MCP initialize handshake negotiates protocol version and returns capabilities."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": Config.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-suite", "version": "1.0"},
        },
    }
    response = client.post("/mcp", json=payload)
    assert response.status_code == 200
    assert "mcp-session-id" in response.headers

    data = parse_mcp_response(response)
    assert "result" in data
    assert data["result"]["protocolVersion"] == Config.MCP_PROTOCOL_VERSION
    assert "capabilities" in data["result"]
    assert "serverInfo" in data["result"]


def test_server_dual_transport_sse(client: TestClient):
    """7. Server-Sent Events endpoint /sse opens an active text/event-stream."""
    with client.stream("GET", "/sse", headers={"Accept": "text/event-stream"}) as stream:
        assert stream.status_code == 200
        content_type = stream.headers.get("content-type", "")
        assert "text/event-stream" in content_type

        # Verify initial endpoint advertisement event
        lines = stream.iter_lines()
        first_line = next(lines)
        assert first_line.startswith("event:")
        assert "endpoint" in first_line


def test_server_mcp_prompts_list(client: TestClient, mcp_session: str):
    """8. MCP prompts/list returns registered implement_feature and debug_issue prompts."""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "prompts/list",
        "params": {},
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    prompts = data["result"]["prompts"]
    prompt_names = [p["name"] for p in prompts]

    assert "implement_feature" in prompt_names
    assert "debug_issue" in prompt_names

    # Check argument schema of implement_feature
    impl_prompt = next(p for p in prompts if p["name"] == "implement_feature")
    arg_names = [a["name"] for a in impl_prompt.get("arguments", [])]
    assert "feature_name" in arg_names
    assert "specification" in arg_names


def test_server_mcp_prompt_get_implement_feature(client: TestClient, mcp_session: str):
    """9. MCP prompts/get for implement_feature returns structured workflow guidance."""
    payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "prompts/get",
        "params": {
            "name": "implement_feature",
            "arguments": {
                "feature_name": "UserAuthentication",
                "specification": "Add OAuth2 authentication flow with token renewal.",
                "files_to_modify": "src/auth.py, src/routes.py",
            },
        },
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    messages = data["result"]["messages"]
    assert len(messages) >= 1

    content_text = messages[0]["content"]["text"]
    assert "UserAuthentication" in content_text
    assert "OAuth2 authentication" in content_text
    assert "inspect_ast" in content_text
    assert "file_ops" in content_text
    assert "run_command" in content_text


def test_server_mcp_prompt_get_debug_issue(client: TestClient, mcp_session: str):
    """10. MCP prompts/get for debug_issue returns structured failure triage guidance."""
    payload = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "prompts/get",
        "params": {
            "name": "debug_issue",
            "arguments": {
                "error_message": "IndexError: list index out of range in fetch_records()",
                "reproducing_command": "pytest tests/test_db.py",
                "suspected_files": "src/db.py",
            },
        },
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    messages = data["result"]["messages"]
    assert len(messages) >= 1

    content_text = messages[0]["content"]["text"]
    assert "IndexError: list index out of range" in content_text
    assert "pytest tests/test_db.py" in content_text
    assert "diagnostics" in content_text
    assert "code_search" in content_text


def test_server_mcp_prompt_get_unknown(client: TestClient, mcp_session: str):
    """11. MCP prompts/get for unknown prompt returns JSON-RPC error code -32602."""
    payload = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "prompts/get",
        "params": {
            "name": "nonexistent_custom_prompt",
            "arguments": {},
        },
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    assert "error" in data
    assert data["error"]["code"] == -32602


def test_server_mcp_resources_list(client: TestClient, mcp_session: str):
    """12. MCP resources/list returns token-usage and workspace conventions resources."""
    payload = {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "resources/list",
        "params": {},
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    resources = data["result"]["resources"]
    uris = [r["uri"] for r in resources]

    assert "session://token-usage" in uris
    assert "project://conventions" in uris


def test_server_mcp_resource_read_token_usage(client: TestClient, mcp_session: str):
    """13. MCP resources/read for session://token-usage returns live budget metrics JSON."""
    payload = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "resources/read",
        "params": {
            "uri": "session://token-usage",
        },
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    contents = data["result"]["contents"]
    assert len(contents) >= 1
    assert contents[0]["uri"] == "session://token-usage"
    assert contents[0]["mimeType"] == "application/json"

    token_data = json.loads(contents[0]["text"])
    assert "total_estimated_tokens" in token_data
    assert token_data["budget_limit"] == 1_000_000
    assert "remaining" in token_data
    assert "percent_used" in token_data


def test_server_mcp_resource_read_project_conventions(client: TestClient, mcp_session: str):
    """14. MCP resources/read for project://conventions returns workspace conventions markdown."""
    payload = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "resources/read",
        "params": {
            "uri": "project://conventions",
        },
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    contents = data["result"]["contents"]
    assert len(contents) >= 1
    assert contents[0]["uri"] == "project://conventions"
    assert contents[0]["mimeType"] == "text/markdown"

    assert "Always write tests first." in contents[0]["text"]


def test_server_mcp_resource_read_unknown(client: TestClient, mcp_session: str):
    """15. MCP resources/read for unknown resource returns JSON-RPC error code -32602."""
    payload = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "resources/read",
        "params": {
            "uri": "unknown://nonexistent_resource",
        },
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    assert "error" in data
    assert data["error"]["code"] == -32602


def test_server_tool_schemas_unstripped(client: TestClient, mcp_session: str):
    """16. MCP tools/list returns all 6 tools with complete unstripped Pydantic inputSchemas."""
    payload = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/list",
        "params": {},
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    tools = data["result"]["tools"]
    tool_names = {t["name"] for t in tools}

    expected_tools = {"file_ops", "code_search", "run_command", "git_ops", "inspect_ast", "diagnostics"}
    assert expected_tools.issubset(tool_names)

    for tool in tools:
        assert "inputSchema" in tool, f"Tool {tool['name']} missing inputSchema"
        schema = tool["inputSchema"]
        assert schema.get("type") == "object"
        assert "properties" in schema
        assert len(schema["properties"]) > 0, f"Tool {tool['name']} has empty properties"
        assert "_lazy_schema_required" not in json.dumps(tool)


def test_server_mcp_missing_session_id(client: TestClient):
    """17. Request to /mcp without session ID returns HTTP 400 with JSON-RPC error -32600."""
    payload = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/list",
        "params": {},
    }
    response = client.post("/mcp", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == -32600
    assert "Missing session ID" in data["error"]["message"]


def test_server_mcp_unknown_method(client: TestClient, mcp_session: str):
    """18. Request with unknown method returns JSON-RPC error code -32601."""
    payload = {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "custom/unknown_endpoint",
        "params": {},
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200

    data = parse_mcp_response(response)
    assert "error" in data
    assert data["error"]["code"] == -32601


def test_server_windows_in_process_no_port_conflict(app_instance):
    """19. Concurrent and sequential in-process TestClients execute without port conflicts."""
    for i in range(3):
        with TestClient(app_instance) as tc:
            health = tc.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"
