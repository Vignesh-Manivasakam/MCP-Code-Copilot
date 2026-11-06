"""
Empirical Stress Test Suite for Milestone M3 Core Services Remediation.
=======================================================================
Authored by: Challenger M3-Fix-1
Target modules:
- copilot/services/ast_service.py
- copilot/services/telemetry.py

Test areas:
1. Deeply nested TypeScript classes, interfaces with generic inheritance,
   abstract classes, and export variations.
2. Broken Python code (SyntaxError, IndentationError, unclosed quotes, etc.)
   verifying "success": True, "syntax_error": True, and fallback behavior.
3. TokenTracker quota boundary thresholds: 79.9%, 80.0%, 80.1%, 100%, 150%
   with "warning_header": bool, "warning_message", and diagnostics.
4. Token budget compliance (<200 tokens) across large Python and TS files,
   deep hierarchies, and extreme identifier lengths.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

from config import Config
from copilot.services.ast_service import (
    ASTService,
    compact_outline,
    generate_ast_outline,
    parse_js_ts,
    parse_python_ast,
)
from copilot.services.telemetry import (
    TokenTracker,
    get_session_diagnostics,
    get_token_tracker,
    get_token_usage,
    reset_telemetry,
    reset_token_usage,
)


# ===========================================================================
# AREA 1: TypeScript AST Stress Testing
# ===========================================================================

def test_ts_deeply_nested_classes_and_blocks(tmp_path):
    """
    Stress test 1.1: Deeply nested TypeScript classes and block scopes (5 levels).
    Verifies that regex and block end matching correctly calculate line spans
    without crashing or negative line numbers.
    """
    ts_file = tmp_path / "deep_nested.ts"
    content = """
export class Level1 {
    class Level2 {
        class Level3 {
            class Level4 {
                class Level5 {
                    val: number = 5;
                }
            }
        }
    }
}
"""
    ts_file.write_text(content, encoding="utf-8")
    result = generate_ast_outline(ts_file)

    assert result["success"] is True
    assert result["language"] == "typescript"
    assert result["fallback"] is False
    assert result["error"] is None

    outline = result["outline"]
    names = {s["name"]: s for s in outline}

    assert "Level1" in names and names["Level1"]["exported"] is True
    assert "Level2" in names and names["Level2"]["exported"] is False
    assert "Level3" in names and names["Level3"]["exported"] is False
    assert "Level4" in names and names["Level4"]["exported"] is False
    assert "Level5" in names and names["Level5"]["exported"] is False

    for name, sym in names.items():
        assert sym["start_line"] > 0
        assert sym["end_line"] >= sym["start_line"]
        assert sym["lines"] == [sym["start_line"], sym["end_line"]]


def test_ts_interfaces_with_generic_inheritance(tmp_path):
    """
    Stress test 1.2: TypeScript interfaces with multiple generic parameters,
    nested generic constraints, and multiple inheritance (extends A, B).
    """
    ts_file = tmp_path / "generics.ts"
    content = """
export interface SimpleGeneric<T> {
    id: T;
}

export interface ComplexGeneric<T, R extends Map<string, Array<T>> = Map<string, Array<T>>> extends SimpleGeneric<T>, IDisposable {
    execute(context: T): R;
}

interface InternalConfig<K extends string | number, V = any> {
    store: Record<K, V>;
}
"""
    ts_file.write_text(content, encoding="utf-8")
    result = generate_ast_outline(ts_file)

    assert result["success"] is True
    assert result["language"] == "typescript"

    names = {s["name"]: s for s in result["outline"]}
    assert "SimpleGeneric" in names
    assert names["SimpleGeneric"]["exported"] is True
    assert names["SimpleGeneric"]["type_parameters"] == "<T>"

    assert "ComplexGeneric" in names
    assert names["ComplexGeneric"]["exported"] is True
    assert "<T, R extends Map<string, Array<T>> = Map<string, Array<T>>>" in names["ComplexGeneric"]["type_parameters"]

    assert "InternalConfig" in names
    assert names["InternalConfig"]["exported"] is False
    assert "<K extends string | number, V = any>" in names["InternalConfig"]["type_parameters"]


def test_ts_abstract_classes_and_export_variations(tmp_path):
    """
    Stress test 1.3: Abstract classes, default exports, generator functions,
    and various export statement styles.
    """
    ts_file = tmp_path / "exports.ts"
    content = """
