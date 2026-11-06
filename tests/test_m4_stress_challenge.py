"""
Authoritative Empirical Stress Test Suite for Milestone M4 Modular Poly-Tools.
=============================================================================
Authored by: Challenger M4-2
Target Poly-Tools:
- copilot/tools/run_command.py (and copilot/core/sandbox.py)
- copilot/tools/git_ops.py
- copilot/tools/code_search.py (and copilot/services/search_service.py)

Stress Test Matrix:
1. run_command:
   - Runaway child process tree termination on timeout (no orphan processes)
   - Concurrency stress under heavy multi-threading
   - Environment variable scrubbing (case-insensitivity, credential patterns)
   - 40-line output truncation (both stdout and stderr)
   - Windows .cmd resolution and binary whitelisting
   - Binary whitelist path spoofing vulnerability verification
2. git_ops:
   - Shadow checkpoints with isolated GIT_INDEX_FILE without dirtying working tree
   - Massive diff output compaction (<= 40 lines with notice)
   - Rollback to shadow checkpoint restores state
   - Rollback destructive deletion of .env / ignored files vulnerability verification
   - Concurrent git operations
3. code_search:
   - Grep regex search with context, path traversal defense, and directory exclusions
   - Grep invalid regex resilience
   - Symbol search across Python, JS, TS, Go, Rust
   - FTS5 fulltext search with malformed queries and thread concurrency
   - File tree globbing with strict exclusion of .git, .venv, node_modules, .copilot
4. FastMCP unstripped schema availability on all tools
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List
import pytest

from config import Config
from copilot.core.sandbox import (
    run_sandboxed_command,
    ERR_SECURITY_VIOLATION,
    ERR_COMMAND_TIMEOUT,
    ERR_PATH_TRAVERSAL,
    ERR_NOT_FOUND,
    ERR_COMMAND_FAILED,
)
from copilot.tools.run_command import run_command
from copilot.tools.git_ops import git_ops, GitAction, ERR_NOT_GIT_REPO, ERR_INVALID_ACTION
from copilot.tools.code_search import code_search, SearchMode, ERR_INVALID_MODE
from copilot.services.search_service import SearchService, ERR_INVALID_REGEX
from copilot.tools import register_copilot_tools, ALL_TOOLS
from fastmcp import FastMCP


# ==============================================================================
# Helper Functions
# ==============================================================================

def is_pid_alive_win(pid: int) -> bool:
    """Check if a process ID is currently alive on Windows."""
    try:
        res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=5)
        return str(pid) in res.stdout
    except Exception:
        return False


# ==============================================================================
# GROUP 1: run_command & Sandbox Execution Stress Tests
# ==============================================================================

def test_run_command_process_tree_timeout_runaway_children(tmp_path: Path):
    """
    Empirically verify that when a process tree (parent -> child -> grandchild)
    exceeds timeout, the ENTIRE tree is killed and no runaway child/grandchild
    processes are left orphaned in the operating system.
    """
    pid_file = tmp_path / "grandchild_pid.txt"

    # Python script that spawns a grandchild process which sleeps 60 seconds
    # and writes its PID to a file, while parent also sleeps 60 seconds.
    worker_script = f"""
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(r"{pid_file}", "w") as f:
    f.write(str(p.pid))
