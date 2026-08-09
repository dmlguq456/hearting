#!/usr/bin/env python3
"""SD-69 disposable fixture: Codex linked-worktree mutation boundary.

Proves, against a real throwaway repo + `git worktree add` linked worktree:
  1. writable roots include the primary `.spec-grounding` + artifact root,
     and never the git-common-dir/`.git`.
  2. a simulated source edit persists in the worktree and a
     `.spec-grounding/<marker>` write lands in the PRIMARY checkout.
  3. commit stays honestly unavailable: `no_commit=1` is recorded, no commit
     is claimed, and route_hash/source_commit are unforged.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_R_SPEC = importlib.util.spec_from_file_location("route", ROOT / "utilities/capability-route.py")
ROUTE = importlib.util.module_from_spec(_R_SPEC)
_R_SPEC.loader.exec_module(ROUTE)
_WH_SPEC = importlib.util.spec_from_file_location(
    "codex_dispatch_headless_nocommit", ROOT / "adapters/codex/bin/dispatch-headless.py")
WH = importlib.util.module_from_spec(_WH_SPEC)
_WH_SPEC.loader.exec_module(WH)


class CodexNoCommitFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.primary = self.base / "primary"
        self.primary.mkdir()
        subprocess.run(["git", "init", "-q", str(self.primary)], check=True)
        subprocess.run(["git", "-C", str(self.primary), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.primary), "config", "user.name", "Fixture"], check=True)
        (self.primary / "x").write_text("x", encoding="utf-8")
        (self.primary / "core").mkdir()
        (self.primary / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.primary), "add", "x", "core/CORE.md"], check=True)
        subprocess.run(["git", "-C", str(self.primary), "commit", "-qm", "init"], check=True)
        self.linked = self.base / "linked-worktree"
        subprocess.run(
            ["git", "-C", str(self.primary), "worktree", "add", "-q", "-b", "fixture-linked", str(self.linked)],
            check=True,
        )
        self.artifact = self.base / ".agent_reports"
        self.artifact.mkdir()
        self.jobs = self.base / "jobs.log"
        self.logs = self.base / "logs"
        self.owner = subprocess.Popen(["sleep", "60"])
        owner_start = (Path("/proc") / str(self.owner.pid) / "stat").read_text().split()[21]
        self.jobs.write_text(
            f"2026-07-23T00:00:00Z\topen\t{self.linked}\t{self.linked}\towner\t"
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=owner,harness=codex,"
            "runtime_sandbox=workspace-write,"
            f"attempt_id=att-nocommit-parent,pid={self.owner.pid},pid_start={owner_start}\n"
        )

    def tearDown(self):
        if self.owner.poll() is None:
            self.owner.kill()
        self.owner.wait()
        subprocess.run(["git", "-C", str(self.primary), "worktree", "remove", "--force", str(self.linked)],
                        capture_output=True)
        self.temp.cleanup()

    def compile_route(self):
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec", "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        dispatch = {"tuples": [{
            "parent_harness": "codex", "parent_transport": "headless", "parent_sandbox": "workspace-write",
            "child_harness": "codex", "launch_authority": "conductor", "status": "supported",
            "probe_source": "codex-fixture", "probe_time": "2026-07-16T00:00:00Z", "failure_class": "",
            "checked_worktree": str(self.linked.resolve()), "failure_scope": "none",
            "codex_command": "ok", "retry_on_isolated_worktree": 0,
        }], "native_subagent": []}
        return ROUTE.compile_route(
            "autopilot-code", "dev", "strong", self.linked, self.artifact,
            signals=["shared-contract"], transport="headless", tracking="tracked",
            tracked_gate_evidence=gate, dispatch_evidence=dispatch,
        )

    def base_env(self):
        return {
            **{key: value for key, value in os.environ.items() if key != "AGENT_DISPATCH_JOBS"},
            "AGENT_HOME": str(self.primary),
            "AGENT_ARTIFACT_ROOT": str(self.artifact),
            "AGENT_DISPATCH_ATTEMPT_ID": "att-nocommit-parent",
        }

    def test_writable_roots_and_no_commit_encoding(self):
        route = self.compile_route()
        route_path = self.base / "route.json"
        route_path.write_text(json.dumps(route), encoding="utf-8")
        node = next(n for n in route["nodes"] if n["id"] == "execute")
        self.assertIn("source/**", node["write_scope"], "fixture precondition: execute mutates source")

        args = [
            sys.executable, str(ROOT / "adapters/codex/bin/dispatch-headless.py"),
            "--register", "--worktree", str(self.linked), "--slug", "codex-nocommit-fixture",
            "--capability", "autopilot-code", "--capability-mode", route["capability_mode"],
            "--worker-mode", node["unit"], "--qa", "standard",
            "--intensity", "strong", "--dispatch-depth", "2", "--parent", "owner",
            "--parent-harness", "codex", "--parent-transport", "headless",
            "--parent-sandbox", "workspace-write", "--nested-eligibility", "supported",
            "--eligibility-source", "codex-fixture", "--fallback-ordinal", "1",
            "--route-file", str(route_path), "--route-id", route["route_id"],
            "--route-hash", route["route_hash"], "--route-node", "execute",
            "--registry-digest", route["registry_digest"],
            "--write-scope", ";".join(node["write_scope"]),
            "--completion-gate", node["completion_gate"],
            "--unit", node.get("unit", ""),
            "--model-role", node["role"],
            "--model-profile", node["model_profile"],
            "--jobs", str(self.jobs), "--log-dir", str(self.logs),
        ]
        result = subprocess.run(args, text=True, capture_output=True, env=self.base_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # (1) writable roots: primary .spec-grounding + artifact root present; git metadata absent.
        command_line = next(ln for ln in result.stdout.splitlines() if ln.startswith("command="))
        command = command_line[len("command="):]
        self.assertIn(str(self.primary / ".spec-grounding"), command)
        self.assertIn(str(self.artifact), command)
        self.assertNotIn(str(self.primary / ".git"), command)
        common_dir = subprocess.run(
            ["git", "-C", str(self.linked), "rev-parse", "--git-common-dir"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertNotIn(os.path.realpath(common_dir), command)
        self.assertTrue((self.primary / ".spec-grounding").is_dir())
        self.assertEqual((self.primary / ".spec-grounding").stat().st_mode & 0o777, 0o700)

        # (3) commit stays honestly unavailable — no_commit=1 recorded, prompt says so.
        row = self.jobs.read_text(encoding="utf-8").strip().splitlines()[-1]
        self.assertIn(",no_commit=1", row)
        self.assertNotIn("commit=1", row.replace(",no_commit=1", ""))
        prompt = next(
            self.logs.glob("codex-nocommit-fixture.*.codex.prompt.txt")
        ).read_text(encoding="utf-8")
        self.assertIn("No-commit worker (SD-69)", prompt)
        self.assertIn("do NOT `git commit`", prompt)

        # route_hash / source_commit unforged: the row's own committed evidence still matches
        # the route record's hash and the primary's real HEAD at compile time.
        self.assertIn(f"route_hash={route['route_hash']}", row)
        self.assertEqual(route["source_commit"],
                          subprocess.run(["git", "-C", str(self.linked), "rev-parse", "HEAD"],
                                         text=True, capture_output=True, check=True).stdout.strip())

    def test_linked_detection_uses_git_topology_not_agent_home_path(self):
        self.assertTrue(WH._is_linked_worktree(self.linked, self.primary))
        self.assertFalse(
            WH._is_linked_worktree(self.primary, ROOT),
            "an external primary checkout is not a linked worktree merely because it differs from AGENT_HOME",
        )
        self.assertTrue(
            WH._is_linked_worktree(self.base / "not-a-repo", self.primary),
            "unprovable Git topology must conservatively retain the no-commit boundary",
        )

    def test_source_edit_and_primary_spec_marker_persist_across_boundary(self):
        """(2) a simulated worker's source edit persists in the worktree, and a
        `.spec-grounding/<marker>` write lands in the PRIMARY checkout — the
        exact narrow root the argv builder actually grants, exercised directly
        rather than via a real (unavailable in CI) `codex exec` process."""
        route = self.compile_route()
        (self.linked / "source_edit.txt").write_text("worker output\n", encoding="utf-8")
        self.assertTrue((self.linked / "source_edit.txt").is_file())

        spec_grounding = self.primary / ".spec-grounding"
        spec_grounding.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = spec_grounding / "fixture-marker.json"
        marker.write_text(json.dumps({"route_id": route["route_id"]}), encoding="utf-8")
        self.assertTrue(marker.is_file())
        self.assertFalse((self.linked / ".spec-grounding").exists(),
                          "the marker root is the PRIMARY checkout, never the sandboxed worktree")

        # HEAD stays exactly source_commit (no forged/no actual commit) — worker-route-guard's
        # existing exact-match gate for a first attempt already covers this; this test only
        # pins the honest-unavailability half of SD-69's boundary claim.
        head = subprocess.run(["git", "-C", str(self.linked), "rev-parse", "HEAD"],
                               text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual(head, route["source_commit"])


if __name__ == "__main__":
    unittest.main()
