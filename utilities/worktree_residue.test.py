#!/usr/bin/env python3
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "utilities/worktree-residue.py"


def run_helper(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HELPER), *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def fields(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


class WorktreeResidueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Fixture"], check=True)
        (self.repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        (self.repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        stub_dir = self.repo / "sample-app/node_modules/dep"
        stub_dir.mkdir(parents=True)
        (self.repo / ".agent-build-residue").write_text(
            "# webpack tracing stubs\nsample-app/node_modules/*\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        # build residue + bystanders, all created after the commit
        (stub_dir / "stub.js").write_text("stub\n", encoding="utf-8")
        (self.repo / "keep.txt").write_text("keep\n", encoding="utf-8")
        (self.repo / "ignored.log").write_text("ignored\n", encoding="utf-8")
        self.audit = self.base / "audit/build-residue.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def test_check_lists_matching_residue_without_removing(self):
        result = run_helper("--worktree", str(self.repo), "--audit", str(self.audit))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = fields(result.stdout)
        self.assertEqual(out["status"], "check")
        self.assertEqual(out["matched"], "1")
        self.assertEqual(out["removed"], "0")
        self.assertIn("residue=sample-app/node_modules/dep/stub.js", result.stdout)
        self.assertTrue((self.repo / "sample-app/node_modules/dep/stub.js").is_file())
        self.assertFalse(self.audit.exists())

    def test_clean_removes_only_matched_untracked_and_audits(self):
        result = run_helper("--worktree", str(self.repo), "--clean", "--audit", str(self.audit))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = fields(result.stdout)
        self.assertEqual(out["status"], "cleaned")
        self.assertEqual(out["removed"], "1")
        self.assertFalse((self.repo / "sample-app/node_modules/dep/stub.js").exists())
        # empty stub dirs are pruned up to the worktree root
        self.assertFalse((self.repo / "sample-app").exists())
        self.assertTrue((self.repo / "keep.txt").is_file())
        self.assertTrue((self.repo / "ignored.log").is_file())
        self.assertTrue((self.repo / "tracked.txt").is_file())
        record = json.loads(self.audit.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["removed"], ["sample-app/node_modules/dep/stub.js"])
        self.assertEqual(record["skipped_unsafe"], [])
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn("sample-app", status)

    def test_tracked_file_matching_pattern_is_untouchable(self):
        tracked_stub = self.repo / "sample-app/node_modules/dep/tracked.js"
        tracked_stub.write_text("tracked stub\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", str(tracked_stub)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "tracked stub"], check=True)
        result = run_helper("--worktree", str(self.repo), "--clean", "--audit", str(self.audit))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(tracked_stub.is_file())
        self.assertFalse((self.repo / "sample-app/node_modules/dep/stub.js").exists())

    def test_symlink_is_unlinked_never_followed(self):
        outside = self.base / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.repo / "sample-app/node_modules/evil"
        link.symlink_to(outside)
        result = run_helper("--worktree", str(self.repo), "--clean", "--audit", str(self.audit))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(link.exists())
        self.assertTrue(outside.is_file())

    def test_clean_without_patterns_fails_closed(self):
        (self.repo / ".agent-build-residue").unlink()
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "drop patterns"], check=True)
        result = run_helper("--worktree", str(self.repo), "--clean", "--audit", str(self.audit))
        self.assertEqual(result.returncode, 2)
        self.assertIn("no-residue-patterns", result.stdout)
        self.assertTrue((self.repo / "keep.txt").is_file())

    def test_cli_glob_extends_pattern_file(self):
        (self.repo / "dist.tmp").write_text("tmp\n", encoding="utf-8")
        result = run_helper(
            "--worktree", str(self.repo), "--glob", "*.tmp", "--clean", "--audit", str(self.audit)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.repo / "dist.tmp").exists())
        self.assertFalse((self.repo / "sample-app/node_modules/dep/stub.js").exists())

    def test_non_git_target_is_rejected(self):
        plain = self.base / "plain"
        plain.mkdir()
        result = run_helper("--worktree", str(plain), "--clean", "--glob", "*")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not-a-git-worktree", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
