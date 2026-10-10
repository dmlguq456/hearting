#!/usr/bin/env python3
"""Cycle seal abolition (artifact-path-contract §45) end-to-end regressions.

One file for the whole §45 loop, one slice prefix per implementation step:
`test_a1_*` covers D-123 (writes, parents, one inclusion rule, binding);
`test_a2_*` covers the preserved copies, the next-document publisher and the
D-127 proofs (a finished cycle stays finished after its files change);
`test_b1_*` covers the bounded, lock-free scan of D-124 and the history calls of
D-125 (a refresh is one short publication, never a long lock);
`test_b2_*` covers the triggers that start it (begin, Claude Stop, Codex Stop, OpenCode
`session.idle`) and the root cursor they share;
`test_c1_*` covers D-126 (cycle-move, cycle-mark, delete, and a hand-made move or
deletion found again by the next list, begin or close);
`test_d1_*` covers the older tools (a mark is a disposition, titles and workflow groups read a
finished cycle as it is now, the batch and freeze digest guards stay).  Every
fixture runs on an isolated temporary artifact root; the real canonical root,
registry, and routes directory are never touched.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_campaign as CAMP  # noqa: E402
import artifact_history as H  # noqa: E402 -- the real recorder; a fake in sys.modules stands in for it per test
import artifact_checkpoint_trigger as TRIG  # noqa: E402
import artifact_index  # noqa: E402
import artifact_manifest as M  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_receipt as RCPT  # noqa: E402
import dispatch_lock_order  # noqa: E402
import dispatch_terminal_commit as T  # noqa: E402

_FX_SPEC = importlib.util.spec_from_file_location(
    "producer_fixtures_for_seal_abolition", Path(__file__).with_name("artifact_producer.test.py"))
FX = importlib.util.module_from_spec(_FX_SPEC)
_FX_SPEC.loader.exec_module(FX)
R = FX.R


def _load_sibling(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TERM = _load_sibling("terminal_fixtures_for_seal_abolition", "dispatch_terminal_commit.test.py")
INLINE = _load_sibling("inline_fixtures_for_seal_abolition", "inline_finish.test.py")


class SealAbolitionBase(FX.ProducerTestBase):
    def setUp(self):
        # A worker's own AGENT_* variables must not leak into the fixture runtime.
        scrubbed = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("AGENT_")}
        self.addCleanup(os.environ.update, scrubbed)
        # Same switch tools/run-tests.py sets: route-hash and identity checks refuse, not warn.
        # A machine's own config (e.g. a closed-cycle refresh switch) must not steer the fixture either.
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        patcher = mock.patch.dict(os.environ, {"HEARTING_GATES": "on", "XDG_CONFIG_HOME": config.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def finish(self, route, route_file, result, *, rel="plans/cycle/plan.md", data=b"plan body\n"):
        """Write one output, close the route, and finalize the cycle."""
        self.write_output(result, rel, data)
        self.close(route, route_file)
        return P.finalize(self.root, cycle_id=result["cycle_id"])

    def manifest(self, result):
        return json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))


class A1PolicyWritesParentsTest(SealAbolitionBase):
    # -- A28-1 -----------------------------------------------------------
    def test_a1_open_parent_child_first_and_cross_campaign(self):
        self.activate()
        parent_route, parent_file, parent = self.begin(campaign_key="causal-stream")
        child_route, child_file = self.route(slug="followup", parent_cycle_id=parent["cycle_id"])
        child = P.begin(self.root, route_file=child_file, capability="autopilot-code", intensity="direct")
        self.write_output(parent)
        self.write_output(child)
        self.close(child_route, child_file)
        # The child seals first: the parent is still open and not in the index.
        sealed_child = P.finalize(self.root, cycle_id=child["cycle_id"])
        self.assertEqual(sealed_child["status"], "sealed")
        index = adm.load_index(self.root)
        self.assertIn(child["cycle_id"], index.cycles)
        self.assertNotIn(parent["cycle_id"], index.cycles)
        self.assertEqual(P.read_cycle_record(self.root, child["cycle_id"])["parent_cycle_id"], parent["cycle_id"])
        # The parent seals afterwards, in either order.
        self.close(parent_route, parent_file)
        self.assertEqual(P.finalize(self.root, cycle_id=parent["cycle_id"])["status"], "sealed")
        index = adm.load_index(self.root)
        self.assertIn(parent["cycle_id"], index.cycles)
        self.assertIn(child["cycle_id"], index.cycles)

        # A parent in another campaign is just a parent: the child keeps its own campaign.
        other_file = self.route(slug="other-first", campaign_key="other-stream")[1]
        other = P.begin(self.root, route_file=other_file, capability="autopilot-code", intensity="direct")
        self.assertNotEqual(other["campaign_id"], parent["campaign_id"])
        cross_route, cross_file = self.route(
            slug="cross-campaign", campaign_key="other-stream", parent_cycle_id=parent["cycle_id"])
        cross = P.begin(self.root, route_file=cross_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(cross["campaign_id"], other["campaign_id"])
        self.assertEqual(P.read_cycle_record(self.root, cross["cycle_id"])["parent_cycle_id"], parent["cycle_id"])
        self.write_output(cross)
        self.close(cross_route, cross_file)
        self.assertEqual(P.finalize(self.root, cycle_id=cross["cycle_id"])["status"], "sealed")
        self.assertIn(cross["cycle_id"], adm.load_index(self.root).cycles)

    def test_a1_parent_state_does_not_gate_a_child(self):
        self.activate()
        _, _, first = self.begin(campaign_key="stream-a")
        parent = P.read_cycle_record(self.root, first["cycle_id"])
        route_file = self.route(slug="lifecycle-child")[1]
        parent["state"] = "superseded"
        P._write_cycle_record(self.root, parent, exclusive=False)
        child = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                        parent_cycle_id=first["cycle_id"])
        self.assertEqual(P.read_cycle_record(self.root, child["cycle_id"])["parent_cycle_id"], first["cycle_id"])
        # What stays: the parent must be a producer record of this root, and a
        # superseded campaign still takes no new cycle from `begin`.
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=self.route(slug="orphan-child")[1], capability="autopilot-code",
                    intensity="direct", parent_cycle_id="cyc_" + "9" * 32)
        self.assertEqual(ctx.exception.code, "parent-cycle-not-joinable")
        campaign = P.read_campaign(self.root, first["campaign_id"])
        campaign["state"] = "superseded"
        P._write_campaign(self.root, campaign, exclusive=False)
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=self.route(slug="late-child")[1], capability="autopilot-code",
                    intensity="direct", parent_cycle_id=first["cycle_id"])
        self.assertEqual(ctx.exception.code, "campaign-not-active")

    def test_a1_index_parent_context_keeps_self_parent_and_orphan_judgments(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="index-parent")
        self.finish(route, route_file, result)
        document = self.manifest(result)
        child = json.loads(json.dumps(document))
        child["cycle"]["cycle_id"] = "cyc_" + "7" * 32
        child["cycle"]["parent_cycle_id"] = "cyc_" + "8" * 32
        empty = artifact_index.empty(document["artifact_root_id"])
        report = artifact_index.check(empty, child, idempotency_key="k", manifest_digest="d")
        self.assertIn("index-orphan-parent-cycle", {v.code for v in report.violations})
        known = artifact_index.check(
            empty, child, idempotency_key="k", manifest_digest="d",
            known_parent_cycle_ids=frozenset({child["cycle"]["parent_cycle_id"]}))
        self.assertNotIn("index-orphan-parent-cycle", {v.code for v in known.violations})
        self.assertNotIn("index-parent-cycle-campaign-mismatch", {v.code for v in known.violations})
        child["cycle"]["parent_cycle_id"] = child["cycle"]["cycle_id"]
        selfish = artifact_index.check(
            empty, child, idempotency_key="k", manifest_digest="d",
            known_parent_cycle_ids=frozenset({child["cycle"]["cycle_id"]}))
        self.assertIn("index-self-parent-cycle", {v.code for v in selfish.violations})

    # -- A28-2 -----------------------------------------------------------
    def test_a1_closed_cycle_write_and_output_allow(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="closed-writes")
        self.assertEqual(self.finish(route, route_file, result)["status"], "sealed")
        cycle_dir = Path(result["cycle_dir"])
        target = cycle_dir / "artifacts" / "plans" / "cycle" / "late-edit.md"
        verdict = P.check_write(self.root, target)
        self.assertEqual(verdict["verdict"], "allow", verdict)
        self.assertEqual(verdict["cycle_id"], result["cycle_id"])
        self.assertEqual(P.cycle_bucket(self.root, target), ("plans", result["cycle_id"]))
        output, layout = P.resolve_output_dir(self.root, "plans", cycle_dir_hint=str(cycle_dir))
        self.assertEqual((output, layout), (cycle_dir / "artifacts" / "plans", "cycle"))
        # The route's own lineage finds the closed cycle and may write into it again.
        record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertEqual(P.route_cycle_for(self.root, route)["cycle_id"], result["cycle_id"])
        self.assertTrue(P.cycle_route_admission(self.root, record, route).allow)
        self.assertEqual(
            P.require_cycle_output(self.root, target, cycle_id=result["cycle_id"], route_id=route["route_id"]),
            cycle_dir / "artifacts")
        # A review report is prepared against the closed cycle as well.
        review_output = cycle_dir / "artifacts" / "plans" / "review.md"
        binding = P.prepare_review_output_binding(
            self.root, cycle_id=result["cycle_id"], producer_id=record["producer_id"], attempt_id="att-late-review",
            review_output=review_output, capability="autopilot-code", unit="qa/code-review",
            worktree=str(Path(route["cwd"]).resolve()))
        self.assertEqual(binding["output_path"], str(review_output))
        # An abandoned cycle takes writes too: state never decides.
        abandoned_route, abandoned_file = self.route(slug="abandoned-writes", campaign_key="closed-writes")
        abandoned = P.begin(self.root, route_file=abandoned_file, capability="autopilot-code", intensity="direct")
        self.write_output(abandoned)
        P.finalize(self.root, cycle_id=abandoned["cycle_id"], state="abandoned",
                   abandon_reason="operator-decision", allow_open_route=True)
        late = Path(abandoned["cycle_dir"]) / "artifacts" / "plans" / "cycle" / "after-abandon.md"
        self.assertEqual(P.check_write(self.root, late)["verdict"], "allow")

    def test_a1_hidden_symlink_and_temporary_exclusion(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="inclusion-rule")
        outside = Path(self._tmp.name) / "outside-secret.txt"
        outside.write_text("must never be read\n", encoding="utf-8")
        artifacts = Path(result["cycle_dir"]) / "artifacts"
        kept = {
            "plans/cycle/plan.md": b"plan body\n",
            "plans/_internal/notes.md": b"support path stays\n",
            "plans/cycle/data.parquet": bytes(range(256)),
        }
        for rel, data in kept.items():
            self.write_output(result, rel, data)
        excluded = {
            ".pytest_cache/v/cache/lastfailed": b"{}",
            "plans/cycle/.hidden.md": b"hidden file",
            "plans/.git/config": b"[core]",
            "plans/cycle/__pycache__/mod.cpython-312.pyc": b"\x00pyc",
            "plans/cycle/stray.pyc": b"\x00pyc",
            "plans/cycle/draft.md.swp": b"swap",
            "plans/cycle/draft.md~": b"backup",
            "plans/cycle/.#draft.md": b"emacs lock",
            "plans/cycle/scratch.tmp": b"tmp",
            "plans/cycle/download.part": b"part",
        }
        for rel, data in excluded.items():
            self.write_output(result, rel, data)
        link = artifacts / "plans" / "cycle" / "outside-link.md"
        os.symlink(outside, link)
        dir_link = artifacts / "plans" / "cycle" / "linked-dir"
        os.symlink(outside.parent, dir_link)
        self.close(route, route_file)
        reads = []
        original_read_bytes = Path.read_bytes

        def tracking_read_bytes(path):
            reads.append(str(path))
            return original_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", tracking_read_bytes):
            sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed")
        manifest_text = (Path(result["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8")
        for rel in kept:
            self.assertIn("artifacts/" + rel, manifest_text, rel)
        for rel in excluded:
            self.assertNotIn(rel, manifest_text, rel)
        self.assertNotIn("outside-link.md", manifest_text)
        self.assertNotIn("linked-dir", manifest_text)
        self.assertEqual(
            sorted(sealed["excluded_hidden"]),
            sorted("artifacts/" + rel for rel in excluded))
        self.assertEqual(
            sorted(sealed["excluded_symlinks"]),
            ["artifacts/plans/cycle/linked-dir", "artifacts/plans/cycle/outside-link.md"])
        # A link is only lstat-ed: neither the link nor its target was read.
        self.assertEqual([p for p in reads if "outside" in p or "linked-dir" in p], [])
        self.assertEqual(outside.read_text(encoding="utf-8"), "must never be read\n")

    def test_pytest_basetemp_files_are_output_and_its_links_and_caches_are_not(self):
        # §D-2 (b): no pytest name rule.  A basetemp a user named is ordinary output; what pytest
        # leaves that the one rule already drops (its `*current` link, `.pytest_cache`, bytecode) stays dropped.
        self.activate()
        route, route_file, result = self.begin(campaign_key="pytest-basetemp")
        self.write_output(result, "plans/report.md")
        kept = ["plans/evidence/scratch/pytest-all/test_x0/out.json", "plans/evidence/pytest-report.html"]
        for rel in kept:
            self.write_output(result, rel, b"{}\n")
        scratch = Path(result["cycle_dir"]) / "artifacts" / "plans/evidence/scratch/pytest-all"
        os.symlink("test_x0", scratch / "test_xcurrent")
        self.write_output(result, ".pytest_cache/v/x", b"x")
        self.write_output(result, "plans/evidence/__pycache__/m.pyc", b"\x00pyc")
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed", sealed)
        listed = {row["locator"]["path"] for row in self.manifest(result)["artifact_revisions"]}
        self.assertEqual(listed, {"artifacts/plans/report.md"} | {"artifacts/" + rel for rel in kept})
        self.assertEqual(sorted(sealed["excluded_symlinks"]),
                         ["artifacts/plans/evidence/scratch/pytest-all/test_xcurrent"])
        self.assertEqual(sorted(sealed["excluded_hidden"]),
                         ["artifacts/.pytest_cache/v/x", "artifacts/plans/evidence/__pycache__/m.pyc"])

    def test_a1_enumerate_applies_one_rule_without_flags(self):
        self.activate()
        _, _, result = self.begin(campaign_key="one-rule")
        self.write_output(result, "plans/cycle/plan.md")
        self.write_output(result, "plans/cycle/.cache/blob", b"x")
        os.symlink(Path(self._tmp.name), Path(result["cycle_dir"]) / "artifacts" / "plans" / "cycle" / "link")
        excluded, links = [], []
        rows, violations = P._enumerate_output(Path(result["cycle_dir"]), excluded=excluded,
                                                excluded_symlinks=links)
        self.assertEqual(violations, [])
        self.assertEqual([rel for rel, _data in rows], ["artifacts/plans/cycle/plan.md"])
        self.assertEqual(excluded, ["artifacts/plans/cycle/.cache/blob"])
        self.assertEqual(links, ["artifacts/plans/cycle/link"])

    def test_a1_location_and_runtime_guards_unchanged(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="guards")
        self.finish(route, route_file, result)
        cycle_dir = Path(result["cycle_dir"])
        # location contract: legacy top level, shared, campaign record, control files, unknown cycle.
        legacy = P.check_write(self.root, self.root / "plans" / "2026-10-01_x" / "plan.md")
        self.assertEqual((legacy["verdict"], legacy["reason"]), ("deny", "legacy-top-level-write-denied"))
        shared = self.root / "shared" / "spec" / ("ref_" + "1" * 32) / "revisions" / ("rrev_" + "2" * 32) / "prd.md"
        self.assertEqual(P.check_write(self.root, shared)["reason"], "shared-revision-immutable")
        self.assertEqual(P.check_write(self.root, cycle_dir.parent / "campaign.json")["reason"],
                         "campaign-record-machine-managed")
        self.assertEqual(P.check_write(self.root, cycle_dir / "manifest.json")["reason"], "outside-cycle-artifacts")
        self.assertEqual(P.check_write(self.root, cycle_dir / ".cycle.json")["verdict"], "deny")
        unknown = cycle_dir.parent / "2026-10-01_unknown" / "artifacts" / "plans" / "x.md"
        self.assertEqual(P.check_write(self.root, unknown)["verdict"], "deny")
        # containment: a path that climbs out of the cycle never classifies as a cycle write.
        escape = cycle_dir / "artifacts" / ".." / ".." / "escape.md"
        self.assertEqual(P.check_write(self.root, escape)["verdict"], "deny")
        # a symlinked component is followed to where it really points, never into the cycle.
        elsewhere = Path(self._tmp.name) / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, cycle_dir / "artifacts" / "plans" / "linked")
        through = P.check_write(self.root, cycle_dir / "artifacts" / "plans" / "linked" / "x.md")
        self.assertEqual((through["verdict"], through["reason"]), ("allow", "outside-artifact-root"), through)
        self.assertNotEqual(through.get("layout"), "cycle")
        # route hash seal: a tampered route file stays unreadable as a route.
        tampered, _tampered_file = self.route(slug="tamper-probe", campaign_key="guards")
        tampered["campaign_key"] = "tampered"
        with self.assertRaisesRegex(ValueError, "modified route hash"):
            R.verify_route(tampered)

    # -- binding ---------------------------------------------------------
    def test_a1_binding_identity_ignores_campaign_move_only(self):
        record = {"campaign_id": "camp_" + "1" * 32, "cycle_id": "cyc_" + "2" * 32,
                  "producer_id": "prod_" + "3" * 32, "route_id": "rt-fixture", "route_hash": "h" * 64,
                  "route_file": "/routes/rt-fixture.json"}
        stored = T.cycle_identity_digest(record)
        moved = {**record, "campaign_id": "camp_" + "9" * 32}
        self.assertNotEqual(T.cycle_identity_digest(moved), stored)
        self.assertTrue(T.cycle_identity_matches(moved, stored, campaign_id=record["campaign_id"]))
        self.assertTrue(T.cycle_identity_matches(record, stored, campaign_id=record["campaign_id"]))
        for field, value in (("cycle_id", "cyc_" + "8" * 32), ("producer_id", "prod_" + "8" * 32),
                             ("route_id", "rt-other"), ("route_hash", "e" * 64)):
            with self.subTest(field=field):
                changed = {**moved, field: value}
                self.assertFalse(T.cycle_identity_matches(changed, stored, campaign_id=record["campaign_id"]))


L = FX.L


def _row_bytes(rows, key, value):
    return [json.dumps(row, sort_keys=True) for row in rows if row.get(key) == value]


class A2SnapshotsAndRefreshTest(SealAbolitionBase):
    """§45 D-124: copies come first, a closed cycle publishes its next document."""

    def closed(self, campaign_key="a2-stream", files=None):
        self.activate()
        route, route_file = self.route(slug=campaign_key, campaign_key=campaign_key)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for rel, data in (files or {"plans/cycle/plan.md": b"plan body\n"}).items():
            self.write_output(result, rel, data)
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed", sealed)
        return route, route_file, result

    def closed_with_route(self, campaign_key, files=None):
        self.activate()
        route, route_file = self.route(slug=campaign_key, campaign_key=campaign_key)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for rel, data in (files or {"plans/cycle/plan.md": b"plan body\n"}).items():
            self.write_output(result, rel, data)
        self.close(route, route_file)
        self.assertEqual(P.finalize(self.root, cycle_id=result["cycle_id"])["status"], "sealed")
        return route, route, route_file, result

    def edit(self, result, rel, data):
        (Path(result["cycle_dir"]) / "artifacts" / rel).write_bytes(data)

    def snapshot_names(self, result):
        directory = L.manifest_snapshot_dir(self.root, result["cycle_id"])
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def test_a2_first_and_refinalize_snapshot_before_publish(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="a2-snapshots")
        self.write_output(result, "plans/cycle/plan.md", b"plan body\n")
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        seen = []
        exclusive, atomic = P._write_exclusive, P._write_atomic

        def spy_exclusive(path, data, mode=0o644):
            if Path(path).name == "manifest.json":
                copy = L.manifest_snapshot_path(self.root, cycle_id, json.loads(data)["manifest_revision_id"])
                seen.append(("first", copy.is_file() and copy.read_bytes() == data))
            return exclusive(path, data, mode)

        def spy_atomic(path, data, mode=0o644):
            if Path(path).name == "manifest.json":
                copy = L.manifest_snapshot_path(self.root, cycle_id, json.loads(data)["manifest_revision_id"])
                seen.append(("refinalize", copy.is_file() and copy.read_bytes() == data))
            return atomic(path, data, mode)

        with mock.patch.object(P, "_write_exclusive", spy_exclusive), mock.patch.object(P, "_write_atomic", spy_atomic):
            P.finalize(self.root, cycle_id=cycle_id)
            first_raw = (Path(result["cycle_dir"]) / "manifest.json").read_bytes()
            self.edit(result, "plans/cycle/plan.md", b"plan body, edited\n")
            again = P.finalize(self.root, cycle_id=cycle_id)
        self.assertTrue(again["refreshed"], again)
        self.assertEqual(seen, [("first", True), ("refinalize", True)])
        manifest = Path(result["cycle_dir"]) / "manifest.json"
        copies = {json.loads(p.read_bytes())["manifest_revision_id"]: p.read_bytes()
                  for p in L.manifest_snapshot_dir(self.root, cycle_id).iterdir()}
        self.assertEqual(len(copies), 2)
        self.assertIn(first_raw, copies.values())
        self.assertIn(manifest.read_bytes(), copies.values())


    def test_a2_refinalize_continuation_keeps_previous_terminal(self):
        _, route, route_file, result = self.closed_with_route("a2-continuation")
        cycle_id = result["cycle_id"]
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        first = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = P.read_cycle_record(self.root, cycle_id)
        binding_first = {key: record[key] for key in ("campaign_id", "cycle_id", "producer_id", "route_id", "route_hash")}
        binding_first["cycle_record_digest"] = T.cycle_identity_digest(record)
        # A plain refresh never touches the close: state, routes and the terminal record stay as written.
        self.edit(result, "plans/cycle/plan.md", b"plan body, edited\n")
        plain = P.finalize(self.root, cycle_id=cycle_id)
        self.assertTrue(plain["refreshed"])
        self.assertNotIn("terminal_added", plain)
        second = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("routes", "cycle"):
            self.assertEqual(first[key], second[key], key)
        # A route that continues the closed cycle writes into it and, while open, adds no terminal record.
        continuation = R.build_continuation_route(route, resume_from_node="inline", requested_boundary="inline",
                                                  reason="a2-continuation", artifact_root=self.root)
        continuation_file = R.canonical_route_path(self.root, continuation["route_id"])
        R.publish_continuation_route(continuation, route, continuation_file)
        self.assertTrue(R.bind_continuation_cycle(self.root, route, continuation)["bound"])
        self.write_output(result, "plans/cycle/after.md", b"written by the continuation\n")
        during = P.finalize(self.root, cycle_id=cycle_id)
        self.assertTrue(during["refreshed"])
        self.assertNotIn("terminal_added", during)
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["routes"], first["routes"])
        # Closed, it adds exactly its own terminal record -- even with no file changed -- and keeps the earlier one.
        self.close(continuation, continuation_file)
        closed = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(closed.get("terminal_added"), continuation["route_id"], closed)
        self.assertTrue(closed["refreshed"])
        third = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(third["routes"][:1], first["routes"])
        self.assertEqual([row["route_id"] for row in third["routes"]], [route["route_id"], continuation["route_id"]])
        self.assertNotEqual(third["routes"][1]["terminal_marker"], "pending")
        added = [e for e in third["events"][len(second["events"]):] if e["event_type"] == "route.terminal.recorded"]
        self.assertEqual([e["event_id"] for e in added], [third["routes"][1]["terminal_evidence_id"]])
        self.assertEqual(third["events"][:len(first["events"])], first["events"])
        self.assertEqual(third["cycle"], first["cycle"])
        self.assertTrue(M.validate_update(third, preserved=[first, second], previous=second).ok)
        # Each route's own proof holds, and the same leaf closing again adds nothing more.
        leaf_binding = dict(binding_first, route_id=continuation["route_id"], route_hash=continuation["route_hash"])
        for binding in (binding_first, leaf_binding):
            verified = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
            self.assertEqual(verified["status"], "already-sealed")
        again = P.finalize(self.root, cycle_id=cycle_id)
        self.assertFalse(again["refreshed"])
        self.assertNotIn("terminal_added", again)
        self.assertTrue(adm.verify_index(self.root).ok)

    def test_a2_exact_finalize_of_a_continuation_records_its_terminal(self):
        _, route, route_file, result = self.closed_with_route("a2-exact-continuation")
        cycle_id = result["cycle_id"]
        record = P.read_cycle_record(self.root, cycle_id)
        continuation = R.build_continuation_route(route, resume_from_node="inline", requested_boundary="inline",
                                                  reason="a2-exact", artifact_root=self.root)
        continuation_file = R.canonical_route_path(self.root, continuation["route_id"])
        R.publish_continuation_route(continuation, route, continuation_file)
        self.assertTrue(R.bind_continuation_cycle(self.root, route, continuation)["bound"])
        self.write_output(result, "plans/cycle/continued.md", b"continued\n")
        self.close(continuation, continuation_file)
        binding = {key: record[key] for key in ("campaign_id", "cycle_id", "producer_id")}
        binding.update(route_id=continuation["route_id"], route_hash=continuation["route_hash"],
                       cycle_record_digest=T.cycle_identity_digest(record))
        # The finish that closed the continuation proves it: the cycle's document gains its record first.
        done = P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(done["status"], "already-sealed")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([row["route_id"] for row in document["routes"]], [route["route_id"], continuation["route_id"]])
        self.assertIn("artifacts/plans/cycle/continued.md",
                      [row["locator"]["path"] for row in document["artifact_revisions"]])
        again = P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(again["manifest_digest"], done["manifest_digest"])

    def test_a2_refinalize_resumes_the_same_revision_after_a_crash(self):
        _, _, result = self.closed("a2-crash")
        cycle_id = result["cycle_id"]
        manifest = Path(result["cycle_dir"]) / "manifest.json"
        before = manifest.read_bytes()
        # Crash after the copy and the journal, before manifest.json is replaced.
        self.edit(result, "plans/cycle/plan.md", b"edited once\n")
        real_atomic = P._write_atomic

        def refuse_manifest(path, data, mode=0o644):
            if Path(path).name == "manifest.json":
                raise OSError("simulated crash before the swap")
            return real_atomic(path, data, mode)

        with mock.patch.object(P, "_write_atomic", refuse_manifest), self.assertRaises(OSError):
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(manifest.read_bytes(), before)
        planned = [json.loads(p.read_bytes())["manifest_revision_id"]
                   for p in L.manifest_snapshot_dir(self.root, cycle_id).iterdir()
                   if p.read_bytes() != before]
        self.assertEqual(len(planned), 1)
        resumed = P.finalize(self.root, cycle_id=cycle_id)
        self.assertFalse(resumed["refreshed"], resumed)
        published = json.loads(manifest.read_bytes())
        self.assertEqual(published["manifest_revision_id"], planned[0])
        self.assertFalse(P.journal_path(self.root, cycle_id).exists())
        self.assertEqual(adm.load_index(self.root).manifests[cycle_id]["manifest_digest"],
                         m_digest(published))
        self.assertEqual(P.read_cycle_record(self.root, cycle_id)["manifest_digest"], m_digest(published))
        # Crash after manifest.json was replaced, before the index and record followed.
        self.edit(result, "plans/cycle/plan.md", b"edited twice\n")
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=cycle_id, crash_after_manifest=True)
        crashed = json.loads(manifest.read_bytes())
        self.assertNotEqual(adm.load_index(self.root).manifests[cycle_id]["manifest_digest"], m_digest(crashed))
        P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(json.loads(manifest.read_bytes())["manifest_revision_id"], crashed["manifest_revision_id"])
        self.assertEqual(adm.load_index(self.root).manifests[cycle_id]["manifest_digest"], m_digest(crashed))
        self.assertEqual(len(self.snapshot_names(result)), 3)
        self.assertTrue(adm.verify_index(self.root).ok)

    def test_a2_refinalize_publishes_next_revision_and_keeps_the_rest(self):
        files = {"plans/cycle/plan.md": b"plan body\n", "plans/cycle/notes.md": b"notes\n",
                 "plans/cycle/drop.md": b"to be dropped\n"}
        _, _, result = self.closed("a2-refresh", files)
        cycle_id = result["cycle_id"]
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        before = json.loads(manifest_path.read_text(encoding="utf-8"))
        record_before = P.read_cycle_record(self.root, cycle_id)
        artifacts = Path(result["cycle_dir"]) / "artifacts"
        self.edit(result, "plans/cycle/notes.md", b"notes, changed\n")
        (artifacts / "plans/cycle/drop.md").unlink()
        self.write_output(result, "plans/cycle/new.md", b"new file\n")
        self.write_output(result, "plans/cycle/.cache/blob", b"hidden")
        os.symlink(Path(self._tmp.name), artifacts / "plans/cycle/link")
        out = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(out["status"], "already-sealed")
        self.assertTrue(out["refreshed"])
        self.assertEqual(out["changes"], {"added": ["artifacts/plans/cycle/new.md"],
                                          "modified": ["artifacts/plans/cycle/notes.md"],
                                          "removed": ["artifacts/plans/cycle/drop.md"]})
        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        by_path = lambda doc: {r["locator"]["path"]: r for r in doc["artifact_revisions"]}
        old_rows, new_rows = by_path(before), by_path(after)
        plan = "artifacts/plans/cycle/plan.md"
        self.assertEqual(json.dumps(old_rows[plan], sort_keys=True), json.dumps(new_rows[plan], sort_keys=True))
        self.assertEqual(_row_bytes(before["artifacts"], "artifact_id", old_rows[plan]["artifact_id"]),
                         _row_bytes(after["artifacts"], "artifact_id", old_rows[plan]["artifact_id"]))
        notes = "artifacts/plans/cycle/notes.md"
        self.assertEqual(old_rows[notes]["artifact_id"], new_rows[notes]["artifact_id"])
        self.assertNotEqual(old_rows[notes]["artifact_revision_id"], new_rows[notes]["artifact_revision_id"])
        self.assertEqual(new_rows[notes]["revision_sequence"], 1)
        self.assertNotIn("artifacts/plans/cycle/drop.md", new_rows)
        self.assertNotIn(old_rows["artifacts/plans/cycle/drop.md"]["artifact_id"],
                         {a["artifact_id"] for a in after["artifacts"]})
        self.assertIn("artifacts/plans/cycle/new.md", new_rows)
        self.assertNotIn(".cache", json.dumps(after))
        self.assertNotIn("link", json.dumps(after["artifact_revisions"]))
        # Earlier events are untouched; only two revision records follow them.
        self.assertEqual(after["events"][:len(before["events"])], before["events"])
        tail = after["events"][len(before["events"]):]
        self.assertEqual([e["event_type"] for e in tail], ["artifact.revision.recorded"] * 2)
        # The close itself is not rewritten.
        for key in ("cycle", "routes", "manifest_id"):
            self.assertEqual(before[key], after[key], key)
        self.assertNotEqual(before["manifest_revision_id"], after["manifest_revision_id"])
        # The document reads only with its earlier copy beside it.
        self.assertFalse(M.validate(after).ok)
        self.assertFalse(M.validate_update(after, preserved=[], previous=before).ok)
        self.assertTrue(M.validate_update(after, preserved=[before], previous=before).ok)
        # Index, record and rebuild agree; the removed file's ID stays taken.
        index = adm.load_index(self.root)
        digest = m_digest(after)
        self.assertEqual(index.manifests[cycle_id]["manifest_digest"], digest)
        self.assertEqual(index.cycles[cycle_id]["manifest_digest"], digest)
        dropped_artifact = old_rows["artifacts/plans/cycle/drop.md"]["artifact_id"]
        self.assertIn(dropped_artifact, index.stable_ids)
        record = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual((record["state"], record["sealed_on"], record["manifest_digest"]),
                         ("sealed", record_before["sealed_on"], digest))
        self.assertEqual(len(self.snapshot_names(result)), 2)
        self.assertTrue(adm.verify_index(self.root).ok)
        rebuilt = adm.rebuild_index(self.root)
        self.assertIn(dropped_artifact, rebuilt.stable_ids)
        self.assertEqual(artifact_index.canonical_bytes(rebuilt), artifact_index.canonical_bytes(adm.load_index(self.root)))
        # Nothing changed, nothing written.
        watched = [manifest_path, P.cycle_record_path(self.root, cycle_id), adm._index_path(self.root),
                   *L.manifest_snapshot_dir(self.root, cycle_id).iterdir()]
        stamps = {p: p.stat().st_mtime_ns for p in watched}
        quiet = P.finalize(self.root, cycle_id=cycle_id)
        self.assertFalse(quiet["refreshed"])
        self.assertEqual(stamps, {p: p.stat().st_mtime_ns for p in watched})
        self.assertEqual(self.snapshot_names(result), sorted(p.name for p in stamps if p.parent.name == cycle_id))

    def test_a2_update_validator_keeps_earlier_records_and_resolves_only_this_cycle(self):
        files = {"plans/cycle/plan.md": b"plan body\n", "plans/cycle/extra.md": b"extra\n"}
        _, _, result = self.closed("a2-validator", files)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        before = json.loads(manifest_path.read_text(encoding="utf-8"))
        (Path(result["cycle_dir"]) / "artifacts/plans/cycle/plan.md").unlink()
        (Path(result["cycle_dir"]) / "artifacts/plans/cycle/extra.md").unlink()
        self.write_output(result, "plans/cycle/other.md", b"other\n")
        P.finalize(self.root, cycle_id=result["cycle_id"])
        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Every file the first document had is gone, including the required primary.
        self.assertEqual([r["locator"]["path"] for r in after["artifact_revisions"]], ["artifacts/plans/cycle/other.md"])
        self.assertTrue(M.validate_update(after, preserved=[before], previous=before).ok)
        # A foreign cycle's copy resolves nothing.
        _, _, other = self.closed("a2-validator-other")
        foreign = json.loads((Path(other["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(M.validate_update(after, preserved=[foreign], previous=before).ok)
        # An earlier event may not change, vanish, or be followed by anything but a revision record.
        changed = json.loads(json.dumps(after))
        changed["events"][0]["payload"] = {"locator": "artifacts/other"}
        self.assertIn("update-earlier-events-changed",
                      {v.code for v in M.validate_update(changed, preserved=[before], previous=before).violations})
        missing = json.loads(json.dumps(after))
        del missing["events"][0]
        self.assertIn("update-earlier-events-changed",
                      {v.code for v in M.validate_update(missing, preserved=[before], previous=before).violations})
        stray = json.loads(json.dumps(after))
        stray["events"].append(dict(stray["events"][-1], event_id="evt_" + "9" * 32, stream_id="strm_" + "9" * 32,
                                    event_type="decision.recorded"))
        self.assertIn("update-event-type-not-allowed",
                      {v.code for v in M.validate_update(stray, preserved=[before], previous=before).violations})
        moved = json.loads(json.dumps(after))
        moved["cycle"]["state"] = "abandoned"
        self.assertIn("update-cycle-field-changed",
                      {v.code for v in M.validate_update(moved, preserved=[before], previous=before).violations})

    def test_a2_index_swaps_a_cycle_row_only_on_its_digest(self):
        _, _, result = self.closed("a2-index", {"plans/cycle/plan.md": b"plan body\n", "plans/cycle/drop.md": b"d\n"})
        _, _, other = self.closed("a2-index-other")
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        before = json.loads(manifest_path.read_text(encoding="utf-8"))
        (Path(result["cycle_dir"]) / "artifacts/plans/cycle/drop.md").unlink()
        self.edit(result, "plans/cycle/plan.md", b"changed\n")
        index_before = adm.load_index(self.root)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        cycle_id = result["cycle_id"]
        old_digest, new_digest = m_digest(before), m_digest(after)
        plain = artifact_index.check(index_before, after, idempotency_key=cycle_id, manifest_digest=new_digest)
        self.assertIn("index-cycle-id-duplicate", {v.code for v in plain.violations})
        wrong = artifact_index.check(index_before, after, idempotency_key=cycle_id, manifest_digest=new_digest,
                                     replaces_manifest_digest="sha256:" + "0" * 64)
        self.assertIn("manifest-revision-append-out-of-scope", {v.code for v in wrong.violations})
        right = artifact_index.check(index_before, after, idempotency_key=cycle_id, manifest_digest=new_digest,
                                     replaces_manifest_digest=old_digest)
        self.assertTrue(right.ok, right.violations)
        # A cycle never takes another cycle's ID, swap or not.
        other_doc = json.loads((Path(other["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        stolen = json.loads(json.dumps(other_doc))
        stolen["artifacts"][0]["artifact_id"] = before["artifacts"][0]["artifact_id"]
        stolen["artifact_revisions"][0]["artifact_id"] = before["artifacts"][0]["artifact_id"]
        refused = artifact_index.check(index_before, stolen, idempotency_key=other["cycle_id"],
                                       manifest_digest="sha256:" + "1" * 64,
                                       replaces_manifest_digest=index_before.manifests[other["cycle_id"]]["manifest_digest"])
        self.assertIn("index-stable-id-duplicate", {v.code for v in refused.violations})
        # Applying keeps what the cycle owned: nothing is retired by dropping a row.
        applied = artifact_index.apply(index_before, after, cycle_path=index_before.cycles[cycle_id]["cycle_path"],
                                       manifest_digest=new_digest, idempotency_key=cycle_id)
        for stable_id in index_before.stable_ids:
            self.assertIn(stable_id, applied.stable_ids)
        self.assertEqual(applied.manifests[cycle_id]["manifest_digest"], new_digest)

    def test_a2_rebuild_index_knows_open_parents(self):
        self.activate()
        parent_route, parent_file, parent = self.begin(campaign_key="a2-open-parent")
        child_route, child_file = self.route(slug="a2-child", parent_cycle_id=parent["cycle_id"])
        child = P.begin(self.root, route_file=child_file, capability="autopilot-code", intensity="direct")
        self.write_output(parent)
        self.write_output(child)
        self.close(child_route, child_file)
        self.assertEqual(P.finalize(self.root, cycle_id=child["cycle_id"])["status"], "sealed")
        # The parent is still open and not in the index: a rebuild keeps the child.
        rebuilt = adm.rebuild_index(self.root)
        self.assertIn(child["cycle_id"], rebuilt.cycles)
        self.assertNotIn(parent["cycle_id"], rebuilt.cycles)
        self.assertTrue(adm.verify_index(self.root).ok)

    def test_a2_locked_index_read_once(self):
        # A first close reads the index once under the admission lock, and so does every
        # later refresh (§45 correction B: the new locked sections never re-read it).
        self.activate()
        route, route_file = self.route(slug="a2-first-read", campaign_key="a2-first-read")
        first = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(first)
        self.close(route, route_file)
        first_reads = []
        real_first = adm.load_index

        def counting_first(root):
            first_reads.append(1)
            return real_first(root)

        with mock.patch.object(adm, "load_index", counting_first):
            self.assertEqual(P.finalize(self.root, cycle_id=first["cycle_id"])["status"], "sealed")
        self.assertEqual(len(first_reads), 1, first_reads)
        _, _, result = self.closed("a2-index-read")
        self.edit(result, "plans/cycle/plan.md", b"edited\n")
        reads = []
        real_load = adm.load_index

        def counting(root):
            reads.append(1)
            return real_load(root)

        with mock.patch.object(adm, "load_index", counting):
            out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertTrue(out["refreshed"])
        self.assertLessEqual(len(reads), 1, reads)
        reads.clear()
        with mock.patch.object(adm, "load_index", counting):
            self.assertFalse(P.finalize(self.root, cycle_id=result["cycle_id"])["refreshed"])
        self.assertLessEqual(len(reads), 1, reads)


class A2ProofsAfterEditsTest(SealAbolitionBase):
    """§45 D-127: a closed cycle stays closed in every proof, whatever happens to its files."""

    def prepared(self, campaign_key="a2-proofs"):
        self.activate()
        route, route_file, result = self.begin(campaign_key=campaign_key)
        output = self.write_output(result, "plans/cycle/final_report.md", b"verified report\n")
        self.close(route, route_file)
        record = P.read_cycle_record(self.root, result["cycle_id"])
        binding = {key: record[key] for key in ("campaign_id", "cycle_id", "producer_id", "route_hash")}
        binding["cycle_record_digest"] = T.cycle_identity_digest(record)
        return route, route_file, result, output, binding

    def test_a2_verify_finalized_cycle_ignores_later_edits_and_names_the_recorded_revision(self):
        route, route_file, result, output, binding = self.prepared()
        cycle_id = result["cycle_id"]
        P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        first = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        # The report is edited, and a file appears: the proof does not look at them.
        output.write_bytes(b"verified report, edited later\n")
        self.write_output(result, "plans/cycle/later.md", b"later\n")
        manifest = Path(result["cycle_dir"]) / "manifest.json"
        before = manifest.read_bytes()
        self.assertEqual(P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding), first)
        self.assertEqual(P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)["status"],
                         "already-sealed")
        self.assertEqual(manifest.read_bytes(), before)
        # Closing again publishes the next document; the proof of the earlier revision stands.
        refreshed = P.finalize(self.root, cycle_id=cycle_id)
        self.assertTrue(refreshed["refreshed"])
        current = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(current["manifest_digest"], refreshed["manifest_digest"])
        recorded = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding,
                                            expected_manifest_digest=first["manifest_digest"])
        self.assertEqual((recorded["manifest_digest"], recorded["updated_since"], recorded["current_manifest_digest"]),
                         (first["manifest_digest"], True, refreshed["manifest_digest"]))
        # A cycle that moved to another campaign is the same cycle.
        moved = dict(binding, campaign_id="camp_" + "9" * 32)
        self.assertEqual(P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=moved)["status"],
                         "already-sealed")
        # What stays refused: another cycle's identity, a missing terminal record, a drifted index.
        with self.assertRaises(P.ProducerError) as ctx:
            P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=dict(binding, producer_id="prod_" + "8" * 32))
        self.assertEqual((ctx.exception.code, ctx.exception.detail), ("already-sealed-mismatch", "producer_id"))
        index = adm.load_index(self.root)
        payload = json.loads(json.dumps(artifact_index.to_payload(index)))
        payload["manifests"][cycle_id]["manifest_digest"] += "-foreign"
        adm._write_index(self.root, artifact_index.parse(payload))
        with self.assertRaises(P.ProducerError) as ctx:
            P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(ctx.exception.detail, "index")
        adm._write_index(self.root, index)
        outcome = R.outcome_path(route_file)
        saved = outcome.read_bytes()
        outcome.unlink()
        with self.assertRaises(P.ProducerError) as ctx:
            P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(ctx.exception.detail, "completion-evidence")
        outcome.write_bytes(saved)

    def test_a2_missing_snapshot_replays_terminal_only(self):
        route, route_file, result, output, binding = self.prepared("a2-no-copy")
        cycle_id = result["cycle_id"]
        P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        recorded = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        output.unlink()
        self.write_output(result, "plans/cycle/other.md", b"other\n")
        refreshed = P.finalize(self.root, cycle_id=cycle_id)
        self.assertTrue(refreshed["refreshed"])
        # The copies are lost (a cycle edited before it was ever observed has none).
        import shutil
        shutil.rmtree(L.manifest_snapshot_dir(self.root, cycle_id))
        replay = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding,
                                          expected_manifest_digest=recorded["manifest_digest"])
        self.assertEqual((replay["manifest_digest"], replay["updated_since"]), (recorded["manifest_digest"], True))
        # The terminal record itself is still held to its proof.
        outcome = R.outcome_path(route_file)
        outcome.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(P.ProducerError) as ctx:
            P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding,
                                     expected_manifest_digest=recorded["manifest_digest"])
        self.assertEqual(ctx.exception.code, "already-sealed-mismatch")

    def test_a2_receipt_accepts_preserved_revision(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="a2-receipt")
        self.write_output(result, "plans/cycle/plan.md", b"plan body\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        first = json.loads(manifest_path.read_text(encoding="utf-8"))

        def receipt_for(document, rel):
            revision = next(row for row in document["artifact_revisions"] if row["locator"]["path"] == rel)
            return RCPT.build_v3(
                completed_at="2026-10-01T00:00:00Z", repository_id=document["repository_id"],
                campaign_id=document["campaign"]["campaign_id"], cycle_id=document["cycle"]["cycle_id"],
                artifact_id=revision["artifact_id"], artifact_revision_id=revision["artifact_revision_id"],
                manifest_id=document["manifest_id"], manifest_revision_id=document["manifest_revision_id"])

        rel = "artifacts/plans/cycle/plan.md"
        early = receipt_for(first, rel)
        verdict = RCPT.resolve(self.root, early)
        self.assertEqual((verdict.state, verdict.detail), ("accepted", None), verdict)
        # The cycle publishes a later document: the earlier receipt was true when written.
        self.write_output(result, "plans/cycle/plan.md", b"plan body, edited\n")
        P.finalize(self.root, cycle_id=result["cycle_id"])
        second = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertNotEqual(first["manifest_revision_id"], second["manifest_revision_id"])
        old = RCPT.resolve(self.root, early)
        self.assertEqual((old.state, old.detail), ("accepted", "updated-since"), old)
        fresh = RCPT.resolve(self.root, receipt_for(second, rel))
        self.assertEqual((fresh.state, fresh.detail), ("accepted", None), fresh)
        # A revision no copy holds, and an artifact the revision never had, stay unresolved.
        unknown = dict(early, manifest_revision_id="mrev_" + "7" * 32)
        self.assertEqual(RCPT.resolve(self.root, unknown).state, "rejected")
        mismatched = dict(early, artifact_id="art_" + "6" * 32)
        refused = RCPT.resolve(self.root, mismatched)
        self.assertEqual(refused.state, "rejected")
        # Without its copy the old receipt has nothing to be read against.
        import shutil
        shutil.rmtree(L.manifest_snapshot_dir(self.root, result["cycle_id"]))
        self.assertEqual(RCPT.resolve(self.root, early).reason, "local-manifest-unregistered")


class A2EnvelopeReplayTest(TERM._TerminalCommitFixture):
    """The stored envelope is delivered as stored; the report it names may have moved on."""

    def seal_once(self):
        owner = TERM.owner_route_binding.OwnerRouteBinding(
            str(self.route_file), self.route["route_id"], self.route["route_hash"])
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            first = T.settle_terminal_commit(self.request(), T.TerminalCommitServices(
                close_route=lambda *a, **k: None, finalize_exact_cycle=lambda *a, **k: None,
                seal_envelope=T._default_seal_envelope))
        self.assertEqual(first.result, "completed")
        return owner

    def replay(self, owner):
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            return T.settle_terminal_commit(self.request(), T.TerminalCommitServices())

    def test_a2_envelope_replay_after_report_edit_move_remove(self):
        owner = self.seal_once()
        slot = T._commit_state_path(self.request()).parent
        stored = (slot / "owner-envelope.txt").read_text()
        meta_before = (slot / "owner-envelope.json").read_bytes()
        unchanged = self.replay(owner)
        self.assertEqual((unchanged.result, unchanged.detail), ("completed", None))
        self.artifact.write_text("edited after the settlement\n", encoding="utf-8")
        edited = self.replay(owner)
        self.assertEqual((edited.result, edited.reason, edited.detail),
                         ("completed", None, "primary-changed-after-seal"))
        self.assertEqual(edited.envelope_text, stored)
        moved = self.artifact.with_name("moved-report.md")
        self.artifact.rename(moved)
        away = self.replay(owner)
        self.assertEqual((away.result, away.detail), ("completed", "primary-missing-after-seal"))
        self.assertEqual(away.envelope_text, stored)
        moved.unlink()
        gone = self.replay(owner)
        self.assertEqual((gone.result, gone.detail), ("completed", "primary-missing-after-seal"))
        self.assertEqual(gone.envelope_text, stored)
        # The envelope record is never rewritten with the new bytes.
        self.assertEqual((slot / "owner-envelope.txt").read_text(), stored)
        self.assertEqual((slot / "owner-envelope.json").read_bytes(), meta_before)
        # What stays refused: an envelope that is not the one sealed.
        (slot / "owner-envelope.txt").write_text("artifact: elsewhere\nverdict: PASS\nblocker: none\n")
        refused = self.replay(owner)
        self.assertEqual((refused.result, refused.detail), ("recoverable", "envelope-content-mismatch"))


class A2InlineFinishReplayTest(unittest.TestCase):
    def test_a2_inline_finished_replay_uses_recorded_revision(self):
        fixture = INLINE.PublicInlineFinishTest("test_public_finish_seals_and_exact_replay_returns_one_receipt")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        first = fixture.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        receipt = json.loads(first.stdout)
        cycle_id = fixture.cycle["cycle_id"]
        state_file = fixture.root / ".runtime/inline-finish/v1" / fixture.route["route_id"] / "finish.json"
        state_before = state_file.read_bytes()
        # After the finish the evidence file is edited and a new file appears; closing the
        # cycle again publishes the next manifest document.
        fixture.evidence.write_bytes(b"evidence, edited after the finish\n")
        (fixture.cycle_dir / "artifacts/documents/extra.md").write_bytes(b"extra\n")
        refreshed = INLINE.artifact_producer.finalize(fixture.root, cycle_id=cycle_id)
        self.assertTrue(refreshed["refreshed"], refreshed)
        self.assertNotEqual(refreshed["manifest_digest"], receipt["manifest_digest"])
        replay = fixture.finish()
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replayed = json.loads(replay.stdout)
        self.assertTrue(replayed["replay"])
        self.assertEqual(replayed["inline_finish_id"], receipt["inline_finish_id"])
        self.assertEqual(replayed["manifest_digest"], receipt["manifest_digest"])
        self.assertEqual(replayed["manifest_updated_since"], refreshed["manifest_digest"])
        # The evidence going away is no different, and the stored receipt is not rewritten.
        fixture.evidence.unlink()
        gone = fixture.finish()
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertEqual(json.loads(gone.stdout)["manifest_digest"], receipt["manifest_digest"])
        self.assertEqual(state_file.read_bytes(), state_before)


class A2SharedPublicationTest(SealAbolitionBase):
    _cycle = FX.ComponentSetPreservation._cycle
    _reference = FX.ComponentSetPreservation._reference
    _journals = FX.ComponentSetPreservation._journals

    def admit(self, cycle, generation, **kw):
        return P.admit_shared(self.root, cycle_id=cycle["cycle_id"], kind="spec",
                              source=f"gen{generation}", key="prd", **kw)

    def test_a2_shared_retry_survives_source_changes(self):
        cycle = self._cycle([["a"], ["a"]])
        first = self.admit(cycle, 0, base_revision="none")
        source = Path(cycle["cycle_dir"]) / "artifacts/gen0"
        digest = P.read_cycle_record(self.root, cycle["cycle_id"])["manifest_digest"]
        latest = self._reference(first["shared_reference_id"])["latest_revision_id"]
        # The published PRD is edited, moved, then removed: the same call is the same publication.
        (source / "a/prd.md").write_text("edited after publication")
        for step in ("edited", "moved", "removed"):
            if step == "moved":
                source.rename(source.with_name("gen0-moved"))
            elif step == "removed":
                import shutil
                shutil.rmtree(source.with_name("gen0-moved"))
            retry = self.admit(cycle, 0, base_revision="none")
            self.assertEqual(retry["status"], "reused", step)
            self.assertEqual(retry["shared_reference_revision_id"], first["shared_reference_revision_id"], step)
            self.assertEqual(self._reference(first["shared_reference_id"])["latest_revision_id"], latest, step)
            self.assertEqual(self._journals(), [], step)
            self.assertEqual(P.read_cycle_record(self.root, cycle["cycle_id"])["manifest_digest"], digest, step)
        # Closing the cycle again records the changes; the retry still finds its publication.
        self.assertTrue(P.finalize(self.root, cycle_id=cycle["cycle_id"])["refreshed"])
        self.assertNotEqual(P.read_cycle_record(self.root, cycle["cycle_id"])["manifest_digest"], digest)
        again = self.admit(cycle, 0, base_revision="none")
        self.assertEqual((again["status"], again["shared_reference_revision_id"]),
                         ("reused", first["shared_reference_revision_id"]))
        # A new publication takes the source as it is now, and records the manifest it was taken from.
        (Path(cycle["cycle_dir"]) / "artifacts/gen1/a/prd.md").write_text("gen1, edited before publishing")
        second = self.admit(cycle, 1, base_revision=first["shared_reference_revision_id"])
        self.assertEqual(second["status"], "admitted")
        self.assertEqual((Path(second["revision_dir"]) / "a/prd.md").read_text(), "gen1, edited before publishing")
        revision = json.loads((Path(second["revision_dir"]) / P.REVISION_RECORD_NAME).read_text())
        self.assertEqual(revision["source"]["manifest_digest"],
                         P.read_cycle_record(self.root, cycle["cycle_id"])["manifest_digest"])
        # A finished publication with a damaged record is still refused: its own bytes are checked.
        record_path = Path(first["revision_dir"]) / P.REVISION_RECORD_NAME
        record = json.loads(record_path.read_text())
        record["spec_base_revision_id"] = "rrev_" + "5" * 32
        record_path.write_text(json.dumps(record))
        with self.assertRaises(P.ProducerError):
            self.admit(cycle, 0, base_revision="none")

    def test_a2_uncommitted_shared_journal_still_checks_stable_input(self):
        cycle = self._cycle([["a"], ["a"]])
        first = self.admit(cycle, 0, base_revision="none")
        with mock.patch.object(P, "_commit_shared", side_effect=RuntimeError("crash before commit")):
            with self.assertRaises(RuntimeError):
                self.admit(cycle, 1, base_revision=first["shared_reference_revision_id"])
        self.assertEqual(len(self._journals()), 1)
        latest = self._reference(first["shared_reference_id"])["latest_revision_id"]
        source_file = Path(cycle["cycle_dir"]) / "artifacts/gen1/a/prd.md"
        original = source_file.read_bytes()
        # The input the publication took no longer matches: it is not committed over a changed source.
        source_file.write_bytes(b"changed while the publication waited to commit")
        swept = P._recover_locked(self.root)
        self.assertTrue(swept["unresolved"], swept)
        self.assertEqual(self._reference(first["shared_reference_id"])["latest_revision_id"], latest)
        source_file.write_bytes(original)
        swept = P._recover_locked(self.root)
        self.assertEqual(swept["unresolved"], [])
        self.assertEqual(self._journals(), [])
        self.assertNotEqual(self._reference(first["shared_reference_id"])["latest_revision_id"], latest)


class A2CampaignCloseTest(SealAbolitionBase):
    def closed_campaign(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="a2-close")
        output = self.write_output(result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        return result, output, Path(result["cycle_dir"]).parent / "campaign.json"

    def test_a2_campaign_close_without_reason_after_edit(self):
        result, output, campaign = self.closed_campaign()
        record = json.loads(campaign.read_text())
        self.assertEqual(record["completion_criterion"]["statement"], CAMP.DEFAULT_COMPLETION_CRITERION)
        # A member's file was edited after its close, and a file added: neither stops the close.
        output.write_bytes(b"plan body, edited after the close\n")
        self.write_output(result, "plans/cycle/added.md", b"added\n")
        status = CAMP.status(self.root, campaign)
        self.assertNotIn("close_refusal", status)
        self.assertFalse(status["reason_required"])
        closed = CAMP.close(self.root, campaign)
        self.assertEqual(closed["status"], "satisfied")
        event = json.loads((campaign.parent / CAMP.EVENTS_DIR / "000001.json").read_text())
        self.assertEqual(event["payload"]["closure"]["reason"], CAMP.DEFAULT_COMPLETION_CRITERION)
        # The earlier fixed criterion sentence reads the same way.
        legacy = json.loads(campaign.read_text())
        CAMP.reopen(self.root, campaign, reason="legacy sentence check")
        legacy = json.loads(campaign.read_text())
        legacy["completion_criterion"] = {"statement": CAMP.LEGACY_DEFAULT_COMPLETION_CRITERION}
        P._write_campaign(self.root, legacy, exclusive=False)
        CAMP.close(self.root, campaign)
        event = json.loads((campaign.parent / CAMP.EVENTS_DIR / "000003.json").read_text())
        self.assertEqual(event["payload"]["closure"]["reason"], CAMP.LEGACY_DEFAULT_COMPLETION_CRITERION)
        # A reason given is recorded as given.
        CAMP.reopen(self.root, campaign, reason="again")
        CAMP.close(self.root, campaign, reason="explicit reason")
        event = json.loads((campaign.parent / CAMP.EVENTS_DIR / "000005.json").read_text())
        self.assertEqual(event["payload"]["closure"]["reason"], "explicit reason")

    def test_a2_open_route_is_the_only_close_refusal(self):
        result, output, campaign = self.closed_campaign()
        route, route_file = self.route(slug="a2-open-member", campaign_key="a2-close")
        member = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        status = CAMP.status(self.root, campaign)
        self.assertEqual(status["close_refusal"]["reason"], "campaign-cycle-provisional-active")
        # Once its route closes, the cycle that never closed is no longer in the way.
        self.close(route, route_file)
        status = CAMP.status(self.root, campaign)
        self.assertNotIn("close_refusal", status)
        rows = {row["cycle_id"]: row for row in status["cycles"]}
        self.assertEqual(rows[member["cycle_id"]]["state"], "open")
        self.assertIsNone(rows[member["cycle_id"]]["manifest_digest"])
        self.assertEqual(CAMP.close(self.root, campaign)["status"], "satisfied")


class FakeHistoryModule(types.ModuleType):
    """Same API shape as `artifact_history` (f0): make_event / publish_events_locked / publish_events."""

    class HistoryError(Exception):
        pass

    class HistoryPublishError(HistoryError):
        pass

    def __init__(self):
        super().__init__("artifact_history")
        self.made = []
        self.rejected = []
        self.published = []
        self.fail_publish = False
        self.on_publish = None

    def make_actor(self, *args, **kwargs):
        return H.make_actor(*args, **kwargs)

    def actor_from_env(self, *args, **kwargs):
        return H.actor_from_env(*args, **kwargs)

    def make_event(self, **kwargs):
        """The real recorder's shape check, so a line it would refuse fails the test instead of being dropped."""
        self.made.append(dict(kwargs))
        try:
            H.make_event(**kwargs)
        except H.HistoryError as exc:
            self.rejected.append((dict(kwargs), str(exc)))
            raise self.HistoryError(f"event-invalid:{exc}") from exc
        return {"event_id": kwargs.get("event_id") or "hevt_" + "0" * 32, **kwargs}

    def publish_events_locked(self, root, events):
        if not adm.holds_lock(Path(root)):
            raise self.HistoryError("admission-lock-required")
        if self.on_publish is not None:
            self.on_publish(root, events)
        if self.fail_publish:
            raise self.HistoryPublishError("simulated publish failure")
        directory = Path(root) / ".runtime/artifact-producer/v1/history/2026-10"
        directory.mkdir(parents=True, exist_ok=True)
        for event in events:
            path = directory / (event["event_id"] + ".jsonl")
            raw = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
            if path.exists():
                if path.read_bytes() != raw:
                    raise self.HistoryPublishError("conflict")
                continue
            path.write_bytes(raw)
            self.published.append(event)
        if events:
            (directory.parent / "LATEST.json").write_text(
                json.dumps({"event_id": events[-1]["event_id"], "count": len(self.published)}), encoding="utf-8")
        return [event["event_id"] for event in events]

    def publish_events(self, root, events, **_kwargs):
        fd = adm._acquire_lock(Path(root), 5.0)
        try:
            return self.publish_events_locked(root, events)
        finally:
            adm._release_lock(Path(root), fd)


