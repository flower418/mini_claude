# ── Memory system ──────────────────────────────────────
# Storage: .memory/<entry-name>/entry.md (YAML frontmatter + body)
# Index:   .memory/MEMORY.md (auto-generated catalog)
import re
import yaml

from config import MEMORY_DIR, MEMORY_PRUNE_THRESHOLD, client, MODEL

MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = ["user", "feedback", "project", "reference"]
TYPE_DESCRIPTIONS = {
    "user":      "User preferences, coding style, and personal context",
    "feedback":  "User feedback on the agent's work and corrections",
    "project":   "Project-specific knowledge, conventions, and architecture",
    "reference": "Reference information, facts, and learned knowledge",
}


# ── Init ────────────────────────────────────────────────

def init_memory():
    """Create .memory directory and index if they don't exist."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    if not MEMORY_INDEX.exists():
        _rebuild_index()


# ── Entry helpers ───────────────────────────────────────

def _read_entry(name: str) -> dict | None:
    """Read an entry's frontmatter + body. Returns None if not found."""
    path = MEMORY_DIR / name / "entry.md"
    if not path.exists():
        # legacy: try flat file
        for t in MEMORY_TYPES:
            flat = MEMORY_DIR / f"{t}.md"
            if flat.exists() and name == t:
                meta = {"name": t, "type": t, "description": TYPE_DESCRIPTIONS[t]}
                meta["body"] = flat.read_text()
                return meta
        return None
    raw = path.read_text()
    return _parse_frontmatter(raw)


