"""BC-ROUTE-POPUP-NARROW-1119 — a route display's collection reads only what the display draws.

`collect_all(route_sessions=[(harness, sid)])` serves the route popup, the STATUS popup, the panel's
route cache and tidy-clear. None of them draws a job's location or campaign label, the pending-delivery
count, the peer-message ledger rows, the prompt display, or an event time next to a title; the routes
popup draws no steward badge either. This file keeps two promises together:

* the popups and the panel cache show the same bytes as a full collection (T1, T2);
* those reads are not made on that path, and a campaign label is read once (T3).

Only native observation backends are stubbed. The values the skipped reads would produce are made
non-empty (peer ledger, pending delivery, steward relation, campaign cycle), so a full collection shows
them and an equal popup proves the display does not need them.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import time
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_registry_cache  # noqa: E402
import dispatch_seat_handover  # noqa: E402
import human_wait_state as cache  # noqa: E402
import session_shell as shell  # noqa: E402
from fleet import collectors, route, titles  # noqa: E402
from fleet.collectors import dispatch, peer_messages, prompt_inputs, steward  # noqa: E402
from fleet.explorer import live  # noqa: E402
from fleet.refresh import as_snapshot  # noqa: E402
from fleet.session_line import session_route_status  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_route_popup_scope_1117 import ScopedCollectionFixture, SID_A, SID_B  # noqa: E402

CAMPAIGN_TITLE = "Popup campaign 1119"


def _route_record(root, work, nodes, owner_attempt):
    record = {"schema_version": 1, "nodes": nodes, "cwd": str(work),
              "artifact_root": str(root / "artifacts"), "capability": "autopilot-code",
              "owner_attempt_id": owner_attempt}
    record["route_hash"] = route.route_hash(record)
    record["route_id"] = "rt-" + record["route_hash"].split(":")[1][:16]
    return record


class NarrowFixture(ScopedCollectionFixture):
    """Session A owns a staged route (worker in a split registry), an inline route and a finished
    one under a campaign; session B owns one more route that A's display must not show."""

    b_in_same_repo = True

    def build_route(self):
        root, work = self.root, self.work
        staged = _route_record(root, work, [{"id": "plan", "depends_on": []},
                                            {"id": "execute", "depends_on": ["plan"]}], "att-staged")
        inline = _route_record(root, work, [{"id": "inline", "depends_on": []}], "att-inline")
        done = _route_record(root, work, [{"id": "report", "depends_on": []}], "att-done")
        # Session B works in a folder of its own. Inside the same repository a steward relation can
        # exist (it is between sessions of one repository) but the STATUS popup's route count then
        # also differs between a scoped and a full collection (BC-ROUTE-POPUP-SLOW-1117 left that as
        # it was), so the status test puts B in a repository of its own.
        self.work_b = work / "sub" if self.b_in_same_repo else root / "work-b"
        self.work_b.mkdir(parents=True)
        if not self.b_in_same_repo:
            (self.work_b / ".git").mkdir()
        other = _route_record(root, self.work_b, [{"id": "other", "depends_on": []}], "att-other")
        self.route_id = staged["route_id"]
        self.inline_route_id, self.done_route_id = inline["route_id"], done["route_id"]
        self.other_route_id = other["route_id"]
        canonical, split = [], []
        now = time.time()

        def row(into, status, attempt, parent, record, worker_type, node, depth):
            cwd = self.work_b if parent == SID_B else self.work
            path = root / (record["route_id"] + ".json")
            if not path.exists():
                path.write_text(json.dumps(record))
            meta = {"attempt_id": attempt, "parent_sid": parent, "parent_harness": self.harness,
                    "harness": self.harness, "capability": "autopilot-code", "worker_type": worker_type,
                    "dispatch_depth": str(depth), "route_node": node, "route_id": record["route_id"],
                    "route_hash": record["route_hash"], "route_file": str(path),
                    "owner_route_id": record["route_id"], "owner_route_hash": record["route_hash"],
                    "owner_route_file": str(path), "artifact_root": str(root / "artifacts")}
            into.append(f"{now}\t{status}\trepo\t{cwd}\t{'owner' if worker_type == 'owner' else 'stage'}\t"
                        + ",".join(k + "=" + v for k, v in meta.items()))
        row(canonical, "running", "att-staged", SID_A, staged, "owner", "execute", 1)
        row(split, "running", "att-worker", SID_A, staged, "stage", "execute", 2)
        row(canonical, "running", "att-inline", SID_A, inline, "owner", "inline", 1)
        row(canonical, "done", "att-done", SID_A, done, "owner", "report", 1)
        row(canonical, "running", "att-other", SID_B, other, "owner", "other", 1)
        self.jobs.write_text("\n".join(canonical) + "\n")
        self.split_registry = root / "split-jobs.log"
        self.split_registry.write_text("\n".join(split) + "\n")
        # A campaign cycle that names the staged route: its owner/worker rows get a `campaign_label`.
        cycles = root / "artifacts" / ".runtime" / "artifact-producer" / "v1" / "cycles"
        cycles.mkdir(parents=True)
        (cycles / ("cyc_" + "0" * 32 + ".json")).write_text(json.dumps(
            {"route_id": staged["route_id"], "title": CAMPAIGN_TITLE}))

    def setUp(self):
        super().setUp()
        self.sessions[1].cwd = str(self.work_b)
        def patch(target, *args, **kwargs):
            patcher = mock.patch.object(target, *args, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The worker row lives in a registry the canonical set does not name: its parent is A.
        dispatch._scan_processes.observed_registries = {str(self.split_registry): {"att-worker"}}
        self.calls = {}

        def count(name, result=None, effect=None):
            def run(*args, **kwargs):
                self.calls[name] = self.calls.get(name, 0) + 1
                return effect(*args, **kwargs) if effect else result
            return run

        def relation(sessions, **_kwargs):
            self.calls["steward.enrich"] = self.calls.get("steward.enrich", 0) + 1
            for session in sessions:
                session.steward = False
                session.steward_targets = session.steward_parents = None
                if session.session_id == SID_A:
                    session.steward = True
                    session.steward_targets = [{"harness": "claude", "session_id": SID_B, "source": "ledger", "ts": 1.0}]
        peers = {"by_session": {(self.harness, SID_A): {"sent_1h": 2, "recv_1h": 1, "pending_obligations": [],
                                                       "last_recv": {"age_min": 3, "kind": "notice"}}}}
        patch(steward, "enrich", side_effect=relation)
        patch(peer_messages, "collect", side_effect=count("peer_messages.collect", peers))
        patch(prompt_inputs, "enrich", side_effect=count("prompt_inputs.enrich"))
        patch(titles, "annotate", side_effect=count("titles.annotate"))
        patch(dispatch, "_pending_delivery_counts",
              side_effect=count("pending_delivery", {"pending": 1, "expired": 0, "retained_batch_duties": 0}))
        patch(dispatch, "_fill_locations", side_effect=count("fill_locations"))
        original = dispatch._campaign_labels
        patch(dispatch, "_campaign_labels", side_effect=count("campaign_labels", effect=original))

    def popup(self, request, view=None):
        binding = {"harness": self.harness, "sid": SID_A, "native_pid": 2, "native_start": "1"}
        if view is None:      # the popup's own collection
            return shell._panel_popup(binding, dict(request))
        with mock.patch.object(shell, "_popup_view_model", view):
            return shell._panel_popup(binding, dict(request))

    def full_view(self, harness, sid, kind):
        return live.build_view_model(as_snapshot(self.collect(False)))

    def requested(self):
        return [(self.harness, SID_A)]


class NarrowRoutePopupTest(NarrowFixture):
    def test_fixture_does_the_work_the_display_does_not_need(self):
        """The skipped reads have something to read; the full collection shows it, so a byte-equal popup means something."""
        snapshot = self.collect(False)
        self.assertEqual(snapshot.pending_delivery, {"pending": 1, "expired": 0, "retained_batch_duties": 0})
        session = next(s for s in snapshot.sessions if s.session_id == SID_A)
        self.assertEqual((session.peer_sent_1h, session.peer_recv_1h), (2, 1))
        self.assertTrue(session.steward_targets)
        self.assertIn(CAMPAIGN_TITLE, {getattr(j, "campaign_label", None) for j in snapshot.jobs})
        self.assertTrue(any(j.attempt_id == "att-worker" for j in snapshot.jobs), "the split registry's worker is read")
        scoped = self.collect(True)
        self.assertEqual({j.route_id or j.owner_route_id for j in scoped.jobs} & {self.other_route_id}, set())

    def test_routes_popup_matches_a_full_collection(self):
        for request in ({"kind": "routes", "columns": 100},
                        {"kind": "routes", "columns": 100, "open": "ROUTE"},
                        {"kind": "routes", "columns": 100, "open": "ROUTE", "page": True},
                        {"kind": "routes", "columns": 100, "open": "INLINE", "page": True}):
            request = dict(request)
            if request.get("open") == "ROUTE":
                request["open"] = self.route_id
            elif request.get("open") == "INLINE":
                request["open"] = self.inline_route_id
            with self.subTest(request=request):
                narrow = self.popup(request)
                full = self.popup(request, self.full_view)
                self.assertTrue(narrow.get("ok", True), narrow)
                self.assertTrue(narrow["rows"])
                self.assertIn(self.route_id, narrow["routes"])
                self.assertNotIn(self.other_route_id, narrow["routes"])
                for key in ("rows", "styles", "hits", "routes"):
                    self.assertEqual(narrow.get(key), full.get(key), key)

    def panel_values(self, snapshot):
        records = []
        labels, panels = session_route_status(snapshot, include_details=True, records_out=records)
        keys = {"%s\0%s" % (self.harness, SID_A)}
        values = {"labels": {k: v for k, v in labels.items() if k in keys},
                  "panels": {k: v for k, v in panels.items() if k in keys},
                  "doers": {k: v for k, v in cache.session_doers(snapshot).items() if k in keys},
                  "repos": {k: v for k, v in (live.session_repos(records[0]) if records else {}).items() if k in keys},
                  "peers": {k: v for k, v in live.session_peers(
                      snapshot, records=records[0] if records else None).items() if k in keys}}
        return json.dumps(values, sort_keys=True, default=str)

    def test_panel_cache_values_match_a_full_collection(self):
        narrow = collectors.collect_all(harness_filter={self.harness}, route_sessions=self.requested(),
                                        include_report_verification=False)
        full = collectors.collect_all(harness_filter={self.harness}, include_report_verification=False)
        self.assertTrue(narrow.observations["basic"]["all_new"])
        narrow_values, full_values = self.panel_values(narrow), self.panel_values(full)
        self.assertEqual(narrow_values, full_values)
        self.assertIn("route", narrow_values)
        self.assertTrue(json.loads(narrow_values)["peers"], "the steward relation reaches the panel's PEERS line")

    def test_refresh_cache_publishes_the_full_collections_values(self):
        published = {}
        demands = {"%s\0%s" % (self.harness, SID_A): time.time()}

        def keep(result):
            published.update(result)
            return True
        # `_refresh_cache` names the collector as `tools.fleet.collectors`, a second module object beside the
        # `fleet.collectors` these stubs sit on: route its one call to the stubbed one.
        import tools.fleet.collectors as refresh_collectors
        with mock.patch.object(cache, "_route_demands", return_value=demands), \
             mock.patch.object(cache, "_notify_result"), mock.patch.object(cache, "publish_cache", side_effect=keep), \
             mock.patch.object(refresh_collectors, "collect_all", side_effect=collectors.collect_all):
            self.assertTrue(cache._refresh_cache())
        full = collectors.collect_all(harness_filter={self.harness}, include_report_verification=False)
        records = []
        labels, panels = session_route_status(full, include_details=True, records_out=records)
        self.assertEqual(json.dumps(published["route_status"], sort_keys=True),
                         json.dumps({k: v for k, v in labels.items() if k in demands}, sort_keys=True))
        self.assertEqual(json.dumps(published["route_panels"], sort_keys=True, default=str),
                         json.dumps({k: v for k, v in panels.items() if k in demands}, sort_keys=True, default=str))
        self.assertEqual(json.dumps({k: v for k, v in published["session_peers"].items() if k in demands}, sort_keys=True),
                         json.dumps({k: v for k, v in live.session_peers(full, records=records[0]).items() if k in demands},
                                    sort_keys=True))
        self.assertTrue(published["route_status"] and published["session_peers"])


class NarrowStatusPopupTest(NarrowFixture):
    b_in_same_repo = False

    def test_status_popup_matches_a_full_collection(self):
        request = {"kind": "status", "columns": 100, "cwd": str(self.work), "routes": [], "part": "fleet"}
        narrow, full = self.popup(request), self.popup(request, self.full_view)
        self.assertTrue(narrow.get("ok", True), narrow)
        self.assertTrue(narrow["rows"])
        for key in ("rows", "styles", "hits"):
            self.assertEqual(narrow.get(key), full.get(key), key)


class NarrowReadsTest(NarrowFixture):
    UNNEEDED = ("pending_delivery", "fill_locations", "peer_messages.collect",
                "prompt_inputs.enrich", "titles.annotate")

    def test_a_full_collection_still_reads_everything(self):
        self.collect(False)
        for name in self.UNNEEDED:
            self.assertGreaterEqual(self.calls.get(name, 0), 1, name)
        self.assertEqual(self.calls.get("campaign_labels"), 2)

    def test_a_route_display_leaves_out_what_it_does_not_draw(self):
        self.collect(True)
        for name in self.UNNEEDED:
            self.assertEqual(self.calls.get(name, 0), 0, name)

    def test_a_campaign_label_is_read_once(self):
        """`_collect` no longer reads it, because `work_titles.annotate` reads it for the same jobs."""
        scoped = self.collect(True)
        self.assertEqual(self.calls.get("campaign_labels"), 1)
        full = self.collect(False)
        label = lambda snapshot: sorted((j.attempt_id, j.campaign_label) for j in snapshot.jobs if j.attempt_id)
        self.assertIn(CAMPAIGN_TITLE, [item[1] for item in label(scoped)])
        self.assertEqual([item for item in label(scoped) if item[0] != "att-other"],
                         [item for item in label(full) if item[0] != "att-other"
                          and item[0] in {j.attempt_id for j in scoped.jobs}])

    def test_each_registry_is_resolved_once_per_display_read(self):
        """Regression guard: the registry parse and the handover question happen once per path."""
        generations, holders = {}, {}
        original_generation = dispatch_registry_cache._observe_generation
        original_holders = dispatch_seat_handover.job_holders

        def generation(path, *args, **kwargs):
            generations[str(path)] = generations.get(str(path), 0) + 1
            return original_generation(path, *args, **kwargs)

        def job_holders(records, jobs, **kwargs):
            holders[str(jobs)] = holders.get(str(jobs), 0) + 1
            return original_holders(records, jobs, **kwargs)
        with mock.patch.object(dispatch_registry_cache, "_observe_generation", generation), \
             mock.patch.object(dispatch_seat_handover, "job_holders", job_holders):
            self.collect(True)
        self.assertEqual(set(holders), {str(self.jobs), str(self.split_registry)})
        self.assertEqual(set(holders.values()), {1}, holders)
        self.assertTrue(generations and max(generations.values()) <= 1, generations)

    def test_the_routes_popup_reads_no_steward_badge(self):
        shell._popup_view_model(self.harness, SID_A, "routes")
        self.assertEqual(self.calls.get("steward.enrich", 0), 0)

    def test_the_status_popup_and_the_panel_cache_still_read_it(self):
        shell._popup_view_model(self.harness, SID_A, "status")
        self.assertEqual(self.calls.get("steward.enrich", 0), 1)
        collectors.collect_all(harness_filter={self.harness}, route_sessions=self.requested(),
                               include_report_verification=False)
        self.assertEqual(self.calls.get("steward.enrich", 0), 2)

    def test_a_display_pass_leaves_the_full_passs_peer_ledger_alone(self):
        """`fleet --json` reads `last_peer_messages` after its full pass; a display's pass reads no ledger and sets nothing."""
        self.collect(False)
        held = collectors.collect_all.last_peer_messages
        self.assertIsNotNone(held)
        reads = self.calls["peer_messages.collect"]
        self.collect(True)
        self.assertIs(collectors.collect_all.last_peer_messages, held)
        self.assertEqual(self.calls["peer_messages.collect"], reads)


class NarrowRoutePopupCodexTest(NarrowRoutePopupTest):
    harness = "codex"


class NarrowRoutePopupOpenCodeTest(NarrowRoutePopupTest):
    harness = "opencode"


class NarrowStatusPopupCodexTest(NarrowStatusPopupTest):
    harness = "codex"


class NarrowStatusPopupOpenCodeTest(NarrowStatusPopupTest):
    harness = "opencode"


class NarrowReadsCodexTest(NarrowReadsTest):
    harness = "codex"


class NarrowReadsOpenCodeTest(NarrowReadsTest):
    harness = "opencode"


if __name__ == "__main__":
    unittest.main()
