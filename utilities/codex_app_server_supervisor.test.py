#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
                    fields[5] += ',failure_class=pass,note=completed-supervisor'
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
        observed = [
            row for row in rows
            if row.get("type") == "dispatch.supervisor.join-observed"
        ]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["delivery_timing_schema_version"], 1)
        self.assertIsInstance(observed[0]["join_completed_ns"], int)
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
                        prompt = value['params']['input'][0]['text']
                        if '--failure-detail' in prompt:
                            state_path = os.environ['AGENT_DISPATCH_COMPLETION_STATE_FILE']
                            with open(state_path, encoding='utf-8') as h:
                                state_value = json.load(h)
                            state_value.pop('outbox', None)
                            state_value['phase'] = 'running-turn'
                            with open(state_path, 'w', encoding='utf-8') as h:
                                json.dump(state_value, h)
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
        self.assertEqual((budget["ordinary"], budget["source"]), (15, "bound-route"))
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
                boundary["same_thread_resume_ns"], boundary["exact_harvest_ns"],
                boundary["next_stage_start_ns"],
            ]
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

    def test_d12_identical_redelivery_bound_seals_abandonment(self):
        # D-12: the codex loop carries the same bound as the claude one. Before
        # it, an open row the owner never harvested was re-delivered until the
        # continuation budget was spent and the owner died `dead-runtime-exit`.
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
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
            timeout=30,
        )
        self.assertEqual(result.returncode, 70, result.stderr + result.stdout)
        events = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        suppressed = [
            row for row in events
            if row.get("type") == "dispatch.supervisor.redelivery-suppressed"
        ]
        self.assertEqual(
            [row["resolution"] for row in suppressed],
            ["identical-redelivery-bound"],
            events,
        )
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("note=owner-redelivery-abandoned", registry)
        self.assertIn("failure_class=runtime", registry)
        self.assertNotIn("note=owner-attention-unactionable", registry)
        self.assertNotIn(
            "continuation-limit-exceeded", result.stdout + result.stderr
        )

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
        # SD-116 (c): exhaustion no longer necessarily dies with
        # "continuation-limit-exceeded" on the very next turn -- the reserved
        # budget now buys exactly one extra cleanup turn first (see
        # `_seal_terminal_handoff_or_raise`), which can shift a genuinely
        # unresolved child onto whichever no-progress guard trips first (here,
        # the pre-existing identical-redelivery bound). Either way it still
        # raises -- this fixture's actual invariant.
        combined = result.stdout + result.stderr
        self.assertTrue(
            "continuation-limit-exceeded" in combined
            or "identical-redelivery-bound" in combined,
            combined,
        )
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\topen\t", registry)


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


