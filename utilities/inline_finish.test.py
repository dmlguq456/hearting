#!/usr/bin/env python3
"""Public CLI and forward-recovery tests for direct inline finish."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import concurrent.futures
import unittest
import unittest.mock
import uuid
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_lifecycle
import artifact_producer
import dispatch_terminal_commit
import inline_finish

CAP_SPEC = importlib.util.spec_from_file_location(
    "inline_finish_capability_route", ROOT / "utilities/capability-route.py")
CAP = importlib.util.module_from_spec(CAP_SPEC)
CAP_SPEC.loader.exec_module(CAP)

GATES = ["atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
         "no-artifact-handoff", "no-independent-verifier", "focused-verification"]


class PendingForCycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="inline-finish-pending-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / ".runtime/inline-finish/v1"

    def slot(self, name, state="finished", cycle="cyc-current"):
        path = self.home / name / "finish.json"
        row = {"schema": "inline_finish_v1", "intent": {"cycle_id": cycle}, "state": state}
        if state is None:
            row.pop("state")
        inline_finish._atomic(path, row)
        return path, row

    def test_only_finished_slots_leave_no_pending_work_and_remain_readable(self):
        stored = [self.slot("rt-old-" + str(n)) for n in range(3)]
        before = {path: path.read_bytes() for path, _ in stored}
        self.assertIsNone(inline_finish.pending_for_cycle(self.root, "cyc-current"))
        for path, row in stored:
            self.assertEqual(inline_finish.pending_state(self.root, path.parent.name), row)
            self.assertEqual(path.read_bytes(), before[path])

    def test_one_unfinished_slot_is_selected_beside_finished_history(self):
        old, _ = self.slot("rt-old")
        current, row = self.slot("rt-continuation", "route-closed")
        before = {path: path.read_bytes() for path in (old, current)}
        self.assertEqual(inline_finish.pending_for_cycle(self.root, "cyc-current"), row)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_two_unfinished_slots_still_refuse_ambiguity(self):
        self.slot("rt-old")
        self.slot("rt-current", "route-closed")
        self.slot("rt-competing", "claimed")
        with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-cycle-ambiguous"):
            inline_finish.pending_for_cycle(self.root, "cyc-current")

    def test_other_cycles_are_not_pending_for_this_cycle(self):
        self.slot("rt-foreign", "route-closed", "cyc-other")
        self.slot("rt-foreign-next", "claimed", "cyc-other")
        self.assertIsNone(inline_finish.pending_for_cycle(self.root, "cyc-current"))

    def test_unknown_and_missing_states_remain_pending(self):
        self.slot("rt-old")
        for state in ("future-state", None):
            with self.subTest(state=state):
                _, row = self.slot("rt-unknown", state)
                self.assertEqual(inline_finish.pending_for_cycle(self.root, "cyc-current"), row)
                with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-in-progress"):
                    inline_finish.assert_not_pending(self.root, "rt-unknown")

    def test_symlink_slots_and_route_directories_are_still_refused(self):
        path, _ = self.slot("rt-old")
        raw = path.read_bytes()
        outside = self.root / "outside.json"
        outside.write_bytes(raw)
        path.unlink(); path.symlink_to(outside)
        with self.assertRaises(inline_finish.InlineFinishError):
            inline_finish.pending_for_cycle(self.root, "cyc-current")
        self.assertEqual(outside.read_bytes(), raw)
        path.unlink(); path.write_bytes(raw)
        (self.home / "rt-link").symlink_to(path.parent, target_is_directory=True)
        with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-intent-unsafe"):
            inline_finish.pending_for_cycle(self.root, "cyc-current")

    def test_bad_json_or_schema_is_checked_before_finished_state_is_ignored(self):
        path, _ = self.slot("rt-old")
        for payload in (b"not-json", b'{"schema":"other","state":"finished","intent":{"cycle_id":"cyc-current"}}'):
            with self.subTest(payload=payload):
                path.write_bytes(payload)
                with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-intent-corrupt"):
                    inline_finish.pending_for_cycle(self.root, "cyc-current")


class PublicInlineFinishTest(unittest.TestCase):
    CAPABILITY = "autopilot-code"
    CAPABILITY_MODE = "dev"
    CAMPAIGN_ARGUMENTS = ("--campaign-key", "inline-finish-test")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="inline-finish-test-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.root = self.base / "artifacts"
        self.root.mkdir()
        # The public compiler seals the harness source root as its runtime
        # identity. Keep that real source root while isolating mutable session
        # data with unique ids.
        self.home = ROOT
        self.jobs = self.base / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Inline Finish Test"], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.old_env = {key: os.environ.get(key) for key in (
            "AGENT_HOME", "AGENT_DISPATCH_JOBS", "AGENT_DISPATCH_DEPTH",
            "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_ROUTE_FILE", "AGENT_ROUTE_ID",
            "AGENT_ROUTE_NODE", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID",
            "OPENCODE_SESSION_ID", "AGENT_DISPATCH_CALLER_HARNESS", "XDG_STATE_HOME",
            "HEARTING_INLINE_FINISH_CRASH_AT", "AGENT_ARTIFACT_CHECKPOINT",
            "HEARTING_WORKFLOW_GROUP_REVIEW")}
        os.environ.update({
            "AGENT_HOME": str(self.home), "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_DISPATCH_DEPTH": "0", "CODEX_THREAD_ID": "inline-finish-test-session",
            "XDG_STATE_HOME": str(self.base / "state"),
            # Isolate compose/start as well as finish: automatic detached
            # checkpoints must not outlive this fixture's temporary root.
            "AGENT_ARTIFACT_CHECKPOINT": "off",
            # Sealing also owns a separate detached group-review sweep. This
            # fixture tests finish/admission, not that background lifecycle.
            "HEARTING_WORKFLOW_GROUP_REVIEW": "off",
        })
        for key in ("AGENT_DISPATCH_ATTEMPT_ID", "AGENT_ROUTE_FILE", "AGENT_ROUTE_ID",
                    "AGENT_ROUTE_NODE", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID",
                    "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                    "HEARTING_INLINE_FINISH_CRASH_AT"):
            os.environ.pop(key, None)
        artifact_producer.activate(
            self.root, repository_id="repo_" + "a" * 32,
            artifact_root_id="root_" + "b" * 32,
            w7={"campaign_id": "camp_" + "c" * 32},
        )
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec", "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        self.prompt = self.base / "task.md"
        self.prompt.write_text("Exercise the direct inline finish transaction.\n", encoding="utf-8")
        compose = [sys.executable, str(ROOT / "utilities/capability-route.py"), "compose",
                   "--slug", "inline-finish-test", *self.CAMPAIGN_ARGUMENTS,
                   "--shape", "direct", "--capability", self.CAPABILITY, "--capability-mode", self.CAPABILITY_MODE,
                   "--intensity", "direct", "--cwd", str(self.repo),
                   "--artifact-root", str(self.root), "--tracking", "tracked",
                   "--prompt-file", str(self.prompt),
                   "--spec-read", "fixture", "--drift-verdict", "within-spec", "--artifact-guard", "fixture",
                   "--owner", "codex", "--parent-harness", "codex", *self.extra_compose_arguments()]
        composed = subprocess.run(compose, cwd=self.repo, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(composed.returncode, 0, composed.stderr)
        route_receipt = json.loads(composed.stdout)
        self.route_file = Path(route_receipt["route_file"])
        self.route = json.loads(self.route_file.read_text(encoding="utf-8"))
        started = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "start",
                                  "--route", str(self.route_file), "--jobs", str(self.jobs)],
                                 cwd=self.repo, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.cycle = artifact_producer.route_cycle_for(self.root, self.route)
        self.assertIsNotNone(self.cycle, started.stdout + started.stderr + repr(artifact_producer.status(self.root)))
        self.cycle_dir = artifact_producer.cycle_dir(
            self.root, self.cycle["campaign_id"], self.cycle["cycle_id"], self.cycle)
        self.evidence = self.cycle_dir / "artifacts/documents/evidence.md"
        self.evidence.parent.mkdir(parents=True)
        self.evidence.write_bytes(b"exact terminal evidence\n")
        self.summary = self.base / "summary.md"
        self.summary.write_text("Finished the inline route.\n", encoding="utf-8")
        self.command = [
            sys.executable, str(ROOT / "utilities/capability-route.py"), "finish",
            "--route", str(self.route_file), "--evidence", str(self.evidence),
            "--summary-file", str(self.summary),
        ]
        self.env = os.environ.copy()
        # `complete` would start a detached checkpoint that outlives the fixture root.
        self.env["AGENT_ARTIFACT_CHECKPOINT"] = "off"

    def extra_compose_arguments(self):
        return []

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def finish(self, fault=None):
        env = self.env.copy()
        if fault:
            env["HEARTING_INLINE_FINISH_CRASH_AT"] = fault
        else:
            env.pop("HEARTING_INLINE_FINISH_CRASH_AT", None)
        return subprocess.run(self.command, cwd=self.repo, env=env, capture_output=True, text=True)

    def finish_as(self, harness, session_id):
        env = self.env.copy()
        for key in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID"):
            env.pop(key, None)
        env[{"codex":"CODEX_THREAD_ID", "claude":"CLAUDE_CODE_SESSION_ID",
             "opencode":"OPENCODE_SESSION_ID"}[harness]] = session_id
        env["AGENT_DISPATCH_CALLER_HARNESS"] = harness
        return subprocess.run(self.command, cwd=self.repo, env=env, capture_output=True, text=True)

    def test_finish_without_registry_environment_or_existing_default(self):
        self.env.pop("AGENT_DISPATCH_JOBS", None)
        self.env["XDG_STATE_HOME"] = str(self.base / "empty-state")
        receipt = self.finish()
        self.assertEqual(receipt.returncode, 0, receipt.stderr + receipt.stdout)
        self.assertFalse((self.base / "empty-state/hearting/dispatch/jobs.log").exists())

    def test_finish_discovers_an_existing_default_registry(self):
        self.env.pop("AGENT_DISPATCH_JOBS", None)
        self.env["XDG_STATE_HOME"] = str(self.base / "default-state")
        registry = self.base / "default-state/hearting/dispatch/jobs.log"
        registry.parent.mkdir(parents=True)
        registry.write_text("field\tfield\tfield\tfield\tfield\troute_id=" + self.route["route_id"] + "\n")
        receipt = self.finish()
        self.assertNotEqual(receipt.returncode, 0)
        self.assertIn("finish-registered-route-ineligible", receipt.stderr)

    def test_loose_evidence_stays_in_place_and_replays(self):
        loose = self.cycle_dir / "artifacts/code_change.md"
        self.evidence.rename(loose)
        self.command[self.command.index("--evidence") + 1] = str(loose)
        receipt = self.finish()
        self.assertEqual(receipt.returncode, 0, receipt.stderr + receipt.stdout)
        self.assertTrue(loose.is_file())
        self.assertFalse((self.cycle_dir / "artifacts/plans/code_change.md").exists())
        replay = self.finish()
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)

    def test_first_finish_consumes_same_route_historical_false_close(self):
        initial, created = CAP.close_route(self.route, self.route_file, allow_unproven=True, jobs=self.jobs)
        self.assertTrue(created)
        self.assertFalse(initial["terminal_gate_proven"])
        original = CAP.outcome_path(self.route_file).read_bytes()
        result = self.finish()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["inline_finish_id"])
        outcome = json.loads(CAP.outcome_path(self.route_file).read_text(encoding="utf-8"))
        self.assertTrue(outcome["terminal_gate_proven"])
        retained = list(self.route_file.parent.glob(
            f"{self.route_file.stem}.historical-false-*.outcome.json"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), original)

    def _historical_false_close(self):
        initial, created = CAP.close_route(self.route, self.route_file, allow_unproven=True, jobs=self.jobs)
        self.assertTrue(created)
        self.assertFalse(initial["terminal_gate_proven"])
        return CAP.outcome_path(self.route_file).read_bytes()

    def _assert_false_close_finish_resumes_once(self, fault):
        original = self._historical_false_close()
        crashed = self.finish(fault)
        self.assertNotEqual(crashed.returncode, 0)
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])
        outcome_path = CAP.outcome_path(self.route_file)
        self.assertTrue(json.loads(outcome_path.read_text(encoding="utf-8"))["terminal_gate_proven"])
        retained = list(self.route_file.parent.glob(f"{self.route_file.stem}.historical-false-*.outcome.json"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), original)
        settled = outcome_path.read_bytes()
        again = self.finish()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(outcome_path.read_bytes(), settled)
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_historical_false_close_finish_resumes_after_marker_write_crash(self):
        self._assert_false_close_finish_resumes_once("after-marker-write")

    def test_historical_false_close_finish_resumes_after_close_write_crash(self):
        self._assert_false_close_finish_resumes_once("after-close-write")

    def test_historical_false_close_finish_resumes_after_manifest_crash(self):
        self._assert_false_close_finish_resumes_once("after-manifest")

    def test_historical_false_close_finish_rejects_another_intent_and_keeps_bytes(self):
        original = self._historical_false_close()
        self.assertNotEqual(self.finish("after-claim").returncode, 0)
        self.summary.write_text("a different intent\n", encoding="utf-8")
        other = self.finish()
        self.assertNotEqual(other.returncode, 0)
        self.assertIn("finish-intent-conflict", other.stderr)
        self.assertEqual(CAP.outcome_path(self.route_file).read_bytes(), original)
        self.assertEqual(list(self.route_file.parent.glob("*.historical-false-*")), [])
        self.summary.write_text("Finished the inline route.\n", encoding="utf-8")
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    def _inline_identity_false_close(self, **different):
        """A false close that carries this inline execution's own actual tuple, with no finish state left.

        The marker, intent id and binding are the ones a real first finish made (it is stopped right
        after its marker); the close is recorded while that gate reads unproven, as an earlier
        caller could have, and the finish state is then absent, so the next finish is a first one."""
        crashed = self.finish("after-marker")
        self.assertNotEqual(crashed.returncode, 0)
        state_path = self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        record = artifact_producer.route_cycle_for(self.root, self.route)
        identity = artifact_lifecycle.read_root_identity(self.root)
        campaign = artifact_producer.campaign_or_tombstone(self.root, record["campaign_id"])
        binding = {"kind": "inline_producer_binding_v1", "artifact_root_id": identity.artifact_root_id,
                   "campaign_key": campaign.get("key"), "campaign_id": record["campaign_id"],
                   "cycle_id": record["cycle_id"], "producer_id": record["producer_id"],
                   "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                   "cycle_record_digest": dispatch_terminal_commit.cycle_identity_digest(record),
                   "terminal_marker_digest": state["terminal_marker_digest"],
                   "evidence_sha256": state["intent"]["evidence_sha256"], "inline_finish_id": state["inline_finish_id"]}
        held = dict(inline_finish_id=state["inline_finish_id"], inline_commit=state["intent"]["commit"],
                    expected_terminal_marker_digest=state["terminal_marker_digest"],
                    expected_summary_digest=state["intent"]["summary_sha256"],
                    expected_producer_binding_digest=inline_finish._digest(
                        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()))
        held.update(different)
        recorded_commit = held.pop("commit", state["intent"]["commit"])
        state_path.unlink()
        unproven = {"inline": {"passed": False, "reason": "completion-attempt-not-current"}}
        with unittest.mock.patch.object(CAP, "terminal_gate_observation", return_value=unproven):
            outcome, created = CAP.close_route(
                self.route, self.route_file, recorded_commit, "an earlier caller's close",
                allow_unproven=True, jobs=self.jobs, **held)
        self.assertTrue(created)
        self.assertFalse(outcome["terminal_gate_proven"])
        self.assertEqual(outcome["inline_finish_id"], held["inline_finish_id"])
        return CAP.outcome_path(self.route_file).read_bytes(), state_path

    def test_first_inline_finish_consumes_a_historical_false_close_carrying_its_own_inline_tuple(self):
        original, state_path = self._inline_identity_false_close()
        result = self.finish()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        outcome = json.loads(CAP.outcome_path(self.route_file).read_text(encoding="utf-8"))
        self.assertTrue(outcome["terminal_gate_proven"])
        self.assertEqual(outcome["inline_finish_id"], receipt["inline_finish_id"])
        retained = list(self.route_file.parent.glob(f"{self.route_file.stem}.historical-false-*.outcome.json"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), original)
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")
        settled = CAP.outcome_path(self.route_file).read_bytes()
        again = self.finish()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(json.loads(again.stdout)["replay"])
        self.assertEqual(CAP.outcome_path(self.route_file).read_bytes(), settled)
        self.assertEqual(len(list(self.route_file.parent.glob("*.historical-false-*.outcome.json"))), 1)

    def _route_closed_false_finish(self):
        from types import SimpleNamespace
        original, state_path = self._inline_identity_false_close()
        args = SimpleNamespace(evidence=str(self.evidence), summary_file=str(self.summary), commit=None)
        unknown = {"inline": {"passed": False, "reason": "completion-attempt-not-current"}}

        def stop_after_close(point):
            if point == "after-close":
                raise inline_finish.InlineFinishError("fixture-after-close")

        with unittest.mock.patch.object(CAP, "terminal_gate_observation", return_value=unknown), \
                unittest.mock.patch.object(inline_finish, "_fault", side_effect=stop_after_close):
            with self.assertRaisesRegex(inline_finish.InlineFinishError, "fixture-after-close"):
                inline_finish.finish(args, self.route, self.route_file, CAP)
        self.assertEqual(json.loads(state_path.read_text())["state"], "route-closed")
        self.assertEqual(CAP.outcome_path(self.route_file).read_bytes(), original)
        self.assertTrue(CAP.terminal_gate_proven(CAP.terminal_gate_observation(self.route, jobs=self.jobs)))
        return original, state_path

    def test_route_closed_false_finish_consumes_late_completion_and_replays_once(self):
        original, state_path = self._route_closed_false_finish()
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])
        outcome_path = CAP.outcome_path(self.route_file)
        settled = outcome_path.read_bytes()
        self.assertIs(json.loads(settled)["terminal_gate_proven"], True)
        self.assertEqual(json.loads(state_path.read_text())["state"], "finished")
        retained = list(self.route_file.parent.glob("*.historical-false-*.outcome.json"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), original)
        before = {path: path.read_bytes() for path in (state_path, outcome_path, self.cycle_dir / "manifest.json")}
        again = self.finish()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(len(list(self.route_file.parent.glob("*.historical-false-*.outcome.json"))), 1)

    def test_route_closed_false_finish_resumes_after_close_publication_before_state_update(self):
        original, state_path = self._route_closed_false_finish()
        crashed = self.finish("after-close-write")
        self.assertNotEqual(crashed.returncode, 0)
        self.assertIn("fault-injected-after-close-write", crashed.stderr)
        self.assertEqual(json.loads(state_path.read_text())["state"], "node-completed")
        settled = CAP.outcome_path(self.route_file).read_bytes()
        self.assertIs(json.loads(settled)["terminal_gate_proven"], True)
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(CAP.outcome_path(self.route_file).read_bytes(), settled)
        retained = list(self.route_file.parent.glob("*.historical-false-*.outcome.json"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), original)

    def test_route_closed_false_finish_refuses_foreign_intent_and_outcome_drift(self):
        original, state_path = self._route_closed_false_finish()
        original_state = state_path.read_bytes()
        self.summary.write_text("foreign intent\n", encoding="utf-8")
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-intent-conflict", refused.stderr)
        self.assertEqual(state_path.read_bytes(), original_state)
        self.assertEqual(CAP.outcome_path(self.route_file).read_bytes(), original)
        self.summary.write_text("Finished the inline route.\n", encoding="utf-8")
        outcome_path = CAP.outcome_path(self.route_file)
        outcome = json.loads(original)
        outcome["inline_finish_id"] = "f" * 64
        outcome_path.write_text(json.dumps(outcome, sort_keys=True) + "\n", encoding="utf-8")
        drifted = outcome_path.read_bytes()
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-outcome-drift", refused.stderr)
        self.assertEqual(outcome_path.read_bytes(), drifted)
        self.assertEqual(state_path.read_bytes(), original_state)
        self.assertEqual(list(self.route_file.parent.glob("*.historical-false-*")), [])

    def test_route_closed_false_finish_preserves_actual_cancelled_workflow(self):
        from workflow_state import WorkflowLedger
        original, state_path = self._route_closed_false_finish()
        ledger = WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        for state in ("READY", "RUNNING", "CANCELLED"):
            ledger.set_workflow_state(state, actor="fixture", evidence={"reason": "user stop"})
        journal, cache = ledger.journal_path.read_bytes(), ledger.state_path.read_bytes()
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("completion-terminal-gate-unproven", refused.stderr)
        self.assertEqual(CAP.outcome_path(self.route_file).read_bytes(), original)
        self.assertEqual(list(self.route_file.parent.glob("*.historical-false-*")), [])
        self.assertEqual(json.loads(state_path.read_text())["state"], "route-closed")
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "open")
        self.assertEqual(ledger.journal_path.read_bytes(), journal)
        self.assertEqual(ledger.state_path.read_bytes(), cache)

    def test_route_closed_false_finish_keeps_unknown_completion_unproven(self):
        from types import SimpleNamespace
        original, state_path = self._route_closed_false_finish()
        original_state = state_path.read_bytes()
        args = SimpleNamespace(evidence=str(self.evidence), summary_file=str(self.summary), commit=None)
        unknown = {"inline": {"passed": False, "reason": "completion-attempt-not-current"}}
        with unittest.mock.patch.object(CAP, "terminal_gate_observation", return_value=unknown):
            with self.assertRaisesRegex(artifact_producer.ProducerError, "completion-terminal-gate-unproven"):
                inline_finish.finish(args, self.route, self.route_file, CAP)
        self.assertEqual(state_path.read_bytes(), original_state)
        self.assertEqual(CAP.outcome_path(self.route_file).read_bytes(), original)
        self.assertEqual(list(self.route_file.parent.glob("*.historical-false-*")), [])

    def test_first_inline_finish_with_its_own_inline_tuple_resumes_after_each_crash_once(self):
        original, state_path = self._inline_identity_false_close()
        for fault in ("after-claim", "after-close-write", "after-manifest"):
            crashed = self.finish(fault)
            self.assertNotEqual(crashed.returncode, 0, fault)
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        retained = list(self.route_file.parent.glob(f"{self.route_file.stem}.historical-false-*.outcome.json"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), original)
        self.assertTrue(json.loads(CAP.outcome_path(self.route_file).read_text(encoding="utf-8"))["terminal_gate_proven"])

    def test_first_inline_finish_refuses_a_false_close_whose_inline_tuple_is_not_its_own(self):
        for label, different in (("another intent", dict(inline_finish_id="0" * 64)),
                                 ("another summary", dict(expected_summary_digest="1" * 64)),
                                 ("another commit", dict(commit="2" * 40)),
                                 ("another marker", dict(expected_terminal_marker_digest="3" * 64)),
                                 ("another binding", dict(expected_producer_binding_digest="4" * 64))):
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                original, state_path = self._inline_identity_false_close(**different)
                marker_file = CAP.completion_dir(self.route["route_id"]) / "inline.json"
                marker_before = marker_file.read_bytes()
                refused = self.finish()
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("finish-route-outcome-conflict", refused.stderr)
                self.assertEqual(CAP.outcome_path(self.route_file).read_bytes(), original)
                self.assertEqual(marker_file.read_bytes(), marker_before)
                self.assertFalse(state_path.exists())  # nothing was claimed
                self.assertEqual(list(self.route_file.parent.glob("*.historical-false-*")), [])

    def test_identity_bearing_historical_false_close_is_not_taken_over_by_a_first_inline_finish(self):
        # A false record that names a registered owner's tuple is that owner's: the inline
        # caller has no prior intent from which to supply the identity, so it is not consumed here.
        initial, created = CAP.close_route(self.route, self.route_file, allow_unproven=True, jobs=self.jobs,
                                           terminal_commit_id="c" * 40, expected_owner_attempt_id="att-registered-owner")
        self.assertTrue(created)
        original = CAP.outcome_path(self.route_file).read_bytes()
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-route-outcome-conflict", refused.stderr)
        self.assertEqual(CAP.outcome_path(self.route_file).read_bytes(), original)

    def test_public_compose_start_finish_cli_flow_and_campaign_seal(self):
        receipt = self.finish()
        self.assertEqual(receipt.returncode, 0, receipt.stderr)
        self.assertEqual(json.loads(receipt.stdout)["schema"], "finish_receipt_v1")
        self.assertEqual(self.route.get("campaign_key"), "inline-finish-test")
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_depth_zero_public_finish_resolves_all_supported_harness_sessions(self):
        # Independent fixtures preserve the one-shot finish state for each
        # caller identity across the supported runtimes.
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                fixture = PublicInlineFinishTest("test_public_finish_seals_and_exact_replay_returns_one_receipt")
                fixture.setUp()
                try:
                    result = fixture.finish_as(harness, "inline-finish-" + harness)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["schema"], "finish_receipt_v1")
                finally:
                    fixture.tearDown()

    def test_unassigned_and_parent_cycle_direct_routes_finish(self):
        # f7 2026-10-16: a route composed with `--unassigned` or `--parent-cycle` names no
        # campaign key of its own, and finish refused it as finish-campaign-key-mismatch.
        for name, arguments in (("unassigned", ("--unassigned",)),
                                ("parent-cycle", ("--parent-cycle", self.cycle["cycle_id"]))):
            with self.subTest(selection=name):
                fixture = type("Fixture", (PublicInlineFinishTest,), {"CAMPAIGN_ARGUMENTS": arguments})(
                    self._testMethodName)
                if name == "unassigned":
                    fixture.setUp()
                else:
                    self._compose_child(fixture)
                try:
                    self.assertIsNone(fixture.route.get("campaign_key"))
                    first = fixture.finish()
                    self.assertEqual(first.returncode, 0, first.stderr)
                    self.assertEqual(json.loads(first.stdout)["schema"], "finish_receipt_v1")
                    record = artifact_producer.read_cycle_record(fixture.root, fixture.cycle["cycle_id"])
                    self.assertEqual(record["state"], "sealed")
                    replay = fixture.finish()
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                    self.assertTrue(json.loads(replay.stdout)["replay"])
                finally:
                    if name == "unassigned":
                        fixture.tearDown()

    def _compose_child(self, fixture):
        """Compose and start `fixture`'s direct route in this repository, artifact root and registry."""
        fixture.repo, fixture.root, fixture.env = self.repo, self.root, self.env.copy()
        prompt = self.base / "child-task.md"
        prompt.write_text("Finish a direct route that joins its parent cycle.\n", encoding="utf-8")
        capability_route = [sys.executable, str(ROOT / "utilities/capability-route.py")]
        composed = subprocess.run([
            *capability_route, "compose", "--slug", "inline-finish-child", *fixture.CAMPAIGN_ARGUMENTS,
            "--shape", "direct", "--capability", self.CAPABILITY, "--capability-mode", self.CAPABILITY_MODE,
            "--intensity", "direct", "--cwd", str(self.repo), "--artifact-root", str(self.root),
            "--tracking", "tracked", "--prompt-file", str(prompt),
            "--spec-read", "fixture", "--drift-verdict", "within-spec", "--artifact-guard", "fixture",
            "--owner", "codex", "--parent-harness", "codex"],
            cwd=self.repo, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(composed.returncode, 0, composed.stderr)
        fixture.route_file = Path(json.loads(composed.stdout)["route_file"])
        fixture.route = json.loads(fixture.route_file.read_text(encoding="utf-8"))
        started = subprocess.run([*capability_route, "start", "--route", str(fixture.route_file),
                                  "--jobs", str(self.jobs)],
                                 cwd=self.repo, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(started.returncode, 0, started.stderr)
        fixture.cycle = artifact_producer.route_cycle_for(self.root, fixture.route)
        self.assertIsNotNone(fixture.cycle, started.stdout + started.stderr)
        evidence = artifact_producer.cycle_dir(
            self.root, fixture.cycle["campaign_id"], fixture.cycle["cycle_id"], fixture.cycle) / "artifacts/documents/evidence.md"
        evidence.parent.mkdir(parents=True)
        evidence.write_bytes(b"child terminal evidence\n")
        fixture.command = [*capability_route, "finish", "--route", str(fixture.route_file),
                           "--evidence", str(evidence), "--summary-file", str(self.summary)]

    def test_public_finish_seals_and_exact_replay_returns_one_receipt(self):
        first = self.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        receipt = json.loads(first.stdout)
        self.assertEqual(receipt["schema"], "finish_receipt_v1")
        self.assertFalse(receipt["replay"])
        record = artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])
        self.assertEqual(record["state"], "sealed")
        document = json.loads((self.cycle_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], 2)
        replay = self.finish()
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_receipt = json.loads(replay.stdout)
        self.assertTrue(replay_receipt["replay"])
        self.assertEqual(replay_receipt["inline_finish_id"], receipt["inline_finish_id"])

    def test_claimed_evidence_drift_refuses_then_exact_bytes_resume(self):
        crashed = self.finish("after-claim")
        self.assertNotEqual(crashed.returncode, 0)
        self.evidence.write_bytes(b"changed evidence\n")
        drift = self.finish()
        self.assertNotEqual(drift.returncode, 0)
        self.assertIn("finish-evidence-drift", drift.stderr)
        self.evidence.write_bytes(b"exact terminal evidence\n")
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_manifest_commit_crash_recovers_same_exact_finish(self):
        crashed = self.finish("after-manifest")
        self.assertNotEqual(crashed.returncode, 0)
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_continuation_close_recovery_preserves_finished_history_and_current_close(self):
        first = self.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        original = self.route
        original_slot = self.root / ".runtime/inline-finish/v1" / original["route_id"] / "finish.json"
        old_paths = [original_slot, self.route_file, CAP.outcome_path(self.route_file),
                     CAP.completion_dir(original["route_id"], jobs=self.jobs) / "inline.json"]
        old_bytes = {path: path.read_bytes() for path in old_paths}
        self.assertEqual(json.loads(old_bytes[original_slot])["state"], "finished")
        continuation = CAP.build_continuation_route(
            original, resume_from_node="inline", requested_boundary="inline",
            reason="inline-finish-recovery-fixture", artifact_root=self.root)
        path = CAP.canonical_route_path(self.root, continuation["route_id"])
        CAP.publish_continuation_route(continuation, original, path)
        bound = CAP.bind_continuation_cycle(self.root, original, continuation)
        self.assertTrue(bound["bound"], bound)
        self.route, self.route_file = continuation, path
        self.command[self.command.index("--route") + 1] = str(path)
        self.assertEqual(artifact_producer.route_cycle_for(self.root, continuation)["cycle_id"], self.cycle["cycle_id"])
        crashed = self.finish("after-close")
        self.assertNotEqual(crashed.returncode, 0)
        self.assertNotIn("finish-cycle-ambiguous", crashed.stderr)
        current_slot = self.root / ".runtime/inline-finish/v1" / continuation["route_id"] / "finish.json"
        current = json.loads(current_slot.read_bytes())
        self.assertEqual(current["state"], "route-closed")
        self.assertEqual(inline_finish.pending_for_cycle(self.root, self.cycle["cycle_id"]), current)
        current_paths = [path, CAP.outcome_path(path),
                         CAP.completion_dir(continuation["route_id"], jobs=self.jobs) / "inline.json"]
        current_bytes = {item: item.read_bytes() for item in current_paths}
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])
        self.assertEqual(inline_finish.pending_state(self.root, continuation["route_id"])["state"], "finished")
        self.assertIsNone(inline_finish.pending_for_cycle(self.root, self.cycle["cycle_id"]))
        self.assertEqual({item: item.read_bytes() for item in old_bytes}, old_bytes)
        self.assertEqual({item: item.read_bytes() for item in current_bytes}, current_bytes)
        replay = self.finish()
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout)["inline_finish_id"], json.loads(resumed.stdout)["inline_finish_id"])

    def test_marker_write_crash_recovers_the_same_exact_finish(self):
        crashed = self.finish("after-marker-write")
        self.assertNotEqual(crashed.returncode, 0)
        marker = CAP.completion_dir(self.route["route_id"]) / "inline.json"
        self.assertTrue(marker.is_file())
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])

    def test_concurrent_same_intent_finishes_one_cycle_and_reuses_receipt(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _n: self.finish(), (1, 2)))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        receipts = [json.loads(result.stdout) for result in (first, second)]
        self.assertEqual(receipts[0]["inline_finish_id"], receipts[1]["inline_finish_id"])
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_outcome_write_crash_recovers_exact_close(self):
        crashed = self.finish("after-close-write")
        self.assertNotEqual(crashed.returncode, 0)
        self.assertTrue(self.route_file.with_name(self.route_file.stem + ".outcome.json").is_file())
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])

    def test_index_applied_crash_recovers_exact_seal(self):
        crashed = self.finish("after-index")
        self.assertNotEqual(crashed.returncode, 0)
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])

    def test_finished_receipt_write_crash_replays_without_second_seal(self):
        crashed = self.finish("after-receipt")
        self.assertNotEqual(crashed.returncode, 0)
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        receipt = json.loads(resumed.stdout)
        self.assertTrue(receipt["replay"])
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_summary_or_evidence_path_change_conflicts_with_claim(self):
        self.assertNotEqual(self.finish("after-claim").returncode, 0)
        self.summary.write_text("different summary\n", encoding="utf-8")
        changed_summary = self.finish()
        self.assertNotEqual(changed_summary.returncode, 0)
        self.assertIn("finish-intent-conflict", changed_summary.stderr)
        alternate = self.cycle_dir / "artifacts/documents/alternate.md"
        alternate.write_bytes(self.evidence.read_bytes())
        index = self.command.index("--evidence") + 1
        old = self.command[index]
        self.command[index] = str(alternate)
        changed_path = self.finish()
        self.command[index] = old
        self.assertNotEqual(changed_path.returncode, 0)
        self.assertIn("finish-intent-conflict", changed_path.stderr)

    def test_ambiguous_cross_harness_session_is_refused_before_intent(self):
        env = self.env.copy()
        env.pop("AGENT_DISPATCH_CALLER_HARNESS", None)
        env.pop("AGENT_DISPATCH_CURRENT_HARNESS", None)
        env["CLAUDE_CODE_SESSION_ID"] = "competing-session"
        refused = subprocess.run(self.command, cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-caller-harness-ambiguous", refused.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_finished_replay_rechecks_marker_evidence_binding(self):
        first = self.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        marker_file = CAP.completion_dir(self.route["route_id"]) / "inline.json"
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
        marker["evidence"]["sha256"] = "0" * 64
        marker_file.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
        replay = self.finish()
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("finish-marker-evidence-mismatch", replay.stderr)

    def test_finished_replay_refuses_missing_outcome_or_manifest(self):
        first = self.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        outcome = self.route_file.with_name(self.route_file.stem + ".outcome.json")
        original = outcome.read_bytes()
        outcome.unlink()
        missing_outcome = self.finish()
        self.assertNotEqual(missing_outcome.returncode, 0)
        self.assertIn("finish-outcome-missing-or-corrupt", missing_outcome.stderr)
        outcome.write_bytes(original)
        manifest = self.cycle_dir / "manifest.json"
        manifest.unlink()
        missing_manifest = self.finish()
        self.assertNotEqual(missing_manifest.returncode, 0)
        self.assertIn("already-sealed-mismatch", missing_manifest.stderr)

    def test_closed_and_sealed_replay_refuse_changed_prior_step(self):
        self.assertNotEqual(self.finish("after-close").returncode, 0)
        outcome = self.route_file.with_name(self.route_file.stem + ".outcome.json")
        row = json.loads(outcome.read_text(encoding="utf-8"))
        row["summary_digest"] = "0" * 64
        outcome.write_text(json.dumps(row), encoding="utf-8")
        closed_drift = self.finish()
        self.assertNotEqual(closed_drift.returncode, 0)
        self.assertIn("finish-outcome-drift", closed_drift.stderr)
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "open")

    def test_finished_replay_refuses_missing_exact_index(self):
        first = self.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        index = self.root / ".runtime/artifact-admission/v1/index.json"
        self.assertTrue(index.is_file())
        index.unlink()
        replay = self.finish()
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("already-sealed-mismatch", replay.stderr)

    def test_registered_route_is_refused_before_intent(self):
        metadata = "route_id=" + self.route["route_id"]
        self.jobs.write_text("field\tfield\tfield\tfield\tfield\t" + metadata + "\n", encoding="utf-8")
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-registered-route-ineligible", refused.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_registered_caller_is_refused_before_intent(self):
        env = self.env.copy()
        env["AGENT_DISPATCH_ATTEMPT_ID"] = "att-" + "a"*48
        refused = subprocess.run(self.command, cwd=self.repo, env=env,
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-registered-caller-ineligible", refused.stderr)

    def test_closed_route_still_refuses_before_finish_intent(self):
        outcome = self.route_file.with_name(self.route_file.stem + ".outcome.json")
        outcome.write_text("{}\n", encoding="utf-8")
        closed = self.finish()
        self.assertNotEqual(closed.returncode, 0)
        self.assertIn("finish-route-already-closed", closed.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_synthetic_scope_finishes_with_unrelated_tracked_state_and_replays_without_git_changes(self):
        def git(*args, check=True):
            return subprocess.run(["git", "-C", str(self.repo), *args], check=check,
                                  capture_output=True)

        branch = git("branch", "--show-current").stdout.decode().strip()
        for name in ("staged file.txt", "unstaged file.txt", "rename source.txt"):
            (self.repo / name).write_bytes((name + " base\n").encode())
        git("add", "staged file.txt", "unstaged file.txt", "rename source.txt")
        git("commit", "-qm", "add tracked finish fixtures")

        git("checkout", "-qb", "finish-side")
        (self.repo / "side-only.txt").write_bytes(b"side\n")
        git("add", "side-only.txt")
        git("commit", "-qm", "side change")
        git("checkout", "-q", branch)
        (self.repo / "ours-only.txt").write_bytes(b"ours\n")
        git("add", "ours-only.txt")
        git("commit", "-qm", "ours change")
        merged = git("merge", "--no-commit", "--no-ff", "finish-side", check=False)
        self.assertEqual(merged.returncode, 0, merged.stderr.decode(errors="replace"))
        self.assertTrue((self.repo / ".git/MERGE_HEAD").is_file())

        (self.repo / "staged file.txt").write_bytes(b"staged edit\n")
        git("add", "staged file.txt")
        (self.repo / "unstaged file.txt").write_bytes(b"unstaged edit\n")
        git("mv", "rename source.txt", "renamed target.txt")

        def git_snapshot():
            names = ("README.md", "staged file.txt", "unstaged file.txt",
                     "renamed target.txt", "side-only.txt")
            operation = {name: (self.repo / ".git" / name).read_bytes()
                         for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE")
                         if (self.repo / ".git" / name).is_file()}
            return {
                "files": {name: (self.repo / name).read_bytes() for name in names if (self.repo / name).is_file()},
                "entries": git("ls-files", "--stage", "-z").stdout,
                "tree": git("write-tree").stdout,
                "status": git("status", "--porcelain=v1", "-z", "--untracked-files=no").stdout,
                "operation": operation,
            }

        before = git_snapshot()
        first = self.finish()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        receipt = json.loads(first.stdout)
        observed = set(receipt["unscoped_tracked_dirt"])
        self.assertTrue({"staged file.txt", "unstaged file.txt",
                         "rename source.txt", "renamed target.txt", "side-only.txt"} <= observed)
        self.assertEqual(git_snapshot(), before)
        stored_before = json.loads(
            (self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").read_text())
        replay = self.finish()
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
        replay_receipt = json.loads(replay.stdout)
        self.assertTrue(replay_receipt["replay"])
        self.assertEqual(replay_receipt["unscoped_tracked_dirt"], receipt["unscoped_tracked_dirt"])
        self.assertEqual(git_snapshot(), before)
        stored_after = json.loads(
            (self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").read_text())
        self.assertEqual(stored_after, stored_before)

    def test_mixed_synthetic_and_concrete_file_directory_or_glob_scope_still_refuses(self):
        (self.repo / "README.md").write_text("dirty source\n", encoding="utf-8")
        nested = self.repo / "pkg" / "nested dir" / "tracked file.py"
        nested.parent.mkdir(parents=True)
        nested.write_text("base\n", encoding="utf-8")
        test_file = self.repo / "tests" / "unit" / "tracked file.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "pkg", "tests"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "add nested scope fixtures"], check=True)
        nested.write_text("dirty nested source\n", encoding="utf-8")
        test_file.write_text("dirty test source\n", encoding="utf-8")
        args = SimpleNamespace(evidence=str(self.evidence), summary_file=str(self.summary), commit=None)
        state_path = self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json"
        for concrete in ("README.md", "README.md/**", "READ*", "pkg/**", "p*/**", "tests/**"):
            with self.subTest(scope=concrete):
                scope = ["source-scoped", concrete]
                with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-scoped-tracked-dirt"):
                    inline_finish._unscoped_tracked_dirt(str(self.repo), scope)
                self.assertFalse(state_path.exists())

    def test_synthetic_scope_vocabulary_does_not_match_tracked_paths(self):
        (self.repo / "source").mkdir()
        (self.repo / "source/source.py").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "source/source.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "add source fixture"], check=True)
        (self.repo / "source/source.py").write_text("dirty\n", encoding="utf-8")
        for token in ("source-scoped", "source/**", "source-alternative/**", "target-artifact"):
            with self.subTest(token=token):
                self.assertEqual(inline_finish._unscoped_tracked_dirt(str(self.repo), [token]),
                                 ["source/source.py"])

    def test_porcelain_status_keeps_both_rename_paths_and_spaces_for_scope_checks(self):
        def git(*args, check=True):
            return subprocess.run(["git", "-C", str(self.repo), *args], check=check,
                                  capture_output=True)

        (self.repo / "old name.txt").write_bytes(b"rename bytes\n")
        git("add", "old name.txt")
        git("commit", "-qm", "add rename fixture")
        git("mv", "old name.txt", "new name.txt")
        observed = set(inline_finish._unscoped_tracked_dirt(str(self.repo), []))
        self.assertEqual(observed, {"old name.txt", "new name.txt"})
        for scope in ("old name.txt", "new name.txt"):
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-scoped-tracked-dirt"):
                    inline_finish._unscoped_tracked_dirt(str(self.repo), ["source-scoped", scope])

    def test_read_only_status_disables_optional_index_refresh(self):
        result = subprocess.CompletedProcess([], 0, stdout=b"")
        with unittest.mock.patch.object(inline_finish.subprocess, "run", return_value=result) as run:
            self.assertEqual(inline_finish._unscoped_tracked_dirt(str(self.repo), []), [])
        self.assertEqual(run.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_fresh_scope_judgment_rejects_concrete_dirt_added_after_the_first_scan(self):
        scope = ["source-scoped", "README.md"]
        self.assertEqual(inline_finish._unscoped_tracked_dirt(str(self.repo), scope), [])
        (self.repo / "README.md").write_text("raced scoped dirt\n", encoding="utf-8")
        with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-scoped-tracked-dirt"):
            inline_finish._unscoped_tracked_dirt(str(self.repo), scope)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" /
                          self.route["route_id"] / "finish.json").exists())

    def test_locked_revalidation_rejects_recursive_glob_dirt_without_mutating_sealed_route(self):
        nested = self.repo / "pkg" / "locked dir" / "tracked file.py"
        nested.parent.mkdir(parents=True)
        nested.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "pkg"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "add locked glob fixture"], check=True)
        route_bytes = self.route_file.read_bytes()
        original = inline_finish._unscoped_tracked_dirt
        calls = 0

        def introduce_scoped_dirt(cwd, scope):
            nonlocal calls
            calls += 1
            # Exercise the same shared judgement with a concrete recursive glob
            # while leaving the compiler-sealed route and its hash untouched.
            concrete_scope = [*scope, "p*/**"]
            if calls == 2:
                nested.write_text("raced scoped dirt\n", encoding="utf-8")
            return original(cwd, concrete_scope)

        args = SimpleNamespace(evidence=str(self.evidence), summary_file=str(self.summary), commit=None)
        with unittest.mock.patch.object(inline_finish, "_unscoped_tracked_dirt",
                                        side_effect=introduce_scoped_dirt):
            with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-scoped-tracked-dirt"):
                inline_finish.finish(args, self.route, self.route_file, CAP)
        self.assertEqual(calls, 2)
        self.assertEqual(self.route_file.read_bytes(), route_bytes)
        self.assertEqual(CAP.route_hash(json.loads(route_bytes)), self.route["route_hash"])
        self.assertFalse((self.root / ".runtime/inline-finish/v1" /
                          self.route["route_id"] / "finish.json").exists())

    def test_locked_revalidation_reports_unrelated_dirt_that_arrives_after_first_scan(self):
        args = SimpleNamespace(evidence=str(self.evidence), summary_file=str(self.summary), commit=None)
        original = inline_finish._unscoped_tracked_dirt
        calls = 0

        def introduce_unrelated_dirt(cwd, scope):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.repo / "README.md").write_text("raced unrelated dirt\n", encoding="utf-8")
            return original(cwd, scope)

        with unittest.mock.patch.object(inline_finish, "_unscoped_tracked_dirt",
                                        side_effect=introduce_unrelated_dirt):
            result = inline_finish.finish(args, self.route, self.route_file, CAP)
        self.assertEqual(calls, 2)
        self.assertEqual(result["unscoped_tracked_dirt"], ["README.md"])
        self.assertTrue(result["inline_finish_id"])

    def test_changed_result_commit_conflicts_with_existing_claim(self):
        self.assertNotEqual(self.finish("after-claim").returncode, 0)
        (self.repo / "README.md").write_text("later source result\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "later source result"], check=True)
        changed = self.finish()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("finish-intent-conflict", changed.stderr)

    def test_complete_and_close_are_fenced_while_finish_is_pending(self):
        self.assertNotEqual(self.finish("after-claim").returncode, 0)
        complete = subprocess.run([
            sys.executable, str(ROOT / "utilities/capability-route.py"), "complete",
            "--route", str(self.route_file), "--node", "inline", "--evidence", str(self.evidence),
        ], cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(complete.returncode, 0)
        self.assertIn("finish-in-progress", complete.stderr)
        close = subprocess.run([
            sys.executable, str(ROOT / "utilities/capability-route.py"), "close", "--route", str(self.route_file),
        ], cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(close.returncode, 0)
        self.assertIn("finish-in-progress", close.stderr)

    def test_public_foreign_finalize_cannot_seal_pending_finish(self):
        self.assertNotEqual(self.finish("after-close").returncode, 0)
        command = [sys.executable, str(ROOT / "utilities/artifact_producer.py"),
                   "finalize", "--artifact-root", str(self.root), "--cycle", self.cycle["cycle_id"]]
        foreign = subprocess.run(command, cwd=self.repo, env=self.env,
                                 capture_output=True, text=True)
        self.assertNotEqual(foreign.returncode, 0)
        self.assertIn("finish-in-progress", foreign.stdout + foreign.stderr, repr(foreign))
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "open")
        self.assertFalse((self.cycle_dir / "manifest.json").exists())

    def test_foreign_cycle_record_refuses_before_finish_intent(self):
        record_path = artifact_producer.cycle_record_path(self.root, self.cycle["cycle_id"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["route_id"] = "rt-" + "f" * 16
        record_path.write_text(json.dumps(record), encoding="utf-8")
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-route-cycle-missing", refused.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_non_direct_route_and_foreign_owner_context_refuse_before_slot(self):
        import inline_finish
        from types import SimpleNamespace

        args = SimpleNamespace(evidence=str(self.evidence), summary_file=str(self.summary), commit=None)
        for intensity in ("quick", "standard"):
            with self.subTest(intensity=intensity):
                candidate = dict(self.route, effective_intensity=intensity)
                with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-route-not-direct"):
                    inline_finish.finish(args, candidate, self.route_file, CAP)
        env = self.env.copy()
        env["AGENT_DISPATCH_ATTEMPT_ID"] = "att-" + "f" * 48
        env["AGENT_ROUTE_ID"] = "rt-" + "e" * 16
        foreign = subprocess.run(self.command, cwd=self.repo, env=env,
                                 capture_output=True, text=True)
        self.assertNotEqual(foreign.returncode, 0)
        self.assertIn("finish-registered-caller-ineligible", foreign.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_explicit_commit_not_source_descendant_is_refused_before_intent(self):
        original_branch = subprocess.run(
            ["git", "-C", str(self.repo), "symbolic-ref", "--short", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        (self.repo / "README.md").write_text("orphan branch\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "--orphan", "unrelated"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "unrelated history"], check=True)
        unrelated = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                   check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", original_branch], check=True)
        self.command.extend(["--commit", unrelated])
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-commit-not-source-descendant", refused.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def _descendant_commit(self, branch=None):
        tree = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD^{tree}"],
                              check=True, capture_output=True, text=True).stdout.strip()
        commit = subprocess.run(["git", "-C", str(self.repo), "commit-tree", tree, "-p", "HEAD",
                                 "-m", "merged elsewhere"],
                                check=True, capture_output=True, text=True).stdout.strip()
        if branch:
            subprocess.run(["git", "-C", str(self.repo), "update-ref", branch, commit], check=True)
        return commit

    def test_a_result_merged_on_another_branch_finishes_without_moving_head(self):
        # A linked-worktree PR lands on the remote main while the shared checkout's HEAD stays put.
        commit = self._descendant_commit("refs/remotes/origin/main")
        self.command.extend(["--commit", commit])
        finished = self.finish()
        self.assertEqual(finished.returncode, 0, finished.stderr)

    def test_a_descendant_on_no_branch_is_refused(self):
        self.command.extend(["--commit", self._descendant_commit()])
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-commit-not-source-descendant", refused.stderr)

    def test_active_review_lease_is_refused_before_intent(self):
        lease_path = artifact_producer._review_lease_path(self.root, self.cycle["cycle_id"], "att-review")
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text("{}\n", encoding="utf-8")
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-active-review-lease", refused.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())
        lease_path.unlink()
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    def test_symlink_evidence_is_refused_before_intent(self):
        outside = self.base / "outside.md"
        outside.write_text("outside evidence\n", encoding="utf-8")
        original = self.cycle_dir / "artifacts/documents/evidence-original.md"
        self.evidence.rename(original)
        self.evidence.symlink_to(outside)
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())


class SpecInlinePublicationTest(unittest.TestCase):
    def _fixture(self):
        fixture = PublicInlineFinishTest()
        fixture.CAPABILITY, fixture.CAPABILITY_MODE = "autopilot-spec", "update"
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        prd = fixture.cycle_dir / "artifacts/spec/prd.md"
        prd.parent.mkdir(parents=True, exist_ok=True)
        prd.write_bytes(b"# Official user PRD\n")
        return fixture, prd

    def test_inline_finish_publishes_after_seal_and_replays_without_changing_finish_receipt(self):
        fixture, prd = self._fixture()
        source = prd.read_bytes()
        for fault in ("after-finalize", "after-admission"):
            failed = fixture.finish(fault)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("fault-injected-" + fault, failed.stderr)
        first = fixture.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        receipt = json.loads(first.stdout)
        self.assertEqual(receipt["shared_publication"]["status"], "admitted", receipt)
        state_path = fixture.root / ".runtime/inline-finish/v1" / fixture.route["route_id"] / "finish.json"
        state = state_path.read_bytes()
        outcome = CAP.outcome_path(fixture.route_file).read_bytes()
        manifest = (fixture.cycle_dir / "manifest.json").read_bytes()
        again = fixture.finish()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout)["shared_publication"]["shared_reference_revision_id"],
                         receipt["shared_publication"]["shared_reference_revision_id"])
        self.assertEqual(artifact_producer.list_references(fixture.root, "spec")[0]["revisions"],
                         [receipt["shared_publication"]["shared_reference_revision_id"]])
        self.assertEqual(state_path.read_bytes(), state)
        self.assertEqual(CAP.outcome_path(fixture.route_file).read_bytes(), outcome)
        self.assertEqual((fixture.cycle_dir / "manifest.json").read_bytes(), manifest)
        self.assertEqual(prd.read_bytes(), source)
        document = json.loads(manifest)
        primary = next(a["artifact_id"] for a in document["artifacts"] if a["role"] == "primary")
        self.assertEqual(next(r["locator"]["path"] for r in document["artifact_revisions"]
                              if r["artifact_id"] == primary), "artifacts/spec/prd.md")

    def test_inline_missing_prd_reports_publication_pending_without_reopening_finished_work(self):
        fixture, prd = self._fixture()
        prd.unlink()
        done = fixture.finish()
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout)
        self.assertEqual(receipt["shared_publication"]["reason"], "official-spec-prd-missing")
        self.assertFalse(prd.exists())
        self.assertEqual(artifact_producer.list_references(fixture.root, "spec"), [])
        outcome = json.loads(CAP.outcome_path(fixture.route_file).read_text())
        self.assertTrue(outcome["terminal_gate_proven"])


class NextLegFinishTest(PublicInlineFinishTest):
    """A direct route that is leg 0 of an approved route plan reports `next_leg` on finish and replay."""

    PLAN_INDEX = 0
    NEXT_LEG = {"shape": "staged", "graph": ["execute", "test"]}

    def extra_compose_arguments(self):
        import route_plan
        self.task_file = self.base / "leg-task.md"
        self.task_file.write_text("the reusable work request\n", encoding="utf-8")
        leg = {"capability": "autopilot-code", "mode": "dev", "shape": "direct", "graph": None, "intensity": None,
               "why": "x"}
        second = {**leg, **self.NEXT_LEG}
        frame = {"route_id": "rt-" + "1" * 16, "route_hash": "sha256:" + "2" * 64, "cycle_id": "cyc_" + "3" * 32}
        decision = route_plan.build_decision(
            frame_route=frame, selected="Go", reason="", briefs=[], intent={"path": "x", "sha256": "0" * 64},
            proposal={"summary": "two", "legs": [leg, second], "entry_approvals": []},
            first_leg_compose={"leg": 0, "context": {
                "cwd": str(self.repo), "artifact_root": str(self.root), "slug": "inline-finish-test",
                "campaign_key": "inline-finish-test", "parent_cycle": frame["cycle_id"],
                "prompt_file": str(self.task_file), "prompt_sha256": "0" * 64, "spec_read": "fixture", "owner": None}})
        self.record_path = self.root / "decisions" / "route-decision.json"
        self.record_path.parent.mkdir(parents=True)
        self.record_path.write_bytes(route_plan.render(route_plan.build_record(decision)))
        return ["--route-plan", f"{self.record_path}#{self.PLAN_INDEX}"]

    def add_origin(self, *, ahead=False):
        """The next leg changes source, so its start prepares a worktree from origin's default branch.
        `ahead` moves that branch one commit past this checkout; returns origin's tip."""
        origin = self.base / "origin.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.repo), str(origin)], check=True)
        remotes = subprocess.run(["git", "-C", str(self.repo), "remote"], check=True,
                                 capture_output=True, text=True).stdout.split()
        verb = "set-url" if "origin" in remotes else "add"
        subprocess.run(["git", "-C", str(self.repo), "remote", verb, "origin", str(origin)], check=True)
        if ahead:
            writer = self.base / "writer"
            subprocess.run(["git", "clone", "-q", str(origin), str(writer)], check=True)
            subprocess.run(["git", "-C", str(writer), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                            "commit", "--allow-empty", "-qm", "remote tip"], check=True)
            subprocess.run(["git", "-C", str(writer), "push", "-q", "origin", "HEAD"], check=True)
        return subprocess.run(["git", "--git-dir", str(origin), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    def start_finished(self):
        started = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "start",
                                  "--route", str(self.route_file), "--jobs", str(self.jobs)],
                                 cwd=self.repo, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(started.returncode, 0, started.stderr)
        return json.loads(started.stdout)

    def checkout_state(self):
        git = lambda *args: subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                                           capture_output=True, text=True).stdout
        return git("rev-parse", "HEAD"), git("branch", "--show-current"), git("status", "--porcelain")

    def preparations(self):
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in (self.root / ".runtime/worktree-preparation").glob("*.json")]

    def test_the_first_finish_and_every_replay_carry_the_same_next_leg(self):
        first = json.loads(self.finish().stdout)
        self.assertEqual(self.route["route_plan"]["index"], 0)
        next_leg = first["next_leg"]
        self.assertEqual((next_leg["index"], next_leg["leg"]["shape"]), (1, "staged"))
        import shlex
        argv = shlex.split(next_leg["compose_command"])
        self.assertEqual(argv[argv.index("--route-plan") + 1], f"{self.record_path}#1")
        self.assertEqual(argv[argv.index("--parent-cycle") + 1], self.cycle["cycle_id"])
        self.assertEqual(argv[argv.index("--campaign-key") + 1], "inline-finish-test")
        self.assertEqual(argv[argv.index("--graph") + 1], "execute,test")
        self.assertIn("--start", argv)
        replay = json.loads(self.finish().stdout)
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["next_leg"], next_leg)
        self.assertEqual({k: v for k, v in replay.items() if k not in ("replay",)},
                         {k: v for k, v in first.items() if k not in ("replay",)})

    def test_the_stored_receipt_and_its_identity_are_untouched(self):
        self.finish()
        stored = artifact_producer and json.loads(
            (self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").read_text())["receipt"]
        self.assertEqual(set(stored), {"schema", "inline_finish_id", "route_id", "route_hash", "cycle_id",
                                       "terminal_marker_digest", "manifest_digest", "commit", "replay"})

    def test_the_start_receipt_of_the_finished_route_and_route_status_carry_the_same_next_leg(self):
        next_leg = json.loads(self.finish().stdout)["next_leg"]
        self.add_origin()
        before = self.checkout_state()
        receipt = self.start_finished()
        # The start of a finished leg starts the plan's next leg (plan cursor). In a HOME with no
        # supported harness the leg cannot be prepared: the finished leg's receipt then says why,
        # beside the same next_leg. Either way the plan's next leg was taken up, never dropped.
        (prepared,) = self.preparations()
        allocation = Path(prepared["path"]).resolve()
        self.assertEqual((prepared["state"], allocation.parent), ("ready", (self.base / "repo-wt").resolve()))
        if "plan_advanced" in receipt:
            self.assertEqual(receipt["plan_advanced"]["leg"], next_leg["index"], receipt)
            started_leg = json.loads(Path(receipt["plan_advanced"]["route_file"]).read_text(encoding="utf-8"))
            self.assertEqual(started_leg["route_plan"]["index"], next_leg["index"])
            self.assertEqual(started_leg["cwd"], str(allocation))
        else:
            self.assertEqual((receipt["state"], receipt["next_leg"]), ("completed", next_leg), receipt)
            self.assertEqual((receipt["plan_advance"]["state"], receipt["plan_advance"]["leg"]),
                             ("not-started", next_leg["index"]), receipt)
        self.assertNotIn("next_leg", receipt.get("required_action") or "")
        self.assertEqual(self.checkout_state(), before)
        status = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "status",
                                 "--artifact-root", str(self.root)], cwd=self.repo, env=os.environ.copy(),
                                capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        rows = [row for row in json.loads(status.stdout) if row["route_id"] == self.route["route_id"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["next_leg"], next_leg)
        self.assertTrue(rows[0]["closed"])

    def test_a_next_leg_without_its_worktree_keeps_the_plan_and_the_same_start_retries(self):
        next_leg = json.loads(self.finish().stdout)["next_leg"]
        published = lambda: sorted(p.name for p in (self.root / ".runtime/routes").glob("rt-*.json"))
        # A repository with no remote starts from its own branch; an origin that
        # cannot be reached is the retryable failure this test exercises.
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        str(self.base / "unreachable.git")], check=True)
        routes, before = published(), self.checkout_state()
        failed = self.start_finished()           # this checkout's origin cannot be fetched
        self.assertEqual((failed["state"], failed["reason"]),
                         ("needs-attention", "source-worktree-preparation-failed"), failed)
        self.assertEqual((failed["plan_advance"]["state"], failed["plan_advance"]["leg"]),
                         ("retryable", next_leg["index"]), failed)
        self.assertIn("remote-default-fetch-failed", failed["plan_advance"]["reason"])
        self.assertEqual(failed["next_leg"], next_leg)
        self.assertNotIn("required_action", failed)
        self.assertEqual((published(), self.preparations(), self.checkout_state()), (routes, [], before))
        self.add_origin()
        retried = self.start_finished()
        self.assertTrue("plan_advanced" in retried or retried["plan_advance"]["state"] == "not-started", retried)
        (prepared,) = self.preparations()
        self.assertEqual(Path(prepared["path"]).resolve().parent, (self.base / "repo-wt").resolve())

    def test_a_route_that_has_not_finished_has_no_next_leg_anywhere(self):
        status = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "status",
                                 "--artifact-root", str(self.root)], cwd=self.repo, env=os.environ.copy(),
                                capture_output=True, text=True)
        rows = [row for row in json.loads(status.stdout) if row["route_id"] == self.route["route_id"]]
        self.assertFalse(rows[0]["closed"])
        self.assertNotIn("next_leg", rows[0])

    def test_a_vanished_record_leaves_the_receipt_as_it_was_without_the_key(self):
        self.finish()
        self.record_path.unlink()
        replay = json.loads(self.finish().stdout)
        self.assertNotIn("next_leg", replay)
        self.assertTrue(replay["replay"])


class DirectNextLegFinishTest(NextLegFinishTest):
    """A direct next leg needs no harness probe, so its start runs in any HOME."""

    NEXT_LEG = {"shape": "direct", "graph": None}

    def test_the_successor_of_a_leg_in_the_primary_is_sealed_in_its_own_worktree(self):
        self.finish()
        self.assertEqual(Path(self.route["cwd"]).resolve(), self.repo.resolve())   # this leg ran in the primary
        tip = self.add_origin(ahead=True)
        before = self.checkout_state()
        receipt = self.start_finished()
        self.assertEqual((receipt["state"], receipt["plan_advanced"]["leg"]), ("inline", 1), receipt)
        started = json.loads(Path(receipt["plan_advanced"]["route_file"]).read_text(encoding="utf-8"))
        (prepared,) = self.preparations()
        self.assertEqual(started["cwd"], str(Path(prepared["path"]).resolve()))
        self.assertEqual(Path(started["cwd"]).parent, (self.base / "repo-wt").resolve())
        head = subprocess.run(["git", "-C", started["cwd"], "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual((head, self.checkout_state()), (tip, before))
        self.assertNotEqual(tip, before[0].strip())                                  # origin's tip, not local HEAD


class LastLegFinishTest(NextLegFinishTest):
    PLAN_INDEX = 1

    def test_the_first_finish_and_every_replay_carry_the_same_next_leg(self):
        first = json.loads(self.finish().stdout)
        self.assertNotIn("next_leg", first)
        self.assertNotIn("next_leg", json.loads(self.finish().stdout))
        started = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "start",
                                  "--route", str(self.route_file), "--jobs", str(self.jobs)],
                                 cwd=self.repo, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(json.loads(started.stdout)["state"], "completed")
        self.assertNotIn("next_leg", started.stdout)
        status = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "status",
                                 "--artifact-root", str(self.root)], cwd=self.repo, env=os.environ.copy(),
                                capture_output=True, text=True)
        self.assertNotIn("next_leg", status.stdout)


