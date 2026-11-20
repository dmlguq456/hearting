#!/usr/bin/env python3
"""The size rule of a campaign's closing event keeps what was valid valid, and `status` judges what `close` judges.

A payload of 64 KiB or less was always valid, whatever its rows weigh; only a larger one is looked at in parts
(what is not a member row within 64 KiB, the member rows within their allowance for each member).  The one case
that matters in practice is a campaign of a single cycle whose one row is large (a route closed with many
terminal gates unproven).  `campaign-status` builds the closing event the close would publish and asks the same
function, so the two cannot say different things at the read limit of a record."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_campaign as C
import artifact_lifecycle as LC
import artifact_manifest as M
import artifact_producer as P

_spec = importlib.util.spec_from_file_location(
    "campaign_event_size_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)

FLOOR = 64 * 1024


def payload_of(*, row_bytes, rows=1, fixed_bytes=0):
    """A closing payload whose `snapshot.cycles` takes about `row_bytes` in all and whose own text takes `fixed_bytes`."""
    cycles = [{"cycle_id": "cyc_%032x" % n, "pad": "x" * max(0, row_bytes // rows - 40)} for n in range(rows)]
    return {"snapshot": {"campaign": {"cycles": ["cyc_%032x" % n for n in range(rows)], "goal": "g" * fixed_bytes},
                         "cycles": cycles}}


def violations_of(event_type, payload):
    found = []
    members = M._member_lists({"event_type": event_type, "payload": payload})
    M._v_event_payload(payload, "$.payload", found, members)
    return [(v.code, v.detail) for v in found]


class SizeRuleTest(unittest.TestCase):
    def test_a_payload_within_64_kib_is_valid_whatever_its_rows_weigh(self):
        payload = payload_of(row_bytes=40 * 1024, rows=1)  # one member, rows far over 1 KiB a member
        self.assertLess(M._json_size(payload), FLOOR)
        self.assertEqual(violations_of("campaign.satisfied", payload), [])
        with mock.patch.object(M, "_MEMBER_ROW_ALLOWANCE_BYTES", 1):
            self.assertEqual(violations_of("campaign.satisfied", payload), [])

    def test_a_payload_over_64_kib_keeps_its_member_row_allowance(self):
        payload = payload_of(row_bytes=200 * 1024, rows=3)  # 3 members, rows about 68 KiB each
        self.assertGreater(M._json_size(payload), FLOOR)
        found = violations_of("campaign.satisfied", payload)
        self.assertEqual([code for code, _ in found], ["oversized-payload"])
        self.assertIn("member rows take", found[0][1])
        self.assertIn("more than 1024 bytes each", found[0][1])

    def test_a_payload_over_64_kib_keeps_the_limit_on_what_is_not_a_member_row(self):
        payload = payload_of(row_bytes=300, rows=3, fixed_bytes=FLOOR)
        found = violations_of("campaign.satisfied", payload)
        self.assertEqual([code for code, _ in found], ["oversized-payload"])
        self.assertIn("exceeds 64 KiB apart from its member rows", found[0][1])

    def test_rows_within_their_allowance_make_a_large_payload_valid(self):
        payload = payload_of(row_bytes=300 * 800, rows=300)  # about 800 bytes a member (and the ID list), 240 KB in all
        self.assertGreater(M._json_size(payload), FLOOR)
        self.assertEqual(violations_of("campaign.satisfied", payload), [])

    def test_another_kind_of_event_has_no_allowance(self):
        found = violations_of("campaign.reopened", payload_of(row_bytes=FLOOR, rows=1))
        self.assertEqual([code for code, _ in found], ["oversized-payload"])
        self.assertEqual(found[0][1], "serialized payload exceeds 64 KiB")


class FatRowCampaignTest(F.ProducerTestBase):
    """One cycle, its route closed with many terminal gates unproven: the payload is small, the row is not."""

    def setUp(self):
        super().setUp()
        self.activate()
        route = F.compile_for("direct", self.root, slug="fat-cycle", gate_source="fat-input")
        binding = F.L.admit_runtime_route(self.root, route)
        route_file = Path(binding.route_file)
        child = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                        intensity="direct", campaign_key="fat-campaign")
        self.write_output(child)
        P.finalize(self.root, cycle_id=child["cycle_id"], allow_open_route=True)
        F.R.close_route(route, route_file, commit="a" * 40, summary="fixture")
        outcome_path = LC.canonical_outcome_path(self.root, route["route_id"])
        outcome = json.loads(Path(outcome_path).read_text())
        outcome["terminal_gates"] = {"review-fanout-%02d" % n: {"passed": False, "reason": "registry-unreadable"}
                                     for n in range(14)}
        outcome["terminal_gate_proven"] = False
        Path(outcome_path).write_text(json.dumps(outcome, sort_keys=True))
        self.child = child
        self.path = Path(child["cycle_dir"]).parent / "campaign.json"
        self.event_path = self.path.parent / C.EVENTS_DIR / "000001.json"

    def test_the_fixture_is_a_small_payload_with_a_fat_row(self):
        snapshot = C._snapshot(self.root, self.path)
        self.assertEqual(len(snapshot["cycles"]), 1)
        self.assertGreater(M._json_size(snapshot["cycles"]), C.manifest._MEMBER_ROW_ALLOWANCE_BYTES)
        self.assertLess(M._json_size({"snapshot": snapshot}), FLOOR)

    def test_it_is_closable_closes_and_reads_back_satisfied(self):
        self.assertTrue(C.status(self.root, self.path)["closable"])
        self.assertEqual(C.close(self.root, self.path, reason="done")["status"], "satisfied")
        event = json.loads(self.event_path.read_text())
        self.assertLess(M._json_size(event["payload"]), FLOOR)
        self.assertEqual(C.campaign_state(self.root, self.path).state, "satisfied")
        self.assertEqual(C.status(self.root, self.path)["state"], "satisfied")
        self.assertEqual([row["state"] for row in P.list_campaign_summaries(self.root, active_only=False)],
                         ["satisfied"])

    def test_an_event_written_under_the_old_rule_is_read_by_the_new_one(self):
        # the old rule: the whole payload within 64 KiB and nothing else; the event it let through is the one
        # the new rule writes, so the bytes of a campaign closed before are what is read now
        with mock.patch.object(M, "_MEMBER_ROW_ALLOWANCE_BYTES", 10 ** 9):
            self.assertEqual(C.close(self.root, self.path, reason="done")["status"], "satisfied")
        written = self.event_path.read_bytes()
        self.assertEqual(C.campaign_state(self.root, self.path).state, "satisfied")
        self.assertEqual(C.status(self.root, self.path)["state"], "satisfied")
        self.assertEqual(self.event_path.read_bytes(), written)
        self.assertEqual([row["state"] for row in P.list_campaign_summaries(self.root, active_only=False)],
                         ["satisfied"])

    def test_status_and_close_say_the_same_at_the_read_limit_of_a_record(self):
        # the limit falls between the snapshot and the whole event: a status that weighs the snapshot alone
        # would say closable here and the close would refuse
        state = C.campaign_state(self.root, self.path)
        snapshot = C._snapshot(self.root, self.path)
        first = next(row for row in snapshot["cycles"] if row.get("manifest_id"))
        size = len(C.canonical(C._new_closing_event(self.root, state, snapshot, first))) + 1
        self.assertGreater(size, len(C.canonical(snapshot)) + 1 + 5)
        for limit, closes in ((size - 5, False), (size + 5, True)):
            with self.subTest(limit=limit), mock.patch.object(C, "MAX_JSON", limit):
                answer = C.status(self.root, self.path)
                self.assertEqual(answer["closable"], closes, answer)
                if closes:
                    self.assertEqual(C.close(self.root, self.path)["status"], "satisfied")
                else:
                    self.assertEqual(answer["close_refusal"]["reason"], "campaign-event-too-large")
                    with self.assertRaises(C.CampaignError) as caught:
                        C.close(self.root, self.path)
                    self.assertEqual(caught.exception.code, "campaign-event-too-large")
                    self.assertFalse(self.event_path.exists())
                    self.assertEqual(C.campaign_state(self.root, self.path).state, "active")


if __name__ == "__main__":
    unittest.main()
