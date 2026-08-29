#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "utilities" / "artifact-postscan.py"

ROUTE_ID = "rt-test"
NODE = "inline"
ATTEMPT = "att-test"


def run(args):
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args, capture_output=True, text=True, timeout=30
    )


def write_route(path: Path, write_scope: list[str]) -> None:
    path.write_text(
        json.dumps({"nodes": [{"id": NODE, "write_scope": write_scope}]}), encoding="utf-8"
    )


class PostscanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "cycle"
        self.root.mkdir()
        self.artifact_root = self.tmp / "artifact-root"
        self.artifact_root.mkdir()
        self.route = self.tmp / "route.json"
        write_route(self.route, ["plan/**"])
        self.state = self.tmp / "state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def snapshot(self):
        result = run(
            [
                "snapshot",
                "--root", str(self.root),
                "--out", str(self.state),
                "--route-id", ROUTE_ID,
                "--node", NODE,
                "--attempt-id", ATTEMPT,
                "--artifact-root", str(self.artifact_root),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def compare(self):
        return run(
            [
                "compare",
                "--root", str(self.root),
                "--state", str(self.state),
                "--route", str(self.route),
                "--route-id", ROUTE_ID,
                "--node", NODE,
                "--attempt-id", ATTEMPT,
                "--artifact-root", str(self.artifact_root),
            ]
        )

    def test_no_change_no_violation(self):
        (self.root / "plan").mkdir()
        (self.root / "plan" / "a.md").write_text("x", encoding="utf-8")
        self.snapshot()
        result = self.compare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"violations": []})

    def test_new_file_in_scope_passes(self):
        self.snapshot()
        (self.root / "plan").mkdir()
        (self.root / "plan" / "new.md").write_text("x", encoding="utf-8")
        result = self.compare()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_file_out_of_scope_is_violation(self):
        self.snapshot()
        (self.root / "outside.md").write_text("x", encoding="utf-8")
        result = self.compare()
        self.assertEqual(result.returncode, 2)
        violations = json.loads(result.stdout)
        self.assertEqual(violations[0]["change_kind"], "new")
        self.assertEqual(violations[0]["relpath"], "outside.md")

    def test_digest_change_out_of_scope_is_violation(self):
        (self.root / "outside.md").write_text("before", encoding="utf-8")
        self.snapshot()
        (self.root / "outside.md").write_text("after", encoding="utf-8")
        result = self.compare()
        self.assertEqual(result.returncode, 2)
        violations = json.loads(result.stdout)
        self.assertEqual(violations[0]["change_kind"], "digest-changed")

    def test_deletion_out_of_scope_is_violation(self):
        (self.root / "outside.md").write_text("x", encoding="utf-8")
        self.snapshot()
        (self.root / "outside.md").unlink()
        result = self.compare()
        self.assertEqual(result.returncode, 2)
        violations = json.loads(result.stdout)
        self.assertEqual(violations[0]["change_kind"], "deleted")

    def test_mode_change_out_of_scope_is_violation(self):
        p = self.root / "outside.sh"
        p.write_text("x", encoding="utf-8")
        p.chmod(0o644)
        self.snapshot()
        p.chmod(0o755)
        result = self.compare()
        self.assertEqual(result.returncode, 2)
        violations = json.loads(result.stdout)
        self.assertEqual(violations[0]["change_kind"], "mode-changed")

    def test_dot_prefixed_paths_are_exempt(self):
        self.snapshot()
        (self.root / ".runtime").mkdir()
        (self.root / ".runtime" / "state.json").write_text("{}", encoding="utf-8")
        result = self.compare()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sealed_header_identity_mismatch_is_hard_failure(self):
        self.snapshot()
        result = run(
            [
                "compare",
                "--root", str(self.root),
                "--state", str(self.state),
                "--route", str(self.route),
                "--route-id", "rt-different",
                "--node", NODE,
                "--attempt-id", ATTEMPT,
                "--artifact-root", str(self.artifact_root),
            ]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("postscan-state-identity-mismatch", result.stderr)

    def test_fast_path_reuses_digest_when_size_and_mtime_unchanged(self):
        p = self.root / "plan"
        p.mkdir()
        (p / "a.md").write_text("x", encoding="utf-8")
        self.snapshot()
        first = json.loads(self.state.read_text(encoding="utf-8"))
        self.snapshot()  # second snapshot, nothing changed on disk
        second = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            first["entries"]["plan/a.md"]["digest"], second["entries"]["plan/a.md"]["digest"]
        )


if __name__ == "__main__":
    unittest.main()
