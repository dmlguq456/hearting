#!/usr/bin/env python3
"""Decision table for route_authority: the six cases of 2026-10-06, as they are decided today.

Stage 1 moved each judgment into `route_authority` without changing it. These
rows pin the current answers; a later stage that changes a meaning changes the
matching row here on purpose (ROUTE-AUTHORITY-DESIGN-REVIEW-1006.md §2).
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import route_authority as RA  # noqa: E402
import dispatch_contract as DC  # noqa: E402
import dispatch_replacement as DR  # noqa: E402
import dispatch_seat_handover as HANDOVER  # noqa: E402
import opencode_runtime  # noqa: E402
from execution_access import ExecutionAccessRequest  # noqa: E402
from route_identity import route_hash  # noqa: E402


class RotatedFrameCapacityAuthorityTest(unittest.TestCase):
    def test_verified_pair_rotation_uses_ordinary_capacity_without_transition(self):
        meta = {"worker_type": "frame", "dispatch_depth": "1",
                "route_node": "frame-alternative", "note": "dead-capacity",
                "attempt_id": "att-pair"}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch("route_parent_close.row_requested", return_value=False), \
             mock.patch.object(DR, "_frame_pair_rotation_verified", return_value=True):
            jobs = Path(td) / "jobs.log"
            jobs.write_text("")
            self.assertEqual(RA._replacement_kind("done", meta, jobs=jobs, lines=[]), "capacity")



def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fail_row(attempt_id, **extra):
    """A done stage worker whose envelope said FAIL, as the registry records it."""
    return {"_status": "done", "attempt_id": attempt_id, "note": "dead-worker-fail",
            "failure_class": "fail", "worker_type": "stage", **extra}


class Case1ParentSessionTest(unittest.TestCase):
    """Case 1: a Codex parent handed its seat to a Claude successor (beside -> retire)."""

    OWNER = {"dispatch_depth": "1", "worker_type": "owner", "attempt_id": "att-owner",
             "parent_sid": "codex-sid", "parent_harness": "codex",
             "owner_route_id": "rt-96bab699717dba8f", "owner_route_hash": "sha256:96ba"}

    def _owns_after(self, successor_sid, successor_harness):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            handover = {"from": "codex-sid", "sid": successor_sid, "harness": successor_harness,
                        "bindings": [HANDOVER.binding_of(jobs, self.OWNER)], "ts": 1}
            with mock.patch.object(HANDOVER, "_lineage_seats", return_value={"seat": [handover]}):
                return RA.owns(self.OWNER, successor_sid, jobs)

    def test_the_registered_session_owns_its_route(self):
        self.assertTrue(RA.owns(self.OWNER, "codex-sid", None))
        self.assertFalse(RA.owns(self.OWNER, "", None))

    def test_handover_cannot_take_an_open_foreign_owner_with_an_empty_actor(self):
        decision = RA.decide_continuation(action="handover", status="open", metadata=self.OWNER,
                                          facts={"eligible": True, "actor": ""})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "work-parent-recovery-required")

    def test_a_recorded_successor_inherits_on_any_harness(self):
        # RA-2 (stage 2): a recorded handover row moves the parent role across harnesses.
        self.assertTrue(self._owns_after("codex-sid-after-clear", "codex"))
        self.assertTrue(self._owns_after("claude-sid", "claude"))

    def test_only_current_recipient_owns_after_two_handovers(self):
        jobs = Path("/tmp/registered-parent-handover-jobs")
        binding = HANDOVER.binding_of(jobs, self.OWNER)
        rows = [
            {"from": "codex-sid", "sid": "claude-sid", "harness": "claude",
             "bindings": [binding], "ts": 1},
            {"from": "claude-sid", "sid": "opencode-sid", "harness": "opencode",
             "bindings": [binding], "ts": 2},
        ]
        with mock.patch.object(HANDOVER, "_lineage_seats", return_value={"seat": rows}):
            self.assertFalse(RA.owns(self.OWNER, "codex-sid", jobs))
            self.assertFalse(RA.owns(self.OWNER, "claude-sid", jobs))
            self.assertTrue(RA.owns(self.OWNER, "opencode-sid", jobs))

    def test_old_parent_cannot_write_a_second_snapshot_but_current_successor_can(self):
        import session_tidy as tidy
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        jobs = root / "jobs.log"
        binding = HANDOVER.binding_of(jobs, self.OWNER)
        seat = tidy.Seat("pane", "snapshot-lineage-seat", "w9:pSnapshot")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
            snapshot = {"schema": HANDOVER.SCHEMA, "seat": seat.as_dict(),
                        "from": {"harness": "codex", "sid": "codex-sid"},
                        "at": 1, "bindings": [binding], "snapshot_id": "snapshot-a"}
            tidy.atomic_write_json(HANDOVER._snapshot_path(seat.key), snapshot)
            ledger = tidy._ledger_path(seat)
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(json.dumps({"event": "handover", "from": "codex-sid",
                "sid": "successor", "harness": "codex", "ts": 2,
                "bindings": [binding]}) + "\n")
            before_snapshot, before_ledger = (HANDOVER._snapshot_path(seat.key).read_bytes(),
                                               ledger.read_bytes())
            with mock.patch.object(HANDOVER, "_confirmed_clear", return_value=True), \
                 mock.patch.object(tidy, "session_summary", return_value={
                     ("codex", "third"): {"first_seen": 3}}):
                self.assertIsNone(HANDOVER.record_locked(
                    seat, "codex", "third", "start", "clear", 3))
            self.assertEqual(HANDOVER._snapshot_path(seat.key).read_bytes(), before_snapshot)
            self.assertEqual(ledger.read_bytes(), before_ledger)

            # The current recipient can take a fresh snapshot and hand it on.
            snapshot.update({"from": {"harness": "codex", "sid": "successor"},
                             "at": 3, "snapshot_id": "snapshot-b"})
            tidy.atomic_write_json(HANDOVER._snapshot_path(seat.key), snapshot)
            with mock.patch.object(HANDOVER, "_confirmed_clear", return_value=True), \
                 mock.patch.object(tidy, "session_summary", return_value={
                     ("codex", "third"): {"first_seen": 4}}):
                row = HANDOVER.record_locked(seat, "codex", "third", "start", "clear", 4)
            self.assertEqual((row["from"], row["sid"]), ("successor", "third"))

    def test_unreadable_handover_keeps_registered_parent_safe_default(self):
        jobs = Path("/tmp/registered-parent-unreadable-jobs")
        with mock.patch.object(HANDOVER, "_lineage_seats", side_effect=OSError("unreadable")):
            self.assertTrue(RA.owns(self.OWNER, "codex-sid", jobs))
            self.assertFalse(RA.owns(self.OWNER, "claude-sid", jobs))

    def test_a_non_owner_cannot_start_the_replacement_owner(self):
        with self.assertRaises(DC.DispatchContractError) as refused:
            RA.require_replacement_parent(None, {}, self.OWNER, current_session=lambda: "claude-sid")
        self.assertEqual(refused.exception.reason, "replacement-parent-identity-unproven")
        RA.require_replacement_parent(None, {}, self.OWNER, current_session=lambda: "codex-sid")

    def test_identity_refusal_reentry_depends_on_exact_lineage_not_old_claim_shape(self):
        metadata = {"attempt_id": "att-owner", "worker_type": "owner"}
        facts = {"exact_claim": True, "identity_refusal": True,
                 "source_replacement_eligible": True, "successor_unstarted_or_absent": True}
        decision = RA.decide_continuation(action="identity-refusal-reentry", status="done",
                                          metadata=metadata, facts=facts)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.next_action, "start")
        for field in facts:
            with self.subTest(field=field):
                refused = RA.decide_continuation(action="identity-refusal-reentry", status="done",
                    metadata=metadata, facts={**facts, field: False})
                self.assertFalse(refused.allowed)
        self.assertFalse(RA.decide_continuation(action="identity-refusal-reentry", status="open",
            metadata=metadata, facts=facts).allowed)

    def test_replacement_lineage_compares_the_registered_parent_exactly(self):
        self.assertTrue(RA.lineage_parent_matches(self.OWNER, thread_id="codex-sid", parent_attempt_id=None))
        self.assertFalse(RA.lineage_parent_matches(self.OWNER, thread_id="claude-sid", parent_attempt_id=None))
        stage = {"dispatch_depth": "2", "parent_attempt_id": "att-owner"}
        self.assertTrue(RA.lineage_parent_matches(stage, thread_id="x", parent_attempt_id="att-owner"))
        self.assertFalse(RA.lineage_parent_matches(stage, thread_id="x", parent_attempt_id="att-other"))

    def test_replacement_parent_attempt_and_depth2_session_remain_exact(self):
        stage={**self.OWNER,'dispatch_depth':'2','parent_attempt_id':'att-owner'}
        self.assertTrue(RA.replacement_parent_matches(stage,stage,None))
        self.assertFalse(RA.replacement_parent_matches(stage,{**stage,'parent_sid':'other'},None))
        self.assertFalse(RA.replacement_parent_matches(stage,{**stage,'parent_attempt_id':'other'},None))
        with mock.patch.object(RA,'owns',return_value=True):
            self.assertFalse(RA.replacement_parent_matches(stage,{**stage,'parent_sid':'successor'},None))

    def test_the_runtime_parent_is_the_calling_session(self):
        env = {"CODEX_THREAD_ID": "codex-sid", "CODEX_DISPATCH_PARENT_CURRENT_FORCE": "1"}
        for honor_force, expected in ((True, "codex-sid"), (False, "synthetic")):
            args = SimpleNamespace(dispatch_depth=2, parent_session_id="synthetic",
                                   parent_slug="owner", parent_harness="claude")
            RA.bind_runtime_parent(args, honor_force=honor_force, environ=env)
            self.assertEqual(args.parent_session_id, expected)
        args = SimpleNamespace(dispatch_depth=1, parent_session_id="synthetic",
                               parent_slug="owner", parent_harness="claude")
        RA.bind_runtime_parent(args, environ={"CLAUDE_CODE_SESSION_ID": "claude-sid"})
        self.assertEqual((args.parent_session_id, args.parent_harness, args.parent_slug),
                         ("claude-sid", "claude", None))
        # Every harness binds the same way: an OpenCode caller is its own runtime parent too,
        # and an ambiguous caller binds nothing.
        for env, expected in (({"OPENCODE_SESSION_ID": "ses_1"}, ("ses_1", "opencode", None)),
                              ({"CLAUDE_SESSION_ID": "claude-old"}, ("claude-old", "claude", None)),
                              ({"CODEX_THREAD_ID": "x", "CLAUDE_CODE_SESSION_ID": "c"},
                               ("synthetic", "claude", "owner"))):
            args = SimpleNamespace(dispatch_depth=1, parent_session_id="synthetic",
                                   parent_slug="owner", parent_harness="claude")
            RA.bind_runtime_parent(args, environ=env)
            self.assertEqual((args.parent_session_id, args.parent_harness, args.parent_slug), expected)
        self.assertEqual(RA.caller_identity({"CLAUDE_CODE_SESSION_ID": "claude-sid"}), ("claude", "claude-sid"))
        # One reading now: the correction sender is the same session caller_identity names
        # (the old copy did not read CLAUDE_CODE_SESSION_ID and labelled it "operator").
        self.assertEqual(RA.correction_source_session({"CLAUDE_CODE_SESSION_ID": "claude-sid"}), "claude-sid")


class Case2SealedPinTest(unittest.TestCase):
    """Case 2: `--pin owner=codex` stays sealed in the route; only the route's parent moves the
    owner pin later, by a recorded change (RA-3, `OwnerPinChangeTest`), never by a new hash."""

    ROUTE = {"selection_pins": {"owner": {"harness": "codex"}, "worker": {"harness": "codex"}}}

    def test_the_sealed_pin_beats_the_request_while_it_is_available(self):
        self.assertEqual(RA.pinned_launch_harness(self.ROUTE, worker_type="owner", requested="claude",
                                                  available=lambda harness: True), ("codex", "claude"))
        self.assertEqual(RA.pinned_launch_harness(self.ROUTE, worker_type="owner", requested="claude",
                                                  available=lambda harness: False), ("claude", None))
        self.assertEqual(RA.sealed_pin_harness(self.ROUTE, worker_type="stage"), "codex")
        self.assertEqual(RA.PIN_TARGETS, ("owner", "frame", "worker"))

    def test_the_pin_is_part_of_the_route_identity_and_the_replacement_harness_is_fixed(self):
        repinned = {"selection_pins": {"owner": {"harness": "claude"}, "worker": {"harness": "codex"}}}
        self.assertNotEqual(route_hash(self.ROUTE), route_hash(repinned))
        self.assertIn("harness", RA.REPLACEMENT_FIXED_KEYS)


def _tuple(parent, child, sandbox="workspace-write", status="supported"):
    return {"parent_harness": parent, "parent_transport": "headless", "parent_sandbox": sandbox,
            "child_harness": child, "launch_authority": "conductor", "status": status,
            "failure_class": "", "probe_source": "fixture", "checked_worktree": "/w",
            "failure_scope": "none", "retry_on_isolated_worktree": 0}


def _chain(tuples):
    same = [row for row in tuples if row["child_harness"] == row["parent_harness"]]
    cross = [row for row in tuples if row["child_harness"] != row["parent_harness"]]
    return [{"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": same},
            {"ordinal": 2, "fallback_hop": "cross-harness-headless", "candidates": cross},
            {"ordinal": 3, "fallback_hop": "native-subagent", "candidates": [], "fleet_visibility": "degraded"},
            {"ordinal": 4, "fallback_hop": "inline", "status": "eligible-after-prior-hop-exhaustion",
             "reason_enum": "runtime-unavailable", "fleet_visibility": "none"}]


class OwnerPinChangeTest(unittest.TestCase):
    """RA-3 (stage 2): the BC route rt-96bab699 shape -- a Codex owner and Codex worker pin
    with only Codex-parent tuples -- moves its next owner to Claude; the sealed route stays."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        tuples = [_tuple("codex", "codex"), _tuple("codex", "claude"), _tuple("codex", "opencode", status="unsupported")]
        self.route = {"route_id": "rt-96bab699717dba8f", "route_hash": "sha256:sealed",
                      "artifact_root": self.temp.name, "cwd": "/w", "effective_intensity": "standard",
                      "dispatch_contract_version": 3,
                      "selection_pins": {"contract_version": 1, "owner": {"harness": "codex", "model": None, "effort": None},
                                         "worker": {"harness": "codex", "model": None, "effort": None}},
                      "dispatch_evidence": {"tuples": tuples, "native_subagent": []},
                      "nodes": [{"id": "test", "dispatch_depth": 2, "fallback_hops": _chain(tuples)}]}
        self.sealed = json.loads(json.dumps(self.route))
        self.probed = [_tuple("claude", "claude", sandbox="bypass"), _tuple("claude", "codex", sandbox="bypass")]

    def change(self, harness="claude", **extra):
        return RA.record_pin_change(self.route, target="owner",
                                    pin={"harness": harness, "model": None, "effort": None},
                                    by={"harness": "codex", "session_id": "codex-sid"}, source="unattributed",
                                    tuples=self.probed if harness == "claude" else [], candidates=[], **extra)

    def test_nothing_recorded_is_the_sealed_route_itself(self):
        self.assertIs(RA.route_in_force(self.route), self.route)
        self.assertEqual(RA.sealed_pin_harness(self.route, worker_type="owner"), "codex")

    def test_same_owner_pin_adds_missing_checked_evidence_once_without_rewriting_route(self):
        self.route["selection_pins"]["owner"]["harness"] = "opencode"
        sealed = json.loads(json.dumps(self.route))
        values = dict(target="owner", pin={"harness": "opencode", "model": None, "effort": None},
                      by={"harness": "claude", "session_id": "parent"}, source="existing-intent",
                      tuples=[_tuple("opencode", "codex")], candidates=[])
        self.assertIsNotNone(RA.record_pin_change(self.route, **values))
        self.assertEqual(self.route, sealed)
        view = RA.route_in_force(self.route)
        self.assertEqual(view["route_hash"], sealed["route_hash"])
        self.assertIn("opencode", {row["parent_harness"] for row in view["dispatch_evidence"]["tuples"]})
        self.assertIsNone(RA.changed_pin_harness(self.route, "owner"))
        self.assertIsNone(RA.moved_owner_harness(self.route, "codex"))
        self.assertIsNone(RA.record_pin_change(self.route, **values))
        self.assertEqual(len(RA.pin_changes(self.route)), 1)

    def test_same_pin_replaces_only_derived_unsupported_tuple_and_hop(self):
        values = dict(target="owner", pin=self.route["selection_pins"]["owner"],
                      by={}, source="checked", tuples=[_tuple("codex", "opencode")], candidates=[])
        self.assertIsNotNone(RA.record_pin_change(self.route, **values))
        view = RA.route_in_force(self.route)
        for rows in (view["dispatch_evidence"]["tuples"], view["nodes"][0]["fallback_hops"][1]["candidates"]):
            self.assertEqual(next(row["status"] for row in rows if row["child_harness"] == "opencode"), "supported")
        self.assertEqual(self.route["dispatch_evidence"]["tuples"][-1]["status"], "unsupported")
        self.assertIsNone(RA.record_pin_change(self.route, **values))

    def test_the_parent_moves_the_owner_and_the_worker_pin_stays(self):
        row = self.change()
        self.assertEqual((row["previous"]["harness"], row["pin"]["harness"], row["source"]), ("codex", "claude", "unattributed"))
        self.assertEqual(self.route, self.sealed)                 # the sealed dict and its hash are untouched
        view = RA.route_in_force(self.route)
        self.assertEqual(view["route_hash"], "sha256:sealed")
        self.assertEqual(RA.sealed_pin_harness(self.route, worker_type="owner"), "claude")
        self.assertEqual(RA.sealed_pin_harness(self.route, worker_type="stage"), "codex")
        self.assertEqual(RA.pinned_launch_harness(self.route, worker_type="owner", requested="codex",
                                                  available=lambda h: True), ("claude", "codex"))
        hops = view["nodes"][0]["fallback_hops"]
        self.assertIn(("claude", "claude"), {(r["parent_harness"], r["child_harness"]) for r in hops[0]["candidates"]})
        self.assertIn(("claude", "codex"), {(r["parent_harness"], r["child_harness"]) for r in hops[1]["candidates"]})
        self.assertEqual(len(view["dispatch_evidence"]["tuples"]), 5)
        self.assertIsNone(self.change())                         # the pin in force already is claude
        self.assertEqual(len(RA.pin_changes(self.route)), 1)
        self.assertEqual(RA.route_in_force(view), view)          # applying twice changes nothing

    def test_a_claude_owner_of_the_changed_route_launches_its_codex_worker(self):
        self.change()
        route_file = Path(self.temp.name) / "route.json"
        route_file.write_text(json.dumps({**self.route, "schema_version": 2}))
        policy = DC.headless_attempt_policy(
            route_file=str(route_file), route_node="test", intensity="standard", harness="codex",
            dispatch_depth=2, parent_slug="owner", execution_surface="registered-headless",
            registered_worker=True, fallback_hop="cross-harness-headless", fallback_ordinal=2,
            parent_harness="claude", parent_transport="headless", parent_sandbox="bypass",
            launch_authority="conductor")
        self.assertEqual(policy["fallback_hop"], "cross-harness-headless")
        owner = _load("route_authority_dispatch_owner", "utilities/dispatch-owner.py")
        self.assertIn("claude", owner._sealed_owner_context(route_file)["harnesses"])

    def isolated_env(self):
        base = Path(self.temp.name)
        return {"HOME": str(base / "home"), "XDG_STATE_HOME": str(base / "xdg"),
                "HARNESS_STATE_ROOT": str(base / "state"), "AGENT_PEER_LEDGER_ROOT": str(base / "peer"),
                "AGENT_DISPATCH_JOBS": str(base / "state" / "jobs.log"), "HERDR_PANE_ID": "wB:p99",
                "PATH": os.environ.get("PATH", "")}

    def start_pin(self, values, *, caller=("codex", "codex-sid"), owner_parent="codex-sid", probed=None):
        router = _load("route_authority_capability_route", "utilities/capability-route.py")
        import work_start
        rows = {"att-owner": ("done", {"dispatch_depth": "1", "worker_type": "owner",
                                       "owner_route_id": self.route["route_id"],
                                       "owner_route_hash": self.route["route_hash"],
                                       "parent_harness": "codex", "parent_sid": owner_parent,
                                       "attempt_id": "att-owner"})}
        probe = {"tuples": [_tuple("codex", "codex")] + (self.probed if probed is None else probed), "candidates": []}
        with mock.patch.dict(os.environ, self.isolated_env(), clear=True), \
             mock.patch.object(RA, "caller_identity", return_value=caller), \
             mock.patch.object(work_start, "_rows", return_value=rows), \
             mock.patch.object(router, "_compose_readiness", return_value=probe) as readiness:
            result = router._change_pins(self.route, Path(self.temp.name) / "jobs.log", values)
        return result, readiness

    def test_start_pin_probes_the_new_owner_and_records_the_change(self):
        result, readiness = self.start_pin(["owner=claude"])
        self.assertTrue(result["changed"])
        self.assertEqual((result["owner"]["harness"], result["previous"]["harness"], result["source"]),
                         ("claude", "codex", "unattributed"))
        self.assertEqual(readiness.call_args.args[2], "claude")          # probed by the runtime, now
        self.assertEqual(sorted(readiness.call_args.args[3]), ["claude", "codex", "opencode"])
        (row,) = RA.pin_changes(self.route)
        self.assertEqual({(t["parent_harness"], t["child_harness"]) for t in row["tuples"]},
                         {("claude", "claude"), ("claude", "codex")})   # only the new owner's rows
        self.assertEqual(row["by"], {"harness": "codex", "session_id": "codex-sid"})
        again, _ = self.start_pin(["owner=claude"])
        self.assertFalse(again["changed"])

    def test_start_pin_refuses_what_it_cannot_do_and_records_nothing(self):
        for values, kwargs, reason in (
                (["owner=claude"], {"caller": ("claude", "another-sid")}, "start-pin-parent-only"),
                (["worker=opencode"], {"probed": [_tuple("codex", "opencode", status="unsupported")]},
                 "start-pin-worker-unready:opencode"),
                (["owner=claude"], {"probed": [_tuple("claude", "codex", status="unsupported")]}, "start-pin-owner-unready:claude")):
            with self.subTest(reason=reason), self.assertRaises(ValueError) as refused:
                self.start_pin(values, **kwargs)
            self.assertIn(reason, str(refused.exception))
        self.assertEqual(RA.pin_changes(self.route), [])

    def test_confirmed_handover_refuses_old_parent_pin_and_allows_successor(self):
        import session_tidy as tidy
        jobs = Path(self.temp.name) / "jobs.log"
        meta = {"attempt_id": "att-owner", "dispatch_depth": "1", "worker_type": "owner",
                "parent_sid": "codex-sid", "parent_harness": "codex",
                "owner_route_id": self.route["route_id"], "owner_route_hash": self.route["route_hash"]}
        jobs.write_text("now\trunning\t/w\t/w\towner\t" +
                        ",".join(f"{key}={value}" for key, value in meta.items()) + "\n")
        seat = tidy.Seat("pane", "fixture-pin-successor", "w9:pSuccessor")
        binding = HANDOVER.binding_of(jobs, meta)
        with mock.patch.dict(os.environ, self.isolated_env(), clear=True):
            tidy.atomic_write_json(HANDOVER._snapshot_path(seat.key), {
                "schema": HANDOVER.SCHEMA,
                "seat": {"kind": "pane", "key": seat.key, "pane": seat.pane},
                "from": {"sid": "codex-sid"}, "bindings": [binding],
            })
            ledger = tidy._ledger_path(seat)
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(json.dumps({"event": "handover", "from": "codex-sid",
                "sid": "successor", "harness": "codex", "ts": 2,
                "bindings": [binding]}) + "\n")
            self.assertEqual(HANDOVER.effective_parent(meta, jobs), "successor")
            with self.assertRaisesRegex(ValueError, "start-pin-parent-only"):
                self.start_pin(["owner=claude"], caller=("codex", "codex-sid"))
            result, _readiness = self.start_pin(["owner=claude"], caller=("codex", "successor"))
        self.assertTrue(result["changed"])
        self.assertEqual(RA.pin_changes(self.route)[-1]["by"],
                         {"harness": "codex", "session_id": "successor"})

    def test_only_the_exact_current_prompt_transfer_sets_peer_source(self):
        router = _load("route_authority_capability_route_source", "utilities/capability-route.py")
        peer = _load("route_authority_peer_message", "utilities/peer-message.py")
        import argparse, session_tidy, time
        with mock.patch.dict(os.environ, self.isolated_env(), clear=True):
            self.assertEqual(router._turn_peer_source("codex", "codex-sid", "/w"), "unattributed")
            sender = {"harness": "claude", "session_id": "peer-sid", "name": "hearting-4d"}
            recipient = {"harness": "codex", "session_id": "codex-sid"}
            body = "[steer] exact current request"
            text, ref = peer.prepare_peer_message(body, sender, recipient, defer=True, refs=["route-current"])
            origin = peer.prompt_origin(text, "codex", "codex-sid")
            self.assertEqual((origin["kind"], origin["transfer_ref"]), ("peer", ref))
            session_tidy.run_hook("codex", "prompt", "codex-sid", cwd="/w", prompt_text=text)
            seat = session_tidy.resolve_seat("codex", "/w", os.environ, "codex-sid", "")
            seq = session_tidy.read_prompt_seq(seat)
            marker = session_tidy.prompt_state(seat)
            self.assertEqual(marker["request_origin"]["transfer_ref"], ref)
            self.assertNotIn(body, json.dumps(marker))
            expected = "peer:hearting-4d·" + ref
            self.assertEqual(router._turn_peer_source("codex", "codex-sid", "/w"), expected)
            with mock.patch.object(peer.time, "gmtime", return_value=time.gmtime(marker["at"])):
                peer.cmd_record(argparse.Namespace(
                    from_harness="opencode", from_session_id="unrelated-sid", from_name="unrelated", from_project="p",
                    to_harness="codex", to_session_id="codex-sid", to_name=None, kind="notice", surface="herdr",
                    status="received", receipt="exact-peer-ref", ref=["f" * 32], transfer_ref=None,
                    body_file=None, body_stdin=False, body_text="herdr steer received"))
            self.assertEqual(router._turn_peer_source("codex", "codex-sid", "/w"), expected)
            session_tidy.run_hook("codex", "prompt", "codex-sid", cwd="/w",
                                  prompt_text="separate user request")
            next_seq = session_tidy.read_prompt_seq(seat)
            self.assertGreater(next_seq, seq)
            self.assertEqual(router._turn_peer_source("codex", "codex-sid", "/w"), "unattributed")
            future = time.gmtime(time.time() + 900)
            with mock.patch.object(peer.time, "gmtime", return_value=future):
                peer.cmd_record(argparse.Namespace(
                    from_harness="claude", from_session_id="late-peer", from_name="late", from_project="p",
                    to_harness="codex", to_session_id="codex-sid", to_name=None, kind="notice", surface="herdr",
                    status="received", receipt="exact-peer-ref", ref=["e" * 32], transfer_ref=None,
                    body_file=None, body_stdin=False, body_text="late notice"))
            self.assertEqual(router._turn_peer_source("codex", "codex-sid", "/w"), "unattributed")
            self.assertEqual(router._turn_peer_source("codex", "other-sid", "/w"), "unattributed")

    def test_only_pin_targets_change_and_other_routes_rows_are_ignored(self):
        with self.assertRaises(ValueError):
            RA.record_pin_change(self.route, target="unit", pin={"harness": "claude"}, by={}, source="x",
                                 tuples=[], candidates=[])
        path = RA.pin_changes_path(self.route)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "route_id": self.route["route_id"], "route_hash": "sha256:other",
                                    "target": "owner", "pin": {"harness": "claude"}}) + "\nnot json\n")
        self.assertIs(RA.route_in_force(self.route), self.route)