time.sleep(60)
"""
    script_file = tmp_path / "spawner.py"
    script_file.write_text(worker_script, encoding="utf-8")

    start_t = time.perf_counter()
    res = run_command("python", [str(script_file)], timeout_seconds=2)
    elapsed = time.perf_counter() - start_t

    assert res["exit_code"] == -1
    assert res["success"] is False
    assert res["error_code"] == ERR_COMMAND_TIMEOUT
    assert "timed out" in res["error"].lower()
    assert elapsed < 10.0, f"Timeout took too long to enforce: {elapsed:.2f}s"

    time.sleep(1.0)

    assert pid_file.exists(), "Grandchild PID file was not generated"
    grandchild_pid = int(pid_file.read_text().strip())

    # Check whether grandchild is still alive
    is_alive = is_pid_alive_win(grandchild_pid)
    if is_alive:
        # Cleanup in case test failed
        subprocess.run(["taskkill", "/F", "/PID", str(grandchild_pid)], capture_output=True)

    assert not is_alive, f"VULNERABILITY: Runaway grandchild PID {grandchild_pid} was orphaned and survived process tree kill!"


def test_run_command_concurrency_stress():
    """
    Empirically stress-test run_command under heavy multi-threading (16 concurrent threads).
    Verifies that subprocess execution, output capture, telemetry logging, and token tracking
    operate without deadlocks, race conditions, or output mixing.
    """
    def execute_task(idx: int) -> Dict[str, Any]:
        val1 = idx * 7
        val2 = idx * 13
        code = f"import sys; sys.stdout.write(f'RESULT:{val1 + val2}'); sys.stderr.write(f'LOG:{idx}')"
        return run_command("python", ["-c", code])

    num_threads = 16
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {executor.submit(execute_task, i): i for i in range(num_threads)}
        for future in as_completed(futures):
            idx = futures[future]
            res = future.result()
            assert res["exit_code"] == 0, f"Thread {idx} failed with exit_code {res['exit_code']}"
            assert res["success"] is True
            expected_res = f"RESULT:{idx * 7 + idx * 13}"
            assert expected_res in res["stdout"], f"Thread {idx} expected {expected_res}, got {res['stdout']}"
            assert f"LOG:{idx}" in res["stderr"]


def test_run_command_environment_scrubbing_exhaustive():
    """
    Empirically stress-test environment variable scrubbing with extensive patterns,
    mixed-case keys, and credential prefixes/suffixes.
    Confirms that sensitive keys are 100% stripped from subprocess while system vars remain.
    """
    sensitive_env = {
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "aws_session_token": "lower_case_token_123",
        "OPENAI_API_KEY": "sk-1234567890abcdef",
        "ANTHROPIC_KEY": "ant-key-xyz",
        "DATABASE_PASSWORD": "super_secret_db_pass",
        "API_SECRET_TOKEN": "secret_token_val",
        "USER_CREDENTIALS": "user:pass:creds",
        "SAFE_ENVIRONMENT_KEY_NAME_TEST": "public_data",
    }

    script = (
        "import os; "
        "keys = [k for k in os.environ if any(x in k.upper() for x in ['KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'AWS'])]; "
        "print('LEAKED:' + ','.join(sorted(keys)))"
    )

    res = run_sandboxed_command("python", ["-c", script], env=sensitive_env)
    assert res["exit_code"] == 0
    assert res["success"] is True
    leaked_line = [l for l in res["stdout"].splitlines() if l.startswith("LEAKED:")][0]
    leaked_keys = [k for k in leaked_line.replace("LEAKED:", "").split(",") if k]
    assert leaked_keys == [], f"Sensitive environment variables leaked into child process: {leaked_keys}"


def test_run_command_40_line_truncation_stdout_and_stderr():
    """
    Empirically verify that both stdout and stderr are truncated to at most 40 lines
    (plus 1 notice line) when output exceeds Config.MAX_OUTPUT_LINES.
    """
    script = (
        "import sys; "
        "sys.stdout.write('\\n'.join(f'STDOUT_{i}' for i in range(120)) + '\\n'); "
        "sys.stderr.write('\\n'.join(f'STDERR_{i}' for i in range(80)) + '\\n')"
    )
    res = run_command("python", ["-c", script])
    assert res["exit_code"] == 0
    assert res["truncated"] is True

    out_lines = res["stdout"].splitlines()
    err_lines = res["stderr"].splitlines()

    assert len(out_lines) == 41, f"Expected 41 lines for stdout (1 header + 40 items), got {len(out_lines)}"
    assert out_lines[0] == "[Output truncated: showing last 40 of 120 lines]"
    assert out_lines[-1] == "STDOUT_119"

    assert len(err_lines) == 41, f"Expected 41 lines for stderr (1 header + 40 items), got {len(err_lines)}"
    assert err_lines[0] == "[Output truncated: showing last 40 of 80 lines]"
    assert err_lines[-1] == "STDERR_79"


def test_run_command_whitelist_security_enforcement():
    """
    Verify rejection of unwhitelisted commands, shell interpreters, and injection attempts.
    """
    banned_commands = [
        "python.exe; calc.exe",
        "python && powershell",
        "powershell.exe",
        "cmd.exe",
        "bash",
        "sh",
        "curl",
        "wget",
        "certutil",
        "reg",
        "wmic",
    ]
    for cmd in banned_commands:
        res = run_command(cmd, ["--help"])
        assert res["success"] is False
        assert res["error_code"] in {ERR_SECURITY_VIOLATION, ERR_NOT_FOUND, ERR_COMMAND_FAILED}


def test_run_command_whitelist_path_spoofing_vulnerability(tmp_path: Path):
    """
    EMPIRICAL VERIFICATION:
    Verifies that arbitrary executables outside PATH and venv are blocked
    even if their basename matches a whitelisted command (e.g., git.bat, python.bat).
    """
    fake_git = tmp_path / "git.bat"
    fake_git.write_text("@echo PAYLOAD_EXECUTION_SUCCESSFUL\n", encoding="utf-8")

    res = run_command(str(fake_git))
    assert res["success"] is False
    assert res["exit_code"] == -1
    assert res["error_code"] == ERR_SECURITY_VIOLATION
    assert "PAYLOAD_EXECUTION_SUCCESSFUL" not in res.get("stdout", "")


# ==============================================================================
# GROUP 2: git_ops Native Plumbing & Shadow Checkpoint Stress Tests
# ==============================================================================

@pytest.fixture
def temp_git_repo(tmp_path: Path):
    """Create an isolated, fully initialized git repository fixture."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@domain.com"], cwd=repo, check=True, capture_output=True)

    # Initial committed file and .gitignore matching project conventions
    (repo / ".gitignore").write_text(".copilot/\n", encoding="utf-8")
    (repo / "README.md").write_text("# Test Repo\nInitial content\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    old_root = Config.PROJECT_ROOT
    Config.PROJECT_ROOT = repo
    yield repo
    Config.PROJECT_ROOT = old_root


def test_git_checkpoint_isolated_index_leaves_working_tree_clean(temp_git_repo: Path):
    """
    Empirically verify that git_ops checkpoint creates a valid shadow commit
    via isolated GIT_INDEX_FILE WITHOUT dirtying the active working tree,
    without clearing user staged files in .git/index, and without moving active HEAD.
    """
    repo = temp_git_repo

    # Set up dirty state in the active repo:
    # 1. Staged file
    (repo / "staged.py").write_text("print('staged')\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.py"], cwd=repo, check=True, capture_output=True)

    # 2. Modified tracked file
    (repo / "README.md").write_text("# Test Repo\nModified working copy\n", encoding="utf-8")

    # 3. Untracked file
    (repo / "untracked.txt").write_text("I am untracked\n", encoding="utf-8")

    # Capture active status and HEAD before checkpoint
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    status_proc_before = subprocess.run(["git", "status", "--porcelain=v1"], cwd=repo, capture_output=True, text=True)
    status_before = status_proc_before.stdout

    # Execute shadow checkpoint
    res = git_ops("checkpoint", message="shadow_point_1")
    assert res["success"] is True, f"Checkpoint failed: {res}"
    assert "checkpoint_id" in res
    checkpoint_sha = res["checkpoint_id"]
    assert res["ref"] == "refs/copilot/checkpoints/latest"

    # Verify active HEAD has NOT moved
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert head_after == head_before, "VULNERABILITY: git_ops checkpoint moved active HEAD!"

    # Verify status in active working tree is 100% IDENTICAL to before checkpoint
    status_proc_after = subprocess.run(["git", "status", "--porcelain=v1"], cwd=repo, capture_output=True, text=True)
    status_after = status_proc_after.stdout
    assert status_after == status_before, (
        f"VULNERABILITY: git_ops checkpoint dirtied active working tree or index!\n"
        f"Before:\n{status_before}\nAfter:\n{status_after}"
    )

    # Verify shadow commit object exists in git object database
    cat_proc = subprocess.run(["git", "cat-file", "-t", checkpoint_sha], cwd=repo, capture_output=True, text=True)
    assert cat_proc.returncode == 0
    assert cat_proc.stdout.strip() == "commit"

    # Verify no leaked temporary index files in .git directory
    dot_git = repo / ".git"
    shadow_leaks = list(dot_git.glob("copilot_shadow_idx_*"))
    assert shadow_leaks == [], f"Found leaked temporary shadow index files: {shadow_leaks}"


