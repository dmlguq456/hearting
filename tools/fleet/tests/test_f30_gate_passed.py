#!/usr/bin/env python3
"""Hermetic unit tests — completion-gate PASS evidence (prd.md:308, v10 minor #2).

The v10 cycle left `gate_passed` as an honest gap: no completion marker existed anywhere on
disk (`plans/2026-07-15_fleet-v10-process-view/_internal/carryover.md` §1). stage-dispatch v13
(SD-56) landed the first real ones, so `fixtures/completion/rt-4883b1e245310b16/` is a VERBATIM
copy of that route's four markers, paired with `fixtures/route/real_sd13_staged.json` — the
actual record they were written against. Every mismatch/garbage case is derived from those
reals inside a tempdir, so the pass path is proven against production bytes and the no-claim
paths are proven against minimal, explainable mutations of them.

Contract under test (prd.md:308): marker present AND route_id/route_hash both match the record
= passed (True). EVERYTHING else — absent, route_id mismatch, hash mismatch, garbage json,
unreadable dir — is `None` ("무주장"), never False and never a failure glyph.

Stdlib unittest only; tempfile.TemporaryDirectory + mock (test_f28_route.py precedent). No
writes to the live `.dispatch/completion/` tree.
"""
import json
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import route                     # noqa: E402
from fleet import render                    # noqa: E402

_FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_ROUTE_FIX = os.path.join(_FIXDIR, "route", "real_sd13_staged.json")
_MARKER_FIX = os.path.join(_FIXDIR, "completion", "rt-4883b1e245310b16")

_NODES = ("plan", "execute", "test", "report")


class GateMarkBase(unittest.TestCase):
    """Builds a throwaway agent-home whose `.dispatch/completion/<route_id>/` holds copies of
    the real markers. `self.home` is what `gate_mark(home=...)` / `AGENT_HOME` point at."""

    def setUp(self):
        route.clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.record = {
            "schema_version": 2,
            "dispatch_contract_version": 3,
            "registry_digest": "sha256:" + ("a" * 64),
            "owner_dispatch_depth": 1,
            "max_dispatch_depth": 2,
            "nodes": [
                {
                    "id": node,
                    "kind": "pipeline-stage",
                    "dispatch_depth": 2,
                    "completion_gate": "code-" + node,
                }
                for node in _NODES
            ],
        }
        self.record["route_hash"] = route.route_hash(self.record)
        self.record["route_id"] = "rt-" + self.record["route_hash"].split(":", 1)[1][:16]
        self.route_id = self.record["route_id"]
        self.cdir = os.path.join(self.home, ".dispatch", "completion", self.route_id)
        os.makedirs(self.cdir)
        for node in _NODES:
            evidence_path = os.path.join(self.home, node + ".md")
            with open(evidence_path, "w", encoding="utf-8") as handle:
                handle.write(node + " evidence\n")
            evidence_sha = hashlib.sha256(
                (node + " evidence\n").encode()
            ).hexdigest()
            attempt_id = "att-fleet-" + node
            marker = {
                "schema_version": 2,
                "route_id": self.route_id,
                "route_hash": self.record["route_hash"],
                "registry_digest": self.record["registry_digest"],
                "node_id": node,
                "attempt_id": attempt_id,
                "dispatch_depth": 2,
                "transport": "headless",
                "execution_surface": "registered-headless",
                "registered_worker": True,
                "fallback_hop": "same-harness-headless",
                "completion_gate": "code-" + node,
                "evidence": {"path": evidence_path, "sha256": evidence_sha},
                "sequence": 1,
                "completed_at": "2026-07-21T00:00:00Z",
            }
            self._write(node + ".1.json", marker)
            self._write(node + ".json", marker)
            self._write(node + "." + attempt_id + ".attempt.json", {
                "schema_version": 2,
                "route_id": self.route_id,
                "node_id": node,
                "attempt_id": attempt_id,
                "dispatch_depth": 2,
                "transport": "headless",
                "execution_surface": "registered-headless",
                "registered_worker": True,
                "fallback_hop": "same-harness-headless",
                "evidence_sha256": evidence_sha,
                "completion_marker": os.path.join(self.cdir, node + ".json"),
                "completion_marker_history": os.path.join(self.cdir, node + ".1.json"),
            })

    def tearDown(self):
        self._tmp.cleanup()
        route.clear_cache()

    def _write(self, name, payload):
        path = os.path.join(self.cdir, name)
        with open(path, "w", encoding="utf-8") as f:
            if isinstance(payload, str):
                f.write(payload)
            else:
                json.dump(payload, f)
        route.clear_cache()   # the cache is mtime+size keyed; same-second rewrites can collide
        return path

    def _marker(self, node):
        with open(os.path.join(self.cdir, node + ".json"), encoding="utf-8") as f:
            return json.load(f)