class B1RefreshBase(SealAbolitionBase):
    """§45 D-124/D-125: the bounded refresh of a closed cycle."""

    def setUp(self):
        super().setUp()
        self.history = FakeHistoryModule()
        patcher = mock.patch.dict(sys.modules, {"artifact_history": self.history})
        patcher.start()
        self.addCleanup(patcher.stop)
        # A line the recorder would refuse is dropped by the producer without a sound; here it fails the test.
        self.addCleanup(lambda: self.assertEqual(self.history.rejected, []))
        # The per-cycle interval has its own test; the others observe a cycle again at once.
        interval = mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL": "0"})
        interval.start()
        self.addCleanup(interval.stop)

    def closed(self, campaign_key="b1-stream", files=None, activate=True):
        if activate:
            self.activate()
        route, route_file = self.route(slug=campaign_key, campaign_key=campaign_key)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for rel, data in (files or {"plans/cycle/plan.md": b"plan body\n"}).items():
            self.write_output(result, rel, data)
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed", sealed)
        # The close line has its own test; the refresh tests count only what a refresh sends.
        self.history.made.clear()
        self.history.published.clear()
        return result

    def edit(self, result, rel, data):
        (Path(result["cycle_dir"]) / "artifacts" / rel).write_bytes(data)

    def refresh(self, result, **kwargs):
        kwargs.setdefault("trigger", "turn-end")
        return P.refresh_cycle(self.root, result["cycle_id"], **kwargs)

    def manifest_path(self, result):
        return Path(result["cycle_dir"]) / "manifest.json"

    def snapshot_names(self, result):
        directory = L.manifest_snapshot_dir(self.root, result["cycle_id"])
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def tree_state(self, result):
        """(path -> (size, mtime_ns)) of everything a quiet refresh must leave alone."""
        cycle_id = result["cycle_id"]
        watched = [self.manifest_path(result), P.cycle_record_path(self.root, cycle_id), adm._index_path(self.root),
                   self.root / ".runtime/artifact-producer/v1/history",
                   L.manifest_snapshot_dir(self.root, cycle_id),
                   self.root / ".runtime/artifact-producer/v1/checkpoints"]
        state = {}
        for path in watched:
            for item in ([path] if path.is_file() else sorted(path.rglob("*")) if path.is_dir() else []):
                if item.is_file():
                    stat = item.stat()
                    state[str(item.relative_to(self.root))] = (stat.st_size, stat.st_mtime_ns)
        return state


