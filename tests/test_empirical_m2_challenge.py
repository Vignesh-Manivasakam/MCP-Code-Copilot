"""
Empirical Challenge & Stress Test Suite for Milestone M2
========================================================
Rigorous verification of:
1. Path jail edge cases: relative traversal, null bytes, Windows drive traversal,
   redundant dots, valid absolute paths inside root, UNC paths, root itself.
2. Sensitive files: casing variations (.ENV, .Git/Config), subpaths, trailing chars.
3. Sandbox execution: unwhitelisted binaries, injection attempts, process timeout
   with infinite loop, process tree termination of child processes, and environment scrubbing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
import pytest

from config import Config
from copilot.core.security import (
    validate_path,
    is_sensitive_file,
    SecurityValidator,
)
from copilot.core.sandbox import (
    run_sandboxed_command,
    normalize_command_name,
    is_command_allowed,
    ERR_SECURITY_VIOLATION,
    ERR_COMMAND_TIMEOUT,
    ERR_NOT_FOUND,
    ERR_PATH_TRAVERSAL,
    ERR_COMMAND_FAILED,
)


# ==============================================================================
# 1. Path Jail Edge Cases
# ==============================================================================

class TestPathJailEdgeCases:
    """Empirically stress-test path jail boundary conditions and attacks."""

    def test_relative_traversal_variations(self, setup_test_project: Path):
        """Test varied relative traversal payloads escaping root."""
        payloads = [
            "../../etc/passwd",
            "..\\..\\Windows\\System32\\calc.exe",
            "src/../../outside.txt",
            "src/./../../outside.txt",
            "deep/nested/folder/../../../../outside.txt",
            "..",
            "../",
            "..\\",
            "../../",
            "..\\..\\",
            "./../../outside.txt",
            "src/../../../",
        ]
        for payload in payloads:
            is_valid, resolved, err = validate_path(payload)
            assert is_valid is False, f"Payload should be rejected: {payload}"
            assert resolved is None
            assert "outside" in err.lower() or "ERR_PATH_TRAVERSAL" in err

    def test_traversal_looping_back_inside_root(self, setup_test_project: Path):
        """Relative traversal that navigates up but stays strictly within root should succeed."""
        # e.g., src/../src/app.py
        app_file = setup_test_project / "src" / "app.py"
        is_valid, resolved, err = validate_path("src/../src/app.py")
        assert is_valid is True
        assert resolved == app_file.resolve()
        assert err == ""

    def test_null_byte_injection_vectors(self, setup_test_project: Path):
        """Rejects null-byte injection attempts without unhandled runtime exceptions."""
        null_payloads = [
            "main.py\x00.txt",
            "src/\x00/app.py",
            "\x00",
            "src/app.py\x00",
            "src/app.py\x00extra",
            "../outside\x00/app.py",
        ]
        for payload in null_payloads:
            is_valid, resolved, err = validate_path(payload)
            assert is_valid is False, f"Null byte payload must be rejected: {repr(payload)}"
            assert resolved is None
            assert "Null bytes" in err or "ERR_PATH_TRAVERSAL" in err

    def test_windows_drive_traversal_and_cross_drive(self, setup_test_project: Path):
        """Rejects absolute paths pointing to different drives or out-of-root Windows locations."""
        current_drive = setup_test_project.drive.upper() if setup_test_project.drive else "D:"
        other_drive = "C:" if current_drive != "C:" else "D:"

        cross_drive_paths = [
            f"{other_drive}\\Windows\\System32\\cmd.exe",
            f"{other_drive}:\\secret.txt",
            f"{other_drive}:relative_on_other_drive",
        ]
        for p in cross_drive_paths:
            is_valid, resolved, err = validate_path(p)
            assert is_valid is False, f"Cross-drive path must be rejected: {p}"
            assert resolved is None
            assert "outside" in err.lower() or "ERR_PATH_TRAVERSAL" in err

    def test_redundant_dots_and_complex_normalization(self, setup_test_project: Path):
        """Normalizes multiple redundant dots and current-dir markers."""
        app_py = setup_test_project / "src" / "app.py"
        test_cases = [
            "src/./././app.py",
            "./src/app.py",
            "././src/./app.py",
            "src/../src/./app.py",
        ]
        for path_str in test_cases:
            is_valid, resolved, err = validate_path(path_str)
            assert is_valid is True, f"Valid redundant dot path failed: {path_str}"
            assert resolved == app_py.resolve()
            assert err == ""

    def test_valid_absolute_paths_inside_root(self, setup_test_project: Path):
        """Accepts various absolute paths that physically resolve inside project root."""
        app_py = setup_test_project / "src" / "app.py"
        abs_resolved = app_py.resolve()

        # Exact resolved path
        is_valid, resolved, err = validate_path(abs_resolved)
        assert is_valid is True
        assert resolved == abs_resolved

        # String representation
        is_valid, resolved, err = validate_path(str(abs_resolved))
        assert is_valid is True
        assert resolved == abs_resolved

        # Forward slash variation of absolute path
        fwd_slash = str(abs_resolved).replace("\\", "/")
        is_valid, resolved, err = validate_path(fwd_slash)
        assert is_valid is True
        assert resolved == abs_resolved

        # Drive letter casing variation on Windows
        if sys.platform == "win32" and abs_resolved.drive:
            drv = abs_resolved.drive
            rest = str(abs_resolved)[len(drv):]
            flipped_drv = drv.lower() if drv.isupper() else drv.upper()
            flipped_path = flipped_drv + rest
            is_valid, resolved, err = validate_path(flipped_path)
            assert is_valid is True
            assert resolved == abs_resolved

    def test_empty_and_whitespace_and_none_paths(self, setup_test_project: Path):
        """Rejects None, empty string, and whitespace-only paths."""
        for empty_val in [None, "", "   ", "\t\n  "]:
            is_valid, resolved, err = validate_path(empty_val)
            assert is_valid is False
            assert resolved is None
            assert "ERR_PATH_TRAVERSAL" in err


# ==============================================================================
# 2. Sensitive Files Protection & Casing Variations
# ==============================================================================

class TestSensitiveFilesChallenge:
    """Stress-test sensitive file protection across casing, subpaths, and bypass attempts."""

    @pytest.mark.parametrize("cased_env", [
        ".env",
        ".ENV",
        ".Env",
        ".eNv",
        "./.env",
        ".//.ENV",
        ".\\.env",
        ".\\sub\\.env",
        "nested/.env",
        "nested/deep/.ENV",
        "nested\\deep\\.env",
    ])
    def test_env_casing_and_subpath_variations(self, setup_test_project: Path, cased_env: str):
        """Rejects .env and casing/subpath variations."""
        assert is_sensitive_file(cased_env) is True
        is_valid, resolved, err = validate_path(cased_env)
        assert is_valid is False, f"Should reject sensitive file: {cased_env}"
        assert resolved is None
        assert "Sensitive file access is denied" in err

    @pytest.mark.parametrize("cased_git_config", [
        ".git/config",
        ".GIT/CONFIG",
        ".Git/Config",
        ".git/Config",
        ".Git/CONFIG",
        r".git\config",
        r".Git\Config",
        r".GIT\CONFIG",
        "sub/.git/config",
        "sub/.Git/Config",
        r"sub\.git\config",
        r"sub\.Git\Config",
    ])
    def test_git_config_casing_and_subpaths(self, setup_test_project: Path, cased_git_config: str):
        """Rejects .git/config and casing/subpath variations."""
        assert is_sensitive_file(cased_git_config) is True
        is_valid, resolved, err = validate_path(cased_git_config)
        assert is_valid is False, f"Should reject git config: {cased_git_config}"
        assert resolved is None
        assert "Sensitive file access is denied" in err

    @pytest.mark.parametrize("cased_ssh_secrets", [
        "id_rsa",
        "ID_RSA",
        "Id_Rsa",
        "id_ed25519",
        "ID_ED25519",
        ".secrets",
        ".SECRETS",
        ".Secrets",
        "keys/id_rsa",
        "keys/ID_RSA",
        "config/.secrets",
        "config/.SECRETS",
    ])
    def test_ssh_keys_and_secrets(self, setup_test_project: Path, cased_ssh_secrets: str):
        """Rejects id_rsa, id_ed25519, .secrets and casing/subpath variations."""
        assert is_sensitive_file(cased_ssh_secrets) is True
        is_valid, resolved, err = validate_path(cased_ssh_secrets)
        assert is_valid is False, f"Should reject sensitive secret: {cased_ssh_secrets}"
        assert resolved is None
        assert "Sensitive file access is denied" in err

    def test_absolute_path_to_sensitive_file(self, setup_test_project: Path):
        """Rejects absolute paths pointing to sensitive files inside project root."""
        git_cfg = setup_test_project / ".git" / "config"
        is_valid, resolved, err = validate_path(str(git_cfg.resolve()))
        assert is_valid is False
        assert resolved is None
        assert "Sensitive file access is denied" in err

    def test_non_sensitive_names_not_blocked(self, setup_test_project: Path):
        """Ensures benign files containing similar substrings are not falsely blocked."""
        benign_files = [
            "src/environment.py",
            "src/git_helper.py",
            "id_rsa.pub",       # Public SSH key is safe to read
            "src/secrets_manager.py",
            "env_config.json",
        ]
        for b in benign_files:
            assert is_sensitive_file(b) is False


# ==============================================================================
# 3. Sandbox Execution Challenge
# ==============================================================================

class TestSandboxExecutionChallenge:
    """Stress-test sandbox whitelisting, process tree termination, and env scrubbing."""

    def test_unwhitelisted_commands_blocked(self):
        """Blocks prohibited executables regardless of arguments or variations."""
        unwhitelisted = [
            "curl",
            "rm",
            "powershell",
            "powershell.exe",
            "cmd",
            "cmd.exe",
            "bash",
            "sh",
            "wget",
            "certutil",
            "reg",
            "shutdown",
            "del",
        ]
        for cmd in unwhitelisted:
            res = run_sandboxed_command(cmd, ["--help"])
            assert res["exit_code"] == -1, f"Command should be blocked: {cmd}"
            assert res["success"] is False
            assert res["error_code"] == ERR_SECURITY_VIOLATION
            assert "not whitelisted" in res["error"]

    def test_path_prefixed_unwhitelisted_commands_blocked(self):
        """Blocks unwhitelisted commands specified by absolute or relative path."""
        bad_paths = [
            r"C:\Windows\System32\cmd.exe",
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "/bin/bash",
            "/usr/bin/curl",
        ]
        for p in bad_paths:
            res = run_sandboxed_command(p, ["-h"])
            assert res["exit_code"] == -1
            assert res["success"] is False
            assert res["error_code"] == ERR_SECURITY_VIOLATION

    def test_command_injection_shell_metacharacters(self):
        """Confirms shell injection metacharacters (&, |, ;, `) are not executed."""
        # Because shell=False, passing "python && whoami" is treated as executable name
        res = run_sandboxed_command("python && whoami", [])
        assert res["exit_code"] in (-1, 127)
        assert res["success"] is False

    def test_process_timeout_infinite_loop(self):
        """Terminates an infinite loop process after timeout_seconds."""
        start = time.perf_counter()
        res = run_sandboxed_command(
            "python",
            ["-c", "while True: pass"],
            timeout_seconds=1,
        )
        elapsed = time.perf_counter() - start

        assert res["exit_code"] == -1
        assert res["success"] is False
        assert res["error_code"] == ERR_COMMAND_TIMEOUT
        assert "timed out" in res["error"].lower()
        # Verify elapsed time was close to 1 second (e.g. 1.0s to 3.5s accounting for taskkill)
        assert 0.9 <= elapsed <= 4.0

    def test_process_tree_kill_on_timeout(self, tmp_path: Path):
        """Verifies that child processes spawned by the target script are terminated on timeout."""
        # Create a helper script that spawns a long-lived child process and writes child PID
        pid_file = tmp_path / "child.pid"
        helper_script = tmp_path / "spawn_child.py"
        helper_script.write_text(f"""
import subprocess, sys, time
# Spawn a child sleep process
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(r"{pid_file}", "w") as f:
    f.write(str(child.pid))
# Wait indefinitely
while True:
    time.sleep(0.5)
""", encoding="utf-8")

        res = run_sandboxed_command(
            "python",
            [str(helper_script)],
            timeout_seconds=2,
            cwd=tmp_path,
        )
        assert res["exit_code"] == -1
        assert res["error_code"] == ERR_COMMAND_TIMEOUT

        # Wait a moment for OS cleanup
        time.sleep(0.5)

        # Check if child process is dead
        assert pid_file.exists(), "Child PID file was not created"
        child_pid = int(pid_file.read_text().strip())

        # Check process status using tasklist on Windows or kill -0 on POSIX
        if sys.platform == "win32":
            check = subprocess.run(
                ["tasklist", "/FI", f"PID eq {child_pid}"],
                capture_output=True,
                text=True,
            )
            # If terminated, tasklist reports "No tasks are running which match the specified criteria."
            is_alive = str(child_pid) in check.stdout
            assert not is_alive, f"Child process tree was NOT killed! PID {child_pid} still running"
        else:
            try:
                os.kill(child_pid, 0)
                is_alive = True
            except OSError:
                is_alive = False
            assert not is_alive, f"Child PID {child_pid} still running"

    def test_environment_variable_scrubbing_exhaustive(self):
        """Verifies thorough scrubbing of API keys, tokens, credentials, and passwords."""
        sensitive_env = {
            # Sensitive keys
            "OPENAI_API_KEY": "sk-proj-testkey123456",
            "openai_api_key": "lowercase-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret-access",
            "AWS_SESSION_TOKEN": "aws-session-token",
            "AZURE_OPENAI_KEY": "azure-key-123",
            "GITHUB_TOKEN": "ghp_tokencode",
            "MY_DATABASE_PASSWORD": "supersecretpassword",
            "SERVICE_CREDENTIAL": "credential-value",
            "CLIENT_SECRET": "client-secret-val",
            # Safe keys
            "SAFE_PUBLIC_VAR": "visible_to_subprocess",
            "PROJECT_NAME": "CodeCopilot",
        }

        # Include essential OS variables for python to execute
        for k in ["PATH", "SYSTEMROOT", "TEMP", "TMP"]:
            if k in os.environ:
                sensitive_env[k] = os.environ[k]

        check_script = """
import os, sys
keys = list(os.environ.keys())
print("VARS:" + ",".join(keys))
"""
        res = run_sandboxed_command(
            "python",
            ["-c", check_script],
            env=sensitive_env,
        )
        assert res["exit_code"] == 0
        assert res["success"] is True

        vars_line = [ln for ln in res["stdout"].splitlines() if ln.startswith("VARS:")][0]
        active_vars = set(vars_line.replace("VARS:", "").split(","))

        # Check sensitive keys are scrubbed
        assert "OPENAI_API_KEY" not in active_vars
        assert "openai_api_key" not in active_vars
        assert "AWS_SECRET_ACCESS_KEY" not in active_vars
        assert "AWS_SESSION_TOKEN" not in active_vars
        assert "AZURE_OPENAI_KEY" not in active_vars
        assert "GITHUB_TOKEN" not in active_vars
        assert "MY_DATABASE_PASSWORD" not in active_vars
        assert "SERVICE_CREDENTIAL" not in active_vars
        assert "CLIENT_SECRET" not in active_vars

        # Check safe keys are retained
        assert "SAFE_PUBLIC_VAR" in active_vars
        assert "PROJECT_NAME" in active_vars