class GateMarkTest(GateMarkBase):
    def test_real_markers_all_pass(self):
        """The whole point: the repo's first four production markers read as PASSED against the
        record they were actually written from."""
        for node in _NODES:
            with self.subTest(node=node):
                self.assertIs(route.gate_mark(self.record, node, home=self.home), True)

    def test_exact_inline_fallback_marker_passes_without_registered_worker(self):
        marker = self._marker("plan")
        marker.update({
            "transport": "headless",
            "execution_surface": "inline",
            "registered_worker": False,
            "fallback_hop": "inline",
        })
        self._write("plan.json", marker)
        self._write("plan.1.json", marker)
        link_name = "plan." + marker["attempt_id"] + ".attempt.json"
        with open(os.path.join(self.cdir, link_name), encoding="utf-8") as handle:
            link = json.load(handle)
        link.update({
            "transport": "headless",
            "execution_surface": "inline",
            "registered_worker": False,
            "fallback_hop": "inline",
        })
        self._write(link_name, link)
        self.assertIs(route.gate_mark(self.record, "plan", home=self.home), True)

    def test_absent_marker_is_no_claim(self):
        os.remove(os.path.join(self.cdir, "test.json"))
        route.clear_cache()
        self.assertIsNone(route.gate_mark(self.record, "test", home=self.home))
        self.assertIs(route.gate_mark(self.record, "plan", home=self.home), True)

    def test_absent_route_dir_is_no_claim(self):
        shutil.rmtree(self.cdir)
        route.clear_cache()
        for node in _NODES:
            self.assertIsNone(route.gate_mark(self.record, node, home=self.home))

    def test_route_id_mismatch_is_no_claim_not_failure(self):
        m = self._marker("plan")
        m["route_id"] = "rt-0000000000000000"
        self._write("plan.json", m)
        # `None`, not False — a marker we cannot tie to this record is silence, not a verdict.
        self.assertIsNone(route.gate_mark(self.record, "plan", home=self.home))

    def test_route_hash_mismatch_is_no_claim(self):
        m = self._marker("plan")
        m["route_hash"] = "sha256:" + ("0" * 64)
        self._write("plan.json", m)
        self.assertIsNone(route.gate_mark(self.record, "plan", home=self.home))

    def test_garbage_json_never_raises(self):
        for junk in ("{not json at all", "", "[]", "null", '"a string"', "\x00\xff"):
            with self.subTest(junk=junk):
                self._write("plan.json", junk)
                self.assertIsNone(route.gate_mark(self.record, "plan", home=self.home))

    def test_marker_missing_required_fields_is_no_claim(self):
        for drop in ("route_id", "route_hash"):
            with self.subTest(drop=drop):
                m = self._marker("plan")
                del m[drop]
                self._write("plan.json", m)
                self.assertIsNone(route.gate_mark(self.record, "plan", home=self.home))

    def test_legacy_marker_without_schema_or_sequence_is_no_claim(self):
        m = self._marker("plan")
        del m["sequence"]
        del m["schema_version"]
        self._write("plan.json", m)
        self.assertIsNone(route.gate_mark(self.record, "plan", home=self.home))

    def test_canonical_outranks_stale_history(self):
        """History files exist alongside canonical; canonical is what the writer atomically
        replaces with the newest, so a stale mismatching history file must not demote it."""
        stale = self._marker("plan")
        stale["route_hash"] = "sha256:" + ("0" * 64)
        self._write("plan.99.json", stale)
        self.assertIs(route.gate_mark(self.record, "plan", home=self.home), True)

    def test_history_without_canonical_is_no_claim(self):
        os.remove(os.path.join(self.cdir, "plan.json"))
        self.assertIsNone(route.gate_mark(self.record, "plan", home=self.home))

    def test_mismatched_immutable_history_is_no_claim(self):
        stale = self._marker("plan")
        stale["attempt_id"] = "att-conflict"
        self._write("plan.1.json", stale)
        self.assertIsNone(route.gate_mark(self.record, "plan", home=self.home))

    def test_consistently_linked_but_invalid_attempt_axes_are_no_claim(self):
        marker = self._marker("plan")
        marker["transport"] = "interactive"
        self._write("plan.json", marker)
        self._write("plan.1.json", marker)
        link_name = "plan." + marker["attempt_id"] + ".attempt.json"
        with open(os.path.join(self.cdir, link_name), encoding="utf-8") as handle:
            link = json.load(handle)
        link["transport"] = "interactive"
        self._write(link_name, link)
        self.assertIsNone(route.gate_mark(self.record, "plan", home=self.home))

    def test_bad_inputs_never_raise(self):
        for bad_record in (None, "x", 123, {}, {"route_id": 1, "route_hash": 2}):
            with self.subTest(record=bad_record):
                self.assertIsNone(route.gate_mark(bad_record, "plan", home=self.home))
        for bad_node in (None, "", 123, [], {}):
            with self.subTest(node=bad_node):
                self.assertIsNone(route.gate_mark(self.record, bad_node, home=self.home))

    def test_node_id_never_traverses(self):
        self.assertIsNone(route.gate_mark(self.record, "../../plan", home=self.home))

    def test_cache_is_mtime_keyed(self):
        self.assertIs(route.gate_mark(self.record, "plan", home=self.home), True)
        self.assertIs(route.gate_mark(self.record, "plan", home=self.home), True)

    def test_home_defaults_to_agent_home_env(self):
        with mock.patch.dict(os.environ, {"AGENT_HOME": self.home}):
            self.assertIs(route.gate_mark(self.record, "plan"), True)

    def test_read_only_no_writes_to_completion_tree(self):
        before = {n: os.stat(os.path.join(self.cdir, n)).st_mtime_ns
                  for n in os.listdir(self.cdir)}
        route.resolve_gate_marks({self.route_id: self.record}, home=self.home)
        after = {n: os.stat(os.path.join(self.cdir, n)).st_mtime_ns
                 for n in os.listdir(self.cdir)}
        self.assertEqual(before, after)


