#!/usr/bin/env python3
"""SD-165 (A165-1): the three adapters print the same part catalog and forward
`capability:stage` graph tokens to the one compiler unmodified. SD-164: so do
`--shape framed` with its hints and `--route-plan <record>#<i>`."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# The card screens these tests compare are Korean and UI text defaults to English,
# so this suite declares the Korean operator language itself (tools/i18n/ui_text.py).
os.environ["FLEET_NOW_LANG"] = "Korean"

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = [sys.executable, str(ROOT / "utilities" / "capability-route.py")]
SURFACES = {
    "claude": [sys.executable, str(ROOT / "adapters" / "claude" / "bin" / "capability-route.py")],
    "codex": ["sh", str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh")],
    "opencode": ["sh", str(ROOT / "adapters" / "opencode" / "bin" / "preflight.sh")],
}
AUDIT_BORROW = "inspect,autopilot-research:retrieval,autopilot-research:synthesis,report"


class AdapterParityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        # Dev activation: every surface resolves this checkout, never an installed release.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
        self.env.update(AGENT_HOME=str(ROOT), AGENT_DISPATCH_JOBS=str(base / "jobs.log"),
                        DISPATCH_DEFAULTS_CONFIG=str(ROOT / "profiles" / "dispatch-defaults.yaml"))
        (base / "jobs.log").write_text("", encoding="utf-8")
        self.repo, self.artifacts = base / "repo", base / "artifacts"
        self.repo.mkdir()
        self.artifacts.mkdir()
        for argv in (["git", "init", "-q", str(self.repo)],
                     ["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"],
                     ["git", "-C", str(self.repo), "config", "user.name", "Test"]):
            subprocess.run(argv, check=True)
        (self.repo / "README").write_text("fixture\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        self.evidence = base / "dispatch-evidence.json"
        self.evidence.write_text(json.dumps({"tuples": [{
            "parent_harness": "claude", "parent_transport": "headless", "parent_sandbox": "adapter-default",
            "child_harness": "codex", "launch_authority": "conductor", "status": "supported",
            "probe_source": "fixture-probe", "probe_time": "2026-07-16T00:00:00Z", "failure_class": "",
            "checked_worktree": str(self.repo.resolve()), "failure_scope": "none", "codex_command": "ok",
            "retry_on_isolated_worktree": 0}]}))

    def run_surface(self, argv, *args):
        result = subprocess.run([*argv, *args], text=True, capture_output=True, env=self.env, cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_stages_output_is_identical_on_every_adapter(self):
        for args in (("stages", "--capability", "autopilot-lab", "--json"),
                     ("stages", "--capability", "autopilot-lab"), ("stages", "--json")):
            expected = self.run_surface(PORTABLE, *args).stdout
            self.assertIn("autopilot-lab:diagnose", expected)
            for name, argv in SURFACES.items():
                with self.subTest(adapter=name, args=args):
                    self.assertEqual(self.run_surface(argv, *args).stdout, expected)

    def test_compose_forwards_part_tokens_unmodified(self):
        args = ("compose", "--shape", "staged", "--capability", "audit", "--graph", AUDIT_BORROW,
                "--slug", "sd165-parity", "--unassigned", "--cwd", str(self.repo),
                "--artifact-root", str(self.artifacts), "--dispatch-evidence", str(self.evidence),
                "--parent-harness", "claude", "--spec-read", "not-applicable", "--explain")
        expected = json.loads(self.run_surface(PORTABLE, *args).stdout)
        self.assertEqual([node["id"] for node in expected["nodes"]], [
            "inspect", "autopilot-research-retrieval", "autopilot-research-retrieval-alternative",
            "autopilot-research-synthesis", "report"])
        for name, argv in SURFACES.items():
            with self.subTest(adapter=name):
                result = self.run_surface(argv, *args)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertIn("빌린 부품 autopilot-research:retrieval·autopilot-research:synthesis", result.stderr)

    def compose_args(self, *extra):
        return ("compose", "--slug", "sd164-parity", "--unassigned", "--cwd", str(self.repo),
                "--artifact-root", str(self.artifacts), "--dispatch-evidence", str(self.evidence),
                "--parent-harness", "claude", "--spec-read", "not-applicable", *extra)

    def sealed_route(self, argv, *args):
        """The route file one surface wrote, and the card text it printed."""
        result = self.run_surface(argv, *args)
        line = next(item for item in result.stderr.splitlines() if item.startswith("route_file="))
        return json.loads(Path(line.partition("=")[2]).read_text(encoding="utf-8")), result.stderr

    def test_framed_shape_and_its_hints_arrive_unchanged(self):
        task = self.artifacts / "task.md"
        task.write_text("Audit the parser and report.\n", encoding="utf-8")
        args = self.compose_args("--shape", "framed", "--capability", "audit", "--capability-mode", "audit",
                                 "--graph", "inspect,report", "--profile", "light", "--prompt-file", str(task))
        expected, _ = self.sealed_route(PORTABLE, *args)
        self.assertEqual(expected["capability"], "route-frame")          # the hints did not become the route
        self.assertEqual(expected["selection"]["shape"], "framed")
        hints = expected["work_request"]["routing_hints"]
        self.assertEqual((hints["capability"], hints["graph"], hints["profile"]), ("audit", "inspect,report", "light"))
        for name, argv in SURFACES.items():
            with self.subTest(adapter=name):
                route, card = self.sealed_route(argv, *args)
                self.assertEqual(route, expected)
                self.assertIn("frame이 방향과 경로를 조립해 제안합니다", card)

    def test_route_plan_path_and_index_arrive_unchanged(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import route_plan as RP
        leg = {"capability": "autopilot-code", "shape": "staged", "graph": ["execute", "test", "report"]}
        decision = RP.build_decision(
            frame_route={"route_id": "rt-frame", "route_hash": "sha256:frame", "cycle_id": "cyc-frame"},
            selected="yes", reason="", briefs=[], intent={"path": "intent.md", "sha256": "sha256:i"},
            proposal={"legs": [leg]}, first_leg_compose={"leg": 0})
        record = self.artifacts / "route-decision.json"
        record.write_bytes(RP.render(RP.build_record(decision)))
        graph = ("--shape", "staged", "--capability", "autopilot-code", "--graph", "execute,test,report")
        args = self.compose_args(*graph, "--route-plan", f"{record}#0")
        expected, _ = self.sealed_route(PORTABLE, *args)
        self.assertEqual(expected["route_plan"]["index"], 0)             # the plan reached the compiler and sealed
        self.assertEqual(expected["route_plan"]["digest"], RP.decision_digest(decision))
        self.assertFalse([n for n in expected["nodes"] if n["id"].startswith("frame")])
        for name, argv in SURFACES.items():
            with self.subTest(adapter=name):
                self.assertEqual(self.sealed_route(argv, *args)[0], expected)
        # An explicit unreadable plan refuses before publication on every adapter.
        missing = self.compose_args(*graph, "--route-plan", f"{self.artifacts}/absent.json#0")
        before = {p: p.read_bytes() for p in self.artifacts.rglob("rt-*.json")}
        for name, argv in {"portable": PORTABLE, **SURFACES}.items():
            with self.subTest(adapter=name, plan="unreadable"):
                result = subprocess.run([*argv, *missing], text=True, capture_output=True,
                                        env=self.env, cwd=self.repo)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("compose-route-plan-refused", result.stderr)
                self.assertNotIn("[경로]", result.stderr)
                self.assertNotIn("route_file=", result.stderr)
        self.assertEqual({p: p.read_bytes() for p in self.artifacts.rglob("rt-*.json")}, before)


if __name__ == "__main__":
    unittest.main()
