# ── Imports ────────────────────────────────────────────
import ast
import glob as glob_module
import inspect
import os
from pathlib import Path
import subprocess
import json

try:
    import readline
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import dotenv_values

# ── Environment ────────────────────────────────────────
REPO_DIR = Path(__file__).resolve().parent
ENV_FILE = REPO_DIR / ".env"


def load_repo_config() -> dict[str, str]:
    if not ENV_FILE.exists():
        raise RuntimeError(f"Missing {ENV_FILE}. Create it in the repo root; see README.md.")

    config = {k: v for k, v in dotenv_values(ENV_FILE).items() if v}
    missing = [key for key in ("ANTHROPIC_API_KEY", "MODEL_ID") if not config.get(key)]
    if missing:
        raise RuntimeError(f"Missing {', '.join(missing)} in {ENV_FILE}")
    return config


CONFIG = load_repo_config()
for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "MODEL_ID"):
    os.environ.pop(key, None)

client = Anthropic(
    api_key=CONFIG["ANTHROPIC_API_KEY"],
    base_url=CONFIG.get("ANTHROPIC_BASE_URL"),
)
MODEL = CONFIG["MODEL_ID"]

# ── System prompt ──────────────────────────────────────
SYSTEM = (
    f"You are a coding agent at {REPO_DIR}. "
    "For complex sub-problems, use the task tool to spawn a subagent."
) 

SUB_SYSTEM = (
    f"You are a coding agent at {REPO_DIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)

CURRENT_TODOS: list[dict] = []

def _normalize_todos(todos):
    # 进行兜底，如果 todos 传入字符串，尝试变成 list
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

    # todos: [didct]
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None

# 把 todo 写入全局的 CURRENT_TODOS
def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS

    todos, error = _normalize_todos(todos)
    if error:
        return error

    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))

    return f"Updated {len(CURRENT_TODOS)} tasks"

# ── Tool definitions ───────────────────────────────────
# 模型经过训练，在他认为需要调用工具时，就会阅读 TOOLS 里的 description，然后生成 schema 规范的 block
# 我们在处理时，就会根据他的 block.type，选择调用合适的工具
# 模型的能力是训练得来的，但是模型使用工具的能力需要我们为他建设
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name": "task","description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},}
]

# subagent 没有 subagent 功能
SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

# ── Tool implementation ────────────────────────────────
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

def safe_path(p: str) -> Path:
    path = (REPO_DIR / p).resolve()
    if not path.is_relative_to(REPO_DIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

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

# 进行文件的通配
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
    """Spawn a subagent with fresh messages[], return summary only."""
    print(f"\n\033[35m[Subagent spawned]\033[0m")

    messages = [{"role": "user", "content": description}]  # fresh context
    for _ in range(30):  # safety limit
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
                # Issue 1: subagent also runs hooks (permissions apply)
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

    # Issue 5: fallback if safety limit hit during tool_use
    # extract_text 用于提取 llm 的 text 回复
    # 如果最后一条不是 text，就可能是 tool_use 之类
    result = extract_text(messages[-1]["content"])
    if not result:
        # last message is tool_result, look backwards for assistant text
        # 然后就倒序往前查找，尝试提取文本
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        # 如果还没找到，就表示没有最终回答
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result  # only summary, entire message history discarded
    

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
    "task": spawn_subagent
}

SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob
}

def extract_text(content) -> str:
    """Extract text from message content blocks."""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")


# 为 agent 添加 hook
# 他有一个定时出发机制，在我们的设计里，选择在四个时候触发
# 1.对话进入 llm 前，触发一次
# 2.进入 llm 后，调用工具前，触发一次
# 3.调用工具后，退出前，触发一次
# 4.退出后触发一次
# 添加 hook 的目的是让我们不需要把所有额外添加的性能全写在循环里
# 我们只需要在 loop 内部增加 hook，然后在 loop 外为每个阶段注册 hook
# 这样每个阶段都能有自己应当执行的程序，同时保留了 loop 的完整功能
HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}

# 把某个特定的事件注册进 hook
# callback 指某个需要登记起来，某个阶段调用的函数
def register_hook(event: str, callback):
    HOOKS[event].append(callback)

# trigger 指触发某个 event 上的所有 hook
# *args 指可变参数，数量不限
def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# 用三道机制来保证运行的安全
# 1.极端危险程序，直接拒绝
# 2.有风险的程序：如果满足一定规则，如写在 workspace 外或者删除某些文件，需要用户审核
# 如果不满足这些条件，就直接通过，运行
# 如果满足风险，则进入第三个审核
# 3.用户审核：审核步骤 2 放过来的命令，判断是否通过
# 4.如果 3 重审核都没筛出去，那就直接运行
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

# 把 permission 判断改写成 hook 形式，这样就不用在 loop 中单独添加代码，避免冗余
# 只有需要停止后续流程时才会返回内容
# 否则默认返回 none，表示可以继续流程
def permission_hook(block):
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"

        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
                
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        try:
            safe_path(path)
        except ValueError:
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    
    return None

# 只返回 none，不拦截流程
def log_hook(block):
    """PreToolUse: log every tool call."""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None

def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None

# UserPromptSubmit hook: log user input before it reaches the LLM
def context_inject_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {REPO_DIR}\033[0m")
    return None

# stop hook
# 统计调用了多少次 tool
def summary_hook(messages: list):
    tool_count = 0

    for m in messages:
        content = m.get("content")

        if isinstance(content, list):
            for b in content:
                if (isinstance(b, dict)) and b.get("type") == "tool_result":
                    tool_count += 1

    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)

# ── Agent loop ─────────────────────────────────────────
# 初始阶段，用户输入一句 prompt
# 然后进入循环，llm 根据这句话，判断是否要调用工具
# 如果调用工具，就会把工具调用的结果重新输入 history，喂给 llm 进行下一步决策
# 如果没有，则把对话内容返回给用户

# 一个全局计数器，标志多长时间没有更新 todo
# 如果长时间没有更新，就发送 prompt，让模型更新
# 实现规划的功能
rounds_since_todo = 0
def agent_loop(messages: list):
    global rounds_since_todo

    while True:
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user", 
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)

            # 模型在 stop 前强制他再对话一轮，比如审查结果之类的
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        rounds_since_todo += 1
        results = []

        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)
                # 检查 PreToolUse hook
                # 如果有任务返回，直接给 llm 加一轮对话
                # 如果没有返回，表示没有触发需要中断的 hook，直接执行下面的 loop
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue

                handler = TOOL_HANDLERS.get(block.name) # 健壮性
                output = safe_dispatch(handler, block.input) if handler else f"Unknown {block.name}"

                trigger_hooks("PostToolUse", block, output)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
                
                if block.name == "todo_write":
                    rounds_since_todo = 0

        messages.append({"role": "user", "content": results})


# ── Entry point ────────────────────────────────────────
if __name__ == "__main__":
    print("mini_claude agent — 输入问题，回车发送。q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36m>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        trigger_hooks("UserPromptSubmit", query)

        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 获取 llm 的回复
        last = history[-1]["content"]
        if isinstance(last, list):
            for block in last:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
