"""
File encoding detection utilities for Code Copilot MCP Server
"""
import logging
from pathlib import Path
from typing import Tuple

import chardet

from config import Config

logger = logging.getLogger(__name__)


def detect_encoding(file_path: Path) -> str:
    """
    Detect the character encoding of a file using chardet.

    Reads the first 10 KB of the file; if chardet confidence is ≥ 0.7 the
    detected encoding is returned directly.  Otherwise the function tries each
    of Config.FALLBACK_ENCODINGS in order and returns the first one that can
    decode the sample without errors.  Falls back to Config.DEFAULT_ENCODING
    when nothing works.

    Args:
        file_path: Absolute path to the file to inspect.

    Returns:
        An encoding string suitable for use in ``open()``.
    """
    try:
        with open(file_path, "rb") as fh:
            raw = fh.read(10_000)

        result = chardet.detect(raw)
        detected = result.get("encoding")
        confidence = result.get("confidence", 0.0)

        if detected and confidence >= 0.7:
            return detected

        logger.debug(
            "Low confidence encoding detection (%.2f) for %s – trying fallbacks",
            confidence,
            file_path,
        )

        for enc in Config.FALLBACK_ENCODINGS:
            try:
                raw.decode(enc)
                return enc
            except (UnicodeDecodeError, LookupError):
                continue

    except OSError as exc:
        logger.warning("Cannot read file for encoding detection (%s): %s", file_path, exc)

    return Config.DEFAULT_ENCODING


def read_file_with_encoding(file_path: Path) -> Tuple[str, str]:
    """
    Read a text file attempting automatic encoding detection.

    Tries the detected encoding first, then each fallback encoding, and
    finally reads with UTF-8 and ``errors='replace'`` as a last resort so
    callers always receive a string.

    Args:
        file_path: Absolute path to the file to read.

    Returns:
        ``(content, encoding_used)`` – a tuple of the file text and the
        encoding that successfully decoded it.
    """
    primary = detect_encoding(file_path)
    candidates = [primary] + [e for e in Config.FALLBACK_ENCODINGS if e != primary]

    for enc in candidates:
        try:
            with open(file_path, "r", encoding=enc, errors="strict") as fh:
                content = fh.read()
            return content, enc
        except (UnicodeDecodeError, LookupError):
            continue

    # Last resort: replace undecodable bytes rather than raising
    with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    logger.warning("Read %s with replacement characters (all encodings failed)", file_path)
    return content, "utf-8"
