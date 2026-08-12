#!/usr/bin/env python3
"""I-2 regression (assignment 검증요구 (a) / plan-check round-1 T4, frame §4):
a completion marker written under the canonical dispatch state root
(dirname of AGENT_DISPATCH_JOBS) must survive a release rotation that
physically deletes the old packaged AGENT_HOME (tools/install/distribution.py
_cleanup_releases), and every reader -- completion_marker_gate,
dispatch-registry.py, tools/fleet/route.py -- must still find the same
marker afterward. Before this cycle, the writer used
$AGENT_HOME/.dispatch/completion, which _cleanup_releases deletes wholesale
every second rotation.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "utilities"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROUTE = _load("route_for_rotation_test", "utilities/capability-route.py")
REGISTRY = _load("dispatch_registry_for_rotation_test", "utilities/dispatch-registry.py")
FLEET_ROUTE = _load("fleet_route_for_rotation_test", "tools/fleet/route.py")
import dispatch_contract as DC  # noqa: E402


class DispatchStateRootRotationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

        # Fake runtime-state home: a stable location that survives rotation.
        self.runtime_jobs = self.base / "rt" / ".harness" / "dispatch" / "jobs.log"

        # Fake packaged release: this whole tree gets rmtree'd, simulating
        # tools/install/distribution.py _cleanup_releases dropping an old
        # release once a newer one has been kept.
        self.release = self.base / "releases" / "v1"
        (self.release / "core").mkdir(parents=True)
        (self.release / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")

        self.repo = self.base / "repo"
        self.repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Fixture"], check=True)
        (self.repo / "x").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "x"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)

        self.artifact = self.base / ".agent_reports"
        self.artifact.mkdir()

        self._prior_env = {
            key: os.environ.get(key)
            for key in ("AGENT_HOME", "AGENT_DISPATCH_JOBS", "CLAUDE_HOME")
        }
        os.environ["AGENT_HOME"] = str(self.release)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.runtime_jobs)
        os.environ.pop("CLAUDE_HOME", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _compile_route(self):
        rows = [{
            "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "workspace-write", "child_harness": "codex",
            "launch_authority": "conductor", "status": "supported",
            "probe_source": "rotation-fixture", "probe_time": "2026-08-12T00:00:00Z",
            "failure_class": "", "checked_worktree": str(self.repo.resolve()),
            "failure_scope": "none", "codex_command": "ok",
            "retry_on_isolated_worktree": 0,
        }]
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec", "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        return ROUTE.compile_route(
            "autopilot-code", "dev", "strong", self.repo, self.artifact,
            signals=["shared-contract"], transport="headless", tracking="tracked",
            tracked_gate_evidence=gate,
            dispatch_evidence={"tuples": rows, "native_subagent": []},
        )

    def test_marker_survives_release_rotation_across_all_readers(self):
        route = self._compile_route()
        node = next(n for n in route["nodes"] if n["id"] == "plan")
        evidence = self.base / "plan.md"
        evidence.write_text("plan\n", encoding="utf-8")

        # complete_node() (not write_completion_marker() directly) is the real
        # entry point every wrapper uses -- it also publishes the exact-attempt
        # linkage sidecar that completion_marker_is_current()/the gate require.
        marker, _ = ROUTE.complete_node(
            route, node, "plan", evidence,
            attempt_id="att-rotation-fixture",
            explicit_attempt_metadata={
                "attempt_schema_version": 2,
                "dispatch_depth": 2,
                "transport": "headless",
                "execution_surface": "registered-headless",
                "registered_worker": True,
                "fallback_hop": "same-harness-headless",
            },
        )

        # State root is beside the runtime registry, never inside the release.
        state_root = DC.dispatch_state_root(self.runtime_jobs)
        canonical_path = state_root / "completion" / route["route_id"] / "plan.json"
        self.assertTrue(canonical_path.is_file())
        self.assertFalse(str(canonical_path).startswith(str(self.release)))

        # Simulate _cleanup_releases dropping the packaged root entirely.
        shutil.rmtree(self.release)
        self.assertFalse(self.release.exists())

        # Reader 1: dispatch_contract.completion_marker_gate (called with a
        # fabricated depends_on to force the marker lookup path).
        route_with_dep = dict(route)
        route_with_dep["nodes"] = [
            dict(n, depends_on=["plan"]) if n["id"] == "execute" else n
            for n in route["nodes"]
        ]
        route_file = self.base / "route.json"
        route_file.write_text(json.dumps(route_with_dep), encoding="utf-8")
        # completion_marker_gate must not raise completion-marker-missing for
        # the now-satisfied "plan" dependency of "execute".
        try:
            DC.completion_marker_gate(
                str(route_file), "execute", "start",
                self.release, self.runtime_jobs,
            )
        except DC.DispatchContractError as exc:
            self.assertNotEqual(
                exc.reason, "completion-marker-missing",
                f"reader 1 (completion_marker_gate) lost the marker after rotation: {exc.detail}",
            )

        # Reader 2: dispatch-registry.py's route_incomplete() -- the same
        # primitive Fleet's orphan classifier and preflight status use.
        fake_row = {
            "repo": str(self.repo), "worktree": str(self.repo), "slug": "owner",
            "meta": {
                "route_id": route["route_id"], "route_file": str(route_file),
                "attempt_id": "att-owner-fixture",
            },
        }
        incomplete, status = REGISTRY.route_incomplete(
            fake_row, self.release, rows=[fake_row], jobs=self.runtime_jobs,
        )
        self.assertEqual(status, "ok")
        self.assertNotIn("plan", incomplete)

        # Reader 3: tools/fleet/route.py gate_mark() -- the Fleet UI's own
        # completion-marker reader.
        passed = FLEET_ROUTE.gate_mark(route, "plan", home=str(self.release))
        self.assertIs(passed, True)

        # Read-fallback: an explicit legacy-relative candidate (no state root
        # override) must not find anything new post-rotation -- the writer
        # never wrote there -- proving the marker really lived at the new
        # root, not merely readable through a coincidence.
        legacy_marker = self.release / ".dispatch" / "completion" / route["route_id"] / "plan.json"
        self.assertFalse(legacy_marker.exists())


if __name__ == "__main__":
    unittest.main()
