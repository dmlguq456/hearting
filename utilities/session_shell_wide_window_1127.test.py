"""An idle shell window takes a message whatever its size, and its form/draft judgment reads as before.

11-27: every readiness request carried the whole native viewport, each row padded to the window's
width, and a request past 64 KiB was refused before the backend saw it; the refusal read as the
hold ``unobserved`` ("its rest is not confirmed"). A 121 x 537 window's empty screen alone was
65 KB, so its idle session took no message (nor moved to a new release) for hours. The host now
sends each row up to its last non-blank cell and, for a screen still too large, its top rows
through the first with text and the bottom rows that fit; the reader judges that only when the
judgment reads the same on it as on the whole screen, and otherwise says ``screen-large``.

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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from fixtures.session_shell_rig import (  # noqa: E402
    ROOT, SID, AnswerReachesIdleShellTest, ShellRig, _peer_module)

import peer_steward_duties as duties  # noqa: E402
import session_shell  # noqa: E402

ENTRY = ROOT / "utilities/session-shell/entry.py"
# The native sizes of the windows seen held (rows, columns); the side panel takes PANEL more columns.
SIZES = ((60, 240), (121, 267), (121, 507), (121, 537), (200, 600))
PANEL = 42
# host.cjs SCREEN_BYTES: the most a carried screen takes.
SCREEN_BYTES = 48 * 1024


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


def cut(lines, bottom, keep_head=True):
    """Rows through the first with text (unless dropped), nulls, then rows from ``bottom`` on."""
    head = next((i for i, cells in enumerate(lines) if cells), -1) + 1 if keep_head else 0
    if bottom <= head:
        return wire(lines)
    return [runs if (y < head or y >= bottom) else None for y, runs in enumerate(wire(lines))]


class CutScreensReadAsWholeScreensTest(unittest.TestCase):
    """The judgment over every screen a host can carry reads as over the whole screen, or says it cannot."""

    def test_row_ends_carry_nothing_the_judgment_reads(self):
        for harness in ("claude", "codex", "opencode"):
            for rows, cols in SIZES:
                for name, lines in screens(harness, rows, cols):
                    with self.subTest(harness=harness, size=(rows, cols), screen=name):
                        whole = duties._screen_ready(harness, lines)
                        got, why = session_shell.screen_lines(harness, wire(trimmed(lines)))
                        self.assertEqual(why, "")
                        self.assertEqual(duties._screen_ready(harness, got), whole)

    def test_every_cut_reads_as_the_whole_screen_or_is_screen_large(self):
        judged = refused = 0
        for harness in ("claude", "codex", "opencode"):
            for (rows, cols), step in (((30, 100), 1), ((121, 537), 9)):
                for name, lines in screens(harness, rows, cols):
                    whole = duties._screen_ready(harness, lines)
                    short = trimmed(lines)
                    for keep_head in (True, False):
                        for bottom in range(1, len(short), step):
                            got, why = session_shell.screen_lines(harness, cut(short, bottom, keep_head))
                            if got is None:
                                self.assertEqual(why, "screen-large", (harness, name, bottom))
                                refused += 1
                                continue
                            judged += 1
                            self.assertEqual(duties._screen_ready(harness, got), whole,
                                             (harness, (rows, cols), name, bottom, keep_head))
        # Both answers happen: a cut is judged when it can be, not refused wholesale.
        self.assertGreater(judged, 1000)
        self.assertGreater(refused, 1000)

    def test_a_cut_that_keeps_the_box_and_fifteen_rows_is_judged_in_each_harness(self):
        for harness in ("claude", "codex", "opencode"):
            for state in ("empty", "draft", "draft-hangul"):
                with self.subTest(harness=harness, state=state):
                    box = bottoms(harness, 537)[state]
                    lines = trimmed(transcript(121 - len(box), 537, "dense") + box)
                    got, why = session_shell.screen_lines(harness, cut(lines, 121 - len(box) - 20))
                    self.assertEqual(why, "")
                    self.assertEqual(duties._screen_ready(harness, got), duties._screen_ready(harness, lines))
        self.assertIsNone(duties._screen_ready("claude", trimmed(transcript(100, 537, "dense") + bottoms("claude", 537)["suggestion"])))

    def test_a_form_in_the_last_rows_of_a_cut_reads_as_a_form_in_each_harness(self):
        for harness, state in (("claude", "menu"), ("codex", "location"), ("opencode", "form"), ("opencode", "dialog")):
            with self.subTest(harness=harness, state=state):
                box = bottoms(harness, 537)[state]
                lines = trimmed(transcript(121 - len(box), 537, "dense") + box)
                got, why = session_shell.screen_lines(harness, cut(lines, 121 - len(box) - 20))
                self.assertEqual((why, duties._screen_ready(harness, got)), ("", "form-open"))
                self.assertEqual(duties._screen_ready(harness, lines), "form-open")

    def test_what_a_cut_cannot_show_is_screen_large_never_ready(self):
        cases = []
        # OpenCode's box goes on above the cut: its only text is in a row left off.
        tall = trimmed(transcript(40, 300, "dense") + bottoms("opencode", 300)["tall-draft"])
        cases.append(("opencode", tall, len(tall) - 30))
        # Claude's marker row is above the cut (a second draft line under it is all that came).
        lines = trimmed(transcript(80, 300, "dense") + [row("❯ draft", 300)] + [row("  more of it", 300)] * 30
                        + [row("─" * 80, 300)])
        cases.append(("claude", lines, len(lines) - 20))
        # Codex's hook review heads the screen; its confirm line is in a row left off.
        review = [row("Hooks need review", 300), row("2 hooks are new or changed.", 300), row("\u203a 1. Review hooks", 300),
                  row("  2. Trust all and continue", 300), row("  3. Continue without trusting", 300)]
        lines = trimmed(review + transcript(20, 300, "dense") + [row("Press enter to confirm", 300)]
                        + transcript(90, 300, "dense") + bottoms("codex", 300)["empty"])
        cases.append(("codex", lines, len(lines) - 40))
        for harness, lines, bottom in cases:
            with self.subTest(harness=harness):
                self.assertNotEqual(duties._screen_ready(harness, lines), None)
                self.assertEqual(session_shell.screen_lines(harness, cut(lines, bottom)), (None, "screen-large"))

    def test_a_malformed_carry_is_unavailable(self):
        lines = wire(trimmed(transcript(30, 80, "dense") + bottoms("claude", 80)["empty"]))
        for screen in ("x", [None, [["a", False]], None, [["b", False]], None], [["a"]], [[["a", 1]]], {"rows": []}):
            with self.subTest(screen=screen):
                self.assertEqual(session_shell.screen_lines("claude", screen), (None, "screen-unavailable"))
        broken = [runs if y not in (3, 9) else None for y, runs in enumerate(lines)]
        self.assertEqual(session_shell.screen_lines("claude", broken), (None, "screen-unavailable"))


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
                        if harness == "claude":
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

    def test_a_draft_a_form_a_suggestion_and_hangul_read_as_in_a_small_window(self):
        size = SIZES[-1]
        # A dim suggestion in the empty composer is no draft: the message goes in.
        with self.window("claude", size, "suggestion", ARI_NATIVE_FILL="dense", ARI_NATIVE_SUGGEST="try this") as rig:
            rig.wait_for(b"try this", "the suggestion never painted")
            self.assertTrue(self.deliver("claude", self.send("claude", "PEER-AFTER-SUGGESTION"))[-1].get("ok"))
        for name, keys, marker, hold in (("draft", "half typed", b"half typed", "draft"),
                                         ("hangul", "\ud55c\uae00 \uc870\ud569", "\uc870\ud569".encode(), "draft"),
                                         ("form", "/model\r", b"Select model", "form")):
            with self.subTest(case=name), self.window("claude", size, "keeps-" + name, ARI_NATIVE_FILL="dense") as rig:
                rig.type(keys)
                rig.wait_for(marker, "the native never painted " + name)
                # Once the keys are quiet (host.cjs KEY_QUIET_MS) the reading is the screen's own.
                rig.wait(lambda: rig.status().get("hold") == hold, "the hold never read " + hold)
                results = self.deliver("claude", self.send("claude", "PEER-NOT-OVER-" + name.upper()), passes=3)
                self.assertEqual([result.get("reason") for result in results], ["not-idle-empty"] * 3)
                self.assertEqual({result.get("hold") for result in results}, {hold}, results)
                self.assertEqual(rig.prompts(), [])

    def test_a_screen_whose_box_cannot_be_carried_says_so(self):
        # The composer at the top and the window filled to the width under it: what fits from the
        # bottom holds no composer row, so its emptiness is not read and the hold names why.
        with self.window("claude", SIZES[-1], "below", idle=False, ARI_NATIVE_FILL="below") as rig:
            rig.wait(lambda: rig.status().get("hold") == "screen-large", "the hold never named the screen")
            results = self.deliver("claude", self.send("claude", "PEER-NOT-OVER-LARGE"), passes=2)
            self.assertEqual({result.get("hold") for result in results}, {"screen-large"}, results)
            self.assertEqual(session_shell.native_screen_reason(rig.current_binding()), (None, session_shell.SCREEN_LARGE))
        import peer_request
        self.assertTrue(peer_request.hold_words({"state": "pending", "held": {"reason": "screen-large"}}))


if __name__ == "__main__":
    unittest.main()
