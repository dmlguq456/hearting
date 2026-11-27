#!/usr/bin/env python3
"""A route sealed on one managed release finishes after the installer moves `current`.

Two managed releases of one fixture install (`<XDG_DATA_HOME>/hearting/releases/{A,B}`
and the installer's `current` pointer) stand in for an update that lands while a
staged route runs. The route is sealed through A. Release B differs in its unit
catalog, as a release that adds a unit does, so B's registry check classifies the
sealed route as compiled elsewhere. The installer then points `current` at B,
either before the route's next registered worker starts (that worker launches on
B) or while it runs (it stays on A). Its fake Claude CLI ends with a PASS handoff,
and the worker's own reap watcher must accept that PASS exactly as it does when
nothing moved. B's code must also keep reading the sealed route as the same route:
verifying it must not report the route's own hash as broken.

No real model, account, tmux server, user session or installed release is used:
HOME, XDG_DATA_HOME, XDG_STATE_HOME, the registry and both releases live under the
test's private temporary directory.
"""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
from fixture_processes import unique_attempt_id  # noqa: E402
import dispatch_completion_join as JOIN  # noqa: E402
import frame_interview  # noqa: E402

PARENT = unique_attempt_id("att-flip-owner")
VERSIONS = ("v0.0.1-flip-a", "v0.0.1-flip-b")
TARGET = "plan-check"  # a registered review worker: the route's first verification node

# Every top-level tree a release carries. Test files are left out: they are not
# release code here, and a smaller tree keeps the release identity digest cheap.
RELEASE_TREES = ("adapters", "capabilities", "core", "docs", "hooks", "profiles", "roles",
                 "tools", "utilities")
RELEASE_FILES = ("harness-manifest.json",)
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.test.py", "*.test.sh", "test_*.py")

FAKE_CLAUDE = (
    "#!/usr/bin/env python3\n"
    "import json, os, time\n"
    "sentinel = os.environ.get('FAKE_SENTINEL')\n"
    "if sentinel:\n"
    "    open(sentinel, 'w').write(os.environ['AGENT_DISPATCH_ATTEMPT_ID'])\n"
    "    while os.path.exists(sentinel): time.sleep(0.01)\n"
    "text = 'artifact: %s\\nverdict: PASS\\nblocker: none' % os.environ['FAKE_EVIDENCE']\n"
    "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
    "'result':text}), flush=True)\n"
)

# Load one release's wrapper module and run its main() with the two seams
# `dispatch_namespace_e2e.test.py` replaces: no title/NOW summary owner and the
# detached lifecycle, so no host service manager is asked for a scope.
RUNNER = """
import contextlib, importlib.util, io, json, sys
from pathlib import Path
from unittest import mock

wrapper_path = Path(sys.argv[1])
argv = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
spec = importlib.util.spec_from_file_location("flip_wrapper", wrapper_path)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)
out, err = io.StringIO(), io.StringIO()
with contextlib.ExitStack() as stack:
    stack.enter_context(mock.patch.object(wrapper, "launch_summary_owner",
                                          return_value={"summary_owner": "flip-fixture"}))
    if hasattr(wrapper, "reconcile_launch_lifecycle") and hasattr(wrapper, "DETACHED"):
        resolution = wrapper.reconcile_launch_lifecycle(
            wrapper.DETACHED, {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
            evidence={"lifecycle_selector_source": "host-like"})
        stack.enter_context(mock.patch.object(wrapper, "reconcile_launch_lifecycle",
                                              return_value=resolution))
    stack.enter_context(contextlib.redirect_stdout(out))
    stack.enter_context(contextlib.redirect_stderr(err))
    code = wrapper.main(argv)
print("FLIP_RESULT:" + json.dumps({"code": code, "stdout": out.getvalue(), "stderr": err.getvalue()}))
"""

