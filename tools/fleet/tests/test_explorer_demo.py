"""End to end: the ``--demo`` snapshot -> live builder -> draw, at every width, view, page and fold.

This closes the real-data path: no exception, no overlap, nothing past the screen edge, nothing over
the feed/key/status lines, and no hit area over empty cells or below the pane rule. Pages whose
content the demo has no source for (raw output, dialogue scroll strips, 2-way stage groups) are
swept from the checked fixture instead.
"""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState, VIEWS, reduce
from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.tests import explorer_fixture as ef

WIDTHS = (80, 100, 140, 168)
ROWS = 45
FIXTURE = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"
FLEET = Path(__file__).resolve().parents[1] / "fleet.py"


def page_starts(vm):
    starts = [NavState(view=view, title=view) for view in VIEWS]
    starts += [NavState(scope="repo:" + str(repo["id"]), title=str(repo["id"])) for repo in vm.repos]
    starts += [NavState(scope="session:" + str(row["tag"]), title=str(row["tag"])) for row in vm.sessions]
    starts += [NavState(scope="route:" + str(route["id"]), title=str(route["id"])) for route in vm.routes]
    starts += [NavState(usage_open=True, view=view, title=view) for view in VIEWS]
    return starts


class DemoBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vm = ef.demo_model()

    def test_every_harness_fills_the_same_session_fields(self):
        by_harness = {}
        for row in self.vm.sessions:
            by_harness.setdefault(row["harness"], []).append(row)
        self.assertEqual(set(by_harness), {"claude", "codex", "opencode"})
        for harness, rows in by_harness.items():
            for row in rows:
                with self.subTest(harness=harness, tag=row["tag"]):
                    self.assertEqual(set(row), set(self.vm.sessions[0]))
                    fields = ["tag", "state", "repo", "repo_id", "age"]
                    if row["state"] not in ("stale", "dead"):      # a stale row has no live telemetry
                        fields += ["model", "effort", "context"]
                    for field in fields:
                        self.assertIsNotNone(row[field], field)

    def test_the_demo_carries_routes_questions_peers_and_compute(self):
        self.assertTrue(self.vm.routes and self.vm.questions and self.vm.peer_edges["links"])
        self.assertTrue(self.vm.compute and self.vm.usage and self.vm.memory)
        owner = next(route for route in self.vm.routes if route["session_tag"])
        self.assertTrue(owner["stages"])
        self.assertIn(owner["id"], self.vm.nodes)


class DemoSweep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vm = ef.demo_model()

    def sweep(self, cols):
        seen, problems, drawn = set(), [], []
        for index, start in enumerate(page_starts(self.vm)):
            depth = 3 if index == 0 else 2          # every page is a start; folds open from there
            for state, frame, trace in ef.explore_states(self.vm, draw, reduce, start, cols, ROWS, depth, seen):
                drawn.append(state)
                found = ef.frame_violations(frame, trace, cols, ROWS)
                if found:
                    problems.append((state.view, state.scope, state.folds, state.usage_open, found[:3]))
        return drawn, problems

    def test_every_reachable_state_is_clean_at_every_width(self):
        for cols in WIDTHS:
            with self.subTest(cols=cols):
                drawn, problems = self.sweep(cols)
                self.assertEqual(problems[:5], [])
                self.assertGreater(len(drawn), 300)
                self.assertEqual({state.view for state in drawn}, set(VIEWS))
                kinds = {state.scope.split(":")[0] for state in drawn}
                self.assertEqual(kinds, {"-", "repo", "session", "route"})
                self.assertTrue(any(state.folds for state in drawn))
                self.assertEqual({state.usage_open for state in drawn}, {True, False})


