"""
AST Service Module for Code Copilot.
===================================
Structural symbol outline extractor supporting Python, JavaScript/TypeScript,
and Markdown/Text. Provides syntax error fallback to regex, hierarchical symbol
nesting (Outer.Inner.method), and token economy compaction (<200 tokens).
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from config import Config
from copilot.core.security import validate_path

logger = logging.getLogger(__name__)

# Standard language extensions
PYTHON_EXTS: Set[str] = {".py", ".pyw", ".pyi"}
JS_EXTS: Set[str] = {".js", ".jsx", ".mjs", ".cjs"}
TS_EXTS: Set[str] = {".ts", ".tsx", ".mts", ".cts"}
MARKDOWN_EXTS: Set[str] = {".md", ".markdown"}


class ASTService:
    """Service providing code outline extraction and token economy compaction."""

    @classmethod
    def get_outline(
        cls,
        file_path: Path | str,
        max_tokens: int = Config.MAX_AST_TOKENS,
    ) -> Dict[str, Any]:
        """Alias for generate_ast_outline."""
        return generate_ast_outline(file_path, max_tokens=max_tokens)

    @classmethod
    def generate_outline(
        cls,
        file_path: Path | str,
        max_tokens: int = Config.MAX_AST_TOKENS,
    ) -> Dict[str, Any]:
        """Alias for generate_ast_outline."""
        return generate_ast_outline(file_path, max_tokens=max_tokens)


def detect_file_language(path: Path) -> str:
    """Detect language category from file extension."""
    suffix = path.suffix.lower()
    if suffix in PYTHON_EXTS:
        return "python"
    if suffix in TS_EXTS:
        return "typescript"
    if suffix in JS_EXTS:
        return "javascript"
    if suffix in MARKDOWN_EXTS:
        return "markdown"
    return "text"


def read_file_source(path: Path) -> Tuple[str, int]:
    """Read file content with UTF-8 and fallback encodings, returning (content, line_count)."""
    encodings = [Config.DEFAULT_ENCODING] + getattr(Config, "FALLBACK_ENCODINGS", ["latin-1"])
    for enc in encodings:
        try:
            content = path.read_text(encoding=enc)
            return content, len(content.splitlines())
        except (UnicodeDecodeError, LookupError):
            continue
    content = path.read_text(encoding="utf-8", errors="replace")
    return content, len(content.splitlines())


def parse_python_ast(
    source: str,
    filename: str = "<string>",
) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
    """Parse Python source into symbol list via ast.parse with SyntaxError fallback."""
    try:
        tree = ast.parse(source, filename=filename)
        symbols: List[Dict[str, Any]] = []

        def traverse(
            node: ast.AST,
            scope: Optional[List[str]] = None,
            parent_class: Optional[Dict[str, Any]] = None,
        ) -> None:
            if scope is None:
                scope = []

            for item in getattr(node, "body", []):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    is_async = isinstance(item, ast.AsyncFunctionDef)
                    full_name = ".".join(scope + [item.name])
                    kind = (
                        "async_method" if is_async and parent_class else (
                            "method" if parent_class else (
                                "async_function" if is_async else "function"
                            )
                        )
                    )
                    decorators = ["@" + ast.unparse(d) for d in item.decorator_list]
                    raw_doc = ast.get_docstring(item) or ""
                    doc_summary = raw_doc.strip().split("\n")[0].strip() if raw_doc else ""
                    sig_args = ast.unparse(item.args)
                    returns = ast.unparse(item.returns) if item.returns else None
                    signature = f"({sig_args})" + (f" -> {returns}" if returns else "")

                    sym: Dict[str, Any] = {
                        "name": full_name,
                        "simple_name": item.name,
                        "kind": kind,
                        "lines": [item.lineno, getattr(item, "end_lineno", item.lineno)],
                        "start_line": item.lineno,
                        "end_line": getattr(item, "end_lineno", item.lineno),
                        "signature": signature,
                        "parameters": [ast.unparse(a) for a in item.args.args],
                        "returns": returns,
                        "decorators": decorators,
                        "is_async": is_async,
                        "docstring": doc_summary,
                        "parent": ".".join(scope) if scope else None,
                    }
                    symbols.append(sym)
                    if parent_class is not None and "methods" in parent_class:
                        parent_class["methods"].append(sym)
                    traverse(item, scope + [item.name], None)

                elif isinstance(item, ast.ClassDef):
                    full_name = ".".join(scope + [item.name])
                    decorators = ["@" + ast.unparse(d) for d in item.decorator_list]
                    raw_doc = ast.get_docstring(item) or ""
                    doc_summary = raw_doc.strip().split("\n")[0].strip() if raw_doc else ""
                    bases = [ast.unparse(b) for b in item.bases]

                    sym = {
                        "name": full_name,
                        "simple_name": item.name,
                        "kind": "class",
                        "lines": [item.lineno, getattr(item, "end_lineno", item.lineno)],
                        "start_line": item.lineno,
                        "end_line": getattr(item, "end_lineno", item.lineno),
                        "bases": bases,
                        "decorators": decorators,
                        "docstring": doc_summary,
                        "parent": ".".join(scope) if scope else None,
                        "methods": [],
                    }
                    symbols.append(sym)
                    traverse(item, scope + [item.name], sym)

        traverse(tree)
        return symbols, False, None

    except (SyntaxError, IndentationError) as exc:
        logger.warning("SyntaxError parsing %s: %s. Falling back to regex.", filename, exc)
        symbols = parse_python_regex_fallback(source)
        error_msg = f"SyntaxError at line {getattr(exc, 'lineno', '?')}: {getattr(exc, 'msg', str(exc))}"
        return symbols, True, error_msg


def parse_python_regex_fallback(source: str) -> List[Dict[str, Any]]:
    """Graceful fallback parser using regex matching for Python symbols."""
    lines = source.splitlines()
    symbols: List[Dict[str, Any]] = []
    cls_re = re.compile(r"^(\s*)class\s+([A-Za-z0-9_]+)(?:\((.*?)\))?:")
    fn_re = re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z0-9_]+)\s*\(?")
    indent_stack: List[Tuple[int, str]] = []

    for idx, line in enumerate(lines):
        line_num = idx + 1
        m_cls = cls_re.match(line)
        if m_cls:
            indent = len(m_cls.group(1))
            name = m_cls.group(2)
            while indent_stack and indent_stack[-1][0] >= indent:
                indent_stack.pop()
            parent = ".".join([s[1] for s in indent_stack]) if indent_stack else None
            full_name = f"{parent}.{name}" if parent else name
            indent_stack.append((indent, name))
            symbols.append({
                "name": full_name,
                "simple_name": name,
                "kind": "class",
                "lines": [line_num, line_num],
                "start_line": line_num,
                "end_line": line_num,
                "parent": parent,
                "methods": [],
                "decorators": [],
                "docstring": "",
            })
            continue

        m_fn = fn_re.match(line)
        if m_fn:
            indent = len(m_fn.group(1))
            name = m_fn.group(2)
            while indent_stack and indent_stack[-1][0] >= indent:
                indent_stack.pop()
            parent = ".".join([s[1] for s in indent_stack]) if indent_stack else None
            full_name = f"{parent}.{name}" if parent else name
            is_async = "async " in line
            kind = "async_method" if is_async and parent else (
                "method" if parent else ("async_function" if is_async else "function")
            )
            sym = {
                "name": full_name,
                "simple_name": name,
                "kind": kind,
                "lines": [line_num, line_num],
                "start_line": line_num,
                "end_line": line_num,
                "is_async": is_async,
                "parent": parent,
                "decorators": [],
                "docstring": "",
            }
            symbols.append(sym)
            indent_stack.append((indent, name))

    return symbols


def parse_js_ts(source: str) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
    """Extract exported and declared functions, classes, and interfaces from JS/TS source."""
    lines = source.splitlines()
    symbols: List[Dict[str, Any]] = []

    def find_block_end(start_idx: int) -> int:
        depth = 0
        found_open = False
        for i in range(start_idx, len(lines)):
            depth += lines[i].count("{") - lines[i].count("}")
            if "{" in lines[i]:
                found_open = True
            if found_open and depth <= 0:
                return i + 1
        return start_idx + 1

    EXPORT_RE = re.compile(r"^\s*export\b")
    INTERFACE_RE = re.compile(
        r"^\s*(?:export\s+)?interface\s+([A-Za-z0-9_$]+)(?:<(.*?)>)?(?:\s+extends|\s*\{|\s*$)"
    )
    CLASS_RE = re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z0-9_$]+)(?:<(.*?)>)?(?:\s+extends|\s+implements|\s*\{|\s*$)"
    )
    FN_RE = re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function(?:\s*\*)?\s+([A-Za-z0-9_$]+)(?:<(.*?)>)?\s*\((.*?)\)"
    )
    ARROW_RE = re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?(?:<(.*?)>\s*)?\((.*?)\)\s*(?::\s*([^{=]+))?\s*=>"
    )

    for idx, line in enumerate(lines):
        line_num = idx + 1
        is_exported = bool(EXPORT_RE.match(line))

        m = INTERFACE_RE.match(line)
        if m:
            end = find_block_end(idx)
            sym = {
                "name": m.group(1),
                "simple_name": m.group(1),
                "kind": "interface",
                "lines": [line_num, end],
                "start_line": line_num,
                "end_line": end,
                "exported": is_exported,
            }
            if m.group(2):
                sym["type_parameters"] = f"<{m.group(2)}>"
            symbols.append(sym)
            continue

        m = CLASS_RE.match(line)
        if m:
            end = find_block_end(idx)
            sym = {
                "name": m.group(1),
                "simple_name": m.group(1),
                "kind": "class",
                "lines": [line_num, end],
                "start_line": line_num,
                "end_line": end,
                "exported": is_exported,
                "methods": [],
            }
            if m.group(2):
                sym["type_parameters"] = f"<{m.group(2)}>"
            symbols.append(sym)
            continue

        m = FN_RE.match(line)
        if m:
            end = find_block_end(idx)
            type_params = f"<{m.group(2)}>" if m.group(2) else ""
            sig = f"{type_params}({m.group(3)})" if type_params else f"({m.group(3)})"
            sym = {
                "name": m.group(1),
                "simple_name": m.group(1),
                "kind": "function",
                "lines": [line_num, end],
                "start_line": line_num,
                "end_line": end,
                "exported": is_exported,
                "signature": sig,
            }
            if m.group(2):
                sym["type_parameters"] = type_params
            symbols.append(sym)
            continue

        m = ARROW_RE.match(line)
        if m:
            end = find_block_end(idx)
            type_params = f"<{m.group(2)}>" if m.group(2) else ""
            sig = f"{type_params}({m.group(3)})" if type_params else f"({m.group(3)})"
            sym = {
                "name": m.group(1),
                "simple_name": m.group(1),
                "kind": "function",
                "lines": [line_num, end],
                "start_line": line_num,
                "end_line": end,
                "exported": is_exported,
                "signature": sig,
            }
            if m.group(2):
                sym["type_parameters"] = type_params
            symbols.append(sym)
            continue

    return symbols, False, None


def parse_markdown(source: str) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
    """Extract top-level markdown headings."""
    lines = source.splitlines()
    headings: List[Dict[str, Any]] = []
    heading_re = re.compile(r"^(#{1,6})\s+(.*)")

    for idx, line in enumerate(lines):
        m = heading_re.match(line.strip())
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            headings.append({
                "name": title,
                "simple_name": title,
                "kind": "heading",
                "level": level,
                "lines": [idx + 1, idx + 1],
                "start_line": idx + 1,
                "end_line": idx + 1,
            })
    return headings, False, None


def compact_outline(
    symbols: List[Dict[str, Any]],
    max_tokens: int = Config.MAX_AST_TOKENS,
    chars_per_token: float = Config.CHARS_PER_TOKEN,
) -> Tuple[List[Dict[str, Any]], str, int, bool]:
    """
    Format and compact symbols to guarantee total token consumption stays under max_tokens.
    Returns (compacted_symbols, outline_text, token_count, is_truncated).
    """
    if not symbols:
        return [], "", 0, False

    # Tier 0: Full outline with docstrings and signatures
    lines: List[str] = []
    for s in symbols:
        sig = s.get("signature", "")
        doc = (" - " + s["docstring"]) if s.get("docstring") else ""
        lines.append(f"{s['kind']} {s['name']}{sig} [{s['lines'][0]}-{s['lines'][1]}]{doc}")
    text = "\n".join(lines)
    tokens = int(len(text) / chars_per_token)
    if tokens <= max_tokens:
        return symbols, text, tokens, False

    # Tier 1: Drop docstrings
    lines = [f"{s['kind']} {s['name']}{s.get('signature', '')} [{s['lines'][0]}-{s['lines'][1]}]" for s in symbols]
    text = "\n".join(lines)
    tokens = int(len(text) / chars_per_token)
    if tokens <= max_tokens:
        compact_syms = [{k: v for k, v in s.items() if k != "docstring"} for s in symbols]
        return compact_syms, text, tokens, True

    # Tier 2: Drop signatures
    lines = [f"{s['name']} [{s['lines'][0]}-{s['lines'][1]}]" for s in symbols]
    text = "\n".join(lines)
    tokens = int(len(text) / chars_per_token)
    if tokens <= max_tokens:
        compact_syms = [{"name": s["name"], "kind": s["kind"], "lines": s["lines"]} for s in symbols]
        return compact_syms, text, tokens, True

    # Tier 3: Truncate lines and summarize
    budget_chars = int(max_tokens * chars_per_token)
    result_lines: List[str] = []
    result_syms: List[Dict[str, Any]] = []
    used_chars = 0

    for idx, s in enumerate(symbols):
        line = f"{s['name']} [{s['lines'][0]}-{s['lines'][1]}]"
        remaining = len(symbols) - idx
        notice = f"... (+{remaining} more symbols)"
        if used_chars + len(line) + 1 + len(notice) > budget_chars:
            result_lines.append(notice)
            break
        result_lines.append(line)
        result_syms.append({"name": s["name"], "kind": s["kind"], "lines": s["lines"]})
        used_chars += len(line) + 1

    text = "\n".join(result_lines)
    tokens = int(len(text) / chars_per_token)
    return result_syms, text, tokens, True


def generate_ast_outline(
    file_path: Path | str,
    max_tokens: int = Config.MAX_AST_TOKENS,
) -> Dict[str, Any]:
    """
    Generate structural outline for a file within the token budget.

    Args:
        file_path: Path to the target file (relative or absolute).
        max_tokens: Maximum token budget (default: Config.MAX_AST_TOKENS = 200).

    Returns:
        Dictionary conforming to the AST Service specification:
        {
            "file_path": str,
            "language": str,
            "outline": list[dict],
            "outline_text": str,
            "token_count": int,
            "total_lines": int,
            "total_symbols": int,
            "fallback": bool,
            "truncated": bool,
            "error": str | None
        }
    """
    is_valid, resolved_path, err_msg = validate_path(file_path, check_sensitive=True)
    if not is_valid or resolved_path is None:
        return {
            "success": False,
            "file_path": str(file_path),
            "language": "unknown",
            "outline": [],
            "outline_text": "",
            "token_count": 0,
            "total_lines": 0,
            "total_symbols": 0,
            "syntax_error": False,
            "fallback": True,
            "truncated": False,
            "error": err_msg or "Path validation failed",
        }

    if not resolved_path.exists():
        return {
            "success": False,
            "file_path": str(file_path),
            "language": "unknown",
            "outline": [],
            "outline_text": "",
            "token_count": 0,
            "total_lines": 0,
            "total_symbols": 0,
            "syntax_error": False,
            "fallback": True,
            "truncated": False,
            "error": f"ERR_NOT_FOUND: File not found: {resolved_path}",
        }

    source, total_lines = read_file_source(resolved_path)
    lang = detect_file_language(resolved_path)

    if lang == "python":
        symbols, fallback, error = parse_python_ast(source, filename=str(resolved_path))
    elif lang in ("typescript", "javascript"):
        symbols, fallback, error = parse_js_ts(source)
    elif lang == "markdown":
        symbols, fallback, error = parse_markdown(source)
    else:
        # Unsupported language: gracefully return line count and empty outline
        symbols, fallback, error = [], True, None

    # Compact outline according to token budget
    compacted_symbols, outline_text, token_count, is_truncated = compact_outline(
        symbols,
        max_tokens=max_tokens,
        chars_per_token=getattr(Config, "CHARS_PER_TOKEN", 4.0),
    )

    return {
        "success": True,
        "file_path": str(file_path),
        "language": lang,
        "outline": symbols if not is_truncated else compacted_symbols,
        "outline_text": outline_text,
        "token_count": token_count,
        "total_lines": total_lines,
        "total_symbols": len(symbols),
        "syntax_error": bool(error and "SyntaxError" in str(error)),
        "fallback": fallback,
        "truncated": is_truncated,
        "error": error,
    }
