#!/usr/bin/env python3
"""The retired distiller's leftover state files go once; nothing else in a store does."""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: E402
import retired_memory_state as retired  # noqa: E402

STALE = (
    ".distill-state-0a1b2c3d-1111-2222-3333-444455556666",
    ".distill-state-ses_f6b5dbfccffetI7z3sqjrkxtho",
    ".turn-state-0a1b2c3d-1111-2222-3333-444455556666",
    ".codex-turn-state-0a1b2c3d-1111-2222-3333-444455556666",
    ".distill-err-0a1b2c3d-1111-2222-3333-444455556666",
    ".distill-err-periodic-ebb01fce1f",
    ".distill-failures.log",
    ".opencode-distill-stamp-ses_f5f1effe7eM36wup0mukiS",
    ".codex-distill-out-abc",
    ".codex-distill-prompt-abc",
)
# Names that must survive: user data, live state, and anything merely alike.
KEPT = (
    "memory.db", "memory.db-wal", "memory.db-shm", "memory.db.pre-migrate-v7.bak",
    "dump.jsonl", "deleted-records.jsonl", "write-events.jsonl",
    "source-history.jsonl", "source-history.20260930T101010123456.jsonl",
    ".exchange-state.json", ".exchange-run.lock", ".exchange-schedule.lock",
    ".sync-v2.lock", ".briefing-2026-09-30", ".postit-roots",
    "README.md", ".gitignore", "notes.txt",
    ".distill-state", ".distill-statement-x", ".turn-state", ".distill-budget",
    ".xdistill-state-abc", ".distill-state-a/b",
    ".distill-err", ".opencode-turn-state-ses_abc", ".opencode-distill-state-ses_abc",
    ".codex-distill-state-abc", ".distill-budget-3",
)


class RetiredMemoryStateTest(unittest.TestCase):
    def _store(self, root):
        store = Path(root) / "store"
        store.mkdir()
        for name in STALE + tuple(n for n in KEPT if "/" not in n):
            (store / name).write_text("x\n")
        (store / ".git").mkdir()
        (store / ".git" / "HEAD").write_text("ref\n")
        (store / "backups").mkdir()
        (store / "backups" / "memory.db").write_text("backup\n")
        (store / ".opencode-distill-workdir").mkdir()
        (store / ".opencode-distill-workdir" / "inner").write_text("dir stays\n")
        (store / "recall-opportunities").mkdir()
        return store

    def test_only_the_retired_names_go(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            self.assertEqual(retired.retire(store), len(STALE))
            left = set(os.listdir(store))
            for name in STALE:
                self.assertNotIn(name, left)
            for name in KEPT:
                if "/" not in name:
                    self.assertIn(name, left, name)
            self.assertTrue((store / ".git" / "HEAD").is_file())
            self.assertTrue((store / "backups" / "memory.db").is_file())
            self.assertTrue((store / ".opencode-distill-workdir" / "inner").is_file())
            self.assertTrue((store / "recall-opportunities").is_dir())
            self.assertEqual(retired.retire(store), 0)  # once is enough

    def test_a_symlink_or_directory_with_a_retired_name_is_left_alone(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            precious = Path(root) / "precious.txt"
            precious.write_text("keep\n")
            (store / ".turn-state-linked").symlink_to(precious)
            (store / ".distill-state-dir").mkdir()
            (store / ".distill-state-dir" / "inner").write_text("keep\n")
            retired.retire(store)
            self.assertTrue((store / ".turn-state-linked").is_symlink())
            self.assertEqual(precious.read_text(), "keep\n")
            self.assertEqual((store / ".distill-state-dir" / "inner").read_text(), "keep\n")

    def test_a_missing_or_unreadable_store_is_silent(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(retired.retire(Path(root) / "absent"), 0)
            plain = Path(root) / "file"
            plain.write_text("not a directory\n")
            self.assertEqual(retired.retire(plain), 0)

    def test_the_default_store_follows_mem_store(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            saved = os.environ.get("MEM_STORE")
            os.environ["MEM_STORE"] = str(store)
            try:
                self.assertEqual(retired.default_store(), store)
                self.assertEqual(retired.retire(), len(STALE))
            finally:
                if saved is None:
                    os.environ.pop("MEM_STORE", None)
                else:
                    os.environ["MEM_STORE"] = saved

    def test_nothing_current_reads_or_writes_a_retired_name(self):
        root = Path(__file__).resolve().parents[2]
        # mem.py and the modules it is split into (tools/memory/mem_*.py, "Split out of `mem.py`").
        mem_parts = [path for path in sorted((root / "tools/memory").glob("mem_*.py"))
                     if "Split out of `mem.py`" in path.read_text(encoding="utf-8")]
        live = [root / "tools/memory/mem.py", *mem_parts, *(root / "hooks").glob("*.sh"),
                *(root / "hooks").glob("*.py"), *(root / "adapters/codex/hooks").glob("*.py"),
                *(root / "adapters/codex/bin").glob("*.sh"),
                *(root / "adapters/opencode/bin").glob("*.sh"),
                root / "adapters/opencode/plugins/hearting-guards.js"]
        pattern = re.compile(r"distill-state|turn-state|distill-err|distill-budget|distill-failures|"
                             r"distill-stamp|distill-out|distill-prompt")
        for path in live:
            if path.name == "portable-guards.test.sh":
                continue
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), str(path))

    def test_install_paths_call_it(self):
        root = Path(__file__).resolve().parent
        self.assertIn("retired_memory_state.retire(mem_store)",
                      (root / "bootstrap.py").read_text(encoding="utf-8"))
        self.assertIn("retired_memory_state.retire()",
                      (root / "runtime_activation.py").read_text(encoding="utf-8"))

    def test_restore_memory_drops_them_even_without_a_database(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            result = bootstrap.restore_memory(store)
            self.assertEqual(result["action"], "skipped")
            self.assertFalse((store / STALE[0]).exists())
            self.assertTrue((store / "dump.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
