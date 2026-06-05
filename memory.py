# ── Memory system ──────────────────────────────────────
import json
import re

from config import MEMORY_DIR, MEMORY_PRUNE_THRESHOLD, client, MODEL

MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = {
    "user":      "User preferences, coding style, and personal context",
    "feedback":  "User feedback on the agent's work and corrections",
    "project":   "Project-specific knowledge, conventions, and architecture",
    "reference": "Reference information, facts, and learned knowledge",
}


# ── Init ────────────────────────────────────────────────

def init_memory():
    """Create .memory directory and default files if they don't exist."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    for name, desc in MEMORY_TYPES.items():
        path = MEMORY_DIR / f"{name}.md"
        if not path.exists():
            path.write_text(f"# {name}\n\n{desc}\n")
    _rebuild_index()


# ── Index ───────────────────────────────────────────────

def _rebuild_index():
    """Rebuild MEMORY.md from actual memory files."""
    lines = ["# Memory Index\n"]
    for name, desc in MEMORY_TYPES.items():
        path = MEMORY_DIR / f"{name}.md"
        if not path.exists():
            continue
        content = path.read_text()
        line_count = len([l for l in content.split("\n") if l.strip() and not l.startswith("#")])
        size_hint = ""
        if line_count <= 1:
            size_hint = "(empty)"
        elif line_count <= 5:
            size_hint = f"({line_count} entries)"
        else:
            size_hint = f"({line_count} entries, {len(content)} chars)"
        lines.append(f"- **{name}.md**: {desc} {size_hint}")
    MEMORY_INDEX.write_text("\n".join(lines) + "\n")


def get_memory_index() -> str:
    """Return content of MEMORY.md for system prompt injection."""
    if not MEMORY_INDEX.exists():
        init_memory()
    return MEMORY_INDEX.read_text()


# ── Search ──────────────────────────────────────────────

def search_memory(query: str) -> str:
    """Search all memory files for relevant info. Called via memory_search tool."""
    if not query.strip():
        return "Please provide a search query."
    query_lower = query.lower()
    results = []
    for name in MEMORY_TYPES:
        path = MEMORY_DIR / f"{name}.md"
        if not path.exists():
            continue
        content = path.read_text()
        # keyword overlap scoring
        keywords = [w for w in query_lower.split() if len(w) > 1]
        if not keywords:
            results.append(f"### {name}.md\n\n{content[:2000]}")
            continue
        score = sum(1 for kw in keywords if kw in content.lower())
        if score > 0:
            results.append(f"### {name}.md (relevance: {score})\n\n{content[:3000]}")
    if not results:
        return f"No memory matched query: {query}"
    return "\n\n---\n\n".join(results)


# ── Write ───────────────────────────────────────────────

def write_memory(mem_type: str, content: str) -> str:
    """Write content to a memory file. Called via memory_write tool."""
    mem_type = mem_type.lower().strip()
    if mem_type not in MEMORY_TYPES:
        return f"Invalid memory type: {mem_type}. Valid types: {', '.join(MEMORY_TYPES)}"
    content = content.strip()
    if not content:
        return "No content provided."
    if len(content) < 10:
        return "Content too short to be meaningful."

    path = MEMORY_DIR / f"{mem_type}.md"
    existing = path.read_text() if path.exists() else ""

    # dedup
    new_lines = [l.strip() for l in content.split("\n") if l.strip()]
    existing_lower = existing.lower()
    overlap = sum(1 for line in new_lines if line.lower()[:60] in existing_lower)
    if new_lines and overlap / len(new_lines) > 0.6:
        return f"Content already exists in {mem_type}.md (skipped)"

    path.write_text(existing.rstrip() + f"\n\n{content}\n")
    _rebuild_index()
    print(f"\033[90m[memory] Wrote to {mem_type}.md\033[0m")
    return f"Saved to .memory/{mem_type}.md"


# ── Consolidation ───────────────────────────────────────

def consolidate_memory(conversation_snippet: str):
    """After a conversation turn, ask LLM to judge and extract new knowledge."""
    prompt = (
        "You are a memory gatekeeper. Analyze this conversation and decide whether "
        "the USER explicitly stated anything worth remembering long-term.\n\n"
        "WHAT TO EXTRACT — only from the USER's own words:\n"
        "- user: stated preferences, coding style, personal context\n"
        "- feedback: corrections, satisfaction, criticism about the agent's work\n"
        "- project: facts about the codebase, conventions, architecture discussed\n"
        "- reference: URLs, technical facts mentioned in conversation\n\n"
        "CRITICAL — DO NOT EXTRACT:\n"
        "- Anything from tool commands or tool outputs (write_file content, bash output, etc.)\n"
        "- Code style inferred from files the agent created (the agent chooses style, not the user)\n"
        "- File paths, usernames, or environment details\n"
        "- Vague statements like 'write a python program' with no specifics\n"
        "- Information the agent generated (only what the USER said)\n\n"
        "EXAMPLES of what to SKIP (output NO_NEW_INFO):\n"
        "- User: 'create test.py' → Agent: writes file with tabs → SKIP (user didn't say anything about tabs)\n"
        "- User: '写个程序' → Agent: asks clarifying question → SKIP (no preference stated)\n\n"
        "EXAMPLES of what to KEEP:\n"
        "- User: 'I prefer tabs, not spaces' → user: Prefers tabs for indentation\n"
        "- User: '简单写写，我就做个小测试' → user: 偏好简单的小程序用于测试\n\n"
        "Output NO_NEW_INFO if nothing matches the KEEP criteria. "
        "Otherwise format as:\n"
        "---TYPE: <category>---\n"
        "<markdown bullet points>\n"
        "---END---\n\n"
        f"CONVERSATION:\n{conversation_snippet[-8000:]}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        result = "\n".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
    except Exception as e:
        print(f"\033[90m[memory] Consolidation skipped (API error: {e})\033[0m")
        return

    if not result or "NO_NEW_INFO" in result:
        return

    _parse_and_save(result)


def _parse_and_save(text: str):
    """Parse LLM consolidation output and append to memory files."""
    pattern = r"---TYPE:\s*(\w+)---\s*\n(.*?)\n---END---"
    matches = re.findall(pattern, text, re.DOTALL)
    updated = False

    for mem_type, content in matches:
        mem_type = mem_type.lower().strip()
        if mem_type not in MEMORY_TYPES:
            continue
        content = content.strip()
        if not content or len(content) < 20:
            continue

        path = MEMORY_DIR / f"{mem_type}.md"
        existing = path.read_text() if path.exists() else ""

        # dedup: skip if substantial content overlap (>60% of lines already present)
        new_lines = [l.strip() for l in content.split("\n") if l.strip() and not l.startswith("#")]
        if not new_lines:
            continue
        existing_lower = existing.lower()
        overlap = sum(1 for line in new_lines if line.lower()[:60] in existing_lower)
        if overlap / len(new_lines) > 0.6:
            continue

        path.write_text(existing.rstrip() + f"\n\n{content}\n")
        print(f"\033[90m[memory] Updated {mem_type}.md\033[0m")
        updated = True

    if updated:
        _rebuild_index()
        for name in MEMORY_TYPES:
            path = MEMORY_DIR / f"{name}.md"
            if path.exists() and len(path.read_text()) > MEMORY_PRUNE_THRESHOLD:
                _prune_file(name)


# ── Pruning ─────────────────────────────────────────────

def _prune_file(mem_type: str):
    """Prune redundant content from a single memory file."""
    path = MEMORY_DIR / f"{mem_type}.md"
    content = path.read_text()

    prompt = (
        f"Prune this memory file by consolidating redundant or duplicate information. "
        f"Keep all unique, valuable facts. Merge similar items. Remove obsolete info. "
        f"Output the cleaned version in the same markdown format.\n\n"
        f"ORIGINAL ({mem_type}.md):\n{content}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
        )
        pruned = "\n".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
    except Exception as e:
        print(f"\033[90m[memory] Pruning skipped for {mem_type}.md (API error: {e})\033[0m")
        return

    if pruned and len(pruned) < len(content) * 0.9:
        path.write_text(pruned)
        print(f"\033[90m[memory] Pruned {mem_type}.md ({len(content)} -> {len(pruned)} chars)\033[0m")
    elif pruned:
        path.write_text(pruned)
        print(f"\033[90m[memory] Refreshed {mem_type}.md\033[0m")

    _rebuild_index()
