#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
"""Raw experiment data gets one place outside the cycle: `raw_data_dir`.

The value is `<raw_data_root>/<project>` when the user's compute-hosts.yaml
names a visible, writable root, else exactly the runtime dir. It reaches the
worker by the runtime_dir path: environment, the prompt's runtime line, and
the three harnesses' write grants (only that project's folder).
"""
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
import worker_bootstrap as W  # noqa: E402
import fixture_processes  # noqa: E402

ADAPTERS = ("claude", "codex", "opencode")


def _write_hosts(home: Path, line: str | None) -> Path:
    path = home / ".config" / "hearting" / "compute-hosts.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "schema_version: 1\n# raw_data_root: /commented/out\nhosts:\n"
    if line is not None:
        body = line + "\n" + body
    path.write_text(body, encoding="utf-8")
    return path


def _project(root: Path, name: str = "proj") -> Path:
    artifacts = root / name / ".agent_reports"
    artifacts.mkdir(parents=True)
    return artifacts


class RawDataDirCandidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="raw-dir-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.data = base / "data" / "_agent_raw"
        self.data.mkdir(parents=True)
        self.artifacts = _project(base / "work")
        self.env = {"HOME": str(self.home)}
        self.runtime = W.runtime_dir_candidate(self.artifacts, self.env)

    def candidate(self, artifacts=None, env=None):
        artifacts = self.artifacts if artifacts is None else artifacts
        env = self.env if env is None else env
        runtime = W.runtime_dir_candidate(artifacts, env)
        return W.raw_data_dir_candidate(artifacts, runtime, env)

    def test_configured_root_gives_the_project_folder(self):
        _write_hosts(self.home, f"raw_data_root: {self.data}")
        self.assertEqual(self.candidate(), str(self.data / "proj"))
        # A trailing comment and quotes on the one line read the same.
        _write_hosts(self.home, f"raw_data_root: '{self.data}'   # data folder")
        self.assertEqual(self.candidate(), str(self.data / "proj"))

    def test_override_path_of_the_same_file_is_honored(self):
        other = Path(self.tmp.name) / "hosts.yaml"
        other.write_text(f"schema_version: 1\nraw_data_root: {self.data}\nhosts:\n", encoding="utf-8")
        env = {**self.env, "COMPUTE_HOSTS_CONFIG": str(other)}
        self.assertEqual(self.candidate(env=env), str(self.data / "proj"))

    def test_absent_value_falls_back_to_the_exact_runtime_dir(self):
        self.assertEqual(self.candidate(), self.runtime)          # no file at all
        _write_hosts(self.home, None)                              # only a commented line
        self.assertEqual(self.candidate(), self.runtime)
        _write_hosts(self.home, "raw_data_root:")                  # empty value
        self.assertEqual(self.candidate(), self.runtime)
        _write_hosts(self.home, "raw_data_root: relative/path")    # not absolute
        self.assertEqual(self.candidate(), self.runtime)
        # The checkpoint path is then the same, character for character.
        slug_path = "experiments/2026-11-16_x/runs/run-001/ckpt/"
        self.assertEqual(f"{self.candidate()}/{slug_path}", f"{self.runtime}/{slug_path}")

    def test_invisible_root_falls_back(self):
        _write_hosts(self.home, f"raw_data_root: {self.data.parent / 'missing'}")
        self.assertEqual(self.candidate(), self.runtime)
        not_dir = self.data.parent / "file"
        not_dir.write_text("x")
        _write_hosts(self.home, f"raw_data_root: {not_dir}")
        self.assertEqual(self.candidate(), self.runtime)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores mode bits")
    def test_unwritable_root_falls_back(self):
        _write_hosts(self.home, f"raw_data_root: {self.data}")
        self.data.chmod(0o555)
        self.addCleanup(self.data.chmod, 0o755)
        self.assertEqual(self.candidate(), self.runtime)
        # An existing but unwritable project folder falls back too.
        self.data.chmod(0o755)
        (self.data / "proj").mkdir()
        (self.data / "proj").chmod(0o555)
        self.addCleanup((self.data / "proj").chmod, 0o755)
        self.assertEqual(self.candidate(), self.runtime)

    def test_linked_project_names_share_one_folder(self):
        _write_hosts(self.home, f"raw_data_root: {self.data}")
        base = Path(self.tmp.name).resolve() / "projects"
        real = _project(base, "BC_Real")
        (base / "BC_Alias").symlink_to(base / "BC_Real", target_is_directory=True)
        alias = base / "BC_Alias" / ".agent_reports"
        self.assertEqual(self.candidate(artifacts=real), str(self.data / "BC_Real"))
        self.assertEqual(self.candidate(artifacts=alias), str(self.data / "BC_Real"))
        # A linked artifact root name (`.agent_reports` -> `.claude_reports`) too.
        legacy = base / "Legacy"
        (legacy / ".claude_reports").mkdir(parents=True)
        (legacy / ".agent_reports").symlink_to(legacy / ".claude_reports", target_is_directory=True)
        self.assertEqual(self.candidate(artifacts=legacy / ".agent_reports"), str(self.data / "Legacy"))

    def test_folder_that_would_cover_the_project_or_home_falls_back(self):
        work = self.artifacts.parent.parent
        _write_hosts(self.home, f"raw_data_root: {work}")          # <root>/proj is the project itself
        self.assertEqual(self.candidate(), self.runtime)
        _write_hosts(self.home, "raw_data_root: /")
        self.assertEqual(self.candidate(), self.runtime)
        inside_home = self.home / ".local" / "state"                # e.g. the harness state root
        inside_home.mkdir(parents=True)
        _write_hosts(self.home, f"raw_data_root: {inside_home}")
        self.assertEqual(self.candidate(), self.runtime)
        linked = self.data / "proj"
        linked.symlink_to(self.home, target_is_directory=True)     # a link out of the data folder
        _write_hosts(self.home, f"raw_data_root: {self.data}")
        self.assertEqual(self.candidate(), self.runtime)

    def test_prepare_creates_only_the_project_folder(self):
        _write_hosts(self.home, f"raw_data_root: {self.data}")
        candidate = self.candidate()
        self.assertEqual(W.prepare_raw_data_dir(candidate, self.runtime), candidate)
        self.assertTrue(Path(candidate).is_dir())
        self.assertEqual(sorted(os.listdir(self.data)), ["proj"])
        # The runtime fallback is returned untouched; a vanished root falls back.
        self.assertEqual(W.prepare_raw_data_dir(self.runtime, self.runtime), self.runtime)
        self.assertEqual(W.prepare_raw_data_dir(str(self.data.parent / "gone" / "proj"), self.runtime),
                         self.runtime)

    def test_prompt_changes_the_runtime_line_without_adding_one(self):
        base = {"AGENT_ARTIFACT_CYCLE_ID": "cyc_x", "AGENT_ARTIFACT_OUTPUT_DIR": "/a/artifacts",
                "AGENT_RUNTIME_DIR": "/p/.agent_runtime"}
        raw = "/d/_agent_raw/p"
        prompt = W.artifact_context_prompt({**base, "AGENT_RAW_DATA_DIR": raw})
        self.assertEqual(len(prompt.splitlines()), 5, prompt)
        self.assertIn("- runtime_dir: /p/.agent_runtime\n", prompt)
        rule = next(line for line in prompt.splitlines() if "raw_data_dir" in line)
        self.assertIn(raw, rule)
        for word in ("checkpoints", "raw results", "artifacts"):
            self.assertIn(word, rule)

    def test_no_raw_value_leaves_the_prompt_byte_equal_to_before(self):
        # The line main (59c720022) printed; a user who set nothing sees no change.
        before = ("- artifact_cycle_id: cyc_x\n- artifact_output_dir: /a/artifacts\n"
                  "- runtime_dir: /p/.agent_runtime\n"
                  "- Put environments, caches, external checkouts, and checkpoints under runtime_dir; "
                  "keep reports and provenance in artifacts.\n"
                  "- Resolve relative artifact paths beneath artifact_output_dir.\n")
        base = {"AGENT_ARTIFACT_CYCLE_ID": "cyc_x", "AGENT_ARTIFACT_OUTPUT_DIR": "/a/artifacts",
                "AGENT_RUNTIME_DIR": "/p/.agent_runtime"}
        for extra in ({}, {"AGENT_RAW_DATA_DIR": ""}, {"AGENT_RAW_DATA_DIR": "/p/.agent_runtime"}):
            self.assertEqual(W.artifact_context_prompt({**base, **extra}).encode(), before.encode(), extra)


class ComputeHostRawDataDirTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("raw_dir_compute_hosts", ROOT / "utilities" / "compute-hosts.py")
        self.hosts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hosts)

    def test_namespaced_run_sees_a_covered_value_at_the_launcher_path(self):
        host = {"ssh_host": "gpu-a", "path_namespace": {"/home/nas": "/mnt/nas"}}
        value = "/home/nas/user/DB/_agent_raw/proj"
        self.assertEqual(self.hosts.run_raw_data_dir(host, value), value)
        self.assertEqual(self.hosts.run_raw_data_dir(host, "/srv/local/_agent_raw/proj"), "")
        self.assertEqual(self.hosts.run_raw_data_dir({"ssh_host": "gpu-b"}, value), value)
        self.assertEqual(self.hosts.run_raw_data_dir(host, ""), "")
        self.assertEqual(self.hosts.run_raw_data_dir(host, "relative"), "")

    def test_run_setup_never_hands_an_empty_value(self):
        host = {"ssh_host": "gpu-a", "path_namespace": {"/home/nas": "/mnt/nas"}}
        runtime = "/home/nas/p/.agent_runtime"
        line = self.hosts.raw_data_dir_setup(host, {"AGENT_RAW_DATA_DIR": "/home/nas/d/proj",
                                                    "AGENT_RUNTIME_DIR": runtime})
        self.assertEqual(line, "export AGENT_RAW_DATA_DIR=/home/nas/d/proj")
        # Outside the namespace, unset, or just the runtime dir: the key is unset, not ''.
        for env in ({"AGENT_RAW_DATA_DIR": "/elsewhere/proj", "AGENT_RUNTIME_DIR": runtime},
                    {}, {"AGENT_RAW_DATA_DIR": runtime, "AGENT_RUNTIME_DIR": runtime}):
            line = self.hosts.raw_data_dir_setup(host, env)
            self.assertEqual(line, "unset AGENT_RAW_DATA_DIR", env)
            self.assertNotIn("''", line)


