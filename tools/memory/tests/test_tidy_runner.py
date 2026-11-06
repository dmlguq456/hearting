#!/usr/bin/env python3
"""session-tidy runner: enqueue, detached supervisor, registered worker, apply, handoff.

Every subprocess runs inside ``tidy_isolation`` (a ``/var/tmp`` root for HOME, XDG_*,
MEM_STORE; remote off; no worker markers).  The registered worker, the exact-identity
state check and ``peer-steward`` are replaced by fake executables that live under that root; the replacement only works
because the test also sets ``HEARTING_TIDY_TEST_ROOT`` (see
``session_tidy_runner.test_root``).  The fake worker leaves its own finisher process,
so the "nothing is left behind" check at the end of every test is a real PID check.

Worker scenarios (``scenario.json``, one entry per launcher call; ``gate`` is a file the
test creates to let the worker finish, so no assertion depends on timing):
``ok`` · ``governor`` · ``fail-start`` · ``bad-json`` · ``bad-shape`` · ``no-output`` · ``hang`` ·
``symlink`` (the output is a link to ``target``) · ``oversize`` · ``directory`` (the output is a folder).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "utilities"))

from tidy_isolation import isolated_env  # noqa: E402
import session_tidy as st  # noqa: E402
import pane_ownership  # noqa: E402
import session_tidy_runner as runner  # noqa: E402
from test_tidy_isolation import FIXTURES  # noqa: E402

TIDY = ROOT / "utilities" / "session_tidy.py"
MEM = ROOT / "tools" / "memory" / "mem.py"
PANE = "test:pane-a"
CHOICE_SID = "11111111-1111-4111-8111-111111111111"
DAY = 86400
WAIT = 90.0

FAKE_WORKER = textwrap.dedent('''\
    #!{python}
    import json, os, pathlib, stat, subprocess, sys
    root = pathlib.Path(os.environ["FAKE_ROOT"])
    args = sys.argv[1:]
    def arg(name):
        return args[args.index(name) + 1] if name in args else ""
    counter = root / "calls.count"
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    scenario = json.loads((root / "scenario.json").read_text())
    calls = scenario.get("calls", [])
    spec = calls[n - 1] if n <= len(calls) else scenario.get("default", {{"mode": "ok"}})
    mode = spec.get("mode", "ok")
    prompt_path = pathlib.Path(arg("--prompt-file"))
    prompt = prompt_path.read_text()
    fields = {{}}
    for line in prompt.splitlines():
        for key in ("batch_id", "input (read only)", "input_digest", "output (write exactly this one file)"):
            if line.startswith(key + ":"):
                fields[key] = line.split(":", 1)[1].strip()
    input_path = pathlib.Path(fields["input (read only)"])
    saved = root / "inputs" / (str(n) + ".json")
    saved.parent.mkdir(exist_ok=True)
    saved.write_text(input_path.read_text())
    with open(root / "calls.jsonl", "a") as handle:
        handle.write(json.dumps({{
            "n": n, "mode": mode, "argv": args, "env_keys": sorted(os.environ), "cwd": os.getcwd(),
            "prompt": prompt, "prompt_mode": stat.S_IMODE(prompt_path.stat().st_mode),
            "input_mode": stat.S_IMODE(input_path.stat().st_mode), "fields": fields}}) + "\\n")
    if mode == "governor":
        print("check=failed\\nreason=model-worker-governor-denied\\nretryable=1\\nretry_after_seconds=0\\nchild_spawned=0")
        sys.exit(75)
    if mode == "fail-start":
        print("check=failed\\nreason=invalid-dispatch-worker-mode\\nchild_spawned=0")
        sys.exit(64)
    jobs = root / "jobs.log"
    attempt = "att-fake-" + str(n)
    meta = "capability=session-tidy,attempt_id=" + attempt + ",parent_attempt_id=att-other,worker_type=support"
    with open(jobs, "a") as handle:
        handle.write("2026-10-01T00:00:00Z\\topen\\t/w\\t/w\\t" + arg("--slug") + "\\t" + meta + "\\n")
    finisher = {finisher!r}
    proc = subprocess.Popen(
        [sys.executable, "-c", finisher, json.dumps(spec), fields["output (write exactly this one file)"],
         str(jobs), attempt],
        start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True)
    with open(root / "pids", "a") as handle:
        handle.write(str(proc.pid) + "\\n")
    print("check=ok")
    print("job_registry=" + str(jobs))
    print("attempt_id=" + attempt)
    print("registered=1")
    print("started=1")
    print("child_spawned=1")
    print("child_pid=" + str(proc.pid))
    print("child_pid_start=1")
    ''')

FAKE_FINISHER = textwrap.dedent('''\
    import json, os, sys, time
    spec, output, jobs, attempt = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
    mode = spec.get("mode", "ok")
    if mode == "hang":
        sys.exit(0)
    gate = spec.get("gate")
    deadline = time.time() + 120
    while gate and not os.path.exists(gate) and time.time() < deadline:
        time.sleep(0.05)
    if mode == "ok":
        doc = {"schema_version": 7, "batch_id": "the-worker-is-wrong", "input_digest": "wrong",
               "actions": spec.get("actions", [])}
        if "related" in spec:
            doc["related_existing_ids"] = spec["related"]
        if "card" in spec:
            doc["card"] = spec["card"]
        open(output, "w").write(json.dumps(doc))
    elif mode == "symlink":
        os.symlink(spec["target"], output)
    elif mode == "oversize":
        open(output, "w").write('{"actions": [], "pad": "' + "x" * (1100 * 1024) + '"}')
    elif mode == "directory":
        os.mkdir(output)
    elif mode == "bad-json":
        open(output, "w").write("{not json at all")
    elif mode == "bad-shape":
        open(output, "w").write(json.dumps({"actions": "x"}))
    with open(jobs, "a") as handle:
        handle.write("2026-10-01T00:00:01Z\\tdone\\t/w\\t/w\\tx\\tcapability=session-tidy,attempt_id=" + attempt + "\\n")
    ''')

FAKE_STATE = textwrap.dedent('''\
    #!{python}
    import os, pathlib, sys
    args = sys.argv[1:]
    def arg(name):
        return args[args.index(name) + 1]
    jobs, attempt = pathlib.Path(arg("--jobs")), arg("--attempt")
    root = pathlib.Path(os.environ["FAKE_ROOT"])
    with open(root / "states.jsonl", "a") as handle:
        handle.write(" ".join(args) + "\\n")
    rows = [l.split("\\t") for l in jobs.read_text().splitlines()] if jobs.exists() else []
    mine = [r for r in rows if len(r) > 5 and ("attempt_id=" + attempt) in r[5].split(",")]
    print("check=ok")
    print("state=" + ("dead" if mine and mine[-1][1] == "done" else "working"))
    ''')

FAKE_STEWARD = textwrap.dedent('''\
    #!{python}
    import json, os, pathlib, sys
    root = pathlib.Path(os.environ["FAKE_ROOT"])
    args = sys.argv[1:]
    body = pathlib.Path(args[args.index("--body-file") + 1]).read_text() if "--body-file" in args else None
    with open(root / "steward.jsonl", "a") as handle:
        handle.write(json.dumps({{"argv": args, "body": body}}) + "\\n")
    verdict = os.environ.get("FAKE_PEER_VERDICT", "true")
    exits = {{"true": 0, "failed": 1, "queued": 3, "unverified": 5}}
    print("prompted=" + verdict + " target=" + args[1] + " state_before=idle verify=observed ms=12")
    sys.exit(exits[verdict])
    ''')


FAKE_HERDR = textwrap.dedent('''\
    #!{python}
    import json, os, pathlib, sys, time
    root = pathlib.Path(os.environ["FAKE_ROOT"])
    args = sys.argv[1:]
    with open(root / "herdr.jsonl", "a") as handle:
        handle.write(json.dumps(args) + "\\n")
    gate = root / "idle-gate"
    deadline = time.time() + 60
    while (root / "hold-idle").exists() and not gate.exists() and time.time() < deadline:
        time.sleep(0.05)
    state = os.environ.get("FAKE_PANE_STATE", "idle")
    if state == "timeout":
        sys.stderr.write(json.dumps({{"error": {{"code": "timeout"}}}}))
        sys.exit(1)
    print(json.dumps({{"result": {{"agent": {{"agent": "claude", "agent_status": state, "pane_id": args[2]}}}}}}))
    ''')

FAKE_CLEAR_STEWARD = textwrap.dedent('''\
    #!{python}
    import json, os, pathlib, sys
    root = pathlib.Path(os.environ["FAKE_ROOT"])
    args = sys.argv[1:]
    exits = {{"true": 0, "skipped": 3, "failed": 1, "unverified": 5}}
    if args[0] == "continue":
        with open(root / "continue.jsonl", "a") as handle:
            handle.write(json.dumps({{"argv": args}}) + "\\n")
        verdict = os.environ.get("FAKE_CONTINUE_VERDICT", "true")
        print("continued=" + verdict + " target=" + args[1] + ("" if verdict == "true" else " reason=herdr-exit-1"))
        sys.exit(exits[verdict])
    with open(root / "clear.jsonl", "a") as handle:
        handle.write(json.dumps({{"argv": args}}) + "\\n")
    verdict = os.environ.get("FAKE_CLEAR_VERDICT", "true")
    extra = " new_session=sid-NEW" if verdict == "true" else " reason=draft"
    print("cleared=" + verdict + " target=" + args[1] + extra)
    sys.exit(exits[verdict])
    ''')


def jsonl(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def claude_row(kind: str, text: str, cwd: str = "/w") -> dict:
    return {"type": kind, "cwd": cwd, "sessionId": "s", "message": {"role": kind, "content": text}}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().rsplit(b") ", 1)[1][:1] != b"Z"
    except OSError:
        return False


class RunnerCase(unittest.TestCase):

    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        # Runner/clear fixtures start after pane admission. Physical ownership
        # is tested on real ptys in utilities/pane_ownership.test.py.
        proof = mock.patch.object(pane_ownership, "verified_pane", side_effect=lambda pane, *a, **kw: pane or "")
        proof.start()
        self.addCleanup(proof.stop)
        self.addCleanup(self.reap_detached)       # runs before the root is removed
        self.cwd = self.iso.root / "proj"
        self.cwd.mkdir()
        self.state = self.iso.xdg_state / "hearting" / "session-tidy"
        self.fake = self.iso.root / "fake"
        (self.fake / "bin").mkdir(parents=True)
        self.worker_cmd = self.fake / "bin" / "worker.py"
        finisher = FAKE_FINISHER
        self.worker_cmd.write_text(FAKE_WORKER.format(python=sys.executable, finisher=finisher), encoding="utf-8")
        self.steward_cmd = self.fake / "bin" / "steward.py"
        self.steward_cmd.write_text(FAKE_STEWARD.format(python=sys.executable), encoding="utf-8")
        self.state_cmd = self.fake / "bin" / "state.py"
        self.state_cmd.write_text(FAKE_STATE.format(python=sys.executable), encoding="utf-8")
        for path in (self.worker_cmd, self.steward_cmd, self.state_cmd):
            path.chmod(0o755)
        self.projects = self.iso.claude_dir / "projects" / "-proj"
        self.projects.mkdir(parents=True, exist_ok=True)
        self.scenario([])

    # -- fakes -------------------------------------------------------------

    def scenario(self, calls, default=None):
        doc = {"calls": calls, "default": default or {"mode": "ok"}}
        (self.fake / "scenario.json").write_text(json.dumps(doc), encoding="utf-8")

    def calls(self):
        path = self.fake / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def saved_input(self, n=1):
        return json.loads((self.fake / "inputs" / f"{n}.json").read_text(encoding="utf-8"))

    def gate(self, name="gate"):
        return str(self.fake / name)

    def open_gate(self, name="gate"):
        Path(self.gate(name)).write_text("go", encoding="utf-8")

    # -- running -----------------------------------------------------------

    def env(self, **more):
        env = {
            "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
            "HEARTING_TIDY_WORKER_CMD": str(self.worker_cmd),
            "HEARTING_TIDY_STATE_CMD": str(self.state_cmd),
            "HEARTING_TIDY_PEER_STEWARD": str(self.steward_cmd),
            "HEARTING_TIDY_POLL": "0.05",
            "HEARTING_TIDY_GOVERNOR_SLEEP_MIN": "0.02",
            "HEARTING_TIDY_GOVERNOR_SLEEP_MAX": "0.05",
            "HEARTING_TIDY_WORKER_WAIT": "60",
            "FAKE_ROOT": str(self.fake),
            "HERDR_PANE_ID": PANE,
        }
        env.update(more)
        return env

    def cli(self, *args, input=None, **more):
        code = (f"import sys; sys.path.insert(0, {str(ROOT / 'utilities')!r}); "
                "import pane_ownership; "
                "pane_ownership.verified_pane=lambda pane,*a,**kw: pane or ''; "
                "import session_tidy; sys.argv=['session_tidy.py',*sys.argv[1:]]; "
                "raise SystemExit(session_tidy.main())")
        return self.iso.run([sys.executable, "-c", code, *args], input=input, extra=self.env(**more), cwd=self.cwd)

    def transcript(self, sid, rows):
        path = self.projects / f"{sid}.jsonl"
        jsonl(path, rows)
        return path

    def enqueue(self, sid, **more):
        result = self.cli("enqueue", "--harness", "claude", "--session-id", sid, **more)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = result.stdout.strip()
        self.assertRegex(line, r"^enqueue=tidy-\d{14}-[0-9a-f]{6} seat=[0-9a-f]+ status=queued clear=manual hint=/clear$")
        return line.split()[0].split("=", 1)[1]

    def item(self, qid):
        return st.read_json(self.state / "queue" / f"{qid}.json")

    def wait_for(self, predicate, what, timeout=WAIT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def wait_status(self, qid, *statuses):
        return self.wait_for(lambda: (self.item(qid) or {}).get("status") in statuses and self.item(qid),
                             f"{qid} to reach {statuses}")

    def runner_pid(self, qid):
        raw = self.wait_for(lambda: (self.state / "queue" / f"{qid}.pid").exists(), "the runner pid file")
        self.assertTrue(raw)
        return int((self.state / "queue" / f"{qid}.pid").read_text(encoding="ascii"))

    def reap_detached(self):
        """Every detached process this test started must exit by itself; none may remain."""
        pids = []
        if (self.state / "queue").is_dir():
            pids += [int(p.read_text(encoding="ascii")) for p in (self.state / "queue").glob("*.pid")
                     if p.read_text(encoding="ascii").strip().isdigit()]
        if (self.fake / "pids").exists():
            pids += [int(x) for x in (self.fake / "pids").read_text().split()]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(alive(p) for p in pids):
            time.sleep(0.1)
        left = [p for p in pids if alive(p)]
        for pid in left:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertEqual(left, [], "detached processes were still running at the end of the test")

    # -- memory ------------------------------------------------------------

    def mem(self, *args):
        return self.iso.run([sys.executable, MEM, *args], cwd=self.cwd, extra=self.env())

    def records(self):
        db = self.iso.mem_store / "memory.db"
        if not db.exists():
            return {}
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return {r[0]: r[1:] for r in con.execute("SELECT id, status, type, tier, body FROM records")}
        finally:
            con.close()

    def notices(self):
        out = []
        folder = self.state / "notices"
        if folder.is_dir():
            for path in folder.glob("*.json"):
                out += [i["text"] for i in json.loads(path.read_text(encoding="utf-8")).get("items", [])]
        return out

    def watermark(self, sid, harness="claude"):
        return st.read_json(self.state / "watermarks" / f"{harness}-{st._digest(sid, size=16)}.json")

class IdleBackgroundCardRunTest(RunnerCase):
    def prepare_idle(self, sid, body=None):
        import session_tidy_clear as clear
        import session_tidy_idle_card as cards
        seat = st.resolve_seat("claude", str(self.cwd), sid=sid)
        st.run_hook("claude", "start", sid, cwd=str(self.cwd))
        st.run_hook("claude", "prompt", sid, cwd=str(self.cwd), prompt_text="Original goal stays")
        if body:
            st.write_card(seat, "claude", sid, body, cwd=str(self.cwd))
        self.assertEqual(clear.observe_idle_event("claude", sid, str(self.cwd), event_id="finished-turn",
            tokens=600000, now=1000), "booking=scheduled")
        req = clear.read_reservation(seat.key)
        req["idle"]["due_at"] = st.now_epoch() - 1
        req["deadline"] = st.now_epoch() + 300
        clear._write_reservation(req)
        claim, reason = clear.claim_idle_card_request(clear.reservation_path(seat.key), req["nonce"])
        self.assertEqual(reason, "")
        with mock.patch.object(runner, "_start_queued", side_effect=lambda item: item):
            accepted, reason = cards.enqueue(claim)
        self.assertTrue(accepted, reason)
        req = clear.read_reservation(seat.key)
        return seat, req, runner.read_item(req["queue_id"])

    def test_one_existing_worker_saves_the_full_card_applies_memory_and_injects_next_input(self):
        import session_tidy_clear as clear
        import tidy_transcripts as tt
        sid = "hidden-memory"
        original = "2026-11-06 user: preserve the original goal and spec/project/prd.md"
        self.transcript(sid, [claude_row("user", original, str(self.cwd))])
        with self.iso.patched_environ(self.env()):
            old = tt.read_pending("claude", sid, str(self.projects / f"{sid}.jsonl"))
            tt.mark_applied("claude", sid, old)
        with (self.projects / f"{sid}.jsonl").open("a") as f:
            f.write(json.dumps(claude_row("user", "New approved storage decision", str(self.cwd))) + "\n")
        previous = "## 원래 목표\n" + original + "\n## 사용자 원문\n" + ("Keep this dated requirement exactly.\n" * 400) + "FULL-HISTORY-END"
        body = "\n\n".join("## " + field + "\n" + (original if i == 0 else "none")
                               for i, field in enumerate(row["label"] for row in st.card_format.fields()))
        self.scenario([{"mode": "ok", "card": body, "actions": [
            {"kind": "add", "type": "decision", "tier": "durable", "body": "Hidden helper keeps the new storage decision."}]}])
        with self.iso.patched_environ(self.env()), \
             mock.patch.object(clear, "_ensure_helper", return_value=(True, "")), \
             mock.patch.object(clear, "_idle_route_reason", return_value=""):
            seat, req, item = self.prepare_idle(sid, previous)
            sequence = st.read_prompt_seq(seat)
            runner.process_item(item)
            self.assertEqual(runner.read_item(item["id"])["status"], "notified", runner.read_item(item["id"]))
            data = self.saved_input()
            self.assertIn("FULL-HISTORY-END", data["idle_card"]["previous_card"])
            self.assertEqual(data["idle_card"]["card_format"], st.card_format.policy())
            text = data["idle_card"]["conversation"]
            if data["idle_card"]["append_caller_session_text"]:
                text += next(row["text"] for row in data["sessions"] if row["sid"] == sid)
            self.assertIn(original, text)
            self.assertNotIn(original, data["sessions"][0]["text"])
            self.assertEqual(len(self.calls()), 1)
            self.assertEqual(st.read_prompt_seq(seat), sequence)
            self.assertEqual(st.read_latest_card(seat)["body"], body)
            self.assertTrue(clear._accepted_queue_relation(clear.read_reservation(seat.key), seat))
            self.assertIn("Hidden helper keeps the new storage decision.", " ".join(row[3] for row in self.records().values()))
            self.assertFalse(tt.read_pending("claude", sid, str(self.projects / f"{sid}.jsonl")).text)
            sent, deferred = [], []
            st.run_hook("claude", "start", sid, source="compact", cwd=str(self.cwd), render_context=False)
            st.run_hook("claude", "prompt", sid, cwd=str(self.cwd), prompt_text="Next task",
                        emit=sent.append, deferred=deferred)
            self.assertIn(original, "\n".join(sent))
            st.apply_deferred(deferred)
            self.assertNotIn(body, "\n".join(self.notices()))

    def test_missing_card_does_not_discard_valid_memory_or_authorize_compaction(self):
        import session_tidy_clear as clear
        sid = "hidden-no-card"
        self.transcript(sid, [claude_row("user", "Keep the storage decision", str(self.cwd))])
        self.scenario([{"mode": "ok", "actions": [
            {"kind": "add", "type": "decision", "tier": "durable", "body": "Memory survives a missing card."}]}])
        with self.iso.patched_environ(self.env()), \
             mock.patch.object(clear, "_ensure_helper", return_value=(True, "")), \
             mock.patch.object(clear, "_idle_route_reason", return_value=""):
            seat, req, item = self.prepare_idle(sid)
            runner.process_item(item)
            self.assertEqual(runner.read_item(item["id"])["status"], "notified", runner.read_item(item["id"]))
            self.assertIn("Memory survives a missing card.", " ".join(row[3] for row in self.records().values()))
            self.assertIsNone(st.read_latest_card(seat))
            self.assertFalse(clear._accepted_queue_relation(clear.read_reservation(seat.key), seat))


    def test_memory_failure_after_card_save_keeps_the_same_accepted_card(self):
        import session_tidy_clear as clear
        sid = "hidden-memory-failure"
        self.transcript(sid, [claude_row("user", "Keep the original goal", str(self.cwd))])
        self.scenario([{"mode": "ok", "card": "## 원래 목표\nKeep the original goal", "actions": []}])
        with self.iso.patched_environ(self.env()), \
             mock.patch.object(clear, "_ensure_helper", return_value=(True, "")), \
             mock.patch.object(clear, "_idle_route_reason", return_value=""):
            seat, req, item = self.prepare_idle(sid)
            with mock.patch.object(runner, "apply_actions", side_effect=runner.RunnerFailure("fixture apply failure")):
                runner.process_item(item)
            self.assertEqual(runner.read_item(item["id"])["status"], "failed")
            self.assertIn("Keep the original goal", st.read_latest_card(seat)["body"])
            self.assertTrue(clear._accepted_queue_relation(clear.read_reservation(seat.key), seat))

    def test_a_new_input_during_the_worker_keeps_memory_but_rejects_its_old_card(self):
        import session_tidy_clear as clear
        sid = "hidden-late-worker"
        self.transcript(sid, [claude_row("user", "A durable approved choice", str(self.cwd))])
        self.scenario([{"mode": "ok", "card": "stale card", "actions": [
            {"kind": "add", "type": "decision", "tier": "durable", "body": "The old approved choice remains durable."}]}])
        with self.iso.patched_environ(self.env()), \
             mock.patch.object(clear, "_ensure_helper", return_value=(True, "")), \
             mock.patch.object(clear, "_idle_route_reason", return_value=""):
            seat, req, item = self.prepare_idle(sid)
            validate = runner.validate_actions
            def new_input(*args):
                validate(*args)
                st.run_hook("claude", "prompt", sid, cwd=str(self.cwd), prompt_text="New unrelated work")
            with mock.patch.object(runner, "validate_actions", side_effect=new_input):
                runner.process_item(item)
            self.assertEqual(runner.read_item(item["id"])["status"], "notified", runner.read_item(item["id"]))
            self.assertIsNone(st.read_latest_card(seat))
            self.assertIn("The old approved choice remains durable.", " ".join(row[3] for row in self.records().values()))



class EnqueueTest(RunnerCase):

    def test_operation_identity_reuses_queue_row_and_recovers_runner_launch_failure(self):
        operation = "compact-event-identity"
        with self.iso.patched_environ({"HERDR_PANE_ID": ""}):
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            with mock.patch.object(runner, "start_detached", side_effect=[OSError("launch"), 4242]) as start:
                first = runner.enqueue_item(seat, "claude", "sid-A", str(self.cwd),
                                            operation_id=operation, start=False)
                failed = runner._start_queued(first)
                self.assertEqual(failed["id"], first["id"])
                self.assertEqual(failed["status"], "queued")
                self.assertEqual(failed["runner_start_error"], "OSError")

                resumed = runner.enqueue_item(seat, "claude", "sid-A", str(self.cwd),
                                              operation_id=operation)
                self.assertEqual(resumed["id"], first["id"])
                self.assertEqual(resumed["runner_pid"], 4242)
                self.assertEqual(start.call_count, 2)
                self.assertEqual(len([item for item in runner.list_items()
                                      if item.get("operation_id") == operation]), 1)

                resumed["status"] = "notified"
                runner.write_item(resumed)
                terminal = runner.enqueue_item(seat, "claude", "sid-A", str(self.cwd),
                                               operation_id=operation)
                self.assertEqual((terminal["id"], terminal["status"]), (first["id"], "notified"))
                self.assertEqual(start.call_count, 2)

    def test_interrupted_apply_stage_restarts_under_the_same_qid_and_batch(self):
        with self.iso.patched_environ(self.env()):
            seat = st.resolve_seat("claude", str(self.cwd), os.environ)
            qid = runner.operation_queue_id("compact-interrupted-operation")
            item = {"schema": runner.SCHEMA, "id": qid, "created": runner._now(),
                    "status": "applying", "updated": runner._now(), "seat": seat.as_dict(),
                    "harness": "claude", "sid": "sid-A", "cwd": str(self.cwd),
                    "transcript": "", "operation_id": "compact-interrupted-operation",
                    "batch": qid, "attempts": 1, "runner_pid": 99999999, "runner_pid_start": "1"}
            runner.write_item(item)
            with mock.patch.object(runner, "start_detached", return_value=4242) as launch:
                resumed = runner._start_queued(item)
            self.assertEqual(resumed["id"], qid)
            launch.assert_called_once_with(qid)

            applied = []
            bundle = SimpleNamespace(empty=False, unreadable=[], exchange=self.iso.root / "exchange")
            with mock.patch.object(runner, "assemble", return_value=bundle), \
                    mock.patch.object(runner, "prepare_exchange", return_value=str(self.iso.root / "exchange")), \
                    mock.patch.object(runner, "dispatch_worker", return_value={"attempt_id": "attempt"}), \
                    mock.patch.object(runner, "wait_worker"), \
                    mock.patch.object(runner, "validate_actions"), \
                    mock.patch.object(runner, "apply_actions", side_effect=lambda row, run_dir, _bundle:
                                      applied.append((row["id"], run_dir.name)) or "applied"), \
                    mock.patch.object(runner, "advance_watermarks"), \
                    mock.patch.object(runner, "publish_memory"), \
                    mock.patch.object(runner, "notify"), \
                    mock.patch.object(runner, "coverage_note", return_value=""), \
                    mock.patch.object(runner, "remove_exchange"):
                runner.process_item(resumed)
            self.assertEqual(applied, [(qid, qid)], runner.read_item(qid))
            self.assertEqual(runner.read_item(qid)["status"], "notified")
            self.assertEqual(runner.read_item(qid)["batch"], qid)
            with mock.patch.object(runner, "start_detached") as launch:
                runner._start_queued(runner.read_item(qid))
            launch.assert_not_called()
            self.assertEqual(applied, [(qid, qid)])

    def test_enqueue_returns_one_line_at_once_and_the_tidy_finishes_on_its_own(self):
        shutil.copy(FIXTURES / "claude-choice.jsonl", self.projects / f"{CHOICE_SID}.jsonl")
        self.scenario([{"mode": "ok", "gate": self.gate(), "actions": [
            {"kind": "add", "type": "decision", "tier": "durable",
             "body": "The worker's own durable decision about storage."}]}])
        qid = self.enqueue(CHOICE_SID)
        # The call returned while the worker is still held at its gate: nothing waited for it.
        self.assertNotIn(self.item(qid)["status"], ("notified", "failed"))
        for path in (self.state / "queue" / f"{qid}.json",):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.wait_status(qid, "waiting-worker")
        self.open_gate()
        self.wait_status(qid, "notified")

        # input bundle seen by the worker
        data = self.saved_input()
        self.assertEqual(data["caller"], {"harness": "claude", "sid": CHOICE_SID})
        self.assertEqual(data["limits"], {"max_new_records": 10, "max_duplicate_groups": 5})
        self.assertIn("정리 시험을 시작합니다", data["sessions"][0]["text"])
        self.assertIn("저장 방식을 어떻게 할까요?", {c["question"] for c in data["user_choices"]})
        self.assertEqual(data["sessions"][0]["cursor_from"], 0)

        # the worker's file was applied by mem tidy-apply, the user's answers were recorded as decisions
        bodies = " ".join(row[3] for row in self.records().values())
        self.assertIn("The worker's own durable decision about storage.", bodies)
        self.assertIn("[결정]", bodies)
        # the watermark moved to where the bundle ended
        mark = self.watermark(CHOICE_SID)
        self.assertEqual(mark["cursor"], data["sessions"][0]["cursor_to"])
        self.assertGreater(mark["cursor"], 0)
        # one result line, with the way back
        notes = self.notices()
        self.assertEqual(len(notes), 1, notes)
        self.assertRegex(notes[0], rf"^\[tidy\] 묶음 {qid}: .*되돌리기: mem tidy-undo {qid}$")
        # and the input bundle (conversation text) is gone afterwards
        self.assertFalse((self.state / "runs" / qid / "input_v1.json").exists())
        self.assertTrue((self.state / "runs" / qid / "result.json").exists())
        # the notice reaches the calling session at its next prompt
        shown = self.cli("hook", "--harness", "claude", "--event", "prompt", "--session-id", CHOICE_SID).stdout
        self.assertIn(f"mem tidy-undo {qid}", shown)

    def test_the_registered_worker_launch_is_one_route_free_support_tuple(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        (call,) = self.calls()
        argv = call["argv"]

        def value(flag):
            return argv[argv.index(flag) + 1]

        self.assertEqual(argv[0], "--start")
        self.assertEqual(value("--worker-type"), "support")
        self.assertEqual(value("--unit"), "ops/session-tidy-memory")
        self.assertEqual(value("--model-profile"), "balanced-deep")
        self.assertEqual(value("--dispatch-depth"), "1")
        self.assertEqual(value("--capability"), "session-tidy")
        self.assertEqual(value("--slug"), f"session-{qid}")
        for flag in ("--route-file", "--route-id", "--parent", "--worker-mode", "--model", "--effort"):
            self.assertNotIn(flag, argv)
        # the end of the worker is read from the checked exact-identity state, on this very attempt
        states = (self.fake / "states.jsonl").read_text().splitlines()
        self.assertGreaterEqual(len(states), 1)
        words = states[-1].split()
        self.assertEqual(words[words.index("--attempt") + 1], "att-fake-1")
        self.assertEqual(words[words.index("--jobs") + 1], str(self.fake / "jobs.log"))
        self.assertEqual(words[words.index("--pid-start") + 1], "1")
        self.assertEqual(words[-1], "attempt-state")
        # the prompt and the bundle are private files; the prompt names both paths
        self.assertEqual((call["prompt_mode"], call["input_mode"]), (0o600, 0o600))
        # the worker's files live under the artifact root (what the wrapper launches it with), not the state folder
        folder = self.cwd / ".agent_reports" / ".runtime" / "session-tidy" / qid
        self.assertIn(str(folder / "actions.json"), call["prompt"])
        self.assertIn(str(folder / "input_v1.json"), call["prompt"])
        self.assertNotIn(str(self.state), call["prompt"])
        self.assertIn("`artifact: -`", call["prompt"])

    def test_the_worker_starts_without_the_callers_worker_marker_session_or_pane(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        qid = self.enqueue("sid-A", CLAUDE_CODE_SESSION_ID="sid-A", AGENT_ROUTE_ID="rt-x",
                           AGENT_ARTIFACT_ROOT="/nowhere")
        self.wait_status(qid, "notified")
        (call,) = self.calls()
        keys = set(call["env_keys"])
        for forbidden in ("HERDR_PANE_ID", "CLAUDE_CODE_SESSION_ID", "AGENT_ROUTE_ID", "AGENT_ARTIFACT_ROOT",
                          "AGENT_SESSION_ROLE", "AGENT_DISPATCH_DEPTH", "AGENT_DISPATCH_CHILD"):
            self.assertNotIn(forbidden, keys)

    def test_an_enqueue_from_a_worker_session_does_nothing(self):
        self.transcript("sid-A", [claude_row("user", "x")])
        result = self.cli("enqueue", "--harness", "claude", "--session-id", "sid-A", AGENT_SESSION_ROLE="worker")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "enqueue=none reason=worker"))
        self.assertFalse((self.state / "queue").exists())

    def test_nothing_new_to_tidy_ends_without_a_worker(self):
        qid = self.enqueue("sid-empty")      # no record of this session at all
        self.wait_status(qid, "notified")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.notices(), ["[정리] 새로 정리할 대화가 없습니다."])


class ProjectCardSlotRunTest(RunnerCase):

    def test_project_queue_round_trip_uses_the_callers_card_slot(self):
        with self.iso.patched_environ({"HERDR_PANE_ID": ""}):
            seat_b = st.session_seat(st.resolve_seat("claude", str(self.cwd), sid="runner-B"),
                                     "claude", "runner-B")
            seat_a = st.session_seat(st.resolve_seat("claude", str(self.cwd), sid="runner-A"),
                                     "claude", "runner-A")
            with st.seat_lock(seat_b.key):
                st.write_card(seat_b, "claude", "runner-B", "B card", cwd=str(self.cwd))
            with st.seat_lock(seat_a.key):
                st.write_card(seat_a, "claude", "runner-A", "A card", cwd=str(self.cwd))
            project_seat = st.seat_for_project("claude", seat_b.project_key)
            with mock.patch.object(runner, "start_detached", return_value=0):
                item = runner.enqueue_item(project_seat, "claude", "runner-B", str(self.cwd))
            queued_physical_seat = st.seat_from_dict(item["seat"])
            queued_session_seat = st.session_seat(
                queued_physical_seat, item["harness"], item["sid"])
            self.assertEqual(st.session_identity(queued_session_seat), ("claude", "runner-B"))
            self.assertEqual(queued_physical_seat, project_seat)
            self.assertNotIn("sid", item["seat"])
            self.assertEqual(item["card_generation"], 1)
            run_dir = st.state_root() / "runs" / "batch-project"
            run_dir.mkdir(parents=True)
            import turn_change_record
            automatic = {'session_id': 'runner-B', 'summary': 'check exit 1; final reply reference'}
            with mock.patch.object(turn_change_record, 'supplementary_records', return_value=[automatic]) as observe:
                bundle = runner.assemble(item, "batch-project", run_dir)
            doc = json.loads(bundle.path.read_text(encoding="utf-8"))
            observe.assert_any_call('claude', 'runner-B', str(self.cwd))
            self.assertIn(automatic, doc['automatic_records'])
            self.assertEqual(doc["card"], "B card")
            before_a = st.read_latest_card(seat_a)
            with mock.patch.object(runner, "memory_refs", return_value=([{"id": "mem-B"}], 0)), \
                    mock.patch.object(runner, "coverage_note", return_value="covered"):
                runner.publish_memory(item, "batch-project", run_dir, bundle, "applied")
            self.assertEqual(st.read_latest_card(seat_a), before_a)
            self.assertEqual(st.read_latest_card(seat_b)["memory_refs"]["refs"], [{"id": "mem-B"}])


class ProjectCardHandoffTest(RunnerCase):

    def test_project_handoff_sends_and_marks_only_the_callers_card(self):
        for sid in ("runner-A", "runner-B"):
            started = self.cli("hook", "--harness", "claude", "--event", "start", "--session-id", sid,
                               HERDR_PANE_ID="")
            self.assertEqual(started.returncode, 0, started.stderr)
        for sid, body in (("runner-B", "B1 body"), ("runner-A", "A1 body")):
            wrote = self.cli("card", "--harness", "claude", "--session-id", sid, "--text", body,
                             HERDR_PANE_ID="")
            self.assertEqual(wrote.returncode, 0, wrote.stderr)
        with self.iso.patched_environ({"HERDR_PANE_ID": ""}):
            seat_a = st.resolve_seat("claude", str(self.cwd), sid="runner-A")
            seat_b = st.resolve_seat("claude", str(self.cwd), sid="runner-B")
            before_a = st.read_latest_card(seat_a)
        result = self.cli("handoff", "peer-pane", "--harness", "claude", "--session-id", "runner-B",
                          HERDR_PANE_ID="", FAKE_PEER_VERDICT="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^prompted=true target=peer-pane .* exit=0$")
        sent = [json.loads(line) for line in (self.fake / "steward.jsonl").read_text().splitlines()]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["body"].strip(), "B1 body")
        with self.iso.patched_environ({"HERDR_PANE_ID": ""}):
            after_a = st.read_latest_card(seat_a)
            after_b = st.read_latest_card(seat_b)
        self.assertEqual(after_a, before_a)
        self.assertEqual(after_b["body"], "B1 body")
        self.assertTrue(after_b["handed_off"])


class AutoClearTest(RunnerCase):
    """``enqueue`` inside herdr: the memory runner and the clear helper are two detached
    processes that never wait for each other (fake herdr and fake ``peer-steward.py clear``)."""

    def setUp(self):
        super().setUp()
        self.herdr_cmd = self.fake / "bin" / "herdr.py"
        self.herdr_cmd.write_text(FAKE_HERDR.format(python=sys.executable), encoding="utf-8")
        self.clear_cmd = self.fake / "bin" / "clear.py"
        self.clear_cmd.write_text(FAKE_CLEAR_STEWARD.format(python=sys.executable), encoding="utf-8")
        for path in (self.herdr_cmd, self.clear_cmd):
            path.chmod(0o755)

    def env(self, **more):
        env = super().env(HEARTING_TIDY_HERDR=str(self.herdr_cmd), HEARTING_TIDY_PEER_STEWARD=str(self.clear_cmd))
        env.update(more)
        return env

    def enqueue_line(self, sid, *flags, **more):
        result = self.cli("enqueue", "--harness", "claude", "--session-id", sid, *flags, **more)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def reservation(self):
        files = list((self.state / "clear").glob("*.json")) if (self.state / "clear").is_dir() else []
        return st.read_json(files[0]) if files else None

    def steps(self, name):
        path = self.fake / name
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def helper_gone(self):
        res = self.reservation()
        pid = (res or {}).get("helper", {}).get("pid")
        return bool(pid) and not alive(pid)

    def test_one_enqueue_starts_both_detached_processes_and_the_window_is_cleared_silently(self):
        shutil.copy(FIXTURES / "claude-choice.jsonl", self.projects / f"{CHOICE_SID}.jsonl")
        self.scenario([{"mode": "ok", "gate": self.gate(), "actions": []}])
        (self.fake / "hold-idle").write_text("x", encoding="utf-8")        # the turn has not ended yet
        line = self.enqueue_line(CHOICE_SID)
        self.assertRegex(line, r"^enqueue=tidy-\d{14}-[0-9a-f]{6} seat=[0-9a-f]+ status=queued clear=scheduled$")
        qid = line.split()[0].split("=", 1)[1]
        self.wait_for(lambda: (self.reservation() or {}).get("helper", {}).get("pid"), "the helper to register")
        self.wait_status(qid, "waiting-worker")                            # the tidy runs while the window waits
        self.assertEqual(self.reservation()["status"], "reserved")
        self.assertEqual(self.steps("clear.jsonl"), [])                    # nothing is typed before idle
        (self.fake / "idle-gate").write_text("go", encoding="utf-8")       # the turn ended: idle
        self.wait_for(lambda: (self.reservation() or {}).get("status") == "cleared", "the clear")
        # the memory worker is still held: the clear did not wait for it
        self.assertNotIn(self.item(qid)["status"], ("notified", "failed"))
        calls = self.steps("clear.jsonl")
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual((argv[0], argv[1]), ("clear", PANE))
        self.assertEqual(argv[argv.index("--request") + 1], str(self.state / "clear" / f"{self.item(qid)['seat']['key']}.json"))
        self.assertEqual(argv[argv.index("--nonce") + 1], self.reservation()["nonce"])
        self.open_gate()
        self.wait_status(qid, "notified")
        self.assertEqual(len(self.notices()), 1, self.notices())           # only the memory line: the clear was silent
        self.wait_for(self.helper_gone, "the helper to exit")

    def test_a_cleared_window_with_a_card_is_continued_once_and_silently(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        card = self.cli("card", "--harness", "claude", "--session-id", "sid-A", "--text", "다음 할 일: 표식 쓰기")
        self.assertEqual(card.returncode, 0, card.stderr)
        line = self.enqueue_line("sid-A")
        self.assertTrue(line.endswith("status=queued clear=scheduled"), line)
        self.wait_for(lambda: ((self.reservation() or {}).get("continued") or {}).get("state") == "sent",
                      "the continue")
        calls = self.steps("continue.jsonl")
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual((argv[0], argv[1]), ("continue", PANE))
        self.assertEqual(argv[argv.index("--nonce") + 1], self.reservation()["nonce"])
        self.assertEqual(len(self.steps("clear.jsonl")), 1)
        self.wait_for(self.helper_gone, "the helper to exit")
        self.assertEqual([n for n in self.notices() if "이어서해" in n], [])     # a success is silent

    def test_no_continue_clears_and_types_nothing_after_it_and_a_failed_continue_says_so_once(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        self.cli("card", "--harness", "claude", "--session-id", "sid-A", "--text", "다음 할 일: 표식 쓰기")
        line = self.enqueue_line("sid-A", "--no-continue")
        self.assertTrue(line.endswith("status=queued clear=scheduled continue=off"), line)
        self.wait_for(lambda: (self.reservation() or {}).get("status") == "cleared", "the clear")
        self.wait_for(self.helper_gone, "the helper to exit")
        self.assertEqual(self.steps("continue.jsonl"), [])
        self.cli("card", "--harness", "claude", "--session-id", "sid-A", "--text", "다음 할 일: 다시")
        self.enqueue_line("sid-A", FAKE_CONTINUE_VERDICT="failed")
        self.wait_for(lambda: ((self.reservation() or {}).get("continued") or {}).get("state") == "failed",
                      "the failed continue")
        self.wait_for(lambda: any("이어서해" in n for n in self.notices()), "the one result line")
        self.assertEqual(len(self.steps("continue.jsonl")), 1)

    def test_no_clear_starts_no_helper_and_cancels_a_pending_booking(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        (self.fake / "hold-idle").write_text("x", encoding="utf-8")
        self.assertTrue(self.enqueue_line("sid-A").endswith("clear=scheduled"))
        self.wait_for(lambda: (self.reservation() or {}).get("helper", {}).get("pid"), "the helper to register")
        pid = self.reservation()["helper"]["pid"]
        line = self.enqueue_line("sid-A", "--no-clear")
        self.assertTrue(line.endswith("status=queued clear=off"), line)
        self.assertIsNone(self.reservation())
        (self.fake / "idle-gate").write_text("go", encoding="utf-8")       # the first helper wakes and finds nothing
        self.wait_for(lambda: not alive(pid), "the cancelled helper to exit")
        self.assertEqual(self.steps("clear.jsonl"), [])
        self.assertEqual([n for n in self.notices() if "비우" in n], [])

    def test_a_newer_enqueue_replaces_the_booking_and_only_the_new_helper_types(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        (self.fake / "hold-idle").write_text("x", encoding="utf-8")
        self.enqueue_line("sid-A")
        self.wait_for(lambda: (self.reservation() or {}).get("helper", {}).get("pid"), "the first helper")
        first = self.reservation()
        self.enqueue_line("sid-A")
        self.wait_for(lambda: (self.reservation() or {}).get("nonce") != first["nonce"]
                      and (self.reservation() or {}).get("helper", {}).get("pid"), "the second helper")
        (self.fake / "idle-gate").write_text("go", encoding="utf-8")
        self.wait_for(lambda: (self.reservation() or {}).get("status") == "cleared", "the clear")
        self.wait_for(lambda: not alive(first["helper"]["pid"]), "the replaced helper to exit")
        self.assertEqual(len(self.steps("clear.jsonl")), 1)                 # one clear, not two

    def test_a_window_that_cannot_be_cleared_gets_one_line_and_is_never_retried(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        line = self.enqueue_line("sid-A", FAKE_CLEAR_VERDICT="skipped")
        self.assertTrue(line.endswith("clear=scheduled"), line)
        self.wait_for(lambda: (self.reservation() or {}).get("status") == "skipped", "the skip")
        self.wait_for(lambda: any("입력창" in n for n in self.notices()), "the one result line")
        self.assertEqual(len(self.steps("clear.jsonl")), 1)
        self.assertEqual(len([n for n in self.notices() if "자동으로 비우지 않았습니다" in n]), 1)

    def test_a_window_that_never_goes_idle_times_out_with_one_line_and_no_input(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        self.enqueue_line("sid-A", FAKE_PANE_STATE="timeout")
        self.wait_for(lambda: (self.reservation() or {}).get("status") == "skipped", "the timeout")
        self.assertEqual(self.steps("clear.jsonl"), [])
        self.wait_for(lambda: any("10분" in n for n in self.notices()), "the one result line")

    def test_the_helper_is_not_started_for_a_worker_or_outside_herdr(self):
        result = self.cli("enqueue", "--harness", "claude", "--session-id", "sid-A", AGENT_SESSION_ROLE="worker")
        self.assertEqual(result.stdout.strip(), "enqueue=none reason=worker")
        no_pane = self.iso.run([sys.executable, TIDY, "enqueue", "--harness", "codex", "--session-id", "sid-C"],
                               extra={k: v for k, v in self.env().items() if k != "HERDR_PANE_ID"}, cwd=self.cwd)
        self.assertTrue(no_pane.stdout.strip().endswith("clear=manual hint=/clear"), no_pane.stdout)
        self.assertFalse((self.state / "clear").exists())


class DetachTest(RunnerCase):

    def test_the_runner_survives_the_death_of_the_callers_process_group(self):
        self.transcript("sid-A", [claude_row("user", "죽은 부모의 결정")])
        self.scenario([{"mode": "ok", "gate": self.gate()}])
        # a stand-in for the harness's tool process: its own process group, runs the enqueue, then idles
        parent_code = (
            "import subprocess, sys, time\n"
            f"r = subprocess.run([sys.executable, {str(TIDY)!r}, 'enqueue', '--harness', 'claude', "
            "'--session-id', 'sid-A'], capture_output=True, text=True)\n"
            "print(r.stdout.strip(), flush=True)\n"
            "time.sleep(120)\n")
        parent = subprocess.Popen([sys.executable, "-c", parent_code], env=self.iso.env(self.env()),
                                  cwd=self.cwd, stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            line = parent.stdout.readline().strip()
            self.assertRegex(line, r"^enqueue=tidy-\d{14}-[0-9a-f]{6} ")
            qid = line.split()[0].split("=", 1)[1]
            pid = self.runner_pid(qid)
            self.wait_status(qid, "waiting-worker")
            os.killpg(parent.pid, signal.SIGKILL)       # what a harness cleanup of the caller's group does
            parent.wait(timeout=30)
            time.sleep(0.3)
            self.assertTrue(alive(pid), "the detached runner died with the caller's process group")
            self.open_gate()
            self.wait_status(qid, "notified")
        finally:
            if parent.poll() is None:
                os.killpg(parent.pid, signal.SIGKILL)
                parent.wait(timeout=30)
            parent.stdout.close()
        # the runner leaves on its own after the queue is empty
        self.wait_for(lambda: not alive(pid), "the runner to exit")


class SerializeTest(RunnerCase):

    def test_two_reservations_at_once_finish_one_after_the_other_without_a_refusal(self):
        self.transcript("sid-A", [claude_row("user", "첫 결정")])
        self.transcript("sid-B", [claude_row("user", "둘째 결정")])
        self.scenario([{"mode": "ok", "gate": self.gate("gate-a")}, {"mode": "ok", "gate": self.gate("gate-b")}])
        first = self.enqueue("sid-A")
        self.wait_status(first, "waiting-worker")
        second = self.enqueue("sid-B")          # accepted at once; it queues behind the first
        pid_b = self.runner_pid(second)
        time.sleep(0.5)
        self.assertEqual(self.item(second)["status"], "queued")
        self.assertTrue(alive(pid_b))
        self.assertEqual(len(self.calls()), 1)
        self.open_gate("gate-b")
        self.open_gate("gate-a")
        self.wait_status(first, "notified")
        self.wait_status(second, "notified")
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        # strictly one at a time: the second worker started after the first one's row closed
        self.assertEqual([c["fields"]["batch_id"] for c in calls], [first, second])
        self.assertEqual(len(self.notices()), 2)

    def test_the_same_session_tidied_twice_reads_only_what_came_after_the_first(self):
        path = self.transcript("sid-A", [claude_row("user", "옛 메시지 하나"), claude_row("assistant", "옛 답")])
        first = self.enqueue("sid-A")
        self.wait_status(first, "notified")
        mark = self.watermark("sid-A")["cursor"]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(claude_row("user", "새 메시지 둘"), ensure_ascii=False) + "\n")
        second = self.enqueue("sid-A")
        self.wait_status(second, "notified")
        one, two = self.saved_input(1), self.saved_input(2)
        self.assertIn("옛 메시지 하나", one["sessions"][0]["text"])
        text = two["sessions"][0]["text"]
        self.assertIn("새 메시지 둘", text)
        self.assertNotIn("옛 메시지 하나", text)
        self.assertEqual(two["sessions"][0]["cursor_from"], mark)
        self.assertGreater(self.watermark("sid-A")["cursor"], mark)


class RecentSessionsTest(RunnerCase):

    def test_recent_untidied_sessions_of_the_seat_join_and_older_ones_do_not(self):
        now = time.time()
        fresh = self.transcript("sid-fresh", [claude_row("user", "어제 닫은 세션의 결정")])
        stale = self.transcript("sid-stale", [claude_row("user", "나흘 전 세션의 결정")])
        os.utime(fresh, (now - 1 * DAY, now - 1 * DAY))
        os.utime(stale, (now - 4 * DAY, now - 4 * DAY))
        self.transcript("sid-A", [claude_row("user", "지금 세션의 결정")])
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "sid-fresh", "start", transcript=str(fresh), now=now - 1 * DAY)
            st.record_event(seat, "claude", "sid-stale", "start", transcript=str(stale), now=now - 4 * DAY)
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        got = {s["sid"]: s["role"] for s in self.saved_input()["sessions"]}
        self.assertEqual(got, {"sid-A": "caller", "sid-fresh": "recent"})
        for sid in ("sid-A", "sid-fresh"):
            self.assertGreater(self.watermark(sid)["cursor"], 0)
        self.assertIsNone(self.watermark("sid-stale"))


class FailureTest(RunnerCase):

    def setUp(self):
        super().setUp()
        self.transcript("sid-A", [claude_row("user", "실패해도 남아야 하는 결정")])
        self.mem("add", "durable", "lesson", "an existing lesson that must stay exactly as it is")
        self.card_line = self.cli("card", "--harness", "claude", "--session-id", "sid-A",
                                  "--text", "진행 중: 시험\n다음: 계속").stdout.strip()
        self.card_path = Path(self.card_line.split()[0].split("=", 1)[1])
        self.card_before = self.card_path.read_bytes()
        self.records_before = self.records()
        self.assertEqual(len(self.records_before), 1)

    def assert_untouched_and_told(self, qid, reason_part):
        item = self.wait_status(qid, "failed")
        self.assertIn(reason_part, item["error"])
        # The card's own text is untouched; only the "참고할 기억" section may follow it, and it says why.
        after = self.card_path.read_bytes()
        self.assertTrue(after.startswith(self.card_before), after)
        self.assertIn("정돈을 끝내지 못함", after.decode("utf-8")[len(self.card_before):])
        self.assertEqual(self.records(), self.records_before)
        self.assertIsNone(self.watermark("sid-A"))
        notes = self.notices()
        self.assertEqual(len(notes), 1, notes)
        self.assertRegex(notes[0], r"^\[정리\] 기억 정리를 끝내지 못했습니다\. 카드와 기존 기억은 그대로이고 다음 정리 때 이어서 처리됩니다\. \(사유: ")
        self.assertNotIn("\n", notes[0])
        self.assertFalse((self.state / "runs" / qid / "input_v1.json").exists())

    def test_a_record_that_cannot_be_read_is_a_failure_never_nothing_new(self):
        path = self.projects / "sid-A.jsonl"
        path.chmod(0)
        try:
            qid = self.enqueue("sid-A")
            self.assert_untouched_and_told(qid, "대화 기록을 읽지 못했습니다")
        finally:
            path.chmod(0o600)
        self.assertNotIn("새로 정리할 대화가 없습니다", " ".join(self.notices()))
        self.assertEqual(self.calls(), [])

    def test_a_worker_that_wrote_something_that_is_not_json(self):
        self.scenario([{"mode": "bad-json"}])
        self.assert_untouched_and_told(self.enqueue("sid-A"), "not valid JSON")

    def test_a_worker_that_wrote_the_wrong_shape(self):
        self.scenario([{"mode": "bad-shape"}])
        self.assert_untouched_and_told(self.enqueue("sid-A"), "actions list")

    def test_a_worker_that_finished_without_writing(self):
        self.scenario([{"mode": "no-output"}])
        self.assert_untouched_and_told(self.enqueue("sid-A"), "without writing actions.json")

    def test_a_launcher_that_refuses_for_a_reason_other_than_capacity(self):
        self.scenario([{"mode": "fail-start"}])
        self.assert_untouched_and_told(self.enqueue("sid-A"), "invalid-dispatch-worker-mode")
        self.assertEqual(len(self.calls()), 1)         # no retry for a real refusal

    def test_a_worker_that_never_finishes_is_given_up_on_after_the_wait_limit(self):
        self.scenario([{"mode": "hang"}])
        self.assert_untouched_and_told(self.enqueue("sid-A", HEARTING_TIDY_WORKER_WAIT="1"), "did not finish")

    def test_the_next_tidy_picks_the_same_conversation_up_again(self):
        self.scenario([{"mode": "bad-json"}, {"mode": "ok", "actions": [
            {"kind": "add", "type": "decision", "tier": "durable", "body": "Recovered on the next tidy."}]}])
        failed = self.enqueue("sid-A")
        self.wait_status(failed, "failed")
        again = self.enqueue("sid-A")
        self.wait_status(again, "notified")
        self.assertIn("실패해도 남아야 하는 결정", self.saved_input(2)["sessions"][0]["text"])
        self.assertIn("Recovered on the next tidy.", " ".join(r[3] for r in self.records().values()))
        self.assertIsNotNone(self.watermark("sid-A"))


class WorkerFolderTest(RunnerCase):
    """The worker's own files live under the artifact root the wrapper launches it with."""

    def folder(self, qid):
        return self.cwd / ".agent_reports" / ".runtime" / "session-tidy" / qid

    def test_the_folder_is_made_private_holds_an_exact_input_copy_and_goes_after_a_clean_finish(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        self.scenario([{"mode": "ok", "gate": self.gate(), "actions": []}])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "waiting-worker")
        folder = self.folder(qid)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for name in ("input_v1.json", "prompt.md"):
            self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600, name)
        # the same bytes as the state copy, so the digest the worker was given still holds
        self.assertEqual((folder / "input_v1.json").read_bytes(), (self.state / "runs" / qid / "input_v1.json").read_bytes())
        self.open_gate()
        self.wait_status(qid, "notified")
        self.assertFalse(folder.exists())                                  # nothing of the worker's is left behind
        self.assertTrue((self.state / "runs" / qid / "actions.json").is_file())   # the checked copy tidy-apply read
        checked = json.loads((self.state / "runs" / qid / "actions.json").read_text(encoding="utf-8"))
        self.assertEqual((checked["batch_id"], checked["schema_version"]), (qid, 1))   # the runner's identity, not the worker's

    def test_a_failed_batch_removes_the_conversation_copy_but_keeps_the_workers_answer_for_a_look(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        self.scenario([{"mode": "bad-json"}])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "failed")
        self.assertEqual(sorted(p.name for p in self.folder(qid).iterdir()), ["actions.json"])
        self.assertEqual(self.item(qid)["exchange"], str(self.folder(qid)))

    def test_a_link_a_folder_or_an_oversize_file_as_the_answer_is_refused(self):
        outside = self.iso.root / "elsewhere.json"
        outside.write_text('{"actions": []}', encoding="utf-8")
        cases = (({"mode": "symlink", "target": str(outside)}, "cannot be read"),
                 ({"mode": "oversize"}, "too large"),
                 ({"mode": "directory"}, "cannot be read|not a regular file|without writing"))
        self.scenario([spec for spec, _why in cases])                # one launcher call per case, in order
        for spec, why in cases:
            with self.subTest(mode=spec["mode"]):
                self.transcript("sid-A", [claude_row("user", f"무언가 결정했다 {spec['mode']}")])
                qid = self.enqueue("sid-A")
                item = self.wait_status(qid, "failed")
                self.assertRegex(item["error"], why)
                self.assertFalse((self.state / "runs" / qid / "actions.json").exists())     # nothing was applied
                self.assertEqual(self.records(), {})

    def test_the_folder_is_made_when_the_project_has_no_artifact_root_yet(self):
        self.assertFalse((self.cwd / ".agent_reports").exists())
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        self.assertTrue((self.cwd / ".agent_reports").is_dir())
        (call,) = self.calls()
        self.assertIn(str(self.cwd / ".agent_reports" / ".runtime" / "session-tidy" / qid), call["prompt"])

    def test_the_root_is_the_one_the_wrapper_resolves_for_a_linked_worktree(self):
        primary = self.iso.root / "primary"
        linked = self.iso.root / "linked"
        for command in (["git", "init", "-q", str(primary)],
                        ["git", "-C", str(primary), "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q",
                         "--allow-empty", "-m", "x"],
                        ["git", "-C", str(primary), "worktree", "add", "-q", str(linked)]):
            done = self.iso.run(command)
            self.assertEqual(done.returncode, 0, done.stderr)
        (primary / ".agent_reports").mkdir()
        with self.iso.patched_environ({"AGENT_ARTIFACT_ROOT": "/ignored"}):
            self.assertEqual(runner.artifact_root_for(str(linked)), (primary / ".agent_reports").resolve())

    def test_an_unresolvable_root_ends_the_tidy_with_the_usual_one_line(self):
        with self.assertRaises(runner.RunnerFailure) as caught:
            runner.artifact_root_for(str(self.iso.root / "no-such-folder"))
        self.assertIn("no artifact root for the worker's files", str(caught.exception))

    def test_artifact_resolution_timeout_preserves_unread_memory_and_warns_once(self):
        with self.iso.patched_environ(self.env()):
            seat = st.resolve_seat("claude", str(self.cwd), os.environ)
            st.write_card(seat, "claude", "sid-A", "진행 중인 일: 시간 초과 시험")
            item = {"schema": runner.SCHEMA, "id": runner.new_id(), "created": runner._now(),
                    "status": "queued", "updated": runner._now(), "seat": seat.as_dict(),
                    "harness": "claude", "sid": "sid-A", "cwd": str(self.cwd), "transcript": "", "attempts": 0}
            runner.write_item(item)
            bundle = mock.Mock(empty=False, cursors=[], unread=[], related=[], exchange=None)
            failure = runner.RunnerFailure("no artifact root for the worker's files: TimeoutExpired")
            with mock.patch.object(runner, "assemble", return_value=bundle), \
                 mock.patch.object(runner, "artifact_root_for", side_effect=failure), \
                 mock.patch.object(runner, "dispatch_worker") as dispatch, \
                 mock.patch.object(runner, "advance_watermarks") as advance:
                runner.process_item(item)
            self.assertEqual(self.item(item["id"])["status"], "failed")
            dispatch.assert_not_called()
            advance.assert_not_called()
            self.assertIsNone(self.watermark("sid-A"))
            self.assertEqual(st.read_latest_card(st.session_seat(seat, "claude", "sid-A"))["body"],
                             "진행 중인 일: 시간 초과 시험")
            self.assertEqual(len(self.notices()), 1)
            self.assertIn("TimeoutExpired", self.notices()[0])

    def test_a_symlinked_folder_is_refused(self):
        self.transcript("sid-A", [claude_row("user", "무언가 결정했다")])
        elsewhere = self.iso.root / "elsewhere"
        elsewhere.mkdir()
        (self.cwd / ".agent_reports" / ".runtime").mkdir(parents=True)
        os.symlink(elsewhere, self.cwd / ".agent_reports" / ".runtime" / "session-tidy")
        qid = self.enqueue("sid-A")
        item = self.wait_status(qid, "failed")
        self.assertIn("symlink", item["error"])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_pruning_a_finished_entry_removes_only_its_own_folder(self):
        mine, other = self.folder("tidy-old"), self.folder("tidy-keep")
        for path in (mine, other):
            path.mkdir(parents=True)
            (path / "actions.json").write_text("{}", encoding="utf-8")
        runner.remove_exchange(str(self.cwd))                        # not a worker folder: left alone
        runner.remove_exchange(str(mine))
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())


