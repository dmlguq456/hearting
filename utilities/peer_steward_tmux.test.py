#!/usr/bin/env python3
"""peer-steward on tmux without Herdr: clear and prompt against a fake Claude TUI.

Each test starts its own tmux server on a socket inside its temporary directory and
points ``$TMUX`` at it; the user's tmux and Herdr servers are never reached (no
``herdr`` executable is visible). The fake runtime is a Python TUI named ``claude``
(argv[0]) that writes its own session registry the way Claude does, asks for
bracketed paste, logs every byte it receives, and on ``/clear`` moves its registry to
a new session id.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "tools"))
sys.path.insert(0, str(_HERE))
_SPEC = importlib.util.spec_from_file_location("peer_steward_tmux_under_test", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(peer_steward)

OLD = "3c1d2e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
NEW = "7e6d5c4b-3a29-4180-9f7e-6d5c4b3a2918"

_FAKE_CLAUDE = r'''
import json, os, sys, termios, tty
from pathlib import Path
log_path, sid, status, new_sid, draft = sys.argv[1:6]
log = open(log_path, "ab", buffering=0)
stat = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
registry = Path(os.environ["HOME"]) / ".claude" / "sessions"
registry.mkdir(parents=True, exist_ok=True)

def declare(session, state):
    record = {"pid": os.getpid(), "sessionId": session, "procStart": stat[19],
              "kind": "interactive", "status": state, "cwd": os.getcwd()}
    (registry / f".{os.getpid()}.tmp").write_text(json.dumps(record))
    os.replace(registry / f".{os.getpid()}.tmp", registry / f"{os.getpid()}.json")

def draw(text):
    rule = "─" * 40
    sys.stdout.write("\x1b[2J\x1b[H\x1b[?2004h❯ earlier\r\n\r\n" + rule + "\r\n❯ " + text
                     + "\r\n" + rule + "\r\n  footer\r\n")
    sys.stdout.flush()

declare(sid, status)
if len(sys.argv) > 6:
    # What the runtime's own hook process would run, inside this exact runtime.
    exec(Path(sys.argv[6]).read_text(), {"sid": sid, "pane": os.environ["TMUX_PANE"], "out": log_path + ".out"})
tty.setraw(sys.stdin.fileno())
draw(draft)
seen = b""
while True:
    data = os.read(sys.stdin.fileno(), 4096)
    if not data:
        break
    log.write(data)
    seen += data
    if seen.endswith(b"\x1b[201~\r"):
        if b"/clear" in seen:
            declare(new_sid, "idle")
        else:
            declare(sid, "busy")
        seen = b""
        draw("")
'''


@unittest.skipUnless(shutil.which("tmux") and shutil.which("bash"), "tmux or bash is not installed")
class TmuxPeerStewardTest(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in tuple(os.environ):
            if key.startswith(("AGENT_", "HERDR_", "OPENCODE_", "CLAUDE_", "CODEX_", "XDG_", "TMUX")):
                os.environ.pop(key, None)
        os.environ.pop("MEM_DISTILL", None)
        os.environ.pop("FLEET_TITLE_REFRESH", None)
        home = self.root / "home"
        os.environ.update(HOME=str(home), AGENT_PEER_LEDGER_ROOT=str(self.root / "ledger"),
                          AGENT_DISPATCH_JOBS=str(self.root / "jobs.log"),
                          XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".local/state"),
                          XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"))
        (self.root / "jobs.log").touch()
        self.socket = self.root / "t.sock"
        self.env = dict(os.environ, SHELL="/bin/sh", PS1="$ ")
        started = self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "p1", "-x", "160", "-y", "40", "sh")
        if started.returncode:
            self.fail(f"isolated tmux server did not start: {started.stderr.strip()}")
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", str(self.socket), "kill-server"],
                                               capture_output=True, env=self.env, timeout=10, check=False))
        self.pane = self.tmux("display-message", "-p", "-t", "p1", "#{pane_id}").stdout.strip()
        os.environ["TMUX"] = f"{self.socket},1,0"
        (self.root / "claude.py").write_text(_FAKE_CLAUDE)
        self.log = self.root / "input.log"
        self.log.touch()
        which = shutil.which

        def no_herdr(name, *args, **kwargs):
            return None if name == "herdr" else which(name, *args, **kwargs)
        patch = mock.patch.object(shutil, "which", side_effect=no_herdr)
        patch.start()
        self.addCleanup(patch.stop)
        runner = mock.patch.object(peer_steward.peer_obligations, "ensure_normal_runner", return_value=False)
        runner.start()
        self.addCleanup(runner.stop)

    def tmux(self, *args, socket=None):
        return subprocess.run(["tmux", "-S", str(socket or self.socket), *args], capture_output=True,
                              text=True, env=self.env, timeout=10, check=False)

    @property
    def address(self):
        return f"{self.pane}@{os.path.realpath(self.socket)}"

    def start_claude(self, status="idle", draft="", *extra, socket=None, log=None, sid=OLD):
        socket, log = socket or self.socket, log or self.log
        line = shlex.join(["exec", "-a", "claude", sys.executable, str(self.root / "claude.py"),
                           str(log), sid, status, NEW, draft or "", *extra])
        self.tmux("send-keys", "-t", self.pane, "-l", "bash -c " + shlex.quote(line), socket=socket)
        self.tmux("send-keys", "-t", self.pane, "Enter", socket=socket)
        surface = peer_steward.terminal_surface.TmuxSurface(socket=str(socket))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            agent = (surface.agent(self.pane) or {}).get("result", {}).get("agent") or {}
            if (agent.get("agent_session") or {}).get("value") == sid and "footer" in (
                    surface.read_visible(self.pane) or ""):
                return agent["pid"]
            time.sleep(.05)
        self.fail("the fake Claude never took the pane")

    def book(self):
        import session_tidy as st
        import session_tidy_clear as clear
        seat = st.Seat("pane", st._digest("pane", self.address), self.address, "claude", "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, "claude", OLD, "card body", prompt_seq=0)
            clear._write_reservation({
                "schema": 1, "nonce": "n0nce", "status": "reserved", "created": now, "deadline": now + 600,
                "seat": {"kind": "pane", "key": seat.key, "pane": self.address, "harness": "claude",
                         "project_key": ""},
                "harness": "claude", "sid": OLD, "cwd": str(self.root),
                "card_generation": card["generation"], "prompt_seq": 0})
        return clear.reservation_path(seat.key)

    def steward(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = peer_steward.main(list(argv))
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        return rc, lines[-1] if lines else ""

    def typed(self):
        return self.log.read_bytes()

    def transcript(self):
        """The session's own transcript with no background agents, so the booking clears."""
        path = Path(os.environ["HOME"]) / ".claude" / "projects" / "proj" / f"{OLD}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "user", "sessionId": OLD}) + "\n")
        return path

    def test_an_idle_claude_with_an_empty_box_is_cleared_once_and_the_new_session_is_seen(self):
        self.transcript()
        self.start_claude()
        path = self.book()
        rc, line = self.steward("clear", self.address, "--request", str(path), "--nonce", "n0nce")
        self.assertEqual(rc, 0, line)
        self.assertIn("cleared=true", line)
        self.assertIn(f"old_session={OLD}", line)
        self.assertIn(f"new_session={NEW}", line)
        self.assertEqual(self.typed(), b"\x1b[200~/clear\x1b[201~\r")

    def test_without_its_transcript_the_tmux_pane_gets_compact_like_a_herdr_pane(self):
        # Agents that may still run keep their window: the shared booking types /compact.
        self.start_claude()
        path = self.book()
        rc, line = self.steward("clear", self.address, "--request", str(path), "--nonce", "n0nce")
        self.assertIn("reason=compact-pending", line)
        self.assertEqual(self.typed(), b"\x1b[200~/compact\x1b[201~\r")

    def test_a_draft_or_a_running_turn_is_never_typed_over(self):
        for status, draft, reason in (("idle", "half typed text", "reason=draft"),
                                      ("busy", "", "reason=not-idle-working"),
                                      ("waiting", "", "reason=form-open")):
            with self.subTest(status=status, draft=draft):
                self.tmux("respawn-pane", "-k", "-t", self.pane, "sh")
                self.log.write_bytes(b"")
                self.start_claude(status=status, draft=draft)
                path = self.book()
                rc, line = self.steward("clear", self.address, "--request", str(path), "--nonce", "n0nce")
                self.assertNotEqual(rc, 0, line)
                self.assertIn(reason, line)
                self.assertEqual(self.typed(), b"")

    def test_a_pane_that_is_not_the_booked_session_is_not_cleared(self):
        self.start_claude()
        path = self.book()
        record = json.loads(path.read_text())
        record["sid"] = "11111111-2222-4333-8444-555555555555"
        path.write_text(json.dumps(record))
        rc, line = self.steward("clear", self.address, "--request", str(path), "--nonce", "n0nce")
        self.assertNotEqual(rc, 0, line)
        self.assertEqual(self.typed(), b"")

    def test_a_prompt_to_an_idle_claude_pane_is_typed_once_through_its_guards(self):
        self.start_claude()
        rc, line = self.steward("prompt", self.pane, "hello from a peer")
        self.assertIn("to_harness=claude", line)
        self.assertNotIn("reason=herdr-not-found", line)
        typed = self.typed()
        self.assertEqual(typed.count(b"\x1b[200~"), 1, typed)
        self.assertIn(b"hello from a peer", typed)
        self.assertTrue(typed.endswith(b"\x1b[201~\r"), typed)

    def test_a_prompt_to_a_busy_claude_pane_types_nothing_and_stays_queued(self):
        self.start_claude(status="busy")
        rc, line = self.steward("prompt", self.pane, "hello from a peer")
        self.assertIn("prompted=queued", line)
        self.assertEqual(self.typed(), b"")

    def test_a_pane_in_copy_mode_is_neither_prompted_nor_cleared(self):
        self.start_claude()
        self.tmux("copy-mode", "-t", self.pane)
        rc, line = self.steward("prompt", self.pane, "first line\nsecond line")
        self.assertIn("prompted=queued", line)
        self.assertIn("reason=pane-in-mode", line)
        path = self.book()
        rc, line = self.steward("clear", self.address, "--request", str(path), "--nonce", "n0nce")
        self.assertIn("cleared=skipped", line)
        self.assertIn("reason=pane-in-mode", line)
        self.assertEqual(self.typed(), b"")
        self.assertEqual(self.tmux("display-message", "-p", "-t", self.pane, "#{pane_in_mode}").stdout.strip(), "1")

    def test_a_synchronized_window_gives_neither_pane_any_input(self):
        self.start_claude()
        neighbour_log = self.root / "neighbour.log"
        neighbour_log.touch()
        line = shlex.join([sys.executable, str(self.root / "claude.py"), str(neighbour_log),
                           "00000000-0000-4000-8000-000000000000", "idle", NEW, ""])
        self.tmux("split-window", "-d", "-t", self.pane, line)
        self.tmux("set-option", "-w", "-t", self.pane, "synchronize-panes", "on")
        rc, line = self.steward("prompt", self.pane, "hello from a peer")
        self.assertIn("reason=pane-synchronized", line)
        path = self.book()
        rc, line = self.steward("clear", self.address, "--request", str(path), "--nonce", "n0nce")
        self.assertIn("reason=pane-synchronized", line)
        self.assertEqual(self.typed(), b"")
        self.assertEqual(neighbour_log.read_bytes(), b"")

    def test_a_bare_pane_id_is_this_servers_pane_and_an_address_names_its_server(self):
        other = self.root / "u.sock"
        started = self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "p2", "-x", "160", "-y", "40", "sh",
                            socket=other)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", str(other), "kill-server"],
                                               capture_output=True, env=self.env, timeout=10, check=False))
        self.assertEqual(self.tmux("display-message", "-p", "-t", "p2", "#{pane_id}", socket=other).stdout.strip(),
                         self.pane)
        other_log = self.root / "other.log"
        other_log.touch()
        self.start_claude()
        self.start_claude(socket=other, log=other_log, sid="2b2b2b2b-3c3c-4d4d-8e8e-5f5f5f5f5f5f")
        rc, line = self.steward("prompt", self.pane, "to this server")
        self.assertIn(f"target={self.address}", line)
        rc, line = self.steward("prompt", f"{self.pane}@{os.path.realpath(other)}", "to the other server")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not other_log.read_bytes():
            time.sleep(.05)
        self.assertIn(b"to this server", self.typed())
        self.assertNotIn(b"to the other server", self.typed())
        self.assertIn(b"to the other server", other_log.read_bytes())
        self.assertNotIn(b"to this server", other_log.read_bytes())

    def test_the_clear_helper_waits_for_the_runtime_turn_record_on_tmux(self):
        import session_tidy_clear as clear
        pid = self.start_claude(status="busy")
        self.assertEqual(clear.pane_command(self.pane), shutil.which("tmux"))
        self.assertEqual(clear._herdr_wait_idle(self.pane, 300), "timeout")
        registry = Path(os.environ["HOME"]) / ".claude" / "sessions" / f"{pid}.json"
        registry.write_text(json.dumps(dict(json.loads(registry.read_text()), status="idle")))
        self.assertEqual(clear._herdr_wait_idle(self.pane, 5000), "idle")
        self.assertEqual(clear._herdr_wait_idle("%999", 300), "agent-not-found")

    def in_runtime(self, code):
        snippet = self.root / "snippet.py"
        snippet.write_text(f"import sys\nsys.path[:0] = [{str(_HERE)!r}, {str(_HERE.parent / 'tools')!r}]\n"
                           + code)
        self.start_claude("idle", "", str(snippet))
        out = Path(str(self.log) + ".out")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not out.exists():
            time.sleep(.05)
        return json.loads(out.read_text())

    def test_the_runtime_hook_gets_a_tmux_pane_seat(self):
        seat = self.in_runtime(
            "import json, os, session_tidy as st\n"
            "from pathlib import Path\n"
            "seat = st.resolve_seat('claude', os.getcwd(), os.environ, sid)\n"
            "Path(out).write_text(json.dumps([seat.kind, seat.pane, seat.key]))\n")
        import session_tidy as st
        self.assertEqual(seat, ["pane", self.address, st._digest("pane", self.address)])
        # The same hint in another process proves no pane: it stays the project seat.
        os.environ["TMUX_PANE"] = self.pane
        self.assertEqual(st.resolve_seat("claude", str(self.root), os.environ, OLD).kind, "project")

    def test_the_runtime_projects_its_header_onto_its_own_tmux_pane(self):
        self.in_runtime(
            "import json\nfrom pathlib import Path\nfrom fleet.herdr_projection import project\n"
            "project('claude', sid, report_session=False, pane_id=pane)\n"
            "Path(out).write_text(json.dumps('done'))\n")
        title = self.tmux("display-message", "-p", "-t", self.pane, "#{pane_title}").stdout.strip()
        option = self.tmux("show-options", "-p", "-t", self.pane, "-v", "@hearting_title").stdout.strip()
        self.assertIn("claude", title)
        self.assertEqual(option, title)
        # Another process (this test) is not that runtime: it may not retitle the pane.
        from fleet.herdr_projection import project
        self.tmux("select-pane", "-t", self.pane, "-T", "kept")
        project("claude", OLD, report_session=False, pane_id=self.pane)
        self.assertEqual(self.tmux("display-message", "-p", "-t", self.pane, "#{pane_title}").stdout.strip(),
                         "kept")


if __name__ == "__main__":
    unittest.main(verbosity=2)