export abstract class BaseRepository<T> implements IRepo<T> {
    abstract getById(id: string): Promise<T>;
}

export default abstract class DefaultService<T> {
    abstract run(): void;
}

abstract class InternalAbstractProcessor {
    abstract process(): void;
}

export default class MainApp {
    start() {}
}

export async function* streamTokens<T>(input: T[]): AsyncIterable<T> {
    for (const item of input) yield item;
}

export default async function defaultWorker(): Promise<void> {}

export const arrowProcessor = async <T, R>(item: T): Promise<R> => {
    return {} as R;
};

export let simpleHandler = (req: any, res: any) => {};

// Non-exported functions containing the word 'export'
function exportUserData(uid: string): void {}
function unexportedFunction(): void {}
function calculateMetrics(): void { /* export this later */ }
"""
    ts_file.write_text(content, encoding="utf-8")
    result = generate_ast_outline(ts_file)

    assert result["success"] is True
    assert result["language"] == "typescript"
    names = {s["name"]: s for s in result["outline"]}

    # Abstract classes
    assert "BaseRepository" in names
    assert names["BaseRepository"]["kind"] == "class"
    assert names["BaseRepository"]["exported"] is True
    assert names["BaseRepository"]["type_parameters"] == "<T>"

    assert "DefaultService" in names
    assert names["DefaultService"]["kind"] == "class"
    assert names["DefaultService"]["exported"] is True

    assert "InternalAbstractProcessor" in names
    assert names["InternalAbstractProcessor"]["kind"] == "class"
    assert names["InternalAbstractProcessor"]["exported"] is False

    assert "MainApp" in names
    assert names["MainApp"]["kind"] == "class"
    assert names["MainApp"]["exported"] is True

    # Functions
    assert "streamTokens" in names
    assert names["streamTokens"]["exported"] is True
    assert names["streamTokens"]["kind"] == "function"

    assert "defaultWorker" in names
    assert names["defaultWorker"]["exported"] is True

    assert "arrowProcessor" in names
    assert names["arrowProcessor"]["exported"] is True
    assert "<T, R>" in names["arrowProcessor"]["signature"]

    assert "simpleHandler" in names
    assert names["simpleHandler"]["exported"] is True

    # False positive export resistance
    assert "exportUserData" in names
    assert names["exportUserData"]["exported"] is False
    assert "unexportedFunction" in names
    assert names["unexportedFunction"]["exported"] is False
    assert "calculateMetrics" in names
    assert names["calculateMetrics"]["exported"] is False


# ===========================================================================
# AREA 2: Broken Python Syntax & Envelope Verification
# ===========================================================================

def test_python_syntax_error_unclosed_parenthesis(tmp_path):
    """
    Stress test 2.1: Python SyntaxError (unclosed parenthesis).
    Verifies success=True, syntax_error=True, fallback=True, and symbol recovery.
    """
    py_file = tmp_path / "unclosed_paren.py"
    content = """class ClassA:
    def method_a(self):
        return 1

