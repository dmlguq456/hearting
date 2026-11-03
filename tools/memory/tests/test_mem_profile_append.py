#!/usr/bin/env python3
"""`mem profile-append` must splice one item into the manual-memo block only.

The full body goes through the normal source upsert (same record id, prior
body in history). A raw partial `mem add --source user-profile:<stem>` would
overwrite the structured sections instead.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from helpers import MEMORY_DIR


MEM = MEMORY_DIR / "mem.py"

SEED = """aspect: 06_collaboration_style

## 협업 방식
- 먼저 방향을 짧게 확인한다.

## 사용자 수동 메모
- 기존 메모 하나.
"""


class ProfileAppendTest(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = self.root / "store"
        self.projects = self.root / "projects"
        self.profile_dir = self.root / "profile"
        for path in (self.store, self.projects, self.profile_dir,
                     self.root / "home", self.root / "state"):
            path.mkdir()
        config = self.root / "config" / "hearting"
        config.mkdir(parents=True)
        (config / "memory-sync.json").write_text('{"enabled": false}\n')
        self.mem("add", "durable", "profile", SEED, "--scope", "global",
                 "--source", "user-profile:06_collaboration_style")

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

    def mem(self, *args, check=True):
        result = subprocess.run(
            [sys.executable, str(MEM), *args],
            cwd=self.project,
            env=self.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def test_append_keeps_structured_sections_and_id(self):
        first = self.mem("profile", "06_collaboration_style").stdout
        self.assertIn("먼저 방향을 짧게 확인한다.", first)
        done = self.mem("profile-append", "06_collaboration_style", "새 메모 한 줄.")
        self.assertIn("[upsert]", done.stdout)
        body = self.mem("profile", "06_collaboration_style").stdout
        self.assertIn("먼저 방향을 짧게 확인한다.", body)
        self.assertIn("기존 메모 하나.", body)
        self.assertIn("새 메모 한 줄.", body)

    def test_append_by_number_and_alias(self):
        self.mem("profile-append", "06", "번호로 덧붙이기.")
        self.mem("profile-append", "collaboration", "별칭으로 덧붙이기.")
        body = self.mem("profile", "06_collaboration_style").stdout
        self.assertIn("번호로 덧붙이기.", body)
        self.assertIn("별칭으로 덧붙이기.", body)

    def test_prior_body_stays_in_history(self):
        self.mem("profile-append", "06_collaboration_style", "기록 남기기.")
        shown = self.mem("history", self._profile_id(), "--show", "1").stdout
        self.assertIn("기존 메모 하나.", shown)
        self.assertNotIn("기록 남기기.", shown)

    def test_missing_aspect_and_empty_text_are_refused(self):
        self.assertEqual(self.mem("profile-append", "no-such-aspect", "x", check=False).returncode, 2)
        self.assertEqual(self.mem("profile-append", "06_collaboration_style", "  ", check=False).returncode, 2)

    def _profile_read_only(self, expected="먼저 방향을 짧게 확인한다."):
        """Run `mem profile` as a Codex worker sandbox sees it: the store and
        state are unwritable and the identity directory chmod fails with EROFS."""
        if os.geteuid() == 0:
            self.skipTest("permission bits do not stop root from writing")
        paths = [self.store, *self.store.iterdir(), self.root / "state",
                 *(self.root / "state").rglob("*")]
        modes = {path: path.stat().st_mode & 0o777 for path in paths}
        listing = sorted(p.name for p in self.store.iterdir())
        shim = (
            "import errno, os, runpy, sys\n"
            "def chmod(path, *a, **k):\n"
            "    raise OSError(errno.EROFS, 'Read-only file system', str(path))\n"
            "os.chmod = chmod\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        try:
            for path in paths:
                path.chmod(0o555 if path.is_dir() else 0o444)
            result = subprocess.run(
                [sys.executable, "-c", shim, str(MEM), "profile", "06_collaboration_style"],
                cwd=self.project, env=self.env(), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            unchanged = {path: path.stat().st_mode & 0o777 for path in paths}
        finally:
            for path in reversed(paths):
                path.chmod(modes[path])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(expected, result.stdout)
        self.assertEqual(unchanged, {path: 0o555 if path.is_dir() else 0o444 for path in paths})
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), listing)

    def test_profile_reads_a_checkpointed_read_only_store(self):
        self.assertFalse(Path(f"{self.store / 'memory.db'}-wal").exists())
        self._profile_read_only()

    def test_profile_reads_a_live_read_only_store(self):
        # Another process holds the store open (the usual case), so a commit stays
        # in -wal: the read-only reader must see it through the existing -shm.
        holder = sqlite3.connect(self.store / "memory.db")
        self.addCleanup(holder.close)
        holder.execute("SELECT COUNT(*) FROM records").fetchone()
        self.mem("profile-append", "06_collaboration_style", "아직 체크포인트 전인 메모.")
        self.assertGreater(Path(f"{self.store / 'memory.db'}-wal").stat().st_size, 0)
        self._profile_read_only(expected="아직 체크포인트 전인 메모.")

    def test_profile_rereads_when_the_store_changes_under_an_immutable_read(self):
        # A host writer may start while a sandboxed reader reads the WAL-less file
        # as immutable; that read must not count, and the choice is made again.
        probe = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('mem_probe', sys.argv[1])\n"
            "mem = importlib.util.module_from_spec(spec); spec.loader.exec_module(mem)\n"
            "states = iter([(1, 1, 1), (2, 2, 2), (3, 3, 3), (3, 3, 3)])\n"
            "calls = []\n"
            "mem._profile_store_state = lambda db: calls.append(db) or next(states)\n"
            "real = mem.sqlite3.connect\n"
            "def connect(target, *a, **k):\n"
            "    if 'immutable=1' not in str(target):\n"
            "        raise mem.sqlite3.OperationalError('attempt to write a readonly database')\n"
            "    return real(target, *a, **k)\n"
            "mem.sqlite3.connect = connect\n"
            "rows = mem._profile_rows(\"SELECT COUNT(*) FROM records WHERE type='profile'\")\n"
            "print(len(calls), rows[0][0])\n"
        )
        result = subprocess.run([sys.executable, "-c", probe, str(MEM)], cwd=self.project,
                                env=self.env(), text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        # first attempt: before (1,1,1) != after (2,2,2) -> retry; second: (3,3,3) == (3,3,3)
        self.assertEqual(result.stdout.split(), ["4", "1"])

    def _profile_id(self):
        shown = self.mem("recall", "기존 메모 하나.", "--full").stdout
        for line in shown.splitlines():
            if "profile-aspect-06-collaboration" in line:
                token = line.strip().split()[-1].rstrip(":")
                if token:
                    return token
        raise AssertionError(f"no profile id in recall output: {shown!r}")


if __name__ == "__main__":
    unittest.main()
