#!/usr/bin/env python3
"""A refine owner's own `transaction` honours `preview-disposition` (D2).

The refine `review,transaction` route is composed, admitted and begun through the production
paths; only the owner's native result and its registry rows are fixtures. `gate` and `release`
are the real `workflow-supervisor.py`, so the gate journal, epochs and who may release are real.
Kept in its own file: every case builds a real route and cycle, and the cost belongs here rather
than in `artifact_producer.test.py`.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SPEC = importlib.util.spec_from_file_location(
    "producer_fixture_for_owner_preview", Path(__file__).with_name("artifact_producer.test.py"))
PF = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PF)
R, P, L, D, nested = PF.R, PF.P, PF.L, PF.D, PF.nested

# Completing nodes here must not launch detached open-cycle checkpoints into
# fixture roots, including from CLI subprocesses this suite spawns. Such a child
# inherits the owner's attempt tag, so a `release` that checks owner quiescence
# while it is still running reports `owner-process-unverifiable`.
os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")


# The OpenCode adapter always seals its scoped launch config; recovery compares
# that recorded permission and refuses a launch that recorded none.
OPENCODE_OWNER_CONFIG = json.dumps({
    "permissions": [{"action": "read", "resource": "*", "effect": "allow"}],
    "agents": {},
})

NATIVE_OWNER_RESULT = {
    "claude": lambda text: [{"type": "result", "subtype": "success", "is_error": False, "result": text}],
    "codex": lambda text: [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                           {"type": "turn.completed"}],
    "opencode": lambda text: [{"type": "text", "sessionID": "ses_test", "part": {"type": "text", "text": text}},
                              {"type": "step_finish", "sessionID": "ses_test",
                               "part": {"type": "step-finish", "reason": "stop"}}],
}


class OwnerRefineBase(PF.ProducerTestBase):
    """A real refine `review,transaction` route whose depth-1 owner executes the transaction.

    Compose, admission, producer begin, review completion and the gate journal are the
    production paths; only the owner's native result and its registry rows are fixtures.
    `gate`/`release` run the real `workflow-supervisor.py` (a person's release is the
    default actor, a registered worker's is refused).
    """

    PREVIEW = "preview-disposition"

    def setUp(self):
        super().setUp()
        # A worker running these tests carries its own route/attempt binding; the fixture owner has none.
        scrub = mock.patch.dict(os.environ)
        scrub.start()
        self.addCleanup(scrub.stop)
        for key in [k for k in os.environ if k.startswith((
                "AGENT_ROUTE", "AGENT_OWNER", "AGENT_DISPATCH_ATTEMPT", "AGENT_DISPATCH_REGISTERED",
                "AGENT_ARTIFACT_CYCLE", "AGENT_ARTIFACT_OUTPUT", "AGENT_PARENT", "AGENT_WORKFLOW"))]:
            os.environ.pop(key)

    def row(self, status, slug, meta):
        return (f"2026-10-01T00:00:00Z\t{status}\t{R.ROOT}\t{R.ROOT}\t{slug}\t"
                + ",".join(f"{k}={v}" for k, v in meta.items()) + "\n")

    def build(self, harness="claude", *, review=True, close=True, parent="claude-parent-runtime",
              report_rel="owner-report.md", verdict="PASS", route_plan=None):
        os.environ["AGENT_HOME"] = str(R.ROOT)
        self.activate()
        self.harness = harness
        self.route = R.compose_route(
            capability="autopilot-refine", capability_mode=None, shape="staged", graph="review,transaction",
            slug="owner-preview", cwd=R.ROOT, artifact_root=self.root, intensity="standard",
            dispatch_evidence={"tuples": [nested(harness, "codex")]}, unassigned=True,
            work_request={"text": "Refine the README", "owner_harness": harness}, route_plan=route_plan)
        self.path = Path(L.admit_runtime_route(self.root, self.route).route_file)
        # The process-wide descendant scan keys on attempt id. Each build must
        # be distinct both from sibling processes and from an earlier case in
        # this process whose observation helper may still be exiting.
        identity = f"{os.getpid()}-{uuid.uuid4().hex}"
        self.owner, self.child = f"att-preview-owner-{identity}", f"att-preview-review-{identity}"
        self.log = self.jobs.parent / "owner.jsonl"
        self.owner_meta = dict(
            attempt_id=self.owner, worker_type="owner", unit="_kernel/owner", dispatch_depth=1,
            registered_worker=1, harness=harness, owner_route_file=self.path,
            owner_route_id=self.route["route_id"], owner_route_hash=self.route["route_hash"],
            parent_sid="fixture-parent", parent_completion_delivery=parent,
            attempt_schema_version=2, log_file=self.log, workflow_completion="runtime-v1")
        self.jobs.write_text(self.row("open", "owner", self.owner_meta))
        self.cycle = P.begin(self.root, route_file=self.path, capability="autopilot-refine",
                             intensity="standard", jobs=self.jobs, owner_attempt_id=self.owner)
        self.preview = self.write_output(self.cycle, rel="reviews/refine/preview.md", data=b"preview of the edit\n")
        self.report = self.write_output(self.cycle, rel=report_rel, data=b"owner report\n")
        if review:
            node = self.route["nodes"][0]
            child_meta = dict(
                attempt_schema_version=2, attempt_id=self.child, parent_attempt_id=self.owner, dispatch_depth=2,
                transport="headless", execution_surface="registered-headless", registered_worker=1,
                fallback_hop="same-harness-headless", harness="codex", route_id=self.route["route_id"],
                route_hash=self.route["route_hash"], route_node=node["id"], failure_class="pass",
                launch_outcome="reaped-before-publish")
            self.jobs.write_text(self.row("open", "owner", self.owner_meta) + self.row("open", "review", child_meta))
            R.complete_node(self.route, node, node["id"], self.preview, jobs=self.jobs, attempt_id=self.child)
        self.verdict = verdict
        blocker = "none" if verdict == "PASS" else "the gate command was refused, so I did not apply"
        text = f"artifact: {self.report}\nverdict: {verdict}\nblocker: {blocker}"
        self.log.write_text("\n".join(json.dumps(x) for x in NATIVE_OWNER_RESULT[harness](text)) + "\n")
        if close:
            self.close_owner()
        return self

    def three_leg_plan(self):
        """The refine `review,transaction` route as leg #1 of a code -> refine -> code report route plan."""
        import route_plan as RP
        task = Path(self._tmp.name) / "leg-task.md"
        task.write_text("the reusable work request\n")
        code = {"capability": "autopilot-code", "mode": "dev", "shape": "staged", "graph": ["execute", "test"],
                "intensity": None, "why": "x"}
        refine = {"capability": "autopilot-refine", "mode": None, "shape": "staged",
                  "graph": ["review", "transaction"], "intensity": None, "why": "x"}
        report = {**code, "graph": ["report"]}
        frame = {"route_id": "rt-" + "1" * 16, "route_hash": "sha256:" + "2" * 64, "cycle_id": "cyc_" + "3" * 32}
        decision = RP.build_decision(
            frame_route=frame, selected="Go", reason="", briefs=[], intent={"path": "x", "sha256": "0" * 64},
            proposal={"summary": "three", "legs": [code, refine, report], "entry_approvals": []},
            first_leg_compose={"leg": 0, "context": {
                "cwd": str(R.ROOT), "artifact_root": str(self.root), "slug": "owner-preview",
                "campaign_key": "owner-preview", "parent_cycle": frame["cycle_id"],
                "prompt_file": str(task), "prompt_sha256": "0" * 64, "spec_read": "fixture", "owner": None}})
        path = self.root / "decisions" / "route-decision.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(RP.render(RP.build_record(decision)))
        return RP.read_route_plan(f"{path}#1", self.root)

    def close_owner(self):
        lines = self.jobs.read_text().splitlines()
        for i, line in enumerate(lines):
            fields = line.split("\t")
            meta = D.parse_registry_metadata(fields[5])
            if meta.get("attempt_id") != self.owner:
                continue
            blocked = getattr(self, "verdict", "PASS") == "BLOCKED"
            meta.update(execution_surface="registered-headless", transport="headless",
                        fallback_hop="same-harness-headless", failure_class="blocked" if blocked else "pass",
                        note="dead-worker-blocked" if blocked else "completed-supervisor",
                        launch_outcome="reaped-before-publish")
            fields[1] = "done"
            fields[5] = ",".join(f"{k}={v}" for k, v in meta.items())
            lines[i] = "\t".join(fields)
        self.jobs.write_text("\n".join(lines) + "\n")

    def supervisor(self, *argv, worker=False):
        env = {**os.environ, "AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_DISPATCH_JOBS": str(self.jobs)}
        env.pop("AGENT_DISPATCH_REGISTERED_WORKER", None)
        if worker:
            env["AGENT_DISPATCH_REGISTERED_WORKER"] = "1"
        return subprocess.run([sys.executable, str(R.ROOT / "utilities" / "workflow-supervisor.py"), *argv],
                              text=True, capture_output=True, env=env)

    def raise_gate(self, artifact=None):
        done = self.supervisor("gate", "--route", str(self.path), "--gate", self.PREVIEW, "--block",
                               "--jobs", str(self.jobs), "--artifact", str(artifact or self.preview))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def release(self, decision="proceed", worker=False):
        return self.supervisor("release", "--route", str(self.path), "--gate", self.PREVIEW,
                               "--decision", decision, "--jobs", str(self.jobs), "--actor", "user", worker=worker)

    def release_in_process(self, decision="proceed"):
        """`release` run in this process so the continuation (a `start`) can be observed, not spawned."""
        import contextlib
        import io
        spec = importlib.util.spec_from_file_location("sup_for_release", R.ROOT / "utilities" / "workflow-supervisor.py")
        sup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sup)
        self.continuation_calls = []

        def continuation(route_path, jobs):
            import dispatch_replacement as DR
            self.continuation_calls.append({"attempt": self.owner})
            with mock.patch.object(DR, "advance", return_value={"state": "not-applicable"}) as advance:
                self.start(run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no launch")))
            advance.assert_called()
            return 0, {"state": "running"}

        out = io.StringIO()
        with self.env(), mock.patch.object(sup, "start_owner_continuation", continuation), contextlib.redirect_stdout(out):
            sup.main(["release", "--route", str(self.path), "--gate", self.PREVIEW, "--decision", decision,
                      "--actor", "user", "--jobs", str(self.jobs)])
        return json.loads(out.getvalue())

    def resolution(self):
        import workflow_state as WS
        return WS.human_gate_resolution(
            WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs).journal(), self.PREVIEW)

    def workflow_state(self):
        import workflow_state as WS
        return WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs).state()["workflow_state"]

    def env(self):
        return mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_DISPATCH_JOBS": str(self.jobs)})

    def meta(self):
        from dispatch_completion_join import exact_attempt_row
        return exact_attempt_row(self.jobs, self.owner).metadata

    def settle(self):
        import dispatch_terminal_commit as T
        with self.env():
            return T.settle_owner_completion(self.jobs, "done", self.meta())

    def start(self, run=None):
        import work_start as W
        with self.env():
            return W.start_work(self.route, self.path, self.jobs, **({"run": run} if run else {}))

    def gate_records(self):
        return sorted((self.jobs.parent / "pending-delivery").rglob("*.json")) if (self.jobs.parent / "pending-delivery").exists() else []


