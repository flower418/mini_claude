# ── Imports ────────────────────────────────────────────
import os
from pathlib import Path
import subprocess

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
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# ── Tool definitions ───────────────────────────────────
# 模型经过训练，在他认为需要调用工具时，就会阅读 TOOLS 里的 description，然后生成 schema 规范的 block
# 我们在处理时，就会根据他的 block.type，选择调用合适的工具
# 模型的能力是训练得来的，但是模型使用工具的能力需要我们为他建设
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

# ── Tool implementation ────────────────────────────────
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=os.getcwd(),
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

def run_read(path:str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit}) more lines"]
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
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=REPO_DIR):
            if (REPO_DIR / match).resolve().is_relative_to(REPO_DIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"
    
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}

# ── Agent loop ─────────────────────────────────────────
# 初始阶段，用户输入一句 prompt
# 然后进入循环，llm 根据这句话，判断是否要调用工具
# 如果调用工具，就会把工具调用的结果重新输入 history，喂给 llm 进行下一步决策
# 如果没有，则把对话内容返回给用户
def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m$ {block.name}\033[0m")
                handler = TOOL_HANDLERS[block.name]
                output = handler(**block.input) if handler else f"Unknown {block.name}"
                print(str(output)[:200])
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

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
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 获取 llm 的回复
        last = history[-1]["content"]
        if isinstance(last, list):
            for block in last:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
