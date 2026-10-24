"""GPU arbitration tests: fake devices, real concurrent CPU-only payloads."""
import concurrent.futures
import importlib.util
import io
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpu_leases as G

SOURCE = Path(G.__file__).read_text()
OBS = {"reachable": True, "gpus": [
    {"index": 0, "uuid": "GPU-zero", "free_mib": 24000, "utilization_gpu_pct": 0, "processes": []},
    {"index": 1, "uuid": "GPU-one", "free_mib": 23000, "utilization_gpu_pct": 0, "processes": []}]}


class LeasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "dispatch" / "gpu-leases.json"
        self.no_tmux_bin = self.root / "no-tmux-bin"
        self.no_tmux_bin.mkdir()
        for name, target in (("python3", sys.executable), ("bash", "/bin/bash"),
                             ("id", "/usr/bin/id"), ("sh", "/bin/sh"),
                             ("tr", "/usr/bin/tr"), ("env", "/usr/bin/env")):
            (self.no_tmux_bin / name).symlink_to(target)

    def acquire(self, **kwargs):
        return G.acquire(self.state, OBS, **kwargs)

    def test_selection_and_explicit_conflict_gives_owner_task_time_and_free_gpu(self):
        lease = self.acquire(owner={"label": "SR_CorrNet [cd]"}, task="train", run_id="one")
        self.assertEqual(lease["gpus"], ["0"])
        with self.assertRaisesRegex(G.GPUUnavailable, r"SR_CorrNet \[cd\].*train.*free GPUs: gpu1"):
            self.acquire(requested="0")
        second = self.acquire()
        self.assertEqual(second["gpus"], ["1"])
        with self.assertRaisesRegex(G.GPUUnavailable, "free GPUs: none"):
            self.acquire()

    def test_idle_memory_holding_process_is_occupied_with_or_without_owner(self):
        for owner in ({"label": "TF [33]"}, None):
            observation = json.loads(json.dumps(OBS))
            observation["gpus"][0]["processes"] = [{"pid": 9, "used_memory_mib": 7700,
                                                      "owner": owner, "command": "idle train"}]
            lease = G.acquire(self.state, observation)
            self.assertEqual(lease["gpus"], ["1"])
            G.release(self.state, lease)
            with self.assertRaisesRegex(G.GPUUnavailable, "idle train"):
                G.acquire(self.state, observation, requested="0")

    def test_share_still_records_a_lease_and_default_remains_exclusive(self):
        first = self.acquire(requested="0")
        second = self.acquire(requested="0", share=True)
        self.assertEqual(len(G.snapshot(self.state)), 2)
        G.release(self.state, first)
        with self.assertRaises(G.GPUUnavailable):
            self.acquire(requested="0")
        G.release(self.state, second)
        self.assertEqual(self.acquire(requested="0")["gpus"], ["0"])

    def test_dead_or_reused_pid_is_pruned_immediately_but_unknown_is_retained(self):
        lease = self.acquire(requested="0")
        with G.locked(self.state) as data:
            data["leases"][lease["token"]]["starttime"] = "0"
        self.assertEqual(self.acquire(requested="0")["gpus"], ["0"])
        with G.locked(self.state) as data:
            row = next(iter(data["leases"].values()))
            row["pid_namespace"] = "unknown namespace"
        with self.assertRaises(G.GPUUnavailable):
            self.acquire(requested="0")

    def test_multi_gpu_admission_is_all_or_nothing_and_uuid_is_canonicalized(self):
        self.acquire(requested="1")
        with self.assertRaises(G.GPUUnavailable):
            self.acquire(requested="0,1")
        self.assertEqual(len(G.snapshot(self.state)), 1)
        self.assertEqual(self.acquire(requested="GPU-zero")["gpus"], ["0"])

    def test_snapshot_does_not_create_or_rewrite_remote_state(self):
        missing = self.root / "remote" / "gpu-leases.json"
        self.assertEqual(G.snapshot(missing), [])
        self.assertFalse(missing.parent.exists())
        self.acquire(requested="0")
        before = self.state.read_bytes()
        before_stat = self.state.stat()
        with mock.patch.object(G, "living", return_value=False), \
                mock.patch.object(G, "locked", side_effect=AssertionError("probe must not lock/write")):
            self.assertEqual(G.snapshot(self.state), [])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.state.stat().st_mtime_ns, before_stat.st_mtime_ns)

    def test_unknown_gpu_measurement_refuses_gpu_but_explicit_cpu_still_runs(self):
        for row in ({"reachable": False}, {**OBS, "detail": "smi error"},
                    {**OBS, "process_detail": "process query failed"}):
            with self.assertRaises(G.GPUUnavailable):
                G.acquire(self.state, row)
            self.assertIsNone(G.acquire(self.state, row, requested=""))

    def options(self, name, requested=None):
        run_dir = self.root / name
        inner = ('__HEARTING_GPU_SETUP__ && python3 -c '
                 + "'import os,time; print(os.environ[\"CUDA_VISIBLE_DEVICES\"], flush=True); time.sleep(.6)'"
                 + ' > ' + str(run_dir / "log") + '; printf "%s" "$?" > ' + str(run_dir / "exit_code"))
        return {"state_path": str(self.state), "observation": OBS, "requested": requested,
                "owner": {"label": name}, "task": "CPU fixture", "run_id": "gpu-lease-test-" + name,
                "run_dir": str(run_dir), "inner": inner}

    def launch(self, options, env=None, payload_source=None):
        payload_source = payload_source or G.serialized_payload_source()
        code = SOURCE + '\nprint(json.dumps(launch_compute(%r, %r)))' % (options, payload_source)
        launch_env = {**os.environ, "PATH": str(self.no_tmux_bin)}
        if env:
            launch_env.update(env)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=10, env=launch_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_tmux_launch_uses_private_server_and_exact_wrapper_environment(self):
        fakebin = self.root / "tmux-bin"
        fakebin.mkdir()
        records = self.root / "tmux-records"
        fake = fakebin / "tmux"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "server = args[args.index('-L') + 1]\n"
            "records = pathlib.Path(os.environ['HEARTING_FAKE_TMUX_RECORDS'])\n"
            "def record(row):\n"
            "    path = records / (row['event'] + '.jsonl')\n"
            "    path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with path.open('a') as handle: handle.write(json.dumps(row) + '\\n')\n"
            "if 'start-server' in args:\n"
            "    record({'event':'start', 'server':server, 'cwd':os.getcwd(), "
            "'pins':{k:os.environ.get(k) for k in ('AGENT_HOME','AGENT_DISPATCH_ATTEMPT_ID',"
            "'CODEX_THREAD_ID','CLAUDE_CODE_SESSION_ID','TMUX','TMUX_PANE',"
            "'HEARTING_COMPUTE_RUN_ID','HEARTING_COMPUTE_HOST','HEARTING_RESOURCE_RUN_ID',"
            "'HEARTING_GPU_LEASE_RELEASE')}})\n"
            "else:\n"
            "    tail = args[args.index('new-session') + 1:]\n"
            "    cwd = '/'; command = None; index = 0\n"
            "    while index < len(tail):\n"
            "        item = tail[index]\n"
            "        if item in ('-d',): index += 1\n"
            "        elif item in ('-s','-c'):\n"
            "            value = tail[index + 1]; index += 2\n"
            "            if item == '-s': run_id = value\n"
            "            elif item == '-c': cwd = value\n"
            "        else: command = item; break\n"
            "    record({'event':'session', 'server':server, 'cwd':cwd, "
            "'command':command, 'run_id':run_id})\n"
            "    raise SystemExit(subprocess.run(['/bin/sh','-c',command], cwd=cwd).returncode)\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        options = self.options("isolated-tmux", requested="")
        output = Path(options["run_dir"]) / "transport-result"
        options["inner"] = (
            "__HEARTING_GPU_SETUP__ && printf '%s|%s|%s' \"$AGENT_HOME\" "
            "\"$AGENT_DISPATCH_ATTEMPT_ID\" \"$CODEX_THREAD_ID\" > " + str(output)
        )
        options["cwd"] = str(self.root)
        options["tmux_environment"] = {
            **{key: "" for key in G.session_environment_keys()},
            "AGENT_HOME": "/fixture/pinned-release",
            "AGENT_DISPATCH_ATTEMPT_ID": "att-current-launch",
            "CODEX_THREAD_ID": "current-thread",
            "HEARTING_COMPUTE_RUN_ID": options["run_id"],
            "HEARTING_COMPUTE_HOST": "fixture-host",
            "HEARTING_RESOURCE_RUN_ID": "",
            "HEARTING_GPU_LEASE_RELEASE": "",
            "FIXTURE_QUOTING": "space 'quote' \"double\"\n$HOME `tick`",
        }
        server_env = {
            "PATH": str(fakebin) + os.pathsep + str(self.no_tmux_bin),
            "HEARTING_FAKE_TMUX_RECORDS": str(records),
            "AGENT_HOME": "/launcher/stale-release",
            "AGENT_DISPATCH_ATTEMPT_ID": "att-launcher-stale",
            "CODEX_THREAD_ID": "launcher-stale-thread",
            "TMUX": "/tmp/default-tmux,123,0",
            "TMUX_PANE": "%9",
            "HEARTING_COMPUTE_RUN_ID": "stale-compute-run",
            "HEARTING_COMPUTE_HOST": "stale-host",
            "HEARTING_RESOURCE_RUN_ID": "stale-resource-run",
            "HEARTING_GPU_LEASE_RELEASE": "echo stale-lease",
        }
        wrapper_env_path = self.root / "wrapper-env.json"
        payload_source = (G.serialized_payload_source() +
                          ("\nopen(%r, 'w').write(json.dumps({key: os.environ.get(key) "
                           "for key in ('AGENT_HOME', 'AGENT_DISPATCH_ATTEMPT_ID', "
                           "'CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID', "
                           "'HEARTING_RESOURCE_RUN_ID', 'FIXTURE_QUOTING')}))\n") %
                          str(wrapper_env_path))
        receipt = self.launch(options, env=server_env, payload_source=payload_source)
        self.assertEqual(output.read_text(),
                         "/fixture/pinned-release|att-current-launch|current-thread")
        transport = receipt["tmux_transport"]
        self.assertEqual(transport["kind"], "hearting-tmux-v1")
        self.assertRegex(transport["server"], r"^hearting-[0-9a-f]{20}$")
        start = json.loads((records / "start.jsonl").read_text().splitlines()[0])
        session = json.loads((records / "session.jsonl").read_text().splitlines()[0])
        self.assertEqual(start["server"], transport["server"])
        self.assertEqual(start["cwd"], "/")
        self.assertEqual(set(start["pins"].values()), {None})
        self.assertEqual(session["cwd"], str(self.root))
        self.assertTrue(session["command"].startswith("exec env "))
        self.assertEqual(shlex.split(session["command"])[0:2], ["exec", "env"])
        wrapper_env = json.loads(wrapper_env_path.read_text())
        self.assertEqual(wrapper_env["AGENT_HOME"], "/fixture/pinned-release")
        self.assertEqual(wrapper_env["AGENT_DISPATCH_ATTEMPT_ID"], "att-current-launch")
        self.assertEqual(wrapper_env["CODEX_THREAD_ID"], "current-thread")
        self.assertEqual(wrapper_env["CLAUDE_CODE_SESSION_ID"], "")
        self.assertEqual(wrapper_env["HEARTING_RESOURCE_RUN_ID"], "")
        self.assertEqual(wrapper_env["FIXTURE_QUOTING"], options["tmux_environment"]["FIXTURE_QUOTING"])

    def test_tmux_21_and_30a_fakes_run_both_launch_paths_without_legacy_options(self):
        fakebin = self.root / "old-tmux-bin"
        fakebin.mkdir()
        fake = fakebin / "tmux"
        fake.write_text(
            "#!%s\nimport os,sys,subprocess\n"
            "args=sys.argv[1:]\n"
            "if '-e' in args:\n print('tmux: unknown option -- e', file=sys.stderr); sys.exit(1)\n"
            "if args[-1] == 'start-server': sys.exit(0)\n"
            "if 'new-session' not in args: sys.exit(2)\n"
            "tail=args[args.index('new-session')+1:]\n"
            "cwd='/'\n"
            "while tail:\n"
            " item=tail.pop(0)\n"
            " if item == '-d': continue\n"
            " if item == '-s': tail.pop(0); continue\n"
            " if item == '-c': cwd=tail.pop(0); continue\n"
            " command=item; break\n"
            "open(os.environ['TMUX_TEST_CWD'], 'w').write(cwd)\n"
            "sys.exit(subprocess.run(['/bin/sh','-c',command], cwd=cwd).returncode)\n" % sys.executable,
            encoding="utf-8")
        fake.chmod(0o755)
        for version in ("2.1", "3.0a"):
            for namespaced in (False, True):
                with self.subTest(version=version, namespaced=namespaced):
                    tag = version.replace(".", "") + ("-ns" if namespaced else "-plain")
                    options = self.options(tag, requested="")
                    probe = self.root / (tag + "-wrapper.json")
                    output = self.root / (tag + "-payload.txt")
                    options["inner"] = ("__HEARTING_GPU_SETUP__ && printf '%s|%s' "
                                        "\"$AGENT_HOME\" \"$HEARTING_RESOURCE_RUN_ID\" > "
                                        + shlex.quote(str(output)))
                    options["tmux_environment"] = {
                        "AGENT_HOME": "/current/release", "AGENT_DISPATCH_ATTEMPT_ID": "att-current",
                        "HEARTING_COMPUTE_RUN_ID": options["run_id"],
                        "HEARTING_COMPUTE_HOST": "old-tmux", "HEARTING_RESOURCE_RUN_ID": "",
                        "HEARTING_GPU_LEASE_RELEASE": "", "CODEX_THREAD_ID": "thread-current",
                        "CLAUDE_CODE_SESSION_ID": "", "EMPTY_VALUE": "",
                    }
                    payload_source = (G.serialized_payload_source() +
                                      ("\nopen(%r, 'w').write(json.dumps({k: os.environ.get(k) "
                                       "for k in ('AGENT_HOME','AGENT_DISPATCH_ATTEMPT_ID', "
                                       "'HEARTING_RESOURCE_RUN_ID','CLAUDE_CODE_SESSION_ID')}))\n") %
                                      str(probe))
                    launch_env = {"PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
                                  "AGENT_HOME": "stale", "AGENT_DISPATCH_ATTEMPT_ID": "stale",
                                  "HEARTING_RESOURCE_RUN_ID": "stale", "TMUX": "stale",
                                  "TMUX_TEST_CWD": str(self.root / (tag + "-cwd"))}
                    if namespaced:
                        options.update({"path_namespace": {"/target": "/source"},
                                        "run_root": "/target/runs", "cwd": None})
                        namespace_source = ("class _NamespaceRefusal(Exception): pass\n"
                                            "def _ns_safe_run_fd(*args): return None\n"
                                            "def _ns_enter(*args): return None, None\n")
                        with mock.patch.object(G, "_ns_namespace_creation"), \
                             mock.patch.object(G, "_PATH_NAMESPACE_SOURCE", namespace_source), \
                             mock.patch.dict(os.environ, launch_env, clear=True):
                            receipt = G.launch_compute_namespaced(options, payload_source)
                    else:
                        options["cwd"] = str(self.root)
                        with mock.patch.dict(os.environ, launch_env, clear=True):
                            receipt = G.launch_compute(options, payload_source)
                    self.assertEqual(receipt["tmux_transport"]["kind"], "hearting-tmux-v1")
                    expected_cwd = "/" if namespaced else str(self.root)
                    self.assertEqual(Path(launch_env["TMUX_TEST_CWD"]).read_text(), expected_cwd)
                    self.assertEqual(output.read_text(), "/current/release|")
                    wrapper_env = json.loads(probe.read_text())
                    self.assertEqual(wrapper_env["AGENT_HOME"], "/current/release")
                    self.assertEqual(wrapper_env["AGENT_DISPATCH_ATTEMPT_ID"], "att-current")
                    self.assertEqual(wrapper_env["HEARTING_RESOURCE_RUN_ID"], "")
                    self.assertEqual(wrapper_env["CLAUDE_CODE_SESSION_ID"], "")

    def test_tmux_launch_failure_releases_lease_for_a_retry(self):
        fakebin = self.root / "failing-tmux-bin"
        fakebin.mkdir()
        tmux = fakebin / "tmux"
        tmux.write_text(
            "#!/bin/sh\n"
            'if [ "$3" = "start-server" ]; then exit 0; fi\n'
            "exit 1\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        options = self.options("tmux-failure", requested="0")
        payload_source = G.serialized_payload_source()
        code = SOURCE + "\nlaunch_compute(%r, %r)\n" % (options, payload_source)
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            timeout=10,
            env={**os.environ, "PATH": str(fakebin) + os.pathsep + str(self.no_tmux_bin)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(G.snapshot(self.state), [])
        retry = G.acquire(self.state, OBS, requested="0", run_id="tmux-retry")
        self.assertEqual(retry["gpus"], ["0"])
        G.release(self.state, retry)

    def test_tmux_environment_command_round_trips_shell_sensitive_values(self):
        values = {"EMPTY_PIN": "", "SPACE_PIN": "space value",
                  "QUOTE_PIN": "single' double\" newline\n $HOME `tick`"}
        environment = G._validated_tmux_launch_environment(values)
        command = G._tmux_launch_command(
            environment,
            "python3 -c " + shlex.quote(
                "import json,os; print(json.dumps({k:os.environ.get(k) for k in "
                "('EMPTY_PIN','SPACE_PIN','QUOTE_PIN')}))"))
        self.assertTrue(command.startswith("exec env "))
        tokens = shlex.split(command)
        executable_index = tokens.index("python3")
        assignment_keys = [token.split("=", 1)[0] for token in tokens[2:executable_index]]
        self.assertEqual(assignment_keys, sorted(environment))
        result = subprocess.run(["/bin/sh", "-c", command], capture_output=True,
                                text=True, check=True)
        self.assertEqual(json.loads(result.stdout), values)
        with self.assertRaises(G.GPUUnavailable):
            G._validated_tmux_launch_environment({"BAD=KEY": "value"})
        with self.assertRaises(G.GPUUnavailable):
            G._validated_tmux_launch_environment({"BAD_VALUE": 1})
        with self.assertRaises(G.GPUUnavailable):
            G._validated_tmux_launch_environment({"BAD_VALUE": "nul\0value"})

    def test_tmux_client_failure_has_bounded_structured_output_without_argv(self):
        fake = self.root / "tmux-diagnostic"
        fake.write_text("#!%s\nimport sys\nsys.stdout.write('o' * 5000)\n"
                        "sys.stderr.write('stderr-marker\\n')\nsys.exit(7)\n" % sys.executable,
                        encoding="utf-8")
        fake.chmod(0o755)
        for operation in ("start-server", "new-session"):
            with self.subTest(operation=operation), self.assertRaises(G.GPUUnavailable) as caught:
                G._run_tmux_launch(operation, [str(fake), "private-payload-secret"], {})
            detail = json.loads(str(caught.exception))
            self.assertEqual(detail["operation"], operation)
            self.assertEqual(detail["exit_code"], 7)
            self.assertIn("[truncated]", detail["stdout"])
            self.assertIn("stderr-marker", detail["stderr"])
            self.assertNotIn("private-payload-secret", str(caught.exception))

    def test_tmux_client_does_not_wait_for_daemon_inherited_capture_fds(self):
        finished = self.root / "daemon-finished"
        fake = self.root / "tmux-daemon-child"
        fake.write_text(
            "#!%s\nimport os,time\npid=os.fork()\n"
            "if pid == 0:\n time.sleep(0.5); open(%r,'w').write('done'); os._exit(0)\n"
            "os._exit(0)\n" % (sys.executable, str(finished)), encoding="utf-8")
        fake.chmod(0o755)
        started = time.monotonic()
        G._run_tmux_launch("new-session", [str(fake)], {})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.4)
        deadline = time.monotonic() + 2
        while not finished.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(finished.exists())

    def test_two_concurrent_sessions_choose_distinct_gpus_and_release_on_exit(self):
        options = [self.options("one"), self.options("two")]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(self.launch, options))
        self.assertEqual({row["gpus"] for row in receipts}, {"0", "1"})
        deadline = time.monotonic() + 5
        while G.snapshot(self.state) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(G.snapshot(self.state), [])
        for opts, receipt in zip(options, receipts):
            self.assertEqual((Path(opts["run_dir"]) / "log").read_text().strip(), receipt["gpus"])
            self.assertEqual((Path(opts["run_dir"]) / "exit_code").read_text(), "0")

    def test_resource_and_compute_share_the_same_admission(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.state.parent / "jobs.log")}), \
             mock.patch.object(G, "local_observation", return_value=OBS), \
             mock.patch.object(G, "launcher_owner", return_value={"label": "resource [ab]"}):
            path, lease, env = G.resource_admission({"resource_class": "gpu"}, ["true"], run_id="resource")
        self.assertEqual(path, self.state)
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "0")
        receipt = self.launch(self.options("compute"))
        self.assertEqual(receipt["gpus"], "1")
        self.assertEqual(subprocess.run(["sh", "-c", env["HEARTING_GPU_LEASE_RELEASE"]]).returncode, 0)
        self.assertNotIn(lease["token"], {r["token"] for r in G.snapshot(self.state)})

    def _registered_jobs(self, *, include_child=False, parent_sid="registered-parent"):
        jobs = self.root / "dispatch" / "jobs.log"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        owner_meta = {
            "attempt_id": "att-route-owner", "worker_type": "owner", "dispatch_depth": "1",
            "owner_route_id": "rt-gpu-owner", "owner_route_hash": "sha256:gpu-owner",
            "parent_sid": parent_sid,
        }
        def row(status, slug, metadata):
            values = ",".join("%s=%s" % item for item in metadata.items())
            return "\t".join(("2099-01-01T00:00:00Z", status, "/work", "/work", slug, values)) + "\n"
        content = row("done", "owner", owner_meta)
        if include_child:
            child_meta = {
                "attempt_id": "att-route-child", "parent_attempt_id": "att-route-owner",
                "worker_type": "stage", "dispatch_depth": "2", "route_id": "rt-gpu-owner",
                "route_hash": "sha256:gpu-owner", "route_node": "execute",
            }
            content += row("open", "execute", child_meta)
        jobs.write_text(content, encoding="utf-8")
        return jobs, owner_meta

    def _route_ledger(self, state, harness, sid):
        path = Path(state) / harness / (sid + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"event": "compose", "sid": sid}) + "\n", encoding="utf-8")

    def _owner_environment(self, jobs, *, attempt="att-route-owner", depth="1",
                           caller="codex", session_key="CODEX_THREAD_ID", session_id=""):
        return {"AGENT_DISPATCH_ATTEMPT_ID": attempt, "AGENT_DISPATCH_DEPTH": depth,
                "AGENT_DISPATCH_JOBS": str(jobs), "AGENT_DISPATCH_CALLER_HARNESS": caller,
                session_key: session_id}

    def test_registered_worker_environments_resolve_parent_harness_from_unique_ledger(self):
        launchers = (("codex", "CODEX_THREAD_ID", ""),
                     ("claude", "CLAUDE_CODE_SESSION_ID", "claude-worker"),
                     ("opencode", "OPENCODE_SESSION_ID", "opencode-worker"))
        for parent_harness in ("claude", "codex", "opencode"):
            for caller, key, worker_sid in launchers:
                with self.subTest(parent_harness=parent_harness, caller=caller):
                    parent_sid = "parent-%s" % parent_harness
                    jobs, _ = self._registered_jobs(parent_sid=parent_sid)
                    route_state = self.root / ("route-chains-%s-%s" % (parent_harness, caller))
                    self._route_ledger(route_state, parent_harness, parent_sid)
                    environ = self._owner_environment(
                        jobs, caller=caller, session_key=key, session_id=worker_sid)
                    with mock.patch.dict(os.environ, {
                            "XDG_STATE_HOME": str(self.root / "state-home"),
                            "FLEET_ROUTE_CHAIN_DIR": str(route_state)}, clear=False):
                        owner = G.launcher_owner(environ)
                    self.assertEqual((owner["harness"], owner["id"]),
                                     (parent_harness, parent_sid))

    def test_registered_depth_two_attempt_resolves_its_exact_depth_one_parent(self):
        jobs, _ = self._registered_jobs(include_child=True, parent_sid="depth-one-parent")
        route_state = self.root / "route-chains-depth-two"
        self._route_ledger(route_state, "codex", "depth-one-parent")
        environ = self._owner_environment(
            jobs, attempt="att-route-child", depth="2", caller="claude",
            session_key="CLAUDE_CODE_SESSION_ID", session_id="child-session")
        with mock.patch.dict(os.environ, {
                "XDG_STATE_HOME": str(self.root / "state-home-depth-two"),
                "FLEET_ROUTE_CHAIN_DIR": str(route_state)}, clear=False):
            owner = G.launcher_owner(environ)
        self.assertEqual((owner["harness"], owner["id"]), ("codex", "depth-one-parent"))

    def test_local_resource_admission_records_registered_parent_as_lease_owner(self):
        jobs, _ = self._registered_jobs(parent_sid="local-route-parent")
        route_state = self.root / "route-chains-local-admission"
        self._route_ledger(route_state, "opencode", "local-route-parent")
        with mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_ATTEMPT_ID": "att-route-owner",
                "AGENT_DISPATCH_DEPTH": "1",
                "AGENT_DISPATCH_JOBS": str(jobs),
                "AGENT_DISPATCH_CALLER_HARNESS": "claude",
                "CLAUDE_CODE_SESSION_ID": "worker-session",
                "CUDA_VISIBLE_DEVICES": "0",
                "XDG_STATE_HOME": str(self.root / "state-home-local-admission"),
                "FLEET_ROUTE_CHAIN_DIR": str(route_state)}, clear=False), \
             mock.patch.object(G, "local_observation", return_value=OBS):
            path, lease, _env = G.resource_admission(
                {"resource_class": "gpu"}, ["python", "train.py"],
                jobs=jobs, run_id="local-registered-run")
        self.assertEqual(path, self.state)
        self.assertEqual((lease["owner"]["harness"], lease["owner"]["id"]),
                         ("opencode", "local-route-parent"))
        G.release(path, lease)

    def test_confirmed_handover_supplies_current_sid_and_harness_without_parent_harness(self):
        jobs, owner_meta = self._registered_jobs(parent_sid="before-clear")
        state_home = self.root / "state-home-handover"
        tidy = state_home / "hearting" / "session-tidy"
        seat_key = "seat-fixture"
        binding = {"jobs": str(jobs.resolve()), "attempt": "att-route-owner",
                   "route": "rt-gpu-owner", "hash": "sha256:gpu-owner",
                   "node": "", "parent": "before-clear"}
        snapshot = {"schema": 1, "seat": {"kind": "pane", "key": seat_key},
                    "from": {"harness": "claude", "sid": "before-clear"},
                    "at": 1, "bindings": [binding]}
        handover_dir = tidy / "handover"
        handover_dir.mkdir(parents=True)
        (handover_dir / (seat_key + ".json")).write_text(json.dumps(snapshot), encoding="utf-8")
        ledger_dir = tidy / "sessions"
        ledger_dir.mkdir(parents=True)
        event = {"event": "handover", "from": "before-clear", "sid": "after-clear",
                 "harness": "opencode", "ts": 2, "bindings": [binding]}
        (ledger_dir / (seat_key + ".jsonl")).write_text(json.dumps(event) + "\n", encoding="utf-8")
        environ = self._owner_environment(
            jobs, caller="claude", session_key="CLAUDE_CODE_SESSION_ID", session_id="worker-session")
        with mock.patch.dict(os.environ, {
                "XDG_STATE_HOME": str(state_home),
                "FLEET_ROUTE_CHAIN_DIR": str(self.root / "route-chains-handover")}, clear=False):
            owner = G.launcher_owner(environ)
        self.assertNotIn("parent_harness", owner_meta)
        self.assertEqual((owner["harness"], owner["id"]), ("opencode", "after-clear"))

    def test_registered_lineage_failures_do_not_fall_back_to_worker_session(self):
        jobs, _ = self._registered_jobs(parent_sid="ambiguous-parent")
        route_state = self.root / "route-chains-ambiguous"
        self._route_ledger(route_state, "codex", "ambiguous-parent")
        self._route_ledger(route_state, "claude", "ambiguous-parent")
        environ = self._owner_environment(
            jobs, caller="claude", session_key="CLAUDE_CODE_SESSION_ID", session_id="worker-session")
        with mock.patch.dict(os.environ, {
                "XDG_STATE_HOME": str(self.root / "state-home-ambiguous"),
                "FLEET_ROUTE_CHAIN_DIR": str(route_state)}, clear=False):
            self.assertIsNone(G.launcher_owner(environ))
            missing = {**environ, "AGENT_DISPATCH_JOBS": str(self.root / "missing.log")}
            self.assertIsNone(G.launcher_owner(missing))
            malformed = {**environ, "AGENT_DISPATCH_DEPTH": "7"}
            self.assertIsNone(G.launcher_owner(malformed))

    def test_registered_attempt_without_valid_depth_stays_unknown_and_gpu_admission_is_fail_soft(self):
        jobs = self.root / "dispatch-depth" / "jobs.log"
        jobs.parent.mkdir(parents=True)
        jobs.write_text("", encoding="utf-8")
        for index, depth in enumerate((None, "", "not-a-depth", "3", " 1", "01")):
            environ = {
                "AGENT_DISPATCH_ATTEMPT_ID": "att-missing-depth",
                "AGENT_DISPATCH_JOBS": str(jobs),
                "CLAUDE_CODE_SESSION_ID": "worker-session",
                "CODEX_THREAD_ID": "other-session",
                "CUDA_VISIBLE_DEVICES": "0",
            }
            if depth is not None:
                environ["AGENT_DISPATCH_DEPTH"] = depth
            with self.subTest(depth=depth), \
                 mock.patch.dict(os.environ, environ, clear=True), \
                 mock.patch.object(G, "local_observation", return_value=OBS):
                self.assertIsNone(G.launcher_owner(os.environ, jobs=jobs))
                path, lease, _gpu_env = G.resource_admission(
                    {"resource_class": "gpu"}, ["python", "train.py"],
                    jobs=jobs, run_id="unknown-depth-%s" % index)
                self.assertEqual(path, jobs.parent / "gpu-leases.json")
                self.assertIsNotNone(lease)  # identity uncertainty does not block launch
                self.assertIsNone(lease["owner"])
                payload_env = G.launcher_session_environment({
                    "CLAUDE_CODE_SESSION_ID": "worker-session",
                    "CODEX_THREAD_ID": "other-session",
                }, owner=lease["owner"])
                self.assertFalse(set(G.session_environment_keys()) & payload_env.keys())
                G.release(path, lease)

    def test_unregistered_identity_fallback_and_session_environment_alias_cleanup(self):
        direct = {"AGENT_DISPATCH_CALLER_HARNESS": "codex", "CODEX_THREAD_ID": "direct-session"}
        self.assertEqual(G.launcher_owner(direct)["id"], "direct-session")
        explicit_zero = {**direct, "AGENT_DISPATCH_ATTEMPT_ID": "att-explicit-zero",
                         "AGENT_DISPATCH_DEPTH": "0"}
        self.assertEqual(G.launcher_owner(explicit_zero)["id"], "direct-session")
        source = {"CLAUDE_CODE_SESSION_ID": "worker-session", "CLAUDE_SESSION_ID": "old-alias",
                  "CODEX_THREAD_ID": "foreign-codex", "OPENCODE_SESSION_ID": "foreign-opencode",
                  "KEEP_ME": "preserved"}
        owner = {"kind": "session", "harness": "codex", "id": "route-parent"}
        updated = G.launcher_session_environment(source, owner=owner)
        self.assertEqual(source["CLAUDE_CODE_SESSION_ID"], "worker-session")
        self.assertEqual(updated["KEEP_ME"], "preserved")
        self.assertEqual(updated.get("CODEX_THREAD_ID"), "route-parent")
        self.assertFalse((set(G.session_environment_keys()) - {"CODEX_THREAD_ID"}) & updated.keys())

    def test_explicit_cpu_payload_in_gpu_scoped_route_skips_gpu_probe_and_lease(self):
        for value in ("", "-1"):
            self.assertEqual(G.select({}, [], requested=value), [])
            for command in (["python", "cpu_payload.py"],
                            ["env", "CUDA_VISIBLE_DEVICES=" + value, "python", "cpu_payload.py"]):
                with self.subTest(value=value, command=command), \
                     mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": value}), \
                     mock.patch.object(G, "local_observation") as probe:
                    self.assertEqual(G.resource_admission({"resource_class": "gpu"},
                                     command, gpu_scoped=True), (None, None, {}))
                probe.assert_not_called()

    def test_unbound_wrapper_never_runs_after_its_pending_reservation_was_reaped(self):
        lease = self.acquire()
        G.release(self.state, lease)
        marker = self.root / "forbidden"
        with self.assertRaises(G.GPUUnavailable):
            G.payload(str(self.state), lease, "touch " + str(marker))
        self.assertFalse(marker.exists())

    def test_resource_runner_fences_records_and_releases_actual_gpu_payload(self):
        spec = importlib.util.spec_from_file_location("_gpu_test_runner", Path(G.__file__).with_name("resource-runner.py"))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        route = self.root / "route.json"
        route.write_text(json.dumps({"capability": "autopilot-lab", "artifact_root": str(self.root),
            "nodes": [{"id": "full-run", "kind": "resource-runner", "resource_class": "long-running",
                       "resource_transport": "detached-process"}]}))
        registry, log = self.root / "runs.json", self.root / "resource.log"
        output, children = io.StringIO(), []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.state.parent / "jobs.log")}), \
             mock.patch.object(G, "local_observation", return_value=OBS), \
             mock.patch.object(runner.subprocess, "run"), mock.patch.object(runner, "register_registry"), \
             mock.patch.object(runner.subprocess, "Popen", side_effect=spawn), mock.patch("sys.stdout", output):
            runner.main(["--registry", str(registry), "start", "--run-id", "local", "--cwd", str(self.root),
                         "--log", str(log), "--route", str(route), "--node", "full-run", "--smoke-attestation", "unused",
                         "--", sys.executable, "-c", "import os; print(os.environ['CUDA_VISIBLE_DEVICES'])"])
            children[0].wait(timeout=5)
        row = json.loads(output.getvalue())
        self.assertEqual(row["gpus"], "0")
        self.assertEqual(log.read_text().strip(), "0")
        self.assertEqual(Path(row["sentinel"]).read_text(), "0")
        self.assertEqual(G.snapshot(self.state), [])

    def test_shared_state_separates_hosts_and_never_prunes_foreign_host_pids(self):
        first = self.acquire(requested="0")
        with G.locked(self.state) as data:
            data["leases"][first["token"]]["host"] = "another-host"
            data["leases"][first["token"]]["starttime"] = "0"
        second = self.acquire(requested="0")
        self.assertEqual(second["gpus"], ["0"])
        with G.locked(self.state) as data:
            self.assertIn(first["token"], data["leases"])
        self.assertEqual([row["token"] for row in G.snapshot(self.state)], [second["token"]])

    def test_supervised_full_run_uses_existing_gpu_scope_before_releasing_payload(self):
        import artifact_producer
        spec = importlib.util.spec_from_file_location("_gpu_verified_runner", Path(G.__file__).with_name("resource-runner.py"))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        jobs = self.state.parent / "jobs.log"
        jobs.parent.mkdir(parents=True)
        jobs.write_text("")
        route_file, registry, log = self.root / "verified-route.json", self.root / "verified-runs.json", self.root / "verified.log"
        route = {"route_id": "rt-gpu-scope", "route_hash": "sha256:fixture", "capability": "autopilot-lab",
                 "nodes": [{"id": "full-run", "kind": "resource-runner", "resource_class": "long-running"}]}
        placeholder = {"run_id": "supervised", "cwd": str(self.root), "log": str(log),
            "route": str(route_file), "node": "full-run", "status": "launching", "sentinel": str(log) + ".exit",
            "progress_file": str(log) + ".progress", "owner_wait": {"session_id": "fixture"},
            "command": [sys.executable, "-c", "import os; print(os.environ['CUDA_VISIBLE_DEVICES'])"]}
        children, output = [], io.StringIO()
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc
        with mock.patch.object(G, "local_observation", return_value=OBS), \
             mock.patch.object(runner, "register_registry"), \
             mock.patch.object(artifact_producer, "prepare_route_artifact_env", return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.root)}), \
             mock.patch.object(runner, "start_watch", return_value=(mock.Mock(), runner.proc_identity(os.getpid()))), \
             mock.patch.object(runner.subprocess, "run"), \
             mock.patch.object(runner.subprocess, "Popen", side_effect=spawn), mock.patch("sys.stdout", output):
            runner.start_verified(registry, SimpleNamespace(jobs=str(jobs), run_id="supervised", node="full-run"),
                                  route, route_file, placeholder)
            children[0].wait(timeout=5)
        self.assertEqual(json.loads(output.getvalue())["gpus"], "0")
        self.assertEqual(log.read_text().strip(), "0")
        self.assertEqual(G.snapshot(self.state), [])

    def test_inline_cuda_choice_is_honored_and_suggestions_exclude_reservations(self):
        self.assertEqual(G.requested_devices(["env", "CUDA_VISIBLE_DEVICES=1", "python", "train.py"]), "1")
        tool_spec = importlib.util.spec_from_file_location("_gpu_suggest_compute", Path(G.__file__).with_name("compute-hosts.py"))
        tool = importlib.util.module_from_spec(tool_spec)
        tool_spec.loader.exec_module(tool)
        observation = json.loads(json.dumps(OBS))
        observation["gpus"][0]["reservations"] = [{"token": "busy"}]
        with mock.patch.object(tool, "probe_host", return_value=observation):
            self.assertEqual(tool._run_gpu_observation("here", {})["suggested_gpu"], 1)

    def test_compute_cmd_run_auto_selects_and_refuses_busy_explicit_device_end_to_end(self):
        spec = importlib.util.spec_from_file_location("_gpu_end_to_end_compute", Path(G.__file__).with_name("compute-hosts.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        finish = self.root / "finish"
        code = ("import os,time; from pathlib import Path; print(os.environ['CUDA_VISIBLE_DEVICES'],flush=True); "
                "end=time.monotonic()+10; p=Path(%r); "
                "exec('while not p.exists() and time.monotonic()<end: time.sleep(.02)')" % str(finish))
        args = SimpleNamespace(host="fixture", command=[sys.executable, "-c", code], name="managed-fixture",
                               cwd=None, env=None, gpus=None, share=False, dry_run=False, json=True)
        real_run = subprocess.run
        def local_remote(_host, script, **kwargs):
            return real_run(["bash", "-c", script], capture_output=True, text=True, timeout=10,
                            env={**os.environ, "PATH": str(self.no_tmux_bin)})
        output, warning = io.StringIO(), io.StringIO()
        with mock.patch.object(tool, "load_config", return_value={"run_root": self.root / "managed-runs",
                         "hosts": {"fixture": {"ssh_host": "local"}}}), \
             mock.patch.object(G, "state_path", return_value=self.state), \
             mock.patch.object(tool, "_run_gpu_observation", return_value=OBS), \
             mock.patch.object(tool, "_launcher_session_owner", return_value=None), \
             mock.patch.object(tool, "_launcher_provenance", return_value={}), \
             mock.patch.object(tool, "_launcher_route", return_value=None), \
             mock.patch.object(tool, "_spawn_completion_watch", return_value=False), \
             mock.patch.object(tool, "remote", side_effect=local_remote), \
             mock.patch("sys.stdout", output), mock.patch("sys.stderr", warning):
            try:
                self.assertEqual(tool.cmd_run(args), 0, warning.getvalue())
                receipt = json.loads(output.getvalue())
                self.assertEqual(receipt["gpus"], "0")
                self.assertTrue(receipt["gpu_lease"]["token"])
                args.gpus = "0"
                self.assertEqual(tool.cmd_run(args), 1)
                self.assertIn("GPU in use", warning.getvalue())
                self.assertIn("free GPUs: gpu1", warning.getvalue())
            finally:
                finish.write_text("done")
        deadline = time.monotonic() + 5
        while G.snapshot(self.state) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(G.snapshot(self.state), [])
        self.assertEqual((self.root / "managed-runs" / receipt["run_id"] / "log").read_text().strip(), "0")

    def test_composed_probe_script_compiles_without_changing_early_exit_indentation(self):
        spec = importlib.util.spec_from_file_location("_gpu_test_compute", Path(G.__file__).with_name("compute-hosts.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        def check_script(_host, script, **kwargs):
            body = script.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            compile(body, "composed-probe", "exec")
            return subprocess.CompletedProcess([], 0, json.dumps({"gpus": [], "gpu_leases": []}), "")
        with mock.patch.object(tool, "remote", side_effect=check_script), \
             mock.patch.object(G, "state_path", return_value=self.state):
            self.assertTrue(tool.probe_host("fixture", {"ssh_host": "local"}, [], [])["reachable"])


class PathNamespaceSerializationTest(unittest.TestCase):
    def test_legacy_compute_source_is_exact_pre_extension_bytes(self):
        raw = Path(G.__file__).read_bytes()
        marker = b"# BEGIN PATH_NAMESPACE EXTENSION\n"
        prefix = raw.split(marker, 1)[0]
        self.assertEqual(G.serialized_compute_source(False).encode(), prefix)
        self.assertNotIn(b"_PATH_NAMESPACE_SOURCE", prefix)
        self.assertEqual(G.serialized_compute_source(True).encode(), raw)

    def test_remote_namespace_helper_rejects_missing_source_before_syscalls(self):
        namespace = {}
        exec(G._PATH_NAMESPACE_SOURCE, namespace)
        with mock.patch.object(namespace["_ns_os"].path, "exists", return_value=False):
            with self.assertRaises(namespace["_NamespaceRefusal"]) as caught:
                namespace["_ns_enter"]({"/target": "/missing"}, "/target/runs")
        self.assertEqual(caught.exception.code, "source-missing")
        self.assertEqual(str(caught.exception), "path_namespace refused: source missing: /missing")

    def test_remote_namespace_helper_reports_unshare_eperm_before_mount_or_mkdir(self):
        namespace = {}
        exec(G._PATH_NAMESPACE_SOURCE, namespace)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, target = root / "source", root / "target"
            (source / "runs").mkdir(parents=True)
            target.mkdir()
            rows = [(42, str(source), "nfs"), (1, "/", "rootfs")]
            libc = mock.MagicMock()
            libc.unshare.return_value = -1
            with mock.patch.dict(namespace, {"_ns_mountinfo": mock.Mock(return_value=rows)}), \
                 mock.patch.object(namespace["_ns_os"].path, "ismount", return_value=True), \
                 mock.patch.object(namespace["_ns_ctypes"], "CDLL", return_value=libc), \
                 mock.patch.object(namespace["_ns_ctypes"], "get_errno", return_value=1):
                with self.assertRaises(namespace["_NamespaceRefusal"]) as caught:
                    namespace["_ns_enter"]({str(target): str(source)}, str(target / "runs"))
            self.assertEqual(caught.exception.code, "unshare")
            self.assertEqual(str(caught.exception),
                             "path_namespace refused: unshare failed: EPERM (1)")
            libc.mount.assert_not_called()
            self.assertFalse((source / "runs" / "fixture").exists())

    def test_remote_namespace_helper_refuses_source_that_is_not_a_mount(self):
        namespace = {}
        exec(G._PATH_NAMESPACE_SOURCE, namespace)
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source"
            source.mkdir()
            with mock.patch.object(namespace["_ns_os"].path, "ismount", return_value=False):
                with self.assertRaises(namespace["_NamespaceRefusal"]) as caught:
                    namespace["_ns_enter"]({"/target": str(source)}, "/target/runs")
        self.assertEqual(caught.exception.code, "source-not-mount")
        self.assertEqual(str(caught.exception),
                         "path_namespace refused: source is not a mount point: " + str(source))

    def test_namespaced_runner_source_compiles_with_refusal_record_path(self):
        with tempfile.TemporaryDirectory() as td:
            captured = {}
            options = {"state_path": str(Path(td) / "leases.json"),
                       "observation": {"reachable": True, "gpus": []},
                       "requested": "", "share": False, "owner": None,
                       "task": "fixture", "run_id": "fixture-run",
                       "run_dir": "/target/runs/fixture-run",
                       "inner": "true", "cwd": None,
                       "tmux_environment": {"HEARTING_COMPUTE_RUN_ID": "fixture-run"},
                       "path_namespace": {"/target": "/source"},
                       "run_root": "/target/runs"}
            def popen(command, **_kwargs):
                captured["runner_path"] = command[1]
            with mock.patch.object(G, "_ns_namespace_creation"), \
                 mock.patch.object(G.subprocess, "call", return_value=1), \
                 mock.patch.object(G.subprocess, "Popen", side_effect=popen):
                G.launch_compute_namespaced(options, G.serialized_payload_source())
            runner = Path(captured["runner_path"]).read_text()
            compile(runner, "namespace-runner", "exec")
            self.assertIn("_ns_record_refusal(safe_fd, exc)", runner)
            self.assertIn('b"125\\n"', runner)
            self.assertIn("_NamespaceRefusal('runner', 'runner failed: '", runner)
            self.assertTrue(runner.startswith("import os as _runner_os; _runner_os.unlink(__file__)"))

    def test_generated_runner_executes_named_arguments_and_unlinks_itself(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "state" / "leases.json"
            lease = {"token": "lease-token", "pid": 77, "gpus": ["0"]}
            options = {"state_path": str(state_path),
                       "observation": {"reachable": True, "gpus": []},
                       "requested": "0", "share": False, "owner": None,
                       "task": "fixture", "run_id": "fixture-run",
                       "run_dir": "/target/runs/fixture-run", "inner": "echo inner",
                       "cwd": "/work", "tmux_environment": {"FIXTURE": "yes"},
                       "path_namespace": {"/target": "/lab"},
                       "run_root": "/target/runs"}
            payload_source = "\n".join([
                "events = []",
                "class _NamespaceRefusal(Exception):",
                "    def __init__(self, code, message):",
                "        super().__init__(message); self.code = code",
                "def _ns_safe_run_fd(*args):",
                "    events.append(('safe_run', args))",
                "    if mode == 'refusal': raise _NamespaceRefusal('source-missing', 'path_namespace refused: source missing: /lab')",
                "    if mode == 'error': raise RuntimeError('injected runner error')",
                "    return 31",
                "def _ns_enter(*args): events.append(('enter', args)); return 32, 33",
                "def _ns_record_refusal(fd, error): events.append(('record', fd, error.code))",
                "def payload(*args): events.append(('payload', args)); return 0",
                "def release(*args): events.append(('release', args))",
                "class _ns_os:",
                "    @staticmethod",
                "    def close(fd): events.append(('close', fd))",
            ])
            for mode in ("success", "refusal", "error"):
                captured = {}
                def run_tmux(_operation, argv, _environment):
                    if "new-session" in argv:
                        captured["argv"] = argv
                        captured["runner"] = Path(shlex.split(argv[-1])[-1])
                bound = mock.MagicMock()
                bound.__enter__.return_value = {"leases": {}}
                with mock.patch.object(G, "acquire", return_value=lease), \
                     mock.patch.object(G, "release") as launcher_release, \
                     mock.patch.object(G, "_ns_namespace_creation"), \
                     mock.patch.object(G, "_PATH_NAMESPACE_SOURCE", ""), \
                     mock.patch.object(G.subprocess, "call", return_value=0), \
                     mock.patch.object(G, "_run_tmux_launch", side_effect=run_tmux), \
                     mock.patch.object(G, "locked", return_value=bound), \
                     mock.patch.object(G.time, "monotonic", side_effect=[0, 0]):
                    G.launch_compute_namespaced(options, payload_source)
                runner_path = captured["runner"]
                self.assertEqual(runner_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(runner_path.parent.stat().st_mode & 0o777, 0o700)
                argv_text = "\0".join(captured["argv"])
                self.assertLess(len(argv_text), 2048)
                self.assertNotIn("_ns_safe_run_fd", argv_text)
                self.assertNotIn(payload_source, argv_text)
                runner = runner_path.read_text()
                self.assertTrue(runner.startswith("import os as _runner_os; _runner_os.unlink(__file__)"))
                events = []
                namespace = {"__file__": str(runner_path), "mode": mode, "events": events}
                with self.assertRaises(SystemExit) as result:
                    exec(compile(runner, str(runner_path), "exec"), namespace)
                events = namespace["events"]
                if mode == "success":
                    self.assertEqual(result.exception.code, 0)
                    self.assertIn(("safe_run", ({"/target": "/lab"}, "/target/runs",
                                                  "/target/runs/fixture-run")), events)
                    self.assertIn(("enter", ({"/target": "/lab"}, "/target/runs", "/work",
                                               "/target/runs/fixture-run")), events)
                    self.assertIn(("payload", (str(state_path), lease, "echo inner")), events)
                else:
                    self.assertEqual(result.exception.code, 125)
                    self.assertEqual([event for event in events if event[0] == "payload"], [])
                    self.assertEqual([event for event in events if event[0] == "release"],
                                     [("release", (str(state_path), lease))])
                self.assertFalse(runner_path.exists())
                launcher_release.assert_not_called()

    def test_runner_file_is_outside_targets_bounded_and_removed_on_launch_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "local" / "state.json"
            options = {"state_path": str(state_path),
                       "observation": {"reachable": True, "gpus": []},
                       "requested": "", "task": "fixture", "run_id": "bounded",
                       "run_dir": "/mnt/lab/runs/bounded", "inner": "true", "cwd": None,
                       "tmux_environment": {"FIXTURE": "yes"},
                       "path_namespace": {"/mnt/lab": "/volume/lab"},
                       "run_root": "/mnt/lab/runs"}
            captured = {}
            def run_tmux(_operation, argv, _environment):
                if "new-session" in argv:
                    captured["argv"] = argv
                    captured["runner"] = Path(shlex.split(argv[-1])[-1])
                    raise OSError("tmux launch failed")
            with mock.patch.object(G, "acquire", return_value=None), \
                 mock.patch.object(G, "_ns_namespace_creation"), \
                 mock.patch.object(G, "_PATH_NAMESPACE_SOURCE", "x" * 200000), \
                 mock.patch.object(G.subprocess, "call", return_value=0), \
                 mock.patch.object(G, "_run_tmux_launch", side_effect=run_tmux):
                with self.assertRaisesRegex(OSError, "tmux launch failed"):
                    G.launch_compute_namespaced(options, "payload" * 10000)
            argv_text = "\0".join(captured["argv"])
            self.assertLess(len(argv_text), 2048)
            self.assertNotIn("x" * 100, argv_text)
            runner_path = captured["runner"]
            self.assertFalse(runner_path.exists())
            self.assertFalse((root / "volume").exists())

            target = root / "target"
            target.mkdir()
            inside = dict(options, state_path=str(target / "leases.json"),
                          path_namespace={str(target): "/volume/lab"},
                          run_root=str(target / "runs"), run_id="inside-target")
            with mock.patch.object(G, "acquire", return_value=None), \
                 mock.patch.object(G, "_ns_namespace_creation"), \
                 mock.patch.object(G.subprocess, "call", return_value=0), \
                 mock.patch.object(G, "_run_tmux_launch") as start:
                with self.assertRaisesRegex(G.GPUUnavailable, "inside a path_namespace target"):
                    G.launch_compute_namespaced(inside, "payload")
            start.assert_not_called()
            self.assertFalse((target / "compute-runners").exists())

            runner_dir = root / "local" / "compute-runners"
            runner_dir.mkdir(parents=True, exist_ok=True)
            collision = runner_dir / "bounded.py"
            collision.write_text("owned by another launch\n")
            with mock.patch.object(G, "acquire", return_value=None), \
                 mock.patch.object(G, "_ns_namespace_creation"), \
                 mock.patch.object(G.subprocess, "call", return_value=0), \
                 mock.patch.object(G, "_run_tmux_launch"):
                with self.assertRaises(FileExistsError):
                    G.launch_compute_namespaced(options, "payload")
            self.assertEqual(collision.read_text(), "owned by another launch\n")

    def test_mountinfo_chooses_top_stacked_mount_and_namespace_ids_are_not_rechecked(self):
        namespace = {}
        exec(G._PATH_NAMESPACE_SOURCE, namespace)
        rows = [(10, "/", "rootfs"), (54, "/mnt/nas", "lab"),
                (88, "/mnt/nas", "company")]
        self.assertEqual(namespace["_ns_mount_id"]("/mnt/nas", rows), 88)
        # Mount IDs are namespace-local. _ns_enter consumes the outside snapshot
        # once and validates the bound paths by their captured inode identities.
        self.assertNotIn("_ns_mount_id(source, rows_after)", G._PATH_NAMESPACE_SOURCE)

    def test_namespace_identity_checks_compare_run_root_and_bound_target(self):
        namespace = {}
        exec(G._PATH_NAMESPACE_SOURCE, namespace)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, target = root / "source", root / "target"
            (source / "runs").mkdir(parents=True)
            (target / "runs").mkdir(parents=True)
            rows = [(42, str(source), "nfs"), (1, "/", "rootfs")]
            libc = mock.MagicMock()
            libc.unshare.return_value = 0
            libc.mount.return_value = 0
            original_stat = os.stat
            calls = {"target": 0, "run_root": 0}
            def fake_stat(path, *args, **kwargs):
                value = original_stat(path, *args, **kwargs)
                if path == str(target):
                    calls["target"] += 1
                    if calls["target"] > 1 and mismatch == "target":
                        return SimpleNamespace(st_dev=value.st_dev + 1, st_ino=value.st_ino,
                                               st_mode=value.st_mode)
                if path == str(target / "runs") and mismatch.startswith("run_root"):
                    calls["run_root"] += 1
                    if calls["run_root"] > 1:
                        return SimpleNamespace(st_dev=value.st_dev + (mismatch == "run_root_dev"),
                                               st_ino=value.st_ino + (mismatch == "run_root_ino"),
                                               st_mode=value.st_mode)
                return value
            for mismatch in ("run_root_dev", "run_root_ino", "target"):
                calls.update(target=0, run_root=0)
                with mock.patch.dict(namespace, {"_ns_mountinfo": mock.Mock(return_value=rows),
                                                   "open": mock.mock_open()}), \
                     mock.patch.object(namespace["_ns_os"].path, "ismount", return_value=True), \
                     mock.patch.object(namespace["_ns_ctypes"], "CDLL", return_value=libc), \
                     mock.patch.object(namespace["_ns_os"], "stat", side_effect=fake_stat), \
                     mock.patch.object(namespace["_ns_os"], "chdir"):
                    with self.assertRaises(namespace["_NamespaceRefusal"]) as caught:
                        namespace["_ns_enter"]({str(target): str(source)}, str(target / "runs"))
                self.assertEqual(caught.exception.code, "identity-mismatch")

    def test_new_mount_ids_after_unshare_do_not_refuse_matching_identities(self):
        namespace = {}
        exec(G._PATH_NAMESPACE_SOURCE, namespace)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source, target = base / "source", base / "target"
            (source / "runs").mkdir(parents=True)
            (target / "runs").mkdir(parents=True)
            real_stat = os.stat
            source_stat, runs_stat = real_stat(source), real_stat(source / "runs")
            rows_before = [(54, str(source), "lab"), (1, "/", "rootfs")]
            rows_after = [(2150, str(source), "lab"), (1364, "/", "rootfs")]
            mountinfo = mock.Mock(side_effect=[rows_before, rows_after])
            libc = mock.MagicMock()
            libc.unshare.return_value = 0
            libc.mount.return_value = 0
            def fake_stat(path, *args, **kwargs):
                if path == str(target): return source_stat
                if path == str(target / "runs"): return runs_stat
                return real_stat(path, *args, **kwargs)
            with mock.patch.dict(namespace, {"_ns_mountinfo": mountinfo, "open": mock.mock_open()}), \
                 mock.patch.object(namespace["_ns_os"].path, "ismount", return_value=True), \
                 mock.patch.object(namespace["_ns_ctypes"], "CDLL", return_value=libc), \
                 mock.patch.object(namespace["_ns_os"], "stat", side_effect=fake_stat), \
                 mock.patch.object(namespace["_ns_os"], "chdir"):
                root_fd, run_fd = namespace["_ns_enter"](
                    {str(target): str(source)}, str(target / "runs"), "/")
            self.assertGreaterEqual(root_fd, 0)
            self.assertIsNone(run_fd)
            namespace["_ns_os"].close(root_fd)
            self.assertEqual(mountinfo.call_count, 1)

    def test_namespaced_launch_restores_wrapper_wait_and_input_validation(self):
        with tempfile.TemporaryDirectory() as td:
            options = {"state_path": str(Path(td) / "state"),
                       "observation": {"reachable": True, "gpus": []},
                       "requested": "0", "task": "wait", "run_id": "wait-run",
                       "run_dir": str(Path(td) / "run"), "inner": "true",
                       "path_namespace": {"/target": "/source"},
                       "run_root": "/target/runs"}
            lease = {"token": "token", "pid": 77, "gpus": ["0"]}
            context = mock.MagicMock()
            context.__enter__.return_value = {"leases": {"token": lease}}
            with mock.patch.object(G, "acquire", return_value=lease), \
                 mock.patch.object(G, "release") as release, \
                 mock.patch.object(G, "_ns_namespace_creation"), \
                 mock.patch.object(G.subprocess, "call", return_value=1), \
                 mock.patch.object(G.subprocess, "Popen") as popen_mock, \
                 mock.patch.object(G, "locked", return_value=context), \
                 mock.patch.object(G.time, "monotonic", side_effect=[0, 6]):
                with self.assertRaisesRegex(G.GPUUnavailable, "did not establish its wrapper"):
                    G.launch_compute_namespaced(options, G.serialized_payload_source())
            release.assert_called_once_with(options["state_path"], lease)
            popen_mock.assert_called_once()

            bound = mock.MagicMock()
            bound.__enter__.return_value = {"leases": {"token": {**lease, "pid": 88}}}
            with mock.patch.object(G, "acquire", return_value=lease), \
                 mock.patch.object(G, "_ns_namespace_creation"), \
                 mock.patch.object(G.subprocess, "call", return_value=1), \
                 mock.patch.object(G.subprocess, "Popen"), \
                 mock.patch.object(G, "locked", return_value=bound), \
                 mock.patch.object(G.time, "monotonic", side_effect=[0, 1]):
                result = G.launch_compute_namespaced(options, G.serialized_payload_source())
            self.assertEqual(result["gpu_lease"], lease)

            # Child failure is synchronous and happens before either transport.
            for lease_value in (None, lease):
                run_dir = Path(td) / ("cpu" if lease_value is None else "gpu")
                child_options = dict(options, run_dir=str(run_dir),
                                     requested="" if lease_value is None else "0")
                with mock.patch.object(G, "acquire", return_value=lease_value), \
                     mock.patch.object(G, "release") as release_child, \
                     mock.patch.object(G, "_ns_namespace_creation",
                                       side_effect=G.GPUUnavailable("creation child failed: missing receipt")), \
                     mock.patch.object(G.subprocess, "call") as tmux_check, \
                     mock.patch.object(G.subprocess, "Popen") as child_popen:
                    with self.assertRaisesRegex(G.GPUUnavailable, "missing receipt"):
                        G.launch_compute_namespaced(child_options, G.serialized_payload_source())
                    release_child.assert_called_once_with(child_options["state_path"], lease_value)
                    tmux_check.assert_not_called()
                    child_popen.assert_not_called()
                    self.assertFalse(run_dir.exists())

            for key, value in (("tmux_environment", []), ("tmux_environment", None),
                               ("cwd", "relative"), ("cwd", "")):
                bad = dict(options, **{key: value})
                with mock.patch.object(G, "acquire", return_value=None), \
                     mock.patch.object(G, "release") as release_bad, \
                     mock.patch.object(G, "_ns_namespace_creation") as creation:
                    with self.assertRaises(G.GPUUnavailable):
                        G.launch_compute_namespaced(bad, G.serialized_payload_source())
                    release_bad.assert_called_once()
                    creation.assert_not_called()

    def test_refusal_record_is_descriptor_relative_exclusive_and_ordered(self):
        namespace = {}
        exec(G._PATH_NAMESPACE_SOURCE, namespace)
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run"
            run.mkdir()
            fd = os.open(run, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, fd)
            error = namespace["_NamespaceRefusal"]("unshare", "unshare failed: EPERM (1)")
            namespace["_ns_record_refusal"](fd, error)
            self.assertEqual((run / "stop_reason").read_text(), "path-namespace-unshare\n")
            self.assertEqual((run / "exit_code").read_text(), "125\n")
            (run / "natural").write_text("0\n")
            with self.assertRaises(FileExistsError):
                namespace["_ns_os"].open("exit_code", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                           0o600, dir_fd=fd)
            (run / "exit_code").unlink()
            (run / "exit_code").symlink_to("natural")
            with self.assertRaises(OSError):
                namespace["_ns_record_refusal"](fd, error)
            self.assertEqual((run / "natural").read_text(), "0\n")

    def test_private_bind_proc_map_and_chdir_failures_are_closed(self):
        namespace = {}
        exec(G._PATH_NAMESPACE_SOURCE, namespace)
        failures = [("private-mount", "private"), ("bind-mount", "bind"),
                    ("setgroups", "setgroups"), ("uid-map", "uid_map"),
                    ("gid-map", "gid_map"), ("cwd", "chdir")]
        for expected, failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                source, target = base / "source", base / "target"
                (source / "runs").mkdir(parents=True)
                (target / "runs").mkdir(parents=True)
                real_stat = os.stat
                source_stat = real_stat(source)
                source_run_stat = real_stat(source / "runs")
                rows = [(41, str(source), "nfs"), (1, "/", "rootfs")]
                libc = mock.MagicMock()
                libc.unshare.return_value = 0
                mount_calls = {"n": 0}
                def mount(*_args):
                    mount_calls["n"] += 1
                    if failure == "private" and mount_calls["n"] == 1:
                        return -1
                    if failure == "bind" and mount_calls["n"] == 2:
                        return -1
                    return 0
                libc.mount.side_effect = mount
                writes = []
                class FakeFile(io.StringIO):
                    def __init__(self, path):
                        super().__init__()
                        self.name = path
                    def __enter__(self): return self
                    def __exit__(self, *_args): return False
                    def write(self, content):
                        writes.append((self.name, content))
                        if failure == self.name.rsplit("/", 1)[-1]:
                            raise PermissionError(1, "denied")
                        return len(content)
                def fake_open(path, mode="r", **_kwargs):
                    if path.startswith("/proc/self/"):
                        if failure == path.rsplit("/", 1)[-1]:
                            raise PermissionError(1, "denied")
                        return FakeFile(path)
                    return __builtins__["open"](path, mode)
                def fake_stat(path, *args, **kwargs):
                    if path == str(target): return source_stat
                    if path == str(target / "runs"): return source_run_stat
                    return real_stat(path, *args, **kwargs)
                def chdir(_path):
                    if failure == "chdir": raise OSError(1, "denied")
                with mock.patch.dict(namespace, {"_ns_mountinfo": mock.Mock(return_value=rows),
                                                   "open": fake_open}), \
                     mock.patch.object(namespace["_ns_os"].path, "ismount", return_value=True), \
                     mock.patch.object(namespace["_ns_ctypes"], "CDLL", return_value=libc), \
                     mock.patch.object(namespace["_ns_ctypes"], "get_errno", return_value=1), \
                     mock.patch.object(namespace["_ns_os"], "stat", side_effect=fake_stat), \
                     mock.patch.object(namespace["_ns_os"], "chdir", side_effect=chdir):
                    with self.assertRaises(namespace["_NamespaceRefusal"]) as caught:
                        namespace["_ns_enter"]({str(target): str(source)}, str(target / "runs"), "/cwd")
                self.assertEqual(caught.exception.code, expected)
                self.assertFalse((source / "runs" / "new-run").exists())

    def test_creation_child_waits_for_check_before_mkdir_and_reports_failure(self):
        namespace = {}
        exec(G._PATH_NAMESPACE_SOURCE, namespace)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = root / "run"
            checked = root / "checked"
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            def enter(*_args):
                checked.write_text("verified")
                return os.dup(fd), None
            with mock.patch.dict(namespace, {"_ns_enter": enter}):
                namespace["_ns_namespace_creation"]({"/target": "/source"},
                                                      "/target/runs", str(run_dir), None)
            self.assertEqual(checked.read_text(), "verified")
            self.assertTrue(run_dir.is_dir())

            failed_dir = root / "failed"
            def refuse(*_args):
                raise namespace["_NamespaceRefusal"]("unshare", "unshare failed: EPERM (1)")
            with mock.patch.dict(namespace, {"_ns_enter": refuse}):
                with self.assertRaises(namespace["_NamespaceRefusal"]) as caught:
                    namespace["_ns_namespace_creation"]({"/target": "/source"},
                                                          "/target/runs", str(failed_dir), None)
            self.assertEqual(caught.exception.code, "unshare")
            self.assertEqual(str(caught.exception),
                             "path_namespace refused: unshare failed: EPERM (1)")
            self.assertFalse(failed_dir.exists())
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
