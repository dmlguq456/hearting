#!/usr/bin/env python3
"""session-tidy shared state: cards, hook injection, ledger, notices, transcript reading.

Every subprocess and every in-process call runs inside ``tidy_isolation`` (a
``/var/tmp`` root for HOME, XDG_*, MEM_STORE, remote disabled, no worker/pane
markers unless a test sets one on purpose).
"""

from __future__ import annotations

import contextlib
import io
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
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "utilities"))

from tidy_isolation import isolated_env  # noqa: E402
import session_tidy as st  # noqa: E402
import pane_ownership  # noqa: E402
import session_tidy_clear as clear  # noqa: E402
import session_tidy_runner as runner  # noqa: E402
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
        # These are card/clear fixtures on an already verified pane. Actual
        # foreground/inherited-daemon provenance is tested with real ptys in
        # utilities/pane_ownership.test.py; never add a production test bypass.
        proof = mock.patch.object(pane_ownership, "verified_pane", side_effect=lambda pane, *a, **kw: pane or "")
        proof.start()
        self.addCleanup(proof.stop)

    def cli(self, *args, pane=PANE, extra=None, input=None, cwd=None):
        env = dict(extra or {})
        if pane:
            env["HERDR_PANE_ID"] = pane
        code = (f"import sys; sys.path.insert(0, {str(ROOT / 'utilities')!r}); "
                "import pane_ownership; "
                "pane_ownership.verified_pane=lambda pane,*a,**kw: pane or ''; "
                "import session_tidy; sys.argv=['session_tidy.py',*sys.argv[1:]]; "
                "raise SystemExit(session_tidy.main())")
        return self.iso.run([sys.executable, "-c", code, *args], input=input, extra=env, cwd=cwd or self.cwd)

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


class ProjectFallbackTest(TidyCase):

    def test_project_seat_sid_round_trips_without_changing_legacy_constructors(self):
        with self.library({"HERDR_PANE_ID": ""}):
            project = st.resolve_seat("claude", str(self.cwd), sid="sid-roundtrip")
            pane5 = st.Seat("pane", "pane-key", "pane-id", "claude", "")
            pane6 = st.Seat("pane", "pane-key", "pane-id", "claude", "", None)
            self.assertEqual(st.seat_from_dict(project.as_dict()), project)
            self.assertEqual(st.Seat(**project.as_dict()), project)
            self.assertEqual((pane5.sid, pane6.sid), ("", ""))

    def test_legacy_project_card_stays_with_its_author_and_new_author_gets_a_new_slot(self):
        with self.library({"HERDR_PANE_ID": ""}):
            shared = st.seat_for_project("claude", st.project_key_for(str(self.cwd)))
            author_a = st.dataclasses.replace(shared, sid="legacy-A")
            author_b = st.dataclasses.replace(shared, sid="new-B")
            with st.seat_lock(shared.key):
                st.write_card(shared, "claude", "legacy-A", "legacy body", cwd=str(self.cwd))
                self.assertEqual(st.card_key(author_a), shared.key)
                continued = st.write_card(author_a, "claude", "legacy-A", "continued body", cwd=str(self.cwd))
                separate = st.write_card(author_b, "claude", "new-B", "new body", cwd=str(self.cwd))
            self.assertEqual(continued["generation"], 2)
            self.assertEqual(separate["generation"], 1)
            self.assertEqual(st.card_key(author_b), st._digest("project-card", shared.key, "new-B"))
            self.assertTrue((self.state / "card-history" / shared.key / "00000001.json").is_file())
            self.assertEqual(st.read_latest_project_card(shared)[0]["body"], "new body")

    def test_latest_project_lookup_uses_a_fixed_pointer_without_card_enumeration(self):
        with self.library({"HERDR_PANE_ID": ""}):
            seat = st.resolve_seat("claude", str(self.cwd), sid="pointer-A")
            with st.seat_lock(seat.key):
                st.write_card(seat, "claude", "pointer-A", "pointed body", cwd=str(self.cwd))
            with mock.patch.object(Path, "glob", side_effect=AssertionError("card enumeration")):
                card, chosen = st.read_latest_project_card(seat)
            self.assertEqual((card["body"], chosen.sid), ("pointed body", "pointer-A"))

    def test_live_project_authors_keep_separate_cards_and_do_not_receive_each_others(self):
        self.hook("claude", "start", "sid-A", pane=None)
        self.hook("claude", "start", "sid-B", pane=None)
        self.card("sid-B", "B1", pane=None)
        self.card("sid-A", "A1", pane=None)
        with self.library({"HERDR_PANE_ID": ""}):
            a = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            b = st.resolve_seat("claude", str(self.cwd), sid="sid-B")
            self.assertEqual(a.key, b.key)
            self.assertNotEqual(st.card_key(a), st.card_key(b))
            self.assertEqual(st.read_latest_card(a)["body"], "A1")
            self.assertEqual(st.read_latest_card(b)["body"], "B1")
        for sid in ("sid-A", "sid-B"):
            self.assertEqual(self.hook("claude", "prompt", sid, pane=None), "")
            self.assertEqual(self.hook("claude", "start", sid, "--source", "resume", pane=None), "")
        self.card("sid-A", "A2", pane=None)
        self.card("sid-B", "B2", pane=None)
        self.assertEqual(self.hook("claude", "prompt", "sid-A", pane=None), "")
        self.assertEqual(self.hook("claude", "prompt", "sid-B", pane=None), "")
        with self.library({"HERDR_PANE_ID": ""}):
            self.assertEqual(st.read_latest_card(a)["generation"], 2)
            self.assertEqual(st.read_latest_card(b)["generation"], 2)
        self.assertEqual(len(list((self.state / "cards").glob("*.json"))), 2)

    def test_project_compaction_prefers_the_callers_newer_card_over_the_seat_latest(self):
        self.hook("claude", "start", "sid-A", pane=None)
        self.hook("claude", "start", "sid-B", pane=None)
        self.card("sid-A", "A1", pane=None)
        self.card("sid-B", "B1", pane=None)
        resumed = self.hook("claude", "start", "sid-A", "--source", "compact", pane=None)
        self.assertIn("A1", resumed)
        self.assertNotIn("B1", resumed)
        self.assertEqual(self.hook("claude", "prompt", "sid-A", pane=None), "")
        self.assertEqual(self.hook("claude", "prompt", "sid-B", pane=None), "")

    def test_project_card_clear_is_delivered_once_for_claude_and_codex(self):
        for harness, author, successor, following in (
                ("claude", "claude-author", "claude-next", "claude-last"),
                ("codex", "codex-author", "codex-next", "codex-last")):
            self.card(author, f"{harness}-card", harness=harness, pane=None)
            first = self.hook(harness, "start", successor, "--source", "clear", pane=None)
            self.assertIn(f"{harness}-card", first)
            self.assertEqual(self.hook(harness, "prompt", successor, pane=None), "")
            self.assertEqual(self.hook(harness, "start", following, "--source", "clear", pane=None), "")

    def test_late_project_memory_revision_is_retried_after_failed_emit_once(self):
        self.hook("claude", "start", "author-A", pane=None)
        self.card("author-A", "layer A", pane=None)
        self.assertIn("layer A", self.hook("claude", "start", "successor-C", pane=None))
        with self.library({"HERDR_PANE_ID": ""}):
            author = st.resolve_seat("claude", str(self.cwd), sid="author-A")
            st.update_memory_refs(author, batch="late-B", status="applied", refs=[{"id": "mem-late"}],
                                 now=200)

            def broken(_text):
                raise BrokenPipeError()

            with self.assertRaises(BrokenPipeError):
                st.run_hook("claude", "prompt", "successor-C", cwd=str(self.cwd), now=201, emit=broken)
        got = self.hook("claude", "prompt", "successor-C", pane=None)
        self.assertIn("mem-late", got)
        self.assertNotIn("[세션 카드]", got)
        self.assertEqual(self.hook("claude", "prompt", "successor-C", pane=None), "")
        with self.library({"HERDR_PANE_ID": ""}):
            receipt = st.read_json(st._consumed_path(author))
        self.assertEqual((receipt["generation"], receipt["memory_revision"]), (1, 1))

    def test_legacy_shared_project_card_is_delivered_once_without_moving_its_files(self):
        self.hook("claude", "start", "legacy-author", pane=None)
        with self.library({"HERDR_PANE_ID": ""}):
            seat = st.seat_for_project("claude", st.project_key_for(str(self.cwd)))
            with st.seat_lock(seat.key):
                st.write_card(seat, "claude", "legacy-author", "legacy handoff", cwd=str(self.cwd))
            old_path = st.card_json_path(seat)
        received = self.hook("claude", "start", "legacy-next", pane=None)
        self.assertIn("legacy handoff", received)
        self.assertEqual(self.hook("claude", "start", "legacy-following", pane=None), "")
        self.assertTrue(old_path.is_file())
        self.assertFalse((self.state / "project-latest" / f"{seat.key}.json").exists())

    def test_project_delivery_requires_strict_replacement_but_unknown_author_is_eligible(self):
        with self.library({"HERDR_PANE_ID": ""}):
            seat = st.resolve_seat("claude", str(self.cwd), sid="same-time-author")
            st.record_event(seat, "claude", "same-time-author", "start", now=100)
            st.write_card(seat, "claude", "same-time-author", "strict card", cwd=str(self.cwd), now=150)
            st.record_event(seat, "claude", "same-time-author", "prompt", now=200)
            # Equal first_seen/last_seen is not a replacement.
            output = st.run_hook("claude", "start", "equal-successor", cwd=str(self.cwd), now=200)
            self.assertEqual(output, "")
            # A valid card whose author is absent from the ledger retains notice-compatible delivery.
            unknown = st.dataclasses.replace(seat, sid="missing-ledger-author")
            st.write_card(unknown, "claude", "missing-ledger-author", "unknown author card",
                          cwd=str(self.cwd), now=300)
            self.assertIn("unknown author card",
                          st.run_hook("claude", "start", "unknown-author-successor", cwd=str(self.cwd), now=301))

    def test_project_resolve_caller_keeps_ledger_seat_but_never_borrows_its_sid(self):
        self.hook("claude", "start", "ledger-author", pane=None)
        with self.library({"HERDR_PANE_ID": ""}):
            seat, harness, sid = st.resolve_caller(None, None, str(self.cwd))
        self.assertEqual((seat.kind, harness), ("project", "claude"))
        self.assertTrue(sid.startswith("unknown-"))
        self.assertEqual(seat.sid, sid)
        self.assertNotEqual(sid, "ledger-author")


class SeatTest(TidyCase):

    def test_verified_pane_is_the_seat_else_harness_and_project(self):
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
        self.proven = {}
        proof = mock.patch.object(pane_ownership, "verified_pane", side_effect=self._verified_pane)
        proof.start()
        self.addCleanup(proof.stop)

    def _verified_pane(self, pane, harness, sid=None, **kwargs):
        if harness != "codex":
            return pane or ""
        expected = self.proven.get(sid)
        endpoint = str(kwargs.get("socket_path") or "")
        return pane if expected == (pane, endpoint) else ""

    def prove(self, sid, pane, socket_path=""):
        self.proven[sid] = (pane, socket_path)

    def agents(self, rows):
        self.agents_file.write_text(json.dumps({"id": "cli:agent:list", "result": {"agents": rows}}), encoding="utf-8")

    def agent(self, pane, sid, harness="codex", cwd=None):
        return {"agent": harness, "pane_id": pane, "agent_status": "idle", "cwd": str(cwd or self.cwd),
                "foreground_cwd": str(cwd or self.cwd),
                "agent_session": {"kind": "id", "source": f"herdr:{harness}", "value": sid}}

    def seat(self, harness="codex", sid="t-A", source="", cwd=None):
        with self.iso.patched_environ(self.env):
            return st.resolve_seat(harness, str(cwd or self.cwd), None, sid, source)

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
        self.prove("t-A", "w:p1")
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
        self.assertEqual((own.kind, own.pane, own.sid), ("project", "", "t-A"))
        self.assertEqual(claude.pane, "w:p5")
        self.assertFalse(self.calls.exists(), self.calls.read_text() if self.calls.exists() else "")

    def test_named_herdr_server_without_its_socket_keeps_codex_pane_unknown(self):
        self.agents([self.agent("w:p1", "t-A")])
        self.prove("t-A", "w:p1")
        env = {**self.env, "AGENT_HERDR_SESSION": "secondary"}
        with self.iso.patched_environ(env):
            seat = st.resolve_seat("codex", str(self.cwd), None, "t-A")
        self.assertEqual((seat.kind, seat.pane, seat.sid), ("project", "", "t-A"))
        self.assertFalse(self.calls.exists())

    def test_a_pane_whose_ledger_knows_the_thread_keeps_it_while_herdr_shows_the_old_session(self):
        self.agents([self.agent("w:p1", "t-A"), self.agent("w:p2", "t-X")])
        self.assertEqual(self.seat(sid="t-B").kind, "project")      # nobody knows t-B
        self.know("w:p1", "t-B")
        self.assertEqual(self.seat(sid="t-B").kind, "project")      # ledger is only a hint
        self.prove("t-A", "w:p1")
        self.prove("t-B", "w:p1")
        self.assertEqual(self.seat(sid="t-B").pane, "w:p1")
        self.assertEqual(self.seat(sid="t-C").kind, "project")
        self.know("w:p2", "t-B")                                     # two panes claim it: no decision
        self.assertEqual(self.seat(sid="t-B").kind, "project")
        self.agents([self.agent("w:p1", "t-B"), self.agent("w:p2", "t-X")])
        self.assertEqual(self.seat(sid="t-B").pane, "w:p1")         # herdr's own value is decisive
        self.agents([self.agent("w:p3", "t-Y")])                     # the pane is gone: its ledger is not consulted
        self.proven.pop("t-B", None)
        self.assertEqual(self.seat(sid="t-B").kind, "project")

    def test_the_first_hook_after_a_clear_belongs_to_the_one_window_waiting_for_its_successor(self):
        self.agents([self.agent("w:p1", "t-A"), self.agent("w:p2", "t-X", cwd=self.iso.root / "else")])
        self.book("w:p1", "t-A")
        self.prove("t-B", "w:p1")
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
        self.prove("t-B", "w:p1")
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
            st.run_hook("codex", "start", "t-B", source="clear", cwd=str(self.cwd), emit=out.append)
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
        self.prove("t-A", "w:p1")
        self.prove("t-B", "w:p1")
        with self.iso.patched_environ(self.env):
            seat = self.seat(sid="t-A")
            self.assertEqual(seat.pane, "w:p1")
            st.write_card(seat, "codex", "t-A", "표식-데몬", cwd=str(self.cwd))
            st.run_hook("codex", "start", "t-A", cwd=str(self.cwd), emit=lambda _t: None)
        # /clear: herdr keeps showing t-A, the auto-clear booking waits; t-B's start hook is the first sign of it.
        self.book("w:p1", "t-A")
        out = []
        with self.iso.patched_environ(self.env):
            st.run_hook("codex", "start", "t-B", source="clear", cwd=str(self.cwd), emit=out.append)
        self.assertIn("표식-데몬", "".join(out))
        with clear_booking_seen(self, "w:p1") as booking:
            self.assertEqual(booking["observed"]["sid"], "t-B")      # the clear helper sees its successor
        # the booking is replaced (a second tidy by t-B) -- t-B keeps its pane through the ledger
        self.book("w:p1", "t-B")
        self.assertEqual(self.seat(sid="t-B").pane, "w:p1")
        # a project-seat session of the same project does not get the pane's card
        out = []
        with self.iso.patched_environ(self.env):
            st.run_hook("codex", "start", "t-Z", cwd=str(self.cwd), emit=out.append)
        self.assertEqual("".join(out), "")

    def test_enqueue_books_the_auto_clear_for_the_pane_found_in_herdr(self):
        self.agents([self.agent("w:p1", "t-A")])
        self.prove("t-A", "w:p1")
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
        self.prove("t-A", "w:p1")
        with self.iso.patched_environ(self.env):
            self.assertIsNone(handover.pane_seat({}, "codex", "t-A"))
            self.assertEqual(handover.pane_seat(os.environ, "codex", "t-A").pane, "w:p1")
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
        activity = [line for line in lines if line.get("event") not in {"tidy-claim", "real-prompt"}]
        self.assertLessEqual(len(activity), 4)

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


