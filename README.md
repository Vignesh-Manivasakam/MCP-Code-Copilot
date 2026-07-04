# 🤖 Code Copilot MCP Server

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/Framework-FastMCP_v2-green.svg)](https://github.com/modelcontextprotocol/python-sdk)
[![Starlette](https://img.shields.io/badge/Server-Starlette-blue.svg)](https://www.starlette.io/)
[![Uvicorn](https://img.shields.io/badge/ASGI-Uvicorn-blue.svg)](https://www.uvicorn.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

<p align="center">
  <img src="assets/MCP.png" alt="MCP Server Architecture" width="800"/>
  <br>
  <em>Secure Model Context Protocol sandbox controller with file inspection metrics</em>
</p>

An enterprise-ready **Model Context Protocol (MCP)** server that gives LLM assistants (such as Claude Desktop or cursor) secure, read-write access to a single designated project folder. Built with **FastMCP v2** and **Starlette / Uvicorn**, it implements middleware safeguards and path validators to protect against system traversal.

---

## 🏗️ Architecture & Security Model

The server acts as a validated bridge between an external AI client and your filesystem:

```mermaid
graph TD
    Client["AI Client (e.g., Claude Desktop)"] -->|MCP Tool Calls| Server["Starlette MCP Server"]
    Server --> Logger["Logging & CORS Middleware"]
    Logger --> Validator{"Path Sandbox Validator"}
    Validator -->|Security Violation| Reject["Error Response (Rejects '..', absolute paths, symlink escapes)"]
    Validator -->|Safe Path| Tools["10 Tier-1 File Tools"]
    Tools --> Filesystem[("Project Filesystem")]
```

### Sandbox Protections:
* **Relative Path Checks**: Absolute paths are blocked.
* **Directory Traversal Blocking**: Any path components containing `..` are explicitly rejected.
* **Symlink Escaping Protection**: Resolves all symbolic links using `Path.resolve()` and checks that the resolved target path begins with the designated `PROJECT_ROOT` prefix.

---

## 🛠️ Tool Registry

The server registers 10 high-performance tools:
1. `read_file`: Reads files safely with auto-encoding detection via `chardet`.
2. `write_file`: Overwrites files (with optional automatic `.bak` backups).
3. `create_file`: Initializes a new file.
4. `list_files`: Scans directories using glob filters.
5. `get_file_structure`: Generates a nested directory tree with configurable depth.
6. `search_in_files`: Runs regex/text searches.
7. `find_function`: Extracts function and method definitions.
8. `find_references`: Scans for whole-word occurrences of a symbol.
9. `get_file_info`: Retrieves size, encoding, and syntax language.
10. `analyze_file`: Calculates code metrics (lines, classes, functions, imports).

---

## 📁 Repository Directory Structure

```text
MCP-Code-Copilot/
├── middleware/
│   ├── security.py           # Path validation and sandbox rules
│   └── logging.py            # Event logging middleware
├── tools/
│   └── file_operations.py    # All 10 file tools
├── utils/
│   ├── encoding_detector.py  # chardet-based encoding detection
│   ├── file_validator.py     # Helpers (binary checks, size calculations)
│   └── error_handler.py      # Standardised error responses
├── prompts/
│   ├── skills.md             # System instructions for AI client
│   └── image_prompt_MCP_Code_Copilot.md # ChatGPT design prompts
├── config.py                 # Configuration constants
├── requirements.txt          # Python dependencies
└── server.py                 # Starlette web server entry point
```

---

## 🚀 Installation & Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Launch the Server
Start the server and pass the target sandbox folder path:
```bash
python server.py /path/to/your/sandbox/project --port 8000
```
Upon startup, the server console will display:
```text
============================================================
🚀 Code Copilot MCP Server  v1.0.0
============================================================
  Project : my-project
  MCP     : http://127.0.0.1:8000/mcp
  Health  : http://127.0.0.1:8000/health
============================================================
```

### 3. Register with Claude Desktop
Add the server config parameters to your `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "code-copilot": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```