class DemoDetailSweep(unittest.TestCase):
    """The same sweep with the selection pumps' results in: seats, cards, conversation, pane text, owner output."""

    @classmethod
    def setUpClass(cls):
        cls.vm = ef.demo_model(detail=True)

    def test_the_selection_fed_sections_are_filled(self):
        vm = self.vm
        self.assertTrue(vm.seats and vm.generations and vm.cards and vm.dialogue and vm.attempts and vm.models)
        row = next(item for item in vm.sessions if item["tag"] == "a7")
        self.assertEqual(row["pane"], "w1:p1")
        self.assertTrue(row["pane_lines"] and row["generation"])

    def test_every_reachable_state_with_the_detail_is_clean_at_every_width(self):
        starts = [NavState(view="HISTORY", title="HISTORY"), NavState(view="MODELS", title="MODELS"),
                  NavState(view="QUESTIONS", title="QUESTIONS"),
                  NavState(scope="session:a7", title="a7"), NavState(scope="repo:demo-app", title="demo-app"),
                  NavState(scope="route:rt-271492e352d13180", title="route")]
        for cols in WIDTHS:
            seen, count = set(), 0
            with self.subTest(cols=cols):
                for start in starts:
                    for state, frame, trace in ef.explore_states(self.vm, draw, reduce, start, cols, ROWS, 3, seen):
                        count += 1
                        found = ef.frame_violations(frame, trace, cols, ROWS)
                        self.assertEqual(found[:3], [], (state.view, state.scope, state.folds, state.scroll))
                self.assertGreater(count, 100)

    def test_the_live_branch_title_and_bare_harness_cells(self):
        frame = draw(self.vm, NavState(), 140, ROWS)
        text = "\n".join(frame.text_rows())
        self.assertIn("(main)", text)                      # a known branch follows the title
        stale = next(item for item in self.vm.sessions if item["state"] == "stale")
        row = next(line for line in frame.text_rows() if f"[{stale['tag']}]" in line and "claude" in line)
        self.assertNotIn("claude (", row)                  # a stale row has no telemetry: the bare harness


class TreeOverflow(unittest.TestCase):
    """A session tree taller than its pane ends with ``… N more rows``; the rows it hides are not clickable."""

    def test_cut_rows_are_counted_and_unclickable_at_every_height(self):
        from dataclasses import replace
        vm = ef.demo_model()
        many = replace(vm, repos=tuple(vm.repos) * 3)
        for cols in WIDTHS:
            for rows in (36, 45, 60):
                frame = draw(many, NavState(), cols, rows)
                text = frame.text_rows()
                limit = min(rows - 3 - 1, 34)
                with self.subTest(cols=cols, rows=rows):
                    note = text[limit - 1][4:20]
                    self.assertRegex(note, r"^… \d+ more rows")
                    hidden = int(note.split()[1])
                    needed = sum(1 + sum(1 + len([r for r in vm.routes if r.get("session_tag") == s["tag"]
                                                  and r.get("state") not in ("done", "dead")])
                                         for s in vm.sessions if s.get("repo_id") == repo["id"])
                                 for repo in many.repos
                                 if any(s.get("repo_id") == repo["id"] for s in vm.sessions))   # no session, no row
                    self.assertEqual(hidden, 13 + needed - limit + 1)
                    for hit in frame.hits:
                        if hit.action[0] in ("scope", "route") and hit.x0 < 32:
                            self.assertLess(hit.y, limit - 1, hit)

    def test_a_tree_that_fits_has_no_note(self):
        text = "\n".join(draw(ef.demo_model(), NavState(), 140, 60).text_rows())
        self.assertNotIn("more rows", text)