class CompactObservationTest(TidyCase):
    SID = "sid-compact"

    def append_claude(self, path, row):
        with path.open("ab") as handle:
            handle.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))

    def claude_row(self, kind, uid, text="", stop=""):
        return {"type": kind, "uuid": uid,
                "message": {"stop_reason": stop, "content": [{"type": "text", "text": text}]}}

    def test_full_compact_false_idle_waits_for_the_later_end_turn_and_keeps_recent_tail(self):
        path = self.iso.root / f"{self.SID}.jsonl"
        self.append_claude(path, self.claude_row("user", "u1", "작업 시작"))
        self.append_claude(path, self.claude_row("assistant", "a1", "도구 호출", stop="tool_use"))
        self.append_claude(path, {"type": "user", "uuid": "u-tool", "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "private output"}]}})
        self.append_claude(path, {"type": "assistant", "uuid": "summary", "isCompactSummary": True,
                                  "compactMetadata": {"trigger": "auto"},
                                  "message": {"content": [{"type": "text", "text": "summary"}]}})
        boundary = path.stat().st_size
        info = path.stat()
        anchor = {"boundary_offset": boundary, "transcript_identity": f"{info.st_dev}:{info.st_ino}"}
        with mock.patch.object(tt, "read_watermark", side_effect=AssertionError("watermark touched")), \
             mock.patch.object(tt, "mark_applied", side_effect=AssertionError("watermark touched")):
            pending = tt.compact_observation("claude", self.SID, str(path), anchor)
            self.assertEqual(pending["state"], "pending")
            self.assertEqual(pending["latest_type"], "user")
            self.append_claude(path, self.claude_row("assistant", "a2", "최종 결과: 완료", stop="end_turn"))
            ready = tt.compact_observation("claude", self.SID, str(path), anchor)
        self.assertEqual(ready["state"], "ready")
        self.assertIn("최종 결과: 완료", ready["recent"])
        self.assertFalse((self.state / "watermarks").exists())

    def test_idle_compact_uses_real_end_turn_and_preserved_segment_is_deduped(self):
        path = self.iso.root / f"{self.SID}.jsonl"
        self.append_claude(path, {**self.claude_row("user", "u1", "남겨 둘 시작 대화"), "parentUuid": None})
        self.append_claude(path, {**self.claude_row("assistant", "a1", "남겨 둘 응답"), "parentUuid": "u1"})
        self.append_claude(path, {**self.claude_row("user", "u2", "요약이 못 본 요청"), "parentUuid": "a1"})
        self.append_claude(path, {**self.claude_row("assistant", "a2", "압축 전 완료", stop="end_turn"),
                                  "parentUuid": "u2"})
        self.append_claude(path, {"type": "assistant", "uuid": "summary", "isCompactSummary": True,
                                  "compactMetadata": {"trigger": "auto", "preservedSegment": {
                                      "startUuid": "u1", "endUuid": "u2"}},
                                  "message": {"content": [{"type": "text", "text": "요약"}]}})
        anchor = {"boundary_offset": path.stat().st_size}
        observation = tt.compact_observation("claude", self.SID, str(path), anchor)
        self.assertEqual(observation["state"], "ready")
        self.assertIn("요약이 못 본 요청", observation["recent"])
        self.assertEqual(observation["recent"].count("남겨 둘 시작 대화"), 1)

    def test_partial_latest_row_and_replaced_transcript_fail_closed(self):
        path = self.iso.root / f"{self.SID}.jsonl"
        self.append_claude(path, self.claude_row("assistant", "a1", "완료", stop="end_turn"))
        info = path.stat()
        anchor = {"boundary_offset": info.st_size, "transcript_identity": f"{info.st_dev}:{info.st_ino}"}
        with path.open("ab") as handle:
            handle.write(b'{"type":"assistant","message":')
        self.assertEqual(tt.compact_observation("claude", self.SID, str(path), anchor)["state"], "unknown")
        replacement = self.iso.root / "replacement.jsonl"
        replacement.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(tt.compact_observation("claude", self.SID, str(replacement), anchor)["state"], "unknown")

    def test_giant_tool_result_sidechain_and_utf8_tail_keep_only_dialogue_and_latest_conclusion(self):
        path = self.iso.root / f"{self.SID}.jsonl"
        self.append_claude(path, self.claude_row("user", "u1", "시작 " + "가" * 6000))
        self.append_claude(path, {"type": "assistant", "uuid": "summary", "isCompactSummary": True,
                                  "message": {"content": [{"type": "text", "text": "요약 본문"}]}})
        info = path.stat()
        anchor = {"boundary_offset": info.st_size, "transcript_identity": f"{info.st_dev}:{info.st_ino}"}
        self.append_claude(path, {"type": "user", "uuid": "u-tool", "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "SECRET-" + "x" * (2 << 20)}]}})
        pending = tt.compact_observation("claude", self.SID, str(path), anchor)
        self.assertNotEqual(pending["state"], "ready")
        self.append_claude(path, {**self.claude_row("assistant", "side", "sidechain 비공개", stop="end_turn"),
                                  "isSidechain": True})
        self.assertNotEqual(tt.compact_observation("claude", self.SID, str(path), anchor)["state"], "ready")
        self.append_claude(path, self.claude_row("user", "u2", "다음 요청 " + "나" * 6000))
        self.append_claude(path, self.claude_row("assistant", "a-final", "마지막 결론: 한글 끝", stop="end_turn"))
        ready = tt.compact_observation("claude", self.SID, str(path), anchor)
        self.assertEqual(ready["state"], "ready")
        self.assertLessEqual(len(ready["recent"].encode("utf-8")), tt.COMPACT_RECENT_BYTES)
        self.assertTrue(ready["recent"].endswith("마지막 결론: 한글 끝"))
        self.assertNotIn("SECRET", ready["recent"])
        self.assertNotIn("sidechain", ready["recent"])
        ready["recent"].encode("utf-8")
        self.assertTrue(ready["recent_partial"])

    def test_malformed_latest_row_other_session_and_missing_boundary_never_become_ready(self):
        path = self.iso.root / f"{self.SID}.jsonl"
        self.append_claude(path, self.claude_row("assistant", "a1", "완료", stop="end_turn"))
        info = path.stat()
        anchor = {"boundary_offset": info.st_size, "transcript_identity": f"{info.st_dev}:{info.st_ino}"}
        self.assertEqual(tt.compact_observation("claude", self.SID, str(path), anchor)["state"], "ready")
        self.assertEqual(tt.compact_observation("claude", "another-session", str(path), anchor)["state"], "unknown")
        self.assertEqual(tt.compact_observation("claude", self.SID, str(path), {})["state"], "unknown")
        with path.open("ab") as handle:
            handle.write(b"{not-json}\n")
        self.assertEqual(tt.compact_observation("claude", self.SID, str(path), anchor)["state"], "unknown")


class CodexCompactObservationTest(TidyCase):
    SID = "codex-compact-session"

    def write_row(self, path, row):
        with path.open("ab") as handle:
            handle.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))

    def test_codex_requires_a_later_same_session_task_complete_not_idle(self):
        path = self.iso.root / "rollout-codex-compact.jsonl"
        self.write_row(path, {"type": "session_meta", "payload": {
            "id": self.SID, "cwd": str(self.cwd), "source": {"kind": "cli"}}})
        self.write_row(path, {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "compacting"}]}})
        info = path.stat()
        anchor = {"boundary_offset": info.st_size, "transcript_identity": f"{info.st_dev}:{info.st_ino}",
                  "turn_id": "turn-1"}
        pending = tt.compact_observation("codex", self.SID, str(path), anchor)
        self.assertEqual((pending["state"], pending["reason"]), ("pending", "turn-not-ended"))
        self.write_row(path, {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-other"}})
        self.assertEqual(tt.compact_observation("codex", self.SID, str(path), anchor)["state"], "pending")
        self.write_row(path, {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}})
        ready = tt.compact_observation("codex", self.SID, str(path), anchor)
        self.assertEqual((ready["state"], ready["turn_id"]), ("ready", "turn-1"))
        self.assertEqual(tt.compact_observation("codex", "foreign-session", str(path), anchor)["state"], "unknown")

    def test_user_input_after_completion_and_transcript_replacement_are_not_turn_end_proof(self):
        path = self.iso.root / "rollout-codex-input.jsonl"
        self.write_row(path, {"type": "session_meta", "payload": {"id": self.SID, "source": {"kind": "cli"}}})
        self.write_row(path, {"type": "response_item", "payload": {"type": "message", "role": "assistant"}})
        info = path.stat()
        anchor = {"boundary_offset": info.st_size, "transcript_identity": f"{info.st_dev}:{info.st_ino}"}
        self.write_row(path, {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}})
        self.write_row(path, {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                                       "content": [{"type": "input_text", "text": "new"}]}})
        self.assertEqual(tt.compact_observation("codex", self.SID, str(path), anchor)["reason"],
                         "new-input-after-turn")
        replacement = self.iso.root / "rollout-codex-replacement.jsonl"
        replacement.write_bytes(path.read_bytes())
        self.assertEqual(tt.compact_observation("codex", self.SID, str(replacement), anchor)["state"], "unknown")


