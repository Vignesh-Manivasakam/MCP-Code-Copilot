import copy
import logging
import time
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class GatingState:
    DISCOVERY = "DISCOVERY"
    INSPECT = "INSPECT"
    EDIT = "EDIT"
    TESTING = "TESTING"

class GatingRegistry:
    def __init__(self):
        self.state = GatingState.DISCOVERY
        self.last_activity = time.time()
        self.timeout = 300.0  # 5 minutes idle timeout
        self.original_schemas: Dict[str, Dict[str, Any]] = {}
        
    def check_timeout(self) -> None:
        """Reset state to DISCOVERY if idle timeout has expired."""
        now = time.time()
        if now - self.last_activity > self.timeout:
            if self.state != GatingState.DISCOVERY:
                logger.info("Tool gating idle timeout expired. Resetting state to DISCOVERY.")
                self.state = GatingState.DISCOVERY
        self.last_activity = now

    def transition(self, tool_name: str) -> None:
        """Progress state based on the current tool execution."""
        self.check_timeout()
        
        previous = self.state
        
        # Category transitions
        if tool_name in ("list_files", "get_file_structure", "search_in_files", "grep_search", "search_codebase"):
            if self.state == GatingState.DISCOVERY:
                self.state = GatingState.INSPECT
                
        elif tool_name in ("read_file", "batch_read_files", "analyze_file"):
            if self.state in (GatingState.DISCOVERY, GatingState.INSPECT):
                self.state = GatingState.EDIT
                
        elif tool_name in ("modify_file", "write_file", "create_file"):
            self.state = GatingState.TESTING
            
        elif tool_name in ("execute_command", "set_project_root"):
            self.state = GatingState.DISCOVERY

        if self.state != previous:
            logger.info("Gating state transitioned: %s -> %s (triggered by '%s')", previous, self.state, tool_name)

    def get_tool_gating_status(self, tool_name: str) -> str:
        """Returns whether a tool is 'VISIBLE', 'LAZY', or 'HIDDEN' based on current state."""
        self.check_timeout()
        
        # When no project is configured, only allow project setup tools
        from config import Config
        if Config.PROJECT_ROOT is None:
            if tool_name in ("set_project_root", "get_tool_schema"):
                return "VISIBLE"
            return "HIDDEN"
        
        # Always visible system tools
        if tool_name in ("get_tool_schema", "set_project_root", "get_file_info"):
            return "VISIBLE"
            
        # State definitions
        if self.state == GatingState.DISCOVERY:
            # Only search & metadata tools are visible
            if tool_name in (
                "list_files", "get_file_structure", "search_in_files", "grep_search", 
                "find_function", "find_references", "search_codebase", 
                "lsp_find_definition", "lsp_find_references"
            ):
                return "VISIBLE"
            # Read file is visible but lazy loaded
            if tool_name == "read_file":
                return "LAZY"
            return "HIDDEN"
            
        elif self.state == GatingState.INSPECT:
            # Search tools + reading tools
            if tool_name in (
                "list_files", "get_file_structure", "search_in_files", "grep_search", 
                "find_function", "find_references", "search_codebase",
                "lsp_find_definition", "lsp_find_references", "read_file"
            ):
                return "VISIBLE"
            if tool_name in ("batch_read_files", "analyze_file"):
                return "LAZY"
            return "HIDDEN"
            
        elif self.state == GatingState.EDIT:
            # Search + read + edit tools
            if tool_name in (
                "list_files", "get_file_structure", "search_in_files", "grep_search", 
                "find_function", "find_references", "search_codebase",
                "lsp_find_definition", "lsp_find_references", "read_file", 
                "batch_read_files", "analyze_file"
            ):
                return "VISIBLE"
            if tool_name in ("modify_file", "write_file", "create_file", "undo_edit", "get_diagnostics"):
                return "LAZY"
            return "HIDDEN"
            
        elif self.state == GatingState.TESTING:
            # Everything unlocked
            if tool_name == "execute_command":
                return "LAZY"
            return "VISIBLE"
            
        return "VISIBLE"

# Global singleton registry
gating_registry = GatingRegistry()

def setup_gating_middleware(mcp) -> None:
    """Monkey-patch FastMCP's tool list and call execution to prune schemas and track states."""
    original_list_tools = mcp.list_tools
    
    # 1. Patch list_tools
    async def gated_list_tools(*args, **kwargs):
        tools = await original_list_tools(*args, **kwargs)
        
        # Populate original schema registry once
        for t in tools:
            if t.name not in gating_registry.original_schemas:
                gating_registry.original_schemas[t.name] = copy.deepcopy(t.parameters)
                
        gated_tools = []
        for t in tools:
            status = gating_registry.get_tool_gating_status(t.name)
            if status == "HIDDEN":
                continue
                
            t_gated = copy.copy(t)
            if status == "LAZY":
                # Strip parameters to lazy load them
                t_gated.parameters = {
                    "type": "object",
                    "properties": {
                        "_lazy_schema_required": {
                            "type": "boolean",
                            "description": (
                                f"To invoke {t.name}, you MUST first fetch its arguments via "
                                f"get_tool_schema(tool_name='{t.name}')."
                            )
                        }
                    },
                    "required": ["_lazy_schema_required"]
                }
                t_gated.description = (
                    f"[LAZY SCHEMA - call get_tool_schema('{t.name}') first] {t.description or ''}"
                )
            
            gated_tools.append(t_gated)
            
        return gated_tools

    mcp.list_tools = gated_list_tools

    # 2. Patch call_tool to intercept tool calls for state transitions
    original_call_tool = mcp.call_tool
    async def gated_call_tool(name: str, *args, **kwargs):
        gating_registry.transition(name)
        return await original_call_tool(name, *args, **kwargs)
        
    mcp.call_tool = gated_call_tool
