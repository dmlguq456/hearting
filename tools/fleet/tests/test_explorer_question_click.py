"""QUESTIONS answered by pressing alone: each painted line is the place a press lands (E9 C5).

A press is the event ``render._explorer_handle`` hands the reducer after decoding SGR bytes,
aimed at the cell where the words are painted, at 80 and 140 columns, after scrolling, with
Korean and emoji text. ``open pane`` reaches the multiplexer that holds the question's pane.
"""
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.grid import cell_width
from tools.fleet.explorer.runtime import ExplorerRuntime, focus_command, focus_pane, _herdr_focus
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import ExplorerViewModel

LABELS = ("진행 (그대로)", "수정 🙂", "stop", "넷째 선택지", "five", "여섯 — 마지막")
LONG = ("패널·상태줄·Fleet 세 화면을 5차 시안대로 고칩니다. " * 6).strip()


def _ref(index=0):
    return {"id": f"S1:q{index}", "harness": "codex", "session_id": f"sid-{index}",
            "request_id": f"req-{index}", "turn_id": f"turn-{index}", "question_id": f"qq-{index}"}


def _row(index=0, **extra):
    ref = _ref(index)
    return {"id": ref["id"], "tag": ref["id"], "ref": ref, "kind": "question",
            "source": "S1-question", "wait": f"{index + 1}m", "h": "codex",
            "m": ("gpt-6.1-sol", "fam_gpt"), "e": "xhigh", "session_tag": f"a{index}",
            "open_tag": f"a{index}", "pane": f"w1:p{index + 2}", **extra}


def _envelope(index=0, *, kind="question", prompt=LONG, choices=LABELS, state="open"):
    ref = _ref(index)
    return {"v": 1, "harness": "codex", "session_id": ref["session_id"],
            "request_id": ref["request_id"], "turn_id": ref["turn_id"],
            "question_id": ref["question_id"], "kind": kind, "state": state,
            "detail": ({"questions": [{"id": ref["question_id"], "question": prompt,
                                       "choices": list(choices)}]} if kind == "question"
                       else {"operation": "git push origin HEAD", "target": "repo"}),
            "support": {"supported": True}, "native_ref": {"transport": "fixture", "epoch": 1}}


def _vm(count=1):
    return ExplorerViewModel(questions=tuple(_row(i) for i in range(count)), system={
        "answer_waits": {"state": "complete", "total": count, "older": 0}})


def _selected(envelope, index=0, draft=None, scroll=()):
    return NavState(view="QUESTIONS", title="QUESTIONS", scroll=scroll,
                    folds=(("qsel:QUESTIONS", _ref(index)["id"]),),
                    question_overlay={"ref": _ref(index), "detail": envelope, "draft": draft,
                                      "state": envelope["state"], "editing": False,
                                      "pending": False})


def _press(state, frame, x, y):
    return reduce(state, {"type": "mouse", "phase": "press", "button": 0, "x": x, "y": y}, frame)


def _locate(frame, needle):
    """The painted cell where ``needle`` starts, by screen cells (wide glyphs count 2)."""
    for y, row in enumerate(frame.text_rows()):
        at = row.find(needle)
        if at >= 0:
            return sum(cell_width(ch) for ch in row[:at]), y
    return None


