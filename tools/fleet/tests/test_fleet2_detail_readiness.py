"""First-screen detail readiness is explicit and bound to the current basic pass."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import copy
import unittest
from unittest import mock

from tools.fleet import details, projection, route_chain
from tools.fleet.collectors import _usage_cache_observation
from tools.fleet.model import DispatchJob, Session, WorkProjection
from tools.fleet.projection import required_projection_details_complete
from tools.fleet.refresh import (
    LiveSnapshot,
    required_details_all_new,
    required_details_complete,
    set_required_details,
)


def _basic(identity):
    return LiveSnapshot(observations={
        "basic": {"state": "ok", "generation_identity": identity,
                  "observed_at": 100.0, "all_new": True},
        "required-details": {"state": "pending", "generation_identity": identity,
                              "all_new": False},
    })


class Fleet2DetailReadinessTests(unittest.TestCase):
    @staticmethod
    def _session(*, route_id=None):
        row = Session(harness="claude", pid=17, proc_start=100, session_id="sid-a",
                      cwd="/repo", liveness="idle", route_id=route_id)
        row.peer_last_recv = {"from_session_id": "sid-peer", "age_min": 2}
        row.peer_last_sent = {"to_session_id": "sid-peer", "age_min": 3}
        row.peer_sent_1h = 2
        row.peer_recv_1h = 1
        row.peer_obligations = [{"kind": "watch"}]
        return row

    def test_optional_last_good_survives_required_only_pass_for_exact_row(self):
        previous = LiveSnapshot(
            sessions=[self._session()], memory={
                "by_repo": {"repo": [{"id": "record"}]},
                "database": {"state": "success", "by_repo": {"repo": [{"id": "backlog"}]}}},
            observations={"optional:memory": {"state": "success"},
                          "optional:memory-db": {"state": "success"},
                          "optional:peers": {"state": "success"}})
        current = LiveSnapshot(sessions=[self._session()], memory=None)
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, current))
        row = retained.snapshot.sessions[0]
        self.assertEqual(retained.snapshot.memory, previous.memory)
        self.assertEqual(row.peer_last_recv["from_session_id"], "sid-peer")
        self.assertEqual(row.peer_sent_1h, 2)
        self.assertEqual(row.peer_obligations, [{"kind": "watch"}])
        self.assertEqual(retained.snapshot.observations["optional:peers"]["state"], "success")
        self.assertEqual(retained.snapshot.memory["database"]["by_repo"]["repo"][0]["id"],
                         "backlog")

    def test_failed_database_read_keeps_prior_backlog_with_failed_health(self):
        previous = LiveSnapshot(
            memory={"database": {"state": "success", "by_repo": {"repo": [{"id": "task-1"}]}}},
            observations={"optional:memory-db": {"state": "success"}})
        current = LiveSnapshot(
            memory={"database": {"state": "failed", "by_repo": {}}},
            observations={"optional:memory-db": {"state": "failed"}})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, current))
        database = retained.snapshot.memory["database"]
        self.assertEqual(database["state"], "failed")
        self.assertTrue(database["last_good"])
        self.assertEqual(database["last_good_by_repo"]["repo"][0]["id"], "task-1")

    def test_unknown_peer_observation_keeps_exact_last_good_and_marks_stale(self):
        prior_session = self._session()
        previous = LiveSnapshot(sessions=[prior_session],
                                observations={"optional:peers": {"state": "success",
                                                                  "observed_at": 10}})
        current_session = self._session()
        current_session.peer_last_recv = None
        current_session.peer_last_sent = None
        current_session.peer_sent_1h = 0
        current_session.peer_recv_1h = 0
        current_session.peer_obligations = []
        current = LiveSnapshot(sessions=[current_session],
                               observations={"optional:peers": {"state": "unknown",
                                                                 "observed_at": 11}})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, current))
        self.assertEqual(retained.snapshot.sessions[0].peer_last_recv["from_session_id"], "sid-peer")
        self.assertEqual(retained.snapshot.observations["optional:peers"]["state"], "unknown")
        self.assertTrue(retained.snapshot.observations["optional:peers"]["last_good"])

    def test_peer_health_success_empty_clears_and_failed_partial_preserves(self):
        from types import SimpleNamespace

        empty = {"records": [], "by_session": {}}
        row = self._session()
        success = details._apply_peer_observation(
            lambda *, include_health: (empty, SimpleNamespace(state="success")), [row])
        self.assertEqual(success, "success")
        self.assertIsNone(row.peer_last_recv)
        self.assertIsNone(row.peer_last_sent)
        self.assertEqual(row.peer_sent_1h, 0)
        self.assertEqual(row.peer_obligations, [])

        row = self._session()
        prior = (copy.deepcopy(row.peer_last_recv), copy.deepcopy(row.peer_last_sent),
                 row.peer_sent_1h, row.peer_recv_1h, copy.deepcopy(row.peer_obligations))
        partial = {"records": [{"kind": "partial"}], "by_session": {}}
        failed = details._apply_peer_observation(
            lambda *, include_health: (partial, SimpleNamespace(state="failed")), [row])
        self.assertEqual(failed, "failed")
        self.assertEqual((row.peer_last_recv, row.peer_last_sent, row.peer_sent_1h,
                          row.peer_recv_1h, row.peer_obligations), prior)

    def test_legacy_empty_peer_mapping_is_unknown_and_absent_does_not_clear_last_good(self):
        row = self._session()
        prior = copy.deepcopy(row.peer_last_recv)
        state = details._apply_peer_observation(
            lambda: {"records": [], "by_session": {}}, [row])
        self.assertEqual(state, "unknown")
        self.assertEqual(row.peer_last_recv, prior)

        current = self._session()
        current.peer_last_recv = current.peer_last_sent = None
        current.peer_sent_1h = current.peer_recv_1h = 0
        current.peer_obligations = []
        previous = LiveSnapshot(sessions=[self._session()],
                                observations={"optional:peers": {"state": "success"}})
        current_snapshot = LiveSnapshot(sessions=[current],
                                        observations={"optional:peers": {"state": "absent"}})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, current_snapshot))
        self.assertEqual(retained.snapshot.sessions[0].peer_last_recv["from_session_id"], "sid-peer")

    def test_optional_last_good_does_not_cross_changed_binding_or_confirmed_empty(self):
        previous = LiveSnapshot(
            sessions=[self._session(route_id="route-old")], memory={"by_repo": {"repo": [1]}},
            observations={"optional:memory": {"state": "success"},
                          "optional:peers": {"state": "success"}})
        changed_row = self._session(route_id="route-new")
        changed_row.peer_last_recv = None
        changed_row.peer_last_sent = None
        changed_row.peer_sent_1h = 0
        changed_row.peer_recv_1h = 0
        changed_row.peer_obligations = []
        changed = LiveSnapshot(sessions=[changed_row], memory=None)
        changed.observations = {"optional:memory": {"state": "absent"},
                                "optional:peers": {"state": "success"}}
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, changed))
        self.assertIsNone(retained.snapshot.memory)
        self.assertIsNone(retained.snapshot.sessions[0].peer_last_recv)
        self.assertEqual(retained.snapshot.sessions[0].peer_sent_1h, 0)

    def _steward_session(self, *, route_id=None, steward=True):
        row = self._session(route_id=route_id)
        row.steward = steward
        row.steward_targets = ([{"harness": "codex", "session_id": "sid-worker", "source": "watch"}]
                               if steward else None)
        row.steward_parents = None
        return row

    def test_required_pass_keeps_last_judged_steward_role_through_merge(self):
        # Only the optional pass judges the role; the next required pass carries
        # the model default False, which must not overwrite a judged steward.
        previous = LiveSnapshot(sessions=[self._steward_session()],
                                observations={"optional:steward": {"state": "success",
                                                                    "observed_at": 10}})
        required = LiveSnapshot(sessions=[self._session()], observations={})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, required))
        row = retained.snapshot.sessions[0]
        self.assertTrue(row.steward)
        self.assertEqual(row.steward_targets[0]["session_id"], "sid-worker")
        self.assertEqual(retained.snapshot.observations["optional:steward"]["state"], "success")

        basic = LiveSnapshot(sessions=[self._session()])
        merged = details.merge(basic, details.DetailSnapshot(details.state_key(basic),
                                                             retained.snapshot))
        self.assertTrue(merged.sessions[0].steward)
        self.assertEqual(merged.sessions[0].steward_targets[0]["session_id"], "sid-worker")

        failed = LiveSnapshot(sessions=[self._session()],
                              observations={"optional:steward": {"state": "failed",
                                                                  "observed_at": 11}})
        kept = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, failed))
        self.assertTrue(kept.snapshot.sessions[0].steward)
        self.assertEqual(kept.snapshot.observations["optional:steward"]["state"], "failed")
        self.assertTrue(kept.snapshot.observations["optional:steward"]["last_good"])

    def test_judged_steward_clear_and_changed_binding_do_not_inherit_role(self):
        previous = LiveSnapshot(sessions=[self._steward_session(route_id="route-old")],
                                observations={"optional:steward": {"state": "success"}})
        cleared = LiveSnapshot(sessions=[self._steward_session(route_id="route-old",
                                                               steward=False)],
                               observations={"optional:steward": {"state": "success"}})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, cleared))
        self.assertFalse(retained.snapshot.sessions[0].steward)
        self.assertIsNone(retained.snapshot.sessions[0].steward_targets)

        moved = LiveSnapshot(sessions=[self._session(route_id="route-new")], observations={})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, previous), details.DetailSnapshot({}, moved))
        self.assertFalse(retained.snapshot.sessions[0].steward)
        self.assertIsNone(retained.snapshot.sessions[0].steward_targets)

        never_judged = LiveSnapshot(sessions=[self._steward_session()], observations={})
        required = LiveSnapshot(sessions=[self._session()], observations={})
        retained = details.retain_optional_last_good(
            details.DetailSnapshot({}, never_judged), details.DetailSnapshot({}, required))
        self.assertFalse(retained.snapshot.sessions[0].steward)
        self.assertNotIn("optional:steward", retained.snapshot.observations)

    def test_required_details_need_success_and_exact_basic_identity(self):
        pending = _basic("basic-1")
        self.assertFalse(pending.required_details_ready)
        self.assertFalse(required_details_complete(pending.observations))

        set_required_details(pending, "ok", all_new=True, observed_at=101.0)
        self.assertTrue(pending.required_details_ready)
        self.assertTrue(required_details_all_new(pending.observations))

        pending.observations["required-details"]["generation_identity"] = "basic-older"
        self.assertFalse(pending.required_details_ready)

    def test_readiness_and_all_new_are_distinct(self):
        continued = _basic("basic-2")
        set_required_details(continued, "ok", all_new=False, observed_at=102.0)
        self.assertTrue(continued.required_details_ready)
        self.assertFalse(continued.required_details_all_new)

    def test_usage_cache_read_completes_while_http_refresh_remains_pending(self):
        observed = _usage_cache_observation(
            {"freshness": "stale", "observed_at": 99.0}, refresh_pending=True)
        self.assertEqual(observed["state"], "ok")
        self.assertEqual(observed["cache_state"], "stale")
        self.assertEqual(observed["refresh_state"], "pending")

        failed = _usage_cache_observation(None)
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["attempted"])

    def test_required_projection_rejects_missing_route_proof_and_required_read_errors(self):
        row = DispatchJob(key="execute", slug="worker", route_id="route-1")
        row.work_projection = WorkProjection(
            route_id="route-1", route_hash="sha256:route",
            _route_view={"record": {"route_id": "route-1", "route_hash": "sha256:route"}})
        self.assertTrue(required_projection_details_complete([row]))

        row._required_detail_error = True
        self.assertFalse(required_projection_details_complete([row]))
        row._required_detail_error = False
        row.work_projection = WorkProjection(route_id="route-1", ambiguity="route-record-mismatch")
        self.assertFalse(required_projection_details_complete([row]))

    def test_validated_continuation_needs_matching_route_binding(self):
        row = DispatchJob(key="execute", slug="worker", route_id="route-2",
                          route_hash="sha256:route-2")
        row._continuation_validated = True
        row.work_projection = WorkProjection(route_id="route-2", route_hash="sha256:route-2")
        self.assertTrue(required_projection_details_complete([row]))

        row.work_projection = WorkProjection(route_id="route-2", route_hash="sha256:stale")
        self.assertFalse(required_projection_details_complete([row]))

    def test_merge_only_adopts_details_from_the_current_basic_identity(self):
        current = _basic("basic-current")
        completed = copy.deepcopy(current)
        set_required_details(completed, "ok", all_new=True, observed_at=103.0)
        merged = details.merge(
            current, details.DetailSnapshot(details.state_key(current), completed))
        self.assertTrue(merged.required_details_ready)
        self.assertTrue(merged.required_details_all_new)

        old_basic = _basic("basic-old")
        old_detail = copy.deepcopy(old_basic)
        set_required_details(old_detail, "ok", all_new=True, observed_at=104.0)
        newer_basic = _basic("basic-newer")
        stale = details.merge(
            newer_basic, details.DetailSnapshot(details.state_key(old_basic), old_detail))
        self.assertFalse(stale.required_details_ready)
        self.assertEqual(stale.observations["required-details"]["state"], "pending")

    def test_complete_basic_pass_skips_a_second_required_detail_pass(self):
        source = _basic("basic-complete")
        set_required_details(source, "ok", all_new=True, observed_at=105.0)
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._campaign_labels") as campaigns, \
                mock.patch("tools.fleet.collectors._adopt_child_titles") as child_titles, \
                mock.patch("tools.fleet.titles.annotate") as title_details, \
                mock.patch.object(route_chain, "enrich") as route_enrich, \
                mock.patch.object(projection, "attach_projections") as attach, \
                mock.patch("tools.fleet.collectors.dispatch._scan_degradations") as degradations, \
                mock.patch("tools.fleet.work_titles.annotate") as work_titles, \
                mock.patch("tools.fleet.collectors.dispatch._pending_delivery_counts",
                           return_value=None) as deliveries, \
                mock.patch("tools.fleet.collectors.dispatch._candidate_jobs_paths",
                           return_value=[]), \
                mock.patch("tools.fleet.collectors.peer_messages.collect", return_value={}) as peers, \
                mock.patch("tools.fleet.collectors.steward.enrich") as steward, \
                mock.patch("tools.fleet.herdr_projection.refresh_observed_tag_metadata") as panes:
            result = details._enrich(source, required=False)

        steward.assert_called_once()
        self.assertEqual(result.snapshot.observations["optional:steward"]["state"], "success")
        route_enrich.assert_not_called()
        attach.assert_not_called()
        degradations.assert_not_called()
        campaigns.assert_called_once()
        child_titles.assert_called_once()
        title_details.assert_called_once()
        work_titles.assert_called_once()
        deliveries.assert_called_once()
        peers.assert_called_once()
        panes.assert_called_once()
        self.assertTrue(result.snapshot.required_details_ready)

    def test_failed_steward_judgment_is_recorded_not_reported_as_clear(self):
        source = _basic("basic-steward-failed")
        set_required_details(source, "ok", all_new=True, observed_at=106.0)
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._campaign_labels"), \
                mock.patch("tools.fleet.collectors._adopt_child_titles"), \
                mock.patch("tools.fleet.titles.annotate"), \
                mock.patch("tools.fleet.work_titles.annotate"), \
                mock.patch("tools.fleet.collectors.dispatch._pending_delivery_counts",
                           return_value=None), \
                mock.patch("tools.fleet.collectors.dispatch._candidate_jobs_paths",
                           return_value=[]), \
                mock.patch("tools.fleet.collectors.peer_messages.collect", return_value={}), \
                mock.patch("tools.fleet.collectors.steward.enrich",
                           side_effect=RuntimeError("marker root unreadable")), \
                mock.patch("tools.fleet.herdr_projection.refresh_observed_tag_metadata"):
            result = details._enrich(source, required=False)
        self.assertEqual(result.snapshot.observations["optional:steward"]["state"], "failed")

    def test_required_only_pass_defers_optional_detail_sources(self):
        source = _basic("basic-pending")
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._campaign_labels") as campaigns, \
                mock.patch("tools.fleet.collectors._adopt_child_titles") as child_titles, \
                mock.patch("tools.fleet.titles.annotate") as title_details, \
                mock.patch.object(route_chain, "enrich") as route_enrich, \
                mock.patch.object(projection, "attach_projections") as attach, \
                mock.patch("tools.fleet.collectors.dispatch._scan_degradations") as degradations, \
                mock.patch("tools.fleet.work_titles.annotate") as work_titles, \
                mock.patch("tools.fleet.collectors.dispatch._pending_delivery_counts") as deliveries, \
                mock.patch("tools.fleet.collectors.peer_messages.collect") as peers, \
                mock.patch("tools.fleet.collectors.steward.enrich") as steward, \
                mock.patch("tools.fleet.herdr_projection.refresh_observed_tag_metadata") as panes:
            result = details._enrich(source, required_only=True)

        route_enrich.assert_called_once()
        attach.assert_called_once()
        degradations.assert_called_once()
        self.assertNotIn("optional:steward", result.snapshot.observations)
        for optional in (campaigns, child_titles, title_details, work_titles,
                         deliveries, peers, steward, panes):
            optional.assert_not_called()
        self.assertTrue(result.snapshot.required_details_ready)

    def test_required_detail_keeps_default_report_verification_policy(self):
        source = _basic("basic-report-policy")
        with mock.patch("tools.fleet.collectors.dispatch._fill_locations"), \
                mock.patch("tools.fleet.collectors.dispatch._scan_degradations",
                           return_value={}), \
                mock.patch.object(route_chain, "enrich"), \
                mock.patch.object(projection, "attach_projections") as attach, \
                mock.patch.object(projection, "required_projection_details_complete",
                                  return_value=True):
            details._enrich(source, required_only=True)
            self.assertIs(attach.call_args.kwargs["include_report_verification"], True)

            attach.reset_mock()
            details._enrich(source, required_only=True,
                            include_report_verification=False)
            self.assertIs(attach.call_args.kwargs["include_report_verification"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
