"""A one-step route's folded ROUTES row says what its doer is on now (supervisor 2026-11-12).

The progress cell of a route with one stage was only ``one-shot ●``; it now carries the open
tool's allow-listed summary (``● python3 tools/run-tests.py``), cut by whole values (a path
gives way to its file name, then the tool name stands alone), and the right-hand ``>_ tool``
cell is not drawn a second time. Nothing observed keeps ``one-shot ●``; a staged route's
folded row is unchanged.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from unittest import mock

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.live import build_view_model
from tools.fleet.explorer.state import NavState
from tools.fleet.model import DispatchJob, Session, WorkProjection

NOW = 1_800_000_000.0
ONE = [{"id": "one-shot", "state": "active", "harness": "claude"}]
STAGED = [{"id": "plan", "state": "done"}, {"id": "execute", "state": "active", "harness": "claude"},
          {"id": "review", "state": "todo"}]


def _owner(rid, title, nodes, **values):
    view = {"cwd": "/synthetic/repo", "capability": "autopilot-code", "nodes": nodes}
    work = WorkProjection(source="route-exact", route_id=rid, _route_view={"view": view, "record": {
        "route_id": rid, "owner_attempt_id": "att-" + rid, "display_title": title}})
    return DispatchJob(key="o-" + rid, slug="task", harness="claude", depth=1, route_id=rid,
                       attempt_id="att-" + rid, parent_sid="sid-parent", liveness="working",
                       work_projection=work, resolved_model="claude-opus-5-5", effort="medium",
                       elapsed_min=47, active_context_tokens=61_000, **values)


def _rows(jobs, width):
    session = Session(harness="claude", pid=10, session_id="sid-parent", session_tag="e9",
                      cwd="/synthetic/repo", title="supervisor", liveness="idle")
    vm = build_view_model({"sessions": [session], "jobs": jobs, "resources": [],
                           "project_of": lambda _: "repo"}, now=NOW)
    with mock.patch("time.time", return_value=NOW):
        rows = draw(vm, NavState(view="ROUTES"), width, 30).text_rows()
    return {title: next(row for row in rows if title in row)
            for title in ("t-run", "t-edit", "t-idle", "t-stage")}


class OneStepFoldedNowTest(unittest.TestCase):
    def jobs(self):
        return [
            _owner("rt-a", "t-run", ONE, _tool_observed=True,
                   exec_tool={"name": "python3", "target": "tools/run-tests.py", "since": NOW - 185}),
            _owner("rt-b", "t-edit", ONE, _tool_observed=True,
                   exec_tool={"name": "Edit", "target": "utilities/session-shell/host.cjs"}),
            _owner("rt-c", "t-idle", ONE),
            _owner("rt-d", "t-stage", STAGED, _tool_observed=True,
                   exec_tool={"name": "python3", "target": "tools/run-tests.py", "since": NOW - 60}),
        ]

    def test_the_progress_cell_carries_the_open_tool_summary_once(self):
        # Review RV-PR1560 follow-up 10 (spec E9 rule 24): the kind word leads the cell
        # while the tool shows, as on the shell panel's third row.
        wide = _rows(self.jobs(), 150)
        self.assertIn("one-shot ● python3 tools/run-tests.py  3m", wide["t-run"])
        self.assertIn("one-shot ● Edit host.cjs", wide["t-edit"])
        for title in ("t-run", "t-edit"):
            self.assertNotIn(">_", wide[title])          # not again in the tool cell
            self.assertEqual(wide[title].count("one-shot"), 1)
            self.assertIn("47m", wide[title])

    def test_a_narrow_row_cuts_whole_values(self):
        narrow = _rows(self.jobs(), 120)
        self.assertIn("one-shot ● Edit", narrow["t-edit"])
        self.assertIn("one-shot ● python3", narrow["t-run"])
        for title in ("t-run", "t-edit"):
            self.assertNotIn("…", narrow[title].split("●", 1)[1])
            self.assertIn("47m", narrow[title])

    def test_nothing_observed_keeps_the_stage_and_a_staged_row_is_unchanged(self):
        for width in (120, 150):
            rows = _rows(self.jobs(), width)
            self.assertIn("one-shot ●", rows["t-idle"])
            self.assertNotIn("● python3", rows["t-stage"])
            self.assertIn("execute ●", rows["t-stage"])
        self.assertIn(">_ python3", _rows(self.jobs(), 150)["t-stage"])


if __name__ == "__main__":
    unittest.main()
