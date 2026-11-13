#!/usr/bin/env python3
"""Compatibility checks for immutable owner launch records and optional turn evidence."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "1fddc2ab341a56f8aa2edecfdf607c43bc1733ad"
sys.path.insert(0, str(ROOT / "utilities"))
import commit_policy  # noqa: E402
import dispatch_replacement as replacement  # noqa: E402
import route_authority  # noqa: E402


class OwnerReplacementPermissionCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-permission-compat-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.baseline_file = self.root / "route_authority_baseline.py"
        source = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{BASELINE}:utilities/route_authority.py"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.baseline_file.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("route_authority_baseline", self.baseline_file)
        self.baseline = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = self.baseline
        spec.loader.exec_module(self.baseline)

    def new_launch_input(self):
        attempt = "att-compat"
        args = SimpleNamespace(
            attempt_id=attempt, jobs_path=self.jobs, worktree=str(self.worktree),
            route_id="rt-compat", route_node="execute", route_hash="sha256:" + "a" * 64,
            replacement_input_argv=["--start", "--attempt-id", attempt, "--prompt-text", "task"],
            replacement_runtime_sandbox="workspace-write", launch_lifecycle="detached",
            nested_headless_network=True,
        )
        fragment = replacement.seal_launch_input(args, "codex", "task")
        metadata = dict(part.split("=", 1) for part in fragment.lstrip(",").split(","))
        return replacement.launch_input(self.jobs, attempt, metadata)

    def test_new_producer_record_remains_readable_by_baseline_reader(self):
        payload = self.new_launch_input()
        self.assertEqual(payload["schema"], replacement.SCHEMA)
        self.assertTrue({"attempt_id", "jobs", "worktree", "argv", "task", "resolved",
                         "launch_home", "applied_permissions"}.issubset(payload))
        before = self.baseline.granted_permissions(
            payload["applied_permissions"], payload["launch_home"], payload["worktree"])
        current = route_authority.granted_permissions(
            payload["applied_permissions"], payload["launch_home"], payload["worktree"])
        # The older reader remains readable and preserves its then-current
        # projection. The new reader adds runtime_sandbox to the compared
        # permission tuple because this task identified it as a real axis.
        self.assertEqual({key: value for key, value in current.items() if key != "runtime_sandbox"},
                         before)
        self.assertEqual(current["runtime_sandbox"], "workspace-write")
        self.assertTrue(self.baseline.same_sealed_work(payload, payload))

    def test_optional_turn_projection_does_not_mutate_launch_record_or_break_old_readers(self):
        payload = self.new_launch_input()
        before = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        event = commit_policy.permission_projection_event(
            {"git_state_known": True, "git_operation": None, "observation_only": False,
             "may_commit": True},
            adapter="codex", attempt_id=payload["attempt_id"], thread_id="thread-compat",
            turn_id="turn-1", sandbox="workspace-write", network_access=True,
            file_enforcement="os-sandbox", read_enforcement="os-sandbox",
            network_enforcement="os-sandbox",
        )
        self.assertEqual(event["type"], "dispatch.supervisor.permission-projection")
        self.assertEqual(event["attempt_id"], payload["attempt_id"])
        self.assertEqual(json.dumps(payload, sort_keys=True, separators=(",", ":")), before)
        self.assertTrue(self.baseline.same_sealed_work(payload, payload))

    def test_legacy_record_without_turn_evidence_keeps_the_same_current_judgment(self):
        payload = self.new_launch_input()
        legacy = json.loads(json.dumps(payload))
        legacy.pop("turn_permission_projection", None)
        self.assertTrue(route_authority.same_sealed_work(legacy, payload))
        self.assertTrue(self.baseline.same_sealed_work(legacy, payload))


if __name__ == "__main__":
    unittest.main()
