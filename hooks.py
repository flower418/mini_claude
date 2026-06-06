# ── Hook system ─────────────────────────────────────────
from config import DENY_LIST, DESTRUCTIVE, REPO_DIR, safe_path

HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}
_INITIALIZED = False


def register_hook(event: str, callback):
    """Register a callback for a hook event."""
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """Trigger all callbacks for an event. First non-None return blocks."""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# ── Built-in hooks ─────────────────────────────────────

def permission_hook(block):
    """PreToolUse: three-tier safety check."""
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


def log_hook(block):
    """PreToolUse: log tool name only (compact)."""
    print(f"\033[90m[{block.name}]\033[0m", end="", flush=True)
    return None


def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None


def context_inject_hook(query: str):
    """UserPromptSubmit: log working directory."""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {REPO_DIR}\033[0m")
    return None


def summary_hook(messages: list):
    """Stop: count total tool calls in session."""
    return None


def init_hooks():
    """Register all built-in hooks. Called once at startup."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    register_hook("UserPromptSubmit", context_inject_hook)
    register_hook("PreToolUse", permission_hook)
    register_hook("PreToolUse", log_hook)
    register_hook("PostToolUse", large_output_hook)
    register_hook("Stop", summary_hook)
    _INITIALIZED = True
