#!/usr/bin/env python3
"""session-tidy shared state: cards, hook injection, ledger, notices, transcript reading.

Every subprocess and every in-process call runs inside ``tidy_isolation`` (a
``/var/tmp`` root for HOME, XDG_*, MEM_STORE, remote disabled, no worker/pane
markers unless a test sets one on purpose).
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "utilities"))

from tidy_isolation import isolated_env  # noqa: E402
import session_tidy as st  # noqa: E402
import session_tidy_clear as clear  # noqa: E402
import tidy_transcripts as tt  # noqa: E402
from test_tidy_isolation import FIXTURES, FIXTURE_NOW, load_opencode_fixture  # noqa: E402

TIDY = ROOT / "utilities" / "session_tidy.py"
DAY = 86400
PANE = "test:pane-a"
PANE_BESIDE = "test:pane-beside"


class TidyCase(unittest.TestCase):

    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.cwd = self.iso.root / "proj"
        self.cwd.mkdir()
        self.state = self.iso.xdg_state / "hearting" / "session-tidy"

    def cli(self, *args, pane=PANE, extra=None, input=None, cwd=None):
        env = dict(extra or {})
        if pane:
            env["HERDR_PANE_ID"] = pane
        return self.iso.run([sys.executable, TIDY, *args], input=input, extra=env, cwd=cwd or self.cwd)

    def hook(self, harness, event, sid, *more, pane=PANE, extra=None, cwd=None):
        result = self.cli("hook", "--harness", harness, "--event", event, "--session-id", sid,
                          *more, pane=pane, extra=extra, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def card(self, sid, text, harness="claude", pane=PANE, extra=None, cwd=None):
        result = self.cli("card", "--harness", harness, "--session-id", sid, "--text", text,
                          pane=pane, extra=extra, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def library(self, extra=None):
        env = {"HERDR_PANE_ID": PANE}
        env.update(extra or {})
        return self.iso.patched_environ(env)


class CardWriteTest(TidyCase):

    def test_card_is_written_at_once_with_private_modes(self):
        line = self.card("sid-A", "진행 중인 일: 정리 시험\n기다리는 결정: 없음\n다음 할 일: 병합\n관련: PR 12")
        match = re.fullmatch(r"card=(\S+) seat=([0-9a-f]+)", line)
        self.assertIsNotNone(match, line)
        text_path = Path(match.group(1))
        json_path = text_path.with_suffix(".json")
        for path in (text_path, json_path):
            self.assertTrue(path.is_file(), path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        for directory in (self.state, self.state / "cards", self.state / "sessions", self.state / "locks"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual((data["generation"], data["author"]["sid"], data["author"]["harness"]), (1, "sid-A", "claude"))
        self.assertIn("기다리는 결정", data["body"])
        self.assertIn("기다리는 결정", text_path.read_text(encoding="utf-8"))

    def test_second_card_advances_the_generation_and_keeps_history(self):
        self.card("sid-A", "첫 카드")
        self.card("sid-A", "둘째 카드")
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((data["generation"], data["body"]), (2, "둘째 카드"))
        history = list((self.state / "card-history").glob("*/*.json"))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text(encoding="utf-8"))["body"], "첫 카드")

    def test_card_needs_no_argument_the_body_alone_is_enough(self):
        result = self.cli("card", input="본문만 넘김", extra={"CLAUDE_CODE_SESSION_ID": "sid-env"})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((data["author"]["harness"], data["author"]["sid"]), ("claude", "sid-env"))

    def test_each_harness_session_variable_names_the_author(self):
        for name, value, harness in (("CODEX_THREAD_ID", "thr-1", "codex"), ("OPENCODE_SESSION_ID", "ses_1", "opencode")):
            result = self.cli("card", input="본문", extra={name: value}, pane=f"test:{harness}")
            self.assertEqual(result.returncode, 0, result.stderr)
        authors = sorted(
            (json.loads(p.read_text(encoding="utf-8"))["author"]["harness"],
             json.loads(p.read_text(encoding="utf-8"))["author"]["sid"])
            for p in (self.state / "cards").glob("*.json"))
        self.assertEqual(authors, [("codex", "thr-1"), ("opencode", "ses_1")])

    def test_author_falls_back_to_the_latest_ledger_session_of_the_seat(self):
        self.hook("claude", "start", "sid-ledger")
        result = self.cli("card", input="환경변수 없이 작성")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((data["author"]["harness"], data["author"]["sid"]), ("claude", "sid-ledger"))
        self.assertEqual(self.hook("claude", "prompt", "sid-ledger"), "")     # it is the author

    def test_card_body_is_not_rejected_for_shape_only_capped_at_8_kib(self):
        self.assertTrue(self.card("sid-A", "네 칸 없이 그냥 적음").startswith("card="))
        big = "가" * 6000                                                       # 18,000 bytes
        self.assertTrue(self.card("sid-A", big).startswith("card="))
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertLessEqual(len(data["body"].encode("utf-8")), st.CARD_BODY_MAX_BYTES)
        self.assertEqual(self.cli("card", "--harness", "claude", "--session-id", "sid-A", "--text", "  ").stdout.strip(),
                         "card=none reason=empty-body")

    def test_body_from_a_file(self):
        body = self.iso.root / "card.txt"
        body.write_text("파일 본문", encoding="utf-8")
        result = self.cli("card", "--harness", "codex", "--session-id", "t1", "--file", str(body))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("파일 본문", next((self.state / "cards").glob("*.md")).read_text(encoding="utf-8"))


class CardConsumeTest(TidyCase):

    def test_new_session_receives_the_card_once_at_start_and_not_again(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "진행 중인 일: 표식-7Q")
        got = self.hook("claude", "start", "sid-B", "--source", "clear")
        self.assertIn("표식-7Q", got)
        self.assertIn("[세션 카드]", got)
        self.assertIn(str(self.state / "cards"), got)
        self.assertEqual(self.hook("claude", "prompt", "sid-B"), "")
        self.assertEqual(self.hook("claude", "prompt", "sid-B"), "")

    def test_new_session_that_only_sends_a_prompt_receives_it_once(self):
        self.card("sid-A", "표식-P1")
        self.assertIn("표식-P1", self.hook("codex", "prompt", "sid-C"))
        self.assertEqual(self.hook("codex", "prompt", "sid-C"), "")

    def test_authoring_session_gets_nothing_from_ordinary_prompts_or_restarts(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "표식-A")
        for _ in range(3):
            self.assertEqual(self.hook("claude", "prompt", "sid-A"), "")
        self.assertEqual(self.hook("claude", "start", "sid-A", "--source", "resume"), "")

    def test_card_arrives_once_after_compact_for_the_author(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "표식-C")
        after = self.hook("claude", "start", "sid-A", "--source", "compact")
        self.assertIn("표식-C", after)
        self.assertEqual(self.hook("claude", "prompt", "sid-A"), "")
        # the compact delivery was the one hand-over of this generation for the whole seat
        self.assertEqual(self.hook("claude", "start", "sid-B"), "")

    def test_one_generation_is_handed_over_once_for_the_whole_seat(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "표식-S")
        self.assertIn("표식-S", self.hook("claude", "start", "sid-B"))
        self.assertEqual(self.hook("claude", "start", "sid-C"), "")
        self.assertEqual(self.hook("claude", "prompt", "sid-C"), "")
        self.assertEqual(self.hook("claude", "start", "sid-B", "--source", "compact"), "")
        self.assertEqual(self.hook("claude", "start", "sid-A", "--source", "compact"), "")
        self.card("sid-A", "표식-S2")                                      # a new card: once more
        self.assertIn("표식-S2", self.hook("claude", "start", "sid-C", "--source", "resume"))
        self.assertEqual(self.hook("claude", "start", "sid-D"), "")

    def test_compact_event_only_marks_the_round_and_the_next_prompt_delivers(self):
        self.hook("opencode", "start", "ses_A")
        self.card("ses_A", "표식-O", harness="opencode")
        self.assertEqual(self.hook("opencode", "compact", "ses_A"), "")
        self.assertIn("표식-O", self.hook("opencode", "prompt", "ses_A"))
        self.assertEqual(self.hook("opencode", "prompt", "ses_A"), "")

    def test_start_and_compact_event_for_one_compaction_count_once(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "표식-D")
        self.assertIn("표식-D", self.hook("claude", "start", "sid-A", "--source", "compact"))
        self.assertEqual(self.hook("claude", "compact", "sid-A"), "")
        self.assertEqual(self.hook("claude", "prompt", "sid-A"), "")

    def test_a_newer_card_generation_is_delivered_again(self):
        self.card("sid-A", "표식-1")
        self.assertIn("표식-1", self.hook("claude", "start", "sid-B"))
        self.card("sid-A", "표식-2")
        got = self.hook("claude", "start", "sid-B", "--source", "resume")
        self.assertIn("표식-2", got)
        self.assertNotIn("표식-1", got)

    def test_no_card_means_no_output_and_exit_zero(self):
        for event in ("start", "prompt", "compact"):
            result = self.cli("hook", "--harness", "claude", "--event", event, "--session-id", "sid-X")
            self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_an_empty_state_folder_never_makes_output(self):
        self.assertFalse(self.state.exists())
        self.assertEqual(self.hook("codex", "start", "t-new"), "")

    def test_reread_repeats_the_consumed_text_for_that_session_only(self):
        self.card("sid-A", "표식-R")
        first = self.hook("opencode", "prompt", "ses_B")
        self.assertIn("표식-R", first)
        again = self.hook("opencode", "prompt", "ses_B", "--reread")
        self.assertEqual(again.strip(), first.strip())
        self.assertEqual(self.hook("opencode", "prompt", "ses_C", "--reread"), "")
        self.hook("opencode", "compact", "ses_B")
        self.assertEqual(self.hook("opencode", "prompt", "ses_B", "--reread").strip().count("표식-R"), 0)

    def test_receipt_is_written_only_after_the_output_went_out(self):
        self.card("sid-A", "표식-E")

        def broken(_text):
            raise BrokenPipeError()

        with self.library():
            with self.assertRaises(BrokenPipeError):
                st.run_hook("claude", "start", "sid-B", cwd=str(self.cwd), emit=broken)
            sent = []
            text = st.run_hook("claude", "start", "sid-B", cwd=str(self.cwd), emit=sent.append)
        self.assertIn("표식-E", text)
        self.assertEqual(sent, [text])

    def test_output_never_exceeds_2400_bytes_and_always_names_the_file(self):
        self.card("sid-A", "가나다라 " * 900)
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.write_notice(seat, "정리 결과: 새 기록 3건. 되돌리기: mem tidy-undo b1", author_harness="claude", author_sid="sid-A")
        got = self.hook("claude", "start", "sid-B")
        self.assertLessEqual(len(got.encode("utf-8")), st.INJECTION_MAX_BYTES)
        self.assertIn("이하 생략", got)
        self.assertIn(str(self.state / "cards"), got)
        self.assertIn("[정리 결과]", got)

    def test_header_carries_the_write_time_and_elapsed_days(self):
        self.card("sid-A", "오래된 카드")
        json_path = next((self.state / "cards").glob("*.json"))
        data = json.loads(json_path.read_text(encoding="utf-8"))
        data["authored_at_epoch"] -= 4 * DAY
        json_path.write_text(json.dumps(data), encoding="utf-8")
        got = self.hook("claude", "start", "sid-B")
        self.assertIn("4일 전", got)
        self.assertIn("오래된 카드", got)                                      # an old card is never dropped


class SeatTest(TidyCase):

    def test_pane_is_the_seat_when_present_else_harness_and_project(self):
        with self.iso.patched_environ({"HERDR_PANE_ID": "test:p9"}):
            pane_a = st.resolve_seat("claude", str(self.cwd))
            pane_b = st.resolve_seat("codex", str(self.cwd))
        self.assertEqual((pane_a.kind, pane_a.key), ("pane", pane_b.key))
        with self.iso.patched_environ():
            claude = st.resolve_seat("claude", str(self.cwd))
            codex = st.resolve_seat("codex", str(self.cwd))
            other_dir = self.iso.root / "other"
            other_dir.mkdir()
            other = st.resolve_seat("claude", str(other_dir))
        self.assertEqual(claude.kind, "project")
        self.assertEqual(len({claude.key, codex.key, other.key, pane_a.key}), 4)

    def test_without_a_pane_the_card_reaches_only_the_same_harness_and_project(self):
        self.card("sid-A", "표식-S", pane=None)
        self.assertIn("표식-S", self.hook("claude", "start", "sid-B", pane=None))
        self.assertEqual(self.hook("codex", "start", "t-B", pane=None), "")
        elsewhere = self.iso.root / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.hook("claude", "start", "sid-C", pane=None, cwd=elsewhere), "")
        self.assertEqual(self.hook("claude", "start", "sid-D", pane="test:other-pane"), "")

    def test_different_panes_do_not_share_cards(self):
        self.card("sid-A", "표식-pane-a", pane="test:pane-a")
        self.assertEqual(self.hook("claude", "start", "sid-B", pane="test:pane-b"), "")
        self.assertIn("표식-pane-a", self.hook("claude", "start", "sid-B2", pane="test:pane-a"))


class DaemonCodexSeatTest(TidyCase):
    """Codex in the shared app-server daemon has no HERDR_PANE_ID: its pane comes from herdr."""

    def setUp(self):
        super().setUp()
        self.fake_herdr = self.iso.root / "fake-herdr"
        self.agents_file = self.iso.root / "agents.json"
        self.calls = self.iso.root / "herdr-calls.txt"
        self.fake_herdr.write_text(
            f'#!/bin/sh\necho "$@" >> "{self.calls}"\ncat "{self.agents_file}"\n', encoding="utf-8")
        self.fake_herdr.chmod(0o755)
        self.agents([])
        self.env = {"HEARTING_TIDY_TEST_ROOT": str(self.iso.root), "HEARTING_TIDY_HERDR": str(self.fake_herdr)}

    def agents(self, rows):
        self.agents_file.write_text(json.dumps({"id": "cli:agent:list", "result": {"agents": rows}}), encoding="utf-8")

    def agent(self, pane, sid, harness="codex", cwd=None):
        return {"agent": harness, "pane_id": pane, "agent_status": "idle", "cwd": str(cwd or self.cwd),
                "foreground_cwd": str(cwd or self.cwd),
                "agent_session": {"kind": "id", "source": f"herdr:{harness}", "value": sid}}

    def seat(self, harness="codex", sid="t-A", source="", cwd=None):
        with self.iso.patched_environ(self.env):
            return st.resolve_seat(harness, str(cwd or self.cwd), {}, sid, source)

    @staticmethod
    def pane_key(pane):
        return st._digest("pane", pane)

    def know(self, pane, sid):
        """The pane's seat ledger has seen ``sid`` (what a hook of that session leaves behind)."""
        with self.iso.patched_environ(self.env):
            seat = st.Seat("pane", self.pane_key(pane), pane, "codex", "")
            with st.seat_lock(seat.key):
                st.record_event(seat, "codex", sid, "prompt", cwd=str(self.cwd))

    def book(self, pane, sid, status="reserved", cwd=None):
        key = self.pane_key(pane)
        now = st.now_epoch()
        body = {"schema": 1, "nonce": "n" + key[:6], "status": status, "created": now - 30, "deadline": now + 500,
                "seat": {"kind": "pane", "key": key, "pane": pane, "harness": "codex", "project_key": ""},
                "harness": "codex", "sid": sid, "cwd": str(cwd or self.cwd), "card_generation": 1, "prompt_seq": 1}
        (self.state / "clear").mkdir(parents=True, exist_ok=True)
        (self.state / "clear" / f"{key}.json").write_text(json.dumps(body), encoding="utf-8")

    def test_the_one_codex_pane_with_the_thread_id_is_the_seat(self):
        self.agents([self.agent("w:p1", "t-A"), self.agent("w:p2", "t-other")])
        seat = self.seat()
        self.assertEqual((seat.kind, seat.pane, seat.key), ("pane", "w:p1", self.pane_key("w:p1")))

    def test_no_match_two_matches_a_failing_herdr_and_other_harnesses_stay_on_the_project_seat(self):
        for rows in ([], [self.agent("w:p1", "t-other")],
                     [self.agent("w:p1", "t-A"), self.agent("w:p2", "t-A")],
                     [self.agent("w:p1", "t-A", harness="claude")]):
            self.agents(rows)
            self.assertEqual(self.seat().kind, "project", rows)
        self.agents_file.write_text("not json", encoding="utf-8")
        self.assertEqual(self.seat().kind, "project")
        self.agents_file.write_text(json.dumps({"error": {"code": "boom"}}), encoding="utf-8")
        self.assertEqual(self.seat().kind, "project")
        self.agents_file.unlink()                                    # the fake exits non-zero with no output
        self.assertEqual(self.seat().kind, "project")
        with self.iso.patched_environ({"HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                                       "HEARTING_TIDY_HERDR": str(self.iso.root / "missing-herdr")}):
            self.assertEqual(st.resolve_seat("codex", str(self.cwd), {}, "t-A").kind, "project")

    def test_other_harnesses_and_a_pane_variable_never_ask_herdr(self):
        self.agents([self.agent("w:p1", "sid-A", harness="claude"), self.agent("w:p9", "t-A")])
        self.assertEqual(self.seat("claude", "sid-A").kind, "project")
        self.assertEqual(self.seat("opencode", "sid-A").kind, "project")
        self.assertEqual(self.seat("codex", None).kind, "project")
        with self.iso.patched_environ(self.env):
            own = st.resolve_seat("codex", str(self.cwd), {"HERDR_PANE_ID": "w:p5"}, "t-A")
            claude = st.resolve_seat("claude", str(self.cwd), {"HERDR_PANE_ID": "w:p5"}, "sid-A")
        self.assertEqual((own.pane, claude.pane), ("w:p5", "w:p5"))
        self.assertFalse(self.calls.exists(), self.calls.read_text() if self.calls.exists() else "")

    def test_a_pane_whose_ledger_knows_the_thread_keeps_it_while_herdr_shows_the_old_session(self):
        self.agents([self.agent("w:p1", "t-A"), self.agent("w:p2", "t-X")])
        self.assertEqual(self.seat(sid="t-B").kind, "project")      # nobody knows t-B
        self.know("w:p1", "t-B")
        self.assertEqual(self.seat(sid="t-B").pane, "w:p1")
        self.assertEqual(self.seat(sid="t-C").kind, "project")
        self.know("w:p2", "t-B")                                     # two panes claim it: no decision
        self.assertEqual(self.seat(sid="t-B").kind, "project")
        self.agents([self.agent("w:p1", "t-B"), self.agent("w:p2", "t-X")])
        self.assertEqual(self.seat(sid="t-B").pane, "w:p1")         # herdr's own value is decisive
        self.agents([self.agent("w:p3", "t-Y")])                     # the pane is gone: its ledger is not consulted
        self.assertEqual(self.seat(sid="t-B").kind, "project")

    def test_the_first_hook_after_a_clear_belongs_to_the_one_window_waiting_for_its_successor(self):
        self.agents([self.agent("w:p1", "t-A"), self.agent("w:p2", "t-X", cwd=self.iso.root / "else")])
        self.book("w:p1", "t-A")
        self.assertEqual(self.seat(sid="t-B", source="clear").pane, "w:p1")
        self.book("w:p1", "t-A", status="unverified")
        self.assertEqual(self.seat(sid="t-B", source="clear").pane, "w:p1")
        self.assertEqual(self.seat(sid="t-B", source="startup").kind, "project")   # only a clear start
        self.assertEqual(self.seat(sid="t-B", source="").kind, "project")
        self.assertEqual(self.seat(sid="t-B", source="clear", cwd=self.iso.root / "else").kind, "project")
        self.book("w:p1", "t-A", status="cleared")                   # nothing waits any more
        self.assertEqual(self.seat(sid="t-B", source="clear").kind, "project")
        self.book("w:p1", "t-old")                                   # herdr shows another session than the booked one
        self.assertEqual(self.seat(sid="t-B", source="clear").kind, "project")
        self.know("w:p1", "t-A")                                     # ... unless the seat's ledger puts it before
        self.know("w:p1", "t-old")
        self.assertEqual(self.seat(sid="t-B", source="clear").pane, "w:p1")
        self.book("w:p1", "t-A", cwd=self.iso.root / "elsewhere")    # the booking is for another directory
        self.assertEqual(self.seat(sid="t-B", source="clear").kind, "project")

    def test_a_confirmed_clear_names_the_thread_whose_hooks_belong_to_the_window(self):
        # Two Codex windows in one directory: the "one window waiting" rule gives up, the thread id
        # the clear read off w:p1's own screen does not.
        self.agents([self.agent("w:p1", "t-A"), self.agent("w:p2", "t-X")])
        key = self.pane_key("w:p1")
        with self.iso.patched_environ(self.env):
            seat = st.Seat("pane", key, "w:p1", "codex", "")
            with st.seat_lock(seat.key):
                st.write_card(seat, "codex", "t-A", "표식-정확일치")
        for status, new, expect in (("cleared", "t-B", "w:p1"), ("cleared", "-", None), ("reserved", "t-B", None),
                                    ("cleared", "t-other", None)):
            with self.subTest(status=status, new=new):
                self.book("w:p1", "t-A", status=status)
                path = self.state / "clear" / f"{key}.json"
                body = json.loads(path.read_text(encoding="utf-8"))
                body["new_session"] = new
                path.write_text(json.dumps(body), encoding="utf-8")
                for source in ("clear", ""):           # the start hook and the prompt hook alike
                    seat = self.seat(sid="t-B", source=source)
                    self.assertEqual(seat.pane if expect else seat.kind, expect or "project")
        self.book("w:p1", "t-A", status="cleared")
        body = json.loads((self.state / "clear" / f"{key}.json").read_text(encoding="utf-8"))
        body["new_session"] = "t-B"
        (self.state / "clear" / f"{key}.json").write_text(json.dumps(body), encoding="utf-8")
        self.agents_file.write_text("not json", encoding="utf-8")   # herdr fails just then: the id still decides
        self.assertEqual(self.seat(sid="t-B").pane, "w:p1")
        out = []
        with self.iso.patched_environ(self.env):
            st.run_hook("codex", "start", "t-B", source="clear", cwd=str(self.cwd), env={}, emit=out.append)
        self.assertIn("표식-정확일치", "".join(out))

    def test_a_second_codex_window_in_the_directory_or_no_booking_is_no_decision(self):
        self.agents([self.agent("w:p1", "t-A"), self.agent("w:p2", "t-X")])
        self.book("w:p1", "t-A")
        self.assertEqual(self.seat(sid="t-B", source="clear").kind, "project")
        self.agents([self.agent("w:p1", "t-A")])
        (self.state / "clear" / f"{self.pane_key('w:p1')}.json").unlink()
        self.assertEqual(self.seat(sid="t-B", source="clear").kind, "project")

    def test_the_hook_card_and_successor_work_through_the_pane_found_in_herdr(self):
        self.agents([self.agent("w:p1", "t-A")])
        with self.iso.patched_environ(self.env):
            st.run_hook("codex", "start", "t-A", cwd=str(self.cwd), env={}, emit=lambda _t: None)
            card = self.iso.run([sys.executable, TIDY, "card", "--harness", "codex", "--session-id", "t-A",
                                 "--text", "표식-데몬"], extra=self.env, cwd=self.cwd)
        self.assertEqual(card.returncode, 0, card.stderr)
        self.assertIn(f"seat={self.pane_key('w:p1')}", card.stdout)
        # /clear: herdr keeps showing t-A, the auto-clear booking waits; t-B's start hook is the first sign of it.
        self.book("w:p1", "t-A")
        out = []
        with self.iso.patched_environ(self.env):
            st.run_hook("codex", "start", "t-B", source="clear", cwd=str(self.cwd), env={}, emit=out.append)
        self.assertIn("표식-데몬", "".join(out))
        with clear_booking_seen(self, "w:p1") as booking:
            self.assertEqual(booking["observed"]["sid"], "t-B")      # the clear helper sees its successor
        # the booking is replaced (a second tidy by t-B) -- t-B keeps its pane through the ledger
        self.book("w:p1", "t-B")
        self.assertEqual(self.seat(sid="t-B").pane, "w:p1")
        # a project-seat session of the same project does not get the pane's card
        out = []
        with self.iso.patched_environ(self.env):
            st.run_hook("codex", "start", "t-Z", cwd=str(self.cwd), env={}, emit=out.append)
        self.assertEqual("".join(out), "")

    def test_enqueue_books_the_auto_clear_for_the_pane_found_in_herdr(self):
        self.agents([self.agent("w:p1", "t-A")])
        started = []
        with self.iso.patched_environ(self.env), \
                mock.patch.object(clear, "_start_helper", side_effect=lambda key, nonce: started.append(key) or 4242):
            resolved = st.resolve_caller("codex", "t-A", str(self.cwd))
            line = clear.schedule_for_enqueue(resolved[0], "codex", "t-A", str(self.cwd))
        self.assertEqual(line, "clear=scheduled")
        self.assertEqual(started, [self.pane_key("w:p1")])
        self.agents([self.agent("w:p1", "t-A"), self.agent("w:p2", "t-A")])
        with self.iso.patched_environ(self.env):
            resolved = st.resolve_caller("codex", "t-A", str(self.cwd))
            self.assertEqual(clear.schedule_for_enqueue(resolved[0], "codex", "t-A", str(self.cwd)),
                             "clear=manual hint=/clear")

    def test_the_handover_storage_lookup_finds_the_pane_of_a_daemon_codex_session(self):
        import dispatch_seat_handover as handover
        self.agents([self.agent("w:p1", "t-A")])
        with self.iso.patched_environ(self.env):
            self.assertEqual(handover.pane_seat({}, "codex", "t-A").pane, "w:p1")
            self.assertIsNone(handover.pane_seat({}, None, "t-A"))
            self.assertIsNone(handover.pane_seat({}, "claude", "t-A"))
            self.assertEqual(handover.storage_recipients("t-A", {}, "codex"), [("t-A", None)])


@contextlib.contextmanager
def clear_booking_seen(case, pane):
    with case.iso.patched_environ():
        yield clear.read_reservation(st._digest("pane", pane))


class WorkerTest(TidyCase):

    MARKERS = ({"AGENT_SESSION_ROLE": "worker"}, {"AGENT_DISPATCH_CHILD": "1"},
               {"AGENT_DISPATCH_DEPTH": "2"}, {"OPENCODE_DISPATCH_SLUG": "slug-1"},
               {"FLEET_TITLE_REFRESH": "1"}, {"MEM_DISTILL": "1"})

    def test_worker_that_inherited_the_supervisor_pane_gets_nothing_and_leaves_no_trace(self):
        self.card("sid-A", "표식-W")                       # written by the main session in that pane
        for marker in self.MARKERS:
            with self.subTest(marker=marker):
                got = self.hook("claude", "start", "sid-worker", extra=marker)      # pane PANE inherited
                self.assertEqual(got, "")
                self.assertEqual(self.hook("claude", "prompt", "sid-worker", extra=marker), "")
                ledgers = "".join(p.read_text(encoding="utf-8") for p in (self.state / "sessions").glob("*.jsonl"))
                self.assertNotIn("sid-worker", ledgers)
        self.assertFalse((self.state / "consumed").exists())
        self.assertIn("표식-W", self.hook("claude", "start", "sid-B"))               # the real next session still gets it

    def test_worker_writes_no_card_and_sees_no_notice(self):
        self.card("sid-A", "원래 카드")
        for marker in self.MARKERS:
            result = self.cli("card", "--harness", "claude", "--session-id", "sid-worker", "--text", "워커 카드", extra=marker)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "card=none reason=worker"))
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((data["generation"], data["body"]), (1, "원래 카드"))
        with self.library():
            st.write_notice(st.resolve_seat("claude", str(self.cwd)), "결과 한 줄")
        self.assertEqual(self.hook("claude", "prompt", "sid-worker", extra={"AGENT_SESSION_ROLE": "worker"}), "")

    def test_the_worker_check_runs_before_the_pane_check_in_process(self):
        self.assertTrue(st.is_worker({"AGENT_SESSION_ROLE": "worker", "HERDR_PANE_ID": "wB:p1N"}))
        self.assertFalse(st.is_worker({"HERDR_PANE_ID": "wB:p1N"}))
        with self.library({"AGENT_SESSION_ROLE": "worker"}):
            self.assertEqual(st.run_hook("claude", "start", "s1", cwd=str(self.cwd)), "")
        self.assertFalse(self.state.exists())


