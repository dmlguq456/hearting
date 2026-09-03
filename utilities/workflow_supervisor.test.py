#!/usr/bin/env python3
"""Acceptance suite for the portable tracked-workflow continuation contract.

Every test here maps to one clause of `core/WORKFLOW.md §0.6` / `OPERATIONS §5.12`,
and the BC_ResNet_tf cases are the regression pilot: a run that finished training
while nothing owned evaluation must now either be refused at compile or carried
forward by the supervisor.
"""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

import workflow_state as WS  # noqa: E402
import capability_topology as TOPO  # noqa: E402
import dispatch_contract as DC  # noqa: E402


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules, so register before exec.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SUP = _load("workflow_supervisor", "utilities/workflow-supervisor.py")
ROUTE = _load("capability_route", "utilities/capability-route.py")
RUNNER_CLI = ROOT / "utilities" / "resource-runner.py"

GATE = {
    "spec_read": {"satisfied": True, "source": "fixture-prd-sha256"},
    "drift_verdict": "within-spec",
    "workflow_mode": "tracked",
    "artifact_guard": {"satisfied": True, "source": "fixture-prechecked"},
}


def nested_tuple(worktree, parent="claude", child="codex"):
    sandbox = ROUTE.WRAPPER_PARENT_SANDBOXES[parent][0]
    return {
        "parent_harness": parent, "parent_transport": "headless",
        "parent_sandbox": sandbox, "child_harness": child,
        "launch_authority": "conductor", "status": "supported",
        "probe_source": "fixture-probe", "probe_time": "2026-08-04T00:00:00Z",
        "failure_class": "",
        "checked_worktree": str(Path(worktree).resolve()), "failure_scope": "none",
        "codex_command": "ok" if child == "codex" else "not-applicable",
        "retry_on_isolated_worktree": 0,
    }


def dispatch_evidence(worktree):
    return {"tuples": [nested_tuple(worktree, "claude", "claude"), nested_tuple(worktree, "claude", "codex")],
            "native_subagent": []}


