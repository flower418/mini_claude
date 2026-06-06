# ── Minimal MCP client (JSON-RPC over stdio) ────────────
# Connects to MCP servers, discovers their tools, wraps them for the agent.
import json
import subprocess
import threading
import time
import uuid
from queue import Queue, Empty


class MCPServer:
    """One MCP server connection. Handles JSON-RPC lifecycle."""

    def __init__(self, name: str, command: str, args: list[str] | None = None):
        self.name = name
        self._process: subprocess.Popen | None = None
        self._command = command
        self._args = args or []
        self._request_id = 0
        self._pending: dict[int, Queue] = {}
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self.tools: list[dict] = []  # MCP tool schemas
        self._running = False

    def start(self):
        """Spawn the MCP server process and initialize handshake."""
        self._process = subprocess.Popen(
            [self._command] + self._args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        # MCP initialize handshake
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mini_claude", "version": "1.0"},
        })
        if result:
            # Send initialized notification
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            # Discover tools
            tools_result = self._request("tools/list", {})
            if tools_result and "tools" in tools_result:
                self.tools = tools_result["tools"]
                print(f"\033[90m[mcp:{self.name}] {len(self.tools)} tools discovered\033[0m")

    def stop(self):
        self._running = False
        if self._process:
            self._process.terminate()

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Invoke an MCP tool and return its text result."""
        result = self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if result is None:
            return f"Error: MCP tool '{tool_name}' call failed (no response)"
        if "content" in result:
            texts = [c["text"] for c in result["content"] if c.get("type") == "text"]
            return "\n".join(texts) or str(result["content"])
        return json.dumps(result)

    def _request(self, method: str, params: dict, timeout: float = 30) -> dict | None:
        """Send a JSON-RPC request and wait for response."""
        with self._lock:
            rid = self._request_id
            self._request_id += 1
            q: Queue = Queue()
            self._pending[rid] = q
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return q.get(timeout=timeout)
        except Empty:
            return None
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    def _send(self, msg: dict):
        if self._process and self._process.stdin:
            try:
                self._process.stdin.write(json.dumps(msg) + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def _read_loop(self):
        """Read JSON-RPC responses from server stdout."""
        while self._running and self._process and self._process.stdout:
            try:
                line = self._process.stdout.readline()
                if not line:
                    break
                msg = json.loads(line)
                rid = msg.get("id")
                if rid is not None:
                    with self._lock:
                        q = self._pending.get(rid)
                    if q:
                        q.put(msg.get("result") or msg.get("error"))
            except (json.JSONDecodeError, Exception):
                continue


# ── Registry ─────────────────────────────────────────────

_servers: dict[str, MCPServer] = {}
_mcp_tools_cache: list[dict] = []  # Anthropic-format tools from MCP servers


def _mcp_to_anthropic(mcp_tool: dict) -> dict:
    """Convert MCP tool schema to Anthropic tool schema."""
    props = {}
    required = []
    for name, schema in mcp_tool.get("inputSchema", {}).get("properties", {}).items():
        prop = {"type": schema.get("type", "string")}
        if "description" in schema:
            prop["description"] = schema["description"]
        if "enum" in schema:
            prop["enum"] = schema["enum"]
        props[name] = prop
    if mcp_tool.get("inputSchema", {}).get("required"):
        required = mcp_tool["inputSchema"]["required"]
    return {
        "name": f"mcp_{mcp_tool['name']}",
        "description": f"[MCP] {mcp_tool.get('description', '')}",
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


def connect_server(name: str, command: str, args: list[str] | None = None) -> str:
    """Connect to an MCP server, discover its tools."""
    if name in _servers:
        return f"Error: MCP server '{name}' already connected"
    server = MCPServer(name, command, args or [])
    try:
        server.start()
    except Exception as e:
        return f"Error connecting to MCP server '{name}': {e}"
    _servers[name] = server
    _rebuild_tools()
    return f"Connected to MCP server '{name}' ({len(server.tools)} tools)"


def disconnect_server(name: str) -> str:
    if name not in _servers:
        return f"Error: MCP server '{name}' not found"
    _servers[name].stop()
    del _servers[name]
    _rebuild_tools()
    return f"Disconnected MCP server '{name}'"


def _rebuild_tools():
    global _mcp_tools_cache
    _mcp_tools_cache = []
    for server in _servers.values():
        for tool in server.tools:
            _mcp_tools_cache.append(_mcp_to_anthropic(tool))


def get_mcp_tools() -> list[dict]:
    """Return all MCP tools in Anthropic format (for merging into TOOLS)."""
    return _mcp_tools_cache


def call_mcp_tool(full_name: str, arguments: dict) -> str:
    """Route an mcp_ prefixed tool call to the correct server."""
    # full_name is "mcp_<tool_name>", strip prefix
    tool_name = full_name[4:]  # remove "mcp_"
    for server in _servers.values():
        if any(t["name"] == tool_name for t in server.tools):
            return server.call_tool(tool_name, arguments)
    return f"Error: MCP tool '{full_name}' not found on any connected server"


def list_servers() -> str:
    if not _servers:
        return "(no MCP servers connected)"
    lines = []
    for name, s in _servers.items():
        lines.append(f"  {name}: {len(s.tools)} tools ({s._command})")
    return "\n".join(lines)
