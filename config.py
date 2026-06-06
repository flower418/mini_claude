# ── Environment & configuration ────────────────────────
import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import dotenv_values

REPO_DIR = Path(__file__).resolve().parent
SKILLS_DIR = REPO_DIR / "skills"
ENV_FILE = REPO_DIR / ".env"
TOOL_RESULTS_DIR = REPO_DIR / ".task_outputs" / "tool-results"
TRANSCRIPT_DIR = REPO_DIR / ".transcripts"
MEMORY_DIR = REPO_DIR / ".memory"
MEMORY_PRUNE_THRESHOLD = 6000
TASK_DIR = REPO_DIR / ".task"
SCHEDULE_DIR = REPO_DIR / ".schedule"
AGENTS_DIR = REPO_DIR / ".agents"

CONTEXT_LIMIT = 100000
KEEP_RECENT = 5
PERSIST_THRESHOLD = 50000

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


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
MODEL_FALLBACK = CONFIG.get("MODEL_FALLBACK_ID")  # optional lighter model for 529 fallback


def safe_path(p: str) -> Path:
    """Resolve and sandbox a path within REPO_DIR."""
    path = (REPO_DIR / p).resolve()
    if not path.is_relative_to(REPO_DIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def extract_text(content) -> str:
    """Extract plain text from Anthropic message content blocks."""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text"
    )
