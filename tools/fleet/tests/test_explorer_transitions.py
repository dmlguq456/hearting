"""Pure r13 reducer contract checks."""
import unittest
import json
from pathlib import Path

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.input import ExplorerInput
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import load_fixture

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "explorer"


def _expected_nav(st):
    scope = st["scope"] or "-"
    if scope != "-" and ":" not in scope:
        scope = "session:" + scope
    ui = dict(st.get("ui") or {})
    folds = tuple(sorted((str(key), "" if value is None else str(value))
                         for key, value in ui.items() if key not in ("focus", "arrow", "rawoff")
                         and not str(key).startswith(("dialogue:", "card:", "log:"))))
    scroll = tuple(sorted((str(key), int(value)) for key, value in ui.items()
                          if key in ("rawoff",) or str(key).startswith(("dialogue:", "card:", "log:"))))
    return NavState(view=st["tab"], scope=scope, opened=str(ui.get("sopen:OVERVIEW") or ""),
                    usage_open=bool(st["uopen"]), scroll=scroll, title=st["tab"], folds=folds)


def _frame_json(frame):
    from tools.fleet.tests.test_explorer_oracle import expand_rows
    cells = [[list(cell) for cell in row] for row in frame.cells]
    hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in frame.hits]
    return cells, hits


class ViewerByteTrace(unittest.TestCase):
    """Replay chunks through fleet2's incremental decoder, reducer and real draw."""

    def test_viewer_main_byte_traces_match_state_cells_and_ordered_hits(self):
        vm = load_fixture(FIXTURE_DIR / "view_model.json")
        fixture = json.loads((FIXTURE_DIR / "transitions.json").read_text())
        frames = fixture["frames"]
        row_pool = fixture["rows"]
        cases = fixture["cases"]
        for case in cases:
            with self.subTest(case=case["name"], cols=case["cols"]):
                reader = ExplorerInput()
                state = NavState()
                expected_by_state = {}
                for step in case["trace"]:
                    projection = _reference_projection(step["st"])
                    expected_by_state[projection] = step
                order = tuple("session:" + str(item.get("tag")) for item in vm.sessions)
                event_step = 0
                for chunk_index, chunk in enumerate(case["chunks"]):
                    events = reader.feed(chunk.encode("latin1"))
                    for event in events:
                        event_step += 1
                        if event.get("type") == "key" and event.get("key") == "q":
                            continue
                        frame = draw(vm, state, case["cols"], 45, beat=1)
                        tabs = tuple(hit.action[1] for hit in frame.hits
                                      if hit.action[:1] == ("tab",))
                        state, effects = reduce(state, {**event, "tabs": tabs, "order": order}, frame)
                        projection = _nav_projection(state)
                        expected = expected_by_state.get(projection)
                        context = f"step={event_step} chunk={chunk_index} event={event!r}"
                        self.assertIsNotNone(expected, f"{context}: unexpected state {projection}")
                        if expected is None:
                            continue
                        expected_frame = frames[expected["frame"]]
                        actual = draw(vm, state, case["cols"], 45, beat=1)
                        actual_cells, actual_hits = _frame_json(actual)
                        expected_cells = _expand([row_pool[index] for index in expected_frame["cells"]])
                        difference = _first_cell_difference(expected_cells, actual_cells)
                        self.assertIsNone(difference, f"{context}: {difference or 'cell grids differ'}")
                        if actual_hits != expected_frame["hits"]:
                            self.fail(f"{context}: {_first_hit_difference(expected_frame['hits'], actual_hits)}")
                        if event.get("type") == "key" and event.get("key") in ("/", "F"):
                            self.assertIn(("command", "filter" if event["key"] == "/" else "feed"), effects)


def _expand(rows):
    return [[cell for glyph, colour, tint, count in row
             for cell in [[glyph, colour, tint]] * count] for row in rows]


def _reference_projection(st):
    return _nav_projection(_expected_nav(st))


def _nav_projection(state):
    folds = {key[3:] if key.startswith("ui:") else key: value
             for key, value in state.fold_map().items()}
    return (state.view, state.scope, state.usage_open, state.opened,
            tuple(sorted(folds.items())), tuple(sorted(state.scroll)))