class TailFirstRunTest(RunnerCase):
    """A long record: the recent decision is in the first input, later tidies work backwards."""

    def big(self, rows=1500):
        texts = [f"기록 {i:05d} " + "나" * 700 for i in range(rows)] + ["최근 결정 마커"]
        return self.transcript("sid-A", [claude_row("user" if i % 2 == 0 else "assistant", t)
                                         for i, t in enumerate(texts)])

    def test_the_first_input_holds_the_recent_decision_and_the_line_says_part_was_not_read(self):
        path = self.big()
        self.assertGreater(path.stat().st_size, 1024 * 1024)
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        first = self.saved_input(1)["sessions"][0]
        self.assertIn("최근 결정 마커", first["text"])
        self.assertNotIn("기록 00000", first["text"])
        self.assertEqual((first["unit"], first["total"], first["cursor_to"]), ("byte", path.stat().st_size, path.stat().st_size))
        (note,) = self.notices()
        self.assertRegex(note, r"일부만 읽음\(byte [\d,]+–[\d,]+ / 전체 [\d,]+\) — 앞부분은 다음 정리에서 계속")
        self.assertRegex(note, rf"— 되돌리기: mem tidy-undo {qid}$")          # the way back is still last
        mark = self.watermark("sid-A")
        self.assertEqual(mark["pending"], [[0, first["cursor_from"]]])         # the old front is still unread

    def test_later_tidies_read_further_back_without_overlap_until_nothing_is_left(self):
        path = self.big(rows=600)
        ends = []
        for round_ in range(1, 9):
            qid = self.enqueue("sid-A")
            self.wait_status(qid, "notified")
            data = self.saved_input(round_)["sessions"][0]
            ends.append((data["cursor_from"], data["cursor_to"]))
            if not data["pending_after"]:
                break
        else:
            self.fail("the record was never fully read")
        self.assertGreater(len(ends), 2)
        for (a_from, _a_to), (_b_from, b_to) in zip(ends, ends[1:]):
            self.assertEqual(b_to, a_from)                                       # each run ends where the last began
        self.assertEqual(ends[-1][0], 0)
        self.assertEqual(self.watermark("sid-A")["pending"], [])
        # and now there is truly nothing: no worker, and the old "nothing new" wording is honest
        before = len(self.calls())
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(self.notices()[-1], "[정리] 새로 정리할 대화가 없습니다.")
        self.assertTrue(all("일부만 읽음" not in n for n in self.notices()[-1:]))

    def test_an_append_is_read_first_and_the_older_front_is_not_overwritten(self):
        path = self.big(rows=700)
        first = self.enqueue("sid-A")
        self.wait_status(first, "notified")
        backlog = self.watermark("sid-A")["pending"]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(claude_row("user", "그 뒤에 이어진 결정"), ensure_ascii=False) + "\n")
        second = self.enqueue("sid-A")
        self.wait_status(second, "notified")
        data = self.saved_input(2)["sessions"][0]
        self.assertEqual(data["text"], "[user] 그 뒤에 이어진 결정")
        self.assertEqual(self.watermark("sid-A")["pending"], backlog)            # the front waits where it was

    def test_a_part_with_nothing_to_tidy_never_reads_as_nothing_new(self):
        rows = [claude_row("user", "처음 대화")] + [
            {"type": "user", "cwd": "/w", "isMeta": True, "message": {"role": "user", "content": "x" * 800}}
            for _ in range(900)]
        self.transcript("sid-A", rows)
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        self.assertEqual(self.calls() and self.saved_input(1)["sessions"][0]["text"], "[user] 처음 대화")
        self.assertNotIn("새로 정리할 대화가 없습니다", " ".join(self.notices()))

    def test_proposals_beyond_the_batch_limit_keep_their_conversation_unread(self):
        self.transcript("sid-A", [claude_row("user", "결정이 많은 대화")])
        many = [{"kind": "add", "type": "decision", "tier": "working", "body": f"Distinct decision number {i} about topic {i}."}
                for i in range(12)]
        self.scenario([{"mode": "ok", "actions": many}])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        self.assertEqual(self.watermark("sid-A"), None)                           # not marked read
        (note,) = self.notices()
        self.assertIn("상한을 넘은 제안은 다음 정리에서 다시 봅니다", note)
        self.assertRegex(note, rf"— 되돌리기: mem tidy-undo {qid}$")


