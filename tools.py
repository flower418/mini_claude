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
    TOOL("memory_write", "Save important information to memory. Use this when the user explicitly states a preference, feedback, or project fact worth remembering.",
         {"mem_type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]},
          "content": {"type": "string"}}),
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
        results = []
        for match in glob_module.glob(pattern, root_dir=REPO_DIR):
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


TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
    "compact": run_compact, "memory_search": search_memory,
    "memory_write": write_memory,
}

SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}