def _first_cell_difference(expected, actual):
    for y in range(max(len(expected), len(actual))):
        erow = expected[y] if y < len(expected) else []
        arow = actual[y] if y < len(actual) else []
        for x in range(max(len(erow), len(arow))):
            ecell = erow[x] if x < len(erow) else None
            acell = arow[x] if x < len(arow) else None
            if ecell != acell:
                return f"first cell difference row={y} col={x}: expected={ecell!r} actual={acell!r}"
    return None


def _first_hit_difference(expected, actual):
    for index in range(max(len(expected), len(actual))):
        ehit = expected[index] if index < len(expected) else None
        ahit = actual[index] if index < len(actual) else None
        if ehit != ahit:
            return f"first hit difference index={index}: expected={ehit!r} actual={ahit!r}"
    return "ordered hit maps differ"


class CoreTransitions(unittest.TestCase):
    def test_view_cycle_and_usage_toggle(self):
        state, effects = reduce(NavState(), {"type": "key", "key": "tab"})
        self.assertEqual((state.view, effects), ("PEERS", ()))
        state, effects = reduce(state, {"type": "key", "key": "u"})
        self.assertTrue(state.usage_open)
        self.assertEqual(effects, ())

    def test_wheel_sign_clamp_and_drag_lifecycle_on_scroll_hits(self):
        from types import SimpleNamespace
        from tools.fleet.explorer.grid import Hit
        frame = SimpleNamespace(hits=(Hit(7, 10, 30, ("scroll", "rawoff", 20, -1)),))
        state = NavState(scroll=(("rawoff", 8),))
        state, _ = reduce(state, {"type": "wheel", "direction": 1, "x": 12, "y": 7}, frame)
        self.assertEqual(dict(state.scroll)["rawoff"], 5)
        state, _ = reduce(state, {"type": "wheel", "direction": -1, "x": 12, "y": 7}, frame)
        self.assertEqual(dict(state.scroll)["rawoff"], 8)
        state, _ = reduce(state, {"type": "mouse", "phase": "press", "button": 0,
                                  "x": 12, "y": 7}, frame)
        self.assertEqual(state.drag, ("rawoff", 20, 7, 8, -1))
        state, _ = reduce(state, {"type": "mouse", "phase": "press", "button": 0,
                                  "motion": True, "x": 12, "y": 10}, frame)
        self.assertEqual(dict(state.scroll)["rawoff"], 5)
        state, _ = reduce(state, {"type": "mouse", "phase": "release", "button": 0,
                                  "x": 12, "y": 10}, frame)
        self.assertEqual(state.drag, ())

    def test_incremental_mouse_packets_preserve_scroll_sign_and_split(self):
        decoder = ExplorerInput()
        self.assertEqual(decoder.feed(b"\x1b[<64;10"), ())
        first = decoder.feed(b";8M\x1b[<65;10;8M")
        self.assertEqual([(row["type"], row["direction"]) for row in first],
                         [("wheel", 1), ("wheel", -1)])
        drag = decoder.feed(b"\x1b[<0;10;8M\x1b[<32;10;11M\x1b[<0;10;11m")
        self.assertEqual([row["phase"] for row in drag], ["press", "press", "release"])
        self.assertEqual([row["motion"] for row in drag], [False, True, False])

    def test_fold_reopens_on_second_activation(self):
        state, _ = reduce(NavState(), {"type": "click", "action": ("fold", "sopen:OVERVIEW", "s1")})
        self.assertEqual(state.opened, "s1")
        state, _ = reduce(state, {"type": "click", "action": ("fold", "sopen:OVERVIEW", "s1")})
        self.assertEqual(state.opened, "")


