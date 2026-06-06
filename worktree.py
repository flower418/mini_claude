# ── Worktree isolation: per-task sandboxed directories ──
# Each task gets its own copy of the repo. Agents work inside it.
# Status: active → done → merged | discarded | conflict
import shutil
import time
from pathlib import Path

from config import REPO_DIR, get_workdir

WORKTREE_DIR = REPO_DIR / ".worktrees"


def _status_path(task_id: str) -> Path:
    return WORKTREE_DIR / task_id / ".wt_status"


def _read_status(task_id: str) -> str:
    sp = _status_path(task_id)
    if sp.exists():
        return sp.read_text().strip()
    return "unknown"


def _write_status(task_id: str, status: str):
    sp = _status_path(task_id)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(status)


def create(task_id: str) -> Path:
    """Copy the repo into .worktrees/{task_id}/. Returns the worktree path."""
    dest = WORKTREE_DIR / task_id
    if dest.exists():
        shutil.rmtree(dest)
    _copy_repo(REPO_DIR, dest)
    _write_status(task_id, "active")
    print(f"\033[90m[wt] Created: {task_id}\033[0m")
    return dest


def merge(task_id: str) -> str:
    """Copy modified/new files from worktree back to main repo. Detects conflicts."""
    src = WORKTREE_DIR / task_id
    if not src.exists():
        return f"Error: worktree '{task_id}' not found"

    conflicts = []
    merged = 0
    for f in src.rglob("*"):
        rel = f.relative_to(src)
        if rel.parts[0].startswith("."):
            continue
        if f.is_dir():
            continue
        target = REPO_DIR / rel
        if target.exists():
            if f.read_bytes() != target.read_bytes():
                # Both modified — conflict
                conflicts.append(str(rel))
            # else: identical, skip
        else:
            # New file
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            merged += 1

    if conflicts:
        _write_status(task_id, "conflict")
        return f"CONFLICT in {len(conflicts)} files: {', '.join(conflicts)}. Worktree kept for manual resolution."
    _write_status(task_id, "merged")
    shutil.rmtree(src)
    print(f"\033[90m[wt] Merged: {task_id} ({merged} new files)\033[0m")
    return f"Merged {merged} new files. Worktree cleaned up."


def discard(task_id: str) -> str:
    """Delete the worktree without merging."""
    src = WORKTREE_DIR / task_id
    if src.exists():
        shutil.rmtree(src)
    print(f"\033[90m[wt] Discarded: {task_id}\033[0m")
    return f"Discarded worktree '{task_id}'"


def keep(task_id: str) -> str:
    """Mark worktree as done but preserved for review."""
    _write_status(task_id, "done")
    return f"Worktree '{task_id}' kept for review (status: done)"


def list_worktrees() -> str:
    if not WORKTREE_DIR.exists():
        return "(no worktrees)"
    lines = []
    for d in sorted(WORKTREE_DIR.iterdir()):
        if d.is_dir():
            st = _read_status(d.name)
            icon = {"active": "\033[36m●\033[0m", "done": "\033[32m✓\033[0m", "conflict": "\033[31m!\033[0m", "merged": "\033[90m-\033[0m"}.get(st, "?")
            lines.append(f"  {icon} {d.name} ({st})")
    return "\n".join(lines) if lines else "(no worktrees)"


def _copy_repo(src: Path, dest: Path):
    """Copy repo files, skipping hidden dirs and .worktrees itself."""
    dest.mkdir(parents=True, exist_ok=True)
    skip = {".git", ".worktrees", ".task", ".memory", ".schedule", ".agents",
            ".transcripts", ".task_outputs", "__pycache__", ".venv", "venv"}
    for item in src.iterdir():
        if item.name.startswith(".") and item.name in skip:
            continue
        if item.name.startswith(".") and item.is_dir():
            continue
        target = dest / item.name
        if item.is_dir():
            _copy_repo(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
