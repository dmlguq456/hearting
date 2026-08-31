"""F-25 — single state classifier (PRD v8 §4.8).

Every case is HERMETIC: fixtures carry pre-collected evidence dicts, so no test touches
/proc, ~/.claude, or the clock. `reset_state_tracker()` in setUp stops cross-tick dwell
state from leaking between cases (R7).

The fixtures under fixtures/state_model/ pin the instability cases this feature exists to
fix — each one's `_case` field says which real observation it reproduces.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import model                                          # noqa: E402
from fleet.collectors import dispatch, liveness                  # noqa: E402
from fleet.model import DispatchJob, Session                     # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "state_model")


def load(name):
    with open(os.path.join(FIX, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


class FixtureBase(unittest.TestCase):
    def setUp(self):
        model.reset_state_tracker()

    def check(self, state, ev, expect, label=""):
        self.assertEqual(state, expect["state"], "%s state" % label)
        self.assertEqual(ev["state"], state, "%s evidence/state 불일치" % label)
        if "tier" in expect:
            self.assertEqual(ev["tier"], expect["tier"], "%s tier" % label)
        if "source" in expect:
            self.assertEqual(ev["source"], expect["source"], "%s source" % label)
        if "raw_status" in expect:
            self.assertEqual(ev["raw_status"], expect["raw_status"], "%s raw_status" % label)
        # derived ⇔ tier 3 — the same source condition the render layer's `~` prefix uses.
        self.assertEqual(ev["derived"], ev["tier"] == 3, "%s derived⇔tier3 불변식" % label)


class SessionFixtureTest(FixtureBase):
    """Single-tick session cases."""

    def _run(self, name):
        fx = load(name)
        state, ev = model.classify_session(fx["input"], fx["now"],
                                           key=tuple(fx["key"]) if fx.get("key") else None)
        self.check(state, ev, fx["expect"], name)
        return state, ev

    def test_ghost_unused(self):
        """F-26 acceptance shape — the real pid 1168514 renders as unused, tier 1."""
        state, ev = self._run("ghost_unused")
        self.assertLessEqual(ev["inputs"]["activity_ms"], model.UNUSED_ACTIVITY_MS)
        self.assertFalse(ev["inputs"]["transcript"])
        self.assertEqual(ev["raw_status"], "idle")   # registry still says idle — unused refines it

    def test_registry_fresh_no_status(self):
        """Missing status/updatedAt keys tolerate rather than crash."""
        self._run("registry_fresh_no_status")

    def test_registry_busy_no_transcript(self):
        """Axis guard — busy is never narrowed to unused."""
        self._run("registry_busy_no_transcript")

    def test_pid_reuse(self):
        """start-time mismatch discards registry evidence → dead (fail closed)."""
        self._run("pid_reuse")

    def test_snapshot_single_tick(self):
        """--json/--once: first observation commits immediately, hysteresis is a no-op."""
        _state, ev = self._run("snapshot_single_tick")
        self.assertIsNone(ev["hysteresis"])


class SessionPairFixtureTest(FixtureBase):
    def test_codex_fd_owner_pair(self):
        fx = load("codex_fd_owner_pair")
        for case in fx["cases"]:
            state, ev = model.classify_session(case["input"], fx["now"])
            self.check(state, ev, case["expect"], case["_case"])
        # Both rows survive: fd ownership attributes telemetry, it never removes a session.
        states = [model.classify_session(c["input"], fx["now"])[0] for c in fx["cases"]]
        self.assertNotIn("dead", states)


class HysteresisSequenceTest(FixtureBase):
    """Multi-tick sequences — the dwell contract."""

    def _run_seq(self, name):
        fx = load(name)
        key = tuple(fx["key"])
        for tick in fx["ticks"]:
            state, ev = model.classify_session(tick["input"], tick["now"], key=key)
            exp = tick["expect"]
            self.assertEqual(state, exp["state"], tick["_tick"])
            if "tier" in exp:
                self.assertEqual(ev["tier"], exp["tier"], tick["_tick"])
            if exp.get("pending"):
                self.assertIsNotNone(ev["hysteresis"], tick["_tick"])
                self.assertEqual(ev["hysteresis"]["pending"], exp["pending"], tick["_tick"])
                self.assertEqual(ev["hysteresis"]["dwell_sec"], exp["dwell_sec"], tick["_tick"])
            elif "hysteresis" in exp:
                self.assertIsNone(ev["hysteresis"], tick["_tick"])

    def test_flap_60s_boundary_holds_for_dwell(self):
        """tier-3 mtime flapping is absorbed — the exact noise the dwell was prescribed for."""
        self._run_seq("flap_60s_boundary")

    def test_flap_registry_tier1_is_immediate(self):
        """★ tier gate — a tier-1 busy→idle declaration is NEVER delayed by the dwell."""
        self._run_seq("flap_registry_tier1")

    def test_flap_recovery_is_immediate(self):
        """Upgrades never wait."""
        self._run_seq("flap_recovery")

    def test_dwell_only_applies_to_tier_3(self):
        """HYST_APPLIES_TO_TIER is the gate, asserted directly (plan §2.4)."""
        self.assertEqual(model.HYST_APPLIES_TO_TIER, (3,))

    def test_immediate_states_never_dwell(self):
        """dead/killed/done land on the tick they are observed, from any prior state."""
        key = ("s", "claude", 9001, "9000001")
        base = {"harness": "claude", "pid": 9001, "pid_alive": True, "proc_start": "9000001",
                "proc_start_match": True, "orphan": False, "status": "busy",
                "mtime": 999.0, "transcript": True, "activity_ms": 5000.0}
        state, _ = model.classify_session(dict(base), 1000.0, key=key)
        self.assertEqual(state, "working")
        gone = dict(base, pid_alive=False)
        state, ev = model.classify_session(gone, 1001.0, key=key)
        self.assertEqual(state, "dead")          # working → dead with no dwell
        self.assertIsNone(ev["hysteresis"])

    def test_codex_task_complete_bypasses_mtime_and_working_idle_dwell(self):
        key = ("s", "codex", 9002, "9000002")
        base = {
            "harness": "codex", "pid": 9002, "pid_alive": True,
            "proc_start": "9000002", "proc_start_match": True,
            "orphan": False, "status": None, "mtime": 999.0,
            "transcript": True, "task_lifecycle": "task_started",
        }
        state, _ = model.classify_session(base, 1000.0, key=key)
        self.assertEqual(state, "working")
        completed = dict(base, task_lifecycle="task_complete", mtime=1000.5)
        state, ev = model.classify_session(completed, 1001.0, key=key)
        self.assertEqual(state, "idle")
        self.assertEqual(ev["tier"], 2)
        self.assertEqual(ev["source"], "codex-lifecycle")
        self.assertIsNone(ev["hysteresis"])

    def test_explicit_registry_status_still_outranks_codex_lifecycle(self):
        value = {
            "harness": "codex", "pid": 9003, "pid_alive": True,
            "proc_start": "9000003", "proc_start_match": True,
            "orphan": False, "status": "busy", "mtime": 999.0,
            "transcript": True, "task_lifecycle": "task_complete",
        }
        state, ev = model.classify_session(value, 1000.0)
        self.assertEqual(state, "working")
        self.assertEqual(ev["tier"], 1)


class TrackerLifecycleTest(FixtureBase):
    """R7 — singleton state must not leak or grow unbounded."""

    def _tick(self, key, now, status="busy"):
        ev = {"harness": "claude", "pid": key[2], "pid_alive": True, "proc_start": key[3],
              "proc_start_match": True, "orphan": False, "status": status,
              "mtime": now - 1, "transcript": True, "activity_ms": 5000.0}
        return model.classify_session(ev, now, key=key)

    def test_sweep_drops_rows_that_vanished(self):
        a, b = ("s", "claude", 1, "1"), ("s", "claude", 2, "2")
        self._tick(a, 1000.0)
        self._tick(b, 1000.0)
        model.tracker_sweep()
        self.assertEqual(len(model._TRACKER._store), 2)
        self._tick(a, 1002.0)          # only `a` seen this tick
        model.tracker_sweep()
        self.assertEqual(list(model._TRACKER._store), [a])

    def test_reset_is_hermetic(self):
        self._tick(("s", "claude", 3, "3"), 1000.0)
        model.reset_state_tracker()
        self.assertEqual(len(model._TRACKER._store), 0)

    def test_proc_start_in_key_prevents_pid_reuse_confusion(self):
        """A recycled pid gets a DIFFERENT tracker key, so it can never inherit the dead
        process's dwell state — this is why proc_start is part of the key."""
        old = ("s", "claude", 4242, "111")
        new = ("s", "claude", 4242, "999")
        self._tick(old, 1000.0)
        self.assertNotIn(new, model._TRACKER._store)


