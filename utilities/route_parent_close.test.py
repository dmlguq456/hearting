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
            XDG_STATE_HOME=str(self.base / "state"), CODEX_THREAD_ID="fixture-parent",
            HEARTING_GATES="off", COMPUTE_HOSTS_CONFIG=str(self.base / "no-compute.yaml"))
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
