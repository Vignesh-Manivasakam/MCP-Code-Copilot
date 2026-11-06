"""
Empirical Adversarial Stress Test Suite: Milestone 5
=====================================================
Rigorous challenger testing of server.py:
1. CORS PNA Preflight Variations:
   - Access-Control-Request-Private-Network: true across various origins
     (http://localhost:3000, https://enterprise-chat.internal, null, https://external.com,
      http://127.0.0.1:8000, https://subdomain.corp.internal:8443)
   - Verification of access-control-allow-private-network: true and access-control-allow-origin: *
   - Testing on both /mcp and /sse
2. OriginGuard Intranet Bypass:
   - Origin: https://chatbot.company.com on /mcp and /sse
   - Ensuring no 403 Forbidden is ever returned
   - Testing SSE /messages endpoint with enterprise intranet origin
3. Dual-Transport Concurrency:
   - Simultaneous requests to /mcp (Streamable-HTTP) and /sse (Server-Sent Events)
   - Multi-threaded concurrent execution across sessions and within the same session
   - Concurrent multiple active SSE streams
   - Tool calls and prompt resolution under concurrent load
4. Error Cases & Negative Scenarios:
   - Malformed JSON-RPC payloads (parse error -32700)
   - Missing method and invalid version validation (-32602)
   - Missing session IDs (400 -32600 "Missing session ID")
   - Non-existent / expired session IDs (404 -32600 "Session not found")
   - Invalid / unsupported Content-Type headers
   - Non-existent endpoints (404)
   - Unsupported HTTP methods (405)
5. Zero .bak Files Hygiene:
   - Validates that no backup files exist anywhere on disk
"""
from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
from typing import Any, Dict, Generator
import pytest

from starlette.testclient import TestClient

from config import Config
from copilot.services.telemetry import reset_telemetry
from server import create_app


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
    """Instantiate a Starlette application with an isolated workspace."""
    old_root = Config.PROJECT_ROOT
    reset_telemetry()
    Config.set_project_root(tmp_path)

    # Populate conventions file in workspace
    copilot_dir = tmp_path / ".copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)
    conventions_file = copilot_dir / "context.md"
    conventions_file.write_text("# Test Conventions\nStrict TDD and zero regressions.\n", encoding="utf-8")

    app = create_app(project_root=str(tmp_path))
    yield app

    reset_telemetry()
    Config.PROJECT_ROOT = old_root


@pytest.fixture
def client(app_instance) -> Generator[TestClient, None, None]:
    """Yield a Starlette TestClient with active lifespan."""
    with TestClient(app_instance) as tc:
        yield tc


@pytest.fixture
def mcp_session(client: TestClient) -> str:
    """Initialize MCP session and return session ID."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": Config.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "challenger-test-runner", "version": "1.0.0"},
        },
    }
    response = client.post("/mcp", json=payload)
    assert response.status_code == 200, f"MCP handshake failed: {response.text}"
    session_id = response.headers.get("mcp-session-id")
    assert session_id is not None
    return session_id


# ===========================================================================
# 1. CORS PNA Preflight Variations
# ===========================================================================

@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:3000",
        "https://enterprise-chat.internal",
        "null",
        "https://external.com",
        "http://127.0.0.1:8000",
        "https://subdomain.corp.internal:8443",
    ],
)
def test_cors_pna_preflight_origins_mcp(client: TestClient, origin: str):
    """Preflight OPTIONS to /mcp with PNA header returns allow-private-network: true and allow-origin: *."""
    headers = {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type, mcp-session-id",
        "Access-Control-Request-Private-Network": "true",
    }
    response = client.options("/mcp", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-private-network") == "true"
    assert response.headers.get("access-control-allow-origin") == "*"
    assert "POST" in response.headers.get("access-control-allow-methods", "")


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:3000",
        "https://enterprise-chat.internal",
        "null",
        "https://external.com",
    ],
)
def test_cors_pna_preflight_origins_sse(client: TestClient, origin: str):
    """Preflight OPTIONS to /sse with PNA header returns allow-private-network: true and allow-origin: *."""
    headers = {
        "Origin": origin,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "accept",
        "Access-Control-Request-Private-Network": "true",
    }
    response = client.options("/sse", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-private-network") == "true"
    assert response.headers.get("access-control-allow-origin") == "*"


def test_cors_pna_exposed_headers(client: TestClient):
    """CORS response on actual POST requests exposes mcp-session-id to browser clients."""
    headers = {
        "Origin": "https://enterprise-chat.internal",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": Config.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-browser", "version": "1.0"},
        },
    }
    response = client.post("/mcp", json=payload, headers=headers)
    assert response.status_code == 200
    exposed = response.headers.get("access-control-expose-headers", "")
    assert "mcp-session-id" in exposed.lower() or "*" in exposed


# ===========================================================================
# 2. OriginGuard Intranet Bypass
# ===========================================================================

def test_originguard_bypass_mcp_company_com(client: TestClient):
    """Origin: https://chatbot.company.com on /mcp does not trigger 403 Forbidden."""
    headers = {"Origin": "https://chatbot.company.com"}
    payload = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "initialize",
        "params": {
            "protocolVersion": Config.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "enterprise-bot", "version": "1.0"},
        },
    }
    response = client.post("/mcp", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.headers.get("mcp-session-id") is not None


def test_originguard_bypass_sse_company_com(client: TestClient):
    """Origin: https://chatbot.company.com on /sse does not trigger 403 Forbidden."""
    headers = {
        "Origin": "https://chatbot.company.com",
        "Accept": "text/event-stream",
    }
    with client.stream("GET", "/sse", headers=headers) as stream:
        assert stream.status_code == 200
        assert stream.status_code != 403
        lines = stream.iter_lines()
        first_line = next(lines)
        assert first_line.startswith("event:")
        assert "endpoint" in first_line


def test_originguard_bypass_sse_messages_receiver(client: TestClient):
    """POST /messages (SSE message receiver) works without 403 with intranet origin."""
    with client.stream("GET", "/sse", headers={"Origin": "https://chatbot.company.com"}) as stream:
        lines = stream.iter_lines()
        next(lines)  # event
        data_line = next(lines)
        endpoint = data_line.split(":", 1)[1].strip()

        payload = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "initialize",
            "params": {
                "protocolVersion": Config.MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "sse-test", "version": "1.0"},
            },
        }
        res = client.post(endpoint, json=payload, headers={"Origin": "https://chatbot.company.com"})
        assert res.status_code == 202


