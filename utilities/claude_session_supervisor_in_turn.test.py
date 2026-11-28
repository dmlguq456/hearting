#!/usr/bin/env python3
"""A correction filed while a Claude owner's turn runs is shown there early and delivered next turn.

The real supervisor drives a fake `claude` that runs the real PreToolUse hook
from the supervisor's `--settings`. The hook only shows a copy of queued
corrections beside a tool result (OPERATIONS §5.10); the receipt follows the
next-turn delivery alone, so every failure path keeps main's receipt.
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
import errno, json, os, signal, subprocess, sys
args = sys.argv[1:]
trace, jobs, parent, root = (os.environ[name] for name in ('FAKE_TRACE', 'FAKE_JOBS', 'FAKE_PARENT', 'FAKE_ROOT'))
plan = json.loads(os.environ.get('FAKE_PLAN', '{}'))
hook = json.loads(args[args.index('--settings') + 1])['hooks']['PreToolUse'][0]['hooks'][0]['command']
# The real hook in a process whose state write fails (EIO): its mark is lost.
eio_hook = [sys.executable, '-c', 'import errno, runpy, sys; sys.path.insert(0, sys.argv[1] + "/utilities"); '
            'import dispatch_owner_input as I\ndef fail(*_): raise OSError(errno.EIO, "EIO")\nI._write = fail; '
            'sys.argv = sys.argv[2:]; runpy.run_path(sys.argv[0], run_name="__main__")',
            root, root + '/hooks/registered-parent-park.py']
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

def run_turn(prompt, uuid):
    with open(trace, encoding='utf-8') as handle:
        turn = 1 + sum('"turn-start"' in line for line in handle)
    session = args[args.index('--resume' if '--resume' in args else '--session-id') + 1]
    log(event='turn-start', turn=turn, prompt=prompt, session=session)
    if '<owner-corrections>' in prompt:
        log(event='read', turn=turn, ids=ids_in(prompt), noted='Already shown beside a tool result' in prompt)
    with open(os.environ['AGENT_DISPATCH_COMPLETION_STATE_FILE'], encoding='utf-8') as handle:
        delivered = json.load(handle)['delivered_attempt_ids']
    for index, step in enumerate(plan.get(str(turn), []), 1):
        tool = f'toolu-{turn}-{index}'
        request = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {},
                   'tool_use_id': tool, 'cwd': os.getcwd()}
        if step.get('subagent'):
            request['agent_id'] = 'agent-1'
        if step.get('hook') != 'skip':
            command = eio_hook if step.get('hook') == 'eio' else hook
            done = subprocess.run(command, shell=isinstance(command, str), input=json.dumps(request),
                                  capture_output=True, text=True, timeout=60,
                                  env={**os.environ, 'AGENT_HOME': root, 'AGENT_DISPATCH_JOBS': jobs,
                                       'AGENT_DISPATCH_ATTEMPT_ID': parent,
                                       'AGENT_DISPATCH_COMPLETION_MODE': 'supervised'})
            assert done.returncode == 0, done.stderr
            context = (json.loads(done.stdout)['hookSpecificOutput'].get('additionalContext', '')
                       if done.stdout.strip() else '')
            if context:
                log(event='shown', turn=turn, tool=tool, ids=ids_in(context))
        for item in step.get('file', []):
            I.submit(jobs, parent, 'correction ' + item, item)
        if step.get('kill'):
            os.kill(os.getppid(), signal.SIGKILL)
            os._exit(0)
        if step.get('close'):
            os._exit(0)
    final = os.environ.get('FAKE_FINAL_FIRST') == '1' or delivered
    out({'type': 'assistant', 'parent_tool_use_id': None,
         'message': {'id': f'msg-{turn}', 'content': [{'type': 'text', 'text': 'done'}]}})
    out({'type': 'result', 'subtype': 'success', 'is_error': False, 'user_message_uuids': [uuid],
         'result': 'artifact: -\nverdict: PASS\nblocker: none' if final else 'runtime_wait: registered-children'})
    if os.environ.get('FAKE_VANISH_AFTER') == str(turn):
        os.rename(sys.argv[0], sys.argv[0] + '.gone')  # the next turn's process cannot start

if '--input-format' not in args:
    run_turn(sys.stdin.read(), None)
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
        result = subprocess.run(command, input="initial assignment", text=True, capture_output=True,
                                env=env, timeout=120)
        self.assertEqual(result.returncode, rc, result.stderr[-4000:] + result.stdout[-4000:])
        return [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()]

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
        return [(row["turn"], row["noted"]) for row in trace
                if row["event"] == "read" and request_id in row["ids"]]

    def test_a_first_turn_correction_is_shown_there_once_and_delivered_next_turn(self):
        trace = self.run_owner({"1": [{"file": ["C"]}, {}, {}]})
        # Shown beside the next tool result only, not again at the tool after it.
        self.assertEqual(self.shown(trace), ["toolu-1-2"])
        # Delivered once by the next prompt, which notes the earlier copy; main's receipt.
        self.assertEqual(self.reads(trace), [(2, True)])
        self.assertEqual(self.history(), DELIVERED)
        self.assertEqual((self.receipt()["turn_id"], self.receipt()["shown"]), ("2", "toolu-1-2"))
        self.assertIn("is also shown beside the claude owner's next tool result",
                      I.inspect(self.jobs, PARENT)["delivery_notice"].lower())

    def test_a_subagent_tool_or_no_later_tool_shows_no_copy(self):
        for name, plan in {"subagent": {"1": [{"file": ["C"]}, {"subagent": True}]},
                           "after-last-tool": {"1": [{}, {"file": ["C"]}]}}.items():
            with self.subTest(name):
                trace = self.run_owner(plan)
                self.assertEqual(self.shown(trace), [])
                self.assertEqual(self.reads(trace), [(2, False)])
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
            trace = self.run_owner({"1": [{"file": ["C"]}, {"hook": "eio"}, {}, {}]})
            # The lost mark only shows it once more.
            self.assertEqual(self.shown(trace), ["toolu-1-2", "toolu-1-3"])
            self.assertEqual(self.reads(trace), [(2, True)])
            self.assertEqual(self.history(), DELIVERED)
        # The supervisor ends on these two in main as well, with main's receipt (handed back
        # through the supervision notice): the copy leaves it exactly as a run whose hook
        # showed nothing, and the hook never moved it out of `queued`.
        for name, plan, kwargs, expected in (
                ("turn exception (stdout closed mid-turn)", {"1": [{"file": ["C"]}, {"close": True}]},
                 {"child": False, "rc": 70}, [("queued", None), ("undelivered", "supervisor-stopped")]),
                ("next turn not started (SubmissionNotStarted)", {"1": [{"file": ["C"]}, {}]},
                 {"rc": 70, "transport": "resume-process", "env": {"FAKE_VANISH_AFTER": "1"}},
                 [("queued", None), ("sending", "next-turn"), ("delivery-unknown", "supervisor-stopped")])):
            with self.subTest(name):
                trace = self.run_owner(plan, **kwargs)
                self.assertEqual((self.shown(trace), self.reads(trace)), (["toolu-1-2"], []))
                self.assertEqual(self.history(), expected)
                self.run_owner({turn: [{**step, "hook": "skip"} for step in steps]
                                for turn, steps in plan.items()}, **kwargs)
                self.assertEqual(self.history(), expected)

    def test_the_reserved_last_turn_shows_no_copy(self):
        trace = self.run_owner({"2": [{"file": ["C"]}, {}]}, extra=("--max-continuations", "1"))
        prompts = [row["prompt"] for row in trace if row["event"] == "turn-start"]
        self.assertIn("reserved", prompts[1])
        self.assertEqual(self.shown(trace), [])
        self.assertEqual(self.reads(trace), [(3, False)])
        self.assertEqual(self.history(), DELIVERED)


if __name__ == "__main__":
    unittest.main()
