#!/usr/bin/env python3
"""Retire a just-started shell seat: an unselected seat, a suggestion-only composer, the wait line.

Seen on 11-27: retire on four sessions started two or three minutes earlier. Two Codex
seats that had never taken a prompt waited forever on ``foreground-unverified`` (their
attachment has selected no session yet), one Claude seat waited on ``draft`` because the
composer's faint ``Try "…"`` suggestion was read as typed text, and every wait said
nothing about what would release it. Only attachment, terminal and runtime
observations are synthetic: no window, herdr session or native model is touched.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import session_shell

spec = importlib.util.spec_from_file_location("fresh_seat_fixture",
    ROOT / "utilities/peer_wrapped_foreground.test.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
peer = fixture.peer
REAL_SCREEN_READY = peer._screen_ready
UNSELECTED = "-"
OTHER_SID = "01000000-0000-7000-8000-000000000009"
RULE = "─" * 24

# A second pane's own PTY owner with its host and native children, as a real seat has.
_SECOND_BROKER = (
    "import subprocess,sys\n"
    "kids=[subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.buffer.read()'],"
    "stdin=subprocess.PIPE) for _ in range(2)]\n"
    "print(' '.join(str(k.pid) for k in kids),flush=True)\n"
    "sys.stdin.buffer.read()\n"
    "[k.stdin.close() for k in kids]\n"
    "[k.wait(timeout=5) for k in kids]\n")


W = fixture.WrappedForegroundTest


class SeatBase(unittest.TestCase):
    """The shared wrapped-seat fixture's setup and helpers, without its own tests."""
    publish, close_children = W.publish, W.close_children
    select, process_info, ready, send, retire = W.select, W.process_info, W.ready, W.send, W.retire

    def setUp(self):
        W.setUp(self)


def shell_lines(*rows):
    """Native cells as the shell host sends them (``[text, faint]`` runs per row)."""
    lines, why = session_shell.screen_lines([list(map(list, row)) for row in rows])
    assert lines is not None, why
    return lines


class FreshSeatRetireTest(SeatBase):
    def setUp(self):
        super().setUp()
        self.state = "unknown"  # a wrapped seat's turn before any lifecycle event
        mock.patch.object(peer, "_retire_target", side_effect=lambda *a, **k:
                          (self.state, self.ident, 0, None)).start()

    def fresh(self, harness):
        """A seat the shell host attached whose native has selected no session yet."""
        self.select(harness)
        self.publish(harness, sid=UNSELECTED)
        self.ident["session_id"] = UNSELECTED
        self.current_sid = None  # no session record: the native was never prompted

    def second_seat(self, harness):
        """Another just-started seat of the same harness in its own pane."""
        broker = subprocess.Popen([sys.executable, "-c", _SECOND_BROKER],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        def close():
            broker.stdin.close()
            broker.wait(timeout=10)
            broker.stdout.close()
        self.addCleanup(close)
        host_pid, native_pid = map(int, broker.stdout.readline().split())
        owner, host, native = (session_shell.process(pid) for pid in (broker.pid, host_pid, native_pid))
        value = {"schema": session_shell.SCHEMA, "harness": harness, "sid": UNSELECTED,
                 "incarnation": "c" * 32, "socket": self.home.name + "/second.sock",
                 "host_pid": host["pid"], "host_start": host["start"],
                 "native_pid": native["pid"], "native_start": native["start"],
                 "broker_pid": owner["pid"], "broker_start": owner["start"]}
        path = session_shell.binding_path(harness, host["pid"])
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        self.assertEqual(len([row for row in session_shell.bindings(harness=harness)
                              if row["sid"] == UNSELECTED]), 2)

    def test_an_unselected_seat_retires_normally_on_every_adapter(self):
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                self.fresh(harness)
                rc, output = self.retire()
                self.assertEqual(rc, 0, output)
                self.assertIn("retired=true reason=normal-exit", output)
                expected = ([("send-text", "/exit"), ("send-keys", "enter")] if harness == "claude"
                            else [("send-keys", "ctrl+d")])
                self.assertEqual(self.actions, expected + [("close", self.pane)])
                booked = self.store.list()[0]["intent"]["identity"]["foreground"]
                self.assertEqual((booked["pid"], booked["session_id"]), (self.native["pid"], UNSELECTED))

    def test_two_unselected_seats_of_one_harness_keep_each_pane_exact(self):
        self.fresh("codex")
        self.second_seat("codex")
        rc, output = self.retire()
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.actions, [("send-keys", "ctrl+d"), ("close", self.pane)])

    def test_an_unselected_seat_still_withholds_exit_without_its_own_evidence(self):
        proven = SimpleNamespace(confidence="proven", session_id=OTHER_SID)
        cases = {
            "working": lambda stack: setattr(self, "state", "working"),
            "native-proves-a-session": lambda stack: stack.enter_context(mock.patch(
                "fleet.process_identity.process_identity", return_value=proven)),
            "selected-before-exit": lambda stack: setattr(
                self, "before_ready", lambda: self.publish("codex", sid=OTHER_SID)),
            "draft": lambda stack: setattr(self, "screen_reason", "draft"),
        }
        for name, arrange in cases.items():
            with self.subTest(case=name), contextlib.ExitStack() as stack:
                self.fresh("codex")
                self.state, self.screen_reason = "unknown", None
                self.before_ready = lambda: None
                arrange(stack)
                _rc, output = self.retire()
                self.assertEqual(self.actions, [], output)
                self.assertNotIn("retired=true", output)
                self.assertIn("pending=true", output)

    def test_a_selected_seat_with_an_unknown_turn_still_waits(self):
        # Only an unselected seat has no turn to wait for; a selected one keeps the gate.
        self.select("codex")
        self.state = "unknown"
        _rc, output = self.retire()
        self.assertEqual(self.actions, [])
        self.assertIn("reason=agent-unknown", output)