class WorkerPinChangeTest(unittest.TestCase):
    """The BC route rt-96bab699 after its owner moved to Claude: the parent now moves the worker
    pin to a stronger Codex model for the remaining checks; the owner pin and the sealed route stay."""

    change = OwnerPinChangeTest.change
    isolated_env = OwnerPinChangeTest.isolated_env
    start_pin = OwnerPinChangeTest.start_pin

    def setUp(self):
        OwnerPinChangeTest.setUp(self)
        self.change()                                            # the owner already moved to claude
        self.route_file = Path(self.temp.name) / "route.json"
        self.route_file.write_text(json.dumps(self.route))
        self.probed = [_tuple("claude", "codex", sandbox="bypass")]

    def test_the_parent_moves_the_worker_model_and_the_next_stage_launch_uses_it(self):
        import model_profile
        before = model_profile.route_selection_pin(self.route_file, worker_type="stage", adapter="codex")
        self.assertEqual(before, {"status": "harness-only"})
        result, readiness = self.start_pin(["worker=codex:gpt-6.1-sol@high"], caller=("claude", "claude-sid"),
                                           owner_parent="claude-sid")
        self.assertTrue(result["changed"])
        self.assertEqual(result["worker"], {"harness": "codex", "model": "gpt-6.1-sol", "effort": "high"})
        self.assertEqual((readiness.call_args.args[2], readiness.call_args.args[3]), ("claude", ["codex"]))
        rows = RA.pin_changes(self.route)
        self.assertEqual([row["target"] for row in rows], ["owner", "worker"])
        self.assertEqual(rows[-1]["previous"], {"harness": "codex", "model": None, "effort": None})
        self.assertEqual(model_profile.route_selection_pin(self.route_file, worker_type="stage", adapter="codex"),
                         {"status": "applied", "model": "gpt-6.1-sol", "effort": "high"})
        self.assertEqual(model_profile.route_selection_pin(self.route_file, worker_type="review", adapter="codex"),
                         {"status": "applied", "model": "gpt-6.1-sol", "effort": "high"})
        self.assertEqual(RA.sealed_pin_harness(self.route, worker_type="owner"), "claude")   # owner unchanged
        self.assertEqual(self.route, json.loads(self.route_file.read_text()))               # sealed route unchanged
        again, _ = self.start_pin(["worker=codex:gpt-6.1-sol@high"], caller=("claude", "claude-sid"),
                                  owner_parent="claude-sid")
        self.assertFalse(again["changed"])

    def test_composing_the_work_again_carries_the_pins_in_force(self):
        import shlex
        import work_start
        self.start_pin(["worker=codex:gpt-6.1-sol@high"], caller=("claude", "claude-sid"), owner_parent="claude-sid")
        argv = shlex.split(work_start._compose_again({**self.route, "capability": "autopilot-code", "slug": "s"}))
        pins = [argv[i + 1] for i, word in enumerate(argv) if word == "--pin"]
        self.assertIn("owner=claude", pins)
        self.assertIn("worker=codex:gpt-6.1-sol@high", pins)

    def test_a_frame_pin_is_probed_from_this_session(self):
        result, readiness = self.start_pin(["frame=claude"], caller=("claude", "claude-sid"),
                                           owner_parent="claude-sid",
                                           probed=[_tuple("claude", "claude", sandbox="bypass")])
        self.assertEqual((readiness.call_args.args[2], readiness.call_args.args[3]), ("claude", ["claude"]))
        self.assertEqual(result["frame"]["harness"], "claude")
        self.assertEqual(RA.changed_pin_harness(self.route, "frame"), "claude")

    def test_a_derived_continuation_launches_on_the_moved_worker_harness(self):
        import stage_session_contract as SSC
        manifest = {"route_file": str(self.route_file)}
        self.assertIsNone(SSC._worker_adapter(manifest))         # never moved: the source adapter stays
        self.start_pin(["worker=claude"], caller=("claude", "claude-sid"), owner_parent="claude-sid",
                       probed=[_tuple("claude", "claude", sandbox="bypass")])
        self.assertEqual(SSC._worker_adapter(manifest), "claude")


