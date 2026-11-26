#!/usr/bin/env python3
"""A source-keyed overwrite must leave the previous body recoverable.

``mem add ... --source S`` updates the record with the same source in place.
That is the intended identity contract (profiles, pending handoffs, migrate
re-runs), but a different body used to replace the old one with no local
record of it.  These checks drive the public CLI in a throwaway store with the
exchange remote off.
"""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from helpers import MEMORY_DIR


MEM = MEMORY_DIR / "mem.py"
HISTORY_NAME = "source-history.jsonl"

FIRST = "The first body written under this source key, kept for recovery."
SECOND = "The second body written under the same source key replaces it."
THIRD = "A third body under the same source key, longer than the others."


class SourceHistoryTest(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.other_project = self.root / "other-project"
        self.other_project.mkdir()
        self.store = self.root / "store"
        self.projects = self.root / "projects"
        self.profile_dir = self.root / "profile"
        for path in (self.store, self.projects, self.profile_dir,
                     self.root / "home", self.root / "state"):
            path.mkdir()
        config = self.root / "config" / "hearting"
        config.mkdir(parents=True)
        (config / "memory-sync.json").write_text('{"enabled": false}\n')

    def tearDown(self):
        self.tempdir.cleanup()

    def env(self, **extra):
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("MEM_", "AGENT_", "CODEX_", "OPENCODE_"))
        }
        env.update({
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "MEM_STORE": str(self.store),
            "MEM_PROJECTS": str(self.projects),
            "MEM_PROFILE": str(self.profile_dir),
            "MEM_DUMP_COMMIT": "0",
            "MEM_EXCHANGE_AUTO": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.update(extra)
        return env

    def mem(self, *args, cwd=None, env_extra=None, check=True):
        result = subprocess.run(
            [sys.executable, str(MEM), *args],
            cwd=cwd or self.project,
            env=self.env(**(env_extra or {})),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    @staticmethod
    def written_id(result):
        for line in reversed(result.stdout.splitlines()):
            if re.match(r"^\[(write|upsert|reinforce)\]", line):
                return line.rsplit(maxsplit=1)[-1]
        raise AssertionError(f"no record id in output: {result.stdout!r}")

    def add(self, body, source, *extra, tier="durable", rtype="decision", cwd=None):
        return self.mem("add", tier, rtype, body, "--source", source, *extra, cwd=cwd)

    def row(self, rid):
        con = sqlite3.connect(self.store / "memory.db")
        try:
            return con.execute(
                "SELECT body, delivery_state, status FROM records WHERE id=?",
                (rid,)).fetchone()
        finally:
            con.close()

    def history_files(self):
        return sorted(self.store.glob("source-history*.jsonl"))

    def history_lines(self):
        lines = []
        for path in self.history_files():
            lines.extend(l for l in path.read_text().splitlines() if l.strip())
        return lines

    # -- the reported defect -------------------------------------------------

    def test_overwrite_keeps_previous_body_and_can_be_reverted(self):
        rid = self.written_id(self.add(FIRST, "test:overwrite"))
        second = self.add(SECOND, "test:overwrite")
        self.assertEqual(self.written_id(second), rid)
        self.assertEqual(self.row(rid)[0], SECOND)

        listing = self.mem("history", rid).stdout
        self.assertIn("test:overwrite", listing)
        self.assertIn("The first body", listing)

        shown = self.mem("history", rid, "--show", "1").stdout
        self.assertIn(FIRST, shown)

        self.mem("history", rid, "--restore", "1")
        self.assertEqual(self.row(rid)[0], FIRST)
        # The revert is itself a source overwrite: the round trip stays possible.
        self.mem("history", rid, "--restore", "1")
        self.assertEqual(self.row(rid)[0], SECOND)

    def test_overwrite_output_announces_the_kept_body(self):
        rid = self.written_id(self.add(FIRST, "test:announce"))
        second = self.add(SECOND, "test:announce")
        self.assertRegex(second.stdout, r"(?m)^\[upsert\] .*" + re.escape(rid) + r"$")
        self.assertRegex(second.stdout, rf"(?m)^\[history\] .*mem history {re.escape(rid)}")
        # Identity output stays parseable: one line carries the arrow and the id.
        self.assertEqual(sum("→" in line for line in second.stdout.splitlines()), 1)

    def test_same_body_again_creates_no_history(self):
        rid = self.written_id(self.add(FIRST, "test:same"))
        again = self.add(FIRST, "test:same")
        self.assertEqual(self.written_id(again), rid)
        self.assertNotIn("[history]", again.stdout)
        self.assertEqual(self.history_lines(), [])
        self.assertIn("no previous", self.mem("history", rid).stdout.lower())

    def test_history_record_shape_and_permissions(self):
        rid = self.written_id(self.add(FIRST, "test:shape", "--tags", "a,b"))
        self.add(SECOND, "test:shape")
        (path,) = self.history_files()
        self.assertEqual(path.name, HISTORY_NAME)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        (entry,) = [json.loads(line) for line in self.history_lines()]
        self.assertEqual(entry["id"], rid)
        self.assertEqual(entry["source"], "test:shape")
        self.assertEqual(entry["tier"], "durable")
        self.assertEqual(entry["scope"], "project")
        self.assertEqual(entry["type"], "decision")
        self.assertEqual(entry["prev_body"], FIRST)
        self.assertEqual(entry["prev_tags"], ["a", "b"])
        self.assertTrue(entry["cwd_origin"])
        self.assertTrue(entry["prev_updated"])
        self.assertTrue(entry["replaced_at"])
        self.assertTrue(entry["actor"])
        self.assertIn("prev_headline", entry)

    # -- source-keyed callers keep their ID and upsert meaning --------------

    def test_profile_source_keeps_id_and_newest_body(self):
        source = "user-profile:workstyle"
        first = self.written_id(self.add(
            FIRST, source, "--scope", "global", rtype="profile"))
        second = self.written_id(self.add(
            SECOND, source, "--scope", "global", rtype="profile"))
        self.assertEqual(first, second)
        self.assertEqual(self.row(first)[0], SECOND)
        self.assertIn(SECOND, self.mem("profile", "workstyle").stdout)
        # Global records are readable from any project, so recovery is too.
        shown = self.mem("history", first, "--show", "1", cwd=self.other_project).stdout
        self.assertIn(FIRST, shown)
        self.mem("history", first, "--restore", "1", cwd=self.other_project)
        self.assertEqual(self.row(first)[0], FIRST)

    def test_pending_handoff_keeps_id_and_stays_pending(self):
        source = "handoff:test-thread"
        first = self.written_id(self.add(
            FIRST, source, "--requires-consume", rtype="handoff", tier="working"))
        second = self.written_id(self.add(
            SECOND, source, "--requires-consume", rtype="handoff", tier="working"))
        self.assertEqual(first, second)
        body, delivery, status = self.row(first)
        self.assertEqual((body, delivery, status), (SECOND, "pending", "active"))
        self.mem("history", first, "--restore", "1")
        body, delivery, _status = self.row(first)
        self.assertEqual((body, delivery), (FIRST, "pending"))

    def test_migrate_rerun_keeps_id_and_the_old_file_body(self):
        encoded = "-tmp-source-history-project"
        memory_dir = self.projects / encoded / "memory"
        memory_dir.mkdir(parents=True)
        note = memory_dir / "note.md"
        note.write_text(f"---\ntype: lesson\n---\n{FIRST}\n")
        self.mem("migrate", "--apply", "--all-projects")
        source = f"auto-memory:{encoded}/note.md"
        con = sqlite3.connect(self.store / "memory.db")
        try:
            (rid,) = con.execute(
                "SELECT id FROM records WHERE source=?", (source,)).fetchone()
        finally:
            con.close()
        note.write_text(f"---\ntype: lesson\n---\n{SECOND}\n")
        self.mem("migrate", "--apply", "--all-projects")
        con = sqlite3.connect(self.store / "memory.db")
        try:
            rows = con.execute(
                "SELECT id, body FROM records WHERE source=?", (source,)).fetchall()
        finally:
            con.close()
        self.assertEqual([r[0] for r in rows], [rid])
        self.assertIn("second body", rows[0][1])
        entries = [json.loads(line) for line in self.history_lines()]
        self.assertEqual([e["id"] for e in entries], [rid])
        self.assertIn("first body", entries[0]["prev_body"])
        # migrate runs again with no file change: no further history.
        self.mem("migrate", "--apply", "--all-projects")
        self.assertEqual(len(self.history_lines()), 1)

    # -- boundaries ---------------------------------------------------------

    def test_history_of_another_project_is_not_readable(self):
        rid = self.written_id(self.add(FIRST, "test:scoped"))
        self.add(SECOND, "test:scoped")
        refused = self.mem("history", rid, "--show", "1", cwd=self.other_project,
                           check=False)
        self.assertNotIn(FIRST, refused.stdout + refused.stderr)
        self.assertNotEqual(refused.returncode, 0)
        self.mem("history", rid, "--restore", "1", cwd=self.other_project, check=False)
        self.assertEqual(self.row(rid)[0], SECOND)

    def test_history_write_failure_does_not_fail_the_write(self):
        rid = self.written_id(self.add(FIRST, "test:diskfull"))
        (self.store / HISTORY_NAME).mkdir()  # unwritable as a file
        result = self.add(SECOND, "test:diskfull")
        self.assertEqual(self.written_id(result), rid)
        self.assertEqual(self.row(rid)[0], SECOND)
        warnings = [l for l in result.stderr.splitlines() if "[history]" in l]
        self.assertEqual(len(warnings), 1, result.stderr)

    def test_large_history_rotates_without_losing_bodies(self):
        rid = self.written_id(self.add("body number 0 " + "x" * 60, "test:rotate"))
        for n in range(1, 9):
            self.add(f"body number {n} " + "x" * 60, "test:rotate",
                     )
            # each overwrite records the body it replaced
        # Re-run with a tiny size cap so the file has to rotate.
        small = {"MEM_SOURCE_HISTORY_MAX_BYTES": "600"}
        for n in range(9, 16):
            self.mem("add", "durable", "decision", f"body number {n} " + "x" * 60,
                     "--source", "test:rotate", env_extra=small)
        self.assertGreater(len(self.history_files()), 1)
        bodies = {json.loads(line)["prev_body"].split()[2]
                  for line in self.history_lines()}
        self.assertEqual(bodies, {str(n) for n in range(0, 15)})
        # Recovery reads every generation, newest first.
        listing = self.mem("history", rid).stdout
        self.assertIn("body number 14", listing)
        self.assertIn("body number 0", listing)

    def test_concurrent_overwrites_lose_no_previous_body(self):
        rid = self.written_id(self.add("body number 0 " + "x" * 60, "test:race"))
        small = {"MEM_SOURCE_HISTORY_MAX_BYTES": "700"}
        procs = []
        for n in range(1, 13):
            procs.append(subprocess.Popen(
                [sys.executable, str(MEM), "add", "durable", "decision",
                 f"body number {n} " + "x" * 60, "--source", "test:race"],
                cwd=self.project, env=self.env(**small), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err + out)
        final = self.row(rid)[0]
        kept = {json.loads(line)["prev_body"].split()[2] for line in self.history_lines()}
        written = {str(n) for n in range(0, 13)}
        # Every body but the one still in the record was replaced, and each is kept.
        self.assertEqual(kept, written - {final.split()[2]})
        self.assertGreater(len(self.history_files()), 1)
        self.assertEqual(len(self.history_lines()), 12)

    def test_history_stays_out_of_the_dump(self):
        rid = self.written_id(self.add(FIRST, "test:dump"))
        self.add(SECOND, "test:dump")
        self.mem("sync")
        dump = self.store / "dump.jsonl"
        if dump.exists():
            self.assertNotIn(FIRST, dump.read_text())
        self.assertTrue(self.history_lines())
        self.assertEqual(self.row(rid)[0], SECOND)


if __name__ == "__main__":
    unittest.main()