class NoticeTest(TidyCase):

    def notice(self, text="정리 결과: 새 기록 2건. 되돌리기: mem tidy-undo b7", author=("claude", "sid-A")):
        with self.library():
            st.write_notice(st.resolve_seat(author[0], str(self.cwd)), text,
                            author_harness=author[0], author_sid=author[1])

    def test_notice_is_shown_once_at_the_next_prompt_of_the_authoring_session(self):
        self.hook("claude", "start", "sid-A")
        self.notice()
        got = self.hook("claude", "prompt", "sid-A")
        self.assertIn("[정리 결과]", got)
        self.assertIn("mem tidy-undo b7", got)
        self.assertEqual(self.hook("claude", "prompt", "sid-A"), "")

    def test_notice_goes_to_a_new_session_when_the_author_is_gone(self):
        self.hook("claude", "start", "sid-A")
        self.notice()
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "sid-B", "start", now=st.now_epoch() + 5)
        got = self.hook("claude", "start", "sid-B", "--source", "clear")
        self.assertIn("mem tidy-undo b7", got)
        self.assertEqual(self.hook("claude", "prompt", "sid-B"), "")
        self.assertEqual(self.hook("claude", "prompt", "sid-A"), "")            # already shown once

    def test_notice_waits_for_a_living_author_in_a_paneless_seat(self):
        self.hook("claude", "start", "sid-A", pane=None)
        with self.iso.patched_environ():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.write_notice(seat, "결과 한 줄", author_harness="claude", author_sid="sid-A")
        self.assertEqual(self.hook("claude", "start", "sid-B", pane=None), "")   # sid-A was active a moment ago
        self.assertIn("결과 한 줄", self.hook("claude", "prompt", "sid-A", pane=None))

    def test_notice_and_card_arrive_together_within_the_cap(self):
        self.hook("claude", "start", "sid-A")
        self.card("sid-A", "표식-N")
        self.notice()
        got = self.hook("claude", "start", "sid-B")
        self.assertIn("[정리 결과]", got)
        self.assertIn("표식-N", got)
        self.assertLessEqual(len(got.encode("utf-8")), st.INJECTION_MAX_BYTES)