class JobFixtureTest(FixtureBase):
    def _run_pair(self, name):
        fx = load(name)
        for case in fx["cases"]:
            state, ev = model.classify_job(case["input"], fx["now"])
            self.check(state, ev, case["expect"], case["_case"])

    def test_job_queued_vs_working(self):
        self._run_pair("job_queued_vs_working")

    def test_job_cancelled_vocabulary(self):
        """Terminal words map into fleet's vocabulary; raw survives for audit."""
        self._run_pair("job_cancelled")

    def test_drill_dedup_pair(self):
        """F-18a absorb is now evidence, weighed by the single classifier."""
        self._run_pair("drill_dedup_pair")

    def test_loop_proc_is_tier2(self):
        state, ev = model.classify_job(
            {"source": "proc", "key": "oncall", "is_loop": True, "harness": "claude",
             "status": None, "elapsed_min": 1, "slug": "loop-a", "transcript": None}, 1000.0)
        self.assertEqual(state, "working")
        self.assertEqual(ev["tier"], 2)

    def test_parent_extinction_ladder_fixtures(self):
        base = {"pid_scope":"namespace-local", "pid_authoritative":False,
                "pid_alive":False, "proc_start_match":False,
                "attempt_descendants":"unverifiable",
                "heartbeat":{"phase":"tool", "sequence":1, "updated_at":999.0,
                              "attempt_id":"child", "route_id":"r", "route_node":"execute"},
                "observed_liveness":{"state":"unverifiable"},
                "attempt_id":"child", "route_id":"r", "route_node":"execute"}
        evidence = model.classify_attempt_evidence(
            dict(base, parent_extinction={"state":"proven", "reason":"parent-terminal-process:gone"}), 1000.0)
        self.assertEqual(evidence["state"], "dead")
        self.assertEqual(evidence["source"], "parent")
        self.assertEqual(evidence["rule"], "parent-terminal-process:gone")
        evidence = model.classify_attempt_evidence(
            dict(base, pid_authoritative=True, pid_alive=True, proc_start_match=True,
                 parent_extinction={"state":"proven", "reason":"parent-proof"}), 1000.0)
        self.assertEqual(evidence["state"], "working")
        self.assertEqual(evidence["source"], "proc")
        populated = model.classify_attempt_evidence(
            dict(base, attempt_descendants="populated",
                 parent_extinction={"state":"proven", "reason":"parent-proof"}), 1000.0)
        self.assertEqual(populated["state"], "working")
        self.assertEqual(populated["source"], "namespace")
        ordinary = model.classify_attempt_evidence(base, 1000.0)
        self.assertEqual(ordinary["state"], "working")
        for proof in (None, {"state":"unproven", "reason":"no-proof"}):
            unchanged = model.classify_attempt_evidence(dict(base, parent_extinction=proof), 1000.0)
            self.assertEqual(unchanged["state"], "working")