class ResolveGateMarksTest(GateMarkBase):
    def test_inline_marker_survives_real_resolution_and_parallel_collapse(self):
        # S-2 additional surface: exercise the production carrier end to end.
        # One member is rewritten to the exact inline attempt axes, then the
        # marker resolver, route view, and parallel collapse must all preserve
        # the group's completed gate.
        for node in self.record["nodes"][:3]:
            node["parallel_group"] = "frame"
        self.record["route_hash"] = route.route_hash(self.record)
        self.record["route_id"] = (
            "rt-" + self.record["route_hash"].split(":", 1)[1][:16]
        )
        old_cdir = self.cdir
        self.route_id = self.record["route_id"]
        self.cdir = os.path.join(
            self.home, ".dispatch", "completion", self.route_id
        )
        os.rename(old_cdir, self.cdir)

        for node in _NODES:
            attempt_id = "att-fleet-" + node
            for name in (node + ".1.json", node + ".json"):
                path = os.path.join(self.cdir, name)
                with open(path, encoding="utf-8") as handle:
                    marker = json.load(handle)
                marker.update({
                    "route_id": self.route_id,
                    "route_hash": self.record["route_hash"],
                })
                if node == "execute":
                    marker.update({
                        "execution_surface": "inline",
                        "registered_worker": False,
                        "fallback_hop": "inline",
                    })
                self._write(name, marker)

            link_name = node + "." + attempt_id + ".attempt.json"
            with open(os.path.join(self.cdir, link_name), encoding="utf-8") as handle:
                link = json.load(handle)
            link.update({
                "route_id": self.route_id,
                "completion_marker": os.path.join(self.cdir, node + ".json"),
                "completion_marker_history": os.path.join(
                    self.cdir, node + ".1.json"
                ),
            })
            if node == "execute":
                link.update({
                    "execution_surface": "inline",
                    "registered_worker": False,
                    "fallback_hop": "inline",
                })
            self._write(link_name, link)

        marks = route.resolve_gate_marks(
            {self.route_id: self.record}, home=self.home
        )
        view = route.build_views(
            [], {}, {self.route_id: self.record}, 1_000_000.0, marks
        )[0]
        collapsed = render._collapse_parallel_nodes(view["nodes"])
        group = next(node for node in collapsed if node["id"] == "frame(3-way)")
        self.assertEqual(set(marks[self.route_id]), set(_NODES))
        self.assertEqual(group["state"], "done")
        self.assertIs(group["gate_passed"], True)

    def test_row_registry_state_root_outranks_observer_home(self):
        observer = os.path.join(self._tmp.name, "observer")
        os.makedirs(observer)
        state_root = os.path.dirname(os.path.dirname(self.cdir))
        launch_home = os.path.join(self._tmp.name, "release")
        os.makedirs(launch_home)
        job = mock.Mock(route_id=self.route_id)
        job._launch_home = launch_home
        job._registry_path = os.path.join(state_root, "jobs.log")
        marks = route.resolve_gate_marks(
            {self.route_id: self.record}, home=observer, jobs=[job]
        )
        self.assertEqual(set(marks[self.route_id]), set(_NODES))

    def test_resolve_returns_only_passed_nodes(self):
        os.remove(os.path.join(self.cdir, "report.json"))
        route.clear_cache()
        marks = route.resolve_gate_marks({self.route_id: self.record}, home=self.home)
        self.assertEqual(marks, {self.route_id: {"plan": True, "execute": True, "test": True}})

    def test_resolve_omits_route_with_no_marks(self):
        shutil.rmtree(self.cdir)
        route.clear_cache()
        self.assertEqual(route.resolve_gate_marks({self.route_id: self.record}, home=self.home), {})

    def test_resolve_tolerates_empty_and_none(self):
        self.assertEqual(route.resolve_gate_marks(None), {})
        self.assertEqual(route.resolve_gate_marks({}), {})


