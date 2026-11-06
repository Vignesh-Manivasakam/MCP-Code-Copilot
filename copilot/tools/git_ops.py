"""
Consolidated Git Operations Poly-Tool for Code Copilot MCP Server.
==================================================================
Native Git plumbing integration supporting repository status, diff analysis with
stat summaries and 40-line compaction, shadow stash/commit checkpoints without dirtying
active Git working tree, and safe workspace rollback.
"""
from __future__ import annotations

from enum import Enum
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional
import uuid

from config import Config
from copilot.core.security import is_sensitive_file
from copilot.services.telemetry import (
    compact_output,
    estimate_tokens,
    get_token_tracker,
    record_trace,
)

logger = logging.getLogger(__name__)

ERR_NOT_GIT_REPO = "ERR_NOT_GIT_REPO"
ERR_INVALID_ACTION = "ERR_INVALID_ACTION"
ERR_GIT_FAILED = "ERR_GIT_FAILED"


class GitAction(str, Enum):
    STATUS = "status"
    DIFF = "diff"
    CHECKPOINT = "checkpoint"
    ROLLBACK = "rollback"


def _get_project_root() -> Path:
    return Path(Config.PROJECT_ROOT).resolve() if Config.PROJECT_ROOT else Path.cwd().resolve()


def _is_git_repo(repo_root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception:
        return False


def _git_status(repo_root: Path) -> Dict[str, Any]:
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "-u"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return {
            "action": GitAction.STATUS.value,
            "success": False,
            "error": ERR_GIT_FAILED,
            "error_code": ERR_GIT_FAILED,
            "message": proc.stderr.strip() or "git status failed",
        }

    branch_proc = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    branch = branch_proc.stdout.strip()
    if not branch:
        # Detached HEAD or initial commit
        rev_proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        branch = rev_proc.stdout.strip() or "HEAD"

    staged: List[str] = []
    modified: List[str] = []
    untracked: List[str] = []
    deleted: List[str] = []

    exclude_prefixes = (".copilot", ".venv", "node_modules", "__pycache__")
    for line in proc.stdout.splitlines():
        if len(line) < 3:
            continue
        code = line[:2]
        path = line[3:].strip()
        clean_path = path.replace("\\", "/").rstrip("/")
        if clean_path in exclude_prefixes or any(clean_path.startswith(p + "/") for p in exclude_prefixes):
            continue
        if code == "??":
            untracked.append(path)
        else:
            if code[0] in "MADRC":
                staged.append(path)
            if code[1] == "M":
                modified.append(path)
            if code[0] == "D" or code[1] == "D":
                deleted.append(path)

    is_clean = len(staged) == 0 and len(modified) == 0 and len(untracked) == 0 and len(deleted) == 0

    return {
        "action": GitAction.STATUS.value,
        "success": True,
        "branch": branch,
        "is_clean": is_clean,
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
        "deleted": deleted,
        "total_changes": len(staged) + len(modified) + len(untracked) + len(deleted),
        "raw": proc.stdout,
    }


def _git_diff(repo_root: Path, target_ref: Optional[str] = None) -> Dict[str, Any]:
    stat_cmd = ["git", "diff", "--stat"]
    diff_cmd = ["git", "diff"]
    if target_ref:
        stat_cmd.append(target_ref)
        diff_cmd.append(target_ref)

    stat_proc = subprocess.run(
        stat_cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    diff_proc = subprocess.run(
        diff_cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if diff_proc.returncode != 0:
        return {
            "action": GitAction.DIFF.value,
            "success": False,
            "error": ERR_GIT_FAILED,
            "error_code": ERR_GIT_FAILED,
            "message": diff_proc.stderr.strip() or "git diff failed",
        }

    raw_diff = diff_proc.stdout
    lines = raw_diff.splitlines()
    total_lines = len(lines)
    max_lines = getattr(Config, "MAX_OUTPUT_LINES", 40)

    if total_lines > max_lines:
        compacted, is_truncated = compact_output(raw_diff, max_lines=max_lines)
    else:
        compacted, is_truncated = raw_diff, False

    return {
        "action": GitAction.DIFF.value,
        "success": True,
        "target_ref": target_ref,
        "stat": stat_proc.stdout.strip(),
        "diff": compacted,
        "truncated": is_truncated,
        "total_lines": total_lines,
    }


def _git_checkpoint(repo_root: Path, message: Optional[str] = None) -> Dict[str, Any]:
    checkpoint_msg = message or f"copilot_checkpoint_{int(time.time())}"
    dot_git = repo_root / ".git"
    shadow_idx = dot_git / f"copilot_shadow_idx_{uuid.uuid4().hex[:8]}"
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(shadow_idx)

    try:
        # Stage all changes into shadow index
        add_proc = subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )
        if add_proc.returncode != 0:
            return {
                "action": GitAction.CHECKPOINT.value,
                "success": False,
                "error": ERR_GIT_FAILED,
                "error_code": ERR_GIT_FAILED,
                "message": f"git add failed: {add_proc.stderr}",
            }

        # Write tree object
        tree_proc = subprocess.run(
            ["git", "write-tree"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )
        if tree_proc.returncode != 0:
            return {
                "action": GitAction.CHECKPOINT.value,
                "success": False,
                "error": ERR_GIT_FAILED,
                "error_code": ERR_GIT_FAILED,
                "message": f"git write-tree failed: {tree_proc.stderr}",
            }
        tree_hash = tree_proc.stdout.strip()

        # Check if parent HEAD exists
        head_proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        commit_cmd = ["git", "commit-tree", tree_hash, "-m", f"checkpoint: {checkpoint_msg}"]
        if head_proc.returncode == 0 and head_proc.stdout.strip():
            commit_cmd.extend(["-p", head_proc.stdout.strip()])

        commit_proc = subprocess.run(
            commit_cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if commit_proc.returncode != 0:
            return {
                "action": GitAction.CHECKPOINT.value,
                "success": False,
                "error": ERR_GIT_FAILED,
                "error_code": ERR_GIT_FAILED,
                "message": f"git commit-tree failed: {commit_proc.stderr}",
            }
        commit_sha = commit_proc.stdout.strip()

        # Update shadow checkpoint reference
        subprocess.run(
            ["git", "update-ref", "refs/copilot/checkpoints/latest", commit_sha],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )

        return {
            "action": GitAction.CHECKPOINT.value,
            "success": True,
            "checkpoint_id": commit_sha,
            "message": checkpoint_msg,
            "ref": "refs/copilot/checkpoints/latest",
        }
    finally:
        if shadow_idx.exists():
            try:
                shadow_idx.unlink()
            except OSError:
                pass


def _git_rollback(repo_root: Path, target_ref: Optional[str] = None) -> Dict[str, Any]:
    target_commit = target_ref
    if not target_commit:
        verify_ref = subprocess.run(
            ["git", "rev-parse", "--verify", "refs/copilot/checkpoints/latest"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if verify_ref.returncode == 0 and verify_ref.stdout.strip():
            target_commit = verify_ref.stdout.strip()
        else:
            target_commit = "HEAD"

    if target_commit == "HEAD":
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_root, check=True, capture_output=True)
        clean_cmd = ["git", "clean", "-fd"]
        sensitive_patterns = getattr(Config, "SENSITIVE_FILES", {
            ".env",
            ".git/config",
            "id_rsa",
            "id_ed25519",
            ".secrets",
        })
        for pattern in sensitive_patterns:
            clean_cmd.extend(["-e", pattern])
        subprocess.run(clean_cmd, cwd=repo_root, check=True, capture_output=True)
        return {
            "action": GitAction.ROLLBACK.value,
            "success": True,
            "restored_to": "HEAD",
            "message": "Successfully rolled back to HEAD",
        }

    # Rollback to specific shadow commit
    checkout_proc = subprocess.run(
        ["git", "checkout", target_commit, "--", "."],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if checkout_proc.returncode != 0:
        return {
            "action": GitAction.ROLLBACK.value,
            "success": False,
            "error": ERR_GIT_FAILED,
            "error_code": ERR_GIT_FAILED,
            "message": checkout_proc.stderr.strip() or f"Failed to checkout {target_commit}",
        }

    # Remove untracked files not in the checkpoint commit
    tree_proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", target_commit],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if tree_proc.returncode == 0:
        commit_files = set(tree_proc.stdout.splitlines())
        exclude_dirs = set(getattr(Config, "EXCLUDE_DIRS", {".git", ".venv", "node_modules", ".copilot"})) | {".git", ".copilot"}
        candidates: List[tuple[str, Path]] = []
        for root_dir, dirnames, filenames in os.walk(repo_root):
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
            dir_path = Path(root_dir)
            for fname in filenames:
                fpath = dir_path / fname
                try:
                    rel_p = fpath.relative_to(repo_root).as_posix()
                    if rel_p not in commit_files:
                        # Defense 1: Never delete sensitive files (.env, keys, secrets)
                        if is_sensitive_file(fpath) or is_sensitive_file(rel_p):
                            continue
                        candidates.append((rel_p, fpath))
                except Exception:
                    pass

        # Defense 2: Check gitignore via git check-ignore (never delete ignored files)
        if candidates:
            ignored_paths: set[str] = set()
            try:
                check_ignore_proc = subprocess.run(
                    ["git", "check-ignore", "-z", "--stdin"],
                    input=("\0".join(rel_p for rel_p, _ in candidates) + "\0").encode("utf-8"),
                    cwd=repo_root,
                    capture_output=True,
                )
                if check_ignore_proc.returncode in (0, 1):
                    ignored_paths = {
                        p.replace("\\", "/")
                        for p in check_ignore_proc.stdout.decode("utf-8", errors="replace").split("\0")
                        if p
                    }
                else:
                    # Fail-safe: if check-ignore returns error (>1), protect all candidates
                    logger.warning("git check-ignore error during rollback: %s", check_ignore_proc.stderr)
                    ignored_paths = {rel_p for rel_p, _ in candidates}
            except Exception as e:
                logger.warning("git check-ignore exception during rollback: %s", e)
                ignored_paths = {rel_p for rel_p, _ in candidates}

            for rel_p, fpath in candidates:
                if rel_p not in ignored_paths:
                    try:
                        fpath.unlink()
                    except OSError:
                        pass

    return {
        "action": GitAction.ROLLBACK.value,
        "success": True,
        "restored_to": target_commit,
        "message": f"Successfully rolled back to {target_commit}",
    }


def git_ops(
    action: str,
    message: Optional[str] = None,
    target_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Clean Git control using native plumbing and shadow stash checkpoints.

    Parameters:
        action: Git operation ('status', 'diff', 'checkpoint', 'rollback').
        message: Optional commit or checkpoint message.
        target_ref: Optional file path or commit reference for diff or rollback.

    Returns:
        Structured response dictionary with operation results and status metadata.
    """
    start_time = time.perf_counter()
    clean_action = str(action).strip().lower()

    valid_actions = {
        GitAction.STATUS.value,
        GitAction.DIFF.value,
        GitAction.CHECKPOINT.value,
        GitAction.ROLLBACK.value,
    }

    if clean_action not in valid_actions:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        res = {
            "error": ERR_INVALID_ACTION,
            "error_code": ERR_INVALID_ACTION,
            "message": f"Unsupported git action '{action}'. Supported actions: {', '.join(sorted(valid_actions))}",
            "action": action,
            "success": False,
        }
        record_trace(
            tool_name="git_ops",
            duration_ms=elapsed_ms,
            status="error",
            error_code=ERR_INVALID_ACTION,
            estimated_tokens=estimate_tokens(json.dumps(res)),
            metadata={"action": action},
        )
        return res

    repo_root = _get_project_root()
    if not _is_git_repo(repo_root):
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        res = {
            "error": ERR_NOT_GIT_REPO,
            "error_code": ERR_NOT_GIT_REPO,
            "message": f"Directory '{repo_root}' is not a git repository",
            "action": clean_action,
            "success": False,
        }
        record_trace(
            tool_name="git_ops",
            duration_ms=elapsed_ms,
            status="error",
            error_code=ERR_NOT_GIT_REPO,
            estimated_tokens=estimate_tokens(json.dumps(res)),
            metadata={"action": clean_action},
        )
        return res

    if clean_action == GitAction.STATUS.value:
        res = _git_status(repo_root)
    elif clean_action == GitAction.DIFF.value:
        res = _git_diff(repo_root, target_ref=target_ref)
    elif clean_action == GitAction.CHECKPOINT.value:
        res = _git_checkpoint(repo_root, message=message)
    elif clean_action == GitAction.ROLLBACK.value:
        res = _git_rollback(repo_root, target_ref=target_ref)
    else:
        res = {"error": ERR_INVALID_ACTION, "error_code": ERR_INVALID_ACTION, "success": False}

    elapsed_ms = (time.perf_counter() - start_time) * 1000
    res["execution_time_ms"] = round(elapsed_ms, 2)

    # Telemetry tracing
    tokens = estimate_tokens(json.dumps(res, default=str))
    record_trace(
        tool_name="git_ops",
        duration_ms=elapsed_ms,
        status="success" if res.get("success") else "error",
        error_code=res.get("error_code"),
        estimated_tokens=tokens,
        metadata={"action": clean_action},
    )

    # Token warning injection
    tracker = get_token_tracker()
    if tracker.is_warning_active():
        res["warning"] = tracker.get_warning_header()

    return res


def register_git_tools(mcp: Any) -> List[str]:
    """Register git_ops tool on FastMCP instance."""
    mcp.tool()(git_ops)
    return ["git_ops"]


__all__ = [
    "git_ops",
    "register_git_tools",
    "GitAction",
    "ERR_NOT_GIT_REPO",
    "ERR_INVALID_ACTION",
    "ERR_GIT_FAILED",
]