class RelocationGuardTest(unittest.TestCase):
    """plan §3 — the heuristics were RELOCATED into the classifier, not left as patch layers.

    The structural guard (exactly two `.liveness =` assignment sites) is enforced by the
    Step 1 verification command; these assert the behavioral half.
    """

    def setUp(self):
        model.reset_state_tracker()

    def test_constants_are_not_duplicated(self):
        """dispatch's grace constant is the model's, not a second copy that can drift."""
        self.assertIs(dispatch._QUEUED_GRACE_MIN, model.JOB_QUEUED_GRACE_MIN)
        self.assertIs(liveness.STALE_MIN, model.SESSION_STALE_MIN)

    def test_liveness_classify_delegates_and_stamps_evidence(self):
        s = Session(harness="claude", pid=os.getpid(), cwd="/tmp", status="busy",
                    mtime=1000.0, started_at=900.0, updated_at=1000.0)
        s._has_transcript = True
        state = liveness.classify(s, 1000.0)
        self.assertEqual(state, "working")
        self.assertEqual(s.state_evidence["tier"], 1)
        self.assertEqual(s.state_evidence["state"], "working")

    def test_dispatch_liveness_stamps_evidence(self):
        j = DispatchJob(key="oncall", slug="loop-x", source="proc", cwd="/tmp")  # oncall ∈ _LOOP_KEYS
        state = dispatch._dispatch_liveness(j, 1000.0)
        self.assertEqual(state, "working")
        self.assertEqual(j.state_evidence["tier"], 2)

    def test_unused_is_in_the_vocabulary(self):
        self.assertIn("unused", model.LIVENESS_STATES)