class Case3TestRoundTest(unittest.TestCase):
    """Case 3: two test FAILs, one of them only incomplete, spent the standard cap."""

    ROUTE = {"effective_intensity": "standard"}
    NODE = {"id": "test", "kind": "pipeline-stage"}

    def test_every_fail_spends_a_round_whatever_its_reason(self):
        rows = [("done", _fail_row("att-r1")), ("done", _fail_row("att-r2"))]
        budget = RA.round_budget(self.ROUTE, self.NODE, rows)
        self.assertEqual((budget.state, budget.verdict_rounds, budget.cap), ("exhausted", 2, 2))
        self.assertTrue(RA.last_verdict_blocking(rows, "test"))
        self.assertTrue(RA.gate_unmet(rows, "test"))
        # RA-5 (stage 2): an unfinished round ends BLOCKED; it spends no round and leaves
        # the gate unmet, so the phases after it are a gap retry. Two BLOCKED in a row still bind.
        blocked = [("done", {"note": "dead-worker-blocked", "failure_class": "blocked", "worker_type": "stage"})]
        self.assertEqual(RA.round_budget(self.ROUTE, self.NODE, blocked).verdict_rounds, 0)
        self.assertTrue(RA.gate_unmet(blocked, "test"))
        self.assertFalse(RA.gate_unmet(rows + [("done", {"note": "completed-marker", "worker_type": "stage"})], "test"))
        self.assertEqual(RA.round_budget(self.ROUTE, self.NODE, blocked * 2).state, "verdictless-bound")

    def test_a_declared_phase_is_outside_the_full_stage_rounds(self):
        phase = SimpleNamespace(subsession_id="ss-gap-1", subsession_index=1, subsession_count=5,
                                subsession_mode="serial", session_chain_id="ssc-gap-1",
                                phase_brief="brief.md", narrow_verify="true", expected_round_trips=12,
                                stage_authority=0, dispatch_depth=2, route_id="rt-96bab699717dba8f",
                                route_node="test")
        self.assertTrue(RA.declared_subsession(phase))
        self.assertTrue(RA.no_stage_authority({"subsession_id": "ss-gap-1", "stage_authority": "0"}))
        self.assertFalse(RA.no_stage_authority({"note": "dead-worker-fail"}))
        with self.assertRaises(DC.DispatchContractError) as refused:
            RA.declared_subsession(SimpleNamespace(subsession_id="ss-gap-1", stage_authority=0))
        self.assertEqual(refused.exception.reason, "subsession-arguments-incomplete")

    def test_the_three_subsession_marks_stay_distinct(self):
        only_id = {"subsession_id": "ss-1"}
        only_authority = {"stage_authority": "false"}
        both = {"subsession_id": "ss-1", "stage_authority": "0"}
        self.assertEqual([RA.no_stage_authority(m) for m in (only_id, only_authority, both)], [False, True, True])
        self.assertEqual([RA.subsession_row(m) for m in (only_id, only_authority, both)], [True, True, True])
        self.assertEqual([RA.linked_worktree_slice(m) for m in (only_id, only_authority, both)], [False, False, True])
        self.assertEqual([RA.subsession_launch(i, a) for i, a in ((None, 1), ("ss-1", 1), (None, 0))],
                         [False, True, True])


class Case4RetryLinkTest(unittest.TestCase):
    """Case 4: a readable FAIL that inherited a transport retry link exhausted the replacement.

    RA-4 (stage 2): a readable FAIL or BLOCKED never becomes a retry
    predecessor, whatever its round admission; only a transport failure does.
    """

    def test_a_readable_result_is_never_a_retry_predecessor(self):
        latest = _fail_row("att-0c43", automatic_retry_of="att-4cf2")
        self.assertEqual(RA.retry_predecessor([latest]), "")
        blocked = {**latest, "note": "dead-worker-blocked", "failure_class": "blocked"}
        self.assertEqual(RA.retry_predecessor([blocked]), "")
        death = {**latest, "note": "dead-route-completion-rejected", "failure_class": "contract"}
        self.assertEqual(RA.retry_predecessor([death]), "att-0c43")

    def test_new_settled_request_does_not_inherit_a_spent_transport_retry(self):
        import dispatch_replacement as replacement
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            args = SimpleNamespace(attempt_id="att-gap-two", jobs_path=jobs,
                                   worktree=td, replacement_input_argv=["--slug", "gap-two"])
            latest = {"_status": "done", "attempt_id": args.attempt_id,
                      "note": "dead-parent-terminated", "failure_class": "runtime",
                      "automatic_retry_of": "att-gap-one", "launch_outcome": "never-launched"}
            latest.update(DC.parse_registry_metadata(replacement.seal_launch_input(
                args, "codex", "Close migration fixtures and import integration.")))
            before = dict(latest)
            new = "Close historical/current conflict and actual DB parity."
            self.assertEqual(RA.retry_predecessor([latest], jobs=jobs, task=new), "")
            for task in (None, "", "Close migration fixtures and import integration.",
                         "  Close migration fixtures\n and import integration.  "):
                with self.subTest(task=task):
                    self.assertEqual(RA.retry_predecessor([latest], jobs=jobs, task=task), args.attempt_id)
            self.assertFalse(replacement._budget_exhausted(jobs, latest))
            self.assertEqual(latest, before)
            candidate = {**latest, "attempt_id": "att-gap-three"}
            candidate.pop("automatic_retry_of")
            self.assertIsNone(replacement.admission(jobs, [], candidate))
            # Same-task transport continuation retains lineage without a count cutoff.
            self.assertIsNone(replacement.admission(jobs, ["now\tdone\t/repo\t/wt\tgap-two\t" +
                ",".join(f"{k}={v}" for k, v in latest.items() if not k.startswith("_"))],
                {**candidate, "automatic_retry_of": args.attempt_id}))
            for state in ("live", "unverifiable"):
                with mock.patch.object(DC, "attempt_process_quiescence",
                                       return_value=SimpleNamespace(state=state)):
                    self.assertEqual(RA.retry_predecessor([latest], jobs=jobs, task=new), args.attempt_id)
            conflicted = {**latest, "terminal_conflict": "pending"}
            with mock.patch.object(DC, "terminal_conflict_pending", return_value=True):
                self.assertEqual(RA.retry_predecessor([conflicted], jobs=jobs, task=new), args.attempt_id)
            replacement._directory(jobs).joinpath("inputs", args.attempt_id + ".json").write_text("{}")
            self.assertEqual(RA.retry_predecessor([latest], jobs=jobs, task=new), args.attempt_id)

    def test_changed_request_after_success_is_new_work_but_same_request_is_not(self):
        import dispatch_replacement as replacement
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            attempt_id = "att-success"
            args = SimpleNamespace(attempt_id=attempt_id, jobs_path=jobs, worktree=td,
                                   replacement_input_argv=["--slug", "execute"])
            old_task = "Close migration fixtures and import integration."
            row = {"_status": "done", "attempt_id": attempt_id,
                   "note": "completed-marker", "failure_class": "pass",
                   "launch_outcome": "never-launched"}
            row.update(DC.parse_registry_metadata(
                replacement.seal_launch_input(args, "codex", old_task)))
            changed = "Close historical/current conflict and actual DB parity."
            with mock.patch.object(DC, "attempt_process_quiescence",
                                   return_value=SimpleNamespace(state="quiescent")):
                self.assertTrue(RA.new_owner_request(row, jobs=jobs, task=changed))
                self.assertFalse(RA.new_owner_request(row, jobs=jobs, task=old_task))
                self.assertFalse(RA.new_owner_request(row, jobs=jobs, task="  Close migration\n fixtures and import integration.  "))
                self.assertFalse(RA.new_owner_request(row, jobs=jobs, task=None))
                self.assertEqual(RA.retry_predecessor([row], jobs=jobs, task=changed), "")
                for task in (None, old_task, "  Close migration\n fixtures and import integration.  "):
                    self.assertEqual(RA.retry_predecessor([row], jobs=jobs, task=task), "")
                self.assertFalse(RA.new_owner_request({**row, "terminal_conflict": "1"},
                                                      jobs=jobs, task=changed))
            for state in ("live", "unverifiable"):
                with mock.patch.object(DC, "attempt_process_quiescence",
                                       return_value=SimpleNamespace(state=state)):
                    self.assertFalse(RA.new_owner_request(row, jobs=jobs, task=changed))

    def test_changed_task_cannot_skip_an_open_unlaunched_transport_successor(self):
        latest = {"_status": "open", "launch_claimed": "0", "attempt_id": "att-gap-two",
                  "automatic_retry_of": "att-gap-one"}
        self.assertEqual(RA.retry_predecessor([latest], jobs="unused", task="new request"), "att-gap-one")

    def test_the_attempt_policy_retries_only_transport_failures(self):
        from dispatch_attempt_policy import decide_attempt
        def retry(note, failure_class):
            return decide_attempt("done", {"note": note, "failure_class": failure_class},
                                  process_state="quiescent").retry_kind
        self.assertEqual(retry("dead-worker-fail", "fail"), "")
        self.assertEqual(retry("dead-worker-blocked", "blocked"), "")
        self.assertEqual(retry("dead-exact-pid", "contract"), "fallback")
        self.assertEqual(retry("dead-invalid-envelope", "invalid-envelope"), "fallback")
        self.assertEqual(retry("dead-capacity", "capacity"), "capacity")

    def test_a_next_link_still_requires_the_predecessor_binding(self):
        source = ("2026-10-06T08:00:00Z\tdone\t/repo\t/wt\ttest-r2\t"
                  "attempt_id=att-0c43,automatic_retry_of=att-4cf2,route_id=rt-f22b0e181b82b4a3,route_node=test")
        with self.assertRaises(DC.DispatchContractError) as refused:
            DC._automatic_retry_admission([source], {"attempt_id": "att-next", "automatic_retry_of": "att-0c43"})
        self.assertEqual(refused.exception.reason, "retry-predecessor-binding-mismatch")


