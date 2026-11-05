"""Hermetic unit tests — F-<next> route chain ledger + reader (tools/fleet/route_chain.py).

Every test that touches disk sets `FLEET_ROUTE_CHAIN_DIR`/`FLEET_CAPABILITY_GROUNDING_DIR` to a
tmp dir explicitly (never unset — a real session env must never leak into a real ledger, plan §4
K-1). Stdlib unittest + mock only.
"""
import copy
import glob as glob_module
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import route, route_chain  # noqa: E402

_FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "route",
                        "continuation_round")


def _sess(harness="claude", session_id="sess-a", aliases=None, **extra):
    ns = types.SimpleNamespace(harness=harness, session_id=session_id,
                                session_aliases=aliases or [], route_chain=None)
    for key, value in extra.items():
        setattr(ns, key, value)
    return ns


class EnvTmpTestCase(unittest.TestCase):
    """Base class: isolate FLEET_ROUTE_CHAIN_DIR / FLEET_CAPABILITY_GROUNDING_DIR to tmp."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="route-chain-test-")
        self.chains_dir = os.path.join(self._tmp, "chains")
        self.grounding_dir = os.path.join(self._tmp, "grounding")
        self._patcher = mock.patch.dict(os.environ, {
            "FLEET_ROUTE_CHAIN_DIR": self.chains_dir,
            "FLEET_CAPABILITY_GROUNDING_DIR": self.grounding_dir,
        })
        self._patcher.start()
        route.clear_cache()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)
        route.clear_cache()


class StateRootTest(EnvTmpTestCase):
    def test_state_root_env_override(self):
        self.assertEqual(route_chain.state_root(), self.chains_dir)

    def test_state_root_xdg_default(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-rc-test"}):
            del os.environ["FLEET_ROUTE_CHAIN_DIR"]
            self.assertEqual(route_chain.state_root(),
                              "/tmp/xdg-rc-test/agent-fleet/route-chains")

    def test_capability_grounding_dir_env_and_default(self):
        self.assertEqual(route_chain.capability_grounding_dir(), self.grounding_dir)
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-rc-test"}):
            del os.environ["FLEET_CAPABILITY_GROUNDING_DIR"]
            self.assertEqual(route_chain.capability_grounding_dir(),
                              "/tmp/xdg-rc-test/agent-fleet/capability-grounding")


class LedgerPathTest(EnvTmpTestCase):
    def test_rejects_unknown_harness(self):
        with self.assertRaises(ValueError):
            route_chain.ledger_path("gemini", "sid-1")

    def test_rejects_unsafe_session_id(self):
        for bad in ("../escape", "a/b", "", "x" * 200, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    route_chain.ledger_path("claude", bad)

    def test_accepts_safe_session_id(self):
        path = route_chain.ledger_path("claude", "sess-a.1")
        self.assertEqual(path, os.path.join(self.chains_dir, "claude", "sess-a.1.jsonl"))

    def test_full_history_cache_uses_all_file_stamp_fields_and_invalidates_rewrite(self):
        path = route_chain.ledger_path("claude", "cache-session")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        first = route_chain.build_line({"route_id": "rt-first", "route_hash": "h"},
                                       event="compose", harness="claude",
                                       session_id="cache-session", route_file="/tmp/a")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(first) + "\n")
        self.assertEqual(len(route_chain.read_tail("claude", "cache-session", None)), 1)
        with mock.patch.object(route_chain.json, "loads", wraps=json.loads) as loads:
            self.assertEqual(len(route_chain.read_tail("claude", "cache-session", None)), 1)
            self.assertEqual(loads.call_count, 0)
            second = dict(first, route_id="rt-other")
            before = json.dumps(first)
            after = json.dumps(second)
            self.assertEqual(len(before), len(after))
            cached_stat = os.stat(path)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(after + "\n")
            os.utime(path, ns=(cached_stat.st_atime_ns,
                               cached_stat.st_mtime_ns + 1_000_000_000))
            self.assertEqual(route_chain.read_tail("claude", "cache-session", None)[0]["route_id"],
                             "rt-other")
            self.assertEqual(loads.call_count, 1)


class WriterIdentityTest(EnvTmpTestCase):
    def test_depth_zero_single_native_session_is_the_anchor(self):
        self.assertEqual(route_chain.writer_identity({"CLAUDE_CODE_SESSION_ID": "sess-a"}),
                         ("claude", "sess-a"))
        self.assertEqual(route_chain.writer_identity(
            {"CODEX_THREAD_ID": "thr-1", "AGENT_DISPATCH_DEPTH": "0"}), ("codex", "thr-1"))

    def test_workers_ambiguity_and_unsafe_ids_have_no_anchor(self):
        self.assertIsNone(route_chain.writer_identity(
            {"CLAUDE_CODE_SESSION_ID": "sess-a", "AGENT_DISPATCH_DEPTH": "1"}))
        self.assertIsNone(route_chain.writer_identity(
            {"CLAUDE_CODE_SESSION_ID": "sess-a", "OPENCODE_SESSION_ID": "oc-1"}))
        self.assertEqual(route_chain.writer_identity(
            {"CLAUDE_CODE_SESSION_ID": "sess-a", "OPENCODE_SESSION_ID": "oc-1",
             "AGENT_DISPATCH_CALLER_HARNESS": "opencode"}), ("opencode", "oc-1"))
        self.assertIsNone(route_chain.writer_identity({"CLAUDE_CODE_SESSION_ID": "../escape"}))
        self.assertIsNone(route_chain.writer_identity({}))


class ComposingAnchorTest(EnvTmpTestCase):
    ROUTE = {"route_id": "rt-" + "a" * 32, "artifact_root": "/x", "campaign_key": "k"}

    def put(self, session_id, event, route=None):
        line = route_chain.build_line(route or self.ROUTE, event=event, harness="claude",
                                      session_id=session_id, route_file="/x/r.json")
        self.assertTrue(route_chain.append("claude", session_id, line))

    def test_the_composing_ledger_names_the_anchor_for_any_asking_process(self):
        self.put("sess-a", "compose")
        for env in ({"CLAUDE_CODE_SESSION_ID": "sess-a"},
                    {"CLAUDE_CODE_SESSION_ID": "worker-d", "AGENT_DISPATCH_DEPTH": "1"}, {}):
            self.assertEqual(route_chain.composing_anchor(self.ROUTE["route_id"], env), ("claude", "sess-a"))

    def test_start_only_unknown_and_contested_routes_have_no_anchor(self):
        self.put("sess-a", "start")
        self.assertIsNone(route_chain.composing_anchor(self.ROUTE["route_id"], {}))
        self.assertIsNone(route_chain.composing_anchor("rt-" + "b" * 32, {}))
        self.assertIsNone(route_chain.composing_anchor(None, {}))
        self.put("sess-a", "compose")
        self.put("sess-b", "compose")
        self.assertIsNone(route_chain.composing_anchor(self.ROUTE["route_id"], {}))
        self.assertEqual(route_chain.composing_anchor(
            self.ROUTE["route_id"], {"CLAUDE_CODE_SESSION_ID": "sess-b"}), ("claude", "sess-b"))


    def test_an_older_composing_ledger_is_found_past_any_number_of_newer_unrelated_ones(self):
        """The route's creation time, not a count of recent ledgers, bounds the scan."""
        created = time.time() - 10_000
        self.put("composer", "compose")
        self._age("composer", created + 100)                                  # after the route, before the rest
        self.put("older-than-route", "compose")
        self._age("older-than-route", created - 500)                          # cannot hold this route's compose
        other = {"route_id": "rt-" + "c" * 32, "artifact_root": "/x", "campaign_key": "k"}
        for n in range(route_chain.ANCHOR_SCAN_FILES + 6):
            self.put(f"unrelated-{n}", "compose", route=other)
            self._age(f"unrelated-{n}", created + 1_000 + n)
        route_id = self.ROUTE["route_id"]
        self.assertEqual(route_chain.composing_anchor(route_id, {}, not_before=created), ("claude", "composer"))
        # without the route's creation time the old count bound stays, and misses it
        self.assertIsNone(route_chain.composing_anchor(route_id, {}))
        # a ledger last written before the route existed is not scanned, even when it carries the line
        self.assertIsNone(route_chain.composing_anchor(route_id, {}, not_before=created + 200))

    def test_two_newer_ledgers_claiming_the_route_still_give_no_anchor(self):
        created = time.time() - 10_000
        for sid in ("sess-a", "sess-b"):
            self.put(sid, "compose")
            self._age(sid, created + 50)
        self.assertIsNone(route_chain.composing_anchor(self.ROUTE["route_id"], {}, not_before=created))

    def _age(self, session_id, mtime):
        os.utime(route_chain.ledger_path("claude", session_id), (mtime, mtime))