class ReconcileEvidenceDerivationTest(unittest.TestCase):
    """D4 — reconcile now runs BEFORE the classify loop, so in production the proc row's
    liveness is still the default "unknown" and reconcile must DERIVE it. Every other test
    pins `liveness` on the proc row, which skips that derivation branch entirely — this one
    exercises the path production actually takes (phase_01 review F5).
    """

    def setUp(self):
        model.reset_state_tracker()

    def _pair(self):
        registry = DispatchJob(key="drill", slug="drill-claude-CASE-20260711160000-12345",
                               cwd="/tmp/drill-CASE-x/repo", pid=None, source="jobs",
                               status="open", elapsed_min=5)
        proc = DispatchJob(key="drill", slug="drill", worker_role="CASE",
                           cwd="/tmp/drill-CASE-x/repo", pid=999, source="proc")
        return registry, proc

    def test_derives_proc_liveness_when_not_pinned(self):
        registry, proc = self._pair()
        self.assertEqual(proc.liveness, "unknown")      # the production entry condition
        with mock.patch.object(dispatch, "_dispatch_liveness",
                               wraps=dispatch._dispatch_liveness) as spy:
            jobs = dispatch._reconcile_drill_rows([registry, proc], now=1000.0)
        self.assertEqual(len(jobs), 1)
        # The derivation branch ran, against the proc row, without tracking it.
        self.assertTrue(spy.called)
        self.assertIs(spy.call_args[0][0], proc)
        self.assertFalse(spy.call_args[1]["track"])

    def test_dropped_proc_row_leaves_no_tracker_entry(self):
        """F6 — a row that vanishes from the board must not hold tracker state."""
        registry, proc = self._pair()
        dispatch._reconcile_drill_rows([registry, proc], now=1000.0)
        self.assertNotIn(("j", "drill"), model._TRACKER._store)

    def test_derived_working_proc_is_absorbed_as_tier2_evidence(self):
        """End-to-end on the production path, nothing pinned or mocked: `drill` is a loop key,
        so the proc row derives tier-2 `working`; the registry row has no transcript and would
        derive tier-3 `queued`; the absorbed evidence must win and be labelled tier 2."""
        registry, proc = self._pair()
        jobs = dispatch._reconcile_drill_rows([registry, proc], now=1000.0)
        self.assertEqual(jobs[0]._proc_liveness, "working")
        self.assertEqual(dispatch._dispatch_liveness(jobs[0], now=1000.0), "working")
        self.assertEqual(jobs[0].state_evidence["tier"], 2)
        self.assertEqual(jobs[0].state_evidence["source"], "proc")

    def test_registry_row_alone_derives_queued(self):
        """The control for the test above: with no proc row to correlate, the same registry
        row falls back to its own tier-3 derivation."""
        registry, _proc = self._pair()
        jobs = dispatch._reconcile_drill_rows([registry], now=1000.0)
        self.assertIsNone(getattr(jobs[0], "_proc_liveness", None))
        self.assertEqual(dispatch._dispatch_liveness(jobs[0], now=1000.0), "queued")
        self.assertEqual(jobs[0].state_evidence["tier"], 3)


