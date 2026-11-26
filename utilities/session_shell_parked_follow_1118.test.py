"""11-18: a window whose conversation went to the background reads that session in its panel.

The shell host keeps the session id it was told (the one the window started with) and the panel
used to read that id's frozen values while the conversation went on elsewhere. When the window's
viewer process holds a background session (both facts of ``session_registry.parked_successor``),
the panel reads the held session's route chain, context, NOW, peers, requests, model and number.
Input, delivery and questions keep the host's own ids.

Real child processes carry the argv; the records, the display tap and the title sidecar are real
files in a private HOME. The route cache and Fleet number are supplied, and unrelated display
refreshes stay off while the test reads those files.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import fcntl
import importlib.util
import json
import re
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
spec = importlib.util.spec_from_file_location("session_shell_parked_1118", ROOT / "utilities/session_shell.py")
shell = importlib.util.module_from_spec(spec)
sys.modules["session_shell_parked_1118"] = shell
spec.loader.exec_module(shell)

import human_wait_state  # noqa: E402
import session_tidy  # noqa: E402
from fleet import gitinfo, session_line, session_registry as sr, titles  # noqa: E402

OLD = "92d7cdcd-3cef-4583-80e6-f9a4561ac629"
NEW = "e675a253-0561-410f-aa04-c12345cabf70"
ROUTE = {"id": "rt-new", "title": "route of the held session", "state": "running", "worker_stage": "plan",
         "shape": "one-shot", "owner": "autopilot-code", "stages": [{"name": "plan", "st": "active"}]}
OLD_ROUTE = dict(ROUTE, id="rt-old", title="finished route of the original")
_HOLD = "import os; os.write(1, b'R'); os.read(0, 1)"


class ParkedPanel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pkp-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "OPENCODE_", "CLAUDE_", "CODEX_", "XDG_", "HARNESS_")):
                os.environ.pop(key, None)
        self.home = self.root / "home"
        self.config = self.home / ".claude"
        (self.config / "sessions").mkdir(parents=True)
        (self.config / ".statusline").mkdir()
        os.environ.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.config),
                          XDG_STATE_HOME=str(self.home / ".local" / "state"),
                          XDG_CACHE_HOME=str(self.home / ".cache"),
                          AGENT_PEER_LEDGER_ROOT=str(self.root / "peer"))
        # Git fields are outside the assertions. Don't launch a detached refresh into this home.
        self.addCleanup(os.close, gitinfo._display_claim(gitinfo._display_request(str(self.root))))

    def stop(self, child):
        child.stdin.close()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            raise

    def spawn(self, *argv):
        child = subprocess.Popen([sys.executable, "-c", _HOLD, *argv],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=self.root)
        self.addCleanup(self.stop, child)
        with child.stdout:
            # The record and the resume argv must describe a process that has finished exec.
            self.assertTrue(select.select([child.stdout], [], [], 10)[0], "fixture child did not start")
            self.assertEqual(child.stdout.read(1), b"R", "fixture child exited before it was ready")
        return child

    def record(self, child, **fields):
        start = sr._proc_start(child.pid)
        self.assertIsNotNone(start, f"no process start for fixture PID {child.pid}")
        (self.config / "sessions" / ("%d.json" % child.pid)).write_text(json.dumps(
            {"pid": child.pid, "procStart": start, "startedAt": 1_700_000_000_000, "cwd": str(self.root), **fields}))
        return start

    def scene(self, parked=True, tap_pid=None):  # tap_pid: the pid the display tap names
        """A viewer of OLD (holding the job) and the held background session NEW, with its tap."""
        self.viewer = self.spawn()
        self.vstart = self.record(self.viewer, sessionId=OLD, kind="interactive", status="idle",
                                  **({"parkedJobId": "e675a253"} if parked else {}))
        self.bg = self.spawn("--session-id", NEW, "--fork-session", "--resume", "/p/%s.jsonl" % OLD)
        self.bstart = self.record(self.bg, sessionId=NEW, kind="bg", jobId="e675a253", status="busy")
        tap = {"session_id": NEW, "pid": tap_pid if tap_pid is not None else self.bg.pid, "proc_start": self.bstart, "cwd": str(self.root),
               "model": {"display_name": "Held Model", "id": "held-model"}, "effort": {"level": "max"},
               "context_window": {"used_percentage": 23, "context_window_size": 1000000}}
        (self.config / ".statusline" / (NEW + ".json")).write_text(json.dumps(tap))
        titles.write(NEW, "the held conversation", summary="NOW the held conversation works", now=time.time())

    def project(self):
        calls = []

        def statusline(harness, sid):
            calls.append(sid)
            return (0, 1 if sid == NEW else 0), "route 1" if sid == NEW else ""

        view = {"route_panels": {"claude\0" + NEW: [ROUTE], "claude\0" + OLD: [OLD_ROUTE]},
                "session_peers": {}, "by_session": {}}
        tags = {OLD: "c7", NEW: "74"}
        with mock.patch.object(human_wait_state, "cached_statusline", side_effect=statusline), \
                mock.patch.object(human_wait_state, "cached_view", return_value=view), \
                mock.patch("fleet.session_tags.assigned_tag", side_effect=lambda h, s: tags.get(s)), \
                mock.patch.object(session_tidy, "prompt_display", return_value={"received": []}):
            out = shell.panel_projection("claude", OLD, native_pid=self.viewer.pid, native_start=self.vstart,
                                         columns=100, rows=40)
        return out, calls

    def routes(self, out):
        return [row["id"] for row in out["panel"]["routes"]]

    def test_panel_reads_the_held_session(self):
        self.scene()
        out, calls = self.project()
        self.assertEqual(calls, [NEW])                       # the route cache is asked about NEW
        self.assertEqual(self.routes(out), ["rt-new"])        # ROUTE
        self.assertEqual(out["context"], 23)                  # CTX, from the tap NEW's process wrote
        self.assertEqual(out["native_display"]["model"], "Held Model")
        self.assertEqual(out["native_display"]["effort"], "max")
        self.assertIn("[74]", out["footer"])                  # the number Fleet gave NEW
        self.assertNotIn("[c7]", out["footer"])
        self.assertIn("23%", out["footer"])                   # the same number the bottom line shows
        # The host's own turn record stays the viewer's: input readiness is not moved.
        self.assertEqual(out["turn_record"], str(self.config / "sessions" / ("%d.json" % self.viewer.pid)))
        self.assertIn(str(self.config / "sessions" / ("%d.json" % self.bg.pid)), out["watch"])
        self.assertIn(str(self.config / "sessions" / ("%d.json" % self.viewer.pid)), out["watch"])

    def test_panel_context_is_the_number_of_the_bottom_line(self):
        """The window's bottom line is the held process's own statusline script; the panel
        must show the same number for the same callback (rounding half up, no ceiling)."""
        self.scene()
        tap_path = self.config / ".statusline" / (NEW + ".json")
        tap = json.loads(tap_path.read_text())
        bottom_line_config = self.root / "bottom-line"
        bottom_line_config.mkdir()
        # The real statusline script reaches across a process boundary. Hold the existing
        # fixture-local lock so rendering never leaves an answer-wait producer behind.
        projection = human_wait_state.state_root()
        projection.mkdir(mode=0o700, parents=True)
        lock = self.enterContext((projection / ".projection.lock").open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(self.home),
               "CLAUDE_CONFIG_DIR": str(bottom_line_config), "AGENT_HOME": str(ROOT),
               "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"], "PYTHONDONTWRITEBYTECODE": "1",
               "HARNESS_CAPACITY_REFRESH_DISABLE": "1"}
        for used, expected in ((0, 0), (22.4, 22), (22.5, 23), (23, 23), (98.6, 99),
                               (99.5, 100), (99.6, 100), (100, 100)):
            with self.subTest(used=used):
                tap["context_window"]["used_percentage"] = used
                payload = json.dumps(tap)
                tap_path.write_text(payload)
                shown = subprocess.run(
                    ["bash", str(ROOT / "adapters/claude/statusline.sh")], input=payload, text=True,
                    capture_output=True, timeout=60, cwd=self.root, env=env)
                self.assertEqual(shown.returncode, 0, shown.stderr)
                line = re.sub(r"\x1b\[[0-9;]*m", "", shown.stdout)
                match = re.search(r"context [━─]+ (\d+)%", line)
                self.assertIsNotNone(match, line)
                self.assertEqual(int(match.group(1)), expected, line)
                out, _ = self.project()
                self.assertIn(match.group(1) + "%", out["footer"])
                self.assertEqual(session_line._percent(out["context"]), match.group(1) + "%")

    def test_no_job_in_the_window_record_reads_the_original(self):
        self.scene(parked=False)
        out, calls = self.project()
        self.assertEqual(calls, [OLD])
        self.assertEqual(self.routes(out), ["rt-old"])
        self.assertIsNone(out["context"])                    # OLD has no tap: nothing is borrowed from NEW
        self.assertIn("[c7]", out["footer"])

    def test_link_ends_at_the_next_projection(self):
        self.scene()
        self.assertEqual(self.routes(self.project()[0]), ["rt-new"])
        self.stop(self.bg)
        out, calls = self.project()
        self.assertEqual((calls, self.routes(out)), ([OLD], ["rt-old"]))
        self.assertIn("[c7]", out["footer"])

    def test_tap_of_another_process_is_not_borrowed(self):
        self.scene(tap_pid=1)                                 # the tap names another process, not the held one
        out, _ = self.project()
        self.assertIsNone(out["context"])

    def test_viewer_of_another_window_is_not_followed(self):
        self.scene()
        other = self.spawn()
        start = self.record(other, sessionId=OLD, kind="interactive", status="idle")
        with mock.patch.object(human_wait_state, "cached_statusline", return_value=((0, 0), "")) as asked, \
                mock.patch.object(human_wait_state, "cached_view", return_value={}), \
                mock.patch.object(session_tidy, "prompt_display", return_value={"received": []}):
            shell.panel_projection("claude", OLD, native_pid=other.pid, native_start=start, columns=100, rows=40)
        self.assertEqual(asked.call_args.args[1], OLD)

    def test_the_supervision_mark_is_read_for_the_session_the_panel_shows(self):
        """Beside NEW's number the mark is NEW's: OLD's supervision does not draw one, and NEW's does."""
        from fleet.collectors import steward as steward_role
        self.scene()
        for marked, expected in ((OLD, False), (NEW, True)):
            asked = []

            def role(harness, sid, **kw):
                asked.append(sid)
                return sid == marked
            with mock.patch.object(steward_role, "role_flag", side_effect=role):
                out, _ = self.project()
            self.assertEqual(asked, [NEW])
            self.assertIs(out["panel"]["steward"], expected, marked)
        # No link: the window's own session, as before.
        self.stop(self.bg)
        asked = []
        with mock.patch.object(steward_role, "role_flag", side_effect=lambda h, sid, **kw: asked.append(sid) or True):
            out, _ = self.project()
        self.assertEqual(asked, [OLD])
        self.assertIs(out["panel"]["steward"], True)

    def test_one_projection_reads_the_session_records_once(self):
        """The panel decides the link once per projection, not once per value it draws."""
        self.scene()
        real = sr._live_claude_records
        with mock.patch.object(sr, "_live_claude_records", side_effect=real) as read:
            self.project()
        self.assertEqual(read.call_count, 1)

    def test_other_harnesses_are_unchanged(self):
        self.scene()
        with mock.patch.object(human_wait_state, "cached_statusline", return_value=((0, 0), "")) as asked, \
                mock.patch.object(human_wait_state, "cached_view", return_value={}), \
                mock.patch.object(session_tidy, "prompt_display", return_value={"received": []}), \
                mock.patch.object(sr, "parked_successor") as link:
            for harness in ("codex", "opencode"):
                shell.panel_projection(harness, OLD, native_pid=self.viewer.pid, native_start=self.vstart,
                                       columns=100, rows=40)
                self.assertEqual(asked.call_args.args, (harness, OLD))
        link.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
