#!/usr/bin/env python3
"""A parallel execute slice launched in a linked worktree of the route's repository.

Real throwaway primary checkout + `git worktree add` linked worktree, a real
`dispatch-node.py` -> claude wrapper -> `worker-route-guard.py` chain (register
phase; no model is spawned). The route cwd is the primary checkout, the slice
worktree is the linked one, and two live owners share one slug:

  * the slice binds exactly the owner whose attempt id it inherited,
  * a slice without an inherited id fails closed (never the wrong owner),
  * a launch that is not a slice still compares the exact worktree,
  * the worker guard accepts the linked worktree only while the gates are off,
  * every wrapper's start command runs in the worktree it was given.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_R_SPEC = importlib.util.spec_from_file_location("route", ROOT / "utilities/capability-route.py")
ROUTE = importlib.util.module_from_spec(_R_SPEC)
_R_SPEC.loader.exec_module(ROUTE)
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_contract as D  # noqa: E402


def load_wrapper(name):
    spec = importlib.util.spec_from_file_location(
        f"{name}_dispatch_headless_linked", ROOT / f"adapters/{name}/bin/dispatch-headless.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.com", *args],
                          cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


class LinkedWorktreeSliceLaunchTest(unittest.TestCase):
    OWNERS = ("att-owner-alpha", "att-owner-bravo")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.primary = self.base / "primary"
        self.primary.mkdir()
        git("init", "-q", cwd=self.primary)
        (self.primary / "core").mkdir()
        (self.primary / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        git("add", "core/CORE.md", cwd=self.primary)
        git("commit", "-qm", "init", cwd=self.primary)
        self.linked = self.base / "linked"
        git("worktree", "add", "-q", "-b", "fixture-slice", str(self.linked), cwd=self.primary)
        self.addCleanup(lambda: subprocess.run(
            ["git", "-C", str(self.primary), "worktree", "remove", "--force", str(self.linked)],
            capture_output=True))
        self.artifact = self.base / ".agent_reports"
        self.artifact.mkdir()
        self.jobs = self.base / "state" / "jobs.log"
        self.jobs.parent.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        stub = self.bin / "claude"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        self.owner_procs = [subprocess.Popen(["sleep", "120"]) for _ in self.OWNERS]
        self.addCleanup(self._reap_owners)
        rows = []
        for attempt_id, proc in zip(self.OWNERS, self.owner_procs):
            start = (Path("/proc") / str(proc.pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
            rows.append(
                f"2026-10-02T00:00:00Z\topen\t{self.primary}\t{self.primary}\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,worker_type=owner,harness=claude,"
                f"runtime_sandbox=adapter-default,attempt_id={attempt_id},pid={proc.pid},pid_start={start}")
        self.jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.route = self._compile_route()
        self.route_path = self.base / "route.json"
        self.route_path.write_text(json.dumps(self.route), encoding="utf-8")
        self.node = next(n for n in self.route["nodes"] if n["id"] == "execute")
        self.brief = self.base / "brief.md"
        self.brief.write_text("slice brief\n", encoding="utf-8")

    def _reap_owners(self):
        for proc in self.owner_procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def _compile_route(self):
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec", "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        dispatch = {"tuples": [{
            "parent_harness": "claude", "parent_transport": "headless", "parent_sandbox": "adapter-default",
            "child_harness": "claude", "launch_authority": "conductor", "status": "supported",
            "probe_source": "fixture-check", "probe_time": "2026-10-02T00:00:00Z", "failure_class": "",
            "checked_worktree": str(self.primary), "failure_scope": "none",
            "codex_command": "not-applicable", "retry_on_isolated_worktree": 0,
        }], "native_subagent": []}
        # Sealed under the same hermetic dev activation the launches below run in.
        with mock.patch.dict(os.environ, self.env(), clear=True):
            return ROUTE.compile_route(
                "autopilot-code", "dev", "standard", self.primary, self.artifact,
                transport="headless", tracking="tracked",
                tracked_gate_evidence=gate, dispatch_evidence=dispatch,
            )

    def env(self, **extra):
        # Hermetic: the runtime that runs this test exports its own AGENT_* identity.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "HEARTING_"))}
        env.update({
            "PATH": f"{self.bin}:{env.get('PATH', '')}",
            "AGENT_HOME": str(ROOT),
            "AGENT_ARTIFACT_ROOT": str(self.artifact),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_DISPATCH_CURRENT_HARNESS": "claude",
            "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": "adapter-default",
        })
        env.update(extra)
        return env

    def register_slice(self, *, inherited=None, slug="slice-b", attempt="att-slice-beta", env=None):
        command = [
            sys.executable, str(ROOT / "utilities/dispatch-node.py"),
            "--route", str(self.route_path), "--node", "execute", "--adapter", "claude",
            "--action", "register", "--slug", slug, "--parent", "owner", "--jobs", str(self.jobs),
            "--subsession-id", "ss-linked-1", "--subsession-index", "1", "--subsession-count", "2",
            "--subsession-mode", "parallel", "--session-chain-id", "ssc-linked-1",
            "--phase-brief", str(self.brief), "--stage-authority", "0",
            "--fixed-file", "x.txt", "--narrow-verify", "true", "--expected-round-trips", "2",
            "--attempt-id", attempt, "--subsession-worktree", str(self.linked),
        ]
        if inherited:
            command += ["--", "--parent-attempt-id", inherited]
        return subprocess.run(command, text=True, capture_output=True, env=env or self.env())

    def rows(self):
        return [line.split("\t") for line in self.jobs.read_text(encoding="utf-8").splitlines() if line]

    @staticmethod
    def metadata(row):
        return dict(item.split("=", 1) for item in row[5].split(",") if "=" in item)

    def slice_row(self, attempt="att-slice-beta"):
        found = [row for row in self.rows() if self.metadata(row).get("attempt_id") == attempt]
        self.assertEqual(len(found), 1, self.rows())
        return found[0]

    def test_registered_linked_slice_binds_exactly_the_inherited_owner(self):
        result = self.register_slice(inherited="att-owner-bravo")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = self.slice_row()
        meta = self.metadata(row)
        self.assertEqual(row[3], str(self.linked))
        self.assertEqual(row[2], str(self.linked))
        self.assertEqual(meta["parent_attempt_id"], "att-owner-bravo")
        self.assertEqual(meta["launch_head"], git("rev-parse", "HEAD", cwd=self.linked))
        self.assertEqual(meta["source_commit_branch"], "fixture-slice")

    def test_linked_slice_report_uses_the_route_cycle_after_parent_validation(self):
        import artifact_producer

        begun = artifact_producer.begin(
            self.artifact, route_file=self.route_path, capability="autopilot-code",
            intensity="standard", require_cycle=True, jobs=self.jobs)
        output = Path(begun["env"]["AGENT_ARTIFACT_OUTPUT_DIR"])
        self.brief.write_text("report: evidence/ss-linked-1/report.md\n", encoding="utf-8")
        result = self.register_slice(inherited="att-owner-bravo")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = self.slice_row()
        meta = self.metadata(row)
        self.assertEqual((row[3], meta["parent_attempt_id"]),
                         (str(self.linked), "att-owner-bravo"))
        header = Path(meta["state_ledger"]).read_text(encoding="utf-8").splitlines()[0]
        ledger = json.loads(header.removeprefix("<!-- worker-state-ledger:v1 ").removesuffix(" -->"))
        self.assertEqual(set(ledger["fixed_files"]),
                         {str(self.linked / "x.txt"), str(output / "evidence/ss-linked-1/report.md")})

    def claim(self, attempt):
        """Claim a registered row exactly as a start does, against the exact owner it inherited."""
        binding = D.resolve_live_parent_attempt(
            self.jobs, parent_slug="owner", repo=str(self.primary), worktree=str(self.primary),
            expected_attempt_id="att-owner-bravo")
        procs = []

        def spawn(_gate):
            procs.append(subprocess.Popen(["sleep", "60"], start_new_session=True, pass_fds=(_gate,)))
            return procs[-1]

        self.addCleanup(lambda: [(p.kill(), p.wait()) for p in procs])
        return D.spawn_claimed_attempt(self.jobs, attempt, parent_binding=binding, spawn=spawn)

    def test_claim_time_parent_identity_accepts_a_slice_in_the_linked_worktree(self):
        registered = self.register_slice(inherited="att-owner-bravo")
        self.assertEqual(registered.returncode, 0, registered.stdout + registered.stderr)
        self.assertNotEqual(self.slice_row()[3], str(self.primary))
        self.claim("att-slice-beta")
        self.assertEqual(self.metadata(self.slice_row())["launch_claimed"], "1")

    def test_claim_time_parent_identity_still_refuses_a_plain_child_in_another_worktree(self):
        registered = self.wrapper_register(self.primary)
        self.assertEqual(registered.returncode, 0, registered.stdout + registered.stderr)
        lines = []
        for line in self.jobs.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if "attempt_id=att-plain-gamma" in fields[5]:
                fields[3] = str(self.linked)
            lines.append("\t".join(fields))
        self.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(D.DispatchContractError) as caught:
            self.claim("att-plain-gamma")
        self.assertEqual(caught.exception.reason, "parent-attempt-identity-changed")
        self.assertEqual(self.metadata(self.slice_row("att-plain-gamma"))["launch_claimed"], "0")

    def test_explicit_parent_selection_picks_the_other_same_slug_owner(self):
        result = self.register_slice(inherited="att-owner-alpha")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.metadata(self.slice_row())["parent_attempt_id"], "att-owner-alpha")

    def test_linked_slice_without_inherited_attempt_id_fails_closed(self):
        before = self.jobs.read_text(encoding="utf-8")
        result = self.register_slice()
        self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
        self.assertIn("reason=live-parent-not-found", result.stdout)
        self.assertIn("child_spawned=0", result.stdout)
        self.assertEqual(self.jobs.read_text(encoding="utf-8"), before)


    def wrapper_register(self, worktree, *, attempt="att-plain-gamma", slug="plain-stage", env=None):
        """A depth-2 launch that is not a slice, straight into the claude wrapper."""
        route = self.route
        command = [
            sys.executable, str(ROOT / "adapters/claude/bin/dispatch-headless.py"),
            "--register", "--worktree", str(worktree), "--slug", slug,
            "--capability", "autopilot-code", "--capability-mode", route["capability_mode"],
            "--worker-mode", self.node["unit"], "--intensity", "standard",
            "--dispatch-depth", "2", "--parent", "owner",
            "--parent-attempt-id", "att-owner-bravo",
            "--parent-harness", "claude", "--parent-transport", "headless",
            "--parent-sandbox", "adapter-default", "--nested-eligibility", "supported",
            "--eligibility-source", "fixture-check", "--fallback-ordinal", "1",
            "--route-file", str(self.route_path), "--route-id", route["route_id"],
            "--route-hash", route["route_hash"], "--route-node", "execute",
            "--registry-digest", route["registry_digest"],
            "--write-scope", ";".join(self.node["write_scope"]),
            "--completion-gate", self.node["completion_gate"],
            "--unit", self.node.get("unit", ""),
            "--model-role", self.node["role"], "--model-profile", self.node["model_profile"],
            "--attempt-id", attempt,
            "--jobs", str(self.jobs), "--log-dir", str(self.base / "logs"),
        ]
        return subprocess.run(command, text=True, capture_output=True, env=env or self.env())

    def test_non_slice_depth2_launch_keeps_exact_worktree_parent_lookup(self):
        result = self.wrapper_register(self.linked)
        self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
        self.assertIn("reason=parent-attempt-not-found", result.stdout)
        self.assertEqual(len(self.rows()), 2)
        result = self.wrapper_register(self.primary)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.metadata(self.slice_row("att-plain-gamma"))["parent_attempt_id"], "att-owner-bravo")

    def test_worker_guard_accepts_linked_worktree(self):
        command = [
            sys.executable, str(ROOT / "utilities/worker-route-guard.py"), "validate",
            "--route", str(self.route_path), "--node", "execute", "--cwd", str(self.linked),
            "--artifact-root", str(self.artifact), "--launch-phase", "start",
            "--model-role", self.node["role"], "--model-profile", self.node["model_profile"],
            "--unit", self.node["unit"],
        ]
        off = subprocess.run(command, text=True, capture_output=True, env=self.env())
        self.assertEqual(off.returncode, 0, off.stdout + off.stderr)
        report = json.loads(off.stdout)
        self.assertEqual(Path(report["cwd"]).resolve(), self.linked)
        self.assertEqual(report["git"]["head"], git("rev-parse", "HEAD", cwd=self.linked))
        self.assertEqual(report["git"]["branch"], "fixture-slice")

    def test_wrapper_start_commands_run_in_the_worktree_they_were_given(self):
        """What `--worktree` becomes in each harness's real launch command (dry-run preview)."""
        for name in ("claude", "codex", "opencode"):
            with self.subTest(wrapper=name):
                command = [
                    sys.executable, str(ROOT / f"adapters/{name}/bin/dispatch-headless.py"),
                    "--dry-run", "--worktree", str(self.linked), "--slug", "dry-slice",
                    "--capability", "autopilot-code", "--capability-mode", "dev",
                    "--qa", "standard", "--intensity", "standard", "--model-role", "fast implementer",
                    "--jobs", str(self.jobs), "--log-dir", str(self.base / "logs"),
                ]
                result = subprocess.run(command, text=True, capture_output=True, env=self.env())
                if result.returncode == 69:  # the harness CLI itself is not probeable here
                    self.skipTest(f"{name}: {result.stdout.strip().splitlines()[1:2]}")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                line = next(ln for ln in result.stdout.splitlines() if ln.startswith("command="))
                self.assertIn(str(self.linked), line)
                self.assertNotIn(f"--worktree {self.primary} ", line)
                self.assertNotIn(f"--dir {self.primary} ", line)


if __name__ == "__main__":
    unittest.main()