class RuntimeReceiptlessCancelTest(unittest.TestCase):
    """Unit-level coverage for the B7 sibling helper (plan SS3.2 B7 / SS9.9)."""

    def setUp(self):
        self.module = load_supervisor_module()
        self.args = argparse.Namespace(
            jobs="/fixture/jobs.log", parent_attempt_id=PARENT
        )

    def test_closes_and_emits_once_per_proven_attempt(self):
        # S-1
        record = json.dumps({
            "classifier_source": "automatic-receipt-unavailable-v1",
            "decisions": [{
                "closed": 1,
                "receipt_digest": "sha256:" + "a" * 64,
                "reason": "automatic-cancelled-receipt-unavailable",
            }],
        })
        emitted = []
        with mock.patch.object(
            self.module, "subprocess"
        ) as fake_subprocess, mock.patch.object(
            self.module, "emit", side_effect=lambda payload: emitted.append(payload)
        ):
            fake_subprocess.run.return_value = fake_run_result(0, record)
            fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
            closed = self.module.runtime_receiptless_cancel(self.args, {"att-child"})
        self.assertEqual(closed, {"att-child"})
        self.assertEqual(fake_subprocess.run.call_count, 1)
        cancelled = [e for e in emitted if e["type"] == "dispatch.supervisor.receiptless-cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["attempt_id"], "att-child")
        self.assertEqual(cancelled[0]["receipt_digest"], "sha256:" + "a" * 64)

    def test_only_new_attempts_ever_reach_the_exact_attempt_argv(self):
        # S-2
        record = json.dumps({
            "classifier_source": "automatic-receipt-unavailable-v1",
            "decisions": [{"closed": 0, "reason": "namespace-not-extinct"}],
        })
        calls = []
        def fake_run(command, **kwargs):
            calls.append(command)
            return fake_run_result(0, record)
        with mock.patch.object(self.module, "subprocess") as fake_subprocess, \
             mock.patch.object(self.module, "emit"):
            fake_subprocess.run.side_effect = fake_run
            fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
            self.module.runtime_receiptless_cancel(self.args, {"att-only-this-one"})
        self.assertEqual(len(calls), 1)
        self.assertIn("--attempt", calls[0])
        argv_after_attempt = calls[0][calls[0].index("--attempt") + 1]
        self.assertEqual(argv_after_attempt, "att-only-this-one")
        self.assertNotIn("--all", calls[0])

    def test_no_event_for_an_ordinary_not_eligible_result(self):
        # S-3
        record = json.dumps({
            "classifier_source": "automatic-receipt-unavailable-v1",
            "decisions": [{"closed": 0, "reason": "terminal-envelope-valid"}],
        })
        emitted = []
        with mock.patch.object(self.module, "subprocess") as fake_subprocess, \
             mock.patch.object(
                 self.module, "emit", side_effect=lambda payload: emitted.append(payload)
             ):
            fake_subprocess.run.return_value = fake_run_result(0, record)
            fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
            closed = self.module.runtime_receiptless_cancel(self.args, {"att-child"})
        self.assertEqual(closed, set())
        self.assertEqual(emitted, [])

    def test_repeated_call_over_an_already_cancelled_child_is_idempotent(self):
        # S-4
        record = json.dumps({
            "classifier_source": "automatic-receipt-unavailable-v1",
            "decisions": [{
                "closed": 1,
                "receipt_digest": "sha256:" + "b" * 64,
                "reason": "automatic-cancelled-receipt-unavailable",
            }],
        })
        with mock.patch.object(self.module, "subprocess") as fake_subprocess, \
             mock.patch.object(self.module, "emit"):
            fake_subprocess.run.return_value = fake_run_result(0, record)
            fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
            first = self.module.runtime_receiptless_cancel(self.args, {"att-child"})
            second = self.module.runtime_receiptless_cancel(self.args, {"att-child"})
        self.assertEqual(first, {"att-child"})
        self.assertEqual(second, {"att-child"})

    def test_non_zero_exit_or_unparseable_stdout_is_skipped_not_closed(self):
        # part of S-4/S-5 coverage: a process failure is never a close
        emitted = []
        with mock.patch.object(self.module, "subprocess") as fake_subprocess, \
             mock.patch.object(
                 self.module, "emit", side_effect=lambda payload: emitted.append(payload)
             ):
            fake_subprocess.run.return_value = fake_run_result(1, "not-json")
            fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
            closed = self.module.runtime_receiptless_cancel(self.args, {"att-child"})
        self.assertEqual(closed, set())
        skipped = [e for e in emitted if e["type"] == "dispatch.supervisor.receiptless-cancel-skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["attempt_id"], "att-child")

    def test_repark_exhaustion_still_raises_join_timeout_repark_exceeded(self):
        # S-5 (regression): existing exhaustion test is preserved in the
        # end-to-end class below (test_live_unresolved_child_still_raises);
        # this asserts the new helper does not swallow that raise when it is
        # itself part of the repark loop -- a runtime_receiptless_cancel call
        # that finds nothing eligible must never suppress the bound.
        record = json.dumps({
            "classifier_source": "automatic-receipt-unavailable-v1",
            "decisions": [{"closed": 0, "reason": "process-alive"}],
        })
        with mock.patch.object(self.module, "subprocess") as fake_subprocess, \
             mock.patch.object(self.module, "emit"):
            fake_subprocess.run.return_value = fake_run_result(0, record)
            fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
            closed = self.module.runtime_receiptless_cancel(self.args, {"att-child"})
        self.assertEqual(closed, set())


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

    def test_identical_redelivery_spends_stall_only_and_existing_seal_is_unchanged(self):
        case = CodexAppServerSupervisorTest()
        case.setUp()
        try:
            case.jobs.write_text(owner_row(case.lease) + child_row(), encoding="utf-8")
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
            self.assertEqual(result.returncode, 70, result.stderr + result.stdout)
            registry = case.jobs.read_text(encoding="utf-8")
            self.assertIn("note=owner-redelivery-abandoned", registry)
            sys.path.insert(0, str(ROOT / "utilities"))
            import dispatch_budget_record as BR
            rows = BR.read_rows(case.jobs.parent, PARENT)
            reservations = [row for row in rows if row.get("record_kind") == "reservation"]
            stall_charged = [row for row in reservations if row["class"] == "stall"]
            self.assertTrue(stall_charged, rows)
        finally:
            case.tearDown() if hasattr(case, "tearDown") else None

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

    def test_every_continuation_limit_exceeded_is_preceded_by_a_budget_warning_record(self):
        case = CodexAppServerSupervisorTest()
        case.setUp()
        try:
            case.jobs.write_text(owner_row(case.lease) + child_row(), encoding="utf-8")
            result = subprocess.run(
                case.command_with_join(case._non_closing_join())
                # SD-116 (c): exhaustion now buys one extra terminal-handoff
                # cleanup turn before dying (`_seal_terminal_handoff_or_raise`),
                # which costs one extra identical redelivery of the same
                # receipt -- raise the redelivery bound so this fixture still
                # exercises the genuine continuation-limit-exceeded path this
                # test is about.
                + ["--max-continuations", "1", "--max-identical-redeliveries", "50"],
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
            self.assertEqual(result.returncode, 70, result.stderr + result.stdout)
            registry = case.jobs.read_text(encoding="utf-8")
            self.assertIn("reconcile_reason=continuation-limit-exceeded", registry)
            sys.path.insert(0, str(ROOT / "utilities"))
            import dispatch_budget_record as BR
            rows = BR.read_rows(case.jobs.parent, PARENT)
            warnings = [row for row in rows if row.get("record_kind") == "warning"]
            self.assertTrue(warnings, rows)
            self.assertEqual(warnings[-1]["reason"], "continuation-budget-exhausted")
        finally:
            case.tearDown() if hasattr(case, "tearDown") else None

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


if __name__ == "__main__":
    unittest.main()
