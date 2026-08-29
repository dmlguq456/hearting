#!/usr/bin/env python3

from __future__ import annotations

import json
import hashlib
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
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
                if '--failure-detail' in prompt:
                    with open(state_path, encoding='utf-8') as state_handle:
                        state_value = json.load(state_handle)
                    state_value.pop('outbox', None)
                    state_value['phase'] = 'running-turn'
                    with open(state_path, 'w', encoding='utf-8') as state_handle:
                        json.dump(state_value, state_handle)
                with open(state_path, encoding='utf-8') as state_handle:
                    delivered = json.load(state_handle)['delivered_attempt_ids']
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
                final_first = os.environ.get('FAKE_NO_CHILD') == '1'
                text = ('runtime_wait: registered-children' if dry_first and not delivered
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
                    if '--failure-detail' in prompt:
                        with open(state_path, encoding='utf-8') as state_handle:
                            state_value = json.load(state_handle)
                        state_value.pop('outbox', None)
                        state_value['phase'] = 'running-turn'
                        with open(state_path, 'w', encoding='utf-8') as state_handle:
                            json.dump(state_value, state_handle)
                    with open(state_path, encoding='utf-8') as state_handle:
                        delivered = json.load(state_handle)['delivered_attempt_ids']
                    with open(os.environ['FAKE_TRACE'], 'a', encoding='utf-8') as h:
                        h.write(json.dumps({'event':'turn-start','pid':os.getpid(),
                                            'time':time.monotonic(),'session':session,
                                            'prompt':prompt,'delivered':delivered}) + '\\n')
                    text = ('artifact: -\\nverdict: PASS\\nblocker: none'
                            if delivered else 'runtime_wait: registered-children')
                    print(json.dumps({'type':'result','subtype':'success','is_error':False,
                                      'result':text}), flush=True)
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
                        fields[5] += terminal
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
                if '--failure-detail' in prompt:
                    with open(state_path, encoding='utf-8') as h:
                        state_value = json.load(h)
                    state_value.pop('outbox', None)
                    state_value['phase'] = 'running-turn'
                    with open(state_path, 'w', encoding='utf-8') as h:
                        json.dump(state_value, h)
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
        self.assertEqual((budget["ordinary"], budget["source"]), (15, "bound-route"))
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
                boundary["same_thread_resume_ns"], boundary["exact_harvest_ns"],
                boundary["next_stage_start_ns"],
            ]
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

    def test_remediation_prompt_uses_shared_absolute_harvest_surface(self):
        prompt = supervisor.remediation_prompt({"att-child"}, jobs="/tmp/fixture-jobs.log")
        self.assertIn(
            str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh")
            + " harvest --jobs /tmp/fixture-jobs.log --attempt-id att-child --mark-done",
            prompt,
        )
        self.assertIn("shared, runtime-neutral registry harvest compatibility surface", prompt)
        self.assertIn("does not change either harness", prompt)

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
                        fields[5] += ',failure_class=pass,note=completed-supervisor'
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

    def test_join_timeout_repark_bound_trips_without_spurious_model_turn(self):
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        # More timeouts than --max-join-reparks allows: the supervisor must
        # fail closed via the repark bound, never by silently delivering a
        # timeout receipt to the model as if it were actionable.
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
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("join-timeout-repark-exceeded", result.stdout + result.stderr)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        turn_starts = [row for row in trace if row["event"] == "turn-start"]
        self.assertEqual(len(turn_starts), 1, trace)

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

    def test_d5_unsatisfiable_receipt_is_never_delivered_and_seals_protocol(self):
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        cmd = self.command_with_join(self._non_closing_join())
        cmd[1] = str(self._divergent_surface_supervisor())
        result = subprocess.run(
            cmd,
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=20,
        )
        self.assertEqual(result.returncode, 70, result.stderr + result.stdout)
        events = self._events(result.stdout)
        unsatisfiable = [
            row for row in events
            if row.get("type") == "dispatch.supervisor.receipt-unsatisfiable"
        ]
        self.assertEqual(len(unsatisfiable), 1, events)
        self.assertEqual(unsatisfiable[0]["reason"], "unrecognized-surface")
        # The row, not the event, is the evidence that a terminal was sealed.
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("note=owner-attention-unactionable", registry)
        self.assertIn("failure_class=protocol", registry)
        self.assertNotIn("note=owner-redelivery-abandoned", registry)
        self.assertNotIn("note=dead-runtime-exit", registry)
        # Not delivered at all: only the initial turn ever ran.
        self.assertEqual(len(self._turn_starts()), 1, self.trace.read_text())

    def test_d5a_actionable_but_unexecuted_receipt_is_redelivered_not_sealed(self):
        # The round-2 tripwire. An open row whose required action stays
        # `complete-open` because the owner has not run the command yet is a
        # legitimate state: a non-advancing row must never seal on its own.
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = subprocess.run(
            self.command_with_join(self._non_closing_join()),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=30,
        )
        turns = self._turn_starts()
        # initial + first delivery + at least one re-delivery after the first
        # unchanged pass.
        self.assertGreaterEqual(len(turns), 3, self.trace.read_text())
        events = self._events(result.stdout)
        suppressed = [
            row for row in events
            if row.get("type") == "dispatch.supervisor.redelivery-suppressed"
        ]
        # Exactly one suppression, and only after the bound -- never on the
        # first unchanged pass.
        self.assertEqual(
            [row["resolution"] for row in suppressed],
            ["identical-redelivery-bound"],
            events,
        )
        self.assertEqual(suppressed[0]["identical_redeliveries"], 3, events)

    def test_d5b_identical_bound_seals_abandonment_never_attention_or_budget(self):
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = subprocess.run(
            self.command_with_join(self._non_closing_join()),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=30,
        )
        self.assertEqual(result.returncode, 70, result.stderr + result.stdout)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("note=owner-redelivery-abandoned", registry)
        self.assertIn("failure_class=runtime", registry)
        self.assertNotIn("note=owner-attention-unactionable", registry)
        # DC-8b: the continuation budget must never be what stops an identical
        # prompt -- that is the defect this bound replaces.
        combined = result.stdout + result.stderr
        self.assertNotIn("continuation-limit-exceeded", combined)
        # The route node stays incomplete, so SD-106 same-node redispatch
        # remains available: no completion marker is published by the seal.
        self.assertEqual(
            list((self.base / "supervisor-state").glob("**/completion/**")), []
        )
        self.assertNotIn("completion_marker=", registry)

    def test_d6_in_place_row_advance_resumes_the_normal_loop(self):
        # An unchanged outbox buys in-place work first. When that work advances
        # the row, the loop resumes normally and the counter resets.
        counter = self.base / "advancing_join.count"
        counter.write_text("0", encoding="utf-8")
        script = self.base / "fake_join_advancing.py"
        script.write_text(
            textwrap.dedent(
                """\
                import json, sys
                counter_path = COUNTER_PATH
                jobs = sys.argv[sys.argv.index('--jobs') + 1]
                parent = sys.argv[sys.argv.index('--parent-attempt-id') + 1]
                attempts = [sys.argv[i + 1] for i, v in enumerate(sys.argv) if v == '--attempt-id']
                with open(counter_path, encoding='utf-8') as handle:
                    calls = int(handle.read()) + 1
                with open(counter_path, 'w', encoding='utf-8') as handle:
                    handle.write(str(calls))
                status, action = 'open', 'complete-open'
                if calls >= 2:
                    with open(jobs, encoding='utf-8') as handle:
                        lines = handle.read().splitlines()
                    out = []
                    for line in lines:
                        fields = line.split(TAB)
                        if len(fields) == 6 and 'attempt_id=att-child,' in fields[5] + ',':
                            fields[1] = 'done'
                            fields[5] += ',failure_class=pass,note=completed-supervisor'
                        out.append(TAB.join(fields))
                    with open(jobs, 'w', encoding='utf-8') as handle:
                        handle.write('\\n'.join(out) + '\\n')
                    status, action = 'done', 'advance-completed'
                print(json.dumps({'schema_version': 2, 'state': 'ready',
                    'parent_attempt_id': parent,
                    'children': [{'attempt_id': a, 'status': status, 'readiness': 'ready',
                                  'reason': 'registry-closed',
                                  'required_action': action} for a in attempts]}))
                """
            )
            .replace("COUNTER_PATH", repr(str(counter)))
            .replace("TAB", repr("\t")),
            encoding="utf-8",
        )
        self.jobs.write_text(owner_row(self.lease) + child_row(), encoding="utf-8")
        result = subprocess.run(
            self.command_with_join(script),
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=30,
        )
        events = self._events(result.stdout)
        suppressed = [
            row for row in events
            if row.get("type") == "dispatch.supervisor.redelivery-suppressed"
        ]
        self.assertEqual([row["resolution"] for row in suppressed], ["row-advanced"], events)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertNotIn("note=owner-redelivery-abandoned", registry)
        self.assertNotIn("note=owner-attention-unactionable", registry)

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

    def test_live_unresolved_child_still_raises(self):
        # Fix 4 must not become "never fail": a genuinely unresolved, live
        # (non-quiescent) child with no terminal evidence still raises.
        log = self.base / "att-live.claude.jsonl"
        route = self.base / "route.json"
        row = (
            f"2026-07-23T00:00:00Z\topen\t{self.base}\t{self.base}\tchild\t"
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,launch_started=1,"
            "fallback_hop=same-harness-headless,harness=claude,"
            f"attempt_id=att-live,parent_attempt_id={PARENT},"
            f"log_file={log},artifact_root={self.artifact_root},"
            f"route_file={route},route_node=frame\n"
        )
        self.jobs.write_text(owner_row(self.lease) + row, encoding="utf-8")
        join_script = self._non_closing_join()
        result = subprocess.run(
            self.command_with_join(join_script)
            + ["--max-continuations", "1"],
            input="initial assignment",
            text=True,
            capture_output=True,
            env=self.child_env(FAKE_TRACE=str(self.trace)),
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("continuation-limit-exceeded", result.stdout + result.stderr)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\topen\t", registry)


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

    def test_identical_redelivery_spends_stall_only_and_existing_seal_is_unchanged(self):
        case = ClaudeSessionSupervisorTest()
        case.setUp()
        try:
            case.jobs.write_text(owner_row(case.lease) + child_row(), encoding="utf-8")
            result = subprocess.run(
                case.command_with_join(case._non_closing_join()),
                input="initial assignment",
                text=True,
                capture_output=True,
                env=case.child_env(FAKE_TRACE=str(case.trace)),
                timeout=30,
            )
            self.assertEqual(result.returncode, 70, result.stderr + result.stdout)
            registry = case.jobs.read_text(encoding="utf-8")
            self.assertIn("note=owner-redelivery-abandoned", registry)
            sys.path.insert(0, str(ROOT / "utilities"))
            import dispatch_budget_record as BR
            rows = BR.read_rows(case.jobs.parent, PARENT)
            reservations = [row for row in rows if row.get("record_kind") == "reservation"]
            self.assertTrue(reservations)
            stall_charged = [row for row in reservations if row["class"] == "stall"]
            self.assertTrue(stall_charged, rows)
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

    def test_every_continuation_limit_exceeded_is_preceded_by_a_budget_warning_record(self):
        case = ClaudeSessionSupervisorTest()
        case.setUp()
        try:
            case.jobs.write_text(owner_row(case.lease) + child_row(), encoding="utf-8")
            result = subprocess.run(
                case.command_with_join(case._non_closing_join()) + ["--max-continuations", "1"],
                input="initial assignment",
                text=True,
                capture_output=True,
                env=case.child_env(FAKE_TRACE=str(case.trace)),
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


if __name__ == "__main__":
    unittest.main()
