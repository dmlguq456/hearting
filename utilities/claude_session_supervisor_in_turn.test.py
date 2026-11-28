#!/usr/bin/env python3
"""A correction filed while a Claude owner's ordinary turn runs is shown there early and delivered next turn.

The real supervisor drives a fake `claude` that calls the real hooks from the
supervisor's `--settings` the way Claude Code 2.1.332 was measured to: every
prompt gets a fresh `prompt_id`, UserPromptSubmit sees the stream-json prompt
trimmed, every hook of that turn (and of its subagents, which add `agent_id`)
carries it, and a turn Claude opens itself (`task-notification`) carries a new
one. The hook shows a copy only beside a main-thread tool whose `prompt_id` was
bound to the prompt the supervisor recorded for an ordinary turn (OPERATIONS
§5.10); the receipt follows the next-turn delivery alone, so every failure path
keeps main's receipt.
"""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import hashlib
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
import fcntl, json, os, re, shlex, signal, subprocess, sys, time, uuid as U
args = sys.argv[1:]
trace, jobs, parent, root = (os.environ[name] for name in ('FAKE_TRACE', 'FAKE_JOBS', 'FAKE_PARENT', 'FAKE_ROOT'))
plan = json.loads(os.environ.get('FAKE_PLAN', '{}'))
hooks = json.loads(args[args.index('--settings') + 1])['hooks']
TOOL_HOOK = shlex.split(hooks['PreToolUse'][0]['hooks'][0]['command'])
PROMPT_HOOK = shlex.split(hooks['UserPromptSubmit'][0]['hooks'][0]['command'])
STREAM = '--input-format' in args
SESSION = args[args.index('--resume' if '--resume' in args else '--session-id') + 1]
# The real hook in a process whose every read of the input state fails (EIO).
EIO = ('import errno, pathlib, runpy, sys\nread = pathlib.Path.read_text\n'
       'def fail(self, *a, **k):\n    if str(self).endswith(".input.json"): raise OSError(errno.EIO, "EIO")\n'
       '    return read(self, *a, **k)\npathlib.Path.read_text = fail\n'
       'sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")')
# The real hook in a process whose write of the input state fails (EIO): its mark is lost.
MARK_EIO = ('import errno, runpy, sys\nsys.path.insert(0, sys.argv[1] + "/utilities")\n'
            'import dispatch_owner_input as I\ndef fail(*_): raise OSError(errno.EIO, "EIO")\nI._write = fail\n'
            'sys.argv = sys.argv[2:]; runpy.run_path(sys.argv[0], run_name="__main__")')
sys.path.insert(0, os.path.join(root, 'utilities'))
import dispatch_owner_input as I

def log(**row):
    with open(trace, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(row) + '\n')

def out(value):
    sys.stdout.write(json.dumps(value) + '\n')
    sys.stdout.flush()

def ids_in(text):
    found = []
    for line in text.split('\n'):  # the generator's own line breaks, not U+2028
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and 'id' in item:
            found.append(item['id'])
    return found

def input_state():
    with open(I._path(jobs, parent), encoding='utf-8') as handle:
        return json.load(handle)

def wait_blocked(process):
    # Until the hook waits on the lock, or ended at its unlocked pre-check.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and process.poll() is None:
        with open('/proc/locks', encoding='utf-8') as handle:
            if any('->' in line and f' {process.pid} ' in line for line in handle):
                log(event='waited-on-lock', pid=process.pid)
                return
        time.sleep(0.01)
    if process.poll() is None:
        raise AssertionError('the hook neither waited on the input lock nor ended')

