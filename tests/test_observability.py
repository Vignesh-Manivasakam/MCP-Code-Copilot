"""
Authoritative Test Suite 8: Observability & Telemetry.
======================================================
Tests structured JSONL tracing, standardized error categorization,
session diagnostics, token tracking (>80% quota warning), and smart 40-line output compaction.
"""
import json
from pathlib import Path
import pytest

from config import Config
from copilot.services.telemetry import (
    ERR_DIFF_MISMATCH,
    SECURITY_VIOLATION,
    TIMEOUT,
    TokenTracker,
    compact_output,
    estimate_tokens,
    get_recent_traces,
    get_session_diagnostics,
    get_token_tracker,
    get_token_usage,
    record_trace,
    reset_telemetry,
    reset_token_usage,
)


@pytest.fixture(autouse=True)
def clean_telemetry_state():
    """Ensure every test begins with clean telemetry and token tracker state."""
    reset_telemetry()
    yield
    reset_telemetry()


def test_telemetry_trace_generation(tmp_path):
    """Executing a tool writes a structured JSON record to .copilot/logs/trace.jsonl."""
    log_file = tmp_path / ".copilot" / "logs" / "trace.jsonl"
    Config.PROJECT_ROOT = tmp_path

    record = record_trace(
        tool_name="file_ops",
        duration_ms=45.123,
        status="success",
        error_code=None,
        estimated_tokens=150,
        metadata={"action": "write", "file": "src/app.py"},
        log_path=log_file,
    )

    assert record["tool_name"] == "file_ops"
    assert record["duration_ms"] == 45.123
    assert record["status"] == "success"
    assert record["error_code"] is None
    assert record["estimated_tokens"] == 150
    assert record["metadata"]["action"] == "write"

    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["tool_name"] == "file_ops"
    assert parsed["duration_ms"] == 45.123


def test_telemetry_get_session_diagnostics(tmp_path):
    """Calling get_session_diagnostics() returns recent tool performance and error rates."""
    log_file = tmp_path / ".copilot" / "logs" / "trace.jsonl"

    # Initially empty session
    empty_diag = get_session_diagnostics(log_path=log_file)
    assert empty_diag["total_tool_calls"] == 0
    assert empty_diag["error_rate"] == 0.0
    assert empty_diag["avg_duration_ms"] == 0.0

    # Record two successes and one error
    record_trace("file_ops", 20.0, "success", log_path=log_file)
    record_trace("code_search", 30.0, "success", log_path=log_file)
    record_trace("run_command", 100.0, "error", error_code="ERR_COMMAND_FAILED", log_path=log_file)

    diag = get_session_diagnostics(log_path=log_file)
    assert diag["total_tool_calls"] == 3
    assert diag["successful_calls"] == 2
    assert diag["failed_calls"] == 1
    assert diag["error_rate"] == round(1 / 3, 4)
    assert diag["total_duration_ms"] == 150.0
    assert diag["avg_duration_ms"] == 50.0
    assert diag["calls_by_tool"]["file_ops"] == 1
    assert diag["calls_by_tool"]["code_search"] == 1
    assert diag["calls_by_tool"]["run_command"] == 1
    assert diag["errors_by_code"]["ERR_COMMAND_FAILED"] == 1


def test_telemetry_error_categorization(tmp_path):
    """A failed patch is classified under ERR_DIFF_MISMATCH with exact millisecond duration."""
    log_file = tmp_path / ".copilot" / "logs" / "trace.jsonl"

    record = record_trace(
        tool_name="file_ops",
        duration_ms=12.456,
        status="error",
        error_code="ERR_DIFF_MISMATCH",
        log_path=log_file,
    )

    assert record["error_code"] == ERR_DIFF_MISMATCH
    assert record["duration_ms"] == 12.456

    # Verify alias normalization
    record_alias = record_trace(
        tool_name="run_command",
        duration_ms=30000.0,
        status="error",
        error_code="TIMEOUT_ERROR",
        log_path=log_file,
    )
    assert record_alias["error_code"] == TIMEOUT

    record_sec = record_trace(
        tool_name="file_ops",
        duration_ms=2.0,
        status="error",
        error_code="ERR_SECURITY_VIOLATION",
        log_path=log_file,
    )
    assert record_sec["error_code"] == SECURITY_VIOLATION


def test_telemetry_token_usage_tracking():
    """Approximates input and output character counts across tool calls."""
    tracker = TokenTracker()

    # 400 chars of input -> 100 tokens at 4.0 chars/token
    input_text = "x" * 400
    # 200 chars of output -> 50 tokens
    output_text = "y" * 200

    usage = tracker.record_usage(input_text=input_text, output_text=output_text)
    assert tracker.prompt_tokens == 100
    assert tracker.completion_tokens == 50
    assert tracker.total_estimated_tokens == 150
    assert usage["total_estimated_tokens"] == 150
    assert usage["percent_used"] == round((150 / 1_000_000) * 100, 2)
    assert usage["warning"] is False
    assert usage["warning_header"] is False
    assert usage["warning_message"] is None


def test_telemetry_token_quota_warning():
    """Produces warning header when token usage exceeds 80% quota (800,000 tokens)."""
    tracker = TokenTracker()

    # Record 850,000 tokens directly
    usage = tracker.record_usage(tokens=850_000)
    assert usage["warning"] is True
    assert usage["warning_header"] is True
    assert tracker.is_warning_active() is True
    assert usage["warning_message"] is not None
    assert "80%" in usage["warning_message"]
    assert "850,000" in usage["warning_message"]

    header = tracker.get_warning_header()
    assert header is not None
    assert "WARNING" in header


