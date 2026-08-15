#!/usr/bin/env python3

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "utilities" / "codex-app-server-supervisor.py"
PARENT = "att-parent"


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
                    elif method == 'thread/start':
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
                        if '--failure-detail' in prompt:
                            with open(state_path, encoding='utf-8') as h:
                                state_value = json.load(h)
                            state_value.pop('outbox', None)
                            state_value['phase'] = 'running-turn'
                            with open(state_path, 'w', encoding='utf-8') as h:
                                json.dump(state_value, h)
                        with open(state_path, encoding='utf-8') as h:
                            delivered = json.load(h)['delivered_attempt_ids']
                        record('turn-start', turn=turns, prompt=prompt, delivered=delivered,
                               lease_held=lease_held)
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
                with open(jobs, 'a', encoding='utf-8') as h:
                    for attempt in attempts:
                        h.write('2026-07-23T00:00:01Z\\tdone\\t/repo\\t/wt\\tchild\\t'
                                'attempt_schema_version=2,dispatch_depth=2,transport=headless,'
                                'execution_surface=registered-headless,registered_worker=1,'
                                f'attempt_id={attempt},parent_attempt_id={parent},'
                                'failure_class=pass,note=completed-supervisor\\n')
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
        terminal = next(i for i, row in enumerate(rows) if row.get("type") == "turn.completed")
        self.assertEqual(rows[terminal - 1]["item"]["type"], "agent_message")
        self.assertIn("verdict: PASS", rows[terminal - 1]["item"]["text"])
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
        self.jobs.write_text(owner_row(self.lease), encoding="utf-8")
        result = self.run_supervisor(
            FAKE_LAUNCH_STARTED_RACE="1", FAKE_JOBS=str(self.jobs)
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
                    elif method == 'thread/start':
                        send({'jsonrpc':'2.0','id':value['id'],'result':{'thread':{'id':'thread-long'}}})
                    elif method == 'turn/start':
                        turns += 1
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
        self.assertEqual((budget["limit"], budget["source"]), (15, "bound-route"))
        resumed = [row for row in rows if row.get("type") == "dispatch.supervisor.resumed"]
        self.assertEqual(len(resumed), 13)
        self.assertEqual(resumed[-1]["continuation_ordinal"], 13)
        self.assertEqual(sum(row.get("type") == "turn.completed" for row in rows), 1)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/repo\t/wt\towner\t", registry)
        self.assertIn("note=completed-supervisor", registry)

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
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
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

    def test_live_unresolved_child_still_raises(self):
        log = self.base / "att-live.codex.jsonl"
        route = self.base / "route.json"
        row = (
            f"2026-07-23T00:00:00Z\topen\t{self.base}\t{self.base}\tchild\t"
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,launch_started=1,"
            "fallback_hop=same-harness-headless,harness=codex,"
            f"attempt_id=att-live,parent_attempt_id={PARENT},"
            f"log_file={log},artifact_root={self.artifact_root},"
            f"route_file={route},route_node=frame\n"
        )
        self.jobs.write_text(owner_row(self.lease) + row, encoding="utf-8")
        result = subprocess.run(
            self.command_with_join(self._non_closing_join()) + ["--max-continuations", "1"],
            input="initial assignment",
            text=True,
            capture_output=True,
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("continuation-limit-exceeded", result.stdout + result.stderr)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\topen\t", registry)


if __name__ == "__main__":
    unittest.main()