def hook(kind, request, mode=None, locked=False):
    env = {**os.environ, 'AGENT_HOME': root, 'AGENT_DISPATCH_JOBS': jobs,
           'AGENT_DISPATCH_ATTEMPT_ID': parent, 'AGENT_DISPATCH_COMPLETION_MODE': 'supervised'}
    command = PROMPT_HOOK if kind == 'prompt' else TOOL_HOOK
    if mode == 'eio':
        command = [sys.executable, '-c', EIO, root + '/hooks/registered-parent-park.py'] + command[1:]
    if mode == 'mark-eio':
        command = [sys.executable, '-c', MARK_EIO, root, root + '/hooks/registered-parent-park.py']
    if mode == 'no-id':  # Claude Code before 2.1.196
        request.pop('prompt_id', None)
    held = None
    if locked:  # another writer holds the input lock while this hook starts
        held = os.open(str(I._path(jobs, parent)) + '.lock', os.O_RDWR)
        fcntl.flock(held, fcntl.LOCK_EX)
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env)
        process.stdin.write(json.dumps(request))
        process.stdin.close()
        if held is not None:
            wait_blocked(process)
    finally:
        if held is not None:
            os.close(held)
    stdout, stderr = process.stdout.read(), process.stderr.read()
    assert process.wait(timeout=60) == 0, stderr
    return stdout

def submit(text, prompt_id, turn, mode=None):
    """Claude calls UserPromptSubmit before it processes a prompt (trimmed from stream-json)."""
    if mode == 'skip':  # the hook failed or timed out: Claude discards it and goes on
        return
    sent = text.strip() if STREAM else text
    if mode == 'normalized':
        sent = sent.replace('\n', '\r\n')
    stdout = hook('prompt', {'session_id': SESSION, 'cwd': os.getcwd(), 'prompt_id': prompt_id,
                             'hook_event_name': 'UserPromptSubmit', 'prompt': sent},
                  'no-id' if mode == 'no-id' else 'eio' if mode == 'eio' else None)
    log(event='prompt-hook', turn=turn, stdout=stdout)

def tools(turn, tag, steps, prompt_id, mode=None):
    for index, step in enumerate(steps, 1):
        tool = f'toolu-{tag}-{index}'
        request = {'session_id': SESSION, 'cwd': os.getcwd(), 'prompt_id': prompt_id,
                   'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {}, 'tool_use_id': tool}
        if step.get('subagent'):
            request['agent_id'] = 'agent-1'
        if step.get('merge'):  # a prompt folded into this running turn keeps its prompt_id
            submit(step['merge'], prompt_id, turn)
        if step.get('hook') != 'skip':
            stdout = hook('tool', request, step.get('hook') or mode, locked=step.get('locked', False))
            context = (json.loads(stdout)['hookSpecificOutput'].get('additionalContext', '')
                       if stdout.strip() else '')
            if context:
                log(event='shown', turn=turn, tool=tool, ids=ids_in(context))
        if step.get('drop'):  # a writer that drops the turn record
            value = input_state()
            value.pop('turn', None)
            I._write(I._path(jobs, parent), value)
        for item in step.get('file', []):
            I.submit(jobs, parent, 'correction ' + item, item)
        for item in step.get('cli', []):
            # The previous head's writer left `"queued": 0`; `correct` keeps unknown keys.
            value = input_state()
            I._write(I._path(jobs, parent), {**value, 'queued': 0})
            with open(trace + '.message', 'w', encoding='utf-8') as handle:
                handle.write('correction ' + item)
            done = subprocess.run([sys.executable, root + '/utilities/capability-route.py', 'correct',
                                   '--jobs', jobs, '--attempt-id', parent, '--request-id', item,
                                   '--message-file', trace + '.message'], capture_output=True, text=True, timeout=60)
            assert done.returncode == 0, done.stderr
            log(event='cli', receipt=json.loads(done.stdout.splitlines()[0]))
        if step.get('kill'):
            os.kill(os.getppid(), signal.SIGKILL)
            os._exit(0)
        if step.get('close'):
            os._exit(0)

def held(until_reserved):
    """Hold the supervisor state lock, delaying its next turn state (and so its next record),
    optionally until it reserved its last turn."""
    handle = open(os.environ['AGENT_DISPATCH_COMPLETION_STATE_FILE'] + '.lock', 'a+')
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    ledger = os.path.join(os.path.dirname(jobs), 'supervisor-budget', parent + '.jsonl')
    deadline = time.monotonic() + 30
    while until_reserved and time.monotonic() < deadline:
        if os.path.exists(ledger) and '"terminal-handoff"' in open(ledger, encoding='utf-8').read():
            log(event='reserved-while-held')
            break
        time.sleep(0.02)
    return handle

def claude_turn(turn, spec, prompt_id):
    """What Claude does on its own after a result: a task-notification turn or a subagent."""
    lock = held(spec['hold'] == 'reserve') if spec.get('hold') else None
    if spec.get('kind') == 'subagent':  # a background subagent keeps its parent's prompt_id
        tools(turn, f'sub{turn}', spec['steps'], prompt_id)
    else:
        own = str(U.uuid4())
        submit('<task-notification>\n<task-id>bg</task-id>\n</task-notification>', own, turn)
        tools(turn, f'n{turn}', spec['steps'], own)
        out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'noted',
             'origin': {'kind': 'task-notification'}})
    if lock is not None:
        lock.close()