class BuildLineTest(EnvTmpTestCase):
    def _route(self, **overrides):
        base = {
            "route_id": "rt-abc", "route_hash": "sha256:abc", "capability": "autopilot-code",
            "capability_mode": "dev", "effective_intensity": "standard",
            "artifact_root": "/tmp/art", "campaign_key": "k1", "campaign_unassigned": False,
            "parent_cycle_id": None, "slug": "my-slug", "source_route_id": None,
            "selection": {"route_origin": "compose", "shape": "solo"},
            "work_request": "x" * 5000,
        }
        base.update(overrides)
        return base

    def test_line_shape_and_no_work_request(self):
        line = route_chain.build_line(self._route(), event="compose", harness="claude",
                                       session_id="sess-a", route_file="/tmp/r.json")
        self.assertNotIn("work_request", line)
        self.assertEqual(line["capability"], "autopilot-code")
        self.assertEqual(line["shape"], "solo")
        self.assertEqual(line["v"], route_chain.SCHEMA)

    def test_shape_absent_when_not_compose(self):
        r = self._route(selection={"route_origin": "compile", "shape": "solo"})
        line = route_chain.build_line(r, event="compile", harness="claude", session_id="s",
                                       route_file="/tmp/r.json")
        self.assertIsNone(line["shape"])

    def test_slug_truncated_to_80(self):
        r = self._route(slug="x" * 200)
        line = route_chain.build_line(r, event="compose", harness="claude", session_id="s",
                                       route_file="/tmp/r.json")
        self.assertEqual(len(line["slug"]), 80)

    def test_oversized_line_raises(self):
        r = self._route(artifact_root="a" * 5000)
        with self.assertRaises(ValueError):
            route_chain.build_line(r, event="compose", harness="claude", session_id="s",
                                    route_file="/tmp/r.json")

    def test_plan_capped_at_max(self):
        plan = ["p%d" % i for i in range(20)]
        line = route_chain.build_line(self._route(), event="compose", harness="claude",
                                       session_id="s", route_file="/tmp/r.json",
                                       plan=plan, plan_source="explicit")
        self.assertEqual(len(line["plan"]), route_chain.MAX_PLAN)


