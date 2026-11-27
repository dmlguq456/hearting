"""A message reaches an idle shell window whose last bytes did not change its screen.

11-16 (a window that looked idle with an empty composer kept messages for hours): keys and reports
that cannot put a character into an empty composer (a lone Esc, Left, Home and page keys, a click or
a focus report, the terminal's own replies) leave no repaint behind them, so a reading that waited for one
waited for the next prompt. Once they have been quiet for two seconds (host.cjs ``KEY_QUIET_MS``),
an idle reading of the screen with an empty composer settles them and a waiting message goes in,
once. A Claude window whose turn ended while a background shell still runs (its session record says
``shell``) is idle for delivery as well. Nothing the person is doing is typed into: a draft (also
one Up brought back), an unfinished paste, an open menu and a key that may have typed something the
native has not painted yet keep the message waiting; so do Up, Down, Right and End, which can bring
text back (the native must paint once after them: a late repaint of an old prompt is not read as empty). A real host runs in a private PTY with a
synthetic native (``fixtures.session_shell_rig``); no model, account or user pane is used.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from fixtures.session_shell_rig import (  # noqa: E402
    ROOT, SID, AnswerReachesIdleShellTest, ShellRig, _peer_module)

ENTRY = ROOT / "utilities/session-shell/entry.py"
# host.cjs KEY_QUIET_MS: how long keys without a turn must be quiet before a screen settles them.
QUIET = 2.0
CLICK = "\x1b[<0;6;3M\x1b[<0;6;3m"
FOCUS = "\x1b[O\x1b[I"
REPLIES = "\x1b[24;80R\x1b[?62;22c\x1b]11;rgb:0000/0000/0000\x1b\\"


class _Delivery:
    """A message sent to the rig's window and the runner's delivery passes."""

    def send(self, harness, body):
        """A peer message to the window, sealed the way `peer-steward prompt` seals it."""
        _text, ref = _peer_module().prepare_peer_message(
            body, {"harness": "codex", "session_id": "synthetic-sender"},
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

    def pending_state(self, ref):
        return (_peer_module()._read_pending(ref) or {}).get("state")

    def press(self, rig, *keys):
        """Each key as its own read, the way a terminal sends them."""
        for key in keys:
            rig.type(key)
            time.sleep(0.05)
        self.pressed_at = time.monotonic()

    def takes_it(self, rig, body, *, after_quiet=True):
        ref = self.send("claude", body)
        results = self.deliver("claude", ref, passes=14)
        self.assertTrue(results[-1].get("ok"), [result.get("reason") for result in results])
        if after_quiet:
            # Taken at a pass that came once the keys had been quiet; every pass before it waited.
            self.assertGreaterEqual(self.delivered_at - self.pressed_at, QUIET - 0.05)
        self.assertEqual({result.get("reason") for result in results[:-1]} - {"not-idle-empty"}, set())
        rig.wait(lambda: any(body in row["prompt"] for row in rig.prompts()), "the message never reached the native")
        rig.wait(lambda: self.pending_state(ref) == "received", "the native's own receiver never recorded it")
        # Taken once: a later pass finds it already received and sends nothing.
        count = len(rig.prompts())
        self.assertFalse(self.deliver("claude", ref, passes=1)[-1].get("ok"))
        self.assertEqual(len(rig.prompts()), count)
        return ref

    def keeps_it(self, rig, body, harness="claude"):
        before = len(rig.prompts())
        ref = self.send(harness, body)
        time.sleep(max(0.0, QUIET + 0.3 - (time.monotonic() - self.pressed_at)))
        results = self.deliver(harness, ref, passes=3)
        self.assertEqual([result.get("reason") for result in results], ["not-idle-empty"] * 3)
        self.assertEqual(len(rig.prompts()), before)
        self.assertEqual(self.pending_state(ref), "pending")
        return ref


class QuietBytesDeliveryTest(_Delivery, AnswerReachesIdleShellTest):
    # ---- (a) bytes that do not change the screen ----
    def test_a_click_the_native_does_not_read_keeps_the_window_ready(self):
        rig = ShellRig(self, ENTRY, "click-unread").start()
        self.press(rig, CLICK)
        self.takes_it(rig, "PEER-AFTER-CLICK-UNREAD", after_quiet=False)

    def test_a_message_goes_in_after_a_click_the_native_reads(self):
        rig = ShellRig(self, ENTRY, "click", env={"ARI_NATIVE_MOUSE": "1"}).start()
        self.press(rig, CLICK)
        self.takes_it(rig, "PEER-AFTER-CLICK")

    def test_focus_reports_keep_the_window_ready(self):
        rig = ShellRig(self, ENTRY, "focus").start()
        self.press(rig, "\x1b[O", "\x1b[I")
        self.takes_it(rig, "PEER-AFTER-FOCUS", after_quiet=False)

    def test_a_message_goes_in_after_a_lone_esc(self):
        rig = ShellRig(self, ENTRY, "esc").start()
        self.press(rig, "\x1b")
        self.takes_it(rig, "PEER-AFTER-ESC")

    def test_a_message_goes_in_after_esc_twice(self):
        rig = ShellRig(self, ENTRY, "esc-twice").start()
        self.press(rig, "\x1b", "\x1b")
        self.takes_it(rig, "PEER-AFTER-ESC-TWICE")

    def test_a_message_goes_in_after_cursor_keys(self):
        rig = ShellRig(self, ENTRY, "arrows").start()
        self.press(rig, "\x1b[D", "\x1b[1;5D", "\x1b[H", "\x1b[5~", "\x1b[Z")
        self.takes_it(rig, "PEER-AFTER-ARROWS")

    def keeps_after_keys(self, name, *keys):
        # (SS3 forms are read by the vm test below: the synthetic native types `ESC O x` as text.)
        # Down, Right and End may fill the composer (a history entry, a suggestion). A native that
        # paints nothing after them has not shown what they did: the message waits, as before 11-16.
        rig = ShellRig(self, ENTRY, name).start()
        self.press(rig, *keys)
        self.keeps_it(rig, "PEER-NOT-AFTER-" + name.upper())

    def test_down_the_native_does_not_paint_after_keeps_the_message_waiting(self):
        self.keeps_after_keys("down", "\x1b[B", "\x1b[1;2B")

    def test_right_the_native_does_not_paint_after_keeps_the_message_waiting(self):
        self.keeps_after_keys("right", "\x1b[C", "\x1b[1;5C")

    def test_end_the_native_does_not_paint_after_keeps_the_message_waiting(self):
        self.keeps_after_keys("end", "\x1b[F", "\x1b[4~", "\x1b[8~", "\x1b[1;5F")

    def test_up_that_the_native_paints_late_never_lets_a_message_over_the_prompt_it_brings_back(self):
        # The native brings the last prompt back four seconds after Up and paints nothing before. The
        # two quiet seconds must not stand for that key: the message stays out before the paint and,
        # once the old prompt shows, over it (11-16 review: the message was appended to it at 2s).
        rig = ShellRig(self, ENTRY, "up-late", env={"ARI_NATIVE_MODE": "slow-up", "ARI_NATIVE_LAG": "4"}).start()
        self.press(rig, "\x1b[A")
        self.keeps_it(rig, "PEER-NOT-BEFORE-THE-LATE-PAINT")
        rig.wait(lambda: "❯ warm" in rig.screen(), "Up never brought the last prompt back")
        self.keeps_it(rig, "PEER-NOT-OVER-THE-LATE-PROMPT")
        self.assertEqual([row["prompt"] for row in rig.prompts()], [])

    def test_a_message_goes_in_after_the_terminals_own_replies(self):
        rig = ShellRig(self, ENTRY, "replies").start()
        self.press(rig, REPLIES)
        self.takes_it(rig, "PEER-AFTER-REPLIES")

    def test_a_focus_report_after_a_local_command_does_not_hold_the_message(self):
        # `/model`, Esc: the native closed its menu and painted; the terminal then reported a focus
        # change. That report is no key: the keys before it still settle once they have been quiet.
        rig = ShellRig(self, ENTRY, "menu-focus").start()
        rig.type("/model\r")
        rig.wait(lambda: "/model" in rig.local_commands(), "the menu never opened")
        self.press(rig, "\x1b")
        rig.wait(lambda: "MENU_DISMISSED" in rig.local_commands(), "the menu never closed")
        time.sleep(0.3)
        rig.type(FOCUS)
        self.takes_it(rig, "PEER-AFTER-MENU-FOCUS")

    # ---- (b) a Claude turn that ended with a background shell still running ----
    def test_a_message_goes_in_while_a_background_shell_runs(self):
        rig = ShellRig(self, ENTRY, "bgshell").start()
        rig.type("bgshell\r")
        rig.wait(lambda: rig.prompts(), "the turn never ran")
        record = self.case / "native-claude" / "sessions"
        rig.wait(lambda: any(json.loads(path.read_text()).get("status") == "shell" for path in record.glob("*.json")),
                 "the session record never said shell")
        self.pressed_at = time.monotonic()
        self.takes_it(rig, "PEER-BESIDE-A-BACKGROUND-SHELL", after_quiet=False)

    # ---- what is never typed into ----
    def test_a_prompt_brought_back_with_up_is_a_draft(self):
        rig = ShellRig(self, ENTRY, "up").start()
        self.press(rig, "\x1b[A")
        rig.wait(lambda: "❯ warm" in rig.screen(), "Up never brought the last prompt back")
        self.keeps_it(rig, "PEER-NOT-OVER-HISTORY")

    def test_cursor_keys_in_an_open_menu_never_let_a_message_into_it(self):
        rig = ShellRig(self, ENTRY, "menu-arrows").start()
        rig.type("/model\r")
        rig.wait(lambda: "/model" in rig.local_commands(), "the menu never opened")
        self.press(rig, "\x1b[B", "\x1b[A")
        self.keeps_it(rig, "PEER-NOT-INTO-A-MENU")
        self.assertEqual(rig.local_commands(), ["/model"])

    def test_an_unfinished_paste_is_never_typed_into(self):
        rig = ShellRig(self, ENTRY, "paste").start()
        self.press(rig, "\x1b[200~half pasted")
        self.keeps_it(rig, "PEER-NOT-INTO-A-PASTE")

    def test_cursor_keys_over_a_draft_never_let_a_message_over_it(self):
        rig = ShellRig(self, ENTRY, "draft-arrows").start()
        rig.type("half typed")
        rig.wait_for(b"half typed", "the draft never painted")
        self.press(rig, "\x1b[D", "\x1b[D", "\x1b")
        self.keeps_it(rig, "PEER-NOT-OVER-A-DRAFT")

    def test_a_draft_beside_a_background_shell_is_never_typed_over(self):
        rig = ShellRig(self, ENTRY, "bgshell-draft").start()
        rig.type("bgshell\r")
        rig.wait(lambda: rig.prompts(), "the turn never ran")
        rig.type("half typed")
        rig.wait_for(b"half typed", "the draft never painted")
        self.pressed_at = time.monotonic()
        self.keeps_it(rig, "PEER-NOT-OVER-A-DRAFT-BESIDE-A-SHELL")

    # ---- the other harnesses ----
    def test_codex_start_gate_opens_after_a_lone_esc(self):
        # Codex takes messages from its own queue; the shell's reading gates only whether an
        # interrupted thread may start (`_codex_shell_start_gate`). It is the same reading.
        import importlib.util
        rig = ShellRig(self, ENTRY, "codex-esc", "codex").start()
        self.press(rig, "\x1b")
        spec = importlib.util.spec_from_file_location("hi_steward", ROOT / "utilities/peer-steward.py")
        steward = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = steward
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(steward)
        rig.wait(lambda: steward._codex_shell_start_gate(rig.current_binding()),
                 "the Codex window's idle, empty composer never opened its start gate")

    def test_opencode_keeps_a_message_for_the_next_prompt_after_a_lone_esc(self):
        # OpenCode's composer reads empty while a dialog or its terminal pane has the focus, so keys
        # without a turn wait for the next submitted turn there (unchanged).
        rig = ShellRig(self, ENTRY, "opencode-esc", "opencode").start()
        self.press(rig, "\x1b")
        self.keeps_it(rig, "PEER-OPENCODE-AFTER-ESC", harness="opencode")


class HeldReasonTest(_Delivery, AnswerReachesIdleShellTest):
    """The sender's record says why the window keeps its message: the receiving host's own word."""

    def hold(self, rig):
        return rig.status().get("hold")

    def test_the_host_names_why_it_holds(self):
        rig = ShellRig(self, ENTRY, "why").start()
        self.assertEqual(self.hold(rig), "")
        rig.type("half typed")
        rig.wait_for(b"half typed", "the draft never painted")
        self.assertEqual(self.hold(rig), "unsettled")   # its keys are not quiet yet
        time.sleep(QUIET + 0.2)
        self.assertEqual(self.hold(rig), "draft")
        ref = self.send("claude", "PEER-HELD-BY-A-DRAFT")
        result = self.deliver("claude", ref, passes=1)[-1]
        self.assertEqual((result.get("reason"), result.get("hold")), ("not-idle-empty", "draft"))
        rig.stop()   # one window per session at a time
        menu = ShellRig(self, ENTRY, "why-menu").start()
        menu.type("/model\r")
        menu.wait(lambda: "/model" in menu.local_commands(), "the menu never opened")
        time.sleep(QUIET + 0.2)
        self.assertEqual(self.hold(menu), "form")
        menu.stop()
        paste = ShellRig(self, ENTRY, "why-paste").start()
        paste.type("\x1b[200~half pasted")
        time.sleep(0.3)
        self.assertEqual(self.hold(paste), "paste")

    def test_the_senders_record_and_its_words_keep_why(self):
        import peer_request
        rig = ShellRig(self, ENTRY, "record").start()
        rig.type("half typed")
        rig.wait_for(b"half typed", "the draft never painted")
        time.sleep(QUIET + 0.2)
        ref = self.send("claude", "PEER-RECORD-HELD")
        self.courier(passes=0)   # loads the runner
        steward = sys.modules["ari_courier"]
        steward._schedule_message_obligation(_peer_module()._read_pending(ref), "target")
        self.courier()
        row = _peer_module()._read_pending(ref)
        self.assertEqual((row["state"], row["held"]["reason"]), ("pending", "draft"))
        self.assertEqual(peer_request.delivery_word(row),
                         "awaiting delivery · text stands in its input · it goes in once that is sent or cleared")
        stamp = row["held"]["at"]
        self.courier()   # an unchanged reason is not written again
        self.assertEqual(_peer_module()._read_pending(ref)["held"]["at"], stamp)
        # The one-hour notice to the sender carries the same words.
        with mock.patch.object(steward.peer_message, "prepare_peer_message", return_value=("", "e" * 32)), \
                mock.patch.object(steward, "_schedule_message_obligation"):
            steward._ensure_sender_delay_notice(row)
        body = steward.peer_obligations.ObligationStore().get("delay-" + ref)["intent"]["body"]
        self.assertIn("is still pending delivery; it was sent at ", body)
        self.assertTrue(body.endswith(" UTC. text stands in its input · it goes in once that is sent or cleared."), body)
        # The person sends their draft: that prompt's hooks hand the message over, and the word follows.
        rig.type("\r")
        rig.wait(lambda: rig.prompts(), "the person's prompt never ran")
        self.assertIn("PEER-RECORD-HELD", rig.prompts()[0]["context"])
        self.courier()
        row = _peer_module()._read_pending(ref)
        self.assertEqual((row["state"], peer_request.delivery_word(row)), ("received", "seen"), row)

    def test_a_window_outside_the_shell_and_an_older_host_are_named(self):
        import peer_request
        import session_shell
        with mock.patch.object(session_shell, "release_behind", return_value=True):
            self.assertEqual(session_shell._hold({}, {"ok": True, "state": "idle", "empty": False}), "old-shell")
            self.assertEqual(session_shell._hold({}, {"ok": True, "state": "working"}), "busy")
            self.assertEqual(session_shell._hold({}, {"ok": True, "state": "blocked"}), "form")
            self.assertEqual(session_shell._hold({}, {"ok": True, "state": "idle", "hold": "draft"}), "draft")
        row = {"state": "pending", "held": {"reason": "no-shell"}}
        self.assertEqual(peer_request.delivery_word(row),
                         "awaiting delivery · outside the shell · type once in that window to deliver it")
        self.assertEqual(peer_request.delivery_word({"state": "pending"}), "awaiting delivery")
        for state, word in (("unverified", "sent"), ("queued", "delivered"), ("received", "seen")):
            self.assertEqual(peer_request.delivery_word({"state": state, "held": {"reason": "draft"}}), word)
        catalog = json.loads((ROOT / "tools/i18n/messages.json").read_text())["messages"]
        for keys in peer_request._HOLD_WORDS.values():
            for key in keys:
                self.assertTrue(catalog[key]["en"] and catalog[key]["ko"], key)


class QuietKeyReadingTest(unittest.TestCase):
    """host.cjs's own reading, with a clock the test moves: which bytes owe a repaint."""

    def test_keys_that_cannot_type_settle_once_quiet_and_others_wait_for_a_repaint(self):
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),a=require('node:assert/strict');
const text=fs.readFileSync(process.argv[1],'utf8');
const piece=name=>{const begin=text.search(new RegExp('^(?:async )?function '+name+'\\(','m'));return text.slice(begin,text.indexOf('\n}',begin)+2);};
const code=text.match(/^const QUIET_KEY=.*;$/m)[0]+'\n'+piece('refreshReadiness')+'\n'+piece('observeInput');
function make(harness='claude'){
  const s={Buffer,now:0,epoch:0,nativeEpoch:0,lastEnter:-1,submitted:-1,typed:0,empty:true,keyTail:'',paste:false,relay:false,
    currentDelivery:null,sid:'sid',sequence:0,selection:0,screenRevision:0,keyRevision:-1,keyAt:0,state:'idle',harness,
    KEY_QUIET_MS:2000,lastHold:'',HOLD_OF:{},asked:0,screenSnapshot:()=>[],queueDisplay:()=>{},queueProjection:()=>{}};
  s.performance={now:()=>s.now};s.source=async()=>{s.asked++;return {ok:true,ready:true};};
  vm.createContext(s);vm.runInContext(code,s);return s;
}
const after=async(keys,{harness,paint=false}={})=>{
  const s=make(harness);for(const key of keys)s.observeInput(Buffer.from(key,'latin1'));
  if(paint)s.screenRevision++;
  s.now=1999;await s.refreshReadiness();const early=s.empty;
  s.now=2000;await s.refreshReadiness();return {early,late:s.empty,s};
};
(async()=>{
  for(const keys of [['\x1b'],['\x1b','\x1b'],['\x1b[D','\x1bOD','\x1b[1;2D'],['\x1b[1;5D','\x1b[H','\x1b[3~','\x1b[5~','\x1b[6~','\x1b[2~','\x1bOH','\x1b[15~','\x1bOP','\x1b[Z'],
      ['\x1b[<0;6;3M\x1b[<0;6;3m'],['\x1b[24;80R\x1b[?62;22c\x1b[>0;95;0c\x1b[?2026;2$y'],
      ['\x1b]11;rgb:0000/0000/0000\x1b\\'],['\x1b]11;rgb:0000/','0000/0000\x07'],['\x1b[O','\x1b','\x1b[I']]){
    const r=await after(keys);a.equal(r.early,false,JSON.stringify(keys)+' within the quiet time');
    a.equal(r.late,true,JSON.stringify(keys)+' settles once quiet, with no repaint');
    a.equal(r.s.keyTail,'');a.equal(r.s.submitted,r.s.nativeEpoch);
  }
  // Up, Down, Right and End may bring text into the composer (history, a suggestion): they owe a repaint.
  for(const keys of [['\x1b[A'],['\x1b[B'],['\x1b[C'],['\x1b[F'],['\x1bOA'],['\x1bOB'],['\x1bOC'],['\x1bOF'],['\x1b[4~'],['\x1b[8~'],
      ['\x1b[1;5A'],['\x1b[1;2B'],['\x1b[1;5C'],['\x1b[1;3F'],['\x1b[1;5C','\x1b[D']]){
    const r=await after(keys);a.equal(r.late,false,JSON.stringify(keys)+' may have recalled text: no repaint, no proof');
    const p=await after(keys,{paint:true});a.equal(p.late,true,JSON.stringify(keys)+' a repaint after it and the quiet time');
  }
  for(const keys of [['x'],['\t'],['\x7f'],['\x15'],['\x19'],['\x1b[13;2u'],['\x1b[27;5;13~'],['\x1bOM'],['\x1bb','c'],
      ['\x1b[200~','x','\x1b[201~'],['\x1b]11;x\r'],['\x1b[D','a','\x1b[C']]){
    const r=await after(keys);a.equal(r.late,false,JSON.stringify(keys)+' may have typed: no repaint, no proof');
    const p=await after(keys,{paint:true});a.equal(p.late,true,JSON.stringify(keys)+' a repaint after it and the quiet time');
  }
  const paste=await after(['\x1b[200~half']);a.equal(paste.late,false,'an unfinished paste');a.equal(paste.s.asked,0);
  const opencode=await after(['\x1b'],{harness:'opencode',paint:true});a.equal(opencode.late,false,'OpenCode waits for its next turn');
  const split=make();split.observeInput(Buffer.from('\x1b]11;x\x1b[200~'));a.equal(split.paste,true,'a paste starting inside a reply is a paste');
  const enter=make();enter.observeInput(Buffer.from('\x1b]hello\r'));a.equal(enter.lastEnter,enter.nativeEpoch,'an Enter ends an unfinished reply');
  const focus=make();focus.observeInput(Buffer.from('\x1b[I'));a.equal(focus.keyAt,0,'a focus report restarts no quiet time');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        import subprocess
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/host.cjs")],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


class ShellRecordReadinessTest(unittest.TestCase):
    """`_input_readiness` reads Claude's own session record: `shell` is a turn that has ended."""

    def readiness(self, status):
        sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
        import session_shell
        binding = {"harness": "claude", "sid": SID, "native_pid": 4242, "native_start": "77"}
        record = {"pid": 4242, "procStart": "77", "sessionId": SID, "status": status}
        screen = [[["❯ ", False]], [["─" * 18, False]]]
        with mock.patch("fleet.session_registry.read", return_value=record), \
                mock.patch.object(session_shell, "valid_binding", return_value=True):
            return session_shell._input_readiness(binding, {"screen": screen, "state": "idle"})

    def test_a_turn_that_ended_beside_a_background_shell_is_idle(self):
        self.assertEqual(self.readiness("shell"), {"ok": True, "idle": True, "ready": True, "reason": None})

    def test_a_running_or_asking_turn_is_not(self):
        for status in ("busy", "waiting", "exited", None):
            with self.subTest(status=status):
                result = self.readiness(status)
                self.assertFalse(result["ready"])
                self.assertEqual(result["reason"], "native-idle-unobserved")


if __name__ == "__main__":
    unittest.main()
