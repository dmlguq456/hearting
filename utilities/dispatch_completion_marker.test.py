#!/usr/bin/env python3
"""SD-56 fixtures: completion marker canonical write + start-time gate."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_contract as D
spec = importlib.util.spec_from_file_location("route", ROOT / "utilities/capability-route.py")
ROUTE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ROUTE)
WRAPPER_PARENT_SANDBOXES = ROUTE.WRAPPER_PARENT_SANDBOXES
_node_spec = importlib.util.spec_from_file_location("dispatch_node_for_marker_test", ROOT / "utilities/dispatch-node.py")
DISPATCH_NODE = importlib.util.module_from_spec(_node_spec)
_node_spec.loader.exec_module(DISPATCH_NODE)

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
        # SD-112 §13.33.2-(8): the env-less dispatch state root is the stable
        # per-user root, no longer AGENT_HOME/.dispatch. Give the fixture its
        # own HOME so that root is fixture-owned (see `base_env`), and expect
        # markers there.
        self.stable_home = self.base / "stable-home"
        self.stable_home.mkdir()
        self.stable_dispatch = (
            self.stable_home / ".local" / "state" / "hearting" / "dispatch"
        )

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def stable_root_env(self):
        """Pin in-process stable-root resolution to this fixture's own HOME.

        Subprocess flows get this through `base_env`. The `classify` fixtures
        below call `dispatch-registry.py` in-process, where
        `dispatch_state_roots()` reads the ambient `os.environ` instead and
        would hunt for markers under the invoking developer's real state root.
        """
        prior = {
            key: os.environ.get(key)
            for key in ("HOME", "XDG_STATE_HOME", "HARNESS_STATE_ROOT")
        }
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ["HOME"] = str(self.stable_home)
        try:
            yield
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def compile_route(self, intensity="strong", capability="autopilot-code", capability_mode="dev", signals=None):
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
        # Compile and adapter validation must see the same deliberate runtime,
        # state root and git environment. Otherwise the installed host release
        # is sealed here and the wrapper never reaches the marker being tested.
        if signals is None:
            signals = ["shared-contract"] if intensity == "strong" else []
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            ROUTE._forget_launch_path(ROOT)
            route = ROUTE.compile_route(
                capability, capability_mode, intensity, self.repo, self.artifact,
                signals=signals, transport="headless", tracking="tracked",
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
        # registry never leaks into this fixture's stable-root-relative marker
        # expectations.
        env = {
            **os.environ,
            "AGENT_HOME": str(self.agent_home),
            "AGENT_ARTIFACT_ROOT": str(self.artifact),
            "OPENCODE_CONFIG_CONTENT": "{}",
            # `stable_state_root` reads HARNESS_STATE_ROOT -> XDG_STATE_HOME
            # -> HOME; inheriting any of the three would send this fixture's
            # env-less markers into the invoking developer's real state root.
            "HOME": str(self.stable_home),
            # `complete` would start a detached checkpoint that outlives the fixture root.
            "AGENT_ARTIFACT_CHECKPOINT": "off",
        }
        env.pop("AGENT_DISPATCH_JOBS", None)
        env.pop("XDG_STATE_HOME", None)
        env.pop("HARNESS_STATE_ROOT", None)
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

    def revise(self, route_path, node_id, evidence_path, *, basis, answers=None, direction=None,
               reason=None, jobs=None, author_attempt_id="att-revise-fixture"):
        command = [sys.executable, str(ROOT / "utilities/capability-route.py"), "revise",
                   "--route", str(route_path), "--node", node_id, "--evidence", str(evidence_path),
                   "--basis", basis, "--author-attempt-id", author_attempt_id]
        if answers: command += ["--answers", ",".join(answers)]
        if direction: command += ["--direction", direction]
        if reason: command += ["--reason", reason]
        if jobs is not None: command += ["--jobs", str(jobs)]
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
        canonical = self.stable_dispatch / "completion" / route["route_id"] / "plan.json"
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

        # Write the markers this route's "execute" node depends on, derived
        # from the compiled route rather than hardcoded: at "strong"+
        # intensity `plan-check` is a 2..3-way join_policy=all parallel group
        # (W3), so `execute.depends_on` names every realized leg (e.g.
        # `plan-check` and `plan-check-alternative`), each gated by its own
        # completion marker keyed on node id. Re-run: the gate itself must no
        # longer be the blocker (other reasons -- e.g. missing real
        # claude/codex/opencode binaries -- are acceptable).
        execute_node = next(n for n in route["nodes"] if n["id"] == "execute")
        for node_id in execute_node["depends_on"]:
            evidence = self.base / f"{node_id}.md"
            evidence.write_text(f"{node_id} body\n", encoding="utf-8")
            completed = self.complete(route_path, node_id, evidence)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for harness in ADAPTERS:
            with self.subTest(harness=harness, phase="after-marker"):
                command = self.wrapper_command(harness, "start", route_path, route, "execute")
                result = subprocess.run(command, text=True, capture_output=True, env=self.base_env())
                self.assertNotIn("reason=completion-marker-missing", result.stdout)

    def test_corrupt_predecessor_marker_is_typed_refusal_before_spawn(self):
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, "route-corrupt-predecessor.json")
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        self.assertEqual(self.complete(route_path, "plan", evidence).returncode, 0)
        marker = self.stable_dispatch / "completion" / route["route_id"] / "plan.json"
        marker.write_text("{corrupt", encoding="utf-8")
        result = subprocess.run(
            self.wrapper_command("codex", "start", route_path, route, "plan-check"),
            text=True, capture_output=True, env=self.base_env(),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reason=completion-marker-integrity-broken", result.stdout)
        self.assertIn("completion-marker-unreadable", result.stdout)
        self.assertIn("child_spawned=0", result.stdout)
        self.assertFalse(self.jobs.exists())

    # SD-154/B-2 defect #2, M1 (real gate, not stubbed) ----------------------
    def test_a_sd154_2_real_gate_reports_next_action_for_route_state_refusal(self):
        """The real wrapper's `completion_marker_gate` -- the exact function
        `stage-dispatch-fallback.py`'s own stub test (`stage_dispatch_
        fallback.test.py::FallbackTest::
        test_a_sd154_2_route_state_refusal_does_not_descend_to_inline`)
        proves the fallback chain never descends past -- reports a supported
        `next_action` for its own `completion-marker-missing` refusal, at
        exit 65, `child_spawned=0`.
        """
        route = self.compile_route()
        route_path = self.write_route(route, "route-v3-next-action.json")
        harness = next(iter(ADAPTERS))
        command = self.wrapper_command(harness, "start", route_path, route, "execute")
        result = subprocess.run(command, text=True, capture_output=True, env=self.base_env())
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reason=completion-marker-missing", result.stdout)
        self.assertIn("child_spawned=0", result.stdout)
        self.assertIn("next_action=", result.stdout)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        self.assertTrue(fields.get("next_action"), result.stdout)

    # SD-154 revision transition ---------------------------------------------
    def test_a_sd154_1_revise_records_history_and_admits_next_round(self):
        """A-SD154-1: `revise` publishes marker k+1 (`stage_authority=revision`)
        over N, leaves `<N>.1.json` byte-identical, and the canonical marker
        advances -- N is `current` again by `gate_currency`.
        """
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, "route-revise-1.json")
        plan_evidence = self.base / "plan.md"
        plan_evidence.write_text("plan v1\n", encoding="utf-8")
        completed = self.complete(route_path, "plan", plan_evidence)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

        canonical_dir = self.stable_dispatch / "completion" / route["route_id"]
        plan_seq1_bytes = (canonical_dir / "plan.1.json").read_bytes()

        plan_evidence.write_text("plan v2 (corrected)\n", encoding="utf-8")
        result = self.revise(
            route_path, "plan", plan_evidence,
            basis="user-direction", direction="gate-release-fixture-1",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        marker = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(marker["stage_authority"], "revision")
        self.assertEqual(marker["sequence"], 2)
        self.assertEqual(marker["revision"]["basis"], "user-direction")
        self.assertEqual(marker["revision"]["of_sequence"], 1)
        self.assertEqual((canonical_dir / "plan.1.json").read_bytes(), plan_seq1_bytes)
        plan_canonical_now = json.loads((canonical_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan_canonical_now["sequence"], 2)
        self.assertTrue(D.completion_marker_is_current(route, next(
            node for node in route["nodes"] if node["id"] == "plan"
        ), canonical_dir / "plan.json", plan_canonical_now))
        link = json.loads((canonical_dir / f"plan.{plan_canonical_now['attempt_id']}.attempt.json").read_text())
        self.assertEqual(link["completion_marker"], str(canonical_dir / "plan.json"))

    def test_a_sd154_revision_chain_seq3_keeps_official_link_and_live_evidence(self):
        """Official revise supports seq2+ with a real original attempt-link.

        Every revision points to immutable predecessor bytes, while all
        revisions intentionally share the live evidence path that revise
        updates. The complete predecessor chain must remain current.
        """
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, "route-revise-seq3.json")
        evidence = self.base / "plan.md"
        evidence.write_text("plan v1\n", encoding="utf-8")
        self.assertEqual(self.complete(route_path, "plan", evidence).returncode, 0)
        for sequence, content in ((2, "plan v2\n"), (3, "plan v3\n")):
            evidence.write_text(content, encoding="utf-8")
            result = self.revise(
                route_path, "plan", evidence, basis="user-direction",
                direction=f"gate-release-fixture-{sequence}",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            marker = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(marker["sequence"], sequence)
        canonical_dir = self.stable_dispatch / "completion" / route["route_id"]
        latest = json.loads((canonical_dir / "plan.json").read_text())
        self.assertEqual(latest["revision"]["of_sequence"], 2)
        self.assertEqual(latest["evidence"]["path"], str(evidence.resolve()))
        self.assertEqual(
            D.gate_currency(route, next(n for n in route["nodes"] if n["id"] == "plan"),
                            canonical_dir / "plan.json", latest).state,
            "current",
        )

    def test_a_sd154_revision_chain_longer_than_legacy_ceiling_is_current(self):
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, "route-revise-long-chain.json")
        evidence = self.base / "plan-long-chain.md"
        evidence.write_text("plan v1\n", encoding="utf-8")
        self.assertEqual(self.complete(route_path, "plan", evidence).returncode, 0)
        for sequence in range(2, 131):
            evidence.write_text(f"plan v{sequence}\n", encoding="utf-8")
            result = self.revise(
                route_path, "plan", evidence, basis="user-direction",
                direction=f"long-chain-{sequence}",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        canonical_dir = self.stable_dispatch / "completion" / route["route_id"]
        latest = json.loads((canonical_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["sequence"], 130)
        currency = D.gate_currency(
            route, next(n for n in route["nodes"] if n["id"] == "plan"),
            canonical_dir / "plan.json", latest,
        )
        self.assertEqual(currency.state, "current", currency)

    def test_a_sd154_rejects_forged_revision_predecessor_history_and_link(self):
        import shutil
        for target in ("history", "link"):
            with self.subTest(target=target):
                route = self.compile_route(intensity="standard")
                route_path = self.write_route(route, f"route-revise-forged-{target}.json")
                directory = self.stable_dispatch / "completion" / route["route_id"]
                if directory.exists():
                    shutil.rmtree(directory)
                evidence = self.base / f"plan-{target}.md"
                evidence.write_text("plan v1\n", encoding="utf-8")
                self.assertEqual(self.complete(route_path, "plan", evidence).returncode, 0)
                evidence.write_text("plan v2\n", encoding="utf-8")
                revised = self.revise(
                    route_path, "plan", evidence, basis="user-direction",
                    direction=f"forged-{target}",
                )
                self.assertEqual(revised.returncode, 0, revised.stdout + revised.stderr)
                latest = json.loads((directory / "plan.json").read_text())
                if target == "history":
                    history = directory / "plan.1.json"
                    forged = json.loads(history.read_text())
                    forged["completion_gate"] = "forged-gate"
                    history.write_text(json.dumps(forged), encoding="utf-8")
                else:
                    link_path = directory / f"plan.{latest['attempt_id']}.attempt.json"
                    link = json.loads(link_path.read_text())
                    link["completion_marker"] = str(directory / "forged.json")
                    link_path.write_text(json.dumps(link), encoding="utf-8")
                currency = D.gate_currency(
                    route, next(n for n in route["nodes"] if n["id"] == "plan"),
                    directory / "plan.json", latest,
                )
                self.assertTrue(currency.state.startswith("integrity-broken:"), currency)

    def test_a_sd154_rejects_malformed_and_cyclic_revision_edges(self):
        import hashlib
        import shutil
        for defect in ("malformed", "cycle"):
            with self.subTest(defect=defect):
                route = self.compile_route(intensity="standard")
                route_path = self.write_route(route, f"route-revise-edge-{defect}.json")
                directory = self.stable_dispatch / "completion" / route["route_id"]
                if directory.exists():
                    shutil.rmtree(directory)
                evidence = self.base / f"plan-edge-{defect}.md"
                evidence.write_text("plan v1\n", encoding="utf-8")
                self.assertEqual(self.complete(route_path, "plan", evidence).returncode, 0)
                for sequence in (2, 3):
                    evidence.write_text(f"plan v{sequence}\n", encoding="utf-8")
                    result = self.revise(
                        route_path, "plan", evidence, basis="user-direction",
                        direction=f"edge-{defect}-{sequence}",
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                latest = json.loads((directory / "plan.json").read_text())
                if defect == "malformed":
                    latest["revision"]["of_sequence"] = "2"
                else:
                    prior = json.loads((directory / "plan.2.json").read_text())
                    prior["revision"]["of_sequence"] = 3
                    prior_bytes = json.dumps(prior, sort_keys=True, separators=(",", ":")).encode()
                    # History is compared as parsed JSON; the digest in the
                    # next edge seals these exact bytes, as official revise
                    # does for its serialized marker.
                    (directory / "plan.2.json").write_bytes(prior_bytes)
                    latest["revision"]["of_marker_sha256"] = hashlib.sha256(prior_bytes).hexdigest()
                latest_bytes = json.dumps(latest, sort_keys=True, separators=(",", ":")).encode()
                (directory / "plan.3.json").write_bytes(latest_bytes)
                (directory / "plan.json").write_bytes(latest_bytes)
                currency = D.gate_currency(
                    route, next(n for n in route["nodes"] if n["id"] == "plan"),
                    directory / "plan.json", latest,
                )
                self.assertEqual(currency.state, "integrity-broken:identity-mismatch")
                self.assertEqual(currency.reason, "revision-provenance-invalid")

    def test_a_sd154_3_revision_tombstones_downstream_marker(self):
        """A-SD154-3: a revision over N tombstones the canonical marker of
        every node downstream of N -- `execute` (which depends on
        `plan-check`, not `plan`, directly) is reopened too.
        """
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, "route-revise-3.json")
        plan_evidence = self.base / "plan.md"
        plan_evidence.write_text("plan v1\n", encoding="utf-8")
        self.assertEqual(self.complete(route_path, "plan", plan_evidence).returncode, 0)
        plan_check_evidence = self.base / "plan-check.md"
        plan_check_evidence.write_text("plan-check v1\n", encoding="utf-8")
        self.assertEqual(self.complete(route_path, "plan-check", plan_check_evidence).returncode, 0)

        plan_evidence.write_text("plan v2 (corrected)\n", encoding="utf-8")
        result = self.revise(
            route_path, "plan", plan_evidence,
            basis="owner-correction", reason="fixture: plan needed a correction",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        canonical_dir = self.stable_dispatch / "completion" / route["route_id"]
        tombstone = json.loads((canonical_dir / "plan-check.json").read_text(encoding="utf-8"))
        self.assertEqual(tombstone.get("state"), "superseded-by-upstream-revision")
        self.assertEqual(tombstone.get("superseded_by"), {"node": "plan", "sequence": 2})

        for action in ("start", "dry-run"):
            with self.subTest(action=action):
                command = self.wrapper_command("claude", action, route_path, route, "execute")
                refused = subprocess.run(command, text=True, capture_output=True, env=self.base_env())
                self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
                self.assertIn("reason=completion-evidence-superseded", refused.stdout)
                self.assertIn("detail=plan-check:superseded-by=plan@2", refused.stdout)
                self.assertIn("child_spawned=0", refused.stdout)
                self.assertIn("next_action=rerun dependency node plan-check", refused.stdout)
        (canonical_dir / "plan-check.json").unlink()
        missing = subprocess.run(
            self.wrapper_command("claude", "start", route_path, route, "execute"),
            text=True, capture_output=True, env=self.base_env(),
        )
        self.assertNotEqual(missing.returncode, 0, missing.stdout + missing.stderr)
        self.assertIn("reason=completion-marker-missing", missing.stdout)
        self.assertIn("detail=plan-check", missing.stdout)
        self.assertIn("child_spawned=0", missing.stdout)

    def test_a_sd154_5_integrity_refusals_publish_nothing(self):
        """A-SD154-5: kept refusals publish zero new markers."""
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, "route-revise-5.json")
        plan_evidence = self.base / "plan.md"
        plan_evidence.write_text("plan v1\n", encoding="utf-8")
        self.assertEqual(self.complete(route_path, "plan", plan_evidence).returncode, 0)
        canonical_dir = self.stable_dispatch / "completion" / route["route_id"]

        with self.subTest("unchanged-evidence"):
            listing_before = sorted(canonical_dir.glob("plan.*.json"))
            result = self.revise(
                route_path, "plan", plan_evidence,
                basis="user-direction", direction="gate-release-fixture-2",
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("revision-evidence-unchanged", result.stdout + result.stderr)
            self.assertEqual(sorted(canonical_dir.glob("plan.*.json")), listing_before)

        with self.subTest("basis-unverified-review-findings-with-no-jobs"):
            plan_evidence.write_text("plan v2\n", encoding="utf-8")
            listing_before = sorted(canonical_dir.glob("plan.*.json"))
            result = self.revise(
                route_path, "plan", plan_evidence,
                basis="review-findings", answers=["att-not-a-real-verdict"],
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("revision-basis-unverified", result.stdout + result.stderr)
            self.assertEqual(sorted(canonical_dir.glob("plan.*.json")), listing_before)

        with self.subTest("target-marker-absent"):
            result = self.revise(
                route_path, "plan-check", plan_evidence,
                basis="owner-correction", reason="fixture: no plan-check marker exists yet",
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("revision-target-marker-absent", result.stdout + result.stderr)

        with self.subTest("sealed-cycle-evidence"):
            # D-120: `publish_revision_locked` must reuse the same
            # `require_cycle_output` admission `_publish_completion_locked`
            # already enforces -- a revision cannot certify evidence from
            # outside the route's bound cycle any more than an original
            # completion can.
            import artifact_producer as P
            cycle_route = self.compile_route(intensity="standard")
            cycle_route_path = self.write_route(cycle_route, "route-revise-5-cycle.json")
            with mock.patch.dict(os.environ, self.base_env(), clear=True):
                issued = P.begin(self.artifact, route_file=cycle_route_path,
                                 capability=cycle_route["capability"],
                                 intensity=cycle_route["effective_intensity"], require_cycle=True)
                bound_evidence = Path(issued["cycle_dir"]) / "artifacts" / "plan.md"
                bound_evidence.parent.mkdir(parents=True, exist_ok=True)
                bound_evidence.write_text("plan v1 (bound)\n", encoding="utf-8")
                completed = self.complete(
                    cycle_route_path, "plan", bound_evidence,
                    attempt_id="att-inline-plan-cycle-fixture", attempt_axes={
                        "dispatch_depth": 2, "transport": "interactive", "execution_surface": "inline",
                        "registered_worker": "0", "fallback_hop": "inline",
                    })
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                # Change the marker's OWN recorded evidence file in place first,
                # so `gate_currency` sees drift (`revised-unrecorded`) and lets
                # the call reach the evidence-admission check below -- the
                # refusal under test is about the NEW `--evidence` path
                # (`outside_evidence`), not about whether a revision is owed.
                bound_evidence.write_text("plan v1 (bound, edited)\n", encoding="utf-8")
                outside_evidence = self.base / "plan-outside-cycle.md"
                outside_evidence.write_text("plan v2 (outside the bound cycle)\n", encoding="utf-8")
                cycle_canonical_dir = self.stable_dispatch / "completion" / cycle_route["route_id"]
                listing_before = sorted(cycle_canonical_dir.glob("plan.*.json"))
                result = self.revise(
                    cycle_route_path, "plan", outside_evidence,
                    basis="owner-correction", reason="fixture: evidence outside the bound cycle",
                )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("artifact-outside-bound-cycle", result.stdout + result.stderr)
            self.assertEqual(sorted(cycle_canonical_dir.glob("plan.*.json")), listing_before)

    def _gates_off_cycle_route(self, name):
        """A standard route bound to an open cycle, with `plan` completed inline from cycle evidence."""
        import artifact_producer as P
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, name)
        issued = P.begin(self.artifact, route_file=route_path, capability=route["capability"],
                         intensity=route["effective_intensity"], require_cycle=True)
        evidence = Path(issued["cycle_dir"]) / "artifacts" / "plan.md"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("plan v1\n", encoding="utf-8")
        completed = self.complete(route_path, "plan", evidence)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        marker_path = self.stable_dispatch / "completion" / route["route_id"] / "plan.json"
        return route, route_path, evidence, marker_path

    def test_gates_off_an_evidence_edit_is_history_and_revise_still_records_it(self):
        """The BC deadlock (2026-10-02): with gates off, continuation read the edited
        evidence as current while `revise` refused it as unchanged. Both now read the
        same comparison -- recorded digest vs. the evidence -- and an operation that
        proceeds on the edited completion keeps the edit as one history line; reads
        (including a dry-run) write nothing."""
        with mock.patch.dict(os.environ, {**self.base_env(), "HEARTING_GATES": "off"}, clear=True):
            route, route_path, evidence, marker_path = self._gates_off_cycle_route("route-gates-off-edit.json")
            plan = next(n for n in route["nodes"] if n["id"] == "plan")
            recorded = json.loads(marker_path.read_text())["evidence"]["sha256"]
            evidence.write_text("plan v2, corrected after the smoke run\n", encoding="utf-8")
            history = D.evidence_change_history_path(marker_path, "plan")
            listing = sorted(p.name for p in marker_path.parent.iterdir())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                for _ in range(2):
                    currency = D.evidence_currency(route, plan, marker_path)
                    self.assertEqual(currency.state, "current")
                D.completion_marker_gate(str(route_path), "plan-check", "dry-run", self.agent_home, self.jobs)
            self.assertNotIn("revised-unrecorded", err.getvalue())
            self.assertEqual(sorted(p.name for p in marker_path.parent.iterdir()), listing)
            # An operation that proceeds on `plan` keeps the edit, once.
            evidence_check = evidence.with_name("plan-check.md")
            evidence_check.write_text("plan-check ok\n", encoding="utf-8")
            completed = self.complete(route_path, "plan-check", evidence_check)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertNotIn("revised-unrecorded", completed.stderr)
            D.note_evidence_change(route, plan, marker_path)
            lines = [json.loads(line) for line in history.read_text().splitlines()]
            self.assertEqual(len(lines), 1)
            self.assertEqual((lines[0]["previous_sha256"], lines[0]["sha256"], lines[0]["reason"]),
                             (recorded, currency.evidence_digest, "evidence-changed-after-completion"))
            for key in ("observed_at", "observed_by", "evidence_changed_at", "observed_in"):
                self.assertTrue(lines[0][key], key)
            listed = ROUTE._route_revisions(route)
            self.assertEqual([(row["node"], row["basis"], row["of_evidence_sha256"], row["evidence_sha256"])
                              for row in listed], [("plan", "automatic", recorded, currency.evidence_digest)])
            revised = self.revise(route_path, "plan", evidence, basis="owner-correction",
                                  reason="fixture: corrected after the smoke run")
            self.assertEqual(revised.returncode, 0, revised.stdout + revised.stderr)
            self.assertEqual(json.loads(marker_path.read_text())["sequence"], 2)
            listed = ROUTE._route_revisions(route)
            # One shape for both kinds of history row.
            self.assertEqual({frozenset(row) for row in listed}, {frozenset(listed[0])})
            self.assertEqual(sorted(row["basis"] for row in listed), ["automatic", "owner-correction"])
            again = self.revise(route_path, "plan", evidence, basis="owner-correction",
                                reason="fixture: nothing new")
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("revision-evidence-unchanged", again.stdout + again.stderr)

    def test_missing_completed_evidence_is_history_only_with_gates_off(self):
        with mock.patch.dict(os.environ, {**self.base_env(), "HEARTING_GATES": "off"}, clear=True):
            route, _route_path, evidence, marker_path = self._gates_off_cycle_route("route-gates-off-delete.json")
            node = next(n for n in route["nodes"] if n["id"] == "plan")
            recorded = json.loads(marker_path.read_text())["evidence"]["sha256"]
            evidence.unlink()
            currency = D.evidence_currency(route, node, marker_path)
            self.assertEqual((currency.state, currency.reason, currency.evidence_digest),
                             ("current", "completion-evidence-recorded-missing", recorded))
            D.note_evidence_change(route, node, marker_path)
            D.note_evidence_change(route, node, marker_path)
            history = [json.loads(line) for line in
                       D.evidence_change_history_path(marker_path, "plan").read_text().splitlines()]
            self.assertEqual(len(history), 1)
            self.assertEqual((history[0]["change_kind"], history[0]["reason"], history[0]["sha256"]),
                             ("missing", "evidence-missing-after-completion", recorded))
        with mock.patch.dict(os.environ, {**self.base_env(), "HEARTING_GATES": "on"}, clear=True):
            route, _route_path, evidence, marker_path = self._gates_off_cycle_route("route-gates-on-delete.json")
            evidence.unlink()
            node = next(n for n in route["nodes"] if n["id"] == "plan")
            self.assertEqual(D.evidence_currency(route, node, marker_path).reason,
                             "completion-evidence-unreadable")

    def test_revise_unchanged_means_the_named_evidence_equals_the_recorded_digest(self):
        """One rule in both gate modes: a revision is owed exactly when the evidence it
        names differs from the digest the marker recorded."""
        for gates in ("on", "off"):
            with self.subTest(gates=gates), \
                    mock.patch.dict(os.environ, {**self.base_env(), "HEARTING_GATES": gates}, clear=True):
                route, route_path, evidence, marker_path = self._gates_off_cycle_route(f"route-unchanged-{gates}.json")
                same_bytes = evidence.with_name("plan-copy.md")
                same_bytes.write_text("plan v1\n", encoding="utf-8")
                if gates == "on":
                    # The marker's own file was edited, but the named evidence carries the recorded bytes.
                    evidence.write_text("plan v1, edited in place\n", encoding="utf-8")
                    refused = self.revise(route_path, "plan", same_bytes, basis="owner-correction",
                                          reason="fixture: same bytes, new path")
                    self.assertNotEqual(refused.returncode, 0)
                    self.assertIn("revision-evidence-unchanged", refused.stdout + refused.stderr)
                else:
                    # The marker's own file is untouched; the named evidence differs.
                    different = evidence.with_name("plan-v2.md")
                    different.write_text("plan v2 at a new path\n", encoding="utf-8")
                    allowed = self.revise(route_path, "plan", different, basis="owner-correction",
                                          reason="fixture: new evidence, new path")
                    self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
                    self.assertEqual(json.loads(marker_path.read_text())["evidence"]["path"], str(different))

    def test_a_sd154_9_execute_revision_records_descendant_commits(self):
        """A-SD154-9 (plan A-2): an `execute` revision is a code change, not
        just new gate evidence -- `revision.commits` is the descendant range
        SD-156's `source_lineage_verdict` proves, from execute's own most
        recent terminal `launch_head` (recorded on the registry row) to the
        current HEAD."""
        self.jobs = self.stable_dispatch / "jobs.log"
        self.jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs.touch(mode=0o600)
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, "route-revise-9.json")
        by_id = {n["id"]: n for n in route["nodes"]}
        inline_metadata = {
            "attempt_schema_version": 2, "dispatch_depth": 2, "transport": "interactive",
            "execution_surface": "inline", "registered_worker": False, "fallback_hop": "inline",
        }
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            for node_id, attempt_id in (
                ("plan", "att-inline-plan-fixture"),
                ("plan-check", "att-inline-plan-check-fixture"),
                ("execute", "att-inline-execute-fixture"),
            ):
                evidence = self.base / f"{node_id}.md"
                evidence.write_text(f"{node_id} v1\n", encoding="utf-8")
                ROUTE._publish_completion_locked(
                    route, by_id[node_id], node_id, evidence, jobs=self.jobs,
                    attempt_id=attempt_id, attempt_metadata=inline_metadata,
                )
        sealed_head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                     text=True, capture_output=True, check=True).stdout.strip()
        self.write_row("done", "exec-launch", "att-execute-launch",
                       f"note=completed-marker,launch_head={sealed_head}", node_id="execute")
        (self.repo / "y").write_text("y", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "y"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "execute revision fixture"], check=True)
        new_head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                  text=True, capture_output=True, check=True).stdout.strip()
        self.assertNotEqual(new_head, sealed_head)
        execute_evidence = self.base / "execute.md"
        execute_evidence.write_text("execute v2 (revised)\n", encoding="utf-8")
        result = self.revise(
            route_path, "execute", execute_evidence,
            basis="owner-correction", reason="fixture: execute code changed", jobs=self.jobs,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        marker = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(marker["revision"]["commits"], [new_head])

    def _a_sd154_10_fixture(self, route_name):
        """A `plan` marker under the SAME state root `admit_round` resolves
        (`continuation_closure_fixture`'s convention: jobs.log lives under
        `stable_dispatch`, not a sibling `self.base` path -- otherwise the
        marker write and `admit_round`'s explicit-jobs completion_dir resolve
        two different directories, SD-154 P2's asymmetry pitfall)."""
        self.jobs = self.stable_dispatch / "jobs.log"
        self.jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs.touch(mode=0o600)
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, route_name)
        plan_node = next(n for n in route["nodes"] if n["id"] == "plan")
        plan_evidence = self.base / "plan.md"
        plan_evidence.write_text("plan v1\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            ROUTE._publish_completion_locked(
                route, plan_node, "plan", plan_evidence, jobs=self.jobs,
                attempt_id="att-plan-1", attempt_metadata={
                    "attempt_schema_version": 2, "dispatch_depth": plan_node["dispatch_depth"],
                    "transport": "interactive", "execution_surface": "inline",
                    "registered_worker": False, "fallback_hop": "inline",
                },
            )
        # The plan gate's evidence changes in place (a mid-route correction)
        # without anyone calling `revise` -- condition (1), `revised-unrecorded`.
        plan_evidence.write_text("plan v2 (corrected)\n", encoding="utf-8")
        plan_check_node = next(n for n in route["nodes"] if n["id"] == "plan-check")
        pipe = ("capability=autopilot-code,attempt_schema_version=2,registered_worker=1,"
                f"route_id={route['route_id']},route_node=plan-check,worker_type=review,"
                "note=completed-review-blocking,attempt_id=att-plancheck-1")
        with self.jobs.open("a", encoding="utf-8") as fh:
            fh.write(f"2026-08-24T00:00:00Z\tdone\t{self.repo}\t{self.repo}\tslug-r1\t{pipe}\n")
        return route, plan_check_node

    def test_a_sd154_10_admit_round_auto_records_once(self):
        """A-SD154-10 (13.59.3 rule 8): before admitting `plan-check`'s next
        round, `admit_round` auto-records a `plan` revision naming the last
        blocking round as its `answers` -- once, idempotently.
        """
        route, plan_check_node = self._a_sd154_10_fixture("route-a10.json")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            admission = DISPATCH_NODE.admit_round(
                route, plan_check_node, self.jobs, owner_attempt_id="att-owner-1",
            )
        self.assertEqual(len(admission.auto_revisions), 1, admission.auto_revisions)
        recorded = admission.auto_revisions[0]
        self.assertEqual(recorded["stage_authority"], "revision")
        self.assertEqual(recorded["revision"]["basis"], "review-findings")
        self.assertEqual(recorded["revision"]["answers"], ["att-plancheck-1"])
        self.assertEqual(recorded["revision"]["recorded_by"], "runtime-auto")
        self.assertEqual(recorded["revision"]["author_attempt_id"], "att-owner-1")
        self.assertEqual(admission.budget.round_kind, "closure-check")

        # Idempotent: `plan` is `current` again, so a second call records nothing.
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            second = DISPATCH_NODE.admit_round(
                route, plan_check_node, self.jobs, owner_attempt_id="att-owner-1",
            )
        self.assertEqual(second.auto_revisions, ())

    def test_a_sd154_dry_run_previews_auto_revision_without_mutation(self):
        route, plan_check_node = self._a_sd154_10_fixture("route-a10-dry-run.json")
        route_path = self.base / "route-a10-dry-run.json"
        canonical_dir = self.stable_dispatch / "completion" / route["route_id"]
        plan_marker_path = canonical_dir / "plan.json"
        before_marker = plan_marker_path.read_bytes()
        before_jobs = self.jobs.read_bytes()
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            admission = DISPATCH_NODE.admit_round(
                route, plan_check_node, self.jobs, owner_attempt_id="att-owner-dry",
                record_auto_revisions=False,
            )
        self.assertEqual(admission.auto_revisions, ())
        self.assertEqual(admission.planned_revision_nodes, frozenset({"plan"}))
        self.assertEqual(admission.budget.round_kind, "closure-check")
        # The preview fixture models a finished review but stores no process
        # identity. Supply that observation at the common reader, without
        # inventing launch/namespace fields in the historical row.
        with mock.patch.dict(os.environ, self.base_env(), clear=True), \
                mock.patch.object(D, "attempt_process_quiescence", return_value=
                                  D.ProcessQuiescence("quiescent", "controlled-review-exit")) as process:
            D.completion_marker_gate(
                str(route_path), "plan-check", "dry-run", self.agent_home, self.jobs,
                planned_revision_nodes=admission.planned_revision_nodes,
            )
        self.assertEqual(process.call_args.args[0]["attempt_id"], "att-plancheck-1")
        self.assertEqual(plan_marker_path.read_bytes(), before_marker)
        self.assertEqual(self.jobs.read_bytes(), before_jobs)
        self.assertFalse((canonical_dir / "plan.2.json").exists())

    def test_a_sd154_dry_run_unobserved_review_preserves_marker_and_row(self):
        route, plan_check_node = self._a_sd154_10_fixture("route-a10-unknown.json")
        route_path = self.base / "route-a10-unknown.json"
        canonical_dir = self.stable_dispatch / "completion" / route["route_id"]
        marker = canonical_dir / "plan.json"
        before_marker, before_jobs = marker.read_bytes(), self.jobs.read_bytes()
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            admission = DISPATCH_NODE.admit_round(
                route, plan_check_node, self.jobs, owner_attempt_id="att-owner-dry",
                record_auto_revisions=False,
            )
            with self.assertRaises(D.DispatchContractError) as caught:
                D.completion_marker_gate(
                    str(route_path), "plan-check", "dry-run", self.agent_home, self.jobs,
                    planned_revision_nodes=admission.planned_revision_nodes,
                )
        self.assertEqual(caught.exception.reason, "prior-attempt-unverifiable")
        self.assertIn("process-identity-missing", caught.exception.detail)
        self.assertEqual((marker.read_bytes(), self.jobs.read_bytes()), (before_marker, before_jobs))
        self.assertFalse((canonical_dir / "plan.2.json").exists())

    def test_a_sd154_10_tampered_history_records_nothing(self):
        """A tampered `<N>.1.json` history file is `integrity-broken`, not
        `revised-unrecorded` -- `admit_round` must not paper over it."""
        route, plan_check_node = self._a_sd154_10_fixture("route-a10-tamper.json")
        canonical_dir = self.stable_dispatch / "completion" / route["route_id"]
        history_path = canonical_dir / "plan.1.json"
        tampered = json.loads(history_path.read_text(encoding="utf-8"))
        tampered["evidence"]["sha256"] = "0" * 64
        history_path.write_text(json.dumps(tampered), encoding="utf-8")

        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            admission = DISPATCH_NODE.admit_round(
                route, plan_check_node, self.jobs, owner_attempt_id="att-owner-1",
            )
        self.assertEqual(admission.auto_revisions, ())

    def test_a_sd154_10_admit_round_same_revision_on_every_surface(self):
        """A-SD154-10 parity: real behavior, not just a static text census.

        `dispatch_node.test.py::ReviewRoundCapTest::
        test_a_sd154_10_admit_round_is_the_one_admission_entry_on_every_surface`
        only greps each launch surface's source for an `admit_round(` call
        and the absence of a stale `revisions=()` literal -- it never runs
        anything. This drives the exact fixture
        `test_a_sd154_10_admit_round_auto_records_once` proves once, through
        THREE separately loaded module objects (`dispatch-node.py`'s own
        `admit_round`, and `dispatch-batch.py`/`stage-dispatch-fallback.py`'s
        `DISPATCH_NODE.admit_round` -- the identical shared function per
        A-SD153-7, but called here through each surface's own import alias):
        the auto-recorded `plan` revision (`recorded_by=runtime-auto`,
        `answers=[plan-check r1]`) is written exactly once, before any of the
        three surfaces would launch a worker, and a second call -- through a
        DIFFERENT surface each time -- finds `plan` already current and
        records nothing more.
        """
        route, plan_check_node = self._a_sd154_10_fixture("route-a10-parity.json")
        canonical_dir = self.stable_dispatch / "completion" / route["route_id"]

        def _load(name, relative_path):
            spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        dispatch_node = _load("a_sd154_10_parity_dispatch_node", "utilities/dispatch-node.py")
        dispatch_batch = _load("a_sd154_10_parity_dispatch_batch", "utilities/dispatch-batch.py")
        stage_fallback = _load("a_sd154_10_parity_stage_fallback", "utilities/stage-dispatch-fallback.py")

        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            first = dispatch_node.admit_round(
                route, plan_check_node, self.jobs, owner_attempt_id="att-owner-parity",
            )
        self.assertEqual(len(first.auto_revisions), 1, first.auto_revisions)
        self.assertEqual(first.auto_revisions[0]["stage_authority"], "revision")
        self.assertEqual(first.auto_revisions[0]["revision"]["recorded_by"], "runtime-auto")
        self.assertEqual(first.auto_revisions[0]["revision"]["answers"], ["att-plancheck-1"])
        self.assertEqual(first.budget.round_kind, "closure-check")
        plan_marker = json.loads((canonical_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan_marker["sequence"], 2)
        self.assertEqual(plan_marker["stage_authority"], "revision")

        # A second call through dispatch-batch.py's own admit_round alias
        # sees `plan` already current -- nothing new, same admitted round.
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            second = dispatch_batch.DISPATCH_NODE.admit_round(
                route, plan_check_node, self.jobs, owner_attempt_id="att-owner-parity",
            )
        self.assertEqual(second.auto_revisions, ())
        self.assertEqual(second.budget.round_kind, "closure-check")
        self.assertEqual(
            json.loads((canonical_dir / "plan.json").read_text(encoding="utf-8"))["sequence"], 2)

        # And stage-dispatch-fallback.py's own alias agrees, still idempotent.
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            third = stage_fallback.DISPATCH_NODE.admit_round(
                route, plan_check_node, self.jobs, owner_attempt_id="att-owner-parity",
            )
        self.assertEqual(third.auto_revisions, ())
        self.assertEqual(third.budget, second.budget)
        self.assertEqual(third.budget, first.budget)

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
            # Asserts dispatch-batch stops on the real gate's route-state refusal.
            (ROOT / "utilities" / "dispatch-batch.test.py").resolve(),
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
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")
        completed = self.complete(route_path, "plan", evidence)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        directory = self.stable_dispatch / "completion" / route["route_id"]
        canonical = directory / "plan.json"
        marker = json.loads(canonical.read_text(encoding="utf-8"))
        marker.pop("schema_version")
        canonical.write_text(json.dumps(marker), encoding="utf-8")
        result = subprocess.run(
            self.wrapper_command("codex", "start", route_path, route, "plan-check"),
            text=True, capture_output=True, env=self.base_env(),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reason=completion-marker-integrity-broken", result.stdout)
        self.assertIn("completion-marker-identity-mismatch", result.stdout)

    # fixture 9 ---------------------------------------------------------
    def test_reharvest_preserves_history_and_latest_is_authoritative(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("v1\n", encoding="utf-8")
        first = self.complete(route_path, "plan", evidence)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        directory = self.stable_dispatch / "completion" / route["route_id"]
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
        directory = self.stable_dispatch / "completion" / route["route_id"]
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
        directory = self.stable_dispatch / "completion" / route["route_id"]
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
        directory = self.stable_dispatch / "completion" / route["route_id"]
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
        directory = self.stable_dispatch / "completion" / route["route_id"]
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
        directory = self.stable_dispatch / "completion" / route["route_id"]
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
        directory = self.stable_dispatch / "completion" / route["route_id"]
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
        canonical = self.stable_dispatch / "completion" / route["route_id"] / "plan.json"
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
            canonical = self.stable_dispatch / "completion" / route["route_id"] / "plan.json"
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
        with self.stable_root_env():
            category, reason, note = registry.classify(current_row, args, newest, rows)
        self.assertEqual(note, "completed-marker")
        self.assertEqual(category, "marker-backed-stale")
        # The weak unrelated row has no group identity or marker linkage.
        # Actual unknown observation must keep it open, even when applying
        # exact-death reconciliation beside the valid marker repair.
        unrelated_row = next(r for r in rows if r["meta"].get("attempt_id") == "att-unrelated")
        with self.stable_root_env():
            unrelated_category, _, unrelated_note = registry.classify(unrelated_row, args, newest, rows)
        self.assertEqual((unrelated_category, unrelated_note), ("unverifiable", None))
        before_jobs = self.jobs.read_bytes()
        args.attempt = "att-unrelated"
        args.session = args.route = args.node = args.job = None
        args.all = False
        args.apply = True
        args.only_exact_dead = True
        args.audit = args.integration_ref = None
        args.cascade_grace = args.cascade_kill_wait = 0
        output = io.StringIO()
        with self.stable_root_env(), contextlib.redirect_stdout(output):
            registry.reconcile(rows, args)
        reconciled = json.loads(output.getvalue())
        self.assertEqual(reconciled["closed"], 0)
        self.assertEqual(self.jobs.read_bytes(), before_jobs)

        # A controlled quiescent observation still proposes generic death,
        # rather than borrowing the other attempt's completed marker.
        with self.stable_root_env(), \
                mock.patch.object(D, "attempt_process_quiescence", return_value=
                                  D.ProcessQuiescence("quiescent", "controlled-process-exit")) as process:
            _, _, unrelated_note = registry.classify(unrelated_row, args, newest, rows)
        self.assertEqual(process.call_args.args[0]["attempt_id"], "att-unrelated")
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

        directory = self.stable_dispatch / "completion" / route["route_id"]
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
        with self.stable_root_env():
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
        directory = self.stable_dispatch / "completion" / route["route_id"]
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
        directory = self.stable_dispatch / "completion" / route["route_id"]
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
        directory = self.stable_dispatch / "completion" / route["route_id"]
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

    # OPERATIONS §5.10 owner-closure fixtures ----------------------------------
    # A review round that records blocking findings ends
    # `completed-review-blocking`. It is marker-eligible only through the
    # evidence-bound owner-closure gate; a true dead worker, a missing record,
    # an unlinked record, or an unexhausted budget keep the SD-94 refusal.
    _REVIEW_ROW = (
        "note=completed-review-blocking,worker_type=review,unit=qa/plan-review,"
        "classifier_source=completion-join-terminal-verdict-v1,"
        "reconcile_reason=typed-review-blocking,"
        "launch_outcome=governed-process-group-drained"
    )

    def review_blocking_row(self, attempt_id, round_no, *, note=None, artifact=True, directory=None):
        """One finished plan-check review round: exact log + readable in-root artifact."""
        review = (directory or self.artifact) / "_internal" / "plan_reviews" / f"round_{round_no}.md"
        review.parent.mkdir(parents=True, exist_ok=True)
        if artifact:
            review.write_text(f"## Plan Review Results\nround {round_no}: 1 blocking finding\n",
                              encoding="utf-8")
        log = self.logs / f"{attempt_id}.claude.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            json.dumps({"type": "system", "subtype": "init"}) + "\n"
            + json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "result": f"artifact: {review}\nverdict: FAIL\nblocker: blocking findings",
            }) + "\n",
            encoding="utf-8",
        )
        extra = self._REVIEW_ROW
        if note is not None:
            extra = extra.replace("note=completed-review-blocking", f"note={note}")
        extra += f",log_file={log},artifact_root={self.artifact}"
        self.write_row("done", f"plan-check-r{round_no}", attempt_id, extra, node_id="plan-check")
        self._reap_real_process(attempt_id)
        return review

    def owner_closure(self, route, name="round_2.owner-closure.md", *, attempts=(), artifacts=(),
                      verdict="closed-by-owner", node="plan-check", directory=None):
        gate = next(n["completion_gate"] for n in route["nodes"] if n["id"] == node)
        memo = (directory or (self.artifact / "_internal" / "plan_reviews")) / name
        memo.parent.mkdir(parents=True, exist_ok=True)
        rows = "\n".join(
            f"| {i + 1} | `{attempt}` | `{artifact}` | blocking-findings |"
            for i, (attempt, artifact) in enumerate(zip(attempts, artifacts))
        )
        memo.write_text(
            f"---\nauthor: autopilot-code owner\nnode: {node}\ngate: {gate}\nverdict: {verdict}\n---\n\n"
            "# gate closure (owner judgement)\n\n"
            "| Round | Attempt | Artifact | Reviewer verdict |\n|---|---|---|---|\n"
            f"{rows}\n\nEvery prior blocker closed by one batched correction; the residual is "
            "resolved by owner ruling R-1 and carried to the pipeline summary.\n",
            encoding="utf-8",
        )
        return memo

    def continuation_closure_fixture(self):
        """Real producer admission, official lineage and preserved source FAILs."""
        import artifact_producer as P
        self.jobs = self.stable_dispatch / "jobs.log"
        self.jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs.touch(mode=0o600)
        route = self.compile_route("standard")
        env = self.base_env()
        # Empty issued cycles exercise the cutover contract that the historical
        # same-route fixtures (unbound legacy artifact roots) did not reach.
        with mock.patch.dict(os.environ, env, clear=True):
            source_path = ROUTE.canonical_route_path(self.artifact, route["route_id"])
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(json.dumps(route))
            issued = P.begin(self.artifact, route_file=source_path, capability=route["capability"],
                             intensity=route["effective_intensity"], require_cycle=True)
            source_output = Path(issued["cycle_dir"]) / "artifacts"
            r1 = self.review_blocking_row("att-source-review-r1", 1, directory=source_output)
            r2 = self.review_blocking_row("att-source-review-r2", 2, directory=source_output)
            # review_blocking_row records a real reaped process for each
            # terminal row, shared by same-route and continuation proof.
            continuation = ROUTE.build_continuation_route(
                route, resume_from_node=route["nodes"][0]["id"], requested_boundary=route["nodes"][0]["id"],
                reason="fixture continuation", artifact_root=self.artifact,
            )
            continuation_path = ROUTE.canonical_route_path(self.artifact, continuation["route_id"])
            continuation_path.write_text(json.dumps(continuation))
            current = P.begin(self.artifact, route_file=continuation_path, capability=route["capability"],
                              intensity=route["effective_intensity"], require_cycle=True)
            output = Path(current["cycle_dir"]) / "artifacts"
            memo = self.owner_closure(continuation, directory=output / "_internal/plan_reviews",
                                      attempts=("att-source-review-r1", "att-source-review-r2"),
                                      artifacts=(r1.name, r2.name))
            node = next(n for n in continuation["nodes"] if n["id"] == "plan-check")
            for dependency in node["depends_on"]:
                predecessor = next(n for n in continuation["nodes"] if n["id"] == dependency)
                evidence = output / f"{dependency}.md"
                evidence.write_text("fixture predecessor result\n")
                ROUTE._publish_completion_locked(
                    continuation, predecessor, dependency, evidence, jobs=self.jobs,
                    attempt_id=f"att-inline-{dependency}-fixture", attempt_metadata={
                        "attempt_schema_version": 2, "dispatch_depth": predecessor["dispatch_depth"],
                        "transport": "interactive", "execution_surface": "inline",
                        "registered_worker": False, "fallback_hop": "inline",
                    },
                )
        return route, continuation, continuation_path, node, memo, (r1, r2)

    def test_continuation_closure_preserves_source_and_unblocks_consumer(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        before = self.jobs.read_bytes(), tuple(p.read_bytes() for p in reviews)
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            # D-120: the continuation's `begin` rebound the source's already-open
            # cycle (verified lineage), so the boundary this once tripped is
            # exactly what SD-155/D-120 dissolves -- the source's own route_id
            # now resolves the *shared* bound cycle memo lives in.
            import artifact_producer as P
            output_dir = P.require_cycle_output(self.artifact, memo, route_id=source["route_id"])
            memo.resolve().relative_to(output_dir.resolve())  # no ValueError: memo is inside
            proof = ROUTE.continuation_owner_closure_plan(route, node, memo, self.jobs, "att-source-review-r2")
            self.assertEqual(proof["rounds"], 2)
            target = ROUTE.completion_dir(route["route_id"]) / "plan-check.json"
            self.assertFalse(target.exists())
            marker, receipt = ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertEqual(receipt["source_rows_changed"], 0)
            self.assertEqual(marker["review_independence"], "owner-overridden")
            self.assertFalse(marker["registered_worker"])
            self.assertTrue(D.completion_marker_is_current(route, node, target), marker)
            self.assertEqual(D.completion_attempt_readiness(route, node, marker, self.jobs).state, "ready")
            self.assertTrue(ROUTE._marker_identity_row(route, node, node["id"], node["completion_gate"],
                                                       jobs=self.jobs, exact_terminal=True)["passed"])
            consumer = next(n for n in route["nodes"] if "plan-check" in n.get("depends_on", []))
            # All adapters call this shared gate before they can spawn.
            D.completion_marker_gate(path, consumer["id"], "start", self.agent_home, jobs=self.jobs)
            bytes_before = target.read_bytes()
            again, _ = ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertEqual(marker, again)
            self.assertEqual(target.read_bytes(), bytes_before)
            self.assertFalse(target.with_name("plan-check.2.json").exists())
            self.assertFalse((ROUTE.completion_dir(source["route_id"]) / "plan-check.json").exists())
        self.assertEqual(before, (self.jobs.read_bytes(), tuple(p.read_bytes() for p in reviews)))

    def test_owner_workflow_gaps_sees_no_gap_behind_a_real_owner_closure_marker(self):
        """SD-153 defect #1 (plan §3 B-1): an owner-executed terminal node
        (e.g. autopilot-spec's `prd-transaction`) that depends on a review
        node closed by owner-closure must see NO gap in
        `dispatch_terminal_commit.owner_workflow_gaps` -- through the real
        `capability-route complete` path (never a hand-written JSON marker or
        a mocked `_route_module`, the way `dispatch_terminal_commit.test.py`'s
        `_TerminalCommitFixture` would). P3's handoff could not fit this
        scenario into that fixture (it mocks the whole route module) and left
        it an open item; this reuses the review-closure apparatus already
        proven by `test_registered_review_shape_passes_exact_terminal_identity`
        for the real-path fixture it asked for.

        `owner_workflow_gaps` walks every node `plan-check` transitively
        depends on (`frame`/`frame-alternative`/`plan`) AND separately checks
        every OTHER declared terminal node in the route (`report`) -- so the
        route here is trimmed to just the ancestor chain plus the synthetic
        owner-terminal, and the three ancestors get the same trivial inline
        completion `continuation_closure_fixture` already uses for a review
        node's own immediate dependency (`registered_worker: False` makes
        `completion_attempt_readiness` "ready" without a live registry row).
        """
        self.jobs = self.stable_dispatch / "jobs.log"
        self.jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs.touch(mode=0o600)
        route = self.compile_route()
        self.write_route(route)
        by_id = {n["id"]: n for n in route["nodes"]}
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            for ancestor_id in ("frame", "frame-alternative", "plan", "plan-alternative"):
                ancestor = by_id[ancestor_id]
                evidence = self.base / f"{ancestor_id}.md"
                evidence.write_text(f"{ancestor_id} fixture output\n", encoding="utf-8")
                ROUTE._publish_completion_locked(
                    route, ancestor, ancestor_id, evidence, jobs=self.jobs,
                    attempt_id=f"att-inline-{ancestor_id}-fixture", attempt_metadata={
                        "attempt_schema_version": 2, "dispatch_depth": ancestor["dispatch_depth"],
                        "transport": "interactive", "execution_surface": "inline",
                        "registered_worker": False, "fallback_hop": "inline",
                    },
                )
            r1 = self.review_blocking_row("att-shape-owner-r1", 1)
            r2 = self.review_blocking_row("att-shape-owner-r2", 2)
            self._reap_real_process("att-shape-owner-r2")
            memo = self.owner_closure(route, attempts=("att-shape-owner-r1", "att-shape-owner-r2"),
                                      artifacts=(r1.name, r2.name))
            node = by_id["plan-check"]
            marker, _receipt = ROUTE.complete_node(route, node, "plan-check", memo,
                                                    jobs=self.jobs, attempt_id="att-shape-owner-r2")
            self.assertEqual(marker["review_independence"], "owner-overridden")
            sync_node = {
                "id": "prd-transaction", "kind": "capability-owner", "unit": "_kernel/owner",
                "dispatch_depth": 1, "terminal": True, "depends_on": ["plan-check"],
                "completion_gate": "prd-transaction-complete",
            }
            trimmed_route = {
                **route,
                "nodes": [by_id["frame"], by_id["frame-alternative"], by_id["plan"],
                          by_id["plan-alternative"], node, sync_node],
            }
            owner_metadata = {
                "workflow_completion": "runtime-v1", "worker_type": "owner",
                "dispatch_depth": "1", "attempt_id": "att-owner-sync-fixture", "failure_class": "pass",
            }
            import dispatch_terminal_commit as T
            gaps = T.owner_workflow_gaps(self.jobs, owner_metadata, trimmed_route)
        self.assertEqual(gaps, {})

    def test_a_sd153_4_blocked_smoke_rounds_close_through_owner_inline(self):
        """A-SD153-4 (artifact-path-contract-neighboring stage-dispatch
        §13.59.6): the only recipe node with `id == "smoke"`
        (`autopilot-lab` setup mode, `capabilities/topologies.json`) is a
        `review-worker` in `ROUND_CAPPED_NODE_IDS`. Two `dead-worker-blocked`
        rounds in a row bind the node to `review-verdictless-bound` at
        launch admission; the owner then closes it through the same
        unregistered/explicit-axes inline path `continuation_closure_fixture`
        already uses for a dependency completion. The published marker must
        carry `round_census` naming the closure class, and the downstream
        node's start must be admitted off that marker alone."""
        route = self.compile_route(intensity="standard", capability="autopilot-lab",
                                    capability_mode="setup", signals=["smoke-required"])
        route_path = self.write_route(route)
        by_id = {n["id"]: n for n in route["nodes"]}
        node = by_id["smoke"]
        self.assertEqual(node["kind"], "review-worker")
        self.assertEqual(by_id["full-run"]["depends_on"], ["smoke"])
        for round_no in (1, 2):
            self.write_row("done", f"smoke-r{round_no}", f"att-smoke-r{round_no}",
                           "worker_type=review,note=dead-worker-blocked,failure_class=blocked",
                           node_id="smoke")

        node_spec = importlib.util.spec_from_file_location(
            "a_sd153_4_dispatch_node", ROOT / "utilities/dispatch-node.py")
        dispatch = importlib.util.module_from_spec(node_spec)
        node_spec.loader.exec_module(dispatch)
        budget = dispatch.admit_round(route, node, self.jobs).budget
        self.assertEqual(budget.state, "verdictless-bound")
        self.assertEqual(budget.verdict_rounds, 0)
        self.assertEqual(budget.verdictless_rounds, 2)
        self.assertEqual(budget.cap, 2)
        self.assertEqual(budget.next_action, "native-subagent")

        evidence = self.base / "smoke-attestation.json"
        evidence.write_text("{}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker = ROUTE._publish_completion_locked(
                route, node, "smoke", evidence, jobs=self.jobs,
                attempt_id="att-owner-inline-smoke", attempt_metadata={
                    "attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                    "transport": "interactive", "execution_surface": "inline",
                    "registered_worker": False, "fallback_hop": "inline",
                },
            )
            self.assertEqual(marker.get("round_census"), {
                "verdict_rounds": 0, "verdictless_rounds": 2, "cap": 2,
                "closure_class": "review-verdictless-bound",
            })
            canonical_path = ROUTE.completion_dir(route["route_id"]) / "smoke.json"
            canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
            self.assertEqual(canonical["round_census"]["closure_class"], "review-verdictless-bound")
            # The downstream node's start reads the marker alone -- the two
            # dead rows never closed through a completion, but that no longer
            # matters once the owner-inline marker exists and is current.
            D.completion_marker_gate(route_path, "full-run", "start", self.agent_home, jobs=self.jobs)

    # SD-153 rule 5 correction round -- every ROUND_CAPPED_NODE_IDS marker
    # (not only the review-worker verdictless-bound inline case above)
    # carries round_census. Each test below closes one 🔴 shape the phase
    # review named as missing.

    def test_a_sd153_5a_registered_review_verdict_marker_carries_round_census(self):
        """A registered review worker's own first-pass PASS: `round_census`
        counts this exact landing round as the one verdict round even though
        its own registry row is still `open` (not yet marked `done`) at the
        moment the marker is written -- `write_completion_marker` excludes
        the completing attempt's own row rather than reading it "live" a
        beat early, and folds it back in as `verdict_rounds=1`."""
        route = self.compile_route(intensity="standard")
        self.write_route(route, "route-census-registered-review.json")
        self.current_route = route
        by_id = {n["id"]: n for n in route["nodes"]}
        node = by_id["plan-check"]
        self.write_row("open", "plan-check-r1", "att-census-pc-r1", "worker_type=review", node_id="plan-check")
        evidence = self.base / "plan-check.md"
        evidence.write_text("plan-check passes\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker, _ = ROUTE.complete_node(route, node, "plan-check", evidence, self.jobs, "att-census-pc-r1")
        self.assertEqual(marker.get("round_census"), {
            "verdict_rounds": 1, "verdictless_rounds": 0, "cap": 2,
            "closure_class": "registered-verdict",
        })

    def test_a_sd153_5b_registered_test_node_verdict_marker_carries_round_census(self):
        """The same registered-verdict shape on the `test` node -- SD-153
        rule 5 explicitly includes `test` even though its `kind` is
        `pipeline-stage`, not `review-worker` (C-14's declared exception)."""
        route = self.compile_route(intensity="standard")
        self.write_route(route, "route-census-registered-test.json")
        self.current_route = route
        by_id = {n["id"]: n for n in route["nodes"]}
        node = by_id["test"]
        self.assertEqual(node["kind"], "pipeline-stage")
        self.write_row("open", "test-r1", "att-census-test-r1", "worker_type=test", node_id="test")
        evidence = self.base / "test.md"
        evidence.write_text("tests pass\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker, _ = ROUTE.complete_node(route, node, "test", evidence, self.jobs, "att-census-test-r1")
        self.assertEqual(marker.get("round_census"), {
            "verdict_rounds": 1, "verdictless_rounds": 0, "cap": 2,
            "closure_class": "registered-verdict",
        })

    def test_a_sd153_5c_owner_closure_marker_carries_round_census(self):
        """SD-124 owner-closure over two exhausted blocking rounds:
        `closure_class="owner-closure"` unconditionally, with the real
        verdict_rounds the two blocking rows spent."""
        self.jobs = self.stable_dispatch / "jobs.log"
        self.jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs.touch(mode=0o600)
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, "route-census-owner-closure.json")
        self.current_route = route
        by_id = {n["id"]: n for n in route["nodes"]}
        r1 = self.review_blocking_row("att-census-oc-r1", 1)
        r2 = self.review_blocking_row("att-census-oc-r2", 2)
        memo = self.owner_closure(route, name="census_oc.owner-closure.md",
                                  attempts=("att-census-oc-r1", "att-census-oc-r2"),
                                  artifacts=(r1.name, r2.name))
        node = by_id["plan-check"]
        closed = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-census-oc-r2")
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        marker = json.loads(closed.stdout.strip().splitlines()[0])
        self.assertEqual(marker["review_independence"], "owner-overridden")
        self.assertEqual(marker.get("round_census"), {
            "verdict_rounds": 2, "verdictless_rounds": 0, "cap": 2,
            "closure_class": "owner-closure",
        })

    def test_a_sd153_5d_test_node_owner_run_verdictless_marker_carries_round_census(self):
        """SD-153 rule 3's bound, on a non-review capped node: two verdict-
        less `test` rounds bind the node, and the owner running the test
        directly (command + output as evidence) is recorded
        `closure_class="owner-run-verdictless"` -- distinct from the
        review-worker `smoke` case above."""
        route = self.compile_route(intensity="standard")
        self.write_route(route, "route-census-test-verdictless.json")
        self.current_route = route
        by_id = {n["id"]: n for n in route["nodes"]}
        node = by_id["test"]
        for round_no in (1, 2):
            self.write_row("done", f"test-r{round_no}", f"att-test-r{round_no}",
                           "worker_type=test,note=dead-invalid-envelope", node_id="test")
        evidence = self.base / "test-owner-run.md"
        evidence.write_text(
            "owner ran the tests directly.\ncommand: python3 -m pytest\noutput: 42 passed\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker = ROUTE._publish_completion_locked(
                route, node, "test", evidence, jobs=self.jobs,
                attempt_id="att-owner-run-test", attempt_metadata={
                    "attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                    "transport": "interactive", "execution_surface": "inline",
                    "registered_worker": False, "fallback_hop": "inline",
                },
            )
        self.assertEqual(marker.get("round_census"), {
            "verdict_rounds": 0, "verdictless_rounds": 2, "cap": 2,
            "closure_class": "owner-run-verdictless",
        })

    def test_a_sd153_5e_inline_override_over_unresolved_fail_with_budget_remaining(self):
        """An inline completion landing over ONE unresolved blocking FAIL,
        with budget still open (`verdict_rounds=1 < cap=2`) -- SD-153 rule 5
        / SD-134 A75-9: `closure_class="owner-override-unlinked"` regardless
        of remaining budget, and the gate is recorded `degraded` (not
        `owner-overridden`, since this bypassed the evidence-linked owner-
        closure path entirely)."""
        route = self.compile_route(intensity="standard")
        self.write_route(route, "route-census-override-remaining.json")
        self.current_route = route
        by_id = {n["id"]: n for n in route["nodes"]}
        node = by_id["plan-check"]
        self.review_blocking_row("att-override-remaining-r1", 1)
        evidence = self.base / "override-remaining.md"
        evidence.write_text("owner completes inline without an evidence-linked closure\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker = ROUTE._publish_completion_locked(
                route, node, "plan-check", evidence, jobs=self.jobs,
                attempt_id="att-override-remaining", attempt_metadata={
                    "attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                    "transport": "interactive", "execution_surface": "inline",
                    "registered_worker": False, "fallback_hop": "inline",
                },
            )
        self.assertEqual(marker["review_independence"], "degraded")
        self.assertEqual(marker.get("round_census"), {
            "verdict_rounds": 1, "verdictless_rounds": 0, "cap": 2,
            "closure_class": "owner-override-unlinked",
        })

    def test_a_sd153_5f_inline_override_over_unresolved_fail_with_budget_spent(self):
        """The same override shape, budget already exhausted
        (`verdict_rounds=2 >= cap=2`) -- "예산이 남아 있든 소진됐든" still
        `owner-override-unlinked`, and the closed route's degraded list
        names the node (SD-134 A75-9's existing plumbing)."""
        route = self.compile_route(intensity="standard")
        route_path = ROUTE.canonical_route_path(self.artifact, route["route_id"])
        route_path.parent.mkdir(parents=True, exist_ok=True)
        route_path.write_text(json.dumps(route), encoding="utf-8")
        self.current_route = route
        by_id = {n["id"]: n for n in route["nodes"]}
        node = by_id["plan-check"]
        self.review_blocking_row("att-override-spent-r1", 1)
        self.review_blocking_row("att-override-spent-r2", 2)
        evidence = self.base / "override-spent.md"
        evidence.write_text("owner completes inline without an evidence-linked closure\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker = ROUTE._publish_completion_locked(
                route, node, "plan-check", evidence, jobs=self.jobs,
                attempt_id="att-override-spent", attempt_metadata={
                    "attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                    "transport": "interactive", "execution_surface": "inline",
                    "registered_worker": False, "fallback_hop": "inline",
                },
            )
            self.assertEqual(marker["review_independence"], "degraded")
            self.assertEqual(marker.get("round_census"), {
                "verdict_rounds": 2, "verdictless_rounds": 0, "cap": 2,
                "closure_class": "owner-override-unlinked",
            })
            outcome, _ = ROUTE.close_route(route, route_path, commit="a" * 40, allow_unproven=True, jobs=self.jobs)
        self.assertIn("plan-check", outcome.get("review_independence_degraded", []))

    def test_a_sd153_5g_plain_inline_completion_with_no_unresolved_fail(self):
        """A plain inline completion with no prior round history at all --
        no verdict, no unresolved FAIL, no bound streak --
        `closure_class="inline-no-unresolved-fail"`."""
        route = self.compile_route(intensity="standard")
        self.write_route(route, "route-census-inline-plain.json")
        self.current_route = route
        by_id = {n["id"]: n for n in route["nodes"]}
        node = by_id["plan-check"]
        evidence = self.base / "inline-plain.md"
        evidence.write_text("owner completes inline, nothing to override\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker = ROUTE._publish_completion_locked(
                route, node, "plan-check", evidence, jobs=self.jobs,
                attempt_id="att-inline-plain", attempt_metadata={
                    "attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                    "transport": "interactive", "execution_surface": "inline",
                    "registered_worker": False, "fallback_hop": "inline",
                },
            )
        self.assertEqual(marker.get("round_census"), {
            "verdict_rounds": 0, "verdictless_rounds": 0, "cap": 2,
            "closure_class": "inline-no-unresolved-fail",
        })

    def test_a_sd154_4_one_closure_check_after_budget(self):
        """A-SD154-4 (13.59.6): standard `plan-check` spends its verdict
        budget on two blocking rounds (cap=2 -> exhausted). An explicit
        `revise` on `plan` naming the last blocking round as its answer makes
        the NEXT `plan-check` admission a one-time `closure-check` (13.59.3
        rule 7). A second attempt at the same trick -- calling `admit_round`
        again without a NEW revision naming the newest blocking round -- falls
        straight back to `review-round-budget-exhausted`. The closure-check
        round itself may still end blocking; owner-closure still closes the
        node once its (now three-round) budget is exhausted."""
        self.jobs = self.stable_dispatch / "jobs.log"
        self.jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs.touch(mode=0o600)
        route = self.compile_route(intensity="standard")
        route_path = self.write_route(route, "route-revise-4.json")
        by_id = {n["id"]: n for n in route["nodes"]}
        plan_evidence = self.base / "plan.md"
        plan_evidence.write_text("plan v1\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            ROUTE._publish_completion_locked(
                route, by_id["plan"], "plan", plan_evidence, jobs=self.jobs,
                attempt_id="att-sd154-4-plan-1", attempt_metadata={
                    "attempt_schema_version": 2, "dispatch_depth": by_id["plan"]["dispatch_depth"],
                    "transport": "interactive", "execution_surface": "inline",
                    "registered_worker": False, "fallback_hop": "inline",
                },
            )
        r1 = self.review_blocking_row("att-sd154-4-r1", 1)
        r2 = self.review_blocking_row("att-sd154-4-r2", 2)
        plan_check_node = by_id["plan-check"]

        budget = DISPATCH_NODE.admit_round(route, plan_check_node, self.jobs,
                                           owner_attempt_id="att-sd154-4-owner").budget
        self.assertEqual(budget.state, "exhausted")
        self.assertEqual(budget.round_kind, "correction")

        plan_evidence.write_text("plan v2 (corrected per r2)\n", encoding="utf-8")
        revised = self.revise(
            route_path, "plan", plan_evidence,
            basis="review-findings", answers=["att-sd154-4-r2"], jobs=self.jobs,
        )
        self.assertEqual(revised.returncode, 0, revised.stdout + revised.stderr)

        # Exactly one closure-check is admitted off that revision.
        admission = DISPATCH_NODE.admit_round(route, plan_check_node, self.jobs,
                                              owner_attempt_id="att-sd154-4-owner")
        self.assertEqual(admission.budget.state, "admit")
        self.assertEqual(admission.budget.round_kind, "closure-check")
        self.assertTrue(admission.budget.closure_check_used)
        self.assertEqual(admission.auto_revisions, ())  # plan is already current -- nothing auto-recorded

        # The closure-check round itself runs and is blocking again (r3).
        r3 = self.review_blocking_row("att-sd154-4-r3", 3)

        # A second closure-check needs its OWN revision naming r3; none exists,
        # so this falls back to the ordinary exhausted refusal.
        second = DISPATCH_NODE.admit_round(route, plan_check_node, self.jobs,
                                           owner_attempt_id="att-sd154-4-owner")
        self.assertEqual(second.budget.state, "exhausted")
        self.assertEqual(second.budget.round_kind, "correction")

        # The closure-check round's FAIL still lets owner-closure close the
        # node -- the (now three-round) verdict budget is exhausted either way.
        memo = self.owner_closure(
            route, name="round_3.owner-closure.md",
            attempts=("att-sd154-4-r1", "att-sd154-4-r2", "att-sd154-4-r3"),
            artifacts=(r1.name, r2.name, r3.name),
        )
        closed = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-sd154-4-r3")
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        marker = json.loads(closed.stdout.strip().splitlines()[0])
        self.assertEqual(marker["review_independence"], "owner-overridden")

    def test_continuation_source_locator_does_not_change_lineage_hash(self):
        """A CLI filesystem path is invocation context, never route identity."""
        source, _route, path, _node, _memo, _reviews = self.continuation_closure_fixture()
        verified = ROUTE.verify_route(json.loads(path.read_text()))
        original_hash = verified["route_hash"]
        verified["route_file"] = str(path)
        self.assertEqual(ROUTE.route_hash(verified), original_hash)
        verified.pop("route_file")
        continuation = ROUTE.build_continuation_route(
            verified, resume_from_node="test", requested_boundary="test",
            reason="locator regression", artifact_root=self.artifact,
        )
        self.assertEqual(verified["route_hash"], original_hash)
        self.assertEqual(continuation["source_route_hash"], original_hash)

    def test_continuation_closure_refuses_missing_dependency_and_live_round(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            dependency = ROUTE.completion_dir(route["route_id"]) / f"{node['depends_on'][0]}.json"
            original = dependency.read_bytes()
            dependency.unlink()
            with self.assertRaisesRegex(ValueError, "dependency-unproven"):
                ROUTE.continuation_owner_closure_plan(route, node, memo, self.jobs, "att-source-review-r2")
            dependency.write_bytes(original)
            self.current_route = route
            self.write_row("open", "new-review", "att-current-review", "worker_type=review", node_id="plan-check")
            before = self.jobs.read_bytes()
            with self.assertRaisesRegex(ValueError, "round-still-open"):
                ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertEqual(self.jobs.read_bytes(), before)
            self.assertFalse((ROUTE.completion_dir(route["route_id"]) / "plan-check.json").exists())

    def test_continuation_closure_recovers_interrupted_publication(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            target = ROUTE.completion_dir(route["route_id"]) / "plan-check.json"
            before = self.jobs.read_bytes()
            write = ROUTE.atomic_write
            def crash_after_history(path, value):
                if Path(path) == target:
                    raise OSError("fixture crash after immutable history")
                return write(path, value)
            with mock.patch.object(ROUTE, "atomic_write", side_effect=crash_after_history):
                with self.assertRaisesRegex(OSError, "fixture crash"):
                    ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertFalse(target.exists())
            history = target.with_name("plan-check.1.json").read_bytes()
            marker, _ = ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertEqual(marker["sequence"], 1)
            self.assertEqual(target.with_name("plan-check.1.json").read_bytes(), history)
            self.assertFalse(target.with_name("plan-check.2.json").exists())
            target.unlink()  # Also recover when the exact attempt link exists.
            again, _ = ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertEqual(again, marker)
            self.assertTrue(D.completion_marker_is_current(route, node, target))
            link = ROUTE._attempt_completion_path(route, node["id"], "att-source-review-r2", jobs=self.jobs)
            original_link = link.read_bytes()
            link.unlink()
            self.assertFalse(D.completion_marker_is_current(route, node, target))
            ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertEqual(link.read_bytes(), original_link)
            self.assertTrue(D.completion_marker_is_current(route, node, target))
            self.assertEqual(self.jobs.read_bytes(), before)

    def test_sd161_input_revision_partial_write_never_publishes_broken_history(self):
        import artifact_receipt
        route, node, output, owner = self.sd161_input_fixture()
        evidence = output / "plan.md"
        evidence.write_text("v1")
        self.review_blocking_row("att-input-r1", 1, directory=output)
        self.sd161_bind_row(route, "att-input-r1", evidence)
        evidence.write_text("v2")
        directory = self.jobs.parent / "review-input-revisions" / route["route_id"] / node["id"]
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            with mock.patch.object(artifact_receipt.os, "fsync", side_effect=OSError("interrupted temporary write")):
                with self.assertRaisesRegex(OSError, "interrupted temporary write"):
                    ROUTE.publish_review_input_revision(route, node["id"], evidence,
                        answers=("att-input-r1",), author_attempt_id=owner, jobs=self.jobs)
            self.assertFalse(list(directory.glob("*.json")))
            # A process killed before finally cleanup may leave a private temp;
            # history enumeration and the next sequence must ignore it.
            (directory / ".interrupted.tmp").write_text('{"schema_version":')
            self.assertEqual(ROUTE._review_input_revision_records(route, node["id"], self.jobs), [])
            result = ROUTE.publish_review_input_revision(route, node["id"], evidence,
                answers=("att-input-r1",), author_attempt_id=owner, jobs=self.jobs)
            self.assertEqual(result["input_revision"]["sequence"], 1)
            self.assertEqual(json.loads((directory / "000001.json").read_text()), result["input_revision"])

    def test_sd161_check_and_writer_share_terminal_claim_fence_without_check_mutation(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        claim = D.terminal_claim_path(self.jobs, route["route_id"], "att-source-review-r2")
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_text(json.dumps({"schema_version": 1, "route_id": route["route_id"],
                                    "owner_attempt_id": "att-source-review-r2"}))
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in self.base.rglob("*") if p.is_file()}
            with self.assertRaises(D.DispatchContractError) as checked:
                ROUTE.owner_closure_plan(route, node, memo, self.jobs, "att-source-review-r2")
            self.assertEqual(checked.exception.reason, "terminal-claim-conflict")
            self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                                      for p in self.base.rglob("*") if p.is_file()})
            with self.assertRaises(D.DispatchContractError) as written:
                ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertEqual(written.exception.reason, checked.exception.reason)
            self.assertFalse((ROUTE.completion_dir(route["route_id"]) / "plan-check.json").exists())

    def test_sd161_check_and_writer_share_stage_authority_and_metadata_seal_fences(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        source_node = next(n for n in source["nodes"] if n["id"] == node["id"])
        original = self.jobs.read_text()
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            for suffix, reason in ((",stage_authority=0", "row-contract-invalid:stage-authority-zero-without-subsession"),
                                   (",route_node=plan-check", "owner-closure-seal-refused:attempt-immutable-metadata-duplicate")):
                self.jobs.write_text("\n".join(line + (suffix if "attempt_id=att-source-review-r2," in line else "")
                                                for line in original.splitlines()) + "\n")
                for action in (ROUTE.owner_closure_plan,
                               lambda r,n,e,j,a: ROUTE.complete_node(r,n,n["id"],e,j,a)):
                    with self.assertRaisesRegex(ValueError, reason):
                        action(source, source_node, memo, self.jobs, "att-source-review-r2")
            self.assertFalse((ROUTE.completion_dir(source["route_id"]) / "plan-check.json").exists())

    def test_sd161_exact_completed_owner_closure_replay_is_read_only_and_evidence_bound(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        source_node = next(n for n in source["nodes"] if n["id"] == node["id"])
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker, _ = ROUTE.complete_node(source, source_node, node["id"], memo, self.jobs, "att-source-review-r2")
            before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in self.base.rglob("*") if p.is_file()}
            proof = ROUTE.owner_closure_plan(source, source_node, memo, self.jobs, "att-source-review-r2")
            self.assertTrue(proof["already_completed"])
            self.assertEqual(proof["marker"], marker)
            self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                                      for p in self.base.rglob("*") if p.is_file()})
            replayed, _ = ROUTE.complete_node(source, source_node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertEqual(replayed, marker)
            memo.write_text(memo.read_text() + "\nchanged disposition\n")
            for action in (ROUTE.owner_closure_plan,
                           lambda r,n,e,j,a: ROUTE.complete_node(r,n,n["id"],e,j,a)):
                with self.assertRaisesRegex(ValueError, "immutable attempt completion differs"):
                    action(source, source_node, memo, self.jobs, "att-source-review-r2")
            self.assertEqual(marker["review_independence"], "owner-overridden")

    def test_sd161_same_route_check_is_read_only_and_matches_writer(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        source_path = ROUTE.canonical_route_path(self.artifact, source["route_id"])
        source_node = next(n for n in source["nodes"] if n["id"] == "plan-check")
        def snapshot():
            return {str(p.relative_to(self.base)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for root in (self.artifact, self.stable_dispatch) for p in root.rglob("*") if p.is_file()}
        before = snapshot()
        command = [sys.executable, str(ROOT / "utilities/capability-route.py"), "complete", "--check",
                   "--route", str(source_path), "--node", "plan-check", "--evidence", str(memo),
                   "--jobs", str(self.jobs), "--attempt-id", "att-source-review-r2"]
        result = subprocess.run(command, env=self.base_env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(snapshot(), before)
        checked = json.loads(result.stdout)["owner_closure_proof"]
        self.assertIn("closure", checked)
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker, receipt = ROUTE.complete_node(source, source_node, "plan-check", memo,
                                                  self.jobs, "att-source-review-r2")
        self.assertEqual(marker["review_independence"], "owner-overridden")
        self.assertEqual(receipt["blocking_attempts"], checked["closure"]["blocking_attempts"])
        self.assertIn("note=completed-review-blocking", self.jobs.read_text())

    def test_sd161_continuation_current_attempt_uses_same_route_proof(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        self.current_route = route
        current_review = self.review_blocking_row("att-current-review-r3", 3, directory=reviews[0].parents[2])
        memo = self.owner_closure(route, attempts=("att-source-review-r1", "att-source-review-r2", "att-current-review-r3"),
                                  artifacts=tuple(p.name for p in reviews) + (current_review.name,), directory=memo.parent)
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            proof = ROUTE.owner_closure_plan(route, node, memo, self.jobs, "att-current-review-r3")
            self.assertEqual(proof["rounds"], 3)
            self.assertIn("closure", proof)
            marker, receipt = ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-current-review-r3")
            self.assertTrue(marker["registered_worker"])
            self.assertEqual(marker["review_independence"], "owner-overridden")
            self.assertEqual(len(receipt["blocking_attempts"]), 3)
            self.assertEqual(marker["round_census"]["verdict_rounds"], 3)

    def sd161_input_fixture(self):
        import artifact_producer as P
        self.jobs = self.stable_dispatch / "jobs.log"
        self.jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs.touch(mode=0o600)
        base = self.compile_route("standard")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            route = ROUTE.compose_route(
                capability="autopilot-code", capability_mode="dev", shape="staged",
                graph="plan-check,execute,impl-review,test,report", slug="sd161-input",
                cwd=self.repo, artifact_root=self.artifact, intensity="standard", unassigned=True,
                spec_read="fixture", dispatch_evidence=base["dispatch_evidence"], jobs=self.jobs,
            )
            path = ROUTE.canonical_route_path(self.artifact, route["route_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(route))
            issued = P.begin(self.artifact, route_file=path, capability=route["capability"],
                             intensity=route["effective_intensity"], require_cycle=True)
        self.current_route = route
        owner = "att-sd161-owner"
        with self.jobs.open("a") as stream:
            stream.write(f"2026-09-28T00:00:00Z\topen\t{self.repo}\t{self.repo}\towner\t"
                         f"attempt_id={owner},worker_type=owner,dispatch_depth=1,registered_worker=1,"
                         f"owner_route_file={path},owner_route_id={route['route_id']},owner_route_hash={route['route_hash']}\n")
        return route, next(n for n in route["nodes"] if n["id"] == "plan-check"), Path(issued["cycle_dir"]) / "artifacts", owner

    def sd161_bind_row(self, route, attempt, evidence):
        import review_input
        metadata = {"attempt_id": attempt, "route_id": route["route_id"],
                    "route_hash": route["route_hash"], "route_node": "plan-check"}
        candidate = review_input.resolve_input(route, next(n for n in route["nodes"] if n["id"] == "plan-check"),
                                              self.jobs, evidence)
        digest = review_input.seal_binding(self.jobs, metadata, candidate)
        self.jobs.write_text("\n".join(line + (",review_input_digest=" + digest if f"attempt_id={attempt}," in line else "")
                                      for line in self.jobs.read_text().splitlines()) + "\n")

    def test_sd161_input_revision_is_not_completion_and_total_verdicts_are_bounded(self):
        route, node, output, owner = self.sd161_input_fixture()
        evidence = output / "plan.md"
        evidence.write_text("plan v1\n")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            for number in (1, 2, 3):
                attempt = f"att-input-r{number}"
                self.review_blocking_row(attempt, number, directory=output)
                self.sd161_bind_row(route, attempt, evidence)
                evidence.write_text(f"plan v{number + 1}\n")
                original = self.jobs.read_bytes()
                result = ROUTE.publish_revision_locked(route, node["id"], evidence, basis="review-findings",
                    answers=(attempt,), author_attempt_id=owner, jobs=self.jobs)
                self.assertNotIn("marker", result)
                self.assertEqual(original, self.jobs.read_bytes())
                self.assertFalse((ROUTE.completion_dir(route["route_id"], jobs=self.jobs) / "plan-check.json").exists())
                history = self.jobs.parent / "review-input-revisions" / route["route_id"] / node["id"]
                before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in history.glob("*.json")}
                self.assertEqual(result, ROUTE.publish_review_input_revision(route, node["id"], evidence,
                    answers=(attempt,), author_attempt_id=owner, jobs=self.jobs))
                self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in history.glob("*.json")})
                rows = ROUTE._review_round_rows(self.jobs.read_text().splitlines(), route["route_id"], node["id"], jobs=self.jobs)
                budget = ROUTE.REVIEW_ROUND_CAP.round_budget(route, node, rows,
                    revisions=ROUTE._dependency_revisions(route, node, self.jobs))
                self.assertEqual(budget.state, "admit" if number < 3 else "exhausted")

    def test_sd161_same_route_live_and_unverifiable_process_refuse_check_and_writer(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        source_node = next(n for n in source["nodes"] if n["id"] == "plan-check")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            for state in ("live", "unverifiable"):
                with mock.patch.object(D, "attempt_process_quiescence", return_value=D.ProcessQuiescence(state, "fixture")):
                    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                              for p in self.base.rglob("*") if p.is_file()}
                    with self.assertRaisesRegex(ValueError, f"owner-closure-round-{state}"):
                        ROUTE.owner_closure_plan(source, source_node, memo, self.jobs, "att-source-review-r2")
                    self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                                              for p in self.base.rglob("*") if p.is_file()})
                    with self.assertRaisesRegex(ValueError, f"owner-closure-round-{state}"):
                        ROUTE.complete_node(source, source_node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertFalse((ROUTE.completion_dir(source["route_id"], jobs=self.jobs) / "plan-check.json").exists())

    def test_sd161_input_revision_recovery_and_current_input_filter(self):
        import review_input
        route, node, output, owner = self.sd161_input_fixture()
        evidence = output / "plan.md"
        evidence.write_text("v1")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            self.review_blocking_row("att-input-r1", 1, directory=output)
            self.sd161_bind_row(route, "att-input-r1", evidence)
            self.review_blocking_row("att-input-r2", 2, directory=output)
            self.sd161_bind_row(route, "att-input-r2", evidence)
            old = review_input.resolve_input(route, node, self.jobs, evidence)
            evidence.write_text("v2")
            revised = review_input.resolve_input(route, node, self.jobs, evidence)
            ROUTE.publish_review_input_revision(route, node["id"], evidence, answers=("att-input-r2",),
                                                author_attempt_id=owner, jobs=self.jobs)
            self.assertEqual(ROUTE._dependency_revisions(route, node, self.jobs, reviewed_input=old), [])
            self.assertEqual(len(ROUTE._dependency_revisions(route, node, self.jobs, reviewed_input=revised)), 1)
            recovery = ROUTE.REVIEW_ROUND_CAP.recovery_fields("review-worker", route=route, node=node, jobs=self.jobs)
            self.assertIn("--answers att-input-r2", recovery["recovery_revise_command"])
            # An old FAIL without its sealed input can still use owner closure,
            # but cannot invent an input revision from the review's output.
            self.jobs.write_text(self.jobs.read_text().replace(",review_input_digest=", ",legacy_input_digest="))
            recovery = ROUTE.REVIEW_ROUND_CAP.recovery_fields("review-worker", route=route, node=node, jobs=self.jobs)
            self.assertNotIn("recovery_revise_command", recovery)
            self.assertIn("recovery_check_command", recovery)
            with self.assertRaisesRegex(ValueError, "review-input-revision-source-mismatch"):
                ROUTE._dependency_revisions(route, node, self.jobs)

    def test_sd161_continuation_cannot_reset_input_revision_budget(self):
        import artifact_producer as P
        route, node, output, owner = self.sd161_input_fixture()
        evidence = output / "plan.md"
        evidence.write_text("v1")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            for number in (1, 2, 3):
                attempt = f"att-lineage-r{number}"
                self.review_blocking_row(attempt, number, directory=output)
                self.sd161_bind_row(route, attempt, evidence)
                evidence.write_text(f"v{number + 1}")
                ROUTE.publish_review_input_revision(route, node["id"], evidence, answers=(attempt,),
                                                    author_attempt_id=owner, jobs=self.jobs)
            current = ROUTE.build_continuation_route(route, resume_from_node=node["id"],
                requested_boundary=node["id"], reason="fixture correction", artifact_root=self.artifact)
            path = ROUTE.canonical_route_path(self.artifact, current["route_id"])
            path.write_text(json.dumps(current))
            P.begin(self.artifact, route_file=path, capability=current["capability"],
                    intensity=current["effective_intensity"], require_cycle=True)
            current_node = next(n for n in current["nodes"] if n["id"] == node["id"])
            admission = DISPATCH_NODE.admit_round(current, current_node, self.jobs)
            self.assertEqual(admission.budget.state, "exhausted")
            self.assertEqual(admission.budget.verdict_rounds, 3)
            self.assertEqual(len(ROUTE._dependency_revisions(current, current_node, self.jobs)), 3)
            recovery = ROUTE.REVIEW_ROUND_CAP.recovery_fields("review-worker", route=current,
                node=current_node, jobs=self.jobs, route_file=path)
            self.assertIn("att-lineage-r3", recovery["recovery_check_command"])
            self.assertNotIn("recovery_revise_command", recovery)

    def test_sd161_input_revision_refuses_wrong_answer_owner_unchanged_and_outside_cycle(self):
        route, node, output, owner = self.sd161_input_fixture()
        evidence = output / "plan.md"
        evidence.write_text("v1")
        self.review_blocking_row("att-input-r1", 1, directory=output)
        self.sd161_bind_row(route, "att-input-r1", evidence)
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            def revise(path=evidence, answers=("att-input-r1",), author=owner):
                return ROUTE.publish_review_input_revision(route, node["id"], path,
                    answers=answers, author_attempt_id=author, jobs=self.jobs)
            with self.assertRaisesRegex(ValueError, "revision-evidence-unchanged"):
                revise()
            evidence.write_text("v2")
            with self.assertRaisesRegex(ValueError, "answer-not-current"):
                revise(answers=("att-other",))
            with self.assertRaisesRegex(ValueError, "owner-not-exact"):
                revise(author="att-other")
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-worker"}):
                with self.assertRaisesRegex(ValueError, "owner-caller-mismatch"):
                    revise()
            outside = self.base / "outside.md"
            outside.write_text("v2")
            with self.assertRaises(Exception):
                revise(path=outside)
            binding = self.jobs.parent / "review-inputs/att-input-r1.json"
            data = json.loads(binding.read_text())
            data["sha256"] = "0" * 64
            binding.write_text(json.dumps(data))
            with self.assertRaises(D.DispatchContractError) as failure:
                revise()
            self.assertEqual(failure.exception.reason, "reviewed-evidence-binding-mismatch")
        self.assertFalse((self.jobs.parent / "review-input-revisions").exists())

    def test_continuation_closure_check_cli_is_read_only(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        def snapshot():
            return {str(p.relative_to(self.base)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for root in (self.artifact, self.stable_dispatch) for p in root.rglob("*") if p.is_file()}
        before = snapshot()
        result = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"),
                                 "complete", "--check", "--route", str(path), "--node", node["id"],
                                 "--evidence", str(memo), "--jobs", str(self.jobs),
                                 "--attempt-id", "att-source-review-r2"],
                                env=self.base_env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["read_only"])
        self.assertEqual(snapshot(), before)

    def test_closure_check_on_a_node_that_is_not_a_review_answers_not_applicable(self):
        # Codex r4: a refine owner's read-only `--check` on its `transaction` node was refused with
        # `owner-closure-source-attempt-not-exact`; owner closure exists only for a blocking review.
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        other = next(n for n in route["nodes"] if n.get("kind") != "review-worker")
        def snapshot():
            return {str(p.relative_to(self.base)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for root in (self.artifact, self.stable_dispatch) for p in root.rglob("*") if p.is_file()}
        before = snapshot()
        result = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"),
                                 "complete", "--check", "--route", str(path), "--node", other["id"],
                                 "--evidence", str(memo), "--jobs", str(self.jobs),
                                 "--attempt-id", "att-source-review-r2"],
                                env=self.base_env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual((payload["result"], payload["read_only"], payload["node_id"]),
                         ("not-applicable", True, other["id"]))
        self.assertEqual(snapshot(), before)

    def test_continuation_closure_unknown_conflict_and_changed_review_hold_consumption(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker, _ = ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            target = ROUTE.completion_dir(route["route_id"]) / "plan-check.json"
            original_marker, original_rows = target.read_bytes(), self.jobs.read_bytes()
            with mock.patch.object(D, "attempt_process_quiescence", return_value=D.ProcessQuiescence("unverifiable", "namespace-not-visible")):
                ready = D.completion_attempt_readiness(route, node, marker, self.jobs)
                self.assertEqual(ready.state, "unverifiable")
                self.assertIn("namespace-not-visible", ready.reason)
            # A currently unresolved conflict cannot consume the old receipt.
            with mock.patch.object(D, "terminal_conflict_pending", return_value=True):
                self.assertFalse(D.completion_marker_is_current(route, node, target))
                self.assertEqual(D.completion_attempt_readiness(route, node, marker, self.jobs).state, "unverifiable")
            reviews[0].write_text("changed findings\n")
            self.assertFalse(D.completion_marker_is_current(route, node, target))
            with self.assertRaisesRegex(ValueError, "node-already-complete"):
                ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            self.assertEqual(target.read_bytes(), original_marker)
            self.assertEqual(self.jobs.read_bytes(), original_rows)

    def test_continuation_closure_rejects_sibling_wrong_cycle_and_forged_lineage(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            # D-120: `old_memo` sits under the *source's* cycle dir, and that
            # cycle is exactly the one the continuation's `begin` rebound into
            # (verified lineage) -- so this is no longer "wrong cycle", it is
            # the same cycle by design, and the plan proceeds.
            old_memo = self.owner_closure(source, directory=reviews[0].parent,
                                         attempts=("att-source-review-r1", "att-source-review-r2"),
                                         artifacts=tuple(p.name for p in reviews))
            proof = ROUTE.continuation_owner_closure_plan(route, node, old_memo, self.jobs, "att-source-review-r2")
            self.assertEqual(proof["rounds"], 2)
            forged = copy.deepcopy(route)
            forged["source_route_hash"] = "sha256:" + "f" * 64
            with self.assertRaisesRegex(ValueError, "route-lineage-unverified"):
                ROUTE.continuation_owner_closure_plan(forged, node, memo, self.jobs, "att-source-review-r2")
            source_path = ROUTE.canonical_route_path(self.artifact, source["route_id"])
            original = source_path.read_bytes()
            changed = copy.deepcopy(source)
            next(n for n in changed["nodes"] if n["id"] == node["id"])["unit"] = "qa/another-review"
            source_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "route-lineage-unverified"):
                ROUTE.continuation_owner_closure_plan(route, node, memo, self.jobs, "att-source-review-r2")
            source_path.write_bytes(original)
            self.assertFalse((ROUTE.completion_dir(route["route_id"]) / "plan-check.json").exists())

    def test_continuation_round_census_is_shared_with_dispatch_and_not_reset_by_slug(self):
        source, route, path, node, memo, reviews = self.continuation_closure_fixture()
        spec = importlib.util.spec_from_file_location("closure_dispatch_node", ROOT / "utilities/dispatch-node.py")
        dispatch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dispatch)
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            rounds = dispatch.prior_round_attempts(self.jobs, route["route_id"], node["id"],
                                                   exclude_slug="plan-check-r2", route=route)
            self.assertEqual(len(rounds), 2)
            # SD-153: `prior_round_attempts` now returns (cols, metadata) pairs
            # -- the full row census `round_budget` needs -- not (slug, note).
            self.assertEqual([meta.get("note") for cols, meta in rounds], ["completed-review-blocking"] * 2)

    def test_two_blocking_rounds_with_owner_closure_publish_the_marker(self):
        route = self.compile_route()          # strong -> review round cap 2
        route_path = self.write_route(route)
        r1 = self.review_blocking_row("att-review-r1", 1)
        r2 = self.review_blocking_row("att-review-r2", 2)
        memo = self.owner_closure(route, attempts=("att-review-r1", "att-review-r2"),
                                  artifacts=(r1.name, r2.name))
        result = self.complete(route_path, "plan-check", memo, jobs=self.jobs,
                               attempt_id="att-review-r2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        directory = self.stable_dispatch / "completion" / route["route_id"]
        canonical = directory / "plan-check.json"
        self.assertTrue(canonical.is_file())
        marker = json.loads(canonical.read_text(encoding="utf-8"))
        self.assertEqual(marker["attempt_id"], "att-review-r2")
        self.assertEqual(marker["evidence"]["path"], str(memo.resolve()))
        status, meta = self.read_row("att-review-r2")
        self.assertEqual(status, "done")                        # never re-closed
        self.assertEqual(meta.get("note"), "completed-marker")
        self.assertEqual(meta.get("gate_closure"), "owner-closure")
        self.assertEqual(meta.get("owner_closure"), str(memo.resolve()))
        self.assertTrue(meta.get("review_artifact_b64"))
        self.assertNotEqual(meta.get("failure_class"), "pass")  # a FAIL review never becomes pass
        # round 1 is left exactly as the reviewer ended it
        r1_status, r1_meta = self.read_row("att-review-r1")
        self.assertEqual((r1_status, r1_meta.get("note")), ("done", "completed-review-blocking"))
        self.assertIsNone(r1_meta.get("completion_marker"))
        receipt = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")][-1]
        self.assertEqual(receipt["status"], "marker-appended")
        self.assertEqual(receipt["gate_closure"], "owner-closure")
        self.assertEqual(receipt["blocking_attempts"], ["att-review-r1", "att-review-r2"])
        # idempotent replay: note=completed-marker now wins -> already-closed path, one marker
        again = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-review-r2")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertFalse((directory / "plan-check.2.json").exists())

    def _reap_real_process(self, attempt_id):
        """Stamp one row with a real, reaped session leader's process identity.

        `attempt_process_quiescence` needs actual pid/namespace fields to call
        a row quiescent; a synthetic `done` row with none is `unverifiable`,
        not `ready`. No namespace mismatch or missing PID is synthesized.
        """
        for line in self.jobs.read_text().splitlines():
            columns = line.split("\t")
            if len(columns) == 6:
                metadata = D.parse_registry_metadata(columns[5])
                if metadata.get("attempt_id") == attempt_id and metadata.get("pid"):
                    return
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE, start_new_session=True)
        start = D.process_start_ticks(child.pid)
        namespace = D.process_namespace_identity(child.pid)
        child.communicate(timeout=5)
        self.assertIsNotNone(start)
        self.assertIsNotNone(namespace)
        rows = self.jobs.read_text().splitlines()
        self.jobs.write_text("\n".join(
            line + (f",pid={child.pid},pid_start={start},pgid={child.pid},"
                    f"pid_ns={namespace},pid_observer_ns={namespace}"
                    if f"attempt_id={attempt_id}," in line else "") for line in rows) + "\n")

    def test_registered_review_shape_passes_exact_terminal_identity(self):
        # B-1 (SD-153 defect #1): the *same* review row closed in place by the
        # owner (registered-review shape) must reach the same "the owner
        # ruled on this node" conclusion `_marker_identity_row` already gives
        # a continuation's own synthetic marker (see the sibling test below).
        # `write_completion_marker` does not thread an explicit `jobs=` through
        # its own `completion_dir()` call, so a `jobs.log` outside the stable
        # per-user root (this class's default `self.jobs`) reads back a marker
        # `_marker_identity_row` cannot find at that same explicit path -- move
        # the registry itself onto the stable root, as `continuation_closure_
        # fixture` already does, so registry and marker root coincide.
        self.jobs = self.stable_dispatch / "jobs.log"
        self.jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs.touch(mode=0o600)
        route = self.compile_route()
        self.write_route(route)
        r1 = self.review_blocking_row("att-shape-r1", 1)
        r2 = self.review_blocking_row("att-shape-r2", 2)
        self._reap_real_process("att-shape-r2")
        memo = self.owner_closure(route, attempts=("att-shape-r1", "att-shape-r2"),
                                  artifacts=(r1.name, r2.name))
        node = next(n for n in route["nodes"] if n["id"] == "plan-check")
        # In-process (like the continuation-shape sibling below), not the
        # `complete()` subprocess: `write_completion_marker` does not thread
        # `jobs=` through its own `completion_dir()` call, so a subprocess
        # invoked with `base_env()`'s cleared `AGENT_DISPATCH_JOBS` writes the
        # marker under the stable per-user root while `--jobs` only pointed
        # the registry elsewhere -- a real root-resolution asymmetry in
        # `write_completion_marker`, out of this package's scope, that an
        # in-process call with one consistent `jobs` value does not hit.
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker, _ = ROUTE.complete_node(route, node, "plan-check", memo,
                                            jobs=self.jobs, attempt_id="att-shape-r2")
            proof = ROUTE._marker_identity_row(
                route, node, "plan-check", node.get("completion_gate"),
                jobs=self.jobs, exact_terminal=True)
        self.assertTrue(proof["passed"], proof)
        self.assertEqual(ROUTE.owner_closure_shape(marker), "registered-review")

    def test_continuation_shape_passes_exact_terminal_identity(self):
        # The sibling of the test above: a continuation's own synthetic
        # owner-closure marker, `owner_closure_shape`'s other non-None value.
        source, route, path, node, memo, _reviews = self.continuation_closure_fixture()
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            marker, _ = ROUTE.complete_node(route, node, node["id"], memo, self.jobs, "att-source-review-r2")
            proof = ROUTE._marker_identity_row(
                route, node, node["id"], node["completion_gate"],
                jobs=self.jobs, exact_terminal=True)
        self.assertTrue(proof["passed"], proof)
        self.assertEqual(ROUTE.owner_closure_shape(marker), "continuation")

    def test_unshaped_blocking_row_does_not_pass_exact_terminal_identity(self):
        # A blocking review round that nobody ever ruled over is neither
        # shape -- `owner_closure_shape` returns None and exact-terminal
        # identity must not pass over its head.
        route = self.compile_route()
        self.write_route(route)
        self.review_blocking_row("att-plain-r1", 1)
        self._reap_real_process("att-plain-r1")
        node = next(n for n in route["nodes"] if n["id"] == "plan-check")
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            proof = ROUTE._marker_identity_row(
                route, node, "plan-check", node.get("completion_gate"),
                jobs=self.jobs, exact_terminal=True)
        self.assertFalse(proof["passed"], proof)

    def test_true_dead_worker_is_refused_even_with_a_closure_record(self):
        # The core safety property: a worker that did not finish (dead-*) is
        # never completable by an owner memo, however well-formed.
        route = self.compile_route()
        route_path = self.write_route(route)
        r1 = self.review_blocking_row("att-dead-r1", 1, note="dead-worker-fail")
        r2 = self.review_blocking_row("att-dead-r2", 2, note="dead-worker-fail")
        memo = self.owner_closure(route, attempts=("att-dead-r1", "att-dead-r2"),
                                  artifacts=(r1.name, r2.name))
        directory = self.stable_dispatch / "completion" / route["route_id"]
        for attempt in ("att-dead-r2", "att-dead-r1"):
            result = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id=attempt)
            self.assertNotEqual(result.returncode, 0, attempt)
            self.assertIn("attempt-row-terminal-without-completion:dead-worker-fail", result.stderr)
            self.assertFalse((directory / "plan-check.json").exists())
        for attempt in ("att-dead-r1", "att-dead-r2"):
            status, meta = self.read_row(attempt)
            self.assertEqual((status, meta.get("note")), ("done", "dead-worker-fail"))

    def test_owner_closure_refusals_are_typed_and_publish_nothing(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        directory = self.stable_dispatch / "completion" / route["route_id"]
        # (1) budget not exhausted: one blocking round at strong (cap 2) -- a
        # correction round is the answer while budget remains, not a ruling.
        r1 = self.review_blocking_row("att-early-r1", 1)
        early = self.owner_closure(route, "round_1.owner-closure.md",
                                   attempts=("att-early-r1",), artifacts=(r1.name,))
        result = self.complete(route_path, "plan-check", early, jobs=self.jobs, attempt_id="att-early-r1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner-closure-round-budget-not-exhausted", result.stderr)
        self.assertFalse((directory / "plan-check.json").exists())
        r2 = self.review_blocking_row("att-early-r2", 2)
        both = ("att-early-r1", "att-early-r2")
        names = (r1.name, r2.name)
        cases = []
        plain = self.artifact / "_internal" / "plan_reviews" / "closure-notes.md"
        plain.write_text(self.owner_closure(route, attempts=both, artifacts=names).read_text(encoding="utf-8"),
                         encoding="utf-8")
        cases.append((plain, "owner-closure-evidence-name-invalid"))
        cases.append((self.owner_closure(route, "unlinked.owner-closure.md"),
                      "owner-closure-evidence-unlinked"))
        cases.append((self.owner_closure(route, "flag.owner-closure.md", attempts=both,
                                         artifacts=names, verdict="closed"),
                      "owner-closure-frontmatter-invalid"))
        cases.append((self.owner_closure(route, "wrong-node.owner-closure.md", attempts=both,
                                         artifacts=names, node="impl-review"),
                      "owner-closure-frontmatter-invalid"))
        cases.append((self.owner_closure(route, "outside.owner-closure.md", attempts=both,
                                         artifacts=names, directory=self.base / "elsewhere"),
                      "owner-closure-evidence-outside-root"))
        for evidence, reason in cases:
            with self.subTest(reason=reason):
                result = self.complete(route_path, "plan-check", evidence, jobs=self.jobs,
                                       attempt_id="att-early-r2")
                self.assertNotEqual(result.returncode, 0, evidence.name)
                self.assertIn(reason, result.stderr)
                self.assertFalse((directory / "plan-check.json").exists())
                status, meta = self.read_row("att-early-r2")
                self.assertEqual((status, meta.get("note")), ("done", "completed-review-blocking"))
        # (2) a non-review node cannot borrow the path even with the note
        self.write_row("done", "plan-x", "att-plan-blocking",
                       "note=completed-review-blocking,worker_type=stage")
        plan_memo = self.owner_closure(route, "plan.owner-closure.md", attempts=("att-plan-blocking",),
                                       artifacts=("plan.md",), node="plan")
        result = self.complete(route_path, "plan", plan_memo, jobs=self.jobs, attempt_id="att-plan-blocking")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner-closure-node-not-review", result.stderr)
        # (3) the review artifact the exact log names has vanished -> unverifiable
        r2.unlink()
        good = self.owner_closure(route, "late.owner-closure.md", attempts=both, artifacts=names)
        result = self.complete(route_path, "plan-check", good, jobs=self.jobs, attempt_id="att-early-r2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner-closure-review-artifact-unverifiable", result.stderr)
        self.assertFalse((directory / "plan-check.json").exists())

    # Review-round-2 findings (B1, M3, M4, minor 9/10) ---------------------------
    def test_b1_registry_unsafe_closure_path_is_refused_before_anything_is_sealed(self):
        # The evidence path is sealed into the registry pipe; a ',' or '=' in the
        # filename could forge fields, a tab a 7-field line, a newline a whole row.
        route = self.compile_route()
        route_path = self.write_route(route)
        r1 = self.review_blocking_row("att-inj-r1", 1)
        r2 = self.review_blocking_row("att-inj-r2", 2)
        directory = self.stable_dispatch / "completion" / route["route_id"]
        both, names = ("att-inj-r1", "att-inj-r2"), (r1.name, r2.name)
        forged = "r2,failure_class=pass,note=completed-supervisor,stage_authority=0.owner-closure.md"
        for name in (forged, "eq=sign.owner-closure.md", "tab\there.owner-closure.md",
                     "new\nline.owner-closure.md"):
            with self.subTest(name=name.encode("unicode_escape").decode()):
                memo = self.owner_closure(route, name, attempts=both, artifacts=names)
                result = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-inj-r2")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("owner-closure-evidence-path-unsafe", result.stderr)
                self.assertFalse((directory / "plan-check.json").exists())
                status, meta = self.read_row("att-inj-r2")
                self.assertEqual((status, meta.get("note")), ("done", "completed-review-blocking"))
                self.assertNotIn("stage_authority", meta)
                self.assertNotEqual(meta.get("failure_class"), "pass")
        # every registry line is still a 6-field row (no forged rows / 7-field lines)
        for line in self.jobs.read_text(encoding="utf-8").splitlines():
            self.assertEqual(len(line.split("\t")), 6, line)

    def test_b1_closure_facts_are_sealed_through_the_sanitizing_writer(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        r1 = self.review_blocking_row("att-seal-r1", 1)
        r2 = self.review_blocking_row("att-seal-r2", 2)
        memo = self.owner_closure(route, attempts=("att-seal-r1", "att-seal-r2"), artifacts=(r1.name, r2.name))
        result = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-seal-r2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = next(l for l in self.jobs.read_text(encoding="utf-8").splitlines() if "att-seal-r2" in l)
        fields = line.split("\t")
        self.assertEqual(len(fields), 6)
        pipe = fields[5]
        keys = [part.split("=", 1)[0] for part in pipe.split(",") if "=" in part]
        # the seal appears exactly once and only through the writer (sorted, no raw duplicate)
        self.assertEqual(keys.count("gate_closure"), 1)
        self.assertEqual(keys.count("owner_closure"), 1)
        self.assertEqual(keys.count("review_artifact_b64"), 1)
        meta = D.parse_registry_metadata(pipe)
        self.assertEqual(meta.get("owner_closure"), str(memo.resolve()))
        self.assertEqual(meta.get("gate_closure"), "owner-closure")
        self.assertEqual(meta.get("note"), "completed-marker")
        self.assertIn("gate_closure", D.ATTEMPT_TERMINAL_EVIDENCE_KEYS)
        self.assertIn("owner_closure", D.ATTEMPT_TERMINAL_EVIDENCE_KEYS)

    def test_m3_live_review_round_blocks_closure_and_only_terminated_rounds_count(self):
        route = self.compile_route()          # strong -> cap 2
        route_path = self.write_route(route)
        directory = self.stable_dispatch / "completion" / route["route_id"]
        r1 = self.review_blocking_row("att-live-r1", 1)
        memo = self.owner_closure(route, "round_1.owner-closure.md", attempts=("att-live-r1",), artifacts=(r1.name,))
        # round 2 registered but its launch never closed the row -> still open
        for status in ("open", "running"):
            with self.subTest(status=status):
                self.jobs.write_text("\n".join(
                    l for l in self.jobs.read_text(encoding="utf-8").splitlines() if "att-live-r2" not in l
                ) + "\n", encoding="utf-8")
                self.write_row(status, "plan-check-r2", "att-live-r2", "worker_type=review", node_id="plan-check")
                result = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-live-r1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("owner-closure-round-still-open:attempt=att-live-r2", result.stderr)
                self.assertFalse((directory / "plan-check.json").exists())
        # SD-153 (A-SD153-5, revised): a terminated-but-not-blocking round (a
        # real BLOCKED worker, never a PASS/FAIL/blocking verdict) does NOT
        # spend the exhaustion budget by itself any more -- only one genuine
        # verdict (r1's blocking review) has landed, so closure is still
        # premature, and a third registered round is still admitted rather
        # than refused as budget-exhausted.
        self.jobs.write_text("\n".join(
            l for l in self.jobs.read_text(encoding="utf-8").splitlines() if "att-live-r2" not in l
        ) + "\n", encoding="utf-8")
        self.write_row("done", "plan-check-r2", "att-live-r2",
                       "worker_type=review,note=dead-worker-blocked,failure_class=blocked", node_id="plan-check")
        result = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-live-r1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "owner-closure-round-budget-not-exhausted:rounds=1;max_round=2;verdictless_streak=1;bound=2",
            result.stderr,
        )
        self.assertFalse((directory / "plan-check.json").exists())
        status, meta = self.read_row("att-live-r2")
        self.assertEqual((status, meta.get("note")), ("done", "dead-worker-blocked"))   # untouched
        node_spec = importlib.util.spec_from_file_location(
            "m3_dispatch_node", ROOT / "utilities/dispatch-node.py")
        dispatch = importlib.util.module_from_spec(node_spec)
        node_spec.loader.exec_module(dispatch)
        node = next(n for n in route["nodes"] if n["id"] == "plan-check")
        budget = dispatch.admit_round(route, node, self.jobs).budget
        self.assertEqual(budget.state, "admit")

    def upstream_superseded_review_fixture(self):
        self.jobs = self.stable_dispatch / "jobs.log"
        self.jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs.touch(mode=0o600)
        route = self.compile_route(intensity="standard")
        path = self.write_route(route)
        plan = self.base / "plan.md"
        plan.write_text("original plan\n")
        self.assertEqual(self.complete(path, "plan", plan).returncode, 0)
        review = self.review_blocking_row("att-original-fail", 1)
        self.jobs.write_text(self.jobs.read_text().replace(
            "attempt_id=att-original-fail,", "attempt_id=att-original-fail,failure_class=fail,"))
        self.write_row("running", "review-pass", "att-original-pass", "worker_type=review", node_id="plan-check")
        self._reap_real_process("att-original-pass")
        passed = self.base / "review-pass.md"
        passed.write_text("independent PASS on original plan\n")
        result = self.complete(path, "plan-check", passed, jobs=self.jobs, attempt_id="att-original-pass")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan.write_text("corrected plan\n")
        result = self.revise(path, "plan", plan, basis="owner-correction", reason="corrected upstream source")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        node = next(n for n in route["nodes"] if n["id"] == "plan-check")
        directory = self.stable_dispatch / "completion" / route["route_id"]
        memo = self.owner_closure(route, attempts=("att-original-fail",), artifacts=(review.name,))
        return route, path, node, directory, memo

    def test_upstream_superseded_pass_admits_original_fail_owner_closure(self):
        route, path, node, directory, memo = self.upstream_superseded_review_fixture()
        history = {p.name: p.read_bytes() for p in directory.glob("plan-check.*.json")
                   if p.name != "plan-check.attempt.json"}  # mutable compatibility pointer
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                      for root in (self.artifact, self.stable_dispatch) for p in root.rglob("*") if p.is_file()}
            proof = ROUTE.owner_closure_plan(route, node, memo, self.jobs, "att-original-fail")
            self.assertEqual(proof["source_attempt_id"], "att-original-fail")
            self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                                     for root in (self.artifact, self.stable_dispatch) for p in root.rglob("*") if p.is_file()})
        result = self.complete(path, "plan-check", memo, jobs=self.jobs, attempt_id="att-original-fail")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        marker = json.loads((directory / "plan-check.json").read_text())
        self.assertEqual(marker["review_independence"], "owner-overridden")
        self.assertEqual(marker["review_gate_closure"], "owner-closure")
        self.assertNotIn("state", marker)
        for name, raw in history.items():
            self.assertEqual((directory / name).read_bytes(), raw)
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            self.assertEqual(D.gate_currency(route, node, directory / "plan-check.json", observe=True).state, "current")
        _, meta = self.read_row("att-original-fail")
        self.assertEqual(meta["failure_class"], "fail")

    def test_supersession_reader_proves_original_attempt_link_in_both_gate_modes(self):
        route, _, node, directory, _ = self.upstream_superseded_review_fixture()
        for mode in ("on", "off"):
            with self.subTest(gates=mode), mock.patch.dict(os.environ, {**self.base_env(), "HEARTING_GATES": mode}, clear=True):
                observed = D.gate_currency(route, node, directory / "plan-check.json", observe=True)
                self.assertEqual(observed.state, "superseded", observed)
                execution = D.gate_currency(route, node, directory / "plan-check.json")
                self.assertEqual(execution.state, "superseded" if mode == "on" else "current", execution)
        link = directory / "plan-check.att-original-pass.attempt.json"
        link.unlink()
        with mock.patch.dict(os.environ, {**self.base_env(), "HEARTING_GATES": "off"}, clear=True):
            self.assertEqual(D.gate_currency(route, node, directory / "plan-check.json").reason,
                             "revision-predecessor-link-invalid")

    def test_supersession_history_does_not_require_deleted_historical_payloads(self):
        route, _, node, directory, _ = self.upstream_superseded_review_fixture()
        (self.base / "plan.md").unlink()
        (self.base / "review-pass.md").unlink()
        for mode in ("on", "off"):
            with self.subTest(gates=mode), mock.patch.dict(os.environ, {**self.base_env(), "HEARTING_GATES": mode}, clear=True):
                self.assertEqual(D.gate_currency(route, node, directory / "plan-check.json", observe=True).state,
                                 "superseded")
                if mode == "off":
                    self.assertEqual(D.gate_currency(route, node, directory / "plan-check.json").state, "current")

    def test_superseded_closure_refuses_forged_history_and_newer_blocking_round(self):
        route, path, node, directory, memo = self.upstream_superseded_review_fixture()
        self.review_blocking_row("att-newer-fail", 3)
        memo = self.owner_closure(route, attempts=("att-original-fail", "att-newer-fail"),
                                  artifacts=("round_1.md", "round_3.md"))
        before = (directory / "plan-check.json").read_bytes()
        result = self.complete(path, "plan-check", memo, jobs=self.jobs, attempt_id="att-original-fail")
        self.assertIn("owner-closure-node-already-complete", result.stderr)
        self.assertEqual(before, (directory / "plan-check.json").read_bytes())
        original = json.loads(before)
        for field, value in (("superseded_by", {"node": "execute", "sequence": 2}),
                             ("review_independence", "owner-overridden")):
            forged = dict(original, **{field: value})
            for name in ("plan-check.json", "plan-check.2.json"):
                (directory / name).write_text(json.dumps(forged))
            with mock.patch.dict(os.environ, self.base_env(), clear=True):
                currency = D.gate_currency(route, node, directory / "plan-check.json", observe=True)
                self.assertEqual(currency.reason, "supersession-provenance-invalid")
                with self.assertRaisesRegex(ValueError, "canonical-marker-unproven"):
                    ROUTE.owner_closure_plan(route, node, memo, self.jobs, "att-newer-fail")

    def test_revision_provenance_can_cross_a_superseded_predecessor(self):
        route, path, node, directory, _ = self.upstream_superseded_review_fixture()
        evidence = self.base / "review-pass.md"
        evidence.write_text("new review disposition\n")
        with mock.patch.dict(os.environ, {"HEARTING_GATES": "off"}):
            result = self.revise(path, "plan-check", evidence, basis="owner-correction", reason="new disposition")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for mode in ("on", "off"):
            with self.subTest(gates=mode), mock.patch.dict(os.environ, {**self.base_env(), "HEARTING_GATES": mode}, clear=True):
                self.assertEqual(D.gate_currency(route, node, directory / "plan-check.json").state, "current")

    def test_supersession_reader_refuses_malformed_predecessor_and_upstream(self):
        route, _, node, directory, _ = self.upstream_superseded_review_fixture()
        for path in (directory / "plan-check.1.json", directory / "plan.2.json"):
            original = path.read_bytes()
            for value in ([], None):
                with self.subTest(path=path.name, value=value):
                    path.write_text(json.dumps(value))
                    with mock.patch.dict(os.environ, self.base_env(), clear=True):
                        currency = D.gate_currency(route, node, directory / "plan-check.json", observe=True)
                    self.assertEqual(currency.reason, "supersession-provenance-invalid")
            path.write_bytes(original)

    def test_m4b_latest_round_closure_replaces_a_superseded_pass_marker(self):
        """A correction added a review round after round 1 passed: the round 1
        marker is no longer current, so closing the exhausted latest round must
        not be refused as `node-already-complete`; closing an older round
        while a later one exists still is."""
        route = self.compile_route()          # strong -> cap 2
        route_path = self.write_route(route)
        directory = self.stable_dispatch / "completion" / route["route_id"]
        self.write_row("running", "plan-check-r1", "att-stale-r1", "worker_type=review", node_id="plan-check")
        self._reap_real_process("att-stale-r1")
        passed = self.base / "plan-check-pass.md"
        passed.write_text("round 1: PASS\n", encoding="utf-8")
        first = self.complete(route_path, "plan-check", passed, jobs=self.jobs, attempt_id="att-stale-r1")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(json.loads((directory / "plan-check.json").read_text())["attempt_id"], "att-stale-r1")
        r2 = self.review_blocking_row("att-stale-r2", 2)
        memo = self.owner_closure(route, attempts=("att-stale-r2",), artifacts=(r2.name,))
        closed = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-stale-r2")
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        marker = json.loads((directory / "plan-check.json").read_text())
        self.assertEqual(marker["attempt_id"], "att-stale-r2")
        self.assertTrue((directory / "plan-check.1.json").is_file())   # round 1 history kept
        status, meta = self.read_row("att-stale-r2")
        self.assertEqual((meta.get("gate_closure"), meta.get("note")), ("owner-closure", "completed-marker"))

    def test_m4c_superseded_marker_does_not_admit_closing_an_older_round(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        directory = self.stable_dispatch / "completion" / route["route_id"]
        self.write_row("running", "plan-check-r1", "att-old-r1", "worker_type=review", node_id="plan-check")
        self._reap_real_process("att-old-r1")
        passed = self.base / "plan-check-pass.md"
        passed.write_text("round 1: PASS\n", encoding="utf-8")
        self.assertEqual(self.complete(route_path, "plan-check", passed, jobs=self.jobs,
                                       attempt_id="att-old-r1").returncode, 0)
        r2 = self.review_blocking_row("att-old-r2", 2)
        r3 = self.review_blocking_row("att-old-r3", 3)
        memo = self.owner_closure(route, attempts=("att-old-r2", "att-old-r3"), artifacts=(r2.name, r3.name))
        before = (directory / "plan-check.json").read_text(encoding="utf-8")
        older = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-old-r2")
        self.assertNotEqual(older.returncode, 0)
        self.assertIn("owner-closure-node-already-complete:attempt=att-old-r1", older.stderr)
        self.assertEqual((directory / "plan-check.json").read_text(encoding="utf-8"), before)
        latest = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-old-r3")
        self.assertEqual(latest.returncode, 0, latest.stdout + latest.stderr)
        self.assertEqual(json.loads((directory / "plan-check.json").read_text())["attempt_id"], "att-old-r3")

    def test_m4_second_closure_on_another_attempt_is_refused_and_keeps_the_canonical_marker(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        directory = self.stable_dispatch / "completion" / route["route_id"]
        r1 = self.review_blocking_row("att-dup-r1", 1)
        r2 = self.review_blocking_row("att-dup-r2", 2)
        memo = self.owner_closure(route, attempts=("att-dup-r1", "att-dup-r2"), artifacts=(r1.name, r2.name))
        first = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-dup-r2")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        canonical_before = (directory / "plan-check.json").read_text(encoding="utf-8")
        second = self.complete(route_path, "plan-check", memo, jobs=self.jobs, attempt_id="att-dup-r1")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("owner-closure-node-already-complete:attempt=att-dup-r2", second.stderr)
        self.assertEqual((directory / "plan-check.json").read_text(encoding="utf-8"), canonical_before)
        self.assertFalse((directory / "plan-check.2.json").exists())
        status, meta = self.read_row("att-dup-r1")
        self.assertEqual((status, meta.get("note")), ("done", "completed-review-blocking"))
        self.assertIsNone(meta.get("completion_marker"))

    def test_minor9_duplicate_frontmatter_key_and_substring_attempt_ids_are_refused(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        directory = self.stable_dispatch / "completion" / route["route_id"]
        r1 = self.review_blocking_row("att-word-r1", 1)
        r2 = self.review_blocking_row("att-word-r2", 2)
        good = self.owner_closure(route, attempts=("att-word-r1", "att-word-r2"), artifacts=(r1.name, r2.name))
        text = good.read_text(encoding="utf-8")
        dup = good.with_name("dup.owner-closure.md")
        dup.write_text(text.replace("verdict: closed-by-owner\n", "verdict: closed\nverdict: closed-by-owner\n"),
                       encoding="utf-8")
        result = self.complete(route_path, "plan-check", dup, jobs=self.jobs, attempt_id="att-word-r2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner-closure-frontmatter-invalid:duplicate=verdict", result.stderr)
        # `att-word-r1` must not be satisfied by the longer id `att-word-r10`
        sub = good.with_name("substr.owner-closure.md")
        sub.write_text(text.replace("`att-word-r1`", "`att-word-r10`"), encoding="utf-8")
        result = self.complete(route_path, "plan-check", sub, jobs=self.jobs, attempt_id="att-word-r2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner-closure-evidence-unlinked:attempt=att-word-r1", result.stderr)
        self.assertFalse((directory / "plan-check.json").exists())

    def test_minor10_unknown_intensity_is_a_typed_refusal(self):
        # SD-153: a compiled route's sealed `continuation_budget.review_round_cap`
        # is now authoritative over a mutated `effective_intensity` -- only a
        # legacy route with no sealed cap re-derives from `effective_intensity`
        # (and can therefore hit an unknown one), so the fixture must drop the
        # field to still exercise this refusal.
        route = dict(self.compile_route())
        route.pop("continuation_budget", None)
        route["effective_intensity"] = "mythic"
        node = next(n for n in route["nodes"] if n["id"] == "plan-check")
        with self.assertRaises(ValueError) as caught:
            ROUTE._owner_closure_eligibility(
                route, node, "plan-check", self.base / "x.owner-closure.md",
                {"worker_type": "review", "attempt_id": "att-x"}, [],
            )
        self.assertEqual(str(caught.exception), "owner-closure-intensity-unknown:mythic")

    # F-1 fixture ------------------------------------------------------
    # A detached leg drains with no result file while its exact live parent
    # conductor has not yet run `complete`. reap-watch must defer the
    # missing-result closure so the conductor's later, legitimate `complete`
    # still succeeds -- this is the end-to-end proof for
    # utilities/dispatch_reap_watch.test.py's unit-level parent fixtures.
    def test_reap_deferral_lets_the_live_conductor_publish_its_marker(self):
        route = self.compile_route()
        route_path = self.write_route(route)
        evidence = self.base / "plan.md"
        evidence.write_text("plan body\n", encoding="utf-8")

        parent = subprocess.Popen(["sleep", "5"])
        leg = subprocess.Popen(
            ["sleep", "0.05"],
            env={**os.environ, D.ATTEMPT_DESCENDANT_ENV: "att-leg"},
            start_new_session=True,
        )
        try:
            parent_identity = D.process_launch_identity(parent.pid)
            parent_extra = ",".join(
                f"{k}={v}" for k, v in parent_identity.items() if k != "pid"
            )
            parent_line = (
                f"2026-08-13T00:00:00Z\topen\t{self.repo}\t{self.repo}\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,worker_type=owner,"
                f"attempt_id=att-parent,pid={parent.pid},{parent_extra}"
            )
            with self.jobs.open("a", encoding="utf-8") as fh:
                fh.write(parent_line + "\n")

            leg_identity = D.process_launch_identity(leg.pid)
            leg_extra = ",".join(
                f"{k}={v}" for k, v in leg_identity.items() if k != "pid"
            )
            leg_extra += (
                f",pid={leg.pid},launch_lifecycle=detached,parent=owner,"
                "parent_attempt_id=att-parent,"
                f"log_file={self.base / 'missing.jsonl'}"
            )
            self.write_row("open", "leg-slug", "att-leg", extra=leg_extra, node_id="plan")

            watcher = subprocess.Popen(
                [
                    sys.executable, str(ROOT / "utilities/dispatch-reap-watch.py"),
                    "--jobs", str(self.jobs),
                    "--attempt-id", "att-leg",
                    "--pid", str(leg.pid),
                    "--pid-start", leg_identity["pid_start"],
                    "--pgid", leg_identity["pgid"],
                    "--interval", "0.02",
                    "--parent-recheck-interval", "0.05",
                ]
            )
            leg.wait(timeout=5)

            deadline = time.time() + 3
            status, meta = None, None
            while time.time() < deadline:
                status, meta = self.read_row("att-leg")
                if meta and meta.get("reap_close_deferred") == "parent-live:process":
                    break
                time.sleep(0.05)
            self.assertEqual(status, "open")
            self.assertEqual(meta.get("reap_close_deferred"), "parent-live:process")

            result = self.complete(
                route_path, "plan", evidence, jobs=self.jobs, attempt_id="att-leg",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            self.assertEqual(watcher.wait(timeout=5), 0)
            status, meta = self.read_row("att-leg")
            self.assertEqual(status, "done")
            self.assertEqual(meta.get("note"), "completed-marker")
            self.assertNotEqual(meta.get("note"), "dead-missing-result")
            directory = self.stable_dispatch / "completion" / route["route_id"]
            self.assertTrue((directory / "plan.json").is_file())
        finally:
            parent.kill()
            parent.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
