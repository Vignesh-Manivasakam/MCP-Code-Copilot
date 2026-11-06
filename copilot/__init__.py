"""FastMCP Code Copilot.

A production-grade coding assistant server supporting Anthropic, Gemini, and OpenAI
enterprise chatbot interfaces with token-optimized file editing, AST inspection,
and sandboxed execution.
"""

from typing import Final

__title__: Final[str] = "copilot"
__version__: Final[str] = "0.2.0"
__author__: Final[str] = "Vignesh Manivasakam"
__license__: Final[str] = "MIT"
__description__: Final[str] = (
    "Production-grade FastMCP Code Copilot assistant with poly-tool architecture"
)

__all__ = [
    "__title__",
    "__version__",
    "__author__",
    "__license__",
    "__description__",
    "get_version",
]


def get_version() -> str:
    """Return the package version string."""
    return __version__