def compile_fixture(capability, capability_mode, cwd, signals):
    return ROUTE.compile_route(
        capability, capability_mode, "standard", cwd, cwd,
        predicates=[], signals=signals, transport="headless",
        transport_evidence="fixture", inline_reason=None, tracking="tracked",
        tracked_gate_evidence=copy.deepcopy(GATE),
        dispatch_evidence=dispatch_evidence(cwd),
    )


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.workflow_root = self.base / "workflow"
        self._previous = os.environ.get("AGENT_WORKFLOW_ROOT")
        os.environ["AGENT_WORKFLOW_ROOT"] = str(self.workflow_root)
        # `terminal_gate_state()` now reads completion markers through
        # `capability-route.py`'s `resolve_agent_home()`; pin AGENT_HOME to an isolated
        # temp dir (with the fixture `core/CORE.md` `resolve_agent_home()` requires to
        # honor the override) so gate reads never touch the real installed home.
        self.agent_home = self.base / "agent-home"
        (self.agent_home / "core").mkdir(parents=True)
        (self.agent_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self._previous_agent_home = os.environ.get("AGENT_HOME")
        os.environ["AGENT_HOME"] = str(self.agent_home)
        # `release_actor_kind()` reads the ambient dispatch env, so a test run
        # launched *from* a registered worker would otherwise classify every
        # fixture release as `headless-owner`. Tests opt in explicitly instead.
        self._previous_dispatch = {
            key: os.environ.get(key)
            for key in ("AGENT_DISPATCH_REGISTERED_WORKER", "AGENT_DISPATCH_ATTEMPT_ID",
                        "AGENT_OWNER_ROUTE_FILE")
        }
        for key in self._previous_dispatch:
            os.environ.pop(key, None)
        self.addCleanup(self._restore)
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        if self._previous is None:
            os.environ.pop("AGENT_WORKFLOW_ROOT", None)
        else:
            os.environ["AGENT_WORKFLOW_ROOT"] = self._previous
        if self._previous_agent_home is None:
            os.environ.pop("AGENT_HOME", None)
        else:
            os.environ["AGENT_HOME"] = self._previous_agent_home
        for key, value in self._previous_dispatch.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # -- fixtures -------------------------------------------------------------
    def write_route(self, nodes, route_id="rt-fixture0000000", route_hash="sha256:fixture"):
        route = {
            "schema_version": 2, "route_id": route_id, "route_hash": route_hash,
            "capability": "autopilot-lab", "capability_mode": "setup",
            "effective_intensity": "standard", "cwd": str(self.base),
            "human_gates": [], "human_gate_bindings": [], "nodes": nodes,
        }
        path = self.base / f"{route_id}.json"
        path.write_text(json.dumps(route, indent=2), encoding="utf-8")
        return route, path

    def _two_stage_nodes(self, continuation=None):
        return [
            {"id": "run", "kind": "resource-runner", "depends_on": [],
             "outputs": ["run.json"], "write_scope": ["run.json"],
             "completion_gate": "fixture-run",
             "continuation": continuation or {"kind": "supervised"}},
            {"id": "verify", "kind": "review-worker", "depends_on": ["run"],
             "outputs": ["reviews/verdict.json"], "write_scope": ["reviews/**"],
             "completion_gate": "fixture-verify", "dispatch_depth": 2,
             "terminal": True, "terminal_gate": "fixture-verify"},
        ]

    def two_stage_route(self, continuation=None, human_gate=None,
                        route_id="rt-fixture0000000", route_hash="sha256:fixture"):
        nodes = self._two_stage_nodes(continuation)
        route, path = self.write_route(nodes, route_id=route_id, route_hash=route_hash)
        if human_gate:
            route["human_gates"] = [human_gate]
            route["human_gate_bindings"] = [
                {"gate": human_gate, "node": "verify", "position": "entry"}]
            path.write_text(json.dumps(route, indent=2), encoding="utf-8")
        return route, path

    def owner_registry(self, route_id="rt-fixture0000000", *,
                       attempt_id="att-fixtureowner0001",
                       session_id="sess-fixture-depth0",
                       recipient_kind="claude-parent-runtime"):
        """A depth-1 owner row plus the dispatch state root the gate record lands in.

        SD-123 (8): the recipient of a gate is the depth-0 session that opened the
        route, and the only place naming it is the owner's registry row
        (`parent_sid`) -- the route record's `owner_attempt_id` is `-` for a
        standard+ route compiled by that session.
        """
        state_root = self.base / "dispatch"
        state_root.mkdir(parents=True, exist_ok=True)
        jobs = state_root / "jobs.log"
        metadata = ",".join([
            f"attempt_id={attempt_id}", f"parent_sid={session_id}",
            f"parent_completion_delivery={recipient_kind}", "dispatch_depth=1",
            "worker_type=owner", f"owner_route_id={route_id}", "harness=claude",
            "registered_worker=1", "execution_surface=registered-headless",
        ])
        jobs.write_text(
            "\t".join(["2026-09-03T00:00:00Z", "open", str(self.base), str(self.base),
                       "fixture-owner", metadata]) + "\n",
            encoding="utf-8",
        )
        return jobs, session_id, attempt_id

    def block_gate(self, path, gate, jobs=None, artifact="shards/frame/frame-summary.json",
                   route_id="rt-fixture0000000"):
        if jobs is None:
            jobs, _session, _attempt = self.owner_registry(route_id=route_id)
        return jobs, SUP.main(["gate", "--route", str(path), "--gate", gate, "--block",
                               "--jobs", str(jobs), "--artifact", artifact])

    def resource_registry(self, *, exit_code=0, pid=None, starttime=None,
                          command_hash=None, status="running", sentinel=True):
        """A registry row plus a real sentinel, so evidence is genuinely readable."""
        registry = self.base / "resource-runs.json"
        sentinel_path = self.base / "run.log.exit"
        if sentinel and exit_code is not None:
            sentinel_path.write_text(str(exit_code), encoding="utf-8")
        row = {
            "run_id": "fixture-run", "pid": pid if pid is not None else 999999999,
            "starttime": starttime or "1",
            "command_hash": command_hash or "0" * 64,
            "process_group": pid if pid is not None else 999999999,
            "cwd": str(self.base), "log": str(self.base / "run.log"),
            "sentinel": str(sentinel_path), "command": ["true"],
            "route": None, "node": "run", "status": status,
            "started_at": time.time() - 60,
        }
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"fixture-run": row}},
                                       indent=2), encoding="utf-8")
        return registry

    def arm(self, route_path, registry, *, node="run", command=None, extra=()):
        marker = self.base / "successor-started"
        # The successor is detached, so it can outlive the fixture teardown; a missing
        # temp dir then is teardown noise, not a defect.
        argv = command or [
            sys.executable, "-c",
            f"import contextlib\nwith contextlib.suppress(OSError):\n"
            f"    open({str(marker)!r},'a').write('x')",
        ]
        args = [
            "arm", "--route", str(route_path), "--node", node,
            "--predecessor-kind", "resource", "--predecessor-id", "fixture-run",
            "--resource-registry", str(registry),
            "--successor-command", json.dumps(argv),
            *extra,
        ]
        SUP.main(args)
        return marker

    def arm_external(self, route_path, registry, *, node="run", extra=()):
        """Arm a supervised watch with no way to start the successor -- the exact
        BC_ResNet_tf shape: another checked surface is declared to own the start, and
        nothing actually starts it."""
        SUP.main([
            "arm", "--route", str(route_path), "--node", node,
            "--predecessor-kind", "resource", "--predecessor-id", "fixture-run",
            "--resource-registry", str(registry), "--successor-external", *extra,
        ])

    class _SurveyArgs:
        def __init__(self, artifact_root, stale_after_seconds, json_output):
            self.artifact_root = str(artifact_root)
            self.stale_after_seconds = stale_after_seconds
            self.json = json_output

    def run_survey(self, artifact_root=None, *, stale_after_seconds=None):
        args = self._SurveyArgs(
            artifact_root if artifact_root is not None else self.base,
            stale_after_seconds if stale_after_seconds is not None else SUP.DEFAULT_STALE_AFTER_SECONDS,
            True,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = SUP.cmd_survey(args)
        return code, json.loads(buf.getvalue())


# ---------------------------------------------------------------------------
# A. the portable state machine
# ---------------------------------------------------------------------------
class TestStateMachine(WorkflowFixture):
    def test_complete_is_reachable_only_through_terminal_verify(self):
        self.assertTrue(WS.can_transition("TERMINAL_VERIFY", "COMPLETE"))
        for state in ("RUNNING", "STAGE_SUCCEEDED", "NEXT_REGISTERED", "NEXT_RUNNING",
                      "READY", "CREATED"):
            self.assertFalse(WS.can_transition(state, "COMPLETE"),
                             f"{state} must not reach COMPLETE directly")

    def test_human_gate_never_advances_automatically(self):
        for target in ("STAGE_SUCCEEDED", "NEXT_REGISTERED", "NEXT_RUNNING",
                       "TERMINAL_VERIFY", "COMPLETE"):
            self.assertFalse(WS.can_transition("BLOCKED_HUMAN_GATE", target))
        self.assertTrue(WS.can_transition("BLOCKED_HUMAN_GATE", "RUNNING"))

    def test_terminal_states_are_absorbing(self):
        for state in ("COMPLETE", "FAILED_TERMINAL", "CANCELLED"):
            self.assertEqual(WS.vocabulary()["transitions"][state], ())

    def test_journal_is_the_source_of_truth_after_a_crash(self):
        ledger = WS.WorkflowLedger("rt-crash0000000000", "sha256:x")
        ledger.record("a", "RUNNING")
        ledger.record("a", "STAGE_SUCCEEDED")
        ledger.state_path.unlink()
        recovered = ledger.state()
        self.assertEqual(recovered["nodes"]["a"]["state"], "STAGE_SUCCEEDED")
        self.assertTrue(ledger.state_path.is_file())

    def test_torn_journal_line_is_dropped_not_guessed(self):
        ledger = WS.WorkflowLedger("rt-torn00000000000", "sha256:x")
        ledger.record("a", "RUNNING")
        with ledger.journal_path.open("a", encoding="utf-8") as handle:
            handle.write('{"node": "b", "state": "STAGE_SUC')
        self.assertEqual(set(ledger.state()["nodes"]), {"a"})

    def test_illegal_node_transition_is_refused(self):
        ledger = WS.WorkflowLedger("rt-illegal00000000", "sha256:x")
        ledger.record("a", "FAILED_TERMINAL")
        with self.assertRaises(WS.WorkflowStateError):
            ledger.record("a", "STAGE_SUCCEEDED")


# ---------------------------------------------------------------------------
# B. supervisor advance semantics
# ---------------------------------------------------------------------------
class TestSupervisorAdvance(WorkflowFixture):
    def test_successful_stage_registers_the_next_stage_exactly_once(self):
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        marker = self.arm(path, registry)
        ledger = SUP.ledger_for(route)
        first = SUP.poll_once(route, ledger)
        second = SUP.poll_once(route, ledger)
        third = SUP.poll_once(route, ledger)
        self.assertEqual(first[0]["action"], "advanced")
        self.assertEqual([row["successor"] for row in first[0]["successors"]], ["verify"])
        self.assertTrue(first[0]["successors"][0]["created"])
        self.assertEqual(second[0]["action"], "settled")
        self.assertEqual(third[0]["action"], "settled")
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "the successor must actually start")
        self.assertEqual(marker.read_text(), "x", "the successor must start exactly once")
        self.assertEqual(len(ledger.claims()), 1)

    def test_failed_stage_does_not_run_downstream(self):
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=1)
        marker = self.arm(path, registry)
        ledger = SUP.ledger_for(route)
        result = SUP.poll_once(route, ledger)
        self.assertEqual(result[0]["action"], "halt-failed")
        self.assertEqual(ledger.state()["nodes"]["run"]["state"], "FAILED_RETRYABLE")
        self.assertEqual(ledger.claims(), {})
        time.sleep(0.2)
        self.assertFalse(marker.exists())
        self.assertEqual(SUP.poll_once(route, ledger)[0]["action"], "halted")

    def test_absent_exit_sentinel_is_never_read_as_success(self):
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=None, sentinel=False)
        self.arm(path, registry)
        ledger = SUP.ledger_for(route)
        result = SUP.poll_once(route, ledger)
        self.assertEqual(result[0]["action"], "halt-failed")
        self.assertEqual(ledger.claims(), {})

    def test_pid_reuse_is_distinguished_from_a_clean_exit(self):
        """A live PID whose start time differs is a different process, not our run."""
        route, path = self.two_stage_route()
        # os.getpid() is alive but its start time will not be "1".
        registry = self.resource_registry(exit_code=None, sentinel=False,
                                          pid=os.getpid(), starttime="1")
        self.arm(path, registry)
        ledger = SUP.ledger_for(route)
        result = SUP.poll_once(route, ledger)
        evidence = result[0]["evidence"]
        self.assertEqual(evidence["liveness"], "stale")
        self.assertEqual(evidence["reason"], "process-identity-mismatch")
        self.assertEqual(result[0]["action"], "halt-failed")
        # The same row with an absent PID is `exited`, a materially different verdict.
        other = self.resource_registry(exit_code=0, pid=999999998, starttime="7")
        row = json.loads(other.read_text())["runs"]["fixture-run"]
        self.assertEqual(SUP.RR.classify_identity(row)[0], "exited")

    def test_concurrent_supervisors_start_one_successor(self):
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        marker = self.arm(path, registry)

        # Four genuinely separate supervisor processes, racing on the same route.
        command = [sys.executable, str(ROOT / "utilities" / "workflow-supervisor.py"),
                   "poll", "--route", str(path)]
        environment = {**os.environ, "AGENT_WORKFLOW_ROOT": str(self.workflow_root)}
        workers = [subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE) for _ in range(4)]
        outcomes = [worker.communicate() for worker in workers]
        for worker, (_out, err) in zip(workers, outcomes):
            self.assertEqual(worker.returncode, 0, err.decode())
        advanced = [json.loads(out)["results"][0]["action"] for out, _err in outcomes]
        self.assertEqual(advanced.count("advanced"), 1,
                         f"exactly one supervisor may advance, saw {advanced}")
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.05)
        ledger = SUP.ledger_for(route)
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual(marker.read_text(), "x",
                         "four concurrent supervisors must create one downstream job")

    def test_restart_resumes_from_the_last_confirmed_stage(self):
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        marker = self.arm(path, registry)
        SUP.poll_once(route, SUP.ledger_for(route))
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.05)
        # A restarted supervisor has no in-memory state: it must rebuild from disk.
        restarted = _load("workflow_supervisor_restart", "utilities/workflow-supervisor.py")
        fresh_route = restarted.load_route(path)
        result = restarted.poll_once(fresh_route, restarted.ledger_for(fresh_route))
        self.assertEqual(result[0]["action"], "settled")
        self.assertEqual(marker.read_text(), "x")
        self.assertEqual(len(restarted.ledger_for(fresh_route).claims()), 1)

    def test_a_human_gate_can_never_be_supervised_into_advancing(self):
        """No arming path exists for a human gate, and a blocked workflow stays blocked."""
        route, path = self.two_stage_route(
            continuation={"kind": "human-gate", "gate": "run-authorization"},
            human_gate="run-authorization")
        registry = self.resource_registry(exit_code=0)
        with self.assertRaisesRegex(SUP.SupervisorError, "supervisor governs only"):
            self.arm(path, registry, extra=["--successor-external"])
        self.block_gate(path, "run-authorization")
        ledger = SUP.ledger_for(route)
        self.assertEqual(ledger.state()["workflow_state"], "BLOCKED_HUMAN_GATE")
        self.assertEqual(SUP.poll_once(route, ledger), [])
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(ledger.state()["workflow_state"], "BLOCKED_HUMAN_GATE")
        SUP.main(["gate", "--route", str(path), "--gate", "run-authorization",
                  "--release", "--by", "fixture-user"])
        self.assertEqual(ledger.state()["workflow_state"], "RUNNING")

    def test_monitor_continuation_waits_for_a_matched_condition(self):
        route, path = self.two_stage_route(
            continuation={"kind": "monitor", "monitor": "external-check"})
        registry = self.resource_registry(exit_code=0)
        evidence_path = self.base / "monitor.json"
        evidence_path.write_text(json.dumps({"condition": "pending"}), encoding="utf-8")
        marker = self.arm(path, registry,
                          extra=["--monitor-evidence", str(evidence_path)])
        ledger = SUP.ledger_for(route)
        self.assertEqual(SUP.poll_once(route, ledger)[0]["action"], "wait-monitor")
        self.assertEqual(ledger.claims(), {})
        evidence_path.write_text(json.dumps({"condition": "matched"}), encoding="utf-8")
        self.assertEqual(SUP.poll_once(route, ledger)[0]["action"], "advanced")
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertTrue(marker.exists())

    def test_declared_artifact_absence_halts_the_advance(self):
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        self.arm(path, registry, extra=["--artifact-base", str(self.base)])
        ledger = SUP.ledger_for(route)
        result = SUP.poll_once(route, ledger)
        self.assertEqual(result[0]["action"], "halt-missing-artifact")
        self.assertEqual(ledger.claims(), {})

    def test_arm_refuses_a_continuation_with_no_way_to_start_the_successor(self):
        _route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        with self.assertRaises(SUP.SupervisorError) as caught:
            SUP.main(["arm", "--route", str(path), "--node", "run",
                      "--predecessor-kind", "resource", "--predecessor-id", "fixture-run",
                      "--resource-registry", str(registry)])
        self.assertIn("successor-command", str(caught.exception))

    def test_arm_refuses_a_terminal_node(self):
        _route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        with self.assertRaises(SUP.SupervisorError):
            SUP.main(["arm", "--route", str(path), "--node", "verify",
                      "--predecessor-kind", "resource", "--predecessor-id", "fixture-run",
                      "--resource-registry", str(registry),
                      "--successor-command", '["true"]'])