def run_turn(prompt, uuid):
    with open(trace, encoding='utf-8') as handle:
        turn = 1 + sum('"turn-start"' in line for line in handle)
    spec = plan.get(str(turn), [])
    spec = {'steps': spec} if isinstance(spec, list) else spec
    with open(os.environ['AGENT_DISPATCH_COMPLETION_STATE_FILE'], encoding='utf-8') as handle:
        delivered = json.load(handle)['delivered_attempt_ids']
    log(event='turn-start', turn=turn, prompt=prompt, session=SESSION,
        recorded='digest' in input_state().get('turn', {}))
    if '<owner-corrections>' in prompt:
        noted = re.search(r'Already shown beside a tool result in the previous turn: ([^.]*)[.]', prompt)
        log(event='read', turn=turn, ids=ids_in(prompt), noted=noted.group(1).split(', ') if noted else [])
    prompt_id = str(U.uuid4())
    fold = spec.get('fold')
    if fold is not None:  # the prompt arrives while a turn Claude opened is running: it joins that turn
        submit('<task-notification>\n<task-id>bg</task-id>\n</task-notification>', prompt_id, turn)
        tools(turn, f'{turn}-before', fold, prompt_id)
    submit(prompt, prompt_id, turn, spec.get('prompt'))
    tools(turn, turn, spec.get('steps', []), prompt_id, spec.get('prompt'))
    if spec.get('error'):
        status = spec['error']
        out({'type': 'assistant', 'parent_tool_use_id': None, 'error': 'server_error',
             'message': {'id': f'msg-{turn}', 'content': [{'type': 'text', 'text': f'API Error: {status}'}]}})
        text = ('Failed to authenticate. API Error: 403 Attention Required! | Cloudflare' if status == 403
                else f'API Error: {status} {{"type":"error","error":{{"type":"api_error"}}}}')
        out({'type': 'result', 'subtype': 'success', 'is_error': True, 'api_error_status': status,
             'user_message_uuids': [uuid], 'result': text})
        return
    final = os.environ.get('FAKE_FINAL_FIRST') == '1' or delivered
    out({'type': 'assistant', 'parent_tool_use_id': None,
         'message': {'id': f'msg-{turn}', 'content': [{'type': 'text', 'text': 'done'}]}})
    result = {'type': 'result', 'subtype': 'success', 'is_error': False, 'user_message_uuids': [uuid],
              'result': 'artifact: -\nverdict: PASS\nblocker: none' if final else 'runtime_wait: registered-children'}
    if fold is not None:
        result['origin'] = {'kind': 'task-notification'}
    out(result)
    for after in spec.get('after', []):
        claude_turn(turn, after, prompt_id)
    if os.environ.get('FAKE_VANISH_AFTER') == str(turn):
        os.rename(sys.argv[0], sys.argv[0] + '.gone')  # the next turn's process cannot start

