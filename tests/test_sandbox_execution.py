"""
Authoritative Test Suite 5: Sandboxed Command Execution.
========================================================
Tests sandboxed command execution, binary whitelisting, Windows .cmd resolution,
environment variable scrubbing, process tree timeout killing, output truncation,
and run_command poly-tool integration as specified in Part 4 Suite 5.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import pytest

from config import Config
from copilot.core.sandbox import (
    run_sandboxed_command,
    ERR_SECURITY_VIOLATION,
    ERR_COMMAND_TIMEOUT,
    ERR_NOT_FOUND,
    ERR_PATH_TRAVERSAL,
    ERR_COMMAND_FAILED,
)
from copilot.tools.run_command import run_command


# ==============================================================================
# Positive Cases
# ==============================================================================

def test_sandbox_execute_pytest():
    """Successfully runs pytest --version and returns exit code 0."""
    res = run_sandboxed_command("pytest", ["--version"])
    assert res["exit_code"] == 0
    assert "pytest" in res["stdout"] or "pytest" in res["stderr"]
    assert res["success"] is True
    assert res["truncated"] is False
    assert res["error"] is None


def test_sandbox_execute_python_script():
    """Runs python -c "print('hello')" and captures stdout."""
    res = run_sandboxed_command("python", ["-c", "print('hello')"])
    assert res["exit_code"] == 0
    assert res["stdout"].strip() == "hello"
    assert res["success"] is True
    assert res["truncated"] is False
    assert res["error"] is None


def test_run_command_poly_tool_direct():
    """Tests the high-level run_command tool interface directly."""
    res = run_command("python", ["-c", "print('direct_tool_call')"])
    assert res["exit_code"] == 0
    assert "direct_tool_call" in res["stdout"]
    assert res["success"] is True
    assert "execution_time_ms" in res


# ==============================================================================
# Edge Cases (X, Y, Z)
# ==============================================================================

def test_sandbox_windows_cmd_resolution():
    """Correctly executes npm --version on Windows by resolving npm.cmd via shutil.which."""
    npm_path = shutil.which("npm")
    if not npm_path:
        pytest.skip("npm is not installed on this system")

    res = run_sandboxed_command("npm", ["--version"])
    assert res["exit_code"] == 0
    assert res["success"] is True
    assert res["truncated"] is False
    assert any(char.isdigit() for char in res["stdout"])


def test_sandbox_environment_scrubbing():
    """Verifies that sensitive variables (AWS_*, *KEY*, *TOKEN*, *SECRET*) are stripped."""
    test_env = {
        "AWS_SECRET_ACCESS_KEY": "super_secret_aws",
        "OPENAI_API_KEY": "sk-123456789",
        "GITHUB_TOKEN": "ghp_abcdef",
        "MY_DB_PASSWORD": "secret_password",
        "SAFE_ENV_VAR": "visible_value",
    }
    script = (
        "import os; "
        "print('AWS:' + str('AWS_SECRET_ACCESS_KEY' in os.environ)); "
        "print('KEY:' + str('OPENAI_API_KEY' in os.environ)); "
        "print('TOKEN:' + str('GITHUB_TOKEN' in os.environ)); "
        "print('SAFE:' + str('SAFE_ENV_VAR' in os.environ))"
    )
    res = run_sandboxed_command("python", ["-c", script], env=test_env)
    assert res["exit_code"] == 0
    lines = res["stdout"].splitlines()
    assert "AWS:False" in lines
    assert "KEY:False" in lines
    assert "TOKEN:False" in lines
    assert "SAFE:True" in lines


def test_sandbox_output_truncation():
    """When a script outputs > 40 lines of logs, truncates to last 40 lines with notice."""
    script = "for i in range(100): print(f'output line {i}')"
    res = run_sandboxed_command("python", ["-c", script])
    assert res["exit_code"] == 0
    assert res["truncated"] is True
    stdout_lines = res["stdout"].splitlines()
    assert len(stdout_lines) == 41
    assert "[Output truncated: showing last 40 of 100 lines]" == stdout_lines[0]
    assert "output line 99" == stdout_lines[-1]


def test_run_command_working_directory(tmp_path: Path):
    """Executes command in specified working directory and returns relative files."""
    test_sub = tmp_path / "subdir"
    test_sub.mkdir()
    (test_sub / "marker.txt").write_text("found_marker", encoding="utf-8")

    script = "from pathlib import Path; print(Path('marker.txt').exists())"
    res = run_command("python", ["-c", script], working_directory=str(test_sub))
    assert res["exit_code"] == 0
    assert "True" in res["stdout"]


# ==============================================================================
# Negative & Failure Cases
# ==============================================================================

def test_sandbox_reject_unwhitelisted_command():
    """Rejects unwhitelisted commands like curl, powershell, rm, wget, bash."""
    for bad_cmd in ["curl", "powershell", "rm", "wget", "bash", "cmd.exe", "bash.exe"]:
        res = run_command(bad_cmd, ["--help"])
        assert res["exit_code"] == -1
        assert res["success"] is False
        assert res["error_code"] == ERR_SECURITY_VIOLATION
        assert "not whitelisted" in res["error"]


def test_sandbox_kill_process_on_timeout():
    """A script running an infinite loop is terminated after timeout and children killed."""
    res = run_command(
        "python",
        ["-c", "import time; time.sleep(10)"],
        timeout_seconds=1,
    )
    assert res["exit_code"] == -1
    assert res["success"] is False
    assert res["error_code"] == ERR_COMMAND_TIMEOUT
    assert "timed out" in res["error"].lower()


def test_sandbox_nonzero_exit_code():
    """Returns exit code with captured stderr when a command fails, without crashing."""
    script = "import sys; sys.stderr.write('Fatal execution failure\\n'); sys.exit(42)"
    res = run_command("python", ["-c", script])
    assert res["exit_code"] == 42
    assert res["success"] is False
    assert "Fatal execution failure" in res["stderr"]
    assert res["error_code"] == ERR_COMMAND_FAILED


def test_sandbox_nonexistent_working_directory():
    """Returns ERR_PATH_TRAVERSAL when working directory does not exist."""
    fake_cwd = "C:/nonexistent_folder_abc_xyz_123"
    res = run_command("python", ["--version"], working_directory=fake_cwd)
    assert res["exit_code"] == -1
    assert res["error_code"] == ERR_PATH_TRAVERSAL
    assert "does not exist" in res["error"]
