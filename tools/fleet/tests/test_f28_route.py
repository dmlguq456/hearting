#!/usr/bin/env python3
"""Hermetic unit tests — F-28a route record consumption (route.py + dispatch.py collector
edits + fleet.py --json). Stdlib unittest only, tempfile.TemporaryDirectory + mock (test_dispatch.py
precedent). No real dispatch/record I/O outside the copied fixtures under tests/fixtures/route/.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import projection, route          # noqa: E402
from fleet.collectors import dispatch       # noqa: E402
from fleet.model import DispatchJob, Session # noqa: E402

_FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "route")
_COMPOSED = os.path.join(_FIXDIR, "synth_composed_survey.json")


def _fx(name):
    return os.path.join(_FIXDIR, name)


class RouteLoadTest(unittest.TestCase):
    def setUp(self):
        route.clear_cache()

    def tearDown(self):
        route.clear_cache()

    def test_t1_1_load_real_claude(self):
        rec = route.load(_fx("real_claude_staged.json"))
        self.assertIsInstance(rec, dict)
        self.assertEqual(rec["route_id"], "rt-9fa0fed86699b8f5")

    def test_t1_2_load_real_codex(self):
        rec = route.load(_fx("real_codex_staged.json"))
        self.assertIsInstance(rec, dict)
        self.assertEqual(rec["route_id"], "rt-f942824768304759")

    def test_t1_3_load_missing_path_never_raises(self):
        rec = route.load("/no/such/path/route.json")
        self.assertIsNone(rec)

    def test_t1_3b_non_path_like_input_never_raises(self):
        # I2 regression pin — found during Step 5 integration sweep: os.path.abspath(123)
        # raised TypeError, which a malformed pipe-parse value could theoretically reach.
        for bad in (123, 1.5, {}, [], object(), b"", ()):
            with self.subTest(bad=bad):
                self.assertIsNone(route.load(bad))

    def test_t1_4_hash_recompute_matches_both_real_records(self):
        for name in ("real_claude_staged.json", "real_codex_staged.json"):
            with open(_fx(name), encoding="utf-8") as f:
                rec = json.load(f)
            self.assertEqual(route.route_hash(rec), rec["route_hash"], name)

    def test_t1_5_broken_hash_rejected(self):
        self.assertIsNone(route.load(_fx("synth_broken_hash.json")))

    def test_t1_6_bad_schema_rejected(self):
        self.assertIsNone(route.load(_fx("synth_bad_schema.json")))

    def test_t1_7_pipe_hash_mismatch_rejected(self):
        rec = route.load(_fx("real_claude_staged.json"), expect_hash="sha256:deadbeef")
        self.assertIsNone(rec)

    def test_t1_8_cache_hit_skips_second_json_load(self):
        route.clear_cache()
        with mock.patch("fleet.route.json.load", wraps=json.load) as m:
            route.load(_fx("real_claude_staged.json"))
            route.load(_fx("real_claude_staged.json"))
            self.assertEqual(m.call_count, 1)

    def test_t1_9_cache_invalidates_on_mtime_change(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "r.json")
            shutil.copy(_fx("real_claude_staged.json"), path)
            rec1 = route.load(path)
            self.assertIsNotNone(rec1)
            # rewrite with the OTHER real record (different mtime+size+content)
            shutil.copy(_fx("real_codex_staged.json"), path)
            os.utime(path, None)
            rec2 = route.load(path)
            self.assertIsNotNone(rec2)
            self.assertNotEqual(rec1["route_id"], rec2["route_id"])

    def test_current_route_rejects_declared_max_below_realized_depth(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "route.json")
            record = {
                "schema_version": 2,
                "dispatch_contract_version": 3,
                "owner_dispatch_depth": 1,
                "max_dispatch_depth": 1,
                "nodes": [{
                    "id": "execute",
                    "dispatch_depth": 2,
                }],
            }
            record["route_hash"] = route.route_hash(record)
            record["route_id"] = (
                "rt-" + record["route_hash"].split(":", 1)[1][:16]
            )
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            self.assertIsNone(route.load(path))


class NodeOrderTest(unittest.TestCase):
    def test_t1_10_parallel_lab_levels(self):
        with open(_fx("synth_parallel_lab.json"), encoding="utf-8") as f:
            rec = json.load(f)
        levels = route.node_order(rec)
        self.assertEqual(levels, [
            ["setup"], ["eval-asr", "eval-sep", "eval-vad"], ["aggregate"], ["report"],
        ])

    def test_t1_11_cycle_never_raises_and_keeps_all_nodes(self):
        rec = {"nodes": [
            {"id": "a", "depends_on": ["b"]},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": []},
        ]}
        levels = route.node_order(rec)
        seen = [nid for level in levels for nid in level]
        self.assertEqual(sorted(seen), ["a", "b", "c"])

    def test_t1_10b_composed_survey_route_preserves_record_order_fanin_and_metadata(self):
        record = route.load(_COMPOSED)
        self.assertEqual(route.node_order(record), [["survey"], ["claim-a", "claim-b"], ["synth"]])
        claim_b = next(node for node in record["nodes"] if node["id"] == "claim-b")
        self.assertEqual(claim_b["depends_on"], ["survey"])
        self.assertEqual(claim_b["write_scope"], ["reviews/claim-b/**"])
        synth = next(node for node in record["nodes"] if node["id"] == "synth")
        self.assertEqual(synth["depends_on"], ["claim-a", "claim-b"])


def _write_jobs_route_log(tmpdir):
    with open(_fx("jobs_route.log"), encoding="utf-8") as f:
        template = f.read()
    content = template.format(FIXDIR=_FIXDIR)
    path = os.path.join(tmpdir, "jobs.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class CollectRouteFieldsTest(unittest.TestCase):
    def setUp(self):
        route.clear_cache()

    def tearDown(self):
        # module-global stash (dispatch.collect.last_route_nodes) must never leak into a
        # later test that reads it without collecting first (JsonAdditiveTest below).
        dispatch.collect.last_route_nodes = {}

    def test_t1_12_jobs_carry_route_fields_even_when_record_gone(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_path = _write_jobs_route_log(td)
            with mock.patch.object(dispatch.procscan, "_ps_lines", return_value=[]):
                jobs = dispatch.collect(jobs_path=jobs_path)
            by_slug = {j.slug: j for j in jobs}
            self.assertIn("fleet-v10-plan", by_slug)
            self.assertEqual(by_slug["fleet-v10-plan"].route_id, "rt-9fa0fed86699b8f5")
            self.assertEqual(by_slug["fleet-v10-plan"].route_node, "plan")
            # v94-note-db-steward-research: route_file points at a real-world path this
            # checkout cannot resolve — the job itself must still be alive (tolerant §3.3).
            self.assertIn("v94-note-db-steward-research", by_slug)
            self.assertEqual(by_slug["v94-note-db-steward-research"].route_id,
                             "rt-5e123969aa5a202b")

    def test_t1_13_terminal_route_node_survives_as_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_path = _write_jobs_route_log(td)
            with mock.patch.object(dispatch.procscan, "_ps_lines", return_value=[]):
                dispatch.collect(jobs_path=jobs_path)
            nodes = dispatch.collect.last_route_nodes
            self.assertIn("rt-9fa0fed86699b8f5", nodes)
            self.assertNotIn("plan", ())  # sanity no-op guard
            # Historical terminal rows remain diagnostic history, but cannot
            # authoritatively set current route-node state.
            report = nodes["rt-9ff8199b5372cfdb"]["report"]
            test = nodes["rt-f942824768304759"]["test"]
            self.assertNotIn("status", report)
            self.assertNotIn("status", test)
            self.assertEqual(
                report["attempt_history"][-1]["contract_status"], "legacy-read-only"
            )
            self.assertEqual(
                test["attempt_history"][-1]["contract_status"], "legacy-read-only"
            )

    def test_t1_13b_terminal_only_route_still_resolves_to_a_record_not_heuristic(self):
        """code-test verification.md §10 — the exact defect: `v93-reading-face-d1-test-r6`
        (rt-f942824768304759) is a DONE row in the fixture — `dispatch.collect()`'s returned
        `jobs` therefore carries NO live job for that route_id (the row never survives
        `_scan_jobs_log`'s terminal-row filter). Before the fix, `route.collect_views()` had no
        way to find `real_codex_staged.json` at all in that situation and produced a
        `source="heuristic", nodes=[]` degrade view for a perfectly valid, hash-verified
        record — this pins the fix (`_scan_route_nodes` now carries `route_file` in its
        evidence, and `resolve_records` consults it when no live job does).

        v16: the explicit-invalid degrade view's `source` was renamed `heuristic` ->
        `unknown` (plan Step 1.2.3); the substance of this pin — a hash-verified record for
        a route with no live job must still resolve to the real record, not a degrade view
        at all — is unchanged.

        Found during this route's execution: `_scan_route_nodes` was gating `route_file`/
        `route_hash`/`parent` behind this row's OWN attempt-contract-v3 validity, so any
        legacy-schema terminal row (like this fixture's, which predates
        `attempt_schema_version=2`) never surfaced its route_file at all and this pin's
        route silently degraded — the same bug the docstring describes, reintroduced by an
        unrelated later attempt-contract gate. Fixed in `tools/fleet/collectors/dispatch.py`
        by moving those three registry-identity fields out of the contract-gated update
        (they name WHERE the record lives, not whether this attempt's own dispatch metadata
        validates)."""
        route.clear_cache()
        with tempfile.TemporaryDirectory() as td:
            jobs_path = _write_jobs_route_log(td)
            with mock.patch.object(dispatch.procscan, "_ps_lines", return_value=[]):
                jobs = dispatch.collect(jobs_path=jobs_path)
            self.assertFalse(any(j.route_id == "rt-f942824768304759" for j in jobs),
                             "fixture precondition: this route has no LIVE job at all")
            views = route.collect_views(jobs, dispatch.collect.last_route_nodes)
        view = next(v for v in views if v["route_id"] == "rt-f942824768304759")
        self.assertNotEqual(view["source"], "unknown")
        self.assertTrue(view["nodes"], "the sealed record must resolve with real nodes")


class BuildViewsTest(unittest.TestCase):
    def setUp(self):
        with open(_fx("synth_parallel_lab.json"), encoding="utf-8") as f:
            self.record = json.load(f)
        self.route_id = self.record["route_id"]
        self.now = 1_800_000_000.0

    def test_t1_14_active_beats_done(self):
        job = DispatchJob(key="eval-asr", slug="lab-eval-asr", route_id=self.route_id,
                          route_node="eval-asr", liveness="working", elapsed_min=8,
                          model="sonnet", effort="medium", pid=555)
        ev = {self.route_id: {"eval-asr": {"status": "done", "slug": "lab-eval-asr-old",
                                           "ts": self.now - 600, "note": None}}}
        views = route.build_views([job], ev, {self.route_id: self.record}, self.now)
        self.assertEqual(len(views), 1)
        node = next(n for n in views[0]["nodes"] if n["id"] == "eval-asr")
        self.assertEqual(node["state"], "active")

    def test_t1_15_failed_registry_row(self):
        ev = {self.route_id: {"eval-sep": {"status": "done", "slug": "lab-eval-sep",
                                           "ts": self.now - 300, "note": "fleet-kill"}}}
        views = route.build_views([], ev, {self.route_id: self.record}, self.now)
        node = next(n for n in views[0]["nodes"] if n["id"] == "eval-sep")
        self.assertEqual(node["state"], "failed")

    def test_exact_reconcile_needed_is_not_failed_or_complete(self):
        job = DispatchJob(
            key="eval-sep", slug="lab-eval-sep", route_id=self.route_id,
            route_node="eval-sep", attempt_id="att-reconcile", liveness="stale",
            elapsed_min=3, note="reconcile-needed",
        )
        job.state_evidence = {"attempt": {
            "state": "stale", "source": "shared-observer",
            "rule": "terminal-observed/reconcile-needed",
            "attempt_id": job.attempt_id, "route_id": self.route_id,
            "route_node": "eval-sep",
            "observed_liveness": {
                "state": "reconcile-needed", "reason": "terminal-observed",
                "process_state": "quiescent",
            },
        }}
        views = route.build_views([job], {}, {self.route_id: self.record}, self.now)
        node = next(n for n in views[0]["nodes"] if n["id"] == "eval-sep")
        self.assertEqual(node["state"], "reconciling")
        self.assertIsNone(node["gate_passed"])
        self.assertEqual(views[0]["progress"]["done"], 0)
        public = route.summary(views)[0]
        public_node = next(n for n in public["nodes"] if n["id"] == "eval-sep")
        self.assertEqual(public_node["state"], "reconciling")

    def _reconciling_job(self, node="eval-sep"):
        job = DispatchJob(
            key=node, slug="lab-" + node, route_id=self.route_id, route_node=node,
            attempt_id="att-" + node, liveness="stale", elapsed_min=3,
            note="reconcile-needed",
        )
        job.state_evidence = {"attempt": {
            "state": "stale", "source": "shared-observer",
            "rule": "terminal-observed/reconcile-needed",
            "attempt_id": job.attempt_id, "route_id": self.route_id, "route_node": node,
            "observed_liveness": {"state": "reconcile-needed", "reason": "process-exited",
                                  "process_state": "quiescent"},
        }}
        return job

    def test_route_whose_only_in_flight_work_has_no_process_is_unknown_not_working(self):
        # rt-454aff269a3d2452 (2026-11-02): a frame leg's process was proven gone, its
        # registry row stayed open, and the route read "working 8d" with nothing running.
        views = route.build_views([self._reconciling_job()], {}, {self.route_id: self.record},
                                  self.now)
        self.assertEqual(views[0]["liveness"], "unknown")
        node = next(n for n in views[0]["nodes"] if n["id"] == "eval-sep")
        self.assertEqual(node["state"], "reconciling")

    def test_any_live_or_unobservable_route_job_keeps_route_liveness_unset(self):
        # A quiet ("stale" by transcript age) job is not proof that its process ended.
        for liveness in ("working", "idle", "blocked", "unknown", "stale", None):
            other = DispatchJob(key="eval-asr", slug="lab-eval-asr", route_id=self.route_id,
                                route_node="eval-asr", liveness=liveness, elapsed_min=1)
            views = route.build_views([self._reconciling_job(), other], {},
                                      {self.route_id: self.record}, self.now)
            self.assertIsNone(views[0]["liveness"], liveness)
        unrelated = DispatchJob(key="x", slug="x", route_id="rt-other", route_node="eval-asr",
                                liveness="working", elapsed_min=1)
        self.assertEqual(route.route_liveness(
            [{"id": "eval-sep", "state": "reconciling"}], [self._reconciling_job(), unrelated],
            self.route_id), "unknown")
        self.assertIsNone(route.route_liveness([{"id": "a", "state": "pending"}], [], None))
        self.assertIsNone(route.route_liveness(
            [{"id": "a", "state": "reconciling"}, {"id": "b", "state": "recovering"}], [], None))

    def test_reconcile_note_without_exact_evidence_remains_failed(self):
        job = DispatchJob(
            key="eval-sep", slug="lab-eval-sep", route_id=self.route_id,
            route_node="eval-sep", liveness="stale", note="reconcile-needed",
        )
        views = route.build_views([job], {}, {self.route_id: self.record}, self.now)
        node = next(n for n in views[0]["nodes"] if n["id"] == "eval-sep")
        self.assertEqual(node["state"], "failed")

    def test_pending_when_no_evidence(self):
        views = route.build_views([], {}, {self.route_id: self.record}, self.now)
        node = next(n for n in views[0]["nodes"] if n["id"] == "report")
        self.assertEqual(node["state"], "pending")

    def test_heuristic_view_when_record_unresolved(self):
        # v16: an explicit route_id with no loadable record is a stable "unknown" degrade
        # view (plan Step 1.2.3), not the retired "heuristic" label.
        job = DispatchJob(key="x", slug="x", route_id="rt-unknown0000000")
        views = route.build_views([job], {}, {}, self.now)
        self.assertEqual(views[0]["source"], "unknown")
        self.assertEqual(views[0]["nodes"], [])

    def test_current_unit_metadata_survives_view_and_json_summary(self):
        record = json.loads(json.dumps(self.record))
        record["unit_catalog_digest"] = "sha256:" + "a" * 64
        record["composed"] = True
        record["nodes"][0]["unit"] = "code-plan"
        record["nodes"][0]["unit_choices"] = ["code-plan", "spec-plan"]
        views = route.build_views([], {}, {self.route_id: record}, self.now)
        view = views[0]
        node = next(n for n in view["nodes"] if n["id"] == record["nodes"][0]["id"])
        self.assertEqual(view["unit_catalog_digest"], record["unit_catalog_digest"])
        self.assertTrue(view["composed"])
        self.assertEqual(node["unit"], "code-plan")
        self.assertEqual(node["unit_choices"], ["code-plan", "spec-plan"])
        projected = route.summary(views)[0]
        self.assertEqual(projected["unit_catalog_digest"], record["unit_catalog_digest"])
        self.assertTrue(projected["composed"])
        self.assertEqual(projected["nodes"][0]["unit"], "code-plan")

    def test_legacy_route_metadata_defaults_are_additive(self):
        views = route.build_views([], {}, {self.route_id: self.record}, self.now)
        projected = route.summary(views)[0]
        self.assertIsNone(projected["unit_catalog_digest"])
        self.assertFalse(projected["composed"])
        self.assertTrue(all(node["unit"] is None for node in projected["nodes"]))

    def test_malformed_unit_metadata_degrades_without_string_explosion(self):
        record = json.loads(json.dumps(self.record))
        record["nodes"][0]["unit"] = {"not": "a unit id"}
        record["nodes"][0]["unit_choices"] = "code-plan"
        view = route.build_views([], {}, {self.route_id: record}, self.now)[0]
        node = next(n for n in view["nodes"] if n["id"] == record["nodes"][0]["id"])
        self.assertIsNone(node["unit"])
        self.assertEqual(node["unit_choices"], [])


class JsonAdditiveTest(unittest.TestCase):
    def setUp(self):
        route.clear_cache()
        dispatch.collect.last_route_nodes = {}   # test isolation — no leaked module state

    def test_t1_16_route_key_additive(self):
        if __package__:
            pass
        from fleet import fleet as fleetmod
        sessions, jobs = [], []
        out = json.loads(fleetmod._snapshot_json(sessions, jobs))
        self.assertIn("sessions", out)
        self.assertIn("jobs", out)
        self.assertIn("summary", out)
        self.assertIn("route", out)
        self.assertEqual(out["route"], [])

    def test_t1_17_no_route_records_empty_array(self):
        from fleet import fleet as fleetmod
        job = DispatchJob(key="code", slug="no-route-job")   # no route_id at all
        out = json.loads(fleetmod._snapshot_json([], [job]))
        self.assertEqual(out["route"], [])
        self.assertIn("route_file", out["jobs"][0])   # additive per-job keys present
        self.assertIsNone(out["jobs"][0]["route_file"])
        self.assertIn("unit", out["jobs"][0])
        self.assertIsNone(out["jobs"][0]["unit"])

    def test_t1_18_populated_snapshot_old_key_only_consumer_is_unchanged(self):
        from fleet import fleet as fleetmod
        record = route.load(_COMPOSED)
        job = DispatchJob(
            key="claim", slug="claim-a", harness="claude", model="opus",
            effort="high", elapsed_min=7, note="legacy-note", liveness="stale",
            route_id=record["route_id"], route_file=_COMPOSED, route_node="claim-a",
        )
        session = Session(harness="claude", pid=401, cwd="/proj", session_id="sid",
                          liveness="working", ctx_pct=42)
        evidence = {record["route_id"]: {"claim-a": {
            "status": "killed", "note": "legacy-note", "route_file": _COMPOSED,
        }}}
        projection.attach_projections([session], [job], node_evidence=evidence, now=100.0)
        out = json.loads(fleetmod._snapshot_json([session], [job]))

        # This intentionally models a pre-v16 reader: it touches only the old
        # top-level fields and legacy route-node fields, ignoring all additions.
        legacy = {
            "sessions": out["sessions"],
            "jobs": out["jobs"],
            "summary": out["summary"],
            "route": out["route"],
        }
        self.assertEqual(legacy["summary"]["dispatch_count"], 1)
        node = next(item for item in legacy["route"][0]["nodes"] if item["id"] == "claim-a")
        self.assertEqual(
            {key: node[key] for key in ("model", "harness", "effort", "elapsed_min", "note")},
            {"model": "opus", "harness": "claude", "effort": "high",
             "elapsed_min": 7, "note": "legacy-note"},
        )
        self.assertIn("work_projection", legacy["jobs"][0])
        self.assertIn("context", legacy["sessions"][0])
        self.assertNotIn("_context_evidence", json.dumps(out))
        self.assertNotIn("_route_view", json.dumps(out))


class OrphanConductorAnnotationTest(unittest.TestCase):
    """SD-64/71: Fleet's current-attempt view stamps note/resume_boundary on a
    dead depth-1 owner row that the shared registry classifier detects as
    orphaned, reusing dispatch-registry.py's classify() in-process."""

    def setUp(self):
        route.clear_cache()
        dispatch.collect.last_route_nodes = {}
        dispatch._ORPHAN_REGISTRY_MOD = None

    def _write_route(self, td, route_id):
        record = {"route_id": route_id, "nodes": [
            {"id": "plan", "depends_on": []}, {"id": "execute", "depends_on": ["plan"]},
            {"id": "test", "depends_on": ["execute"]}, {"id": "report", "depends_on": ["test"]}]}
        path = os.path.join(td, "route.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return path

    def _owner_with_open_child(self, *, quiescent):
        from split_module_fixture import patch_split
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            marker_dir = os.path.join(home, ".dispatch", "completion", "rt-fleet-orphan")
            os.makedirs(marker_dir)
            with open(os.path.join(marker_dir, "plan.json"), "w", encoding="utf-8") as fh:
                json.dump({"node_id": "plan"}, fh)
            route_file = self._write_route(td, "rt-fleet-orphan")
            jobs_path = os.path.join(td, "jobs.log")
            with open(jobs_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "2026-07-19T00:00:00Z\topen\t/r\t/w\towner\t"
                    "worker_type=owner,attempt_id=att-fleet-owner,"
                    "pid=999999996,pid_start=1,attempt_schema_version=2,"
                    "dispatch_depth=1,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    "fallback_hop=same-harness-headless\n"
                )
                # No pid/pid_start is needed here: the registry contract counts any open
                # exact child row and preserves it for main-session diagnosis.
                fh.write(
                    "2026-07-19T00:00:01Z\topen\t/r\t/w\tchild\t"
                    "route_id=rt-fleet-orphan,route_file=%s,route_node=execute,"
                    "attempt_id=att-fleet-child,parent=owner,"
                    "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    "fallback_hop=same-harness-headless\n" % route_file
                )
            # This annotation unit consumes the shared process verdict. A bare
            # extinct PID does not establish that its original group is empty.
            registry = dispatch._orphan_registry_module()
            self.assertIsNotNone(registry)
            contract = sys.modules["dispatch_contract"]
            observation = contract.ProcessQuiescence(
                "quiescent" if quiescent else "unverifiable",
                "local-pid-gone" if quiescent else "process-group-missing")
            with open(jobs_path, "rb") as fh:
                before = fh.read()
            with mock.patch.object(dispatch.procscan, "_ps_lines", return_value=[]), \
                 patch_split(contract, "attempt_process_quiescence", return_value=observation), \
                 mock.patch.object(dispatch, "observed_supervised_owner_liveness", return_value=None), \
                 mock.patch.dict(os.environ, {"AGENT_HOME": home}):
                # Isolate the orphan annotator's coarse dead-job input. The
                # shared registry verdict still decides whether it may stamp
                # the unchanged original row; missing group evidence cannot.
                jobs = dispatch.collect(jobs_path=jobs_path)
            with open(jobs_path, "rb") as fh:
                self.assertEqual(fh.read(), before)
            return next(j for j in jobs if j.slug == "owner")

    def test_dead_owner_with_open_child_is_stamped_orphaned(self):
        owner = self._owner_with_open_child(quiescent=True)
        self.assertEqual(owner.liveness, "dead")
        self.assertEqual(owner.note, "dead-parent-orphaned")
        self.assertEqual(owner.resume_boundary, "execute")

    def test_missing_group_observation_keeps_the_same_original_row_unannotated(self):
        owner = self._owner_with_open_child(quiescent=False)
        self.assertIsNone(owner.note)
        self.assertIsNone(owner.resume_boundary)

    def test_dead_owner_with_completed_route_is_never_stamped(self):
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            marker_dir = os.path.join(home, ".dispatch", "completion", "rt-fleet-clean")
            os.makedirs(marker_dir)
            for node in ("plan", "execute", "test", "report"):
                with open(os.path.join(marker_dir, "%s.json" % node), "w", encoding="utf-8") as fh:
                    json.dump({"node_id": node}, fh)
            route_file = self._write_route(td, "rt-fleet-clean")
            jobs_path = os.path.join(td, "jobs.log")
            with open(jobs_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "2026-07-19T00:00:00Z\topen\t/r\t/w\towner-clean\t"
                    "route_id=rt-fleet-clean,route_file=%s,worker_type=owner,"
                    "attempt_id=att-fleet-owner-clean,pid=999999994,pid_start=1,"
                    "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    "fallback_hop=same-harness-headless\n" % route_file
                )
            with mock.patch.object(dispatch.procscan, "_ps_lines", return_value=[]), \
                 mock.patch.dict(os.environ, {"AGENT_HOME": home}):
                jobs = dispatch.collect(jobs_path=jobs_path)
            owner = next(j for j in jobs if j.slug == "owner-clean")
            self.assertEqual(owner.liveness, "dead")
            self.assertIsNone(owner.note)


if __name__ == "__main__":
    unittest.main()
