# ── Agent team: async sub-agents communicating via JSONL mailboxes ──
# Protocol: typed messages (task/shutdown/plan/accept/reject) with request_id
# Each request goes through request → confirm/reject cycle
import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime

from config import AGENTS_DIR, MODEL, client

LEAD_NAME = "lead"
_agent_threads: dict[str, threading.Thread] = {}
_agent_configs: dict[str, "AgentConfig"] = {}
_current = threading.local()
_pending_requests: dict[str, dict] = {}
_requests_lock = threading.Lock()

# Message types
TYPE_TASK     = "task"
TYPE_SHUTDOWN = "shutdown"
TYPE_PLAN     = "plan"
TYPE_ACCEPT   = "accept"
TYPE_REJECT   = "reject"


@dataclass
class AgentConfig:
    name: str
    role: str
    system_prompt: str = ""


# ── Mailbox ──────────────────────────────────────────────

class Mailbox:
    """Thread-safe JSONL inbox with cursor-based reads."""

    def __init__(self, agent_name: str):
        self._path = AGENTS_DIR / agent_name / "inbox.jsonl"
        self._cursor_path = AGENTS_DIR / agent_name / "inbox.cursor"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read_cursor(self) -> int:
        try:
            return int(self._cursor_path.read_text().strip() or "0")
        except (FileNotFoundError, ValueError):
            return 0

    def _write_cursor(self, pos: int):
        self._cursor_path.write_text(str(pos))

    def send(self, from_agent: str, body: str, msg_type: str = TYPE_TASK, request_id: str = ""):
        envelope = {
            "type": msg_type,
            "request_id": request_id,
            "from": from_agent,
            "body": body,
            "timestamp": datetime.now().isoformat(),
        }
        line = json.dumps(envelope, ensure_ascii=False)
        with self._lock:
            with open(self._path, "a") as f:
                f.write(line + "\n")

    def read_all(self) -> list[dict]:
        """Read new messages since last cursor advance."""
        with self._lock:
            if not self._path.exists():
                return []
            lines = [l for l in self._path.read_text().splitlines() if l.strip()]
            cursor = self._read_cursor()
            new_lines = lines[cursor:]
            if new_lines:
                self._write_cursor(cursor + len(new_lines))
            return [json.loads(l) for l in new_lines]

    def peek(self) -> list[dict]:
        """Read all messages without advancing cursor."""
        with self._lock:
            if not self._path.exists():
                return []
            lines = [l for l in self._path.read_text().splitlines() if l.strip()]
            cursor = self._read_cursor()
            result = [json.loads(l) for l in lines]
            for i, m in enumerate(result):
                m["_read"] = i < cursor
            return result

    def has_mail(self) -> bool:
        with self._lock:
            if not self._path.exists():
                return False
            total = len([l for l in self._path.read_text().splitlines() if l.strip()])
            return total > self._read_cursor()


# ── Lifecycle ────────────────────────────────────────────

def cleanup_stale():
    """Remove all .agents/ dirs including lead's from previous sessions."""
    if AGENTS_DIR.exists():
        for d in AGENTS_DIR.iterdir():
            if d.is_dir():
                shutil.rmtree(d)
        # Re-create lead's mailbox dir
        Mailbox(LEAD_NAME)
        print(f"\033[90m[team] Cleaned stale agent dirs\033[0m")


def _whoami() -> str:
    return getattr(_current, "name", LEAD_NAME)


def _resolve_agent(name: str) -> str:
    name = name.strip().lower()
    if name in ("default", "main", "orchestrator"):
        return LEAD_NAME
    return name


# ── Protocol: send typed messages ────────────────────────

