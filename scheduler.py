# ── 4-layer scheduler: thread → queue → processor → execute ──
# Layer 1: Scheduler thread checks every 1s if tasks are due
# Layer 2: Due tasks are pushed to an in-memory queue
# Layer 3: Entry point dequeues and injects when agent is idle
# Layer 4: Agent processes the injected prompt, result goes into history
import hashlib
import json
import threading
import time
from datetime import datetime, timedelta

from config import SCHEDULE_DIR

_queue: list[dict] = []
_lock = threading.Lock()
_running = False
_thread: threading.Thread | None = None


# ── Persistence ─────────────────────────────────────────

def _schedule_path(task_id: str):
    return SCHEDULE_DIR / f"{task_id}.json"


def _load_all() -> list[dict]:
    if not SCHEDULE_DIR.exists():
        return []
    tasks = []
    for f in sorted(SCHEDULE_DIR.glob("*.json")):
        try:
            tasks.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            continue
    return tasks


def _save(task: dict):
    SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
    _schedule_path(task["id"]).write_text(json.dumps(task, indent=2, ensure_ascii=False))


def _delete(task_id: str):
    path = _schedule_path(task_id)
    if path.exists():
        path.unlink()


# ── Next-run computation ────────────────────────────────

def _compute_next(task: dict, now: float) -> float | None:
    """Return the next unix timestamp this task should fire, or None if done."""
    interval = task.get("interval_seconds")
    at_time = task.get("at_time")

    if interval:
        last = task.get("last_run", 0)
        # If never run, schedule immediately
        if not last:
            return now
        return last + interval

    if at_time:
        try:
            if "T" in at_time:
                # ISO datetime: "2026-06-06T15:00:00" — one-shot
                return datetime.fromisoformat(at_time).timestamp()
            else:
                # "HH:MM" — daily recurring
                h, m = map(int, at_time.split(":"))
                now_dt = datetime.fromtimestamp(now)
                target = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
                if target.timestamp() <= now:
                    target += timedelta(days=1)
                return target.timestamp()
        except (ValueError, OverflowError):
            return None

    return None


# ── Layer 1+2: Scheduler thread ─────────────────────────

def _scheduler_loop():
    """Background thread: every 1s, check all tasks, push due ones to queue."""
    global _running
    while _running:
        now = time.time()
        for task in _load_all():
            if not task.get("enabled", True):
                continue
            next_run = task.get("next_run")
            if next_run and next_run <= now:
                with _lock:
                    _queue.append({
                        "id": task["id"],
                        "subject": task["subject"],
                        "prompt": task["prompt"],
                    })
                task["last_run"] = now
                new_next = _compute_next(task, now)
                task["next_run"] = new_next
                if new_next is None:
                    task["enabled"] = False
                _save(task)
                print(f"\033[90m[sched] Triggered: {task['subject']}\033[0m")
        time.sleep(1)


def start():
    """Start the scheduler background thread (idempotent)."""
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _thread.start()
    # Load existing tasks and compute next_run for any that don't have it
    now = time.time()
    for task in _load_all():
        if task.get("enabled", True) and not task.get("next_run"):
            task["next_run"] = _compute_next(task, now)
            _save(task)
    print(f"\033[90m[sched] Started ({len(_load_all())} tasks loaded)\033[0m")


# ── Layer 3: Queue interface ────────────────────────────

def enqueue(task: dict):
    """Push a task directly to the execution queue (for immediate/one-shot)."""
    with _lock:
        _queue.append(task)


def dequeue() -> dict | None:
    """Pop next task from queue. Returns None if empty."""
    with _lock:
        return _queue.pop(0) if _queue else None


def queue_size() -> int:
    with _lock:
        return len(_queue)


# ── CRUD for tools ──────────────────────────────────────

def add_schedule(subject: str, prompt: str, interval_seconds: int = 0, at_time: str = "") -> str:
    """Create a new scheduled task. Persist to disk."""
    if not subject.strip() or not prompt.strip():
        return "Error: subject and prompt are required"
    if not interval_seconds and not at_time:
        return "Error: specify interval_seconds or at_time"

    task_id = "sched_" + hashlib.md5(f"{subject}{time.time()}".encode()).hexdigest()[:8]
    now = time.time()
    task = {
        "id": task_id,
        "subject": subject.strip(),
        "prompt": prompt.strip(),
        "interval_seconds": interval_seconds,
        "at_time": at_time,
        "enabled": True,
        "last_run": None,
        "next_run": _compute_next({
            "interval_seconds": interval_seconds,
            "at_time": at_time,
            "last_run": None,
        }, now),
        "created_at": datetime.fromtimestamp(now).isoformat(),
    }
    _save(task)
    next_str = datetime.fromtimestamp(task["next_run"]).strftime("%Y-%m-%d %H:%M:%S") if task["next_run"] else "N/A"
    print(f"\033[90m[sched] Added: {task_id} (next: {next_str})\033[0m")
    return f"Scheduled '{task_id}': {subject}\nNext run: {next_str}"


def list_schedules() -> str:
    """List all scheduled tasks with their next run time."""
    tasks = _load_all()
    if not tasks:
        return "(no scheduled tasks)"
    lines = []
    for t in tasks:
        icon = "\033[32m✓\033[0m" if t.get("enabled", True) else "\033[31m✗\033[0m"
        next_str = "N/A"
        if t.get("next_run"):
            next_str = datetime.fromtimestamp(t["next_run"]).strftime("%Y-%m-%d %H:%M:%S")
        interval = f" every {t['interval_seconds']}s" if t.get("interval_seconds") else ""
        at_str = f" at {t['at_time']}" if t.get("at_time") else ""
        last = f" (last: {datetime.fromtimestamp(t['last_run']).strftime('%H:%M:%S')})" if t.get("last_run") else " (never)"
        lines.append(f"  [{icon}] {t['id']}: {t['subject']}{interval}{at_str} → next {next_str}{last}")
    return "\n".join(lines)


def cancel_schedule(task_id: str) -> str:
    """Remove a scheduled task."""
    task = None
    for t in _load_all():
        if t["id"] == task_id:
            task = t
            break
    if task is None:
        return f"Error: schedule '{task_id}' not found"
    _delete(task_id)
    print(f"\033[90m[sched] Cancelled: {task_id}\033[0m")
    return f"Cancelled schedule '{task_id}': {task['subject']}"
