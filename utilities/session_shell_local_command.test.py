"""A message reaches an idle shell window after the person ran a local command there.

A local command (`/model`) opens a menu and closes it again without starting a turn: the native
took the keys, but no prompt callback follows them. The window is idle and its composer is empty
once the menu is gone; a message sent to it is delivered then, as to any idle window, and a draft
the person left in the composer is still never typed over. A real host runs in a private PTY with a
synthetic native (``fixtures.session_shell_rig``); no model, account or user pane is used.
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


class LocalCommandDeliveryTest(AnswerReachesIdleShellTest):
    def after_local_command(self, harness, close, *, name):
        rig = ShellRig(self, ENTRY, name, harness).start()
        command = "/models" if harness == "opencode" else "/model"
        rig.type(command + "\r")
        rig.wait(lambda: command in rig.local_commands(), "the local command's menu never opened")
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
                break
            time.sleep(0.5)
        return results

    def takes_it(self, harness, close, name):
        rig = self.after_local_command(harness, close, name=name)
        ref = self.send(harness, "PEER-AFTER-LOCAL-COMMAND")
        results = self.deliver(harness, ref)
        self.assertTrue(results[-1].get("ok"), [result.get("reason") for result in results])
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

    def test_opencode_takes_a_message_after_a_local_command(self):
        self.takes_it("opencode", "\r", "opencode-chosen")

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
        # A native that took a key but has not painted since: the old screen proves nothing.
        rig = ShellRig(self, ENTRY, "unpainted").start()
        rig.type("\x1b[1;5D")   # a key the synthetic native neither echoes nor repaints for
        time.sleep(0.5)
        self.assertFalse(rig.status().get("empty"))


if __name__ == "__main__":
    unittest.main()
