#!/usr/bin/env python3
"""Codex/Claude PID-namespace completion-lifecycle parity, from a packaged root.

Each harness's fixture builds a temporary installed-style package (copied
``utilities/``, ``tools/fleet/``, ``adapters/codex/``, ``adapters/claude/``)
and launches that harness's ``dispatch-headless.py`` from *inside* it, in a
fresh subprocess, so the wrapper's own imports (``dispatch_contract``,
``codex_dispatch_terminal``, the Fleet model, ...) can only resolve under the
package root -- never under this checkout. The launched worker itself is held
open inside a real ``bwrap --unshare-pid`` boundary so the outer observer must
prove liveness from evidence, not from a shared process table.

The test module's own inspection tooling (``dispatch_completion_join`` and
``codex_dispatch_terminal.terminal_envelope_observed``) loads from the
checkout: it only parses the jobs.log/log-file wire format the packaged wrapper
wrote, and is not part of the packaged-root provenance claim. Route compilation
runs from the installed-style package so its sealed root identity is exact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_completion_join as JOIN  # noqa: E402
import dispatch_contract  # noqa: E402
from codex_dispatch_terminal import terminal_envelope_observed  # noqa: E402

# This fixture builds its own route and registry. Inheriting the caller's
# dispatch identity makes the wrapper verify the *caller's* owner route against
# the fixture's, which fails as owner-route-verification-failed when the suite
# runs inside a real dispatch (CI has no such environment, so it only ever bites
# locally). Every fixture environment is scrubbed of these.
INHERITED_DISPATCH_KEYS = (
    "AGENT_OWNER_ROUTE_FILE",
    "AGENT_OWNER_ROUTE_ID",
    "AGENT_OWNER_ROUTE_HASH",
    "AGENT_ROUTE_FILE",
    "AGENT_ROUTE_ID",
    "AGENT_ROUTE_NODE",
    "AGENT_DISPATCH_COMPLETION_STATE_FILE",
    "AGENT_DISPATCH_SUPERVISOR_LEASE_FILE",
    "AGENT_DISPATCH_PARENT_ATTEMPT_ID",
    "AGENT_DISPATCH_ATTEMPT_ID",
    "AGENT_DISPATCH_CHILD",
)


def base_environ() -> dict:
    """os.environ minus any inherited dispatch/route identity."""
    return {k: v for k, v in os.environ.items() if k not in INHERITED_DISPATCH_KEYS}


# Fake harness executables. Both print the harness's real terminal envelope
# shape so codex_dispatch_terminal.terminal_envelope_observed()/
# inspect_terminal_attempt() -- shared across both harnesses -- accept them.
FAKE_CODEX_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import json, os, time\n"
    "open(os.environ['FAKE_PID_FILE'], 'w').write(str(os.getpid()))\n"
    "open(os.environ['FAKE_SENTINEL'], 'w').write(os.environ['AGENT_DISPATCH_ATTEMPT_ID'])\n"
    "while os.path.exists(os.environ['FAKE_SENTINEL']): time.sleep(0.01)\n"
    "text = 'artifact: %s\\nverdict: PASS\\nblocker: none' % os.environ['FAKE_EVIDENCE']\n"
    "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}), flush=True)\n"
    "print(json.dumps({'type':'turn.completed'}), flush=True)\n"
)
FAKE_CLAUDE_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import json, os, time\n"
    "open(os.environ['FAKE_PID_FILE'], 'w').write(str(os.getpid()))\n"
    "open(os.environ['FAKE_SENTINEL'], 'w').write(os.environ['AGENT_DISPATCH_ATTEMPT_ID'])\n"
    "while os.path.exists(os.environ['FAKE_SENTINEL']): time.sleep(0.01)\n"
    "text = 'artifact: %s\\nverdict: PASS\\nblocker: none' % os.environ['FAKE_EVIDENCE']\n"
    "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
    "'result':text}), flush=True)\n"
)

HARNESSES = {
    "codex": {
        "wrapper_rel": "adapters/codex/bin/dispatch-headless.py",
        "fake_bin_name": "codex",
        "fake_script": FAKE_CODEX_SCRIPT,
        "parent_sandbox": "workspace-write",
    },
    "claude": {
        "wrapper_rel": "adapters/claude/bin/dispatch-headless.py",
        "fake_bin_name": "claude",
        "fake_script": FAKE_CLAUDE_SCRIPT,
        "parent_sandbox": "adapter-default",
    },
}

# The packaged-root subprocess runner mirrors dispatch-headless.py's own
# check_runtime_projection()/ensure_runtime_home_projection() mocks so it
# never shells out to the real preflight.sh, while every other import comes
# from the package root passed as argv[1].
RUNNER_SCRIPT = """
import contextlib, importlib.util, io, json, sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

