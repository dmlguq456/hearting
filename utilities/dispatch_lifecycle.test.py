#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from split_module_fixture import patch_split

P = Path(__file__).with_name("dispatch_lifecycle.py")
SPEC = importlib.util.spec_from_file_location(
    "dispatch_lifecycle", P
)
L = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = L
SPEC.loader.exec_module(L)


class LifecycleTest(unittest.TestCase):
    def test_live_leader_does_not_repeat_the_global_process_group_scan(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(.15)"],
                                 start_new_session=True)
        real_scan = L._group_empty
        scans = []
        def scan(pgid):
            self.assertIsNotNone(child.poll(), "live leader already proves nonempty")
            scans.append(pgid)
            return real_scan(pgid)
        try:
            with mock.patch.object(L, "_group_empty", side_effect=scan):
                result = L.wait_foreground(child, 3, poll_interval=.01)
            self.assertEqual(result.failure, "")
            self.assertTrue(result.group_empty)
            self.assertTrue(scans, "exit still requires the full fresh cleanup observation")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    def _run_fake_runtime_chain(self, harness, mode):
        from foreground_activity import ForegroundActivityContext
        deadline = 2
        # A blocked chain must reach no-progress inside the hard deadline,
        # after five interpreter start-ups. An active chain's runtime-only
        # phases (the fake CLI's own quiet time before and after its tool,
        # plus interpreter start-up and exit, none of which counts as
        # progress) must stay inside one window. A quarter of the deadline
        # leaves both sides room on a loaded runner.
        window = deadline / 4
        # Two of the launcher's samples (window / 20 each) are enough for it
        # to observe the CLI alone before and after its tool.
        runtime_only = window / 10
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            helper = base / "runtime_helper.py"
            fake_cli = base / harness
            fake_tool = base / "governor.py"
            log = base / "attempt.log"
            tool = {
                "blocked": "time.sleep(5)",
                "cpu": "end=time.monotonic()+0.65\nwhile time.monotonic()<end: pass",
                "log": "end=time.monotonic()+0.65\nwhile time.monotonic()<end: open(log, 'a').write('x\\n'); time.sleep(0.02)",
                "reaped-cpu": "end=time.monotonic()+0.13\nwhile time.monotonic()<end: pass",
            }[mode]
            fake_tool.write_text(
                "import sys,time\n"
                "mode,log=sys.argv[1:]\n"
                f"{tool}\n",
                encoding="utf-8",
            )
            fake_cli.write_text(
                "import subprocess,sys,time\n"
                "mode,log,tool=sys.argv[1:]\n"
                f"time.sleep({runtime_only})\n"
                "p=subprocess.Popen([sys.executable,tool,mode,log])\n"
                "p.wait()\n"
                f"time.sleep({runtime_only})\n",
                encoding="utf-8",
            )
            helper.write_text(
                "import os,subprocess,sys,time\n"
                "harness,mode,log,tool=sys.argv[1:]\n"
                f"p=subprocess.Popen([sys.executable,{str(fake_cli)!r},mode,log,{str(fake_tool)!r}])\n"
                "while p.poll() is None:\n"
                " end=time.monotonic()+0.004\n"
                " while time.monotonic()<end: pass\n"
                " time.sleep(0.02)\n"
                "p.wait()\n",
                encoding="utf-8",
            )
            # The outer Python leader retains its shell child; the helper remains
            # alive and polls while the fake harness CLI waits for its tool.
            leader = (
                "import subprocess,sys; "
                "subprocess.run(['sh','-c',sys.executable+' '+sys.argv[1]+' '+sys.argv[2]+' '+sys.argv[3]+' '+sys.argv[4]+' '+sys.argv[5]+'; wait'])"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", leader, str(helper), harness, mode, str(log), str(fake_tool)],
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            start = L.process_start_ticks(child.pid)
            context = ForegroundActivityContext(
                window, harness, log, {}, runtime_executable=harness,
            )
            try:
                outcome = L.wait_foreground(child, deadline, poll_interval=0.01,
                                            activity_context=context)
                return outcome
            finally:
                if child.poll() is None:
                    L._bounded_group_stop(child, start or "", poll_interval=0.01)

    def test_quiet_foreground_child_stops_with_no_progress(self):
        from foreground_activity import ForegroundActivityContext
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "attempt.log"
            child = subprocess.Popen(
                ["/bin/sh", "-c", "sleep 10"], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            start = L.process_start_ticks(child.pid)
            context = ForegroundActivityContext(
                window_seconds=0.4, harness="sh", log_path=log,
                metadata={}, runtime_executable="/bin/true",
            )
            try:
                outcome = L.wait_foreground(child, 5, poll_interval=0.02,
                                            activity_context=context)
                self.assertEqual(outcome.failure, "no-progress")
                self.assertTrue(outcome.group_empty)
            finally:
                if child.poll() is None:
                    L._bounded_group_stop(child, start or "", poll_interval=0.01)

    def test_unobserved_time_before_a_known_sample_never_counts_as_quiet(self):
        import foreground_activity as FA
        window = 0.2
        samples = []
        stalled_end = []

        class StalledFirstObservation:
            def __init__(self, *_args):
                pass

            def sample(self):
                samples.append(time.monotonic())
                if len(samples) == 1:
                    # A /proc scan stalls on a loaded host and still cannot
                    # judge the child; nothing about that time is known.
                    time.sleep(0.3)
                    stalled_end.append(time.monotonic())
                    return FA.ActivityObservation(True, True, ("observation-uncertain",))
                return FA.ActivityObservation(False, False, ())

        with tempfile.TemporaryDirectory() as td:
            child = subprocess.Popen(
                ["/bin/sh", "-c", "sleep 10"], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            start = L.process_start_ticks(child.pid)
            context = FA.ForegroundActivityContext(window, "sh", Path(td) / "attempt.log", {})
            try:
                with mock.patch.object(FA, "ForegroundActivityMonitor", StalledFirstObservation):
                    outcome = L.wait_foreground(child, 5, poll_interval=0.01,
                                                activity_context=context)
                self.assertEqual(outcome.failure, "no-progress")
                # The confirming sample comes a full window after the first
                # known one, never a window after the stalled unknown one.
                self.assertGreaterEqual(samples[-1] - stalled_end[0], window)
            finally:
                if child.poll() is None:
                    L._bounded_group_stop(child, start or "", poll_interval=0.01)

    def test_log_and_tool_cpu_activity_survive_multiple_windows(self):
        from foreground_activity import ForegroundActivityContext
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "attempt.log"
            commands = [
                ["/bin/sh", "-c", f"i=0; while [ $i -lt 12 ]; do echo x >> {log}; i=$((i+1)); sleep 0.05; done"],
                ["/bin/sh", "-c", f"{sys.executable} -c 'import time; e=time.monotonic()+0.8; exec(\"while time.monotonic()<e: pass\")'"],
            ]
            for command in commands:
                child = subprocess.Popen(command, start_new_session=True,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                start = L.process_start_ticks(child.pid)
                context = ForegroundActivityContext(
                    window_seconds=0.25, harness=Path(command[0]).name, log_path=log,
                    metadata={}, runtime_executable="/bin/true",
                )
                try:
                    outcome = L.wait_foreground(child, 4, poll_interval=0.02,
                                                activity_context=context)
                    self.assertNotEqual(outcome.failure, "no-progress")
                    self.assertEqual(outcome.failure, "")
                finally:
                    if child.poll() is None:
                        L._bounded_group_stop(child, start or "", poll_interval=0.01)

    def test_runtime_cpu_noise_is_ignored_but_child_tool_cpu_counts(self):
        from foreground_activity import ForegroundActivityContext
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "attempt.log"
            runtime = subprocess.Popen(
                [sys.executable, "-c", "import time; e=time.monotonic()+1.0\nwhile time.monotonic()<e: pass"],
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            start = L.process_start_ticks(runtime.pid)
            context = ForegroundActivityContext(
                window_seconds=0.3, harness=Path(sys.executable).name, log_path=log,
                metadata={}, runtime_executable=sys.executable,
            )
            try:
                outcome = L.wait_foreground(runtime, 4, poll_interval=0.02,
                                            activity_context=context)
                self.assertEqual(outcome.failure, "no-progress")
            finally:
                if runtime.poll() is None:
                    L._bounded_group_stop(runtime, start or "", poll_interval=0.01)

            tool_parent_code = (
                "import subprocess,time\n"
                "tool=subprocess.Popen(['/bin/sh','-c','while :; do :; done'])\n"
                "try:\n"
                " time.sleep(2.4)\n"
                "finally:\n"
                " if tool.poll() is None:\n"
                "  tool.terminate()\n"
                " tool.wait()\n"
            )
            tool_parent = subprocess.Popen(
                [sys.executable, "-c", tool_parent_code],
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            start = L.process_start_ticks(tool_parent.pid)
            try:
                outcome = L.wait_foreground(tool_parent, 5, poll_interval=0.02,
                                            activity_context=context)
                self.assertEqual(outcome.failure, "")
            finally:
                if tool_parent.poll() is None:
                    L._bounded_group_stop(tool_parent, start or "", poll_interval=0.01)

    def test_missing_harness_cli_stays_uncertain_until_hard_deadline(self):
        from foreground_activity import ForegroundActivityContext
        with tempfile.TemporaryDirectory() as td:
            child = subprocess.Popen(
                ["/bin/sh", "-c", "sleep 2"], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            start = L.process_start_ticks(child.pid)
            context = ForegroundActivityContext(
                0.12, "codex", Path(td) / "attempt.log", {},
            )
            began = time.monotonic()
            try:
                outcome = L.wait_foreground(child, 0.55, poll_interval=0.02,
                                            activity_context=context)
                self.assertEqual(outcome.failure, "timeout")
                self.assertGreaterEqual(time.monotonic() - began, 0.5)
                self.assertLess(time.monotonic() - began, 1.5)
            finally:
                if child.poll() is None:
                    L._bounded_group_stop(child, start or "", poll_interval=0.01)

    def test_real_adapter_shaped_runtime_chains_distinguish_blocked_from_tools(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                blocked = self._run_fake_runtime_chain(harness, "blocked")
                self.assertEqual(blocked.failure, "no-progress")
                for mode in ("cpu", "log", "reaped-cpu"):
                    with self.subTest(harness=harness, mode=mode):
                        active = self._run_fake_runtime_chain(harness, mode)
                        self.assertNotEqual(active.failure, "no-progress")
                        self.assertEqual(active.failure, "")
    def test_namespace_detection_supports_host_and_remounted_proc(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            status = base / "status"
            comm = base / "comm"
            status.write_text("Name:\tpython\nNSpid:\t400\n", encoding="utf-8")
            comm.write_text("systemd\n", encoding="utf-8")
            self.assertFalse(L.pid_namespace_scoped(status, comm))
            status.write_text("Name:\tpython\nNSpid:\t400\t1\n", encoding="utf-8")
            self.assertTrue(L.pid_namespace_scoped(status, comm))
            evidence = L.pid_namespace_evidence(status, comm)
            self.assertEqual(evidence["lifecycle_selector_source"], "nspid-vector")
            self.assertEqual(evidence["lifecycle_nspid_width"], "2")
            status.write_text("Name:\tpython\nNSpid:\t1\n", encoding="utf-8")
            comm.write_text("bwrap\n", encoding="utf-8")
            self.assertTrue(L.pid_namespace_scoped(status, comm))

    def test_selection_preserves_detached_compatibility(self):
        self.assertEqual(
            L.select_launch_lifecycle({}, namespace_scoped=False), L.DETACHED
        )
        self.assertEqual(
            L.select_launch_lifecycle({}, namespace_scoped=True), L.FOREGROUND_SCOPED
        )

    def test_override_promoted_for_transient_pid1_class_scope(self):
        # A-1
        self.assertEqual(
            L.select_launch_lifecycle(
                {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
                evidence={
                    "lifecycle_selector_source": "pid1-class",
                    "lifecycle_pid1_class": "non-system-init",
                    "lifecycle_nspid_width": "1",
                },
            ),
            L.FOREGROUND_SCOPED,
        )

    def test_override_retained_for_host_like_scope_no_sandbox(self):
        # A-2 (anchor falsifier #1)
        self.assertEqual(
            L.select_launch_lifecycle(
                {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
                evidence={"lifecycle_selector_source": "host-like"},
            ),
            L.DETACHED,
        )

    def test_override_rejected_for_host_like_scope_workspace_write_sandbox(self):
        # A-3
        self.assertEqual(
            L.select_launch_lifecycle(
                {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
                evidence={"lifecycle_selector_source": "host-like"},
                parent_sandbox="workspace-write",
            ),
            L.FOREGROUND_SCOPED,
        )

    def test_override_rejected_for_env_derived_sandbox_label(self):
        # A-4
        self.assertEqual(
            L.select_launch_lifecycle(
                {
                    "AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1",
                    "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
                },
                evidence={"lifecycle_selector_source": "host-like"},
                parent_sandbox=None,
            ),
            L.FOREGROUND_SCOPED,
        )

    def test_override_rejected_for_every_transient_selector_source(self):
        # A-5
        for source in ("nspid-vector", "pid1-class", "proc-unreadable"):
            self.assertEqual(
                L.select_launch_lifecycle(
                    {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
                    evidence={"lifecycle_selector_source": source},
                ),
                L.FOREGROUND_SCOPED,
                source,
            )

    def test_selection_namespace_scoped_keyword_unchanged_without_override(self):
        # A-6
        self.assertEqual(
            L.select_launch_lifecycle({}, namespace_scoped=True), L.FOREGROUND_SCOPED
        )
        self.assertEqual(
            L.select_launch_lifecycle({}, namespace_scoped=False), L.DETACHED
        )

    def test_wrapper_reselection_promotes_detached_without_failed_attempt(self):
        resolution = L.reconcile_launch_lifecycle(
            L.DETACHED,
            {},
            evidence={
                "lifecycle_selector_source": "pid1-class",
                "lifecycle_nspid_width": "1",
                "lifecycle_pid1_class": "non-system-init",
            },
        )
        self.assertEqual(resolution.requested, L.DETACHED)
        self.assertEqual(resolution.effective, L.FOREGROUND_SCOPED)
        self.assertEqual(resolution.reselection, "promoted-wrapper-scope")
        self.assertEqual(
            resolution.metadata()["lifecycle_selector_source"], "pid1-class"
        )

    def test_wrapper_reselection_retains_override_only_for_host_like_scope(self):
        # A-8
        resolution = L.reconcile_launch_lifecycle(
            L.DETACHED,
            {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
            evidence={"lifecycle_selector_source": "host-like"},
        )
        self.assertEqual(resolution.effective, L.DETACHED)
        self.assertEqual(resolution.reselection, "retained-wrapper-scope")
        self.assertEqual(resolution.override, "honored")
        self.assertEqual(resolution.metadata()["launch_lifecycle_override"], "honored")

    def test_wrapper_reselection_rejects_override_for_transient_scope(self):
        # A-7
        resolution = L.reconcile_launch_lifecycle(
            L.DETACHED,
            {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
            evidence={"lifecycle_selector_source": "pid1-class"},
        )
        self.assertEqual(resolution.effective, L.FOREGROUND_SCOPED)
        self.assertEqual(resolution.reselection, "override-rejected-transient-scope")
        self.assertEqual(resolution.override, "rejected")
        self.assertEqual(
            resolution.metadata()["launch_lifecycle_override"], "rejected"
        )

    def test_wrapper_reselection_reports_rejection_even_without_delta(self):
        # A-9
        resolution = L.reconcile_launch_lifecycle(
            L.FOREGROUND_SCOPED,
            {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
            evidence={"lifecycle_selector_source": "pid1-class"},
        )
        self.assertEqual(resolution.effective, L.FOREGROUND_SCOPED)
        self.assertEqual(resolution.reselection, "override-rejected-transient-scope")

    def test_wrapper_reselection_promotes_without_override_unchanged(self):
        # A-10 (regression, verbatim intent of prior :55-68 test)
        resolution = L.reconcile_launch_lifecycle(
            L.DETACHED,
            {},
            evidence={"lifecycle_selector_source": "pid1-class"},
        )
        self.assertEqual(resolution.effective, L.FOREGROUND_SCOPED)
        self.assertEqual(resolution.reselection, "promoted-wrapper-scope")
        self.assertEqual(resolution.override, "absent")

    def test_foreground_wait_reports_success_and_child_signal(self):
        success = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
        self.assertEqual(L.wait_foreground(success, 2), L.ForegroundResult(0, ""))
        signaled = subprocess.Popen(
            ["sh", "-c", "kill -TERM $$"], start_new_session=True
        )
        outcome = L.wait_foreground(signaled, 2)
        self.assertEqual(outcome.failure, f"signal-{signal.SIGTERM}")

    def test_foreground_wait_forwards_wrapper_signal(self):
        # The wrapper itself was asked to stop: `interrupted`, not the
        # `signal-N` a worker that died of a signal on its own reports above.
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum):
                proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
                timer = threading.Timer(0.05, lambda: os.kill(os.getpid(), signum))
                timer.start()
                try:
                    outcome = L.wait_foreground(proc, 2, poll_interval=0.01)
                    self.assertEqual(outcome.failure, L.FOREGROUND_INTERRUPTED)
                    self.assertEqual(outcome.failure, "interrupted")
                    self.assertTrue(outcome.group_empty)
                    self.assertIsNotNone(proc.poll())
                finally:
                    timer.cancel()
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()

    def test_foreground_wait_reports_interrupted_when_the_child_exits_first(self):
        # The forwarded signal ends the child before the loop's own check: the
        # after-loop branch still reports the wrapper's stop request.
        proc = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
        proc.wait()
        received_late = {"done": False}
        real_group_empty = L._group_empty

        def group_empty_with_signal(pgid):
            if not received_late["done"]:
                received_late["done"] = True
                os.kill(os.getpid(), signal.SIGTERM)
            return real_group_empty(pgid)

        with patch_split(L, "process_start_ticks", return_value="1"), \
             mock.patch.object(L, "_group_empty", side_effect=group_empty_with_signal), \
             mock.patch.object(L, "_terminate_group", return_value="signalled"):
            outcome = L.wait_foreground(proc, 2, poll_interval=0.01)
        self.assertEqual(outcome.failure, "interrupted")

    def test_foreground_timeout_terminates_group(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        outcome = L.wait_foreground(proc, 0.05, poll_interval=0.01)
        self.assertEqual(outcome.failure, "timeout")
        self.assertTrue(outcome.group_empty)
        self.assertIsNotNone(proc.poll())

    def test_foreground_parent_identity_loss_terminates_child_group(self):
        parent = subprocess.Popen(["sleep", "60"])
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        parent_start = L.process_start_ticks(parent.pid)
        timer = threading.Timer(0.05, parent.kill)
        timer.start()
        try:
            outcome = L.wait_foreground(
                child,
                3,
                parent_pid=parent.pid,
                parent_pid_start=parent_start,
                poll_interval=0.01,
            )
            self.assertEqual(outcome.failure, "parent-terminated")
            self.assertIsNotNone(child.poll())
        finally:
            timer.cancel()
            if parent.poll() is None:
                parent.kill()
            parent.wait()
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_foreground_parent_callback_loss_terminates_child_group(self):
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        checks = iter((True, True, False))
        try:
            outcome = L.wait_foreground(
                child,
                3,
                parent_pid=999999,
                parent_pid_start="unobservable",
                parent_is_live=lambda: next(checks, False),
                poll_interval=0.01,
            )
            self.assertEqual(outcome.failure, "parent-terminated")
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_unverifiable_group_is_never_reported_empty(self):
        observation = type("Observation", (), {"state": "unverifiable"})()
        with patch_split(L, "process_group_observation", return_value=observation):
            self.assertIsNone(L._group_empty(123))

    def test_missing_leader_has_no_numeric_group_signal_authority(self):
        proc = mock.Mock(pid=437)
        with patch_split(
            L, "signal_exact_process_group", return_value="leader-gone"
        ) as exact, mock.patch.object(L.os, "killpg") as killpg:
            result = L._terminate_group(proc, signal.SIGTERM, "42")
        self.assertEqual(result, "leader-gone")
        exact.assert_called_once_with(437, "42", signal.SIGTERM)
        killpg.assert_not_called()

    def test_identity_unavailable_still_reaps_direct_child(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        with patch_split(L, "process_start_ticks", return_value=None):
            outcome = L.wait_foreground(proc, 2)
        self.assertEqual(outcome.failure, "process-identity-unavailable")
        self.assertFalse(outcome.group_empty)
        self.assertIsNotNone(proc.poll())

    def test_exceptional_observation_path_does_not_leave_direct_child(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        with mock.patch.object(L, "_group_empty", side_effect=RuntimeError("fixture")):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                L.wait_foreground(proc, 2, poll_interval=0.01)
        self.assertIsNotNone(proc.poll())

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "SIGHUP unavailable")
    def test_foreground_installs_and_restores_sighup_forwarding(self):
        proc = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
        with mock.patch.object(L.signal, "getsignal", return_value=signal.SIG_DFL), \
             mock.patch.object(L.signal, "signal") as install:
            outcome = L.wait_foreground(proc, 2)
        self.assertEqual(outcome.exit_code, 0)
        sighup_calls = [call for call in install.call_args_list if call.args[0] == signal.SIGHUP]
        self.assertEqual(len(sighup_calls), 2)


# A stand-in wrapper: on SIGINT/SIGTERM it "cleans up" for CLEANUP seconds,
# writes how many stop requests it saw, and exits 0 -- what a foreground
# wrapper does while it stops its worker and closes the row.
FAKE_WRAPPER = r"""
import os, signal, sys, time
seen = []
def stop(signum, _frame):
    seen.append(signum)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
cleanup = float(sys.argv[1])
marker = sys.argv[2]
open(marker + ".ready", "w").close()
while not seen:
    time.sleep(0.01)
deadline = time.monotonic() + cleanup
while time.monotonic() < deadline:
    time.sleep(0.01)
with open(marker, "w") as out:
    out.write(",".join(str(s) for s in seen))
print("worker_failure=interrupted", flush=True)
print("cleanup-said-to-stderr", file=sys.stderr, flush=True)
"""


class ForwardingTerminationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "fake_wrapper.py"
        self.script.write_text(FAKE_WRAPPER, encoding="utf-8")
        self.marker = self.root / "seen"

    def command(self, cleanup):
        return [sys.executable, str(self.script), str(cleanup), str(self.marker)]

    def run_with_fixture_pipe_holder(self, *, returncode, hold_seconds, timeout):
        if not sys.platform.startswith("linux"):
            self.skipTest("explicit adopted-child reaping fixture requires Linux")
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
            self.skipTest("PR_GET_CHILD_SUBREAPER is unavailable")
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            self.skipTest("PR_SET_CHILD_SUBREAPER is unavailable")
        pid_file = self.root / "pipe-holder.pid"
        self.script.write_text(
            "import os, pathlib, sys, time\n"
            "delay, pid_file, code = float(sys.argv[1]), sys.argv[2], int(sys.argv[3])\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    time.sleep(delay)\n"
            "    os._exit(0)\n"
            "pathlib.Path(pid_file).write_text(str(pid))\n"
            "print('fixture-receipt=wrapper-exited', flush=True)\n"
            "print('fixture-stderr=wrapper-exited', file=sys.stderr, flush=True)\n"
            "sys.exit(code)\n",
            encoding="utf-8",
        )
        started = time.monotonic()
        try:
            run = L.run_forwarding_termination(
                [sys.executable, str(self.script), str(hold_seconds), str(pid_file), str(returncode)],
                capture=True, timeout=timeout,
            )
            elapsed = time.monotonic() - started
            holder_pid = int(pid_file.read_text())
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                try:
                    waited, _status = os.waitpid(holder_pid, os.WNOHANG)
                except ChildProcessError:
                    self.fail("fixture pipe holder was not adopted for explicit reaping")
                if waited == holder_pid:
                    return run, elapsed
                time.sleep(0.01)
            os.kill(holder_pid, signal.SIGKILL)
            os.waitpid(holder_pid, 0)
            self.fail("finite fixture pipe holder did not exit")
        finally:
            if pid_file.is_file():
                try:
                    holder_pid = int(pid_file.read_text())
                    waited, _status = os.waitpid(holder_pid, os.WNOHANG)
                    if waited == 0:
                        os.kill(holder_pid, signal.SIGKILL)
                        os.waitpid(holder_pid, 0)
                except (ChildProcessError, ProcessLookupError, ValueError):
                    pass
            libc.prctl(36, previous.value, 0, 0, 0)

    def fd_count(self):
        path = Path("/proc/self/fd")
        return len(list(path.iterdir())) if path.is_dir() else None

    def send_after_ready(self, signals, *, gap=0.0):
        """Send stop requests to THIS process once the wrapper can take them."""
        sent = []
        ready = Path(str(self.marker) + ".ready")

        def fire():
            deadline = time.monotonic() + 20
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            for index, signum in enumerate(signals):
                if index:
                    time.sleep(gap)
                sent.append(time.monotonic())
                os.kill(os.getpid(), signum)
        thread = threading.Thread(target=fire, daemon=True)
        thread.start()
        return thread, sent

    def test_plain_run_captures_both_streams_without_signals(self):
        run = L.run_forwarding_termination(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
            capture=True, timeout=10,
        )
        self.assertEqual((run.returncode, run.stdout, run.stderr), (3, "out\n", "err\n"))
        self.assertIsNone(run.received_signal)
        self.assertFalse(run.cleanup_incomplete)

    def test_post_exit_drain_preserves_receipt_streams_and_zero_or_nonzero_status(self):
        for returncode in (0, 7):
            with self.subTest(returncode=returncode):
                run, elapsed = self.run_with_fixture_pipe_holder(
                    returncode=returncode, hold_seconds=1.2, timeout=2,
                )
                self.assertEqual(run.returncode, returncode)
                self.assertEqual(run.stdout, "fixture-receipt=wrapper-exited\n")
                self.assertEqual(run.stderr, "fixture-stderr=wrapper-exited\n")
                self.assertTrue(run.cleanup_incomplete)
                self.assertLess(elapsed, 0.8)

    def test_timeout_none_is_bounded_after_wrapper_exit_with_delayed_eof(self):
        run, elapsed = self.run_with_fixture_pipe_holder(
            returncode=0, hold_seconds=1.2, timeout=None,
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("fixture-receipt=wrapper-exited", run.stdout)
        self.assertTrue(run.cleanup_incomplete)
        self.assertLess(elapsed, 0.8)

    def test_finite_large_output_is_fully_drained_across_bounded_turns(self):
        output = "o" * (256 * 1024)
        error = "e" * (192 * 1024)
        self.script.write_text(
            "import sys\n"
            f"sys.stdout.write({output!r} + '\\nreceipt=complete\\n'); sys.stdout.flush()\n"
            f"sys.stderr.write({error!r} + '\\nreceipt=stderr-complete\\n'); sys.stderr.flush()\n",
            encoding="utf-8",
        )
        run = L.run_forwarding_termination(
            [sys.executable, str(self.script)], capture=True, timeout=5,
        )
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout, output + "\nreceipt=complete\n")
        self.assertEqual(run.stderr, error + "\nreceipt=stderr-complete\n")
        self.assertFalse(run.cleanup_incomplete)

    def test_sustained_output_cannot_starve_the_outer_timeout(self):
        self.script.write_text(
            "import os, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text('ready')\n"
            "chunk = b'x' * 65536\n"
            "while True: os.write(1, chunk)\n",
            encoding="utf-8",
        )
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            L.run_forwarding_termination(
                [sys.executable, str(self.script), str(self.marker)],
                capture=True, timeout=0.25, poll_interval=0.01,
            )
        self.assertLess(time.monotonic() - started, 1.5)

    def test_sustained_output_cannot_starve_the_fixed_signal_grace(self):
        self.script.write_text(
            "import os, pathlib, signal, sys\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "pathlib.Path(sys.argv[1]).write_text('ready')\n"
            "chunk = b'x' * 65536\n"
            "while True: os.write(1, chunk)\n",
            encoding="utf-8",
        )
        thread, sent = self.send_after_ready([signal.SIGTERM])
        try:
            run = L.run_forwarding_termination(
                [sys.executable, str(self.script), str(self.marker) + ".ready"],
                capture=True, timeout=10, grace=0.3, poll_interval=0.01,
            )
            ended = time.monotonic()
        finally:
            thread.join()
        self.assertEqual(len(sent), 1)
        self.assertEqual(run.received_signal, signal.SIGTERM)
        self.assertEqual(run.returncode, -signal.SIGKILL)
        self.assertTrue(run.cleanup_incomplete)
        self.assertGreaterEqual(ended - sent[0], 0.3)
        self.assertLess(ended - sent[0], 1.5)

    def test_post_exit_pipe_cancellation_leaves_no_reader_thread_or_fd(self):
        before_threads = {thread.ident for thread in threading.enumerate()}
        before_fds = self.fd_count()
        for _ in range(3):
            run, _elapsed = self.run_with_fixture_pipe_holder(
                returncode=0, hold_seconds=0.7, timeout=None,
            )
            self.assertTrue(run.cleanup_incomplete)
        self.assertEqual({thread.ident for thread in threading.enumerate()}, before_threads)
        after_fds = self.fd_count()
        if before_fds is not None:
            self.assertEqual(after_fds, before_fds)

    def test_uncaptured_run_returns_no_text(self):
        run = L.run_forwarding_termination([sys.executable, "-c", "pass"], capture=False, timeout=None)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, None, None))

    def test_outer_timeout_keeps_subprocess_run_meaning(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            L.run_forwarding_termination(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture=True, timeout=0.3,
            )
        self.assertLess(time.monotonic() - started, 5)

    def test_signal_is_forwarded_and_the_wrapper_cleanup_is_awaited(self):
        before = signal.getsignal(signal.SIGINT)
        thread, sent = self.send_after_ready([signal.SIGINT])
        run = L.run_forwarding_termination(self.command(1.0), capture=True, timeout=30, grace=10)
        ended = time.monotonic()
        thread.join()
        self.assertEqual(run.received_signal, signal.SIGINT)
        self.assertFalse(run.cleanup_incomplete)
        self.assertEqual(run.returncode, 0)
        self.assertIn("worker_failure=interrupted", run.stdout)
        self.assertIn("cleanup-said-to-stderr", run.stderr)
        self.assertEqual(self.marker.read_text(), str(int(signal.SIGINT)))
        self.assertGreaterEqual(ended - sent[0], 1.0)
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_foreground_outer_timeout_forwards_TERM_and_waits_for_receipt_cleanup(self):
        run = L.run_forwarding_termination(self.command(.2), capture=True, timeout=.3,
                                          grace=2, terminate_on_timeout=True)
        self.assertTrue(run.timed_out)
        self.assertEqual(run.received_signal, signal.SIGTERM)
        self.assertFalse(run.cleanup_incomplete)
        self.assertTrue(self.marker.is_file())
        self.assertIn('worker_failure=interrupted', run.stdout)

    def test_foreground_outer_timeout_reports_incomplete_without_reexecution(self):
        run = L.run_forwarding_termination(self.command(2), capture=True, timeout=.3,
                                          grace=.1, terminate_on_timeout=True)
        self.assertTrue(run.timed_out)
        self.assertTrue(run.cleanup_incomplete)
        self.assertEqual(run.returncode, -signal.SIGKILL)
        self.assertFalse(self.marker.exists())

    def test_repeated_signals_are_forwarded_without_restarting_the_deadline(self):
        # Wrapper cleanup takes 3s; the grace is 1.5s. Stop requests arrive at
        # 0s, 0.6s and 1.2s: a deadline restarted by each would fire at 2.7s.
        thread, sent = self.send_after_ready(
            [signal.SIGINT, signal.SIGTERM, signal.SIGINT], gap=0.6)
        run = L.run_forwarding_termination(self.command(3.0), capture=True, timeout=30, grace=1.5)
        ended = time.monotonic()
        thread.join()
        self.assertEqual(len(sent), 3)
        self.assertEqual(run.received_signal, signal.SIGINT)
        self.assertTrue(run.cleanup_incomplete)
        self.assertEqual(run.returncode, -signal.SIGKILL)
        self.assertFalse(self.marker.exists())
        self.assertGreaterEqual(ended - sent[0], 1.45)
        self.assertLess(ended - sent[0], 2.4)

    def test_repeated_signals_reach_the_wrapper(self):
        thread, _sent = self.send_after_ready([signal.SIGINT, signal.SIGTERM], gap=0.2)
        run = L.run_forwarding_termination(self.command(1.0), capture=True, timeout=30, grace=10)
        thread.join()
        self.assertFalse(run.cleanup_incomplete)
        self.assertEqual(
            self.marker.read_text(), f"{int(signal.SIGINT)},{int(signal.SIGTERM)}")

    def test_a_stop_before_launch_launches_nothing(self):
        def stopped_popen(*_args, **_kwargs):
            raise AssertionError("must not launch after a stop request")

        real_signal = L.signal.signal
        real_getsignal = L.signal.getsignal
        before = real_getsignal(signal.SIGINT)
        installed = {}

        def capture(signum, handler):
            installed[signum] = handler
            return real_signal(signum, handler)

        with mock.patch.object(L.signal, "signal", side_effect=capture), \
             mock.patch.object(L.subprocess, "Popen", side_effect=stopped_popen) as popen:
            # The stop request lands while the handlers are being installed.
            def getsignal(signum):
                handler = installed.get(signal.SIGINT)
                if handler is not None and signum == signal.SIGHUP:
                    handler(signal.SIGINT, None)
                return real_getsignal(signum)

            with mock.patch.object(L.signal, "getsignal", side_effect=getsignal):
                run = L.run_forwarding_termination(["true"], capture=True, timeout=5)
        popen.assert_not_called()
        self.assertEqual(run.received_signal, signal.SIGINT)
        self.assertEqual((run.stdout, run.stderr), ("", ""))
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_handlers_are_left_alone_off_the_main_thread(self):
        result = {}

        def work():
            result["run"] = L.run_forwarding_termination(
                [sys.executable, "-c", "print('ok')"], capture=True, timeout=10)

        thread = threading.Thread(target=work)
        thread.start()
        thread.join(10)
        self.assertEqual(result["run"].stdout, "ok\n")


if __name__ == "__main__":
    unittest.main()