class HysteresisEvidenceCoherenceTest(unittest.TestCase):
    """phase_01 review F4 — while a dwell holds, the evidence must describe the state it
    actually EMITS, not the one it is suppressing. Otherwise `state: working` ships with
    `rule: "no activity within 60s"`, and F-25's whole point is auditable evidence."""

    def setUp(self):
        model.reset_state_tracker()

    def _tick(self, now, mtime):
        key = ("s", "codex", 7001, "7000000")
        ev = {"harness": "codex", "pid": 7001, "pid_alive": True, "proc_start": "7000000",
              "proc_start_match": None, "orphan": False, "status": None,
              "mtime": mtime, "transcript": True, "activity_ms": None}
        return model.classify_session(ev, now, key=key)

    def test_held_evidence_describes_the_emitted_state(self):
        self._tick(1000.0, 970.0)                     # working (tier 3, "activity within 60s")
        state, ev = self._tick(1010.0, 940.0)         # idle wants in; dwell holds working
        self.assertEqual(state, "working")
        self.assertEqual(ev["state"], "working")
        self.assertIn("activity within", ev["rule"])  # the WORKING rule, not the idle one
        self.assertNotIn("no activity", ev["rule"])

    def test_suppressed_verdict_is_still_reported(self):
        self._tick(1000.0, 970.0)
        _state, ev = self._tick(1010.0, 940.0)
        h = ev["hysteresis"]
        self.assertEqual(h["pending"], "idle")
        self.assertEqual(h["dwell_sec"], 90)
        self.assertEqual(h["suppressed_rule"], "no activity within 60s")
        self.assertEqual(h["suppressed_tier"], 3)

    def test_derived_still_matches_tier_while_held(self):
        self._tick(1000.0, 970.0)
        _state, ev = self._tick(1010.0, 940.0)
        self.assertEqual(ev["derived"], ev["tier"] == 3)

    def test_evidence_inputs_are_a_snapshot_not_a_live_reference(self):
        """F8-3 — mutating the caller's dict must not rewrite a past verdict."""
        ev_in = {"harness": "codex", "pid": 1, "pid_alive": True, "status": "busy",
                 "mtime": 999.0, "transcript": True, "activity_ms": None}
        _state, ev = model.classify_session(ev_in, 1000.0)
        ev_in["status"] = "TAMPERED"
        self.assertEqual(ev["inputs"]["status"], "busy")


class AdditiveSchemaTest(unittest.TestCase):
    """§2.5 — state_evidence is additive; nothing was removed or renamed."""

    def test_session_keeps_every_pre_v8_field(self):
        pre_v8 = ("harness", "pid", "cwd", "orphan", "app_server", "is_child", "detached",
                  "elapsed_min", "session_id", "slug", "title", "model", "effort", "ctx_pct",
                  "rl_5h", "rl_7d", "rl_ms", "rl_rs", "rl_windows", "cost", "tokens",
                  "active_context_tokens", "context_window_tokens", "session_input_tokens",
                  "session_cached_input_tokens", "session_output_tokens",
                  "session_reasoning_output_tokens", "session_total_tokens", "status",
                  "mtime", "liveness", "branch", "mem_worker")
        d = Session(harness="claude", pid=1).to_dict()
        for f in pre_v8:
            self.assertIn(f, d, "pre-v8 필드 삭제/개명: %s" % f)
        self.assertIn("state_evidence", d)

    def test_job_keeps_every_pre_v8_field(self):
        pre_v8 = ("key", "stage", "mode", "qa", "pid", "model", "elapsed_min", "slug", "cwd",
                  "parent_sid", "parent_cwd", "is_child", "harness", "qa_source", "source",
                  "status", "liveness", "profile", "branch", "depth", "parent_slug",
                  "intensity", "worker_role", "capability_owner", "effort", "model_role")
        d = DispatchJob(key="code").to_dict()
        for f in pre_v8:
            self.assertIn(f, d, "pre-v8 필드 삭제/개명: %s" % f)
        self.assertIn("state_evidence", d)
        self.assertIn("unit", d)
        self.assertIsNone(d["unit"])

    def test_defaults_are_none_so_absent_harnesses_render_dash(self):
        s = Session(harness="codex", pid=1)
        for f in ("proc_start", "registry_proc_start", "started_at", "updated_at",
                  "registry_name", "kind", "provenance", "state_evidence"):
            self.assertIsNone(getattr(s, f), f)


