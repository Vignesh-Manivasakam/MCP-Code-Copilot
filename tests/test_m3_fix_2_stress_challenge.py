"""
Empirical Challenge & Stress Test Suite: Milestone M3-Fix-2
============================================================
Authored by: Challenger M3-Fix-2
Target modules:
- copilot/services/search_service.py
- copilot/services/telemetry.py

Adversarial stress-testing against:
1. SearchService concurrency, SQLite WAL mode, and Windows NTFS file locking.
2. Rapid consecutive initialize_index(force=True) calls with preexisting physical views and tables.
3. Output compaction with 10,000+ line diffs, massive pytest logs, exact <=40 line budgets, and zero duplicate summaries.
4. Telemetry logging, deque iteration, and unlinking across 20 threads without RuntimeError or PermissionError.
5. Adversarial input validation and edge cases.
"""
from __future__ import annotations

import concurrent.futures
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List
import pytest

from config import Config
from copilot.services.search_service import SearchService, ERR_INVALID_REGEX, ERR_PATH_TRAVERSAL
from copilot.services.telemetry import (
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


# ==============================================================================
# Area 1: SearchService Concurrency, SQLite WAL & Windows File Locking
# ==============================================================================

class TestSearchServiceConcurrencyAndLocking:
    """Stress-test SearchService under high multi-threaded read/write load on Windows."""

    def test_search_concurrent_writes_and_queries(self, tmp_path: Path):
        """Concurrently write files, index them, and query FTS5 across 12 threads."""
        proj_dir = tmp_path / "project"
        proj_dir.mkdir(parents=True, exist_ok=True)
        src_dir = proj_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)

        service = SearchService(project_root=proj_dir)
        service.initialize_index(force=True)

        num_files = 30
        for i in range(num_files):
            fpath = src_dir / f"module_{i}.py"
            fpath.write_text(
                f"def calculate_metric_{i}():\n    return {i} * 42\n# Keyword token_{i}\n",
                encoding="utf-8",
            )

        errors: List[Exception] = []

        def worker_index(file_idx: int):
            try:
                fpath = src_dir / f"module_{file_idx}.py"
                success = service.index_file(fpath)
                assert success is True
            except Exception as exc:
                errors.append(exc)

        def worker_search(query_str: str):
            try:
                res = service.fts_search(query_str, max_results=10)
                assert isinstance(res, dict)
                assert "results" in res
            except Exception as exc:
                errors.append(exc)

        def worker_grep(query_str: str):
            try:
                res = service.grep_search(query_str, max_results=10)
                assert isinstance(res, dict)
                assert "results" in res
            except Exception as exc:
                errors.append(exc)

        # Launch 90 concurrent operations across 12 threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            futures = []
            for i in range(num_files):
                futures.append(executor.submit(worker_index, i))
                futures.append(executor.submit(worker_search, f"calculate_metric_{i}"))
                futures.append(executor.submit(worker_grep, rf"def calculate_metric_{i}"))

            for f in concurrent.futures.as_completed(futures):
                f.result()

        assert len(errors) == 0, f"Encountered concurrency exceptions: {errors}"

        # Verify that all connections are closed and db file can be deleted without [WinError 32]
        db_file = service.index_path
        assert db_file.exists()
        try:
            db_file.unlink()
        except PermissionError as pe:
            pytest.fail(f"Database file is still locked by unclosed connection: {pe}")

    def test_search_concurrent_reindex_and_active_queries(self, tmp_path: Path):
        """Concurrently call index_all(force=True) while multiple threads read FTS5."""
        proj_dir = tmp_path / "proj_heavy"
        proj_dir.mkdir(parents=True, exist_ok=True)
        src = proj_dir / "src"
        src.mkdir(parents=True, exist_ok=True)

        for i in range(25):
            (src / f"service_{i}.py").write_text(
                f"class ServiceRunner{i}:\n    def execute(self):\n        return 'payload_{i}'\n",
                encoding="utf-8",
            )

        service = SearchService(project_root=proj_dir)
        service.index_all(force=True)

        stop_event = threading.Event()
        query_errors: List[Exception] = []
        queries_completed = [0]

        def continuous_reader():
            while not stop_event.is_set():
                try:
                    res = service.fts_search("ServiceRunner", max_results=10)
                    assert isinstance(res, dict)
                    queries_completed[0] += 1
                    time.sleep(0.005)
                except Exception as exc:
                    query_errors.append(exc)

        threads = [threading.Thread(target=continuous_reader) for _ in range(6)]
        for t in threads:
            t.daemon = True
            t.start()

        reindex_errors: List[Exception] = []
        try:
            # Reindex repeatedly 5 times while readers are querying
            for _ in range(5):
                service.index_all(force=True)
                time.sleep(0.02)
        except Exception as exc:
            reindex_errors.append(exc)
        finally:
            stop_event.set()
            for t in threads:
                t.join(timeout=3.0)

        assert len(reindex_errors) == 0, f"Re-indexing failed: {reindex_errors}"
        assert len(query_errors) == 0, f"Query threads encountered errors: {query_errors}"
        assert queries_completed[0] > 0