if not STREAM:
    run_turn(sys.stdin.buffer.read().decode('utf-8'), None)
    sys.exit(0)
buffer = b''
while True:
    while b'\n' not in buffer:
        chunk = os.read(0, 65536)
        if not chunk:
            sys.exit(0)
        buffer += chunk
    line, buffer = buffer.split(b'\n', 1)
    payload = json.loads(line)
    run_turn(payload['message']['content'][0]['text'], payload.get('uuid'))
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


# main's receipt for a correction a later prompt delivers.
DELIVERED = [("queued", None), ("sending", "next-turn"), ("accepted", None), ("turn-completed", None)]


class InTurnCorrectionTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".agent_reports").mkdir()
        self.join = self.root / "fake_join.py"
        self.join.write_text(textwrap.dedent(FAKE_JOIN), encoding="utf-8")
        self.runs = 0

    def new_run(self, *, child: bool = True) -> None:
        self.runs += 1
        self.base = self.root / f"run-{self.runs}"
        self.base.mkdir()
        self.jobs, self.trace = self.base / "jobs.log", self.base / "trace.jsonl"
        self.trace.touch()
        self.claude = self.base / "fake_claude"
        self.claude.write_text(f"#!{sys.executable}\n" + textwrap.dedent(FAKE_CLAUDE), encoding="utf-8")
        self.claude.chmod(0o755)
        lease = self.base / "supervisor-state" / f"{PARENT}.lease"
        self.jobs.write_text(owner_row(lease) + (child_row() if child else ""), encoding="utf-8")
        self.child = child

    def supervise(self, plan: dict, *, rc: int = 0, transport: str = "stream-json", extra=(), env=None) -> list[dict]:
        command = [
            sys.executable, str(SUPERVISOR), "--worktree", str(self.root), "--jobs", str(self.jobs),
            "--parent-attempt-id", PARENT, "--state-file", str(self.base / "supervisor-state.json"),
            "--lease-file", str(self.base / "supervisor-state" / f"{PARENT}.lease"),
            "--add-dir", str(self.root), "--claude-command", str(self.claude),
            "--join-command", f"{sys.executable} {self.join}", "--join-timeout", "2",
            "--join-interval", "0.02", "--turn-transport", transport, *extra,
        ]
        env = {**os.environ, "AGENT_ARTIFACT_ROOT": str(self.root / ".agent_reports"),
               "FAKE_TRACE": str(self.trace), "FAKE_JOBS": str(self.jobs), "FAKE_PARENT": PARENT,
               "FAKE_ROOT": str(ROOT), "FAKE_PLAN": json.dumps(plan), **(env or {})}
        if not self.child:
            env["FAKE_FINAL_FIRST"] = "1"
        result = subprocess.run(command, input="initial assignment\nwith two lines", text=True, capture_output=True,
                                env=env, timeout=180)
        self.assertEqual(result.returncode, rc, result.stderr[-4000:] + result.stdout[-4000:])
        trace = [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()]
        # UserPromptSubmit adds its plain stdout to the model context: it must stay empty.
        self.assertEqual({row["stdout"] for row in trace if row["event"] == "prompt-hook"} - {""}, set())
        return trace

    def run_owner(self, plan: dict, *, child: bool = True, **kwargs) -> list[dict]:
        self.new_run(child=child)
        return self.supervise(plan, **kwargs)

    def receipt(self, request_id: str = "C") -> dict:
        requests = json.loads(I._path(self.jobs, PARENT).read_text())["requests"]
        return next(item for item in requests if item["id"] == request_id)

    def history(self, request_id: str = "C") -> list[tuple]:
        return [(event["state"], event.get("mode") or event.get("reason"))
                for event in self.receipt(request_id)["events"]]

    @staticmethod
    def shown(trace: list[dict], request_id: str = "C") -> list[str]:
        return [row["tool"] for row in trace if row["event"] == "shown" and request_id in row["ids"]]

    @staticmethod
    def reads(trace: list[dict], request_id: str = "C") -> list[tuple[int, bool]]:
        return [(row["turn"], request_id in row["noted"]) for row in trace
                if row["event"] == "read" and request_id in row["ids"]]

    @staticmethod
    def recorded(trace: list[dict]) -> list[bool]:
        return [row["recorded"] for row in trace if row["event"] == "turn-start"]

    def test_a_first_turn_correction_is_shown_there_once_and_delivered_next_turn(self):
        for transport in ("stream-json", "resume-process"):
            with self.subTest(transport):
                trace = self.run_owner({"1": [{"file": ["C"]}, {}, {}]}, transport=transport)
                # Shown beside the next tool result only, not again at the tool after it.
                self.assertEqual(self.shown(trace), ["toolu-1-2"])
                # Delivered once by the next prompt, which notes the earlier copy; main's receipt.
                self.assertEqual(self.reads(trace), [(2, True)])
                self.assertEqual(self.history(), DELIVERED)
                self.assertEqual((self.receipt()["turn_id"], self.receipt()["shown"]), ("2", "toolu-1-2"))
                self.assertIn("a copy may also appear beside its next tool result",
                              I.inspect(self.jobs, PARENT)["delivery_notice"])
                self.assertTrue(all(self.recorded(trace)))

    def test_a_turn_claude_opens_after_the_result_shows_no_copy_while_the_reserve_is_taken(self):
        # Review round 3: the ordinary turn's result, the supervisor reserving its last turn while
        # the state lock delays its next turn state, then the tool of a turn Claude opened itself.
        trace = self.run_owner({"1": {"steps": [{}], "after": [
            {"hold": "reserve", "steps": [{"file": ["C"]}, {}, {"locked": True}]}]}},
            extra=("--max-continuations", "1"))
        self.assertIn({"event": "reserved-while-held"}, trace)
        self.assertEqual(self.shown(trace), [])
        self.assertNotIn("shown", self.receipt())
        self.assertEqual(self.reads(trace)[:1], [(2, False)])

    def test_a_background_subagent_after_the_result_shows_no_copy(self):
        trace = self.run_owner({"1": {"steps": [{}], "after": [
            {"kind": "subagent", "hold": "lock", "steps": [{"file": ["C"], "subagent": True},
                                                          {"subagent": True}]}]}})
        self.assertEqual((self.shown(trace), self.reads(trace)), ([], [(2, False)]))
        self.assertEqual(self.history(), DELIVERED)

    def test_a_subagent_tool_or_no_later_tool_shows_no_copy(self):
        for name, plan in {"subagent": {"1": [{"file": ["C"]}, {"subagent": True}]},
                           "after-last-tool": {"1": [{}, {"file": ["C"]}]}}.items():
            with self.subTest(name):
                trace = self.run_owner(plan)
                self.assertEqual(self.shown(trace), [])
                self.assertEqual(self.reads(trace), [(2, False)])
                self.assertEqual(self.history(), DELIVERED)

    def test_a_hook_that_waited_on_the_input_lock_shows_only_for_the_bound_prompt_id(self):
        trace = self.run_owner({"1": {"steps": [{"file": ["C"]}, {"locked": True}], "after": [
            {"hold": "lock", "steps": [{"file": ["D"]}, {"locked": True}]}]}})
        self.assertEqual((self.shown(trace), self.shown(trace, "D")), (["toolu-1-2"], []))
        self.assertEqual((self.reads(trace), self.reads(trace, "D")), ([(2, True)], [(2, False)]))
        self.assertEqual(len([row for row in trace if row["event"] == "waited-on-lock"]), 1)

    def test_corrections_filed_during_the_turn_keep_showing_and_a_folded_prompt_keeps_the_binding(self):
        # Claude folds a prompt written during a tool into the running turn under the same prompt_id.
        trace = self.run_owner({"1": [{"file": ["C"]}, {"merge": "a folded prompt", "cli": ["D"]}, {}]})
        self.assertEqual((self.shown(trace), self.shown(trace, "D")), (["toolu-1-2"], ["toolu-1-3"]))
        self.assertEqual((self.reads(trace), self.reads(trace, "D")), ([(2, True)], [(2, True)]))
        self.assertEqual((self.history(), self.history("D")), (DELIVERED, DELIVERED))
        receipts = [row["receipt"] for row in trace if row["event"] == "cli"]
        self.assertTrue(receipts and all("shown" not in json.dumps(receipt) for receipt in receipts))
        self.assertNotIn("shown", json.dumps(I.inspect(self.jobs, PARENT)))

    def test_known_limit_a_supervisor_prompt_folded_into_a_turn_claude_opened_shows_after_the_fold(self):
        # Measured on 2.1.332 (probe g): the folded prompt's UserPromptSubmit carries the running
        # turn's prompt_id, so tools after the fold show the copy; the supervisor takes that turn's
        # result as its answer (the uuid echoes).
        trace = self.run_owner({"1": {"fold": [{"file": ["C"]}, {}], "steps": [{}, {}]}})
        self.assertEqual(self.shown(trace), ["toolu-1-1"])
        self.assertEqual((self.reads(trace), self.history()), ([(2, True)], DELIVERED))

    def test_the_reserved_turn_and_its_edge_retry_show_none_and_the_correction_turn_shows_one(self):
        for transport in ("stream-json", "resume-process"):
            with self.subTest(transport):
                trace = self.run_owner({"2": {"steps": [{"file": ["C"]}, {}], "error": 403},
                                        "3": [{"file": ["E"]}, {}], "4": [{"file": ["D"]}, {}, {}]},
                                       transport=transport, extra=("--max-continuations", "1"))
                turns = [row for row in trace if row["event"] == "turn-start"]
                self.assertIn("reserved", turns[1]["prompt"])
                self.assertIn("transient service block", turns[2]["prompt"])
                # The supervisor records a prompt only for a turn it opened as ordinary.
                self.assertEqual(self.recorded(trace), [True, False, False] + [True] * (len(turns) - 3))
                self.assertEqual((self.shown(trace), self.shown(trace, "E")), ([], []))
                self.assertEqual((self.reads(trace), self.reads(trace, "E")), ([(3, False)], [(4, False)]))
                self.assertEqual((self.shown(trace, "D"), self.reads(trace, "D")), (["toolu-4-2"], [(5, True)]))
                self.assertEqual((self.history(), self.history("D")), (DELIVERED, DELIVERED))

    def test_without_a_binding_no_copy_is_shown_and_delivery_is_mains(self):
        for name, spec in {
                "hooks without prompt_id (Claude Code before 2.1.196)": {"prompt": "no-id"},
                "UserPromptSubmit failed or timed out": {"prompt": "skip"},
                "the prompt normalized differently": {"prompt": "normalized"},
                "the input state unreadable to the hooks": {"prompt": "eio"},
                "a writer dropped the turn record": {"steps": [{"file": ["C"], "drop": True}, {}, {}]}}.items():
            with self.subTest(name):
                steps = spec.get("steps", [{"file": ["C"]}, {}, {}])
                # A turn Claude opens later binds nothing in place of the missed one.
                trace = self.run_owner({"1": {**spec, "steps": steps, "after": [{"hold": "lock", "steps": [{}]}]}})
                self.assertEqual((self.shown(trace), self.reads(trace)), ([], [(2, False)]))
                self.assertEqual(self.history(), DELIVERED)

    def test_the_review_loss_paths_keep_it_queued_for_main_delivery(self):
        with self.subTest("supervisor SIGKILL, then restart"):
            trace = self.run_owner({"1": [{"file": ["C"]}, {"kill": True}]}, rc=-9)
            self.assertEqual(self.shown(trace), ["toolu-1-2"])
            self.assertEqual(self.receipt()["state"], "queued")
            # The same attempt and session reattach, as the adapter does.
            trace = self.supervise({}, extra=("--resume-session", trace[0]["session"]))
            self.assertEqual(self.reads(trace), [(2, True)])
            self.assertEqual(self.history(), DELIVERED)
        with self.subTest("the hook's state write fails (EIO)"):
            trace = self.run_owner({"1": [{"file": ["C"]}, {"hook": "mark-eio"}, {}, {}]})
            # The lost mark only shows it once more.
            self.assertEqual(self.shown(trace), ["toolu-1-2", "toolu-1-3"])
            self.assertEqual(self.reads(trace), [(2, True)])
            self.assertEqual(self.history(), DELIVERED)
        # The supervisor ends on these in main as well, with main's receipt (handed back through the
        # supervision notice): the copy leaves it exactly as a run whose hooks showed nothing, and the
        # hook never moved it out of `queued`.
        for name, plan, kwargs, expected in (
                ("turn exception (stdout closed mid-turn)", {"1": [{"file": ["C"]}, {"close": True}]},
                 {"child": False, "rc": 70}, [("queued", None), ("undelivered", "supervisor-stopped")]),
                ("next turn not started (SubmissionNotStarted)", {"1": [{"file": ["C"]}, {}]},
                 {"rc": 70, "transport": "resume-process", "env": {"FAKE_VANISH_AFTER": "1"}},
                 [("queued", None), ("sending", "next-turn"), ("delivery-unknown", "supervisor-stopped")]),
                ("API error (500)", {"1": {"steps": [{"file": ["C"]}, {}], "error": 500}},
                 {"child": False, "rc": 3}, [("queued", None), ("undelivered", "supervisor-stopped")])):
            with self.subTest(name):
                trace = self.run_owner(plan, **kwargs)
                self.assertEqual((self.shown(trace), self.reads(trace)), (["toolu-1-2"], []))
                self.assertEqual(self.history(), expected)
                unhooked = {turn: ({**spec, "prompt": "skip"} if isinstance(spec, dict) else
                                   {"steps": spec, "prompt": "skip"}) for turn, spec in plan.items()}
                trace = self.run_owner(unhooked, **kwargs)
                self.assertEqual(self.shown(trace), [])
                self.assertEqual(self.history(), expected)

    def test_an_explicit_cleanup_turn_shows_no_copy(self):
        for transport in ("stream-json", "resume-process"):
            with self.subTest(transport):
                self.new_run()
                route = {"cwd": str(self.root), "artifact_root": str(self.root / ".agent_reports"),
                         "nodes": [{"id": "report", "terminal": True}],
                         "workflow_contract": {"terminal_nodes": ["report"]},
                         "runtime_support": {"terminal_commit": True}}
                encoded = json.dumps(route, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                route["route_hash"] = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
                route["route_id"] = "rt-" + route["route_hash"].split(":", 1)[1][:16]
                (self.base / "route.json").write_text(json.dumps(route), encoding="utf-8")
                trace = self.supervise(
                    {"2": [{"file": ["C"]}, {}, {}]}, rc=3, transport=transport,
                    extra=("--max-continuations", "1", "--enable-terminal-commit",
                           "--route-file", str(self.base / "route.json"),
                           "--route-id", route["route_id"], "--route-hash", route["route_hash"]))
                turns = [row for row in trace if row["event"] == "turn-start"]
                self.assertIn("partial report", turns[1]["prompt"])
                self.assertEqual(self.recorded(trace), [True, False])
                self.assertEqual(self.shown(trace), [])
                self.assertNotIn("shown", self.receipt())


if __name__ == "__main__":
    unittest.main()
