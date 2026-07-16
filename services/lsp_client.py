import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

class LSPClient:
    def __init__(self, project_root: Path, language: str = "python"):
        self.project_root = project_root
        self.language = language.lower()
        self.process: Optional[subprocess.Popen] = None
        self.request_id = 1
        self.initialized = False
        
    def _find_server_command(self) -> Optional[List[str]]:
        if self.language == "python":
            # Check pyright-langserver or pyright
            cmd = shutil.which("pyright-langserver")
            if cmd:
                return [cmd, "--stdio"]
            cmd = shutil.which("pyright")
            if cmd:
                return [cmd, "--stdio"]
        elif self.language in ("javascript", "typescript"):
            cmd = shutil.which("typescript-language-server")
            if cmd:
                return [cmd, "--stdio"]
        return None

    def start(self) -> bool:
        """Spawn the language server daemon process."""
        cmd = self._find_server_command()
        if not cmd:
            logger.warning("No LSP server executable found for language: %s", self.language)
            return False
            
        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(self.project_root),
                bufsize=0
            )
            logger.info("Spawned LSP daemon for %s: %s", self.language, cmd)
            self._initialize_handshake()
            return True
        except Exception as e:
            logger.error("Failed to start LSP process: %s", e)
            return False

    def _send_message(self, method: str, params: Dict[str, Any], is_request: bool = True) -> Optional[int]:
        if not self.process or not self.process.stdin:
            return None
            
        msg: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        
        req_id = None
        if is_request:
            req_id = self.request_id
            msg["id"] = req_id
            self.request_id += 1
            
        body = json.dumps(msg)
        payload = f"Content-Length: {len(body)}\r\n\r\n{body}"
        
        try:
            self.process.stdin.write(payload.encode("utf-8"))
            self.process.stdin.flush()
            return req_id
        except Exception as e:
            logger.error("Error writing to LSP stdin: %s", e)
            return None

    def _read_message(self) -> Optional[Dict[str, Any]]:
        if not self.process or not self.process.stdout:
            return None
            
        try:
            # Read header
            line = self.process.stdout.readline().decode("utf-8")
            if not line:
                return None
                
            content_length = 0
            if "Content-Length:" in line:
                content_length = int(line.split(":")[1].strip())
                # Read until empty line delimiter
                while True:
                    delim = self.process.stdout.readline().decode("utf-8")
                    if delim == "\r\n" or delim == "\n" or not delim:
                        break
                        
            if content_length > 0:
                body = self.process.stdout.read(content_length).decode("utf-8")
                return json.loads(body)
        except Exception as e:
            logger.error("Error reading from LSP stdout: %s", e)
            
        return None

    def _initialize_handshake(self) -> None:
        """Perform LSP handshake."""
        params = {
            "processId": None,
            "rootPath": str(self.project_root),
            "rootUri": self.project_root.as_uri(),
            "capabilities": {},
            "initializationOptions": {}
        }
        
        req_id = self._send_message("initialize", params)
        if req_id is not None:
            # Wait for response
            response = self._read_message()
            if response and response.get("id") == req_id:
                # Send initialized notification
                self._send_message("initialized", {}, is_request=False)
                self.initialized = True
                logger.info("LSP initialization handshake complete.")

    def find_definition(self, rel_file_path: str, line: int, character: int) -> List[Dict[str, Any]]:
        """Query textDocument/definition for target position."""
        if not self.initialized:
            return []
            
        abs_uri = (self.project_root / rel_file_path).as_uri()
        params = {
            "textDocument": {"uri": abs_uri},
            "position": {"line": line - 1, "character": character}
        }
        
        req_id = self._send_message("textDocument/definition", params)
        if req_id is None:
            return []
            
        # Read messages until we find response matching the request id
        for _ in range(20):  # Safety loop limit
            response = self._read_message()
            if not response:
                break
            if response.get("id") == req_id:
                result = response.get("result")
                return self._parse_locations(result)
                
        return []

    def find_references(self, rel_file_path: str, line: int, character: int) -> List[Dict[str, Any]]:
        """Query textDocument/references for target position."""
        if not self.initialized:
            return []
            
        abs_uri = (self.project_root / rel_file_path).as_uri()
        params = {
            "textDocument": {"uri": abs_uri},
            "position": {"line": line - 1, "character": character},
            "context": {"includeDeclaration": True}
        }
        
        req_id = self._send_message("textDocument/references", params)
        if req_id is None:
            return []
            
        for _ in range(20):
            response = self._read_message()
            if not response:
                break
            if response.get("id") == req_id:
                result = response.get("result")
                return self._parse_locations(result)
                
        return []

    def _parse_locations(self, result: Any) -> List[Dict[str, Any]]:
        """Normalize response Location | Location[] | LocationLink[] list."""
        if not result:
            return []
            
        locations = []
        if isinstance(result, dict):
            locations = [result]
        elif isinstance(result, list):
            locations = result
            
        parsed = []
        for loc in locations:
            # Handle Location vs LocationLink structures
            uri = loc.get("uri") or loc.get("targetUri")
            range_obj = loc.get("range") or loc.get("targetSelectionRange")
            
            if uri and range_obj:
                try:
                    # Convert file URI back to relative path
                    file_path = Path(uri.replace("file:///", "").replace("file://", ""))
                    if file_path.is_relative_to(self.project_root):
                        rel_path = file_path.relative_to(self.project_root).as_posix()
                    else:
                        rel_path = file_path.as_posix()
                except Exception:
                    rel_path = uri
                    
                start = range_obj.get("start", {})
                end = range_obj.get("end", {})
                parsed.append({
                    "file_path": rel_path,
                    "start_line": start.get("line", 0) + 1,
                    "start_character": start.get("character", 0),
                    "end_line": end.get("line", 0) + 1,
                    "end_character": end.get("character", 0)
                })
        return parsed

    def stop(self) -> None:
        """Terminate the subprocess."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
            self.initialized = False
