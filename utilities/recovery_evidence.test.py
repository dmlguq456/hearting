#!/usr/bin/env python3
"""Temporary-state recovery observations through registry and launch consumers."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch_contract as D
from codex_dispatch_terminal import inspect_terminal_attempt
from recovery_evidence import observe_attempt
from split_module_fixture import patch_split

spec = importlib.util.spec_from_file_location("recovery_registry", HERE / "dispatch-registry.py")
REGISTRY = importlib.util.module_from_spec(spec)
spec.loader.exec_module(REGISTRY)


class RecoveryEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.artifacts = self.base / ".agent_reports"
        self.artifacts.mkdir()
        self.log = self.base / "attempt.jsonl"
        namespace = D.process_namespace_identity()
        self.metadata = {
            "attempt_schema_version": "2", "attempt_id": "att-recovery-evidence",
            "route_id": "rt-recovery", "route_node": "execute", "dispatch_depth": "2",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "worker_type": "stage", "harness": "codex",
            "pid": "99999999", "pid_start": "1", "pgid": "99999999",
            "pid_scope": "namespace-local", "pid_ns": namespace,
            "pid_observer_ns": namespace, "launch_claimed": "1", "launch_started": "1",
            "launch_lifecycle": "foreground-scoped", "artifact_root": str(self.artifacts),
            "log_file": str(self.log),
        }
        # The host's unrelated protected processes can make a real /proc walk
        # incomplete. Supply the existing observer's exact empty scan while
        # retaining real PID/start/group observation and real registry writes.
        self.scan = patch_split(D, "attempt_tagged_descendants", return_value=D.ProcessGroupObservation("empty", ()))
        self.scan.start()
        self.addCleanup(self.scan.stop)
        self.addCleanup(self.temp.cleanup)

    def write_row(self, status="open", **metadata):
        self.metadata.update(metadata)
        encoded = ",".join(f"{key}={value}" for key, value in self.metadata.items())
        self.jobs.write_text(f"2026-10-09T00:00:00Z\t{status}\t{self.base}\t{self.base}\texecute\t{encoded}\n")

    def args(self, *, apply=False, only_exact_dead=False):
        return types.SimpleNamespace(
            attempt=self.metadata["attempt_id"], session=None, route=None, node=None,
            job=None, all=False, jobs=self.jobs, agent_home=self.base, apply=apply,
            only_exact_dead=only_exact_dead, audit=None, now=time.time(),
            integration_ref=None, cascade_grace=0, cascade_kill_wait=0,
        )

    def reconcile(self, *, apply=False, only_exact_dead=False):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), mock.patch.object(REGISTRY, "ensure_attempt_owner", return_value={}), \
                mock.patch.object(REGISTRY, "materialize_after_terminal_close"):
            REGISTRY.reconcile(REGISTRY.read_rows(self.jobs), self.args(apply=apply, only_exact_dead=only_exact_dead))
        return json.loads(stream.getvalue())

    def terminal(self, harness="codex", verdict="FAIL"):
        message = f"artifact: -\nverdict: {verdict}\nblocker: {'none' if verdict == 'PASS' else 'reported failure'}"
        if harness == "codex":
            rows = [{"type": "item.completed", "item": {"type": "agent_message", "text": message}}, {"type": "turn.completed"}]
        elif harness == "claude":
            rows = [{"type": "result", "subtype": "success", "is_error": False, "result": message}]
        else:
            rows = [{"type": "text", "sessionID": "ses_recovery", "part": {"type": "text", "text": message}},
                    {"type": "step_finish", "sessionID": "ses_recovery", "part": {"type": "step-finish", "reason": "stop"}}]
        self.log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.metadata.update(
            launch_outcome="governed-process-reaped", group_reap_proof=D.GROUP_REAP_PROOF,
            group_reap_pgid=self.metadata["pgid"],
            attempt_descendant_proof=D.ATTEMPT_DESCENDANT_PROOF,
            attempt_descendant_observer_ns=D.process_namespace_identity(),
        )

    def test_unknown_launch_missing_empty_and_incomplete_log_share_exact_death(self):
        for content in (None, "", '{"type":"turn.started"}\n', '{"type":"unfinished'):
            with self.subTest(content=content):
                if content is None:
                    self.log.unlink(missing_ok=True)
                else:
                    self.log.write_text(content)
                self.write_row()
                before = self.jobs.read_bytes()
                observation = observe_attempt("open", self.metadata, worktree=self.base)
                self.assertEqual((observation.process.state, observation.result_state, observation.decision.action),
                                 ("quiescent", "none", "reconcile"))
                dry = self.reconcile(only_exact_dead=True)
                self.assertEqual(dry["decisions"][0]["category"], "exact-dead")
                self.assertEqual(self.jobs.read_bytes(), before)
                applied = self.reconcile(apply=True, only_exact_dead=True)
                self.assertEqual(applied["closed"], 1, applied)
                self.assertIn("note=dead-exact-pid", self.jobs.read_text())
                fresh = D.parse_registry_metadata(self.jobs.read_text().strip().split("\t")[5])
                settled = observe_attempt("done", fresh, worktree=self.base)
                self.assertEqual((settled.process.state, settled.decision.action), ("quiescent", "inspect-failure"))
                self.assertTrue(settled.decision.retry_allowed)
                D._sibling_attempt_gate({"route_id": self.metadata["route_id"]}, "execute", self.jobs, attempt_id="att-next")
                self.assertEqual(self.reconcile(apply=True, only_exact_dead=True)["closed"], 0)

    def test_unreadable_log_is_unknown_and_never_committed_as_death(self):
        self.write_row()
        with mock.patch("codex_dispatch_terminal._tail_lines", side_effect=PermissionError):
            view = observe_attempt("open", self.metadata, worktree=self.base)
            self.assertEqual((view.process.state, view.result_state, view.decision.action),
                             ("quiescent", "unverifiable", "recover"))
            before = self.jobs.read_bytes()
            result = self.reconcile(apply=True, only_exact_dead=True)
        self.assertEqual(result["closed"], 0)
        self.assertEqual(result["decisions"][0]["category"], "unverifiable")
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_resume_and_reconcile_share_missing_unreadable_and_live_answers(self):
        self.write_row()
        route = {"route_id": self.metadata["route_id"]}
        self.assertEqual(D.existing_attempt_launch_state(self.jobs, self.metadata["attempt_id"])[0], "existing-dead")
        D._sibling_attempt_gate(route, "execute", self.jobs, attempt_id="att-next")
        self.assertEqual(self.reconcile()["decisions"][0]["category"], "exact-dead")
        with mock.patch("codex_dispatch_terminal._tail_lines", side_effect=PermissionError):
            self.assertEqual(D.existing_attempt_launch_state(self.jobs, self.metadata["attempt_id"])[0], "existing-unverified")
            with self.assertRaises(D.DispatchContractError) as caught:
                D._sibling_attempt_gate(route, "execute", self.jobs, attempt_id="att-next")
            self.assertEqual(caught.exception.reason, "prior-attempt-unverifiable")
            self.assertEqual(self.reconcile()["decisions"][0]["category"], "unverifiable")
        self.write_row(pid=str(os.getpid()), pid_start=D.process_start_ticks(os.getpid()), pgid=str(os.getpid()))
        self.assertEqual(D.existing_attempt_launch_state(self.jobs, self.metadata["attempt_id"])[0], "existing-active")
        with self.assertRaises(D.DispatchContractError) as caught:
            D._sibling_attempt_gate(route, "execute", self.jobs, attempt_id="att-next")
        self.assertEqual(caught.exception.reason, "prior-attempt-still-live")
        self.assertEqual(self.reconcile()["decisions"][0]["category"], "active")

    def test_read_only_status_projects_the_same_process_and_result_axes(self):
        self.write_row()
        before = self.jobs.read_bytes()
        status = REGISTRY.observed_status_rows(REGISTRY.read_rows(self.jobs), self.args())["rows"][0]
        self.assertEqual((status["process"], status["result"], status["action"]), ("quiescent", "none", "reconcile"))
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_actual_results_use_the_terminal_writer_in_all_three_harnesses(self):
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                self.terminal(harness)
                self.write_row(harness=harness)
                view = observe_attempt("open", self.metadata, worktree=self.base)
                self.assertEqual((view.result_state, view.decision.reason), ("settleable", "terminal-observed"))
                guarded = self.reconcile(apply=True, only_exact_dead=True)
                self.assertEqual(guarded["closed"], 0)
                self.assertEqual(guarded["decisions"][0]["proposed_note"], "dead-worker-fail")
                settled = self.reconcile(apply=True)
                self.assertEqual(settled["closed"], 1, settled)
                metadata = D.parse_registry_metadata(self.jobs.read_text().strip().split("\t")[5])
                self.assertEqual((metadata["note"], metadata["failure_class"]), ("dead-worker-fail", "fail"))

    def test_late_result_vetoes_generic_death_inside_the_existing_lock(self):
        self.write_row()
        original = REGISTRY.close_attempt_row_if
        def late_result(*args, **kwargs):
            self.terminal(verdict="FAIL")
            D.annotate_attempt_row_if(self.jobs, self.metadata["attempt_id"], {
                key: self.metadata[key] for key in (
                    "launch_outcome", "group_reap_proof", "group_reap_pgid",
                    "attempt_descendant_proof", "attempt_descendant_observer_ns",
                )
            }, lambda _fields: True)
            return original(*args, **kwargs)
        with mock.patch.object(REGISTRY, "close_attempt_row_if", side_effect=late_result):
            result = self.reconcile(apply=True, only_exact_dead=True)
        self.assertEqual(result["closed"], 0, result)
        self.assertIn("revalidation-veto:terminal-handoff", result["decisions"][0]["reason"])
        self.assertNotIn("note=dead-exact-pid", self.jobs.read_text())
        self.assertEqual(self.reconcile(apply=True)["closed"], 1)

    def test_pid_reuse_does_not_override_a_surviving_tagged_child(self):
        self.write_row(pid=str(os.getpid()), pid_start="1", pgid=str(os.getpid()))
        view = observe_attempt("open", self.metadata, worktree=self.base)
        self.assertEqual(view.process.state, "quiescent")
        self.assertIn("pid-reused", view.process.reason)
        with patch_split(D, "attempt_tagged_descendants", return_value=D.ProcessGroupObservation("populated", ((4242, "10", "S"),))):
            view = observe_attempt("open", self.metadata, worktree=self.base)
            self.assertEqual((view.process.state, view.decision.action), ("live", "wait"))
            result = self.reconcile(apply=True, only_exact_dead=True)
        self.assertEqual(result["closed"], 0)
        self.assertEqual(result["decisions"][0]["category"], "active")

    def test_inaccessible_namespace_preserves_unknown(self):
        self.write_row(pid_ns="pid:[9999999999]", pid_observer_ns="pid:[9999999999]")
        with patch_split(D, "authoritative_process_identities", return_value=[]), \
                patch_split(D, "namespace_gone", return_value="present"), \
                patch_split(D, "attempt_tagged_descendants", return_value=D.ProcessGroupObservation("unverifiable", (), "observer-namespace-mismatch")):
            view = observe_attempt("open", self.metadata, worktree=self.base)
            result = self.reconcile(apply=True, only_exact_dead=True)
        self.assertEqual((view.process.state, view.decision.action), ("unverifiable", "recover"))
        self.assertEqual(result["closed"], 0)

    def test_study_summary_without_group_observer_remains_unknown(self):
        # The report's summary omitted the stored group/observer fields. Keep
        # that abbreviated evidence intact rather than inventing missing data.
        self.metadata.update(pid="1920575", pid_start="595755203", pid_ns="pid:[4026531836]")
        self.metadata.pop("pgid")
        self.metadata.pop("pid_observer_ns")
        self.write_row()
        before = self.jobs.read_bytes()
        observation = observe_attempt("open", self.metadata, worktree=self.base)
        self.assertEqual((observation.process.state, observation.result_state, observation.decision.action),
                         ("unverifiable", "none", "recover"))
        self.assertEqual(observation.decision.responsible, "supervision-controller")
        self.assertEqual(self.reconcile()["decisions"][0]["category"], "unverifiable")
        applied = self.reconcile(apply=True, only_exact_dead=True)
        self.assertEqual((applied["decisions"][0]["category"], applied["closed"]), ("unverifiable", 0))
        self.assertEqual(D.existing_attempt_launch_state(self.jobs, self.metadata["attempt_id"])[0], "existing-unverified")
        with self.assertRaises(D.DispatchContractError) as caught:
            D._sibling_attempt_gate({"route_id": self.metadata["route_id"]}, "execute", self.jobs, attempt_id="att-next")
        self.assertEqual(caught.exception.reason, "prior-attempt-unverifiable")
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_study_original_review_smoke_row_closes_and_can_resume(self):
        self.write_row(
            attempt_id="att-d331a2c57af2ae157be9f82eab5abeca004b88775f0044ed",
            route_id="rt-b97a60012576904a", route_node="eval-smoke", worker_type="review",
            parent_attempt_id="att-a2e39042174e1e6974383ab8f49a34a7",
            pid="1920575", pid_start="595755203", pgid="1920575",
            pid_ns="pid:[4026531836]", pid_observer_ns="pid:[4026531836]",
        )
        # This is the namespace recorded by the real row. A fresh proof is
        # supplied by that namespace; it is never rewritten into the registry.
        with patch_split(D, "process_namespace_identity", return_value="pid:[4026531836]"), \
                patch_split(D, "_proc_observation", return_value=("missing", "", "")), \
                patch_split(D, "process_group_observation", return_value=D.ProcessGroupObservation("empty", ())):
            before = self.jobs.read_bytes()
            self.assertEqual(self.reconcile()["decisions"][0]["category"], "exact-dead")
            self.assertEqual(D.existing_attempt_launch_state(self.jobs, self.metadata["attempt_id"])[0], "existing-dead")
            D._sibling_attempt_gate({"route_id": self.metadata["route_id"]}, "eval-smoke", self.jobs, attempt_id="att-next")
            self.assertEqual(self.jobs.read_bytes(), before)
            result = self.reconcile(apply=True, only_exact_dead=True)
            self.assertEqual(result["closed"], 1, result)
            fresh = D.parse_registry_metadata(self.jobs.read_text().strip().split("\t")[5])
            self.assertEqual(observe_attempt("done", fresh, worktree=self.base).process.state, "quiescent")
            D._sibling_attempt_gate({"route_id": self.metadata["route_id"]}, "eval-smoke", self.jobs, attempt_id="att-next")
            self.assertNotEqual(fresh.get("failure_class"), "pass")

    def test_committed_pass_fail_and_cancellation_keep_their_outcomes(self):
        for note, failure_class, outcome in (("completed-marker", "pass", "succeeded"),
                                             ("dead-worker-fail", "fail", "failed"),
                                             ("cancelled-by-parent", "cancelled", "cancelled")):
            with self.subTest(note=note):
                metadata = {**self.metadata, "note": note, "failure_class": failure_class,
                            "registered_worker": "0"}
                view = observe_attempt("done", metadata, worktree=self.base)
                self.assertEqual((view.result_state, view.decision.outcome), ("settled", outcome))
                self.assertFalse(view.decision.retry_allowed)

    def test_absent_terminal_file_and_inaccessible_file_are_distinct(self):
        self.assertEqual(inspect_terminal_attempt(self.log, worktree=self.base, artifact_root_metadata=self.artifacts)["state"], "absent")
        with mock.patch("codex_dispatch_terminal._tail_lines", side_effect=PermissionError):
            self.assertEqual(inspect_terminal_attempt(self.log, worktree=self.base, artifact_root_metadata=self.artifacts)["state"], "error")


if __name__ == "__main__":
    unittest.main()
