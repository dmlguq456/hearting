#!/usr/bin/env python3
import ast
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from dispatch_degradation import record_degradation
import dispatch_stage_advance as SA

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/tools"
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

UTILITIES = Path(__file__).resolve().parent
ROOT = UTILITIES.parent


class DegradationWriterTest(unittest.TestCase):
    def setUp(self):
        # record_degradation() resolves the dispatch state root ahead of
        # agent_home/.dispatch, preferring an inherited AGENT_DISPATCH_JOBS --
        # clear it so these agent-home-relative fixtures aren't sensitive to
        # the invoking shell's real registry.
        self._prior_jobs = os.environ.pop("AGENT_DISPATCH_JOBS", None)
        self.addCleanup(self._restore_jobs)

    def _restore_jobs(self):
        if self._prior_jobs is not None:
            os.environ["AGENT_DISPATCH_JOBS"] = self._prior_jobs

    def test_depth_zero_is_not_written(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(record_degradation(agent_home=home, dispatch_depth=0,
                                                  writer="stage-dispatch-fallback.py"))
            self.assertFalse(os.path.exists(os.path.join(home, ".dispatch")))

    def test_schema_bounds_and_route_shard(self):
        with tempfile.TemporaryDirectory() as home:
            jobs = os.path.join(home, "dispatch", "jobs.log")
            path = record_degradation(agent_home=home, route_id="rt-test", route_node="exec",
                                      route_hash="sha256:test", dispatch_depth=1,
                                      fallback_hop="inline", execution_surface="inline",
                                      writer="stage-dispatch-fallback.py",
                                      reason="r" * 200, detail="d" * 600,
                                      attempt_trace="t" * 3000, jobs=jobs)
            with open(path, encoding="utf-8") as stream:
                row = json.loads(stream.readline())
            self.assertEqual(set(("schema_version", "kind", "ts", "route_id", "route_node",
                                  "route_hash", "dispatch_depth", "fallback_hop",
                                  "execution_surface", "writer")) - set(row), set())
            self.assertLessEqual(len(row["reason"]), 160)
            self.assertLessEqual(len(row["detail"]), 512)
            self.assertLessEqual(len(row["attempt_trace"]), 2048)
            self.assertTrue(row["event_id"].startswith("dg-"))
            self.assertTrue(row["reason"].endswith("…"))
            self.assertTrue(row["detail"].endswith("…"))
            self.assertTrue(row["attempt_trace"].endswith("…"))

    def test_failures_are_fail_open_and_return_none(self):
        # B47-8: SD-93d (1)-(4) re-run for W5 (`dispatch_completion_join.py`)
        # alongside the pre-existing W1 writer -- fail-open behavior must
        # not depend on which registered writer triggered the failure.
        cases = ("readonly", "serialization", "lock")
        writers = ("stage-dispatch-fallback.py", "dispatch_completion_join.py")
        for writer in writers:
            for case in cases:
                with self.subTest(writer=writer, case=case), tempfile.TemporaryDirectory() as home:
                    jobs = os.path.join(home, "dispatch", "jobs.log")
                    if case == "readonly":
                        with open(os.path.join(home, "dispatch"), "w", encoding="utf-8"):
                            pass
                        result = record_degradation(agent_home=home, route_id="rt-ro",
                                                    route_node="execute", route_hash="h",
                                                    dispatch_depth=2, fallback_hop="inline",
                                                    execution_surface="inline",
                                                    writer=writer, jobs=jobs)
                    elif case == "serialization":
                        result = record_degradation(agent_home=home, route_id="rt-json",
                                                    route_node="execute", route_hash="h",
                                                    dispatch_depth=2, fallback_hop="inline",
                                                    execution_surface="inline",
                                                    writer=writer,
                                                    last_direct_failure=object(), jobs=jobs)
                    else:
                        root = os.path.join(home, "dispatch", "degradations")
                        os.makedirs(root)
                        lock_path = os.path.join(root, "rt-lock.jsonl.lock")
                        holder = subprocess.Popen([sys.executable, "-c", (
                            "import fcntl,time,sys; f=open(sys.argv[1],'a+'); "
                            "fcntl.flock(f,fcntl.LOCK_EX); time.sleep(.5)"), lock_path])
                        try:
                            time.sleep(.05)
                            result = record_degradation(agent_home=home, route_id="rt-lock",
                                                        route_node="execute", route_hash="h",
                                                        dispatch_depth=2, fallback_hop="inline",
                                                        execution_surface="inline",
                                                        writer=writer, jobs=jobs)
                        finally:
                            holder.wait(timeout=2)
                    self.assertIsNone(result)

    def test_b47_8_w5_write_failure_is_harmless(self):
        # SD-93d (1): a write failure for the W5 writer is swallowed --
        # the caller (`_close_invalid_envelope_child`) never sees an
        # exception and the invalid-envelope close itself is unaffected.
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, "dispatch"), "w", encoding="utf-8"):
                pass
            jobs = os.path.join(home, "dispatch", "jobs.log")
            result = record_degradation(agent_home=home, route_id="rt-w5-ro",
                                        route_node="execute", route_hash="h",
                                        dispatch_depth=2, fallback_hop=None,
                                        execution_surface=None,
                                        writer="dispatch_completion_join.py",
                                        reason="invalid-envelope", jobs=jobs)
            self.assertIsNone(result)

    def test_invalid_required_record_is_skipped_by_consumer(self):
        from fleet.collectors import dispatch
        with tempfile.TemporaryDirectory() as home:
            root = os.path.join(home, ".dispatch", "degradations")
            os.makedirs(root)
            valid = {"schema_version": 1, "kind": "degradation", "ts": 1,
                     "route_id": "rt-valid", "route_node": "execute", "route_hash": "h",
                     "dispatch_depth": 2, "fallback_hop": "inline",
                     "execution_surface": "inline", "writer": "stage-dispatch-fallback.py"}
            invalid = dict(valid)
            del invalid["route_hash"]
            with open(os.path.join(root, "rt-valid.jsonl"), "w", encoding="utf-8") as stream:
                stream.write(json.dumps(invalid) + "\n")
                stream.write(json.dumps(valid) + "\n")
            rows = dispatch._scan_degradations(["rt-valid"], agent_home=home)
            self.assertEqual(len(rows["rt-valid"]), 1)
            self.assertEqual(rows["rt-valid"][0]["route_hash"], "h")

    def test_subprocess_append_over_4096_bytes_has_no_torn_lines(self):
        with tempfile.TemporaryDirectory() as home:
            jobs = os.path.join(home, "dispatch", "jobs.log")
            code = """import sys
from dispatch_degradation import record_degradation
record_degradation(agent_home=sys.argv[1], route_id='rt-race', route_node='execute', route_hash='h', dispatch_depth=2, fallback_hop='inline', execution_surface='inline', writer='stage-dispatch-fallback.py', detail='x'*5000, attempt_id=sys.argv[2], jobs=sys.argv[3])
"""
            processes = [subprocess.Popen([sys.executable, "-c", code, home, str(i), jobs],
                                          cwd=os.path.dirname(__file__),
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                         for i in range(12)]
            for process in processes:
                self.assertEqual(process.wait(timeout=5), 0)
            path = os.path.join(home, "dispatch", "degradations", "rt-race.jsonl")
            with open(path, "rb") as stream:
                lines = stream.readlines()
            self.assertEqual(len(lines), 12)
            for line in lines:
                self.assertTrue(line.endswith(b"\n"))
                self.assertEqual(json.loads(line)["route_id"], "rt-race")

    def test_new_optional_fields_survive_into_ledger_content(self):
        # R6/D7: every new typed-reason field added in v43 must be carried into
        # the ledger row verbatim, not silently dropped by the closed allowlist.
        with tempfile.TemporaryDirectory() as home:
            jobs = os.path.join(home, "dispatch", "jobs.log")
            path = record_degradation(agent_home=home, route_id="rt-v43", route_node="execute",
                                      route_hash="sha256:v43", dispatch_depth=2,
                                      fallback_hop="cross-harness-headless",
                                      execution_surface="registered-headless",
                                      writer="capability-route.py",
                                      reason="parent-cross-same-harness",
                                      leg_class="peer", auxiliary_check="-",
                                      parent_cross="degraded", cause="affinity-pinned",
                                      sole_gate="degraded",
                                      subsession_id="ss-v43-w1c-claude",
                                      slice_manifest_sha256="sha256:slice", jobs=jobs)
            self.assertIsNotNone(path)
            with open(path, encoding="utf-8") as stream:
                row = json.loads(stream.readline())
            self.assertEqual(row["writer"], "capability-route.py")
            self.assertEqual(row["leg_class"], "peer")
            self.assertEqual(row["auxiliary_check"], "-")
            self.assertEqual(row["parent_cross"], "degraded")
            self.assertEqual(row["cause"], "affinity-pinned")
            self.assertEqual(row["sole_gate"], "degraded")
            self.assertEqual(row["subsession_id"], "ss-v43-w1c-claude")
            self.assertEqual(row["slice_manifest_sha256"], "sha256:slice")
            self.assertEqual(row["reason"], "parent-cross-same-harness")

    def test_capability_route_writer_is_authorized(self):
        # D7: without capability-route.py on the writer allowlist the SD-103
        # owner-side record would silently produce no row at all.
        with tempfile.TemporaryDirectory() as home:
            jobs = os.path.join(home, "dispatch", "jobs.log")
            path = record_degradation(agent_home=home, route_id="rt-owner", route_node="execute",
                                      route_hash="sha256:owner", dispatch_depth=2,
                                      fallback_hop=None, execution_surface="registered-headless",
                                      writer="capability-route.py",
                                      reason="subdivision-scope-violation", jobs=jobs)
            self.assertIsNotNone(path)
            with open(path, encoding="utf-8") as stream:
                row = json.loads(stream.readline())
            self.assertEqual(row["writer"], "capability-route.py")
            self.assertEqual(row["reason"], "subdivision-scope-violation")

    def test_writer_allowlist_preserves_legacy_and_adds_reaper(self):
        with tempfile.TemporaryDirectory() as home:
            jobs = os.path.join(home, "dispatch", "jobs.log")
            writers = (
                "stage-dispatch-fallback.py",
                "dispatch-batch.py",
                "capability-route.py",
                "dispatch-reap-watch.py",
                "dispatch_completion_join.py",
            )
            for index, writer in enumerate(writers):
                with self.subTest(writer=writer):
                    route_id = f"rt-writer-{index}"
                    path = record_degradation(
                        agent_home=home,
                        route_id=route_id,
                        route_node="execute",
                        route_hash="sha256:writer",
                        dispatch_depth=2,
                        fallback_hop="same-harness-headless",
                        execution_surface="registered-headless",
                        writer=writer,
                        jobs=jobs,
                    )
                    self.assertIsNotNone(path)
                    with open(path, encoding="utf-8") as stream:
                        self.assertEqual(json.loads(stream.readline())["writer"], writer)

            refused = record_degradation(
                agent_home=home,
                route_id="rt-unknown-writer",
                route_node="execute",
                route_hash="sha256:writer",
                dispatch_depth=2,
                fallback_hop="same-harness-headless",
                execution_surface="registered-headless",
                writer="unknown-writer.py",
                jobs=jobs,
            )
            self.assertIsNone(refused)
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        home, "dispatch", "degradations", "rt-unknown-writer.jsonl"
                    )
                )
            )
            unattributed = os.path.join(home, "dispatch", "degradations", "_unattributed.jsonl")
            self.assertTrue(os.path.exists(unattributed))
            with open(unattributed, encoding="utf-8") as stream:
                rows = [json.loads(line) for line in stream if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "writer-unregistered")
            self.assertEqual(rows[0]["writer"], "unknown-writer.py")

    def test_b47_4_no_writer_cardinality_assertion_anywhere(self):
        # §4 ②-A: the writer allowlist must be sealed by enumeration, never
        # by cardinality -- no reader may assert `len(_WRITERS)` or a
        # "writer set has N members" claim anywhere in the repo.
        scan_dirs = (
            "utilities", "tools", "adapters", "hooks", "core",
            "capabilities", "roles", "skills", "loops",
        )
        exclude_dirs = {
            ".git", ".agent_reports", ".claude_reports", "__pycache__",
            "node_modules",
        }
        exclude_prefixes = (
            str(ROOT / "adapters" / "claude" / "skills"),
            str(ROOT / "adapters" / "claude" / "plugin-marketplace"),
        )
        self_path = str(Path(__file__).resolve())
        cardinality_patterns = [
            re.compile(r"len\(\s*_WRITERS\s*\)"),
            re.compile(r"_WRITERS\s*\)?\s*(==|!=)\s*4\b"),
            re.compile(r"writer\s*집합(은|는)?\s*4"),
            re.compile(r"4개의\s*writer"),
            re.compile(r"W1\s*~\s*W4\b"),
        ]
        violations = []
        for scan_dir in scan_dirs:
            base = ROOT / scan_dir
            if not base.is_dir():
                continue
            for pattern in ("*.py", "*.md", "*.tsv"):
                for path in base.rglob(pattern):
                    resolved = str(path.resolve())
                    if resolved == self_path:
                        continue
                    if any(part in exclude_dirs for part in path.parts):
                        continue
                    if any(resolved.startswith(prefix) for prefix in exclude_prefixes):
                        continue
                    try:
                        text = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    for lineno, line in enumerate(text.splitlines(), start=1):
                        for cardinality_pattern in cardinality_patterns:
                            if cardinality_pattern.search(line):
                                violations.append(f"{path}:{lineno}: {line.strip()}")
        self.assertEqual(violations, [], "writer-set cardinality assertion found:\n" + "\n".join(violations))

        import dispatch_degradation as DEG
        self.assertIn("dispatch_completion_join.py", DEG._WRITERS)
        self.assertIn("writer-unregistered", DEG._KINDS)

        with tempfile.TemporaryDirectory() as home:
            jobs = os.path.join(home, "dispatch", "jobs.log")
            refused = record_degradation(
                agent_home=home, route_id="rt-b47-4", route_node="execute",
                route_hash="h", dispatch_depth=2, writer="does-not-exist.py", jobs=jobs,
            )
            self.assertIsNone(refused)
            unattributed = os.path.join(home, "dispatch", "degradations", "_unattributed.jsonl")
            with open(unattributed, encoding="utf-8") as stream:
                rows = [json.loads(line) for line in stream if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "writer-unregistered")
            normal_rows = [
                p for p in Path(home, "dispatch", "degradations").glob("*.jsonl")
                if p.name != "_unattributed.jsonl"
            ]
            self.assertEqual(normal_rows, [])