class B1RefreshTest(B1RefreshBase):
    # -- A28-3 -----------------------------------------------------------
    def test_b1_taslp_25_plus26_modified4_and_noop(self):
        files = {f"plans/cycle/declared_{i:02d}.md": f"declared {i}\n".encode() for i in range(25)}
        result = self.closed("b1-taslp", files)
        cycle_id = result["cycle_id"]
        before = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        raw_before = self.manifest_path(result).read_bytes()
        record_before = P.read_cycle_record(self.root, cycle_id)
        for i in range(4):
            self.edit(result, f"plans/cycle/declared_{i:02d}.md", f"declared {i}, edited\n".encode())
        for i in range(26):
            self.write_output(result, f"plans/cycle/added_{i:02d}.md", f"added {i}\n".encode())
        out = self.refresh(result)
        self.assertEqual(out["status"], "emitted", out)
        after = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        self.assertEqual(len(after["artifact_revisions"]), 51)
        self.assertEqual(len(self.history.made), 30)
        self.assertEqual(len(self.history.published), 30)
        self.assertEqual(sorted(e["operation"] for e in self.history.published),
                         ["add"] * 26 + ["update"] * 4)
        kinds = {(e["kind"], e["target_type"], e["actor"]["by"]) for e in self.history.published}
        self.assertEqual(kinds, {("artifact", "artifact", "rule")})
        sample = next(e for e in self.history.published if e["operation"] == "update")
        self.assertTrue(sample["field"].startswith("artifacts/plans/cycle/declared_"))
        self.assertEqual(set(sample["before"]), {"digest", "bytes"})
        self.assertEqual(set(sample["after"]), {"digest", "bytes"})
        self.assertEqual(sample["reason"], "turn-end")
        # Rows that did not change are byte for byte the same; the close itself is not rewritten.
        old_rows = {r["locator"]["path"]: r for r in before["artifact_revisions"]}
        new_rows = {r["locator"]["path"]: r for r in after["artifact_revisions"]}
        for i in range(4, 25):
            rel = f"artifacts/plans/cycle/declared_{i:02d}.md"
            self.assertEqual(json.dumps(old_rows[rel], sort_keys=True), json.dumps(new_rows[rel], sort_keys=True))
            self.assertEqual(_row_bytes(before["artifacts"], "artifact_id", old_rows[rel]["artifact_id"]),
                             _row_bytes(after["artifacts"], "artifact_id", old_rows[rel]["artifact_id"]))
        for key in ("cycle", "routes", "manifest_id"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after["events"][:len(before["events"])], before["events"])
        record = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual((record["state"], record["cycle_state"], record["sealed_on"]),
                         ("sealed", "completed", record_before["sealed_on"]))
        # The earlier document stays as a preserved copy, byte for byte.
        copies = {p.name: p.read_bytes() for p in L.manifest_snapshot_dir(self.root, cycle_id).iterdir()}
        self.assertIn(raw_before, copies.values())
        self.assertEqual(len(copies), 2)
        self.assertTrue(adm.verify_index(self.root).ok)
        # The second refresh writes nothing at all.
        state = self.tree_state(result)
        made, published = len(self.history.made), len(self.history.published)
        quiet = self.refresh(result)
        self.assertEqual(quiet["status"], "unchanged", quiet)
        self.assertEqual(state, self.tree_state(result))
        self.assertEqual((made, published), (len(self.history.made), len(self.history.published)))

    def test_b1_noop_refresh_touches_nothing(self):
        result = self.closed("b1-noop", {"plans/cycle/a.md": b"a\n", "plans/cycle/b.md": b"b\n"})
        self.edit(result, "plans/cycle/b.md", b"b, edited\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        # A cycle named by the trigger leaves no bookkeeping behind when nothing changed.
        state = self.tree_state(result)
        for trigger in ("turn-end", "explicit", "supervisor-poll"):
            self.assertEqual(self.refresh(result, trigger=trigger)["status"], "unchanged")
        self.assertEqual(state, self.tree_state(result))
        latest = self.root / ".runtime/artifact-producer/v1/history/LATEST.json"
        self.assertTrue(latest.exists())
        stamp = latest.stat().st_mtime_ns
        self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertEqual(stamp, latest.stat().st_mtime_ns)
        # A change touches only the changed row.
        manifest = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        old_a = next(r for r in manifest["artifact_revisions"] if r["locator"]["path"].endswith("/a.md"))
        self.edit(result, "plans/cycle/b.md", b"b, edited again\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        again = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        new_a = next(r for r in again["artifact_revisions"] if r["locator"]["path"].endswith("/a.md"))
        self.assertEqual(json.dumps(old_a, sort_keys=True), json.dumps(new_a, sort_keys=True))
        self.assertEqual(len(self.snapshot_names(result)), 3)

    def test_b1_off_switch_pauses_closed_cycle_refresh(self):
        result = self.closed("b1-off", {"plans/cycle/a.md": b"a\n"})
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        with tempfile.TemporaryDirectory() as config:
            (Path(config) / "hearting").mkdir()
            switch = Path(config) / "hearting" / P.CLOSED_CYCLE_REFRESH_OFF_FILE
            switch.write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config}):
                state = self.tree_state(result)
                for trigger in ("turn-end", "explicit", "supervisor-poll"):
                    got = self.refresh(result, trigger=trigger)
                    self.assertEqual((got["status"], got["reason"]), ("skipped", "refresh-off"))
                self.assertEqual(P.refresh_sweep(self.root, trigger="turn-end")["status"], "off")
                self.assertEqual(state, self.tree_state(result))
                switch.unlink()
                self.assertEqual(self.refresh(result)["status"], "emitted")

    # -- correction B ----------------------------------------------------
    def test_b1_locked_index_read_once(self):
        result = self.closed("b1-index-read", {"plans/cycle/a.md": b"a\n"})
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        locked_reads, free_reads = [], []
        real_load = adm.load_index

        def counting(root):
            (locked_reads if adm.holds_lock(Path(root)) else free_reads).append(1)
            return real_load(root)

        with mock.patch.object(adm, "load_index", counting):
            self.assertEqual(self.refresh(result)["status"], "emitted")
        self.assertEqual(len(locked_reads), 1, locked_reads)
        locked_reads.clear()
        with mock.patch.object(adm, "load_index", counting):
            self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertLessEqual(len(locked_reads), 1, locked_reads)

    # -- lock choreography (D-124 "잠금") ----------------------------------
    def test_b1_scan_and_hash_hold_no_lock_and_publication_is_short(self):
        files = {f"plans/cycle/f{i}.md": f"f{i}\n".encode() for i in range(6)}
        result = self.closed("b1-locks", files)
        for i in range(3):
            self.edit(result, f"plans/cycle/f{i}.md", f"edited {i}\n".encode())
        seen = []
        real_stream = P._stream_file_facts

        def watching(path):
            seen.append((dispatch_lock_order.held(), adm.holds_lock(self.root)))
            fd = adm.try_acquire_lock(self.root)  # the admission lock is free to anyone while we hash
            self.assertIsNotNone(fd)
            adm._release_lock(self.root, fd)
            return real_stream(path)

        holds = []
        real_acquire, real_release = adm._acquire_lock, adm._release_lock
        started = {}

        def acquire(root, timeout, now=None):
            fd = real_acquire(root, timeout, now)
            started[fd] = time.monotonic()
            return fd

        def release(root, fd):
            holds.append(time.monotonic() - started.pop(fd, time.monotonic()))
            return real_release(root, fd)

        with mock.patch.object(P, "_stream_file_facts", watching), \
                mock.patch.object(adm, "_acquire_lock", acquire), mock.patch.object(adm, "_release_lock", release):
            self.assertEqual(self.refresh(result)["status"], "emitted")
        self.assertGreaterEqual(len(seen), 3)
        self.assertEqual({entry for entry in seen}, {((), False)})
        self.assertTrue(holds and max(holds) < 1.0, holds)

    def test_b1_history_goes_first_and_a_failed_history_leaves_the_manifest(self):
        result = self.closed("b1-history-first", {"plans/cycle/a.md": b"a\n"})
        raw = self.manifest_path(result).read_bytes()
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        order = []
        self.history.on_publish = lambda root, events: order.append(self.manifest_path(result).read_bytes() == raw)
        self.history.fail_publish = True
        out = self.refresh(result)
        self.assertEqual(out["status"], "skipped", out)
        self.assertEqual(out["reason"], "history-unavailable")
        self.assertEqual(self.manifest_path(result).read_bytes(), raw)
        self.assertEqual(len(self.snapshot_names(result)), 1)
        self.assertNotIn("history_pending", P.read_cycle_record(self.root, result["cycle_id"]))
        # The next trigger finds the same change again and publishes it.
        self.history.fail_publish = False
        self.assertEqual(self.refresh(result)["status"], "emitted")
        self.assertEqual(order, [True, True])
        self.assertNotEqual(self.manifest_path(result).read_bytes(), raw)
        self.assertEqual(len(self.history.published), 1)

    def test_b1_missing_recorder_keeps_lines_pending_and_the_next_trigger_delivers(self):
        result = self.closed("b1-pending", {"plans/cycle/a.md": b"a\n", "plans/cycle/b.md": b"b\n"})
        cycle_id = result["cycle_id"]
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        self.write_output(result, "plans/cycle/c.md", b"c\n")
        with mock.patch.dict(sys.modules, {"artifact_history": None}):  # import fails: not merged yet
            out = self.refresh(result)
            self.assertEqual(out["status"], "emitted", out)
            pending = P.read_cycle_record(self.root, cycle_id)["history_pending"]
            self.assertEqual(len(pending), 2)
            self.assertEqual({entry["operation"] for entry in pending}, {"add", "update"})
            for entry in pending:
                self.assertRegex(entry["event_id"], r"^hevt_[0-9a-f]{32}$")
                self.assertRegex(entry["transaction_id"], r"^htxn_[0-9a-f]{32}$")
            # Still no recorder: nothing is lost and nothing is rewritten.
            self.assertEqual(self.refresh(result)["status"], "unchanged")
            self.assertEqual(len(P.read_cycle_record(self.root, cycle_id)["history_pending"]), 2)
        # The recorder appears but fails: the lines stay.
        self.history.fail_publish = True
        self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertEqual(len(P.read_cycle_record(self.root, cycle_id)["history_pending"]), 2)
        self.history.fail_publish = False
        out = self.refresh(result)
        self.assertEqual(out["status"], "unchanged")
        self.assertEqual(len(self.history.published), 2)
        self.assertEqual({e["event_id"] for e in self.history.published}, {e["event_id"] for e in pending})
        self.assertNotIn("history_pending", P.read_cycle_record(self.root, cycle_id))
        state = self.tree_state(result)
        self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertEqual(state, self.tree_state(result))

    def test_b1_pending_survives_a_crash_after_the_manifest(self):
        result = self.closed("b1-crash-pending", {"plans/cycle/a.md": b"a\n"})
        cycle_id = result["cycle_id"]
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        with mock.patch.dict(sys.modules, {"artifact_history": None}):
            with self.assertRaises(adm.AdmissionRecoveryRequired):
                self.refresh(result, crash_after_manifest=True)
            P.recover(self.root)
            self.assertEqual(len(P.read_cycle_record(self.root, cycle_id).get("history_pending", [])), 1)
        self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertEqual(len(self.history.published), 1)

    def test_b1_first_close_records_one_line_or_leaves_it_pending(self):
        self.activate()
        route, route_file = self.route(slug="b1-close", campaign_key="b1-close")
        first = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(first)
        self.close(route, route_file)
        self.history.fail_publish = True  # a failing recorder never fails the close
        self.assertEqual(P.finalize(self.root, cycle_id=first["cycle_id"])["status"], "sealed")
        record = P.read_cycle_record(self.root, first["cycle_id"])
        self.assertEqual(len(record["history_pending"]), 1)
        line = record["history_pending"][0]
        self.assertEqual((line["kind"], line["target_type"], line["field"], line["operation"]),
                         ("lifecycle", "cycle", "state", "update"))
        document = json.loads(self.manifest_path(first).read_text(encoding="utf-8"))
        value = line["after"]["value"]
        self.assertEqual(set(value), {"state", "manifest_digest", "revision_id", "files", "excluded"})
        self.assertEqual((value["state"], value["manifest_digest"], value["revision_id"], value["files"]),
                         ("completed", m_digest(document), document["manifest_revision_id"], 1))
        self.history.fail_publish = False
        self.assertEqual(self.refresh(first)["status"], "unchanged")
        self.assertEqual([e["field"] for e in self.history.published], ["state"])
        self.assertNotIn("history_pending", P.read_cycle_record(self.root, first["cycle_id"]))
        # With the recorder present the close line is published at once.
        route2, route_file2 = self.route(slug="b1-close-two", campaign_key="b1-close")
        second = P.begin(self.root, route_file=route_file2, capability="autopilot-code", intensity="direct")
        self.write_output(second)
        self.close(route2, route_file2)
        P.finalize(self.root, cycle_id=second["cycle_id"])
        self.assertEqual(len(self.history.published), 2)
        self.assertNotIn("history_pending", P.read_cycle_record(self.root, second["cycle_id"]))

    # -- A28-4 -----------------------------------------------------------
    def test_b1_delete_everything_and_edit_twice_through_the_bounded_path(self):
        files = {"plans/cycle/plan.md": b"plan\n", "plans/cycle/notes.md": b"notes\n"}
        result = self.closed("b1-delete", files)
        cycle_id = result["cycle_id"]
        artifacts = Path(result["cycle_dir"]) / "artifacts"
        self.edit(result, "plans/cycle/notes.md", b"notes 1\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        self.edit(result, "plans/cycle/notes.md", b"notes 2\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        self.assertEqual(len(self.snapshot_names(result)), 3)
        before = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        required = next(r for r in before["artifact_revisions"] if r["locator"]["path"].endswith("/plan.md"))
        (artifacts / "plans/cycle/plan.md").unlink()
        out = self.refresh(result)
        self.assertEqual(out["changes"]["removed"], ["artifacts/plans/cycle/plan.md"])
        self.assertEqual([e["operation"] for e in self.history.published[-1:]], ["delete"])
        self.assertEqual(self.history.published[-1]["after"], {"value": None})
        (artifacts / "plans/cycle/notes.md").unlink()
        self.write_output(result, "plans/cycle/.cache/blob", b"hidden")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        after = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        self.assertEqual(after["artifact_revisions"], [])
        self.assertEqual(after["events"][:len(before["events"])], before["events"])
        self.assertNotIn(".cache", json.dumps(after))
        self.assertNotIn(".cache", json.dumps(self.history.published))
        earlier = [json.loads(raw) for raw in
                   (p.read_text(encoding="utf-8") for p in L.manifest_snapshot_dir(self.root, cycle_id).iterdir())]
        self.assertTrue(M.validate_update(after, preserved=earlier, previous=before).ok)
        index = adm.load_index(self.root)
        self.assertIn(required["artifact_id"], index.stable_ids)
        rebuilt = adm.rebuild_index(self.root)
        self.assertEqual(artifact_index.canonical_bytes(rebuilt), artifact_index.canonical_bytes(adm.load_index(self.root)))
        self.assertIn(required["artifact_id"], rebuilt.stable_ids)
        # An empty file list is still a closed cycle; a later file is simply added.
        self.write_output(result, "plans/cycle/again.md", b"again\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")

    # -- A28-9 -----------------------------------------------------------
    def test_b1_cycle_closed_by_an_older_release_refreshes_without_preparation(self):
        result = self.closed("b1-old-release", {"plans/cycle/a.md": b"a\n"})
        raw = self.manifest_path(result).read_bytes()
        shutil.rmtree(L.manifest_snapshot_dir(self.root, result["cycle_id"]))
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        out = self.refresh(result)
        self.assertEqual(out["status"], "emitted", out)
        copies = {p.name: p.read_bytes() for p in L.manifest_snapshot_dir(self.root, result["cycle_id"]).iterdir()}
        self.assertIn(raw, copies.values())
        self.assertEqual(len(copies), 2)
        self.assertTrue(adm.verify_index(self.root).ok)

    def test_b1_a_file_changed_after_the_scan_skips_this_publication_only(self):
        result = self.closed("b1-recheck", {"plans/cycle/a.md": b"a\n"})
        raw = self.manifest_path(result).read_bytes()
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        real_lock = P._refresh_lock

        def lock_after_a_write(root, cycle_id, *, timeout):
            self.edit(result, "plans/cycle/a.md", b"a, edited again while we scanned\n")
            return real_lock(root, cycle_id, timeout=timeout)

        with mock.patch.object(P, "_refresh_lock", lock_after_a_write):
            out = self.refresh(result)
        self.assertEqual((out["status"], out["reason"]), ("skipped", "superseded"))
        self.assertEqual(self.manifest_path(result).read_bytes(), raw)
        self.assertEqual(self.history.published, [])
        self.assertEqual(self.refresh(result)["status"], "emitted")

    def test_b1_a_busy_refresh_lock_defers_without_an_error(self):
        result = self.closed("b1-busy", {"plans/cycle/a.md": b"a\n"})
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        with P._refresh_lock(self.root, result["cycle_id"], timeout=0.0):
            out = self.refresh(result)
        self.assertEqual((out["status"], out["reason"]), ("skipped", "busy"))
        self.assertEqual(self.refresh(result)["status"], "emitted")

    def test_b1_unknown_and_open_cycles_are_skipped_not_failed(self):
        self.activate()
        route, route_file, opened = self.begin(campaign_key="b1-open")
        self.assertEqual(self.refresh(opened)["reason"], "cycle-not-closed")
        self.assertEqual(P.refresh_cycle(self.root, "cyc_" + "5" * 32)["reason"], "cycle-unknown")

    # -- budget (D-124 "비용 예산") ----------------------------------------
    def test_b1_walk_budget_leaves_a_cursor_and_never_reads_the_rest_as_deleted(self):
        files = {f"plans/cycle/f{i:02d}.md": f"f{i}\n".encode() for i in range(30)}
        result = self.closed("b1-walk", files)
        for i in range(30):
            self.edit(result, f"plans/cycle/f{i:02d}.md", f"f{i} edited\n".encode())
        (Path(result["cycle_dir"]) / "artifacts/plans/cycle/f29.md").unlink()
        runs, removed = 0, []
        while True:
            runs += 1
            out = self.refresh(result, budget=P.RefreshBudget(max_walk_entries=12))
            removed.extend(out.get("changes", {}).get("removed", []))
            if out.get("complete"):
                break
            self.assertEqual(out["status"], "emitted", out)
            self.assertIsNotNone(out["cursor"])
            self.assertLess(runs, 12)
        self.assertGreaterEqual(runs, 3)
        # Only the file whose absence was looked at directly leaves the rows, once.
        self.assertEqual(removed, ["artifacts/plans/cycle/f29.md"])
        document = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        rows = {r["locator"]["path"]: r for r in document["artifact_revisions"]}
        self.assertEqual(len(rows), 29)
        for i in range(29):
            data = f"f{i} edited\n".encode()
            self.assertEqual(rows[f"artifacts/plans/cycle/f{i:02d}.md"]["content_digest"],
                             "sha256:" + hashlib.sha256(data).hexdigest())
        self.assertTrue(adm.verify_index(self.root).ok)
        # Everything seen, nothing left to do: a full pass is quiet again.
        self.assertEqual(self.refresh(result, budget=P.RefreshBudget(max_walk_entries=12))["status"], "unchanged")

    def test_b1_hash_budget_stops_between_files_and_a_changed_file_is_always_finished(self):
        files = {f"plans/cycle/f{i}.bin": bytes([i]) * (600 * 1024) for i in range(4)}
        result = self.closed("b1-hash", files)
        for i in range(4):
            self.edit(result, f"plans/cycle/f{i}.bin", bytes([i + 10]) * (600 * 1024))
        out = self.refresh(result, budget=P.RefreshBudget(max_hash_bytes=1024 * 1024))
        self.assertFalse(out["complete"], out)
        self.assertLessEqual(len(out["changes"]["modified"]), 2)
        self.assertGreaterEqual(len(out["changes"]["modified"]), 1)
        for _ in range(6):
            if self.refresh(result, budget=P.RefreshBudget(max_hash_bytes=1024 * 1024)).get("complete"):
                break
        document = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        for row in document["artifact_revisions"]:
            index = int(row["locator"]["path"].split("/f")[-1].split(".")[0])
            self.assertEqual(row["content_digest"],
                             "sha256:" + hashlib.sha256(bytes([index + 10]) * (600 * 1024)).hexdigest())

    def test_b1_a_300_mib_file_is_reflected_in_one_run_by_streaming(self):
        result = self.closed("b1-300mib", {"plans/cycle/small.md": b"small\n", "plans/cycle/model.bin": b"x\n"})
        big = Path(result["cycle_dir"]) / "artifacts/plans/cycle/model.bin"
        size = 300 * 1024 * 1024
        with open(big, "wb") as handle:
            handle.truncate(size)
        expected = hashlib.sha256()
        zeros = bytes(1024 * 1024)
        for _ in range(300):
            expected.update(zeros)
        tracemalloc.start()
        try:
            out = self.refresh(result)  # the default budget is 256 MiB: smaller than this one file
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(out["status"], "emitted", out)
        self.assertLess(peak, 64 * 1024 * 1024, peak)
        document = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        row = next(r for r in document["artifact_revisions"] if r["locator"]["path"].endswith("model.bin"))
        self.assertEqual((row["content_digest"], row["byte_size"]), ("sha256:" + expected.hexdigest(), size))

    def test_b1_15300_temporary_files_finish_inside_the_budget_and_never_reach_history(self):
        result = self.closed("b1-temp", {"plans/cycle/plan.md": b"plan\n"})
        tmp = Path(result["cycle_dir"]) / "artifacts/plans/cycle/scratch"
        tmp.mkdir(parents=True)
        for i in range(15300):
            (tmp / f"t{i}.tmp").write_bytes(b"")
        cache = Path(result["cycle_dir"]) / "artifacts/plans/cycle/.pytest_cache"
        cache.mkdir()
        (cache / "x").write_bytes(b"x")
        self.edit(result, "plans/cycle/plan.md", b"plan, edited\n")
        started = time.monotonic()
        out = self.refresh(result)
        self.assertLess(time.monotonic() - started, 60.0)
        self.assertEqual(out["status"], "emitted", out)
        self.assertTrue(out["complete"])
        self.assertLessEqual(out["walked"], 20000)
        self.assertGreaterEqual(out["walked"], 15300)
        self.assertEqual([e["field"] for e in self.history.published], ["artifacts/plans/cycle/plan.md"])
        self.assertNotIn(".tmp", json.dumps(json.loads(self.manifest_path(result).read_text(encoding="utf-8"))))
        # A tree larger than the walk budget stops and continues, and never asks for a lock meanwhile.
        out = self.refresh(result, budget=P.RefreshBudget(max_walk_entries=5000))
        self.assertFalse(out["complete"])
        self.assertIsNotNone(out["cursor"])

    # -- root sweep (D-124 "순환 커서") ------------------------------------
    def test_b1_sweep_cursor_observes_every_closed_cycle_with_no_cutoff(self):
        old = {}
        for i in range(14):
            old[i] = self.closed(f"b1-sweep-{i:02d}", {"plans/cycle/plan.md": b"plan\n"}, activate=(i == 0))
        # Three of them were closed more than three days ago.
        for i in (0, 5, 13):
            record = P.read_cycle_record(self.root, old[i]["cycle_id"])
            record["sealed_on"] = "2026-09-20T00:00:00Z"
            P._write_cycle_record(self.root, record, exclusive=False)
        for i in range(14):
            self.edit(old[i], "plans/cycle/plan.md", f"plan edited {i}\n".encode())
        env = mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL": "0"})
        env.start()
        self.addCleanup(env.stop)
        calls = 0
        while calls < 60:
            calls += 1
            out = P.refresh_sweep(self.root, trigger="turn-end", budget=P.RefreshBudget(max_walk_entries=7))
            if all(json.loads(self.manifest_path(old[i]).read_text(encoding="utf-8"))["artifact_revisions"][0][
                    "content_digest"] == "sha256:" + hashlib.sha256(f"plan edited {i}\n".encode()).hexdigest()
                   for i in range(14)):
                break
        else:
            self.fail("the cursor did not reach every closed cycle")
        self.assertGreater(calls, 2)
        cursor = self.root / ".runtime/artifact-producer/v1/checkpoints/refresh/cursor.json"
        self.assertTrue(cursor.is_file())
        # The cycle a trigger names is seen first, whatever the cursor says.
        self.edit(old[3], "plans/cycle/plan.md", b"named cycle edited\n")
        self.edit(old[12], "plans/cycle/plan.md", b"far cycle edited\n")
        out = P.refresh_sweep(self.root, trigger="turn-end", first_cycle_id=old[12]["cycle_id"],
                              budget=P.RefreshBudget(max_walk_entries=4))
        self.assertEqual(out["refreshed"][0], old[12]["cycle_id"])

    def test_b1_checkpoint_command_reaches_a_closed_cycle_and_the_sweep(self):
        env = mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL": "0"})
        env.start()
        self.addCleanup(env.stop)
        one = self.closed("b1-cp-one", {"plans/cycle/plan.md": b"plan\n"})
        two = self.closed("b1-cp-two", {"plans/cycle/plan.md": b"plan\n"}, activate=False)
        self.edit(one, "plans/cycle/plan.md", b"one edited\n")
        self.edit(two, "plans/cycle/plan.md", b"two edited\n")
        out = P.checkpoint(self.root, cycle_id=one["cycle_id"], trigger="explicit")
        self.assertEqual((out["status"], out["cycle_id"]), ("emitted", one["cycle_id"]))
        # An automatic trigger sees its own cycle and then the rest of the root.
        self.edit(one, "plans/cycle/plan.md", b"one edited again\n")
        out = P.checkpoint(self.root, cycle_id=one["cycle_id"], trigger="turn-end")
        self.assertEqual(out["status"], "emitted")
        self.assertIn(two["cycle_id"], out["refresh"]["refreshed"])

    # -- explicit callers ------------------------------------------------
    def test_b1_explicit_reclose_scans_before_it_takes_the_lock(self):
        result = self.closed("b1-reclose", {"plans/cycle/a.md": b"a\n", "plans/cycle/b.md": b"b\n"})
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        hashed = []
        real_stream = P._stream_file_facts

        def watching(path):
            hashed.append(adm.holds_lock(self.root))
            return real_stream(path)

        with mock.patch.object(P, "_stream_file_facts", watching):
            out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertTrue(out["refreshed"], out)
        self.assertTrue(hashed)
        self.assertEqual(set(hashed), {False})

    def test_b1_cursor_walks_nested_directories_without_skipping_or_repeating(self):
        files = {f"plans/d{d}/sub/f{i}.md": f"d{d} f{i}\n".encode() for d in range(5) for i in range(4)}
        files.update({f"plans/d{d}.md": f"top {d}\n".encode() for d in range(5)})
        result = self.closed("b1-nested", files)
        for rel in files:
            self.edit(result, rel, b"edited " + rel.encode())
        hashed = []
        real_stream = P._stream_file_facts

        def counting(path):
            hashed.append(Path(path).relative_to(result["cycle_dir"]).as_posix())
            return real_stream(path)

        with mock.patch.object(P, "_stream_file_facts", counting):
            for _ in range(30):
                if self.refresh(result, budget=P.RefreshBudget(max_walk_entries=6)).get("complete"):
                    break
            else:
                self.fail("the walk never completed")
        self.assertEqual(sorted(hashed), sorted("artifacts/" + rel for rel in files))  # each file read exactly once
        document = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        for row in document["artifact_revisions"]:
            rel = row["locator"]["path"][len("artifacts/"):]
            self.assertEqual(row["content_digest"], "sha256:" + hashlib.sha256(b"edited " + rel.encode()).hexdigest())
        self.assertEqual(len(document["artifact_revisions"]), len(files))

    def test_b1_failures_never_fail_the_command_that_triggered_the_refresh(self):
        result = self.closed("b1-nofail", {"plans/cycle/a.md": b"a\n"})
        raw = self.manifest_path(result).read_bytes()
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        # Whatever goes wrong inside a refresh is a skipped result, not an exception.
        with mock.patch.object(P, "_bounded_scan", side_effect=P.ProducerError("artifacts-dir-missing", "x")):
            out = self.refresh(result)
        self.assertEqual((out["status"], out["reason"]), ("skipped", "artifacts-dir-missing"))
        with mock.patch.object(adm, "_acquire_lock", side_effect=adm.AdmissionBusy("busy")):
            out = self.refresh(result)
        self.assertEqual((out["status"], out["reason"]), ("skipped", "busy"))
        self.assertEqual(self.manifest_path(result).read_bytes(), raw)
        # The checkpoint command a trigger runs returns a result too.
        with mock.patch.object(P, "refresh_cycle", side_effect=RuntimeError("boom")):
            sweep = P.refresh_sweep(self.root, trigger="turn-end")
        self.assertEqual(sweep["refreshed"], [])
        # A recorder that raises never fails an explicit re-close: the lines wait in the record.
        self.history.fail_publish = True
        out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertTrue(out["refreshed"], out)
        self.assertEqual(len(P.read_cycle_record(self.root, result["cycle_id"])["history_pending"]), 1)
        self.history.fail_publish = False
        self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertEqual(len(self.history.published), 1)
        self.assertNotIn("history_pending", P.read_cycle_record(self.root, result["cycle_id"]))

    def test_b1_real_history_recorder_takes_the_same_lines(self):
        module = H
        with mock.patch.dict(sys.modules, {"artifact_history": module}):
            result = self.closed("b1-real-recorder", {"plans/cycle/a.md": b"a\n"})
            self.edit(result, "plans/cycle/a.md", b"a, edited\n")
            self.write_output(result, "plans/cycle/b.md", b"b\n")
            self.assertEqual(self.refresh(result)["status"], "emitted")
            events = list(module.iter_events(self.root))
            self.assertEqual(sorted((e["kind"], e["operation"], e["field"]) for e in events),
                             [("artifact", "add", "artifacts/plans/cycle/b.md"),
                              ("artifact", "update", "artifacts/plans/cycle/a.md"),
                              ("lifecycle", "update", "state")])
            self.assertTrue((self.root / ".runtime/artifact-producer/v1/history/LATEST.json").is_file())
            self.assertNotIn("history_pending", P.read_cycle_record(self.root, result["cycle_id"]))

    def test_b1_automatic_trigger_waits_for_the_interval_after_a_publication(self):
        result = self.closed("b1-interval", {"plans/cycle/a.md": b"a\n"})
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL": "900"}):
            self.edit(result, "plans/cycle/a.md", b"a, edited\n")
            self.assertEqual(self.refresh(result, now=1_000_000.0)["status"], "emitted")
            self.edit(result, "plans/cycle/a.md", b"a, edited twice\n")
            out = self.refresh(result, now=1_000_100.0)
            self.assertEqual((out["status"], out["reason"]), ("skipped", "min-interval"))
            self.assertEqual(self.refresh(result, now=1_000_100.0, trigger="explicit")["status"], "emitted")
            self.edit(result, "plans/cycle/a.md", b"a, edited three times\n")
            self.assertEqual(self.refresh(result, now=1_001_200.0)["status"], "emitted")

    def test_b1_explicit_refresh_ignores_the_interval_and_the_budget(self):
        result = self.closed("b1-explicit", {"plans/cycle/a.md": b"a\n"})
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        self.assertEqual(self.refresh(result, trigger="explicit")["status"], "emitted")
        self.edit(result, "plans/cycle/a.md", b"a, edited twice\n")
        out = self.refresh(result, trigger="explicit")
        self.assertEqual(out["status"], "emitted", out)
        self.assertTrue(out["complete"])


class B2TriggerTest(B1RefreshBase):
    """§45 D-124 trigger parity: every harness and `begin` start the same checkpoint child."""

    HOOKS = Path(__file__).resolve().parents[1]

    def setUp(self):
        super().setUp()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.state_home = state.name
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": state.name})
        env.start()
        self.addCleanup(env.stop)

    def popen(self):
        """Patch the launcher so a trigger records its child instead of starting one."""
        # Only the launcher's own `subprocess` name is replaced: the module is shared, and the
        # fixtures run git through it.
        in_test = mock.patch.object(TRIG, "in_test_process", return_value=False)
        fake = mock.patch.object(TRIG, "subprocess")
        in_test.start()
        self.addCleanup(in_test.stop)
        started = fake.start()
        self.addCleanup(fake.stop)
        return started.Popen

    def child_argvs(self, popen):
        return [call.args[0][1:] for call in popen.call_args_list]

    def checkpoint_argv(self, trigger, root, cycle_id):
        return [str(TRIG.PRODUCER), "checkpoint", "--trigger", trigger, "--artifact-root", str(root),
                "--cycle", cycle_id]

    # -- begin / compose --------------------------------------------------
    def test_b2_begin_observes_the_parent_and_the_cycle_it_continues(self):
        parent = self.closed("b2-begin", {"plans/cycle/plan.md": b"plan\n"})
        self.edit(parent, "plans/cycle/plan.md", b"plan, edited after the close\n")
        popen = self.popen()
        interval = mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL": "900"})
        interval.start()
        self.addCleanup(interval.stop)
        child_route, child_file = self.route(slug="b2-child", parent_cycle_id=parent["cycle_id"])
        child = P.begin(self.root, route_file=child_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(self.child_argvs(popen), [self.checkpoint_argv("begin", self.root, parent["cycle_id"])])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        # The child that begin started is the ordinary checkpoint command: it refreshes the closed parent.
        out = subprocess.run([sys.executable] + self.child_argvs(popen)[0], capture_output=True, text=True,
                             env={**os.environ, "AGENT_ARTIFACT_CHECKPOINT": "off"}, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        document = json.loads(self.manifest_path(parent).read_text(encoding="utf-8"))
        self.assertEqual(document["artifact_revisions"][0]["content_digest"],
                         "sha256:" + hashlib.sha256(b"plan, edited after the close\n").hexdigest())
        # A continuing begin on the same route sees the cycle it resumes, once per interval.
        popen.reset_mock()
        again = P.begin(self.root, route_file=child_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(again["status"], "resumed")
        self.assertEqual(self.child_argvs(popen),
                         [self.checkpoint_argv("begin", self.root, child["cycle_id"])])
        popen.reset_mock()
        P.begin(self.root, route_file=child_file, capability="autopilot-code", intensity="direct")
        popen.assert_not_called()

    def test_b2_begin_never_waits_on_or_fails_because_of_the_observation(self):
        parent = self.closed("b2-quiet", {"plans/cycle/plan.md": b"plan\n"})
        popen = self.popen()
        popen.side_effect = OSError("no process slots")
        route, route_file = self.route(slug="b2-quiet-child", parent_cycle_id=parent["cycle_id"])
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(result["layout"], "cycle")
        self.assertTrue(popen.called)  # it was tried, and the failure stayed inside the launcher
        with mock.patch.object(TRIG, "launch", side_effect=RuntimeError("launcher broke")) as launch:
            route, route_file = self.route(slug="b2-quiet-two", parent_cycle_id=parent["cycle_id"])
            result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(result["layout"], "cycle")
        launch.assert_called()
        # The observation is silent when automatic checkpoints are off.
        popen.reset_mock(side_effect=True)
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT": "off"}):
            route, route_file = self.route(slug="b2-quiet-three", parent_cycle_id=parent["cycle_id"])
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        popen.assert_not_called()
        # A begin with no parent and no earlier cycle has nothing to observe.
        route, route_file = self.route(slug="b2-quiet-fresh", campaign_key="b2-fresh-stream")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home + "/fresh"}):
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        popen.assert_not_called()

    # -- the three harnesses ---------------------------------------------
    def _hook(self, relative, name):
        spec = importlib.util.spec_from_file_location(name, self.HOOKS / relative)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _fire(self, module, payload, env):
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            return module.main()

    def test_b2_three_harness_triggers_and_root_cursor(self):
        result = self.closed("b2-harness", {"plans/cycle/plan.md": b"plan\n"})
        cycle_id, root = result["cycle_id"], str(self.root)
        worker_env = {"AGENT_ARTIFACT_ROOT": root, "AGENT_ARTIFACT_CYCLE_ID": cycle_id,
                      "AGENT_SESSION_ROLE": "worker"}
        route_file = str(self.root / "rt-b2.json")
        popen = self.popen()
        claude = self._hook("hooks/open-cycle-checkpoint.py", "b2_claude_stop")
        sys.path.insert(0, str(self.HOOKS / "tools"))
        self.addCleanup(sys.path.remove, str(self.HOOKS / "tools"))
        codex = self._hook("adapters/codex/hooks/stop-lifecycle.py", "b2_codex_stop")
        seen_sessions = []

        def lookup(harness, session_id):
            seen_sessions.append((harness, session_id))
            return root, route_file

        with mock.patch.object(TRIG, "session_route", side_effect=lookup), \
                mock.patch("session_summary_trigger.launch_trigger"), mock.patch("fleet.interaction.clear_wait"):
            for harness, module in (("claude", claude), ("codex", codex)):
                # A dispatched worker names its cycle in its environment ...
                popen.reset_mock()
                self.assertEqual(self._fire(module, {"session_id": f"sid-{harness}-w"}, worker_env), 0)
                self.assertEqual(self.child_argvs(popen), [self.checkpoint_argv("turn-end", root, cycle_id)])
                self.assertEqual(popen.call_args.kwargs["env"]["AGENT_ARTIFACT_CYCLE_ID"], cycle_id)
                # ... an interactive session is found from its session id on stdin.
                popen.reset_mock()
                self.assertEqual(self._fire(module, {"session_id": f"sid-{harness}-i"}, {}), 0)
                self.assertEqual(self.child_argvs(popen),
                                 [[str(TRIG.PRODUCER), "checkpoint", "--trigger", "turn-end",
                                   "--artifact-root", root, "--route", route_file]])
                # The off switch silences the hook without failing the turn.
                popen.reset_mock()
                self.assertEqual(self._fire(module, {"session_id": f"sid-{harness}-o"},
                                            {**worker_env, "AGENT_ARTIFACT_CHECKPOINT": "off"}), 0)
                popen.assert_not_called()
        self.assertEqual(seen_sessions, [("claude", "sid-claude-i"), ("codex", "sid-codex-i")])

    def test_b2_opencode_session_idle_runs_the_same_trigger(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        plugin = self.HOOKS / "adapters/opencode/plugins/hearting-guards.js"
        scratch = Path(self.state_home) / "opencode"
        (scratch / "bin").mkdir(parents=True)
        shim = scratch / "bin" / "python3"
        shim.write_text(
            "#!/bin/sh\nd=\"$FAKE_PYTHON_LOG/$$\"; mkdir -p \"$d\"; printf '%s\\n' \"$@\" > \"$d/argv\"\n"
            "env > \"$d/env\"; cat > \"$d/stdin\"\n", encoding="utf-8")
        shim.chmod(0o755)
        script = scratch / "fire.mjs"
        script.write_text(
            f'import {{ AgentHarnessGuards }} from {json.dumps(plugin.as_uri())}\n'
            'const hooks = await AgentHarnessGuards({ directory: process.cwd(), worktree: process.cwd() })\n'
            'await hooks.event({ event: { type: "session.idle", properties: { sessionID: process.env.B2_SID } } })\n'
            'await new Promise((resolve) => setTimeout(resolve, 1500))\n', encoding="utf-8")

        def fire(sid, extra):
            log = scratch / f"log-{sid}"
            log.mkdir()
            env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENT_", "HERDR_"))}
            env.update({"PATH": f"{scratch / 'bin'}{os.pathsep}{env.get('PATH', '')}", "B2_SID": sid,
                        "FAKE_PYTHON_LOG": str(log), **extra})
            done = subprocess.run([node, str(script)], cwd=str(scratch), env=env, capture_output=True, text=True,
                                  timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            calls = []
            for entry in sorted(log.iterdir()):
                argv = (entry / "argv").read_text(encoding="utf-8").splitlines()
                if any(arg.endswith("artifact_checkpoint_trigger.py") for arg in argv):
                    calls.append({"argv": argv, "stdin": (entry / "stdin").read_text(encoding="utf-8"),
                                  "env": (entry / "env").read_text(encoding="utf-8").splitlines()})
            return calls

        trigger = str(self.HOOKS / "utilities" / "artifact_checkpoint_trigger.py")
        # An interactive session: the session id travels on stdin.
        calls = fire("sid-main", {})
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["argv"], [trigger, "turn-end", "--harness", "opencode"])
        self.assertEqual(json.loads(calls[0]["stdin"])["sessionID"], "sid-main")
        # A worker is a target too, and its cycle environment reaches the child.
        root, cycle_id = str(self.root), "cyc_" + "d" * 32
        calls = fire("sid-worker", {"AGENT_SESSION_ROLE": "worker", "AGENT_ARTIFACT_ROOT": root,
                                    "AGENT_ARTIFACT_CYCLE_ID": cycle_id})
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["argv"], [trigger, "turn-end", "--harness", "opencode"])
        self.assertIn(f"AGENT_ARTIFACT_CYCLE_ID={cycle_id}", calls[0]["env"])
        self.assertIn(f"AGENT_ARTIFACT_ROOT={root}", calls[0]["env"])
        # The existing off switch is respected before anything starts.
        self.assertEqual(fire("sid-off", {"AGENT_ARTIFACT_CHECKPOINT": "off"}), [])

    def test_b2_twelve_old_cycles_round_robin_and_interval(self):
        cycles = []
        for i in range(13):
            cycles.append(self.closed(f"b2-rr-{i:02d}", {"plans/cycle/plan.md": b"plan\n"}, activate=(i == 0)))
        for i in (0, 6, 12):  # closed more than three days ago
            record = P.read_cycle_record(self.root, cycles[i]["cycle_id"])
            record["sealed_on"] = "2026-09-20T00:00:00Z"
            P._write_cycle_record(self.root, record, exclusive=False)
        for i, result in enumerate(cycles):
            self.edit(result, "plans/cycle/plan.md", f"edited {i}\n".encode())
        real = P.RefreshBudget

        class Small(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs) if args or kwargs else super().__init__(max_walk_entries=6)

        def current(result):
            return json.loads(self.manifest_path(result).read_text(encoding="utf-8"))["artifact_revisions"][0][
                "content_digest"]

        wanted = ["sha256:" + hashlib.sha256(f"edited {i}\n".encode()).hexdigest() for i in range(13)]
        triggers = ("stage-complete", "supervisor-poll", "turn-end", "begin")
        with mock.patch.object(P, "RefreshBudget", Small):
            for call in range(80):
                out = P.checkpoint(self.root, cycle_id=cycles[0]["cycle_id"], trigger=triggers[call % 4])
                self.assertNotEqual(out.get("reason"), "checkpoint-trigger-invalid")
                if [current(result) for result in cycles] == wanted:
                    break
            else:
                self.fail("the root cursor did not reach every closed cycle")
        self.assertGreater(call, 1)
        # The 900 second interval holds for every automatic trigger and never for an explicit one.
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL": "900"}):
            self.edit(cycles[4], "plans/cycle/plan.md", b"edited again\n")
            stamp = time.time() + 1000  # past the last observation of this cycle
            first = P.checkpoint(self.root, cycle_id=cycles[4]["cycle_id"], trigger="begin", now=stamp)
            self.assertEqual(first["status"], "emitted", first)
            self.edit(cycles[4], "plans/cycle/plan.md", b"edited a third time\n")
            quiet = P.checkpoint(self.root, cycle_id=cycles[4]["cycle_id"], trigger="begin", now=stamp + 60)
            self.assertEqual((quiet["status"], quiet["reason"]), ("skipped", "min-interval"))
            explicit = P.checkpoint(self.root, cycle_id=cycles[4]["cycle_id"], trigger="explicit", now=stamp + 60)
            self.assertEqual(explicit["status"], "emitted")


TASLP_ROOT = Path("/home/nas/user/Uihyeop/IIPLab/projects/2025-12_TASLP_SR-CorrNet/.agent_reports")
TASLP_MANUSCRIPT = "cyc_8513b910a82937ab87404e5d0677c7ce"
TASLP_CHEATSHEET = "cyc_a33b829c03c2d972fd59271e3de169fd"
RFC3339 = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"


class C1ChangesBase(B1RefreshBase):
    """§45 D-126: moving, marking and deleting cycles, and finding a hand-made change."""

    def closed_with(self, slug, campaign_key, files=None, *, activate=False):
        if activate:
            self.activate()
        route, route_file = self.route(slug=slug, campaign_key=campaign_key)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for rel, data in (files or {"plans/cycle/plan.md": b"plan body\n"}).items():
            self.write_output(result, rel, data)
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed", sealed)
        self.history.made.clear()
        self.history.published.clear()
        return result

    def open_with(self, slug, campaign_key):
        route, route_file = self.route(slug=slug, campaign_key=campaign_key)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(result, "plans/cycle/draft.md", b"draft\n")
        self.history.made.clear()
        return route, route_file, result

    def record(self, result):
        return P.read_cycle_record(self.root, result["cycle_id"])

    def lines(self, **match):
        return [m for m in self.history.made if all(m.get(k) == v for k, v in match.items())]

    def observe_then_reconcile(self):
        """A query leaves bookkeeping to the next writer, including pending history."""
        def snapshot():
            return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob("*") if p.is_file()}
        before = snapshot()
        history = (list(self.history.made), list(self.history.published))
        P.list_campaign_summaries(self.root, active_only=False)
        self.assertEqual(snapshot(), before)
        self.assertEqual((self.history.made, self.history.published), history)
        P.reconcile_root(self.root)
        P.deliver_pending_history(self.root)

    def cycle_path(self, result):
        record = self.record(result)
        return P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record)

    def index_row(self, result):
        index = adm.load_index(self.root)
        return index.cycles.get(result["cycle_id"]), index.manifests.get(result["cycle_id"])

    def locator_map(self):
        return json.loads((self.root / "campaigns" / "INDEX.json").read_text(encoding="utf-8"))

    def tree_digest(self, top):
        digest = hashlib.sha256()
        for path in sorted(Path(top).rglob("*")):
            if path.is_file():
                digest.update(path.relative_to(top).as_posix().encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def taslp_copy(self):
        if not TASLP_ROOT.is_dir():
            self.skipTest("the TASLP root is not on this host")
        target = Path(self._tmp.name) / "taslp-copy"
        shutil.copytree(TASLP_ROOT, target, symlinks=True)
        return target


class C1MarkTest(C1ChangesBase):
    # -- A28-13 ------------------------------------------------------------
    def test_c1_mark_discard_supersede_clear_open_and_closed(self):
        closed = self.closed_with("mark-closed", "c1-mark", activate=True)
        route, route_file, live = self.open_with("mark-open", "c1-mark")
        gone = self.closed_with("mark-gone", "c1-mark")
        P.delete_cycle(self.root, gone["cycle_id"])
        for target in (closed, live):
            cycle_id = target["cycle_id"]
            before = self.record(target)
            manifest_before = (self.cycle_path(target) / "manifest.json").read_bytes() \
                if (self.cycle_path(target) / "manifest.json").exists() else None
            files_before = self.tree_digest(self.cycle_path(target))
            self.history.made.clear()
            out = P.cycle_mark(self.root, cycle_id, discard=True, reason="owner said drop it")
            self.assertEqual(out["status"], "marked", out)
            after = self.record(target)
            mark = after["disposition"]
            self.assertEqual((mark["kind"], mark["reason"], mark["marked_by"]), ("discarded", "owner said drop it", "human"))
            self.assertRegex(mark["marked_at"], RFC3339)
            self.assertNotIn("superseded_by", mark)
            # Only the one field moved: state, manifest digest, files and manifest bytes are as they were.
            # Normal writer observation digest follows the disposition; all
            # other record fields and payload bytes stay as they were.
            self.assertEqual({k: v for k, v in after.items() if k not in {"disposition", "control_record_digest"}},
                             {k: v for k, v in before.items() if k != "control_record_digest"})
            self.assertEqual(self.tree_digest(self.cycle_path(target)), files_before)
            if manifest_before is not None:
                self.assertEqual((self.cycle_path(target) / "manifest.json").read_bytes(), manifest_before)
            (line,) = self.lines(kind="lifecycle", field="disposition")
            self.assertEqual((line["target_type"], line["target_id"], line["operation"]), ("cycle", cycle_id, "add"))
            self.assertEqual(line["before"], {"value": None})
            self.assertEqual(line["after"]["value"]["kind"], "discarded")
            self.assertEqual(line["reason"], "owner said drop it")
            # A mark never blocks a write, a close or a refresh.
            self.write_output(target, "plans/cycle/after-mark.md", b"after the mark\n")
            self.history.made.clear()
            out = P.cycle_mark(self.root, cycle_id, superseded_by=[closed["cycle_id"] if target is live else live["cycle_id"],
                                                                    gone["cycle_id"]])
            mark = self.record(target)["disposition"]
            self.assertEqual(mark["kind"], "superseded")
            self.assertEqual(len(mark["superseded_by"]), 2)
            self.assertIn(gone["cycle_id"], mark["superseded_by"])  # a deleted cycle is a valid replacement
            self.assertIsNone(mark["reason"])
            (line,) = self.lines(field="disposition")
            self.assertEqual((line["operation"], line["before"]["value"]["kind"]), ("update", "discarded"))
            self.history.made.clear()
            P.cycle_mark(self.root, cycle_id, clear=True)
            self.assertNotIn("disposition", self.record(target))
            (line,) = self.lines(field="disposition")
            self.assertEqual((line["operation"], line["after"]), ("update", {"value": None}))  # the recorder deletes only `state`
            # Clearing what is not there still says so, once.
            self.history.made.clear()
            P.cycle_mark(self.root, cycle_id, clear=True)
            self.assertNotIn("disposition", self.record(target))
        self.assertEqual(self.record(live)["state"], "open")
        self.assertEqual(self.record(closed)["state"], "sealed")
        # A replacement that is no cycle of this root is the one thing named wrong.
        with self.assertRaises(P.ProducerError):
            P.cycle_mark(self.root, closed["cycle_id"], superseded_by=["cyc_" + "9" * 32])
        # The command line: no confirmation flag, a reason when one is given.
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = P.main(["cycle-mark", "--artifact-root", str(self.root), "--cycle", closed["cycle_id"],
                           "--discard", "--reason", "cli"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["status"], "marked")
        self.assertEqual(self.record(closed)["disposition"]["reason"], "cli")

    def test_c1_old_superseded_records_keep_display_semantics(self):
        first = self.closed_with("old-sup-a", "c1-old-sup", activate=True)
        second = self.closed_with("old-sup-b", "c1-old-sup")
        # A record the older writer made: the state itself says superseded.
        record = dict(self.record(first), state="superseded", superseded_by=[second["cycle_id"]],
                      superseded_event_id="evt_" + "1" * 32)
        P._write_cycle_record(self.root, record, exclusive=False)
        record = self.record(first)
        self.assertEqual(record["state"], "superseded")
        shown = P.cycle_disposition(record)
        self.assertEqual((shown["kind"], shown["superseded_by"]), ("superseded", [second["cycle_id"]]))
        self.assertIsNone(P.cycle_disposition(self.record(second)))
        # The old fields are left alone by a new mark and a clear, and the new field wins when both exist.
        P.cycle_mark(self.root, first["cycle_id"], discard=True)
        record = self.record(first)
        self.assertEqual((record["state"], record["superseded_by"]), ("superseded", [second["cycle_id"]]))
        self.assertEqual(P.cycle_disposition(record)["kind"], "discarded")
        P.cycle_mark(self.root, first["cycle_id"], clear=True)
        record = self.record(first)
        self.assertEqual((record["state"], record["superseded_by"]), ("superseded", [second["cycle_id"]]))
        self.assertEqual(P.cycle_disposition(record)["kind"], "superseded")
        # The campaign list reads both the same way.
        summary = next(row for row in P.list_campaign_summaries(self.root) if row["key"] == "c1-old-sup")
        self.assertEqual(summary["cycle_count"], 2)
        self.assertEqual(summary["dispositions"], {"superseded": 1})
        P.cycle_mark(self.root, second["cycle_id"], discard=True)
        summary = next(row for row in P.list_campaign_summaries(self.root) if row["key"] == "c1-old-sup")
        self.assertEqual(summary["dispositions"], {"superseded": 1, "discarded": 1})
        self.assertTrue(summary["all_set_aside"])

    def test_c1_taslp_copy_discard_clear_preserves_payload(self):
        copy = self.taslp_copy()
        record_path = copy / ".runtime/artifact-producer/v1/cycles" / (TASLP_MANUSCRIPT + ".json")
        campaigns = copy / "campaigns"
        payload = self.tree_digest(campaigns)
        before = json.loads(record_path.read_text())
        reason = ("user instruction: set this manuscript aside; the new draft lives in "
                  "campaigns/2026-09-30_tasl-sr-corrnet-revision-r1/2026-10-01_strategy-derived-cheatsheet")
        marked = P.cycle_mark(copy, TASLP_MANUSCRIPT, discard=True, reason=reason)
        self.assertEqual(marked["status"], "marked", marked)
        record = json.loads(record_path.read_text())
        self.assertEqual(record["disposition"]["kind"], "discarded")
        self.assertEqual(record["disposition"]["reason"], reason)
        self.assertEqual({k: v for k, v in record.items() if k not in {"disposition", "control_record_digest"}},
                         {k: v for k, v in before.items() if k != "control_record_digest"})
        self.assertEqual(self.tree_digest(campaigns), payload)  # no file or manifest of any cycle moved
        self.assertEqual(len(self.lines(field="disposition")), 1)
        P.cycle_mark(copy, TASLP_MANUSCRIPT, clear=True)
        self.assertEqual({k: v for k, v in json.loads(record_path.read_text()).items() if k != "control_record_digest"},
                         {k: v for k, v in before.items() if k != "control_record_digest"})
        self.assertEqual(self.tree_digest(campaigns), payload)
        self.assertEqual(len(self.lines(field="disposition")), 2)
        # The real root was only read.
        self.assertNotIn("disposition", json.loads(
            (TASLP_ROOT / ".runtime/artifact-producer/v1/cycles" / (TASLP_MANUSCRIPT + ".json")).read_text()))

    # -- the fourth mode: which document stands for the cycle ----------------
    def roles(self, result):
        document = self.manifest(result)
        by_id = {row["artifact_id"]: row for row in document["artifacts"]}
        return {row["locator"]["path"]: by_id[row["artifact_id"]]["role"] for row in document["artifact_revisions"]}

    def test_c1_mark_primary_swaps_roles_only(self):
        result = self.closed_with("primary-swap", "c1-primary", {
            "plans/cycle/plan.md": b"plan body\n", "plans/cycle/other.md": b"other\n"}, activate=True)
        before = self.manifest(result)
        roles_before = self.roles(result)
        old_primary = next(rel for rel, role in roles_before.items() if role == "primary")
        wanted = next(rel for rel in roles_before if rel != old_primary)
        self.history.made.clear()
        out = P.cycle_mark(self.root, result["cycle_id"], primary=wanted, reason="the other one is the report")
        self.assertEqual(out["status"], "marked", out)
        after = self.manifest(result)
        roles = self.roles(result)
        self.assertEqual(roles[wanted], "primary")
        self.assertNotEqual(roles[old_primary], "primary")
        self.assertEqual([r for r, role in roles.items() if role == "primary"], [wanted])
        self.assertNotEqual(after["manifest_revision_id"], before["manifest_revision_id"])
        # Content is untouched: both rows keep their artifact and revision IDs, and every event stands.
        self.assertEqual(after["artifact_revisions"], before["artifact_revisions"])
        self.assertEqual(after["events"][:len(before["events"])], before["events"])
        self.assertEqual(after["routes"], before["routes"])
        self.assertEqual(after["cycle"], before["cycle"])
        differing = [(a["artifact_id"], a["role"]) for a, b in zip(after["artifacts"], before["artifacts"]) if a != b]
        self.assertEqual(len(differing), 2)
        # The earlier document is kept as it was published.
        names = self.snapshot_names(result)
        self.assertIn(before["manifest_revision_id"] + ".json", names)
        # One history line: field primary, cycle-relative paths.
        (line,) = self.lines(kind="lifecycle", field="primary")
        self.assertEqual((line["target_type"], line["operation"]), ("cycle", "update"))
        self.assertEqual((line["before"], line["after"]), ({"value": old_primary}, {"value": wanted}))
        self.assertEqual(line["reason"], "the other one is the report")
        self.assertEqual(self.record(result)["manifest_digest"], m_digest(after))
        row, manifest_row = self.index_row(result)
        self.assertEqual(manifest_row["manifest_digest"], m_digest(after))
        # The same designation again writes nothing.
        state = self.tree_state(result)
        self.history.made.clear()
        again = P.cycle_mark(self.root, result["cycle_id"], primary=wanted)
        self.assertEqual(again["status"], "unchanged", again)
        self.assertEqual(self.tree_state(result), state)
        self.assertEqual(self.history.made, [])
        # A path the manifest does not list is named and nothing changes (an ordinary usage error).
        with self.assertRaises(P.ProducerError):
            P.cycle_mark(self.root, result["cycle_id"], primary="artifacts/plans/cycle/missing.md")
        self.assertEqual(self.tree_state(result), state)
        # Neither can the mark modes be mixed.
        with self.assertRaises(P.ProducerError):
            P.cycle_mark(self.root, result["cycle_id"], primary=old_primary, discard=True)

    def test_c1_mark_primary_survives_refresh_and_rebuild(self):
        result = self.closed_with("primary-keep", "c1-primary-keep", {
            "plans/cycle/plan.md": b"plan body\n", "plans/cycle/other.md": b"other\n",
            "plans/cycle/third.md": b"third\n"}, activate=True)
        roles = self.roles(result)
        old_primary = next(rel for rel, role in roles.items() if role == "primary")
        wanted = next(rel for rel in roles if rel != old_primary)
        P.cycle_mark(self.root, result["cycle_id"], primary=wanted)
        marked = self.manifest(result)
        # Another file is edited and the cycle is closed again; then the index is rebuilt.
        other = next(rel for rel in roles if rel not in (old_primary, wanted))
        self.edit(result, other[len("artifacts/"):], b"third, edited\n")
        refreshed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertTrue(refreshed["refreshed"], refreshed)
        after = self.manifest(result)
        self.assertEqual(self.roles(result)[wanted], "primary")
        self.assertEqual([r for r, role in self.roles(result).items() if role == "primary"], [wanted])
        for rel in (old_primary, wanted):
            rows = lambda doc: next(r for r in doc["artifact_revisions"] if r["locator"]["path"] == rel)
            self.assertEqual(rows(after), rows(marked))
        self.assertEqual(after["events"][:len(marked["events"])], marked["events"])
        self.assertEqual([e["stream_sequence"] for e in after["events"][:len(marked["events"])]],
                         [e["stream_sequence"] for e in marked["events"]])
        incremental = adm.load_index(self.root)
        rebuilt = adm.rebuild_index(self.root)
        self.assertEqual(artifact_index.to_payload(rebuilt)["cycles"], artifact_index.to_payload(incremental)["cycles"])
        self.assertEqual(artifact_index.to_payload(rebuilt)["manifests"], artifact_index.to_payload(incremental)["manifests"])
        self.assertEqual(self.roles(result)[wanted], "primary")

    def test_c1_taslp_copy_primary_redesignation(self):
        copy = self.taslp_copy()
        wanted = "artifacts/reviews/refine/revision_cheatsheet.md"
        manifest_path = next((copy / "campaigns").glob("*/2026-10-01_strategy-derived-cheatsheet/manifest.json"))
        before = json.loads(manifest_path.read_text())
        by_id = {row["artifact_id"]: row for row in before["artifacts"]}
        old = next(r["locator"]["path"] for r in before["artifact_revisions"] if by_id[r["artifact_id"]]["role"] == "primary")
        out = P.cycle_mark(copy, TASLP_CHEATSHEET, primary=wanted, reason="redesignated by the owner")
        self.assertEqual(out["status"], "marked", out)
        after = json.loads(manifest_path.read_text())
        by_id = {row["artifact_id"]: row for row in after["artifacts"]}
        primaries = [r["locator"]["path"] for r in after["artifact_revisions"] if by_id[r["artifact_id"]]["role"] == "primary"]
        self.assertEqual(primaries, [wanted])
        self.assertNotEqual(old, wanted)
        self.assertEqual(after["artifact_revisions"], before["artifact_revisions"])
        self.assertEqual(len(after["events"]), len(before["events"]))
        self.assertEqual(len(self.lines(field="primary")), 1)
        self.assertEqual(json.loads((TASLP_ROOT / "campaigns" / manifest_path.relative_to(copy / "campaigns")).read_text()), before)


class C1MoveTest(C1ChangesBase):
    # -- A28-7 -------------------------------------------------------------
    def test_c1_move_and_manual_reconcile_keep_ids(self):
        mover = self.closed_with("same-slug", "c1-src", {"plans/cycle/a.md": b"a\n", "plans/cycle/b.md": b"b\n"}, activate=True)
        peer = self.closed_with("same-slug", "c1-dst")
        src_id, dst_id = mover["campaign_id"], peer["campaign_id"]
        self.assertNotEqual(src_id, dst_id)
        old_document = self.manifest(mover)
        old_locator = self.record(mover)["locator"]
        self.assertEqual(self.record(peer)["locator"], old_locator)  # the same name in two campaigns
        out = P.cycle_move(self.root, mover["cycle_id"], campaign=dst_id, reason="regroup")
        self.assertEqual(out["status"], "moved", out)
        record = self.record(mover)
        new_dir = Path(out["cycle_dir"])
        self.assertEqual((record["cycle_id"], record["campaign_id"]), (mover["cycle_id"], dst_id))
        self.assertEqual(record["locator"], old_locator + "-2")  # D-90: the smallest unused suffix
        self.assertEqual(new_dir, P.campaign_dir(self.root, dst_id) / record["locator"])
        self.assertFalse(Path(mover["cycle_dir"]).exists())
        self.assertEqual(json.loads((new_dir / ".cycle.json").read_text())["campaign_id"], dst_id)
        document = json.loads((new_dir / "manifest.json").read_text())
        self.assertEqual((document["cycle"]["campaign_id"], document["campaign"]["campaign_id"]), (dst_id, dst_id))
        self.assertNotEqual(document["manifest_revision_id"], old_document["manifest_revision_id"])
        self.assertEqual(document["artifact_revisions"], old_document["artifact_revisions"])
        self.assertEqual(document["events"], old_document["events"])
        self.assertIn(old_document["manifest_revision_id"] + ".json", self.snapshot_names(mover))
        cycle_row, manifest_row = self.index_row(mover)
        self.assertEqual((cycle_row["campaign_id"], cycle_row["cycle_path"]),
                         (dst_id, new_dir.relative_to(self.root).as_posix()))
        self.assertEqual(manifest_row["manifest_digest"], m_digest(document))
        self.assertEqual(self.record(mover)["manifest_digest"], m_digest(document))
        self.assertEqual(P.read_campaign(self.root, src_id)["cycles"], [])
        self.assertEqual(P.read_campaign(self.root, dst_id)["cycles"], [peer["cycle_id"], mover["cycle_id"]])
        self.assertEqual(self.locator_map()[mover["cycle_id"]], new_dir.relative_to(self.root).as_posix())
        (line,) = self.lines(kind="lifecycle", field="campaign")
        self.assertEqual((line["target_type"], line["target_id"], line["operation"]),
                         ("cycle", mover["cycle_id"], "move"))
        self.assertEqual((line["before"]["value"], line["after"]["value"]), (src_id, dst_id))
        self.assertEqual(line["reason"], "regroup")
        status = CAMP.status(self.root, dst_id)
        self.assertNotIn("close_refusal", status)
        self.assertEqual({row["cycle_id"] for row in status["cycles"]}, {peer["cycle_id"], mover["cycle_id"]})
        # The parent is changed by the same command and by it alone.
        self.history.made.clear()
        P.cycle_move(self.root, mover["cycle_id"], parent=peer["cycle_id"])
        self.assertEqual(self.record(mover)["parent_cycle_id"], peer["cycle_id"])
        self.assertEqual(self.manifest_of(mover)["cycle"]["parent_cycle_id"], peer["cycle_id"])
        (line,) = self.lines(field="parent")
        self.assertEqual((line["before"]["value"], line["after"]["value"]), (None, peer["cycle_id"]))
        self.assertEqual(self.lines(field="campaign"), [])
        P.cycle_move(self.root, mover["cycle_id"], no_parent=True)
        self.assertIsNone(self.record(mover)["parent_cycle_id"])
        self.assertIsNone(self.manifest_of(mover)["cycle"]["parent_cycle_id"])
        with self.assertRaises(P.ProducerError):
            P.cycle_move(self.root, mover["cycle_id"], parent=peer["cycle_id"], no_parent=True)
        # A target that is closed is opened again, as a begin would.
        third = self.closed_with("third", "c1-closed")
        CAMP.close(self.root, third["campaign_id"])
        self.assertEqual(P.read_campaign(self.root, third["campaign_id"])["state"], "satisfied")
        P.cycle_move(self.root, mover["cycle_id"], campaign=third["campaign_id"])
        self.assertEqual(P.read_campaign(self.root, third["campaign_id"])["state"], "active")
        self.assertEqual(self.record(mover)["campaign_id"], third["campaign_id"])
        # A campaign set aside takes a cycle as well; its state is not changed by that.
        aside = self.closed_with("aside", "c1-aside")
        P.mark_cycle_superseded(self.root, aside["cycle_id"], superseded_by=[mover["cycle_id"]],
                                superseded_event_id="evt_" + "2" * 32)
        P.mark_campaign_superseded(self.root, aside["campaign_id"])
        P.cycle_move(self.root, mover["cycle_id"], campaign=aside["campaign_id"])
        self.assertEqual(self.record(mover)["campaign_id"], aside["campaign_id"])
        self.assertEqual(P.read_campaign(self.root, aside["campaign_id"])["state"], "superseded")
        # The campaign may be named by its key as well; an unknown one changes nothing.
        P.cycle_move(self.root, mover["cycle_id"], campaign="c1-src")
        self.assertEqual(self.record(mover)["campaign_id"], src_id)
        state = (self.record(mover), self.locator_map())
        with self.assertRaises(P.ProducerError):
            P.cycle_move(self.root, mover["cycle_id"], campaign="camp_" + "8" * 32)
        self.assertEqual((self.record(mover), self.locator_map()), state)
        # An open cycle moves too, with no manifest to write.
        route, route_file, live = self.open_with("live-move", "c1-live")
        P.cycle_move(self.root, live["cycle_id"], campaign=src_id)
        moved_live = self.cycle_path(live)
        self.assertEqual(self.record(live)["campaign_id"], src_id)
        self.assertFalse((moved_live / "manifest.json").exists())
        self.assertEqual(self.record(live)["state"], "open")
        self.assertTrue((moved_live / "artifacts/plans/cycle/draft.md").is_file())
        # The command line: no confirmation flag; `--parent` and `--no-parent` exclude each other.
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = P.main(["cycle-move", "--artifact-root", str(self.root), "--cycle", live["cycle_id"],
                           "--campaign", dst_id, "--parent", peer["cycle_id"], "--reason", "cli"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["status"], "moved")
        self.assertEqual((self.record(live)["campaign_id"], self.record(live)["parent_cycle_id"]), (dst_id, peer["cycle_id"]))
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            P.main(["cycle-move", "--artifact-root", str(self.root), "--cycle", live["cycle_id"],
                    "--parent", peer["cycle_id"], "--no-parent"])

    def manifest_of(self, result):
        return json.loads((self.cycle_path(result) / "manifest.json").read_text(encoding="utf-8"))

    def test_c1_manual_move_and_rename_are_found_by_the_next_writer(self):
        mover = self.closed_with("hand-move", "c1-hand-src", {"plans/cycle/a.md": b"a\n"}, activate=True)
        peer = self.closed_with("hand-peer", "c1-hand-dst")
        src_id, dst_id = mover["campaign_id"], peer["campaign_id"]
        old_revision = self.manifest(mover)["manifest_revision_id"]
        source = Path(mover["cycle_dir"])
        target = P.campaign_dir(self.root, dst_id) / source.name
        os.rename(str(source), str(target))
        # Queries remain pure; the next writer reconciles the physical move.
        self.observe_then_reconcile()
        rows = P.list_campaign_summaries(self.root, active_only=False)
        self.assertEqual({row["key"]: row["cycle_count"] for row in rows}["c1-hand-dst"], 2)
        record = self.record(mover)
        self.assertEqual((record["cycle_id"], record["campaign_id"], record["locator"]), (mover["cycle_id"], dst_id, source.name))
        self.assertEqual(json.loads((target / ".cycle.json").read_text())["campaign_id"], dst_id)
        document = json.loads((target / "manifest.json").read_text())
        self.assertEqual(document["cycle"]["campaign_id"], dst_id)
        self.assertNotEqual(document["manifest_revision_id"], old_revision)
        cycle_row, _ = self.index_row(mover)
        self.assertEqual((cycle_row["campaign_id"], cycle_row["cycle_path"]), (dst_id, target.relative_to(self.root).as_posix()))
        self.assertEqual(P.read_campaign(self.root, src_id)["cycles"], [])
        self.assertEqual(P.read_campaign(self.root, dst_id)["cycles"], [peer["cycle_id"], mover["cycle_id"]])
        (line,) = self.lines(field="campaign")
        self.assertEqual((line["operation"], line["actor"]["by"], line["before"]["value"], line["after"]["value"]),
                         ("move", "rule", src_id, dst_id))
        # Looking again changes nothing.
        before = (self.record(mover), self.locator_map(), adm.load_index(self.root))
        self.history.made.clear()
        self.observe_then_reconcile()
        self.assertEqual((self.record(mover), self.locator_map(), adm.load_index(self.root)), before)
        self.assertEqual(self.history.made, [])
        # The campaign closes: the moved cycle is a member of where it is.
        self.assertEqual(CAMP.close(self.root, dst_id)["status"], "satisfied")
        # A campaign folder renamed by hand keeps its ID; its record follows the folder.
        folder = P.campaign_dir(self.root, dst_id)
        renamed = folder.with_name("renamed-by-hand")
        os.rename(str(folder), str(renamed))
        self.history.made.clear()
        self.observe_then_reconcile()
        self.assertEqual(P.read_campaign(self.root, dst_id)["locator"], "renamed-by-hand")
        self.assertEqual(P.campaign_dir(self.root, dst_id), renamed)
        (line,) = self.lines(field="path", target_type="campaign")
        self.assertEqual((line["target_id"], line["actor"]["by"]), (dst_id, "rule"))
        self.assertEqual(Path(self.locator_map()[mover["cycle_id"]]).parts[1], "renamed-by-hand")
        cycle_row, _ = self.index_row(mover)
        self.assertEqual(Path(cycle_row["cycle_path"]).parts[1], "renamed-by-hand")
        # A cycle folder renamed inside its campaign keeps its ID too.
        inner = renamed / source.name
        os.rename(str(inner), str(renamed / "inner-rename"))
        self.observe_then_reconcile()
        self.assertEqual(self.record(mover)["locator"], "inner-rename")
        self.assertEqual(self.record(mover)["campaign_id"], dst_id)

    def test_c1_an_unreadable_or_odd_scan_never_makes_a_tombstone(self):
        keep = self.closed_with("odd-scan", "c1-odd", {"plans/cycle/a.md": b"a\n"}, activate=True)
        other = self.closed_with("odd-other", "c1-odd-other")
        # A symbolic link where a campaign folder was is not a campaign and not a deletion.
        folder = P.campaign_dir(self.root, other["campaign_id"])
        hidden = folder.with_name("moved-away")
        os.rename(str(folder), str(hidden))
        os.symlink(str(hidden), str(folder))
        P.list_campaign_summaries(self.root, active_only=False)
        for result in (keep, other):
            self.assertNotIn("deleted_at", self.record(result))
        self.assertIsNone(P.read_campaign_tombstone(self.root, other["campaign_id"]))
        os.unlink(str(folder))
        os.rename(str(hidden), str(folder))
        # A cycle seen twice (a copy of its folder) is not guessed at: neither is declared gone or moved.
        original = Path(keep["cycle_dir"])
        copy = P.campaign_dir(self.root, other["campaign_id"]) / "copy-of-keep"
        shutil.copytree(str(original), str(copy))
        P.list_campaign_summaries(self.root, active_only=False)
        self.assertEqual(self.record(keep)["campaign_id"], keep["campaign_id"])
        self.assertNotIn("deleted_at", self.record(keep))
        self.assertEqual(self.record(keep)["locator"], original.name)
        # An unreadable campaign folder stops the deletion judgment for the whole listing.
        victim = self.closed_with("odd-victim", "c1-odd-victim")
        shutil.rmtree(str(Path(victim["cycle_dir"])))
        folder = P.campaign_dir(self.root, keep["campaign_id"])
        os.chmod(str(folder), 0)
        try:
            out = P.reconcile_root(self.root)
            self.assertNotEqual(out.get("status"), "reconciled", out)
            self.assertNotIn("deleted_at", self.record(victim))
        finally:
            os.chmod(str(folder), 0o755)
        # With every folder readable again (and the copy gone) the removed cycle is found, and only that one.
        shutil.rmtree(str(copy))
        P.reconcile_root(self.root)
        self.assertIn("deleted_at", self.record(victim))
        self.assertNotIn("deleted_at", self.record(keep))


class C1DeleteTest(C1ChangesBase):
    # -- A28-8 -------------------------------------------------------------
    def test_c1_delete_preserves_records_and_handoff(self):
        keep = self.closed_with("del-keep", "c1-del", {"plans/cycle/k.md": b"k\n"}, activate=True)
        gone = self.closed_with("del-gone", "c1-del", {"plans/cycle/g.md": b"g\n", "plans/cycle/h.md": b"h\n"})
        gone_document = self.manifest(gone)
        digest = m_digest(gone_document)
        folder = Path(gone["cycle_dir"])
        index_before = adm.load_index(self.root)
        out = P.delete_cycle(self.root, gone["cycle_id"], reason="not needed")
        self.assertEqual(out["status"], "deleted", out)
        self.assertFalse(folder.exists())
        record = self.record(gone)
        self.assertRegex(record["deleted_at"], RFC3339)
        self.assertEqual((record["cycle_id"], record["state"]), (gone["cycle_id"], "sealed"))
        self.assertNotIn(gone["cycle_id"], P.read_campaign(self.root, gone["campaign_id"])["cycles"])
        self.assertEqual(P.read_campaign(self.root, gone["campaign_id"])["cycles"], [keep["cycle_id"]])
        # The list and the index drop the current rows; the IDs stay owned.
        index = adm.load_index(self.root)
        self.assertNotIn(gone["cycle_id"], index.cycles)
        self.assertNotIn(gone["cycle_id"], index.manifests)
        self.assertEqual(index.stable_ids, index_before.stable_ids)
        self.assertEqual(index.event_ids, index_before.event_ids)
        self.assertNotIn(gone["cycle_id"], self.locator_map())
        self.assertIn(gone_document["manifest_revision_id"] + ".json", self.snapshot_names(gone))
        (line,) = self.lines(kind="lifecycle", target_type="cycle", operation="delete")
        self.assertEqual(line["target_id"], gone["cycle_id"])
        self.assertEqual(line["before"]["value"]["manifest_digest"], digest)
        self.assertEqual(line["before"]["value"]["path"], folder.relative_to(self.root).as_posix())
        self.assertEqual(line["reason"], "not needed")
        # The ID is never issued again: an index rebuilt from what is left still owns it.
        rebuilt = adm.rebuild_index(self.root)
        self.assertEqual(rebuilt.stable_ids, index_before.stable_ids)
        self.assertNotIn(gone["cycle_id"], rebuilt.cycles)
        # Deleting again, and deleting what is already gone by hand, is the same answer.
        self.history.made.clear()
        again = P.delete_cycle(self.root, gone["cycle_id"])
        self.assertEqual(again["status"], "already-deleted", again)
        self.assertEqual(self.history.made, [])
        self.assertEqual(CAMP.close(self.root, keep["campaign_id"])["status"], "satisfied")
        # A campaign: its folder goes, its record stays beside the root's other runtime records.
        campaign_id = keep["campaign_id"]
        campaign_folder = P.campaign_dir(self.root, campaign_id)
        last_path = campaign_folder.relative_to(self.root).as_posix()
        key = P.read_campaign(self.root, campaign_id)["key"]
        self.history.made.clear()
        out = P.delete_campaign(self.root, campaign_id, reason="whole stream dropped")
        self.assertEqual(out["status"], "deleted", out)
        self.assertFalse(campaign_folder.exists())
        tomb = P.read_campaign_tombstone(self.root, campaign_id)
        self.assertEqual((tomb["campaign_id"], tomb["key"], tomb["last_path"]), (campaign_id, key, last_path))
        self.assertRegex(tomb["deleted_at"], RFC3339)
        self.assertIsNone(P.read_campaign(self.root, campaign_id))
        self.assertFalse((self.root / last_path).exists())  # nothing re-creates the folder
        self.assertEqual([row for row in P.list_campaign_summaries(self.root, active_only=False)
                          if row["campaign_id"] == campaign_id], [])
        self.assertNotIn(keep["cycle_id"], self.locator_map())
        self.assertIsNotNone(self.record(keep)["deleted_at"])
        # One line for each member that was still there, and one for the campaign.
        member_lines = self.lines(target_type="cycle", operation="delete")
        self.assertEqual([m["target_id"] for m in member_lines], [keep["cycle_id"]])
        (campaign_line,) = self.lines(target_type="campaign", operation="delete")
        self.assertEqual(campaign_line["target_id"], campaign_id)
        self.assertEqual(self.lines(target_type="campaign")[0]["reason"], "whole stream dropped")
        # The preserved copies stay for every member, deleted earlier or now.
        for result in (gone, keep):
            self.assertTrue(self.snapshot_names(result))
        self.assertFalse(self.root.joinpath(last_path).exists())
        # A listing and a recovery find nothing to put back, and the index rebuilt from what is left agrees.
        P.list_campaign_summaries(self.root, active_only=False)
        self.assertFalse(self.root.joinpath(last_path).exists())
        recovered = P.recover(self.root)
        self.assertEqual(recovered["status"], "recovered", recovered)
        self.assertFalse(self.root.joinpath(last_path).exists())
        self.assertTrue(adm.verify_index(self.root).ok)
        # The command line takes exactly one of the two.
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = P.main(["delete", "--artifact-root", str(self.root), "--cycle", gone["cycle_id"]])
        self.assertEqual(code, 0, out.getvalue())
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            P.main(["delete", "--artifact-root", str(self.root)])

    def test_c1_old_cycle_snapshot_before_delete(self):
        result = self.closed_with("old-copy", "c1-old-copy", {"plans/cycle/a.md": b"a\n"}, activate=True)
        manifest = (Path(result["cycle_dir"]) / "manifest.json").read_bytes()
        # A cycle closed before copies existed has none: the first sight of it is this delete.
        shutil.rmtree(L.manifest_snapshot_dir(self.root, result["cycle_id"]))
        seen = []
        real_rmtree = shutil.rmtree

        def watching(path, *args, **kwargs):
            if Path(path) == Path(result["cycle_dir"]):
                seen.append(sorted(p.name for p in L.manifest_snapshot_dir(self.root, result["cycle_id"]).iterdir()))
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(P.shutil, "rmtree", watching):
            P.delete_cycle(self.root, result["cycle_id"])
        revision = json.loads(manifest)["manifest_revision_id"]
        self.assertEqual(seen, [[revision + ".json"]], "the copy is made before the folder goes")
        self.assertEqual(L.manifest_snapshot_path(self.root, result["cycle_id"], revision).read_bytes(), manifest)

    def test_c1_refinalize_immediate_delete_preserves_latest_revision(self):
        result = self.closed_with("re-close", "c1-reclose", {"plans/cycle/a.md": b"a\n"}, activate=True)
        first = self.manifest(result)["manifest_revision_id"]
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        refreshed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertTrue(refreshed["refreshed"], refreshed)
        latest = self.manifest(result)
        P.delete_cycle(self.root, result["cycle_id"])
        names = self.snapshot_names(result)
        self.assertEqual(names, sorted([first + ".json", latest["manifest_revision_id"] + ".json"]))
        copy = L.manifest_snapshot_path(self.root, result["cycle_id"], latest["manifest_revision_id"])
        self.assertEqual(json.loads(copy.read_text())["manifest_revision_id"], latest["manifest_revision_id"])
        self.assertEqual(M.manifest_digest(json.loads(copy.read_text())), self.record(result)["manifest_digest"])

    def test_c1_manual_delete_before_observation_uses_terminal(self):
        result = self.closed_with("hand-del", "c1-hand-del", {"plans/cycle/a.md": b"a\n"}, activate=True)
        other = self.closed_with("hand-del-peer", "c1-hand-del")
        shutil.rmtree(L.manifest_snapshot_dir(self.root, result["cycle_id"]))  # closed before copies existed
        shutil.rmtree(result["cycle_dir"])
        self.observe_then_reconcile()
        record = self.record(result)
        self.assertRegex(record["deleted_at"], RFC3339)
        self.assertNotIn(result["cycle_id"], adm.load_index(self.root).cycles)
        self.assertEqual(P.read_campaign(self.root, result["campaign_id"])["cycles"], [other["cycle_id"]])
        # Nothing could be copied from a folder that was already gone: none is made up.
        self.assertEqual(self.snapshot_names(result), [])
        (line,) = self.lines(target_type="cycle", operation="delete")
        self.assertEqual((line["target_id"], line["actor"]["by"]), (result["cycle_id"], "rule"))
        # The campaign still closes and a replay still finds the cycle (deleted).
        self.assertEqual(CAMP.close(self.root, other["campaign_id"])["status"], "satisfied")
        verdict = P.verify_finalized_cycle(self.root, cycle_id=result["cycle_id"], expected_binding={
            "cycle_id": result["cycle_id"], "producer_id": record["producer_id"]},
            expected_manifest_digest=record["manifest_digest"])
        self.assertEqual((verdict["manifest_digest"], verdict["deleted"]), (record["manifest_digest"], True))
        # A whole campaign gone by hand: every member is recorded, and the campaign's own record is made.
        solo = self.closed_with("hand-del-solo", "c1-hand-del-solo")
        folder = P.campaign_dir(self.root, solo["campaign_id"])
        last_path = folder.relative_to(self.root).as_posix()
        shutil.rmtree(str(folder))
        self.history.made.clear()
        self.observe_then_reconcile()
        tomb = P.read_campaign_tombstone(self.root, solo["campaign_id"])
        self.assertEqual((tomb["campaign_id"], tomb["last_path"]), (solo["campaign_id"], last_path))
        self.assertIsNotNone(self.record(solo)["deleted_at"])
        self.assertEqual(len(self.lines(target_type="campaign", operation="delete")), 1)
        self.assertFalse(folder.exists())

    def test_c1_deleted_cycle_campaign_replay_and_close(self):
        # A finished cycle is deleted, then its campaign: the finish is replayed as it was recorded.
        fixture = INLINE.PublicInlineFinishTest("test_public_finish_seals_and_exact_replay_returns_one_receipt")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        first = fixture.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        receipt = json.loads(first.stdout)
        cycle_id = fixture.cycle["cycle_id"]
        campaign_id = fixture.cycle["campaign_id"]
        root = fixture.root
        state_file = root / ".runtime/inline-finish/v1" / fixture.route["route_id"] / "finish.json"
        state_before = state_file.read_bytes()
        INLINE.artifact_producer.delete_cycle(root, cycle_id)
        gone = fixture.finish()
        self.assertEqual(gone.returncode, 0, gone.stderr)
        replayed = json.loads(gone.stdout)
        self.assertTrue(replayed["replay"])
        self.assertEqual((replayed["inline_finish_id"], replayed["manifest_digest"]),
                         (receipt["inline_finish_id"], receipt["manifest_digest"]))
        self.assertTrue(replayed.get("deleted_since"), replayed)
        INLINE.artifact_producer.delete_campaign(root, campaign_id)
        gone = fixture.finish()
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertEqual(json.loads(gone.stdout)["manifest_digest"], receipt["manifest_digest"])
        self.assertEqual(state_file.read_bytes(), state_before)

    def test_c1_close_after_deleting_a_member(self):
        keep = self.closed_with("close-keep", "c1-close-del", activate=True)
        gone = self.closed_with("close-gone", "c1-close-del")
        P.delete_cycle(self.root, gone["cycle_id"])
        status = CAMP.status(self.root, keep["campaign_id"])
        self.assertNotIn("close_refusal", status)
        self.assertEqual([row["cycle_id"] for row in status["cycles"]], [keep["cycle_id"]])
        self.assertEqual(CAMP.close(self.root, keep["campaign_id"])["status"], "satisfied")
        self.assertEqual(self.record(gone)["state"], "sealed")
        # A cycle can still name a deleted one as its parent.
        route, route_file = self.route(slug="close-child", campaign_key="c1-close-del")
        child = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                        parent_cycle_id=gone["cycle_id"])
        self.assertEqual(child["status"], "begun", child)
        self.assertEqual(self.record(child)["parent_cycle_id"], gone["cycle_id"])

    # -- a route that was running when its cycle was deleted ---------------
    def test_c1_deleted_live_route_ends_without_material_success(self):
        route, route_file, live = self.open_with("live-delete", "c1-live-del")
        folder = Path(live["cycle_dir"])
        self.assertTrue((folder / "artifacts/plans/cycle/draft.md").is_file())
        P.delete_cycle(self.root, live["cycle_id"], reason="dropped while running")
        self.assertFalse(folder.exists())
        # The route keeps its quality gate: closing it is exactly what it was.
        self.close(route, route_file)
        out = P.finalize(self.root, cycle_id=live["cycle_id"])
        self.assertEqual(out["status"], "no-lineage", out)
        self.assertFalse(out["lineage_committed"])
        record = self.record(live)
        self.assertEqual(record["state"], "no-lineage")
        self.assertRegex(record["deleted_at"], RFC3339)
        self.assertFalse(folder.exists(), "the folder is never put back")
        self.assertFalse(folder.parent.joinpath(folder.name).exists())
        self.assertNotIn(live["cycle_id"], adm.load_index(self.root).cycles)
        # The same answer on a second call, and the abandon path of the runtime's own sweep.
        again = P.finalize(self.root, cycle_id=live["cycle_id"], state="abandoned", abandon_reason="route-unrecoverable")
        self.assertEqual(again["status"], "no-lineage")
        self.assertFalse(folder.exists())
        # The terminal proof for it is the existing no-output one: never a verified material cycle.
        verdict = P.verify_finalized_cycle(self.root, cycle_id=live["cycle_id"], expected_binding={
            "cycle_id": live["cycle_id"], "producer_id": record["producer_id"]})
        self.assertTrue(verdict.get("deleted"))
        self.assertNotIn("manifest_digest", verdict)
        # A begin by the same route is a new cycle, not the old folder back.
        route2, route_file2 = self.route(slug="live-delete-next", campaign_key="c1-live-del")
        nxt = P.begin(self.root, route_file=route_file2, capability="autopilot-code", intensity="direct")
        self.assertNotEqual(nxt["cycle_id"], live["cycle_id"])
        self.assertFalse(folder.exists())

    def test_c1_deleted_inline_route_finishes_as_closed_without_proof(self):
        fixture = INLINE.PublicInlineFinishTest("test_public_finish_seals_and_exact_replay_returns_one_receipt")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        cycle_id = fixture.cycle["cycle_id"]
        folder = fixture.cycle_dir
        INLINE.artifact_producer.delete_cycle(fixture.root, cycle_id)
        done = fixture.finish()
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout)
        self.assertEqual(receipt["state"], "cycle-deleted")
        self.assertFalse(folder.exists())
        outcome = json.loads(R.outcome_path(fixture.route_file).read_text())
        self.assertIsNot(outcome.get("terminal_gate_proven"), True)
        self.assertIn(INLINE.artifact_producer.read_cycle_record(fixture.root, cycle_id)["state"], {"abandoned", "no-lineage"})
        # Asked again, it is the same answer.
        again = fixture.finish()
        self.assertEqual(again.returncode, 0, again.stderr)

    # -- A28-11 ------------------------------------------------------------
    def test_c1_mutation_history_failure_pending_after_delete(self):
        moved = self.closed_with("pend-move", "c1-pend-a", {"plans/cycle/a.md": b"a\n"}, activate=True)
        peer = self.closed_with("pend-peer", "c1-pend-b")
        self.history.fail_publish = True
        out = P.cycle_move(self.root, moved["cycle_id"], campaign=peer["campaign_id"])
        self.assertEqual(out["status"], "moved")
        pending = self.record(moved)["history_pending"]
        self.assertEqual([entry["field"] for entry in pending], ["campaign"])
        self.assertEqual(self.history.published, [])
        P.cycle_mark(self.root, moved["cycle_id"], discard=True)
        self.assertEqual([e["field"] for e in self.record(moved)["history_pending"]], ["campaign", "disposition"])
        # Deleting keeps the change and the record that carries the lines.
        out = P.delete_cycle(self.root, moved["cycle_id"])
        self.assertEqual(out["status"], "deleted")
        record = self.record(moved)
        self.assertEqual([e["operation"] for e in record["history_pending"]], ["move", "add", "delete"])
        out = P.delete_campaign(self.root, peer["campaign_id"])
        self.assertEqual(out["status"], "deleted")
        tomb = P.read_campaign_tombstone(self.root, peer["campaign_id"])
        self.assertTrue(tomb["history_pending"])
        self.assertTrue(self.record(peer)["history_pending"])
        # A close and a reopen leave their state lines the same way.
        solo = self.closed_with("pend-solo", "c1-pend-solo")
        CAMP.close(self.root, solo["campaign_id"])
        CAMP.reopen(self.root, solo["campaign_id"], reason="again")
        sidecar = P.campaign_runtime_record(self.root, solo["campaign_id"])
        self.assertEqual([e["field"] for e in sidecar["history_pending"]], ["state", "state"])
        # The recorder comes back: the next trigger hands every line over once and empties the lists.
        self.history.fail_publish = False
        wanted = ({e["event_id"] for e in self.record(moved)["history_pending"]}
                  | {e["event_id"] for e in self.record(peer)["history_pending"]}
                  | {e["event_id"] for e in tomb["history_pending"]}
                  | {e["event_id"] for e in sidecar["history_pending"]}
                  | {e["event_id"] for e in self.record(solo)["history_pending"]})  # its own close line
        self.observe_then_reconcile()
        self.assertEqual({e["event_id"] for e in self.history.published}, wanted)
        self.assertNotIn("history_pending", self.record(moved))
        self.assertNotIn("history_pending", self.record(peer))
        self.assertNotIn("history_pending", P.read_campaign_tombstone(self.root, peer["campaign_id"]))
        self.assertNotIn("history_pending", P.campaign_runtime_record(self.root, solo["campaign_id"]))
        # No recorder at all: the command still succeeds and the lines wait.
        with mock.patch.dict(sys.modules, {"artifact_history": None}):
            P.cycle_mark(self.root, solo["cycle_id"], discard=True)
            self.assertEqual(len(self.record(solo)["history_pending"]), 1)
        P.checkpoint(self.root, cycle_id=solo["cycle_id"], trigger="explicit")
        self.assertNotIn("history_pending", self.record(solo))


class C9ReparentOnDeleteTest(C1ChangesBase):
    """Deleting a cycle (by command or by hand) leaves no survivor naming it as its parent.

    The importer that reads the root fails all of it on one parent that is not a cycle of the root, so
    the survivors take the nearest surviving ancestor (or none), one history line each."""

    def child_of(self, slug, key, parent=None, *, activate=False):
        if activate:
            self.activate()
        route, route_file = self.route(slug=slug, campaign_key=key)
        extra = {"parent_cycle_id": parent} if parent else {}
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct", **extra)
        self.write_output(result, "plans/cycle/plan.md", slug.encode() + b"\n")
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed", sealed)
        self.history.made.clear()
        self.history.published.clear()
        return result

    def survivors_name_survivors(self):
        """What the importer needs: every surviving cycle's parent is a surviving cycle of the root, or none."""
        records = {r["cycle_id"]: r for r in P.list_cycle_records(self.root)}
        for cycle_id, record in records.items():
            if record.get("deleted_at"):
                continue
            parent = record.get("parent_cycle_id")
            if parent is not None:
                self.assertIn(parent, records, f"{cycle_id} names a cycle with no record")
                self.assertFalse(records[parent].get("deleted_at"), f"{cycle_id} names a deleted cycle")

    def parent_lines(self, child):
        return self.lines(kind="lifecycle", target_type="cycle", target_id=child["cycle_id"], field="parent")

    def assert_one_parent_line(self, child, before, after):
        (line,) = self.parent_lines(child)
        self.assertEqual((line["operation"], line["before"], line["after"]),
                         ("update", {"value": before}, {"value": after}))

    def test_t9_1_deleting_a_cycle_gives_its_children_the_nearest_surviving_ancestor(self):
        a = self.child_of("t91-a", "c9-chain", activate=True)
        b = self.child_of("t91-b", "c9-chain", a["cycle_id"])
        c = self.child_of("t91-c", "c9-chain", b["cycle_id"])
        out = P.delete_cycle(self.root, b["cycle_id"])
        self.assertEqual(out["status"], "deleted", out)
        self.assertEqual(out["reparented"], [{"cycle_id": c["cycle_id"], "before": b["cycle_id"], "after": a["cycle_id"]}])
        self.assertEqual(self.record(c)["parent_cycle_id"], a["cycle_id"])
        self.assertEqual(self.manifest(c)["cycle"]["parent_cycle_id"], a["cycle_id"])
        self.assertEqual(self.record(c)["parent_cycle_state_at_begin"], self.record(a)["state"])
        self.assert_one_parent_line(c, b["cycle_id"], a["cycle_id"])
        self.survivors_name_survivors()
        # The first of the chain goes too: nothing is left above the child.
        self.history.made.clear()
        out = P.delete_cycle(self.root, a["cycle_id"])
        self.assertEqual(out["reparented"], [{"cycle_id": c["cycle_id"], "before": a["cycle_id"], "after": None}])
        self.assertIsNone(self.record(c)["parent_cycle_id"])
        self.assertNotIn("parent_cycle_state_at_begin", self.record(c))
        self.assertIsNone(self.manifest(c)["cycle"]["parent_cycle_id"])
        self.assert_one_parent_line(c, a["cycle_id"], None)
        self.survivors_name_survivors()

    def test_t9_2_deleting_a_campaign_leaves_its_outside_children_with_no_parent_in_it(self):
        a = self.child_of("t92-a", "c9-x", activate=True)
        b = self.child_of("t92-b", "c9-x", a["cycle_id"])
        self.child_of("t92-y", "c9-y")  # the other campaign exists before a child of x's cycles joins it
        c = self.child_of("t92-c", "c9-y", b["cycle_id"])
        d = self.child_of("t92-d", "c9-y", a["cycle_id"])
        out = P.delete_campaign(self.root, a["campaign_id"], reason="whole stream dropped")
        self.assertEqual(out["status"], "deleted", out)
        self.assertEqual(sorted((row["cycle_id"], row["before"], row["after"]) for row in out["reparented"]),
                         sorted([(c["cycle_id"], b["cycle_id"], None), (d["cycle_id"], a["cycle_id"], None)]))
        for child, before in ((c, b), (d, a)):
            self.assertIsNone(self.record(child)["parent_cycle_id"])
            self.assertIsNone(self.manifest(child)["cycle"]["parent_cycle_id"])
            self.assert_one_parent_line(child, before["cycle_id"], None)
        self.survivors_name_survivors()

    def test_t9_3_a_cycle_folder_removed_by_hand_is_handled_the_same_way(self):
        a = self.child_of("t93-a", "c9-hand", activate=True)
        b = self.child_of("t93-b", "c9-hand", a["cycle_id"])
        c = self.child_of("t93-c", "c9-hand", b["cycle_id"])
        shutil.rmtree(b["cycle_dir"])
        out = P.reconcile_root(self.root)
        self.assertEqual(out["status"], "reconciled", out)
        self.assertEqual(out["cycle_gone"], [b["cycle_id"]])
        self.assertEqual(out["reparented"], [{"cycle_id": c["cycle_id"], "before": b["cycle_id"], "after": a["cycle_id"]}])
        self.assertEqual(self.record(c)["parent_cycle_id"], a["cycle_id"])
        self.assertEqual(self.manifest(c)["cycle"]["parent_cycle_id"], a["cycle_id"])
        self.assert_one_parent_line(c, b["cycle_id"], a["cycle_id"])
        self.survivors_name_survivors()

    def test_t9_3_a_campaign_folder_removed_by_hand_is_handled_the_same_way(self):
        a = self.child_of("t93-ca", "c9-hand-x", activate=True)
        b = self.child_of("t93-cb", "c9-hand-x", a["cycle_id"])
        self.child_of("t93-cy", "c9-hand-y")
        c = self.child_of("t93-cc", "c9-hand-y", b["cycle_id"])
        shutil.rmtree(str(P.campaign_dir(self.root, a["campaign_id"])))
        out = P.reconcile_root(self.root)
        self.assertEqual(out["status"], "reconciled", out)
        self.assertEqual(out["reparented"], [{"cycle_id": c["cycle_id"], "before": b["cycle_id"], "after": None}])
        self.assertIsNone(self.record(c)["parent_cycle_id"])
        self.assert_one_parent_line(c, b["cycle_id"], None)
        self.survivors_name_survivors()

    def test_t9_4_what_the_deleted_cycle_did_not_parent_is_left_byte_for_byte(self):
        a = self.child_of("t94-a", "c9-keep", activate=True)
        b = self.child_of("t94-b", "c9-keep", a["cycle_id"])
        other = self.child_of("t94-o", "c9-other")
        other_child = self.child_of("t94-oc", "c9-other", other["cycle_id"])
        watched = [a, other, other_child]

        def snapshot():
            return [(P.cycle_record_path(self.root, r["cycle_id"]).read_bytes(),
                     (Path(r["cycle_dir"]) / "manifest.json").read_bytes()) for r in watched]

        before = snapshot()
        out = P.delete_cycle(self.root, b["cycle_id"])
        self.assertEqual(out["status"], "deleted", out)
        self.assertNotIn("reparented", out)
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.parent_lines(other_child), [])
        # A child whose folder is gone is no reason to refuse: the delete goes through.
        gone_child = self.child_of("t94-g", "c9-keep", a["cycle_id"])
        shutil.rmtree(gone_child["cycle_dir"])
        self.assertEqual(P.delete_cycle(self.root, a["cycle_id"])["status"], "deleted")

    def test_t9_5_a_parent_that_was_already_broken_is_mended_by_the_next_delete(self):
        a = self.child_of("t95-a", "c9-old", activate=True)
        old = self.child_of("t95-d", "c9-old", a["cycle_id"])
        e = self.child_of("t95-e", "c9-old", old["cycle_id"])
        g = self.child_of("t95-g", "c9-old", a["cycle_id"])
        f = self.child_of("t95-f", "c9-old", a["cycle_id"])
        # What an earlier release left behind: a record that says deleted, a record that names a cycle with no record.
        P._write_cycle_record(self.root, dict(P.read_cycle_record(self.root, old["cycle_id"]),
                                              deleted_at="2026-09-01T00:00:00Z", deleted_by="delete"), exclusive=False)
        ghost = "cyc_" + "7" * 32
        self.assertIsNone(P.read_cycle_record(self.root, ghost))
        P._write_cycle_record(self.root, dict(P.read_cycle_record(self.root, g["cycle_id"]), parent_cycle_id=ghost),
                              exclusive=False)
        out = P.delete_cycle(self.root, f["cycle_id"])
        self.assertEqual(out["status"], "deleted", out)
        self.assertEqual(sorted((row["cycle_id"], row["before"], row["after"]) for row in out["reparented"]),
                         sorted([(e["cycle_id"], old["cycle_id"], a["cycle_id"]), (g["cycle_id"], ghost, None)]))
        self.assertEqual(self.record(e)["parent_cycle_id"], a["cycle_id"])
        self.assertIsNone(self.record(g)["parent_cycle_id"])
        self.assert_one_parent_line(e, old["cycle_id"], a["cycle_id"])
        self.assert_one_parent_line(g, ghost, None)
        self.survivors_name_survivors()

    def test_t9_6_a_surviving_child_with_no_folder_is_mended_in_its_record(self):
        a = self.child_of("t96-a", "c9-missing", activate=True)
        b = self.child_of("t96-b", "c9-missing", a["cycle_id"])
        h = self.child_of("t96-h", "c9-missing", b["cycle_id"])
        shutil.rmtree(h["cycle_dir"])
        self.assertFalse(self.record(h).get("deleted_at"))
        out = P.delete_cycle(self.root, b["cycle_id"])
        self.assertEqual(out["status"], "deleted", out)
        self.assertEqual(out["reparented"], [{"cycle_id": h["cycle_id"], "before": b["cycle_id"],
                                              "after": a["cycle_id"], "folder": "missing"}])
        self.assertEqual(self.record(h)["parent_cycle_id"], a["cycle_id"])
        self.assertFalse(Path(h["cycle_dir"]).exists(), "nothing puts the folder back")
        self.assert_one_parent_line(h, b["cycle_id"], a["cycle_id"])
        self.survivors_name_survivors()


