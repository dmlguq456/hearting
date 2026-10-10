#!/usr/bin/env python3

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import textwrap
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "utilities" / "codex-app-server-supervisor.py"
PARENT = "att-parent"
DELIVERY_TIMING_POINTS = (
    "last_child_terminal_ns", "join_completed_ns", "same_thread_resume_ns",
    "exact_harvest_ns", "next_stage_start_ns", "final_report_marker_ns",
    "owner_terminal_envelope_ns",
)


def seal_route(value: dict) -> dict:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    value["route_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    value["route_id"] = "rt-" + value["route_hash"].split(":", 1)[1][:16]
    return value


def owner_row(lease: Path, status: str = "open") -> str:
    return (
        f"2026-07-23T00:00:00Z\t{status}\t/repo\t/wt\towner\t"
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "fallback_hop=same-harness-headless,worker_type=owner,harness=codex,"
        "completion_delivery=app-server-supervised,supervisor_lease=flock-v1,"
        f"supervisor_lease_file={lease},supervisor_lease_nonce={'d' * 64},"
        f"attempt_id={PARENT}\n"
    )


def child_row(attempt: str = "att-child", slug: str = "child", status: str = "open") -> str:
    return (
        f"2026-07-23T00:00:00Z\t{status}\t/repo\t/wt\t{slug}\t"
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,launch_started=1,"
        f"attempt_id={attempt},parent_attempt_id={PARENT},note=RAW_CHILD_SENTINEL\n"
    )


def chain_successor_row(index: int) -> str:
    """A serial sub-session registered up front and never started."""
    return (
        f"2026-07-23T00:00:0{index}Z\topen\t/repo\t/wt\tchain-{index}\t"
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,launch_claimed=0,"
        f"session_chain_id=ssc-fixture,subsession_mode=serial,subsession_index={index},"
        f"attempt_id=att-chain-{index},parent_attempt_id={PARENT}\n"
    )


class CodexAppServerSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.base)], check=True)
        self.artifact_root = self.base / ".agent_reports"
        self.artifact_root.mkdir()
        self.jobs = self.base / "jobs.log"
        self.state = self.base / "supervisor-state.json"
        self.lease = self.base / "supervisor-state" / f"{PARENT}.lease"
        self.trace = self.base / "trace.jsonl"
        self.app = self.base / "fake_app.py"
        self.join = self.base / "fake_join.py"
        self.app.write_text(
            textwrap.dedent(
                """\
                import fcntl, json, os, sys, threading, time
                trace = os.environ['FAKE_TRACE']
                turns = 0
                def record(event, **extra):
                    with open(trace, 'a', encoding='utf-8') as h:
                        h.write(json.dumps({'event': event, 'time': time.monotonic(), **extra}) + '\\n')
                def send(value):
                    print(json.dumps(value), flush=True)
                for line in sys.stdin:
                    value = json.loads(line)
                    method = value.get('method')
                    if method == 'initialize':
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'server':'fake'}})
                    elif method == 'initialized':
                        pass
                    elif method in ('thread/start', 'thread/resume'):
                        if method == 'thread/resume':
                            record('thread-resume', thread=value['params']['threadId'])
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'thread':{'id':'thread-1'}}})
                    elif method == 'turn/start':
                        turns += 1
                        prompt = value['params']['input'][0]['text']
                        lease_fd = os.open(os.environ['AGENT_DISPATCH_SUPERVISOR_LEASE_FILE'], os.O_RDWR)
                        try:
                            try:
                                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                lease_held = True
                            else:
                                lease_held = False
                                fcntl.flock(lease_fd, fcntl.LOCK_UN)
                        finally:
                            os.close(lease_fd)
                        state_path = os.environ.get('AGENT_DISPATCH_COMPLETION_STATE_FILE')
                        with open(state_path, encoding='utf-8') as h:
                            state = json.load(h)
                            delivered = state['delivered_attempt_ids']
                        if os.environ.get('FAKE_MIXED_START') == '1' and 'att-child' in delivered:
                            jobs = os.environ['FAKE_JOBS']
                            with open(jobs, encoding='utf-8') as h:
                                rows = h.read().replace('launch_started=0', 'launch_started=1')
                            with open(jobs, 'w', encoding='utf-8') as h:
                                h.write(rows)
                        record('turn-start', turn=turns, prompt=prompt, delivered=delivered,
                               lease_held=lease_held, phase=state['phase'], outbox=state.get('outbox'))
                        turn_id = f'turn-{turns}'
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'turn':{'id':turn_id}}})
                        send({'jsonrpc':'2.0','method':'thread/tokenUsage/updated','params':{
                            'threadId':'thread-1','turnId':turn_id,'tokenUsage':{
                                'last':{'inputTokens':80000,'cachedInputTokens':10000,
                                        'outputTokens':9000,'reasoningOutputTokens':1000,
                                        'totalTokens':100000},
                                'total':{'inputTokens':120000,'cachedInputTokens':30000,
                                         'outputTokens':5000,'reasoningOutputTokens':1000,
                                         'totalTokens':156000},
                                'modelContextWindow':200000,
                                'prompt':'MUST_NOT_LEAK'}}})
                        send({'jsonrpc':'2.0','method':'item/started','params':{
                            'threadId':'thread-1','turnId':turn_id,'item':{
                                'type':'commandExecution','id':f'cmd-{turns}',
                                'command':'python3 worker.py --private value','status':'inProgress'}}})
                        send({'jsonrpc':'2.0','method':'item/completed','params':{
                            'threadId':'thread-1','turnId':turn_id,'item':{
                                'type':'commandExecution','id':f'cmd-{turns}',
                                'command':'python3 worker.py --private value',
                                'aggregatedOutput':'MUST_NOT_REACH_FLEET','exitCode':0,
                                'status':'completed'}}})
                        dry_first = os.environ.get('FAKE_DRY_RUN_FIRST') == '1'
                        launch_race = os.environ.get('FAKE_LAUNCH_STARTED_RACE') == '1'
                        if launch_race and turns == 1:
                            jobs = os.environ['FAKE_JOBS']
                            with open(jobs, 'a', encoding='utf-8') as h:
                                for suffix in ('a', 'b'):
                                    h.write(f'2026-08-11T00:00:00Z\\topen\\t/repo\\t/wt\\tchild-race-{suffix}\\t'
                                            'attempt_schema_version=2,dispatch_depth=2,transport=headless,'
                                            'execution_surface=registered-headless,registered_worker=1,'
                                            f'launch_started=0,attempt_id=att-child-race-{suffix},'
                                            'parent_attempt_id=att-parent\\n')
                            def publish_started():
                                time.sleep(0.05)
                                for suffix in ('a', 'b'):
                                    with open(jobs, 'a', encoding='utf-8') as h:
                                        h.write(f'2026-08-11T00:00:01Z\\topen\\t/repo\\t/wt\\tchild-race-{suffix}\\t'
                                                'attempt_schema_version=2,dispatch_depth=2,transport=headless,'
                                                'execution_surface=registered-headless,registered_worker=1,'
                                                f'launch_started=1,attempt_id=att-child-race-{suffix},'
                                                'parent_attempt_id=att-parent\\n')
                                    time.sleep(0.03)
                            threading.Thread(target=publish_started, daemon=True).start()
                        if dry_first and turns == 2:
                            with open(os.environ['FAKE_JOBS'], 'a', encoding='utf-8') as h:
                                h.write('2026-08-11T00:00:00Z\\topen\\t/repo\\t/wt\\tchild\\t'
                                        'attempt_schema_version=2,dispatch_depth=2,transport=headless,'
                                        'execution_surface=registered-headless,registered_worker=1,'
                                        'launch_started=1,attempt_id=att-child-retry,'
                                        'parent_attempt_id=att-parent\\n')
                        final_first = os.environ.get('FAKE_NO_CHILD') == '1'
                        text = ('runtime_wait: registered-children' if dry_first and turns <= 2
                                else 'artifact: -\\nverdict: PASS\\nblocker: none'
                                if turns > 1 or final_first else 'runtime_wait: registered-children')
                        if launch_race and turns == 1 and os.environ.get('FAKE_RACE_TEXT'):
                            text = os.environ['FAKE_RACE_TEXT']
                        if os.environ.get('FAKE_BREAK_STATE_AUDIT') == '1':
                            state_path = os.environ['AGENT_DISPATCH_COMPLETION_STATE_FILE']
                            audit = state_path + '.transitions.jsonl'
                            try:
                                os.unlink(audit)
                            except FileNotFoundError:
                                pass
                            os.mkdir(audit)
                        send({'jsonrpc':'2.0','method':'item/completed','params':{
                            'threadId':'thread-1','turnId':turn_id,'completedAtMs':1,
                            'item':{'type':'agentMessage','id':f'msg-{turns}','text':text,
                                    'phase':None,'memoryCitation':None}}})
                        send({'jsonrpc':'2.0','method':'turn/completed','params':{
                            'threadId':'thread-1','turn':{'id':turn_id,'status':'completed'}}})
                """
            ),
            encoding="utf-8",
        )
        self.join.write_text(
            textwrap.dedent(
                """\
                import json, os, sys, time
                trace = os.environ['FAKE_TRACE']
                jobs = sys.argv[sys.argv.index('--jobs') + 1]
                parent = sys.argv[sys.argv.index('--parent-attempt-id') + 1]
                attempts = [sys.argv[i + 1] for i, value in enumerate(sys.argv) if value == '--attempt-id']
                def record(event, **extra):
                    with open(trace, 'a', encoding='utf-8') as h:
                        h.write(json.dumps({'event':event,'time':time.monotonic(), **extra}) + '\\n')
                state_path = os.path.join(os.path.dirname(trace), 'supervisor-state.json')
                with open(state_path, encoding='utf-8') as state_handle:
                    phase = json.load(state_handle)['phase']
                record('join-start', phase=phase)
                time.sleep(0.2)
                with open(jobs, encoding='utf-8') as h:
                    lines = h.read().splitlines()
                kept, current = [], {}
                for line in lines:
                    fields = line.split('\\t')
                    metadata = dict(part.split('=', 1) for part in fields[5].split(',') if '=' in part) if len(fields) == 6 else {}
                    attempt = metadata.get('attempt_id')
                    if attempt in attempts:
                        current[attempt] = fields
                    else:
                        kept.append(line)
                for attempt in attempts:
                    fields = current[attempt]
                    fields[1] = 'done'
                    fields[5] += ',failure_class=pass,note=completed-supervisor,launch_outcome=never-launched'
                    kept.append('\\t'.join(fields))
                with open(jobs, 'w', encoding='utf-8') as h:
                    h.write('\\n'.join(kept) + '\\n')
                record('join-end')
                print(json.dumps({'schema_version':2,'state':'ready','parent_attempt_id':parent,
                    'children':[{'attempt_id':attempt,'status':'done','readiness':'ready',
                                 'reason':'registry-closed','required_action':'advance-completed'} for attempt in attempts]}))
                """
            ),
            encoding="utf-8",
        )

    def command(self, *, broken_app: Path | None = None) -> list[str]:
        app = broken_app or self.app
        return [
            sys.executable,
            str(SUPERVISOR),
            "--worktree", str(self.base),
            "--jobs", str(self.jobs),
            "--parent-attempt-id", PARENT,
            "--state-file", str(self.state),
            "--lease-file", str(self.lease),
            "--sandbox", "danger-full-access",
            "--app-server-command", f"{sys.executable} {app}",
            "--join-command", f"{sys.executable} {self.join}",
            "--join-timeout", "2",
            "--join-interval", "0.02",
        ]

    def run_supervisor(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "FAKE_TRACE": str(self.trace), **extra_env}
        return subprocess.run(
            self.command(),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )


    def test_mixed_model_resource_outboxes_restart_after_model_interruption(self):
        self._assert_mixed_resource_outbox_delivery("model")

    def test_mixed_model_resource_outboxes_restart_after_resource_interruption(self):
        self._assert_mixed_resource_outbox_delivery("resource")

    def _assert_mixed_resource_outbox_delivery(self, interrupt_on):
        import dispatch_resource_wait as RESOURCE
        from types import SimpleNamespace
        module = load_supervisor_module()
        join = RESOURCE.JOIN
        self.jobs.write_text(owner_row(self.lease) + (child_row(status="done").rstrip("\n") + ",failure_class=pass,launch_outcome=never-launched\n"))
        route = seal_route({"schema_version":2,"cwd":str(self.base),
            "nodes":[{"id":"full-run","kind":"resource-runner","continuation":{"kind":"supervised"}},
                     {"id":"verify"}]})
        route_path = self.base / "mixed-route.json"
        route_path.write_text(json.dumps(route))
        route_args = ["--route-file",str(route_path),"--route-id",route["route_id"],"--route-hash",route["route_hash"]]
        run = {"run_id":"finished-resource","pid":555,"starttime":"201","command":["approved"],
               "node":"full-run","status":"succeeded","sentinel":"/fixture-exit"}
        stage = {"state":"STAGE_SUCCEEDED","evidence":{"resource_sha256":RESOURCE.RESUME.row_digest(run)}}
        sup = SimpleNamespace(poll_once=mock.Mock(),resource_evidence=lambda _: {"terminal":True,"succeeded":True,"liveness":"exited","exit_code":0},
            artifact_evidence=lambda _: {"checked":True,"missing":[]},runner=lambda:SimpleNamespace(read_sentinel=lambda _:0))
        ctx = (sup,route,SimpleNamespace(state=lambda:{"nodes":{"full-run":stage}}),
               [({"node":"full-run","successors":["verify"]},run)])
        args = SimpleNamespace(parent_attempt_id=PARENT,route_id=route["route_id"],route_hash=route["route_hash"],jobs=str(self.jobs))
        native = "mixed-native-session"
        turns = []
        interrupted = False
        acknowledgements = []
        original_acknowledge = RESOURCE.acknowledge
        def acknowledge(*args):
            accepted = original_acknowledge(*args)
            acknowledgements.append((args[2],accepted))
            return accepted
        def turn(*a,**k):
            nonlocal interrupted
            prompt,session = k["prompt"],k["thread_id"]
            kind = "resource" if "Runtime resource receipt" in prompt else "model"
            state = join.read_supervisor_phase_state(self.state,PARENT)
            self.assertIsNotNone(state.resource["outbox"])
            self.assertEqual(state.resource["delivered"],[])
            self.assertEqual(state.resource["outbox"]["receipt_id"],resource_id)
            self.assertEqual(session,native)
            turns.append(kind)
            if kind == "model":
                self.assertIsNotNone(state.outbox)
                self.assertNotIn("Runtime resource receipt",prompt)
            else:
                self.assertIsNone(state.outbox)
                self.assertIn('"verification_pass":false',prompt)
            if kind == interrupt_on and not interrupted:
                interrupted = True
                raise module.SupervisorError("fixture-receiving-turn-interrupted")
            text = "runtime_wait: registered-children" if kind=="model" else "artifact: -\nverdict: PASS\nblocker: none"
            return text,{}
        with mock.patch.object(RESOURCE,"context",return_value=ctx), \
             mock.patch.object(RESOURCE,"acknowledge",side_effect=acknowledge), \
             mock.patch.object(RESOURCE.RESUME,"supervisor_alive",return_value=True), \
             mock.patch.object(module,"run_turn",side_effect=turn), \
             mock.patch.object(module,"reconcile",return_value=True), \
             mock.patch.object(module,"emit"), mock.patch.object(module,"AppServer") as app:
            join.write_supervisor_state(self.state,PARENT,set(),phase="running-turn")
            RESOURCE.wait(args,self.state,SimpleNamespace(thread_id=native,pending=lambda:False),set(),lambda _:None)
            original = join.read_supervisor_phase_state(self.state,PARENT).resource["outbox"]
            resource_id = original["receipt_id"]
            receipt = {"schema_version":2,"state":"ready","parent_attempt_id":PARENT,
                "children":[{"attempt_id":"att-child","status":"done","readiness":"ready",
                             "reason":"registry-closed","required_action":"advance-completed"}],
                "delivery_timing":{"delivery_timing_schema_version":1,**{point:None for point in DELIVERY_TIMING_POINTS}}}
            join.prepare_supervisor_outbox(self.state,PARENT,set(),receipt,join.current_children(self.jobs,PARENT,{"att-child"}))
            app.return_value.request.return_value={"thread":{"id":native}}
            with mock.patch.object(sys,"stdin",io.StringIO("assignment")):
                self.assertEqual(module.main(self.command()[2:]+route_args),70)
            held = join.read_supervisor_phase_state(self.state,PARENT)
            self.assertEqual(held.resource["outbox"],original)
            self.assertEqual(held.resource["delivered"],[])
            self.assertEqual(held.outbox is not None,interrupt_on=="model")
            self.assertEqual(acknowledgements,[])
            with mock.patch.object(sys,"stdin",io.StringIO("assignment")):
                self.assertEqual(module.main(self.command()[2:]+route_args),0)
            self.assertEqual(acknowledgements,[(resource_id,True)])
            self.assertFalse(RESOURCE.acknowledge(self.state,PARENT,resource_id))
            self.assertEqual(acknowledgements,[(resource_id,True),(resource_id,False)])
        self.assertEqual(turns,["model","model","resource"] if interrupt_on=="model" else ["model","resource","resource"])
        self.assertEqual(sum(kind=="resource" for kind in turns),1 if interrupt_on=="model" else 2)
        self.assertEqual(sup.poll_once.call_count,1)

    def test_resource_only_park_resumes_same_owner_after_runtime_poll_not_a_bash_wait(self):
        import dispatch_resource_wait as RESOURCE
        from types import SimpleNamespace
        module = load_supervisor_module()
        self.jobs.write_text(owner_row(self.lease))
        route_value = seal_route({"schema_version":2,"cwd":str(self.base),
            "nodes":[{"id":"full-run","kind":"resource-runner","continuation":{"kind":"supervised"}},
                     {"id":"verify"}]})
        route_path = self.base / "resource-route.json"
        route_path.write_text(json.dumps(route_value))
        route_args = ["--route-file",str(route_path),"--route-id",route_value["route_id"],"--route-hash",route_value["route_hash"]]
        row = {"run_id":"approved","pid":555,"starttime":"201","command":["approved"],
               "node":"full-run","status":"running","sentinel":"/fixture-exit"}
        stage = {"state":"RUNNING"}
        events,turns = [],[]
        def poll(*_):
            events.append("runtime-poll")
            row.update(status="succeeded",exit_code=0)
            stage.update(state="STAGE_SUCCEEDED",evidence={"resource_sha256":RESOURCE.RESUME.row_digest(row)})
        sup = SimpleNamespace(poll_once=poll,resource_evidence=lambda _: {"terminal":True,"succeeded":True,"liveness":"exited","exit_code":0},
            artifact_evidence=lambda _: {"checked":True,"missing":[]},runner=lambda:SimpleNamespace(read_sentinel=lambda _:0))
        ledger = SimpleNamespace(state=lambda:{"nodes":{"full-run":stage}})
        ctx = (sup,route_value,ledger,[({"node":"full-run","successors":["verify"]},row)])
        def turn(*a,**k):
            events.append("model-turn")
            prompt,session = k["prompt"],k["thread_id"]
            turns.append((session,prompt))
            text = "runtime_wait: registered-children" if len(turns)==1 else "artifact: -\nverdict: PASS\nblocker: none"
            return text,{}
        owned_payload,owned_watch=mock.Mock(),mock.Mock()
        owned_payload.poll.side_effect=lambda:events.append("reap-owned-payload")
        owned_watch.poll.side_effect=lambda:events.append("reap-owned-watch")
        def context(args,control):
            args.resource_children={RESOURCE.resource_key(row):(owned_payload,owned_watch)}
            return ctx
        with mock.patch.object(RESOURCE,"context",side_effect=context), \
             mock.patch.object(RESOURCE.RESUME,"supervisor_alive",return_value=True), \
             mock.patch.object(module,"run_turn",side_effect=turn), \
             mock.patch.object(module,"reconcile",return_value=True), \
             mock.patch.object(sys,"stdin",io.StringIO("initial assignment")), \
             mock.patch.object(module,"emit"), mock.patch.object(module,"AppServer") as app:
            app.return_value.request.return_value={"thread":{"id":"same-native"}}
            self.assertEqual(module.main(self.command()[2:]+route_args),0)
        self.assertEqual(events,["model-turn","reap-owned-payload","reap-owned-watch","runtime-poll","model-turn"])
        owned_payload.poll.assert_called_once()
        owned_watch.poll.assert_called_once()
        self.assertEqual(len(turns),2)
        self.assertEqual(turns[0][0],turns[1][0])
        self.assertIn("Runtime resource receipt",turns[1][1])
        self.assertIn('"verification_pass":false',turns[1][1])
        self.assertIn("not a model child",turns[1][1])
        self.assertNotIn("registration-required",turns[1][1])
        creation = next(c for c in app.return_value.request.call_args_list if c.args[0]=="thread/start")
        self.assertIs(creation.args[1]["ephemeral"],False)

    def test_resource_payload_reuses_selected_native_sandbox_without_global_or_inherit_changes(self):
        module=load_supervisor_module()
        args=SimpleNamespace(sandbox="workspace-write",network_access=False,worktree=str(self.base),
            writable_root=[str(self.base/"output")],native_permission_profile=None)
        row={"cwd":str(self.base/"payload"),"command":["python3","producer.py","--epochs","1"]}
        command,selection=module.resource_sandbox_command(args,row)
        self.assertEqual(command[:2],["codex","sandbox"])
        self.assertIn("--include-managed-config",command)
        self.assertEqual(command[command.index("-C")+1],str(self.base))
        self.assertEqual(command[-6:],["resource-payload",row["cwd"],"python3","producer.py","--epochs","1"])
        self.assertIn('"extends"=":workspace"',command[5])
        self.assertIn('"enabled"=false',command[5])
        self.assertIn(str(self.base/"output"),command[5])
        self.assertEqual(selection,{"mode":"workspace-write","enforcement":"os-sandbox","network_access":False})
        self.assertNotIn("--inherit-pid-namespace",command)
        profile={"default_permissions":"hearting_linked_commit","permissions":{"hearting_linked_commit":{
            "extends":":workspace","filesystem":{str(self.base/".git/config"):"read"},"network":{"enabled":False}}}}
        args.native_permission_profile=profile
        command,_=module.resource_sandbox_command(args,row)
        self.assertIn('"'+str(self.base/".git/config")+'"="read"',command[5])
        args.native_permission_profile=None
        args.sandbox="read-only"
        command,selection=module.resource_sandbox_command(args,row)
        self.assertIn('"extends"=":read-only"',command[5])
        self.assertIn('"filesystem"={}',command[5])
        self.assertEqual(selection["enforcement"],"os-sandbox")
        args.sandbox="danger-full-access"
        command,selection=module.resource_sandbox_command(args,row)
        self.assertEqual(command[2:4],["-P",":danger-full-access"])
        self.assertEqual(selection["enforcement"],"none")
        args.sandbox="unsupported"
        with self.assertRaisesRegex(module.SupervisorError,"resource-sandbox-selection-unsupported"):
            module.resource_sandbox_command(args,row)

    def test_controller_intent_correction_scope_claim_and_exact_argv_boundaries(self):
        import dispatch_resource_wait as RESOURCE
        import dispatch_owner_input as INPUT
        runner=RESOURCE.supervisor().runner()
        args=SimpleNamespace(jobs=str(self.jobs),parent_attempt_id=PARENT,
            resource_launch_command=lambda row: (["codex","sandbox","--",*row["command"]],{"mode":"workspace-write"}))
        row={"run_id":"queued","cwd":str(self.base),"log":str(self.base/"output.log"),"route":str(self.base/"route.json"),
            "jobs":str(self.jobs),"node":"full-run","command":["python3","producer.py"],"parent_attempt_id":PARENT,
            "resource_policy":"supervised-owner","status":"launching","launch_state":"queued",
            "owner_wait":{"launch_scope":"codex-owner-controller","owner_pid":os.getpid(),"session_id":"same-native",
                "owner_start":runner.proc_identity(os.getpid())["starttime"],"parent_attempt_id":PARENT,"jobs":str(self.jobs)},
            "launch_request":{"smoke_attestation":"/exact-smoke.json","config_manifest":None}}
        self.jobs.write_text("")
        registry=self.base/"resource-registry.json"
        registry.write_text(json.dumps({"schema_version":1,"runs":{"queued":row}}))
        armed={"resource_registry":str(registry)}
        control=SimpleNamespace(thread_id="same-native")
        state={"target":"exact","thread_id":"same-native","requests":[]}
        import contextlib
        @contextlib.contextmanager
        def locked(*_):
            yield None,state
        def validate_call(argv,*,controller):
            self.assertEqual(argv,["--registry",str(registry),"start","--run-id","queued","--cwd",str(self.base),
                "--log",str(self.base/"output.log"),"--route",str(self.base/"route.json"),"--node","full-run",
                "--parent-attempt-id",PARENT,"--jobs",str(self.jobs),"--smoke-attestation","/exact-smoke.json",
                "--","python3","producer.py"])
            self.assertEqual(controller.expected,row)
            self.assertEqual(controller.command,["codex","sandbox","--","python3","producer.py"])
            with controller.guard():
                pass
        parent=SimpleNamespace(status="open",metadata={"harness":"codex","pid":str(os.getpid()),"pid_start":row["owner_wait"]["owner_start"]})
        with mock.patch.object(INPUT,"_locked",side_effect=locked),mock.patch.object(INPUT,"_target",return_value=(parent,"exact")), \
             mock.patch.object(runner,"main",side_effect=validate_call) as launch:
            RESOURCE.admit_controller_launch(args,control,armed,row)
            self.assertEqual(launch.call_count,1)
            state["requests"]=[{"state":"queued"}]
            with self.assertRaises(runner.LaunchDeferred):
                RESOURCE.admit_controller_launch(args,control,armed,row)
            state["requests"]=[]
            self.jobs.write_text(child_row())
            with self.assertRaisesRegex(runner.LaunchDeferred,"resource-model-child-pending"):
                RESOURCE.admit_controller_launch(args,control,armed,row)
            self.jobs.write_text(child_row().replace("launch_started=1","launch_started=0"))
            with self.assertRaisesRegex(runner.LaunchDeferred,"resource-model-child-pending"):
                RESOURCE.admit_controller_launch(args,control,armed,row)
            self.jobs.write_text("")
            parent.metadata["pid_start"]="foreign-start"
            with self.assertRaisesRegex(RESOURCE.JOIN.JoinContractError,"resource-owner-input-binding-changed"):
                RESOURCE.admit_controller_launch(args,control,armed,row)
            parent.metadata["pid_start"]=row["owner_wait"]["owner_start"]
            state["thread_id"]="foreign"
            with self.assertRaisesRegex(RESOURCE.JOIN.JoinContractError,"resource-owner-input-binding-changed"):
                RESOURCE.admit_controller_launch(args,control,armed,row)
            state["thread_id"]="same-native"
            with mock.patch.object(RESOURCE.os,"readlink",side_effect=["pid:[host]","pid:[foreign]"]):
                before=launch.call_count
                with self.assertRaisesRegex(RESOURCE.JOIN.JoinContractError,"resource-controller-scope-unavailable"):
                    RESOURCE.admit_controller_launch(args,control,armed,row)
                self.assertEqual(launch.call_count,before)
            claimed={**row,"launch_state":"claimed"}
            registry.write_text(json.dumps({"schema_version":1,"runs":{"queued":claimed}}))
            before=launch.call_count
            RESOURCE.admit_controller_launch(args,control,armed,claimed)
            self.assertEqual(launch.call_count,before)
            failed=json.loads(registry.read_text())["runs"]["queued"]
            self.assertEqual(failed["failure_class"],"resource-launch-incomplete")
            self.assertNotIn("pid",failed)

    def test_controller_launch_refusal_returns_resource_receipt_to_same_owner(self):
        import dispatch_resource_wait as RESOURCE
        runner = RESOURCE.supervisor().runner()
        registry = self.base / "resource-registry.json"
        row = {"run_id": "queued", "cwd": str(self.base), "log": str(self.base / "out.log"),
            "route": str(self.base / "route.json"), "jobs": str(self.jobs), "node": "full-run",
            "command": ["python3", "remote_bridge.py"], "parent_attempt_id": PARENT,
            "sentinel": str(self.base / "out.log.exit"),
            "resource_policy": "supervised-owner", "status": "launching", "launch_state": "queued",
            "owner_wait": {"launch_scope": "codex-owner-controller", "owner_pid": os.getpid(),
                "owner_start": runner.proc_identity(os.getpid())["starttime"], "session_id": "same-native"},
            "launch_request": {"smoke_attestation": None, "config_manifest": None}}
        args = SimpleNamespace(jobs=str(self.jobs), parent_attempt_id=PARENT, route_id="rt-test",
            route_hash="sha256:test", resource_launch_command=lambda r: (r["command"], {}))
        control = SimpleNamespace(thread_id="same-native", pending=lambda: False)
        armed = {"resource_registry": str(registry), "node": "full-run", "successors": ["verify"]}
        error = {"type": "GPUUnavailable", "message": "Local GPU admission on controller: GPU in use"}
        stage = {"state": "FAILED_RETRYABLE"}
        sup = SimpleNamespace(runner=lambda: runner, poll_once=mock.Mock(),
            resource_evidence=lambda _: {"terminal": True, "succeeded": False, "liveness": "exited", "exit_code": None},
            artifact_evidence=lambda _: {"missing": ["run.json"]})
        def context(*_):
            actual = json.loads(registry.read_text())["runs"]["queued"]
            return sup, {}, SimpleNamespace(state=lambda: {"nodes": {"full-run": stage}}), [(armed, actual)]
        def refuse(_argv, *, controller):
            failed = {**row, "status": "failed", "workflow_state": "FAILED_RETRYABLE",
                "launch_state": "not-started", "failure_class": "resource-launch-incomplete",
                "launch_controller": controller.identity, "launch_error": error}
            runner.publish_verified_run(registry, "queued", row, failed)
            raise runner.gpu_leases.GPUUnavailable(error["message"])
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"queued": row}}))
        RESOURCE.JOIN.write_supervisor_state(self.state, PARENT, set(), phase="running-turn")
        with mock.patch.object(runner, "main", side_effect=refuse), \
             mock.patch.object(RESOURCE, "context", side_effect=context):
            prompt = RESOURCE.wait(args, self.state, control, set(), lambda _: None)
            self.assertIn("same-native", prompt)
            self.assertIn(error["message"], prompt)
            self.assertIn('"state":"needs-attention"', prompt)
            self.assertIn('"workflow_complete":false', prompt)
            self.assertIn("normal distinct __a<N> resource retry", prompt)
            self.assertNotIn("do not restart the resource", prompt)
            box = RESOURCE.JOIN.read_supervisor_phase_state(self.state, PARENT).resource["outbox"]
            self.assertEqual(box["receipt"]["launch_error"], error)
            self.assertEqual(box["receipt"]["successors"], [])
            RESOURCE.acknowledge(self.state, PARENT, box["receipt_id"])
        # A failure without this controller's settled pre-release row still raises.
        for changes in ({"launch_state": "started"}, {"command": ["foreign"]},
                        {"launch_controller": {"pid": -1}}, {"launch_state": "claimed"}):
            registry.write_text(json.dumps({"schema_version": 1, "runs": {"queued": row}}))
            def unsettled(argv, *, controller):
                try:
                    refuse(argv, controller=controller)
                except runner.gpu_leases.GPUUnavailable:
                    actual = json.loads(registry.read_text())["runs"]["queued"]
                    runner.publish_verified_run(registry, "queued", actual, {**actual, **changes})
                    raise
            with self.subTest(changes=changes), mock.patch.object(runner, "main", side_effect=unsettled):
                with self.assertRaises(runner.gpu_leases.GPUUnavailable):
                    RESOURCE.admit_controller_launch(args, control, armed, row)

    def test_preclaim_validation_refusals_return_through_same_owner_outbox(self):
        import dispatch_resource_wait as RESOURCE
        import dispatch_owner_input as INPUT
        runner = RESOURCE.supervisor().runner()
        registry = self.base / "validation-registry.json"
        starttime = runner.proc_identity(os.getpid())["starttime"]
        row = {"run_id": "queued", "cwd": str(self.base), "log": str(self.base / "out.log"),
            "route": str(self.base / "route.json"), "jobs": str(self.jobs), "node": "full-run",
            "command": ["python3", "remote_bridge.py"], "parent_attempt_id": PARENT,
            "sentinel": str(self.base / "out.log.exit"), "workflow_state": "READY",
            "resource_policy": "supervised-owner", "status": "launching", "launch_state": "queued",
            "owner_wait": {"launch_scope": "codex-owner-controller", "owner_pid": os.getpid(),
                "owner_start": starttime, "session_id": "same-native", "parent_attempt_id": PARENT,
                "jobs": str(self.jobs)},
            "launch_request": {"smoke_attestation": None, "config_manifest": None}}
        args = SimpleNamespace(jobs=str(self.jobs), parent_attempt_id=PARENT, route_id="rt-test",
            route_hash="sha256:test", resource_launch_command=lambda r: (r["command"], {}))
        control = SimpleNamespace(thread_id="same-native", pending=lambda: False)
        armed = {"resource_registry": str(registry), "node": "full-run", "successors": ["verify"],
            "predecessor_id": "queued", "resource_binding": RESOURCE.resource_body_digest(row)}
        stage = {"state": "FAILED_RETRYABLE"}
        ledger = SimpleNamespace(lock=contextlib.nullcontext,
            state=lambda: {"nodes": {"full-run": stage}})
        sup = SimpleNamespace(runner=lambda: runner, poll_once=mock.Mock(),
            load_route=lambda _: {}, ledger_for=lambda *_: ledger, read_armed=lambda _: {"full-run": armed},
            resource_continuation_cancelled=lambda *_: False, _evaluate=mock.Mock(),
            resource_evidence=lambda _: {"terminal": True, "succeeded": False, "liveness": "exited", "exit_code": None},
            artifact_evidence=lambda _: {"missing": ["run.json"]})
        parent = SimpleNamespace(status="open", metadata={"harness": "codex", "pid": str(os.getpid()),
            "pid_start": starttime})
        @contextlib.contextmanager
        def locked(*_):
            yield None, {"target": "exact", "thread_id": "same-native", "requests": []}
        def context(*_):
            actual = json.loads(registry.read_text())["runs"]["queued"]
            return sup, {}, ledger, [(armed, actual)]
        cli_error = SystemExit(65)
        cli_error.resource_message = "config provenance does not match smoke attestation"
        failures = [subprocess.CalledProcessError(65, ["smoke-attestation.py", "verify"],
            stderr="stale smoke input: /current/standing-policy.json"), cli_error,
            FileNotFoundError("missing configuration manifest")]
        self.jobs.write_text("")
        for error in failures:
            with self.subTest(error=type(error).__name__):
                registry.write_text(json.dumps({"schema_version": 1, "runs": {"queued": row}}))
                self.state = self.base / (type(error).__name__ + "-state.json")
                RESOURCE.JOIN.write_supervisor_state(self.state, PARENT, set(), phase="running-turn")
                with mock.patch.object(runner, "_main", side_effect=error), \
                     mock.patch.object(RESOURCE, "supervisor", return_value=sup), \
                     mock.patch.object(RESOURCE, "context", side_effect=context), \
                     mock.patch.object(RESOURCE, "model_legs_pending", return_value=False), \
                     mock.patch.object(INPUT, "_locked", side_effect=locked), \
                     mock.patch.object(INPUT, "_target", return_value=(parent, "exact")), \
                     mock.patch.object(runner.subprocess, "Popen") as spawn:
                    prompt = RESOURCE.wait(args, self.state, control, set(), lambda _: None)
                    diagnostic = runner.launch_error(error)
                    self.assertIn(diagnostic["message"], prompt)
                    self.assertIn("same-native", prompt)
                    self.assertIn('"state":"needs-attention"', prompt)
                    failed = json.loads(registry.read_text())["runs"]["queued"]
                    self.assertTrue(runner.resource_never_started(failed))
                    self.assertEqual(failed["launch_request"], row["launch_request"])
                    box = RESOURCE.JOIN.read_supervisor_phase_state(self.state, PARENT).resource["outbox"]
                    self.assertEqual(box["receipt"]["parent_attempt_id"], PARENT)
                    self.assertEqual(box["receipt"]["session_id"], "same-native")
                    self.assertEqual(box["receipt"]["launch_error"], diagnostic)
                    self.assertEqual(box["receipt"]["successors"], [])
                    RESOURCE.acknowledge(self.state, PARENT, box["receipt_id"])
                    spawn.assert_not_called()

    def test_resource_phase_restart_resumes_exact_native_thread_without_new_start(self):
        module = load_supervisor_module()
        self.jobs.write_text(owner_row(self.lease))
        resource = {"session_id":"thread-1","delivered":["a"*64],"outbox":None}
        module.write_supervisor_state(self.state,PARENT,set(),phase="recovery")
        with module.RESOURCE_WAIT.JOIN._supervisor_state_lock(self.state):
            module.RESOURCE_WAIT.JOIN._write_supervisor_state_unlocked(self.state,PARENT,set(),phase="recovery",resource=resource)
        result = self.run_supervisor(FAKE_NO_CHILD="1")
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(trace[0], {**trace[0],"event":"thread-resume","thread":"thread-1"})
        self.assertEqual(sum(t["event"]=="thread-resume" for t in trace),1)
        self.assertEqual(sum(t["event"]=="turn-start" for t in trace),1)
        self.assertNotIn("registration-required",result.stdout)

    def test_runtime_wait_has_no_model_activity_until_exact_join_is_ready(self):
        self.jobs.write_text(
            owner_row(self.lease)
            + child_row("att-child-a", "child-a")
            + child_row("att-child-b", "child-b"),
            encoding="utf-8",
        )
        result = self.run_supervisor()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        events = [item["event"] for item in trace]
        self.assertEqual(events, ["turn-start", "join-start", "join-end", "turn-start"])
        self.assertEqual(trace[0]["delivered"], [])
        self.assertEqual(trace[0]["phase"], "running-turn")
        self.assertEqual(trace[3]["phase"], "running-turn")
        self.assertEqual(set(trace[3]["outbox"]["attempt_ids"]), {"att-child-a", "att-child-b"})
        self.assertEqual(trace[3]["outbox"]["consumed_attempt_ids"], [])
        self.assertTrue(all(item.get("lease_held") for item in trace if item["event"] == "turn-start"))
        self.assertEqual(
            set(trace[3]["delivered"]), {"att-child-a", "att-child-b"}
        )
        self.assertLess(trace[1]["time"], trace[2]["time"])
        self.assertLessEqual(trace[2]["time"], trace[3]["time"])
        self.assertEqual(trace[1]["phase"], "parked")
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(sum(row.get("type") == "turn.completed" for row in rows), 1)
        self.assertEqual(
            sum(row.get("type") == "dispatch.supervisor.turn.started" for row in rows),
            2,
        )
        telemetry = [row for row in rows
                     if row.get("type") == "dispatch.supervisor.token_usage"]
        self.assertEqual(len(telemetry), 2)
        self.assertEqual(telemetry[-1]["token_usage"]["last"]["total_tokens"], 100000)
        self.assertEqual(telemetry[-1]["token_usage"]["model_context_window"], 200000)
        self.assertNotIn("prompt", telemetry[-1]["token_usage"])
        self.assertEqual(
            [row["item"]["id"] for row in rows if row.get("type") == "item.started"],
            ["cmd-1", "cmd-2"],
        )
        final_messages = [
            row["item"]["text"]
            for row in rows
            if row.get("type") == "item.completed"
            and row.get("item", {}).get("type") == "agent_message"
            and "verdict: PASS" in row["item"].get("text", "")
        ]
        self.assertEqual(final_messages, ["artifact: -\nverdict: PASS\nblocker: none"])
        resumed = [row for row in rows if row.get("type") == "dispatch.supervisor.resumed"]
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["attempt_count"], 2)
        observed = [
            row for row in rows
            if row.get("type") == "dispatch.supervisor.join-observed"
        ]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["delivery_timing_schema_version"], 1)
        self.assertIsInstance(observed[0]["join_completed_ns"], int)
        # This transport fixture supplies no marker/committed child receipt.
        # A bare done/pass word must arrive as attention, not proved success.
        self.assertIn('"delivery_classification":"attention"', trace[3]["prompt"])
        timing_events = [
            row for row in rows
            if row.get("type") == "dispatch.supervisor.delivery-timing"
        ]
        self.assertEqual(len(timing_events), 1, rows)
        timing = timing_events[0]
        points = [timing[point] for point in DELIVERY_TIMING_POINTS]
        self.assertIsNone(timing["next_stage_start_ns"])
        observed_points = [value for value in points if value is not None]
        self.assertTrue(all(isinstance(value, int) for value in observed_points), timing)
        self.assertEqual(observed_points, sorted(observed_points))
        self.assertEqual(timing["same_thread_resume_count"], 1)
        terminal = next(i for i, row in enumerate(rows) if row.get("type") == "turn.completed")
        final_item = next(
            row for row in reversed(rows[:terminal])
            if row.get("type") == "item.completed"
            and row.get("item", {}).get("type") == "agent_message"
        )
        self.assertIn("verdict: PASS", final_item["item"]["text"])
        self.assertNotIn("RAW_CHILD_SENTINEL", result.stdout)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.lease.exists())
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/repo\t/wt\towner\t", registry)
        self.assertIn("note=completed-supervisor", registry)
        log = self.base / "attempt.codex.jsonl"
        log.write_text(result.stdout, encoding="utf-8")
        inspected = subprocess.run(
            [
                sys.executable,
                str(ROOT / "utilities" / "codex_dispatch_terminal.py"),
                "--worktree", str(self.base),
                "--artifact-root-metadata", str(self.artifact_root),
                str(log),
            ],
            text=True,
            capture_output=True,
            env={**os.environ, "AGENT_ARTIFACT_ROOT": str(self.artifact_root)},
        )
        self.assertEqual(inspected.returncode, 0, inspected.stderr + inspected.stdout)
        self.assertIn("\tvalid\texact-turn-completed\tPASS\tnone\tnone", inspected.stdout)

    def test_budget_warning_reaches_the_prompt_handed_to_owner(self):
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = subprocess.run(
            self.command() + ["--continuation-warning-threshold", "999"],
            input="initial assignment",
            text=True,
            capture_output=True,
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        turns = [
            json.loads(line)
            for line in self.trace.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "turn-start"
        ]
        self.assertEqual(len(turns), 2, turns)
        self.assertNotIn("[continuation-budget-warning]", turns[0]["prompt"])
        self.assertEqual(
            turns[1]["prompt"].count("[continuation-budget-warning]"), 1
        )

    def test_premature_pass_resumes_same_owner_before_terminal_commit(self):
        self.jobs.write_text(owner_row(self.lease).replace("attempt_id=att-parent", "workflow_completion=runtime-v1,attempt_id=att-parent"))
        route = self.base / "workflow-route.json"
        value = seal_route({"schema_version": 2, "cwd": str(self.base),
            "nodes": [{"id": "report", "dispatch_depth": 2, "terminal": True}], "resume_retry_boundaries": []})
        route.write_text(json.dumps(value))
        proof = self.base / "report.proof"
        wrapper = self.base / "checked-supervisor.py"
        wrapper.write_text(
            "import sys, runpy, os\nfrom pathlib import Path\nfrom types import SimpleNamespace\n"
            + "sys.path.insert(0, " + repr(str(SUPERVISOR.parent)) + ")\n"
            + "import dispatch_terminal_commit as T\n"
            + "route_module = T._route_module()\n"
            + "route_module._marker_identity_row = lambda *a, **k: "
              "{'passed': Path(os.environ['FAKE_REPORT_PROOF']).exists(), 'reason': 'marker-unreadable'}\n"
            + "T._route_module = lambda: route_module\n"
            + "runpy.run_path(" + repr(str(SUPERVISOR)) + ", run_name='__main__')\n")
        self.app.write_text(self.app.read_text().replace("turns += 1", "turns += 1\n        if turns == 2: open(os.environ['FAKE_REPORT_PROOF'], 'w').write('proved')"))
        command = self.command(); command[1] = str(wrapper)
        command += ["--route-file", str(route), "--route-id", value["route_id"], "--route-hash", value["route_hash"]]
        result = subprocess.run(command, input="initial assignment", text=True, capture_output=True,
            env={**os.environ, "FAKE_TRACE": str(self.trace), "FAKE_NO_CHILD": "1", "FAKE_REPORT_PROOF": str(proof)}, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        turns = [json.loads(line) for line in self.trace.read_text().splitlines() if json.loads(line).get("event") == "turn-start"]
        self.assertEqual(len(turns), 2, turns)
        self.assertIn("[workflow-completion-pending]", turns[1]["prompt"])
        self.assertIn("report", turns[1]["prompt"])
        self.assertIn("workflow-completion-incomplete", result.stdout)
        self.assertEqual(len(self.jobs.read_text().splitlines()), 1)
        self.assertIn("completed-supervisor", self.jobs.read_text())

    def test_no_child_finishes_in_one_turn(self):
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = self.run_supervisor(FAKE_NO_CHILD="1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual([item["event"] for item in trace], ["turn-start"])
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(sum(row.get("type") == "turn.completed" for row in rows), 1)
        self.assertEqual(
            sum(row.get("type") == "dispatch.supervisor.turn.started" for row in rows),
            1,
        )
        self.assertFalse(self.state.exists())
        self.assertFalse(self.lease.exists())
        self.assertEqual(
            sum(row.get("type") == "dispatch.supervisor.owner-boundary" for row in rows),
            0,
        )

    def test_input_queued_before_the_first_turn_reaches_that_turn(self):
        import dispatch_owner_input as owner_input
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        owner_input.initialize_owner_input(self.jobs, PARENT, "codex-active-turn")
        owner_input.submit(self.jobs, PARENT, "early word", "early")
        result = self.run_supervisor(FAKE_NO_CHILD="1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(len(trace), 1)
        self.assertIn("early word", trace[0]["prompt"])
        receipt = owner_input.inspect(self.jobs, PARENT)
        self.assertEqual(receipt["requests"][0]["state"], "turn-completed")
        self.assertEqual(receipt["requests"][0]["thread_id"], "thread-1")
        self.assertFalse(receipt["accepting"])

    def test_runtime_v1_owner_without_route_arguments_finishes(self):
        self.jobs.write_text(
            owner_row(self.lease).replace("attempt_id=", "workflow_completion=runtime-v1,attempt_id="),
            encoding="utf-8")
        result = self.run_supervisor(FAKE_NO_CHILD="1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_start_failure_before_the_first_consumer_leaves_the_input_undelivered(self):
        import dispatch_owner_input as owner_input
        broken = self.base / "broken.py"
        broken.write_text(
            "import json,sys\n"
            "v=json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'id':v['id'],'result':{'ok':1}}),flush=True)\n",
            encoding="utf-8",
        )
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        owner_input.initialize_owner_input(self.jobs, PARENT, "codex-active-turn")
        owner_input.submit(self.jobs, PARENT, "early word", "early")
        result = subprocess.run(
            self.command(broken_app=broken), input="initial assignment", text=True,
            capture_output=True, env={**os.environ, "FAKE_TRACE": str(self.trace)}, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("\tdone\t/repo\t/wt\towner\t", self.jobs.read_text(encoding="utf-8"))
        receipt = owner_input.inspect(self.jobs, PARENT)
        self.assertFalse(receipt["accepting"])
        self.assertEqual(receipt["requests"][0]["delivery_observation"], "undelivered")
        self.assertTrue(owner_input.unresolved(self.jobs, PARENT))

    def test_terminal_state_write_failure_does_not_replace_classified_exit(self):
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = self.run_supervisor(
            FAKE_NO_CHILD="1", FAKE_BREAK_STATE_AUDIT="1"
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn(
            "supervisor-finalize-state-JoinContractError",
            result.stdout,
        )

    def test_empty_runtime_wait_retries_start_in_same_thread_before_join(self):
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = self.run_supervisor(
            FAKE_DRY_RUN_FIRST="1", FAKE_JOBS=str(self.jobs)
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        turns = [row for row in trace if row["event"] == "turn-start"]
        self.assertEqual(len(turns), 3)
        self.assertIn("rerun the checked child dispatch with --start", turns[1]["prompt"])
        self.assertIn("registered=1, started=1, and child_spawned=1", turns[1]["prompt"])
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        correction = next(
            row for row in rows
            if row.get("continuation_reason") == "runtime-wait-without-started-child"
        )
        self.assertEqual(correction["state"], "registration-required")

    def test_runtime_wait_settles_launch_started_race_without_retry(self):
        self._assert_launch_started_race_settles()

    def test_turn_without_the_sentinel_settles_the_launch_started_race(self):
        # Registered children are waited for whatever the turn ended with, as in Claude and OpenCode.
        self._assert_launch_started_race_settles(FAKE_RACE_TEXT="Started both children.")

    def _assert_launch_started_race_settles(self, **extra_env):
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = self.run_supervisor(
            FAKE_LAUNCH_STARTED_RACE="1", FAKE_JOBS=str(self.jobs), **extra_env
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        turns = [row for row in trace if row["event"] == "turn-start"]
        self.assertEqual(len(turns), 2)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertFalse(any(
            row.get("continuation_reason") == "runtime-wait-without-started-child"
            for row in rows
        ))
        settled = [
            row for row in rows
            if row.get("type") == "dispatch.supervisor.launch-settled"
        ]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["attempt_count"], 2)
        parked = [
            row for row in rows
            if row.get("type") == "dispatch.supervisor.parked"
        ]
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]["attempt_count"], 2)

    def test_a_finished_owner_ends_without_starting_its_unstarted_chain(self):
        # BC rt-96bab699: after a correction the owner reported its result; the successors it
        # never started drew "rerun with --start" until the supervisor died. They close instead.
        self.jobs.write_text(owner_row(self.lease) + chain_successor_row(2) + chain_successor_row(3),
                             encoding="utf-8")
        result = self.run_supervisor(FAKE_NO_CHILD="1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("registration-required", result.stdout)
        self.assertNotIn("runtime-wait-without-started-child", result.stdout)
        rows = self.jobs.read_text(encoding="utf-8").splitlines()
        for index in (2, 3):
            row = next(line for line in rows if f"attempt_id=att-chain-{index}," in line + ",")
            self.assertIn("\tdone\t", row)
            self.assertIn("launch_outcome=never-launched", row)

    def test_started_child_is_collected_before_correcting_unstarted_sibling(self):
        pending = child_row("att-pending").replace("launch_started=1", "launch_started=0")
        self.jobs.write_text(owner_row(self.lease) + child_row() + pending)
        result = self.run_supervisor(FAKE_MIXED_START="1", FAKE_JOBS=str(self.jobs))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual([event["event"] for event in trace], [
            "turn-start", "join-start", "join-end", "turn-start",
            "join-start", "join-end", "turn-start",
        ])
        self.assertEqual(trace[3]["delivered"], ["att-child"])
        self.assertEqual(set(trace[-1]["delivered"]), {"att-child", "att-pending"})
        self.assertNotIn('"state": "registration-required"', result.stdout)

    def test_bound_long_route_survives_thirteen_continuations_and_completes(self):
        route = self.base / "long-route.json"
        route_value = seal_route({
            "schema_version": 2,
            "cwd": str(self.base),
            "nodes": [{"id": f"node-{index}"} for index in range(8)],
            "resume_retry_boundaries": [f"node-{index}" for index in range(7)],
        })
        route.write_text(json.dumps(route_value), encoding="utf-8")
        long_app = self.base / "long_app.py"
        long_app.write_text(
            textwrap.dedent(
                """\
                import json, os, sys
                turns = 0
                def send(value):
                    print(json.dumps(value), flush=True)
                for line in sys.stdin:
                    value = json.loads(line)
                    method = value.get('method')
                    if method == 'initialize':
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'server':'fake'}})
                    elif method == 'initialized':
                        pass
                    elif method in ('thread/start', 'thread/resume'):
                        if method == 'thread/resume':
                            record('thread-resume', thread=value['params']['threadId'])
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'thread':{'id':'thread-long'}}})
                    elif method == 'turn/start':
                        turns += 1
                        prompt = value['params']['input'][0]['text']
                        if turns <= 13:
                            attempt = f'att-child-{turns}'
                            with open(os.environ['LONG_JOBS'], 'a', encoding='utf-8') as h:
                                h.write('2026-08-06T00:00:00Z\\topen\\t/repo\\t/wt\\t'
                                        f'child-{turns}\\tattempt_schema_version=2,'
                                        'dispatch_depth=2,transport=headless,'
                                        'execution_surface=registered-headless,registered_worker=1,launch_started=1,'
                                        f'attempt_id={attempt},parent_attempt_id=att-parent\\n')
                            text = 'runtime_wait: registered-children'
                        else:
                            text = 'artifact: report.md\\nverdict: PASS\\nblocker: none'
                        turn_id = f'turn-{turns}'
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'turn':{'id':turn_id}}})
                        send({'jsonrpc':'2.0','method':'item/completed','params':{
                            'threadId':'thread-long','turnId':turn_id,
                            'item':{'type':'agentMessage','id':f'msg-{turns}','text':text}}})
                        send({'jsonrpc':'2.0','method':'turn/completed','params':{
                            'threadId':'thread-long','turn':{'id':turn_id,'status':'completed'}}})
                """
            ),
            encoding="utf-8",
        )
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        command = self.command(broken_app=long_app) + [
            "--route-file", str(route),
            "--route-id", route_value["route_id"],
            "--route-hash", route_value["route_hash"],
        ]
        result = subprocess.run(
            command,
            input="initial assignment",
            text=True,
            capture_output=True,
            env={**os.environ, "FAKE_TRACE": str(self.trace), "LONG_JOBS": str(self.jobs)},
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        budget = next(row for row in rows if row.get("type") == "dispatch.supervisor.continuation-budget")
        self.assertEqual(budget["source"], "bound-route")
        self.assertGreater(budget["ordinary"], 13)
        self.assertEqual(budget["limit"], budget["ordinary"] + budget["reserved"])
        resumed = [row for row in rows if row.get("type") == "dispatch.supervisor.resumed"]
        self.assertEqual(len(resumed), 13)
        self.assertEqual(resumed[-1]["continuation_ordinal"], 13)
        self.assertEqual(sum(row.get("type") == "turn.completed" for row in rows), 1)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/repo\t/wt\towner\t", registry)
        self.assertIn("note=completed-supervisor", registry)
        # Every completed child except the last is immediately followed, in
        # the same owner turn, by the next child's dispatch -- one
        # owner-boundary crossing per hand-off, none after the final child
        # (there is no next dispatch to cross into).
        boundaries = [row for row in rows if row.get("type") == "dispatch.supervisor.owner-boundary"]
        self.assertEqual(len(boundaries), 12)
        # "ordinal" is the position of this owner-boundary event within the
        # (currently always single) batch of same-type events emitted at one
        # crossing -- it is not a running crossing counter across the whole
        # route, and no consumer reads it as one. The emitter always emits
        # exactly one such event per crossing, so ordinal==1 on every one of
        # the 12 crossings here is the intended, fixed value.
        self.assertTrue(all(row["ordinal"] == 1 for row in boundaries))
        self.assertTrue(all(row["parent_attempt_id"] == PARENT for row in boundaries))
        for index, boundary in enumerate(boundaries, start=1):
            self.assertEqual(boundary["new_attempt_ids"], [f"att-child-{index + 1}"])
            self.assertEqual(boundary["new_count"], 1)
            self.assertIn(f"att-child-{index}", boundary["previous_attempt_ids"])
            self.assertEqual(boundary["previous_count"], index)
            timing_order = [
                boundary["last_child_terminal_ns"], boundary["join_completed_ns"],
                boundary["same_thread_resume_ns"],
                boundary["next_stage_start_ns"],
            ]
            self.assertIsNone(boundary["exact_harvest_ns"])
            self.assertEqual(timing_order, sorted(timing_order))
        self.assertFalse(any(
            row.get("type") == "dispatch.supervisor.owner-boundary"
            and "att-child-14" in row.get("new_attempt_ids", [])
            for row in rows
        ))

    def test_protocol_failure_emits_no_false_terminal(self):
        broken = self.base / "broken.py"
        broken.write_text(
            "import json,sys\n"
            "v=json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'id':v['id'],'result':{'ok':1}}),flush=True)\n",
            encoding="utf-8",
        )
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        env = {**os.environ, "FAKE_TRACE": str(self.trace)}
        result = subprocess.run(
            self.command(broken_app=broken),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"type":"turn.completed"', result.stdout)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.lease.exists())
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/repo\t/wt\towner\t", registry)
        self.assertIn("note=dead-protocol", registry)

    def test_failed_turn_with_usage_limit_closes_capacity(self):
        # plan.md item 4: a structured Codex TurnError (codexErrorInfo =
        # usageLimitExceeded) must close as capacity, not the generic
        # dead-runtime-exit a bare non-zero exit or unstructured reason gets.
        capacity_app = self.base / "capacity_app.py"
        capacity_app.write_text(
            textwrap.dedent(
                """\
                import json, sys
                def send(value):
                    print(json.dumps(value), flush=True)
                for line in sys.stdin:
                    value = json.loads(line)
                    method = value.get('method')
                    if method == 'initialize':
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'server':'fake'}})
                    elif method == 'initialized':
                        pass
                    elif method in ('thread/start', 'thread/resume'):
                        if method == 'thread/resume':
                            record('thread-resume', thread=value['params']['threadId'])
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'thread':{'id':'thread-1'}}})
                    elif method == 'turn/start':
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'turn':{'id':'turn-1'}}})
                        send({'jsonrpc':'2.0','method':'turn/completed','params':{
                            'threadId':'thread-1','turn':{'id':'turn-1','status':'failed',
                                'error':{'message':"You've hit your usage limit",
                                         'codexErrorInfo':'usageLimitExceeded',
                                         'additionalDetails':'Resets in 13 days'}}}})
                """
            ),
            encoding="utf-8",
        )
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = subprocess.run(
            self.command(broken_app=capacity_app),
            input="initial assignment",
            text=True,
            capture_output=True,
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
            timeout=10,
        )
        self.assertEqual(result.returncode, 70, result.stderr + result.stdout)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        failed = [row for row in rows if row.get("type") == "dispatch.supervisor.turn.failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["codex_error_info"], "usageLimitExceeded")
        self.assertIn("usage limit", failed[0]["message"])
        self.assertIn("Resets in 13 days", failed[0]["additional_details"])
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/repo\t/wt\towner\t", registry)
        self.assertIn("note=dead-capacity", registry)
        self.assertIn("failure_class=capacity", registry)

    # -- Phase 4 (plan.md, round_1 finding 1 dependency): owner restoration,
    # byte-isomorphic Codex case (SD-43 sibling principle: a Claude PASS is
    # not proxy evidence for Codex). --

    def _non_closing_join(self) -> Path:
        script = self.base / "fake_join_stale.py"
        script.write_text(
            textwrap.dedent(
                """\
                import json, sys
                parent = sys.argv[sys.argv.index('--parent-attempt-id') + 1]
                attempts = [sys.argv[i + 1] for i, value in enumerate(sys.argv) if value == '--attempt-id']
                print(json.dumps({'schema_version':2,'state':'ready','parent_attempt_id':parent,
                    'children':[{'attempt_id':attempt,'status':'open','readiness':'ready',
                                 'reason':'terminal-observed','required_action':'complete-open'} for attempt in attempts]}))
                """
            ),
            encoding="utf-8",
        )
        return script

    def _blocked_child_row(self) -> str:
        log = self.base / "att-child.codex.jsonl"
        artifact = self.artifact_root / "brief.md"
        artifact.write_text("evidence\n", encoding="utf-8")
        log.write_text(
            "\n".join(json.dumps(row) for row in [
                {"type": "system", "subtype": "init"},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": f"artifact: {artifact}\nverdict: BLOCKED\nblocker: stuck",
                    },
                },
                {"type": "turn.completed"},
            ]) + "\n",
            encoding="utf-8",
        )
        route = self.base / "route.json"
        return (
            f"2026-07-23T00:00:00Z\topen\t{self.base}\t{self.base}\tchild\t"
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,launch_started=1,"
            "fallback_hop=same-harness-headless,harness=codex,"
            f"attempt_id=att-child,parent_attempt_id={PARENT},"
            f"log_file={log},artifact_root={self.artifact_root},"
            f"route_file={route},route_node=frame,"
            "launch_outcome=reaped-before-publish\n"
        )

    def command_with_join(self, join_script: Path) -> list[str]:
        cmd = self.command()
        idx = cmd.index("--join-command")
        cmd[idx + 1] = f"{sys.executable} {join_script}"
        return cmd

    def test_blocked_child_reconciles_without_owned_children_error(self):
        self.jobs.write_text(owner_row(self.lease) + self._blocked_child_row(), encoding="utf-8")
        result = subprocess.run(
            self.command_with_join(self._non_closing_join()),
            input="initial assignment",
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "FAKE_TRACE": str(self.trace),
                "AGENT_ARTIFACT_ROOT": str(self.artifact_root),
            },
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn(
            "owned-children-remain-open-after-resume",
            result.stdout + result.stderr,
        )
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        reconciled = [
            row for row in rows
            if row.get("type") == "dispatch.supervisor.reconciled"
            and row.get("attempt_id") == "att-child"
        ]
        self.assertEqual(len(reconciled), 1, rows)
        self.assertEqual(reconciled[0]["outcome"], "closed")
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("dead-worker-blocked", registry)

    def test_attention_is_runtime_acknowledged_without_model_harvest(self):
        self.jobs.write_text(owner_row(self.lease) + self._blocked_child_row(), encoding="utf-8")
        result = subprocess.run(self.command_with_join(self._non_closing_join()),
            input="initial assignment", text=True, capture_output=True,
            env={**os.environ, "FAKE_TRACE": str(self.trace),
                 "AGENT_ARTIFACT_ROOT": str(self.artifact_root)}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        turns = [json.loads(line) for line in self.trace.read_text().splitlines()
                 if json.loads(line)["event"] == "turn-start"]
        self.assertEqual(len(turns), 2, turns)
        self.assertIn("inspect-done-failure", turns[1]["prompt"])
        self.assertNotIn("redelivery-suppressed", result.stdout)
        registry = self.jobs.read_text()
        self.assertIn("dead-worker-blocked", registry)
        self.assertNotIn("owner-redelivery-abandoned", registry)
        self.assertFalse(self.state.exists())

    def test_unverifiable_child_keeps_controller_responsibility_without_model_redelivery(self):
        self._assert_unverifiable_child_is_retained("open")

    def test_done_word_does_not_discharge_unverified_child_cleanup(self):
        self._assert_unverifiable_child_is_retained("done")

    def _assert_unverifiable_child_is_retained(self, child_status):
        parent = owner_row(self.lease).rstrip("\n") + ",parent_sid=parent-test,parent_completion_delivery=codex-managed-gateway\n"
        self.jobs.write_text(parent + child_row(status=child_status), encoding="utf-8")
        output = self.base / "controller.jsonl"
        env = {**os.environ, "FAKE_TRACE": str(self.trace), "AGENT_ARTIFACT_ROOT": str(self.artifact_root)}
        with output.open("w") as stream:
            process = subprocess.Popen(self.command_with_join(self._non_closing_join())
                + ["--max-continuations", "2"], stdin=subprocess.PIPE,
                stdout=stream, stderr=subprocess.STDOUT, text=True, env=env)
            try:
                process.stdin.write("initial assignment")
                process.stdin.close()
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and "dispatch.supervisor.reparked" not in output.read_text():
                    self.assertIsNone(process.poll(), output.read_text())
                    time.sleep(0.02)
                self.assertIn("dispatch.supervisor.reparked", output.read_text())
                checkpoints = [json.loads(line) for line in output.read_text().splitlines()
                    if line.startswith("{") and json.loads(line).get("type") == "dispatch.supervisor.reparked"]
                self.assertEqual(checkpoints[0]["notice_error"], "", checkpoints)
                self.assertIsNone(process.poll())
                turns = [json.loads(line) for line in self.trace.read_text().splitlines()
                         if json.loads(line)["event"] == "turn-start"]
                self.assertEqual(len(turns), 2, turns)
                self.assertIn("\topen\t", self.jobs.read_text())
                # The fixture now supplies terminal/quiescence evidence. The
                # controller, not another model bookkeeping turn, observes it.
                settled = child_row(status="done").rstrip("\n") + ",launch_outcome=never-launched,note=dead-launch-error,failure_class=runtime\n"
                self.jobs.write_text(parent + settled)
                self.assertEqual(process.wait(timeout=10), 0, output.read_text())
                turns = [json.loads(line) for line in self.trace.read_text().splitlines()
                         if json.loads(line)["event"] == "turn-start"]
                self.assertEqual(len(turns), 2)
                self.assertNotIn("owner-redelivery-abandoned", self.jobs.read_text())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)




def load_supervisor_module():
    spec = importlib.util.spec_from_file_location(
        "codex_app_server_supervisor_unit", SUPERVISOR
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fake_run_result(returncode, stdout):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


class OneTurnStageTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location("stage_supervisor", SUPERVISOR)
        self.supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.supervisor)

    def test_usage_numeric_allowlist_preserves_zero_and_decreasing_last(self):
        wire = {
            "last": {"inputTokens": 0, "outputTokens": 4, "totalTokens": 4,
                     "reasoningOutputTokens": True, "cachedInputTokens": 1.0,
                     "x": "secret"},
            "total": {"totalTokens": 9, "inputTokens": -1,
                      "outputTokens": "8", "cachedInputTokens": float("inf")},
            "modelContextWindow": 2**63,
            "prompt": "private prompt", "extra": {"secret": "value"},
        }
        first = self.supervisor.normalize_token_usage(wire)
        self.assertEqual(first, {"last": {"input_tokens": 0, "output_tokens": 4,
                                           "total_tokens": 4},
                                 "total": {"total_tokens": 9}})
        lower = self.supervisor.normalize_token_usage({
            "last": {"totalTokens": 2}, "total": {"totalTokens": 10},
            "modelContextWindow": "100000",
        })
        self.assertEqual(lower["last"]["total_tokens"], 2)
        self.assertNotIn("model_context_window", lower)
        self.assertNotIn("model_context_window", self.supervisor.normalize_token_usage({
            "last": {"totalTokens": 0}, "total": {},
        }))
        self.assertEqual(self.supervisor.normalize_token_usage({
            "last": {"totalTokens": 2**63 - 1}, "total": {},
            "modelContextWindow": 0,
        }), {"last": {"total_tokens": 2**63 - 1}, "total": {},
             "model_context_window": 0})
        self.assertNotIn("prompt", json.dumps(first))

    def test_one_turn_uses_ephemeral_thread_one_turn_and_closes_server(self):
        fake = self.root / "fake.py"
        trace = self.root / "trace.jsonl"
        fake.write_text(textwrap.dedent("""\
            import json, os, signal, sys
            trace = os.environ['STAGE_TRACE']
            def send(value): print(json.dumps(value), flush=True)
            def record(value):
                with open(trace, 'a', encoding='utf-8') as out:
                    out.write(json.dumps(value) + '\\n')
            def close(*_):
                record({'event':'closed'})
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, close)
            turns = 0
            for line in sys.stdin:
                value = json.loads(line); method = value.get('method')
                if method == 'initialize':
                    send({'jsonrpc':'2.0','id':value['id'],'result':{}})
                elif method == 'initialized':
                    record({'event':'initialized'})
                elif method == 'thread/start':
                    record({'event':'thread','params':value['params']})
                    send({'jsonrpc':'2.0','id':value['id'],'result':{'thread':{'id':'thread-self'}}})
                elif method == 'turn/start':
                    turns += 1
                    record({'event':'turn','params':value['params']})
                    send({'jsonrpc':'2.0','method':'thread/tokenUsage/updated','params':{
                        'threadId':'thread-self','turnId':'turn-self','tokenUsage':{
                            'last':{'totalTokens':0},'total':{'totalTokens':11},
                            'modelContextWindow':None,'prompt':'DO_NOT_LOG'}}})
                    send({'jsonrpc':'2.0','method':'thread/tokenUsage/updated','params':{
                        'threadId':'foreign-thread','turnId':'turn-self','tokenUsage':{
                            'last':{'totalTokens':99},'total':{'totalTokens':99}}}})
                    send({'jsonrpc':'2.0','method':'thread/tokenUsage/updated','params':{
                        'threadId':'thread-self','turnId':'foreign-turn','tokenUsage':{
                            'last':{'totalTokens':88},'total':{'totalTokens':88}}}})
                    send({'jsonrpc':'2.0','id':value['id'],'result':{'turn':{'id':'turn-self'}}})
                    send({'jsonrpc':'2.0','method':'item/completed','params':{
                        'turnId':'turn-self','item':{'type':'agentMessage','id':'m1',
                        'text':'artifact: -\\nverdict: PASS\\nblocker: none'}}})
                    send({'jsonrpc':'2.0','method':'turn/completed','params':{
                        'turn':{'id':'turn-self','status':'completed'}}})
                elif method == 'shutdown':
                    break
        """), encoding="utf-8")
        env = {**os.environ, "STAGE_TRACE": str(trace)}
        result = subprocess.run(
            [sys.executable, str(SUPERVISOR), "--one-turn", "--worktree", str(self.root),
             "--sandbox", "read-only", "--app-server-command", f"{sys.executable} {fake}"],
            input="stage assignment", text=True, capture_output=True, env=env, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        usage = [row for row in rows if row.get("type") == "dispatch.supervisor.token_usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["thread_id"], "thread-self")
        self.assertEqual(usage[0]["turn_id"], "turn-self")
        self.assertEqual(usage[0]["token_usage"]["last"]["total_tokens"], 0)
        self.assertNotIn("prompt", json.dumps(usage))
        self.assertEqual(sum(row.get("type") == "turn.completed" for row in rows), 1)
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        thread = next(row for row in events if row.get("event") == "thread")
        turn = next(row for row in events if row.get("event") == "turn")
        self.assertIs(thread["params"]["ephemeral"], True)
        self.assertEqual(turn["params"]["threadId"], "thread-self")
        self.assertEqual(sum(row.get("event") == "turn" for row in events), 1)
        self.assertIn({"event": "closed"}, events)
        self.assertNotIn("--parent-attempt-id", result.args)

    def test_empty_prompt_and_app_server_eof_fail_without_terminal_success(self):
        marker = self.root / "started"
        fake = self.root / "eof.py"
        fake.write_text(
            "import pathlib, sys\n"
            f"pathlib.Path({str(marker)!r}).write_text('started')\n"
            "sys.stdin.readline()\n",
            encoding="utf-8",
        )
        common = [sys.executable, str(SUPERVISOR), "--one-turn", "--worktree", str(self.root),
                  "--sandbox", "read-only", "--app-server-command", f"{sys.executable} {fake}"]
        empty = subprocess.run(common, input=" \n", text=True, capture_output=True, timeout=10)
        self.assertEqual(empty.returncode, 64)
        self.assertFalse(marker.exists())
        self.assertIn('"reason":"initial-prompt-empty"', empty.stdout)

        eof = subprocess.run(common, input="stage assignment", text=True,
                             capture_output=True, timeout=10)
        self.assertEqual(eof.returncode, 70)
        rows = [json.loads(line) for line in eof.stdout.splitlines()]
        self.assertTrue(any(row.get("reason") == "app-server-eof" for row in rows))
        self.assertFalse(any(row.get("type") == "turn.completed" for row in rows))

    def test_runtime_failures_and_completed_handoffs_keep_classification(self):
        from codex_dispatch_terminal import inspect_terminal_attempt
        from dispatch_supervisor_terminal import classify_supervisor_log

        artifact_root = self.root / ".agent_reports"
        artifact_root.mkdir()
        artifact = artifact_root / "review.md"
        artifact.write_text("Fixture review findings\n", encoding="utf-8")
        fake = self.root / "terminal.py"
        fake.write_text(textwrap.dedent("""\
            import json, os, sys
            def send(value): print(json.dumps(value), flush=True)
            scenario = os.environ['STAGE_SCENARIO']
            for line in sys.stdin:
                value=json.loads(line); method=value.get('method')
                if method == 'initialize':
                    send({'jsonrpc':'2.0','id':value['id'],'result':{}})
                elif method == 'thread/start':
                    send({'jsonrpc':'2.0','id':value['id'],'result':{'thread':{'id':'thread-terminal'}}})
                elif method == 'turn/start':
                    send({'jsonrpc':'2.0','id':value['id'],'result':{'turn':{'id':'turn-terminal'}}})
                    if scenario in ('failed', 'interrupted'):
                        send({'jsonrpc':'2.0','method':'turn/completed','params':{
                            'turn':{'id':'turn-terminal','status':scenario,
                                    'error':{'message':scenario}}}})
                        continue
                    if scenario != 'missing':
                        if scenario == 'invalid':
                            text = 'not a handoff'
                        else:
                            verdict = {'pass':'PASS', 'fail':'FAIL', 'blocked':'BLOCKED'}[scenario]
                            blocker = 'none' if verdict == 'PASS' else 'fixture'
                            text = 'artifact: ' + os.environ['STAGE_ARTIFACT'] + '\\nverdict: ' + verdict + '\\nblocker: ' + blocker
                        send({'jsonrpc':'2.0','method':'item/completed','params':{
                            'turnId':'turn-terminal','item':{'type':'agentMessage','id':'final','text':text}}})
                    send({'jsonrpc':'2.0','method':'turn/completed','params':{
                        'turn':{'id':'turn-terminal','status':'completed'}}})
        """), encoding="utf-8")
        command = [sys.executable, str(SUPERVISOR), "--one-turn", "--worktree", str(self.root),
                   "--sandbox", "read-only", "--app-server-command", f"{sys.executable} {fake}"]
        for scenario, expected, terminal_event, semantic_note in (
                ("failed", 70, False, None), ("interrupted", 70, False, None),
                ("pass", 0, True, "completed-supervisor"),
                ("fail", 0, True, "dead-worker-fail"),
                ("blocked", 0, True, "dead-worker-blocked"),
                ("missing", 0, True, "dead-contract"),
                ("invalid", 0, True, "dead-contract")):
            with self.subTest(scenario=scenario):
                result = subprocess.run(command, input="stage assignment", text=True,
                                        capture_output=True, timeout=10,
                                        env={**os.environ, "STAGE_SCENARIO": scenario,
                                             "STAGE_ARTIFACT": str(artifact)})
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                rows = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(any(row.get("type") == "turn.completed" for row in rows),
                                 terminal_event)
                self.assertFalse(any(row.get("type") == "dispatch.supervisor.resumed" for row in rows))
                if scenario in {"failed", "interrupted"}:
                    self.assertTrue(any(row.get("type") == "dispatch.supervisor.turn.failed"
                                         for row in rows), result.stdout)
                    continue
                log = self.root / f"{scenario}.codex.jsonl"
                log.write_text(result.stdout, encoding="utf-8")
                self.assertEqual(classify_supervisor_log(log, "codex").note, semantic_note)
                with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(artifact_root)}):
                    inspected = inspect_terminal_attempt(
                        log, worktree=self.root, artifact_root_metadata=artifact_root,
                        worker_type="review",
                    )
                if scenario in {"pass", "fail", "blocked"}:
                    self.assertEqual(inspected["state"], "valid")
                    self.assertEqual(inspected["verdict"], scenario.upper())
                    self.assertEqual(inspected["artifact_state"], "readable")
                    final = next(row["item"]["text"] for row in rows
                                 if row.get("type") == "item.completed"
                                 and row.get("item", {}).get("type") == "agent_message")
                    self.assertIn(f"verdict: {scenario.upper()}", final)
                    if scenario == "fail":
                        self.assertEqual(inspected["failure_note"], "completed-review-blocking")
                else:
                    self.assertNotEqual(inspected["state"], "valid")

    def test_named_permission_profile_is_applied_without_turn_sandbox_override(self):
        profile = {"default_permissions": "profile-test",
                   "permissions": {"profile-test": {
                       "filesystem": {str(self.root): "write"},
                       "network": {"enabled": True}}}}
        captured = {}

        class FakeServer:
            def __init__(self, command, cwd, env):
                captured["command"] = command
                captured["cwd"] = cwd
                self.events = [
                    {"method": "item/completed", "params": {"turnId": "turn-profile",
                     "item": {"type": "agentMessage", "id": "final",
                              "text": chr(10).join(("artifact: -", "verdict: PASS",
                                                    "blocker: none"))}}},
                    {"method": "turn/completed", "params": {
                     "turn": {"id": "turn-profile", "status": "completed"}}},
                ]

            def request(self, method, params):
                captured.setdefault("requests", []).append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thread-profile"}}
                if method == "turn/start":
                    return {"turn": {"id": "turn-profile"}}
                return {}

            def notification(self, method):
                captured.setdefault("notifications", []).append(method)

            def next_event(self):
                return self.events.pop(0)

            def close(self):
                captured["closed"] = True

        args = self.supervisor.parser().parse_args([
            "--one-turn", "--worktree", str(self.root), "--sandbox", "workspace-write",
            "--approval", "on-request", "--network-access", "--writable-root", str(self.root),
            "--model", "model-pinned", "--reasoning", "xhigh", "--primary-git-commit",
        ])
        output = io.StringIO()
        with mock.patch.object(self.supervisor, "commit_profile_config", return_value=profile) as build, \
             mock.patch.object(self.supervisor, "config_arguments", return_value=["--profile-test"]), \
             mock.patch.object(self.supervisor, "AppServer", FakeServer), \
             mock.patch.object(self.supervisor.sys, "stdin", io.StringIO("prompt")), \
             mock.patch.object(self.supervisor.sys, "stdout", output):
            result = self.supervisor.run_one_turn(args)
        self.assertEqual(result, 0)
        build.assert_called_once_with(
            str(self.root), [str(self.root)], "workspace-write", True,
            primary_commit=True,
        )
        self.assertIn("--profile-test", captured["command"])
        self.assertEqual(captured["cwd"], str(self.root))
        requests = dict(captured["requests"])
        thread = requests["thread/start"]
        turn = requests["turn/start"]
        self.assertEqual(thread["cwd"], str(self.root))
        self.assertIs(thread["ephemeral"], True)
        self.assertEqual(thread["approvalPolicy"], "on-request")
        self.assertNotIn("sandbox", thread)
        self.assertEqual(turn["threadId"], "thread-profile")
        self.assertEqual(turn["model"], "model-pinned")
        self.assertEqual(turn["effort"], "xhigh")
        self.assertNotIn("sandboxPolicy", turn)
        self.assertTrue(captured["closed"])
        self.assertIn('"type":"turn.completed"', output.getvalue())


class ActiveTurnDeadlineParityTest(unittest.TestCase):
    """The Codex App Server turn has no elapsed-time deadline: a live turn that
    stays quiet across many read polls still completes on its own turn."""

    def test_live_app_server_turn_is_not_interrupted_by_elapsed_time(self):
        module = load_supervisor_module()
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "slow_app.py"
            trace = Path(temp) / "trace"
            script.write_text(textwrap.dedent(f"""\
                import json, sys, time
                def send(value):
                    print(json.dumps(value), flush=True)
                for line in sys.stdin:
                    value = json.loads(line)
                    with open({str(trace)!r}, 'a') as handle:
                        handle.write(value.get('method', '?') + '\\n')
                    if value.get('method') == 'turn/start':
                        send({{'jsonrpc':'2.0','id':value['id'],'result':{{'turn':{{'id':'turn-1'}}}}}})
                        time.sleep(1.4)   # well past several 0.2s read polls
                        send({{'jsonrpc':'2.0','method':'item/completed','params':{{
                            'threadId':'thread-1','turnId':'turn-1','item':{{
                                'type':'agentMessage','id':'m1','text':'done'}}}}}})
                        send({{'jsonrpc':'2.0','method':'turn/completed','params':{{
                            'threadId':'thread-1','turn':{{'id':'turn-1','status':'completed'}}}}}})
                """))
            server = module.AppServer([sys.executable, str(script)], temp, dict(os.environ))
            self.addCleanup(server.close)
            args = SimpleNamespace(worktree=temp, writable_root=[], sandbox="danger-full-access",
                                   network_access=False, approval="inherit", model=None,
                                   reasoning=None, owner_input=None)
            started = time.monotonic()
            with mock.patch.object(module, "emit"):
                text, item = module.run_turn(server, thread_id="thread-1", prompt="go", args=args)
            self.assertGreater(time.monotonic() - started, 1.0)
            self.assertEqual(text, "done")
            self.assertIsNone(server.process.poll())   # same live server, never interrupted
            self.assertEqual(trace.read_text().split().count("turn/start"), 1)
            self.assertNotIn("turn/interrupt", trace.read_text())


class SharedReceiptlessRecoveryTest(unittest.TestCase):
    """The former Codex-only recovery now belongs to the shared join."""

    def setUp(self):
        load_supervisor_module()
        import dispatch_completion_join
        self.module = dispatch_completion_join
        self.child = argparse.Namespace(attempt_id="att-child", status="open",
                                        metadata={"pid_scope": "namespace-local"})
        # These cases exercise the cancellation receipt contract after the
        # separate exact-death classifier has declined to close the row.
        exact = mock.patch.object(self.module, "reconcile_exact_dead_attempt",
                                  return_value={"closed": False})
        exact.start()
        self.addCleanup(exact.stop)

    def response(self, *, closed=0, reason="namespace-not-extinct", digest=None):
        return fake_run_result(0, json.dumps({
            "classifier_source": "automatic-receipt-unavailable-v1",
            "decisions": [{"attempt_id": "att-child", "closed": closed,
                           "reason": reason, "receipt_digest": digest}],
        }))

    def test_exact_proven_closure_and_scope(self):
        with mock.patch.object(self.module.subprocess, "run", return_value=self.response(
                closed=1, digest="sha256:" + "a" * 64)) as run:
            result = self.module.recover_receiptless_attempt(Path("/fixture/jobs.log"), self.child)
        self.assertTrue(result["closed"])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--attempt") + 1], "att-child")
        self.assertNotIn("--all", command)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_unproven_and_live_rows_are_not_closed(self):
        for reason in ("process-alive", "namespace-not-extinct", "namespace-observation-unavailable"):
            with self.subTest(reason=reason), mock.patch.object(self.module.subprocess, "run",
                    return_value=self.response(reason=reason)):
                result = self.module.recover_receiptless_attempt(Path("/fixture/jobs.log"), self.child)
            self.assertFalse(result["closed"])
            self.assertEqual(result["reason"], reason)

    def test_missing_proof_and_malformed_output_are_not_closure(self):
        for response in (self.response(closed=1), fake_run_result(1, "broken"),
                         fake_run_result(0, "[]")):
            with self.subTest(response=response), mock.patch.object(self.module.subprocess, "run", return_value=response):
                result = self.module.recover_receiptless_attempt(Path("/fixture/jobs.log"), self.child)
            self.assertFalse(result["closed"])
            self.assertEqual(result["reason"], "recovery-process-failed")

    def test_second_ineligible_call_does_not_invent_another_closure(self):
        with mock.patch.object(self.module.subprocess, "run", side_effect=[
                self.response(closed=1, digest="sha256:" + "b" * 64),
                self.response(reason="attempt-already-terminal")]):
            first = self.module.recover_receiptless_attempt(Path("/fixture/jobs.log"), self.child)
            second = self.module.recover_receiptless_attempt(Path("/fixture/jobs.log"), self.child)
        self.assertTrue(first["closed"])
        self.assertFalse(second["closed"])


class TypedReceiptStageAdvanceNegotiationTest(unittest.TestCase):
    """SD-110 A-18: an un-negotiated (default) call takes the literal,
    unmodified v2 path -- golden-byte identical to the pre-SD-110 receipt."""

    def setUp(self):
        self.module = load_supervisor_module()

    def _join_value(self):
        return {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": PARENT,
            "children": [
                {
                    "attempt_id": "att-child",
                    "status": "done",
                    "readiness": "ready",
                    "reason": "registry-closed",
                    "required_action": "advance-completed",
                }
            ],
        }

    def test_default_call_is_byte_identical_to_pre_sd110(self):
        value = self._join_value()
        receipt = self.module._typed_receipt(value, PARENT, {"att-child"})
        golden = json.dumps(receipt, sort_keys=True)
        negotiated_but_recordless = self.module._typed_receipt(
            value, PARENT, {"att-child"}, accept_stage_advance=True
        )
        self.assertEqual(receipt["schema_version"], 2)
        self.assertNotIn("stage_advance", receipt)
        self.assertEqual(json.dumps(negotiated_but_recordless, sort_keys=True), golden)
        # SD-119: Codex's own supervisor loop is bound to the chain-advance
        # path (R2b), but a join with no chain metadata is a no-op -- this
        # receipt never carries a chain key, byte-identical to pre-SD-119.
        # Claude-only realized behavior confirmed by measurement (SD-OPEN-15):
        # this call proves the shared no-op contract, not cross-harness parity.
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_subsession_advance as subsession_advance

        no_chain = subsession_advance.coordinate_chain_advance_from_joined_rows(
            Path("/nonexistent/jobs.registry"), PARENT, {"att-child": SimpleNamespace(
                attempt_id="att-child", status="done", metadata={},
            )},
        )
        self.assertIsNone(no_chain)
        self.assertNotIn("chain_id", json.dumps(receipt, sort_keys=True))

    def test_negotiated_advanced_record_attaches_v3_block(self):
        value = self._join_value()
        record = {
            "schema_version": 1,
            "stage_advance_id": "sadv-" + "0" * 64,
            "route_id": "rt-0000000000000000",
            "route_hash": "sha256:" + "0" * 64,
            "predecessor_node": "plan",
            "predecessor_terminal_attempt_id": "att-plan",
            "successor_node": "execute",
            "successor_attempt_id": "att-execute",
            "claim_key": ["sha256:" + "0" * 64, "execute", 0],
            "brief_template_digest": "sha256:" + "1" * 64,
            "outcome": "advanced",
            "reason": "",
            "registered": True,
            "started": True,
            "child_spawned": True,
        }
        receipt = self.module._typed_receipt(
            value,
            PARENT,
            {"att-child"},
            accept_stage_advance=True,
            stage_advance_record=record,
        )
        self.assertEqual(receipt["schema_version"], 3)
        self.assertEqual(receipt["stage_advance"], record)


class StageAdvanceWiringTest(unittest.TestCase):
    """Block 4: `attempt_stage_advance` wiring at this supervisor's symmetric
    park point (plan §5 block 4, §8.3-1 -- no `terminal_route_completion`
    precedent here, but `coordinate_stage_advance` is a different function and
    does not need one). `coordinate_stage_advance` itself is fully covered by
    `dispatch_stage_advance.test.py`; this only proves the wiring: right
    predecessor/phase inputs, right canary event, byte-identical no-op by
    default, and no advance exception ever escapes."""

    def setUp(self) -> None:
        self.module = load_supervisor_module()
        self.events: list[dict] = []
        self._orig_emit = self.module.emit
        self.module.emit = self.events.append
        self.addCleanup(lambda: setattr(self.module, "emit", self._orig_emit))

    def _args(self, **overrides):
        base = SimpleNamespace(
            route_file="/tmp/sd110-fixture-route.json",
            route_id="rt-fixture0000000",
            route_hash="sha256:" + "a" * 64,
            jobs="/tmp/sd110-fixture-jobs",
            parent_attempt_id=PARENT,
            worktree="/wt",
            enable_stage_advance=False,
        )
        base.__dict__.update(overrides)
        return base

    def _row(self, attempt_id, *, status="done", route_node="a",
             route_id="rt-fixture0000000", route_hash="sha256:" + "a" * 64):
        return SimpleNamespace(
            attempt_id=attempt_id,
            status=status,
            metadata={
                "route_node": route_node,
                "route_id": route_id,
                "route_hash": route_hash,
            },
        )

    def test_disabled_by_default_is_a_byte_identical_no_op(self):
        args = self._args()
        rows = [self._row("att-child")]
        with mock.patch.object(
            self.module.stage_advance, "coordinate_stage_advance"
        ) as coordinate:
            self.module.attempt_stage_advance(args, rows, {"att-child"})
        coordinate.assert_not_called()
        self.assertEqual(self.events, [])

    def test_enabled_advanced_emits_stage_advance_event(self):
        args = self._args(enable_stage_advance=True)
        rows = [self._row("att-child")]
        fake_result = self.module.stage_advance.StageAdvanceResult(
            outcome="advanced", reason="", stage_advance_id="sadv-fixture",
            successor_node="b", successor_attempt_id="att-b",
            claim_key=(args.route_hash, "b", 0),
            brief_template_digest="sha256:" + "b" * 64, gate_closed=True,
            registered=True, started=True, child_spawned=True, record_path=None,
        )
        timing = {"last_child_terminal_ns": 1000, "join_completed_ns": 2000}
        with mock.patch.object(
            self.module.stage_advance, "coordinate_stage_advance",
            return_value=fake_result,
        ) as coordinate:
            self.module.attempt_stage_advance(args, rows, {"att-child"}, timing)
        coordinate.assert_called_once()
        request = coordinate.call_args[0][0]
        self.assertEqual(request.predecessor_node, "a")
        self.assertEqual(request.predecessor_terminal_attempt_id, "att-child")
        self.assertEqual(request.parent_attempt_id, PARENT)
        self.assertEqual(request.supervisor_phase, "parked")
        self.assertEqual(request.delivered_open_attempt_ids, frozenset())
        self.assertEqual(request.harness, "codex")
        self.assertEqual(request.receipt_schema_negotiated, 3)
        self.assertIsInstance(
            coordinate.call_args[0][1],
            self.module.stage_advance.RealStageAdvanceServices,
        )
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["type"], "dispatch.supervisor.stage-advance")
        self.assertEqual(event["advance_mode"], "runtime-deterministic")
        self.assertEqual(event["outcome"], "advanced")
        self.assertEqual(event["predecessor_node"], "a")
        self.assertEqual(event["successor_node"], "b")
        self.assertEqual(event["route_hash"], args.route_hash)
        self.assertEqual(event["parent_attempt_id"], PARENT)
        canary = event["delivery_timing"]
        self.assertEqual(canary["last_child_terminal_ns"], 1000)
        self.assertEqual(canary["join_completed_ns"], 2000)
        self.assertIsNone(canary["same_thread_resume_ns"])
        self.assertIsNone(canary["exact_harvest_ns"])
        self.assertIsInstance(canary["next_stage_start_ns"], int)

    def test_open_sibling_reports_running_turn_phase(self):
        """T1 correction (round-1 blocking finding 1): the real open/running
        attempt-id intersection must reach the core, not an unconditional
        empty frozenset -- mirrors
        claude_session_supervisor.test.py's symmetric assertion."""

        args = self._args(enable_stage_advance=True)
        rows = [
            self._row("att-child"),
            self._row("att-open", status="open"),
            self._row("att-running", status="running"),
        ]
        fake_result = self.module.stage_advance.StageAdvanceResult(
            outcome="refused", reason="stage-advance-phase-ineligible",
            stage_advance_id="", successor_node=None, successor_attempt_id=None,
            claim_key=None, brief_template_digest="", gate_closed=False,
            registered=False, started=False, child_spawned=False, record_path=None,
        )
        with mock.patch.object(
            self.module.stage_advance, "coordinate_stage_advance",
            return_value=fake_result,
        ) as coordinate:
            self.module.attempt_stage_advance(args, rows, {"att-child"})
        request = coordinate.call_args[0][0]
        self.assertEqual(request.supervisor_phase, "running-turn")
        self.assertEqual(
            request.delivered_open_attempt_ids, frozenset({"att-open", "att-running"})
        )
        self.assertEqual(self.events[0]["type"], "dispatch.supervisor.stage-advance-refused")
        self.assertNotIn("delivery_timing", self.events[0])

    def test_route_binding_mismatch_is_skipped_without_a_call(self):
        args = self._args(enable_stage_advance=True)
        rows = [self._row("att-child", route_id="rt-different000000")]
        with mock.patch.object(
            self.module.stage_advance, "coordinate_stage_advance"
        ) as coordinate:
            self.module.attempt_stage_advance(args, rows, {"att-child"})
        coordinate.assert_not_called()
        self.assertEqual(self.events, [])

    def test_service_exception_is_swallowed_as_a_refusal_event_never_raises(self):
        args = self._args(enable_stage_advance=True)
        rows = [self._row("att-child")]
        with mock.patch.object(
            self.module.stage_advance, "coordinate_stage_advance",
            side_effect=RuntimeError("boom"),
        ):
            self.module.attempt_stage_advance(args, rows, {"att-child"})
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["type"], "dispatch.supervisor.stage-advance-refused")
        self.assertEqual(event["outcome"], "refused")
        self.assertEqual(event["reason"], "RuntimeError")

    def test_advanced_outcome_returns_the_durable_record_for_receipt_delivery(self):
        """§13.32.1-(2)6/(3)B, symmetric with claude-session-supervisor.py's
        fixture of the same name: `attempt_stage_advance` reads the durable
        `stage_advance_record_v1` off disk so the call site can feed it
        straight into `receipt_with_stage_advance` under the SAME
        `enable_stage_advance` condition that produced `receipt_schema_negotiated
        == 3` -- never a second, independently-toggled decision. An
        `outcome == "advanced"` record must never coexist with a v2 delivery."""

        args = self._args(enable_stage_advance=True)
        rows = [self._row("att-child")]
        with tempfile.TemporaryDirectory() as tmp:
            record_path = Path(tmp) / "sadv-fixture.json"
            record = {
                "schema_version": 1,
                "stage_advance_id": "sadv-fixture",
                "route_id": "rt-fixture0000000",
                "route_hash": args.route_hash,
                "predecessor_node": "a",
                "predecessor_terminal_attempt_id": "att-child",
                "successor_node": "b",
                "successor_attempt_id": "att-b",
                "claim_key": [args.route_hash, "b", 0],
                "brief_template_digest": "sha256:" + "b" * 64,
                "outcome": "advanced",
                "reason": "",
                "registered": True,
                "started": True,
                "child_spawned": True,
            }
            record_path.write_text(json.dumps(record), encoding="utf-8")
            fake_result = self.module.stage_advance.StageAdvanceResult(
                outcome="advanced", reason="", stage_advance_id="sadv-fixture",
                successor_node="b", successor_attempt_id="att-b",
                claim_key=(args.route_hash, "b", 0),
                brief_template_digest="sha256:" + "b" * 64, gate_closed=True,
                registered=True, started=True, child_spawned=True,
                record_path=record_path,
            )
            with mock.patch.object(
                self.module.stage_advance, "coordinate_stage_advance",
                return_value=fake_result,
            ):
                returned = self.module.attempt_stage_advance(args, rows, {"att-child"})
        self.assertEqual(returned, record)

        base_receipt = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": PARENT,
            "children": [],
        }
        negotiated_delivery = self.module.receipt_with_stage_advance(
            base_receipt, stage_advance_record=returned
        )
        self.assertEqual(negotiated_delivery["schema_version"], 3)
        self.assertEqual(
            negotiated_delivery["stage_advance"]["outcome"], "advanced"
        )
        # T1 correction: no independent `negotiated` bool exists anymore --
        # see the symmetric assertion/comment in
        # claude_session_supervisor.test.py.
        import inspect  # noqa: PLC0415

        self.assertNotIn(
            "negotiated",
            inspect.signature(self.module.receipt_with_stage_advance).parameters,
        )
        recordless_delivery = self.module.receipt_with_stage_advance(
            base_receipt, stage_advance_record=None
        )
        self.assertEqual(recordless_delivery["schema_version"], 2)
        self.assertNotIn("stage_advance", recordless_delivery)
        self.assertIs(recordless_delivery, base_receipt)


class ContinuationTripartiteBudgetTest(unittest.TestCase):
    """SD-116 §13.34.4-(2), symmetric to claude_session_supervisor.test.py's
    identically-named class."""

    def test_delivery_consumes_one_continuation_and_no_model_bookkeeping_stall(self):
        case = CodexAppServerSupervisorTest()
        case.setUp()
        try:
            case.jobs.write_text(owner_row(case.lease) + case._blocked_child_row(), encoding="utf-8")
            result = subprocess.run(
                case.command_with_join(case._non_closing_join()),
                input="initial assignment",
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "FAKE_TRACE": str(case.trace),
                    "AGENT_ARTIFACT_ROOT": str(case.artifact_root),
                },
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            registry = case.jobs.read_text(encoding="utf-8")
            self.assertNotIn("note=owner-redelivery-abandoned", registry)
            sys.path.insert(0, str(ROOT / "utilities"))
            import dispatch_budget_record as BR
            rows = BR.read_rows(case.jobs.parent, PARENT)
            reservations = [row for row in rows if row.get("record_kind") == "reservation"]
            stall_charged = [row for row in reservations if row["class"] == "stall"]
            self.assertEqual(stall_charged, [], rows)
            self.assertEqual(len(reservations), 1, rows)
        finally:
            case.tearDown() if hasattr(case, "tearDown") else None

    def test_workload_budget_keeps_report_and_final_handoff_after_fourteen_turns(self):
        module = load_supervisor_module()
        import dispatch_continuation_budget as B
        budget = B.ContinuationBudget(32, "workload-regression")
        ledger = B.ContinuationLedger(budget)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for ordinal in range(14):
                verdict, _ = module._admit_continuation(
                    ledger, root, parent_attempt_id=PARENT, route_id="rt-workload",
                    route_hash="sha256:" + "e" * 64, ordinal=ordinal,
                    purpose="ordinary", stalled=False)
                self.assertTrue(verdict.admitted, (ordinal, verdict))
            report_dispatch, _ = module._admit_continuation(
                ledger, root, parent_attempt_id=PARENT, route_id="rt-workload",
                route_hash="sha256:" + "e" * 64, ordinal=14,
                purpose="ordinary", stalled=False)
            self.assertTrue(report_dispatch.admitted)
            notice = module._seal_terminal_handoff_or_raise(
                ledger, root, args=_terminal_handoff_args(), ordinal=15,
                failure_reason="terminal-handoff-incomplete", terminal_handoff_issued=[False])
            self.assertIn("final continuation", notice)
            self.assertEqual(ledger.reserved_remaining, 0)

    def test_runtime_wait_without_started_child_spends_stall_only(self):
        case = CodexAppServerSupervisorTest()
        case.setUp()
        try:
            case.jobs.write_text(owner_row(case.lease), encoding="utf-8")
            result = case.run_supervisor(FAKE_DRY_RUN_FIRST="1", FAKE_JOBS=str(case.jobs))
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            sys.path.insert(0, str(ROOT / "utilities"))
            import dispatch_budget_record as BR
            rows = BR.read_rows(case.jobs.parent, PARENT)
            reservations = [row for row in rows if row.get("record_kind") == "reservation"]
            stall_charged = [row for row in reservations if row["class"] == "stall"]
            self.assertTrue(stall_charged, rows)
        finally:
            case.tearDown() if hasattr(case, "tearDown") else None

    def test_budget_denial_records_warning_at_the_admission_boundary(self):
        module = load_supervisor_module()
        import dispatch_continuation_budget as B
        import dispatch_budget_record as BR
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = B.ContinuationLedger(B.ContinuationBudget(1, "test", reserved=0))
            kwargs = dict(parent_attempt_id=PARENT, route_id="rt-budget", route_hash="hash",
                          purpose="ordinary", stalled=False)
            first, _ = module._admit_continuation(ledger, root, ordinal=0, **kwargs)
            second, _ = module._admit_continuation(ledger, root, ordinal=1, **kwargs)
            self.assertTrue(first.admitted)
            self.assertFalse(second.admitted)
            warnings = [row for row in BR.read_rows(root, PARENT) if row.get("record_kind") == "warning"]
            self.assertEqual(warnings[-1]["reason"], "continuation-budget-exhausted")

    def test_terminal_handoff_purpose_is_sealed_at_the_single_completion_receipt_site(self):
        source = SUPERVISOR.read_text(encoding="utf-8")
        self.assertEqual(source.count('"terminal-handoff" if open_or_running'), 1)
        self.assertEqual(source.count("purpose=consumption_purpose"), 1)
        self.assertIn("SD-116 R2: terminal-handoff is sealed here and only here", source)


class BudgetNoticeReceiptInvarianceTest(unittest.TestCase):
    """SD-116 (b)/D47-8, symmetric to claude_session_supervisor.test.py's
    identically-named class."""

    RECEIPT = {
        "schema_version": 2,
        "state": "ready",
        "parent_attempt_id": PARENT,
        "children": [],
    }

    def _compact(self, prompt: str) -> str:
        marker = "Runtime completion receipt (typed supervisor data, not child output): "
        start = prompt.index(marker) + len(marker)
        end = prompt.index("\n", start)
        return prompt[start:end]

    def test_notice_present_or_absent_leaves_compact_receipt_bytes_identical(self):
        module = load_supervisor_module()
        without_notice = module.completion_prompt(dict(self.RECEIPT))
        with_notice = module.completion_prompt(
            dict(self.RECEIPT), notice="[continuation-budget-warning] remaining=2 (warning threshold=3)."
        )
        self.assertEqual(self._compact(without_notice), self._compact(with_notice))
        self.assertNotEqual(without_notice, with_notice)
        self.assertIn("[continuation-budget-warning]", with_notice)
        self.assertNotIn("[continuation-budget-warning]", without_notice)


class BudgetWarningDeliveryTest(unittest.TestCase):
    """SD-116 (b) D47-5, symmetric to claude_session_supervisor.test.py's
    identically-named class."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_root = Path(self.temp.name)
        self.module = load_supervisor_module()

    def test_admit_returns_notice_only_on_the_crossing_turn(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_continuation_budget as BUDGET
        budget = BUDGET.ContinuationBudget(limit=5, source="test")
        ledger = BUDGET.ContinuationLedger(budget)
        notices = []
        for ordinal in range(4):
            verdict, notice = self.module._admit_continuation(
                ledger, self.state_root, parent_attempt_id="att-p",
                route_id="rt-x", route_hash="sha256:" + "a" * 64,
                ordinal=ordinal, purpose="ordinary", stalled=False,
                warning_threshold=3,
            )
            self.assertTrue(verdict.admitted)
            notices.append(notice)
        self.assertEqual(["", notices[1], "", ""], notices)
        self.assertTrue(notices[1])
        self.assertIn("remaining=", notices[1])


class ReservationForcedFailureTest(unittest.TestCase):
    """D47-3, symmetric to claude_session_supervisor.test.py's identically-
    named class."""

    def test_forced_reservation_write_failure_refuses_and_spends_nothing(self):
        module = load_supervisor_module()
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_continuation_budget as BUDGET
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            budget = BUDGET.ContinuationBudget(limit=5, source="test")
            ledger = BUDGET.ContinuationLedger(budget)
            with mock.patch.object(module.budget_record, "_append", return_value=False):
                verdict, notice = module._admit_continuation(
                    ledger, state_root, parent_attempt_id="att-p",
                    route_id="rt-x", route_hash="sha256:" + "c" * 64,
                    ordinal=0, purpose="ordinary", stalled=False,
                )
            self.assertFalse(verdict.admitted)
            self.assertEqual(verdict.refusal, "continuation-budget-unavailable")
            self.assertEqual(ledger.gross_remaining, budget.ordinary)
            self.assertEqual("", notice)


def _terminal_handoff_args(threshold=3):
    return SimpleNamespace(
        parent_attempt_id="att-p", route_id="rt-x",
        route_hash="sha256:" + "d" * 64,
        continuation_warning_threshold=threshold,
    )


class TerminalHandoffCleanupTurnBoundaryTest(unittest.TestCase):
    """impl-review round 1 finding 1, symmetric to
    claude_session_supervisor.test.py's identically-named class:
    `_seal_terminal_handoff_or_raise()` reuses the just-refused ordinary
    admit's `ordinal`. Before the fix, `dispatch_budget_record.reserve()`'s
    CAS key was `(parent_attempt_id, ordinal)` alone, so the
    terminal-handoff reservation collided with the already-appended
    `purpose="ordinary"` reservation at that same ordinal and was refused as
    `reservation-lost` -- the SD-116 (c) 'one last cleanup turn' was never
    actually issued. Drives the real `_admit_continuation`/
    `_seal_terminal_handoff_or_raise` functions against a real tmpdir
    reservation ledger -- no mock stands in for the CAS check being
    regression-tested."""

    def test_exactly_one_cleanup_turn_then_second_cleanup_is_refused(self):
        module = load_supervisor_module()
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_continuation_budget as BUDGET
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            budget = BUDGET.ContinuationBudget(limit=3, source="test")
            ledger = BUDGET.ContinuationLedger(budget)
            terminal_handoff_issued = [False]
            common = dict(
                parent_attempt_id="att-p", route_id="rt-x",
                route_hash="sha256:" + "d" * 64,
            )
            verdict, _ = module._admit_continuation(
                ledger, state_root, ordinal=0, purpose="ordinary", stalled=False, **common,
            )
            self.assertTrue(verdict.admitted)
            verdict, _ = module._admit_continuation(
                ledger, state_root, ordinal=1, purpose="ordinary", stalled=False, **common,
            )
            self.assertTrue(verdict.admitted)
            self.assertEqual(1, ledger.gross_remaining)
            self.assertEqual(1, ledger.reserved_remaining)

            # Refused at the gross==reserved boundary. This still appends a
            # `purpose="ordinary"` reservation row at ordinal=2 even though
            # the ledger refuses the admit -- that append is the collision
            # source the fix must tolerate.
            verdict, _ = module._admit_continuation(
                ledger, state_root, ordinal=2, purpose="ordinary", stalled=False, **common,
            )
            self.assertFalse(verdict.admitted)

            # (a) exactly one budget-exhausted cleanup prompt is issued, at
            # the SAME ordinal the just-refused ordinary admit used.
            prompt = module._seal_terminal_handoff_or_raise(
                ledger, state_root, args=_terminal_handoff_args(), ordinal=2,
                failure_reason="continuation-limit-exceeded",
                terminal_handoff_issued=terminal_handoff_issued,
            )
            self.assertIn("final continuation turn", prompt)
            self.assertTrue(terminal_handoff_issued[0])

            # (b) reserved_remaining becomes 0.
            self.assertEqual(0, ledger.reserved_remaining)

            # (c) a second cleanup is refused and the supervisor terminates.
            with self.assertRaises(module.SupervisorError) as ctx:
                module._seal_terminal_handoff_or_raise(
                    ledger, state_root, args=_terminal_handoff_args(), ordinal=3,
                    failure_reason="continuation-limit-exceeded",
                    terminal_handoff_issued=terminal_handoff_issued,
                )
            self.assertEqual("continuation-limit-exceeded", str(ctx.exception))

            import dispatch_budget_record as BR
            rows = BR.read_rows(state_root, "att-p")
            reservations = [row for row in rows if row.get("record_kind") == "reservation"]
            terminal_reservations = [row for row in reservations if row["purpose"] == "terminal-handoff"]
            self.assertEqual(1, len(terminal_reservations))
            ordinary_at_ordinal_2 = [
                row for row in reservations if row["ordinal"] == 2 and row["purpose"] == "ordinary"
            ]
            self.assertEqual(1, len(ordinary_at_ordinal_2))


class NoticeRenderingIsSharedAcrossSupervisorsTest(unittest.TestCase):
    """Anti-duplication check (plan §4.5, risk 7-7): both supervisors render
    a budget notice through the one shared `dispatch_budget_record.render_notice()`
    -- verified by importing both supervisor modules and asserting their
    notice text is byte-identical for the same input, which is only possible
    if neither has its own local copy of the rendering logic."""

    def test_claude_and_codex_notice_strings_are_byte_identical(self):
        codex_module = load_supervisor_module()
        claude_spec = importlib.util.spec_from_file_location(
            "claude_session_supervisor_unit",
            ROOT / "utilities" / "claude-session-supervisor.py",
        )
        claude_module = importlib.util.module_from_spec(claude_spec)
        claude_spec.loader.exec_module(claude_module)

        codex_notice = codex_module.budget_record.render_notice(
            "budget-warning", remaining=2, threshold=3
        )
        claude_notice = claude_module.budget_record.render_notice(
            "budget-warning", remaining=2, threshold=3
        )
        self.assertEqual(codex_notice, claude_notice)
        self.assertIs(codex_module.budget_record.render_notice, claude_module.budget_record.render_notice)


class TerminalReconcileRefusalRecordTest(unittest.TestCase):
    """SD-115 axis 4 (round 2, review 🔴2): mirrors
    `claude_session_supervisor.test.py`'s `TerminalReconcileRefusalRecordTest`.
    The Claude adapter's `reconcile()` already writes a durable
    `log_delivery_refusal()` record when `reconcile_supervisor_terminal(...)`
    raises; the Codex adapter's identically-shaped `reconcile()` only called
    `emit()`, which vanishes with the process. This proves the Codex call
    site now leaves the same durable trace."""

    def test_supervisor_terminal_reconcile_failure_leaves_a_durable_refusal(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_completion_join as join

        module = load_supervisor_module()
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            args = argparse.Namespace(jobs=str(jobs), parent_attempt_id=PARENT)
            emitted = []
            with mock.patch.object(
                module, "reconcile_supervisor_terminal",
                side_effect=RuntimeError("simulated-terminal-reconcile-crash"),
            ), mock.patch.object(module, "emit", emitted.append):
                result = module.reconcile(args, terminal=None)

            self.assertFalse(result)
            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0]["type"], "dispatch.supervisor.error")
            self.assertIn("RuntimeError", emitted[0]["reason"])

            log = Path(td) / "logs" / join.PENDING_DELIVERY_LOG
            self.assertTrue(log.is_file())
            entries = [
                json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
            ]
            refusals = [e for e in entries if e["attempt_id"] == PARENT]
            self.assertEqual(len(refusals), 1)
            self.assertEqual(refusals[0]["reason"], emitted[0]["reason"])


if __name__ == "__main__":
    unittest.main()