class Case5ReadRootsTest(unittest.TestCase):
    """Case 5: one read-only request is readable and unwritten on every harness (RA-7, stage 3);
    the grant records each runtime's guarantee."""

    def test_read_roots_are_projected_on_every_runtime(self):
        request = ExecutionAccessRequest(
            writable_roots=(), read_roots=(Path("/nas/records"),), network_required=False,
            network_reason="", network_hosts=(), enforcement_required="any", justification=(),
            request_sha256="0" * 64, source_path=Path("/tmp/request.json"))
        for runtime, grade in (("claude-cli", "tool-permission"), ("codex-exec", "os-sandbox"),
                               ("opencode", "tool-permission")):
            with self.subTest(runtime=runtime):
                grant = RA.access_grant(request, runtime=runtime)
                self.assertEqual(grant.read_roots, (Path("/nas/records"),))
                self.assertEqual(grant.unwritable_read_roots, (Path("/nas/records"),))
                self.assertEqual((grant.read_enforcement, grant.unmet), (grade, ()))

    def test_cross_harness_recovery_accepts_installed_source_reads_only(self):
        wrapper = _load("owner_read_scope_opencode", "adapters/opencode/bin/dispatch-headless.py")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree, artifact, install = root / "worktree", root / "artifact", root / "install"
            for path in (worktree, artifact, install / "core", install / "utilities"):
                path.mkdir(parents=True)
            history = {"harness": "codex", "worktree": str(worktree),
                       "jobs": str(root / "state/jobs.log"), "applied_permissions": {}}
            route = {"cwd": str(worktree), "artifact_root": str(artifact), "work_request": {"text": ""}}
            for version in (None, "1.19.0", "2.0.32"):
                with self.subTest(version=version), mock.patch.dict(os.environ, {}):
                    if version is None:
                        os.environ.pop("OPENCODE_RUNTIME_VERSION", None)
                        opencode_runtime.runtime_version.cache_clear()
                        source_v2 = opencode_runtime.is_v2()
                    else:
                        os.environ["OPENCODE_RUNTIME_VERSION"] = version
                        source_v2 = version.startswith("2.")
                    source = ({"permissions": [], "agents": {}} if source_v2 else {})
                    os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps(source)
                    config = json.loads(wrapper.scoped_external_directory_config(
                        str(artifact), agent_home=install, worktree=str(worktree), selected_agent="build"))
                    if "permissions" in config:
                        self.assertNotIn("permission", config)
                        self.assertNotIn("agent", config)
                        self.assertIn("agents", config)
                    recorded = DR._applied_permissions(SimpleNamespace(
                        opencode_config_content=json.dumps(config)))
                    candidate = {"harness": "opencode", "launch_home": str(install),
                                 "applied_permissions": recorded}
                    self.assertTrue(opencode_runtime.opencode_permission_ordered_view(
                        recorded.get("opencode_permission")))
                    RA.require_recovery_grant_addresses(candidate, history, route=route)

                    widened = json.loads(json.dumps(config))
                    if "permissions" in widened:
                        widened["permissions"].append({
                            "action": "edit", "resource": f"{install / 'utilities'}/**",
                            "effect": "allow"})
                    else:
                        widened["permission"].setdefault("edit", {}).pop(
                            f"{install / 'utilities'}/**", None)
                        widened["permission"]["edit"][f"{install / 'utilities'}/**"] = "allow"
                    candidate["applied_permissions"] = DR._applied_permissions(SimpleNamespace(
                        opencode_config_content=json.dumps(widened)))
                    with self.assertRaises(DC.DispatchContractError) as refused:
                        RA.require_recovery_grant_addresses(candidate, history, route=route)
                    self.assertEqual(refused.exception.reason, "replacement-input-tuple-mismatch")

                    outside = json.loads(json.dumps(config))
                    if "permissions" in outside:
                        outside["permissions"].append({
                            "action": "external_directory", "resource": str(root / "outside/**"),
                            "effect": "allow"})
                    else:
                        outside["permission"].setdefault("external_directory", {})[
                            str(root / "outside/**")] = "allow"
                    candidate["applied_permissions"] = DR._applied_permissions(SimpleNamespace(
                        opencode_config_content=json.dumps(outside)))
                    with self.assertRaises(DC.DispatchContractError) as refused:
                        RA.require_recovery_grant_addresses(candidate, history, route=route)
                    self.assertEqual(refused.exception.reason, "replacement-input-tuple-mismatch")
            opencode_runtime.runtime_version.cache_clear()

    def test_missing_null_and_empty_opencode_permissions_have_no_recovery_authority(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree, artifact, install = root / "worktree", root / "artifact", root / "install"
            worktree.mkdir(); artifact.mkdir(); install.mkdir()
            history = {"harness": "codex", "worktree": str(worktree),
                       "jobs": str(root / "state/jobs.log"), "applied_permissions": {}}
            route = {"cwd": str(worktree), "artifact_root": str(artifact),
                     "work_request": {"text": ""}}
            for applied in ({}, {"opencode_permission": None},
                            {"opencode_permission": {}}, {"opencode_permission": []}):
                candidate = {"harness": "opencode", "launch_home": str(install),
                             "applied_permissions": applied}
                with self.subTest(applied=applied), self.assertRaises(DC.DispatchContractError):
                    RA.require_recovery_grant_addresses(candidate, history, route=route)

    def test_selected_agent_recovery_grants_use_global_then_local_policy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree, artifact = root / "worktree", root / "artifact"
            install, readonly = root / "install", root / "records"
            for path in (worktree, artifact, install / "core", install / "utilities", readonly):
                path.mkdir(parents=True, exist_ok=True)
            history = {
                "harness": "codex", "worktree": str(worktree),
                "jobs": str(root / "state" / "jobs.log"),
                "applied_permissions": {"execution_access": {
                    "writable_roots": [], "read_roots": [str(readonly)],
                    "network": "not-requested",
                }},
            }
            route = {"cwd": str(worktree), "artifact_root": str(artifact),
                     "work_request": {"text": ""}}
            global_policy = {
                "external_directory": {
                    "*": "deny", str(worktree) + "/**": "allow",
                    str(artifact) + "/**": "allow", str(readonly) + "/**": "allow",
                },
                "edit": {
                    "*": "deny", str(worktree) + "/**": "allow",
                    str(readonly) + "/**": "deny",
                },
            }

            def recorded(local_policy):
                config = {"permission": global_policy,
                          "agent": {"build": {"permission": local_policy}}}
                applied = DR._applied_permissions(SimpleNamespace(
                    opencode_config_content=json.dumps(config), agent="build"))
                return {"harness": "opencode", "launch_home": str(install),
                        "applied_permissions": applied}

            RA.require_recovery_grant_addresses(recorded({}), history, route=route)
            outside = str(root / "outside") + "/**"
            cases = {
                "selected-edit-outside": {"edit": {outside: "allow"}},
                "selected-external-directory-outside": {
                    "external_directory": {"*": "deny", outside: "allow"}},
                # This later selected path rule overrides the global
                # read-only deny for the same resource.
                "selected-edit-writes-read-only-root": {
                    "edit": {str(readonly) + "/**": "allow"}},
            }
            for name, local_policy in cases.items():
                with self.subTest(case=name), self.assertRaises(DC.DispatchContractError):
                    RA.require_recovery_grant_addresses(
                        recorded(local_policy), history, route=route)


class Case6UncappedAndEnvelopeTest(unittest.TestCase):
    """Case 6: an uncapped execute FAIL; envelopes are matched exactly.

    RA-4 (stage 2): the execute FAIL no longer becomes a retry predecessor, so
    the owner's next execute is new work instead of an exhausted replacement.
    """

    def test_an_uncapped_fail_is_not_a_retry_predecessor(self):
        execute = {"id": "execute", "kind": "pipeline-stage"}
        self.assertFalse(RA.is_round_capped_node(execute))
        self.assertEqual(RA.retry_predecessor([_fail_row("att-2358")]), "")

    def test_a_pass_with_an_explained_none_blocker_is_a_pass(self):
        # RA-8 (stage 2): the note is kept as a remark; other blocker text still breaks PASS.
        text = "artifact: /cycle/test_logs/envelope.md\nverdict: PASS\nblocker: none (범위 한정 PASS)"
        handoff = RA.HANDOFF_RE.search(text).groupdict()
        self.assertEqual(handoff["blocker"], "none (범위 한정 PASS)")
        self.assertIsNone(RA.pass_blocker_violation(handoff["verdict"], handoff["blocker"]))
        self.assertEqual(RA.pass_blocker_note(handoff["blocker"]), "범위 한정 PASS")
        self.assertEqual(RA.pass_blocker_note("none"), "")
        self.assertEqual(RA.pass_blocker_violation("PASS", "G1 skipped"), "pass-blocker-not-none")
        self.assertIsNone(RA.pass_blocker_violation("PASS", "none"))
        self.assertIsNone(RA.pass_blocker_violation("FAIL", "G1 incomplete"))
        self.assertIsNone(RA.HANDOFF_RE.search("verdict: PASS\nblocker: none"))

    def test_a_fence_or_two_trailing_sentences_leave_the_verdict(self):
        # G1: the envelope plus a code fence and/or up to two sentences is
        # still that envelope. The captured fields never include the tail.
        base = "artifact: -\nverdict: {verdict}\nblocker: {blocker}"
        tails = [
            "",
            "\n",
            "\nAll done.",
            "\nAll done.\nClosing out.",
            "\n```log\nsome output\n```",
            "\n```log\nsome output\n```\nAll done.",
            "\n```log\nsome output\n```\nAll done.\nClosing out.",
            "\n\nAll done.",
        ]
        for verdict, blocker in (("PASS", "none"), ("FAIL", "real"), ("BLOCKED", "waiting")):
            for tail in tails:
                with self.subTest(verdict=verdict, tail=tail[:20]):
                    handoff = RA.HANDOFF_RE.search(base.format(verdict=verdict, blocker=blocker) + tail)
                    self.assertIsNotNone(handoff)
                    self.assertEqual(
                        handoff.groupdict(),
                        {"artifact": "-", "verdict": verdict, "blocker": blocker},
                    )
        # Three sentences, a verdict-mentioning tail, or no envelope still fail closed.
        for text in (
            "artifact: -\nverdict: PASS\nblocker: none\nOne.\nTwo.\nThree.",
            "artifact: -\nverdict: PASS\nblocker: none\nverdict: FAIL",
            "artifact: -\nverdict: PASS\nblocker: none\nVerdict: FAIL",
            "artifact: -\nverdict: PASS\nblocker: none\n## 평결: FAIL",
            "artifact: -\nverdict: PASS\nblocker: none\nThe verdict: FAIL.",
            "artifact: -\nverdict: PASS\nblocker: none\nFinal verdict: FAIL",
            "artifact: -\nverdict: PASS\nblocker: none\n`verdict: FAIL`",
            "artifact: -\nverdict: PASS\nblocker: none\n> verdict: FAIL",
            "artifact: -\nverdict: PASS\nblocker: none\nThe verdict: all good",
            "artifact: -\nverdict: PASS\nblocker: none\nAll tests PASS.",
            "artifact: -\nverdict: PASS\nblocker: none\nartifact: -",
            "artifact: -\nverdict: PASS\nblocker: none\n```\nverdict: FAIL\n```",
            "see artifact: -\nverdict: PASS\nblocker: none",
        ):
            with self.subTest(text=text[:40]):
                self.assertIsNone(RA.HANDOFF_RE.search(text))
        # A sentence that names no verdict stays a sentence.
        for tail in ("\nAll tests pass.", "\nDONE.", "\n검증을 마쳤습니다."):
            with self.subTest(tail=tail):
                mentioned = RA.HANDOFF_RE.search(
                    "artifact: -\nverdict: PASS\nblocker: none" + tail
                )
                self.assertEqual(mentioned.group("verdict"), "PASS")
        # Two envelopes still resolve to the last one.
        last = RA.HANDOFF_RE.search(
            "artifact: -\nverdict: PASS\nblocker: none\n\nartifact: -\nverdict: FAIL\nblocker: real"
        )
        self.assertEqual(last.group("verdict"), "FAIL")

    def test_an_envelope_inside_a_fence_never_hides_the_later_one(self):
        # PR #281 review round 1: a fenced later envelope must not be swallowed
        # by an earlier decoy; the later block is the one read (or the tail
        # fails closed when only a field line sits in the fence).
        decoy = ("Contract example:\nartifact: <path>\nverdict: PASS\nblocker: none\n"
                 "```\nartifact: /r.md\nverdict: FAIL\nblocker: 3 tests fail\n```")
        handoff = RA.HANDOFF_RE.search(decoy)
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.group("verdict"), "FAIL")
        self.assertEqual(handoff.group("artifact"), "/r.md")

    def test_a_long_failing_tail_stays_linear(self):
        # PR #281 review rounds 1-3: optional newlines between tail elements
        # made the failing search cubic (4k chars did not finish in 250s), and
        # a long indented line re-scanned the space run per giveback
        # (80k spaces: 2.7s). Newline-only gaps keep both cases linear.
        text = "artifact: -\nverdict: BLOCKED\nblocker: " + "a" * 100_000 + "\nx\ny\nz"
        start = time.monotonic()
        self.assertIsNone(RA.HANDOFF_RE.search(text))
        self.assertLess(time.monotonic() - start, 5.0)
        spaces = "artifact: -\nverdict: BLOCKED\nblocker: b\n" + " " * 200_000 + "verdict: FAIL"
        start = time.monotonic()
        self.assertIsNone(RA.HANDOFF_RE.search(spaces))
        self.assertLess(time.monotonic() - start, 5.0)


