#!/usr/bin/env python3
"""Decision records: one source key for both paths, one waiting file, never a failed release.

The release cases reuse the real frame-interview gate fixtures of
``workflow_supervisor.test.py`` and run ``workflow-supervisor.py release --answers``
in-process.  Every case runs inside ``tidy_isolation`` (``/var/tmp`` root for HOME,
XDG_*, MEM_STORE; remote off), so no real memory store or runtime state is touched.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools" / "memory"))
sys.path.insert(0, str(ROOT / "tools" / "memory" / "tests"))

from tidy_isolation import isolated_env  # noqa: E402
import tidy_decisions as td  # noqa: E402
import tidy_transcripts as tt  # noqa: E402

FIXTURES = ROOT / "tools" / "memory" / "tests" / "fixtures"
MEM = ROOT / "tools" / "memory" / "mem.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class KeyTest(unittest.TestCase):

    def test_key_is_a_fixed_serialization_of_question_and_sorted_answers(self):
        import hashlib
        raw = json.dumps({"answers": ["A", "B"], "question": "Q?"}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = "user-choice:" + hashlib.sha256(raw).hexdigest()
        self.assertEqual(td.choice_source_key("Q?", ["B", "A"]), expected)
        self.assertEqual(td.choice_source_key("  Q?\n", [" A ", "B "]), expected)
        self.assertEqual(td.choice_source_key("Q?", "A"), td.choice_source_key("Q?", ["A"]))

    def test_other_text_changes_the_key_but_options_notes_and_corrections_do_not(self):
        base = td.choice_source_key("Q?", ["A"])
        self.assertNotEqual(base, td.choice_source_key("Q?", ["B"]))
        self.assertNotEqual(base, td.choice_source_key("Other?", ["A"]))
        one = td.make_payload("Q?", ["A"], options=[{"label": "A"}], note="n1", correction="c1")
        two = td.make_payload("Q?", ["A"], options=[{"label": "A"}, {"label": "Z", "description": "z"}],
                              note="different", correction="")
        self.assertEqual(one["source"], two["source"])
        self.assertEqual(one["source"], base)

    def test_body_keeps_the_original_text_and_where_it_came_from(self):
        payload = td.make_payload(
            "저장 방식을 어떻게 할까요?", ["파일로 저장 (권장)"],
            options=[{"label": "파일로 저장 (권장)", "description": "JSON 파일 한 개"}, {"label": "DB로 저장", "description": "표준 DB"}],
            note="나중에 바꿀 수 있나요", correction="처음부터 파일이었어요",
            origin={"kind": "frame-interview", "route_id": "rt-abc", "round": 2}, asked_at="2026-09-30")
        body = td.decision_body(payload)
        for text in ("저장 방식을 어떻게 할까요?", "1. 파일로 저장 (권장) — JSON 파일 한 개", "2. DB로 저장 — 표준 DB",
                     "고른 답: 파일로 저장 (권장)", "사용자 메모: 나중에 바꿀 수 있나요",
                     "이해 확인 정정: 처음부터 파일이었어요", "방향 확인 답 · route rt-abc · 2차", "2026-09-30"):
            self.assertIn(text, body)

    def test_interview_payloads_cover_menu_off_menu_and_a_correction(self):
        interview = {"route_id": "rt-x", "round": 1, "created": "2026-10-01",
                     "understanding": "You want it fast.",
                     "questions": [
                         {"id": "q1", "question": "Which?", "options": [{"label": "One", "means": "first"},
                                                                          {"label": "Two", "means": "second"}]},
                         {"id": "q2", "question": "Else?", "options": [{"label": "Yes", "means": "y"}]}]}
        answers = {"actor_kind": "user", "understanding_confirmed": False, "correction": "no, the other thing",
                   "answers": {"q1": {"choice": 1, "note": "because"},
                               "q2": {"choice": "none", "note": "my own words"}}}
        payloads = td.interview_payloads(interview, answers, cwd="/p", actor_kind="user")
        self.assertEqual([p["answers"] for p in payloads],
                         [["Two"], ["my own words"], ["수정 요청: no, the other thing"]])
        self.assertEqual(payloads[0]["note"], "because")
        self.assertEqual(payloads[1]["note"], "")
        self.assertEqual(payloads[0]["correction"], "no, the other thing")
        confirmed = td.interview_payloads(interview, {**answers, "understanding_confirmed": True}, cwd="/p", actor_kind="user")
        self.assertEqual(len(confirmed), 2)

    def test_interview_memory_requires_effective_user_source_not_a_file_claim(self):
        interview = {"route_id": "rt-x", "questions": [{"id": "q", "question": "Which?",
                                                              "options": [{"label": "One"}]}]}
        answers = {"actor_kind": "user", "understanding_confirmed": True, "answers": {"q": {"choice": 0}}}
        for kind in ("unknown", "supervisor", "automatic", "headless-owner", None):
            with self.subTest(kind=kind), mock.patch.object(td, "save_pending") as save, \
                 mock.patch.object(td.subprocess, "run") as run:
                self.assertEqual(td.interview_payloads(interview, answers, actor_kind=kind), [])
                self.assertEqual(td.record_interview_answers(interview, answers, actor_kind=kind), "none")
                save.assert_not_called()
                run.assert_not_called()
        self.assertEqual(td.record_interview_answers(interview, answers), "none")
        for kind in ("supervisor", "automatic", "unknown", None):
            mismatch = {**answers, "actor_kind": kind}
            self.assertEqual(td.interview_payloads(interview, mismatch, actor_kind="user"), [])
        payloads = td.interview_payloads(interview, answers, actor_kind="user")
        self.assertEqual(payloads[0]["origin"]["actor_kind"], "user")
        self.assertEqual(payloads[0]["answers"], ["One"])


class TwoPathsTest(unittest.TestCase):

    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.proj = self.iso.root / "proj"
        self.proj.mkdir()

    def rows(self):
        db = self.iso.mem_store / "memory.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return con.execute("SELECT id, type, tier, scope, source, body FROM records").fetchall()
        finally:
            con.close()

    def test_a_transcript_answer_and_an_interview_answer_are_one_record(self):
        chunk = tt.read_chunk("claude", FIXTURES / "claude-choice.jsonl")
        asked = chunk.choices[0]
        from_transcript = td.make_payload(
            asked["question"], asked["answers"], options=asked["options"], note=asked["note"],
            origin={"kind": "transcript", "harness": "claude", "session": "s1"}, cwd=str(self.proj))
        interview = {"route_id": "rt-1", "round": 1, "created": "2026-10-01",
                     "questions": [{"id": "q1", "question": f"  {asked['question']} ",
                                    "options": [{"label": "파일로 저장 (권장)", "means": "다른 설명"},
                                                {"label": "그 밖의 선택지", "means": "x"}]}]}
        from_interview = td.interview_payloads(
            interview, {"actor_kind": "user", "understanding_confirmed": True, "answers": {"q1": {"choice": 0, "note": "메모"}}},
            cwd=str(self.proj), actor_kind="user")[0]
        self.assertEqual(from_transcript["source"], from_interview["source"])
        self.assertTrue(from_transcript["source"].startswith("user-choice:"))
        with self.iso.patched_environ():
            mem = load("mem_two_paths", MEM)
            first = td.write_decision(mem, from_transcript)
            second = td.write_decision(mem, from_interview)
            third = td.write_decision(mem, from_transcript)
        self.assertEqual(first[0], "written")
        self.assertEqual((second[0], second[1]), ("skipped", first[1]))
        self.assertEqual((third[0], third[1]), ("skipped", first[1]))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        rid, rtype, tier, scope, source, body = rows[0]
        self.assertEqual((rtype, tier, scope), ("decision", "working", "project"))
        self.assertEqual(source, from_transcript["source"])
        self.assertIn("대화 기록의 질문 도구", body)       # the first writer's text stands; no overwrite
        self.assertNotIn("메모", body)

    def test_a_drain_that_wrote_asks_for_the_background_exchange_like_any_foreground_write(self):
        """The drain child writes through ``mem.write_record`` without ``mem.main()``, so the
        exchange a write owes is requested by the same ``_exchange_after_command`` call."""
        fake_mem = mock.Mock()
        for written, calls in ((1, 1), (0, 0)):
            fake_mem.reset_mock()
            result = {"written": written, "skipped": 0, "kept": 0, "ids": []}
            with self.iso.patched_environ(), mock.patch.object(td, "drain_pending", return_value=result), \
                 mock.patch.object(td, "load_mem", return_value=fake_mem), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(td.main(["drain", "--cwd", str(self.proj)]), 0)
            self.assertEqual(fake_mem._exchange_after_command.call_count, calls)


WF = load("workflow_supervisor_fixture", HERE / "workflow_supervisor.test.py")
SUP = WF.SUP


class ReleaseDecisionTest(WF.TestGateSubjectNotCaller):
    """`workflow-supervisor.py release --answers` is where an answer is accepted."""

    def setUp(self):
        super().setUp()
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        keep = {key: os.environ[key] for key in (
            "AGENT_WORKFLOW_ROOT", "AGENT_HOME", "AGENT_ARTIFACT_CHECKPOINT",
            "HEARTING_WORKFLOW_GROUP_REVIEW", "HEARTING_CAMPAIGN_TITLE_AUTO") if key in os.environ}
        patched = self.iso.patched_environ(extra=keep)
        patched.__enter__()
        self.addCleanup(patched.__exit__, None, None, None)
        self.state = self.iso.xdg_state / "hearting" / "session-tidy"
        self.route, self.path = self.two_stage_route(
            human_gate="frame-review", continuation={"kind": "human-gate", "gate": "frame-review"})
        self.jobs, _session, _attempt = self.owner_registry()
        self.interview, self.value = self._interview()
        self._block_with(self.path, self.jobs, self.interview)
        self.answers_path, self.answers = self._answers(self.value)

    def release(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = SUP.main(["release", "--route", str(self.path), "--gate", "frame-review",
                                    "--decision", "proceed", "--jobs", str(self.jobs),
                                    "--answers", str(self.answers_path), "--actor", "fixture-user"])
        return code, out.getvalue(), err.getvalue()

    def records(self):
        db = self.iso.mem_store / "memory.db"
        if not db.exists():
            return []
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return con.execute("SELECT type, tier, scope, source, body FROM records").fetchall()
        finally:
            con.close()

    def pending(self):
        return sorted((self.state / "decisions" / "pending").glob("*.json"))

    def assert_release_contract(self, code, out):
        self.assertEqual(code, 0)
        payload = json.loads(out)               # stdout stays exactly the one JSON line
        self.assertEqual((payload["decision"], payload["answers_recorded"]), ("proceed", 1))
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_the_accepted_answer_becomes_one_decision_record(self):
        code, out, err = self.release()
        self.assert_release_contract(code, out)
        self.assertEqual(err, "")
        rows = self.records()
        self.assertEqual(len(rows), 1)
        rtype, tier, scope, source, body = rows[0]
        self.assertEqual((rtype, tier, scope), ("decision", "working", "project"))
        self.assertEqual(source, td.choice_source_key("Fix only the approval step, or the questions too?",
                                                      ["Approval only"]))
        for text in ("Fix only the approval step, or the questions too?", "Both (recommended)",
                     "고른 답: Approval only", "사용자 메모: wording can wait", "route rt-fixture0000000"):
            self.assertIn(text, body)
        self.assertEqual(self.pending(), [])

    def test_a_failing_memory_write_keeps_the_release_and_the_original_text(self):
        broken = self.iso.root / "not-a-directory"
        broken.write_text("x", encoding="utf-8")
        os.environ["MEM_STORE"] = str(broken)
        code, out, err = self.release()
        self.assert_release_contract(code, out)
        self.assertEqual(len([line for line in err.splitlines() if line.startswith("[decision]")]), 1, err)
        waiting = self.pending()
        self.assertEqual(len(waiting), 1)
        self.assertEqual(oct(waiting[0].stat().st_mode & 0o777), "0o600")
        payload = json.loads(waiting[0].read_text(encoding="utf-8"))
        self.assertEqual((payload["question"], payload["answers"], payload["note"]),
                         ("Fix only the approval step, or the questions too?", ["Approval only"],
                          "wording can wait"))
        self.assertEqual(self.records(), [])
        # the next tidy writes it and removes the waiting file
        os.environ["MEM_STORE"] = str(self.iso.mem_store)
        actions = self.base / "actions.json"
        actions.write_text(json.dumps({"schema_version": 1, "batch_id": "after-failure", "actions": []}),
                           encoding="utf-8")
        applied = subprocess.run([sys.executable, str(MEM), "tidy-apply", str(actions), "--cwd", str(self.base)],
                                 env=dict(os.environ), capture_output=True, text=True, cwd=str(self.base))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(self.pending(), [])
        self.assertEqual([row[0] for row in self.records()], ["decision"])
        self.assertIn("추가 1", applied.stdout.strip().splitlines()[-1])

    def test_a_timeout_leaves_the_text_waiting_and_the_release_untouched(self):
        with mock.patch.object(td.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="drain", timeout=8)):
            code, out, err = self.release()
        self.assert_release_contract(code, out)
        self.assertIn("[decision]", err)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.records(), [])

    def test_a_missing_memory_tool_is_one_stderr_line(self):
        with mock.patch.dict(sys.modules, {"tidy_decisions": None}):
            code, out, err = self.release()
        self.assert_release_contract(code, out)
        self.assertEqual(len([line for line in err.splitlines() if line.startswith("[decision]")]), 1, err)
        self.assertEqual(self.records(), [])

    def test_a_write_that_already_happened_is_not_repeated(self):
        self.release()
        before = self.records()
        # the same answer reaching the recorder again (a replay) is skipped by its source
        payloads = td.interview_payloads(self.value, self.answers, route_id="rt-fixture0000000",
                                         cwd=str(self.base), actor_kind="user")
        for payload in payloads:
            td.save_pending(payload)
        completed = subprocess.run([sys.executable, str(HERE / "tidy_decisions.py"), "drain", "--cwd", str(self.base)],
                                   env=dict(os.environ), capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["skipped"], 1)
        self.assertEqual(self.records(), before)
        self.assertEqual(self.pending(), [])


# The parent fixture's own tests are not this suite's business.
for _name in [n for n in dir(ReleaseDecisionTest) if n.startswith("test_") and n not in vars(ReleaseDecisionTest)]:
    setattr(ReleaseDecisionTest, _name, None)


if __name__ == "__main__":
    unittest.main()
