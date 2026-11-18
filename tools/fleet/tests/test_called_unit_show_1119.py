"""11-19 a unit a worker called stands below its caller (the registry row names the caller: `caller_attempt_id`).

  * The caller's own route reads as it did before the call (a stage worker, a route's owner doing the work alone, the worker of a
    one-unit route; an owner that shares its slug with the stage worker; the called unit running or just ended): the called row's
    `parent_slug` and its calling session's id are not owner links.
  * `live._stand_called_units` follows the caller's attempt to its row and its route (on, while that is a called unit's), lists
    the legs that still run on that route as `called`, and takes them out of the list of routes: no count of routes holds one. A
    chain that breaks (the calling row is not read) or turns back on itself leaves the leg the route it was.
  * The panel's route rows carry them under the worker (or the lead) that called, and the tables draw one indented row each
    under the route, in its columns; the popup's unfolded rows draw one under the worker rows.
Everything is synthetic: the rows are the registry's own text, read by the collector's own reader.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fleet import route as routemod, session_line, session_panel  # noqa: E402
from fleet.collectors import dispatch  # noqa: E402
from fleet.explorer import live, pages, tables  # noqa: E402
from fleet.explorer.grid import CellGrid  # noqa: E402
from fleet.explorer.state import NavState  # noqa: E402
from fleet.model import Session  # noqa: E402
from fleet.projection import attach_projections  # noqa: E402

NOW = time.time()
CWD = "/synthetic/proj"
SID = "sid-main"
OWNER, WORKER, CALLED, CALLED2 = ("att-" + c * 32 for c in "aefg")
STAGES = ["plan", "plan-check", "execute", "impl-review", "test", "report"]


def att(char):
    return "att-" + char * 32


class World:
    """One temp directory, one registry, the route records the rows name."""

    def __init__(self, test):
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        self.records, self.rows, self.liveness, self.evidence = {}, [], {}, {}

    def seal(self, record):
        record["route_hash"] = routemod.route_hash(record)
        record["route_id"] = "rt-" + record["route_hash"].split(":", 1)[1][:16]
        path = self.tmp / (record["route_id"] + ".route.json")
        path.write_text(json.dumps(record), encoding="utf-8")
        self.records[record["route_id"]] = record
        return record, str(path)

    def staged(self, title="rate limiting"):
        nodes = [{"id": name, "kind": "pipeline-stage", "depth": 2, "depends_on": [STAGES[i - 1]] if i else [], "role": "worker",
                  "write_scope": [name + "/**"], "resource_class": "normal", "completion_gate": "code-" + name,
                  "unit": "dev/backend" if name == "execute" else "plan/code"} for i, name in enumerate(STAGES)]
        return self.seal({"schema_version": 1, "capability": "autopilot-code", "capability_mode": "dev", "requested_intensity": "standard",
                          "effective_intensity": "standard", "execution_topology": "staged", "owner_depth": 1, "tracking": "tracked",
                          "spec_touch": False, "display_title": title, "cwd": CWD, "artifact_root": CWD + "/.agent_reports",
                          "nodes": nodes, "completion_gates": ["code-" + n for n in STAGES], "human_gates": [], "owner_attempt_id": OWNER})

    def solo(self, title="small change"):
        nodes = [{"id": "one-shot", "kind": "pipeline-stage", "depth": 1, "depends_on": [], "role": "owner", "write_scope": ["**"],
                  "resource_class": "normal", "completion_gate": "code-one-shot"}]
        return self.seal({"schema_version": 1, "capability": "autopilot-code", "capability_mode": "dev", "requested_intensity": "quick",
                          "effective_intensity": "quick", "execution_topology": "one-shot-owner", "owner_depth": 1, "tracking": "tracked",
                          "spec_touch": False, "display_title": title, "cwd": CWD, "artifact_root": CWD + "/.agent_reports",
                          "nodes": nodes, "completion_gates": ["code-one-shot"], "human_gates": [], "owner_attempt_id": OWNER})

    def unit(self, unit, field, owner, title, slug, ephemeral=False):
        nodes = [{"id": "unit", "kind": "pipeline-stage", "depth": 1, "depends_on": [], "role": "worker", "write_scope": ["**"],
                  "resource_class": "normal", "completion_gate": "unit-complete", "unit": unit,
                  "worker_type": "review" if "review" in unit else "worker"}]
        root = str(self.tmp / "runtime/ephemeral/0123456789ab/.agent_reports") if ephemeral else CWD + "/.agent_reports"
        return self.seal({"schema_version": 1, "capability": "unit", "field": field, "requested_intensity": "quick",
                          "effective_intensity": "quick", "execution_topology": "one-shot-owner", "selection": {"shape": "solo"},
                          "owner_depth": 1, "tracking": "tracked", "spec_touch": False, "display_title": title, "cwd": CWD,
                          "artifact_root": root, "nodes": nodes, "completion_gates": ["unit-complete"],
                          "human_gates": [], "owner_attempt_id": owner, "slug": slug, **({"ephemeral": True} if ephemeral else {})})

    def row(self, status, slug, meta, minutes_ago=0):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - minutes_ago * 60))
        self.rows.append("\t".join((stamp, status, "repo", CWD, slug, ",".join(f"{k}={v}" for k, v in meta.items()))) + "\n")
        if "attempt_id" in meta:
            self.liveness[meta["attempt_id"]] = "done" if status == "done" else "working"

    def owner(self, rec, path, minutes=40):
        self.row("open", "rate-limit", {"attempt_id": OWNER, "dispatch_depth": 1, "worker_type": "owner", "capability": "code",
                 "assigned_contract": "autopilot-code", "harness": "claude", "model": "claude-opus-5-5", "effort": "xhigh",
                 "owner_route_id": rec["route_id"], "owner_route_hash": rec["route_hash"], "owner_route_file": path,
                 "workflow_completion": "runtime-v1", "intensity": "standard", "parent_sid": SID}, minutes)

    def stage(self, rec, path, slug="rate-limit-execute", attempt=WORKER, minutes=12):
        self.row("open", slug, {"attempt_id": attempt, "dispatch_depth": 2, "worker_type": "stage", "route_id": rec["route_id"],
                 "route_hash": rec["route_hash"], "route_file": path, "route_node": "execute", "parent": "rate-limit",
                 "harness": "claude", "model": "claude-sonnet-5-5", "effort": "high", "unit": "dev/backend", "parent_sid": SID}, minutes)

    def solo_owner(self, rec, path, minutes=20):
        self.row("open", "small-change", {"attempt_id": OWNER, "dispatch_depth": 1, "worker_type": "owner", "capability": "code",
                 "assigned_contract": "autopilot-code", "harness": "claude", "model": "claude-opus-5-5", "effort": "xhigh",
                 "route_id": rec["route_id"], "route_hash": rec["route_hash"], "route_file": path, "route_node": "one-shot",
                 "workflow_completion": "runtime-v1", "intensity": "quick", "parent_sid": SID}, minutes)

    def called(self, attempt, caller, unit, field, title, slug, status="open", minutes=3, model="claude-opus-5-5", effort="xhigh",
               ephemeral=False):
        rec, path = self.unit(unit, field, attempt, title, slug, ephemeral)
        self.row(status, slug, {"attempt_id": attempt, "dispatch_depth": 1, "worker_type": "review" if "review" in unit else "worker",
                 "capability": "unit", "unit": unit, "route_id": rec["route_id"], "route_hash": rec["route_hash"], "route_file": path,
                 "route_node": "unit", "harness": "claude", "model": model, "effort": effort, "workflow_completion": "runtime-v1",
                 "intensity": "quick", **({"caller_attempt_id": caller} if caller else {}), "parent_sid": SID}, minutes)
        if status == "done":
            self.evidence[rec["route_id"]] = {"unit": {"status": "done", "slug": "unit", "ts": NOW - 60, "pid": None, "harness": "claude",
                                                       "model": "Opus 5.5", "effort": "xhigh", "completion_gate": "unit-complete",
                                                       "note": None, "route_file": path, "route_hash": None}}
        return rec

    def snapshot(self, ledger=()):
        path = self.tmp / "jobs.log"
        path.write_text("".join(self.rows), encoding="utf-8")
        jobs, bad = dispatch._scan_jobs_log(path, set())
        assert bad == 0
        for job in jobs:
            job.liveness = self.liveness.get(job.attempt_id, job.liveness)
            job.pid = None
        session = Session(harness="claude", pid=os.getpid(), cwd=CWD, session_id=SID, title="main session", model="Opus 5.5",
                          effort="xhigh", ctx_pct=31, elapsed_min=95, branch="main", liveness="working", session_tag="4f",
                          herdr_attached=True, summary="waiting on the work")
        from fleet import route_chain
        ledger_rows = [{"route_id": rid, "capability": self.records[rid]["capability"], "route_file": str(self.tmp / (rid + ".route.json")),
                        "route_hash": self.records[rid]["route_hash"], "ts": NOW - 3000 + i, "event": "compose"}
                       for i, rid in enumerate(ledger)]
        session.route_chain = route_chain.assemble(ledger_rows, load_record=lambda f, h, rid: self.records.get(rid),
                                                   load_outcome=lambda *a: None, jobs=jobs, node_evidence=self.evidence)
        sessions, jobs = attach_projections([session], jobs, node_evidence=self.evidence, now=NOW, spec_markers={},
                                            capability_groundings={}, spec_marker_dir=str(self.tmp / "markers"),
                                            route_records=dict(self.records))
        sessions[0]._prompt_display = {"human": None, "human_state": "none", "received": []}
        return {"sessions": sessions, "jobs": jobs, "resources": []}


def routes_of(snapshot):
    return list(live.build_view_model(snapshot, now=NOW).routes)


def without(route, *keys):
    return {k: v for k, v in route.items() if k not in keys}


class CalledUnitStandsBelowItsCaller(unittest.TestCase):
    def staged_world(self, shared_slug=False, with_unit=True, status="open"):
        world = World(self)
        rec, path = world.staged()
        world.owner(rec, path)
        world.stage(rec, path, slug="rate-limit" if shared_slug else "rate-limit-execute")
        if with_unit:
            world.called(CALLED, WORKER, "qa/code-review", "code", "review of execute", "unit-review-execute", status=status)
        return world, rec

    def test_the_callers_route_reads_as_it_did_before_the_call(self):
        # (a) a stage worker called, (b) the same with the owner and the stage worker sharing one slug, (c) the unit running, (d) just ended
        for shared in (False, True):
            before, rec = self.staged_world(shared_slug=shared, with_unit=False)
            want = routes_of(before.snapshot(ledger=[rec["route_id"]]))
            self.assertEqual(1, len(want))
            for status in ("open", "done"):
                world, rec = self.staged_world(shared_slug=shared, status=status)
                got = routes_of(world.snapshot(ledger=[rec["route_id"]]))
                self.assertEqual([r["id"] for r in want], [r["id"] for r in got], (shared, status))
                self.assertEqual(without(want[0], "age"), without(got[0], "called", "age"), (shared, status))
                self.assertTrue(got[0]["stages"], (shared, status))

    def test_the_owner_doing_the_work_alone_and_a_one_unit_routes_worker_keep_their_route(self):
        for kind in ("solo", "unit-route"):
            def build(with_unit):
                world = World(self)
                if kind == "solo":
                    rec, path = world.solo()
                    world.solo_owner(rec, path)
                else:
                    rec, path = world.unit("qa/code-review", "code", OWNER, "review of the branch", "unit-review-branch")
                    world.row("open", "unit-review-branch", {"attempt_id": OWNER, "dispatch_depth": 1, "worker_type": "review",
                              "capability": "unit", "unit": "qa/code-review", "route_id": rec["route_id"], "route_hash": rec["route_hash"],
                              "route_file": path, "route_node": "unit", "harness": "claude", "model": "claude-opus-5-5",
                              "effort": "xhigh", "workflow_completion": "runtime-v1", "intensity": "quick", "parent_sid": SID}, 8)
                if with_unit:
                    world.called(CALLED, OWNER, "research/fact-verify", "research", "fact check of notes", "unit-fact-check")
                return world, rec
            before, rec = build(False)
            want = routes_of(before.snapshot(ledger=[rec["route_id"]]))
            world, rec = build(True)
            got = routes_of(world.snapshot(ledger=[rec["route_id"]]))
            self.assertEqual([r["id"] for r in want], [r["id"] for r in got], kind)
            self.assertEqual(without(want[0], "age"), without(got[0], "called", "age"), kind)
            self.assertEqual(["fact-verify"], [c["unit"] for c in got[0]["called"]], kind)

    def test_the_called_unit_is_listed_on_its_callers_route_and_is_no_route_of_its_own(self):
        world, rec = self.staged_world()
        snapshot = world.snapshot(ledger=[rec["route_id"]])
        routes = routes_of(snapshot)
        self.assertEqual([rec["route_id"]], [r["id"] for r in routes])
        called = routes[0]["called"]
        self.assertEqual(1, len(called))
        self.assertEqual(("code-review", WORKER, CALLED, "review of execute", "working"),
                         (called[0]["unit"], called[0]["caller"], called[0]["attempt"], called[0]["title"], called[0]["marker"]))
        # the session's route count is the work it opened
        labels = live.session_route_status(snapshot, NOW)
        self.assertEqual({"claude\0" + SID: "route 1 · execute"}, labels)
        counts, _opened = live.current_route_counts(routes)
        self.assertEqual({"4f": 1}, counts)

    def test_the_record_free_form_stands_the_same_way(self):
        # `--ephemeral`: the route's files live in the scratch root; the registry row, which names the caller, is the same.
        plain, rec = self.staged_world()
        scratch, rec2 = self.staged_world(with_unit=False)
        scratch.called(CALLED, WORKER, "qa/code-review", "code", "review of execute", "unit-review-execute", ephemeral=True)
        want = routes_of(plain.snapshot(ledger=[rec["route_id"]]))
        got = routes_of(scratch.snapshot(ledger=[rec2["route_id"]]))
        self.assertEqual(1, len(got))
        self.assertEqual(without(want[0], "age", "called"), without(got[0], "age", "called"))
        self.assertEqual([without(c, "id") for c in want[0]["called"]], [without(c, "id") for c in got[0]["called"]])

    def test_a_called_unit_that_has_ended_is_not_listed_and_is_no_route_either(self):
        world, rec = self.staged_world(status="done")
        routes = routes_of(world.snapshot(ledger=[rec["route_id"]]))
        self.assertEqual([rec["route_id"]], [r["id"] for r in routes])
        self.assertNotIn("called", routes[0])

    def test_a_unit_called_by_a_called_unit_follows_the_chain_to_the_route_it_stands_below(self):
        world, rec = self.staged_world()
        world.called(CALLED2, CALLED, "research/fact-verify", "research", "fact check of notes", "unit-fact-check", model="claude-sonnet-5-5", effort="high")
        routes = routes_of(world.snapshot(ledger=[rec["route_id"]]))
        self.assertEqual([rec["route_id"]], [r["id"] for r in routes])
        self.assertEqual([("code-review", WORKER), ("fact-verify", CALLED)], [(c["unit"], c["caller"]) for c in routes[0]["called"]])
        self.assertEqual([("code-review", 0), ("fact-verify", 1)], [(i["unit"], d) for i, d in tables.called_lines(routes[0])])

    def test_a_chain_that_breaks_or_turns_back_leaves_the_leg_a_route_of_its_own(self):
        world = World(self)
        world.called(CALLED, att("x"), "qa/code-review", "code", "review of execute", "unit-review-execute")      # no such row
        self.assertEqual(1, len(routes_of(world.snapshot())))
        loop = World(self)
        loop.called(CALLED, CALLED2, "qa/code-review", "code", "review of execute", "unit-review-execute")
        loop.called(CALLED2, CALLED, "research/fact-verify", "research", "fact check of notes", "unit-fact-check")
        routes = routes_of(loop.snapshot())
        self.assertEqual(2, len(routes))
        self.assertTrue(all("called" not in r for r in routes))

    def test_a_leg_the_session_itself_started_stays_a_route(self):
        world = World(self)
        world.called(CALLED, None, "qa/code-review", "code", "review of the branch", "unit-review-branch")
        routes = routes_of(world.snapshot())
        self.assertEqual(1, len(routes))
        self.assertNotIn("called", routes[0])

    def test_the_job_list_still_hangs_the_called_row_below_its_caller(self):
        world, _rec = self.staged_world()
        jobs = {job.attempt_id: job for job in world.snapshot()["jobs"]}
        self.assertEqual("rate-limit-execute", jobs[CALLED].parent_slug)
        self.assertEqual(WORKER, jobs[CALLED].caller_attempt_id)
        self.assertGreaterEqual(jobs[CALLED].depth, 2)
        self.assertIsNone(jobs[WORKER].caller_attempt_id)


class PanelAndTables(unittest.TestCase):
    def panel_rows(self, snapshot):
        labels, panels = session_line.session_route_status(snapshot, NOW, include_details=True)
        return labels, panels["claude\0" + SID]

    def test_the_panel_row_carries_the_called_unit_under_the_worker_that_called(self):
        world = World(self)
        rec, path = world.staged()
        world.owner(rec, path)
        world.stage(rec, path)
        world.called(CALLED, WORKER, "qa/code-review", "code", "review of execute", "unit-review-execute")
        world.called(CALLED2, CALLED, "research/fact-verify", "research", "fact check of notes", "unit-fact-check", model="claude-sonnet-5-5", effort="high")
        labels, rows = self.panel_rows(world.snapshot(ledger=[rec["route_id"]]))
        self.assertEqual("route 1 ⠏1", labels["claude\0" + SID])
        self.assertEqual(1, len(rows))
        worker = rows[0]["workers_live"][0]
        self.assertEqual(["code-review"], [c["unit"] for c in worker["called"]])
        self.assertEqual(["fact-verify"], [c["unit"] for c in worker["called"][0]["called"]])
        self.assertNotIn("called", rows[0])            # nothing under the lead: the worker called it
        shown = session_panel.presentation({"harness": "claude", "model": "Opus 5.5", "effort": "xhigh", "ctx_pct": 31, "tag": "[4f]",
                                            "title": "main session", "route_label": labels["claude\0" + SID], "cwd": CWD, "branch": "main"},
                                           {"route_panels": {"claude\0" + SID: rows}}, "claude", SID, 44, now=NOW)
        called = shown["routes"][0]["workers_live"][0]["called"]
        self.assertEqual("code-review", called[0]["unit_word"])
        self.assertEqual("Opus 5.5", called[0]["model_text"])
        self.assertEqual("review of execute", called[0]["title"])
        self.assertEqual("code", called[0]["kind_word"])
        self.assertEqual("fact-verify", called[0]["called"][0]["unit_word"])
        self.assertTrue(called[0]["elapsed"].endswith("m"))

    def test_a_row_without_a_called_unit_has_no_called_key_anywhere(self):
        world = World(self)
        rec, path = world.staged()
        world.owner(rec, path)
        world.stage(rec, path)
        _labels, rows = self.panel_rows(world.snapshot(ledger=[rec["route_id"]]))
        self.assertNotIn("called", rows[0])
        self.assertNotIn("called", rows[0]["workers_live"][0])

    def test_the_owner_calls_stand_under_the_lead(self):
        world = World(self)
        rec, path = world.solo()
        world.solo_owner(rec, path)
        world.called(CALLED, OWNER, "research/fact-verify", "research", "fact check of notes", "unit-fact-check")
        _labels, rows = self.panel_rows(world.snapshot(ledger=[rec["route_id"]]))
        self.assertEqual(["fact-verify"], [c["unit"] for c in rows[0]["called"]])

    def drawn(self, vm, view, width=150, rows=40, **state):
        from fleet.explorer.frame import draw
        grid = draw(vm, NavState(view=view, **state), width, rows)
        return [line for line in ("".join(cell[0] for cell in row).rstrip() for row in grid.cells) if line.strip()]

    def test_overview_and_routes_draw_one_indented_row_under_the_route_and_count_one_route(self):
        world = World(self)
        rec, path = world.staged()
        world.owner(rec, path)
        world.stage(rec, path)
        world.called(CALLED, WORKER, "qa/code-review", "code", "review of execute", "unit-review-execute")
        vm = live.build_view_model(world.snapshot(ledger=[rec["route_id"]]), now=NOW)
        for view in ("OVERVIEW", "ROUTES"):
            lines = self.drawn(vm, view, folds=(("sopen:OVERVIEW", "4f"),) if view == "OVERVIEW" else ())
            text = "\n".join(lines)
            self.assertIn("review of", text, view)
            at = next(i for i, line in enumerate(lines) if "staged 3/6: execute" in line)
            self.assertIn("review of", lines[at + 1], view)
            self.assertIn("code-review", lines[at + 1], view)
            self.assertNotIn("[4f]", lines[at + 1], view)
            self.assertNotIn("route 2", text, view)
            self.assertNotIn("2 ROUTES", text, view)
        # the unit runs a session of its own: the worker counts by runtime hold it (the owner's route holds the owner)
        self.assertIn("owner·worker claude 2", "\n".join(self.drawn(vm, "OVERVIEW")))
        self.assertIn("route 1", "\n".join(self.drawn(vm, "OVERVIEW")))

    def test_the_popup_table_draws_the_unit_under_the_worker_rows_of_the_opened_route(self):
        world = World(self)
        rec, path = world.staged()
        world.owner(rec, path)
        world.stage(rec, path)
        world.called(CALLED, WORKER, "qa/code-review", "code", "review of execute", "unit-review-execute")
        vm = live.build_view_model(world.snapshot(ledger=[rec["route_id"]]), now=NOW)
        routes = tuple(r for r in vm.routes if r.get("session_tag") == "4f")
        grid = CellGrid(72, 30)
        pages._draw_route_subset(grid, vm, routes, 0, 0, grid.rows - 4, opened=routes[0]["id"], no_repo_group=True)
        lines = [line for line in ("".join(cell[0] for cell in row).rstrip() for row in grid.cells) if line.strip()]
        at = next(i for i, line in enumerate(lines) if "worker" in line and "running" in line)
        self.assertIn("code-review", lines[at + 1])
        self.assertIn("running", lines[at + 1])
        self.assertTrue(lines[at + 1].index("code-review") > lines[at].index("worker"))
        self.assertTrue(lines[at + 1].rstrip().endswith("m"))
        folded = CellGrid(72, 30)
        pages._draw_route_subset(folded, vm, routes, 0, 0, folded.rows - 4, no_repo_group=True)
        text = "\n".join("".join(cell[0] for cell in row).rstrip() for row in folded.cells)
        self.assertIn("revie", text)


if __name__ == "__main__":
    unittest.main()