class LedgerTest(TidyCase):

    def test_hook_records_the_session_transcript_and_cwd(self):
        self.hook("codex", "start", "thr-1", "--transcript", "/x/rollout.jsonl", "--source", "startup")
        with self.library():
            seat = st.resolve_seat("codex", str(self.cwd))
            row = st.session_summary(seat)[("codex", "thr-1")]
        self.assertEqual((row["transcript"], row["cwd"], row["epoch"]), ("/x/rollout.jsonl", str(self.cwd), 0))
        self.assertGreater(row["first_seen"], 0)

    def test_compact_raises_the_epoch_and_prompts_are_throttled(self):
        self.hook("claude", "start", "sid-A")
        for _ in range(4):
            self.hook("claude", "prompt", "sid-A")
        self.hook("claude", "compact", "sid-A")
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            row = st.session_summary(seat)[("claude", "sid-A")]
            lines = st._read_ledger_lines(seat)
        self.assertEqual(row["epoch"], 1)
        self.assertLessEqual(len(lines), 4)                                       # start, one prompt, compact (+ slack)

    def test_a_long_ledger_folds_without_losing_epochs_or_first_seen(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            base = 1_700_000_000.0
            st.record_event(seat, "claude", "old", "start", now=base)
            st.record_event(seat, "claude", "old", "compact", now=base + 1, bump_epoch=True)
            for index in range(st.LEDGER_FOLD_LINES + 50):
                st.record_event(seat, "claude", "busy", "prompt", now=base + 100 + index * 60)
            lines = st._read_ledger_lines(seat)
            rows = st.session_summary(seat)
        self.assertLess(len(lines), st.LEDGER_FOLD_LINES)
        self.assertEqual(rows[("claude", "old")]["epoch"], 1)
        self.assertEqual(rows[("claude", "old")]["first_seen"], base)
        self.assertIn(("claude", "busy"), rows)

    def test_latest_session_picks_the_most_recent_of_a_harness(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "s1", "start", now=1000.0)
            st.record_event(seat, "codex", "s2", "start", now=2000.0)
            st.record_event(seat, "claude", "s3", "start", now=3000.0)
            self.assertEqual(st.latest_session(seat)["sid"], "s3")
            self.assertEqual(st.latest_session(seat, "codex")["sid"], "s2")

    def test_status_prints_state_location_and_card(self):
        self.card("sid-A", "카드")
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"state_root={self.state}", result.stdout)
        self.assertIn("card=gen1", result.stdout)
        info = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual((info["card"]["generation"], info["worker"]), (1, False))


class StateSafetyTest(TidyCase):

    def test_a_hook_is_silent_when_the_state_folder_is_unusable(self):
        blocker = self.iso.root / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        result = self.iso.run([sys.executable, TIDY, "hook", "--harness", "claude", "--event", "start",
                               "--session-id", "s"], extra={"HERDR_PANE_ID": PANE, "XDG_STATE_HOME": str(blocker)})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_a_symlinked_state_folder_is_refused_and_its_target_untouched(self):
        elsewhere = self.iso.root / "elsewhere"
        elsewhere.mkdir()
        self.state.parent.mkdir(parents=True)
        self.state.symlink_to(elsewhere)
        self.assertEqual(self.hook("claude", "start", "s"), "")
        result = self.cli("card", "--harness", "claude", "--session-id", "s", "--text", "x")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_bad_hook_arguments_still_exit_zero(self):
        for args in (["hook"], ["hook", "--harness", "claude", "--event", "start", "--bogus"], ["hook", "--event", "x"]):
            result = self.cli(*args)
            self.assertEqual((result.returncode, result.stdout), (0, ""), args)