class MemoryRefsRunTest(RunnerCase):
    """Card layer B: what the batch really wrote plus the related records, handed over once."""

    def setUp(self):
        super().setUp()
        self.transcript("sid-A", [claude_row("user", "참고 기억 시험 결정")])
        self.mem("add", "durable", "lesson", "An existing lesson about the storage format.")
        self.existing = next(iter(self.records()))
        self.cli("card", "--harness", "claude", "--session-id", "sid-A", "--text", "진행 중: 시험")

    def card(self):
        return st.read_json(next((self.state / "cards").glob("*.json")))

    def start(self, sid, event="start"):
        return self.cli("hook", "--harness", "claude", "--event", event, "--session-id", sid).stdout

    def confirm_successor(self, predecessor="sid-A", successor="sid-B"):
        with self.iso.patched_environ(self.env()):
            seat = st.resolve_seat("claude", str(self.cwd), os.environ, sid=successor)
            seat = st.session_seat(seat, "claude", successor)
            now = st.now_epoch()
            with st.session_state_lock(seat):
                st.record_event(seat, "claude", successor, "start", source="clear",
                                cwd=str(self.cwd), now=now)
                st._append_ledger_entry(seat, {
                    "ts": now + 0.001, "harness": "claude", "sid": successor,
                    "event": "handover", "from": {"harness": "claude", "sid": predecessor},
                    "source": "clear",
                })

    def test_the_actual_new_ids_and_the_related_existing_ones_become_the_list(self):
        self.scenario([{"mode": "ok", "related": [self.existing, "not-a-record", self.existing], "actions": [
            {"kind": "add", "type": "decision", "tier": "durable", "body": "Brand new decision about storage."}]}])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        refs = self.card()["memory_refs"]
        self.assertEqual((refs["revision"], refs["status"], refs["batch"], refs["source_generation"]),
                         (1, "applied", qid, 1))
        new_ids = [r["id"] for r in refs["refs"] if r["kind"] == "새"]
        self.assertEqual(len(new_ids), 1)
        self.assertEqual([r["id"] for r in refs["refs"]], new_ids + [self.existing])    # new first, related after
        self.assertEqual(refs["refs"][-1]["kind"], "관련")
        self.assertEqual(refs["result_path"], str(self.state / "runs" / qid / "result.json"))
        # the card itself did not move, and its text file carries the section
        card = self.card()
        self.assertEqual((card["generation"], card["body"]), (1, "진행 중: 시험"))
        card_seat = st.session_seat(st.seat_from_dict(card["seat"]),
                                    card["author"]["harness"], card["author"]["sid"])
        card_text = self.state / "cards" / f"{st.card_key(card_seat)}.md"
        self.assertIn("[참고할 기억]", card_text.read_text(encoding="utf-8"))

    def test_a_list_is_cut_to_eight_records_and_twelve_hundred_bytes(self):
        actions = [{"kind": "add", "type": "decision", "tier": "working",
                    "body": f"Decision {i}: " + "wordy detail " * 20 + f"unique-{i}", "headline": f"headline {i} " + "긴" * 60}
                   for i in range(10)]
        self.scenario([{"mode": "ok", "actions": actions}])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        refs = self.card()["memory_refs"]
        self.assertLessEqual(len(refs["refs"]), 8)
        self.assertLessEqual(sum(len(f"- {r['id']} [{r['kind']}] {r['headline']}".encode("utf-8")) + 1 for r in refs["refs"]), 1200)
        self.assertGreater(refs["more"], 0)
        self.confirm_successor()
        shown = self.start("sid-B")
        self.assertRegex(shown, rf"외 {refs['more'] + 10 - len(refs['refs'])}건|외 \d+건")
        self.assertIn(refs["result_path"], shown)

    def test_a_tidy_that_ends_after_the_new_session_started_still_reaches_it_once(self):
        self.scenario([{"mode": "ok", "gate": self.gate(), "actions": [
            {"kind": "add", "type": "decision", "tier": "durable", "body": "A decision that lands late."}]}])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "waiting-worker")
        self.confirm_successor()
        first = self.start("sid-B")                                                # cleared before the tidy ended
        self.assertIn("진행 중: 시험", first)
        self.assertNotIn("[참고할 기억]", first)
        self.open_gate()
        self.wait_status(qid, "notified")
        second = self.start("sid-B", "prompt")                                     # layer A was taken; B is new
        self.assertIn("[참고할 기억]", second)
        self.assertNotIn("진행 중: 시험", second)                                    # the card is not handed out again
        self.assertIn("[정리 결과]", second)
        self.assertEqual(self.start("sid-B", "prompt"), "")                        # and neither is B
        self.assertEqual(self.start("sid-C"), "")                                  # nor to a later session

    def test_the_writing_session_does_not_use_layer_b_up_before_the_new_session_can_see_it(self):
        self.scenario([{"mode": "ok", "actions": [
            {"kind": "add", "type": "decision", "tier": "durable", "body": "Handed to the successor only."}]}])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        self.confirm_successor()
        shown = self.start("sid-A", "prompt")
        self.assertIn("[정리 결과]", shown)
        self.assertNotIn("[참고할 기억]", shown)
        self.assertIn("[참고할 기억]", self.start("sid-B"))                        # the successor gets A and B together
        self.assertEqual(self.start("sid-D"), "")                                  # B and A were handed over once

    def test_a_failure_says_so_in_the_same_section_and_names_no_list(self):
        self.scenario([{"mode": "bad-json"}])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "failed")
        refs = self.card()["memory_refs"]
        self.assertEqual((refs["status"], refs["refs"]), ("failed", []))
        self.confirm_successor()
        shown = self.start("sid-B")
        self.assertIn("정돈을 끝내지 못함", shown)
        self.assertIn("진행 중: 시험", shown)

    def test_a_later_batch_replaces_the_list_and_a_late_old_batch_does_not_touch_the_card(self):
        self.scenario([{"mode": "ok", "actions": [
            {"kind": "add", "type": "decision", "tier": "durable", "body": "First batch decision."}]}])
        first = self.enqueue("sid-A")
        self.wait_status(first, "notified")
        self.cli("card", "--harness", "claude", "--session-id", "sid-A", "--text", "진행 중: 둘째 카드")
        card = self.card()
        self.assertEqual((card["generation"], card["memory_refs"]["revision"]), (2, 1))   # B outlives the new card
        self.transcript("sid-A", [claude_row("user", "참고 기억 시험 결정"), claude_row("user", "둘째 배치 결정")])
        self.scenario([{"mode": "ok", "actions": [
            {"kind": "add", "type": "decision", "tier": "durable", "body": "Second batch decision."}]}])
        second = self.enqueue("sid-A")
        self.wait_status(second, "notified")
        refs = self.card()["memory_refs"]
        self.assertEqual((refs["revision"], refs["batch"], refs["source_generation"]), (2, second, 2))
        self.assertEqual(self.card()["body"], "진행 중: 둘째 카드")


