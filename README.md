# Code Copilot MCP Server

An MCP (Model Context Protocol) server that gives AI assistants secure, read/write access to a single project folder.  Built with **FastMCP v2** and **Starlette / Uvicorn**.

---

## Features

- 10 Tier-1 file-operation tools (read, write, create, list, search, …)
- Hard sandbox – access is restricted to a single project root directory
- Path-traversal protection (blocks `../`, absolute paths, symlink escapes)
- Automatic file-encoding detection via `chardet`
- Binary-file detection
- CORS + request-logging middleware
- `/health` and `/project-info` HTTP endpoints

---

## Project Structure

```
code-copilot-mcp/
├── server.py                 # Entry point
├── config.py                 # Configuration constants
├── requirements.txt
│
├── middleware/
│   ├── security.py           # Path validation
│   └── logging.py            # Request/response logging
│
├── tools/
│   └── file_operations.py    # All 10 file tools
│
├── utils/
│   ├── encoding_detector.py  # chardet-based encoding detection
│   ├── file_validator.py     # Helpers (binary check, size formatting, …)
│   └── error_handler.py      # Standardised error responses
│
└── prompts/
    └── skills.md             # System instructions for the AI client
```

---

## Requirements

- Python 3.10+
- See `requirements.txt` for package dependencies

---

## Quick Start

### 1 – Create and activate a virtual environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 2 – Install dependencies

```bash
pip install -r requirements.txt
```

### 3 – Start the server

```bash
python server.py /path/to/your/project
```

Optional flags:

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `8000` | TCP port |
| `--host` | `127.0.0.1` | Bind address (`0.0.0.0` for all interfaces) |

The server will print:

```
============================================================
🚀 Code Copilot MCP Server  v1.0.0
============================================================
  Project : my-project
  MCP     : http://127.0.0.1:8000/mcp
  Health  : http://127.0.0.1:8000/health
============================================================
```

### 4 – Alternative: set the project root via environment variable

```bash
export PROJECT_ROOT=/path/to/your/project
python server.py .   # argument is still required but ignored when env var is set
```

---

## Available Tools

| # | Tool | Description |
|---|------|-------------|
| 1 | `read_file` | Read file content with encoding detection |
| 2 | `write_file` | Write / overwrite a file (optional `.bak` backup) |
| 3 | `create_file` | Create a new file with optional initial content |
| 4 | `list_files` | List directory contents with glob filtering |
| 5 | `get_file_structure` | Nested project tree up to a configurable depth |
| 6 | `search_in_files` | Full-text search across the project |
| 7 | `find_function` | Locate function / method definitions |
| 8 | `find_references` | Find all usages of a symbol (whole-word) |
| 9 | `get_file_info` | Detailed file metadata (size, encoding, language, …) |
| 10 | `analyze_file` | Code metrics: line counts, functions, classes, imports |

---

## Configuration

All tuneable constants live in `config.py`.  The most important ones:

| Constant | Default | Purpose |
|----------|---------|---------|
| `FILE_SIZE_WARNING_THRESHOLD` | 1 MB | Warn when reading large files |
| `FILE_SIZE_MAX_READ` | 50 MB | Hard limit for `read_file` |
| `MAX_WRITE_SIZE` | 10 MB | Hard limit for `write_file` |
| `MAX_SEARCH_RESULTS` | 100 | Upper bound on search results |
| `SEARCH_TIMEOUT_SECONDS` | 10 | Abort search after this many seconds |
| `LOG_LEVEL` | `INFO` (env: `LOG_LEVEL`) | Python logging level |

---

## Security Model

The server enforces a **strict single-folder sandbox**:

1. All paths supplied to any tool are validated before any filesystem access.
2. Absolute paths are rejected outright.
3. Paths containing `..` components are rejected before resolution.
4. After `Path.resolve()` (which follows symlinks), the resolved path must start with `PROJECT_ROOT`.  Any path that escapes the sandbox is rejected with a `SECURITY_VIOLATION` error.

---

## Endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/health` | GET | Plain-text health check |
| `/` | GET | Alias for `/health` |
| `/project-info` | GET | JSON project statistics |
| `/mcp` | GET + POST | MCP protocol endpoint |

---

## Running Tests

```bash
pytest
```

(Test files go in a `tests/` directory – see the Phase 1 testing checklist for manual test cases.)

---

## MCP Client Configuration

To connect an MCP client (e.g. Claude Desktop, or a VS Code extension) point it at the `/mcp` endpoint:

```json
{
  "mcpServers": {
    "code-copilot": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

Attach `prompts/skills.md` as the system prompt / instructions file so the AI client knows how to use the available tools effectively.

---

## Licence

MIT
