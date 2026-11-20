#!/usr/bin/env python3
"""A campaign with many cycles closes, and what the close records stays checked as strictly as before.

The closing event holds one row for each cycle of the campaign.  These tests build a campaign whose rows take
more than the 64 KiB an event's payload had, close it, and read it back through every reader.  """
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import io
import json
from pathlib import Path
import resource
import shutil
import time
import unittest
from unittest import mock

os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_campaign as C
import artifact_manifest as M
import artifact_producer as P

_spec = importlib.util.spec_from_file_location(
    "campaign_scale_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)

MEMBERS = 300  # the rows of this many cycles take more than 64 KiB


def _rss_mib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class BigCampaignTest(F.ProducerTestBase):
    def setUp(self):
        super().setUp()
        self.activate()
        route, route_file, first = self.begin(campaign_key="scale-campaign")
        self.write_output(first)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=first["cycle_id"])
        self.campaign = first["campaign_id"]
        self.path = Path(first["cycle_dir"]).parent / "campaign.json"
        for index in range(1, MEMBERS):
            slug = "member-%03d" % index
            member_route = F.compile_for("direct", self.root, slug=slug, gate_source=slug)
            binding = F.L.admit_runtime_route(self.root, member_route)
            child = P.begin(self.root, route_file=Path(binding.route_file), capability="autopilot-code",
                            intensity="direct", campaign_id=self.campaign)
            self.write_output(child, data=("body %d\n" % index).encode())
            self.close(member_route, Path(binding.route_file))
            P.finalize(self.root, cycle_id=child["cycle_id"])
        self.events = self.path.parent / C.EVENTS_DIR

    def _event_bytes(self, sequence=1):
        return (self.events / ("%06d.json" % sequence)).read_bytes()

    def _phase_close_and_read(self):
        status = C.status(self.root, self.path)
        self.assertTrue(status["closable"], status.get("close_refusal"))
        self.assertEqual(len(status["cycles"]), MEMBERS)

        before = time.monotonic()
        result = C.close(self.root, self.path, reason="a campaign with many cycles is done")
        elapsed = time.monotonic() - before
        self.assertEqual(result["status"], "satisfied")
        raw = self._event_bytes()
        event = json.loads(raw)
        rows = event["payload"]["snapshot"]["cycles"]
        self.assertEqual(len(rows), MEMBERS)
        # The point of the test: this event is past the old limit, and it is the one rule that lets it pass.
        self.assertGreater(len(C.canonical(event["payload"])), 64 * 1024)
        self.assertEqual(raw, C.canonical(event) + b"\n")
        print("members=%d event=%d bytes (%.0f per member) close=%.1fs rss=%.0f MiB" % (
            MEMBERS, len(raw), len(raw) / MEMBERS, elapsed, _rss_mib()), file=sys.stderr)

        self.assertEqual(C.status(self.root, self.path)["status"], "satisfied")
        rows_listed = P.list_campaign_summaries(self.root, active_only=False)
        listed = next(row for row in rows_listed if row["campaign_id"] == self.campaign)
        self.assertEqual(listed["state"], "satisfied")
        export = C.export_current(self.root)
        exported = next(row for row in export["campaigns"] if row["campaign_id"] == self.campaign)
        self.assertEqual(exported["state"], "satisfied")
        self.assertEqual(self._event_bytes(), raw)

        # reopen and close again: a second event of the same size, and `recover` of a committed close
        C.reopen(self.root, self.path, reason="more work")
        self.assertEqual(C.status(self.root, self.path)["state"], "active")
        self.assertEqual(C.close(self.root, self.path, reason="done again")["status"], "satisfied")
        self.assertEqual(C.recover(self.root, self.path)["status"], "satisfied")
        self.assertEqual(self._event_bytes(), raw)

    def _phase_tamper(self):
        original = self._event_bytes()
        event_path = self.events / "000001.json"
        # one byte of a row
        changed = original.replace(b'"state":"completed"', b'"state":"complete_"', 1)
        self.assertNotEqual(changed, original)
        event_path.write_bytes(changed)
        with self.assertRaises(C.CampaignError) as caught:
            C.status(self.root, self.path)
        self.assertEqual(caught.exception.code, "campaign-event-invalid")
        event_path.write_bytes(original)
        # a row with its digest changed, event id and canonical form kept consistent by the attacker, is still
        # refused: the snapshot's digest is in the provenance and the rows are bound to the manifests
        event = json.loads(original)
        event["payload"]["snapshot"]["cycles"][5]["manifest_digest"] = "sha256:" + "0" * 64
        event["event_id"] = C._id("evt", {k: v for k, v in event.items() if k != "event_id"})
        event_path.write_bytes(C.canonical(event) + b"\n")
        with self.assertRaises(C.CampaignError) as caught:
            C.status(self.root, self.path)
        self.assertEqual(caught.exception.code, "campaign-event-invalid")
        event_path.write_bytes(original)
        self.assertEqual(C.status(self.root, self.path)["status"], "satisfied")

    def _phase_allowance(self):
        event = json.loads(self._event_bytes())

        def violations(row):
            found = []
            M._v_event_row(row, "$", found)
            return [(v.code, v.detail) for v in found]

        self.assertEqual(violations(event), [])
        # the event's own text is still held to 64 KiB, whatever the member count
        fat = json.loads(json.dumps(event))
        fat["payload"]["snapshot"]["campaign"]["goal"] = "g" * (64 * 1024)
        found = violations(fat)
        self.assertEqual([code for code, _ in found], ["oversized-payload"])
        self.assertIn("event's own text", found[0][1])
        # a row may not take more than its allowance
        fat = json.loads(json.dumps(event))
        fat["payload"]["snapshot"]["cycles"][0]["terminal_gate_reasons"] = ["r" * 1024 * MEMBERS]
        found = violations(fat)
        self.assertEqual([code for code, _ in found], ["oversized-payload"])
        self.assertIn("member rows take", found[0][1])
        # another event type has no allowance
        other = json.loads(json.dumps(event))
        other["event_type"] = "campaign.reopened"
        self.assertEqual([code for code, _ in violations(other)], ["oversized-payload"])

    def _phase_read_limit(self):
        before = self.path.read_bytes()
        with mock.patch.object(C, "MAX_JSON", 64 * 1024):
            status = C.status(self.root, self.path)
            self.assertFalse(status["closable"])
            self.assertEqual(status["close_refusal"]["reason"], "campaign-event-too-large")
            with self.assertRaises(C.CampaignError) as caught:
                C.close(self.root, self.path, reason="too many cycles")
        self.assertEqual(caught.exception.code, "campaign-event-too-large")
        detail = caught.exception.detail
        self.assertIn("one row for each cycle", str(detail))
        self.assertIn("nothing was written", str(detail))
        self.assertIn("begin further work under another campaign", str(detail))
        self.assertFalse(self.events.exists() and any(self.events.iterdir()))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(C.status(self.root, self.path)["state"], "active")

    def _phase_renamed_root(self):
        raw = self._event_bytes()
        target = Path(self._tmp.name) / "renamed-root"
        shutil.move(str(self.root), str(target))
        new_path = target / self.path.relative_to(self.root)
        before = time.monotonic()
        folded = C.campaign_state(target, new_path)
        elapsed = time.monotonic() - before
        self.assertEqual((folded.state, folded.last_sequence), ("satisfied", 3))
        print("renamed-root read of %d members: %.1fs rss=%.0f MiB" % (MEMBERS, elapsed, _rss_mib()), file=sys.stderr)
        self.assertEqual((new_path.parent / C.EVENTS_DIR / "000001.json").read_bytes(), raw)
        self.assertEqual(C.export_current(target)["campaigns"][0]["state"], "satisfied")
        # a preserved manifest that changed by one byte (of the 300th row's) is refused at the renamed root
        row = json.loads(raw)["payload"]["snapshot"]["cycles"][MEMBERS - 1]
        preserved = (target / P.artifact_lifecycle.MANIFEST_SNAPSHOT_REL / row["cycle_id"] /
                     (row["manifest_revision_id"] + ".json"))
        sealed = preserved.read_bytes()
        preserved.write_bytes(sealed + b" ")
        with self.assertRaises(C.CampaignError) as caught:
            C.campaign_state(target, new_path)
        self.assertEqual(caught.exception.code, "campaign-event-invalid")
        self.assertIn("rename-evidence-tampered", str(caught.exception.detail))
        preserved.write_bytes(sealed)
        self.assertEqual(C.campaign_state(target, new_path).state, "satisfied")

    def test_a_campaign_of_many_cycles_closes_reads_and_stays_checked(self):
        # One fixture serves every step, in order: building it is what takes the time.
        self._phase_read_limit()
        self._phase_close_and_read()
        self._phase_tamper()
        self._phase_allowance()
        self._phase_renamed_root()


if __name__ == "__main__":
    unittest.main()
