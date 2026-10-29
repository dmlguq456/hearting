#!/usr/bin/env python3
"""Read-only lifecycle projection fixtures for Fleet human-gate waits."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import human_gate_observation as H  # noqa: E402
from route_identity import route_hash, route_id_from_hash  # noqa: E402
from dispatch_contract import hold_supervisor_lease, supervisor_lease_path  # noqa: E402
import dispatch_owner_input as owner_input  # noqa: E402


class PendingWaitTest(unittest.TestCase):
    def test_invalid_native_sidecar_projects_unknown_without_exposing_body(self):
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet import interaction

        with mock.patch.dict("os.environ", {
            "FLEET_INTERACTION_STATE_DIR": str(self.root / "interactions"),
            "XDG_STATE_HOME": str(self.root / "xdg"),
        }):
            self.assertTrue(interaction.set_wait(
                "damaged-session", "codex", "decision", "codex-appserver", now=time.time()))
            path = Path(interaction.sidecar_path("damaged-session", "codex"))
            path.write_text('{"question":"PRIVATE damaged source"', encoding="utf-8")
            before = path.read_bytes()
            result = H.read_answer_waits(jobs_paths=[])
            self.assertFalse(result["complete"])
            self.assertEqual(result["items"], [])
            self.assertIn({"source": "S1", "state": "unknown", "reason": "invalid-sidecar"},
                          result["health"])
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertEqual(path.read_bytes(), before)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.interview = self.artifacts / "interview.json"
        self.interview.write_text("{}\n", encoding="utf-8")
        self.route = {"artifact_root": str(self.artifacts), "nodes": []}
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        ledger = self.root / "workflow" / self.route["route_id"] / "journal.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            "at": "2026-10-14T01:02:03Z", "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"], "workflow_state": "BLOCKED_HUMAN_GATE",
            "evidence": {"gate": "frame-review", "artifact": str(self.interview)},
        }) + "\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_actual_raise_is_projected_without_interview_content(self):
        waits = H.pending_human_waits(self.route, jobs=self.jobs)
        self.assertEqual(len(waits), 1)
        self.assertEqual((waits[0]["kind"], waits[0]["source"]), ("decision", "hearting-ledger"))
        self.assertEqual(set(waits[0]), {"gate_id", "kind", "source", "waiting_since", "route_id"})

    def test_mismatched_route_identity_has_no_observation(self):
        route = dict(self.route, route_id="rt-unrelated")
        self.assertEqual(H.pending_human_waits(route, jobs=self.jobs), [])

    def test_journal_parse_cache_invalidates_on_same_size_rewrite(self):
        import workflow_state as WS
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        first = H._journal(ledger)
        with mock.patch.object(H.json, "loads", wraps=H.json.loads) as loads:
            self.assertEqual(H._journal(ledger), first)
            self.assertEqual(loads.call_count, 0)
            raw = ledger.journal_path.read_text(encoding="utf-8")
            changed = raw.replace("BLOCKED_HUMAN_GATE", "CANCELLED_________")
            self.assertEqual(len(changed), len(raw))
            ledger.journal_path.write_text(changed, encoding="utf-8")
            parsed = H._journal(ledger)
            self.assertEqual(loads.call_count, 1)
            self.assertEqual(parsed[0]["workflow_state"], "CANCELLED_________")

    def _blocked_owner_fixture(self):
        route = {"artifact_root": str(self.artifacts), "nodes": []}
        route["route_hash"] = route_hash(route)
        route["route_id"] = route_id_from_hash(route["route_hash"])
        route_file = self.root / "owner.route.json"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        attempt = "att-owner"
        lease = supervisor_lease_path(self.jobs, attempt)
        metadata = ",".join((
            "attempt_schema_version=2", "dispatch_depth=1", "worker_type=owner",
            "transport=headless", "execution_surface=registered-headless", "registered_worker=1",
            "completion_delivery=app-server-supervised", "harness=codex", "attempt_id=" + attempt,
            "supervisor_lease=flock-v1",
            "supervisor_lease_nonce=" + "d" * 64, "supervisor_lease_file=" + str(lease),
            "parent_sid=old-parent", "route_id=" + route["route_id"],
            "parent_completion_delivery=opencode-turn", "managed_sealed_batch_id=batch-owner",
            "route_hash=" + route["route_hash"], "route_file=" + str(route_file),
            "owner_route_id=" + route["route_id"], "owner_route_hash=" + route["route_hash"],
            "owner_route_file=" + str(route_file),
        ))
        self.jobs.write_text("100\topen\t/repo\t/wt\towner\t" + metadata + "\n", encoding="utf-8")
        return route, attempt, lease, metadata

    def test_dead_blocked_owner_wait_closes_on_real_retained_answer_and_successor(self):
        _route, attempt, lease, metadata = self._blocked_owner_fixture()
        owner_input.initialize_owner_input(self.jobs, attempt, "codex-next-turn")
        self.jobs.write_text("300\tdone\t/repo\t/wt\towner\t" + metadata +
                             ",note=dead-worker-blocked,failure_class=blocked\n", encoding="utf-8")
        before = self.jobs.read_bytes()
        first = H.read_answer_waits(jobs_paths=[self.jobs], now=300)
        self.assertEqual([item["source"] for item in first["items"]], ["S3"])
        self.assertEqual(first["items"][0]["owner"]["session_id"], "old-parent")
        self.assertEqual(self.jobs.read_bytes(), before)

        owner_input.submit(self.jobs, attempt, "answer from person", "answer-1")
        answered = H.read_answer_waits(jobs_paths=[self.jobs], now=301)
        self.assertEqual(answered["total"], 0)
        import dispatch_supervision as supervision
        notice = supervision.materialize(
            self.jobs, {attempt}, reason=supervision.ANSWER_AWAITING_PARENT)[0]
        import dispatch_pending_delivery as pending_delivery
        self.assertEqual((notice["recipient_kind"], notice["recipient_digest"]),
                         ("opencode-turn", pending_delivery.recipient_digest("old-parent")))
        self.assertTrue(supervision.notice_is_current(notice))
        self.assertIn("answer is kept", supervision.render_text(notice["receipt"]))
        self.jobs.write_text(self.jobs.read_text(encoding="utf-8") +
            "now\topen\t/repo\t/wt\towner\t"
            f"attempt_schema_version=2,dispatch_depth=1,worker_type=owner,attempt_id=att-successor,"
            f"automatic_retry_of={attempt},route_id={_route['route_id']},"
            f"route_hash={_route['route_hash']},route_file={self.root / 'owner.route.json'}\n",
            encoding="utf-8")
        successor = H.read_answer_waits(jobs_paths=[self.jobs], now=302)
        self.assertEqual(successor["total"], 0)

    def test_later_owner_on_different_route_file_does_not_close_wait(self):
        route, _attempt, _lease, metadata = self._blocked_owner_fixture()
        route_file = self.root / "same-route-other-file.json"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        old = metadata + ",note=dead-worker-blocked,failure_class=blocked"
        later = ("attempt_schema_version=2,dispatch_depth=1,worker_type=owner,"
                 "attempt_id=att-other-file,owner_route_id=" + route["route_id"] +
                 ",owner_route_hash=" + route["route_hash"] +
                 ",owner_route_file=" + str(route_file))
        self.jobs.write_text("100\tdone\t/repo\t/wt\towner\t" + old + "\n" +
                             "200\topen\t/repo\t/wt\towner\t" + later + "\n",
                             encoding="utf-8")
        result = H.read_answer_waits(jobs_paths=[self.jobs], now=300)
        self.assertEqual([item["attempt_id"] for item in result["items"] if item["source"] == "S3"],
                         ["att-owner"])

    def test_unchanged_route_and_outcome_are_reused_and_new_outcome_invalidates(self):
        route, attempt, _lease, metadata = self._blocked_owner_fixture()
        self.jobs.write_text("300\tdone\t/repo\t/wt\towner\t" + metadata +
                             ",note=dead-worker-blocked,failure_class=blocked\n", encoding="utf-8")
        from tools.fleet import route as routes
        with mock.patch.object(routes, "load", wraps=routes.load) as route_load, \
             mock.patch.object(routes, "load_outcome", wraps=routes.load_outcome) as outcome_load:
            self.assertEqual(H.read_answer_waits(jobs_paths=[self.jobs], now=301)["total"], 1)
            first_counts = (route_load.call_count, outcome_load.call_count)
            self.assertEqual(H.read_answer_waits(jobs_paths=[self.jobs], now=302)["total"], 1)
            self.assertEqual((route_load.call_count, outcome_load.call_count), first_counts)
            outcome_file = Path(self.root / "owner.route.outcome.json")
            outcome_file.write_text(json.dumps({"route_id": route["route_id"],
                "route_hash": route["route_hash"], "closed_at": "fixture", "match": True}),
                encoding="utf-8")
            self.assertEqual(H.read_answer_waits(jobs_paths=[self.jobs], now=303)["total"], 0)
            self.assertEqual((route_load.call_count, outcome_load.call_count),
                             (first_counts[0] + 1, first_counts[1] + 1))

    def test_candidate_metadata_is_reused_for_an_unchanged_registry_generation(self):
        self._blocked_owner_fixture()
        import dispatch_registry_cache as registry_cache
        with registry_cache.registry_read_scope():
            view = registry_cache.registry_snapshot(self.jobs).view
        H._GENERATION_CACHE.clear()
        original = registry_cache.RegistryRecord.completion_metadata
        with mock.patch.object(registry_cache.RegistryRecord, "completion_metadata",
                               autospec=True, side_effect=original) as metadata_parse:
            first = H._generation_candidates(view)
            first_parse_count = metadata_parse.call_count
            second = H._generation_candidates(view)
            self.assertIs(first, second)
            self.assertGreater(first_parse_count, 0)
            self.assertEqual(metadata_parse.call_count, first_parse_count)

    def test_owner_handover_lineage_survives_cached_generation_for_s2_and_s3(self):
        route, _attempt, _lease, metadata = self._blocked_owner_fixture()
        # A real-shaped owner has a registered parent but no parent_harness. The
        # seat ledger hands its exact route binding to a successor on another harness.
        metadata += ",dispatch_depth=1,route_node="
        instant = time.time()
        self.jobs.write_text(
            f"{instant}\tdone\t/repo\t/wt\towner\t" + metadata +
            ",note=dead-worker-blocked,failure_class=blocked\n", encoding="utf-8")

        state_home = self.root / "xdg-state"
        tidy_root = state_home / "hearting" / "session-tidy"
        (tidy_root / "handover").mkdir(parents=True)
        (tidy_root / "sessions").mkdir(parents=True)
        seat_key = "fixture-seat"
        binding = {"jobs": str(self.jobs.resolve()), "route": route["route_id"],
                   "hash": route["route_hash"], "node": ""}
        snapshot = {"schema": 1, "seat": {"key": seat_key},
                    "from": {"harness": "codex", "sid": "old-parent"},
                    "bindings": [binding]}
        (tidy_root / "handover" / f"{seat_key}.json").write_text(
            json.dumps(snapshot), encoding="utf-8")
        handover = {"event": "handover", "from": "old-parent", "sid": "new-parent",
                    "harness": "claude", "ts": instant - 1, "bindings": [binding]}
        (tidy_root / "sessions" / f"{seat_key}.jsonl").write_text(
            json.dumps(handover) + "\n", encoding="utf-8")

        import dispatch_registry_cache as registry_cache
        H._GENERATION_CACHE.clear()
        with registry_cache.registry_read_scope():
            candidate_view = registry_cache.registry_snapshot(self.jobs).view
        route_candidates = H._generation_candidates(candidate_view)[3]
        self.assertTrue(route_candidates)
        wait = {"gate_id": "fixture-gate", "kind": "decision", "source": "hearting-ledger",
                "waiting_since": instant, "route_id": route["route_id"]}
        with mock.patch.dict("os.environ", {"XDG_STATE_HOME": str(state_home)}), \
                mock.patch.object(H, "pending_human_waits", return_value=[wait]):
            first = H.read_answer_waits(jobs_paths=[self.jobs], now=instant)
            second = H.read_answer_waits(jobs_paths=[self.jobs], now=instant + 1)
        for result in (first, second):
            attributed = {item["source"]: item for item in result["items"]}
            self.assertEqual(set(attributed), {"S2", "S3"}, repr(result))
            for item in attributed.values():
                self.assertEqual(item["attribution"], "lineage")
                self.assertEqual(item["owner"]["session_id"], "new-parent")
                self.assertEqual(item["owner"]["harness"], "claude")
        with registry_cache.registry_read_scope():
            view = registry_cache.registry_snapshot(self.jobs).view
        self.assertIn((view.path_identity, view.signature), H._GENERATION_CACHE)

    def test_advisory_lease_contention_keeps_reader_safe_and_unknown(self):
        _route, attempt, lease, metadata = self._blocked_owner_fixture()
        held = hold_supervisor_lease(self.jobs, attempt, lease)
        held.__enter__()
        self.addCleanup(held.__exit__, None, None, None)
        self.jobs.write_text("300\tdone\t/repo\t/wt\towner\t" + metadata +
                             ",note=dead-worker-blocked,failure_class=blocked\n", encoding="utf-8")
        result = H.read_answer_waits(jobs_paths=[self.jobs], now=300)
        self.assertTrue(result["complete"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["source"], "S3")

    def test_blocked_owner_72_hour_and_seven_day_projection_boundaries(self):
        self._blocked_owner_fixture()
        terminal = self.jobs.read_text(encoding="utf-8").splitlines()[0].split("\t")[-1]
        terminal += ",note=dead-worker-blocked,failure_class=blocked"
        self.jobs.write_text("300\tdone\t/repo\t/wt\towner\t" + terminal + "\n", encoding="utf-8")
        current = H.read_answer_waits(jobs_paths=[self.jobs], now=300 + 72 * 3600 - 1)
        self.assertEqual((current["total"], current["stale_total"]), (1, 0))

        self.jobs.write_text("299\tdone\t/repo\t/wt\towner\t" +
                             terminal + "\n", encoding="utf-8")
        folded = H.read_answer_waits(jobs_paths=[self.jobs], now=300 + 72 * 3600)
        self.assertEqual((folded["total"], folded["stale_total"]), (0, 1))
        self.assertTrue(folded["items"][0]["stale"])

        self.jobs.write_text("299\tdone\t/repo\t/wt\towner\t" +
                             terminal + "\n", encoding="utf-8")
        dropped = H.read_answer_waits(jobs_paths=[self.jobs], now=300 + 7 * 86400)
        self.assertEqual((dropped["items"], dropped["total"], dropped["stale_total"]), ([], 0, 0))

    def test_s2_72_hour_and_older_projection_boundaries(self):
        route, _attempt, _lease, metadata = self._blocked_owner_fixture()
        self.jobs.write_text("100\topen\t/repo\t/wt\towner\t" + metadata + "\n",
                             encoding="utf-8")
        instant = 1000.0

        def pending(_route, *, jobs, outcome=None):
            del jobs, outcome
            return [{"gate_id": "fixture-gate", "kind": "decision", "source": "hearting-ledger",
                     "waiting_since": pending.since, "route_id": route["route_id"]}]

        with mock.patch.object(H, "pending_human_waits", side_effect=pending):
            pending.since = instant - 72 * 3600 + 1
            current = H.read_answer_waits(jobs_paths=[self.jobs], now=instant)
            self.assertEqual((current["total"], current["stale_total"]), (1, 0))
            pending.since = instant - 72 * 3600
            older = H.read_answer_waits(jobs_paths=[self.jobs], now=instant)
            self.assertEqual((older["total"], older["stale_total"]), (0, 1))
            pending.since = instant - 7 * 86400 - 1
            older = H.read_answer_waits(jobs_paths=[self.jobs], now=instant)
            self.assertEqual((older["total"], older["stale_total"],
                              [item["source"] for item in older["items"]]),
                             (0, 1, ["S2"]))

    def test_unresolved_requests_survive_older_window(self):
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet import interaction
        from tools.fleet.collectors.interaction import enrich
        import human_wait_state

        route, _attempt, _lease, metadata = self._blocked_owner_fixture()
        now = time.time()
        waiting_since = now - 9 * 86400
        self.jobs.write_text(f"{waiting_since}\topen\t/repo\t/wt\towner\t{metadata}\n",
                             encoding="utf-8")
        ledger = self.root / "workflow" / route["route_id"] / "journal.jsonl"
        ledger.write_text(json.dumps({
            "at": datetime.fromtimestamp(waiting_since, timezone.utc).isoformat(),
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "workflow_state": "BLOCKED_HUMAN_GATE",
            "evidence": {"gate": "frame-review", "artifact": str(self.interview)},
        }) + "\n", encoding="utf-8")

        interaction_root = self.root / "interaction"
        xdg_state = self.root / "xdg-state"
        with mock.patch.dict("os.environ", {
            "FLEET_INTERACTION_STATE_DIR": str(interaction_root),
            "XDG_STATE_HOME": str(xdg_state),
        }):
            self.assertTrue(interaction.set_wait(
                "native-older", "codex", "decision", "codex-appserver",
                now=waiting_since, request_id="request-older", turn_id="turn-older"))
            signal = human_wait_state.signal_value(
                "codex", "signal-older", "seat-older", "[답변 필요] older fixture",
                sequence=1, now=waiting_since)
            self.assertIsNotNone(signal)
            self.assertTrue(human_wait_state.write_signal(signal))
            self.assertEqual(len(H.pending_human_waits(route, jobs=self.jobs)), 1)

            session = SimpleNamespace(
                harness="codex", session_id="native-older",
                started_at=waiting_since - 100, _interaction_activity=None,
                interaction_state=None,
            )
            enrich(session, now=now)
            self.assertIsNotNone(session.interaction_state)
            self.assertEqual(session.interaction_state["waiting_since"], waiting_since)

            result = H.read_answer_waits(jobs_paths=[self.jobs], now=now)
            self.assertEqual({item["source"] for item in result["items"]}, {"S1", "S2", "S4"},
                             repr(result))
            self.assertEqual((result["total"], result["stale_total"], result["by_session"]),
                             (0, 3, {}))
            self.assertTrue(all(item["stale"] for item in result["items"]))
            self.assertTrue(human_wait_state.publish_cache(result))
            projection = human_wait_state._read_cache_file()
            fleet_cache = json.loads(human_wait_state._fleet_cache_path().read_text(encoding="utf-8"))
            self.assertEqual((projection["total"], projection["older"], projection["by_session"]),
                             (0, 3, {}))
            self.assertEqual((fleet_cache["total"], fleet_cache["older"]), (0, 3))
            self.assertTrue(all("question" not in item for item in fleet_cache["items"]))

            self.assertTrue(interaction.clear_wait("native-older", "codex"))
            self.assertTrue(human_wait_state.close_signal("seat-older", sequence=1))
            ledger.write_text(json.dumps({
                "at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "route_id": route["route_id"], "route_hash": route["route_hash"],
                "workflow_state": "CANCELLED",
            }) + "\n", encoding="utf-8")
            cleared = H.read_answer_waits(jobs_paths=[self.jobs], now=now + 1)
            self.assertEqual((cleared["items"], cleared["total"], cleared["stale_total"]),
                             ([], 0, 0))

    def test_unbound_legacy_blocked_owner_is_not_an_unknown_wait(self):
        self.jobs.write_text("100\tdone\t/repo\t/wt\towner\t"
                             "attempt_schema_version=2,worker_type=owner,attempt_id=att-legacy,"
                             "note=dead-worker-blocked,failure_class=blocked\n", encoding="utf-8")
        result = H.read_answer_waits(jobs_paths=[self.jobs], now=300)
        self.assertTrue(result["complete"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["health"], [])


if __name__ == "__main__":
    unittest.main()
