#!/usr/bin/env python3
"""D-86: a session is shown each candidate record once.

Every check runs the CLI (or a bridge that calls it) against a throwaway store
with the automatic exchange off and no remote. Nothing here reads or writes the
real store, settings, or event logs.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import MEMORY_DIR, git


MEM = MEMORY_DIR / "mem.py"
ROOT = MEMORY_DIR.parents[1]
CODEX_START = ROOT / "adapters" / "codex" / "hooks" / "sessionstart-lifecycle.py"
OPENCODE_PLUGIN = ROOT / "adapters" / "opencode" / "plugins" / "hearting-guards.js"
ID_LINE = re.compile(r"^- \[[^\]]+\] (\S+): ", re.M)
QUERY = "gadget"


def ids_in(text):
    return ID_LINE.findall(text)


class SeenTestCase(unittest.TestCase):
    """One store with eight records that all match ``gadget``."""

    RECORDS = 8

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory(prefix="seen-", dir="/var/tmp")
        cls.root = Path(cls.tempdir.name)
        cls.fake_bin = cls.root / "bin"
        cls.fake_bin.mkdir()
        cls.real_python = sys.executable
        compact_bridge = cls.fake_bin / "python3"
        compact_bridge.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in *worker-state-hook.py*compact-after*--harness*opencode*) exit 0 ;; esac\n"
            f"exec {shlex.quote(cls.real_python)} \"$@\"\n", encoding="utf-8")
        compact_bridge.chmod(0o700)
        cls.project = cls.root / "project"
        cls.project.mkdir()
        git(cls.project, "init")
        git(cls.project, "remote", "add", "origin", "https://example.invalid/team/project.git")
        (cls.root / "home").mkdir()
        config = cls.root / "config" / "hearting"
        config.mkdir(parents=True)
        (config / "memory-sync.json").write_text('{"enabled": false}\n')
        cls.store = cls.root / "store"
        cls.state = cls.root / "state"
        cls.seen_dir = cls.root / "state" / "agent-memory" / "candidate-seen"
        for index in range(cls.RECORDS):
            result = cls.mem("add", "durable", "lesson",
                             f"gadget note {index} explains the widget cache layout",
                             "--headline", f"gadget headline {index} widget cache")
            assert result.returncode == 0, result.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    @classmethod
    def environment(cls, **extra):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("MEM_", "AGENT_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({
            "HOME": str(cls.root / "home"),
            "XDG_CONFIG_HOME": str(cls.root / "config"),
            "XDG_STATE_HOME": str(cls.state),
            "MEM_STORE": str(cls.store),
            "MEM_PROJECTS": str(cls.root / "projects"),
            "MEM_EXCHANGE_AUTO": "0",
            "MEM_RECALL_EVENTS": str(cls.root / "events.jsonl"),
            "MEM_RECALL_RECEIPTS": str(cls.state / "agent-memory" / "recall-opportunities"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": str(cls.fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        })
        env.update(extra)
        return env

    @classmethod
    def mem(cls, *args, stdin=None, env=None, timeout=60):
        return subprocess.run(
            [sys.executable, str(MEM), *args], cwd=cls.project,
            env=cls.environment(**(env or {})), input=stdin,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)

    def candidates(self, session, query=QUERY, *extra, env=None):
        result = self.mem("candidates", query, "--session-id", session,
                          "--runtime", "test", *extra, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def events(self):
        path = self.root / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def receipt(self, session):
        import hashlib
        key = hashlib.sha256(
            b"memory-recall-opportunity-v1\0" + session.encode()).hexdigest()
        path = self.state / "agent-memory" / "recall-opportunities" / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def sid(self, label):
        return f"{self.id().rsplit('.', 1)[-1]}-{label}"


class DisplayHistoryTest(SeenTestCase):

    def test_a_second_probe_of_the_same_session_drops_what_it_already_showed(self):
        session = self.sid("s")
        first = self.candidates(session)
        shown = ids_in(first)
        self.assertEqual(len(shown), 6)
        stamp = self.receipt(session)["created_at_ns"]
        time.sleep(0.01)
        second = self.candidates(session)
        # The top six were all shown; the seventh and eighth are NOT pulled in.
        self.assertEqual(second, "")
        self.assertEqual(len(second.encode()), 0)
        receipt = self.receipt(session)
        self.assertGreater(receipt["created_at_ns"], stamp, "the receipt is still written")
        self.assertEqual(receipt["result_ids"], [])

    def test_hook_mode_prints_nothing_when_everything_was_shown(self):
        session = self.sid("s")
        first = self.mem("candidates", QUERY, "--session-id", session, "--hook")
        self.assertIn("additionalContext", first.stdout)
        second = self.mem("candidates", QUERY, "--session-id", session, "--hook")
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stdout, "")

    def test_another_session_sees_the_records_again(self):
        self.assertEqual(len(ids_in(self.candidates(self.sid("one")))), 6)
        self.assertEqual(len(ids_in(self.candidates(self.sid("two")))), 6)

    def test_no_session_means_no_deduplication(self):
        for placeholder in ("memory-prompt-hook", "opencode-plugin", "codex-hook", ""):
            with self.subTest(placeholder=placeholder):
                first = self.candidates(placeholder)
                second = self.candidates(placeholder)
                self.assertEqual(len(ids_in(first)), 6)
                self.assertEqual(first, second)
        for placeholder in ("memory-prompt-hook", "opencode-plugin", "codex-hook", ""):
            self.assertEqual(
                list(self.seen_dir.glob(f"{session_digest(placeholder)}.*")), [],
                "a placeholder session must never own a history file")

    def test_part_of_the_top_results_seen_only_the_rest_is_shown_and_nothing_pads(self):
        session = self.sid("s")
        head = ids_in(self.candidates(session, QUERY, "--limit", "3"))
        self.assertEqual(len(head), 3)
        rest = ids_in(self.candidates(session, QUERY, "--limit", "6"))
        self.assertEqual(len(rest), 3, "at most 6 - seen ids, never padded from rank 7+")
        self.assertFalse(set(head) & set(rest))
        self.assertEqual(self.candidates(session, QUERY, "--limit", "6"), "")

    def test_the_event_records_what_was_output_and_how_many_were_dropped(self):
        session = self.sid("s")
        prompt = f"{QUERY} zzuniqueprompt"
        first = self.candidates(session, prompt)
        second = self.candidates(session, prompt)
        mine = [e for e in self.events() if e.get("sid_sha256") and
                e["event"] == "candidate-probe"][-2:]
        self.assertEqual(mine[0]["result_ids"], ids_in(first))
        self.assertEqual(mine[0]["output_utf8_bytes"], len(first.rstrip("\n").encode()))
        self.assertEqual(mine[0]["suppressed_count"], 0)
        self.assertEqual(mine[1]["result_ids"], [])
        self.assertEqual(mine[1]["result_count"], 0)
        self.assertEqual(mine[1]["output_utf8_bytes"], len(second.encode()))
        self.assertEqual(mine[1]["suppressed_count"], 6)
        self.assertNotIn("zzuniqueprompt", json.dumps(mine), "no raw prompt in the event")

    def test_receipts_readers_only_need_a_bounded_id_list(self):
        session = self.sid("s")
        self.candidates(session)
        receipt = self.receipt(session)
        self.assertLessEqual(len(receipt["result_ids"]), 6)
        self.assertEqual(receipt["result_count"], len(receipt["result_ids"]))
        self.assertEqual(receipt["source"], "candidate-probe")

    def test_a_deferred_probe_commits_only_when_the_caller_applies_it(self):
        session = self.sid("deferred")
        before = len(self.events())
        result = self.mem("candidates", QUERY, "--session-id", session,
                          "--runtime", "test", "--hook",
                          env={"MEM_CANDIDATE_DEFER_COMMIT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        pending = value["deferredCandidateReceipt"]
        shown = ids_in(value["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(len(shown), 6)
        self.assertIsNone(self.receipt(session), "the probe wrote its receipt before delivery")
        self.assertEqual(len(self.events()), before, "the probe wrote its event before delivery")
        again = self.mem("candidates", QUERY, "--session-id", session, "--runtime", "test")
        self.assertEqual(ids_in(again.stdout), shown, "uncommitted IDs must stay eligible")
        applied = self.mem("_apply-candidate-receipt", stdin=json.dumps(pending))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout), {"applied": True})
        receipt = self.receipt(session)
        self.assertEqual(receipt["result_ids"], shown)
        self.assertEqual(ids_in(self.candidates(session)), [], "the applied IDs are now consumed")

    def test_concurrent_probes_of_one_session_never_share_an_id(self):
        session = self.sid("s")
        procs = [subprocess.Popen(
            [sys.executable, str(MEM), "candidates", QUERY, "--limit", "2",
             "--session-id", session, "--runtime", "test"],
            cwd=self.project, env=self.environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(5)]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            outputs.append(out)
        shown = [item for out in outputs for item in ids_in(out)]
        self.assertEqual(len(shown), len(set(shown)), outputs)
        self.assertEqual(len(shown), 2, "two ranked slots, shown exactly once in total")

    def test_files_are_private_and_bounded_by_id_count_and_age(self):
        session = self.sid("s")
        self.candidates(session)
        files = list(self.seen_dir.glob("*.json"))
        self.assertTrue(files)
        self.assertEqual(oct(self.seen_dir.stat().st_mode & 0o777), "0o700")
        for path in files:
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        mem = load_mem(self)
        digest, name = mem._seen_names(session, mem.project_key(self.project))
        state = mem._seen_load(name, time.time())
        stale = {key: time.time() - 25 * 3600 for key in state["ids"]}
        state["ids"] = stale
        mem._seen_save(name, digest, "p", state, time.time())
        self.assertEqual(len(ids_in(self.candidates(session))), 6, "past the 24h TTL")
        # More than 512 ids: the oldest are dropped, the newest kept.
        now = time.time()
        big = {"ids": {f"id-{i}": now - 1000 + i for i in range(700)},
               "body_ids": {}, "saved_bytes": 0, "body_bytes": 0}
        mem._seen_save(name, digest, "p", big, now)
        loaded = mem._seen_load(name, now)
        self.assertEqual(len(loaded["ids"]), 512)
        self.assertIn("id-699", loaded["ids"])
        self.assertNotIn("id-0", loaded["ids"])

    def test_a_broken_or_unusable_history_fails_open(self):
        session = self.sid("s")
        self.candidates(session)
        for path in self.seen_dir.glob(f"*.json"):
            if session_digest(session) in path.name:
                path.write_text("{not json")
        self.assertEqual(len(ids_in(self.candidates(session))), 6)
        blocker = self.root / "not-a-directory"
        blocker.write_text("x")
        env = {"MEM_CANDIDATE_SEEN": str(blocker / "seen")}
        one = self.candidates(self.sid("blocked"), env=env)
        two = self.candidates(self.sid("blocked"), env=env)
        self.assertEqual(len(ids_in(one)), 6)
        self.assertEqual(one, two, "no history available: candidates still show")


def session_digest(session):
    import hashlib
    return hashlib.sha256(b"memory-candidate-seen-v1\0" + session.encode()).hexdigest()


_MEM_MODULES = {}


def load_mem(case):
    """Import mem.py once per test class, pointed at that class's throwaway paths."""
    module = _MEM_MODULES.get(case.root)
    if module is None:
        saved = dict(os.environ)
        try:
            for key in list(os.environ):
                if key.startswith(("MEM_", "AGENT_")):
                    del os.environ[key]
            os.environ.update(case.environment())
            spec = importlib.util.spec_from_file_location(
                f"mem_seen_under_test_{len(_MEM_MODULES)}", MEM)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            _MEM_MODULES[case.root] = module
        finally:
            os.environ.clear()
            os.environ.update(saved)
    return module


