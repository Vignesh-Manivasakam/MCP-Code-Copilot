"""
Search Service for Code Copilot.
================================
Provides SQLite FTS5 indexed full-text code search, regex grep search with context lines,
file-tree globbing, and symbol extraction with directory hygiene and result truncation.
"""
from __future__ import annotations

from contextlib import contextmanager
import fnmatch
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from config import Config
from copilot.core.security import (
    filter_excluded_dirs,
    is_binary_file,
    is_sensitive_file,
    validate_path,
)

logger = logging.getLogger(__name__)

# Standardized error codes
ERR_INVALID_REGEX = "ERR_INVALID_REGEX"
ERR_NOT_FOUND = "ERR_NOT_FOUND"
ERR_PATH_TRAVERSAL = "ERR_PATH_TRAVERSAL"
ERR_SEARCH_TIMEOUT = "ERR_SEARCH_TIMEOUT"


class SearchService:
    """Centralized code search engine supporting fulltext, grep, glob, and symbol queries."""

    def __init__(
        self,
        index_path: Optional[Path | str] = None,
        project_root: Optional[Path | str] = None,
        exclude_dirs: Optional[Set[str]] = None,
        table_name: Optional[str] = None,
    ) -> None:
        self._custom_index_path = Path(index_path).resolve() if index_path is not None else None
        self._custom_project_root = Path(project_root).resolve() if project_root is not None else None
        self._custom_exclude_dirs = exclude_dirs
        self._table_name = table_name or getattr(Config, "FTS_TABLE_NAME", "code_fts")

    @property
    def project_root(self) -> Path:
        """Resolve current project root dynamically."""
        if self._custom_project_root is not None:
            return self._custom_project_root
        if Config.PROJECT_ROOT is not None:
            return Path(Config.PROJECT_ROOT).resolve()
        return Path.cwd().resolve()

    @property
    def index_path(self) -> Path:
        """Resolve current index database path dynamically."""
        if self._custom_index_path is not None:
            return self._custom_index_path
        if self._custom_project_root is not None:
            return self._custom_project_root / ".copilot" / "index.db"
        if Config.INDEX_DB_PATH is not None:
            return Path(Config.INDEX_DB_PATH).resolve()
        return self.project_root / ".copilot" / "index.db"

    @property
    def exclude_dirs(self) -> Set[str]:
        """Resolve directory exclusion set."""
        if self._custom_exclude_dirs is not None:
            return self._custom_exclude_dirs
        return getattr(Config, "EXCLUDE_DIRS", {
            ".git", ".venv", "venv", "node_modules", "__pycache__",
            ".pytest_cache", ".mcp_index", ".copilot", "build", "dist",
        })

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Open a SQLite connection configured with WAL and busy timeout, guaranteeing closure."""
        db_path = self.index_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            with conn:
                yield conn
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # FTS5 Index Management
    # --------------------------------------------------------------------------

    def initialize_index(self, force: bool = False) -> None:
        """Create the FTS5 virtual table if it does not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if force:
                # Safely drop views and tables regardless of whether code_search is a view or table
                try:
                    cursor.execute("DROP VIEW IF EXISTS code_search;")
                except sqlite3.OperationalError:
                    pass
                try:
                    cursor.execute(f"DROP TABLE IF EXISTS {self._table_name};")
                except sqlite3.OperationalError:
                    pass
                try:
                    cursor.execute("DROP TABLE IF EXISTS code_search;")
                except sqlite3.OperationalError:
                    pass
                try:
                    cursor.execute("DROP TABLE IF EXISTS code_files;")
                except sqlite3.OperationalError:
                    pass
            cursor.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {self._table_name} USING fts5(
                    file_path UNINDEXED,
                    content,
                    tokenize="unicode61"
                );
            """)
            if self._table_name != "code_search":
                cursor.execute(f"""
                    CREATE VIEW IF NOT EXISTS code_search AS 
                    SELECT file_path, content FROM {self._table_name};
                """)
            conn.commit()

    def index_file(self, file_path: Path | str) -> bool:
        """Read and index a single text file into the FTS5 index table."""
        p = Path(file_path)
        if not p.is_absolute():
            p = self.project_root / p

        if not p.exists() or not p.is_file():
            return False
        if is_sensitive_file(p) or is_binary_file(p):
            return False

        try:
            rel_path = p.relative_to(self.project_root).as_posix()
        except ValueError:
            return False

        parts_lower = {part.lower() for part in p.parts}
        if any(ex.lower() in parts_lower for ex in self.exclude_dirs):
            return False

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.debug("Failed reading %s: %s", p, exc)
            return False

        self.initialize_index()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"DELETE FROM {self._table_name} WHERE file_path = ?;", (rel_path,))
            cursor.execute(
                f"INSERT INTO {self._table_name} (file_path, content) VALUES (?, ?);",
                (rel_path, content),
            )
            conn.commit()
        return True

    def index_all(self, force: bool = False) -> int:
        """Walk project root and index all eligible text files."""
        self.initialize_index(force=force)
        indexed_count = 0
        records: List[Tuple[str, str]] = []

        for root_dir, dirnames, filenames in os.walk(self.project_root):
            filter_excluded_dirs(dirnames, self.exclude_dirs)
            dir_path = Path(root_dir)

            for fname in filenames:
                full_path = dir_path / fname
                if is_sensitive_file(full_path) or is_binary_file(full_path):
                    continue

                try:
                    if full_path.stat().st_size > 10 * 1024 * 1024:
                        continue
                    content = full_path.read_text(encoding="utf-8", errors="replace")
                    rel_path = full_path.relative_to(self.project_root).as_posix()
                    records.append((rel_path, content))
                except Exception:
                    continue

        if records:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"DELETE FROM {self._table_name};")
                cursor.executemany(
                    f"INSERT INTO {self._table_name} (file_path, content) VALUES (?, ?);",
                    records,
                )
                conn.commit()
            indexed_count = len(records)

        return indexed_count

    # --------------------------------------------------------------------------
    # 1. Fulltext Search (FTS5)
    # --------------------------------------------------------------------------

    def fts_search(
        self,
        query: str,
        max_results: int = 25,
    ) -> Dict[str, Any]:
        """Perform indexed fulltext search returning <<<match>>> highlighted snippets."""
        clean_query = query.strip()
        if not clean_query:
            return {
                "query": query,
                "total_matches": 0,
                "truncated": False,
                "results": [],
                "message": "Search query cannot be empty",
            }

        # Auto-index if database file does not exist
        if not self.index_path.exists():
            self.index_all()

        tokens = re.findall(r"\w+", clean_query)
        if not tokens:
            return {
                "query": query,
                "total_matches": 0,
                "truncated": False,
                "results": [],
                "message": f"No matches found for query: '{query}'",
            }

        match_query = " OR ".join(f'"{t}"*' for t in tokens if t)

        results: List[Dict[str, Any]] = []
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                    SELECT 
                        file_path, 
                        snippet({self._table_name}, 1, '<<<', '>>>', '...', 64) as match_snippet
                    FROM {self._table_name} 
                    WHERE {self._table_name} MATCH ? 
                    LIMIT ?;
                    """,
                    (match_query, max_results + 1),
                )
                rows = cursor.fetchall()

                count_cur = conn.cursor()
                count_cur.execute(
                    f"SELECT count(*) FROM {self._table_name} WHERE {self._table_name} MATCH ?;",
                    (match_query,),
                )
                count_row = count_cur.fetchone()
                total_matches = count_row[0] if count_row else len(rows)

                for r in rows[:max_results]:
                    results.append({
                        "file": r[0],
                        "file_path": r[0],
                        "snippet": r[1],
                    })
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 query failed for '%s': %s", match_query, exc)
            return {
                "query": query,
                "total_matches": 0,
                "truncated": False,
                "results": [],
                "message": f"No matches found for query: '{query}'",
            }

        is_truncated = total_matches > max_results
        notice = f"Showing {len(results)} of {total_matches} matches." if is_truncated else None

        return {
            "query": query,
            "total_matches": total_matches,
            "truncated": is_truncated,
            "truncation_notice": notice,
            "results": results,
            "message": f"No matches found for query: '{query}'" if not results else None,
        }

    # --------------------------------------------------------------------------
    # 2. Grep Regex Content Search
    # --------------------------------------------------------------------------

    def grep_search(
        self,
        query: str,
        path: Optional[Path | str] = None,
        file_pattern: Optional[str] = None,
        max_results: int = 25,
        context_lines: int = 2,
        case_sensitive: bool = True,
    ) -> Dict[str, Any]:
        """Search file contents for regex or text with surrounding context lines."""
        if not query:
            return {
                "error": "ERR_INVALID_REGEX",
                "error_code": "ERR_INVALID_REGEX",
                "message": "Search query cannot be empty",
                "query": query,
                "results": [],
                "total_matches": 0,
                "truncated": False,
            }

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(query, flags)
        except re.error as exc:
            return {
                "error": "ERR_INVALID_REGEX",
                "error_code": "ERR_INVALID_REGEX",
                "message": f"Invalid regex query '{query}': {exc}",
                "query": query,
                "results": [],
                "total_matches": 0,
                "truncated": False,
            }

        search_root = self.project_root
        if path is not None:
            is_valid, resolved, err_msg = validate_path(path, self.project_root)
            if not is_valid or resolved is None:
                return {
                    "error": "ERR_PATH_TRAVERSAL",
                    "error_code": "ERR_PATH_TRAVERSAL",
                    "message": err_msg,
                    "query": query,
                    "results": [],
                    "total_matches": 0,
                    "truncated": False,
                }
            search_root = resolved

        matches: List[Dict[str, Any]] = []
        total_matches = 0
        files_searched = 0

        target_files: List[Path] = []
        if search_root.is_file():
            target_files = [search_root]
        else:
            for root_dir, dirnames, filenames in os.walk(search_root):
                filter_excluded_dirs(dirnames, self.exclude_dirs)
                dir_path = Path(root_dir)
                for fname in filenames:
                    target_files.append(dir_path / fname)

        for fpath in target_files:
            if file_pattern and not fnmatch.fnmatch(fpath.name, file_pattern):
                continue
            if is_sensitive_file(fpath) or is_binary_file(fpath):
                continue

            try:
                if fpath.stat().st_size > 10 * 1024 * 1024:
                    continue
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            files_searched += 1
            lines = content.splitlines()
            try:
                rel_path = fpath.relative_to(self.project_root).as_posix()
            except ValueError:
                rel_path = str(fpath).replace("\\", "/")

            for i, line in enumerate(lines):
                if compiled.search(line):
                    total_matches += 1
                    if len(matches) < max_results:
                        ctx_start = max(0, i - context_lines)
                        ctx_end = min(len(lines), i + context_lines + 1)
                        context_block = "\n".join(
                            f"{'>' if j == i else ' '} {j + 1}: {lines[j]}"
                            for j in range(ctx_start, ctx_end)
                        )
                        matches.append({
                            "file": rel_path,
                            "file_path": rel_path,
                            "line": i + 1,
                            "line_number": i + 1,
                            "content": line.strip(),
                            "context": context_block,
                            "context_lines": lines[ctx_start:ctx_end],
                        })

        is_truncated = total_matches > max_results
        notice = f"Showing {len(matches)} of {total_matches} matches." if is_truncated else None

        return {
            "query": query,
            "total_matches": total_matches,
            "files_searched": files_searched,
            "truncated": is_truncated,
            "truncation_notice": notice,
            "results": matches,
            "message": f"No matches found for query: '{query}'" if total_matches == 0 else None,
        }

    # --------------------------------------------------------------------------
    # 3. File-Tree Glob Search
    # --------------------------------------------------------------------------

    def glob_files(
        self,
        pattern: str = "*",
        root_dir: Optional[Path | str] = None,
        max_results: int = 25,
    ) -> Dict[str, Any]:
        """Search project file tree matching a glob pattern, pruning excluded dirs."""
        search_root = self.project_root
        if root_dir is not None:
            is_valid, resolved, _ = validate_path(root_dir, self.project_root)
            if is_valid and resolved is not None:
                search_root = resolved

        matches: List[Dict[str, Any]] = []
        total_matches = 0

        for root, dirnames, filenames in os.walk(search_root):
            filter_excluded_dirs(dirnames, self.exclude_dirs)
            dir_path = Path(root)

            for name in dirnames + filenames:
                item_path = dir_path / name
                if is_sensitive_file(item_path):
                    continue

                try:
                    rel_path = item_path.relative_to(self.project_root).as_posix()
                except ValueError:
                    rel_path = str(item_path).replace("\\", "/")

                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_path, pattern):
                    total_matches += 1
                    if len(matches) < max_results:
                        is_file = item_path.is_file()
                        size = 0
                        try:
                            size = item_path.stat().st_size if is_file else 0
                        except OSError:
                            pass

                        matches.append({
                            "path": rel_path,
                            "file_path": rel_path,
                            "name": name,
                            "is_file": is_file,
                            "is_dir": item_path.is_dir(),
                            "size": size,
                        })

        is_truncated = total_matches > max_results
        notice = f"Showing {len(matches)} of {total_matches} files." if is_truncated else None

        return {
            "pattern": pattern,
            "total_matches": total_matches,
            "truncated": is_truncated,
            "truncation_notice": notice,
            "results": matches,
            "message": f"No files found matching pattern: '{pattern}'" if total_matches == 0 else None,
        }

    # --------------------------------------------------------------------------
    # 4. Symbol Search
    # --------------------------------------------------------------------------

    def search_symbols(
        self,
        query: str,
        path: Optional[Path | str] = None,
        file_pattern: Optional[str] = None,
        max_results: int = 25,
    ) -> Dict[str, Any]:
        """Search for class/function definitions matching symbol query."""
        if not query:
            return {
                "query": query,
                "total_matches": 0,
                "truncated": False,
                "results": [],
                "message": "Symbol query cannot be empty",
            }

        sym_escaped = re.escape(query)
        symbol_patterns = [
            re.compile(rf"^\s*(?:async\s+)?def\s+({sym_escaped}\w*)\s*\("),             # Python def
            re.compile(rf"^\s*class\s+({sym_escaped}\w*)\b"),                            # Python class
            re.compile(rf"\bfunction\s+({sym_escaped}\w*)\s*\("),                        # JS/TS fn
            re.compile(rf"\basync\s+function\s+({sym_escaped}\w*)\s*\("),                # JS/TS async fn
            re.compile(rf"\b(?:const|let|var)\s+({sym_escaped}\w*)\s*=\s*(?:async\s*)?(?:function|\()"), # JS arrow
            re.compile(rf"\bclass\s+({sym_escaped}\w*)\b"),                              # JS/TS class
            re.compile(rf"\bfunc\s+({sym_escaped}\w*)\s*\("),                            # Go/Swift
            re.compile(rf"\bfn\s+({sym_escaped}\w*)\s*\("),                              # Rust
        ]

        search_root = self.project_root
        if path is not None:
            is_valid, resolved, _ = validate_path(path, self.project_root)
            if is_valid and resolved is not None:
                search_root = resolved

        matches: List[Dict[str, Any]] = []
        total_matches = 0

        for root, dirnames, filenames in os.walk(search_root):
            filter_excluded_dirs(dirnames, self.exclude_dirs)
            dir_path = Path(root)

            for fname in filenames:
                if file_pattern and not fnmatch.fnmatch(fname, file_pattern):
                    continue
                fpath = dir_path / fname
                if is_sensitive_file(fpath) or is_binary_file(fpath):
                    continue

                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                lines = content.splitlines()
                try:
                    rel_path = fpath.relative_to(self.project_root).as_posix()
                except ValueError:
                    rel_path = str(fpath).replace("\\", "/")

                for i, line in enumerate(lines):
                    for pat in symbol_patterns:
                        m = pat.search(line)
                        if m:
                            total_matches += 1
                            if len(matches) < max_results:
                                matches.append({
                                    "file": rel_path,
                                    "file_path": rel_path,
                                    "line": i + 1,
                                    "symbol": m.group(1),
                                    "definition": line.strip(),
                                })
                            break

        is_truncated = total_matches > max_results
        notice = f"Showing {len(matches)} of {total_matches} symbols." if is_truncated else None

        return {
            "query": query,
            "total_matches": total_matches,
            "truncated": is_truncated,
            "truncation_notice": notice,
            "results": matches,
            "message": f"No symbols found for query: '{query}'" if total_matches == 0 else None,
        }

    # Alias for DISPATCH.md naming compatibility
    symbol_search = search_symbols