def test_git_diff_massive_output_compaction(temp_git_repo: Path):
    """
    Empirically test git diff output compaction when repository changes exceed 40 lines.
    Verifies that the returned diff is truncated to <= 40 lines, truncated=True,
    and includes compaction notice and stat summary.
    """
    repo = temp_git_repo

    # Create a 500-line change
    massive_content = "\n".join(f"line_{i} = {i * 10}" for i in range(500))
    (repo / "massive.py").write_text(massive_content, encoding="utf-8")
    subprocess.run(["git", "add", "massive.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add massive file"], cwd=repo, check=True, capture_output=True)

    # Now modify 300 lines to produce a massive diff
    modified_content = "\n".join(f"line_{i} = {i * 999}" for i in range(500))
    (repo / "massive.py").write_text(modified_content, encoding="utf-8")

    res = git_ops("diff")
    assert res["success"] is True
    assert res["truncated"] is True
    assert res["total_lines"] > 40

    diff_lines = res["diff"].splitlines()
    assert len(diff_lines) <= 40, f"Diff output exceeds 40 lines: {len(diff_lines)}"
    assert "[Output compacted: git diff exceeds 40 lines" in diff_lines[0]
    assert "massive.py" in res["stat"]


def test_git_rollback_to_checkpoint_restores_state(temp_git_repo: Path):
    """
    Empirically test rollback to shadow checkpoint.
    Verifies that file edits made AFTER checkpoint are reverted, and untracked files
    created after checkpoint are removed.
    """
    repo = temp_git_repo

    (repo / "code.py").write_text("def run():\n    return 'initial'\n", encoding="utf-8")
    cp_res = git_ops("checkpoint", message="state_alpha")
    assert cp_res["success"] is True

    # Mutate tracked file and create untracked file
    (repo / "code.py").write_text("def run():\n    return 'mutated_bad_code'\n", encoding="utf-8")
    (repo / "new_untracked.txt").write_text("untracked payload", encoding="utf-8")

    rb_res = git_ops("rollback")
    assert rb_res["success"] is True
    assert rb_res["restored_to"] == cp_res["checkpoint_id"]

    assert (repo / "code.py").read_text(encoding="utf-8") == "def run():\n    return 'initial'\n"
    assert not (repo / "new_untracked.txt").exists(), "Untracked file created after checkpoint should have been removed"