# ==============================================================================
# Area 2: Rapid Consecutive initialize_index(force=True) & Schema States
# ==============================================================================

class TestInitializeIndexForceAndSchemaCollisions:
    """Stress-test initialize_index(force=True) against preexisting physical views/tables."""

    def test_rapid_consecutive_initialize_index_force(self, tmp_path: Path):
        """50 rapid sequential calls to initialize_index(force=True) must not raise OperationalError."""
        service = SearchService(project_root=tmp_path)
        for i in range(50):
            service.initialize_index(force=True)

        # Confirm functional index after 50 force drops
        f = tmp_path / "hello.py"
        f.write_text("def ping(): return 'pong'", encoding="utf-8")
        assert service.index_file(f) is True
        res = service.fts_search("ping")
        assert res["total_matches"] == 1

    def test_concurrent_initialize_index_force_threads(self, tmp_path: Path):
        """10 threads calling initialize_index(force=True) simultaneously."""
        service = SearchService(project_root=tmp_path)
        service.initialize_index(force=True)

        errors: List[Exception] = []

        def reindex_worker():
            try:
                for _ in range(5):
                    service.initialize_index(force=True)
            except Exception as exc:
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(reindex_worker) for _ in range(10)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        assert len(errors) == 0, f"Concurrent initialize_index errors: {errors}"

    def test_preexisting_physical_table_as_code_search(self, tmp_path: Path):
        """Preexisting physical table named 'code_search' must be dropped without error."""
        db_file = tmp_path / ".copilot" / "index.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)

        # Manually create physical table named code_search
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE code_search (id INTEGER PRIMARY KEY, dummy TEXT);")
        conn.execute("CREATE TABLE code_files (id INTEGER PRIMARY KEY, path TEXT);")
        conn.commit()
        conn.close()

        # SearchService default uses table_name='code_fts' and view='code_search'
        service = SearchService(index_path=db_file, project_root=tmp_path)
        # force=True must handle dropping code_search even though it is a TABLE, not a VIEW
        service.initialize_index(force=True)

        # Verify index works
        src = tmp_path / "test.py"
        src.write_text("def sample_function(): pass", encoding="utf-8")
        assert service.index_file(src) is True
        res = service.fts_search("sample_function")
        assert res["total_matches"] == 1

    def test_preexisting_view_when_table_name_is_code_search(self, tmp_path: Path):
        """Preexisting VIEW named 'code_search' when table_name is set to 'code_search'."""
        db_file = tmp_path / ".copilot" / "index.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)

        # Pre-create code_fts table and code_search VIEW
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE code_fts (file_path TEXT, content TEXT);")
        conn.execute("CREATE VIEW code_search AS SELECT file_path, content FROM code_fts;")
        conn.commit()
        conn.close()

        # Now configure SearchService where table_name='code_search'
        service = SearchService(index_path=db_file, project_root=tmp_path, table_name="code_search")
        service.initialize_index(force=True)

        # Confirm code_search is now a virtual table and searchable
        src = tmp_path / "test2.py"
        src.write_text("def unique_symbol_xyz(): pass", encoding="utf-8")
        assert service.index_file(src) is True
        res = service.fts_search("unique_symbol_xyz")
        assert res["total_matches"] == 1


# ==============================================================================
# Area 3: Output Compaction Stress Testing with 10,000+ Lines
# ==============================================================================

