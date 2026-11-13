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
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import os
from pathlib import Path
import select
import signal
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
    return {k: v for k, v in os.environ.items()
            if k not in INHERITED_DISPATCH_KEYS and not k.startswith(
                ("AGENT_ARTIFACT_", "AGENT_PRODUCER_", "AGENT_ROUTE_",
                 "AGENT_OWNER_ROUTE_", "AGENT_DISPATCH_", "REPORT_BUNDLE_"))}


# Fake harness executables. Both print the harness's real terminal envelope
# shape so codex_dispatch_terminal.terminal_envelope_observed()/
# inspect_terminal_attempt() -- shared across both harnesses -- accept them.
FAKE_CODEX_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys, time\n"
    "if sys.argv[1:2] == ['app-server']: sys.exit(69)\n"
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
from fleet import model as fleet_model
sys.modules["pidns_fleet_model"] = fleet_model

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
from fleet import model as fleet_model
sys.modules["pidns_live_fleet_model"] = fleet_model
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


# SD-OPEN-61 needs two real process tables: this supervisor holds the lease
# outside bwrap and launches its tagged guard inside. No model CLI is involved.
SD61_HOST_SUPERVISOR = """
import fcntl, json, os, subprocess, sys
from pathlib import Path

lease = Path(sys.argv[1]).open("w+")
lease.write(sys.argv[2])
lease.flush()
fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
stat = Path("/proc/self/stat").read_text()
print(json.dumps({"pid": os.getpid(), "pid_start": stat[stat.rfind(")") + 2:].split()[19],
                  "pid_ns": os.readlink("/proc/self/ns/pid")}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    if command["action"] == "release":
        fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        print(json.dumps({"released": True}), flush=True)
    elif command["action"] == "probe":
        result = subprocess.run(command["argv"], text=True, capture_output=True,
            env={**os.environ, "AGENT_DISPATCH_ATTEMPT_ID": command["attempt"]}, timeout=15)
        print(json.dumps({"code": result.returncode, "stdout": result.stdout,
                          "stderr": result.stderr}), flush=True)
    elif command["action"] == "exit":
        break
lease.close()
"""