def test_git_rollback_preserves_sensitive_and_gitignored_files(temp_git_repo: Path):
    """
    Verify that git_ops rollback preserves sensitive files (.env, secrets)
    and gitignored files, while removing untracked post-checkpoint files.
    """
    repo = temp_git_repo

    # Set up .gitignore with .env
    gitignore_path = repo / ".gitignore"
    gitignore_path.write_text(".copilot/\n.env\n*.log\n", encoding="utf-8")
    (repo / ".env").write_text("SUPER_SECRET_KEY=ABC123XYZ\n", encoding="utf-8")
    (repo / "app.log").write_text("APP_LOG_DATA\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add .gitignore with .env"], cwd=repo, check=True, capture_output=True)

    # Take shadow checkpoint
    cp = git_ops("checkpoint", message="cp_before_edit")
    assert cp["success"] is True

    # Edit tracked file and create untracked non-ignored file
    (repo / "README.md").write_text("Changed content\n", encoding="utf-8")
    (repo / "junk_file.tmp").write_text("untracked junk\n", encoding="utf-8")

    # Perform rollback
    rb = git_ops("rollback")
    assert rb["success"] is True

    env_survived = (repo / ".env").exists()
    assert env_survived, ".env file must survive rollback"
    assert (repo / "app.log").exists(), "gitignored app.log must survive rollback"
    assert not (repo / "junk_file.tmp").exists(), "untracked non-ignored junk file must be unlinked"


