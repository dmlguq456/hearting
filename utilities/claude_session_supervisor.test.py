#!/usr/bin/env python3

from __future__ import annotations

import json
import io
import hashlib
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "utilities" / "claude-session-supervisor.py"
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
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_completion_join as join  # noqa: E402
_SPEC = importlib.util.spec_from_file_location("claude_session_supervisor", SUPERVISOR)
supervisor = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = supervisor
assert _SPEC.loader is not None
_SPEC.loader.exec_module(supervisor)


def owner_row(lease: Path, status: str = "open") -> str:
    return (
        f"2026-07-23T00:00:00Z\t{status}\t/repo\t/wt\towner\t"
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "fallback_hop=same-harness-headless,worker_type=owner,harness=claude,"
        "completion_delivery=session-resume-supervised,supervisor_lease=flock-v1,"
        f"supervisor_lease_file={lease},supervisor_lease_nonce={'c' * 64},"
        f"attempt_id={PARENT}\n"
    )


def child_row(status: str = "open", harness: str = "claude") -> str:
    return (
        f"2026-07-23T00:00:00Z\t{status}\t/repo\t/wt\tchild\t"
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,launch_started=1,"
        f"harness={harness},"
        f"attempt_id=att-child,parent_attempt_id={PARENT},note=RAW_CLAUDE_SENTINEL\n"
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


class ClaudeSessionSupervisorTest(unittest.TestCase):
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
        self.claude = self.base / "fake_claude.py"
        self.stream_claude = self.base / "fake_stream_claude.py"
        self.join = self.base / "fake_join.py"
        self.claude.write_text(
            textwrap.dedent(
                """\
                import json, os, sys, time
                args = sys.argv[1:]
                resume = '--resume' in args
                key = '--resume' if resume else '--session-id'
                session = args[args.index(key) + 1]
                prompt = sys.stdin.read()
                state_path = os.environ['AGENT_DISPATCH_COMPLETION_STATE_FILE']
                with open(state_path, encoding='utf-8') as state_handle:
                    state = json.load(state_handle)
                    assert state['phase'] == 'running-turn', state
                    delivered = state['delivered_attempt_ids']
                    if delivered:
                        assert set(state['outbox']['attempt_ids']).issubset(delivered), state
                if os.environ.get('FAKE_MIXED_START') == '1' and 'att-child' in delivered:
                    jobs = os.environ['FAKE_JOBS']
                    with open(jobs, encoding='utf-8') as h:
                        rows = h.read().replace('launch_started=0', 'launch_started=1')
                    with open(jobs, 'w', encoding='utf-8') as h:
                        h.write(rows)
                with open(os.environ['FAKE_TRACE'], 'a', encoding='utf-8') as h:
                    h.write(json.dumps({'event':'turn-start','time':time.monotonic(),
                                        'resume':resume,'session':session,'prompt':prompt,
                                        'args':args,'delivered':delivered}) + '\\n')
                dry_first = os.environ.get('FAKE_DRY_RUN_FIRST') == '1'
                if dry_first and resume and not delivered:
                    with open(os.environ['FAKE_JOBS'], 'a', encoding='utf-8') as h:
                        h.write('2026-08-11T00:00:00Z\\topen\\t/repo\\t/wt\\tchild\\t'
                                'attempt_schema_version=2,dispatch_depth=2,transport=headless,'
                                'execution_surface=registered-headless,registered_worker=1,'
                                'launch_started=1,attempt_id=att-child-retry,'
                                'parent_attempt_id=att-parent\\n')
                launch_race = os.environ.get('FAKE_LAUNCH_STARTED_RACE') == '1'
                if launch_race and not resume:
                    # Registered now, fenced start published a moment after this turn ends.
                    import subprocess
                    jobs = os.environ['FAKE_JOBS']
                    row = ('2026-08-11T00:00:0{n}Z\\topen\\t/repo\\t/wt\\tchild-race-{s}\\t'
                           'attempt_schema_version=2,dispatch_depth=2,transport=headless,'
                           'execution_surface=registered-headless,registered_worker=1,'
                           'launch_started={n},attempt_id=att-child-race-{s},parent_attempt_id=att-parent\\n')
                    with open(jobs, 'a', encoding='utf-8') as h:
                        h.write(row.format(n=0, s='a') + row.format(n=0, s='b'))
                    subprocess.Popen([sys.executable, '-c',
                                      'import sys, time; time.sleep(0.3); '
                                      'open(sys.argv[1], "a", encoding="utf-8").write(sys.argv[2])',
                                      jobs, row.format(n=1, s='a') + row.format(n=1, s='b')],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
                final_first = os.environ.get('FAKE_NO_CHILD') == '1'
                text = ('Started both children.' if launch_race and not resume
                        else 'runtime_wait: registered-children' if dry_first and not delivered
                        else 'artifact: -\\nverdict: PASS\\nblocker: none'
                        if resume or final_first else 'runtime_wait: registered-children')
                if os.environ.get('FAKE_BREAK_STATE_AUDIT') == '1':
                    audit = state_path + '.transitions.jsonl'
                    try:
                        os.unlink(audit)
                    except FileNotFoundError:
                        pass
                    os.mkdir(audit)
                print(json.dumps({'type':'system','subtype':'init',
                                  'private':'RAW_PARENT_CONTEXT_SENTINEL'}))
                print(json.dumps({'type':'result','subtype':'success','is_error':False,
                                  'result':text}))
                """
            ),
            encoding="utf-8",
        )
        self.stream_claude.write_text(
            textwrap.dedent(
                """\
                import json, os, sys, time
                args = sys.argv[1:]
                session = args[args.index('--session-id') + 1]
                state_path = os.environ['AGENT_DISPATCH_COMPLETION_STATE_FILE']
                with open(os.environ['FAKE_TRACE'], 'a', encoding='utf-8') as h:
                    h.write(json.dumps({'event':'process-start','pid':os.getpid(),
                                        'session':session,'args':args}) + '\\n')
                for line in sys.stdin:
                    payload = json.loads(line)
                    prompt = payload['message']['content'][0]['text']
                    with open(state_path, encoding='utf-8') as state_handle:
                        state = json.load(state_handle)
                        assert state['phase'] == 'running-turn', state
                        delivered = state['delivered_attempt_ids']
                        if delivered:
                            assert set(state['outbox']['attempt_ids']).issubset(delivered), state
                    with open(os.environ['FAKE_TRACE'], 'a', encoding='utf-8') as h:
                        h.write(json.dumps({'event':'turn-start','pid':os.getpid(),
                                            'time':time.monotonic(),'session':session,
                                            'prompt':prompt,'delivered':delivered,
                                            'uuid':payload.get('uuid')}) + '\\n')
                    text = ('artifact: -\\nverdict: PASS\\nblocker: none'
                            if delivered else 'runtime_wait: registered-children')
                    result = {'type':'result','subtype':'success','is_error':False,'result':text}
                    if os.environ.get('FAKE_ECHO_UUID') == '1':
                        result['user_message_uuids'] = [payload.get('uuid')]
                    print(json.dumps(result), flush=True)
                    if os.environ.get('FAKE_STALE_NOTIFICATION') == '1' and not delivered:
                        # A Bash call Claude Code backgrounded finishes while the
                        # owner is parked: Claude starts a turn of its own and its
                        # reply sits in the pipe ahead of the next prompt's.
                        stale = {'type':'result','subtype':'success','is_error':False,
                                 'origin':{'kind':'task-notification'},
                                 'result':'runtime_wait: registered-children'}
                        print(json.dumps(stale), flush=True)
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
                with open(trace, 'a', encoding='utf-8') as h:
                    h.write(json.dumps({'event':'join-start','time':time.monotonic()}) + '\\n')
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
                        route_file = os.environ.get('FAKE_TERMINAL_ROUTE')
                        terminal = ''
                        if route_file:
                            with open(route_file, encoding='utf-8') as route_handle:
                                route = json.load(route_handle)
                            marker = os.environ['FAKE_TERMINAL_MARKER']
                            with open(marker, 'w', encoding='utf-8') as marker_handle:
                                json.dump({'schema_version': 2,
                                           'route_id': route['route_id'],
                                           'route_hash': route['route_hash'],
                                           'node_id': 'report',
                                           'attempt_id': attempt}, marker_handle)
                            terminal = (f",note=completed-marker,"
                                        f"route_id={route['route_id']},"
                                        f"route_hash={route['route_hash']},"
                                        f"route_node=report,completion_marker={marker}")
                        else:
                            terminal = ',failure_class=pass,note=completed-supervisor'
                        fields[1] = 'done'
                        fields[5] += terminal + ',launch_outcome=never-launched'
                        kept.append('\\t'.join(fields))
                with open(jobs, 'w', encoding='utf-8') as h:
                    h.write('\\n'.join(kept) + '\\n')
                with open(trace, 'a', encoding='utf-8') as h:
                    h.write(json.dumps({'event':'join-end','time':time.monotonic()}) + '\\n')
                print(json.dumps({'schema_version':2,'state':'ready','parent_attempt_id':parent,
                    'children':[{'attempt_id':attempt,'status':'done','readiness':'ready',
                                 'reason':'registry-closed','required_action':'advance-completed'} for attempt in attempts]}))
                """
            ),
            encoding="utf-8",
        )

    def command(self, claude: Path | None = None) -> list[str]:
        return [
            sys.executable,
            str(SUPERVISOR),
            "--worktree", str(self.base),
            "--jobs", str(self.jobs),
            "--parent-attempt-id", PARENT,
            "--state-file", str(self.state),
            "--lease-file", str(self.lease),
            "--add-dir", str(self.base),
            "--claude-command", f"{sys.executable} {claude or self.claude}",
            "--join-command", f"{sys.executable} {self.join}",
            "--join-timeout", "2",
            "--join-interval", "0.02",
            "--disallowed-tool", "Monitor",
        ]

    def child_env(self, **extra: str) -> dict[str, str]:
        """Fixture-pinned subprocess environment.

        This suite also runs inside dispatched workers, whose real
        ``AGENT_ARTIFACT_ROOT`` otherwise contradicts every fixture artifact
        root: reconcile then skips with ``terminal-error:artifact-root-mismatch``
        and the run only ends at ``continuation-limit-exceeded``. Pin the
        fixture root for every child process instead of leaking the caller's.
        """
        return {
            **os.environ,
            "AGENT_ARTIFACT_ROOT": str(self.artifact_root),
            **extra,
        }

    def run_supervisor(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace), **extra_env),
            timeout=10,
        )


    def test_mixed_model_resource_outboxes_restart_after_model_interruption(self):
        self._assert_mixed_resource_outbox_delivery("model")

    def test_mixed_model_resource_outboxes_restart_after_resource_interruption(self):
        self._assert_mixed_resource_outbox_delivery("resource")

    def _assert_mixed_resource_outbox_delivery(self, interrupt_on):
        import dispatch_resource_wait as RESOURCE
        from types import SimpleNamespace
        module = supervisor
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
            prompt,session = a[2],a[1]
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
            return {"type":"result","subtype":"success","is_error":False,"result":text},0
        with mock.patch.object(RESOURCE,"context",return_value=ctx), \
             mock.patch.object(RESOURCE,"acknowledge",side_effect=acknowledge), \
             mock.patch.object(RESOURCE.RESUME,"supervisor_alive",return_value=True), \
             mock.patch.object(module,"run_turn",side_effect=turn), \
             mock.patch.object(module,"reconcile",return_value=True), \
             mock.patch.object(module,"emit"), mock.patch.object(module,"resolved_turn_transport",return_value="resume-process"):
            join.write_supervisor_state(self.state,PARENT,set(),phase="running-turn")
            RESOURCE.wait(args,self.state,SimpleNamespace(thread_id=native,pending=lambda:False),set(),lambda _:None)
            original = join.read_supervisor_phase_state(self.state,PARENT).resource["outbox"]
            resource_id = original["receipt_id"]
            receipt = {"schema_version":2,"state":"ready","parent_attempt_id":PARENT,
                "children":[{"attempt_id":"att-child","status":"done","readiness":"ready",
                             "reason":"registry-closed","required_action":"advance-completed"}],
                "delivery_timing":{"delivery_timing_schema_version":1,**{point:None for point in DELIVERY_TIMING_POINTS}}}
            join.prepare_supervisor_outbox(self.state,PARENT,set(),receipt,join.current_children(self.jobs,PARENT,{"att-child"}))
            self.assertEqual(join.read_supervisor_phase_state(self.state,PARENT).resource["session_id"],native)
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
        module = supervisor
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
            prompt,session = a[2],a[1]
            turns.append((session,prompt))
            text = "runtime_wait: registered-children" if len(turns)==1 else "artifact: -\nverdict: PASS\nblocker: none"
            return {"type":"result","subtype":"success","is_error":False,"result":text},0
        with mock.patch.object(RESOURCE,"context",return_value=ctx), \
             mock.patch.object(RESOURCE.RESUME,"supervisor_alive",return_value=True), \
             mock.patch.object(module,"run_turn",side_effect=turn), \
             mock.patch.object(module,"reconcile",return_value=True), \
             mock.patch.object(sys,"stdin",io.StringIO("initial assignment")), \
             mock.patch.object(module,"emit"), mock.patch.object(module,"resolved_turn_transport",return_value="resume-process"):
            self.assertEqual(module.main(self.command()[2:]+route_args),0)
        self.assertEqual(events,["model-turn","runtime-poll","model-turn"])
        self.assertEqual(len(turns),2)
        self.assertEqual(turns[0][0],turns[1][0])
        self.assertIn("Runtime resource receipt",turns[1][1])
        self.assertIn('"verification_pass":false',turns[1][1])
        self.assertIn("not a model child",turns[1][1])
        self.assertNotIn("registration-required",turns[1][1])
    def test_resource_phase_restart_resumes_exact_native_session(self):
        self.jobs.write_text(owner_row(self.lease))
        join = supervisor.RESOURCE_WAIT.JOIN
        join.write_supervisor_state(self.state,PARENT,set(),phase="recovery")
        with join._supervisor_state_lock(self.state):
            join._write_supervisor_state_unlocked(self.state,PARENT,set(),phase="recovery",
                resource={"session_id":"fixture-original-native","delivered":["a"*64],"outbox":None})
        result = self.run_supervisor()
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(len(trace),1)
        self.assertEqual(trace[0]["session"],"fixture-original-native")
        self.assertTrue(trace[0]["resume"])
        self.assertIn("--resume",trace[0]["args"])
        self.assertNotIn("--session-id",trace[0]["args"])
        self.assertNotIn("registration-required",result.stdout)

    def test_resume_uses_same_session_once_after_join(self):
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = self.run_supervisor()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(
            [item["event"] for item in trace],
            ["turn-start", "join-start", "join-end", "turn-start"],
        )
        first, second = trace[0], trace[3]
        self.assertFalse(first["resume"])
        self.assertTrue(second["resume"])
        self.assertEqual(first["session"], second["session"])
        self.assertEqual(first["delivered"], [])
        self.assertEqual(second["delivered"], ["att-child"])
        self.assertNotIn("--no-session-persistence", first["args"])
        self.assertIn("--session-id", first["args"])
        self.assertIn("--resume", second["args"])
        for turn in (first, second):
            self.assertIn("--settings", turn["args"])
            settings = json.loads(
                turn["args"][turn["args"].index("--settings") + 1]
            )
            pre_tool = settings["hooks"]["PreToolUse"][0]
            self.assertEqual(pre_tool["matcher"], "*")
            hook = pre_tool["hooks"][0]
            self.assertEqual(hook["type"], "command")
            self.assertIn("hooks/registered-parent-park.py", hook["command"])
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(sum(row.get("type") == "result" for row in rows), 1)
        self.assertEqual(rows[-1]["subtype"], "success")
        self.assertNotIn("RAW_CLAUDE_SENTINEL", result.stdout)
        self.assertNotIn("RAW_PARENT_CONTEXT_SENTINEL", result.stdout)
        self.assertFalse(self.state.exists())
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/repo\t/wt\towner\t", registry)
        self.assertIn("note=completed-supervisor", registry)
        log = self.base / "attempt.claude.jsonl"
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
            env=self.child_env(),
        )
        self.assertEqual(inspected.returncode, 0, inspected.stderr + inspected.stdout)
        self.assertIn("\tvalid\texact-claude-result\tPASS\tnone\tnone", inspected.stdout)

    def test_budget_warning_reaches_the_prompt_handed_to_owner(self):
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = subprocess.run(
            self.command() + ["--continuation-warning-threshold", "999"],
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
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

    def test_stream_transport_reuses_one_process_and_emits_boundary_timings(self):
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = subprocess.run(
            self.command(self.stream_claude)
            + ["--turn-transport", "stream-json"],
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        process_rows = [row for row in trace if row["event"] == "process-start"]
        turns = [row for row in trace if row["event"] == "turn-start"]
        self.assertEqual(len(process_rows), 1, trace)
        self.assertEqual(len(turns), 2, trace)
        self.assertEqual({row["pid"] for row in turns}, {process_rows[0]["pid"]})
        self.assertIn("--input-format", process_rows[0]["args"])
        self.assertNotIn("--resume", process_rows[0]["args"])
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        starts = [row for row in rows if row.get("type") == "dispatch.supervisor.turn-started"]
        completed = [
            row for row in rows if row.get("type") == "dispatch.supervisor.turn-completed"
        ]
        joins = [row for row in rows if row.get("type") == "dispatch.supervisor.join-completed"]
        teardowns = [
            row for row in rows if row.get("type") == "dispatch.supervisor.teardown-completed"
        ]
        self.assertEqual(len(starts), 2, rows)
        self.assertEqual(len(completed), 2, rows)
        self.assertEqual(len(joins), 1, rows)
        self.assertEqual(joins[0]["delivery_timing_schema_version"], 1)
        self.assertIsInstance(joins[0]["join_completed_ns"], int)
        # The fake join closes a row without a marker or committed receipt.
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
        self.assertEqual(len(teardowns), 1, rows)
        self.assertEqual(teardowns[0]["reason"], "route-terminal")
        self.assertTrue(all(row["transport"] == "stream-json" for row in starts))
        self.assertTrue(all(row["duration_seconds"] >= 0 for row in completed + joins + teardowns))
        self.assertEqual(rows[-1]["type"], "result")

    def _run_with_stale_notification(self, echo_uuid):
        # 2026-09-27: two owners resumed into a 0 s turn that returned the
        # previous runtime_wait (a backgrounded call's own turn) and died
        # dead-contract. The stale origin-bearing result must be skipped.
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = subprocess.run(
            self.command(self.stream_claude) + ["--turn-transport", "stream-json"],
            input="initial assignment", text=True, capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace), FAKE_STALE_NOTIFICATION="1",
                               FAKE_ECHO_UUID=echo_uuid),
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(rows[-1]["type"], "result")
        self.assertIn("verdict: PASS", rows[-1]["result"])
        skipped = [row for row in rows if row.get("type") == "dispatch.supervisor.result-skipped"]
        self.assertEqual([row["origins"] for row in skipped], [["task-notification"]], rows)
        teardowns = [row for row in rows if row.get("type") == "dispatch.supervisor.teardown-completed"]
        self.assertEqual([row["reason"] for row in teardowns], ["route-terminal"])
        turns = [json.loads(line) for line in self.trace.read_text().splitlines()
                 if json.loads(line).get("event") == "turn-start"]
        uuids = [turn["uuid"] for turn in turns]
        self.assertEqual(len(uuids), 2)
        self.assertTrue(all(uuids) and len(set(uuids)) == 2, uuids)

    def test_stale_notification_result_is_skipped_without_uuid_echo(self):
        self._run_with_stale_notification("0")

    def test_stale_notification_result_is_skipped_with_uuid_echo(self):
        self._run_with_stale_notification("1")

    def test_result_answers_turn_rule(self):
        mine = "5f0c8a2e-0000-4000-8000-000000000001"
        self.assertTrue(supervisor.result_answers_turn({"type": "result"}, mine))
        stale = {"type": "result", "origin": {"kind": "task-notification"}}
        self.assertFalse(supervisor.result_answers_turn(stale, mine))
        self.assertFalse(supervisor.result_answers_turn(stale))
        folded = dict(stale, user_message_uuids=["other", mine])
        self.assertTrue(supervisor.result_answers_turn(folded, mine))
        self.assertFalse(supervisor.result_answers_turn(dict(stale, user_message_uuids=["other"]), mine))
        # Older runtimes echo a single `user_message_uuid`.
        self.assertTrue(supervisor.result_answers_turn(dict(stale, user_message_uuid=mine), mine))
        peer = {"type": "result", "origin": {"kind": "peer", "name": "home-os-21"}}
        self.assertFalse(supervisor.result_answers_turn(peer, mine))

    def test_terminal_marker_closes_stream_without_final_owner_turn(self):
        route = self.base / "terminal-route.json"
        route_value = seal_route({
            "schema_version": 2,
            "cwd": str(self.base),
            "nodes": [{"id": "report", "terminal": True}],
            "workflow_contract": {"terminal_nodes": ["report"]},
            "resume_retry_boundaries": [],
        })
        route.write_text(json.dumps(route_value), encoding="utf-8")
        marker = self.base / "report.json"
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = subprocess.run(
            self.command(self.stream_claude)
            + [
                "--turn-transport", "stream-json",
                "--route-file", str(route),
                "--route-id", route_value["route_id"],
                "--route-hash", route_value["route_hash"],
            ],
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(
                FAKE_TRACE=str(self.trace),
                FAKE_TERMINAL_ROUTE=str(route),
                FAKE_TERMINAL_MARKER=str(marker),
            ),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        turns = [row for row in trace if row["event"] == "turn-start"]
        self.assertEqual(len(turns), 1, trace)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        fast = [
            row for row in rows
            if row.get("type") == "dispatch.supervisor.terminal-fast-path"
        ]
        self.assertEqual(len(fast), 1, rows)
        self.assertEqual(fast[0]["terminal_nodes"], ["report"])
        self.assertTrue(fast[0]["continuation_saved"])
        self.assertFalse(
            any(row.get("type") == "dispatch.supervisor.resumed" for row in rows)
        )
        self.assertEqual(rows[-1]["type"], "result")
        self.assertEqual(
            rows[-1]["result"], "artifact: -\nverdict: PASS\nblocker: none"
        )
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("failure_class=pass", registry)
        self.assertIn("reconcile_reason=exact-final-handoff", registry)

    def test_main_checked_settlement_skips_reserved_prompt_after_exact_join(self):
        # Exercise the real main loop and fake transport/join processes. The
        # settlement adapter is stubbed; its authority proof has separate tests.
        import io
        import dispatch_terminal_commit as terminal
        route = self.base / "checked-route.json"
        route_value = dict(cwd=str(self.base), artifact_root=str(self.artifact_root),
            nodes=[dict(id="report", terminal=True)],
            workflow_contract=dict(terminal_nodes=["report"]),
            runtime_support=dict(terminal_commit=True))
        route_value["route_hash"] = supervisor.canonical_route_hash(route_value)
        route_value["route_id"] = supervisor.route_id_from_hash(route_value["route_hash"])
        route.write_text(json.dumps(route_value))
        report = self.artifact_root / "report.md"; report.write_text("fixture evidence")
        envelope = f"artifact: {report}\nverdict: PASS\nblocker: none\n"
        slot = terminal.terminal_slot(self.artifact_root, route_value["route_id"], PARENT)
        slot.mkdir(parents=True)
        def settled_adapter(*unused):
            (slot / "terminal-commit.json").write_text(json.dumps(dict(state="owner-envelope-sealed",
                owner_attempt_id=PARENT, route_hash=route_value["route_hash"], terminal_commit_id="fixture-commit")))
            return terminal.TerminalCommitResult("completed", None, None, ("report",), envelope)
        self.jobs.write_text(owner_row(self.lease) + child_row())
        events = []
        args = self.command()[2:] + ["--route-file", str(route),
            "--route-id", route_value["route_id"], "--route-hash", route_value["route_hash"],
            "--enable-terminal-commit", "--max-continuations", "1"]
        with mock.patch.dict(os.environ, self.child_env(FAKE_TRACE=str(self.trace))), \
             mock.patch.object(supervisor.sys, "stdin", io.StringIO("initial assignment")), \
             mock.patch.object(supervisor, "emit", events.append), \
             mock.patch.object(supervisor, "terminal_commit_adapter", side_effect=settled_adapter), \
             mock.patch.object(supervisor, "prepare_cleanup_handoff") as cleanup:
            rc = supervisor.main(args)
        self.assertEqual(rc, 0, events)
        cleanup.assert_not_called()
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(sum(row["event"] == "turn-start" for row in trace), 1)
        self.assertFalse(supervisor.budget_record.read_rows(self.base, PARENT))
        saved = [event for event in events if event.get("continuation_saved")]
        self.assertEqual(len(saved), 1, events)
        self.assertEqual(events[-1]["result"], envelope)
        self.assertIn("\tdone\t", self.jobs.read_text())
        completed = list((self.base / "terminal-handoffs").glob("v1/*/*/completion.json"))
        self.assertEqual(len(completed), 1)
        self.assertEqual(json.loads(completed[0].read_text())["status"], "completed")
        replay_events=[]
        with mock.patch.dict(os.environ,self.child_env()), \
             mock.patch.object(supervisor.sys,"stdin",io.StringIO("original assignment")), \
             mock.patch.object(supervisor,"emit",replay_events.append), \
             mock.patch.object(supervisor,"terminal_commit_adapter",side_effect=settled_adapter), \
             mock.patch.object(supervisor,"run_turn") as duplicate_send:
            self.assertEqual(supervisor.main(args),70,replay_events)
        duplicate_send.assert_not_called()
        self.assertEqual(replay_events[-1]["reason"],"supervisor-lease-attempt-not-open")
        self.assertIn("failure_class=pass",self.jobs.read_text())

    def test_main_restart_reconciles_three_submission_states_without_transport(self):
        import io
        import dispatch_terminal_commit as terminal
        route=self.base/"restart-route.json"
        value=dict(cwd=str(self.base),artifact_root=str(self.artifact_root),
                   nodes=[dict(id="report",terminal=True)],runtime_support=dict(terminal_commit=True))
        value["route_hash"]=supervisor.canonical_route_hash(value)
        value["route_id"]=supervisor.route_id_from_hash(value["route_hash"])
        route.write_text(json.dumps(value))
        report=self.artifact_root/"restart-report.md"; report.write_text("verified fixture")
        envelope=f"artifact: {report}\nverdict: PASS\nblocker: none\n"
        slot=terminal.terminal_slot(self.artifact_root,value["route_id"],PARENT);slot.mkdir(parents=True)
        (slot/"terminal-commit.json").write_text(json.dumps(dict(state="owner-envelope-sealed",
            owner_attempt_id=PARENT,route_hash=value["route_hash"],terminal_commit_id="restart-commit")))
        for status in ("submitted","not-submitted","submission-unknown","no-intent"):
            with self.subTest(status=status):
                state_root=self.base/status;state_root.mkdir();jobs=state_root/"jobs.log"
                lease=state_root/"supervisor-state"/f"{PARENT}.lease"
                jobs.write_text(owner_row(lease))
                claim=supervisor.budget_record.claim_terminal_handoff(state_root,owner_attempt_id=PARENT,
                    route_hash=value["route_hash"],child_attempt_ids=[])
                if status != "no-intent":
                    intent=supervisor.budget_record.convert_claim_to_prompt_intent(state_root,claim,
                        prompt="cleanup",cleanup_scope=dict(owner_attempt_id=PARENT,route_id=value["route_id"],
                        route_hash=value["route_hash"],artifact_root=str(self.artifact_root)))
                    supervisor.budget_record.begin_submission(state_root,intent)
                if status not in {"submission-unknown","no-intent"}:
                    supervisor.budget_record.reconcile_submission(state_root,intent,
                        evidence_kind="transport-receipt" if status=="submitted" else "pre-send-failure",
                        evidence=dict(intent_id=intent["intent_id"],prompt_digest=intent["prompt_digest"]))
                argv=self.command()[2:]
                argv[argv.index("--jobs")+1]=str(jobs)
                if "--lease-file" in argv:
                    argv[argv.index("--lease-file")+1]=str(lease)
                argv += ["--route-file",str(route),"--route-id",value["route_id"],
                         "--route-hash",value["route_hash"],"--enable-terminal-commit"]
                events=[]
                with mock.patch.dict(os.environ,self.child_env()), \
                     mock.patch.object(supervisor.sys,"stdin",io.StringIO("original assignment")), \
                     mock.patch.object(supervisor,"emit",events.append), \
                     mock.patch.object(supervisor,"run_turn") as send, \
                     mock.patch.object(supervisor,"ClaudeStreamSession") as stream, \
                     mock.patch.object(supervisor,"terminal_commit_adapter",return_value=
                       terminal.TerminalCommitResult("completed",None,None,("report",),envelope)) as settle:
                    rc=supervisor.main(argv)
                send.assert_not_called();stream.assert_not_called()
                self.assertEqual(rc,70 if status=="submission-unknown" else 0,events)
                observed=[e for e in events if e.get("type")=="dispatch.supervisor.terminal-handoff-recovered"]
                self.assertTrue(observed,events)
                self.assertEqual(observed[0]["status"],"not-submitted" if status=="no-intent" else status)
                if status=="submission-unknown":
                    settle.assert_not_called();self.assertIn("\topen\t",jobs.read_text())
                    self.assertIsNone(observed[0]["effective_reserved_charge"])
                else:
                    settle.assert_called_once()
                    self.assertEqual(observed[0]["effective_reserved_charge"],int(status=="submitted"))
                    self.assertEqual(sum(bool(e.get("continuation_saved")) for e in events),int(status in {"not-submitted","no-intent"}))

    def test_terminal_fast_path_rejects_mismatched_marker(self):
        route = self.base / "terminal-route.json"
        route_value = seal_route({
            "schema_version": 2,
            "cwd": str(self.base),
            "nodes": [{"id": "report", "terminal": True}],
            "workflow_contract": {"terminal_nodes": ["report"]},
            "resume_retry_boundaries": [],
        })
        route.write_text(json.dumps(route_value), encoding="utf-8")
        marker = self.base / "report.json"
        marker.write_text(json.dumps({
            "schema_version": 2,
            "route_id": route_value["route_id"],
            "route_hash": route_value["route_hash"],
            "node_id": "report",
            "attempt_id": "att-other",
        }), encoding="utf-8")
        args = SimpleNamespace(
            route_file=str(route),
            route_id=route_value["route_id"],
            route_hash=route_value["route_hash"],
        )
        row = SimpleNamespace(
            status="done",
            attempt_id="att-child",
            metadata={
                "failure_class": "pass",
                "route_id": route_value["route_id"],
                "route_hash": route_value["route_hash"],
                "route_node": "report",
                "completion_marker": str(marker),
            },
        )
        self.assertEqual(supervisor.terminal_route_completion(args, [row]), ())

    def test_terminal_route_completion_uses_canonical_four_key_exclusion_not_legacy_two_key(self):
        """A82-1/F-9: `route_identity.ROUTE_HASH_EXCLUDED_KEYS` is the four-key
        canonical exclusion set (route_hash, route_id, owner_attempt_id,
        route_family_key). A route compiled with `owner_attempt_id` and
        `route_family_key` present must still verify under the supervisor's
        hash check -- a legacy two-key (route_hash, route_id only) inline
        recomputation would fold those two extra keys into the hash and
        reject every such route (the exact F-9 regression)."""
        sys.path.insert(0, str(ROOT / "utilities"))
        import route_identity

        route_value = {
            "schema_version": 2,
            "cwd": str(self.base),
            "nodes": [{"id": "report", "terminal": True}],
            "workflow_contract": {"terminal_nodes": ["report"]},
            "resume_retry_boundaries": [],
            "owner_attempt_id": "att-owner-xyz",
            "route_family_key": "family-abc",
        }
        digest = route_identity.route_hash(route_value)
        route_value["route_hash"] = digest
        route_value["route_id"] = route_identity.route_id_from_hash(digest)
        route = self.base / "canonical-exclusion-route.json"
        route.write_text(json.dumps(route_value), encoding="utf-8")
        marker = self.base / "report.json"
        marker.write_text(json.dumps({
            "schema_version": 2,
            "route_id": route_value["route_id"],
            "route_hash": route_value["route_hash"],
            "node_id": "report",
            "attempt_id": "att-child",
        }), encoding="utf-8")
        args = SimpleNamespace(
            route_file=str(route),
            route_id=route_value["route_id"],
            route_hash=route_value["route_hash"],
        )
        row = SimpleNamespace(
            status="done",
            attempt_id="att-child",
            metadata={
                "failure_class": "pass",
                "route_id": route_value["route_id"],
                "route_hash": route_value["route_hash"],
                "route_node": "report",
                "completion_marker": str(marker),
            },
        )
        self.assertEqual(
            supervisor.terminal_route_completion(args, [row]), ("report",)
        )
        # And the legacy two-key recomputation this cycle removed would have
        # produced a *different* digest for the same payload -- pin that gap
        # explicitly so a future regression back to the inline form is caught
        # even if this route happens not to carry the two extra keys.
        legacy_bare = {
            key: value for key, value in route_value.items()
            if key not in {"route_hash", "route_id"}
        }
        legacy_digest = "sha256:" + hashlib.sha256(
            json.dumps(legacy_bare, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertNotEqual(legacy_digest, digest)

    def test_terminal_route_completion_deferred_rows(self):
        """C3 (S3a): `verdict_pass(metadata) or success_note(metadata)` replaces
        the inline `failure_class == "pass" or note in SUCCESS_NOTES` OR.

        A row the completion budget already published the marker for reads
        the literal note `completed-marker` regardless of its deferred origin
        (the marker-append writer stamps that note unconditionally), so this
        is a pinning test, not a red/green regression: both the pre-swap and
        post-swap expressions already accept it. A row still only
        typed-deferred (no marker) is excluded before and after, the same way
        an ordinary non-pass row always was.
        """
        route = self.base / "terminal-route-deferred.json"
        route_value = seal_route({
            "schema_version": 2,
            "cwd": str(self.base),
            "nodes": [{"id": "report", "terminal": True}],
            "workflow_contract": {"terminal_nodes": ["report"]},
            "resume_retry_boundaries": [],
        })
        route.write_text(json.dumps(route_value), encoding="utf-8")
        marker = self.base / "report.json"
        marker.write_text(json.dumps({
            "schema_version": 2,
            "route_id": route_value["route_id"],
            "route_hash": route_value["route_hash"],
            "node_id": "report",
            "attempt_id": "att-deferred",
        }), encoding="utf-8")
        args = SimpleNamespace(
            route_file=str(route),
            route_id=route_value["route_id"],
            route_hash=route_value["route_hash"],
        )
        completed_row = SimpleNamespace(
            status="done",
            attempt_id="att-deferred",
            metadata={
                "note": "completed-marker",
                "failure_class": "infrastructure",
                "classifier_source": "registered-wrapper-completion-transient-v1",
                "completion_marker": str(marker),
                "route_id": route_value["route_id"],
                "route_hash": route_value["route_hash"],
                "route_node": "report",
            },
        )
        self.assertEqual(
            supervisor.terminal_route_completion(args, [completed_row]), ("report",)
        )
        pending_row = SimpleNamespace(
            status="done",
            attempt_id="att-deferred",
            metadata={
                "note": "completion-deferred",
                "failure_class": "infrastructure",
                "classifier_source": "registered-wrapper-completion-transient-v1",
                "route_id": route_value["route_id"],
                "route_hash": route_value["route_hash"],
                "route_node": "report",
            },
        )
        self.assertEqual(supervisor.terminal_route_completion(args, [pending_row]), ())

    def test_supervisor_source_has_no_second_inline_route_hash_expression(self):
        """A82-1: supervisor must call the canonical helper exactly once and
        must not carry its own inline sha256-over-bare-route expression."""
        source = SUPERVISOR.read_text(encoding="utf-8")
        self.assertNotIn('key not in {"route_hash", "route_id"}', source)
        self.assertIn("canonical_route_hash(route)", source)

    def test_hash_correctness_alone_does_not_enable_the_live_terminal_commit_fast_path(self):
        """§13.53.2: hash correction landing is not sufficient by itself to
        flip on the live (`terminal_commit_mode`) fast path -- that requires
        the explicit checked support switch (`--enable-terminal-commit` or
        `AGENT_DISPATCH_TERMINAL_COMMIT=1`), never route-hash agreement."""
        args = SimpleNamespace(
            route_file=str(self.base / "does-not-need-to-exist.json"),
            route_id="rt-0000000000000000",
            route_hash="sha256:" + ("0" * 64),
            enable_terminal_commit=False,
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_DISPATCH_TERMINAL_COMMIT", None)
            self.assertFalse(supervisor.terminal_commit_enabled(args))

    def test_session_announcement_precedes_every_turn_and_leaks_nothing(self):
        """The receipt log must name the child session it never transcribes.

        Regression: with no announcement the summary owner had only this log to
        read, and a log of control rows plus one `result` yields no conversational
        text at all — so supervised owners rendered in Fleet with no title and no
        NOW line for their entire run.
        """
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = self.run_supervisor()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        announcements = [
            row for row in rows if row.get("type") == "dispatch.supervisor.session"
        ]
        self.assertEqual(len(announcements), 1)
        self.assertEqual(rows[0], announcements[0])
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        turns = [row for row in trace if row["event"] == "turn-start"]
        self.assertTrue(turns)
        for turn in turns:
            self.assertEqual(announcements[0]["session_id"], turn["session"])
        self.assertEqual(announcements[0]["cwd"], str(self.base))
        # Announcing identity must not become a channel for model or prompt content.
        self.assertEqual(
            set(announcements[0]),
            {"type", "parent_attempt_id", "session_id", "cwd"},
        )
        self.assertNotIn("RAW_PARENT_CONTEXT_SENTINEL", result.stdout)
        self.assertNotIn("RAW_CLAUDE_SENTINEL", result.stdout)
        self.assertEqual(sum(row.get("type") == "result" for row in rows), 1)

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
        self.claude.write_text(self.claude.read_text().replace("resume = '--resume' in args", "resume = '--resume' in args\nif resume: open(os.environ['FAKE_REPORT_PROOF'], 'w').write('proved')"))
        command = self.command(); command[1] = str(wrapper)
        command += ["--route-file", str(route), "--route-id", value["route_id"], "--route-hash", value["route_hash"]]
        result = subprocess.run(command, input="initial assignment", text=True, capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace), FAKE_NO_CHILD="1", FAKE_REPORT_PROOF=str(proof)), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        turns = [json.loads(line) for line in self.trace.read_text().splitlines() if json.loads(line).get("event") == "turn-start"]
        self.assertEqual(len(turns), 2, turns)
        self.assertIn("[workflow-completion-pending]", turns[1]["prompt"])
        self.assertIn("report", turns[1]["prompt"])
        self.assertIn("workflow-completion-incomplete", result.stdout)
        self.assertEqual(len(self.jobs.read_text().splitlines()), 1)
        self.assertIn("completed-supervisor", self.jobs.read_text())

    def test_no_child_finishes_without_resume(self):
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = self.run_supervisor(FAKE_NO_CHILD="1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(len(trace), 1)
        self.assertFalse(trace[0]["resume"])
        self.assertFalse(self.state.exists())
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            sum(row.get("type") == "dispatch.supervisor.owner-boundary" for row in rows),
            0,
        )

    def test_input_queued_before_the_first_turn_reaches_that_turn(self):
        import dispatch_owner_input as owner_input
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        owner_input.initialize_owner_input(self.jobs, PARENT, "claude-next-turn")
        owner_input.submit(self.jobs, PARENT, "early word", "early")
        result = self.run_supervisor(FAKE_NO_CHILD="1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(len(trace), 1)
        self.assertFalse(trace[0]["resume"])
        self.assertIn("early word", trace[0]["prompt"])
        receipt = owner_input.inspect(self.jobs, PARENT)
        self.assertEqual(receipt["requests"][0]["state"], "turn-completed")
        self.assertEqual(receipt["requests"][0]["thread_id"], trace[0]["session"])
        self.assertFalse(receipt["accepting"])

    def test_runtime_v1_owner_without_route_arguments_finishes(self):
        self.jobs.write_text(
            owner_row(self.lease).replace("attempt_id=", "workflow_completion=runtime-v1,attempt_id="),
            encoding="utf-8")
        result = self.run_supervisor(FAKE_NO_CHILD="1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_empty_runtime_wait_retries_start_in_same_session_before_join(self):
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = self.run_supervisor(
            FAKE_DRY_RUN_FIRST="1", FAKE_JOBS=str(self.jobs)
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        turns = [row for row in trace if row["event"] == "turn-start"]
        self.assertEqual(len(turns), 3)
        self.assertTrue(turns[1]["resume"])
        self.assertIn("rerun the checked child dispatch with --start", turns[1]["prompt"])
        self.assertIn("registered=1, started=1, and child_spawned=1", turns[1]["prompt"])
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(any(
            row.get("continuation_reason") == "runtime-wait-without-started-child"
            and row.get("state") == "registration-required"
            for row in rows
        ))

    def test_turn_without_the_sentinel_settles_the_launch_started_race(self):
        # Two children registered, their fenced start published just after the owner's turn
        # ended without `runtime_wait`: the runtime waits for them, as Codex does.
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        command = self.command()
        command[command.index("--join-interval") + 1] = "0.4"
        result = subprocess.run(
            command, input="initial assignment", text=True, capture_output=True, timeout=20,
            env=self.child_env(FAKE_TRACE=str(self.trace), FAKE_LAUNCH_STARTED_RACE="1",
                               FAKE_JOBS=str(self.jobs)))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(len([row for row in trace if row["event"] == "turn-start"]), 2)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertNotIn('"state": "registration-required"', result.stdout)
        self.assertEqual([row["attempt_count"] for row in rows
                          if row.get("type") == "dispatch.supervisor.launch-settled"], [2])
        self.assertEqual([row["attempt_count"] for row in rows
                          if row.get("type") == "dispatch.supervisor.parked"], [2])

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
        pending = child_row().replace("att-child", "att-pending").replace("launch_started=1", "launch_started=0")
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
        long_claude = self.base / "long_claude.py"
        long_claude.write_text(
            textwrap.dedent(
                """\
                import json, os, sys
                state_path = os.environ['AGENT_DISPATCH_COMPLETION_STATE_FILE']
                prompt = sys.stdin.read()
                with open(state_path, encoding='utf-8') as h:
                    delivered = json.load(h)['delivered_attempt_ids']
                turn = len(delivered) + 1
                if turn <= 13:
                    attempt = f'att-child-{turn}'
                    with open(os.environ['LONG_JOBS'], 'a', encoding='utf-8') as h:
                        h.write('2026-08-06T00:00:00Z\\topen\\t/repo\\t/wt\\t'
                                f'child-{turn}\\tattempt_schema_version=2,'
                                'dispatch_depth=2,transport=headless,'
                                'execution_surface=registered-headless,registered_worker=1,launch_started=1,'
                                f'attempt_id={attempt},parent_attempt_id=att-parent\\n')
                    text = 'runtime_wait: registered-children'
                else:
                    text = 'artifact: report.md\\nverdict: PASS\\nblocker: none'
                print(json.dumps({'type':'result','subtype':'success','is_error':False,
                                  'result':text}))
                """
            ),
            encoding="utf-8",
        )
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        command = self.command(long_claude) + [
            "--route-file", str(route),
            "--route-id", route_value["route_id"],
            "--route-hash", route_value["route_hash"],
        ]
        result = subprocess.run(
            command,
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace), LONG_JOBS=str(self.jobs)),
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
        self.assertEqual(sum(row.get("type") == "result" for row in rows), 1)
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

    def test_codex_child_uses_same_claude_resume_adapter(self):
        self.jobs.write_text(
            owner_row(self.lease) + child_row(harness="codex"), encoding="utf-8"
        )
        result = self.run_supervisor()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        turns = [row for row in trace if row["event"] == "turn-start"]
        self.assertEqual(len(turns), 2)
        self.assertFalse(turns[0]["resume"])
        self.assertTrue(turns[1]["resume"])
        self.assertEqual(turns[0]["session"], turns[1]["session"])
        self.assertEqual(turns[1]["delivered"], ["att-child"])
        self.assertNotIn("RAW_CLAUDE_SENTINEL", result.stdout)

    def test_completion_prompt_carries_only_exact_checked_harvest(self):
        prompt = supervisor.completion_prompt(
            {
                "schema_version": 2,
                "state": "ready",
                "parent_attempt_id": PARENT,
                "children": [
                    {
                        "attempt_id": "att-child-a",
                        "status": "open",
                        "readiness": "ready",
                        "reason": "terminal-observed",
                        "required_action": "complete-open",
                    },
                    {
                        "attempt_id": "att-child-b",
                        "status": "open",
                        "readiness": "ready",
                        "reason": "terminal-observed",
                        "required_action": "complete-open",
                    },
                ],
            },
            jobs="/tmp/fixture-jobs.log",
        )
        self.assertEqual(prompt.count("preflight.sh harvest --jobs"), 2)
        self.assertEqual(
            prompt.count(
                str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh")
                + " harvest --jobs /tmp/fixture-jobs.log --attempt-id"
            ),
            2,
        )
        self.assertIn("shared, runtime-neutral registry harvest compatibility surface", prompt)
        self.assertIn("does not select or change the owner or child harness", prompt)
        self.assertIn("--attempt-id att-child-a --status open --mark-done", prompt)
        self.assertIn("--attempt-id att-child-b --status open --mark-done", prompt)
        self.assertNotIn("RAW_CLAUDE_SENTINEL", prompt)

    def test_harvest_surface_survives_a_release_rotation(self):
        # Regression for the 2026-08-14 candidate 3 deadlock. Launched through a
        # managed `current` pointer, the supervisor used to resolve the harvest
        # surface down to the versioned release directory. After `current`
        # rotated, the owner's park guard no longer recognized that path, so it
        # denied every command the receipt told the owner to run.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name in ("v1", "v2"):
            release = tmp / "releases" / name
            (release / "core").mkdir(parents=True)
            (release / "core" / "CORE.md").write_text("x", encoding="utf-8")
            (release / "adapters" / "codex" / "bin").mkdir(parents=True)
            (release / "adapters" / "codex" / "bin" / "preflight.sh").write_text(
                "#!/bin/sh\n", encoding="utf-8"
            )
            (release / "utilities").mkdir()
        current = tmp / "current"
        current.symlink_to(tmp / "releases" / "v1")
        launched = current / "utilities" / "claude-session-supervisor.py"

        surface = supervisor.harvest_surface(str(launched))

        self.assertEqual(
            surface, str(current / "adapters" / "codex" / "bin" / "preflight.sh")
        )
        # Rotate `current` the way a managed release upgrade does.
        current.unlink()
        current.symlink_to(tmp / "releases" / "v2")
        self.assertTrue(Path(surface).is_file())
        self.assertEqual(
            Path(surface).resolve(),
            (tmp / "releases" / "v2" / "adapters" / "codex" / "bin"
             / "preflight.sh").resolve(),
        )

    def test_missing_result_has_no_false_terminal(self):
        broken = self.base / "broken.py"
        broken.write_text("print('not-json')\n", encoding="utf-8")
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = subprocess.run(
            self.command(broken),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"type":"result"', result.stdout)
        self.assertFalse(self.state.exists())
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/repo\t/wt\towner\t", registry)
        self.assertIn("note=dead-protocol", registry)

    def test_fable_429_is_exact_dead_capacity_and_never_stays_open(self):
        limited = self.base / "limited.py"
        limited.write_text(
            "import json\n"
            "print(json.dumps({'type':'result','subtype':'error_during_execution',"
            "'is_error':True,'terminal_reason':'api_error','api_error_status':429,"
            "'result':\"You've reached your Fable 5 limit; resets later\"}))\n",
            encoding="utf-8",
        )
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = subprocess.run(
            self.command(limited),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=10,
        )
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertNotIn("\topen\t/repo\t/wt\towner\t", registry)
        self.assertIn("note=dead-capacity", registry)
        self.assertIn("failure_class=capacity", registry)
        self.assertIn("api_status=429", registry)

    def test_http_auth_status_wins_over_incidental_capacity_words(self):
        denied = self.base / "denied.py"
        denied.write_text(
            "import json\n"
            "print(json.dumps({'type':'result','subtype':'error_during_execution',"
            "'is_error':True,'api_error_status':401,"
            "'result':'Unauthorized after rate limit check'}))\n",
            encoding="utf-8",
        )
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = subprocess.run(
            self.command(denied),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=10,
        )
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("note=dead-auth", registry)
        self.assertIn("failure_class=auth", registry)
        self.assertIn("api_status=401", registry)

    # -- Phase 4 (plan.md, round_1 finding 1 dependency): owner restoration --

    def _non_closing_join(self) -> Path:
        """A join fake that reports readiness but never rewrites jobs.log —
        reproduces the incident: a real terminal envelope exists but the
        registry row was never closed."""
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

    def _timeout_then_ready_join(self, timeouts: int) -> Path:
        """A join fake that reports `timeout` for the first N calls, then
        `ready` — reproduces the S-1 owner ordinal-4 timeout incident so the
        supervisor's repark loop can be exercised without a real 3600s wait."""
        counter = self.base / "join_calls.count"
        counter.write_text("0", encoding="utf-8")
        script = self.base / "fake_join_timeout.py"
        script.write_text(
            textwrap.dedent(
                f"""\
                import json, os, sys, time
                trace = os.environ['FAKE_TRACE']
                counter_path = {str(counter)!r}
                jobs = sys.argv[sys.argv.index('--jobs') + 1]
                parent = sys.argv[sys.argv.index('--parent-attempt-id') + 1]
                attempts = [sys.argv[i + 1] for i, value in enumerate(sys.argv) if value == '--attempt-id']
                calls = int(open(counter_path, encoding='utf-8').read())
                calls += 1
                state_path = os.path.join(os.path.dirname(trace), 'supervisor-state.json')
                with open(state_path, encoding='utf-8') as state_handle:
                    phase = json.load(state_handle)['phase']
                with open(counter_path, 'w', encoding='utf-8') as h:
                    h.write(str(calls))
                with open(trace, 'a', encoding='utf-8') as h:
                    h.write(json.dumps({{'event': 'join-call', 'time': time.monotonic(),
                                         'ordinal': calls, 'phase': phase}}) + '\\n')
                if calls <= {timeouts}:
                    print(json.dumps({{'schema_version': 2, 'state': 'timeout',
                        'parent_attempt_id': parent,
                        'children': [{{'attempt_id': attempt, 'status': 'open',
                                       'readiness': 'pending', 'reason': 'process-alive',
                                       'required_action': 'complete-open'}} for attempt in attempts]}}))
                else:
                    with open(jobs, encoding='utf-8') as h:
                        lines = h.read().splitlines()
                    kept, current = [], {{}}
                    for line in lines:
                        fields = line.split('\\t')
                        metadata = dict(part.split('=', 1) for part in fields[5].split(',') if '=' in part) if len(fields) == 6 else {{}}
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
                    print(json.dumps({{'schema_version': 2, 'state': 'ready',
                        'parent_attempt_id': parent,
                        'children': [{{'attempt_id': attempt, 'status': 'done',
                                       'readiness': 'ready', 'reason': 'registry-closed',
                                       'required_action': 'advance-completed'}} for attempt in attempts]}}))
                """
            ),
            encoding="utf-8",
        )
        return script

    def test_join_timeout_reparks_without_model_turn_or_continuation_spend(self):
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        join_script = self._timeout_then_ready_join(timeouts=2)
        result = subprocess.run(
            self.command_with_join(join_script),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        turn_starts = [row for row in trace if row["event"] == "turn-start"]
        join_calls = [row for row in trace if row["event"] == "join-call"]
        # Two internal timeouts must be reparked in place: exactly one initial
        # turn and one resume turn total, never a per-timeout model turn.
        self.assertEqual(len(turn_starts), 2, trace)
        self.assertFalse(turn_starts[0]["resume"])
        self.assertTrue(turn_starts[1]["resume"])
        # The join itself is retried across the timeouts until it resolves.
        self.assertEqual(len(join_calls), 3, trace)
        self.assertTrue(all(row["phase"] == "parked" for row in join_calls), trace)
        # A timeout receipt must never be folded into the delivered set that
        # is handed to the model — only the eventual `ready` receipt is.
        self.assertEqual(turn_starts[1]["delivered"], ["att-child"])
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(sum(row.get("type") == "result" for row in rows), 1)
        control = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if json.loads(line).get("type", "").startswith("dispatch.supervisor")
        ]
        reparked = [row for row in control if row["type"] == "dispatch.supervisor.reparked"]
        self.assertEqual(len(reparked), 2, control)
        self.assertEqual([row["repark_ordinal"] for row in reparked], [1, 2])

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

    def test_join_deadlines_preserve_owner_past_old_repark_bound(self):
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        # Deadlines cannot end a live dependency. The compatibility flag no
        # longer grants termination; eventual ready produces exactly one turn.
        join_script = self._timeout_then_ready_join(timeouts=10)
        cmd = self.command_with_join(join_script)
        idx = cmd.index("--join-timeout")
        cmd[idx + 1] = "0.05"
        result = subprocess.run(
            cmd + ["--max-join-reparks", "2"],
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("join-timeout-repark-exceeded", result.stdout + result.stderr)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        turn_starts = [row for row in trace if row["event"] == "turn-start"]
        self.assertEqual(len(turn_starts), 2, trace)

    def _blocked_child_row(self) -> str:
        log = self.base / "att-child.claude.jsonl"
        artifact = self.artifact_root / "brief.md"
        artifact.write_text("evidence\n", encoding="utf-8")
        log.write_text(
            json.dumps({"type": "system", "subtype": "init"}) + "\n"
            + json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "result": f"artifact: {artifact}\nverdict: BLOCKED\nblocker: stuck",
            }) + "\n",
            encoding="utf-8",
        )
        route = self.base / "route.json"
        return (
            f"2026-07-23T00:00:00Z\topen\t{self.base}\t{self.base}\tchild\t"
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,launch_started=1,"
            "fallback_hop=same-harness-headless,harness=claude,"
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
        join_script = self._non_closing_join()
        result = subprocess.run(
            self.command_with_join(join_script),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertNotIn(
            "owned-children-remain-open-after-resume",
            result.stdout + result.stderr,
        )
        reconciled = [
            row for row in rows
            if row.get("type") == "dispatch.supervisor.reconciled"
            and row.get("attempt_id") == "att-child"
        ]
        self.assertEqual(len(reconciled), 1, rows)
        self.assertEqual(reconciled[0]["outcome"], "closed")
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("dead-worker-blocked", registry)
        # The batch resolves through the ordinary join-then-reconcile cycle,
        # never through the model-facing `remediation_prompt` continuation —
        # every prompt after the first is the plain harvest/completion
        # receipt, not a "contract violation" remediation demand.
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        turns = [row for row in trace if row["event"] == "turn-start"]
        self.assertGreaterEqual(len(turns), 2, turns)
        for turn in turns[1:]:
            self.assertTrue(turn["resume"])
            self.assertNotIn("Runtime completion contract violation", turn["prompt"])

    # -- Phase D2 (plan SS3.4 D2a/D2b, checklist DC-6/DC-7/DC-8b/DC-14) --

    def _events(self, stdout: str) -> list[dict]:
        rows = []
        for line in stdout.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def _turn_starts(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.trace.read_text().splitlines()
            if json.loads(line)["event"] == "turn-start"
        ]

    def _divergent_surface_supervisor(self) -> Path:
        """A supervisor launched on a path whose harvest surface no guard admits.

        Reproduces the shape D2a exists to catch: the supervisor prescribes a
        command string the park guard cannot classify, so no model turn and no
        number of re-deliveries could ever satisfy the receipt. Built by
        launching through a symlink directory, exactly as the 2026-08-14
        release-rotation deadlock did.
        """

        link = self.base / "detached-launch"
        link.mkdir()
        for module in (ROOT / "utilities").glob("*.py"):
            (link / module.name).symlink_to(module)
        return link / "claude-session-supervisor.py"

    def test_attention_is_runtime_acknowledged_without_model_harvest(self):
        self.jobs.write_text(owner_row(self.lease) + self._blocked_child_row(), encoding="utf-8")
        result = subprocess.run(self.command_with_join(self._non_closing_join()),
            input="initial assignment", text=True, capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        turns = self._turn_starts()
        self.assertEqual(len(turns), 2, turns)
        self.assertIn("inspect-done-failure", turns[1]["prompt"])
        self.assertNotIn("redelivery-suppressed", result.stdout)
        self.assertIn("dead-worker-blocked", self.jobs.read_text())
        self.assertNotIn("owner-redelivery-abandoned", self.jobs.read_text())
        self.assertFalse(self.state.exists())

    def test_compatibility_command_path_cannot_prevent_notification_delivery(self):
        self.jobs.write_text(owner_row(self.lease) + self._blocked_child_row(), encoding="utf-8")
        cmd = self.command_with_join(self._non_closing_join())
        cmd[1] = str(self._divergent_surface_supervisor())
        result = subprocess.run(cmd, input="initial assignment", text=True,
            capture_output=True, env=self.child_env(FAKE_TRACE=str(self.trace)), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(self._turn_starts()), 2)
        self.assertNotIn("receipt-unsatisfiable", result.stdout)
        self.assertNotIn("owner-attention-unactionable", self.jobs.read_text())

    def test_d7_ordinary_advance_suppresses_nothing_and_spends_one_continuation(self):
        # Regression: the normal path -- a receipt whose row advances between
        # passes -- must be byte-identical to its pre-D2 behaviour.
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = self.run_supervisor()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        events = self._events(result.stdout)
        for kind in (
            "dispatch.supervisor.redelivery-suppressed",
            "dispatch.supervisor.receipt-unsatisfiable",
        ):
            self.assertEqual([row for row in events if row.get("type") == kind], [], events)
        resumed = [row for row in events if row.get("type") == "dispatch.supervisor.resumed"]
        self.assertEqual([row["continuation_ordinal"] for row in resumed], [1], events)
        self.assertEqual(len(self._turn_starts()), 2, self.trace.read_text())

    def test_d8_recovered_outbox_reconciles_before_the_first_refresh(self):
        # D3: a supervisor restarting onto an open-but-finished child must
        # reconcile before it refreshes, or it hands the owner a receipt whose
        # prescribed action the registry has already made impossible.
        self.jobs.write_text(
            owner_row(self.lease) + self._blocked_child_row(), encoding="utf-8"
        )
        receipt = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": PARENT,
            "children": [
                {
                    "attempt_id": "att-child",
                    "status": "open",
                    "readiness": "ready",
                    "reason": "terminal-observed",
                    "required_action": "complete-open",
                }
            ],
            "delivery_timing": {
                "delivery_timing_schema_version": 1,
                **{point: None for point in DELIVERY_TIMING_POINTS},
            },
        }
        rows = join.current_children(self.jobs, PARENT, {"att-child"})
        join.prepare_supervisor_outbox(self.state, PARENT, set(), receipt, rows)
        result = subprocess.run(
            self.command_with_join(self._non_closing_join()),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=30,
        )
        events = self._events(result.stdout)
        kinds = [row.get("type") for row in events]
        self.assertIn("dispatch.supervisor.reconciled", kinds, events)
        # Reconcile precedes the first turn the recovered receipt is delivered on.
        self.assertLess(
            kinds.index("dispatch.supervisor.reconciled"),
            kinds.index("dispatch.supervisor.turn-started"),
            events,
        )
        # And what it then delivers is satisfiable.
        self.assertEqual(
            [row for row in events
             if row.get("type") == "dispatch.supervisor.receipt-unsatisfiable"],
            [],
            events,
        )

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
                # The controller reads concurrently. Truncate/write briefly
                # removes the attempt and legitimately enters its 30s observer
                # backoff; publish the fixture just like the atomic real writer.
                staged = self.jobs.with_suffix(".settled")
                staged.write_text(parent + settled)
                staged.replace(self.jobs)
                self.assertEqual(process.wait(timeout=10), 0, output.read_text())
                turns = [json.loads(line) for line in self.trace.read_text().splitlines()
                         if json.loads(line)["event"] == "turn-start"]
                self.assertEqual(len(turns), 2)
                self.assertNotIn("owner-redelivery-abandoned", self.jobs.read_text())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)




class TypedReceiptStageAdvanceNegotiationTest(unittest.TestCase):
    """SD-110 A-18: an un-negotiated (default) call takes the literal,
    unmodified v2 path -- golden-byte identical to the pre-SD-110 receipt."""

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
        receipt = supervisor.typed_receipt(value, PARENT, {"att-child"})
        golden = json.dumps(receipt, sort_keys=True)
        negotiated_but_recordless = supervisor.typed_receipt(
            value, PARENT, {"att-child"}, accept_stage_advance=True
        )
        self.assertEqual(receipt["schema_version"], 2)
        self.assertNotIn("stage_advance", receipt)
        self.assertEqual(json.dumps(negotiated_but_recordless, sort_keys=True), golden)
        # SD-119: the chain-advance path is wired into the supervisor, but a
        # join with no sub-session chain metadata is a no-op -- this join's
        # receipt bytes stay byte-identical to pre-SD-119, and no chain key
        # ever appears in it.
        no_chain = supervisor.subsession_advance.coordinate_chain_advance_from_joined_rows(
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
        receipt = supervisor.typed_receipt(
            value,
            PARENT,
            {"att-child"},
            accept_stage_advance=True,
            stage_advance_record=record,
        )
        self.assertEqual(receipt["schema_version"], 3)
        self.assertEqual(receipt["stage_advance"], record)


class StageAdvanceWiringTest(unittest.TestCase):
    """Block 4: `attempt_stage_advance` wiring at the `terminal_route_completion`
    call site -- `coordinate_stage_advance` itself is fully covered by
    `dispatch_stage_advance.test.py`; this only proves the supervisor extracts
    the right predecessor/phase inputs, emits the right canary event, defaults
    to a byte-identical no-op, and never lets an advance exception escape."""

    def setUp(self) -> None:
        self.events: list[dict] = []
        self._orig_emit = supervisor.emit
        supervisor.emit = self.events.append
        self.addCleanup(lambda: setattr(supervisor, "emit", self._orig_emit))

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
            supervisor.stage_advance, "coordinate_stage_advance"
        ) as coordinate:
            supervisor.attempt_stage_advance(args, rows, {"att-child"})
        coordinate.assert_not_called()
        self.assertEqual(self.events, [])

    def test_enabled_advanced_emits_stage_advance_event(self):
        args = self._args(enable_stage_advance=True)
        rows = [self._row("att-child")]
        fake_result = supervisor.stage_advance.StageAdvanceResult(
            outcome="advanced", reason="", stage_advance_id="sadv-fixture",
            successor_node="b", successor_attempt_id="att-b",
            claim_key=(args.route_hash, "b", 0),
            brief_template_digest="sha256:" + "b" * 64, gate_closed=True,
            registered=True, started=True, child_spawned=True, record_path=None,
        )
        timing = {"last_child_terminal_ns": 1000, "join_completed_ns": 2000}
        with mock.patch.object(
            supervisor.stage_advance, "coordinate_stage_advance",
            return_value=fake_result,
        ) as coordinate:
            supervisor.attempt_stage_advance(args, rows, {"att-child"}, timing)
        coordinate.assert_called_once()
        request = coordinate.call_args[0][0]
        self.assertEqual(request.predecessor_node, "a")
        self.assertEqual(request.predecessor_terminal_attempt_id, "att-child")
        self.assertEqual(request.parent_attempt_id, PARENT)
        self.assertEqual(request.supervisor_phase, "parked")
        self.assertEqual(request.delivered_open_attempt_ids, frozenset())
        self.assertEqual(request.receipt_schema_negotiated, 3)
        self.assertIsInstance(
            coordinate.call_args[0][1], supervisor.stage_advance.RealStageAdvanceServices
        )
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["type"], "dispatch.supervisor.stage-advance")
        self.assertEqual(event["advance_mode"], "runtime-deterministic")
        self.assertEqual(event["outcome"], "advanced")
        self.assertEqual(event["predecessor_node"], "a")
        self.assertEqual(event["successor_node"], "b")
        canary = event["delivery_timing"]
        self.assertEqual(canary["last_child_terminal_ns"], 1000)
        self.assertEqual(canary["join_completed_ns"], 2000)
        self.assertIsNone(canary["same_thread_resume_ns"])
        self.assertIsNone(canary["exact_harvest_ns"])
        self.assertIsInstance(canary["next_stage_start_ns"], int)
        self.assertEqual(event["route_hash"], args.route_hash)
        self.assertEqual(event["parent_attempt_id"], PARENT)

    def test_open_sibling_reports_running_turn_phase(self):
        """T1 correction (round-1 blocking finding 1): the real intersection
        of currently open/running attempt ids must reach the core as
        `delivered_open_attempt_ids`, not an unconditional empty frozenset --
        that constant is exactly what let a live path start a successor
        while another child remained open, because the one guard meant to
        catch it (`request.delivered_open_attempt_ids`) could never fire."""

        args = self._args(enable_stage_advance=True)
        rows = [
            self._row("att-child"),
            self._row("att-open", status="open"),
            self._row("att-running", status="running"),
        ]
        fake_result = supervisor.stage_advance.StageAdvanceResult(
            outcome="refused", reason="stage-advance-phase-ineligible",
            stage_advance_id="", successor_node=None, successor_attempt_id=None,
            claim_key=None, brief_template_digest="", gate_closed=False,
            registered=False, started=False, child_spawned=False, record_path=None,
        )
        with mock.patch.object(
            supervisor.stage_advance, "coordinate_stage_advance",
            return_value=fake_result,
        ) as coordinate:
            supervisor.attempt_stage_advance(args, rows, {"att-child"})
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
            supervisor.stage_advance, "coordinate_stage_advance"
        ) as coordinate:
            supervisor.attempt_stage_advance(args, rows, {"att-child"})
        coordinate.assert_not_called()
        self.assertEqual(self.events, [])

    def test_service_exception_is_swallowed_as_a_refusal_event_never_raises(self):
        args = self._args(enable_stage_advance=True)
        rows = [self._row("att-child")]
        with mock.patch.object(
            supervisor.stage_advance, "coordinate_stage_advance",
            side_effect=RuntimeError("boom"),
        ):
            supervisor.attempt_stage_advance(args, rows, {"att-child"})
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["type"], "dispatch.supervisor.stage-advance-refused")
        self.assertEqual(event["outcome"], "refused")
        self.assertEqual(event["reason"], "RuntimeError")

    def test_advanced_outcome_returns_the_durable_record_for_receipt_delivery(self):
        """§13.32.1-(2)6/(3)B: `attempt_stage_advance` reads the durable
        `stage_advance_record_v1` off disk (the same file
        `RealStageAdvanceServices` would have fsynced) so the call site can
        feed it straight into `receipt_with_stage_advance` under the SAME
        `enable_stage_advance` condition that produced `receipt_schema_negotiated
        == 3` above -- never a second, independently-toggled decision."""

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
            fake_result = supervisor.stage_advance.StageAdvanceResult(
                outcome="advanced", reason="", stage_advance_id="sadv-fixture",
                successor_node="b", successor_attempt_id="att-b",
                claim_key=(args.route_hash, "b", 0),
                brief_template_digest="sha256:" + "b" * 64, gate_closed=True,
                registered=True, started=True, child_spawned=True,
                record_path=record_path,
            )
            with mock.patch.object(
                supervisor.stage_advance, "coordinate_stage_advance",
                return_value=fake_result,
            ):
                returned = supervisor.attempt_stage_advance(args, rows, {"att-child"})
        self.assertEqual(returned, record)

        base_receipt = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": PARENT,
            "children": [],
        }
        # The single condition ON: this is the only path that may ever
        # produce a v3 delivery.
        negotiated_delivery = join.receipt_with_stage_advance(
            base_receipt, stage_advance_record=returned
        )
        self.assertEqual(negotiated_delivery["schema_version"], 3)
        self.assertEqual(
            negotiated_delivery["stage_advance"]["outcome"], "advanced"
        )
        # T1 correction: `receipt_with_stage_advance` no longer accepts an
        # independent `negotiated` bool that could disagree with the
        # `enable_stage_advance` gate that produced `returned` in the first
        # place -- `--enable-stage-advance` unset means `attempt_stage_advance`
        # itself returns `None` (asserted elsewhere), so the only way to reach
        # a v2 delivery here is `stage_advance_record=None`. An
        # `outcome == "advanced"` record can therefore never coexist with a
        # v2 delivery: the incoherent combination round 4 fixed is now
        # unrepresentable, not merely untriggered.
        import inspect  # noqa: PLC0415

        self.assertNotIn(
            "negotiated",
            inspect.signature(join.receipt_with_stage_advance).parameters,
        )
        recordless_delivery = join.receipt_with_stage_advance(
            base_receipt, stage_advance_record=None
        )
        self.assertEqual(recordless_delivery["schema_version"], 2)
        self.assertNotIn("stage_advance", recordless_delivery)
        self.assertIs(recordless_delivery, base_receipt)


class ContinuationTripartiteBudgetTest(unittest.TestCase):
    """SD-116 §13.34.4-(2): gross ceiling / stall counter / terminal reserve."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.lease = self.base / "supervisor-state" / f"{PARENT}.lease"

    def _budget_rows(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_budget_record as BR
        return BR.read_rows(self.jobs.parent, PARENT)

    def _delegate(self):
        return ClaudeSessionSupervisorTest

    def test_workload_budget_keeps_report_and_final_handoff_after_fourteen_turns(self):
        budget = supervisor_budget_module().ContinuationBudget(32, "workload-regression")
        ledger = supervisor_budget_module().ContinuationLedger(budget)
        state_root = self.base / "workload"
        for ordinal in range(14):
            verdict, _ = supervisor._admit_continuation(
                ledger, state_root, parent_attempt_id=PARENT, route_id="rt-workload",
                route_hash="sha256:" + "e" * 64, ordinal=ordinal,
                purpose="ordinary", stalled=False)
            self.assertTrue(verdict.admitted, (ordinal, verdict))
        report_dispatch, _ = supervisor._admit_continuation(
            ledger, state_root, parent_attempt_id=PARENT, route_id="rt-workload",
            route_hash="sha256:" + "e" * 64, ordinal=14,
            purpose="ordinary", stalled=False)
        self.assertTrue(report_dispatch.admitted)
        notice = supervisor._seal_terminal_handoff_or_raise(
            ledger, state_root, args=_terminal_handoff_args(), ordinal=15,
            failure_reason="terminal-handoff-incomplete", terminal_handoff_issued=[False])
        self.assertIn("final continuation", notice)
        self.assertEqual(ledger.reserved_remaining, 0)

    def test_delivery_consumes_one_continuation_and_no_model_bookkeeping_stall(self):
        case = ClaudeSessionSupervisorTest()
        case.setUp()
        try:
            case.jobs.write_text(owner_row(case.lease) + case._blocked_child_row(), encoding="utf-8")
            result = subprocess.run(
                case.command_with_join(case._non_closing_join()),
                input="initial assignment",
                text=True,
                capture_output=True,
                env=case.child_env(FAKE_TRACE=str(case.trace)),
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            registry = case.jobs.read_text(encoding="utf-8")
            self.assertNotIn("note=owner-redelivery-abandoned", registry)
            sys.path.insert(0, str(ROOT / "utilities"))
            import dispatch_budget_record as BR
            rows = BR.read_rows(case.jobs.parent, PARENT)
            reservations = [row for row in rows if row.get("record_kind") == "reservation"]
            self.assertTrue(reservations)
            stall_charged = [row for row in reservations if row["class"] == "stall"]
            self.assertEqual(stall_charged, [], rows)
            self.assertEqual(len(reservations), 1, rows)
        finally:
            case.tearDown() if hasattr(case, "tearDown") else None

    def test_runtime_wait_without_started_child_spends_stall_only(self):
        case = ClaudeSessionSupervisorTest()
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
        module = supervisor
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
    """SD-116 (b)/D47-8: the primary receipt-bytes-unchanged assertion."""

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
        without_notice = supervisor.completion_prompt(dict(self.RECEIPT))
        with_notice = supervisor.completion_prompt(
            dict(self.RECEIPT), notice="[continuation-budget-warning] remaining=2 (warning threshold=3)."
        )
        self.assertEqual(self._compact(without_notice), self._compact(with_notice))
        self.assertNotEqual(without_notice, with_notice)
        self.assertIn("[continuation-budget-warning]", with_notice)
        self.assertNotIn("[continuation-budget-warning]", without_notice)


class BudgetWarningDeliveryTest(unittest.TestCase):
    """SD-116 (b) D47-5: the warning notice reaches the owner's next-turn
    prompt exactly once, at the turn that crosses the threshold, and not
    before."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_root = Path(self.temp.name)

    def test_admit_returns_notice_only_on_the_crossing_turn(self):
        # ordinary=5, reserved=1(default) -> gross_remaining after each
        # ordinary admit is 4, 3, 2, 1. threshold=3 -> gross_remaining first
        # reads <= 3 right after the *second* admit, and only there.
        budget = supervisor_budget_module().ContinuationBudget(limit=5, source="test")
        ledger = supervisor_budget_module().ContinuationLedger(budget)
        notices = []
        for ordinal in range(4):
            verdict, notice = supervisor._admit_continuation(
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

    def test_no_notice_before_threshold_is_crossed(self):
        budget = supervisor_budget_module().ContinuationBudget(limit=10, source="test")
        ledger = supervisor_budget_module().ContinuationLedger(budget)
        verdict, notice = supervisor._admit_continuation(
            ledger, self.state_root, parent_attempt_id="att-p",
            route_id="rt-x", route_hash="sha256:" + "b" * 64,
            ordinal=0, purpose="ordinary", stalled=False,
            warning_threshold=3,
        )
        self.assertTrue(verdict.admitted)
        self.assertEqual("", notice)


class ReservationForcedFailureTest(unittest.TestCase):
    """D47-3: a forced reservation-write failure refuses admission and spends
    nothing -- the atomic-write outcome, not an in-process counter, is what
    the ledger's `reservation_ok` decision rests on."""

    def test_forced_reservation_write_failure_refuses_and_spends_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            budget = supervisor_budget_module().ContinuationBudget(limit=5, source="test")
            ledger = supervisor_budget_module().ContinuationLedger(budget)
            with mock.patch.object(supervisor.budget_record, "_append", return_value=False):
                verdict, notice = supervisor._admit_continuation(
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
    """impl-review round 1 finding 1: `_seal_terminal_handoff_or_raise()`
    reuses the just-refused ordinary admit's `ordinal`. Before the fix,
    `dispatch_budget_record.reserve()`'s CAS key was `(parent_attempt_id,
    ordinal)` alone, so the terminal-handoff reservation collided with the
    already-appended `purpose="ordinary"` reservation at that same ordinal
    and was refused as `reservation-lost` -- the SD-116 (c) 'one last
    cleanup turn' was never actually issued and the owner died with
    `continuation-limit-exceeded` on the very next admit instead of getting
    the promised cleanup turn. This drives the real `_admit_continuation`/
    `_seal_terminal_handoff_or_raise` functions against a real tmpdir
    reservation ledger -- no mock stands in for the CAS check being
    regression-tested."""

    def test_exactly_one_cleanup_turn_then_second_cleanup_is_refused(self):
        with tempfile.TemporaryDirectory() as home:
            state_root = Path(home)
            budget = supervisor_budget_module().ContinuationBudget(limit=3, source="test")
            ledger = supervisor_budget_module().ContinuationLedger(budget)
            terminal_handoff_issued = [False]
            common = dict(
                parent_attempt_id="att-p", route_id="rt-x",
                route_hash="sha256:" + "d" * 64,
            )
            verdict, _ = supervisor._admit_continuation(
                ledger, state_root, ordinal=0, purpose="ordinary", stalled=False, **common,
            )
            self.assertTrue(verdict.admitted)
            verdict, _ = supervisor._admit_continuation(
                ledger, state_root, ordinal=1, purpose="ordinary", stalled=False, **common,
            )
            self.assertTrue(verdict.admitted)
            self.assertEqual(1, ledger.gross_remaining)
            self.assertEqual(1, ledger.reserved_remaining)

            # Refused at the gross==reserved boundary. This still appends a
            # `purpose="ordinary"` reservation row at ordinal=2 even though
            # the ledger refuses the admit -- that append is the collision
            # source the fix must tolerate.
            verdict, _ = supervisor._admit_continuation(
                ledger, state_root, ordinal=2, purpose="ordinary", stalled=False, **common,
            )
            self.assertFalse(verdict.admitted)

            # (a) exactly one budget-exhausted cleanup prompt is issued, at
            # the SAME ordinal the just-refused ordinary admit used.
            prompt = supervisor._seal_terminal_handoff_or_raise(
                ledger, state_root, args=_terminal_handoff_args(), ordinal=2,
                failure_reason="continuation-limit-exceeded",
                terminal_handoff_issued=terminal_handoff_issued,
            )
            self.assertIn("final continuation turn", prompt)
            self.assertTrue(terminal_handoff_issued[0])

            # (b) reserved_remaining becomes 0.
            self.assertEqual(0, ledger.reserved_remaining)

            # (c) a second cleanup is refused and the supervisor terminates.
            with self.assertRaises(supervisor.SupervisorError) as ctx:
                supervisor._seal_terminal_handoff_or_raise(
                    ledger, state_root, args=_terminal_handoff_args(), ordinal=3,
                    failure_reason="continuation-limit-exceeded",
                    terminal_handoff_issued=terminal_handoff_issued,
                )
            self.assertEqual("continuation-limit-exceeded", str(ctx.exception))

            sys.path.insert(0, str(ROOT / "utilities"))
            import dispatch_budget_record as BR
            rows = BR.read_rows(state_root, "att-p")
            reservations = [row for row in rows if row.get("record_kind") == "reservation"]
            terminal_reservations = [row for row in reservations if row["purpose"] == "terminal-handoff"]
            self.assertEqual(1, len(terminal_reservations))
            ordinary_at_ordinal_2 = [
                row for row in reservations if row["ordinal"] == 2 and row["purpose"] == "ordinary"
            ]
            self.assertEqual(1, len(ordinary_at_ordinal_2))


class TerminalHandoffProducerBindingDegradeTest(unittest.TestCase):
    """R1 (2026-09-27 owner decision, evidence E1-E8): a runtime-v1
    launcher's `prepare_route_artifact_env(start=True)` opens the route's
    cycle before the owner attempt row exists. A missing launch binding now
    degrades to a partial-report-only cleanup handoff."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.route_file = self.root / "route.json"
        self.jobs = self.root / "jobs.log"
        import route_identity
        self.route = {
            "route_id": "placeholder", "route_hash": "placeholder",
            "nodes": [{"id": "execute", "terminal": True}],
            "workflow_contract": {"terminal_nodes": ["execute"]},
            "artifact_root": str(self.root),
        }
        route_hash = route_identity.route_hash(self.route)
        self.route["route_hash"] = route_hash
        self.route["route_id"] = route_identity.route_id_from_hash(route_hash)
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")
        owner = "att-launcher-owner"
        self.jobs.write_text(
            f"2026-09-27T00:00:00Z\topen\t{self.root}\t{self.root}\towner\t"
            f"attempt_id={owner},worker_type=owner,dispatch_depth=1,registered_worker=1,harness=claude,"
            f"owner_route_id={self.route['route_id']},owner_route_hash={self.route['route_hash']},"
            f"owner_route_file={self.route_file}\n",
            encoding="utf-8",
        )
        self.args = SimpleNamespace(
            route_file=str(self.route_file), route_id=self.route["route_id"],
            route_hash=self.route["route_hash"], parent_attempt_id=owner,
            jobs=str(self.jobs), turn_timeout=600, continuation_warning_threshold=3,
        )
        self.ledger = SimpleNamespace(gross_remaining=0, stall_remaining=0, reserved_remaining=1)
        row = join.ChildRow(
            order=0, status="done", slug="execute", attempt_id="att-execute", raw="",
            metadata={
                "attempt_id": "att-execute", "parent_attempt_id": owner,
                "route_id": self.route["route_id"], "route_node": "execute",
                "attempt_schema_version": "2", "dispatch_depth": "2", "registered_worker": "1",
                "transport": "headless", "execution_surface": "registered-headless",
                "fallback_hop": "same-harness-headless", "harness": "codex",
                "note": "completed-marker", "failure_class": "pass",
                "launch_outcome": "reaped-before-publish",
            },
        )
        self.rows = [row]
        self.claim = supervisor.budget_record.claim_terminal_handoff(
            self.root, owner_attempt_id=owner, route_hash=self.route["route_hash"],
            child_attempt_ids=["att-execute"])
        self.cycle_id = "cyc_" + "9" * 32
        cycle_dir = self.root / ".runtime/artifact-producer/v1/cycles"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        (cycle_dir / f"{self.cycle_id}.json").write_text(json.dumps({
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "cycle_id": self.cycle_id, "state": "open",
            "campaign_id": "camp_" + "9" * 32, "producer_id": "prod_" + "9" * 32,
        }), encoding="utf-8")
        import dispatch_terminal_commit as terminal
        self.terminal = terminal
        self.identity_patch = mock.patch.object(
            terminal.artifact_lifecycle, "read_root_identity",
            return_value=SimpleNamespace(repository_id="repo_x", artifact_root_id="root_y"),
        )
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        self.lifecycle_patch = mock.patch.object(terminal, "producer_lifecycle_applies", return_value=True)
        self.lifecycle_patch.start()
        self.addCleanup(self.lifecycle_patch.stop)
        import owner_route_binding
        owner_binding = owner_route_binding.OwnerRouteBinding(
            str(self.route_file), self.route["route_id"], self.route["route_hash"])
        self.validate_patch = mock.patch.object(terminal, "validate_owner_route", return_value=owner_binding)
        self.validate_patch.start()
        self.addCleanup(self.validate_patch.stop)

    def test_missing_binding_degrades_to_partial_report_scope(self):
        binding_path = self.terminal.producer_binding_path(
            self.root, self.route["route_id"], self.args.parent_attempt_id)
        self.assertFalse(binding_path.exists())
        prompt, intent = supervisor.prepare_cleanup_handoff(self.args, self.ledger, self.claim, self.rows)
        self.assertIsInstance(prompt, str)
        self.assertFalse(binding_path.exists())
        scope = intent["cleanup_scope"]
        self.assertIsNone(scope["cycle_id"])
        self.assertEqual(scope["allowed_operations"], ["partial-report", "read", "verify"])
        self.assertEqual(scope["producer_binding_refusal"], "producer-binding-required")
        self.assertNotIn("cleanup-recover", prompt)
        self.assertIn("settlement cannot be proved", prompt)
        self.assertIn("partial/BLOCKED report", prompt)

        loaded = SimpleNamespace(binding={"cycle_id": self.cycle_id})
        normal_args = SimpleNamespace(**vars(self.args))
        normal_args.parent_attempt_id = "att-normal-binding"
        normal_claim = supervisor.budget_record.claim_terminal_handoff(
            self.root, owner_attempt_id=normal_args.parent_attempt_id,
            route_hash=self.route["route_hash"], child_attempt_ids=["att-execute"])
        with mock.patch.object(self.terminal, "load_producer_binding", return_value=loaded):
            normal_prompt, normal_intent = supervisor.prepare_cleanup_handoff(
                normal_args, self.ledger, normal_claim, self.rows)
        self.assertIn("cleanup-recover", normal_prompt)
        self.assertEqual(normal_intent["cleanup_scope"]["cycle_id"], self.cycle_id)


class EscapedTerminalCommitRegistryRegressionTest(ClaudeSessionSupervisorTest):
    def test_escaped_error_closes_real_owner_row_with_sanitized_evidence(self):
        import io
        import dispatch_terminal_commit as terminal
        route = self.base / "error-route.json"
        value = dict(cwd=str(self.base), artifact_root=str(self.artifact_root),
            nodes=[dict(id="report", terminal=True)],
            workflow_contract=dict(terminal_nodes=["report"]),
            runtime_support=dict(terminal_commit=True))
        value["route_hash"] = supervisor.canonical_route_hash(value)
        value["route_id"] = supervisor.route_id_from_hash(value["route_hash"])
        route.write_text(json.dumps(value), encoding="utf-8")
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        events = []
        args = self.command()[2:] + ["--route-file", str(route), "--route-id", value["route_id"],
            "--route-hash", value["route_hash"], "--enable-terminal-commit", "--max-continuations", "1"]
        error = terminal.TerminalCommitError("producer-binding-required", "one\ttwo,three\nfour")
        with mock.patch.dict(os.environ, self.child_env(FAKE_TRACE=str(self.trace))), \
             mock.patch.object(supervisor.sys, "stdin", io.StringIO("initial assignment")), \
             mock.patch.object(supervisor, "emit", events.append), \
             mock.patch.object(supervisor, "terminal_commit_adapter", side_effect=error):
            self.assertEqual(supervisor.main(args), 70, events)
        fields = self.jobs.read_text(encoding="utf-8").splitlines()[0].split("\t")
        self.assertEqual(len(fields), 6)
        self.assertEqual(fields[1], "done")
        meta = __import__("dispatch_contract").parse_registry_metadata(fields[5])
        self.assertEqual(meta["note"], "dead-terminal-commit")
        self.assertEqual(meta["reconcile_reason"], "producer-binding-required")
        self.assertEqual(meta["terminal_commit_detail"], "one two three four")
        self.assertNotIn("detail", meta)
        self.assertNotIn("terminal_slot", meta)
        self.assertNotIn("route_file", meta)
        self.assertEqual(events[-1]["reason"], "producer-binding-required")

    def test_generic_exception_closes_row_when_commit_module_is_absent(self):
        import io
        route = self.base / "generic-route.json"
        value = dict(cwd=str(self.base), artifact_root=str(self.artifact_root),
            nodes=[dict(id="report", terminal=True)],
            workflow_contract=dict(terminal_nodes=["report"]),
            runtime_support=dict(terminal_commit=True))
        value["route_hash"] = supervisor.canonical_route_hash(value)
        value["route_id"] = supervisor.route_id_from_hash(value["route_hash"])
        route.write_text(json.dumps(value), encoding="utf-8")
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        events = []
        args = self.command()[2:] + ["--route-file", str(route), "--route-id", value["route_id"],
            "--route-hash", value["route_hash"], "--enable-terminal-commit", "--max-continuations", "1"]
        with mock.patch.dict(os.environ, self.child_env(FAKE_TRACE=str(self.trace))), \
             mock.patch.object(supervisor.sys, "stdin", io.StringIO("initial assignment")), \
             mock.patch.object(supervisor, "emit", events.append), \
             mock.patch.object(supervisor, "terminal_commit_adapter", side_effect=RuntimeError("generic")), \
             mock.patch.dict(supervisor.sys.modules, {"dispatch_terminal_commit": None}):
            self.assertEqual(supervisor.main(args), 70, events)
        row = self.jobs.read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("\tdone\t", row)
        self.assertIn("note=dead-runtime-exit", row)
        self.assertNotIn("terminal_commit_detail", row)


def supervisor_budget_module():
    sys.path.insert(0, str(ROOT / "utilities"))
    import dispatch_continuation_budget as BUDGET
    return BUDGET


class PermissionPosturePassthrough(unittest.TestCase):
    """core/OPERATIONS.md §5.10: the supervisor carries the wrapper-resolved
    permission posture onto the first and every resumed print turn."""

    def _args(self, **overrides):
        base = SimpleNamespace(claude_command=None, add_dir=[], model=None, effort=None,
                               disallowed_tool=[], permission_mode=None, allowed_tool=[])
        base.__dict__.update(overrides)
        return base

    def test_bypass_posture_is_pinned_on_first_and_resumed_turns(self):
        args = self._args(permission_mode="bypassPermissions")
        for resume in (False, True):
            command = supervisor.claude_command(args, "sid-1", resume)
            self.assertIn("--permission-mode", command)
            self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")
            self.assertNotIn("--allowedTools", command)
            self.assertIn("--resume" if resume else "--session-id", command)

    def test_allowlist_posture_passes_only_the_given_rules(self):
        rule = "Bash(python3 /h/utilities/capability-route.py *)"
        command = supervisor.claude_command(self._args(allowed_tool=[rule]), "sid-1", False)
        self.assertNotIn("--permission-mode", command)
        self.assertEqual(command[command.index("--allowedTools") + 1], rule)

    def test_absent_posture_leaves_the_command_unchanged(self):
        command = supervisor.claude_command(self._args(), "sid-1", False)
        self.assertNotIn("--permission-mode", command)
        self.assertNotIn("--allowedTools", command)

    def test_worker_catalog_masking_survives_first_and_resumed_owner_turns(self):
        with mock.patch.dict(os.environ, {"HEARTING_WORKER_HOME": "/worker"}):
            for resume in (False, True):
                command = supervisor.claude_command(self._args(permission_mode="auto"), "sid-1", resume)
                self.assertIn("--disable-slash-commands", command)
                self.assertIn("--strict-mcp-config", command)
                self.assertEqual(command[command.index("--mcp-config") + 1], '{"mcpServers":{}}')
                self.assertIn("--settings", command)  # existing parent parking guard
                self.assertIn("--permission-mode", command)


class TerminalReconcileRefusalRecordTest(unittest.TestCase):
    """SD-115 axis 4 (짝), C47-14 (호출부): `reconcile()`'s `except Exception`
    already reported failure to its caller via `return False` -- it never
    swallowed the exception. What was missing was a durable trace: an
    `emit()`-only report is stdout/stderr-only and vanishes with the
    process (materialize_after_terminal_close's own swallowed failures at
    least got `_log_pending_delivery_refusal`; this call site got nothing).
    This asserts the fix leaves both: the emitted event AND a durable
    refusal record."""

    def test_supervisor_terminal_reconcile_failure_leaves_a_durable_refusal(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            args = SimpleNamespace(jobs=str(jobs), parent_attempt_id=PARENT)
            emitted = []
            with mock.patch.object(
                supervisor, "reconcile_supervisor_terminal",
                side_effect=RuntimeError("simulated-terminal-reconcile-crash"),
            ), mock.patch.object(supervisor, "emit", emitted.append):
                result = supervisor.reconcile(args, terminal=None)

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


class PostJoinClaimTest(unittest.TestCase):
    def test_reserve_boundary_with_open_child_claims_before_join_without_charge_or_prompt(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_budget_record as budget_record
        with tempfile.TemporaryDirectory() as home:
            claim = budget_record.claim_terminal_handoff(home, owner_attempt_id="o", route_hash="h", child_attempt_ids=["c"], continuation_ordinal=1)
            self.assertEqual(claim["status"], "prepared")



class SubmitSettlementTest(unittest.TestCase):
    """D6/§13.53.8: submit_turn's stdin and process transports must each
    resolve to exactly one of the three TurnSubmission states, and a failure
    that happens before any byte is attempted must be the only path to the
    confirmed `not-submitted` state."""

    def test_stdin_closed_before_send_is_confirmed_not_submitted(self):
        transport = SimpleNamespace(stdin=None)
        result = supervisor.submit_turn(transport, "hello")
        self.assertEqual(result.status, "not-submitted")

    def test_stdin_marked_closed_before_send_is_confirmed_not_submitted(self):
        transport = SimpleNamespace(stdin=SimpleNamespace(closed=True))
        result = supervisor.submit_turn(transport, "hello")
        self.assertEqual(result.status, "not-submitted")

    def test_stdin_write_failure_is_submission_unknown_not_not_submitted(self):
        class FailingStdin:
            closed = False

            def write(self, payload):
                raise OSError("write failed mid-stream")

            def flush(self):
                pass

        transport = SimpleNamespace(stdin=FailingStdin())
        result = supervisor.submit_turn(transport, "hello")
        self.assertEqual(result.status, "submission-unknown")

    def test_stdin_flush_failure_after_successful_write_is_submission_unknown(self):
        class PartialWriteStdin:
            closed = False

            def write(self, payload):
                return None

            def flush(self):
                raise BrokenPipeError("peer closed after partial consumption")

        transport = SimpleNamespace(stdin=PartialWriteStdin())
        result = supervisor.submit_turn(transport, "hello")
        self.assertEqual(result.status, "submission-unknown")

    def test_stdin_write_success_reaches_submitted(self):
        class OkStdin:
            closed = False
            written = b""

            def write(self, payload):
                self.written += payload

            def flush(self):
                pass

        transport = SimpleNamespace(stdin=OkStdin(), returncode=0,
                                     read_result=lambda timeout: {"type": "result"})
        result = supervisor.submit_turn(transport, "hello")
        self.assertEqual(result.status, "submitted")
        self.assertEqual(result.result, {"type": "result"})

    def test_process_transport_oserror_reaches_seam_as_submission_unknown_not_supervisor_error(self):
        # D6/§13.53.8(3): OSError raised inside a callable transport (the
        # process transport's own subprocess.run call) must surface through
        # submit_turn's seam as submission-unknown, not bypass the seam by
        # propagating a raw exception.
        def process_transport(prompt, *, timeout):
            raise OSError("no such file or directory")

        result = supervisor.submit_turn(process_transport, "hello")
        self.assertEqual(result.status, "submission-unknown")

    def test_process_transport_timeout_expired_reaches_seam_as_submission_unknown(self):
        def process_transport(prompt, *, timeout):
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=timeout)

        result = supervisor.submit_turn(process_transport, "hello", timeout=1)
        self.assertEqual(result.status, "submission-unknown")
        self.assertEqual(result.rc, 124)

    def test_process_creation_failure_is_confirmed_not_submitted(self):
        # Regression: process_transport used to catch (OSError,
        # subprocess.TimeoutExpired) and re-raise SupervisorError
        # ("claude-turn-process-failed"), bypassing submit_turn's seam
        # entirely. run_turn still raises SupervisorError for a non-submitted
        # outcome (legacy caller contract), but the reason must now come
        # from the seam's own status vocabulary, not the old wrapper string.
        args = SimpleNamespace(turn_timeout=1, worktree=".", state_file=None,
                                claude_command=None, add_dir=[], model=None,
                                effort=None, disallowed_tool=[],
                                permission_mode=None, allowed_tool=[])
        with mock.patch.object(supervisor.subprocess, "Popen",
                                side_effect=OSError("boom")):
            with self.assertRaises(supervisor.SupervisorError) as ctx:
                supervisor.run_turn(args, "sess", "hi", resume=False)
        self.assertIn("not-submitted", str(ctx.exception))
        self.assertNotIn("claude-turn-process-failed", str(ctx.exception))



class DurableHandoffTransportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("")
        self.received = self.root / "received"
        self.program = self.root / "transport.py"
        self.program.write_text(
            "import pathlib,sys,json,time\n"
            f"p=pathlib.Path({str(self.received)!r})\n"
            "with p.open('a') as f: f.write(sys.stdin.read()+'\\n')\n"
            "print(json.dumps({'type':'result','subtype':'success','result':'partial'}))\n")
        self.args = SimpleNamespace(jobs=str(self.jobs), parent_attempt_id="owner",
            worktree=str(self.root), state_file=None, turn_timeout=1,
            claude_command=shlex.join([sys.executable, str(self.program)]),
            add_dir=[], model=None, effort=None, disallowed_tool=[], permission_mode=None, allowed_tool=[])
        self.claim = supervisor.budget_record.claim_terminal_handoff(self.root,
            owner_attempt_id="owner", route_hash="hash", child_attempt_ids=["child"])
        self.intent = supervisor.budget_record.convert_claim_to_prompt_intent(self.root, self.claim,
            prompt="cleanup", cleanup_scope={"route_id":"route", "owner_attempt_id":"owner"},
            remaining={"gross_remaining":1, "stall_remaining":2, "reserved_remaining":1})

    def send(self, **kwargs):
        with mock.patch.object(supervisor, "emit"):
            return supervisor.run_turn(self.args, "session", "cleanup", resume=True,
                                       handoff_intent=self.intent, **kwargs)

    def charge(self):
        return supervisor.budget_record.read_effective_charge(self.root, self.intent)

    def test_process_submission_charges_once_and_restart_never_resends(self):
        self.assertEqual(self.send()[1], 0)
        self.assertEqual(self.received.read_text(), "cleanup\n")
        self.assertEqual(self.charge(), 1)
        with self.assertRaisesRegex(supervisor.SupervisorError, "recovery-unavailable"):
            self.send()
        self.assertEqual(self.received.read_text(), "cleanup\n")
        self.assertEqual(len(supervisor.budget_record.read_rows(self.root, "owner")), 1)

    def test_pre_spawn_failure_has_zero_effective_charge_and_no_retry(self):
        self.args.claude_command = str(self.root / "absent")
        with self.assertRaisesRegex(supervisor.SupervisorError, "not-submitted"):
            self.send()
        self.assertEqual(self.charge(), 0)
        with self.assertRaisesRegex(supervisor.SupervisorError, "recovery-unavailable"):
            self.send()
        self.assertFalse(self.received.exists())

    def test_process_timeout_and_restart_preserve_unknown(self):
        self.program.write_text("import time\ntime.sleep(10)\n")
        self.args.turn_timeout = 0.05
        with self.assertRaisesRegex(supervisor.SupervisorError, "submission-unknown"):
            self.send()
        self.assertIsNone(self.charge())
        with self.assertRaisesRegex(supervisor.SupervisorError, "recovery-unavailable"):
            self.send()
        self.assertIsNone(self.charge())

    def test_stream_partial_write_and_restart_preserve_unknown(self):
        stream = SimpleNamespace(submit=mock.Mock(side_effect=BrokenPipeError("partial")))
        with self.assertRaisesRegex(supervisor.SupervisorError, "submission-unknown"):
            self.send(stream_session=stream)
        self.assertIsNone(self.charge())
        with self.assertRaisesRegex(supervisor.SupervisorError, "recovery-unavailable"):
            self.send(stream_session=stream)
        self.assertEqual(stream.submit.call_count, 1)

    def test_handoff_submission_never_asks_for_the_no_progress_window(self):
        stream = SimpleNamespace(submit=mock.Mock(return_value=({"type": "result"}, 0)), idle_reset=True)
        self.send(stream_session=stream)
        self.assertIs(stream.idle_reset, False)   # fixed deadline: the lease outlives no live handoff

    def test_prompt_drift_never_calls_transport(self):
        stream = SimpleNamespace(submit=mock.Mock())
        with self.assertRaisesRegex(supervisor.SupervisorError, "prompt-conflict"):
            supervisor.run_turn(self.args, "session", "other", resume=True,
                stream_session=stream, handoff_intent=self.intent)
        stream.submit.assert_not_called()
        self.assertIsNone(self.charge())



class ActiveTurnDeadlineTest(unittest.TestCase):
    """`--turn-timeout` is a no-progress window for the live stream turn: any
    output from the live process restarts it, silence for the whole window
    still fails with the existing reason, and a sealed handoff keeps its fixed
    deadline."""

    WINDOW = 0.6

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = SimpleNamespace(worktree=str(self.root), state_file=None, turn_timeout=self.WINDOW)

    def session(self, body):
        script = self.root / "live.py"
        script.write_text(
            "import json,sys,time\n"
            "turn=json.loads(sys.stdin.readline())['uuid']\n"
            "def emit(**v):\n"
            "    print(json.dumps(v),flush=True)\n"
            + textwrap.dedent(body))
        patch = mock.patch.object(supervisor, "claude_command",
                                  return_value=[sys.executable, str(script)])
        patch.start()
        self.addCleanup(patch.stop)
        session = supervisor.ClaudeStreamSession(self.args, "sess")
        self.addCleanup(session.process.stdout.close)
        self.addCleanup(session.close)
        return session

    CHATTY = """
        for _ in range(6):
            time.sleep(0.3)
            emit(type='assistant', n=1)
        emit(type='result', subtype='success', result='ok', user_message_uuids=[turn])
        sys.stdin.read()
    """

    def test_stream_live_turn_with_output_survives_past_the_window(self):
        session = self.session(self.CHATTY)
        pid = session.process.pid
        started = time.monotonic()
        result, rc = supervisor.run_turn(
            self.args, "sess", "hi", resume=False, stream_session=session)
        self.assertGreater(time.monotonic() - started, self.WINDOW)   # the turn outlived the window
        self.assertEqual(result["result"], "ok")
        self.assertIsNone(session.process.poll())                     # same live process, not killed
        self.assertEqual(session.process.pid, pid)

    def test_stream_silence_for_the_whole_window_still_fails_with_the_existing_reason(self):
        session = self.session("sys.stdin.read()\n")
        started = time.monotonic()
        with self.assertRaisesRegex(supervisor.SupervisorError, "claude-turn-process-failed"):
            supervisor.run_turn(self.args, "sess", "hi", resume=False, stream_session=session)
        self.assertLess(time.monotonic() - started, 5)

    def test_stream_silence_after_early_output_fails_one_window_after_the_last_chunk(self):
        session = self.session("emit(type='assistant', n=1)\nsys.stdin.read()\n")
        session.idle_reset = True
        started = time.monotonic()
        with self.assertRaisesRegex(supervisor.SupervisorError, "claude-turn-process-failed"):
            session.run_turn("hi", 0.4)
        self.assertLess(time.monotonic() - started, 5)

    def test_real_exit_after_output_still_fails_as_result_missing(self):
        session = self.session("emit(type='assistant', n=1)\n")
        session.idle_reset = True
        with self.assertRaisesRegex(supervisor.SupervisorError, "claude-result-missing"):
            session.run_turn("hi", 5)

    def test_a_direct_bounded_turn_keeps_its_fixed_deadline_even_with_output(self):
        session = self.session(self.CHATTY)
        with self.assertRaisesRegex(supervisor.SupervisorError, "claude-turn-process-failed"):
            session.run_turn("hi", self.WINDOW)   # idle_reset defaults to off

    def test_only_a_general_owner_turn_asks_for_the_no_progress_window(self):
        stream = SimpleNamespace(submit=mock.Mock(return_value=({"type": "result"}, 0)))
        supervisor.run_turn(self.args, "sess", "hi", resume=False, stream_session=stream)
        self.assertIs(stream.idle_reset, True)


class TerminalCommitActivationTests(unittest.TestCase):
    """§13.53.2: both halves of the checked support must hold.

    The route seals the runtime-capability verdict; the Claude adapter turns
    that into `--enable-terminal-commit`; the supervisor requires *both* plus
    exact route identity. Any one of them alone must leave the legacy
    owner-driven close/finalize path in place."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.route_file = self.base / "route.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _seal(self, *, declared):
        route = {"capability": "autopilot-code", "nodes": [{"id": "report", "terminal": True}],
                 "runtime_support": {"terminal_commit": declared}}
        route["route_hash"] = supervisor.canonical_route_hash(route)
        route["route_id"] = supervisor.route_id_from_hash(route["route_hash"])
        self.route_file.write_text(json.dumps(route), encoding="utf-8")
        return route

    def _args(self, route, *, requested):
        return SimpleNamespace(route_file=str(self.route_file), route_id=route["route_id"],
                               route_hash=route["route_hash"], enable_terminal_commit=requested)

    def test_both_halves_required(self):
        for declared in (True, False):
            for requested in (True, False):
                route = self._seal(declared=declared)
                with self.subTest(declared=declared, requested=requested):
                    with mock.patch.dict(os.environ, {}, clear=False):
                        os.environ.pop("AGENT_DISPATCH_TERMINAL_COMMIT", None)
                        self.assertIs(
                            supervisor.terminal_commit_enabled(self._args(route, requested=requested)),
                            declared and requested)

    def test_a_route_that_does_not_declare_support_ignores_the_env_switch(self):
        """The env variable is a runtime opt-in, not an override: it can never
        open the gate on a route whose runtime lacks the contract."""
        route = self._seal(declared=False)
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_TERMINAL_COMMIT": "1"}):
            self.assertFalse(supervisor.terminal_commit_enabled(self._args(route, requested=False)))

    def test_route_identity_drift_closes_the_gate_on_a_declaring_route(self):
        route = self._seal(declared=True)
        drifted = self._args(route, requested=True)
        drifted.route_hash = "sha256:" + "0" * 64
        self.assertFalse(supervisor.terminal_commit_enabled(drifted))

    def test_adapter_passes_the_flag_only_when_the_route_declares_support(self):
        path = ROOT / "adapters" / "claude" / "bin" / "dispatch-headless.py"
        spec = importlib.util.spec_from_file_location("claude_dispatch_headless_probe", path)
        headless = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = headless
        spec.loader.exec_module(headless)
        for declared in (True, False):
            self._seal(declared=declared)
            with self.subTest(declared=declared):
                self.assertIs(
                    headless._route_declares_terminal_commit_support(str(self.route_file)), declared)
        self.assertFalse(headless._route_declares_terminal_commit_support(str(self.base / "absent.json")))



if __name__ == "__main__":
    unittest.main()