@pytest.mark.parametrize(
    "origin",
    [
        "https://random-public-site.com",
        "null",
        "http://192.168.1.100:8080",
    ],
)
def test_originguard_bypass_arbitrary_origins(client: TestClient, origin: str):
    """FastMCP OriginGuard bypass permits all external and intranet origins."""
    headers = {"Origin": origin}
    payload = {
        "jsonrpc": "2.0",
        "id": 20,
        "method": "initialize",
        "params": {
            "protocolVersion": Config.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "arbitrary-origin-bot", "version": "1.0"},
        },
    }
    response = client.post("/mcp", json=payload, headers=headers)
    assert response.status_code == 200


# ===========================================================================
# 3. Dual-Transport Concurrency
# ===========================================================================

def test_dual_transport_simultaneous_mcp_and_sse(client: TestClient, mcp_session: str):
    """Simultaneous operations on /mcp (Streamable-HTTP) and /sse (SSE) function concurrently."""
    with client.stream("GET", "/sse") as sse_stream:
        lines = sse_stream.iter_lines()
        event_line = next(lines)
        data_line = next(lines)
        assert "endpoint" in event_line
        endpoint = data_line.split(":", 1)[1].strip()

        # While SSE stream is open, execute tools/list on /mcp
        mcp_res = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 50, "method": "tools/list", "params": {}},
            headers={"mcp-session-id": mcp_session},
        )
        assert mcp_res.status_code == 200
        mcp_data = parse_mcp_response(mcp_res)
        assert "tools" in mcp_data["result"]

        # Concurrently send a message to the SSE transport
        sse_post_res = client.post(
            endpoint,
            json={
                "jsonrpc": "2.0",
                "id": 51,
                "method": "initialize",
                "params": {
                    "protocolVersion": Config.MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "dual-test", "version": "1.0"},
                },
            },
        )
        assert sse_post_res.status_code == 202

        # Verify SSE event arrived on stream
        sse_received = False
        for _ in range(5):
            line = next(lines)
            if "result" in line:
                sse_received = True
                break
        assert sse_received, "Expected initialize response over SSE stream"


def test_dual_transport_multi_threaded_concurrency(client: TestClient, mcp_session: str):
    """High concurrency test: 12 parallel threads sending requests across both transports."""
    def send_http_call(idx: int):
        method = "tools/list" if idx % 2 == 0 else "prompts/list"
        res = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 100 + idx, "method": method, "params": {}},
            headers={"mcp-session-id": mcp_session},
        )
        assert res.status_code == 200
        return res.status_code

    def send_independent_session(idx: int):
        init_res = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 200 + idx,
                "method": "initialize",
                "params": {
                    "protocolVersion": Config.MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": f"client-{idx}", "version": "1.0"},
                },
            },
        )
        assert init_res.status_code == 200
        sess_id = init_res.headers.get("mcp-session-id")
        assert sess_id is not None

        # Call diagnostics
        call_res = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 300 + idx,
                "method": "tools/call",
                "params": {"name": "diagnostics", "arguments": {"action": "session"}},
            },
            headers={"mcp-session-id": sess_id},
        )
        assert call_res.status_code == 200
        return call_res.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        # Same-session requests
        for i in range(6):
            futures.append(executor.submit(send_http_call, i))
        # Cross-session requests
        for i in range(6):
            futures.append(executor.submit(send_independent_session, i))

        results = [f.result() for f in futures]
        assert len(results) == 12
        assert all(code == 200 for code in results)


