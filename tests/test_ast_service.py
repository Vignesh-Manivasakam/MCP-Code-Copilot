"""
Authoritative Test Suite 3: AST Outlines & Code Inspection.
===========================================================
Tests AST Service against Python, JS/TS, Markdown, large-file compaction,
and syntax error fallbacks as specified in Part 4 Suite 3.
"""
from pathlib import Path
import pytest

from config import Config
from copilot.services.ast_service import ASTService, generate_ast_outline


def test_ast_python_classes_and_functions(tmp_path):
    """Returns class name, line spans [start, end], method names, and docstring summaries."""
    py_file = tmp_path / "sample.py"
    content = '''"""Module docstring."""

class Calculator:
    """A simple calculator class."""

    def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    def subtract(self, a: int, b: int) -> int:
        """Subtract b from a."""
        return a - b

def standalone_helper(x: str) -> str:
    """Helper function docstring."""
    return x.strip()
'''
    py_file.write_text(content, encoding="utf-8")

    result = generate_ast_outline(py_file)
    assert result["language"] == "python"
    assert result["fallback"] is False
    assert result["error"] is None
    assert result["total_lines"] >= 15

    outline = result["outline"]
    assert len(outline) >= 3

    # Find Calculator class
    calc_classes = [s for s in outline if s["kind"] == "class" and s["name"] == "Calculator"]
    assert len(calc_classes) == 1
    calc = calc_classes[0]
    assert calc["start_line"] == 3
    assert calc["end_line"] >= 12
    assert "calculator class" in calc["docstring"]

    # Check methods attached to class or present in outline
    method_names = [m["simple_name"] for m in calc["methods"]]
    assert "add" in method_names
    assert "subtract" in method_names

    # Check standalone function
    helpers = [s for s in outline if s["name"] == "standalone_helper"]
    assert len(helpers) == 1
    assert helpers[0]["kind"] == "function"
    assert helpers[0]["start_line"] >= 13
    assert "Helper function docstring" in helpers[0]["docstring"]


def test_ast_javascript_typescript_functions(tmp_path):
    """Returns exported functions, classes, and interfaces for TS/JS files."""
    ts_file = tmp_path / "service.ts"
    content = """export interface UserConfig {
    id: string;
    timeout: number;
}

export class SessionManager {
    private config: UserConfig;
    constructor(cfg: UserConfig) {
        this.config = cfg;
    }
}

export async function fetchUserData(userId: string): Promise<UserConfig> {
    const res = await fetch(`/api/user/${userId}`);
    return res.json();
}

export const processMetrics = async (raw: number[]): Promise<number> => {
    return raw.reduce((a, b) => a + b, 0);
};
"""
    ts_file.write_text(content, encoding="utf-8")

    result = generate_ast_outline(ts_file)
    assert result["language"] == "typescript"
    assert result["fallback"] is False
    assert result["error"] is None

    outline = result["outline"]
    names = {s["name"]: s for s in outline}

    assert "UserConfig" in names
    assert names["UserConfig"]["kind"] == "interface"
    assert names["UserConfig"]["exported"] is True
    assert names["UserConfig"]["lines"][0] == 1

    assert "SessionManager" in names
    assert names["SessionManager"]["kind"] == "class"
    assert names["SessionManager"]["exported"] is True

    assert "fetchUserData" in names
    assert names["fetchUserData"]["kind"] == "function"
    assert names["fetchUserData"]["exported"] is True

    assert "processMetrics" in names
    assert names["processMetrics"]["kind"] == "function"
    assert names["processMetrics"]["exported"] is True


def test_ast_async_and_decorated_functions(tmp_path):
    """Correctly captures @decorator and async def declarations with their line ranges."""
    py_file = tmp_path / "async_demo.py"
    content = """@decorator_one
@decorator_two(option=True)
async def async_worker(task_id: int) -> bool:
    \"\"\"Process async background task.\"\"\"
    return True

class AsyncService:
    @staticmethod
    async def run_job():
        return 42
"""
    py_file.write_text(content, encoding="utf-8")

    result = generate_ast_outline(py_file)
    assert result["fallback"] is False

    outline = result["outline"]
    worker = next(s for s in outline if s["simple_name"] == "async_worker")
    assert worker["kind"] == "async_function"
    assert worker["is_async"] is True
    assert "@decorator_one" in worker["decorators"]
    assert any("@decorator_two" in d for d in worker["decorators"])
    assert worker["start_line"] == 3
    assert worker["end_line"] == 5

    # Async method inside class
    method = next(s for s in outline if s["simple_name"] == "run_job")
    assert method["kind"] == "async_method"
    assert method["is_async"] is True
    assert "@staticmethod" in method["decorators"]


