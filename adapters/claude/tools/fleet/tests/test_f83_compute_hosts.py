import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet import fleet, render  # noqa: E402
from fleet.collectors import compute_hosts  # noqa: E402


class ComputeHostCollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "compute-hosts.yaml"
        self.config.write_text("schema_version: 1\n", encoding="utf-8")
        self.tool = self.root / "compute-hosts.py"

    def _env(self):
        return mock.patch.dict(os.environ, {
            "COMPUTE_HOSTS_CONFIG": str(self.config),
            "FLEET_COMPUTE_HOSTS_TOOL": str(self.tool),
        }, clear=False)

    def test_unconfigured_inventory_is_absent_not_an_error_panel(self):
        with mock.patch.dict(os.environ, {
                "COMPUTE_HOSTS_CONFIG": str(self.root / "missing.yaml")}, clear=False):
            self.assertIsNone(compute_hosts.collect())

    def test_json_bridge_preserves_full_gpu_process_payload(self):
        payload = {
            "run_root": "/runs",
            "hosts": [{
                "host": "cnn", "reachable": True, "observed_at": 12.0,
                "gpus": [{
                    "index": 0, "uuid": "GPU-A", "utilization_gpu_pct": 71,
                    "memory_used_mib": 8192, "memory_total_mib": 24576,
                    "processes": [{"pid": 7, "proc_start": 99,
                                   "owner": {"kind": "run", "label": "run:x"}}],
                }],
            }],
        }
        self.tool.write_text(
            "import json\nprint(json.dumps(" + repr(payload) + "))\n", encoding="utf-8")
        with self._env():
            result = compute_hosts.collect()
        self.assertTrue(result["configured"])
        self.assertEqual(result["hosts"][0]["gpus"][0]["processes"][0]["pid"], 7)
        self.assertEqual(result["observed_at"], 12.0)

    def test_malformed_and_timeout_are_typed_diagnostics(self):
        self.tool.write_text(
            "import sys\nprint('bad inventory', file=sys.stderr)\nraise SystemExit(2)\n",
            encoding="utf-8",
        )
        with self._env():
            malformed = compute_hosts.collect()
        self.assertEqual(malformed["hosts"], [])
        self.assertIn("bad inventory", malformed["error"])

        self.tool.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
        with self._env():
            timed_out = compute_hosts.collect(timeout=0.05)
        self.assertIn("timed out", timed_out["error"])


class ComputeHostRenderTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, render, "_BLINK_ON", render._BLINK_ON)
        render.set_process_view(False)
        render.set_hearting({"version": "v2.0.0", "install_method": "linked"})
        self.snapshot = {
            "configured": True,
            "hosts": [
                {"host": "moving4", "self": True, "reachable": True,
                 "cpu_utilization_pct": 37, "cpu_count": 32, "load": "3.2 3.0 2.8",
                 "gpus": [
                    {"index": 0, "name": "NVIDIA RTX 6000 Ada Generation",
                     "utilization_gpu_pct": 84,
                     "memory_used_mib": 32768, "memory_total_mib": 49152,
                     "processes": [
                         {"pid": 1, "used_memory_mib": 16000,
                          "owner": {"kind": "job", "label": "job:train"}},
                         {"pid": 2, "used_memory_mib": 8000,
                          "owner": {"kind": "run", "label": "run:eval"}},
                         {"pid": 3, "used_memory_mib": 4000, "owner": None},
                     ]},
                    {"index": 1, "name": "NVIDIA RTX 6000 Ada", "utilization_gpu_pct": None,
                     "memory_used_mib": 1024, "memory_total_mib": 49152,
                     "processes": [{"pid": 4, "used_memory_mib": 1024, "owner": None}]},
                ]},
                {"host": "cnn", "self": False, "reachable": True,
                 "cpu_utilization_pct": 8, "cpu_count": 24, "load": "0.4 0.3 0.2",
                 "gpus": [
                    {"index": 0, "name": "NVIDIA A100", "utilization_gpu_pct": 7,
                     "memory_used_mib": 512, "memory_total_mib": 40960,
                     "processes": []},
                ]},
                {"host": "xavier", "self": False, "reachable": False,
                 "detail": "timed out", "gpus": []},
                {"host": "cpu", "self": False, "reachable": True,
                 "cpu_utilization_pct": None, "cpu_count": 64, "gpus": []},
            ],
        }
        render.set_compute_hosts(self.snapshot)

    def tearDown(self):
        render.set_compute_hosts(None)
        render.set_hearting(None)
        render.set_process_view(False)

    def test_gpu_rows_keep_identity_and_never_overflow_supported_widths(self):
        for width in (168, 120, 100, 60):
            with self.subTest(width=width):
                rows = render._compute_host_rows(width)
                self.assertTrue(all(render._dw(render._plain(row)) <= width for row in rows))
                text = "\n".join(render._plain(row) for row in rows)
                for value in ("Compute Resources 3/4", "◆ HOME moving4", "CPU", "0:", "1:",
                              "UTIL", "VRAM", "32/48G", "1/48G", "xavier", "down",
                              "cpu", "no gpu"):
                    self.assertIn(value, text)
                self.assertNotIn("g0", text)
                self.assertNotIn("g1", text)
        wide = "\n".join(render._plain(row) for row in render._compute_host_rows(168))
        for value in ("RTX 6000 Ada Generation", "job:train", "run:eval", "+1",
                      "unattributed:process#4", "LOAD 3.2/32c"):
            self.assertIn(value, wide)
        home_row = next(row for row in render._compute_host_rows(168)
                        if "◆ HOME" in render._plain(row))
        self.assertIn(("◆ HOME", "home_chip"), home_row)

    def test_exact_session_owner_is_an_explicit_gpu_link(self):
        gpu = {
            "index": 1, "name": "NVIDIA RTX 6000 Ada Generation",
            "utilization_gpu_pct": 25, "memory_used_mib": 8192,
            "memory_total_mib": 49152,
            "processes": [{
                "pid": 7,
                "owner": {"kind": "session", "harness": "claude",
                          "id": "f11a0486-c090-4fea-86be-c8097817761e",
                          "label": "claude:f11a0486"},
            }],
        }
        text = render._plain(render._gpu_token(gpu, 141, show_name=True))
        self.assertIn("1:", text)
        self.assertIn("RTX 6000 Ada Generation", text)
        self.assertIn("↳ session claude/f11a0486", text)
        self.assertNotIn("g1", text)

    def test_sub_tenth_gib_has_no_inequality_marker(self):
        self.assertEqual(render._gpu_gib(1), "0.0")
        self.assertNotIn("<", render._gpu_gib(1))

    def test_resource_panel_is_below_the_top_summary_divider(self):
        lines = render._build_lines([], [], "both", False, 0, term_width=120)
        text = [render._plain(line) for line in lines]
        hearting_at = next(i for i, line in enumerate(text) if "hearting v2.0.0" in line)
        resource_at = next(i for i, line in enumerate(text) if "Compute Resources" in line)
        pulse_at = next(i for i, line in enumerate(text) if line.startswith("  fleet "))
        dividers = [i for i, line in enumerate(text) if line == "─────"]
        self.assertGreaterEqual(len(dividers), 2)
        self.assertLess(hearting_at, pulse_at)
        self.assertLess(pulse_at, dividers[0])
        self.assertLess(dividers[0], resource_at)
        self.assertLess(resource_at, dividers[1])

    def test_process_view_also_separates_resources_from_cards(self):
        lines = render._build_process_lines([], [], {}, 0, None, 120, "wide")
        text = [render._plain(line) for line in lines]
        resource_at = next(i for i, line in enumerate(text) if "Compute Resources" in line)
        process_at = next(i for i, line in enumerate(text) if "PROCESS VIEW" in line)
        divider_at = next(i for i, line in enumerate(text)
                          if i > resource_at and line == "─────")
        self.assertLess(resource_at, divider_at)
        self.assertLess(divider_at, process_at)

    def test_public_json_is_additive_and_keeps_unfolded_processes(self):
        with mock.patch.object(fleet, "_collect_memory", return_value=None), \
             mock.patch.object(fleet, "_collect_governor", return_value=None):
            payload = json.loads(fleet._snapshot_json(
                [], [], compute_host_snapshot=self.snapshot))
        self.assertIn("compute_hosts", payload)
        processes = payload["compute_hosts"]["hosts"][0]["gpus"][0]["processes"]
        self.assertEqual(len(processes), 3)
        self.assertIsNone(processes[2]["owner"])

    def test_live_input_is_not_blocked_by_first_gpu_probe(self):
        gpu_entered = threading.Event()
        release = threading.Event()
        getch_called = threading.Event()

        def collector(harness_filter=None):
            return [], []

        def gpu_refresh():
            gpu_entered.set()
            release.wait()
            return self.snapshot

        collector.last_resource_jobs = []
        collector.last_usage_snapshots = {}
        collector.compute_hosts_refresh = gpu_refresh

        class Screen:
            def timeout(self, _value):
                pass

            def getch(self):
                getch_called.set()
                return ord("q")

        result = []
        with mock.patch.object(render, "_init_colors"), \
             mock.patch.object(render, "_draw"), \
             mock.patch.object(render.curses, "curs_set"), \
             mock.patch.dict(render.os.environ, {"HERDR_ENV": "1"}):
            thread = threading.Thread(
                target=lambda: result.append(render._loop(Screen(), collector, None, "both", 2.0)))
            thread.start()
            self.assertTrue(gpu_entered.wait(1.0))
            self.assertTrue(getch_called.wait(0.2))
            release.set()
            thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])


if __name__ == "__main__":
    unittest.main()