class AdapterRawDataGrantTest(unittest.TestCase):
    """Each harness launch gets AGENT_RAW_DATA_DIR, the prompt value and a write
    grant for `<raw root>/<project>` only -- never the raw root or a sibling."""

    def setUp(self):
        import opencode_context_window
        from fixtures.opencode_native_context import launch_context
        native = mock.patch.object(opencode_context_window, "prepare_launch_context", side_effect=launch_context)
        native.start()
        self.addCleanup(native.stop)
        self.parent_procs = []
        self.saved_env = dict(os.environ)

    def tearDown(self):
        for proc in self.parent_procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        os.environ.clear()
        os.environ.update(self.saved_env)

    def seed_parent(self, jobs, repo, attempt, harness, sandbox):
        proc = subprocess.Popen(["sleep", "60"])
        self.parent_procs.append(proc)
        start = (Path("/proc") / str(proc.pid) / "stat").read_text().split()[21]
        jobs.write_text(
            f"2026-07-23T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            f"fallback_hop=same-harness-headless,worker_type=owner,harness={harness},"
            f"runtime_sandbox={sandbox},"
            f"attempt_id={attempt},pid={proc.pid},pid_start={start}\n")

    def load_wrapper(self, harness):
        spec = importlib.util.spec_from_file_location(
            f"{harness}_raw_dir_fixture", ROOT / f"adapters/{harness}/bin/dispatch-headless.py")
        wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wrapper)
        return wrapper

    def test_three_harnesses_grant_only_the_project_folder(self):
        import artifact_producer as AP
        import artifact_lifecycle as AL
        route_spec = importlib.util.spec_from_file_location("raw_dir_compose_route", ROOT / "utilities/capability-route.py")
        route_module = importlib.util.module_from_spec(route_spec)
        route_spec.loader.exec_module(route_module)
        from fixtures.opencode_native_context import launch_context
        sandboxes = {"claude": "adapter-default", "codex": "workspace-write", "opencode": "adapter-default"}
        for harness in ADAPTERS:
            with self.subTest(harness=harness), tempfile.TemporaryDirectory(prefix="raw-grant-") as td:
                root = Path(td).resolve()
                source = root / "original"
                source.mkdir()
                artifact = source / ".agent_reports"
                artifact.mkdir()
                jobs = root / "jobs.log"
                logs = root / "logs"
                fakebin = root / "bin"
                fakebin.mkdir()
                logs.mkdir()
                home = root / "home"
                home.mkdir()
                raw_root = root / "data" / "_agent_raw"
                raw_root.mkdir(parents=True)
                (raw_root / "other-project").mkdir()
                _write_hosts(home, f"raw_data_root: {raw_root}")
                expected_raw = raw_root / "original"
                attempt = fixture_processes.unique_attempt_id(f"att-parent-raw-{harness}")
                sandbox = sandboxes[harness]
                evidence_rows = [{
                    "parent_harness": child, "parent_transport": "headless",
                    "parent_sandbox": sandboxes[child], "child_harness": child,
                    "launch_authority": "conductor", "status": "supported",
                    "probe_source": f"{child}-runtime-fixture", "probe_time": "2026-10-24T00:00:00Z",
                    "failure_class": "", "checked_worktree": str(source),
                    "failure_scope": "none", "codex_command": "ok" if child == "codex" else "not-applicable",
                    "retry_on_isolated_worktree": 0,
                } for child in ADAPTERS]
                base_env = {"PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""), "HOME": str(home),
                            "AGENT_HOME": str(ROOT), "AGENT_ARTIFACT_ROOT": str(artifact),
                            "AGENT_DISPATCH_JOBS": str(jobs), "OPENCODE_CONFIG_CONTENT": "{}",
                            "XDG_STATE_HOME": str(root / "xdg"),
                            # A non-Git launch makes its worker temp folder below TMPDIR.
                            "TMPDIR": str(root)}
                with mock.patch.dict(os.environ, {**base_env, "AGENT_DISPATCH_ATTEMPT_ID": attempt}, clear=True):
                    route = route_module.compose_route(
                        capability="autopilot-code", capability_mode="dev", shape="staged", graph="plan",
                        slug="raw-dir-fixture", cwd=source, artifact_root=artifact, unassigned=True,
                        children=list(ADAPTERS), parent_harness=harness,
                        dispatch_evidence={"tuples": evidence_rows, "native_subagent": []})
                AP.activate(artifact, repository_id="repo_" + "a" * 32, artifact_root_id="root_" + "b" * 32,
                            w7={"campaign_id": "camp_" + "c" * 32})
                route_binding = AL.admit_runtime_route(artifact, route)
                route_path = Path(route_binding.route_file)
                node = next(row for row in route["nodes"] if row["id"] == "plan")
                digest = hashlib.sha256(str(artifact.resolve()).encode("utf-8")).hexdigest()
                expected_runtime = root / "xdg" / "hearting" / "dispatch" / "project-runtimes" / digest
                with mock.patch.dict(os.environ, {**base_env, "AGENT_DISPATCH_ATTEMPT_ID": attempt}, clear=True):
                    producer_env = AP.prepare_route_artifact_env(route_path, start=True, jobs=jobs)
                # The main session's producer environment carries the same value.
                self.assertEqual(producer_env["AGENT_RAW_DATA_DIR"], str(expected_raw))

                fake = fakebin / harness
                child_record = f"child-{harness}.json"
                fake.write_text(
                    "#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
                    "args=sys.argv[1:]\n"
                    "if args==['--version']:\n print('2.0.32'); raise SystemExit(0)\n"
                    f"record=Path(os.environ['AGENT_RUNTIME_DIR'])/'{child_record}'\n"
                    "record.write_text(json.dumps({'argv':args,'env':dict(os.environ)}),encoding='utf-8')\n"
                    "if args[:1]==['run']:\n"
                    " print(json.dumps({'type':'text','sessionID':'ses_fixture','part':{'text':'ok'}}))\n"
                    " print(json.dumps({'type':'step_finish','sessionID':'ses_fixture','part':{'reason':'stop'}}))\n",
                    encoding="utf-8")
                fake.chmod(0o755)
                self.seed_parent(jobs, source, attempt, harness, sandbox)
                wrapper = self.load_wrapper(harness)
                argv = ["dispatch-headless.py", "--start", "--worktree", str(source),
                        "--slug", f"raw-dir-{harness}",
                        "--capability", route["capability"], "--capability-mode", route["capability_mode"],
                        "--intensity", route["effective_intensity"], "--dispatch-depth", "2",
                        "--worker-type", "stage", "--worker-mode", node["unit"], "--worker-role", "code-plan",
                        "--assigned-contract", node["completion_gate"], "--owner", "autopilot-code",
                        "--owner-harness", harness, "--unit", node["unit"], "--parent", "owner",
                        "--jobs", str(jobs), "--log-dir", str(logs),
                        "--attempt-id", fixture_processes.unique_attempt_id(f"att-child-raw-{harness}"),
                        "--parent-attempt-id", attempt, "--parent-harness", harness, "--parent-transport", "headless",
                        "--parent-sandbox", sandbox, "--launch-authority", "conductor",
                        "--nested-eligibility", "supported", "--eligibility-source", f"{harness}-fixture",
                        "--fallback-ordinal", "1", "--route-file", str(route_path), "--route-id", route["route_id"],
                        "--route-hash", route["route_hash"], "--route-node", node["id"],
                        "--registry-digest", route["registry_digest"], "--write-scope", ";".join(node["write_scope"]),
                        "--model-role", node["role"], "--model-profile", node["model_profile"],
                        "--launch-lifecycle", "foreground-scoped", "--foreground-timeout", "20",
                        "--prompt-text", "raw data dir child fixture", "--qa", "standard"]
                runtime_env = {**base_env, **producer_env,
                               "AGENT_DISPATCH_ATTEMPT_ID": attempt, "AGENT_DISPATCH_CHILD": "1",
                               "AGENT_ROUTE_FILE": str(route_path), "AGENT_ROUTE_ID": route["route_id"],
                               "AGENT_ROUTE_HASH": route["route_hash"]}
                patches = [mock.patch.dict(os.environ, runtime_env, clear=True)]
                opencode_configs = []
                if harness == "opencode":
                    patches.append(mock.patch.object(wrapper.opencode_context, "prepare_launch_context",
                                                     side_effect=launch_context))
                    real_builder = wrapper.scoped_external_directory_config

                    def capture(*call_args, _real=real_builder, **call_kwargs):
                        raw = _real(*call_args, **call_kwargs)
                        opencode_configs.append(raw)
                        return raw
                    patches.append(mock.patch.object(wrapper, "scoped_external_directory_config", side_effect=capture))
                if hasattr(wrapper, "check_runtime_projection"):
                    patches.append(mock.patch.object(wrapper, "check_runtime_projection", return_value=0))
                if harness == "codex":
                    patches.append(mock.patch.object(wrapper, "codex_app_server_available", return_value=False))
                if hasattr(wrapper, "launch_summary_owner"):
                    patches.append(mock.patch.object(wrapper, "launch_summary_owner",
                                                     return_value={"summary_owner": "test-fixture"}))
                stream = io.StringIO()
                for patcher in patches:
                    patcher.start()
                try:
                    with redirect_stdout(stream):
                        result = wrapper.main(argv)
                finally:
                    for patcher in reversed(patches):
                        patcher.stop()
                output = stream.getvalue()
                self.assertEqual(result, 0, output)
                capture_path = expected_runtime / child_record
                self.assertTrue(capture_path.is_file(), output)
                child = json.loads(capture_path.read_text(encoding="utf-8"))
                self.assertEqual(child["env"].get("AGENT_RAW_DATA_DIR"), str(expected_raw))
                self.assertEqual(child["env"].get("AGENT_RUNTIME_DIR"), str(expected_runtime))
                prompt_path = next((line.split("=", 1)[1] for line in output.splitlines()
                                    if line.startswith("prompt_file=")), None)
                self.assertTrue(prompt_path and Path(prompt_path).is_file(), output)
                prompt = Path(prompt_path).read_text(encoding="utf-8")
                self.assertIn(f"raw_data_dir ({expected_raw})", prompt)
                self.assertTrue(expected_raw.is_dir())
                if harness == "opencode":
                    texts = opencode_configs
                else:
                    texts = [json.dumps(child["argv"])]
                    granted = [child["argv"][i + 1] for i, flag in enumerate(child["argv"][:-1])
                               if flag in ("--add-dir", "--writable-root")]
                    self.assertIn(str(expected_raw), granted, child["argv"])
                    self.assertNotIn(str(raw_root), granted, child["argv"])
                self.assertTrue(any(str(expected_raw) in text for text in texts), texts)
                for text in texts:
                    for suffix in re.findall(re.escape(str(raw_root)) + r'([^"]*)"', text):
                        self.assertTrue(suffix == "/original" or suffix.startswith("/original/"),
                                        (suffix, text[:400]))


if __name__ == "__main__":
    unittest.main()
