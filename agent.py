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
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


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
                print(f"\033[33m$ {block.input['command']}\033[0m")
                output = run_bash(block.input["command"])
                print(output[:200])
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
