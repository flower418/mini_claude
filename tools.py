# ── Tool definitions & implementations ──────────────────
import ast
import glob as glob_module
import inspect
import json
import subprocess

from config import REPO_DIR, DENY_LIST, safe_path, MODEL, client, extract_text
from skills import SUB_SYSTEM, load_skill
from hooks import trigger_hooks
from compact import run_compact
from memory import search_memory, write_memory
from task_system import run_create_task, run_claim_task, run_complete_task, run_list_tasks, run_get_task
import background
from background import should_background
import scheduler
from agent_team import (
    spawn_agent, send_to_agent, check_agent_mail, list_agents, kill_agent,
    request_shutdown, request_plan, respond_request, approve_request, reject_request,
)

CURRENT_TODOS: list[dict] = []
rounds_since_todo = 0
rounds_since_memory = 0


def _normalize_todos(todos):
    """Validate and normalize todos input (handles string JSON)."""
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None


def run_todo_write(todos: list) -> str:
    """Update the global task list and print it."""
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {
            "pending": " ",
            "in_progress": "\033[36m▸\033[0m",
            "completed": "\033[32m✓\033[0m",
        }[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"


# ── Tool definitions (Anthropic schema) ────────────────

TOOL = lambda name, desc, props, required=None: {
    "name": name,
    "description": desc,
    "input_schema": {
        "type": "object",
        "properties": props,
        "required": required or list(props.keys()),
    },
}

TOOLS = [
    TOOL("bash", "Run a shell command.", {"command": {"type": "string"}}),
    TOOL("read_file", "Read file contents.", {
        "path": {"type": "string"},
        "offset": {"type": "integer"},
        "limit": {"type": "integer"},
    }, ["path"]),
    TOOL("write_file", "Write content to a file.", {
        "path": {"type": "string"},
        "content": {"type": "string"},
    }),
    TOOL("edit_file", "Replace exact text in a file once.", {
        "path": {"type": "string"},
        "old_text": {"type": "string"},
        "new_text": {"type": "string"},
    }),
    TOOL("glob", "Find files matching a glob pattern.", {"pattern": {"type": "string"}}),
    TOOL("todo_write", "Create and manage a task list for your current coding session.", {
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                },
                "required": ["content", "status"],
            },
        }
    }),
    TOOL("task", "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
         {"description": {"type": "string"}}),
    TOOL("load_skill", "Load the full content of a skill by name.",
         {"name": {"type": "string"}}),
    TOOL("compact", "Summarize earlier conversation to free context space.",
         {"focus": {"type": "string"}}, []),
    TOOL("memory_search", "Search user memory for preferences, feedback, project context, and learned facts. Call this BEFORE responding to any user request.",
         {"query": {"type": "string"}}),
    TOOL("memory_write", "Save important information to memory. Use when the user explicitly states a preference, feedback, or project fact.",
         {"name": {"type": "string"},
          "description": {"type": "string"},
          "mem_type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]},
          "content": {"type": "string"}}),
    TOOL("create_task", "Create a new task in the persistent task system. Tasks support dependency tracking across sessions.",
         {"subject": {"type": "string"},
          "description": {"type": "string"},
          "blockedBy": {"type": "array", "items": {"type": "string"}}},
         ["subject"]),
    TOOL("claim_task", "Claim a pending task (sets status to in_progress). Fails if dependencies are not met.",
         {"task_id": {"type": "string"},
          "owner": {"type": "string"}},
         ["task_id"]),
    TOOL("complete_task", "Mark an in_progress task as completed. Notes any newly unblocked dependents.",
         {"task_id": {"type": "string"}}),
    TOOL("list_tasks", "List tasks with status icons and dependency arrows. Optionally filter by status.",
         {"status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
         []),
    TOOL("get_task", "Get full details of a specific task including dependents.",
         {"task_id": {"type": "string"}}),
    TOOL("run_background", "Submit a complex subtask to run in the background (non-blocking, returns task_id immediately). Use when a task: requires heavy analysis, scans many files, would need 3+ tool calls, or blocks the user from continuing. For quick/simple tasks use inline tools instead. Tip: call judge_background first if unsure.",
         {"prompt": {"type": "string"},
          "focus": {"type": "string"}},
         ["prompt"]),
    TOOL("judge_background", "Judge whether a task should run in background or inline. Returns verdict with reasons.",
         {"subject": {"type": "string"},
          "description": {"type": "string"}},
         ["subject"]),
    TOOL("list_background", "List status of background tasks: running, completed (pending collection).",
         {}, []),
    TOOL("schedule_task", "Schedule a CronJob. cron format: [sec] minute hour day month weekday. Use 6 fields for sub-minute intervals (e.g. '*/15 * * * * *' = every 15s). 5 fields defaults sec=0. Set recurring=false for one-shot, durable=false for memory-only.",
         {"id": {"type": "string"},
          "cron": {"type": "string"},
          "prompt": {"type": "string"},
          "recurring": {"type": "boolean"},
          "durable": {"type": "boolean"}},
         ["cron", "prompt"]),
    TOOL("list_schedule", "List all scheduled tasks with next run times.",
         {}, []),
    TOOL("cancel_schedule", "Cancel and remove a scheduled task.",
         {"task_id": {"type": "string"}}),
    TOOL("spawn_agent", "Spawn a new sub-agent in its own thread. It communicates via inbox. Use send_to_agent to give it tasks.",
         {"name": {"type": "string"},
          "role": {"type": "string"},
          "system_prompt": {"type": "string"}},
         ["name", "role"]),
    TOOL("send_to_agent", "Send a message/task to a sub-agent. It will process asynchronously and reply to your inbox.",
         {"agent_name": {"type": "string"},
          "message": {"type": "string"}}),
    TOOL("check_agent_mail", "Check an agent's inbox (defaults to yours). Sub-agents can also use this.",
         {"agent_name": {"type": "string"}}, []),
    TOOL("list_agents", "List all spawned sub-agents and their status.",
         {}, []),
    TOOL("kill_agent", "Remove an agent: clean up its files and stop tracking it.",
         {"name": {"type": "string"}}),
    TOOL("request_shutdown", "Send a shutdown request to a sub-agent. It will auto-accept and clean up.",
         {"agent_name": {"type": "string"}}),
    TOOL("approve_request", "Approve a pending plan request from a sub-agent.",
         {"request_id": {"type": "string"}}),
    TOOL("reject_request", "Reject a pending plan request from a sub-agent.",
         {"request_id": {"type": "string"},
          "reason": {"type": "string"}},
         ["request_id"]),
    TOOL("request_plan", "(Sub-agent only) Send a plan to lead for approval before executing.",
         {"description": {"type": "string"}}),
    TOOL("respond_request", "(Sub-agent only) Accept or reject an incoming request (e.g. shutdown).",
         {"request_id": {"type": "string"},
          "accept": {"type": "boolean"},
          "reason": {"type": "string"}},
         ["request_id", "accept"]),
]