# ---------------------------------------------------------------------------
# B2. A50-8 `release` closes the gap `gate --release` alone leaves open
# ---------------------------------------------------------------------------
class TestGateRelease(WorkflowFixture):
    def _blocked(self):
        route, path = self.two_stage_route(
            continuation={"kind": "human-gate", "gate": "frame-review"},
            human_gate="frame-review")
        self.jobs, _code = self.block_gate(path, "frame-review")
        return route, path

    def test_proceed_claims_and_reports_the_successor_exactly_once(self):
        route, path = self._blocked()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = SUP.main(["release", "--route", str(path), "--gate", "frame-review",
                             "--decision", "proceed", "--actor", "fixture-user"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["decision"], "proceed")
        self.assertEqual(payload["node"], "run")
        self.assertEqual(len(payload["successors"]), 1)
        self.assertEqual(payload["successors"][0]["successor"], "verify")
        self.assertTrue(payload["successors"][0]["created"])
        ledger = SUP.ledger_for(route)
        self.assertEqual(ledger.state()["workflow_state"], "RUNNING")
        self.assertEqual(len(ledger.claims()), 1)
        # A second release attempt is refused outright by the same state
        # guard cmd_gate's --release already uses -- the gate is no longer
        # BLOCKED_HUMAN_GATE, so "exactly once" never depends on re-deriving
        # the claim key twice.
        with self.assertRaisesRegex(SUP.SupervisorError, "not blocked"):
            SUP.main(["release", "--route", str(path), "--gate", "frame-review",
                     "--decision", "proceed"])
        self.assertEqual(len(ledger.claims()), 1)

    def test_proceed_requires_a_blocked_workflow(self):
        route, path = self.two_stage_route(
            continuation={"kind": "human-gate", "gate": "frame-review"},
            human_gate="frame-review")
        with self.assertRaisesRegex(SUP.SupervisorError, "not blocked"):
            SUP.main(["release", "--route", str(path), "--gate", "frame-review",
                     "--decision", "proceed"])

    def test_release_rejects_an_undeclared_gate(self):
        route, path = self._blocked()
        with self.assertRaisesRegex(SUP.SupervisorError, "no human gate"):
            SUP.main(["release", "--route", str(path), "--gate", "no-such-gate",
                     "--decision", "proceed"])

    def test_revise_reaches_failed_retryable_via_the_declared_two_hop_path(self):
        # BLOCKED_HUMAN_GATE has no direct transition to FAILED_RETRYABLE in the
        # topology registry -- only via RUNNING, both hops declared. Confirms
        # `release --decision revise` does not need a vocabulary widening.
        self.assertFalse(WS.can_transition("BLOCKED_HUMAN_GATE", "FAILED_RETRYABLE"))
        route, path = self._blocked()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = SUP.main(["release", "--route", str(path), "--gate", "frame-review",
                             "--decision", "revise", "--actor", "fixture-user"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["decision"], "revise")
        self.assertEqual(payload["retry_boundary"], "frame")
        ledger = SUP.ledger_for(route)
        self.assertEqual(ledger.state()["workflow_state"], "FAILED_RETRYABLE")
        self.assertEqual(ledger.claims(), {})

    def test_stop_sets_cancelled_with_operator_decision_evidence(self):
        route, path = self._blocked()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = SUP.main(["release", "--route", str(path), "--gate", "frame-review",
                             "--decision", "stop", "--actor", "fixture-user"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["decision"], "stop")
        ledger = SUP.ledger_for(route)
        self.assertEqual(ledger.state()["workflow_state"], "CANCELLED")
        journal = ledger.journal()
        last = journal[-1]
        self.assertEqual(last["evidence"]["abandon_reason"], "operator-decision")


# ---------------------------------------------------------------------------
# C. completion is terminal-node bound
# ---------------------------------------------------------------------------
class TestCompletion(WorkflowFixture):
    def test_workflow_does_not_complete_before_its_terminal_node(self):
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        self.arm(path, registry)
        ledger = SUP.ledger_for(route)
        SUP.poll_once(route, ledger)
        self.assertNotEqual(ledger.state()["workflow_state"], "COMPLETE")

        class Args:
            route = str(path)

        self.assertEqual(SUP.cmd_complete(Args()), 3)
        self.assertNotEqual(ledger.state()["workflow_state"], "COMPLETE")

    def test_derive_workflow_state_requires_verified_terminal_gates(self):
        nodes = {"run": {"state": "STAGE_SUCCEEDED"}, "verify": {"state": "STAGE_SUCCEEDED"}}
        self.assertEqual(
            WS.derive_workflow_state(nodes, ["verify"], terminal_gates_passed=False),
            "TERMINAL_VERIFY")
        self.assertEqual(
            WS.derive_workflow_state(nodes, ["verify"], terminal_gates_passed=True),
            "COMPLETE")

    def test_failure_outranks_downstream_success(self):
        nodes = {"run": {"state": "FAILED_RETRYABLE"}, "verify": {"state": "STAGE_SUCCEEDED"}}
        self.assertEqual(
            WS.derive_workflow_state(nodes, ["verify"], terminal_gates_passed=True),
            "FAILED_RETRYABLE")


# ---------------------------------------------------------------------------
# D. status projection and resource visibility
# ---------------------------------------------------------------------------
class TestStatusProjection(WorkflowFixture):
    def test_status_exposes_workflow_stage_resource_and_failure(self):
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        self.arm(path, registry)
        ledger = SUP.ledger_for(route)
        SUP.poll_once(route, ledger)
        captured = subprocess.run(
            [sys.executable, str(ROOT / "utilities" / "workflow-supervisor.py"),
             "status", "--route", str(path), "--json"],
            capture_output=True, text=True,
            env={**os.environ, "AGENT_WORKFLOW_ROOT": str(self.workflow_root)})
        self.assertEqual(captured.returncode, 0, captured.stderr)
        payload = json.loads(captured.stdout)
        for field in ("workflow_state", "current_stage", "next_stage", "terminal_nodes",
                      "terminal_gates", "resource_children", "failure_reason",
                      "updated_at", "human_gate_bindings", "claims"):
            self.assertIn(field, payload)
        self.assertEqual(payload["terminal_nodes"], ["verify"])
        self.assertEqual(payload["next_stage"], ["verify"])
        self.assertEqual([child["run_id"] for child in payload["resource_children"]],
                         ["fixture-run"])

    def test_resource_child_is_listed_even_when_the_global_index_misses_it(self):
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        self.arm(path, registry)
        children = SUP.resource_children(route, SUP.ledger_for(route))
        self.assertEqual([child["run_id"] for child in children], ["fixture-run"])


# ---------------------------------------------------------------------------
# E. resource-runner lifecycle
# ---------------------------------------------------------------------------
class TestResourceLifecycle(WorkflowFixture):
    def run_registry(self):
        return self.base / "runs.json"

    def test_reap_persists_terminal_status_exit_code_and_ended_at(self):
        registry = self.run_registry()
        sentinel = self.base / "job.log.exit"
        sentinel.write_text("0", encoding="utf-8")
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"job": {
            "run_id": "job", "pid": 999999997, "starttime": "5",
            "command_hash": "a" * 64, "process_group": 999999997,
            "cwd": str(self.base), "log": str(self.base / "job.log"),
            "sentinel": str(sentinel), "command": ["true"], "status": "running",
        }}}), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(RUNNER_CLI), "--registry", str(registry),
             "reap", "--run-id", "job"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["workflow_state"], "STAGE_SUCCEEDED")
        self.assertIsNotNone(payload["ended_at"])
        stored = json.loads(registry.read_text())["runs"]["job"]
        self.assertEqual(stored["status"], "succeeded")

    def test_no_stale_running_survives_an_observation(self):
        registry = self.run_registry()
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"job": {
            "run_id": "job", "pid": 999999996, "starttime": "5",
            "command_hash": "b" * 64, "process_group": 999999996,
            "cwd": str(self.base), "log": str(self.base / "job.log"),
            "sentinel": str(self.base / "absent.exit"), "command": ["true"],
            "status": "running",
        }}}), encoding="utf-8")
        subprocess.run([sys.executable, str(RUNNER_CLI), "--registry", str(registry),
                        "status", "--run-id", "job"], capture_output=True, text=True, check=True)
        stored = json.loads(registry.read_text())["runs"]["job"]
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["failure_class"], "no-exit-sentinel")

    def test_sentinel_wrapper_persists_the_payload_exit_status(self):
        registry = self.run_registry()
        log = self.base / "direct.log"
        sentinel = Path(str(log) + ".exit")
        runner = _load("resource_runner_direct", "utilities/resource-runner.py")
        with open(log, "ab") as stream:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", runner.SENTINEL_SCRIPT, "resource-runner",
                 "sh", "-c", "exit 7"],
                cwd=self.base, env={**os.environ, "AGENT_RESOURCE_SENTINEL": str(sentinel)},
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        identity = None
        for _ in range(50):
            identity = SUP.RR.proc_identity(proc.pid)
            if identity:
                break
            time.sleep(0.01)
        self.assertIsNotNone(identity)
        proc.wait(timeout=30)
        for _ in range(100):
            if sentinel.is_file():
                break
            time.sleep(0.02)
        self.assertEqual(sentinel.read_text().strip(), "7",
                         "the wrapper must persist the payload exit status")
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"job": {
            **identity, "run_id": "job", "process_group": proc.pid,
            "cwd": str(self.base), "log": str(log), "sentinel": str(sentinel),
            "command": ["sh", "-c", "exit 7"], "status": "running",
            "parent_attempt_id": "att-fixture", "workflow_state": "RUNNING",
        }}}), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(RUNNER_CLI), "--registry", str(registry),
             "reap", "--run-id", "job"], capture_output=True, text=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["exit_code"], 7)
        self.assertEqual(payload["failure_class"], "exit-7")
        self.assertEqual(payload["parent_attempt_id"], "att-fixture")