class CodexCompactBridgeTest(TidyCase):
    SID = "codex-compact-main"

    def setUp(self):
        super().setUp()
        self.seat = st.Seat("pane", "a" * 32, pane=PANE, harness="codex", project_key="fixture")
        self.resolve = mock.patch.object(st, "resolve_seat", return_value=self.seat)
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.env = self.iso.env({"HERDR_PANE_ID": PANE})
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def rollout(self):
        path = self.iso.root / f"rollout-{self.SID}.jsonl"
        with path.open("wb") as handle:
            handle.write((json.dumps({"type": "session_meta", "payload": {
                "id": self.SID, "cwd": str(self.cwd), "source": {"kind": "cli"}}}) + "\n").encode())
            handle.write((json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "compacted"}]}})
                + "\n").encode())
        return path

    def test_automatic_remote_v2_requests_a_real_card_then_queues_snapshots_clears_and_continues_once(self):
        import dispatch_seat_handover as handover

        path = self.rollout()
        requests, clears, continues = [], [], []

        def request(pathname, nonce, target):
            requests.append((target, nonce))
            claim, reason = clear.claim_idle_card_request(pathname, nonce)
            self.assertIsNotNone(claim, reason)
            prompt = clear.idle_card_request_text(claim)
            self.assertIn("자동 압축 뒤 카드 작성 요청", prompt)
            self.assertNotIn("이어서해", prompt)
            self.assertEqual(st.run_hook("codex", "prompt", self.SID, cwd=str(self.cwd),
                                         env=self.env, prompt_text=prompt), "")
            with st.seat_lock(self.seat.key):
                st.write_card(self.seat, "codex", self.SID,
                              "진행 중: 실제 main 카드\n다음 할 일: 다음 입력을 기다림",
                              cwd=str(self.cwd), prompt_seq=st.read_prompt_seq(self.seat))
            args = SimpleNamespace(harness="codex", session_id=self.SID, cwd=str(self.cwd),
                                   no_clear=False, no_continue=False)
            with mock.patch.object(st, "resolve_caller", return_value=(self.seat, "codex", self.SID)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(st.cmd_enqueue(args), 0)
            self.assertTrue(clear.finish_idle_card_request(claim, accepted=True))
            return {"accepted": "true"}

        def clear_once(*_args):
            clears.append(1)
            return {"cleared": "true", "new_session": "codex-successor"}

        def continue_once(*_args):
            continues.append(1)
            return {"continued": "true"}

        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}), \
                mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
                mock.patch.object(clear, "_start_helper", return_value=4321), \
                mock.patch.object(clear, "_helper_is_live", return_value=True), \
                mock.patch.object(clear, "_run_idle_card_request", side_effect=request), \
                mock.patch.object(clear, "_prepare_handover_snapshot", wraps=clear._prepare_handover_snapshot), \
                mock.patch.object(handover, "collect_snapshot_bindings", return_value=[]), \
                mock.patch.object(handover, "publish_snapshot_locked", return_value=0), \
                mock.patch.object(runner, "start_detached", return_value=9876), \
                mock.patch.object(clear, "validate_continue", side_effect=lambda p, n: (clear.read_reservation(self.seat.key), "")):
            result = st.run_hook("codex", "compact", self.SID, phase="after", compact_summary="",
                                 compact_context={"trigger": "auto", "turn_id": "turn-compact-1",
                                                  "event_id": "compact-event-1"},
                                 transcript=str(path), cwd=str(self.cwd), env=self.env)
            self.assertEqual(result, "booking=scheduled")
            booking = clear.read_reservation(self.seat.key)
            self.assertTrue(booking["compact"]["card_request_required"])
            self.assertEqual(booking["memory_state"], "queued")
            self.assertEqual(runner.read_item(booking["queue_id"])["operation_id"], booking["operation_id"])
            with path.open("ab") as handle:
                handle.write((json.dumps({"type": "event_msg", "payload": {
                    "type": "task_complete", "turn_id": "turn-compact-1"}}) + "\n").encode())
            result = clear.run_helper(self.seat.key, booking["nonce"], wait=lambda *_: "idle",
                                      steward=clear_once, continuer=continue_once, sleep=lambda _delay: None)

        self.assertEqual(result, "continued")
        self.assertEqual(requests, [(PANE, booking["nonce"])])
        self.assertEqual((len(clears), len(continues)), (1, 1))
        accepted = clear.read_reservation(self.seat.key)
        self.assertEqual((accepted["origin"], accepted["status"], accepted["followup_kind"]),
                         ("compact", "cleared", "continue"))
        self.assertEqual(accepted["snapshot_state"], "ready")
        self.assertEqual(accepted["continued"]["state"], "sent")
        card = st.read_latest_card(self.seat)
        self.assertEqual(card["body"], "진행 중: 실제 main 카드\n다음 할 일: 다음 입력을 기다림")
        queue = runner.read_item(accepted["queue_id"])
        self.assertEqual(queue["operation_id"], accepted["operation_id"])
        self.assertEqual(int(queue.get("card_generation", 0) or 0), accepted["acceptance"]["source_generation"])
        self.assertEqual(accepted["acceptance"]["card_generation"], card["generation"])

    def test_a_marked_reply_card_is_taken_at_turn_end_with_the_same_queue_clear_and_continue(self):
        import dispatch_seat_handover as handover

        path = self.rollout()
        requests, clears, continues = [], [], []

        def request(pathname, nonce, target):
            requests.append((target, nonce))
            claim, reason = clear.claim_idle_card_request(pathname, nonce)
            self.assertIsNotNone(claim, reason)
            prompt = clear.idle_card_request_text(claim)
            self.assertIn("자동 압축 뒤 카드 작성 요청", prompt)
            self.assertNotIn("이어서해", prompt)
            self.assertEqual(st.run_hook("codex", "prompt", self.SID, cwd=str(self.cwd),
                                         env=self.env, prompt_text=prompt), "")
            self.assertTrue(clear.finish_idle_card_request(claim, accepted=True))
            begin, end = st.reply_card_markers(claim["nonce"])
            self.assertIn(begin, prompt)
            self.assertIn(end, prompt)
            # The session answers with the card only; the turn-end observer takes it.
            self.assertEqual(st.accept_reply_card("codex", self.SID, str(self.cwd), "그냥 답"), "reply-card=none")
            reply = f"{begin}\n진행 중: 실제 main 카드\n다음 할 일: 다음 입력을 기다림\n{end}"
            self.assertEqual(st.accept_reply_card("codex", self.SID, str(self.cwd), reply[:-3]),
                             "reply-card=none reason=unmarked")                       # cut off
            line = st.accept_reply_card("codex", self.SID, str(self.cwd), reply)
            self.assertTrue(line.startswith("reply-card=accepted enqueue="), line)
            self.assertTrue(line.endswith("clear=scheduled"), line)
            self.assertEqual(st.accept_reply_card("codex", self.SID, str(self.cwd), reply),
                             "reply-card=skipped reason=already-accepted")            # Stop seen twice
            return {"accepted": "true"}

        def clear_once(*_args):
            clears.append(1)
            return {"cleared": "true", "new_session": "codex-successor"}

        def continue_once(*_args):
            continues.append(1)
            return {"continued": "true"}

        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}), \
                mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
                mock.patch.object(clear, "_start_helper", return_value=4321), \
                mock.patch.object(clear, "_helper_is_live", return_value=True), \
                mock.patch.object(clear, "_run_idle_card_request", side_effect=request), \
                mock.patch.object(clear, "_prepare_handover_snapshot", wraps=clear._prepare_handover_snapshot), \
                mock.patch.object(handover, "collect_snapshot_bindings", return_value=[]), \
                mock.patch.object(handover, "publish_snapshot_locked", return_value=0), \
                mock.patch.object(runner, "start_detached", return_value=9876), \
                mock.patch.object(clear, "validate_continue", side_effect=lambda p, n: (clear.read_reservation(self.seat.key), "")):
            result = st.run_hook("codex", "compact", self.SID, phase="after", compact_summary="",
                                 compact_context={"trigger": "auto", "turn_id": "turn-compact-1",
                                                  "event_id": "compact-event-1"},
                                 transcript=str(path), cwd=str(self.cwd), env=self.env)
            self.assertEqual(result, "booking=scheduled")
            booking = clear.read_reservation(self.seat.key)
            self.assertTrue(booking["compact"]["card_request_required"])
            self.assertEqual(booking["memory_state"], "queued")
            self.assertEqual(runner.read_item(booking["queue_id"])["operation_id"], booking["operation_id"])
            with path.open("ab") as handle:
                handle.write((json.dumps({"type": "event_msg", "payload": {
                    "type": "task_complete", "turn_id": "turn-compact-1"}}) + "\n").encode())
            result = clear.run_helper(self.seat.key, booking["nonce"], wait=lambda *_: "idle",
                                      steward=clear_once, continuer=continue_once, sleep=lambda _delay: None)

        self.assertEqual(result, "continued")
        self.assertEqual(requests, [(PANE, booking["nonce"])])
        self.assertEqual((len(clears), len(continues)), (1, 1))
        accepted = clear.read_reservation(self.seat.key)
        self.assertEqual((accepted["origin"], accepted["status"], accepted["followup_kind"]),
                         ("compact", "cleared", "continue"))
        self.assertEqual(accepted["snapshot_state"], "ready")
        self.assertEqual(accepted["continued"]["state"], "sent")
        card = st.read_latest_card(self.seat)
        self.assertEqual(card["body"], "진행 중: 실제 main 카드\n다음 할 일: 다음 입력을 기다림")
        queue = runner.read_item(accepted["queue_id"])
        self.assertEqual(queue["operation_id"], accepted["operation_id"])
        self.assertEqual(int(queue.get("card_generation", 0) or 0), accepted["acceptance"]["source_generation"])
        self.assertEqual(accepted["acceptance"]["card_generation"], card["generation"])

    def test_auto_compact_persists_same_operation_queue_before_card_request_and_after_cancel(self):
        path = self.rollout()
        starts = []

        def start_once(*args, **kwargs):
            starts.append((args, kwargs))
            return None if len(starts) == 1 else 9876

        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}), \
                mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
                mock.patch.object(clear, "_start_helper", return_value=4321), \
                mock.patch.object(runner, "start_detached", side_effect=start_once):
            result = st.run_hook("codex", "compact", self.SID, phase="after", compact_summary="",
                                 compact_context={"trigger": "auto", "turn_id": "turn-early-queue",
                                                  "event_id": "compact-early-queue"},
                                 transcript=str(path), cwd=str(self.cwd), env=self.env)
            self.assertEqual(result, "booking=scheduled")
            booking = clear.read_reservation(self.seat.key)
            queue_id = booking["queue_id"]
            item = runner.read_item(queue_id)
            self.assertIsNotNone(item, "card-request booking has no durable memory queue row")
            operation_id = booking["operation_id"]
            self.assertEqual((item["operation_id"], item["id"]), (operation_id, queue_id))

            st.run_hook("codex", "prompt", self.SID, cwd=str(self.cwd), env=self.env,
                        prompt_text="new user input cancels the card request")
            cancelled = clear.read_reservation(self.seat.key)
            self.assertEqual(cancelled["status"], "cancelled")
            retained = runner.read_item(queue_id)
            self.assertIsNotNone(retained)
            self.assertEqual((retained["id"], retained["operation_id"]), (queue_id, operation_id))
            self.assertGreaterEqual(len(starts), 2, "normal prompt reconciliation did not restart the same queue")

    def test_old_booking_and_queue_can_be_accepted_but_partial_relation_cannot_clear(self):
        path = self.rollout()
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}), \
                mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
                mock.patch.object(clear, "_start_helper", return_value=4321), \
                mock.patch.object(runner, "start_detached", return_value=9876):
            result = st.run_hook("codex", "compact", self.SID, phase="after", compact_summary="",
                                 compact_context={"trigger": "auto", "turn_id": "turn-legacy",
                                                  "event_id": "compact-legacy"},
                                 transcript=str(path), cwd=str(self.cwd), env=self.env)
            self.assertEqual(result, "booking=scheduled")
            booking = clear.read_reservation(self.seat.key)
            booking["compact"]["turn_end_observed"] = True
            booking.pop("acceptance", None)
            booking.pop("source_card_generation", None)
            booking["memory_state"] = "awaiting-card"
            clear._write_reservation(booking)

            queue = runner.read_item(booking["queue_id"])
            source_generation = queue.get("card_generation")
            queue.pop("operation_id", None)  # legacy queue row remains addressable by its stable ID
            runner.write_item(queue)
            claim, reason = clear.claim_idle_card_request(clear.reservation_path(self.seat.key), booking["nonce"])
            self.assertEqual(reason, "")
            st.run_hook("codex", "prompt", self.SID, cwd=str(self.cwd), env=self.env,
                        prompt_text=clear.idle_card_request_text(claim))
            self.assertTrue(clear.finish_idle_card_request(claim, accepted=True))
            with st.seat_lock(self.seat.key):
                st.write_card(self.seat, "codex", self.SID, "실제 compact 카드", cwd=str(self.cwd),
                              prompt_seq=st.read_prompt_seq(self.seat))
            args = SimpleNamespace(harness="codex", session_id=self.SID, cwd=str(self.cwd),
                                   no_clear=False, no_continue=False)
            with mock.patch.object(st, "resolve_caller", return_value=(self.seat, "codex", self.SID)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(st.cmd_enqueue(args), 0)

            accepted = clear.read_reservation(self.seat.key)
            accepted_queue = runner.read_item(booking["queue_id"])
            self.assertEqual(accepted["acceptance"]["schema"], 1)
            self.assertEqual(accepted_queue.get("card_generation"), source_generation)
            self.assertEqual(accepted_queue["operation_id"], accepted["operation_id"])
            valid, reason = clear.validate_request(clear.reservation_path(self.seat.key), booking["nonce"])
            self.assertIsNotNone(valid, reason)

            accepted["acceptance"] = {"schema": 1, "nonce": booking["nonce"], "queue_id": booking["queue_id"]}
            clear._write_reservation(accepted)
            invalid, reason = clear.validate_request(clear.reservation_path(self.seat.key), booking["nonce"])
            self.assertIsNone(invalid)
            self.assertEqual(reason, "idle-card-not-accepted")
            retained = runner.read_item(booking["queue_id"])
            self.assertEqual((retained["id"], retained["operation_id"]),
                             (booking["queue_id"], accepted["operation_id"]))

    def test_manual_unknown_or_unidentified_codex_compaction_stays_queue_only(self):
        path = self.rollout()
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}), \
                mock.patch.object(runner, "start_detached", return_value=9876):
            for trigger, turn_id, expected in (("manual", "turn-1", "trigger-manual"),
                                               ("unknown", "turn-1", "trigger-unknown"),
                                               ("auto", "", "turn-identity-unknown")):
                result = st.run_hook("codex", "compact", self.SID, phase="after", compact_summary="",
                                     compact_context={"trigger": trigger, "turn_id": turn_id},
                                     transcript=str(path), cwd=str(self.cwd), env=self.env)
                self.assertIn(f"reason={expected}", result)
                self.assertIsNone(clear.read_reservation(self.seat.key))
                clear.cancel(self.seat)

    def test_new_real_prompt_cancels_the_old_compact_request_and_replay_does_not_resurrect_it(self):
        path = self.rollout()
        context = {"trigger": "auto", "turn_id": "turn-cancel", "event_id": "compact-cancel"}
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}), \
                mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
                mock.patch.object(clear, "_start_helper", return_value=4321), \
                mock.patch.object(runner, "start_detached", return_value=9876), \
                mock.patch.object(clear, "_run_idle_card_request") as request:
            result = st.run_hook("codex", "compact", self.SID, phase="after", compact_summary="",
                                 compact_context=context, transcript=str(path), cwd=str(self.cwd), env=self.env)
            self.assertEqual(result, "booking=scheduled")
            self.assertEqual(st.run_hook("codex", "prompt", self.SID, cwd=str(self.cwd), env=self.env,
                                         prompt_text="실제 새 입력"), "")
            held = clear.read_reservation(self.seat.key)
            self.assertEqual((held["status"], held["reason"]), ("cancelled", "new-input"))
            replay = st.run_hook("codex", "compact", self.SID, phase="after", compact_summary="",
                                 compact_context=context, transcript=str(path), cwd=str(self.cwd), env=self.env)
        self.assertEqual(replay, "booking=off")
        request.assert_not_called()
        self.assertEqual(clear.read_reservation(self.seat.key)["status"], "cancelled")

    def test_uncertain_card_request_is_terminal_and_never_retried(self):
        path = self.rollout()
        calls = []

        def uncertain(pathname, nonce, _target):
            calls.append(1)
            claim, reason = clear.claim_idle_card_request(pathname, nonce)
            self.assertIsNotNone(claim, reason)
            self.assertFalse(clear.finish_idle_card_request(claim, accepted=False, reason="partial-send"))
            return {"accepted": "unverified", "reason": "partial-send"}

        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}), \
                mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
                mock.patch.object(clear, "_start_helper", return_value=4321), \
                mock.patch.object(runner, "start_detached", return_value=9876), \
                mock.patch.object(clear, "_run_idle_card_request", side_effect=uncertain):
            st.run_hook("codex", "compact", self.SID, phase="after", compact_summary="",
                        compact_context={"trigger": "auto", "turn_id": "turn-uncertain",
                                         "event_id": "compact-uncertain"},
                        transcript=str(path), cwd=str(self.cwd), env=self.env)
            booking = clear.read_reservation(self.seat.key)
            with path.open("ab") as handle:
                handle.write((json.dumps({"type": "event_msg", "payload": {
                    "type": "task_complete", "turn_id": "turn-uncertain"}}) + "\n").encode())
            first = clear.run_helper(self.seat.key, booking["nonce"], wait=lambda *_: "idle",
                                     steward=lambda *_: self.fail("uncertain send must not clear"),
                                     sleep=lambda _delay: None)
            second = clear.run_helper(self.seat.key, booking["nonce"], wait=lambda *_: "idle",
                                      steward=lambda *_: self.fail("terminal booking must not retry"),
                                      sleep=lambda _delay: None)
        self.assertEqual(first, "unverified")
        self.assertEqual(second, "superseded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(clear.read_reservation(self.seat.key)["status"], "unverified")


class CompactProgressRecoveryTest(TidyCase):
    SID = "claude-compact-recovery"

    def setUp(self):
        super().setUp()
        self.seat = st.Seat("pane", "b" * 32, pane=PANE, harness="claude", project_key="fixture")
        self.resolve = mock.patch.object(st, "resolve_seat", return_value=self.seat)
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.env = self.iso.env({"HERDR_PANE_ID": PANE})
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_durable_queue_survives_booking_lock_failure_then_one_apply_and_one_clear_resume(self):
        import session_tidy_idle
        import dispatch_seat_handover as handover

        path = self.iso.root / f"{self.SID}.jsonl"
        rows = [
            {"type": "user", "uuid": "u1", "message": {"content": [{"type": "text", "text": "start"}]}},
            {"type": "assistant", "uuid": "a1", "message": {"stop_reason": "tool_use",
                "content": [{"type": "text", "text": "working"}]}},
            {"type": "assistant", "uuid": "compact-summary", "isCompactSummary": True,
             "compactMetadata": {"trigger": "auto"},
             "message": {"content": [{"type": "text", "text": "compact summary"}]}},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        real_schedule = clear.schedule_for_enqueue
        schedule_calls = []

        def fail_once(*args, **kwargs):
            schedule_calls.append(1)
            if len(schedule_calls) == 1:
                raise BlockingIOError("synthetic lock contention")
            return real_schedule(*args, **kwargs)

        starts = []
        def runner_start(_qid):
            starts.append(_qid)
            if len(starts) == 1:
                raise OSError("synthetic runner start failure")
            return 4242

        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}), \
                mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
                mock.patch.object(clear, "_start_helper", return_value=4343) as helper_start, \
                mock.patch.object(clear, "schedule_for_enqueue", side_effect=fail_once), \
                mock.patch.object(runner, "start_detached", side_effect=runner_start):
            first = st.run_hook("claude", "compact", self.SID, phase="after", compact_summary="compact summary",
                                compact_context={"trigger": "auto", "summary_id": "compact-summary-id"},
                                transcript=str(path), cwd=str(self.cwd), env=self.env)
            self.assertIn("booking=pending reason=reservation-write-BlockingIOError", first)
            self.assertIn("runner-start=OSError", first)
            queued = runner.list_items()
            self.assertEqual(len(queued), 1)
            qid = queued[0]["id"]
            operation_id = queued[0]["operation_id"]
            self.assertEqual(queued[0]["status"], "queued")
            self.assertIsNone(clear.read_reservation(self.seat.key))

            with path.open("ab") as handle:
                handle.write((json.dumps({"type": "assistant", "uuid": "a2",
                    "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}})
                    + "\n").encode())
            recovered = session_tidy_idle.observe_payload("claude", {
                "hook_event_name": "Stop", "session_id": self.SID, "cwd": str(self.cwd),
                "transcript_path": str(path),
            })
            self.assertEqual(recovered, "booking=scheduled")
            booking = clear.read_reservation(self.seat.key)
            self.assertEqual((booking["queue_id"], booking["operation_id"]), (qid, operation_id))
            self.assertEqual(len(runner.list_items()), 1)
            self.assertEqual(starts, [qid, qid])
            helper_start.assert_called_once()

            apply_calls = []
            bundle = SimpleNamespace(empty=False, unreadable=[], exchange=self.iso.root / "exchange")
            with mock.patch.object(runner, "assemble", return_value=bundle), \
                    mock.patch.object(runner, "prepare_exchange", return_value=str(self.iso.root / "exchange")), \
                    mock.patch.object(runner, "dispatch_worker", return_value={"attempt_id": "attempt"}), \
                    mock.patch.object(runner, "wait_worker"), \
                    mock.patch.object(runner, "validate_actions"), \
                    mock.patch.object(runner, "apply_actions", side_effect=lambda item, run_dir, _bundle:
                                      apply_calls.append(run_dir.name) or "applied"), \
                    mock.patch.object(runner, "advance_watermarks"), \
                    mock.patch.object(runner, "publish_memory"), \
                    mock.patch.object(runner, "notify"), \
                    mock.patch.object(runner, "coverage_note", return_value=""), \
                    mock.patch.object(runner, "remove_exchange"):
                runner.run(qid)
                runner.run(qid)
            self.assertEqual(apply_calls, [qid])
            self.assertEqual(runner.read_item(qid)["batch"], qid)

            clears, continues = [], []
            def clear_once(*_args):
                clears.append(1)
                return {"cleared": "true", "new_session": "claude-successor"}
            def continue_once(*_args):
                continues.append(1)
                return {"continued": "true"}
            with mock.patch.object(handover, "collect_snapshot_bindings", return_value=[]), \
                    mock.patch.object(handover, "publish_snapshot_locked", return_value=0):
                result = clear.run_helper(self.seat.key, booking["nonce"], wait=lambda *_: "idle",
                                          steward=clear_once, continuer=continue_once,
                                          sleep=lambda _delay: None)
            self.assertEqual(result, "continued")
            self.assertEqual((len(clears), len(continues)), (1, 1))
            finished = clear.read_reservation(self.seat.key)
            self.assertEqual((finished["status"], finished["continued"]["state"]), ("cleared", "sent"))


class OpenCodeCompactObservationTest(TidyCase):
    SID = "ses_fixture0000000000000001"

    def setUp(self):
        super().setUp()
        load_opencode_fixture(self.iso.opencode_db)

    def message(self, con, mid, data, time_ms):
        con.execute("INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
                    (mid, self.SID, time_ms, time_ms, json.dumps(data)))

    def part(self, con, pid, mid, data, time_ms):
        con.execute("INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, mid, self.SID, time_ms, time_ms, json.dumps(data)))

    def test_matching_auto_part_latest_user_parent_finish_and_native_idle_are_required(self):
        con = sqlite3.connect(self.iso.opencode_db)
        self.addCleanup(con.close)
        base_time = 1_800_000_000_000
        self.message(con, "compact-parent", {"role": "user", "time": {"created": base_time}}, base_time)
        self.part(con, "compact-part", "compact-parent", {"type": "compaction", "auto": True,
                                                            "tail_start_id": "compact-parent"}, base_time)
        self.message(con, "summary", {"role": "assistant", "summary": True,
                                        "parentID": "compact-parent", "time": {"created": base_time + 1}}, base_time + 1)
        self.part(con, "summary-text", "summary", {"type": "text", "text": "요약"}, base_time + 1)
        self.message(con, "user-current", {"role": "user", "time": {"created": base_time + 2}}, base_time + 2)
        self.part(con, "user-text", "user-current", {"type": "text", "text": "계속 요청"}, base_time + 2)
        self.message(con, "assistant-current", {"role": "assistant", "parentID": "user-current",
                                                  "finish": "stop", "time": {"created": base_time + 3}}, base_time + 3)
        self.part(con, "assistant-text", "assistant-current", {"type": "text", "text": "최종 결과"}, base_time + 3)
        con.commit()
        anchor = {"summary_id": "summary", "parent_id": "compact-parent",
                  "tail_start_id": "compact-parent", "compaction_part_id": "compact-part"}
        self.assertEqual(tt.compact_observation("opencode", self.SID, str(self.iso.opencode_db), anchor,
                                                native_idle=False)["state"], "pending")
        ready = tt.compact_observation("opencode", self.SID, str(self.iso.opencode_db), anchor,
                                       native_idle=True)
        self.assertEqual(ready["state"], "ready")
        self.assertIn("최종 결과", ready["recent"])
        con.execute("UPDATE message SET data = ? WHERE id = 'assistant-current'",
                    (json.dumps({"role": "assistant", "parentID": "user-current", "finish": "tool-calls"}),))
        con.commit()
        self.assertEqual(tt.compact_observation("opencode", self.SID, str(self.iso.opencode_db), anchor,
                                                native_idle=True)["state"], "pending")
        con.execute("UPDATE message SET data = ? WHERE id = 'assistant-current'",
                    (json.dumps({"role": "assistant", "parentID": "user-current", "finish": "stop"}),))
        con.commit()
        self.assertEqual(tt.compact_observation("opencode", self.SID, str(self.iso.opencode_db), anchor,
                                                native_idle=True)["state"], "ready")

    def seed_opencode_compact(self, con, *, user_data=None, assistant_data=None, auto=True):
        base_time = 1_800_000_000_000
        self.message(con, "compact-parent", {"role": "user", "time": {"created": base_time}}, base_time)
        self.part(con, "compact-part", "compact-parent", {"type": "compaction", "auto": auto,
                                                            "tail_start_id": "compact-parent"}, base_time)
        self.message(con, "summary", {"role": "assistant", "summary": True, "finish": "stop",
                                        "parentID": "compact-parent", "time": {"created": base_time + 1}}, base_time + 1)
        self.part(con, "summary-text", "summary", {"type": "text", "text": "요약"}, base_time + 1)
        if user_data is not None:
            self.message(con, "user-current", user_data, base_time + 2)
            self.part(con, "user-text", "user-current", {"type": "text", "text": "요청"}, base_time + 2)
        if assistant_data is not None:
            self.message(con, "assistant-current", assistant_data, base_time + 3)
            self.part(con, "assistant-text", "assistant-current", {"type": "text", "text": "응답"}, base_time + 3)
        con.commit()
        return {"summary_id": "summary", "parent_id": "compact-parent",
                "tail_start_id": "compact-parent", "compaction_part_id": "compact-part"}

    def observe(self, anchor, idle=True):
        return tt.compact_observation("opencode", self.SID, str(self.iso.opencode_db), anchor,
                                      native_idle=idle)["state"]

    def test_summary_finish_synthetic_continuation_error_and_manual_part_are_not_completion(self):
        con = sqlite3.connect(self.iso.opencode_db)
        self.addCleanup(con.close)
        anchor = self.seed_opencode_compact(con)              # only the summary exists: its finish is not the turn
        self.assertEqual(self.observe(anchor), "pending")
        con.execute("DELETE FROM message WHERE id IN ('user-current', 'assistant-current')")
        base_time = 1_800_000_000_000
        self.message(con, "user-current", {"role": "user", "synthetic": True, "time": {"created": base_time + 2}},
                     base_time + 2)
        self.message(con, "assistant-current", {"role": "assistant", "parentID": "user-current", "finish": "stop",
                                                  "time": {"created": base_time + 3}}, base_time + 3)
        con.commit()
        self.assertEqual(self.observe(anchor), "pending")     # synthetic continue user + an older finish
        con.execute("UPDATE message SET data = ? WHERE id = 'user-current'",
                    (json.dumps({"role": "user", "time": {"created": base_time + 2}}),))
        con.execute("UPDATE message SET data = ? WHERE id = 'assistant-current'",
                    (json.dumps({"role": "assistant", "parentID": "user-current", "finish": "stop",
                                 "error": {"name": "UnknownError"}}),))
        con.commit()
        self.assertEqual(self.observe(anchor), "pending")     # an errored finish
        con.execute("UPDATE message SET data = ? WHERE id = 'assistant-current'",
                    (json.dumps({"role": "assistant", "parentID": "other-user", "finish": "stop"}),))
        con.commit()
        self.assertEqual(self.observe(anchor), "pending")     # not the latest user's answer
        con.execute("UPDATE message SET data = ? WHERE id = 'assistant-current'",
                    (json.dumps({"role": "assistant", "parentID": "user-current", "finish": "stop"}),))
        con.commit()
        self.assertEqual(self.observe(anchor, idle=False), "pending")
        self.assertEqual(self.observe(anchor), "ready")
        con.execute("UPDATE part SET data = ? WHERE id = 'compact-part'",
                    (json.dumps({"type": "compaction", "auto": False}),))
        con.commit()
        self.assertEqual(self.observe(anchor), "unknown")     # a manual compaction never clears

    def test_stale_other_session_and_pre_booking_runtime_status_never_mark_idle(self):
        extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root)}
        context = {"trigger": "auto", "identity": "opencode-status", "transcript": str(self.iso.opencode_db)}
        with self.iso.patched_environ(extra), mock.patch.object(clear, "_start_helper", return_value=1), \
                mock.patch.object(clear, "herdr_command", return_value="/bin/true"):
            seat = st.resolve_seat("opencode", str(self.cwd), sid=self.SID)
            with st.seat_lock(seat.key):
                st.write_card(seat, "opencode", self.SID, "요약", cwd=str(self.cwd),
                              prompt_seq=st.read_prompt_seq(seat), origin="runtime-compact",
                              compact_context=context)
            clear.schedule_for_enqueue(seat, "opencode", self.SID, str(self.cwd), compact_context=context)
            req = clear.read_reservation(seat.key)
            self.assertFalse(clear.observe_runtime_status(self.SID, "idle", observed_at=req["created"] - 5,
                                                          cwd=str(self.cwd)))
            self.assertFalse(clear.observe_runtime_status("ses_other", "idle", observed_at=req["created"] + 1,
                                                          cwd=str(self.cwd)))
            self.assertNotIn("runtime_status", clear.read_reservation(seat.key))
            self.assertTrue(clear.observe_runtime_status(self.SID, "busy", observed_at=req["created"] + 1,
                                                         cwd=str(self.cwd)))
            held = clear.read_reservation(seat.key)
            self.assertEqual(held["runtime_status"]["status"], "busy")
            self.assertEqual(clear.compact_observation(held)["state"], "unknown")   # no anchor/idle: never ready


    def test_matching_auto_compact_and_post_booking_idle_observation_clear_once(self):
        import dispatch_seat_handover as handover
        base_time = 1_800_000_000_000
        con = sqlite3.connect(self.iso.opencode_db)
        self.addCleanup(con.close)
        self.message(con, "compact-parent", {"role": "user", "time": {"created": base_time}}, base_time)
        self.part(con, "compact-part", "compact-parent", {"type": "compaction", "auto": True,
                                                            "tail_start_id": "compact-parent"}, base_time)
        self.message(con, "summary", {"role": "assistant", "summary": True,
                                        "parentID": "compact-parent", "time": {"created": base_time + 1}}, base_time + 1)
        self.part(con, "summary-text", "summary", {"type": "text", "text": "요약 전문"}, base_time + 1)
        self.message(con, "user-current", {"role": "user", "time": {"created": base_time + 2}}, base_time + 2)
        self.part(con, "user-text", "user-current", {"type": "text", "text": "요청"}, base_time + 2)
        self.message(con, "assistant-current", {"role": "assistant", "parentID": "user-current",
                                                  "finish": "stop", "time": {"created": base_time + 3}}, base_time + 3)
        self.part(con, "assistant-text", "assistant-current", {"type": "text", "text": "새 세션에서 볼 결론"},
                  base_time + 3)
        con.commit()

        fake_herdr = self.iso.root / "fake-herdr"
        fake_herdr.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_herdr.chmod(0o755)
        extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                 "HEARTING_TIDY_HERDR": str(fake_herdr)}
        context = {"trigger": "auto", "identity": "opencode-e2e", "transcript": str(self.iso.opencode_db),
                   "summary_id": "summary", "parent_id": "compact-parent",
                   "tail_start_id": "compact-parent", "compaction_part_id": "compact-part"}
        with self.iso.patched_environ(extra), mock.patch.object(clear, "_start_helper", return_value=1), \
                mock.patch.object(clear, "herdr_command", return_value=str(fake_herdr)):
            seat = st.resolve_seat("opencode", str(self.cwd), sid=self.SID)
            with st.seat_lock(seat.key):
                st.write_card(seat, "opencode", self.SID, "요약 전문", cwd=str(self.cwd),
                              prompt_seq=st.read_prompt_seq(seat), origin="runtime-compact",
                              compact_context=context)
            clear.schedule_for_enqueue(seat, "opencode", self.SID, str(self.cwd), compact_context=context)
            req = clear.read_reservation(seat.key)
            stale = st.run_hook("opencode", "runtime-status", self.SID, cwd=str(self.cwd),
                                compact_context={"status": "idle",
                                                 "observed_at": (req["created"] - 5) * 1000},
                                env=os.environ)
            self.assertEqual(stale, "")
            held = clear.read_reservation(seat.key)
            self.assertNotIn("runtime_status", held)
            self.assertEqual(clear.compact_observation(held)["state"], "pending")
            snapshot_calls, send_calls = [], []
            idle_events, observations = [], []

            def observe_new_idle(_seconds):
                idle_events.append(st.run_hook(
                    "opencode", "runtime-status", self.SID, cwd=str(self.cwd),
                    compact_context={"status": "idle", "observed_at": (req["created"] + 1) * 1000},
                    env=os.environ))
                accepted = clear.read_reservation(seat.key)
                observations.append((accepted["runtime_status"], clear.compact_observation(accepted)["state"]))

            with mock.patch.object(handover, "publish_snapshot_locked",
                                   side_effect=lambda *a, **k: snapshot_calls.append(1) or 1):
                result = clear.run_helper(
                    seat.key, req["nonce"], wait=lambda *_a: "idle",
                    steward=lambda *_a: send_calls.append("clear") or {
                        "cleared": "true", "new_session": "sid-next"},
                    continuer=lambda *_a: send_calls.append("continue") or {"continued": "true"},
                    sleep=observe_new_idle)
            with self.iso.patched_environ():
                saved = st.read_latest_card(seat)
        self.assertEqual(result, "continued")
        self.assertEqual(send_calls, ["clear", "continue"])
        self.assertEqual(idle_events, ["booking=pending"])
        self.assertEqual(len(observations), 1)
        runtime_status, observation_state = observations[0]
        self.assertEqual((runtime_status["sid"], runtime_status["status"]), (self.SID, "idle"))
        self.assertGreater(runtime_status["observed_at"], req["created"])
        self.assertEqual(observation_state, "ready")
        self.assertEqual(snapshot_calls, [1])
        self.assertIn("새 세션에서 볼 결론", saved["body"])
        self.assertIn(str(self.iso.opencode_db), saved["body"])


