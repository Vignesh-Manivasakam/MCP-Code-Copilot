"""
Configuration for Code Copilot MCP Server
=========================================
Centralized configuration class defining workspace paths, sandboxed execution
settings, directory exclusions, sensitive file protection, and token telemetry quotas.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Set

_UNSET = object()


class _ConfigMeta(type):
    """
    Metaclass enabling dynamic path calculation and test isolation.

    When `Config.PROJECT_ROOT` changes (e.g. `Config.PROJECT_ROOT = tmp_path` in pytest),
    all derived paths (`HISTORY_DB_PATH`, `TRACE_LOG_PATH`, `INDEX_DB_PATH`, `CONTEXT_FILE_PATH`)
    automatically resolve relative to the new project root.
    """
    _project_root: Any = _UNSET
    _history_db_path: Optional[Path] = None
    _trace_log_path: Optional[Path] = None
    _index_db_path: Optional[Path] = None
    _context_file_path: Optional[Path] = None

    @property
    def PROJECT_ROOT(cls) -> Optional[Path]:
        """Project root path. Defaults to os.environ['PROJECT_ROOT'] or Path.cwd()."""
        if cls._project_root is _UNSET:
            env_val = os.environ.get("PROJECT_ROOT")
            return Path(env_val).resolve() if env_val else Path.cwd().resolve()
        return cls._project_root

    @PROJECT_ROOT.setter
    def PROJECT_ROOT(cls, value: Any) -> None:
        if value is None or value is _UNSET:
            cls._project_root = value
        else:
            cls._project_root = Path(value).resolve()

    @property
    def COPILOT_DIR(cls) -> Optional[Path]:
        """Base directory for .copilot metadata and storage."""
        if cls.PROJECT_ROOT is None:
            return None
        return cls.PROJECT_ROOT / ".copilot"

    @property
    def HISTORY_DB_PATH(cls) -> Optional[Path]:
        """Path to SQLite reverse-patch history database."""
        if cls._history_db_path is not None:
            return cls._history_db_path
        if cls.COPILOT_DIR is None:
            return None
        return cls.COPILOT_DIR / "history.db"

    @HISTORY_DB_PATH.setter
    def HISTORY_DB_PATH(cls, value: Optional[Path]) -> None:
        cls._history_db_path = Path(value).resolve() if value is not None else None

    @property
    def TRACE_LOG_PATH(cls) -> Optional[Path]:
        """Path to structured JSONL telemetry trace log."""
        if cls._trace_log_path is not None:
            return cls._trace_log_path
        if cls.COPILOT_DIR is None:
            return None
        return cls.COPILOT_DIR / "logs" / "trace.jsonl"

    @TRACE_LOG_PATH.setter
    def TRACE_LOG_PATH(cls, value: Optional[Path]) -> None:
        cls._trace_log_path = Path(value).resolve() if value is not None else None

    @property
    def INDEX_DB_PATH(cls) -> Optional[Path]:
        """Path to SQLite FTS5 code search index database."""
        if cls._index_db_path is not None:
            return cls._index_db_path
        if cls.COPILOT_DIR is None:
            return None
        return cls.COPILOT_DIR / "index.db"

    @INDEX_DB_PATH.setter
    def INDEX_DB_PATH(cls, value: Optional[Path]) -> None:
        cls._index_db_path = Path(value).resolve() if value is not None else None

    @property
    def CONTEXT_FILE_PATH(cls) -> Optional[Path]:
        """Path to workspace conventions context markdown file."""
        if cls._context_file_path is not None:
            return cls._context_file_path
        if cls.COPILOT_DIR is None:
            return None
        return cls.COPILOT_DIR / "context.md"

    @CONTEXT_FILE_PATH.setter
    def CONTEXT_FILE_PATH(cls, value: Optional[Path]) -> None:
        cls._context_file_path = Path(value).resolve() if value is not None else None

    @property
    def LOGS_DIR(cls) -> Optional[Path]:
        """Path to .copilot/logs directory."""
        if cls.COPILOT_DIR is None:
            return None
        return cls.COPILOT_DIR / "logs"


class Config(metaclass=_ConfigMeta):
    """Centralized configuration for Code Copilot server and poly-tools."""

    def __getattr__(self, name: str) -> Any:
        return getattr(type(self), name)

    # ---------------------------------------------------------------------------
    # Server & Protocol Settings
    # ---------------------------------------------------------------------------
    SERVER_NAME: str = "Code Copilot MCP Server"
    SERVER_VERSION: str = "0.2.0"
    DEFAULT_PORT: int = 8001
    DEFAULT_HOST: str = "127.0.0.1"
    MCP_PROTOCOL_VERSION: str = "2024-11-05"

    # CORS & Transport Settings
    CORS_ALLOW_PRIVATE_NETWORK: bool = True
    HOST_ORIGIN_PROTECTION: bool = False

    # ---------------------------------------------------------------------------
    # History Settings (Part 1.1 & Part 4 Suite 6)
    # ---------------------------------------------------------------------------
    MAX_HISTORY_ENTRIES: int = 20
    MAX_ENTRIES: int = 20  # Backward-compatibility alias
    HISTORY_TABLE_NAME: str = "edit_history"

    # ---------------------------------------------------------------------------
    # Sandbox Settings (Part 1.3, 3.3 & Part 4 Suite 5)
    # ---------------------------------------------------------------------------
    ALLOWED_COMMANDS: Set[str] = {
        "python",
        "pytest",
        "npm",
        "pip",
        "poetry",
        "uv",
        "cargo",
        "go",
        "dotnet",
        "ruff",
        "black",
        "flake8",
        "git",
        "node",
    }
    DEFAULT_TIMEOUT_SECONDS: int = 30
    COMMAND_TIMEOUT_MAX: int = 300
    MAX_OUTPUT_LINES: int = 40
    ENV_SCRUB_PATTERNS: list[str] = [
        "*KEY*",
        "*TOKEN*",
        "*SECRET*",
        "AWS_*",
        "*PASSWORD*",
        "*CREDENTIAL*",
    ]

    # ---------------------------------------------------------------------------
    # Directory Hygiene & Exclusions (Part 4 Suite 4)
    # ---------------------------------------------------------------------------
    EXCLUDE_DIRS: Set[str] = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mcp_index",
        ".copilot",
        "build",
        "dist",
        ".vscode",
        ".idea",
    }
    IGNORED_DIRS: Set[str] = EXCLUDE_DIRS  # Backward-compatibility alias

    # ---------------------------------------------------------------------------
    # Sensitive File Protection (Part 4 Suite 1)
    # ---------------------------------------------------------------------------
    SENSITIVE_FILES: Set[str] = {
        ".env",
        ".git/config",
        "id_rsa",
        "id_ed25519",
        ".secrets",
    }

    # ---------------------------------------------------------------------------
    # Telemetry & Token Budget Settings (Part 1.2 & Part 4 Suite 8)
    # ---------------------------------------------------------------------------
    TOKEN_BUDGET_LIMIT: int = 1_000_000
    TOKEN_WARNING_THRESHOLD: float = 0.8
    CHARS_PER_TOKEN: float = 4.0
    ENABLE_TELEMETRY: bool = True

    # ---------------------------------------------------------------------------
    # AST & Search Defaults
    # ---------------------------------------------------------------------------
    MAX_AST_TOKENS: int = 200
    AST_SUPPORTED_EXTENSIONS: Set[str] = {".py", ".js", ".jsx", ".ts", ".tsx"}
    GREP_CONTEXT_LINES: int = 2
    GREP_MAX_RESULTS: int = 25
    MAX_SEARCH_RESULTS: int = 25
    SEARCH_TIMEOUT_SECONDS: int = 10
    FTS_TABLE_NAME: str = "code_fts"

    # ---------------------------------------------------------------------------
    # File Limits & Encodings
    # ---------------------------------------------------------------------------
    FILE_SIZE_WARNING_THRESHOLD: int = 1 * 1024 * 1024   # 1 MB
    FILE_SIZE_CHUNK_THRESHOLD: int = 5 * 1024 * 1024     # 5 MB
    FILE_SIZE_MAX_READ: int = 50 * 1024 * 1024            # 50 MB
    MAX_WRITE_SIZE: int = 10 * 1024 * 1024               # 10 MB
    MAX_BATCH_READ_FILES: int = 10
    DEFAULT_ENCODING: str = "utf-8"
    FALLBACK_ENCODINGS: list[str] = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    ENABLE_REQUEST_LOGGING: bool = True

    # In-memory registry caches for backward compatibility
    FILE_READ_REGISTRY: dict = {}
    EDIT_HISTORY: dict = {}

    # ---------------------------------------------------------------------------
    # Management Methods
    # ---------------------------------------------------------------------------
    @classmethod
    def set_project_root(cls, path: str | Path) -> None:
        """Set and validate project root path, creating .copilot directories."""
        project_path = Path(path).resolve()
        if not project_path.exists():
            raise ValueError(f"Project path does not exist: {project_path}")
        if not project_path.is_dir():
            raise ValueError(f"Project path is not a directory: {project_path}")
        cls.PROJECT_ROOT = project_path
        cls.ensure_directories()

    @classmethod
    def ensure_directories(cls) -> None:
        """Ensure .copilot and log directories exist."""
        if cls.COPILOT_DIR is not None:
            cls.COPILOT_DIR.mkdir(parents=True, exist_ok=True)
        if cls.LOGS_DIR is not None:
            cls.LOGS_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def validate_config(cls) -> None:
        """Validate required configuration before server start."""
        if cls.PROJECT_ROOT is None:
            raise RuntimeError("PROJECT_ROOT must be set before starting server")
        if not cls.PROJECT_ROOT.exists():
            raise ValueError(f"PROJECT_ROOT does not exist: {cls.PROJECT_ROOT}")
        if not cls.PROJECT_ROOT.is_dir():
            raise ValueError(f"PROJECT_ROOT is not a directory: {cls.PROJECT_ROOT}")

    @classmethod
    def reset(cls) -> None:
        """Reset configuration state back to defaults (for test isolation)."""
        cls.PROJECT_ROOT = _UNSET
        cls.HISTORY_DB_PATH = None
        cls.TRACE_LOG_PATH = None
        cls.INDEX_DB_PATH = None
        cls.CONTEXT_FILE_PATH = None
        cls.FILE_READ_REGISTRY.clear()
        cls.EDIT_HISTORY.clear()


if os.environ.get("PROJECT_ROOT"):
    Config.set_project_root(os.environ["PROJECT_ROOT"])