class BuildViewsGatePassedTest(GateMarkBase):
    """`build_views` must stay PURE — it consumes resolved marks, it never reads a marker."""

    def _view(self, gate_marks=None):
        views = route.build_views([], {}, {self.route_id: self.record}, 1_000_000.0, gate_marks)
        self.assertEqual(len(views), 1)
        return views[0]

    def test_default_is_no_claim_everywhere(self):
        v = self._view()
        self.assertEqual([n["gate_passed"] for n in v["nodes"]], [None] * 4)

    def test_marks_land_on_the_right_nodes(self):
        v = self._view({self.route_id: {"plan": True, "test": True}})
        got = {n["id"]: n["gate_passed"] for n in v["nodes"]}
        self.assertEqual(got, {"plan": True, "execute": None, "test": True, "report": None})

    def test_marker_makes_node_done_but_done_without_marker_stays_no_claim(self):
        """F-41b lets an exact marker complete a marker-only inline node.  The reverse
        remains independent: terminal registry evidence does not invent a gate marker."""
        v = self._view({self.route_id: {"plan": True}})
        plan = next(n for n in v["nodes"] if n["id"] == "plan")
        self.assertEqual(plan["state"], "done")
        self.assertIs(plan["gate_passed"], True)

        done = route.build_views(
            [], {self.route_id: {"execute": {"status": "done"}}},
            {self.route_id: self.record}, 1_000_000.0,
        )[0]
        execute = next(n for n in done["nodes"] if n["id"] == "execute")
        self.assertEqual(execute["state"], "done")
        self.assertIsNone(execute["gate_passed"])

    def test_build_views_does_no_io(self):
        with mock.patch("os.stat", side_effect=AssertionError("build_views touched the fs")), \
             mock.patch("builtins.open", side_effect=AssertionError("build_views touched the fs")):
            self._view({self.route_id: {"plan": True}})

    def test_collect_views_resolves_marks(self):
        with mock.patch.dict(os.environ, {"AGENT_HOME": self.home}), \
             mock.patch.object(route, "resolve_records",
                               return_value={self.route_id: self.record}):
            views = route.collect_views([], {}, now=1_000_000.0)
        self.assertEqual([n["gate_passed"] for n in views[0]["nodes"]], [True] * 4)

    def test_heuristic_view_has_no_nodes_to_mark(self):
        # v16: renamed "heuristic" -> "unknown" for an unresolved explicit route (Step 1.2.3).
        views = route.build_views([], {"rt-deadbeefdeadbeef": {}}, {}, 1_000_000.0)
        self.assertEqual(views[0]["source"], "unknown")
        self.assertEqual(views[0]["nodes"], [])


