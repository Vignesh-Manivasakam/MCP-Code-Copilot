import sqlite3
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from utils.file_validator import walk_safe_paths, is_text_file
from utils.encoding_detector import detect_encoding

logger = logging.getLogger(__name__)

DB_DIR_NAME = ".mcp_index"
DB_FILE_NAME = "code_index.db"

def get_db_path(project_root: Path) -> Path:
    return project_root / DB_DIR_NAME / DB_FILE_NAME

def initialize_index(project_root: Path) -> None:
    """Create the SQLite FTS5 database and tables if they don't exist."""
    db_dir = project_root / DB_DIR_NAME
    db_dir.mkdir(exist_ok=True)
    db_path = get_db_path(project_root)
    
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        # Create virtual table using FTS5 for fast code search
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS code_search USING fts5(
                file_path UNINDEXED,
                content,
                tokenize="unicode61"
            );
        """)
        conn.commit()
    except Exception as e:
        logger.error("Failed to initialize FTS5 SQLite database: %s", e)
        raise e
    finally:
        conn.close()

def index_file(project_root: Path, file_path: Path) -> bool:
    """Read and index a single file into the FTS5 table."""
    db_path = get_db_path(project_root)
    if not db_path.exists():
        initialize_index(project_root)
        
    # Check if binary or outside sandbox
    rel_path = file_path.relative_to(project_root).as_posix()
    if not is_text_file(file_path):
        return False
        
    try:
        encoding = detect_encoding(file_path)
        content = file_path.read_text(encoding=encoding, errors="replace")
    except Exception as e:
        logger.debug("Failed to read file %s for indexing: %s", file_path, e)
        return False
        
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        # Delete old index if exists
        cursor.execute("DELETE FROM code_search WHERE file_path = ?;", (rel_path,))
        # Insert new content
        cursor.execute(
            "INSERT INTO code_search (file_path, content) VALUES (?, ?);",
            (rel_path, content)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("Failed to index file %s: %s", rel_path, e)
        return False
    finally:
        conn.close()

def index_all_files(project_root: Path) -> int:
    """Walk and index all text files in the project root."""
    initialize_index(project_root)
    indexed_count = 0
    
    for file_path in walk_safe_paths(project_root):
        if file_path.is_file():
            # Skip indexing the database file itself
            if DB_DIR_NAME in file_path.parts:
                continue
            if index_file(project_root, file_path):
                indexed_count += 1
                
    logger.info("Indexed %d files in project root: %s", indexed_count, project_root)
    return indexed_count

def search_code(project_root: Path, query_str: str, limit: int = 25) -> List[Dict[str, Any]]:
    """Search for keywords in the indexed code and return matching files and snippets."""
    db_path = get_db_path(project_root)
    if not db_path.exists():
        initialize_index(project_root)
        index_all_files(project_root)
        
    conn = sqlite3.connect(str(db_path))
    results = []
    try:
        cursor = conn.cursor()
        # Search query using MATCH and highlight/snippet
        # FTS5 MATCH searches for terms. Escape special characters or wrap in double quotes
        # We clean the query_str to prevent syntax issues in FTS5 queries
        clean_query = query_str.replace("'", " ").replace('"', ' ').strip()
        if not clean_query:
            return []
            
        # We can construct a simple prefix search to make it dynamic
        match_query = " OR ".join([f'"{q}*"' for q in clean_query.split() if q])
        
        cursor.execute(
            """
            SELECT 
                file_path, 
                snippet(code_search, 1, '<<<', '>>>', '...', 64) as match_snippet
            FROM code_search 
            WHERE code_search MATCH ? 
            LIMIT ?;
            """,
            (match_query, limit)
        )
        
        for row in cursor.fetchall():
            results.append({
                "file_path": row[0],
                "snippet": row[1]
            })
    except Exception as e:
        logger.error("Failed search query for '%s': %s", query_str, e)
    finally:
        conn.close()
        
    return results