class TestOutputCompactionStress:
    """Stress-test compact_output with massive logs, diffs, and malformed inputs."""

    def test_massive_12000_line_git_diff(self):
        """12,000 line git diff must be compacted to <= 40 lines with summary notice."""
        diff_lines = [
            "diff --git a/large_file.py b/large_file.py",
            "index 0123456..abcdef7 100644",
            "--- a/large_file.py",
            "+++ b/large_file.py",
            "@@ -1,500 +1,12000 @@",
        ]
        diff_lines.extend([f"+added_line_content_{i} = {i}" for i in range(12000)])

        raw_diff = "\n".join(diff_lines)
        compacted, was_compacted = compact_output(raw_diff, max_lines=40)

        assert was_compacted is True
        res_lines = compacted.splitlines()
        assert len(res_lines) <= 40
        assert "[Output compacted: git diff exceeds 40 lines" in res_lines[0]
        assert "diff --git a/large_file.py b/large_file.py" in res_lines[1]

    def test_massive_15000_line_pytest_log_zero_duplicate_summary(self):
        """15,000 line pytest log with 50 failures must yield <= 40 lines and zero duplicate summary."""
        lines = []
        # 1. Test runner progress (5,000 lines)
        for i in range(5000):
            lines.append(f"tests/test_suite_{i % 10}.py .............F............. [ {i // 50}%]")

        # 2. Failures section (50 failures * 255 lines = 12,750 lines)
        lines.append("=================================== FAILURES ===================================")
        for f in range(50):
            lines.append(f"_________________________________ test_case_{f} __________________________________")
            lines.append("Traceback (most recent call last):")
            lines.append(f'  File "tests/test_mod.py", line {100 + f}, in test_case_{f}')
            lines.append(f"    assert result_{f} == expected_{f}")
            lines.append(f"E   AssertionError: assert False == True (failure {f})")
            for j in range(250):
                lines.append(f"    debug_context_dump_{j} = 'val_{j}'")

        # 3. Short test summary info section
        lines.append("=========================== short test summary info ============================")
        for f in range(50):
            lines.append(f"FAILED tests/test_mod.py::test_case_{f} - AssertionError: assert False == True (failure {f})")
        lines.append("======================== 50 failed, 1950 passed in 25.43s ========================")

        raw_log = "\n".join(lines)
        assert len(lines) > 15000

        compacted, was_compacted = compact_output(raw_log, max_lines=40)

        assert was_compacted is True
        res_lines = compacted.splitlines()
        assert len(res_lines) <= 40

        # Header check
        assert "[Output compacted: showing failing test traces and summary" in res_lines[0]

        # Verify summary lines exist and are NOT duplicated
        final_summary_line = "======================== 50 failed, 1950 passed in 25.43s ========================"
        assert final_summary_line in res_lines
        assert res_lines.count(final_summary_line) == 1

        # Verify short summary info header does not appear in traces
        assert "=== short test summary info ===" not in compacted

        # Verify no duplicate FAILED lines in output
        failed_lines = [l for l in res_lines if l.startswith("FAILED ")]
        for fl in failed_lines:
            assert res_lines.count(fl) == 1, f"Duplicate summary line found: {fl}"

    def test_general_output_50000_lines_strict_budget(self):
        """50,000 lines of general output must be compacted to exactly <= 40 lines."""
        raw_text = "\n".join(f"Log line {i}: processing item {i}" for i in range(50000))
        compacted, was_compacted = compact_output(raw_text, max_lines=40)

        assert was_compacted is True
        res_lines = compacted.splitlines()
        assert len(res_lines) <= 40
        # Check structure: 3 head lines + 1 notice + 36 tail lines = 40 lines
        assert res_lines[0] == "Log line 0: processing item 0"
        assert res_lines[1] == "Log line 1: processing item 1"
        assert res_lines[2] == "Log line 2: processing item 2"
        assert "[Output compacted: showing first 3 and last 36 of 50000 lines]" in res_lines[3]
        assert res_lines[-1] == "Log line 49999: processing item 49999"

    @pytest.mark.parametrize("budget", [5, 10, 20, 40, 100])
    def test_compact_output_variable_budgets(self, budget: int):
        """Verify strict adherence to arbitrary max_lines budgets."""
        raw_text = "\n".join(f"item_{i}" for i in range(500))
        compacted, was_compacted = compact_output(raw_text, max_lines=budget)
        assert was_compacted is True
        assert len(compacted.splitlines()) <= budget

    def test_compact_output_boundary_conditions(self):
        """Boundary checks: empty, exactly 40, exactly 41, single huge line."""
        # Empty
        c, w = compact_output("", 40)
        assert c == "" and w is False

        # Exactly 40 lines
        text_40 = "\n".join(f"line_{i}" for i in range(40))
        c, w = compact_output(text_40, 40)
        assert c == text_40 and w is False

        # Exactly 41 lines
        text_41 = "\n".join(f"line_{i}" for i in range(41))
        c, w = compact_output(text_41, 40)
        assert w is True
        assert len(c.splitlines()) <= 40

        # Single massive line (100,000 chars without newline)
        huge_line = "A" * 100000
        c, w = compact_output(huge_line, 40)
        assert w is False
        assert c == huge_line


