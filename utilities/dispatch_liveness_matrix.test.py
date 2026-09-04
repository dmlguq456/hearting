#!/usr/bin/env python3
"""Boundary/property matrix for the dispatch liveness lifecycle seam.

One harness that walks the product space where liveness bugs have historically
hidden — {lifecycle} x {pid-scope} x {timeout-sentinel} — instead of patching one
cell at a time. It targets the two coverage GAPS that let a cross-harness
``claude -p`` execute worker be abandoned ~10s after launch while its attempt row
stayed ``open`` (later surfaced as Fleet ORPHANED):

  1. the outer-subprocess deadline seam, where ``foreground_timeout <= 0`` (the
     wrapper's "wait indefinitely" sentinel) collapsed to the shortest wall;
  2. the foreground-scoped x namespace-local *executed* launch cell, which existing
     suites never drive (they force it to detached via a host-like evidence
     override, which no longer reaches detached from a transient scope).

The other axis-cells (host/namespace detection, clean-exit, marker, heartbeat,
no-progress, capacity, orphan) are already covered by dispatch_lifecycle.test.py,
dispatch_registry.test.py, stage_dispatch_*.test.py and the fleet suite; this file
does not duplicate them — it fills the untested boundary column.
"""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


def _load(mod_name, filename):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Load the lifecycle helper first so the fallback module's
# `from dispatch_lifecycle import ...` binds to the same object.
L = _load("dispatch_lifecycle", "dispatch_lifecycle.py")
F = _load("stage_dispatch_fallback", "stage-dispatch-fallback.py")

FG = L.FOREGROUND_SCOPED
DETACHED = L.DETACHED
DIRECT_TIMEOUT = 45.0  # argparse default for --direct-timeout

# The timeout-sentinel axis. `<= 0` is wait_foreground's documented "wait
# indefinitely" sentinel; the positive values are ordinary bounds; 3600 is the
# argparse default. Historically only positive values >= 1 were ever tested.
SENTINELS = [-1.0, 0.0, 1.0, 45.0, 3600.0]


class OuterDeadlineBoundaryTest(unittest.TestCase):
    """The seam that turned foreground_timeout=0 (infinite) into a 10s wall."""

    def deadline(self, ft, lifecycle):
        return F.outer_subprocess_timeout(ft, lifecycle, DIRECT_TIMEOUT)

    def test_detached_ignores_foreground_timeout(self):
        # Detached wrappers return immediately; the foreground bound is irrelevant,
        # so no sentinel value may perturb the detached spawn-confirm window.
        for ft in SENTINELS:
            self.assertEqual(self.deadline(ft, DETACHED), DIRECT_TIMEOUT, msg=f"ft={ft}")

    def test_infinite_sentinel_is_not_the_shortest_wall(self):
        # CORE REGRESSION. foreground_timeout <= 0 means "wait indefinitely" inside
        # the wrapper (wait_foreground -> proc.wait(timeout=None)). The outer
        # deadline must therefore be at least as patient as any FINITE bound — never
        # collapse to the shortest possible wall. Pre-fix: 0 -> 10s < 3610s, which
        # abandoned a just-launched cross-harness claude worker.
        finite_max = self.deadline(3600.0, FG)
        for ft in (0.0, -1.0):
            d = self.deadline(ft, FG)
            self.assertTrue(
                d is None or d >= finite_max,
                msg=(f"sentinel foreground_timeout={ft} collapsed the outer deadline "
                     f"to {d}, shorter than the finite-3600 deadline {finite_max}; a "
                     f"child that opted into 'wait indefinitely' is abandoned early."),
            )

    def test_finite_foreground_deadline_is_monotonic_and_bounded(self):
        # Ordinary positive bounds must stay finite and non-decreasing in the
        # requested timeout (the grace-margin contract), so real launches with an
        # explicit bound are never regressed by the sentinel fix.
        finite = [ft for ft in SENTINELS if ft > 0]
        deadlines = [self.deadline(ft, FG) for ft in finite]
        for ft, d in zip(finite, deadlines):
            self.assertIsNotNone(d, msg=f"finite ft={ft} must stay bounded")
            self.assertGreaterEqual(d, ft, msg=f"ft={ft}: outer deadline below inner bound")
        self.assertEqual(deadlines, sorted(deadlines), msg="non-monotonic finite deadlines")

    def test_foreground_deadline_is_always_finite(self):
        # "never wait indefinitely" must survive inf/nan/huge too, not just <=0.
        # (Cross-model review found bounded_foreground_timeout let +inf through.)
        for ft in (float("inf"), float("nan"), 1e18, 0.0, -1.0, 5.0, 3600.0):
            d = self.deadline(ft, FG)
            self.assertIsNotNone(d, msg=f"ft={ft}")
            self.assertTrue(math.isfinite(d), msg=f"ft={ft} -> non-finite outer deadline {d}")


