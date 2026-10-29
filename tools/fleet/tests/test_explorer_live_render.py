"""Renderer rules for live view models, each reproduced on a small scrubbed synthetic view model."""
import contextlib
import io
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.resources import short_cmd
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.system import usage_text
from tools.fleet.explorer.tables import _harness_mix, _stage_hue, _stage_items, gauge_cells
from tools.fleet.explorer.view_model import ExplorerViewModel, load_fixture

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"


def _mapping():
    return load_fixture(FIXTURES / "view_model.json").to_mapping()


def _vm(**changes):
    values = _mapping()
    values.update(changes)
    return ExplorerViewModel.from_mapping(values)


def _frame(vm, view="OVERVIEW", cols=140, rows=60, scope="-"):
    return draw(vm, NavState(view=view, title=view, scope=scope), cols, rows, beat=1)


def _find(frame, text, line=""):
    """(row, column) of the first cell run that spells ``text`` on a row that also holds ``line``."""
    for y, row in enumerate(frame.text_rows()):
        x = row.find(text)
        if x >= 0 and line in row:
            return y, x
    raise AssertionError(f"{text!r} is not drawn")


def _key(frame, text, line=""):
    y, x = _find(frame, text, line)
    return frame.cells[y][x][1]


def _route(**changes):
    route = dict(_mapping()["routes"][1])
    route.update(changes)
    return route


class StageRule(unittest.TestCase):
    def test_worker_step_takes_the_hue_of_its_index_in_the_route_stages(self):
        stages = [{"name": name, "st": "done"} for name in ("alpha", "bravo", "charlie")] + [
            {"name": "delta-step", "st": "active"}, {"name": "echo", "st": "todo"}]
        base = dict(stages=stages, worker_stage="delta-step", worker_stage_key=None, worker_step_key=None,
                    worker=["worker", [["codex", "hb_codex"]], [["running 3m", "dim"]]],
                    activity="running", state="working")
        self.assertEqual(_stage_hue({"stages": stages}, "delta-step"), "y")
        self.assertEqual(_stage_hue({"stages": stages}, "not-a-stage"), "g")
        running = _frame(_vm(routes=[_route(**base)]), "ROUTES")
        self.assertEqual(_key(running, "delta-step", "└ worker"), "g_idle")        # (yellow, normal weight) while it runs
        self.assertEqual(_key(running, "running 3m", "└ worker"), "stg3_on")       # (yellow, bold)
        parked = dict(base, worker=["worker", [["codex", "hb_codex"]], [["3m · worker ended", "dim"]]])
        self.assertEqual(_key(_frame(_vm(routes=[_route(**parked)]), "ROUTES"), "delta-step", "└ worker"), "lvl_y_dim")

    def test_every_stage_index_has_a_hue(self):
        items = _stage_items([{"name": f"s{index}", "st": "active"} for index in range(6)])
        keys = [parts[0][1] for parts, _status in items]
        self.assertNotIn("unknown", keys)
        self.assertEqual(keys[:2], ["hb_opencode", "hb_claude"])

    def test_harness_mix_counts_only_drawn_depth2_workers(self):
        base = dict(state="working", harness="claude",
                    worker=["worker", [["codex", "hb_codex"]], [["3m", "dim"]]])

        def text(**changes):
            return "".join(part for part, _key in _harness_mix([_route(**base, **changes)]))
        self.assertIn("claude 1 · codex 0 · opencode 0", text(subworker=None))
        self.assertIn("claude 1 · codex 1 · opencode 0", text(subworker=("execute", "codex", "running")))

    def test_worker_effort_max_is_kept_in_the_folded_cell(self):
        worker = ["worker", [["codex", "hb_codex"], [" (", "dim"], ["gpt-x", "fam_gpt"], ["·", "dim"],
                             ["max", "dead"], [")", "dim"]], [["3m", "dim"]]]
        frame = _frame(_vm(routes=[_route(worker=worker)]), "ROUTES", cols=100)
        self.assertIn("codex·max", "\n".join(frame.text_rows()))


class GaugeRule(unittest.TestCase):
    def test_a_value_above_zero_fills_at_least_one_cell(self):
        self.assertEqual([gauge_cells(6, value) for value in (0, 1, 6, 50, 99, 100)], [0, 1, 1, 3, 5, 6])

    def test_overview_context_gauge_shows_a_small_percentage(self):
        sessions = [dict(session, context=6, window=0.75) for session in _mapping()["sessions"]]
        frame = _frame(_vm(sessions=sessions))
        self.assertIn("━─────", "\n".join(frame.text_rows()))