class C1InlineFinishAfterMoveTest(unittest.TestCase):
    """The route's cycle is the same cycle after it moved; a finish is not refused for the campaign's key."""

    def test_c1_inline_finish_follows_a_cycle_moved_before_it_finished(self):
        fixture = INLINE.PublicInlineFinishTest("test_public_finish_seals_and_exact_replay_returns_one_receipt")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        root = fixture.root
        cycle_id = fixture.cycle["cycle_id"]
        origin = INLINE.artifact_producer.read_campaign(root, fixture.cycle["campaign_id"])
        # A second campaign under another key (the way `begin` makes one).
        other_id = "camp_" + "d" * 32
        created = origin["created_on"]
        locator, suffix = INLINE.artifact_producer.artifact_locator.allocate_locator(root / "campaigns", created, "other-stream")
        other = {"schema_version": 1, "contract": "artifact-producer/v1", "campaign_id": other_id,
                 "key": "other-stream", "slug": "other-stream", "title": "other-stream", "slug_source": "campaign-key",
                 "slug_truncated": False, "locator": locator, "locator_suffix": suffix, "goal": "g",
                 "completion_criterion": {"statement": "done"}, "state": "active", "created_on": created, "cycles": []}
        fd = INLINE.artifact_producer.artifact_admission._acquire_lock(root, 5.0)
        try:
            INLINE.artifact_producer._write_campaign(root, other, exclusive=True)
            INLINE.artifact_producer.artifact_locator.update_indexes(root, [other_id])
        finally:
            INLINE.artifact_producer.artifact_admission._release_lock(root, fd)
        moved = INLINE.artifact_producer.cycle_move(root, cycle_id, campaign=other_id)
        self.assertEqual(moved["status"], "moved", moved)
        new_dir = Path(moved["cycle_dir"])
        evidence = new_dir / fixture.evidence.relative_to(fixture.cycle_dir)
        self.assertTrue(evidence.is_file())
        fixture.command[fixture.command.index("--evidence") + 1] = str(evidence)
        done = fixture.finish()
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout)
        self.assertEqual(receipt["cycle_id"], cycle_id)
        record = INLINE.artifact_producer.read_cycle_record(root, cycle_id)
        self.assertEqual((record["campaign_id"], record["state"]), (other_id, "sealed"))
        # The finish replays as it was, and again after the cycle is moved back out of that campaign.
        replay = fixture.finish()
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertTrue(json.loads(replay.stdout)["replay"])


