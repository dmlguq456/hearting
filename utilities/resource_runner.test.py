#!/usr/bin/env python3
"""CLI regression tests for the detached resource authorization boundary."""
import contextlib
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
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CLEAN_ENV = {k: v for k, v in os.environ.items() if not (
    k in {"AGENT_HOME", "AGENT_DISPATCH_JOBS"}
    or k.startswith("AGENT_DISPATCH_") or k.startswith("AGENT_ROUTE_")
    or k.startswith("AGENT_ARTIFACT_")
)}
CLEAN_ENV["AGENT_HOME"] = str(ROOT)
CLEAN_ENV["CUDA_VISIBLE_DEVICES"] = ""  # authorization/lifecycle fixtures run CPU payloads
RUNNER = ROOT / "utilities" / "resource-runner.py"
ROUTER = ROOT / "utilities" / "capability-route.py"
SMOKE = ROOT / "tools" / "smoke-attestation.py"
spec = importlib.util.spec_from_file_location("runner", RUNNER)
R = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(R)


class TestProgressLaunch(unittest.TestCase):
    def test_main_assigns_one_path_and_replaces_inherited_run_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, log = root / "runs.json", root / "eval.log"
            route = root / "route.json"
            route.write_text(json.dumps({"capability": "autopilot-lab", "artifact_root": str(root),
                "nodes": [{"id": "eval-run", "kind": "resource-runner", "resource_transport": "detached-process"}]}))
            payload = (f"import sys, time; sys.path.insert(0, {str(ROOT / 'utilities')!r}); "
                       "from resource_progress import write_progress; assert write_progress(1, 'item'); time.sleep(.2)")
            children = []
            real_popen = subprocess.Popen
            def spawn(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child
            with mock.patch.object(R.subprocess, "run"), mock.patch.object(R, "register_registry"), \
                 mock.patch.object(R.subprocess, "Popen", side_effect=spawn), mock.patch("sys.stdout"), \
                 mock.patch.dict(os.environ, {"AGENT_RESOURCE_PROGRESS_FILE": str(root / "foreign.json"),
                                             "AGENT_RESOURCE_RUN_ID": "foreign", "AGENT_RESOURCE_NODE": "other"}):
                R.main(["--registry", str(registry), "start", "--run-id", "auto", "--cwd", str(root),
                        "--log", str(log), "--route", str(route), "--node", "eval-run",
                        "--smoke-attestation", str(root / "smoke.json"), "--", sys.executable, "-c", payload])
                children[0].wait(timeout=5)
            row = json.loads(registry.read_text())["runs"]["auto"]
            self.assertEqual(row["progress_file"], str(log) + ".progress.json")
            counter = json.loads(Path(row["progress_file"]).read_text())
            self.assertEqual((counter["run_id"], counter["node"], counter["completed"]), ("auto", "eval-run", 1))
            self.assertFalse((root / "foreign.json").exists())
            self.assertEqual(R.read_sentinel(row["sentinel"]), 0)


class TestRunner(unittest.TestCase):
    def test_verified_publication_cas_preserves_changed_or_missing_reservation(self):
        expected = {"run_id":"resume", "status":"launching", "command":["approved"], "token":"one"}
        for current in ({**expected,"command":["foreign"]}, {**expected,"token":"two"}, None):
            with self.subTest(current=current):
                data = {"schema_version":1,"runs":{} if current is None else {"resume":current}}
                self.registry.write_text(json.dumps(data))
                before = self.registry.read_bytes()
                with self.assertRaisesRegex(ValueError,"resource-reservation-changed"):
                    R.publish_verified_run(self.registry,"resume",expected,{**expected,"status":"running"})
                self.assertEqual(self.registry.read_bytes(),before)
        self.registry.write_text(json.dumps({"schema_version":1,"runs":{"resume":expected}}))
        published = {**expected,"status":"running"}
        R.publish_verified_run(self.registry,"resume",expected,published)
        self.assertEqual(json.loads(self.registry.read_text())["runs"]["resume"],published)

        jobs = self.base / "cas-jobs.log"
        jobs.write_text("")
        args = SimpleNamespace(jobs=str(jobs), run_id="resume", node="resume-run")
        route_file = self.base / "cas-route.json"
        for changed in ({**expected,"command":["foreign"]}, None):
            with self.subTest(exception_row=changed):
                self.registry.write_text(json.dumps({"schema_version":1,"runs":{}}))
                preserved = []
                def interrupt(_registry):
                    self.registry.write_text(json.dumps({"schema_version":1,
                        "runs":{} if changed is None else {"resume":changed}}))
                    preserved.append(self.registry.read_bytes())
                    raise RuntimeError("launch-interrupted")
                with mock.patch.object(R,"register_registry",side_effect=interrupt), \
                     mock.patch.object(R.subprocess,"Popen") as launch:
                    with self.assertRaisesRegex(ValueError,"resource-reservation-changed"):
                        R.start_verified(self.registry,args,{},route_file,dict(expected))
                self.assertEqual(self.registry.read_bytes(),preserved[0])
                self.assertEqual(launch.call_count,0)

    def test_owner_resource_external_watch_once_fence_and_fast_exit(self):
        import artifact_producer
        jobs = self.base / "owner-jobs.log"
        jobs.write_text("")
        route_file = self.base / "owner-route.json"
        args = SimpleNamespace(jobs=str(jobs), run_id="owner-run", node="full-run")
        placeholder = {"run_id":"owner-run", "cwd":str(self.repo), "log":str(self.log),
            "command":[sys.executable,"-c",f"import sys; sys.path.insert(0, {str(ROOT / 'utilities')!r}); from resource_progress import write_progress; assert write_progress(12, 'epoch', total=50); from pathlib import Path; Path({str(self.launch)!r}).write_text('once')"],
            "route":str(route_file), "node":"full-run", "status":"launching", "sentinel":str(self.log)+".exit",
            "parent_attempt_id":"att-parent", "progress_file":str(self.log)+".progress.json",
            "owner_wait":{"session_id":"same-native"}}
        real_popen = subprocess.Popen
        watch = mock.Mock(pid=os.getpid())
        watch.poll.return_value = None
        payloads = []
        def launch(argv, **kwargs):
            if "watch" in argv:
                os.write(kwargs["pass_fds"][0],b"ready\n")
                return watch
            proc = real_popen(argv, **kwargs)
            payloads.append(proc)
            return proc
        route = {"route_id":"rt-owner", "route_hash":"sha256:owner"}
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}), \
             mock.patch.object(artifact_producer,"prepare_route_artifact_env",return_value={"AGENT_ARTIFACT_OUTPUT_DIR":str(self.base)}), \
             mock.patch.object(R,"register_registry"), \
             mock.patch.object(R.subprocess,"run") as arm, \
             mock.patch.object(R.subprocess,"Popen",side_effect=launch), \
             mock.patch("sys.stdout"):
            R.start_verified(self.registry,args,route,route_file,dict(placeholder))
            payloads[0].wait(timeout=5)
            R.start_verified(self.registry,args,route,route_file,dict(placeholder))
        self.assertEqual(len(payloads),1)
        self.assertEqual(self.launch.read_text(),"once")
        self.assertEqual(arm.call_count,1)
        self.assertIn("--successor-external",arm.call_args.args[0])
        self.assertNotIn("--successor-command",arm.call_args.args[0])
        row = json.loads(self.registry.read_text())["runs"]["owner-run"]
        self.assertEqual(row["resource_policy"],"supervised-owner")
        self.assertEqual(row["owner_wait"],placeholder["owner_wait"])
        self.assertEqual(R.read_sentinel(row["sentinel"]),0)
        progress = json.loads(Path(row["progress_file"]).read_text())
        self.assertEqual((progress["run_id"], progress["node"], progress["completed"], progress["total"]),
                         ("owner-run", "full-run", 12, 50))

    def test_codex_tool_intent_has_no_process_until_controller_admission_once(self):
        import artifact_producer
        jobs = self.base / "codex-owner-jobs.log"
        jobs.write_text("")
        route_file = self.base / "codex-owner-route.json"
        args = SimpleNamespace(jobs=str(jobs),run_id="codex-owner-run",node="full-run")
        body = {"run_id":args.run_id,"cwd":str(self.repo),"log":str(self.log),
            "command":[sys.executable,"-c",f"from pathlib import Path; import time; time.sleep(.2); Path({str(self.launch)!r}).write_text('once')"],
            "route":str(route_file),"node":"full-run","status":"launching","sentinel":str(self.log)+".exit",
            "parent_attempt_id":"att-parent","owner_wait":{"session_id":"same-native","launch_scope":"codex-owner-controller"},
            "launch_request":{"smoke_attestation":None,"smoke_attestation_sha256":None,"config_manifest":None,"config_manifest_sha256":None}}
        real_popen = subprocess.Popen
        watch = mock.Mock(pid=os.getpid())
        watch.poll.return_value=None
        payloads=[]
        def launch(argv,**kwargs):
            if "watch" in argv:
                os.write(kwargs["pass_fds"][0],b"ready\n")
                return watch
            proc=real_popen(argv,**kwargs)
            payloads.append(proc)
            return proc
        route={"route_id":"rt-codex-owner","route_hash":"sha256:owner"}
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}), \
             mock.patch.object(artifact_producer,"prepare_route_artifact_env",return_value={"AGENT_ARTIFACT_OUTPUT_DIR":str(self.base)}), \
             mock.patch.object(R,"register_registry"), mock.patch.object(R.subprocess,"run"), \
             mock.patch("sys.stdout") as stdout, mock.patch.object(R.subprocess,"Popen",side_effect=launch) as spawn:
            R.start_verified(self.registry,args,route,route_file,dict(body))
            queued=json.loads(self.registry.read_text())["runs"][args.run_id]
            receipt=json.loads(stdout.write.call_args_list[-2].args[0])
            self.assertEqual(queued["launch_state"],"queued")
            self.assertNotIn("pid",queued)
            self.assertIs(receipt["payload_spawned"],False)
            self.assertIs(receipt["supervisor_alive"],False)
            self.assertEqual(spawn.call_count,0)
            self.assertFalse(self.launch.exists())
            R.start_verified(self.registry,args,route,route_file,dict(body))
            self.assertEqual(spawn.call_count,0)
            identity={**R.proc_identity(os.getpid()),"pid_namespace":os.readlink("/proc/self/ns/pid")}
            admission=SimpleNamespace(expected=queued,identity=identity,command=body["command"],
                sandbox={"mode":"workspace-write","enforcement":"os-sandbox"},guard=contextlib.nullcontext)
            R.start_verified(self.registry,args,route,route_file,dict(body),controller=admission)
            running=json.loads(self.registry.read_text())["runs"][args.run_id]
            self.assertEqual(running["pid_namespace"],os.readlink("/proc/self/ns/pid"))
            self.assertEqual(R.classify_identity(running)[0],"working")
            self.assertEqual(running["launch_controller"],identity)
            self.assertEqual(running["launch_state"],"started")
            self.assertFalse(self.launch.exists())
            self.assertIs(admission.children[0],payloads[0])
            self.assertIs(admission.children[1],watch)
            self.assertEqual(admission.row,running)
            payloads[0].wait(timeout=5)
            self.assertEqual(R.classify_identity(running)[0],"exited")
            self.assertEqual(self.launch.read_text(),"once")
            self.assertEqual(R.read_sentinel(running["sentinel"]),0)
            before=self.registry.read_bytes()
            with self.assertRaisesRegex(ValueError,"resource-reservation-changed"):
                R.start_verified(self.registry,args,route,route_file,dict(body),controller=admission)
            self.assertEqual(self.registry.read_bytes(),before)
            self.assertEqual(len(payloads),1)
            foreign={**running,"pid_namespace":"pid:[foreign]"}
            self.assertEqual(R.classify_identity(foreign)[0],"stale")
            self.assertEqual(R.classify_identity(foreign)[2],"process-namespace-mismatch")

    def test_gpu_launch_refusal_preserves_failure_and_cpu_bridge_retry_starts_once(self):
        import artifact_producer
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                jobs = self.base / (harness + "-jobs.log")
                jobs.write_text("")
                registry = self.base / (harness + "-runs.json")
                route_file = self.base / (harness + "-route.json")
                route = {"route_id": "rt-gpu-refusal-" + harness, "route_hash": "sha256:" + harness,
                         "nodes": [{"id": "full-run", "resource_class": "gpu"}]}
                args = SimpleNamespace(jobs=str(jobs), run_id="bridge", node="full-run")
                log = self.base / (harness + "-failed.log")
                marker = self.base / (harness + "-payload")
                command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('once')"]
                owner = {"session_id": "same-native"}
                if harness == "codex":
                    owner["launch_scope"] = "codex-owner-controller"
                body = {"run_id": "bridge", "cwd": str(self.repo), "log": str(log), "command": command,
                        "route": str(route_file), "node": "full-run", "status": "launching",
                        "sentinel": str(log) + ".exit", "parent_attempt_id": "att-parent", "owner_wait": owner}
                if harness == "codex":
                    body["launch_request"] = {}
                def controller(expected):
                    return SimpleNamespace(expected=expected, identity={**R.proc_identity(os.getpid()),
                        "pid_namespace": os.readlink("/proc/self/ns/pid")},
                        command=expected["command"], sandbox={}, guard=contextlib.nullcontext)
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}), \
                     mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                                       return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.base)}), \
                     mock.patch.object(R, "register_registry"), mock.patch.object(R.subprocess, "run"), \
                     mock.patch("sys.stdout"), mock.patch.object(R.subprocess, "Popen") as spawn:
                    admission = None
                    if harness == "codex":
                        R.start_verified(registry, args, route, route_file, dict(body))
                        queued = json.loads(registry.read_text())["runs"]["bridge"]
                        admission = controller(queued)
                    with mock.patch.object(R.gpu_leases, "resource_admission",
                                           side_effect=R.gpu_leases.GPUUnavailable("Local GPU admission: in use")):
                        with self.assertRaises(R.gpu_leases.GPUUnavailable):
                            R.start_verified(registry, args, route, route_file, dict(body), controller=admission)
                    spawn.assert_not_called()
                failed = json.loads(registry.read_text())["runs"]["bridge"]
                self.assertEqual(failed["launch_state"], "not-started")
                self.assertEqual(failed["launch_error"], {"type": "GPUUnavailable", "message": "Local GPU admission: in use"})
                self.assertTrue(R.resource_never_started(failed))
                self.assertFalse(marker.exists())
                # The existing empty CUDA spelling describes a CPU control
                # bridge; target-host compute admission remains its own step.
                args.run_id = "bridge__a1"
                retry_log = self.base / (harness + "-retry.log")
                retry = {**body, "run_id": args.run_id, "log": str(retry_log),
                    "sentinel": str(retry_log) + ".exit", "command": ["env", "CUDA_VISIBLE_DEVICES=", *command]}
                real_popen = subprocess.Popen
                watch = mock.Mock(pid=os.getpid())
                watch.poll.return_value = None
                payloads = []
                def launch(argv, **kwargs):
                    if "watch" in argv:
                        os.write(kwargs["pass_fds"][0], b"ready\n")
                        return watch
                    proc = real_popen(argv, **kwargs)
                    payloads.append(proc)
                    return proc
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}), \
                     mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                                       return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.base)}), \
                     mock.patch.object(R, "register_registry"), mock.patch.object(R.subprocess, "run"), \
                     mock.patch("sys.stdout"), mock.patch.object(R.subprocess, "Popen", side_effect=launch), \
                     mock.patch.object(R.gpu_leases, "local_observation") as probe:
                    R.start_verified(registry, args, route, route_file, dict(retry))
                    if harness == "codex":
                        queued = json.loads(registry.read_text())["runs"][args.run_id]
                        R.start_verified(registry, args, route, route_file, dict(retry), controller=controller(queued))
                    payloads[0].wait(timeout=5)
                    R.start_verified(registry, args, route, route_file, dict(retry))
                    probe.assert_not_called()
                runs = json.loads(registry.read_text())["runs"]
                self.assertEqual(runs["bridge"], failed)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(marker.read_text(), "once")
                self.assertEqual(R.read_sentinel(runs[args.run_id]["sentinel"]), 0)

    def test_controller_correction_before_release_preserves_queued_body_and_payload_zero(self):
        import artifact_producer
        jobs=self.base/"correction-jobs.log"; jobs.write_text("")
        route_file=self.base/"correction-route.json"
        args=SimpleNamespace(jobs=str(jobs),run_id="correction-run",node="full-run")
        row={"run_id":args.run_id,"cwd":str(self.repo),"log":str(self.log),"command":[sys.executable,"-c",f"open({str(self.launch)!r},'w').write('wrong')"],
             "route":str(route_file),"jobs":str(jobs),"node":"full-run","status":"launching","sentinel":str(self.log)+".exit",
             "parent_attempt_id":"att-parent","resource_policy":"supervised-owner","launch_state":"queued",
             "owner_wait":{"session_id":"same-native","launch_scope":"codex-owner-controller"},"launch_request":{}}
        self.registry.write_text(json.dumps({"schema_version":1,"runs":{args.run_id:row}}))
        checks=[]
        @contextlib.contextmanager
        def guard():
            checks.append("check")
            if len(checks)==2:
                raise R.LaunchDeferred("correction-pending")
            yield
        watch=mock.Mock(pid=os.getpid()); watch.poll.return_value=None
        real_popen=subprocess.Popen
        payloads=[]
        def launch(argv,**kwargs):
            if "watch" in argv:
                os.write(kwargs["pass_fds"][0],b"ready\n"); return watch
            proc=real_popen(argv,**kwargs); payloads.append(proc); return proc
        admission=SimpleNamespace(expected=row,identity={"pid_namespace":os.readlink("/proc/self/ns/pid")},
            command=row["command"],sandbox={"mode":"workspace-write"},guard=guard)
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}), \
             mock.patch.object(artifact_producer,"prepare_route_artifact_env",return_value={"AGENT_ARTIFACT_OUTPUT_DIR":str(self.base)}), \
             mock.patch.object(R,"register_registry"),mock.patch.object(R.subprocess,"run"), \
             mock.patch.object(R.subprocess,"Popen",side_effect=launch):
            with self.assertRaises(R.LaunchDeferred):
                R.start_verified(self.registry,args,{"route_id":"rt-correction","route_hash":"sha256:c"},route_file,dict(row),controller=admission)
        self.assertEqual(json.loads(self.registry.read_text())["runs"][args.run_id],row)
        self.assertEqual(payloads[0].returncode,125)
        self.assertFalse(self.launch.exists())
        self.assertFalse(Path(row["sentinel"]).exists())
        watch.terminate.assert_called_once()

    def test_owner_start_derives_existing_native_tuple_and_refuses_foreign_or_unknown_before_spawn(self):
        import dispatch_resource_wait as OWNER_RESOURCE
        import owner_route_binding as OWNER
        import dispatch_contract as CONTRACT
        import dispatch_owner_input as INPUT
        join = OWNER_RESOURCE.JOIN
        jobs = self.base / "binding-state/jobs.log"
        jobs.parent.mkdir()
        jobs.write_text("time\topen\t/repo\t/wt\towner\tattempt_id=att-parent,attempt_schema_version=2,worker_type=owner,dispatch_depth=1\n")
        state = CONTRACT.dispatch_state_root(jobs) / "supervisor-state/att-parent.json"
        join.write_supervisor_state(state,"att-parent",set(),phase="running-turn")
        args = SimpleNamespace(jobs=None,parent_attempt_id=None)
        route_file = self.base / "bound-route.json"
        route = {"route_id":"rt-bound","route_hash":"sha256:bound"}
        binding = SimpleNamespace(route_file=str(route_file),route_id="rt-bound",route_hash="sha256:bound")
        native = {"thread_id":"same-native","supervisor_live":True}
        env = {"AGENT_DISPATCH_COMPLETION_MODE":"supervised", "AGENT_DISPATCH_ATTEMPT_ID":"att-parent",
               "AGENT_OWNER_ROUTE_FILE":str(route_file),"AGENT_DISPATCH_JOBS":str(jobs),
               "AGENT_DISPATCH_COMPLETION_STATE_FILE":str(state)}
        with mock.patch.object(OWNER,"resolve_owner_route_lifecycle",return_value=(binding,"bound")), \
             mock.patch.object(CONTRACT,"resolve_live_parent_attempt",return_value=SimpleNamespace(pid=444,pid_start="200")) as live, \
             mock.patch.object(INPUT,"inspect",return_value=native), \
             mock.patch.object(R.subprocess,"Popen") as spawn:
            actual = OWNER_RESOURCE.start_binding(route,route_file,args,env)
            self.assertEqual(actual,{"parent_attempt_id":"att-parent","session_id":"same-native",
                "route_id":"rt-bound","route_hash":"sha256:bound","jobs":str(jobs),"owner_pid":444,"owner_start":"200"})
            self.assertEqual(args.parent_attempt_id,"att-parent")
            old_jobs=jobs.read_text()
            jobs.write_text(old_jobs.rstrip("\n")+",harness=codex\n")
            self.assertEqual(OWNER_RESOURCE.start_binding(route,route_file,args,env)["launch_scope"],"codex-owner-controller")
            jobs.write_text(old_jobs)
            self.assertEqual(live.call_args.kwargs["repo"],"/repo")
            self.assertEqual(live.call_args.kwargs["worktree"],"/wt")
            self.assertEqual(join.read_supervisor_phase_state(state,"att-parent").resource["session_id"],"same-native")
            before = state.read_bytes()
            args.parent_attempt_id="att-foreign"
            with self.assertRaisesRegex(ValueError,"resource-owner-attempt-conflict"):
                OWNER_RESOURCE.start_binding(route,route_file,args,env)
            args.parent_attempt_id="att-parent"
            native["supervisor_live"]=False
            with self.assertRaisesRegex(ValueError,"resource-owner-session-unavailable"):
                OWNER_RESOURCE.start_binding(route,route_file,args,env)
            native.update(supervisor_live=True,thread_id="different-native")
            with self.assertRaisesRegex(ValueError,"resource-owner-session-conflict"):
                OWNER_RESOURCE.start_binding(route,route_file,args,env)
            self.assertEqual(state.read_bytes(),before)
            self.assertEqual(spawn.call_count,0)
            self.assertIsNone(OWNER_RESOURCE.start_binding(route,route_file,args,{}))

    def _resume_route(self):
        evidence = self.base / "headless.json"
        evidence.write_text(json.dumps({"candidates": [{"harness": "codex", "transport": "headless",
            "surface": "registered-headless", "status": "supported", "probe_source": "fixture",
            "probe_time": "2026-10-05T00:00:00Z"}]}))
        prompt = self.base / "request.txt"
        prompt.write_text("Verify the already approved same-code/config resume after it exits.\n")
        result = subprocess.run([sys.executable, str(ROUTER), "compose", "--capability", "autopilot-lab",
            "--capability-mode", "setup", "--shape", "direct", "--graph", "resume-run,run-verify",
            "--slug", "resume-fixture", "--cwd", str(self.repo), "--artifact-root", str(self.artifacts),
            "--registered-headless-evidence", str(evidence), "--unassigned", "--prompt-file", str(prompt)],
            env=CLEAN_ENV, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        route = json.loads(result.stdout)
        self.resume_path = self.artifacts / ".runtime/routes" / (route["route_id"] + ".json")
        self.assertTrue(self.resume_path.is_file())
        self.jobs = self.base / "state/jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.write_text("")
        return json.loads(self.resume_path.read_text())

    def test_verified_resume_concurrent_exact_body_starts_once_and_fast_exit_has_identity(self):
        self._resume_route()
        # Failed payload deliberately ends before any verifier can be admitted.
        # This tests real reserve/fence/watch behavior without a model invocation.
        args = ["start", "--run-id", "resume", "--cwd", str(self.repo), "--log", str(self.log),
                "--route", str(self.resume_path), "--node", "resume-run", "--jobs", str(self.jobs),
                "--", sys.executable, "-c", f"from pathlib import Path; p=Path({str(self.launch)!r}); "
                "p.open('a').write('x'); raise SystemExit(7)"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: self.cli(*args), range(2)))
        for outcome in outcomes:
            self.assertEqual(outcome.returncode, 0, outcome.stderr)
        receipts = [json.loads(o.stdout) for o in outcomes]
        self.assertEqual(sum(r["payload_spawned"] for r in receipts), 1)
        self.assertEqual(sum(r["replayed"] for r in receipts), 1)
        started = next(r for r in receipts if r["payload_spawned"])
        self.assertTrue(started["supervision"]["starttime"])
        self.assertFalse(started["verification_admitted"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = json.loads(self.registry.read_text())["runs"]["resume"]
            if row["status"] == "failed": break
            time.sleep(.05)
        self.assertEqual(self.launch.read_text(), "x")
        self.assertEqual(row["exit_code"], 7)
        self.assertEqual(row["failure_class"], "exit-7")
        self.assertTrue(row["starttime"])
        replay = self.cli(*args)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertFalse(json.loads(replay.stdout)["payload_spawned"])
        changed = self.cli(*args[:-1], "raise SystemExit(9)")
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("resource-route-body-conflict", changed.stderr)
        different_run = list(args)
        different_run[different_run.index("--run-id")+1] = "another"
        self.assertIn("resource-route-body-conflict", self.cli(*different_run).stderr)
        self.assertEqual(self.launch.read_text(), "x")
        self.assertEqual(self.jobs.read_text(), "")

    def test_verified_resume_watch_spawn_failure_releases_no_payload(self):
        route = self._resume_route()
        args = type("Args", (), {"jobs": str(self.jobs), "node": "resume-run", "run_id": "resume"})()
        placeholder = {"run_id": "resume", "route": str(self.resume_path), "node": "resume-run",
            "cwd": str(self.repo), "log": str(self.log), "sentinel": str(self.log)+".exit",
            "command": [sys.executable, "-c", f"open({str(self.launch)!r},'w').write('wrong')"],
            "status": "launching", "workflow_state": "READY"}
        original = subprocess.Popen
        def spawn(argv, **kwargs):
            if "watch" in argv: raise OSError("synthetic watch spawn failure")
            return original(argv, **kwargs)
        with mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT), "AGENT_WORKFLOW_ROOT": str(self.base/"workflow"),
                                         "AGENT_RESOURCE_RUN_INDEX": str(self.index)}), \
             mock.patch.object(R.subprocess, "Popen", side_effect=spawn):
            with self.assertRaisesRegex(OSError, "synthetic watch"):
                R.start_verified(self.registry, args, route, self.resume_path, placeholder)
        self.assertFalse(self.launch.exists())
        self.assertEqual(json.loads(self.registry.read_text())["runs"]["resume"]["status"], "failed")
        self.assertEqual(self.jobs.read_text(), "")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Runs after (LIFO) nothing else and before the directory removal: a
        # started run still writing its log made that removal fail in CI.
        self.addCleanup(self._stop_started_runs)
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
            sys.executable, str(ROUTER), "compile", "--slug", "resource-runner-fixture",
            "--capability", "autopilot-lab",
            "--capability-mode", "setup", "--intensity", "auto", "--signal", "resource-run",
            "--cwd", str(self.repo), "--artifact-root", str(self.artifacts),
            "--dispatch-evidence", str(evidence), "--tracking", "untracked",
            "--spec-read", "not-applicable", "--drift-verdict", "no-project-spec",
            "--workflow-mode", "untracked", "--artifact-guard", "preflight-passed",
        ], text=True, capture_output=True, env=CLEAN_ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        compiled = json.loads(result.stdout)
        self.route = (
            self.artifacts / ".runtime" / "routes" / f"{compiled['route_id']}.json"
        )
        self.assertTrue(self.route.is_file())
        self.registry = self.base / "registry.json"
        self.index = self.base / "resource-runs.index.json"
        self.log = self.base / "logs" / "run.log"
        self.launch = self.base / "launched"
        self.attestation = self.base / "smoke.json"
        subprocess.run([
            sys.executable, str(SMOKE), "attest", "--input", str(self.repo / "config"),
            "--cwd", str(self.repo), "--output", str(self.attestation), "--", sys.executable, "-c", "pass",
        ], check=True, stdout=subprocess.DEVNULL)

    def _stop_started_runs(self):
        try:
            runs = json.loads(self.registry.read_text()).get("runs", {})
        except (AttributeError, OSError, ValueError):
            return
        own = os.getpgrp()
        groups = {run.get("process_group") for run in runs.values() if isinstance(run, dict)}
        for group in groups:
            try:
                group = int(group)
                if group <= 1 or group == own:
                    continue  # fixtures record this test's own group on purpose
                os.killpg(group, signal.SIGKILL)
            except (OSError, TypeError, ValueError):
                continue
            for _ in range(50):
                try:
                    os.killpg(group, 0)
                except OSError:
                    break
                time.sleep(0.02)

    def cli(self, *args, cwd=None):
        return subprocess.run([
            sys.executable, str(RUNNER), "--registry", str(self.registry), *args,
        ], cwd=cwd, text=True, capture_output=True,
           env={**CLEAN_ENV, "AGENT_HOME": str(ROOT),
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
