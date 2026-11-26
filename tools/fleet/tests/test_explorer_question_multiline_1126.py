"""11-26: Fleet's written answer takes lines and the editing keys people use, as the shell's question popup does.

Enter alone still sends. In the explorer curses' newline mode is off, so Enter arrives as CR and Ctrl+J as LF: in a
written answer LF is a line break; on anything else LF is Enter as it always was. Alt+Enter (`ESC CR`) and a modified
Enter the terminal reports put a line break in too, `\\` then Enter turns the `\\` into one, and a bracketed paste goes in
whole (its line breaks are line breaks, never Enter). Home/End go to the line's ends, the word and line keys edit, ↑↓
go between the answer's rows at the width Fleet drew it, and the answer grows within the page.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import pty
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tools.fleet import render_input
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState, _question_text_draft, question_answer_rows, reduce
from tools.fleet.explorer.view_model import ExplorerViewModel
from tools.fleet.render_input import (KEY_ALT_OTHER, KEY_NEWLINE, KEY_PASTE, KEY_PASTE_PART, KEY_TEXT_END,
                                      KEY_TEXT_START, KEY_WORD_DELETE, KEY_WORD_LEFT, _CursesInput, answer_key)

TOOLS = Path(__file__).resolve().parents[2]
TABS = ["QUESTIONS", "OVERVIEW"]


class FakeCurses:
    ERR = -1
    KEY_MOUSE = 409
    KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 259, 258, 260, 261
    KEY_PPAGE, KEY_NPAGE, KEY_HOME, KEY_END, KEY_DC = 339, 338, 262, 360, 330


class FakeScreen:
    def __init__(self, data=b""):
        self.values = list(data)

    def timeout(self, delay):
        pass

    def getch(self):
        return self.values.pop(0) if self.values else FakeCurses.ERR


def decode(data, *, explorer=True, alt_keys=False, reader=None):
    reader = reader or _CursesInput(FakeCurses, explorer_mode=explorer)
    reader.alt_keys = alt_keys
    screen, events = FakeScreen(data), []
    for _ in range(len(data) + 8):
        event = reader.read(screen)
        if event[0] != FakeCurses.ERR:
            events.append(event)
        elif not screen.values and not reader.has_buffered_input():
            break
    return events


class ReaderTest(unittest.TestCase):
    def test_line_break_keys_decode_to_one_key(self):
        for raw in (b"\x1b\r", b"\x1b\n", b"\x1b[13;2u", b"\x1b[13;3u", b"\x1b[13;5u", b"\x1b[27;2;13~",
                    b"\x1b[27;5;13~", b"\x1b[106;5u"):
            with self.subTest(raw=raw):
                self.assertEqual(decode(raw + b"x"), [(KEY_NEWLINE, None), (ord("x"), None)])
        # An unmodified kitty Enter is no line break (an inert sequence, as before).
        self.assertEqual(decode(b"\x1b[13;1ux"), [(ord("x"), None)])
        # Outside the explorer the bytes are as before: Esc, then Enter.
        self.assertEqual(decode(b"\x1b\r", explorer=False), [(27, None), (13, None)])
        # Enter (CR) and Ctrl+J (LF) stay their own bytes for the explorer to tell apart.
        self.assertEqual(decode(b"\r\n"), [(13, None), (10, None)])

    def test_a_bracketed_paste_is_one_key_with_its_text(self):
        body = "가\r나\r\n다\n라\t마\x07바\x00".encode()
        self.assertEqual(decode(b"\x1b[200~" + body + b"\x1b[201~q"),
                         [(KEY_PASTE, "가\n나\n다\n라\t마바"), (ord("q"), None)])
        # Split across reads (and a CR LF split too): one key at the end marker.
        reader = _CursesInput(FakeCurses, explorer_mode=True)
        self.assertEqual(decode(b"\x1b[200~one\r", reader=reader), [])
        self.assertTrue(reader.has_buffered_input())
        self.assertEqual(decode(b"\ntwo\x1b[20", reader=reader), [])
        self.assertEqual(decode(b"1~", reader=reader), [(KEY_PASTE, "one\ntwo")])
        # A paste whose bytes stop for a second has stalled (review 1861): what came goes in, and the paste goes on.
        reader = _CursesInput(FakeCurses, explorer_mode=True)
        decode(b"\x1b[200~late", reader=reader)
        reader.paste_since -= 2
        self.assertEqual(reader.read(FakeScreen()), (KEY_PASTE_PART, "late"))
        self.assertTrue(reader.has_buffered_input())
        self.assertEqual(decode(b" more\r\x1b[201~", reader=reader), [(KEY_PASTE, " more\n")])
        self.assertFalse(reader.has_buffered_input())
        # Outside the explorer a paste's markers stay inert as before.
        self.assertEqual(decode(b"\x1b[200~ab\x1b[201~", explorer=False), [(ord("a"), None), (ord("b"), None)])
        # 11-26 follow-up: an escape sequence in a paste (a coloured log) is left out whole, not only its Esc.
        self.assertEqual(decode(b"\x1b[200~a\x1b[31mb\x1b[0mc\x1b[201~"), [(KEY_PASTE, "abc")])
        self.assertEqual(decode("\x1b[200~가\x1b[1;32m나\x1b[m\r\n다\x1b[201~".encode()), [(KEY_PASTE, "가나\n다")])

    def test_a_stalled_paste_goes_on_and_only_a_key_pressed_then_ends_it(self):
        """Review 1861: after a stall the rest of a paste is its text; a key pressed then (nothing readable right after
        it: Enter, Esc after its 25 ms, Ctrl+] at any time) ends the paste and is that key; the paste's rest is dropped."""
        def stalled(data):
            reader = _CursesInput(FakeCurses, explorer_mode=True)
            events = decode(data, reader=reader)
            reader.paste_since -= 2
            events.append(reader.read(FakeScreen()))
            return reader, events
        # The rest's CR, tab and escape sequence are text (a CR never Enter).
        reader, events = stalled("\x1b[200~첫 줄\r둘".encode())
        self.assertEqual(events, [(KEY_PASTE_PART, "첫 줄\n둘")])
        self.assertEqual(decode("째 줄\r셋\t째\x1b[31m!\x1b[201~".encode(), reader=reader), [(KEY_PASTE, "째 줄\n셋\t째!")])
        # Enter pressed: the paste ends as what came, then Enter; its rest is dropped up to its end mark.
        reader, _ = stalled("\x1b[200~부분 글".encode())
        self.assertEqual(decode(b"\r", reader=reader), [(KEY_PASTE, ""), (13, None)])
        self.assertEqual(decode("나머지\r끝\x1b[201~q".encode(), reader=reader), [(ord("q"), None)])
        # A byte right after it: the paste's own CR.
        reader, _ = stalled(b"\x1b[200~a")
        self.assertEqual(decode(b"\rb\x1b[201~", reader=reader), [(KEY_PASTE, "\nb")])
        # Esc pressed: after its 25 ms with nothing more; a `[` within them is a sequence of the paste.
        reader, _ = stalled(b"\x1b[200~log")
        self.assertEqual(decode(b"\x1b", reader=reader), [])
        reader.paste_key_since -= 1
        self.assertEqual(reader.read(FakeScreen()), (KEY_PASTE, ""))
        self.assertEqual(reader.read(FakeScreen()), (27, None))
        self.assertEqual(decode(b"[31mred\r\x1b[201~", reader=reader), [])
        reader, _ = stalled(b"\x1b[200~ab")
        self.assertEqual(decode(b"\x1b", reader=reader), [])
        self.assertEqual(decode(b"[201~", reader=reader), [(KEY_PASTE, "")])
        self.assertFalse(reader.has_buffered_input())
        # Ctrl+] at any time, not stalled too; with no end mark the dropping ends a second after its last byte.
        reader = _CursesInput(FakeCurses, explorer_mode=True)
        decode("\x1b[200~앞 글".encode(), reader=reader)
        self.assertEqual(decode(b"\x1d", reader=reader), [(KEY_PASTE, "앞 글"), (29, None)])
        self.assertEqual(decode(b"more", reader=reader), [])
        reader.paste_since -= 2
        reader.read(FakeScreen())
        self.assertFalse(reader.has_buffered_input())
        self.assertEqual(decode(b"z", reader=reader), [(ord("z"), None)])
        # Not stalled, a lone Enter or Esc is the paste's own.
        self.assertEqual(decode(b"\x1b[200~a\rb\x1b[201~"), [(KEY_PASTE, "a\nb")])
        # What a stall lets in stops before what may join the bytes after it.
        for head, tail, whole in ((b"a\r", b"\nb", "a\nb"), ("\uac00".encode()[:2], "\uac00".encode()[2:] + b"x", "\uac00x"),
                                  (b"x\x1b[3", b"1my", "xy"), (b"a\n\n", b"b", "a\n\nb")):
            with self.subTest(head=head):
                reader, events = stalled(b"\x1b[200~" + head)
                parts = [payload for _code, payload in events if payload]
                parts += [payload for _code, payload in decode(tail + b"\x1b[201~", reader=reader)]
                self.assertEqual("".join(parts), whole)

    def test_word_text_and_delete_keys(self):
        self.assertEqual(decode(b"\x1b[1;5D\x1b[1;3D"), [(KEY_WORD_LEFT, None)] * 2)
        self.assertEqual(decode(b"\x1b[3;5~"), [(KEY_WORD_DELETE, None)])
        self.assertEqual(decode(b"\x1b[1;5H\x1b[1;5F"), [(KEY_TEXT_START, None), (KEY_TEXT_END, None)])
        # Delete had no decoding with keypad off: it was inert in Fleet.
        self.assertEqual(decode(b"\x1b[3~"), [(FakeCurses.KEY_DC, None)])
        self.assertEqual(decode(b"\x1b[3~", explorer=False), [])
        # Alt+letter is the answer's only while one is written; otherwise Esc then the letter, as before.
        self.assertEqual(decode(b"\x1bb", alt_keys=True), [(KEY_WORD_LEFT, None)])
        self.assertEqual(decode(b"\x1bb"), [(27, None), (ord("b"), None)])
        self.assertEqual(decode("\x1b한x".encode(), alt_keys=True), [(KEY_ALT_OTHER, None), (ord("x"), None)])

    def test_what_each_input_stands_for(self):
        self.assertEqual(answer_key(10, writable=True, cr_enter=True), "newline")
        self.assertEqual(answer_key(10, writable=False, cr_enter=True), "enter")
        self.assertIsNone(answer_key(10, writable=True, cr_enter=False), "newline mode on: LF is Enter as before")
        self.assertIsNone(answer_key(13, writable=True, cr_enter=True))
        self.assertEqual(answer_key(KEY_NEWLINE, writable=True), "newline")
        self.assertEqual(answer_key(KEY_NEWLINE, writable=False), "")
        self.assertEqual(answer_key(KEY_PASTE, "a\nb", writable=True), "text:a\nb")
        self.assertEqual(answer_key(KEY_PASTE, "a\nb", finding=True), "text:a b")
        self.assertEqual(answer_key(KEY_PASTE, "q", writable=False), "", "a paste is no command")
        for code, name in ((1, "ctrl-a"), (21, "ctrl-u"), (11, "ctrl-k"), (23, "word-back"), (KEY_WORD_LEFT, "word-left"),
                           (KEY_ALT_OTHER, "")):
            self.assertEqual(answer_key(code, editing=True), name)
            self.assertEqual(answer_key(code, editing=False), "")
        self.assertIsNone(answer_key(ord("a"), writable=True))


