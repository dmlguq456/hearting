#!/usr/bin/env python3
"""A correction filed while a Claude owner's turn runs reaches that turn, or the next one.

The real supervisor drives a fake stream-json `claude` that runs the real
PreToolUse hook from the supervisor's `--settings` and models what the model
reads: a hook context is read only by a later main-thread reply, one over
Claude Code's 10,000-character hook limit only as its 2,000-character preview.
"""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "utilities" / "claude-session-supervisor.py"
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
import dispatch_owner_input as I  # noqa: E402
from fixture_processes import unique_attempt_id  # noqa: E402

PARENT = unique_attempt_id("att-in-turn-owner")

FAKE_CLAUDE = r'''
import json, os, select, subprocess, sys
args = sys.argv[1:]
trace, jobs, parent = os.environ['FAKE_TRACE'], os.environ['FAKE_JOBS'], os.environ['FAKE_PARENT']
plan = json.loads(os.environ.get('FAKE_PLAN', '{}'))
hook = json.loads(args[args.index('--settings') + 1])['hooks']['PreToolUse'][0]['hooks'][0]['command']
hook_events = '--include-hook-events' in args
sys.path.insert(0, os.path.join(os.environ['FAKE_ROOT'], 'utilities'))
import dispatch_owner_input as I

def log(**row):
    with open(trace, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(row) + '\n')

def out(value):
    sys.stdout.write(json.dumps(value) + '\n')
    sys.stdout.flush()

def ids_in(text, hook=True):
    if hook and len(text) > 10000:
        text = text[:2000]
    found = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and 'id' in item:
            found.append(item['id'])
    return found

buffer = b''
def next_message():
    global buffer
    while b'\n' not in buffer:
        chunk = os.read(0, 65536)
        if not chunk:
            return None
        buffer += chunk
    line, buffer = buffer.split(b'\n', 1)
    return json.loads(line)

def stdin_waiting():
    return b'\n' in buffer or bool(select.select([0], [], [], 0)[0])

attached = []
def reply(turn, message, content):
    ids = [item for context in attached for item in ids_in(context)]
    attached.clear()
    if ids:
        log(event='read', turn=turn, ids=ids, via='hook')
    out({'type': 'assistant', 'parent_tool_use_id': None, 'message': {'id': message, 'content': content}})

log(event='process-start', args=args)
turn = 0
while True:
    payload = next_message()
    if payload is None:
        break
    turn += 1
    prompt = payload['message']['content'][0]['text']
    log(event='turn-start', turn=turn, prompt=prompt)
    if prompt.strip().startswith('<owner-corrections>'):
        # #1890 R2 red 1: a UserPromptSubmit hook that blocks a correction sent on its own.
        log(event='blocked', turn=turn)
        out({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 0,
             'result': 'blocked', 'user_message_uuids': [payload.get('uuid')]})
        continue
    if '<owner-corrections>' in prompt:
        log(event='read', turn=turn, ids=ids_in(prompt[prompt.index('<owner-corrections>'):], hook=False), via='prompt')
    with open(os.environ['AGENT_DISPATCH_COMPLETION_STATE_FILE'], encoding='utf-8') as handle:
        delivered = json.load(handle)['delivered_attempt_ids']
    mid_turn = stopped = False
    for index, step in enumerate(plan.get(str(turn), []), 1):
        tool, sub = f'toolu-{turn}-{index}', bool(step.get('subagent'))
        thread = 'toolu-task' if sub else None
        use = [{'type': 'tool_use', 'id': tool, 'name': 'Bash', 'input': {}}]
        if sub:
            out({'type': 'assistant', 'parent_tool_use_id': thread, 'message': {'id': 'sub-' + tool, 'content': use}})
        else:
            reply(turn, f'msg-{turn}-{index}', use)
        request = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {},
                   'tool_use_id': tool, 'cwd': os.getcwd()}
        if sub:
            request['agent_id'] = 'agent-1'
        done = subprocess.run(hook, shell=True, input=json.dumps(request), capture_output=True, text=True,
                              timeout=60, env={**os.environ, 'AGENT_HOME': os.environ['FAKE_ROOT'],
                                               'AGENT_DISPATCH_JOBS': jobs, 'AGENT_DISPATCH_ATTEMPT_ID': parent,
                                               'AGENT_DISPATCH_COMPLETION_MODE': 'supervised'})
        outcome = step.get('hook', 'success')
        if hook_events and outcome != 'silent':
            out({'type': 'system', 'subtype': 'hook_response', 'hook_id': tool, 'hook_name': 'PreToolUse:Bash',
                 'hook_event': 'PreToolUse', 'output': done.stdout, 'stdout': done.stdout,
                 'stderr': done.stderr, 'exit_code': done.returncode, 'outcome': outcome})
        context = ''
        if done.returncode == 0 and done.stdout.strip() and outcome != 'cancelled':
            context = json.loads(done.stdout)['hookSpecificOutput'].get('additionalContext', '')
        if context:
            log(event='carried', turn=turn, tool=tool, ids=ids_in(context))
        for item in step.get('file', []):
            I.submit(jobs, parent, item.get('text') or 'd' * item['size'], item['id'])
            log(event='filed', turn=turn, ids=[item['id']])
        mid_turn = mid_turn or stdin_waiting()
        error = bool(step.get('error'))
        out({'type': 'user', 'parent_tool_use_id': thread, 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': tool, 'content': 'ok', 'is_error': error}]}})
        if context and not error and not sub:
            attached.append(context)
        if step.get('stop'):
            stopped = True
            break
    if mid_turn:
        log(event='mid-turn-write', turn=turn)
    if not stopped:
        reply(turn, f'msg-{turn}-final', [{'type': 'text', 'text': 'done'}])
    final = os.environ.get('FAKE_FINAL_FIRST') == '1' or delivered
    out({'type': 'result', 'subtype': 'success', 'is_error': False, 'user_message_uuids': [payload.get('uuid')],
         'result': 'artifact: -\nverdict: PASS\nblocker: none' if final else 'runtime_wait: registered-children'})
    if mid_turn:
        # #1890 R2 red 5: a correction written mid-turn opens a late turn whose output comes first.
        log(event='late-output', turn=turn)
        out({'type': 'assistant', 'parent_tool_use_id': None, 'message': {'id': f'late-{turn}', 'content': [
            {'type': 'text', 'text': 'x' * 180000}]}})
'''