class SummaryJsonTest(GateMarkBase):
    def test_gate_passed_is_additive_and_no_existing_key_moved(self):
        marks = {self.route_id: {"plan": True}}
        views = route.build_views([], {}, {self.route_id: self.record}, 1_000_000.0, marks)
        node = route.summary(views)[0]["nodes"][0]
        self.assertIn("gate_passed", node)
        self.assertIs(node["gate_passed"], True)
        # The v10 key set plus the additive gate, v14 portable-unit metadata, and v16's
        # additive `write_scope` (plan Step 1.2.2 — carried in node views/public route JSON).
        self.assertEqual(sorted(node), sorted([
            "id", "depends_on", "level", "state", "gate", "note",
            "elapsed_min", "model", "harness", "effort", "gate_passed",
            "unit", "unit_choices", "write_scope"]))

    def test_unmarked_node_serializes_as_null_not_false(self):
        views = route.build_views([], {}, {self.route_id: self.record}, 1_000_000.0)
        node = route.summary(views)[0]["nodes"][0]
        self.assertIsNone(node["gate_passed"])
        self.assertIn('"gate_passed": null', json.dumps(node, indent=1))


class GateMarkRenderTest(unittest.TestCase):
    """The mark is a SEPARATE segment in `gate_t` — never folded into the state glyph's text
    (prd.md:308: an independent dimension), and never drawn for a no-claim node."""

    def _node(self, **kw):
        base = {"id": "plan", "state": "done", "level": 0, "elapsed_min": 12,
                "gate": "code-plan", "gate_passed": None, "model": None, "effort": None,
                "job": None, "depends_on": []}
        base.update(kw)
        return base

    def test_passed_node_emits_mark_segment(self):
        lines = render._route_card_l2({"nodes": [self._node(gate_passed=True)]})
        segs = lines[0]
        self.assertEqual(segs, [("plan ✓12m", "dim"), (render._GATE_MARK, "gate_t")])

    def test_no_claim_node_emits_no_mark(self):
        lines = render._route_card_l2({"nodes": [self._node()]})
        self.assertEqual(lines[0], [("plan ✓12m", "dim")])
        self.assertNotIn(render._GATE_MARK, "".join(t for t, _k in lines[0]))

    def test_mark_never_shares_the_state_glyph_colour(self):
        """The regression this guards: a dim `⊸` on a dim `done` node melts into one phrase
        (the merge render.py:101 warns about), which is why the mark owns `gate_t`."""
        for state in ("done", "pending", "reconciling", "failed"):
            with self.subTest(state=state):
                lines = render._route_card_l2({"nodes": [self._node(state=state,
                                                                    gate_passed=True)]})
                marks = [k for t, k in lines[0] if t == render._GATE_MARK]
                self.assertEqual(marks, ["gate_t"])

    def test_mark_survives_every_state(self):
        for state in ("done", "active", "reconciling", "failed", "pending"):
            with self.subTest(state=state):
                lines = render._route_card_l2({"nodes": [self._node(state=state,
                                                                    gate_passed=True)]})
                self.assertIn(render._GATE_MARK, [t for t, _k in lines[0]])

    def test_fan_out_branch_rows_carry_the_mark(self):
        nodes = [self._node(id="a", level=0, gate_passed=True),
                 self._node(id="b", level=0, gate_passed=None)]
        lines = render._route_card_l2({"nodes": nodes})
        self.assertEqual(len(lines), 2)
        self.assertIn(render._GATE_MARK, [t for t, _k in lines[0]])
        self.assertNotIn(render._GATE_MARK, [t for t, _k in lines[1]])

    def test_width_wraps_without_dropping_nodes_or_gate_marks(self):
        """Narrow cards wrap the sealed route; they never fold completed nodes away."""
        nodes = [self._node(id="plan", level=0, gate_passed=True),
                 self._node(id="execute", level=1, state="active", elapsed_min=8,
                            gate_passed=True, depends_on=["plan"]),
                 self._node(id="test", level=2, state="pending", elapsed_min=None,
                            depends_on=["execute"])]

        def drawn(ns, width):
            lines = render._route_card_l2({"nodes": ns}, max_width=width)
            self.assertTrue(lines)
            self.assertTrue(all(sum(render._dw(t) for t, _k in line) <= width
                                for line in lines))
            return lines, "\n".join("".join(t for t, _k in line) for line in lines)

        for width in (30, 36, 37, 60, 200):
            with self.subTest(width=width):
                lines, text = drawn(nodes, width)
                for primary in ("plan ✓", "execute ●", "test ○"):
                    self.assertEqual(text.count(primary), 1)
                self.assertEqual([key for line in lines for value, key in line
                                  if value == render._GATE_MARK], ["gate_t", "gate_t"])
        self.assertGreater(len(drawn(nodes, 30)[0]), 1)
        self.assertEqual(len(drawn(nodes, 200)[0]), 1)