class ListTransitions(unittest.TestCase):
    def test_view_rows_stay_in_list_while_overview_route_is_the_exception(self):
        state, _ = reduce(NavState(), {"type": "click", "action": ("view", "HISTORY")})
        self.assertEqual((state.view, state.scope), ("HISTORY", "-"))
        state, _ = reduce(state, {"type": "click", "action": ("tab", "OVERVIEW")})
        state, _ = reduce(state, {"type": "click", "action": ("fold", "sopen:OVERVIEW", "s1")})
        self.assertEqual((state.view, state.scope, state.opened), ("OVERVIEW", "-", "s1"))
        state, _ = reduce(state, {"type": "click", "action":
                                  ("root", "ROUTES", {"ropen:ROUTES": "r1"})})
        self.assertEqual((state.view, state.scope, state.opened_for("ropen:ROUTES")),
                         ("ROUTES", "-", "r1"))

    def test_route_row_opens_routes_view_and_fold_is_view_local(self):
        state, effects = reduce(NavState(), {"type": "click", "action":
                                               ("root", "ROUTES", {"ropen:ROUTES": "f5"})})
        self.assertEqual((state.view, state.scope, state.opened_for("ropen:ROUTES")),
                         ("ROUTES", "-", "f5"))
        state, _ = reduce(state, {"type": "click", "action": ("fold", "ropen:ROUTES", "f5")})
        self.assertEqual(state.opened_for("ropen:ROUTES"), "")
        state, _ = reduce(state, {"type": "click", "action": ("tab", "HISTORY")})
        self.assertEqual(state.opened_for("ropen:ROUTES"), "")
        self.assertEqual(effects, ())


class ModelTransitions(unittest.TestCase):
    def test_profile_model_and_effort_changes_are_viewer_only(self):
        state = NavState(view="MODELS", title="MODELS")
        for action in (("ui", "sel", "light/codex"), ("ui", "model", "gpt-6-luna"),
                       ("ui", "effort", "high"), ("ui", "apply", "apply command")):
            state, effects = reduce(state, {"type": "click", "action": action})
            self.assertEqual(effects, ())
        self.assertEqual(state.fold_map()["ui:sel"], "light/codex")
        self.assertEqual(state.fold_map()["ui:model"], "gpt-6-luna")
        self.assertEqual(state.fold_map()["ui:effort"], "high")

    def test_tab_and_click_select_quiet_views_without_effects(self):
        state = NavState()
        for view in ("COMPUTE", "MEMORY", "MODELS", "SYSTEM"):
            state, effects = reduce(state, {"type": "click", "action": ("tab", view)})
            self.assertEqual(state.view, view)
            self.assertEqual(effects, ())

    def test_question_defaults_open_and_toggles_without_page_navigation(self):
        state = NavState(view="QUESTIONS")
        self.assertEqual(state.opened_for("qshut:QUESTIONS", ""), "")
        state, _ = reduce(state, {"type": "click", "action": ("fold", "qshut:QUESTIONS", "bb")})
        self.assertEqual(state.opened_for("qshut:QUESTIONS"), "bb")
        self.assertEqual(state.scope, "-")
        state, _ = reduce(state, {"type": "click", "action": ("fold", "qshut:QUESTIONS", "bb")})
        self.assertEqual(state.opened_for("qshut:QUESTIONS"), "")

    def test_question_pane_and_answered_text_actions_stay_advisory(self):
        state = NavState(view="QUESTIONS")
        state, effects = reduce(state, {"type": "click", "action": ("pane", "bb", "pane-bb")})
        self.assertEqual(effects, (("focus", "bb", "pane-bb"),))
        self.assertEqual(state.scope, "-")
        state, _ = reduce(state, {"type": "click", "action": ("fold", "aopen:QUESTIONS", "repo:0")})
        self.assertEqual(state.opened_for("aopen:QUESTIONS"), "repo:0")

    def test_history_and_peer_selection_remain_isolated(self):
        state = NavState(view="HISTORY")
        state, _ = reduce(state, {"type": "click", "action": ("fold", "hopen:HISTORY", "0c")})
        state, _ = reduce(state, {"type": "click", "action": ("fold", "pfocus:PEERS", "f5")})
        self.assertEqual(state.opened_for("hopen:HISTORY"), "0c")
        self.assertEqual(state.opened_for("pfocus:PEERS"), "f5")
        self.assertEqual(state.scope, "-")

    def test_generation_selection_is_scoped_to_its_history_seat(self):
        state = NavState(view="HISTORY")
        state, effects = reduce(state, {"type": "click", "action": ("ui", "gen:0c", 4)})
        self.assertEqual(state.opened_for("ui:gen:0c"), "4")
        self.assertEqual(state.opened_for("ui:gen:34"), "")
        self.assertEqual(state.scope, "-")
        self.assertEqual(effects, ())