SD61_INNER_GUARD = """
import fcntl, json, os, sys
from pathlib import Path

contract_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(contract_root / "utilities"))
import dispatch_contract as D
assert Path(D.__file__).resolve() == contract_root / "utilities/dispatch_contract.py"
jobs = Path(sys.argv[2])
fields = jobs.read_text().strip().split("\\t")
metadata = D.parse_registry_metadata(fields[5])
attempt = metadata["attempt_id"]
assert os.environ["AGENT_DISPATCH_ATTEMPT_ID"] == attempt
inner_ns = D.process_namespace_identity()
assert inner_ns != metadata["pid_observer_ns"]
host_pid_visible = (Path("/proc") / metadata["pid"]).exists()
assert not host_pid_visible
governed = D.attempt_governed_process_quiescence(metadata)
assert (governed.state, governed.reason) == ("unverifiable", "process-namespace-unverifiable")
descendants = D.attempt_tagged_descendants(metadata)
assert descendants.state == "populated"
assert os.getpid() in [member[0] for member in descendants.members]
aggregate = D.attempt_process_quiescence(metadata)
assert (aggregate.state, aggregate.reason) == ("live", "attempt-descendant-live")
with Path(metadata["supervisor_lease_file"]).open("r+") as lease:
    try:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raw_lease_locked = True
    else:
        raw_lease_locked = False
        fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
result = {"module": str(Path(D.__file__).resolve()), "outer_pid": int(metadata["pid"]),
          "inner_pid": os.getpid(), "outer_namespace": metadata["pid_observer_ns"],
          "inner_namespace": inner_ns, "host_pid_visible": host_pid_visible,
          "recorded_pid_scope": metadata["pid_scope"], "recorded_pgid": metadata["pgid"],
          "governed_reason": governed.reason, "descendants": descendants.state,
          "tagged_self": True, "raw_lease_locked": raw_lease_locked,
          "exact_lease_held": D.supervisor_lease_is_held(jobs, metadata)}
try:
    binding = D.resolve_live_parent_attempt(jobs, parent_slug=fields[4], repo=fields[2],
        worktree=fields[3], expected_attempt_id=attempt)
except D.DispatchContractError as exc:
    result.update(resolved=False, reason=exc.reason, detail=str(exc))
else:
    result.update(resolved=True, source=binding.liveness_source,
                  observed_pid=binding.observed_pid,
                  revalidated=D.parent_attempt_binding_is_live(jobs, binding))
print(json.dumps(result, sort_keys=True))
"""


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
            # Linked checkouts used for local verification may live in /tmp.
            "--ro-bind", str(ROOT), str(ROOT),
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
                    "--slug", "dispatch-namespace-fixture",
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
                predecessor_node = next(item for item in route["nodes"]
                                        if item["id"] == predecessor)
                predecessor_attempt = f"att-fixture-{predecessor}"
                # The frame pair gate proves exact completed registry attempts;
                # inline marker files alone are not registered frame evidence.
                with jobs.open("a", encoding="utf-8") as registry:
                    registry.write(
                        f"2026-08-16T00:00:00Z\topen\t{repo}\t{repo}\t{predecessor}\t"
                        "attempt_schema_version=2,transport=headless,"
                        "execution_surface=registered-headless,registered_worker=1,"
                        "fallback_hop=same-harness-headless,launch_outcome=never-launched,"
                        f"dispatch_depth={predecessor_node['dispatch_depth']},"
                        f"worker_type={predecessor_node['worker_type']},harness={harness},"
                        f"attempt_id={predecessor_attempt},route_file={route_path},"
                        f"route_id={route['route_id']},route_hash={route['route_hash']},"
                        f"route_node={predecessor},artifact_root={artifact_root}\n"
                    )
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "utilities" / "capability-route.py"),
                        "complete",
                        "--route", str(route_path),
                        "--node", predecessor,
                        "--evidence", str(predecessor_evidence),
                        "--attempt-id", predecessor_attempt,
                        "--jobs", str(jobs),
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
                jobs.read_text(encoding="utf-8") +
                f"2026-08-16T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                f"fallback_hop=same-harness-headless,worker_type=owner,harness={harness},"
                f"runtime_sandbox={config['parent_sandbox']},attempt_id=att-parent-pidns,"
                f"parent_sid=pidns-fixture,owner_route_id={route['route_id']},"
                # A fixture-only depth-0 recipient on the supported human-gate
                # carrier; independent of the mock child's harness below.
                "parent_completion_delivery=claude-parent-runtime,"
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
                "--foreground-timeout", "60",
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

            # The real launch fence must reject an entry gate that has never
            # been raised. Prepare it through the supervisor, not a forged
            # release sidecar, before testing the live namespace lifecycle.
            refused = self.run_packaged(package_root, wrapper_path, argv, env)
            self.assertEqual(refused["code"], 65, refused)
            self.assertIn("reason=human-gate-not-raised", refused["wrapper_output"])
            self.assertIn("child_spawned=0", refused["wrapper_output"])
            import frame_interview
            gate_artifact = artifact_root / "frame-review.json"
            summary_path = artifact_root / "frame-summary.json"
            summary_path.write_text(json.dumps({"route_id": route["route_id"],
                "artifacts": [str(artifact_root / f"{name}.md")
                              for name in node.get("depends_on", [])]}), encoding="utf-8")
            interview = {
                "schema": frame_interview.SCHEMA, "route_id": route["route_id"],
                "summary": str(summary_path),
                "understanding": "Check completion behavior in an isolated process.",
                "brief": {"problem": "Completion needs verification.",
                          "outcome": "Record the observed result.",
                          "affected": "The test only.",
                          "constraints": "Use fake executables.", "open": ""},
                "questions": [],
            }
            gate_artifact.write_text(json.dumps(interview), encoding="utf-8")
            answers = frame_interview.answers_template(interview)
            answers["actor_kind"] = "user"
            answers["understanding_confirmed"] = True
            answers_path = artifact_root / "frame-answers.json"
            answers_path.write_text(json.dumps(answers), encoding="utf-8")
            for action in (["gate", "--block", "--artifact", str(gate_artifact)],
                           ["release", "--decision", "proceed", "--answers", str(answers_path)]):
                gate = subprocess.run(
                    [sys.executable, str(package_root / "utilities" / "workflow-supervisor.py"),
                     *action, "--route", str(route_path), "--gate", "frame-review",
                     "--jobs", str(jobs)],
                    env=fixture_env, text=True, capture_output=True,
                )
                self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)

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
            # The Codex launch runs several Python helpers before the worker
            # starts; on a loaded CI runner that alone has taken over 5s. The
            # loop leaves as soon as the sentinel appears or the wrapper exits,
            # and --foreground-timeout above stays well past this bound.
            deadline = time.monotonic() + 30
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

            # Hold the detached reap watcher at the exact pre-receipt edge.
            # Without this barrier a fast CI runner can close the row between
            # observing the terminal envelope and the intended timeout probe,
            # turning a transition assertion into a scheduler race.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                open_row = JOIN.exact_attempt_row(jobs, attempt)
                if open_row.metadata.get("reap_watch_pid"):
                    break
                if packaged.poll() is not None:
                    self.fail(f"{harness} wrapper exited before publishing reap watcher identity")
                time.sleep(0.005)
            else:
                self.fail(f"{harness} wrapper did not publish reap watcher identity")
            reap_watch_pid = int(open_row.metadata["reap_watch_pid"])
            os.kill(reap_watch_pid, signal.SIGSTOP)
            watcher = {"stopped": True}

            def resume_watcher() -> None:
                if not watcher["stopped"]:
                    return
                try:
                    os.kill(reap_watch_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                watcher["stopped"] = False

            self.addCleanup(resume_watcher)
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
            resume_watcher()
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

    def test_sd61_real_outer_supervisor_lease_survives_tagged_inner_guard(self) -> None:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            if os.environ.get("HEARTING_REQUIRE_PIDNS") == "1":
                self.fail("bubblewrap is required for the SD-OPEN-61 regression")
            self.skipTest("bubblewrap is unavailable")
        # Override only the guard's import root for installed-vs-checkout
        # reproduction. The loaded module path is asserted inside the namespace.
        contract_root = Path(os.environ.get("HEARTING_SD61_CONTRACT_ROOT", ROOT)).resolve()
        with tempfile.TemporaryDirectory(prefix="sd61-pidns-") as temp_dir:
            base = Path(temp_dir).resolve()
            jobs = base / "jobs.log"
            attempt = "att-" + base.name
            nonce = os.urandom(32).hex()
            lease = dispatch_contract.supervisor_lease_path(jobs, attempt)
            lease.parent.mkdir(parents=True)
            runner = base / "inner_guard.py"
            runner.write_text(SD61_INNER_GUARD, encoding="utf-8")
            namespace = [
                bwrap, "--die-with-parent", "--unshare-pid",
                "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
                "--tmpfs", "/tmp", "--ro-bind", str(ROOT), str(ROOT),
                "--bind", str(base), str(base),
            ]
            env = {**base_environ(), "TMPDIR": str(base),
                   "XDG_STATE_HOME": str(base / "state"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
            env.pop("PYTHONPATH", None)
            available = subprocess.run([*namespace, "true"], env=env,
                                       text=True, capture_output=True, timeout=10)
            if available.returncode:
                reason = "bubblewrap PID namespace unavailable: " + available.stderr.strip()
                if os.environ.get("HEARTING_REQUIRE_PIDNS") == "1":
                    self.fail(reason)
                self.skipTest(reason)
            payload = (f"kind={dispatch_contract.SUPERVISOR_LEASE_KIND}\n"
                       f"attempt_id={attempt}\nnonce={nonce}\n")
            host = subprocess.Popen(
                [sys.executable, "-c", SD61_HOST_SUPERVISOR, str(lease), payload],
                env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
            )

            def receive() -> dict:
                ready, _, _ = select.select([host.stdout], [], [], 20)
                self.assertTrue(ready, "host supervisor did not answer within 20 seconds")
                line = host.stdout.readline()
                self.assertTrue(line, f"host supervisor exited={host.poll()}")
                return json.loads(line)

            def send(command: dict) -> None:
                host.stdin.write(json.dumps(command) + "\n")
                host.stdin.flush()

            try:
                identity = receive()
                self.assertEqual(identity["pid"], host.pid)
                self.assertEqual(os.getpgid(host.pid), host.pid)
                self.assertEqual(identity["pid_start"], dispatch_contract.process_start_ticks(host.pid))
                self.assertEqual(identity["pid_ns"], dispatch_contract.process_namespace_identity())
                metadata = {
                    "attempt_schema_version": "2", "dispatch_depth": "1",
                    "transport": "headless", "execution_surface": "registered-headless",
                    "registered_worker": "1", "worker_type": "owner",
                    "fallback_hop": "same-harness-headless", "attempt_id": attempt,
                    "capability": "autopilot-code", "intensity": "standard", "harness": "codex",
                    "runtime_sandbox": "workspace-write", "completion_delivery": "app-server-supervised",
                    "pid": str(host.pid), "pid_start": identity["pid_start"], "pgid": str(host.pid),
                    "pid_scope": "host-visible", "pid_ns": identity["pid_ns"],
                    "pid_observer_ns": identity["pid_ns"],
                    "supervisor_lease": dispatch_contract.SUPERVISOR_LEASE_KIND,
                    "supervisor_lease_file": str(lease), "supervisor_lease_nonce": nonce,
                }

                def write_row() -> None:
                    jobs.write_text(
                        f"2026-09-07T00:00:00Z\topen\t{base}\t{base}\towner\t"
                        + ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n",
                        encoding="utf-8",
                    )

                write_row()
                self.assertTrue(dispatch_contract.supervisor_lease_is_held(jobs, metadata))
                for case in ("held", "nonce-mismatch", "released"):
                    with self.subTest(case=case):
                        metadata["supervisor_lease_nonce"] = (
                            "0" * 64 if case == "nonce-mismatch" else nonce
                        )
                        write_row()
                        if case == "released":
                            send({"action": "release"})
                            self.assertEqual(receive(), {"released": True})
                        send({"action": "probe", "attempt": attempt,
                              "argv": [*namespace, sys.executable, str(runner),
                                       str(contract_root), str(jobs)]})
                        response = receive()
                        self.assertEqual(response["code"], 0, response)
                        result = json.loads(response["stdout"])
                        # The real outer parent is still alive after its inner
                        # guard exited, even when /proc inside cannot see it.
                        self.assertIsNone(host.poll())
                        self.assertTrue(dispatch_contract.process_identity_is_live(
                            host.pid, identity["pid_start"]))
                        result.update(case=case, outer_parent_alive=True, inner_exit=response["code"])
                        print("SD61_PIDNS_OBSERVATION:" + json.dumps(result, sort_keys=True), flush=True)
                        self.assertEqual(result["raw_lease_locked"], case != "released", result)
                        self.assertEqual(result["exact_lease_held"], case == "held", result)
                        self.assertEqual(result["resolved"], case == "held", result)
                        if case == "held":
                            self.assertEqual(result["source"], "supervisor-lease", result)
                            self.assertIsNone(result["observed_pid"], result)
                            self.assertTrue(result["revalidated"], result)
                        else:
                            self.assertEqual(result["reason"], "parent-attempt-not-live", result)
                send({"action": "exit"})
                stdout, stderr = host.communicate(timeout=5)
                self.assertEqual(host.returncode, 0, stdout + stderr)
            finally:
                if host.poll() is None:
                    host.terminate()
                host.communicate(timeout=5)

    def test_codex_pass_receipt_marker_and_join_are_ordered(self) -> None:
        self._run_harness("codex")

    def test_claude_pass_receipt_marker_and_join_are_ordered(self) -> None:
        self._run_harness("claude")


# No harness/model is launched: the child only captures its real kernel
# identity and writes a PASS envelope. Exercise the normal frame join, rather
# than calling a settlement helper that bypasses its process checks.
BOOT_ONLY_FRAME_SCRIPT = r'''
import json, os, subprocess, sys, tempfile
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'utilities'))
import dispatch_contract as D
import dispatch_completion_join as J
import resource_run_registry as R

base = Path(tempfile.mkdtemp(prefix='frame-boot-'))
repo, art, home = (base / name for name in ('repo', 'artifacts', 'home'))
for path in (repo, art, home):
    path.mkdir()
jobs = base / 'jobs.log'
os.environ.update(
    HOME=str(home), XDG_CONFIG_HOME=str(base / 'config'),
    XDG_STATE_HOME=str(base / 'state'), XDG_DATA_HOME=str(base / 'data'),
    AGENT_HOME=str(root), AGENT_ARTIFACT_ROOT=str(art),
    AGENT_DISPATCH_JOBS=str(jobs), HEARTING_CAPACITY_PROBE_CLI=os.devnull)
for args in (['init', '-q'], ['config', 'user.email', 'fixture@example.invalid'],
             ['config', 'user.name', 'Fixture']):
    subprocess.run(['git', '-C', str(repo), *args], check=True)
(repo / 'file.txt').write_text('fixture\n')
subprocess.run(['git', '-C', str(repo), 'add', 'file.txt'], check=True)
subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'fixture'], check=True)
evidence = base / 'dispatch.json'
evidence.write_text(json.dumps({'tuples': [{
    'parent_harness': 'claude', 'parent_transport': 'headless',
    'parent_sandbox': 'adapter-default', 'child_harness': 'claude',
    'launch_authority': 'conductor', 'status': 'supported',
    'probe_source': 'fixture', 'probe_time': '2026-11-05T00:00:00Z',
    'failure_class': '', 'checked_worktree': str(repo), 'failure_scope': 'none',
    'codex_command': 'ok', 'retry_on_isolated_worktree': 0}], 'native_subagent': []}))
compiled = subprocess.run([
    sys.executable, str(root / 'utilities/capability-route.py'), 'compile',
    '--capability', 'autopilot-code', '--capability-mode', 'dev',
    '--slug', 'frame-boot-fixture', '--intensity', 'standard', '--cwd', str(repo),
    '--artifact-root', str(art), '--signal', 'shared-contract',
    '--transport', 'headless', '--tracking', 'tracked', '--spec-read', 'fixture',
    '--drift-verdict', 'within-spec', '--workflow-mode', 'tracked',
    '--artifact-guard', 'fixture', '--dispatch-evidence', str(evidence)],
    text=True, capture_output=True, timeout=20)
assert compiled.returncode == 0, compiled.stdout + compiled.stderr
route = json.loads(compiled.stdout)
route_path = art / '.runtime/routes' / (route['route_id'] + '.json')
brief = art / 'frame.md'
brief.write_text('---\nstatus: framed\n---\n# Direction\nUse the existing module.\n')
original_brief = brief.read_bytes()
identity_file, log = base / 'identity.json', base / 'frame.claude.jsonl'
attempt = 'att-boot-frame'
child_code = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import governor_identity as G
Path(sys.argv[2]).write_text(json.dumps(G.capture_local_identity()))
text = 'artifact: ' + sys.argv[3] + '\\nverdict: PASS\\nblocker: none'
print(json.dumps({'type': 'result', 'subtype': 'success',
                  'is_error': False, 'result': text}), flush=True)
"""
with log.open('w') as out:
    child = subprocess.Popen([
        sys.executable, '-c', child_code, str(root / 'utilities'),
        str(identity_file), str(brief)], stdout=out, start_new_session=True,
        env={**os.environ, 'AGENT_DISPATCH_ATTEMPT_ID': attempt})
    assert child.wait(timeout=10) == 0
identity = json.loads(identity_file.read_text())
assert identity['boot_id'] and 'boot_host' not in identity, identity
assert R.boot_identity() == {}, 'Full host identity must remain unavailable'
state = art / '.runtime/model-worker-governor/state.json'
state.parent.mkdir(parents=True, exist_ok=True)
state.write_text(json.dumps({'claims': {'fixture': {
    'claimant_pid': identity['pid'], 'claimant_starttime': identity['starttime'],
    'claimant_identity': identity}}}))
namespace = 'pid:[%s]' % identity['pid_namespace']
meta = {
    'attempt_schema_version': '2', 'dispatch_depth': '1', 'transport': 'headless',
    'execution_surface': 'registered-headless', 'registered_worker': '1',
    'fallback_hop': 'same-harness-headless', 'worker_type': 'frame',
    'harness': 'claude', 'attempt_id': attempt, 'route_file': str(route_path),
    'route_id': route['route_id'], 'route_hash': route['route_hash'],
    'route_node': 'frame', 'artifact_root': str(art), 'log_file': str(log),
    'pid': str(identity['pid']), 'pid_start': identity['starttime'],
    'pgid': str(identity['pid']), 'pid_ns': namespace, 'pid_observer_ns': namespace,
    'launch_lifecycle': 'foreground-scoped', 'launch_outcome': 'governed-process-reaped',
    'group_reap_proof': D.GROUP_REAP_PROOF, 'group_reap_pgid': str(identity['pid']),
    'launch_claimed': '1', 'launch_started': '1'}
jobs.write_text('2026-11-05T00:00:00Z\topen\t%s\t%s\tframe\t%s\n' % (
    repo, repo, ','.join(k + '=' + v for k, v in meta.items())))
receipt = J.join_selected_attempts(jobs=jobs, expected_attempts={attempt},
                                 interval=.01, timeout=1, recover=True)
assert receipt['state'] == 'ready', receipt
row = J.exact_attempt_row(jobs, attempt)
assert row.status == 'done', row
marker = Path(row.metadata['completion_marker'])
assert json.loads(marker.read_text())['attempt_id'] == attempt
assert brief.read_bytes() == original_brief
assert len(jobs.read_text().splitlines()) == 1, 'Recovery must not relaunch'
print(json.dumps({'state': receipt['state'], 'attempt_id': attempt}))
'''


class BootOnlyFrameRecoveryTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('bwrap'), 'bubblewrap is required')
    def test_pass_frame_is_recovered_normally_without_machine_id(self):
        # A fresh /etc deliberately has no machine-id. The kernel boot UUID,
        # real PID namespace, process identity and normal receipt checks remain.
        with tempfile.TemporaryDirectory() as td:
            world = Path(td)
            script = world / 'probe.py'
            script.write_text(BOOT_ONLY_FRAME_SCRIPT)
            args = [
                'bwrap', '--die-with-parent', '--unshare-user', '--unshare-pid',
                '--unshare-uts', '--ro-bind', '/', '/', '--proc', '/proc',
                '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/etc',
                '--ro-bind', str(ROOT), str(ROOT), '--bind', str(world), str(world),
                sys.executable, '-u', str(script), str(ROOT)]
            with (world / 'result.log').open('w+') as out:
                result = subprocess.run(args, env={**base_environ(), 'TMPDIR': str(world),
                                        'PYTHONDONTWRITEBYTECODE': '1'},
                                        stdout=out, stderr=subprocess.STDOUT, timeout=40)
                out.seek(0)
                output = out.read()
            self.assertEqual(result.returncode, 0, output)
            self.assertEqual(json.loads(output.splitlines()[-1])['state'], 'ready')


if __name__ == "__main__":
    unittest.main()