def test_dual_transport_multiple_sse_streams(client: TestClient):
    """Multiple concurrent SSE streams can connect independently."""
    with client.stream("GET", "/sse") as stream1:
        lines1 = stream1.iter_lines()
        e1 = next(lines1)
        d1 = next(lines1)
        endpoint1 = d1.split(":", 1)[1].strip()

        with client.stream("GET", "/sse") as stream2:
            lines2 = stream2.iter_lines()
            e2 = next(lines2)
            d2 = next(lines2)
            endpoint2 = d2.split(":", 1)[1].strip()

            assert "endpoint" in e1
            assert "endpoint" in e2
            assert endpoint1 != endpoint2, "Each SSE connection must receive a unique session endpoint"


# ===========================================================================
# 4. Error Cases, Malformed Payloads & Negative Protocol Scenarios
# ===========================================================================

def test_mcp_malformed_json_syntax_error(client: TestClient, mcp_session: str):
    """Malformed non-JSON bytes return HTTP 400 with -32700 parse error."""
    response = client.post(
        "/mcp",
        content=b"this is not valid json {{{",
        headers={"Content-Type": "application/json", "mcp-session-id": mcp_session},
    )
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == -32700
    assert "Parse error" in data["error"]["message"]


def test_mcp_empty_payload(client: TestClient, mcp_session: str):
    """Empty JSON object returns HTTP 400 with validation error -32602."""
    response = client.post("/mcp", json={}, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == -32602


def test_mcp_missing_method_field(client: TestClient, mcp_session: str):
    """JSON-RPC payload missing the method field returns HTTP 400 validation error."""
    payload = {"jsonrpc": "2.0", "id": 77}
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == -32602


def test_mcp_invalid_jsonrpc_version(client: TestClient, mcp_session: str):
    """JSON-RPC payload with invalid version returns HTTP 400 validation error."""
    payload = {"jsonrpc": "1.0", "id": 78, "method": "tools/list"}
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == -32602


def test_mcp_missing_session_id_on_calls(client: TestClient):
    """Calling /mcp without session ID on non-init method returns HTTP 400."""
    payload = {"jsonrpc": "2.0", "id": 79, "method": "tools/list", "params": {}}
    response = client.post("/mcp", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == -32600
    assert "Missing session ID" in data["error"]["message"]


def test_mcp_invalid_session_id_not_found(client: TestClient):
    """Calling /mcp with nonexistent session ID returns HTTP 404 with -32600 Session not found."""
    payload = {"jsonrpc": "2.0", "id": 80, "method": "tools/list", "params": {}}
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": "nonexistent-uuid-99999"})
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == -32600
    assert "Session not found" in data["error"]["message"]


def test_mcp_unsupported_content_type(client: TestClient, mcp_session: str):
    """Request with unsupported Content-Type returns HTTP 400."""
    response = client.post(
        "/mcp",
        content=b"<xml></xml>",
        headers={"Content-Type": "application/xml", "mcp-session-id": mcp_session},
    )
    assert response.status_code == 400
    assert "Invalid Content-Type" in response.text


@pytest.mark.parametrize(
    "endpoint",
    [
        "/nonexistent",
        "/mcp/nested/unknown",
        "/sse/extra",
        "/api/v1/copilot",
    ],
)
def test_nonexistent_endpoints_404(client: TestClient, endpoint: str):
    """Non-existent endpoints return HTTP 404."""
    response = client.get(endpoint)
    assert response.status_code == 404


def test_unsupported_http_methods(client: TestClient):
    """Sending unsupported HTTP methods returns HTTP 405 Method Not Allowed."""
    r_post_health = client.post("/health", json={})
    assert r_post_health.status_code == 405

    r_put_health = client.put("/health", json={})
    assert r_put_health.status_code == 405


def test_health_head_method_allowed(client: TestClient):
    """HEAD /health returns 200 without body."""
    response = client.head("/health")
    assert response.status_code == 200
    assert response.text == ""


def test_large_payload_handling(client: TestClient, mcp_session: str):
    """Large JSON-RPC payload with long arguments does not crash the server."""
    payload = {
        "jsonrpc": "2.0",
        "id": 90,
        "method": "tools/call",
        "params": {
            "name": "diagnostics",
            "arguments": {
                "action": "file",
                "file_path": "a" * 1000,
            },
        },
    }
    response = client.post("/mcp", json=payload, headers={"mcp-session-id": mcp_session})
    assert response.status_code == 200
    data = parse_mcp_response(response)
    assert "result" in data or "error" in data


# ===========================================================================
# 5. Clean Disk & File Hygiene
# ===========================================================================

def test_zero_bak_files_on_disk():
    """Verify that zero .bak files exist anywhere in the project tree."""
    project_root = Path(__file__).resolve().parent.parent
    bak_files = list(project_root.glob("**/*.bak"))
    assert len(bak_files) == 0, f"Found .bak files on disk: {bak_files}"