def jsonl(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def claude_row(kind: str, text: str, cwd: str = "/w") -> dict:
    return {"type": kind, "cwd": cwd, "sessionId": "s", "message": {"role": kind, "content": text}}


class ChunkReaderTest(TidyCase):

    def test_a_large_input_is_read_in_whole_row_chunks_and_the_cursor_moves_only_that_far(self):
        path = self.iso.root / "big.jsonl"
        rows = [claude_row("user" if i % 2 == 0 else "assistant", f"행 {i:04d} " + "가" * 700) for i in range(400)]
        jsonl(path, rows)
        limit, cursor, seen, texts = 64 * 1024, 0, 0, []
        raw = path.read_bytes()
        while True:
            chunk = tt.read_chunk("claude", path, cursor, limit_bytes=limit)
            self.assertLessEqual(chunk.cursor_to - cursor, limit)
            self.assertEqual(raw[chunk.cursor_to - 1:chunk.cursor_to], b"\n")     # always on a row boundary
            seen += chunk.rows
            texts.append(chunk.text)
            if chunk.eof:
                break
            self.assertGreater(chunk.cursor_to, cursor)
            cursor = chunk.cursor_to
        self.assertEqual((seen, chunk.cursor_to), (400, len(raw)))
        joined = "\n".join(texts)
        self.assertEqual([m for m in re.findall(r"행 (\d{4})", joined)], [f"{i:04d}" for i in range(400)])
        first = tt.read_chunk("claude", path, 0, limit_bytes=limit)
        self.assertNotIn("행 0399", first.text)                                     # nothing skipped to the tail

    def test_only_the_part_after_the_watermark_is_read(self):
        path = self.iso.root / "s.jsonl"
        jsonl(path, [claude_row("user", f"메시지 {i}") for i in range(6)])
        with self.library():
            self.assertEqual(tt.read_watermark("claude", "sid-1"), {"cursor": 0})
            first = tt.read_chunk("claude", path, 0, limit_bytes=1)                 # one row
            tt.write_watermark("claude", "sid-1", first.cursor_to, source=str(path))
            mark = tt.read_watermark("claude", "sid-1")
            self.assertEqual(mark["cursor"], first.cursor_to)
            self.assertEqual(stat.S_IMODE(tt.watermark_path("claude", "sid-1").stat().st_mode), 0o600)
            rest = tt.read_pending("claude", "sid-1", path)
            self.assertNotIn("메시지 0", rest.text)
            self.assertIn("메시지 1", rest.text)
            self.assertIn("메시지 5", rest.text)
            tt.write_watermark("claude", "sid-1", rest.cursor_to, source=str(path))
            self.assertEqual(tt.read_pending("claude", "sid-1", path).text, "")
            with open(path, "a", encoding="utf-8") as handle:                       # the session goes on
                handle.write(json.dumps(claude_row("user", "메시지 새로"), ensure_ascii=False) + "\n")
            self.assertEqual(tt.read_pending("claude", "sid-1", path).text, "[user] 메시지 새로")

    def test_a_replaced_or_shorter_file_restarts_from_the_beginning(self):
        path = self.iso.root / "s.jsonl"
        jsonl(path, [claude_row("user", "옛날 " + "x" * 200) for _ in range(5)])
        with self.library():
            tt.write_watermark("claude", "sid-2", path.stat().st_size, source=str(path))
            path.unlink()
            jsonl(path, [claude_row("user", "새 파일")])
            self.assertEqual(tt.read_pending("claude", "sid-2", path).text, "[user] 새 파일")

    def test_a_half_written_last_row_does_not_move_the_cursor(self):
        path = self.iso.root / "s.jsonl"
        good = json.dumps(claude_row("user", "완성된 행"), ensure_ascii=False) + "\n"
        partial = json.dumps(claude_row("user", "쓰는 중"), ensure_ascii=False)
        path.write_text(good + partial[:20], encoding="utf-8")
        chunk = tt.read_chunk("claude", path, 0)
        self.assertEqual((chunk.rows, chunk.cursor_to, chunk.text), (1, len(good.encode("utf-8")), "[user] 완성된 행"))
        self.assertFalse(chunk.eof)
        path.write_text(good + partial, encoding="utf-8")                          # complete, but no newline yet
        chunk = tt.read_chunk("claude", path, chunk.cursor_to)
        self.assertEqual((chunk.rows, chunk.eof, chunk.text), (1, True, "[user] 쓰는 중"))

    def test_a_single_row_longer_than_the_limit_is_read_whole(self):
        path = self.iso.root / "s.jsonl"
        jsonl(path, [claude_row("user", "길다 " + "나" * 5000), claude_row("user", "짧다")])
        chunk = tt.read_chunk("claude", path, 0, limit_bytes=100)
        self.assertEqual(chunk.rows, 1)
        self.assertIn("길다", chunk.text)
        self.assertGreater(chunk.cursor_to, 5000)
        second = tt.read_chunk("claude", path, chunk.cursor_to, limit_bytes=100)
        self.assertEqual((second.text, second.eof), ("[user] 짧다", True))

    def test_a_row_beyond_the_hard_cap_is_stepped_over_and_counted(self):
        path = self.iso.root / "s.jsonl"
        jsonl(path, [claude_row("user", "앞"), claude_row("user", "거대 " + "z" * 3000), claude_row("user", "뒤")])
        original = tt.MAX_ROW_BYTES
        tt.MAX_ROW_BYTES = 1000
        self.addCleanup(setattr, tt, "MAX_ROW_BYTES", original)
        first = tt.read_chunk("claude", path, 0, limit_bytes=200)
        self.assertIn("앞", first.text)
        second = tt.read_chunk("claude", path, first.cursor_to, limit_bytes=200)
        self.assertEqual((second.rows, second.skipped_oversize), (0, 1))
        third = tt.read_chunk("claude", path, second.cursor_to, limit_bytes=200)
        self.assertEqual((third.text, third.eof), ("[user] 뒤", True))

    def test_a_missing_record_is_an_empty_unread_chunk(self):
        chunk = tt.read_chunk("claude", self.iso.root / "gone.jsonl", 7)
        self.assertEqual((chunk.cursor_to, chunk.text, chunk.choices), (7, "", []))
        self.assertTrue(chunk.error)

    def test_an_open_opencode_question_holds_the_cursor_until_it_is_answered(self):
        load_opencode_fixture(self.iso.opencode_db)
        original = tt.now_epoch
        tt.now_epoch = lambda: FIXTURE_NOW
        self.addCleanup(setattr, tt, "now_epoch", original)
        sid = "ses_fixture0000000000000001"
        first = tt.read_chunk("opencode", self.iso.opencode_db, 0, sid)
        self.assertEqual((first.blocked, first.cursor_to), ("open-question", 5))
        connection = sqlite3.connect(self.iso.opencode_db)
        row = connection.execute("SELECT data FROM part WHERE rowid = 6").fetchone()[0]
        part = json.loads(row)
        part["state"].update({"status": "completed", "metadata": {"answers": [["진행 (권장)"]]},
                              "output": "User has answered your questions."})
        connection.execute("UPDATE part SET data = ? WHERE rowid = 6", (json.dumps(part, ensure_ascii=False),))
        connection.commit()
        connection.close()
        second = tt.read_chunk("opencode", self.iso.opencode_db, first.cursor_to, sid)
        self.assertEqual([(c["question"], c["answers"]) for c in second.choices],
                         [("이 방향으로 진행할까요?", ["진행 (권장)"])])
        self.assertEqual((second.cursor_to, second.eof, second.blocked), (6, True, ""))

    def test_a_stale_open_opencode_question_does_not_block_forever(self):
        load_opencode_fixture(self.iso.opencode_db)
        original = tt.now_epoch
        tt.now_epoch = lambda: FIXTURE_NOW + 2 * tt.OPEN_QUESTION_STALE_SEC
        self.addCleanup(setattr, tt, "now_epoch", original)
        chunk = tt.read_chunk("opencode", self.iso.opencode_db, 0, "ses_fixture0000000000000001")
        self.assertEqual((chunk.blocked, chunk.cursor_to, chunk.eof), ("", 6, True))

    def test_opencode_rows_are_chunked_by_rowid_with_the_cursor_at_the_last_row_read(self):
        load_opencode_fixture(self.iso.opencode_db)
        original = tt.now_epoch
        tt.now_epoch = lambda: FIXTURE_NOW + 2 * tt.OPEN_QUESTION_STALE_SEC
        self.addCleanup(setattr, tt, "now_epoch", original)
        sid, cursor, order = "ses_fixture0000000000000001", 0, []
        while True:
            chunk = tt.read_chunk("opencode", self.iso.opencode_db, cursor, sid, limit_bytes=1)
            order.append(chunk.cursor_to)
            if chunk.eof:
                break
            cursor = chunk.cursor_to
        self.assertEqual(order, [1, 2, 3, 4, 5, 6])


def codex_row(kind: str, text: str) -> dict:
    block = "input_text" if kind == "user" else "output_text"
    return {"type": "response_item", "payload": {"type": "message", "role": kind,
                                                 "content": [{"type": block, "text": text}]}}


class TailFirstReadingTest(TidyCase):
    """The newest unread range is read first; an old unread front is never overwritten by a newer end."""

    SID = "sid-tail"
    ROWS = 4000

    def setUp(self):
        super().setUp()
        # ``tempfile`` remembers the first temp dir it saw; keep it from remembering a test's own
        # (about to be deleted) one when the OpenCode snapshot copies the database.
        self.addCleanup(setattr, tempfile, "tempdir", tempfile.tempdir)
        tempfile.tempdir = str(self.iso.tmpdir)

    def make(self, harness, n=None, tag="행", pad=600):
        """A multi-MB record (about ``n`` * ``pad`` bytes) whose last row is the recent decision."""
        n = n or self.ROWS
        texts = [f"{tag} {i:05d} " + "가" * pad for i in range(n)] + ["최근 결정 마커"]
        if harness == "opencode":
            load_opencode_fixture(self.iso.opencode_db)
            con = sqlite3.connect(self.iso.opencode_db)
            con.execute("DELETE FROM part")
            con.execute("INSERT OR REPLACE INTO message (id, session_id, time_created, time_updated, data) "
                        "VALUES ('msg_big', ?, 1, 1, '{\"role\": \"user\"}')", (self.SID,))
            con.executemany("INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
                            "VALUES (?, 'msg_big', ?, ?, ?, ?)",
                            [(f"prt_{i:06d}", self.SID, 1, 1, json.dumps({"type": "text", "text": t}, ensure_ascii=False))
                             for i, t in enumerate(texts)])
            con.commit()
            con.close()
            return self.iso.opencode_db
        path = self.iso.root / f"{harness}-big.jsonl"
        build = claude_row if harness == "claude" else codex_row
        jsonl(path, [build("user" if i % 2 == 0 else "assistant", t) for i, t in enumerate(texts)])
        return path

    def drain(self, harness, path, limit=192 * 1024, appended=None):
        """Read and apply chunk after chunk; returns every chunk (the last one is the empty "nothing left")."""
        chunks = []
        with self.library():
            for step in range(200):
                chunk = tt.read_pending(harness, self.SID, path, limit)
                self.assertEqual(chunk.error, "")
                chunks.append(chunk)
                if chunk.cursor_to == chunk.cursor_from and not chunk.pending_after:
                    return chunks
                tt.mark_applied(harness, self.SID, chunk)
                if appended and step == 0:
                    appended()
        self.fail("the unread ranges never ran out")

    def test_the_recent_decision_is_in_the_first_input_and_later_runs_reach_the_front(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                path = self.make(harness)
                if harness != "opencode":
                    self.assertGreater(path.stat().st_size, 2 * 1024 * 1024)
                chunks = self.drain(harness, path)
                first = chunks[0]
                self.assertIn("최근 결정 마커", first.text)
                self.assertNotIn("행 00000", first.text)                       # not the front of a 2.5MB record
                self.assertTrue(first.pending_after)                            # and it says part is still unread
                self.assertEqual(first.unit, "rowid" if harness == "opencode" else "byte")
                self.assertRegex(tt.describe_coverage(first), r"^(byte|rowid) [\d,]+–[\d,]+ / 전체 [\d,]+$")
                joined = "\n".join(c.text for c in reversed(chunks) if c.text)
                numbers = re.findall(r"행 (\d{5})", joined)
                self.assertEqual(numbers, [f"{i:05d}" for i in range(self.ROWS)])  # every row, once, in order
                self.assertEqual(chunks[-1].pending_after, [])
                with self.library():
                    mark = tt.read_watermark(harness, self.SID)
                self.assertEqual((mark["pending"], mark["cursor"]), ([], mark["end"]))      # nothing is left unread

    def test_nothing_is_marked_read_until_it_is_applied(self):
        path = self.make("claude", n=600)
        with self.library():
            a = tt.read_pending("claude", self.SID, path, 64 * 1024)
            b = tt.read_pending("claude", self.SID, path, 64 * 1024)         # the apply failed: same range again
            self.assertEqual((a.cursor_from, a.cursor_to, a.text), (b.cursor_from, b.cursor_to, b.text))
            tt.mark_applied("claude", self.SID, a)
            c = tt.read_pending("claude", self.SID, path, 64 * 1024)
            self.assertEqual(c.cursor_to, a.cursor_from)                     # continues right before it
            self.assertNotIn("최근 결정 마커", c.text)

    def test_an_append_is_read_first_and_the_old_unread_front_stays_pending(self):
        path = self.make("claude", n=1500)
        added = [claude_row("user", f"새 {i}") for i in range(3)]

        def append():
            with open(path, "a", encoding="utf-8") as handle:
                for row in added:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        chunks = self.drain("claude", path, limit=128 * 1024, appended=append)
        self.assertEqual(chunks[1].text, "[user] 새 0\n[user] 새 1\n[user] 새 2")         # the delta, before any backlog
        self.assertTrue(chunks[1].pending_after)                                          # the front is still unread
        texts = "\n".join(c.text for c in chunks)
        self.assertEqual(sorted(re.findall(r"행 (\d{5})", texts)), [f"{i:05d}" for i in range(1500)])
        self.assertEqual(len(re.findall(r"최근 결정 마커", texts)), 1)

    def test_a_schema_1_watermark_becomes_the_start_of_the_unread_range(self):
        path = self.make("claude", n=300)
        raw = path.read_bytes()
        cut = raw.index(b"\n", len(raw) // 2) + 1
        with self.library():
            tt.write_watermark("claude", self.SID, cut, source=str(path))     # what the old runner wrote
            chunks = self.drain("claude", path, limit=1 << 30)
        self.assertEqual((chunks[0].cursor_from, chunks[0].cursor_to), (cut, len(raw)))
        self.assertIn("최근 결정 마커", chunks[0].text)

    def test_a_replaced_record_is_read_again_from_its_start_and_a_half_row_waits(self):
        path = self.iso.root / "r.jsonl"
        jsonl(path, [claude_row("user", "옛 " + "x" * 300) for _ in range(5)])
        with self.library():
            chunk = tt.read_pending("claude", self.SID, path, 1 << 20)
            tt.mark_applied("claude", self.SID, chunk)
            path.unlink()
            good = json.dumps(claude_row("user", "새 파일"), ensure_ascii=False) + "\n"
            partial = json.dumps(claude_row("user", "쓰는 중"), ensure_ascii=False)
            path.write_text(good + partial[:15], encoding="utf-8")
            chunk = tt.read_pending("claude", self.SID, path, 1 << 20)
            self.assertEqual((chunk.text, chunk.cursor_from), ("[user] 새 파일", 0))
            tt.mark_applied("claude", self.SID, chunk)
            path.write_text(good + partial, encoding="utf-8")
            self.assertEqual(tt.read_pending("claude", self.SID, path, 1 << 20).text, "[user] 쓰는 중")

    def test_a_row_beyond_the_hard_cap_is_stepped_over_and_counted(self):
        path = self.iso.root / "o.jsonl"
        jsonl(path, [claude_row("user", "앞"), claude_row("user", "거대 " + "z" * 3000), claude_row("user", "뒤")])
        original = tt.MAX_ROW_BYTES
        tt.MAX_ROW_BYTES = 1000
        self.addCleanup(setattr, tt, "MAX_ROW_BYTES", original)
        chunks = self.drain("claude", path, limit=200)
        self.assertEqual(sum(c.skipped_oversize for c in chunks), 1)
        self.assertEqual([c.text for c in chunks if c.text], ["[user] 뒤", "[user] 앞"])

    def test_a_question_and_its_answer_in_different_chunks_still_pair_when_the_answer_is_read_first(self):
        path = FIXTURES / "codex-choice.jsonl"
        with self.library():
            choices = [c for chunk in self.drain("codex", path, limit=1) for c in chunk.choices]
        self.assertEqual(sorted(tuple(c["answers"]) for c in choices),
                         [("진행 (Recommended)",), ("한 파일만 하고 나머지는 나중에",)])

    def test_opencode_holds_back_at_an_open_question_and_rereads_a_row_that_changed(self):
        load_opencode_fixture(self.iso.opencode_db)
        original = tt.now_epoch
        tt.now_epoch = lambda: FIXTURE_NOW
        self.addCleanup(setattr, tt, "now_epoch", original)
        sid = "ses_fixture0000000000000001"
        with self.library():
            first = tt.read_pending("opencode", sid, self.iso.opencode_db, 1 << 20)
            self.assertEqual((first.blocked, first.cursor_to), ("open-question", 6))      # rowids 1..5 only
            self.assertEqual(first.pending_after, [[6, 7]])                               # the question stays unread
            tt.mark_applied("opencode", sid, first)
            con = sqlite3.connect(self.iso.opencode_db)
            part = json.loads(con.execute("SELECT data FROM part WHERE rowid = 6").fetchone()[0])
            part["state"].update({"status": "completed", "metadata": {"answers": [["진행 (권장)"]]}})
            con.execute("UPDATE part SET data = ?, time_updated = time_updated + 5000 WHERE rowid = 6",
                        (json.dumps(part, ensure_ascii=False),))
            con.commit()
            con.close()
            second = tt.read_pending("opencode", sid, self.iso.opencode_db, 1 << 20)
            self.assertEqual([c["answers"] for c in second.choices], [["진행 (권장)"]])
            tt.mark_applied("opencode", sid, second)
            self.assertEqual(tt.read_pending("opencode", sid, self.iso.opencode_db, 1 << 20).pending_after, [])


class MemoryLayerTest(TidyCase):
    """Card layer B ("참고할 기억"): its own revision and receipt, beside the unchanged layer A."""

    def refs(self, n=3, batch="b1", status="applied", **more):
        rows = [{"id": f"mem-{i}", "kind": "새", "headline": f"기억 {i} 제목"} for i in range(n)]
        return dict(batch=batch, status=status, refs=rows, **more)

    def update(self, **kw):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            return st.update_memory_refs(seat, **kw)

    def read(self):
        return json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))

    def test_only_memory_refs_changes_and_the_same_batch_result_is_a_no_op(self):
        self.card("sid-A", "카드 본문")
        before = self.read()
        first = self.update(**self.refs(result_path="/r/result.json"))
        after = self.read()
        for key in ("body", "author", "authored_at", "authored_at_epoch", "generation", "prompt_seq"):
            self.assertEqual(after.get(key), before.get(key), key)
        self.assertEqual((first["revision"], first["source_generation"]), (1, 1))
        again = self.update(**self.refs(result_path="/r/result.json"))
        self.assertEqual(again["revision"], 1)                                  # the same batch: same revision
        self.assertEqual(self.update(**self.refs(batch="b2"))["revision"], 2)
        self.assertIsNone(st.update_memory_refs(st.Seat("pane", "nocard", "x", "claude", ""), batch="b", status="applied", refs=[]))

    def test_a_new_card_keeps_the_finished_list_and_the_late_batch_names_the_card_it_was_queued_under(self):
        self.card("sid-A", "첫 카드")
        self.update(**self.refs())
        self.card("sid-A", "둘째 카드")
        card = self.read()
        self.assertEqual((card["generation"], card["memory_refs"]["revision"]), (2, 1))
        late = self.update(**self.refs(batch="old-runner"), source_generation=1)
        self.assertEqual((late["source_generation"], self.read()["generation"], self.read()["body"]), (1, 2, "둘째 카드"))

    def test_a_new_session_gets_the_card_and_the_list_in_one_injection_within_the_cap(self):
        self.card("sid-A", "가나다라 " * 900)
        self.update(**self.refs(n=8, more=4, coverage="일부만 읽음(byte 1–2 / 전체 3) — 앞부분은 다음 정리에서 계속",
                                result_path=str(self.state / "runs" / "b1" / "result.json")))
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.write_notice(seat, "정리 결과: 새 기록 3건. 되돌리기: mem tidy-undo b1", author_harness="claude", author_sid="sid-A")
        got = self.hook("claude", "start", "sid-B")
        self.assertLessEqual(len(got.encode("utf-8")), st.INJECTION_MAX_BYTES)
        for needle in ("[정리 결과]", "[세션 카드]", "[참고할 기억]", "mem-0", "일부만 읽음", "b1/result.json", "카드 전문:"):
            self.assertIn(needle, got)
        self.assertEqual(self.hook("claude", "prompt", "sid-B"), "")

    def test_a_listed_record_is_never_a_body_and_the_list_alone_stays_within_its_bytes(self):
        self.card("sid-A", "본문")
        self.update(**self.refs(n=8, more=0))
        text = st.build_memory_injection(self.read()["memory_refs"], st.MEMORY_REFS_BYTES + 600)
        self.assertLessEqual(len(text.encode("utf-8")), st.MEMORY_REFS_BYTES + 600)
        tiny = st.build_memory_injection(self.read()["memory_refs"], 160)
        self.assertLessEqual(len(tiny.encode("utf-8")), 160)
        self.assertTrue(tiny.startswith("[참고할 기억]"))

    def test_a_session_that_took_layer_a_still_gets_a_newer_list_once_and_a_failed_emit_keeps_it(self):
        self.card("sid-A", "카드")
        self.assertIn("카드", self.hook("claude", "start", "sid-B"))            # A taken before any list exists
        self.update(**self.refs())

        def broken(_text):
            raise BrokenPipeError()

        with self.library():
            with self.assertRaises(BrokenPipeError):
                st.run_hook("claude", "prompt", "sid-B", cwd=str(self.cwd), emit=broken)
        got = self.hook("claude", "prompt", "sid-B")                            # the failed emit used nothing up
        self.assertIn("[참고할 기억]", got)
        self.assertNotIn("[세션 카드]", got)
        self.assertEqual(self.hook("claude", "prompt", "sid-B"), "")
        consumed = json.loads(next((self.state / "consumed").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((consumed["generation"], consumed["memory_revision"]), (1, 1))     # two receipts, kept apart
        self.update(**self.refs(batch="b2"))
        self.assertIn("[참고할 기억]", self.hook("claude", "prompt", "sid-B"))   # a newer revision is handed out again

    def test_reread_keeps_what_the_session_already_got_and_adds_the_later_list(self):
        self.card("sid-A", "OpenCode 카드")
        with self.library({"HERDR_PANE_ID": PANE}):
            st.run_hook("opencode", "start", "ses_B", cwd=str(self.cwd), emit=lambda t: None)
        self.update(**self.refs())
        with self.library({"HERDR_PANE_ID": PANE}):
            st.run_hook("opencode", "prompt", "ses_B", cwd=str(self.cwd), emit=lambda t: None)
            replay = []
            st.run_hook("opencode", "prompt", "ses_B", cwd=str(self.cwd), reread=True, emit=replay.append)
        self.assertIn("OpenCode 카드", replay[0])                                 # layer A is still in the replay
        self.assertIn("[참고할 기억]", replay[0])


class RecentSessionsTest(TidyCase):

    def make_claude_record(self, sid: str, cwd: Path, age_days: float, now: float) -> Path:
        directory = self.iso.claude_dir / "projects" / "-proj"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sid}.jsonl"
        jsonl(path, [claude_row("user", "이전 대화", cwd=str(cwd))])
        os.utime(path, (now - age_days * DAY, now - age_days * DAY))
        return path

    def test_three_day_boundary_uses_the_ledger_and_is_inclusive(self):
        now = 1_800_000_000.0
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "just-in", "start", now=now - 3 * DAY + 1)
            st.record_event(seat, "claude", "on-edge", "start", now=now - 3 * DAY)
            st.record_event(seat, "claude", "just-out", "start", now=now - 3 * DAY - 1)
            st.record_event(seat, "claude", "today", "start", now=now - 5)
            got = [r["sid"] for r in tt.select_recent_sessions(seat, now=now)]
        self.assertEqual(got, ["today", "just-in", "on-edge"])

    def test_the_record_mtime_can_keep_a_ledger_session_inside_the_window(self):
        now = 1_800_000_000.0
        path = self.make_claude_record("long-running", self.cwd, 0.5, now)
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "long-running", "start", transcript=str(path), now=now - 9 * DAY)
            got = tt.select_recent_sessions(seat, now=now)
        self.assertEqual([r["sid"] for r in got], ["long-running"])

    def test_unledgered_past_records_join_only_when_the_project_matches_and_no_pane_is_set(self):
        now = 1_800_000_000.0
        other = self.iso.root / "other"
        other.mkdir()
        self.make_claude_record("same-project", self.cwd, 1, now)
        self.make_claude_record("other-project", other, 1, now)
        self.make_claude_record("too-old", self.cwd, 3.01, now)
        self.make_claude_record("edge-in", self.cwd, 2.99, now)
        with self.iso.patched_environ():
            seat = st.resolve_seat("claude", str(self.cwd))
            got = {r["sid"]: r["source"] for r in tt.select_recent_sessions(seat, now=now)}
        self.assertEqual(got, {"same-project": "project-key", "edge-in": "project-key"})
        with self.library():                                                        # a pane seat: no guessing from paths
            pane_seat = st.resolve_seat("claude", str(self.cwd))
            self.assertEqual(tt.select_recent_sessions(pane_seat, now=now), [])

    def test_ledger_entries_win_over_discovery_for_the_same_session(self):
        now = 1_800_000_000.0
        path = self.make_claude_record("both", self.cwd, 1, now)
        with self.iso.patched_environ():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "both", "start", transcript=str(path), now=now - DAY)
            got = tt.select_recent_sessions(seat, now=now)
        self.assertEqual([(r["sid"], r["source"]) for r in got], [("both", "ledger")])

    def test_codex_and_opencode_records_are_found_by_their_own_project_marker(self):
        now = FIXTURE_NOW
        rollout = self.iso.codex_home / "sessions" / "2026" / "09" / "30" / \
            "rollout-2026-09-30T01-00-00-22222222-2222-4222-8222-222222222222.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text((FIXTURES / "codex-choice.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        os.utime(rollout, (now - 3600, now - 3600))
        load_opencode_fixture(self.iso.opencode_db)
        with self.iso.patched_environ():
            codex_seat = st.seat_for_project("codex", st.project_key_for("/work/fixture-project"))
            open_seat = st.seat_for_project("opencode", st.project_key_for("/work/fixture-project"))
            claude_seat = st.seat_for_project("claude", st.project_key_for("/work/fixture-project"))
            codex = tt.select_recent_sessions(codex_seat, now=now)
            opencode = tt.select_recent_sessions(open_seat, now=now)
            claude = tt.select_recent_sessions(claude_seat, now=now)
        self.assertEqual([r["sid"] for r in codex], ["22222222-2222-4222-8222-222222222222"])
        self.assertEqual([r["sid"] for r in opencode], ["ses_fixture0000000000000001"])
        self.assertEqual(claude, [])