def _send_envelope(to_agent: str, body: str, msg_type: str = TYPE_TASK, request_id: str = "") -> str:
    """Send a typed message. Generates request_id if needed."""
    to_agent = _resolve_agent(to_agent)
    if not request_id and msg_type in (TYPE_SHUTDOWN, TYPE_PLAN):
        request_id = "req_" + uuid.uuid4().hex[:12]
    sender = _whoami()
    Mailbox(to_agent).send(sender, body, msg_type, request_id)

    if msg_type in (TYPE_SHUTDOWN, TYPE_PLAN):
        with _requests_lock:
            _pending_requests[request_id] = {
                "type": msg_type, "from": sender, "to": to_agent,
                "status": "pending", "timestamp": time.time(),
            }
    type_label = f"[{msg_type}]" if msg_type != TYPE_TASK else ""
    print(f"\033[90m[team] {sender} → {to_agent}: {type_label}\033[0m")
    return f"Sent {type_label} to '{to_agent}'" + (f" (req: {request_id})" if request_id else "")


def _send_response(request_id: str, accept: bool, reason: str = "") -> str:
    """Respond to a pending request."""
    with _requests_lock:
        req = _pending_requests.get(request_id)
    if not req:
        return f"Error: request '{request_id}' not found"
    resp_type = TYPE_ACCEPT if accept else TYPE_REJECT
    target = req["from"]
    body = reason or ("accepted" if accept else "rejected")
    Mailbox(target).send(_whoami(), body, resp_type, request_id)
    with _requests_lock:
        _pending_requests[request_id]["status"] = "accepted" if accept else "rejected"
    label = "accept" if accept else "reject"
    print(f"\033[90m[team] {_whoami()} → {target}: [{label}] {request_id}\033[0m")
    return f"Sent [{label}] for request '{request_id}'"


# ── Agent thread ─────────────────────────────────────────

def _remove_agent(name: str):
    """Internal: remove agent from registry and disk."""
    if name in _agent_configs:
        del _agent_configs[name]
    if name in _agent_threads:
        del _agent_threads[name]
    agent_dir = AGENTS_DIR / name
    if agent_dir.exists():
        shutil.rmtree(agent_dir)


def _agent_loop(name: str, role: str, system_prompt: str):
    """Sub-agent thread: idle loop → process tasks → handle protocol messages."""
    _current.name = name
    from tools import SUB_TOOLS, SUB_HANDLERS, safe_dispatch
    from config import extract_text

    sys_prompt = system_prompt or (
        f"You are sub-agent '{name}'. Role: {role}. "
        f"Work on the given task and return a concise result. Use tools if needed. "
        f"If you need approval for a complex plan, use request_plan. "
        f"Use respond_request to accept/reject incoming requests."
    )

    inbox = Mailbox(name)
    lead_mail = Mailbox(LEAD_NAME)

    while True:
        msgs = inbox.read_all()
        if not msgs:
            time.sleep(0.5)
            continue

        # ── Classify incoming messages ────────────────
        shutdown_msgs = [m for m in msgs if m.get("type") == TYPE_SHUTDOWN]
        plan_responses = [m for m in msgs if m.get("type") in (TYPE_ACCEPT, TYPE_REJECT)]
        task_msgs = [m for m in msgs if m.get("type", TYPE_TASK) == TYPE_TASK]

        # Nothing actionable
        if not shutdown_msgs and not task_msgs:
            continue

        # ── Build conversation ────────────────────────
        context_parts = []
        for pr in plan_responses:
            decision = "APPROVED" if pr["type"] == TYPE_ACCEPT else "REJECTED"
            context_parts.append(f"[SYSTEM: Plan {decision}] {pr['body']}")
        for sm in shutdown_msgs:
            context_parts.append(
                f"[PROTOCOL: shutdown request {sm['request_id']}] "
                f"Use respond_request to accept or reject. "
                f"Reject if you are still working; accept if idle/done."
            )
        for tm in task_msgs:
            context_parts.append(f"[{tm['from']} @ {tm['timestamp'][:19]}]: {tm['body']}")

        if not context_parts:
            continue
        combined = "\n\n".join(context_parts)
        messages = [{"role": "user", "content": combined}]

        # ── Tool-calling loop ─────────────────────────
        accepted_shutdown = None
        for _ in range(12):
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
                    if block.name not in ("check_agent_mail", "send_to_agent"):
                        print(f"  \033[90m[{name}] {block.name}\033[0m")
                    # Track if shutdown was accepted
                    if block.name == "respond_request":
                        inp = block.input
                        if inp.get("accept") and inp.get("request_id") in [sm["request_id"] for sm in shutdown_msgs]:
                            accepted_shutdown = inp["request_id"]
            messages.append({"role": "user", "content": results})

        # ── Handle shutdown acceptance ────────────────
        if accepted_shutdown:
            _remove_agent(name)
            print(f"\033[90m[team:{name}] Shutdown {accepted_shutdown} accepted, exiting\033[0m")
            return

        # ── Send task result to lead ──────────────────
        result = ""
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if result and not shutdown_msgs:  # don't send result for shutdown-only interactions
            lead_mail.send(name, result)
            print(f"\033[90m  [{name}] → lead ({len(result)} chars)\033[0m")