class DeathPatternTest(unittest.TestCase):
    """Audit #9: one death/limit table for every harness, at launch and in liveness."""

    def test_every_reader_uses_the_one_table(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                wrapper = _load(f"route_authority_wrapper_{harness}", f"adapters/{harness}/bin/dispatch-headless.py")
                self.assertIs(wrapper.DEATH_PATTERNS, RA.DEATH_PATTERNS)
                self.assertIs(wrapper.scan_anchored_death, RA.scan_anchored_death)
        self.assertIs(DC.anchored_capacity_failure, RA.anchored_capacity_failure)
        for path in ("adapters/codex/bin/dispatch-liveness.py", "adapters/opencode/bin/dispatch-liveness.py",
                     "utilities/dispatch-liveness.sh"):
            text = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn("scan_anchored_death", text, path)
            self.assertNotIn("LIMIT_RE", text, path)

    def test_lines_that_split_by_harness_now_classify_alike(self):
        # Claude's wrapper missed the first two; OpenCode's liveness missed the third.
        cases = {"usage_limit_reached": "usage-limit",
                 "error: exceeded retry limit, last status: 429 Too Many Requests": "usage-limit",
                 "network is unreachable": "network-operation-not-permitted",
                 "permission requested: bash; auto-rejecting": "permission-reject",
                 "Selected model is at capacity": "capacity"}
        for line, label in cases.items():
            with self.subTest(line=line):
                self.assertEqual(RA.scan_anchored_death(line), (label, ""))
        self.assertEqual(RA.scan_death("You've hit your session limit · resets 3pm"), ("session-limit", "3pm"))
        for prose in ("This report discusses rate limits at length " + "x" * 200,
                      "the model is at capacity per an earlier note, continuing",
                      "req_429abc finished"):
            with self.subTest(prose=prose[:30]):
                self.assertIsNone(RA.scan_anchored_death(prose))


class OneHomeTest(unittest.TestCase):
    """The old names are this module's judgments, not copies."""

    def test_old_names_resolve_to_route_authority(self):
        import dispatch_parent_completion as P
        import model_profile as MP
        import stage_session_runtime as SSR
        import codex_dispatch_terminal as CDT
        import dispatch_supervisor_terminal as DST
        import route_plan as RP
        F = _load("route_authority_fallback", "utilities/stage-dispatch-fallback.py")
        CR = _load("route_authority_capability_route", "utilities/capability-route.py")
        pairs = [
            (P.interactive_parent_identity, RA.caller_identity),
            (P.default_parent_session_id, RA.default_parent_session_id),
            (P.default_parent_harness, RA.default_parent_harness),
            (DC.is_linked_worktree_slice, RA.linked_worktree_slice),
            (MP.pin_target, RA.pin_target),
            (MP.sealed_pin_harness, RA.sealed_pin_harness),
            (MP.pinned_launch_harness, RA.pinned_launch_harness),
            (SSR.declared, RA.declared_subsession),
            (F.retry_predecessor, RA.retry_predecessor),
            (CDT._HANDOFF_RE, RA.HANDOFF_RE),
            (DST._HANDOFF_RE, RA.HANDOFF_RE),
            (RP._PIN_TARGETS, RA.PIN_TARGETS),
            (CR.SELECTION_PIN_TARGETS, RA.PIN_TARGETS),
            (CR._LIVE_ROW_STATUSES, RA.LIVE_ROW_STATUSES),
        ]
        for old, new in pairs:
            self.assertIs(old, new)



class WrapperAdmissionTest(unittest.TestCase):
    """RA-11 (stage 3): the three wrappers' completion/preview gate and access binding are
    route_authority's, and they read the same registry before the claim."""

    WRAPPERS = ("claude", "codex", "opencode")

    def test_the_three_wrappers_call_the_one_admission(self):
        # validate_route_record is shared in dispatch_wrapper_common now: the
        # admission's gate call lives there, once, plus the main() call in
        # each wrapper (PR #299 review).
        common = (ROOT / "utilities" / "dispatch_wrapper_common.py").read_text(encoding="utf-8")
        self.assertEqual(common.count("route_authority.completion_gate("), 1)
        for harness in self.WRAPPERS:
            with self.subTest(harness=harness):
                wrapper = _load(f"route_authority_{harness}_wrapper", f"adapters/{harness}/bin/dispatch-headless.py")
                self.assertIs(wrapper.completion_gate_fail_fields, RA.completion_gate_fail_fields)
                source = (ROOT / f"adapters/{harness}/bin/dispatch-headless.py").read_text(encoding="utf-8")
                self.assertEqual(source.count("route_authority.completion_gate("), 1)
                self.assertEqual(source.count("route_authority.bind_launch_access("), 1)
                self.assertNotIn("load_parent_effective_grant(", source)
                self.assertNotIn("recover_preview_gate_after_refusal(", source)

    def test_a_refused_gate_comes_back_with_its_preview_recovery_and_fields(self):
        args = SimpleNamespace(route_file="/r/route.json", route_node="test", attempt_id="att-x",
                               jobs=None, agent_home=ROOT)
        seen = []
        def refusing(*a, **k):
            seen.append(k["planned_revision_nodes"])
            raise DC.DispatchContractError("human-gate-not-raised", "preview-disposition")
        with mock.patch("review_input.preview_request_nodes", return_value=("plan",)), \
             mock.patch.object(DC, "recover_preview_gate_after_refusal", return_value="preview_gate_recovery=unavailable"):
            reason, code, fields = RA.completion_gate(args, "start", ROOT, Path("/j/jobs.log"), gate=refusing,
                                                      before=(lambda: seen.append("before"),))
        self.assertEqual(seen, ["before", ("plan",)])
        self.assertEqual((reason, code, fields["detail"], fields["child_spawned"]),
                         ("human-gate-not-raised", 65, "preview_gate_recovery=unavailable", "0"))
        self.assertIsNone(RA.completion_gate(args, "start", ROOT, Path("/j/jobs.log"), gate=lambda *a, **k: None))

    def test_the_registry_before_the_claim_is_one_rule(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": "/inherited/jobs.log"}):
            self.assertEqual(RA.prelaunch_registry(SimpleNamespace(jobs="/explicit/jobs.log", agent_home=ROOT)),
                             Path("/explicit/jobs.log"))
            self.assertEqual(RA.prelaunch_registry(SimpleNamespace(jobs=None, agent_home=ROOT)),
                             Path("/inherited/jobs.log"))

    def test_a_child_access_request_needs_its_exact_parent(self):
        from execution_access import ExecutionAccessError
        with tempfile.TemporaryDirectory() as td:
            request = Path(td) / "request.json"
            request.write_text("{}")
            args = SimpleNamespace(worktree=td, artifact_root=td, jobs_path=Path(td) / "jobs.log", agent_home=ROOT,
                                   dispatch_depth=2, execution_access_file=str(request), parent_binding=None)
            with self.assertRaises(ExecutionAccessError) as refused:
                RA.bind_launch_access(args, runtime="opencode", default_roots=())
            self.assertEqual(refused.exception.reason, "execution-access-exceeds-parent:parent-grant-unknown")
            args.execution_access_file = None
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AGENT_DISPATCH_EXECUTION_ACCESS_FILE", None)
                self.assertIsNone(RA.bind_launch_access(args, runtime="opencode", default_roots=()))


class ComputeLaunchAccessTest(unittest.TestCase):
    def test_borrowed_owner_and_compute_child_keep_defaults_noncompute_child_filters_them(self):
        from execution_access import (
            COMPUTE_INVENTORY_REASON, COMPUTE_NETWORK_REASON, ParentGrant,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree, artifacts, state = root / "worktree", root / "artifacts", root / "state"
            worktree.mkdir(); artifacts.mkdir(); state.mkdir()
            run_root, inventory_root = root / "runs", root / "inventory"
            run_root.mkdir(); inventory_root.mkdir()
            request_path = root / "request.json"
            request_path.write_text(json.dumps({
                "schema_version": 1,
                "writable_roots": [str(run_root)],
                "read_roots": [str(inventory_root)],
                "network": {"required": True, "reason": COMPUTE_NETWORK_REASON, "hosts": []},
                "enforcement_required": "any",
                "justification": {str(run_root): "Compute-hosts inventory run_root for lab resource work",
                                  str(inventory_root): COMPUTE_INVENTORY_REASON},
            }))
            route = {"route_id": "rt-borrowed-access", "route_hash": "sha256:" + "a" * 64,
                     "capability": "autopilot-code", "cwd": str(worktree),
                     "artifact_root": str(artifacts), "nodes": [
                         {"id": "lab-run", "part": "autopilot-lab:full-run"},
                         {"id": "test"},
                     ]}
            route_file = root / "route.json"
            route_file.write_text(json.dumps(route))
            parent = ParentGrant((run_root,), (inventory_root,), network_allowed=True,
                                 attempt_id="att-parent", runtime="opencode")
            results = {}
            for node, depth in ((None, 1), ("lab-run", 2), ("test", 2)):
                args = SimpleNamespace(
                    worktree=str(worktree), artifact_root=str(artifacts), jobs_path=state / "jobs.log",
                    agent_home=ROOT, dispatch_depth=depth, execution_access_file=str(request_path),
                    parent_binding=SimpleNamespace(attempt_id="att-parent") if depth == 2 else None,
                    route_file=str(route_file), route_node=node, owner_route_binding=None,
                )
                patchers = [mock.patch("execution_access.load_parent_effective_grant", return_value=parent)]
                with patchers[0]:
                    results[node or "owner"] = RA.bind_launch_access(
                        args, runtime="opencode", default_roots=(), network_available=True)
            self.assertEqual(results["owner"].read_roots, (inventory_root,))
            self.assertEqual(results["lab-run"].read_roots, (inventory_root,))
            self.assertEqual(results["owner"].network, "granted-unenforced")
            self.assertEqual(results["lab-run"].network, "granted-unenforced")
            self.assertEqual(results["test"].read_roots, ())
            self.assertEqual(results["test"].network, "not-requested")

class SealedContractReadTest(unittest.TestCase):
    """An activated replacement still reads the route's original contracts."""

    @classmethod
    def setUpClass(cls):
        cls.opencode = _load("sealed_contract_opencode", "adapters/opencode/bin/dispatch-headless.py")
        cls.claude = _load("sealed_contract_claude", "adapters/claude/bin/dispatch-headless.py")
        cls.distribution = _load("sealed_contract_distribution", "tools/install/distribution.py")

    def test_owner_and_child_without_access_request_keep_the_prune_pin_readable(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {
                "OPENCODE_CONFIG_CONTENT": ""}):
            os.environ.pop("AGENT_DISPATCH_EXECUTION_ACCESS_FILE", None)
            root = Path(td)
            old, new = root / "releases/v1", root / "releases/v2"
            for home in (old, new):
                for name in ("capabilities", "core", "utilities"):
                    (home / name).mkdir(parents=True)
                (home / "capabilities/autopilot-lab.md").write_text("lab contract")
            route = {"route_id": "rt-contract", "launch_compatibility_tuple": {
                "launch_home": {"path": str(old)}}}
            route_file = root / "route.json"
            route_file.write_text(json.dumps(route))
            # This is the existing install pin, with no new retention input.
            pin = self.distribution._route_record_launch_home(route_file, {}, {})
            self.assertEqual(pin, str(old))
            for depth in (1, 2):
                args = SimpleNamespace(worktree=str(root / "work"), artifact_root=str(root / "reports"),
                    jobs_path=root / "state/jobs.log", agent_home=new, dispatch_depth=depth,
                    execution_access_file=None, parent_binding=None,
                    route_file=str(route_file) if depth == 2 else None,
                    owner_route_binding=SimpleNamespace(route_file=str(route_file)) if depth == 1 else None)
                grant = RA.bind_launch_access(args, runtime="opencode", default_roots=())
                self.assertIsNone(grant)  # Contract visibility requires no access request.
                self.assertIn(Path(pin) / "capabilities", args.contract_read_roots)
                config = json.loads(self.opencode.scoped_external_directory_config(
                    args.artifact_root, agent_home=new, worktree=args.worktree,
                    contract_read_roots=args.contract_read_roots, selected_agent="build"))
                legacy = (opencode_runtime.opencode_v2_to_legacy_config(config)
                          if "permissions" in config else config)
                recorded = DR._applied_permissions(SimpleNamespace(
                    opencode_config_content=json.dumps(config)))
                global_rules = recorded["opencode_permission"]
                self.assertTrue(opencode_runtime.opencode_permission_ordered_view(global_rules))
                if "permissions" in config:
                    self.assertNotIn("permission", config)
                    self.assertNotIn("agent", config)
                    self.assertIn("agents", config)
                for rules in (global_rules, legacy["agent"]["build"]["permission"]):
                    for home in (old, new):
                        for name in ("capabilities", "core", "utilities"):
                            self.assertEqual(rules["external_directory"][str(home / name) + "/**"], "allow")
                            self.assertEqual(rules["edit"][str(home / name) + "/**"], "deny")
                    self.assertEqual(rules["external_directory"].get(
                        "*", global_rules["external_directory"].get("*", "deny")), "deny")
                    self.assertNotIn(str(old) + "/**", rules["external_directory"])
                args.execution_access_grant = None
                read_dirs, denies = self.claude._read_only_projection(args)
                for home in (old, new):
                    for name in ("capabilities", "core", "utilities"):
                        self.assertIn(str(home / name), read_dirs)
                        self.assertIn(f"Edit(//{str(home / name).lstrip('/')}/**)", denies)

    def test_read_projection_preserves_the_approved_development_worktree(self):
        args = SimpleNamespace(worktree=str(ROOT), execution_access_grant=None,
                               contract_read_roots=RA.contract_read_roots(ROOT))
        self.assertEqual(self.claude._read_only_projection(args), ([], []))

    def test_same_install_contract_permissions_compare_equal_but_another_checkout_does_not(self):
        old, new = "/opt/hearting/releases/v1", "/opt/hearting/releases/v2"
        worktree = "/work/project"
        observations = []
        for version in (None, "1.19.0", "2.0.32"):
            with self.subTest(version=version), mock.patch.dict(os.environ, {}):
                if version is None:
                    os.environ.pop("OPENCODE_RUNTIME_VERSION", None)
                    opencode_runtime.runtime_version.cache_clear()
                    source_v2 = opencode_runtime.is_v2()
                else:
                    os.environ["OPENCODE_RUNTIME_VERSION"] = version
                    source_v2 = version.startswith("2.")
                source = {"permissions": [], "agents": {}} if source_v2 else {}
                os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps(source)

                def permissions(home, route=None):
                    content = self.opencode.scoped_external_directory_config(
                        "/reports", agent_home=Path(home),
                        contract_read_roots=RA.contract_read_roots(home, route), worktree=worktree)
                    config = json.loads(content)
                    if "permissions" in config:
                        self.assertNotIn("permission", config)
                        self.assertNotIn("agent", config)
                    applied = DR._applied_permissions(SimpleNamespace(opencode_config_content=content))
                    self.assertTrue(opencode_runtime.opencode_permission_ordered_view(
                        applied.get("opencode_permission")))
                    return applied

                before = permissions(old)
                after = permissions(new, {"launch_compatibility_tuple": {"launch_home": {"path": old}}})
                equal_before = RA.granted_permissions(before, old, worktree)
                equal_after = RA.granted_permissions(after, new, worktree)
                self.assertEqual(equal_before, equal_after)
                other = permissions(new, {"launch_compatibility_tuple": {"launch_home": {"path": "/work/dev"}}})
                self.assertNotEqual(equal_before, RA.granted_permissions(other, new, worktree))
                observations.append(equal_before)
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(observations[1], observations[2])
        opencode_runtime.runtime_version.cache_clear()


class ReleaseAxisTest(unittest.TestCase):
    """Launch roots that differ only because the installed release moved, from one managed
    release to another verified one, are where the launch runs, not the work."""

    OLD, NEW = "/opt/hearting/releases/v1", "/opt/hearting/releases/v2"

    @staticmethod
    def identity(kind, path, release):
        return {"kind": kind, "path": path, "release_id": release,
                "content_digest": "sha256:c" + release, "binding_digest": "sha256:b" + release}

    def roots(self, top, release, *, registry=None, jobs="/state/dispatch/jobs.log"):
        return {"runtime_root": self.identity("runtime_root", top, release),
                "launch_home": self.identity("launch_home", top, release),
                "wrapper_root": self.identity("wrapper_root", top + "/adapters", release),
                "registry_root": self.identity("registry_root", registry or top, release),
                "jobs_path": self.identity("jobs_path", jobs, release),
                "grounding_roots.cwd": self.identity("grounding_roots.cwd", "/work", "abc")}

    def moved(self, sealed, current, verified=None):
        return RA.release_moved(sealed, current, managed_release=lambda path: path == (verified or self.NEW))

    def test_a_managed_release_move_relieves_the_release_roots_and_the_registry_s_release(self):
        sealed = self.roots(self.OLD, "release:v1:" + "a" * 12)
        current = self.roots(self.NEW, "release:v2:" + "b" * 12)
        self.assertEqual(self.moved(sealed, current), RA.RELEASE_LAUNCH_ROOTS | {"jobs_path"})

    def test_a_registry_or_work_root_that_moved_is_never_relieved(self):
        sealed = self.roots(self.OLD, "release:v1:" + "a" * 12)
        current = self.roots(self.NEW, "release:v2:" + "b" * 12, jobs="/other/jobs.log",
                             registry="/home/me/checkout")
        self.assertEqual(self.moved(sealed, current), RA.RELEASE_LAUNCH_ROOTS - {"registry_root"})

    def test_a_projection_tree_a_checkout_or_a_malformed_root_is_never_a_release_move(self):
        sealed = self.roots(self.OLD, "release:v1:" + "a" * 12)
        projection = self.roots("/home/me/.claude", "release:v2:" + "b" * 12)
        self.assertEqual(self.moved(sealed, projection), frozenset())        # not a verified release copy
        checkout = self.roots(self.OLD, "0" * 40)
        self.assertEqual(self.moved(checkout, self.roots(self.NEW, "release:v2:" + "b" * 12)), frozenset())
        for broken in (None, 7, {"kind": "runtime_root", "path": "relative/root"},
                       {"kind": "wrapper_root", "path": self.NEW}, {"kind": "runtime_root", "path": None}):
            with self.subTest(broken=broken):
                current = dict(self.roots(self.NEW, "release:v2:" + "b" * 12), runtime_root=broken)
                self.assertEqual(self.moved(sealed, current), frozenset())

    def test_the_replacement_keeps_the_runtime_derived_values_of_the_release(self):
        import dispatch_replacement as R
        self.assertIs(R.RUNTIME_DERIVED_KEYS, RA.RELEASE_DERIVED_VALUES)


class AccessChangeTest(unittest.TestCase):
    """BC rt-839dbd48: the route's parent hands its next owner another access request; the
    request in force is the latest `access` row beside the route, and the sealed route stays."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        (base / "wt").mkdir()
        self.data = base / "DB" / "WWD_LG"
        self.data.mkdir(parents=True)
        self.route = {"route_id": "rt-839dbd48af21216d", "route_hash": "sha256:" + "e" * 64,
                      "artifact_root": str(base / "artifacts"), "cwd": str(base / "wt"),
                      "selection_pins": {"contract_version": 1, "owner": {"harness": "opencode", "model": None,
                                                                         "effort": None}}}
        self.request_file = base / "access.json"
        self.request_file.write_text(json.dumps({"schema_version": 1, "writable_roots": [],
                                                 "read_roots": [str(self.data)], "network": {"required": False}}))
        self.jobs = base / "state" / "jobs.log"

    def row(self, root="/data/a", source="unattributed", route=None, digest=None):
        request = {"read_roots": [root]}
        return RA.record_access_change(route or self.route, request=request,
                                       request_sha256=digest or RA.request_digest(request),
                                       by={"harness": "opencode", "session_id": "oc-sid"}, source=source)

    def test_the_latest_access_row_is_in_force_and_the_pins_are_untouched(self):
        self.assertIsNone(RA.access_in_force(self.route))
        first = self.row()
        self.assertIsNone(first["previous_sha256"])
        self.assertIsNone(self.row())                          # the same request again records nothing
        second = self.row("/data/b", source="peer:hearting-4d·" + "f" * 32)
        self.assertEqual(second["previous_sha256"], first["request_sha256"])
        self.assertEqual(RA.access_in_force(self.route)["request_sha256"], second["request_sha256"])
        self.row("/data/c", digest="0" * 64)                     # a row that does not digest to its request
        self.assertEqual(RA.access_in_force(self.route)["request_sha256"], second["request_sha256"])
        self.assertEqual(RA.pin_changes(self.route), [])       # the pin readers skip access rows
        self.assertIs(RA.route_in_force(self.route), self.route)
        other = dict(self.route, route_hash="sha256:" + "0" * 64)
        self.assertIsNone(RA.access_in_force(other))

    def given(self, *, caller=("opencode", "oc-sid"), owner_parent="oc-sid", env=True):
        router = _load("route_authority_capability_route_access", "utilities/capability-route.py")
        import work_start
        rows = {"att-owner": ("done", {"worker_type": "owner", "owner_route_id": self.route["route_id"],
                                       "parent_sid": owner_parent, "attempt_id": "att-owner"})}
        extra = {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(self.request_file)} if env else {}
        with mock.patch.dict(os.environ, extra), \
             mock.patch.object(RA, "caller_identity", return_value=caller), \
             mock.patch.object(work_start, "_rows", return_value=rows), \
             mock.patch.object(router, "_turn_peer_source", return_value="unattributed"):
            if not env:
                os.environ.pop("AGENT_DISPATCH_EXECUTION_ACCESS_FILE", None)
            return router._record_access_change(self.route, self.jobs)

    def test_the_parent_s_request_at_start_or_correct_is_recorded_once(self):
        self.assertIsNone(self.given(env=False))
        result = self.given()
        self.assertEqual((result["changed"], result["source"]), (True, "unattributed"))
        (row,) = RA.access_changes(self.route)
        self.assertEqual(row["request"]["read_roots"], [str(self.data)])
        self.assertEqual(row["by"], {"harness": "opencode", "session_id": "oc-sid"})
        self.assertFalse(self.given()["changed"])

    def test_another_session_or_an_invalid_request_records_nothing(self):
        refused = self.given(caller=("claude", "other-sid"))
        self.assertEqual(refused, {"changed": False, "reason": "access-change-parent-only"})
        self.request_file.write_text(json.dumps({"schema_version": 1, "writable_roots": ["/"],
                                                 "read_roots": [], "network": {"required": False}}))
        invalid = self.given()
        self.assertFalse(invalid["changed"])
        self.assertTrue(invalid["reason"].startswith("execution-access-root-too-broad"))
        self.assertEqual(RA.access_changes(self.route), [])


class RelocationAuthorityTest(unittest.TestCase):
    def test_no_live_history_does_not_grant_parent_ownership(self):
        observed = SimpleNamespace(resource_paths=set(), open_paths=set(), kept=lambda *args: None)
        with tempfile.TemporaryDirectory() as directory:
            result = RA.relocation_admission(Path(directory), [{"route_id": "rt-history"}], [], evidence=observed)
        self.assertTrue(result.allowed)
        self.assertFalse(RA.owns({"parent_sid": "old-parent"}, "supervisor-without-ack", None))

    def test_existing_live_unknown_lease_resource_reasons_protect_history(self):
        for reason in ["owner-live", "lease-held", "resource-run", "finish-pending", "human-gate"]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                observed = SimpleNamespace(resource_paths=set(), open_paths=set(), kept=lambda *args: reason)
                result = RA.relocation_admission(Path(directory), [{"route_id": "rt-source"}], [], evidence=observed)
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, reason)
        with mock.patch("route_autoclose._Evidence", side_effect=PermissionError("unknown process")):
            self.assertFalse(RA.relocation_admission(Path("/missing"), [], []).allowed)

    def test_cycle_holder_without_route_and_target_unrelated_work(self):
        observed = SimpleNamespace(resource_paths={"/source/cycle/log"}, open_paths=set(), kept=lambda *args: None)
        self.assertFalse(RA.relocation_admission(Path("/source"), [], [Path("/source/cycle")], evidence=observed).allowed)
        observed.resource_paths = {"/target/unrelated/log"}
        self.assertTrue(RA.relocation_admission(Path("/source"), [], [Path("/source/cycle")], evidence=observed).allowed)


class RelocationCorrectionTest(unittest.TestCase):
    def test_missing_historical_completion_keeps_history_without_closing(self):
        import route_autoclose as ac
        import dispatch_terminal_commit as terminal
        import inline_finish
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route_path = root / ".runtime/routes/rt-stale.json"
            route_path.parent.mkdir(parents=True)
            raw = b'{"route_id":"rt-stale", "cwd":"/old/missing/root"}'
            route_path.write_bytes(raw)
            observed = ac._Evidence.__new__(ac._Evidence)
            observed.root = root
            observed.held = set()
            observed.owners = {"rt-stale": [{"jobs": "/old/missing/jobs.log", "status": "done", "metadata": {}}]}
            observed.resource_paths = observed.resource_routes = observed.open_paths = set()
            observed.gate_roots = []
            with mock.patch.object(terminal, "owner_completion_state", return_value=terminal.CompletionState("unknown", "FileNotFoundError")), \
                 mock.patch.object(inline_finish, "pending_state", return_value=None):
                result = RA.relocation_admission(root, [{"route_id": "rt-stale"}], [], evidence=observed)
            self.assertTrue(result.allowed, result.reason)
            self.assertEqual(route_path.read_bytes(), raw)
            self.assertFalse(route_path.with_suffix(".outcome.json").exists())
            self.assertFalse(RA.owns({"parent_sid": "old-parent"}, "without-ack", None))

    def test_positive_pending_and_unknown_execution_still_protected(self):
        import route_autoclose as ac
        import dispatch_terminal_commit as terminal
        import inline_finish
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observed = ac._Evidence.__new__(ac._Evidence)
            observed.root = root
            observed.resource_paths = observed.open_paths = set()
            observed.gate_roots = []
            observed.owners = {"rt-selected": [{"jobs": "/old/jobs.log", "status": "done", "metadata": {}}]}
            for state, held, resources, pending, reason in [
                (terminal.CompletionState("pending", "workflow-not-complete"), set(), set(), None, "owner-settlement-pending"),
                (terminal.CompletionState("unknown", "ValueError"), set(), set(), None, "owner-settlement-pending"),
                (terminal.CompletionState("unknown", "FileNotFoundError"), {"rt-selected"}, set(), None, "owner-live"),
                (terminal.CompletionState("unknown", "FileNotFoundError"), set(), {"rt-selected"}, None, "resource-run"),
                (terminal.CompletionState("unknown", "FileNotFoundError"), set(), set(), {"state": "sealing"}, "finish-pending")]:
                observed.held, observed.resource_routes = held, resources
                with self.subTest(reason=reason), mock.patch.object(terminal, "owner_completion_state", return_value=state), \
                     mock.patch.object(inline_finish, "pending_state", return_value=pending):
                    result = RA.relocation_admission(root, [], [], selected_route_ids=["rt-selected"], evidence=observed)
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, reason)

    def test_selected_manifest_route_and_external_parent_support_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".runtime/routes/rt-selected.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"route_id": "rt-selected", "parent_route_id": "rt-external"}))
            seen = []
            evidence = SimpleNamespace(resource_paths=set(), open_paths=set(), kept=lambda rid, *args: seen.append(rid))
            result = RA.relocation_admission(root, [], [], selected_route_ids=["rt-selected"], evidence=evidence)
            self.assertTrue(result.allowed)
            self.assertEqual(set(result.route_ids), {"rt-selected", "rt-external"})
            self.assertEqual(set(seen), set(result.route_ids))


class CancellationProofBoundaryTest(unittest.TestCase):
    def test_live_cancelled_related_child_never_becomes_a_terminal_proof(self):
        for state in ("live", "unverifiable"):
            choice = RA.decide_continuation(action="terminal-child", status="open",
                metadata={"parent_close_requested": "1"},
                facts={"process": SimpleNamespace(state=state, reason="cleanup-pending")})
            self.assertEqual((choice.allowed, choice.state, choice.reason), (False, "wait", "child-not-quiescent"))
            self.assertTrue(choice.retry_when)

    def test_quiet_cancel_request_still_requires_the_terminal_writer(self):
        choice = RA.decide_continuation(action="terminal-child", status="open",
            metadata={"parent_close_requested": "1"},
            facts={"process": SimpleNamespace(state="quiescent", reason="exited")})
        self.assertEqual((choice.allowed, choice.reason, choice.next_action),
                         (False, "child-not-terminal", "same-command"))

    def test_cancelled_record_keeps_conflict_obligation_and_existing_record_action(self):
        metadata = {"note": "cancelled-by-parent", "failure_class": "cancelled", "terminal_conflict": "1"}
        decision = RA.decide_continuation(action="attempt", status="done", metadata=metadata,
            facts={"process_state": "quiescent"})
        self.assertEqual(decision.next_action, "inspect-conflict")
        self.assertEqual(RA.decide_continuation(action="attempt-action", status="done", metadata=metadata).next_action,
                         "advance-completed")

    def test_quiet_committed_results_do_not_wait_for_their_cancellation_intent_again(self):
        for note, failure in (("completed-marker", "pass"), ("dead-worker-fail", "fail"),
                              ("cancelled-by-parent", "cancelled")):
            metadata = {"note": note, "failure_class": failure, "parent_close_requested": "1"}
            original = dict(metadata)
            choice = RA.decide_continuation(action="terminal-child", status="done", metadata=metadata,
                facts={"process": SimpleNamespace(state="quiescent", reason="cleanup-settled")})
            self.assertTrue(choice.allowed, note)
            metadata["terminal_conflict"] = "1"
            conflict = RA.decide_continuation(action="terminal-child", status="done", metadata=metadata,
                facts={"process": SimpleNamespace(state="quiescent", reason="cleanup-settled")})
            self.assertEqual((conflict.allowed, conflict.reason), (False, "transaction-conflict"))
            self.assertEqual({k: v for k, v in metadata.items() if k != "terminal_conflict"}, original)
            execution = RA.decide_continuation(action="attempt", status="done", metadata=original,
                facts={"process_state": "live"})
            self.assertIn(execution.next_action, {"cancel", "settle-cancellation"})


class InputContinuationBoundaryTest(unittest.TestCase):
    def test_only_original_input_admissions_pass_for_each_harness(self):
        quiet, unknown = SimpleNamespace(state="quiescent", reason="exited"), SimpleNamespace(state="unverifiable", reason="identity-missing")
        cases = [
            ("cancelled", {}, quiet, True, True, False, "settle"),
            ("killed", {}, quiet, True, True, False, "settle"),
            ("done", {"note": "completed-marker"}, quiet, True, True, False, "settle"),
            ("open", {}, quiet, False, True, False, "wait"),
            ("open", {}, unknown, True, False, False, "wait"),
            ("done", {"note": "dead-worker-blocked"}, unknown, False, False, True, "retained"),
            ("done", {"note": "dead-worker-fail", "failure_class": "fail", "worker_type": "owner"}, unknown, False, False, True, "retained"),
            ("done", {"note": "dead-runtime-exit", "worker_type": "owner", "launch_started": "1",
                      "supervisor_lease": "flock-v1", "supervisor_lease_file": "/unused"}, unknown, False, False, False, "wait"),
        ]
        for harness in ("claude", "codex", "opencode"):
            for status, metadata, process, accepting, reachable, allowed, state in cases:
                with self.subTest(harness=harness, status=status, note=metadata.get("note"), accepting=accepting):
                    choice = RA.decide_continuation(action="owner-input", status=status,
                        metadata={"harness": harness, **metadata}, facts={"process": process,
                            "accepting": accepting, "reachable": reachable})
                    self.assertEqual((choice.allowed, choice.state), (allowed, state))
                    if state == "wait":
                        self.assertEqual(choice.next_action, "same-command")
                        self.assertTrue(choice.retry_when)


class OwnerFormatOnlyEndTest(unittest.TestCase):
    BASE = {"worker_type": "owner", "note": "dead-contract", "failure_class": "contract",
            "classifier_source": "supervisor-terminal-v1", "process_exit": "0",
            "reconcile_reason": "final-handoff-invalid", "terminal_event": "claude-result"}

    def test_metadata_allowlist_and_answerable_classification(self):
        self.assertTrue(RA.owner_result_format_only_end("done", self.BASE))
        self.assertEqual(RA.answerable_owner_end("done", self.BASE), "BLOCKED")
        for event in ("claude-result", "opencode-result", "turn.completed", "step_finish.stop"):
            for reason in ("final-handoff-invalid", "pass-blocker-not-none"):
                with self.subTest(event=event, reason=reason):
                    self.assertTrue(RA.owner_result_format_only_end(
                        "done", {**self.BASE, "terminal_event": event, "reconcile_reason": reason}))
        for key, value in (("worker_type", "stage"), ("worker_type", "review"),
                           ("worker_type", "frame"), ("worker_type", ""), ("note", "dead-worker-blocked"),
                           ("failure_class", "runtime"), ("classifier_source", "other"),
                           ("process_exit", "1"), ("reconcile_reason", "other"),
                           ("terminal_event", "other")):
            with self.subTest(key=key, value=value):
                self.assertFalse(RA.owner_result_format_only_end("done", {**self.BASE, key: value}))
        self.assertFalse(RA.owner_result_format_only_end("open", self.BASE))
        self.assertFalse(RA.owner_result_format_only_end("done", {k: v for k, v in self.BASE.items()
                                                                    if k != "worker_type"}))


class ResourcePredecessorPidReuseTest(unittest.TestCase):
    """A finished run whose PID a later process reused is still finished."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sentinel = Path(directory.name) / "run.log.exit"
        self.sentinel.write_text("0\n", encoding="utf-8")
        import resource_run_registry
        self.live = resource_run_registry.proc_identity(os.getpid())
        self.namespace = os.readlink("/proc/self/ns/pid")

    def row(self, **changes):
        row = {"run_id": "finished", "status": "succeeded", "workflow_state": "STAGE_SUCCEEDED",
               "exit_code": 0, "sentinel": str(self.sentinel), "pid": os.getpid(),
               "starttime": str(int(self.live["starttime"]) - 1),
               "command_hash": "recorded-command", "pid_namespace": self.namespace}
        row.update(changes)
        return row

    def test_later_process_on_the_same_pid_does_not_keep_the_run_open(self):
        self.assertTrue(RA.resource_predecessor_finished(self.row()))

    def test_unproven_reuse_stays_unfinished(self):
        cases = {
            "exec-same-start": {"starttime": self.live["starttime"]},
            "earlier-start": {"starttime": str(int(self.live["starttime"]) + 1)},
            "unparsable-start": {"starttime": "wrong"},
            "namespace-unrecorded": {"pid_namespace": None},
            "no-sentinel": {"sentinel": str(self.sentinel) + ".missing"},
            "sentinel-disagrees": {"exit_code": 3},
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.assertFalse(RA.resource_predecessor_finished(self.row(**change)))

    def test_terminal_exit_evidence_survives_a_different_pid_namespace(self):
        import resource_run_registry as RR
        for status, code in (("failed", 1), ("succeeded", 0)):
            with self.subTest(status=status):
                self.sentinel.write_text(str(code) + "\n", encoding="utf-8")
                row = self.row(status=status, exit_code=code, pid_namespace="pid:[foreign]")
                before = dict(row)
                self.assertEqual(RR.classify_identity(row),
                                 ("stale", None, "process-namespace-mismatch"))
                self.assertTrue(RA.resource_predecessor_finished(row))
                self.assertEqual(row, before)

    def test_namespace_mismatch_cannot_replace_missing_or_contradictory_exit_evidence(self):
        self.sentinel.write_text("1\n", encoding="utf-8")
        cases = {
            "running": {"status": "running"},
            "launching": {"status": "launching"},
            "unknown-status": {"status": "unknown"},
            "cancelled": {"cancel_requested": True},
            "parent-close": {"parent_close_requested": True},
            "missing-exit": {"exit_code": None},
            "different-exit": {"exit_code": 2},
            "missing-sentinel": {"sentinel": str(self.sentinel) + ".missing"},
            "inconsistent-success": {"status": "succeeded"},
            "missing-identity": {"pid": None},
        }
        for name, changes in cases.items():
            with self.subTest(case=name):
                row = self.row(**{"status": "failed", "exit_code": 1,
                                  "pid_namespace": "pid:[foreign]", **changes})
                self.assertFalse(RA.resource_predecessor_finished(row))
        self.sentinel.write_text("0\n", encoding="utf-8")
        self.assertFalse(RA.resource_predecessor_finished(
            self.row(status="failed", exit_code=0, pid_namespace="pid:[foreign]")))
        self.sentinel.write_text("not an exit\n", encoding="utf-8")
        self.assertFalse(RA.resource_predecessor_finished(
            self.row(status="failed", exit_code=1, pid_namespace="pid:[foreign]")))

    def test_live_process_is_protected_even_with_a_terminal_record_and_sentinel(self):
        self.sentinel.write_text("1\n", encoding="utf-8")
        self.assertFalse(RA.resource_predecessor_finished(
            self.row(**{**self.live, "status": "failed", "exit_code": 1})))

    def test_foreign_failed_retry_waits_for_recorded_group_quiescence(self):
        import signal
        import subprocess
        import resource_run_registry as RR

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "failed.exit"
            sentinel.write_text("23\n", encoding="utf-8")
            child_pid_file = root / "payload.pid"
            leader_code = (
                "import pathlib, subprocess, sys; "
                "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); raise SystemExit(23)"
            )
            leader = subprocess.Popen(
                [sys.executable, "-c", leader_code, str(child_pid_file)],
                start_new_session=True,
            )
            try:
                self.assertEqual(leader.wait(timeout=10), 23)
                payload_pid = int(child_pid_file.read_text(encoding="utf-8"))
                self.assertEqual(os.getpgid(payload_pid), leader.pid)
                os.kill(payload_pid, 0)
                row = {
                    "run_id": "foreign-failed",
                    "status": "failed",
                    "workflow_state": "FAILED_RETRYABLE",
                    "exit_code": 23,
                    "sentinel": str(sentinel),
                    "pid": leader.pid,
                    "starttime": "1",
                    "command_hash": "recorded-command",
                    "pid_namespace": "pid:[foreign]",
                    "process_group": leader.pid,
                    "resource_policy": "supervised-owner",
                }
                candidate = {"log": str(root / "retry.log"),
                             "sentinel": str(root / "retry.exit")}
                before = dict(row)

                # The payload child remains alive, but this process cannot
                # observe a group in the recorded foreign namespace.
                self.assertFalse(RR.resource_process_group_quiescent(row))
                with self.assertRaisesRegex(ValueError, "resource-route-body-conflict"):
                    RA.require_resource_predecessors([row], candidate)
                self.assertEqual(row, before)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "23\n")

                os.kill(payload_pid, signal.SIGTERM)
                deadline = time.monotonic() + 5
                ended = False
                while time.monotonic() < deadline:
                    try:
                        state = Path(f"/proc/{payload_pid}/stat").read_text(encoding="utf-8")
                        ended = state.rsplit(") ", 1)[1].split()[0] == "Z"
                    except FileNotFoundError:
                        ended = True
                    if ended:
                        break
                    time.sleep(0.01)
                self.assertTrue(ended, "payload child did not end")

                # A positive observation from the resource's namespace makes
                # the same retry admission path available without rewriting
                # its failure row, sentinel, or history.
                with mock.patch.object(RR, "resource_process_group_quiescent", return_value=True) as observe:
                    RA.require_resource_predecessors([row], candidate)
                observe.assert_called_once_with(row)
                self.assertEqual(row, before)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "23\n")
            finally:
                try:
                    os.killpg(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class SettledResourceCancellationTest(unittest.TestCase):
    def setUp(self):
        import resource_run_registry as RR
        self.registry = RR
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.row = {
            "run_id": "cancelled", "status": "failed", "workflow_state": "CANCELLED",
            "cancel_requested": True, "failure_class": "cancelled", "ended_at": 123,
            "exit_code": None, "sentinel": str(self.root / "old.exit"),
            "log": str(self.root / "old.log"), "pid": 999999999, "starttime": "1",
            "command_hash": "old-command", "process_group": 999999999,
            "pid_namespace": os.readlink("/proc/self/ns/pid"), "launch_state": "started",
            "resource_policy": "supervised-owner",
        }
        self.candidate = {"log": str(self.root / "next.log"),
                          "sentinel": str(self.root / "next.exit")}

    def test_settled_cancellation_without_exit_evidence_admits_a_new_attempt(self):
        before = dict(self.row)
        self.assertTrue(RA.resource_predecessor_finished(self.row))
        RA.require_resource_predecessors([self.row], self.candidate)
        self.assertEqual(self.row, before)
        self.assertFalse(Path(self.row["sentinel"]).exists())

    def test_unsettled_cancelled_resources_remain_protected(self):
        cases = {
            "no-end": {"ended_at": None}, "running": {"status": "running"},
            "launching": {"status": "launching"}, "queued": {"launch_state": "queued"},
            "claimed": {"launch_state": "claimed"}, "parent-close": {"parent_close_requested": True},
            "no-identity": {"pid": None}, "no-group": {"process_group": None},
            "no-namespace": {"pid_namespace": None}, "foreign-namespace": {"pid_namespace": "pid:[foreign]"},
            "live": {**self.registry.proc_identity(os.getpid()), "process_group": os.getpgrp()},
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                row = {**self.row, **change}
                with self.assertRaisesRegex(ValueError, "resource-route-body-conflict"):
                    RA.require_resource_predecessors([row], self.candidate)
        for state in ("working", "reaping", "stale"):
            with self.subTest(identity=state), mock.patch.object(
                    self.registry, "classify_identity", return_value=(state, None, "fixture")):
                self.assertFalse(RA.resource_predecessor_finished(self.row))
        with mock.patch.object(self.registry, "resource_process_group_quiescent", return_value=False):
            self.assertFalse(RA.resource_predecessor_finished(self.row))

    def test_cancelled_resource_never_counts_as_scientific_success(self):
        import dispatch_resource_wait as WAIT
        Path(self.row["sentinel"]).write_text("0\n")
        self.assertTrue(RA.resource_predecessor_finished(self.row))
        self.assertFalse(WAIT.resource_execution_succeeded(self.row))

    def test_settled_cancellation_keeps_all_prior_evidence_paths_protected(self):
        for field in ("log", "sentinel", "progress_file"):
            with self.subTest(field=field):
                candidate = {**self.candidate, field: self.row["log"]}
                with self.assertRaisesRegex(ValueError, "resource-route-body-conflict"):
                    RA.require_resource_predecessors([self.row], candidate)

    def test_compute_cancellation_uses_fresh_quiescence_and_rechecks_captured_row(self):
        row = {**self.row, "execution": {"kind": "compute-hosts", "run_id": "remote"}}
        with mock.patch.object(self.registry, "compute_execution_observation",
                               return_value={"state": "quiescent"}) as observe:
            self.assertTrue(RA.resource_predecessor_finished(row))
        observe.assert_called_once_with(row)
        captured = {"row": dict(row), "state": "quiescent"}
        with mock.patch.object(self.registry, "compute_execution_observation",
                               side_effect=AssertionError("remote probe under reservation lock")):
            RA.require_resource_predecessors([row], self.candidate,
                                             observations={row["run_id"]: captured})
            for observation in (None, {**captured, "state": "unknown"},
                                {**captured, "state": "not-quiescent"},
                                {**captured, "row": {**row, "ended_at": 124}}):
                with self.subTest(observation=observation):
                    with self.assertRaisesRegex(ValueError, "resource-route-body-conflict"):
                        RA.require_resource_predecessors([row], self.candidate,
                            observations={row["run_id"]: observation})
            for change in ({"ended_at": None}, {"status": "running"}, {"status": "launching"},
                           {"launch_state": "claimed"}, {"launch_state": "queued"},
                           {"parent_close_requested": True}):
                changed = {**row, **change}
                with self.subTest(change=change):
                    with self.assertRaisesRegex(ValueError, "resource-route-body-conflict"):
                        RA.require_resource_predecessors([changed], self.candidate,
                            observations={row["run_id"]: {"row": changed, "state": "quiescent"}})
            with self.assertRaisesRegex(ValueError, "resource-route-body-conflict"):
                RA.require_resource_predecessors([row], {"log": row["log"]},
                    observations={row["run_id"]: captured})


class NestedHostSandboxEqualityTest(unittest.TestCase):
    """Only a matching, known workspace-write parent bounds nested Codex access."""

    @staticmethod
    def inputs():
        resolved = {
            "dispatch_depth": 1,
            "sandbox": "workspace-write",
            "parent_session_id": "codex-owner-session",
            "parent_harness": "codex",
            "parent_transport": "app-server",
            "parent_sandbox": "workspace-write",
            "launch_authority": "parent-session",
        }
        previous = {
            "attempt_id": "att-replay",
            "harness": "codex",
            "jobs": "/isolated/state/jobs.log",
            "worktree": "/isolated/worktree",
            "argv": ["--start", "--sandbox", "workspace-write"],
            "task": "same work",
            "resolved": dict(resolved),
            "applied_permissions": {
                "launch_lifecycle": "foreground-scoped",
                "runtime_sandbox": "danger-full-access",
                "nested_headless_network": False,
            },
        }
        current = {
            **previous,
            "resolved": dict(resolved),
            "applied_permissions": {
                "launch_lifecycle": "detached",
                "runtime_sandbox": "workspace-write",
                "nested_headless_network": False,
            },
        }
        return previous, current

    def test_exact_nested_parent_makes_host_replay_effectively_equal(self):
        previous, current = self.inputs()
        self.assertNotEqual(
            RA.granted_permissions(previous["applied_permissions"]),
            RA.granted_permissions(current["applied_permissions"]),
        )  # The raw launcher settings still differ.
        self.assertEqual(RA._granted_permission_pair(previous, current)[0],
                         RA._granted_permission_pair(previous, current)[1])
        self.assertIsNone(RA.permission_mismatch_detail(previous, current))
        self.assertTrue(RA.same_sealed_work(previous, current))

    def test_real_widening_narrowing_and_unproven_parent_boundaries_stay_distinct(self):
        previous, current = self.inputs()
        cases = {
            "host-widening": ("runtime_sandbox", "danger-full-access", None),
            "host-narrowing": ("runtime_sandbox", "read-only", None),
            "parent-absent": (None, None, "absent"),
            "parent-unknown": (None, None, "unknown"),
            "parent-identity-changed": (None, None, "different"),
            "parent-boundary-changed": (None, None, "read-only"),
        }
        for name, (permission_key, permission_value, parent_change) in cases.items():
            with self.subTest(case=name):
                candidate = json.loads(json.dumps(current))
                if permission_key:
                    candidate["applied_permissions"][permission_key] = permission_value
                elif parent_change == "absent":
                    candidate["resolved"] = {"dispatch_depth": 1, "sandbox": "workspace-write"}
                elif parent_change == "unknown":
                    candidate["resolved"]["parent_sandbox"] = "unknown"
                elif parent_change == "different":
                    candidate["resolved"]["parent_session_id"] = "other-session"
                elif parent_change == "read-only":
                    candidate["resolved"]["parent_sandbox"] = "read-only"
                detail = RA.permission_mismatch_detail(previous, candidate)
                self.assertIsNotNone(detail)
                self.assertIn("axis=file", detail)
                self.assertFalse(RA.same_sealed_work(previous, candidate))


if __name__ == "__main__":
    unittest.main()
