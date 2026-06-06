# ── Runtime state primitives ─────────────────────────────
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class IdentifierPolicy:
    """Normalize and validate filesystem-backed runtime identifiers."""

    label: str
    pattern: str
    lower: bool = False
    replace_spaces: bool = False
    aliases: dict[str, str] = field(default_factory=dict)

    def normalize(self, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"invalid {self.label}: {value!r}")
        normalized = value.strip()
        if self.replace_spaces:
            normalized = normalized.replace(" ", "-")
        if self.lower:
            normalized = normalized.lower()
        normalized = self.aliases.get(normalized, normalized)
        if not re.fullmatch(self.pattern, normalized):
            raise ValueError(f"invalid {self.label}: {normalized!r}")
        return normalized

    def is_valid(self, value: str) -> bool:
        try:
            self.normalize(value)
            return True
        except ValueError:
            return False


TASK_ID_POLICY = IdentifierPolicy("task id", r"^[a-z0-9][a-z0-9-]{0,120}$")
SCHEDULE_ID_POLICY = IdentifierPolicy("schedule id", r"^[A-Za-z0-9][A-Za-z0-9_-]{0,120}$")
AGENT_NAME_POLICY = IdentifierPolicy(
    "agent name",
    r"^[a-z0-9][a-z0-9-]{0,80}$",
    lower=True,
    replace_spaces=True,
    aliases={"default": "lead", "main": "lead", "orchestrator": "lead"},
)


def slugify_ascii(text: str, max_len: int = 40, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len]
    return slug or fallback


def atomic_write_text(path: Path, text: str):
    """Write text via same-directory replace to avoid torn state files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_json_file(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json_file(path: Path, payload: Any):
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))


def append_jsonl(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


class JsonDirStore:
    """Small JSON document store keyed by validated identifiers."""

    def __init__(self, root: Path, policy: IdentifierPolicy, suffix: str = ".json"):
        self.root = root
        self.policy = policy
        self.suffix = suffix

    def normalize_id(self, identifier: str) -> str:
        return self.policy.normalize(identifier)

    def path(self, identifier: str) -> Path:
        return self.root / f"{self.normalize_id(identifier)}{self.suffix}"

    def get(self, identifier: str) -> dict | None:
        try:
            path = self.path(identifier)
        except ValueError:
            return None
        return read_json_file(path)

    def write(self, identifier: str, payload: dict):
        write_json_file(self.path(identifier), payload)

    def delete(self, identifier: str) -> bool:
        try:
            path = self.path(identifier)
        except ValueError:
            return False
        if not path.exists():
            return False
        path.unlink()
        return True

    def list(self, factory: Callable[[dict], Any] | None = None) -> list[Any]:
        if not self.root.exists():
            return []
        items = []
        for path in sorted(self.root.glob(f"*{self.suffix}")):
            if not self.policy.is_valid(path.stem):
                continue
            payload = read_json_file(path)
            if payload is None:
                continue
            if factory:
                try:
                    payload = factory(payload)
                except (TypeError, ValueError):
                    continue
            items.append(payload)
        return items
