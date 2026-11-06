"""
Sandbox Module for Code Copilot.
================================
Sandboxed command execution with binary whitelisting, Windows .cmd resolution,
environment variable scrubbing, process tree timeout killing, and output truncation.
"""
from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Optional, Set

from config import Config

logger = logging.getLogger(__name__)

# Standard error taxonomy codes
ERR_SECURITY_VIOLATION = "ERR_SECURITY_VIOLATION"
ERR_COMMAND_TIMEOUT = "ERR_COMMAND_TIMEOUT"
ERR_NOT_FOUND = "ERR_NOT_FOUND"
ERR_PATH_TRAVERSAL = "ERR_PATH_TRAVERSAL"
ERR_COMMAND_FAILED = "ERR_COMMAND_FAILED"


def normalize_command_name(command: str) -> str:
    """
    Extract and normalize the base binary stem for whitelist comparison.
    Strips directory prefixes, lowercases, and removes extensions (.exe, .cmd, .bat).
    """
    if not command or not command.strip():
        return ""
    clean = command.strip().replace("\\", "/").rstrip("/")
    base_name = clean.split("/")[-1].lower()
    for ext in (".exe", ".cmd", ".bat"):
        if base_name.endswith(ext):
            base_name = base_name[:-len(ext)]
            break
    return base_name