class GateDetailRowTest(GateMarkBase):
    """The `a`-toggle `gates:` row — names always, `⊸` only where proven."""

    def _card(self, gate_marks):
        views = route.build_views([], {}, {self.route_id: self.record}, 1_000_000.0, gate_marks)
        out, _meta = render._route_card(views[0], {}, 120, 1_000_000.0)
        rows = [segs for segs in out
                if segs and isinstance(segs[0][0], str) and "gates: " in segs[0][0]]
        return rows

    def test_gates_row_hidden_without_show_all(self):
        render.set_show_all(False)
        try:
            self.assertEqual(self._card({self.route_id: {"plan": True}}), [])
        finally:
            render.set_show_all(False)

    def test_gates_row_marks_only_passed_gates(self):
        render.set_show_all(True)
        try:
            rows = self._card({self.route_id: {"plan": True, "report": True}})
            self.assertEqual(len(rows), 1)
            segs = rows[0]
            text = "".join(t for t, _k in segs)
            self.assertEqual(text, "      gates: code-plan ⊸, code-execute, code-test, "
                                   "code-report ⊸")
            self.assertEqual([k for t, k in segs if t == render._GATE_MARK],
                             ["gate_t", "gate_t"])
        finally:
            render.set_show_all(False)

    def test_gates_row_unmarked_shows_bare_names(self):
        render.set_show_all(True)
        try:
            segs = self._card({})[0]
            self.assertEqual("".join(t for t, _k in segs),
                             "      gates: code-plan, code-execute, code-test, code-report")
        finally:
            render.set_show_all(False)


