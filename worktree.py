# ── Worktree isolation: per-task sandboxed directories ──
# Each task gets its own copy of the repo. Agents work inside it.
# Status: active → done → merged | discarded | conflict
import hashlib
import shutil
from pathlib import Path

from config import REPO_DIR
from state_store import TASK_ID_POLICY, atomic_write_text, read_json_file, write_json_file

WORKTREE_DIR = REPO_DIR / ".worktrees"
MANIFEST_NAME = ".wt_manifest.json"
STATUS_NAME = ".wt_status"
SKIP_NAMES = {".git", ".worktrees", ".task", ".memory", ".schedule", ".agents",
              ".transcripts", ".task_outputs", "__pycache__", ".venv", "venv",
              ".env", MANIFEST_NAME, STATUS_NAME}


def _worktree_path(task_id: str) -> Path:
    return WORKTREE_DIR / TASK_ID_POLICY.normalize(task_id)


def _status_path(task_id: str) -> Path:
    return _worktree_path(task_id) / STATUS_NAME


def _read_status(task_id: str) -> str:
    sp = _status_path(task_id)
    if sp.exists():
        return sp.read_text().strip()
    return "unknown"


def _write_status(task_id: str, status: str):
    atomic_write_text(_status_path(task_id), status)


def _manifest_path(task_id: str) -> Path:
    return _worktree_path(task_id) / MANIFEST_NAME


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_repo_files(root: Path):
    for f in root.rglob("*"):
        rel = f.relative_to(root)
        if not rel.parts:
            continue
        if any(part in SKIP_NAMES for part in rel.parts):
            continue
        if f.is_file():
            yield rel, f


def _write_manifest(task_id: str, root: Path):
    manifest = {str(rel): _file_hash(path) for rel, path in _iter_repo_files(root)}
    write_json_file(_manifest_path(task_id), manifest)


def _read_manifest(task_id: str) -> dict[str, str]:
    return read_json_file(_manifest_path(task_id), {})


def create(task_id: str) -> Path:
    """Copy the repo into .worktrees/{task_id}/. Returns the worktree path."""
    dest = _worktree_path(task_id)
    if dest.exists():
        shutil.rmtree(dest)
    _copy_repo(REPO_DIR, dest)
    _write_manifest(task_id, dest)
    _write_status(task_id, "active")
    print(f"\033[90m[wt] Created: {task_id}\033[0m")
    return dest


def merge(task_id: str) -> str:
    """Copy agent changes back to main repo. Detects true concurrent conflicts."""
    try:
        src = _worktree_path(task_id)
    except ValueError as e:
        return f"Error: {e}"
    if not src.exists():
        return f"Error: worktree '{task_id}' not found"

    manifest = _read_manifest(task_id)
    conflicts = []
    merged = 0
    for rel, f in _iter_repo_files(src):
        rel_str = str(rel)
        target = REPO_DIR / rel
        worktree_hash = _file_hash(f)
        base_hash = manifest.get(rel_str)

        if target.exists():
            target_hash = _file_hash(target)
            if target_hash == worktree_hash:
                continue
            if base_hash is None:
                conflicts.append(rel_str)
                continue
            if worktree_hash == base_hash:
                continue
            if target_hash != base_hash:
                conflicts.append(rel_str)
                continue
            shutil.copy2(f, target)
            merged += 1
        else:
            if base_hash is not None and worktree_hash == base_hash:
                continue
            if base_hash is not None:
                conflicts.append(rel_str)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            merged += 1

    for rel_str, base_hash in manifest.items():
        worktree_file = src / rel_str
        if worktree_file.exists():
            continue
        target = REPO_DIR / rel_str
        if not target.exists():
            continue
        if _file_hash(target) != base_hash:
            conflicts.append(rel_str)
            continue
        target.unlink()
        merged += 1

    if conflicts:
        _write_status(task_id, "conflict")
        return f"CONFLICT in {len(conflicts)} files: {', '.join(conflicts)}. Worktree kept for manual resolution."
    _write_status(task_id, "merged")
    shutil.rmtree(src)
    print(f"\033[90m[wt] Merged: {task_id} ({merged} changes)\033[0m")
    return f"Merged {merged} changes. Worktree cleaned up."


def discard(task_id: str) -> str:
    """Delete the worktree without merging."""
    try:
        src = _worktree_path(task_id)
    except ValueError as e:
        return f"Error: {e}"
    if src.exists():
        shutil.rmtree(src)
    print(f"\033[90m[wt] Discarded: {task_id}\033[0m")
    return f"Discarded worktree '{task_id}'"


def keep(task_id: str) -> str:
    """Mark worktree as done but preserved for review."""
    try:
        _write_status(task_id, "done")
    except ValueError as e:
        return f"Error: {e}"
    return f"Worktree '{task_id}' kept for review (status: done)"


def list_worktrees() -> str:
    if not WORKTREE_DIR.exists():
        return "(no worktrees)"
    lines = []
    for d in sorted(WORKTREE_DIR.iterdir()):
        if not d.is_dir():
            continue
        try:
            st = _read_status(d.name)
        except ValueError:
            continue
        icon = {"active": "\033[36m●\033[0m", "done": "\033[32m✓\033[0m", "conflict": "\033[31m!\033[0m", "merged": "\033[90m-\033[0m"}.get(st, "?")
        lines.append(f"  {icon} {d.name} ({st})")
    return "\n".join(lines) if lines else "(no worktrees)"


def _copy_repo(src: Path, dest: Path):
    """Copy repo files, skipping hidden/local state."""
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in SKIP_NAMES:
            continue
        target = dest / item.name
        if item.is_dir():
            _copy_repo(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
