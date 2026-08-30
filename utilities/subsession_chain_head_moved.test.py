#!/usr/bin/env python3
"""SD-119 WP5/WP6 (plan.md §4.6): the exact ci-green regression --

1/4 slices commit and move HEAD before slice 2 registers/starts. Two
separate surfaces are exercised against tmpdir fixtures only (no live
state, no live artifact root):

* `stage-session-chain.py`'s serial register loop (WP5 all-or-nothing).
* `worker-route-guard.py`'s `planned_subsession_ok` lineage acceptance,
  now referencing `dispatch_completion_join.SUCCESS_NOTES` instead of the
  single literal `"completed-supervisor"` (WP6).
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ROUTE = _load("capability_route_for_head_moved_fixture", ROOT / "utilities" / "capability-route.py")
GUARD = _load("worker_route_guard_for_head_moved_fixture", ROOT / "utilities" / "worker-route-guard.py")
CHAIN = _load("stage_session_chain_for_head_moved_fixture", ROOT / "utilities" / "stage-session-chain.py")

ALL = [
    "atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
    "no-artifact-handoff", "no-independent-verifier", "focused-verification",
]


def _dispatch(worktree):
    return {
        "tuples": [{
            "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "workspace-write", "child_harness": "codex",
            "launch_authority": "conductor", "status": "supported",
            "probe_source": "fixture", "probe_time": "2026-07-16T00:00:00Z",
            "failure_class": "", "checked_worktree": str(Path(worktree).resolve()),
            "failure_scope": "none", "codex_command": "ok", "retry_on_isolated_worktree": 0,
        }],
        "native_subagent": [],
    }


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    (repo / "x").write_text("a")
    subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "a"], check=True)


def _compile_route(repo: Path) -> dict:
    gate = {
        "spec_read": {"satisfied": True, "source": "prd-sha256"},
        "drift_verdict": "within-spec", "workflow_mode": "tracked",
        "artifact_guard": {"satisfied": True, "source": "conductor"},
    }
    return ROUTE.compile_route(
        "autopilot-code", "dev", "strong", repo, repo, predicates=ALL,
        signals=["shared-contract"], transport="headless", tracking="tracked",
        tracked_gate_evidence=gate, dispatch_evidence=_dispatch(repo),
    )


def _write_registry_row(
    jobs_path: Path, *, status: str, route_id: str, node_id: str, attempt_id: str,
    session_chain_id: str, subsession_index: int, subsession_count: int,
    note: str = "", failure_class: str = "",
) -> None:
    fields = {
        "route_id": route_id, "route_node": node_id, "attempt_id": attempt_id,
        "subsession_id": f"ss-{subsession_index}",
        "subsession_purpose": "planned", "subsession_mode": "serial",
        "session_chain_id": session_chain_id, "stage_authority": "0",
        "subsession_index": str(subsession_index), "subsession_count": str(subsession_count),
    }
    if note:
        fields["note"] = note
    if failure_class:
        fields["failure_class"] = failure_class
    pipe = ",".join(f"{key}={value}" for key, value in fields.items())
    line = "\t".join(["2026-08-30T00:00:00Z", status, "repo", "worktree", "slug", pipe])
    with jobs_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class ChainHeadMovedGuardAcceptanceTest(unittest.TestCase):
    """`worker-route-guard.py`'s `planned_subsession_ok` side: exact ci-green
    reproduction (plan.md §4.6 table)."""

    CHAIN_ID = "chain-head-moved-fixture"

    def _prep(self, td, *, prior_note="completed-supervisor"):
        repo = Path(td) / "repo"
        _init_repo(repo)
        route = _compile_route(repo)
        # Slice 1's own work: a commit that moves HEAD to a first-parent
        # descendant of route["source_commit"] -- exactly ci-green's defect.
        (repo / "x").write_text("b")
        subprocess.run(["git", "-C", str(repo), "commit", "-am", "b", "-q"], check=True)
        path = Path(td) / "route.json"
        path.write_text(json.dumps(route))
        jobs = Path(td) / "jobs.log"
        jobs.write_text("")
        _write_registry_row(
            jobs, status="done", route_id=route["route_id"], node_id="execute",
            attempt_id="att-slice-1", session_chain_id=self.CHAIN_ID,
            subsession_index=1, subsession_count=4,
            note=prior_note, failure_class="pass",
        )
        # Slice 2's own row: the pre-registration this whole mechanism rests
        # on -- it exists in the registry *before* slice 2's validate call,
        # exactly as WP5's all-or-nothing register loop guarantees.
        _write_registry_row(
            jobs, status="open", route_id=route["route_id"], node_id="execute",
            attempt_id="att-slice-2", session_chain_id=self.CHAIN_ID,
            subsession_index=2, subsession_count=4,
        )
        return repo, route, path, jobs

    def test_slice_2_validate_passes_via_planned_subsession_ok_despite_head_move(self):
        with tempfile.TemporaryDirectory() as td:
            repo, route, path, jobs = self._prep(td)
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                _, node, _ = GUARD.validate_route_contract(
                    path, "execute", repo, repo, current_attempt="att-slice-2",
                )
            self.assertEqual(node["id"], "execute")

    def test_control_a_removing_slice_2s_own_pre_registration_row_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            repo, route, path, jobs = self._prep(td)
            # Rewrite the registry without slice 2's own row -- the sole
            # thing this control changes.
            lines = [
                line for line in jobs.read_text(encoding="utf-8").splitlines()
                if "attempt_id=att-slice-2" not in line
            ]
            jobs.write_text("\n".join(lines) + ("\n" if lines else ""))
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                with self.assertRaises(GUARD.WorkerRouteError) as ctx:
                    GUARD.validate_route_contract(
                        path, "execute", repo, repo, current_attempt="att-slice-2",
                    )
            self.assertEqual(ctx.exception.reason, "route-source-commit-mismatch")

    def test_control_b_slice_1_note_outside_success_notes_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            repo, route, path, jobs = self._prep(td, prior_note="in-progress")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                with self.assertRaises(GUARD.WorkerRouteError) as ctx:
                    GUARD.validate_route_contract(
                        path, "execute", repo, repo, current_attempt="att-slice-2",
                    )
            self.assertEqual(ctx.exception.reason, "route-source-commit-mismatch")

    def test_wp6_accepts_the_second_success_note_completed_marker(self):
        with tempfile.TemporaryDirectory() as td:
            repo, route, path, jobs = self._prep(td, prior_note="completed-marker")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                _, node, _ = GUARD.validate_route_contract(
                    path, "execute", repo, repo, current_attempt="att-slice-2",
                )
            self.assertEqual(node["id"], "execute")

    def test_missing_agent_dispatch_jobs_rejects_for_a_different_reason(self):
        # §7-9: a missing AGENT_DISPATCH_JOBS silently makes
        # _qualifying_subsession_lineage() return False, so the same typed
        # reason as the real accept/refuse cases must not appear here for
        # the wrong underlying cause. This asserts only that this refusal is
        # (a) still a refusal and (b) does not accidentally read the guard's
        # git-lineage evidence as satisfied any other way -- the top-level
        # exception type and reason string are identical to the other
        # rejections above (worker-route-guard.py has exactly one typed
        # reason for the whole mutating-scope branch), so what actually
        # distinguishes "missing env" from "bad lineage" is that removing
        # AGENT_DISPATCH_JOBS refuses even when every registry row above is
        # otherwise perfectly valid.
        with tempfile.TemporaryDirectory() as td:
            repo, route, path, jobs = self._prep(td)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AGENT_DISPATCH_JOBS", None)
                with self.assertRaises(GUARD.WorkerRouteError) as ctx:
                    GUARD.validate_route_contract(
                        path, "execute", repo, repo, current_attempt="att-slice-2",
                    )
            self.assertEqual(ctx.exception.reason, "route-source-commit-mismatch")

    def test_missing_agent_dispatch_jobs_short_circuits_before_reading_any_registry_row(self):
        # impl-review round 1 finding 4: the previous version of this test
        # only asserted the same top-level `route-source-commit-mismatch`
        # reason as `test_control_a_...`/`test_control_b_...` above, which
        # would also pass if a future regression made the missing-env case
        # fall through to the genuine lineage evaluation and coincidentally
        # fail there too -- it would pass for the wrong reason. This asserts
        # the lineage helper's own result and internal short-circuit
        # directly: with `AGENT_DISPATCH_JOBS` unset, `_qualifying_subsession_lineage()`
        # returns `False` from its very first guard clause and never calls
        # `FALLBACK.registry_rows()` at all -- unlike a genuine lineage
        # mismatch (control A/B), which always reads the registry and then
        # evaluates row content.
        with tempfile.TemporaryDirectory() as td:
            repo, route, path, jobs = self._prep(td)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AGENT_DISPATCH_JOBS", None)
                with mock.patch.object(GUARD.FALLBACK, "registry_rows") as registry_rows:
                    result = GUARD._qualifying_subsession_lineage(
                        route["route_id"], "execute", "att-slice-2",
                    )
            self.assertFalse(result)
            registry_rows.assert_not_called()

        # Control: with the same otherwise-valid fixture and
        # `AGENT_DISPATCH_JOBS` set, the same helper reads the registry and
        # accepts -- proving the missing-env `False` above is a distinct
        # code path, not a coincidentally-identical lineage refusal.
        with tempfile.TemporaryDirectory() as td:
            repo, route, path, jobs = self._prep(td)
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}):
                result = GUARD._qualifying_subsession_lineage(
                    route["route_id"], "execute", "att-slice-2",
                )
            self.assertTrue(result)


class ChainSerialRegisterAtomicityTest(unittest.TestCase):
    """`stage-session-chain.py`'s serial register loop side (WP5)."""

    def _manifest(self, base: Path, session_count: int = 4) -> dict:
        sessions = [
            {
                "subsession_id": f"ss-{i}", "index": i, "count": session_count,
                "adapter": "claude", "slug": f"slug-{i}", "phase_brief": f"brief-{i}",
                "narrow_verify": "true", "expected_round_trips": 1,
                "attempt_id": f"att-stage-session-{i}", "fixed_files": [],
            }
            for i in range(1, session_count + 1)
        ]
        return {
            "route_file": str(base / "route.json"), "route_node": "execute",
            "worktree": str(base), "chain_id": "chain-fixture", "mode": "serial",
            "sessions": sessions, "_manifest_path": str(base / "chain.json"),
            "_manifest_sha256": "deadbeef",
        }

    def _run(self, base: Path, manifest: dict, action: str, *, run_checked_side_effect):
        envelope = base / "chain.json"
        envelope.write_text(json.dumps({
            "route_file": manifest["route_file"], "route_node": "execute",
        }))
        (base / "route.json").write_text(json.dumps({"nodes": [{"id": "execute"}]}))
        with mock.patch.object(CHAIN, "load_manifest", return_value=manifest), \
                mock.patch.object(CHAIN.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(CHAIN, "resolve_global_registry") as registry, \
                mock.patch.object(CHAIN, "run_checked", side_effect=run_checked_side_effect), \
                mock.patch.object(sys, "argv", [
                    "stage-session-chain.py", action,
                    "--manifest", str(envelope), "--parent", "owner",
                    "--jobs", str(base / "jobs.log"),
                ]):
            registry.return_value = mock.Mock(path=str(base / "jobs.log"))
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                result = CHAIN.main()
                printed = out.getvalue()
        return result, printed

    def test_full_registration_then_single_start_evidence_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manifest = self._manifest(base)
            started = []

            def launch(command):
                started.append(command[command.index("--action") + 1])
                return mock.Mock(returncode=0, stdout="", stderr="")

            result, printed = self._run(base, manifest, "start", run_checked_side_effect=launch)
            self.assertEqual(result, 0)
            self.assertEqual(started, ["register"] * 4 + ["start"])
            lines = [line for line in printed.splitlines() if line]
            self.assertEqual(lines, [
                "chain_id=chain-fixture",
                "chain_manifest_sha256=deadbeef",
                "registered_sessions=4",
                "registered=1",
                "started=1",
                "started_subsession_index=1",
                "child_spawned=1",
                "runtime_wait=registered-children",
            ])

    def test_third_register_forced_failure_cancels_prior_rows_and_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manifest = self._manifest(base)
            (base / "jobs.log").write_text("")
            calls = {"n": 0}
            cancelled_ids = []

            def launch(command):
                calls["n"] += 1
                if calls["n"] == 3:
                    return mock.Mock(returncode=65, stdout="", stderr="register-failed\n")
                return mock.Mock(returncode=0, stdout="", stderr="")

            def fake_close(jobs, attempt_id, note):
                cancelled_ids.append(attempt_id)
                return True

            with mock.patch.object(CHAIN, "close_attempt_row", side_effect=fake_close):
                result, printed = self._run(base, manifest, "register", run_checked_side_effect=launch)
            self.assertEqual(result, 65)
            self.assertEqual(cancelled_ids, ["att-stage-session-1", "att-stage-session-2"])
            payload = json.loads(printed.splitlines()[0])
            self.assertEqual(payload["state"], "subdivision-batch-refused")
            self.assertEqual(
                payload["reason"], CHAIN.SUBDIVISION_ADMISSION.BATCH_REGISTRATION_INCOMPLETE,
            )
            self.assertEqual(payload["admitted_rows"], 0)
            self.assertEqual(payload["admitted_models"], 0)
            self.assertEqual(payload["cancelled_rows"], 2)

    def test_normal_path_still_prints_registered_one_and_evidence_eight_lines(self):
        # A-5 invariant (SD-119 (2)): the WP5 change must not touch the
        # already-correct `registered=1` evidence line.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manifest = self._manifest(base)

            def launch(command):
                return mock.Mock(returncode=0, stdout="", stderr="")

            result, printed = self._run(base, manifest, "start", run_checked_side_effect=launch)
            self.assertEqual(result, 0)
            self.assertIn("registered=1", printed)
            lines = [line for line in printed.splitlines() if line]
            self.assertEqual(len(lines), 8)


if __name__ == "__main__":
    unittest.main()
