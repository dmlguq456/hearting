#!/usr/bin/env python3
"""Managed service provenance through the existing lifecycle callbacks."""

from __future__ import annotations

import importlib.util
import errno
import io
import json
import os
from pathlib import Path
import fcntl
import hashlib
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools" / "install"))

import peer_obligations as obligations  # noqa: E402
import dispatch_contract as contract  # noqa: E402
import runtime_activation  # noqa: E402

_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "peer_steward_under_runner_lifecycle_test", ROOT / "utilities/peer-steward.py")
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)


def _pidfd_handoff_available():
    if not callable(getattr(signal, "pidfd_send_signal", None)):
        return False
    try:
        fd = contract.pidfd_open(os.getpid())
    except OSError as exc:
        if exc.errno == errno.ENOSYS:
            return False
        raise
    try:
        signal.pidfd_send_signal(fd, 0)
    except OSError as exc:
        if exc.errno == errno.ENOSYS:
            return False
        raise
    finally:
        os.close(fd)
    return True


_PIDFD_HANDOFF = _pidfd_handoff_available()


class LifecycleBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.data = self.base / "data"
        self.releases = self.data / "releases"
        self.state = self.base / "state"
        self.jobs = self.base / "dispatch" / "jobs.log"
        self.external_python = self.base / "external-python"
        for path in (self.home, self.releases, self.state, self.external_python):
            path.mkdir(parents=True, exist_ok=True)
        self.a = self._release("A")
        self.b = self._release("B")
        self.current = self.data / "current"
        self._point_current(self.a)
        self.env = self._environment(self.a)
        obligations._RUNNERS.clear()
        self.addCleanup(obligations._RUNNERS.clear)

    def _release(self, version):
        root = self.releases / version
        for relative in ("core", "utilities", "tools/install", "tools/fleet",
                         "hooks", "adapters/codex/hooks", "adapters/opencode/plugins"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
        (root / "harness-manifest.json").write_text("{}\n", encoding="utf-8")
        (root / "core/CORE.md").write_text("fixture\n", encoding="utf-8")
        for relative in ("utilities/peer_obligations.py", "utilities/peer-steward.py",
                         "tools/install/distribution.py", "hooks/herdr-session-projection.py",
                         "adapters/codex/hooks/herdr_session_projection.py",
                         "adapters/opencode/plugins/hearting-guards.js"):
            shutil.copy2(ROOT / relative, root / relative)
        (root / "utilities/peer-message.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def peer_state_root():\n"
            "    return Path(os.environ['AGENT_PEER_LEDGER_ROOT'])\n",
            encoding="utf-8")
        (root / "tools/fleet/herdr_projection.py").write_text(
            "def project(*args, **kwargs):\n    return True\n", encoding="utf-8")
        return root

    def _environment(self, code_root):
        return {
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.base / "xdg-data"),
            "XDG_STATE_HOME": str(self.base / "xdg-state"),
            "HARNESS_DATA_ROOT": str(self.data),
            "AGENT_PEER_LEDGER_ROOT": str(self.state),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_HOME": str(code_root),
            "HERDR_PANE_ID": "fixture-pane",
            "AGENT_RUNTIME_ROOT": str(self.a),
            "PYTHONPATH": os.pathsep.join((str(self.a / "utilities"), str(self.external_python))),
            "PATH": os.environ.get("PATH", ""),
        }

    def _point_current(self, target):
        self.current.unlink(missing_ok=True)
        self.current.symlink_to(target, target_is_directory=True)

    def _load_python_callback(self, name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _import_with_tools(self, root, name, path):
        saved = {key: sys.modules.get(key) for key in ("fleet", "fleet.herdr_projection")}
        sys.modules.pop("fleet", None)
        sys.modules.pop("fleet.herdr_projection", None)
        sys.path.insert(0, str(root / "tools"))
        try:
            return self._load_python_callback(name, path)
        finally:
            sys.path.remove(str(root / "tools"))
            sys.modules.pop("fleet", None)
            sys.modules.pop("fleet.herdr_projection", None)
            for key, value in saved.items():
                if value is not None:
                    sys.modules[key] = value

    def _claude_callback(self):
        module = self._import_with_tools(
            self.a, "fixture_claude_projection", self.a / "hooks/herdr-session-projection.py")
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(subprocess, "Popen") as launch, \
             mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps({
                 "session_id": "sid-a", "hook_event_name": "SessionStart"}))):
            self.assertEqual(module.main(), 0)
        return launch.call_args.args[0], launch.call_args.kwargs

    def _codex_callback(self):
        module = self._import_with_tools(
            self.a, "fixture_codex_projection",
            self.a / "adapters/codex/hooks/herdr_session_projection.py")
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertTrue(module.project({"hook_event_name": "SessionStart"}, "sid-a"))
        return launch.call_args.args[0], launch.call_args.kwargs

    def _opencode_callback(self):
        if not shutil.which("node"):
            self.skipTest("node is unavailable for the OpenCode callback fixture")
        source = (self.a / "adapters/opencode/plugins/hearting-guards.js").read_text()
        start = source.index("function reconnectPeerObligations() {")
        end = source.index("\n}", start) + 2
        function = source[start:end]
        script = f'''import path from "node:path";
import {{ realpathSync }} from "node:fs";
const root = process.env.FIXTURE_ROOT;
const pluginRoot = root;
const calls = [];
const spawn = (exe, args, options) => {{
  calls.push({{exe, args, options}});
  return {{on() {{ return this }}, unref() {{}}}};
}};
{function}
reconnectPeerObligations();
console.log(JSON.stringify(calls[0]));
'''
        env = dict(self.env, FIXTURE_ROOT=str(self.a))
        run = subprocess.run(["node", "--input-type=module", "-e", script], env=env,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        captured = json.loads(run.stdout)
        return [captured["exe"], *captured["args"]], captured["options"]

    def _activation_callback(self):
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(runtime_activation.subprocess, "Popen") as launch:
            self.assertTrue(runtime_activation.reconnect_peer_obligations(self.b))
        return launch.call_args.args[0], launch.call_args.kwargs

    def _open_duty(self, suffix):
        store = obligations.ObligationStore(self.state)
        store.create("retire-fixture-" + suffix, "retire", {"fixture": suffix},
                     {"target": "unobserved-fixture"})

    def _watch_arm(self, *, generation=3, rearm_count=17, timeout=86400):
        return {
            "watch_id": "watch-exact", "target": "fixture-target", "server": "named",
            "agent": {"harness": "codex", "session_id": "sid-watch", "pane": "w1:p9"},
            "steward": {"harness": "claude", "session_id": "sid-steward",
                        "project": "/fixture/steward"},
            "armed_ts": "2026-10-18T01:02:03Z", "until": ["idle", "done"],
            "refs": ["attempt-one", "attempt-two"], "timeout": timeout,
            "rearmed_from": "watch-prior", "rearm_count": rearm_count,
            "observer_generation": generation,
        }

    def _v33533_watch_argv(self, watch_id, arm, *, rearm_count, initial):
        """Reproduce the frozen v3.35.33 initial/restart builders verbatim."""
        agent = arm["agent"]
        steward = arm["steward"]
        argv = [
            "__watch-run", "--watch-id", watch_id,
            "--target", arm["target"], "--server", arm["server"],
            "--expected-harness", agent["harness"],
            "--expected-session-id", agent["session_id"],
            "--expected-pane", agent["pane"],
            "--steward-harness", steward["harness"],
            "--steward-session-id", steward["session_id"],
            "--steward-project", steward["project"],
            "--armed-ts", arm["armed_ts"], "--rearm-count", str(rearm_count),
        ]
        for state in arm["until"]:
            argv.extend(("--until", state))
        if initial and arm.get("timeout") is not None:
            argv.extend(("--timeout", str(arm["timeout"])))
        for ref in arm["refs"]:
            argv.extend(("--ref", ref))
        if not initial and arm.get("timeout") is not None:
            argv.extend(("--timeout", str(arm["timeout"])))
        if initial and arm.get("rearmed_from"):
            argv.extend(("--rearmed-from", arm["rearmed_from"]))
        return argv

    def _fixture_watch_script(self, code_root, source):
        path = code_root / "utilities/peer-steward.py"
        path.write_text(source, encoding="utf-8")
        return path

    def _start_fixture_watch(self, code_root, duty_argv, *, suffix, extra_env=None,
                             program_source=None, remove_env=()):
        script = code_root / "utilities/peer-steward.py"
        ready = self.base / (suffix + "-ready")
        source = program_source or (
            "import fcntl, os, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(os.environ['FIXTURE_READY'], 'w').write('ready')\n"
            "while True: time.sleep(0.1)\n"
        )
        if not script.exists() or script.read_text(encoding="utf-8") != source:
            self._fixture_watch_script(code_root, source)
        lock_path = self.state / (suffix + ".watch.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        first_repeated = next((index for index, value in enumerate(duty_argv)
                               if value in {"--until", "--ref", "--timeout", "--rearmed-from",
                                            "--service-current"}), len(duty_argv))
        args = [*duty_argv[:first_repeated], "--lock-fd", str(fd),
                *duty_argv[first_repeated:]]
        env = dict(self._environment(code_root), FIXTURE_READY=str(ready))
        for key in remove_env:
            env.pop(key, None)
        if extra_env:
            env.update(extra_env)
        process = subprocess.Popen(
            [sys.executable, str(script), *args], pass_fds=(fd,), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(fd)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            process.terminate()
            process.wait(timeout=5)
            self.fail(f"fixture watch did not acquire its lock: {suffix}")
        return process, lock_path

    def _start_inherited_fixture_watch(self, code_root, duty_argv, *, lock_path,
                                       environment, suffix):
        """Transfer a real locked open description from an exiting starter."""
        script = code_root / "utilities/peer-steward.py"
        ready = self.base / (suffix + "-ready")
        source = (
            "import sys, time\n"
            f"open({str(ready)!r}, 'w', encoding='utf-8').write('ready')\n"
            "while True: time.sleep(0.1)\n"
        )
        self._fixture_watch_script(code_root, source)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        starter_source = (
            "import fcntl, json, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "if fd != 5:\n"
            "    os.dup2(fd, 5, inheritable=True)\n"
            "    os.close(fd)\n"
            "fcntl.flock(5, fcntl.LOCK_EX)\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    null = os.open(os.devnull, os.O_WRONLY)\n"
            "    os.dup2(null, 1)\n"
            "    os.dup2(null, 2)\n"
            "    os.execve(sys.executable, [sys.executable, sys.argv[2], "
            "*json.loads(sys.argv[3]), '--lock-fd', '5'], os.environ)\n"
            "print(json.dumps({'watcher': child, 'starter': os.getpid()}), flush=True)\n"
            "os.close(5)\n"
        )
        starter = None
        process = None
        try:
            starter = subprocess.Popen(
                [sys.executable, "-c", starter_source, str(lock_path), str(script),
                 json.dumps(list(duty_argv))], env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            output, error = starter.communicate(timeout=5)
            self.assertEqual(starter.returncode, 0, error)
            info = json.loads(output.strip())
            watcher_pid = info["watcher"]
            starter_pid = info["starter"]
            self.assertFalse(Path(f"/proc/{starter_pid}").exists(),
                             "starter must be gone while the observer keeps its lock")
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready.exists():
                os.kill(watcher_pid, signal.SIGTERM)
                self.fail(f"inherited-lock fixture did not start: {suffix}")
            start = obligations._proc_start_identity(watcher_pid)

            class ForkedObserver:
                def __init__(self):
                    self.pid = watcher_pid

                def poll(self):
                    return None if obligations._proc_start_identity(self.pid) == start \
                        else -signal.SIGTERM

                def terminate(self):
                    if self.poll() is None:
                        os.kill(self.pid, signal.SIGTERM)

                def wait(self, timeout=None):
                    deadline = None if timeout is None else time.monotonic() + timeout
                    while self.poll() is None:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired("inherited-watch", timeout)
                        time.sleep(0.01)
                    return -signal.SIGTERM

            process = ForkedObserver()
            return process, starter_pid, ready
        finally:
            if process is None and starter is not None and starter.poll() is None:
                starter.terminate()
                starter.wait(timeout=5)

    def _replace_fixture_watch(self, process, lock_path, arm, expected_argvs, *, root=None,
                               stored_start=Ellipsis, requested_lock=None):
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self._environment(self.a), clear=True):
            binding = obligations.managed_service_binding(self.a)
            expected_start = (obligations._proc_start_identity(process.pid)
                              if stored_start is Ellipsis else stored_start)
            return obligations.replace_managed_observer(
                process.pid, expected_start, requested_lock or lock_path,
                "utilities/peer-steward.py", expected_argvs[0], binding,
                lock_fd_option="--lock-fd", exact_arguments=True,
                argument_variants=expected_argvs[1:], root=root or self.a)

    def _run_captured_cli(self, command, options, *, expected_root):
        callback_env = options.get("env") or self.env
        code_root = Path(command[1]).resolve().parent.parent
        cli_args = command[2:]
        self.assertEqual(cli_args[:1], ["__ensure-obligations"])
        self.assertIn("--lifecycle-root", cli_args)
        self._open_duty("callback-" + code_root.name)
        original_file = obligations.__file__
        obligations._RUNNERS.clear()
        try:
            with mock.patch.object(obligations, "__file__",
                                   str(code_root / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, callback_env, clear=True), \
                 mock.patch.object(peer_steward, "_ensure_watch_observers"), \
                 mock.patch.object(peer_steward, "_resume_message_obligation"), \
                 mock.patch.object(peer_steward.dispatch_batch_obligations, "ensure_observers"), \
                 mock.patch.object(obligations.subprocess, "Popen") as runner_launch:
                self.assertEqual(peer_steward.main(cli_args), 0)
            argv = runner_launch.call_args.args[0]
            self.assertEqual(Path(argv[1]), expected_root / "utilities/peer-steward.py")
            self.assertEqual(argv[-2:], ["--service-current", str(self.current)])
            child_env = runner_launch.call_args.kwargs["env"]
            self.assertEqual(child_env["AGENT_HOME"], str(expected_root))
            self.assertEqual(child_env["AGENT_DISPATCH_JOBS"], str(self.jobs))
            self.assertEqual(child_env["AGENT_RUNTIME_ROOT"], str(self.a))
            paths = child_env["PYTHONPATH"].split(os.pathsep)
            self.assertIn(str(expected_root / "utilities"), paths)
            self.assertIn(str(self.external_python), paths)
        finally:
            obligations.__file__ = original_file
            obligations._RUNNERS.clear()

    def test_all_lifecycle_callbacks_use_the_same_managed_binding(self):
        self._point_current(self.b)
        with mock.patch.dict(os.environ, self.env, clear=True):
            callbacks = [self._claude_callback(), self._codex_callback(),
                         self._opencode_callback(), self._activation_callback()]
        for command, options in callbacks:
            with self.subTest(callback=command[1]):
                self._run_captured_cli(command, options, expected_root=self.b)

    def test_normal_producer_reuses_locked_binding_but_direct_and_checkout_stay_fixed(self):
        self._point_current(self.b)
        lock_path = obligations.ObligationStore(self.state).root / obligations.RUNNER_LOCK_NAME
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self.env, clear=True):
            self.assertFalse(obligations.ensure_normal_runner(
                self.state, lifecycle_root=str(self.a)))
        original_inode = (lock_path.stat().st_dev, lock_path.stat().st_ino)
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"schema": 1, "install_data_root": str(self.data),
                                   "service_current": str(self.current)})

        store = obligations.ObligationStore(self.state)
        store.create("ordinary-producer", "message", {"fixture": "normal"},
                     {"ref": "a" * 32, "body_digest": "b" * 64})
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertTrue(obligations.ensure_normal_runner(self.state))
            argv = launch.call_args.args[0]
            self.assertEqual(Path(argv[1]), self.b / "utilities/peer-steward.py")
            self.assertEqual(argv[-2:], ["--service-current", str(self.current)])
            child_env = launch.call_args.kwargs["env"]
            self.assertEqual(child_env["AGENT_HOME"], str(self.b))
            self.assertEqual(child_env["AGENT_DISPATCH_JOBS"], str(self.jobs))
        self.assertEqual((lock_path.stat().st_dev, lock_path.stat().st_ino), original_inode)
        self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8")), payload)

        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as direct:
            self.assertTrue(obligations.ensure_runner(self.state))
            direct_argv = direct.call_args.args[0]
            self.assertEqual(Path(direct_argv[1]), self.a / "utilities/peer-steward.py")
            self.assertNotIn("--service-current", direct_argv)

        checkout = self.base / "checkout"
        (checkout / "utilities").mkdir(parents=True)
        (checkout / "utilities/peer-steward.py").touch()
        checkout_env = self._environment(checkout)
        with mock.patch.object(obligations, "__file__",
                               str(checkout / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, checkout_env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as fixed:
            self.assertTrue(obligations.ensure_normal_runner(
                self.state, lifecycle_root=str(checkout)))
            fixed_argv = fixed.call_args.args[0]
            self.assertEqual(Path(fixed_argv[1]), checkout / "utilities/peer-steward.py")
            self.assertNotIn("--service-current", fixed_argv)

        mismatch_env = self._environment(self.b)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, mismatch_env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as mismatch:
            self.assertTrue(obligations.ensure_normal_runner(self.state))
            mismatch_argv = mismatch.call_args.args[0]
            self.assertEqual(Path(mismatch_argv[1]), self.a / "utilities/peer-steward.py")
            self.assertNotIn("--service-current", mismatch_argv)

        foreign_data = self.base / "foreign-data"
        foreign_releases = foreign_data / "releases"
        foreign_releases.mkdir(parents=True)
        foreign = foreign_releases / "Foreign"
        shutil.copytree(self.b, foreign)
        (foreign_data / "current").symlink_to(foreign, target_is_directory=True)
        foreign_env = self._environment(foreign)
        foreign_env["HARNESS_DATA_ROOT"] = str(foreign_data)
        with mock.patch.object(obligations, "__file__",
                               str(foreign / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, foreign_env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as other_install:
            self.assertTrue(obligations.ensure_normal_runner(self.state))
            foreign_argv = other_install.call_args.args[0]
            self.assertEqual(Path(foreign_argv[1]), foreign / "utilities/peer-steward.py")
            self.assertNotIn("--service-current", foreign_argv)

        lock_path.write_text("not-json", encoding="utf-8")
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as malformed:
            self.assertTrue(obligations.ensure_normal_runner(self.state))
            malformed_argv = malformed.call_args.args[0]
            self.assertEqual(Path(malformed_argv[1]), self.a / "utilities/peer-steward.py")
            self.assertNotIn("--service-current", malformed_argv)
        self.assertEqual(lock_path.read_text(encoding="utf-8"), "not-json")

    def test_fixed_invocations_checkouts_foreign_installs_and_overrides_stay_fixed(self):
        self._point_current(self.b)
        cases = []
        checkout = self.base / "checkout"
        checkout.mkdir()
        for relative in ("utilities", "tools/install", "core"):
            (checkout / relative).mkdir(parents=True, exist_ok=True)
        (checkout / "core/CORE.md").touch()
        (checkout / "harness-manifest.json").touch()
        for relative in ("utilities/peer_obligations.py", "utilities/peer-steward.py",
                         "tools/install/distribution.py"):
            shutil.copy2(ROOT / relative, checkout / relative)
        (checkout / "utilities/peer-message.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def peer_state_root():\n"
            "    return Path(os.environ['AGENT_PEER_LEDGER_ROOT'])\n",
            encoding="utf-8")
        foreign_data = self.base / "foreign-data"
        foreign = foreign_data / "releases" / "Foreign"
        for relative in ("utilities", "tools/install", "core"):
            (foreign / relative).mkdir(parents=True, exist_ok=True)
        (foreign / "RELEASE_VERSION").touch()
        (foreign / "harness-manifest.json").touch()
        (foreign / "core/CORE.md").touch()
        for relative in ("utilities/peer_obligations.py", "utilities/peer-steward.py",
                         "tools/install/distribution.py"):
            shutil.copy2(ROOT / relative, foreign / relative)
        (foreign / "utilities/peer-message.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def peer_state_root():\n"
            "    return Path(os.environ['AGENT_PEER_LEDGER_ROOT'])\n",
            encoding="utf-8")

        cases.extend((
            ("direct-pin", self.a, self._environment(self.a), ["__ensure-obligations"]),
            ("checkout", checkout, self._environment(checkout),
             ["__ensure-obligations", "--lifecycle-root", str(checkout)]),
            ("foreign-install", foreign, self._environment(foreign),
             ["__ensure-obligations", "--lifecycle-root", str(foreign)]),
            ("override-mismatch", self.a, self._environment(self.b),
             ["__ensure-obligations", "--lifecycle-root", str(self.a)]),
        ))
        for name, code_root, env, cli_args in cases:
            with self.subTest(case=name), \
                 mock.patch.object(obligations, "__file__",
                                   str(code_root / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(peer_steward, "_ensure_watch_observers"), \
                 mock.patch.object(peer_steward, "_resume_message_obligation"), \
                 mock.patch.object(peer_steward.dispatch_batch_obligations, "ensure_observers"), \
                 mock.patch.object(obligations.subprocess, "Popen") as launch:
                self._open_duty(name)
                self.assertEqual(peer_steward.main(cli_args), 0)
                argv = launch.call_args.args[0]
                self.assertEqual(Path(argv[1]), code_root / "utilities/peer-steward.py")
                self.assertNotIn("--service-current", argv)

    def test_broken_pointer_keeps_binding_and_retries_from_the_running_release(self):
        self.current.unlink()
        self.current.symlink_to(self.releases / "missing", target_is_directory=True)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self.env, clear=True):
            binding = obligations.managed_service_binding(self.a)
            self.assertEqual(binding, str(self.current))
            self.assertIsNone(obligations.active_service_root(binding))
            self._open_duty("broken-pointer")
            with mock.patch.object(obligations.subprocess, "Popen") as launch:
                self.assertTrue(obligations.ensure_runner(self.state, service_current=binding))
            argv = launch.call_args.args[0]
            self.assertEqual(Path(argv[1]), self.a / "utilities/peer-steward.py")
            self.assertEqual(argv[-2:], ["--service-current", str(self.current)])
            self._point_current(self.b)
            self.assertEqual(obligations.active_service_root(binding), self.b)

    def test_service_script_uses_only_the_verified_lifecycle_binding(self):
        self._point_current(self.b)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self._environment(self.a), clear=True):
            binding = obligations.managed_service_binding(self.a)
            script, environment, observed_binding = obligations.service_script(
                "utilities/peer-steward.py", binding)
            self.assertEqual(script, self.b / "utilities/peer-steward.py")
            self.assertEqual(environment["AGENT_HOME"], str(self.b))
            self.assertEqual(observed_binding, binding)

            fixed_script, fixed_environment, fixed_binding = obligations.service_script(
                "utilities/peer-steward.py")
            self.assertEqual(fixed_script, self.a / "utilities/peer-steward.py")
            self.assertIsNone(fixed_environment)
            self.assertIsNone(fixed_binding)

    def test_activation_request_preserves_binding_for_the_shared_runner(self):
        from types import SimpleNamespace

        self._point_current(self.b)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self._environment(self.a), clear=True), \
             mock.patch.object(peer_steward, "_watch_reconciliation_needed", return_value=True), \
             mock.patch.object(peer_steward, "_ensure_watch_observers") as ensure_watches, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "ensure_observers") as ensure_batches, \
             mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertEqual(peer_steward.cmd_ensure_obligations(
                SimpleNamespace(lifecycle_root=str(self.a))), 0)
        ensure_watches.assert_not_called()
        ensure_batches.assert_not_called()
        argv = launch.call_args.args[0]
        self.assertIn("__obligation-runner", argv)
        self.assertEqual(argv[-2:], ["--service-current", str(self.current)])

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_takeover_signals_only_an_exact_same_install_observer_holding_its_duty_lock(self):
        self._point_current(self.b)
        script = self.a / "utilities/peer-steward.py"
        ready = self.base / "legacy-observer-ready"
        script.write_text(
            "import fcntl, os, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(os.environ['FIXTURE_READY'], 'w').write('ready')\n"
            "time.sleep(30)\n", encoding="utf-8")
        lock_path = self.state / "watch.lease"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        required = ("__watch-run", "--watch-id", "watch-exact",
                    "--target", "fixture-target", "--server", "default",
                    "--expected-harness", "codex",
                    "--expected-session-id", "sid-watch")
        command = [sys.executable, str(script), *required, "--lock-fd", str(fd)]
        # The selected caller release may differ from the observer's script
        # root while both roots still belong to the same managed install.
        env = dict(self._environment(self.a), FIXTURE_READY=str(ready),
                   AGENT_HOME=str(self.b))
        process = subprocess.Popen(command, pass_fds=(fd,), env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        os.close(fd)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "legacy observer did not acquire its duty lock")
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self._environment(self.a), clear=True):
                binding = obligations.managed_service_binding(self.a)
                with mock.patch.object(contract, "pidfd_open",
                                       side_effect=OSError(errno.ENOSYS, "pidfd unavailable")):
                    self.assertFalse(obligations.replace_managed_observer(
                        process.pid, obligations._proc_start_identity(process.pid), lock_path,
                        "utilities/peer-steward.py", required, binding,
                        lock_fd_option="--lock-fd", root=self.a))
                with mock.patch.object(signal, "pidfd_send_signal", None):
                    self.assertFalse(obligations.replace_managed_observer(
                        process.pid, obligations._proc_start_identity(process.pid), lock_path,
                        "utilities/peer-steward.py", required, binding,
                        lock_fd_option="--lock-fd", root=self.a))
                self.assertIsNone(process.poll(), "unsupported pidfd must preserve the live duty")
                probe_fd = os.open(lock_path, os.O_RDONLY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe_fd)
                result = obligations.replace_managed_observer(
                    process.pid, obligations._proc_start_identity(process.pid), lock_path,
                    "utilities/peer-steward.py", required, binding,
                    lock_fd_option="--lock-fd", root=self.a)
            self.assertEqual(result, "replaced")
            self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_pid_only_old_completion_writer_resumes_under_its_exact_locked_duty(self):
        import dispatch_batch_obligations as batch

        from types import SimpleNamespace

        self._point_current(self.b)
        self.jobs.parent.mkdir(parents=True, exist_ok=True)
        self.jobs.write_text("fixture\n", encoding="utf-8")
        duty_args = SimpleNamespace(
            jobs=self.jobs, sealed_batch_id="sealed-pid-only",
            parent_attempt_id="att-parent", parent_session_id="sid-parent",
            attempt_id=["att-child"], delivery_parent_id="att-parent",
            control_socket=None, queue_socket=None, thread_id=None,
            interval=1.0, timeout=2.0,
        )
        duty_id, _created = batch.create(duty_args)
        outcome = {"state": "ready", "children": [
            {"attempt_id": "att-child", "status": "done"},
        ]}
        batch.update(self.jobs, duty_id, state="delivery-pending", outcome=outcome,
                     reason="observing")
        record_path = batch._root(self.jobs) / (duty_id + ".json")
        lock_path = batch._root(self.jobs) / (duty_id + ".lease")
        ready = self.base / "pid-only-old-writer-ready"
        old_writer = self.a / "utilities/codex-managed-completion.py"
        replacement = self.b / "utilities/codex-managed-completion.py"
        old_writer.write_text(
            "import fcntl, json, os, sys, time\n"
            "from pathlib import Path\n"
            "fd = int(sys.argv[sys.argv.index('--obligation-lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "record = Path(os.environ['FIXTURE_RECORD'])\n"
            "data = json.loads(record.read_text())\n"
            "data['observer'] = {'pid': os.getpid()}\n"
            "temp = record.with_suffix('.tmp')\n"
            "temp.write_text(json.dumps(data, sort_keys=True))\n"
            "os.replace(temp, record)\n"
            "Path(os.environ['FIXTURE_READY']).write_text('ready')\n"
            "while True: time.sleep(0.1)\n", encoding="utf-8")
        replacement.write_text("# isolated current completion observer target\n", encoding="utf-8")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        process = None
        try:
            old_env = dict(self._environment(self.a),
                           FIXTURE_RECORD=str(record_path), FIXTURE_READY=str(ready))
            process = subprocess.Popen(
                [sys.executable, str(old_writer), "--jobs", str(self.jobs),
                 "--resume-obligation", duty_id, "--obligation-lock-fd", str(lock_fd)],
                pass_fds=(lock_fd,), env=old_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            os.close(lock_fd)
            lock_fd = None
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "old writer did not persist its PID-only row")
            before = batch.read(self.jobs, duty_id)
            self.assertEqual(before["observer"], {"pid": process.pid})
            old_argv = (Path(f"/proc/{process.pid}/cmdline").read_bytes()
                        .decode().rstrip("\0").split("\0"))
            old_lock_fd = old_argv[old_argv.index("--obligation-lock-fd") + 1]
            self.assertTrue(obligations._process_holds_flock(
                process.pid, lock_path, old_lock_fd))

            class Launched:
                pid = 987654321

            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self._environment(self.a), clear=True):
                binding = obligations.managed_service_binding(self.a)
                with mock.patch.object(batch, "_pid_start", return_value="fixture-start"), \
                     mock.patch.object(batch.subprocess, "Popen", return_value=Launched()) as launch:
                    self.assertEqual(batch.ensure_observers(
                        self.jobs, service_current=binding), 1)
            self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
            after = batch.read(self.jobs, duty_id)
            self.assertEqual(after["observer"], {"pid": Launched.pid,
                                                  "pid_start": "fixture-start"})
            self.assertEqual(after["state"], "delivery-pending")
            for field in ("identity", "effective_attempts", "outcome", "delivery",
                          "recipient", "parent_attempt_id", "parent_session_id", "ack"):
                if field in before or field in after:
                    self.assertEqual(after.get(field), before.get(field), field)
            command = launch.call_args.args[0]
            self.assertEqual(Path(command[1]), replacement)
            self.assertEqual(command[command.index("--resume-obligation") + 1], duty_id)
            self.assertEqual(command[-2:], ["--service-current", binding])
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_watch_takeover_accepts_complete_initial_and_advanced_legacy_argv(self):
        self._point_current(self.b)
        cases = (
            (self._watch_arm(generation=1, rearm_count=4), 4, True),
            # v3.35.33 persisted generation/count after spawn; the live
            # generation-2 observer therefore carries rearm-count 1.
            (self._watch_arm(generation=2, rearm_count=1), 1, False),
            (self._watch_arm(generation=3, rearm_count=17), 2, False),
        )
        for index, (arm, accepted_generation, initial_layout) in enumerate(cases):
            with self.subTest(observer_generation=arm["observer_generation"]):
                # These arrays are copied from v3.35.33's cmd_watch and
                # _spawn_watch_observer layouts, not generated by the current
                # selector under test.
                actual = self._v33533_watch_argv(
                    arm["watch_id"], arm, rearm_count=accepted_generation,
                    initial=initial_layout)
                accepted = peer_steward._watch_observer_takeover_argvs(arm["watch_id"], arm)
                self.assertIn(actual, accepted)
                process, lock_path = self._start_fixture_watch(
                    self.a, actual, suffix=f"watch-positive-{index}")
                try:
                    start = obligations._proc_start_identity(process.pid)
                    self.assertTrue(obligations._process_holds_flock(
                        process.pid, lock_path, str(self._fixture_fd(process.pid))))
                    self.assertEqual(self._replace_fixture_watch(
                        process, lock_path, arm, accepted, stored_start=None), "replaced")
                    self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF and hasattr(os, "fork"),
                         "Linux pidfd and inherited flock handoff")
    def test_host_legacy_watch_without_agent_home_is_taken_over_under_inherited_lock(self):
        host_home = self.base / "legacy-host-home"
        host_data = host_home / ".local" / "share" / "hearting"
        host_releases = host_data / "releases"
        host_a = host_releases / "A"
        host_b = host_releases / "B"
        host_releases.mkdir(parents=True)
        shutil.copytree(self.a, host_a)
        shutil.copytree(self.b, host_b)
        current = host_data / "current"
        current.symlink_to(host_b, target_is_directory=True)

        host_state = host_home / ".local" / "state" / "hearting" / "dispatch"
        host_watch_root = host_state / "peer-watches"
        watch_id = "legacy-host-watch"
        arm = self._watch_arm(generation=2, rearm_count=1)
        arm["watch_id"] = watch_id
        actual_legacy_argv = self._v33533_watch_argv(
            watch_id, arm, rearm_count=1, initial=False)
        self.assertNotIn("--generation", actual_legacy_argv)

        caller_env = self._environment(host_a)
        caller_env.update({
            "HOME": str(host_home),
            "HARNESS_DATA_ROOT": str(host_data),
            "XDG_STATE_HOME": str(host_home / ".local" / "state"),
            "AGENT_PEER_LEDGER_ROOT": str(host_state),
        })
        legacy_env = {
            "HOME": str(host_home),
            "XDG_STATE_HOME": str(host_home / ".local" / "state"),
            "PATH": "/usr/bin:/bin",
        }
        for absent in ("AGENT_HOME", "CLAUDE_HOME", "HARNESS_DATA_ROOT", "XDG_DATA_HOME"):
            self.assertNotIn(absent, legacy_env)

        paths = peer_steward._watch_paths(watch_id, host_watch_root)
        paths.arm.parent.mkdir(parents=True, exist_ok=True)
        old_source = (
            "import sys, time\n"
            "int(sys.argv[sys.argv.index('--lock-fd') + 1])\n"
            "while True: time.sleep(0.1)\n"
        )
        self._fixture_watch_script(host_a, old_source)
        new_ready = self.base / "host-current-watch-ready"
        new_source = (
            "import fcntl, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            f"open({str(new_ready)!r}, 'w', encoding='utf-8').write('ready')\n"
            "while True: time.sleep(0.1)\n"
        )
        self._fixture_watch_script(host_b, new_source)

        process, stale_owner_pid, _old_ready = self._start_inherited_fixture_watch(
            host_a, actual_legacy_argv, lock_path=paths.lock,
            environment=legacy_env, suffix="host-legacy-watch")
        old_pid = process.pid
        old_start = obligations._proc_start_identity(old_pid)
        arm["watcher"] = {"pid": old_pid, "pid_start": old_start}
        paths.arm.write_text(json.dumps(arm, sort_keys=True), encoding="utf-8")

        spawned_current = []

        class CapturingSubprocess:
            def __getattr__(self, name):
                return getattr(subprocess, name)

            def Popen(self, *args, **kwargs):
                proc = subprocess.Popen(*args, **kwargs)
                spawned_current.append(proc)
                return proc

        def cleanup_fixture_watchers():
            latest = peer_steward._read_json(paths.arm) or {}
            current_pid = (latest.get("watcher") or {}).get("pid")
            for current_process in spawned_current:
                if current_process.poll() is None:
                    current_process.terminate()
                current_process.wait(timeout=5)
            for pid in {old_pid, current_pid}:
                if not isinstance(pid, int) or not obligations._proc_start_identity(pid):
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.wait(timeout=5)
            deadline = time.monotonic() + 5
            while current_pid and obligations._proc_start_identity(current_pid) \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
        self.addCleanup(cleanup_fixture_watchers)

        lock_stat = paths.lock.stat()
        old_fdinfo = Path(f"/proc/{old_pid}/fdinfo/5").read_text(encoding="ascii")
        flock = next(line.split() for line in old_fdinfo.splitlines()
                     if line.startswith("lock:"))
        self.assertEqual(flock[2], "FLOCK")
        # The kernel's FLOCK owner-PID projection varies after fork/exit. The
        # takeover proof relies on the live fd's exact open lock and inode.
        self.assertEqual(flock[6],
                         f"{os.major(lock_stat.st_dev):02x}:{os.minor(lock_stat.st_dev):02x}:"
                         f"{lock_stat.st_ino}")
        self.assertFalse(Path(f"/proc/{stale_owner_pid}").exists())
        self.assertEqual(Path(f"/proc/{old_pid}/fd/5").resolve(), paths.lock.resolve())
        self.assertTrue(obligations._process_holds_flock(old_pid, paths.lock, "5"))
        self.assertEqual(old_start, obligations._proc_start_identity(old_pid))

        accepted_argvs = peer_steward._watch_observer_takeover_argvs(watch_id, arm)
        self.assertIn(actual_legacy_argv, accepted_argvs)
        with mock.patch.object(obligations, "__file__",
                               str(host_a / "utilities/peer_obligations.py")), \
             mock.patch.object(peer_steward, "__file__",
                               str(host_a / "utilities/peer-steward.py")), \
             mock.patch.dict(os.environ, caller_env, clear=True), \
             mock.patch.object(peer_steward, "_settle_terminal_watch_if_unobserved",
                               return_value=False), \
             mock.patch.object(peer_steward.herdr_observation,
                               "socket_path_for_endpoint", return_value=None), \
             mock.patch.object(peer_steward, "_watch_terminal_from_view", return_value=None), \
             mock.patch.object(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward, "subprocess", CapturingSubprocess()):
            binding = obligations.managed_service_binding(host_a)
            self.assertEqual(binding, str(current))
            peer_steward._ensure_watch_observers(service_current=binding)

        self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
        updated_arm = peer_steward._read_json(paths.arm)
        new_pid = (updated_arm.get("watcher") or {}).get("pid")
        self.assertIsInstance(new_pid, int)
        deadline = time.monotonic() + 5
        while not new_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(new_ready.exists(), "current release was not started after lock handoff")
        self.assertFalse(obligations._proc_start_identity(old_pid))
        self.assertEqual(Path(f"/proc/{new_pid}/cmdline").read_bytes()
                         .decode().rstrip("\0").split("\0")[1],
                         str(host_b / "utilities/peer-steward.py"))
        self.assertTrue(obligations._process_holds_flock(
            new_pid, paths.lock, self._fixture_fd(new_pid)))
        new_fd = self._fixture_fd(new_pid)
        new_fdinfo = Path(f"/proc/{new_pid}/fdinfo/{new_fd}").read_text(encoding="ascii")
        new_flock = next(line.split() for line in new_fdinfo.splitlines()
                         if line.startswith("lock:"))
        self.assertEqual(new_flock[6], flock[6], "handoff changed the locked inode")
        self.assertEqual((updated_arm["watch_id"], updated_arm["target"],
                          updated_arm["server"], updated_arm["agent"],
                          updated_arm["steward"], updated_arm["armed_ts"],
                          updated_arm["until"], updated_arm["refs"]),
                         (arm["watch_id"], arm["target"], arm["server"], arm["agent"],
                          arm["steward"], arm["armed_ts"], arm["until"], arm["refs"]))
        self.assertEqual(updated_arm["observer_generation"], 3)
        self.assertEqual(updated_arm["rearm_count"], 2)
        self.assertFalse(paths.receipt.exists(), "observer replacement must not settle the watch")
        self.assertFalse(paths.ack.exists(), "observer replacement must not create an ACK")
        probe = os.open(paths.lock, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_managed_runner_reuses_external_discovery_for_real_legacy_process_census(self):
        import dispatch_batch_obligations as batch

        watch_root = self.base / "watch-census"
        watch_root.mkdir()
        ready_root = self.base / "observer-ready"
        ready_root.mkdir()
        fixture_source = (
            "import fcntl, os, sys, time\n"
            "if '--lock-fd' in sys.argv:\n"
            "    fd = int(sys.argv[sys.argv.index('--lock-fd') + 1])\n"
            "    kind = 'watch'\n"
            "    identity = sys.argv[sys.argv.index('--watch-id') + 1]\n"
            "else:\n"
            "    fd = int(sys.argv[sys.argv.index('--obligation-lock-fd') + 1])\n"
            "    kind = 'batch'\n"
            "    identity = sys.argv[sys.argv.index('--resume-obligation') + 1]\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "ready = os.environ.get('FIXTURE_READY') or os.path.join(os.environ['FIXTURE_READY_ROOT'], kind + '-' + identity + '-' + str(os.getpid()))\n"
            "open(ready, 'w').close()\n"
            "while True: time.sleep(0.2)\n"
        )
        for release in (self.a, self.b):
            (release / "utilities/peer-steward.py").write_text(fixture_source, encoding="utf-8")
            (release / "utilities/codex-managed-completion.py").write_text(
                fixture_source, encoding="utf-8")
        self._point_current(self.b)

        # Retain the prior measurement's large registry input and model sixteen
        # simultaneously live duties: nine watch owners and seven completion
        # observers. The fixture processes hold real FLOCKs in this PID namespace.
        self.jobs.parent.mkdir(parents=True, exist_ok=True)
        self.jobs.write_text("x" * 23_000_000, encoding="utf-8")
        env = dict(self._environment(self.a), FIXTURE_READY_ROOT=str(ready_root))
        old_processes = []
        current_processes = []
        lock_paths = []

        def cleanup_fixture_processes():
            for process in (*old_processes, *current_processes):
                if process.poll() is None:
                    process.terminate()
            for process in (*old_processes, *current_processes):
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        self.addCleanup(cleanup_fixture_processes)

        def start_old(script, args, lock_path, lock_option, ready_path):
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            command_args = list(args)
            if lock_option == "--lock-fd":
                repeated = {"--until", "--ref", "--timeout", "--rearmed-from"}
                index = next((i for i, value in enumerate(command_args) if value in repeated),
                             len(command_args))
            else:
                index = len(command_args)
            command_args[index:index] = [lock_option, str(fd)]
            process = subprocess.Popen(
                [sys.executable, str(script), *command_args], pass_fds=(fd,),
                env=dict(env, FIXTURE_READY=str(ready_path)),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.close(fd)
            deadline = time.monotonic() + 5
            while not ready_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready_path.exists():
                process.terminate()
                process.wait(timeout=5)
                self.fail(f"fixture observer did not acquire its lock: {ready_path.name}")
            old_processes.append(process)
            lock_paths.append(lock_path)
            return process

        watches = []
        for index in range(9):
            watch_id = f"watch-legacy-{index}"
            arm = self._watch_arm(generation=3, rearm_count=17)
            arm["watch_id"] = watch_id
            lock_path = watch_root / f"{watch_id}.lock"
            actual = self._v33533_watch_argv(watch_id, arm, rearm_count=2, initial=False)
            process = start_old(
                self.a / "utilities/peer-steward.py", actual, lock_path, "--lock-fd",
                ready_root / f"watch-{watch_id}-PLACEHOLDER")
            # The process name includes its PID; wait by the stable prefix.
            self.assertTrue(any(ready_root.glob(f"watch-{watch_id}-*")))
            arm["watcher"] = {
                "pid": process.pid,
                "pid_start": obligations._proc_start_identity(process.pid),
            }
            (watch_root / f"{watch_id}.json").write_text(
                json.dumps(arm), encoding="utf-8")
            watches.append((watch_id, lock_path, process))

        batches = []
        for index in range(7):
            args = SimpleNamespace(
                jobs=self.jobs, sealed_batch_id=f"sealed-legacy-{index}",
                parent_attempt_id="att-parent", parent_session_id=None,
                attempt_id=[f"att-legacy-{index}"], delivery_parent_id="att-parent",
                control_socket=None, queue_socket=None, thread_id=None,
                interval=1.0, timeout=2.0,
            )
            duty_id, _record = batch.create(args)
            state_root = batch._root(self.jobs)
            lock_path = state_root / f"{duty_id}.lease"
            old_args = ["--jobs", str(self.jobs), "--resume-obligation", duty_id]
            process = start_old(
                self.a / "utilities/codex-managed-completion.py", old_args, lock_path,
                "--obligation-lock-fd", ready_root / f"batch-{duty_id}-PLACEHOLDER")
            batch.update(self.jobs, duty_id, observer={
                "pid": process.pid,
                "pid_start": obligations._proc_start_identity(process.pid),
            })
            batches.append((duty_id, lock_path, process))

        store = obligations.ObligationStore(self.state)
        runner_lock = store.root / obligations.RUNNER_LOCK_NAME
        runner_lock.parent.mkdir(parents=True, exist_ok=True)
        runner_fd = os.open(runner_lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(runner_fd, fcntl.LOCK_EX)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, env, clear=True):
            binding = obligations.managed_service_binding(self.a)
        self.assertEqual(binding, str(self.current))

        stop_at = time.monotonic() + 20.0
        real_sleep = time.sleep
        real_popen = subprocess.Popen
        watch_scans = []
        batch_scans = []
        registry_reads = []
        passes = []
        original_watch_entries = peer_steward._watch_entries
        original_batch_records = batch._read_only_records
        original_reconcile = peer_steward._reconcile_external_obligations
        original_read_text = Path.read_text

        def count_watch_scan(root):
            watch_scans.append(root)
            return original_watch_entries(root)

        def count_batch_scan(path):
            batch_scans.append(path)
            return original_batch_records(path)

        def count_registry_read(path, *args, **kwargs):
            if path.resolve(strict=False) == self.jobs.resolve(strict=False):
                registry_reads.append(path)
            return original_read_text(path, *args, **kwargs)

        def count_reconcile(*args, **kwargs):
            passes.append(1)
            return original_reconcile(*args, **kwargs)

        class StopRunnerWindow(Exception):
            pass

        def measured_sleep(delay):
            if delay < 0.5:
                return real_sleep(delay)
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                raise StopRunnerWindow()
            real_sleep(min(delay, remaining))
            if time.monotonic() >= stop_at:
                raise StopRunnerWindow()

        def track_current(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            current_processes.append(process)
            return process

        def process_ticks():
            raw = Path(f"/proc/{os.getpid()}/stat").read_text()
            return raw[raw.rfind(")") + 2:].split()

        cpu_fields = process_ticks()
        start_tick = cpu_fields[19]
        cpu_before = int(cpu_fields[11]) + int(cpu_fields[12])
        started = time.monotonic()
        try:
            with mock.patch.object(peer_steward, "_watch_root", return_value=watch_root), \
                 mock.patch.object(peer_steward, "_watch_entries", side_effect=count_watch_scan), \
                 mock.patch.object(peer_steward, "_herdr_missing", return_value=False), \
                 mock.patch.object(peer_steward.herdr_observation,
                                   "socket_path_for_endpoint", return_value=None), \
                 mock.patch.object(peer_steward, "_watch_terminal_from_view", return_value=None), \
                 mock.patch.object(batch, "_jobs_paths",
                                   side_effect=lambda jobs=None, strict=True: [
                                       Path(jobs).resolve(strict=False) if jobs else self.jobs.resolve()
                                   ]), \
                 mock.patch.object(batch, "_read_only_records", side_effect=count_batch_scan), \
                 mock.patch.object(peer_steward, "_reconcile_external_obligations",
                                   side_effect=count_reconcile), \
                 mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.object(obligations, "exec_service_runner", return_value=False), \
                 mock.patch.object(Path, "read_text", count_registry_read), \
                 mock.patch.object(subprocess, "Popen", side_effect=track_current), \
                 mock.patch.object(peer_steward.time, "sleep", side_effect=measured_sleep):
                try:
                    peer_steward.cmd_obligation_runner(SimpleNamespace(
                        state_root=self.state, lock_fd=runner_fd,
                        service_current=binding))
                except StopRunnerWindow:
                    pass
        finally:
            old_exit_codes = [process.poll() for process in old_processes]
            for process in (*old_processes, *current_processes):
                if process.poll() is None:
                    process.terminate()
            for process in (*old_processes, *current_processes):
                process.wait(timeout=5)
            fcntl.flock(runner_fd, fcntl.LOCK_UN)
            os.close(runner_fd)

        elapsed = time.monotonic() - started
        cpu_fields = process_ticks()
        self.assertEqual(cpu_fields[19], start_tick)
        cpu_after = int(cpu_fields[11]) + int(cpu_fields[12])
        hz = os.sysconf("SC_CLK_TCK")
        cpu_percent = (cpu_after - cpu_before) * 100.0 / (hz * elapsed)
        self.assertGreaterEqual(elapsed, 19.0)
        self.assertGreaterEqual(len(passes), 3)
        self.assertEqual(len(current_processes), 16)
        self.assertGreaterEqual(len(watch_scans), len(passes))
        self.assertLessEqual(len(watch_scans), len(passes) + 1)
        self.assertEqual(len(batch_scans), len(watch_scans))
        self.assertLessEqual(len(registry_reads), 1)
        print(
            f"runner_idle_window=20s one_core=100% child_cpu=excluded "
            f"cpu={cpu_percent:.3f}% passes={len(passes)} "
            f"watch_scans={len(watch_scans)} batch_scans={len(batch_scans)} "
            f"jobs_registry_reads={len(registry_reads)} live_legacy=16"
        )
        self.assertLess(cpu_percent, 15.0,
                        f"steady runner consumed {cpu_percent:.2f}% of one core")
        self.assertEqual(old_exit_codes, [-signal.SIGTERM] * 16)

    def _fixture_fd(self, pid):
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode().rstrip("\0").split("\0")
        return argv[argv.index("--lock-fd") + 1]

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_watch_takeover_leaves_every_full_duty_mismatch_untouched(self):
        self._point_current(self.b)
        arm = self._watch_arm(generation=3, rearm_count=17)
        accepted = peer_steward._watch_observer_takeover_argvs(arm["watch_id"], arm)
        actual = peer_steward._watch_observer_duty_argv(
            arm["watch_id"], arm, rearm_generation=2, include_rearmed_from=False)

        def set_value(argv, option, value, occurrence=0):
            result = list(argv)
            positions = [i for i, item in enumerate(result[:-1]) if item == option]
            result[positions[occurrence] + 1] = value
            return result

        def swap_repeated(argv, option):
            result = list(argv)
            positions = [i for i, item in enumerate(result[:-1]) if item == option]
            result[positions[0] + 1], result[positions[1] + 1] = (
                result[positions[1] + 1], result[positions[0] + 1])
            return result

        def remove_option(argv, option):
            result = list(argv)
            index = result.index(option)
            del result[index:index + 2]
            return result

        mutations = (
            ("watch-id", lambda a: set_value(a, "--watch-id", "other-watch")),
            ("target", lambda a: set_value(a, "--target", "other-target")),
            ("server", lambda a: set_value(a, "--server", "other-server")),
            ("harness", lambda a: set_value(a, "--expected-harness", "claude")),
            ("session", lambda a: set_value(a, "--expected-session-id", "other-sid")),
            ("pane", lambda a: set_value(a, "--expected-pane", "w1:p9-other")),
            ("same-pane-different-duty", lambda a: set_value(a, "--ref", "other-ref", 0)),
            ("steward-harness", lambda a: set_value(a, "--steward-harness", "codex")),
            ("steward-session", lambda a: set_value(a, "--steward-session-id", "other-steward")),
            ("steward-project", lambda a: set_value(a, "--steward-project", "/other/project")),
            ("armed-time", lambda a: set_value(a, "--armed-ts", "2026-10-18T01:02:04Z")),
            ("stale-arm-generation", lambda a: set_value(a, "--rearm-count", "1")),
            ("until-value", lambda a: set_value(a, "--until", "working", 0)),
            ("until-order", lambda a: swap_repeated(a, "--until")),
            ("until-extra", lambda a: [*a, "--until", "blocked"]),
            ("ref-value", lambda a: set_value(a, "--ref", "other-ref", 0)),
            ("ref-order", lambda a: swap_repeated(a, "--ref")),
            ("ref-extra", lambda a: [*a, "--ref", "attempt-two"]),
            ("timeout-value", lambda a: set_value(a, "--timeout", "86401")),
            ("timeout-omitted", lambda a: remove_option(a, "--timeout")),
            ("unexpected-option", lambda a: [*a, "--unrecognized-duty", "value"]),
            ("unexpected-value", lambda a: [*a, "stray-value"]),
            ("duplicate-scalar", lambda a: [*a, "--target", "fixture-target"]),
            ("duplicate-lock-control", lambda a: [*a, "--lock-fd", "999"]),
            ("unvalidated-service-control", lambda a: [*a, "--service-current", "/wrong/current"]),
        )
        for index, (name, mutate) in enumerate(mutations):
            with self.subTest(mismatch=name):
                process, lock_path = self._start_fixture_watch(
                    self.a, mutate(actual), suffix=f"watch-mismatch-{index}")
                try:
                    start = obligations._proc_start_identity(process.pid)
                    fd_number = self._fixture_fd(process.pid)
                    self.assertTrue(obligations._process_holds_flock(
                        process.pid, lock_path, fd_number))
                    self.assertFalse(self._replace_fixture_watch(
                        process, lock_path, arm, accepted, stored_start=None))
                    self.assertIsNone(process.poll(), "mismatched duty must remain live")
                    self.assertEqual(start, obligations._proc_start_identity(process.pid))
                    self.assertTrue(obligations._process_holds_flock(
                        process.pid, lock_path, fd_number))
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_watch_takeover_preserves_fixed_and_foreign_process_roots(self):
        self._point_current(self.b)
        arm = self._watch_arm(generation=3)
        accepted = peer_steward._watch_observer_takeover_argvs(arm["watch_id"], arm)
        actual = peer_steward._watch_observer_duty_argv(
            arm["watch_id"], arm, rearm_generation=2, include_rearmed_from=False)

        checkout = self.base / "checkout"
        shutil.copytree(self.a, checkout)
        foreign_data = self.base / "foreign-data"
        foreign_releases = foreign_data / "releases"
        foreign_releases.mkdir(parents=True)
        foreign = foreign_releases / "Foreign"
        shutil.copytree(self.a, foreign)
        (foreign_data / "current").symlink_to(foreign, target_is_directory=True)
        for name, source_root, extra_env, remove_env in (
                ("checkout", checkout, None, ()),
                ("foreign", foreign, {"HARNESS_DATA_ROOT": str(foreign_data)}, ()),
                ("foreign-data-root", self.a,
                 {"HARNESS_DATA_ROOT": str(foreign_data)}, ()),
                ("foreign-agent-home", self.a, {"AGENT_HOME": str(checkout)}, ()),
                ("empty-agent-home", self.a, {"AGENT_HOME": ""}, ()),
                ("empty-claude-home", self.a, {"CLAUDE_HOME": ""}, ()),
                ("contradictory-agent-homes", self.a,
                 {"CLAUDE_HOME": str(checkout)}, ()),
                ("missing-default-data-root-proof", self.a, None,
                 ("HOME", "AGENT_HOME", "CLAUDE_HOME", "HARNESS_DATA_ROOT", "XDG_DATA_HOME"))):
            with self.subTest(root=name):
                process, lock_path = self._start_fixture_watch(
                    source_root, actual, suffix=f"watch-root-{name}", extra_env=extra_env,
                    remove_env=remove_env)
                try:
                    start = obligations._proc_start_identity(process.pid)
                    fd_number = self._fixture_fd(process.pid)
                    self.assertTrue(obligations._process_holds_flock(
                        process.pid, lock_path, fd_number))
                    self.assertFalse(self._replace_fixture_watch(
                        process, lock_path, arm, accepted, stored_start=None))
                    self.assertIsNone(process.poll())
                    self.assertEqual(start, obligations._proc_start_identity(process.pid))
                    self.assertTrue(obligations._process_holds_flock(
                        process.pid, lock_path, fd_number))
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_takeover_keeps_stale_identity_and_unlocked_observers_untouched(self):
        self._point_current(self.b)
        arm = self._watch_arm(generation=3, rearm_count=17)
        accepted = peer_steward._watch_observer_takeover_argvs(arm["watch_id"], arm)
        actual = peer_steward._watch_observer_duty_argv(
            arm["watch_id"], arm, rearm_generation=2, include_rearmed_from=False)

        def replace(process, lock_path, *, expected_start=Ellipsis, requested_lock=None,
                    missing_start=False):
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self._environment(self.a), clear=True):
                binding = obligations.managed_service_binding(self.a)
                if missing_start:
                    expected_start = None
                elif expected_start is Ellipsis:
                    expected_start = obligations._proc_start_identity(process.pid)
                return obligations.replace_managed_observer(
                    process.pid, expected_start,
                    requested_lock or lock_path, "utilities/peer-steward.py",
                    accepted[0], binding, lock_fd_option="--lock-fd",
                    exact_arguments=True, argument_variants=accepted[1:], root=self.a)

        process, lock_path = self._start_fixture_watch(
            self.a, actual, suffix="watch-stale-start")
        wrong_lock = lock_path.with_name("watch-other.lock")
        wrong_lock.touch()
        try:
            start = obligations._proc_start_identity(process.pid)
            self.assertFalse(replace(process, lock_path, expected_start=str(int(start) + 1)))
            self.assertFalse(replace(process, lock_path, requested_lock=wrong_lock,
                                     missing_start=True))
            self.assertFalse(replace(process, lock_path, expected_start="malformed-start"))
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self._environment(self.a), clear=True):
                binding = obligations.managed_service_binding(self.a)
                self.assertFalse(obligations.replace_managed_observer(
                    2**30, None, lock_path, "utilities/peer-steward.py", accepted[0],
                    binding, lock_fd_option="--lock-fd", exact_arguments=True,
                    argument_variants=accepted[1:], root=self.a))
            self.assertIsNone(process.poll())
            self.assertEqual(obligations._proc_start_identity(process.pid), start)
            self.assertTrue(obligations._process_holds_flock(
                process.pid, lock_path, self._fixture_fd(process.pid)))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

        no_flock_source = (
            "import os, sys, time\n"
            "open(os.environ['FIXTURE_READY'], 'w').write('ready')\n"
            "time.sleep(30)\n"
        )
        process, lock_path = self._start_fixture_watch(
            self.a, actual, suffix="watch-no-flock", program_source=no_flock_source)
        try:
            self.assertFalse(replace(process, lock_path, missing_start=True))
            self.assertIsNone(process.poll())
            self.assertFalse(obligations._process_holds_flock(
                process.pid, lock_path, self._fixture_fd(process.pid)))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_watch_takeover_keeps_exact_current_observer(self):
        self._point_current(self.b)
        arm = self._watch_arm(generation=3)
        accepted = peer_steward._watch_observer_takeover_argvs(arm["watch_id"], arm)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self._environment(self.a), clear=True):
            binding = obligations.managed_service_binding(self.a)
        actual = peer_steward._watch_observer_duty_argv(
            arm["watch_id"], arm, rearm_generation=2, service_current=binding)
        process, lock_path = self._start_fixture_watch(self.b, actual, suffix="watch-current")
        try:
            start = obligations._proc_start_identity(process.pid)
            fd_number = self._fixture_fd(process.pid)
            self.assertEqual(self._replace_fixture_watch(
                process, lock_path, arm, accepted), "current")
            self.assertIsNone(process.poll())
            self.assertEqual(start, obligations._proc_start_identity(process.pid))
            self.assertTrue(obligations._process_holds_flock(process.pid, lock_path, fd_number))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_pre_signal_reinspection_keeps_same_pid_current_after_self_exec(self):
        self._point_current(self.b)
        arm = self._watch_arm(generation=3)
        accepted = peer_steward._watch_observer_takeover_argvs(arm["watch_id"], arm)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self._environment(self.a), clear=True):
            binding = obligations.managed_service_binding(self.a)
        trigger = self.base / "exec-trigger"
        exec_ready = self.base / "exec-ready"
        source = (
            "import fcntl, os, sys, time\n"
            "from pathlib import Path\n"
            "fd = int(sys.argv[sys.argv.index('--lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "Path(os.environ['FIXTURE_READY']).write_text('ready')\n"
            "target = os.environ.get('FIXTURE_EXEC_TO')\n"
            "if target:\n"
            "    gate = Path(os.environ['FIXTURE_EXEC_TRIGGER'])\n"
            "    while not gate.exists(): time.sleep(0.005)\n"
            "    env = dict(os.environ)\n"
            "    env.pop('FIXTURE_EXEC_TO', None)\n"
            "    env['AGENT_HOME'] = os.environ['FIXTURE_EXEC_AGENT_HOME']\n"
            "    env['FIXTURE_READY'] = os.environ['FIXTURE_EXEC_READY']\n"
            "    os.execve(sys.executable, [sys.executable, target, *sys.argv[1:]], env)\n"
            "while True: time.sleep(0.1)\n"
        )
        self._fixture_watch_script(self.b, source)
        actual = peer_steward._watch_observer_duty_argv(
            arm["watch_id"], arm, rearm_generation=2, service_current=binding)
        process, lock_path = self._start_fixture_watch(
            self.a, actual, suffix="watch-self-exec", program_source=source,
            extra_env={"FIXTURE_EXEC_TO": str(self.b / "utilities/peer-steward.py"),
                       "FIXTURE_EXEC_TRIGGER": str(trigger),
                       "FIXTURE_EXEC_READY": str(exec_ready),
                       "FIXTURE_EXEC_AGENT_HOME": str(self.b)})
        pid_start = obligations._proc_start_identity(process.pid)
        real_start_identity = obligations._proc_start_identity
        inspections = 0

        def exec_at_recheck(pid):
            nonlocal inspections
            if pid == process.pid:
                inspections += 1
                if inspections == 2:
                    trigger.touch()
                    deadline = time.monotonic() + 5
                    while not exec_ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.005)
                    if not exec_ready.exists():
                        raise AssertionError("observer did not self-exec to current release")
            return real_start_identity(pid)

        try:
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self._environment(self.a), clear=True), \
                 mock.patch.object(obligations, "_proc_start_identity",
                                  side_effect=exec_at_recheck):
                result = obligations.replace_managed_observer(
                    process.pid, pid_start, lock_path, "utilities/peer-steward.py",
                    accepted[0], binding, lock_fd_option="--lock-fd",
                    exact_arguments=True, argument_variants=accepted[1:], root=self.a)
            self.assertEqual(result, "current")
            self.assertEqual(obligations._proc_start_identity(process.pid), pid_start)
            argv = Path(f"/proc/{process.pid}/cmdline").read_bytes().decode().rstrip("\0").split("\0")
            self.assertEqual(Path(argv[1]), self.b / "utilities/peer-steward.py")
            self.assertIsNone(process.poll(), "current process must not receive SIGTERM")
            self.assertTrue(obligations._process_holds_flock(
                process.pid, lock_path, self._fixture_fd(process.pid)))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_pre_signal_reinspection_uses_new_current_release_selection(self):
        self._point_current(self.a)
        arm = self._watch_arm(generation=3)
        accepted = peer_steward._watch_observer_takeover_argvs(arm["watch_id"], arm)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self._environment(self.a), clear=True):
            binding = obligations.managed_service_binding(self.a)
        actual = peer_steward._watch_observer_duty_argv(
            arm["watch_id"], arm, rearm_generation=2, service_current=binding)
        process, lock_path = self._start_fixture_watch(
            self.b, actual, suffix="watch-selection-change")
        pid_start = obligations._proc_start_identity(process.pid)
        real_validate = obligations.validate_service_binding
        validations = 0

        def update_current_on_recheck(service_current, *, root=None):
            nonlocal validations
            validations += 1
            if validations == 3:
                self._point_current(self.b)
            return real_validate(service_current, root=root)

        try:
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self._environment(self.a), clear=True), \
                 mock.patch.object(obligations, "validate_service_binding",
                                  side_effect=update_current_on_recheck):
                result = obligations.replace_managed_observer(
                    process.pid, pid_start, lock_path, "utilities/peer-steward.py",
                    accepted[0], binding, lock_fd_option="--lock-fd",
                    exact_arguments=True, argument_variants=accepted[1:], root=self.a)
            self.assertEqual(result, "current")
            self.assertEqual(obligations._proc_start_identity(process.pid), pid_start)
            self.assertIsNone(process.poll(), "newly current process must not receive SIGTERM")
            self.assertTrue(obligations._process_holds_flock(
                process.pid, lock_path, self._fixture_fd(process.pid)))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_lifecycle_replaces_exact_legacy_runner_and_starts_current_under_same_lock(self):
        self._point_current(self.b)
        store = obligations.ObligationStore(self.state)
        duty = store.create("legacy-runner-duty", "message", {"fixture": "legacy"},
                            {"ref": "a" * 32})
        script = self.a / "utilities/peer-steward.py"
        ready = self.base / "legacy-runner-ready"
        script.write_text(
            "import fcntl, os, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(os.environ['FIXTURE_READY'], 'w').write('ready')\n"
            "time.sleep(30)\n", encoding="utf-8")
        lock_path = store.root / obligations.RUNNER_LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        state_root = str(store.root.parents[1])
        process = subprocess.Popen(
            [sys.executable, str(script), "__obligation-runner", "--lock-fd", str(fd),
             "--state-root", state_root], pass_fds=(fd,),
            env=dict(self._environment(self.a), FIXTURE_READY=str(ready)),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(fd)
        launched = {}

        def capture_launch(*args, **kwargs):
            lock_fd = kwargs["pass_fds"][0]
            launched["lock_path"] = Path(os.path.realpath(f"/proc/self/fd/{lock_fd}"))
            return SimpleNamespace(poll=lambda: None)

        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "legacy runner did not acquire runner-current.lock")
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self._environment(self.a), clear=True), \
                 mock.patch.object(obligations, "_RUNNERS", []), \
                 mock.patch.object(obligations.subprocess, "Popen",
                                   side_effect=capture_launch) as launch:
                self.assertTrue(obligations.ensure_normal_runner(
                    self.state, lifecycle_root=self.a, reconciliation_required=True))
            self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
            argv = launch.call_args.args[0]
            self.assertEqual(Path(argv[1]), self.b / "utilities/peer-steward.py")
            self.assertEqual(argv[-2:], ["--service-current", str(self.current)])
            self.assertEqual(launch.call_args.kwargs["env"]["AGENT_HOME"], str(self.b))
            self.assertEqual(launched["lock_path"], lock_path.resolve())
            self.assertEqual(store.get(duty["id"]), duty)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_update_takes_over_live_batch_and_old_caller_cannot_duplicate_its_lease(self):
        import dispatch_batch_obligations as batch

        self._point_current(self.b)
        self.jobs.parent.mkdir(parents=True, exist_ok=True)
        self.jobs.write_text(
            "t\tdone\t/repo\t/wt\tchild\tattempt_schema_version=2,attempt_id=att-child,"
            "parent_sid=sid-parent,dispatch_depth=1,execution_surface=registered-headless,"
            "registered_worker=1,launch_claimed=1\n", encoding="utf-8")
        args = SimpleNamespace(jobs=self.jobs, sealed_batch_id="sealed-fixture",
                               parent_attempt_id="att-parent", parent_session_id=None,
                               attempt_id=["att-child"], control_socket=None,
                               queue_socket=None, thread_id=None, interval=1.0, timeout=2.0)
        duty_id, _created = batch.create(args)
        batch.update(self.jobs, duty_id, state="delivery-pending", reason="observing",
                     outcome={"state": "ready", "children": [{"attempt_id": "att-child"}]})
        before = batch.read(self.jobs, duty_id)
        jobs_digest = __import__("hashlib").sha256(self.jobs.read_bytes()).hexdigest()
        state_root = batch._root(self.jobs)
        lease_path = state_root / (duty_id + ".lease")
        old_ready = self.base / "old-batch-ready"
        current_ready = self.base / "current-batch-ready"
        observer_source = (
            "import fcntl, os, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--obligation-lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(os.environ['FIXTURE_READY'], 'w').write('ready')\n"
            "time.sleep(30)\n")
        old_script = self.a / "utilities/codex-managed-completion.py"
        current_script = self.b / "utilities/codex-managed-completion.py"
        old_script.write_text(observer_source, encoding="utf-8")
        current_script.write_text(
            "import os, sys, time\n"
            "open(os.environ['FIXTURE_READY'], 'w').write('ready')\n"
            "time.sleep(30)\n", encoding="utf-8")
        fd = os.open(lease_path, os.O_CREAT | os.O_RDWR, 0o600)
        old_command = [sys.executable, str(old_script), "--jobs", str(self.jobs),
                       "--resume-obligation", duty_id, "--obligation-lock-fd", str(fd)]
        old_environment = dict(self._environment(self.a), FIXTURE_READY=str(old_ready))
        old_process = subprocess.Popen(
            old_command, pass_fds=(fd,), env=old_environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(fd)
        current_process = None
        real_popen = subprocess.Popen
        captured = {}

        def launch_current(*command_args, **kwargs):
            command = command_args[0]
            captured["command"] = list(command)
            captured["environment"] = dict(kwargs["env"])
            captured["lease_path"] = Path(os.path.realpath(
                f"/proc/self/fd/{kwargs['pass_fds'][0]}"))
            nonlocal current_process
            current_process = real_popen(
                command, **kwargs)
            return current_process

        try:
            deadline = time.monotonic() + 5
            while not old_ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(old_ready.exists(), "legacy batch observer did not acquire its lease")
            batch.update(self.jobs, duty_id, observer={
                "pid": old_process.pid, "pid_start": batch._pid_start(old_process.pid)})
            before = batch.read(self.jobs, duty_id)
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ,
                                 dict(self._environment(self.a), FIXTURE_READY=str(current_ready)),
                                 clear=True), \
                 mock.patch.object(obligations, "_RUNNERS", []), \
                 mock.patch.object(batch.subprocess, "Popen", side_effect=launch_current):
                binding = obligations.managed_service_binding(self.a)
                self.assertEqual(batch.ensure_observers(
                    self.jobs, service_current=binding), 1)
            self.assertEqual(old_process.wait(timeout=5), -signal.SIGTERM)
            deadline = time.monotonic() + 5
            while not current_ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(current_ready.exists(), "current batch observer did not acquire its lease")
            self.assertEqual(Path(captured["command"][1]), current_script)
            self.assertEqual(captured["command"][-2:], ["--service-current", str(self.current)])
            self.assertEqual(captured["environment"]["AGENT_HOME"], str(self.b))
            self.assertEqual(captured["lease_path"], lease_path.resolve())

            # The old caller's existing lease-first path cannot spawn a sibling
            # while the current observer owns the same duty.
            def old_caller_ensure():
                inherited = batch.acquire(self.jobs, duty_id)
                if inherited is None:
                    return 0
                try:
                    os.set_inheritable(inherited, True)
                    subprocess.Popen(
                        [sys.executable, str(old_script), "--jobs", str(self.jobs),
                         "--resume-obligation", duty_id,
                         "--obligation-lock-fd", str(inherited)],
                        pass_fds=(inherited,))
                    return 1
                finally:
                    os.close(inherited)

            with mock.patch.object(batch.subprocess, "Popen") as old_launch:
                self.assertEqual(old_caller_ensure(), 0)
            old_launch.assert_not_called()
            after = batch.read(self.jobs, duty_id)
            for field in ("identity", "arguments", "outcome", "state", "delivery"):
                self.assertEqual(after[field], before[field], field)
            self.assertEqual(hashlib.sha256(self.jobs.read_bytes()).hexdigest(), jobs_digest)
        finally:
            if current_process is not None and current_process.poll() is None:
                current_process.terminate()
            if current_process is not None:
                current_process.wait(timeout=5)
            if old_process.poll() is None:
                old_process.terminate()
            old_process.wait(timeout=5)

    @unittest.skipUnless(_PIDFD_HANDOFF, "pidfd handoff unavailable")
    def test_base_source_finally_respawn_is_reconciled_in_both_activation_orders(self):
        import contextlib
        import dispatch_batch_obligations as batch

        base_commit = "414a76bec4963e3e22d6f89766282cdf5f6b739b"
        old_batch_source = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT}", "show",
             f"{base_commit}:utilities/dispatch_batch_obligations.py"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        old_completion_source = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT}", "show",
             f"{base_commit}:utilities/codex-managed-completion.py"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout

        self._point_current(self.b)
        self.jobs.parent.mkdir(parents=True, exist_ok=True)
        self.jobs.write_bytes(b"")
        args = SimpleNamespace(
            jobs=self.jobs, sealed_batch_id="sealed-old-caller-fixture",
            parent_attempt_id="att-parent", parent_session_id=None,
            attempt_id=["att-child"], control_socket=None, queue_socket=None,
            thread_id=None, interval=1.0, timeout=2.0,
        )
        duty_id, _ = batch.create(args)
        batch.update(self.jobs, duty_id, state="delivery-pending", reason="observing",
                     outcome={"state": "done", "attempt_ids": ["att-child"]},
                     delivery="pending")
        before = batch.read(self.jobs, duty_id)
        jobs_digest = hashlib.sha256(self.jobs.read_bytes()).hexdigest()
        lease_path = batch._root(self.jobs) / (duty_id + ".lease")

        old_ready = [self.base / "old-first-ready", self.base / "old-after-release-ready"]
        current_ready = [self.base / "current-activation-ready", self.base / "current-reconcile-ready"]
        old_script = self.a / "utilities/codex-managed-completion.py"
        current_script = self.b / "utilities/codex-managed-completion.py"
        old_script.write_text(
            "import fcntl, os, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--obligation-lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(os.environ['FIXTURE_READY'], 'w').write(str(os.getpid()))\n"
            "while True: time.sleep(60)\n", encoding="utf-8")
        current_script.write_text(
            "import fcntl, os, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--obligation-lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(os.environ['FIXTURE_READY'], 'w').write(str(os.getpid()))\n"
            "while True: time.sleep(60)\n", encoding="utf-8")
        (self.a / "utilities/dispatch_batch_obligations.py").write_text(
            old_batch_source, encoding="utf-8")
        legacy_caller = self.a / "utilities/legacy-completion-caller.py"
        legacy_caller.write_text(old_completion_source, encoding="utf-8")
        driver = self.base / "run-old-caller.py"
        driver.write_text(
            "import importlib.util, sys\n"
            "batch_spec = importlib.util.spec_from_file_location(\n"
            "    'dispatch_batch_obligations', sys.argv[5])\n"
            "batch_module = importlib.util.module_from_spec(batch_spec)\n"
            "sys.modules['dispatch_batch_obligations'] = batch_module\n"
            "batch_spec.loader.exec_module(batch_module)\n"
            "spec = importlib.util.spec_from_file_location('legacy_completion', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print(module.batch_obligations.__file__, flush=True)\n"
            "def interrupted(_args): raise RuntimeError('fixture-interruption')\n"
            "module._execute_once = interrupted\n"
            "args = module.parser().parse_args(['--jobs', sys.argv[2], '--resume-obligation',\n"
            "    sys.argv[3], '--obligation-lock-fd', sys.argv[4]])\n"
            "try: module.execute(args)\n"
            "except RuntimeError as exc:\n"
            "    if str(exc) != 'fixture-interruption': raise\n",
            encoding="utf-8")

        launched = []
        old_pids = []
        real_popen = subprocess.Popen

        def wait_ready(path, seconds=5):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not path.exists():
                time.sleep(0.01)
            self.assertTrue(path.exists(), f"fixture observer did not start: {path}")

        def launch_old_finally(ready_path):
            fd = batch.acquire(self.jobs, duty_id)
            self.assertIsNotNone(fd, "released duty lease should be available to the old caller")
            os.set_inheritable(fd, True)
            fixture_env = dict(self._environment(self.a),
                               FIXTURE_READY=str(ready_path),
                               PYTHONPATH=os.pathsep.join((
                                   str(self.a / "utilities"), str(ROOT / "utilities"),
                                   str(self.external_python))))
            try:
                result = subprocess.run(
                    [sys.executable, str(driver), str(legacy_caller), str(self.jobs),
                     duty_id, str(fd),
                     str(self.a / "utilities/dispatch_batch_obligations.py")],
                    env=fixture_env, pass_fds=(fd,),
                    capture_output=True, text=True, timeout=20)
            finally:
                os.close(fd)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(Path(result.stdout.strip()),
                             self.a / "utilities/dispatch_batch_obligations.py")
            wait_ready(ready_path)
            record = batch.read(self.jobs, duty_id)
            observer = record["observer"]
            old_pids.append((observer["pid"], observer["pid_start"]))
            return observer

        def launch_current(*command_args, **kwargs):
            process = real_popen(command_args[0], **kwargs)
            launched.append((process, list(command_args[0]), dict(kwargs["env"])))
            return process

        runner_store = obligations.ObligationStore(self.state)
        runner_lock = runner_store.root / obligations.RUNNER_LOCK_NAME
        runner_lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        runner_fd = os.open(runner_lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # Caller first: the unchanged 414a source runs its actual execute/finally
            # path and starts its sibling from A; the normal activation then takes it
            # over with B under the same lease.
            first_old = launch_old_finally(old_ready[0])
            activation_env = dict(self._environment(self.a), FIXTURE_READY=str(current_ready[0]))
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, activation_env, clear=True), \
                 mock.patch.object(batch.subprocess, "Popen", side_effect=launch_current):
                binding = obligations.managed_service_binding(self.a)
                self.assertEqual(batch.ensure_observers(self.jobs, service_current=binding), 1)
            self.assertEqual(launched[0][1][1], str(current_script))
            wait_ready(current_ready[0])
            self.assertFalse(peer_steward._pid_identity_ok(
                first_old["pid"], first_old["pid_start"]))

            # Activation first: release B's lease, then run the unchanged old
            # caller's real finally/re-ensure path again. It respawns A as before.
            activation_process = launched[0][0]
            activation_process.terminate()
            activation_process.wait(timeout=5)
            second_old = launch_old_finally(old_ready[1])
            self.assertEqual(batch.read(self.jobs, duty_id)["observer"]["pid"],
                             second_old["pid"])

            fcntl.flock(runner_fd, fcntl.LOCK_EX)
            runner_env = dict(self._environment(self.a),
                              FIXTURE_READY=str(current_ready[1]))

            class StopAfterTakeover(Exception):
                pass

            def after_reconcile(_delay):
                wait_ready(current_ready[1])
                self.assertFalse(peer_steward._pid_identity_ok(
                    second_old["pid"], second_old["pid_start"]))
                raise StopAfterTakeover()

            runner_clock = SimpleNamespace(
                sleep=after_reconcile, monotonic=time.monotonic,
                monotonic_ns=time.monotonic_ns, time=time.time)

            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, runner_env, clear=True), \
                 mock.patch.object(obligations, "exec_service_runner"), \
                 mock.patch.object(batch.subprocess, "Popen", side_effect=launch_current), \
                 mock.patch.object(peer_steward, "time", runner_clock):
                binding = obligations.managed_service_binding(self.a)
                with self.assertRaises(StopAfterTakeover):
                    peer_steward.cmd_obligation_runner(SimpleNamespace(
                        state_root=self.state, lock_fd=runner_fd,
                        service_current=binding))

            reconciled_process, reconciled_command, reconciled_env = launched[-1]
            self.assertEqual(Path(reconciled_command[1]), current_script)
            self.assertEqual(reconciled_env["AGENT_HOME"], str(self.b))
            self.assertEqual(Path(os.path.realpath(
                f"/proc/{reconciled_process.pid}/fd/{reconciled_command[
                    reconciled_command.index('--obligation-lock-fd') + 1]}")), lease_path.resolve())
            after = batch.read(self.jobs, duty_id)
            for field in ("id", "identity", "arguments", "outcome", "state", "delivery",
                          "effective_attempts", "replacement_lineage"):
                self.assertEqual(after.get(field), before.get(field), field)
            self.assertNotIn("service_current", after["arguments"])
            self.assertEqual(hashlib.sha256(self.jobs.read_bytes()).hexdigest(), jobs_digest)
            self.assertEqual(after["delivery"], "pending", "observer takeover is not an ACK")
        finally:
            try:
                fcntl.flock(runner_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(runner_fd)
            for process, _command, _environment in launched:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
            for pid, pid_start in old_pids:
                if peer_steward._pid_identity_ok(pid, pid_start):
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 5
                    while peer_steward._pid_identity_ok(pid, pid_start) and time.monotonic() < deadline:
                        time.sleep(0.01)

    def test_exec_error_preserves_the_lock_duty_environment_and_retry_binding(self):
        import fcntl

        self._point_current(self.b)
        store = obligations.ObligationStore(self.state)
        duty = store.create("message-exec-error", "message", {"fixture": "exec-error"},
                            {"ref": "c" * 32})
        lock_path = store.root / obligations.RUNNER_LOCK_NAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited = os.get_inheritable(fd)
        try:
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self.env, clear=True), \
                 mock.patch.object(os, "execve", side_effect=OSError("fixture exec failure")):
                before_env = dict(os.environ)
                self.assertFalse(obligations.exec_service_runner(
                    fd, self.state, str(self.current)))
                self.assertEqual(dict(os.environ), before_env)
                self.assertEqual(os.get_inheritable(fd), inherited)
                self.assertEqual(store.get(duty["id"]), duty)
                probe = os.open(lock_path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
                self.assertEqual(obligations.active_service_root(str(self.current)), self.b)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
