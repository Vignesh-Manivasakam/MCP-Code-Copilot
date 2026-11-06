"""
Authoritative Empirical Challenge Suite for Milestone M3 AST Service.
=====================================================================
Stress-tests:
1. Token economy: 2,500-line synthetic Python file -> assert < 200 tokens.
2. Syntax error fallback: invalid syntax -> assert fallback: True, success: True,
   and symbols extracted without throwing unhandled exceptions.
3. Nested classes/methods: 4 levels deep -> assert dot notation and parent links.
4. JS/TS outlines: interfaces, classes, functions, arrow functions, and edge cases
   (substring collisions, generics, abstract classes).
"""
from pathlib import Path
import pytest

from config import Config
from copilot.services.ast_service import ASTService, generate_ast_outline, parse_js_ts


def test_challenge_token_economy_2500_lines(tmp_path):
    """
    Challenge 1: Token economy.
    Generate a 2,500-line synthetic Python file with nested classes, functions,
    and docstrings. Assert outline token count strictly < 200 tokens.
    """
    py_file = tmp_path / "huge_synthetic.py"
    parts = ["# Synthetic 2,500-line file for token economy challenge\n"]
    for i in range(120):
        parts.append(
            f"class ModuleServiceManager{i}:\n"
            f"    '''Docstring for ModuleServiceManager{i} describing subsystem.'''\n"
            f"    def execute_subroutine_{i}(self, payload: dict, timeout: int = 60) -> dict:\n"
            f"        '''Execute subroutine {i} with payload.'''\n"
            f"        x = {i}\n"
            f"        return {{'status': 'ok', 'id': x}}\n"
            f"    class NestedSubService{i}:\n"
            f"        '''Inner service {i}.'''\n"
            f"        def inner_run_{i}(self) -> None:\n"
            f"            pass\n"
        )
        parts.append("\n" * 15)

    content = "".join(parts)
    py_file.write_text(content, encoding="utf-8")
    lines = content.splitlines()
    assert len(lines) >= 2500, f"File had {len(lines)} lines, expected >= 2500"

    result = generate_ast_outline(py_file, max_tokens=200)

    # Verification of line count and symbol extraction
    assert result["total_lines"] >= 2500
    assert result["total_symbols"] > 50

    # Strict token economy assertions: strictly < 200 tokens
    assert result["token_count"] < 200, f"Token count was {result['token_count']}, expected < 200"
    assert result["truncated"] is True
    estimated = len(result["outline_text"]) / Config.CHARS_PER_TOKEN
    assert estimated < 200, f"Estimated tokens {estimated} >= 200"


def test_challenge_syntax_error_fallback(tmp_path):
    """
    Challenge 2: Syntax error fallback.
    Test broken Python syntax (e.g. 'def foo(').
    Assert fallback: True, success: True, and symbols extracted without throwing exception.
    """
    broken_file = tmp_path / "broken_syntax.py"
    content = (
        "class FirstValidClass:\n"
        "    def method_one(self):\n"
        "        pass\n"
        "\n"
        "def foo(\n"  # Broken syntax: unclosed parenthesis
        "\n"
        "def second_valid_func():\n"
        "    return 42\n"
        "\n"
        "class SecondValidClass:\n"
        "    def method_two(self):\n"
        "        return 'ok'\n"
    )
    broken_file.write_text(content, encoding="utf-8")

    result = generate_ast_outline(broken_file)

    # Must fall back gracefully without unhandled exception
    assert result["fallback"] is True, f"Expected fallback=True, got {result['fallback']}"
    assert result["error"] is not None
    assert "SyntaxError" in result["error"]

    # Symbols extracted via regex fallback
    extracted_names = [s["simple_name"] for s in result["outline"]]
    assert "FirstValidClass" in extracted_names
    assert "foo" in extracted_names
    assert "second_valid_func" in extracted_names
    assert "SecondValidClass" in extracted_names

    # Check dispatch contract requirement: success == True
    # (Notice: ast_service currently omits 'success' key)
    assert result.get("success") is True, (
        f"Missing 'success: True' in return dict. Keys present: {list(result.keys())}"
    )


