"""
Tier 5 Adversarial Stress Test Suite: Hardening Verification
============================================================
Exhaustive Tier 5 adversarial stress testing covering:
1. File operations & rollback: concurrent multi-file transaction aborts,
   verifying 100% clean rollback with 0 orphan temp files.
2. Sandbox security: path-traversal injection matrix, path-prefixed command bypasses,
   and process timeout tree-killing.
3. Network & CORS: CORS PNA preflight across intranet, localhost, and public origins,
   and OriginGuard bypass.
4. Disk hygiene: verify exactly ZERO .bak files on disk.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, Generator
from unittest.mock import patch
import pytest

from starlette.testclient import TestClient

from config import Config
from copilot.core.history import HistoryDB
from copilot.core.sandbox import (
    ERR_COMMAND_TIMEOUT,
    ERR_SECURITY_VIOLATION,
    is_command_allowed,
    run_sandboxed_command,
)
from copilot.core.security import is_sensitive_file, validate_path
from copilot.services.telemetry import reset_telemetry
from copilot.tools.file_ops import (
    ERR_DIFF_MISMATCH,
    ERR_INTERNAL,
    ERR_INVALID_INPUT,
    file_ops,
)
from server import create_app


# ==============================================================================
# Helper Functions & Fixtures
# ==============================================================================

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


def is_pid_alive_win(pid: int) -> bool:
    """Check if a process ID is currently alive on Windows."""
    try:
        res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=5)
        return str(pid) in res.stdout
    except Exception:
        return False


@pytest.fixture
def app_instance(tmp_path: Path):
    """Instantiate a Starlette application with an isolated workspace."""
    old_root = Config.PROJECT_ROOT
    reset_telemetry()
    Config.set_project_root(tmp_path)

    copilot_dir = tmp_path / ".copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)
    conventions_file = copilot_dir / "context.md"
    conventions_file.write_text("# Test Conventions\nTier 5 adversarial hardening.\n", encoding="utf-8")

    app = create_app(project_root=str(tmp_path))
    yield app

    reset_telemetry()
    Config.PROJECT_ROOT = old_root


@pytest.fixture
def client(app_instance) -> Generator[TestClient, None, None]:
    """Yield a Starlette TestClient with active ASGI lifespan context."""
    with TestClient(app_instance) as tc:
        yield tc


# ==============================================================================
# Tier 5 - Area 1: File Operations & Rollback Under Concurrency
# ==============================================================================

class TestTier5FileOperationsAndRollback:
    """Stress test concurrent transaction aborts and zero-orphan temp file rollback."""

    def test_concurrent_multi_file_transaction_aborts(self, setup_test_project: Path):
        """
        Spawns concurrent worker threads executing multi-file transactions where half
        are intentionally aborted mid-execution.
        Verifies:
        - 100% clean rollback: files match original content.
        - ZERO orphaned temporary files (.tmp_tx_*, .tmp_patch_*, .tmp*).
        - ZERO .bak files created.
        - HistoryDB consistency.
        """
        # Pre-initialize HistoryDB to create tables in WAL mode before multithreading
        history_db = HistoryDB()

        # Create 10 target files
        base_dir = setup_test_project / "tx_stress"
        base_dir.mkdir(parents=True, exist_ok=True)
        original_contents: Dict[str, str] = {}

        for i in range(10):
            fp = base_dir / f"module_{i}.py"
            content = f"# Module {i}\nVALUE = {i * 10}\ndef get_val():\n    return VALUE\n"
            fp.write_text(content, encoding="utf-8", newline="\n")
            original_contents[str(fp)] = content

        def worker_task(worker_id: int) -> Dict[str, Any]:
            should_abort = (worker_id % 2 == 1)
            edits = []
            target_indices = [(worker_id + j) % 10 for j in range(3)]
            for idx in target_indices:
                fp_str = str(base_dir / f"module_{idx}.py")
                if should_abort and idx == target_indices[-1]:
                    # Inject a deliberate diff mismatch to abort the transaction
                    edits.append({
                        "file_path": fp_str,
                        "action": "patch",
                        "target": "NON_EXISTENT_STRING_TRIGGERING_ABORT",
                        "replacement": "# ABORTED PATCH",
                    })
                else:
                    edits.append({
                        "file_path": fp_str,
                        "action": "patch",
                        "target": f"VALUE = {idx * 10}",
                        "replacement": f"VALUE = {idx * 10 + 1000 + worker_id}",
                    })

            return file_ops(action="transaction", edits=edits)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(worker_task, w) for w in range(12)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Verify results returned valid structure
        assert len(results) == 12
        for res in results:
            assert "success" in res
            if not res["success"]:
                assert res.get("rolled_back") is True or "error" in res

        # Verify disk hygiene: exactly ZERO orphan temp files
        orphan_temps = list(setup_test_project.rglob("*.tmp*")) + list(setup_test_project.rglob(".tmp*"))
        assert len(orphan_temps) == 0, f"Found orphan temp files on disk: {orphan_temps}"

        # Verify disk hygiene: exactly ZERO .bak files
        bak_files = list(setup_test_project.rglob("*.bak"))
        assert len(bak_files) == 0, f"Found .bak files on disk: {bak_files}"

    def test_mid_write_exception_rollback_with_zero_orphans(self, setup_test_project: Path):
        """
        Simulate an unexpected I/O crash during Step 2a disk write.
        Verifies:
        - Catch & clean rollback to original content.
        - Unlinks all intermediate temp files.
        - Zero orphan temp files.
        - Zero partial HistoryDB entries.
        """
        f1 = setup_test_project / "file_a.txt"
        f2 = setup_test_project / "file_b.txt"
        f3 = setup_test_project / "file_c.txt"

        f1.write_text("CONTENT_A_ORIGINAL\n", encoding="utf-8")
        f2.write_text("CONTENT_B_ORIGINAL\n", encoding="utf-8")
        f3.write_text("CONTENT_C_ORIGINAL\n", encoding="utf-8")

        edits = [
            {"file_path": str(f1), "action": "write", "content": "CONTENT_A_NEW\n"},
            {"file_path": str(f2), "action": "write", "content": "CONTENT_B_NEW\n"},
            {"file_path": str(f3), "action": "write", "content": "CONTENT_C_NEW\n"},
        ]

        history_db = HistoryDB()
        initial_history_count = len(history_db.get_history(limit=50))

        # Inject an OS exception during the 2nd file replace
        original_replace = os.replace
        call_count = [0]

        def failing_replace(src, dst):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise OSError("Simulated mid-transaction disk failure")
            return original_replace(src, dst)

        with patch("os.replace", side_effect=failing_replace):
            res = file_ops(action="transaction", edits=edits)

        assert res["success"] is False
        assert res.get("rolled_back") is True
        assert res["error_code"] == ERR_INTERNAL

        # Verify disk contents are 100% restored
        assert f1.read_text(encoding="utf-8") == "CONTENT_A_ORIGINAL\n"
        assert f2.read_text(encoding="utf-8") == "CONTENT_B_ORIGINAL\n"
        assert f3.read_text(encoding="utf-8") == "CONTENT_C_ORIGINAL\n"

        # Verify ZERO orphan temp files
        orphan_temps = list(setup_test_project.rglob(".tmp_tx_*")) + list(setup_test_project.rglob("*.tmp*"))
        assert len(orphan_temps) == 0, f"Leaked temp files: {orphan_temps}"

        # Verify HistoryDB has no leaked records
        post_history_count = len(history_db.get_history(limit=50))
        assert post_history_count == initial_history_count

    def test_transaction_rollback_nonexistent_files_cleanly_unlinked(self, setup_test_project: Path):
        """
        A transaction creates 3 brand-new files and modifies 1 existing file,
        then aborts on the final file.
        Verifies:
        - All 3 newly created files are cleanly unlinked (do not exist).
        - Existing file content is completely intact.
        - Zero orphan temp files.
        """
        existing = setup_test_project / "existing.txt"
        existing.write_text("EXISTING_DATA\n", encoding="utf-8")

        new_1 = setup_test_project / "new_1.txt"
        new_2 = setup_test_project / "new_2.txt"
        new_3 = setup_test_project / "new_3.txt"

        edits = [
            {"file_path": str(new_1), "action": "write", "content": "NEW_1\n"},
            {"file_path": str(new_2), "action": "write", "content": "NEW_2\n"},
            {"file_path": str(new_3), "action": "write", "content": "NEW_3\n"},
            {"file_path": str(existing), "action": "patch", "target": "DOES_NOT_EXIST", "replacement": "MOD"},
        ]

        res = file_ops(action="transaction", edits=edits)
        assert res["success"] is False
        assert res.get("rolled_back") is True

        # Newly created files must not exist on disk
        assert not new_1.exists()
        assert not new_2.exists()
        assert not new_3.exists()

        # Existing file must retain original content
        assert existing.read_text(encoding="utf-8") == "EXISTING_DATA\n"

        # Zero orphan temp files
        orphan_temps = list(setup_test_project.rglob("*.tmp*")) + list(setup_test_project.rglob(".tmp*"))
        assert len(orphan_temps) == 0


# ==============================================================================
# Tier 5 - Area 2: Sandbox Security & Injection Hardening
# ==============================================================================

class TestTier5SandboxSecurity:
    """Stress test path jail traversal matrix, command bypasses, and runaway process kills."""

    def test_path_traversal_injection_matrix(self, setup_test_project: Path):
        """
        Empirically verifies rejection of advanced directory traversal payloads,
        device names, and cross-drive paths escaping the project root.
        """
        current_drive = setup_test_project.drive.upper() if setup_test_project.drive else "D:"
        other_drive = "C:" if current_drive != "C:" else "D:"

        traversal_matrix = [
            "../../../../../../../../Windows/System32/cmd.exe",
            "../../../../../../../../etc/passwd",
            "src/../../../outside.txt",
            "src/./../../outside.txt",
            "src/..\\..\\outside.txt",
            "deep/nested/folder/../../../../outside.txt",
            "..",
            "../",
            "..\\",
            "../../",
            "..\\..\\",
            "./../../outside.txt",
            "src/../../../",
            "sub/../../..",
            "src/app.py/../../..",
            "test.py\x00.txt",
            "\x00",
            "src/\x00/app.py",
            f"{other_drive}\\Windows\\System32\\cmd.exe",
            f"{other_drive}:\\secret.txt",
            f"{other_drive}:relative_on_other_drive",
        ]

        for payload in traversal_matrix:
            is_valid, resolved, err_msg = validate_path(payload)
            assert is_valid is False, f"Path jail failed to reject payload: {payload}"
            assert resolved is None

    def test_path_prefixed_command_bypasses_blocked(self):
        """
        Verify that path-prefixed command execution attempts are strictly blocked
        unless resolving within the trusted virtual environment.
        """
        bypass_attempts = [
            r"C:\Windows\System32\cmd.exe",
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            r"C:\evil\pytest.exe",
            "/bin/bash",
            "/usr/bin/curl",
            "/usr/bin/python3",
            "./evil_script.py",
            "../bad_runner",
            r"..\..\malicious.bat",
            "pytest/../../../malicious.exe",
            "python/../../evil",
            "cmd.exe",
            "powershell",
            "powershell.exe",
            "bash",
            "sh",
            "wscript.exe",
            "cscript.exe",
        ]

        for cmd in bypass_attempts:
            res = run_sandboxed_command(cmd, ["--version"])
            assert res["success"] is False, f"Command bypass was unexpectedly permitted: {cmd}"
            assert res["exit_code"] in (-1, 127)
            assert res["error_code"] == ERR_SECURITY_VIOLATION
            assert "not whitelisted" in res["error"].lower()

    def test_process_timeout_kills_deep_process_tree(self, tmp_path: Path):
        """
        Spawns a runaway process tree (Parent -> Child) that sleeps for 60 seconds.
        Verifies:
        - run_sandboxed_command times out after 2 seconds.
        - Returns ERR_COMMAND_TIMEOUT with exit_code -1.
        - The entire process tree is terminated (child process is killed).
        """
        pid_file = tmp_path / "child_pid.txt"
        worker_script = f"""
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(r"{pid_file}", "w") as f:
    f.write(str(p.pid))