class PartialApplyTest(RunnerCase):
    """A batch that stopped after some writes must say so and keep its undo command."""

    def run_item(self, apply_stub, **patches):
        patches.setdefault("advance_watermarks", runner.advance_watermarks)
        with self.iso.patched_environ(self.env()):
            seat = st.resolve_seat("claude", str(self.cwd), os.environ)
            item = {"schema": runner.SCHEMA, "id": runner.new_id(), "created": runner._now(),
                    "status": "queued", "updated": runner._now(), "seat": seat.as_dict(),
                    "harness": "claude", "sid": "sid-A", "cwd": str(self.cwd), "transcript": "", "attempts": 0}
            runner.write_item(item)
            bundle = mock.Mock(empty=False, cursors=[], unread=[], related=[], exchange=self.cwd / "worker-folder")
            with mock.patch.object(runner, "assemble", return_value=bundle), \
                    mock.patch.object(runner, "prepare_exchange", return_value=bundle.exchange), \
                    mock.patch.object(runner, "dispatch_worker", return_value={"attempt_id": "att-x"}), \
                    mock.patch.object(runner, "wait_worker"), \
                    mock.patch.object(runner, "validate_actions"), \
                    mock.patch.object(runner, "apply_actions", side_effect=apply_stub), \
                    mock.patch.multiple(runner, **patches):
                runner.process_item(item)
            return item["id"], self.item(item["id"])

    @staticmethod
    def journal(run_dir, done, intent=1):
        ops = ([{"op": "add", "state": "done", "id": f"r{n}"} for n in range(done)]
               + [{"op": "add", "state": "intent"}] * intent + [{"op": "add", "state": "dropped"}])
        st.atomic_write_json(run_dir / "undo.json", {"ops": ops})

    def test_an_applier_that_stopped_halfway_is_reported_as_partial_with_the_undo_command(self):
        def stopped(item, run_dir, bundle):
            self.journal(run_dir, 2)
            raise runner.RunnerFailure(f"묶음 {run_dir.name}: 부분 적용 2건 · RuntimeError: boom "
                                       f"— 되돌리기: mem tidy-undo {run_dir.name}")
        qid, item = self.run_item(stopped)
        self.assertEqual((item["status"], item["applied"]), ("failed", [2, 1]))
        notes = self.notices()
        self.assertEqual(len(notes), 1, notes)
        self.assertRegex(notes[0], rf"^\[정리\] 기억 정리가 중간에 멈췄습니다\(2건 반영, 1건은 반영 여부 불명\)\. "
                                   rf".*RuntimeError: boom\) — 되돌리기: mem tidy-undo {qid}$")
        self.assertNotIn("그대로", notes[0])
        self.assertEqual(notes[0].count("mem tidy-undo"), 1)

    def test_a_failure_after_a_full_apply_still_names_the_writes_and_the_undo_command(self):
        def applied(item, run_dir, bundle):
            self.journal(run_dir, 1, intent=0)
            return f"[tidy] 묶음 {run_dir.name}: 추가 1 — 되돌리기: mem tidy-undo {run_dir.name}"
        qid, item = self.run_item(applied, advance_watermarks=mock.Mock(side_effect=OSError("disk full")))
        self.assertEqual((item["status"], item["applied"]), ("failed", [1, 0]))
        self.assertRegex(self.notices()[0], rf"^\[정리\] 기억 정리가 중간에 멈췄습니다\(1건 반영\)\. .*"
                                            rf"— 되돌리기: mem tidy-undo {qid}$")

    def test_a_write_that_landed_before_the_journal_could_mark_it_finished_is_not_called_untouched(self):
        def unmarked(item, run_dir, bundle):
            self.journal(run_dir, 0, intent=1)        # the store changed, the "done" flush did not
            raise runner.RunnerFailure("apply stopped: OSError: journal write failed")
        qid, item = self.run_item(unmarked)
        self.assertEqual(item["applied"], [0, 1])
        note = self.notices()[0]
        self.assertRegex(note, rf"^\[정리\] 기억 정리가 중간에 멈췄습니다\(0건 반영, 1건은 반영 여부 불명\)\. .*"
                               rf"— 되돌리기: mem tidy-undo {qid}$")
        self.assertNotIn("그대로", note)

    def test_an_unreadable_journal_is_reported_as_unknown_with_the_undo_command(self):
        def garbled(item, run_dir, bundle):
            (run_dir / "undo.json").write_text("{not json", encoding="utf-8")
            raise runner.RunnerFailure("apply stopped")
        qid, _item = self.run_item(garbled)
        note = self.notices()[0]
        self.assertRegex(note, rf"^\[정리\] 기억 정리가 중간에 멈췄습니다\(반영 여부를 확인하지 못했습니다\)\. .*"
                               rf"— 되돌리기: mem tidy-undo {qid}$")

    def test_a_failure_before_any_write_still_says_memory_is_untouched(self):
        def refused(item, run_dir, bundle):
            raise runner.RunnerFailure("apply did not run: boom")
        _qid, item = self.run_item(refused)
        self.assertEqual(item["applied"], [0, 0])
        self.assertRegex(self.notices()[0], r"^\[정리\] 기억 정리를 끝내지 못했습니다\. 카드와 기존 기억은 그대로")