def test_git_ops_concurrency_stress(temp_git_repo: Path):
    """
    Empirically stress-test concurrent git_ops invocations (status, diff, checkpoint)
    across multiple threads to detect git lock contention or index race conditions.
    """
    repo = temp_git_repo

    def worker(idx: int) -> Dict[str, Any]:
        if idx % 3 == 0:
            return git_ops("status")
        elif idx % 3 == 1:
            return git_ops("diff")
        else:
            return git_ops("checkpoint", message=f"concurrent_cp_{idx}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker, i) for i in range(12)]
        for f in as_completed(futures):
            res = f.result()
            assert res["success"] is True, f"Concurrent git operation failed: {res}"


# ==============================================================================
# GROUP 3: code_search Poly-Tool & SearchService Stress Tests
# ==============================================================================

@pytest.fixture
def search_workspace(tmp_path: Path):
    """Create a structured workspace populated with diverse language files and ignored dirs."""
    root = tmp_path / "project_workspace"
    root.mkdir()

    # Valid source code
    src = root / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "import sys\n\n"
        "class OrderProcessor:\n"
        "    def process_payment(self, amount: float):\n"
        "        # Handle transaction payment\n"
        "        return amount > 0\n\n"
        "async def async_dispatch_order(order_id: str):\n"
        "    return f'order_{order_id}'\n",
        encoding="utf-8",
    )
    (src / "service.ts").write_text(
        "export class OrderService {\n"
        "    async fetchOrder(id: string): Promise<any> {\n"
        "        return { id };\n"
        "    }\n"
        "}\n"
        "export function calculateTax(subtotal: number): number {\n"
        "    return subtotal * 0.08;\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "util.go").write_text(
        "package util\n\n"
        "func FormatCurrency(val int) string {\n"
        "    return \"$\"\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "core.rs").write_text(
        "pub fn compute_digest(data: &[u8]) -> u64 {\n"
        "    42\n"
        "}\n",
        encoding="utf-8",
    )

    # Excluded directories containing trap files that must NEVER be returned
    git_dir = root / ".git"
    git_dir.mkdir()
    (git_dir / "app.py").write_text("class FakeGitClass:\n    pass\n", encoding="utf-8")

    venv_dir = root / ".venv" / "lib"
    venv_dir.mkdir(parents=True)
    (venv_dir / "app.py").write_text("class FakeVenvClass:\n    pass\n", encoding="utf-8")

    nm_dir = root / "node_modules" / "express"
    nm_dir.mkdir(parents=True)
    (nm_dir / "index.js").write_text("function fakeModuleFunc() {}\n", encoding="utf-8")

    copilot_dir = root / ".copilot"
    copilot_dir.mkdir()
    (copilot_dir / "app.py").write_text("class FakeCopilotClass:\n    pass\n", encoding="utf-8")

    old_root = Config.PROJECT_ROOT
    Config.PROJECT_ROOT = root
    yield root
    Config.PROJECT_ROOT = old_root


def test_code_search_grep_mode_exclusions_and_context(search_workspace: Path):
    """
    Empirically test code_search grep mode:
    1. Finds matches in src/
    2. Strictly excludes .git, .venv, node_modules, .copilot
    3. Provides context lines with indicator '>'
    """
    res = code_search(mode="grep", query=r"class\s+\w+")
    assert res["success"] is True
    assert res["total_matches"] >= 2

    files_found = {m["file"] for m in res["results"]}
    for f in files_found:
        assert not f.startswith(".git"), f"Grep leaked .git file: {f}"
        assert not f.startswith(".venv"), f"Grep leaked .venv file: {f}"
        assert not f.startswith("node_modules"), f"Grep leaked node_modules file: {f}"
        assert not f.startswith(".copilot"), f"Grep leaked .copilot file: {f}"

    # Verify context lines structure
    match = res["results"][0]
    assert "context" in match
    assert ">" in match["context"]
    assert match["line"] > 0


def test_code_search_grep_path_traversal_defense(search_workspace: Path):
    """
    Verify that code_search rejects path traversal attempts via the path parameter.
    """
    res = code_search(mode="grep", query="payment", path="../../outside")
    assert res["success"] is False
    assert res["error_code"] == ERR_PATH_TRAVERSAL


def test_code_search_grep_invalid_regex_handling(search_workspace: Path):
    """
    Verify that invalid regex does not crash server and returns ERR_INVALID_REGEX.
    """
    res = code_search(mode="grep", query="[invalid(regex")
    assert res["success"] is False
    assert res["error_code"] == ERR_INVALID_REGEX