class LocateTest(TidyCase):

    def test_transcripts_are_found_under_each_harness_home(self):
        claude = self.iso.claude_dir / "projects" / "-proj" / "sid-9.jsonl"
        claude.parent.mkdir(parents=True)
        claude.write_text("{}\n", encoding="utf-8")
        codex = self.iso.codex_home / "sessions" / "2026" / "09" / "30" / "rollout-2026-09-30T00-00-00-abc-def.jsonl"
        codex.parent.mkdir(parents=True)
        codex.write_text("{}\n", encoding="utf-8")
        load_opencode_fixture(self.iso.opencode_db)
        with self.iso.patched_environ():
            self.assertEqual(tt.locate_transcript("claude", "sid-9"), claude)
            self.assertEqual(tt.locate_transcript("codex", "abc-def"), codex)
            self.assertEqual(tt.locate_transcript("opencode", "ses_x"), self.iso.opencode_db)
            self.assertIsNone(tt.locate_transcript("claude", "nope"))
            self.assertEqual(tt.locate_transcript("codex", "zzz", hint=str(claude)), claude)


class PromptSeqTest(TidyCase):
    """Real prompts are counted per seat, in a file the ledger throttle and fold never touch."""

    def seq(self, pane=PANE):
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            return st.read_prompt_seq(st.resolve_seat("claude", str(self.cwd)))

    def test_only_real_prompts_count(self):
        self.hook("claude", "start", "sid-A")
        self.hook("claude", "start", "sid-A", "--source", "compact")
        self.hook("claude", "compact", "sid-A")
        self.card("sid-A", "카드")
        self.assertEqual(self.seq(), 0)
        for _ in range(3):
            self.hook("claude", "prompt", "sid-A")
        self.assertEqual(self.seq(), 3)

    def test_prompts_inside_the_ledger_throttle_window_still_count(self):
        self.hook("claude", "start", "sid-A")
        for _ in range(5):                                   # all within PROMPT_LEDGER_THROTTLE_SEC
            self.hook("claude", "prompt", "sid-A")
        lines = (next((self.state / "sessions").glob("*.jsonl"))).read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), 1 + 5)                   # the ledger did throttle ...
        self.assertEqual(self.seq(), 5)                      # ... the count did not

    def test_a_reread_is_not_a_prompt_and_a_second_pane_counts_alone(self):
        self.hook("claude", "prompt", "sid-A")
        self.hook("claude", "prompt", "sid-A", "--reread")
        self.hook("claude", "prompt", "sid-Z", pane="test:pane-z")
        self.assertEqual((self.seq(), self.seq("test:pane-z")), (1, 1))

    def test_the_count_survives_a_ledger_fold(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            for n in range(st.LEDGER_FOLD_LINES + 20):
                st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd), now=1000.0 + n * 100)
            self.assertEqual(st.read_prompt_seq(seat), st.LEDGER_FOLD_LINES + 20)
            self.assertLess(len(st._read_ledger_lines(seat)), st.LEDGER_FOLD_LINES + 20)

    def test_the_card_records_the_count_it_was_written_at(self):
        self.hook("claude", "prompt", "sid-A")
        self.hook("claude", "prompt", "sid-A")
        self.card("sid-A", "카드")
        data = json.loads(next((self.state / "cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(data["prompt_seq"], 2)


class ClearBookingTest(TidyCase):
    """``enqueue`` books at most one clear per seat; the helper is only started, never run here."""

    def setUp(self):
        super().setUp()
        self.fake_herdr = self.iso.root / "fake-herdr"
        self.fake_herdr.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fake_herdr.chmod(0o755)
        self.started = []

    def book(self, harness="claude", sid="sid-A", pane=PANE, opt_out=False, herdr=True, seq_now=None):
        extra = {"HERDR_PANE_ID": pane} if pane else {}
        if herdr:
            extra.update({"HEARTING_TIDY_TEST_ROOT": str(self.iso.root), "HEARTING_TIDY_HERDR": str(self.fake_herdr)})
        with self.iso.patched_environ(extra), \
                mock.patch.object(clear, "_start_helper", side_effect=lambda key, nonce: self.started.append(nonce) or 4242):
            seat = st.resolve_seat(harness, str(self.cwd))
            return clear.schedule_for_enqueue(seat, harness, sid, str(self.cwd), opt_out=opt_out), seat

    def reservation(self, seat):
        with self.iso.patched_environ():
            return clear.read_reservation(seat.key)

    def test_a_pane_inside_herdr_books_the_clear_with_the_exact_identity(self):
        self.hook("claude", "prompt", "sid-A")
        self.card("sid-A", "카드")
        line, seat = self.book()
        self.assertEqual(line, "clear=scheduled")
        res = self.reservation(seat)
        self.assertEqual((res["harness"], res["sid"], res["status"], res["card_generation"], res["prompt_seq"]),
                         ("claude", "sid-A", "reserved", 1, 1))
        self.assertEqual(res["seat"]["pane"], PANE)
        self.assertEqual(res["deadline"] - res["created"], 600.0)
        self.assertEqual(res["helper"]["pid"], 4242)
        self.assertEqual(stat.S_IMODE((self.state / "clear" / f"{seat.key}.json").stat().st_mode), 0o600)
        self.assertEqual(len(self.started), 1)

    def test_no_continue_books_the_clear_alone(self):
        extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                 "HEARTING_TIDY_HERDR": str(self.fake_herdr)}
        with self.iso.patched_environ(extra), mock.patch.object(clear, "_start_helper", return_value=4242):
            seat = st.resolve_seat("claude", str(self.cwd))
            self.assertEqual(clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd), no_continue=True),
                             "clear=scheduled continue=off")
            self.assertTrue(clear.read_reservation(seat.key)["continue_off"])
            self.assertEqual(clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd)), "clear=scheduled")
            self.assertFalse(clear.read_reservation(seat.key)["continue_off"])

    def test_no_clear_keeps_the_window_and_cancels_a_pending_booking(self):
        line, seat = self.book()
        self.assertEqual(line, "clear=scheduled")
        line, _ = self.book(opt_out=True)
        self.assertEqual(line, "clear=off")
        self.assertIsNone(self.reservation(seat))
        self.assertEqual(len(self.started), 1)                  # the opt-out started nothing

    def test_a_newer_tidy_replaces_the_booking_and_the_old_nonce_is_dead(self):
        self.book()
        line, seat = self.book()
        first, second = self.started
        self.assertNotEqual(first, second)
        self.assertEqual(self.reservation(seat)["nonce"], second)
        with self.iso.patched_environ():
            self.assertEqual(clear.validate_request(clear.reservation_path(seat.key), first), (None, "superseded"))
            self.assertEqual(clear.validate_request(clear.reservation_path(seat.key), second)[1], "")

    def test_a_prompt_between_the_card_and_the_enqueue_books_nothing(self):
        self.hook("claude", "prompt", "sid-A")
        self.card("sid-A", "카드")
        self.hook("claude", "prompt", "sid-A")                  # the user typed again after the card
        line, seat = self.book()
        self.assertEqual(line, "clear=skipped reason=new-input")
        self.assertIsNone(self.reservation(seat))
        self.assertEqual(self.started, [])

    def test_outside_herdr_the_manual_command_of_each_harness_is_printed(self):
        for harness, hint in (("claude", "/clear"), ("codex", "/clear"), ("opencode", "/new")):
            line, seat = self.book(harness=harness, pane=None)       # no pane: a project seat
            self.assertEqual(line, f"clear=manual hint={hint}")
            self.assertIsNone(self.reservation(seat))
        line, seat = self.book(herdr=False)                          # a pane id but no herdr to ask
        self.assertEqual(line, "clear=manual hint=/clear")
        self.assertEqual(self.started, [])

    def test_a_manual_enqueue_cancels_the_older_booking(self):
        _line, seat = self.book()
        self.book(herdr=False)
        self.assertIsNone(self.reservation(seat))

    def test_the_enqueue_command_of_a_worker_in_the_inherited_pane_books_nothing(self):
        result = self.cli("enqueue", "--harness", "claude", "--session-id", "sid-A",
                          extra={"AGENT_SESSION_ROLE": "worker", "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                                 "HEARTING_TIDY_HERDR": str(self.fake_herdr)})
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "enqueue=none reason=worker"))
        self.assertFalse((self.state / "clear").exists())

    def test_a_start_at_the_booked_pane_is_noted_as_the_successor(self):
        _line, seat = self.book()
        self.hook("claude", "start", "sid-A")                        # the same session: not a successor
        self.assertNotIn("observed", self.reservation(seat))
        self.hook("claude", "start", "sid-B", "--source", "clear")
        self.assertEqual(self.reservation(seat)["observed"]["sid"], "sid-B")
        self.hook("codex", "start", "sid-C")                         # another harness at the pane: ignored
        self.assertEqual(self.reservation(seat)["observed"]["sid"], "sid-B")

    def test_request_checks_expiry_card_generation_new_input_and_location(self):
        self.hook("claude", "prompt", "sid-A")
        self.card("sid-A", "카드")
        _line, seat = self.book()
        path = self.state / "clear" / f"{seat.key}.json"
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            res = clear.read_reservation(seat.key)
            self.assertEqual(clear.validate_request(path)[1], "")
            self.assertEqual(clear.validate_request(path, now=res["deadline"] + 1), (None, "expired"))
            elsewhere = self.iso.root / "copy.json"
            elsewhere.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(clear.validate_request(elsewhere), (None, "request-unreadable"))
            link = self.state / "clear" / "ffffffffffffffff.json"
            os.symlink(path, link)
            self.assertEqual(clear.validate_request(link), (None, "request-unreadable"))
            link.unlink()
        self.card("sid-A", "새 카드")
        with self.iso.patched_environ():
            self.assertEqual(clear.validate_request(path), (None, "card-changed"))
        self.hook("claude", "prompt", "sid-A")
        with self.iso.patched_environ():
            self.assertEqual(clear.validate_request(path), (None, "new-input"))


