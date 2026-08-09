#!/usr/bin/env python3
"""CLI regression tests for the detached resource authorization boundary."""
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLEAN_ENV = {k: v for k, v in os.environ.items() if k != "AGENT_ARTIFACT_ROOT"}
RUNNER = ROOT / "utilities" / "resource-runner.py"
ROUTER = ROOT / "utilities" / "capability-route.py"
SMOKE = ROOT / "tools" / "smoke-attestation.py"
spec = importlib.util.spec_from_file_location("runner", RUNNER)
R = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(R)


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "config").write_text("ok\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        self.artifacts = self.base / "artifacts"
        self.artifacts.mkdir()
        self.home = self.base / "agent-home"
        (self.home / "core").mkdir(parents=True)
        (self.home / "core" / "CORE.md").write_text("core\n")
        (self.home / "utilities").symlink_to(ROOT / "utilities", target_is_directory=True)
        self.route = self.artifacts / ".runtime" / "routes" / "fixture.json"
        self.route.parent.mkdir(parents=True)
        evidence = self.base / "dispatch-evidence.json"
        evidence.write_text(json.dumps({"tuples": [{
            "harness": "codex", "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "workspace-write", "child_harness": "codex",
            "launch_authority": "conductor", "status": "supported", "probe_source": "test",
            "probe_time": "2026-07-27T00:00:00Z", "failure_class": "none",
            "checked_worktree": str(self.repo.resolve()), "failure_scope": "none",
            "codex_command": "ok", "retry_on_isolated_worktree": 0,
        }], "native_subagent": []}))
        result = subprocess.run([
            sys.executable, str(ROUTER), "compile", "--capability", "autopilot-lab",
            "--capability-mode", "setup", "--intensity", "auto", "--signal", "resource-run",
            "--cwd", str(self.repo), "--artifact-root", str(self.artifacts),
            "--dispatch-evidence", str(evidence), "--tracking", "untracked",
            "--spec-read", "not-applicable", "--drift-verdict", "no-project-spec",
            "--workflow-mode", "untracked", "--artifact-guard", "preflight-passed",
            "--output", str(self.route),
        ], text=True, capture_output=True, env=CLEAN_ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.registry = self.base / "registry.json"
        self.index = self.base / "resource-runs.index.json"
        self.log = self.base / "logs" / "run.log"
        self.launch = self.base / "launched"
        self.attestation = self.base / "smoke.json"
        subprocess.run([
            sys.executable, str(SMOKE), "attest", "--input", str(self.repo / "config"),
            "--cwd", str(self.repo), "--output", str(self.attestation), "--", sys.executable, "-c", "pass",
        ], check=True, stdout=subprocess.DEVNULL)

    def cli(self, *args, cwd=None):
        return subprocess.run([
            sys.executable, str(RUNNER), "--registry", str(self.registry), *args,
        ], cwd=cwd, text=True, capture_output=True,
           env={**CLEAN_ENV, "AGENT_HOME": str(self.home),
                "AGENT_RESOURCE_RUN_INDEX": str(self.index)})

    def start_args(self, route=None, node="full-run", smoke=None, run_id="case", config_manifest=None):
        return ("start", "--run-id", run_id, "--cwd", str(self.repo), "--log", str(self.log),
                "--route", str(route or self.route), "--node", node,
                *(('--smoke-attestation', str(smoke)) if smoke is not None else ()),
                *(('--config-manifest', str(config_manifest)) if config_manifest is not None else ()),
                "--", sys.executable, "-c", f"from pathlib import Path; import time; Path({str(self.launch)!r}).write_text('launched'); time.sleep(30)")

    def seal_config_manifest(self, label):
        PROV = ROOT / "tools" / "lab-config-provenance.py"
        artifact_root = self.base / f"artifacts-{label}"
        artifact_root.mkdir()
        result = subprocess.run([
            sys.executable, str(PROV), "seal", "--repo", str(self.repo), "--config", "./config",
            "--slug", "demo", "--artifact-root", str(artifact_root),
        ], check=True, capture_output=True, text=True, env=CLEAN_ENV)
        computed_run_id = json.loads(result.stdout)["run_id"]
        return artifact_root / "experiments" / "demo" / "_internal" / "configs" / f"{computed_run_id}.manifest.json"

    def attest_with_config_manifest(self, manifest_path, name):
        attestation = self.base / f"{name}.json"
        subprocess.run([
            sys.executable, str(SMOKE), "attest", "--input", str(self.repo / "config"),
            "--config-manifest", str(manifest_path), "--cwd", str(self.repo),
            "--output", str(attestation), "--", sys.executable, "-c", "pass",
        ], check=True, stdout=subprocess.DEVNULL, env=CLEAN_ENV)
        return attestation

    def assert_rejected_before_side_effects(self, *args, cli_cwd=None):
        result = self.cli(*args, cwd=cli_cwd)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists(), result.stderr)
        self.assertFalse(self.registry.exists(), result.stderr)
        self.assertFalse(self.launch.exists(), result.stderr)

    def test_pid_identity_and_registry(self):
        identity = R.proc_identity(os.getpid())
        self.assertTrue(identity)
        self.assertTrue(R.alive(identity))
        identity["starttime"] = "0"
        self.assertFalse(R.alive(identity))
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runs.json"
            R.locked_update(path, lambda data: data["runs"].update(x={"pid": 1}))
            self.assertIn("x", json.loads(path.read_text())["runs"])

    def test_actual_cli_rejects_every_invalid_launch_proof_before_side_effects(self):
        cases = [
            ("omitted route", tuple(x for x in self.start_args() if x not in ("--route", str(self.route)))),
            ("omitted node", tuple(x for x in self.start_args() if x not in ("--node", "full-run"))),
            ("unknown node", self.start_args(node="missing")),
            ("missing smoke", self.start_args(smoke=None)),
            ("invalid smoke", self.start_args(smoke=self.base / "missing-smoke.json")),
        ]
        # Omitted flags are represented explicitly to ensure argparse rejects them.
        for name, args in cases:
            with self.subTest(name=name):
                self.assert_rejected_before_side_effects(*args)

        linked = self.base / "linked-route.json"
        linked.symlink_to(self.route)
        self.assert_rejected_before_side_effects(*self.start_args(route=linked, smoke=self.attestation))

        for name, mutate in (
            ("tampered route", lambda row: row.update(route_id="rt-tampered")),
            ("stale source commit", lambda row: row.update(source_commit="0" * 40)),
            ("wrong kind", lambda row: next(n for n in row["nodes"] if n["id"] == "full-run").update(kind="pipeline-stage")),
            ("wrong resource transport", lambda row: next(n for n in row["nodes"] if n["id"] == "full-run").update(resource_transport="inline")),
        ):
            row = json.loads(self.route.read_text())
            mutate(row)
            row.pop("route_hash", None)
            row.pop("route_id", None)
            bare = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            row["route_hash"] = "sha256:" + hashlib.sha256(bare).hexdigest()
            row["route_id"] = "rt-" + row["route_hash"].split(":", 1)[1][:16]
            candidate = self.base / f"{name.replace(' ', '-')}.json"
            candidate.write_text(json.dumps(row))
            with self.subTest(name=name):
                self.assert_rejected_before_side_effects(*self.start_args(route=candidate, smoke=self.attestation))

        other = self.base / "other-cwd"
        other.mkdir()
        wrong_cwd_args = list(self.start_args(smoke=self.attestation))
        wrong_cwd_args[wrong_cwd_args.index("--cwd") + 1] = str(other)
        self.assert_rejected_before_side_effects(*wrong_cwd_args)

    def test_valid_detached_start_status_stop_cleans_process_group(self):
        result = self.cli(*self.start_args(smoke=self.attestation))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), repr(result))
        run = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(self.log.exists())
        status = self.cli("status", "--run-id", "case")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["run_id"], "case")
        stop = self.cli("stop", "--run-id", "case")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        for _ in range(50):
            if not R.proc_identity(run["pid"]):
                break
            time.sleep(0.02)
        self.assertFalse(R.proc_identity(run["pid"]))
        self.assertEqual(os.getpgid(run["pid"]) if Path(f"/proc/{run['pid']}").exists() else None, None)

    def test_stop_rejects_changed_process_group_identity(self):
        result = self.cli(*self.start_args(smoke=self.attestation))
        self.assertEqual(result.returncode, 0, result.stderr)
        run = json.loads(result.stdout.strip().splitlines()[-1])
        R.locked_update(
            self.registry,
            lambda data: data["runs"]["case"].update(process_group=run["process_group"] + 1),
        )
        stop = self.cli("stop", "--run-id", "case")
        self.assertNotEqual(stop.returncode, 0)
        self.assertIsNotNone(R.proc_identity(run["pid"]))
        R.locked_update(
            self.registry,
            lambda data: data["runs"]["case"].update(process_group=run["process_group"]),
        )
        self.assertEqual(self.cli("stop", "--run-id", "case").returncode, 0)

    def test_stop_rejects_command_identity_mismatch_without_signal(self):
        result = self.cli(*self.start_args(smoke=self.attestation))
        self.assertEqual(result.returncode, 0, result.stderr)
        run = json.loads(result.stdout.strip().splitlines()[-1])
        R.locked_update(
            self.registry,
            lambda data: data["runs"]["case"].update(command_hash="0" * 64),
        )
        stop = self.cli("stop", "--run-id", "case")
        self.assertNotEqual(stop.returncode, 0)
        self.assertIsNotNone(R.proc_identity(run["pid"]))
        R.locked_update(
            self.registry,
            lambda data: data["runs"]["case"].update(command_hash=run["command_hash"]),
        )
        self.assertEqual(self.cli("stop", "--run-id", "case").returncode, 0)

    def test_index_existing_registry_does_not_restart_process(self):
        legacy = self._legacy_row("existing")
        R.locked_update(self.registry, lambda data: data["runs"].update(existing=legacy))
        before = R.proc_identity(os.getpid())
        result = subprocess.run(
            [sys.executable, str(RUNNER), "index", "--registry", str(self.registry)],
            text=True, capture_output=True,
            env={**CLEAN_ENV, "AGENT_RESOURCE_RUN_INDEX": str(self.index)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(R.proc_identity(os.getpid()), before)
        payload = json.loads(self.index.read_text())
        self.assertEqual(
            [row["path"] for row in payload["registries"].values()],
            [str(self.registry.resolve())],
        )

    def _legacy_row(self, run_id):
        identity = R.proc_identity(os.getpid())
        return {**identity, "run_id": run_id, "process_group": os.getpgid(os.getpid()),
                "cwd": str(self.repo), "log": str(self.log), "command": ["true"],
                "route": str(self.route), "node": "full-run", "status": "running"}

    # T7 -- a running legacy process (registry row predating the config
    # fields) must keep the same status/alive() judgment under the new code.
    def test_legacy_run_row_without_config_fields_is_unaffected(self):
        legacy_run = self._legacy_row("legacy")
        R.locked_update(self.registry, lambda data: data["runs"].update(legacy=legacy_run))
        self.assertTrue(R.alive(legacy_run))
        result = self.cli("status", "--run-id", "legacy")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)
        self.assertEqual(row["run_id"], "legacy")
        self.assertEqual(row["status"], "running")
        self.assertNotIn("config_ref", row)
        self.assertNotIn("config_sha256", row)

    # T13 -- an existing_run_exception recorded in run.json (a surface the
    # harness never writes) must not cause the registry row to be rewritten,
    # and the original worktree/command/config path/run ID stay intact.
    def test_existing_run_exception_in_run_json_does_not_trigger_a_registry_rewrite(self):
        legacy_run = self._legacy_row("legacy")
        R.locked_update(self.registry, lambda data: data["runs"].update(legacy=legacy_run))
        registry_before = self.registry.read_bytes()
        run_json = self.base / "run.json"
        run_json.write_text(json.dumps({
            "worktree": str(self.repo), "command": ["true"], "config_path": "config",
            "run_id": "legacy",
            "existing_run_exception": {
                "reason": "policy predates this run", "policy_version": "2026-08-03",
                "applies_from": "2026-08-04",
            },
        }, indent=2))
        run_json_before = run_json.read_bytes()
        status = self.cli("status", "--run-id", "legacy")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(self.registry.read_bytes(), registry_before)
        self.assertEqual(run_json.read_bytes(), run_json_before)
        row = json.loads(status.stdout)
        self.assertEqual(row["cwd"], str(self.repo))
        self.assertEqual(row["command"], ["true"])
        self.assertEqual(row["run_id"], "legacy")

    # T14 -- adding the new provenance fields must not make Fleet mistake an
    # ordinary process for a separate training run: the registry gains
    # exactly one row per start, never a phantom second one.
    def test_ordinary_start_creates_exactly_one_registry_row(self):
        result = self.cli(*self.start_args(smoke=self.attestation))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.registry.read_text())
        self.assertEqual(len(data["runs"]), 1)
        self.assertNotIn("config_ref", data["runs"]["case"])
        indexed = json.loads(self.index.read_text())
        self.assertEqual(
            [record["path"] for record in indexed["registries"].values()],
            [str(self.registry.resolve())],
        )

    # B3 regression -- a config manifest whose run_id disagrees with --run-id
    # (and isn't a valid --attempt suffix of it) is rejected before Popen.
    def test_config_manifest_run_id_mismatch_is_rejected_before_side_effects(self):
        manifest = self.seal_config_manifest("sealed-run")
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-mismatch")
        self.assert_rejected_before_side_effects(
            *self.start_args(smoke=attestation, run_id="mismatched-run-id", config_manifest=manifest))

    # B3 regression -- on a match, the registry key and the row's run_id
    # field are always identical (never split by the manifest's own run_id).
    def test_config_manifest_matching_run_id_binds_registry_key_to_row_field(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-match")
        result = self.cli(*self.start_args(smoke=attestation, run_id=manifest_run_id, config_manifest=manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        run = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(run["run_id"], manifest_run_id)
        data = json.loads(self.registry.read_text())
        self.assertIn(manifest_run_id, data["runs"])
        self.assertEqual(data["runs"][manifest_run_id]["run_id"], manifest_run_id)
        self.assertEqual(data["runs"][manifest_run_id]["config_ref"], "path:config")

    # B3 regression -- the documented "__aN" attempt-suffix policy: a
    # registry key that retries the same sealed manifest under a distinct
    # key is accepted, and the row still carries the registry key, not the
    # manifest's run_id.
    def test_config_manifest_attempt_suffix_is_accepted(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-attempt")
        attempt_run_id = f"{manifest_run_id}__a2"
        result = self.cli(*self.start_args(smoke=attestation, run_id=attempt_run_id, config_manifest=manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.registry.read_text())
        self.assertIn(attempt_run_id, data["runs"])
        self.assertEqual(data["runs"][attempt_run_id]["run_id"], attempt_run_id)

    # T-F3-5 (A9): every one of these must be rejected before Popen, whether
    # by safe_run_id() or by the exact-match-then-regex attempt policy.
    def test_T_F3_5_unsafe_or_invalid_run_ids_are_rejected(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-unsafe")
        cases = [
            "run__a0", "run__a01", "run__a", "run__aX", "run__a1x", "run__a-1", "../evil",
            # regex matches, but base != manifest_run_id after the split -- must
            # still be rejected by the exact-match-first ordering (A9).
            f"{manifest_run_id}__a1__a2",
        ]
        for bad in cases:
            with self.subTest(run_id=bad):
                self.assert_rejected_before_side_effects(
                    *self.start_args(smoke=attestation, run_id=bad, config_manifest=manifest))

    # T-F9-1: the full provenance field set is visible on `status`.
    def test_T_F9_1_status_exposes_the_full_provenance_field_set(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-fields")
        result = self.cli(*self.start_args(smoke=attestation, run_id=manifest_run_id, config_manifest=manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.cli("status", "--run-id", manifest_run_id)
        self.assertEqual(status.returncode, 0, status.stderr)
        row = json.loads(status.stdout)
        for key in ("run_id", "config_ref", "config_sha256", "source_commit",
                    "source_dirty", "source_git_state", "config_layout"):
            self.assertIn(key, row)

    # T-F9-2: status/stop/tail never create a process -- exactly one registry
    # row for one start, no phantom second run.
    def test_T_F9_2_read_commands_never_create_processes_or_rows(self):
        result = self.cli(*self.start_args(smoke=self.attestation))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.cli("status", "--run-id", "case")
        self.cli("tail", "--run-id", "case")
        self.cli("stop", "--run-id", "case")
        self.cli("status", "--run-id", "case")
        data = json.loads(self.registry.read_text())
        self.assertEqual(len(data["runs"]), 1)

    # T-F7-6: tampering the config source after attest (before start) must be
    # rejected before any process, log, or registry row is created.
    def test_T_F7_6_source_tamper_after_attest_is_rejected_before_side_effects(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-tamper")
        m = json.loads(manifest.read_text())
        Path(m["source_path"]).write_text("tampered-after-attest")
        self.assert_rejected_before_side_effects(
            *self.start_args(smoke=attestation, run_id=manifest_run_id, config_manifest=manifest))

    # T-G4-7: a smoke attestation missing its required hash must be rejected
    # by `start` before Popen, the registry row, or the log file exist.
    def test_T_G4_7_missing_attestation_hash_is_rejected_before_side_effects(self):
        data = json.loads(self.attestation.read_text())
        del data["attestation_hash"]
        broken = self.base / "broken-hash-smoke.json"
        broken.write_text(json.dumps(data))
        self.assert_rejected_before_side_effects(*self.start_args(smoke=broken))

    # T-G4-8: a smoke attestation with only partial config-provenance metadata
    # (one of the three fields dropped, hash recomputed over the rest) must
    # be rejected the same way -- config provenance is all-or-none.
    def test_T_G4_8_partial_config_metadata_is_rejected_before_side_effects(self):
        manifest = self.seal_config_manifest("sealed-run")
        manifest_run_id = json.loads(manifest.read_text())["run_id"]
        attestation = self.attest_with_config_manifest(manifest, "config-smoke-partial")
        data = json.loads(attestation.read_text())
        del data["config_source_path"]
        del data["attestation_hash"]
        data["attestation_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        broken = self.base / "broken-partial-smoke.json"
        broken.write_text(json.dumps(data))
        self.assert_rejected_before_side_effects(
            *self.start_args(smoke=broken, run_id=manifest_run_id, config_manifest=manifest))


if __name__ == "__main__":
    unittest.main()