class PressLandsOnThePaintedLine(unittest.TestCase):
    def test_every_choice_line_answers_wherever_it_is_pressed_at_80_and_140_columns(self):
        for cols in (80, 140):
            for index, label in enumerate(LABELS):
                with self.subTest(cols=cols, choice=label):
                    state = _selected(_envelope())
                    frame = draw(_vm(), state, cols, 45)
                    spot = _locate(frame, f"{index + 1}  {label[:4]}")
                    self.assertIsNotNone(spot, "\n".join(frame.text_rows()))
                    _x, y = spot
                    for x in (34, spot[0] + 1, cols - 4):        # left edge, label, far right
                        chosen, effects = _press(state, frame, x, y)
                        self.assertEqual(effects[0][0], "questions.draft", (cols, label, x))
                        self.assertEqual(chosen.question_overlay["draft"]["answers"]["qq-0"],
                                         [label])

    def test_the_long_question_wraps_inside_the_pane_and_choices_stay_on_their_own_lines(self):
        for cols in (80, 140):
            with self.subTest(cols=cols):
                frame = draw(_vm(), _selected(_envelope()), cols, 60)
                rows = frame.text_rows()
                body = [row for row in rows if "패널·상태줄" in row]
                self.assertGreater(len(body), 2 if cols == 80 else 1)
                self.assertTrue(all(sum(cell_width(c) for c in row) <= cols for row in rows))
                for index, label in enumerate(LABELS):
                    x, y = _locate(frame, f"{index + 1}  {label[:4]}")
                    hit = next(h.action for h in frame.hits if h.y == y and h.x0 <= x < h.x1)
                    self.assertEqual(hit, ("question.choice", "qq-0", label))

    def test_row_press_unfolds_in_place_and_reads_then_the_same_press_folds_it(self):
        state = NavState(view="QUESTIONS", title="QUESTIONS")
        vm = _vm(5)
        frame = draw(vm, state, 140, 45)
        x, y = _locate(frame, "[a3]")
        state, effects = _press(state, frame, x + 40, y)
        self.assertEqual(effects, (("questions.read", _ref(3)),))
        self.assertEqual(state.opened_for("qopen:QUESTIONS"), "S1:q3")
        frame = draw(vm, state, 140, 45)
        self.assertIn("open pane", frame.text_rows()[y + 1])
        state, effects = _press(state, frame, x + 40, y)
        self.assertEqual(effects, ())
        self.assertNotIn("open pane", "\n".join(draw(vm, state, 140, 45).text_rows()))

    def test_zero_one_and_five_waits_each_get_one_pressable_line(self):
        for count in (0, 1, 5):
            with self.subTest(count=count):
                frame = draw(_vm(count), NavState(view="QUESTIONS"), 80, 45)
                rows = frame.text_rows()
                self.assertTrue(any(f"◑답 대기 {count}" in row for row in rows))
                opens = [h for h in frame.hits if h.action[:1] == ("question.open",)]
                self.assertEqual(len(opens), count)
                for index in range(count):
                    _x, y = _locate(frame, f"[a{index}]")
                    self.assertEqual(next(h.action[1] for h in opens if h.y == y), f"S1:q{index}")

    def test_scrolled_rows_keep_their_own_press(self):
        vm = _vm(40)
        for offset in (0, 7, 23):
            with self.subTest(offset=offset):
                frame = draw(vm, NavState(view="QUESTIONS", scroll=(("pane", offset),)), 80, 24)
                for y, row in enumerate(frame.text_rows()):
                    if "◑ [a" not in row:
                        continue
                    tag = row.split("◑ [", 1)[1].split("]", 1)[0]
                    hit = next(h.action for h in frame.hits
                               if h.y == y and h.x0 <= 50 < h.x1)
                    self.assertEqual(hit[:2], ("question.open", "S1:q" + tag[1:]))

    def test_submit_is_pressable_only_once_the_answer_is_complete(self):
        state = _selected(_envelope())
        frame = draw(_vm(), state, 140, 45)
        self.assertFalse(any(h.action == ("question.submit",) for h in frame.hits))
        state = _selected(_envelope(), draft={"answers": {"qq-0": [LABELS[1]]}})
        frame = draw(_vm(), state, 140, 45)
        x, y = _locate(frame, "submit")
        self.assertIn("● 2  수정 🙂", "\n".join(frame.text_rows()))
        _state, effects = _press(state, frame, x + 2, y)
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][2], {"answers": {"qq-0": [LABELS[1]]}})

    def test_a_number_key_picks_that_choice_like_a_press(self):
        state = _selected(_envelope())
        frame = draw(_vm(), state, 140, 45)
        chosen, effects = reduce(state, {"type": "key", "key": "4"}, frame)
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(chosen.question_overlay["draft"]["answers"]["qq-0"], [LABELS[3]])
        _state, effects = reduce(state, {"type": "key", "key": "9"}, frame)
        self.assertEqual(effects, ())
        from tools.fleet.render_explorer import _explorer_key_name
        self.assertEqual(_explorer_key_name(ord("3")), "3")

    def test_an_approval_line_offers_each_decision_and_open_pane(self):
        state = _selected(_envelope(kind="approval"))
        frame = draw(_vm(), state, 140, 45)
        text = "\n".join(frame.text_rows())
        self.assertIn("approve │ reject │ defer │ open pane", text)
        for decision in ("approve", "reject"):
            x, y = _locate(frame, decision)
            _state, effects = _press(state, frame, x + 1, y)
            self.assertEqual(effects[0][0], "questions.submit")
            self.assertEqual(effects[0][2], {"decision": decision})

    def test_a_wait_fleet_cannot_answer_still_opens_its_pane(self):
        unknown = {"state": "unknown", "support": {"supported": False}, "detail": None}
        state = _selected(unknown)
        frame = draw(_vm(), state, 80, 45)
        self.assertIn("answer in its pane │ open pane", "\n".join(frame.text_rows()))
        x, y = _locate(frame, "open pane")
        _state, effects = _press(state, frame, x + 2, y)
        self.assertEqual(effects, (("focus", "a0", "w1:p2"),))
        pressable = {h.action[0] for h in frame.hits if h.y > 4 and h.x0 >= 33}
        self.assertNotIn("question.submit", pressable)
        self.assertNotIn("question.choice", pressable)