class C1EnvelopeAfterDeleteTest(TERM._TerminalCommitFixture):
    """A finished route's report sat in a cycle that was deleted: the stored envelope is delivered, and says so."""

    def test_c1_envelope_replay_says_the_cycle_was_deleted(self):
        cycle_dir = self.root / "campaigns/2026-10-01_stream/2026-10-01_report-cycle"
        (cycle_dir / "artifacts").mkdir(parents=True)
        moved = cycle_dir / "artifacts/summary.md"
        self.artifact.rename(moved)
        self.artifact = moved
        self.gates["execute"]["evidence"] = str(moved)
        owner = A2EnvelopeReplayTest.seal_once(self)
        stored = (self._slot() / "owner-envelope.txt").read_text()
        # The producer's record of the deleted cycle (the folder holding the report is gone).
        records = self.root / ".runtime/artifact-producer/v1/cycles"
        records.mkdir(parents=True)
        (records / ("cyc_" + "e" * 32 + ".json")).write_text(json.dumps(
            {"cycle_id": "cyc_" + "e" * 32, "locator": "2026-10-01_report-cycle", "state": "sealed",
             "deleted_at": "2026-10-02T00:00:00Z"}), encoding="utf-8")
        shutil.rmtree(str(cycle_dir))
        gone = A2EnvelopeReplayTest.replay(self, owner)
        self.assertEqual((gone.result, gone.detail), ("completed", "cycle-deleted-after-seal"))
        self.assertEqual(gone.envelope_text, stored)


