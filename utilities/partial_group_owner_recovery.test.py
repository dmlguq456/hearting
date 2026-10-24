#!/usr/bin/env python3
"""One isolated recovery cycle, with real reservations, wrappers and child PIDs.

Only model payloads, runtime bootstrap and backend usage observations are fake.
The test keeps route validation, reservation validation, registration, owner
correction/replacement, completion markers and the next-stage gate in force.
"""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from types import SimpleNamespace, MethodType
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_contract as DC
import dispatch_owner_input as INPUT
import dispatch_replacement as REPLACEMENT
import work_start as START
import artifact_producer as PRODUCER


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FIXTURE = load("partial_owner_fixture", "execution_access_owner_start.test.py")
OWNER = load("partial_owner_launch", "dispatch-owner.py")


class PartialGroupOwnerRecoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FIXTURE.OwnerGrantStartTest.setUpClass()

    def setUp(self):
        self.f = FIXTURE.OwnerGrantStartTest("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.stop_fixture_processes)
        self.api = self.f.route_api
        self.prompts = {}
        self.fail_gap = True
        self.env = dict(self.f.env, CLAUDE_SESSION_ID="fixture-session",
                        HEARTING_GATES="on", HEARTING_WORKFLOW_GROUP_REVIEW="off",
                        OPENCODE_DISPATCH_EARLY_EXIT_WATCH=".2",
                        AGENT_ARTIFACT_ROOT=str(self.f.artifacts),
                        AGENT_MODEL_GOVERNOR_ROOT=str(self.f.artifacts / ".runtime/model-worker-governor"))
        self.f.worker.write_text(
            "import os,pathlib,sys,time\n"
            "pathlib.Path(sys.argv[4]).write_text(pathlib.Path(sys.argv[3]).read_text())\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n")

    def stop_fixture_processes(self):
        # A timed-out batch may not have returned its rows yet. Match the
        # private fixture paths before signalling any late wrapper or watcher.
        prefix = str(self.f.root) + "/"
        for process in Path("/proc").iterdir():
            if not process.name.isdigit(): continue
            try:
                argv = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
                if any(arg.startswith(prefix) for arg in argv):
                    os.kill(int(process.name), signal.SIGTERM)
            except OSError:
                pass

    def wrapper(self, argv, env):
        f = self.f
        harness = next(h for h in f.wrappers if f"adapters/{h}/" in argv[1])
        wrapper = f.wrappers[harness]
        argsv = ["dispatch-headless.py", *argv[2:]]
        attempt = argsv[argsv.index("--attempt-id") + 1]
        node = argsv[argsv.index("--route-node") + 1] if "--route-node" in argsv else "owner"
        marker, release = f.root / (attempt + ".started"), f.root / (attempt + ".release")
        out, err = io.StringIO(), io.StringIO()

        def payload(args, prompt, *_):
            if node == "autopilot-research-retrieval-alternative" and self.fail_gap:
                return shlex.join([sys.executable, "-c",
                    "from pathlib import Path; import os; "
                    f"Path({str(marker)!r}).write_text(str(os.getpid())); "
                    "print('fixture failed retrieval alternative', flush=True); raise SystemExit(1)"])
            return shlex.join([sys.executable, str(f.worker), str(marker), str(release),
                               str(prompt), str(f.root / (attempt + ".prompt.txt"))])

        real_run = subprocess.run
        def observed_run(command, **kwargs):
            result = real_run(command, **kwargs)
            if "worker-route-guard.py" in " ".join(str(c) for c in command):
                (f.root / (attempt + ".guard.json")).write_text(json.dumps({"argv": command, "stdout": result.stdout, "stderr": result.stderr}))
            return result
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "run", side_effect=observed_run))
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(redirect_stdout(out)); stack.enter_context(redirect_stderr(err))
            stack.enter_context(mock.patch("dispatch_lifecycle.pid_namespace_evidence", return_value={
                "lifecycle_selector_source": "host-like", "lifecycle_nspid_width": "1",
                "lifecycle_pid1_class": "system-init"}))
            for name, value in (("check_runtime_projection", 0),
                                ("prepare_nested_codex_home", f.home / ".codex"),
                                ("_core_grounding_dir", f.root / "bootstrap/core")):
                if hasattr(wrapper, name):
                    stack.enter_context(mock.patch.object(wrapper, name, return_value=value))
            stack.enter_context(mock.patch.object(wrapper.shutil, "which", return_value=sys.executable))
            stack.enter_context(mock.patch.object(wrapper, "resolve_artifact_root", return_value=str(f.artifacts)))
            stack.enter_context(mock.patch.object(wrapper, "shell_command", side_effect=payload))
            stack.enter_context(mock.patch.object(wrapper, "attach_summary_owner", return_value={}))
            for name in ("launch_orphan_watch",):
                if hasattr(wrapper, name):
                    stack.enter_context(mock.patch.object(wrapper, name, return_value=os.getpid()))
            def sidecar(args, _):
                args.managed_sidecar_state, args.managed_sidecar_reason = "not-started", "-"
                args.managed_sidecar_pid = args.managed_sealed_batch_id = args.managed_sidecar_log = "-"
            stack.enter_context(mock.patch.object(wrapper, "launch_parent_completion_sidecar", side_effect=sidecar))
            rc = wrapper.main(argsv)
        if rc == 0 and "started=1" in out.getvalue():
            _, metadata = f.row(attempt)
            f.processes.append(int(metadata["pid"]))
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists(), out.getvalue() + err.getvalue())
            captured = f.root / (attempt + ".prompt.txt")
            if captured.exists(): self.prompts[attempt] = captured.read_text()
        receipt = subprocess.CompletedProcess(argv, rc, out.getvalue(), err.getvalue())
        (f.root / (attempt + ".receipt.json")).write_text(json.dumps({"stdout": receipt.stdout, "stderr": receipt.stderr}))
        return receipt

    def owner_run(self, command, **_):
        if Path(command[1]).name == "dispatch-headless.py":
            return self.wrapper(command, _.get("env", dict(os.environ)))
        out, err = io.StringIO(), io.StringIO()
        def launch(argv, env):
            result = self.wrapper([sys.executable, str(ROOT / "adapters/opencode/bin/dispatch-headless.py"), *argv[1:]], env)
            print(result.stdout, end=""); print(result.stderr, end="", file=sys.stderr)
            return result.returncode
        with redirect_stdout(out), redirect_stderr(err), \
             mock.patch.object(OWNER, "_usage", return_value={h: "ok" for h in ("claude", "codex", "opencode")}), \
             mock.patch.object(OWNER._capacity, "capacity_report", return_value={"scores": {"opencode": 100}, "sources": {}}), \
             mock.patch.object(OWNER, "run_owner_wrapper", side_effect=launch):
            rc = OWNER.main(command[2:])
        return subprocess.CompletedProcess(command, rc, out.getvalue(), err.getvalue())

    def batch(self, owner, continuation=None, prompt="original sealed request\n"):
        f = self.f
        fields, _ = f.row(owner)
        env = dict(self.env, AGENT_DISPATCH_SELF_SLUG=fields[4], AGENT_DISPATCH_ATTEMPT_ID=owner,
                   AGENT_DISPATCH_CURRENT_HARNESS="opencode", AGENT_DISPATCH_CALLER_HARNESS="opencode",
                   AGENT_DISPATCH_CURRENT_SANDBOX="adapter-default", AGENT_DISPATCH_CHILD="1")
        injection = f.root / "injection"
        injection.mkdir(exist_ok=True)
        (injection / "sitecustomize.py").write_text(
            "import os, sys, runpy, traceback\nfrom pathlib import Path\n"
            "if Path(sys.argv[0]).name == 'dispatch-batch.py':\n"
            f" sys.path.insert(0,{str(ROOT / 'utilities')!r})\n"
            " import dispatch_lifecycle\n"
            " dispatch_lifecycle.pid_namespace_evidence=lambda *a,**k: {'lifecycle_selector_source':'host-like','lifecycle_nspid_width':'1','lifecycle_pid1_class':'system-init'}\n"
            "if Path(sys.argv[0]).name == 'dispatch-headless.py':\n"
            " try:\n"
            f"  module=runpy.run_path({str(Path(__file__).resolve())!r},run_name='fixture_driver')\n"
            "  rc=module['fixture_wrapper']()\n"
            " except BaseException:\n  traceback.print_exc(); rc=1\n"
            " sys.stdout.flush(); sys.stderr.flush(); os._exit(rc)\n")
        env.update(PYTHONPATH=str(injection), HEARTING_RECOVERY_FIXTURE=str(f.root),
                   HEARTING_RECOVERY_FAIL_GAP=str(int(self.fail_gap)), CODEX_DISPATCH_EARLY_EXIT_WATCH=".2",
                   AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN="1")
        argv = ["--route", str(self.path), "--parallel-group", "autopilot-research-retrieval", "--start",
                "--jobs", str(f.jobs), "--prompt-text", prompt]
        if continuation is not None: argv += ["--continuation", str(continuation)]
        result = subprocess.run([sys.executable, str(ROOT / "utilities/dispatch-batch.py"), *argv],
                                env=env, capture_output=True, text=True, timeout=120)
        for path in f.root.glob("*.prompt.txt"):
            self.prompts[path.name.removesuffix(".prompt.txt")] = path.read_text()
        for line in f.jobs.read_text().splitlines():
            metadata = DC.parse_registry_metadata(line.split("\t")[-1])
            if metadata.get("pid") and line.split("\t")[1] in {"open", "running"}:
                pid = int(metadata["pid"])
                if pid not in f.processes: f.processes.append(pid)
        try: return result.returncode, json.loads(result.stdout.splitlines()[-1])
        except (ValueError, IndexError): self.fail(result.stdout + result.stderr)

    def finish_process(self, attempt):
        _, meta = self.f.row(attempt)
        (self.f.root / (attempt + ".release")).touch()
        pid = int(meta["pid"])
        try: os.waitpid(pid, 0)
        except ChildProcessError: pass  # Batch-spawned workers belong to its wrapper.
        self.f.processes.remove(pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, meta = self.f.row(attempt)
            if DC.attempt_process_quiescence(meta, terminal_receipt=True).state == "quiescent": return
            time.sleep(.01)
        self.fail("fixture process did not drain: " + attempt)

    def complete(self, leg):
        aid, node_id = leg["attempt_id"], leg["node"]
        self.finish_process(aid)
        record = PRODUCER.route_cycle_for(self.f.artifacts, self.route)
        output = PRODUCER.cycle_dir(self.f.artifacts, record["campaign_id"], record["cycle_id"], record) / "artifacts"
        leg = "retrieval-alternative" if node_id.endswith("-alternative") else "retrieval"
        evidence = output / f"spec/_internal/research/parts/autopilot-research/retrieval/shards/{leg}/REPORT.md"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("# Research\nPASS\n")
        node = next(n for n in self.route["nodes"] if n["id"] == node_id)
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.api.complete_node(self.route, node, node_id, evidence, jobs=self.f.jobs, attempt_id=aid)

    def test_sealed_group_owner_correction_gap_completion_and_review_gate(self):
        f, api = self.f, self.api
        evidence = f.root / "dispatch-evidence.json"
        evidence.write_text(json.dumps({"tuples": [{
            "parent_harness": "opencode", "parent_transport": "headless", "parent_sandbox": "adapter-default",
            "child_harness": h, "launch_authority": "conductor", "status": "supported",
            "probe_source": "isolated-fixture", "probe_time": "2026-10-09T00:00:00Z", "failure_class": "",
            "checked_worktree": str(f.worktree), "failure_scope": "none", "codex_command": "ok",
            "retry_on_isolated_worktree": 0} for h in ("claude", "codex", "opencode")], "native_subagent": []}))
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.route = api.compose_route(capability="autopilot-spec", capability_mode="update", shape="staged",
                graph="autopilot-research:retrieval,autopilot-research:synthesis,review,prd-transaction",
                slug="partial-owner-recovery", cwd=str(f.worktree),
                artifact_root=str(f.artifacts), campaign_key="partial-owner-recovery", spec_read="isolated-fixture",
                parent_harness="opencode", dispatch_evidence=json.loads(evidence.read_text()), jobs=f.jobs,
                selection_pins={"owner": {"harness": "opencode", "model": None, "effort": None},
                                "worker": {"harness": "codex", "model": None, "effort": None}},
                work_request={"text": "Research, independently review, and update the specification.", "owner_harness": "opencode"})
            self.path = api.canonical_route_path(f.artifacts, self.route["route_id"])
            api.write_once(self.path, self.route)
            api.verify_route(self.route, expected_cwd=str(f.worktree))
            launched = START._start(self.route, self.path, f.jobs, "owner", "opencode", self.owner_run)
        self.assertEqual(launched["exit_code"], 0, launched)
        owner = launched["attempt_id"]
        INPUT.initialize_owner_input(f.jobs, owner, "opencode-next-turn")
        rc, first = self.batch(owner)
        self.assertEqual(first["state"], "partial-failure", first)
        peer, gap = first["legs"]
        self.assertEqual(peer["child_spawned"], "1", (first, [p.read_text() for p in f.root.glob("*.guard.json")]))
        self.assertEqual(gap["launch_state"], "failed", first)
        self.assertEqual(gap["child_spawned"], "1", first)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            gap_fields, gap_meta = f.row(gap["attempt_id"])
            if gap_fields[1] == "done" and DC.attempt_process_quiescence(gap_meta, terminal_receipt=True).state == "quiescent":
                break
            time.sleep(.05)
        else:
            self.fail("failed sibling cleanup: " + repr((gap_fields, DC.attempt_process_quiescence(gap_meta, terminal_receipt=True))))
        self.complete(peer)
        peer_row = f.row(peer["attempt_id"])[0]
        peer_spawn_marker = (f.root / (peer["attempt_id"] + ".started")).read_bytes()
        marker_path = api.completion_dir(self.route["route_id"], jobs=f.jobs) / "autopilot-research-retrieval.json"
        marker_bytes = marker_path.read_bytes()
        self.finish_process(owner)
        _, owner_meta = f.row(owner)
        owner_artifact = f.artifacts / "owner.md"
        owner_artifact.write_text("BLOCKED: retrieval alternative failed.\n")
        Path(owner_meta["log_file"]).write_text(json.dumps({"type": "text", "part": {"type": "text",
            "text": f"artifact: {owner_artifact}\nverdict: BLOCKED\nblocker: failed sibling"}}) + "\n")
        DC.reconcile_attempt_terminal(f.jobs, owner, "dead-worker-blocked", evidence={"failure_class": "blocked"})
        input_path = REPLACEMENT._directory(f.jobs) / "batch-inputs"
        manifest = json.loads(next(input_path.glob("*.json")).read_text())["manifest"]
        with mock.patch.dict(os.environ, self.env, clear=True):
            partial_options = dict(source_group_id="autopilot-research-retrieval", source_batch_manifest=manifest,
                failed_source_attempt_id=gap["attempt_id"],
                gap_leg_id="autopilot-research-retrieval-alternative")
            continuation = api.build_continuation_route(
                self.route, resume_from_node="autopilot-research-retrieval-alternative",
                requested_boundary="autopilot-research-retrieval", reason="retry failed retrieval alternative",
                artifact_root=str(f.artifacts), partial_group=partial_options)
            partial = continuation["partial_group_continuation"]
            continuation_path = api.canonical_route_path(f.artifacts, continuation["route_id"])
            api.publish_continuation_route(continuation, self.route, continuation_path)
        with mock.patch.dict(os.environ, self.env, clear=True):
            answer = INPUT.submit(f.jobs, owner, "Continue only the approved research gap.", "gap-fix")
            start_work = START.start_work
            with mock.patch.object(START, "start_work", side_effect=lambda *a, **kw:
                                   start_work(*a, **kw, run=self.owner_run)):
                restarted = api._continue_after_answer(f.jobs, owner, answer)
        self.assertTrue(restarted.get("owner_started"), restarted)
        new_owner = restarted["owner_attempt_id"]
        self.assertNotEqual(owner, new_owner, restarted)
        self.assertIn("Continue only the approved research gap.", self.prompts[new_owner])
        self.assertEqual(f.row(new_owner)[1]["automatic_retry_of"], owner)
        self.fail_gap = False
        brief = "New recovery request: reuse successful retrieval and finish its alternative.\n"
        rc, retried = self.batch(new_owner, continuation_path, brief)
        self.assertEqual(rc, 0, retried)
        self.assertEqual((retried["newly_started"], retried["existing"], retried["reused_peer_count"]), (1, 1, 1))
        replacement = retried["legs"][1]
        self.assertEqual(replacement["attempt_id"], partial["replacement_attempt_id"])
        self.assertEqual(replacement["child_spawned"], "1")
        self.assertEqual(f.row(replacement["attempt_id"])[1]["parent_attempt_id"], new_owner)
        replacement_prompt = self.prompts[replacement["attempt_id"]]
        self.assertIn(brief, replacement_prompt)
        self.assertIn("original sealed request", replacement_prompt)
        self.assertIn("Round protocol (round 2 of route node `autopilot-research-retrieval-alternative`", replacement_prompt)
        self.assertIn("판정 없음(dead-launch-exit-1)", replacement_prompt)
        self.assertIn("full execution of the original assigned task and scope", replacement_prompt)
        self.assertIn("verdict-less attempts provide execution context only", replacement_prompt)
        self.assertNotIn("This is a full independent review", replacement_prompt)
        self.assertNotIn("This is a correction round", replacement_prompt)
        self.complete(replacement)
        with mock.patch.dict(os.environ, self.env, clear=True):
            DC.completion_marker_gate(str(self.path), "autopilot-research-synthesis", "start", ROOT, f.jobs)
        self.assertEqual(marker_bytes, marker_path.read_bytes())
        self.assertEqual(peer_row, f.row(peer["attempt_id"])[0])
        self.assertEqual(peer_spawn_marker, (f.root / (peer["attempt_id"] + ".started")).read_bytes())
        print(json.dumps({"verdict": "PASS", "source_route": self.route["route_id"],
            "initial_batch": {"state": first["state"], "spawned": sum(int(leg["child_spawned"]) for leg in first["legs"])},
            "blocked_owner": owner, "continuation": continuation["route_id"], "new_owner": new_owner,
            "gap_attempt": replacement["attempt_id"], "gap_pid": f.row(replacement["attempt_id"])[1]["pid"],
            "gap_spawned": replacement["child_spawned"], "gap_completed": True,
            "reused_peer_count": retried["reused_peer_count"], "peer_evidence_unchanged": True,
            "synthesis_gate": "PASS", "retry_and_round_guidance_delivered": True}, sort_keys=True))


def fixture_wrapper():
    """Inject the model payload into the actual wrapper process, before main."""
    FIXTURE.OwnerGrantStartTest.setUpClass()
    root = Path(os.environ["HEARTING_RECOVERY_FIXTURE"])
    fixture = SimpleNamespace(root=root, worktree=root / "worktree", artifacts=root / "artifacts",
        home=root / "home", state=root / "state/dispatch", jobs=root / "state/dispatch/jobs.log",
        worker=root / "worker.py", processes=[], wrappers=FIXTURE.OwnerGrantStartTest.wrappers)
    fixture.row = MethodType(FIXTURE.OwnerGrantStartTest.row, fixture)
    fixture.fail = lambda message: (_ for _ in ()).throw(AssertionError(message))
    test = PartialGroupOwnerRecoveryTest("runTest")
    test.f, test.prompts = fixture, {}
    test.fail_gap = os.environ["HEARTING_RECOVERY_FAIL_GAP"] == "1"
    result = test.wrapper([sys.executable, *sys.argv], dict(os.environ))
    print(result.stdout, end=""); print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    unittest.main()
