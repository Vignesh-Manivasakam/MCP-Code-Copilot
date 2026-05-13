"""
Configuration for Code Copilot MCP Server
"""
import os
from pathlib import Path
from typing import Optional


class Config:
    """Server configuration"""

    # Server Settings
    SERVER_NAME = "Code Copilot MCP Server"
    SERVER_VERSION = "1.0.0"
    DEFAULT_PORT = 8001

    # Project Root - MUST BE SET before starting the server
    PROJECT_ROOT: Optional[Path] = None

    # File Size Limits (in bytes)
    FILE_SIZE_WARNING_THRESHOLD = 1 * 1024 * 1024   # 1 MB  – warn user
    FILE_SIZE_CHUNK_THRESHOLD = 5 * 1024 * 1024     # 5 MB  – auto chunk
    FILE_SIZE_MAX_READ = 50 * 1024 * 1024            # 50 MB – reject read
    MAX_WRITE_SIZE = 10 * 1024 * 1024               # 10 MB – reject write

    # Search Limits
    MAX_SEARCH_RESULTS = 100
    SEARCH_TIMEOUT_SECONDS = 10

    # Encoding
    DEFAULT_ENCODING = "utf-8"
    FALLBACK_ENCODINGS = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]

    # Logging
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    ENABLE_REQUEST_LOGGING = True

    @classmethod
    def set_project_root(cls, path: str) -> None:
        """Set and validate project root path."""
        project_path = Path(path).resolve()

        if not project_path.exists():
            raise ValueError(f"Project path does not exist: {project_path}")

        if not project_path.is_dir():
            raise ValueError(f"Project path is not a directory: {project_path}")

        cls.PROJECT_ROOT = project_path
        print(f"✅ Project root set to: {cls.PROJECT_ROOT}")

    @classmethod
    def validate_config(cls) -> None:
        """Validate required configuration before server start."""
        if cls.PROJECT_ROOT is None:
            raise RuntimeError("PROJECT_ROOT must be set before starting server")


# Auto-initialise from environment variable when present
if os.environ.get("PROJECT_ROOT"):
    Config.set_project_root(os.environ["PROJECT_ROOT"])
