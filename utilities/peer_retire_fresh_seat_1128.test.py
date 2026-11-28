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
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import session_shell
from fixtures.session_shell_rig import AnswerReachesIdleShellTest, ShellRig  # noqa: E402

spec = importlib.util.spec_from_file_location("fresh_seat_fixture",
    ROOT / "utilities/peer_wrapped_foreground.test.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
peer = fixture.peer
REAL_SCREEN_READY = peer._screen_ready
REAL_PANE_READINESS = peer._pane_readiness
REAL_READ_SCREEN = peer._read_screen
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
    """Native cells as the shell host sends them (``[text, faint]`` runs per row, a reverse-video
    run ``[text, faint, true]``)."""
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
    """Claude draws an empty composer's suggestion faint with the cursor block on its first letter.

    That bright first cell is left out only when the reader shows the cursor on it
    (reverse video); review 11-28 (PR #1933): without that evidence a person's bright
    typed letter beside a faint cursor or completion read as an empty box."""

    SUGGESTION_ANSI = "❯\xa0\x1b[7mT\x1b[27m\x1b[2mry \"fix lint errors\"\x1b[22m\r\n" + RULE + "\r\n"
    # name: (herdr ANSI input line, shell host runs, `_screen_ready`)
    SCREENS = {
        "cursor-on-the-suggestion": ("❯ \x1b[7mT\x1b[27m\x1b[2mry \"fix lint errors\"\x1b[22m",
                                     [("❯ ", False), ("T", False, True), ('ry "fix lint errors"', True)], None),
        "typed-letter-then-faint-reverse-cursor": (
            "❯ T\x1b[2;7mr\x1b[27my \"fix lint errors\"\x1b[22m",
            [("❯ T", False), ("r", True, True), ('y "fix lint errors"', True)], "draft"),
        "typed-text-then-a-blank-cursor": ("❯ Try\x1b[7m \x1b[27m",
                                           [("❯ Try", False), (" ", False, True)], "draft"),
        "typed-letter-blank-cursor-faint-rest": ("❯ T\x1b[7m \x1b[27m\x1b[2mry\x1b[22m",
                                                 [("❯ T", False), (" ", False, True), ("ry", True)], "draft"),
        "cursor-on-a-typed-first-letter": ("❯ \x1b[7mT\x1b[27mry this",
                                           [("❯ ", False), ("T", False, True), ("ry this", False)], "draft"),
        "bright-after-the-faint-run": ("❯ \x1b[7mT\x1b[27m\x1b[2mry\x1b[22m now",
                                       [("❯ ", False), ("T", False, True), ("ry", True), (" now", False)], "draft"),
        "gap-before-the-faint-run": ("❯ \x1b[7mT\x1b[27m \x1b[2msuggestion\x1b[22m",
                                     [("❯ ", False), ("T", False, True), (" ", False), ("suggestion", True)], "draft"),
        "truecolor-typed-text": ("❯ \x1b[38;2;255;255;255mTyped draft\x1b[0m",
                                 [("❯ Typed draft", False)], "draft"),
        "truecolor-two-operand": ("❯ \x1b[38;2;2;136;136mTyped draft\x1b[0m",
                                  [("❯ Typed draft", False)], "draft"),
    }

    def setUp(self):
        super().setUp()
        mock.patch.object(peer, "_screen_ready", REAL_SCREEN_READY).start()

    def herdr_lines(self, line):
        return peer._screen_lines(line + "\r\n" + RULE + "\r\n")

    def host_lines(self, runs):
        return shell_lines(runs, [(RULE, False)])

    def suggestion_lines(self):
        return self.host_lines(self.SCREENS["cursor-on-the-suggestion"][1])

    def test_both_readers_agree_on_each_cursor_screen(self):
        for name, (ansi, runs, expected) in self.SCREENS.items():
            with self.subTest(case=name):
                self.assertEqual(REAL_SCREEN_READY("claude", self.herdr_lines(ansi)), expected)
                self.assertEqual(REAL_SCREEN_READY("claude", self.host_lines(runs)), expected)
        self.assertIsNone(REAL_SCREEN_READY("claude", peer._screen_lines(self.SUGGESTION_ANSI)))
        # A second line before the box closes is a draft's, whatever the first line shows.
        second = [("❯ ", False), ("T", False, True), ("ry", True)]
        self.assertEqual(REAL_SCREEN_READY("claude", shell_lines(second, [("  more", False)], [(RULE, False)])),
                         "draft")

    def test_an_older_host_without_cursor_evidence_keeps_the_draft(self):
        # Two-value runs carry no reverse video: the bright first letter may be typed.
        older = self.host_lines([("❯ T", False), ('ry "fix lint errors"', True)])
        self.assertEqual(REAL_SCREEN_READY("claude", older), "draft")
        self.assertEqual(session_shell.screen_lines([[["T", False, False]]]), (None, "screen-unavailable"))
        self.assertEqual(session_shell.screen_lines([[["T", False, True, True]]]), (None, "screen-unavailable"))

    def test_the_stored_claude_screens_keep_their_judgment(self):
        rule = "─" * 40
        stored = {
            "CLAUDE_EMPTY": ("\x1b[0m\x1b[38;2;80;80;80m❯ \x1b[0m\x1b[38;2;255;255;255m[earlier message]\x1b[0m\n\n"
                             + rule + "\n❯ \r\n" + rule + "\n  \U0001f4c1 proj │ main\n  bypass permissions on\n", None),
            "CLAUDE_SUGGESTION": ("❯ earlier\n\n" + rule + "\n❯ \x1b[2mrun the tests again\x1b[0m\n" + rule
                                  + "\n  footer\n", None),
            "CLAUDE_DRAFT": ("❯ earlier\n\n" + rule + "\n❯ half typed text\n" + rule + "\n  footer\n", "draft"),
            "CLAUDE_DRAFT_SECOND_LINE": (rule + "\n❯ \x1b[2m\x1b[0m\n  second line of a draft\n" + rule
                                         + "\n  footer\n", "draft"),
        }
        for name, (raw, expected) in stored.items():
            with self.subTest(screen=name):
                self.assertEqual(REAL_SCREEN_READY("claude", peer._screen_lines(raw)), expected)

    def test_retire_continue_and_delivery_read_the_same_judgment(self):
        cases = {name: (runs, expected) for name, (_ansi, runs, expected) in self.SCREENS.items()}
        cases["older-host-format"] = ([("❯ T", False), ('ry "fix lint errors"', True)], "draft")
        for name, (runs, expected) in cases.items():
            with self.subTest(case=name):
                lines = self.host_lines(runs)
                # retire
                self.select("claude")
                self.screen = lines
                rc, output = self.retire()
                if expected is None:
                    self.assertIn("retired=true reason=normal-exit", output)
                else:
                    self.assertEqual(self.actions, [])
                    self.assertIn(f"reason={expected}", output)
                # continue
                with mock.patch.object(peer, "_read_screen_reason", return_value=(lines, "")):
                    self.assertEqual(peer._continue_composer_reason(self.pane, "claude"), expected)
                # an answer or message delivered through the shell seat
                binding = self.publish("claude")
                record = {"pid": binding["native_pid"], "procStart": binding["native_start"],
                          "sessionId": binding["sid"], "status": "idle"}
                with mock.patch("fleet.session_registry.read", return_value=record), \
                     mock.patch.object(session_shell, "valid_binding", return_value=True):
                    answer = session_shell._input_readiness(
                        binding, {"screen": [list(map(list, row)) for row in (runs, [(RULE, False)])],
                                  "state": "idle"})
                self.assertEqual((answer["ready"], answer["reason"]), (expected is None, expected))

    def test_public_retire_exits_a_seat_showing_only_the_suggestion(self):
        self.select("claude")
        self.screen = self.suggestion_lines()
        rc, output = self.retire()
        self.assertEqual(rc, 0, output)
        self.assertIn("retired=true reason=normal-exit", output)
        self.assertEqual(self.actions, [("send-text", "/exit"), ("send-keys", "enter"),
                                        ("close", self.pane)])

    def test_public_retire_keeps_a_real_draft(self):
        self.select("claude")
        self.screen = shell_lines([("❯ Try this", False)], [(RULE, False)])
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

    def test_an_unread_state_is_told_apart_from_a_known_turn(self):
        unknown = peer._retire_wait_detail("agent-unknown-herdr-corroboration")
        self.assertEqual(unknown, peer._retire_wait_detail("native-turn-unknown"))
        self.assertNotEqual(unknown, peer._retire_wait_detail("agent-working-herdr-corroboration"))
        self.assertNotEqual(unknown, peer._retire_wait_detail("native-turn-busy"))
        self.assertIn("state", unknown.lower())

    def test_a_finished_retire_has_no_release_line(self):
        self.select("claude")
        rc, output = self.retire()
        self.assertEqual(rc, 0)
        self.assertNotIn("detail=", output)


EXITS = {"claude": [("send-text", "/exit"), ("send-keys", "enter")],
         "codex": [("send-keys", "ctrl+d")], "opencode": [("send-keys", "ctrl+d")]}
CODEX_EMPTY = ("recap line\n\n\n\x1b[1m\u203a\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n\n"
               "  proj \u00b7 master \u00b7 Context 85% used\n")


class RealSeatBase(SeatBase):
    """An unselected seat retired through the real readiness projection: no `ready` stand-in.

    Review 11-28 (PR #1933): the shared fixture's readiness answered `ready`, so the
    real projection's refusal of an `unknown` turn (`pane-identity-unverified`) went unseen."""

    def setUp(self):
        super().setUp()
        self.state = "unknown"
        mock.patch.object(peer, "_retire_target", side_effect=lambda *a, **k:
                          (self.state, dict(self.ident), 0, None)).start()
        mock.patch.object(peer, "_pane_readiness", REAL_PANE_READINESS).start()

    def fresh(self, harness):
        self.select(harness)
        self.publish(harness, sid=UNSELECTED)
        self.ident["session_id"] = UNSELECTED
        self.current_sid = None
        self.state, self.host_state, self.host_sid, self.host_screen = "unknown", "unknown", None, None

    def retire_as(self, target):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            rc = peer.cmd_retire(SimpleNamespace(target=target, _resume_request_id=None))
        return rc, output.getvalue()


class RealReadinessTest(RealSeatBase):
    def test_the_command_retires_an_unselected_seat_on_every_adapter(self):
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                self.fresh(harness)
                rc, output = self.retire()
                self.assertEqual(rc, 0, output)
                self.assertIn("retired=true reason=normal-exit", output)
                self.assertEqual(self.actions, EXITS[harness] + [("close", self.pane)])

    def test_the_runner_resumes_an_unselected_seat_on_every_adapter(self):
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                self.fresh(harness)
                self.screen_reason = "draft"
                _rc, output = self.retire()
                self.assertIn("reason=draft", output)
                self.assertEqual(self.actions, [])
                self.screen_reason = None
                duty = self.store.list()[0]
                with contextlib.redirect_stdout(io.StringIO()) as resumed:
                    peer._resume_retire_obligation(duty, self.store)
                self.assertIn("retired=true reason=normal-exit", resumed.getvalue())
                self.assertEqual(self.actions, EXITS[harness] + [("close", self.pane)])
                self.assertEqual(self.store.get(duty["id"])["state"], "complete")

    def test_only_the_booked_unselected_seat_in_its_own_shell_birth_has_no_turn(self):
        self.fresh("codex")
        booked = peer._retire_foreground(self.pane, "codex")
        self.assertEqual(booked["attachment"]["sid"], UNSELECTED)

        def readiness(seat, ident=None):
            observed = dict(self.ident, **(ident or {}))
            with mock.patch.object(peer, "_retire_target", return_value=("unknown", observed, 0, None)):
                return REAL_PANE_READINESS(self.pane, "unknown", expected_harness="codex",
                                           expected_sid=observed["session_id"], unselected_seat=seat)
        self.assertEqual(readiness(booked).state, "ready")
        self.assertNotEqual(readiness(None).state, "ready")
        self.assertNotEqual(readiness(booked, {"session_id": OTHER_SID}).state, "ready")
        self.assertNotEqual(readiness({**booked, "attachment": {**booked["attachment"], "sid": OTHER_SID}}).state,
                            "ready")
        self.assertNotEqual(readiness({**booked, "shell_start": "1"}).state, "ready")
        # Registered work bound to the pane's processes is still read for such a seat.
        with mock.patch.object(peer.peer_obligations, "execution_attempts_for_processes",
                               return_value=("att-bound",)), \
             mock.patch.object(peer.peer_obligations, "bound_work_for_pane",
                               return_value=((), "unknown")) as bound:
            self.assertNotEqual(readiness(booked).state, "ready")
        bound.assert_called_once()


class LastLookTest(RealSeatBase):
    """A first prompt that lands after the last screen read starts no exit (review 🔴2).

    The host selects the seat at once on the prompt's start hook and records the
    attachment only after (host.cjs `source('register')` on `setImmediate`)."""

    def test_input_after_the_last_screen_read_sends_no_key_and_leaves_the_duty_waiting(self):
        arrivals = {
            "turn-shows": lambda: setattr(self, "state", "working"),
            "host-selected-before-its-record": lambda: setattr(self, "host_sid", OTHER_SID),
            "host-turn": lambda: setattr(self, "host_state", "working"),
        }
        for name, arrive in arrivals.items():
            for harness in ("codex", "claude", "opencode"):
                with self.subTest(case=name, harness=harness):
                    self.fresh(harness)

                    def screen(*_a, seen=None, **_k):
                        if seen is not None:
                            seen["epoch"] = self.host_epoch
                        arrive()
                        return ["synthetic composer"]
                    with mock.patch.object(peer, "_read_screen", side_effect=screen):
                        _rc, output = self.retire()
                    self.assertEqual(self.actions, [], output)
                    self.assertIn("pending=true", output)
                    observation = self.store.list()[0]["observation"]
                    self.assertEqual((observation["phase"], observation["keys_sent"]), ("waiting", 0))
                    # Nothing was requested: the same retire looks again instead of
                    # answering `retire-already-pending`.
                    self.state, self.host_sid, self.host_state = "unknown", None, "unknown"
                    rc, output = self.retire()
                    self.assertEqual(rc, 0, output)
                    self.assertEqual(self.actions, EXITS[harness] + [("close", self.pane)])

    def test_a_stop_after_a_sent_key_keeps_the_exit_requested_with_its_count(self):
        self.fresh("claude")
        self.after_text = lambda: self.publish("claude", sid=UNSELECTED, incarnation="b" * 32)
        _rc, output = self.retire()
        self.assertEqual(self.actions, [("send-text", "/exit")])
        self.assertIn("reason=foreground-changed", output)
        observation = self.store.list()[0]["observation"]
        self.assertEqual((observation["phase"], observation["keys_sent"]), ("exit-requested", 1))


class HostAnswerBeforeTheKeyTest(RealSeatBase):
    """The booked attachment's own answer right before the first key (review 2 🔴②, PR #1933).

    A first prompt and its Enter reach the host before the native's start hook selects
    the seat: the booked attachment still answers, with `empty=false` and a later `epoch`
    (host.cjs counts every key it takes). A shell editor opened in that gap answers
    `focused=true`. Either sends no key and leaves the duty waiting, from the command and
    from the runner alike; the same request exits once the host shows the screen's input."""

    ARRIVALS = {  # what reached the host after the screen read, and the wait it gives
        "keys-before-the-start-hook": (lambda test: setattr(test, "host_epoch", 23), "target-changed"),
        "a-shell-editor-opened": (lambda test: setattr(test, "host_focused", True), "shell-editor-open"),
    }

    def after_the_screen(self, arrive):
        def screen(*_a, seen=None, **_k):
            if seen is not None:
                seen["epoch"] = self.host_epoch
            arrive(self)
            return ["synthetic composer"]
        return mock.patch.object(peer, "_read_screen", side_effect=screen)

    def test_input_that_reached_the_host_after_the_screen_sends_no_key(self):
        for name, (arrive, reason) in self.ARRIVALS.items():
            for harness in ("codex", "claude", "opencode"):
                with self.subTest(case=name, harness=harness):
                    self.fresh(harness)
                    self.host_epoch, self.host_focused = 7, False
                    with self.after_the_screen(arrive):
                        _rc, output = self.retire()
                    self.assertEqual(self.actions, [], output)
                    self.assertIn(f"reason={reason} ", output)
                    duty = self.store.list()[0]
                    self.assertEqual((duty["observation"]["phase"], duty["observation"]["keys_sent"]),
                                     ("waiting", 0))
                    self.host_epoch, self.host_focused = 7, False
                    with self.after_the_screen(arrive), contextlib.redirect_stdout(io.StringIO()):
                        peer._resume_retire_obligation(self.store.get(duty["id"]), self.store)
                    self.assertEqual(self.actions, [])
                    observation = self.store.get(duty["id"])["observation"]
                    self.assertEqual((observation["phase"], observation["keys_sent"], observation["reason"]),
                                     ("waiting", 0, reason))
                    self.host_epoch, self.host_focused = 7, False
                    rc, output = self.retire()
                    self.assertEqual(rc, 0, output)
                    self.assertEqual(self.actions, EXITS[harness] + [("close", self.pane)])


class HostBoxReadingTest(SeatBase):
    """A selected seat's host reads its own box: the exit starts only on `empty=true`, and an
    answer without its `epoch` or `focused` value is no answer (zero keys, the duty waits)."""

    def test_a_selected_seat_waits_unless_its_host_reads_the_box_empty(self):
        cases = {
            "host-reads-no-empty-box": lambda: setattr(self, "host_empty", False),
            "no-epoch": lambda: setattr(self, "host_epoch", None),
            "no-focused-value": lambda: setattr(self, "host_focused", None),
        }
        for name, arrange in cases.items():
            with self.subTest(case=name):
                self.select("codex")
                self.host_empty, self.host_epoch, self.host_focused = True, 7, False
                arrange()
                _rc, output = self.retire()
                self.assertEqual(self.actions, [], output)
                self.assertIn("reason=draft-unknown ", output)
                observation = self.store.list()[0]["observation"]
                self.assertEqual((observation["phase"], observation["keys_sent"]), ("waiting", 0))
        self.select("codex")
        self.host_empty, self.host_epoch, self.host_focused = True, 7, False
        rc, output = self.retire()
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.actions, EXITS["codex"] + [("close", self.pane)])