class GovernorTest(RunnerCase):

    def test_governor_refusals_are_waited_out_not_reported_as_failures(self):
        self.transcript("sid-A", [claude_row("user", "자리가 날 때까지 기다린다")])
        self.scenario([{"mode": "governor"}, {"mode": "governor"}, {"mode": "ok"}])
        qid = self.enqueue("sid-A")
        self.wait_status(qid, "notified")
        self.assertEqual([c["mode"] for c in self.calls()], ["governor", "governor", "ok"])
        self.assertEqual(self.item(qid)["result"], "applied")
        self.assertEqual(len(self.notices()), 1)

    def test_a_governor_that_never_frees_a_seat_ends_after_the_wait_limit_with_one_line(self):
        self.transcript("sid-A", [claude_row("user", "끝내 자리가 안 난다")])
        self.scenario([], default={"mode": "governor"})
        qid = self.enqueue("sid-A", HEARTING_TIDY_GOVERNOR_WAIT="0.4")
        item = self.wait_status(qid, "failed")
        self.assertIn("no free worker seat", item["error"])
        self.assertGreaterEqual(len(self.calls()), 2)
        self.assertIsNone(self.watermark("sid-A"))
        self.assertEqual(len(self.notices()), 1)


class LockWaitTest(RunnerCase):

    def hold_lock(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.state / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return fd

    def test_a_runner_that_waits_too_long_leaves_its_entry_queued_and_the_next_tidy_finishes_it(self):
        import fcntl
        self.transcript("sid-A", [claude_row("user", "잠금 때문에 밀린 결정")])
        fd = self.hold_lock()
        first = self.enqueue("sid-A", HEARTING_TIDY_LOCK_WAIT="1.0")
        pid = self.runner_pid(first)
        self.wait_for(lambda: not alive(pid), "the waiting runner to give up")
        self.assertEqual(self.item(first)["status"], "queued")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.notices(), [])
        fcntl.flock(fd, fcntl.LOCK_UN)
        second = self.enqueue("sid-A")          # the next normal reservation continues the queue
        self.wait_status(second, "notified")
        self.assertEqual(self.item(first)["status"], "notified")
        self.assertEqual(self.item(first)["result"], "applied")
        self.assertEqual(self.item(second)["result"], "nothing-new")
        self.assertEqual(len(self.calls()), 1)       # one worker, for the conversation both entries share


