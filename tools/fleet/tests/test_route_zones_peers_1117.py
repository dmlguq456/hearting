"""11-17 (the person: route zone sketch 1, the PEERS sketch, and the single-stage staged route): Fleet's session page reads
the shell panel's grammar.

  * The session page's route list is split by named rules, ` ─ direct ─`, ` ─ dispatch ─`, ` ─ request ─`, in that order (a zone
    with no row has none); the requests are the third zone, under the routes, and the section head counts the routes alone.
  * `PEERS  [c7] ⚑ ─ [d4] [3b]` stands under the page title, before MODEL, the same order from every window; a window with no
    relation has no line; short of cells the last numbers go as `+N`; a number that is not this window's opens that page.
  * A route staged into one stage reads `code(standard) · staged 1/1: execute`, as the panel does.
  * The supervision relation is the one the steward columns read (same repository, registered), and the panel's cache map.
  * `+N`: the numbers a group lost are counted inside that group; a group whose head does not fit goes whole, and no count is put
    on the groups left. The older PEERS row (the sessions a message links, and the `PEERS tab ›` link) stays under MESSAGES: whole
    in a window with no supervision, and in a window with one it leaves off the numbers the top line names.
  * The panel's route rows carry each session's NOW (the owner's, each worker's) from the value Fleet's session rows carry,
    with no read of its own.
Everything is synthetic.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import re
import time
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

from tools.fleet.explorer import live, tables
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.grid import CellGrid
from tools.fleet.explorer.state import NavState

NOW = time.mktime((2026, 11, 17, 22, 0, 0, 0, 0, -1))
X = 34
NAMES = ["plan", "plan-check", "execute", "impl-review", "test", "report"]


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def session(tag, harness="claude", **values):
    row = {"harness": harness, "session_id": "sid-" + tag, "session_tag": tag, "cwd": "/synthetic/hearting",
           "liveness": "idle", "title": "title " + tag, "summary": "now " + tag, "context_pct": 42, "window": 1.0}
    row.update(values)
    return row


def watch(tag, harness="claude"):
    return {"harness": harness, "session_id": "sid-" + tag, "name": tag, "kind": "watch", "ts": iso(NOW - 8000),
            "source": "watch"}


def request(requester="3b", recipient="a1"):
    created, closed = NOW - 6000, NOW - 300
    return {"schema": "peer-request-v1", "id": "rq-0a1b2c", "title": "a request title", "created": created, "updated": closed,
            "requester": {"harness": "claude", "session_id": "sid-" + requester, "name": requester},
            "recipient": {"harness": "codex", "session_id": "sid-" + recipient, "name": recipient},
            "state": "final",
            "revisions": [{"n": 1, "label": "", "transfer_ref": "a" * 32, "created": created, "state": "current",
                           "followups": []}],
            "reports": [{"n": 1, "revision": 1, "kind": "final", "result": "", "transfer_ref": "b" * 32, "at": closed,
                         "late": False}], "cancel": None, "sending": []}


def observation(records):
    sides = {}
    for record in records:
        sides.setdefault("claude:sid-" + record["requester"]["name"], []).append([record["id"], "out"])
        sides.setdefault("codex:sid-" + record["recipient"]["name"], []).append([record["id"], "in"])
    return {"sent": {}, "requests": {r["id"]: r for r in records}, "sides": sides,
            "rows": {"a" * 32: {"state": "received"}, "b" * 32: {"state": "received"}},
            "refs": ["a" * 32, "b" * 32] if records else []}


def model(sessions, records=(), routes=()):
    seen = {"state": "success", "observed_at": NOW}
    if records:
        seen["messages"] = observation(records)
    vm = live.build_view_model({"sessions": sessions, "resources": [], "observations": {"optional:peers": seen}}, now=NOW)
    return replace(vm, routes=tuple(routes)) if routes else vm


def route(ident, tag, shape, title, **extra):
    row = {"id": ident, "session_tag": tag, "repo": "hearting", "title": title, "state": "working", "shape": shape,
           "capability": "code", "intensity": "standard", "stages": [], "legs": (), "age": "2m", "current": True,
           "harness": "claude", "model": ("Opus 5.5",), "effort": "high", "worker": [], "owner_gauge": [41, None]}
    row.update(extra)
    return row


def page(vm, tag, cols=120, lines=45):
    rows = draw(vm, NavState(scope="session:" + tag), cols, lines, beat=1).text_rows()
    return [row[X:].rstrip() for row in rows]


def tree():
    """c7 supervises d4 and 3b (codex); 3b supervises a1; e9 stands alone; zz is a session of another repository c7 also lists."""
    return [session("c7", steward=True, steward_targets=[watch("d4"), watch("3b", "codex"), watch("zz")]),
            session("d4"), session("3b", "codex", steward=True, steward_targets=[watch("a1")]), session("a1"), session("e9"),
            session("zz", cwd="/synthetic/other")]


def peers_line(lines):
    return next((line for line in lines if line.lstrip().startswith("PEERS")), None)


class SessionPageZones(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        env.start()
        self.addCleanup(env.stop)
        stages = [{"name": name, "st": "done" if i < 2 else "active" if i == 2 else "todo"} for i, name in enumerate(NAMES)]
        self.direct = route("r1", "d4", "direct", "alpha", intensity="direct")
        self.dispatch = route("r2", "d4", "staged", "beta", stages=stages, legs=(("code", "active", "3/6"),))
        self.sessions = tree()

    def test_the_routes_stand_in_their_zones_under_named_rules_and_the_requests_are_the_third(self):
        vm = model([session("3b"), session("a1", "codex")],
                   [request()], [route("r1", "3b", "direct", "alpha", intensity="direct"),
                                 route("r2", "3b", "staged", "beta",
                                       stages=self.dispatch["stages"], legs=self.dispatch["legs"])])
        lines = page(vm, "3b")
        rules = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*─ (direct|dispatch|request) ─+", line)]
        self.assertEqual([lines[i].split()[1] for i in rules], ["direct", "dispatch", "request"])
        self.assertTrue(any("alpha" in line for line in lines[rules[0]:rules[1]]))
        self.assertTrue(any("beta" in line for line in lines[rules[1]:rules[2]]))
        self.assertTrue(any("→ [a1] codex" in line for line in lines[rules[2]:]))
        head = next(line for line in lines if re.match(r"\d+ ROUTES|ROUTES", line))
        self.assertRegex(head, r"^2 ROUTES ─+$")                      # the routes alone: no `PEER REQUEST` tab
        self.assertFalse(any("PEER REQUEST" in line for line in lines))

    def test_a_zone_with_no_row_has_no_rule_and_requests_alone_read_routes_and_one_rule(self):
        vm = model([session("3b"), session("a1", "codex")], [], [route("r2", "3b", "staged", "beta")])
        names = [line.split()[1] for line in page(vm, "3b") if re.fullmatch(r"\s*─ (direct|dispatch|request) ─+", line)]
        self.assertEqual(names, ["dispatch"])
        only = model([session("3b"), session("a1", "codex")], [request()])
        lines = page(only, "3b")
        self.assertTrue(any(re.fullmatch(r"ROUTES ─+", line) for line in lines), lines)
        self.assertEqual([line.split()[1] for line in lines if re.fullmatch(r"\s*─ (direct|dispatch|request) ─+", line)],
                         ["request"])
        none = page(model([session("3b")]), "3b")
        self.assertFalse(any(re.fullmatch(r"\s*─ (direct|dispatch|request) ─+", line) for line in none))

    def test_the_zone_names_are_the_zones_colors_and_the_other_tables_have_no_rules(self):
        vm = model([session("3b")], [], [route("r1", "3b", "direct", "alpha", intensity="direct")])
        grid = draw(vm, NavState(scope="session:3b"), 120, 45, beat=1)
        text = grid.text_rows()
        row = next(i for i, line in enumerate(text) if re.search(r"─ direct ─", line))
        at = text[row].index("direct")
        self.assertEqual(grid.cells[row][at][1], "zone_direct")
        for scope in ("ROUTES", "OVERVIEW"):
            rows = draw(vm, NavState(scope=scope) if scope == "ROUTES" else NavState(), 140, 45, beat=1).text_rows()
            self.assertFalse(any(re.search(r"─ (direct|dispatch|request) ─", line) for line in rows), scope)

    def test_the_direct_word_is_not_a_bracket_and_a_real_intensity_stays(self):
        # (11-18: `code` is a field's own name, so its word and bracket wear the field's hue.)
        self.assertEqual(tables._kind_cell({"capability": "code", "shape": "direct", "intensity": "direct"}), [("code", "kind_code")])
        self.assertEqual(tables._kind_cell({"capability": "code", "shape": "direct", "intensity": ""}), [("code", "kind_code")])
        self.assertEqual(tables._kind_cell({"capability": "code", "shape": "direct", "intensity": "quick"}),
                         [("code", "kind_code"), ("(quick)", "kindq_code")])
        self.assertEqual(tables._kind_cell({"capability": "code", "shape": "staged", "intensity": "standard"}),
                         [("code", "kind_code"), ("(standard)", "kinds_code")])
        # A route with no field hue keeps the bold body word and the dim bracket.
        self.assertEqual(tables._kind_cell({"capability": "spec", "shape": "staged", "intensity": "standard"}),
                         [("spec", "grp"), ("(standard)", "dim")])


class SinglestageStaged(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        env.start()
        self.addCleanup(env.stop)

    def cell(self, route, width):
        grid = CellGrid(width, 1)
        grid.beat = 1
        tables._draw_route_progress(grid, 0, width, 0, route, card=False)
        return "".join(c[0] for c in grid.cells[0]).rstrip()

    def test_a_staged_route_with_one_stage_reads_the_stage_line_the_panel_reads(self):
        one = {"state": "working", "shape": "staged", "capability": "code", "intensity": "standard",
               "stages": [{"name": "execute", "st": "active"}], "legs": (("code", "active", "1/1"),)}
        self.assertEqual(self.cell(one, 60), "code(standard) · staged 1/1: execute")
        self.assertEqual(self.cell(one, 24), "staged 1/1: execute")
        self.assertNotIn("●", self.cell(one, 60))
        self.assertNotIn("(execute", self.cell(one, 60))
        # The route its owner does alone (shape one-shot-owner) stays as it was.
        solo = {"state": "working", "shape": "one-shot-owner", "capability": "code", "intensity": "quick",
                "stages": [{"name": "one-shot", "st": "active"}], "legs": (("code", "active", None),)}
        self.assertEqual(self.cell(solo, 60), "code(quick)")


class PeersLine(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        env.start()
        self.addCleanup(env.stop)
        self.vm = model(tree())

    def test_the_three_windows_read_the_same_order_under_the_title_before_model(self):
        for tag, want in (("c7", r"\[c7\] ⚑ ─ \[d4\] \[3b\]"), ("d4", r"\[c7\] ⚑ ─ \[d4\]"),
                          ("3b", r"\[c7\] ⚑ ─ \[3b\] · \[3b\] ⚑ ─ \[a1\]"), ("a1", r"\[3b\] ⚑ ─ \[a1\]")):
            lines = page(self.vm, tag)
            top = next(i for i, line in enumerate(lines) if line.startswith("── [" + tag + "]"))
            self.assertRegex(lines[top + 1], r"^  PEERS +" + want + r"(   PEERS tab ›)?$", (tag, lines[top + 1]))
            self.assertTrue(lines[top + 2].lstrip().startswith("MODEL"), tag)
            # one supervision line, once (the older row under MESSAGES is the message links', and a different row)
            self.assertEqual(sum(1 for line in lines if line.lstrip().startswith("PEERS") and " ⚑ ─ " in line), 1, tag)

    def test_a_window_with_no_relation_has_no_line_and_no_empty_row(self):
        lines = page(self.vm, "e9")
        self.assertIsNone(peers_line(lines))
        top = next(i for i, line in enumerate(lines) if line.startswith("── [e9]"))
        self.assertTrue(lines[top + 1].lstrip().startswith("MODEL"))

    def test_only_the_same_repository_counts_as_supervision(self):
        self.assertEqual({k: list(v) for k, v in self.vm.peer_edges["supervision"].items()}, {"c7": ["d4", "3b"], "3b": ["a1"]})   # zz is another repository's
        zz = page(self.vm, "zz")
        self.assertFalse(any(" ⚑ ─ " in line for line in zz))
        self.assertNotIn("[zz]", peers_line(page(self.vm, "c7")))

    def test_it_carries_the_harness_colors_the_own_number_bold_the_flag_pink_and_no_state(self):
        grid = draw(self.vm, NavState(scope="session:3b"), 120, 45, beat=1)
        text = grid.text_rows()
        row = next(i for i, line in enumerate(text) if "PEERS" in line and "⚑" in line)
        line = text[row]

        def key(token, nth=0):
            at = -1
            for _ in range(nth + 1):
                at = line.index(token, at + 1)
            return grid.cells[row][at][1]
        self.assertEqual(key("[c7]"), "hb_claude")
        self.assertEqual(key("[3b]"), "hbb_codex")
        self.assertEqual(key("[3b]", 1), "hbb_codex")
        self.assertEqual(key("[a1]"), "hb_claude")
        self.assertEqual(key("⚑"), "tag_steward")
        self.assertEqual(key("─"), "dim")
        self.assertNotRegex(line.split("PEERS", 1)[1], r"working|idle|blocked|●|⠏")

    def test_short_of_cells_the_last_numbers_go_as_plus_n(self):
        vm = model([session("c7", steward=True, steward_targets=[watch(t) for t in ("d1", "d2", "d3", "d4", "d5")]),
                    *[session(t) for t in ("d1", "d2", "d3", "d4", "d5")]])
        whole = peers_line(page(vm, "c7", cols=140))
        self.assertRegex(whole, r"\[c7\] ⚑ ─ \[d1\] \[d2\] \[d3\] \[d4\] \[d5\]")
        narrow = peers_line(page(vm, "c7", cols=70))
        self.assertRegex(narrow, r"\+\d")
        self.assertNotIn("[d5]", narrow)
        self.assertIn("[c7] ⚑ ─ [d1]", narrow)

    def test_a_group_with_no_number_left_keeps_its_head_and_its_count_else_it_goes_whole(self):
        """The reviewed fault: `[c7] ⚑ ─ [3b] +2` read as c7 supervising three, while 3b supervises two."""
        many = ("a1", "a2", "a3", "a4", "a5")
        vm = model([session("c7", steward=True, steward_targets=[watch("3b", "codex")]),
                    session("3b", "codex", steward=True, steward_targets=[watch(t) for t in many]),
                    *[session(t) for t in many]])
        self.assertEqual(peers_line(page(vm, "3b", cols=140)).split("PEERS", 1)[1].split("   PEERS tab")[0].strip(),
                         "[c7] ⚑ ─ [3b] · [3b] ⚑ ─ [a1] [a2] [a3] [a4] [a5]")
        for cols in range(70, 100):
            line = peers_line(page(vm, "3b", cols=cols)).split("PEERS", 1)[1].split("   PEERS tab")[0].strip()
            self.assertNotRegex(line, r"\] \+\d+ ·", (cols, line))
            if "+" in line:                                      # the count is inside the group that lost the numbers
                self.assertRegex(line, r"· \[3b\] ⚑ ─( \[a\d\])* \+\d$", (cols, line))
            else:
                self.assertRegex(line, r"^\[c7\] ⚑ ─ \[3b\]( · \[3b\] ⚑ ─( \[a\d\])+)?$", (cols, line))
        # A head that does not fit takes the group out whole, with no count on the one left.
        narrow = peers_line(page(vm, "3b", cols=70)).split("PEERS", 1)[1].strip()
        self.assertTrue(narrow.startswith("[c7] ⚑ ─ [3b]"), narrow)
        self.assertNotRegex(narrow, r"\] \+\d")

    def test_the_older_row_stays_where_no_supervision_stands_and_leaves_off_what_the_top_line_names(self):
        # A window a message links, with no supervision: the older row as it stood (`PEERS  [d4]   PEERS tab ›`).
        vm = model(tree())
        linked = replace(vm, peer_edges={**vm.peer_edges, "supervision": {},
                                         "links": [(("e9", "claude"), ("d4", "claude"), "message", "19:46", "")]})
        lines = page(linked, "e9")
        self.assertIsNone(next((line for line in lines if re.match(r"\s+PEERS +\[\w+\] ⚑", line)), None))
        old = [line for line in lines if re.match(r"\s+PEERS  \[d4\]   PEERS tab ›$", line)]
        self.assertEqual(len(old), 1, lines)
        # A supervised window: the top line has the supervision, the older row only what it does not (here the message link),
        # and the tab link stays on the row where the top line has no room for it.
        both = replace(vm, peer_edges={**vm.peer_edges,
                                       "links": list(vm.peer_edges["links"]) + [(("d4", "claude"), ("e9", "claude"), "message", "19:46", "")]})
        wide = page(both, "d4", cols=120)
        self.assertTrue(any(re.match(r"\s+PEERS +\[c7\] ⚑ ─ \[d4\]   PEERS tab ›$", line) for line in wide), wide)
        self.assertTrue(any(re.match(r"\s+PEERS  \[e9\]   PEERS tab ›$", line) for line in wide), wide)
        self.assertFalse(any(re.match(r"\s+PEERS  \[c7\]", line) for line in wide), wide)   # c7 is named on top already
        narrow = page(vm, "d4", cols=70)
        self.assertTrue(any(re.match(r"\s+PEERS +\[c7\] ⚑ ─ \[d4\]$", line) for line in narrow), narrow)
        self.assertTrue(any(re.match(r"\s+PEERS tab ›$", line) for line in narrow), narrow)     # the link is not lost
        # The link is a press target on the older row.
        grid = draw(linked, NavState(scope="session:e9"), 120, 45, beat=1)
        row = next(i for i, text in enumerate(grid.text_rows()) if re.search(r"PEERS  \[d4\]   PEERS tab ›", text))
        self.assertIn(("tab", "PEERS", "e9"), {tuple(hit.action) for hit in grid.hits if hit.y == row})
        # A cut number is not "named": the older row lists it.
        three = page(vm, "3b", cols=70)
        self.assertTrue(any(re.match(r"\s+PEERS  \[a1\]   PEERS tab ›$", line) for line in three), three)

    def test_a_number_that_is_not_this_windows_opens_its_page(self):
        grid = draw(self.vm, NavState(scope="session:3b"), 120, 45, beat=1)
        text = grid.text_rows()
        row = next(i for i, line in enumerate(text) if "PEERS" in line and "⚑" in line)
        hits = {tuple(hit.action) for hit in grid.hits if hit.y == row}
        self.assertIn(("page", "session", "c7"), hits)
        self.assertIn(("page", "session", "a1"), hits)
        self.assertNotIn(("page", "session", "3b"), hits)               # its own number is no link


class RouteNowFeed(unittest.TestCase):
    """The panel's route rows carry each session's NOW: Fleet's own value, already on the job (no read of its own)."""

    @staticmethod
    def job(**values):
        base = dict(harness="claude", resolved_model="claude-opus-5-5", effort="high", liveness="working", exec_tool=None,
                    subagents=(), attempt_id="a1", summary="fixing the review findings", depth=1, context_pct=41,
                    active_context_tokens=None, worker_type="owner", route_node=None, elapsed_min=42)
        base.update(values)
        return types.SimpleNamespace(**base)

    def test_the_owner_and_each_worker_carry_their_sessions_now_and_a_dead_route_none(self):
        from fleet import session_line
        owner = self.job()
        worker = self.job(attempt_id="w1", summary="adding the field to the route record", worker_type="stage", route_node="execute",
                          depth=2, context_pct=22, resolved_model="claude-sonnet-5-5", elapsed_min=12)
        silent = self.job(attempt_id="w2", summary=None, worker_type="stage", route_node="plan", depth=2)
        row = {"id": "r", "worker_stage": "execute", "state": "working",
               "worker_contexts": [{"stage": "execute", "attempt": "w1", "harness": "claude"}]}
        with mock.patch("fleet.titles.fresh_summary_with_ts", side_effect=AssertionError("a read of its own")), \
                mock.patch("fleet.titles.fresh_summary", side_effect=AssertionError("a read of its own")):
            doer = session_line.route_doer(row, [owner, worker], live._get, live)
            tree = session_line.route_tree(row, [owner, worker], owner, doer, live._get, live, 1000.0, "working")
            alone = session_line.route_doer({"state": "working"}, [owner], live._get, live)
            quiet = session_line.route_doer({"state": "working", "worker_stage": "plan"}, [owner, silent], live._get, live)
            dead = session_line.route_doer({"state": "dead"}, [owner], live._get, live)
        self.assertEqual(doer["now"], "adding the field to the route record")        # the worker leads the row
        self.assertEqual(tree["owner_doer"]["now"], "fixing the review findings")
        self.assertEqual(tree["workers_live"][0]["now"], "adding the field to the route record")
        self.assertEqual(alone["now"], "fixing the review findings")
        self.assertNotIn("now", quiet)                                               # none: the panel draws no NOW row
        self.assertNotIn("now", dead)

    def test_the_value_is_bounded_and_reaches_the_panels_paint_rows(self):
        from fleet import session_line, session_panel
        self.assertNotIn("now", session_line._doer_values({"now": "x" * 400}))               # over its bound: left out
        row = {"id": "r1", "title": "t", "state": "working", "shape": "staged", "now": "n" * 400, "harness": "claude", "model": "Opus 5.5",
               "owner_doer": {"harness": "claude", "model": "Opus 5.5", "now": "owner is on it"},
               "workers_live": [{"stage": "execute", "marker": "working", "role": "worker", "harness": "claude", "model": "Sonnet 5.5",
                                 "now": "worker is on it"}]}
        shown = session_panel.presentation({}, {"route_panels": {"claude\0sid": [row]}}, "claude", "sid", 60)["routes"][0]
        self.assertEqual(shown["owner_paint"]["now"], "owner is on it")
        self.assertEqual(shown["workers_live"][0]["now"], "worker is on it")
        self.assertLessEqual(len(shown["now"]), 160)