class C1LockedReadTest(C1ChangesBase):
    """Correction B for the commands: one admission-lock section reads the index at most once."""

    def test_c1_locked_index_read_once(self):
        mover = self.closed_with("lock-move", "c1-lock-a", {"plans/cycle/a.md": b"a\n", "plans/cycle/b.md": b"b\n"},
                                 activate=True)
        peer = self.closed_with("lock-peer", "c1-lock-b")
        locked_reads = []
        real_load = adm.load_index

        def counting(root):
            if adm.holds_lock(Path(root)):
                locked_reads.append(1)
            return real_load(root)

        def reads(call):
            locked_reads.clear()
            with mock.patch.object(adm, "load_index", counting):
                call()
            return len(locked_reads)

        self.assertEqual(reads(lambda: P.cycle_mark(self.root, mover["cycle_id"], discard=True)), 0)
        manifest = self.manifest(mover)
        by_id = {row["artifact_id"]: row for row in manifest["artifacts"]}
        other = next(r["locator"]["path"] for r in manifest["artifact_revisions"]
                     if by_id[r["artifact_id"]]["role"] != "primary")
        self.assertLessEqual(reads(lambda: P.cycle_mark(self.root, mover["cycle_id"], primary=other)), 1)
        self.assertLessEqual(reads(lambda: P.cycle_move(self.root, mover["cycle_id"], campaign=peer["campaign_id"])), 1)
        # A folder renamed by hand inside its campaign: the same one read.
        folder = self.cycle_path(mover)
        os.rename(str(folder), str(folder.with_name("hand-renamed")))
        self.assertLessEqual(reads(lambda: P.reconcile_root(self.root)), 1)
        self.assertEqual(self.record(mover)["locator"], "hand-renamed")
        self.assertLessEqual(reads(lambda: P.delete_cycle(self.root, mover["cycle_id"])), 1)
        self.assertLessEqual(reads(lambda: P.delete_campaign(self.root, peer["campaign_id"])), 1)