# The read `capability-route.py close` makes on the installed release: verify the
# sealed record (a stale catalog is tolerated) and walk its lineage.
LINEAGE_CHECK = """
import importlib.util, json, sys
from pathlib import Path
root, route_path = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "utilities"))
spec = importlib.util.spec_from_file_location("flip_route", root / "utilities" / "capability-route.py")
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)
import route_identity, route_lineage
raw = json.loads(route_path.read_text(encoding="utf-8"))
verified = route.verify_route(raw, raw["cwd"], allow_stale_registry=True)
lineage = route_lineage.verified_route_lineage(verified)
print(json.dumps({"code_root": str(Path(route.__file__).resolve().parents[1]),
                  "registry_current": verified.get("_registry_current", True),
                  "sealed": raw["route_hash"], "verified": route_identity.route_hash(verified),
                  "lineage": [item["route_id"] for item in lineage]}))
"""


def scrubbed_environ() -> dict:
    """The caller's environment without any dispatch, route or artifact identity."""
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("AGENT_", "HEARTING_", "REPORT_BUNDLE_", "CLAUDE_"))}


class ManagedInstall:
    """`<data>/hearting/releases/<version>` trees and the installer's `current` pointer."""

    def __init__(self, base: Path):
        self.data = base / "xdg-data" / "hearting"
        self.releases = self.data / "releases"
        self.releases.mkdir(parents=True)
        self.current = self.data / "current"
        self.roots = []
        for version in VERSIONS:
            root = self.releases / version
            like = self.roots[0] if self.roots else None
            for tree in RELEASE_TREES:
                if (ROOT / tree).is_dir():
                    shutil.copytree((like or ROOT) / tree, root / tree, ignore=_IGNORE, symlinks=True,
                                    copy_function=os.link if like is not None else shutil.copy2)
            for name in RELEASE_FILES:
                if (ROOT / name).is_file():
                    shutil.copy2(ROOT / name, root / name)
            (root / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
            # The marker `distribution._publish_release` writes beside a published tree.
            (root / ".hearting-release.json").write_text(json.dumps({
                "schema": 1, "version": version,
                "archive_sha256": hashlib.sha256(version.encode()).hexdigest(),
                "published_at": "2026-11-27T00:00:00Z"}), encoding="utf-8")
            self.roots.append(root)
        self.a, self.b = self.roots
        # B's unit catalog differs (one frontmatter comment), like a release that
        # added a unit: its catalog digest no longer equals the one A sealed.
        unit = sorted(path for path in (self.b / "roles" / "units").glob("*/*.md")
                      if not path.name.startswith("_") and not path.parent.name.startswith("_")
                      and path.read_text(encoding="utf-8").startswith("---\n"))[0]
        text = unit.read_text(encoding="utf-8")
        unit.unlink()  # break the hard link to A's copy before changing B's
        unit.write_text(text.replace("---\n", "---\n# fixture release B\n", 1), encoding="utf-8")
        self.point(self.a)

    def point(self, target: Path) -> None:
        """Swap the pointer atomically, as the installer's commit does."""
        staged = self.data / ".current.next"
        staged.unlink(missing_ok=True)
        staged.symlink_to(target, target_is_directory=True)
        os.replace(staged, self.current)


class RouteReleaseFlipTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._install_temp = tempfile.TemporaryDirectory(prefix="flip-install-")
        cls.install = ManagedInstall(Path(cls._install_temp.name).resolve())

    @classmethod
    def tearDownClass(cls):
        cls._install_temp.cleanup()

    def setUp(self):
        self.install.point(self.install.a)

    def run_flip(self, *, flip) -> dict:
        """`flip`: None, "before" (the worker launches on B) or "during" (it runs on A)."""
        install = self.install
        temp = tempfile.TemporaryDirectory(prefix="flip-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        home = base / "home"
        home.mkdir()
        repo = base / "repo"
        repo.mkdir()
        git_env = {**scrubbed_environ(), "HOME": str(home)}
        for command in (["init", "-q"], ["config", "user.email", "fixture@example.invalid"],
                        ["config", "user.name", "Fixture"]):
            subprocess.run(["git", "-C", str(repo), *command], check=True, env=git_env)
        (repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True, env=git_env)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True, env=git_env)

        artifact_root = base / ".agent_reports"
        artifact_root.mkdir()
        jobs = base / "jobs.log"
        evidence = artifact_root / "pass.md"
        evidence.write_text("flip fixture PASS\n", encoding="utf-8")
        common = {
            **scrubbed_environ(),
            "HOME": str(home),
            "XDG_DATA_HOME": str(install.data.parent),
            "XDG_STATE_HOME": str(base / "xdg-state"),
            "AGENT_HOME": str(install.current),
            "AGENT_ARTIFACT_ROOT": str(artifact_root),
            "AGENT_DISPATCH_JOBS": str(jobs),
            "AGENT_MODEL_GOVERNOR_ROOT": str(artifact_root / ".runtime" / "model-worker-governor"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        common.pop("PYTHONPATH", None)
        dispatch_path = base / "dispatch-evidence.json"
        dispatch_path.write_text(json.dumps({"tuples": [{
            "parent_harness": "claude", "parent_transport": "headless",
            "parent_sandbox": "adapter-default", "child_harness": "claude",
            "launch_authority": "conductor", "status": "supported",
            "probe_source": "claude-flip-fixture", "probe_time": "2026-11-27T00:00:00Z",
            "failure_class": "", "checked_worktree": str(repo), "failure_scope": "none",
            "codex_command": "ok", "retry_on_isolated_worktree": 0,
        }], "native_subagent": []}), encoding="utf-8")

        def route_tool(*args):
            # Printed runtime commands name the installer's pointer.
            return subprocess.run(
                [sys.executable, str(install.current / "utilities" / args[0]), *args[1:]],
                env=common, text=True, capture_output=True)

        compiled = route_tool(
            "capability-route.py", "compile", "--capability", "autopilot-code",
            "--capability-mode", "dev", "--slug", "release-flip-fixture", "--intensity", "standard",
            "--cwd", str(repo), "--artifact-root", str(artifact_root),
            "--signal", "shared-contract", "--transport", "headless", "--tracking", "tracked",
            "--spec-read", "fixture", "--drift-verdict", "within-spec", "--workflow-mode", "tracked",
            "--artifact-guard", "fixture", "--dispatch-evidence", str(dispatch_path))
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        route = json.loads(compiled.stdout)
        route_path = artifact_root / ".runtime" / "routes" / f"{route['route_id']}.json"
        sealed_bytes = route_path.read_bytes()
        nodes = {item["id"]: item for item in route["nodes"]}
        node = nodes[TARGET]
        frame = nodes["plan"]["depends_on"]

        def complete_fixture(name):
            fixture_evidence = artifact_root / f"{name}.md"
            fixture_evidence.write_text("fixture predecessor\n", encoding="utf-8")
            fixture_attempt = unique_attempt_id(f"att-flip-{name}")
            worker_type = nodes[name].get("worker_type") or (
                "review" if nodes[name].get("kind") == "review-worker" else "stage")
            with jobs.open("a", encoding="utf-8") as registry:
                registry.write(
                    f"2026-11-27T00:00:00Z\topen\t{repo}\t{repo}\t{name}\t"
                    "attempt_schema_version=2,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    "fallback_hop=same-harness-headless,launch_outcome=never-launched,"
                    f"dispatch_depth={nodes[name]['dispatch_depth']},"
                    f"worker_type={worker_type},harness=claude,"
                    f"attempt_id={fixture_attempt},route_file={route_path},"
                    f"route_id={route['route_id']},route_hash={route['route_hash']},"
                    f"route_node={name},artifact_root={artifact_root}\n")
            completed = route_tool("capability-route.py", "complete", "--route", str(route_path),
                                   "--node", name, "--evidence", str(fixture_evidence),
                                   "--attempt-id", fixture_attempt, "--jobs", str(jobs))
            self.assertEqual(completed.returncode, 0, name + ": " + completed.stdout + completed.stderr)

        for name in frame:
            complete_fixture(name)
        parent = subprocess.Popen(["sleep", "300"], start_new_session=True)

        def stop_parent():
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=10)
        self.addCleanup(stop_parent)
        parent_start = (Path("/proc") / str(parent.pid) / "stat").read_text().split()[21]
        with jobs.open("a", encoding="utf-8") as registry:
            registry.write(
                f"2026-11-27T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,worker_type=owner,harness=claude,"
                f"runtime_sandbox=adapter-default,attempt_id={PARENT},"
                f"parent_sid=flip-fixture,owner_route_id={route['route_id']},"
                "parent_completion_delivery=claude-parent-runtime,"
                f"pid={parent.pid},pid_start={parent_start}\n")
        summary_path = artifact_root / "frame-summary.json"
        summary_path.write_text(json.dumps({"route_id": route["route_id"], "artifacts": [
            str(artifact_root / f"{name}.md") for name in frame]}), encoding="utf-8")
        interview = {
            "schema": frame_interview.SCHEMA, "route_id": route["route_id"],
            "summary": str(summary_path),
            "understanding": "Check completion across a release move in an isolated process.",
            "brief": {"problem": "Completion after an install.", "outcome": "Record the result.",
                      "affected": "The test only.", "constraints": "Use fake executables.",
                      "open": ""},
            "questions": [],
        }
        gate_artifact = artifact_root / "frame-review.json"
        gate_artifact.write_text(json.dumps(interview), encoding="utf-8")
        answers = frame_interview.answers_template(interview)
        answers["actor_kind"] = "user"
        answers["understanding_confirmed"] = True
        answers_path = artifact_root / "frame-answers.json"
        answers_path.write_text(json.dumps(answers), encoding="utf-8")
        for action in (["gate", "--block", "--artifact", str(gate_artifact)],
                       ["release", "--decision", "proceed", "--answers", str(answers_path)]):
            gate = route_tool("workflow-supervisor.py", *action, "--route", str(route_path),
                              "--gate", "frame-review", "--jobs", str(jobs))
            self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        for name in node["depends_on"]:
            complete_fixture(name)

        if flip == "before":
            install.point(install.b)
        fakebin = base / "bin"
        fakebin.mkdir()
        (fakebin / "claude").write_text(FAKE_CLAUDE, encoding="utf-8")
        (fakebin / "claude").chmod(0o755)
        attempt = unique_attempt_id(f"att-flip-{TARGET}")
        argv = [
            "dispatch-headless.py", "--start", "--worktree", str(repo),
            "--slug", f"flip-{TARGET}", "--capability", "autopilot-code",
            "--capability-mode", route["capability_mode"], "--worker-mode", node["unit"],
            "--intensity", route["effective_intensity"], "--dispatch-depth", "2",
            "--parent", "owner", "--worker-role", node["role"], "--owner", "autopilot-code",
            "--jobs", str(jobs), "--log-dir", str(base / "logs"), "--attempt-id", attempt,
            "--parent-harness", "claude", "--parent-transport", "headless",
            "--parent-sandbox", "adapter-default", "--launch-authority", "conductor",
            "--nested-eligibility", "supported", "--eligibility-source", "claude-flip-fixture",
            "--fallback-ordinal", "1", "--route-file", str(route_path),
            "--route-id", route["route_id"], "--route-hash", route["route_hash"],
            "--route-node", node["id"], "--registry-digest", route["registry_digest"],
            "--write-scope", ";".join(node["write_scope"]), "--unit", node["unit"],
            "--model-role", node["role"], "--model-profile", node["model_profile"],
            "--foreground-timeout", "120", "--prompt-text", "fixture",
        ]
        sentinel = base / "worker.sentinel"
        env = {**common, "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
               "AGENT_DISPATCH_CHILD": "1", "AGENT_DISPATCH_ATTEMPT_ID": PARENT,
               "AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1", "FAKE_EVIDENCE": str(evidence)}
        if flip == "during":
            env["FAKE_SENTINEL"] = str(sentinel)
        (base / "runner.py").write_text(RUNNER, encoding="utf-8")
        (base / "argv.json").write_text(json.dumps(argv), encoding="utf-8")
        # The owner's printed start names the pointer, so the worker launches on
        # whichever release the pointer names at that moment.
        process = subprocess.Popen(
            [sys.executable, str(base / "runner.py"),
             str(install.current / "adapters" / "claude" / "bin" / "dispatch-headless.py"),
             str(base / "argv.json")],
            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)

        def stop_launch():
            sentinel.unlink(missing_ok=True)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        self.addCleanup(stop_launch)
        # One deadline covers the start, the wrapper and the completion together;
        # a late run names what was pending, the wrapper's own exit and the row.
        budget = 240
        deadline = time.monotonic() + budget
        stdout = stderr = ""

        def late(pending):
            try:
                last = JOIN.exact_attempt_row(jobs, attempt).raw
            except JOIN.JoinContractError as exc:
                last = f"not registered ({exc})"
            return (f"{pending} at the {budget}s deadline; wrapper returncode={process.poll()!r}; "
                    f"last completion row: {last}\nwrapper output: {stdout}{stderr}")

        if flip == "during":
            while not sentinel.is_file():
                self.assertIsNone(process.poll(), "the worker ended before it started")
                if time.monotonic() > deadline:
                    self.fail(late("the worker had not started"))
                time.sleep(0.02)
            install.point(install.b)
            sentinel.unlink()
        try:
            stdout, stderr = process.communicate(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = (value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                              for value in (exc.stdout, exc.stderr))
            self.fail(late("the wrapper had not returned"))
        self.assertEqual(process.returncode, 0, stdout + stderr)
        while True:
            try:
                row = JOIN.exact_attempt_row(jobs, attempt)
            except JOIN.JoinContractError as exc:
                self.fail(f"the worker was never registered ({exc}): {stdout}")
            if row.status != "open":
                break
            if time.monotonic() > deadline:
                self.fail(late("the PASS attempt was still open"))
            time.sleep(0.05)
        self.assertEqual(row.status, "done", f"the PASS attempt was never closed: {row.raw}\n{stdout}")
        # The close a person or parent runs next reads the route on the installed release.
        (base / "lineage.py").write_text(LINEAGE_CHECK, encoding="utf-8")
        checked = subprocess.run(
            [sys.executable, str(base / "lineage.py"), str(install.current), str(route_path)],
            env=common, text=True, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        return {"row": row, "route": route, "route_path": route_path, "sealed_bytes": sealed_bytes,
                "lineage": json.loads(checked.stdout.strip().splitlines()[-1]),
                "lineage_stderr": checked.stderr}

    def assert_pass_accepted(self, result: dict, *, launch_home: Path) -> None:
        row = result["row"]
        self.assertEqual(row.metadata.get("launch_home"), str(launch_home), row.raw)
        self.assertEqual(row.metadata.get("note"), "completed-marker", row.raw)
        self.assertEqual(row.metadata.get("failure_class"), "pass", row.raw)
        self.assertTrue(Path(row.metadata["completion_marker"]).is_file(), row.raw)
        self.assertEqual(result["route_path"].read_bytes(), result["sealed_bytes"],
                         "the sealed route record never changes")

    def assert_lineage_verified(self, result: dict, *, code_root: Path, registry_current: bool) -> None:
        lineage = result["lineage"]
        self.assertEqual(lineage["code_root"], str(code_root))
        self.assertIs(lineage["registry_current"], registry_current)
        self.assertEqual(lineage["verified"], lineage["sealed"], lineage)
        self.assertEqual(lineage["lineage"], [result["route"]["route_id"]])
        self.assertNotIn("route-lineage-unverified", result["lineage_stderr"])
        self.assertNotIn("route-hash-mismatch", result["lineage_stderr"])

    def test_pass_is_accepted_when_nothing_moves(self):
        result = self.run_flip(flip=None)
        self.assert_pass_accepted(result, launch_home=self.install.a)
        self.assert_lineage_verified(result, code_root=self.install.a, registry_current=True)

    def test_pass_is_accepted_when_current_moves_before_the_worker_starts(self):
        result = self.run_flip(flip="before")
        self.assert_pass_accepted(result, launch_home=self.install.b)
        self.assert_lineage_verified(result, code_root=self.install.b, registry_current=False)

    def test_pass_is_accepted_when_current_moves_while_the_worker_runs(self):
        result = self.run_flip(flip="during")
        self.assert_pass_accepted(result, launch_home=self.install.a)
        self.assert_lineage_verified(result, code_root=self.install.b, registry_current=False)


if __name__ == "__main__":
    unittest.main()
