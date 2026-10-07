import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
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

    def test_live_ssh_connection_supplies_only_exact_unique_session_evidence(self):
        ns = _probe_namespace()
        ns["proc_stat"] = mock.Mock(return_value={"ppid": 0, "start": 99})
        ns["same_euid"] = mock.Mock(return_value=True)
        ns["harness_process"] = mock.Mock(return_value=None)
        ns["OWNER_CLAIMS"] = []
        base = {
            "client_address": "192.0.2.10", "client_port": 41000,
            "server_address": "198.51.100.20", "server_port": 22,
        }
        exact = [{
            **base,
            "owner": {"kind": "session", "harness": "codex", "id": "sid-exact"},
        }]
        ns["SSH_SESSION_BRIDGES"] = ns["load_ssh_session_bridges"](exact)
        ns["identity_env"] = mock.Mock(return_value={
            "SSH_CONNECTION": "192.0.2.10 41000 198.51.100.20 22",
        })
        owner, reason, session_owner = ns["process_owner"](7, 99)
        self.assertEqual(owner["id"], "sid-exact")
        self.assertEqual(owner["source"], "ssh-connection+ancestry")
        self.assertIsNone(reason)
        self.assertEqual(session_owner["id"], "sid-exact")

        ns["identity_env"] = mock.Mock(return_value={
            "SSH_CONNECTION": "192.0.2.10 41001 198.51.100.20 22",
        })
        owner, reason, session_owner = ns["process_owner"](7, 99)
        self.assertIsNone(owner)
        self.assertEqual(reason, "no-exact-owner")
        self.assertIsNone(session_owner)

        ambiguous = exact + [{
            **base,
            "owner": {"kind": "session", "harness": "claude", "id": "sid-other"},
        }]
        ns["SSH_SESSION_BRIDGES"] = ns["load_ssh_session_bridges"](ambiguous)
        ns["identity_env"] = mock.Mock(return_value={
            "SSH_CONNECTION": "192.0.2.10 41000 198.51.100.20 22",
        })
        owner, reason, session_owner = ns["process_owner"](7, 99)
        self.assertIsNone(owner)
        self.assertEqual(reason, "ambiguous-session")
        self.assertIsNone(session_owner)

        self.assertIsNone(ns["normalized_ssh_connection"]("malformed"))
        self.assertIsNone(ns["normalized_ssh_connection"](
            "192.0.2.10 0 198.51.100.20 22"))
        self.assertIsNone(ns["normalized_ssh_connection"](
            ("192.0.2.10", True, "198.51.100.20", 22)))
        self.assertEqual(ns["normalized_ssh_connection"](
            "2001:0db8::1 41000 ::ffff:198.51.100.20 22"),
            ("2001:db8::1", 41000, "198.51.100.20", 22))


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
                        {"pid": 300, "proc_start": 42, "used_memory_mib": 8192,
                         "command": "/home/test/envs/xxx/bin/python train.py  --epochs 10 --output /keep/full-path",
                         "owner": {"kind": "job", "id": "att-one", "label": "job:train"},
                         "session_owner": {"kind": "session", "harness": "codex",
                                           "id": "sid-exact"}},
                        {"pid": 301, "proc_start": 43, "used_memory_mib": 4096,
                         "command": "python worker.py",
                         "owner": {"kind": "run", "id": "run-one", "label": "run:one"},
                         "session_owner": {"kind": "session", "harness": "codex",
                                           "id": "sid-exact"}},
                    ],
                }, {
                    "index": 1, "name": "NVIDIA L40S", "utilization_gpu_pct": 20,
                    "memory_used_mib": 1024, "memory_total_mib": 49152,
                    "processes": [{
                        "pid": 302, "proc_start": 44,
                        "used_memory_mib": 1024, "command": "python eval.py",
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
        original = json.dumps(self.snapshot, sort_keys=True)
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
            self.assertNotIn("/home/test/envs/xxx/bin/python", process_text)
            if width == 168:
                self.assertIn("↳ python train.py --epochs 10 --output full-path", process_text)
            self.assertEqual(json.dumps(self.snapshot, sort_keys=True), original)

    def test_exact_relation_aggregates_multi_gpu_in_stable_order(self):
        resources = render._gpu_session_resources(self.snapshot)
        linked = render._gpu_resources_for_session(self.session, resources)
        self.assertEqual([(row["host"], row["index"]) for row in linked],
                         [("cnn", 0), ("cnn", 1)])
        self.assertEqual(linked[0]["process_count"], 2)
        self.assertEqual(linked[0]["used_memory_mib"], 12288)
        self.assertTrue(all(not row["processes"] for row in linked))
        near = Session(harness="codex", pid=102, session_id="sid-exact-other")
        self.assertEqual(render._gpu_resources_for_session(near, resources), [])
        job = DispatchJob(key="autopilot-code", harness="codex")
        job._runtime_session_id = "sid-exact"
        self.assertEqual(render._gpu_resources_for_session(job, resources), linked)

    def test_claimed_training_processes_label_gpu_items_under_exact_session(self):
        codex = Session(harness="codex", pid=101, proc_start="11",
                        cwd="/tmp/f88-project", session_id="sid-exact",
                        title="Codex [92]", liveness="working")
        old = Session(harness="claude", pid=102, proc_start="12",
                      cwd="/tmp/f88-project", session_id="old-claude",
                      title="old Claude", liveness="working")
        claimed = {"kind": "session", "harness": "codex", "id": "sid-exact",
                   "source": "persistent-claim+ancestry"}
        snapshot = {"configured": True, "hosts": [{"host": "moving4", "gpus": [
            {"index": 0, "name": "NVIDIA A100", "processes": [
                {"pid": 3697573, "proc_start": 90,
                 "command": "python run.py --engine_mode train --config _m6_lx3nx4.yaml",
                 "owner": claimed, "session_owner": claimed, "used_memory_mib": 12000},
            ]},
            {"index": 1, "name": "NVIDIA A100", "processes": [
                {"pid": 1416464, "proc_start": 91,
                 "command": "python run.py --engine_mode train_ft --config _ft09__m3_lx3nx4.yaml",
                 "owner": claimed, "session_owner": claimed, "used_memory_mib": 10000},
                {"pid": 1859127, "proc_start": 92, "command": "python other.py",
                 "owner": {**claimed, "source": "ssh-connection+ancestry"},
                 "session_owner": claimed},
            ]},
        ]}]}
        render.set_compute_hosts(snapshot)
        resources = render._gpu_session_resources()
        linked = render._gpu_resources_for_session(codex, resources)
        self.assertEqual(render._gpu_resources_for_session(old, resources), [])
        for width in (168, 100, 60):
            rows = render._gpu_resource_strip(linked, term_width=width)
            text = [render._plain(row) for row in rows]
            self.assertTrue(all(render._dw(line) <= width for line in text))
            joined = "\n".join(text)
            self.assertEqual(joined.count("GPU moving4:0 (M6 학습)"), 1)
            self.assertEqual(joined.count("GPU moving4:1 (M3_9 학습)"), 1)
            self.assertNotIn("other.py", "\n".join(text))
        narrow = [render._plain(row) for row in render._gpu_resource_strip(linked, 60)]
        self.assertEqual(len(narrow), 2)
        self.assertIn("12 GB", narrow[0])
        self.assertIn("9.8 GB", narrow[1])
        for lines in (
            render._build_lines([codex, old], [], "both", False, 0,
                                layout="wide", term_width=120),
            render._build_process_lines([codex, old], [], {}, 0, None, 120, "wide"),
        ):
            text = [render._plain(line) for line in lines if line]
            gpu_rows = [line for line in text if "GPU moving4:" in line]
            self.assertEqual("\n".join(gpu_rows).count("GPU moving4:0 (M6 학습)"), 1)
            self.assertEqual("\n".join(gpu_rows).count("GPU moving4:1 (M3_9 학습)"), 1)
            self.assertFalse(any(" RUN " in line for line in text))
            self.assertLess(next(i for i, line in enumerate(text) if "Codex [92]" in line),
                            next(i for i, line in enumerate(text) if "GPU moving4:" in line))
        render._COMPUTE_HOSTS_SET_AT -= 3 * render._COMPUTE_HOST_INTERVAL + 1
        self.assertEqual(render._gpu_session_resources(), {})
        stale_lines = render._build_lines([codex], [], "both", False, 0,
                                          layout="wide", term_width=120)
        self.assertFalse(any("GPU moving4:" in render._plain(line)
                             for line in stale_lines if line))

    def test_gpu_labels_require_exact_claim_and_keep_multiple_processes_on_one_gpu(self):
        claimed = {"kind": "session", "harness": "codex", "id": "sid-exact",
                   "source": "persistent-claim+ancestry"}
        run = {"kind": "run", "id": "registered-run"}
        processes = [
            {"pid": 1, "proc_start": 10, "command": "python run.py --engine_mode train --config _m6_lx3nx4.yaml",
             "owner": claimed, "session_owner": claimed},
            {"pid": 2, "proc_start": 20, "command": "python run.py --engine_mode eval --config unknown_eval.yaml",
             "owner": claimed, "session_owner": claimed},
            {"pid": 3, "proc_start": 30, "command": "python registered.py --name registered",
             "owner": run, "session_owner": claimed},
            {"pid": 4, "proc_start": 40, "command": "python ambiguous.py --name ambiguous",
             "owner": {**claimed, "source": "ambiguous-session"}, "session_owner": claimed},
            {"pid": 5, "proc_start": 50, "command": "python wrong.py --name wrong",
             "owner": {**claimed, "id": "sid-other"}, "session_owner": claimed},
            {"pid": 6, "proc_start": None, "command": "python reused.py --name reused",
             "owner": claimed, "session_owner": claimed},
        ]
        snapshot = {"configured": True, "hosts": [{"host": "cnn", "gpus": [
            {"index": 0, "processes": processes},
        ]}]}
        linked = render._gpu_resources_for_session(
            self.session, render._gpu_session_resources(snapshot))
        self.assertEqual(len(linked), 1)
        self.assertEqual(len(linked[0]["processes"]), 2)
        text = render._plain(render._gpu_resource_strip(linked, term_width=168)[0])
        self.assertIn("GPU cnn:0 (M6 학습, unknown_eval)", text)
        for absent in ("registered", "ambiguous", "wrong", "reused", "unknown_eval 학습"):
            self.assertNotIn(absent, text)
        self.assertEqual(render._gpu_process_label("python train.py --engine_mode eval --config other.yaml"),
                         "other")
        self.assertEqual(render._gpu_process_label("python unusual.py"), "python unusual.py")

    def test_managed_run_fences_stale_ancestor_sessions_end_to_end(self):
        module = _compute_hosts_module()
        identity_keys = {
            "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_SELF_SLUG",
            "HEARTING_COMPUTE_RUN_ID", "HEARTING_COMPUTE_HOST",
            "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
            "OPENCODE_SESSION_ID",
        }
        clean = {key: value for key, value in os.environ.items()
                 if key not in identity_keys}
        parent_source = (
            "import os, subprocess, sys\n"
            "env = dict(os.environ)\n"
            "for key in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', "
            "'CODEX_SESSION_ID', 'OPENCODE_SESSION_ID'):\n"
            "    env.pop(key, None)\n"
            "mode = sys.argv[1]\n"
            "if mode != 'generic':\n"
            "    env['HEARTING_COMPUTE_RUN_ID'] = 'managed-run'\n"
            "    env['HEARTING_COMPUTE_HOST'] = 'local'\n"
            "if mode in ('exact', 'generic'):\n"
            "    env['CODEX_THREAD_ID'] = 'launch-thread'\n"
            "if mode == 'conflict':\n"
            "    env['CODEX_THREAD_ID'] = 'launch-thread'\n"
            "    env['CLAUDE_CODE_SESSION_ID'] = 'other-launcher'\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)'], env=env)\n"
            "print(child.pid, flush=True)\n"
            "child.wait()\n"
        )

        with tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR", "/tmp")) as tmp:
            fake_smi = Path(tmp) / "nvidia-smi"
            fake_smi.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "if any(a.startswith('--query-gpu=') for a in sys.argv):\n"
                " print('0, GPU-A, NVIDIA A100, 42, 40960, 12288')\n"
                "else:\n"
                " print('GPU-A, %s, python, 1024' % os.environ['TARGET_PID'])\n",
                encoding="utf-8",
            )
            fake_smi.chmod(0o755)
            stale_parent_env = {
                **clean,
                "CODEX_THREAD_ID": "stale-thread",
                "CODEX_SESSION_ID": "other-stale",
                "CLAUDE_CODE_SESSION_ID": "stale-claude",
            }
            for mode in ("exact", "missing", "conflict", "generic"):
                with self.subTest(mode=mode):
                    child_pid = None
                    parent = subprocess.Popen(
                        [sys.executable, "-c", parent_source, mode],
                        text=True, stdout=subprocess.PIPE, env=stale_parent_env,
                    )
                    try:
                        child_pid = int(parent.stdout.readline().strip())
                        parent_environ = (Path("/proc") / str(parent.pid) / "environ").read_bytes()
                        child_environ = (Path("/proc") / str(child_pid) / "environ").read_bytes()
                        self.assertIn(b"CODEX_THREAD_ID=stale-thread\0", parent_environ)
                        self.assertIn(b"CLAUDE_CODE_SESSION_ID=stale-claude\0",
                                      parent_environ)
                        self.assertNotIn(b"stale-thread", child_environ)
                        self.assertNotIn(b"stale-claude", child_environ)
                        if mode != "generic":
                            self.assertIn(b"HEARTING_COMPUTE_RUN_ID=managed-run\0",
                                          child_environ)
                        result = subprocess.run(
                            ["bash", "-c", module.PROBE_SCRIPT],
                            text=True, capture_output=True, timeout=5,
                            env={
                                **clean,
                                "PATH": tmp + os.pathsep + os.environ.get("PATH", ""),
                                "TARGET_PID": str(child_pid),
                            },
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        payload = json.loads(result.stdout)
                    finally:
                        if child_pid is not None:
                            try:
                                os.kill(child_pid, 15)
                            except ProcessLookupError:
                                pass
                        try:
                            parent.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            parent.terminate()
                            parent.wait(timeout=2)
                        if parent.stdout is not None:
                            parent.stdout.close()

                    process = payload["gpus"][0]["processes"][0]
                    if mode == "generic":
                        self.assertNotEqual(process["owner"]["kind"], "run")
                    else:
                        self.assertEqual(process["owner"]["kind"], "run")
                        self.assertEqual(process["owner"]["id"], "managed-run")
                    session_owner = process.get("session_owner")
                    if mode == "exact":
                        self.assertEqual(
                            (session_owner["harness"], session_owner["id"]),
                            ("codex", "launch-thread"),
                        )
                    else:
                        self.assertIsNone(session_owner)

                    snapshot = {
                        "configured": True,
                        "hosts": [{"host": "local", **payload}],
                    }
                    render.set_compute_hosts(snapshot)
                    try:
                        target_session = Session(
                            harness="codex", pid=103, session_id="launch-thread")
                        linked = render._gpu_resources_for_session(
                            target_session, render._gpu_session_resources(snapshot))
                    finally:
                        render.set_compute_hosts(None)
                    self.assertEqual(bool(linked), mode == "exact")

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


class GpuCommandDisplayTest(unittest.TestCase):
    def rows(self, commands, width=120, progress=None):
        return render._gpu_process_rows({"processes": [
            {"pid": 400 + index, "command": command, "progress": progress}
            for index, command in enumerate(commands)]}, "", width)

    def test_script_and_path_options_are_visible_without_changing_the_snapshot(self):
        process = {"command": "/home/user/env/bin/python /home/user/project/train.py "
                   "--config=/home/user/configs/x.yaml --output ./runs/latest/",
                   "progress": {"line": "TRAIN: 99% loss=0.5", "training": {"attempt": 99}}}
        before = copy.deepcopy(process)
        rows = render._gpu_process_rows({"processes": [process]}, "", 120)
        self.assertEqual([render._plain(row).strip() for row in rows], [
            "↳ python train.py --config=x.yaml --output latest"])
        self.assertEqual(process, before)

    def test_duplicate_filenames_keep_their_parent_folder(self):
        commands = ["python /work/alpha/train.py --config /cfg/a/x.yaml",
                    "python /work/beta/train.py --config /cfg/b/x.yaml"]
        rows = [render._plain(row).strip() for row in self.rows(commands)]
        self.assertEqual(rows, ["↳ python alpha/train.py --config a/x.yaml",
                                "↳ python beta/train.py --config b/x.yaml"])
        same = [render._plain(row).strip() for row in self.rows([commands[0]] * 2)]
        self.assertEqual(same, ["↳ python train.py --config x.yaml"] * 2)

    def test_quoted_paths_and_nonpath_arguments_remain_legible(self):
        command = ('python "/work/my project/train model.py" '
                   '--config="/cfg/my config.yaml" --url https://host/a/b --ratio 1/2 '
                   '-c "print(\'/work/data\')"')
        words = render._gpu_command_words(render._gpu_display_command(command))
        self.assertEqual(words, ["python", "train model.py", "--config=my config.yaml",
                                "--url", "https://host/a/b", "--ratio", "1/2",
                                "-c", "print('/work/data')"])

    def test_middle_ellipsis_preserves_script_and_final_config(self):
        command = "python /long/project/train.py --description " + "x" * 120 + " --config /cfg/x.yaml"
        for width in (40, 60, 100):
            with self.subTest(width=width):
                shown = render._plain(self.rows([command], width)[0])
                self.assertIn("python train.py", shown)
                self.assertIn("…", shown)
                self.assertTrue(shown.endswith("x.yaml"), shown)
                self.assertLess(render._dw(shown), width)

    def test_unicode_and_malformed_commands_stay_bounded(self):
        commands = ["python /work/학습.py --설명 " + "가" * 80 + " --config /cfg/실험.yaml",
                    "python '/unclosed/train.py --config /cfg/x.yaml", ""]
        for width in (0, 1, 6, 23, 60):
            with self.subTest(width=width):
                for row in self.rows(commands, width):
                    self.assertLessEqual(render._dw(render._plain(row)), width)


if __name__ == "__main__":
    unittest.main()