class CommandRule(unittest.TestCase):
    def test_process_command_keeps_the_script_and_short_options(self):
        self.assertEqual(short_cmd("python -B /srv/work/resume_x.py --request /srv/in/A2.json"),
                         "resume_x.py --request A2.json")
        self.assertEqual(short_cmd("python x.py --request-sha256 0123456789abcdef0123 --seed 7"),
                         "x.py --seed 7")
        self.assertEqual(short_cmd("python x.py --sha=0123456789abcdef0123"), "x.py")
        self.assertEqual(short_cmd("tool --flag"), "tool --flag")


class HeaderRule(unittest.TestCase):
    def test_answered_head_counts_the_first_repos_own_rows(self):
        answered = (("repo-a", (("10:00", "one"), ("10:01", "two"))), ("repo-b", (("09:00", "three"),)))
        system = dict(_mapping()["system"], answered_total=3)
        frame = _frame(_vm(answered=answered, system=system), "QUESTIONS")
        self.assertIn("repo-a 2 · 3 decision records in all projects", "\n".join(frame.text_rows()))


class CutRule(unittest.TestCase):
    def test_route_header_uses_the_cell_before_age_without_overlapping_it(self):
        for cols in (80, 100, 140, 168):
            with self.subTest(cols=cols):
                rows = _frame(_vm(), "ROUTES", cols=cols).text_rows()
                header = next(row[34:] for row in rows if "REPO" in row and "ROUTE" in row)
                labels = ("REPO", "ID", "ROUTE", "KIND", "HARNESS",
                          *(("PROGRESS",) if "PROGRESS" in header else ()), "AGE")
                positions = [header.index(label) for label in labels]
                self.assertEqual(positions, sorted(positions))
                for position, label in zip(positions, labels):
                    self.assertEqual(header[position:position + len(label)], label)
                for index in range(len(labels) - 1):
                    self.assertLess(positions[index] + len(labels[index]), positions[index + 1])

    def test_route_kind_uses_the_cell_before_age_instead_of_dropping_its_last_glyph(self):
        route = _route(capability="lab", mode="eval", intensity="standard", age="22h 32m")
        rows = _frame(_vm(routes=[route], repos=[]), "ROUTES", cols=80).text_rows()
        line = next(row for row in rows if "lab(eval" in row)
        kind = line.index("lab(eval")
        age = line.index("22h 32m")
        self.assertEqual(line[kind:kind + 8], "lab(eval")
        self.assertLess(kind + 8, age)
        self.assertEqual(line[age:age + 7], "22h 32m")
        # REPO now precedes ID; the 80-column pane shares the remaining cells.
        owner = str(route.get("harness"))
        self.assertIn(owner[:5], line)

    def test_system_field_lines_are_cut_at_the_pane_edge_without_an_ellipsis(self):
        system = dict(_mapping()["system"], install={"version": "v1.2.3", "managed": True, "pinned": True,
                                                       "distribution": True},
                      usage_api={"state": "on", "freshness": "9m ago"})
        rows = _frame(_vm(system=system), "SYSTEM", cols=80).text_rows()
        field = next(row for row in rows if "version:" in row)
        self.assertNotIn("…", field)

    def test_overview_summary_is_cut_at_the_screen_edge_without_an_ellipsis(self):
        rows = _frame(_vm(), "OVERVIEW", cols=80).text_rows()
        summary = next(row for row in rows if " sessions · " in row and "working" in row)
        self.assertNotIn("…", summary)

    def test_session_context_line_keeps_last_clear_when_the_time_is_unknown(self):
        sessions = [dict(session, last_clear=None) for session in _mapping()["sessions"]]
        tag = next(seat[0] for seat in _mapping()["seats"])
        frame = _frame(_vm(sessions=sessions), "OVERVIEW", cols=168, scope="session:" + tag)
        context = next(row for row in frame.text_rows() if "context:" in row)
        self.assertIn("· last clear ·", context)


class ModelsRule(unittest.TestCase):
    def _models(self, **changes):
        return dict(_mapping()["models"], **changes)

    def test_main_only_models_print_their_names_not_a_tuple(self):
        models = self._models(main_only={"claude": ("fable",), "codex": ["gpt-6-astra", "other-model"]})
        text = "\n".join(_frame(_vm(models=models), "MODELS").text_rows())
        self.assertIn("claude Fable 5.1  ·  codex gpt-6-astra  ·  codex other-model", text)
        self.assertNotIn("('", text)

    def test_missing_tidy_points_draw_the_configured_bar_not_a_red_one(self):
        def bar_keys(models):
            frame = _frame(_vm(models=models), "MODELS")
            y, x = _find(frame, "claude")
            y = next(index for index, row in enumerate(frame.text_rows())
                     if index > y and row[34:].lstrip().startswith("claude") and "━" in row)
            return [cell[1] for cell in frame.cells[y] if cell[0] == "━"]
        configured = bar_keys(self._models(tidy=[500000, 700000]))
        self.assertEqual(bar_keys(self._models(tidy=None)), configured)
        self.assertIn("g_idle", configured)
        self.assertNotEqual(set(configured), {"g_blocked"})