class OpenCodeTransactionReadTest(TidyCase):

    SID = "ses_fixture0000000000000001"

    def writer(self):
        load_opencode_fixture(self.iso.opencode_db)
        con = sqlite3.connect(self.iso.opencode_db)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        return con

    def append(self, con):
        con.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                    ("prt_wal", "msg_fix01", self.SID, 1, 1,
                     json.dumps({"type": "text", "text": "WAL append marker"})))
        con.commit()

    def test_one_read_only_view_stays_consistent_while_a_wal_writer_appends(self):
        writer = self.writer()
        with tt._opencode_view(self.iso.opencode_db) as reader:
            before = reader.execute("SELECT COUNT(*) FROM part").fetchone()[0]
            self.append(writer)
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM part").fetchone()[0], before)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                reader.execute("DELETE FROM part")
        with tt._opencode_view(self.iso.opencode_db) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM part").fetchone()[0], before + 1)

    def test_chunks_tail_and_discovery_read_live_wal_without_copying_the_database(self):
        writer = self.writer()
        self.append(writer)
        self.assertGreater(Path(str(self.iso.opencode_db) + "-wal").stat().st_size, 0)
        with self.library(), mock.patch.object(tt._rt, "_opencode_snapshot", side_effect=OSError("copy changed")), \
                mock.patch.object(tt, "opencode_db_path", return_value=self.iso.opencode_db), \
                mock.patch.object(tt, "now_epoch", return_value=FIXTURE_NOW + 2 * tt.OPEN_QUESTION_STALE_SEC):
            before = tt.read_watermark("opencode", self.SID)
            chunk = tt.read_chunk("opencode", self.iso.opencode_db, sid=self.SID)
            tail = tt.read_pending("opencode", self.SID, self.iso.opencode_db)
            sessions = list(tt._discover("opencode", 0))
            self.assertEqual((chunk.error, tail.error), ("", ""))
            self.assertIn("[user] WAL append marker", chunk.text)
            self.assertIn("[user] WAL append marker", tail.text)
            self.assertIn(self.SID, [item["sid"] for item in sessions])
            self.assertEqual(tt.read_watermark("opencode", self.SID), before)

    def test_an_unreadable_database_keeps_its_cause_on_one_line_and_does_not_advance(self):
        self.iso.opencode_db.write_text("not a SQLite database")
        with self.library(), mock.patch.object(tt.time, "sleep"):
            before = tt.read_watermark("opencode", self.SID)
            chunk = tt.read_pending("opencode", self.SID, self.iso.opencode_db)
            self.assertIn("DatabaseError: file is not a database", chunk.error)
            self.assertNotIn("\n", chunk.error)
            self.assertLessEqual(len(chunk.error), 220)
            self.assertEqual(tt.read_watermark("opencode", self.SID), before)


class OpenCodeReadRetryTest(unittest.TestCase):
    """A snapshot of the shared OpenCode database is refused now and then; the read is tried again."""

    def chunk(self, error=""):
        return tt.Chunk("opencode", "db", 0, 0, error=error)

    def test_a_refused_snapshot_is_tried_again_until_it_reads(self):
        answers = [self.chunk("unreadable: OSError"), self.chunk("unreadable: OSError"), self.chunk()]
        calls = []
        result = tt._opencode_retry(lambda: calls.append(1) or answers[len(calls) - 1], pause=0)
        self.assertEqual((result.error, len(calls)), ("", 3))

    def test_a_read_that_keeps_failing_ends_with_the_last_error_after_a_bounded_number_of_tries(self):
        calls = []
        result = tt._opencode_retry(lambda: calls.append(1) or self.chunk("unreadable: OSError"), pause=0)
        self.assertEqual((result.error, len(calls)), ("unreadable: OSError", tt.OPENCODE_READ_TRIES))

    def test_a_clean_read_is_not_repeated(self):
        calls = []
        tt._opencode_retry(lambda: calls.append(1) or self.chunk(), pause=0)
        self.assertEqual(len(calls), 1)


class ClearHelperTest(TidyCase):
    """The detached helper's decisions, with the herdr wait and ``peer-steward.py clear`` replaced."""

    def setUp(self):
        super().setUp()
        self.fake_herdr = self.iso.root / "fake-herdr"
        self.fake_herdr.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fake_herdr.chmod(0o755)
        self.extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                      "HEARTING_TIDY_HERDR": str(self.fake_herdr), "HEARTING_TIDY_CLEAR_OBSERVE": "0.3"}
        self.calls = []

    def test_helper_env_keeps_the_herdr_socket_but_not_the_callers_pane(self):
        env = clear._helper_env({"HERDR_PANE_ID": PANE, "HERDR_TAB_ID": "t", "HERDR_ENV": "1",
                                 "HERDR_SOCKET_PATH": "/run/herdr/custom.sock", "CLAUDE_CODE_SESSION_ID": "s",
                                 "PATH": "/usr/bin"})
        self.assertEqual(env.get("HERDR_SOCKET_PATH"), "/run/herdr/custom.sock")
        self.assertEqual(env.get("PATH"), "/usr/bin")
        for dropped in ("HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_ENV", "CLAUDE_CODE_SESSION_ID"):
            self.assertNotIn(dropped, env)
        self.assertNotIn("HERDR_SOCKET_PATH", clear._helper_env({"HERDR_PANE_ID": PANE}))

    def booked(self, harness="claude", sid="sid-A"):
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "_start_helper", return_value=1):
            seat = st.resolve_seat(harness, str(self.cwd))
            clear.schedule_for_enqueue(seat, harness, sid, str(self.cwd))
            return seat, clear.read_reservation(seat.key)

    def run_helper(self, seat, req, wait="idle", verdict=None, during=None):
        def fake_wait(pane, timeout_ms):
            self.calls.append(("wait", pane))
            if during:
                during()
            return wait

        def fake_steward(path, nonce, pane):
            self.calls.append(("clear", pane, nonce))
            return dict(verdict or {"cleared": "true", "new_session": "sid-B"})

        with self.iso.patched_environ(self.extra):
            return clear.run_helper(seat.key, req["nonce"], wait=fake_wait, steward=fake_steward,
                                    sleep=lambda _s: None)

    def notices(self, seat):
        data = st.read_json(self.state / "notices" / f"{seat.key}.json")
        return [i["text"] for i in (data or {}).get("items", [])]

    def status(self, seat):
        return st.read_json(self.state / "clear" / f"{seat.key}.json")["status"]

    def test_an_idle_window_is_cleared_once_and_silently(self):
        seat, req = self.booked()
        self.assertEqual(self.run_helper(seat, req), "cleared")
        self.assertEqual([c[0] for c in self.calls], ["wait", "clear"])
        self.assertEqual(self.calls[1][1:], (PANE, req["nonce"]))
        self.assertEqual(self.notices(seat), [])
        self.assertEqual(self.status(seat), "cleared")

    def test_a_window_that_stays_busy_is_left_alone_with_one_line(self):
        seat, req = self.booked()
        self.assertEqual(self.run_helper(seat, req, wait="timeout"), "skipped")
        self.assertEqual([c[0] for c in self.calls], ["wait"])           # nothing was typed
        self.assertEqual(len(self.notices(seat)), 1)
        self.assertIn("10분", self.notices(seat)[0])
        self.assertIn("/clear", self.notices(seat)[0])

    def test_a_prompt_that_arrives_before_idle_cancels_the_clear(self):
        seat, req = self.booked()

        def typed():
            with self.iso.patched_environ(self.extra):
                st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd))
        self.assertEqual(self.run_helper(seat, req, during=typed), "skipped")
        self.assertEqual([c[0] for c in self.calls], ["wait"])           # idle came, but the input is new
        self.assertIn("새 입력", self.notices(seat)[0])

    def test_the_steward_verdicts_skip_fail_unverified_each_leave_one_line(self):
        for verdict, outcome, word in (({"cleared": "skipped", "reason": "draft"}, "skipped", "입력창에 쓰던 글"),
                                       ({"cleared": "skipped", "reason": "form-open"}, "skipped", "선택"),
                                       ({"cleared": "skipped", "reason": "draft-unknown"}, "skipped", "비어 있는지"),
                                       ({"cleared": "failed", "reason": "herdr-exit-1"}, "failed", "실패"),
                                       ({"cleared": "unverified", "reason": "new-session-not-observed"},
                                        "unverified", "확인하지 못했습니다")):
            with self.subTest(verdict=verdict):
                self.calls = []
                seat, req = self.booked()
                self.assertEqual(self.run_helper(seat, req, verdict=verdict), outcome)
                self.assertEqual([c[0] for c in self.calls], ["wait", "clear"])    # typed at most once, never again
                last = self.notices(seat)[-1]
                self.assertIn(word, last)

    def test_an_unverified_send_is_not_resent_and_becomes_cleared_when_the_new_start_shows_up(self):
        seat, req = self.booked()

        def late_start():
            with self.iso.patched_environ(self.extra):
                st.run_hook("claude", "start", "sid-B", source="clear", cwd=str(self.cwd))
        sleeps = []
        verdict = {"cleared": "unverified", "reason": "new-session-not-observed"}

        def fake_sleep(sec):
            sleeps.append(sec)
            late_start()
        with self.iso.patched_environ(self.extra):
            outcome = clear.run_helper(
                seat.key, req["nonce"], wait=lambda *_a: "idle",
                steward=lambda *_a: self.calls.append("clear") or verdict, sleep=fake_sleep)
        self.assertEqual(outcome, "cleared")
        self.assertEqual(self.calls, ["clear"])                           # one send, no second one
        self.assertEqual(self.notices(seat), [])

    def test_a_successor_that_starts_after_the_helper_gave_up_turns_the_doubt_into_a_clear(self):
        seat, req = self.booked()
        verdict = {"cleared": "unverified", "reason": "new-session-not-observed"}
        self.assertEqual(self.run_helper(seat, req, verdict=verdict), "unverified")
        self.assertIn("확인하지 못했습니다", self.notices(seat)[-1])
        shown = []
        with self.iso.patched_environ(self.extra):
            st.write_notice(seat, "[정리] 다른 결과", author_harness="claude", author_sid="sid-A")
            st.run_hook("claude", "start", "sid-B", source="clear", cwd=str(self.cwd), emit=shown.append)  # late hook
        held = st.read_json(self.state / "clear" / f"{seat.key}.json")
        self.assertEqual((held["status"], held["new_session"], held["observed"]["sid"]), ("cleared", "sid-B", "sid-B"))
        self.assertIn("다른 결과", "".join(shown))                           # only the doubt was dropped
        self.assertNotIn("확인하지 못했습니다", "".join(shown))

    def test_a_replaced_or_cancelled_helper_writes_nothing(self):
        seat, req = self.booked()
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "_start_helper", return_value=2):
            clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd))       # a newer tidy
        self.assertEqual(self.run_helper(seat, req, wait="timeout"), "superseded")
        self.assertEqual(self.notices(seat), [])
        with self.iso.patched_environ(self.extra):
            clear.cancel(seat)                                                        # then --no-clear
            self.assertEqual(clear.run_helper(seat.key, "whatever", wait=lambda *_a: "idle"), "superseded")
        self.assertEqual(self.notices(seat), [])

    def test_the_memory_notice_survives_and_the_clear_line_is_added_after_it(self):
        seat, req = self.booked()
        with self.iso.patched_environ(self.extra):
            st.write_notice(seat, "[정리] 새 기록 1건", author_harness="claude", author_sid="sid-A")
        self.run_helper(seat, req, verdict={"cleared": "skipped", "reason": "draft"})
        texts = self.notices(seat)
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].startswith("[정리] 새 기록"))
        self.assertIn("입력창", texts[1])

    def test_each_harness_names_its_own_manual_command_in_the_line(self):
        for harness, hint in (("codex", "/clear"), ("opencode", "/new")):
            seat, req = self.booked(harness=harness, sid=f"sid-{harness}")
            self.run_helper(seat, req, wait="timeout")
            self.assertIn(hint, self.notices(seat)[-1])

    def test_an_expired_booking_is_never_typed(self):
        seat, req = self.booked()
        path = self.state / "clear" / f"{seat.key}.json"
        data = st.read_json(path)
        data["deadline"] = st.now_epoch() - 1
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.run_helper(seat, req), "skipped")
        self.assertEqual(self.calls, [])
        self.assertIn("예약 시간", self.notices(seat)[0])