class SD110LedgerIsolationTest(unittest.TestCase):
    """C-21/C-22 (plan.md §6): SD-110 leaves SD-93's sealed ledger contract
    untouched, and a normal runtime stage advance produces zero ledger
    events -- 13.32.1-(5) says the degradation ledger is unrelated
    observation surface, SD-109's "no ledger row for normal completion"
    text extends unchanged to SD-110's own "normal advance"."""

    def test_row_composition_and_writer_allowlist_are_the_pre_sd110_frozen_sets(self):
        import dispatch_degradation as DEG

        self.assertEqual(
            DEG._KINDS,
            {"degradation", "chain-exhausted", "leg-failure", "writer-unregistered"},
        )
        self.assertEqual(
            DEG._WRITERS,
            {
                "stage-dispatch-fallback.py",
                "dispatch-batch.py",
                "capability-route.py",
                "dispatch-reap-watch.py",
                "dispatch_completion_join.py",
            },
        )
        # Neither `dispatch_stage_advance.py` nor either session supervisor is
        # on the writer allowlist -- SD-110 has no ledger-writer identity.
        self.assertNotIn("dispatch_stage_advance.py", DEG._WRITERS)
        self.assertNotIn("claude-session-supervisor.py", DEG._WRITERS)
        self.assertNotIn("codex-app-server-supervisor.py", DEG._WRITERS)

    def test_event_id_dedup_key_and_required_fields_are_unchanged(self):
        import dispatch_degradation as DEG
        from fleet.collectors import dispatch as FLEET_DISPATCH

        self.assertEqual(
            FLEET_DISPATCH._DEGRADATION_REQUIRED,
            {
                "schema_version", "kind", "ts", "route_id", "route_node",
                "route_hash", "dispatch_depth", "fallback_hop",
                "execution_surface", "writer",
            },
        )
        # `_event_id` dedup identity is a fixed field tuple hashed to a
        # stable `dg-` prefixed digest -- same row, same event id, always.
        row = {
            "kind": "degradation", "route_id": "rt-dedup", "route_node": "execute",
            "attempt_id": "att-1", "parallel_leg_index": None,
            "parallel_leg_count": None, "fallback_ordinal": None,
            "writer": "stage-dispatch-fallback.py",
        }
        self.assertEqual(DEG._event_id(row), DEG._event_id(dict(row)))
        self.assertTrue(DEG._event_id(row).startswith("dg-"))

    def test_stage_advance_modules_never_reference_the_degradation_ledger(self):
        for relative in (
            "utilities/dispatch_stage_advance.py",
            "utilities/claude-session-supervisor.py",
            "utilities/codex-app-server-supervisor.py",
        ):
            path = ROOT / relative
            source = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "dispatch_degradation", source,
                f"{relative} must not reference the SD-93 ledger module",
            )

    def _fixture_route(self):
        nodes = [
            {
                "id": "a", "depends_on": [], "terminal": False,
                "advance_class": "runtime-eligible", "commit_expected": False,
                "unit": "dev/backend", "completion_gate": "gate",
                "write_scope": ["out/**"], "inputs": ["in"], "outputs": ["out"],
                "continuation": {"kind": "inline-next"},
            },
            {
                "id": "b", "depends_on": ["a"], "terminal": False,
                "advance_class": "runtime-eligible", "commit_expected": False,
                "unit": "dev/backend", "completion_gate": "gate",
                "write_scope": ["out/**"], "inputs": ["in"], "outputs": ["out"],
                "continuation": {"kind": "inline-next"},
            },
        ]
        payload = {
            "capability": "fixture-cap", "capability_mode": "dev",
            "effective_intensity": "standard", "nodes": nodes,
            "advance_generation": 0,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        payload["route_hash"] = digest
        payload["route_id"] = "rt-" + digest.split(":", 1)[1][:16]
        return payload

    def test_normal_advance_writes_no_ledger_file_and_never_calls_the_writer(self):
        route = self._fixture_route()

        class FakeServices:
            def close_gate(self, request, *, node, terminal_attempt_id, artifact):
                return {"closed": True, "node": node, "artifact": artifact}

            def claim(self, request, *, stage_advance_id, claim_key, successor_node):
                return SA.StageAdvanceClaim(
                    stage_advance_id=stage_advance_id, claim_key=claim_key,
                    successor_attempt_id="att-b-0001", replayed=False,
                )

            def start_successor(self, request, *, claim, successor, slug, prompt_file):
                assert prompt_file.is_file()
                return {"registered": True, "started": True, "child_spawned": True, "slug": slug}

            def observe_record(self, request, *, stage_advance_id):
                return None

            def process_quiescence(self, request, *, attempt_id):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.registry"
            jobs.touch()
            route_file = root / "route.json"
            route_file.write_text(json.dumps(route), encoding="utf-8")
            request = SA.StageAdvanceRequest(
                jobs=jobs, route_file=route_file, predecessor_node="a",
                predecessor_terminal_attempt_id="att-a-0001",
                parent_attempt_id="att-parent-0001", supervisor_phase="parked",
                delivered_open_attempt_ids=frozenset(), receipt_schema_negotiated=3,
                harness="claude", worktree=str(root),
            )
            marker_dir = root / "completion" / route["route_id"]
            marker_dir.mkdir(parents=True)
            (marker_dir / "a.json").write_text(json.dumps({
                "schema_version": 2, "route_id": route["route_id"],
                "route_hash": route["route_hash"], "node_id": "a",
                "attempt_id": "att-a-0001",
                "evidence": {"path": "/tmp/fixture-artifact.md", "state": "verified"},
            }), encoding="utf-8")
            with mock.patch.object(
                SA.ROUTE, "completion_dir", return_value=marker_dir
            ), mock.patch.object(
                SA, "gate_evidence", return_value=("verdict: PASS", None)
            ), mock.patch("dispatch_degradation.record_degradation") as writer:
                result = SA.coordinate_stage_advance(request, FakeServices())
            self.assertEqual(result.outcome, "advanced")
            writer.assert_not_called()
            self.assertFalse((root / ".dispatch" / "degradations").exists())


if __name__ == "__main__":
    unittest.main()
