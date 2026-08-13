#!/usr/bin/env python3

from __future__ import annotations

import json
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "utilities" / "claude-session-supervisor.py"
PARENT = "att-parent"


def seal_route(value: dict) -> dict:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    value["route_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    value["route_id"] = "rt-" + value["route_hash"].split(":", 1)[1][:16]
    return value
sys.path.insert(0, str(ROOT / "utilities"))
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
                with open(jobs, 'a', encoding='utf-8') as h:
                    for attempt in attempts:
                        h.write('2026-07-23T00:00:01Z\\tdone\\t/repo\\t/wt\\tchild\\t'
                                'attempt_schema_version=2,dispatch_depth=2,transport=headless,'
                                'execution_surface=registered-headless,registered_worker=1,'
                                f'attempt_id={attempt},parent_attempt_id={parent},'
                                'failure_class=pass,note=completed-supervisor\\n')
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

    def run_supervisor(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(),
            input="initial assignment",
            text=True,
            capture_output=True,
            env={**os.environ, "FAKE_TRACE": str(self.trace), **extra_env},
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
            env={**os.environ, "AGENT_ARTIFACT_ROOT": str(self.artifact_root)},
        )
        self.assertEqual(inspected.returncode, 0, inspected.stderr + inspected.stdout)
        self.assertIn("\tvalid\texact-claude-result\tPASS\tnone\tnone", inspected.stdout)

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
        self.assertEqual(sum(row.get("type") == "result" for row in rows), 1)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/repo\t/wt\towner\t", registry)
        self.assertIn("note=completed-supervisor", registry)

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
            }
        )
        self.assertEqual(prompt.count("preflight.sh harvest --attempt-id"), 2)
        self.assertEqual(
            prompt.count(
                str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh")
                + " harvest --attempt-id"
            ),
            2,
        )
        self.assertIn("shared, runtime-neutral registry harvest compatibility surface", prompt)
        self.assertIn("does not select or change the owner or child harness", prompt)
        self.assertIn("--attempt-id att-child-a --status open --mark-done", prompt)
        self.assertIn("--attempt-id att-child-b --status open --mark-done", prompt)
        self.assertNotIn("RAW_CLAUDE_SENTINEL", prompt)

    def test_remediation_prompt_uses_shared_absolute_harvest_surface(self):
        prompt = supervisor.remediation_prompt({"att-child"})
        self.assertIn(
            str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh")
            + " harvest --attempt-id att-child --mark-done",
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
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
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
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
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
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
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
                    with open(jobs, 'a', encoding='utf-8') as h:
                        for attempt in attempts:
                            h.write('2026-07-23T00:00:01Z\\tdone\\t/repo\\t/wt\\tchild\\t'
                                    'attempt_schema_version=2,dispatch_depth=2,transport=headless,'
                                    'execution_surface=registered-headless,registered_worker=1,'
                                    f'attempt_id={{attempt}},parent_attempt_id={{parent}},'
                                    'failure_class=pass,note=completed-supervisor\\n')
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
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
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
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
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
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
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
            env={**os.environ, "FAKE_TRACE": str(self.trace)},
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("continuation-limit-exceeded", result.stdout + result.stderr)
        registry = self.jobs.read_text(encoding="utf-8")
        self.assertIn("\topen\t", registry)


if __name__ == "__main__":
    unittest.main()
