"""
Test Suite for copilot/core/sandbox.py
Tests sandboxed command execution, whitelist validation, Windows .cmd resolution,
environment variable scrubbing, process tree timeout killing, and output truncation.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import pytest

from config import Config
from copilot.core.sandbox import (
    run_sandboxed_command,
    normalize_command_name,
    is_command_allowed,
    resolve_binary_path,
    scrub_environment,
    ERR_SECURITY_VIOLATION,
    ERR_COMMAND_TIMEOUT,
    ERR_NOT_FOUND,
    ERR_PATH_TRAVERSAL,
    ERR_COMMAND_FAILED,
)


# ==============================================================================
# Positive Cases
# ==============================================================================

def test_sandbox_execute_python_script():
    """Runs python -c "print('hello')" and captures stdout."""
    res = run_sandboxed_command("python", ["-c", "print('hello')"])
    assert res["exit_code"] == 0
    assert res["stdout"].strip() == "hello"
    assert res["success"] is True
    assert res["truncated"] is False
    assert res["error"] is None
    assert res["error_code"] is None


def test_sandbox_execute_pytest():
    """Successfully runs pytest --version and returns exit code 0."""
    res = run_sandboxed_command("pytest", ["--version"])
    assert res["exit_code"] == 0
    assert "pytest" in res["stdout"] or "pytest" in res["stderr"]
    assert res["success"] is True


# ==============================================================================
# Edge Cases
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
    # Version should look like semver (e.g. 10.8.2 or 11.0.0)
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
    # Pass environment with sensitive values
    res = run_sandboxed_command(
        "python",
        [
            "-c",
            "import os; print('AWS:' + str('AWS_SECRET_ACCESS_KEY' in os.environ)); print('KEY:' + str('OPENAI_API_KEY' in os.environ)); print('TOKEN:' + str('GITHUB_TOKEN' in os.environ)); print('SAFE:' + str('SAFE_ENV_VAR' in os.environ))",
        ],
        env=test_env,
    )
    assert res["exit_code"] == 0
    lines = res["stdout"].splitlines()
    assert "AWS:False" in lines
    assert "KEY:False" in lines
    assert "TOKEN:False" in lines
    assert "SAFE:True" in lines


def test_sandbox_output_truncation():
    """When a script outputs > 40 lines of logs, truncates to last 40 lines and prepends notice."""
    res = run_sandboxed_command(
        "python",
        ["-c", "for i in range(100): print(f'output line {i}')"],
    )
    assert res["exit_code"] == 0
    assert res["truncated"] is True
    stdout_lines = res["stdout"].splitlines()
    # 1 notice line + 40 output lines = 41 lines
    assert len(stdout_lines) == 41
    assert "[Output truncated: showing last 40 of 100 lines]" == stdout_lines[0]
    assert "output line 99" == stdout_lines[-1]


def test_sandbox_normalize_command_name():
    """Verifies command normalization handles paths, extensions, and casing."""
    assert normalize_command_name(r"C:\Python312\python.exe") == "python"
    assert normalize_command_name("/usr/bin/npm") == "npm"
    assert normalize_command_name("npm.cmd") == "npm"
    assert normalize_command_name("PYTEST.BAT") == "pytest"
    assert normalize_command_name("git") == "git"


# ==============================================================================
# Negative & Failure Cases
# ==============================================================================

def test_sandbox_reject_unwhitelisted_command():
    """Rejects unwhitelisted commands like curl, powershell, rm, wget, bash."""
    for bad_cmd in ["curl", "powershell", "rm", "wget", "bash", "cmd.exe", "bash.exe"]:
        res = run_sandboxed_command(bad_cmd, ["--help"])
        assert res["exit_code"] == -1
        assert res["success"] is False
        assert res["error_code"] == ERR_SECURITY_VIOLATION
        assert "not whitelisted" in res["error"]


def test_sandbox_kill_process_on_timeout():
    """A script running an infinite loop is terminated after timeout and child processes killed."""
    res = run_sandboxed_command(
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
    res = run_sandboxed_command(
        "python",
        ["-c", "import sys; sys.stderr.write('Something failed\\n'); sys.exit(42)"],
    )
    assert res["exit_code"] == 42
    assert res["success"] is False
    assert "Something failed" in res["stderr"]
    assert res["error_code"] == ERR_COMMAND_FAILED


def test_sandbox_nonexistent_working_directory():
    """Returns ERR_PATH_TRAVERSAL when working directory does not exist."""
    fake_cwd = Path("C:/nonexistent_folder_abc_123")
    res = run_sandboxed_command("python", ["--version"], cwd=fake_cwd)
    assert res["exit_code"] == -1
    assert res["error_code"] == ERR_PATH_TRAVERSAL
    assert "does not exist" in res["error"]