class NamelessRepos(unittest.TestCase):
    """A project nobody knows is never named: no ``(unknown)``/``(root)``, no rule over nothing, rows stay."""

    @classmethod
    def setUpClass(cls):
        from dataclasses import replace
        vm = ef.demo_model()
        first = vm.repos[0]["id"]
        cls.hidden_tags = {row["tag"] for row in vm.sessions if row["repo_id"] == first}
        sessions = tuple(dict(row, repo="" if row["repo_id"] == first else row["repo"],
                              repo_id="" if row["repo_id"] == first else row["repo_id"])
                         for row in vm.sessions)
        routes = tuple(dict(route, repo="" if route.get("repo") == first else route.get("repo"))
                       for route in vm.routes)
        spare = dict(next(route for route in routes if route.get("session_tag")), id="rt-docs", repo="docs",
                     session_tag=None, past_from=None)
        repos = tuple(dict(repo, id="", name="") if repo["id"] == first else repo for repo in vm.repos)
        repos += ({"id": "docs", "name": "docs", "done": None, "age": None},)
        cls.vm = replace(vm, sessions=sessions, routes=routes + (spare,), repos=repos)

    def test_no_screen_names_the_unknown_project_or_draws_an_empty_rule(self):
        for cols in WIDTHS:
            with self.subTest(cols=cols):
                seen = set()
                for start in page_starts(self.vm):
                    for state, frame, trace in ef.explore_states(self.vm, draw, reduce, start, cols, ROWS, 2, seen):
                        rows = frame.text_rows()
                        text = "\n".join(rows)
                        self.assertNotIn("(unknown)", text)
                        self.assertNotIn("(root)", text)
                        self.assertEqual(ef.frame_violations(frame, trace, cols, ROWS)[:3], [])
                        if state.view == "OVERVIEW" and state.scope == "-":
                            for index, line in enumerate(rows[:-1]):
                                body = line[34:]
                                if body.lstrip(" ").startswith("┈ "):
                                    below = rows[index + 1][34:]
                                    self.assertIn("[", below, (state.folds, index, line))

    def test_the_overview_rule_is_drawn_only_above_its_sessions(self):
        text = "\n".join(draw(self.vm, NavState(), 140, ROWS).text_rows())
        self.assertNotIn("docs/", text.split("ROUTES")[0])
        tree = "\n".join(line[:32] for line in draw(self.vm, NavState(), 140, ROWS).text_rows())
        self.assertNotIn("docs/", tree)                    # a repo with no session has no tree row either
        for tag in self.hidden_tags:
            self.assertIn(f"[{tag}]", text)

    def test_the_nameless_group_stays_reachable(self):
        frame = draw(self.vm, NavState(), 140, ROWS)
        reachable = {hit.action[-1] for hit in frame.hits if hit.action[0] in ("scope", "fold")}
        for tag in self.hidden_tags:
            self.assertIn(tag, reachable)

    def test_the_builder_turns_hidden_labels_into_no_name(self):
        from tools.fleet.explorer import live
        self.assertEqual(live._repo_of(lambda _cwd: "(unknown)", "/data/x"), "")
        self.assertEqual(live._repo_of(lambda _cwd: "(root)", "/"), "")
        self.assertEqual(live._repo_of(None, None), "")
        self.assertEqual(live._repo_of(None, None, "(unknown)"), "")
        self.assertEqual(live._repo_of(lambda _cwd: "demo", "/data/demo"), "demo")


class FixtureScrollSweep(unittest.TestCase):
    """Raw-output and dialogue strips scroll; the demo has none, the fixture has several."""

    def test_pages_with_scroll_strips_stay_clean_while_wheeling_and_unfolding(self):
        vm = load_fixture(FIXTURE)
        for cols in WIDTHS:
            seen, scrolled = set(), 0
            with self.subTest(cols=cols):
                for scope in ("route:f5", "session:0c", "route:cd", "session:f5", "session:34"):
                    start = NavState(scope=scope, title=scope)
                    for state, frame, trace in ef.explore_states(vm, draw, reduce, start, cols, ROWS, 2, seen):
                        scrolled += bool(state.scroll and any(value for _key, value in state.scroll))
                        found = [item for item in ef.frame_violations(frame, trace, cols, ROWS)
                                 if "past the screen edge" not in item]
                        self.assertEqual(found[:3], [], (scope, state.folds, state.scroll))
                if cols >= 140:             # the narrow layouts have no scroll strip to wheel
                    self.assertGreater(scrolled, 20)


class OnceCommand(unittest.TestCase):
    def test_explorer_once_prints_the_demo_screen_at_every_width_in_process(self):
        from tools.fleet import render
        snapshot, hosts = ef.demo_snapshot()
        snapshot.compute_hosts = hosts
        previous = os.environ.get("COLUMNS"), os.environ.get("LINES")
        render.set_explorer_mode(True)
        try:
            for cols in WIDTHS:
                os.environ["COLUMNS"], os.environ["LINES"] = str(cols), str(ROWS)
                out = io.StringIO()
                with self.subTest(cols=cols), contextlib.redirect_stdout(out):
                    code = render.render_once(lambda harness_filter=None: snapshot, None, None)
                self.assertEqual(code, 0)
                lines = out.getvalue().splitlines()
                self.assertEqual(len(lines), ROWS)
                self.assertTrue(all(len(line) <= cols for line in lines))
                self.assertIn("hearting ›", lines[-1])
        finally:
            render.set_explorer_mode(False)
            for name, value in zip(("COLUMNS", "LINES"), previous):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_fleet_explorer_once_demo_exits_zero(self):
        env = dict(os.environ, COLUMNS="100", LINES=str(ROWS), PYTHONDONTWRITEBYTECODE="1")
        done = subprocess.run([sys.executable, str(FLEET), "--explorer", "--once", "--demo"], env=env,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        self.assertIn("OVERVIEW", done.stdout)


if __name__ == "__main__":
    unittest.main()
