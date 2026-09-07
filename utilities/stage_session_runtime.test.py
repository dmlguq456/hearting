#!/usr/bin/env python3
"""W7D: the stage-session ledger lives under `.runtime/`, never a legacy bucket."""
from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_producer  # noqa: E402
import stage_session_runtime as runtime  # noqa: E402
from dispatch_contract import DispatchContractError  # noqa: E402


def _args(worktree: Path, brief: Path, fixed: Path, **overrides) -> argparse.Namespace:
    values = dict(
        subsession_id="ss-0001", subsession_index=1, subsession_count=1, subsession_mode="serial",
        subsession_purpose="planned", session_chain_id="ssc-0001", phase_brief=str(brief),
        stage_authority=0, fixed_file=[str(fixed)], narrow_verify="python3 -c pass",
        expected_round_trips=1, state_dir=None, dispatch_depth=2, route_id="rt-0123456789abcdef",
        route_node="execute", attempt_id="att-" + "1" * 32, worktree=str(worktree),
        execution_surface="registered-headless", registered_worker="1", fallback_hop="same-harness-headless",
        capability_owner="owner",
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class StageSessionStateDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "artifact-root"
        self.root.mkdir()
        self.worktree = base / "wt"
        self.worktree.mkdir()
        self.fixed = self.worktree / "a.py"
        self.fixed.write_text("x\n", encoding="utf-8")
        self.brief = base / "brief.md"
        self.brief.write_text("brief\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_state_dir_is_runtime_owned(self):
        self.assertEqual(
            runtime.default_state_dir(self.root, "rt-0123456789abcdef"),
            self.root / ".runtime" / "stage-sessions" / "rt-0123456789abcdef",
        )
        args = _args(self.worktree, self.brief, self.fixed)
        runtime.bind(args, artifact_root=self.root, action="dry-run")
        ledger = Path(args.state_ledger)
        self.assertEqual(ledger.parent, self.root / ".runtime" / "stage-sessions" / "rt-0123456789abcdef")
        self.assertEqual(artifact_producer.check_write(self.root, ledger)["reason"], "runtime-owned")

    def test_dry_run_creates_nothing(self):
        args = _args(self.worktree, self.brief, self.fixed)
        runtime.bind(args, artifact_root=self.root, action="dry-run")
        self.assertFalse((self.root / ".runtime").exists())

    def test_legacy_state_dir_is_refused_once_the_cutover_is_active(self):
        artifact_producer.activate(self.root, repository_id="repo_" + "c" * 32, artifact_root_id="root_" + "d" * 32)
        legacy = self.root / "plans" / "stage-sessions" / "rt-0123456789abcdef" / "_internal" / "state"
        args = _args(self.worktree, self.brief, self.fixed, state_dir=str(legacy))
        with self.assertRaises(DispatchContractError) as caught:
            runtime.bind(args, artifact_root=self.root, action="start")
        self.assertEqual(caught.exception.reason, "subsession-state-dir-write-denied")
        self.assertIn("legacy-top-level-write-denied", caught.exception.detail)
        self.assertFalse(legacy.exists())
        self.assertFalse(Path(str(legacy) + ".lock").exists())
        default = _args(self.worktree, self.brief, self.fixed)
        runtime.bind(default, artifact_root=self.root, action="dry-run")
        self.assertTrue(default.state_ledger.startswith(str(self.root / ".runtime" / "stage-sessions")))


class StageSessionMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "artifact-root"
        self.root.mkdir()
        self.worktree = base / "wt"
        self.worktree.mkdir()
        self.fixed = self.worktree / "a.py"
        self.fixed.write_text("x\n", encoding="utf-8")
        self.brief = base / "brief.md"
        self.brief.write_text("brief\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parallel_metadata_carries_batch_group_serial_does_not(self):
        parallel_args = _args(
            self.worktree, self.brief, self.fixed,
            subsession_mode="parallel", session_chain_id="ssc-parallel",
        )
        runtime.bind(parallel_args, artifact_root=self.root, action="dry-run")
        parallel_metadata = runtime.metadata(parallel_args)
        self.assertIn(",batch_group=ssc-parallel", parallel_metadata)

        serial_args = _args(
            self.worktree, self.brief, self.fixed,
            subsession_mode="serial", session_chain_id="ssc-serial",
        )
        runtime.bind(serial_args, artifact_root=self.root, action="dry-run")
        serial_metadata = runtime.metadata(serial_args)
        self.assertNotIn("batch_group=", serial_metadata)

    def test_metadata_is_identical_across_register_and_start_actions(self):
        register_args = _args(self.worktree, self.brief, self.fixed)
        runtime.bind(register_args, artifact_root=self.root, action="register")
        register_metadata = runtime.metadata(register_args)

        start_args = _args(self.worktree, self.brief, self.fixed)
        runtime.bind(start_args, artifact_root=self.root, action="start")
        start_metadata = runtime.metadata(start_args)

        self.assertEqual(register_metadata, start_metadata)


if __name__ == "__main__":
    unittest.main()
