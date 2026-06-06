# ── Agent team: async sub-agents communicating via JSONL mailboxes ──
# Lead spawns agents; each agent runs in its own thread.
# Communication: append to agent's inbox.jsonl, read + truncate.
import json
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime

from config import AGENTS_DIR, MODEL, client

LEAD_NAME = "lead"
_agent_threads: dict[str, threading.Thread] = {}
_agent_configs: dict[str, "AgentConfig"] = {}
_current = threading.local()  # thread-local: tracks which agent is executing


@dataclass
class AgentConfig:
    name: str
    role: str
    system_prompt: str = ""


# ── Mailbox ──────────────────────────────────────────────

class Mailbox:
    """Thread-safe JSONL inbox. read_all() returns messages then truncates."""

    def __init__(self, agent_name: str):
        self._path = AGENTS_DIR / agent_name / "inbox.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def send(self, from_agent: str, body: str):
        msg = json.dumps({
            "from": from_agent,
            "body": body,
            "timestamp": datetime.now().isoformat(),
        }, ensure_ascii=False)
        with self._lock:
            with open(self._path, "a") as f:
                f.write(msg + "\n")

    def read_all(self) -> list[dict]:
        """Read all messages, then truncate the file. Returns list of {from, body, timestamp}."""
        with self._lock:
            if not self._path.exists():
                return []
            text = self._path.read_text().strip()
            if not text:
                return []
            messages = [json.loads(line) for line in text.splitlines() if line.strip()]
            self._path.write_text("")
            return messages

    def has_mail(self) -> bool:
        with self._lock:
            return self._path.exists() and bool(self._path.read_text().strip())


# ── Agent thread ─────────────────────────────────────────

def _agent_loop(name: str, role: str, system_prompt: str):
    """Sub-agent thread: poll inbox → process with LLM+tools → send result to lead."""
    _current.name = name  # thread-local so tools know who's calling

    # Lazy imports to avoid circular dependency with tools.py
    from tools import SUB_TOOLS, SUB_HANDLERS, safe_dispatch
    from config import extract_text

    sys_prompt = system_prompt or f"You are sub-agent '{name}'. Role: {role}. Work on the given task and return a concise result. Use tools if needed."

    inbox = Mailbox(name)
    lead_mail = Mailbox(LEAD_NAME)

    while True:
        msgs = inbox.read_all()
        if not msgs:
            time.sleep(0.5)
            continue

        # Combine all pending messages
        combined = "\n\n".join(f"[{m['from']} @ {m['timestamp'][:19]}]: {m['body']}" for m in msgs)
        messages = [{"role": "user", "content": combined}]

        # Tool-calling loop (max 8 turns)
        for _ in range(8):
            try:
                response = client.messages.create(
                    model=MODEL,
                    system=sys_prompt,
                    messages=messages,
                    tools=SUB_TOOLS,
                    max_tokens=4000,
                )
            except Exception as e:
                lead_mail.send(name, f"(API error: {e})")
                break

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                break

            results = []
            for block in response.content:
                if block.type == "tool_use":
                    handler = SUB_HANDLERS.get(block.name)
                    output = safe_dispatch(handler, block.input) if handler else f"Unknown tool: {block.name}"
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                    print(f"  \033[90m[team:{name}] {block.name}: {output[:100]}\033[0m")
            messages.append({"role": "user", "content": results})

        # Extract result: scan backward for the last assistant message with text
        result = ""
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if result:
            lead_mail.send(name, result)
            print(f"\033[90m[team:{name}] → lead ({len(result)} chars)\033[0m")


# ── API for tools ────────────────────────────────────────

def spawn_agent(name: str, role: str, system_prompt: str = "") -> str:
    """Spawn a new sub-agent in its own thread."""
    name = name.strip().lower().replace(" ", "-")
    if not name or name == LEAD_NAME:
        return f"Error: invalid agent name '{name}'"
    if name in _agent_threads:
        return f"Error: agent '{name}' already exists"

    cfg = AgentConfig(name=name, role=role, system_prompt=system_prompt)
    _agent_configs[name] = cfg

    # Persist config
    cfg_dir = AGENTS_DIR / name
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False))

    t = threading.Thread(target=_agent_loop, args=(name, role, system_prompt), daemon=True)
    t.start()
    _agent_threads[name] = t
    print(f"\033[90m[team] Spawned: {name} ({role})\033[0m")
    return f"Spawned agent '{name}' ({role}). Use send_to_agent to give it work."


def _whoami() -> str:
    """Return the current agent's name (thread-local), defaulting to 'lead'."""
    return getattr(_current, "name", LEAD_NAME)


def send_to_agent(agent_name: str, message: str) -> str:
    """Send a message to an agent's inbox. Sender is auto-detected from thread context."""
    agent_name = agent_name.strip().lower()
    # Map common aliases to "lead"
    if agent_name in ("default", "lead", "main", "orchestrator"):
        agent_name = LEAD_NAME
    sender = _whoami()
    Mailbox(agent_name).send(sender, message)
    print(f"\033[90m[team] {sender} → {agent_name}: {message[:60]}\033[0m")
    return f"Sent to '{agent_name}'."


def check_agent_mail(agent_name: str = "") -> str:
    """Read an agent's inbox. If no name given, reads current agent's inbox (thread-local)."""
    name = agent_name.strip().lower() if agent_name else _whoami()
    msgs = Mailbox(name).read_all()
    if not msgs:
        return "(no mail)"
    lines = []
    for m in msgs:
        lines.append(f"[{m['from']}]: {m['body']}")
    return "\n\n".join(lines)


def list_agents() -> str:
    """List all spawned agents and their roles."""
    if not _agent_configs:
        return "(no agents spawned)"
    lines = []
    for name, cfg in _agent_configs.items():
        alive = name in _agent_threads and _agent_threads[name].is_alive()
        icon = "\033[32m●\033[0m" if alive else "\033[31m✗\033[0m"
        lines.append(f"  {icon} {name}: {cfg.role}")
    return "\n".join(lines)


def kill_agent(name: str) -> str:
    """Remove an agent: clear inbox + config from disk, drop from registry."""
    name = name.strip().lower()
    if name == LEAD_NAME:
        return "Error: cannot kill the lead agent"
    if name not in _agent_configs:
        return f"Error: agent '{name}' not found"

    del _agent_configs[name]
    if name in _agent_threads:
        del _agent_threads[name]  # daemon thread will die with process

    # Clean up disk
    import shutil
    agent_dir = AGENTS_DIR / name
    if agent_dir.exists():
        shutil.rmtree(agent_dir)

    print(f"\033[90m[team] Killed: {name}\033[0m")
    return f"Killed agent '{name}' and cleaned up .agents/{name}/"