class OpenPaneReachesTheMultiplexer(unittest.TestCase):
    def test_the_focus_effect_is_executed_not_dropped(self):
        seen = []
        import tools.fleet.explorer.runtime as runtime
        original = runtime.focus_pane
        runtime.focus_pane = lambda pane, *a, **k: seen.append(pane)
        try:
            ExplorerRuntime().apply_effects((("focus", "a0", "w1:p2"),), _vm())
        finally:
            runtime.focus_pane = original
        self.assertEqual(seen, ["w1:p2"])

    def test_which_multiplexer_command_a_pane_gets(self):
        self.assertEqual(focus_command("w1:p2", {"HERDR_SOCKET_PATH": "/x/herdr.sock"}),
                         ("herdr", "/x/herdr.sock", "w1:p2"))
        tmux = focus_command("%7", {"TMUX": "/tmp/tmux-1/default,1,0"})
        self.assertEqual(tmux[0], "tmux")
        self.assertIn(["tmux", "select-pane", "-t", "%7"], tmux[1])
        for pane, env in (("w1:p2", {}), ("%7", {}), ("", {"TMUX": "x"}),
                          ("w1:p2", {"HERDR_SOCKET_PATH": "/s", "FLEET_DEMO": "1"})):
            self.assertIsNone(focus_command(pane, env), (pane, env))
        calls = []
        self.assertTrue(focus_pane("%7", {"TMUX": "x"}, runner=lambda argv, **_k: calls.append(argv)))
        self.assertEqual(calls[0], ["tmux", "select-window", "-t", "%7"])

    def test_herdr_receives_one_pane_focus_request_for_that_pane(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = str(Path(directory) / "herdr.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            received = []

            def serve():
                connection, _ = server.accept()
                with connection:
                    data = b""
                    while not data.endswith(b"\n"):
                        data += connection.recv(4096)
                    received.append(json.loads(data))
                    connection.sendall(b'{"id":"x","result":{"type":"pane_info"}}\n')
            thread = threading.Thread(target=serve)
            thread.start()
            try:
                self.assertTrue(_herdr_focus(path, "w1:p2"))
            finally:
                thread.join(5)
                server.close()
        self.assertEqual(received[0]["method"], "pane.focus")
        self.assertEqual(received[0]["params"], {"pane_id": "w1:p2"})
        self.assertFalse(_herdr_focus(os.path.join(directory, "gone.sock"), "w1:p2"))


if __name__ == "__main__":
    unittest.main()
