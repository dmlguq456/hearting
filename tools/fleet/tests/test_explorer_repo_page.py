"""Repo detail reuses collected facts without turning memory updates into completed work."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest import mock

from tools.fleet.explorer.frame import draw, measure_pane_max
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import load_fixture


FIXTURE = Path(__file__).parent / "fixtures/explorer/view_model.json"


def pane(frame):
    return "\n".join("".join(cell[0] for cell in row[34:]) for row in frame.cells[4:-4])


class RepoPageTest(unittest.TestCase):
    def setUp(self):
        self.lang = mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "English"})
        self.lang.start()
        self.addCleanup(self.lang.stop)
        vm = load_fixture(FIXTURE)
        self.ident = str(vm.repos[0]["id"])
        self.other = str(vm.repos[1]["id"])
        own = dict(vm.repos[0], done="confirmed-result", done_result="success", age="1m",
                   status={"line": "status-from-doc", "next": "next-from-doc", "questions": 0})
        sessions = tuple(dict(row, title="own-session", last_input="› 1m · human-line")
                         for row in vm.sessions if row.get("repo_id", row.get("repo")) == self.ident)[:1]
        route = dict(vm.routes[0], id="repo-active", repo=self.ident, current=True,
                     state="working", title="active-route", outcome=None)
        done = dict(route, id="repo-done", current=False, state="done", activity="ended",
                    title="closed-route", outcome="success")
        failed = dict(done, id="repo-failed", title="failed-result-sentinel", outcome="failure")
        foreign = dict(route, id="foreign-active", repo=self.other, title="foreign-route-sentinel")
        memory = {"state": "success", "stale": False, "projects": {
            self.ident: {"counts": {"doing": 1, "waiting": 1, "open": 1},
                         "open": [("doing", "11-06", "doing-item"),
                                  ("waiting", "11-06", "waiting-item"),
                                  ("open", "11-06", "backlog-item")]},
            self.other: {"open": [("doing", "11-06", "foreign-work-sentinel")]}}}
        work = [{"project": self.ident, "at": "2026-11-06T12:00:00Z", "title": "small-work",
                 "reference": "journals/2026-11-06.md#entry", "result_targets": [{"path": "greet.py"}]},
                {"project": self.other, "title": "foreign-journal-sentinel"}]
        self.vm = replace(vm, repos=(own, vm.repos[1]), sessions=sessions,
                          routes=(route, done, failed, foreign),
                          memory=((self.ident, (("12:00", "saved", "decision", "memory-save-sentinel"),)),),
                          system=dict(vm.system, project_memory=memory, journal_records=work))
        self.state = NavState(scope="repo:" + self.ident)

    def test_six_sections_scope_and_success_sources(self):
        frame = draw(self.vm, self.state, 168, 90, beat=1)
        text = pane(frame)
        names = ("STATUS", "OPEN WORK", "OPEN ROUTES", "RECENT RESULTS", "RECENT WORK", "SESSIONS")
        positions = [text.index("── " + name) for name in names]
        self.assertEqual(positions, sorted(positions))
        for value in ("status-from-doc", "doing-item", "waiting-item", "active-route",
                      "confirmed-result", "closed-route", "small-work", "greet.py", "human-line"):
            self.assertIn(value, text)
        for value in ("foreign-route-sentinel", "foreign-work-sentinel", "foreign-journal-sentinel",
                      "memory-save-sentinel", "failed-result-sentinel", "backlog-item"):
            self.assertNotIn(value, text)

    def test_existing_fold_and_sidebar_actions_and_scroll_reach_sessions(self):
        frame = draw(self.vm, self.state, 140, 30, beat=1)
        hit = next(hit for hit in frame.hits if hit.action == ("fold", "repo:open", self.ident))
        opened, effects = reduce(self.state, {"type": "click", "action": hit.action}, frame)
        self.assertEqual(effects, ())
        text = pane(draw(self.vm, opened, 140, 90, beat=1))
        self.assertIn("backlog-item", text)
        # The existing page viewport scrolls the whole sequence, including its last session.
        frame = draw(self.vm, opened, 140, 30, beat=1)
        end, _ = reduce(opened, {"type": "key", "key": "end",
                                "max_scroll": measure_pane_max(self.vm, opened, 140, 30)}, frame)
        bottom = draw(self.vm, end, 140, 30, beat=1)
        self.assertIn("own-session", pane(bottom))
        session = str(self.vm.sessions[0]["tag"])
        hit = next(hit for hit in bottom.hits if hit.action == ("scope", session))
        page, effects = reduce(end, {"type": "click", "action": hit.action}, bottom)
        self.assertEqual(page.scope, "session:" + session)
        self.assertEqual(effects, ())

    def test_unknown_zero_and_last_good_are_distinct(self):
        for state, stale, expected in (("unknown", False, "?"), ("success", False, "No open work."),
                                       ("error", True, "last")):
            with self.subTest(state=state, stale=stale):
                system = dict(self.vm.system, project_memory={"state": state, "stale": stale, "projects": {}})
                text = pane(draw(replace(self.vm, system=system), self.state, 168, 90, beat=1))
                block = text.split("── OPEN WORK", 1)[1].split("── OPEN ROUTES", 1)[0]
                self.assertIn(expected, block)
                if state == "unknown":
                    self.assertNotIn("No open work", block)

    def test_unconfirmed_done_label_does_not_become_a_completed_result(self):
        repos = tuple(dict(row, done_result=None) for row in self.vm.repos)
        vm = replace(self.vm, repos=repos, routes=())
        text = pane(draw(vm, self.state, 168, 90, beat=1))
        recent = text.split("── RECENT RESULTS", 1)[1].split("── RECENT WORK", 1)[0]
        self.assertIn("No confirmed completed results.", recent)
        self.assertNotIn("confirmed-result", recent)

    def test_paused_open_route_uses_the_existing_history_boundary(self):
        paused = dict(self.vm.routes[0], state="idle", current=False, activity="ended", title="paused-route")
        text = pane(draw(replace(self.vm, routes=(paused,)), self.state, 168, 90, beat=1))
        block = text.split("── OPEN ROUTES", 1)[1].split("── RECENT RESULTS", 1)[0]
        self.assertIn("paused-route", block)

    def test_last_input_remains_visible_when_the_table_has_no_input_column(self):
        for cols in (80, 100, 140, 168):
            with self.subTest(cols=cols):
                text = pane(draw(self.vm, self.state, cols, 90, beat=1))
                sessions = text.split("── SESSIONS", 1)[1]
                self.assertIn("human-line", sessions)

    def test_narrow_long_cjk_page_keeps_sidebar_and_renderer_does_no_source_reads(self):
        project = {"counts": {"doing": 1}, "open": [("doing", "11-06", "긴 제목" * 1000)]}
        system = dict(self.vm.system, project_memory={"state": "success", "projects": {self.ident: project}})
        vm = replace(self.vm, system=system)
        for cols in (60, 80, 100, 140, 168):
            with self.subTest(cols=cols):
                # Warm the existing language catalog before denying source IO during painting.
                frame = draw(vm, self.state, cols, 45, beat=1)
                short = draw(self.vm, self.state, cols, 45, beat=1)
                self.assertEqual([row[:32] for row in frame.cells], [row[:32] for row in short.cells])
                with mock.patch("builtins.open", side_effect=AssertionError("render must use projections")):
                    actual = draw(vm, self.state, cols, 45, beat=1)
                self.assertEqual(actual.cells, frame.cells)
                self.assertTrue(all(len(row) == cols for row in actual.cells))


if __name__ == "__main__":
    unittest.main()
