# ── Background task runner (non-blocking agent work) ────
import concurrent.futures
import hashlib
import re
import threading
import time

from config import MODEL, client

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
_futures: dict[str, concurrent.futures.Future] = {}
_results: dict[str, str] = {}
_lock = threading.Lock()

# Heuristic keywords that suggest a task is background-worthy
_BG_KEYWORDS_HEAVY = [
    "scan", "audit", "review all", "entire codebase", "whole project",
    "analyze every", "across all", "comprehensive", "refactor all",
    "migrate", "generate report", "summarize all", "benchmark",
]
_BG_KEYWORDS_LIGHT = [
    "fix typo", "rename variable", "add comment", "single file",
    "simple change", "one line", "print", "echo",
]


def should_background(subject: str, description: str = "") -> str:
    """Heuristic: judge whether a task should run in background. Returns verdict + reasons."""
    text = f"{subject} {description}".lower()
    reasons = []

    # Heavy indicators
    for kw in _BG_KEYWORDS_HEAVY:
        if kw in text:
            reasons.append(f"+ keyword '{kw}'")

    # Estimate complexity: description length
    desc_len = len(description)
    if desc_len > 300:
        reasons.append(f"+ long description ({desc_len} chars)")
    elif desc_len < 30 and not description:
        reasons.append(f"- no description")

    # Light indicators (counter-signals)
    light_hits = [kw for kw in _BG_KEYWORDS_LIGHT if kw in text]
    if light_hits:
        reasons.append(f"- light keyword: {light_hits}")

    # References to multiple files
    file_refs = re.findall(r"\b[\w./-]+\.\w{1,6}\b", text)
    if len(set(file_refs)) > 3:
        reasons.append(f"+ references {len(set(file_refs))} files/dirs")

    score = sum(1 for r in reasons if r.startswith("+")) - sum(1 for r in reasons if r.startswith("-"))

    if score > 0:
        return f"BACKGROUND — {', '.join(reasons)}"
    elif reasons:
        return f"INLINE — {', '.join(reasons)}"
    else:
        return "INLINE — no clear signal either way"


def _run_background_task(task_id: str, prompt: str):
    """Execute a background LLM call in a worker thread."""
    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8000,
        )
        result = "\n".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
        if response.stop_reason == "max_tokens":
            result += "\n\n(warning: output truncated)"
        with _lock:
            _results[task_id] = result or "(empty response)"
    except Exception as e:
        with _lock:
            _results[task_id] = f"(background task failed: {e})"
    finally:
        with _lock:
            _futures.pop(task_id, None)


def submit(prompt: str) -> str:
    """Submit a prompt to run in background. Returns task_id immediately."""
    task_id = "bg_" + hashlib.md5(f"{prompt}{time.time()}".encode()).hexdigest()[:8]
    future = _executor.submit(_run_background_task, task_id, prompt)
    with _lock:
        _futures[task_id] = future
    print(f"\033[90m[bg] Submitted: {task_id}\033[0m")
    return task_id


def collect() -> list[tuple[str, str]]:
    """Collect completed background results. Returns (task_id, result) pairs."""
    with _lock:
        done = list(_results.items())
        _results.clear()
    return done


def pending_count() -> int:
    """Number of still-running background tasks."""
    with _lock:
        return len(_futures)


def list_pending() -> list[str]:
    """List IDs of still-running background tasks."""
    with _lock:
        return list(_futures.keys())


def list_done() -> list[str]:
    """List IDs of completed tasks waiting for collection."""
    with _lock:
        return list(_results.keys())