def broken_func(a, b, c:
    return a + b

class ClassB:
    def method_b(self):
        return 2
"""
    py_file.write_text(content, encoding="utf-8")
    result = generate_ast_outline(py_file)

    assert result["success"] is True
    assert result["syntax_error"] is True
    assert result["fallback"] is True
    assert result["error"] is not None
    assert "SyntaxError" in result["error"]

    extracted = [s["simple_name"] for s in result["outline"]]
    assert "ClassA" in extracted
    assert "method_a" in extracted
    assert "broken_func" in extracted
    assert "ClassB" in extracted
    assert "method_b" in extracted


def test_python_indentation_error_handling(tmp_path):
    """
    Stress test 2.2: Python IndentationError.
    Verifies success=True, syntax_error=True, fallback=True, and symbol recovery.
    """
    py_file = tmp_path / "bad_indent.py"
    content = """class IndentedClass:
    def good_method(self):
        return 'ok'

def bad_indent_func():
pass
  x = 1

def next_good_func():
    return 42
"""
    py_file.write_text(content, encoding="utf-8")
    result = generate_ast_outline(py_file)

    assert result["success"] is True
    assert result["syntax_error"] is True
    assert result["fallback"] is True
    assert result["error"] is not None

    extracted = [s["simple_name"] for s in result["outline"]]
    assert "IndentedClass" in extracted
    assert "good_method" in extracted
    assert "bad_indent_func" in extracted
    assert "next_good_func" in extracted


def test_python_unclosed_triple_quotes(tmp_path):
    """
    Stress test 2.3: Python unclosed multi-line string / docstring.
    Verifies success=True, syntax_error=True, fallback=True.
    """
    py_file = tmp_path / "unclosed_quotes.py"
    content = """class HeaderClass:
    '''Unclosed docstring starts here and never terminates...

def orphaned_func():
    return True
"""
    py_file.write_text(content, encoding="utf-8")
    result = generate_ast_outline(py_file)

    assert result["success"] is True
    assert result["syntax_error"] is True
    assert result["fallback"] is True
    assert result["error"] is not None

    extracted = [s["simple_name"] for s in result["outline"]]
    assert "HeaderClass" in extracted
    assert "orphaned_func" in extracted


def test_python_clean_valid_file_envelope(tmp_path):
    """
    Stress test 2.4: Clean valid Python file.
    Verifies success=True, syntax_error=False, fallback=False, error=None.
    """
    py_file = tmp_path / "valid.py"
    content = """class ValidClass:
    def valid_method(self) -> int:
        return 100
"""
    py_file.write_text(content, encoding="utf-8")
    result = generate_ast_outline(py_file)

    assert result["success"] is True
    assert result["syntax_error"] is False
    assert result["fallback"] is False
    assert result["error"] is None
    assert result["total_symbols"] == 2


def test_ast_outline_missing_and_sensitive_file_envelope(tmp_path):
    """
    Stress test 2.5: Missing file and sensitive file envelope contracts.
    Verifies success=False, syntax_error=False, fallback=True.
    """
    missing_file = tmp_path / "does_not_exist.py"
    res_missing = generate_ast_outline(missing_file)
    assert res_missing["success"] is False
    assert res_missing["syntax_error"] is False
    assert res_missing["fallback"] is True
    assert "ERR_NOT_FOUND" in res_missing["error"]

    sensitive_file = tmp_path / ".env"
    sensitive_file.write_text("SECRET=123", encoding="utf-8")
    res_sensitive = generate_ast_outline(sensitive_file)
    assert res_sensitive["success"] is False
    assert res_sensitive["syntax_error"] is False
    assert res_sensitive["fallback"] is True
    assert "Sensitive file" in res_sensitive["error"]


# ===========================================================================
# AREA 3: TokenTracker Quota Thresholds Stress Testing
# ===========================================================================

def test_tokentracker_exact_threshold_boundaries():
    """
    Stress test 3.1: TokenTracker exact threshold boundaries:
    - 79.9% (799,000 / 1,000,000): warning=False, warning_header=False, warning_message=None
    - 80.0% (800,000 / 1,000,000): warning=True, warning_header=True, warning_message present
    - 80.1% (801,000 / 1,000,000): warning=True, warning_header=True, warning_message present
    - 100.0% (1,000,000 / 1,000,000): warning=True, warning_header=True, remaining=0
    - 150.0% (1,500,000 / 1,000,000): warning=True, warning_header=True, remaining=0, percent_used=150.0
    """
    tracker = TokenTracker()
    assert tracker.budget_limit == 1_000_000

    # 1. Below threshold: 79.9%
    tracker.reset()
    tracker.record_usage(tokens=799_000)
    usage_799 = tracker.get_usage()
    assert usage_799["total_estimated_tokens"] == 799_000
    assert usage_799["percent_used"] == 79.9
    assert usage_799["remaining"] == 201_000
    assert usage_799["warning"] is False
    assert usage_799["warning_header"] is False
    assert usage_799["warning_message"] is None
    assert tracker.is_warning_active() is False
    assert tracker.get_warning_header() is None

    # 2. Exactly at threshold: 80.0%
    tracker.reset()
    tracker.record_usage(tokens=800_000)
    usage_800 = tracker.get_usage()
    assert usage_800["total_estimated_tokens"] == 800_000
    assert usage_800["percent_used"] == 80.0
    assert usage_800["remaining"] == 200_000
    assert usage_800["warning"] is True
    assert usage_800["warning_header"] is True
    assert isinstance(usage_800["warning_message"], str)
    assert "80%" in usage_800["warning_message"]
    assert tracker.is_warning_active() is True
    assert tracker.get_warning_header() == usage_800["warning_message"]

    # 3. Slightly above threshold: 80.1%
    tracker.reset()
    tracker.record_usage(tokens=801_000)
    usage_801 = tracker.get_usage()
    assert usage_801["total_estimated_tokens"] == 801_000
    assert usage_801["percent_used"] == 80.1
    assert usage_801["remaining"] == 199_000
    assert usage_801["warning"] is True
    assert usage_801["warning_header"] is True
    assert isinstance(usage_801["warning_message"], str)
    assert tracker.is_warning_active() is True

    # 4. At 100% capacity
    tracker.reset()
    tracker.record_usage(tokens=1_000_000)
    usage_100 = tracker.get_usage()
    assert usage_100["total_estimated_tokens"] == 1_000_000
    assert usage_100["percent_used"] == 100.0
    assert usage_100["remaining"] == 0
    assert usage_100["warning"] is True
    assert usage_100["warning_header"] is True
    assert tracker.is_warning_active() is True

    # 5. Over capacity: 150%
    tracker.reset()
    tracker.record_usage(tokens=1_500_000)
    usage_150 = tracker.get_usage()
    assert usage_150["total_estimated_tokens"] == 1_500_000
    assert usage_150["percent_used"] == 150.0
    assert usage_150["remaining"] == 0
    assert usage_150["warning"] is True
    assert usage_150["warning_header"] is True
    assert tracker.is_warning_active() is True


def test_session_diagnostics_token_warning_header_integration(tmp_path):
    """
    Stress test 3.2: Verify get_session_diagnostics() contract across threshold transitions.
    """
    reset_telemetry()
    tracker = get_token_tracker()

    # Step A: Under threshold
    tracker.reset()
    tracker.record_usage(tokens=500_000)
    diag_low = get_session_diagnostics()
    assert diag_low["warning_active"] is False
    assert diag_low["warning_header"] is False
    assert diag_low["token_usage"]["warning_header"] is False
    assert diag_low["token_usage"]["warning_message"] is None

    # Step B: Over threshold
    tracker.record_usage(tokens=350_000)  # total 850,000 -> 85%
    diag_high = get_session_diagnostics()
    assert diag_high["warning_active"] is True
    assert diag_high["warning_header"] is True
    assert diag_high["token_usage"]["warning_header"] is True
    assert isinstance(diag_high["token_usage"]["warning_message"], str)
    assert "80%" in diag_high["token_usage"]["warning_message"]

    reset_telemetry()


def test_tokentracker_concurrent_stress():
    """
    Stress test 3.3: High-concurrency race condition test for TokenTracker.
    Spawns 20 threads simultaneously recording token batches.
    Verifies lock safety, exact token accumulation, and threshold activation.
    """
    tracker = TokenTracker()
    num_threads = 20
    increments_per_thread = 50
    tokens_per_call = 1_000  # Total per thread: 50,000; Total overall: 1,000,000

    def worker():
        for _ in range(increments_per_thread):
            tracker.record_usage(tokens=tokens_per_call)

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker) for _ in range(num_threads)]
        for f in futures:
            f.result()

    usage = tracker.get_usage()
    expected_total = num_threads * increments_per_thread * tokens_per_call
    assert usage["total_estimated_tokens"] == expected_total == 1_000_000
    assert usage["percent_used"] == 100.0
    assert usage["warning"] is True
    assert usage["warning_header"] is True
    assert usage["remaining"] == 0


# ===========================================================================
# AREA 4: Token Budget Compliance (<200 Tokens)
# ===========================================================================

def test_ast_token_budget_massive_python_file(tmp_path):
    """
    Stress test 4.1: Massive Python file with 250 classes, 500 methods,
    deep inheritance, and extensive docstrings (>5,000 lines).
    Asserts outline token count strictly < 200 tokens.
    """
    py_file = tmp_path / "giant_python.py"
    parts = ["# Giant Python test file\n"]
    for i in range(250):
        parts.append(
            f"class GiantServiceHandler_{i}(BaseClass):\n"
            f"    '''Comprehensive documentation string for GiantServiceHandler_{i}.'''\n"
            f"    def execute_primary_{i}(self, payload: dict) -> dict:\n"
            f"        '''Primary execution subroutine.'''\n"
            f"        return payload\n"
            f"    def execute_secondary_{i}(self, code: int = {i}) -> bool:\n"
            f"        '''Secondary execution subroutine.'''\n"
            f"        return code > 0\n"
        )
        parts.append("\n" * 15)

    content = "".join(parts)
    py_file.write_text(content, encoding="utf-8")
    lines = content.splitlines()
    assert len(lines) > 5000

    result = generate_ast_outline(py_file, max_tokens=200)

    assert result["success"] is True
    assert result["total_lines"] > 5000
    assert result["total_symbols"] >= 750
    assert result["truncated"] is True

    # Token budget verification
    assert result["token_count"] <= 200
    estimated = len(result["outline_text"]) / Config.CHARS_PER_TOKEN
    assert estimated <= 200, f"Token estimate {estimated} exceeded 200"
    assert "... (+" in result["outline_text"]


def test_ast_token_budget_massive_typescript_file(tmp_path):
    """
    Stress test 4.2: Massive TypeScript file with 200 exported interfaces,
    classes, functions, and arrow functions (>4,000 lines).
    Asserts outline token count strictly < 200 tokens.
    """
    ts_file = tmp_path / "giant_ts.ts"
    parts = ["// Giant TypeScript test file\n"]
    for i in range(120):
        parts.append(
            f"export interface IInterface_{i}<T> {{\n"
            f"    id: string;\n"
            f"    data: T;\n"
            f"}}\n\n"
            f"export class ServiceClass_{i}<T> implements IInterface_{i}<T> {{\n"
            f"    id: string = '{i}';\n"
            f"    data!: T;\n"
            f"}}\n\n"
            f"export async function executeOperation_{i}<T>(input: T): Promise<T> {{\n"
            f"    return input;\n"
            f"}}\n\n"
            f"export const arrowHandler_{i} = (ctx: any): void => {{}};\n\n"
        )
        parts.append("\n" * 20)

    content = "".join(parts)
    ts_file.write_text(content, encoding="utf-8")
    assert len(content.splitlines()) > 4000

    result = generate_ast_outline(ts_file, max_tokens=200)

    assert result["success"] is True
    assert result["total_lines"] > 4000
    assert result["total_symbols"] >= 400
    assert result["truncated"] is True
    assert result["token_count"] <= 200
    estimated = len(result["outline_text"]) / Config.CHARS_PER_TOKEN
    assert estimated <= 200


def test_ast_token_budget_extreme_symbol_identifier_lengths(tmp_path):
    """
    Stress test 4.3: Edge case where identifiers have abnormally long names (e.g. 500 chars).
    Verifies that compact_outline handles long identifiers without exceeding max_tokens.
    """
    py_file = tmp_path / "long_names.py"
    parts = []
    for i in range(15):
        long_name = f"ExtremelyLongClassIdentifier_{'A' * 400}_{i}"
        parts.append(f"class {long_name}:\n    def {'func_' + 'B' * 400}_{i}(self):\n        pass\n")

    py_file.write_text("".join(parts), encoding="utf-8")
    result = generate_ast_outline(py_file, max_tokens=200)

    assert result["success"] is True
    assert result["truncated"] is True
    assert result["token_count"] <= 200
    estimated = len(result["outline_text"]) / Config.CHARS_PER_TOKEN
    assert estimated <= 200


def test_ast_token_budget_custom_budgets(tmp_path):
    """
    Stress test 4.4: Custom token limits: max_tokens=50, max_tokens=100, max_tokens=500.
    Verifies strict compliance across different budget thresholds.
    """
    py_file = tmp_path / "multi_budget.py"
    parts = []
    for i in range(60):
        parts.append(f"class SubSystem_{i}:\n    def action_{i}(self):\n        '''Docstring.'''\n        pass\n")
    py_file.write_text("".join(parts), encoding="utf-8")

    for budget in [50, 100, 300, 500]:
        res = generate_ast_outline(py_file, max_tokens=budget)
        assert res["success"] is True
        assert res["token_count"] <= budget, f"Exceeded custom budget {budget}: got {res['token_count']}"
        estimated = len(res["outline_text"]) / Config.CHARS_PER_TOKEN
        assert estimated <= budget



# ===========================================================================
# ADDITIONAL EMPIRICAL STRESS TESTS: Edge Cases & Boundaries
# ===========================================================================

def test_python_ast_complex_nesting_and_unicode(tmp_path):
    """
    Stress test 5.1: Python hierarchical nesting across scopes:
    outer_func -> InnerClass -> inner_method -> local_closure.
    Also tests unicode symbols and decorator annotations.
    """
    py_file = tmp_path / "deep_python.py"
    content = '''@decorator_outer(param="val")
def outer_service(x: int) -> str:
    """Outer function docstring."""
    class LocalContainer:
        """Local container class."""
        @property
        def item(self) -> int:
            def inner_closure():
                pass
            return 42
    return str(x)

def unicode_fn_λ_test():
    pass
'''
    py_file.write_text(content, encoding="utf-8")
    result = generate_ast_outline(py_file)

    assert result["success"] is True
    assert result["syntax_error"] is False
    assert result["fallback"] is False

    names = {s["name"]: s for s in result["outline"]}
    assert "outer_service" in names
    assert names["outer_service"]["kind"] == "function"
    assert "@decorator_outer" in names["outer_service"]["decorators"][0]

    assert "outer_service.LocalContainer" in names
    assert names["outer_service.LocalContainer"]["kind"] == "class"

    assert "outer_service.LocalContainer.item" in names
    assert names["outer_service.LocalContainer.item"]["kind"] == "method"

    assert "outer_service.LocalContainer.item.inner_closure" in names
    assert names["outer_service.LocalContainer.item.inner_closure"]["kind"] == "function"

    assert "unicode_fn_λ_test" in names


def test_python_ast_weird_syntax_fallback(tmp_path):
    """
    Stress test 5.2: Python syntax errors from invalid identifiers and invalid await.
    """
    py_file = tmp_path / "weird_syntax.py"
    content = """def 999_invalid_start():
    pass

class NormalClass:
    def good_method(self):
        pass
"""
    py_file.write_text(content, encoding="utf-8")
    result = generate_ast_outline(py_file)

    assert result["success"] is True
    assert result["syntax_error"] is True
    assert result["fallback"] is True
    assert "SyntaxError" in result["error"]
    names = [s["simple_name"] for s in result["outline"]]
    assert "NormalClass" in names


def test_telemetry_estimate_tokens_edge_cases():
    """
    Stress test 5.3: estimate_tokens on non-string, bytes, unicode, and empty payloads.
    """
    from copilot.services.telemetry import estimate_tokens
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens(b"hello world") == 2  # 11 bytes // 4.0 = 2
    assert estimate_tokens("hello world" * 10) == 27  # 110 // 4 = 27
    assert estimate_tokens({"key": "value"}) > 0
    assert estimate_tokens(123456789) > 0


def test_telemetry_output_compaction_boundary_40_vs_41_lines():
    """
    Stress test 5.4: Output compaction line threshold boundaries:
    - 40 lines: uncompacted (returns identical text, was_compacted=False)
    - 41 lines: compacted (<= 40 lines, was_compacted=True)
    """
    from copilot.services.telemetry import compact_output

    text_40 = "\n".join([f"line_{i}" for i in range(40)])
    compacted_40, was_40 = compact_output(text_40, max_lines=40)
    assert was_40 is False
    assert compacted_40 == text_40
    assert len(compacted_40.splitlines()) == 40

    text_41 = "\n".join([f"line_{i}" for i in range(41)])
    compacted_41, was_41 = compact_output(text_41, max_lines=40)
    assert was_41 is True
    assert len(compacted_41.splitlines()) <= 40
    assert "[Output compacted:" in compacted_41


def test_telemetry_record_trace_resilience_and_flush(tmp_path):
    """
    Stress test 5.5: record_trace immediately flushes data and handles unexpected inputs.
    """
    from copilot.services.telemetry import record_trace, get_recent_traces
    trace_file = tmp_path / "custom_trace.jsonl"

    rec1 = record_trace(
        tool_name="test_tool",
        duration_ms=-5.5,  # Edge case: negative duration
        status="success",
        error_code=None,
        estimated_tokens=50,
        metadata={"nested": {"data": 123}},
        log_path=trace_file,
    )
    assert rec1["duration_ms"] == -5.5
    assert trace_file.exists()

    # Verify disk persistence via immediate read
    content = trace_file.read_text(encoding="utf-8")
    assert "test_tool" in content
    assert '"duration_ms": -5.5' in content

    recent = get_recent_traces(limit=5, log_path=trace_file)
    assert len(recent) >= 1
    assert recent[-1]["tool_name"] == "test_tool"