class LegacyInlineFinishTest(PublicInlineFinishTest):
    """A single-inline-node route with a non-direct intensity (e.g. resplit-seal)
    finishes through the one `finish` command via the legacy path."""

    def _legacy_args(self):
        from types import SimpleNamespace
        return SimpleNamespace(evidence=str(self.evidence), summary_file=str(self.summary), commit=None)

    def test_legacy_shaped_route_finishes_through_the_legacy_path(self):
        import unittest.mock
        route = dict(self.route, effective_intensity="standard")
        with unittest.mock.patch.dict(os.environ, self.env):
            receipt = CAP._legacy_inline_finish(self._legacy_args(), route, str(self.route_file), CAP,
                                                entry_error=inline_finish.InlineFinishError("finish-route-not-direct"))
        self.assertEqual(receipt.get("state"), "legacy-finished")
        self.assertTrue(receipt.get("legacy"))
        self.assertTrue(CAP.outcome_path(self.route_file).exists())

    def test_second_finish_reports_already_closed(self):
        import unittest.mock
        route = dict(self.route, effective_intensity="standard")
        with unittest.mock.patch.dict(os.environ, self.env):
            CAP._legacy_inline_finish(self._legacy_args(), route, str(self.route_file), CAP,
                                      entry_error=inline_finish.InlineFinishError("finish-route-not-direct"))
            with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-route-already-closed"):
                CAP._legacy_inline_finish(self._legacy_args(), route, str(self.route_file), CAP,
                                          entry_error=inline_finish.InlineFinishError("finish-route-not-direct"))

    def test_multi_node_shape_reraises_the_entry_refusal(self):
        import unittest.mock
        route = dict(self.route, effective_intensity="standard",
                     nodes=[dict(self.route["nodes"][0]), dict(self.route["nodes"][0], id="extra")])
        with unittest.mock.patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-route-not-direct"):
                CAP._legacy_inline_finish(self._legacy_args(), route, str(self.route_file), CAP,
                                          entry_error=inline_finish.InlineFinishError("finish-route-not-direct"))


# The plan subclasses only add their own tests: do not run the inherited ones a second time.
for _klass in (NextLegFinishTest, LastLegFinishTest, LegacyInlineFinishTest):
    for _name in dir(PublicInlineFinishTest):
        if _name.startswith("test_") and _name not in _klass.__dict__:
            setattr(_klass, _name, None)
for _name in dir(DirectNextLegFinishTest):
    if _name.startswith("test_") and _name not in DirectNextLegFinishTest.__dict__:
        setattr(DirectNextLegFinishTest, _name, None)
LastLegFinishTest.test_the_stored_receipt_and_its_identity_are_untouched = None
LastLegFinishTest.test_a_vanished_record_leaves_the_receipt_as_it_was_without_the_key = None
LastLegFinishTest.test_the_start_receipt_of_the_finished_route_and_route_status_carry_the_same_next_leg = None
LastLegFinishTest.test_a_next_leg_without_its_worktree_keeps_the_plan_and_the_same_start_retries = None
LastLegFinishTest.test_a_route_that_has_not_finished_has_no_next_leg_anywhere = None


if __name__ == "__main__":
    unittest.main()