# ==============================================================================
# Area 4: Telemetry Concurrency across 20 Threads
# ==============================================================================

class TestTelemetryConcurrencyStress:
    """Stress-test telemetry logging, iteration, and reset across 20 threads."""

    def test_telemetry_20_threads_concurrency_no_mutated_deque_or_permission_error(self, tmp_path: Path):
        """Concurrently write traces, read traces, fetch diagnostics, and reset telemetry across 20 threads."""
        log_file = tmp_path / "stress_trace.jsonl"
        reset_telemetry(log_path=log_file)

        stop_event = threading.Event()
        errors: List[Exception] = []
        op_counts = {"writes": 0, "reads": 0, "diagnostics": 0, "resets": 0}
        counts_lock = threading.Lock()

        def writer_thread(t_id: int):
            seq = 0
            while not stop_event.is_set():
                try:
                    record_trace(
                        tool_name=f"tool_worker_{t_id}",
                        duration_ms=1.5 + (seq % 100),
                        status="success" if seq % 5 != 0 else "error",
                        error_code=None if seq % 5 != 0 else "ERR_COMMAND_FAILED",
                        estimated_tokens=50,
                        metadata={"worker": t_id, "seq": seq},
                        log_path=log_file,
                    )
                    with counts_lock:
                        op_counts["writes"] += 1
                    seq += 1
                except Exception as exc:
                    errors.append(exc)
                time.sleep(0.002)

        def reader_thread():
            while not stop_event.is_set():
                try:
                    traces = get_recent_traces(limit=25, log_path=log_file)
                    assert isinstance(traces, list)
                    with counts_lock:
                        op_counts["reads"] += 1
                except Exception as exc:
                    errors.append(exc)
                time.sleep(0.003)

        def diag_thread():
            while not stop_event.is_set():
                try:
                    diag = get_session_diagnostics(traces_limit=25, log_path=log_file)
                    assert isinstance(diag, dict)
                    assert "total_tool_calls" in diag
                    assert "token_usage" in diag
                    with counts_lock:
                        op_counts["diagnostics"] += 1
                except Exception as exc:
                    errors.append(exc)
                time.sleep(0.005)

        def reset_thread():
            while not stop_event.is_set():
                try:
                    time.sleep(0.05)
                    reset_telemetry(log_path=log_file)
                    with counts_lock:
                        op_counts["resets"] += 1
                except Exception as exc:
                    errors.append(exc)

        # 20 threads: 8 writers, 6 readers, 4 diagnostics, 2 resets
        threads: List[threading.Thread] = []
        for i in range(8):
            threads.append(threading.Thread(target=writer_thread, args=(i,)))
        for _ in range(6):
            threads.append(threading.Thread(target=reader_thread))
        for _ in range(4):
            threads.append(threading.Thread(target=diag_thread))
        for _ in range(2):
            threads.append(threading.Thread(target=reset_thread))

        for t in threads:
            t.daemon = True
            t.start()

        # Let the stress test run under intense load for 2.5 seconds
        time.sleep(2.5)
        stop_event.set()

        for t in threads:
            t.join(timeout=3.0)

        # Validate that NO errors occurred
        runtime_errors = [e for e in errors if isinstance(e, RuntimeError)]
        perm_errors = [e for e in errors if isinstance(e, PermissionError)]

        assert len(runtime_errors) == 0, f"Encountered deque mutation RuntimeErrors: {runtime_errors}"
        assert len(perm_errors) == 0, f"Encountered Windows PermissionErrors: {perm_errors}"
        assert len(errors) == 0, f"Encountered other exceptions during concurrent telemetry: {errors}"

        # Confirm operations actually executed
        assert op_counts["writes"] > 100, f"Too few writes: {op_counts['writes']}"
        assert op_counts["reads"] > 50, f"Too few reads: {op_counts['reads']}"
        assert op_counts["diagnostics"] > 20, f"Too few diagnostics: {op_counts['diagnostics']}"
        assert op_counts["resets"] > 2, f"Too few resets: {op_counts['resets']}"

    def test_token_tracker_concurrency_and_warning_threshold(self):
        """TokenTracker accurately aggregates concurrent token recordings without race conditions."""
        tracker = get_token_tracker()
        tracker.reset()

        num_threads = 10
        iters_per_thread = 10
        tokens_per_iter = 10_000

        def token_worker():
            for _ in range(iters_per_thread):
                tracker.record_usage(tokens=tokens_per_iter)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(token_worker) for _ in range(num_threads)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        usage = tracker.get_usage()
        expected_total = num_threads * iters_per_thread * tokens_per_iter
        assert usage["total_estimated_tokens"] == expected_total
        assert usage["warning"] is True
        assert usage["warning_header"] is True
        assert "[WARNING:" in usage["warning_message"]

        tracker.reset()
        reset_usage = tracker.get_usage()
        assert reset_usage["total_estimated_tokens"] == 0
        assert reset_usage["warning"] is False


