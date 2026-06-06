import tempfile
import unittest
import sys
import types
from contextlib import contextmanager
from pathlib import Path

anthropic_stub = types.ModuleType("anthropic")


class _DummyAnthropic:
    def __init__(self, *args, **kwargs):
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, *args, **kwargs):
        raise RuntimeError("Anthropic client is stubbed in tests")


anthropic_stub.Anthropic = _DummyAnthropic
sys.modules.setdefault("anthropic", anthropic_stub)

dotenv_stub = types.ModuleType("dotenv")
dotenv_stub.dotenv_values = lambda path: {
    "ANTHROPIC_API_KEY": "test-key",
    "MODEL_ID": "test-model",
}
sys.modules.setdefault("dotenv", dotenv_stub)

yaml_stub = types.ModuleType("yaml")


class _YamlError(Exception):
    pass


def _safe_load_frontmatter(text):
    data = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()
    return data


yaml_stub.YAMLError = _YamlError
yaml_stub.safe_load = _safe_load_frontmatter
sys.modules.setdefault("yaml", yaml_stub)

import agent_team
import hooks
import memory
import scheduler
import state_store
import task_system
import tools
import worktree


class RuntimeEdgeTests(unittest.TestCase):
    def test_hooks_initialize_once(self):
        original_hooks = {name: callbacks[:] for name, callbacks in hooks.HOOKS.items()}
        original_initialized = hooks._INITIALIZED
        try:
            for callbacks in hooks.HOOKS.values():
                callbacks.clear()
            hooks._INITIALIZED = False

            hooks.init_hooks()
            hooks.init_hooks()

            self.assertEqual(len(hooks.HOOKS["UserPromptSubmit"]), 1)
            self.assertEqual(len(hooks.HOOKS["PreToolUse"]), 2)
            self.assertEqual(len(hooks.HOOKS["PostToolUse"]), 1)
            self.assertEqual(len(hooks.HOOKS["Stop"]), 1)
        finally:
            hooks.HOOKS.clear()
            hooks.HOOKS.update(original_hooks)
            hooks._INITIALIZED = original_initialized

    def test_safe_dispatch_catches_handler_exceptions(self):
        def boom():
            raise RuntimeError("boom")

        self.assertEqual(tools.safe_dispatch(boom, {}), "Error running boom: boom")

    def test_memory_index_rebuilds_from_entries(self):
        original_dir = memory.MEMORY_DIR
        original_index = memory.MEMORY_INDEX
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            memory.MEMORY_DIR = root
            memory.MEMORY_INDEX = root / "MEMORY.md"
            try:
                entry_dir = root / "fresh-entry"
                entry_dir.mkdir(parents=True)
                (entry_dir / "entry.md").write_text(
                    "---\n"
                    "name: Fresh Entry\n"
                    "description: Current memory item\n"
                    "type: user\n"
                    "---\n\n"
                    "Useful remembered fact.\n"
                )
                root.mkdir(exist_ok=True)
                memory.MEMORY_INDEX.write_text("- stale item\n")

                index = memory.get_memory_index()

                self.assertIn("Fresh Entry", index)
                self.assertNotIn("stale item", index)
            finally:
                memory.MEMORY_DIR = original_dir
                memory.MEMORY_INDEX = original_index

    def test_task_ids_reject_path_traversal(self):
        original_dir = task_system.TASK_DIR
        with tempfile.TemporaryDirectory() as td:
            task_system.TASK_DIR = Path(td)
            try:
                self.assertIn("invalid task id", task_system.run_claim_task("../escape"))
                self.assertIn(
                    "invalid blockedBy task id",
                    task_system.run_create_task("child", blockedBy=["../escape"]),
                )
            finally:
                task_system.TASK_DIR = original_dir

    def test_schedule_ids_and_bad_cron_are_rejected(self):
        original_dir = scheduler.SCHEDULE_DIR
        original_memory_jobs = scheduler._memory_jobs.copy()
        original_queue = scheduler._queue[:]
        with tempfile.TemporaryDirectory() as td:
            scheduler.SCHEDULE_DIR = Path(td)
            scheduler._memory_jobs.clear()
            scheduler._queue.clear()
            try:
                self.assertIn(
                    "invalid schedule id",
                    scheduler.add_schedule(id="../escape", cron="* * * * *", prompt="run"),
                )
                self.assertIn(
                    "invalid cron expression",
                    scheduler.add_schedule(id="ok", cron="*/0 * * * * *", prompt="run"),
                )
                self.assertIn(
                    "Scheduled 'trimmed'",
                    scheduler.add_schedule(id="trimmed", cron="* * * * *", prompt="run"),
                )
                self.assertIn("Cancelled schedule 'trimmed'", scheduler.cancel_schedule(" trimmed "))
            finally:
                scheduler.SCHEDULE_DIR = original_dir
                scheduler._memory_jobs.clear()
                scheduler._memory_jobs.update(original_memory_jobs)
                scheduler._queue[:] = original_queue

    def test_cron_step_fields_start_from_explicit_base(self):
        self.assertEqual(sorted(scheduler._parse_field("*/15", 0, 59))[:4], [0, 15, 30, 45])
        self.assertEqual(sorted(scheduler._parse_field("1/15", 0, 59))[:4], [1, 16, 31, 46])

    def test_agent_names_reject_path_traversal(self):
        original_dir = agent_team.AGENTS_DIR
        with tempfile.TemporaryDirectory() as td:
            agent_team.AGENTS_DIR = Path(td)
            try:
                self.assertIn("invalid agent name", agent_team.send_to_agent("../escape", "hi"))
                self.assertIn("invalid agent name", agent_team.spawn_agent("../escape", "role"))
                self.assertIn("invalid agent name", agent_team.kill_agent("../escape"))
            finally:
                agent_team.AGENTS_DIR = original_dir

    def test_agent_mailbox_uses_normalized_names_and_skips_bad_jsonl(self):
        original_dir = agent_team.AGENTS_DIR
        with tempfile.TemporaryDirectory() as td:
            agent_team.AGENTS_DIR = Path(td)
            try:
                box = agent_team.Mailbox("Code Reviewer")
                box.send("lead", "hello")
                box._path.write_text(box._path.read_text() + "not json\n")
                box.send("lead", "again")

                self.assertEqual(box._path.parent.name, "code-reviewer")
                self.assertEqual([m["body"] for m in box.read_all()], ["hello", "again"])
                self.assertFalse(box.has_mail())
            finally:
                agent_team.AGENTS_DIR = original_dir

    def test_worktree_merges_modified_files(self):
        with self._patched_worktree() as root:
            (root / "a.txt").write_text("base\n")
            (root / ".gitignore").write_text("kept\n")
            (root / ".env").write_text("secret\n")

            wt = worktree.create("task-1")
            self.assertTrue((wt / ".gitignore").exists())
            self.assertFalse((wt / ".env").exists())
            (wt / "a.txt").write_text("changed\n")

            result = worktree.merge("task-1")

            self.assertIn("Merged 1 changes", result)
            self.assertEqual((root / "a.txt").read_text(), "changed\n")
            self.assertFalse((root / ".worktrees" / "task-1").exists())

    def test_worktree_deletes_files_changed_only_in_worktree(self):
        with self._patched_worktree() as root:
            (root / "delete-me.txt").write_text("base\n")

            wt = worktree.create("task-2")
            (wt / "delete-me.txt").unlink()

            result = worktree.merge("task-2")

            self.assertIn("Merged 1 changes", result)
            self.assertFalse((root / "delete-me.txt").exists())

    def test_worktree_reports_conflict_for_concurrent_edits(self):
        with self._patched_worktree() as root:
            (root / "conflict.txt").write_text("base\n")

            wt = worktree.create("task-3")
            (root / "conflict.txt").write_text("main\n")
            (wt / "conflict.txt").write_text("worktree\n")

            result = worktree.merge("task-3")

            self.assertIn("CONFLICT", result)
            self.assertEqual((root / "conflict.txt").read_text(), "main\n")
            self.assertTrue((root / ".worktrees" / "task-3").exists())

    def test_worktree_rejects_invalid_task_ids(self):
        with self._patched_worktree():
            self.assertIn("invalid task id", worktree.merge("../escape"))
            self.assertIn("invalid task id", worktree.discard("../escape"))
            self.assertIn("invalid task id", worktree.keep("../escape"))

    def test_worktree_listing_skips_invalid_state_dirs(self):
        with self._patched_worktree() as root:
            (root / ".worktrees" / "Bad").mkdir(parents=True, exist_ok=True)
            (root / ".worktrees" / "task-4").mkdir(parents=True)
            (root / ".worktrees" / "task-4" / ".wt_status").write_text("done")

            listing = worktree.list_worktrees()

            self.assertIn("task-4", listing)
            self.assertNotIn("Bad", listing)

    def test_state_store_validates_ids_and_filters_bad_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = state_store.JsonDirStore(root, state_store.TASK_ID_POLICY)

            store.write("task-1", {"id": "task-1"})
            (root / "Bad.json").write_text('{"id": "Bad"}')
            (root / "task-2.json").write_text("{bad json")

            self.assertEqual(state_store.AGENT_NAME_POLICY.normalize("Main"), "lead")
            self.assertEqual(state_store.AGENT_NAME_POLICY.normalize("Code Reviewer"), "code-reviewer")
            self.assertFalse(state_store.TASK_ID_POLICY.is_valid("../escape"))
            self.assertEqual(store.get("task-1"), {"id": "task-1"})
            self.assertEqual(store.list(), [{"id": "task-1"}])
            self.assertIsNone(store.get("../escape"))
            self.assertFalse(store.delete("../escape"))

    @contextmanager
    def _patched_worktree(self):
        original_repo = worktree.REPO_DIR
        original_worktrees = worktree.WORKTREE_DIR
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree.REPO_DIR = root
            worktree.WORKTREE_DIR = root / ".worktrees"
            try:
                yield root
            finally:
                worktree.REPO_DIR = original_repo
                worktree.WORKTREE_DIR = original_worktrees


if __name__ == "__main__":
    unittest.main()