class NamespaceLocalForegroundCellTest(unittest.TestCase):
    """foreground-scoped x namespace-local — the executed cell today's bug lived in.

    Existing coverage only drives the OVERRIDE path
    (AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN=1 with host-like evidence -> detached).
    Without the override a namespace-local child selects the foreground-scoped
    path, so wait_foreground runs against it — exactly the pid=29 case that was
    abandoned. Guard that this cell is real and the wait path actually reaps its
    child.
    """

    def test_namespace_local_selects_foreground_scoped(self):
        self.assertEqual(L.select_launch_lifecycle({}, namespace_scoped=True), FG)

    def test_wait_foreground_reaps_its_child(self):
        # A foreground-scoped launch must wait on (and reap) its child rather than
        # abandon it. Drive the real wait path with a fast child.
        child = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
        outcome = L.wait_foreground(child, 5)
        self.assertEqual(outcome, L.ForegroundResult(0, ""))


class WrapperInternalWaitBoundTest(unittest.TestCase):
    """The wrapper's own foreground wait must also refuse to wait indefinitely.

    If only the outer deadline were bounded, a sentinel-0 child would run until the
    outer subprocess.run killed the whole wrapper — losing the wrapper's own
    SIGTERM->SIGKILL cleanup and clean terminal row. Clamping inside wait_foreground
    keeps the wrapper self-bounding.
    """

    def test_clamp_bounds_all_pathological_inputs(self):
        # <=0 and non-finite (inf/nan) -> default; finite-above-ceiling -> ceiling;
        # ordinary finite -> unchanged. Every result is finite and positive.
        self.assertEqual(L.bounded_foreground_timeout(0.0), L.FOREGROUND_TIMEOUT_DEFAULT)
        self.assertEqual(L.bounded_foreground_timeout(-5.0), L.FOREGROUND_TIMEOUT_DEFAULT)
        self.assertEqual(L.bounded_foreground_timeout(float("inf")), L.FOREGROUND_TIMEOUT_DEFAULT)
        self.assertEqual(L.bounded_foreground_timeout(float("nan")), L.FOREGROUND_TIMEOUT_DEFAULT)
        self.assertEqual(L.bounded_foreground_timeout(1e18), L.FOREGROUND_TIMEOUT_MAX)
        self.assertEqual(L.bounded_foreground_timeout(120.0), 120.0)
        for v in (0.0, -5.0, float("inf"), float("nan"), 1e18, 120.0):
            b = L.bounded_foreground_timeout(v)
            self.assertTrue(math.isfinite(b) and b > 0, msg=f"v={v} -> {b}")

    def test_wait_foreground_never_waits_indefinitely_on_sentinel(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        with mock.patch.object(L, "bounded_foreground_timeout", return_value=0.05) as bounded:
            outcome = L.wait_foreground(proc, 0, poll_interval=0.01)
        bounded.assert_called_once_with(0)
        self.assertEqual(outcome.failure, "timeout")
        self.assertTrue(outcome.group_empty)
        self.assertIsNotNone(proc.poll())


class HelperWiringTest(unittest.TestCase):
    """Guard the real wrapper-launch call sites keep routing through the helper.

    Cross-model review noted the unit tests would still pass if a call site were
    reverted to the inline ``foreground_timeout + 10`` while the helper stayed
    intact. This source-level guard fails on exactly that regression.
    """

    def test_no_inline_prefix_formula_and_both_sites_wired(self):
        src = Path(__file__).with_name("stage-dispatch-fallback.py").read_text(encoding="utf-8")
        self.assertNotIn("foreground_timeout + 10", src, "pre-fix inline deadline formula reintroduced")
        self.assertNotIn("foreground_timeout+10", src)
        # one def + two wrapper-launch call sites
        self.assertGreaterEqual(
            src.count("outer_subprocess_timeout("), 3,
            "a wrapper-launch subprocess.run stopped routing its timeout through the helper",
        )


class FallbackRuntimeWiringTest(unittest.TestCase):
    """End-to-end: drive the real capacity_retry fallback and prove the value it
    hands subprocess.run is the CLAMPED deadline, not the 10s abandonment wall.

    HelperWiringTest guards the call site textually; this proves the *runtime*
    behavior a reverted or mis-clamped site would break (cross-model review 4b):
    a foreground sentinel 0 must arrive at subprocess.run as ~3610s, never 10s.
    """

    def test_foreground_sentinel_reaches_subprocess_run_as_clamped_deadline(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(
                "2026-07-16T00:00:00Z\tdone\t/r\t/w\ts\t"
                "route_id=r,route_node=test,attempt_id=att-initial0001,model=gpt-5.6-sol,"
                "parent_harness=codex,parent_transport=headless,parent_sandbox=workspace-write,"
                "child_harness=codex,launch_authority=conductor,note=dead-capacity\n",
                encoding="utf-8",
            )
            args = type("Args", (), {
                "slug": "s", "parent": "p", "jobs": jobs,
                "capacity_model": "gpt-5.6-luna", "capacity_reasoning": "medium",
                "capacity_effort": None, "capacity_variant": None, "direct_timeout": 2,
                "action": "register", "progress_window_seconds": 0, "watchdog_max_windows": 2,
                "launch_lifecycle": FG, "foreground_timeout": 0.0,  # the sentinel that caused the bug
            })()
            route = {"route_id": "r", "route_hash": "sha256:x"}
            node = {"id": "test"}
            row = {"child_harness": "codex", "parent_harness": "codex",
                   "parent_transport": "headless", "parent_sandbox": "workspace-write",
                   "launch_authority": "conductor"}
            failed = {"attempt_id": "att-initial0001", "model": "gpt-5.6-sol"}
            captured = {}

            def capture(*_a, **kw):
                captured["timeout"] = kw.get("timeout")
                return subprocess.CompletedProcess(
                    [], 0,
                    stdout="check=ok\nmodel=gpt-5.6-luna\nearly_death=-\nattempt_id=att-x\nduplicate_attempt=0\n",
                    stderr="",
                )

            with mock.patch.object(F, "allowed_capacity_settings", return_value=True), \
                 mock.patch.object(F, "wrapper_command", return_value=["fake"]), \
                 mock.patch.object(F.subprocess, "run", side_effect=capture):
                F.capacity_retry(args, route, node, row, 1, failed, [])

            expected = L.bounded_foreground_timeout(0.0) + 10.0  # 3610.0
            self.assertEqual(captured.get("timeout"), expected)
            self.assertNotEqual(captured.get("timeout"), 10.0, "sentinel 0 still collapses to the 10s wall")
            self.assertTrue(math.isfinite(captured["timeout"]))


R = _load("dispatch_registry", "dispatch-registry.py")
J = _load("dispatch_completion_join", "dispatch_completion_join.py")


def _dead_pid():
    pid = 4190000
    while Path(f"/proc/{pid}").exists():
        pid += 1
    return pid


_META_AXES = {
    "attempt_schema_version": "2",
    "dispatch_depth": "2",
    "transport": "headless",
    "execution_surface": "registered-headless",
    "registered_worker": "1",
    "fallback_hop": "cross-harness-headless",
    "harness": "codex",
    "route_id": "rt-cell",
    "route_node": "plan-alternative",
    "route_hash": "sha256:cellhash",
    "registry_digest": "sha256:cellreg",
    "completion_gate": "code-plan",
    "attempt_id": "att-cell0001",
}


class _TerminalCellFixture:
    """One {verdict} x {artifact} x {marker} x {process} cell on disk."""

    def __init__(self, td, *, verdict="PASS", artifact_kind="dir",
                 process="dead", marker=False):
        base = Path(td)
        self.home = base / "home"
        self.worktree = base / "wt"
        self.root = base / "root"
        for path in (self.home, self.worktree, self.root):
            path.mkdir(parents=True, exist_ok=True)
        self.jobs = base / "jobs.log"
        log = base / "child.codex.jsonl"

        if artifact_kind == "bucket":
            # The ordinary shape of a real deliverable: a directory of files
            # inside the artifact root (a cycle's document bucket).
            bucket = self.root / "documents" / "2026-09-04_deliverable"
            bucket.mkdir(parents=True, exist_ok=True)
            (bucket / "REPORT.md").write_text("body\n", encoding="utf-8")
            artifact = str(bucket)
        elif artifact_kind == "dir":
            artifact = f"{self.root}/"
        elif artifact_kind == "file":
            artifact_path = self.root / "plan.alternative.md"
            artifact_path.write_text("plan body\n", encoding="utf-8")
            artifact = str(artifact_path)
        else:  # "none"
            artifact = "-"
        self.artifact = artifact
        events = [
            {"type": "item.completed",
             "item": {"id": "i1", "type": "agent_message",
                      "text": f"artifact: {artifact}\nverdict: {verdict}\nblocker: none"}},
            {"type": "turn.completed", "usage": {}},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

        if process == "live":
            pid = os.getpid()
            pid_start = R.process_start_ticks(pid)
        else:
            pid = _dead_pid()
            pid_start = "7486194400"
        meta = dict(_META_AXES)
        meta.update({
            "artifact_root": str(self.root),
            "route_file": str(base / "route.json"),
            "log_file": str(log),
            "pid": str(pid),
            "pid_start": str(pid_start),
            "pgid": str(pid),
        })
        self.meta = meta
        if marker:
            self._write_marker()
        pipe = ",".join(f"{k}={v}" for k, v in meta.items())
        self.raw = (f"2026-07-29T00:00:00.000000Z\topen\t{self.worktree}\t"
                    f"{self.worktree}\tcell-slug\t{pipe}")
        self.jobs.write_text(self.raw + "\n", encoding="utf-8")

    def _write_marker(self):
        directory = self.home / ".dispatch" / "completion" / "rt-cell"
        directory.mkdir(parents=True, exist_ok=True)
        evidence_path = self.root / "plan.alternative.md"
        if not evidence_path.is_file():
            evidence_path.write_text("plan body\n", encoding="utf-8")
        sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        marker = {
            "schema_version": 2,
            "route_id": "rt-cell",
            "route_hash": "sha256:cellhash",
            "registry_digest": "sha256:cellreg",
            "node_id": "plan-alternative",
            "attempt_id": "att-cell0001",
            "completion_gate": "code-plan",
            "dispatch_depth": 2,
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": True,
            "fallback_hop": "cross-harness-headless",
            "sequence": 1,
            "evidence": {"path": str(evidence_path), "sha256": sha},
        }
        history = directory / "plan-alternative.1.json"
        history.write_text(json.dumps(marker), encoding="utf-8")
        linkage = {
            "schema_version": 2,
            "route_id": "rt-cell",
            "node_id": "plan-alternative",
            "attempt_id": "att-cell0001",
            "dispatch_depth": 2,
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": True,
            "fallback_hop": "cross-harness-headless",
            "completion_marker": str(directory / "plan-alternative.json"),
            "completion_marker_history": str(history),
            "evidence_sha256": sha,
        }
        (directory / "plan-alternative.att-cell0001.attempt.json").write_text(
            json.dumps(linkage), encoding="utf-8"
        )

    def classify(self):
        rows = R.read_rows(self.jobs)
        assert rows and rows[0]["attempt_contract_status"] == "current", rows
        args = type("Args", (), {
            "agent_home": self.home, "now": time.time(),
            "jobs": self.jobs, "integration_ref": None,
        })()
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(self.root)}):
            return R.classify(rows[0], args, {}, rows)

    def child_row(self):
        rows = R.read_rows(self.jobs)
        return J.ChildRow(
            order=0, status="open", slug="cell-slug",
            attempt_id="att-cell0001", raw=self.raw, metadata=rows[0]["meta"],
        )


class TerminalHandoffMarkerBoundaryTest(unittest.TestCase):
    """Reconciler cells for the 2026-07-29 guard-parity incident: a route-bound
    PASS handoff must never classify `completed-*` without its completion
    marker (SD-70). Pre-fix, `classify` closed the incident row as
    `completed-terminal-handoff` from the envelope alone, hiding a worker that
    produced no in-scope artifact — the artifact-blind `inspect_terminal_log`
    disagreed with the artifact-checking `inspect_terminal_attempt` used by the
    supervisor on the same envelope.
    """

    def test_pass_dir_artifact_dead_process_is_dead_invalid_envelope(self):
        # The exact incident cell: PASS + directory artifact + no marker +
        # quiescent process. Completed classification would hide the failure.
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(td, artifact_kind="dir", process="dead")
            category, reason, note = cell.classify()
        self.assertEqual(note, "dead-invalid-envelope")
        self.assertEqual(category, "terminal-handoff")
        self.assertIn("marker-missing", reason)

    def test_pass_file_artifact_dead_process_is_dead_missing_marker(self):
        # A readable in-root artifact without the SD-70 marker is still not
        # completion evidence — but the note distinguishes the envelope layer.
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(td, artifact_kind="file", process="dead")
            category, reason, note = cell.classify()
        self.assertEqual(note, "dead-missing-marker")
        self.assertEqual(category, "terminal-handoff")

    def test_pass_no_marker_never_proposes_completed(self):
        # Regression guard across the artifact x process product: no marker-less
        # PASS cell may propose a `completed-*` note.
        for artifact_kind in ("dir", "file", "none"):
            for process in ("dead", "live"):
                with tempfile.TemporaryDirectory() as td:
                    cell = _TerminalCellFixture(
                        td, artifact_kind=artifact_kind, process=process
                    )
                    _, _, note = cell.classify()
                self.assertFalse(
                    str(note or "").startswith("completed"),
                    msg=f"{artifact_kind}/{process} -> {note}",
                )

    def test_pass_live_process_stays_active(self):
        # A still-draining worker that already printed its envelope must not be
        # closed dead by the reconciler (SD-79: quiescence gates the close).
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(td, artifact_kind="file", process="live")
            category, _, note = cell.classify()
        self.assertEqual(category, "active")
        self.assertIsNone(note)

    def test_pass_marker_linked_closes_completed_marker(self):
        # With the SD-70 marker + linkage present the row still closes as
        # completed — the fix must not regress legal marker-backed completion.
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(
                td, artifact_kind="file", process="dead", marker=True
            )
            category, reason, note = cell.classify()
        self.assertEqual(note, "completed-marker")
        self.assertEqual((category, reason),
                         ("marker-backed-stale", "completed-marker-linkage"))

    def test_fail_and_blocked_keep_typed_failure_notes(self):
        for verdict, expected in (("FAIL", "dead-worker-fail"),
                                  ("BLOCKED", "dead-worker-blocked")):
            with tempfile.TemporaryDirectory() as td:
                cell = _TerminalCellFixture(td, verdict=verdict, process="dead")
                category, _, note = cell.classify()
            self.assertEqual((category, note), ("terminal-handoff", expected),
                             msg=verdict)

    def test_completed_fallback_formula_not_reintroduced(self):
        # Source guard in this file's convention: the pre-fix one-liner that
        # turned any PASS envelope into completed-terminal-handoff must not
        # come back.
        src = Path(__file__).with_name("dispatch-registry.py").read_text(encoding="utf-8")
        self.assertNotIn('or "completed-terminal-handoff"', src)


class InvalidEnvelopeChildDegradeTest(unittest.TestCase):
    """Supervisor-side cells: `close_finished_child` on a child whose envelope
    can never legally complete. The fixture's invalid artifact is the artifact
    ROOT itself, which is refused as `artifact-is-root` — a directory deliverable
    inside the root is valid (a cycle document bucket is the ordinary shape), and
    the root names no more than nothing does. Pre-fix
    it returned only a skip reason, the owner got an impossible remediation
    prompt, and the second resume raised owned-children-remain-open-after-resume
    — killing the whole pipeline (incident owner att-7eb9d956, exit 70).
    """

    def test_quiescent_invalid_artifact_closes_typed_dead(self):
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(td, artifact_kind="dir", process="dead")
            row = cell.child_row()
            observed = J.observed_attempt_liveness(row.status, row.metadata,
                                                   terminal_envelope=True)
            self.assertEqual(observed.state, "reconcile-needed",
                             msg="fixture must present a quiescent exact process")
            with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(cell.root)}):
                outcome = J.close_finished_child(row, jobs=cell.jobs)
            self.assertEqual(outcome, "")
            closed = cell.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t", closed)
        self.assertIn("note=dead-invalid-envelope", closed)
        self.assertIn("reconcile_reason=terminal-invalid:artifact-is-root", closed)

    def test_quiescent_pass_without_artifact_closes_typed_dead(self):
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(td, artifact_kind="none", process="dead")
            with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(cell.root)}):
                outcome = J.close_finished_child(cell.child_row(), jobs=cell.jobs)
            self.assertEqual(outcome, "")
            closed = cell.jobs.read_text(encoding="utf-8")
        self.assertIn("note=dead-invalid-envelope", closed)

    def test_a_directory_deliverable_is_not_degraded(self):
        # The regression this cycle exists for: three depth-1 owners emitted a
        # PASS envelope naming a document directory and were closed
        # `dead-invalid-envelope`. A bucket inside the root must reach completion,
        # not the degrade path.
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(td, artifact_kind="bucket", process="dead")
            row = cell.child_row()
            with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(cell.root)}):
                outcome = J.close_finished_child(row, jobs=cell.jobs)
            # It reaches route completion (which this fixture has no route for)
            # instead of being refused as an invalid envelope.
            self.assertEqual(outcome, "completion-rejected")
            closed = cell.jobs.read_text(encoding="utf-8")
        self.assertNotIn("note=dead-invalid-envelope", closed)

    def test_live_process_keeps_skip_and_row_open(self):
        # Never race a still-draining worker: with the exact process live the
        # skip reason is preserved and the row stays open.
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(td, artifact_kind="dir", process="live")
            with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(cell.root)}):
                outcome = J.close_finished_child(cell.child_row(), jobs=cell.jobs)
            self.assertEqual(outcome, "terminal-invalid:artifact-is-root")
            self.assertIn("\topen\t", cell.jobs.read_text(encoding="utf-8"))

    def test_unverifiable_process_keeps_row_open(self):
        # Missing process identity is no quiescence proof (SD-79 fail-closed):
        # the degrade close must not fire.
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(td, artifact_kind="dir", process="dead")
            metadata = {k: v for k, v in cell.child_row().metadata.items()
                        if k not in {"pid", "pid_start", "pgid"}}
            row = J.ChildRow(order=0, status="open", slug="cell-slug",
                             attempt_id="att-cell0001", raw=cell.raw,
                             metadata=metadata)
            with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(cell.root)}):
                outcome = J.close_finished_child(row, jobs=cell.jobs)
            self.assertEqual(outcome, "terminal-invalid:artifact-is-root")
            self.assertIn("\topen\t", cell.jobs.read_text(encoding="utf-8"))

    def test_valid_readable_artifact_still_routes_completion(self):
        # The legal path is untouched: a readable in-root artifact goes through
        # run_route_completion, not the degrade close.
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(td, artifact_kind="file", process="dead")
            with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(cell.root)}), \
                 mock.patch.object(J, "run_route_completion", return_value="") as sealed:
                outcome = J.close_finished_child(cell.child_row(), jobs=cell.jobs)
            self.assertEqual(outcome, "")
            sealed.assert_called_once()
            self.assertIn("\topen\t", cell.jobs.read_text(encoding="utf-8"))

    def test_reconcile_finished_children_reports_degrade_as_closed(self):
        # The supervisor consumes this exact seam: a degraded child must come
        # back with an empty reason so `unresolved` shrinks instead of raising
        # owned-children-remain-open-after-resume.
        with tempfile.TemporaryDirectory() as td:
            cell = _TerminalCellFixture(td, artifact_kind="dir", process="dead")
            row = cell.child_row()
            with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(cell.root)}):
                outcomes = J.reconcile_finished_children(
                    {row.attempt_id: row}, {row.attempt_id}, jobs=cell.jobs
                )
        self.assertEqual(outcomes, {"att-cell0001": ""})


if __name__ == "__main__":
    unittest.main()
