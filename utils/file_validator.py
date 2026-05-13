"""
File validation utilities for Code Copilot MCP Server
"""
from pathlib import Path

from config import Config


def is_text_file(file_path: Path) -> bool:
    """
    Determine whether a file contains text (not binary data).

    Reads the first 8 KB and checks for null bytes, which are a reliable
    indicator of binary content in most text encodings.

    Args:
        file_path: Absolute path to the file to inspect.

    Returns:
        ``True`` if the file appears to be a text file.
    """
    try:
        with open(file_path, "rb") as fh:
            chunk = fh.read(8_192)
        return b"\x00" not in chunk
    except OSError:
        return False


def get_file_size_human(size_bytes: int) -> str:
    """
    Convert a byte count to a human-readable string.

    Examples::

        get_file_size_human(1024)      -> "1.0 KB"
        get_file_size_human(1048576)   -> "1.0 MB"

    Args:
        size_bytes: File size in bytes.

    Returns:
        Formatted string with appropriate unit.
    """
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def should_warn_file_size(size_bytes: int) -> bool:
    """Return ``True`` if the file size exceeds the warning threshold."""
    return size_bytes > Config.FILE_SIZE_WARNING_THRESHOLD


# Mapping of file extensions to human-readable language names
_EXTENSION_MAP = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".jsx": "JavaScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".c": "C",
    ".h": "C/C++ Header",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".cs": "C#",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".json": "JSON",
    ".xml": "XML",
    ".md": "Markdown",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".ini": "INI",
    ".sh": "Shell",
    ".bat": "Batch",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".r": "R",
    ".scala": "Scala",
    ".dart": "Dart",
    ".lua": "Lua",
    ".pl": "Perl",
    ".vue": "Vue",
    ".svelte": "Svelte",
}


def detect_language_from_extension(file_path: Path) -> str:
    """
    Infer the programming/markup language from a file's extension.

    Args:
        file_path: Path whose suffix is inspected.

    Returns:
        Language name string, or ``"Unknown"`` if the extension is not mapped.
    """
    return _EXTENSION_MAP.get(file_path.suffix.lower(), "Unknown")
