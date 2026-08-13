#!/usr/bin/env python3
"""SD-56 fixtures: completion marker canonical write + start-time gate."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("route", ROOT / "utilities/capability-route.py")
ROUTE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ROUTE)
WRAPPER_PARENT_SANDBOXES = ROUTE.WRAPPER_PARENT_SANDBOXES

ADAPTERS = {
    "codex": ([sys.executable, str(ROOT / "adapters/codex/bin/dispatch-headless.py")], ["--model", "gpt-test", "--reasoning", "low"]),
    "claude": ([sys.executable, str(ROOT / "adapters/claude/bin/dispatch-headless.py")], ["--model", "claude-test", "--effort", "low"]),
    "opencode": ([sys.executable, str(ROOT / "adapters/opencode/bin/dispatch-headless.py")], ["--model", "provider/test", "--variant", "low"]),
}


class CompletionMarkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Fixture"], check=True)
        (self.repo / "x").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "x"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        self.artifact = self.base / ".agent_reports"
        self.artifact.mkdir()
        self.agent_home = self.base / "agent-home"
        (self.agent_home / "core").mkdir(parents=True)
        (self.agent_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self.jobs = self.base / "jobs.log"
        self.logs = self.base / "logs"

    def tearDown(self):
        self.temp.cleanup()

    def compile_route(self):
        rows = [
            {
                "parent_harness": harness,
                "parent_transport": "headless",
                "parent_sandbox": WRAPPER_PARENT_SANDBOXES[harness][0],
                "child_harness": harness,
                "launch_authority": "conductor",
                "status": "supported",
                "probe_source": f"{harness}-fixture",
                "probe_time": "2026-07-16T00:00:00Z",
                "failure_class": "",
                "checked_worktree": str(self.repo.resolve()),
                "failure_scope": "none",
                "codex_command": "ok" if harness == "codex" else "not-applicable",
                "retry_on_isolated_worktree": 0,
            }
            for harness in ADAPTERS
        ]
        evidence = {"tuples": rows, "native_subagent": []}
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec",
            "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        route = ROUTE.compile_route(
            "autopilot-code", "dev", "strong", self.repo, self.artifact,
            signals=["shared-contract"], transport="headless", tracking="tracked",
            tracked_gate_evidence=gate, dispatch_evidence=evidence,
        )
        self.current_route = route
        return route

    def as_v2(self, route):
        # Hand-forced historical v2 shape for the read-only compatibility
        # boundary. New register/start operations must reject it.
        forced = copy.deepcopy(route)
        forced.pop("dispatch_contract_version", None)
        forced["broker_contract_version"] = 2
        for row in forced.get("dispatch_evidence", {}).get("tuples", []):
            row["launch_authority"] = "ancestor-broker"
            row["broker_root"] = str(self.base / "broker")
            row.pop("broker_instance", None)
        for node in forced.get("nodes", []):
            for hop in node.get("dispatch_fallback", []):
                for candidate in hop.get("candidates", []):
                    candidate["launch_authority"] = "ancestor-broker"
                    candidate["broker_root"] = str(self.base / "broker")
                    candidate.pop("broker_instance", None)
        forced["route_hash"] = ROUTE.route_hash(forced)
        forced["route_id"] = "rt-" + forced["route_hash"].split(":", 1)[1][:16]
        return forced

    def as_v1(self, route):
        forced = self.as_v2(route)
        forced["broker_contract_version"] = 1
        for row in forced.get("dispatch_evidence", {}).get("tuples", []):
            if row.get("launch_authority") == "ancestor-broker":
                row["broker_instance"] = "brk-" + "f" * 32
        for node in forced.get("nodes", []):
            for hop in node.get("dispatch_fallback", []):
                for candidate in hop.get("candidates", []):
                    if candidate.get("launch_authority") == "ancestor-broker":
                        candidate["broker_instance"] = "brk-" + "f" * 32
        forced["route_hash"] = ROUTE.route_hash(forced)
        forced["route_id"] = "rt-" + forced["route_hash"].split(":", 1)[1][:16]
        return forced

    def write_route(self, route, name="route.json"):
        path = self.base / name
        path.write_text(json.dumps(route), encoding="utf-8")
        return path

    def base_env(self):
        # completion_dir() resolves the dispatch state root ahead of
        # AGENT_HOME/.dispatch (I-2 unification), preferring an inherited
        # AGENT_DISPATCH_JOBS -- clear it so the invoking shell's real
        # registry never leaks into this fixture's agent-home-relative marker
        # expectations.
        env = {
            **os.environ,
            "AGENT_HOME": str(self.agent_home),
            "AGENT_ARTIFACT_ROOT": str(self.artifact),
            "OPENCODE_CONFIG_CONTENT": "{}",
        }
        env.pop("AGENT_DISPATCH_JOBS", None)
        # A depth-1 owner session that launched this test process exports
        # AGENT_OWNER_ROUTE_FILE/ID/HASH; inherited verbatim, the wrapper
        # child reads it as a real owner binding and verify_route() fails
        # closed on the mismatched cwd before the completion-marker gate is
        # even reached (review Q-4) -- a fixture isolation gap, not a bug in
        # the wrapper.
        env.pop("AGENT_OWNER_ROUTE_FILE", None)
        env.pop("AGENT_OWNER_ROUTE_ID", None)
        env.pop("AGENT_OWNER_ROUTE_HASH", None)
        return env

    def wrapper_command(self, harness, action, route_path, route, node_id):
        wrapper, _ = ADAPTERS[harness]
        node = next(n for n in route["nodes"] if n["id"] == node_id)
        return wrapper + [
            f"--{action}", "--worktree", str(self.repo), "--slug", f"{harness}-{node_id}",
            "--capability", "autopilot-code", "--capability-mode", route["capability_mode"],
            "--worker-mode", node["unit"],
            "--intensity", route["effective_intensity"], "--dispatch-depth", "2", "--parent", "owner",
            "--worker-role", "code-" + node_id, "--owner", "autopilot-code",
            "--jobs", str(self.jobs), "--log-dir", str(self.logs),
            "--parent-harness", harness, "--parent-transport", "headless", "--parent-sandbox", "fixture",
            "--launch-authority", "conductor", "--nested-eligibility", "supported",
            "--eligibility-source", f"{harness}-fixture", "--fallback-ordinal", "1",
            "--route-file", str(route_path), "--route-id", route["route_id"],
            "--route-hash", route["route_hash"], "--route-node", node_id,
            "--registry-digest", route["registry_digest"],
            "--write-scope", ";".join(node["write_scope"]),
            "--unit", node.get("unit", ""),
            "--model-role", node["role"],
            "--model-profile", node["model_profile"],
        ]

    def complete(self, route_path, node_id, evidence_path, jobs=None, attempt_id=None, attempt_axes=None):
        if jobs is None and attempt_id is None:
            attempt_id = f"att-inline-{node_id}-fixture"
            attempt_axes = {
                "dispatch_depth": 2,
                "transport": "interactive",
                "execution_surface": "inline",
                "registered_worker": "0",
                "fallback_hop": "inline",
            }
        command = [sys.executable, str(ROOT / "utilities/capability-route.py"), "complete",
                   "--route", str(route_path), "--node", node_id, "--evidence", str(evidence_path)]
        if jobs is not None: command += ["--jobs", str(jobs)]
        if attempt_id is not None: command += ["--attempt-id", attempt_id]
        if attempt_axes is not None:
            command += [
                "--dispatch-depth", str(attempt_axes["dispatch_depth"]),
                "--transport", attempt_axes["transport"],
                "--execution-surface", attempt_axes["execution_surface"],
                "--registered-worker", str(attempt_axes["registered_worker"]),
                "--fallback-hop", attempt_axes["fallback_hop"],
            ]
        return subprocess.run(command, text=True, capture_output=True, env=self.base_env())

    def registered_axes(self):
        return {
            "dispatch_depth": 2,
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": "1",
            "fallback_hop": "same-harness-headless",
        }

    # fixture 6 -----------------------------------------------------------
    def test_complete_writes_canonical_marker(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        result = self.complete(route_path, "plan", evidence)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        canonical = self.agent_home / ".dispatch" / "completion" / route["route_id"] / "plan.json"
        self.assertTrue(canonical.is_file())
        marker = json.loads(canonical.read_text(encoding="utf-8"))
        self.assertEqual(marker["route_id"], route["route_id"])
        self.assertEqual(marker["route_hash"], route["route_hash"])
        self.assertEqual(marker["registry_digest"], route["registry_digest"])
        self.assertEqual(marker["node_id"], "plan")
        self.assertEqual(marker["completion_gate"], "code-plan")
        import hashlib
        self.assertEqual(marker["evidence"]["sha256"], hashlib.sha256(evidence.read_bytes()).hexdigest())

    # fixture 7 -------------------------------------------------------------
    def test_start_without_dependency_marker_fails_closed(self):
        route = self.compile_route()
        route_path = self.write_route(route, "route-v3.json")
        for harness in ADAPTERS:
            with self.subTest(harness=harness):
                command = self.wrapper_command(harness, "start", route_path, route, "execute")
                result = subprocess.run(command, text=True, capture_output=True, env=self.base_env())
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("reason=completion-marker-missing", result.stdout)
                self.assertIn("child_spawned=0", result.stdout)
                self.assertFalse(self.jobs.exists())

        # Write the markers this route's "execute" node depends on (plan and
        # the hoisted plan-check review sibling), then re-run: the gate itself
        # must no longer be the blocker (other reasons -- e.g. missing real
        # claude/codex/opencode binaries -- are acceptable).
        for node_id, body in (("plan", "plan body\n"), ("plan-check", "plan review verdict\n")):
            evidence = self.base / f"{node_id}.md"
            evidence.write_text(body, encoding="utf-8")
            completed = self.complete(route_path, node_id, evidence)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for harness in ADAPTERS:
            with self.subTest(harness=harness, phase="after-marker"):
                command = self.wrapper_command(harness, "start", route_path, route, "execute")
                result = subprocess.run(command, text=True, capture_output=True, env=self.base_env())
                self.assertNotIn("reason=completion-marker-missing", result.stdout)

    # fixture 8 -------------------------------------------------------------
    def test_marker_absence_is_not_a_failure(self):
        # (a) Historical v1/v2 records remain inspectable, but may not create
        # new registry rows or children after broker retirement.
        for version, legacy_route in ((1, self.as_v1(self.compile_route())), (2, self.as_v2(self.compile_route()))):
            self.assertEqual(legacy_route.get("broker_contract_version"), version)
            legacy_path = self.write_route(legacy_route, f"route-v{version}.json")
            for action in ("register", "start"):
                for harness in ADAPTERS:
                    with self.subTest(harness=harness, phase=f"v{version}-{action}"):
                        command = self.wrapper_command(harness, action, legacy_path, legacy_route, "execute")
                        result = subprocess.run(command, text=True, capture_output=True, env=self.base_env())
                        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn("legacy-broker-route-read-only", result.stdout + result.stderr)
                        self.assertNotIn("reason=completion-marker-missing", result.stdout)

        # (b) Record-unbound --start (no --route-file at all) -> the route
        # completion-marker gate does not apply.
        # not fire either (no route to evaluate depends_on against).
        for harness in ADAPTERS:
            with self.subTest(harness=harness, phase="unbound"):
                wrapper, model = ADAPTERS[harness]
                command = wrapper + [
                    "--start", "--worktree", str(self.repo), "--slug", f"{harness}-unbound",
                    "--capability", "autopilot-code", "--capability-mode", "dev",
                    "--worker-mode", "dev/backend", "--unit", "dev/backend",
                    "--intensity", "standard", "--dispatch-depth", "2", "--parent", "owner",
                    "--worker-role", "code-execute", "--owner", "autopilot-code",
                    "--jobs", str(self.jobs), "--log-dir", str(self.logs),
                    "--parent-harness", harness, "--parent-transport", "headless", "--parent-sandbox", "fixture",
                    "--launch-authority", "conductor", "--nested-eligibility", "supported",
                    "--eligibility-source", f"{harness}-fixture", "--fallback-ordinal", "1",
                ] + model
                result = subprocess.run(command, text=True, capture_output=True, env=self.base_env())
                self.assertNotIn("reason=completion-marker-missing", result.stdout)

        # (c) static guardian: nothing outside the gate helper itself and the
        # adapters' generic `fail(e.reason, ...)` relay maps marker absence
        # to a failure string.
        offenders = []
        search_roots = [ROOT / "utilities", ROOT / "adapters", ROOT / "tools" / "fleet"]
        allow = {
            (ROOT / "utilities" / "dispatch_contract.py").resolve(),
            (ROOT / "utilities" / "dispatch_completion_marker.test.py").resolve(),
            (ROOT / "utilities" / "dispatch_state_root_rotation.test.py").resolve(),
        }
        for adapter in ("claude", "codex", "opencode"):
            allow.add((ROOT / "adapters" / adapter / "bin" / "dispatch-headless.py").resolve())
        for search_root in search_roots:
            if not search_root.is_dir():
                continue
            for path in search_root.rglob("*.py"):
                if path.resolve() in allow:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if "completion-marker-missing" in text:
                    offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_dependency_gate_rejects_schema_less_or_unlinked_marker(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        completed = self.complete(route_path, "plan", evidence)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        canonical = directory / "plan.json"
        marker = json.loads(canonical.read_text(encoding="utf-8"))
        marker.pop("schema_version")
        canonical.write_text(json.dumps(marker), encoding="utf-8")
        result = subprocess.run(
            self.wrapper_command("codex", "start", route_path, route, "execute"),
            text=True, capture_output=True, env=self.base_env(),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reason=completion-marker-missing", result.stdout)

    # fixture 9 ---------------------------------------------------------
    def test_reharvest_preserves_history_and_latest_is_authoritative(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("v1\n", encoding="utf-8")
        first = self.complete(route_path, "plan", evidence)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        history_1 = directory / "plan.1.json"
        canonical = directory / "plan.json"
        self.assertTrue(history_1.is_file())
        first_marker = json.loads(canonical.read_text(encoding="utf-8"))
        self.assertEqual(first_marker["sequence"], 1)

        # same evidence again -> no-op (no new history file).
        second = self.complete(route_path, "plan", evidence)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        history_2 = directory / "plan.2.json"
        self.assertFalse(history_2.is_file())

        # changed evidence -> new history entry, old one untouched, canonical
        # points at the latest.
        evidence.write_text("v2\n", encoding="utf-8")
        third = self.complete(
            route_path, "plan", evidence,
            attempt_id="att-inline-plan-retry",
            attempt_axes={
                "dispatch_depth": 2,
                "transport": "interactive",
                "execution_surface": "inline",
                "registered_worker": "0",
                "fallback_hop": "inline",
            },
        )
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        self.assertTrue(history_2.is_file())
        self.assertEqual(json.loads(history_1.read_text(encoding="utf-8")), first_marker)
        latest = json.loads(canonical.read_text(encoding="utf-8"))
        self.assertEqual(latest["sequence"], 2)
        import hashlib
        self.assertEqual(latest["evidence"]["sha256"], hashlib.sha256(evidence.read_bytes()).hexdigest())

    def test_same_attempt_changed_evidence_fails_before_canonical_mutation(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("first\n", encoding="utf-8")
        first = self.complete(route_path, "plan", evidence)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        canonical = directory / "plan.json"
        before = canonical.read_bytes()

        evidence.write_text("forged retry\n", encoding="utf-8")
        changed = self.complete(route_path, "plan", evidence)
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("immutable attempt completion differs", changed.stderr)
        self.assertEqual(canonical.read_bytes(), before)
        self.assertFalse((directory / "plan.2.json").exists())

    def test_same_evidence_registered_then_inline_creates_new_history(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("same evidence\n", encoding="utf-8")
        self.write_row("open", "registered", "att-registered-first")
        registered = self.complete(
            route_path, "plan", evidence,
            jobs=self.jobs, attempt_id="att-registered-first",
        )
        self.assertEqual(registered.returncode, 0, registered.stdout + registered.stderr)
        inline = self.complete(
            route_path, "plan", evidence,
            attempt_id="att-inline-second",
            attempt_axes={
                "dispatch_depth": 2,
                "transport": "interactive",
                "execution_surface": "inline",
                "registered_worker": "0",
                "fallback_hop": "inline",
            },
        )
        self.assertEqual(inline.returncode, 0, inline.stdout + inline.stderr)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        first = json.loads((directory / "plan.1.json").read_text())
        second = json.loads((directory / "plan.2.json").read_text())
        latest = json.loads((directory / "plan.json").read_text())
        self.assertEqual(first["attempt_id"], "att-registered-first")
        self.assertEqual(first["execution_surface"], "registered-headless")
        self.assertEqual(second["attempt_id"], "att-inline-second")
        self.assertEqual(second["execution_surface"], "inline")
        self.assertEqual(latest, second)

    def test_same_evidence_inline_then_registered_creates_new_history(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "execute.md"
        evidence.write_text("same evidence\n", encoding="utf-8")
        inline = self.complete(
            route_path, "execute", evidence,
            attempt_id="att-inline-first",
            attempt_axes={
                "dispatch_depth": 2,
                "transport": "interactive",
                "execution_surface": "inline",
                "registered_worker": "0",
                "fallback_hop": "inline",
            },
        )
        self.assertEqual(inline.returncode, 0, inline.stdout + inline.stderr)
        self.write_row("open", "registered", "att-registered-second", node_id="execute")
        registered = self.complete(
            route_path, "execute", evidence,
            jobs=self.jobs, attempt_id="att-registered-second",
        )
        self.assertEqual(registered.returncode, 0, registered.stdout + registered.stderr)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        first = json.loads((directory / "execute.1.json").read_text())
        second = json.loads((directory / "execute.2.json").read_text())
        self.assertEqual(first["execution_surface"], "inline")
        self.assertEqual(second["execution_surface"], "registered-headless")
        self.assertEqual(
            json.loads((directory / "execute.json").read_text()), second
        )

    def test_same_evidence_different_native_surfaces_create_new_history(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "test.md"
        evidence.write_text("same evidence\n", encoding="utf-8")
        axes = {
            "dispatch_depth": 2,
            "transport": "headless",
            "registered_worker": "0",
            "fallback_hop": "native-subagent",
        }
        codex = self.complete(
            route_path, "test", evidence,
            attempt_id="att-codex-native",
            attempt_axes={
                **axes, "execution_surface": "codex-native-subagent"
            },
        )
        self.assertEqual(codex.returncode, 0, codex.stdout + codex.stderr)
        claude = self.complete(
            route_path, "test", evidence,
            attempt_id="att-claude-native",
            attempt_axes={
                **axes, "execution_surface": "claude-subagent"
            },
        )
        self.assertEqual(claude.returncode, 0, claude.stdout + claude.stderr)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        first = json.loads((directory / "test.1.json").read_text())
        second = json.loads((directory / "test.2.json").read_text())
        self.assertEqual(first["execution_surface"], "codex-native-subagent")
        self.assertEqual(second["execution_surface"], "claude-subagent")
        self.assertEqual(json.loads((directory / "test.json").read_text()), second)


    # SD-70 fixtures -------------------------------------------------------
    def write_row(self, status, slug, attempt_id, extra="", node_id="plan"):
        contract = (
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless"
        )
        line = (
            f"2026-07-19T00:00:00Z\t{status}\t{self.repo}\t{self.repo}\t{slug}\t"
            f"attempt_id={attempt_id},{contract},route_id={self.current_route['route_id']},"
            f"route_hash={self.current_route['route_hash']},"
            f"registry_digest={self.current_route['registry_digest']},route_node={node_id},"
            f"completion_gate={next(node['completion_gate'] for node in self.current_route['nodes'] if node['id'] == node_id)}"
        )
        if extra: line += "," + extra
        with self.jobs.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_row(self, attempt_id):
        for line in self.jobs.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            meta = dict(p.split("=", 1) for p in fields[5].split(",") if "=" in p)
            if meta.get("attempt_id") == attempt_id:
                return fields[1], meta
        return None, None

    def test_complete_with_attempt_closes_only_current_row(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        self.write_row("done", "prior-blocked", "att-prior", "note=blocked")
        self.write_row("open", "current", "att-current")
        self.write_row("open", "live-retry", "att-retry")
        result = self.complete(route_path, "plan", evidence, jobs=self.jobs, attempt_id="att-current")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status, meta = self.read_row("att-current")
        self.assertEqual(status, "done")
        self.assertEqual(meta.get("note"), "completed-marker")
        status, meta = self.read_row("att-prior")
        self.assertEqual(status, "done"); self.assertEqual(meta.get("note"), "blocked")
        status, _ = self.read_row("att-retry")
        self.assertEqual(status, "open")

    def test_complete_duplicate_same_attempt_is_idempotent(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        self.write_row("open", "current", "att-dup")
        first = self.complete(route_path, "plan", evidence, jobs=self.jobs, attempt_id="att-dup")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.complete(route_path, "plan", evidence, jobs=self.jobs, attempt_id="att-dup")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        rows = [line for line in self.jobs.read_text(encoding="utf-8").splitlines() if "att-dup" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("\tdone\t", rows[0])

    def test_noncompletion_terminal_row_is_rejected_before_marker_write(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "blocked.md"
        evidence.write_text("must not publish\n", encoding="utf-8")
        self.write_row("done", "blocked", "att-blocked-target", "note=dead-test")
        result = self.complete(
            route_path, "plan", evidence,
            jobs=self.jobs, attempt_id="att-blocked-target",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attempt-row-terminal-without-completion", result.stderr)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        self.assertFalse((directory / "plan.json").exists())
        self.assertFalse((directory / "plan.1.json").exists())

    def test_concurrent_completions_serialize_history_and_canonical_sequence(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        attempts = ("att-concurrent-a", "att-concurrent-b")
        evidence_paths = []
        for index, attempt in enumerate(attempts):
            self.write_row("open", f"worker-{index}", attempt)
            evidence = self.base / f"concurrent-{index}.md"
            evidence.write_text(f"evidence {index}\n", encoding="utf-8")
            evidence_paths.append(evidence)
        processes = []
        for attempt, evidence in zip(attempts, evidence_paths):
            command = [
                sys.executable, str(ROOT / "utilities/capability-route.py"), "complete",
                "--route", str(route_path), "--node", "plan",
                "--evidence", str(evidence), "--jobs", str(self.jobs),
                "--attempt-id", attempt,
            ]
            processes.append(subprocess.Popen(
                command, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self.base_env(),
            ))
        results = [process.communicate(timeout=20) + (process.returncode,) for process in processes]
        self.assertTrue(all(code == 0 for _, _, code in results), results)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        first = json.loads((directory / "plan.1.json").read_text())
        second = json.loads((directory / "plan.2.json").read_text())
        canonical = json.loads((directory / "plan.json").read_text())
        self.assertEqual({first["attempt_id"], second["attempt_id"]}, set(attempts))
        self.assertEqual(canonical["sequence"], 2)
        self.assertEqual(canonical, second)
        self.assertFalse((directory / "plan.3.json").exists())

    def test_complete_attempt_mismatch_fails_closed_marker_preserved(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        # no row for this attempt id at all
        result = self.complete(
            route_path, "plan", evidence, jobs=self.jobs, attempt_id="att-missing",
            attempt_axes=self.registered_axes(),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attempt-row-absent", result.stdout + result.stderr)
        canonical = self.agent_home / ".dispatch" / "completion" / route["route_id"] / "plan.json"
        self.assertTrue(canonical.is_file(), "marker must be preserved even when the row close fails")

    def test_complete_unwritable_jobs_marker_preserved_then_reconcile_repairs(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        unwritable_dir = self.base / "readonly"
        unwritable_dir.mkdir(mode=0o500)
        unwritable_jobs = unwritable_dir / "jobs.log"
        try:
            result = self.complete(
                route_path, "plan", evidence, jobs=unwritable_jobs,
                attempt_id="att-unwritable", attempt_axes=self.registered_axes(),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("row-close-failed", result.stdout + result.stderr)
            canonical = self.agent_home / ".dispatch" / "completion" / route["route_id"] / "plan.json"
            self.assertTrue(canonical.is_file())
        finally:
            unwritable_dir.chmod(0o700)

        # Now simulate the same exact attempt landing in the real registry
        # (as if the launcher retried the write) and confirm reconcile
        # repairs exactly that stale marker-backed row, never breadth-closing.
        dead_pid = "pid=999999999,pid_start=123456"
        linked = f"{dead_pid},route_id={route['route_id']},route_node=plan"
        self.write_row("open", "current", "att-unwritable", extra=linked)
        self.write_row("open", "unrelated", "att-unrelated", extra=dead_pid)
        registry_spec = importlib.util.spec_from_file_location(
            "dispatch_registry", ROOT / "utilities/dispatch-registry.py")
        registry = importlib.util.module_from_spec(registry_spec)
        registry_spec.loader.exec_module(registry)
        rows = registry.read_rows(self.jobs)

        class Args:
            pass
        args = Args()
        args.agent_home = self.agent_home
        args.jobs = self.jobs
        args.now = 0.0
        newest = {}
        for row in rows:
            key = (row["meta"].get("route_id"), row["meta"].get("route_node"))
            if all(key): newest[key] = row["order"]
        current_row = next(r for r in rows if r["meta"].get("attempt_id") == "att-unwritable")
        category, reason, note = registry.classify(current_row, args, newest, rows)
        self.assertEqual(note, "completed-marker")
        self.assertEqual(category, "marker-backed-stale")
        # The unrelated dead attempt has no marker linkage, so it still
        # falls through to the pre-existing generic dead-exact-pid path
        # rather than being folded into the SD-70 completed-marker repair.
        unrelated_row = next(r for r in rows if r["meta"].get("attempt_id") == "att-unrelated")
        _, _, unrelated_note = registry.classify(unrelated_row, args, newest, rows)
        self.assertEqual(unrelated_note, "dead-exact-pid")
        self.assertNotEqual(unrelated_note, "completed-marker")

    def test_later_retry_cannot_overwrite_prior_attempt_repair_linkage(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        first_evidence = self.base / "first-plan.md"
        first_evidence.write_text("first plan\n", encoding="utf-8")
        missing_jobs = self.base / "missing-dir" / "jobs.log"
        first = self.complete(
            route_path, "plan", first_evidence,
            jobs=missing_jobs, attempt_id="att-prior-link",
            attempt_axes=self.registered_axes(),
        )
        self.assertNotEqual(first.returncode, 0)
        self.assertIn("attempt-row-absent", first.stdout + first.stderr)

        second_evidence = self.base / "second-plan.md"
        second_evidence.write_text("second plan\n", encoding="utf-8")
        self.write_row("open", "retry", "att-later-link")
        second = self.complete(
            route_path, "plan", second_evidence,
            jobs=self.jobs, attempt_id="att-later-link",
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

        replay = self.complete(
            route_path, "plan", first_evidence,
            jobs=missing_jobs, attempt_id="att-prior-link",
            attempt_axes=self.registered_axes(),
        )
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("attempt-row-absent", replay.stdout + replay.stderr)

        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        prior_link = json.loads((directory / "plan.att-prior-link.attempt.json").read_text())
        latest_link = json.loads((directory / "plan.attempt.json").read_text())
        self.assertEqual(prior_link["attempt_id"], "att-prior-link")
        self.assertEqual(latest_link["attempt_id"], "att-later-link")

        dead = "pid=999999995,pid_start=1"
        self.write_row(
            "open", "prior-stale", "att-prior-link",
            extra=f"{dead},route_id={route['route_id']},route_node=plan",
        )
        registry_spec = importlib.util.spec_from_file_location(
            "dispatch_registry_retry_link", ROOT / "utilities/dispatch-registry.py")
        registry = importlib.util.module_from_spec(registry_spec)
        registry_spec.loader.exec_module(registry)
        rows = registry.read_rows(self.jobs)
        prior_row = next(r for r in rows if r["meta"].get("attempt_id") == "att-prior-link")
        class Args:
            pass
        args = Args(); args.agent_home = self.agent_home; args.jobs = self.jobs; args.now = 0.0
        newest = {}
        for row in rows:
            key = (row["meta"].get("route_id"), row["meta"].get("route_node"))
            if all(key): newest[key] = row["order"]
        category, _, note = registry.classify(prior_row, args, newest, rows)
        self.assertEqual(category, "marker-backed-stale")
        self.assertEqual(note, "completed-marker")


    # SD-94 fixtures -------------------------------------------------------
    # A `parent_completion_delivery=claude-parent-runtime` supervisor closes the exact row
    # BEFORE `complete` runs, so SD-70's "complete closes the row" order never happens on
    # that path. The four cases below pin the corrected eligibility and the fail-closed
    # boundary around it; the SD-70 fixtures above are the untouched regression baseline.
    _SUPERVISOR_PASS = (
        "note=completed-supervisor,failure_class=pass,"
        "classifier_source=supervisor-terminal-v1,detected_by=completion-supervisor"
    )

    def test_supervisor_closed_pass_row_earns_its_marker(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        self.write_row("done", "supervised", "att-supervised", self._SUPERVISOR_PASS)
        self.write_row("open", "sibling", "att-sibling")
        result = self.complete(route_path, "plan", evidence, jobs=self.jobs,
                               attempt_id="att-supervised")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        canonical = directory / "plan.json"
        self.assertTrue(canonical.is_file())
        status, meta = self.read_row("att-supervised")
        self.assertEqual(status, "done")                      # never re-closed
        self.assertEqual(meta.get("note"), "completed-marker")
        self.assertEqual(meta.get("failure_class"), "pass")   # supervisor evidence survives
        self.assertEqual(meta.get("completion_marker"), str(canonical))
        self.assertEqual(meta.get("completion_marker_history"),
                         str(directory / f"plan.{json.loads(canonical.read_text())['sequence']}.json"))
        # the same route/node's other attempt is untouched
        sibling_status, sibling_meta = self.read_row("att-sibling")
        self.assertEqual(sibling_status, "open")
        self.assertIsNone(sibling_meta.get("completion_marker"))

    def test_supervisor_closed_non_pass_row_stays_refused(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "blocked.md"
        evidence.write_text("must not publish\n", encoding="utf-8")
        self.write_row("done", "blocked", "att-supervised-blocked",
                       "note=completed-supervisor,failure_class=blocked")
        result = self.complete(route_path, "plan", evidence, jobs=self.jobs,
                               attempt_id="att-supervised-blocked")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attempt-row-terminal-without-completion", result.stderr)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        self.assertFalse((directory / "plan.json").exists())
        status, meta = self.read_row("att-supervised-blocked")
        self.assertEqual(status, "done")
        self.assertEqual(meta.get("note"), "completed-supervisor")

    def test_supervisor_marker_duplicate_complete_is_idempotent(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        self.write_row("done", "supervised", "att-supervised-dup", self._SUPERVISOR_PASS)
        first = self.complete(route_path, "plan", evidence, jobs=self.jobs,
                              attempt_id="att-supervised-dup")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.complete(route_path, "plan", evidence, jobs=self.jobs,
                               attempt_id="att-supervised-dup")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        rows = [line for line in self.jobs.read_text(encoding="utf-8").splitlines()
                if "att-supervised-dup" in line]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].count("completion_marker="), 1)
        directory = self.agent_home / ".dispatch" / "completion" / route["route_id"]
        self.assertTrue((directory / "plan.1.json").is_file())
        self.assertFalse((directory / "plan.2.json").exists())   # no second history write

    def test_other_terminal_notes_are_unaffected_by_the_sd94_exception(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        for attempt, extra in (
            ("att-killed-note", "note=dead-worker-fail,failure_class=fail"),
            ("att-no-note", "failure_class=pass"),
            ("att-orphan-note", "note=dead-parent-orphaned,failure_class=pass"),
        ):
            self.write_row("done", attempt, attempt, extra)
            result = self.complete(route_path, "plan", evidence, jobs=self.jobs,
                                   attempt_id=attempt)
            self.assertNotEqual(result.returncode, 0, attempt)
            self.assertIn("attempt-row-terminal-without-completion", result.stderr, attempt)


if __name__ == "__main__":
    unittest.main()
