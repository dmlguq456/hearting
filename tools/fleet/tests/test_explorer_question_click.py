"""QUESTIONS answered by pressing alone: each painted line is the place a press lands (E9 C5).

A press is the event ``render._explorer_handle`` hands the reducer after decoding SGR bytes,
aimed at the cell where the words are painted, at 80 and 140 columns, after scrolling, with
Korean and emoji text. ``open pane`` reaches the multiplexer that holds the question's pane.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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

    def test_the_title_and_model_words_are_the_session_rows_own(self):
        # The title follows the screen language through ``answer_wait_word``; the MODEL cell
        # is ``model_cell_parts`` (``medium`` → ``med``), the same words a session row draws.
        from unittest import mock
        for lang, word in (("English", "◑ to answer 2"), ("Korean", "◑답 대기 2")):
            with self.subTest(lang=lang), mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
                rows = draw(_vm(2), NavState(view="QUESTIONS"), 140, 45).text_rows()
                self.assertTrue(any(f"── {word} ─" in row for row in rows), rows)
        vm = ExplorerViewModel(questions=(_row(0, h="claude", m=("Fable 5.2", "fam_fable"),
                                                e="medium"),))
        rows = draw(vm, NavState(view="QUESTIONS"), 140, 45).text_rows()
        self.assertTrue(any("claude Fable 5.2 med" in row for row in rows), rows)

    def test_zero_one_and_five_waits_each_get_one_pressable_line(self):
        for count in (0, 1, 5):
            with self.subTest(count=count):
                frame = draw(_vm(count), NavState(view="QUESTIONS"), 80, 45)
                rows = frame.text_rows()
                self.assertTrue(any(f"── ◑ to answer {count} ─" in row for row in rows))
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
        self.assertIn("answer in its window │ open pane", "\n".join(frame.text_rows()))
        x, y = _locate(frame, "open pane")
        _state, effects = _press(state, frame, x + 2, y)
        self.assertEqual(effects, (("focus", "a0", "w1:p2"),))
        pressable = {h.action[0] for h in frame.hits if h.y > 4 and h.x0 >= 33}
        self.assertNotIn("question.submit", pressable)
        self.assertNotIn("question.choice", pressable)

    def test_a_wait_fleet_cannot_answer_names_where_by_its_reason(self):
        # Spec E9 rule 54 (b)-(e): from Fleet a wait it cannot answer says what to do by the
        # request's reason, in the same words as the shell panel (another session's window).
        for reason, words in (("marker-has-no-bound-native-response-surface", "answer in its window"),
                              ("owner-correction-lineage-unverified", "answer in the parent"),
                              ("owner-correction-already-pending", "sending"),
                              ("codex-native-request-not-current", "closed"),
                              ("some-later-reason", "answer in its window")):
            for envelope in ({"state": "unknown", "support": {"supported": False, "reason": reason}, "detail": None},
                             {**_envelope(), "support": {"supported": False, "reason": reason}, "native_ref": None}):
                state = _selected(envelope)
                text = "\n".join(draw(_vm(), state, 120, 45).text_rows())
                self.assertIn(words + " │ open pane", text, reason)
                self.assertNotIn(reason, text)
                self.assertNotIn("answer in its pane", text)


def _tmux(name, *argv):
    import subprocess
    return subprocess.run(["tmux", "-L", name, "-f", "/dev/null", *argv], capture_output=True,
                          text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()


class OpenPaneReachesTheMultiplexer(unittest.TestCase):
    def test_the_focus_effect_is_executed_with_the_server_its_pane_was_observed_on(self):
        seen = []
        import tools.fleet.explorer.runtime as runtime
        original = runtime.focus_pane
        runtime.focus_pane = lambda pane, server=None, *a, **k: seen.append((pane, server))
        vm = ExplorerViewModel(questions=(_row(0, pane_server="herdr:default"),))
        try:
            ExplorerRuntime().apply_effects((("focus", "a0", "w1:p2"),), vm)
            ExplorerRuntime().apply_effects((("focus", "a0", "w9:p9"),), vm)
        finally:
            runtime.focus_pane = original
        # The row's own pane goes with its server; a pane the row does not hold has none.
        self.assertEqual(seen, [("w1:p2", "herdr:default"), ("w9:p9", None)])

    def test_which_multiplexer_command_a_pane_gets(self):
        self.assertEqual(focus_command("w1:p2", "herdr:default", {}), ("herdr", "default", "w1:p2"))
        self.assertEqual(focus_command("w1:p2", "herdr:work", {}), ("herdr", "work", "w1:p2"))
        tmux = focus_command("%7", "tmux:/tmp/tmux-1/b", {})
        self.assertEqual(tmux[0], "tmux")
        self.assertIn(["tmux", "-S", "/tmp/tmux-1/b", "select-pane", "-t", "%7"], tmux[1])
        self.assertIn(["tmux", "-L", "default", "select-pane", "-t", "%7"],
                      focus_command("%7", "tmux:", {})[1])
        # The pane's server unknown (or of the other multiplexer): nothing is asked anywhere,
        # even though Fleet itself sits in a tmux or Herdr server with the same pane numbers.
        inside = {"TMUX": "/tmp/tmux-1/a,1,0", "HERDR_SOCKET_PATH": "/s"}
        for pane, server, env in (("w1:p2", None, inside), ("%7", None, inside),
                                  ("%7", "herdr:default", inside), ("w1:p2", "tmux:", inside),
                                  ("", "tmux:", {}),
                                  ("w1:p2", "herdr:default", {"FLEET_DEMO": "1"})):
            self.assertIsNone(focus_command(pane, server, env), (pane, server, env))
        calls = []
        self.assertFalse(focus_pane("%7", None, inside, runner=lambda argv, **_k: calls.append(argv)))
        self.assertEqual(calls, [])

    def test_herdr_focus_uses_the_observed_endpoint_without_an_inherited_socket(self):
        asked, sent = [], []
        resolve = lambda endpoint, _env: asked.append(endpoint) or "/run/herdr-default.sock"
        self.assertTrue(focus_pane("w1:p2", "herdr:default", {}, runner=lambda *a: sent.append(a),
                                   resolve=resolve))
        self.assertEqual((asked, sent), (["default"], [("/run/herdr-default.sock", "w1:p2")]))
        # The endpoint's socket cannot be found: no request.
        sent.clear()
        focus_pane("w1:p2", "herdr:default", {}, runner=lambda *a: sent.append(a),
                   resolve=lambda *_a: "")
        self.assertEqual(sent, [])

    def test_the_endpoint_socket_is_the_inherited_one_only_when_it_belongs_to_that_endpoint(self):
        import sys
        from unittest import mock
        from tools.fleet.explorer.runtime import herdr_endpoint_socket
        herdr_endpoint_socket("default", {})          # puts utilities/ on the path
        import session_tidy
        with mock.patch.object(session_tidy, "_herdr_endpoint_socket",
                               side_effect=lambda server: f"/reported/{server or 'default'}.sock"):
            self.assertEqual(herdr_endpoint_socket("default", {"HERDR_SOCKET_PATH": "/inherited.sock"}),
                             "/inherited.sock")
            self.assertEqual(herdr_endpoint_socket("default", {}), "/reported/default.sock")
            self.assertEqual(herdr_endpoint_socket("work", {"HERDR_SOCKET_PATH": "/inherited.sock"}),
                             "/reported/work.sock")
        self.assertIn("session_tidy", sys.modules)

    @unittest.skipUnless(__import__("shutil").which("tmux"), "tmux is not installed")
    def test_two_tmux_servers_with_the_same_pane_numbers_move_only_the_questions_server(self):
        import subprocess
        names = ("fqc-focus-a-%d" % os.getpid(), "fqc-focus-b-%d" % os.getpid())
        try:
            for name in names:
                _tmux(name, "new-session", "-d", "-s", "fixture", "-x", "120", "-y", "40", "sleep 120")
                _tmux(name, "new-window", "-t", "fixture:1", "sleep 120")
            sockets = {name: _tmux(name, "display-message", "-p", "-t", "fixture", "#{socket_path}")
                       for name in names}
            active = lambda: {name: _tmux(name, "display-message", "-p", "-t", "fixture",
                                          "#{pane_id}") for name in names}
            before = active()
            target = _tmux(names[1], "display-message", "-p", "-t", "fixture:0", "#{pane_id}")
            self.assertEqual(target, "%0")
            self.assertEqual(before, {names[0]: "%1", names[1]: "%1"})
            env = {"TMUX": sockets[names[0]] + ",1,0"}           # Fleet itself runs in server A
            row = _row(0, pane=target, pane_server="tmux:" + sockets[names[1]])
            vm = ExplorerViewModel(questions=(row,))
            state = _selected({"state": "unknown", "support": {"supported": False}, "detail": None})
            frame = draw(vm, state, 80, 45)
            x, y = _locate(frame, "open pane")
            _state, effects = _press(state, frame, x + 2, y)
            self.assertEqual(effects, (("focus", "a0", "%0"),))
            from tools.fleet.explorer.runtime import pane_target
            self.assertTrue(focus_pane(*pane_target(vm, *effects[0][1:]), env, runner=subprocess.run))
            self.assertEqual(active(), {names[0]: "%1", names[1]: "%0"})
            # The same press with the pane's server unknown asks neither server.
            _tmux(names[1], "select-window", "-t", "fixture:1")
            unknown = ExplorerViewModel(questions=(_row(0, pane=target),))
            self.assertFalse(focus_pane(*pane_target(unknown, *effects[0][1:]), env,
                                        runner=subprocess.run))
            self.assertEqual(active(), {names[0]: "%1", names[1]: "%1"})
        finally:
            for name in names:
                subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True,
                               stdin=subprocess.DEVNULL)

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


class APaneKeepsTheServerItWasObservedOn(unittest.TestCase):
    def test_each_pane_source_names_its_server_or_none(self):
        from unittest import mock
        from tools.fleet.explorer import live
        with mock.patch.object(live, "herdr_pane_server", return_value="herdr:work"):
            herdr = live._session_pane({"session_identity_evidence": {"pane": "w1:p2"}}, {})
            tmux = live._session_pane({"tmux_pane": "%3", "tmux_server": "/tmp/t/b"}, {})
            default = live._session_pane({"tmux_pane": "%3", "tmux_server": ""}, {})
            delivered = live._session_pane({"session_id": "s1"}, {"s1": "w1:p9"})
        self.assertEqual(herdr, {"pane": "w1:p2", "pane_server": "herdr:work", "pane_from": "census"})
        self.assertEqual(tmux, {"pane": "%3", "pane_server": "tmux:/tmp/t/b", "pane_from": "census"})
        self.assertEqual(default, {"pane": "%3", "pane_server": "tmux:", "pane_from": "census"})
        # Known only from a delivered message: no server, so the press moves nothing.
        self.assertEqual(delivered, {"pane": "w1:p9", "pane_server": None, "pane_from": "peer"})


PATHS = ("build", "pending-merge", "pending-merge-over-older-map")


def _pane_cell(source, current_map, *, merged=False, path=None):
    """One session and its question, published through ``path``, the session's pane learnt
    from ``source`` and, with ``current_map``, also seen as ``w1:p2`` by a current pane read.

    ``build`` composes the detail directly; ``pending-merge`` merges it later into a model
    composed without it; ``pending-merge-over-older-map`` merges it into a model composed
    with an older map that saw ``w1:p7``."""
    path = path or ("pending-merge" if merged else "build")
    import copy
    from unittest import mock
    from tools.fleet.explorer import live
    from tools.fleet.explorer.types import ExplorerDetail, FocusKey, FocusRequest, FocusResult, Health
    from tools.fleet.model import Session
    sid = "fixture-map-sid"
    extra = {"census-herdr": {"session_identity_evidence": {"pane": "w1:p5"}},
             "census-tmux": {"tmux_pane": "%3", "tmux_server": "/tmp/fixture/b"}}.get(source, {})
    session = Session("codex", 424242, session_id=sid, session_tag="aa", proc_start="fixture-birth",
                      cwd="/tmp/fixture", liveness="blocked", **extra)
    snapshot = {"sessions": [session], "jobs": [], "answer_waits": {
        "complete": True, "total": 1, "older": 0,
        "items": [{"id": "S1:fixture-question", "source": "S1-question", "kind": "decision",
                   "harness": "codex", "session_id": sid, "since": 1000, "stale": "current",
                   "attribution": "exact"}]}}
    if source == "peer":
        snapshot["peer_messages"] = {"records": [{"ts": "2026-11-11T00:00:00Z",
                                                  "to": {"session_id": sid, "pane": "w1:p2"}}]}

    def pane_map(pane):
        key = FocusKey("codex", sid)
        request = FocusRequest(key, 1, 1, wants=("panes",),
                               bindings=tuple(live.detail_bindings(copy.deepcopy(snapshot)).items()))
        return ExplorerDetail(panes={sid: pane}, results={"panes": FocusResult(
            key, 1, 1, health=Health("success", 1001), request=request)})
    accepted = pane_map("w1:p2") if current_map else None
    with mock.patch.object(live, "herdr_pane_server", return_value="herdr:work"):
        if path == "build":
            return live.build_view_model(snapshot, accepted_detail=accepted, now=1002)
        older = pane_map("w1:p7") if path == "pending-merge-over-older-map" else None
        # the read arrives after the complete model was published
        return live.selected_detail(live.build_view_model(snapshot, accepted_detail=older, now=1002),
                                    accepted)


def _open_pane_requests(vm, state, cols=140):
    """Press every drawn ``open pane`` and count the focus requests each reaches, by server."""
    from tools.fleet.explorer.runtime import pane_target
    frame = draw(vm, state, cols, 45)
    hits = [h for h in frame.hits if h.action[:1] == ("pane",)]
    asked = []
    for hit in hits:
        _state, effects = _press(state, frame, hit.x0, hit.y)
        for effect in effects:
            pane, server = pane_target(vm, *effect[1:])
            focus_pane(pane, server, {}, runner=lambda *a, **_k: asked.append((pane, server)),
                       resolve=lambda endpoint, _env: f"/run/{endpoint}.sock")
    # A tmux focus is three commands to one server; count it once.
    return len(hits), sorted(set(asked))


class ASessionAndItsQuestionOpenTheSamePane(unittest.TestCase):
    """Every pane source × current pane read: the session row and its question hold one pair."""

    EXPECTED = {
        ("none", False): (None, None),
        ("none", True): ("w1:p2", "herdr:work"),
        ("peer", False): ("w1:p2", None),          # known only from a message: moves nothing
        ("peer", True): ("w1:p2", "herdr:work"),
        ("census-herdr", False): ("w1:p5", "herdr:work"),
        ("census-herdr", True): ("w1:p5", "herdr:work"),
        ("census-tmux", False): ("%3", "tmux:/tmp/fixture/b"),
        ("census-tmux", True): ("%3", "tmux:/tmp/fixture/b"),
    }

    def test_every_publication_path_draw_and_press_agree_for_each_cell(self):
        # A census pair is never replaced by the map (census-herdr's w1:p5 differs from the
        # map's w1:p2); every path settles the same pair, and the question opens it too.
        cells = 0
        for (source, current_map), (pane, server) in self.EXPECTED.items():
          for path in PATHS:
            if path == "pending-merge-over-older-map" and not current_map:
                continue                              # no new detail: nothing is merged
            with self.subTest(source=source, current_map=current_map, path=path):
                cells += 2
                vm = _pane_cell(source, current_map, path=path)
                row, question = vm.sessions[0], vm.questions[0]
                self.assertEqual((row.get("pane"), row.get("pane_server")), (pane, server))
                self.assertEqual((question.get("pane"), question.get("pane_server")), (pane, server))
                for state in (NavState(view="QUESTIONS", folds=(("qopen:QUESTIONS", question["tag"]),)),
                              NavState(scope="session:aa")):
                    hits, asked = _open_pane_requests(vm, state)
                    self.assertEqual(hits, 1 if pane else 0, state)
                    self.assertEqual(asked, [(pane, server)] if server else [], state)
        self.assertEqual(cells, 40)                   # 4 sources x (2 paths + 3 paths) x 2 rows

    def test_a_pane_read_merged_into_the_shown_model_reaches_its_question(self):
        for source in ("none", "peer"):
            with self.subTest(source=source):
                vm = _pane_cell(source, True, merged=True)
                pair = ("w1:p2", "herdr:work")
                self.assertEqual((vm.sessions[0].get("pane"), vm.sessions[0].get("pane_server")), pair)
                question = vm.questions[0]
                self.assertEqual((question.get("pane"), question.get("pane_server")), pair)
                state = NavState(view="QUESTIONS", folds=(("qopen:QUESTIONS", question["tag"]),))
                self.assertEqual(_open_pane_requests(vm, state), (1, [pair]))


class AnOpenPaneWayRemainsWhenNarrow(unittest.TestCase):
    """Every session with a pane keeps a pressable link and an Enter that names its pane."""

    def test_session_page_link_and_enter_at_every_width(self):
        from tools.fleet.explorer.view_model import load_fixture
        from tools.fleet.explorer.runtime import pane_target
        vm = load_fixture(Path(__file__).parent / "fixtures" / "explorer" / "view_model.json")
        sessions = [row for row in vm.sessions if row.get("pane")]
        self.assertGreater(len(sessions), 3)
        for cols in (80, 100, 120, 140):
            for session in sessions:
                tag, pane = str(session["tag"]), str(session["pane"])
                with self.subTest(cols=cols, tag=tag):
                    state = NavState(scope="session:" + tag)
                    frame = draw(vm, state, cols, 45)
                    hits = [h for h in frame.hits if h.action == ("pane", tag, pane)]
                    self.assertEqual(len(hits), 1, "\n".join(frame.text_rows()))
                    _state, effects = _press(state, frame, hits[0].x0, hits[0].y)
                    self.assertEqual(effects, (("focus", tag, pane),))
                    _state, effects = reduce(state, {"type": "key", "key": "enter"}, frame)
                    self.assertEqual(effects, (("focus", tag, pane),))
                    # Even an Enter that found no drawn link names the selected session's pane.
                    self.assertEqual(pane_target(vm, tag, "")[0], pane)

    def test_overview_unfolded_question_keeps_its_link_at_80_columns(self):
        from dataclasses import replace
        session = {"tag": "a0", "harness": "codex", "state": "blocked", "ask": "question",
                   "title": "fixture", "repo": "r", "repo_id": "r", "pane": "w1:p2"}
        vm = ExplorerViewModel(repos=({"id": "r", "name": "r"},), sessions=(session,),
                               questions=(_row(0, ask=LONG),))
        for cols in (80, 140):
            with self.subTest(cols=cols):
                state = NavState(folds=(("sopen:OVERVIEW", "a0"),), opened="a0")
                frame = draw(vm, state, cols, 45)
                hits = [h for h in frame.hits if h.action[:1] == ("pane",)]
                self.assertTrue(hits, "\n".join(frame.text_rows()))
                self.assertEqual(hits[0].action, ("pane", "a0", "w1:p2"))


if __name__ == "__main__":
    unittest.main()