class PeersRule(unittest.TestCase):
    def test_unknown_context_draws_no_gauge_on_the_card(self):
        sessions = [dict(session, context=None) for session in _mapping()["sessions"]]
        frame = _frame(_vm(sessions=sessions), "PEERS")
        self.assertNotIn("━", "\n".join(row[34:] for row in frame.text_rows()[5:]))


class UsageRule(unittest.TestCase):
    def test_unknown_usage_is_a_dot(self):
        self.assertEqual(usage_text(SimpleNamespace(usage_snapshots={}, sessions=())), "·")


class HeaderCallerFallback(unittest.TestCase):
    @staticmethod
    def snapshot():
        from tools.fleet.model import Session
        from tools.fleet.refresh import LiveSnapshot

        states = ["working"] * 15 + ["idle"] * 2 + ["blocked"]
        sessions = [Session(harness="codex", pid=index + 1, session_tag=f"h{index}",
                             cwd="/synthetic/repo", liveness=state)
                    for index, state in enumerate(states)]
        snapshot = LiveSnapshot(sessions=sessions, hearting={
            "version": "v3.50.3-20-gad247e2b6", "install_method": "unmanaged"})
        snapshot.usage_snapshots = {"codex": {"payload": {"rl_5h": 20, "rl_7d": 30},
                                               "observed_at": 2_000_000_000}}
        names = ("trainer-west-gpu-node-01", "cnn", "xavier", "larynx", "mpwav")
        snapshot.compute_hosts = {"hosts": [
            {"host": name, "self": index == 0, "reachable": True, "cpu_count": 4,
             "cpu_utilization_pct": 25, "memory_total_mib": 8192,
             "memory_used_mib": 4096, "gpus": []}
            for index, name in enumerate(names)
        ]}
        return snapshot, names[0]

    @staticmethod
    def runtime():
        return SimpleNamespace(prepare=lambda snapshot, detail, *, vm, generation, now: (vm, {}))

    def test_live_compose_keeps_source_host_and_draws_bounded_valid_header(self):
        from tools.fleet import render
        from tools.fleet.explorer.grid import cell_width

        snapshot, host_name = self.snapshot()
        with patch.object(render, "_explorer_runtime", return_value=self.runtime()):
            frame = render._explorer_compose(snapshot, 80, 45,
                                              NavState(view="OVERVIEW", scope="-"), now=2_000_000_000)
        row = frame.text_rows()[0]

        self.assertEqual(render._EXPLORER_MODEL.hosts[0]["name"], host_name)
        self.assertEqual(render._EXPLORER_MODEL.compute[0][0], host_name)
        self.assertTrue(row.startswith("⌂ trainer"), row)
        self.assertIn("…", row)
        self.assertIn("⠏ 15", row)
        self.assertIn("● 2", row)
        self.assertIn("● 1", row)
        self.assertIn("hearting v3.50.3-20-gad247e2b6 · unmanaged", row)
        self.assertRegex(row.rstrip(), r"\d{2}:\d{2}$")
        self.assertEqual(cell_width(row), 80)

    def test_render_once_caller_handles_a_valid_overbudget_compute_host(self):
        from tools.fleet import render
        from tools.fleet.explorer.grid import cell_width

        snapshot, host_name = self.snapshot()
        previous_mode = render._EXPLORER_ENABLED
        render.set_explorer_mode(True)
        try:
            with patch.object(render, "_explorer_runtime", return_value=self.runtime()), \
                    patch.object(render, "_collect_memory", return_value=None), \
                    patch.object(render, "_collect_governor", return_value=None), \
                    patch.object(render.gitinfo, "enrich_entities"), \
                    patch.dict(os.environ, {"FLEET_DEMO": "1", "COLUMNS": "80", "LINES": "45"}):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = render.render_once(lambda harness_filter=None: snapshot, None, None)
        finally:
            render.set_explorer_mode(previous_mode)
        lines = output.getvalue().splitlines()
        self.assertEqual(result, 0)
        self.assertEqual(len(lines), 45)
        self.assertEqual(cell_width(lines[0]), 80)
        self.assertTrue(lines[0].startswith("⌂ trainer"), lines[0])
        self.assertIn("…", lines[0])
        self.assertIn("hearting v3.50.3-20-gad247e2b6 · unmanaged", lines[0])
        self.assertRegex(lines[0].rstrip(), r"\d{2}:\d{2}$")
        self.assertEqual(snapshot.compute_hosts["hosts"][0]["host"], host_name)


if __name__ == "__main__":
    unittest.main()