class NodeStateMarkerSupersedesTest(unittest.TestCase):
    """2026-07-24 (user "execute도 X로 뜨는데 왜?"): a completion marker supersedes a
    SUPERSEDED dead attempt (failed -> done). F-41b also makes a valid marker authoritative
    for an inline fallback leg that intentionally has no jobs.log row."""

    class _Row:
        def __init__(self, node, liveness, state_evidence=None):
            self.route_node, self.liveness = node, liveness
            self.elapsed_min, self.model, self.harness = 5, "m", "claude"
            self.effort, self.pid = "high", 111
            self.registry_priority = self.registry_order = None
            self.attempt_id = self.route_id = self.note = None
            self.state_evidence = state_evidence

    @staticmethod
    def _reconcile_evidence():
        return {"attempt": {
            "state": "stale", "source": "shared-observer",
            "rule": "terminal-observed/reconcile-needed",
            "attempt_id": None, "route_id": None, "route_node": "execute",
            "observed_liveness": {
                "state": "reconcile-needed", "reason": "terminal-observed",
                "process_state": "quiescent",
            },
        }}

    def test_dead_attempt_with_marker_is_done(self):
        st = route._node_state("execute", [self._Row("execute", "dead")], {}, 100.0,
                               completion_marked=True)
        self.assertEqual(st["state"], "done")
        self.assertIsNone(st["job"])            # complete, not a live target
        self.assertEqual(st["elapsed_min"], 5)  # dead attempt's telemetry retained

    def test_dead_attempt_without_marker_is_failed(self):
        st = route._node_state("execute", [self._Row("execute", "dead")], {}, 100.0,
                               completion_marked=False)
        self.assertEqual(st["state"], "failed")

    def test_exact_reconcile_attempt_without_marker_is_reconciling(self):
        row = self._Row("execute", "stale", self._reconcile_evidence())
        st = route._node_state("execute", [row], {}, 100.0, completion_marked=False)
        self.assertEqual(st["state"], "reconciling")
        self.assertEqual(st["note"], "reconcile-needed")
        self.assertIs(st["job"], row)

    def test_exact_reconcile_attempt_with_marker_is_done(self):
        row = self._Row("execute", "stale", self._reconcile_evidence())
        st = route._node_state("execute", [row], {}, 100.0, completion_marked=True)
        self.assertEqual(st["state"], "done")
        self.assertIsNone(st["job"])

    def test_mismatched_attempt_axis_cannot_mint_reconciling(self):
        row = self._Row("execute", "stale", self._reconcile_evidence())
        row.attempt_id = "att-current"
        row.state_evidence["attempt"]["attempt_id"] = "att-other"
        st = route._node_state("execute", [row], {}, 100.0, completion_marked=False)
        self.assertEqual(st["state"], "failed")

    def test_newer_reconcile_attempt_supersedes_older_failed_retry(self):
        old = self._Row("execute", "dead")
        old.registry_order = 1
        current = self._Row("execute", "stale", self._reconcile_evidence())
        current.registry_order = 2
        st = route._node_state("execute", [old, current], {}, 100.0,
                               completion_marked=False)
        self.assertEqual(st["state"], "reconciling")
        self.assertIs(st["job"], current)

    def test_newer_generic_failure_supersedes_older_reconcile_attempt(self):
        old = self._Row("execute", "stale", self._reconcile_evidence())
        old.registry_order = 1
        current = self._Row("execute", "dead")
        current.registry_order = 2
        st = route._node_state("execute", [old, current], {}, 100.0,
                               completion_marked=False)
        self.assertEqual(st["state"], "failed")
        self.assertIs(st["job"], current)

    def test_dead_note_evidence_with_marker_is_done(self):
        ev = {"execute": {"status": "done", "note": "dead-no-progress"}}
        st = route._node_state("execute", [], ev, 100.0, completion_marked=True)
        self.assertEqual(st["state"], "done")

    def test_marker_only_inline_node_is_done(self):
        st = route._node_state("plan", [], {}, 100.0, completion_marked=True)
        self.assertEqual(st["state"], "done")
        self.assertIsNone(st["job"])

    def test_active_wins_over_marker(self):
        st = route._node_state("execute", [self._Row("execute", "working")], {}, 100.0,
                               completion_marked=True)
        self.assertEqual(st["state"], "active")

    def test_inline_plan_group_is_steady_while_execute_is_sole_active_stage(self):
        record = {
            "route_hash": "sha256:test",
            "nodes": [
                {"id": "plan", "depends_on": [], "parallel_group": "plan",
                 "completion_gate": "code-plan"},
                {"id": "plan-inline", "depends_on": [], "parallel_group": "plan",
                 "completion_gate": "code-plan"},
                {"id": "execute", "depends_on": ["plan", "plan-inline"],
                 "completion_gate": "code-execute"},
            ],
        }
        execute = self._Row("execute", "working")
        view = route._record_view(
            record, "rt-inline-plan", [execute], {}, 100.0,
            gate_marks_for_route={"plan": True, "plan-inline": True},
        )
        collapsed = render._collapse_parallel_nodes(view["nodes"])
        self.assertEqual([(node["id"], node["state"]) for node in collapsed],
                         [("plan(2-way)", "done"), ("execute", "active")])
        self.assertEqual([node["id"] for node in collapsed if node["state"] == "active"],
                         ["execute"])

    def test_done_plus_pending_plan_group_is_not_fabricated_active(self):
        """A started downstream node must not make a mixed parallel group blink.

        F-41d explicitly places ``done`` above ``pending``. This is the live Claude owner
        shape where one plan leg completed, the unused alternative never registered, and
        execute is the only genuinely active stage.
        """
        record = {
            "route_hash": "sha256:test",
            "nodes": [
                {"id": "plan", "depends_on": [], "parallel_group": "plan",
                 "completion_gate": "code-plan"},
                {"id": "plan-alternative", "depends_on": [], "parallel_group": "plan",
                 "completion_gate": "code-plan"},
                {"id": "execute", "depends_on": ["plan", "plan-alternative"],
                 "completion_gate": "code-execute"},
            ],
        }
        execute = self._Row("execute", "working")
        view = route._record_view(
            record, "rt-partial-plan", [execute],
            {"plan": {"status": "done", "note": "completed-marker"}}, 100.0,
        )
        collapsed = render._collapse_parallel_nodes(view["nodes"])
        self.assertEqual([(node["id"], node["state"]) for node in collapsed],
                         [("plan(2-way)", "done"), ("execute", "active")])