class MemoryLayerTest(TidyCase):
    """Card layer B ("참고할 기억"): its own revision and receipt, beside the unchanged layer A."""

    def test_first_reply_guidance_survives_all_harnesses_budgets_and_late_memory(self):
        guidance = st.CARD_FIRST_REPLY_GUIDANCE
        for requirement in ("첫 답 맨 앞", "도구 호출 전에", "사용자의 언어", "하던 일 · 남은 일 · 다음 할 일",
                            "5줄 안팎", "한 번", "다른 지시를 먼저 주면 요약을 생략", "그 지시만"):
            self.assertIn(requirement, guidance)
        for harness in st.HARNESSES:
            for bundled in (False, True):
                for long_body in (False, True):
                    with self.subTest(harness=harness, bundled=bundled, long_body=long_body):
                        pane = f"{PANE}-{harness}-{bundled}-{long_body}"
                        receiver = f"receiver-{harness}-{bundled}-{long_body}"
                        body = "진행 중: 카드 시험\n다음 할 일: 검증\n" + ("세부 내용 " * 900 if long_body else "")
                        self.card("author-A", body, harness=harness, pane=pane)
                        with self.library({"HERDR_PANE_ID": pane}):
                            seat = st.resolve_seat(harness, str(self.cwd))
                            if bundled:
                                st.update_memory_refs(seat, **self.refs(n=8))
                                st.write_notice(seat, "정리 결과: 새 기록 3건. 되돌리기: mem tidy-undo b1",
                                                author_harness=harness, author_sid="author-A")
                            got = st.run_hook(harness, "start", receiver, cwd=str(self.cwd))
                            self.assertEqual(got.count(guidance), 1)
                            self.assertIn(guidance, got.splitlines())
                            self.assertLess(got.index(guidance), got.index("진행 중: 카드 시험"))
                            self.assertIn("카드 전문:", got)
                            self.assertLessEqual(len(got.encode("utf-8")), 2400)
                            replay = self.hook(harness, "prompt", receiver, "--reread", pane=pane)
                            self.assertEqual(replay, got + "\n")
                            self.assertLessEqual(len(replay.encode("utf-8")), 2400)
                            self.assertEqual("이하 생략" in got, long_body)
                            self.assertEqual("[참고할 기억]" in got, bundled)
                            self.assertEqual(st.run_hook(harness, "prompt", receiver, cwd=str(self.cwd)), "")
                            st.update_memory_refs(seat, **self.refs(batch="late-B"))
                            late = st.run_hook(harness, "prompt", receiver, cwd=str(self.cwd))
                            self.assertIn("[참고할 기억]", late)
                            self.assertNotIn("[세션 카드]", late)
                            self.assertNotIn(guidance, late)
                            self.assertNotIn("[첫 답]", late)

    def test_seat_lineage_three_harnesses_returns_sources_cards_and_locations(self):
        rows = [
            {"event": "start", "harness": "claude", "sid": "claude-old", "ts": 1,
             "first_seen": 1, "source": "startup"},
            {"event": "start", "harness": "claude", "sid": "claude-new", "ts": 3,
             "first_seen": 3, "source": "clear"},
            {"event": "handover", "harness": "claude", "sid": "claude-new",
             "from": {"harness": "claude", "sid": "claude-old"}, "source": "clear"},
            {"event": "start", "harness": "codex", "sid": "codex-one", "ts": 2,
             "first_seen": 2, "source": "resume"},
            {"event": "start", "harness": "opencode", "sid": "opencode-one", "ts": 4,
             "first_seen": 4, "source": "fork"},
        ]
        cards = {("claude", "claude-old"): [
                     {"generation": 2, "path": "/card/2.json", "status": "available"}],
                 ("claude", "claude-new"): [
                     {"generation": 1, "path": "/card/1.json", "status": "available"}]}
        locations = {("claude", "claude-old"): {"path": "/raw/claude.jsonl", "status": "available"},
                     ("claude", "claude-new"): {"path": None, "status": "missing"}}
        projected = st._project_seat_lineage(rows, cards, locations, sid="claude-new")
        self.assertEqual([row["sid"] for row in projected[:3]],
                         ["claude-new", "opencode-one", "codex-one"])
        self.assertEqual(projected[0]["source"], "clear")
        self.assertEqual(projected[0]["relation"], "confirmed")
        self.assertEqual(projected[2]["source"], "resume")
        self.assertEqual(projected[2]["card"]["status"], "none")
        self.assertEqual(projected[3]["transcript"]["status"], "available")

    def test_tidy_clear_start_records_idempotent_succession_after_reservation_observation(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "sid-old", "start", source="startup",
                            cwd=str(self.cwd), now=100)
            clear._write_reservation({"schema": 1, "nonce": "clear-nonce", "status": "reserved",
                                      "created": 150, "seat": seat.as_dict(), "harness": "claude",
                                      "sid": "sid-old"})
            st.run_hook("claude", "start", "sid-new", source="clear", cwd=str(self.cwd), now=200)
            booking = clear.read_reservation(seat.key)
            rows = st._read_ledger_lines(seat)
            st.run_hook("claude", "start", "sid-new", source="clear", cwd=str(self.cwd), now=201)
            lineage = st._project_seat_lineage(st._read_ledger_lines(seat), {}, {}, sid="sid-new")
            notice = st.build_human_history_notice(seat, "claude", "sid-new")
        self.assertEqual(booking["observed"]["sid"], "sid-new")
        succession = [row for row in rows if row.get("event") == "succession"]
        self.assertEqual(len(succession), 1)
        self.assertFalse(any(row.get("event") == "handover" for row in rows))
        self.assertEqual((succession[0]["from"], succession[0]["sid"], succession[0]["source"]),
                         ("sid-old", "sid-new", "tidy-clear"))
        self.assertEqual(lineage[0]["relation"], "confirmed")
        self.assertEqual(lineage[0]["predecessor"], {"harness": "claude", "sid": "sid-old"})
        self.assertIn("이전 대화 열기", notice)

    def test_history_notice_loads_managed_key_with_only_utilities_on_subprocess_path(self):
        import subprocess
        import sys

        install_dir = ROOT / "tools" / "install"
        sys.path.insert(0, str(install_dir))
        try:
            import herdr_integration
            block, _ = herdr_integration._history_block("hearting", {"original_absent": True})
        finally:
            sys.path.remove(str(install_dir))
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config" / "herdr" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_bytes(block)
            code = (
                f"import sys; sys.path.insert(0, {str(ROOT / 'utilities')!r}); "
                "import session_tidy as st; "
                "st.seat_lineage=lambda *a, **k: [{"
                "'harness':'claude','sid':'current','relation':'confirmed',"
                "'predecessor':{'harness':'claude','sid':'previous'},'transcript':{}}, "
                "{'harness':'claude','sid':'previous','transcript':{'status':'available'}}]; "
                "seat=st.Seat('pane','seat-key','pane-id','claude'); "
                "print(st.build_human_history_notice(seat,'claude','current',env={"
                "'HERDR_ACTIVE_PANE_ID':'workspace:pane','HERDR_SOCKET_PATH':'/tmp/socket',"
                f"'HOME':{str(home)!r}}}))"
            )
            proc = subprocess.run([sys.executable, "-c", code], cwd=home,
                                  env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
                                  capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Ctrl-b Shift-H", proc.stdout)

    def test_manual_clear_without_booking_stays_an_unconfirmed_neighbor(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.record_event(seat, "claude", "sid-old", "start", source="startup",
                            cwd=str(self.cwd), now=100)
            st.run_hook("claude", "start", "sid-new", source="clear", cwd=str(self.cwd), now=200)
            rows = st._read_ledger_lines(seat)
            lineage = st._project_seat_lineage(rows, {}, {}, sid="sid-new")
            notice = st.build_human_history_notice(seat, "claude", "sid-new")
        self.assertFalse(any(row.get("event") == "succession" for row in rows))
        self.assertEqual(lineage[0]["relation"], "chronological")
        self.assertIsNone(st._confirmed_predecessor(lineage, "claude", "sid-new"))
        self.assertEqual(notice, "")

    def test_lineage_history_expired_missing_and_unreadable_are_distinct(self):
        rows = [{"event": "summary", "harness": "claude", "sid": name, "ts": i,
                 "first_seen": i} for i, name in enumerate(("expired", "missing", "unreadable"), 1)]
        cards = {("claude", "expired"): [{"generation": 7, "path": None, "status": "expired"}],
                 ("claude", "unreadable"): [{"generation": 1, "path": "/card/bad", "status": "unreadable"}]}
        locations = {("claude", name): {"path": None, "status": status}
                     for name, status in (("expired", "missing"), ("missing", "missing"),
                                          ("unreadable", "unreadable"))}
        result = st._project_seat_lineage(rows, cards, locations)
        by_sid = {row["sid"]: row for row in result}
        self.assertEqual(by_sid["expired"]["card"]["status"], "expired")
        self.assertEqual(by_sid["missing"]["card"]["status"], "none")
        self.assertEqual(by_sid["unreadable"]["card"]["status"], "unreadable")
        self.assertEqual(by_sid["unreadable"]["transcript"]["status"], "unreadable")

    def test_lineage_query_reads_existing_cards_and_transcripts_without_state_writes(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            transcript = self.iso.root / "previous-session.jsonl"
            transcript.write_text('{"type":"message"}\n', encoding="utf-8")
            st.record_event(seat, "claude", "sid-old", "start", source="startup",
                            transcript=str(transcript), cwd=str(self.cwd), now=100)
            st.write_card(seat, "claude", "sid-old", "이전 카드", cwd=str(self.cwd), now=101)
            st.record_event(seat, "claude", "sid-new", "start", source="clear",
                            cwd=str(self.cwd), now=200)
            st._append_ledger_entry(seat, {"ts": 201, "event": "handover", "harness": "claude",
                                           "sid": "sid-new", "from": {"harness": "claude", "sid": "sid-old"},
                                           "source": "clear"})
            st.write_card(seat, "claude", "sid-new", "현재 카드", cwd=str(self.cwd), now=202)
            ledger = st._ledger_path(seat).read_bytes()
            before = {str(path.relative_to(self.state)): path.read_bytes()
                      for path in self.state.rglob("*") if path.is_file()}
            lineage = st.seat_lineage(seat, harness="claude", sid="sid-new")
            after = {str(path.relative_to(self.state)): path.read_bytes()
                     for path in self.state.rglob("*") if path.is_file()}
        self.assertEqual(lineage[0]["relation"], "confirmed")
        self.assertEqual(lineage[0]["card"]["generation"], 2)
        self.assertEqual(lineage[1]["sid"], "sid-old")
        self.assertEqual(lineage[1]["card"]["generation"], 1)
        self.assertEqual(lineage[1]["transcript"], {"path": str(transcript), "status": "available"})
        self.assertEqual(ledger, (self.state / "sessions" / f"{seat.key}.jsonl").read_bytes())
        self.assertEqual(before, after)

    def test_confirmed_clear_card_injection_includes_previous_transcript_within_cap(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            transcript = self.iso.root / "previous-clear.jsonl"
            transcript.write_text('{"type":"message"}\n', encoding="utf-8")
            st.record_event(seat, "claude", "sid-before", "start", source="startup",
                            transcript=str(transcript), cwd=str(self.cwd), now=100)
            st.write_card(seat, "claude", "sid-before", "진행 중: 이전 일\n세부 " * 500,
                          cwd=str(self.cwd), now=101)
            st._append_ledger_entry(seat, {"ts": 199, "event": "handover", "harness": "claude",
                                           "sid": "sid-after", "from": {"harness": "claude",
                                           "sid": "sid-before"}, "source": "clear"})
            st.write_notice(seat, "정리 결과 한 줄", author_harness="claude", author_sid="sid-before")
            output = st.run_hook("claude", "start", "sid-after", source="clear", cwd=str(self.cwd), now=200)
        self.assertIn("이전 대화: claude sid-befo · 원문 ", output)
        self.assertIn(str(transcript), output)
        self.assertLessEqual(len(output.encode("utf-8")), st.INJECTION_MAX_BYTES)

    def test_previous_transcript_footer_and_memory_stay_under_2400_utf8_bytes(self):
        previous = {"harness": "claude", "sid": "a" * 36,
                    "transcript": {"path": "/" + "한" * 500, "status": "available"}}
        card = {"author": {"harness": "claude", "sid": "b" * 36},
                "authored_at_epoch": 10, "body": "본문 " * 1200}
        text = st.build_card_injection(card, Path("/card.json"), 10, 1600,
                                       previous_transcript=previous)
        self.assertIn("이전 대화: claude", text)
        self.assertIn("원문 경로 생략", text)
        self.assertLessEqual(len(text.encode("utf-8")), 1600)

    def test_confirmed_edge_beats_chronology_for_previous_transcript(self):
        rows = [
            {"event": "start", "harness": "claude", "sid": "sid-a", "ts": 1, "first_seen": 1,
             "source": "startup"},
            {"event": "start", "harness": "claude", "sid": "sid-b", "ts": 2, "first_seen": 2,
             "source": "startup"},
            {"event": "start", "harness": "claude", "sid": "sid-c", "ts": 3, "first_seen": 3,
             "source": "clear"},
            {"event": "handover", "harness": "claude", "sid": "sid-c",
             "from": {"harness": "claude", "sid": "sid-a"}, "source": "clear"},
        ]
        locations = {("claude", "sid-a"): {"path": "/raw/a.jsonl", "status": "unreadable"},
                     ("claude", "sid-b"): {"path": "/raw/b.jsonl", "status": "available"}}
        for harness in (None, "claude"):
            projected = st._project_seat_lineage(rows, {}, locations, harness=harness, sid="sid-c")
            self.assertEqual(projected[0]["predecessor"], {"harness": "claude", "sid": "sid-a"})
            self.assertEqual(projected[1]["sid"], "sid-b")
            self.assertIsNone(projected[1]["predecessor"])
            previous = st._confirmed_predecessor(projected, "claude", "sid-c")
            self.assertEqual(previous["sid"], "sid-a")
        card = {"author": {"harness": "claude", "sid": "sid-a"}, "authored_at_epoch": 10, "body": "본문"}
        text = st.build_card_injection(card, Path("/card.json"), 10, 2000, previous_transcript=previous)
        self.assertIn("이전 대화: claude sid-a · 원문 읽기 불가", text)
        self.assertNotIn("/raw/b.jsonl", text)
        chronological = st._project_seat_lineage(rows[:3], {}, locations, sid="sid-c")
        self.assertIsNone(st._confirmed_predecessor(chronological, "claude", "sid-c"))

    def test_clear_footer_names_handover_source_not_intervening_session(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            before = self.iso.root / "before.jsonl"
            middle = self.iso.root / "middle.jsonl"
            for path in (before, middle):
                path.write_text('{"type":"message"}\n', encoding="utf-8")
            st.record_event(seat, "claude", "sid-before", "start", source="startup",
                            transcript=str(before), cwd=str(self.cwd), now=100)
            st.write_card(seat, "claude", "sid-before", "진행 중: 이전 일", cwd=str(self.cwd), now=101)
            st.record_event(seat, "claude", "sid-middle", "start", source="startup",
                            transcript=str(middle), cwd=str(self.cwd), now=150)
            st._append_ledger_entry(seat, {"ts": 199, "event": "handover", "harness": "claude",
                                           "sid": "sid-after", "from": {"harness": "claude",
                                           "sid": "sid-before"}, "source": "clear"})
            output = st.run_hook("claude", "start", "sid-after", source="clear", cwd=str(self.cwd), now=200)
        self.assertIn(f"이전 대화: claude sid-befo · 원문 {before}", output)
        self.assertNotIn(str(middle), output)
        self.assertNotIn("sid-midd", output)
        self.assertLessEqual(len(output.encode("utf-8")), st.INJECTION_MAX_BYTES)

    def test_clear_footer_for_unrecorded_handover_source_reports_missing_transcript(self):
        with self.library():
            seat = st.resolve_seat("claude", str(self.cwd))
            st.write_card(seat, "claude", "sid-ghost", "진행 중: 이전 일", cwd=str(self.cwd), now=101)
            st.record_event(seat, "claude", "sid-other", "start", source="startup",
                            cwd=str(self.cwd), now=150)
            st._append_ledger_entry(seat, {"ts": 199, "event": "handover", "harness": "claude",
                                           "sid": "sid-after", "from": {"harness": "claude",
                                           "sid": "sid-ghost"}, "source": "clear"})
            output = st.run_hook("claude", "start", "sid-after", source="clear", cwd=str(self.cwd), now=200)
        self.assertIn("이전 대화: claude sid-ghos · 원문 없음", output)
        self.assertNotIn("sid-othe", output.split("이전 대화:", 1)[-1])

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
        with self.library():
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
        lines = [json.loads(line) for line in (next((self.state / "sessions").glob("*.jsonl")))
                 .read_text(encoding="utf-8").splitlines()]
        self.assertLess(sum(row.get("event") == "prompt" for row in lines), 5)
        self.assertEqual(sum(row.get("event") == "real-prompt" for row in lines), 5)
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

    def write_compact_card(self, seat, sid="sid-A", identity="compact-A"):
        context = {"trigger": "auto", "identity": identity, "transcript": ""}
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}), st.seat_lock(seat.key):
            st.write_card(seat, "claude", sid, "automatic summary", cwd=str(self.cwd),
                          prompt_seq=st.read_prompt_seq(seat), origin="runtime-compact",
                          compact_context=context)
        return context

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

    def test_compact_booking_uses_three_hours_and_inherits_only_pending_no_continue(self):
        extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                 "HEARTING_TIDY_HERDR": str(self.fake_herdr)}
        with self.iso.patched_environ(extra), mock.patch.object(clear, "_start_helper", return_value=4242):
            seat = st.resolve_seat("claude", str(self.cwd))
            clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd), no_continue=True)
            context = self.write_compact_card(seat)
            self.assertEqual(clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd),
                                                        compact_context=context), "clear=scheduled continue=off")
            req = clear.read_reservation(seat.key)
            self.assertEqual(req["deadline"] - req["created"], clear.COMPACT_CLEAR_DEADLINE_SEC)
            self.assertTrue(req["continue_off"])
            self.assertEqual(req["compact"]["identity"], context["identity"])

    def test_compact_does_not_replace_an_in_flight_clear_claim(self):
        extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                 "HEARTING_TIDY_HERDR": str(self.fake_herdr)}
        with self.iso.patched_environ(extra), mock.patch.object(clear, "_start_helper", return_value=4242):
            seat = st.resolve_seat("claude", str(self.cwd))
            st.write_card(seat, "claude", "sid-A", "manual card", cwd=str(self.cwd))
            clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd))
            old = clear.read_reservation(seat.key)
            claimed, reason = clear.claim_clear(clear.reservation_path(seat.key), old["nonce"])
            self.assertEqual(reason, "")
            context = self.write_compact_card(seat, identity="compact-B")
            self.assertEqual(clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd),
                                                        compact_context=context),
                             "clear=skipped reason=clear-in-flight")
            held = clear.read_reservation(seat.key)
            self.assertEqual(held["nonce"], old["nonce"])
            self.assertEqual(held["clear_claim"], claimed["clear_claim"])

    def test_clear_claim_has_one_consumer_and_revalidates_compact_evidence(self):
        _line, seat = self.book()
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            req = clear.read_reservation(seat.key)
            first, reason = clear.claim_clear(clear.reservation_path(seat.key), req["nonce"])
            self.assertEqual(reason, "")
            self.assertIsNone(clear.claim_clear(clear.reservation_path(seat.key), req["nonce"])[0])
            self.assertEqual(clear.validate_request(clear.reservation_path(seat.key), req["nonce"])[1], "superseded")
            self.assertEqual(clear.validate_request(clear.reservation_path(seat.key), req["nonce"],
                                                     _sending_claim=first["clear_claim"])[1], "")

    def test_compact_to_compact_and_compact_to_manual_keep_one_booking_and_only_pending_stop(self):
        extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                 "HEARTING_TIDY_HERDR": str(self.fake_herdr)}
        with self.iso.patched_environ(extra), mock.patch.object(clear, "_start_helper", return_value=4242):
            seat = st.resolve_seat("claude", str(self.cwd))
            first_ctx = self.write_compact_card(seat, identity="compact-1")
            self.assertEqual(clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd),
                                                        compact_context=first_ctx), "clear=scheduled")
            first = clear.read_reservation(seat.key)
            # the same callback again coalesces into the booking it already made
            self.assertEqual(clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd),
                                                        compact_context=first_ctx), "clear=scheduled")
            self.assertEqual(clear.read_reservation(seat.key)["nonce"], first["nonce"])
            second_ctx = self.write_compact_card(seat, identity="compact-2")
            clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd), compact_context=second_ctx)
            second = clear.read_reservation(seat.key)
            self.assertNotEqual(second["nonce"], first["nonce"])
            self.assertEqual(second["compact"]["identity"], "compact-2")
            self.assertFalse(second["continue_off"])
            # a manual tidy afterwards keeps its own explicit choice and replaces the compact booking
            self.assertEqual(clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd), no_continue=True),
                             "clear=scheduled continue=off")
            manual = clear.read_reservation(seat.key)
            self.assertNotIn("origin", manual)
            self.assertNotEqual(manual["nonce"], second["nonce"])
            # a stop carried by a finished booking, or by another session's booking, is not inherited
            clear._finish(seat.key, manual["nonce"], "cleared")
            third_ctx = self.write_compact_card(seat, identity="compact-3")
            clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd), compact_context=third_ctx)
            self.assertFalse(clear.read_reservation(seat.key)["continue_off"])

    def test_replayed_compact_resumes_helper_start_failure_without_replacing_booking(self):
        extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                 "HEARTING_TIDY_HERDR": str(self.fake_herdr)}
        with self.iso.patched_environ(extra):
            seat = st.resolve_seat("claude", str(self.cwd))
            context = self.write_compact_card(seat, identity="compact-resume")
            with mock.patch.object(clear, "_start_helper", side_effect=[OSError("injected"), 4242]) as start:
                first = clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd),
                                                   compact_context=context, operation_id="compact-resume",
                                                   queue_id="tidy-op-resume")
                before = clear.read_reservation(seat.key)
                self.assertEqual(first, "clear=pending reason=helper-not-started-OSError")
                self.assertEqual(before["status"], "reserved")
                self.assertEqual(before["helper_start"]["state"], "failed")

                second = clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd),
                                                    compact_context=context, operation_id="compact-resume",
                                                    queue_id="tidy-op-resume")
                after = clear.read_reservation(seat.key)
                self.assertEqual(second, "clear=scheduled")
                self.assertEqual(after["nonce"], before["nonce"])
                self.assertEqual(after["queue_id"], "tidy-op-resume")
                self.assertEqual(start.call_count, 2)

                clear.cancel(seat)
                replay = clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd),
                                                    compact_context=context, operation_id="compact-resume",
                                                    queue_id="tidy-op-resume")
                self.assertEqual(replay, "clear=off")
                self.assertEqual(start.call_count, 2)

    def test_compact_never_inherits_a_stop_from_another_session_booking(self):
        extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                 "HEARTING_TIDY_HERDR": str(self.fake_herdr)}
        with self.iso.patched_environ(extra), mock.patch.object(clear, "_start_helper", return_value=4242):
            seat = st.resolve_seat("claude", str(self.cwd))
            clear.schedule_for_enqueue(seat, "claude", "sid-OLD", str(self.cwd), no_continue=True)
            context = self.write_compact_card(seat, sid="sid-A", identity="compact-other")
            result = clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd), compact_context=context)
            self.assertEqual(result, "clear=skipped reason=booking-target-changed")
            self.assertEqual(clear.read_reservation(seat.key)["sid"], "sid-OLD")

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