class NamespaceLocalDescendantEvidenceTest(unittest.TestCase):
    """A heartbeat says the attempt spoke; only a scan says it still runs.

    SD-58: a namespace-local row's PID is unreadable here, so freshness was the
    only thing left to judge it by -- and a worker that stopped mid-sentence
    keeps a fresh heartbeat forever, leaving the row open with no note to close
    on. These pin that a proven-empty scan outranks freshness while an
    impossible scan still does not.
    """

    def evidence(self, **extra):
        base = {
            "pid": None, "proc_start": "", "pid_local": 437, "pid_local_start": "1",
            "pid_authoritative": False, "pid_alive": False, "proc_start_match": False,
            "pid_scope": "namespace-local",
            "attempt_id": "att-ghost", "route_id": "rt-ghost", "route_node": "execute",
            "heartbeat": {"attempt_id": "att-ghost", "route_id": "rt-ghost",
                          "route_node": "execute", "phase": "tool", "sequence": 3,
                          "updated_at": 1000.0},
        }
        base.update(extra)
        return base

    def test_proven_empty_scan_outranks_a_fresh_heartbeat(self):
        verdict = model.classify_attempt_evidence(
            self.evidence(attempt_descendants="empty"), now=1060.0)
        self.assertEqual(verdict["state"], "dead")
        self.assertEqual(verdict["source"], "namespace")
        self.assertIn("attempt-tagged", verdict["rule"])

    def test_live_tagged_process_works_without_a_fresh_heartbeat(self):
        verdict = model.classify_attempt_evidence(
            self.evidence(attempt_descendants="populated",
                          heartbeat=None), now=99000.0)
        self.assertEqual((verdict["state"], verdict["source"]),
                         ("working", "namespace"))

    def test_populated_descendant_overrides_terminal_summary(self):
        verdict = model.classify_attempt_evidence(
            self.evidence(attempt_descendants="populated",
                          terminal_observation={
                              "attempt_id": "att-ghost", "route_id": "rt-ghost",
                              "route_node": "execute", "terminal_action": "completed-marker",
                          }), now=99000.0)
        self.assertEqual(verdict["state"], "working")
        self.assertEqual(verdict["source"], "namespace")

    def test_shared_observer_terminal_exception_outranks_raw_descendant(self):
        verdict = model.classify_attempt_evidence(
            self.evidence(
                attempt_descendants="populated",
                observed_liveness={
                    "state": "terminal",
                    "reason": "sealed-artifact-proof",
                },
            ), now=99000.0)
        self.assertEqual((verdict["state"], verdict["source"]),
                         ("done", "shared-observer"))

    def test_unscannable_and_absent_probes_both_stay_fail_closed(self):
        # An impossible scan, and an older caller that supplies no probe at all.
        for evidence in (self.evidence(attempt_descendants="unverifiable"),
                         self.evidence()):
            stale = model.classify_attempt_evidence(evidence, now=99000.0)
            self.assertEqual(stale["state"], "unknown", evidence.get("attempt_descendants"))
            fresh = model.classify_attempt_evidence(evidence, now=1060.0)
            self.assertEqual(fresh["state"], "working", evidence.get("attempt_descendants"))

    def test_authoritative_process_evidence_still_wins(self):
        live = model.classify_attempt_evidence(self.evidence(
            pid=437, proc_start="1", pid_authoritative=True, pid_alive=True,
            proc_start_match=True, attempt_descendants="empty"), now=1060.0)
        self.assertEqual((live["state"], live["source"]), ("working", "proc"))
        terminal = model.classify_attempt_evidence(self.evidence(
            attempt_descendants="empty",
            heartbeat={"attempt_id": "att-ghost", "route_id": "rt-ghost",
                       "route_node": "execute", "phase": "terminal", "sequence": 4,
                       "updated_at": 1000.0}), now=1060.0)
        self.assertEqual((terminal["state"], terminal["source"]), ("done", "heartbeat"))


if __name__ == "__main__":
    unittest.main()