class ResetSignalTest(SeenTestCase):

    def start(self, source, session, **extra):
        payload = json.dumps({"hook_event_name": "SessionStart", "source": source,
                              "session_id": session})
        result = self.mem("inject", "--hook", stdin=payload, **extra)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_claude_compact_and_clear_empty_the_history_but_resume_and_startup_do_not(self):
        for source, forgets in (("compact", True), ("clear", True),
                                ("resume", False), ("startup", False)):
            with self.subTest(source=source):
                session = self.sid(source)
                self.assertEqual(len(ids_in(self.candidates(session))), 6)
                self.start(source, session)
                again = ids_in(self.candidates(session))
                self.assertEqual(len(again), 6 if forgets else 0)

    def test_a_signal_for_another_session_leaves_this_history_alone(self):
        session = self.sid("mine")
        self.candidates(session)
        self.start("compact", self.sid("other"))
        self.assertEqual(self.candidates(session), "")

    def test_inject_hook_without_stdin_never_waits_for_it(self):
        for stdin in (subprocess.DEVNULL, subprocess.PIPE):
            with self.subTest(stdin=stdin):
                began = time.monotonic()
                proc = subprocess.Popen(
                    [sys.executable, str(MEM), "inject", "--hook"], cwd=self.project,
                    env=self.environment(), stdin=stdin,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    # A pipe that is never written nor closed must not hold it up.
                    self.assertEqual(proc.wait(timeout=10), 0)
                finally:
                    if proc.stdin:
                        proc.stdin.close()
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                self.assertLess(time.monotonic() - began, 5)
        garbage = self.mem("inject", "--hook", stdin="not json at all")
        self.assertEqual(garbage.returncode, 0)

    def test_the_claude_session_start_command_passes_stdin_through_to_mem(self):
        settings = json.loads(
            (ROOT / "adapters" / "claude" / "settings.json").read_text())
        commands = [hook["command"]
                    for group in settings["hooks"]["SessionStart"]
                    for hook in group["hooks"]
                    if "mem.py" in hook["command"] and "inject --hook" in hook["command"]]
        self.assertEqual(len(commands), 1)
        command = commands[0].replace(
            '"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/tools/memory/mem.py"',
            f'"{MEM}"')
        self.assertIn(str(MEM), command)
        session = self.sid("real-command")
        self.candidates(session)
        payload = json.dumps({"source": "compact", "session_id": session})
        result = subprocess.run(["sh", "-c", command], input=payload, text=True,
                                cwd=self.project, env=self.environment(), timeout=60,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(ids_in(self.candidates(session))), 6)

    def test_the_hidden_reset_command_is_the_shared_helper(self):
        session = self.sid("s")
        self.candidates(session)
        result = self.mem("_seen-reset", "--session-id", session)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(ids_in(self.candidates(session))), 6)
        for placeholder in ("memory-prompt-hook", "codex-hook", ""):
            self.assertEqual(self.mem("_seen-reset", "--session-id", placeholder).returncode, 0)

    def test_codex_session_start_compact_and_clear_call_the_same_helper(self):
        for source, forgets in (("compact", True), ("clear", True),
                                ("startup", False), ("resume", False)):
            with self.subTest(source=source):
                session = self.sid(source)
                self.candidates(session)
                payload = json.dumps({"hook_event_name": "SessionStart", "source": source,
                                      "session_id": session, "cwd": str(self.project)})
                result = subprocess.run(
                    [sys.executable, str(CODEX_START)], input=payload, text=True,
                    env=self.environment(), cwd=self.project, timeout=60,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(ids_in(self.candidates(session))), 6 if forgets else 0)
        worker = self.sid("worker")
        self.candidates(worker)
        payload = json.dumps({"source": "compact", "session_id": worker,
                              "cwd": str(self.project)})
        subprocess.run([sys.executable, str(CODEX_START)], input=payload, text=True,
                       env=self.environment(AGENT_SESSION_ROLE="worker"), cwd=self.project,
                       timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(self.candidates(worker), "", "a worker session does not reset")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_opencode_session_compacted_calls_the_same_helper(self):
        session = self.sid("oc")
        other = self.sid("oc-other")
        self.candidates(session)
        self.candidates(other)
        script = f"""
process.env.AGENT_HOME = {json.dumps(str(ROOT))}
const mod = await import({json.dumps(str(OPENCODE_PLUGIN))})
const plugin = await mod.AgentHarnessGuards({{ directory: {json.dumps(str(self.project))}, worktree: {json.dumps(str(self.project))}, client: {{
  session: {{
    get: async (request) => ({{ data: {{ id: request.path.id, parentID: null }} }}),
    messages: async () => ({{ data: [] }}),
  }},
}} }})
await plugin.event({{ event: {{ type: "session.compacted", properties: {{ sessionID: {json.dumps(session)} }} }} }})
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script], cwd=self.project,
            env=self.environment(), text=True, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(ids_in(self.candidates(session))), 6)
        self.assertEqual(self.candidates(other), "", "only the compacted session resets")


class ShortBodyTest(SeenTestCase):
    """The optional body: off by default; at most one record, 600 chars, half the savings."""

    BODY = "가" * 900

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        result = cls.mem("add", "durable", "lesson", cls.BODY + " zebrafish",
                         "--headline", "zebrafish lead record")
        assert result.returncode == 0, result.stderr

    def run_probe(self, mem, session, query, *, body=True, gap=-1e9):
        """Call the probe in this process (so the score gate can be moved)."""
        mem.CANDIDATE_BODY_MIN_GAP = gap
        buffer = io.StringIO()
        saved_env = os.environ.get("MEM_CANDIDATE_BODY")
        saved_cwd = os.getcwd()
        try:
            if body:
                os.environ["MEM_CANDIDATE_BODY"] = "1"
            else:
                os.environ.pop("MEM_CANDIDATE_BODY", None)
            os.chdir(self.project)
            with contextlib.redirect_stdout(buffer):
                mem.candidates(query, runtime="test", session_id=session)
        finally:
            os.chdir(saved_cwd)
            if saved_env is None:
                os.environ.pop("MEM_CANDIDATE_BODY", None)
            else:
                os.environ["MEM_CANDIDATE_BODY"] = saved_env
        return buffer.getvalue().rstrip("\n")

    def loaded(self):
        return load_mem(self)

    def test_off_by_default_no_body_no_access_time_change(self):
        session = self.sid("s")
        before = self.last_accessed()
        self.candidates(session)
        text = self.candidates(session, "zebrafish")
        self.assertNotIn("  > ", text)
        self.assertEqual(before, self.last_accessed())

    def last_accessed(self):
        import sqlite3
        con = sqlite3.connect(self.store / "memory.db")
        try:
            return con.execute(
                "SELECT id,last_accessed FROM records ORDER BY id").fetchall()
        finally:
            con.close()

    def test_enabled_shows_one_bounded_body_once_and_within_half_the_savings(self):
        mem = self.loaded()
        session = self.sid("s")
        self.run_probe(mem, session, QUERY)            # show six
        first_again = self.run_probe(mem, session, QUERY)  # drop six: savings accrue
        self.assertEqual(first_again, "")
        lead = self.run_probe(mem, session, "zebrafish")
        self.assertEqual(lead.count("  > "), 1)
        body_line = next(l for l in lead.splitlines() if l.startswith("  > "))
        self.assertLessEqual(len(body_line) - 4, 600)
        self.assertLessEqual(len(lead.encode()), 1200)
        digest, name = mem._seen_names(session, mem.project_key(self.project))
        state = mem._seen_load(name, time.time())
        self.assertGreater(state["saved_bytes"], 0)
        self.assertLessEqual(state["body_bytes"], state["saved_bytes"] // 2)
        self.assertEqual(len(state["body_ids"]), 1)
        # The record is now seen: the same query shows nothing, so no second body.
        self.assertEqual(self.run_probe(mem, session, "zebrafish"), "")
        # A body is shown once per session even if the id were forgotten.
        rid = next(iter(state["body_ids"]))
        state["ids"].pop(rid)
        mem._seen_save(name, digest, "p", state, time.time())
        again = self.run_probe(mem, session, "zebrafish")
        self.assertIn(rid, again)
        self.assertNotIn("  > ", again)

    def test_no_body_before_anything_was_saved_or_when_the_lead_is_not_clear(self):
        mem = self.loaded()
        fresh = self.run_probe(mem, self.sid("fresh"), "zebrafish")
        self.assertNotIn("  > ", fresh, "the first probe has saved nothing to spend")
        session = self.sid("s")
        self.run_probe(mem, session, QUERY)
        self.run_probe(mem, session, QUERY)
        unclear = self.run_probe(mem, session, "zebrafish", gap=1e9)
        self.assertNotIn("  > ", unclear)

    def test_the_whole_block_stays_within_1200_bytes_with_a_body(self):
        mem = self.loaded()
        rows = [(f"id-{i}", "durable", "lesson", "h" * 80) for i in range(6)]
        text, shown = mem._render_candidate_block(
            rows, 1200, body=("id-0", "가" * 600))
        self.assertLessEqual(len(text.encode()), 1200)
        self.assertEqual(shown, 6)


if __name__ == "__main__":
    unittest.main()
