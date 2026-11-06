"""
History Module for Code Copilot.
================================
SQLite-backed reverse-patch undo history.
Enforces zero .bak files, 20-entry LRU ring buffer, and external conflict detection.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Generator, Optional, Tuple

from config import Config

logger = logging.getLogger(__name__)


class HistoryDB:
    """Manages SQLite undo history with reverse patches and conflict checking."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        max_entries: Optional[int] = None,
    ) -> None:
        self._custom_db_path = Path(db_path).resolve() if db_path is not None else None
        self._custom_max_entries = max_entries
        self._ensure_db()

    @property
    def db_path(self) -> Path:
        """Dynamically resolve database path to respect dynamic Config.PROJECT_ROOT."""
        if self._custom_db_path is not None:
            return self._custom_db_path
        if Config.HISTORY_DB_PATH is not None:
            return Path(Config.HISTORY_DB_PATH).resolve()
        base = Config.PROJECT_ROOT or Path.cwd()
        return (Path(base) / ".copilot" / "history.db").resolve()

    @property
    def max_entries(self) -> int:
        """Maximum number of entries in the LRU ring buffer."""
        if self._custom_max_entries is not None:
            return self._custom_max_entries
        return getattr(Config, "MAX_HISTORY_ENTRIES", 20)

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_db(self) -> None:
        """Create edits table, indices, and compatibility view if they do not exist."""
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS edits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT COLLATE NOCASE NOT NULL,
                    pre_edit_content TEXT NOT NULL,
                    post_edit_content TEXT NOT NULL,
                    pre_edit_hash TEXT NOT NULL,
                    post_edit_hash TEXT NOT NULL,
                    patch_summary TEXT DEFAULT '',
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_edits_file_path ON edits(file_path COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_edits_timestamp ON edits(timestamp DESC);

                CREATE VIEW IF NOT EXISTS edit_history AS SELECT * FROM edits;
                """
            )

    @staticmethod
    def _compute_hash(content: str) -> str:
        """Compute SHA-256 hash of text content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def record_edit(
        self,
        file_path: str | Path,
        pre_edit_content: str,
        post_edit_content: str,
        patch_summary: str = "",
    ) -> str:
        """
        Record an edit, compute pre/post SHA-256 hashes, save reverse patch,
        and prune entries exceeding max_entries (20).

        Returns:
            The edit ID as a string.
        """
        norm_path = Path(file_path).as_posix()
        pre_hash = self._compute_hash(pre_edit_content)
        post_hash = self._compute_hash(post_edit_content)

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO edits (
                    file_path, pre_edit_content, post_edit_content,
                    pre_edit_hash, post_edit_hash, patch_summary
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    norm_path,
                    pre_edit_content,
                    post_edit_content,
                    pre_hash,
                    post_hash,
                    patch_summary,
                ),
            )
            edit_id = cursor.lastrowid

            # Ring buffer eviction: prune oldest entries beyond max_entries
            conn.execute(
                """
                DELETE FROM edits WHERE id NOT IN (
                    SELECT id FROM edits ORDER BY id DESC LIMIT ?
                )
                """,
                (self.max_entries,),
            )

        return str(edit_id)

    def undo(
        self,
        file_path: Optional[str | Path] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Retrieve most recent edit (or most recent for a specific file).
        Validate current file hash against post_edit_hash for conflict detection.
        Restore pre_edit_content atomically to disk with zero .bak files.
        Remove restored entry from history upon success.

        Returns:
            (success: bool, message: str, restored_file_path: str | None)
        """
        with self._get_connection() as conn:
            if file_path:
                norm_path = Path(file_path).as_posix()
                row = conn.execute(
                    """
                    SELECT * FROM edits
                    WHERE file_path = ? COLLATE NOCASE
                    ORDER BY id DESC LIMIT 1
                    """,
                    (norm_path,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM edits ORDER BY id DESC LIMIT 1"
                ).fetchone()

            if not row:
                return False, "No edits available to undo", None

            record = dict(row)

        record_file_path = record["file_path"]
        target = Path(record_file_path)
        if not target.is_absolute():
            base_root = Config.PROJECT_ROOT or Path.cwd()
            target = (Path(base_root) / record_file_path).resolve()

        # External conflict detection
        if target.exists():
            try:
                with open(target, "r", encoding="utf-8", newline="", errors="replace") as f:
                    current_content = f.read()
                current_hash = self._compute_hash(current_content)
            except OSError as err:
                return False, f"Error reading file for conflict check: {err}", record_file_path

            if current_hash != record["post_edit_hash"]:
                # Check if mismatch is solely due to CRLF vs LF line ending translation
                curr_norm = current_content.replace("\r\n", "\n")
                post_norm = record["post_edit_content"].replace("\r\n", "\n")
                if curr_norm != post_norm:
                    return (
                        False,
                        f"External edit conflict detected: {record_file_path} has been modified outside copilot",
                        record_file_path,
                    )
        else:
            # File does not exist on disk
            if record["post_edit_content"] != "":
                return (
                    False,
                    f"External edit conflict detected: {record_file_path} was deleted externally",
                    record_file_path,
                )

        # Atomic restoration to disk
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if record["pre_edit_content"] == "" and record.get("patch_summary") == "create_file":
                # Undoing file creation -> remove the file
                if target.exists():
                    target.unlink()
            else:
                temp_target = target.with_name(f".tmp_undo_{target.name}")
                with open(temp_target, "w", encoding="utf-8", newline="") as f:
                    f.write(record["pre_edit_content"])
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_target, target)
        except OSError as err:
            return False, f"Failed to restore file on disk: {err}", record_file_path
        finally:
            temp_target = target.with_name(f".tmp_undo_{target.name}")
            if temp_target.exists():
                try:
                    temp_target.unlink()
                except OSError:
                    pass

        # Remove restored entry from history upon success
        with self._get_connection() as conn:
            conn.execute("DELETE FROM edits WHERE id = ?", (record["id"],))

        return True, f"Successfully restored {record_file_path}", record_file_path

    def get_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Retrieve recent edit records ordered by id DESC."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, file_path, pre_edit_content, post_edit_content,
                       pre_edit_hash, post_edit_hash, patch_summary, timestamp
                FROM edits
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def count(self) -> int:
        """Return the number of active history records."""
        with self._get_connection() as conn:
            res = conn.execute("SELECT COUNT(*) FROM edits").fetchone()
            return int(res[0]) if res else 0

    def clear(self) -> None:
        """Clear all history records."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM edits")


__all__ = ["HistoryDB"]
