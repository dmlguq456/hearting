#!/usr/bin/env python3
"""BC continuation review stays owned across turn and supervisor exit."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_completion_join as J
import owner_route_binding as O
import session_supervisor_decisions as D
from split_module_fixture import patch_split

spec = importlib.util.spec_from_file_location("orphan_watch", Path(__file__).with_name("dispatch-orphan-watch.py"))
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)


class ContinuationSupervisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.owner = "att-owner"
        self.source = self.route("rt-source", 0)
        self.target = self.route("rt-target", 1, self.source)
        self.state = self.root / "supervisor-state" / (self.owner + ".json")
        self.state.parent.mkdir()
        J.write_supervisor_state(self.state, self.owner, set(), phase="recovery")
        self.log = self.root / "owner.jsonl"
        self.log.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                       "result": "Review is running.\nruntime_wait: registered-children"}) + "\n")
        self.verify = patch_split(O.ROUTE, "verify_route", side_effect=lambda value, **kw: value)
        self.verify.start()
        self.addCleanup(self.verify.stop)
        self.proof = mock.patch.object(W, "observed_owner_lifecycle", return_value=(
            SimpleNamespace(process_state="quiescent"), "recovery", {}))
        self.proof.start()
        self.addCleanup(self.proof.stop)
        self.bind()
        O.publish_owner_route_advance(self.jobs, owner_attempt_id=self.owner,
            source=self.source, target=self.target, from_generation=0, to_generation=1)
        self.args = SimpleNamespace(jobs=self.jobs, attempt_id=self.owner, parent_attempt_id=self.owner,
            route_file=self.source.route_file, route_id=self.source.route_id, route_hash=self.source.route_hash,
            agent_home=self.root, interval=.01)

    def route(self, name, generation, source=None):
        path = self.root / (name + ".json")
        value = dict(route_id=name, route_hash="sha256:" + name, advance_generation=generation,
                     owner_attempt_id=self.owner, route_family_key="family", cwd=str(self.root),
                     artifact_root=str(self.root / "artifacts"), capability="autopilot-spec", capability_mode="update")
        if source:
            value.update(source_route_id=source.route_id, source_route_hash=source.route_hash,
                         source_route_supersession=dict(from_route_id=source.route_id, from_route_hash=source.route_hash))
        path.write_text(json.dumps(value))
        return O.OwnerRouteBinding(str(path), name, value["route_hash"])

    def row(self, aid, **metadata):
        return "now\topen\t{0}\t{0}\t{1}\t".format(self.root, aid) + ",".join(
            key + "=" + str(value) for key, value in dict(attempt_id=aid, attempt_schema_version=2,
                registered_worker=1, execution_surface="registered-headless", **metadata).items()) + "\n"

    def bind(self, harness="claude", adopted=True):
        self.jobs.write_text(self.row(self.owner, worker_type="owner", dispatch_depth=1, harness=harness,
            owner_route_file=self.source.route_file, owner_route_id=self.source.route_id,
            owner_route_hash=self.source.route_hash, log_file=self.log) +
            (self.row("att-review", worker_type="stage", dispatch_depth=2, parent_attempt_id=self.owner,
                route_file=self.target.route_file, route_id=self.target.route_id, route_hash=self.target.route_hash,
                route_node="review", capability="autopilot-spec", capability_mode="update",
                artifact_root=self.root / "artifacts", launch_started=1) if adopted else "") +
            self.row("att-foreign", worker_type="stage", dispatch_depth=2, parent_attempt_id=self.owner,
                     route_id="rt-unrelated", route_hash="sha256:unrelated", launch_started=1))

    def test_turn_follows_adopted_continuation_and_keeps_foreign_route_out(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.bind(harness)
                self.args.route_file, self.args.route_id, self.args.route_hash = (
                    self.source.route_file, self.source.route_id, self.source.route_hash)
                self.assertEqual(J.current_children(self.jobs, self.owner,
                    route_id=self.args.route_id, route_hash=self.args.route_hash), [])
                D.refresh_owner_route(self.args)
                rows = J.current_children(self.jobs, self.owner,
                    route_id=self.args.route_id, route_hash=self.args.route_hash)
                self.assertEqual([r.attempt_id for r in rows], ["att-review"])
                self.assertEqual(self.args.route_file, self.target.route_file)
                self.assertIn(self.source.route_id, self.jobs.read_text())

    def test_compiled_but_unadopted_route_does_not_move_supervisor(self):
        self.bind(adopted=False)
        D.refresh_owner_route(self.args)
        self.assertEqual(self.args.route_id, self.source.route_id)
        self.assertIsNone(W.recover_waiting_continuation(self.args))

    def test_dead_supervisor_waits_then_uses_normal_start_and_reuses_repeat(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.bind(harness)
                J.write_supervisor_state(self.state, self.owner, set(), phase="recovery")
                receipt = dict(route_id=self.target.route_id, owner_started=True)
                # Native terminal encodings are classified by the existing shared classifier.
                terminal = SimpleNamespace(failure_class="contract")
                with mock.patch.object(W, "classify_supervisor_log", return_value=terminal), \
                     mock.patch("dispatch_supervision._pending", side_effect=[True, False]), \
                     mock.patch.object(W.time, "sleep") as pause, \
                     mock.patch.object(W, "reconcile_supervisor_terminal", return_value="closed") as close, \
                     mock.patch.object(W.subprocess, "run", return_value=SimpleNamespace(
                         returncode=0, stdout=json.dumps(receipt))) as start, \
                     mock.patch.object(W, "_run_registry") as cascade:
                    self.assertTrue(W.recover_waiting_continuation(self.args))
                    pause.assert_called_once_with(.01)
                    close.assert_called_once()
                    self.assertIn(self.target.route_file, start.call_args.args[0])
                    self.assertIn(str(self.jobs), start.call_args.args[0])
                    cascade.assert_not_called()
                    self.assertIsNone(W.recover_waiting_continuation(self.args))
                    start.assert_called_once()
                self.assertIn("att-review", self.jobs.read_text())

    def test_real_blocked_or_parent_close_never_restarts(self):
        for failure in ("pass", "fail", "blocked", "capacity", "auth", "permission"):
            with self.subTest(failure=failure), mock.patch.object(W, "classify_supervisor_log",
                    return_value=SimpleNamespace(failure_class=failure)), mock.patch.object(W.subprocess, "run") as start:
                self.assertIsNone(W.recover_waiting_continuation(self.args))
                start.assert_not_called()
        with mock.patch("route_parent_close.row_requested", return_value=True), mock.patch.object(W.subprocess, "run") as start:
            self.assertIsNone(W.recover_waiting_continuation(self.args))
            start.assert_not_called()

    def test_committed_result_survives_missing_log(self):
        for note, failure in (("completed-supervisor", "pass"), ("dead-worker-fail", "fail"),
                              ("dead-worker-blocked", "blocked")):
            with self.subTest(note=note):
                self.bind()
                self.jobs.write_text(self.jobs.read_text().replace("now\topen", "now\tdone", 1).replace(
                    ",worker_type=owner", ",note=" + note + ",failure_class=" + failure + ",worker_type=owner"))
                with mock.patch.object(W, "classify_supervisor_log", return_value=SimpleNamespace(failure_class="runtime")), \
                     mock.patch.object(W.subprocess, "run") as start:
                    self.assertIsNone(W.recover_waiting_continuation(self.args))
                    start.assert_not_called()

    def test_unknown_owner_and_terminal_conflict_never_start(self):
        with mock.patch.object(W, "observed_owner_lifecycle", return_value=(
                SimpleNamespace(process_state="unverifiable"), "recovery", {})), \
             mock.patch.object(W.subprocess, "run") as start:
            self.assertIsNone(W.recover_waiting_continuation(self.args))
            start.assert_not_called()
        with mock.patch("dispatch_supervision._pending", return_value=False), \
             mock.patch.object(W, "reconcile_supervisor_terminal", return_value="terminal-conflict"), \
             mock.patch("dispatch_supervision.materialize"), mock.patch.object(W.subprocess, "run") as start:
            self.assertFalse(W.recover_waiting_continuation(self.args))
            start.assert_not_called()

    def test_unknown_observation_reparks_watch_without_terminal_commit(self):
        self.args.pid, self.args.pid_start = 99999999, "1"
        with mock.patch.object(W, "process_start", return_value=None), \
             mock.patch.object(W, "observed_owner_lifecycle", side_effect=[
                 (SimpleNamespace(process_state="unverifiable"), "recovery", {}),
                 (SimpleNamespace(process_state="quiescent"), "recovery", {})]), \
             mock.patch.object(W.time, "sleep") as pause, \
             mock.patch.object(W, "reconcile_exact_exit", return_value=0) as close:
            self.assertEqual(W.watch(self.args), 0)
            pause.assert_called_once_with(.01)
            close.assert_called_once()
            self.assertIn("now\topen", self.jobs.read_text())
        with mock.patch.object(W, "observed_owner_lifecycle", return_value=(
                SimpleNamespace(process_state="unverifiable"), "recovery", {})), \
             mock.patch.object(W, "_finish_recovery", return_value=False), \
             mock.patch.object(W, "reconcile_supervisor_terminal") as terminal:
            self.assertEqual(W.reconcile_exact_exit(self.args), 70)
            terminal.assert_not_called()

    def test_refused_start_keeps_state_and_parent_notice(self):
        with mock.patch("dispatch_supervision._pending", return_value=False), \
             mock.patch.object(W, "reconcile_supervisor_terminal", return_value="closed"), \
             mock.patch.object(W.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")), \
             mock.patch("dispatch_supervision.materialize") as notice:
            self.assertFalse(W.recover_waiting_continuation(self.args))
            self.assertTrue(self.state.exists())
            notice.assert_called_once_with(self.jobs, {self.owner}, reason="supervisor-exited")


if __name__ == "__main__":
    unittest.main()
