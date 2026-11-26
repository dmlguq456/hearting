#!/usr/bin/env python3
"""Candidate assertions for the hermetic sibling delivery measurement."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DRIVER = Path(__file__).with_name("dispatch_delivery_batch_fixture.py")


class DeliveryBatchMeasurementTest(unittest.TestCase):
    def test_measurement_stops_only_its_own_writer_before_temporary_cleanup(self):
        import dispatch_delivery_batch_fixture as fixture
        import peer_obligations

        prior = mock.Mock()
        runners = [prior]
        writer = None
        with mock.patch.object(peer_obligations, "_RUNNERS", runners):
            with tempfile.TemporaryDirectory(prefix="delivery-runner-cleanup-") as temporary, \
                    fixture._fixture_obligation_runners():
                directory = Path(temporary)
                script = (
                    "from pathlib import Path; import sys,time\n"
                    "root=Path(sys.argv[1])/'peer-steward'/'obligations'\n"
                    "root.mkdir(parents=True)\n"
                    "print('ready', flush=True)\n"
                    "while True:\n"
                    " (root/'record.json').write_text('{}')\n"
                    " time.sleep(.01)\n"
                )
                writer = subprocess.Popen(
                    [sys.executable, "-c", script, str(directory)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                runners.append(writer)
                self.assertEqual(writer.stdout.readline().strip(), "ready")
                self.assertIsNone(writer.poll())
            self.assertIsNotNone(writer.poll())
            self.assertFalse(directory.exists())
        prior.poll.assert_not_called()
        prior.terminate.assert_not_called()
        writer.stdout.close()
        writer.stderr.close()

    def test_candidate_carriers_coalesce_success_and_deliver_failures(self):
        with tempfile.TemporaryDirectory(prefix="delivery-batch-test-") as temporary:
            output = Path(temporary) / "candidate.json"
            completed = subprocess.run(
                [sys.executable, str(DRIVER), "--source-root", str(ROOT), "--output", str(output)],
                cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(output.read_text(encoding="utf-8"))

        carriers = {"shared_sweep", "claude_async_rewake", "codex_native_queue"}
        self.assertEqual({row["carrier"] for row in result["measurements"]}, carriers)
        measured = {(row["carrier"], row["variant"]): row["observed"]
                    for row in result["measurements"]}
        self.assertEqual(set(measured), {(carrier, variant) for carrier in carriers
                                         for variant in ("all_success", "two_failures")})
        for carrier in sorted(carriers):
            all_success = measured[(carrier, "all_success")]
            mixed = measured[(carrier, "two_failures")]
            self.assertEqual(all_success["wakes"], 1, carrier)
            self.assertEqual(mixed["wakes"], 3, carrier)
            for variant, observation in (("all_success", all_success), ("two_failures", mixed)):
                self.assertEqual(set(observation["ack_transition_count_by_record"].values()), {1},
                                 (carrier, variant))
                self.assertEqual(observation["unsettled_record_ids"], [], (carrier, variant))
            self.assertEqual(sorted(mixed["failure_delivery_completion"].values()), [8, 19], carrier)

        for carrier in carriers:
            for variant in ("all_success", "two_failures"):
                row = next(item for item in result["measurements"]
                           if item["carrier"] == carrier and item["variant"] == variant)
                if carrier == "shared_sweep":
                    self.assertEqual(row["seams"], [])
                else:
                    self.assertTrue(row["seams"], carrier)
                    self.assertFalse(any("patch dispatch_notice_state.resolve_delivery_batch" in seam
                                         for seam in row["seams"]))
                observation = row["observed"]
                self.assertTrue(row["ack_source"])
                self.assertEqual(
                    {source for item in observation["timeline"]
                     for source in item.get("ack_sources", {}).values()},
                    {"fixture-ack"} if carrier == "shared_sweep" else
                    ({"claude-task-notification-transcript"} if carrier == "claude_async_rewake"
                     else {"codex-consumed-evidence"}),
                    (carrier, variant))

        print("carrier | variant | wakes | emissions | injected chars | fixed chars | ACKs | unsettled | ACK source")
        for carrier in sorted(carriers):
            for variant in ("all_success", "two_failures"):
                observation = measured[(carrier, variant)]
                print(f"{carrier} | {variant} | {observation['wakes']} | "
                      f"{observation['emissions']} | {observation['injected_characters']} | "
                      f"{observation['fixed_instruction_characters']} | "
                      f"{sum(observation['ack_transition_count_by_record'].values())} | "
                      f"{len(observation['unsettled_record_ids'])} | "
                      f"{next(row['ack_source'] for row in result['measurements'] if row['carrier'] == carrier and row['variant'] == variant)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