wrapper_path = Path(sys.argv[1])
argv = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

spec = importlib.util.spec_from_file_location("pidns_packaged_wrapper", wrapper_path)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)

wrapper_output = io.StringIO()
wrapper_errors = io.StringIO()
patches = [
    mock.patch.object(wrapper, "launch_summary_owner",
                       return_value={"summary_owner": "pidns-fixture"}),
]
# Only Codex's wrapper shells to preflight.sh for a runtime-projection check
# and symlinks a runtime home; Claude's wrapper has neither concept.
if hasattr(wrapper, "check_runtime_projection"):
    patches.append(mock.patch.object(wrapper, "check_runtime_projection", return_value=0))
if hasattr(wrapper, "ensure_runtime_home_projection"):
    patches.append(mock.patch.object(wrapper, "ensure_runtime_home_projection", return_value=None))
if hasattr(wrapper, "reconcile_launch_lifecycle") and hasattr(wrapper, "DETACHED"):
    lifecycle_resolution = wrapper.reconcile_launch_lifecycle(
        wrapper.DETACHED,
        {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
        evidence={"lifecycle_selector_source": "host-like"},
    )
    patches.append(mock.patch.object(
        wrapper, "reconcile_launch_lifecycle", return_value=lifecycle_resolution
    ))
with contextlib.ExitStack() as stack:
    for patch in patches:
        stack.enter_context(patch)
    stack.enter_context(redirect_stdout(wrapper_output))
    stack.enter_context(redirect_stderr(wrapper_errors))
    code = wrapper.main(argv)

provenance = {
    name: getattr(module, "__file__", None)
    for name, module in sys.modules.items()
    if name in (
        "dispatch_contract", "codex_dispatch_terminal", "codex_managed_dispatch",
        "dispatch_summary", "dispatch_lifecycle", "worker_bootstrap",
    )
}
print("PIDNS_RESULT:" + json.dumps({
    "code": code,
    "sys_path": sys.path,
    "provenance": provenance,
    "wrapper_output": wrapper_output.getvalue(),
    "wrapper_errors": wrapper_errors.getvalue(),
}))
"""

# A second small packaged-root runner that exercises the shared F-25
# classifier directly. It is harness-agnostic (both wrappers publish rows
# read by the same tools/fleet/model.py), so it runs once per fixture rather
# than once per harness.
CLASSIFIER_RUNNER_SCRIPT = """
import importlib.util, json, sys, time
from pathlib import Path

package_root = Path(sys.argv[1])
sys.path.insert(0, str(package_root / "utilities"))
sys.path.insert(0, str(package_root / "tools"))

def load(rel, name):
    spec = importlib.util.spec_from_file_location(name, package_root / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

dispatch_contract = load("utilities/dispatch_contract.py", "pidns_dispatch_contract")
fleet_model = load("tools/fleet/model.py", "pidns_fleet_model")

now = time.time()
base_identity = {"attempt_id": "att-x", "route_id": "rt-x", "route_node": "plan"}

def classify(**overrides):
    ev = dict(base_identity)
    ev.update(overrides)
    return fleet_model.classify_attempt_evidence(ev, now=now)["state"]

results = {}

# Stale heartbeat alone, no positive tag -> unknown.
results["stale_heartbeat_alone"] = classify(
    pid_scope="namespace-local",
    heartbeat={**base_identity, "phase": "tool", "sequence": 1, "updated_at": now - 99999},
)

# Unverifiable scan with a stale heartbeat -> unknown (never dead/working).
results["unverifiable_scan_stale_heartbeat"] = classify(
    pid_scope="namespace-local",
    attempt_descendants="unverifiable",
    heartbeat={**base_identity, "phase": "tool", "sequence": 1, "updated_at": now - 99999},
)

# Unverifiable scan with a fresh heartbeat -> working (bounded fallback only).
results["unverifiable_scan_fresh_heartbeat"] = classify(
    pid_scope="namespace-local",
    attempt_descendants="unverifiable",
    heartbeat={**base_identity, "phase": "tool", "sequence": 1, "updated_at": now - 1},
)

# Authoritative empty scan beats a fresh heartbeat -> dead.
results["empty_scan_beats_fresh_heartbeat"] = classify(
    pid_scope="namespace-local",
    attempt_descendants="empty",
    heartbeat={**base_identity, "phase": "tool", "sequence": 1, "updated_at": now - 1},
)

# PID reuse alone (no populated descendant) never synthesizes liveness.
results["pid_reuse_alone"] = classify(
    pid_scope="namespace-local",
    pid_authoritative=True,
    pid_alive=False,
)

# A populated descendant vetoes PID reuse -> working.
results["pid_reuse_with_populated_descendant"] = classify(
    pid_scope="namespace-local",
    pid_authoritative=True,
    pid_alive=False,
    attempt_descendants="populated",
)

# Real attempt_tagged_descendants(): an attempt id nobody holds, scanned from
# this actual namespace with matching observer -> a real proven-empty scan.
inner_ns = dispatch_contract.process_namespace_identity()
results["real_empty_scan"] = dispatch_contract.attempt_tagged_descendants({
    "attempt_id": "att-nobody-held-by-any-process",
    "pid_observer_ns": inner_ns,
}).state

# Same scan, but the recorded observer namespace does not match this one ->
# unverifiable (invisibility, never treated as absence).
results["real_foreign_scan"] = dispatch_contract.attempt_tagged_descendants({
    "attempt_id": "att-nobody-held-by-any-process",
    "pid_observer_ns": "pid:[foreign-namespace-marker]",
}).state

print("PIDNS_RESULT:" + json.dumps(results))
"""

LIVE_OBSERVER_RUNNER_SCRIPT = """
import importlib.util, json, sys, time
from pathlib import Path

package_root = Path(sys.argv[1])
attempt_id = sys.argv[2]
sys.path.insert(0, str(package_root / "utilities"))
sys.path.insert(0, str(package_root / "tools"))

def load(rel, name):
    spec = importlib.util.spec_from_file_location(name, package_root / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

dispatch_contract = load("utilities/dispatch_contract.py", "pidns_live_dispatch_contract")
fleet_model = load("tools/fleet/model.py", "pidns_live_fleet_model")
observer_ns = dispatch_contract.process_namespace_identity()
scan = dispatch_contract.attempt_tagged_descendants({
    "attempt_id": attempt_id,
    "pid_observer_ns": observer_ns,
})
verdict = fleet_model.classify_attempt_evidence({
    "attempt_id": attempt_id,
    "route_id": "rt-pidns",
    "route_node": "execute",
    "pid_scope": "namespace-local",
    "attempt_descendants": scan.state,
    "heartbeat": {
        "attempt_id": attempt_id, "route_id": "rt-pidns", "route_node": "execute",
        "phase": "tool", "sequence": 1, "updated_at": time.time() - 99999,
    },
}, now=time.time())
print("PIDNS_RESULT:" + json.dumps({
    "scan_state": scan.state,
    "verdict": verdict,
    "provenance": {
        name: getattr(module, "__file__", None)
        for name, module in sys.modules.items()
        if name in ("pidns_live_dispatch_contract", "pidns_live_fleet_model")
    },
    "sys_path": sys.path,
}))
"""


def build_package_root(base: Path) -> Path:
    package_root = base / "packaged-root"
    for rel in (
        "utilities", "tools", "adapters/codex", "adapters/claude",
        "capabilities", "roles", "core", "hooks",
    ):
        src = ROOT / rel
        dst = package_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    for rel in ("harness-manifest.json",):
        src = ROOT / rel
        if src.is_file():
            shutil.copy2(src, package_root / rel)
    return package_root


class NamespaceE2E(unittest.TestCase):
    def enter_pid_namespace(self) -> bool:
        if os.environ.get("HEARTING_PIDNS_CHILD") == "1":
            return False
        bwrap = shutil.which("bwrap")
        required = os.environ.get("HEARTING_REQUIRE_PIDNS") == "1"
        if not bwrap:
            if required:
                self.fail("bubblewrap is required for the PID namespace regression")
            self.skipTest("bubblewrap is unavailable")
        # A hand-built /dev containing only an empty `null` has no /dev/urandom,
        # so git inside the namespace dies with "unable to get random bytes for
        # temporary file" before the test under test even runs. bwrap's own
        # --dev supplies a minimal, correct devtmpfs.
        base = [
            bwrap,
            "--die-with-parent",
            "--unshare-pid",
            "--ro-bind", "/", "/",
            "--proc", "/proc",
            "--dev", "/dev",
            # The namespace must own a writable temporary directory: binding
            # the host /tmp after a read-only root can leave the inner mount
            # absent, making git/tempfile fixtures fail before the liveness
            # assertions run.
            "--tmpfs", "/tmp",
        ]
        # `/` is bound read-only, so a TMPDIR outside /tmp (the test runner
        # isolates one under /var/tmp) is unwritable inside the namespace and
        # mktemp fails. Bind whatever TMPDIR actually is.
        tmpdir = os.environ.get("TMPDIR", "").rstrip("/")
        if tmpdir and not tmpdir.startswith("/tmp") and os.path.isdir(tmpdir):
            base += ["--bind", tmpdir, tmpdir]
        probe = subprocess.run([*base, "true"], text=True, capture_output=True)
        if probe.returncode:
            if required:
                self.fail("bubblewrap PID namespace unavailable: " + probe.stderr.strip())
            self.skipTest("bubblewrap PID namespace unavailable: " + probe.stderr.strip())
        env = {
            **base_environ(),
            "HEARTING_PIDNS_CHILD": "1",
            "HEARTING_HOST_PID_NS": os.readlink("/proc/self/ns/pid"),
            # The namespace owns a fresh /tmp. An inherited TMPDIR below the
            # host /tmp would be hidden by that mount and make preflight's
            # mktemp fail before the fixture reaches the wrapper.
            "TMPDIR": "/tmp",
        }
        result = subprocess.run(
            [
                *base,
                sys.executable,
                str(Path(__file__).resolve()),
                f"{self.__class__.__name__}.{self._testMethodName}",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return True

    def run_packaged(self, package_root: Path, wrapper_path: Path, argv: list[str], env: dict) -> dict:
        with tempfile.TemporaryDirectory() as scratch:
            runner_path = Path(scratch) / "runner.py"
            runner_path.write_text(RUNNER_SCRIPT, encoding="utf-8")
            argv_path = Path(scratch) / "argv.json"
            argv_path.write_text(json.dumps(argv), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(runner_path), str(wrapper_path), str(argv_path)],
                env=env,
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = next(
            (ln for ln in result.stdout.splitlines() if ln.startswith("PIDNS_RESULT:")),
            None,
        )
        self.assertIsNotNone(line, result.stdout + result.stderr)
        return json.loads(line[len("PIDNS_RESULT:"):])

    def start_packaged(self, package_root: Path, wrapper_path: Path, argv: list[str], env: dict):
        scratch = tempfile.TemporaryDirectory()
        runner_path = Path(scratch.name) / "runner.py"
        runner_path.write_text(RUNNER_SCRIPT, encoding="utf-8")
        argv_path = Path(scratch.name) / "argv.json"
        argv_path.write_text(json.dumps(argv), encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(runner_path), str(wrapper_path), str(argv_path)],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        process._pidns_scratch = scratch
        return process

    def run_live_observer(self, package_root: Path, attempt: str, env: dict) -> dict:
        with tempfile.TemporaryDirectory() as scratch:
            runner_path = Path(scratch) / "live_observer.py"
            runner_path.write_text(LIVE_OBSERVER_RUNNER_SCRIPT, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(runner_path), str(package_root), attempt],
                env=env,
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = next(
            (ln for ln in result.stdout.splitlines() if ln.startswith("PIDNS_RESULT:")),
            None,
        )
        self.assertIsNotNone(line, result.stdout + result.stderr)
        return json.loads(line[len("PIDNS_RESULT:"):])

    def run_classifier_checks(self, package_root: Path) -> dict:
        with tempfile.TemporaryDirectory() as scratch:
            runner_path = Path(scratch) / "classifier_runner.py"
            runner_path.write_text(CLASSIFIER_RUNNER_SCRIPT, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(runner_path), str(package_root)],
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = next(
            (ln for ln in result.stdout.splitlines() if ln.startswith("PIDNS_RESULT:")),
            None,
        )
        self.assertIsNotNone(line, result.stdout + result.stderr)
        return json.loads(line[len("PIDNS_RESULT:"):])

    def _run_harness(self, harness: str) -> None:
        if self.enter_pid_namespace():
            return
        config = HARNESSES[harness]
        host_namespace = os.environ["HEARTING_HOST_PID_NS"]
        inner_namespace = os.readlink("/proc/self/ns/pid")
        self.assertNotEqual(inner_namespace, host_namespace)

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # A CI runner's real $HOME can be missing/read-only; git itself
            # (not just this fixture's own subprocess env dicts further down)
            # needs a writable HOME for its own temp-file/config operations,
            # so this must exist before the very first git call.
            home = base / "home"
            home.mkdir()
            git_env = {**base_environ(), "HOME": str(home)}
            repo = base / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True, env=git_env)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"],
                check=True, env=git_env,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Fixture"],
                check=True, env=git_env,
            )
            (repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True, env=git_env)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True, env=git_env)

            package_root = build_package_root(base)
            wrapper_path = package_root / config["wrapper_rel"]
            self.assertTrue(wrapper_path.is_file(), wrapper_path)

            artifact_root = base / ".agent_reports"
            artifact_root.mkdir()
            model_governor_root = artifact_root / ".runtime" / "model-worker-governor"
            evidence = artifact_root / "pass.md"
            evidence.write_text(f"{harness} namespace PASS\n", encoding="utf-8")
            agent_home = package_root
            jobs = base / "jobs.log"
            logs = base / "logs"

            dispatch = {"tuples": [{
                "parent_harness": harness,
                "parent_transport": "headless",
                "parent_sandbox": config["parent_sandbox"],
                "child_harness": harness,
                "launch_authority": "conductor",
                "status": "supported",
                "probe_source": f"{harness}-pidns-fixture",
                "probe_time": "2026-08-16T00:00:00Z",
                "failure_class": "",
                "checked_worktree": str(repo.resolve()),
                "failure_scope": "none",
                "codex_command": "ok",
                "retry_on_isolated_worktree": 0,
            }], "native_subagent": []}
            # Compile from the installed-style package itself. The launch tuple
            # binds both the registry and runtime roots, so compiling through
            # this checkout and launching a copied wrapper would correctly fail
            # closed as launch-runtime-root-mismatch.
            dispatch_path = base / "dispatch-evidence.json"
            dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")
            route_env = {
                **base_environ(),
                "AGENT_HOME": str(agent_home),
                "AGENT_ARTIFACT_ROOT": str(artifact_root),
                "AGENT_DISPATCH_JOBS": str(jobs),
                "HOME": str(home),
                "XDG_STATE_HOME": str(base / "state"),
            }
            compiled = subprocess.run(
                [
                    sys.executable,
                    str(package_root / "utilities" / "capability-route.py"),
                    "compile",
                    "--capability", "autopilot-code",
                    "--capability-mode", "dev",
                    "--intensity", "standard",
                    "--cwd", str(repo),
                    "--artifact-root", str(artifact_root),
                    "--signal", "shared-contract",
                    "--transport", "headless",
                    "--tracking", "tracked",
                    "--spec-read", "fixture",
                    "--drift-verdict", "within-spec",
                    "--workflow-mode", "tracked",
                    "--artifact-guard", "fixture",
                    "--dispatch-evidence", str(dispatch_path),
                ],
                env=route_env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            route = json.loads(compiled.stdout)
            route_path = (
                artifact_root / ".runtime" / "routes" / f"{route['route_id']}.json"
            )
            self.assertTrue(route_path.is_file(), route_path)
            node = next(item for item in route["nodes"] if item["id"] == "plan")
            fixture_env = {
                **base_environ(),
                "AGENT_HOME": str(agent_home),
                "AGENT_ARTIFACT_ROOT": str(artifact_root),
                "AGENT_MODEL_GOVERNOR_ROOT": str(model_governor_root),
                "HOME": str(home),
            }
            # `complete` writes the predecessor markers and `--start` reads them.
            # resolve_dispatch_state_root() picks its root from explicit jobs ->
            # AGENT_DISPATCH_JOBS -> a per-user fallback, so dropping the variable
            # here sent the two calls to structurally different roots: the markers
            # landed, and --start still reported every dependency as absent. Bind
            # both to the same fixture registry and state home.
            fixture_env["AGENT_DISPATCH_JOBS"] = str(jobs)
            fixture_env["XDG_STATE_HOME"] = str(base / "state")
            for predecessor in node.get("depends_on", []):
                predecessor_evidence = artifact_root / f"{predecessor}.md"
                predecessor_evidence.write_text("fixture predecessor\n", encoding="utf-8")
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "utilities" / "capability-route.py"),
                        "complete",
                        "--route", str(route_path),
                        "--node", predecessor,
                        "--evidence", str(predecessor_evidence),
                        "--attempt-id", f"att-inline-{predecessor}",
                        "--dispatch-depth", "2",
                        "--transport", "interactive",
                        "--execution-surface", "inline",
                        "--registered-worker", "0",
                        "--fallback-hop", "inline",
                    ],
                    env=fixture_env,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )

            parent = subprocess.Popen(["sleep", "60"])
            self.addCleanup(lambda: parent.poll() is None and parent.kill())
            parent_start = (Path("/proc") / str(parent.pid) / "stat").read_text().split()[21]
            jobs.write_text(
                f"2026-08-16T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                f"fallback_hop=same-harness-headless,worker_type=owner,harness={harness},"
                f"runtime_sandbox={config['parent_sandbox']},attempt_id=att-parent-pidns,"
                f"pid={parent.pid},pid_start={parent_start}\n",
                encoding="utf-8",
            )

            fakebin = base / "bin"
            fakebin.mkdir()
            fake = fakebin / config["fake_bin_name"]
            fake.write_text(config["fake_script"], encoding="utf-8")
            fake.chmod(0o755)

            attempt = f"att-{harness}-pidns-pass"
            argv = [
                "dispatch-headless.py", "--start",
                "--worktree", str(repo),
                "--slug", f"{harness}-pidns-plan",
                "--capability", "autopilot-code",
                "--capability-mode", route["capability_mode"],
                "--worker-mode", node["unit"],
                "--intensity", route["effective_intensity"],
                "--dispatch-depth", "2",
                "--parent", "owner",
                "--worker-role", "code-plan",
                "--owner", "autopilot-code",
                "--jobs", str(jobs),
                "--log-dir", str(logs),
                "--attempt-id", attempt,
                "--parent-harness", harness,
                "--parent-transport", "headless",
                "--parent-sandbox", config["parent_sandbox"],
                "--launch-authority", "conductor",
                "--nested-eligibility", "supported",
                "--eligibility-source", f"{harness}-pidns-fixture",
                "--fallback-ordinal", "1",
                "--route-file", str(route_path),
                "--route-id", route["route_id"],
                "--route-hash", route["route_hash"],
                "--route-node", node["id"],
                "--registry-digest", route["registry_digest"],
                "--write-scope", ";".join(node["write_scope"]),
                "--unit", node["unit"],
                "--model-role", node["role"],
                "--model-profile", node["model_profile"],
                "--foreground-timeout", "10",
                "--prompt-text", "fixture",
            ]
            marker_root = dispatch_contract.dispatch_state_root(jobs) / "completion" / route["route_id"]
            for predecessor in node.get("depends_on", []):
                self.assertTrue(
                    (marker_root / f"{predecessor}.json").is_file(),
                    f"predecessor marker missing before --start: {predecessor} "
                    f"(looked in {marker_root}); `complete` returning 0 is not "
                    f"evidence that --start can find the marker",
                )
            env = {
                **base_environ(),
                "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
                "HOME": str(home),
                "AGENT_HOME": str(agent_home),
                "AGENT_ARTIFACT_ROOT": str(artifact_root),
                "AGENT_MODEL_GOVERNOR_ROOT": str(model_governor_root),
                "AGENT_DISPATCH_JOBS": str(jobs),
                "AGENT_DISPATCH_CHILD": "1",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-pidns",
                "AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1",
                "FAKE_EVIDENCE": str(evidence),
                "FAKE_PID_FILE": str(base / f"{harness}-fake.pid"),
                "FAKE_SENTINEL": str(base / f"{harness}-fake.sentinel"),
                "XDG_STATE_HOME": str(base / "state"),
            }
            env.pop("PYTHONPATH", None)

            sentinel = Path(env["FAKE_SENTINEL"])
            fake_pid_file = Path(env["FAKE_PID_FILE"])
            packaged = self.start_packaged(package_root, wrapper_path, argv, env)

            def cleanup_packaged():
                sentinel.unlink(missing_ok=True)
                if packaged.poll() is None:
                    packaged.kill()
                try:
                    packaged.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    packaged.kill()
                    packaged.wait(timeout=5)

            self.addCleanup(cleanup_packaged)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if sentinel.is_file() and fake_pid_file.is_file():
                    break
                if packaged.poll() is not None:
                    stdout, stderr = packaged.communicate()
                    self.fail(f"{harness} worker exited before live observation: {stdout}{stderr}")
                time.sleep(0.01)
            else:
                self.fail(f"{harness} worker did not open its sentinel")
            child_pid = int(fake_pid_file.read_text(encoding="utf-8"))
            child_environ = (Path("/proc") / str(child_pid) / "environ").read_bytes()
            self.assertIn(
                f"AGENT_DISPATCH_ATTEMPT_ID={attempt}".encode(), child_environ,
            )
            open_row = JOIN.exact_attempt_row(jobs, attempt)
            self.assertEqual(open_row.status, "open", open_row)

            live_result = self.run_live_observer(package_root, attempt, env)
            self.assertEqual(live_result["scan_state"], "populated", live_result)
            self.assertEqual(live_result["verdict"]["state"], "working", live_result)
            for module_file in live_result["provenance"].values():
                self.assertTrue(module_file.startswith(str(package_root.resolve())), module_file)
                self.assertFalse(module_file.startswith(str(ROOT.resolve())), module_file)
            for entry in live_result["sys_path"]:
                self.assertFalse(entry.startswith(str(ROOT.resolve())), entry)

            sentinel.unlink()
            stdout, stderr = packaged.communicate(timeout=10)
            self.assertEqual(packaged.returncode, 0, stdout + stderr)
            result_line = next(
                (ln for ln in stdout.splitlines() if ln.startswith("PIDNS_RESULT:")), None,
            )
            self.assertIsNotNone(result_line, stdout + stderr)
            run_result = json.loads(result_line[len("PIDNS_RESULT:"):])
            self.assertEqual(run_result["code"], 0, run_result)

            # Provenance: every shared/Fleet module the wrapper imported must
            # resolve below this fixture's package root, never below the
            # checkout that this test file itself lives in.
            package_root_str = str(package_root.resolve())
            checkout_str = str(ROOT.resolve())
            for module_name, module_file in run_result["provenance"].items():
                self.assertIsNotNone(module_file, module_name)
                self.assertTrue(
                    module_file.startswith(package_root_str),
                    f"{module_name} resolved outside the package root: {module_file}",
                )
                self.assertFalse(
                    module_file.startswith(checkout_str),
                    f"{module_name} leaked the checkout path: {module_file}",
                )
            for entry in run_result["sys_path"]:
                self.assertFalse(
                    entry.startswith(checkout_str),
                    f"checkout path leaked onto sys.path: {entry}",
                )

            self.assertFalse(sentinel.exists())

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                published = JOIN.exact_attempt_row(jobs, attempt)
                if terminal_envelope_observed(published.metadata.get("log_file")):
                    break
                time.sleep(0.005)
            else:
                self.fail(f"{harness} terminal envelope was not published")
            blocked_receipt = JOIN.join_batch(
                jobs=jobs,
                parent_attempt_id="att-parent-pidns",
                interval=0.005,
                timeout=0.03,
            )
            self.assertEqual(blocked_receipt["state"], "timeout", blocked_receipt)
            self.assertEqual(
                blocked_receipt["children"][0]["reason"],
                "process-unverifiable",
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                finished = JOIN.exact_attempt_row(jobs, attempt)
                if finished.status == "done":
                    break
                time.sleep(0.02)
            else:
                self.fail(f"{harness} detached receipt watcher did not close the PASS attempt")
            parent.terminate()
            parent.wait(timeout=5)
            self.assertEqual(finished.status, "done")
            self.assertEqual(finished.metadata.get("note"), "completed-marker")
            # `note=completed-marker` and `failure_class=pass` are written
            # together by the marker-bound-delivery-v1 classifier (6c994ba8), and
            # worker-route-guard requires the positive `pass` value to accept a
            # slice's lineage. Asserting None here predates that commit.
            self.assertEqual(finished.metadata.get("failure_class"), "pass")
            self.assertEqual(finished.metadata.get("pid_scope"), "namespace-local")
            self.assertEqual(finished.metadata.get("pid_ns"), inner_namespace)
            self.assertEqual(finished.metadata.get("pid_observer_ns"), inner_namespace)
            self.assertEqual(
                finished.metadata.get("launch_outcome"),
                "governed-process-group-drained",
            )
            self.assertEqual(finished.metadata.get("group_reap_proof"), "pgid-empty-v1")
            self.assertEqual(finished.metadata.get("group_reap_pgid"), finished.metadata.get("pid"))
            self.assertEqual(
                finished.metadata.get("attempt_descendant_proof"),
                "attempt-tagged-empty-v1",
            )
            # A one-element (or any) inner NSpid vector must never publish a
            # host-identity claim from a plain bwrap remount: no outer procfs
            # root was independently proven, so pid_host* stays absent.
            for host_field in (
                "pid_host", "pid_host_start", "pid_host_ns", "pid_host_proof", "pgid_host",
            ):
                self.assertNotIn(host_field, finished.metadata)
            marker = Path(finished.metadata["completion_marker"])
            self.assertTrue(marker.is_file(), marker)
            marker_value = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(marker_value["attempt_id"], attempt)

            ready = JOIN.join_batch(
                jobs=jobs,
                parent_attempt_id="att-parent-pidns",
                interval=0.01,
                timeout=1,
            )
            self.assertEqual(ready["state"], "ready")
            self.assertEqual(ready["children"][0]["required_action"], "advance-completed")

            # Harness-agnostic F-25 classifier ladder, exercised from the same
            # package root and the same real PID namespace: forged/missing
            # proof, a stale heartbeat alone, an unverifiable scan, and PID
            # reuse never synthesize liveness; a proven-empty scan (real,
            # authoritative) still outranks a fresh heartbeat; and a real
            # empty attempt-tagged scan from a foreign observer namespace
            # stays unverifiable rather than becoming a false absence.
            classifier = self.run_classifier_checks(package_root)
            self.assertEqual(classifier["stale_heartbeat_alone"], "unknown", classifier)
            self.assertEqual(classifier["unverifiable_scan_stale_heartbeat"], "unknown", classifier)
            self.assertEqual(classifier["unverifiable_scan_fresh_heartbeat"], "working", classifier)
            self.assertEqual(classifier["empty_scan_beats_fresh_heartbeat"], "dead", classifier)
            self.assertNotEqual(classifier["pid_reuse_alone"], "working", classifier)
            self.assertEqual(classifier["pid_reuse_with_populated_descendant"], "working", classifier)
            self.assertEqual(classifier["real_empty_scan"], "empty", classifier)
            self.assertEqual(classifier["real_foreign_scan"], "unverifiable", classifier)

    def test_codex_pass_receipt_marker_and_join_are_ordered(self) -> None:
        self._run_harness("codex")

    def test_claude_pass_receipt_marker_and_join_are_ordered(self) -> None:
        self._run_harness("claude")


if __name__ == "__main__":
    unittest.main()
