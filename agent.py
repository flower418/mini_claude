# ── Agent entry point & main loop ───────────────────────
import json
import select
import sys
import time

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

from config import MODEL, MODEL_FALLBACK, client, extract_text
from skills import get_system_prompt
import tools
import background
import scheduler
import agent_team
agent_team.cleanup_stale()
from compact import preprocess_pipeline, estimate_size, compact_history, emergency_compact, CONTEXT_LIMIT
from hooks import trigger_hooks, init_hooks

init_hooks()


def _is_retriable(e: Exception) -> bool:
    """Check if error is transient: network issues, rate limits, server 5xx."""
    err = str(e).lower()
    if any(kw in err for kw in ("connection", "timeout", "reset", "refused", "broken pipe")):
        return True
    if hasattr(e, "status_code"):
        code = e.status_code
        if code == 429 or (500 <= code < 600):
            return True
    if any(kw in err for kw in ("rate limit", "server error", "overloaded", "internal error")):
        return True
    return False


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
            if memory.has_entries():
                index = memory.get_memory_index()
                messages.append({"role": "user",
                                 "content": f"<reminder>Check memory before proceeding:\n{index}\nUse memory_search if relevant.</reminder>"})
                tools.rounds_since_memory = 0

        # ── Compact pipeline: preprocessing → LLM compact → API call ──
        messages[:] = preprocess_pipeline(messages)

        if estimate_size(json.dumps(messages, default=str)) > CONTEXT_LIMIT:
            print(f"\033[33m[compact] Context over limit, running LLM compaction...\033[0m")
            messages[:] = compact_history(messages)

        # ── Collect background results ──────────────────
        bg_results = background.collect()
        if bg_results:
            for task_id, result in bg_results:
                messages.append({"role": "user",
                                 "content": f"<background-result task_id=\"{task_id}\">\n{result}\n</background-result>"})

        # ── Collect agent team mail ─────────────────────
        team_msgs = agent_team.Mailbox("lead").read_all()
        if team_msgs:
            for m in team_msgs:
                body = m['body']
                if len(body) > 2000:
                    body = body[:2000] + f"\n... (truncated, {len(m['body'])} chars total)"
                messages.append({"role": "user",
                                 "content": f"<agent-mail from=\"{m['from']}\">\n{body}\n</agent-mail>"})

        # ── API call with 3-tier retry logic ─────────────
        max_tokens = 8000
        max_tokens_retries = 0
        current_model = MODEL

        while True:  # max_tokens expansion loop
            server_retries = 0
            prompt_compacted = False

            while True:  # server retry loop
                try:
                    response = client.messages.create(
                        model=current_model, system=get_system_prompt(), messages=messages,
                        tools=tools.TOOLS, max_tokens=max_tokens,
                    )
                    break
                except Exception as e:
                    err = str(e).lower()
                    # Category 2: prompt too long → emergency compact → retry once
                    if "prompt" in err and ("too long" in err or "too large" in err or "exceed" in err):
                        if prompt_compacted:
                            print("\033[31m[error] Prompt still too long after emergency compact\033[0m")
                            raise
                        messages[:] = emergency_compact(messages)
                        if estimate_size(json.dumps(messages, default=str)) > CONTEXT_LIMIT * 2:
                            print("\033[31m[error] Prompt too large even for emergency compact\033[0m")
                            raise
                        prompt_compacted = True
                        continue
                    # Category 4: 529 overloaded → fallback to lighter model
                    if hasattr(e, "status_code") and e.status_code == 529:
                        if MODEL_FALLBACK and current_model != MODEL_FALLBACK:
                            print(f"\033[33m[retry] Server overloaded (529), switching from {current_model} to {MODEL_FALLBACK}\033[0m")
                            current_model = MODEL_FALLBACK
                            continue
                        # no fallback configured, or already on fallback → treat as regular server error
                    # Category 3: server/network issues → exponential backoff
                    if _is_retriable(e):
                        if server_retries >= 10:
                            print(f"\033[31m[error] Server retries exhausted ({server_retries} attempts)\033[0m")
                            raise
                        wait = 0.5 * (2 ** server_retries)
                        print(f"\033[33m[retry] Server error in {wait:.1f}s (attempt {server_retries + 1}/10): {str(e)[:80]}\033[0m")
                        time.sleep(wait)
                        server_retries += 1
                        continue
                    raise

            # Category 1: output truncated by max_tokens → expand 4x, retry up to 3
            if response.stop_reason == "max_tokens":
                if max_tokens_retries < 3:
                    max_tokens *= 4
                    max_tokens_retries += 1
                    print(f"\033[33m[retry] Output truncated, expanding max_tokens to {max_tokens} (attempt {max_tokens_retries}/3)\033[0m")
                    continue
                else:
                    print(f"\033[33m[warn] Output still truncated after {max_tokens_retries} expansions, proceeding with partial response\033[0m")
            break  # success (or gave up on truncation)

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
    scheduler.start()

    history = []
    while True:
        # Layer 3: check scheduler queue first
        job = scheduler.dequeue()
        if job:
            query = f"[Scheduled: {job.id}]\n{job.prompt}"
            print(f"\n\033[33m[Scheduled] {job.id}\033[0m")
        else:
            # Wait for user input with 1s timeout to re-check queue
            print("\033[36m>> \033[0m", end="", flush=True)
            query = None
            while query is None:
                if select.select([sys.stdin], [], [], 1.0)[0]:
                    try:
                        query = sys.stdin.readline().rstrip("\n")
                    except (EOFError, KeyboardInterrupt):
                        break
                else:
                    job = scheduler.dequeue()
                    if job:
                        query = f"[Scheduled: {job.id}]\n{job.prompt}"
                        print(f"\n\033[33m[Scheduled] {job.id}\033[0m")
                        break
            if query is None:
                break

        if query.strip().lower() in ("q", "exit"):
            agent_team.cleanup_stale()  # clean lead's own inbox on exit
            break
        if not query.strip():
            continue

        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        tools.rounds_since_memory = 0  # new query → encourage fresh memory check
        agent_loop(history)

        # Run any additional queued tasks that arrived during processing
        while True:
            job = scheduler.dequeue()
            if not job:
                break
            query = f"[Scheduled: {job.id}]\n{job.prompt}"
            print(f"\n\033[33m[Scheduled] {job.id}\033[0m")
            history.append({"role": "user", "content": query})
            agent_loop(history)

        # ── Collect orphaned background results ─────────
        orphans = background.collect()
        if orphans:
            for task_id, result in orphans:
                print(f"  \033[90m── {task_id} ──\033[0m\n{result[:500]}")
                if len(result) > 500:
                    print(f"  \033[90m... ({len(result)} chars total)\033[0m")
                history.append({"role": "user",
                                "content": f"<background-result task_id=\"{task_id}\">\n{result}\n</background-result>"})
            print()

        # ── Collect orphaned team mail ───────────────────
        orphan_mail = agent_team.Mailbox("lead").read_all()
        if orphan_mail:
            for m in orphan_mail:
                body = m['body']
                if len(body) > 2000:
                    body = body[:2000] + f"\n... (truncated, {len(m['body'])} chars total)"
                history.append({"role": "user",
                                "content": f"<agent-mail from=\"{m['from']}\">\n{body}\n</agent-mail>"})
            print()

        last = history[-1]["content"]
        if isinstance(last, list):
            for block in last:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()

        # ── Memory consolidation after each turn ─────────
        snippet = json.dumps(history[-6:], default=str, ensure_ascii=False)
        memory.consolidate_memory(snippet)