class AppendTest(EnvTmpTestCase):
    def test_append_reports_the_underlying_error_to_its_caller(self):
        errors = []
        denied = PermissionError(13, "Permission denied", "/state/route-chains/claude")
        with mock.patch.object(route_chain, "_prepare_directory", side_effect=denied):
            self.assertFalse(route_chain.append("claude", "sess-denied", {"v": 1}, on_error=errors.append))
        self.assertEqual(len(errors), 1)
        self.assertIn("PermissionError", errors[0])
        self.assertIn("/state/route-chains/claude", errors[0])

    def test_append_creates_0700_dir_and_0600_file_single_write(self):
        line = {"v": 1, "route_id": "rt-1"}
        with mock.patch("os.write", side_effect=os.write) as spy:
            ok = route_chain.append("claude", "sess-a", line)
            self.assertTrue(ok)
            self.assertEqual(spy.call_count, 1)
        path = route_chain.ledger_path("claude", "sess-a")
        self.assertEqual(oct(os.stat(path).st_mode)[-3:], "600")
        self.assertEqual(oct(os.stat(os.path.dirname(path)).st_mode)[-3:], "700")
        with open(path) as fh:
            self.assertEqual(json.loads(fh.readline()), line)

    def test_append_refuses_symlink_target(self):
        path = route_chain.ledger_path("claude", "sess-b")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        real = path + ".real"
        with open(real, "w") as fh:
            fh.write("")
        os.symlink(real, path)
        ok = route_chain.append("claude", "sess-b", {"v": 1})
        self.assertFalse(ok)

    def test_append_refuses_oversized_line(self):
        ok = route_chain.append("claude", "sess-c", {"v": 1, "pad": "x" * 5000})
        self.assertFalse(ok)

    def test_append_two_lines_both_readable(self):
        route_chain.append("claude", "sess-d", {"v": 1, "n": 1})
        route_chain.append("claude", "sess-d", {"v": 1, "n": 2})
        path = route_chain.ledger_path("claude", "sess-d")
        with open(path) as fh:
            lines = [json.loads(l) for l in fh]
        self.assertEqual([l["n"] for l in lines], [1, 2])


class SweepTest(EnvTmpTestCase):
    def test_sweep_removes_only_old_self_owned_files(self):
        path = route_chain.ledger_path("claude", "sess-old")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{}\n")
        old_time = 0
        os.utime(path, (old_time, old_time))
        removed = route_chain.sweep(now=route_chain.SWEEP_MAX_AGE * 2)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(path))

    def test_sweep_keeps_recent_files(self):
        route_chain.append("claude", "sess-new", {"v": 1})
        removed = route_chain.sweep(now=1.0)
        self.assertEqual(removed, 0)