SUB_TOOLS = [
    TOOL("bash", "Run a shell command.", {"command": {"type": "string"}}),
    TOOL("read_file", "Read file contents.", {
        "path": {"type": "string"},
        "offset": {"type": "integer"},
        "limit": {"type": "integer"},
    }, ["path"]),
    TOOL("write_file", "Write content to a file.", {
        "path": {"type": "string"},
        "content": {"type": "string"},
    }),
    TOOL("edit_file", "Replace exact text in a file once.", {
        "path": {"type": "string"},
        "old_text": {"type": "string"},
        "new_text": {"type": "string"},
    }),
    TOOL("glob", "Find files matching a glob pattern.", {"pattern": {"type": "string"}}),
    TOOL("send_to_agent", "Send a message to another agent's inbox.",
         {"agent_name": {"type": "string"}, "message": {"type": "string"}}),
    TOOL("check_agent_mail", "Check your own inbox for new messages.",
         {}, []),
    TOOL("request_plan", "Send a plan to lead for approval before doing complex work.",
         {"description": {"type": "string"}}),
    TOOL("respond_request", "Accept or reject an incoming request (e.g. shutdown from lead).",
         {"request_id": {"type": "string"},
          "accept": {"type": "boolean"},
          "reason": {"type": "string"}},
         ["request_id", "accept"]),
    TOOL("claim_task", "Claim a pending task (sets status to in_progress). Fails if dependencies are not met.",
         {"task_id": {"type": "string"},
          "owner": {"type": "string"}},
         ["task_id"]),
    TOOL("complete_task", "Mark an in_progress task as completed. Notes any newly unblocked dependents.",
         {"task_id": {"type": "string"}}),
    TOOL("list_tasks", "List tasks with status icons and dependency arrows. Optionally filter by status.",
         {"status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
         []),
]


# ── Tool implementations ───────────────────────────────

def run_bash(command: str) -> str:
    if any(d in command for d in DENY_LIST):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=str(REPO_DIR),
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def run_read(path: str, offset: int = 0, limit: int | None = None) -> str:
    try:
        all_lines = safe_path(path).read_text().splitlines()
        total = len(all_lines)
        offset = max(0, int(offset or 0))
        limit = int(limit) if limit is not None else None
        if offset > total:
            offset = total
        lines = all_lines[offset:]
        if limit is not None and limit >= 0 and limit < len(lines):
            lines = lines[:limit] + [f"... ({total - offset - limit}) more lines"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    try:
        recursive = "**" in pattern
        results = []
        for match in glob_module.glob(pattern, root_dir=REPO_DIR, recursive=recursive):
            if (REPO_DIR / match).resolve().is_relative_to(REPO_DIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def spawn_subagent(description: str) -> str:
    """Spawn a subagent with fresh context, return summary only."""
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=100000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                output = safe_dispatch(handler, block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
        messages.append({"role": "user", "content": results})
    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


def safe_dispatch(handler, inputs: dict) -> str:
    """Call handler with only the parameters it accepts; warn about unknowns."""
    sig = inspect.signature(handler)
    valid = {}
    unknown = []
    for k, v in inputs.items():
        if k in sig.parameters:
            valid[k] = v
        else:
            unknown.append(k)
    if unknown:
        print(f"\033[33m[WARN] Unknown arguments for {handler.__name__}: {unknown}\033[0m")
    try:
        return handler(**valid)
    except TypeError as e:
        return f"Error: bad arguments for {handler.__name__}: {e}"


# ── Background task handlers ────────────────────────────

def run_background(prompt: str, focus: str = "") -> str:
    """Submit a complex subtask to run in a background thread."""
    if not prompt.strip():
        return "Error: prompt is required"
    full_prompt = f"Focus: {focus}\n\n{prompt}" if focus else prompt
    task_id = background.submit(full_prompt)
    return f"Background task submitted: {task_id}\nCheck back with list_background or results will be injected automatically."


def run_judge_background(subject: str, description: str = "") -> str:
    """Judge whether a task should be backgrounded."""
    return should_background(subject, description)


def run_list_background() -> str:
    """List currently running and recently completed background tasks."""
    running = background.list_pending()
    done = background.list_done()
    lines = []
    if running:
        lines.append(f"\033[36m▸\033[0m Running ({len(running)}): {', '.join(running)}")
    if done:
        lines.append(f"\033[32m✓\033[0m Done, awaiting collection ({len(done)}): {', '.join(done)}")
    if not running and not done:
        lines.append("No background tasks.")
    return "\n".join(lines)


# ── Scheduler tool handlers ─────────────────────────────

def run_schedule_task(id: str = "", cron: str = "* * * * *", prompt: str = "", recurring: bool = True, durable: bool = True) -> str:
    return scheduler.add_schedule(id, cron, prompt, recurring, durable)


def run_list_schedule() -> str:
    return scheduler.list_schedules()


def run_cancel_schedule(task_id: str) -> str:
    return scheduler.cancel_schedule(task_id)


# ── Agent team handlers ──────────────────────────────────

def run_spawn_agent(name: str, role: str, system_prompt: str = "") -> str:
    return spawn_agent(name, role, system_prompt)


def run_send_to_agent(agent_name: str, message: str) -> str:
    return send_to_agent(agent_name, message)


def run_check_agent_mail(agent_name: str = "") -> str:
    return check_agent_mail(agent_name)


def run_list_agents() -> str:
    return list_agents()


def run_kill_agent(name: str) -> str:
    return kill_agent(name)


def run_request_shutdown(agent_name: str) -> str:
    return request_shutdown(agent_name)


def run_approve_request(request_id: str) -> str:
    return approve_request(request_id)


def run_reject_request(request_id: str, reason: str = "") -> str:
    return reject_request(request_id, reason)


def run_request_plan(description: str) -> str:
    return request_plan(description)


def run_respond_request(request_id: str, accept: bool, reason: str = "") -> str:
    return respond_request(request_id, accept, reason)


TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
    "compact": run_compact, "memory_search": search_memory,
    "memory_write": write_memory,
    "create_task": run_create_task, "claim_task": run_claim_task,
    "complete_task": run_complete_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task, "run_background": run_background,
    "judge_background": run_judge_background,
    "list_background": run_list_background,
    "schedule_task": run_schedule_task, "list_schedule": run_list_schedule,
    "cancel_schedule": run_cancel_schedule,
    "spawn_agent": run_spawn_agent, "send_to_agent": run_send_to_agent,
    "check_agent_mail": run_check_agent_mail, "list_agents": run_list_agents,
    "kill_agent": run_kill_agent, "request_shutdown": run_request_shutdown,
    "approve_request": run_approve_request, "reject_request": run_reject_request,
    "request_plan": run_request_plan, "respond_request": run_respond_request,
}

SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
    "send_to_agent": run_send_to_agent, "check_agent_mail": run_check_agent_mail,
    "request_plan": run_request_plan, "respond_request": run_respond_request,
    "claim_task": run_claim_task, "complete_task": run_complete_task,
    "list_tasks": run_list_tasks,
}