class ContinueHelperTest(TidyCase):
    """After a confirmed clear the same helper asks ``peer-steward.py continue`` once (both replaced)."""

    def setUp(self):
        super().setUp()
        self.fake_herdr = self.iso.root / "fake-herdr"
        self.fake_herdr.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fake_herdr.chmod(0o755)
        self.extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                      "HEARTING_TIDY_HERDR": str(self.fake_herdr), "HEARTING_TIDY_CLEAR_OBSERVE": "0.3"}
        self.calls = []

    def booked(self, harness="claude", sid="sid-A", no_continue=False, card=True):
        shutil.rmtree(self.state, ignore_errors=True)               # every case starts from an empty seat
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "_start_helper", return_value=1):
            seat = st.resolve_seat(harness, str(self.cwd))
            if card:
                with st.seat_lock(seat.key):
                    st.write_card(seat, harness, sid, "진행 중: 시험\n다음 할 일: 표식 쓰기",
                                  prompt_seq=st.read_prompt_seq(seat))
            clear.schedule_for_enqueue(seat, harness, sid, str(self.cwd), no_continue=no_continue)
            return seat, clear.read_reservation(seat.key)

    def run_helper(self, seat, req, verdict=None, cont=None, after_clear=None, extra=None):
        def fake_steward(path, nonce, pane):
            self.calls.append("clear")
            if after_clear:
                after_clear()
            return dict(verdict or {"cleared": "true", "new_session": "sid-B"})

        def fake_continue(path, nonce, pane):
            self.calls.append(("continue", pane, nonce, Path(path).name))
            return dict(cont or {"continued": "true"})
        with self.iso.patched_environ({**self.extra, **(extra or {})}):
            return clear.run_helper(seat.key, req["nonce"], wait=lambda *_a: "idle", steward=fake_steward,
                                    continuer=fake_continue, sleep=lambda _s: None)

    def booking(self, seat):
        return st.read_json(self.state / "clear" / f"{seat.key}.json")

    def notices(self, seat):
        data = st.read_json(self.state / "notices" / f"{seat.key}.json")
        return [i["text"] for i in (data or {}).get("items", [])]

    def test_a_confirmed_clear_is_continued_once_and_silently(self):
        seat, req = self.booked()
        self.assertEqual(self.run_helper(seat, req), "continued")
        self.assertEqual(self.calls, ["clear", ("continue", PANE, req["nonce"], f"{seat.key}.json")])
        held = self.booking(seat)
        self.assertEqual((held["status"], held["new_session"], held["continued"]["state"]), ("cleared", "sid-B", "sent"))
        self.assertEqual(self.notices(seat), [])

    def test_only_a_confirmed_clear_is_continued(self):
        for verdict in ({"cleared": "skipped", "reason": "draft"}, {"cleared": "failed", "reason": "herdr-exit-1"},
                        {"cleared": "unverified", "reason": "new-session-not-observed"}):
            with self.subTest(verdict=verdict):
                self.calls = []
                seat, req = self.booked()
                self.run_helper(seat, req, verdict=verdict)
                self.assertEqual(self.calls, ["clear"])
                self.assertNotIn("continued", self.booking(seat))

    def test_a_clear_confirmed_by_the_late_start_hook_is_continued(self):
        seat, req = self.booked()

        def late_start(_sec):
            with self.iso.patched_environ(self.extra):
                st.run_hook("claude", "start", "sid-B", source="clear", cwd=str(self.cwd))
        with self.iso.patched_environ(self.extra):
            outcome = clear.run_helper(
                seat.key, req["nonce"], wait=lambda *_a: "idle",
                steward=lambda *_a: {"cleared": "unverified", "reason": "new-session-not-observed"},
                continuer=lambda *a: self.calls.append("continue") or {"continued": "true"}, sleep=late_start)
        self.assertEqual((outcome, self.calls), ("continued", ["continue"]))

    def test_no_continue_and_no_card_clear_without_typing_after_it(self):
        for kwargs in ({"no_continue": True}, {"card": False}):
            with self.subTest(**kwargs):
                self.calls = []
                seat, req = self.booked(**kwargs)
                self.assertEqual(self.run_helper(seat, req), "cleared")
                self.assertEqual(self.calls, ["clear"])
                self.assertEqual(self.notices(seat), [])

    def test_a_prompt_a_new_card_or_a_handoff_after_the_clear_stops_it_quietly(self):
        def prompt():
            with self.iso.patched_environ(self.extra):
                st.run_hook("claude", "prompt", "sid-B", cwd=str(self.cwd))     # the person typed first

        def new_card():
            with self.iso.patched_environ(self.extra):
                seat = st.resolve_seat("claude", str(self.cwd))
                with st.seat_lock(seat.key):
                    st.write_card(seat, "claude", "sid-B", "새 카드")

        def handed():
            with self.iso.patched_environ(self.extra):
                st.mark_card_handed_off(st.resolve_seat("claude", str(self.cwd)))
        for name, during, reason in (("prompt", prompt, "new-input"), ("card", new_card, "card-changed"),
                                     ("handoff", handed, "handed-off")):
            with self.subTest(name):
                self.calls = []
                seat, req = self.booked()
                self.assertEqual(self.run_helper(seat, req, after_clear=during), "cleared")
                self.assertEqual(self.calls, ["clear"])                 # nothing typed after the clear
                self.assertEqual((self.booking(seat)["continued"]["state"],
                                  self.booking(seat)["continued"]["reason"]), ("skipped", reason))
                self.assertEqual(self.notices(seat), [])

    def test_each_continue_verdict_is_recorded_and_only_misses_the_person_did_not_cause_leave_a_line(self):
        cases = (({"continued": "skipped", "reason": "draft"}, None),
                 ({"continued": "skipped", "reason": "new-input"}, None),
                 ({"continued": "skipped", "reason": "not-idle-working"}, "입력 대기"),
                 ({"continued": "skipped", "reason": "card-not-delivered"}, "카드를 받지"),
                 ({"continued": "skipped", "reason": "target-changed"}, "세션이 바뀌었습니다"),
                 ({"continued": "failed", "reason": "herdr-exit-1"}, "넣지 못했습니다"),
                 ({"continued": "unverified", "reason": "submission-not-observed"}, "확인하지 못했습니다"))
        for cont, word in cases:
            with self.subTest(cont=cont):
                self.calls = []
                seat, req = self.booked()
                self.assertEqual(self.run_helper(seat, req, cont=cont), "cleared")
                self.assertEqual(len([c for c in self.calls if c != "clear"]), 1)     # asked once, never again
                state = {"failed": "failed", "unverified": "unverified"}.get(cont["continued"], "skipped")
                self.assertEqual(self.booking(seat)["continued"]["state"], state)
                if word is None:
                    self.assertEqual(self.notices(seat), [])
                else:
                    self.assertEqual(len(self.notices(seat)), 1)
                    self.assertIn(word, self.notices(seat)[0])
                    self.assertIn("이어서해", self.notices(seat)[0])

    def test_a_continue_window_that_passed_types_nothing_and_says_so(self):
        seat, req = self.booked()
        self.assertEqual(self.run_helper(seat, req, extra={"HEARTING_TIDY_CONTINUE_WINDOW": "-1"}), "cleared")
        self.assertEqual(self.calls, ["clear"])
        self.assertIn("예약 시간", self.notices(seat)[0])

    def test_a_newer_tidy_during_the_clear_leaves_nothing_to_continue(self):
        seat, req = self.booked()

        def newer():
            with self.iso.patched_environ(self.extra), mock.patch.object(clear, "_start_helper", return_value=2):
                clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd))
        self.assertEqual(self.run_helper(seat, req, after_clear=newer), "cleared")
        self.assertEqual(self.calls, ["clear"])
        self.assertEqual(self.notices(seat), [])

    def test_the_claim_moves_pending_to_sending_exactly_once(self):
        seat, req = self.booked()
        with self.iso.patched_environ(self.extra):
            path = clear.reservation_path(seat.key)
            clear._finish(seat.key, req["nonce"], "cleared", observed="sid-B")
            self.assertEqual(clear.validate_continue(path, req["nonce"])[1], "")
            self.assertEqual(clear.validate_continue(path, "other"), (None, "superseded"))
            claimed, why = clear.claim_continue(path, req["nonce"])
            self.assertEqual((claimed["continued"]["state"], why), ("sending", ""))
            self.assertEqual(clear.claim_continue(path, req["nonce"]), (None, "superseded"))
            self.assertEqual(clear.validate_continue(path, req["nonce"]), (None, "superseded"))
            self.assertEqual(clear.validate_request(path, req["nonce"]), (None, "superseded"))  # no second clear