class NamedTargetScreenTest(RealSeatBase):
    """Screen and wait reason come from the booked pane and attachment (review 🔴3)."""

    def test_an_open_shell_editor_waits_alike_by_name_and_by_pane(self):
        real_run = subprocess.run

        def herdr(argv, *args, **kwargs):
            if argv[:1] == ["herdr"] and "read" in argv:  # the framed pane, read whole
                return subprocess.CompletedProcess(argv, 0, stdout=CODEX_EMPTY, stderr="")
            return real_run(argv, *args, **kwargs)
        for target in (self.pane, "probe-cx"):
            with self.subTest(target=target), \
                 mock.patch.object(peer, "_read_screen", REAL_READ_SCREEN), \
                 mock.patch.object(peer, "_screen_ready", REAL_SCREEN_READY), \
                 mock.patch.object(peer, "_retire_pane_info", side_effect=lambda pane, *a, **k:
                                   self.process_info() if pane == self.pane else None), \
                 mock.patch.object(peer.subprocess, "run", side_effect=herdr):
                self.fresh("codex")
                for other in set(session_shell.HARNESSES) - {"codex"}:  # one seat per pane, as in use
                    session_shell.binding_path(other, self.host["pid"]).unlink(missing_ok=True)
                self.host_screen = {"ok": True, "focused": True, "screen": [], "state": "unknown"}
                _rc, output = self.retire_as(target)
                self.assertEqual(self.actions, [], output)
                self.assertIn("reason=shell-editor-open", output)


