import pytest
import time
from fastmcp import FastMCP
from middleware.gating import gating_registry, setup_gating_middleware, GatingState

@pytest.fixture(autouse=True)
def reset_gating():
    gating_registry.reset_state = lambda: setattr(gating_registry, "state", GatingState.DISCOVERY)
    gating_registry.state = GatingState.DISCOVERY
    gating_registry.last_activity = time.time()
    yield

@pytest.mark.asyncio
async def test_tool_gating_initial_state():
    mcp = FastMCP("test")

    @mcp.tool()
    def grep_search(query: str) -> str:
        return f"Searching for {query}"

    @mcp.tool()
    def read_file(file_path: str) -> str:
        return f"Reading {file_path}"

    @mcp.tool()
    def modify_file(file_path: str, target: str, replacement: str) -> str:
        return f"Modified {file_path}"

    @mcp.tool()
    def execute_command(command: str) -> str:
        return f"Executed {command}"

    setup_gating_middleware(mcp)
    
    # 1. In DISCOVERY state: grep_search is visible, read_file is lazy, modify_file is hidden
    tools = await mcp.list_tools()
    tool_names = [t.name for t in tools]
    
    assert "grep_search" in tool_names
    assert "read_file" in tool_names
    assert "modify_file" not in tool_names
    assert "execute_command" not in tool_names
    
    # Verify read_file is lazy-loaded (its schema is stripped to a single dummy argument)
    read_tool = next(t for t in tools if t.name == "read_file")
    assert "_lazy_schema_required" in read_tool.parameters["properties"]

@pytest.mark.asyncio
async def test_tool_gating_transitions():
    mcp = FastMCP("test")

    @mcp.tool()
    def grep_search(query: str) -> str:
        return f"Searching for {query}"

    @mcp.tool()
    def read_file(file_path: str) -> str:
        return f"Reading {file_path}"

    @mcp.tool()
    def modify_file(file_path: str, target: str, replacement: str) -> str:
        return f"Modified {file_path}"

    @mcp.tool()
    def execute_command(command: str) -> str:
        return f"Executed {command}"

    setup_gating_middleware(mcp)
    
    # Populate the original schemas cache by listing once
    await mcp.list_tools()
    
    assert gating_registry.state == GatingState.DISCOVERY
    
    # Execute a search tool -> transitions to INSPECT
    await mcp.call_tool("grep_search", {"query": "test"})
    assert gating_registry.state == GatingState.INSPECT
    
    # Under INSPECT, read_file becomes fully visible (non-lazy)
    tools = await mcp.list_tools()
    read_tool = next(t for t in tools if t.name == "read_file")
    assert "file_path" in read_tool.parameters["properties"]
    assert "_lazy_schema_required" not in read_tool.parameters["properties"]
    
    # Execute a read tool -> transitions to EDIT
    await mcp.call_tool("read_file", {"file_path": "server.py"})
    assert gating_registry.state == GatingState.EDIT
    
    # Under EDIT, modify_file is visible but lazy
    tools = await mcp.list_tools()
    tool_names = [t.name for t in tools]
    assert "modify_file" in tool_names
    modify_tool = next(t for t in tools if t.name == "modify_file")
    assert "_lazy_schema_required" in modify_tool.parameters["properties"]
    
    # Execute modify_file -> transitions to TESTING
    await mcp.call_tool("modify_file", {"file_path": "server.py", "target": "a", "replacement": "b"})
    assert gating_registry.state == GatingState.TESTING
    
    # Under TESTING, execute_command is visible but lazy
    tools = await mcp.list_tools()
    tool_names = [t.name for t in tools]
    assert "execute_command" in tool_names
    exec_tool = next(t for t in tools if t.name == "execute_command")
    assert "_lazy_schema_required" in exec_tool.parameters["properties"]
    
    # Execute command -> transitions back to DISCOVERY
    await mcp.call_tool("execute_command", {"command": "pytest"})
    assert gating_registry.state == GatingState.DISCOVERY
