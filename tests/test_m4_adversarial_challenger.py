"""
Adversarial Empirical Stress Suite: Sandbox Security & Git Rollback.
=====================================================================
Validates:
1. Command whitelist path spoofing defenses (scripts named git.bat, python.cmd,
   pytest.exe in temp directories, project root, and relative paths blocked with ERR_SECURITY_VIOLATION).
2. Legitimate command executions (python, git, pytest, sys.executable).
3. Git rollback protection for sensitive files (.env, .secrets, id_rsa, id_ed25519)
   and gitignored files (local.log) during rollback to both shadow checkpoint and HEAD.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest

from config import Config
from copilot.core.sandbox import (
    is_command_allowed,
    resolve_binary_path,
    run_sandboxed_command,
    ERR_SECURITY_VIOLATION,
    ERR_NOT_FOUND,
)
from copilot.tools.run_command import run_command
from copilot.tools.git_ops import git_ops


@pytest.fixture
def clean_git_repo(tmp_path: Path):
    """Fixture to build an isolated Git repository with sensitive and gitignored files."""
    repo = tmp_path / "adversarial_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "ChallengerTest"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "challenger@test.local"], cwd=repo, check=True, capture_output=True)

    # Setup .gitignore
    gitignore_content = "\n".join([
        ".copilot/",
        ".env",
        ".secrets",
        "id_rsa",
        "id_ed25519",
        "local.log",
        "*.log",
        "",
    ])
    (repo / ".gitignore").write_text(gitignore_content, encoding="utf-8")
    (repo / "tracked.py").write_text("def base_func():\n    return 'original'\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "tracked.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with gitignore"], cwd=repo, check=True, capture_output=True)

    old_root = Config.PROJECT_ROOT
    Config.PROJECT_ROOT = repo
    yield repo
    Config.PROJECT_ROOT = old_root


# ==============================================================================
# SECTION 1: Whitelist Path Spoofing Defenses
# ==============================================================================

def test_whitelist_path_spoofing_temp_directory_scripts(tmp_path: Path):
    """
    Test executing scripts named git.bat, python.cmd, pytest.exe located in
    arbitrary temporary directories. Confirm all are blocked with ERR_SECURITY_VIOLATION.
    """
    temp_dir = tmp_path / "spoofed_binaries"
    temp_dir.mkdir()

    spoofed_scripts = [
        temp_dir / "git.bat",
        temp_dir / "python.cmd",
        temp_dir / "pytest.exe",
        temp_dir / "pytest.bat",
    ]

    for script_path in spoofed_scripts:
        script_path.write_text("@echo MALICIOUS_PAYLOAD_TRIGGERED\n", encoding="utf-8")
        
        # Test 1: Direct via run_sandboxed_command with absolute path
        res_sandbox = run_sandboxed_command(str(script_path))
        assert res_sandbox["success"] is False
        assert res_sandbox["exit_code"] == -1
        assert res_sandbox["error_code"] == ERR_SECURITY_VIOLATION
        assert "MALICIOUS_PAYLOAD_TRIGGERED" not in res_sandbox["stdout"]

        # Test 2: Via run_command poly-tool
        res_poly = run_command(str(script_path))
        assert res_poly["success"] is False
        assert res_poly["exit_code"] == -1
        assert res_poly["error_code"] == ERR_SECURITY_VIOLATION
        assert "MALICIOUS_PAYLOAD_TRIGGERED" not in res_poly["stdout"]


def test_whitelist_path_spoofing_project_root_scripts(tmp_path: Path):
    """
    Test executing scripts named git.bat, python.cmd, pytest.exe located in
    project root using relative paths (./git.bat, etc.).
    Confirm all are blocked with ERR_SECURITY_VIOLATION.
    """
    fake_root = tmp_path / "project_root"
    fake_root.mkdir()
    
    old_root = Config.PROJECT_ROOT
    Config.PROJECT_ROOT = fake_root
    try:
        git_bat = fake_root / "git.bat"
        python_cmd = fake_root / "python.cmd"
        pytest_bat = fake_root / "pytest.bat"

        git_bat.write_text("@echo MALICIOUS_GIT\n", encoding="utf-8")
        python_cmd.write_text("@echo MALICIOUS_PYTHON\n", encoding="utf-8")
        pytest_bat.write_text("@echo MALICIOUS_PYTEST\n", encoding="utf-8")

        relative_paths = [
            "./git.bat",
            ".\\git.bat",
            "./python.cmd",
            ".\\python.cmd",
            "./pytest.bat",
            ".\\pytest.bat",
        ]

        for rel_cmd in relative_paths:
            res = run_command(rel_cmd, working_directory=str(fake_root))
            assert res["success"] is False
            assert res["exit_code"] == -1
            assert res["error_code"] == ERR_SECURITY_VIOLATION
            assert "MALICIOUS" not in res["stdout"]
    finally:
        Config.PROJECT_ROOT = old_root


def test_whitelist_path_spoofing_arbitrary_relative_paths(tmp_path: Path):
    """
    Test executing scripts named git.bat, python.cmd, pytest.exe in subdirectories
    and parent directories via arbitrary relative paths.
    Confirm all are blocked with ERR_SECURITY_VIOLATION.
    """
    base_dir = tmp_path / "workspace"
    sub_dir = base_dir / "tools" / "bin"
    sub_dir.mkdir(parents=True)

    fake_git = sub_dir / "git.bat"
    fake_py = sub_dir / "python.cmd"
    fake_git.write_text("@echo SUBDIR_SPOOFED_GIT\n", encoding="utf-8")
    fake_py.write_text("@echo SUBDIR_SPOOFED_PYTHON\n", encoding="utf-8")

    test_relative_invocations = [
        "tools/bin/git.bat",
        "tools\\bin\\git.bat",
        "./tools/bin/git.bat",
        ".\\tools\\bin\\git.bat",
        "tools/bin/python.cmd",
        "tools\\bin\\python.cmd",
        "../workspace/tools/bin/git.bat",
        "..\\workspace\\tools\\bin\\python.cmd",
    ]

    for rel_inv in test_relative_invocations:
        res = run_command(rel_inv, working_directory=str(base_dir))
        assert res["success"] is False
        assert res["exit_code"] == -1
        assert res["error_code"] == ERR_SECURITY_VIOLATION
        assert "SUBDIR_SPOOFED" not in res["stdout"]


def test_cwd_binary_hijack_defense_via_shutil_which(tmp_path: Path):
    """
    Empirically test that when a spoofed git.bat or pytest.cmd is present in
    the current working directory, calling 'git' or 'pytest' without path prefix
    does NOT execute the local unvetted binary.
    """
    fake_project = tmp_path / "hijack_target"
    fake_project.mkdir()

    # Create fake git.bat in the fake project root
    fake_git = fake_project / ("git.bat" if sys.platform == "win32" else "git")
    fake_git.write_text("@echo CWD_HIJACK_EXECUTED\n" if sys.platform == "win32" else "#!/bin/sh\necho CWD_HIJACK_EXECUTED\n")

    old_root = Config.PROJECT_ROOT
    Config.PROJECT_ROOT = fake_project
    old_cwd = Path.cwd()
    os.chdir(fake_project)
    try:
        resolved = resolve_binary_path("git")
        # resolved must either be system git (outside fake_project) or None, NEVER inside fake_project
        if resolved is not None:
            resolved_p = Path(resolved).resolve()
            assert fake_project.resolve() not in resolved_p.parents
            assert resolved_p != fake_git.resolve()

        res = run_command("git", ["--version"], working_directory=str(fake_project))
        assert "CWD_HIJACK_EXECUTED" not in res.get("stdout", "")
    finally:
        os.chdir(old_cwd)
        Config.PROJECT_ROOT = old_root


def test_legitimate_allowed_commands_succeed():
    """
    Confirm legitimate whitelisted commands (python, git, pytest, sys.executable)
    continue to execute properly under the security constraints.
    """
    # 1. python via simple binary name
    res_py = run_command("python", ["-c", "print('PYTHON_LEGIT_OK')"])
    assert res_py["success"] is True
    assert res_py["exit_code"] == 0
    assert "PYTHON_LEGIT_OK" in res_py["stdout"]

    # 2. sys.executable via absolute path
    res_sys_py = run_command(sys.executable, ["-c", "print('SYS_EXEC_LEGIT_OK')"])
    assert res_sys_py["success"] is True
    assert res_sys_py["exit_code"] == 0
    assert "SYS_EXEC_LEGIT_OK" in res_sys_py["stdout"]

    # 3. git via simple binary name
    res_git = run_command("git", ["--version"])
    assert res_git["success"] is True
    assert res_git["exit_code"] == 0
    assert "git version" in res_git["stdout"]

    # 4. pytest via simple binary name
    res_pytest = run_command("pytest", ["--version"])
    assert res_pytest["success"] is True
    assert res_pytest["exit_code"] == 0
    assert "pytest" in (res_pytest["stdout"] + res_pytest["stderr"]).lower()


# ==============================================================================
# SECTION 2: Git Rollback Protection
# ==============================================================================

def test_git_rollback_to_shadow_checkpoint_protection(clean_git_repo: Path):
    """
    Adversarial test of git rollback to a shadow checkpoint:
    1. Create sensitive and gitignored files: .env, .secrets, id_rsa, local.log, id_ed25519.
    2. Take a shadow checkpoint.
    3. Introduce edits to tracked files and create post-checkpoint untracked junk.
    4. Execute rollback to shadow checkpoint.
    5. Verify sensitive and ignored files survive 100%, and post-checkpoint junk is unlinked.
    """
    repo = clean_git_repo

    # 1. Setup sensitive and gitignored files
    env_content = "API_SECRET=live_env_token_999\nDATABASE_URL=postgres://..."
    secrets_content = "CLIENT_SECRET=super_confidential_oauth\n"
    rsa_content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEogIBAAKCAQEA0...\n-----END RSA PRIVATE KEY-----\n"
    ed25519_content = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjE...\n-----END OPENSSH PRIVATE KEY-----\n"
    local_log_content = "2026-11-05 19:30:00 [DEBUG] Local developer session active\n"

    (repo / ".env").write_text(env_content, encoding="utf-8")
    (repo / ".secrets").write_text(secrets_content, encoding="utf-8")
    (repo / "id_rsa").write_text(rsa_content, encoding="utf-8")
    (repo / "id_ed25519").write_text(ed25519_content, encoding="utf-8")
    (repo / "local.log").write_text(local_log_content, encoding="utf-8")

    # 2. Take shadow checkpoint
    cp = git_ops("checkpoint", message="state_before_risky_task")
    assert cp["success"] is True
    checkpoint_id = cp["checkpoint_id"]

    # 3. Post-checkpoint mutations
    (repo / "tracked.py").write_text("def base_func():\n    return 'MUTATED_CORRUPT'\n", encoding="utf-8")
    
    # Create untracked non-ignored junk files
    junk_top = repo / "untracked_junk.tmp"
    junk_top.write_text("untracked junk file at top level\n", encoding="utf-8")
    
    sub_folder = repo / "nested_dir"
    sub_folder.mkdir()
    junk_nested = sub_folder / "nested_junk.py"
    junk_nested.write_text("untracked nested file\n", encoding="utf-8")

    # Verify all exist prior to rollback
    assert (repo / ".env").exists()
    assert (repo / ".secrets").exists()
    assert (repo / "id_rsa").exists()
    assert (repo / "id_ed25519").exists()
    assert (repo / "local.log").exists()
    assert junk_top.exists()
    assert junk_nested.exists()

    # 4. Perform rollback to shadow checkpoint
    rb = git_ops("rollback")
    assert rb["success"] is True
    assert rb["restored_to"] == checkpoint_id

    # 5. Assertions:
    # Tracked file reverted
    assert (repo / "tracked.py").read_text(encoding="utf-8") == "def base_func():\n    return 'original'\n"
    
    # Sensitive and gitignored files survived 100%
    assert (repo / ".env").read_text(encoding="utf-8") == env_content
    assert (repo / ".secrets").read_text(encoding="utf-8") == secrets_content
    assert (repo / "id_rsa").read_text(encoding="utf-8") == rsa_content
    assert (repo / "id_ed25519").read_text(encoding="utf-8") == ed25519_content
    assert (repo / "local.log").read_text(encoding="utf-8") == local_log_content

    # Untracked junk files unlinked
    assert not junk_top.exists(), "Untracked non-ignored file junk_top must be unlinked"
    assert not junk_nested.exists(), "Untracked non-ignored file junk_nested must be unlinked"


def test_git_rollback_to_head_protection(clean_git_repo: Path):
    """
    Adversarial test of git rollback to HEAD:
    1. Create sensitive (.env, .secrets, id_rsa, id_ed25519) and gitignored (local.log) files.
    2. Introduce dirty changes to tracked files and create untracked junk.
    3. Execute rollback to HEAD.
    4. Verify sensitive and ignored files survive 100%, and untracked junk is unlinked.
    """
    repo = clean_git_repo

    env_content = "HEAD_ENV_SECRET=987654321\n"
    secrets_content = "HEAD_SECRETS_DATA=confidential\n"
    rsa_content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQ...\n"
    ed25519_content = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdj...\n"
    local_log_content = "2026-11-05 [INFO] Head log line\n"

    (repo / ".env").write_text(env_content, encoding="utf-8")
    (repo / ".secrets").write_text(secrets_content, encoding="utf-8")
    (repo / "id_rsa").write_text(rsa_content, encoding="utf-8")
    (repo / "id_ed25519").write_text(ed25519_content, encoding="utf-8")
    (repo / "local.log").write_text(local_log_content, encoding="utf-8")

    # Mutate tracked file
    (repo / "tracked.py").write_text("def base_func():\n    return 'MUTATED_BEFORE_HEAD_ROLLBACK'\n", encoding="utf-8")

    # Create untracked non-ignored junk files
    junk_head_1 = repo / "head_junk_file.txt"
    junk_head_1.write_text("head junk file content\n", encoding="utf-8")
    junk_head_2 = repo / "temp_build.obj"
    junk_head_2.write_text("temp binary obj\n", encoding="utf-8")

    assert junk_head_1.exists()
    assert junk_head_2.exists()

    # Perform rollback to HEAD explicitly
    rb = git_ops("rollback", target_ref="HEAD")
    assert rb["success"] is True
    assert rb["restored_to"] == "HEAD"

    # Assertions:
    # Tracked file reverted to HEAD
    assert (repo / "tracked.py").read_text(encoding="utf-8") == "def base_func():\n    return 'original'\n"

    # Sensitive and gitignored files survived 100%
    assert (repo / ".env").read_text(encoding="utf-8") == env_content
    assert (repo / ".secrets").read_text(encoding="utf-8") == secrets_content
    assert (repo / "id_rsa").read_text(encoding="utf-8") == rsa_content
    assert (repo / "id_ed25519").read_text(encoding="utf-8") == ed25519_content
    assert (repo / "local.log").read_text(encoding="utf-8") == local_log_content

    # Junk files unlinked
    assert not junk_head_1.exists(), "head_junk_file.txt should be unlinked by git clean"
    assert not junk_head_2.exists(), "temp_build.obj should be unlinked by git clean"
