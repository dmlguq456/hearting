"""A live viewer follows only committed forward installations and keeps its UI."""
import fcntl
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import install_follow, render


class InstallFollowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.data = self.base / "data"
        self.state = self.base / "state"
        self.state.mkdir()
        self.lock = self.state / "distribution.lock"
        self.lock.touch()
        self.old = self.release("v3.25.18")
        self.new = self.release("v3.25.19")
        self.env = {"HARNESS_DATA_ROOT": str(self.data), "HARNESS_STATE_ROOT": str(self.state)}
        self.activate(self.old)
        self.follower = install_follow.InstallFollower(self.old, env=self.env)

    def release(self, version):
        root = self.data / "releases" / version
        (root / "tools/fleet").mkdir(parents=True)
        (root / "tools/fleet/fleet.py").write_text("# fixture\n")
        (root / "RELEASE_VERSION").write_text(version + "\n")
        (root / ".hearting-release.json").write_text(json.dumps(
            {"schema": 1, "version": version, "archive_sha256": "a" * 64}))
        return root

    def activate(self, root, *, commit=True):
        current = self.data / "current"
        if current.is_symlink():
            current.unlink()
        current.symlink_to(root)
        if commit:
            (self.state / "distribution.json").write_text(json.dumps(
                {"schema": 1, "release_root": str(root), "version": root.name,
                 "archive_sha256": "a" * 64}))

    def test_committed_forward_install_is_selected(self):
        self.assertIsNone(self.follower.target())
        self.activate(self.new)
        self.assertEqual(self.follower.target(), self.new)

    def test_pointer_before_commit_and_failed_install_do_not_follow(self):
        self.activate(self.new, commit=False)
        self.assertIsNone(self.follower.target())
        self.activate(self.old)
        self.assertIsNone(self.follower.target())

    def test_install_lock_defers_even_after_state_write(self):
        self.activate(self.new)
        with self.lock.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertIsNone(self.follower.target())
        self.assertEqual(self.follower.target(), self.new)

    def test_rollback_and_foreign_checkout_are_ignored(self):
        self.activate(self.release("v3.25.17"))
        self.assertIsNone(self.follower.target())
        self.assertIsNone(install_follow.InstallFollower(self.base, env=self.env).target())
        self.activate(self.new)
        other = self.base / "other/releases/v99.0.0"
        other.parent.mkdir(parents=True)
        self.new.rename(other)
        self.activate(other)
        self.assertIsNone(self.follower.target())

    def test_valid_forward_pin_is_a_normal_install_and_unknown_channel_is_ignored(self):
        self.activate(self.new)
        path = self.state / "distribution.json"
        state = json.loads(path.read_text())
        state.update(channel="pinned", pinned_version=self.new.name)
        path.write_text(json.dumps(state))
        self.assertEqual(self.follower.target(), self.new)
        state["channel"] = "unknown"
        path.write_text(json.dumps(state))
        self.assertIsNone(self.follower.target())

    def test_incomplete_or_uncommitted_release_is_ignored(self):
        self.activate(self.new)
        (self.new / "RELEASE_VERSION").write_text("v3.25.999\n")
        self.assertIsNone(self.follower.target())
        (self.new / "RELEASE_VERSION").write_text(self.new.name)
        (self.new / ".hearting-release.json").write_text('{}')
        self.assertIsNone(self.follower.target())

    def test_reexec_rechecks_candidate_and_preserves_cli_and_environment(self):
        self.activate(self.new)
        restored = []

        def read_before_exec(_executable, _argv, env):
            inherited = dict(env)
            inherited[install_follow._HANDOFF] = str(os.dup(int(env[install_follow._HANDOFF])))
            restored.append(install_follow.read_handoff(inherited))

        with mock.patch.object(install_follow.os, "execve", side_effect=read_before_exec) as execute:
            self.follower.restart(self.new, ["--view", "group"], {"offset": 12})
            executable, argv, env = execute.call_args.args
            self.assertEqual(executable, sys.executable)
            self.assertEqual(argv, [sys.executable, str(self.new / "tools/fleet/fleet.py"),
                                    "--view", "group"])
            self.assertEqual(env["AGENT_HOME"], str(self.new))
            self.assertEqual(restored, [{"offset": 12}])
        self.activate(self.old)
        with mock.patch.object(install_follow.os, "execve") as execute:
            self.assertFalse(self.follower.restart(self.new, [], {}))
            execute.assert_not_called()

    def test_failed_exec_keeps_viewer_and_closes_handoff(self):
        self.activate(self.new)
        with mock.patch.object(install_follow.os, "execve", side_effect=OSError("fixture")):
            self.assertFalse(self.follower.restart(self.new, [], {"offset": 12}))
        self.assertIsNone(install_follow.read_handoff({}))

    def test_viewer_handoff_roundtrip_includes_tuple_fold_keys_and_order(self):
        names = ("_OFFSET", "_PROCESS_VIEW", "_SHOW_ALL", "_ROUTE_FOLD", "_SELECT_MODE",
                 "_CURSOR_ID", "_RESUME_ORDER", "_RELOAD_FRAME", "_LAYOUT", "_SHELL_TTY_MODE")
        saved = {key: getattr(render, key) for key in names}
        self.addCleanup(lambda: [setattr(render, key, value) for key, value in saved.items()])
        render._OFFSET, render._PROCESS_VIEW, render._SHOW_ALL = 12, True, True
        render._LAYOUT = "stack"
        render._ROUTE_FOLD = {("gpu-commands", "host", 0): False, "route:one": True}
        render._SELECT_MODE, render._CURSOR_ID = True, (12, "start")
        order = render._LiveOrderState()
        order.groups, order.group_tiers = ["beta", "alpha"], {"beta": 0, "alpha": 0}
        order.sessions = {"beta": [("codex", "sid", 12)]}
        render._RELOAD_FRAME = [[("cached", "dim")]]
        handoff = json.loads(json.dumps(render.viewer_state(order)))
        render.reset_scroll()
        render._ROUTE_FOLD = {}
        render.restore_viewer_state(handoff)
        self.assertEqual(render._OFFSET, 12)
        self.assertTrue(render._PROCESS_VIEW)
        self.assertTrue(render._SHOW_ALL)
        self.assertEqual(render._LAYOUT, "stack")
        self.assertFalse(render._ROUTE_FOLD[("gpu-commands", "host", 0)])
        self.assertEqual(render._CURSOR_ID, (12, "start"))
        self.assertEqual(render._RESUME_ORDER.groups, ["beta", "alpha"])
        self.assertEqual(render._RESUME_ORDER.sessions, order.sessions)
        self.assertEqual(render._RELOAD_FRAME, [[("cached", "dim")]])

    def test_real_exec_keeps_pid_argv_registry_and_restores_stderr(self):
        self.activate(self.new)
        tools = str(Path(__file__).resolve().parents[2])
        (self.new / "tools/fleet/fleet.py").write_text(
            "import json, os, sys\n"
            f"sys.path.insert(0, {tools!r})\n"
            "from fleet.install_follow import read_handoff\n"
            "value = read_handoff()\n"
            "print(json.dumps({'pid': os.getpid(), 'state': value, 'argv': sys.argv[1:], "
            "'home': os.environ['AGENT_HOME'], 'jobs': os.environ['AGENT_DISPATCH_JOBS']}))\n"
            "sys.stderr.write('stderr restored\\n')\n")
        driver = (
            "import os, sys\n"
            f"sys.path.insert(0, {tools!r})\n"
            "from fleet.install_follow import InstallFollower\n"
            "saved = os.dup(2)\n"
            "with open(os.devnull, 'w') as null: os.dup2(null.fileno(), 2)\n"
            f"follower = InstallFollower({str(self.old)!r})\n"
            "target = follower.target()\n"
            "follower.restart(target, ['--view', 'group'], {'pid': os.getpid(), 'offset': 12}, "
            "stderr_fd=saved)\n"
            "raise SystemExit('exec did not happen')\n")
        result = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True,
                                env={**os.environ, **self.env, "AGENT_DISPATCH_JOBS": "/fixture/jobs.log"},
                                timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['pid'], value['state']['pid'])
        self.assertEqual(value['state']['offset'], 12)
        self.assertEqual(value['argv'], ['--view', 'group'])
        self.assertEqual(value['jobs'], '/fixture/jobs.log')
        self.assertEqual(value['home'], str(self.new))
        self.assertEqual(result.stderr, 'stderr restored\n')

    def test_real_exec_restores_original_shell_terminal_modes(self):
        import termios
        self.activate(self.new)
        tools = str(Path(__file__).resolve().parents[2])
        (self.new / "tools/fleet/fleet.py").write_text(
            "import sys\n"
            f"sys.path.insert(0, {tools!r})\n"
            "from fleet import install_follow, render\n"
            "render.restore_viewer_state(install_follow.read_handoff())\n"
            "render._prepare_terminal()\n")
        driver = (
            "import os, sys, tty\n"
            f"sys.path.insert(0, {tools!r})\n"
            "from fleet import install_follow, render\n"
            "render._prepare_terminal()\n"
            "tty.setraw(0)\n"
            f"follower = install_follow.InstallFollower({str(self.old)!r})\n"
            "follower.restart(follower.target(), [], render.viewer_state(render._LiveOrderState()))\n"
            "raise SystemExit('exec did not happen')\n")
        master, slave = os.openpty()
        try:
            before = termios.tcgetattr(slave)
            result = subprocess.run([sys.executable, '-c', driver], stdin=slave,
                                    capture_output=True, text=True, timeout=10,
                                    env={**os.environ, **self.env})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            os.close(slave)
            os.close(master)

    def test_non_tty_input_retains_existing_curses_fallback(self):
        self.addCleanup(setattr, render, "_SHELL_TTY_MODE", render._SHELL_TTY_MODE)
        render._SHELL_TTY_MODE = None
        with open(os.devnull, 'rb') as stream, mock.patch.object(render.sys, 'stdin', stream):
            render._prepare_terminal()
        self.assertIsNone(render._SHELL_TTY_MODE)

    def test_cached_loading_frame_does_not_reset_scroll(self):
        self.addCleanup(setattr, render, "_OFFSET", render._OFFSET)
        self.addCleanup(setattr, render, "_RELOAD_FRAME", render._RELOAD_FRAME)
        self.addCleanup(setattr, render, "_SELECT_MODE", render._SELECT_MODE)
        render._SELECT_MODE = False
        render._OFFSET = 12
        render._RELOAD_FRAME = [[("line %d" % n, "dim")] for n in range(60)]
        screen = mock.Mock()
        screen.getmaxyx.return_value = 24, 100
        with mock.patch.object(render, "_build_lines", side_effect=AssertionError("fresh loading")), \
             mock.patch.object(render, "_addline"), mock.patch.object(render.curses, "doupdate"):
            render._draw(screen, [], [], "both", 0, loading=True)
        self.assertEqual(render._OFFSET, 12)

    def test_curses_loop_defers_restart_for_input_confirmation_and_pending_kill(self):
        for blocked in ('key', 'prompt', 'kill', None):
            with self.subTest(blocked=blocked):
                names = ("_PROMPT", "_PENDING_KILL", "_SELECT_MODE", "_RESUME_ORDER", "_BLINK_ON")
                saved = {key: getattr(render, key) for key in names}
                self.addCleanup(lambda saved=saved: [setattr(render, k, v) for k, v in saved.items()])
                render._PROMPT = {} if blocked == 'prompt' else None
                render._PENDING_KILL = {} if blocked == 'kill' else None
                render._SELECT_MODE = False
                collector = mock.Mock()
                collector.compute_hosts_refresh = None
                collector.install_follower.target.return_value = self.new
                collector.restart_argv = []
                screen = mock.Mock()
                screen.getmaxyx.return_value = 24, 100
                screen.getch.side_effect = [ord('z') if blocked == 'key' else -1, ord('q')]

                def pump(_producer, *_args, **kwargs):
                    value = mock.Mock()
                    value.poll.return_value = ((1, self.new)
                                               if kwargs.get('name') == 'fleet-install-refresh' else None)
                    return value

                with mock.patch.object(render, 'RefreshPump', side_effect=pump), \
                     mock.patch.object(render, '_draw'), mock.patch.object(render, '_init_colors'), \
                     mock.patch.object(render, 'set_refresh_health'), \
                     mock.patch.object(render, '_handle_prompt_key'), \
                     mock.patch.object(render, '_poll_pending_kill'), \
                     mock.patch.object(render.curses, 'curs_set'):
                    self.assertEqual(render._loop(screen, collector, None, 'both', 2), 0)
                self.assertEqual(collector.install_follower.restart.call_count, 0 if blocked else 1)


if __name__ == "__main__":
    unittest.main()
