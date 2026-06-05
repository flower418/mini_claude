# ── Skill registry & system prompt ─────────────────────
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


def build_system() -> str:
    """Build SYSTEM prompt with skill catalog and memory index."""
    from memory import get_memory_index

    catalog = list_skills()
    memory_index = get_memory_index()
    return (
        f"You are a coding agent at {REPO_DIR}.\n\n"
        f"## Memory (check first!)\n{memory_index}\n"
        f"IMPORTANT: Before EVERY user request, call memory_search to check "
        f"for relevant preferences, feedback, or project context. "
        f"Do this even if memory seems empty — it may have been updated.\n\n"
        f"## Skills\n{catalog}\n"
        f"Use load_skill to get full details when needed."
    )


SYSTEM = build_system()


def load_skill(name: str) -> str:
    """Load full skill content. Lookup via registry — no path traversal."""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]