class HandoffTest(RunnerCase):

    def card(self, text="진행 중: 인계 시험\n다음 할 일: 받아서 계속"):
        result = self.cli("card", "--harness", "claude", "--session-id", "sid-A", "--text", text)
        self.assertEqual(result.returncode, 0, result.stderr)
        return text

    def test_the_typed_verdict_and_the_exit_code_of_peer_steward_are_reported_as_they_are(self):
        text = self.card()
        for verdict, code in (("true", 0), ("failed", 1), ("queued", 3), ("unverified", 5)):
            with self.subTest(verdict=verdict):
                result = self.cli("handoff", "peer-pane", "--harness", "claude", "--session-id", "sid-A",
                                  FAKE_PEER_VERDICT=verdict)
                self.assertEqual(result.returncode, code, result.stderr)
                lines = result.stdout.strip().splitlines()
                self.assertEqual(len(lines), 1, result.stdout)
                self.assertRegex(lines[0], rf"^prompted={verdict} target=peer-pane .* exit={code}$")
        sent = [json.loads(x) for x in (self.fake / "steward.jsonl").read_text().splitlines()]
        self.assertEqual(len(sent), 4)
        for row in sent:       # only ever `prompt <target> --body-file <card>`; the body is the card text
            self.assertEqual(row["argv"][:2], ["prompt", "peer-pane"])
            self.assertEqual(row["argv"][2], "--body-file")
            self.assertEqual(len(row["argv"]), 4)
            self.assertEqual(row["body"].strip(), text.strip())
            self.assertNotIn("[첫 답]", row["body"])
        self.assertEqual(list((self.state / "handoff").glob("*")), [])      # the body file does not linger

    def test_large_compact_card_reaches_peer_body_file_without_truncation(self):
        summary = "최종 요약\n" + "세부 내용 " * 3000
        context = {"trigger": "auto", "identity": "handoff-compact", "transcript": "fixture.jsonl"}
        with self.iso.patched_environ(self.iso.env({"HERDR_PANE_ID": PANE})):
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            with st.seat_lock(seat.key):
                card = st.write_card(seat, "claude", "sid-A", summary, cwd=str(self.cwd),
                                     prompt_seq=st.read_prompt_seq(seat), origin="runtime-compact",
                                     compact_context=context)
        self.assertGreater(len(card["body"].encode("utf-8")), st.CARD_BODY_MAX_BYTES)
        result = self.cli("handoff", "peer-pane", "--harness", "claude", "--session-id", "sid-A",
                          FAKE_PEER_VERDICT="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = [json.loads(x) for x in (self.fake / "steward.jsonl").read_text().splitlines()]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["body"], card["body"].rstrip() + "\n")
        self.assertEqual(list((self.state / "handoff").glob("*")), [])

    def test_a_handed_off_card_is_not_continued_in_this_window(self):
        self.card()
        result = self.cli("handoff", "peer-pane", "--harness", "claude", "--session-id", "sid-A",
                          FAKE_PEER_VERDICT="failed")
        self.assertEqual(result.returncode, 1, result.stderr)
        card = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertFalse(card.get("handed_off", False))       # failed delivery retains the card
        self.card("진행 중: 새 카드")
        card = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertNotIn("handed_off", card)                 # a newer card starts unmarked

    def test_without_a_card_nothing_is_sent(self):
        result = self.cli("handoff", "peer-pane", "--harness", "claude", "--session-id", "sid-A")
        self.assertEqual((result.returncode, result.stdout.strip()), (1, "prompted=false reason=no-card"))
        self.assertFalse((self.fake / "steward.jsonl").exists())


class TestHooksAreInertOutsideATestRoot(RunnerCase):

    def test_overrides_need_a_test_root_that_contains_the_state_folder_and_the_executable(self):
        var = "HEARTING_TIDY_WORKER_CMD"

        outside = Path(self.iso.root.parent) / "not-under-the-root.py"
        with self.iso.patched_environ({var: str(self.worker_cmd), "HEARTING_TIDY_POLL": "0.01"}):
            self.assertIsNone(runner.injected_executable(var))           # no test root named
            self.assertEqual(runner.tunable("POLL"), runner.TUNABLES["POLL"])
        with self.iso.patched_environ({var: str(self.worker_cmd), "HEARTING_TIDY_POLL": "0.01",
                                       "HEARTING_TIDY_TEST_ROOT": str(self.iso.root)}):
            self.assertEqual(runner.injected_executable(var), self.worker_cmd.resolve())
            self.assertEqual(runner.tunable("POLL"), 0.01)
        with self.iso.patched_environ({var: str(self.worker_cmd), "HEARTING_TIDY_POLL": "0.01",
                                       "HEARTING_TIDY_TEST_ROOT": str(self.iso.root / "elsewhere")}):
            self.assertIsNone(runner.injected_executable(var))           # state folder is not inside it
            self.assertEqual(runner.tunable("POLL"), runner.TUNABLES["POLL"])
        with self.iso.patched_environ({var: str(outside), "HEARTING_TIDY_TEST_ROOT": str(self.iso.root)}):
            self.assertIsNone(runner.injected_executable(var))           # executable is not inside it

    def test_the_real_launch_goes_through_the_adapters_checked_wrapper(self):
        item = {"harness": "codex", "cwd": str(self.cwd), "id": "tidy-x"}
        # git refuses a checkout owned by another user under the isolated HOME, so trust it explicitly
        trust = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
        with self.iso.patched_environ(trust):
            for harness, wrapper in (("claude", "claude"), ("codex", "codex"), ("opencode", "claude")):
                item["harness"] = harness
                command = runner.launch_command(item, Path("/p"), "slug")
                self.assertEqual(command[1], str(ROOT / "adapters" / wrapper / "bin" / "dispatch-headless.py"))
                self.assertIn("--worker-type", command)
                self.assertNotIn("--route-file", command)


if __name__ == "__main__":
    unittest.main()