# ── Public API (tool handlers) ───────────────────────────

def spawn_agent(name: str, role: str, system_prompt: str = "") -> str:
    name = name.strip().lower().replace(" ", "-")
    if not name or name == LEAD_NAME:
        return f"Error: invalid agent name '{name}'"
    if name in _agent_threads:
        return f"Error: agent '{name}' already exists"

    cfg = AgentConfig(name=name, role=role, system_prompt=system_prompt)
    _agent_configs[name] = cfg
    cfg_dir = AGENTS_DIR / name
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False))

    t = threading.Thread(target=_agent_loop, args=(name, role, system_prompt), daemon=True)
    t.start()
    _agent_threads[name] = t
    print(f"\033[90m[team] Spawned: {name} ({role})\033[0m")
    return f"Spawned agent '{name}' ({role}). Use send_to_agent to give it work."


def send_to_agent(agent_name: str, message: str) -> str:
    return _send_envelope(agent_name, message, TYPE_TASK)


def request_shutdown(agent_name: str) -> str:
    return _send_envelope(agent_name, "shutdown request", TYPE_SHUTDOWN)


def request_plan(description: str) -> str:
    """Sub-agent sends a plan to lead for approval."""
    sender = _whoami()
    if sender == LEAD_NAME:
        return "Error: only sub-agents can request plan approval"
    return _send_envelope(LEAD_NAME, description, TYPE_PLAN)


def respond_request(request_id: str, accept: bool, reason: str = "") -> str:
    """Accept or reject an incoming request (used by sub-agents for shutdown)."""
    return _send_response(request_id, accept, reason)


def approve_request(request_id: str) -> str:
    """Lead approves a pending plan request."""
    return _send_response(request_id, accept=True)


def reject_request(request_id: str, reason: str = "") -> str:
    """Lead rejects a pending plan request."""
    return _send_response(request_id, accept=False, reason=reason)


def check_agent_mail(agent_name: str = "") -> str:
    name = agent_name.strip().lower() if agent_name else _whoami()
    msgs = Mailbox(name).peek()
    if not msgs:
        return "(no mail)"
    lines = []
    for m in msgs:
        tag = "" if m.get("_read") else " \033[33m[new]\033[0m"
        type_tag = f" [{m.get('type', '?')}]" if m.get("type", TYPE_TASK) != TYPE_TASK else ""
        rid = f" ({m['request_id']})" if m.get("request_id") else ""
        lines.append(f"[{m['from']}]{type_tag}{rid}{tag}: {m['body']}")
    return "\n\n".join(lines)


def list_agents() -> str:
    if not _agent_configs:
        return "(no agents spawned)"
    lines = []
    for name, cfg in _agent_configs.items():
        alive = name in _agent_threads and _agent_threads[name].is_alive()
        icon = "\033[32m●\033[0m" if alive else "\033[31m✗\033[0m"
        lines.append(f"  {icon} {name}: {cfg.role}")
    return "\n".join(lines)


def kill_agent(name: str) -> str:
    name = name.strip().lower()
    if name == LEAD_NAME:
        return "Error: cannot kill the lead agent"
    if name not in _agent_configs:
        return f"Error: agent '{name}' not found"
    _remove_agent(name)
    print(f"\033[90m[team] Killed: {name}\033[0m")
    return f"Killed agent '{name}' and cleaned up .agents/{name}/"