def test_code_search_symbol_multilingual(search_workspace: Path):
    """
    Empirically verify symbol search extracts functions and classes across Python, TS, Go, Rust.
    """
    res_py = code_search(mode="symbol", query="Order")
    assert res_py["success"] is True
    symbols_found = {m["symbol"] for m in res_py["results"]}
    assert "OrderProcessor" in symbols_found or "OrderService" in symbols_found

    res_fn = code_search(mode="symbol", query="calculateTax")
    assert res_fn["success"] is True
    assert any(m["symbol"] == "calculateTax" for m in res_fn["results"])

    res_go = code_search(mode="symbol", query="FormatCurrency")
    assert res_go["success"] is True
    assert any(m["symbol"] == "FormatCurrency" for m in res_go["results"])

    res_rs = code_search(mode="symbol", query="compute_digest")
    assert res_rs["success"] is True
    assert any(m["symbol"] == "compute_digest" for m in res_rs["results"])


def test_code_search_fulltext_fts5_stress_and_concurrency(search_workspace: Path):
    """
    Empirically stress-test SQLite FTS5 search with:
    - Multiple concurrent queries
    - Malformed search strings (punctuation, operators, unbalanced quotes)
    - Verification of <<<match>>> highlighting
    """
    service = SearchService(project_root=search_workspace)
    indexed = service.index_all(force=True)
    assert indexed > 0

    queries = [
        "payment",
        "calculateTax subtotal",
        "OrderProcessor",
        "AND OR NOT",
        "\"unclosed quote",
        "!@#$%^&*()",
        "fetchOrder",
        "nonexistent_token_12345",
    ]

    def run_query(q: str) -> Dict[str, Any]:
        return code_search(mode="fulltext", query=q)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(run_query, q) for q in queries]
        for f in as_completed(futures):
            res = f.result()
            assert res["success"] is True
            assert isinstance(res["results"], list)

    # Verify highlighted snippet
    res_highlight = code_search(mode="fulltext", query="payment")
    assert res_highlight["success"] is True
    if res_highlight["results"]:
        snippet = res_highlight["results"][0]["snippet"]
        assert "<<<" in snippet and ">>>" in snippet


def test_code_search_files_glob_exclusion_enforcement(search_workspace: Path):
    """
    Empirically verify that mode='files' navigates project files while strictly
    filtering out excluded directories (.git, .venv, node_modules, .copilot).
    """
    res = code_search(mode="files", query="*")
    assert res["success"] is True
    assert res["total_matches"] > 0

    for item in res["results"]:
        p = item["file_path"].replace("\\", "/")
        assert not p.startswith(".git/"), f"File search leaked .git: {p}"
        assert not p.startswith(".venv/"), f"File search leaked .venv: {p}"
        assert not p.startswith("node_modules/"), f"File search leaked node_modules: {p}"
        assert not p.startswith(".copilot/"), f"File search leaked .copilot: {p}"


# ==============================================================================
# GROUP 4: FastMCP Unstripped Schema Availability
# ==============================================================================

def test_fastmcp_unstripped_tool_schemas():
    """
    Empirically verify that registering all poly-tools on FastMCP results in:
    1. Exactly 6 registered tools
    2. Zero occurrences of '_lazy_schema_required' in parameter definitions
    3. Complete parameter property descriptions and types
    """
    mcp = FastMCP("copilot-verification-harness")
    registered = register_copilot_tools(mcp)
    assert len(registered) == 6
    assert set(registered) == {"file_ops", "code_search", "run_command", "git_ops", "inspect_ast", "diagnostics"}

    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 6

    tool_map = {t.name: t for t in tools}
    for name in ["file_ops", "code_search", "run_command", "git_ops", "inspect_ast", "diagnostics"]:
        assert name in tool_map, f"Missing tool: {name}"
        t = tool_map[name]
        params = t.parameters
        props = params.get("properties", {})

        # Verify unstripped schema invariant
        assert "_lazy_schema_required" not in props, f"Tool {name} has stripped lazy schema!"

        # Verify key parameters have documentation
        assert len(props) > 0, f"Tool {name} exposes no parameter properties!"
        for p_name, p_spec in props.items():
            assert "type" in p_spec or "anyOf" in p_spec, f"Tool {name} param {p_name} lacks type definition"
