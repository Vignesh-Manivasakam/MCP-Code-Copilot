"""
Telemetry and Observability Service for Code Copilot MCP Server.
==============================================================
Provides structured JSONL tracing, standardized error taxonomy,
session diagnostics, token budget tracking against 1M quota (>80% warning),
and smart 40-line output compaction.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Standard Error Taxonomy
# ---------------------------------------------------------------------------
class ErrorTaxonomy(str, Enum):
    """Standardized error categorization codes across all copilot modules."""
    ERR_DIFF_MISMATCH = "ERR_DIFF_MISMATCH"
    ERR_STALE_READ = "ERR_STALE_READ"
    ERR_NOT_FOUND = "ERR_NOT_FOUND"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"
    ERR_SECURITY_VIOLATION = "ERR_SECURITY_VIOLATION"
    TIMEOUT = "TIMEOUT"
    ERR_COMMAND_TIMEOUT = "ERR_COMMAND_TIMEOUT"
    ERR_INVALID_REGEX = "ERR_INVALID_REGEX"
    ERR_PATH_TRAVERSAL = "ERR_PATH_TRAVERSAL"
    ERR_COMMAND_FAILED = "ERR_COMMAND_FAILED"
    ERR_SYNTAX_ERROR = "ERR_SYNTAX_ERROR"
    ERR_INTERNAL = "ERR_INTERNAL"


# Module-level constant exports for direct import
ERR_DIFF_MISMATCH = ErrorTaxonomy.ERR_DIFF_MISMATCH.value
ERR_STALE_READ = ErrorTaxonomy.ERR_STALE_READ.value
ERR_NOT_FOUND = ErrorTaxonomy.ERR_NOT_FOUND.value
SECURITY_VIOLATION = ErrorTaxonomy.SECURITY_VIOLATION.value
ERR_SECURITY_VIOLATION = ErrorTaxonomy.ERR_SECURITY_VIOLATION.value
TIMEOUT = ErrorTaxonomy.TIMEOUT.value
ERR_COMMAND_TIMEOUT = ErrorTaxonomy.ERR_COMMAND_TIMEOUT.value
ERR_INVALID_REGEX = ErrorTaxonomy.ERR_INVALID_REGEX.value
ERR_PATH_TRAVERSAL = ErrorTaxonomy.ERR_PATH_TRAVERSAL.value
ERR_COMMAND_FAILED = ErrorTaxonomy.ERR_COMMAND_FAILED.value
ERR_SYNTAX_ERROR = ErrorTaxonomy.ERR_SYNTAX_ERROR.value
ERR_INTERNAL = ErrorTaxonomy.ERR_INTERNAL.value

_CANONICAL_ERROR_MAP = {
    "SECURITY_VIOLATION": SECURITY_VIOLATION,
    "ERR_SECURITY_VIOLATION": SECURITY_VIOLATION,
    "TIMEOUT": TIMEOUT,
    "ERR_COMMAND_TIMEOUT": TIMEOUT,
    "TIMEOUT_ERROR": TIMEOUT,
    "FILE_NOT_FOUND": ERR_NOT_FOUND,
    "DIRECTORY_NOT_FOUND": ERR_NOT_FOUND,
    "ERR_NOT_FOUND": ERR_NOT_FOUND,
    "PATH_TRAVERSAL": ERR_PATH_TRAVERSAL,
    "ERR_PATH_TRAVERSAL": ERR_PATH_TRAVERSAL,
    "DIFF_MISMATCH": ERR_DIFF_MISMATCH,
    "ERR_DIFF_MISMATCH": ERR_DIFF_MISMATCH,
    "STALE_READ": ERR_STALE_READ,
    "ERR_STALE_READ": ERR_STALE_READ,
    "INVALID_REGEX": ERR_INVALID_REGEX,
    "ERR_INVALID_REGEX": ERR_INVALID_REGEX,
    "COMMAND_FAILED": ERR_COMMAND_FAILED,
    "ERR_COMMAND_FAILED": ERR_COMMAND_FAILED,
    "SYNTAX_ERROR": ERR_SYNTAX_ERROR,
    "ERR_SYNTAX_ERROR": ERR_SYNTAX_ERROR,
    "INTERNAL_ERROR": ERR_INTERNAL,
    "ERR_INTERNAL": ERR_INTERNAL,
}


def normalize_error_code(code: Optional[str]) -> Optional[str]:
    """Map error code or alias to canonical taxonomy string."""
    if not code:
        return None
    clean = str(code).strip()
    return _CANONICAL_ERROR_MAP.get(clean, clean)


# ---------------------------------------------------------------------------
# Token Estimation & Budget Tracker
# ---------------------------------------------------------------------------
def estimate_tokens(text: Any) -> int:
    """
    Approximate token count from text or payload using Config.CHARS_PER_TOKEN (4.0).
    """
    if not text:
        return 0
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    elif not isinstance(text, str):
        text = str(text)
    chars = len(text)
    chars_per_token = getattr(Config, "CHARS_PER_TOKEN", 4.0)
    return int(chars / chars_per_token) if chars_per_token > 0 else chars // 4


class TokenTracker:
    """Session token budget tracker with 80% quota warning."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_estimated_tokens: int = 0

    @property
    def budget_limit(self) -> int:
        return getattr(Config, "TOKEN_BUDGET_LIMIT", 1_000_000)

    @property
    def warning_threshold(self) -> float:
        return getattr(Config, "TOKEN_WARNING_THRESHOLD", 0.8)

    def record_usage(
        self,
        input_text: Optional[str] = None,
        output_text: Optional[str] = None,
        tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Record input/output token approximations into session budget."""
        with self._lock:
            prompt_inc = estimate_tokens(input_text) if input_text else 0
            comp_inc = estimate_tokens(output_text) if output_text else 0
            if tokens is not None:
                self.total_estimated_tokens += tokens
            else:
                self.prompt_tokens += prompt_inc
                self.completion_tokens += comp_inc
                self.total_estimated_tokens += (prompt_inc + comp_inc)
            return self._get_usage_unlocked()

    def get_usage(self) -> Dict[str, Any]:
        """Get live session token consumption metrics."""
        with self._lock:
            return self._get_usage_unlocked()

    def _get_usage_unlocked(self) -> Dict[str, Any]:
        total = self.total_estimated_tokens
        budget = self.budget_limit
        threshold = self.warning_threshold
        warning_active = total >= int(budget * threshold)
        remaining = max(0, budget - total)
        percent_used = round((total / budget) * 100, 2) if budget > 0 else 0.0

        warning_message = None
        if warning_active:
            warning_message = (
                f"[WARNING: Session token consumption exceeds {int(threshold * 100)}% "
                f"of budget limit ({total:,} / {budget:,} tokens). "
                f"Consider starting a new session or narrowing query scope.]"
            )

        return {
            "total_estimated_tokens": total,
            "budget_limit": budget,
            "remaining": remaining,
            "percent_used": percent_used,
            "warning": warning_active,
            "warning_header": warning_active,
            "warning_message": warning_message,
        }

    def is_warning_active(self) -> bool:
        """Check whether token consumption exceeds warning threshold (80%)."""
        with self._lock:
            return self.total_estimated_tokens >= int(self.budget_limit * self.warning_threshold)

    def get_warning_header(self) -> Optional[str]:
        """Return non-intrusive warning header string if threshold exceeded."""
        usage = self.get_usage()
        return usage["warning_message"]

    def reset(self) -> None:
        """Reset session token tracker state."""
        with self._lock:
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.total_estimated_tokens = 0


# Global singleton
_token_tracker = TokenTracker()


def get_token_tracker() -> TokenTracker:
    return _token_tracker


def get_token_usage() -> Dict[str, Any]:
    """Live token usage for @mcp.resource('session://token-usage') and diagnostics."""
    return _token_tracker.get_usage()


def reset_token_usage() -> None:
    _token_tracker.reset()


# ---------------------------------------------------------------------------
# Structured JSONL Tracing
# ---------------------------------------------------------------------------
_trace_lock = threading.Lock()
_in_memory_traces: deque[Dict[str, Any]] = deque(maxlen=500)


def _resolve_trace_log_path(override_path: Optional[Path | str] = None) -> Path:
    """Dynamically determine trace.jsonl path to honor Config.PROJECT_ROOT changes."""
    if override_path is not None:
        return Path(override_path).resolve()
    if Config.TRACE_LOG_PATH is not None:
        return Path(Config.TRACE_LOG_PATH).resolve()
    base = Config.PROJECT_ROOT or Path.cwd()
    return (Path(base) / ".copilot" / "logs" / "trace.jsonl").resolve()


def record_trace(
    tool_name: str,
    duration_ms: float,
    status: str = "success",
    error_code: Optional[str] = None,
    estimated_tokens: int = 0,
    metadata: Optional[Dict[str, Any]] = None,
    log_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """
    Append a structured JSON record to Config.TRACE_LOG_PATH (.copilot/logs/trace.jsonl).

    Resilient: swallows file I/O exceptions so telemetry logging never disrupts user workflows.
    """
    norm_error = normalize_error_code(error_code)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": str(tool_name),
        "duration_ms": round(float(duration_ms), 3),
        "status": str(status),
        "error_code": norm_error,
        "estimated_tokens": int(estimated_tokens),
        "metadata": metadata or {},
    }

    # Record in-memory buffer
    with _trace_lock:
        _in_memory_traces.append(record)

    # Accumulate tokens if provided
    if estimated_tokens > 0:
        _token_tracker.record_usage(tokens=estimated_tokens)

    # Respect ENABLE_TELEMETRY flag
    if not getattr(Config, "ENABLE_TELEMETRY", True):
        return record

    # Append to JSONL file
    target_path = _resolve_trace_log_path(log_path)
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
        with _trace_lock:
            with open(target_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    except Exception as exc:
        logger.warning("Failed to write telemetry trace to %s: %s", target_path, exc)

    return record


def get_recent_traces(
    limit: int = 50,
    tool_name: Optional[str] = None,
    log_path: Optional[Path | str] = None,
) -> List[Dict[str, Any]]:
    """
    Read the most recent trace records from JSONL log file or in-memory fallback.
    """
    target_path = _resolve_trace_log_path(log_path)
    file_records: List[Dict[str, Any]] = []

    if target_path.exists() and target_path.is_file():
        try:
            with _trace_lock:
                with open(target_path, "r", encoding="utf-8") as f:
                    for line in f:
                        clean = line.strip()
                        if not clean:
                            continue
                        try:
                            file_records.append(json.loads(clean))
                        except json.JSONDecodeError:
                            continue
        except Exception as exc:
            logger.warning("Error reading traces from %s: %s", target_path, exc)

    # If file was empty or unavailable, fallback to in-memory deque under lock
    with _trace_lock:
        records = file_records if file_records else list(_in_memory_traces)

    if tool_name:
        records = [r for r in records if r.get("tool_name") == tool_name]

    return records[-limit:]


# ---------------------------------------------------------------------------
# Session Diagnostics
# ---------------------------------------------------------------------------
def get_session_diagnostics(
    traces_limit: int = 50,
    log_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """
    Aggregate session performance metrics:
    - total tool calls, success/failure counts, error rates
    - calls breakdown by tool and error breakdown by error code
    - live token usage metrics against 1M budget
    - list of recent traces
    """
    traces = get_recent_traces(limit=traces_limit, log_path=log_path)
    total_calls = len(traces)
    successful_calls = 0
    failed_calls = 0
    total_duration = 0.0
    calls_by_tool: Dict[str, int] = {}
    errors_by_code: Dict[str, int] = {}

    for t in traces:
        tool = t.get("tool_name", "unknown")
        calls_by_tool[tool] = calls_by_tool.get(tool, 0) + 1

        duration = float(t.get("duration_ms", 0.0))
        total_duration += duration

        status = t.get("status", "success")
        err = t.get("error_code")

        if status == "error" or err is not None:
            failed_calls += 1
            if err:
                norm_err = normalize_error_code(err) or "UNKNOWN"
                errors_by_code[norm_err] = errors_by_code.get(norm_err, 0) + 1
        else:
            successful_calls += 1

    error_rate = round(failed_calls / total_calls, 4) if total_calls > 0 else 0.0
    avg_duration = round(total_duration / total_calls, 2) if total_calls > 0 else 0.0

    return {
        "total_tool_calls": total_calls,
        "successful_calls": successful_calls,
        "failed_calls": failed_calls,
        "error_rate": error_rate,
        "total_duration_ms": round(total_duration, 2),
        "avg_duration_ms": avg_duration,
        "calls_by_tool": calls_by_tool,
        "errors_by_code": errors_by_code,
        "token_usage": get_token_usage(),
        "warning_active": _token_tracker.is_warning_active(),
        "warning_header": _token_tracker.is_warning_active(),
        "recent_traces": traces,
    }


def reset_telemetry(log_path: Optional[Path | str] = None) -> None:
    """Reset all in-memory and disk telemetry state (for test isolation)."""
    with _trace_lock:
        _in_memory_traces.clear()
        reset_token_usage()
        target_path = _resolve_trace_log_path(log_path)
        if target_path.exists() and target_path.is_file():
            try:
                target_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Smart Output Compaction (40-Line Maximum)
# ---------------------------------------------------------------------------
def compact_output(text: str, max_lines: int = Config.MAX_OUTPUT_LINES) -> Tuple[str, bool]:
    """
    Compress long test logs, diffs, or command output to <= max_lines (default 40).

    Returns:
        (compacted_text, was_compacted)
    """
    if not text:
        return "", False

    lines = text.splitlines()
    total = len(lines)
    if total <= max_lines:
        return text, False

    # Strategy A: Pytest / test runner failure logs
    is_test_log = any(
        re.search(pat, text)
        for pat in (
            r"===+ FAILURES ===+",
            r"===+ short test summary info ===+",
            r"FAILED\s+",
            r"Traceback \(most recent call last\):",
            r"E   AssertionError",
        )
    )
    if is_test_log:
        return _compact_test_log(lines, max_lines, total)

    # Strategy B: Git diff logs
    is_git_diff = any(line.startswith("diff --git ") or line.startswith("@@ ") for line in lines[:10])
    if is_git_diff:
        return _compact_git_diff(lines, max_lines, total)

    # Strategy C: General command output (Head + Notice + Tail)
    return _compact_general_output(lines, max_lines, total)


def _compact_test_log(lines: List[str], max_lines: int, total: int) -> Tuple[str, bool]:
    summary_lines: List[str] = []
    for line in reversed(lines[-15:]):
        if re.search(r"===+ .* in [\d\.]+s ===+", line) or re.search(r"FAILED\s+", line):
            summary_lines.insert(0, line)
            if len(summary_lines) >= 3:
                break

    if not summary_lines and lines:
        summary_lines = [lines[-1]]

    summary_set = set(summary_lines)
    failure_lines: List[str] = []
    in_failure = False
    for line in lines:
        if "=== short test summary info ===" in line or line in summary_set:
            in_failure = False
        elif "___ test_" in line or "Traceback" in line or line.startswith("E ") or line.startswith("E   "):
            in_failure = True
        if in_failure:
            failure_lines.append(line)

    notice = f"[Output compacted: showing failing test traces and summary ({max_lines} of {total} lines)]"
    allowed_traces = max(0, max_lines - len(summary_lines) - 1)

    raw_traces = failure_lines if failure_lines else [l for l in lines if l not in summary_set and "=== short test summary info ===" not in l]
    selected_traces = [l for l in raw_traces[-allowed_traces:] if l not in summary_set]
    result_lines = [notice] + selected_traces + summary_lines
    return "\n".join(result_lines[:max_lines]), True


def _compact_git_diff(lines: List[str], max_lines: int, total: int) -> Tuple[str, bool]:
    notice = f"[Output compacted: git diff exceeds {max_lines} lines; showing summary and leading changes ({max_lines} of {total} lines)]"
    allowed = max_lines - 1
    result_lines = [notice] + lines[:allowed]
    return "\n".join(result_lines[:max_lines]), True


def _compact_general_output(lines: List[str], max_lines: int, total: int) -> Tuple[str, bool]:
    head_count = 3
    tail_count = max(0, max_lines - head_count - 1)
    notice = f"[Output compacted: showing first {head_count} and last {tail_count} of {total} lines]"
    result_lines = lines[:head_count] + [notice] + lines[-tail_count:]
    return "\n".join(result_lines[:max_lines]), True
