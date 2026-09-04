#!/usr/bin/env python3
"""Surface/depth contract conformance for dispatch schema v20."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / "utilities/capability-route.py"
ADAPTERS = ("claude", "codex", "opencode")


class DispatchV20ConformanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.artifact = self.base / ".agent_reports"
        self.artifact.mkdir()
        (self.artifact / ".runtime" / "routes").mkdir(parents=True)
        self.gate_args = [
            "--tracking", "tracked",
            "--spec-read", "fixture",
            "--drift-verdict", "within-spec",
            "--workflow-mode", "tracked",
            "--artifact-guard", "fixture",
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def compile_quick(self, evidence):
        evidence_path = self.base / "quick-evidence.json"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        output = self.artifact / ".runtime" / "routes" / "quick-route.json"
        command = [
            sys.executable, str(ROUTE), "compile",
            "--capability", "autopilot-code",
            "--capability-mode", "dev",
            "--slug", "dispatch-v20-fixture",
            "--intensity", "quick",
            "--cwd", str(self.repo),
            "--artifact-root", str(self.artifact),
            "--registered-headless-evidence", str(evidence_path),
            *self.gate_args,
        ]
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode == 0:
            route = json.loads(result.stdout)
            output = (
                self.artifact / ".runtime" / "routes" /
                f"{route['route_id']}.json"
            )
        return result, output

    def wrapper_command(self, adapter, intensity, jobs, logs, *extra):
        explicit_model = {
            "claude": ["--model", "claude-test", "--effort", "low"],
            "codex": ["--model", "gpt-test", "--reasoning", "low"],
            "opencode": ["--model", "provider/test", "--variant", "low"],
        }[adapter]
        # A route-bound registration must present the profile the route actually
        # sealed. Pinning a literal here silently drifts the moment the portable
        # owner policy changes — a quick owner is `balanced-deep`, so a hardcoded
        # `light` made the guard reject its own fixture.
        model = explicit_model
        if "--route-file" in extra:
            options = dict(zip(extra[::2], extra[1::2]))
            sealed = json.loads(
                Path(options["--route-file"]).read_text(encoding="utf-8")
            )
            node = next(
                item for item in sealed["nodes"]
                if item["id"] == options["--route-node"]
            )
            model = ["--model-profile", node["model_profile"]]
        return [
            sys.executable,
            str(ROOT / f"adapters/{adapter}/bin/dispatch-headless.py"),
            "--register",
            "--worktree", str(self.repo),
            "--slug", f"{adapter}-{intensity}",
            "--capability", "autopilot-code",
            "--capability-mode", "dev",
            "--worker-type", "owner",
            "--unit", "_kernel/owner",
            "--assigned-contract", "autopilot-code",
            "--intensity", intensity,
            "--jobs", str(jobs),
            "--log-dir", str(logs),
            *model,
            *extra,
        ]

    def wrapper_env(self):
        env = {
            key: value for key, value in os.environ.items()
            if key != "AGENT_DISPATCH_JOBS"
        }
        env.update(
            AGENT_HOME=str(ROOT),
            AGENT_ARTIFACT_ROOT=str(self.artifact),
            OPENCODE_CONFIG_CONTENT="{}",
        )
        return env

    @staticmethod
    def candidate(**overrides):
        row = {
            "harness": "codex",
            "transport": "headless",
            "surface": "registered-headless",
            "status": "supported",
            "probe_source": "fixture",
            "probe_time": "2026-07-21T00:00:00Z",
        }
        row.update(overrides)
        return row

    def test_invalid_quick_evidence_writes_no_route(self):
        invalid = {
            "empty": {"candidates": []},
            "unknown": {"candidates": [self.candidate(status="unknown")]},
            "interactive": {
                "candidates": [self.candidate(transport="interactive")]
            },
            "native": {
                "candidates": [
                    self.candidate(surface="codex-native-subagent")
                ]
            },
            "inline": {
                "candidates": [self.candidate(surface="inline")]
            },
            "unknown-harness": {
                "candidates": [self.candidate(harness="mystery")]
            },
        }
        for name, evidence in invalid.items():
            with self.subTest(name=name):
                result, output = self.compile_quick(evidence)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("quick-headless-unavailable", result.stderr)
                self.assertFalse(output.exists())

    def test_supported_quick_is_exact_registered_owner(self):
        result, output = self.compile_quick({
            "candidates": [self.candidate()]
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        route = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(route["owner_dispatch_depth"], 1)
        self.assertEqual(route["max_dispatch_depth"], 1)
        self.assertEqual(len(route["nodes"]), 1)
        node = route["nodes"][0]
        self.assertEqual(node["dispatch_depth"], 1)
        self.assertEqual(node["unit"], "_kernel/owner")
        self.assertEqual(node["execution_surface"], "registered-headless")
        self.assertIs(node["registered_worker"], True)
        self.assertNotIn("fallback_hops", node)

    def test_valid_quick_route_registers_exact_axes_on_every_adapter(self):
        evidence = {
            "candidates": [
                self.candidate(harness=adapter) for adapter in ADAPTERS
            ]
        }
        result, output = self.compile_quick(evidence)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        route = json.loads(output.read_text(encoding="utf-8"))
        node = route["nodes"][0]
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter):
                jobs = self.base / f"{adapter}.quick.jobs.log"
                logs = self.base / f"{adapter}.quick.logs"
                command = self.wrapper_command(
                    adapter, "quick", jobs, logs,
                    "--route-file", str(output),
                    "--route-id", route["route_id"],
                    "--route-hash", route["route_hash"],
                    "--route-node", "one-shot",
                    "--registry-digest", route["registry_digest"],
                    "--write-scope", ";".join(node["write_scope"]),
                    "--attempt-id", f"att-{adapter}-quick001",
                )
                registered = subprocess.run(
                    command, text=True, capture_output=True,
                    env=self.wrapper_env(),
                )
                self.assertEqual(
                    registered.returncode, 0,
                    registered.stdout + registered.stderr,
                )
                fields = jobs.read_text(encoding="utf-8").split("\t")
                metadata = dict(
                    item.split("=", 1)
                    for item in fields[5].split(",") if "=" in item
                )
                self.assertEqual(metadata["attempt_schema_version"], "2")
                self.assertEqual(metadata["capability_mode"], "dev")
                self.assertNotIn("worker_mode", metadata)
                self.assertNotIn("mode", metadata)
                self.assertEqual(metadata["dispatch_depth"], "1")
                self.assertEqual(metadata["transport"], "headless")
                self.assertEqual(
                    metadata["execution_surface"], "registered-headless"
                )
                self.assertEqual(metadata["registered_worker"], "1")
                self.assertEqual(
                    metadata["model_profile"], node["model_profile"]
                )
                self.assertEqual(node["model_profile"], "balanced-deep")
                self.assertEqual(
                    metadata["fallback_hop"], "same-harness-headless"
                )

    def test_quick_is_serial_and_exhausts_checked_candidate_budget(self):
        result, output = self.compile_quick({
            "candidates": [self.candidate(harness="codex")]
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        route = json.loads(output.read_text(encoding="utf-8"))
        node = route["nodes"][0]
        jobs = self.base / "serial.jobs.log"
        logs = self.base / "serial.logs"

        def register(attempt):
            return subprocess.run(
                self.wrapper_command(
                    "codex", "quick", jobs, logs,
                    "--route-file", str(output),
                    "--route-id", route["route_id"],
                    "--route-hash", route["route_hash"],
                    "--route-node", "one-shot",
                    "--registry-digest", route["registry_digest"],
                    "--write-scope", ";".join(node["write_scope"]),
                    "--attempt-id", attempt,
                ),
                text=True, capture_output=True, env=self.wrapper_env(),
            )

        first = register("att-quick-serial01")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        concurrent = register("att-quick-serial02")
        self.assertEqual(concurrent.returncode, 0, concurrent.stdout + concurrent.stderr)
        self.assertIn("registered=0", concurrent.stdout)
        self.assertEqual(len(jobs.read_text().splitlines()), 1)

        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_contract
        self.assertTrue(dispatch_contract.close_attempt_row(
            jobs, "att-quick-serial01", "dead-fixture"
        ))
        exhausted = register("att-quick-serial03")
        self.assertNotEqual(exhausted.returncode, 0)
        self.assertIn("reason=quick-registered-headless-exhausted", exhausted.stdout)
        self.assertIn("child_spawned=0", exhausted.stdout)
        self.assertEqual(len(jobs.read_text().splitlines()), 1)

    def test_direct_and_route_unbound_quick_never_register(self):
        for adapter in ADAPTERS:
            for intensity, reason in (
                ("direct", "direct-main-inline-only"),
                ("quick", "quick-headless-unavailable"),
            ):
                with self.subTest(adapter=adapter, intensity=intensity):
                    jobs = self.base / f"{adapter}.{intensity}.jobs.log"
                    logs = self.base / f"{adapter}.{intensity}.logs"
                    result = subprocess.run(
                        self.wrapper_command(
                            adapter, intensity, jobs, logs,
                            "--attempt-id", f"att-{adapter}-{intensity}001",
                        ),
                        text=True, capture_output=True,
                        env=self.wrapper_env(),
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"reason={reason}", result.stdout)
                    self.assertIn("child_spawned=0", result.stdout)
                    self.assertFalse(jobs.exists())
                    self.assertFalse(logs.exists())

    def test_wrapper_rejects_legacy_depth_flag_without_emission(self):
        model = {
            "claude": ["--model", "claude-test", "--effort", "low"],
            "codex": ["--model", "gpt-test", "--reasoning", "low"],
            "opencode": ["--model", "provider/test", "--variant", "low"],
        }
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter):
                jobs = self.base / f"{adapter}.jobs.log"
                logs = self.base / f"{adapter}.logs"
                command = [
                    sys.executable,
                    str(ROOT / f"adapters/{adapter}/bin/dispatch-headless.py"),
                    "--register",
                    "--worktree", str(self.repo),
                    "--slug", f"{adapter}-legacy-depth",
                    "--capability", "autopilot-code",
                    "--mode", "dev/refactor",
                    "--intensity", "standard",
                    "--depth", "1",
                    "--jobs", str(jobs),
                    "--log-dir", str(logs),
                    *model[adapter],
                ]
                env = {
                    key: value for key, value in os.environ.items()
                    if key != "AGENT_DISPATCH_JOBS"
                }
                env.update(
                    AGENT_HOME=str(ROOT),
                    AGENT_ARTIFACT_ROOT=str(self.artifact),
                    OPENCODE_CONFIG_CONTENT="{}",
                )
                result = subprocess.run(
                    command, text=True, capture_output=True, env=env
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unrecognized arguments: --depth", result.stderr)
                self.assertFalse(jobs.exists())
                self.assertFalse(logs.exists())

    def test_active_contract_prose_uses_qualified_dispatch_depth(self):
        active = {
            ROOT / "core/WORKFLOW.md",
            ROOT / "core/CONVENTIONS.md",
            ROOT / "core/OPERATIONS.md",
            ROOT / "core/ADAPTATION.md",
            ROOT / "core/DESIGN_PRINCIPLES.md",
            ROOT / "core/HOOKS.md",
            ROOT / "adapters/claude/CLAUDE.md",
            ROOT / "adapters/codex/AGENTS.md",
            ROOT / "adapters/opencode/AGENTS.md",
            ROOT / "utilities/dispatch-node.py",
            ROOT / "utilities/dispatch-registry.py",
            ROOT / "utilities/capability-route.py",
            ROOT / "tools/sync-entry-skill-layer.py",
            ROOT / "tools/fleet/collectors/dispatch.py",
            ROOT / "tools/fleet/control.py",
            ROOT / "tools/fleet/model.py",
            ROOT / "tools/fleet/render.py",
            ROOT / "tools/fleet/route.py",
            ROOT / "hooks/stage-dispatch-reminder.sh",
        }
        for pattern in (
            "capabilities/*.md",
            "roles/**/*.md",
            "skills/**/*.md",
            "adapters/claude/skills/**/*.md",
            "adapters/codex/skills/**/*.md",
            "adapters/opencode/skills/**/*.md",
        ):
            active.update(ROOT.glob(pattern))
        for adapter in ADAPTERS:
            adapter_root = ROOT / "adapters" / adapter
            for name in ("README.md", "ADAPTATION.md", "AGENTS.md", "CLAUDE.md"):
                path = adapter_root / name
                if path.is_file():
                    active.add(path)
            for name in (
                "dispatch-headless.py",
                "dispatch-liveness.py",
                "preflight.sh",
                "capability-map.sh",
            ):
                path = adapter_root / "bin" / name
                if path.is_file():
                    active.add(path)
        import re
        bare = re.compile(
            r"(?<!dispatch-)\bdepth-[0-3]\b|"
            r"(?<!dispatch-)(?<!dispatch )\bdepth [0-3]\b",
            re.I,
        )
        offenders = []
        for path in sorted(active):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if bare.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}:{line}")
        self.assertEqual(offenders, [])

        # Protected neighboring vocabularies are deliberately not rewritten.
        operations = (ROOT / "core/OPERATIONS.md").read_text(encoding="utf-8")
        self.assertIn("agents.max_depth", operations)
        render = (ROOT / "tools/fleet/render.py").read_text(encoding="utf-8")
        self.assertIn("def _subagent_strip(subs, depth=0)", render)

    def test_standard_pipeline_and_drills_pin_exact_current_attempt_contract(self):
        pipeline = (
            ROOT / "skills/autopilot-code/references/dev-pipeline.md"
        ).read_text(encoding="utf-8")
        for required in (
            "utilities/dispatch-node.py",
            "utilities/dispatch-batch.py",
            '--route "$ROUTE_FILE"',
            '--node "$NODE_ID"',
            '-- --jobs "$CANONICAL_JOBS"',
            '--jobs "$CANONICAL_JOBS" --prompt-text "$STAGE_PROMPT"',
            "ATTEMPT_ID=",
            "--attempt-id <exact-attempt-id>",
        ):
            self.assertIn(required, pipeline)
        self.assertNotIn(
            "adapters/claude/bin/dispatch-headless.py --start",
            pipeline,
        )

        reminder = (
            ROOT / "hooks/stage-dispatch-reminder.sh"
        ).read_text(encoding="utf-8")
        for required in (
            "dispatch-node.py --route <route-file>",
            "--jobs <canonical-jobs.log>",
            "attempt_id",
        ):
            self.assertIn(required, reminder)

        runner = (ROOT / "loops/lib-runner.sh").read_text(encoding="utf-8")
        self.assertIn("worker_type=owner,assigned_contract=drill", runner)
        self.assertNotIn("worker_role=", runner)

        g9 = (
            ROOT / "loops/drill/cases_growing/g9_cross_harness_depth2_dispatch/assert.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("SUPPORTED_BATCH_HARNESSES", g9)
        self.assertIn("model_worker_governor.test.py", g9)
        self.assertIn("concurrent_launch", g9)

        g10 = (
            ROOT / "loops/drill/cases_growing/g10_claude_opencode_depth2_start/assert.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("live-parent-not-found", g10)
        self.assertIn("child_spawned=0", g10)
        self.assertIn("zero registry/governor/runtime/prompt/log/Fleet child", g10)

        g11 = (
            ROOT / "loops/drill/cases_growing/g11_nested_sandbox_lifetime/assert.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("attempt_id", g11)
        self.assertIn("fallback_hop", g11)


if __name__ == "__main__":
    unittest.main()