FAKE_JOIN = r'''
import json, sys
jobs = sys.argv[sys.argv.index('--jobs') + 1]
parent = sys.argv[sys.argv.index('--parent-attempt-id') + 1]
attempts = [sys.argv[i + 1] for i, value in enumerate(sys.argv) if value == '--attempt-id']
with open(jobs, encoding='utf-8') as handle:
    lines = handle.read().splitlines()
kept = []
for line in lines:
    fields = line.split('\t')
    meta = dict(part.split('=', 1) for part in fields[5].split(',') if '=' in part) if len(fields) == 6 else {}
    if meta.get('attempt_id') in attempts:
        fields[1] = 'done'
        fields[5] += ',failure_class=pass,note=completed-supervisor,launch_outcome=never-launched'
    kept.append('\t'.join(fields))
with open(jobs, 'w', encoding='utf-8') as handle:
    handle.write('\n'.join(kept) + '\n')
print(json.dumps({'schema_version': 2, 'state': 'ready', 'parent_attempt_id': parent,
    'children': [{'attempt_id': attempt, 'status': 'done', 'readiness': 'ready', 'reason': 'registry-closed',
                  'required_action': 'advance-completed'} for attempt in attempts]}))
'''


def owner_row(lease: Path) -> str:
    return (
        "2026-11-28T00:00:00Z\topen\t/repo\t/wt\towner\t"
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "fallback_hop=same-harness-headless,worker_type=owner,harness=claude,"
        "completion_delivery=session-resume-supervised,supervisor_lease=flock-v1,"
        f"supervisor_lease_file={lease},supervisor_lease_nonce={'c' * 64},"
        f"attempt_id={PARENT}\n"
    )


def child_row() -> str:
    return (
        "2026-11-28T00:00:00Z\topen\t/repo\t/wt\tchild\t"
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,launch_started=1,"
        f"harness=claude,attempt_id=att-in-turn-child,parent_attempt_id={PARENT}\n"
    )


def correction(request_id: str, size: int = 0) -> dict:
    return {"id": request_id, "size": size} if size else {"id": request_id, "text": f"correction {request_id}"}


class InTurnCorrectionTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".agent_reports").mkdir()
        self.claude = self.root / "fake_claude.py"
        self.claude.write_text(textwrap.dedent(FAKE_CLAUDE), encoding="utf-8")
        self.join = self.root / "fake_join.py"
        self.join.write_text(textwrap.dedent(FAKE_JOIN), encoding="utf-8")
        self.runs = 0

    def run_owner(self, plan: dict, *, child: bool = True) -> list[dict]:
        self.runs += 1
        base = self.root / f"run-{self.runs}"
        base.mkdir()
        self.jobs, self.trace = base / "jobs.log", base / "trace.jsonl"
        lease = base / "supervisor-state" / f"{PARENT}.lease"
        self.jobs.write_text(owner_row(lease) + (child_row() if child else ""), encoding="utf-8")
        command = [
            sys.executable, str(SUPERVISOR), "--worktree", str(self.root), "--jobs", str(self.jobs),
            "--parent-attempt-id", PARENT, "--state-file", str(base / "supervisor-state.json"),
            "--lease-file", str(lease), "--add-dir", str(self.root),
            "--claude-command", f"{sys.executable} {self.claude}",
            "--join-command", f"{sys.executable} {self.join}", "--join-timeout", "2",
            "--join-interval", "0.02", "--turn-transport", "stream-json",
        ]
        env = {**os.environ, "AGENT_ARTIFACT_ROOT": str(self.root / ".agent_reports"),
               "FAKE_TRACE": str(self.trace), "FAKE_JOBS": str(self.jobs), "FAKE_PARENT": PARENT,
               "FAKE_ROOT": str(ROOT), "FAKE_PLAN": json.dumps(plan)}
        if not child:
            env["FAKE_FINAL_FIRST"] = "1"
        result = subprocess.run(command, input="initial assignment", text=True, capture_output=True,
                                env=env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:] + result.stdout[-4000:])
        return [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()]

    def receipt(self, request_id: str) -> dict:
        requests = json.loads(I._path(self.jobs, PARENT).read_text())["requests"]
        return next(item for item in requests if item["id"] == request_id)

    @staticmethod
    def reads(trace: list[dict], request_id: str) -> list[tuple[int, str]]:
        return [(row["turn"], row["via"]) for row in trace
                if row["event"] == "read" and request_id in row["ids"]]

    def test_a_correction_filed_in_the_first_turn_is_read_in_that_turn(self):
        trace = self.run_owner({"1": [{"file": [correction("C")]}, {}]})
        self.assertIn("--include-hook-events", trace[0]["args"])
        self.assertEqual(self.reads(trace, "C"), [(1, "hook")])
        item = self.receipt("C")
        self.assertEqual(item["state"], "turn-completed")
        self.assertEqual([(e["state"], e.get("mode"), e.get("turn_id")) for e in item["events"]][:3],
                         [("queued", None, None), ("sending", "in-turn", "1"), ("accepted", None, "1")])
        prompts = [row["prompt"] for row in trace if row["event"] == "turn-start"]
        self.assertEqual(len(prompts), 2)
        self.assertNotIn("correction C", prompts[1])

    def test_a_carry_the_stream_does_not_confirm_waits_for_the_next_turn(self):
        cases = {
            # The hook timed out after taking it: Claude Code discards the output.
            "cancelled": ({"hook": "cancelled"}, [(2, "prompt")]),
            # An errored tool: whether context rides it is not documented.
            "tool-error": ({"error": True}, [(2, "prompt")]),
            # A runtime without hook events: the model read it, so it may be read twice, never lost.
            "no-hook-event": ({"hook": "silent"}, [(1, "hook"), (2, "prompt")]),
            # A subagent's tool: its context is the subagent's, so the hook leaves it queued.
            "subagent": ({"subagent": True}, [(2, "prompt")]),
        }
        for name, (step, expected) in cases.items():
            with self.subTest(name):
                trace = self.run_owner({"1": [{"file": [correction("C")]}, step]})
                self.assertEqual(self.reads(trace, "C"), expected)
                item = self.receipt("C")
                self.assertEqual((item["state"], item["turn_id"]), ("turn-completed", "2"))
                in_turn = [e for e in item["events"] if e.get("mode") == "in-turn"]
                if name == "subagent":
                    self.assertEqual(in_turn, [])
                else:
                    self.assertEqual(in_turn[0]["state"], "sending")
                    self.assertIn("in-turn-unread", [e.get("reason") for e in item["events"]])

    def test_a_turn_that_ends_right_after_the_tool_never_claims_the_read(self):
        # No reply followed the tool, so the read is unconfirmed even though the hook
        # output was received; the queued correction then opens the existing next turn.
        trace = self.run_owner({"1": [{"file": [correction("C")]}, {"stop": True}]}, child=False)
        self.assertEqual(self.reads(trace, "C")[0], (2, "prompt"))
        item = self.receipt("C")
        self.assertEqual((item["state"], item["turn_id"]), ("turn-completed", "2"))

    def test_a_correction_after_the_last_tool_goes_to_the_next_turn(self):
        trace = self.run_owner({"1": [{}, {"file": [correction("C")]}]})
        self.assertEqual(self.reads(trace, "C"), [(2, "prompt")])
        item = self.receipt("C")
        self.assertEqual((item["state"], item["turn_id"]), ("turn-completed", "2"))
        self.assertFalse(any(e.get("mode") == "in-turn" for e in item["events"]))

    def test_the_r2_losses_do_not_reproduce(self):
        # red 1: a hook that blocks a correction sent on its own; red 5: a 65,536-byte
        # correction next to a late correction turn's 180,000-byte output.
        trace = self.run_owner({"1": [{"file": [correction("C"), correction("D", 65536)]}, {}]})
        events = {row["event"] for row in trace}
        self.assertFalse(events & {"blocked", "mid-turn-write", "late-output"}, trace)
        self.assertEqual(self.reads(trace, "C"), [(1, "hook")])
        self.assertEqual(self.reads(trace, "D"), [(2, "prompt")])
        self.assertEqual([self.receipt(name)["state"] for name in "CD"], ["turn-completed"] * 2)


if __name__ == "__main__":
    unittest.main()