class AnswerTest(unittest.TestCase):
    def setup_question(self, **extra):
        ref = {"id": "S4:synthetic", "harness": "codex", "session_id": "synthetic-session",
               "request_id": "synthetic-request", "question_id": "q"}
        question = {"id": "q", "question": "합성 질문: 여섯 가지를 적어 주세요", "is_other": True, "choices": [], **extra}
        envelope = {"kind": "question", "state": "open", "harness": "codex", "questions": [question],
                    "support": {"supported": True}, "native_ref": {"transport": "hearting-message-answer"}}
        row = {"id": ref["id"], "tag": ref["id"], "ref": ref, "kind": "question", "h": "codex",
               "session_tag": "a1", "source": "S4", "wait": "1m"}
        vm = ExplorerViewModel(questions=(row,), system={"answer_waits": {"total": 1, "complete": True}})
        state = NavState(view="QUESTIONS", title="QUESTIONS", folds=(("qsel:QUESTIONS", ref["id"]),),
                         question_overlay={"ref": ref, "detail": envelope, "draft": None, "state": "open",
                                           "editing": False, "pending": False})
        return state, vm, ref

    def keys(self, keys, cols=100, rows=40, **extra):
        state, vm, ref = self.setup_question(**extra)
        frame = None
        effects = ()
        for key in keys:
            if key == "click-write":
                event = {"type": "click", "action": ("question.edit", "q")}
            else:
                event = {"type": "key", "key": key, "tabs": TABS, "question_refs": [ref]}
            state, effects = reduce(state, event, frame)
            frame = draw(vm, state, cols, rows, beat=1)
        return state, vm, frame, effects

    @staticmethod
    def text(keys):
        return ["click-write"] + ["text:" + char for char in keys]

    @staticmethod
    def typed(state):
        return _question_text_draft(state.question_overlay)

    def answer_rows(self, frame):
        """The answer's rows as drawn: the rows its press targets stand on, each without its lead (`> ` or blank)."""
        rows, out = frame.text_rows(), []
        hits = {hit.y: hit.x0 for hit in frame.hits if hit.action[:1] == ("question.edit",)}
        for y in sorted(hits):
            part = rows[y][hits[y]:].strip(" ")
            out.append(part[2:] if part.startswith("> ") else part.lstrip(">"))
        return out

    def test_a_line_break_goes_in_at_the_place_and_enter_sends_the_lines(self):
        state, vm, frame, _ = self.keys(self.text("첫째") + ["newline"] + ["text:" + c for c in "둘째"])
        self.assertEqual(self.typed(state), "첫째\n둘째")
        self.assertEqual(self.answer_rows(frame), ["첫째", "둘째▏"])
        state, vm, frame, _ = self.keys(self.text("ab") + ["newline", "text:c", "up", "newline"])
        self.assertEqual(self.typed(state), "a\nb\nc")
        state, effects = reduce(state, {"type": "key", "key": "enter", "question_refs": [self.setup_question()[2]]})
        submit = next(effect for effect in effects if effect[0] == "questions.submit")
        self.assertEqual(submit[2]["answers"]["q"], ["a\nb\nc"])
        # A line break starts the writing too (a question that takes words).
        state, _, _, _ = self.keys(["newline", "text:x"])
        self.assertEqual(self.typed(state), "\nx")

    def test_backslash_then_enter_and_a_paste(self):
        state, _, _, _ = self.keys(self.text("a\\") + ["enter", "text:b"])
        self.assertEqual(self.typed(state), "a\nb")
        _, _, _, effects = self.keys(self.text("pw\\") + ["enter"], is_secret=True)
        submit = next(effect for effect in effects if effect[0] == "questions.submit")
        self.assertEqual(submit[2]["answers"]["q"], ["pw\\"], "a secret sends its `\\`")
        state, _, frame, _ = self.keys(self.text("ac") + ["left", "text:1. 첫째\n2. 둘째\n3. 셋째"])
        self.assertEqual(self.typed(state), "a1. 첫째\n2. 둘째\n3. 셋째c")
        self.assertEqual(self.answer_rows(frame), ["a1. 첫째", "2. 둘째", "3. 셋째▏c"])
        # A paste past the limit: as much as fits goes in, and the limit is said.
        state, _, frame, _ = self.keys(["click-write", "text:" + "a" * 4090, "text:" + "가나다"])
        self.assertEqual(self.typed(state), "a" * 4090 + "가나")
        self.assertTrue(state.question_overlay.get("answer_full"))

    def test_line_word_and_text_keys(self):
        cases = [
            (self.text("one two") + ["newline"] + ["text:" + c for c in "three"] + ["home", "text:X", "end", "text:Y"],
             "one two\nXthreeY"),
            (self.text("ab") + ["newline", "text:c", "ctrl-a", "text:X", "ctrl-e", "text:Y"], "ab\nXcY"),
            (self.text("ab") + ["newline", "text:c", "text-start", "text:X", "text-end", "text:Y"], "Xab\ncY"),
            (self.text("한글 낱말, 끝") + ["word-left", "text:X", "word-left", "word-left", "text:Y"], "한글 Y낱말, X끝"),
            (self.text("ab cd") + ["home", "word-right", "text:X", "word-right", "text:Y"], "abX cdY"),
            (self.text("keep this word") + ["word-back"], "keep this "),
            (self.text("one two three") + ["home", "word-delete"], " two three"),
            (self.text("first") + ["newline"] + ["text:" + c for c in "second half"] + ["left"] * 4 + ["ctrl-u"],
             "first\nhalf"),
            (self.text("first") + ["newline"] + ["text:" + c for c in "second half"] + ["left"] * 4 + ["ctrl-k"],
             "first\nsecond "),
            (self.text("ab") + ["newline", "text:cd", "up", "delete"], "abcd"),
            (self.text("a\U0001f44d\U0001f3fd b") + ["newline", "text:c", "word-left", "word-left", "text:X"],
             "a\U0001f44d\U0001f3fd Xb\nc"),
        ]
        for keys, want in cases:
            with self.subTest(keys=keys[-3:], want=want):
                state, _, _, _ = self.keys(keys)
                self.assertEqual(self.typed(state), want)

    def test_up_and_down_move_between_rows_at_the_drawn_width(self):
        state, vm, frame, _ = self.keys(self.text("abcdef") + ["newline", "text:xy", "newline"]
                                        + ["text:" + c for c in "longer line"] + ["up"])
        self.assertEqual(state.question_overlay["caret"], ("q", 9))
        state, _ = reduce(state, {"type": "key", "key": "up", "tabs": TABS}, frame)
        self.assertEqual(state.question_overlay["caret"], ("q", 6), "the column is held over the short line")
        state, _ = reduce(state, {"type": "key", "key": "down", "tabs": TABS}, frame)
        state, _ = reduce(state, {"type": "key", "key": "down", "tabs": TABS}, frame)
        self.assertEqual(state.question_overlay["caret"], ("q", 21))
        # Any other key lets the column go.
        state, _ = reduce(state, {"type": "key", "key": "home", "tabs": TABS}, frame)
        state, _ = reduce(state, {"type": "key", "key": "right", "tabs": TABS}, frame)
        state, _ = reduce(state, {"type": "key", "key": "up", "tabs": TABS}, frame)
        self.assertEqual(state.question_overlay["caret"], ("q", 8))
        # A wrapped line moves by its rows; from the first row ↑ leaves the typing as before.
        text = "0123456789" * 12
        state, vm, frame, _ = self.keys(["click-write", "text:" + text], cols=60)
        width = dict(frame.answer_layout)["q"]
        self.assertGreater(len(question_answer_rows(text, width)), 1)
        state, _ = reduce(state, {"type": "key", "key": "up", "tabs": TABS}, frame)
        self.assertTrue(state.question_overlay["editing"])
        self.assertLess(state.question_overlay["caret"][1], len(text))
        for _ in range(len(question_answer_rows(text, width))):
            state, _ = reduce(state, {"type": "key", "key": "up", "tabs": TABS}, frame)
        self.assertFalse(state.question_overlay["editing"])
        self.assertEqual(self.typed(state), text)

    def test_up_and_down_stay_on_a_wrapped_row_at_its_end(self):
        # A wrapped row's end is the next row's start: ↓ from a column past it stays on the row, before its last
        # character (the shell's popup does the same), never on the next row's first cell.
        from tools.fleet.explorer.state import _question_vertical
        self.assertEqual(question_answer_rows("abcd\nabcdefgh", 4), ((0, 4), (5, 9), (9, 13)))
        self.assertEqual(_question_vertical("abcd\nabcdefgh", 4, 4, 1, None), (8, 4))
        self.assertEqual(_question_vertical("abcdefgh\nabcd", 8, 4, -1, None), (3, 4))
        # Through the reducer at the width Fleet drew the answer.
        state, _vm, frame, _ = self.keys(["click-write", "text:" + "0123456789" * 30], cols=60)
        width = dict(frame.answer_layout)["q"]
        text = "x" * width + "\n" + "y" * (width * 2)
        state, _vm, frame, _ = self.keys(["click-write", "text:" + text, "text-start", "end"], cols=60)
        self.assertEqual(state.question_overlay["caret"], ("q", width))
        state, _ = reduce(state, {"type": "key", "key": "down", "tabs": TABS}, frame)
        self.assertEqual(state.question_overlay["caret"], ("q", 2 * width), "the place stays on the wrapped row")

    def test_a_secret_takes_a_line_break_key_as_enter_and_a_pasted_key_keeps_its_inner_lines(self):
        # 11-26 follow-up: in a secret every line-break key sends, as Ctrl+J did before (the shell's popup alike).
        _, _, _, effects = self.keys(self.text("pw") + ["newline"], is_secret=True)
        submit = next(effect for effect in effects if effect[0] == "questions.submit")
        self.assertEqual(submit[2]["answers"]["q"], ["pw"])
        # A pasted key of several lines keeps them and one of the line breaks it ends with (a private key needs its
        # last one); a one-line secret keeps none.
        state, _, _, _ = self.keys(["click-write", "text:line1\nline2\n\n"], is_secret=True)
        self.assertEqual(self.typed(state), "line1\nline2\n")
        state, _, _, _ = self.keys(["click-write", "text:token\n"], is_secret=True)
        self.assertEqual(self.typed(state), "token")
        # In parts (a stalled paste): whether a part put a line break in decides at the paste's end.
        state, _, _, _ = self.keys(["click-write", "paste-part:a", "paste-part:\nb", "text:\n"], is_secret=True)
        self.assertEqual(self.typed(state), "a\nb\n")
        state, _, _, _ = self.keys(["click-write", "paste-part:ab", "text:c\n"], is_secret=True)
        self.assertEqual(self.typed(state), "abc")
        state, _, _, _ = self.keys(["click-write", "text:\n"], is_secret=True)
        self.assertEqual(self.typed(state), "")
        # Not a secret: the paste goes in whole, its last line breaks too.
        state, _, _, _ = self.keys(["click-write", "text:line1\nline2\n"])
        self.assertEqual(self.typed(state), "line1\nline2\n")

    def test_the_answer_grows_within_the_page_and_follows_the_place(self):
        lines = "\n".join(f"줄 {n}" for n in range(1, 31))
        for rows in (40, 24, 16):
            with self.subTest(rows=rows):
                state, vm, frame, _ = self.keys(["click-write", "text:" + lines], rows=rows)
                shown = self.answer_rows(frame)
                self.assertGreater(len(shown), 1)
                self.assertEqual(shown[-1], "줄 30▏")
                self.assertTrue(any("합성 질문" in line for line in frame.text_rows()), "the question keeps its words")
                self.assertTrue(any("submit" in line for line in frame.text_rows()), "the action line stays")
                state, _ = reduce(state, {"type": "key", "key": "text-start", "tabs": TABS}, frame)
                frame = draw(vm, state, 100, rows, beat=1)
                self.assertEqual(self.answer_rows(frame)[0], "▏줄 1")


