"""An idle shell window takes a message whatever its size, and its form/draft judgment reads as before.

11-27: every readiness request carried the whole native viewport, each row padded to the window's
width, and a request past 64 KiB was refused before the backend saw it; the refusal read as the
hold ``unobserved`` ("its rest is not confirmed"). A 121 x 537 window's empty screen alone was
65 KB, so its idle session took no message (nor moved to a new release) for hours. The host now
sends each row up to its last non-blank cell and, for a screen still too large, its top rows
through the first with text and the bottom rows that fit; the reader judges that only when the
judgment reads the same on it as on the whole screen, and otherwise says ``screen-large``.
Review 1127 (fix 1): a screen is never cut. It is carried whole, each row up to its last non-blank
cell, in a line of up to 1 MiB for the screen requests alone; past that it is ``screen-large`` and
nothing is typed. The form/draft judgment is main's, so a wide window is judged as a small one.

Two parts: the judgment over every cut a host can make of synthetic screens (no process), and a
real host in a private PTY with the synthetic native of ``fixtures.session_shell_rig`` at five
native sizes, its screen empty or filled to the width (rules, Hangul, dim text). No model,
account or user pane is used.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import json
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from fixtures.session_shell_rig import (  # noqa: E402
    ROOT, SID, AnswerReachesIdleShellTest, ShellRig, _peer_module)

sys.path.insert(1, str(ROOT / "tools"))
import peer_steward_duties as duties  # noqa: E402
import session_shell  # noqa: E402

ENTRY = ROOT / "utilities/session-shell/entry.py"
# The native sizes of the windows seen held (rows, columns); the side panel takes PANEL more columns.
SIZES = ((60, 240), (121, 267), (121, 507), (121, 537), (200, 600), (300, 800))
PANEL = 42
SCREEN_BYTES = session_shell.MAX_SCREEN_BYTES


# ---- synthetic screens: rows of (char, faint) cells, padded to the width as the old host sent them ----
def row(text, cols, faint=False, tail_faint=None):
    cells = [(char, faint) for char in text]
    width = sum(2 if "가" <= char <= "힣" else 1 for char in text)
    return cells + [(" ", faint if tail_faint is None else tail_faint)] * max(0, cols - width)


def transcript(rows, cols, kind):
    """``rows`` rows above the input box: blank, or filled to the width as a busy transcript."""
    if kind == "blank":
        return [row("", cols) for _ in range(rows)]
    out = []
    for n in range(rows):
        if n % 4 == 0:
            out.append(row("─" * cols, cols))
        elif n % 4 == 1:
            out.append(row("한글" * (cols // 4), cols))
        elif n % 4 == 2:
            out.append(row(("dim transcript " * cols)[:cols], cols, faint=True))
        else:
            out.append(row("  plain words and more words", cols, tail_faint=True))
    return out


def bottoms(harness, cols):
    """The input box states each harness shows, by name: what the judgment must keep telling apart."""
    rule = row("─" * min(cols, 80), cols)
    status = row("  synthetic-model · ~/project", cols, faint=True)
    if harness == "claude":
        return {
            "empty": [rule, row("❯ ", cols), rule, status],
            "draft": [rule, row("❯ half typed", cols), rule, status],
            "draft-hangul": [rule, row("❯ 한글 조합 중", cols), rule, status],
            "second-line": [rule, row("❯ first", cols), row("  second line", cols), rule, status],
            "suggestion": [rule, row("❯ ", cols) [:2] + row("try a dim suggestion", cols - 2, faint=True), rule, status],
            "form": [row("Select model", cols), row("❯ 1. Default", cols), row("  2. Synthetic", cols),
                     row("Enter to select · Esc to cancel", cols)],
            "no-box": [row("working...", cols), status],
            # A local command's menu in place of the composer: no marker row at all.
            "menu": [row("Select model", cols), row("  1. Default", cols), row("  2. Synthetic", cols),
                     row("Enter to select \u00b7 Esc to cancel", cols)],
        }
    if harness == "codex":
        return {
            "empty": [row("› ", cols), row("", cols), status],
            "draft": [row("› half typed", cols), row("", cols), status],
            "draft-hangul": [row("› 한글", cols), row("", cols), status],
            "suggestion": [row("› ", cols)[:2] + row("Explain this codebase", cols - 2, faint=True), row("", cols), status],
            "popup": [row("› ", cols), row("  /model  choose what model", cols), status],
            "location": [row("Where should the new conversation run?", cols), row("› 1. Use current git worktree", cols),
                         row("  2. Create new git worktree", cols), row("Press enter to confirm", cols)],
        }
    return {
        "empty": [row("┃  ", cols), row("┃  ", cols), row("┃  Build  synthetic-model", cols), status],
        "draft": [row("┃  half typed", cols), row("┃  ", cols), row("┃  Build  synthetic-model", cols), status],
        "draft-hangul": [row("┃  한글", cols), row("┃  Build  synthetic-model", cols), status],
        # A box taller than what a cut keeps: only its first row holds text.
        "tall-draft": [row("┃  top line", cols)] + [row("┃  ", cols) for _ in range(60)]
                      + [row("┃  Build  synthetic-model", cols), status],
        "form": [row("Select model", cols), row("  Default", cols), row("esc to cancel  enter to select", cols)],
        # A dialog whose keys line heads a long list, over no box.
        "dialog": [row("Select model", cols), row("esc to cancel  enter to select", cols)]
                  + [row("  synthetic-model-%d" % n, cols) for n in range(10)],
    }


def screens(harness, rows, cols):
    """(name, rows) for each box state over each transcript; some put a box-like row high in the transcript."""
    old = {"claude": "❯ an old prompt", "codex": "› an old prompt", "opencode": "┃  an old box"}[harness]
    for state, box in bottoms(harness, cols).items():
        for kind in ("blank", "dense"):
            above = transcript(rows - len(box), cols, kind)
            yield "%s/%s" % (state, kind), above + box
            if len(above) > 3:
                high = list(above)
                high[1] = row(old, cols)
                yield "%s/%s/old-row-high" % (state, kind), high + box
    if harness == "claude":
        # A form's words in the last 15 rows with text, above a box that reads empty.
        box = bottoms(harness, cols)["empty"]
        above = transcript(rows - len(box) - 6, cols, "dense") + [row("Enter to select · Esc to cancel", cols)]
        yield "form-words-above-box", above + [row("", cols)] * 5 + box
    if harness == "codex":
        # Codex's hook review owns the top of its screen.
        review = [row("Hooks need review", cols), row("2 hooks are new or changed.", cols), row("› 1. Review hooks", cols),
                  row("  2. Trust all and continue", cols), row("  3. Continue without trusting", cols)]
        yield "hook-review", review + [row("", cols)] * (rows - len(review))
        yield "hook-review-tall", review + transcript(rows - len(review) - 3, cols, "dense") + bottoms(harness, cols)["empty"]


def wire(lines):
    """A screen as the host carries it: runs of [text, dim] per row."""
    out = []
    for cells in lines:
        runs = []
        for char, faint in cells:
            if runs and runs[-1][1] == faint:
                runs[-1][0] += char
            else:
                runs.append([char, faint])
        out.append(runs)
    return out


def trimmed(lines):
    out = [list(cells) for cells in lines]
    for cells in out:
        while cells and cells[-1][0] == " ":
            cells.pop()
    return out


class WholeScreensTest(unittest.TestCase):
    """What the host carries reads as the whole screen; nothing else is read."""

    def test_row_ends_carry_nothing_the_judgment_reads(self):
        for harness in ("claude", "codex", "opencode"):
            for rows, cols in SIZES[:5]:
                for name, lines in screens(harness, rows, cols):
                    with self.subTest(harness=harness, size=(rows, cols), screen=name):
                        got, why = session_shell.screen_lines(wire(trimmed(lines)))
                        self.assertEqual(why, "")
                        self.assertEqual(duties._screen_ready(harness, got), duties._screen_ready(harness, lines))

    def test_a_carry_with_rows_left_off_is_unavailable(self):
        lines = wire(trimmed(transcript(30, 80, "dense") + bottoms("claude", 80)["empty"]))
        for screen in ("x", [None] + lines, lines[:3] + [None] + lines[4:], [["a"]], [[["a", 1]]], {"rows": []}):
            with self.subTest(screen=str(screen)[:40]):
                self.assertEqual(session_shell.screen_lines(screen), (None, "screen-unavailable"))

    def test_the_screen_limit_is_one_number_on_both_sides(self):
        source = (ROOT / "utilities/session-shell/host.cjs").read_text()
        self.assertIn("const SCREEN_BYTES=1024*1024;", source)
        self.assertIn("SCREEN_OPS=new Set(['readiness','payload']),limit=SCREEN_OPS.has(op)?1024*1024:65536;", source)
        self.assertEqual(session_shell.MAX_SCREEN_BYTES, 1024 * 1024)
        self.assertEqual(session_shell.SCREEN_REQUESTS, {"readiness", "payload"})


class HostCarryTest(unittest.TestCase):
    """host.cjs's own code for what a request carries, on a synthetic terminal (no process)."""

    def node(self, code):
        import subprocess
        done = subprocess.run(["node", "-e", code, str(ROOT / "utilities/session-shell/host.cjs")],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_screen_request_past_the_limit_is_refused_as_screen_large_and_never_sent(self):
        # Review 1127 🔴3/🔴7: the whole frame is counted once; past it nothing is read (ok:false), so no
        # caller takes it as an answer about the screen (a form gone, an empty box).
        self.node(r"""
const fs=require('fs'),a=require('node:assert/strict');
const src=fs.readFileSync(process.argv[1],'utf8'),s=src.slice(src.indexOf('function source('),src.indexOf('function readAction('));
const written=[];
const f=new Function('Buffer','setTimeout','clearTimeout',`let callID=0;const calls=new Map(),sid='s',harness='claude';
  const backend={exitCode:null,stdin:{write:frame=>written.push(frame)}};function binding(){return {harness};}
  function readAction(){}function projectionMissed(){}function projectionAnswered(){}
  ${s};return source;`);
const source=f(Buffer,()=>0,()=>{});
(async()=>{
  source('readiness',{screen:[[['x'.repeat(900*1024),false]]]});a.equal(written.length,1,'under 1 MiB goes');
  a.deepEqual(await source('readiness',{screen:[[['x'.repeat(1024*1024),false]]]}),{ok:false,reason:'screen-large'});
  a.deepEqual(await source('payload',{screen:[[['x'.repeat(1024*1024),false]]]}),{ok:false,reason:'screen-large'});
  a.deepEqual(await source('projection',{x:'y'.repeat(70000)}),{ok:false,reason:'source-request-too-large'});
  a.equal(written.length,1,'nothing past its limit is written');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_the_codex_model_reading_carries_the_short_evidence_or_asks_nothing(self):
        # Review 1127 🔴7: the footer reading asks with the native selection's own short evidence; a
        # screen with no composer mark, or a long draft under it, asks nothing.
        self.node(r"""
const fs=require('fs'),a=require('node:assert/strict');
const src=fs.readFileSync(process.argv[1],'utf8');
const piece=(from,to)=>src.slice(src.indexOf(from),src.indexOf(to,src.indexOf(from)));
const code=piece('function nativeSelectionFrame()','let checkingNativeSelection=')+piece('function displayModelKey(','let paintedCursor=');
const uuid='01a1c650-6f32-7880-bb70-815b30d8bd06';
const run=rows=>{const asked=[];const terminal={rows:rows.length,buffer:{active:{viewportY:0,getLine:y=>({translateToString:()=>rows[y]})}}};
  new Function('terminal','asked',`let harness='codex',modelReading=false,native={start:'1'},nativeSelected={},modelReadKey='',selection=0,
    screenRevision=0,epoch=0,relay=false;function source(op,x){asked.push(JSON.stringify(x).length);return {then:()=>({finally:()=>{}})};}
    ${code};observeSelectedModel(Array.from({length:terminal.rows},(_,i)=>terminal.buffer.active.getLine(i).translateToString(true)));`)(terminal,asked);
  return asked;};
const footer=[uuid+' · Context 12% used','? for shortcuts'];
a.equal(run(['x'.repeat(800),'› ','',...footer]).length,1,'a composer and its footer: asked');
a.ok(run(['x'.repeat(800),'› ','',...footer])[0]<400,'with the short evidence only');
a.deepEqual(run(['› '+'x'.repeat(790),...Array(95).fill('  '+'y'.repeat(790)),'',...footer]),[],'a long draft under the mark');
a.deepEqual(run([...Array(100).fill('z'.repeat(800)),...footer]),[],'no composer mark');
""")


class SentLineTest(unittest.TestCase):
    def test_the_panel_and_fleet_say_why_a_sent_answer_waits_on_a_large_screen(self):
        from fleet.session_block import sent_answer_line
        from fleet.config import ui_text
        words = ui_text("peer.delivery.hold_screen_large")
        self.assertIn(words, "".join(text for text, _style in sent_answer_line(["A"], wait="screen-large")))
        self.assertIn(ui_text("peer.delivery.hold_old_shell_large"),
                      "".join(text for text, _style in sent_answer_line(["A"], wait="old-shell-large")))
        code = r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]);
let hold='screen-large';
const block=create({changed(){},answer:async()=>({state:'submitted'}),action(){},hold:()=>hold});
const sent={id:'sent',revision:'r0',answerable:false,request_state:'submitted',sent:['A'],questions:[{id:'q',question:'Q',choices:['A']}]};
block.update([sent],'',{labels:{hold_screen_large:'TOO-LARGE'}});
assert.deepEqual(block.sent().map(r=>r.map(x=>x.text).join('')),['✓ answer sent · A · TOO-LARGE']);
hold='';assert.deepEqual(block.sent().map(r=>r.map(x=>x.text).join('')),['✓ answer sent · A']);
"""
        import subprocess
        done = subprocess.run(["node", "-e", code, str(ROOT / "utilities/session-shell/block-input.cjs")],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)


# ---- a real host ----
class WideWindowDeliveryTest(AnswerReachesIdleShellTest):
    @contextlib.contextmanager
    def window(self, harness, size, name, idle=True, **env):
        """One window at a time, ended on leaving: a window left up would hide the next one's binding."""
        rig = ShellRig(self, ENTRY, name, harness, env=env, size=(size[0], size[1] + PANEL))
        try:
            yield rig.start(idle=idle)
        finally:
            rig.stop()

    def screen_bytes(self, rig):
        screen = rig.session_shell.control(rig.current_binding(), "screen")
        self.assertTrue(screen.get("ok"), screen)
        return len(json.dumps(screen["screen"], ensure_ascii=False, separators=(",", ":")).encode())

    def send(self, harness, body):
        _text, ref = _peer_module().prepare_peer_message(
            body, {"harness": "codex", "session_id": "synthetic-sender"}, {"harness": harness, "session_id": SID}, defer=True)
        return ref

    def deliver(self, harness, ref, passes=6):
        results = []
        for _ in range(passes):
            results.append(session_shell.deliver_pending(ref, harness, SID))
            if results[-1].get("ok"):
                break
            time.sleep(0.5)
        return results

    def test_an_idle_window_of_each_size_takes_a_message(self):
        for harness in ("claude", "opencode"):
            for size in SIZES:
                for fill in ("", "dense"):
                    name = "%s-%d-%d-%s" % (harness, size[0], size[1], fill or "empty")
                    with self.subTest(harness=harness, size=size, fill=fill or "empty"), \
                            self.window(harness, size, name, **({"ARI_NATIVE_FILL": fill} if fill else {})) as rig:
                        self.assertLessEqual(self.screen_bytes(rig), SCREEN_BYTES)
                        body = "PEER-WIDE-%d-%d-%s" % (size[0], size[1], fill or "empty")
                        results = self.deliver(harness, self.send(harness, body))
                        self.assertTrue(results[-1].get("ok"), results)
                        # The native itself took the exact body, not only the socket.
                        rig.wait(lambda: any(body in row["prompt"] for row in rig.prompts()), "the native never took it")

    def test_an_empty_screen_carries_the_same_few_bytes_at_every_size(self):
        sizes = []
        for size in SIZES:
            with self.window("claude", size, "bytes-%d-%d" % size) as rig:
                sizes.append(self.screen_bytes(rig))
        # Only the blank rows' `[]` grow with the window (rows, not cells).
        self.assertLess(max(sizes), 2048, sizes)

    def test_a_wide_codex_window_reads_idle_and_empty(self):
        for size in SIZES[2:]:
            with self.subTest(size=size), self.window("codex", size, "codex-%d-%d" % size, ARI_NATIVE_FILL="dense") as rig:
                status = rig.status()
                self.assertEqual((status.get("state"), status.get("empty"), status.get("hold")), ("idle", True, ""), status)
                lines, why = session_shell.native_screen_reason(rig.current_binding())
                self.assertEqual(why, "")
                self.assertIsInstance(lines, list)

    def keeps(self, rig, keys, marker, hold, name):
        rig.type(keys)
        rig.wait_for(marker, "the native never painted " + name)
        # Once the keys are quiet (host.cjs KEY_QUIET_MS) the reading is the screen's own.
        rig.wait(lambda: rig.status().get("hold") == hold, "the hold never read " + hold)
        body = "PEER-NOT-OVER-" + name.upper()
        ref = self.send("claude", body)
        results = self.deliver("claude", ref, passes=3)
        self.assertEqual([result.get("reason") for result in results], ["not-idle-empty"] * 3)
        self.assertEqual({result.get("hold") for result in results}, {hold}, results)
        # Nothing was written: no prompt, no byte of the message on the native's screen, still pending.
        self.assertEqual(rig.prompts(), [])
        self.assertNotIn(body.encode(), bytes(rig.raw))
        self.assertEqual((_peer_module()._read_pending(ref) or {}).get("state"), "pending")

    def test_a_draft_a_form_a_suggestion_and_hangul_read_as_in_a_small_window(self):
        size = SIZES[-1]
        # A dim suggestion in the empty composer is no draft: the message goes in.
        with self.window("claude", size, "suggestion", ARI_NATIVE_FILL="dense", ARI_NATIVE_SUGGEST="try this") as rig:
            rig.wait_for(b"try this", "the suggestion never painted")
            self.assertTrue(self.deliver("claude", self.send("claude", "PEER-AFTER-SUGGESTION"))[-1].get("ok"))
        for name, keys, marker, hold in (("draft", "half typed", b"half typed", "draft"),
                                         ("hangul", "한글 조합", "조합".encode(), "draft"),
                                         ("form", "/model\r", b"Select model", "form")):
            with self.subTest(case=name), self.window("claude", size, "keeps-" + name, ARI_NATIVE_FILL="dense") as rig:
                self.keeps(rig, keys, marker, hold, name)

    def test_a_key_typed_into_the_window_leaves_its_screen_answer_a_screen(self):
        # Review 1127 (fix 2) 🔴3: retire types a dialog's choice into the window and reads the screen
        # again; that key never turns the screen answer into anything but the screen.
        with self.window("claude", SIZES[0], "typed-key") as rig:
            rig.type("1")
            rig.wait_for("\u276f 1".encode(), "the native never painted the key")
            lines, why = session_shell.native_screen_reason(rig.current_binding())
            self.assertEqual(why, "")
            self.assertIsInstance(lines, list)

    def test_a_screen_past_one_request_waits_and_says_so_everywhere(self):
        # Every cell an accented letter: the 121 x 537 screen is past 1 MiB, so it is not carried at all.
        with self.window("claude", SIZES[3], "heavy", idle=False, ARI_NATIVE_FILL="heavy") as rig:
            rig.wait(lambda: rig.status().get("hold") == "screen-large", "the hold never named the screen")
            body = "PEER-NOT-OVER-LARGE"
            ref = self.send("claude", body)
            results = self.deliver("claude", ref, passes=2)
            self.assertEqual({result.get("hold") for result in results}, {"screen-large"}, results)
            self.assertNotIn(body.encode(), bytes(rig.raw))
            self.assertEqual(session_shell.native_screen_reason(rig.current_binding()), (None, session_shell.SCREEN_LARGE))
            # A sent answer to this session says the same, read from what its waiting message was held on.
            self.assertTrue(_peer_module().note_pending_hold(ref, "screen-large"))
            self.assertEqual(session_shell.answer_delivery_wait("claude", SID, "idle"), "screen-large")
            # Retire's look: the reason, not `screen-unavailable`.
            import importlib.util
            spec = importlib.util.spec_from_file_location("ww_steward", ROOT / "utilities/peer-steward.py")
            steward = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(steward)
            with mock.patch.object(steward, "_shell_seat", return_value=rig.current_binding()):
                self.assertEqual(steward._shell_screen_reason("fixture:p1"), session_shell.SCREEN_LARGE)
        import peer_request
        self.assertTrue(peer_request.hold_words({"state": "pending", "held": {"reason": "screen-large"}}))


if __name__ == "__main__":
    unittest.main()
