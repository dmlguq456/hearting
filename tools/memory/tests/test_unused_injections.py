#!/usr/bin/env python3
"""Injections nobody used (PR unused-injections-1116), with synthetic hook input only.

1. the "Local evidence present" block reaches no harness's context;
2. an unchanged start receipt / "[답변 필요]" status line / fixed peer guidance is not injected
   again (first delivery always, changed text and the turn after a compaction deliver again);
   a text that judges this very turn (the open-question line, a message body) is never withheld;
3. at most three memory candidates in 600 bytes;
4. the session-start summary does not arrive twice for one start, but a new compaction brings it.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT / "utilities"), str(ROOT), str(ROOT / "tools/memory")]
import session_tidy as st
import test_session_tidy as base

HARNESSES = ("claude", "codex", "opencode")
# OpenCode's system-prompt context lasts for one request, so its per-turn repeat is the carrier.
REPEATS_IN_OPENCODE = ("opencode",)


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class EvidenceListIsGoneTest(unittest.TestCase):
    def test_the_probe_and_every_caller_are_removed(self):
        self.assertFalse((ROOT / "hooks/local-evidence-inject.sh").exists())
        for path in ("hooks/session-card-inject.sh", "adapters/codex/hooks/userprompt-lifecycle.py",
                     "adapters/opencode/plugins/hearting-guards.js", "adapters/codex/bin/preflight.sh",
                     "adapters/opencode/bin/preflight.sh"):
            text = read(path)
            self.assertNotIn("local-evidence", text, path)
            self.assertNotIn("Local evidence present", text, path)

    def test_claude_card_hook_never_runs_a_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hooks").mkdir()
            (root / "utilities").mkdir()
            subprocess.run(["cp", str(ROOT / "hooks/session-card-inject.sh"), str(root / "hooks/card.sh")], check=True)
            (root / "utilities/session_tidy.py").write_text(
                'HUMAN_NOTICE_BUDGET_SECONDS = 2.0\n'
                'def run_hook(*a, **kw):\n'
                '    kw["emit"]("PREPARED-CARD")\n'
                '    kw["startup_context"]["due"] = True\n'
                'def apply_deferred(entries): pass\n')
            probe = root / "hooks/local-evidence-inject.sh"
            probe.write_text(f'#!/bin/sh\necho "# Local evidence present"\ntouch "{root}/ran"\n')
            probe.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "OPENCODE_", "FLEET_", "MEM_"))}
            env["AGENT_HOME"] = str(root)
            for event in ("SessionStart", "UserPromptSubmit"):
                out = subprocess.run(["bash", str(root / "hooks/card.sh")], env=env, text=True, capture_output=True,
                                     input=json.dumps({"hook_event_name": event, "session_id": "s", "cwd": directory,
                                                       "prompt": "hi"}), timeout=20).stdout
                self.assertNotIn("Local evidence", out)
            self.assertFalse((root / "ran").exists())

    def test_codex_prompt_probes_carry_no_evidence(self):
        spec = importlib.util.spec_from_file_location("codex_prompt_unused", ROOT / "adapters/codex/hooks/userprompt-lifecycle.py")
        codex = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = codex
        spec.loader.exec_module(codex)
        asked = []
        with mock.patch.object(codex, "run_preflight", side_effect=lambda kind, _cwd: asked.append(kind) or kind), \
             mock.patch.object(codex, "candidate_context", return_value=("candidate", None)), \
             mock.patch.object(codex, "token_budget_context", return_value="budget"):
            parts, _ = codex.prompt_probe_context({}, "/fixture", "sid")
        self.assertEqual(asked, ["briefing"])
        self.assertEqual(parts, ["candidate", "briefing", "budget"])


class RepeatCase(base.TidyCase):
    def setUp(self):
        super().setUp()
        env = self.library()
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        self.now = 1000

    def run_prompt(self, harness, sid, **kw):
        out = []
        self.now += 10
        st.run_hook(harness, kw.pop("event", "prompt"), sid, cwd=str(self.cwd), env=os.environ,
                    now=self.now, emit=out.append, **kw)
        return "\n".join(out)

    def projected(self, harness, sid, question):
        seat = st.resolve_seat(harness, str(self.cwd), sid=sid)
        return {"items": [{"id": "W1", "source": "S1", "question": question, "since": 1, "stale": False,
                           "owner": {"harness": harness, "session_id": "other", "seat_key": "other-seat"}}],
                "by_session": {(harness, sid): ["W1"]}} if seat else {}


class AnswerNeededOnceTest(RepeatCase):
    def test_unchanged_line_is_not_repeated_but_first_changed_and_after_compaction_are(self):
        import human_wait_state
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                sid = "wait-" + harness
                view = {"q": "which file?"}
                with mock.patch.object(human_wait_state, "cached_view",
                                       side_effect=lambda **kw: self.projected(harness, sid, view["q"])):
                    first = self.run_prompt(harness, sid)
                    second = self.run_prompt(harness, sid)
                    self.assertIn("[답변 필요] which file?", first)           # never withheld the first time
                    if harness in REPEATS_IN_OPENCODE:
                        self.assertIn("[답변 필요] which file?", second)
                    else:
                        self.assertNotIn("답변 필요", second)
                        view["q"] = "which branch?"                          # the state changed
                        self.assertIn("[답변 필요] which branch?", self.run_prompt(harness, sid))
                        self.assertNotIn("답변 필요", self.run_prompt(harness, sid))
                        # a compaction may have dropped it from the context: once more
                        self.run_prompt(harness, sid, event="start", source="compact", render_context=False)
                        self.assertIn("[답변 필요] which branch?", self.run_prompt(harness, sid))
                        self.assertNotIn("답변 필요", self.run_prompt(harness, sid))

    def test_open_question_line_comes_with_every_typed_prompt(self):
        # The line judges this very input ("not recorded as the answer"): it is not a status
        # line, so it is new each time the person types while the question is open.
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                sid = "open-" + harness
                line = lambda *a: "[답변 필요] 질문 q1이 아직 열려 있습니다: 방금 입력은 답으로 기록하지 않음"  # noqa: E731
                with mock.patch.object(st, "_open_question_line", side_effect=line):
                    texts = [self.run_prompt(harness, sid, prompt_text=f"something else {n}") for n in range(3)]
                    self.assertTrue(all("질문 q1" in t for t in texts), texts)

    def test_status_line_is_still_told_once_while_the_open_question_line_repeats(self):
        import human_wait_state
        harness, sid = "claude", "mixed-claude"
        line = lambda *a: "[답변 필요] 질문 q1이 아직 열려 있습니다: 방금 입력은 답으로 기록하지 않음"  # noqa: E731
        with mock.patch.object(human_wait_state, "cached_view",
                               side_effect=lambda **kw: self.projected(harness, sid, "which file?")), \
             mock.patch.object(st, "_open_question_line", side_effect=line):
            texts = [self.run_prompt(harness, sid, prompt_text=f"other {n}") for n in range(3)]
        self.assertIn("[답변 필요] which file?", texts[0])
        self.assertTrue(all("[답변 필요] which file?" not in t for t in texts[1:]), texts)
        self.assertTrue(all("질문 q1" in t for t in texts), texts)


class StartReceiptOnceTest(unittest.TestCase):
    def row(self, folder, harness, sid, row_id, text):
        import start_receipt as sr
        key = sr._key(harness, sid)
        import time
        (folder / f"{key}.json").write_text(json.dumps({
            "schema": "start_receipt_v1", "id": row_id, "saved_at": time.time(), "harness": harness, "session_id": sid,
            "receipt": {"route_id": "rt-1", "state": text, "parent_next": "end-turn"}}))

    def test_same_text_once_changed_text_and_compaction_deliver_again_for_every_harness(self):
        import start_receipt as sr
        import context_repeat
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            jobs = home / "jobs" / "jobs.log"
            folder = home / "jobs" / "start-receipts"
            folder.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(home / "state")}):
                for harness in HARNESSES:
                    sid = "sid-" + harness
                    self.row(folder, harness, sid, "a", "running")
                    first = sr.context(harness, sid, jobs)
                    self.assertIn("[start-receipt]", first)                    # first delivery
                    self.row(folder, harness, sid, "b", "running")           # saved again, same text
                    self.assertEqual(sr.context(harness, sid, jobs), "")
                    self.assertEqual(sr.context(harness, sid, jobs), "")
                    self.row(folder, harness, sid, "c", "completed")         # state changed
                    self.assertIn("completed", sr.context(harness, sid, jobs))
                    self.assertEqual(sr.context(harness, sid, jobs), "")
                    context_repeat.reset(harness, sid)                       # compaction / clear
                    self.assertIn("completed", sr.context(harness, sid, jobs))


class PeerGuidanceOnceTest(unittest.TestCase):
    def test_same_guidance_once_then_again_after_reset(self):
        import context_repeat
        spec = importlib.util.spec_from_file_location("peer_unused", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"XDG_STATE_HOME": raw}):
            for harness in HARNESSES:
                recipient = {"harness": harness, "session_id": "peer-" + harness}
                first = peer.receiver_guidance_once("hello", recipient)
                second = peer.receiver_guidance_once("hello", recipient)
                self.assertIn("Peer request", first)
                if harness in REPEATS_IN_OPENCODE:
                    self.assertEqual(second, first)
                    continue
                self.assertEqual(second, "")
                context_repeat.reset(harness, "peer-" + harness)
                self.assertEqual(peer.receiver_guidance_once("hello", recipient), first)


class PeerGuidanceOnlyFixedTextIsWithheldTest(unittest.TestCase):
    def peer(self):
        spec = importlib.util.spec_from_file_location("peer_unused2", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        return peer

    def test_the_same_verified_body_arriving_twice_is_carried_both_times(self):
        peer = self.peer()
        body = "LONG-BODY " * 40
        recipient = {"harness": "claude", "session_id": "long-body"}
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"XDG_STATE_HOME": raw}), \
             mock.patch.object(peer, "_reference_transfer_from_text",
                               side_effect=lambda text: (text.split("ref=")[1][:8], {"to": recipient, "body_ref": {}})), \
             mock.patch.object(peer, "_reference_target_matches", return_value=True), \
             mock.patch.object(peer, "_read_pending", return_value=None), \
             mock.patch.object(peer, "read_peer_payload", return_value={"body": body}), \
             mock.patch.object(peer, "_codex_model_read_access", return_value=False):
            first = peer.receiver_guidance_once("preview (ref=aaaaaaaa)", recipient)
            second = peer.receiver_guidance_once("preview (ref=bbbbbbbb)", recipient)
        self.assertIn(body, first)
        self.assertIn(body, second)

    def test_a_request_command_and_a_failed_body_check_are_never_withheld(self):
        peer = self.peer()
        recipient = {"harness": "codex", "session_id": "differing"}
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"XDG_STATE_HOME": raw}):
            for text, expected in (("r", "Peer request r-1 ... `show r-1`"), ("f", "could not be verified")):
                with self.subTest(text=text), \
                     mock.patch.object(peer, "_request_receiver_guidance",
                                       return_value=expected if text == "r" else ""), \
                     mock.patch.object(peer, "_body_reference_guidance",
                                       return_value=expected if text == "f" else ""):
                    for _ in range(3):
                        self.assertIn(expected, peer.receiver_guidance_once(text, recipient))

    def test_the_fixed_text_counts_as_delivered_only_after_the_caller_flushed(self):
        import context_repeat
        peer = self.peer()
        for harness in ("claude", "codex"):
            recipient = {"harness": harness, "session_id": "flush-" + harness}
            with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"XDG_STATE_HOME": raw}):
                seen = []
                first = peer.receiver_guidance_once("hello", recipient, deferred=seen)
                self.assertIn("Peer request", first)
                # the hook died before it printed: nothing was recorded, so the next turn carries it
                again = []
                self.assertEqual(peer.receiver_guidance_once("hello", recipient, deferred=again), first)
                self.assertFalse(list(context_repeat._dir().glob("*.json")) if context_repeat._dir().exists() else [])
                peer.commit_receiver_guidance(again)                       # it went out
                self.assertEqual(peer.receiver_guidance_once("hello", recipient, deferred=[]), "")


class WorkerCompactionLetsRepeatsInTest(unittest.TestCase):
    def test_a_registered_worker_compaction_forgets_the_repeat_memory(self):
        import context_repeat
        spec = importlib.util.spec_from_file_location("worker_state_hook_unused", ROOT / "utilities/worker-state-hook.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        with tempfile.TemporaryDirectory() as raw:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "FLEET_", "MEM_", "OPENCODE_"))}
            env.update(XDG_STATE_HOME=raw, AGENT_DISPATCH_DEPTH="2", AGENT_SESSION_ROLE="worker")
            with mock.patch.dict(os.environ, env, clear=True):
                for harness in ("claude", "codex"):
                    sid = "worker-" + harness
                    self.assertEqual(context_repeat.fresh_once(harness, sid, "start-receipt", "receipt text"), "receipt text")
                    self.assertEqual(context_repeat.fresh_once(harness, sid, "start-receipt", "receipt text"), "")
                    self.assertEqual(hook._main_compact("compact-before", harness, {"session_id": sid}), 0)
                    self.assertEqual(context_repeat.fresh_once(harness, sid, "start-receipt", "receipt text"), "")
                    self.assertEqual(hook._main_compact("compact-after", harness, {"session_id": sid}), 0)
                    self.assertEqual(context_repeat.fresh_once(harness, sid, "start-receipt", "receipt text"), "receipt text")
                    self.assertEqual(context_repeat.fresh_once(harness, sid, "start-receipt", "receipt text"), "")


class MemoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / "ws").mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "MEM_", "CLAUDE_", "XDG_"))}
        self.env.update(HOME=str(home), XDG_STATE_HOME=str(home / "s"), XDG_DATA_HOME=str(home / "d"),
                        XDG_CONFIG_HOME=str(home / "c"), MEM_INIT="1", MEM_STORE=str(home / "store"))
        self.cwd = home / "ws"

    def mem(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(ROOT / "tools/memory/mem.py"), *args], env=self.env, cwd=self.cwd,
                              input=stdin, text=True, capture_output=True, timeout=60)


class CandidateCapTest(MemoryCase):
    def test_constants_and_output(self):
        import mem_base
        self.assertEqual(mem_base.CANDIDATE_MAX_RESULTS, 3)
        self.assertEqual(mem_base.CANDIDATE_MAX_UTF8_BYTES, 600)
        for n in range(6):
            added = self.mem("add", "--scope", "global", "--headline", f"cap probe headline number {n} " + "x" * 60,
                             "working", "note", f"cap probe body {n}")
            self.assertEqual(added.returncode, 0, added.stderr)
        out = self.mem("candidates", "cap probe headline", "--session-id", "cap-1").stdout
        lines = [l for l in out.splitlines() if l.startswith("- [")]
        self.assertEqual(len(lines), 3, out)
        self.assertLessEqual(len(out.encode("utf-8")), 600)


class SessionStartSummaryOnceTest(MemoryCase):
    def test_same_start_twice_gives_one_summary(self):
        self.assertEqual(self.mem("add", "working", "note", "summary probe record that is long enough to be kept").returncode, 0)
        def start(sid, source):
            return self.mem("inject", "--hook", stdin=json.dumps(
                {"hook_event_name": "SessionStart", "session_id": sid, "source": source})).stdout
        first = start("sum-1", "compact")
        self.assertIn("Unified memory (session-start summary)", first)       # the first one always arrives
        self.assertEqual(start("sum-1", "compact"), "")                       # the same start, again
        self.assertIn("Unified memory", start("sum-1", "resume"))             # another start source
        self.assertIn("Unified memory", start("sum-2", "compact"))            # another session
        self.assertIn("Unified memory", self.mem("inject").stdout)            # manual run unchanged


    def compaction(self, sid):
        """What the PostCompact path does for one compaction of session ``sid``."""
        code = ("import sys; sys.path.insert(0, %r); import context_repeat; "
                "context_repeat.reset('claude', %r, compaction=True)" % (str(ROOT / "utilities"), sid))
        done = subprocess.run([sys.executable, "-c", code], env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def start(self, sid, source="compact"):
        return self.mem("inject", "--hook", stdin=json.dumps(
            {"hook_event_name": "SessionStart", "session_id": sid, "source": source})).stdout

    def test_two_real_compactions_a_second_apart_both_bring_the_summary_whichever_hook_comes_first(self):
        self.assertEqual(self.mem("add", "working", "note", "summary probe record that is long enough to be kept").returncode, 0)
        # PostCompact first, then SessionStart(compact)
        for n in range(2):
            self.compaction("order-a")
            self.assertIn("Unified memory", self.start("order-a"), f"compaction {n + 1}")
        # SessionStart(compact) first, then PostCompact
        for n in range(2):
            self.assertIn("Unified memory", self.start("order-b"), f"compaction {n + 1}")
            self.compaction("order-b")

    def test_one_compaction_event_called_twice_gives_one_summary(self):
        self.assertEqual(self.mem("add", "working", "note", "summary probe record that is long enough to be kept").returncode, 0)
        self.compaction("dup-a")                                              # PostCompact, then the event twice
        self.assertIn("Unified memory", self.start("dup-a"))
        self.assertEqual(self.start("dup-a"), "")
        self.assertIn("Unified memory", self.start("dup-b"))                  # the event twice, PostCompact after
        self.assertEqual(self.start("dup-b"), "")
        self.compaction("dup-b")


if __name__ == "__main__":
    unittest.main()