def test_challenge_nested_hierarchy_4_levels(tmp_path):
    """
    Challenge 3: Nested hierarchy 4 levels deep.
    Test 4-level deep classes/methods (Level1.Level2.Level3.Level4.deep_method).
    Assert dot-separated names and hierarchical parent references.
    """
    nested_file = tmp_path / "deep_nested.py"
    content = (
        "class Level1:\n"
        "    '''Level 1 class.'''\n"
        "    class Level2:\n"
        "        '''Level 2 class.'''\n"
        "        class Level3:\n"
        "            '''Level 3 class.'''\n"
        "            class Level4:\n"
        "                '''Level 4 class.'''\n"
        "                def deep_method(self, x: int) -> int:\n"
        "                    '''Deep method docstring.'''\n"
        "                    return x * 2\n"
    )
    nested_file.write_text(content, encoding="utf-8")

    result = generate_ast_outline(nested_file)
    assert result["fallback"] is False
    assert result["error"] is None

    symbol_map = {s["name"]: s for s in result["outline"]}

    assert "Level1" in symbol_map
    assert "Level1.Level2" in symbol_map
    assert "Level1.Level2.Level3" in symbol_map
    assert "Level1.Level2.Level3.Level4" in symbol_map
    assert "Level1.Level2.Level3.Level4.deep_method" in symbol_map

    deep_sym = symbol_map["Level1.Level2.Level3.Level4.deep_method"]
    assert deep_sym["kind"] == "method"
    assert deep_sym["parent"] == "Level1.Level2.Level3.Level4"
    assert deep_sym["simple_name"] == "deep_method"


def test_challenge_jsts_standard_outlines(tmp_path):
    """
    Challenge 4A: JS/TS standard outlines.
    Verify TS interfaces, exported functions, classes, and arrow functions.
    """
    ts_file = tmp_path / "app.ts"
    content = (
        "export interface UserProfile {\n"
        "    id: string;\n"
        "    name: string;\n"
        "}\n"
        "\n"
        "interface InternalConfig {\n"
        "    debug: boolean;\n"
        "}\n"
        "\n"
        "export class AuthService {\n"
        "    login() {}\n"
        "}\n"
        "\n"
        "class LocalHelper {\n"
        "    compute() {}\n"
        "}\n"
        "\n"
        "export async function authenticate(token: string): Promise<boolean> {\n"
        "    return true;\n"
        "}\n"
        "\n"
        "function localUtility(): void {}\n"
        "\n"
        "export const logout = async (): Promise<void> => {};\n"
    )
    ts_file.write_text(content, encoding="utf-8")

    result = generate_ast_outline(ts_file)
    assert result["language"] == "typescript"
    assert result["fallback"] is False
    assert result["error"] is None

    names = {s["name"]: s for s in result["outline"]}

    assert "UserProfile" in names and names["UserProfile"]["exported"] is True
    assert "InternalConfig" in names and names["InternalConfig"]["exported"] is False
    assert "AuthService" in names and names["AuthService"]["exported"] is True
    assert "LocalHelper" in names and names["LocalHelper"]["exported"] is False
    assert "authenticate" in names and names["authenticate"]["exported"] is True
    assert "localUtility" in names and names["localUtility"]["exported"] is False
    assert "logout" in names and names["logout"]["exported"] is True


def test_challenge_jsts_exported_substring_bug(tmp_path):
    """
    Challenge 4B: JS/TS is_exported substring false-positive challenge.
    Demonstrates that 'export' in line incorrectly flags unexported symbols
    whose name contains 'export' (e.g. exportData, unexportedFunc) or lines
    with inline comments containing 'export'.
    """
    ts_file = tmp_path / "export_sub.ts"
    content = (
        "function exportData(id: string): void {}\n"
        "function unexportedFunc(): void {}\n"
        "function calculateMetrics(): void { // do not export }\n"
    )
    ts_file.write_text(content, encoding="utf-8")

    result = generate_ast_outline(ts_file)
    names = {s["name"]: s for s in result["outline"]}

    assert "exportData" in names
    # Bug: is_exported uses 'export' in line, causing False -> True
    assert names["exportData"]["exported"] is False, (
        "BUG: exportData incorrectly marked as exported: True due to substring match 'export' in line"
    )
    assert names["unexportedFunc"]["exported"] is False, (
        "BUG: unexportedFunc incorrectly marked as exported: True due to substring match 'export' in line"
    )
    assert names["calculateMetrics"]["exported"] is False, (
        "BUG: calculateMetrics incorrectly marked as exported: True due to inline comment 'do not export'"
    )


def test_challenge_jsts_generics_and_abstract_classes(tmp_path):
    """
    Challenge 4C: JS/TS generics and abstract classes support challenge.
    Demonstrates that generic functions and abstract classes are dropped by regexes.
    """
    ts_file = tmp_path / "advanced.ts"
    content = (
        "export function processItem<T>(item: T): T {\n"
        "    return item;\n"
        "}\n"
        "\n"
        "export abstract class AbstractRepository {\n"
        "    abstract findById(id: string): any;\n"
        "}\n"
    )
    ts_file.write_text(content, encoding="utf-8")

    result = generate_ast_outline(ts_file)
    names = {s["name"]: s for s in result["outline"]}

    assert "processItem" in names, "BUG: TS generic function processItem<T> dropped by FN_RE"
    assert "AbstractRepository" in names, "BUG: TS abstract class AbstractRepository dropped by CLASS_RE"