class HostCursorEvidenceTest(AnswerReachesIdleShellTest):
    """The real shell host carries the cursor block as reverse video (host.cjs screenSnapshot).

    A private host in a private PTY with the rig's synthetic Claude native, which paints
    its composer's suggestion as given; no model, account or user pane is used."""
    ENTRY = ROOT / "utilities/session-shell/entry.py"
    SUGGEST = {
        "cursor-on-the-suggestion": ("\x1b[22m\x1b[7mT\x1b[27m\x1b[2mry \"fix lint errors\"", None),
        "typed-letter-then-faint-reverse-cursor": ("\x1b[22mT\x1b[2;7mr\x1b[27my \"fix lint errors\"", "draft"),
    }

    def test_the_host_screen_shows_where_the_cursor_is(self):
        for name, (suggest, expected) in self.SUGGEST.items():
            with self.subTest(case=name):
                rig = ShellRig(self, self.ENTRY, name, "claude", env={"ARI_NATIVE_SUGGEST": suggest},
                               size=(60, 240 + 42))
                try:
                    rig.start(idle=False)
                    rig.wait_for(b"fix lint errors", "the suggestion never painted")
                    screen = session_shell.control(rig.current_binding(), "screen")
                    self.assertTrue(screen.get("ok"), screen)
                    reversed_runs = [run for row in screen["screen"] for run in row if run[2:] == [True]]
                    self.assertEqual([run[0] for run in reversed_runs], ["T" if expected is None else "r"])
                    lines, why = session_shell.native_screen_reason(rig.current_binding())
                    self.assertEqual(why, "")
                    self.assertEqual(REAL_SCREEN_READY("claude", lines), expected)
                finally:
                    rig.stop()