# ---------------------------------------------------------------------------
# F. the graph contract itself, and the BC_ResNet_tf regression
# ---------------------------------------------------------------------------
class TestGraphContract(unittest.TestCase):
    def setUp(self):
        self.registry = TOPO.load_registry()

    def recipe(self, capability, mode):
        return TOPO.resolve_recipe(self.registry, capability, mode)

    def test_every_recipe_declares_a_terminal_and_full_continuations(self):
        for recipe in self.registry["recipes"]:
            nodes = recipe["standard_plus"]["nodes"]
            dependents = {node["id"]: [] for node in nodes}
            for node in nodes:
                for dep in node.get("depends_on", []):
                    dependents[dep].append(node["id"])
            sinks = [node for node in nodes if not dependents[node["id"]]]
            self.assertTrue(sinks, recipe["capability"])
            for node in sinks:
                self.assertTrue(node.get("terminal"), (recipe["capability"], node["id"]))
                self.assertNotEqual(node["kind"], "resource-runner")
            for node in nodes:
                if dependents[node["id"]]:
                    self.assertIn(node["continuation"]["kind"],
                                  self.registry["continuation_kinds"],
                                  (recipe["capability"], node["id"]))

    def test_bc_resnet_shape_is_refused_at_registry_validation(self):
        """The original lab-setup graph — training as the last node — no longer compiles."""
        broken = copy.deepcopy(self.registry)
        recipe = next(r for r in broken["recipes"]
                      if r["capability"] == "autopilot-lab" and "setup" in r["modes"])
        nodes = recipe["standard_plus"]["nodes"]
        recipe["standard_plus"]["nodes"] = [n for n in nodes
                                            if n["id"] not in ("run-verify", "handoff")]
        # run-verify became a parallel-group anchor (W3); drop its group too
        # so the registry validates down to the terminal-shape assertion.
        recipe["standard_plus"]["parallel_groups"] = [
            g for g in recipe["standard_plus"].get("parallel_groups", [])
            if g.get("node") != "run-verify"
        ]
        full_run = next(n for n in recipe["standard_plus"]["nodes"] if n["id"] == "full-run")
        full_run.pop("continuation", None)
        full_run["terminal"] = True
        full_run["terminal_gate"] = full_run["completion_gate"]
        recipe["conditional_extensions"][0]["after"] = ["full-run"]
        recipe["completion_gates"] = [g for g in recipe["completion_gates"]
                                      if g not in ("lab-run-verify", "lab-setup-handoff")]
        recipe["resume_retry_boundaries"] = [b for b in recipe["resume_retry_boundaries"]
                                             if b not in ("run-verify", "handoff")]
        with self.assertRaises(TOPO.TopologyError) as caught:
            TOPO.validate_registry(broken)
        self.assertIn("detached resource run can never be the workflow terminal",
                      str(caught.exception))

    def test_a_detached_node_may_not_claim_a_non_supervised_continuation(self):
        broken = copy.deepcopy(self.registry)
        recipe = next(r for r in broken["recipes"]
                      if r["capability"] == "autopilot-lab" and "eval" in r["modes"])
        node = next(n for n in recipe["standard_plus"]["nodes"] if n["id"] == "eval-run")
        node["continuation"] = {"kind": "inline-next"}
        with self.assertRaisesRegex(TOPO.TopologyError, "cannot continue itself"):
            TOPO.validate_registry(broken)

    def test_a_stage_with_no_continuation_is_refused(self):
        broken = copy.deepcopy(self.registry)
        recipe = next(r for r in broken["recipes"] if r["capability"] == "autopilot-code")
        node = next(n for n in recipe["standard_plus"]["nodes"] if n["id"] == "execute")
        node.pop("continuation")
        with self.assertRaisesRegex(TOPO.TopologyError, "requires a continuation"):
            TOPO.validate_registry(broken)

    def test_an_unbound_human_gate_is_refused(self):
        broken = copy.deepcopy(self.registry)
        recipe = next(r for r in broken["recipes"] if r["capability"] == "autopilot-ship")
        recipe["human_gates"] = ["deploy-authorization", "invented-gate"]
        with self.assertRaisesRegex(TOPO.TopologyError, "bind to exactly one node"):
            TOPO.validate_registry(broken)

    def test_lab_setup_graph_carries_the_run_through_verification_to_handoff(self):
        recipe = self.recipe("autopilot-lab", "setup")
        ids = [node["id"] for node in recipe["standard_plus"]["nodes"]]
        self.assertEqual(ids, ["scaffold", "smoke", "full-run", "run-verify", "handoff"])
        by_id = {node["id"]: node for node in recipe["standard_plus"]["nodes"]}
        self.assertEqual(by_id["smoke"]["continuation"],
                         {"kind": "human-gate", "gate": "full-run-authorization"})
        self.assertEqual(by_id["full-run"]["continuation"], {"kind": "supervised"})
        self.assertTrue(by_id["handoff"]["terminal"])
        self.assertEqual(recipe["conditional_extensions"][0]["after"], ["handoff"])

    def test_lab_eval_resource_node_is_supervised_and_sync_is_terminal(self):
        recipe = self.recipe("autopilot-lab", "eval")
        by_id = {node["id"]: node for node in recipe["standard_plus"]["nodes"]}
        self.assertEqual(by_id["eval-run"]["continuation"], {"kind": "supervised"})
        self.assertTrue(by_id["sync"]["terminal"])

    def test_ship_realizes_its_declared_deploy_authorization(self):
        recipe = self.recipe("autopilot-ship", "default")
        by_id = {node["id"]: node for node in recipe["standard_plus"]["nodes"]}
        self.assertIn("deploy", by_id)
        self.assertIn("post-deploy-verify", by_id)
        self.assertEqual(recipe["human_gate_bindings"],
                         [{"gate": "deploy-authorization", "node": "deploy",
                           "position": "entry"}])
        for reviewer in ("security-review", "release-review"):
            self.assertEqual(by_id[reviewer]["continuation"],
                             {"kind": "human-gate", "gate": "deploy-authorization"})
        self.assertTrue(by_id["post-deploy-verify"]["terminal"])

    def test_code_route_terminal_is_the_report(self):
        recipe = self.recipe("autopilot-code", "dev")
        by_id = {node["id"]: node for node in recipe["standard_plus"]["nodes"]}
        self.assertTrue(by_id["report"]["terminal"])
        self.assertEqual(by_id["execute"]["continuation"], {"kind": "inline-next"})