class C1RealRecorderTest(C1ChangesBase):
    def test_c1_real_history_recorder_takes_the_command_lines(self):
        module = H
        with mock.patch.dict(sys.modules, {"artifact_history": module}):
            mover = self.closed_with("real-move", "c1-real-a", {"plans/cycle/a.md": b"a\n"}, activate=True)
            peer = self.closed_with("real-peer", "c1-real-b")
            P.cycle_move(self.root, mover["cycle_id"], campaign=peer["campaign_id"], reason="regroup")
            P.cycle_mark(self.root, mover["cycle_id"], discard=True)
            P.delete_cycle(self.root, mover["cycle_id"])
            P.delete_campaign(self.root, peer["campaign_id"])
            events = list(module.iter_events(self.root))
            changes = sorted((e["target"]["type"], e["operation"], e["field"]) for e in events
                             if e["kind"] == "lifecycle" and e["operation"] != "update")
            self.assertEqual(changes, [("campaign", "delete", "state"), ("cycle", "add", "disposition"),
                                       ("cycle", "delete", "state"), ("cycle", "delete", "state"),
                                       ("cycle", "move", "campaign")])
            for name in (mover, peer):
                self.assertNotIn("history_pending", self.record(name))
            self.assertNotIn("history_pending", P.read_campaign_tombstone(self.root, peer["campaign_id"]))


class C1RebuildFindsHandMoveTest(C1ChangesBase):
    def test_c1_rebuild_and_recover_find_a_hand_made_move(self):
        mover = self.closed_with("rb-move", "c1-rb-src", {"plans/cycle/a.md": b"a\n"}, activate=True)
        peer = self.closed_with("rb-peer", "c1-rb-dst")
        source = Path(mover["cycle_dir"])
        os.rename(str(source), str(P.campaign_dir(self.root, peer["campaign_id"]) / source.name))
        rebuilt = adm.rebuild_index(self.root)
        self.assertEqual(rebuilt.cycles[mover["cycle_id"]]["campaign_id"], peer["campaign_id"])
        self.assertEqual(self.record(mover)["campaign_id"], peer["campaign_id"])
        # A second hand move, found by `recover` this time.
        again = self.closed_with("rb-move-2", "c1-rb-src")
        source = Path(again["cycle_dir"])
        os.rename(str(source), str(P.campaign_dir(self.root, peer["campaign_id"]) / source.name))
        out = P.recover(self.root)
        self.assertEqual(out["status"], "recovered", out)
        self.assertEqual(self.record(again)["campaign_id"], peer["campaign_id"])
        self.assertTrue(adm.verify_index(self.root).ok)


class D1LegacyConsumersTest(C1ChangesBase):
    """§45 D-123/D-126 for the older tools: a finished cycle is read as it is now, a mark is only a mark."""

    def legacy_superseded(self, result, by):
        record = self.record(result)
        record.update(state="superseded", superseded_by=list(by), superseded_event_id="evt_" + "7" * 32)
        P._write_cycle_record(self.root, record, exclusive=False)

    def test_d1_mark_cycle_superseded_is_a_disposition_not_a_state(self):
        first = self.closed_with("d1-sup-a", "d1-sup", activate=True)
        second = self.closed_with("d1-sup-b", "d1-sup")
        manifest_before = (self.cycle_path(first) / "manifest.json").read_bytes()
        before = self.record(first)
        out = P.mark_cycle_superseded(self.root, first["cycle_id"], superseded_by=[second["cycle_id"]],
                                      superseded_event_id="evt_" + "1" * 32)
        self.assertEqual(out["status"], "updated", out)
        after = self.record(first)
        mark = after["disposition"]
        self.assertEqual((mark["kind"], mark["superseded_by"]), ("superseded", [second["cycle_id"]]))
        self.assertEqual(mark["superseded_event_id"], "evt_" + "1" * 32)
        # Only the one field moved: the cycle is as finished as it was and stays writable.
        self.assertEqual({k: v for k, v in after.items() if k not in {"disposition", "control_record_digest"}},
                         {k: v for k, v in before.items() if k != "control_record_digest"})
        self.assertEqual((self.cycle_path(first) / "manifest.json").read_bytes(), manifest_before)
        self.assertEqual(self.lines(kind="lifecycle", field="disposition", target_id=first["cycle_id"])[0]["operation"], "add")
        self.edit(first, "plans/cycle/plan.md", b"edited after the mark\n")
        self.assertEqual(self.refresh(first)["status"], "emitted")
        # A mark with no named replacement is still a mark (the older callers passed none).
        P.mark_cycle_superseded(self.root, second["cycle_id"], superseded_by=[], superseded_event_id="evt_" + "2" * 32)
        self.assertEqual(self.record(second)["state"], "sealed")
        self.assertEqual(P.cycle_disposition(self.record(second))["kind"], "superseded")
        # The same call again is the same mark, and an unknown cycle is still unknown.
        P.mark_cycle_superseded(self.root, second["cycle_id"], superseded_by=[], superseded_event_id="evt_" + "2" * 32)
        with self.assertRaises(P.ProducerError) as ctx:
            P.mark_cycle_superseded(self.root, "cyc_" + "9" * 32, superseded_by=[], superseded_event_id="evt_" + "3" * 32)
        self.assertEqual(ctx.exception.code, "cycle-unknown")

    def test_d1_mark_cycle_superseded_takes_an_open_cycle_too(self):
        route, route_file, live = self.open_with("d1-open-mark", "d1-open")
        P.mark_cycle_superseded(self.root, live["cycle_id"], superseded_by=[], superseded_event_id="evt_" + "4" * 32)
        self.assertEqual(self.record(live)["state"], "open")
        self.assertEqual(P.cycle_disposition(self.record(live))["kind"], "superseded")

    def test_d1_campaign_superseded_counts_marks_old_states_and_ignores_deleted(self):
        marked = self.closed_with("d1-camp-a", "d1-camp", activate=True)
        legacy = self.closed_with("d1-camp-b", "d1-camp")
        dropped = self.closed_with("d1-camp-c", "d1-camp")
        live = self.closed_with("d1-camp-d", "d1-camp")
        campaign_id = marked["campaign_id"]
        P.cycle_mark(self.root, marked["cycle_id"], discard=True)
        self.legacy_superseded(legacy, [live["cycle_id"]])
        P.delete_cycle(self.root, dropped["cycle_id"])
        with self.assertRaises(P.ProducerError) as ctx:
            P.mark_campaign_superseded(self.root, campaign_id)
        self.assertEqual(ctx.exception.code, "campaign-has-live-cycles")
        P.cycle_mark(self.root, live["cycle_id"], superseded_by=[marked["cycle_id"]])
        self.assertEqual(P.mark_campaign_superseded(self.root, campaign_id)["state"], "superseded")
        self.assertEqual(P.read_campaign(self.root, campaign_id)["state"], "superseded")

    def test_d1_resplit_reads_a_marked_cycle_as_superseded(self):
        import artifact_resplit as RS
        first = self.closed_with("d1-rs-a", "d1-rs", activate=True)
        second = self.closed_with("d1-rs-b", "d1-rs")
        self.assertEqual(RS._supersede_campaign(self.root, first["cycle_id"], dry_run=True)["code"],
                         "campaign-retained-live-cycles")
        P.mark_cycle_superseded(self.root, first["cycle_id"], superseded_by=[second["cycle_id"]],
                                superseded_event_id="evt_" + "5" * 32)
        self.legacy_superseded(second, [first["cycle_id"]])
        out = RS._supersede_campaign(self.root, first["cycle_id"], dry_run=True)
        self.assertEqual(out["code"], "would-supersede", out)
        self.assertEqual(RS.record_display_state(self.record(first)), "superseded")
        self.assertEqual(RS.record_display_state(self.record(second)), "superseded")

    def test_d1_title_candidate_reads_the_current_primary_bytes(self):
        import artifact_cycle_titles as CT
        result = self.closed_with("d1-title", "d1-title", {"plans/cycle/plan.md": b"# First heading of the plan\n"},
                                  activate=True)

        def context():
            directory = self.cycle_path(result)
            return CT.CycleContext(record=self.record(result), manifest=self.manifest(result), cycle_dir=directory,
                                   campaign={}, v2_title=None, route_text=None)

        self.assertEqual(CT._primary_heading_candidate(context()), ("First heading of the plan", None))
        # Edited after the finish, the manifest not yet brought up to date: the file as it is now answers.
        self.edit(result, "plans/cycle/plan.md", b"# Plan after the rewrite\n")
        self.assertEqual(CT._primary_heading_candidate(context()), ("Plan after the rewrite", None))
        # Brought up to date: the primary has two revisions, and the newest one is the one read.
        self.assertEqual(self.refresh(result)["status"], "emitted")
        self.edit(result, "plans/cycle/plan.md", b"# Plan after the second rewrite\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        self.assertEqual(CT._primary_heading_candidate(context()), ("Plan after the second rewrite", None))
        # A primary that is gone is still just missing.
        (self.cycle_path(result) / "artifacts/plans/cycle/plan.md").unlink()
        self.assertEqual(CT._primary_heading_candidate(context()), (None, "primary-missing"))

    def test_d1_title_eligibility_does_not_ask_for_a_state(self):
        import artifact_cycle_titles as CT
        result = self.closed_with("d1-elig", "d1-elig", activate=True)
        identity = L.read_root_identity(self.root)
        path = self.cycle_path(result) / "manifest.json"
        raw = path.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
        self.assertIsNone(CT._eligibility_check(self.record(result), raw, parsed, identity))
        self.legacy_superseded(result, [])
        self.assertIsNone(CT._eligibility_check(self.record(result), raw, parsed, identity))
        P.cycle_mark(self.root, result["cycle_id"], discard=True)
        self.assertIsNone(CT._eligibility_check(self.record(result), raw, parsed, identity))
        # What stays: the title is bound to the manifest the record names.
        record = dict(self.record(result), manifest_digest="sha256:" + "0" * 64)
        self.assertEqual(CT._eligibility_check(record, raw, parsed, identity), "record-digest-mismatch")
        self.assertEqual(CT._eligibility_check(None, raw, parsed, identity), "record-missing")

    def test_d1_workflow_groups_and_review_read_a_finished_cycle_by_its_files(self):
        import artifact_workflow_groups as WG
        import artifact_workflow_group_review as WGR
        kept = self.closed_with("d1-wg-a", "d1-wg", activate=True)
        legacy = self.closed_with("d1-wg-b", "d1-wg")
        gone = self.closed_with("d1-wg-c", "d1-wg")
        campaign = P.read_campaign(self.root, kept["campaign_id"])
        self.legacy_superseded(legacy, [kept["cycle_id"]])
        P.cycle_mark(self.root, kept["cycle_id"], discard=True)
        for item in (kept, legacy):
            directory = self.cycle_path(item)
            value = WG._manifest(self.root, campaign, item["cycle_id"], directory)
            self.assertEqual(value["cycle"]["cycle_id"], item["cycle_id"])
        selection = WGR.select_targets(self.root, None, cycles=[kept["cycle_id"], legacy["cycle_id"]])
        self.assertEqual(sorted(cid for ids in selection.by_campaign.values() for cid in ids),
                         sorted([kept["cycle_id"], legacy["cycle_id"]]), selection.skipped)
        # A cycle that was deleted is gone, not "not sealed".
        P.delete_cycle(self.root, gone["cycle_id"])
        selection = WGR.select_targets(self.root, None, cycles=[gone["cycle_id"]])
        self.assertEqual(selection.skipped, [{"cycle_id": gone["cycle_id"], "reason": "cycle-unknown"}])
        self.assertEqual(selection.by_campaign, {})

    def test_d1_the_unused_start_or_resume_helper_is_gone(self):
        self.assertFalse(hasattr(L, "decide_cycle_start_or_resume"))
        # The default it described lives in the producer: changed material input makes a child cycle.
        self.assertTrue(hasattr(P, "cycle_route_admission"))

    def test_d1_batch_digest_guard_stays(self):
        recovery = Path(__file__).with_name("dispatch-recovery.py").read_text(encoding="utf-8")
        self.assertIn('raise RecoveryError("recovery-source-manifest-digest-mismatch")', recovery)


class Gap2LocksTest(C1ChangesBase):
    """§45 D-124/D-125 after the impl review: nothing is walked or hashed under the admission lock,
    and a refresh's history lines and its manifest go out in one admission section."""

    def probe(self, seen, real):
        """Wrap `real` so each call notes whether the admission lock is held and whether another
        writer could take it right now."""
        def watching(*args, **kwargs):
            held = adm.holds_lock(self.root)
            fd = adm.try_acquire_lock(self.root)
            seen.append((held, fd is not None))
            if fd is not None:
                adm._release_lock(self.root, fd)
            return real(*args, **kwargs)
        return watching

    def test_gap2_refinalize_scans_outside_admission(self):
        files = {f"plans/cycle/f{i}.md": f"f{i}\n".encode() for i in range(6)}
        result = self.closed_with("gap2-reclose", "gap2-reclose", files, activate=True)
        for i in range(3):
            self.edit(result, f"plans/cycle/f{i}.md", f"edited {i}\n".encode())
        seen = []
        with mock.patch.object(P, "_scan_cycle_facts", self.probe(seen, P._scan_cycle_facts)), \
                mock.patch.object(P, "_stream_file_facts", self.probe(seen, P._stream_file_facts)), \
                mock.patch.object(P, "_walk_files", self.probe(seen, P._walk_files)):
            out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertTrue(out["refreshed"], out)
        self.assertGreaterEqual(len(seen), 4)
        self.assertEqual(set(seen), {(False, True)})  # every walk and every hash ran with the lock free

    def test_gap2_refinalize_rescans_once_when_a_file_moves_under_the_scan(self):
        result = self.closed_with("gap2-rescan", "gap2-rescan", {"plans/cycle/a.md": b"a\n"}, activate=True)
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        real = P._prescan_cycle
        calls = []

        def prescan(*args, **kwargs):
            out = real(*args, **kwargs)
            calls.append(adm.holds_lock(self.root))
            if len(calls) == 1:
                self.edit(result, "plans/cycle/a.md", b"a, edited again, longer\n")
            return out

        with mock.patch.object(P, "_prescan_cycle", prescan):
            out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(calls, [False, False])
        self.assertTrue(out["refreshed"], out)
        (row,) = [r for r in self.manifest(result)["artifact_revisions"] if r["locator"]["path"].endswith("a.md")]
        self.assertEqual(row["content_digest"], "sha256:" + hashlib.sha256(b"a, edited again, longer\n").hexdigest())

    def test_gap2_refinalize_leaves_it_for_the_next_look_after_two_misses(self):
        result = self.closed_with("gap2-giveup", "gap2-giveup", {"plans/cycle/a.md": b"a\n"}, activate=True)
        raw = (Path(result["cycle_dir"]) / "manifest.json").read_bytes()
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        real = P._prescan_cycle
        calls = []

        def prescan(*args, **kwargs):
            out = real(*args, **kwargs)
            calls.append(1)
            self.edit(result, "plans/cycle/a.md", b"a" * (len(calls) + 5) + b"\n")
            return out

        with mock.patch.object(P, "_prescan_cycle", prescan):
            out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(len(calls), 2)
        self.assertEqual((out["status"], out["refreshed"]), ("already-sealed", False), out)
        self.assertEqual((Path(result["cycle_dir"]) / "manifest.json").read_bytes(), raw)
        self.assertEqual(self.refresh(result)["status"], "emitted")  # the next look finds the same change

    def test_gap2_recover_reconcile_scans_outside_admission(self):
        mover = self.closed_with("gap2-recover", "gap2-recover", {"plans/cycle/a.md": b"a\n"}, activate=True)
        self.closed_with("gap2-recover-peer", "gap2-recover-peer")
        folder = self.cycle_path(mover)
        os.rename(str(folder), str(folder.with_name("hand-renamed")))
        seen = []
        with mock.patch.object(P, "_scan_layout", self.probe(seen, P._scan_layout)):
            out = P.recover(self.root)
        self.assertIn(out["status"], {"recovered", "recovered-with-problems"})
        self.assertTrue(seen)
        self.assertEqual(set(seen), {(False, True)})
        self.assertEqual(self.record(mover)["locator"], "hand-renamed")  # the hand-made move was still found

    def test_gap2_reconcile_skips_a_change_that_is_gone_when_the_lock_is_taken(self):
        mover = self.closed_with("gap2-undo", "gap2-undo", {"plans/cycle/a.md": b"a\n"}, activate=True)
        folder = self.cycle_path(mover)
        renamed = folder.with_name("hand-renamed")
        os.rename(str(folder), str(renamed))
        real = P._scan_layout
        calls = []

        def scan(root):
            out = real(root)
            calls.append(1)
            if len(calls) == 1:
                os.rename(str(renamed), str(folder))  # put back before the lock is taken
            return out

        with mock.patch.object(P, "_scan_layout", scan):
            P.reconcile_root(self.root)
        self.assertEqual(self.record(mover)["locator"], folder.name)
        self.assertEqual(self.lines(field="path"), [])

    def test_gap2_concurrent_move_between_check_and_publish_writes_no_history(self):
        moved = self.closed_with("gap2-move", "gap2-move-a", {"plans/cycle/a.md": b"a\n"}, activate=True)
        peer = self.closed_with("gap2-move-peer", "gap2-move-b")
        self.edit(moved, "plans/cycle/a.md", b"a, edited\n")
        real_recheck, real_acquire = P._recheck_candidate, adm._acquire_lock
        state = {"checked": False, "moved": False}

        def checking(*args, **kwargs):
            ok = real_recheck(*args, **kwargs)
            state["checked"] = True
            return ok

        def acquire(root, timeout, now=None):
            if state["checked"] and not state["moved"]:
                state["moved"] = True  # the files were checked; the publication's lock is not yet taken
                P.cycle_move(self.root, moved["cycle_id"], campaign=peer["campaign_id"])
            return real_acquire(root, timeout, now)

        def file_lines():
            return [e for e in self.history.published if e.get("kind") == "artifact"]

        with mock.patch.object(P, "_recheck_candidate", checking), mock.patch.object(adm, "_acquire_lock", acquire):
            out = self.refresh(moved)
        self.assertTrue(state["moved"])
        self.assertEqual((out["status"], out["reason"]), ("skipped", "superseded"), out)
        self.assertEqual(file_lines(), [])  # a refresh that was not published leaves no history
        self.assertEqual([m for m in self.history.made if m.get("kind") == "artifact"], [])
        new_dir = self.cycle_path(moved)
        self.assertNotEqual(new_dir, Path(moved["cycle_dir"]))
        before = (new_dir / "manifest.json").read_bytes()
        self.assertEqual(self.refresh(moved)["status"], "emitted")  # the next one, from the new place
        self.assertNotEqual((new_dir / "manifest.json").read_bytes(), before)
        (line,) = file_lines()
        self.assertEqual((line["operation"], line["field"]), ("update", "artifacts/plans/cycle/a.md"))
        self.assertTrue(line["target_path"].startswith(new_dir.relative_to(self.root).as_posix()))

    def test_gap2_history_and_manifest_share_one_admission_section(self):
        result = self.closed_with("gap2-one", "gap2-one", {"plans/cycle/a.md": b"a\n"}, activate=True)
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        sections = []
        real_acquire, real_release = adm._acquire_lock, adm._release_lock
        events = []

        def acquire(root, timeout, now=None):
            fd = real_acquire(root, timeout, now)
            sections.append(len(events))
            events.append("lock")
            return fd

        def release(root, fd):
            events.append("unlock")
            return real_release(root, fd)

        raw = (Path(result["cycle_dir"]) / "manifest.json").read_bytes()
        order = []
        self.history.on_publish = lambda root, lines: order.append(
            ((Path(result["cycle_dir"]) / "manifest.json").read_bytes() == raw, events[-1]))
        with mock.patch.object(adm, "_acquire_lock", acquire), mock.patch.object(adm, "_release_lock", release):
            self.assertEqual(self.refresh(result)["status"], "emitted")
        self.assertEqual(order, [(True, "lock")])  # history first, with the section still open ...
        self.assertEqual(events, ["lock", "unlock"])  # ... and the manifest in that same section
        self.assertNotEqual((Path(result["cycle_dir"]) / "manifest.json").read_bytes(), raw)


    # -- item 8 (a): an open cycle's first close reads its files before the admission lock ----------
    PROBE_ACQUIRE = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import artifact_admission as a\n"
        "try:\n"
        "    fd = a._acquire_lock(Path(sys.argv[2]), 1.0); a._release_lock(Path(sys.argv[2]), fd); print('acquired')\n"
        "except Exception as e:\n"
        "    print('timed-out:' + type(e).__name__)\n")

    def first_close(self, slug, extra=5):
        """An open cycle with `extra + 1` files whose route is closed: the next `finalize` is its first."""
        self.activate()
        route, route_file, result = self.open_with(slug, slug)
        for i in range(extra):
            self.write_output(result, f"plans/cycle/g{i}.md", f"g{i}\n".encode())
        self.close(route, route_file)
        return result

    def payload_reads(self, result, on_read):
        """Patches that call `on_read(kind, path)` wherever a payload byte of this cycle is read."""
        payload = str(Path(result["cycle_dir"]) / "artifacts")
        real_read_bytes, real_stream, real_sha = Path.read_bytes, P._stream_file_facts, P.artifact_lifecycle._sha256_path

        def read_bytes(path):
            if str(path).startswith(payload):
                on_read("read_bytes", path)
            return real_read_bytes(path)

        def stream(path):
            if str(path).startswith(payload):
                on_read("stream", path)
            return real_stream(path)

        def sha(path, *args, **kwargs):
            if str(path).startswith(payload):
                on_read("sha", path)
            return real_sha(path, *args, **kwargs)
        return [mock.patch.object(Path, "read_bytes", read_bytes), mock.patch.object(P, "_stream_file_facts", stream),
                mock.patch.object(P.artifact_lifecycle, "_sha256_path", sha)]

    def test_gap2_first_finalize_scans_outside_admission(self):
        result = self.first_close("gap2-first")
        seen, reads, locked, holds, started = [], [], [], [], {}
        real_acquire, real_release = adm._acquire_lock, adm._release_lock

        def acquire(root, timeout, now=None):
            fd = real_acquire(root, timeout, now)
            started[fd] = time.monotonic()
            return fd

        def release(root, fd):
            holds.append(time.monotonic() - started.pop(fd, time.monotonic()))
            return real_release(root, fd)

        def on_read(kind, path):
            reads.append(str(path))
            held = adm.holds_lock(self.root)
            fd = adm.try_acquire_lock(self.root)
            if fd is not None:
                real_release(self.root, fd)
            if kind == "read_bytes":
                locked.extend([str(path)] if held else [])
            else:
                seen.append((held, fd is not None))
            time.sleep(0.1)

        patches = self.payload_reads(result, on_read) + [
            mock.patch.object(P, "_walk_files", self.probe(seen, P._walk_files)),
            mock.patch.object(P, "_scan_cycle_facts", self.probe(seen, P._scan_cycle_facts)),
            mock.patch.object(adm, "_acquire_lock", acquire), mock.patch.object(adm, "_release_lock", release)]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(out["status"], "sealed", out)
        self.assertGreaterEqual(len(reads), 6)  # every file is read at least once
        self.assertEqual(set(seen), {(False, True)})  # every walk and chunked read ran with the lock free
        # The one read left under the lock is the title's peek at the primary heading (a size-capped file).
        self.assertLessEqual(len(locked), 1, locked)
        self.assertLess(max(holds), 1.0, holds)  # the lock covered the short write section only

    def test_gap2_first_finalize_lets_another_process_take_the_admission_lock_while_it_reads(self):
        result = self.first_close("gap2-first-proc")
        answers = []

        def on_read(kind, path):
            if answers:
                return
            utilities = str(Path(__file__).resolve().parent)
            out = subprocess.run([sys.executable, "-c", self.PROBE_ACQUIRE, utilities, str(self.root)],
                                 capture_output=True, text=True, timeout=60)
            answers.append((out.returncode, out.stdout.strip(), out.stderr.strip()[-300:]))

        with contextlib.ExitStack() as stack:
            for patch in self.payload_reads(result, on_read):
                stack.enter_context(patch)
            out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(out["status"], "sealed", out)
        # The child's exit code and its output are both the answer: it got the flock, and ran clean.
        self.assertEqual(answers, [(0, "acquired", "")])

    def test_gap2_first_finalize_rescans_once_when_a_file_moves_under_the_scan(self):
        result = self.first_close("gap2-first-move", extra=2)
        real = P._prescan_cycle
        calls = []

        def prescan(*args, **kwargs):
            out = real(*args, **kwargs)
            calls.append(adm.holds_lock(self.root))
            if len(calls) == 1:
                self.edit(result, "plans/cycle/g0.md", b"g0, edited again, longer\n")
            return out

        with mock.patch.object(P, "_prescan_cycle", prescan):
            out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(calls, [False, False])  # read again with the lock released, not trusted
        self.assertEqual(out["status"], "sealed", out)
        (row,) = [r for r in self.manifest(result)["artifact_revisions"] if r["locator"]["path"].endswith("g0.md")]
        self.assertEqual(row["content_digest"], "sha256:" + hashlib.sha256(b"g0, edited again, longer\n").hexdigest())

    def test_gap2_first_finalize_keeps_its_refusals(self):
        # A path the manifest cannot name still fails the close, as it did before the read moved out of the lock.
        result = self.first_close("gap2-first-bad", extra=1)
        stray = Path(result["cycle_dir"]) / "stray.md"
        stray.write_text("x", encoding="utf-8")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(caught.exception.code, "output-invalid")
        self.assertIn("file-outside-artifacts:stray.md", caught.exception.detail)
        self.assertEqual(self.record(result)["state"], "open")
        stray.unlink()
        self.assertEqual(P.finalize(self.root, cycle_id=result["cycle_id"])["status"], "sealed")
        # A live review lease still refuses the close.
        held = self.first_close("gap2-first-lease", extra=1)
        lease = P._review_lease_path(self.root, held["cycle_id"], "att-gap2-first")
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
        with mock.patch.object(P, "_live_review_lease", return_value=lease):
            with self.assertRaises(P.ProducerError) as caught:
                P.finalize(self.root, cycle_id=held["cycle_id"])
        self.assertEqual(caught.exception.code, "cycle-finalize-blocked-live-review")
        self.assertEqual(self.record(held)["state"], "open")
        self.assertFalse((Path(held["cycle_dir"]) / "manifest.json").exists())
        # A close that stopped after its manifest was written is finished by `recover`.
        crashed = self.first_close("gap2-first-crash", extra=1)
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=crashed["cycle_id"], crash_after_manifest=True)
        self.assertIn(crashed["cycle_id"], P.recover(self.root)["producer"]["rolled_forward"])
        self.assertEqual(self.record(crashed)["state"], "sealed")