def _is_trusted_binary_path(path: str | Path) -> bool:
    """
    Verify whether an explicit file path resolves strictly to sys.executable
    or within the active virtual environment's Scripts/bin directory.
    Arbitrary local paths, temporary directories, and workspace files are rejected.
    """
    try:
        resolved_p = Path(path).resolve()
        if not resolved_p.is_file():
            return False

        # Allow exact match with the current Python runtime
        if resolved_p == Path(sys.executable).resolve():
            return True

        # Allow executables located strictly within the active virtual environment's Scripts or bin
        venv_dirs: list[Path] = []
        if sys.prefix:
            venv_dirs.append((Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")).resolve())
        if getattr(sys, "base_prefix", None) and sys.base_prefix != sys.prefix:
            venv_dirs.append((Path(sys.base_prefix) / ("Scripts" if sys.platform == "win32" else "bin")).resolve())

        return any(resolved_p.parent == vd for vd in venv_dirs)
    except Exception:
        return False


def is_command_allowed(
    command: str,
    allowed_commands: Optional[Set[str]] = None,
) -> bool:
    """Verify whether the command binary is permitted by Config.ALLOWED_COMMANDS."""
    if not command or not command.strip():
        return False

    clean = command.strip()

    # Reject path-prefixed commands unless they resolve strictly within trusted runtime/venv dirs
    if "/" in clean or "\\" in clean:
        if not _is_trusted_binary_path(clean):
            return False

    if allowed_commands is None:
        allowed_commands = getattr(Config, "ALLOWED_COMMANDS", set())
    norm = normalize_command_name(clean)
    if not norm:
        return False
    return norm in allowed_commands


def resolve_binary_path(command: str) -> Optional[str]:
    """
    Resolve executable path using shutil.which on Windows/POSIX with fallback to sys.executable.
    Prevents path spoofing by verifying trusted directories and excluding untrusted cwd/workspace binaries.
    """
    clean = command.strip()
    if not clean:
        return None

    # 1. If explicit path is provided, only resolve if strictly trusted
    if "/" in clean or "\\" in clean:
        if _is_trusted_binary_path(clean):
            return str(Path(clean).resolve())
        return None

    # 2. Simple binary name resolution
    norm = normalize_command_name(clean)
    if norm == "python":
        return sys.executable

    # 3. Check virtual environment Scripts/bin first (e.g. pytest, ruff in active venv)
    if sys.prefix:
        scripts_dir = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")
        for ext in ("", ".exe", ".cmd", ".bat"):
            candidate = scripts_dir / f"{clean}{ext}"
            if candidate.is_file():
                return str(candidate.resolve())

    # 4. Resolve via shutil.which against system PATH, defending against Windows cwd hijacking
    resolved = shutil.which(clean)
    if resolved:
        res_p = Path(resolved).resolve()
        cwd_p = Path.cwd().resolve()
        project_root = getattr(Config, "PROJECT_ROOT", None)
        root_p = project_root.resolve() if project_root else None

        # Check if the resolved executable came from cwd or workspace root
        is_in_cwd = res_p == cwd_p or cwd_p in res_p.parents
        is_in_root = (res_p == root_p or root_p in res_p.parents) if root_p else False

        # Allow if located inside .venv
        is_inside_venv = False
        if sys.prefix:
            venv_p = Path(sys.prefix).resolve()
            is_inside_venv = res_p == venv_p or venv_p in res_p.parents

        if (is_in_cwd or is_in_root) and not is_inside_venv:
            return None

        return str(res_p)

    return None


def scrub_environment(
    base_env: Optional[dict[str, str]] = None,
    patterns: Optional[list[str]] = None,
) -> dict[str, str]:
    """
    Copy environment variables and scrub sensitive keys matching patterns.
    Preserves vital system variables (PATH, SYSTEMROOT, etc.) while removing credentials.
    """
    if base_env is None:
        base_env = dict(os.environ)
    if patterns is None:
        patterns = getattr(
            Config,
            "ENV_SCRUB_PATTERNS",
            ["*KEY*", "*TOKEN*", "*SECRET*", "AWS_*", "*PASSWORD*", "*CREDENTIAL*"],
        )

    clean: dict[str, str] = {}
    for k, v in base_env.items():
        k_upper = k.upper()
        if not any(fnmatch.fnmatch(k_upper, pat.upper()) for pat in patterns):
            clean[k] = str(v)

    clean["PYTHONUNBUFFERED"] = "1"
    return clean


def kill_process_tree(pid: int) -> None:
    """
    Terminate a process and all its children recursively.
    Uses taskkill /F /T on Windows to defend against runaway worker trees.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except Exception as exc:
            logger.warning("taskkill failed for PID %d: %s", pid, exc)

    try:
        import psutil
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        parent.kill()
    except Exception:
        pass


def truncate_output(text: str, max_lines: int = Config.MAX_OUTPUT_LINES) -> tuple[str, bool]:
    """
    Truncate output lines exceeding max_lines, preserving the last lines with a notice header.
    """
    if not text:
        return "", False
    lines = text.splitlines()
    total = len(lines)
    if total <= max_lines:
        return text, False

    notice = f"[Output truncated: showing last {max_lines} of {total} lines]"
    truncated_lines = [notice] + lines[-max_lines:]
    return "\n".join(truncated_lines), True


def run_sandboxed_command(
    command: str,
    arguments: Optional[list[str]] = None,
    timeout_seconds: int = Config.DEFAULT_TIMEOUT_SECONDS,
    cwd: Optional[Path | str] = None,
    env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """
    Execute a subprocess under security sandboxing constraints.

    Checks performed:
    1. Whitelist validation against Config.ALLOWED_COMMANDS.
    2. Executable resolution (.cmd/.bat support on Windows via shutil.which).
    3. Working directory containment and validation.
    4. Environment scrubbing of sensitive tokens, secrets, and keys.
    5. Process tree termination upon timeout.
    6. Output truncation beyond Config.MAX_OUTPUT_LINES (40).

    Returns:
        {
            "exit_code": int,
            "stdout": str,
            "stderr": str,
            "truncated": bool,
            "success": bool,
            "error_code": str | None,
            "error": str | None,
            "execution_time_ms": float,
            "command": str,
        }
    """
    args = list(arguments) if arguments is not None else []
    display_cmd = f"{command} {' '.join(str(a) for a in args)}".strip()

    # 1. Whitelist validation
    if not is_command_allowed(command):
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Command '{command}' is not whitelisted.",
            "truncated": False,
            "success": False,
            "error_code": ERR_SECURITY_VIOLATION,
            "error": f"Command '{command}' is not whitelisted",
            "execution_time_ms": 0.0,
            "command": display_cmd,
        }

    # 2. Executable resolution
    resolved_bin = resolve_binary_path(command)
    if not resolved_bin:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": f"Executable '{command}' not found on system PATH.",
            "truncated": False,
            "success": False,
            "error_code": ERR_NOT_FOUND,
            "error": f"Executable '{command}' not found",
            "execution_time_ms": 0.0,
            "command": display_cmd,
        }

    # 3. Working directory resolution
    resolved_cwd = Path(cwd).resolve() if cwd else (Config.PROJECT_ROOT or Path.cwd())
    if not resolved_cwd.is_dir():
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Working directory does not exist: {resolved_cwd}",
            "truncated": False,
            "success": False,
            "error_code": ERR_PATH_TRAVERSAL,
            "error": f"Working directory does not exist: {resolved_cwd}",
            "execution_time_ms": 0.0,
            "command": display_cmd,
        }

    # 4. Environment scrubbing
    clean_env = scrub_environment(base_env=env)

    # 5. Execution with process tree timeout
    clamped_timeout = min(
        max(timeout_seconds, 1),
        getattr(Config, "COMMAND_TIMEOUT_MAX", 300),
    )
    full_cmd = [resolved_bin] + [str(a) for a in args]

    start_time = time.perf_counter()
    try:
        proc = subprocess.Popen(
            full_cmd,
            cwd=resolved_cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=clean_env,
        )
        stdout, stderr = proc.communicate(timeout=clamped_timeout)
        elapsed_ms = (time.perf_counter() - start_time) * 1000
    except subprocess.TimeoutExpired:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        kill_process_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except Exception:
            stdout, stderr = "", ""
        return {
            "exit_code": -1,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "truncated": False,
            "success": False,
            "error_code": ERR_COMMAND_TIMEOUT,
            "error": f"Command timed out after {clamped_timeout} seconds",
            "execution_time_ms": round(elapsed_ms, 2),
            "command": display_cmd,
        }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": str(exc),
            "truncated": False,
            "success": False,
            "error_code": ERR_COMMAND_FAILED,
            "error": str(exc),
            "execution_time_ms": round(elapsed_ms, 2),
            "command": display_cmd,
        }

    # 6. Output truncation
    max_lines = getattr(Config, "MAX_OUTPUT_LINES", 40)
    trunc_stdout, stdout_trunc = truncate_output(stdout, max_lines=max_lines)
    trunc_stderr, stderr_trunc = truncate_output(stderr, max_lines=max_lines)
    is_truncated = stdout_trunc or stderr_trunc

    return {
        "exit_code": proc.returncode,
        "stdout": trunc_stdout,
        "stderr": trunc_stderr,
        "truncated": is_truncated,
        "success": proc.returncode == 0,
        "error_code": None if proc.returncode == 0 else ERR_COMMAND_FAILED,
        "error": None if proc.returncode == 0 else f"Command failed with exit code {proc.returncode}",
        "execution_time_ms": round(elapsed_ms, 2),
        "command": display_cmd,
    }


__all__ = [
    "run_sandboxed_command",
    "is_command_allowed",
    "resolve_binary_path",
    "scrub_environment",
    "kill_process_tree",
    "truncate_output",
    "normalize_command_name",
    "ERR_SECURITY_VIOLATION",
    "ERR_COMMAND_TIMEOUT",
    "ERR_NOT_FOUND",
    "ERR_PATH_TRAVERSAL",
    "ERR_COMMAND_FAILED",
]