class SupervisionFeed(unittest.TestCase):
    def test_the_panels_map_names_each_window_its_supervisors_and_the_ones_it_supervises(self):
        got = live.session_peers({"sessions": tree(), "resources": [], "observations": {}}, NOW)
        self.assertEqual(got["claude\0sid-c7"], {"tag": "c7", "supervisors": [], "targets": [["d4", "claude"], ["3b", "codex"]]})
        self.assertEqual(got["claude\0sid-d4"], {"tag": "d4", "supervisors": [["c7", "claude"]], "targets": []})
        self.assertEqual(got["codex\0sid-3b"], {"tag": "3b", "supervisors": [["c7", "claude"]], "targets": [["a1", "claude"]]})
        self.assertNotIn("claude\0sid-e9", got)                         # no relation: no entry
        self.assertNotIn("claude\0sid-zz", got)                         # another repository: not supervision

    def test_the_session_records_a_pass_worked_out_are_used_again_not_worked_out_twice(self):
        snapshot = {"sessions": tree(), "resources": [], "observations": {}}
        records = []
        live.session_route_status(snapshot, NOW, include_details=True, records_out=records)
        self.assertEqual(len(records), 1)
        with mock.patch.object(live, "_session_records", side_effect=AssertionError("worked out again")):
            again = live.session_peers(snapshot, NOW, records=records[0])
        self.assertEqual(again, live.session_peers(snapshot, NOW))


if __name__ == "__main__":
    unittest.main()