class OwnerPreviewApprovalTest(OwnerRefineBase):
    """The owner-executed `transaction` honours `preview-disposition`, whatever the harness says.

    A PASS terminal result is a proposal: without a person's release of the current preview it
    must neither settle nor reach `next_leg`. When the owner never raised the gate the main
    session's `start` turns that into the existing question; when the preview cannot be proved
    the receipt says `human-gate-not-raised` instead.
    """

    def test_existing_sealed_route_keeps_preview_gate_and_binding(self):
        fixture = self.build()
        self.assertIn(self.PREVIEW, fixture.route["human_gates"])
        self.assertTrue(any(row.get("gate") == self.PREVIEW for row in fixture.route["human_gate_bindings"]))

    def test_a_pass_with_no_raise_does_not_settle_and_start_asks_the_existing_question_for_any_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture = OwnerPreviewApprovalTest(
                    "test_a_pass_with_no_raise_does_not_settle_and_start_asks_the_existing_question_for_any_harness")
                fixture.setUp()
                try:
                    fixture.build(harness)
                    result = fixture.settle()
                    self.assertNotEqual(result.result, "completed", result)
                    self.assertNotEqual(fixture.workflow_state(), "COMPLETE")
                    self.assertEqual(fixture.resolution()["status"], "not-raised")
                    gates = R.terminal_gate_observation(fixture.route, jobs=fixture.jobs, exact_terminal=True)
                    self.assertFalse(gates["transaction"]["passed"], gates)
                    self.assertIn("human-gate-not-raised", gates["transaction"]["reason"])
                    self.assertEqual(P.read_cycle_record(fixture.root, fixture.cycle["cycle_id"])["state"], "open")
                    receipt = fixture.start()
                    self.assertEqual((receipt["state"], receipt["reason"], receipt["gate"]),
                                     ("waiting-human-gate", "owner-parked-at-human-gate", fixture.PREVIEW), receipt)
                    self.assertEqual(receipt["gate_artifact"], str(fixture.preview))
                    self.assertIn("workflow-supervisor.py", receipt["release_command"])
                    self.assertIn("--decision proceed", receipt["release_command"])
                    self.assertEqual(Path(receipt["owner_report"]).name, "owner-report.md")
                    self.assertTrue(Path(receipt["owner_report"]).is_file())
                    self.assertIn("owner_report", receipt["next_step"])
                    self.assertNotIn("next_leg", receipt)
                    self.assertEqual(fixture.resolution()["status"], "blocked")
                    self.assertEqual(fixture.resolution()["artifact"], str(fixture.preview))
                    self.assertEqual(fixture.resolution()["artifact_sha256"],
                                     hashlib.sha256(fixture.preview.read_bytes()).hexdigest())
                    self.assertNotEqual(fixture.workflow_state(), "COMPLETE")
                finally:
                    fixture.doCleanups()

    def test_the_question_is_raised_once_only_a_person_can_answer_it_and_proceed_settles(self):
        self.build("claude")
        first = self.start()
        records = self.gate_records()
        self.assertEqual(first["state"], "waiting-human-gate", first)
        self.assertEqual(len(records), 1)
        before = records[0].read_bytes()
        second = self.start()                                   # a repeated start does not raise it twice
        self.assertEqual((second["state"], second["gate"]), ("waiting-human-gate", self.PREVIEW))
        self.assertEqual(self.resolution()["epoch"], 1)
        self.assertEqual([p.read_bytes() for p in self.gate_records()], [before])
        refused = self.release("proceed", worker=True)          # the owner cannot answer for the person
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.resolution()["status"], "blocked")
        self.assertNotEqual(self.settle().result, "completed")
        released = self.release("proceed")
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        receipt = self.start()
        self.assertEqual(receipt["state"], "completed", receipt)
        self.assertEqual(self.workflow_state(), "COMPLETE")
        self.assertEqual(P.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_stop_and_revise_never_settle(self):
        for decision, state, reason in (("stop", "stopped", "human-gate-stop"),
                                        ("revise", "needs-attention", "human-gate-revise-owner-parked")):
            with self.subTest(decision=decision):
                fixture = OwnerPreviewApprovalTest("test_stop_and_revise_never_settle")
                fixture.setUp()
                try:
                    fixture.build("claude")
                    fixture.start()
                    self.assertEqual(fixture.release(decision).returncode, 0)
                    receipt = fixture.start()
                    self.assertEqual((receipt["state"], receipt["reason"]), (state, reason), receipt)
                    self.assertNotEqual(fixture.settle().result, "completed")
                    self.assertNotEqual(fixture.workflow_state(), "COMPLETE")
                finally:
                    fixture.doCleanups()

    def test_an_owner_that_raised_and_was_released_settles_as_before(self):
        self.build("claude", close=False)
        self.raise_gate()
        self.assertEqual(self.release("proceed").returncode, 0)
        self.close_owner()
        self.assertEqual(self.settle().result, "completed")
        self.assertEqual(self.workflow_state(), "COMPLETE")

    def test_a_preview_removed_after_its_release_does_not_hold_the_closure(self):
        # The preview is a file of its own, not the review marker's evidence: removing that one is a different refusal.
        for label, released in (("released", True), ("unreleased", False)):
            with self.subTest(case=label):
                fixture = OwnerPreviewApprovalTest(
                    "test_a_preview_removed_after_its_release_does_not_hold_the_closure")
                fixture.setUp()
                try:
                    fixture.build("claude", close=False)
                    preview = fixture.write_output(fixture.cycle, rel="documents/diff-preview.md",
                                                   data=b"--- a/README\n+++ b/README\n")
                    fixture.raise_gate(preview)
                    if released:
                        self.assertEqual(fixture.release("proceed").returncode, 0)
                    preview.unlink()
                    fixture.close_owner()
                    settled = fixture.settle()
                    if label == "released":
                        self.assertEqual(settled.result, "completed", settled)
                        receipt = fixture.start()
                        self.assertEqual(receipt["state"], "completed", receipt)
                        self.assertEqual(fixture.workflow_state(), "COMPLETE")
                    else:
                        # Only a person's `proceed` authorizes the close; the gate-off record never stands in for it.
                        self.assertNotEqual(settled.result, "completed", settled)
                        self.assertNotEqual(fixture.workflow_state(), "COMPLETE")
                finally:
                    fixture.doCleanups()

    def test_an_owner_that_raised_and_exited_without_a_release_is_waiting(self):
        self.build("claude", close=False)
        self.raise_gate()
        self.close_owner()
        self.assertNotEqual(self.settle().result, "completed")
        receipt = self.start()
        self.assertEqual((receipt["state"], receipt["gate"]), ("waiting-human-gate", self.PREVIEW), receipt)
        self.assertEqual(self.resolution()["epoch"], 1)

    def test_a_pass_with_no_review_marker_keeps_the_existing_unfinished_stage_receipt_and_raises_nothing(self):
        self.build("claude", review=False)
        receipt = self.start()
        # A PASS that left a declared stage undone is unfinished work: the runtime tries one `observed`
        # continuation. This fixture's caller is not the route parent, so the continuation is refused at
        # the parent check and the receipt keeps the missing stages; nothing is launched, raised or completed.
        self.assertEqual((receipt["state"], receipt["reason"]), ("needs-attention", "replacement-parent-identity-unproven"), receipt)
        self.assertEqual(receipt["launches"], [])
        self.assertIn("review", receipt["missing_terminal_gates"])
        self.assertNotIn("next_leg", receipt)
        self.assertEqual(self.resolution()["status"], "not-raised")
        self.assertNotEqual(self.settle().result, "completed")
        self.assertNotEqual(self.workflow_state(), "COMPLETE")

    def test_completing_the_owner_node_by_hand_does_not_bypass_the_gate(self):
        self.build("claude", close=False)
        node = next(n for n in self.route["nodes"] if n["id"] == "transaction")
        with self.env(), self.assertRaises(ValueError) as caught:
            R.complete_node(self.route, node, "transaction", self.report, jobs=self.jobs)
        self.assertIn("human-gate-not-raised", str(caught.exception))
        self.assertFalse((R.completion_dir(self.route["route_id"], jobs=self.jobs) / "transaction.json").exists())


PARENTS = (("claude-parent-runtime", "claude"), ("codex-native-queue", "codex"),
           ("codex-managed-gateway", "codex"), ("opencode-turn", "opencode"), ("poll-fallback", "opencode"))
# Parent kinds whose carrier reads the parent's own records, so a raise keeps one.
RECORD_CARRIERS = ("claude-parent-runtime", "opencode-turn")


class LooseOutputLegClosureTest(OwnerRefineBase):
    """Files left where they were written never hold a document leg open, so `start` names the next leg."""

    def test_a_document_leg_with_loose_outputs_closes_and_names_the_next_leg_for_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture = LooseOutputLegClosureTest(
                    "test_a_document_leg_with_loose_outputs_closes_and_names_the_next_leg_for_every_harness")
                fixture.setUp()
                try:
                    fixture.build(harness, close=False, route_plan=fixture.three_leg_plan())
                    self.assertEqual(fixture.route["route_plan"]["index"], 1)
                    preview = fixture.write_output(fixture.cycle, rel="diff-preview.md", data=b"--- a/README\n+++ b/README\n")
                    fixture.raise_gate(preview)
                    self.assertEqual(fixture.release("proceed").returncode, 0)
                    with fixture.env():
                        checkpoint = P.checkpoint(fixture.root, cycle_id=fixture.cycle["cycle_id"])
                    self.assertEqual(checkpoint["status"], "emitted", checkpoint)
                    fixture.close_owner()
                    settled = fixture.settle()
                    self.assertEqual(settled.result, "completed", settled)
                    # The finished leg starts the plan's next leg itself (plan cursor). Readiness is the
                    # route's own sealed evidence and nothing is launched, on any HOME.
                    import work_start as W
                    evidence = {"tuples": (fixture.route.get("dispatch_evidence") or {}).get("tuples") or [],
                                "candidates": fixture.route.get("registered_headless_candidates") or []}
                    with mock.patch.object(W._route_module(), "proposal_readiness", return_value=evidence):
                        receipt = fixture.start(
                            run=lambda command, **kw: subprocess.CompletedProcess(command, 1, "", ""))
                    self.assertEqual(receipt["plan_advanced"]["leg"], 2, receipt)
                    third = json.loads(Path(receipt["plan_advanced"]["route_file"]).read_text(encoding="utf-8"))
                    self.assertEqual((receipt["route_id"], third["route_plan"]["index"]), (third["route_id"], 2))
                    self.assertEqual(P.read_cycle_record(fixture.root, fixture.cycle["cycle_id"])["state"], "sealed")
                    self.assertTrue(preview.is_file())
                    self.assertEqual(hashlib.sha256(preview.read_bytes()).hexdigest(),
                                     fixture.resolution()["artifact_sha256"])
                    with fixture.env():
                        gates = R.terminal_gate_observation(fixture.route, jobs=fixture.jobs, exact_terminal=True)
                    self.assertTrue(gates["transaction"]["passed"], gates)
                    self.assertEqual(Path(gates["transaction"]["evidence"]), fixture.report)
                    self.assertTrue(fixture.report.is_file())
                    self.assertFalse((P.producer_dir(fixture.root) / "bucket-placements"
                                      / f"{fixture.cycle['cycle_id']}.json").exists())
                finally:
                    fixture.doCleanups()


class OwnerGateReachesEveryParentTest(OwnerRefineBase):
    """The existing question reaches the person on every harness, with no dead end (D2, delivery).

    The runtime raises `preview-disposition` inside the parent's own `start`; the receipt that call
    returns is the delivery, so a parent kind with no push carrier still asks. `poll-fallback` (what an
    OpenCode depth-0 parent without the plugin carrier gets) is polled the same way and takes its owner's
    own `gate --block` with no record; a record-reading carrier (`claude-parent-runtime`, `opencode-turn`)
    also keeps a record; `codex-stop-hook` keeps its typed refusal, see `workflow_supervisor.test.py`.
    """

    def fresh(self, name, *args, **kwargs):
        fixture = type(self)(name)
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture.build(*args, **kwargs)

    def assert_not_settled(self, fixture):
        settled = fixture.settle()                      # a BLOCKED owner has nothing to settle at all
        self.assertTrue(settled is None or settled.result != "completed", settled)
        self.assertNotEqual(fixture.workflow_state(), "COMPLETE")

    def no_launch(self, *a, **k):
        raise AssertionError("asking the question never launches or replaces an owner")

    def assert_asks(self, fixture, receipt, *, records):
        self.assertEqual((receipt["state"], receipt["reason"], receipt["required_action"], receipt["gate"]),
                         ("waiting-human-gate", "owner-parked-at-human-gate", "answer-human-gate", fixture.PREVIEW),
                         receipt)
        self.assertEqual(receipt["gate_artifact"], str(fixture.preview))
        for token in ("workflow-supervisor.py", "--gate " + fixture.PREVIEW, "--decision proceed", str(fixture.jobs)):
            self.assertIn(token, receipt["release_command"])
        self.assertEqual(Path(receipt["owner_report"]).name, "owner-report.md")
        self.assertNotIn("next_leg", receipt)
        self.assertNotIn("parent_next", receipt)
        resolution = fixture.resolution()
        self.assertEqual((resolution["status"], resolution["epoch"], resolution["artifact"]),
                         ("blocked", 1, str(fixture.preview)))
        self.assertEqual(len(fixture.gate_records()), records)
        self.assertNotEqual(fixture.workflow_state(), "COMPLETE")

    def test_a_pass_that_never_raised_asks_in_the_start_receipt_for_every_parent_kind(self):
        for kind, harness in PARENTS:
            with self.subTest(parent=kind):
                fixture = self.fresh("test_a_pass_that_never_raised_asks_in_the_start_receipt_for_every_parent_kind",
                                     harness, parent=kind)
                first = fixture.start(run=self.no_launch)
                self.assert_asks(fixture, first, records=1 if kind in RECORD_CARRIERS else 0)
                again = fixture.start(run=self.no_launch)        # a repeated start asks, never raises twice
                self.assert_asks(fixture, again, records=1 if kind in RECORD_CARRIERS else 0)
                self.assert_not_settled(fixture)

    def test_a_blocked_owner_that_obeyed_the_refusal_line_is_asked_about_never_replaced(self):
        # Owner row `done`, verdict BLOCKED, gate never raised: the same runtime raise, recognised as the
        # existing owner park. No replacement, relaunch or settlement happens before the person answers.
        import dispatch_replacement as DR
        for kind, harness in PARENTS:
            with self.subTest(parent=kind):
                fixture = self.fresh("test_a_blocked_owner_that_obeyed_the_refusal_line_is_asked_about_never_replaced",
                                     harness, parent=kind, verdict="BLOCKED")
                rows = fixture.jobs.read_text()
                with mock.patch.object(DR, "advance", side_effect=AssertionError("a blocked gate never continues")):
                    first = fixture.start(run=self.no_launch)
                    again = fixture.start(run=self.no_launch)
                for receipt in (first, again):
                    self.assert_asks(fixture, receipt, records=1 if kind in RECORD_CARRIERS else 0)
                self.assertEqual(fixture.jobs.read_text(), rows, "no row was added or changed")
                self.assert_not_settled(fixture)
                self.assertEqual(DR.owner_parked_gate(fixture.jobs, fixture.owner)["status"], "blocked")

    def test_the_receipt_says_what_a_proceed_will_really_do_for_each_owner_result(self):
        for verdict, expected, forbidden in (
                ("PASS", "settles the owner's finished work", "starts the continuation"),
                ("BLOCKED", "starts the continuation", "settles the owner's finished work")):
            with self.subTest(verdict=verdict):
                fixture = self.fresh("test_the_receipt_says_what_a_proceed_will_really_do_for_each_owner_result",
                                     "opencode", parent="opencode-turn", verdict=verdict)
                text = fixture.start(run=self.no_launch)["next_step"]
                self.assertIn(expected, text)
                self.assertNotIn(forbidden, text)

    def test_a_proceed_settles_a_passed_owner_for_every_parent_kind(self):
        for kind, harness in PARENTS:
            with self.subTest(parent=kind):
                fixture = self.fresh("test_a_proceed_settles_a_passed_owner_for_every_parent_kind", harness, parent=kind)
                fixture.start(run=self.no_launch)
                released = fixture.release("proceed")
                self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
                receipt = fixture.start(run=self.no_launch)
                self.assertEqual(receipt["state"], "completed", receipt)
                self.assertEqual(fixture.workflow_state(), "COMPLETE")
                self.assertEqual(P.read_cycle_record(fixture.root, fixture.cycle["cycle_id"])["state"], "sealed")

    def test_revise_and_stop_are_handled_for_every_parent_kind_and_owner_result(self):
        import dispatch_replacement as DR
        for kind, harness in PARENTS:
            for verdict in ("PASS", "BLOCKED"):
                for decision, state, reason in (("stop", "stopped", "human-gate-stop"),
                                                ("revise", "needs-attention", "human-gate-revise-owner-parked")):
                    with self.subTest(parent=kind, verdict=verdict, decision=decision):
                        fixture = self.fresh("test_revise_and_stop_are_handled_for_every_parent_kind_and_owner_result",
                                             harness, parent=kind, verdict=verdict)
                        fixture.start(run=self.no_launch)
                        self.assertEqual(fixture.release(decision).returncode, 0)
                        with mock.patch.object(DR, "advance", side_effect=AssertionError("never continues")):
                            receipt = fixture.start(run=self.no_launch)
                        self.assertEqual((receipt["state"], receipt["reason"]), (state, reason), receipt)
                        self.assertEqual(Path(receipt["owner_report"]).name, "owner-report.md")
                        self.assertNotIn("next_leg", receipt)
                        self.assert_not_settled(fixture)

    def test_a_proceed_after_a_blocked_owner_enters_the_existing_replacement_path(self):
        import dispatch_replacement as DR
        for kind, harness in PARENTS:
            with self.subTest(parent=kind):
                fixture = self.fresh("test_a_proceed_after_a_blocked_owner_enters_the_existing_replacement_path",
                                     harness, parent=kind, verdict="BLOCKED")
                fixture.start(run=self.no_launch)
                payload = fixture.release_in_process("proceed")
                self.assertEqual(payload["decision"], "proceed")
                self.assertEqual(payload["owner_continuation"]["state"], "running")
                self.assertEqual(DR.owner_parked_gate(fixture.jobs, fixture.owner)["status"], "proceed")
                fields, meta = DR._rows(fixture.jobs.read_text().splitlines())[fixture.owner]
                self.assertEqual(DR.death_kind(fields, meta, jobs=fixture.jobs), "parked")
                self.assertEqual(fixture.continuation_calls[0]["attempt"], fixture.owner)

    # Every `required_action` token `utilities/work_start.py` emitted at base 237a3e96 (the literals in it,
    # plus `delivery_required_action`'s own). A receipt may only use one of these: a new token is a new
    # thing every consumer would have to learn.
    BASE_REQUIRED_ACTIONS = frozenset({
        "advance-completed", "answer-human-gate", "ask-registered-question", "compose-again", "compose-route",
        "execute-inline", "inspect-closed-route", "inspect-preparation", "inspect-recovery",
        "prepare-frame-question", "report-capacity-wait", "report-gate-revision", "report-pending-work",
        "report-unfinished-work", "report-unresolved-frame", "resume-after-capacity", "resume-inline-finish",
        "revise-frame-question", "wait-for-first-frame-attempt", "wait-for-frame-results"})

    def base_required_actions(self):
        return self.BASE_REQUIRED_ACTIONS

    def test_a_raise_the_runtime_cannot_make_keeps_the_attention_receipt_and_asks_nothing(self):
        # The one case that still ends at `human-gate-not-raised`: the runtime tried to ask and the raise itself
        # was refused (a preview that stopped being provable at that moment, a ledger that cannot enter the
        # gate, a timeout). There is nothing the person could be shown, so nothing is settled, raised or
        # released. A missing review stage is not this case: the runtime continues it once, or keeps its missing-stage receipt.
        import dispatch_contract as DC
        for kind, harness in PARENTS:
            with self.subTest(parent=kind):
                fixture = self.fresh("test_a_raise_the_runtime_cannot_make_keeps_the_attention_receipt_and_asks_nothing",
                                     harness, parent=kind)
                with mock.patch.object(DC, "raise_preview_gate_for_node",
                                       side_effect=ValueError("gate-carrier-refused: ledger cannot enter the gate")):
                    receipt = fixture.start(run=self.no_launch)
                self.assertEqual((receipt["state"], receipt["reason"], receipt["required_action"], receipt["gate"]),
                                 ("needs-attention", "human-gate-not-raised", "report-unfinished-work",
                                  fixture.PREVIEW), receipt)
                # no action token the base did not already emit (a new one is a new thing for consumers to learn)
                self.assertIn(receipt["required_action"], self.base_required_actions())
                self.assertEqual(Path(receipt["owner_report"]).name, "owner-report.md")
                self.assertIn("ledger cannot enter the gate", receipt["next_step"])
                self.assertNotIn("next_leg", receipt)
                self.assertEqual(fixture.resolution()["status"], "not-raised")
                self.assertEqual(fixture.gate_records(), [])
                self.assertNotEqual(fixture.workflow_state(), "COMPLETE")


def polling_parent_kind():
    """What the production selector seals on an owner an OpenCode depth-0 session launches."""
    import dispatch_parent_completion as DPC
    args = SimpleNamespace(action="start", dispatch_depth=1, execution_surface="registered-headless",
                           registered_worker=True, parent_session_id="fixture-parent", parent_harness="opencode")
    with mock.patch.dict(os.environ, {"OPENCODE_SESSION_ID": "fixture-parent"}, clear=False):
        for key in ("AGENT_DISPATCH_CHILD", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "AGENT_DISPATCH_ATTEMPT_ID"):
            os.environ.pop(key, None)
        return DPC.resolve_parent_completion_delivery(args)


class PollingParentBase(OwnerRefineBase):
    """An OpenCode depth-0 parent has no push carrier: it polls `capability-route.py start --wait`."""

    def owner_env(self):
        return {**os.environ, "AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_DISPATCH_JOBS": str(self.jobs),
                "AGENT_DISPATCH_REGISTERED_WORKER": "1", "AGENT_DISPATCH_ATTEMPT_ID": self.owner,
                "AGENT_DISPATCH_CURRENT_HARNESS": "opencode"}

    def owner_block(self):
        """The owner's own `gate --block`, as the runtime-rendered owner prompt tells it to run."""
        return subprocess.run([sys.executable, str(R.ROOT / "utilities" / "workflow-supervisor.py"), "gate",
                               "--route", str(self.path), "--gate", self.PREVIEW, "--block", "--jobs", str(self.jobs),
                               "--artifact", str(self.preview)], text=True, capture_output=True, env=self.owner_env())

    def build_polling(self, **kwargs):
        kind = polling_parent_kind()
        self.assertEqual(kind, "poll-fallback")                  # the real kind, not a fixture's invention
        self.build("opencode", parent=kind, **kwargs)
        self.assertEqual(self.meta()["parent_completion_delivery"], "poll-fallback")

    def wait_while_owner_exits(self, verdict):
        """A parent's `start --wait`: the owner obeys "end your turn" and exits while the call joins it."""
        import work_start as W
        real, closed = W.join_selected_attempts, []

        def join(**kw):
            if not closed:
                self.verdict = verdict
                self.close_owner()
                closed.append(1)
            return real(**{**kw, "timeout": 0})
        with self.env(), mock.patch.dict(os.environ, {"OPENCODE_SESSION_ID": "fixture-parent"}), \
                mock.patch.object(W, "join_selected_attempts", join):
            receipt = W.start_work(self.route, self.path, self.jobs, wait=True,
                                   run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no launch")))
        self.assertEqual(closed, [1])
        return receipt

    def assert_asks(self, receipt, artifact=None):
        self.assertEqual((receipt.get("state"), receipt.get("reason"), receipt.get("required_action"),
                          receipt.get("gate")),
                         ("waiting-human-gate", "owner-parked-at-human-gate", "answer-human-gate", self.PREVIEW),
                         receipt)
        self.assertEqual(receipt["gate_artifact"], str(artifact or self.preview))
        for token in ("workflow-supervisor.py", "--gate " + self.PREVIEW, "--decision proceed", str(self.jobs)):
            self.assertIn(token, receipt["release_command"])
        self.assertNotIn("next_leg", receipt)


class OwnerGateReachesAPollingParentTest(PollingParentBase):
    """An OpenCode depth-0 parent polls `start --wait`; the owner's own raise must arrive there (D2)."""

    def test_an_opencode_owner_raise_reaches_the_polling_parent_in_its_wait_receipt(self):
        self.build_polling(close=False)
        raised = self.owner_block()
        self.assertEqual(raised.returncode, 0, raised.stderr)
        self.assertIsNone(json.loads(raised.stdout)["delivery"])       # no new carrier, no record
        self.assertEqual(self.gate_records(), [])
        resolution = self.resolution()
        self.assertEqual((resolution["status"], resolution["epoch"], resolution["artifact"]),
                         ("blocked", 1, str(self.preview)))
        self.assertTrue(resolution["artifact_sha256"])
        self.assert_asks(self.wait_while_owner_exits("BLOCKED"))
        # approval is still never skipped
        import dispatch_contract as DC
        node = next(n for n in self.route["nodes"] if n["id"] == "transaction")
        with self.env(), self.assertRaises(DC.DispatchContractError) as fenced:
            DC.owner_operation_fence(self.route, node, jobs=self.jobs)
        self.assertEqual(fenced.exception.reason, "human-gate-unreleased")
        self.assertNotEqual(self.release("proceed", worker=True).returncode, 0)
        self.assertEqual(self.resolution()["status"], "blocked")
        payload = self.release_in_process("proceed")
        self.assertEqual(payload["owner_continuation"]["state"], "running", payload)
        import dispatch_replacement as DR
        self.assertEqual(DR.owner_parked_gate(self.jobs, self.owner)["status"], "proceed")

    def test_a_blocked_owner_with_its_review_marker_is_asked_on_a_polling_parent(self):
        self.build_polling(verdict="BLOCKED")
        receipt = self.start(run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no launch")))
        self.assert_asks(receipt)
        self.assertEqual(self.gate_records(), [])


class InlineReviewOwnerSettlesThroughAPollingParentTest(PollingParentBase):
    """An owner that ran `review` itself reaches settlement through a polling parent (round 1).

    The OpenCode r3 owner ran review inline and nothing told it to publish the marker. With the marker
    (the existing `capability-route.py complete --execution-surface inline`) the same run asks, waits,
    continues after `proceed` and settles; without it settlement still refuses the unmarked stage.
    """

    def seal_owner_input(self):
        """The adapter seals the owner's raw launch input before its first registry write."""
        import dispatch_replacement as DR
        prompt = self.jobs.parent / "owner-task.md"
        prompt.write_text("Refine the README\n")
        argv = ["--start", "--worktree", str(R.ROOT), "--slug", "owner", "--capability", "autopilot-refine",
                "--worker-type", "owner", "--dispatch-depth", "1", "--unit", "_kernel/owner",
                "--owner", "autopilot-refine", "--route-file", str(self.path),
                "--parent-session-id", "fixture-parent", "--prompt-file", str(prompt)]
        args = SimpleNamespace(attempt_id=self.owner, jobs_path=self.jobs, worktree=str(R.ROOT),
                               route_id=self.route["route_id"], route_node="", replacement_input_argv=argv,
                               opencode_config_content=OPENCODE_OWNER_CONFIG)
        extra = D.parse_registry_metadata(DR.seal_launch_input(args, "opencode", "Refine the README\n").lstrip(","))
        lines = self.jobs.read_text().splitlines()
        fields = lines[0].split("\t")
        meta = D.parse_registry_metadata(fields[5])
        meta.update(extra)
        fields[5] = ",".join(f"{k}={v}" for k, v in meta.items())
        lines[0] = "\t".join(fields)
        self.jobs.write_text("\n".join(lines) + "\n")
        self.owner_meta.update(extra)

    def inline_review_marker(self, evidence):
        """The documented inline-stage marker, run from inside the owner session."""
        node = next(n for n in self.route["nodes"] if n["id"] == "review")
        return subprocess.run([sys.executable, str(R.ROOT / "utilities" / "capability-route.py"), "complete",
                               "--route", str(self.path), "--node", "review", "--evidence", str(evidence),
                               "--attempt-id", self.owner + "-review-inline",
                               "--dispatch-depth", str(node["dispatch_depth"]), "--transport", "headless",
                               "--execution-surface", "inline", "--registered-worker", "0",
                               "--fallback-hop", "inline"], text=True, capture_output=True, env=self.owner_env())

    def publish_inline_marker(self):
        """Returns the marker's evidence, the artifact a parent-side raise binds the gate to."""
        verdict_file = self.write_output(self.cycle, rel="reviews/refine-verdict.md", data=b"verdict: PASS\n")
        done = self.inline_review_marker(verdict_file)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        marker = json.loads((R.completion_dir(self.route["route_id"], jobs=self.jobs) / "review.json").read_text())
        self.assertEqual((marker["registered_worker"], marker["execution_surface"], marker["fallback_hop"],
                          marker["reviewer_kind"], marker["review_independence"]),
                         (False, "inline", "inline", "owner-inline", "degraded"))
        return verdict_file

    def fake_launch(self, command, **kwargs):
        """Register the continuation owner like the adapter would (real seal + replacement_row)."""
        import dispatch_replacement as DR
        from datetime import datetime, timezone
        self.launches.append(command)
        argv = command[2:]

        def value(flag):
            return argv[argv.index(flag) + 1]
        aid, prior = value("--attempt-id"), value("--automatic-retry-of")
        source = DR._rows(self.jobs.read_text().splitlines())[prior][1]
        task = Path(value("--prompt-file")).read_text()
        args = SimpleNamespace(attempt_id=aid, jobs_path=self.jobs, worktree=value("--worktree"),
                               route_id=self.route["route_id"], route_node="owner", replacement_input_argv=list(argv),
                               opencode_config_content=OPENCODE_OWNER_CONFIG)
        meta = {k: v for k, v in source.items() if k not in {
            "note", "failure_class", "launch_outcome", "replacement_input_digest", "replacement_family_id",
            "replacement_attempt_id", "replacement_claim_digest", "replacement_original_attempt_id",
            "replacement_ordinal", "automatic_retry_of", "log_file", "cleanup_receipt_digest"}}
        self.log2 = self.jobs.parent / (aid + ".jsonl")
        meta.update(attempt_id=aid, automatic_retry_of=prior, launch_claimed="1", log_file=str(self.log2))
        meta.update(D.parse_registry_metadata(DR.seal_launch_input(args, source["harness"], task)))
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = stamp + "\topen\t12\tparent\ttask\t" + ",".join(k + "=" + str(v) for k, v in meta.items())
        with DR._locked(self.jobs) as lines:
            row, _ = DR.replacement_row(self.jobs, lines, row)
            with self.jobs.open("a") as f:
                f.write(row + "\n")
        self.continuation_aid = aid
        # the wrapper publishes the producer binding at owner launch (build() does the same for the first owner)
        P.begin(self.root, route_file=self.path, capability="autopilot-refine", intensity="standard",
                jobs=self.jobs, owner_attempt_id=aid)
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    def release_with_continuation(self):
        """`release proceed`, with only the continuation's launcher replaced by one that registers the row."""
        import contextlib
        import io
        import work_start as W
        spec = importlib.util.spec_from_file_location("sup_for_inline_review", R.ROOT / "utilities" / "workflow-supervisor.py")
        sup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sup)

        def continuation(route_path, jobs):
            from route_parent_close import owner_continuation_processes
            def observe(meta, jobs):
                if (meta.get("attempt_id") == getattr(self, "continuation_aid", None)
                        and W._rows(jobs)[meta["attempt_id"]][0] in {"open", "running"}):
                    return D.ProcessQuiescence("live", "mock-owner-running"), []
                return owner_continuation_processes(meta, jobs)
            with self.env(), mock.patch.dict(os.environ, {"OPENCODE_SESSION_ID": "fixture-parent"}), \
                    mock.patch("route_parent_close.owner_continuation_processes", side_effect=observe):
                return 0, W.start_work(self.route, self.path, self.jobs, run=self.fake_launch)
        out = io.StringIO()
        with self.env(), mock.patch.object(sup, "start_owner_continuation", continuation), contextlib.redirect_stdout(out):
            sup.main(["release", "--route", str(self.path), "--gate", self.PREVIEW, "--decision", "proceed",
                      "--actor", "user", "--jobs", str(self.jobs)])
        return json.loads(out.getvalue())

    def continuation_passes(self):
        """The continuation owner applies the edit and returns PASS."""
        text = f"artifact: {self.report}\nverdict: PASS\nblocker: none"
        self.log2.write_text("\n".join(json.dumps(x) for x in NATIVE_OWNER_RESULT["opencode"](text)) + "\n")
        lines = self.jobs.read_text().splitlines()
        for i, line in enumerate(lines):
            fields = line.split("\t")
            meta = D.parse_registry_metadata(fields[5])
            if meta.get("attempt_id") == self.continuation_aid:
                meta.update(execution_surface="registered-headless", transport="headless",
                            fallback_hop="same-harness-headless", failure_class="pass",
                            note="completed-supervisor", launch_outcome="reaped-before-publish")
                fields[1] = "done"
                fields[5] = ",".join(f"{k}={v}" for k, v in meta.items())
                lines[i] = "\t".join(fields)
        self.jobs.write_text("\n".join(lines) + "\n")
        with mock.patch.dict(os.environ, {"OPENCODE_SESSION_ID": "fixture-parent"}):
            return self.start(run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no second launch")))

    def run_blocked_owner(self, *, marker):
        self.launches = []
        self.build_polling(review=False, close=False)
        self.seal_owner_input()
        if marker:
            self.publish_inline_marker()
        raised = self.owner_block()
        self.assertEqual(raised.returncode, 0, raised.stderr)           # HEAD: 64 gate-recipient-unresolved
        self.assertEqual(self.gate_records(), [])
        self.assert_asks(self.wait_while_owner_exits("BLOCKED"))
        payload = self.release_with_continuation()
        self.assertEqual(payload["owner_continuation"]["state"], "running", payload)
        self.assertEqual(len(self.launches), 1)
        import dispatch_replacement as DR
        row = DR._rows(self.jobs.read_text().splitlines())[self.continuation_aid][1]
        self.assertEqual(row["automatic_retry_of"], self.owner)
        return self.continuation_passes()

    def test_an_inline_review_owner_raises_waits_and_settles_after_proceed(self):
        receipt = self.run_blocked_owner(marker=True)
        self.assertEqual(receipt["state"], "completed", receipt)
        self.assertEqual(len(self.launches), 1)
        import dispatch_terminal_commit as T
        from dispatch_completion_join import exact_attempt_row
        meta = exact_attempt_row(self.jobs, self.continuation_aid).metadata
        with self.env():
            settled = T.settle_owner_completion(self.jobs, "done", meta)
            state = T.owner_completion_state(self.jobs, "done", meta)
        self.assertEqual((settled.result, settled.terminal_nodes), ("completed", ("transaction",)))
        self.assertFalse(str(state.reason).startswith("owner-prerequisite-unproven"), state)
        self.assertEqual(self.workflow_state(), "COMPLETE")
        self.assertEqual(P.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_without_the_inline_marker_the_same_run_stops_before_settlement(self):
        receipt = self.run_blocked_owner(marker=False)
        self.assertEqual((receipt["state"], receipt.get("reason")), ("needs-attention", "workflow-executor-exited"),
                         receipt)
        self.assertEqual(receipt["missing_terminal_gates"], {"review": "completion-marker-absent"})
        import dispatch_terminal_commit as T
        from dispatch_completion_join import exact_attempt_row
        with self.env():
            state = T.owner_completion_state(self.jobs, "done", exact_attempt_row(self.jobs, self.continuation_aid).metadata)
        self.assertTrue(str(state.reason).startswith("owner-prerequisite-unproven"), state)
        self.assertNotEqual(self.workflow_state(), "COMPLETE")

    def test_a_parent_side_raise_accepts_the_inline_marker(self):
        self.build_polling(review=False, close=False)
        evidence = self.publish_inline_marker()
        self.verdict = "PASS"
        self.close_owner()                                   # the owner never raised the gate itself
        receipt = self.start(run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no launch")))
        self.assert_asks(receipt, artifact=evidence)
        self.assertEqual(self.release("proceed").returncode, 0)
        receipt = self.start(run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no launch")))
        self.assertEqual(receipt["state"], "completed", receipt)
        self.assertEqual(self.workflow_state(), "COMPLETE")


if __name__ == "__main__":
    unittest.main()