class E1RecorderLinesTest(C1ChangesBase):
    """The merged recorder (#90): every line this branch makes is one it takes, and it is published."""

    def keep(self, slug, campaign_key, files=None, *, activate=False):
        """A closed cycle; unlike `closed_with` the lines of its close stay counted."""
        if activate:
            self.activate()
        route, route_file = self.route(slug=slug, campaign_key=campaign_key)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for rel, data in (files or {"plans/cycle/plan.md": b"plan body\n"}).items():
            self.write_output(result, rel, data)
        self.close(route, route_file)
        self.assertEqual(P.finalize(self.root, cycle_id=result["cycle_id"])["status"], "sealed")
        return result

    def pending(self):
        """(owner, lines) of every record that still holds lines for the recorder."""
        found = []
        for record in P.list_cycle_records(self.root):
            if record.get("history_pending"):
                found.append(record["cycle_id"])
        runtime = self.root / ".runtime/artifact-producer/v1/campaigns"
        for path in sorted(runtime.glob("*.json")) if runtime.is_dir() else []:
            if json.loads(path.read_text(encoding="utf-8")).get("history_pending"):
                found.append(path.stem)
        return found

    def scenario(self):
        """Every kind of lifecycle line: close, refresh, move, parent, marks, primary, deletes, hand-made changes."""
        mover = self.keep("e1-mover", "e1-a", {"plans/cycle/a.md": b"a\n", "plans/cycle/b.md": b"b\n"},
                          activate=True)
        peer = self.keep("e1-peer", "e1-b", {"plans/cycle/p.md": b"p\n"})
        third = self.keep("e1-third", "e1-c", {"plans/cycle/t.md": b"t\n"})
        fourth = self.keep("e1-fourth", "e1-d", {"plans/cycle/f.md": b"f\n"})
        fifth = self.keep("e1-fifth", "e1-e", {"plans/cycle/g.md": b"g\n"})
        self.edit(mover, "plans/cycle/a.md", b"a, edited\n")
        self.write_output(mover, "plans/cycle/c.md", b"c\n")
        (Path(mover["cycle_dir"]) / "artifacts/plans/cycle/b.md").unlink()
        self.assertEqual(self.refresh(mover)["status"], "emitted")
        self.assertEqual(P.cycle_move(self.root, mover["cycle_id"], campaign=peer["campaign_id"],
                                      parent=peer["cycle_id"], reason="regroup")["status"], "moved")
        P.cycle_move(self.root, mover["cycle_id"], no_parent=True)
        P.cycle_mark(self.root, mover["cycle_id"], discard=True)
        P.cycle_mark(self.root, mover["cycle_id"], superseded_by=[peer["cycle_id"]], reason="redone")
        P.cycle_mark(self.root, mover["cycle_id"], clear=True)
        roles = self.roles(mover)
        other = next(rel for rel, role in roles.items() if role != "primary")
        P.cycle_mark(self.root, mover["cycle_id"], primary=other)
        # The campaign folder of `third` goes by hand before any command looks: delete and mark still name a path.
        shutil.rmtree(str(P.campaign_dir(self.root, third["campaign_id"])))
        P.cycle_mark(self.root, third["cycle_id"], discard=True)
        P.delete_cycle(self.root, third["cycle_id"])
        # Hand-made changes found by the next look: a campaign rename, a cycle carried over, a cycle removed.
        folder = P.campaign_dir(self.root, peer["campaign_id"])
        peer_cycle, carried = Path(self.cycle_path(peer)), Path(self.cycle_path(fourth))
        renamed = folder.with_name("e1-renamed")
        os.rename(str(folder), str(renamed))
        os.rename(str(carried), str(renamed / carried.name))
        shutil.rmtree(str(renamed / peer_cycle.name))
        self.assertEqual(P.reconcile_root(self.root)["status"], "reconciled")
        P.delete_campaign(self.root, fifth["campaign_id"], reason="done with it")
        # A whole campaign removed by hand.
        sixth = self.keep("e1-sixth", "e1-f", {"plans/cycle/s.md": b"s\n"})
        shutil.rmtree(str(P.campaign_dir(self.root, sixth["campaign_id"])))
        self.assertEqual(P.reconcile_root(self.root)["status"], "reconciled")
        fd = adm._acquire_lock(self.root, 5.0)  # the caller of this one holds the admission lock
        try:
            P.record_campaign_state_line(self.root, mover["campaign_id"], before="active", after="closed",
                                         reason=None, event_id=None, command="campaign-close")
        finally:
            adm._release_lock(self.root, fd)
        return mover, peer, third, fourth, fifth, sixth

    def roles(self, result):
        document = json.loads((self.cycle_path(result) / "manifest.json").read_text(encoding="utf-8"))
        by_id = {row["artifact_id"]: row for row in document["artifacts"]}
        return {row["locator"]["path"]: by_id[row["artifact_id"]]["role"] for row in document["artifact_revisions"]}

    def test_e1_every_line_the_branch_makes_is_one_the_recorder_takes(self):
        self.scenario()
        kinds = {(m["target_type"], m["field"], m["operation"]) for m in self.history.made}
        for wanted in [("cycle", "state", "update"), ("cycle", "state", "delete"), ("campaign", "state", "delete"),
                       ("campaign", "state", "update"), ("campaign", "path", "update"),
                       ("cycle", "campaign", "move"), ("cycle", "parent", "update"),
                       ("cycle", "disposition", "add"), ("cycle", "disposition", "update"),
                       ("cycle", "primary", "update"), ("artifact", "artifacts/plans/cycle/a.md", "update")]:
            self.assertIn(wanted, kinds)
        self.assertEqual(self.history.rejected, [])
        for line in self.history.made:
            H.make_event(**line)  # raises on a line the recorder would drop
            self.assertTrue(line["target_path"], line)
        self.assertEqual(self.pending(), [])

    def test_e1_the_real_recorder_publishes_every_line_and_nothing_stays_pending(self):
        made, refused = [], []
        real_make = H.make_event

        def spy(**kwargs):
            try:
                event = real_make(**kwargs)
            except Exception:
                refused.append(kwargs)
                raise
            made.append(event["event_id"])
            return event

        with mock.patch.dict(sys.modules, {"artifact_history": H}), mock.patch.object(H, "make_event", spy):
            mover, peer, third, fourth, fifth, sixth = self.scenario()
            self.assertEqual(refused, [])
            self.assertEqual(self.pending(), [])
            for result in (mover, peer, third, fourth, fifth, sixth):
                self.assertNotIn("history_pending", self.record(result))
            events = list(H.iter_events(self.root))
            self.assertGreater(len(events), 20)
            self.assertEqual(sorted(e["event_id"] for e in events), sorted(set(made)))
            files = sorted((self.root / ".runtime/artifact-producer/v1/history").glob("*/*.jsonl"))
            self.assertEqual(len(files), len(events))
            # One close line for each first close, however many commands came after.
            closes = [e for e in events if e["target"]["type"] == "cycle" and e["field"] == "state"
                      and e["operation"] == "update"]
            self.assertEqual(sorted(e["target"]["id"] for e in closes),
                             sorted(r["cycle_id"] for r in (mover, peer, third, fourth, fifth, sixth)))

    def test_e1_a_cycle_whose_folder_was_never_found_leaves_a_valid_line_at_its_last_place(self):
        result = self.keep("e1-lost", "e1-lost", activate=True)
        record = self.record(result)
        expected = P._cycle_rel(self.root, Path(result["cycle_dir"]))
        self.assertEqual(P._last_known_path(self.root, record, ""), expected)
        self.assertEqual(P._last_known_path(self.root, record, "campaigns/x/y"), "campaigns/x/y")
        shutil.rmtree(str(P.campaign_dir(self.root, result["campaign_id"])))
        self.assertEqual(P._last_known_path(self.root, record, ""), expected)  # from the record alone
        fd = adm._acquire_lock(self.root, 5.0)
        try:
            P._tombstone_cycle_locked(self.root, record, where=P._last_known_path(self.root, record, ""),
                                      command="reconcile", stamp="1", reason="reconcile", now=None, by="rule",
                                      digest=record.get("manifest_digest"))
            P._flush_cycle_pending_locked(self.root, result["cycle_id"])
        finally:
            adm._release_lock(self.root, fd)
        (line,) = self.lines(target_id=result["cycle_id"], operation="delete")
        H.make_event(**line)
        self.assertEqual(line["target_path"], expected)
        self.assertEqual(self.history.rejected, [])

    def test_e1_without_a_recorder_lines_wait_and_the_recorder_takes_them_later(self):
        with mock.patch.dict(sys.modules, {"artifact_history": H}):
            with mock.patch.object(P, "_history_module", return_value=None):
                result = self.keep("e1-wait", "e1-wait", activate=True)
                P.cycle_mark(self.root, result["cycle_id"], discard=True)
                waiting = self.record(result)["history_pending"]
                self.assertEqual([m["field"] for m in waiting], ["state", "disposition"])
                self.assertEqual(self.pending(), [result["cycle_id"]])
            self.assertEqual(P.deliver_pending_history(self.root), 2)
            self.assertEqual(self.pending(), [])
            self.assertEqual(len(list(H.iter_events(self.root))), 2)

    def test_e1_no_op_refresh_writes_nothing_with_the_real_recorder(self):
        with mock.patch.dict(sys.modules, {"artifact_history": H}):
            result = self.keep("e1-noop", "e1-noop", {"plans/cycle/a.md": b"a\n"}, activate=True)
            self.edit(result, "plans/cycle/a.md", b"a, edited\n")
            self.assertEqual(self.refresh(result)["status"], "emitted")
            state = self.tree_state(result)
            for trigger in ("turn-end", "explicit", "supervisor-poll"):
                self.assertEqual(self.refresh(result, trigger=trigger)["status"], "unchanged")
            self.assertEqual(state, self.tree_state(result))

    def test_e1_actor_follows_the_recorder_rules(self):
        with mock.patch.dict(sys.modules, {"artifact_history": H}):
            self.assertEqual(P._history_actor("human"), {"actor": H.actor_from_env("human")})
            self.assertEqual(P._history_actor("rule")["actor"]["by"], "rule")
            env = {"AGENT_DISPATCH_ATTEMPT_ID": "att-1", "AGENT_DISPATCH_CURRENT_HARNESS": "claude",
                   "AGENT_ROUTE_ID": "rt-1"}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(P._history_actor("human")["actor"],
                                 {"by": "agent", "session": "att-1", "harness": "claude", "route": "rt-1",
                                  "attempt": "att-1"})
                self.assertEqual(P._history_actor("rule")["actor"],
                                 {"by": "rule", "session": "att-1", "harness": "claude", "route": "rt-1",
                                  "attempt": "att-1"})
                self.assertEqual(P._history_by("human"), "agent")
            self.assertEqual(P._history_by("human"), "human")


class E2BeginWaitsForAdmissionTest(B1RefreshBase):
    """#89 waits for the admission lock by asking `begin` again; the §45 rules still hold on the way."""

    def setUp(self):
        super().setUp()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": state.name})
        env.start()
        self.addCleanup(env.stop)
        in_test = mock.patch.object(TRIG, "in_test_process", return_value=False)
        fake = mock.patch.object(TRIG, "subprocess")
        in_test.start()
        self.addCleanup(in_test.stop)
        self.popen = fake.start().Popen
        self.addCleanup(fake.stop)

    def test_e2_busy_then_free_is_one_begin_with_one_locked_read_and_no_double_writes(self):
        parent = self.closed("e2-parent", {"plans/cycle/plan.md": b"plan\n"})
        self.edit(parent, "plans/cycle/plan.md", b"plan, edited after the close\n")
        route, route_file = self.route(slug="e2-child", parent_cycle_id=parent["cycle_id"])
        real_acquire, calls = adm._acquire_lock, []

        def acquire(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise adm.AdmissionBusy("busy")
            return real_acquire(*args, **kwargs)

        locked_reads, real_load = [], adm.load_index

        def counting(root):
            if adm.holds_lock(Path(root)):
                locked_reads.append(1)
            return real_load(root)

        before = (self.tree_state(parent), len(self.history.made))
        with mock.patch.object(adm, "_acquire_lock", acquire), mock.patch.object(adm, "load_index", counting), \
                mock.patch.object(time, "sleep"):
            result = P._begin_waiting_for_admission(
                self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual((result["status"], result["layout"]), ("begun", "cycle"))
        self.assertGreaterEqual(len(calls), 2)  # the first try was busy, a later one took the lock
        self.assertEqual(len(locked_reads), 1, locked_reads)
        # The parent is looked at once, after the successful attempt and outside the lock; nothing is written twice.
        argvs = [" ".join(call.args[0]) for call in self.popen.call_args_list]
        self.assertEqual([a for a in argvs if parent["cycle_id"] in a and "checkpoint" in a].__len__(), 1, argvs)
        self.assertEqual(before[0], self.tree_state(parent))
        self.assertEqual(len(self.history.made), before[1])
        self.assertEqual(len(P.list_cycle_records(self.root)), 2)
        self.assertFalse(P.read_cycle_record(self.root, result["cycle_id"]).get("history_pending"))

    def test_e2_an_observation_that_fails_never_leaves_begin(self):
        parent = self.closed("e2-quiet", {"plans/cycle/plan.md": b"plan\n"})
        route, route_file = self.route(slug="e2-quiet-child", parent_cycle_id=parent["cycle_id"])
        with mock.patch.object(TRIG, "launch", side_effect=adm.AdmissionBusy("busy")):
            result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(result["layout"], "cycle")


class E3ReconcileRaceTest(C1ChangesBase):
    """A cycle carried between campaigns while the layout is being read is never taken for gone."""

    def two_campaigns(self):
        first = self.closed_with("e3-one", "e3-a", {"plans/cycle/a.md": b"a\n"}, activate=True)
        second = self.closed_with("e3-two", "e3-b", {"plans/cycle/b.md": b"b\n"})
        folders = {r["campaign_id"]: P.campaign_dir(self.root, r["campaign_id"]) for r in (first, second)}
        early, late = sorted(folders.values(), key=lambda path: path.name)
        by_folder = {folders[r["campaign_id"]]: r for r in (first, second)}
        return by_folder[early], by_folder[late], early, late

    def carry_during_scan(self, moved, early, late):
        """Run `reconcile_root` and, right after the scan reads `early`'s children, carry `moved` from `late` into it."""
        source = Path(moved["cycle_dir"])
        target = early / source.name
        real_listdir, state = os.listdir, {"done": False}

        def listdir(path="."):
            names = real_listdir(path)
            if not state["done"] and Path(str(path)) == early:
                state["done"] = True
                os.rename(str(source), str(target))
            return names

        with mock.patch.object(os, "listdir", listdir):
            out = P.reconcile_root(self.root)
        self.assertTrue(state["done"])
        return out, target

    def test_e3_a_cycle_carried_during_the_scan_is_not_tombstoned_and_the_next_look_moves_it(self):
        keep, moved, early, late = self.two_campaigns()
        out, target = self.carry_during_scan(moved, early, late)
        record = self.record(moved)
        self.assertNotIn("deleted_at", record, f"tombstoned although its folder is at {target}: {out}")
        self.assertEqual(out.get("cycle_gone", []), [])
        self.assertTrue(target.is_dir())
        self.assertEqual(self.index_row(moved)[0]["cycle_path"], P._cycle_rel(self.root, Path(moved["cycle_dir"])))
        # The look after finds the cycle where it now is.
        again = P.reconcile_root(self.root)
        self.assertEqual(again["status"], "reconciled", again)
        record = self.record(moved)
        self.assertEqual(record["campaign_id"], keep["campaign_id"])
        self.assertNotIn("deleted_at", record)
        self.assertEqual(self.index_row(moved)[0]["cycle_path"], P._cycle_rel(self.root, target))
        self.assertIn(moved["cycle_id"], P.read_campaign(self.root, keep["campaign_id"])["cycles"])

    def test_e3_an_incomplete_second_reading_takes_nothing_for_gone(self):
        keep, victim, early, late = self.two_campaigns()
        shutil.rmtree(str(Path(victim["cycle_dir"])))
        incomplete = P._scan_layout(self.root)
        incomplete.complete = False
        real_scan, calls = P._scan_layout, []

        def scan(root):
            calls.append(1)
            return real_scan(root) if len(calls) == 1 else incomplete

        with mock.patch.object(P, "_scan_layout", scan):
            out = P.reconcile_root(self.root)
        self.assertEqual(out["status"], "unchanged", out)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("deleted_at", self.record(victim))
        # With a complete second reading the removal is found as before.
        self.assertEqual(P.reconcile_root(self.root)["status"], "reconciled")
        self.assertIn("deleted_at", self.record(victim))

    def test_e3_a_member_seen_again_survives_the_removal_of_its_campaign(self):
        keep, victim, early, late = self.two_campaigns()
        # `victim`'s campaign folder is removed, but its cycle is carried into `keep`'s while the scan runs.
        source = Path(victim["cycle_dir"])
        target = early / source.name
        os.rename(str(source), str(target))
        shutil.rmtree(str(late))
        first = P._scan_layout(self.root)
        self.assertEqual(first.cycles[victim["cycle_id"]], target)  # the folders say where it is
        # A scan that missed the carried folder: the look-again must stop both from being tombstoned.
        stale = P._LayoutScan(dict(first.campaigns), {k: v for k, v in first.cycles.items()
                                                      if k != victim["cycle_id"]},
                              dict(first.folder_campaign), set(), True)
        published = adm.load_index(self.root) and P.artifact_locator._load_index(self.root)
        changes = P._hand_changes(self.root, stale, published)
        self.assertIn(victim["cycle_id"], changes["cycle_gone"])
        changes, seen = P._look_again_before_gone(self.root, changes)
        self.assertEqual(changes["cycle_gone"], [])
        self.assertIn(victim["cycle_id"], seen)
        self.assertEqual(changes["campaign_gone"], [victim["campaign_id"]])
        out = P._reconcile_locked_run(self.root, None, stale, published, changes, seen)
        self.assertEqual(out["status"], "reconciled")
        self.assertNotIn("deleted_at", self.record(victim))
        self.assertTrue(target.is_dir())


class E4ReviveTest(C1ChangesBase):
    """A cycle a reconcile took for gone comes back with its folder; an explicit delete never does."""

    def test_e4_a_reconcile_tombstone_is_revived_when_the_folder_returns(self):
        keep = self.closed_with("e4-keep", "e4-a", {"plans/cycle/k.md": b"k\n"}, activate=True)
        gone = self.closed_with("e4-gone", "e4-a", {"plans/cycle/g.md": b"g\n"})
        folder = Path(gone["cycle_dir"])
        copy = Path(self._tmp.name) / "e4-copy"
        shutil.copytree(str(folder), str(copy), symlinks=True)
        shutil.rmtree(str(folder))
        self.assertEqual(P.reconcile_root(self.root)["status"], "reconciled")
        record = self.record(gone)
        self.assertEqual((bool(record.get("deleted_at")), record.get("deleted_by")), (True, "reconcile"))
        self.assertNotIn(gone["cycle_id"], P.read_campaign(self.root, gone["campaign_id"])["cycles"])
        self.assertIsNone(self.index_row(gone)[0])
        # The folder is put back (a copy, a restore from a backup).
        shutil.copytree(str(copy), str(folder), symlinks=True)
        self.history.made.clear()
        out = P.reconcile_root(self.root)
        self.assertEqual(out["status"], "reconciled", out)
        record = self.record(gone)
        self.assertNotIn("deleted_at", record)
        self.assertNotIn("deleted_by", record)
        self.assertNotIn("history_pending", record)
        self.assertIn(gone["cycle_id"], P.read_campaign(self.root, gone["campaign_id"])["cycles"])
        cycle_row, manifest_row = self.index_row(gone)
        self.assertEqual(cycle_row["cycle_path"], P._cycle_rel(self.root, folder))
        self.assertEqual(manifest_row["manifest_digest"], m_digest(json.loads((folder / "manifest.json").read_text())))
        (line,) = self.history.made
        self.assertEqual((line["target_type"], line["field"], line["operation"], line["actor"]["by"]),
                         ("cycle", "path", "update", "rule"))
        self.assertEqual((line["before"], line["after"]),
                         ({"value": None}, {"value": P._cycle_rel(self.root, folder)}))
        self.assertEqual(P.reconcile_root(self.root)["status"], "unchanged")
        self.assertNotIn("deleted_at", self.record(keep))

    def test_e4_a_revived_cycle_follows_a_folder_that_came_back_somewhere_else(self):
        keep = self.closed_with("e4-home", "e4-home", {"plans/cycle/k.md": b"k\n"}, activate=True)
        other = self.closed_with("e4-other", "e4-other", {"plans/cycle/o.md": b"o\n"})
        gone = self.closed_with("e4-away", "e4-home", {"plans/cycle/g.md": b"g\n"})
        folder = Path(gone["cycle_dir"])
        copy = Path(self._tmp.name) / "e4-away-copy"
        shutil.copytree(str(folder), str(copy), symlinks=True)
        shutil.rmtree(str(folder))
        P.reconcile_root(self.root)
        self.assertIn("deleted_at", self.record(gone))
        destination = P.campaign_dir(self.root, other["campaign_id"]) / folder.name
        shutil.copytree(str(copy), str(destination), symlinks=True)
        self.assertEqual(P.reconcile_root(self.root)["status"], "reconciled")
        record = self.record(gone)
        self.assertEqual((record["campaign_id"], record["locator"]), (other["campaign_id"], destination.name))
        self.assertNotIn("deleted_at", record)
        self.assertIn(gone["cycle_id"], P.read_campaign(self.root, other["campaign_id"])["cycles"])
        self.assertNotIn(gone["cycle_id"], P.read_campaign(self.root, keep["campaign_id"])["cycles"])
        self.assertEqual(self.index_row(gone)[0]["campaign_id"], other["campaign_id"])
        self.assertEqual(self.index_row(gone)[0]["cycle_path"], P._cycle_rel(self.root, destination))
        self.assertEqual(json.loads((destination / ".cycle.json").read_text())["campaign_id"], other["campaign_id"])

    def test_e4_an_explicit_delete_stopped_before_the_folder_went_is_finished_not_revived(self):
        keep = self.closed_with("e4-del-keep", "e4-del", {"plans/cycle/k.md": b"k\n"}, activate=True)
        gone = self.closed_with("e4-del-gone", "e4-del", {"plans/cycle/g.md": b"g\n"})
        folder = Path(gone["cycle_dir"])
        with mock.patch.object(P, "_remove_folder", side_effect=RuntimeError("stopped here")):
            with self.assertRaises(RuntimeError):
                P.delete_cycle(self.root, gone["cycle_id"])
        record = self.record(gone)
        self.assertEqual((bool(record.get("deleted_at")), record.get("deleted_by")), (True, "delete"))
        self.assertTrue(folder.is_dir())
        out = P.reconcile_root(self.root)
        self.assertNotIn(gone["cycle_id"], out.get("cycle_moves", []))
        record = self.record(gone)
        self.assertTrue(record.get("deleted_at"))
        self.assertEqual(record.get("deleted_by"), "delete")
        # The same command again finishes it.
        self.assertEqual(P.delete_cycle(self.root, gone["cycle_id"])["status"], "deleted")
        self.assertFalse(folder.exists())
        self.assertEqual(P.delete_cycle(self.root, gone["cycle_id"])["status"], "already-deleted")
        self.assertNotIn("deleted_at", self.record(keep))


class E5ManifestReadOutsideLockTest(C1ChangesBase):
    """A reconcile reads (and hashes) the manifests it adopts before the admission lock, not under it."""

    two_campaigns = E3ReconcileRaceTest.two_campaigns

    def probe(self, documents):
        """Record, per call, whether the admission lock was held: every manifest read, and every digest of a document in `documents`."""
        seen = {"read": [], "digest": []}
        real_read, real_digest = P._read_manifest_raw, P.artifact_manifest.manifest_digest

        def read(directory):
            seen["read"].append(adm.holds_lock(self.root))
            return real_read(directory)

        def digest(document):
            if document in documents:
                seen["digest"].append(adm.holds_lock(self.root))
            return real_digest(document)

        patches = (mock.patch.object(P, "_read_manifest_raw", read),
                   mock.patch.object(P.artifact_manifest, "manifest_digest", digest))
        return seen, patches

    def run_probed(self, documents):
        seen, patches = self.probe(documents)
        with patches[0], patches[1]:
            out = P.reconcile_root(self.root)
        self.assertEqual(out["status"], "reconciled", out)
        self.assertTrue(seen["read"], "the manifest was never read")
        self.assertNotIn(True, seen["read"], "a manifest was read with the admission lock held")
        self.assertNotIn(True, seen["digest"], "the adopted manifest was hashed with the admission lock held")
        return out

    def removed_and_returned(self, slug, campaign_key, where=None):
        keep = self.closed_with(slug + "-keep", campaign_key, {"plans/cycle/k.md": b"k\n"}, activate=True)
        gone = self.closed_with(slug + "-gone", campaign_key, {"plans/cycle/g.md": b"g\n"})
        folder = Path(gone["cycle_dir"])
        copy = Path(self._tmp.name) / (slug + "-copy")
        shutil.copytree(str(folder), str(copy), symlinks=True)
        document = json.loads((folder / "manifest.json").read_text())
        shutil.rmtree(str(folder))
        self.assertEqual(P.reconcile_root(self.root)["status"], "reconciled")
        self.assertIn("deleted_at", self.record(gone))
        destination = (where or folder.parent) / folder.name
        shutil.copytree(str(copy), str(destination), symlinks=True)
        return keep, gone, destination, document

    def test_e5_a_revival_reads_and_hashes_the_manifest_before_the_lock(self):
        keep, gone, folder, document = self.removed_and_returned("e5-rev", "e5-rev")
        self.run_probed([document])
        self.assertNotIn("deleted_at", self.record(gone))
        self.assertEqual(self.index_row(gone)[1]["manifest_digest"], m_digest(document))
        self.assertEqual(P.reconcile_root(self.root)["status"], "unchanged")

    def test_e5_a_revival_in_another_campaign_does_not_hash_the_old_manifest_under_the_lock(self):
        home = self.closed_with("e5-home", "e5-home", {"plans/cycle/h.md": b"h\n"}, activate=True)
        other = self.closed_with("e5-other", "e5-other", {"plans/cycle/o.md": b"o\n"})
        keep, gone, destination, document = self.removed_and_returned(
            "e5-away", "e5-home", where=P.campaign_dir(self.root, other["campaign_id"]))
        self.run_probed([document])
        record = self.record(gone)
        self.assertEqual(record["campaign_id"], other["campaign_id"])
        self.assertNotIn("deleted_at", record)
        self.assertEqual(self.index_row(gone)[0]["cycle_path"], P._cycle_rel(self.root, destination))

    def test_e5_a_hand_move_reads_and_hashes_the_manifest_before_the_lock(self):
        keep, moved, early, late = self.two_campaigns()
        source = Path(moved["cycle_dir"])
        document = json.loads((source / "manifest.json").read_text())
        target = early / source.name
        os.rename(str(source), str(target))
        out = self.run_probed([document])
        self.assertEqual(out["cycle_moves"], [moved["cycle_id"]])
        record = self.record(moved)
        self.assertEqual(record["campaign_id"], keep["campaign_id"])
        self.assertEqual(self.index_row(moved)[0]["cycle_path"], P._cycle_rel(self.root, target))
        self.assertEqual(len(self.lines(field="campaign", target_id=moved["cycle_id"])), 1)

    def change_after_preread(self, folder):
        """Patch `_preread_manifests` so the manifest at `folder` is touched (same bytes, new mtime) once it was read."""
        real, state = P._preread_manifests, {"calls": 0}

        def preread(root, changes):
            out = real(root, changes)
            state["calls"] += 1
            if state["calls"] == 1:
                path = Path(folder) / "manifest.json"
                seen = path.stat()
                os.utime(str(path), ns=(seen.st_atime_ns, seen.st_mtime_ns + 1_000_000_000))
            return out

        return mock.patch.object(P, "_preread_manifests", preread), state

    def test_e5_a_manifest_that_changes_after_the_read_is_adopted_by_the_next_look(self):
        keep, moved, early, late = self.two_campaigns()
        source = Path(moved["cycle_dir"])
        target = early / source.name
        os.rename(str(source), str(target))
        patch, state = self.change_after_preread(target)
        adopted = []
        real_adopt = P._adopt_location_locked

        def adopt(*args, **kwargs):
            adopted.append(1)
            return real_adopt(*args, **kwargs)

        with patch, mock.patch.object(P, "_adopt_location_locked", adopt):
            out = P.reconcile_root(self.root)
        self.assertEqual(out["status"], "reconciled", out)
        self.assertEqual(state["calls"], 2)  # the first run saw the change and left it to the second look
        self.assertEqual(len(adopted), 1)
        record = self.record(moved)
        self.assertEqual(record["campaign_id"], keep["campaign_id"])
        self.assertEqual(self.index_row(moved)[0]["cycle_path"], P._cycle_rel(self.root, target))
        self.assertEqual(self.index_row(moved)[1]["manifest_digest"],
                         m_digest(json.loads((target / "manifest.json").read_text())))
        self.assertEqual(len(self.lines(field="campaign", target_id=moved["cycle_id"])), 1)
        self.assertNotIn("history_pending", record)
        self.assertEqual(P.reconcile_root(self.root)["status"], "unchanged")

    def test_e5_a_manifest_that_changes_after_the_read_is_revived_by_the_next_look(self):
        keep, gone, folder, document = self.removed_and_returned("e5-stale", "e5-stale")
        patch, state = self.change_after_preread(folder)
        with patch:
            out = P.reconcile_root(self.root)
        self.assertEqual(out["status"], "reconciled", out)
        self.assertEqual(state["calls"], 2)
        record = self.record(gone)
        self.assertNotIn("deleted_at", record)
        self.assertEqual(self.index_row(gone)[1]["manifest_digest"], m_digest(document))
        self.assertEqual(len(self.lines(field="path", target_id=gone["cycle_id"])), 1)
        self.assertEqual(P.reconcile_root(self.root)["status"], "unchanged")


def m_digest(document):
    return M.manifest_digest(document)


if __name__ == "__main__":
    unittest.main()
