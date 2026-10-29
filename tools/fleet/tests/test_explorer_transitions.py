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
                        # Current accepted renderer: compare every cell and ordered hit.
                        # Historical input chunks and expected navigation states are unchanged.
                        expected_cells = _expand([row_pool[index] for index in expected_frame["cells"]])
                        expected_hits = expected_frame["hits"]
                        difference = _first_cell_difference(expected_cells, actual_cells)
                        self.assertIsNone(difference, f"{context}: {difference or 'cell grids differ'}")
                        if actual_hits != expected_hits:
                            self.fail(f"{context}: {_first_hit_difference(expected_hits, actual_hits)}")
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
    def test_sessions_tree_uses_thin_branches_and_preserves_scope_hits(self):
        vm = load_fixture(FIXTURE_DIR / "view_model.json")
        frame = draw(vm, NavState(), 140, 45)
        tree = "\n".join(row[:32] for row in frame.text_rows())
        self.assertIn("├─ ", tree)
        self.assertIn("└─ ", tree)
        self.assertIn("│  ", tree)
        self.assertNotIn("┣━", tree)
        self.assertNotIn("┗━", tree)
        self.assertNotIn("┃  ", tree)
        self.assertTrue(any(hit.action[:1] == ("scope",) for hit in frame.hits))

    def test_models_buttons_emit_structured_profile_and_context_actions(self):
        vm = load_fixture(FIXTURE_DIR / "view_model.json")
        state = NavState(view="MODELS", folds=(
            ("ui:sel", "balanced/codex"), ("ui:model", "gpt-6.1-sol"),
            ("ui:effort", "high"), ("ui:ctx:codex", "600k")))
        frame = draw(vm, state, 240, 80)
        actions = [hit.action for hit in frame.hits if hit.action[:1] == ("models.apply",)]
        self.assertIn(("models.apply", "codex", "profile", "balanced",
                       "gpt-6.1-sol", "high", True), actions)
        self.assertIn(("models.apply", "codex", "profile", "balanced",
                       "gpt-6.1-sol", "high", False), actions)
        self.assertIn(("models.apply", "codex", "context-window", 600000, False), actions)

    def test_model_actions_use_isolated_normal_utility_and_refresh_current_values(self):
        import os
        import shutil
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet.explorer.runtime import ExplorerRuntime

        data = {
            "profiles": ["balanced-deep"],
            "profile_map": {"opencode": {"balanced-deep": ("muse-spark-1.3-contributor", "xhigh")},
                            "codex": {"balanced": ("gpt-6-luna", "xhigh")}},
            "model_choices": {"opencode": ["openai/gpt-6.1-sol", "muse-spark-1.3-contributor"],
                              "codex": ["gpt-6.1-sol", "gpt-6-luna"]},
            "efforts": ["low", "medium", "high", "xhigh", "max"],
            "token_units": {"600k": 600000, "800k": 800000},
        }
        vm = SimpleNamespace(models=data, ordering={})
        profile = ("models.apply", "opencode", "profile", "balanced-deep",
                   "openai/gpt-6.1-sol", "xhigh", False)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_home = root / "config"
            opencode_runtime = root / "opencode-runtime"
            codex_runtime = root / "codex-runtime"
            environment = {
                "AGENT_HOME": str(Path(__file__).resolve().parents[3]),
                "HOME": str(root), "CLAUDE_CONFIG_DIR": str(root / "claude"),
                "CODEX_HOME": str(root / "codex"), "XDG_CONFIG_HOME": str(config_home),
                "XDG_DATA_HOME": str(root / "data"), "XDG_STATE_HOME": str(root / "state"),
                "HEARTING_MODEL_CONFIG_HOME": str(opencode_runtime),
                "FLEET_DEMO": "",
            }
            with mock.patch.dict(os.environ, environment):
                runtime = ExplorerRuntime()
                opencode_config = opencode_runtime / "agent-config" / "models.conf"
                codex_config = codex_runtime / "agent-config" / "models.conf"
                opencode_config.parent.mkdir(parents=True)
                codex_config.parent.mkdir(parents=True)
                source_root = Path(environment["AGENT_HOME"])
                shutil.copyfile(source_root / "adapters/opencode/config/models.conf", opencode_config)
                shutil.copyfile(source_root / "adapters/codex/config/models.conf", codex_config)
                opencode_before = opencode_config.read_bytes()
                planned = runtime.apply_effects([profile[:-1] + (True,)], vm)
                self.assertEqual(planned["status"], "planned")
                self.assertEqual(opencode_config.read_bytes(), opencode_before)

                applied = runtime.apply_effects([profile], vm)
                self.assertEqual(applied["status"], "changed")
                self.assertEqual(tuple(applied["models"]["profile_map"]["opencode"]["balanced-deep"]),
                                 ("openai/gpt-6.1-sol", "xhigh"))
                self.assertIn("model set:", applied["message"])

                context = ("models.apply", "codex", "context-window", 600000, False)
                codex_before = codex_config.read_bytes()
                with mock.patch.dict(os.environ, {"HEARTING_MODEL_CONFIG_HOME": str(codex_runtime)}):
                    context_dry = runtime.apply_effects([context[:-1] + (True,)], vm)
                    self.assertEqual(context_dry["status"], "planned")
                    self.assertEqual(codex_config.read_bytes(), codex_before)
                    context_applied = runtime.apply_effects([context], vm)
                    self.assertEqual(context_applied["status"], "changed")
                    self.assertEqual(context_applied["models"]["context"]["codex"], 600000)

                bad_data = dict(data)
                bad_choices = dict(data["model_choices"])
                bad_choices["opencode"] = list(bad_choices["opencode"]) + ["bad model"]
                bad_data["model_choices"] = bad_choices
                before = opencode_config.read_bytes()
                bad = runtime.apply_effects([("models.apply", "opencode", "profile", "balanced-deep",
                                               "bad model", "high", False)],
                                            SimpleNamespace(models=bad_data, ordering={}))
                self.assertEqual(bad["status"], "invalid-arguments")
                self.assertIn("model must use", bad["message"])
                self.assertEqual(opencode_config.read_bytes(), before)

                with mock.patch.dict(os.environ, {"FLEET_DEMO": "1"}), \
                        mock.patch.object(ExplorerRuntime, "_model_settings_module") as setter:
                    demo = runtime.apply_effects([profile], vm)
                    self.assertEqual(demo["status"], "read-only")
                    setter.assert_not_called()

    def test_replay_model_action_is_reported_read_only_without_runtime_call(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet import render

        state = NavState(view="MODELS")
        vm = SimpleNamespace(models={}, ordering={})
        runtime = SimpleNamespace(apply_effects=mock.Mock())
        action = ("models.apply", "codex", "context-window", 600000, False)
        with mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", True), \
                mock.patch.object(render, "_EXPLORER_STATE", state), \
                mock.patch.object(render, "_EXPLORER_RUNTIME", runtime):
            result = render._explorer_apply_effects((action,), vm)
            self.assertEqual(result["status"], "read-only")
            self.assertIn("replay", render._EXPLORER_STATE.status)
            runtime.apply_effects.assert_not_called()

    def test_curses_clicked_event_completes_repo_click_without_a_release_packet(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet import render

        vm = load_fixture(FIXTURE_DIR / "view_model.json")
        state = NavState()
        frame = draw(vm, state, 140, 45)
        repo_hit = next(hit for hit in frame.hits if hit.action[:1] == ("scope",)
                        and str(hit.action[1]).startswith("repo:"))
        fake_curses = SimpleNamespace(KEY_MOUSE=999, BUTTON4_PRESSED=64,
                                      BUTTON5_PRESSED=128, BUTTON1_PRESSED=2,
                                      BUTTON1_CLICKED=4, BUTTON1_RELEASED=8)
        with mock.patch.object(render, "curses", fake_curses), \
                mock.patch.object(render, "_EXPLORER_STATE", state), \
                mock.patch.object(render, "_EXPLORER_FRAME", frame), \
                mock.patch.object(render, "_EXPLORER_MODEL", vm), \
                mock.patch.object(render, "_explorer_click_current", return_value=True), \
                mock.patch.object(render, "_explorer_runtime",
                                  return_value=SimpleNamespace(apply_effects=lambda *_args: None)):
            render._explorer_handle(fake_curses.KEY_MOUSE,
                                    (repo_hit.x0, repo_hit.y, fake_curses.BUTTON1_CLICKED),
                                    (45, 140), object())
            self.assertEqual(render._EXPLORER_STATE.scope, repo_hit.action[1])

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

    def test_page_and_home_end_sequences_decode_as_explorer_scroll_keys(self):
        decoder = ExplorerInput()
        events = decoder.feed(b"\x1b[5~\x1b[6~\x1b[H\x1b[F")
        self.assertEqual([row["key"] for row in events],
                         ["pageup", "pagedown", "home", "end"])

    def test_body_keys_and_tree_wheel_use_separate_viewport_offsets(self):
        from types import SimpleNamespace
        frame = SimpleNamespace(
            hits=(),
            pane_scroll=(34, 4, 100, 36, 0, 28, 28, True),
            tree_scroll=(0, 13, 32, 20, 0, 12, 24, 12, {}),
        )
        state = NavState()
        state, _ = reduce(state, {"type": "key", "key": "pagedown"}, frame)
        self.assertEqual(dict(state.scroll)["pane"], 28)
        state, _ = reduce(state, {"type": "key", "key": "home"}, frame)
        self.assertEqual(dict(state.scroll)["pane"], 0)
        state, _ = reduce(state, {"type": "key", "key": "end", "max_scroll": 46}, frame)
        self.assertEqual(dict(state.scroll)["pane"], 46)
        state, _ = reduce(state, {"type": "wheel", "direction": -1, "x": 4, "y": 14}, frame)
        self.assertEqual(dict(state.scroll)["tree"], 3)
        self.assertEqual(dict(state.scroll)["pane"], 46)

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


E8_SID = "5ed20fe8-499b-4490-a87f-f492612948cf"


def _e8_view_model():
    """The fixture plus an e8-shaped session: a seat whose two earlier generations were cleared away."""
    from tools.fleet.explorer.view_model import ExplorerViewModel
    data = load_fixture(FIXTURE_DIR / "view_model.json").to_mapping()
    base = next(item for item in data["sessions"] if item["tag"] == "09")
    data["sessions"] = [dict(base, tag="e8", steward=False, pane="w18:p7")] + list(data["sessions"])
    data["generations"] = dict(data["generations"], e8=[
        {"harness": "claude", "sid": "7dcee068", "at": "10-27 21:05", "kind": "new",
         "connection": None, "note": None, "card": True, "detail": True, "title": "before the compact"},
        {"harness": "claude", "sid": "bbf14dfc", "at": "10-28 08:40", "kind": "new",
         "connection": "new session · no handover record", "note": None, "card": True, "detail": True,
         "title": "before the clear"},
        {"harness": "claude", "sid": "5ed20fe8", "at": "now", "kind": "new",
         "connection": "clear · new session took over", "note": None, "card": False, "detail": True,
         "title": "current generation"}])
    data["seats"] = list(data["seats"]) + [
        ["e8", base["repo"], 3, "08:41", True, "before the clear", [["claude", "new"]] * 3]]
    return ExplorerViewModel.from_mapping(data), base["repo_id"]


def _ordering(vm):
    return {"repo_by_tag": {item["tag"]: item["repo_id"] for item in vm.sessions},
            "tag_keys": {item["tag"]: "sid:" + item["tag"] for item in vm.sessions}}


def _mouse(phase, x, y, ordering, motion=False):
    event = {"type": "mouse", "phase": phase, "button": 0, "x": x, "y": y, "ordering": ordering}
    if motion:
        event["motion"] = True
    return event


def _hit(frame, action, right_pane=None):
    return next(hit for hit in frame.hits if hit.action == action
                and (right_pane is None or (hit.x0 >= 32) == right_pane))


class SessionOpenPath(unittest.TestCase):
    """e8: a session row reaches its page, and the page reaches the generations before a clear."""

    def setUp(self):
        self.vm, self.repo = _e8_view_model()
        self.ordering = _ordering(self.vm)

    def test_session_page_lists_e8_generations_and_an_earlier_pick_asks_for_its_dialogue(self):
        from unittest import mock
        from tools.fleet import render
        from tools.fleet.explorer import live
        state = NavState(scope="session:e8", title="e8")
        frame = draw(self.vm, state, 140, 60, beat=1)
        generations = {hit.action[2]: hit for hit in frame.hits if hit.action[:2] == ("ui", "gen:e8")}
        self.assertEqual(sorted(generations), [0, 1, 2])
        earlier = generations[1]
        state, effects = reduce(state, _mouse("press", earlier.x0 + 2, earlier.y, self.ordering), frame)
        self.assertEqual((state.scope, state.opened_for("ui:gen:e8"), effects), ("session:e8", "1", ()))
        index = {"e8": {"harness": "claude", "sid": E8_SID, "ids": (E8_SID,), "session": None,
                        "repo": self.repo, "title": ""}}
        with mock.patch.object(live, "session_index", return_value=index), \
                mock.patch.object(render, "_explorer_lite_view", side_effect=lambda snapshot: snapshot):
            key, expanded, wants = render._explorer_requests(object(), state, self.vm)["session"]
        self.assertEqual((key.harness, key.sid, expanded), ("claude", E8_SID, ("claude:" + E8_SID,)))
        self.assertIn("dialogue:1", wants)

    def test_overview_unfold_keeps_the_fold_and_its_open_link_reaches_the_session_page(self):
        state, _ = reduce(NavState(), {"type": "click", "action": ("fold", "sopen:OVERVIEW", "e8")})
        self.assertEqual((state.scope, state.opened), ("-", "e8"))
        frame = draw(self.vm, state, 140, 45, beat=1)
        link = _hit(frame, ("page", "session", "e8"))
        self.assertEqual("".join(cell[0] for cell in frame.cells[link.y][link.x0:link.x1]), "(open ▸)")
        self.assertEqual("".join(cell[0] for cell in frame.cells[link.y][link.x0 - 7:link.x0]).strip(),
                         "PAGE")
        self.assertEqual([hit.action for hit in frame.hits if hit.action[:1] == ("page",)],
                         [("page", "session", "e8")])
        opened, effects = reduce(state, _mouse("press", link.x0, link.y, self.ordering), frame)
        self.assertEqual((opened.scope, opened.drag, effects), ("session:e8", (), ()))
        released, _ = reduce(opened, _mouse("release", link.x0, link.y, self.ordering), frame)
        self.assertEqual(released.scope, "session:e8")
        self.assertEqual(released.opened, "e8")              # the in-place fold stays where it was

    def test_a_cut_link_draws_no_hit(self):
        from tools.fleet.explorer import tables
        from tools.fleet.explorer.grid import CellGrid
        grid = CellGrid(60, 30)
        tables._session_open_link(grid, 40, 47, 52, 5, "e8")
        self.assertEqual(grid.hits, [])
        tables._session_open_link(grid, 40, 47, 60, 26, "e8")      # the overflow probe row stays blank
        self.assertEqual("".join(cell[0] for cell in grid.cells[26]).strip(), "")
        tables._session_open_link(grid, 40, 47, 60, 5, "e8")
        self.assertEqual([(hit.y, hit.x0, hit.x1, hit.action) for hit in grid.hits],
                         [(5, 47, 55, ("page", "session", "e8"))])

    def test_right_pane_scope_row_opens_on_an_unmoved_press_and_release(self):
        state = NavState(scope="repo:" + self.repo, title=self.repo)
        frame = draw(self.vm, state, 140, 45, beat=1)
        row = _hit(frame, ("scope", "e8"), right_pane=True)
        x, y = row.x0 + 4, row.y
        pressed, _ = reduce(state, _mouse("press", x, y, self.ordering), frame)
        self.assertEqual(pressed.drag[:4], ("tree", "session", "e8", "e8"))   # M8: a tree drag starts
        released, effects = reduce(pressed, _mouse("release", x, y, self.ordering), frame)
        self.assertEqual((released.scope, released.drag, effects), ("session:e8", (), ()))

    def test_tree_click_and_reorder_are_unchanged(self):
        frame = draw(self.vm, NavState(), 140, 45, beat=1)
        tree = [hit for hit in frame.hits if hit.action[:1] == ("scope",) and hit.x1 <= 32
                and ":" not in str(hit.action[1])]
        source = next(hit for hit in tree if hit.action[1] == "a3")
        target = next(hit for hit in tree if hit.action[1] == "33")
        pressed, _ = reduce(NavState(), _mouse("press", source.x0 + 2, source.y, self.ordering), frame)
        clicked, _ = reduce(pressed, _mouse("release", source.x0 + 2, source.y, self.ordering), frame)
        self.assertEqual((clicked.scope, clicked.drag), ("session:a3", ()))
        moved, _ = reduce(pressed, _mouse("press", target.x0 + 2, target.y, self.ordering, True), frame)
        self.assertTrue(moved.drag[7] and moved.drag[8], moved.drag)
        dropped, effects = reduce(moved, _mouse("release", target.x0 + 2, target.y, self.ordering), frame)
        self.assertEqual((dropped.scope, dropped.drag), ("-", ()))
        self.assertEqual(effects, (("reorder", "session", self.ordering["repo_by_tag"]["a3"],
                                    "sid:a3", "sid:33"),))

    def test_an_unmoved_press_released_elsewhere_is_a_click_only_on_the_same_identity(self):
        frame = draw(self.vm, NavState(), 140, 45, beat=1)
        tree = [hit for hit in frame.hits if hit.action[:1] == ("scope",) and hit.x1 <= 32
                and ":" not in str(hit.action[1])]
        source, other = tree[0], tree[1]
        pressed, _ = reduce(NavState(), _mouse("press", source.x0 + 2, source.y, self.ordering), frame)
        self.assertFalse(pressed.drag[8])
        # No motion packet arrived, so drag[8] still says "unmoved": the release position decides.
        elsewhere, effects = reduce(pressed, _mouse("release", other.x0 + 2, other.y, self.ordering), frame)
        self.assertEqual((elsewhere.scope, elsewhere.drag, effects), ("-", (), ()))
        right, _ = reduce(pressed, _mouse("release", 60, source.y + 1, self.ordering), frame)
        self.assertEqual((right.scope, right.drag), ("-", ()))
        along, _ = reduce(pressed, _mouse("release", source.x0 + 9, source.y, self.ordering), frame)
        self.assertEqual(along.scope, "session:" + source.action[1])     # the same hit, further along
        page = draw(self.vm, NavState(scope="repo:" + self.repo, title=self.repo), 140, 45, beat=1)
        same = _hit(page, ("scope", "e8"), right_pane=True)
        tree_e8 = _hit(page, ("scope", "e8"), right_pane=False)
        pressed, _ = reduce(NavState(scope="repo:" + self.repo),
                            _mouse("press", tree_e8.x0 + 2, tree_e8.y, self.ordering), page)
        across, _ = reduce(pressed, _mouse("release", same.x0 + 4, same.y, self.ordering), page)
        self.assertEqual(across.scope, "session:e8")                    # another hit of that identity

    def test_an_aged_hit_map_still_takes_the_release_of_an_unmoved_tree_press(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet import render
        frame = draw(self.vm, NavState(scope="repo:" + self.repo, title=self.repo), 140, 45, beat=1)
        row = _hit(frame, ("scope", "e8"), right_pane=True)
        fake_curses = SimpleNamespace(KEY_MOUSE=999, BUTTON4_PRESSED=64, BUTTON5_PRESSED=128,
                                      BUTTON1_PRESSED=2, BUTTON1_CLICKED=4, BUTTON1_RELEASED=8)
        model = SimpleNamespace(ordering={"repo_by_tag": self.ordering["repo_by_tag"]})
        current = {"value": True}
        with mock.patch.object(render, "curses", fake_curses), \
                mock.patch.object(render, "_EXPLORER_STATE", NavState(scope="repo:" + self.repo)), \
                mock.patch.object(render, "_EXPLORER_FRAME", frame), \
                mock.patch.object(render, "_EXPLORER_MODEL", model), \
                mock.patch.object(render, "_explorer_click_current",
                                  side_effect=lambda *_args: current["value"]), \
                mock.patch.object(render, "_explorer_runtime",
                                  return_value=SimpleNamespace(apply_effects=lambda *_args: None)):
            render._explorer_handle(999, (row.x0 + 4, row.y, 2), (45, 140), object())
            self.assertEqual(render._EXPLORER_STATE.drag[:3], ("tree", "session", "e8"))
            current["value"] = False                     # a refresh tick between press and release
            render._explorer_handle(999, (row.x0 + 4, row.y, 8), (45, 140), object())
            self.assertEqual((render._EXPLORER_STATE.scope, render._EXPLORER_STATE.drag),
                             ("session:e8", ()))
            render._explorer_handle(999, (row.x0 + 4, row.y, 2), (45, 140), object())
            self.assertEqual(render._EXPLORER_STATE.drag, ())   # an aged press is still not adopted

    def test_a_refresh_after_the_paint_does_not_age_a_click_aimed_at_it(self):
        from unittest import mock
        from tools.fleet import render
        frame = draw(self.vm, NavState(), 140, 45, beat=1)
        with mock.patch.object(render, "_EXPLORER_FRAME", frame), \
                mock.patch.object(render, "_HIT_SIZE", (45, 140)), \
                mock.patch.object(render, "_HIT_VALID", False), \
                mock.patch.object(render, "_HIT_SNAPSHOT_ID", None):
            # A snapshot landed after the paint: the frame on screen is still the one clicked.
            self.assertTrue(render._explorer_click_current((45, 140), object()))
            # A resize means the click was aimed at a paint that is no longer there.
            self.assertFalse(render._explorer_click_current((40, 140), object()))
        with mock.patch.object(render, "_EXPLORER_FRAME", None), \
                mock.patch.object(render, "_HIT_SIZE", (45, 140)):
            self.assertFalse(render._explorer_click_current((45, 140), None))


if __name__ == "__main__":
    unittest.main()
