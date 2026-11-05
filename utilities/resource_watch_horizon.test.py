#!/usr/bin/env python3
"""Resource lifetime, identity-safe watch recovery, and original-parent handback."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from split_module_fixture import patch_split
spec = importlib.util.spec_from_file_location("horizon_fixture", HERE / "workflow_supervisor.test.py")
FIX = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = FIX
spec.loader.exec_module(FIX)
SUP = FIX.SUP


class ResourceWatchHorizonTest(FIX.WorkflowFixture):
    _resume_fixture = FIX.TestSupervisorAdvance._resume_fixture

    def living(self, *, ordinary=False):
        import dispatch_resource_wait as OWNER_RESOURCE
        route, path, jobs, registry, output = self._resume_fixture(ordinary=ordinary)
        row = json.loads(registry.read_text())["runs"]["fixture-run"]
        row.update(**SUP.RR.proc_identity(os.getpid()), status="running", exit_code=None,
                   supervision={"pid": 999999999, "starttime": "1", "command_hash": "0" * 64})
        Path(row["sentinel"]).unlink()
        registry.write_text(json.dumps({"runs": {"fixture-run": row}}))
        ledger = SUP.ledger_for(route, jobs)
        node = row["node"]
        arm_path = ledger.root / "armed" / (node + ".json")
        armed = json.loads(arm_path.read_text())
        if ordinary:
            armed["resource_binding"] = OWNER_RESOURCE.resource_body_digest(row)
            arm_path.write_text(json.dumps(armed))
        return route, path, jobs, registry, output, ledger, armed, row

    def watch(self, observations, *, resource=True):
        route, path = self.two_stage_route()
        ledger = SUP.ledger_for(route)
        results = [{"node": "run", "action": "wait", "evidence": {"liveness": live}}
                   for live in observations] + [{"node": "run", "action": "settled"}]
        now = [0.0]
        def sleep(_):
            now[0] += 90000.0
        armed = {"run": {"predecessor_kind": "resource" if resource else "registered"}}
        args = SimpleNamespace(route=path, jobs=None, max=86400, interval=1, ready_fd=None)
        with mock.patch.object(SUP, "poll_once", side_effect=[[r] for r in results]) as poll, \
                mock.patch.object(SUP, "read_armed", return_value=armed), \
                mock.patch.object(SUP, "resume_recovered_resource_owner", return_value=[]), \
                mock.patch.object(SUP.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(SUP.time, "sleep", side_effect=sleep), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = SUP.cmd_watch(args)
        return result, json.loads(out.getvalue()), poll.call_count, now[0]

    def test_exact_resource_survives_multiple_days_then_settles(self):
        code, out, calls, elapsed = self.watch(["working"] * 4)
        self.assertEqual((code, calls, out["timeout"]), (0, 5, False))
        self.assertGreater(elapsed, 3 * 86400)

    def test_dead_reused_unknown_and_unreaped_resources_keep_bounded_observation(self):
        for live in ("exited", "stale", "unknown", "reaping"):
            with self.subTest(liveness=live):
                code, out, calls, _ = self.watch(["working", live])
                self.assertEqual((code, calls, out["timeout"]), (3, 2, True))

    def test_registered_watch_keeps_its_explicit_deadline(self):
        code, out, calls, _ = self.watch(["working"] * 3, resource=False)
        self.assertEqual((code, calls, out["timeout"]), (3, 2, True))

    def test_concurrent_resume_starts_one_observer_and_preserves_payload_and_outputs(self):
        route, _, _, registry, output, ledger, armed, original = self.living(ordinary=True)
        producer = output / "run.json"
        before = producer.read_bytes(), producer.stat().st_ino, producer.stat().st_mtime_ns
        identity = SUP.RR.proc_identity(os.getpid())
        with mock.patch.object(SUP.runner(), "start_watch", return_value=(mock.Mock(), identity)) as start:
            with ThreadPoolExecutor(max_workers=4) as pool:
                recovered = list(pool.map(lambda _: SUP.reattach_resource_watch(route, ledger, armed), range(4)))
            self.assertEqual(start.call_count, 1)
        self.assertEqual(sum(r["recovered"] for r in recovered), 1)
        row = json.loads(registry.read_text())["runs"]["fixture-run"]
        self.assertEqual({k: v for k, v in row.items() if k != "supervision"},
                         {k: v for k, v in original.items() if k != "supervision"})
        self.assertEqual((producer.read_bytes(), producer.stat().st_ino, producer.stat().st_mtime_ns), before)
        self.assertFalse(Path(row["sentinel"]).exists())

    def test_absent_or_reused_pid_never_attaches_or_launches_payload(self):
        route, _, _, registry, _, ledger, armed, original = self.living()
        for change in ({"pid": 999999999}, {"starttime": "reused"}, {"command_hash": "0" * 64}):
            with self.subTest(change=change):
                row = {**original, **change}
                registry.write_text(json.dumps({"runs": {"fixture-run": row}}))
                with mock.patch.object(SUP.runner(), "start_watch") as start:
                    self.assertIsNone(SUP.reattach_resource_watch(route, ledger, armed))
                    start.assert_not_called()
                self.assertEqual(json.loads(registry.read_text())["runs"]["fixture-run"], row)

    def test_running_compute_receipt_reattaches_without_remote_observation(self):
        import dispatch_resource_wait as OWNER_RESOURCE
        route, _, _, registry, _, ledger, armed, row = self.living(ordinary=True)
        row.update({"status": "running", "execution": {"kind": "compute-hosts",
                    "run_id": "compute-live", "receipt_state": "running"},
                    "compute_run_id": "compute-live", "pid": None, "starttime": None,
                    "command_hash": None, "pid_namespace": None, "process_group": None})
        row["resource_binding"] = OWNER_RESOURCE.resource_body_digest(row)
        armed["resource_binding"] = row["resource_binding"]
        registry.write_text(json.dumps({"runs": {row["run_id"]: row}}))
        with mock.patch.object(SUP.RR, "compute_execution_observation",
                               side_effect=AssertionError("remote observation under recovery")), \
                mock.patch.object(SUP.runner(), "start_watch",
                                  return_value=(mock.Mock(), SUP.RR.proc_identity(os.getpid()))) as start:
            self.assertEqual(SUP.resource_liveness_readonly(armed)["liveness"], "working")
            self.assertEqual(SUP.predecessor_liveness_readonly(armed)["liveness"], "working")
            recovered = SUP.reattach_resource_watch(route, ledger, armed)
        self.assertIsNotNone(recovered)
        self.assertTrue(recovered["recovered"])
        start.assert_called_once()
        self.assertEqual(json.loads(registry.read_text())["runs"][row["run_id"]]["execution"],
                         row["execution"])

    def test_real_observer_ready_handshake_and_replay_keep_the_same_resource(self):
        route, _, _, registry, _, ledger, armed, original = self.living()
        owned = []
        start = SUP.runner().start_watch
        def observer(*args):
            proc, identity = start(*args)
            owned.append(proc)
            return proc, identity
        try:
            with mock.patch.object(SUP.runner(), "start_watch", side_effect=observer):
                first = SUP.reattach_resource_watch(route, ledger, armed)
                again = SUP.reattach_resource_watch(route, ledger, armed)
            self.assertEqual(len(owned), 1)
            self.assertTrue(SUP.RESOURCE_RESUME.supervisor_alive(first["supervision"]))
            self.assertFalse(again["recovered"])
            row = json.loads(registry.read_text())["runs"]["fixture-run"]
            self.assertEqual({k: v for k, v in row.items() if k != "supervision"},
                             {k: v for k, v in original.items() if k != "supervision"})
        finally:
            # These handles belong only to the fixture's new observers.
            for proc in owned:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)

    def test_parent_close_journal_suppresses_recovery_without_reclassifying_resource(self):
        route, _, _, registry, _, ledger, armed, row = self.living()
        original = copy.deepcopy(row)
        with mock.patch.object(ledger, "journal", return_value=[{"evidence": {"parent_close": {"close": True}}}]), \
                mock.patch.object(SUP.runner(), "start_watch") as start:
            self.assertIsNone(SUP.reattach_resource_watch(route, ledger, armed))
            start.assert_not_called()
        self.assertEqual(json.loads(registry.read_text())["runs"]["fixture-run"], original)

    def test_old_timeout_is_bound_to_exact_resource_identity(self):
        route, _, _, _, _, ledger, armed, row = self.living()
        log = ledger.root / "resource/watch.log"
        log.parent.mkdir()
        receipt = {"route_id": route["route_id"], "timeout": True, "results": [{
            "node": armed["node"], "evidence": {"liveness": "working",
            "identity": f"{row['run_id']}:{row['pid']}:{row['starttime']}:None"}}]}
        log.write_text(json.dumps(receipt) + "\n")
        self.assertTrue(SUP.resource_watch_expired(ledger, armed, row))
        self.assertFalse(SUP.resource_watch_expired(ledger, armed, {**row, "starttime": "reused"}))
        receipt["timeout"] = False
        log.write_text(json.dumps(receipt) + "\n")
        self.assertFalse(SUP.resource_watch_expired(ledger, armed, row))

    def test_complete_timeout_receipt_and_second_recovery_preserve_expiry(self):
        route, _, _, registry, _, ledger, armed, row = self.living()
        log = ledger.root / "resource/watch.log"
        log.parent.mkdir()
        receipt = {"route_id": route["route_id"], "timeout": True, "results": [{
            "node": armed["node"], "evidence": {"liveness": "working",
            "identity": f"{row['run_id']}:{row['pid']}:{row['starttime']}:None",
            "artifacts": {"path": "x" * 70000}}}]}
        log.write_text(json.dumps(receipt) + "\n" + "unrelated crash noise\n" * 8000)
        self.assertTrue(SUP.resource_watch_expired(ledger, armed, row))
        for recovery in range(2):
            with mock.patch.object(SUP.RESOURCE_RESUME, "supervisor_alive", return_value=False), \
                    mock.patch.object(SUP.runner(), "start_watch", return_value=(mock.Mock(), SUP.RR.proc_identity(os.getpid()))):
                recovered = SUP.reattach_resource_watch(route, ledger, armed)
                self.assertTrue(recovered["supervision"]["expired"])
                if recovery == 0:
                    log.write_text("rotated\n")
        actual = json.loads(registry.read_text())["runs"]["fixture-run"]
        self.assertEqual(actual["pid"], row["pid"])
        self.assertEqual(actual["starttime"], row["starttime"])

    def test_existing_start_returns_the_resource_continuation_before_owner_replacement(self):
        import work_start as START
        route, path, jobs, _, _, _, _, _ = self.living()
        route["work_request"] = {"text": "continue this resource", "owner_harness": "codex"}
        with mock.patch.object(SUP.runner(), "start_watch", return_value=(mock.Mock(), SUP.RR.proc_identity(os.getpid()))), \
                mock.patch("dispatch_resource_wait.supervisor", return_value=SUP), \
                mock.patch.object(START, "_advance_plan", side_effect=lambda r, j, result, **kw: result):
            result = START.start_work(route, path, jobs)
            replay = START.start_work(route, path, jobs)
        self.assertEqual((result["state"], result["owner_started"], result["parent_next"]),
                         ("resource-watching", False, "end-turn"))
        self.assertFalse(replay["resource_watches"][0]["recovered"])

    def test_parked_controller_recovers_live_resource_before_empty_child_refusal(self):
        import dispatch_resource_wait as WAIT
        route, _, jobs, _, _, ledger, armed, row = self.living(ordinary=True)
        control = SimpleNamespace(thread_id="native", pending=lambda: False)
        args = SimpleNamespace(parent_attempt_id="att-parent")
        found = (SUP, route, ledger, [(armed, row)])
        class Parked(Exception):
            pass
        with mock.patch.object(WAIT, "context", return_value=found), \
                mock.patch.object(WAIT, "pending_prompt", return_value=None), \
                mock.patch.object(WAIT.JOIN, "read_supervisor_phase_state", return_value=None), \
                mock.patch.object(WAIT, "_write"), \
                mock.patch.object(SUP, "poll_once", return_value=[]), \
                mock.patch.object(SUP.runner(), "start_watch", return_value=(mock.Mock(), SUP.RR.proc_identity(os.getpid()))) as start:
            with self.assertRaises(Parked):
                WAIT.wait(args, self.base / "phase.json", control, [], emit=lambda _: None,
                          sleep=lambda _: (_ for _ in ()).throw(Parked()))
            self.assertEqual(start.call_count, 1)

    def test_recovered_success_hands_back_only_to_ended_owner_on_every_harness(self):
        import dispatch_owner_input as INPUT
        import dispatch_supervision as DELIVERY
        route, _, jobs, registry, _ = self._resume_fixture(ordinary=True)
        row = json.loads(registry.read_text())["runs"]["fixture-run"]
        row.update(status="succeeded", exit_code=0, supervision={"recovered": True, "expired": True})
        registry.write_text(json.dumps({"runs": {"fixture-run": row}}))
        ledger = SUP.ledger_for(route, jobs)
        evidence = SUP.resource_evidence(SUP.read_armed(ledger)["full-run"])
        ledger.record("full-run", "STAGE_SUCCEEDED", evidence=evidence, actor="fixture")
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness), \
                    mock.patch.object(INPUT, "_target", return_value=(SimpleNamespace(
                        status="done", metadata={"harness": harness, "note": "dead-worker-blocked"}), "target")), \
                    patch_split(INPUT, "supervisor_lease_is_held", return_value=False), \
                    mock.patch.object(INPUT, "retained", return_value=[]), \
                    mock.patch.object(INPUT, "submit", return_value={"retained": True}) as submit, \
                    mock.patch.object(DELIVERY, "materialize", return_value=[{
                        "receipt": {"recipient_thread_id": "original-parent"},
                        "recipient_kind": harness + "-parent-runtime"}]) as deliver, \
                    mock.patch.object(DELIVERY, "continue_for_parent") as resume:
                SUP.resume_recovered_resource_owner(route, ledger)
                self.assertEqual(submit.call_args.args[:2], (str(jobs), "att-parent"))
                self.assertEqual(deliver.call_args.kwargs["reason"], DELIVERY.ANSWER_AWAITING_PARENT)
                self.assertIn("do not launch its payload again", submit.call_args.args[2])
                resume.assert_not_called()
        with mock.patch.object(ledger, "journal", return_value=[{"evidence": {"parent_close": {"close": True}}}]), \
                mock.patch.object(INPUT, "submit") as submit:
            SUP.resume_recovered_resource_owner(route, ledger)
            submit.assert_not_called()

        for harness in ("claude", "codex", "opencode"):
            for status, note in (("done", "dead-worker-fail"), ("running", "dead-worker-blocked")):
                with self.subTest(harness=harness, status=status, note=note), \
                        mock.patch.object(INPUT, "_target", return_value=(SimpleNamespace(status=status,
                            metadata={"harness": harness, "note": note, "terminal_verdict": "FAIL"}), "target")), \
                        mock.patch.object(INPUT, "submit") as submit, \
                        mock.patch.object(DELIVERY, "materialize") as deliver:
                    SUP.resume_recovered_resource_owner(route, ledger)
                    submit.assert_not_called()
                    deliver.assert_not_called()

    def test_settled_watch_retries_held_lease_and_submission_race_before_exiting(self):
        import dispatch_owner_input as INPUT
        import dispatch_supervision as DELIVERY
        route, path, jobs, registry, _ = self._resume_fixture(ordinary=True)
        row = json.loads(registry.read_text())["runs"]["fixture-run"]
        row.update(status="succeeded", exit_code=0, supervision={"expired": True})
        registry.write_text(json.dumps({"runs": {"fixture-run": row}}))
        ledger = SUP.ledger_for(route, jobs)
        evidence = SUP.resource_evidence(SUP.read_armed(ledger)["full-run"])
        ledger.record("full-run", "STAGE_SUCCEEDED", evidence=evidence, actor="fixture")
        args = SimpleNamespace(route=path, jobs=jobs, max=86400, interval=1, ready_fd=None)
        with mock.patch.object(SUP, "poll_once", side_effect=lambda *a: [{"node": "full-run", "action": "settled"}]) as poll, \
                mock.patch.object(INPUT, "_target", return_value=(SimpleNamespace(status="done",
                    metadata={"note": "dead-worker-blocked"}), "target")), \
                patch_split(INPUT, "supervisor_lease_is_held", side_effect=[True, False, False]), \
                mock.patch.object(INPUT, "retained", return_value=[]), \
                mock.patch.object(INPUT, "submit", side_effect=[INPUT.InputError("lease-race"), {"retained": True}]) as submit, \
                mock.patch.object(DELIVERY, "materialize") as deliver, \
                mock.patch.object(SUP.time, "sleep"), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(SUP.cmd_watch(args), 0)
        self.assertEqual((poll.call_count, submit.call_count, deliver.call_count), (3, 2, 1))
        self.assertFalse(json.loads(out.getvalue())["timeout"])


if __name__ == "__main__":
    unittest.main()