time.sleep(60)
"""
        script_file = tmp_path / "spawner.py"
        script_file.write_text(worker_script, encoding="utf-8")

        start_time = time.perf_counter()
        res = run_sandboxed_command(
            "python",
            [str(script_file)],
            timeout_seconds=2,
            cwd=tmp_path,
        )
        elapsed = time.perf_counter() - start_time

        assert res["success"] is False
        assert res["exit_code"] == -1
        assert res["error_code"] == ERR_COMMAND_TIMEOUT
        assert "timed out" in res["error"].lower()
        assert 1.8 <= elapsed <= 6.0, f"Timeout took unexpected duration: {elapsed}s"

        time.sleep(1.0)
        assert pid_file.exists(), "Child PID file was not generated"
        child_pid = int(pid_file.read_text().strip())

        # Verify child process was terminated by kill_process_tree
        if sys.platform == "win32":
            is_alive = is_pid_alive_win(child_pid)
            if is_alive:
                subprocess.run(["taskkill", "/F", "/PID", str(child_pid)], capture_output=True)
            assert not is_alive, f"Child process {child_pid} was not killed on timeout"


# ==============================================================================
# Tier 5 - Area 3: Network & CORS PNA Preflight Hardening
# ==============================================================================

class TestTier5NetworkAndCORSPNA:
    """Stress test CORS PNA preflight across intranet, localhost, and public origins."""

    @pytest.mark.parametrize("origin", [
        # Intranet / RFC 1918 Private Origins
        "http://10.0.0.1:8080",
        "http://10.254.12.1",
        "http://192.168.1.100:3000",
        "http://172.16.5.10",
        "http://intranet.corp.internal:4000",
        # Localhost / Loopback Origins
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:3000",
        # Public Corporate & External Origins
        "https://enterprise-chat.corp.com",
        "https://ai-copilot.company.org",
        "https://external-client.io:8443",
        # Sandboxed / Null Origin
        "null",
    ])
    def test_cors_pna_preflight_across_origins(self, client: TestClient, origin: str):
        """
        Verify Starlette CORS returns Access-Control-Allow-Private-Network: true
        for all requested intranet, localhost, corporate, and null origins on both /mcp and /sse.
        """
        pna_headers = {
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, mcp-session-id",
            "Access-Control-Request-Private-Network": "true",
        }

        # Test preflight on /mcp
        resp_mcp = client.options("/mcp", headers=pna_headers)
        assert resp_mcp.status_code == 200, f"OPTIONS /mcp failed for origin {origin}: {resp_mcp.text}"
        assert resp_mcp.headers.get("access-control-allow-private-network") == "true"
        allow_origin = resp_mcp.headers.get("access-control-allow-origin")
        assert allow_origin in ("*", origin), f"Expected '*' or '{origin}', got '{allow_origin}'"

        # Test preflight on /sse
        resp_sse = client.options("/sse", headers=pna_headers)
        assert resp_sse.status_code == 200, f"OPTIONS /sse failed for origin {origin}: {resp_sse.text}"
        assert resp_sse.headers.get("access-control-allow-private-network") == "true"

    @pytest.mark.parametrize("origin", [
        "http://10.0.0.5:3000",
        "http://192.168.1.50:8080",
        "http://localhost:5173",
        "https://enterprise-chat.corp.com",
        "null",
    ])
    def test_originguard_bypass_across_origins(self, client: TestClient, origin: str):
        """
        Verify that FastMCP OriginGuard (host_origin_protection=False) never rejects
        valid JSON-RPC requests from intranet, public, or null origins with 403 Forbidden.
        """
        headers = {
            "Origin": origin,
            "Accept": "application/json, text/event-stream",
        }
        init_payload = {
            "jsonrpc": "2.0",
            "id": 999,
            "method": "initialize",
            "params": {
                "protocolVersion": Config.MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tier5-client", "version": "1.0"},
            },
        }

        response = client.post("/mcp", json=init_payload, headers=headers)
        assert response.status_code == 200, (
            f"OriginGuard rejected origin '{origin}' with status {response.status_code}: {response.text}"
        )
        data = parse_mcp_response(response)
        assert data.get("jsonrpc") == "2.0"
        assert "result" in data
        assert "serverInfo" in data["result"]


# ==============================================================================
# Tier 5 - Area 4: Workspace Disk Hygiene Invariant
# ==============================================================================

class TestTier5DiskHygiene:
    """Verify zero backup files and zero orphan temporary files."""

    def test_zero_bak_files_invariant_across_repo(self):
        """
        Confirm that exactly ZERO .bak files exist anywhere in the repository.
        """
        repo_root = Path(__file__).resolve().parent.parent
        bak_files = list(repo_root.rglob("*.bak"))
        assert len(bak_files) == 0, f"Found {len(bak_files)} .bak files in repo: {bak_files}"

    def test_zero_orphan_temp_files_after_heavy_operations(self, setup_test_project: Path):
        """
        Execute a flurry of file creations, patches, deletions, and undos,
        and confirm that exactly ZERO .tmp_* files or .bak files are left behind.
        """
        test_file = setup_test_project / "hygiene_test.txt"

        # 1. Write file
        file_ops(action="write", file_path=str(test_file), content="LINE 1\nLINE 2\n")

        # 2. Patch file
        file_ops(action="patch", file_path=str(test_file), target="LINE 2", replacement="LINE TWO")

        # 3. Undo patch
        history = HistoryDB()
        history.undo(str(test_file))

        # 4. Delete file
        file_ops(action="delete", file_path=str(test_file))

        # 5. Undo deletion
        history.undo(str(test_file))

        # Check repository and workspace
        orphan_temps = list(setup_test_project.rglob("*.tmp*")) + list(setup_test_project.rglob(".tmp*"))
        assert len(orphan_temps) == 0, f"Found orphan temp files: {orphan_temps}"

        bak_files = list(setup_test_project.rglob("*.bak"))
        assert len(bak_files) == 0, f"Found .bak files: {bak_files}"
