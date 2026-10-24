"""Privacy and liveness composition for ledger-backed interaction observations."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
UTILITIES = Path(__file__).resolve().parents[3] / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))
from fleet import interaction as native_interaction  # noqa: E402
from fleet import render, route_chain  # noqa: E402
from fleet import collectors  # noqa: E402
from fleet.collectors import interaction as interaction_collector  # noqa: E402
from fleet.collectors import procscan  # noqa: E402
from fleet.model import Session, classify_session  # noqa: E402
from fleet.render import _interaction_badge  # noqa: E402


class HumanGateWaitProjectionTest(unittest.TestCase):
    def test_ledger_wait_coexists_with_native_wait_without_public_route_content(self):
        session = Session(harness="claude", pid=1, session_id="parent")
        session.interaction_state = {"kind": "approval", "source": "claude-transcript", "waiting_since": 10}
        session.interaction_observations = [{
            "kind": "decision", "source": "hearting-ledger", "waiting_since": 5,
        }]
        ev = {"pid_alive": True, "proc_start_match": True,
              "interaction_wait": [session.interaction_state, *session.interaction_observations]}
        state, _ = classify_session(ev, 20)
        self.assertEqual(state, "blocked")
        self.assertEqual(_interaction_badge(session), " approval needed + answer needed")
        public = session.to_dict()
        self.assertEqual(public["interaction_observations"], session.interaction_observations)
        projected_waits = json.dumps(public["interaction_observations"] + [public["interaction_state"]])
        self.assertNotIn("route_id", projected_waits)
        self.assertNotIn("question", projected_waits)
        self.assertNotIn("answers", projected_waits)

    def test_identity_guard_still_precedes_wait_classification(self):
        ev = {"pid_alive": False, "proc_start_match": False,
              "interaction_wait": [{"kind": "decision", "source": "hearting-ledger"}]}
        state, _ = classify_session(ev, 20)
        self.assertEqual(state, "dead")


QUESTION_TEXT = "SENTINEL-QUESTION which scope should the change cover"
UNDERSTANDING = "SENTINEL-UNDERSTANDING keep both results as measured."
OPTION_BOTH, OPTION_ONE = "SENTINEL-OPTION-both", "SENTINEL-OPTION-one"
NOTE_TEXT = "SENTINEL-NOTE typed correction"
GATE = "frame-review"


class HumanGateEndToEndTest(unittest.TestCase):
    """Real collect_all -> liveness -> public JSON -> card path.

    Only process scanning, harness enrichers (their external transcripts), the
    herdr query and the usage cache are replaced; the ledger reader, the route
    loader, the shared answer validator, the session join and the classifier run
    as shipped.
    """

    def setUp(self):
        import frame_interview as FI
        import workflow_state as WS
        import work_start
        from route_identity import route_hash, route_id_from_hash
        self.FI, self.WS, self.work_start = FI, WS, work_start
        self.route_hash, self.route_id_from_hash = route_hash, route_id_from_hash
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.jobs = self.base / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir(parents=True)
        self.jobs.write_text("", encoding="utf-8")
        env = {
            "HOME": str(self.base / "home"), "XDG_STATE_HOME": str(self.base / "state"),
            "XDG_CONFIG_HOME": str(self.base / "config"), "XDG_CACHE_HOME": str(self.base / "cache"),
            "CLAUDE_CONFIG_DIR": str(self.base / "claude-home"), "CODEX_HOME": str(self.base / "codex-home"),
            "FLEET_ROUTE_CHAIN_DIR": str(self.base / "chains"),
            "FLEET_CAPABILITY_GROUNDING_DIR": str(self.base / "grounding"),
            "FLEET_INTERACTION_STATE_DIR": str(self.base / "interactions"),
            "FLEET_SESSION_REGISTRY_DIR": str(self.base / "sessions"),
            "FLEET_TITLE_STATE_DIR": str(self.base / "titles"),
            "AGENT_DISPATCH_JOBS": str(self.jobs), "AGENT_HOME": str(self.base / "agent-home"),
            "AGENT_ARTIFACT_ROOT": str(self.base / "artifact-root"),
        }
        patched = mock.patch.dict(os.environ, env)
        patched.start()
        self.addCleanup(patched.stop)
        for key in ("AGENT_WORKFLOW_ROOT", "AGENT_DISPATCH_REGISTERED_WORKER", "AGENT_DISPATCH_DEPTH"):
            os.environ.pop(key, None)
        self.rows = []
        self.aliases = {}
        self.now = time.time()
        self.processes = [subprocess.Popen(["sleep", "300"]) for _ in range(8)]
        self.addCleanup(lambda: [(p.kill(), p.wait()) for p in self.processes])
        from fleet.collectors import dispatch, usage_cache
        from fleet import fleet as fleetmod, session_registry
        self.fleetmod = fleetmod
        for patch in (
            mock.patch.object(procscan, "scan", side_effect=self._scan),
            mock.patch.object(dispatch, "_scan_processes", return_value=[]),
            mock.patch.object(session_registry, "session_aliases",
                              side_effect=lambda harness, pid, **kw: list(self.aliases.get((harness, pid), []))),
            mock.patch.object(usage_cache, "account_usage",
                              return_value={"payload": None, "freshness": "unknown", "observed_at": None}),
            mock.patch.object(fleetmod, "_collect_memory", return_value=None),
            mock.patch.object(fleetmod, "_collect_governor", return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("claude", "codex", "opencode"):
            module = __import__("fleet.collectors." + name, fromlist=["enrich"])
            for attribute in ("enrich", "prepare_tick"):
                if hasattr(module, attribute):
                    patch = mock.patch.object(module, attribute, return_value=None)
                    patch.start()
                    self.addCleanup(patch.stop)
        from fleet.collectors import herdr
        patch = mock.patch.object(herdr, "enrich", return_value={"complete": True})
        patch.start()
        self.addCleanup(patch.stop)
        self.counter = 0

    # -- fixture builders -------------------------------------------------
    def _scan(self, *args, **kwargs):
        sessions = []
        for spec in self.rows:
            session = Session(**spec["kwargs"])
            if spec.get("activity") is not None:
                session._interaction_activity = spec["activity"]
            sessions.append(session)
        return sessions

    def add_session(self, harness, sid, *, child=False, activity=None, cwd=None):
        pid = self.processes[len(self.rows) % len(self.processes)].pid
        self.rows.append({"kwargs": dict(
            harness=harness, pid=pid, cwd=cwd or str(self.base / "work"), session_id=sid,
            started_at=self.now - 5000, is_child=child), "activity": activity})
        return sid

    def make_route(self, label):
        self.counter += 1
        artifact_root = self.base / "artifacts" / ("%s-%d" % (label, self.counter))
        (artifact_root / "shards/frame").mkdir(parents=True)
        record = {
            "schema_version": 2, "dispatch_contract_version": 3, "owner_dispatch_depth": 1,
            "max_dispatch_depth": 2, "capability": "autopilot-code", "capability_mode": "dev",
            "effective_intensity": "standard", "cwd": str(self.base / "work"),
            "artifact_root": str(artifact_root), "human_gates": [GATE],
            "human_gate_bindings": [{"gate": GATE, "node": "plan", "position": "entry"}],
            "work_request": {"text": "fixture %s %d" % (label, self.counter)},
            "nodes": [{"id": "frame", "kind": "worker", "dispatch_depth": 2, "depends_on": []},
                      {"id": "plan", "kind": "worker", "dispatch_depth": 2, "depends_on": ["frame"]}],
        }
        record["route_hash"] = self.route_hash(record)
        record["route_id"] = self.route_id_from_hash(record["route_hash"])
        path = self.base / "routes" / (record["route_id"] + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return {"record": record, "path": path, "artifact_root": artifact_root, "round": 0}

    def compose(self, route, harness, sid, ts, event="compose"):
        line = route_chain.build_line(route["record"], event=event, harness=harness, session_id=sid,
                                      route_file=route["path"], now=ts)
        self.assertTrue(route_chain.append(harness, sid, line))

    def ledger(self, route):
        record = route["record"]
        return self.WS.WorkflowLedger(record["route_id"], record["route_hash"], jobs=self.jobs)

    def iso(self, ts):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

    def append_journal(self, route, ts, workflow_state, evidence):
        record = route["record"]
        self.ledger(route)._append({
            "at": self.iso(ts), "route_id": record["route_id"], "route_hash": record["route_hash"],
            "node": None, "state": None, "workflow_state": workflow_state, "evidence": evidence,
            "actor": "fixture", "pid": os.getpid()})

    def raise_gate(self, route, ts, extra_questions=()):
        """Register the next round's interview and raise frame-review on it."""
        route["round"] += 1
        directory = route["artifact_root"] / ("shards/frame/round-%d" % route["round"])
        directory.mkdir(parents=True, exist_ok=True)
        interview = {
            "schema": self.FI.SCHEMA, "route_id": route["record"]["route_id"], "round": route["round"],
            "understanding": UNDERSTANDING,
            "brief": {field: "" for field in self.FI.BRIEF_FIELDS},
            "questions": [{
                "id": "scope", "topic": "scope", "question": QUESTION_TEXT, "kind": "choice",
                "options": [{"label": OPTION_BOTH, "means": "both"}, {"label": OPTION_ONE, "means": "one"}],
                "recommended": 0, "why": "only the person can say"}, *extra_questions],
        }
        path = directory / "interview.json"
        path.write_text(json.dumps(interview), encoding="utf-8")
        import hashlib
        self.append_journal(route, ts, "BLOCKED_HUMAN_GATE", {
            "gate": GATE, "artifact": str(path), "interview": True, "questions": len(interview["questions"]),
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        route["interview"] = interview
        route["interview_path"] = path
        return path

    def native_reply(self, route, answer=OPTION_ONE, restatement="예"):
        interview = route["interview"]
        return [{"question": "제가 이해한 내용: " + interview["understanding"],
                 "options": ["예 (권장)", "아니오(고쳐 말하기)"], "answer": restatement},
                {"question": QUESTION_TEXT, "options": [OPTION_BOTH, OPTION_ONE], "answer": answer}]

    def record_valid_answer(self, route):
        recorded = self.work_start.record_native_answer(route["record"], self.jobs, self.native_reply(route))
        self.assertIsNotNone(recorded)
        return recorded

    def write_answers(self, route, name="answers.json", **overrides):
        answers = self.FI.answers_template(route["interview"])
        answers.update({"actor_kind": "user", "understanding_confirmed": True})
        answers["answers"] = {"scope": {"choice": 1, "note": ""}}
        answers.update(overrides)
        path = route["interview_path"].parent / name
        path.write_text(json.dumps(answers), encoding="utf-8")
        return path

    # -- observation ------------------------------------------------------
    def snapshot(self):
        # Fleet collection is cache-only. Seed the private projection from the
        # fixture's exact route-chain/journal observations as a producer would.
        sessions = self._scan()
        collectors.apply_session_aliases(sessions)
        interaction_collector.enrich_human_gates(sessions, jobs_path=str(self.jobs), now=self.now)
        items, by_session = [], {}
        for session in sessions:
            for observation in session.interaction_observations:
                item_id = "S2:test:%s:%s:%d" % (session.harness, session.session_id,
                                                  int(observation["waiting_since"] * 1000))
                items.append({"id": item_id, "source": "S2", "kind": observation["kind"],
                              "since": observation["waiting_since"], "question": None,
                              "owner": {"harness": session.harness,
                                        "session_id": session.session_id},
                              "attribution": "lineage"})
                by_session.setdefault((session.harness, session.session_id), []).append(item_id)
        import human_wait_state
        human_wait_state.publish_cache({"items": items, "by_session": by_session,
            "total": len(items), "stale_total": 0, "unattributed": [], "health": [],
            "observed_at": self.now, "complete": True})
        snap = collectors.collect_all(jobs_path=str(self.jobs))
        failures = {key: value for key, value in snap.observations.items()
                    if key.startswith("human-gates") and value.get("state") == "failed"}
        self.assertEqual(failures, {})
        return snap

    def find(self, snap, harness, sid):
        rows = [s for s in snap.sessions if s.harness == harness and s.session_id == sid]
        self.assertEqual(len(rows), 1, (harness, sid))
        return rows[0]

    def public(self, snap, harness, sid):
        text = self.fleetmod._snapshot_json(snap.sessions, snap.jobs, observations={})
        payload = json.loads(text)
        row = next(r for r in payload["sessions"] if r["harness"] == harness and r["session_id"] == sid)
        return row, text

    def card(self, session):
        wide = render._plain(render._session_row(session, narrow=False, name_width=40))
        narrow = render._plain(render._session_row_2line(session, term_width=100)[0])
        return wide, narrow

    def chip(self, harness, sid):
        """(wide-card has answer chip, narrow-card has it, public observations, liveness)."""
        snap = self.snapshot()
        session = self.find(snap, harness, sid)
        wide, narrow = self.card(session)
        row, text = self.public(snap, harness, sid)
        for leaked in (QUESTION_TEXT, UNDERSTANDING, OPTION_BOTH, OPTION_ONE, NOTE_TEXT):
            self.assertNotIn(leaked, wide + narrow + text)
        return ("answer needed" in wide, "answer needed" in narrow,
                row.get("interaction_observations") or [], session.liveness)

    def assert_waiting(self, harness, sid):
        wide, narrow, observed, liveness = self.chip(harness, sid)
        self.assertTrue(wide and narrow, (wide, narrow))
        self.assertEqual([(o["kind"], o["source"]) for o in observed], [("decision", "S2")])
        self.assertEqual(liveness, "blocked")

    def assert_quiet(self, harness, sid):
        wide, narrow, observed, liveness = self.chip(harness, sid)
        self.assertFalse(wide or narrow, (wide, narrow))
        self.assertEqual(observed, [])
        self.assertNotEqual(liveness, "blocked")

    def waiting_route(self, harness, sid="sid-parent-0001", extra_questions=(), **session_kwargs):
        self.add_session(harness, sid, **session_kwargs)
        route = self.make_route(harness)
        self.compose(route, harness, sid, self.now - 1000)
        self.raise_gate(route, self.now - 900, extra_questions)
        return route, sid

    # -- cases ------------------------------------------------------------
    def test_valid_answer_clears_wait_before_any_release_for_each_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness):
                route, sid = self.waiting_route(harness, sid="sid-%s-0001" % harness)
                self.assert_waiting(harness, sid)
                self.record_valid_answer(route)
                # Nothing released the gate: the journal still says blocked.
                resolution = self.WS.human_gate_resolution(self.ledger(route).journal(), GATE)
                self.assertEqual(resolution["status"], "blocked")
                self.assert_quiet(harness, sid)
                self.rows.clear()

    def test_a_first_partial_native_reply_keeps_the_wait(self):
        second = {"id": "order", "topic": "order", "question": "SENTINEL-QUESTION-2 which order first", "kind": "choice",
                  "options": [{"label": "SENTINEL-OPTION-a", "means": "a"}, {"label": "SENTINEL-OPTION-b", "means": "b"}],
                  "recommended": 0, "why": "only the person can say"}
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness):
                route, sid = self.waiting_route(harness, sid="sid-%s-0002" % harness, extra_questions=(second,))
                first_call = self.native_reply(route)                       # the restatement and the first question
                kept = self.work_start.record_native_answer(route["record"], self.jobs, first_call)
                self.assertEqual(kept.name, "answers.native.partial.json")
                self.assertFalse((route["interview_path"].parent / "answers.native.json").exists())
                self.assert_waiting(harness, sid)                           # one question is still unanswered
                kept = self.work_start.record_native_answer(route["record"], self.jobs, [
                    {"question": second["question"], "options": ["SENTINEL-OPTION-a", "SENTINEL-OPTION-b"],
                     "answer": "SENTINEL-OPTION-b"}])
                self.assertEqual(kept.name, "answers.native.json")
                resolution = self.WS.human_gate_resolution(self.ledger(route).journal(), GATE)
                self.assertEqual(resolution["status"], "blocked")           # nothing released the gate
                self.assert_quiet(harness, sid)
                self.rows.clear()

    def test_cancelled_or_closed_route_has_no_chip_for_each_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness, how="cancelled"):
                route, sid = self.waiting_route(harness, sid="sid-%s-cancel" % harness)
                self.assert_waiting(harness, sid)
                self.append_journal(route, self.now - 10, "CANCELLED",
                                    {"gate": GATE, "abandon_reason": "operator-decision"})
                self.assert_quiet(harness, sid)
                self.rows.clear()
            with self.subTest(harness, how="closed"):
                route, sid = self.waiting_route(harness, sid="sid-%s-closed" % harness)
                self.assert_waiting(harness, sid)
                outcome = route["path"].with_name(route["path"].stem + ".outcome.json")
                outcome.write_text(json.dumps({
                    "route_id": route["record"]["route_id"], "route_hash": route["record"]["route_hash"],
                    "closed_at": self.iso(self.now - 5), "terminal_gate_proven": True}), encoding="utf-8")
                self.assert_quiet(harness, sid)
                self.rows.clear()

    def test_unusable_answers_never_clear_or_move_the_wait(self):
        route, sid = self.waiting_route("claude")
        self.assert_waiting("claude", sid)
        # An empty reply through the real bridge records nothing.
        self.assertIsNone(self.work_start.record_native_answer(
            route["record"], self.jobs, self.native_reply(route, answer="")))
        self.assertFalse((route["interview_path"].parent / "answers.native.json").exists())
        self.assert_waiting("claude", sid)
        foreign = self.make_route("claude")
        cases = {
            "acknowledgement": dict(answers={}, accepted=True),
            "empty-answers": dict(answers={}),
            "automatic-source": dict(actor_kind="automatic"),
            "unknown-source": dict(actor_kind="unknown"),
            "other-round": dict(round=route["round"] + 1),
            "other-route": dict(route_id=foreign["record"]["route_id"]),
            "no-such-option": dict(answers={"scope": {"choice": 9, "note": ""}}),
        }
        for name, overrides in cases.items():
            for file_name in ("answers.json", "answers.native.json"):
                with self.subTest(name, file=file_name):
                    path = self.write_answers(route, name=file_name, **overrides)
                    self.assert_waiting("claude", sid)
                    path.unlink()
        # A different session or a worker child row is neither given the chip nor
        # does it change the parent's.
        self.add_session("claude", "sid-bystander-0002")
        self.add_session("claude", "sid-worker-0003", child=True)
        self.add_session("codex", sid)  # same id, another harness
        snap = self.snapshot()
        self.assertTrue(self.card(self.find(snap, "claude", sid))[0].count("answer needed"))
        for other in (("claude", "sid-bystander-0002"), ("claude", "sid-worker-0003"), ("codex", sid)):
            session = self.find(snap, *other)
            self.assertEqual(session.interaction_observations, [])
            self.assertNotIn("answer needed", " ".join(self.card(session)))
        # The same route answered by a real valid reply clears it: the negatives
        # above were not vacuous.
        self.record_valid_answer(route)
        self.assert_quiet("claude", sid)

    def test_a_worker_row_alone_does_not_clear_or_hold_a_wait(self):
        route, sid = self.waiting_route("codex", sid="sid-worker-0010", child=True)
        snap = self.snapshot()
        worker = self.find(snap, "codex", sid)
        self.assertEqual(worker.interaction_observations, [])
        self.assertNotIn("answer needed", " ".join(self.card(worker)))

    def test_previous_round_answer_does_not_answer_the_new_round(self):
        route, sid = self.waiting_route("opencode")
        self.record_valid_answer(route)
        self.assert_quiet("opencode", sid)
        stale = route["interview_path"].parent / "answers.native.json"
        self.assertTrue(stale.is_file())
        # The person asked for a revision: a new raise registers round 2.
        self.append_journal(route, self.now - 700, "BLOCKED_HUMAN_GATE", {
            "gate": GATE, "released_gate": GATE, "decision": "revise", "released_by": "user",
            "actor_kind": "user"})
        self.raise_gate(route, self.now - 600)
        self.assertEqual(route["round"], 2)
        self.assert_waiting("opencode", sid)
        self.write_answers(route, round=1)  # a round-1 answer filed beside round 2
        self.assert_waiting("opencode", sid)
        self.record_valid_answer(route)
        self.assert_quiet("opencode", sid)

    def test_native_approval_and_ledger_decision_clear_independently(self):
        for harness, source in (("claude", "claude-permission"), ("codex", "codex-permissionrequest")):
            with self.subTest(harness):
                sid = "sid-%s-both" % harness
                route, sid = self.waiting_route(harness, sid=sid)
                self.assertTrue(native_interaction.set_wait(sid, harness, "approval", source, self.now - 100))
                snap = self.snapshot()
                session = self.find(snap, harness, sid)
                for text in self.card(session):
                    self.assertIn("approval needed", text)
                    self.assertIn("answer needed", text)
                self.assertEqual(session.liveness, "blocked")
                # Native approval ends (marker cleared); the ledger decision stays.
                self.assertTrue(native_interaction.clear_wait(sid, harness))
                self.assert_waiting(harness, sid)
                # Native approval returns while the decision is answered: only it stays.
                self.assertTrue(native_interaction.set_wait(sid, harness, "approval", source, self.now - 50))
                self.record_valid_answer(route)
                snap = self.snapshot()
                session = self.find(snap, harness, sid)
                for text in self.card(session):
                    self.assertIn("approval needed", text)
                    self.assertNotIn("answer needed", text)
                self.assertEqual(session.liveness, "blocked")
                self.assertTrue(native_interaction.clear_wait(sid, harness))
                self.rows.clear()

    def test_new_transcript_activity_does_not_clear_the_ledger_wait(self):
        harness, sid = "codex", "sid-codex-active"
        route, sid = self.waiting_route(harness, sid=sid, activity=self.now - 1)
        self.assertTrue(native_interaction.set_wait(sid, harness, "approval", "codex-permissionrequest",
                                                   self.now - 100))
        session = self.find(self.snapshot(), harness, sid)
        # Activity after the native marker retires that marker, never the ledger wait.
        self.assertIsNone(session.interaction_state)
        for text in self.card(session):
            self.assertNotIn("approval needed", text)
            self.assertIn("answer needed", text)
        self.assertEqual(session.liveness, "blocked")

    def handover(self, harness, *, started_at, receiver_visible=True, alias=False):
        composer, receiver = "sid-%s-composer" % harness, "sid-%s-receiver" % harness
        self.add_session(harness, composer)
        shown = receiver
        if alias:
            shown = "sid-%s-resumed" % harness
            self.aliases[(harness, self.processes[len(self.rows) % len(self.processes)].pid)] = [receiver]
        if receiver_visible:
            self.add_session(harness, shown)
        route = self.make_route(harness)
        self.compose(route, harness, composer, self.now - 1000)
        self.compose(route, harness, receiver, started_at, event="start")
        self.raise_gate(route, self.now - 900)
        return route, composer, shown

    def test_gate_raised_after_a_handover_belongs_to_the_receiving_session(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness):
                route, composer, receiver = self.handover(harness, started_at=self.now - 950)
                self.assert_quiet(harness, composer)
                self.assert_waiting(harness, receiver)
                self.record_valid_answer(route)
                self.assert_quiet(harness, composer)
                self.assert_quiet(harness, receiver)
                self.rows.clear()

    def test_gate_raised_before_the_receiver_started_stays_with_the_composer(self):
        route, composer, receiver = self.handover("claude", started_at=self.now - 800)
        self.assert_waiting("claude", composer)
        self.assert_quiet("claude", receiver)

    def test_receiver_that_is_not_visible_leaves_the_gate_with_the_composer(self):
        route, composer, _receiver = self.handover("codex", started_at=self.now - 950,
                                                   receiver_visible=False)
        self.assert_waiting("codex", composer)

    def test_receiver_known_by_a_resume_alias_gets_the_gate(self):
        route, composer, shown = self.handover("claude", started_at=self.now - 950, alias=True)
        self.assertTrue(shown.endswith("resumed"))
        self.assert_quiet("claude", composer)
        self.assert_waiting("claude", shown)

    def test_dead_process_never_shows_the_wait(self):
        route, sid = self.waiting_route("claude", sid="sid-dead-0001")
        self.rows[-1]["kwargs"]["pid"] = 2 ** 22 + 12345
        snap = self.snapshot()
        session = self.find(snap, "claude", sid)
        self.assertNotEqual(session.liveness, "blocked")


if __name__ == "__main__":
    unittest.main()