class SuggestionComposerTest(SeatBase):
    """Claude draws an empty composer's suggestion faint with the cursor block on its first letter."""

    SUGGESTION_ANSI = "❯\xa0\x1b[7mT\x1b[27m\x1b[2mry \"fix lint errors\"\x1b[22m\r\n" + RULE + "\r\n"

    def setUp(self):
        super().setUp()
        mock.patch.object(peer, "_screen_ready", REAL_SCREEN_READY).start()

    def suggestion_lines(self):
        return shell_lines([("❯ T", False), ('ry "fix lint errors"', True)], [(RULE, False)])

    def test_the_suggestion_is_an_empty_composer_from_herdr_and_from_the_shell_host(self):
        self.assertIsNone(REAL_SCREEN_READY("claude", peer._screen_lines(self.SUGGESTION_ANSI)))
        self.assertIsNone(REAL_SCREEN_READY("claude", self.suggestion_lines()))

    def test_typed_text_beside_a_suggestion_is_still_a_draft(self):
        drafts = {
            "one-typed-letter": [[("❯ T", False)], [(RULE, False)]],
            "typed-then-faint-ghost": [[("❯ Try", False), (' "fix"', True)], [(RULE, False)]],
            "cursor-after-typed-letter": [[("❯ Tr", False), ('y "fix"', True)], [(RULE, False)]],
            "bright-after-the-faint-run": [[("❯ T", False), ("ry", True), (" now", False)], [(RULE, False)]],
            "gap-before-the-faint-run": [[("❯ a ", False), ("suggestion", True)], [(RULE, False)]],
            "second-line": [[("❯ T", False), ("ry", True)], [("  more", False)], [(RULE, False)]],
        }
        for name, rows in drafts.items():
            with self.subTest(case=name):
                self.assertEqual(REAL_SCREEN_READY("claude", shell_lines(*rows)), "draft")

    def test_public_retire_exits_a_seat_showing_only_the_suggestion(self):
        self.select("claude")
        with mock.patch.object(peer, "_read_screen", return_value=self.suggestion_lines()):
            rc, output = self.retire()
        self.assertEqual(rc, 0, output)
        self.assertIn("retired=true reason=normal-exit", output)
        self.assertEqual(self.actions, [("send-text", "/exit"), ("send-keys", "enter"),
                                        ("close", self.pane)])

    def test_public_retire_keeps_a_real_draft(self):
        self.select("claude")
        typed = shell_lines([("❯ Try this", False)], [(RULE, False)])
        with mock.patch.object(peer, "_read_screen", return_value=typed):
            _rc, output = self.retire()
        self.assertEqual(self.actions, [])
        self.assertIn("reason=draft", output)


class PendingWaitLineTest(SeatBase):
    """A pending retire names, on its own line, what releases it; nothing new is asked."""

    def test_each_pending_result_carries_one_release_line(self):
        cases = {
            "draft": lambda stack: setattr(self, "screen_reason", "draft"),
            "form-open": lambda stack: setattr(self, "screen_reason", "form-open"),
            "agent-working-herdr-corroboration": lambda stack: stack.enter_context(mock.patch.object(
                peer, "_retire_target", return_value=("working", self.ident, 0, None))),
            "foreground-unverified": lambda stack: stack.enter_context(mock.patch.object(
                peer, "_retire_foreground", return_value=None)),
        }
        for reason, arrange in cases.items():
            with self.subTest(reason=reason), contextlib.ExitStack() as stack:
                self.select("claude")
                self.screen_reason = None
                arrange(stack)
                _rc, output = self.retire()
                self.assertEqual(self.actions, [])
                line = output.strip().splitlines()[-1]
                self.assertIn(f"reason={reason} ", line)
                self.assertIn("pending=true", line)
                detail = line.split(" detail=", 1)
                self.assertEqual(len(detail), 2, line)
                self.assertEqual(detail[1], peer._retire_wait_detail(reason))
                self.assertTrue(detail[1].strip())

    def test_release_lines_name_the_cause_family(self):
        words = {"draft": "input box", "draft-unknown": "input box", "form-open": "dialog",
                 "foreground-unverified": "close", "agent-working-herdr-corroboration": "turn",
                 "pane-in-mode-wait": "tmux", "shell-screen-large": "screen",
                 "exit-send-unknown": "exit", "some-new-reason": "some-new-reason"}
        for reason, word in words.items():
            with self.subTest(reason=reason):
                self.assertIn(word, peer._retire_wait_detail(reason).lower())

    def test_a_finished_retire_has_no_release_line(self):
        self.select("claude")
        rc, output = self.retire()
        self.assertEqual(rc, 0)
        self.assertNotIn("detail=", output)


if __name__ == "__main__":
    unittest.main()
