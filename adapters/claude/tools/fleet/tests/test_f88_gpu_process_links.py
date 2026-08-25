import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet import render
from tools.fleet.model import DispatchJob, Session, SubAgent


ROOT = Path(__file__).resolve().parents[3]


def _compute_hosts_module():
    path = ROOT / "utilities" / "compute-hosts.py"
    spec = importlib.util.spec_from_file_location("f88_compute_hosts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _probe_namespace():
    script = _compute_hosts_module().PROBE_SCRIPT
    source = script.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    helpers = source.split("\npayload = {", 1)[0]
    namespace = {}
    exec(compile(helpers, "<f88-probe-helpers>", "exec"), namespace)
    return namespace


class ProbeCommandAndSessionEvidenceTest(unittest.TestCase):
    def test_command_is_control_argv_and_display_bounded(self):
        ns = _probe_namespace()
        text = ns["command_text"]([
            b"python", b"train\nmodel.py", b"\x01--epochs", b"10",
        ])
        self.assertEqual(text, "python train model.py --epochs 10")
        self.assertNotRegex(text, r"[\x00-\x1f\x7f]")

        long = ns["command_text"]([("가" * 200).encode()])
        self.assertLessEqual(sum(2 if ord(char) > 127 else 1 for char in long), 160)
        argv = ns["command_text"]([str(index).encode() for index in range(80)])
        self.assertNotIn(" 32 ", " " + argv + " ")

    def test_command_read_is_same_euid_pid_start_safe_and_fail_soft(self):
        ns = _probe_namespace()
        stable = {"ppid": 1, "start": 44}
        ns["proc_stat"] = mock.Mock(return_value=stable)
        ns["same_euid"] = mock.Mock(return_value=True)
        with mock.patch.object(ns["Path"], "open", return_value=io.BytesIO(
                b"python\0train.py\0--epochs\0" + b"10\0")):
            self.assertEqual(ns["process_command"](7, 44, "/usr/bin/python"),
                             "python train.py --epochs 10")

        with mock.patch.object(ns["Path"], "open", return_value=io.BytesIO(b"")):
            self.assertEqual(ns["process_command"](7, 44, "/usr/bin/python"),
                             "/usr/bin/python")
        with mock.patch.object(ns["Path"], "open", side_effect=PermissionError):
            self.assertEqual(ns["process_command"](7, 44, "python"), "python")

        ns["proc_stat"] = mock.Mock(side_effect=[stable, {"ppid": 1, "start": 45}])
        with mock.patch.object(ns["Path"], "open", return_value=io.BytesIO(b"wrong\0")):
            self.assertEqual(ns["process_command"](7, 44, "python"), "python")

        ns["proc_stat"] = mock.Mock(return_value=stable)
        ns["same_euid"] = mock.Mock(return_value=False)
        with mock.patch.object(ns["Path"], "open") as opened:
            self.assertEqual(ns["process_command"](7, 44, "python"), "python")
        opened.assert_not_called()

    def test_primary_owner_and_unique_session_evidence_are_independent(self):
        ns = _probe_namespace()
        ns["proc_stat"] = mock.Mock(return_value={"ppid": 0, "start": 99})
        ns["same_euid"] = mock.Mock(return_value=True)
        ns["harness_process"] = mock.Mock(return_value=None)
        ns["OWNER_CLAIMS"] = []
        ns["identity_env"] = mock.Mock(return_value={
            "AGENT_DISPATCH_ATTEMPT_ID": "att-one",
            "AGENT_DISPATCH_SELF_SLUG": "train",
            "CODEX_THREAD_ID": "sid-exact",
            "CODEX_SESSION_ID": "sid-exact",
        })
        owner, reason, session_owner = ns["process_owner"](7, 99)
        self.assertEqual(owner["kind"], "job")
        self.assertIsNone(reason)
        self.assertEqual((session_owner["harness"], session_owner["id"]),
                         ("codex", "sid-exact"))

        ns["identity_env"] = mock.Mock(return_value={
            "AGENT_DISPATCH_ATTEMPT_ID": "att-one",
            "CODEX_THREAD_ID": "sid-one",
            "CLAUDE_CODE_SESSION_ID": "sid-two",
        })
        owner, reason, session_owner = ns["process_owner"](7, 99)
        self.assertEqual(owner["kind"], "job")
        self.assertIsNone(reason)
        self.assertIsNone(session_owner)

    def test_persistent_claim_supplies_exact_session_evidence(self):
        ns = _probe_namespace()
        ns["proc_stat"] = mock.Mock(return_value={"ppid": 0, "start": 99})
        ns["same_euid"] = mock.Mock(return_value=True)
        ns["identity_env"] = mock.Mock(return_value={})
        ns["harness_process"] = mock.Mock(return_value=None)
        ns["proc_cmdline_sha256"] = mock.Mock(return_value="hash")
        ns["OWNER_CLAIMS"] = [{
            "root_pid": 7, "root_start": 99, "root_cmdline_sha256": "hash",
            "owner": {"kind": "session", "harness": "claude", "id": "sid-claim"},
        }]
        owner, reason, session_owner = ns["process_owner"](7, 99)
        self.assertEqual(owner["id"], "sid-claim")
        self.assertIsNone(reason)
        self.assertEqual(session_owner["id"], "sid-claim")


class GpuProcessAndResourceRenderTest(unittest.TestCase):
    def setUp(self):
        self.original_blink = render._BLINK_ON
        self.addCleanup(setattr, render, "_BLINK_ON", self.original_blink)
        self.session = Session(
            harness="codex", pid=101, proc_start="11", cwd="/tmp/f88-project",
            session_id="sid-exact", title="training", liveness="working",
            subagents=[SubAgent(agent_type="explorer", active=True)],
        )
        self.snapshot = {
            "configured": True,
            "hosts": [{
                "host": "cnn", "reachable": True, "cpu_utilization_pct": 10,
                "cpu_count": 16, "gpus": [{
                    "index": 0, "name": "NVIDIA A100", "utilization_gpu_pct": 80,
                    "memory_used_mib": 12288, "memory_total_mib": 40960,
                    "processes": [
                        {"pid": 300, "used_memory_mib": 8192,
                         "command": "python train.py --epochs 10",
                         "owner": {"kind": "job", "id": "att-one", "label": "job:train"},
                         "session_owner": {"kind": "session", "harness": "codex",
                                           "id": "sid-exact"}},
                        {"pid": 301, "used_memory_mib": 4096,
                         "command": "python worker.py",
                         "owner": {"kind": "run", "id": "run-one", "label": "run:one"},
                         "session_owner": {"kind": "session", "harness": "codex",
                                           "id": "sid-exact"}},
                    ],
                }, {
                    "index": 1, "name": "NVIDIA L40S", "utilization_gpu_pct": 20,
                    "memory_used_mib": 1024, "memory_total_mib": 49152,
                    "processes": [{
                        "pid": 302, "used_memory_mib": 1024, "command": "python eval.py",
                        "owner": None,
                        "session_owner": {"kind": "session", "harness": "codex",
                                          "id": "sid-exact"},
                    }],
                }],
            }],
        }
        render.set_compute_hosts(self.snapshot)
        self.addCleanup(render.set_compute_hosts, None)
        self.addCleanup(render.set_process_view, False)

    def test_upper_rows_are_owner_free_command_only_and_bounded(self):
        for width in (168, 100, 60):
            rows = render._compute_host_rows(width, [self.session])
            self.assertTrue(all(render._dw(render._plain(row)) <= width for row in rows))
            text = "\n".join(render._plain(row) for row in rows)
            self.assertIn("python train.py", text)
            for owner in ("job:train", "run:one", "CX/sid-exac"):
                self.assertNotIn(owner, text)
            process_text = "\n".join(render._plain(row) for row in
                                     render._gpu_process_rows(
                                         self.snapshot["hosts"][0]["gpus"][0], "", width))
            self.assertNotIn("PID ", process_text)
            self.assertNotIn("VRAM ", process_text)
            self.assertNotIn("MiB", process_text)

    def test_exact_relation_aggregates_multi_gpu_in_stable_order(self):
        resources = render._gpu_session_resources(self.snapshot)
        linked = render._gpu_resources_for_session(self.session, resources)
        self.assertEqual([(row["host"], row["index"]) for row in linked],
                         [("cnn", 0), ("cnn", 1)])
        self.assertEqual(linked[0]["process_count"], 2)
        self.assertEqual(linked[0]["used_memory_mib"], 12288)
        near = Session(harness="codex", pid=102, session_id="sid-exact-other")
        self.assertEqual(render._gpu_resources_for_session(near, resources), [])
        job = DispatchJob(key="autopilot-code", harness="codex")
        job._runtime_session_id = "sid-exact"
        self.assertEqual(render._gpu_resources_for_session(job, resources), linked)

    def test_resource_strip_matches_native_indent_and_degrades_without_overflow(self):
        linked = render._gpu_resources_for_session(
            self.session, render._gpu_session_resources(self.snapshot))
        for width in (168, 100, 60):
            row = render._gpu_resource_strip(linked, term_width=width)[0]
            text = render._plain(row)
            self.assertLessEqual(render._dw(text), width)
            self.assertTrue(text.startswith(render._SUBAGENT_IND + "● GPU cnn:0"))
            self.assertIn("GPU cnn:1", text)
            self.assertNotIn("⚡", text)
            self.assertNotIn("▣", text)
            self.assertNotIn(" proc", text)
            self.assertNotIn("MiB", text)
        wide = render._gpu_resource_strip(linked, term_width=168)[0]
        self.assertIn("12 GB", render._plain(wide))
        self.assertIn(("A100", "gpu_ampere"), wide)
        self.assertIn(("L40S", "gpu_ada"), wide)

    def test_resource_pulse_reuses_live_tick_and_keeps_metadata_dim(self):
        linked = render._gpu_resources_for_session(
            self.session, render._gpu_session_resources(self.snapshot))
        render._BLINK_ON = True
        on = render._gpu_resource_strip(linked, term_width=168)[0]
        render._BLINK_ON = False
        off = render._gpu_resource_strip(linked, term_width=168)[0]
        self.assertEqual(on[1], ("●", "g_work"))
        self.assertEqual(off[1], ("●", "g_work_off"))
        self.assertIn(("GPU cnn:0", "name_dim"), on)
        self.assertIn((" · 12 GB", "dim"), on)

    def test_gpu_model_families_are_stable_dim_color_keys(self):
        expected = {
            "NVIDIA B200": "gpu_blackwell",
            "NVIDIA H100": "gpu_hopper",
            "NVIDIA RTX 6000 Ada Generation": "gpu_rtx6000",
            "NVIDIA RTX A6000": "gpu_rtx6000",
            "NVIDIA RTX 4090": "gpu_rtx4090",
            "NVIDIA RTX 5090": "gpu_rtx5090",
            "NVIDIA A100": "gpu_ampere",
            "NVIDIA T4": "gpu_turing",
            "Mystery Accelerator": "gpu_other",
        }
        for model, key in expected.items():
            with self.subTest(model=model):
                self.assertEqual(render._gpu_model_key(model), key)
                self.assertEqual(render._HUE_OF[key][1], render._A_DIM)

    def test_gpu_family_colors_bind_to_initialized_palette_pairs(self):
        previous = dict(render._COLOR)

        def restore_colors():
            render._COLOR.clear()
            render._COLOR.update(previous)

        self.addCleanup(restore_colors)
        with mock.patch.object(render.curses, "start_color"), \
                mock.patch.object(render.curses, "use_default_colors"), \
                mock.patch.object(render.curses, "can_change_color", return_value=False), \
                mock.patch.object(render.curses, "init_pair"), \
                mock.patch.object(render.curses, "color_pair", side_effect=lambda pair: pair << 8), \
                mock.patch.object(render.curses, "COLORS", 256, create=True):
            render._init_colors()

        self.assertEqual(render._COLOR["gpu_ada"], render._COLOR["h_claude"])
        self.assertEqual(render._COLOR["gpu_hopper"], render._COLOR["h_codex"])
        self.assertEqual(render._COLOR["gpu_ampere"], render._COLOR["h_opencode"])
        self.assertEqual(render._COLOR["gpu_rtx6000"], render._COLOR["h_opencode"])
        self.assertEqual(render._COLOR["gpu_rtx4090"], render._COLOR["h_claude"])
        self.assertNotEqual(render._COLOR["gpu_rtx6000"], render._COLOR["gpu_rtx4090"])
        for family in ("gpu_rtx6000", "gpu_rtx4090", "gpu_rtx5090",
                       "gpu_blackwell", "gpu_hopper", "gpu_ada", "gpu_ampere", "gpu_turing"):
            with self.subTest(family=family):
                self.assertNotEqual(render._COLOR[family] & ~render.curses.A_DIM, 0)

    def test_group_and_process_views_share_link_and_order_after_subagent_strip(self):
        group = render._build_lines(
            [self.session], [], "both", False, 0, layout="wide", term_width=120)
        process = render._build_process_lines(
            [self.session], [], {}, 0, None, 120, "wide")
        for lines in (group, process):
            text = [render._plain(line) for line in lines if line]
            sub_at = next(index for index, line in enumerate(text) if "⚡explorer" in line)
            gpu_at = next(index for index, line in enumerate(text) if "● GPU cnn:0" in line)
            self.assertLess(sub_at, gpu_at)
            self.assertEqual(sum("● GPU cnn:0" in line for line in text), 1)

    def test_missing_visible_session_leaves_only_upper_process_view(self):
        lines = render._build_process_lines([], [], {}, 0, None, 100, "wide")
        text = "\n".join(render._plain(line) for line in lines if line)
        self.assertIn("python train.py", text)
        self.assertNotIn(render._SUBAGENT_IND + "● GPU", text)


if __name__ == "__main__":
    unittest.main()
