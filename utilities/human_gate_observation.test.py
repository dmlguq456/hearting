#!/usr/bin/env python3
"""Read-only lifecycle projection fixtures for Fleet human-gate waits."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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

_REAL_PROBE = H._session_liveness_probe


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
        # Never read the developer's live Codex rollouts or session processes: a
        # real pending question or a real owner exit would change these fixtures.
        for patch in (mock.patch.object(H, "_codex_rollout_waits",
                                        return_value={"complete": True, "pending": {}}),
                      mock.patch.object(H, "_session_liveness_probe",
                                        lambda: (lambda harness, sid: "unknown"))):
            patch.start()
            self.addCleanup(patch.stop)
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

    def test_a_kept_wait_judgment_names_every_gate_file_it_read(self):
        """The judgment of a route is reused only while the journal, the interview artifact and both
        answer files are the same files: an answer that appears or changes next to the artifact is an input."""
        key = (self.route["route_id"], self.route["route_hash"], str(self.root / "route.json"))
        args = (key, self.route, dict(self.route), None, self.jobs, self.root / "workflow")
        H._WAIT_MEMO.clear()
        start = H.metrics_snapshot()

        def moved():
            now = H.metrics_snapshot()
            return (now["route_wait_recomputes"] - start["route_wait_recomputes"],
                    now["route_wait_reuses"] - start["route_wait_reuses"])

        first = H._waits_for(*args)
        self.assertEqual(len(first), 1)
        self.assertEqual(H._waits_for(*args), first)
        self.assertEqual(moved(), (1, 1))
        answers = self.artifacts / "answers.json"
        answers.write_text("{}\n", encoding="utf-8")                       # an answer file appears
        H._waits_for(*args)
        self.assertEqual(moved(), (2, 1))
        answers.write_text('{"answers": {}}\n', encoding="utf-8")          # and changes
        H._waits_for(*args)
        self.assertEqual(moved(), (3, 1))
        H._waits_for(*args)
        self.assertEqual(moved(), (3, 2))
        journal = self.root / "workflow" / self.route["route_id"] / "journal.jsonl"
        journal.write_text(journal.read_text(encoding="utf-8").replace("frame-review", "other-gate"),
                           encoding="utf-8")                                  # the journal changes
        self.assertEqual(H._waits_for(*args)[0]["gate_id"], "other-gate")
        self.assertEqual(moved(), (4, 2))
        self.assertEqual(H._waits_for(*args)[0]["gate_id"], "other-gate")
        self.assertEqual(moved(), (4, 3))
        # A caller that changes what it was given gets its own copy of a kept answer.
        first = H._waits_for(*args)
        first[0]["gate_id"] = "tampered"
        self.assertEqual(H._waits_for(*args)[0]["gate_id"], "other-gate")

    def test_a_kept_wait_judgment_sees_an_archived_interview_restored(self):
        """2026-11-13 (PR #1595 review): an interview that was missing when the judgment was made is
        an input too. Archived for a while, the answered question waited; restored next to its valid
        answer, the kept judgment must not keep the question waiting."""
        import frame_interview as FI
        doc = {"schema": FI.SCHEMA, "route_id": self.route["route_id"], "round": 1, "questions": []}
        answer = FI.answers_template(doc)
        answer.update(actor_kind="user", understanding_confirmed=True)
        self.interview.write_text(json.dumps(doc), encoding="utf-8")
        (self.artifacts / "answers.json").write_text(json.dumps(answer), encoding="utf-8")
        key = (self.route["route_id"], self.route["route_hash"], str(self.root / "route.json"))
        args = (key, self.route, dict(self.route), None, self.jobs, self.root / "workflow")
        H._WAIT_MEMO.clear()
        self.assertEqual(H._waits_for(*args), [])                   # answered: nothing waits
        archive = self.root / "archive"
        archive.mkdir()
        self.interview.rename(archive / "interview.json")           # the interview is archived
        self.assertEqual(len(H._waits_for(*args)), 1)               # missing evidence clears nothing
        self.assertEqual(len(H._waits_for(*args)), 1)
        (archive / "interview.json").rename(self.interview)         # and restored
        self.assertEqual(H._waits_for(*args), [])
        self.assertEqual(H._waits_for(*args), [])

    def test_a_kept_wait_judgment_follows_the_ledger_the_registry_now_selects(self):
        """2026-11-13 (PR #1595 review): the same registry path can select another ledger when a
        directory on it now links elsewhere; a wait raised only in the new ledger must be seen."""
        import workflow_state as WS
        for name in ("old", "new"):
            (self.root / name).mkdir()
            (self.root / name / "jobs.log").write_text("", encoding="utf-8")
        journal = self.root / "new" / "workflow" / self.route["route_id"] / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps({
            "at": "2026-10-14T01:02:03Z", "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"], "workflow_state": "BLOCKED_HUMAN_GATE",
            "evidence": {"gate": "review"}}) + "\n", encoding="utf-8")
        current = self.root / "current"
        current.symlink_to(self.root / "old", target_is_directory=True)
        jobs = current / "jobs.log"
        key = (self.route["route_id"], self.route["route_hash"], str(self.root / "route.json"))
        H._WAIT_MEMO.clear()

        def waits():
            return H._waits_for(key, self.route, dict(self.route), None, jobs, WS.ledger_root_for(jobs)[0])

        self.assertEqual(waits(), [])
        self.assertEqual(waits(), [])
        current.unlink()
        current.symlink_to(self.root / "new", target_is_directory=True)
        self.assertEqual([wait["gate_id"] for wait in waits()], ["review"])
        self.assertEqual([wait["gate_id"] for wait in waits()], ["review"])

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

    @staticmethod
    def _s3(result):
        """(current, folded, ownerless) blocked owners in the list. A blocked owner is listed for its
        parent agent but counted by no person surface (11-14): ``total`` never includes it."""
        rows = [item for item in result["items"] if item["source"] == "S3"]
        return (sum(not item["stale"] for item in rows), sum(bool(item["stale"]) for item in rows),
                sum(item.get("fold") == "ownerless" for item in rows))

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

    def test_one_selected_wait_reads_only_its_own_route(self):
        """2026-11-11: opening one Fleet question re-observed every older wait.

        With 13 older blocked owners each selected read verified every owner's
        route on the shared artifact root (2,295 NAS reads per S3 row).
        """
        route, attempt, _lease, metadata = self._blocked_owner_fixture()
        rows = []
        for index, name in enumerate(("att-owner", "att-other-a", "att-other-b")):
            route_file = self.root / (name + ".route.json")
            route_file.write_text(json.dumps(route), encoding="utf-8")
            row = (metadata.replace("attempt_id=" + attempt, "attempt_id=" + name)
                   .replace("route_file=" + str(self.root / "owner.route.json"),
                            "route_file=" + str(route_file)))
            rows.append("%d\tdone\t/repo\t/wt\towner\t%s,note=dead-worker-blocked,"
                        "failure_class=blocked\n" % (300 + index, row))
        self.jobs.write_text("".join(rows), encoding="utf-8")
        full = H.read_answer_waits(jobs_paths=[self.jobs], now=400)
        self.assertEqual(sorted(item["id"] for item in full["items"]),
                         ["S3:att-other-a", "S3:att-other-b", "S3:att-owner"])
        with mock.patch.object(H, "_route_outcome", wraps=H._route_outcome) as outcome, \
             mock.patch.object(H, "_codex_rollout_waits") as rollouts:
            one = H.read_answer_waits(jobs_paths=[self.jobs], now=400,
                                      only_id="S3:att-other-a")
        self.assertEqual([item["id"] for item in one["items"]], ["S3:att-other-a"])
        self.assertEqual(outcome.call_count, 1)
        rollouts.assert_not_called()
        self.assertTrue(one["complete"])
        selected = next(item for item in full["items"] if item["id"] == "S3:att-other-a")
        self.assertEqual({key: one["items"][0][key] for key in ("route_id", "since", "owner")},
                         {key: selected[key] for key in ("route_id", "since", "owner")})

    def test_last_pass_route_paths_follow_the_latest_full_pass_only(self):
        """The wake list is built from the routes the latest full pass saw, not from ``_WAIT_MEMO``."""
        route, _attempt, _lease, metadata = self._blocked_owner_fixture()
        files = []
        rows = []
        for name in ("att-a", "att-b"):
            route_file = self.root / (name + ".route.json")
            route_file.write_text(json.dumps(route), encoding="utf-8")
            files.append(str(route_file))
            row = (metadata.replace("attempt_id=att-owner", "attempt_id=" + name)
                   .replace("route_file=" + str(self.root / "owner.route.json"),
                            "route_file=" + str(route_file)))
            rows.append("300\tdone\t/repo\t/wt\towner\t%s,note=dead-worker-blocked,"
                        "failure_class=blocked\n" % row)
        self.jobs.write_text("".join(rows), encoding="utf-8")
        H._WAIT_MEMO.clear()
        H.read_answer_waits(jobs_paths=[self.jobs], now=400)
        both = H.last_pass_route_paths()
        self.assertEqual(both, tuple(sorted(both)))
        self.assertTrue(set(files) <= set(both))
        # The second full pass sees route B only: A is no longer a wake source.
        self.jobs.write_text(rows[1], encoding="utf-8")
        H.read_answer_waits(jobs_paths=[self.jobs], now=401)
        only_b = H.last_pass_route_paths()
        self.assertIn(files[1], only_b)
        self.assertNotIn(files[0], only_b)
        # An ``only_id`` pass never replaces the list, even though it reads route B.
        self.jobs.write_text("".join(rows), encoding="utf-8")
        H.read_answer_waits(jobs_paths=[self.jobs], now=402, only_id="S3:att-a")
        self.assertEqual(H.last_pass_route_paths(), only_b)
        # A full pass over another registry that cannot be read names nothing (the first one is
        # no longer listed), while the memo keeps its old entries.
        H.read_answer_waits(jobs_paths=[self.jobs], now=403)
        self.assertTrue(set(files) <= set(H.last_pass_route_paths()))
        self.assertTrue(H._WAIT_MEMO)
        H.read_answer_waits(jobs_paths=[self.root / "missing" / "jobs.log"], now=404)
        self.assertEqual(H.last_pass_route_paths(), ())
        self.assertTrue(H._WAIT_MEMO, "the accessor must not mirror the memo")
        # A listed registry this pass cannot read confirms none of its routes settled: the paths
        # it named last stay.
        H.read_answer_waits(jobs_paths=[self.jobs], now=405)
        named = H.last_pass_route_paths()
        self.assertTrue(set(files) <= set(named))
        with mock.patch("dispatch_registry_cache.registry_snapshot", side_effect=OSError("synthetic")):
            H.read_answer_waits(jobs_paths=[self.jobs], now=406)
        self.assertEqual(H.last_pass_route_paths(), named)

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
            self.assertEqual(self._s3(H.read_answer_waits(jobs_paths=[self.jobs], now=301))[0], 1)
            first_counts = (route_load.call_count, outcome_load.call_count)
            self.assertEqual(self._s3(H.read_answer_waits(jobs_paths=[self.jobs], now=302))[0], 1)
            self.assertEqual((route_load.call_count, outcome_load.call_count), first_counts)
            outcome_file = Path(self.root / "owner.route.outcome.json")
            outcome_file.write_text(json.dumps({"route_id": route["route_id"],
                "route_hash": route["route_hash"], "closed_at": "fixture", "match": True}),
                encoding="utf-8")
            self.assertEqual(self._s3(H.read_answer_waits(jobs_paths=[self.jobs], now=303))[0], 0)
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

    def _handed_over_owner_fixture(self):
        route, attempt, _lease, metadata = self._blocked_owner_fixture()
        # Transfer the open job through the existing writer, then observe its
        # blocked result. The parent carrier identifies opencode without a
        # parent_harness field; the typed handover moves its exact job to Claude.
        instant = time.time()
        self.jobs.write_text(
            f"{instant}\topen\t/repo\t/wt\towner\t" + metadata + "\n", encoding="utf-8")
        state_home = self.root / "xdg-state"
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        seat = tidy.Seat("pane", "fixture-seat", "fixture:pane")
        with mock.patch.dict("os.environ", {"XDG_STATE_HOME": str(state_home)}):
            row = handover.record_retire_handover(
                "old-parent", "opencode", "new-parent", "claude",
                jobs=self.jobs, now=instant - 1, successor_seat=seat)
        self.assertIsNotNone(row)
        self.assertEqual(row["bindings"], [{
            "jobs": str(self.jobs.resolve()), "route": route["route_id"],
            "hash": route["route_hash"], "node": "", "attempt": attempt, "parent": "old-parent"}])
        self.jobs.write_text(
            f"{instant}\tdone\t/repo\t/wt\towner\t" + metadata +
            ",note=dead-worker-blocked,failure_class=blocked\n", encoding="utf-8")
        import dispatch_registry_cache as registry_cache
        H._GENERATION_CACHE.clear()
        with registry_cache.registry_read_scope():
            candidate_view = registry_cache.registry_snapshot(self.jobs).view
        self.assertTrue(H._generation_candidates(candidate_view)[3])
        wait = {"gate_id": "fixture-gate", "kind": "decision", "source": "hearting-ledger",
                "waiting_since": instant, "route_id": route["route_id"]}
        return state_home, instant, wait, candidate_view

    def _assert_wait_owners(self, result, session, harness):
        attributed = {item["source"]: item for item in result["items"]}
        self.assertEqual(set(attributed), {"S2", "S3"}, repr(result))
        for item in attributed.values():
            self.assertEqual(item["attribution"], "lineage" if session else "unattributed")
            self.assertEqual(item["owner"]["session_id"], session)
            self.assertEqual(item["owner"]["harness"], harness)

    def test_owner_handover_lineage_survives_cached_generation_for_s2_and_s3(self):
        state_home, instant, wait, candidate_view = self._handed_over_owner_fixture()
        with mock.patch.dict("os.environ", {"XDG_STATE_HOME": str(state_home)}), \
                mock.patch.object(H, "pending_human_waits", return_value=[wait]):
            first = H.read_answer_waits(jobs_paths=[self.jobs], now=instant)
            second = H.read_answer_waits(jobs_paths=[self.jobs], now=instant + 1)
        for result in (first, second):
            self._assert_wait_owners(result, "new-parent", "claude")
        import dispatch_registry_cache as registry_cache
        with registry_cache.registry_read_scope():
            view = registry_cache.registry_snapshot(self.jobs).view
        self.assertIn((view.path_identity, view.signature), H._GENERATION_CACHE)
        self.assertIs(H._generation_candidates(view), H._generation_candidates(candidate_view))

    def test_cached_generation_does_not_turn_damaged_handover_into_parent_authority(self):
        state_home, instant, wait, candidate_view = self._handed_over_owner_fixture()
        snapshot_path = state_home / "hearting/session-tidy/handover/fixture-seat.json"
        original = snapshot_path.read_bytes()
        with mock.patch.dict("os.environ", {"XDG_STATE_HOME": str(state_home)}), \
                mock.patch.object(H, "pending_human_waits", return_value=[wait]):
            self._assert_wait_owners(H.read_answer_waits(jobs_paths=[self.jobs], now=instant),
                                     "new-parent", "claude")
            for missing in ("kind", "attempt"):
                with self.subTest(missing=missing):
                    damaged = json.loads(original)
                    if missing == "kind":
                        del damaged["seat"]["kind"]
                    else:
                        del damaged["bindings"][0]["attempt"]
                    snapshot_path.write_text(json.dumps(damaged), encoding="utf-8")
                    for offset in (1, 2):
                        self._assert_wait_owners(
                            H.read_answer_waits(jobs_paths=[self.jobs], now=instant + offset), None, None)
                    snapshot_path.write_bytes(original)
                    self._assert_wait_owners(
                        H.read_answer_waits(jobs_paths=[self.jobs], now=instant + 3), "new-parent", "claude")
        import dispatch_registry_cache as registry_cache
        with registry_cache.registry_read_scope():
            view = registry_cache.registry_snapshot(self.jobs).view
        self.assertEqual(view.signature, candidate_view.signature)
        self.assertIs(H._generation_candidates(view), H._generation_candidates(candidate_view))

    def test_advisory_lease_contention_keeps_reader_safe_and_unknown(self):
        _route, attempt, lease, metadata = self._blocked_owner_fixture()
        held = hold_supervisor_lease(self.jobs, attempt, lease)
        held.__enter__()
        self.addCleanup(held.__exit__, None, None, None)
        self.jobs.write_text("300\tdone\t/repo\t/wt\towner\t" + metadata +
                             ",note=dead-worker-blocked,failure_class=blocked\n", encoding="utf-8")
        result = H.read_answer_waits(jobs_paths=[self.jobs], now=300)
        self.assertTrue(result["complete"])
        self.assertEqual((self._s3(result)[0], result["total"]), (1, 0))
        self.assertEqual(result["items"][0]["source"], "S3")

    def test_a_blocked_owner_whose_parent_took_its_result_waits_for_no_one(self):
        # 11-14: a finished owner whose parent already holds its exact ACKed completion receipt
        # (BLOCKED included) closes like a closed route; an unproven delivery keeps the wait.
        _route, attempt, _lease, metadata = self._blocked_owner_fixture()
        self.jobs.write_text("300\tdone\t/repo\t/wt\towner\t" + metadata +
                             ",note=dead-worker-blocked,failure_class=blocked\n", encoding="utf-8")
        import dispatch_completion_join
        with mock.patch.object(dispatch_completion_join, "acknowledged_attempt_receipt",
                               return_value=None) as unproven:
            kept = H.read_answer_waits(jobs_paths=[self.jobs], now=301)
        self.assertEqual(self._s3(kept)[0], 1)
        self.assertEqual((unproven.call_args.args[1], unproven.call_args.kwargs["recipient_sid"],
                          unproven.call_args.kwargs["parent_completion_delivery"]),
                         (attempt, "old-parent", "opencode-turn"))
        with mock.patch.object(dispatch_completion_join, "acknowledged_attempt_receipt",
                               return_value={"verdict": "BLOCKED"}):
            taken = H.read_answer_waits(jobs_paths=[self.jobs], now=302)
        self.assertEqual([item for item in taken["items"] if item["source"] == "S3"], [])
        with mock.patch.object(dispatch_completion_join, "acknowledged_attempt_receipt",
                               side_effect=OSError("unreadable")):
            unreadable = H.read_answer_waits(jobs_paths=[self.jobs], now=303)
        self.assertEqual(self._s3(unreadable)[0], 1)

    def test_blocked_owner_folds_at_72_hours_and_age_never_drops_it(self):
        self._blocked_owner_fixture()
        terminal = self.jobs.read_text(encoding="utf-8").splitlines()[0].split("\t")[-1]
        terminal += ",note=dead-worker-blocked,failure_class=blocked"
        self.jobs.write_text("300\tdone\t/repo\t/wt\towner\t" + terminal + "\n", encoding="utf-8")
        current = H.read_answer_waits(jobs_paths=[self.jobs], now=300 + 72 * 3600 - 1)
        self.assertEqual(self._s3(current)[:2], (1, 0))

        self.jobs.write_text("299\tdone\t/repo\t/wt\towner\t" +
                             terminal + "\n", encoding="utf-8")
        folded = H.read_answer_waits(jobs_paths=[self.jobs], now=300 + 72 * 3600)
        self.assertEqual(self._s3(folded)[:2], (0, 1))
        self.assertEqual((folded["total"], folded["stale_total"]), (0, 0))
        self.assertTrue(folded["items"][0]["stale"])
        self.assertEqual(folded["items"][0]["fold"], "older")

        # The former seven-day S3 cutoff silently removed an unanswered wait; the
        # shared rule only folds it (user decision 2026-10-26: age never closes).
        self.jobs.write_text("299\tdone\t/repo\t/wt\towner\t" +
                             terminal + "\n", encoding="utf-8")
        for days in (7, 30):
            kept = H.read_answer_waits(jobs_paths=[self.jobs], now=300 + days * 86400)
            self.assertEqual([item["source"] for item in kept["items"]], ["S3"])
            self.assertEqual(self._s3(kept)[:2], (0, 1))
            self.assertEqual(kept["items"][0]["fold"], "older")

    def test_proven_owner_exit_folds_as_ownerless_and_unobserved_owner_stays_current(self):
        _route, attempt, _lease, metadata = self._blocked_owner_fixture()
        self.jobs.write_text("300\tdone\t/repo\t/wt\towner\t" + metadata +
                             ",note=dead-worker-blocked,failure_class=blocked,parent_harness=codex\n",
                             encoding="utf-8")
        seen = []

        def probe(answer):
            def liveness(harness, session_id):
                seen.append((harness, session_id))
                return answer
            return liveness

        ended = H.read_answer_waits(jobs_paths=[self.jobs], now=400, owner_liveness=probe("ended"))
        item = ended["items"][0]
        self.assertEqual((item["owner_state"], item["fold"], item["stale"]),
                         ("none", "ownerless", True))
        self.assertEqual(self._s3(ended), (0, 1, 1))
        self.assertEqual(ended["by_session"], {})
        self.assertIn(("codex", "old-parent"), seen)

        unknown = H.read_answer_waits(jobs_paths=[self.jobs], now=400,
                                      owner_liveness=probe("unknown"))
        self.assertEqual((unknown["items"][0]["owner_state"], unknown["items"][0]["fold"]),
                         ("unknown", None))
        self.assertEqual(self._s3(unknown)[::2], (1, 0))

        live = H.read_answer_waits(jobs_paths=[self.jobs], now=400, owner_liveness=probe("live"))
        self.assertEqual(live["items"][0]["owner_state"], "live")
        self.assertNotIn("registered_owner", live["items"][0])
        # Listed for its parent agent, in no session's question index (11-14).
        self.assertEqual(live["by_session"], {})

    def test_handover_successor_answers_and_keeps_the_registered_parent_visible(self):
        _route, attempt, _lease, metadata = self._blocked_owner_fixture()
        self.jobs.write_text("300\tdone\t/repo\t/wt\towner\t" + metadata +
                             ",note=dead-worker-blocked,failure_class=blocked,parent_harness=codex\n",
                             encoding="utf-8")
        import dispatch_seat_handover
        with mock.patch.object(dispatch_seat_handover, "effective_parent", return_value="new-parent"), \
             mock.patch.object(dispatch_seat_handover, "effective_parent_harness", return_value="codex"):
            result = H.read_answer_waits(jobs_paths=[self.jobs], now=400,
                                         owner_liveness=lambda harness, sid: (
                                             "live" if sid == "new-parent" else "ended"))
        item = result["items"][0]
        self.assertEqual(item["owner"]["session_id"], "new-parent")
        self.assertEqual(item["registered_owner"], {"harness": "codex", "session_id": "old-parent"})
        self.assertEqual((item["owner_state"], item["fold"], self._s3(result)[0]), ("successor", None, 1))
        self.assertEqual(item["id"], "S3:" + attempt)

    def test_codex_question_only_in_its_rollout_is_kept_and_sidecar_wins(self):
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet import interaction

        now = 10_000.0
        with mock.patch.dict("os.environ", {
            "FLEET_INTERACTION_STATE_DIR": str(self.root / "interactions"),
            "XDG_STATE_HOME": str(self.root / "xdg"),
        }), mock.patch.object(H, "_codex_rollout_waits", return_value={"complete": True, "pending": {
                "rollout-only": now - 60, "with-sidecar": now - 30}}):
            self.assertTrue(interaction.set_wait("with-sidecar", "codex", "decision",
                                                 "codex-appserver", now=now - 30,
                                                 request_id="req-1", turn_id="turn-1"))
            result = H.read_answer_waits(jobs_paths=[], now=now,
                                         owner_liveness=lambda harness, sid: "live")
        by_session = {item["owner"]["session_id"]: item for item in result["items"]}
        self.assertEqual(set(by_session), {"rollout-only", "with-sidecar"})
        rollout = by_session["rollout-only"]
        self.assertEqual(rollout["id"], "S1:codex:rollout-only:%d" % int((now - 60) * 1000))
        self.assertEqual((rollout["source"], rollout["kind"], rollout["owner_state"]),
                         ("S1", "decision", "live"))
        self.assertEqual(by_session["with-sidecar"]["request_id"], "req-1")
        self.assertEqual((result["total"], result["complete"]), (2, True))

    def test_unreadable_or_partial_codex_rollouts_are_unknown_not_an_empty_board(self):
        env = {"FLEET_INTERACTION_STATE_DIR": str(self.root / "interactions"),
               "XDG_STATE_HOME": str(self.root / "xdg")}
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(H, "_codex_rollout_waits", return_value=None):
            result = H.read_answer_waits(jobs_paths=[], now=100)
        self.assertFalse(result["complete"])
        self.assertIn({"source": "S1", "state": "unknown", "reason": "codex-rollouts-unreadable"},
                      result["health"])
        partial = {"complete": False, "pending": {"seen-sid": 90.0}}
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(H, "_codex_rollout_waits", return_value=partial):
            result = H.read_answer_waits(jobs_paths=[], now=100)
        self.assertFalse(result["complete"])
        self.assertIn({"source": "S1", "state": "unknown", "reason": "codex-rollouts-partial"},
                      result["health"])
        self.assertEqual([item["owner"]["session_id"] for item in result["items"]], ["seen-sid"])

    def test_failed_lineage_read_keeps_the_owner_unknown_not_ownerless(self):
        self._blocked_owner_fixture()
        terminal = self.jobs.read_text(encoding="utf-8").splitlines()[0].split("\t")[-1]
        self.jobs.write_text("300\tdone\t/repo\t/wt\towner\t" + terminal +
                             ",note=dead-worker-blocked,failure_class=blocked,parent_harness=codex\n",
                             encoding="utf-8")
        import dispatch_seat_handover
        with mock.patch.object(dispatch_seat_handover, "effective_parent",
                               side_effect=OSError("handover-unreadable")):
            result = H.read_answer_waits(jobs_paths=[self.jobs], now=400,
                                         owner_liveness=lambda harness, sid: "ended")
        item = result["items"][0]
        self.assertEqual(item["owner"]["session_id"], "old-parent")
        self.assertEqual((item["owner_state"], item["fold"], self._s3(result)[0]), ("unknown", None, 1))

    def test_owner_probe_outside_the_initial_pid_namespace_proves_no_exit(self):
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import procscan, steward
        # setUp stubs the module's probe; _REAL_PROBE is the shipped one.
        with mock.patch.object(procscan, "initial_pid_namespace", return_value=False), \
                mock.patch.object(steward, "session_liveness", return_value="ended") as seen:
            probe = _REAL_PROBE()
            self.assertEqual(probe("claude", "sid-hidden"), "unknown")
        seen.assert_not_called()
        with mock.patch.object(procscan, "initial_pid_namespace", return_value=True), \
                mock.patch.object(steward, "session_liveness", return_value="ended"):
            self.assertEqual(_REAL_PROBE()("claude", "sid-host"), "ended")

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
            # The judgment is replaced by one that answers differently for the same inputs, so
            # what the pass kept of its answer for those inputs is dropped between the passes.
            H._WAIT_MEMO.clear()
            pending.since = instant - 72 * 3600 + 1
            current = H.read_answer_waits(jobs_paths=[self.jobs], now=instant)
            self.assertEqual((current["total"], current["stale_total"]), (1, 0))
            H._WAIT_MEMO.clear()
            pending.since = instant - 72 * 3600
            older = H.read_answer_waits(jobs_paths=[self.jobs], now=instant)
            self.assertEqual((older["total"], older["stale_total"]), (0, 1))
            H._WAIT_MEMO.clear()
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
            # The marker question (S4 without a reply surface) is answered in its own session's
            # input: listed, folded, never counted by a person surface (11-14).
            self.assertEqual((result["total"], result["stale_total"], result["by_session"]),
                             (0, 2, {}))
            self.assertTrue(all(item["stale"] for item in result["items"]))
            self.assertTrue(human_wait_state.publish_cache(result))
            projection = human_wait_state._read_cache_file()
            fleet_cache = json.loads(human_wait_state._fleet_cache_path().read_text(encoding="utf-8"))
            self.assertEqual((projection["total"], projection["older"], projection["by_session"]),
                             (0, 2, {}))
            self.assertEqual((fleet_cache["total"], fleet_cache["older"]), (0, 2))
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
