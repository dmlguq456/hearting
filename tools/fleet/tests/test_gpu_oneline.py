"""Folded GPU rows summarize observations while preserving expanded detail."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import render
sys.path.append(str(Path(__file__).resolve().parents[3] / "utilities"))  # split_module_fixture
from split_module_fixture import patch_split  # noqa: E402


def owner(harness, tag):
    return {"kind": "session", "harness": harness, "id": tag}


class GPUOneLineTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, render, "_ROUTE_FOLD", render._ROUTE_FOLD)
        self.addCleanup(setattr, render, "_COMPUTE_HOSTS", render._COMPUTE_HOSTS)
        self.addCleanup(setattr, render, "_COMPUTE_HOSTS_SET_AT", render._COMPUTE_HOSTS_SET_AT)
        self.addCleanup(setattr, render, "_FOLDABLE", render._FOLDABLE)
        render._ROUTE_FOLD = {render._GPU_FOLD_ALL: True}
        self.tag = patch_split(render, "_resolve_session_tag", side_effect=lambda h, sid: sid)
        self.tag.start()
        self.addCleanup(self.tag.stop)
        owners = [owner("codex", "bb")] * 3 + [owner("claude", "8e")]
        self.gpu = {"index": 1, "name": "NVIDIA RTX 6000 Ada Generation",
                    "utilization_gpu_pct": 100, "memory_used_mib": 23552,
                    "memory_total_mib": 49152,
                    "processes": [{"pid": n + 10, "session_owner": o,
                                   "command": "python train.py --config detailed-name.yaml"}
                                  for n, o in enumerate(owners)],
                    "reservations": [{"owner": {**o, "label": "saved [%s]" % o["id"]},
                                      "started_at": 1728000000, "task": "detailed task"}
                                     for o in owners]}
        self.host = {"host": "mgpu-a1", "reachable": True, "cpu_count": 64,
                     "gpus": [self.gpu]}
        render.set_compute_hosts({"configured": True, "hosts": [self.host]})

    def rows(self, width):
        return [render._plain(row) for row in render._compute_host_rows(width)]

    def test_matching_runs_fold_to_one_line_and_sorted_session_counts(self):
        for width in (80, 200):
            with self.subTest(width=width):
                lines = self.rows(width)
                self.assertEqual(len(lines), 3)
                self.assertIn("▸ 4 · ", lines[2])
                for tag in ("[bb]", "[8e]"):
                    self.assertEqual(lines[2].count(tag), 1)
                self.assertNotIn("×", lines[2])
                self.assertNotIn("reserved", lines[2])
                self.assertNotIn("detailed", "\n".join(lines))
                self.assertLessEqual(render._dw(lines[2]), width - 1)

    def test_narrow_summary_omits_whole_sessions_without_shifting_metrics(self):
        before = self.rows(80)[2]
        for n, harness in enumerate(("codex", "claude", "opencode")):
            o = owner(harness, "a%d" % n)
            self.gpu["processes"].append({"session_owner": o})
            self.gpu["reservations"].append({"owner": o})
        line = self.rows(80)[2]
        self.assertIn("[bb]", line)
        self.assertIn("+", line)
        self.assertNotIn("×", line)
        for token in ("UTIL", "%", "VRAM", "GB"):
            self.assertEqual(before.index(token), line.index(token))
        self.assertLessEqual(render._dw(line), 79)

    def test_same_total_with_different_owners_is_a_mismatch(self):
        self.gpu["reservations"][-1]["owner"] = owner("opencode", "ff")
        for width in (80, 200):
            line = self.rows(width)[2]
            self.assertTrue(any(token in line for token in ("reserved+", "res+", "R+")))
            self.assertTrue(any(token in line for token in ("unreserved+", "unres+", "U+")))
            self.assertIn("+1", line)
            self.assertLessEqual(render._dw(line), width - 1)

    def test_reserved_only_gpu_is_foldable_and_process_only_is_labelled(self):
        self.gpu["processes"] = []
        line = self.rows(80)[2]
        self.assertIn("reserved+4", line)
        self.assertEqual(len(render._compute_host_rows.fold_rows), 1)
        self.gpu["processes"] = [{"owner": owner("opencode", "ff")}]
        self.gpu["reservations"] = []
        self.assertIn("unres+1", self.rows(80)[2])

    def test_two_mismatch_labels_survive_a_long_host_in_80_columns(self):
        self.host["host"] = "gpu-worker"
        self.gpu["reservations"][-1]["owner"] = owner("opencode", "ff")
        line = self.rows(80)[2]
        self.assertIn("R+1 U+1", line)
        self.assertIn("UTIL 100%", line)
        self.assertIn("23/48GB ▸ 4", line)
        self.assertLessEqual(render._dw(line), 79)

    def test_unknown_queries_do_not_claim_a_reservation_mismatch(self):
        reservations = self.gpu.pop("reservations")
        self.assertNotIn("unreserved+", self.rows(200)[2])
        self.gpu["reservations"] = reservations
        self.gpu["processes"] = []
        self.host["process_detail"] = "query failed"
        self.assertNotIn("reserved+", self.rows(200)[2])
        self.host.pop("process_detail")
        self.gpu["observation_source"] = "proc-fallback"
        self.assertNotIn("reserved+", self.rows(200)[2])
        self.gpu.pop("observation_source")
        self.gpu["processes"] = [{"session_owner": owner("opencode", "ff")}]
        self.gpu["reservations"] = []
        self.host["reservation_detail"] = "leases unreadable"
        self.assertNotIn("unreserved+", self.rows(200)[2])

    def test_expanded_joins_each_reservation_and_its_command_on_one_row(self):
        for n, (process, lease) in enumerate(zip(self.gpu["processes"], self.gpu["reservations"])):
            process["pgid"] = lease["pid"] = 900 + n
        original = copy.deepcopy(self.gpu)
        self.rows(200)  # Build this test's fold targets, not a previous sample's.
        render._FOLDABLE = list(render._compute_host_rows.fold_rows)
        render._toggle_gpu_commands()
        lines = self.rows(200)
        self.assertEqual(len(lines), 7)
        self.assertIn("▾ 4", lines[2])
        runs = lines[3:]
        for line in runs:
            self.assertIn("↳ ", line)
            for detail in ("10-04", "detailed-name.yaml"):
                self.assertIn(detail, line)
            self.assertNotIn("detailed task", line)
            self.assertLessEqual(render._dw(line), 199)
        self.assertEqual(sum("[bb]" in line for line in runs), 3)
        self.assertNotIn("reserved", "\n".join(runs))
        self.assertNotIn("saved", "\n".join(runs))
        self.assertEqual(original, self.gpu)

    def test_unjoinable_runs_keep_their_own_rows(self):
        # Without process-group evidence only [8e], its session's sole run, joins.
        render._ROUTE_FOLD = {render._GPU_FOLD_ALL: False}
        lines = [line.strip() for line in self.rows(200)[3:]]
        self.assertEqual(len(lines), 7)
        self.assertEqual(sum(line.startswith("↳ [8e] · ") and line.endswith(".yaml")
                             for line in lines), 1)
        self.assertEqual(sum(line.startswith("reserved [bb] · ") for line in lines), 3)
        self.assertEqual(lines.count("↳ python train.py --config detailed-name.yaml"), 3)

    def test_narrow_row_keeps_run_identity_and_counts_extra_processes(self):
        render._ROUTE_FOLD = {render._GPU_FOLD_ALL: False}
        for process in self.gpu["processes"]:
            process["pgid"] = 900
        self.gpu["reservations"] = [{**self.gpu["reservations"][0], "pid": 900}]
        for width in (80, 200):
            lines = self.rows(width)[3:]
            self.assertEqual(len(lines), 1)
            self.assertIn("↳ ", lines[0])
            self.assertIn("[bb] / ", lines[0])
            self.assertIn("[8e] · 10-04", lines[0])
            self.assertTrue(lines[0].endswith(" +3"))
            self.assertLessEqual(render._dw(lines[0]), width - 1)
        self.assertIn("detailed-name.yaml", self.rows(200)[3])


if __name__ == "__main__":
    unittest.main()