def _parse_frontmatter(text: str) -> dict | None:
    """Parse YAML frontmatter from entry.md. Returns {name, type, description, body}."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    meta["body"] = parts[2].strip()
    return meta


def _list_entries() -> list[dict]:
    """List all memory entries with their frontmatter."""
    entries = []
    if not MEMORY_DIR.exists():
        return entries
    for d in sorted(MEMORY_DIR.iterdir()):
        if not d.is_dir():
            continue
        entry = _read_entry(d.name)
        if entry:
            entries.append(entry)
    return entries


def _slugify(text: str) -> str:
    """Generate a safe directory name from text."""
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text.lower()).strip("-")
    return slug[:60] or "entry"


# ── Index ───────────────────────────────────────────────

def _rebuild_index():
    """Rebuild MEMORY.md from all entry directories."""
    entries = _list_entries()
    lines = ["# Memory Index\n"]
    if not entries:
        for t in MEMORY_TYPES:
            lines.append(f"\n## {t}\n({TYPE_DESCRIPTIONS[t]})\n")
    else:
        grouped = {t: [] for t in MEMORY_TYPES}
        for e in entries:
            t = e.get("type", "reference")
            if t in grouped:
                grouped[t].append(e)
        for t in MEMORY_TYPES:
            group = grouped[t]
            lines.append(f"\n## {t}")
            if not group:
                lines.append(f"({TYPE_DESCRIPTIONS[t]} — empty)")
            else:
                for e in group:
                    lines.append(f"- **{e['name']}**: {e.get('description', '')}")
    lines.append("")
    MEMORY_INDEX.write_text("\n".join(lines))


def get_memory_index() -> str:
    """Return content of MEMORY.md for system prompt injection."""
    if not MEMORY_INDEX.exists():
        init_memory()
    return MEMORY_INDEX.read_text()


# ── Search ──────────────────────────────────────────────

def search_memory(query: str) -> str:
    """Search all memory entries for relevant info. Called via memory_search tool."""
    if not query.strip():
        return "Please provide a search query."
    query_lower = query.lower()
    keywords = [w for w in query_lower.split() if len(w) > 1]
    entries = _list_entries()
    results = []
    for e in entries:
        text = f"{e.get('name','')} {e.get('description','')} {e.get('body','')}".lower()
        score = sum(1 for kw in keywords if kw in text) if keywords else 1
        if score > 0:
            results.append((score, e))
    if not results:
        return f"No memory matched query: {query}"
    results.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, e in results:
        body = e.get("body", "")[:3000]
        out.append(
            f"## {e['name']} (type: {e.get('type','?')})\n"
            f"_{e.get('description', '')}_\n\n{body}"
        )
    return "\n\n---\n\n".join(out)


# ── Write ───────────────────────────────────────────────

def write_memory(name: str, description: str, mem_type: str, content: str) -> str:
    """Create or update a memory entry. Called via memory_write tool."""
    mem_type = mem_type.lower().strip()
    if mem_type not in MEMORY_TYPES:
        return f"Invalid type: {mem_type}. Valid: {', '.join(MEMORY_TYPES)}"
    name = name.strip()
    description = description.strip()
    content = content.strip()
    if not name or not content or len(content) < 10:
        return "Name and content required (content ≥ 10 chars)."
    if len(description) > 200:
        description = description[:200] + "..."

    slug = _slugify(name)
    entry_dir = MEMORY_DIR / slug
    entry_dir.mkdir(parents=True, exist_ok=True)

    entry_md = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"type: {mem_type}\n"
        f"---\n\n"
        f"{content}\n"
    )
    entry_file = entry_dir / "entry.md"

    if entry_file.exists():
        existing = entry_file.read_text()
        new_lines = set(content.lower().split("\n"))
        exist_lines = set(existing.lower().split("\n"))
        if len(new_lines & exist_lines) / max(len(new_lines), 1) > 0.6:
            return f"Content largely overlaps existing entry '{slug}' (skipped)"

    entry_file.write_text(entry_md)
    _rebuild_index()
    print(f"\033[90m[memory] Wrote entry: {slug}\033[0m")
    return f"Saved to .memory/{slug}/entry.md (type: {mem_type})"


# ── Consolidation ───────────────────────────────────────

def consolidate_memory(conversation_snippet: str):
    """After a conversation turn, ask LLM to judge and extract new entries."""
    prompt = (
        "You are a memory gatekeeper. Analyze this conversation and decide whether "
        "the USER explicitly stated anything worth remembering long-term.\n\n"
        "WHAT TO EXTRACT — only from the USER's own words:\n"
        "- user: stated preferences, coding style, personal context\n"
        "- feedback: corrections, satisfaction, criticism about the agent's work\n"
        "- project: facts about the codebase, conventions, architecture discussed\n"
        "- reference: URLs, technical facts mentioned in conversation\n\n"
        "CRITICAL — DO NOT EXTRACT:\n"
        "- Anything from tool commands or tool outputs\n"
        "- Code style inferred from files the agent created\n"
        "- File paths, usernames, or environment details\n"
        "- Vague requests with no specifics\n\n"
        "FORMAT EACH ENTRY AS:\n"
        "<<<ENTRY>>>\n"
        "name: <slug-like-name>\n"
        "description: <one-line summary>\n"
        "type: <user|feedback|project|reference>\n"
        "<<<BODY>>>\n"
        "<markdown content with Why/How to apply if relevant>\n"
        "<<<END>>>\n\n"
        "If nothing worth remembering, output only: NO_NEW_INFO\n\n"
        f"CONVERSATION:\n{conversation_snippet[-8000:]}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
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
    """Parse <<<ENTRY>>> blocks and create entry directories."""
    pattern = r"<<<ENTRY>>>\s*\n(.*?)<<<BODY>>>\s*\n(.*?)\n<<<END>>>"
    matches = re.findall(pattern, text, re.DOTALL)
    updated = False

    for meta_str, body in matches:
        meta = {}
        for line in meta_str.strip().split("\n"):
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        name = meta.get("name", "")
        description = meta.get("description", "")
        mem_type = meta.get("type", "reference")
        body = body.strip()

        if not name or not body or len(body) < 20:
            continue
        if mem_type not in MEMORY_TYPES:
            continue

        slug = _slugify(name)
        entry_dir = MEMORY_DIR / slug
        entry_dir.mkdir(parents=True, exist_ok=True)
        entry_file = entry_dir / "entry.md"

        # dedup
        if entry_file.exists():
            existing = entry_file.read_text().lower()
            if body.lower()[:120] in existing:
                continue

        entry_md = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {mem_type}\n"
            f"---\n\n"
            f"{body}\n"
        )
        entry_file.write_text(entry_md)
        print(f"\033[90m[memory] Created entry: {slug}\033[0m")
        updated = True

    if updated:
        _rebuild_index()
        # prune oversized entries
        for entry in _list_entries():
            if len(entry.get("body", "")) > MEMORY_PRUNE_THRESHOLD:
                _prune_entry(entry["name"])


# ── Pruning ─────────────────────────────────────────────

def _prune_entry(name: str):
    """Prune redundant content from a single memory entry."""
    entry = _read_entry(name)
    if not entry:
        return
    body = entry.get("body", "")
    if len(body) <= MEMORY_PRUNE_THRESHOLD:
        return

    prompt = (
        f"Prune this memory entry by removing redundant or duplicate information. "
        f"Keep all unique, valuable facts. Merge similar points. Output only the cleaned body "
        f"(no frontmatter).\n\n"
        f"ENTRY: {name}\nDESCRIPTION: {entry.get('description','')}\n\n"
        f"BODY:\n{body}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )
        pruned = "\n".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
    except Exception:
        return

    if pruned and len(pruned) < len(body) * 0.9:
        slug = _slugify(name)
        entry_file = MEMORY_DIR / slug / "entry.md"
        new_md = (
            f"---\n"
            f"name: {entry['name']}\n"
            f"description: {entry.get('description','')}\n"
            f"type: {entry.get('type','reference')}\n"
            f"---\n\n"
            f"{pruned}\n"
        )
        entry_file.write_text(new_md)
        print(f"\033[90m[memory] Pruned {name} ({len(body)} -> {len(pruned)} chars)\033[0m")
        _rebuild_index()