def test_ast_deeply_nested_classes(tmp_path):
    """Captures inner classes and nested closures with hierarchical names (Outer.Inner.method)."""
    py_file = tmp_path / "nested.py"
    content = """class Outer:
    class Inner:
        def helper(self):
            \"\"\"Inner helper method.\"\"\"
            def local_closure():
                pass
            return 10
"""
    py_file.write_text(content, encoding="utf-8")

    result = generate_ast_outline(py_file)
    outline = result["outline"]

    symbol_names = [s["name"] for s in outline]
    assert "Outer" in symbol_names
    assert "Outer.Inner" in symbol_names
    assert "Outer.Inner.helper" in symbol_names

    helper_sym = next(s for s in outline if s["name"] == "Outer.Inner.helper")
    assert helper_sym["parent"] == "Outer.Inner"
    assert helper_sym["start_line"] == 3
    assert helper_sym["end_line"] == 7


def test_ast_large_file_token_budget(tmp_path):
    """A 2,500-line file generates an outline payload strictly under 200 tokens."""
    py_file = tmp_path / "large_module.py"

    # Generate a ~2,500 line Python file with 70 classes and functions
    parts = ['"""Very large module containing dozens of functions."""\n']
    for i in range(70):
        parts.append(f"""
class ServiceHandler{i}:
    \"\"\"Service handler documentation number {i}.\"\"\"

    def process_{i}(self, data_packet: dict, retry_count: int = 3) -> bool:
        \"\"\"Process request data packet {i} with automatic retry mechanism.\"\"\"
        # Simulate padding lines
        x = 1
        y = 2
        z = x + y + {i}
        return z > 0
""")
        # Add blank lines to exceed 2,500 lines
        parts.append("\n" * 30)

    content = "".join(parts)
    py_file.write_text(content, encoding="utf-8")

    line_count = len(content.splitlines())
    assert line_count >= 2500

    result = generate_ast_outline(py_file, max_tokens=Config.MAX_AST_TOKENS)

    assert result["total_lines"] >= 2500
    assert result["total_symbols"] >= 70

    # Strict token economy assertions
    assert result["token_count"] <= Config.MAX_AST_TOKENS
    assert len(result["outline_text"]) / Config.CHARS_PER_TOKEN <= Config.MAX_AST_TOKENS
    assert result["truncated"] is True


def test_ast_syntax_error_fallback(tmp_path):
    """If file contains unparseable Python syntax, falls back gracefully to regex symbol matching."""
    broken_file = tmp_path / "broken.py"
    content = """class UnfinishedClass:
    def good_func(self):
        pass

    def broken_func(self:
        # missing closing parenthesis and invalid syntax
        pass

def another_func():
    return 42
"""
    broken_file.write_text(content, encoding="utf-8")

    result = generate_ast_outline(broken_file)

    # Must NOT raise SyntaxError; must fallback gracefully
    assert result["fallback"] is True
    assert result["error"] is not None
    assert "SyntaxError" in result["error"]

    # Regex fallback extracts symbols
    symbol_names = [s["simple_name"] for s in result["outline"]]
    assert "UnfinishedClass" in symbol_names
    assert "good_func" in symbol_names
    assert "another_func" in symbol_names


def test_ast_unsupported_language_fallback(tmp_path):
    """For unsupported extensions (.txt, .sh), returns line count without crashing."""
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("Line 1: Note\nLine 2: Another note\nLine 3: Final note\n", encoding="utf-8")

    result_txt = generate_ast_outline(txt_file)
    assert result_txt["fallback"] is True
    assert result_txt["language"] == "text"
    assert result_txt["total_lines"] == 3
    assert result_txt["outline"] == []

    # Markdown outline extraction
    md_file = tmp_path / "README.md"
    md_file.write_text("# Project Title\n\n## Overview\n\n### Sub-section\n", encoding="utf-8")

    result_md = generate_ast_outline(md_file)
    assert result_md["language"] == "markdown"
    assert result_md["fallback"] is False
    assert len(result_md["outline"]) == 3
    headings = [h["name"] for h in result_md["outline"]]
    assert "Project Title" in headings
    assert "Overview" in headings


def test_ast_nonexistent_and_sensitive_file(tmp_path):
    """Graceful error reporting for non-existent and sensitive files."""
    missing_file = tmp_path / "nonexistent.py"
    res_missing = generate_ast_outline(missing_file)
    assert res_missing["fallback"] is True
    assert "ERR_NOT_FOUND" in res_missing["error"]
    assert res_missing["outline"] == []

    # Sensitive file
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=12345", encoding="utf-8")
    res_env = generate_ast_outline(env_file)
    assert res_env["fallback"] is True
    assert "Sensitive file access is denied" in res_env["error"]
    assert res_env["outline"] == []


def test_ast_service_class_aliases(tmp_path):
    """Verifies ASTService classmethod aliases."""
    py_file = tmp_path / "test_alias.py"
    py_file.write_text("def hello(): pass\n", encoding="utf-8")

    res1 = ASTService.get_outline(py_file)
    res2 = ASTService.generate_outline(py_file)
    res3 = generate_ast_outline(py_file)

    assert res1["language"] == res2["language"] == res3["language"] == "python"
    assert len(res1["outline"]) == len(res2["outline"]) == len(res3["outline"]) == 1
