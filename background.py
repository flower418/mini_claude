# ── Background task runner (non-blocking agent work) ────
import concurrent.futures
import hashlib
import threading
import time

from config import MODEL, client

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
_futures: dict[str, concurrent.futures.Future] = {}
_results: dict[str, str] = {}
_lock = threading.Lock()


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