class TidyOneCallTest(TidyCase):
    """``tidy`` = ``card`` then, only for a written card, ``enqueue`` (+ ``handoff``) in one call."""

    def setUp(self):
        super().setUp()
        self.fake_herdr = self.iso.root / "fake-herdr"
        self.fake_herdr.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fake_herdr.chmod(0o755)
        self.started = []

    reservation = ClearBookingTest.reservation

    def tidy(self, text, *, sid="sid-A", harness="claude", no_clear=False, no_continue=False,
             handoff=None, extra=None):
        env = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
               "HEARTING_TIDY_HERDR": str(self.fake_herdr)}
        env.update(extra or {})
        args = SimpleNamespace(harness=harness, session_id=sid, cwd=str(self.cwd), file=None, text=text,
                               no_clear=no_clear, no_continue=no_continue, handoff=handoff)
        out = io.StringIO()
        with self.iso.patched_environ(env), contextlib.redirect_stdout(out), \
                mock.patch.object(clear, "_start_helper",
                                  side_effect=lambda key, nonce: self.started.append(nonce) or 4242), \
                mock.patch.object(runner, "start_detached", return_value=None):
            code = st.cmd_tidy(args)
            seat = st.resolve_seat(harness, str(self.cwd))
        return code, out.getvalue().splitlines(), seat

    def queued(self):
        folder = self.state / "queue"
        return [json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")] if folder.exists() else []

    def test_one_call_writes_the_card_queues_the_tidy_and_books_the_clear(self):
        self.hook("claude", "prompt", "sid-A")
        code, lines, seat = self.tidy("진행 중: 한 번에\n다음 할 일: 기다림")
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("card="), lines)
        self.assertRegex(lines[1], r"^enqueue=\S+ seat=\S+ status=queued clear=scheduled$")
        self.assertEqual(lines[-1], "say=입력창이 비어 있으면 곧 이 창을 비우고, 새 세션이 카드를 받아 이어서 진행합니다. 그 전에 입력하시면 비우지 않습니다.")
        with self.iso.patched_environ():
            self.assertEqual(st.read_latest_card(seat)["body"], "진행 중: 한 번에\n다음 할 일: 기다림")
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.reservation(seat)["status"], "reserved")
        self.assertEqual(len(self.started), 1)

    def test_no_card_means_no_queue_and_no_clear(self):
        self.hook("claude", "prompt", "sid-A")
        code, lines, seat = self.tidy("   ")
        self.assertEqual((code, lines), (1, ["card=none reason=empty-body", "enqueue=none reason=no-card"]))
        self.assertEqual(self.queued(), [])
        self.assertIsNone(self.reservation(seat))
        self.assertEqual(self.started, [])
        with mock.patch.object(st, "write_card", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.tidy("카드")
        self.assertEqual(self.queued(), [])
        self.assertIsNone(self.reservation(seat))

    def test_a_worker_gets_nothing(self):
        code, lines, seat = self.tidy("워커 카드", extra={"AGENT_SESSION_ROLE": "worker"})
        self.assertEqual((code, lines), (0, ["card=none reason=worker"]))
        self.assertFalse((self.state / "cards").exists())
        self.assertEqual(self.queued(), [])

    def test_modes_keep_their_enqueue_meaning(self):
        self.hook("claude", "prompt", "sid-A")
        _code, lines, seat = self.tidy("카드", no_clear=True)
        self.assertTrue(lines[1].endswith("clear=off"), lines)
        self.assertEqual(lines[-1], "say=이 창은 그대로 둡니다. 이제 /clear 하거나 닫아도 됩니다.")
        self.assertIsNone(self.reservation(seat))
        _code, lines, seat = self.tidy("카드 2", no_continue=True)
        self.assertTrue(lines[1].endswith("clear=scheduled continue=off"), lines)
        self.assertEqual(lines[-1], "say=입력창이 비어 있으면 곧 이 창을 비웁니다. 그 전에 입력하시면 비우지 않습니다.")

    def test_handoff_runs_after_the_queue_and_keeps_its_own_line_and_exit_code(self):
        self.hook("claude", "prompt", "sid-A")
        calls = []

        def handoff(seat, target):
            calls.append((len(self.queued()), self.reservation(seat)["status"], target))
            return "prompted=failed target=peer-x", 1

        with mock.patch.object(runner, "handoff", side_effect=handoff):
            code, lines, seat = self.tidy("카드", handoff="peer-x")
        self.assertEqual(code, 1)
        self.assertEqual(calls, [(1, "reserved", "peer-x")])
        self.assertEqual(lines[2], "prompted=failed target=peer-x")
        self.assertEqual(lines[-1], "say=입력창이 비어 있으면 곧 이 창을 비웁니다. 그 전에 입력하시면 비우지 않습니다.")
        self.assertEqual(self.reservation(seat)["status"], "reserved")     # a failed handoff keeps the clear

    def test_an_older_card_and_prompt_do_not_block_the_new_one_call_card(self):
        self.hook("claude", "prompt", "sid-A")
        self.card("sid-A", "이전 카드")
        self.hook("claude", "prompt", "sid-A")
        _code, lines, seat = self.tidy("새 카드")
        self.assertTrue(lines[1].endswith("clear=scheduled"), lines)   # the new card follows the newest prompt

    def test_report_line_for_each_outcome(self):
        line = st.tidy_report_line
        self.assertEqual(line("clear=scheduled", origin="idle"), "입력창이 비어 있으면 곧 이 창을 비웁니다. 그 전에 입력하시면 비우지 않습니다.")
        self.assertEqual(line("clear=manual hint=/new", harness="opencode"), "이제 /new 하거나 닫아도 됩니다.")
        self.assertEqual(line("clear=skipped reason=new-input", harness="opencode"), "이제 /new 하거나 닫아도 됩니다.")
        self.assertEqual(line("clear=skipped reason=new-input", harness="codex"), "이제 /clear 하거나 닫아도 됩니다.")
        self.assertIn("다음 입력", line("clear=pending reason=x"))

    def test_the_automatic_requests_ask_for_one_marked_reply(self):
        for origin in ("idle", "compact"):
            text = clear.idle_card_request_text({"nonce": "n1", "origin": origin})
            begin, end = st.reply_card_markers("n1")
            self.assertIn(begin, text)
            self.assertIn(end, text)
            self.assertNotIn("\n", text)
            self.assertNotIn("enqueue", text)
            self.assertTrue(text.endswith("요청 식별자: n1"))
        self.assertEqual(st.reply_card_body(f"{begin}\n 진행 중: x \n{end}\n", "n1"), "진행 중: x")
        for broken in (f"{begin}\n진행 중: x", f"앞말\n{begin}\n진행 중: x\n{end}", f"{begin}\n\n{end}",
                       f"{begin}\n진행 중: x\n{end} 뒷말"):
            self.assertIsNone(st.reply_card_body(broken, "n1"), broken)


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
        self.jobs = self.iso.root / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.write_text("", encoding="utf-8")
        self.extra = {"HERDR_PANE_ID": PANE, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                      "HEARTING_TIDY_HERDR": str(self.fake_herdr), "HEARTING_TIDY_CLEAR_OBSERVE": "0.3",
                      "AGENT_DISPATCH_JOBS": str(self.jobs)}
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

    def test_helper_restart_uses_only_the_reserved_codex_socket(self):
        seat = st.Seat("pane", "a" * 32, pane=PANE, harness="codex",
                       socket_path="/run/herdr/booked.sock")
        req = {"schema": clear.SCHEMA, "nonce": "0123456789abcdef", "status": "reserved",
               "seat": seat.as_dict(), "harness": "codex", "sid": "thread-A",
               "herdr_transport": {"socket_path": seat.socket_path}}
        process = mock.Mock(pid=4321)
        # The reservation is written and read back under the same isolated state root.
        with self.iso.patched_environ(self.extra):
            clear._write_reservation(req)
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/run/herdr/foreign.sock",
                                              "AGENT_HERDR_SESSION": "foreign"}), \
                    mock.patch.object(clear.subprocess, "Popen", return_value=process) as popen:
                self.assertEqual(clear._start_helper(seat.key, req["nonce"]), 4321)
        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(child_env.get("HERDR_SOCKET_PATH"), seat.socket_path)
        self.assertNotIn("AGENT_HERDR_SESSION", child_env)
        self.assertNotIn("HERDR_PANE_ID", child_env)

    def booked(self, harness="claude", sid="sid-A"):
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "_start_helper", return_value=1):
            seat = st.resolve_seat(harness, str(self.cwd))
            clear.schedule_for_enqueue(seat, harness, sid, str(self.cwd))
            return seat, clear.read_reservation(seat.key)

    def compact_booked(self):
        path = self.iso.root / "sid-A.jsonl"
        path.write_text(json.dumps({"type": "assistant", "uuid": "a-before",
                                    "message": {"stop_reason": "tool_use", "content": [
                                        {"type": "text", "text": "tool call"}]}}) + "\n"
                        + json.dumps({"type": "user", "uuid": "u-tool", "message": {
                            "content": [{"type": "tool_result", "tool_use_id": "t", "content": "result"}]}})
                        + "\n", encoding="utf-8")
        info = path.stat()
        context = {"trigger": "auto", "identity": "compact-helper-test", "transcript": str(path),
                   "boundary_offset": info.st_size,
                   "transcript_identity": f"{info.st_dev}:{info.st_ino}"}
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "_start_helper", return_value=1):
            seat = st.resolve_seat("claude", str(self.cwd))
            with st.seat_lock(seat.key):
                st.write_card(seat, "claude", "sid-A", "full compact summary", cwd=str(self.cwd),
                              prompt_seq=st.read_prompt_seq(seat), origin="runtime-compact",
                              compact_context=context)
            clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd), compact_context=context)
            return seat, clear.read_reservation(seat.key), path

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

    def test_full_compact_false_idle_waits_for_end_turn_then_clears_and_enriches_same_card(self):
        import dispatch_seat_handover as handover
        seat, req, transcript = self.compact_booked()
        def owner_row(attempt):
            meta = {"attempt_id": attempt, "parent_sid": "sid-A", "parent_harness": "claude",
                    "dispatch_depth": "1", "worker_type": "owner", "route_id": "rt-1",
                    "route_hash": "sha256:aa", "route_node": "execute", "route_file": "/tmp/rt-1.json"}
            return "2026-10-15T00:00:00Z\topen\t/w\t/w\tslug\t" + ",".join(
                f"{key}={value}" for key, value in meta.items())
        self.jobs.write_text(owner_row("att-before") + "\n", encoding="utf-8")
        calls, snapshots, slept = [], [], []

        def wait(_pane, _timeout):
            calls.append("wait")
            return "idle"                    # herdr's compact-time idle is deliberately false

        def finish_turn():
            if not slept:
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"type": "assistant", "uuid": "a-final",
                                             "message": {"stop_reason": "end_turn", "content": [
                                                 {"type": "text", "text": "최종 결과: 끝"}]}}) + "\n")
                with self.jobs.open("a", encoding="utf-8") as handle:
                    handle.write(owner_row("att-after") + "\n")
            slept.append(True)

        real_snapshot = handover.publish_snapshot_locked
        def snapshot(*args, **kwargs):
            snapshots.append(1)
            return real_snapshot(*args, **kwargs)
        with self.iso.patched_environ(self.extra), mock.patch.object(handover, "publish_snapshot_locked",
                                                                       side_effect=snapshot):
            outcome = clear.run_helper(
                seat.key, req["nonce"], wait=wait,
                steward=lambda *_a: calls.append("clear") or {"cleared": "true", "new_session": "sid-B"},
                continuer=lambda *_a: calls.append("continue") or {"continued": "true"},
                sleep=lambda _s: finish_turn())
        self.assertEqual(outcome, "continued")
        self.assertEqual(calls, ["wait", "wait", "clear", "continue"])
        self.assertEqual(snapshots, [1])
        with self.iso.patched_environ(self.extra):
            snap = handover.read_snapshot(seat.key)
        self.assertEqual([binding["attempt"] for binding in snap["bindings"]], ["att-before", "att-after"])
        with self.iso.patched_environ():
            card = st.read_latest_card(seat)
        self.assertEqual(card["generation"], req["card_generation"])
        self.assertIn("최종 결과: 끝", card["body"])
        self.assertEqual(card["compact"]["summary"], "full compact summary")

    def test_compact_wait_exceeds_ten_minutes_but_stays_inside_three_hour_deadline(self):
        seat, req, transcript = self.compact_booked()
        now = [req["created"]]
        calls = []

        def advance(_seconds):
            now[0] += 601
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "assistant", "uuid": "a-late",
                                         "message": {"stop_reason": "end_turn", "content": [
                                             {"type": "text", "text": "600초 뒤 완료"}]}}) + "\n")

        with self.iso.patched_environ(self.extra), mock.patch.object(st, "now_epoch", side_effect=lambda: now[0]):
            outcome = clear.run_helper(
                seat.key, req["nonce"], wait=lambda *_a: calls.append("wait") or "idle",
                steward=lambda *_a: calls.append("clear") or {"cleared": "true", "new_session": "sid-B"},
                continuer=lambda *_a: {"continued": "true"}, sleep=advance)
        self.assertEqual(outcome, "continued")
        self.assertGreater(now[0] - req["created"], 600)
        self.assertLess(now[0] - req["created"], clear.COMPACT_CLEAR_DEADLINE_SEC)
        self.assertEqual(calls, ["wait", "wait", "clear"])

    def test_compact_three_hour_expiry_never_calls_steward(self):
        seat, req, _transcript = self.compact_booked()
        now = [req["created"]]
        calls = []

        def advance(_seconds):
            now[0] = req["deadline"] + 1

        with self.iso.patched_environ(self.extra), mock.patch.object(st, "now_epoch", side_effect=lambda: now[0]):
            outcome = clear.run_helper(seat.key, req["nonce"], wait=lambda *_a: "idle",
                                       steward=lambda *_a: calls.append("clear") or {"cleared": "true"},
                                       sleep=advance)
        self.assertEqual(outcome, "skipped")
        self.assertEqual(calls, [])
        notice = self.notices(seat)[0]
        self.assertIn("자동 정리", notice)
        self.assertNotIn("/clear", notice)
        self.assertNotIn("이어서해", notice)

    def test_false_idle_polls_with_bounded_sleeps_and_a_replaced_booking_stops_without_a_send(self):
        seat, req, _transcript = self.compact_booked()
        sleeps, calls = [], []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 5:                     # a newer tidy replaces the booking mid-wait
                clear.schedule_for_enqueue(seat, "claude", "sid-A", str(self.cwd))

        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "_start_helper", return_value=1):
            outcome = clear.run_helper(seat.key, req["nonce"], wait=lambda *_a: calls.append("wait") or "idle",
                                       steward=lambda *_a: calls.append("clear") or {"cleared": "true"},
                                       sleep=sleep)
        self.assertEqual(outcome, "superseded")
        self.assertEqual(calls, ["wait"] * 5)
        self.assertTrue(all(0 < value <= clear.COMPACT_POLL_SEC for value in sleeps))

    def test_recent_tail_refresh_failure_keeps_the_complete_summary_and_still_clears(self):
        seat, req, transcript = self.compact_booked()
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "assistant", "uuid": "a-final", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "끝"}]}}) + "\n")
        calls = []
        with self.iso.patched_environ(self.extra), \
                mock.patch.object(st, "update_compact_card_locked", side_effect=OSError("fixture")):
            outcome = clear.run_helper(seat.key, req["nonce"], wait=lambda *_a: "idle",
                                       steward=lambda *_a: calls.append("clear") or {
                                           "cleared": "true", "new_session": "sid-B"},
                                       continuer=lambda *_a: calls.append("continue") or {"continued": "true"},
                                       sleep=lambda _s: None)
        self.assertEqual((outcome, calls), ("continued", ["clear", "continue"]))
        with self.iso.patched_environ():
            card = st.read_latest_card(seat)
        self.assertEqual(card["generation"], req["card_generation"])
        self.assertEqual(card["compact"]["summary"], "full compact summary")

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

    def test_same_seat_clear_injection_never_requests_handoff_accept(self):
        seat, req = self.booked()
        shown = []
        def late_start(_sec):
            with self.iso.patched_environ(self.extra):
                st.run_hook("claude", "start", "sid-B", source="clear", cwd=str(self.cwd),
                            emit=shown.append)
        with self.iso.patched_environ(self.extra):
            outcome = clear.run_helper(
                seat.key, req["nonce"], wait=lambda *_a: "idle",
                steward=lambda *_a: {"cleared": "unverified", "reason": "new-session-not-observed"},
                continuer=lambda *a: {"continued": "true"}, sleep=late_start)
        self.assertEqual(outcome, "continued")
        output = "".join(shown).lower()
        self.assertNotIn("handoff accept", output)
        self.assertNotIn("session-tidy accept", output)
        self.assertNotIn("peer-message accept", output)

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

    def open_route(self, route, digest):
        path = self.cwd / f"{route}.json"
        path.write_text(json.dumps({"route_id": route, "route_hash": digest}), encoding="utf-8")
        return str(path)

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
                        env=os.environ, emit=out.append)
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

    def test_the_twelve_job_cap_keeps_every_attempt_for_each_selected_job(self):
        self.row("att-active-1", node="active-one")
        self.row("att-active-2", node="active-two", worker="frame")
        route_file = self.open_route("rt-frame", "sha256:frame")
        frame_attempts = []
        for index in range(12):
            meta = self.row(f"att-frame-{index}", status="done", route="rt-frame", node="frame",
                            worker="frame", digest="sha256:frame", route_file=route_file)
            frame_attempts.append(meta["attempt_id"])

        self.assertEqual(self.snapshot(), 14)  # logical job cap, flat binding count
        with self.iso.patched_environ():
            snap = self.handover.read_snapshot(self.seat().key)
        self.assertEqual(len(snap["bindings"]), 14)
        self.assertEqual({(b["route"], b["hash"], b["node"]) for b in snap["bindings"]}, {
            ("rt-1", "sha256:aa", "active-one"),
            ("rt-1", "sha256:aa", "active-two"),
            ("rt-frame", "sha256:frame", "frame"),
        })
        frame_bindings = [b for b in snap["bindings"] if b["node"] == "frame"]
        self.assertEqual({b["attempt"] for b in frame_bindings}, set(frame_attempts))
        self.assertEqual({b["parent"] for b in frame_bindings}, {"sid-A"})

    def test_active_jobs_precede_finished_jobs_and_thirteen_active_jobs_are_deterministic(self):
        self.row("att-running", status="running", node="running")
        route_file = self.open_route("rt-finished", "sha256:finished")
        for index in range(12):
            self.row(f"att-finished-{index}", status="done", route="rt-finished", node=f"done-{index}",
                     digest="sha256:finished", route_file=route_file)
        self.assertEqual(self.snapshot(), 12)
        with self.iso.patched_environ():
            snap = self.handover.read_snapshot(self.seat().key)
        nodes = {b["node"] for b in snap["bindings"]}
        self.assertIn("running", nodes)
        self.assertEqual(nodes & {f"done-{index}" for index in range(12)},
                         {f"done-{index}" for index in range(1, 12)})

        self.rows.clear()
        for index in range(13):
            self.row(f"att-open-{index}", node=f"open-{index}")
        self.assertEqual(self.snapshot(), 12)
        with self.iso.patched_environ():
            snap = self.handover.read_snapshot(self.seat().key)
        self.assertEqual({b["node"] for b in snap["bindings"]},
                         {f"open-{index}" for index in range(1, 13)})

        self.rows.clear()
        route_file = self.open_route("rt-latest", "sha256:latest")
        self.row("att-latest-done", status="open", route="rt-latest", node="latest",
                 digest="sha256:latest", route_file=route_file)
        self.row("att-latest-done", status="done", route="rt-latest", node="latest",
                 digest="sha256:latest", route_file=route_file)
        for index in range(12):
            self.row(f"att-final-{index}", status="done", route="rt-latest", node=f"final-{index}",
                     digest="sha256:latest", route_file=route_file)
        self.assertEqual(self.snapshot(), 12)
        with self.iso.patched_environ():
            snap = self.handover.read_snapshot(self.seat().key)
        self.assertNotIn("latest", {b["node"] for b in snap["bindings"]})
        self.assertEqual({b["node"] for b in snap["bindings"]},
                         {f"final-{index}" for index in range(12)})

    def test_a_confirmed_clear_refreshes_only_the_selected_jobs_after_snapshot(self):
        original = self.row("att-before-clear", node="selected")
        self.assertEqual(self.snapshot(now=st.now_epoch() - 5), 1)

        # This permitted retry appears in the registry after enqueue but before
        # the confirmed start hook.  The hook must bind its exact row identity.
        retry = self.row("att-after-snapshot", node="selected", worker="frame")
        self.row("att-unselected", node="other")
        out, rows = self.start("sid-B")
        self.assertEqual(len(rows), 1)
        expected = {("att-before-clear", "sid-A"), ("att-after-snapshot", "sid-A")}
        with self.iso.patched_environ():
            snapshot = self.handover.read_snapshot(self.seat().key)
        snapshot_pairs = {(b["attempt"], b["parent"]) for b in snapshot["bindings"]}
        ledger_pairs = {(b["attempt"], b["parent"]) for b in rows[0]["bindings"]}
        self.assertEqual(snapshot_pairs, expected)
        self.assertEqual(ledger_pairs, expected)
        self.assertEqual(retry["parent_sid"], "sid-A")
        self.assertEqual(original["parent_sid"], "sid-A")
        self.assertNotIn("att-unselected", {b["attempt"] for b in rows[0]["bindings"]})

    def test_the_clear_start_reads_the_registry_once_and_asks_only_about_new_attempts(self):
        # The card hook has seconds: a full registry walk or route verification per binding of
        # the same job can let a cleared supervisor's SessionStart time out and lose its card.
        route_file = self.open_route("rt-frame", "sha256:frame")
        for index in range(12):
            self.row(f"att-frame-{index}", status="done", route="rt-frame", node="frame", worker="frame",
                     digest="sha256:frame", route_file=route_file)
        self.row("att-open", route_file="/tmp/rt-1.json")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = self.seat()
            with st.seat_lock(seat.key):
                st.record_event(seat, "claude", "sid-A", "start", cwd=str(self.cwd), now=st.now_epoch() - 9)
                st.write_card(seat, "claude", "sid-A", "카드 본문", cwd=str(self.cwd))
                self.assertEqual(self.handover.write_snapshot_locked(
                    seat, "claude", "sid-A", now=st.now_epoch() - 5, jobs=self.jobs), 13)
        self.row("att-retry", worker="frame", route_file="/tmp/rt-1.json")

        import route_authority
        asked, reads = [], []
        decide, read = route_authority.decide_continuation, self.handover.latest_rows
        with mock.patch.object(route_authority, "decide_continuation",
                               side_effect=lambda **kw: asked.append(kw["metadata"]["attempt_id"]) or decide(**kw)), \
                mock.patch.object(self.handover, "latest_rows", side_effect=lambda jobs: reads.append(jobs) or read(jobs)):
            out, rows = self.start("sid-B")
        text = "\n".join(out)
        self.assertIn("카드 본문", text)
        self.assertIn("[이어받은 진행 작업] route=rt-1", text)
        self.assertEqual(asked, ["att-retry"])
        self.assertEqual(len(reads), 1)                     # only resume_line needs the compatibility mapping
        self.assertEqual({b["attempt"] for b in rows[0]["bindings"]},
                         {f"att-frame-{index}" for index in range(12)} | {"att-open", "att-retry"})

    def test_retire_selects_the_kept_union_once_and_preserves_all_flat_bindings(self):
        shared_route = self.open_route("rt-shared", "sha256:shared")
        finished_route = self.open_route("rt-finished", "sha256:finished")
        self.row("att-kept-1", parent="sid-B", route="rt-shared", node="shared",
                 digest="sha256:shared", route_file=shared_route)
        self.row("att-kept-2", parent="sid-B", route="rt-shared", node="shared", worker="frame",
                 digest="sha256:shared", route_file=shared_route)
        self.row("att-new-1", parent="sid-A", route="rt-shared", node="shared",
                 digest="sha256:shared", route_file=shared_route)
        self.row("att-new-2", parent="sid-A", route="rt-shared", node="shared", worker="frame",
                 digest="sha256:shared", route_file=shared_route)
        for index in range(12):
            self.row(f"att-done-{index}", parent="sid-A", status="done", route="rt-finished",
                     node=f"done-{index}", digest="sha256:finished", route_file=finished_route)

        with self.iso.patched_environ({"HERDR_PANE_ID": PANE_BESIDE}):
            seat = self.seat(PANE_BESIDE)
            with st.seat_lock(seat.key):
                st.record_event(seat, "claude", "sid-B", "start", cwd=str(self.cwd),
                                now=st.now_epoch() - 9)
                self.assertEqual(self.handover.write_snapshot_locked(
                    seat, "claude", "sid-B", now=st.now_epoch() - 5, jobs=self.jobs), 2)
        row = self.retire("sid-A", "claude", "sid-B", "claude")
        self.assertIsNotNone(row)
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE_BESIDE}):
            snapshot = self.handover.read_snapshot(seat.key)
        self.assertEqual(len(snapshot["bindings"]), 15)
        self.assertEqual(len({(b["jobs"], b["route"], b["hash"], b["node"])
                              for b in snapshot["bindings"]}), 12)
        self.assertEqual({(b["attempt"], b["parent"]) for b in snapshot["bindings"] if b["node"] == "shared"}, {
            ("att-kept-1", "sid-B"), ("att-kept-2", "sid-B"),
            ("att-new-1", "sid-A"), ("att-new-2", "sid-A"),
        })
        self.assertNotIn("att-done-0", {b["attempt"] for b in snapshot["bindings"]})
        self.assertEqual({b["attempt"] for b in row["bindings"]},
                         {b["attempt"] for b in snapshot["bindings"] if b["parent"] == "sid-A"})

        # The next clear uses the merged snapshot as its bounded authority set.
        _out, next_rows = self.start("sid-C", pane=PANE_BESIDE)
        self.assertEqual([(r["from"], r["sid"]) for r in next_rows],
                         [("sid-A", "sid-B"), ("sid-B", "sid-C")])
        self.assertEqual({(b["attempt"], b["parent"]) for b in next_rows[-1]["bindings"]},
                         {(b["attempt"], b["parent"]) for b in snapshot["bindings"]})

    def test_no_live_attempt_leaves_no_snapshot(self):
        self.row("att-1")
        self.assertEqual(self.snapshot(), 1)
        self.jobs.write_text("", encoding="utf-8")
        self.assertEqual(self.snapshot(), 0)
        with self.iso.patched_environ():
            self.assertIsNone(self.handover.read_snapshot(self.seat().key))

    def test_schema_one_snapshot_and_ledger_keep_legacy_flat_rows_readable(self):
        meta = self.row("att-legacy")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = self.seat()
            binding = self.handover.binding_of(self.jobs, meta)
            legacy_without_parent = {key: value for key, value in binding.items() if key != "parent"}
            snapshot = {"schema": 1, "seat": seat.as_dict(),
                        "from": {"harness": "claude", "sid": "sid-A"}, "at": st.now_epoch(),
                        "bindings": [binding, dict(binding), legacy_without_parent]}
            st.atomic_write_json(self.handover._snapshot_path(seat.key), snapshot)
            ledger_path = st._ledger_path(seat)
            st.ensure_dir(ledger_path.parent)
            ledger_path.write_text(json.dumps({"event": "handover", "from": "sid-A", "sid": "sid-B",
                "harness": "claude", "ts": 1, "bindings": [dict(binding), legacy_without_parent]}) + "\n")

            self.assertEqual(self.handover.read_snapshot(seat.key)["bindings"], snapshot["bindings"])
            self.assertEqual(len(self.handover.handover_rows(seat)), 1)
            self.assertEqual(self.handover.storage_recipients("sid-B"),
                             [("sid-B", None), ("sid-A", frozenset({"att-legacy"}))])

    def test_a_confirmed_clear_start_records_one_row_and_the_successor_answers(self):
        meta = self.row("att-1")
        self.snapshot(now=st.now_epoch() - 5)
        self.assertEqual(self.effective(meta), "sid-A")                 # nothing handed over yet
        out, rows = self.start("sid-B")
        self.assertEqual([(r["from"], r["sid"], r["source"]) for r in rows], [("sid-A", "sid-B", "clear")])
        self.assertEqual(self.effective(meta), "sid-B")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            self.assertTrue(self.handover.owns(meta, "sid-B", self.jobs))
            self.assertFalse(self.handover.owns(meta, "sid-A", self.jobs))    # the registered parent no longer owns after handover
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

    def test_the_card_drops_a_finished_attempt_once_its_route_is_closed(self):
        import dispatch_pending_delivery as pending
        path = Path(self.open_route("rt-1", "sha256:aa"))
        self.row("att-done", status="done", route_file=str(path))
        record_path = pending.record_path(self.jobs.parent, "sid-A", "delivery-owed")
        record_path.parent.mkdir(parents=True)
        record_path.write_text(json.dumps({"attempt_ids": ["att-done"], "state": "pending"}))
        self.snapshot(now=st.now_epoch() - 5)
        self.start("sid-B")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = self.seat()
            self.assertIn("route=rt-1", self.handover.resume_line(seat, "claude", "sid-B"))
            path.with_suffix(".outcome.json").write_text("{}")     # closed; the pending record lingers
            self.assertEqual(self.handover.resume_line(seat, "claude", "sid-B"), "")

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
                                                        env=os.environ, jobs=self.jobs)

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

    def test_retire_hands_open_route_after_terminal_receipt_is_acked(self):
        import dispatch_pending_delivery as pending
        path = self.cwd / ".agent_reports" / ".runtime" / "routes" / "rt-1.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"route_id": "rt-1", "route_hash": "sha256:aa"}))
        meta = self.row("att-done", status="done", route_file=str(path))
        record_path = pending.record_path(self.jobs.parent, "sid-A", "delivery-finished")
        record_path.parent.mkdir(parents=True)
        record_path.write_text(json.dumps({"attempt_ids": ["att-done"], "state": "acked"}))
        before = (self.jobs.read_bytes(), path.read_bytes(), record_path.read_bytes())
        row = self.retire("sid-A", "claude", "sid-B", "codex")
        self.assertIsNotNone(row)
        self.assertEqual([b["attempt"] for b in row["bindings"]], ["att-done"])
        self.assertEqual(self.effective(meta), "sid-B")
        self.assertEqual((self.jobs.read_bytes(), path.read_bytes(), record_path.read_bytes()), before)
        self.assertIsNone(self.retire("sid-A", "claude", "sid-C", "opencode", pane="test:pane-c"))
        self.assertEqual(self.retire("sid-A", "claude", "sid-B", "codex"), row)

    def test_clear_hands_open_route_without_a_pending_completion_record(self):
        path = self.cwd / "rt-1.json"
        path.write_text(json.dumps({"route_id": "rt-1", "route_hash": "sha256:aa"}))
        meta = self.row("att-done", status="done", route_file=str(path))
        self.assertEqual(self.snapshot(now=st.now_epoch() - 5), 1)
        self.start("sid-B")
        self.assertEqual(self.effective(meta), "sid-B")

    def test_done_closed_or_unverifiable_route_is_not_handed_over(self):
        path = self.cwd / "rt-1.json"
        meta = self.row("att-done", status="done", route_file=str(path))
        for raw in (None, "not-json", json.dumps({"route_id": "rt-other", "route_hash": "sha256:aa"}),
                    json.dumps({"route_id": "rt-1", "route_hash": "sha256:other"})):
            with self.subTest(raw=raw):
                if raw is not None:
                    path.write_text(raw)
                self.assertIsNone(self.retire("sid-A", "claude", "sid-B", "codex"))
                self.assertEqual(self.effective(meta), "sid-A")
        path.write_text(json.dumps({"route_id": "rt-1", "route_hash": "sha256:aa"}))
        path.with_suffix(".outcome.json").write_text(json.dumps({"terminal_gate_proven": False}))
        self.assertIsNone(self.retire("sid-A", "claude", "sid-B", "codex"))

    def test_finished_node_less_owner_keeps_its_verified_route_until_close(self):
        import owner_route_binding as owner
        with self.iso.patched_environ():
            route = owner.ROUTE.compile_route(
                "autopilot-code", "dev", "quick", ROOT, self.cwd / ".agent_reports",
                predicates=[], tracking="tracked", tracked_gate_evidence={
                    "spec_read": {"satisfied": True, "source": "canonical-prd-sha256"},
                    "drift_verdict": "within-spec", "workflow_mode": "tracked",
                    "artifact_guard": {"satisfied": True, "source": "conductor-prechecked"}},
                registered_headless_evidence={"candidates": [{"harness": "claude", "transport": "headless",
                    "surface": "registered-headless", "status": "supported", "probe_source": "fixture-probe",
                    "probe_time": "2026-10-07T00:00:00Z"}]})
        path = self.cwd / "route.json"
        path.write_text(json.dumps(route))
        meta = self.row("att-owner-done", status="done", node="", route=route["route_id"],
                        digest=route["route_hash"], route_file=str(path))
        for key in ("route_id", "route_hash", "route_file"):
            meta["owner_" + key] = meta.pop(key)
        self.jobs.write_text("2026-10-01T00:00:00Z\tdone\t/w\t/w\tslug\t" +
                             ",".join(f"{k}={v}" for k, v in meta.items()) + "\n")
        before = self.jobs.read_bytes()
        row = self.retire("sid-A", "claude", "sid-B", "codex")
        self.assertIsNotNone(row)
        self.assertEqual(self.effective(meta), "sid-B")
        self.assertEqual(self.jobs.read_bytes(), before)
        path.with_suffix(".outcome.json").write_text("{}")
        self.assertIsNone(self.retire("sid-B", "codex", "sid-C", "opencode", pane="test:pane-c"))

    # -- seat lineage: a node-less owner and the one answer for delivery and authority ------------------

    def owner_row(self, aid, status="done", parent="sid-A", route="rt-1", digest="sha256:aa"):
        """A depth-1 owner row: it carries no ``route_node`` key at all."""
        meta = {"attempt_id": aid, "parent_sid": parent, "parent_harness": "claude", "dispatch_depth": "1",
                "worker_type": "owner", "route_id": route, "route_hash": digest,
                "route_file": self.open_route(route, digest)}
        self.rows.append(f"2026-10-01T00:00:00Z\t{status}\t/w\t/w\tslug\t" + ",".join(f"{k}={v}" for k, v in meta.items()))
        self.jobs.write_text("\n".join(self.rows) + "\n", encoding="utf-8")
        return meta

    def frame_row(self, aid="att-frame"):
        return self.row(aid, status="open", route="rt-2", node="frame", worker="frame", digest="sha256:bb")

    def chain(self, owner_status="done"):
        """A snapshot -> B -> B snapshot -> C by two same-seat clears; returns (owner, frame)."""
        owner = self.owner_row("att-owner", status=owner_status)
        frame = self.frame_row()
        self.snapshot(now=st.now_epoch() - 9)
        self.start("sid-B")
        self.snapshot(sid="sid-B", now=st.now_epoch() - 5)
        self.start("sid-C")
        return owner, frame

    def ledger_rows(self, pane=PANE):
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            return self.handover.handover_rows(self.seat(pane))

    def append_row(self, pane, row):
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            path = st._ledger_path(self.seat(pane))
            st.ensure_dir(path.parent)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "handover", **row}) + "\n")

    def write_snapshot(self, pane, sid, bindings):
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            seat = self.seat(pane)
            st.atomic_write_json(self.handover._snapshot_path(seat.key), {
                "schema": 1, "seat": seat.as_dict(), "from": {"harness": "claude", "sid": sid},
                "at": st.now_epoch(), "bindings": bindings})

    def binding(self, meta):
        return self.handover.binding_of(self.jobs, meta)

    def owns(self, meta, sid):
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            return self.handover.owns(meta, sid, self.jobs)

    def keys(self, bindings):
        return {(b["route"], b["hash"], b["node"]) for b in bindings}

    def test_a_finished_node_less_owner_is_carried_through_the_second_clear(self):
        owner, frame = self.chain()
        rows = self.ledger_rows()
        self.assertEqual([(r["from"], r["sid"]) for r in rows], [("sid-A", "sid-B"), ("sid-B", "sid-C")])
        owner_key = ("rt-1", "sha256:aa", "")
        self.assertIn(owner_key, self.keys(rows[0]["bindings"]))
        self.assertIn(owner_key, self.keys(rows[1]["bindings"]))
        with self.iso.patched_environ():
            self.assertIn(owner_key, self.keys(self.handover.read_snapshot(self.seat().key)["bindings"]))
        self.assertEqual(self.effective(owner), "sid-C")
        self.assertEqual(self.effective(frame), "sid-C")

    def test_the_second_successor_may_replace_the_finished_owner(self):
        import route_authority
        owner, _frame = self.chain()
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            route_authority.require_replacement_parent(
                self.jobs, {}, owner, current_session=lambda: "sid-C")
            choice = route_authority.decide_continuation(
                action="handover", status="done", metadata=owner, jobs=self.jobs,
                facts={"actor": "sid-C", "eligible": True})
        self.assertTrue(choice.allowed, choice)

    def test_the_second_successor_resumes_an_owner_whose_route_keys_are_owner_prefixed(self):
        import route_authority
        owner, _frame = self.chain()
        reported = {("owner_" + k if k in ("route_id", "route_hash", "route_file") else k): v
                    for k, v in owner.items()}
        self.assertEqual(self.effective(reported), "sid-C")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            route_authority.require_replacement_parent(
                self.jobs, {}, reported, current_session=lambda: "sid-C")

    def test_an_already_broken_chain_still_reaches_its_end(self):
        owner = self.owner_row("att-owner")
        frame = self.frame_row()
        both = [self.binding(owner), self.binding(frame)]
        self.append_row(PANE, {"ts": 1, "from": "sid-A", "sid": "sid-B", "harness": "claude",
                               "source": "clear", "bindings": both})
        self.append_row(PANE, {"ts": 2, "from": "sid-B", "sid": "sid-C", "harness": "claude",
                               "source": "clear", "bindings": [self.binding(frame)]})
        self.write_snapshot(PANE, "sid-B", [self.binding(frame)])
        self.assertEqual(self.effective(owner), "sid-C")
        self.assertTrue(self.owns(owner, "sid-C"))
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            recipients = dict(self.handover.storage_recipients("sid-C"))
        self.assertIn("att-owner", recipients["sid-A"])

    def test_only_the_chain_end_answers_and_a_closed_route_settles(self):
        owner, _frame = self.chain()
        self.assertTrue(self.owns(owner, "sid-C"))
        self.assertFalse(self.owns(owner, "sid-A"))                    # the registered parent handed it on
        self.assertFalse(self.owns(owner, "sid-B"))                    # B handed it on
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            owed = {aid for _key, attempts in self.handover.storage_recipients("sid-B") if attempts
                    for aid in attempts}
            before = {b["attempt"] for b in self.handover.current_bindings("claude", "sid-C", self.jobs)}
            Path(owner["route_file"]).with_suffix(".outcome.json").write_text("{}")
            after = {b["attempt"] for b in self.handover.current_bindings("claude", "sid-C", self.jobs)}
        self.assertNotIn("att-owner", owed)
        self.assertEqual(before, {"att-owner", "att-frame"})
        self.assertEqual(after, {"att-frame"})                         # the settled owner drops, the open frame stays

    def test_a_later_retire_wins_over_the_same_seat_chain(self):
        owner = self.owner_row("att-owner", status="running")
        self.snapshot(now=st.now_epoch() - 9)
        self.start("sid-B")
        self.assertIsNotNone(self.retire("sid-B", "claude", "sid-E", "codex"))
        self.assertEqual(self.effective(owner), "sid-E")
        self.append_row(PANE, {"ts": st.now_epoch() + 5, "from": "sid-B", "sid": "sid-C", "harness": "claude",
                               "source": "clear", "bindings": []})
        self.assertEqual(self.effective(owner), "sid-E")
        self.assertFalse(self.owns(owner, "sid-C"))

    def test_a_retire_row_that_does_not_bind_the_job_leaves_it_with_its_predecessor(self):
        owner = self.owner_row("att-owner", status="running")
        frame = self.frame_row()
        self.snapshot(now=st.now_epoch() - 9)
        self.start("sid-B")
        self.append_row(PANE_BESIDE, {"ts": st.now_epoch() + 5, "from": "sid-B", "sid": "sid-E",
                                      "harness": "codex", "source": "retire",
                                      "bindings": [self.binding(frame)]})
        self.write_snapshot(PANE_BESIDE, "sid-E", [self.binding(frame)])     # what a real retire leaves at the seat
        self.assertEqual(self.effective(frame), "sid-E")
        self.assertEqual(self.effective(owner), "sid-B")

    def killed_or_cancelled_owner_is_handed_over(self, status):
        import route_authority
        owner = self.owner_row("att-owner", status=status, route=f"rt-{status}")
        self.assertEqual(self.snapshot(now=st.now_epoch() - 5), 1)
        self.start("sid-B")
        self.assertEqual(self.effective(owner), "sid-B")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            route_authority.require_replacement_parent(self.jobs, {}, owner, current_session=lambda: "sid-B")
        Path(owner["route_file"]).with_suffix(".outcome.json").write_text("{}")
        self.assertEqual(self.snapshot(now=st.now_epoch() - 4), 0)     # a closed route settles

    def test_a_killed_owner_with_an_open_route_is_handed_over(self):
        self.killed_or_cancelled_owner_is_handed_over("killed")

    def test_a_cancelled_owner_with_an_open_route_is_handed_over(self):
        self.killed_or_cancelled_owner_is_handed_over("cancelled")

    def test_lineage_reads_each_seat_ledger_once_per_read_context(self):
        from peer_read_context import PeerReadContext
        metas = [self.owner_row(f"att-owner-{n}", status="running", route=f"rt-{n}", digest=f"sha256:{n}{n}")
                 for n in range(3)]
        self.write_snapshot(PANE, "sid-A", [self.binding(metas[0])])
        self.write_snapshot(PANE_BESIDE, "sid-A", [self.binding(metas[1])])
        reads = mock.patch.object(st, "_read_ledger_lines", wraps=st._read_ledger_lines)
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}), reads as counted:
            context = PeerReadContext()
            for meta in metas:
                self.handover.effective_parent(meta, self.jobs, read_context=context)
            self.assertEqual(counted.call_count, 2)                    # one read per seat, not per attempt
            counted.reset_mock()
            self.handover.current_bindings("claude", "sid-A", self.jobs)
            self.assertLessEqual(counted.call_count, 2)                # a context of its own reads each once

    def test_ownership_checks_never_resolve_a_project_key(self):
        owner, _frame = self.chain(owner_status="running")
        keys = mock.patch.object(st, "project_key_for", wraps=st.project_key_for)
        with self.iso.patched_environ({"CLAUDE_CODE_SESSION_ID": "sid-C"}), keys as computed:
            self.assertTrue(self.handover.owns(owner, "sid-C", self.jobs))
            self.assertFalse(self.handover.owns(owner, "sid-B", self.jobs))
            self.assertFalse(self.handover.owns(owner, "sid-X", self.jobs))
            self.handover.storage_recipients("sid-C")
        computed.assert_not_called()                                   # no git calls, no project-keys.json

    def test_ownership_resolves_only_the_callers_own_seat_once(self):
        owner, frame = self.chain(owner_status="running")
        seats = mock.patch.object(self.handover, "pane_seat", wraps=self.handover.pane_seat)
        env = {"HERDR_PANE_ID": PANE, "CLAUDE_CODE_SESSION_ID": "sid-C"}
        with mock.patch.dict(self.handover._OWN_SEATS, clear=True), \
                self.iso.patched_environ(env), seats as resolved:
            for _ in range(5):
                for meta in (owner, frame):
                    for sid in ("sid-B", "sid-C", "sid-X"):
                        self.handover.owns(meta, sid, self.jobs)
            self.assertTrue(self.handover.owns(owner, "sid-C", self.jobs))
        self.assertEqual([call.args[2] for call in resolved.call_args_list], ["sid-C"])

    def test_a_broken_state_answers_the_registered_parent(self):
        meta = self.row("att-1")
        with mock.patch.object(self.handover, "_all_snapshots", side_effect=OSError("boom")):
            self.assertEqual(self.handover.effective_parent(meta, self.jobs), "sid-A")

    def test_markerless_role_only_clear_handoff_reaches_c_through_all_three_harnesses(self):
        pm = self.handover._peer_message()
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                pane = f"test:{harness}-continuity"
                a, b, c, d = (f"{harness}-{suffix}" for suffix in ("A", "B", "C", "D"))
                target = {"harness": "codex", "session_id": f"target-{harness}", "name": "target"}
                event = "prompt" if harness == "opencode" else "start"
                source = "" if harness == "opencode" else "clear"
                with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
                    self.assertTrue(pm.mark_steward(harness, a, target, "watch", "1", source="watch"))
                    self.assertEqual(self.snapshot(a, harness, pane=pane), 0)
                    seat = self.seat(pane)
                    a_snapshot = self.handover.read_snapshot(seat.key)
                    self.assertEqual(a_snapshot["bindings"], [])
                    self.assertEqual(a_snapshot["steward"]["roles"][0]["origin"],
                                     {"harness": harness, "sid": a})

                _out, rows = self.start(b, harness, event=event, source=source, pane=pane)
                self.assertEqual([(row["from"], row["sid"]) for row in rows], [(a, b)])
                self.assertEqual(rows[0]["bindings"], [])
                self.assertEqual(rows[0]["steward"]["roles"][0]["targets"], [
                    {**target, "kind": "watch", "ts": "1", "source": "watch"}])
                with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
                    self.assertFalse(pm.steward_marker_path(harness, b).exists())
                with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
                    active = self.handover.resolved_steward_relations(
                        harness, b, seat_key=seat.key)
                self.assertEqual(len(active), 1)
                generation = active[0]["generation"]

                self.assertEqual(self.snapshot(b, harness, pane=pane), 0)
                with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
                    b_snapshot = self.handover.read_snapshot(seat.key)
                _out, rows = self.start(c, harness, event=event, source=source, pane=pane)
                self.assertEqual([(row["from"], row["sid"]) for row in rows], [(a, b), (b, c)])
                self.assertEqual(rows[-1]["steward"]["roles"][0]["generation"], generation)
                self.assertEqual(rows[-1]["steward"]["roles"][0]["lineage"], [
                    {"from": a, "to": b, "snapshot_id": a_snapshot["snapshot_id"]},
                    {"from": b, "to": c,
                     "snapshot_id": b_snapshot["snapshot_id"]},
                ])
                with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
                    self.assertFalse(pm.steward_marker_path(harness, c).exists())

                # Replaying A's saved snapshot after B→C cannot branch the lineage.
                with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
                    with st.seat_lock(seat.key):
                        st.atomic_write_json(self.handover._snapshot_path(seat.key), a_snapshot)
                _out, rows = self.start(d, harness, event=event, source=source, pane=pane)
                self.assertEqual([(row["from"], row["sid"]) for row in rows], [(a, b), (b, c)])
                with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
                    self.assertEqual(self.handover.resolved_steward_relations(
                        harness, d, seat_key=seat.key), [])

    def test_release_before_confirmed_adoption_blocks_role_revival(self):
        pm = self.handover._peer_message()
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            self.assertTrue(pm.mark_steward("claude", "sid-A",
                {"harness": "codex", "session_id": "target", "name": "target"},
                "watch", "1", source="watch"))
            self.assertEqual(self.snapshot(), 0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pm.cmd_release(SimpleNamespace(harness="claude", session_id="sid-A")), 0)
        _out, rows = self.start("sid-B")
        self.assertEqual(rows, [])
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            self.assertEqual(self.handover.resolved_steward_relations(
                "claude", "sid-B", seat_key=self.seat().key), [])

    def test_role_only_handover_rejects_wrong_seat_and_harness_and_dedupes_callback(self):
        pm = self.handover._peer_message()
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            self.assertTrue(pm.mark_steward("claude", "sid-A",
                {"harness": "codex", "session_id": "target", "name": "target"},
                "watch", "1", source="watch"))
            self.assertEqual(self.snapshot(), 0)

        _out, wrong_seat_rows = self.start("sid-wrong-seat", pane=PANE_BESIDE)
        self.assertEqual(wrong_seat_rows, [])
        _out, wrong_harness_rows = self.start("codex-B", harness="codex", pane=PANE)
        self.assertEqual(wrong_harness_rows, [])

        _out, rows = self.start("sid-B", pane=PANE)
        self.assertEqual([(row["from"], row["sid"]) for row in rows], [("sid-A", "sid-B")])
        _out, duplicate = self.start("sid-B", pane=PANE)
        self.assertEqual([(row["from"], row["sid"]) for row in duplicate], [("sid-A", "sid-B")])
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            self.assertEqual(len(self.handover.resolved_steward_relations(
                "claude", "sid-B", seat_key=self.seat().key)), 1)

    def test_clear_and_compact_snapshot_callers_preserve_role_only_payload(self):
        pm = self.handover._peer_message()
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = self.seat()
            self.assertTrue(pm.mark_steward("claude", "sid-A",
                {"harness": "unknown", "name": "-"}, "explicit", "1", source="explicit"))
            with st.seat_lock(seat.key):
                st.record_event(seat, "claude", "sid-A", "start", cwd=str(self.cwd))
                st.write_card(seat, "claude", "sid-A", "work", cwd=str(self.cwd))
            card = st.read_latest_card(seat)
            request = {"schema": clear.SCHEMA, "seat": seat.as_dict(),
                       "harness": "claude", "sid": "sid-A",
                       "nonce": "fixture-nonce", "status": "reserved",
                       "prompt_seq": st.read_prompt_seq(seat),
                       "card_generation": card["generation"], "created": st.now_epoch(),
                       "compact": {"identity": "fixture-compact"}}
            clear._write_reservation(request)
            self.assertTrue(clear._prepare_handover_snapshot(request))
            prepared = self.handover.read_snapshot(seat.key)
            self.assertEqual(prepared["bindings"], [])
            self.assertEqual(prepared["steward"]["roles"][0]["origin"]["sid"], "sid-A")

            with st.seat_lock(seat.key):
                st.atomic_write_json(self.handover._snapshot_path(seat.key), {})
            with mock.patch.object(st, "update_compact_card_locked", return_value=True):
                clear._refresh_compact_card(request, {"recent": "summary", "source": "fixture"})
            refreshed = self.handover.read_snapshot(seat.key)
            self.assertEqual(refreshed["bindings"], [])
            self.assertEqual(refreshed["steward"]["roles"][0]["generation"],
                             prepared["steward"]["roles"][0]["generation"])

    def test_watch_snapshot_keeps_exact_unresolved_watch_identity_across_handover(self):
        pm = self.handover._peer_message()
        watch_id = "watch-fixture-001"
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            self.assertTrue(pm.mark_steward("claude", "sid-A",
                {"harness": "codex", "session_id": "target", "name": "target"},
                "watch", "1", source="watch"))
            seat = self.seat()
            watch_root = pm.peer_state_root() / "peer-watches"
            watch_root.mkdir(parents=True, exist_ok=True)
            arm = {"schema_version": 1, "watch_id": watch_id, "server": "server-fixture",
                   "target": "target-name", "until": ["done"], "deadline": {"wall": 9999999999},
                   "armed_ts": "fixture-time", "steward": {"harness": "claude", "session_id": "sid-A"},
                   "steward_seat": seat.as_dict(),
                   "agent": {"harness": "codex", "session_id": "target", "name": "target",
                             "pane": "w1:p-target"}}
            (watch_root / f"{watch_id}.json").write_text(json.dumps(arm), encoding="utf-8")
            watches = self.handover.resolved_watch_references(
                "claude", "sid-A", seat_key=seat.key)
            self.assertEqual([row["watch_id"] for row in watches], [watch_id])
            self.assertEqual(self.snapshot(), 0)
        _out, rows = self.start("sid-B")
        self.assertEqual(rows[0].get("steward", {}).get("watches", [{}])[0].get("watch_id"),
                         watch_id, repr(rows))
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            ref = self.handover.resolve_watch(watch_id, seat_key=seat.key)
        self.assertIsNotNone(ref, repr(self.handover._transition_rows(seat.key)))
        self.assertEqual(ref["holder"], {"harness": "claude", "sid": "sid-B"})
        self.assertEqual((ref["watch_id"], ref["server"], ref["target"], ref["until"]),
                         (watch_id, "server-fixture", "target-name", ["done"]))
        self.assertEqual(ref["deadline"], {"wall": 9999999999})
        (watch_root / f"{watch_id}.ack.json").write_text(
            json.dumps({"session_id": "sid-B", "harness": "claude"}), encoding="utf-8")
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            self.assertIsNone(self.handover.resolve_watch(watch_id, seat_key=seat.key))

    def test_stale_predecessor_release_revokes_only_its_generation(self):
        pm = self.handover._peer_message()
        pane = "test:stale-release"
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            self.assertTrue(pm.mark_steward("claude", "sid-A",
                {"harness": "codex", "session_id": "target-A", "name": "target-A"},
                "watch", "1", source="watch"))
            self.assertEqual(self.snapshot("sid-A", pane=pane), 0)
            seat = self.seat(pane)
        self.start("sid-B", pane=pane)
        with self.iso.patched_environ({"HERDR_PANE_ID": pane, "CLAUDE_CODE_SESSION_ID": "sid-B"}):
            self.assertTrue(pm.mark_steward("claude", "sid-B",
                {"harness": "codex", "session_id": "target-B", "name": "target-B"},
                "watch", "2", source="watch"))
            current_b = pm.read_steward_markers([pm.peer_state_root()])[("claude", "sid-B")]
            generation_b = current_b["generation"]

        # A late off callback carries A's original session identity. It may
        # revoke A's inherited generation, but cannot unlink B's new marker.
        with self.iso.patched_environ({"HERDR_PANE_ID": pane, "CLAUDE_CODE_SESSION_ID": "sid-A"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.cmd_release(SimpleNamespace(harness="claude", session_id="sid-A")), 0)
            active = self.handover.resolved_steward_relations(
                "claude", "sid-B", seat_key=seat.key)
        self.assertEqual([row["generation"] for row in active], [generation_b])
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            self.assertTrue(pm.steward_marker_path("claude", "sid-B").is_file())
        self.assertEqual(active[0]["targets"][0]["session_id"], "target-B")

    def test_current_holder_release_at_both_hops_prevents_role_resurrection(self):
        pm = self.handover._peer_message()
        for release_sid in ("sid-B", "sid-C"):
            with self.subTest(release_sid=release_sid):
                pane = f"test:release-{release_sid}"
                with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
                    self.assertTrue(pm.mark_steward("claude", "sid-A",
                        {"harness": "codex", "session_id": "target", "name": "target"},
                        "watch", "1", source="watch"))
                    self.assertEqual(self.snapshot("sid-A", pane=pane), 0)
                    seat = self.seat(pane)
                self.start("sid-B", pane=pane)
                if release_sid == "sid-C":
                    self.assertEqual(self.snapshot("sid-B", pane=pane), 0)
                    self.start("sid-C", pane=pane)
                with self.iso.patched_environ({"HERDR_PANE_ID": pane,
                                               "CLAUDE_CODE_SESSION_ID": release_sid}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(pm.cmd_release(SimpleNamespace(
                        harness="claude", session_id=release_sid)), 0)
                if release_sid == "sid-B":
                    self.assertEqual(self.snapshot("sid-B", pane=pane), 0)
                    self.start("sid-C", pane=pane)
                holder = "sid-B" if release_sid == "sid-B" else "sid-C"
                with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
                    self.assertEqual(self.handover.resolved_steward_relations(
                        "claude", holder, seat_key=seat.key), [])

    def test_interrupted_durable_adoption_keeps_a_as_holder_and_retries_same_hop(self):
        pm = self.handover._peer_message()
        pane = "test:interrupted-adoption"
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            self.assertTrue(pm.mark_steward("claude", "sid-A",
                {"harness": "codex", "session_id": "target", "name": "target"},
                "watch", "1", source="watch"))
            self.assertEqual(self.snapshot("sid-A", pane=pane), 0)
            seat = self.seat(pane)
            with mock.patch.object(self.handover, "_append_transition_row",
                                   side_effect=OSError("fixture-fsync-interrupted")):
                _out, rows = self.start("sid-B", pane=pane)
            self.assertEqual(rows, [])
            self.assertEqual(self.handover.resolved_steward_relations(
                "claude", "sid-A", seat_key=seat.key)[0]["holder"]["sid"], "sid-A")

        _out, rows = self.start("sid-B", pane=pane)
        self.assertEqual([(row["from"], row["sid"]) for row in rows], [("sid-A", "sid-B")])
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            self.assertEqual(self.handover.resolved_steward_relations(
                "claude", "sid-B", seat_key=seat.key)[0]["holder"]["sid"], "sid-B")

    def _prepare_transition_failure(self, pane, watch_target="target"):
        pm = self.handover._peer_message()
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            self.assertTrue(pm.mark_steward("claude", "sid-A",
                {"harness": "codex", "session_id": watch_target, "name": watch_target},
                "watch", "fixture-time", source="watch"))
            self.assertEqual(self.snapshot("sid-A", pane=pane), 0)
            seat = self.seat(pane)
            return pm, seat, st._ledger_path(seat)

    def test_partial_transition_write_keeps_complete_ledger_and_retries_one_successor(self):
        pane = "test:partial-transition-write"
        _pm, seat, ledger = self._prepare_transition_failure(pane)
        before = ledger.read_bytes()
        real_write = os.write
        partial_calls = []

        def partial_then_fail(fd, data):
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                target = ""
            if f".{seat.key}.jsonl." not in target or not target.endswith(".tmp"):
                return real_write(fd, data)
            partial_calls.append(len(data))
            if len(partial_calls) == 1:
                return real_write(fd, bytes(data[:max(1, len(data) // 2)]))
            raise OSError("fixture failure after positive partial transition write")

        with mock.patch.object(self.handover.os, "write", side_effect=partial_then_fail):
            _output, rows = self.start("sid-B", pane=pane)
        self.assertGreaterEqual(len(partial_calls), 2)
        self.assertEqual(rows, [])
        self.assertEqual(self.handover.handover_rows(seat), [])
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            holder = self.handover.resolved_steward_relations(
                "claude", "sid-A", seat_key=seat.key)[0]["holder"]["sid"]
        self.assertEqual(holder, "sid-A")
        self.assertEqual(ledger.read_bytes().splitlines()[0], before.splitlines()[0])
        self.assertFalse(list(ledger.parent.glob(f".{seat.key}.jsonl.*.tmp")))

        _output, rows = self.start("sid-B", pane=pane)
        self.assertEqual([(row["from"], row["sid"]) for row in rows], [("sid-A", "sid-B")])
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            self.assertEqual(self.handover.resolved_steward_relations(
                "claude", "sid-B", seat_key=seat.key)[0]["holder"]["sid"], "sid-B")
        _output, duplicate_rows = self.start("sid-B", pane=pane)
        self.assertEqual([(row["from"], row["sid"]) for row in duplicate_rows],
                         [("sid-A", "sid-B")])

    def test_transition_fsync_failure_keeps_predecessor_until_retry(self):
        pane = "test:transition-fsync-failure"
        _pm, seat, ledger = self._prepare_transition_failure(pane)
        before_lines = ledger.read_bytes().splitlines()
        real_fsync = os.fsync
        synced = []

        def fail_after_full_staged_write(fd):
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                target = ""
            if f".{seat.key}.jsonl." in target and target.endswith(".tmp"):
                synced.append(Path(target).stat().st_size)
                raise OSError("fixture fsync failure after complete transition write")
            return real_fsync(fd)

        with mock.patch.object(self.handover.os, "fsync", side_effect=fail_after_full_staged_write):
            _output, rows = self.start("sid-B", pane=pane)
        self.assertEqual(len(synced), 1)
        self.assertGreater(synced[0], 0)
        self.assertEqual(rows, [])
        self.assertEqual(ledger.read_bytes().splitlines()[:len(before_lines)], before_lines)
        with self.iso.patched_environ({"HERDR_PANE_ID": pane}):
            self.assertEqual(self.handover.resolved_steward_relations(
                "claude", "sid-A", seat_key=seat.key)[0]["holder"]["sid"], "sid-A")

        _output, rows = self.start("sid-B", pane=pane)
        self.assertEqual([(row["from"], row["sid"]) for row in rows], [("sid-A", "sid-B")])
        _output, rows = self.start("sid-B", pane=pane)
        self.assertEqual(len(rows), 1)

    def _ack_fixture(self, watch_id, holder="sid-B"):
        root = self.state / f"watch-state-{watch_id}"
        watch_root = root / "peer-watches"
        watch_root.mkdir(parents=True, exist_ok=True)
        seat = st.Seat("pane", "ack-seat-" + watch_id)
        row = {"watch_id": watch_id,
               "holder": {"harness": "claude", "sid": holder},
               "origin": {"harness": "claude", "sid": "sid-A"},
               "seat_key": seat.key, "storage_root": str(root),
               "server": "fixture-server", "target": "fixture-target",
               "agent": {"harness": "codex", "session_id": "target"},
               "until": ["done"], "armed_ts": "fixture-armed",
               "deadline": {"wall": 9999999999}}
        return root, seat, row, watch_root / f"{watch_id}.ack.json"

    def test_ack_partial_and_fsync_failures_leave_no_canonical_record_and_retry(self):
        for failure in ("partial-write", "fsync"):
            with self.subTest(failure=failure):
                watch_id = "ack-fault-" + failure
                _root, _seat, row, ack = self._ack_fixture(watch_id)
                with self.iso.patched_environ(), \
                     mock.patch.object(self.handover, "resolve_watch",
                                       side_effect=lambda candidate, **_kwargs:
                                       dict(row) if candidate == watch_id else None):
                    if failure == "partial-write":
                        real_write = os.write
                        calls = []

                        def partial_then_fail(fd, data):
                            calls.append(len(data))
                            if len(calls) == 1:
                                return real_write(fd, bytes(data[:max(1, len(data) // 2)]))
                            raise OSError("fixture partial ACK write")

                        with mock.patch.object(self.handover.os, "write", side_effect=partial_then_fail):
                            self.assertEqual(self.handover.acknowledge_watch(
                                watch_id, "claude", "sid-B", "fixture-carrier"), "unknown")
                        self.assertGreaterEqual(len(calls), 2)
                    else:
                        real_write = os.write
                        bytes_written = []

                        def count_full_write(fd, data):
                            count = real_write(fd, data)
                            bytes_written.append(count)
                            return count

                        with mock.patch.object(self.handover.os, "write", side_effect=count_full_write), \
                             mock.patch.object(self.handover.os, "fsync",
                                               side_effect=OSError("fixture ACK fsync failure")):
                            self.assertEqual(self.handover.acknowledge_watch(
                                watch_id, "claude", "sid-B", "fixture-carrier"), "unknown")
                        self.assertGreater(sum(bytes_written), 0)
                    self.assertFalse(ack.exists())
                    self.assertFalse(list(ack.parent.glob(f".{ack.name}.*.tmp")))
                    self.assertEqual(self.handover.acknowledge_watch(
                        watch_id, "claude", "sid-B", "retry-carrier"), "created")
                    self.assertTrue(self.handover.watch_ack_valid(
                        ack, watch_id, "claude", "sid-B"))
                    saved = json.loads(ack.read_text(encoding="utf-8"))
                    self.assertEqual((saved["watch_id"], saved["session_id"], saved["harness"]),
                                     (watch_id, "sid-B", "claude"))

    def test_ack_repairs_torn_inode_once_and_rejects_concurrent_stale_recipient(self):
        import concurrent.futures

        watch_id = "ack-concurrent-current-stale"
        _root, _seat, row, ack = self._ack_fixture(watch_id)
        ack.write_bytes(b'{"watch_id":')
        with self.iso.patched_environ(), \
             mock.patch.object(self.handover, "resolve_watch",
                               side_effect=lambda candidate, **_kwargs:
                               dict(row) if candidate == watch_id else None):
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(self.handover.acknowledge_watch,
                                       watch_id, "claude", sid, "fixture-carrier")
                           for sid in ("sid-B", "sid-B", "sid-A")]
                results = [future.result(timeout=10) for future in futures]
            self.assertCountEqual(results, ["created", "already", "not-recipient"])
            inode = ack.stat().st_ino
            self.assertEqual(self.handover.acknowledge_watch(
                watch_id, "claude", "sid-A", "stale-carrier"), "not-recipient")
            self.assertEqual(ack.stat().st_ino, inode)
            self.assertTrue(self.handover.watch_ack_valid(ack, watch_id, "claude", "sid-B"))
            self.assertFalse(self.handover.watch_ack_valid(ack, watch_id, "claude", "sid-A"))
            self.assertEqual(json.loads(ack.read_text(encoding="utf-8"))["session_id"], "sid-B")

        stale_watch = "ack-existing-stale-predecessor"
        _root, _seat, stale_row, stale_ack = self._ack_fixture(stale_watch)
        stale_ack.write_text(json.dumps({"watch_id": stale_watch,
                                         "harness": "claude", "session_id": "sid-A"}),
                             encoding="utf-8")
        before = stale_ack.read_bytes()
        with self.iso.patched_environ(), \
             mock.patch.object(self.handover, "resolve_watch",
                               side_effect=lambda candidate, **_kwargs:
                               dict(stale_row) if candidate == stale_watch else None):
            self.assertEqual(self.handover.acknowledge_watch(
                stale_watch, "claude", "sid-B", "current-carrier"), "unknown")
            self.assertEqual(self.handover.acknowledge_watch(
                stale_watch, "claude", "sid-A", "stale-carrier"), "not-recipient")
        self.assertEqual(stale_ack.read_bytes(), before)

    def test_release_tombstone_survives_ledger_fold_without_advancing_session_activity(self):
        with self.iso.patched_environ({"HERDR_PANE_ID": PANE}):
            seat = self.seat()
            with st.seat_lock(seat.key):
                st.record_event(seat, "claude", "sid-A", "start", cwd=str(self.cwd), now=100)
                self.assertTrue(self.handover.record_steward_release(
                    seat, "claude", "sid-A", "generation-A", now=200))
                for index in range(st.LEDGER_FOLD_LINES + 5):
                    st.record_event(seat, "claude", f"sid-fold-{index}", "start", cwd=str(self.cwd))
            rows = st._read_ledger_lines(seat)
            self.assertTrue(any(row.get("event") == "steward-release"
                                and row.get("generation") == "generation-A" for row in rows))
            self.assertEqual(st.session_summary(seat)[("claude", "sid-A")]["last_seen"], 100)

    def test_release_fsync_failure_keeps_marker_until_tombstone_retry(self):
        pm = self.handover._peer_message()
        pane = "test:release-fsync-failure"
        with self.iso.patched_environ({"HERDR_PANE_ID": pane,
                                       "CLAUDE_CODE_SESSION_ID": "sid-A"}):
            self.assertTrue(pm.mark_steward("claude", "sid-A",
                {"harness": "codex", "session_id": "target", "name": "target"},
                "watch", "fixture-time", source="watch"))
            seat = self.seat(pane)
            with st.seat_lock(seat.key):
                st.record_event(seat, "claude", "sid-A", "start", cwd=str(self.cwd), now=100)
            marker = pm.steward_marker_path("claude", "sid-A")
            real_fsync = os.fsync

            def fail_release_publish(fd):
                try:
                    target = os.readlink(f"/proc/self/fd/{fd}")
                except OSError:
                    target = ""
                if f".{seat.key}.jsonl." in target and target.endswith(".tmp"):
                    raise OSError("fixture release fsync failure")
                return real_fsync(fd)

            with mock.patch.object(self.handover.os, "fsync", side_effect=fail_release_publish), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(pm.cmd_release(SimpleNamespace(
                    harness="claude", session_id="sid-A")), 1)
            self.assertTrue(marker.is_file())
            self.assertEqual(self.handover.resolved_steward_relations(
                "claude", "sid-A", seat_key=seat.key)[0]["holder"]["sid"], "sid-A")

            self.assertEqual(pm.cmd_release(SimpleNamespace(
                harness="claude", session_id="sid-A")), 0)
            self.assertFalse(marker.exists())
            self.assertTrue(any(row.get("event") == "steward-release"
                                for _key, row in self.handover._transition_rows(seat.key)))
            self.assertEqual(st.session_summary(seat)[("claude", "sid-A")]["last_seen"], 100)


if __name__ == "__main__":
    unittest.main()
