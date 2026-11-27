"""A message reaches an idle shell window after the person ran a local command there.

A local command (`/model`) opens a menu and closes it again without starting a turn: the native
took the keys, but no prompt callback follows them. The window is idle and its composer is empty
once the menu is gone; a message sent to it is delivered at a courier pass that comes once the keys
have been quiet for two seconds (host.cjs ``KEY_QUIET_MS``), and a draft the person left in the
composer is still never typed over. Nothing the person is doing is typed into: an Enter whose turn
has not called back yet and a slow native's old screen prove nothing, and an OpenCode window (whose
dialogs leave its composer reading empty) waits for the next submitted turn. A real host runs in a
private PTY with a synthetic native (``fixtures.session_shell_rig``); no model, account or user pane
is used.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from fixtures.session_shell_rig import (  # noqa: E402
    ROOT, SID, AnswerReachesIdleShellTest, ShellRig, _peer_module)

ENTRY = ROOT / "utilities/session-shell/entry.py"
MENU_CLOSED = {"\r": "MODEL_CHOSEN", "\x1b": "MENU_DISMISSED"}
# host.cjs KEY_QUIET_MS: how long keys without a turn must be quiet before a screen settles them.
QUIET = 2.0


class LocalCommandDeliveryTest(AnswerReachesIdleShellTest):
    def after_local_command(self, harness, close, *, name):
        rig = ShellRig(self, ENTRY, name, harness).start()
        command = "/models" if harness == "opencode" else "/model"
        rig.type(command + "\r")
        rig.wait(lambda: command in rig.local_commands(), "the local command's menu never opened")
        self.closed_at = time.monotonic()
        rig.type(close)
        rig.wait(lambda: MENU_CLOSED[close] in rig.local_commands(), "the menu never closed")
        # No turn ran: the native's own prompt hooks never fired for the local command.
        self.assertEqual(rig.prompts(), [])
        rig.wait(lambda: rig.status().get("state") == "idle", "the window is not idle after the menu")
        return rig

    def send(self, harness, body):
        """A peer message to the window, sealed the way `peer-steward prompt` seals it."""
        peer = _peer_module()
        _text, ref = peer.prepare_peer_message(body, {"harness": "codex", "session_id": "synthetic-sender"},
                                               {"harness": harness, "session_id": SID}, defer=True)
        return ref

    def deliver(self, harness, ref, passes=4):
        """The runner's retry: one `deliver_pending` per pass, as the message duty runs it."""
        import session_shell
        results = []
        for _ in range(passes):
            results.append(session_shell.deliver_pending(ref, harness, SID))
            if results[-1].get("ok"):
                self.delivered_at = time.monotonic()
                break
            time.sleep(0.5)
        return results

    def within_quiet(self, harness, ref, since, seconds):
        """Courier passes while the keys typed at ``since`` are younger than ``seconds`` (at least one)."""
        import session_shell
        results = []
        while not results or time.monotonic() - since < seconds:
            results.append(session_shell.deliver_pending(ref, harness, SID))
            time.sleep(0.1)
        return results

    def pending_state(self, ref):
        return (_peer_module()._read_pending(ref) or {}).get("state")

    def takes_it(self, harness, close, name):
        rig = self.after_local_command(harness, close, name=name)
        ref = self.send(harness, "PEER-AFTER-LOCAL-COMMAND")
        results = self.deliver(harness, ref, passes=12)
        self.assertTrue(results[-1].get("ok"), [result.get("reason") for result in results])
        # Taken at a pass that came once the keys had been quiet; every pass before it waited.
        self.assertGreaterEqual(self.delivered_at - self.closed_at, QUIET)
        self.assertEqual({result.get("reason") for result in results[:-1]} - {"not-idle-empty"}, set())
        rig.wait(lambda: rig.prompts(), "the message never reached the native")
        self.assertIn("PEER-AFTER-LOCAL-COMMAND", rig.prompts()[0]["prompt"])
        rig.wait(lambda: (_peer_module()._read_pending(ref) or {}).get("state") == "received",
                 "the native's own receiver never recorded the message")
        # Taken once: a later pass finds it already received and sends nothing.
        self.assertFalse(self.deliver(harness, ref, passes=1)[-1].get("ok") and len(rig.prompts()) > 1)
        self.assertEqual(len(rig.prompts()), 1)

    def test_claude_takes_a_message_after_a_model_menu_was_chosen(self):
        self.takes_it("claude", "\r", "claude-chosen")

    def test_claude_takes_a_message_after_a_model_menu_was_dismissed_with_esc(self):
        self.takes_it("claude", "\x1b", "claude-esc")

    def test_opencode_keeps_a_message_for_the_next_prompt_after_a_local_command(self):
        # OpenCode's composer reads empty while a dialog or its terminal pane has the focus, so keys
        # without a turn wait for the next submitted turn there; that turn's end lets it in, once.
        rig = self.after_local_command("opencode", "\r", name="opencode-chosen")
        ref = self.send("opencode", "PEER-AFTER-LOCAL-COMMAND")
        time.sleep(QUIET + 0.3)
        results = self.deliver("opencode", ref, passes=3)
        self.assertEqual([result.get("reason") for result in results], ["not-idle-empty"] * 3)
        self.assertEqual(rig.prompts(), [])
        rig.type("hello\r")
        rig.wait(lambda: rig.prompts(), "the person's prompt never ran")
        rig.wait(rig.idle_empty, "the window did not return to idle after the person's prompt")
        results = self.deliver("opencode", ref)
        self.assertTrue(results[-1].get("ok"), [result.get("reason") for result in results])
        rig.wait(lambda: len(rig.prompts()) == 2, "the message never reached the native")
        self.assertEqual(rig.prompts()[0]["prompt"], "hello")
        self.assertIn("PEER-AFTER-LOCAL-COMMAND", rig.prompts()[1]["prompt"])
        rig.wait(lambda: self.pending_state(ref) == "received", "the plugin's receiver never recorded it")
        self.assertFalse(self.deliver("opencode", ref, passes=1)[-1].get("ok"))
        self.assertEqual(len(rig.prompts()), 2)

    def test_an_open_opencode_dialog_is_never_typed_into(self):
        # The real OpenCode 2.0.32 model dialog (fixtures/opencode-2.0.32-dialogs) over a session, in
        # a tall and a low window: its composer stays visible below it and reads empty.
        for screen, size in (("models-120x50.txt", (56, 150)), ("models-120x24.txt", (30, 150))):
            with self.subTest(screen=screen):
                rig = ShellRig(self, ENTRY, "dialog-%d" % size[0], "opencode",
                               env={"ARI_DIALOG_SCREEN": screen}, size=size)
                self.addCleanup(rig.stop)
                rig.start()
                rig.type("/models\r")
                rig.wait(lambda: "/models" in rig.local_commands(), "the dialog never opened")
                rig.wait(lambda: "Select model" in rig.screen() and "Search" in rig.screen(),
                         "the real dialog never painted")
                ref = self.send("opencode", "PEER-NOT-INTO-A-DIALOG-%d" % size[0])
                time.sleep(QUIET + 0.3)
                self.assertEqual(rig.status().get("state"), "idle")
                results = self.deliver("opencode", ref, passes=3)
                self.assertEqual([result.get("reason") for result in results], ["not-idle-empty"] * 3)
                # Nothing reached the dialog: no model was chosen by a pasted Enter, no prompt ran.
                self.assertEqual(rig.local_commands(), ["/models"])
                self.assertEqual(rig.prompts(), [])
                self.assertEqual(self.pending_state(ref), "pending")
                rig.stop()

    def test_codex_start_gate_reads_the_same_idle_empty_window(self):
        # Codex takes messages from its own queue; the shell's reading only gates whether an
        # interrupted thread may start (`_codex_shell_start_gate`). It is the same reading.
        import importlib.util
        rig = self.after_local_command("codex", "\r", name="codex-chosen")
        spec = importlib.util.spec_from_file_location("lc_steward", ROOT / "utilities/peer-steward.py")
        steward = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = steward
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(steward)
        rig.wait(lambda: steward._codex_shell_start_gate(rig.current_binding()),
                 "the Codex window's idle, empty composer never opened its start gate")

    def test_a_draft_typed_after_the_local_command_is_still_never_typed_over(self):
        rig = self.after_local_command("claude", "\r", name="claude-draft")
        rig.type("half typed")
        rig.wait_for(b"half typed", "the draft never painted")
        ref = self.send("claude", "PEER-NOT-OVER-A-DRAFT")
        results = self.deliver("claude", ref, passes=3)
        self.assertEqual([result.get("reason") for result in results], ["not-idle-empty"] * 3)
        self.assertEqual(rig.prompts(), [])
        self.assertEqual((_peer_module()._read_pending(ref) or {}).get("state"), "pending")

    def test_keys_the_native_has_not_painted_yet_are_still_unproven(self):
        # A native that took a key that may have typed but has not painted since: the old screen proves
        # nothing, however long the keys have been quiet. (A key that cannot type, such as a cursor key,
        # owes no repaint: session_shell_held_idle.test.py.)
        rig = ShellRig(self, ENTRY, "unpainted").start()
        rig.type("\x1b[13;2u")   # Shift+Enter in CSI u form, which the synthetic native neither echoes nor repaints
        time.sleep(QUIET + 0.3)
        self.assertFalse(rig.status().get("empty"))

    def test_an_enter_whose_turn_has_not_called_back_yet_is_no_proof(self):
        # The person's Enter empties the composer before the turn's prompt callback comes (the
        # native runs its hooks first): a pass in that gap waits, and that prompt's own hooks hand
        # the message over, once.
        gate = self.case / "gate-slow"
        rig = ShellRig(self, ENTRY, "slow", env={"ARI_NATIVE_MODE": "slow", "ARI_NATIVE_GATE": str(gate)}).start()
        ref = self.send("claude", "PEER-IN-GAP")
        typed = time.monotonic()
        rig.type("slow1\r")
        results = self.within_quiet("claude", ref, typed, QUIET - 1.0)
        rig.wait(lambda: "slow1" in [row.get("taken") for row in rig.notes()], "the native never took the Enter")
        self.assertEqual({result.get("reason") for result in results}, {"not-idle-empty"})
        self.assertEqual(self.pending_state(ref), "pending")
        gate.touch()
        rig.wait(lambda: rig.prompts(), "the person's prompt never ran")
        rig.wait(rig.idle_empty, "the window did not return to idle after the person's prompt")
        self.assertEqual([row["prompt"] for row in rig.prompts()], ["slow1"])
        self.assertIn("PEER-IN-GAP", rig.prompts()[0]["context"])
        self.assertFalse(self.deliver("claude", ref, passes=2)[-1].get("ok"))
        self.assertEqual(len(rig.prompts()), 1)

    def test_a_slow_natives_old_screen_before_the_key_is_no_proof(self):
        # The native repaints its old (empty) screen at once and paints the person's key a second and a
        # half later: neither that old screen nor the draft that follows lets the message in.
        rig = ShellRig(self, ENTRY, "lag", env={"ARI_NATIVE_MODE": "lag", "ARI_NATIVE_LAG": "1.5"}).start()
        ref = self.send("claude", "PEER-OVER-A-LAGGING-KEY")
        typed = time.monotonic()
        rig.type("L")
        results = self.within_quiet("claude", ref, typed, QUIET - 1.0)
        rig.wait(lambda: "\u276f L" in rig.screen(), "the lagging key never painted")
        time.sleep(max(0.0, QUIET + 0.3 - (time.monotonic() - typed)))
        results += self.deliver("claude", ref, passes=2)
        self.assertEqual({result.get("reason") for result in results}, {"not-idle-empty"})
        self.assertEqual(rig.prompts(), [])
        self.assertEqual(self.pending_state(ref), "pending")


if __name__ == "__main__":
    unittest.main()
