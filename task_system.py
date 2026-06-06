# ── Recoverable task system with dependency graph ───────
import hashlib
import json
import re
import threading
import time

from config import TASK_DIR

_io_lock = threading.Lock()


def _task_path(task_id: str):
    return TASK_DIR / f"{task_id}.json"


def _load_task(task_id: str) -> dict | None:
    path = _task_path(task_id)
    with _io_lock:
        if not path.exists():
            return None
        return json.loads(path.read_text())


def _save_task(task: dict):
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    data = json.dumps(task, indent=2, ensure_ascii=False)
    with _io_lock:
        _task_path(task["id"]).write_text(data)


def _generate_id(subject: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:40]
    suffix = hashlib.md5(f"{subject}{time.time()}".encode()).hexdigest()[:4]
    return f"{slug}-{suffix}"


def _load_all_tasks() -> list[dict]:
    if not TASK_DIR.exists():
        return []
    tasks = []
    for f in sorted(TASK_DIR.glob("*.json")):
        try:
            tasks.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            continue
    return tasks


# ── Tool handlers ───────────────────────────────────────

def run_create_task(subject: str, description: str = "", blockedBy: list[str] | None = None) -> str:
    """Create a new task, persist to .task/{id}.json."""
    if not subject.strip():
        return "Error: subject is required"
    if subject.strip() == "":
        return "Error: subject cannot be empty"
    blockedBy = blockedBy or []
    # Validate blockedBy IDs
    for bid in blockedBy:
        if _load_task(bid) is None:
            return f"Error: blockedBy task '{bid}' not found"
    task_id = _generate_id(subject)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    task = {
        "id": task_id,
        "subject": subject.strip(),
        "description": description.strip(),
        "status": "pending",
        "owner": None,
        "blockedBy": blockedBy,
        "created_at": now,
        "updated_at": now,
    }
    _save_task(task)
    print(f"\033[90m[task] Created: {task_id}\033[0m")
    return f"Created task '{task_id}': {subject}"


def run_claim_task(task_id: str, owner: str = "agent") -> str:
    """Claim a task (check dependencies first)."""
    task = _load_task(task_id)
    if task is None:
        return f"Error: task '{task_id}' not found"
    if task["status"] != "pending":
        return f"Error: task '{task_id}' is already {task['status']}"

    # Check all blockedBy tasks are completed
    unmet = []
    for bid in task.get("blockedBy", []):
        dep = _load_task(bid)
        if dep is None:
            unmet.append(f"{bid} (not found)")
        elif dep["status"] != "completed":
            unmet.append(f"{bid} ({dep['status']})")
    if unmet:
        return f"Error: unmet dependencies — {', '.join(unmet)}"

    task["status"] = "in_progress"
    task["owner"] = owner
    task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save_task(task)
    print(f"\033[90m[task] Claimed: {task_id} by {owner}\033[0m")
    return f"Claimed task '{task_id}': {task['subject']}"


def run_complete_task(task_id: str) -> str:
    """Mark a task as completed."""
    task = _load_task(task_id)
    if task is None:
        return f"Error: task '{task_id}' not found"
    if task["status"] != "in_progress":
        return f"Error: task '{task_id}' is {task['status']}, not in_progress"

    task["status"] = "completed"
    task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save_task(task)

    # Find newly unblocked tasks
    unblocked = []
    for t in _load_all_tasks():
        if task_id in t.get("blockedBy", []) and t["status"] == "pending":
            still_blocked = any(
                b != task_id and _load_task(b) and _load_task(b)["status"] != "completed"
                for b in t["blockedBy"]
            )
            if not still_blocked:
                unblocked.append(t["id"])

    msg = f"Completed task '{task_id}': {task['subject']}"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
    print(f"\033[90m[task] Completed: {task_id}\033[0m")
    return msg


def run_list_tasks(status: str | None = None) -> str:
    """Show task summary with status icons and dependency arrows."""
    tasks = _load_all_tasks()
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    if not tasks:
        label = f" with status '{status}'" if status else ""
        return f"(no tasks{label})"

    lines = []
    for t in tasks:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        owner_str = f" [{t['owner']}]" if t.get("owner") else ""
        blocked_str = f" \033[90m⬅ {', '.join(t['blockedBy'])}\033[0m" if t.get("blockedBy") else ""
        lines.append(f"  [{icon}] {t['id']}: {t['subject']}{owner_str}{blocked_str}")

    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    """Get full details of a task, including dependents."""
    task = _load_task(task_id)
    if task is None:
        return f"Error: task '{task_id}' not found"

    dependents = [t["id"] for t in _load_all_tasks() if task_id in t.get("blockedBy", [])]

    lines = [
        f"id: {task['id']}",
        f"subject: {task['subject']}",
        f"description: {task['description'] or '(none)'}",
        f"status: {task['status']}",
        f"owner: {task['owner'] or '(unclaimed)'}",
        f"blockedBy: {', '.join(task['blockedBy']) if task['blockedBy'] else '(none)'}",
        f"dependents: {', '.join(dependents) if dependents else '(none)'}",
        f"created_at: {task.get('created_at', '?')}",
        f"updated_at: {task.get('updated_at', '?')}",
    ]
    return "\n".join(lines)
