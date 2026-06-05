# ── Agent entry point & main loop ───────────────────────
import json

try:
    import readline
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass

import memory

memory.init_memory()

from config import MODEL, client, extract_text
from skills import SYSTEM
import tools
from compact import preprocess_pipeline, estimate_size, compact_history, emergency_compact, CONTEXT_LIMIT
from hooks import trigger_hooks, init_hooks

init_hooks()


def agent_loop(messages: list):
    """Run the agent loop: preprocess → API call → tool dispatch → repeat."""

    while True:
        # ── Todo reminder ────────────────────────────────
        if tools.rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            tools.rounds_since_todo = 0

        # ── Memory reminder ──────────────────────────────
        if tools.rounds_since_memory >= 2 and messages:
            index = memory.get_memory_index()
            if "(empty)" not in index:
                messages.append({"role": "user",
                                 "content": f"<reminder>Check memory before proceeding:\n{index}\nUse memory_search if relevant.</reminder>"})
                tools.rounds_since_memory = 0

        # ── Compact pipeline: preprocessing → LLM compact → API call ──
        messages[:] = preprocess_pipeline(messages)

        if estimate_size(json.dumps(messages, default=str)) > CONTEXT_LIMIT:
            print(f"\033[33m[compact] Context over limit, running LLM compaction...\033[0m")
            messages[:] = compact_history(messages)

        try:
            response = client.messages.create(
                model=MODEL, system=SYSTEM, messages=messages,
                tools=tools.TOOLS, max_tokens=8000,
            )
        except Exception as e:
            err = str(e).lower()
            if "prompt" in err and ("too long" in err or "too large" in err or "exceed" in err):
                messages[:] = emergency_compact(messages)
                response = client.messages.create(
                    model=MODEL, system=SYSTEM, messages=messages,
                    tools=tools.TOOLS, max_tokens=8000,
                )
            else:
                raise

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # ── Compact tool: model proactively compacts context ──
        compact_block = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "compact":
                compact_block = block
                break

        if compact_block:
            pre_history = messages[:-1]
            focus = compact_block.input.get("focus")
            compacted = compact_history(pre_history, focus)
            messages[:] = compacted + [messages[-1]]
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": compact_block.id,
                 "content": "Conversation compacted successfully. Summary above."}
            ]})
            tools.rounds_since_todo = 0
            continue

        # ── Normal tool dispatch ─────────────────────────
        tools.rounds_since_todo += 1
        tools.rounds_since_memory += 1
        results = []

        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue

                handler = tools.TOOL_HANDLERS.get(block.name)
                output = tools.safe_dispatch(handler, block.input) if handler else f"Unknown {block.name}"

                trigger_hooks("PostToolUse", block, output)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})

                if block.name == "todo_write":
                    tools.rounds_since_todo = 0
                if block.name == "memory_search" or block.name == "memory_write":
                    tools.rounds_since_memory = 0

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
        tools.rounds_since_memory = 0  # new query → encourage fresh memory check
        agent_loop(history)

        last = history[-1]["content"]
        if isinstance(last, list):
            for block in last:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()

        # ── Memory consolidation after each turn ─────────
        snippet = json.dumps(history[-6:], default=str, ensure_ascii=False)
        memory.consolidate_memory(snippet)