class UnselectedHostAnswerTest(AnswerReachesIdleShellTest):
    """The real shell host for a seat no prompt has selected yet (a private host and PTY).

    OpenCode's home route is such a seat (host.cjs publishes it as `-` at start). It answers the booked `-` attachment with its `epoch` and `focused` but never reads the
    box (`empty=false`: no SID, host.cjs `refreshReadiness`), so the retire's last look takes
    the screen judged at that same epoch as the box's reading. A first prompt and its Enter
    that reach the host while the native holds its start hook move the epoch, and that look
    refuses; once the hook comes the `-` attachment gets no answer at all."""
    ENTRY = ROOT / "utilities/session-shell/entry.py"

    def test_keys_before_the_first_start_hook_move_the_epoch_the_last_look_compares(self):
        gate = self.case / "gate"
        rig = ShellRig(self, self.ENTRY, "unselected", "opencode", env={
            "ARI_NATIVE_UNSELECTED": "1", "ARI_NATIVE_MODE": "slow", "ARI_NATIVE_GATE": str(gate)})
        try:
            rig.start(selected=False)
            binding = rig.binding
            pane = "w1:p3"
            keys = ("harness", "sid", "incarnation", "socket", "host_pid", "host_start",
                    "native_pid", "native_start", "broker_pid", "broker_start")
            booked = {"pid": binding["native_pid"], "session_id": UNSELECTED,
                      "attachment": {key: binding[key] for key in keys if key in binding}}
            ident = {"harness": "opencode", "session_id": UNSELECTED, "name": "probe", "pane": pane}

            def last_look(screen_epoch):
                with mock.patch.object(peer, "_retire_foreground", return_value=booked), \
                     mock.patch.object(peer, "_retire_target", return_value=("unknown", dict(ident), 0, None)):
                    return peer._retire_input_arrived(pane, "opencode", ident, booked, booked, screen_epoch)

            seen = {}
            lines, why = session_shell.native_screen_reason(binding, seen=seen)
            self.assertEqual(why, "")
            self.assertIsInstance(seen["epoch"], int)
            status = session_shell.control(binding, "status")
            self.assertEqual((status["ok"], status["sid"], status["empty"], status["focused"], status["epoch"]),
                             (True, UNSELECTED, False, False, seen["epoch"]))
            self.assertEqual(last_look(seen["epoch"]), "")
            rig.type("slow first\r")
            rig.wait(lambda: any(row.get("taken") == "slow first" for row in rig.notes()),
                     "the native never took the first prompt")
            moved = session_shell.control(binding, "status")
            self.assertEqual((moved["ok"], moved["sid"], moved["empty"]), (True, UNSELECTED, False))
            self.assertGreater(moved["epoch"], seen["epoch"])
            self.assertEqual(last_look(seen["epoch"]), "target-changed")
            gate.touch()
            rig.wait(lambda: session_shell.control(binding, "status").get("ok") is not True,
                     "the start hook never selected the seat")
            self.assertIn(last_look(seen["epoch"]), {"foreground-changed", "foreground-unverified"})
        finally:
            rig.stop()


if __name__ == "__main__":
    unittest.main()
