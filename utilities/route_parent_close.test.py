#!/usr/bin/env python3
"""Safe real-process fixtures for parent close; no provider or live project runs."""
from __future__ import annotations
from types import SimpleNamespace
import contextlib
import io
import hashlib
import time
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch_contract as DC
import route_parent_close as CLOSE
import dispatch_completion_join as JOIN
import dispatch_attempt_policy as POLICY
import workflow_state as WS
import resource_run_registry as RR
import work_start
sys.path.insert(0, str(HERE.parent / "tools"))
import fixture_processes


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROUTE = load("parent_close_route_fixture", "capability-route.py")
SUP = load("parent_close_supervisor_fixture", "workflow-supervisor.py")


class ParentCloseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.jobs = self.base / "dispatch" / "fixture-jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.write_text("")
        self.artifacts = self.base / ".agent_reports"
        self.artifacts.mkdir()
        self.processes = []
        self.suffix = self.base.name
        self.addCleanup(self.reap)
        self.addCleanup(fixture_processes.reap, str(self.base))
        env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_")
               and key not in {"CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID"}}
        env.update(AGENT_HOME=str(HERE.parent), AGENT_DISPATCH_JOBS=str(self.jobs),
            AGENT_ARTIFACT_ROOT=str(self.artifacts), AGENT_ARTIFACT_CHECKPOINT="off",
            AGENT_RESOURCE_RUN_INDEX=str(self.base / "resource-index.json"),
            XDG_STATE_HOME=str(self.base / "state"), CODEX_THREAD_ID="fixture-parent", COMPUTE_HOSTS_CONFIG=str(self.base / "no-compute.yaml"))
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        tuples = [{"parent_harness": h, "child_harness": h, "parent_transport": "headless",
                   "parent_sandbox": ROUTE.WRAPPER_PARENT_SANDBOXES[h][0], "status": "supported",
                   "launch_authority": "conductor", "probe_source": "fixture",
                   "probe_time": "2026-10-08T00:00:00Z", "failure_class": "",
                   "checked_worktree": str(self.base), "failure_scope": "none",
                   "codex_command": "ok" if h == "codex" else "not-applicable",
                   "retry_on_isolated_worktree": 0} for h in ("claude", "codex", "opencode")]
        self.route = ROUTE.compile_route("autopilot-code", "dev", "standard", self.base,
            self.artifacts, predicates=[], transport="headless", transport_evidence="fixture",
            inline_reason=None, tracking="tracked", dispatch_evidence={"tuples": tuples, "native_subagent": []},
            tracked_gate_evidence={"spec_read": {"satisfied": True, "source": "fixture"},
                "drift_verdict": "within-spec", "workflow_mode": "tracked",
                "artifact_guard": {"satisfied": True, "source": "fixture"}})
        self.path = self.artifacts / ".runtime" / "routes" / (self.route["route_id"] + ".json")
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.route))

    def reap(self):
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()

    def aid(self, name):
        return name + "-" + self.suffix

    def process(self, aid, *, ignore_term=False, script=None, env=None):
        script = script or ("import signal,time; " +
            ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "") +
            "print('ready',flush=True); time.sleep(300)")
        process = subprocess.Popen([sys.executable, "-c", script], start_new_session=True,
            stdout=subprocess.PIPE, text=True, env={**os.environ, "AGENT_DISPATCH_ATTEMPT_ID": self.aid(aid), **(env or {})})
        self.processes.append(process)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    def row(self, aid, *, parent=None, process=None, harness="codex", status="open", worktree=None, **extra):
        aid = self.aid(aid)
        parent = self.aid(parent) if parent else None
        meta = {"attempt_schema_version": "2", "dispatch_depth": "2" if parent else "1",
                "transport": "headless", "execution_surface": "registered-headless",
                "fallback_hop": "same-harness-headless",
                "registered_worker": "1", "attempt_id": aid, "harness": harness,
                "launch_claimed": "1" if process else "0"}
        if parent:
            meta.update(parent_attempt_id=parent, route_id=self.route["route_id"],
                        route_hash=self.route["route_hash"], worker_type="stage")
        else:
            meta.update(worker_type="owner", unit="_kernel/owner", parent_sid="fixture-parent",
                owner_route_file=str(self.path), owner_route_id=self.route["route_id"],
                owner_route_hash=self.route["route_hash"], capability="autopilot-code",
                capability_mode="dev", intensity="standard", owner_harness=harness,
                artifact_root=str(self.artifacts))
        if process:
            meta.update(DC.process_launch_identity(process.pid), launch_started="1")
        meta.update(extra)
        DC.validate_attempt_metadata(meta)
        row = "\t".join(["2026-10-08T00:00:00Z", status, str(self.base), str(worktree or self.base), aid,
                         ",".join(k + "=" + str(v) for k, v in meta.items())]) + "\n"
        with self.jobs.open("a") as handle:
            handle.write(row)
        return meta

    def close(self, **kwargs):
        return CLOSE.close(self.route, self.path, jobs=self.jobs, **kwargs)

    def _explicit_jobs_handoff(self):
        import artifact_identity
        import artifact_producer as PRODUCER
        import route_authority as AUTH

        ambient = self.base / "ambient-dispatch" / "jobs.log"
        ambient.parent.mkdir()
        ambient.write_text("")
        os.environ["AGENT_DISPATCH_JOBS"] = str(ambient)
        ids = artifact_identity.IdAllocator()
        PRODUCER.activate(self.artifacts, repository_id=ids.allocate("repository"),
                          artifact_root_id=ids.allocate("artifact_root"),
                          w7={"campaign_id": ids.allocate("campaign")})
        owner = self.row("explicit-owner", harness="codex")
        begun = PRODUCER.begin(
            self.artifacts, route_file=self.path, capability=self.route["capability"],
            intensity=self.route["effective_intensity"], jobs=self.jobs,
            owner_attempt_id=owner["attempt_id"])
        for target in ("owner", "worker"):
            AUTH.record_pin_change(
                self.route, target=target,
                pin={"harness": "claude", "model": None, "effort": None},
                by={"harness": "codex", "session_id": "fixture-parent"},
                source="probe", tuples=[], candidates=[])
        outcome = CLOSE.close(self.route, self.path, jobs=self.jobs,
                              summary=work_start.PIN_HANDOFF_SUMMARY)
        record = PRODUCER.read_cycle_record(self.artifacts, begun["cycle_id"])
        self.assertEqual(outcome["state"], "cancelled", outcome)
        self.assertEqual(CLOSE._rows(self.jobs)[owner["attempt_id"]][1]["failure_class"], "cancelled")
        self.assertNotIn(owner["attempt_id"], CLOSE._rows(ambient))
        self.assertEqual(record["state"], "open", record)
        self.assertIsNotNone(CLOSE.verified_owner_pin_handoff_close(self.route, jobs=self.jobs))
        return begun["cycle_id"], outcome, ambient

    def test_explicit_jobs_handoff_stays_open_on_fresh_close(self):
        cycle_id, _outcome, _ambient = self._explicit_jobs_handoff()
        import artifact_producer as PRODUCER
        self.assertEqual(PRODUCER.read_cycle_record(self.artifacts, cycle_id)["state"], "open")

    def test_explicit_jobs_handoff_stays_open_on_settled_replay(self):
        cycle_id, outcome, _ambient = self._explicit_jobs_handoff()
        replay = CLOSE.close(self.route, self.path, jobs=self.jobs,
                             summary=work_start.PIN_HANDOFF_SUMMARY)
        self.assertEqual(replay, outcome)
        import artifact_producer as PRODUCER
        self.assertEqual(PRODUCER.read_cycle_record(self.artifacts, cycle_id)["state"], "open")

    def terminal_fail_stage(self, *, parent="att-owner", name="att-terminal-fail",
                            status="done", foreground=True, cleanup=True,
                            invalid_foreground=False, cleanup_overrides=None,
                            cleanup_digest=None):
        import base64

        attempt_id = self.aid(name)
        external_namespace = "pid:[999999999]"
        metadata = {
            "registered_worker": "1", "launch_claimed": "1", "launch_started": "1",
            "pid_scope": "namespace-local", "pid": "99999996", "pid_start": "123456",
            "pgid": "99999996", "pid_ns": external_namespace,
            "pid_observer_ns": external_namespace,
            "route_node": self.route["nodes"][0]["id"],
            "failure_class": "fail", "note": "worker-failed",
            "completion_marker": "fixture-marker", "delivery_receipt_b64": "fixture-delivery",
        }
        if foreground:
            metadata.update(
                launch_lifecycle="foreground-scoped",
                launch_outcome=("unsealed-foreground-exit" if invalid_foreground
                                else "governed-process-reaped"),
                group_reap_proof=DC.GROUP_REAP_PROOF, group_reap_pgid="99999996")
        child = self.row(name, parent=parent, status=status, **metadata)
        self.assertEqual(child["attempt_id"], attempt_id)
        self.assertEqual(DC._foreground_reap_receipt(child), foreground and not invalid_foreground)
        if cleanup:
            proof = DC.QuiescenceProof(
                True, "cancellation-quiescence-proven", attempt_id, "exact-teardown",
                int(child["pgid"]), "empty", "empty", True,
                DC._cancellation_quiescence_binding_digest(child))
            cleanup_record = DC._cancellation_quiescence_receipt_record(child, proof)
            cleanup_record["receipt_type"] = DC.CLEANUP_RECEIPT_TYPE
            cleanup_record.update(cleanup_overrides or {})
            child.update(
                cleanup_receipt_b64=base64.b64encode(
                    json.dumps(cleanup_record, sort_keys=True).encode()).decode(),
                cleanup_receipt_digest=cleanup_digest or DC._canonical_sha256(cleanup_record),
            )
            DC.annotate_attempt_row(self.jobs, attempt_id, {
                "cleanup_receipt_b64": child["cleanup_receipt_b64"],
                "cleanup_receipt_digest": child["cleanup_receipt_digest"],
            })
        child = CLOSE._rows(self.jobs)[attempt_id][1]
        if cleanup and not cleanup_overrides and not cleanup_digest:
            self.assertEqual(DC._cleanup_receipt_reason(child), "attempt-cleanup-proven")
        if foreground and cleanup and not invalid_foreground:
            self.assertEqual(DC.attempt_process_quiescence(child, terminal_receipt=True).state,
                             "quiescent")
        return child

    def test_terminal_namespace_receipts_settle_fail_without_changing_result(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        child = self.terminal_fail_stage()
        attempt_id = child["attempt_id"]

        result = self.close()

        self.assertNotEqual(result["state"], "termination-pending", result)
        self.assertEqual(result["state"], "cancelled")
        fields, settled = CLOSE._rows(self.jobs)[attempt_id]
        self.assertEqual(fields[1], "done")
        self.assertEqual(settled.get("parent_close_settled"), "1")
        self.assertEqual(settled["failure_class"], "fail")
        self.assertEqual(settled["note"], "worker-failed")
        self.assertEqual(settled["completion_marker"], "fixture-marker")
        self.assertEqual(settled["delivery_receipt_b64"], "fixture-delivery")
        self.assertEqual(set(settled) - set(child), {"parent_close_settled"})
        registry = self.jobs.read_bytes()
        self.assertEqual(self.close(), result)
        self.assertEqual(self.jobs.read_bytes(), registry)

    def test_open_namespace_receipt_stays_pending(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        child = self.terminal_fail_stage(status="open", name="att-open-receipt")
        result = self.close()
        self.assertEqual(result["state"], "termination-pending")
        self.assertIn(child["attempt_id"], result["pending_attempts"])
        fields, current = CLOSE._rows(self.jobs)[child["attempt_id"]]
        self.assertEqual(fields[1], "open")
        self.assertNotIn("parent_close_settled", current)

    def test_terminal_namespace_without_receipt_stays_pending(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        child = self.terminal_fail_stage(name="att-no-receipt", foreground=False, cleanup=False)
        # The recorded namespace still exists: no extinction proof stands in
        # for the missing receipt, on a host observer or inside a sandbox.
        with mock.patch.object(DC, "namespace_gone", return_value="present"):
            result = self.close()
        self.assertEqual(result["state"], "termination-pending")
        self.assertIn(child["attempt_id"], result["pending_attempts"])
        self.assertNotIn("parent_close_settled", CLOSE._rows(self.jobs)[child["attempt_id"]][1])

    def test_invalid_foreground_with_valid_exact_teardown_stays_pending(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        child = self.terminal_fail_stage(name="att-invalid-foreground",
                                         invalid_foreground=True)
        self.assertFalse(DC._foreground_reap_receipt(child))
        self.assertEqual(DC._cleanup_receipt_reason(child), "attempt-cleanup-proven")
        result = self.close()
        self.assertEqual(result["state"], "termination-pending")
        self.assertIn(child["attempt_id"], result["pending_attempts"])
        self.assertNotIn("parent_close_settled", CLOSE._rows(self.jobs)[child["attempt_id"]][1])

    def test_foreign_cleanup_receipt_without_foreground_stays_pending(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        variants = {
            "att-foreign-attempt": {"cleanup_overrides": {"attempt_id": self.aid("att-other")}},
            "att-foreign-route": {"cleanup_overrides": {"route_id": "rt-other"}},
            "att-foreign-hash": {"cleanup_overrides": {"route_hash": "sha256:" + "1" * 64}},
            "att-foreign-node": {"cleanup_overrides": {"node_or_group_leg": "other-node"}},
            "att-foreign-binding": {"cleanup_overrides": {"binding_digest": "sha256:" + "2" * 64}},
            "att-wrong-digest": {"cleanup_digest": "sha256:" + "0" * 64},
        }
        children = {}
        for name, receipt in variants.items():
            child = self.terminal_fail_stage(name=name, foreground=False, **receipt)
            self.assertEqual(DC._cleanup_receipt_reason(child), "", name)
            children[name] = child
        with mock.patch.object(DC, "namespace_gone", return_value="present"):
            result = self.close()
        self.assertEqual(result["state"], "termination-pending")
        for name, child in children.items():
            with self.subTest(name):
                self.assertIn(child["attempt_id"], result["pending_attempts"])
                current = CLOSE._rows(self.jobs)[child["attempt_id"]][1]
                self.assertNotIn("parent_close_settled", current)
                self.assertEqual(current["failure_class"], "fail")

    def test_bad_cleanup_digest_does_not_veto_valid_foreground_receipt(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        child = self.terminal_fail_stage(name="att-bad-cleanup-digest")
        DC.annotate_attempt_row(self.jobs, child["attempt_id"], {
            "cleanup_receipt_digest": "sha256:" + "0" * 64,
        })
        child = CLOSE._rows(self.jobs)[child["attempt_id"]][1]
        self.assertTrue(DC._foreground_reap_receipt(child))
        self.assertEqual(DC._cleanup_receipt_reason(child), "")
        result = self.close()
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(CLOSE._rows(self.jobs)[child["attempt_id"]][1].get(
            "parent_close_settled"), "1")

    def test_start_pin_receipt_closes_owner_preserves_resource_and_starts_current_pin(self):
        import shlex
        import route_authority as AUTH
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                child = ParentCloseTest(); child.setUp()
                try:
                    target = "claude" if harness == "codex" else "codex"
                    child.route = ROUTE.compose_route(
                        capability="autopilot-code", capability_mode="debug", shape="staged",
                        graph="execute,test,report", slug="pin-handoff", cwd=child.base,
                        artifact_root=child.artifacts, spec_read="fixture", unassigned=True,
                        dispatch_evidence=child.route["dispatch_evidence"], parent_harness="codex",
                        work_request={"text": "Fix the bug and verify it", "owner_harness": harness})
                    child.path = child.path.with_name(child.route["route_id"] + ".json")
                    child.path.write_text(json.dumps(child.route))
                    owner = child.process("att-owner")
                    child.row("att-owner", process=owner, harness=harness)
                    failed_stage = child.terminal_fail_stage(name="att-terminal-fail")
                    resource = child.process("att-owner")
                    child.resource(resource)
                    sealed = child.path.read_bytes()
                    # The real CLI records the pin and returns one exact next command.
                    notice = io.StringIO()
                    argv = ["capability-route.py", "start", "--route", str(child.path),
                            "--jobs", str(child.jobs), "--pin", "owner=" + target,
                            "--pin", "worker=" + target]
                    with mock.patch.object(ROUTE, "_pin_change_probe", return_value=([], [])), \
                            mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(notice):
                        ROUTE.main()
                    receipt = json.loads(notice.getvalue())
                    self.assertEqual(receipt.get("reason"), "owner-pin-handoff", receipt)
                    command = shlex.split(receipt["recovery_command"])
                    self.assertIn("close", command)
                    self.assertNotIn("--stop-resources", command)
                    self.assertIsNone(owner.poll())
                    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    closed = json.loads(result.stdout)
                    self.assertEqual(closed["state"], "cancelled", closed)
                    self.assertIn("successor_route", closed, closed)
                    settled = CLOSE._rows(child.jobs)[failed_stage["attempt_id"]][1]
                    self.assertEqual(settled.get("parent_close_settled"), "1")
                    self.assertEqual(settled["failure_class"], "fail")
                    owner.wait(timeout=5)
                    self.assertIsNone(resource.poll())
                    successor_path = Path(closed["successor_route"])
                    successor = ROUTE.verify_route(json.loads(successor_path.read_text()))
                    self.assertEqual(successor["source_route_id"], child.route["route_id"])
                    self.assertEqual(AUTH.sealed_pin_harness(successor, worker_type="owner"), target)
                    self.assertEqual(child.path.read_bytes(), sealed)
                    replay = subprocess.run(command, capture_output=True, text=True, timeout=60)
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                    self.assertEqual(json.loads(replay.stdout)["successor_route"], str(successor_path))
                    with mock.patch.object(ROUTE, "build_continuation_route",
                                           side_effect=AssertionError("replayed close rebuilt its suffix")), \
                            mock.patch.object(work_start, "_route_module", return_value=ROUTE):
                        replayed = work_start.pin_handoff_continuation(
                            child.route, child.path, child.jobs, closed)
                    self.assertEqual(replayed["successor_route"], str(successor_path))
                    # Changing back to the original harness still follows the current pin,
                    # rather than returning the previously prepared target from the journal.
                    for selected in (harness, target):
                        for pin_target in ("owner", "worker"):
                            AUTH.record_pin_change(child.route, target=pin_target,
                                pin={"harness": selected, "model": None, "effort": None},
                                by={"harness": "codex", "session_id": "fixture-parent"},
                                source="fixture", tuples=[], candidates=[])
                        replayed = work_start.pin_handoff_continuation(
                            child.route, child.path, child.jobs, CLOSE.close(child.route, child.path, jobs=child.jobs))
                        replay_route = json.loads(Path(replayed["successor_route"]).read_text())
                        self.assertEqual(AUTH.sealed_pin_harness(replay_route, worker_type="owner"), selected)
                        self.assertIsNone(resource.poll())
                    calls = []
                    def launch(command, **kwargs):
                        calls.append(command)
                        aid = command[command.index("--attempt-id") + 1]
                        meta = {"attempt_id": aid, "parent_sid": "fixture-parent", "launch_started": "1",
                                "worker_type": "owner", "harness": target, "dispatch_depth": "1",
                                "route_id": successor["route_id"], "route_hash": successor["route_hash"],
                                "owner_route_id": successor["route_id"], "owner_route_hash": successor["route_hash"],
                                "owner_route_file": str(successor_path), "parent_completion_delivery": "codex-native-queue"}
                        with child.jobs.open("a") as stream:
                            stream.write("now\topen\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
                        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")
                    with mock.patch.object(work_start, "join_selected_attempts", return_value={"state": "timeout", "children": []}), \
                            mock.patch.object(work_start, "parent_next", return_value=("end-turn", "fixture", "")):
                        started = work_start.start_work(successor, successor_path, child.jobs, run=launch)
                        repeated = work_start.start_work(successor, successor_path, child.jobs, run=launch)
                    self.assertTrue(started["owner_started"], started)
                    self.assertTrue(repeated["owner_started"], repeated)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0][calls[0].index("--adapter") + 1], target)
                    self.assertIsNone(resource.poll())
                finally:
                    child.doCleanups()

    def test_two_owner_pin_handoffs_keep_the_same_cycle_open_across_harness_rotation(self):
        import artifact_identity
        import artifact_producer as PRODUCER
        import route_authority as AUTH

        rotations = (("codex", "claude", "codex"),
                     ("claude", "opencode", "claude"),
                     ("opencode", "codex", "opencode"))
        for rotation in rotations:
            with self.subTest(rotation=rotation):
                fixture = ParentCloseTest()
                fixture.setUp()
                try:
                    initial = ROUTE.compose_route(
                        capability="autopilot-code", capability_mode="debug", shape="staged",
                        graph="execute,test,report", slug="two-hop-pin-handoff", cwd=fixture.base,
                        artifact_root=fixture.artifacts, spec_read="fixture", unassigned=True,
                        dispatch_evidence=fixture.route["dispatch_evidence"], parent_harness="codex",
                        work_request={"text": "Continue the same task through two owner handoffs",
                                      "owner_harness": rotation[0]})
                    path = ROUTE.canonical_route_path(fixture.artifacts, initial["route_id"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(initial))
                    fixture.route, fixture.path = initial, path

                    ids = artifact_identity.IdAllocator()
                    PRODUCER.activate(fixture.artifacts, repository_id=ids.allocate("repository"),
                                      artifact_root_id=ids.allocate("artifact_root"),
                                      w7={"campaign_id": ids.allocate("campaign")})
                    cycle_id = None
                    route_bytes = []
                    for ordinal, harness in enumerate(rotation):
                        owner = fixture.process(f"handoff-owner-{ordinal}")
                        metadata = fixture.row(f"handoff-owner-{ordinal}", process=owner, harness=harness,
                                               capability_mode=fixture.route.get("capability_mode", "dev"))
                        begun = PRODUCER.begin(
                            fixture.artifacts, route_file=fixture.path,
                            capability=fixture.route["capability"],
                            intensity=fixture.route["effective_intensity"],
                            jobs=fixture.jobs, owner_attempt_id=metadata["attempt_id"])
                        if cycle_id is None:
                            cycle_id = begun["cycle_id"]
                        self.assertEqual(begun["cycle_id"], cycle_id)
                        route_bytes.append((fixture.path, fixture.path.read_bytes()))
                        if ordinal == len(rotation) - 1:
                            break

                        target = rotation[ordinal + 1]
                        for pin_target in ("owner", "worker"):
                            AUTH.record_pin_change(
                                fixture.route, target=pin_target,
                                pin={"harness": target, "model": None, "effort": None},
                                by={"harness": "codex", "session_id": "fixture-parent"},
                                source="fixture", tuples=[], candidates=[])
                        outcome = CLOSE.close(fixture.route, fixture.path, jobs=fixture.jobs,
                                              summary=work_start.PIN_HANDOFF_SUMMARY)
                        self.assertEqual(outcome["state"], "cancelled", outcome)
                        proof = CLOSE.verified_owner_pin_handoff_close(fixture.route, jobs=fixture.jobs)
                        self.assertIsNotNone(proof, {
                            "intent": CLOSE.intent(fixture.route, fixture.jobs),
                            "result": CLOSE.settled_result(fixture.route,
                                WS.WorkflowLedger(fixture.route["route_id"], fixture.route["route_hash"],
                                                  jobs=fixture.jobs)),
                            "owner": CLOSE._rows(fixture.jobs).get(metadata["attempt_id"]),
                            "pins": AUTH.pin_changes(fixture.route),
                        })
                        record = PRODUCER.read_cycle_record(fixture.artifacts, cycle_id)
                        self.assertEqual(record["state"], "open", record)
                        self.assertFalse((Path(begun["cycle_dir"]) / "manifest.json").exists())
                        if owner.poll() is None:
                            owner.terminate()
                            owner.wait(timeout=5)
                        predecessor = fixture.route
                        prepared = work_start.pin_handoff_continuation(
                            fixture.route, fixture.path, fixture.jobs, outcome)
                        self.assertTrue(prepared.get("successor_route"), prepared)
                        successor_path = Path(prepared["successor_route"])
                        successor = ROUTE.verify_route(json.loads(successor_path.read_text()))
                        self.assertEqual(successor["source_route_id"], fixture.route["route_id"])
                        self.assertEqual(successor["reason"],
                                         f"owner-pin-handoff:{metadata['attempt_id']}:{target}")
                        self.assertEqual(AUTH.sealed_pin_harness(successor, worker_type="owner"), target)
                        prepared_proof = CLOSE.verified_owner_pin_handoff_close(
                            predecessor, jobs=fixture.jobs)
                        self.assertEqual((prepared_proof or {}).get("successor", {}).get("route_id"),
                                         successor["route_id"])
                        pin_path = AUTH.pin_changes_path(predecessor)
                        pin_bytes = pin_path.read_bytes()
                        pin_rows = [json.loads(line) for line in pin_bytes.splitlines()]
                        owner_pin = next(row for row in pin_rows if row.get("target") == "owner")
                        owner_pin["route_hash"] = "sha256:" + "0" * 64
                        pin_path.write_text("\n".join(json.dumps(row, sort_keys=True)
                                                       for row in pin_rows) + "\n")
                        self.assertIsNone(CLOSE.verified_owner_pin_handoff_close(
                            predecessor, jobs=fixture.jobs))
                        pin_path.write_bytes(pin_bytes)
                        successor_bytes = successor_path.read_bytes()
                        tampered_successor = json.loads(successor_bytes)
                        tampered_successor["source_route_hash"] = "sha256:" + "0" * 64
                        successor_path.write_text(json.dumps(tampered_successor))
                        self.assertIsNone(CLOSE.verified_owner_pin_handoff_close(
                            predecessor, jobs=fixture.jobs))
                        successor_path.write_bytes(successor_bytes)
                        fixture.route, fixture.path = successor, successor_path

                    record = PRODUCER.read_cycle_record(fixture.artifacts, cycle_id)
                    self.assertEqual(record["state"], "open", record)
                    self.assertFalse((Path(begun["cycle_dir"]) / "manifest.json").exists())
                    for route_path, original in route_bytes:
                        self.assertEqual(route_path.read_bytes(), original)
                finally:
                    fixture.doCleanups()

    def test_pin_change_recorded_by_the_parent_stays_authority_after_later_handover(self):
        """After a seat handover the effective parent pins; the registered parent is gone."""
        import artifact_identity
        import artifact_producer as PRODUCER
        import route_authority as AUTH

        fixture = ParentCloseTest()
        fixture.setUp()
        try:
            ids = artifact_identity.IdAllocator()
            PRODUCER.activate(fixture.artifacts, repository_id=ids.allocate("repository"),
                              artifact_root_id=ids.allocate("artifact_root"),
                              w7={"campaign_id": ids.allocate("campaign")})
            owner = fixture.process("seat-handover-owner")
            metadata = fixture.row("seat-handover-owner", process=owner, harness="codex",
                                   capability_mode=fixture.route.get("capability_mode", "dev"))
            PRODUCER.begin(fixture.artifacts, route_file=fixture.path,
                           capability=fixture.route["capability"],
                           intensity=fixture.route["effective_intensity"],
                           jobs=fixture.jobs, owner_attempt_id=metadata["attempt_id"])
            self.assertNotEqual(metadata.get("parent_sid"), "seat-successor-parent")
            AUTH.record_pin_change(
                fixture.route, target="owner", pin={"harness": "claude", "model": None, "effort": None},
                by={"harness": "claude", "session_id": "seat-successor-parent"},
                source="fixture", tuples=[], candidates=[])
            outcome = CLOSE.close(fixture.route, fixture.path, jobs=fixture.jobs,
                                  summary=work_start.PIN_HANDOFF_SUMMARY)
            self.assertEqual(outcome["state"], "cancelled", outcome)
            # The writer proved its authority at record time; a later seat
            # handover is not asked to re-authorize the same recorded pin.
            for owns in (True, False):
                with mock.patch.object(AUTH, "owns", return_value=owns):
                    self.assertIsNotNone(CLOSE.verified_owner_pin_handoff_close(
                        fixture.route, jobs=fixture.jobs))
            pin_path = AUTH.pin_changes_path(fixture.route)
            saved = pin_path.read_bytes()
            try:
                rows = [json.loads(line) for line in saved.splitlines() if line.strip()]
                for row in rows:
                    row["at"] = "2999-01-01T00:00:00Z"
                pin_path.write_text("\n".join(json.dumps(row, sort_keys=True)
                                              for row in rows) + "\n")
                self.assertIsNone(CLOSE.verified_owner_pin_handoff_close(
                    fixture.route, jobs=fixture.jobs))
                rows = [json.loads(line) for line in saved.splitlines() if line.strip()]
                for row in rows:
                    if row.get("target") == "owner":
                        row["previous"] = {"harness": "opencode"}
                pin_path.write_text("\n".join(json.dumps(row, sort_keys=True)
                                              for row in rows) + "\n")
                self.assertIsNone(CLOSE.verified_owner_pin_handoff_close(
                    fixture.route, jobs=fixture.jobs))
            finally:
                pin_path.write_bytes(saved)
        finally:
            fixture.doCleanups()

    def test_pin_handoff_holder_identity_transfer_and_two_hops(self):
        """Identity unless a proven handoff preserved the run and resumes its node."""
        import route_authority as AUTH

        fixture = ParentCloseTest()
        fixture.setUp()
        try:
            initial = ROUTE.compose_route(
                capability="autopilot-code", capability_mode="debug", shape="staged",
                graph="execute,test,report", slug="holder-two-hops", cwd=fixture.base,
                artifact_root=fixture.artifacts, spec_read="fixture", unassigned=True,
                dispatch_evidence=fixture.route["dispatch_evidence"], parent_harness="codex",
                work_request={"text": "Hold one preserved run across two handoffs",
                              "owner_harness": "codex"})
            path = ROUTE.canonical_route_path(fixture.artifacts, initial["route_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(initial))
            fixture.route, fixture.path = initial, path
            node = "execute"
            run_id = "fixture-preserved-run"
            self.assertFalse(CLOSE.pin_handoff_holder(
                str(path), initial["route_id"], initial["route_hash"], node, run_id,
                jobs=fixture.jobs)["inherited"])
            self.assertFalse(CLOSE.pin_handoff_holder(
                str(path), initial["route_id"], "sha256:" + "0" * 64, node, run_id,
                jobs=fixture.jobs)["inherited"])

            prepared = {}
            for ordinal, (launch, target) in enumerate((("codex", "claude"), ("claude", "codex"))):
                owner = fixture.row(f"holder-owner-{ordinal}", harness=launch,
                                    capability_mode="debug")
                AUTH.record_pin_change(
                    fixture.route, target="owner",
                    pin={"harness": target, "model": None, "effort": None},
                    by={"harness": target, "session_id": "fixture-parent"},
                    source="fixture", tuples=[], candidates=[])
                outcome = CLOSE.close(fixture.route, fixture.path, jobs=fixture.jobs,
                                      summary=work_start.PIN_HANDOFF_SUMMARY)
                self.assertEqual(outcome["state"], "cancelled", outcome)
                if ordinal == 0:
                    self.assertFalse(CLOSE.pin_handoff_holder(
                        str(fixture.path), fixture.route["route_id"],
                        fixture.route["route_hash"], node, run_id,
                        jobs=fixture.jobs)["inherited"],
                        "a handoff that preserved no such run never moves it")
                outcome_path = fixture.path.with_suffix(".outcome.json")
                document = json.loads(outcome_path.read_text())
                document["resources"] = [{"kind": "resource", "run_id": run_id,
                                          "state": "working", "preserved": True}]
                outcome_path.write_text(json.dumps(document))
                prepared = work_start.pin_handoff_continuation(
                    fixture.route, fixture.path, fixture.jobs, outcome)
                self.assertTrue(prepared.get("successor_route"), prepared)
                holder = CLOSE.pin_handoff_holder(
                    str(fixture.path), fixture.route["route_id"], fixture.route["route_hash"],
                    node, run_id, jobs=fixture.jobs)
                self.assertTrue(holder["inherited"], holder)
                self.assertEqual(holder["route_id"], Path(prepared["successor_route"]).stem)
                self.assertEqual(holder["origin"], {
                    "route_file": str(fixture.path),
                    "route_id": fixture.route["route_id"],
                    "route_hash": fixture.route["route_hash"]})
                self.assertEqual(len(holder["chain"]), 1)
                self.assertFalse(CLOSE.pin_handoff_holder(
                    str(fixture.path), fixture.route["route_id"], fixture.route["route_hash"],
                    "plan", run_id, jobs=fixture.jobs)["inherited"])
                self.assertFalse(CLOSE.pin_handoff_holder(
                    str(fixture.path), fixture.route["route_id"], fixture.route["route_hash"],
                    node, "other-run", jobs=fixture.jobs)["inherited"])
                successor_path = Path(prepared["successor_route"])
                fixture.route = ROUTE.verify_route(json.loads(successor_path.read_text()))
                fixture.route.pop("_registry_current", None)
                fixture.path = successor_path

            final = CLOSE.pin_handoff_holder(
                str(path), initial["route_id"], initial["route_hash"], node, run_id,
                jobs=fixture.jobs)
            self.assertEqual((final["route_id"], final["route_hash"]),
                             (fixture.route["route_id"], fixture.route["route_hash"]))
            self.assertEqual(final["origin"]["route_id"], initial["route_id"])
            self.assertEqual(len(final["chain"]), 2)
            self.assertFalse(CLOSE.pin_handoff_holder(
                str(fixture.path), fixture.route["route_id"], fixture.route["route_hash"],
                node, run_id, jobs=fixture.jobs)["inherited"])
        finally:
            fixture.doCleanups()

    def test_owner_pin_handoff_summary_without_recorded_pin_move_is_not_authority(self):
        import artifact_identity
        import artifact_producer as PRODUCER

        fixture = ParentCloseTest()
        fixture.setUp()
        try:
            ids = artifact_identity.IdAllocator()
            PRODUCER.activate(fixture.artifacts, repository_id=ids.allocate("repository"),
                              artifact_root_id=ids.allocate("artifact_root"),
                              w7={"campaign_id": ids.allocate("campaign")})
            process = fixture.process("spoofed-handoff-owner")
            fixture.row("spoofed-handoff-owner", process=process, harness="codex")
            begun = PRODUCER.begin(fixture.artifacts, route_file=fixture.path,
                capability=fixture.route["capability"], intensity=fixture.route["effective_intensity"],
                jobs=fixture.jobs)
            payload = Path(begun["cycle_dir"]) / "artifacts" / "plans" / "handoff-summary-fixture.md"
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_text("ordinary close remains abandoned\n", encoding="utf-8")
            outcome = CLOSE.close(fixture.route, fixture.path, jobs=fixture.jobs,
                                  summary=work_start.PIN_HANDOFF_SUMMARY)
            self.assertEqual(outcome["state"], "cancelled", outcome)
            record = PRODUCER.read_cycle_record(fixture.artifacts, begun["cycle_id"])
            self.assertEqual(PRODUCER._published_cycle_state(fixture.artifacts, record), "abandoned")
            document = json.loads((Path(begun["cycle_dir"]) / "manifest.json").read_text())
            abandoned = next(event for event in document["events"]
                             if event["event_type"] == "cycle.abandoned")
            self.assertEqual(abandoned["payload"]["abandon_reason"], "operator-decision")
            self.assertIsNone(CLOSE.verified_owner_pin_handoff_close(fixture.route, jobs=fixture.jobs))
        finally:
            fixture.doCleanups()

    def _prepare_historical_two_hop_fixture(self):
        import artifact_identity
        import artifact_manifest as MANIFEST
        import artifact_producer as PRODUCER
        import dispatch_terminal_commit as TERMINAL
        import route_authority as AUTH
        import route_identity as ROUTE_IDENTITY
        from dispatch_completion_join import exact_attempt_row

        fixture = ParentCloseTest()
        fixture.setUp()
        try:
            rotation = ("codex", "claude", "codex")
            route = ROUTE.compose_route(
                capability="autopilot-spec", capability_mode="update", shape="staged",
                graph="review,prd-transaction", slug="historical-owner-pin-handoff",
                cwd=fixture.base, artifact_root=fixture.artifacts, spec_read="fixture", unassigned=True,
                dispatch_evidence=fixture.route["dispatch_evidence"], parent_harness="codex",
                work_request={"text": "Complete the same spec cycle after two owner handoffs",
                              "owner_harness": rotation[0]})
            # These sealed digests and release split come from the real target's
            # v3.47.0 route basis and v3.48.0 owner launch. Keep only compact
            # provenance in this hermetic fixture; never depend on an installed
            # release tree at test time.
            old_registry_digest = "sha256:67471577db7a8f65d3c1ae24df920d33031882ae214c938db8d1a628b26bfc2a"
            old_capability_digest = "sha256:bd146b5b0abf97788e9695cf9c70d5f956677d837712688c9fad811035495643"
            old_unit_digest = "sha256:bbdaa2cef3e5759be7d71772c7baebe08d69e575d967973280a0ba9ad6760b5a"
            historical_release_root = fixture.base / "sealed-release-v3.47.0"
            historical_release_root.mkdir()
            (historical_release_root / "release-provenance.json").write_text(json.dumps({
                "version": "v3.47.0", "registry_digest": old_registry_digest,
                "capability_registry_digest": old_capability_digest,
                "unit_catalog_digest": old_unit_digest,
                "unit_count": 34,
                "topology_recipe_count": 29,
                "route_schema_version": route["schema_version"],
            }), encoding="utf-8")
            owner_launch_root = fixture.base / "owner-launch-v3.48.0"
            owner_launch_root.mkdir()
            current_registry_digest = ROUTE.TOPO.registry_digest(ROUTE.TOPO.load_registry())
            current_unit_digest = ROUTE.unit_catalog_digest()
            self.assertNotEqual(old_registry_digest, current_registry_digest)
            self.assertEqual(old_unit_digest, current_unit_digest)
            route["validation_basis"] = {
                "basis_version": 1,
                "registry_root": str(historical_release_root),
                "unit_catalog_root": str(historical_release_root),
                "runtime_root": str(historical_release_root),
                "runtime_root_validated": True,
                "runtime_root_match": True,
            }
            route["registry_digest"] = old_registry_digest
            route["capability_registry_digest"] = old_capability_digest
            route["unit_catalog_digest"] = old_unit_digest
            route["route_hash"] = ROUTE_IDENTITY.route_hash(route)
            route["route_id"] = ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
            route_file = ROUTE.canonical_route_path(fixture.artifacts, route["route_id"])
            route_file.parent.mkdir(parents=True, exist_ok=True)
            route_file.write_text(json.dumps(route))
            fixture.route, fixture.path = route, route_file
            ids = artifact_identity.IdAllocator()
            PRODUCER.activate(fixture.artifacts, repository_id=ids.allocate("repository"),
                              artifact_root_id=ids.allocate("artifact_root"),
                              w7={"campaign_id": ids.allocate("campaign")})

            cycle_id = None
            cycle_dir = None
            outcomes = []
            route_bytes = []
            for ordinal, harness in enumerate(rotation[:2]):
                current_route_bytes = fixture.path.read_bytes()
                process = fixture.process(f"historical-owner-{ordinal}")
                metadata = fixture.row(
                    f"historical-owner-{ordinal}", process=process, harness=harness,
                    capability="autopilot-spec", capability_mode="update", intensity="standard")
                begun = PRODUCER.begin(
                    fixture.artifacts, route_file=fixture.path, capability="autopilot-spec",
                    intensity="standard", jobs=fixture.jobs, owner_attempt_id=metadata["attempt_id"])
                cycle_id = cycle_id or begun["cycle_id"]
                cycle_dir = Path(begun["cycle_dir"])
                self.assertEqual(begun["cycle_id"], cycle_id)
                if ordinal == 1:
                    self.assertEqual(fixture.path.read_bytes(), current_route_bytes,
                                     "owner begin changed the immutable continuation route")
                route_bytes.append((fixture.path, current_route_bytes))
                if ordinal == 0:
                    prd = cycle_dir / "artifacts" / "spec" / "prd.md"
                    prd.parent.mkdir(parents=True, exist_ok=True)
                    prd.write_text("# Historical handoff fixture PRD\n", encoding="utf-8")
                    review = cycle_dir / "artifacts" / "spec" / "_internal" / "reviews" / "verdict.md"
                    review.parent.mkdir(parents=True, exist_ok=True)
                    review.write_text("fixture review passed\n", encoding="utf-8")
                    owner_report = cycle_dir / "artifacts" / "spec" / "_internal" / "owner-report.md"
                    owner_report.parent.mkdir(parents=True, exist_ok=True)
                    owner_report.write_text("fixture terminal report\n", encoding="utf-8")

                target = rotation[ordinal + 1]
                for pin_target in ("owner", "worker"):
                    AUTH.record_pin_change(
                        fixture.route, target=pin_target,
                        pin={"harness": target, "model": None, "effort": None},
                        by={"harness": "codex", "session_id": "fixture-parent"},
                        source="fixture", tuples=[], candidates=[])
                outcome = CLOSE.close(fixture.route, fixture.path, jobs=fixture.jobs,
                                      summary=work_start.PIN_HANDOFF_SUMMARY)
                self.assertEqual(outcome["state"], "cancelled", outcome)
                outcomes.append(outcome)
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                if ordinal == 0:
                    prepared = work_start.pin_handoff_continuation(
                        fixture.route, fixture.path, fixture.jobs, outcome)
                    successor_path = Path(prepared["successor_route"])
                    successor_disk = json.loads(successor_path.read_text())
                    self.assertEqual(AUTH.sealed_pin_harness(successor_disk, worker_type="owner"), target,
                                     successor_disk.get("selection_pins"))
                    fixture.route = ROUTE.verify_route(json.loads(successor_path.read_text()))
                    fixture.route.pop("_registry_current", None)
                    fixture.path = successor_path

            # Reopen the current continuation under its cancelled owner only long enough to
            # retain the same cycle; the second close is the historical preimage boundary.
            final_handoff = outcomes[-1]
            self.assertEqual(PRODUCER.read_cycle_record(fixture.artifacts, cycle_id)["state"], "open")
            live_review_lease_path = PRODUCER._review_lease_path(
                fixture.artifacts, cycle_id, "historical-live-review")
            PRODUCER.review_lease_acquire(
                fixture.artifacts, cycle_id=cycle_id, attempt_id="historical-live-review")
            live_review_lease_bytes = live_review_lease_path.read_bytes()
            PRODUCER.review_lease_release(
                fixture.artifacts, cycle_id=cycle_id, attempt_id="historical-live-review")
            abandoned = PRODUCER.finalize(
                fixture.artifacts, cycle_id=cycle_id, state="abandoned",
                abandon_reason="operator-decision")
            self.assertEqual(abandoned["cycle_state"], "abandoned")
            prior_manifest_path = cycle_dir / "manifest.json"
            prior_manifest_bytes = prior_manifest_path.read_bytes()
            prior_document = json.loads(prior_manifest_bytes)
            from artifact_lifecycle import read_manifest_snapshots
            prior_snapshot_bytes = next(raw for raw, saved in read_manifest_snapshots(
                fixture.artifacts, cycle_id) if saved.get("manifest_revision_id") ==
                prior_document.get("manifest_revision_id"))
            self.assertEqual(prior_document["routes"][0]["route_id"], fixture.route["route_id"])
            self.assertEqual(prior_document["routes"][0]["terminal_marker"], "pending")

            # This is fixture construction for the recorded legacy state: the old close had
            # sealed the second route before publishing its exact third successor.
            # The source routes, outcomes and close journals remain production-generated.
            prepared = work_start.pin_handoff_continuation(
                fixture.route, fixture.path, fixture.jobs, final_handoff)
            final_path = Path(prepared["successor_route"])
            final_route = ROUTE.verify_route(json.loads(final_path.read_text()))
            final_route.pop("_registry_current", None)
            for edge_index, (predecessor_path, _original) in enumerate(route_bytes):
                predecessor = json.loads(predecessor_path.read_text())
                proof = CLOSE.verified_owner_pin_handoff_close(predecessor, jobs=fixture.jobs)
                self.assertIsNotNone(proof, predecessor["route_id"])
                expected_successor = (route_bytes[edge_index + 1][0].stem
                                      if edge_index + 1 < len(route_bytes) else final_route["route_id"])
                self.assertEqual(proof["successor"]["route_id"], expected_successor)
            fixture.route, fixture.path = final_route, final_path
            final_owner_process = fixture.process("historical-final-owner")
            final_owner = fixture.row(
                "historical-final-owner", process=final_owner_process, harness=rotation[2],
                capability="autopilot-spec", capability_mode="update", intensity="standard",
                launch_home=str(owner_launch_root))
            binding = TERMINAL.publish_producer_binding(
                artifact_root=fixture.artifacts, jobs=fixture.jobs, route_file=final_path,
                owner_attempt_id=final_owner["attempt_id"], cycle_id=cycle_id, owner_begin=True)
            self.assertEqual(binding.status, "published")
            route_bytes.append((final_path, final_path.read_bytes()))

            review_node = next(node for node in final_route["nodes"] if node["id"] == "review")
            child = fixture.row(
                "historical-final-review", parent="historical-final-owner", harness="codex",
                route_node=review_node["id"], capability="autopilot-spec", capability_mode="update",
                artifact_root=str(fixture.artifacts), intensity="standard", failure_class="pass",
                launch_outcome="reaped-before-publish")
            ROUTE.complete_node(final_route, review_node, review_node["id"], review,
                                attempt_id=child["attempt_id"], jobs=fixture.jobs)

            native_text = f"artifact: {owner_report}\nverdict: PASS\nblocker: none"
            native_log = fixture.jobs.parent / "historical-owner.jsonl"
            native_rows = {
                "codex": [{"type": "item.completed", "item": {"type": "agent_message", "text": native_text}},
                          {"type": "turn.completed"}],
                "claude": [{"type": "result", "subtype": "success", "is_error": False,
                            "result": native_text}],
                "opencode": [{"type": "text", "sessionID": "ses_fixture",
                              "part": {"type": "text", "text": native_text}},
                             {"type": "step_finish", "sessionID": "ses_fixture",
                              "part": {"type": "step-finish", "reason": "stop"}}],
            }[rotation[2]]
            native_log.write_text("\n".join(json.dumps(row) for row in native_rows) + "\n")
            final_owner_process.terminate()
            final_owner_process.wait(timeout=5)
            lines = fixture.jobs.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                fields = line.split("\t")
                current = DC.parse_registry_metadata(fields[5])
                if current.get("attempt_id") != final_owner["attempt_id"]:
                    continue
                current.update(attempt_schema_version="2", execution_surface="registered-headless",
                    transport="headless", fallback_hop="same-harness-headless", failure_class="pass",
                    note="completed-supervisor", workflow_completion="runtime-v1",
                    launch_outcome="reaped-before-publish", parent_sid="fixture-parent",
                    parent_completion_delivery="codex-managed-gateway", log_file=str(native_log))
                fields[1] = "done"
                fields[5] = ",".join(f"{key}={value}" for key, value in current.items())
                lines[index] = "\t".join(fields)
            fixture.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")

        except BaseException:
            fixture.doCleanups()
            raise
        return SimpleNamespace(**{key: value for key, value in locals().items() if key != "self"})

    def test_historical_two_hop_handoff_finishes_once_and_reuses_shared_admission(self):
        from dispatch_completion_join import exact_attempt_row
        setup = self._prepare_historical_two_hop_fixture()
        fixture = setup.fixture
        PRODUCER, TERMINAL, MANIFEST = setup.PRODUCER, setup.TERMINAL, setup.MANIFEST
        cycle_id, cycle_dir = setup.cycle_id, setup.cycle_dir
        route_bytes, outcomes = setup.route_bytes, setup.outcomes
        prior_manifest_path, prior_manifest_bytes = setup.prior_manifest_path, setup.prior_manifest_bytes
        prior_document, prior_snapshot_bytes = setup.prior_document, setup.prior_snapshot_bytes
        final_path, final_route, final_owner = setup.final_path, setup.final_route, setup.final_owner
        binding, review, owner_report = setup.binding, setup.review, setup.owner_report
        review_node = setup.review_node
        live_review_lease_path = setup.live_review_lease_path
        live_review_lease_bytes = setup.live_review_lease_bytes
        read_manifest_snapshots = setup.read_manifest_snapshots
        try:
            original_finalize = PRODUCER.finalize
            crash_once = {"remaining": True}
            def interrupt_historical_refresh(root, **kwargs):
                if crash_once["remaining"] and kwargs.get("expected_binding") is not None:
                    crash_once["remaining"] = False
                    kwargs["crash_after_manifest"] = True
                return original_finalize(root, **kwargs)
            with mock.patch.object(PRODUCER, "finalize", side_effect=interrupt_historical_refresh):
                interrupted = TERMINAL.settle_owner_completion(
                    fixture.jobs, "done", exact_attempt_row(fixture.jobs, final_owner["attempt_id"]).metadata)
            self.assertEqual(interrupted.result, "recoverable", interrupted)
            self.assertTrue(PRODUCER.journal_path(fixture.artifacts, cycle_id).exists())

            # Probe the real historical completion context while the exact finish is
            # stopped after manifest publication. These checks use the same abandoned
            # snapshot and immutable terminal binding as the production retry below.
            historical_record = PRODUCER.read_cycle_record(fixture.artifacts, cycle_id)
            exact_binding = TERMINAL.load_producer_binding(
                artifact_root=fixture.artifacts, route_id=final_route["route_id"],
                owner_attempt_id=final_owner["attempt_id"]).binding
            def historical_context(expected=exact_binding):
                return PRODUCER._historical_handoff_completion_context(
                    fixture.artifacts, historical_record, prior_document, expected)

            historical = historical_context()
            self.assertIsNotNone(historical, "baseline historical handoff proof must remain valid")
            completion_binding, _sealed_route, historical_outcome, preserved, _gates = historical
            completed_path = cycle_dir / "manifest.json"
            completed_candidate = json.loads(completed_path.read_text())
            self.assertTrue(PRODUCER._evaluate_provisional_completion(
                fixture.artifacts, historical_record, completed_candidate, completion_binding,
                historical_outcome, preserved=preserved, first_completion=True))

            missing_role = json.loads(json.dumps(completed_candidate))
            missing_role["artifacts"] = []
            missing_role["artifact_revisions"] = []
            self.assertFalse(PRODUCER._evaluate_provisional_completion(
                fixture.artifacts, historical_record, missing_role, completion_binding,
                historical_outcome, preserved=preserved, first_completion=True),
                "first historical completion must retain the required-role check")

            primary_revision = next(
                row for row in completed_candidate["artifact_revisions"]
                if (row.get("locator") or {}).get("path", "").endswith("spec/prd.md"))
            primary_payload = cycle_dir / primary_revision["locator"]["path"]
            primary_bytes = primary_payload.read_bytes()
            try:
                primary_payload.write_bytes(primary_bytes + b" changed during historical finish\n")
                self.assertFalse(PRODUCER._evaluate_provisional_completion(
                    fixture.artifacts, historical_record, completed_candidate, completion_binding,
                    historical_outcome, preserved=preserved, first_completion=True),
                    "first historical completion must verify current payload bytes")
            finally:
                primary_payload.write_bytes(primary_bytes)

            foreign_binding = dict(exact_binding)
            foreign_binding["cycle_id"] = "cyc_" + "f" * 32
            self.assertIsNone(historical_context(foreign_binding),
                              "a foreign cycle binding cannot prove this history")
            foreign_binding = dict(exact_binding)
            foreign_binding["owner_attempt_id"] = fixture.aid("foreign-owner")
            self.assertIsNone(historical_context(foreign_binding),
                              "a foreign owner binding cannot prove this history")

            # A canonical, correctly hashed sibling continuation is a real fork
            # candidate; it must make the abandoned history ineligible.
            import route_identity as ROUTE_IDENTITY
            fork = json.loads(final_path.read_text())
            current_pin = fork["selection_pins"]["owner"]["harness"]
            fork["selection_pins"]["owner"]["harness"] = (
                "opencode" if current_pin != "opencode" else "claude")
            fork["route_hash"] = ROUTE_IDENTITY.route_hash(fork)
            fork["route_id"] = ROUTE_IDENTITY.route_id_from_hash(fork["route_hash"])
            fork_path = ROUTE.canonical_route_path(fixture.artifacts, fork["route_id"])
            self.assertNotEqual(fork_path, final_path)
            fork_path.write_text(json.dumps(fork), encoding="utf-8")
            PRODUCER._ROUTE_EDGES.clear()
            try:
                fork_admission = PRODUCER.cycle_route_admission(
                    fixture.artifacts, historical_record, final_route,
                    finalize=True, validation_only=True)
                self.assertEqual(fork_admission.reason,
                                 "cycle-route-binding-mismatch:lineage-fork")
                self.assertIsNone(historical_context(), "a forked continuation cannot take the cycle")
            finally:
                fork_path.unlink()
                PRODUCER._ROUTE_EDGES.clear()

            # Missing and contradictory parent-close history both reject the
            # same real historical consumer; restore exact journal bytes after each probe.
            predecessor = json.loads(route_bytes[-2][0].read_text())
            close_ledger = WS.WorkflowLedger(
                predecessor["route_id"], predecessor["route_hash"], jobs=fixture.jobs)
            journal_before = close_ledger.journal_path.read_bytes()
            journal_lines = journal_before.decode("utf-8").splitlines(keepends=True)
            missing_result_lines = []
            removed_results = 0
            for line in journal_lines:
                entry = json.loads(line)
                evidence = entry.get("evidence") or {}
                if isinstance(evidence.get("parent_close_result"), dict):
                    removed_results += 1
                else:
                    missing_result_lines.append(line)
            self.assertGreater(removed_results, 0)
            try:
                close_ledger.journal_path.write_text("".join(missing_result_lines), encoding="utf-8")
                self.assertIsNone(historical_context(), "missing close result history must fail closed")
            finally:
                close_ledger.journal_path.write_bytes(journal_before)

            contradictory_lines = []
            changed_results = 0
            for line in journal_lines:
                entry = json.loads(line)
                evidence = entry.get("evidence") or {}
                close_result = evidence.get("parent_close_result")
                if isinstance(close_result, dict):
                    close_result["owner_attempt_id"] = fixture.aid("foreign-owner")
                    changed_results += 1
                    line = json.dumps(entry, ensure_ascii=False) + "\n"
                contradictory_lines.append(line)
            self.assertGreater(changed_results, 0)
            try:
                close_ledger.journal_path.write_text("".join(contradictory_lines), encoding="utf-8")
                self.assertIsNone(historical_context(), "contradictory close result history must fail closed")
            finally:
                close_ledger.journal_path.write_bytes(journal_before)

            # The exact owner and children must be quiescent. Exercise both a
            # live child and an unobservable namespace using real registry rows.
            jobs_before = fixture.jobs.read_bytes()
            live_process = fixture.process("historical-live-child")
            fixture.row(
                "historical-live-child", parent="historical-final-owner", process=live_process,
                route_node=review_node["id"], capability="autopilot-spec", capability_mode="update",
                artifact_root=str(fixture.artifacts), intensity="standard")
            try:
                self.assertIsNone(historical_context(), "a live child blocks historical completion")
            finally:
                if live_process.poll() is None:
                    live_process.terminate()
                    live_process.wait(timeout=5)
                fixture.jobs.write_bytes(jobs_before)

            jobs_before = fixture.jobs.read_bytes()
            unknown_child = fixture.terminal_fail_stage(
                parent="historical-final-owner", name="historical-unknown-child",
                status="running", foreground=False, cleanup=False)
            try:
                # Keep this namespace present but unobservable. A host observer
                # may correctly prove the fabricated namespace extinct otherwise.
                with mock.patch.object(DC, "namespace_gone", return_value="present"):
                    self.assertNotEqual(
                        DC.attempt_process_quiescence(unknown_child, terminal_receipt=True).state,
                        "quiescent")
                    self.assertIsNone(historical_context(), "unknown child execution blocks historical completion")
            finally:
                fixture.jobs.write_bytes(jobs_before)

            live_review_lease_path.write_bytes(live_review_lease_bytes)
            try:
                self.assertIsNone(historical_context(), "a live review lease blocks historical completion")
            finally:
                live_review_lease_path.unlink()
            unknown_lease_path = PRODUCER._review_lease_path(
                fixture.artifacts, cycle_id, "historical-unknown-review")
            unknown_lease_path.write_text("{not-json", encoding="utf-8")
            try:
                self.assertIsNone(historical_context(), "an unreadable review lease blocks historical completion")
            finally:
                unknown_lease_path.unlink()

            first = TERMINAL.settle_owner_completion(
                fixture.jobs, "done", exact_attempt_row(fixture.jobs, final_owner["attempt_id"]).metadata)
            self.assertEqual(first.result, "completed", first)
            self.assertEqual(first.shared_publication["status"], "admitted", first.shared_publication)
            completed_bytes = completed_path.read_bytes()
            completed = json.loads(completed_bytes)
            self.assertEqual(completed["cycle"]["state"], "completed")
            self.assertEqual(completed["routes"][:len(prior_document["routes"])], prior_document["routes"])
            self.assertEqual(completed["events"][:len(prior_document["events"])], prior_document["events"])
            self.assertEqual(completed["routes"][-1]["route_id"], final_route["route_id"])
            self.assertTrue(MANIFEST.validate_update(
                completed, preserved=[prior_document], previous=prior_document).ok)
            self.assertNotEqual(prior_manifest_path.read_bytes(), prior_manifest_bytes)
            self.assertIn(prior_snapshot_bytes, [raw for raw, _saved in
                                                read_manifest_snapshots(fixture.artifacts, cycle_id)])
            for route_path, original in route_bytes:
                self.assertEqual(route_path.read_bytes(), original)
            for outcome in outcomes:
                path = Path(outcome["route_file"]).with_suffix(".outcome.json")
                self.assertEqual(json.loads(path.read_text())["state"], "cancelled")

            terminal_state_path = TERMINAL.terminal_slot(
                fixture.artifacts, final_route["route_id"], final_owner["attempt_id"]) / "terminal-commit.json"
            first_terminal = json.loads(terminal_state_path.read_text())
            first_publication = first.shared_publication
            first_event_count = len(completed["events"])
            retry = TERMINAL.settle_owner_completion(
                fixture.jobs, "done", exact_attempt_row(fixture.jobs, final_owner["attempt_id"]).metadata)
            self.assertEqual(retry.result, "completed", retry)
            self.assertEqual(retry.shared_publication["status"], "admitted", retry.shared_publication)
            for identity_key in ("shared_reference_id", "shared_reference_revision_id", "content_digest"):
                self.assertEqual(retry.shared_publication.get(identity_key),
                                 first_publication.get(identity_key), identity_key)
            self.assertEqual(json.loads(terminal_state_path.read_text())["terminal_commit_id"],
                             first_terminal["terminal_commit_id"])
            self.assertEqual(len(json.loads(completed_path.read_text())["events"]), first_event_count)
            self.assertEqual(completed_path.read_bytes(), completed_bytes)
        finally:
            fixture.doCleanups()
    def _seed_historical_terminal_prefix(self, setup):
        import artifact_admission
        import artifact_lifecycle as LIFECYCLE
        import artifact_producer as PRODUCER

        fixture = setup.fixture
        root = fixture.artifacts
        record = PRODUCER.read_cycle_record(root, setup.cycle_id)
        manifest_path = setup.cycle_dir / "manifest.json"
        raw = manifest_path.read_bytes()
        document = json.loads(raw)
        leaf = PRODUCER._finalize_route(root, record)
        self.assertEqual(leaf["route_id"], setup.final_route["route_id"])
        self.assertTrue(PRODUCER.route_is_closed(root, leaf))
        identity = LIFECYCLE.read_root_identity(root)
        binding, sealed_route = LIFECYCLE.bind_existing_runtime_route(
            root, ROUTE.canonical_route_path(root, leaf["route_id"]),
            expected_root_id=identity.artifact_root_id)
        gates = LIFECYCLE._load_capability_route().terminal_gate_observation(
            sealed_route, jobs=fixture.jobs, exact_terminal=True)
        self.assertTrue(ROUTE.terminal_gate_proven(gates))

        allocator = setup.PRODUCER.artifact_identity.IdAllocator()
        revision_id = allocator.allocate("manifest_revision")
        excluded, excluded_symlinks = [], []
        source_facts = PRODUCER._scan_cycle_facts(
            setup.cycle_dir, excluded=excluded, excluded_symlinks=excluded_symlinks)
        self.assertEqual(sorted(source_facts), sorted(
            (row["locator"]["path"], row["content_digest"], row["byte_size"])
            for row in document["artifact_revisions"]))
        event = {
            "event_id": allocator.allocate("event"),
            "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1,
            "event_type": "route.terminal.recorded",
            "target_id": setup.cycle_id,
            "actor": {"kind": "system", "id": "capability-route"},
            "recorded_at": PRODUCER._rfc3339(time.time()),
            "provenance": {
                "source_manifest_id": document["manifest_id"],
                "source_revision_id": revision_id,
                "producer_route_id": leaf["route_id"],
                "algorithm_version": PRODUCER.ALGORITHM_VERSION,
                "schema_version": 1,
                "source_digest": PRODUCER._digest(PRODUCER._canonical([
                    [rel, digest] for rel, digest, _size in source_facts
                ])),
            },
            "evidence_ids": [],
            "payload": {},
        }
        candidate = json.loads(json.dumps(document))
        candidate["manifest_revision_id"] = revision_id
        candidate["routes"] = list(candidate.get("routes", [])) + [{
            "artifact_root_id": identity.artifact_root_id,
            "route_id": binding.route_id,
            "route_hash": binding.route_hash,
            "terminal_marker": "pending",
            "terminal_evidence_id": event["event_id"],
        }]
        candidate["events"] = list(candidate.get("events", [])) + [event]
        candidate = LIFECYCLE._derive_terminal_evidence(
            candidate, binding, sealed_route, recorded_gates=gates)
        report = setup.MANIFEST.validate_update(
            candidate, preserved=PRODUCER._earlier_documents(root, setup.cycle_id, candidate),
            previous=document)
        self.assertTrue(report.ok, report.violations)

        new_raw = setup.MANIFEST.canonical_bytes(candidate)
        new_digest = setup.MANIFEST.manifest_digest(candidate)
        fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
        try:
            latest_record = PRODUCER.read_cycle_record(root, setup.cycle_id)
            index = artifact_admission.load_index(root)
            identity = LIFECYCLE.read_root_identity(root)
            self.assertEqual(latest_record["manifest_digest"], setup.MANIFEST.manifest_digest(document))
            LIFECYCLE.preserve_manifest_snapshot(root, setup.cycle_id, raw)
            PRODUCER._write_journal(
                root, setup.cycle_id, state="refreshing", manifest_digest=new_digest,
                previous_manifest_digest=latest_record["manifest_digest"],
                cycle_path=os.path.relpath(str(setup.cycle_dir), str(root)),
                manifest_revision_id=revision_id)
            LIFECYCLE.preserve_manifest_snapshot(root, setup.cycle_id, new_raw)
            PRODUCER._write_atomic(manifest_path, new_raw)
            PRODUCER._commit_sealed(root, latest_record, candidate, new_digest,
                                    now=time.time(), index=index,
                                    previous_digest=latest_record["manifest_digest"])
        finally:
            artifact_admission._release_lock(root, fd)
        return candidate

    def _replace_historical_fixture_preimage(self, setup, document):
        """Pair fixture-only current/snapshot bytes and advance its normal index/record."""
        import artifact_admission
        import artifact_lifecycle as LIFECYCLE
        import artifact_producer as PRODUCER

        root = setup.fixture.artifacts
        manifest_path = setup.cycle_dir / "manifest.json"
        snapshot_path = LIFECYCLE.manifest_snapshot_path(
            root, setup.cycle_id, document["manifest_revision_id"])
        raw = setup.MANIFEST.canonical_bytes(document)
        snapshot_path.write_bytes(raw)
        PRODUCER._write_atomic(manifest_path, raw)
        fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
        try:
            record = PRODUCER.read_cycle_record(root, setup.cycle_id)
            index = artifact_admission.load_index(root)
            digest = setup.MANIFEST.manifest_digest(document)
            PRODUCER._commit_sealed(root, record, document, digest, now=time.time(),
                                    index=index, previous_digest=record["manifest_digest"])
        finally:
            artifact_admission._release_lock(root, fd)
        return PRODUCER.read_cycle_record(root, setup.cycle_id)

    def test_historical_terminal_provenance_digest_is_recomputed_before_finish(self):
        from dispatch_completion_join import exact_attempt_row

        setup = self._prepare_historical_two_hop_fixture()
        fixture = setup.fixture
        PRODUCER, TERMINAL = setup.PRODUCER, setup.TERMINAL
        try:
            owner_row = exact_attempt_row(fixture.jobs, setup.final_owner["attempt_id"])
            request = TERMINAL._completion_request(fixture.jobs, "done", owner_row.metadata)
            interrupted = TERMINAL.settle_terminal_commit(
                request, TERMINAL.TerminalCommitServices(crash_after="close-after"))
            self.assertEqual(interrupted.result, "recoverable", interrupted)
            binding = TERMINAL.load_producer_binding(
                artifact_root=fixture.artifacts, route_id=setup.final_route["route_id"],
                owner_attempt_id=setup.final_owner["attempt_id"]).binding
            preimage = self._seed_historical_terminal_prefix(setup)
            source_facts = PRODUCER._scan_cycle_facts(
                setup.cycle_dir, excluded=[], excluded_symlinks=[])
            valid_digest = PRODUCER._digest(PRODUCER._canonical([
                [rel, digest] for rel, digest, _size in source_facts
            ]))
            terminal = next(event for event in preimage["events"]
                            if event.get("event_type") == "route.terminal.recorded")
            self.assertEqual(terminal["provenance"]["source_digest"], valid_digest)
            self.assertIsNotNone(PRODUCER._historical_handoff_completion_context(
                fixture.artifacts, PRODUCER.read_cycle_record(fixture.artifacts, setup.cycle_id),
                preimage, binding, jobs=fixture.jobs))

            tampered = json.loads(json.dumps(preimage))
            terminal = next(event for event in tampered["events"]
                            if event.get("event_type") == "route.terminal.recorded")
            terminal["provenance"]["source_digest"] = "sha256:" + "f" * 64
            record = self._replace_historical_fixture_preimage(setup, tampered)
            self.assertEqual((record["state"], record["cycle_state"]), ("sealed", "abandoned"))
            tampered_context = PRODUCER._historical_handoff_completion_context(
                fixture.artifacts, record, tampered, binding, jobs=fixture.jobs)

            env = dict(os.environ)
            env["AGENT_DISPATCH_JOBS"] = str(fixture.jobs)
            command = [sys.executable, str(HERE / "dispatch_terminal_commit.py"),
                       "finish", "--jobs", str(fixture.jobs),
                       "--attempt", setup.final_owner["attempt_id"]]
            shared_root = fixture.artifacts / "shared"
            shared_before = sorted(path.relative_to(fixture.artifacts).as_posix()
                                   for path in shared_root.rglob("*")) if shared_root.exists() else []
            result = subprocess.run(command, cwd=HERE.parent, env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertNotEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertIsNone(tampered_context, "paired manifest/snapshot provenance digest must be recomputed")
            current = json.loads((setup.cycle_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(current["cycle"]["state"], "abandoned")
            self.assertEqual(current, tampered)
            self.assertEqual(len([event for event in current["events"]
                                 if event.get("event_type") == "cycle.completed"]), 0)
            self.assertEqual(PRODUCER.read_cycle_record(fixture.artifacts, setup.cycle_id)["manifest_digest"],
                             setup.MANIFEST.manifest_digest(tampered))
            shared_after = sorted(path.relative_to(fixture.artifacts).as_posix()
                                  for path in shared_root.rglob("*")) if shared_root.exists() else []
            self.assertEqual(shared_after, shared_before)

            cases = (
                ("source_manifest_id", PRODUCER.artifact_identity.IdAllocator().allocate("manifest")),
                ("source_revision_id", PRODUCER.artifact_identity.IdAllocator().allocate("manifest_revision")),
                ("producer_route_id", "rt-" + "f" * 16),
                ("algorithm_version", PRODUCER.ALGORITHM_VERSION + "-other"),
                ("schema_version", 2),
            )
            for field, bad_value in cases:
                candidate = json.loads(json.dumps(tampered))
                saved = next(event for event in candidate["events"]
                             if event.get("event_type") == "route.terminal.recorded")
                saved["provenance"][field] = bad_value
                record = self._replace_historical_fixture_preimage(setup, candidate)
                self.assertIsNone(PRODUCER._historical_handoff_completion_context(
                    fixture.artifacts, record, candidate, binding,
                    jobs=fixture.jobs), field)
        finally:
            fixture.doCleanups()

    def test_historical_terminal_prefix_normal_finish_retry_keeps_explicit_jobs(self):
        from dispatch_completion_join import exact_attempt_row

        for first_ambient, retry_ambient in ((False, True), (True, False)):
            with self.subTest(first_ambient=first_ambient, retry_ambient=retry_ambient):
                setup = self._prepare_historical_two_hop_fixture()
                fixture = setup.fixture
                PRODUCER, TERMINAL = setup.PRODUCER, setup.TERMINAL
                try:
                    owner_row = exact_attempt_row(fixture.jobs, setup.final_owner["attempt_id"])
                    request = TERMINAL._completion_request(fixture.jobs, "done", owner_row.metadata)
                    interrupted = TERMINAL.settle_terminal_commit(
                        request, TERMINAL.TerminalCommitServices(crash_after="close-after"))
                    self.assertEqual(interrupted.result, "recoverable", interrupted)
                    terminal_path = TERMINAL._commit_state_path(request)
                    terminal_before = json.loads(terminal_path.read_text(encoding="utf-8"))
                    self.assertEqual(terminal_before["state"], "route-closed")

                    # Assemble the recorded B preimage from the exact closed route
                    # and outcome, then publish it with the common snapshot/index writer.
                    expected_binding = TERMINAL.load_producer_binding(
                        artifact_root=fixture.artifacts, route_id=setup.final_route["route_id"],
                        owner_attempt_id=setup.final_owner["attempt_id"]).binding
                    binding_path = TERMINAL.producer_binding_path(
                        fixture.artifacts, setup.final_route["route_id"], setup.final_owner["attempt_id"])
                    binding_bytes_before = binding_path.read_bytes()
                    self.assertEqual(expected_binding["route_id"], setup.final_route["route_id"])
                    self.assertEqual(expected_binding["cycle_id"], setup.cycle_id)
                    self.assertTrue(PRODUCER._binding_route_unrecorded(
                        setup.cycle_dir / "manifest.json", expected_binding))
                    preimage = self._seed_historical_terminal_prefix(setup)
                    record = PRODUCER.read_cycle_record(fixture.artifacts, setup.cycle_id)
                    self.assertEqual((record["state"], record["cycle_state"]), ("sealed", "abandoned"))
                    manifest_path = setup.cycle_dir / "manifest.json"
                    preimage_bytes = manifest_path.read_bytes()
                    self.assertEqual(preimage["cycle"]["state"], "abandoned")
                    self.assertEqual(len(preimage["routes"]), 2)
                    self.assertEqual(preimage["routes"][0], setup.prior_document["routes"][0])
                    leaf = preimage["routes"][-1]
                    self.assertEqual(leaf["route_id"], setup.final_route["route_id"])
                    self.assertTrue(leaf["terminal_evidence_id"])
                    terminal_events = [event for event in preimage["events"]
                                       if event.get("event_type") == "route.terminal.recorded"]
                    self.assertEqual(len(terminal_events), 1)
                    self.assertEqual(leaf["terminal_evidence_id"], terminal_events[0]["event_id"])
                    self.assertEqual(preimage["events"][:-1], setup.prior_document["events"])
                    self.assertEqual(terminal_events[0]["payload"]["route_id"], setup.final_route["route_id"])
                    self.assertEqual(terminal_events[0]["payload"]["route_hash"], setup.final_route["route_hash"])
                    preimage_snapshot_count = len(
                        setup.read_manifest_snapshots(fixture.artifacts, setup.cycle_id))
                    basis = setup.final_route["validation_basis"]
                    self.assertEqual(Path(basis["runtime_root"]), setup.historical_release_root)
                    self.assertNotEqual(Path(basis["runtime_root"]), HERE.parent.resolve())
                    self.assertEqual(Path(owner_row.metadata["launch_home"]), setup.owner_launch_root)
                    self.assertEqual(owner_row.metadata["owner_route_id"], setup.final_route["route_id"])
                    self.assertEqual(owner_row.metadata["owner_route_hash"], setup.final_route["route_hash"])
                    self.assertIsNotNone(PRODUCER._historical_handoff_completion_context(
                        fixture.artifacts, record, setup.prior_document, expected_binding,
                        jobs=fixture.jobs), "A context must accept the same explicit registry")
                    source_facts = PRODUCER._scan_cycle_facts(
                        setup.cycle_dir, excluded=[], excluded_symlinks=[])
                    historical = PRODUCER._historical_handoff_completion_context(
                        fixture.artifacts, record, preimage, expected_binding, jobs=fixture.jobs)
                    self.assertIsNotNone(historical, "B preimage must pass the full historical verifier")
                    projection = PRODUCER._provisional_completion_projection(
                        fixture.artifacts, record, preimage,
                        source_facts,
                        revision_id=PRODUCER.artifact_identity.IdAllocator().allocate("manifest_revision"),
                        allocator=PRODUCER.artifact_identity.IdAllocator(), now=time.time(),
                        expected_binding=expected_binding, previous_document=preimage, jobs=fixture.jobs)
                    self.assertIsNotNone(projection, "B first-completion projection must preserve the leaf prefix")

                    ambient_jobs = fixture.base / "ambient-dispatch" / "jobs.log"
                    ambient_jobs.parent.mkdir()
                    ambient_jobs.write_bytes(b"")
                    ambient_bytes = ambient_jobs.read_bytes()

                    def run_finish(use_ambient):
                        env = dict(os.environ)
                        if use_ambient:
                            env["AGENT_DISPATCH_JOBS"] = str(ambient_jobs)
                        else:
                            env.pop("AGENT_DISPATCH_JOBS", None)
                        command = [sys.executable, str(HERE / "dispatch_terminal_commit.py"),
                                   "finish", "--jobs", str(fixture.jobs),
                                   "--attempt", setup.final_owner["attempt_id"]]
                        result = subprocess.run(command, cwd=HERE.parent, env=env,
                                                capture_output=True, text=True, timeout=120)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        return json.loads(result.stdout.strip().splitlines()[-1])

                    first = run_finish(first_ambient)
                    self.assertEqual(first["result"], "completed", first)
                    self.assertEqual(first["shared_publication"]["status"], "admitted", first)
                    completed_bytes = manifest_path.read_bytes()
                    completed = json.loads(completed_bytes)
                    self.assertEqual(completed["cycle"]["state"], "completed")
                    self.assertEqual(completed["routes"], preimage["routes"])
                    self.assertEqual(completed["events"][:len(preimage["events"])], preimage["events"])
                    self.assertEqual(completed["events"][len(preimage["events"]):],
                                     [event for event in completed["events"][len(preimage["events"]):]
                                      if event.get("event_type") == "cycle.completed"])
                    self.assertEqual(len(completed["events"]) - len(preimage["events"]), 1)
                    self.assertEqual(len([event for event in completed["events"]
                                         if event.get("event_type") == "route.terminal.recorded"]), 1)
                    self.assertEqual(len(completed["artifact_revisions"]), len(preimage["artifact_revisions"]))
                    self.assertEqual(len(setup.read_manifest_snapshots(fixture.artifacts, setup.cycle_id)),
                                     preimage_snapshot_count + 1)
                    self.assertTrue(setup.MANIFEST.validate_update(
                        completed, preserved=[preimage], previous=preimage).ok)
                    completion_binding, _sealed_route, historical_outcome, preserved, _gates = historical
                    self.assertTrue(PRODUCER._evaluate_provisional_completion(
                        fixture.artifacts, record, completed, completion_binding, historical_outcome,
                        preserved=preserved, first_completion=True))

                    changed_prefix = json.loads(completed_bytes)
                    changed_prefix["routes"][0]["terminal_marker"] = "sha256:" + "f" * 64
                    self.assertFalse(setup.MANIFEST.validate_update(
                        changed_prefix, preserved=[preimage], previous=preimage).ok)
                    changed_event = json.loads(completed_bytes)
                    terminal_event_index = next(
                        index for index, event in enumerate(changed_event["events"])
                        if event.get("event_type") == "route.terminal.recorded")
                    changed_event["events"][terminal_event_index]["payload"]["route_hash"] = "sha256:" + "e" * 64
                    self.assertFalse(setup.MANIFEST.validate_update(
                        changed_event, preserved=[preimage], previous=preimage).ok)
                    duplicate_event = json.loads(completed_bytes)
                    terminal_copy = dict(terminal_events[0])
                    terminal_copy["event_id"] = "evt_" + "f" * 32
                    duplicate_event["events"].append(terminal_copy)
                    self.assertFalse(setup.MANIFEST.validate_update(
                        duplicate_event, preserved=[preimage], previous=preimage).ok)

                    missing_role = json.loads(completed_bytes)
                    missing_role["artifacts"] = []
                    missing_role["artifact_revisions"] = []
                    self.assertFalse(PRODUCER._evaluate_provisional_completion(
                        fixture.artifacts, record, missing_role, completion_binding, historical_outcome,
                        preserved=preserved, first_completion=True))
                    primary_revision = next(
                        row for row in completed["artifact_revisions"]
                        if row["locator"]["path"].endswith("spec/prd.md"))
                    primary_payload = setup.cycle_dir / primary_revision["locator"]["path"]
                    primary_bytes = primary_payload.read_bytes()
                    try:
                        primary_payload.write_bytes(primary_bytes + b" changed during historical finish\n")
                        self.assertFalse(PRODUCER._evaluate_provisional_completion(
                            fixture.artifacts, record, completed, completion_binding, historical_outcome,
                            preserved=preserved, first_completion=True))
                    finally:
                        primary_payload.write_bytes(primary_bytes)
                    self.assertEqual(ambient_jobs.read_bytes(), ambient_bytes)

                    terminal_after = json.loads(terminal_path.read_text(encoding="utf-8"))
                    binding_bytes = binding_path.read_bytes()
                    self.assertEqual(binding_bytes, binding_bytes_before)
                    self.assertEqual(terminal_after["terminal_commit_id"],
                                     terminal_before["terminal_commit_id"])
                    publication = first["shared_publication"]
                    shared_revisions_path = (fixture.artifacts / "shared" / "spec" /
                                             publication["shared_reference_id"] / "revisions")
                    shared_revisions = sorted(path.name for path in shared_revisions_path.iterdir())
                    self.assertEqual(len(shared_revisions), 1)
                    snapshot_count = len(setup.read_manifest_snapshots(fixture.artifacts, setup.cycle_id))
                    first_event_count = len(completed["events"])

                    retry = run_finish(retry_ambient)
                    self.assertEqual(retry["result"], "completed", retry)
                    for key in ("shared_reference_id", "shared_reference_revision_id", "content_digest"):
                        self.assertEqual(retry["shared_publication"].get(key), publication.get(key), key)
                    self.assertEqual(json.loads(terminal_path.read_text(encoding="utf-8"))["terminal_commit_id"],
                                     terminal_after["terminal_commit_id"])
                    self.assertEqual(binding_path.read_bytes(), binding_bytes)
                    self.assertEqual(manifest_path.read_bytes(), completed_bytes)
                    self.assertEqual(len(json.loads(manifest_path.read_text())["events"]), first_event_count)
                    self.assertEqual(len(setup.read_manifest_snapshots(fixture.artifacts, setup.cycle_id)),
                                     snapshot_count)
                    self.assertEqual(sorted(path.name for path in shared_revisions_path.iterdir()), shared_revisions)
                    self.assertEqual(ambient_jobs.read_bytes(), ambient_bytes)
                finally:
                    fixture.doCleanups()

    def test_attached_then_advanced_owner_and_old_children_close_current_binding(self):
        import owner_route_binding as OWNER
        owner = self.process("att-owner")
        self.row("att-owner", process=owner, owner_route_file="", owner_route_id="", owner_route_hash="")
        self.route.update(advance_generation=0, owner_attempt_id=self.aid("att-owner"),
                          route_family_key="fixture-family")
        self.path.write_text(json.dumps(self.route))
        env = {"AGENT_DISPATCH_ATTEMPT_ID": self.aid("att-owner"),
               "AGENT_DISPATCH_WORKER_TYPE": "owner", "AGENT_DISPATCH_DEPTH": "1",
               "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION": "2",
               "AGENT_DISPATCH_EXECUTION_SURFACE": "registered-headless",
               "AGENT_DISPATCH_REGISTERED_WORKER": "1", "AGENT_DISPATCH_OWNER_HARNESS": "codex",
               "AGENT_DISPATCH_PARENT_SESSION_ID": "fixture-parent", "AGENT_DISPATCH_JOBS": str(self.jobs)}
        with mock.patch.object(OWNER.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
            OWNER.publish_owner_route_attachment_from_environment(self.jobs,
                target_route={**self.route, "route_file": str(self.path)}, environ=env)
            old = self.route.copy()
            old_child = self.process("att-old")
            self.row("att-old", parent="att-owner", process=old_child)
            advanced = {**old, "route_id": old["route_id"] + "-next", "route_hash": "sha256:next",
                "advance_generation": 1, "source_route_id": old["route_id"],
                "source_route_hash": old["route_hash"], "source_route_supersession": {
                    "from_route_id": old["route_id"], "from_route_hash": old["route_hash"]}}
            next_path = self.path.with_name(advanced["route_id"] + ".json")
            next_path.write_text(json.dumps(advanced))
            OWNER.publish_owner_route_advance_from_environment(self.jobs,
                source_route={**old, "route_file": str(self.path)},
                target_route={**advanced, "route_file": str(next_path)}, environ=env)
            self.route, self.path = advanced, next_path
            self.row("att-new", parent="att-owner", process=self.process("att-new"),
                route_file=str(next_path), route_node=self.route["nodes"][0]["id"],
                unit="dev/backend", capability="autopilot-code", capability_mode="dev",
                artifact_root=str(self.artifacts), intensity="standard")
            original = self.jobs.read_text()
            value = CLOSE.request(self.route, self.path, jobs=self.jobs)
            self.jobs.write_text(original)  # Interrupted before row annotations.
            old_meta = CLOSE._rows(self.jobs)[self.aid("att-old")][1]
            self.assertTrue(CLOSE.row_requested(old_meta, self.jobs))
            self.assertEqual(CLOSE.recover_attempt(self.jobs, old_meta)["state"], "cancelled")
            self.assertIn(self.aid("att-old"), value["attempts"])
            replay = work_start.start_work(old, Path(old_meta.get("route_file") or self.path), self.jobs)
            self.assertEqual(replay["state"], "cancelled")
            self.assertNotIn("resume_command", replay)
        self.assertIsNotNone(owner.poll())
        self.assertIsNotNone(old_child.poll())

    def test_current_close_ignores_unrelated_incomplete_history_and_route_free_owner(self):
        self.row("att-owner")
        for parent in ("fixture-parent", "foreign-parent"):
            self.row("att-history-" + parent, status="done", parent_sid=parent,
                     owner_route_file=str(self.base / "removed-old-route.json"),
                     owner_route_id="", owner_route_hash="")
        self.row("att-route-free", owner_route_file="", owner_route_id="", owner_route_hash="")
        result = self.close()
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["owner_attempt_id"], self.aid("att-owner"))
        self.assertEqual(CLOSE._rows(self.jobs)[self.aid("att-route-free")][0][1], "open")

    def test_row_requested_uses_indexed_owner_ancestry_on_unrelated_history(self):
        import owner_route_binding as OWNER
        import dispatch_registry_cache as CACHE

        owner_process = self.process("att-owner")
        self.row("att-owner", process=owner_process)
        child_process = self.process("att-child")
        child = self.row("att-child", parent="att-owner", process=child_process)
        with self.jobs.open("a", encoding="utf-8") as handle:
            for index in range(4000):
                handle.write(
                    f"now\tdone\t{self.base}\t{self.base}\told-{index}\t"
                    f"attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    f"attempt_id=att-unrelated-{index:05d},parent_attempt_id=att-foreign,"
                    "worker_type=stage,note=history\n")
        CACHE.clear_cache()
        CACHE.reset_metrics()
        with mock.patch.object(OWNER, "_registry_snapshot",
                               side_effect=AssertionError("full registry snapshot")), \
             mock.patch.object(DC, "parse_registry_metadata",
                               side_effect=AssertionError("full metadata reparse")):
            self.assertFalse(CLOSE.row_requested(child, self.jobs))
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["content_reads"], 1)
        self.assertEqual(metrics["full_metadata_parses"], 1)

    def test_current_incomplete_binding_still_refuses_without_intent(self):
        self.row("att-owner", owner_route_id="", owner_route_hash="")
        with self.assertRaisesRegex(ValueError, "current-owner-unobservable.*binding-incomplete"):
            self.close()
        self.assertIsNone(CLOSE.intent(self.route, self.jobs))

    def test_same_group_foreign_resource_and_watcher_survive_both_choices(self):
        for stop in (False, True):
            child = ParentCloseTest(); child.setUp()
            try:
                pidfile = child.base / "branches.json"
                script = ("import os,time,json; ps=[]\n"
                    "for i in range(3):\n"
                    " p=os.fork()\n"
                    " if not p: time.sleep(300); os._exit(0)\n"
                    " ps.append(p)\n"
                    "open(" + repr(str(pidfile)) + ",'w').write(json.dumps(ps))\n"
                    "print('ready',flush=True); time.sleep(300)")
                owner = child.process("att-owner", script=script)
                child.row("att-owner", process=owner)
                target, foreign, watcher = json.loads(pidfile.read_text())
                registry, _ = child.resource(SimpleNamespace(pid=target))
                data = json.loads(registry.read_text())
                data["runs"]["foreign"] = {**RR.proc_identity(foreign), "run_id": "foreign",
                    "process_group": owner.pid, "parent_attempt_id": "att-other", "route": "/other/route",
                    "supervision": RR.proc_identity(watcher), "status": "running"}
                registry.write_text(json.dumps(data))
                result = child.close(stop_resources=stop)
                self.assertEqual(result["state"], "cancelled")
                self.assertEqual(DC.process_observation(target)[0] == "present" and
                    DC.process_observation(target)[2] != "Z", not stop)
                for pid in (foreign, watcher):
                    self.assertEqual(DC.process_observation(pid)[0], "present")
                    self.assertNotEqual(DC.process_observation(pid)[2], "Z")
            finally:
                child.doCleanups()

    def test_wrong_resource_namespace_remains_pending_without_local_pid_signals(self):
        owner = self.process("att-owner"); self.row("att-owner", process=owner)
        resource = self.process("att-owner")
        registry, run = self.resource(resource)
        run["pid_namespace"] = "pid:[foreign-namespace]"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"fixture-run": run}}))
        result = self.close(stop_resources=True)
        self.assertEqual(result["state"], "termination-pending")
        self.assertEqual(result["resources"][0]["state"], "termination-pending")
        self.assertIsNone(resource.poll())
        self.assertIsNotNone(owner.poll())
        self.assertNotIn((resource.pid, run["starttime"]), CLOSE._protected([
            {"kind": "resource", "row": run}]))
        run["pid_namespace"] = os.readlink("/proc/self/ns/pid")
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"fixture-run": run}}))
        self.assertEqual(self.close()["state"], "cancelled")
        self.assertIsNotNone(resource.poll())

    def test_terminal_reaper_preserves_resource_with_unknown_namespace(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner, status="done")
        resource = self.process("att-owner")
        registry, run = self.resource(resource)
        run["pid_namespace"] = "pid:[foreign-namespace]"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"fixture-run": run}}))
        owner.terminate(); owner.wait(timeout=2)
        fields, meta = CLOSE._rows(self.jobs)[self.aid("att-owner")]
        resources = CLOSE.known_resources(self.route, self.path, self.jobs, {self.aid("att-owner")})
        self.assertEqual(CLOSE._agent_processes(meta, resources), ([], False))
        CLOSE.reap_terminal_descendants(self.jobs, fields)
        self.assertIsNone(resource.poll())
        cleanup = DC.resolve_attempt_cleanup(self.jobs, self.aid("att-owner"), apply=True)
        self.assertFalse(cleanup["settled"])
        self.assertIsNone(resource.poll())

    def test_terminal_reaper_refreshes_resource_branches_during_grace(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner, status="done")
        leftover = self.process("att-owner", ignore_term=True)
        gate, pidfile = self.base / "fork-now", self.base / "payload.pid"
        script = ("import os,subprocess,time\n"
            "print('ready',flush=True)\n"
            f"while not os.path.exists({str(gate)!r}): time.sleep(0.005)\n"
            "child=subprocess.Popen(['sleep','300'],start_new_session=True)\n"
            f"open({str(pidfile)!r},'w').write(str(child.pid))\n"
            "time.sleep(300)\n")
        resource = self.process("att-owner", script=script)
        self.resource(resource)
        owner.terminate(); owner.wait(timeout=2)
        fields, _ = CLOSE._rows(self.jobs)[self.aid("att-owner")]
        original = CLOSE._protected_records
        payload = None

        def publish_branch_after_first_scan(resources):
            nonlocal payload
            protected = original(resources)
            if payload is None:
                gate.touch()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if pidfile.exists() and pidfile.read_text().isdigit():
                        payload = int(pidfile.read_text())
                        break
                    time.sleep(0.005)
                self.assertIsNotNone(payload)
                birth = DC.process_start_ticks(payload)
                self.addCleanup(CLOSE._signal, payload, birth, signal.SIGKILL)
            return protected

        with mock.patch.object(CLOSE, "_protected_records", side_effect=publish_branch_after_first_scan):
            CLOSE.reap_terminal_descendants(self.jobs, fields)
        observation = DC.process_observation(payload)
        self.assertEqual(observation[0], "present")
        self.assertNotEqual(observation[2], "Z")
        self.assertIsNone(resource.poll())
        self.assertIsNotNone(leftover.poll())

    def test_escaped_payload_is_durable_after_root_exit_and_observer_restart(self):
        owner = self.process("att-owner"); self.row("att-owner", process=owner)
        pidfile = self.base / "escaped.pid"
        script = ("import os,time,signal; p=os.fork()\n"
            "if not p:\n os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(300); os._exit(0)\n"
            "open(" + repr(str(pidfile)) + ",'w').write(str(p)); print('ready',flush=True); time.sleep(300)")
        resource = self.process("att-owner", script=script)
        registry, run = self.resource(resource)
        payload = int(pidfile.read_text())
        value = CLOSE.request(self.route, self.path, jobs=self.jobs, stop_resources=True)
        self.assertIn(payload, {p for p, _ in value["resource_branches"]["fixture-run"]})
        resource.terminate(); resource.wait(timeout=2)
        registry.unlink()  # The durable intent survives deleted runtime sidecar.
        self.assertEqual(CLOSE.recover_attempt(self.jobs,
            CLOSE._rows(self.jobs)[self.aid("att-owner")][1])["state"], "cancelled")
        self.assertIn(DC.process_observation(payload)[2], (None, "", "Z"))

    def test_payload_born_during_term_grace_is_captured_by_exact_resource_tag(self):
        owner = self.process("att-owner"); self.row("att-owner", process=owner)
        pidfile = self.base / "new-payload.pid"
        script = ("import os,time,signal\n"
            "def term(s,f):\n signal.signal(signal.SIGTERM,signal.SIG_IGN); p=os.fork()\n"
            " if not p: os.setsid(); time.sleep(300); os._exit(0)\n"
            " open(" + repr(str(pidfile)) + ",'w').write(str(p))\n"
            "signal.signal(signal.SIGTERM,term); print('ready',flush=True); time.sleep(300)")
        resource = self.process("att-owner", script=script, env={
            "HEARTING_RESOURCE_RUN_ID": "fixture-run",
            "HEARTING_RESOURCE_REGISTRY": str(self.base / "resources.json")})
        self.resource(resource)
        self.assertEqual(self.close(stop_resources=True)["state"], "cancelled")
        payload = int(pidfile.read_text())
        self.assertIn(DC.process_observation(payload)[2], (None, "", "Z"))
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        value = CLOSE.ledger_intent(self.route, ledger)
        self.assertIn(payload, {p for p, _ in CLOSE._saved_branches(value, ledger)["fixture-run"]})

    def test_pending_cancel_blocks_poll_and_all_direct_gate_mutators(self):
        owner = self.process("att-owner"); self.row("att-owner", process=owner)
        CLOSE.request(self.route, self.path, jobs=self.jobs)
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        with mock.patch.object(SUP, "_evaluate", side_effect=AssertionError("advance")), \
             mock.patch.object(SUP, "read_armed", return_value={}):
            self.assertEqual(SUP.poll_once(self.route, ledger), [{"action": "cancelled"}])
        args = SimpleNamespace(route=str(self.path), jobs=str(self.jobs))
        for command in (SUP.cmd_arm, SUP.cmd_gate, SUP.cmd_release, SUP.cmd_complete):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(command(args), 0)
            self.assertEqual(json.loads(output.getvalue())["reason"], CLOSE.NOTE)
        self.assertIsNone(owner.poll())

    def test_poll_admission_and_close_intent_are_serialized_in_both_orders(self):
        owner = self.process("att-owner"); self.row("att-owner", process=owner)
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        entered, release = threading.Event(), threading.Event()
        def evaluate(*args):
            self.assertIsNone(CLOSE.ledger_intent(self.route, ledger))
            entered.set(); self.assertTrue(release.wait(5))
        armed = {self.route["nodes"][0]["id"]: {"continuation_kind": "supervised"}}
        with mock.patch.object(SUP, "read_armed", return_value=armed), \
             mock.patch.object(SUP, "_evaluate", side_effect=evaluate):
            poll = threading.Thread(target=SUP.poll_once, args=(self.route, ledger)); poll.start()
            self.assertTrue(entered.wait(5))
            close = threading.Thread(target=CLOSE.request, args=(self.route, self.path), kwargs={"jobs": self.jobs})
            close.start(); time.sleep(0.05)
            self.assertIsNone(CLOSE.intent(self.route, self.jobs))
            release.set(); poll.join(5); close.join(5)
            self.assertFalse(poll.is_alive()); self.assertFalse(close.is_alive())
        with mock.patch.object(SUP, "read_armed", return_value={}), \
             mock.patch.object(SUP, "_evaluate", side_effect=AssertionError("advance-after-intent")):
            self.assertEqual(SUP.poll_once(self.route, ledger)[0]["action"], "cancelled")

    def test_pending_completion_defers_existing_receipt_without_finish_or_failure(self):
        self.row("att-owner")
        CLOSE.request(self.route, self.path, jobs=self.jobs)
        with mock.patch.object(CLOSE, "_agent_processes", return_value=([], False)):
            state = JOIN.current_delivery_state(self.jobs, self.aid("att-owner"), parent_attempt_id="fixture-parent")
            self.assertTrue(state.cancellation_requested)
            self.assertFalse(state.cancelled)
            with self.assertRaisesRegex(JOIN.CompletionDeferred, "termination-pending"):
                JOIN.delivery_required_action(state)
        receipt = {"schema_version": 2, "state": "ready", "children": [{
            "attempt_id": self.aid("att-owner"), "status": "done", "reason": "registry-closed",
            "required_action": "inspect-done-failure"}]}
        pending = JOIN.CurrentDeliveryState(None, "", "", "", "done", "CANCELLED", False, 1,
            False, workflow_complete=False, cancellation_requested=True)
        with mock.patch.object(JOIN, "current_delivery_state", return_value=pending):
            with self.assertRaises(JOIN.CompletionDeferred):
                JOIN.receipt_with_delivery_observability(receipt, jobs=self.jobs)
        observed = JOIN.wait_for_delivery_projection(receipt, jobs=self.jobs)
        self.assertEqual(observed["children"][0]["required_action"], "advance-completed")
        self.assertEqual(observed["delivery_classification"], "success")

    def test_completion_projection_propagates_ordinary_errors_without_retry(self):
        with mock.patch.object(JOIN, "receipt_with_delivery_observability",
                side_effect=JOIN.JoinContractError("ordinary-contract-error")) as projection:
            with self.assertRaisesRegex(JOIN.JoinContractError, "ordinary-contract-error"):
                JOIN.wait_for_delivery_projection({}, jobs=self.jobs)
            self.assertEqual(projection.call_count, 1)
        self.jobs.write_text("1\topen\t/repo\t/wt\towner\tattempt_id=att-owner,worker_type=owner\n")
        self.assertFalse(CLOSE.row_requested({"attempt_id": "att-owner", "worker_type": "owner"}, self.jobs))

    def test_pending_resource_defers_all_queue_carriers_then_replays_same_records(self):
        import dispatch_pending_delivery as PD
        # The carrier's conventional registry is entirely inside this fixture.
        self.jobs = self.jobs.with_name("jobs.log")
        self.jobs.touch()
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        owner = self.process("att-owner"); self.row("att-owner", process=owner)
        resource = self.process("att-resource")
        registry, run = self.resource(resource)
        run["pid_namespace"] = "pid:[foreign-namespace]"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"fixture-run": run}}))
        self.assertEqual(self.close(stop_resources=True)["state"], "termination-pending")
        meta = CLOSE._rows(self.jobs)[self.aid("att-owner")][1]
        self.assertEqual(meta["parent_close_settled"], "1")  # Agent alone is already terminal.
        original = {}
        kinds = ("claude-parent-runtime", "codex-native-queue", "opencode-turn")
        for kind in kinds:
            for action in ("advance-completed", "inspect-done-failure"):
                receipt = DC._terminal_delivery_receipt(meta)
                receipt["children"][0].update(required_action=action,
                    delivery_classification="attention" if action.startswith("inspect") else "success")
                receipt["delivery_classification"] = receipt["children"][0]["delivery_classification"]
                did = "delivery-" + hashlib.sha256((kind + action).encode()).hexdigest()[:32]
                original[did] = PD.create(self.jobs.parent, recipient_kind=kind, recipient_key="fixture-parent",
                    delivery_id=did, session_generation="", session_generation_supported="0",
                    attempt_ids=[self.aid("att-owner")], parent_attempt_id="fixture-parent",
                    route_id=self.route["route_id"], route_node="owner", receipt=receipt,
                    receipt_digest=PD._canonical_receipt_digest(receipt), row_revisions={self.aid("att-owner"): "fixture"})

        def carrier(kind, action="deliver", payload=None):
            env = {**os.environ, "HOME": str(self.base), "HARNESS_STATE_ROOT": str(self.base)}
            proc = subprocess.run([sys.executable, str(HERE / "dispatch_session_sweep.py"), action,
                "--recipient-kind", kind, "--session", "fixture-parent"], cwd=self.base, env=env,
                input=json.dumps(payload or {}), capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        for kind in kinds:
            for _ in range(2):
                self.assertEqual(carrier(kind), {"text": "", "records": []})
        for did, record in original.items():
            observed = PD.read(self.jobs.parent, "fixture-parent", did)
            self.assertEqual(observed["state"], "pending")
            self.assertEqual(observed["receipt"], record["receipt"])
            self.assertEqual(observed["receipt_digest"], record["receipt_digest"])

        run["pid_namespace"] = os.readlink("/proc/self/ns/pid")
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"fixture-run": run}}))
        self.assertEqual(self.close()["state"], "cancelled")
        self.assertIsNotNone(resource.poll())
        shutil.rmtree(self.artifacts)  # No artifact retention condition for delivery/replay.
        for kind in kinds:
            delivered = carrier(kind)
            self.assertEqual(len(delivered["records"]), 2)
            self.assertIn("Parent-requested close has settled", delivered["text"])
            self.assertIn("required_action=advance-completed", delivered["text"])
            self.assertNotIn("inspect-done-failure", delivered["text"])
            self.assertNotIn("(no harvest command; advance the route)", delivered["text"])
            self.assertEqual(carrier(kind, "ack", delivered)["count"], 2)
            self.assertEqual(carrier(kind)["records"], [])
        for did, record in original.items():
            observed = PD.read(self.jobs.parent, "fixture-parent", did)
            self.assertEqual(observed["state"], "acked")
            self.assertEqual(observed["receipt"], record["receipt"])
            self.assertEqual(observed["receipt_digest"], record["receipt_digest"])

    def test_direct_successor_started_before_intent_is_stopped_from_existing_claim(self):
        owner = self.process("att-owner"); self.row("att-owner", process=owner)
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        first, successor = self.route["nodes"][0]["id"], self.route["nodes"][1]["id"]
        armed = {"route_id": self.route["route_id"], "successor_cwd": str(self.base),
                 "successor_command": [sys.executable, "-c", "import time; time.sleep(300)", str(self.base)],
                 "successor_log": str(self.base / "successor.log")}
        original_spawn = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original_spawn(*args, **kwargs); self.processes.append(process); return process
        with ledger.lock(), mock.patch.object(SUP.subprocess, "Popen", side_effect=spawn):
            outcome = SUP._claim_successors(self.route, ledger, armed, first, [successor])[0]
        process = self.processes[-1]
        self.assertEqual(outcome["pid"], process.pid)
        result = self.close()
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNotNone(process.poll())
        self.assertIsNotNone(owner.poll())

    def test_three_harnesses_inline_and_parked_owner_close_real_processes(self):
        for harness in ("claude", "codex", "opencode"):
            for parked in (False, True):
                with self.subTest(harness=harness, parked=parked):
                    # Each independent fixture gets its own route and registry.
                    child = ParentCloseTest()
                    child.setUp()
                    try:
                        owner = child.process("att-owner")
                        child.row("att-owner", process=owner, harness=harness)
                        if parked:
                            worker = child.process("att-worker")
                            child.row("att-worker", parent="att-owner", process=worker, harness=harness)
                        result = child.close()
                        self.assertEqual(result["state"], "cancelled")
                        self.assertEqual(owner.wait(timeout=2), -signal.SIGTERM)
                        if parked:
                            self.assertEqual(worker.wait(timeout=2), -signal.SIGTERM)
                        self.assertEqual({r[1]["note"] for r in CLOSE._rows(child.jobs).values()}, {CLOSE.NOTE})
                    finally:
                        child.doCleanups()

    def test_never_started_chain_and_post_nodes_fold_no_start_advice(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        serial = dict(session_chain_id="ssc-fixture", subsession_mode="serial",
                 route_node=self.route["nodes"][0]["id"], stage_authority="0",
                 subsession_count="3", subsession_purpose="planned", expected_round_trips="1",
                 phase_brief=str(self.base / "brief.md"), state_ledger=str(self.base / "state.json"),
                 phase_brief_sha256="a" * 64, fixed_files_sha256="b" * 64, narrow_verify_sha256="c" * 64)
        linked = self.base / "linked-slice"
        linked.mkdir()
        worker = self.process("att-slice")
        self.row("att-slice", parent="att-owner", process=worker, worktree=linked,
                 subsession_id="ss-current", subsession_index="1", **serial)
        self.row("att-next", parent="att-owner", worktree=linked,
                 subsession_id="ss-next", subsession_index="2", **serial)
        result = self.close()
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(worker.wait(timeout=2), -signal.SIGTERM)
        next_row = CLOSE._rows(self.jobs)[self.aid("att-next")]
        self.assertEqual(next_row[1]["launch_outcome"], "never-launched")
        self.assertEqual(POLICY.required_action(next_row[0][1], next_row[1]), "advance-completed")
        with mock.patch.object(work_start, "_advance", side_effect=AssertionError("started")):
            replay = work_start.start_work(self.route, self.path, self.jobs)
        self.assertEqual(replay["state"], "cancelled")
        self.assertNotIn("resume_command", replay)
        self.assertEqual({n["state"] for n in WS.WorkflowLedger(self.route["route_id"],
            self.route["route_hash"], jobs=self.jobs).state()["nodes"].values()}, {"CANCELLED"})

    def test_committed_pass_wins_then_cancel_intent_wins_over_late_pass(self):
        self.row("att-owner", status="done", note="completed-supervisor", failure_class="pass")
        self.assertIsNone(self.close())
        self.jobs.write_text("")
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        value = CLOSE.request(self.route, self.path, jobs=self.jobs)
        self.assertEqual(DC.reconcile_attempt_terminal(self.jobs, self.aid("att-owner"), "completed-supervisor",
            evidence={"failure_class": "pass"}), "cancellation-pending")
        self.assertEqual(CLOSE.continue_close(value, jobs=self.jobs)["state"], "cancelled")
        self.assertEqual(DC.reconcile_attempt_terminal(self.jobs, self.aid("att-owner"), "dead-worker-fail",
            evidence={"failure_class": "fail"}), "already-terminal")
        self.assertNotIn("terminal_conflict", CLOSE._rows(self.jobs)[self.aid("att-owner")][1])

    def test_term_ignored_escalates_only_fixture_pid(self):
        owner = self.process("att-owner", ignore_term=True)
        self.row("att-owner", process=owner)
        self.assertEqual(self.close()["state"], "cancelled")
        self.assertEqual(owner.wait(timeout=2), -signal.SIGKILL)

    def test_unobservable_stays_pending_then_restart_closes_without_artifacts(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        with mock.patch.object(CLOSE, "_agent_processes", return_value=([], False)), \
                mock.patch.object(CLOSE, "_signal") as signal_mock:
            self.assertEqual(self.close()["state"], "termination-pending")
            signal_mock.assert_not_called()
        # A fresh execution runner replays the existing journal; output can be gone.
        payload = self.artifacts / "campaigns"
        payload.mkdir()
        (payload / "report.md").write_text("temporary output")
        shutil.rmtree(payload)
        result = CLOSE.recover_attempt(self.jobs, CLOSE._rows(self.jobs)[self.aid("att-owner")][1])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(self.close(), result)

    def test_pid_reuse_does_not_signal_new_process(self):
        owner = self.process("att-owner")
        meta = self.row("att-owner", process=owner)
        identity = DC.AuthoritativeProcessIdentity("local", owner.pid, meta["pid_start"])
        with mock.patch.object(DC, "attempt_process_quiescence", return_value=DC.ProcessQuiescence(
                "quiescent", "pid-reused", identity=identity)), \
             mock.patch.object(DC, "process_observation", return_value=("present", "different-birth", "S")), \
             mock.patch.object(DC, "attempt_tagged_descendants", return_value=DC.ProcessGroupObservation("empty")), \
             mock.patch.object(CLOSE, "_signal") as signal_mock:
            self.assertEqual(self.close()["state"], "cancelled")
            signal_mock.assert_not_called()
        self.assertIsNone(owner.poll())

    def resource(self, process, aid="att-owner", rid="fixture-run"):
        registry = self.base / "resources.json"
        row = {**RR.proc_identity(process.pid), "run_id": rid, "process_group": os.getpgid(process.pid),
               "route": str(self.path), "node": self.route["nodes"][0]["id"],
               "parent_attempt_id": self.aid(aid), "jobs": str(self.jobs), "status": "running"}
        registry.write_text(json.dumps({"schema_version": 1, "runs": {rid: row}}))
        RR.register_registry(registry)
        return registry, row

    def test_gpu_default_preserved_and_opt_in_stops_only_linked_run(self):
        for stop in (False, True):
            with self.subTest(stop=stop):
                child = ParentCloseTest()
                child.setUp()
                try:
                    owner = child.process("att-owner")
                    gpu = child.process("att-owner")
                    unrelated = child.process("att-foreign")
                    child.row("att-owner", process=owner)
                    child.resource(gpu)
                    result = child.close(stop_resources=stop)
                    gpu.poll()
                    self.assertEqual(result["state"], "cancelled")
                    self.assertEqual(gpu.poll() is None, not stop)
                    self.assertIsNone(unrelated.poll())
                    self.assertEqual(result["resources"][0]["preserved"], not stop)
                finally:
                    child.doCleanups()

    def test_parent_completion_cancel_is_delivery_success_without_failure_prompt(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        self.close()
        state = JOIN.current_delivery_state(self.jobs, self.aid("att-owner"), parent_attempt_id="fixture-parent")
        self.assertEqual(JOIN.delivery_classification(state), "success")
        self.assertEqual(JOIN.delivery_required_action(state), "advance-completed")
        self.assertTrue(state.cancelled)
        self.assertEqual(state.verdict, "CANCELLED")
        receipt = DC._terminal_delivery_receipt(CLOSE._rows(self.jobs)[self.aid("att-owner")][1])
        self.assertEqual(receipt["delivery_classification"], "success")
        self.assertEqual(receipt["children"][0]["reason"], CLOSE.NOTE)
        import human_gate_receipt as HG
        # A cancelled gate is consumed before looking for removed artifacts.
        with self.assertRaisesRegex(HG.HumanGateReceiptError, "route-already-closed"):
            HG._load_route({"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                            "job_registry": str(self.jobs), "route_file": str(self.path)})

    def test_real_cli_close_and_duplicate_close_need_only_existing_command(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        command = [sys.executable, str(HERE / "capability-route.py"), "close", "--route", str(self.path)]
        first = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["state"], "cancelled")
        self.assertNotIn("terminal-gate-unproven", first.stderr)
        replay = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout), json.loads(first.stdout))

    def test_resource_in_owner_group_survives_and_missing_output_does_not_change_replay(self):
        pidfile = self.base / "child.pid"
        script = ("import os,time; p=os.fork(); "
            "open(" + repr(str(pidfile)) + ",'w').write(str(p)) if p else None; "
            "print('ready',flush=True) if p else None; time.sleep(300)")
        owner = self.process("att-owner", script=script)
        resource_pid = int(pidfile.read_text())
        self.row("att-owner", process=owner)
        from types import SimpleNamespace
        registry, run = self.resource(SimpleNamespace(pid=resource_pid))
        self.assertEqual(os.getpgid(resource_pid), owner.pid)
        self.assertEqual(self.close()["state"], "cancelled")
        self.assertEqual(RR.classify_identity(run)[0], "working")
        registry.unlink()
        self.assertEqual(self.close()["state"], "cancelled")
        self.assertEqual(RR.classify_identity(run)[0], "working")

    def test_cancelled_watcher_records_result_without_missing_artifact_or_successor(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        self.close()
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        armed = {"node": "fixture-resource", "predecessor_kind": "resource"}
        evidence = {"terminal": True, "succeeded": True, "identity": "fixture-run:ended"}
        with mock.patch.object(SUP, "read_armed", return_value={"fixture-resource": armed}), \
             mock.patch.object(SUP, "resource_evidence", return_value=evidence), \
             mock.patch.object(SUP, "_start_successor", side_effect=AssertionError("successor")), \
             mock.patch.object(SUP, "artifact_evidence", side_effect=AssertionError("artifact")):
            self.assertEqual(SUP.poll_once(self.route, ledger)[0]["action"], "cancelled-result")
            SUP.poll_once(self.route, ledger)
        results = [e for e in ledger.journal() if (e.get("evidence") or {}).get("cancelled_resource_result")]
        self.assertEqual(len(results), 1)
        self.assertEqual(ledger.state()["workflow_state"], "CANCELLED")

    def test_other_parent_cannot_close_and_done_child_is_drained_without_changing_pass(self):
        owner = self.process("att-owner")
        worker = self.process("att-worker")
        self.row("att-owner", process=owner)
        self.row("att-worker", parent="att-owner", process=worker, status="done",
                 note="completed-supervisor", failure_class="pass")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "foreign-parent"}):
            with self.assertRaisesRegex(ValueError, "parent-close-owner-not-owned"):
                self.close()
        self.assertIsNone(owner.poll())
        self.assertEqual(self.close()["state"], "cancelled")
        self.assertEqual(worker.wait(timeout=2), -signal.SIGTERM)
        self.assertEqual(CLOSE._rows(self.jobs)[self.aid("att-worker")][1]["failure_class"], "pass")

    def test_confirmed_handover_refuses_old_parent_close_and_allows_successor(self):
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        owner = self.process("att-owner")
        meta = self.row("att-owner", process=owner)
        seat = tidy.Seat("pane", "fixture-successor-seat", "w8:pSuccessor")
        binding = handover.binding_of(self.jobs, meta)
        tidy.atomic_write_json(handover._snapshot_path(seat.key), {
            "schema": handover.SCHEMA,
            "seat": {"kind": "pane", "key": seat.key, "pane": seat.pane},
            "from": {"sid": meta["parent_sid"]}, "bindings": [binding],
        })
        ledger = tidy._ledger_path(seat)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({"event": "handover", "from": meta["parent_sid"],
            "sid": "successor", "harness": "codex", "ts": time.time(),
            "bindings": [binding]}) + "\n")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": meta["parent_sid"]}):
            with self.assertRaisesRegex(ValueError, "parent-close-owner-not-owned"):
                self.close()
        self.assertIsNone(owner.poll())
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "successor"}):
            self.assertEqual(self.close()["state"], "cancelled")

    def test_crash_after_journal_intent_fences_late_pass_and_launch(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        self.row("att-next", parent="att-owner")
        original = self.jobs.read_text()
        CLOSE.request(self.route, self.path, jobs=self.jobs)
        self.jobs.write_text(original)  # writer died after intent, before annotations
        self.assertEqual(DC.reconcile_attempt_terminal(self.jobs, self.aid("att-owner"),
            "completed-supervisor", evidence={"failure_class": "pass"}), "cancellation-pending")
        with mock.patch.object(CLOSE.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(DC.DispatchContractError, "cancelled-by-parent"):
                DC.spawn_claimed_attempt(self.jobs, self.aid("att-next"), parent_binding=None, spawn=spawn)
            spawn.assert_not_called()
        self.assertEqual(work_start.start_work(self.route, self.path, self.jobs)["state"], "cancelled")

    def test_unstarted_cancellation_is_uncounted_but_cannot_register_a_new_successor(self):
        import review_round_cap as CAP
        fallback = load("parent_close_no_start_fallback", "stage-dispatch-fallback.py")
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        original = self.row("att-gap", parent="att-owner", route_node="execute")
        CLOSE.request(self.route, self.path, jobs=self.jobs)
        self.assertTrue(DC.close_attempt_row_if(
            self.jobs, original["attempt_id"], CLOSE.NOTE, DC.attempt_row_never_started,
            evidence={"failure_class": "cancelled", "parent_close_settled": "1",
                      "launch_outcome": "never-launched"}))
        fields, metadata = CLOSE._rows(self.jobs)[original["attempt_id"]]
        self.assertTrue(DC.attempt_row_terminal_never_started(fields))
        self.assertEqual(CAP.logical_round_records([(fields, metadata)]), [])
        successor = fallback.retry_proven_unstarted_identity(
            self.jobs, self.route["route_id"], "execute", original["attempt_id"])
        self.assertNotEqual(successor, original["attempt_id"])
        candidate = {**original, "attempt_id": successor}
        row = "\t".join(["now", "open", str(self.base), str(self.base), "gap-successor",
                         ",".join(f"{key}={value}" for key, value in candidate.items())])
        before = self.jobs.read_bytes()
        with self.assertRaisesRegex(DC.DispatchContractError, "cancelled-by-parent"):
            DC.claim_attempt_row(self.jobs, successor, row, launch=True)
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertIsNone(owner.poll())  # Authority wins even before the owner stops.

    def test_before_first_dispatch_no_registry_means_no_close_intent(self):
        self.jobs.unlink()
        self.assertIsNone(CLOSE.intent(self.route, self.jobs))
        self.assertIsNone(self.close())
        self.assertFalse((self.jobs.parent / "workflow").exists())

    def test_simultaneous_pass_and_cancel_have_one_committed_outcome(self):
        for _ in range(6):
            child = ParentCloseTest()
            child.setUp()
            try:
                child.row("att-owner")
                barrier = threading.Barrier(2)
                results, errors = {}, []
                def run(name, function):
                    try:
                        barrier.wait(timeout=5)
                        results[name] = function()
                    except BaseException as exc:
                        errors.append(exc)
                threads = [threading.Thread(target=run, args=("pass", lambda:
                    DC.reconcile_attempt_terminal(child.jobs, child.aid("att-owner"),
                        "completed-supervisor", evidence={"failure_class": "pass"}))),
                    threading.Thread(target=run, args=("close", child.close))]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                status, meta = CLOSE._rows(child.jobs)[child.aid("att-owner")]
                outcome = POLICY.committed_outcome(status[1], meta)
                self.assertIn(outcome, {"succeeded", "cancelled"})
                self.assertEqual(results["close"] is None, outcome == "succeeded")
                self.assertNotIn("terminal_conflict", meta)
            finally:
                child.doCleanups()

    def test_stop_resources_selects_shared_group_branch_and_leaves_foreign_run(self):
        pidfile = self.base / "resource.pid"
        script = ("import os,time,signal; p=os.fork(); "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN) if not p else None; "
            "open(" + repr(str(pidfile)) + ",'w').write(str(p)) if p else None; "
            "print('ready',flush=True) if p else None; time.sleep(300)")
        owner = self.process("att-owner", script=script)
        self.row("att-owner", process=owner)
        from types import SimpleNamespace
        resource_pid = int(pidfile.read_text())
        _, run = self.resource(SimpleNamespace(pid=resource_pid))
        foreign = self.process("att-foreign")
        result = self.close(stop_resources=True)
        self.assertEqual(result["state"], "cancelled")
        self.assertNotEqual(RR.classify_identity(run)[0], "working")
        self.assertIsNone(foreign.poll())

    def test_close_finalizes_existing_cycle_and_deleted_output_is_replayable(self):
        import artifact_producer as AP
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        begun = AP.begin(self.artifacts, route_file=self.path, capability="autopilot-code", intensity="standard")
        output = Path(begun["cycle_dir"]) / "artifacts/plans/fixture/REPORT.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("fixture output")
        result = self.close()
        self.assertEqual(result["state"], "cancelled")
        record = AP.read_cycle_record(self.artifacts, begun["cycle_id"])
        self.assertEqual(record["state"], "sealed")
        self.assertEqual(AP._published_cycle_state(self.artifacts, record), "abandoned")
        self.assertEqual(output.read_text(), "fixture output")
        shutil.rmtree(Path(begun["cycle_dir"]))
        self.path.with_suffix(".outcome.json").unlink()
        with mock.patch.object(CLOSE, "_agent_processes", side_effect=AssertionError("already settled")):
            self.assertEqual(self.close(), result)

    def test_simultaneous_close_reuses_one_cancelled_result(self):
        self.row("att-owner")
        barrier = threading.Barrier(2)
        results, errors = [], []
        def close():
            try:
                barrier.wait(timeout=5)
                results.append(self.close())
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=close) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        self.assertEqual(sum("parent_close_result" in (e.get("evidence") or {}) for e in ledger.journal()), 1)

    def test_compute_stop_uses_exact_route_attempt_and_original_configuration(self):
        from types import SimpleNamespace
        self.row("att-owner")
        run_root = self.base / "compute-runs"
        for rid, aid, path in (("own-run", self.aid("att-owner"), str(self.path)),
                               ("foreign-run", "foreign-attempt", str(self.path)),
                               ("other-route", self.aid("att-owner"), "/foreign/route.json")):
            directory = run_root / rid
            directory.mkdir(parents=True)
            (directory / "meta.json").write_text(json.dumps({"provenance": {
                "attempt_id": aid, "route": {"route_id": self.route["route_id"], "route_file": path}}}))
        stopped = []
        compute = SimpleNamespace(ConfigError=ValueError, load_config=lambda: {"run_root": run_root},
            _namespace_self=lambda config: None,
            config_path=lambda: self.base / "original-config.yaml", _run_state=lambda config, rid:
                {"stop_reason": "parent" if rid in stopped else None,
                 "state": "finished" if rid in stopped else "running"})
        actual_run = subprocess.run
        def stop(command, **kwargs):
            if len(command) < 2 or Path(command[1]).name != "compute-hosts.py":
                return actual_run(command, **kwargs)
            self.assertEqual(command[-2:], ["stop", "own-run"])
            self.assertEqual(kwargs["env"]["COMPUTE_HOSTS_CONFIG"], str(self.base / "original-config.yaml"))
            stopped.append(command[-1])
            return SimpleNamespace(returncode=0)
        with mock.patch.object(CLOSE, "_compute", return_value=compute), \
             mock.patch.object(CLOSE.subprocess, "run", side_effect=stop):
            result = self.close(stop_resources=True)
        self.assertEqual(stopped, ["own-run"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual([r["run_id"] for r in result["resources"]], ["own-run"])

    def test_linked_resources_skips_run_root_on_namespace_self_host(self):
        from types import SimpleNamespace
        run_root = self.base / "compute-runs"
        run_root.mkdir()
        run_id = "linked-run"
        run_dir = run_root / run_id
        run_dir.mkdir()
        (run_dir / "meta.json").write_text(json.dumps({"provenance": {
            "attempt_id": self.aid("att-owner"),
            "route": {"route_id": self.route["route_id"], "route_file": str(self.path.resolve())},
            "node": self.route["nodes"][0]["id"]}}))
        compute = CLOSE._compute()
        compute.config_path = lambda: self.base / "compute-hosts.yaml"
        compute._run_state = lambda config, rid: {"stop_reason": None, "state": "running"}

        class UnreadableRunRoot:
            def glob(self, pattern):
                raise AssertionError("namespace self host must not read run_root")

        self_config = {"run_root": UnreadableRunRoot(), "hosts": {
            "local": {"ssh_host": "local", "path_namespace": {"/mnt/shared": "/mnt/shared"}}}}
        compute.load_config = lambda: self_config
        with mock.patch.object(CLOSE, "_compute", return_value=compute):
            self.assertEqual(CLOSE.linked_resources(self.route, self.path, self.jobs,
                {self.aid("att-owner")}), [])

        normal_config = {"run_root": run_root, "hosts": {
            "remote": {"ssh_host": "compute.example"}}}
        compute.load_config = lambda: normal_config
        with mock.patch.object(CLOSE, "_compute", return_value=compute):
            resources = CLOSE.linked_resources(self.route, self.path, self.jobs,
                {self.aid("att-owner")})
        self.assertEqual([resource["run_id"] for resource in resources], [run_id])

    def test_existing_orphan_observer_finishes_interrupted_close_without_parent_recovery(self):
        owner = self.process("att-owner")
        metadata = self.row("att-owner", process=owner)
        with mock.patch.object(CLOSE, "_agent_processes", return_value=([], False)):
            self.assertEqual(self.close()["state"], "termination-pending")
        observer = subprocess.Popen([sys.executable, str(HERE / "dispatch-orphan-watch.py"),
            "--jobs", str(self.jobs), "--agent-home", str(HERE.parent),
            "--attempt-id", self.aid("att-owner"), "--pid", str(owner.pid),
            "--pid-start", metadata["pid_start"], "--interval", "0.02"],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(observer)
        stdout, stderr = observer.communicate(timeout=15)
        self.assertEqual(observer.returncode, 0, stdout + stderr)
        self.assertEqual(owner.wait(timeout=2), -signal.SIGTERM)
        self.assertEqual(self.close()["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