class TestSealedRoutes(unittest.TestCase):
    """The compiled route must carry the contract, not just the registry."""

    def compile(self, capability, mode, signals):
        with tempfile.TemporaryDirectory() as td:
            return compile_fixture(capability, mode, td, signals)

    def test_lab_setup_route_seals_terminal_handoff_and_supervised_run(self):
        route = self.compile("autopilot-lab", "setup", ["resource-run"])
        contract = route["workflow_contract"]
        self.assertEqual(contract["terminal_nodes"], ["handoff"])
        self.assertEqual(contract["continuations"]["full-run"], "supervised")
        self.assertEqual(contract["continuations"]["smoke"], "human-gate")
        self.assertEqual(route["human_gate_bindings"],
                         [{"gate": "full-run-authorization", "node": "full-run",
                           "position": "entry"}])
        ROUTE.verify_route(route, route["cwd"])

    def test_ship_route_seals_post_deploy_verification(self):
        route = self.compile("autopilot-ship", "default", ["human-gate"])
        self.assertEqual(route["workflow_contract"]["terminal_nodes"],
                         ["post-deploy-verify"])
        ROUTE.verify_route(route, route["cwd"])

    def test_code_route_seals_the_report_terminal(self):
        route = self.compile("autopilot-code", "dev", ["shared-contract"])
        self.assertEqual(route["workflow_contract"]["terminal_nodes"], ["report"])
        ROUTE.verify_route(route, route["cwd"])

    def test_tampered_workflow_contract_fails_verification(self):
        route = self.compile("autopilot-lab", "eval", ["gpu"])
        route["workflow_contract"]["terminal_nodes"] = ["eval-run"]
        route["route_hash"] = ROUTE.route_hash(route)
        route["route_id"] = "rt-" + route["route_hash"].split(":", 1)[1][:16]
        with self.assertRaisesRegex(ValueError, "workflow contract"):
            ROUTE.verify_route(route, route["cwd"])

    def test_direct_route_declares_its_single_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            direct = ROUTE.compile_route(
                "autopilot-code", "dev", "direct", td, td,
                predicates=["atomic-outcome", "known-scope", "no-shared-contract",
                            "no-resource-run", "no-artifact-handoff",
                            "no-independent-verifier", "focused-verification"],
                signals=[], transport=None, inline_reason="atomic-direct",
                tracking="tracked", tracked_gate_evidence=copy.deepcopy(GATE))
            self.assertEqual(direct["workflow_contract"]["terminal_nodes"], ["inline"])
            self.assertEqual(direct["workflow_contract"]["continuations"], {})
            ROUTE.verify_route(direct, td)