class ReleaseTreeNoWriteTest(unittest.TestCase):
    def test_append_and_sweep_never_write_under_agent_home(self):
        tmp = tempfile.mkdtemp(prefix="route-chain-release-")
        try:
            release = os.path.join(tmp, "release")
            state = os.path.join(tmp, "state")
            os.makedirs(release)
            with open(os.path.join(release, "seed.txt"), "w") as fh:
                fh.write("seed")
            before = {p: os.stat(p).st_mtime for p in glob_module.glob(release + "/**", recursive=True)}
            with mock.patch.dict(os.environ, {"AGENT_HOME": release, "XDG_STATE_HOME": state}):
                for key in ("FLEET_ROUTE_CHAIN_DIR", "FLEET_CAPABILITY_GROUNDING_DIR"):
                    os.environ.pop(key, None)
                route_chain.append("claude", "sess-relhome", {"v": 1})
                route_chain.sweep()
            after = {p: os.stat(p).st_mtime for p in glob_module.glob(release + "/**", recursive=True)}
            self.assertEqual(before, after)
            self.assertTrue(os.path.isdir(os.path.join(state, "agent-fleet", "route-chains", "claude")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ReadTailTest(EnvTmpTestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(route_chain.read_tail("claude", "nobody"), [])

    def test_drops_partial_first_line_when_seeking_mid_file(self):
        path = route_chain.ledger_path("claude", "sess-tail")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        good = {"v": 1, "harness": "claude", "session_id": "sess-tail",
                "route_id": "rt-1", "route_file": "/tmp/r.json"}
        with open(path, "w") as fh:
            fh.write("garbage-not-json-and-long-padding-" + "x" * 200 + "\n")
            fh.write(json.dumps(good) + "\n")
        lines = route_chain.read_tail("claude", "sess-tail", max_bytes=150)
        self.assertEqual(lines, [good])

    def test_drops_foreign_harness_and_session_lines(self):
        path = route_chain.ledger_path("claude", "sess-tail2")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        wrong_harness = {"v": 1, "harness": "codex", "session_id": "sess-tail2",
                          "route_id": "rt-1", "route_file": "/tmp/r.json"}
        wrong_session = {"v": 1, "harness": "claude", "session_id": "other",
                          "route_id": "rt-1", "route_file": "/tmp/r.json"}
        with open(path, "w") as fh:
            fh.write(json.dumps(wrong_harness) + "\n")
            fh.write(json.dumps(wrong_session) + "\n")
        self.assertEqual(route_chain.read_tail("claude", "sess-tail2"), [])


    def test_drops_line_whose_ts_is_present_but_not_a_number(self):
        path = route_chain.ledger_path("claude", "sess-ts")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        base = {"v": 1, "harness": "claude", "session_id": "sess-ts",
                "route_file": "/tmp/r.json"}
        good = dict(base, route_id="rt-good", ts=2.0)
        no_ts = dict(base, route_id="rt-no-ts")
        with open(path, "w") as fh:
            for bad_ts in ("2026-09-18", None, True):
                fh.write(json.dumps(dict(base, route_id="rt-bad", ts=bad_ts)) + "\n")
            fh.write(json.dumps(no_ts) + "\n")
            fh.write(json.dumps(good) + "\n")
        self.assertEqual(route_chain.read_tail("claude", "sess-ts"), [no_ts, good])

class ChainKeyAndSegmentTest(unittest.TestCase):
    def test_chain_key_priority(self):
        self.assertEqual(route_chain.chain_key({"campaign_key": "k1", "parent_cycle_id": "p1"}), "k1")
        self.assertEqual(route_chain.chain_key({"parent_cycle_id": "p1"}), "parent-cycle:p1")
        self.assertEqual(route_chain.chain_key({}), "_unassigned")

    def test_current_segment_restarts_on_campaign_change(self):
        lines = [
            {"ts": 1, "campaign_key": "k1"}, {"ts": 2, "campaign_key": "k1"},
            {"ts": 3, "campaign_key": "k2"}, {"ts": 4, "campaign_key": "k2"},
        ]
        segment = route_chain.current_segment(lines)
        self.assertEqual([l["ts"] for l in segment], [3, 4])

    def test_current_segment_empty_input(self):
        self.assertEqual(route_chain.current_segment([]), [])


class ParsePlanTest(unittest.TestCase):
    known = {"autopilot-code", "autopilot-refine", "autopilot-research", "autopilot-draft"}

    def test_accepts_short_and_full_names(self):
        self.assertEqual(route_chain.parse_plan("research,draft,code", self.known),
                          ["research", "draft", "code"])
        self.assertEqual(route_chain.parse_plan("autopilot-research,autopilot-draft", self.known),
                          ["research", "draft"])

    def test_rejects_unknown_capability(self):
        with self.assertRaises(ValueError):
            route_chain.parse_plan("not-a-capability", self.known)

    def test_rejects_too_many(self):
        with self.assertRaises(ValueError):
            route_chain.parse_plan(",".join(["code"] * 13), self.known)

    def test_rejects_duplicate_consecutive(self):
        with self.assertRaises(ValueError):
            route_chain.parse_plan("code,code", self.known)

    def test_none_is_none(self):
        self.assertIsNone(route_chain.parse_plan(None, self.known))


class InheritedPlanTest(unittest.TestCase):
    def test_inherited_from_latest_line_with_plan(self):
        lines = [{"plan": ["a", "b"]}, {"plan": None}, {}]
        plan, source = route_chain.inherited_plan(lines)
        self.assertEqual(plan, ["a", "b"])
        self.assertEqual(source, "inherited")

    def test_no_plan_anywhere(self):
        self.assertEqual(route_chain.inherited_plan([{}, {"plan": None}]), ([], None))


def _fake_route(rid, capability, *, campaign_key="k1", route_hash=None, shape=None, **extra):
    record = {"route_id": rid, "route_hash": route_hash or ("sha256:" + rid), "capability": capability,
              "capability_mode": "default", "effective_intensity": "standard",
              "campaign_key": campaign_key, "artifact_root": "/tmp/art"}
    if shape:
        record["selection"] = {"route_origin": "compose", "shape": shape}
    record.update(extra)
    return record


def _line_for(record, *, event="compose", harness="claude", session_id="sess-a", ts=1.0,
              route_file=None, plan=None, plan_source=None):
    return route_chain.build_line(
        record, event=event, harness=harness, session_id=session_id,
        route_file=route_file or ("/tmp/%s.json" % record["route_id"]), now=ts,
        plan=plan, plan_source=plan_source,
    )


class _FakeOutcomes:
    """load_outcome stand-in keyed by route_id."""

    def __init__(self, table):
        self.table = table

    def __call__(self, route_file, rid, route_hash):
        entry = self.table.get(rid)
        if entry is None:
            return {"present": False}
        return dict(entry)


class _FakeRecords:
    """load_record stand-in keyed by route_id (route_hash/route_file ignored)."""

    def __init__(self, table):
        self.table = table

    def __call__(self, route_file, route_hash, rid):
        return self.table.get(rid)


class AssembleStateTest(unittest.TestCase):
    def _assemble(self, lines, records, outcomes, **kwargs):
        return route_chain.assemble(
            lines, load_record=_FakeRecords(records), load_outcome=_FakeOutcomes(outcomes),
            **kwargs,
        )

    def test_open_when_outcome_absent(self):
        r = _fake_route("rt-1", "autopilot-code")
        chain = self._assemble([_line_for(r)], {"rt-1": r}, {})
        self.assertEqual(chain["nodes"][0]["state"], "open")

    def test_done_when_terminal_gate_proven_true(self):
        r = _fake_route("rt-1", "autopilot-code")
        chain = self._assemble([_line_for(r)], {"rt-1": r},
                                {"rt-1": {"present": True, "terminal_gate_proven": True, "match": True}})
        self.assertEqual(chain["nodes"][0]["state"], "done")

    def test_unproven_terminal_gate_is_unknown_not_failure(self):
        r = _fake_route("rt-1", "autopilot-code")
        chain = self._assemble([_line_for(r)], {"rt-1": r},
                                {"rt-1": {"present": True, "terminal_gate_proven": False, "match": True}})
        self.assertEqual(chain["nodes"][0]["state"], "unknown")

    def test_unknown_when_terminal_gate_proven_null_or_absent(self):
        r = _fake_route("rt-1", "autopilot-code")
        for tgp in (None, "absent"):
            chain = self._assemble([_line_for(r)], {"rt-1": r},
                                    {"rt-1": {"present": True, "terminal_gate_proven": tgp, "match": True}})
            self.assertEqual(chain["nodes"][0]["state"], "unknown")

    def test_unknown_with_ambiguity_on_outcome_mismatch(self):
        r = _fake_route("rt-1", "autopilot-code")
        chain = self._assemble([_line_for(r)], {"rt-1": r},
                                {"rt-1": {"present": True, "terminal_gate_proven": True, "match": False}})
        node = chain["nodes"][0]
        self.assertEqual(node["state"], "unknown")
        self.assertIn("outcome-mismatch", node["ambiguity"])

    def test_record_mismatch_is_unknown_with_ambiguity(self):
        r = _fake_route("rt-1", "autopilot-code")
        chain = self._assemble([_line_for(r)], {}, {})
        node = chain["nodes"][0]
        self.assertEqual(node["state"], "unknown")
        self.assertIn("route-record-mismatch", node["ambiguity"])
        self.assertEqual(node["capability"], "autopilot-code")

    def test_same_route_compose_and_start_is_one_node(self):
        r = _fake_route("rt-1", "autopilot-code")
        lines = [_line_for(r, event="compose", ts=1.0), _line_for(r, event="start", ts=2.0)]
        chain = self._assemble(lines, {"rt-1": r}, {})
        self.assertEqual(len(chain["nodes"]), 1)


class AssemblePlanAndVisibilityTest(unittest.TestCase):
    def _assemble(self, lines, records, outcomes):
        return route_chain.assemble(
            lines, load_record=_FakeRecords(records), load_outcome=_FakeOutcomes(outcomes),
        )

    def test_single_node_without_plan_is_not_visible(self):
        r = _fake_route("rt-1", "autopilot-research")
        chain = self._assemble([_line_for(r)], {"rt-1": r}, {})
        self.assertFalse(chain["visible"])

    def test_plan_merge_actual_wins_and_remaining_declared_are_planned(self):
        r1 = _fake_route("rt-1", "research")
        r2 = _fake_route("rt-2", "draft", shape="solo")
        lines = [
            _line_for(r1, ts=1.0, plan=["research", "draft", "apply"], plan_source="explicit"),
            _line_for(r2, ts=2.0),
        ]
        chain = self._assemble(lines, {"rt-1": r1, "rt-2": r2},
                                {"rt-1": {"present": True, "terminal_gate_proven": True, "match": True}})
        labels = [n["label"] for n in chain["nodes"]]
        states = [n["state"] for n in chain["nodes"]]
        self.assertEqual(labels, ["research", "draft", "apply"])
        self.assertEqual(states, ["done", "open", "planned"])
        self.assertTrue(chain["visible"])

    def test_plan_merge_skipped_declared_are_dropped(self):
        r1 = _fake_route("rt-1", "research")
        r2 = _fake_route("rt-2", "apply")
        lines = [
            _line_for(r1, ts=1.0, plan=["research", "draft", "apply"], plan_source="explicit"),
            _line_for(r2, ts=2.0),
        ]
        chain = self._assemble(lines, {"rt-1": r1, "rt-2": r2}, {})
        labels = [n["label"] for n in chain["nodes"]]
        self.assertEqual(labels, ["research", "apply"])

    def test_shape_only_on_last_actual_node(self):
        r1 = _fake_route("rt-1", "research", shape="direct")
        r2 = _fake_route("rt-2", "draft", shape="solo")
        lines = [_line_for(r1, ts=1.0), _line_for(r2, ts=2.0)]
        chain = self._assemble(lines, {"rt-1": r1, "rt-2": r2}, {})
        self.assertIsNone(chain["nodes"][0]["shape"])
        self.assertEqual(chain["nodes"][1]["shape"], "solo")


class AssembleContinuationTest(unittest.TestCase):
    def setUp(self):
        route.clear_cache()
        self.gen0 = route.load(os.path.join(_FIXDIR, "gen0.json"))
        self.gen1 = route.load(os.path.join(_FIXDIR, "gen1.json"))
        self.node_evidence = {
            self.gen0["route_id"]: {
                "plan": {"attempt_history": [
                    {"attempt_id": "att-x", "parent_attempt_id": "att-fixture-owner",
                     "contract_status": "current", "status": "done", "pid": 1},
                ]},
            },
        }

    def _assemble(self, lines, records):
        return route_chain.assemble(
            lines, load_record=_FakeRecords(records), load_outcome=lambda *a: {"present": False},
            node_evidence=self.node_evidence,
        )

    def test_verified_continuation_folds_and_successor_decides_state(self):
        lines = [
            _line_for(self.gen0, event="compose", ts=1.0,
                      route_file=os.path.join(_FIXDIR, "gen0.json")),
            _line_for(self.gen1, event="continuation", ts=2.0,
                      route_file=os.path.join(_FIXDIR, "gen1.json")),
        ]
        records = {self.gen0["route_id"]: self.gen0, self.gen1["route_id"]: self.gen1}
        chain = self._assemble(lines, records)
        self.assertEqual(len(chain["nodes"]), 1)
        self.assertEqual(chain["nodes"][0]["route_id"], self.gen1["route_id"])

    def test_unverified_continuation_is_separate_unknown_node(self):
        broken = copy.deepcopy(self.gen1)
        broken["source_route_hash"] = "sha256:" + "0" * 64
        digest = route.route_hash(broken)
        broken["route_hash"] = digest
        broken["route_id"] = "rt-" + digest.split(":", 1)[1][:16]
        lines = [
            _line_for(self.gen0, event="compose", ts=1.0,
                      route_file=os.path.join(_FIXDIR, "gen0.json")),
            _line_for(broken, event="continuation", ts=2.0, route_file="/tmp/broken.json"),
        ]
        records = {self.gen0["route_id"]: self.gen0, broken["route_id"]: broken}
        chain = self._assemble(lines, records)
        self.assertEqual(len(chain["nodes"]), 2)
        self.assertTrue(any(
            a.startswith("continuation-unverified:") for a in chain["nodes"][1]["ambiguity"]
        ))


class AssembleFailedFoldTest(unittest.TestCase):
    def _assemble(self, lines, records, outcomes):
        return route_chain.assemble(
            lines, load_record=_FakeRecords(records), load_outcome=_FakeOutcomes(outcomes),
        )

    def test_unproven_then_same_capability_keeps_both_observations(self):
        r1 = _fake_route("rt-1", "autopilot-code")
        r2 = _fake_route("rt-2", "autopilot-code")
        lines = [_line_for(r1, ts=1.0), _line_for(r2, ts=2.0)]
        chain = self._assemble(
            lines, {"rt-1": r1, "rt-2": r2},
            {"rt-1": {"present": True, "terminal_gate_proven": False, "match": True},
             "rt-2": {"present": True, "terminal_gate_proven": True, "match": True}},
        )
        self.assertEqual(len(chain["nodes"]), 2)
        self.assertEqual([node["round"] for node in chain["nodes"]], [1, 1])
        self.assertEqual([node["state"] for node in chain["nodes"]], ["unknown", "done"])

    def test_trailing_unproven_stays_unknown(self):
        r1 = _fake_route("rt-1", "autopilot-code")
        chain = self._assemble(
            [_line_for(r1, ts=1.0)], {"rt-1": r1},
            {"rt-1": {"present": True, "terminal_gate_proven": False, "match": True}},
        )
        self.assertEqual(chain["nodes"][0]["state"], "unknown")
        self.assertEqual(chain["nodes"][0]["round"], 1)


class CurrentCapabilityTest(unittest.TestCase):
    def test_open_route_beats_marker(self):
        chain = {"current": {"state": "open", "ts": 100.0, "capability": "autopilot-code",
                              "capability_mode": "dev", "intensity": "standard", "route_id": "rt-1"}}
        marker = {"capability": "autopilot-refine", "source": "marker"}
        result = route_chain.current_capability(chain, marker, session_start=90.0, slack=5.0)
        self.assertEqual(result["capability"], "autopilot-code")
        self.assertEqual(result["source"], "route-chain")

    def test_stale_event_falls_back_to_marker(self):
        chain = {"current": {"state": "open", "ts": 10.0, "capability": "autopilot-code"}}
        marker = {"capability": "autopilot-refine", "source": "marker"}
        result = route_chain.current_capability(chain, marker, session_start=90.0, slack=5.0)
        self.assertEqual(result, marker)

    def test_no_current_node_falls_back_to_marker(self):
        marker = {"capability": "autopilot-refine", "source": "marker"}
        self.assertEqual(route_chain.current_capability({}, marker, session_start=0, slack=5), marker)


class EnrichTest(EnvTmpTestCase):
    def test_enrich_skips_children_and_app_server(self):
        sessions = [
            _sess(harness="opencode", session_id="oc-1"),
            _sess(harness="claude", session_id="c-1", is_child=True),
            _sess(harness="claude", session_id="c-2", app_server=True),
            _sess(harness="claude", session_id="c-3"),
        ]
        route_chain.enrich(sessions)
        # opencode is an env writer like the others: it is enriched, not skipped.
        self.assertIsNotNone(sessions[0].route_chain)   # empty chain dict, not None
        self.assertIsNone(sessions[1].route_chain)
        self.assertIsNone(sessions[2].route_chain)
        self.assertIsNotNone(sessions[3].route_chain)   # empty chain dict, not None

    def test_enrich_handoff_marks_origin_and_receiver(self):
        r1 = _fake_route("rt-1", "research")
        line_origin = _line_for(r1, event="compose", harness="claude", session_id="sess-o", ts=1.0)
        line_receiver = _line_for(r1, event="start", harness="claude", session_id="sess-r", ts=2.0)
        route_chain.append("claude", "sess-o", line_origin)
        route_chain.append("claude", "sess-r", line_receiver)
        origin = _sess(harness="claude", session_id="sess-o")
        receiver = _sess(harness="claude", session_id="sess-r")
        with mock.patch("fleet.route.load", side_effect=lambda *a, **k: r1), \
             mock.patch("fleet.route.load_outcome",
                         side_effect=lambda *a, **k: {"present": False}):
            route_chain.enrich([origin, receiver])
        origin_node = origin.route_chain["nodes"][0]
        receiver_node = receiver.route_chain["nodes"][0]
        self.assertEqual(origin_node["handoff_to"], {"harness": "claude", "session_id": "sess-r"})
        self.assertEqual(receiver_node["handoff_from"], {"harness": "claude", "session_id": "sess-o"})

    def test_targeted_enrich_keeps_other_sessions_as_handoff_context(self):
        r1 = _fake_route("rt-1", "research")
        route_chain.append("claude", "sess-o", _line_for(
            r1, event="compose", harness="claude", session_id="sess-o", ts=1.0))
        route_chain.append("claude", "sess-r", _line_for(
            r1, event="start", harness="claude", session_id="sess-r", ts=2.0))
        origin = _sess(harness="claude", session_id="sess-o")
        receiver = _sess(harness="claude", session_id="sess-r")
        origin.route_chain = {"cached": "preserve"}

        with mock.patch("fleet.route.load", side_effect=lambda *a, **k: r1), \
                mock.patch("fleet.route.load_outcome",
                           side_effect=lambda *a, **k: {"present": False}):
            route_chain.enrich([origin, receiver], only_sessions=[receiver])

        self.assertEqual(origin.route_chain, {"cached": "preserve"})
        self.assertEqual(receiver.route_chain["nodes"][0]["handoff_from"],
                         {"harness": "claude", "session_id": "sess-o"})

    def test_enrich_merges_alias_keys_by_ts(self):
        r1 = _fake_route("rt-1", "research")
        r2 = _fake_route("rt-2", "draft")
        route_chain.append("claude", "old-id", _line_for(r1, harness="claude", session_id="old-id", ts=1.0))
        route_chain.append("claude", "new-id", _line_for(r2, harness="claude", session_id="new-id", ts=2.0))
        sess = _sess(harness="claude", session_id="new-id", aliases=["old-id"])
        with mock.patch("fleet.route.load", side_effect=lambda path, h, rid: r1 if rid == "rt-1" else r2), \
             mock.patch("fleet.route.load_outcome", side_effect=lambda *a, **k: {"present": False}):
            route_chain.enrich([sess])
        labels = [n["label"] for n in sess.route_chain["nodes"]]
        self.assertEqual(labels, ["research", "draft"])


    def test_enrich_one_bad_session_does_not_blank_the_others(self):
        r1 = _fake_route("rt-1", "research")
        route_chain.append("claude", "sess-ok", _line_for(r1, harness="claude",
                                                          session_id="sess-ok", ts=1.0))
        good = _sess(harness="claude", session_id="sess-ok")
        bad = _sess(harness="claude", session_id="sess-bad")
        real_segment = route_chain.current_segment

        def segment(lines):
            if not lines:
                raise TypeError("simulated malformed segment")
            return real_segment(lines)

        with mock.patch.object(route_chain, "current_segment", side_effect=segment), \
             mock.patch("fleet.route.load", side_effect=lambda *a, **k: r1), \
             mock.patch("fleet.route.load_outcome",
                         side_effect=lambda *a, **k: {"present": False}):
            route_chain.enrich([bad, good])
        self.assertEqual([n["label"] for n in good.route_chain["nodes"]], ["research"])

class PerformanceGuardTest(EnvTmpTestCase):
    def test_enrich_never_lists_route_dirs_or_opens_jobs_log(self):
        r1 = _fake_route("rt-1", "research")
        route_chain.append("claude", "sess-perf", _line_for(r1, harness="claude", session_id="sess-perf"))
        sess = _sess(harness="claude", session_id="sess-perf")

        real_listdir, real_scandir, real_glob, real_open = os.listdir, os.scandir, glob_module.glob, open

        def guarded_listdir(path="."):
            assert ".runtime" not in str(path) or "routes" not in str(path), str(path)
            return real_listdir(path)

        def guarded_scandir(path="."):
            assert ".runtime" not in str(path) or "routes" not in str(path), str(path)
            return real_scandir(path)

        def guarded_glob(pattern, *a, **k):
            assert "jobs.log" not in str(pattern) and "routes" not in str(pattern), str(pattern)
            return real_glob(pattern, *a, **k)

        def guarded_open(path, *a, **k):
            assert not str(path).endswith("jobs.log"), str(path)
            return real_open(path, *a, **k)

        load_calls = []
        real_load = route.load

        def counting_load(*a, **k):
            load_calls.append(a)
            return real_load(*a, **k)

        with mock.patch("os.listdir", side_effect=guarded_listdir), \
             mock.patch("os.scandir", side_effect=guarded_scandir), \
             mock.patch("glob.glob", side_effect=guarded_glob), \
             mock.patch("builtins.open", side_effect=guarded_open), \
             mock.patch("fleet.route.load", side_effect=counting_load):
            route_chain.enrich([sess])
        self.assertEqual(len(load_calls), 1)


class RouteOutcomeLoaderTest(unittest.TestCase):
    def setUp(self):
        route.clear_cache()
        self.tmp = tempfile.mkdtemp(prefix="route-outcome-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        route.clear_cache()

    def _write(self, name, payload):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            json.dump(payload, fh)
        return path

    def test_absent_when_no_sidecar(self):
        route_file = os.path.join(self.tmp, "rt-1.json")
        self.assertEqual(route.load_outcome(route_file), {"present": False})

    def test_live_inline_finish_intent_overrides_closed_outcome_to_pending(self):
        route_file = self._write("rt-pending.json", {
            "route_id": "rt-pending", "route_hash": "hash-pending",
            "artifact_root": self.tmp,
        })
        with open(os.path.join(self.tmp, "rt-pending.outcome.json"), "w") as fh:
            json.dump({"terminal_gate_proven": True, "route_id": "rt-pending",
                       "route_hash": "hash-pending"}, fh)
        sys.path.insert(0, os.path.join(_TOOLS_DIR, "..", "utilities"))
        import inline_finish
        with mock.patch.object(inline_finish, "pending_state", return_value={
                "schema": "inline_finish_v1", "state": "route-closed"}):
            result = route.load_outcome(route_file, expect_id="rt-pending", expect_hash="hash-pending")
        self.assertTrue(result["finish_pending"])
        self.assertEqual(result["finish_state"], "route-closed")
        self.assertFalse(result["terminal_gate_proven"])

    def test_present_true_false_and_absent(self):
        route_file = self._write("rt-2.json", {})
        outcome_path = os.path.join(self.tmp, "rt-2.outcome.json")
        for tgp, expected in ((True, True), (False, False)):
            with open(outcome_path, "w") as fh:
                json.dump({"terminal_gate_proven": tgp, "route_id": "rt-2", "route_hash": "h2"}, fh)
            route.clear_cache()
            result = route.load_outcome(route_file, expect_id="rt-2", expect_hash="h2")
            self.assertEqual(result["present"], True)
            self.assertEqual(result["terminal_gate_proven"], expected)
            self.assertTrue(result["match"])
        with open(outcome_path, "w") as fh:
            json.dump({"route_id": "rt-2", "route_hash": "h2"}, fh)   # key literally absent
        route.clear_cache()
        result = route.load_outcome(route_file, expect_id="rt-2", expect_hash="h2")
        self.assertEqual(result["terminal_gate_proven"], "absent")

    def test_mismatch_reported_via_match_false(self):
        route_file = self._write("rt-3.json", {})
        outcome_path = os.path.join(self.tmp, "rt-3.outcome.json")
        with open(outcome_path, "w") as fh:
            json.dump({"terminal_gate_proven": True, "route_id": "rt-3", "route_hash": "wrong"}, fh)
        result = route.load_outcome(route_file, expect_id="rt-3", expect_hash="right")
        self.assertFalse(result["match"])

    def test_cached_across_calls_without_restat(self):
        route_file = self._write("rt-4.json", {})
        outcome_path = os.path.join(self.tmp, "rt-4.outcome.json")
        with open(outcome_path, "w") as fh:
            json.dump({"terminal_gate_proven": True}, fh)
        first = route.load_outcome(route_file)
        with mock.patch("os.stat", side_effect=os.stat) as spy:
            second = route.load_outcome(route_file)
            self.assertGreaterEqual(spy.call_count, 1)
        self.assertEqual(first, second)

    def test_never_writes(self):
        import inspect
        source = inspect.getsource(route.load_outcome) + inspect.getsource(route._load_outcome_uncached)
        for token in ('"w"', "'w'", "os.replace", "os.rename", 'open(path, "a"'):
            self.assertNotIn(token, source)


class AutomaticSupplementTest(EnvTmpTestCase):
    def test_record_supplements_manual_chain_without_becoming_authority(self):
        utilities = os.path.join(os.path.dirname(_TOOLS_DIR), 'utilities')
        sys.path.insert(0, utilities)
        import turn_change_record
        session = _sess('codex', 'native-a', cwd='/repo')
        lines = [{'route_id':'manual','harness':'codex','session_id':'native-a',
                  'route_file':'/missing/manual.json','capability':'autopilot-code','ts':1}]
        supplement = {'session_id':'native-a','changed_files':260,'summary':'exit 1; final reply reference'}
        with mock.patch.object(route_chain, 'session_lines', return_value=lines), \
             mock.patch.object(turn_change_record, 'supplementary_records', return_value=[supplement]) as reader:
            route_chain.enrich([session])
        reader.assert_called_once_with('codex', 'native-a', '/repo')
        self.assertEqual(session.route_chain['current']['route_id'], 'manual')
        self.assertEqual(session.route_chain['automatic_records'], [supplement])
        self.assertTrue(session.route_chain['visible'])
        automatic = {'current':{'automatic':True,'state':'open','ts':5,'capability':'autopilot-code'}}
        self.assertEqual(route_chain.current_capability(automatic, {'source':'marker'}, session_start=0, slack=1), {'source':'marker'})


if __name__ == "__main__":
    unittest.main()