class PrivatePtyTest(unittest.TestCase):
    """The reader under a real curses in a private PTY: the keys as a terminal sends them, then the reducer
    and Fleet's own drawing of the answer (the picture in the report)."""

    CHILD = r"""
import curses, json, sys, time
sys.path.insert(0, sys.argv[2])
from fleet import render_input
def main(screen):
    screen.keypad(False)
    curses.nonl()
    reader = render_input._CursesInput(curses, explorer_mode=True)
    events, quiet = [], time.monotonic() + 3
    open(sys.argv[1] + '.ready', 'w').close()
    while time.monotonic() < quiet:
        code, payload = reader.read(screen, timeout_ms=50)
        if code != curses.ERR:
            events.append([code, payload])
            quiet = time.monotonic() + 1.5
    json.dump(events, open(sys.argv[1], 'w'), ensure_ascii=False)
curses.wrapper(main)
"""

    def test_keys_as_a_terminal_sends_them_reach_the_answer(self):
        with tempfile.TemporaryDirectory(prefix="fm-") as temp:
            out = Path(temp) / "events.json"
            child = Path(temp) / "child.py"
            child.write_text(self.CHILD)
            env = {"PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "HOME": temp, "LANG": "C.UTF-8"}
            pid, fd = pty.fork()
            if pid == 0:
                os.execve(sys.executable, [sys.executable, "-I", str(child), str(out), str(TOOLS)], env)
            try:
                until = time.monotonic() + 15
                while not Path(str(out) + ".ready").exists() and time.monotonic() < until:
                    if select.select([fd], [], [], .05)[0]:
                        os.read(fd, 65536)
                for data in ("첫째 줄".encode(), b"\n", "둘째 줄".encode(), b"\x1b\r", "셋째\\".encode(), b"\r",
                             "넷째".encode(), b"\x1b[200~\xea\xb0\x80\r\xeb\x82\x98\r\n\xeb\x8b\xa4\x1b[201~", b"\x1b[13;2u",
                             "끝".encode()):
                    os.write(fd, data)
                    time.sleep(.12)
                while time.monotonic() < until + 10:
                    ready = select.select([fd], [], [], .1)[0]
                    if ready:
                        try:
                            if not os.read(fd, 65536):
                                break
                        except OSError:
                            break
                    if out.exists():
                        break
                _, status = os.waitpid(pid, 0)
                pid = None
                self.assertEqual(status, 0)
            finally:
                if pid:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                os.close(fd)
            events = json.loads(out.read_text())
        import curses
        keys = []
        for code, payload in events:
            key = answer_key(code, payload, writable=True, editing=True, cr_enter=True)
            if key is None:
                key = ("enter" if code in (13, curses.KEY_ENTER) else
                       "text:" + chr(code) if 32 <= code < 127 else None)
            keys.append(key)
        # The UTF-8 bytes of the typed words come one byte at a time (render._explorer_text_key joins them).
        joined, pending = [], bytearray()
        for (code, _payload), key in zip(events, keys):
            if 128 <= code < 256:
                pending.append(code)
                try:
                    joined.append("text:" + pending.decode())
                    pending = bytearray()
                except UnicodeDecodeError:
                    pass
            elif key:
                joined.append(key)
        self.assertIn("newline", joined, "Ctrl+J and Alt+Enter")
        self.assertEqual(joined.count("newline"), 3, "Ctrl+J, Alt+Enter and kitty Shift+Enter: " + repr(joined))
        self.assertEqual(joined.count("enter"), 1, "the one Enter after `\\`")
        answer = AnswerTest()
        state, vm, _ref = answer.setup_question()
        state, _ = reduce(state, {"type": "click", "action": ("question.edit", "q")})
        frame = None
        for key in joined:
            state, _ = reduce(state, {"type": "key", "key": key, "tabs": TABS}, frame)
            frame = draw(vm, state, 100, 30, beat=1)
        self.assertEqual(_question_text_draft(state.question_overlay), "첫째 줄\n둘째 줄\n셋째\n넷째가\n나\n다\n끝")
        picture = [line.rstrip() for line in draw(vm, state, 100, 30, beat=1).text_rows()]
        print("FLEET_PICTURE=" + json.dumps(picture, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    unittest.main()