class SeatHandoverTest(TidyCase):
    """A cleared window's successor at the same pane answers for the predecessor's depth-1 attempts."""

    def setUp(self):
        super().setUp()
        self.jobs = self.iso.root / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir()
        self.rows = []
        import dispatch_seat_handover as handover
        self.handover = handover

    def row(self, aid, parent="sid-A", status="open", route="rt-1", node="one-shot", depth="1",
            worker="owner", harness="claude", digest="sha256:aa", route_file="/tmp/rt-1.json"):
        meta = {"attempt_id": aid, "parent_sid": parent, "parent_harness": harness, "dispatch_depth": depth,
                "worker_type": worker, "route_id": route, "route_hash": digest, "route_node": node,
                "route_file": route_file}
        self.rows.append(f"2026-10-01T00:00:00Z\t{status}\t/w\t/w\tslug\t" + ",".join(f"{k}={v}" for k, v in meta.items()))
        self.jobs.write_text("\n".join(self.rows) + "\n", encoding="utf-8")
        return meta

    def seat(self, pane=PANE):
        return st.resolve_seat("claude", str(self.cwd), {"HERDR_PANE_ID": pane})

    def snapshot(self, sid="sid-A", harness="claude", pane=PANE, now=None):
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            seat = self.seat(pane)
            with st.seat_lock(seat.key):
                st.record_event(seat, harness, sid, "start", cwd=str(self.cwd), now=now)
                return self.handover.write_snapshot_locked(seat, harness, sid, now=now, jobs=self.jobs)

    def start(self, sid, harness="claude", event="start", source="clear", pane=PANE):
        out = []
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            st.run_hook(harness, event, sid, source=source, cwd=str(self.cwd),
                        env={"HERDR_PANE_ID": pane}, emit=out.append)
            seat = self.seat(pane)
            return out, self.handover.handover_rows(seat)

    def effective(self, meta):
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            return self.handover.effective_parent(meta, self.jobs)

    def test_the_snapshot_takes_only_live_route_bound_depth1_attempts_of_the_session(self):
        keep = self.row("att-keep")
        self.row("att-depth2", depth="2")
        self.row("att-free", route="")
        self.row("att-other", parent="sid-X")
        self.row("att-done", status="done", route="rt-9")
        self.assertEqual(self.snapshot(), 1)
        with self.iso.patched_environ():
            snap = self.handover.read_snapshot(self.seat().key)
        self.assertEqual([b["attempt"] for b in snap["bindings"]], [keep["attempt_id"]])
        self.assertEqual(snap["from"], {"harness": "claude", "sid": "sid-A"})

    def test_no_live_attempt_leaves_no_snapshot(self):
        self.row("att-1")
        self.assertEqual(self.snapshot(), 1)
        self.jobs.write_text("", encoding="utf-8")
        self.assertEqual(self.snapshot(), 0)
        with self.iso.patched_environ():
            self.assertIsNone(self.handover.read_snapshot(self.seat().key))

    def test_a_confirmed_clear_start_records_one_row_and_the_successor_answers(self):
        meta = self.row("att-1")
        self.snapshot(now=st.now_epoch() - 5)
        self.assertEqual(self.effective(meta), "sid-A")                 # nothing handed over yet
        out, rows = self.start("sid-B")
        self.assertEqual([(r["from"], r["sid"], r["source"]) for r in rows], [("sid-A", "sid-B", "clear")])
        self.assertEqual(self.effective(meta), "sid-B")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            self.assertTrue(self.handover.owns(meta, "sid-B", self.jobs))
            self.assertTrue(self.handover.owns(meta, "sid-A", self.jobs))     # the registered parent keeps its name
            self.assertFalse(self.handover.owns(meta, "sid-Z", self.jobs))
        self.assertEqual(meta["parent_sid"], "sid-A")                    # the registry row is untouched
        again, rows2 = self.start("sid-B", event="prompt", source="")
        self.assertEqual(len(rows2), 1)                                  # the same A -> B again is a no-op

    def test_the_card_gets_the_verified_route_and_the_existing_resume_command(self):
        self.row("att-1", route_file="/tmp/rt-1.json")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = self.seat()
            with st.seat_lock(seat.key):
                st.record_event(seat, "claude", "sid-A", "start", cwd=str(self.cwd), now=st.now_epoch() - 9)
                st.write_card(seat, "claude", "sid-A", "카드 본문", cwd=str(self.cwd))
                self.handover.write_snapshot_locked(seat, "claude", "sid-A", jobs=self.jobs)
        out, _ = self.start("sid-B")
        text = "\n".join(out)
        self.assertIn("[이어받은 진행 작업] route=rt-1", text)
        self.assertIn("capability-route.py start --route /tmp/rt-1.json --jobs " + str(self.jobs), text)
        self.assertIn("카드 본문", text)

    def test_only_a_confirmed_clear_hands_over(self):
        meta = self.row("att-1")
        self.snapshot(now=st.now_epoch() - 5)
        self.assertEqual(self.start("sid-B", source="startup")[1], [])          # a fresh start is not a clear
        self.assertEqual(self.start("sid-B2", event="prompt", source="")[1], [])
        self.assertEqual(self.effective(meta), "sid-A")

    def test_the_clear_booking_confirms_a_start_that_carries_no_source(self):
        meta = self.row("att-1")
        self.snapshot(now=st.now_epoch() - 5)
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = self.seat()
            with st.seat_lock(seat.key):
                clear._write_reservation({"schema": 1, "nonce": "n", "status": "reserved", "created": st.now_epoch() - 4,
                                          "deadline": st.now_epoch() + 500, "seat": seat.as_dict(), "harness": "claude",
                                          "sid": "sid-A", "card_generation": 0, "prompt_seq": 0})
        out, rows = self.start("sid-B", source="startup")
        self.assertEqual([(r["from"], r["sid"]) for r in rows], [("sid-A", "sid-B")])
        self.assertEqual(self.effective(meta), "sid-B")

    def test_a_session_older_than_the_snapshot_is_not_its_successor(self):
        self.row("att-1")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = self.seat()
            with st.seat_lock(seat.key):
                st.record_event(seat, "claude", "sid-old", "start", cwd=str(self.cwd), now=st.now_epoch() - 600)
        self.snapshot()
        self.assertEqual(self.start("sid-old", source="clear")[1], [])

    def test_another_pane_or_harness_never_receives_the_attempts(self):
        meta = self.row("att-1")
        self.snapshot(now=st.now_epoch() - 5)
        self.assertEqual(self.start("sid-B", pane="test:pane-b")[1], [])
        self.assertEqual(self.start("sid-B", harness="codex")[1], [])
        self.assertEqual(self.effective(meta), "sid-A")

    def test_no_pane_means_no_handover(self):
        meta = self.row("att-1")
        with self.iso.patched_environ():
            seat = st.resolve_seat("claude", str(self.cwd), {})
            self.assertEqual(seat.kind, "project")
            with st.seat_lock(seat.key):
                self.assertEqual(self.handover.write_snapshot_locked(seat, "claude", "sid-A", jobs=self.jobs), 0)
            self.assertEqual(self.handover.storage_recipients("sid-B", {}), [("sid-B", None)])
        self.assertEqual(self.effective(meta), "sid-A")

    def test_two_successors_of_one_session_are_refused_the_second(self):
        meta = self.row("att-1")
        self.snapshot(now=st.now_epoch() - 5)
        self.assertEqual(len(self.start("sid-B")[1]), 1)
        self.assertEqual(len(self.start("sid-C")[1]), 1)                # A -> C is refused: still one row
        self.assertEqual(self.effective(meta), "sid-B")

    def test_the_next_clear_hands_over_only_through_the_successors_own_tidy(self):
        meta = self.row("att-1")
        self.snapshot(now=st.now_epoch() - 9)
        self.start("sid-B")
        self.assertEqual(self.start("sid-C", source="clear")[1][-1]["sid"], "sid-B")     # no B snapshot: no B -> C
        self.snapshot(sid="sid-B", now=st.now_epoch() - 5)           # B tidies: its snapshot still binds the route
        rows = self.start("sid-D")[1]
        self.assertEqual([(r["from"], r["sid"]) for r in rows], [("sid-A", "sid-B"), ("sid-B", "sid-D")])
        self.assertEqual(self.effective(meta), "sid-D")

    def test_a_replacement_attempt_of_the_same_route_node_follows_the_binding(self):
        self.row("att-1")
        self.snapshot(now=st.now_epoch() - 5)
        self.start("sid-B")
        replacement = self.row("att-2")                                   # same route, hash and node, still parent A
        self.assertEqual(self.effective(replacement), "sid-B")
        other_node = self.row("att-3", node="other")
        self.assertEqual(self.effective(other_node), "sid-A")             # a different node was never bound
        self.assertEqual(self.effective(self.row("att-4", digest="sha256:bb")), "sid-A")

    def test_opencode_hands_over_at_the_first_message_of_the_new_session(self):
        meta = self.row("att-1", parent="ses_A", harness="opencode")
        self.snapshot(sid="ses_A", harness="opencode", now=st.now_epoch() - 5)
        rows = self.start("ses_B", harness="opencode", event="prompt", source="")[1]
        self.assertEqual([(r["from"], r["sid"]) for r in rows], [("ses_A", "ses_B")])
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            self.assertEqual(self.handover.effective_parent(meta, self.jobs), "ses_B")

    def test_storage_stays_with_the_registered_parent_and_only_bound_attempts_reach_the_taker(self):
        self.row("att-1")
        self.snapshot(now=st.now_epoch() - 5)
        self.start("sid-B")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            recipients = self.handover.storage_recipients("sid-B")
            self.assertEqual(recipients, [("sid-B", None), ("sid-A", frozenset({"att-1"}))])
            allowed = recipients[1][1]
            self.assertTrue(self.handover.record_for_session({"attempt_ids": ["att-1"]}, allowed))
            self.assertFalse(self.handover.record_for_session({"attempt_ids": ["att-1", "att-9"]}, allowed))
            self.assertFalse(self.handover.record_for_session({"attempt_ids": []}, allowed))
            self.assertEqual(self.handover.storage_recipients("sid-A"), [("sid-A", None)])

    def test_folding_the_ledger_keeps_the_handover_rows_and_leaves_sessions_alone(self):
        meta = self.row("att-1")
        self.snapshot(now=st.now_epoch() - 5)
        self.start("sid-B")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = self.seat()
            with st.seat_lock(seat.key):
                for index in range(st.LEDGER_FOLD_LINES + 5):
                    st.record_event(seat, "claude", f"sid-n{index}", "start", cwd=str(self.cwd))
            self.assertEqual([(r["from"], r["sid"]) for r in self.handover.handover_rows(seat)], [("sid-A", "sid-B")])
            self.assertTrue(st.latest_session(seat)["sid"].startswith("sid-n"))     # the rows are not sessions
        self.assertEqual(self.effective(meta), "sid-B")

    def retire(self, old, old_harness, new, new_harness, pane=PANE_BESIDE):
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            return self.handover.record_retire_handover(old, old_harness, new, new_harness,
                                                        env={"HERDR_PANE_ID": pane}, jobs=self.jobs)

    def test_a_retire_from_the_pane_beside_hands_a_codex_route_to_a_claude_successor(self):
        # RA-2 (decision 5f0f10): BC [62] codex -> Claude successor; the registry row stays as it is.
        meta = self.row("att-1", parent="sid-A", harness="codex")
        self.row("att-depth2", parent="sid-A", harness="codex", depth="2")
        row = self.retire("sid-A", "codex", "sid-B", "claude")
        self.assertEqual((row["from"], row["sid"], row["harness"], row["source"]), ("sid-A", "sid-B", "claude", "retire"))
        self.assertEqual([(b["attempt"], b["parent"]) for b in row["bindings"]], [("att-1", "sid-A")])
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE_BESIDE}):
            self.assertEqual(self.handover.effective_parent(meta, self.jobs), "sid-B")
            self.assertEqual(self.handover.effective_parent_harness(meta, self.jobs), "claude")
            self.assertTrue(self.handover.owns(meta, "sid-B", self.jobs))
            self.assertEqual(self.handover.storage_recipients("sid-B", {"HERDR_PANE_ID": PANE_BESIDE}),
                             [("sid-B", None), ("sid-A", frozenset({"att-1"}))])
            self.assertEqual(self.handover.read_snapshot(self.seat(PANE_BESIDE).key)["from"],
                             {"harness": "claude", "sid": "sid-B"})        # the seat now names the successor
        self.assertEqual(meta["parent_sid"], "sid-A")
        self.assertEqual(self.retire("sid-A", "codex", "sid-B", "claude"), row)    # repeated: the same row
        self.assertIsNone(self.retire("sid-A", "codex", "sid-C", "claude", pane="test:pane-c"))  # A answers for nothing now
        self.assertEqual(self.effective(meta), "sid-B")

    def test_a_retire_after_a_clear_carries_the_whole_chain_across_panes(self):
        meta = self.row("att-1")
        self.snapshot(now=st.now_epoch() - 5)
        self.start("sid-B")                                              # A -> B by /clear at PANE
        self.retire("sid-B", "claude", "sid-C", "codex")                 # B -> C by retire at PANE_BESIDE
        self.assertEqual(self.effective(meta), "sid-C")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE_BESIDE}):
            recipients = dict(self.handover.storage_recipients("sid-C", {"HERDR_PANE_ID": PANE_BESIDE}))
        self.assertEqual(recipients["sid-A"], frozenset({"att-1"}))      # records stay under the registered parent

    def test_a_retire_with_nothing_live_hands_nothing(self):
        self.row("att-done", parent="sid-A", status="done")
        self.assertIsNone(self.retire("sid-A", "codex", "sid-B", "claude"))
        self.assertIsNone(self.retire("sid-A", "codex", "sid-A", "codex"))

    def test_a_broken_state_answers_the_registered_parent(self):
        meta = self.row("att-1")
        with mock.patch.object(self.handover, "_all_snapshots", side_effect=OSError("boom")):
            self.assertEqual(self.handover.effective_parent(meta, self.jobs), "sid-A")


if __name__ == "__main__":
    unittest.main()
