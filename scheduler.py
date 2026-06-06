# ── 4-layer scheduler with CronJob model ─────────────────
# Layer 1: Scheduler thread checks every 1s if any CronJob is due
# Layer 2: Due jobs are pushed to an in-memory queue
# Layer 3: Entry point dequeues and injects when agent is idle
# Layer 4: Agent processes the injected prompt, result goes into history
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

from config import SCHEDULE_DIR


# ── Cron parser (five or six-field: [sec] minute hour day month weekday) ─

_FIELD_RANGES = [
    (0, 59),   # second  (optional, defaults to 0)
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day
    (1, 12),   # month
    (0, 7),    # weekday (0/7 = Sunday)
]


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """Parse a single cron field into a set of valid integer values."""
    if field == "*":
        return set(range(lo, hi + 1))
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if "/" in part:
            base, step = part.split("/")
            step = int(step)
            if base == "*":
                base_range = range(lo, hi + 1)
            elif "-" in base:
                a, b = map(int, base.split("-"))
                base_range = range(a, b + 1)
            else:
                base_range = range(int(base), hi + 1)
            values.update(n for n in base_range if (n - lo) % step == 0)
        elif "-" in part:
            a, b = map(int, part.split("-"))
            values.update(range(a, b + 1))
        else:
            values.add(int(part))
    return values & set(range(lo, hi + 1))


def _cron_matches(cron: str, dt: datetime) -> bool:
    """Check whether a datetime satisfies a 5 or 6-field cron expression."""
    fields = cron.strip().split()
    n = len(fields)
    if n not in (5, 6):
        return False
    has_sec = n == 6
    targets = [dt.minute, dt.hour, dt.day, dt.month, dt.isoweekday() % 7]
    if has_sec:
        targets.insert(0, dt.second)
    for i, (field, target) in enumerate(zip(fields, targets)):
        valid = _parse_field(field, *_FIELD_RANGES[i + (0 if has_sec else 1)])
        if target not in valid:
            return False
    return True


def _next_cron(cron: str, after: float) -> float | None:
    """Walk forward to find the next matching timestamp.
    Uses second-step for 6-field cron, minute-step for 5-field."""
    fields = cron.strip().split()
    n = len(fields)
    if n not in (5, 6):
        return None
    has_sec = n == 6
    step = timedelta(seconds=1) if has_sec else timedelta(minutes=1)
    dt = datetime.fromtimestamp(after) + step
    dt = dt.replace(microsecond=0)
    if not has_sec:
        dt = dt.replace(second=0)
    deadline = dt + timedelta(days=730)
    while dt <= deadline:
        if _cron_matches(cron, dt):
            return dt.timestamp()
        dt += step
    return None


# ── CronJob model ────────────────────────────────────────

@dataclass
class CronJob:
    id: str
    cron: str               # "[sec] min hour day month weekday" — 5 or 6 field cron
    prompt: str             # injected as user message when triggered
    recurring: bool = True  # False = one-shot, auto-disables after fire
    durable: bool = True    # False = memory only, lost on restart
    last_run: float | None = None
    next_run: float | None = None
    created_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CronJob":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── State ────────────────────────────────────────────────

_queue: list[CronJob] = []
_lock = threading.Lock()
_running = False
_thread: threading.Thread | None = None
_memory_jobs: dict[str, CronJob] = {}  # non-durable jobs


def _schedule_path(task_id: str):
    return SCHEDULE_DIR / f"{task_id}.json"


# ── Persistence (durable jobs only) ──────────────────────