class TestCapabilityIntegration(WorkflowFixture):
    """One end-to-end pass per capability family, on the real compiled graphs."""

    def compiled(self, capability, mode, signals):
        route = compile_fixture(capability, mode, str(self.base), signals)
        path = self.base / f"{route['route_id']}.json"
        path.write_text(json.dumps(route, indent=2), encoding="utf-8")
        return route, path

    def test_lab_setup_run_advances_to_verification_exactly_once(self):
        """BC_ResNet_tf pilot: the finished training run now carries itself forward.

        The 2026-08-04 failure was that training and its hard-negative loop finished and
        nothing owned what came next. On the real `autopilot-lab --mode setup` graph the
        supervisor must now advance `full-run` into `run-verify` — once, and only on
        genuine terminal evidence.
        """
        route, path = self.compiled("autopilot-lab", "setup", ["resource-run"])
        self.assertEqual(route["workflow_contract"]["terminal_nodes"], ["handoff"])
        registry = self.resource_registry(exit_code=0)
        marker = self.arm(path, registry, node="full-run")
        ledger = SUP.ledger_for(route)
        result = SUP.poll_once(route, ledger)
        self.assertEqual(result[0]["action"], "advanced")
        self.assertEqual([row["successor"] for row in result[0]["successors"]],
                         ["run-verify"])
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertEqual(marker.read_text(), "x")
        self.assertEqual(SUP.poll_once(route, ledger)[0]["action"], "settled")
        self.assertEqual(marker.read_text(), "x")
        # Training succeeding is not the workflow succeeding.
        self.assertNotEqual(ledger.state()["workflow_state"], "COMPLETE")

    def test_lab_eval_run_advances_into_metrics(self):
        route, path = self.compiled("autopilot-lab", "eval", ["gpu"])
        registry = self.resource_registry(exit_code=0)
        self.arm(path, registry, node="eval-run")
        result = SUP.poll_once(route, SUP.ledger_for(route))
        self.assertEqual([row["successor"] for row in result[0]["successors"]], ["metrics"])

    def test_code_stages_are_inline_and_a_failed_registered_attempt_is_not_success(self):
        """Code stages continue in-payload; a supervisor must refuse to fake that.

        The failure evidence path still has to be exact, because the same registered
        attempt rows feed every capability that *does* supervise.
        """
        route, path = self.compiled("autopilot-code", "dev", ["shared-contract"])
        registry = self.resource_registry(exit_code=0)
        for node in ("execute", "test", "plan"):
            with self.assertRaisesRegex(SUP.SupervisorError, "supervisor governs only"):
                self.arm(path, registry, node=node)
        self.assertEqual(SUP.ledger_for(route).claims(), {})

        jobs = self.base / "jobs.log"
        def row(status, note, failure_class):
            meta = (f"attempt_id=att-code,route_node=execute,note={note},"
                    f"failure_class={failure_class}")
            return "\t".join(["2026-08-04T00:00:00Z", status, "repo", str(self.base),
                              "slug", meta])
        jobs.write_text(row("done", "dead-limit", "capacity") + "\n", encoding="utf-8")
        evidence = SUP.registered_evidence({"predecessor_id": "att-code", "jobs": str(jobs)})
        self.assertTrue(evidence["terminal"])
        self.assertFalse(evidence["succeeded"])
        jobs.write_text(row("done", "completed-marker", "pass") + "\n", encoding="utf-8")
        self.assertTrue(SUP.registered_evidence(
            {"predecessor_id": "att-code", "jobs": str(jobs)})["succeeded"])
        jobs.write_text(row("open", "", "") + "\n", encoding="utf-8")
        self.assertFalse(SUP.registered_evidence(
            {"predecessor_id": "att-code", "jobs": str(jobs)})["terminal"])

    def test_ship_readiness_stops_at_the_deploy_authorization(self):
        route, path = self.compiled("autopilot-ship", "default", ["human-gate"])
        registry = self.resource_registry(exit_code=0)
        with self.assertRaisesRegex(SUP.SupervisorError, "supervisor governs only"):
            self.arm(path, registry, node="release-review")
        self.block_gate(path, "deploy-authorization", route_id=route["route_id"])
        ledger = SUP.ledger_for(route)
        self.assertEqual(ledger.state()["workflow_state"], "BLOCKED_HUMAN_GATE")
        self.assertEqual(ledger.claims(), {})

    def test_generic_monitor_workflow_advances_only_on_a_matched_condition(self):
        """A composed observe → condition → approved-action → verify graph."""
        compose = _load("compose_route", "utilities/compose-route.py")
        topology = TOPO.load_registry()
        units = [
            {"id": "observe", "unit": "qa/data-curate", "kind": "map-worker",
             "write_scope": ["shards/observe/**"], "outputs": ["shards/observe/state.json"],
             "gate": "note-scan", "continuation": {"kind": "monitor",
                                                   "monitor": "external-state-change"}},
            {"id": "act", "unit": "dev/backend", "depends_on": ["observe"],
             "write_scope": ["source/**"], "outputs": ["source-diff"],
             "gate": "code-execute"},
            {"id": "verify", "unit": "qa/test", "depends_on": ["act"],
             "write_scope": ["reviews/monitor/**"],
             "outputs": ["reviews/monitor-verdict.json"], "gate": "code-test"},
        ]
        recipe = compose.build_recipe(
            "autopilot-code", "dev", units, topology_class="staged",
            quick_write_scope=["source/**"],
            quick_model_profile=topology["owner_profile_by_intensity"]["quick"],
            gate_index=compose.unit_io_gate_index(topology),
            cycle_anchors=["analysis_project"], map_anchor="shards",
            review_anchor="reviews",
            human_gate_bindings=[{"gate": "approved-action", "node": "act",
                                  "position": "entry"}],
        )
        by_id = {node["id"]: node for node in recipe["standard_plus"]["nodes"]}
        # An entry gate on the action node outranks the declared monitor: an approved
        # action is a human decision, not an automatic consequence of a match.
        self.assertEqual(by_id["observe"]["continuation"],
                         {"kind": "human-gate", "gate": "approved-action"})
        self.assertTrue(by_id["verify"]["terminal"])

        unattended = copy.deepcopy(units)
        recipe = compose.build_recipe(
            "autopilot-code", "dev", unattended, topology_class="staged",
            quick_write_scope=["source/**"],
            quick_model_profile=topology["owner_profile_by_intensity"]["quick"],
            gate_index=compose.unit_io_gate_index(topology),
            cycle_anchors=["analysis_project"], map_anchor="shards",
            review_anchor="reviews",
        )
        route = ROUTE.compile_composed_route(
            recipe, "dev", "standard", str(self.base), str(self.base),
            predicates=[], signals=["independent-verifier"], transport="headless",
            transport_evidence="fixture", tracking="tracked",
            tracked_gate_evidence=copy.deepcopy(GATE),
            dispatch_evidence=dispatch_evidence(self.base))
        self.assertEqual(route["workflow_contract"]["continuations"]["observe"], "monitor")
        self.assertEqual(route["workflow_contract"]["terminal_nodes"], ["verify"])
        path = self.base / f"{route['route_id']}.json"
        path.write_text(json.dumps(route, indent=2), encoding="utf-8")
        registry = self.resource_registry(exit_code=0)
        condition = self.base / "condition.json"
        condition.write_text(json.dumps({"condition": "pending"}), encoding="utf-8")
        marker = self.arm(path, registry, node="observe",
                          extra=["--monitor-evidence", str(condition)])
        ledger = SUP.ledger_for(route)
        self.assertEqual(SUP.poll_once(route, ledger)[0]["action"], "wait-monitor")
        condition.write_text(json.dumps({"condition": "matched"}), encoding="utf-8")
        self.assertEqual(SUP.poll_once(route, ledger)[0]["action"], "advanced")
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertEqual(marker.read_text(), "x")


