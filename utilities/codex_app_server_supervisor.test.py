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
import shlex
import subprocess
import sys
import tempfile
import time
import textwrap
from types import SimpleNamespace
import unittest
from unittest import mock
from split_module_fixture import patch_split


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
                if any(arg.startswith('model_context_window=') for arg in sys.argv):
                    record('server-start', argv=sys.argv[1:],
                           runtime_dir=os.environ.get('AGENT_RUNTIME_DIR'))
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
                            if os.environ.get('FAKE_FAIL_THREAD_RESUME') == '1':
                                send({'jsonrpc':'2.0','id':value['id'],
                                      'error':{'code':-32000,'message':'fixture resume failure'}})
                                continue
                        else:
                            record('thread-start')
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
                        if turns == 2 and os.environ.get('FAKE_HOLD_SECOND_TURN') == '1':
                            open(os.environ['FAKE_SECOND_TURN_READY'], 'w').close()
                            deadline = time.monotonic() + 30
                            while (not os.path.exists(os.environ['FAKE_SECOND_TURN_RELEASE'])
                                   and time.monotonic() < deadline):
                                time.sleep(.01)
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
                        git_item_file = os.environ.get('FAKE_GIT_ITEM_FILE')
                        if git_item_file:
                            with open(git_item_file, encoding='utf-8') as handle:
                                git_item = json.load(handle)
                            for routine_index in range(128):
                                routine = {
                                    'type':'commandExecution','id':f'cmd-routine-{routine_index}',
                                    'command':'git status --short','cwd':git_item.get('cwd'),
                                    'aggregatedOutput':('routine success' if routine_index % 2 == 0
                                                        else 'fatal: unrelated command failure'),
                                    'exitCode':(0 if routine_index % 2 == 0 else 1),
                                    'status':('completed' if routine_index % 2 == 0 else 'failed')}
                                send({'jsonrpc':'2.0','method':'item/completed','params':{
                                    'threadId':'thread-1','turnId':turn_id,'item':routine}})
                            incomplete = dict(git_item)
                            incomplete['id'] = 'cmd-incomplete'
                            incomplete.pop('exitCode', None)
                            send({'jsonrpc':'2.0','method':'item/completed','params':{
                                'threadId':'thread-1','turnId':turn_id,'item':incomplete}})
                            unknown_status = dict(git_item)
                            unknown_status['id'] = 'cmd-unknown-status'
                            unknown_status['status'] = 'unknown'
                            send({'jsonrpc':'2.0','method':'item/completed','params':{
                                'threadId':'thread-1','turnId':turn_id,'item':unknown_status}})
                            signal_exit = dict(git_item)
                            signal_exit['id'] = 'cmd-signal-exit'
                            signal_exit['exitCode'] = 130
                            send({'jsonrpc':'2.0','method':'item/completed','params':{
                                'threadId':'thread-1','turnId':turn_id,'item':signal_exit}})
                            for status in ('inProgress', 'declined'):
                                nonterminal = dict(git_item)
                                nonterminal['id'] = f'cmd-{status}'
                                nonterminal['status'] = status
                                send({'jsonrpc':'2.0','method':'item/completed','params':{
                                    'threadId':'thread-1','turnId':turn_id,'item':nonterminal}})
                            send({'jsonrpc':'2.0','method':'item/completed','params':{
                                'threadId':'foreign-thread','turnId':turn_id,'item':git_item}})
                            command_event = {'jsonrpc':'2.0','method':'item/completed','params':{
                                'threadId':'thread-1','turnId':turn_id,'item':git_item}}
                            send(command_event)
                            send(command_event)  # replay the same native callback id
                            record('command-result-replay', item_id=git_item.get('id'))
                            if os.environ.get('FAKE_EXPECT_STEER') == '1':
                                steer = json.loads(sys.stdin.readline())
                                record('turn-steer', request=steer)
                                if os.environ.get('FAKE_STEER_REJECT') == '1':
                                    send({'jsonrpc':'2.0','id':steer['id'],
                                          'error':{'code':-32000,'message':'turn expired'}})
                                else:
                                    send({'jsonrpc':'2.0','id':steer['id'],
                                          'result':{'turnId':turn_id}})
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

    def test_same_thread_turn_start_reprojects_and_records_bound_permission_evidence(self):
        import commit_policy

        module = load_supervisor_module()
        git_dir = Path(subprocess.check_output(
            ["git", "-C", str(self.base), "rev-parse", "--absolute-git-dir"], text=True
        ).strip())
        effective = self.base / "effective.json"
        effective.write_bytes(b"immutable launch effective bytes\n")
        original = effective.read_bytes()
        context = {
            "adapter": "codex", "attempt_id": PARENT, "worker_type": "owner",
            "dispatch_depth": 1, "capability": "autopilot-code", "worktree": str(self.base),
            "route_file": "", "route_id": "", "route_hash": "",
            "artifact_root": str(self.artifact_root), "artifact_output_dir": "",
            "base_sandbox": "workspace-write", "network_eligible": True,
        }

        class Server:
            def __init__(self):
                self.requests = []
                self.events = []
                self.turn = 0

            def request(self, method, params):
                self.requests.append((method, params))
                self.turn += 1
                return {"turn": {"id": f"turn-{self.turn}"}}

            def next_event(self):
                return {"method": "turn/completed", "params": {
                    "turn": {"id": f"turn-{self.turn}", "status": "completed"}}}

        server = Server()
        args = argparse.Namespace(
            parent_attempt_id=PARENT, worktree=str(self.base), sandbox="workspace-write",
            network_access=True, writable_root=[], base_writable_root=[], approval="inherit",
            model=None, reasoning=None, owner_input=None, native_permission_profile=None,
        )
        trace = []
        with mock.patch.object(commit_policy, "read_runtime_permission_context", return_value=context), \
             mock.patch.object(commit_policy, "runtime_permission_context_is_current", return_value=True), \
             mock.patch.object(module, "emit", side_effect=trace.append):
            for operation in (None, "merge", None):
                marker = git_dir / "MERGE_HEAD"
                if operation:
                    marker.write_text("fixture\n", encoding="utf-8")
                else:
                    marker.unlink(missing_ok=True)
                module.run_turn(server, thread_id="thread-stable", prompt="continue", args=args)

        params = [value for method, value in server.requests if method == "turn/start"]
        self.assertEqual([item["sandboxPolicy"]["type"] for item in params],
                         ["workspaceWrite", "readOnly", "workspaceWrite"])
        self.assertEqual([item["sandboxPolicy"]["networkAccess"] for item in params],
                         [True, False, True])
        rows = [item for item in trace if item.get("type") == "dispatch.supervisor.permission-projection"]
        self.assertEqual([(item["attempt_id"], item["thread_id"], item["turn_id"]) for item in rows],
                         [(PARENT, "thread-stable", f"turn-{index}") for index in (1, 2, 3)])
        self.assertEqual([item["git_operation"] for item in rows], ["none", "merge", "none"])
        self.assertEqual([item["read_enforcement"] for item in rows],
                         ["os-sandbox", "os-sandbox", "os-sandbox"])
        self.assertEqual(effective.read_bytes(), original)

    def command(self, *, broken_app: Path | None = None,
                model_context_window: str | None = None) -> list[str]:
        app = broken_app or self.app
        command = [
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
        if model_context_window is not None:
            command += ["--model-context-window", model_context_window]
        return command

    def run_supervisor(self, model_context_window: str | None = None,
                       **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "FAKE_TRACE": str(self.trace), **extra_env}
        return subprocess.run(
            self.command(model_context_window=model_context_window),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )

    def test_context_window_is_captured_by_supervised_server_across_resume(self):
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = self.run_supervisor(model_context_window="600000")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        launch = next(row for row in trace if row["event"] == "server-start")
        self.assertEqual(launch["argv"][-2:], ["-c", "model_context_window=600000"])
        self.assertEqual([row["turn"] for row in trace if row["event"] == "turn-start"], [1, 2])


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
        armed = {"node":"full-run","successors":["verify"]}
        sup = SimpleNamespace(poll_once=mock.Mock(),read_resource_watches=lambda *_: [armed],
            resource_evidence=lambda _: {"terminal":True,"quiescent":True,"succeeded":True,"liveness":"exited","exit_code":0},
            artifact_evidence=lambda _: {"checked":True,"missing":[]},runner=lambda:SimpleNamespace(read_sentinel=lambda _:0))
        ctx = (sup,route,SimpleNamespace(state=lambda:{"nodes":{"full-run":stage}}),
               [(armed,run)])
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
        armed = {"node":"full-run","successors":["verify"]}
        sup = SimpleNamespace(poll_once=poll,read_resource_watches=lambda *_: [armed],
            resource_evidence=lambda _: {"terminal":True,"quiescent":True,"succeeded":True,"liveness":"exited","exit_code":0},
            artifact_evidence=lambda _: {"checked":True,"missing":[]},runner=lambda:SimpleNamespace(read_sentinel=lambda _:0))
        ledger = SimpleNamespace(state=lambda:{"nodes":{"full-run":stage}})
        ctx = (sup,route_value,ledger,[(armed,row)])
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

    def test_progress_carrying_resource_settlements_do_not_spend_the_continuation_budget(self):
        # Phase F d6 (F-1), symmetric to claude_session_supervisor.test.py's
        # identically-named fixture: a series of settled supervised resources
        # keeps reaching the owner under `--max-continuations 2`; the old
        # gross accounting refused the second delivery with exit 70.
        import dispatch_resource_wait as RESOURCE
        from types import SimpleNamespace
        module = load_supervisor_module()
        self.jobs.write_text(owner_row(self.lease))
        route_value = seal_route({"schema_version":2,"cwd":str(self.base),
            "nodes":[{"id":"full-run","kind":"resource-runner","continuation":{"kind":"supervised"}},
                     {"id":"verify"}]})
        route_path = self.base / "resource-series-route.json"
        route_path.write_text(json.dumps(route_value))
        route_args = ["--route-file",str(route_path),"--route-id",route_value["route_id"],
                      "--route-hash",route_value["route_hash"],"--max-continuations","2"]
        resource_count = 5
        runs = [{"run_id":f"series-resource-{index}","pid":600+index,"starttime":"201",
                 "command":["approved"],"node":"full-run","status":"running",
                 "sentinel":"/fixture-exit"} for index in range(resource_count)]
        keys = [RESOURCE.resource_key(run) for run in runs]
        stage = {"state":"STAGE_SUCCEEDED"}
        def remaining_runs():
            state = RESOURCE.JOIN.read_supervisor_phase_state(self.state, PARENT)
            delivered = set(((state.resource or {}).get("delivered") or [])) if state else set()
            return [run for run, key in zip(runs, keys) if key not in delivered]
        def context(*_):
            outstanding = remaining_runs()
            if not outstanding:
                return None
            run = outstanding[0]
            sup = SimpleNamespace(poll_once=mock.Mock(),read_resource_watches=lambda *_: [{"node":"full-run"}],
                resource_evidence=lambda _: {"terminal":True,"quiescent":True,"succeeded":True,"liveness":"exited","exit_code":0},
                artifact_evidence=lambda _: {"checked":True,"missing":[]},runner=lambda:SimpleNamespace(read_sentinel=lambda _:0))
            return (sup,route_value,SimpleNamespace(state=lambda:{"nodes":{"full-run":stage}}),
                    [({"node":"full-run","successors":["verify"]},run)])
        turns = []
        def turn(*a,**k):
            prompt = k["prompt"]
            turns.append(prompt)
            text = ("runtime_wait: registered-children" if len(turns)<=resource_count
                    else "artifact: -\nverdict: PASS\nblocker: none")
            return text,{}
        events = []
        with mock.patch.object(RESOURCE,"context",side_effect=context), \
             mock.patch.object(RESOURCE.RESUME,"supervisor_alive",return_value=True), \
             mock.patch.object(module,"run_turn",side_effect=turn), \
             mock.patch.object(module,"reconcile",return_value=True), \
             mock.patch.object(sys,"stdin",io.StringIO("initial assignment")), \
             mock.patch.object(module,"emit",side_effect=events.append), \
             mock.patch.object(module,"AppServer") as app:
            app.return_value.request.return_value={"thread":{"id":"same-native"}}
            rc = module.main(self.command()[2:]+route_args)
        errors = [event for event in events if event.get("type") == "dispatch.supervisor.error"]
        self.assertEqual(rc,0,errors)
        self.assertEqual(errors,[])
        self.assertEqual(len(turns),resource_count+1)
        resource_prompts = [prompt for prompt in turns if prompt.startswith("Runtime resource receipt")]
        self.assertEqual(len(resource_prompts),resource_count)
        for run in runs:
            self.assertTrue(any(run["run_id"] in prompt for prompt in resource_prompts),run["run_id"])
        import dispatch_budget_record as BR
        reservations = [row for row in BR.read_rows(self.base,PARENT)
                        if row.get("record_kind") == "reservation"]
        self.assertEqual(len(reservations),resource_count,reservations)
        self.assertEqual({row["class"] for row in reservations},{"gross"})
        self.assertEqual({row["gross_remaining"] for row in reservations},{2})

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
        args.owner_runtime_permission_state={"observation_only":True,"may_commit":False}
        args.owner_turn_permission_profiles={
            "observation":"hearting_owner_observe",
            "commit":"hearting_linked_commit",
            "commit_no_network":"hearting_linked_commit_no_network",
        }
        args.native_permission_profile={"default_permissions":"hearting_owner_observe",
            "permissions":{"hearting_owner_observe":{"extends":":read-only","filesystem":{},
                "network":{"enabled":False}},
                "hearting_linked_commit":profile["permissions"]["hearting_linked_commit"],
                "hearting_linked_commit_no_network":profile["permissions"]["hearting_linked_commit"]}}
        args.sandbox="read-only"
        command,_=module.resource_sandbox_command(args,row)
        self.assertEqual(command[command.index("-P")+1],"hearting_owner_observe")
        args.owner_runtime_permission_state={"observation_only":False,"may_commit":True}
        args.network_access=False
        command,_=module.resource_sandbox_command(args,row)
        self.assertEqual(command[command.index("-P")+1],"hearting_linked_commit_no_network")
        args.owner_turn_permission_profiles=None
        args.native_permission_profile=profile
        args.sandbox="read-only"
        command,_=module.resource_sandbox_command(args,row)
        self.assertEqual(command[command.index("-P")+1],":read-only")
        args.owner_runtime_permission_state=None
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

    def test_host_payload_row_alone_leaves_the_owner_profile_for_its_command(self):
        module=load_supervisor_module()
        route={"capability":"autopilot-code","owner_dispatch_depth":1,"cwd":str(self.base),
            "artifact_root":str(self.artifact_root),"selection":{},
            "nodes":[{"id":"execute","kind":"pipeline-stage"},
                {"id":"autopilot-lab-host-run","part":"autopilot-lab:host-run","kind":"resource-runner",
                 "resource_class":"long-running","resource_transport":"detached-process",
                 "continuation":{"kind":"supervised"}}]}
        route_path=self.base/"host-route.json"
        route_path.write_text(json.dumps(route))
        profile={"default_permissions":"hearting_linked_commit","permissions":{"hearting_linked_commit":{
            "extends":":workspace","filesystem":{str(self.base/".git/config"):"read"},"network":{"enabled":False}}}}
        args=SimpleNamespace(sandbox="workspace-write",network_access=False,worktree=str(self.base),
            writable_root=[],native_permission_profile=profile,owner_turn_permission_profiles=None,
            owner_runtime_permission_state=None)
        before=dict(vars(args))
        payload=["systemd-run","--user","--scope","-p","MemoryMax=64M","-p","MemorySwapMax=0","true"]
        row={"cwd":str(self.base/"payload"),"command":payload,"route":str(route_path),
             "node":"autopilot-lab-host-run","jobs":str(self.jobs)}
        env={k:v for k,v in os.environ.items() if k not in {
            "CODEX_DISPATCH_SANDBOX","CODEX_DISPATCH_SANDBOX_FORCE","AGENT_DISPATCH_EXECUTION_ACCESS_FILE"}}
        with mock.patch.dict(os.environ,env,clear=True):
            command,selection=module.resource_sandbox_command(args,row)
            self.assertEqual(command,["codex","sandbox","-P",":danger-full-access","--include-managed-config",
                "-C",str(self.base),"--","/bin/sh","-c",'cd -- "$1" || exit; shift; exec "$@"',
                "resource-payload",row["cwd"],*payload])
            self.assertEqual((selection["mode"],selection["enforcement"],selection["scope"]),
                ("danger-full-access","none","host-process-payload"))
            self.assertNotIn("--inherit-pid-namespace",command)
            self.assertEqual(vars(args),before)
            self.assertEqual(args.sandbox,"workspace-write")
            # The same builder keeps every other row, and a row with no route, on the owner profile.
            for other in ({**row,"node":"execute"},{**row,"node":"missing"},
                          {k:v for k,v in row.items() if k not in {"route","node"}},
                          {**row,"route":str(self.base/"absent.json")}):
                command,selection=module.resource_sandbox_command(args,other)
                self.assertEqual(command[command.index("-P")+1],"hearting_linked_commit",other)
                self.assertEqual(selection,{"mode":"workspace-write","enforcement":"os-sandbox","network_access":False})
            # A caller-forced sandbox stays the owner's, so the host limit stays visible.
            with mock.patch.dict(os.environ,{"CODEX_DISPATCH_SANDBOX_FORCE":"workspace-write"}):
                command,selection=module.resource_sandbox_command(args,row)
                self.assertEqual(command[command.index("-P")+1],"hearting_linked_commit")
                self.assertEqual(selection["enforcement"],"os-sandbox")
            # An invalid caller value stays on the owner profile; the runner start check refuses it.
            with mock.patch.dict(os.environ,{"CODEX_DISPATCH_SANDBOX_FORCE":"bogus"}):
                command,selection=module.resource_sandbox_command(args,row)
                self.assertEqual(command[command.index("-P")+1],"hearting_linked_commit")
                self.assertEqual(selection["enforcement"],"os-sandbox")
            # The builder never raises for access; the runner start check settles a strict request.
            request=self.base/"access-request.json"
            request.write_text(json.dumps({"schema_version":1,"writable_roots":[],"read_roots":[],
                "network":{"required":False,"reason":"","hosts":[]},"enforcement_required":"os-sandbox",
                "justification":{}}))
            with mock.patch.dict(os.environ,{"AGENT_DISPATCH_EXECUTION_ACCESS_FILE":str(request)}):
                command,selection=module.resource_sandbox_command(args,row)
                self.assertEqual(selection["mode"],"danger-full-access")
                command,_=module.resource_sandbox_command(args,{**row,"node":"execute"})
                self.assertEqual(command[command.index("-P")+1],"hearting_linked_commit")
                request.write_text(request.read_text().replace("os-sandbox","any"))
                command,selection=module.resource_sandbox_command(args,row)
                self.assertEqual(selection["enforcement"],"none")

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
            read_resource_watches=lambda *_: [armed],
            resource_evidence=lambda _: {"terminal": True, "quiescent": True, "succeeded": False,
                                         "liveness": "exited", "exit_code": None},
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
            read_resource_watches=lambda *_: [armed],
            resource_continuation_cancelled=lambda *_: False, _evaluate=mock.Mock(),
            resource_evidence=lambda _: {"terminal": True, "quiescent": True, "succeeded": False,
                                         "liveness": "exited", "exit_code": None},
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

    def test_bound_consumer_resume_failure_leaves_open_row_and_state_untouched(self):
        import dispatch_owner_input as owner_input
        from dispatch_contract import hold_supervisor_lease

        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        owner_input.initialize_owner_input(self.jobs, PARENT, "codex-active-turn")
        with hold_supervisor_lease(self.jobs, PARENT, self.lease):
            owner_input.OwnerInput(self.jobs, PARENT, "thread-1", "codex-active-turn", lambda _e: None)
        resource = {"session_id": "thread-1", "delivered": [], "outbox": None}
        module = load_supervisor_module()
        module.write_supervisor_state(self.state, PARENT, set(), phase="recovery")
        with module.RESOURCE_WAIT.JOIN._supervisor_state_lock(self.state):
            module.RESOURCE_WAIT.JOIN._write_supervisor_state_unlocked(
                self.state, PARENT, set(), phase="recovery", resource=resource)
        jobs_before = self.jobs.read_bytes()
        state_before = self.state.read_bytes()

        result = self.run_supervisor(FAKE_FAIL_THREAD_RESUME="1")

        self.assertEqual(result.returncode, 75, result.stderr + result.stdout)
        self.assertIn("dispatch.supervisor.consumer-recovery-failed", result.stdout)
        self.assertEqual(self.jobs.read_bytes(), jobs_before)
        self.assertEqual(self.state.read_bytes(), state_before)
        self.assertFalse(owner_input.supervisor_lease_is_held(self.jobs, owner_input._target(
            self.jobs, PARENT)[0].metadata))
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(sum(row.get("event") == "thread-resume" for row in trace), 1)
        self.assertEqual(sum(row.get("event") == "turn-start" for row in trace), 0)
        self.assertEqual(sum(row.get("event") == "thread-start" for row in trace), 0)

    def test_bound_consumer_lease_contention_leaves_open_row_and_state_untouched(self):
        import dispatch_owner_input as owner_input
        from dispatch_contract import hold_supervisor_lease

        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        owner_input.initialize_owner_input(self.jobs, PARENT, "codex-active-turn")
        with hold_supervisor_lease(self.jobs, PARENT, self.lease):
            owner_input.OwnerInput(self.jobs, PARENT, "thread-1", "codex-active-turn", lambda _e: None)
        resource = {"session_id": "thread-1", "delivered": [], "outbox": None}
        module = load_supervisor_module()
        module.write_supervisor_state(self.state, PARENT, set(), phase="recovery")
        with module.RESOURCE_WAIT.JOIN._supervisor_state_lock(self.state):
            module.RESOURCE_WAIT.JOIN._write_supervisor_state_unlocked(
                self.state, PARENT, set(), phase="recovery", resource=resource)
        jobs_before = self.jobs.read_bytes()
        state_before = self.state.read_bytes()
        with hold_supervisor_lease(self.jobs, PARENT, self.lease):
            result = self.run_supervisor(FAKE_NO_CHILD="1")
        self.assertEqual(result.returncode, 75, result.stderr + result.stdout)
        self.assertIn("dispatch.supervisor.consumer-recovery-failed", result.stdout)
        self.assertEqual(self.jobs.read_bytes(), jobs_before)
        self.assertEqual(self.state.read_bytes(), state_before)
        self.assertFalse(self.trace.exists())

    def test_first_resource_receipt_after_restart_uses_next_durable_ordinal(self):
        import dispatch_owner_input as owner_input
        import dispatch_budget_record as budget_record
        from dispatch_contract import hold_supervisor_lease

        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        owner_input.initialize_owner_input(self.jobs, PARENT, "codex-active-turn")
        with hold_supervisor_lease(self.jobs, PARENT, self.lease):
            owner_input.OwnerInput(self.jobs, PARENT, "thread-1", "codex-active-turn", lambda _e: None)
        module = load_supervisor_module()
        resource = {"session_id": "thread-1", "delivered": [], "outbox": None}
        module.write_supervisor_state(self.state, PARENT, set(), phase="recovery")
        with module.RESOURCE_WAIT.JOIN._supervisor_state_lock(self.state):
            module.RESOURCE_WAIT.JOIN._write_supervisor_state_unlocked(
                self.state, PARENT, set(), phase="recovery", resource=resource)
        remaining = {"gross_remaining": 12, "stall_remaining": 12,
                     "reserved_remaining": 1}
        self.assertTrue(budget_record.reserve(
            self.jobs.parent, parent_attempt_id=PARENT, route_id="rt-ledger",
            route_hash="sha256:" + "c" * 64, ordinal=0, purpose="ordinary",
            klass="gross", remaining=remaining)[0])
        self.assertTrue(budget_record.reserve(
            self.jobs.parent, parent_attempt_id=PARENT, route_id="rt-ledger",
            route_hash="sha256:" + "c" * 64, ordinal=1, purpose="ordinary",
            klass="gross", remaining=remaining)[0])

        turns = []
        resource_waits = []
        injected_outbox = [False]
        original_read = module.read_supervisor_phase_state

        class FakeServer:
            process = None

            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, method, _params):
                if method == "thread/resume":
                    return {"thread": {"id": "thread-1"}}
                return {"ok": True}

            def notification(self, *_args, **_kwargs):
                pass

            def close(self):
                pass

        def fake_read(path, attempt):
            if injected_outbox[0]:
                return SimpleNamespace(resource={"outbox": {"receipt_id": "resource-fixture"}},
                                       delivered_attempt_ids=set(), outbox=None)
            return original_read(path, attempt)

        def fake_wait(*_args, **_kwargs):
            resource_waits.append(True)
            if len(resource_waits) == 2:
                injected_outbox[0] = True
                return "fixture resource receipt"
            return None

        def fake_turn(_server, *, thread_id, prompt, args, activity):
            turns.append((thread_id, prompt))
            if len(turns) == 1:
                return "runtime_wait: registered-children", None
            return "artifact: -\nverdict: PASS\nblocker: none", None

        argv = self.command() [2:] + ["--route-id", "rt-ledger",
                                     "--route-hash", "sha256:" + "c" * 64]
        with mock.patch.object(module, "AppServer", FakeServer), \
             mock.patch.object(module, "run_turn", side_effect=fake_turn), \
             mock.patch.object(module, "read_supervisor_phase_state", side_effect=fake_read), \
             mock.patch.object(module.RESOURCE_WAIT, "wait", side_effect=fake_wait), \
             mock.patch.object(module.RESOURCE_WAIT, "pending_prompt", return_value="receipt prompt"), \
             mock.patch.object(module.RESOURCE_WAIT, "acknowledge", return_value=True), \
             mock.patch.object(module.RESOURCE_WAIT, "needs_recovery", return_value=True), \
             mock.patch.object(module, "reconcile", return_value=False), \
             mock.patch.object(module, "emit", lambda _event: None), \
             mock.patch("foreground_activity.owner_turn_activity", return_value=None), \
             mock.patch("sys.stdin", io.StringIO("same-thread recovery prompt")):
            result = module.main(argv)

        self.assertEqual(result, 70)
        self.assertEqual([thread for thread, _prompt in turns], ["thread-1", "thread-1"])
        rows = budget_record.read_rows(self.jobs.parent, PARENT)
        reservations = [row for row in rows if row.get("record_kind") == "reservation"]
        self.assertEqual([row["ordinal"] for row in reservations], [0, 1, 2])
        self.assertEqual(reservations[-1]["purpose"], "ordinary")

    def test_public_correct_respawns_and_completes_on_same_thread_without_touching_resource(self):
        self._public_correct_respawn_fixture(legacy_record=False)

    def test_legacy_v3532_public_correct_respawns_from_bound_provenance(self):
        self._public_correct_respawn_fixture(legacy_record=True)

    def test_legacy_public_correct_preserves_runtime_with_additional_writable_grant(self):
        self._public_correct_respawn_fixture(legacy_record=True, additional_grant=True)

    def test_respawned_owner_runs_its_utilities_and_hooks_on_the_governing_release(self):
        # T12: the recorded launch_home is where the worker home came from (an earlier
        # release); the respawned child's AGENT_HOME is the release that governs it now.
        self._public_correct_respawn_fixture(legacy_record=False, earlier_launch_home=True)

    def _public_correct_respawn_fixture(self, *, legacy_record, additional_grant=False,
                                        earlier_launch_home=False):
        import contextlib
        import dispatch_completion_join as join
        import dispatch_budget_record as budget_record
        import dispatch_contract as contract
        import dispatch_owner_input as owner_input
        import dispatch_replacement
        import dispatch_resource_wait as owner_resource
        import execution_access
        import route_authority
        import route_parent_close
        from types import SimpleNamespace
        from worker_runtime_home import prepare_worker_home

        base = self.base
        jobs = base / "dispatch" / "jobs.log"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        artifact_root = base / ".agent_reports"
        artifact_root.mkdir(exist_ok=True)
        runtime_dir = base / ".agent_runtime"
        runtime_dir.mkdir(exist_ok=True)
        compute_root = None
        if additional_grant:
            compute_scratch = tempfile.TemporaryDirectory(prefix="hearting-compute-grant-")
            self.addCleanup(compute_scratch.cleanup)
            compute_root = Path(compute_scratch.name) / "opaque-output"
            compute_root.mkdir()
            self.assertFalse(compute_root.is_relative_to(base))
        home = base / "home"
        home.mkdir(exist_ok=True)
        recorded_release = ROOT
        if earlier_launch_home:
            # A tree of links to the same content, at its own path, stands in for the earlier release.
            recorded_release = base / "earlier-release"
            recorded_release.mkdir()
            for entry in ROOT.iterdir():
                if entry.name != ".git":
                    (recorded_release / entry.name).symlink_to(entry)
        xdg_state = base / "xdg-state"
        xdg_state.mkdir(exist_ok=True)
        logs = jobs.parent / "logs"
        logs.mkdir(exist_ok=True)
        native_codex_home = home / ".codex" if legacy_record else base / "native-codex"
        native_codex_home.mkdir(exist_ok=True)
        (native_codex_home / "config.toml").write_text('model = "gpt-fixture"\n', encoding="utf-8")
        worker_home = (jobs.parent / "codex-owner-home" if legacy_record
                       else runtime_dir / "codex-owner-home")
        route_path = base / "owner-route.json"
        attempt = PARENT
        parent_session = "fixture-parent"
        log_path = logs / f"owner.{attempt}.codex.jsonl"
        summary_prompt = logs / f"owner.{attempt}.codex.prompt.txt"
        summary_prompt.write_text("sealed original task prompt", encoding="utf-8")
        correction_path = base / "correction.txt"
        correction_path.write_text("resume the existing checkpoint on the same GPU", encoding="utf-8")
        second_turn_ready = base / "second-turn-ready"
        second_turn_release = base / "second-turn-release"
        resource_process = subprocess.Popen(["sleep", "60"], start_new_session=True)
        def stop_resource_fixture():
            if resource_process.poll() is None:
                resource_process.terminate()
                resource_process.wait(timeout=5)
        self.addCleanup(stop_resource_fixture)
        resource_results = base / "resource-results.json"
        resource_results.write_text('{"checkpoint":"same-live-result"}\n', encoding="utf-8")
        resource_registry = base / "resource-registry.json"
        resource_registry.write_text(json.dumps({"runs": {"cpu-run": {
            "run_id": "cpu-run", "pid": resource_process.pid,
            "starttime": contract.process_start_ticks(resource_process.pid),
            "status": "running", "result_file": str(resource_results),
            "outputs_sha256": hashlib.sha256(resource_results.read_bytes()).hexdigest(),
        }}}), encoding="utf-8")
        resource_registry_before = hashlib.sha256(resource_registry.read_bytes()).hexdigest()
        resource_results_before = hashlib.sha256(resource_results.read_bytes()).hexdigest()

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home), "XDG_STATE_HOME": str(xdg_state),
            "AGENT_HOME": str(ROOT),
            "AGENT_RUNTIME_DIR": str(base / ".caller-runtime" if legacy_record else runtime_dir),
            "AGENT_DISPATCH_JOBS": str(jobs),
            "AGENT_MODEL_GOVERNOR_ROOT": str(artifact_root / ".runtime/model-worker-governor"),
            "FLEET_TITLE_STATE_DIR": str(base / "fleet-titles"),
            "CODEX_THREAD_ID": parent_session,
            "CODEX_APP_SERVER_COMMAND": f"{sys.executable} {self.app}",
            "FAKE_TRACE": str(self.trace), "FAKE_HOLD_SECOND_TURN": "1",
            "FAKE_SECOND_TURN_READY": str(second_turn_ready),
            "FAKE_SECOND_TURN_RELEASE": str(second_turn_release),
            "TMPDIR": str(base),
        }
        def load_local(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            loaded = importlib.util.module_from_spec(spec)
            sys.modules[name] = loaded
            spec.loader.exec_module(loaded)
            return loaded

        route_compiler = load_local("respawn_route_compiler", ROOT / "utilities/capability-route.py")
        with mock.patch.dict(os.environ, env, clear=True):
            route = route_compiler.compile_route(
                capability="autopilot-code", capability_mode="dev", requested_intensity="direct",
                cwd=str(base), artifact_root=str(artifact_root),
                predicates=["atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
                            "no-artifact-handoff", "no-independent-verifier", "focused-verification"],
                signals=[], transport=None, inline_reason="atomic-direct", tracking="tracked",
                tracked_gate_evidence={
                    "spec_read": {"satisfied": True, "source": "fixture-prd"},
                    "drift_verdict": "within-spec", "workflow_mode": "tracked",
                    "artifact_guard": {"satisfied": True, "source": "fixture-prechecked"},
                },
            )
            route_path.write_text(json.dumps(route), encoding="utf-8")
            route_compiler.verify_route(route, str(base))

            prepare_worker_home(
                recorded_release, "codex", "owner", attempt,
                env={"HOME": str(home), "CODEX_HOME": str(native_codex_home),
                     "AGENT_DISPATCH_JOBS": str(jobs)},
                destination=worker_home,
            )
            if legacy_record:
                # v3.53.2 schema-2 homes omitted both derived paths. The
                # existing absolute config symlink remains the provenance.
                manifest_path = worker_home / "worker-home.json"
                legacy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(legacy_manifest.get("schema"), 2)
                legacy_manifest.pop("codex_config_home", None)
                legacy_manifest.pop("home_environment", None)
                manifest_path.write_text(json.dumps(legacy_manifest), encoding="utf-8")
                self.assertTrue((worker_home / "config.toml").is_symlink())
                self.assertFalse(worker_home.is_relative_to(runtime_dir))
            def home_snapshot():
                snapshot = {}
                for path in sorted(worker_home.rglob("*")):
                    rel = str(path.relative_to(worker_home))
                    if path.is_symlink():
                        snapshot[rel] = ("link", os.readlink(path))
                    elif path.is_file():
                        snapshot[rel] = ("file", path.read_bytes())
                return snapshot
            worker_home_before = home_snapshot()
            from worker_runtime_home import recorded_codex_worker_environment
            recorded_environment_before = recorded_codex_worker_environment(
                recorded_release, worker_home, attempt)
            lease = contract.supervisor_lease_path(jobs, attempt)
            model_settings = {"source": "explicit", "model": "gpt-fixture", "reasoning": "high",
                              "context_window": "200000", "role": "owner", "profile": "balanced",
                              "tier": "balanced", "granularity": "model"}
            binding = SimpleNamespace(route_file=str(route_path), route_id=route["route_id"],
                                      route_hash=route["route_hash"])
            default_writable_roots = (artifact_root, runtime_dir, jobs.parent)
            access_grant = None
            access_argv = []
            if additional_grant:
                access_file = base / "execution-access-request.json"
                access_file.write_text(json.dumps({
                    "schema_version": 1, "writable_roots": [str(compute_root)],
                    "read_roots": [],
                    "network": {"required": False, "reason": "", "hosts": []},
                    "enforcement_required": "os-sandbox",
                    "justification": {str(compute_root): "fixture compute output"},
                }), encoding="utf-8")
                access_context = execution_access.AccessContext.build(
                    worktree=base, artifact_root=artifact_root, dispatch_state_root=jobs.parent,
                    agent_home=ROOT, environ=env)
                access_request = execution_access.load_request(access_file, context=access_context)
                access_grant = execution_access.build_grant(
                    access_request, runtime="codex-app-server",
                    default_writable_roots=default_writable_roots,
                    effective_sandbox="workspace-write")
                self.assertEqual(access_grant.additional_writable_roots, (compute_root,))
                self.assertEqual(access_grant.absorbed_writable_roots, ())
                access_argv = ["--execution-access-file", str(access_file)]
            launch_args = SimpleNamespace(
                attempt_id=attempt, jobs_path=jobs, worktree=str(base), replacement_input_argv=[
                    "--approval", "never", "--sandbox", "workspace-write", "--max-continuations", "20",
                    *access_argv],
                capability="autopilot-code", capability_mode="dev", unit="", worker_type="owner",
                worker_mode="", assigned_contract="capability-owner", dispatch_depth=1,
                intensity="standard", qa="standard", sandbox="workspace-write", permission_mode="sandboxed",
                parent_harness="codex", parent_transport="headless", parent_sandbox="workspace-write",
                launch_authority="conductor", parent_session_id=parent_session, parent_attempt_id=None,
                execution_surface="registered-headless", registered_worker=True,
                fallback_hop="same-harness-headless", model_role="owner", model_profile="balanced",
                model="gpt-fixture", reasoning="high", resolved_model_settings=model_settings,
                runtime_dir=str(runtime_dir), resolved_completion_delivery="app-server-supervised",
                parent_completion_delivery="codex-managed-gateway", approval="never",
                launch_lifecycle="detached", nested_headless_network=False, profile=None,
                owner_route_binding=binding, route_id=route["route_id"], route_node="",
                route_file=str(route_path), route_hash=route["route_hash"], artifact_root=str(artifact_root),
                replacement_retry_brief="", execution_access_grant=access_grant,
            )
            input_fragment = dispatch_replacement.seal_launch_input(
                launch_args, "codex", "ORIGINAL_TASK_SENTINEL must not be replayed")
            launch_input_path = (dispatch_replacement._directory(jobs) / "inputs"
                                 / f"{attempt}.json")
            sealed_launch = json.loads(launch_input_path.read_text(encoding="utf-8"))
            if additional_grant:
                recorded_access = sealed_launch["applied_permissions"]["execution_access"]
                self.assertEqual(recorded_access["additional_writable_roots"], [str(compute_root)])
                self.assertEqual(recorded_access["request_sha256"], access_request.request_sha256)
                self.assertEqual(recorded_access["source_path"], str(access_file))
            if earlier_launch_home:
                sealed_launch["launch_home"] = str(recorded_release)
            if legacy_record:
                sealed_launch["resolved"].pop("runtime_dir")
                sealed_launch["applied_permissions"].pop("approval", None)
                sealed_launch["applied_permissions"].pop("commit_policy", None)
            if legacy_record or earlier_launch_home:
                launch_input_path.write_bytes(dispatch_replacement._bytes(sealed_launch))
                old_digest = input_fragment.split("replacement_input_digest=", 1)[1].split(",", 1)[0]
                input_fragment = input_fragment.replace(
                    old_digest, dispatch_replacement._digest(sealed_launch), 1)
            effective_path, effective_digest = execution_access.publish_effective_grant(
                jobs=jobs, attempt_id=attempt, route_id=route["route_id"],
                route_hash=route["route_hash"], runtime="codex-app-server",
                sandbox="workspace-write", grant=access_grant,
                default_writable_roots=default_writable_roots,
                network_allowed=False,
            )
            verified_effective = json.loads(effective_path.read_text(encoding="utf-8"))
            if additional_grant:
                self.assertEqual(verified_effective["request_sha256"], access_request.request_sha256)
                self.assertEqual(verified_effective["request_path"], str(access_file))
                self.assertEqual(set(verified_effective["writable_roots"]),
                                 {str(root) for root in (*default_writable_roots, compute_root)})
            old_pid = "99999999"
            old_start = "100"
            metadata = {
                "attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "fallback_hop": "same-harness-headless", "worker_type": "owner", "harness": "codex",
                "completion_delivery": "app-server-supervised", "supervisor_lease": contract.SUPERVISOR_LEASE_KIND,
                "supervisor_lease_file": str(lease), "supervisor_lease_nonce": "d" * 64,
                "attempt_id": attempt, "launch_claimed": "1", "launch_started": "1",
                "launch_lifecycle": "detached", "launch_fence": "registry-v1",
                "pid": old_pid, "pid_start": old_start,
                "pid_ns": os.readlink("/proc/self/ns/pid"),
                "pid_observer_ns": os.readlink("/proc/self/ns/pid"), "pgid": old_pid,
                "runtime_sandbox": "workspace-write", "runtime_home": str(worker_home),
                "launch_home": str(recorded_release if earlier_launch_home else ROOT.resolve()),
                "artifact_root": str(artifact_root),
                "log_file": str(log_path), "approval": "never", "parent_sid": parent_session,
                "parent_harness": "codex", "parent_transport": "headless",
                "parent_sandbox": "workspace-write", "parent_cwd": str(base),
                "capability": "autopilot-code", "capability_mode": "dev", "intensity": "standard",
                "qa": "standard", "assigned_contract": "capability-owner", "owner": "autopilot-code",
                "owner_harness": "codex", "owner_route_file": str(route_path),
                "owner_route_id": route["route_id"], "owner_route_hash": route["route_hash"],
                "route_file": str(route_path), "route_id": route["route_id"],
                "route_hash": route["route_hash"], "model_source": "explicit", "model_role": "owner",
                "model_profile": "balanced", "model": "gpt-fixture", "reasoning": "high",
                "completion_delivery_reason": "not-applicable",
                "execution_access_effective_file": str(effective_path),
                "execution_access_effective_sha256": effective_digest,
            }
            row_line = (f"2026-10-27T00:00:00Z\topen\t{base}\t{base}\towner\t"
                        + ",".join(f"{key}={value}" for key, value in metadata.items())
                        + input_fragment)
            jobs.write_text(row_line + "\n", encoding="utf-8")
            remaining = {"gross_remaining": 12, "stall_remaining": 12,
                         "reserved_remaining": 1}
            self.assertTrue(budget_record.reserve(
                jobs.parent, parent_attempt_id=attempt, route_id=route["route_id"],
                route_hash=route["route_hash"], ordinal=0, purpose="ordinary", klass="gross",
                remaining=remaining)[0])
            self.assertTrue(budget_record.reserve(
                jobs.parent, parent_attempt_id=attempt, route_id=route["route_id"],
                route_hash=route["route_hash"], ordinal=1, purpose="ordinary", klass="gross",
                remaining=remaining)[0])

            def settle_owned_attempt():
                try:
                    current = join.exact_attempt_row(jobs, attempt)
                    current_meta = current.metadata
                except Exception:
                    return
                pid = current_meta.get("pid", "")
                start = current_meta.get("pid_start", "")
                if not pid.isdigit() or not start or pid == old_pid:
                    return
                for _ in range(40):
                    if contract.process_start_ticks(int(pid)) != start:
                        break
                    time.sleep(0.05)
                if contract.process_start_ticks(int(pid)) == start:
                    pgid = current_meta.get("pgid", "")
                    if pgid == pid:
                        try:
                            os.killpg(int(pgid), 15)
                        except ProcessLookupError:
                            pass
                        for _ in range(20):
                            if contract.process_start_ticks(int(pid)) != start:
                                break
                            time.sleep(0.05)
                        if contract.process_start_ticks(int(pid)) == start:
                            try:
                                os.killpg(int(pgid), 9)
                            except ProcessLookupError:
                                pass
                summary_pid = current_meta.get("summary_owner_pid", "")
                summary_start = current_meta.get("summary_owner_pid_start", "")
                if (summary_pid.isdigit() and summary_start
                        and contract.process_start_ticks(int(summary_pid)) == summary_start):
                    try:
                        os.kill(int(summary_pid), 15)
                    except ProcessLookupError:
                        pass
                    for _ in range(40):
                        if contract.process_start_ticks(int(summary_pid)) != summary_start:
                            break
                        time.sleep(0.05)
                    if contract.process_start_ticks(int(summary_pid)) == summary_start:
                        try:
                            os.kill(int(summary_pid), 9)
                        except ProcessLookupError:
                            pass
                        for _ in range(20):
                            if contract.process_start_ticks(int(summary_pid)) != summary_start:
                                break
                            time.sleep(0.05)
                helper_ids = []
                for proc_dir in Path("/proc").iterdir():
                    if not proc_dir.name.isdigit():
                        continue
                    try:
                        if proc_dir.stat().st_uid != os.getuid():
                            continue
                        argv = [part.decode(errors="replace") for part in
                                (proc_dir / "cmdline").read_bytes().split(b"\0") if part]
                        stat_fields = (proc_dir / "stat").read_text().rsplit(") ", 1)[1].split()
                        start_ticks = stat_fields[19]
                    except (OSError, IndexError):
                        continue
                    joined = " ".join(argv)
                    if (str(log_path) in joined and attempt in joined
                            and ("/utilities/dispatch_summary.py" in joined
                                 or "/tools/fleet/refresh_title.py" in joined)):
                        helper_ids.append((int(proc_dir.name), start_ticks))
                for helper_pid, helper_start in helper_ids:
                    if contract.process_start_ticks(helper_pid) != helper_start:
                        continue
                    try:
                        os.kill(helper_pid, 15)
                    except ProcessLookupError:
                        continue
                    for _ in range(40):
                        if contract.process_start_ticks(helper_pid) != helper_start:
                            break
                        time.sleep(0.05)
                    if contract.process_start_ticks(helper_pid) == helper_start:
                        try:
                            os.kill(helper_pid, 9)
                        except ProcessLookupError:
                            pass
                        for _ in range(20):
                            if contract.process_start_ticks(helper_pid) != helper_start:
                                break
                            time.sleep(0.05)

            self.addCleanup(settle_owned_attempt)
            owner_input.initialize_owner_input(jobs, attempt, "codex-active-turn")
            with contract.hold_supervisor_lease(jobs, attempt, lease):
                owner_input.OwnerInput(jobs, attempt, "thread-1", "codex-active-turn", lambda _e: None)
            headless = load_local("respawn_legacy_dispatch_headless",
                                  ROOT / "adapters/codex/bin/dispatch-headless.py")
            if legacy_record and not additional_grant:
                effective = json.loads(effective_path.read_text(encoding="utf-8"))
                row_fields = row_line.rstrip("\n").split("\t")
                state_path = lease.with_suffix(".json")

                rendered = headless.recorded_supervisor_command(
                    sealed_launch, row_fields, metadata, jobs, state_path, lease,
                    None, log_path, effective, recorded_home=str(home))
                self.assertIn(str(runtime_dir), rendered)
                self.assertIn("--primary-git-commit", rendered)

                alternate_scratch = tempfile.TemporaryDirectory(prefix="hearting-alternate-runtime-")
                self.addCleanup(alternate_scratch.cleanup)
                alternate_runtime = Path(alternate_scratch.name) / "runtime"
                alternate_runtime.mkdir()
                absent = {**effective, "writable_roots": [
                    root for root in effective["writable_roots"] if root != str(runtime_dir)]}
                ambiguous = {**effective, "writable_roots": [
                    *effective["writable_roots"], str(alternate_runtime)]}
                explicit_conflict = json.loads(json.dumps(sealed_launch))
                explicit_conflict["resolved"]["runtime_dir"] = str(alternate_runtime)
                commit_conflict = json.loads(json.dumps(sealed_launch))
                commit_conflict["applied_permissions"]["commit_policy"] = {"may_commit": False}
                refusal_cases = (
                    (sealed_launch, absent, "runtime_dir"),
                    (sealed_launch, ambiguous, "runtime_dir-ambiguous"),
                    (explicit_conflict, effective, "execution_access_writable_roots:runtime_dir"),
                    (commit_conflict, effective, "commit_policy.may_commit-changed"),
                )
                refusal_launches = []
                refusal_real_popen = subprocess.Popen

                def refuse_only_launch_fence(*popen_args, **popen_kwargs):
                    command = popen_kwargs.get("args", popen_args[0] if popen_args else [])
                    parts = [command] if isinstance(command, (str, bytes)) else list(command)
                    if any(str(part).endswith("launch-fence.py") for part in parts):
                        refusal_launches.append(parts)
                    return refusal_real_popen(*popen_args, **popen_kwargs)

                with mock.patch.object(headless.subprocess, "Popen",
                                       side_effect=refuse_only_launch_fence):
                    for candidate_record, candidate_effective, expected_detail in refusal_cases:
                        with self.subTest(legacy_refusal=expected_detail):
                            with self.assertRaises(headless.DispatchContractError) as raised:
                                headless.recorded_supervisor_command(
                                    candidate_record, row_fields, metadata, jobs, state_path, lease,
                                    None, log_path, candidate_effective, recorded_home=str(home))
                            self.assertEqual(raised.exception.detail, expected_detail)
                self.assertEqual(refusal_launches, [])
                self.assertEqual(owner_input.inspect(jobs, attempt)["requests"], [])
                self.assertFalse(self.trace.exists())
            state_path = lease.with_suffix(".json")
            supervisor_module = load_supervisor_module()
            supervisor_module.write_supervisor_state(state_path, attempt, set(), phase="recovery")
            with join._supervisor_state_lock(state_path):
                join._write_supervisor_state_unlocked(
                    state_path, attempt, set(), phase="recovery",
                    resource={"session_id": "thread-1", "delivered": [], "outbox": None})

            route_authority_module = route_authority
            preserved = [{"kind": "resource", "run_id": "cpu-run",
                          "registry": str(resource_registry), "record": "cpu-run"}]
            cap = load_local("respawn_public_capability_route", ROOT / "utilities/capability-route.py")
            output = io.StringIO()
            respawn_outcomes = []
            launch_environments = []
            prompt_temp_dirs = []
            rendered_commands = []
            original_recover_consumer = owner_input.recover_consumer
            real_popen = subprocess.Popen
            real_temporary_file = tempfile.TemporaryFile
            real_recorded_command = headless.recorded_supervisor_command

            def capture_recovery(jobs_arg, attempt_arg, *, session=None, spawn=None):
                def capture_spawn(*spawn_args, **spawn_kwargs):
                    outcome = headless.respawn_supervised_owner(*spawn_args, **spawn_kwargs)
                    respawn_outcomes.append(outcome)
                    return outcome
                return original_recover_consumer(
                    jobs_arg, attempt_arg, session=session, spawn=spawn or capture_spawn)

            def capture_popen(*popen_args, **popen_kwargs):
                command = popen_kwargs.get("args", popen_args[0] if popen_args else [])
                parts = [command] if isinstance(command, (str, bytes)) else list(command)
                if any(str(part).endswith("launch-fence.py") for part in parts):
                    launch_environments.append(dict(popen_kwargs.get("env") or {}))
                return real_popen(*popen_args, **popen_kwargs)

            def capture_temporary_file(*file_args, **file_kwargs):
                if "dir" in file_kwargs:
                    prompt_temp_dirs.append(str(file_kwargs["dir"]))
                return real_temporary_file(*file_args, **file_kwargs)

            def capture_recorded_command(*command_args, **command_kwargs):
                rendered = real_recorded_command(*command_args, **command_kwargs)
                rendered_commands.append(rendered)
                return rendered

            with mock.patch.object(route_authority_module, "owns", return_value=True), \
                 patch_split(contract, "attempt_governed_process_quiescence",
                                   return_value=contract.ProcessQuiescence("quiescent", "fixture")), \
                 patch_split(contract, "supervisor_lease_is_held", return_value=False), \
                 mock.patch.object(route_parent_close, "row_requested", return_value=False), \
                 mock.patch.object(route_parent_close, "owner_continuation_processes",
                                   return_value=(contract.ProcessQuiescence(
                                       "quiescent", "registered-resources-preserved"), preserved)), \
                 mock.patch.object(join, "settle_finished_attempt", return_value={}), \
                 mock.patch.object(owner_input, "recover_consumer", side_effect=capture_recovery), \
                 mock.patch.object(headless.subprocess, "Popen", side_effect=capture_popen), \
                 mock.patch.object(headless.tempfile, "TemporaryFile", side_effect=capture_temporary_file), \
                 mock.patch.object(headless, "recorded_supervisor_command", side_effect=capture_recorded_command), \
                 contextlib.redirect_stdout(output), \
                 mock.patch.object(sys, "argv", [str(ROOT / "utilities/capability-route.py"),
                     "correct", "--attempt-id", attempt, "--jobs", str(jobs),
                     "--message-file", str(correction_path)]):
                public_status = cap.main()
            receipt = json.loads(output.getvalue())
            if additional_grant and receipt.get("consumer_recovery") != "respawned":
                pending = owner_input.inspect(jobs, attempt)["requests"]
                diagnostic = json.dumps({
                    "receipt": receipt,
                    "request_states": [item["state"] for item in pending],
                    "consumer_launches": len(launch_environments),
                    "rendered_commands": len(rendered_commands),
                }, sort_keys=True)
                self.assertEqual(len(pending), 1, diagnostic)
                self.assertEqual(pending[0]["state"], "queued", diagnostic)
                self.assertEqual(launch_environments, [], diagnostic)
                self.assertFalse(self.trace.exists(), diagnostic)
                self.assertIsNone(resource_process.poll(), diagnostic)
                self.assertEqual(hashlib.sha256(resource_registry.read_bytes()).hexdigest(),
                                 resource_registry_before, diagnostic)
                self.assertEqual(hashlib.sha256(resource_results.read_bytes()).hexdigest(),
                                 resource_results_before, diagnostic)
                self.assertEqual(home_snapshot(), worker_home_before, diagnostic)
                self.fail("public correction did not restore the recorded runtime: " + diagnostic)

            deadline = time.monotonic() + 15
            requests = []
            while time.monotonic() < deadline and not second_turn_ready.exists():
                requests = owner_input.inspect(jobs, attempt)["requests"]
                time.sleep(0.05)
            requests = owner_input.inspect(jobs, attempt)["requests"]
            if not requests or requests[0]["state"] != "turn-completed":
                self.fail("same-thread correction did not reach turn-completed: " +
                          self.trace.read_text() if self.trace.exists() else "no App Server trace")
            self.assertTrue(second_turn_ready.exists(), "the same owner did not remain live for resource registration")
            current_meta = join.exact_attempt_row(jobs, attempt).metadata
            resource_env = {
                **env, "AGENT_DISPATCH_COMPLETION_MODE": "supervised",
                "AGENT_DISPATCH_ATTEMPT_ID": attempt, "AGENT_DISPATCH_DEPTH": "1",
                "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION": "2",
                "AGENT_DISPATCH_WORKER_TYPE": "owner",
                "AGENT_DISPATCH_EXECUTION_SURFACE": "registered-headless",
                "AGENT_DISPATCH_REGISTERED_WORKER": "1",
                "AGENT_DISPATCH_JOBS": str(jobs),
                "AGENT_DISPATCH_COMPLETION_STATE_FILE": str(state_path),
                "AGENT_OWNER_ROUTE_FILE": str(route_path),
                "AGENT_OWNER_ROUTE_ID": route["route_id"],
                "AGENT_OWNER_ROUTE_HASH": route["route_hash"],
            }
            resource_binding = owner_resource.start_binding(
                route, route_path, SimpleNamespace(jobs=None, parent_attempt_id=None), resource_env)
            self.assertEqual(resource_binding["parent_attempt_id"], attempt)
            self.assertEqual(resource_binding["session_id"], "thread-1")
            self.assertEqual(resource_binding["owner_pid"], int(current_meta["pid"]))
            reservations = [row for row in budget_record.read_rows(jobs.parent, attempt)
                            if row.get("record_kind") == "reservation"]
            self.assertEqual([row["ordinal"] for row in reservations], [0, 1, 2])
            second_turn_release.touch()

        self.assertEqual(public_status, 0)
        self.assertEqual(receipt["consumer_recovery"], "respawned", receipt)
        self.assertEqual(len(respawn_outcomes), 1)
        self.assertEqual(respawn_outcomes[0]["runtime_dir"], str(runtime_dir))
        self.assertEqual(prompt_temp_dirs, [str(runtime_dir)])
        self.assertEqual(len(launch_environments), 1)
        self.assertEqual(launch_environments[0]["AGENT_RUNTIME_DIR"], str(runtime_dir))
        self.assertEqual(launch_environments[0]["AGENT_HOME"], str(ROOT.resolve()))
        self.assertEqual(respawn_outcomes[0]["respawn_release"], str(ROOT.resolve()))
        self.assertEqual(respawn_outcomes[0]["launch_home"],
                         str(recorded_release if earlier_launch_home else ROOT.resolve()))
        # H1 guard: the worker home and thread store the child resumes are the recorded ones.
        self.assertEqual(recorded_codex_worker_environment(recorded_release, worker_home, attempt),
                         recorded_environment_before)
        self.assertEqual(join.exact_attempt_row(jobs, attempt).metadata["launch_home"],
                         str(recorded_release if earlier_launch_home else ROOT.resolve()))
        if additional_grant:
            self.assertEqual(len(rendered_commands), 1)
            rendered = shlex.split(rendered_commands[0])
            writable_roots = [rendered[index + 1] for index, part in enumerate(rendered)
                              if part == "--writable-root"]
            self.assertEqual(writable_roots, verified_effective["writable_roots"])
            self.assertIn(str(runtime_dir), writable_roots)
            self.assertIn(str(compute_root), writable_roots)
            for key, value in (("request_sha256", "0" * 64),
                               ("source_path", str(access_file) + ".other")):
                with self.subTest(changed_access_binding=key):
                    changed = json.loads(json.dumps(sealed_launch))
                    changed["applied_permissions"]["execution_access"][key] = value
                    with self.assertRaises(headless.DispatchContractError) as raised:
                        real_recorded_command(
                            changed, row_line.rstrip("\n").split("\t"), metadata, jobs,
                            lease.with_suffix(".json"), lease, None, log_path,
                            verified_effective, recorded_home=str(home))
                    self.assertEqual(raised.exception.detail, "execution_access_request")
        self.assertFalse(receipt.get("duplicate"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["state"], "turn-completed")
        self.assertEqual([event["state"] for event in requests[0]["events"]],
                         ["queued", "sending", "accepted", "turn-completed"])
        self.assertIsNone(resource_process.poll())
        self.assertFalse((base / "payload-start-count").exists())
        final_rows = jobs.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(final_rows), 1)
        self.assertEqual(join.exact_attempt_row(jobs, attempt).metadata["attempt_id"], attempt)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        server_start = next(row for row in trace if row.get("event") == "server-start")
        self.assertEqual(server_start["runtime_dir"], str(runtime_dir))
        self.assertEqual(sum(row.get("event") == "thread-resume" for row in trace), 1)
        self.assertEqual(sum(row.get("event") == "thread-start" for row in trace), 0)
        turn = next(row for row in trace if row.get("event") == "turn-start")
        self.assertIn("resume the existing checkpoint", turn["prompt"])
        self.assertNotIn("ORIGINAL_TASK_SENTINEL", turn["prompt"])
        self.assertEqual(hashlib.sha256(resource_registry.read_bytes()).hexdigest(), resource_registry_before)
        self.assertEqual(hashlib.sha256(resource_results.read_bytes()).hexdigest(), resource_results_before)
        self.assertEqual(home_snapshot(), worker_home_before)

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
        self.assertEqual(events, [
            "thread-start", "turn-start", "join-start", "join-end", "turn-start",
        ])
        self.assertEqual(events.count("thread-start"), 1)
        self.assertEqual(events.count("thread-resume"), 0)
        turns = [item for item in trace if item["event"] == "turn-start"]
        self.assertEqual(turns[0]["delivered"], [])
        self.assertEqual(turns[0]["phase"], "running-turn")
        self.assertEqual(turns[1]["phase"], "running-turn")
        self.assertEqual(set(turns[1]["outbox"]["attempt_ids"]), {"att-child-a", "att-child-b"})
        self.assertEqual(turns[1]["outbox"]["consumed_attempt_ids"], [])
        self.assertTrue(all(item.get("lease_held") for item in trace if item["event"] == "turn-start"))
        self.assertEqual(
            set(turns[1]["delivered"]), {"att-child-a", "att-child-b"}
        )
        joins = [item for item in trace if item["event"].startswith("join-")]
        self.assertLess(joins[0]["time"], joins[1]["time"])
        self.assertLessEqual(joins[1]["time"], turns[1]["time"])
        self.assertEqual(joins[0]["phase"], "parked")
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
        self.assertIn('"delivery_classification":"attention"', turns[1]["prompt"])
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
        self.assertEqual([item["event"] for item in trace], ["thread-start", "turn-start"])
        self.assertEqual(sum(item["event"] == "thread-start" for item in trace), 1)
        self.assertEqual(sum(item["event"] == "thread-resume" for item in trace), 0)
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

    def test_real_git_failure_result_is_steered_and_acknowledged_once(self):
        repo = self.base / "git-failure-repo"
        repo.mkdir()

        def git(*args, check=True):
            return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=check)

        git("init", "-q")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        tracked = repo / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        git("add", "tracked.txt")
        git("commit", "-qm", "base")
        tracked.write_text("changed\n", encoding="utf-8")
        git("add", "tracked.txt")
        index = git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip()
        lock = Path(index + ".lock")
        lock.write_bytes(b"fixture lock")
        lock_fd = os.open(lock, os.O_RDONLY)
        self.addCleanup(os.close, lock_fd)
        lock.chmod(0)
        failure = git("commit", "-m", "expected lock failure", check=False)
        self.assertNotEqual(failure.returncode, 0)
        after_failure = {
            "head": git("rev-parse", "HEAD").stdout,
            "index": Path(index).read_bytes(),
            "work": tracked.read_bytes(),
            "lock": (os.fstat(lock_fd).st_dev, os.fstat(lock_fd).st_ino,
                     os.fstat(lock_fd).st_mode, os.fstat(lock_fd).st_size,
                     os.pread(lock_fd, 128, 0)),
        }
        item_file = self.base / "git-command-item.json"
        item_file.write_text(json.dumps({
            "type": "commandExecution", "id": "cmd-real-git-lock",
            "command": "zsh -lc 'git commit -m expected-lock-failure'",
            "cwd": str(repo), "status": "failed", "exitCode": failure.returncode,
            "aggregatedOutput": failure.stderr.decode("utf-8", errors="replace"),
        }), encoding="utf-8")
        self.assertEqual(failure.returncode, 128)

        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = self.run_supervisor(
            FAKE_NO_CHILD="1", FAKE_GIT_ITEM_FILE=str(item_file), FAKE_EXPECT_STEER="1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(sum(row.get("event") == "thread-start" for row in trace), 1, trace)
        self.assertEqual(sum(row.get("event") == "thread-resume" for row in trace), 0, trace)
        steer_rows = [row for row in trace if row.get("event") == "turn-steer"]
        self.assertEqual(len(steer_rows), 1, trace)
        request = steer_rows[0]["request"]
        self.assertEqual(request["method"], "turn/steer")
        self.assertEqual(request["params"]["threadId"], "thread-1")
        self.assertEqual(request["params"]["expectedTurnId"], "turn-1")
        self.assertEqual(len(request["params"]["input"]), 1)
        advisory = request["params"]["input"][0]["text"]
        self.assertIn(str(lock), advisory)
        self.assertIn("Ownership: unknown", advisory)
        self.assertIn("Codex App Server aggregatedOutput", advisory)
        self.assertIn("Stdout/stderr separation or raw bytes may be unavailable", advisory)

        events = [json.loads(line) for line in result.stdout.splitlines()]
        feedback = [row for row in events
                    if row.get("type") == "dispatch.supervisor.git_failure_feedback"]
        self.assertEqual([row["status"] for row in feedback].count("submitted"), 1, feedback)
        self.assertEqual([row["status"] for row in feedback].count("acknowledged"), 1, feedback)
        self.assertEqual([row["status"] for row in feedback].count("duplicate-ignored"), 1, feedback)
        self.assertFalse(any(row.get("reason") == "per-turn-command-result-limit"
                             for row in feedback), feedback)
        original_items = [row["item"] for row in events
                          if row.get("type") == "item.completed"
                          and row.get("item", {}).get("id") == "cmd-real-git-lock"]
        expected_item = {
            "type": "command_execution", "id": "cmd-real-git-lock",
            "command": "zsh -lc 'git commit -m expected-lock-failure'",
            "aggregated_output": failure.stderr.decode("utf-8", errors="replace"),
            "exit_code": 128, "status": "failed",
        }
        self.assertEqual(original_items, [expected_item, expected_item, expected_item])

        # A rejected advisory remains non-blocking: the same turn's original
        # final result is returned, and the rejection is an observation limit.
        self.trace.write_text("", encoding="utf-8")
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        rejected = self.run_supervisor(
            FAKE_NO_CHILD="1", FAKE_GIT_ITEM_FILE=str(item_file),
            FAKE_EXPECT_STEER="1", FAKE_STEER_REJECT="1")
        self.assertEqual(rejected.returncode, 0, rejected.stderr + rejected.stdout)
        rejected_events = [json.loads(line) for line in rejected.stdout.splitlines()]
        rejected_feedback = [row for row in rejected_events
                             if row.get("type") == "dispatch.supervisor.git_failure_feedback"]
        self.assertTrue(any(row.get("status") == "observation-limit"
                            and row.get("reason") == "feedback-rejected"
                            for row in rejected_feedback), rejected_feedback)
        rejected_trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(sum(row.get("event") == "thread-start" for row in rejected_trace), 1,
                         rejected_trace)
        self.assertEqual(sum(row.get("event") == "thread-resume" for row in rejected_trace), 0,
                         rejected_trace)
        self.assertEqual(sum(row.get("type") == "dispatch.supervisor.turn.started"
                             for row in rejected_events), 1)
        self.assertIn("verdict: PASS", rejected.stdout)
        lock_stat = os.fstat(lock_fd)
        current = {
            "head": git("rev-parse", "HEAD").stdout,
            "index": Path(index).read_bytes(),
            "work": tracked.read_bytes(),
            "lock": (lock_stat.st_dev, lock_stat.st_ino, lock_stat.st_mode,
                     lock_stat.st_size, os.pread(lock_fd, 128, 0)),
        }
        self.assertEqual(current, after_failure)

    def test_feedback_bounds_eligible_candidates_and_records_late_replies(self):
        name = "codex_app_server_supervisor_feedback_test_subject"
        spec = importlib.util.spec_from_file_location(name, SUPERVISOR)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)

        class FakeServer:
            next_id = 1

            def __init__(self):
                self.requests = []

            def send(self, value):
                self.requests.append(json.loads(json.dumps(value)))

        server = FakeServer()
        emitted = []
        feedback = module.CodexGitFailureFeedback(server)
        feedback.begin_turn("thread-1", "turn-1")
        error_path = self.base / ".git" / "index.lock"
        error_output = f"fatal: Unable to create '{error_path}': File exists.\n"
        command = "zsh -lc " + shlex.quote(f"git -C {self.base} commit -m fixture")
        wrapped_command = "python3 -c " + shlex.quote(
            "import subprocess; subprocess.run(['git', 'add', 'x'], check=True)")
        self.assertTrue(module.git_index_lock.codex_command_candidate(
            wrapped_command, str(self.base), 1, error_output))
        candidates = []
        diagnosis = {"state": "present", "message": "fixture diagnosis"}

        with mock.patch.object(module, "emit", side_effect=emitted.append), \
                mock.patch.object(module.git_index_lock, "diagnose_codex_command",
                                  return_value=diagnosis) as diagnose:
            for index in range(128):
                ordinary = {
                    "type": "commandExecution", "id": f"ordinary-{index}",
                    "command": "git status --short", "cwd": str(self.base),
                    "status": "completed" if index % 2 == 0 else "failed",
                    "exitCode": 0 if index % 2 == 0 else 1,
                    "aggregatedOutput": "routine success" if index % 2 == 0
                    else "fatal: unrelated command failure",
                }
                feedback.consider("thread-1", "turn-1", ordinary)
            for status, exit_code in (("unknown", 128), ("inProgress", 128),
                                      ("declined", 128), ("failed", 130)):
                unsupported = {
                    "type": "commandExecution", "id": f"unsupported-{status}-{exit_code}",
                    "command": command, "cwd": str(self.base), "status": status,
                    "exitCode": exit_code, "aggregatedOutput": error_output,
                }
                feedback.consider("thread-1", "turn-1", unsupported)

            for index in range(feedback.MAX_COMMANDS_PER_TURN + 1):
                item = {
                    "type": "commandExecution", "id": f"eligible-{index}",
                    "command": wrapped_command if index % 2 else command,
                    "cwd": str(self.base), "status": "failed",
                    "exitCode": 128, "aggregatedOutput": error_output,
                }
                candidates.append((item, json.dumps(item, sort_keys=True)))
                feedback.consider("thread-1", "turn-1", item)
            feedback.consider("thread-1", "turn-1", candidates[0][0])
            feedback.consider("thread-1", "turn-1", candidates[-1][0])

            self.assertEqual(diagnose.call_count, feedback.MAX_COMMANDS_PER_TURN)
            self.assertEqual(server.requests[0]["method"], "turn/steer")
            self.assertEqual(server.requests[0]["params"]["threadId"], "thread-1")
            self.assertEqual(server.requests[0]["params"]["expectedTurnId"], "turn-1")
            self.assertEqual(server.requests[0]["params"]["input"][0]["text"],
                             "fixture diagnosis")
            self.assertTrue(feedback.response({
                "jsonrpc": "2.0", "id": 1, "result": {"turnId": "turn-1"},
            }))
            self.assertTrue(feedback.response({
                "jsonrpc": "2.0", "id": 2,
                "error": {"code": -32000, "message": "turn rejected"},
            }))
            feedback.finish_turn("turn-1")
            self.assertTrue(feedback.response({
                "jsonrpc": "2.0", "id": 3, "result": {"turnId": "turn-1"},
            }))

        key = ("thread-1", "turn-1")
        self.assertEqual(len(feedback.seen[key]), feedback.MAX_COMMANDS_PER_TURN)
        self.assertLessEqual(len(feedback.pending), feedback.MAX_PENDING)
        self.assertLessEqual(len(feedback.turn_order), feedback.MAX_TURNS)
        self.assertEqual(len(feedback.limit_reported), 1)
        self.assertEqual(len(server.requests), feedback.MAX_PENDING)
        self.assertTrue(all(json.dumps(item, sort_keys=True) == before
                            for item, before in candidates))
        statuses = [row.get("status") for row in emitted
                    if row.get("type") == "dispatch.supervisor.git_failure_feedback"]
        self.assertEqual(statuses.count("duplicate-ignored"), 1)
        self.assertEqual(sum(row.get("reason") == "per-turn-command-result-limit"
                             for row in emitted), 1)
        self.assertIn("acknowledged", statuses)
        self.assertIn("feedback-rejected", [row.get("reason") for row in emitted])
        self.assertIn("late-feedback-ack", [row.get("reason") for row in emitted])

    def test_input_queued_before_the_first_turn_reaches_that_turn(self):
        import dispatch_owner_input as owner_input
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        owner_input.initialize_owner_input(self.jobs, PARENT, "codex-active-turn")
        owner_input.submit(self.jobs, PARENT, "early word", "early")
        result = self.run_supervisor(FAKE_NO_CHILD="1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual([item["event"] for item in trace], ["thread-start", "turn-start"])
        self.assertEqual(sum(item["event"] == "thread-start" for item in trace), 1)
        self.assertEqual(sum(item["event"] == "thread-resume" for item in trace), 0)
        turn = next(item for item in trace if item["event"] == "turn-start")
        self.assertIn("early word", turn["prompt"])
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
            "thread-start", "turn-start", "join-start", "join-end", "turn-start",
            "join-start", "join-end", "turn-start",
        ])
        self.assertEqual(sum(event["event"] == "thread-start" for event in trace), 1)
        self.assertEqual(sum(event["event"] == "thread-resume" for event in trace), 0)
        turns = [event for event in trace if event["event"] == "turn-start"]
        self.assertEqual(turns[1]["delivered"], ["att-child"])
        self.assertEqual(set(turns[-1]["delivered"]), {"att-child", "att-pending"})
        self.assertNotIn('"state": "registration-required"', result.stdout)

    def test_unavailable_state_for_pending_bound_input_never_starts_replacement_thread(self):
        import dispatch_owner_input as owner_input
        from dispatch_contract import hold_supervisor_lease

        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        owner_input.initialize_owner_input(self.jobs, PARENT, "codex-active-turn")
        with hold_supervisor_lease(self.jobs, PARENT, self.lease):
            owner_input.OwnerInput(self.jobs, PARENT, "thread-1", "codex-active-turn", lambda _e: None)
            owner_input.submit(self.jobs, PARENT, "keep this correction", "pending-correction")
        jobs_before = self.jobs.read_bytes()

        for saved_state in (None, "{unreadable"):
            with self.subTest(saved_state="missing" if saved_state is None else "unreadable"):
                if saved_state is None:
                    self.state.unlink(missing_ok=True)
                else:
                    self.state.write_text(saved_state, encoding="utf-8")
                state_before = self.state.read_bytes() if self.state.exists() else None
                self.trace.unlink(missing_ok=True)

                result = self.run_supervisor(FAKE_NO_CHILD="1")

                self.assertEqual(result.returncode, 75, result.stderr + result.stdout)
                self.assertIn("dispatch.supervisor.consumer-recovery-failed", result.stdout)
                self.assertEqual(self.jobs.read_bytes(), jobs_before)
                self.assertEqual(self.state.read_bytes() if self.state.exists() else None,
                                 state_before)
                self.assertFalse(self.trace.exists())
                receipt = owner_input.inspect(self.jobs, PARENT)
                self.assertEqual(receipt["requests"][0]["state"], "queued")

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

    def test_named_permission_profile_preserves_ordinary_turn_profile_authority(self):
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
            "--model-context-window", "600000",
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
        self.assertEqual(captured["command"][-2:], ["-c", "model_context_window=600000"])
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
        self.assertNotIn("approvalPolicy", turn)
        self.assertTrue(captured["closed"])
        self.assertIn('"type":"turn.completed"', output.getvalue())

    def test_owner_turn_negotiates_named_profile_or_keeps_legacy_overlay(self):
        profile = {"default_permissions": "hearting_owner_observe", "permissions": {
            "hearting_owner_observe": {"extends": ":read-only", "filesystem": {},
                                        "network": {"enabled": False}},
            "hearting_linked_commit": {"extends": ":workspace", "filesystem": {},
                                        "network": {"enabled": True}},
            "hearting_linked_commit_no_network": {
                "extends": ":workspace", "filesystem": {}, "network": {"enabled": False}},
        }}

        for supports_selector in (True, False):
            with self.subTest(supports_selector=supports_selector):
                captured = {}

                class FakeServer:
                    def __init__(self, command, cwd, env):
                        captured["command"] = command
                        captured["cwd"] = cwd
                        self.events = [
                            {"method": "item/completed", "params": {"turnId": "turn-owner",
                             "item": {"type": "agentMessage", "id": "final",
                                      "text": "artifact: -\\nverdict: PASS\\nblocker: none"}}},
                            {"method": "turn/completed", "params": {
                             "turn": {"id": "turn-owner", "status": "completed"}}},
                        ]

                    def request(self, method, params):
                        captured.setdefault("requests", []).append((method, params))
                        if method == "thread/start":
                            return {"thread": {"id": "thread-owner"}}
                        if method == "turn/start":
                            return {"turn": {"id": "turn-owner"}}
                        return {}

                    def notification(self, method):
                        captured.setdefault("notifications", []).append(method)

                    def next_event(self):
                        return self.events.pop(0)

                    def close(self):
                        captured["closed"] = True

                args = self.supervisor.parser().parse_args([
                    "--one-turn", "--worktree", str(self.root), "--sandbox", "workspace-write",
                    "--network-access", "--app-server-command", "codex app-server --listen stdio://",
                ])
                output = io.StringIO()

                def bind_owner_state(target):
                    target.owner_runtime_permission_state = {
                        "observation_only": False, "may_commit": True,
                    }
                    target.owner_runtime_base_sandbox = "workspace-write"

                with mock.patch.object(self.supervisor, "apply_owner_runtime_permissions",
                                       side_effect=bind_owner_state), \
                     mock.patch.object(self.supervisor, "turn_permissions_available",
                                       return_value=supports_selector), \
                     mock.patch.object(self.supervisor.commit_policy, "worktree_git_dirs",
                                       return_value=(None, None)), \
                     mock.patch.object(self.supervisor, "owner_turn_profile_config",
                                       return_value=profile) as build_owner, \
                     mock.patch.object(self.supervisor, "commit_profile_config",
                                       return_value=None) as build_legacy, \
                     mock.patch.object(self.supervisor, "config_arguments",
                                       return_value=["--owner-profiles"]), \
                     mock.patch.object(self.supervisor, "AppServer", FakeServer), \
                     mock.patch.object(self.supervisor.sys, "stdin", io.StringIO("owner prompt")), \
                     mock.patch.object(self.supervisor.sys, "stdout", output):
                    result = self.supervisor.run_one_turn(args)

                self.assertEqual(result, 0)
                requests = dict(captured["requests"])
                init = requests["initialize"]
                turn = requests["turn/start"]
                if supports_selector:
                    self.assertEqual(init["capabilities"], {"experimentalApi": True})
                    self.assertEqual(turn["permissions"], "hearting_linked_commit")
                    self.assertNotIn("sandboxPolicy", turn)
                else:
                    self.assertIsNone(init["capabilities"])
                    self.assertEqual(turn["sandboxPolicy"]["type"], "workspaceWrite")
                    self.assertNotIn("permissions", turn)
                self.assertIn("--owner-profiles", captured["command"])
                build_owner.assert_called_once()
                build_legacy.assert_not_called()


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

    def test_silent_owner_turn_survives_when_exact_payload_activity_is_observed(self):
        from foreground_activity import ActivityObservation
        from session_supervisor_decisions import OwnerTurnActivity
        module = load_supervisor_module()
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "quiet_app.py"
            script.write_text(textwrap.dedent("""\
                import json, sys, time
                def send(value): print(json.dumps(value), flush=True)
                for line in sys.stdin:
                    value = json.loads(line)
                    if value.get('method') == 'turn/start':
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'turn':{'id':'turn-1'}}})
                        time.sleep(0.8)
                        send({'jsonrpc':'2.0','method':'item/completed','params':{
                            'threadId':'thread-1','turnId':'turn-1','item':{
                                'type':'agentMessage','id':'m1','text':'done'}}})
                        send({'jsonrpc':'2.0','method':'turn/completed','params':{
                            'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}})
            """))
            server = module.AppServer([sys.executable, str(script)], temp, dict(os.environ))
            self.addCleanup(server.close)
            args = SimpleNamespace(worktree=temp, writable_root=[], sandbox="danger-full-access",
                                   network_access=False, approval="inherit", model=None,
                                   reasoning=None, owner_input=None)
            activity = OwnerTurnActivity(
                lambda: ActivityObservation(True, False, ("exact-tool-cpu",)),
                window_seconds=0.3, sample_seconds=0.05,
            )
            started = time.monotonic()
            with mock.patch.object(module, "emit"):
                text, _item = module.run_turn(
                    server, thread_id="thread-1", prompt="go", args=args, activity=activity)
            self.assertGreater(time.monotonic() - started, 0.3)
            self.assertEqual(text, "done")

    def test_current_turn_native_content_and_output_deltas_renew_activity(self):
        from foreground_activity import ActivityObservation
        from session_supervisor_decisions import OwnerTurnActivity
        module = load_supervisor_module()
        accepted = (
            "item/agentMessage/delta",
            "item/plan/delta",
            "item/commandExecution/outputDelta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
        )
        for method in accepted:
            with self.subTest(method=method):
                params = {"threadId": "thread-1", "turnId": "turn-1",
                          "itemId": "item-1", "delta": "working"}
                if method == "item/reasoning/summaryTextDelta":
                    params["summaryIndex"] = 0
                elif method == "item/reasoning/textDelta":
                    params["contentIndex"] = 0
                self.assertTrue(module.codex_payload_progress({
                    "method": method,
                    "params": params,
                }, "thread-1", "turn-1"))

        accepted_work_events = (
            {"method": "item/started", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "item": {"type": "commandExecution", "id": "cmd-1"}}},
            {"method": "item/completed", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "item": {"type": "agentMessage", "id": "message-1"}}},
            {"method": "turn/plan/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "explanation": None,
             "plan": [{"step": "inspect", "status": "inProgress"}]}},
            {"method": "turn/diff/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "diff": "diff --git a/file b/file"}},
            {"method": "item/fileChange/patchUpdated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "itemId": "file-1", "changes": [{
                 "path": "file", "kind": {"type": "update", "move_path": None},
                 "diff": "@@ -1 +1 @@"}]}},
            {"method": "item/mcpToolCall/progress", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "itemId": "mcp-1", "message": "working"}},
            {"method": "item/commandExecution/terminalInteraction", "params": {
             "threadId": "thread-1", "turnId": "turn-1", "itemId": "cmd-1",
             "processId": "process-1", "stdin": "continue\n"}},
        )
        for event in accepted_work_events:
            with self.subTest(accepted_method=event["method"]):
                self.assertTrue(module.codex_payload_progress(event, "thread-1", "turn-1"))

        for event in (
            {"method": "item/started", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "item": {}}},
            {"method": "item/completed", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "item": {"type": "agentMessage", "id": "  "}}},
            {"method": "item/started", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "item": {"type": "unknownItem", "id": "item-1"}}},
            {"method": "item/started", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "item": {"type": "commandExecution", "id": []}}},
            {"method": "item/started", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "item": []}},
            {"method": "item/started", "params": {"threadId": "thread-1",
             "turnId": "turn-1"}},
            {"method": "turn/plan/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "explanation": None}},
            {"method": "turn/plan/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "explanation": None, "plan": []}},
            {"method": "turn/plan/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "explanation": None, "plan": "inspect"}},
            {"method": "turn/plan/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "explanation": None,
             "plan": [{"step": "", "status": "inProgress"}]}},
            {"method": "turn/plan/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "explanation": None,
             "plan": [{"step": "inspect", "status": {}}]}},
            {"method": "turn/plan/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "plan": [{"step": "inspect", "status": "pending"}]}},
            {"method": "turn/diff/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1"}},
            {"method": "turn/diff/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "diff": ""}},
            {"method": "turn/diff/updated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "diff": []}},
            {"method": "item/fileChange/patchUpdated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "itemId": "file-1", "changes": []}},
            {"method": "item/fileChange/patchUpdated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "itemId": "file-1", "changes": [{
                 "path": "file", "kind": {"type": "unknown"}, "diff": "diff"}]}},
            {"method": "item/fileChange/patchUpdated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "itemId": "file-1", "changes": [{
                 "path": "file", "kind": {"type": "update"}, "diff": "diff"}]}},
            {"method": "item/fileChange/patchUpdated", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "changes": [{
                 "path": "file", "kind": {"type": "add"}, "diff": "diff"}]}},
            {"method": "item/mcpToolCall/progress", "params": {"threadId": "thread-1",
             "turnId": "turn-1", "itemId": "mcp-1", "message": "  "}},
            {"method": "item/commandExecution/terminalInteraction", "params": {
             "threadId": "thread-1", "turnId": "turn-1", "itemId": "cmd-1",
             "processId": "process-1", "stdin": ""}},
            {"method": "item/agentMessage/delta", "params": {
                "threadId": "thread-other", "turnId": "turn-1",
                "itemId": "item-1", "delta": "foreign thread"}},
            {"method": "item/commandExecution/outputDelta", "params": {
                "threadId": "thread-1", "turnId": "turn-other",
                "itemId": "item-1", "delta": "foreign turn"}},
            {"method": "item/plan/delta", "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "itemId": "item-1", "delta": ""}},
            {"method": "thread/tokenUsage/updated", "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "tokenUsage": {"totalTokens": 1}}},
            {"method": "turn/started", "params": {
                "threadId": "thread-1", "turnId": "turn-1"}},
            {"method": "thread/name/updated", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "name": "status"}},
            {"method": "item/agentMessage/delta", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "delta": "no item id"}},
            {"method": "item/commandExecution/outputDelta", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1",
                "delta": []}},
            {"method": "item/reasoning/summaryTextDelta", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1",
                "delta": "working"}},
            {"method": "item/reasoning/summaryTextDelta", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1",
                "delta": "working", "summaryIndex": -1}},
            {"method": "item/reasoning/textDelta", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1",
                "delta": "working", "contentIndex": True}},
            {"method": "item/reasoning/summaryPartAdded", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1",
                "summaryIndex": 1}},
            {"method": "item/fileChange/outputDelta", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "file-1",
                "delta": "legacy"}},
            {"method": "item/autoApprovalReview/started", "params": {
                "threadId": "thread-1", "turnId": "turn-1"}},
        ):
            self.assertFalse(module.codex_payload_progress(event, "thread-1", "turn-1"),
                             event["method"])

        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "streaming_delta_app.py"
            script.write_text(textwrap.dedent("""\
                import json, sys, time
                def send(value): print(json.dumps(value), flush=True)
                for line in sys.stdin:
                    request = json.loads(line)
                    if request.get('method') == 'turn/start':
                        send({'jsonrpc':'2.0','id':request['id'],'result':{'turn':{'id':'turn-1'}}})
                        for index in range(12):
                            send({'jsonrpc':'2.0','method':'item/reasoning/summaryTextDelta','params':{
                                'threadId':'thread-1','turnId':'turn-1',
                                'itemId':'reasoning-1','delta':'part','summaryIndex':0}})
                            time.sleep(0.05)
                        send({'jsonrpc':'2.0','method':'item/completed','params':{
                            'threadId':'thread-1','turnId':'turn-1','item':{
                                'type':'agentMessage','id':'message-1','text':'done'}}})
                        send({'jsonrpc':'2.0','method':'turn/completed','params':{
                            'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}})
            """))
            server = module.AppServer([sys.executable, str(script)], temp, dict(os.environ))
            self.addCleanup(server.close)
            args = SimpleNamespace(worktree=temp, writable_root=[], sandbox="danger-full-access",
                                   network_access=False, approval="inherit", model=None,
                                   reasoning=None, owner_input=None)
            activity = OwnerTurnActivity(
                lambda: ActivityObservation(False, False, ("quiet",)),
                window_seconds=0.3, sample_seconds=0.05,
            )
            started = time.monotonic()
            with mock.patch.object(module, "emit"):
                text, _item = module.run_turn(
                    server, thread_id="thread-1", prompt="go", args=args, activity=activity)
            self.assertGreater(time.monotonic() - started, 0.3)
            self.assertEqual(text, "done")

    def test_transport_exception_clears_the_current_turn_activity(self):
        from foreground_activity import ActivityObservation
        from session_supervisor_decisions import OwnerTurnActivity
        module = load_supervisor_module()

        class BrokenServer:
            owner_turn_activity = None
            git_failure_feedback = None

            def request(self, _method, _params):
                return {"turn": {"id": "turn-1"}}

            def next_event(self):
                raise module.SupervisorError("app-server-eof")

        server = BrokenServer()
        activity = OwnerTurnActivity(
            lambda: ActivityObservation(False, False, ("quiet",)),
            window_seconds=1, sample_seconds=0.05,
        )
        args = SimpleNamespace(worktree="/tmp", writable_root=[], sandbox="danger-full-access",
                               network_access=False, approval="inherit", model=None,
                               reasoning=None, owner_input=None)
        with mock.patch.object(module, "emit"), \
             self.assertRaisesRegex(module.SupervisorError, "app-server-eof"):
            module.run_turn(server, thread_id="thread-1", prompt="go", args=args,
                            activity=activity)
        self.assertIsNone(server.owner_turn_activity)

    def test_malformed_same_turn_events_and_accounting_do_not_hide_owner_inactivity(self):
        from foreground_activity import ActivityObservation
        from session_supervisor_decisions import OwnerTurnActivity
        module = load_supervisor_module()
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "bookkeeping_app.py"
            script.write_text(textwrap.dedent("""\
                import json, sys, time
                def send(value): print(json.dumps(value), flush=True)
                notifications = [
                    {'method':'thread/tokenUsage/updated','params':{
                        'threadId':'thread-1','turnId':'turn-1',
                        'tokenUsage':{'last':{'totalTokens':1}}}},
                    {'method':'item/started','params':{
                        'threadId':'thread-1','turnId':'turn-1','item':{}}},
                    {'method':'item/completed','params':{
                        'threadId':'thread-1','turnId':'turn-1',
                        'item':{'type':'unknownItem','id':'item-1'}}},
                    {'method':'turn/plan/updated','params':{
                        'threadId':'thread-1','turnId':'turn-1'}},
                    {'method':'turn/diff/updated','params':{
                        'threadId':'thread-1','turnId':'turn-1','diff':''}},
                ]
                for line in sys.stdin:
                    value = json.loads(line)
                    if value.get('method') == 'turn/start':
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'turn':{'id':'turn-1'}}})
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            for notification in notifications:
                                send({'jsonrpc':'2.0',**notification})
                                time.sleep(0.01)
            """))
            server = module.AppServer([sys.executable, str(script)], temp, dict(os.environ))
            self.addCleanup(server.close)
            args = SimpleNamespace(worktree=temp, writable_root=[], sandbox="danger-full-access",
                                   network_access=False, approval="inherit", model=None,
                                   reasoning=None, owner_input=None)
            activity = OwnerTurnActivity(
                lambda: ActivityObservation(False, False, ("quiet",)),
                window_seconds=0.3, sample_seconds=0.05,
            )
            with mock.patch.object(module, "emit"):
                with self.assertRaisesRegex(module.SupervisorError, "codex-turn-no-progress"):
                    module.run_turn(server, thread_id="thread-1", prompt="go",
                                    args=args, activity=activity)
            self.assertIsNone(server.owner_turn_activity)


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
            # Phase F: a progress-carrying ordinary admit no longer exhausts
            # anything, so the boundary that still refuses is the no-progress
            # stall counter (one stall token here).
            ledger = B.ContinuationLedger(SimpleNamespace(ordinary=1, stall=1, reserved=0))
            kwargs = dict(parent_attempt_id=PARENT, route_id="rt-budget", route_hash="hash",
                          purpose="ordinary", stalled=True)
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

    def test_admit_returns_notice_when_the_configured_budget_starts_at_the_threshold(self):
        # Phase F: a progress admit no longer lowers `gross_remaining`, so an
        # ordinary budget above the threshold never crosses it. The warning
        # still fires exactly once when the effective budget itself starts at
        # or below the threshold (an explicit small `--max-continuations`).
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_continuation_budget as BUDGET
        budget = BUDGET.ContinuationBudget(limit=3, source="test")
        ledger = BUDGET.ContinuationLedger(budget)
        notices = []
        for ordinal in range(3):
            verdict, notice = self.module._admit_continuation(
                ledger, self.state_root, parent_attempt_id="att-p",
                route_id="rt-x", route_hash="sha256:" + "a" * 64,
                ordinal=ordinal, purpose="ordinary", stalled=False,
                warning_threshold=3,
            )
            self.assertTrue(verdict.admitted)
            notices.append(notice)
        self.assertEqual(["", ""], notices[1:])
        self.assertTrue(notices[0])
        self.assertIn("remaining=3", notices[0])


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
            # Phase F: progress admits cannot be refused, so the refusal that
            # appends the colliding `purpose="ordinary"` reservation row is
            # the no-progress stall branch (class "stall", same purpose).
            budget = SimpleNamespace(ordinary=3, stall=0, reserved=1)
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
            self.assertEqual(3, ledger.gross_remaining)
            self.assertEqual(1, ledger.reserved_remaining)

            # Refused at the exhausted stall boundary. This still appends a
            # `purpose="ordinary"` reservation row at ordinal=2 even though
            # the ledger refuses the admit -- that append is the collision
            # source the fix must tolerate.
            verdict, _ = module._admit_continuation(
                ledger, state_root, ordinal=2, purpose="ordinary", stalled=True, **common,
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
