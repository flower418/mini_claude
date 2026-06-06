# ── Skill registry & system prompt ─────────────────────
import hashlib
import yaml

from config import SKILLS_DIR, REPO_DIR

SKILL_REGISTRY: dict[str, dict] = {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


def _scan_skills():
    """Scan skills/ dir, populate SKILL_REGISTRY."""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}


_scan_skills()


def list_skills() -> str:
    """List all skills (name + one-line description)."""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(
        f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values()
    )


SUB_SYSTEM = (
    f"You are a coding agent at {REPO_DIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ── System prompt (cached, composable) ─────────────────

_system_cache: dict[str, str] = {}


def _build_identity() -> str:
    return (
        "You are a coding agent. You help users with software engineering tasks "
        "by reading, writing, and editing code, running shell commands, and searching files. "
        "Always follow existing code conventions and security best practices."
    )


def _build_workspace() -> str:
    return f"Working directory: {REPO_DIR}"


def _build_tools() -> str:
    import tools
    tool_names = ", ".join(t["name"] for t in tools.TOOLS)
    catalog = list_skills()
    return (
        f"Available tools: {tool_names}\n\n"
        f"## Skills\n{catalog}\n"
        f"Use load_skill(name) to get full skill details when needed.\n"
        f"Call memory_search(query) BEFORE responding to check for user preferences.\n"
        f"Agent team: you are 'lead'. Sub-agents reply to 'lead', not 'default'.\n"
        f"When all sub-agent work is done, call kill_agent for each spawned agent to clean up .agents/."
    )


def _build_memory() -> str:
    from memory import get_memory_index
    index = get_memory_index()
    return (
        f"{index}\n"
        f"IMPORTANT: Before every user request, call memory_search to check "
        f"for relevant preferences, feedback, or project context."
    )


def get_system_prompt() -> str:
    """Build system prompt from cached sections. Only rebuilds when inputs change."""
    from memory import get_memory_index, has_entries
    import tools

    memory_index = get_memory_index()
    has_memory = has_entries()

    cache_key_parts = [
        str(REPO_DIR),
        str(len(tools.TOOLS)),
        str(sorted(t["name"] for t in tools.TOOLS)),
        hashlib.md5(memory_index.encode()).hexdigest() if has_memory else "no-memory",
    ]
    cache_key = "|".join(cache_key_parts)

    if _system_cache.get("key") == cache_key:
        print(f"\033[90m[system] cache hit ({len(_system_cache['prompt'])} chars)\033[0m")
        return _system_cache["prompt"]

    sections = {
        "identity":  _build_identity(),
        "workspace": _build_workspace(),
        "tools":     _build_tools(),
    }
    if has_memory:
        sections["memory"] = _build_memory()

    prompt = "\n\n".join(f"## {k}\n{v}" for k, v in sections.items())

    _system_cache["key"] = cache_key
    _system_cache["prompt"] = prompt
    section_names = ", ".join(sections.keys())
    print(f"\033[90m[system] assembled sections: {section_names} ({len(prompt)} chars)\033[0m")
    return prompt


def load_skill(name: str) -> str:
    """Load full skill content. Lookup via registry — no path traversal."""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]