class TestRouteClosure(unittest.TestCase):
    """A cycle that edits the registry must still be able to close its own route."""

    def setUp(self):
        # `close_route`/`route_status` now read completion markers through
        # `resolve_agent_home()`; isolate AGENT_HOME so those reads never touch the
        # real installed home.
        self.tmp_home = tempfile.TemporaryDirectory()
        (Path(self.tmp_home.name) / "core").mkdir(parents=True)
        (Path(self.tmp_home.name) / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self._previous_agent_home = os.environ.get("AGENT_HOME")
        os.environ["AGENT_HOME"] = self.tmp_home.name
        self.addCleanup(self._restore_agent_home)

    def _restore_agent_home(self):
        if self._previous_agent_home is None:
            os.environ.pop("AGENT_HOME", None)
        else:
            os.environ["AGENT_HOME"] = self._previous_agent_home
        self.tmp_home.cleanup()

    def compile(self, cwd):
        return compile_fixture("autopilot-code", "dev", cwd, ["shared-contract"])

    def test_a_registry_edit_does_not_orphan_the_route_that_made_it(self):
        with tempfile.TemporaryDirectory() as td:
            route = self.compile(td)
            path = Path(td) / "route.json"
            ROUTE.write_once(path, route)
            stale = dict(route, registry_digest="sha256:" + "0" * 64)
            stale["route_hash"] = ROUTE.route_hash(stale)
            stale["route_id"] = "rt-" + stale["route_hash"].split(":", 1)[1][:16]
            stale_path = Path(td) / "stale-route.json"
            ROUTE.write_once(stale_path, stale)

            # Anything that could launch or mutate still refuses the stale route.
            with self.assertRaisesRegex(ValueError, "stale registry digest"):
                ROUTE.verify_route(stale)

            verified = ROUTE.verify_route(stale, allow_stale_registry=True)
            self.assertFalse(verified["_registry_current"])
            outcome, created = ROUTE.close_route(verified, stale_path, "deadbeef", "superseded")
            self.assertTrue(created)
            self.assertIs(outcome["registry_current"], False)
            self.assertEqual(outcome["route_id"], stale["route_id"])

            fresh, _created = ROUTE.close_route(
                ROUTE.verify_route(route, allow_stale_registry=True), path, "deadbeef", "done")
            self.assertIs(fresh["registry_current"], True)

            rows = {row["route_id"]: row for row in ROUTE.route_status(td)}
            self.assertTrue(all(row["closed"] for row in rows.values()))
            self.assertIs(rows[stale["route_id"]]["registry_current"], False)

    def test_a_tampered_route_is_never_closable(self):
        with tempfile.TemporaryDirectory() as td:
            route = self.compile(td)
            route["nodes"][0]["write_scope"] = ["source/**"]
            with self.assertRaisesRegex(ValueError, "stale or modified route hash"):
                ROUTE.verify_route(route, allow_stale_registry=True)


class TestParentResumeOncePerBatch(unittest.TestCase):
    """Managed completion resumes the parent thread once per batch, not per child."""

    def test_a_batch_receipt_is_consumed_exactly_once(self):
        join = _load("dispatch_completion_join", "utilities/dispatch_completion_join.py")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "parent-session.json"
            session = "session-fixture"
            for attempt in ("att-one", "att-two"):
                join.register_parent_session_attempt(path, session, attempt)
            state = join.read_parent_session_batch_state(path, session)
            self.assertEqual(set(state.attempt_ids), {"att-one", "att-two"})
            join.write_parent_session_state(path, session, {"att-one", "att-two"},
                                            attempt_ids={"att-one", "att-two"})
            self.assertTrue(join.consume_parent_session_attempt(path, session, "att-one"))
            self.assertFalse(join.consume_parent_session_attempt(path, session, "att-one"),
                             "a delivered receipt may not wake the parent twice")
            self.assertTrue(join.consume_parent_session_attempt(path, session, "att-two"))
            self.assertFalse(path.exists())


# ---------------------------------------------------------------------------
# D. root-scoped read-only survey (P3)
# ---------------------------------------------------------------------------
class TestSurveyLedgerRoot(unittest.TestCase):
    """`default_ledger_root()` must resolve through the validated agent-home resolver,
    not a bare `AGENT_HOME`-or-checkout-`ROOT` fallback -- the exact bug that made a
    linked-worktree `status` call report `CREATED`/empty for a route that was really
    `RUNNING` under the actual installed `AGENT_HOME`."""

    def setUp(self):
        self._previous = {key: os.environ.get(key)
                          for key in ("AGENT_WORKFLOW_ROOT", "AGENT_HOME", "CLAUDE_HOME", "HOME")}
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_ledger_root_uses_validated_agent_home(self):
        for key in ("AGENT_WORKFLOW_ROOT", "AGENT_HOME", "CLAUDE_HOME"):
            os.environ.pop(key, None)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "agent_setting" / "core").mkdir(parents=True)
            (home / "agent_setting" / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
            os.environ["HOME"] = str(home)
            # Red before P3: the old fallback returned the utility checkout `ROOT`
            # (this repo's own worktree), never the real installed `agent_setting`.
            # The expected root is derived through the same
            # `resolve_dispatch_state_root(resolve_agent_home())` call
            # `default_ledger_root()` makes (review N-4): a hardcoded
            # `.../.dispatch` literal goes stale the moment `AGENT_DISPATCH_JOBS`
            # is inherited (every registered worker's environment), since that
            # env var -- not `agent_setting` -- then decides the state root.
            expected = DC.resolve_dispatch_state_root(home / "agent_setting") / "workflow"
            self.assertEqual(WS.default_ledger_root(), expected)
            self.assertNotEqual(WS.default_ledger_root(), WS.ROOT / ".dispatch" / "workflow")

    def test_agent_workflow_root_override_still_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "explicit-workflow-root"
            os.environ["AGENT_WORKFLOW_ROOT"] = str(override)
            os.environ["AGENT_HOME"] = str(Path(tmp) / "unrelated-home")
            self.assertEqual(WS.default_ledger_root(), override)


class TestSurvey(WorkflowFixture):
    def test_survey_ranks_exited_unclaimed_external_watch_as_abandoned(self):
        # The exact BC_ResNet_tf shape: a fresh supervised watch, exact exited resource
        # predecessor, `successor_external: true`, no claim, no successor progress, and
        # an unproven terminal gate.
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        self.arm_external(path, registry)
        code, payload = self.run_survey()
        self.assertEqual(code, 0)
        self.assertTrue(payload["rows"])
        top = payload["rows"][0]
        self.assertEqual(top["route_id"], route["route_id"])
        self.assertEqual(top["risk"]["tier"], "abandoned")
        self.assertTrue(top["armed"]["run"]["successor_external"])
        self.assertEqual(top["armed"]["run"]["predecessor_liveness"], "exited")
        self.assertFalse(top["armed"]["run"]["claimed_or_progressed"])
        self.assertIs(top["terminal_gate_proven"], False)
        self.assertEqual(payload["ledger_root"], str(self.workflow_root))
        # Derived through the same resolve_dispatch_state_root(resolve_agent_home())
        # call the survey command makes (review N-4): a hardcoded
        # `self.agent_home / ".dispatch"` literal goes stale once
        # AGENT_DISPATCH_JOBS is inherited, since that env var then wins over
        # AGENT_HOME for the state root.
        self.assertEqual(
            payload["completion_root"],
            str(DC.resolve_dispatch_state_root(self.agent_home) / "completion"),
        )

    def test_survey_false_positive_matrix(self):
        # (a) an open human gate must never rank abandoned, even with an otherwise
        # identical exited/external/unclaimed watch.
        route_a, path_a = self.two_stage_route(human_gate="run-authorization")
        registry_a = self.resource_registry(exit_code=0)
        self.arm_external(path_a, registry_a)
        self.block_gate(path_a, "run-authorization", route_id=route_a["route_id"])
        _code, payload_a = self.run_survey()
        row_a = next(row for row in payload_a["rows"] if row["route_id"] == route_a["route_id"])
        self.assertNotEqual(row_a["risk"]["tier"], "abandoned")

        # (b) a matching claim for the successor is a claimed external handoff, not
        # abandonment.
        route_b, path_b = self.two_stage_route(
            route_id="rt-fixtureclaimedb", route_hash="sha256:fixtureclaimedb")
        registry_b = self.resource_registry(exit_code=0)
        self.arm_external(path_b, registry_b)
        ledger_b = SUP.ledger_for(route_b)
        ledger_b.claim("b" * 32, {"route_id": route_b["route_id"], "predecessor": "run",
                                  "successor": "verify"})
        _code, payload_b = self.run_survey()
        row_b = next(row for row in payload_b["rows"] if row["route_id"] == route_b["route_id"])
        self.assertNotEqual(row_b["risk"]["tier"], "abandoned")
        self.assertTrue(row_b["armed"]["run"]["claimed_or_progressed"])

        # (c) evidence older than the stale bound demotes to unknown, never abandoned.
        route_c, path_c = self.two_stage_route(
            route_id="rt-fixturestalec0", route_hash="sha256:fixturestalec0")
        registry_c = self.resource_registry(exit_code=0)
        self.arm_external(path_c, registry_c)
        _code, payload_c = self.run_survey(stale_after_seconds=1e-6)
        row_c = next(row for row in payload_c["rows"] if row["route_id"] == route_c["route_id"])
        self.assertNotEqual(row_c["risk"]["tier"], "abandoned")
        self.assertTrue(row_c["evidence_freshness"]["stale"])

        # `successor_external` changes score, never tier, among genuinely abandoned rows.
        route_d, path_d = self.two_stage_route(
            route_id="rt-fixtureexternal", route_hash="sha256:fixtureexternal")
        registry_d = self.resource_registry(exit_code=0)
        self.arm_external(path_d, registry_d)
        route_e, path_e = self.two_stage_route(
            route_id="rt-fixturenoexternal", route_hash="sha256:fixturenoexternal")
        registry_e = self.resource_registry(exit_code=0)
        marker = self.base / "successor-started-e"
        SUP.main([
            "arm", "--route", str(path_e), "--node", "run",
            "--predecessor-kind", "resource", "--predecessor-id", "fixture-run",
            "--resource-registry", str(registry_e),
            "--successor-command", json.dumps([
                sys.executable, "-c",
                f"import contextlib\nwith contextlib.suppress(OSError):\n"
                f"    open({str(marker)!r},'a').write('x')",
            ]),
        ])
        _code, payload_de = self.run_survey()
        row_d = next(row for row in payload_de["rows"] if row["route_id"] == route_d["route_id"])
        row_e = next(row for row in payload_de["rows"] if row["route_id"] == route_e["route_id"])
        self.assertEqual(row_d["risk"]["tier"], "abandoned")
        self.assertEqual(row_e["risk"]["tier"], "abandoned")
        self.assertGreater(row_d["risk"]["score"], row_e["risk"]["score"])

    def test_survey_keeps_unknown_and_corrupt_rows_fail_soft(self):
        good_route, _good_path = self.two_stage_route(
            route_id="rt-fixturenoledger", route_hash="sha256:fixturenoledger")
        corrupt_route, _corrupt_path = self.two_stage_route(
            route_id="rt-fixturecorrupt0", route_hash="sha256:fixturecorrupt0")
        corrupt_ledger = SUP.ledger_for(corrupt_route)
        corrupt_ledger.journal_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_ledger.journal_path.write_text("not-json\nnot-json-either\n", encoding="utf-8")
        malformed_path = self.base / "not-a-route.json"
        malformed_path.write_text(json.dumps({"note": "not a compiled route"}), encoding="utf-8")

        code, payload = self.run_survey()
        self.assertEqual(code, 0)
        rows_by_id = {row["route_id"]: row for row in payload["rows"]}
        self.assertEqual(rows_by_id[good_route["route_id"]]["risk"]["tier"], "unknown")
        self.assertIn("ledger-absent", rows_by_id[good_route["route_id"]]["risk"]["reasons"])
        self.assertEqual(rows_by_id[corrupt_route["route_id"]]["risk"]["tier"], "unknown")
        self.assertIn("ledger-unreadable", rows_by_id[corrupt_route["route_id"]]["risk"]["reasons"])
        unknown_paths = {row["route_file"] for row in payload["rows"] if row["route_id"] is None}
        self.assertIn(str(malformed_path), unknown_paths)
        self.assertTrue(any(d["path"] == str(malformed_path) for d in payload["diagnostics"]))

    def test_survey_is_read_only_on_cache_mismatch(self):
        route, path = self.two_stage_route()
        registry = self.resource_registry(exit_code=0)
        self.arm_external(path, registry)
        ledger = SUP.ledger_for(route)
        ledger.claim("a" * 32, {"route_id": route["route_id"], "predecessor": "run",
                                "successor": "verify"})
        stale_cache = {
            "schema_version": WS.LEDGER_SCHEMA_VERSION, "route_id": route["route_id"],
            "route_hash": route.get("route_hash", ""), "workflow_state": "CREATED",
            "nodes": {}, "journal_entries": 0, "updated_at": None,
        }
        ledger.state_path.write_text(json.dumps(stale_cache), encoding="utf-8")

        def snapshot():
            rows = {}
            for candidate in sorted(self.base.rglob("*")):
                if candidate.is_file():
                    info = candidate.stat()
                    rows[str(candidate)] = (candidate.read_bytes(), info.st_size, info.st_mtime_ns)
            return rows

        before = snapshot()
        code, _payload = self.run_survey()
        after = snapshot()
        self.assertEqual(code, 0)
        self.assertEqual(before, after)
        # `state()` (the mutating path) would have repaired this cache; confirm the
        # read-only survey really left it exactly as planted.
        self.assertEqual(json.loads(ledger.state_path.read_text(encoding="utf-8")), stale_cache)

    def test_survey_preserves_canonical_and_legacy_discovery(self):
        route = compile_fixture("autopilot-lab", "setup", str(self.base), ["resource-run"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = ROUTE.canonical_routes_dir(root) / f"{route['route_id']}.json"
            ROUTE.write_once(canonical, route)
            legacy = root / "routes" / f"{route['route_id']}.json"
            ROUTE.write_once(legacy, route)
            _code, payload = self.run_survey(artifact_root=root)
            rows = {row["route_file"]: row for row in payload["rows"]}
            c_row = rows[str(canonical)]
            l_row = rows[str(legacy)]
            self.assertEqual(c_row["location"], "canonical")
            self.assertFalse(c_row["read_only"])
            self.assertEqual(l_row["location"], "legacy-routes")
            self.assertTrue(l_row["read_only"])
            self.assertIn("duplicate_locations", c_row)
            self.assertIn("duplicate_locations", l_row)


class TestGateSubjectNotCaller(WorkflowFixture):
    """Defect I and its sibling — the caller's identity must never override the
    route the call names.

    Measured 2026-09-03: the implementation owner ran its own suite, and eight
    fixture releases landed in the REAL `rt-6579b69141dc0c00.gate-release.json`
    under `.agent_reports/.runtime/routes/` because `AGENT_OWNER_ROUTE_FILE` was
    preferred over the route passed in. `close_route` folds that sidecar into the
    route outcome, so those eight would have become part of a real route's
    history. Evidence kept out of tree at
    `<scratchpad>/leaked-gate-release.json`; it is deliberately NOT restored
    beside the route.
    """

    def test_sidecar_is_written_beside_the_route_argument_not_the_callers_route(self):
        route, path = self.two_stage_route(human_gate="frame-review")
        foreign = self.base / "rt-callers-own-route.json"
        foreign.write_text("{}", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENT_OWNER_ROUTE_FILE": str(foreign)}):
            sidecar = SUP.gate_release_sidecar_path(path)
        self.assertEqual(sidecar, path.with_name(path.stem + ".gate-release.json"))
        self.assertFalse(foreign.with_name(foreign.stem + ".gate-release.json").exists())

    def test_a_release_leaves_nothing_beside_the_callers_route(self):
        route, path = self.two_stage_route(human_gate="frame-review")
        jobs, _code = self.block_gate(path, "frame-review")
        foreign = self.base / "rt-callers-own-route.json"
        foreign.write_text("{}", encoding="utf-8")
        env = {"AGENT_OWNER_ROUTE_FILE": str(foreign),
               "AGENT_DISPATCH_REGISTERED_WORKER": "1"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            code = SUP.main(["gate", "--route", str(path), "--gate", "frame-review",
                             "--release", "--by", "headless-owner"])
        self.assertEqual(code, 0)
        self.assertFalse(foreign.with_name(foreign.stem + ".gate-release.json").exists())
        rows = json.loads(
            path.with_name(path.stem + ".gate-release.json").read_text(encoding="utf-8")
        )["gate_releases"]
        self.assertEqual([r["gate"] for r in rows], ["frame-review"])
        self.assertEqual(rows[0]["route_hash"], route["route_hash"])

    def test_no_path_means_no_sidecar_rather_than_a_guess(self):
        """A live `rt-*.json` carries no `route_file`, so the old record fallback
        never fired and the env decided everything. With no path there is simply
        no sidecar; the ledger still holds the release."""
        self.assertIsNone(SUP.gate_release_sidecar_path(None))
        self.assertIsNone(SUP.gate_release_sidecar_path(""))
        _route, path = self.two_stage_route(human_gate="frame-review")
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("route_file", record)

    def test_a_foreign_attempt_id_does_not_choose_the_recipient(self):
        """`AGENT_DISPATCH_ATTEMPT_ID` describes the caller. An owner of route A
        blocking a gate on route B must resolve B's owner row, or the gate is
        delivered to a session that does not own it."""
        _route, path = self.two_stage_route(human_gate="frame-review",
                                            route_id="rt-fixture0000000")
        jobs, _session, _attempt = self.owner_registry(route_id="rt-fixture0000000")
        foreign_meta = ",".join([
            "attempt_id=att-foreign-owner0001", "parent_sid=sess-foreign-depth0",
            "parent_completion_delivery=claude-parent-runtime", "dispatch_depth=1",
            "worker_type=owner", "owner_route_id=rt-someone-elses0", "harness=claude",
            "registered_worker=1", "execution_surface=registered-headless",
        ])
        with jobs.open("a", encoding="utf-8") as fh:
            fh.write("\t".join(["2026-09-03T00:00:01Z", "open", str(self.base),
                                str(self.base), "foreign-owner", foreign_meta]) + "\n")
        rows = SUP._registry_rows(jobs)
        with mock.patch.dict(os.environ,
                             {"AGENT_DISPATCH_ATTEMPT_ID": "att-foreign-owner0001"}):
            row = SUP._owner_row(rows, "rt-fixture0000000")
        self.assertIsNotNone(row)
        self.assertEqual(row["meta"]["attempt_id"], "att-fixtureowner0001")
        self.assertEqual(row["meta"]["parent_sid"], "sess-fixture-depth0")

    def test_the_attempt_id_shortcut_still_wins_for_its_own_route(self):
        _route, path = self.two_stage_route(human_gate="frame-review")
        jobs, _session, _attempt = self.owner_registry(route_id="rt-fixture0000000")
        rows = SUP._registry_rows(jobs)
        with mock.patch.dict(os.environ,
                             {"AGENT_DISPATCH_ATTEMPT_ID": "att-fixtureowner0001"}):
            row = SUP._owner_row(rows, "rt-fixture0000000")
        self.assertEqual(row["meta"]["attempt_id"], "att-fixtureowner0001")

    def test_close_route_folds_the_sidecar_into_the_outcome(self):
        """The commit claimed `close_route` folded the sidecar; nothing read it.
        A release recorded in a file no consumer opens is the same silence
        contract (d) exists to end."""
        route, path = self.two_stage_route(human_gate="frame-review")
        self.block_gate(path, "frame-review")
        env = {"AGENT_DISPATCH_REGISTERED_WORKER": "1"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            SUP.main(["gate", "--route", str(path), "--gate", "frame-review",
                      "--release", "--by", "headless-owner"])
        releases = ROUTE._gate_releases(str(path))
        self.assertEqual([r["gate"] for r in releases], ["frame-review"])
        self.assertEqual(releases[0]["actor_kind"], "headless-owner")

    def test_a_missing_or_malformed_sidecar_folds_to_nothing(self):
        _route, path = self.two_stage_route(human_gate="frame-review")
        self.assertEqual(ROUTE._gate_releases(str(path)), [])
        path.with_name(path.stem + ".gate-release.json").write_text("{not json",
                                                                    encoding="utf-8")
        self.assertEqual(ROUTE._gate_releases(str(path)), [])
        path.with_name(path.stem + ".gate-release.json").write_text(
            json.dumps({"gate_releases": [{"no_gate": 1}, "junk"]}), encoding="utf-8")
        self.assertEqual(ROUTE._gate_releases(str(path)), [])

    def test_no_test_in_this_class_writes_outside_its_temporary_root(self):
        """The leak was a test writing into real runtime state. Pin the invariant."""
        canonical = Path.home() / ".agent_reports"
        route, path = self.two_stage_route(human_gate="frame-review")
        sidecar = SUP.gate_release_sidecar_path(path)
        self.assertTrue(str(sidecar).startswith(str(self.base)))
        self.assertFalse(str(sidecar).startswith(str(canonical)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