class ReconciliationRenderTest(unittest.TestCase):
    def _node(self, state="reconciling"):
        return {"id": "frame", "state": state, "level": 0, "elapsed_min": 3,
                "gate_passed": None, "depends_on": []}

    def test_process_node_uses_yellow_gate_pending_label(self):
        text, key, mark = render._route_node_text(self._node())
        self.assertEqual((text, key, mark), ("frame …gate 3m", "lvl_y", ""))

    def test_breadcrumb_and_detail_use_yellow_ellipsis(self):
        breadcrumb = render._route_stage_segs([("frame", "reconciling"),
                                                ("plan", "pending")], True, 80)
        self.assertIn(("frame …", "lvl_y"), breadcrumb)
        for width in (168, 120, 100, 60):
            with self.subTest(width=width):
                detail = render._stage_detail_rows([self._node()], term_width=width)
                self.assertIn(("frame …", "lvl_y"), detail[0])
                self.assertTrue(all(render._dw("".join(t for t, _k in row)) <= width
                                    for row in detail))

    def test_parallel_group_precedence_preserves_failure_and_reconciliation(self):
        def legs(a, b):
            return [
                {"id": "frame", "state": a, "level": 0, "depends_on": [],
                 "parallel_group": "frame"},
                {"id": "frame-alt", "state": b, "level": 0, "depends_on": [],
                 "parallel_group": "frame"},
            ]
        self.assertEqual(render._collapse_parallel_nodes(legs("reconciling", "done"))[0]["state"],
                         "reconciling")
        self.assertEqual(render._collapse_parallel_nodes(legs("reconciling", "active"))[0]["state"],
                         "active")
        self.assertEqual(render._collapse_parallel_nodes(legs("reconciling", "failed"))[0]["state"],
                         "failed")
        self.assertEqual(render._collapse_parallel_nodes(legs("degraded", "reconciling"))[0]["state"],
                         "degraded")
        self.assertEqual(render._collapse_parallel_nodes(legs("done", "pending"))[0]["state"],
                         "done")
        self.assertEqual(render._collapse_parallel_nodes(legs("pending", "pending"))[0]["state"],
                         "pending")


class ProjectionMarkThreadingTest(GateMarkBase):
    """projection._record_view must resolve+thread gate marks (2026-07-24 wiring) so a
    dead-attempt node with a marker renders `done` on the owning session/dispatch _route_view
    — parity with the group view. Without the thread, marks were empty and the node showed ✕."""

    class _Row:
        def __init__(self, node):
            self.route_node, self.liveness = node, "dead"
            self.elapsed_min, self.model, self.harness = 3, "m", "claude"
            self.effort, self.pid = "high", 1
            self.registry_priority = self.registry_order = None

    def test_projection_record_view_supersedes_dead_attempt(self):
        from fleet import projection
        with mock.patch.dict(os.environ, {"AGENT_HOME": self.home}):
            view = projection._record_view(self.record, self.route_id,
                                           [self._Row("plan")], now=1_000_000.0)
        plan = next(n for n in view["nodes"] if n["id"] == "plan")
        self.assertEqual(plan["state"], "done")      # dead attempt + marker -> done
        self.assertIs(plan["gate_passed"], True)

    def test_projection_uses_row_registry_root_without_inherited_jobs_env(self):
        from fleet import projection
        observer = os.path.join(self._tmp.name, "installed-release")
        os.makedirs(observer)
        row = self._Row("plan")
        row.route_id = self.route_id
        row._launch_home = observer
        row._registry_path = os.path.join(self.home, ".dispatch", "jobs.log")
        with mock.patch.dict(os.environ, {"AGENT_HOME": observer}, clear=True):
            view = projection._record_view(
                self.record, self.route_id, [row], now=1_000_000.0
            )
        plan = next(n for n in view["nodes"] if n["id"] == "plan")
        self.assertEqual(plan["state"], "done")
        self.assertIs(plan["gate_passed"], True)


if __name__ == "__main__":
    unittest.main()
