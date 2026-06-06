# ── Context compaction pipeline ─────────────────────────
import json
import time

from config import (
    TOOL_RESULTS_DIR, TRANSCRIPT_DIR, PERSIST_THRESHOLD,
    KEEP_RECENT, CONTEXT_LIMIT, client, MODEL,
)


def estimate_size(messages: str) -> int:
    return len(messages)


# ── Level 1: snip middle messages ──────────────────────

def _has_tool_use(msg) -> bool:
    """Check if a message (assistant) contains tool_use blocks."""
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if not isinstance(content, list):
        return False
    return any(getattr(b, "type", None) == "tool_use" for b in content)


def _has_tool_result(msg) -> bool:
    """Check if a message (user) contains tool_result blocks."""
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def snip_compact(messages, max_messages=100):
    """Keep first 3 + last N messages, preserving tool_use/tool_result pairs."""
    if len(messages) <= max_messages:
        return messages

    # Walk backward from the end, counting messages to keep.
    # Tool_use/tool_result pairs count as one unit — never split.
    keep_count = 0
    i = len(messages) - 1
    while i >= 3 and keep_count < max_messages - 3:
        msg = messages[i]
        if _has_tool_result(msg) and i > 0 and _has_tool_use(messages[i - 1]):
            keep_count += 2  # pair stays together
            i -= 2
        else:
            keep_count += 1
            i -= 1

    keep_tail = max(len(messages) - i - 1, 3)
    snipped = len(messages) - 3 - keep_tail
    if snipped <= 0:
        return messages

    return (
        messages[:3]
        + [{"role": "user", "content": f"[snipped {snipped} messages]"}]
        + messages[-keep_tail:]
    )


# ── Level 2: micro-compact old tool results ────────────

def _collect_tool_results(messages):
    """Find all tool_result blocks with their positions."""
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((mi, bi, block))
    return blocks


def micro_compact(messages):
    """Keep only the last KEEP_RECENT tool results intact; compact older ones."""
    tool_results = _collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT:
        return messages
    for _, _, block in tool_results[:-KEEP_RECENT]:
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# ── Level 3: persist large outputs to disk ─────────────

def persist_large_output(tool_use_id, output):
    """Write large tool output to disk, return preview + path."""
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output)
    return (
        f"<persisted-output>\n"
        f"Full output: {path}\n"
        f"Preview:\n{output[:2000]}\n"
        f"</persisted-output>"
    )


def tool_result_budget(messages, max_bytes=200_000):
    """Enforce byte budget on the latest batch of tool results."""
    last = messages[-1] if messages else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list):
        return messages
    blocks = [
        (i, b) for i, b in enumerate(last["content"])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes:
        return messages

    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for _, block in ranked:
        if total <= max_bytes:
            break
        content = str(block.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue
        tid = block.get("tool_use_id", "unknown")
        block["content"] = persist_large_output(tid, content)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages


# ── Preprocessing pipeline ─────────────────────────────

def preprocess_pipeline(messages):
    """3-step preprocessing: persist large outputs → snip middle → micro-compact."""
    messages = tool_result_budget(messages)
    messages = snip_compact(messages)
    messages = micro_compact(messages)
    return messages


# ── Level 4: LLM summarization ─────────────────────────

def _write_transcript(messages):
    """Save full conversation transcript to disk."""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    return path


def _summarize_history(messages):
    """Use LLM to summarize the conversation."""
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
        "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n"
        + conversation
    )
    response = client.messages.create(
        model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000
    )
    return (
        "\n".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
        or "(empty summary)"
    )


def compact_history(messages, focus=None):
    """LLM compaction: save transcript, summarize, return compact messages."""
    transcript_path = _write_transcript(messages)
    print(f"\033[90m[compact] transcript saved: {transcript_path}\033[0m")
    summary = _summarize_history(messages)
    if focus:
        summary = f"Focus: {focus}\n\n{summary}"
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# ── Emergency compact ──────────────────────────────────

def emergency_compact(messages):
    """Last resort: API prompt too long → keep only last N messages."""
    keep = min(5, len(messages))
    print(f"\033[31m[emergency] Prompt too long, keeping only last {keep} messages\033[0m")
    return messages[-keep:]


# ── Compact tool handler ───────────────────────────────

def run_compact(focus: str = None) -> str:
    """Stub handler. Actual compaction is done by agent_loop on detection."""
    return "Context compacted."