# ==============================================================================
# Area 5: Adversarial Input Mining & Edge Cases
# ==============================================================================

class TestAdversarialInputsAndEdgeCases:
    """Stress-test SearchService and Telemetry with malformed, hostile inputs."""

    def test_fts5_malformed_syntax_and_special_chars(self, tmp_path: Path):
        """FTS5 search with unbalanced quotes, boolean operators, SQL injection, emojis."""
        service = SearchService(project_root=tmp_path)
        service.initialize_index(force=True)

        f = tmp_path / "code.py"
        f.write_text("def authenticate_user(): return True\n# rocket 🚀", encoding="utf-8")
        service.index_file(f)

        hostile_queries = [
            '"""',
            "' OR '1'='1",
            "DROP TABLE code_search;--",
            "NEAR(auth, user, 10)",
            "AND OR NOT",
            "***???",
            "🚀",
            "   ",
            "\t\n",
            "a" * 1000,
        ]

        for q in hostile_queries:
            res = service.fts_search(q)
            assert isinstance(res, dict)
            assert "results" in res
            assert "total_matches" in res

    def test_grep_search_adversarial_queries(self, tmp_path: Path):
        """Grep search handling invalid regex, path traversal, and null characters."""
        service = SearchService(project_root=tmp_path)
        f = tmp_path / "valid.py"
        f.write_text("class TestClass: pass", encoding="utf-8")

        # 1. Invalid regex syntax
        for bad_regex in ["[a-z", "(?P<invalid", "*star", "+plus"]:
            res = service.grep_search(bad_regex)
            assert res.get("error_code") == ERR_INVALID_REGEX
            assert res.get("total_matches") == 0

        # 2. Path traversal attempt in path parameter
        res_traversal = service.grep_search("TestClass", path="../../secret")
        assert res_traversal.get("error_code") == ERR_PATH_TRAVERSAL

    def test_telemetry_resilience_to_non_serializable_metadata(self, tmp_path: Path):
        """record_trace survives non-JSON serializable metadata without crashing."""
        log_file = tmp_path / "safe_trace.jsonl"
        custom_obj = object()
        record = record_trace(
            tool_name="test_tool",
            duration_ms=12.34,
            status="success",
            metadata={
                "path_obj": tmp_path,
                "set_data": {1, 2, 3},
                "custom_class": custom_obj,
            },
            log_path=log_file,
        )
        assert record["status"] == "success"
        traces = get_recent_traces(log_path=log_file)
        assert len(traces) == 1
        assert "path_obj" in traces[0]["metadata"]