def test_telemetry_output_compaction_pytest():
    """Captures long pytest test execution logs and returns <= 40 lines with summary + failure traces."""
    # Generate 5,000-line simulated pytest output with failures
    lines = [f"test_file.py::test_case_{i} PASSED" for i in range(4980)]
    lines.append("=================================== FAILURES ===================================")
    lines.append("____________________________ test_something_broken _____________________________")
    lines.append("Traceback (most recent call last):")
    lines.append('  File "tests/test_example.py", line 42, in test_something_broken')
    lines.append("    assert actual == expected")
    lines.append("E   AssertionError: assert 1 == 2")
    lines.append("=========================== short test summary info ===========================")
    lines.append("FAILED tests/test_example.py::test_something_broken - AssertionError: assert 1 == 2")
    lines.append("======================== 1 failed, 4980 passed in 10.50s ========================")

    full_output = "\n".join(lines)
    compacted, was_compacted = compact_output(full_output, max_lines=40)

    assert was_compacted is True
    compacted_lines = compacted.splitlines()
    assert len(compacted_lines) <= 40
    # Must preserve notice, failure trace and summary info
    assert any("[Output compacted:" in l for l in compacted_lines)
    assert any("AssertionError" in l for l in compacted_lines)
    assert any("1 failed" in l for l in compacted_lines)


def test_telemetry_output_compaction_git_diff():
    """Compresses large git diffs over 40 lines."""
    lines = ["diff --git a/big_file.py b/big_file.py", "index 0000000..1111111 100644", "--- a/big_file.py", "+++ b/big_file.py", "@@ -1,500 +1,500 @@"]
    for i in range(500):
        lines.append(f"+ added line number {i}")

    full_diff = "\n".join(lines)
    compacted, was_compacted = compact_output(full_diff, max_lines=40)

    assert was_compacted is True
    compacted_lines = compacted.splitlines()
    assert len(compacted_lines) <= 40
    assert any("git diff exceeds" in l for l in compacted_lines)
    assert any("+ added line number" in l for l in compacted_lines)


def test_telemetry_output_compaction_short_text():
    """Short outputs under 40 lines are returned unchanged."""
    short = "Line 1\nLine 2\nLine 3\n"
    res, was_compacted = compact_output(short, max_lines=40)
    assert was_compacted is False
    assert res == short


def test_telemetry_io_error_graceful_handling(tmp_path):
    """Write failure to inaccessible or invalid directory does not raise unhandled exception."""
    # Point to a path that is a file masquerading as parent directory
    bad_parent = tmp_path / "file_blocker"
    bad_parent.write_text("i am a file", encoding="utf-8")
    invalid_log_path = bad_parent / "logs" / "trace.jsonl"

    # Should not raise exception
    record = record_trace("test_tool", 10.0, "success", log_path=invalid_log_path)
    assert record["tool_name"] == "test_tool"
    assert record["status"] == "success"


def test_telemetry_corrupted_jsonl_handling(tmp_path):
    """Malformed/corrupted lines in trace.jsonl are safely skipped by reader."""
    log_file = tmp_path / "trace.jsonl"
    valid_record = json.dumps({"tool_name": "tool_ok", "duration_ms": 15.0, "status": "success"})
    corrupted_content = f"{valid_record}\nTHIS IS NOT JSON {{{{\n{valid_record}\n\n"
    log_file.write_text(corrupted_content, encoding="utf-8")

    traces = get_recent_traces(log_path=log_file)
    assert len(traces) == 2
    assert traces[0]["tool_name"] == "tool_ok"
    assert traces[1]["tool_name"] == "tool_ok"

    diag = get_session_diagnostics(log_path=log_file)
    assert diag["total_tool_calls"] == 2
    assert diag["successful_calls"] == 2


def test_telemetry_output_compaction_no_duplicate_summary():
    """Output compaction does not duplicate trailing failure summary lines."""
    lines = [f"test_{i}.py" for i in range(100)] + [
        "FAILURES",
        "test_sample",
        "Traceback:",
    ] + [f"line {j}" for j in range(50)] + [
        "E AssertionError",
        "=== short test summary info ===",
        "FAILED test_sample",
        "=== 1 failed in 1s ===",
    ]
    res, comp = compact_output("\n".join(lines), 40)
    assert comp is True
    res_lines = res.splitlines()
    failed_count = sum(1 for l in res_lines if "FAILED test_sample" in l)
    summary_count = sum(1 for l in res_lines if "=== 1 failed in 1s ===" in l)
    assert failed_count == 1
    assert summary_count == 1


def test_telemetry_concurrent_logging_and_reading(tmp_path):
    """Concurrent threads writing and reading trace.jsonl complete without error."""
    import concurrent.futures

    log_file = tmp_path / "concurrent_trace.jsonl"
    num_writes = 30

    def writer_task(idx):
        record_trace(f"tool_{idx}", 10.0 + idx, "success", log_path=log_file)

    def reader_task():
        return get_recent_traces(limit=50, log_path=log_file)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        write_futures = [executor.submit(writer_task, i) for i in range(num_writes)]
        read_futures = [executor.submit(reader_task) for _ in range(10)]
        for f in concurrent.futures.as_completed(write_futures + read_futures):
            f.result()

    final_traces = get_recent_traces(limit=100, log_path=log_file)
    assert len(final_traces) == num_writes

