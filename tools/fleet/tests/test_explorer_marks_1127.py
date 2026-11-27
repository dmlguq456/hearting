"""11-27 (the person, after the design's before/after pictures: "여덟 가지 모두 지음"): Fleet writes one meaning in one mark.

3. A PEERS card's earlier generation reads ``← gen 5 · 6m`` (the arrow HISTORY's ``gen 5 → [4f]`` answers).
4. An answer wait reads ``◑`` wherever Fleet draws one: the header count, the SESSIONS count, the tree's session and route, the
   OVERVIEW row and its ASK line, ROUTES, the session page's STATE, the PEERS card and its owner line; a recorded ``●`` for it
   reads ``◑`` too. Idle keeps ``●``, a working session its spinner, no observation ``◌``.
6. The place is ``▶``: the choice being chosen on QUESTIONS, the model being picked on MODELS; a single answer's settled ``●`` and
   ``○`` stay.
7. A press that opens another place ends in ``›``: ``(open ›)``, the narrow ``›``, ``u more ›``, a page's ``open ›`` and the narrow
   PANE link; the wide ``⏎ go`` stays a key's name. The press cells are the cells the link was drawn in.
Widths 80, 100 and 132.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import ExplorerViewModel, load_fixture

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
WIDTHS = (80, 100, 132)


def vm_of(edit=None):
    values = load_fixture(FIXTURES / "view_model.json").to_mapping()
    if edit:
        edit(values)
    return ExplorerViewModel.from_mapping(values)


def rows(vm, state, cols, height=45):
    return draw(vm, state, cols, height, beat=1).text_rows()


def session(values, tag):
    return next(item for item in values["sessions"] if item.get("tag") == tag)


def blocked_route(values):
    """A route of [bb] that waits on the person's answer, as the live reader writes one that does not run."""
    route = copy.deepcopy(next(r for r in values["routes"] if r.get("id") == "row1"))
    route.update(id="rt-bbwait", session_tag="bb", state="blocked", activity="ended", tree_label="review")
    values["routes"].append(route)


class Base(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        patch.start()
        self.addCleanup(patch.stop)


class AnswerWaitTest(Base):
    def test_every_answer_wait_reads_half_and_the_other_states_keep_theirs(self):
        def older_record(values):
            blocked_route(values)
            session(values, "09")["answer_wait_marker"] = "●"     # an older record's mark for a wait on an idle session
        vm = vm_of(older_record)
        for cols in WIDTHS:
            with self.subTest(cols=cols):
                shown = rows(vm, NavState(), cols)
                header = shown[0]
                self.assertIn("◑ 1", header)
                self.assertNotIn("● 1 answer", header)
                tally = next(line for line in shown if "SESSIONS" in line)
                self.assertIn("◑1", tally)
                self.assertRegex(tally, r"●\d")                    # idle keeps its dot
                tree = shown[:45]
                self.assertTrue(any("[bb]" in line and "◑" in line[:32] for line in tree), "\n".join(tree))
                self.assertTrue(any("review:" in line and "◑" in line[:32] for line in tree), "\n".join(tree))
                self.assertTrue(any("⠏" in line[:32] for line in tree))   # a working session keeps its spinner
                body = [line[33:] for line in shown]
                # the session's row and its route's row in OVERVIEW's ROUTES section
                self.assertGreaterEqual(sum(1 for line in body if "[bb]" in line and line.lstrip().startswith("◑")), 2, "\n".join(body))
                self.assertTrue(any("[09]" in line and line.lstrip().startswith("◑") for line in body), "\n".join(body))
                unfolded = rows(vm, NavState(opened="bb", folds=(("sopen:OVERVIEW", "bb"),)), cols, 60)
                route_line = next(line for line in unfolded if " ROUTE " in line)          # the unfolded session's ROUTE line
                self.assertIn("◑", route_line, "\n".join(unfolded))
                routes = [line[33:] for line in rows(vm, NavState(view="ROUTES", title="ROUTES"), cols, 60)]
                self.assertTrue(any("[bb]" in line and line.lstrip().startswith("◑") for line in routes), "\n".join(routes))
                page = rows(vm, NavState(scope="session:bb", title="bb"), cols)
                self.assertTrue(any("STATE" in line and "◑ answer needed" in line for line in page), "\n".join(page))
                peers = rows(vm, NavState(view="PEERS", title="PEERS"), cols, 60)
                self.assertTrue(any("◑ [bb]" in line for line in peers), "\n".join(peers))
                self.assertFalse(any("● [bb]" in line for line in peers))

    def test_a_recorded_census_dot_reads_half_at_every_width(self):
        census = {"parts": [("⠏ 3", "spin"), (" working", "dim"), ("  ● 5", "idle"), (" idle", "dim"),
                            ("  ● 2", "blocked"), (" answer needed", "dim"), ("   ", "branch_s")]}
        vm = ExplorerViewModel(census=census, system={"install": {"version": "v4.0.0"}})
        for cols in (60, 80, 100, 132):
            header = rows(vm, NavState(view="OVERVIEW", scope="-"), cols)[0]
            self.assertIn("◑ 2", header, cols)
            self.assertIn("● 5", header, cols)


class EarlierGenerationTest(Base):
    def test_the_card_border_leads_with_the_arrow(self):
        def recent(values):
            for seat in values["seats"]:
                seat[2], seat[3] = 6, "22:47 · 6m ago"
        vm = vm_of(recent)
        for cols in WIDTHS:
            shown = "\n".join(rows(vm, NavState(view="PEERS", title="PEERS"), cols, 60))
            self.assertIn("← gen 5 · 6m", shown, cols)
            self.assertNotIn("◂ gen", shown)



if __name__ == "__main__":
    unittest.main(verbosity=2)