def _load_durable() -> list[CronJob]:
    if not SCHEDULE_DIR.exists():
        return []
    jobs = []
    for f in sorted(SCHEDULE_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
            jobs.append(CronJob.from_dict(d))
        except (json.JSONDecodeError, TypeError):
            continue
    return jobs


def _save_durable(job: CronJob):
    SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
    _schedule_path(job.id).write_text(json.dumps(job.to_dict(), indent=2, ensure_ascii=False))


def _delete_durable(task_id: str):
    path = _schedule_path(task_id)
    if path.exists():
        path.unlink()


def _all_jobs() -> list[CronJob]:
    """All active jobs: durable (disk) + non-durable (memory)."""
    jobs = _load_durable()
    # Merge memory-only jobs
    for mj in _memory_jobs.values():
        jobs.append(mj)
    return jobs


# ── Layer 1+2: Scheduler thread ─────────────────────────

def _scheduler_loop():
    global _running
    while _running:
        now = time.time()
        for job in _all_jobs():
            if job.next_run and job.next_run <= now:
                with _lock:
                    _queue.append(job)
                job.last_run = now
                if job.recurring:
                    job.next_run = _next_cron(job.cron, now)
                else:
                    job.next_run = None  # one-shot done
                if job.durable:
                    _save_durable(job)
                else:
                    _memory_jobs[job.id] = job
                print(f"\033[90m[sched] Triggered: {job.id}\033[0m")
        time.sleep(1)


def start():
    """Start the scheduler background thread (idempotent)."""
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _thread.start()
    # Init next_run for any durable jobs that don't have one
    now = time.time()
    for job in _load_durable():
        if not job.next_run:
            job.next_run = _next_cron(job.cron, now)
            _save_durable(job)
    print(f"\033[90m[sched] {len(_all_jobs())} jobs loaded\033[0m")


# ── Layer 3: Queue interface ────────────────────────────

def dequeue() -> CronJob | None:
    """Pop next scheduled job from queue. Returns None if empty."""
    with _lock:
        return _queue.pop(0) if _queue else None


def queue_size() -> int:
    with _lock:
        return len(_queue)


# ── CRUD for tools ──────────────────────────────────────

def add_schedule(
    id: str = "",
    cron: str = "* * * * *",
    prompt: str = "",
    recurring: bool = True,
    durable: bool = True,
) -> str:
    """Create a new CronJob."""
    if not prompt.strip():
        return "Error: prompt is required"
    cron = cron.strip()
    fields = cron.split()
    if len(fields) not in (5, 6):
        return f"Error: cron must be 5 or 6 fields (got {len(fields)}): [sec] minute hour day month weekday"

    job_id = id.strip() if id.strip() else "sched_" + hashlib.md5(f"{cron}{prompt}{time.time()}".encode()).hexdigest()[:8]
    now = time.time()
    next_run = _next_cron(cron, now)

    job = CronJob(
        id=job_id,
        cron=cron,
        prompt=prompt.strip(),
        recurring=recurring,
        durable=durable,
        last_run=None,
        next_run=next_run,
        created_at=datetime.fromtimestamp(now).isoformat(),
    )
    if durable:
        _save_durable(job)
    else:
        _memory_jobs[job_id] = job

    next_str = datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M:%S") if next_run else "N/A"
    storage = "disk" if durable else "memory"
    print(f"\033[90m[sched] Added: {job_id} ({storage}, next: {next_str})\033[0m")
    return f"Scheduled '{job_id}': {prompt[:60]}\ncron: {cron}  recurring: {recurring}  storage: {storage}  next: {next_str}"


def list_schedules() -> str:
    """List all scheduled CronJobs."""
    jobs = _all_jobs()
    if not jobs:
        return "(no scheduled jobs)"
    lines = []
    for j in jobs:
        next_str = datetime.fromtimestamp(j.next_run).strftime("%Y-%m-%d %H:%M:%S") if j.next_run else "done"
        last_str = datetime.fromtimestamp(j.last_run).strftime("%H:%M:%S") if j.last_run else "never"
        recur = "\033[32m↻\033[0m" if j.recurring else "\033[33m1\033[0m"
        storage = "💾" if j.durable else "🧠"
        lines.append(f"  {storage} {recur} {j.id}: {j.prompt[:50]}  [{j.cron}]  next={next_str}  last={last_str}")
    return "\n".join(lines)


def cancel_schedule(task_id: str) -> str:
    """Remove a scheduled job (both disk and memory)."""
    found = False
    # Check durable
    for job in _load_durable():
        if job.id == task_id:
            _delete_durable(task_id)
            found = True
            break
    # Check memory
    if task_id in _memory_jobs:
        del _memory_jobs[task_id]
        found = True
    if not found:
        return f"Error: schedule '{task_id}' not found"
    print(f"\033[90m[sched] Cancelled: {task_id}\033[0m")
    return f"Cancelled schedule '{task_id}'"
