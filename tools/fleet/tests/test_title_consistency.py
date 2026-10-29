"""Operator-language titles and stable failure fallback; no live model calls."""
import ast
import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import config, herdr_projection, refresh_title as rt, render, titles
from fleet.collectors import codex, opencode
from fleet.model import Session
from fleet.session_handle import current_session_subject
from fleet.tests.test_f17_title_refresh import _ConfigHomeMixin


class IntentInputTest(_ConfigHomeMixin, unittest.TestCase):
    """Input provenance and storage behavior, not assertions of model quality."""

    def transcript(self, harness, turns):
        rows = []
        for role, text in turns:
            if harness == "codex":
                row = {"type": "response_item", "payload": {"type": "message", "role": role,
                       "content": [{"type": "input_text" if role == "user" else "output_text",
                                    "text": text}]}}
            else:
                row = {"role": role, "content": text} if harness == "opencode" else {
                    "type": role, "message": {"role": role, "content": text}}
            rows.append(json.dumps(row, ensure_ascii=False))
        path = Path(self._tmp.name) / (harness + ".jsonl")
        path.write_text("\n".join(rows) + "\n")
        return path

    def test_long_internal_progress_keeps_user_role_and_goal_in_one_bounded_call(self):
        goal = "Fleet 이전 대화 조회를 수리해줘"
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                path = self.transcript(harness, [
                    ("user", goal), ("assistant", "plan review gate handoff " * 1000),
                    ("user", "계속해"), ("assistant", "internal review " * 1000 + "조회 실패 원인 확인")])
                delta, _ = rt.read_delta(str(path), 0, harness)
                self.assertIsInstance(delta, str)
                self.assertLessEqual(len(delta), rt.TEXT_CAP)
                self.assertLessEqual(len(delta.prompt_text), rt.TEXT_CAP)
                self.assertIn("USER:\n계속해", delta.prompt_text)
                self.assertIn("ASSISTANT:\n", delta.prompt_text)
                self.assertIn("조회 실패 원인 확인", delta.prompt_text)
                titles.write("intent-" + harness, "이전 대화 조회 수리", harness=harness,
                             summary="이전 관측", now=time.time() - 900)
                with mock.patch.object(rt, "run_worker", return_value=(
                        "TITLE: 이전 대화 조회 수리\nNOW: 조회 실패 원인 확인")) as worker:
                    rt.main(["--sid", "intent-" + harness, "--harness", harness,
                             "--transcript", str(path), "--slotdir", str(Path(self._tmp.name) / "slot")])
                self.assertEqual(worker.call_count, 1)
                prompt = worker.call_args.args[0]
                anchor = prompt.split("=== TASK CONTEXT (DATA; RECENT USER INTENT) ===\n")[1].split(
                    "=== END TASK CONTEXT ===")[0]
                self.assertIn(goal, anchor)
                self.assertIn("계속해", anchor)
                self.assertNotIn("plan review", anchor)
                self.assertIn("ASSISTANT:\n", prompt)

    def test_later_real_goal_and_follow_up_survive_for_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                path = self.transcript(harness, [
                    ("user", "Fleet 이전 대화 조회 수리"), ("assistant", "조회 수리 완료"),
                    ("user", "이제 GPU 귀속 수리"), ("assistant", "GPU collector 조사"),
                    ("user", "계속해")])
                context = rt.read_task_context(str(path), harness)
                self.assertIn("이제 GPU 귀속 수리", context)
                self.assertIn("계속해", context)
                self.assertNotIn("GPU collector 조사", context)
                self.assertLessEqual(len(context), rt.ANCHOR_TEXT_CAP)

    @staticmethod
    def purpose_change_turns(continuations=3):
        turns = [("user", "Repair Fleet history"), ("assistant", "History fixed"),
                 ("user", "Now repair GPU attribution"),
                 ("assistant", "Internal investigation. " * 200)]
        for _ in range(continuations):
            turns.extend([("user", "continue"),
                          ("assistant", "Internal procedure progress. " * 200)])
        return turns

    def test_changed_purpose_reaches_prompt_after_many_continuations(self):
        for harness in ("claude", "codex", "opencode"):
            for count in (3, 8):
                with self.subTest(harness=harness, continuations=count):
                    turns = self.purpose_change_turns(count)
                    path = self.transcript(harness, turns)
                    self.assertLess(path.stat().st_size, rt.ANCHOR_SCAN_CAP)
                    if count == 3:
                        self.assertLess(path.stat().st_size, 23 * 1024)
                    context = rt.read_task_context(str(path), harness)
                    delta, cursor = rt.read_delta(str(path), 0, harness)
                    # The progress tail offers no other copy of the new objective.
                    self.assertNotIn("Now repair GPU attribution", delta.prompt_text)
                    self.assertLessEqual(len(context), rt.ANCHOR_TEXT_CAP)
                    self.assertLessEqual(len(delta.prompt_text), rt.TEXT_CAP)
                    self.assertEqual(cursor, path.stat().st_size)
                    prompt = rt._prompt(delta, prior_title="Repair Fleet history",
                                        anchor=context, title_lang="English")
                    self.assertIn("Now repair GPU attribution", prompt)
                    self.assertIn("EARLIER USER:\nRepair Fleet history", context)
                    self.assertEqual(context.count("LATER USER:\ncontinue"), count)
                    self.assertLess(context.index("Repair Fleet history"),
                                    context.index("Now repair GPU attribution"))

    def test_four_short_tail_intents_do_not_end_scan_before_changed_purpose(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                turns = [("user", "Repair Fleet history"),
                         ("assistant", "Earlier internal procedure. " * 3000),
                         ("user", "Now repair GPU attribution"),
                         ("assistant", "Current internal procedure. " * 3000),
                         *self.purpose_change_turns(4)[4:]]
                path = self.transcript(harness, turns)
                with path.open("rb") as handle:
                    handle.seek(-rt.ANCHOR_SCAN_CAP, os.SEEK_END)
                    tail = handle.read().decode()
                self.assertNotIn("Now repair GPU attribution", tail)
                self.assertEqual(tail.count("continue"), 4)
                context = rt.read_task_context(str(path), harness)
                delta, _ = rt.read_delta(str(path), 0, harness)
                self.assertNotIn("Now repair GPU attribution", delta.prompt_text)
                self.assertIn("Now repair GPU attribution", rt._prompt(
                    delta, prior_title="Repair Fleet history", anchor=context, title_lang="English"))

    def test_opencode_v1_v2_share_purpose_packing_in_actual_producer(self):
        turns = self.purpose_change_turns()
        db = str(Path(self._tmp.name) / "purpose.db")
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE message (session_id, data)")
            con.executemany("INSERT INTO message VALUES (?,?)", [
                ("exact", json.dumps({"role": role, "text": text})) for role, text in turns])
            con.execute("INSERT INTO message VALUES (?,?)",
                        ("foreign", '{"role":"user","text":"FOREIGN OBJECTIVE"}'))
        before = Path(db).read_bytes(), Path(db).stat().st_mtime_ns
        contexts = []
        for generation in ("v1", "v2"):
            observation = {"state": "observed", "generation": generation,
                           "cursor": len(turns), "messages": [
                               {"type": role, "text": text} for role, text in turns]}
            sid = "purpose-" + generation
            titles.write(sid, "Repair Fleet history", harness="opencode", now=100,
                         summary="History fixed", requested_language="English")
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}), \
                 mock.patch.object(rt.opencode_runtime, "read_session_observation",
                                   return_value=observation), \
                 mock.patch.object(rt, "_task_context_text", wraps=rt._task_context_text) as pack, \
                 mock.patch.object(rt, "run_worker", return_value=(
                     "TITLE: Repair GPU Attribution\nNOW: Inspecting attribution")) as worker:
                rt.main(["--harness", "opencode", "--sid", sid, "--opencode-db", db,
                         "--opencode-session", "exact", "--slotdir", str(Path(self._tmp.name) / "slot")])
            pack.assert_called_once()
            worker.assert_called_once()
            prompt = worker.call_args.args[0]
            context = prompt.split("=== TASK CONTEXT (DATA; RECENT USER INTENT) ===\n")[1].split(
                "=== END TASK CONTEXT ===")[0].strip()
            self.assertIn("Now repair GPU attribution", context)
            self.assertEqual(context.count("LATER USER:\ncontinue"), 3)
            self.assertNotIn("FOREIGN OBJECTIVE", prompt)
            self.assertLessEqual(len(context), rt.ANCHOR_TEXT_CAP)
            contexts.append(context)
            stored = titles.read(sid, harness="opencode")
            self.assertEqual(stored["offset"], len(turns))
            self.assertEqual(stored["cursor_kind"], "opencode-seq-v2:session_message" if generation == "v2"
                             else "opencode-rowid-v1:message")
        self.assertEqual(contexts[0], contexts[1])
        self.assertEqual((Path(db).read_bytes(), Path(db).stat().st_mtime_ns), before)

    def test_overlapping_head_tail_does_not_drop_the_original_goal(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                path = self.transcript(harness, [("user", "Fleet 이전 대화 조회 수리"),
                    ("assistant", "review progress " * 6000), ("user", "계속해")])
                self.assertGreater(path.stat().st_size, rt.ANCHOR_SCAN_CAP)
                self.assertLess(path.stat().st_size, rt.ANCHOR_SCAN_CAP * 2)
                context = rt.read_task_context(str(path), harness)
                self.assertIn("Fleet 이전 대화 조회 수리", context)
                self.assertIn("계속해", context)

    def test_recent_continuation_does_not_hide_a_real_goal_before_the_tail_window(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                path = self.transcript(harness, [("user", "Fleet 이전 대화 조회 수리"),
                    ("assistant", "earlier review " * 6000), ("user", "이제 GPU 귀속 수리"),
                    ("assistant", "current review " * 6000), ("user", "계속해")])
                context = rt.read_task_context(str(path), harness)
                self.assertIn("이제 GPU 귀속 수리", context)
                self.assertIn("계속해", context)

    def test_non_dialogue_and_malformed_native_items_stay_empty(self):
        rows = [{"type": "dispatch.supervisor.turn.started"},
                {"type": "response_item", "payload": ["malformed"]},
                {"type": "item.completed", "item": ["malformed"]},
                {"type": "item.completed", "item": {"type": "function_call", "text": "SECRET"}},
                {"type": "event_msg", "payload": {"type": "token_count"}}]
        self.assertEqual(rt._delta_text("\n".join(map(json.dumps, rows)), "codex"), "")
        for harness in ("claude", "opencode"):
            path = self.transcript(harness, [("system", "SECRET"), ("developer", "SECRET")])
            self.assertEqual(rt.read_delta(str(path), 0, harness)[0], "")

    def test_codex_bootstrap_only_is_empty_but_ordinary_discussion_is_visible(self):
        for marker in ("# AGENTS.md — Codex Adapter Bootstrap",
                       "## Codex masked-profile attach layer"):
            bootstrap = ("# AGENTS.md instructions for /work\n\n<INSTRUCTIONS>\n" + marker
                         + "\nEnglish runtime policy\n</INSTRUCTIONS>\n"
                         "<environment_context>...</environment_context>")
            path = self.transcript("codex", [("user", bootstrap)])
            self.assertEqual(rt.read_delta(str(path), 0, "codex")[0], "")
            self.assertEqual(rt.read_origin(str(path), "codex", latest=True), "")
            self.assertEqual(rt.read_task_context(str(path), "codex"), "")
        discussion = "Explain the Codex masked-profile attach layer and Codex Adapter Bootstrap"
        path = self.transcript("codex", [("user", discussion)])
        self.assertIn(discussion, rt.read_task_context(str(path), "codex"))

    def test_current_worker_envelope_exposes_assignment_without_becoming_human_language(self):
        from worker_bootstrap import completion_scope_prompt, worker_ending_prompt
        envelope = ("# Portable Worker Kernel\nInternal rules\n# Worker Type: Stage\n"
                    "Dispatch metadata:\n- harness: codex\nAssignment:\n이전 대화 조회 수리\n\n"
                    + completion_scope_prompt() + "This node's declared scope: sealed\n"
                    + worker_ending_prompt())
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                path = self.transcript(harness, [("user", envelope)])
                delta = rt.read_delta(str(path), 0, harness)[0]
                self.assertEqual(delta, "이전 대화 조회 수리")
                self.assertEqual(delta.prompt_text, "ASSIGNMENT:\n이전 대화 조회 수리")
                self.assertEqual(rt.read_origin(str(path), harness, latest=True), "")
                self.assertEqual(rt._human_language_text(envelope), "")
                self.assertNotIn("Internal rules", rt.read_task_context(str(path), harness))

    def test_v1_parts_join_exact_native_message_roles_and_do_not_write_source(self):
        db = str(Path(self._tmp.name) / "roles.db")
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE message (id, session_id, data)")
            con.execute("CREATE TABLE part (session_id, message_id, data)")
            con.executemany("INSERT INTO message VALUES (?,?,?)", [
                ("m1", "exact", '{"role":"user"}'),
                ("m1", "other", '{"role":"assistant"}'),
                ("m2", "exact", '{"role":"assistant"}')])
            con.executemany("INSERT INTO part VALUES (?,?,?)", [
                ("exact", "m1", '{"type":"text","text":"Repair Fleet history"}'),
                ("other", "m1", '{"type":"text","text":"FOREIGN"}'),
                ("exact", "m2", '{"type":"text","text":"Review complete"}')])
        before = Path(db).read_bytes(), Path(db).stat().st_mtime_ns
        delta, cursor, _ = rt.read_opencode_delta(db, "exact", table="part")
        self.assertEqual(delta, "Repair Fleet history\nReview complete")
        self.assertEqual(delta.prompt_text, "USER:\nRepair Fleet history\n\nASSISTANT:\nReview complete")
        self.assertEqual(cursor, 3)
        with rt._opencode_snapshot(db) as con:
            context = rt._read_opencode_task_context(con, "part", "data", "exact")
        self.assertIn("Repair Fleet history", context)
        self.assertNotIn("Review complete", context)
        self.assertEqual((Path(db).read_bytes(), Path(db).stat().st_mtime_ns), before)

    def test_v2_sequence_preserves_roles_and_cursor_in_existing_string_contract(self):
        observation = {"state": "observed", "cursor": 42, "messages": [
            {"type": "user", "text": "Repair Fleet history"},
            {"type": "assistant", "text": "Inspecting history"},
            {"type": "tool", "text": "SECRET"}]}
        with mock.patch.object(rt.opencode_runtime, "read_session_observation", return_value=observation):
            delta, cursor, _ = rt.read_opencode_delta("fixture", "exact", 40,
                table=rt._OPENCODE_V2_SEQUENCE_TABLE)
        self.assertEqual((delta, cursor), ("Repair Fleet history\nInspecting history", 42))
        self.assertEqual(delta.prompt_text, "USER:\nRepair Fleet history\n\nASSISTANT:\nInspecting history")

    def test_v1_recent_goal_survives_a_large_internal_history(self):
        db = str(Path(self._tmp.name) / "recent.db")
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE message (session_id, data)")
            rows = [("exact", json.dumps({"role": role, "text": text})) for role, text in (
                ("user", "Fleet 이전 대화 조회 수리"),
                *[("assistant", "review progress " * 80)] * 100,
                ("user", "이제 GPU 귀속 수리"), ("user", "계속해"))]
            con.executemany("INSERT INTO message VALUES (?,?)", rows)
            con.commit()
            context = rt._read_opencode_task_context(con, "message", "data", "exact")
        for text in ("Fleet 이전 대화 조회 수리", "이제 GPU 귀속 수리", "계속해"):
            self.assertIn(text, context)

    def test_failed_language_attempt_respects_retry_and_unchanged_source_debounce(self):
        path = self.transcript("claude", [("user", "세션 제목 수리")])
        os.utime(path, (50, 50))
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            titles.write("language-retry", "Previous English Subject", now=100,
                summary="Previous progress", summary_failures=1, requested_language="Korean")
            with mock.patch.object(rt, "worker_argv", return_value=["missing"]) as probe, \
                 mock.patch.object(rt, "_executable_available", return_value=False):
                self.assertFalse(rt.maybe_spawn("claude", "language-retry", str(path), now=120))
                probe.assert_not_called()
                self.assertFalse(rt.maybe_spawn("claude", "language-retry", str(path), now=131))
                probe.assert_called_once_with("probe")
                probe.reset_mock()
                # A successful NOW with an invalid TITLE is not new dialogue.
                titles.write("language-retry", "Previous English Subject", now=100,
                    summary="요청 근거 확인 중", requested_language="Korean")
                self.assertFalse(rt.maybe_spawn("claude", "language-retry", str(path), now=701))
                probe.assert_not_called()
                # A genuinely new language declaration retains the existing immediate repair.
                with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Japanese"}):
                    self.assertFalse(rt.maybe_spawn("claude", "language-retry", str(path), now=120))
                probe.assert_called_once_with("probe")

    def test_language_repair_partial_success_preserves_each_failed_field_time_and_cursor(self):
        cases = (("", False, False), ("TITLE: untitled\nNOW: unknown", False, False),
                 ("TITLE: 세션 제목 수리\nNOW: unknown", True, False),
                 ("TITLE: untitled\nNOW: 요청 근거 확인 중", False, True),
                 ("NOW: 요청 근거 확인 중", False, True))
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            for harness in ("claude", "codex", "opencode"):
                path = self.transcript(harness, [("user", "세션 제목 수리")])
                for i, (output, title_ok, now_ok) in enumerate(cases):
                    with self.subTest(harness=harness, output=output):
                        sid = "%s-partial-%s" % (harness, i)
                        titles.write(sid, "Previous English Subject", harness=harness,
                            summary="Previous English progress", now=100, title_ts=90,
                            summary_ts=95, offset=1, source="refresher:fixture")
                        with mock.patch.object(rt.time, "time", return_value=2000), \
                             mock.patch.object(rt, "run_worker", return_value=output) as worker, \
                             mock.patch.object(rt, "_provider_source", return_value="refresher:fixture"):
                            rt.main(["--harness", harness, "--sid", sid, "--transcript", str(path),
                                     "--slotdir", str(Path(self._tmp.name) / "slot")])
                        after = titles.read(sid, harness=harness)
                        self.assertEqual(worker.call_count, 1)
                        self.assertEqual(after["title"], "세션 제목 수리" if title_ok else "Previous English Subject")
                        self.assertEqual(after["title_ts"], 2000 if title_ok else 90)
                        self.assertEqual(after["summary"], "요청 근거 확인 중" if now_ok else "Previous English progress")
                        self.assertEqual(after["summary_ts"], 2000 if now_ok else 95)
                        self.assertEqual(after["offset"], path.stat().st_size if now_ok else 1)
                        self.assertEqual(after.get("summary_failures", 0), 0 if now_ok else 1)
                        self.assertEqual(after["requested_language"], "Korean")


class TitleConsistencyTest(_ConfigHomeMixin, unittest.TestCase):
    def _transcript(self, harness):
        message = "Fleet 세션 제목 일관성"
        shapes = {
            "claude": {"type": "user", "message": {"role": "user", "content": message}},
            "codex": {"type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": message}]}},
            "opencode": {"type": "text", "part": {"type": "text", "text": message}},
        }
        path = Path(self._tmp.name) / (harness + ".jsonl")
        path.write_text(json.dumps(shapes[harness], ensure_ascii=False) + "\n")
        return path

    def test_tool_heavy_tail_keeps_visible_dialogue_for_all_harnesses(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                path = self._transcript(harness)
                with path.open("a") as handle:
                    handle.write(json.dumps({"type": "tool", "payload": "x" * (2 << 20)}) + "\n")
                text, offset = rt.read_delta(str(path), 0, harness=harness)
                self.assertIn("Fleet 세션 제목 일관성", text)
                self.assertLessEqual(len(text), rt.TEXT_CAP)
                self.assertEqual(offset, path.stat().st_size)

    def test_codex_task_anchor_survives_tool_heavy_tail(self):
        path = self._transcript("codex")
        with path.open("a") as handle:
            # Put the current intent beyond the separate 64 KiB head anchor too.
            handle.write(json.dumps({"type": "tool", "payload": "x" * (128 << 10)}) + "\n")
            handle.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Latest Fleet subject"}]}}) + "\n")
            handle.write(json.dumps({"type": "tool", "payload": "x" * (2 << 20)}) + "\n")
        self.assertEqual(rt.read_origin(str(path), "codex"), "Latest Fleet subject")

    def test_explicit_now_language_overrides_fleet_for_both_lines(self):
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            self.assertIn("in Korean.", rt._prompt("d"))
            self.assertEqual(rt.validate_title("TITLE: 세션 제목 일관성\nNOW: 시험 중", rt._title_lang()), "세션 제목 일관성")
            config.ensure()
            config.config_path().write_text('{"title_language": "en"}\n')
            prompt = rt._prompt("d")
            self.assertIn("in Korean.", prompt)
            self.assertIn("NOW: one sentence, in Korean,", prompt)
            self.assertEqual(rt.validate_title("세션 제목 일관성", rt._title_lang()), "세션 제목 일관성")

    def test_claude_command_metadata_cannot_be_anchor_or_language(self):
        rows = [
            {"type": "user", "message": {"role": "user", "content": text}}
            for text in ("<local-command-caveat>Do not respond</local-command-caveat>",
                         "<command-name>/model</command-name>",
                         "<local-command-stdout>Set model</local-command-stdout>",
                         "<task-notification>Background task finished</task-notification>",
                         "제목 언어를 맞춰줘")
        ]
        rows.append({"type": "user", "isMeta": True,
                     "message": {"role": "user", "content": "Injected English bootstrap"}})
        rows.append({"type": "user", "isCompactSummary": True,
                     "message": {"role": "user", "content": "Earlier conversation summary"}})
        raw = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        self.assertEqual(rt._origin_text(raw, "claude"), "제목 언어를 맞춰줘")
        self.assertEqual(rt._origin_text(raw, "claude", latest=True), "제목 언어를 맞춰줘")
        self.assertNotIn("Set model", rt._delta_text(raw, "claude"))
        self.assertEqual(rt._user_language("<command-name>한국어 메타</command-name>"), "")

    def test_owner_uses_last_main_observation_for_both_lines(self):
        titles.write("main-ko", "한국어 제목", harness="claude", now=900,
                     source="refresher:claude|user-language=Korean@200")
        titles.write("main-ja", "日本語の題名", harness="opencode", now=1000,
                     source="refresher:opencode|user-language=Japanese@100")
        titles.write("dispatch-att-owner", "English owner", harness="codex", now=1100,
                     source="refresher:codex|user-language=English@300")
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "",
                                          "LC_MESSAGES": "", "FLEET_NOW_LANG": ""}):
            prompt = rt._prompt("Registered worker: scan logs", anchor="Assignment: implementation")
            self.assertIn("in Korean.", prompt)
            self.assertIn("NOW: one sentence, in Korean,", prompt)
            config.ensure()
            config.config_path().write_text('{"title_language":"en"}\n')
            self.assertEqual(rt._title_lang(), "English")

    def test_latest_user_language_ignores_later_native_metadata(self):
        rows = [
            {"type": "user", "message": {"role": "user", "content": text}}
            for text in ("Earlier English request", "제목을 한국어로 맞춰줘",
                         "<local-command-stdout>Set model</local-command-stdout>")
        ]
        raw = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        self.assertEqual(rt._origin_text(raw, "claude", latest=True), "제목을 한국어로 맞춰줘")
        codex = [{"type":"response_item", "payload":{"type":"message", "role":"user",
                  "content":[{"type":"input_text", "text":text}]}}
                 for text in ("제목을 한국어로 맞춰줘", "AGENT_HARNESS_COMPLETION_V1\n{}")]
        self.assertEqual(rt._origin_text("\n".join(json.dumps(row, ensure_ascii=False)
                                                 for row in codex), "codex", latest=True),
                         "제목을 한국어로 맞춰줘")

    def test_main_observation_survives_failed_refresh_and_current_language_wins(self):
        path = self._transcript("claude")
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "",
                                          "LC_MESSAGES": "", "FLEET_NOW_LANG": ""}), \
             mock.patch.object(rt, "run_worker", return_value="") as worker:
            with mock.patch("time.time", return_value=200):
                rt.main(["--sid", "main-observation", "--transcript", str(path),
                         "--slotdir", str(Path(self._tmp.name) / "slot")])
            self.assertIn("user-language=Korean@200", titles.read("main-observation")["source"])
            with path.open("a") as handle:
                handle.write(json.dumps({"type":"user", "message":{"role":"user",
                                         "content":"Please use English now"}}) + "\n")
            with mock.patch("time.time", return_value=300):
                rt.main(["--sid", "main-observation", "--transcript", str(path),
                         "--slotdir", str(Path(self._tmp.name) / "slot")])
            self.assertIn("in English.", worker.call_args.args[0])
            self.assertIn("NOW: one sentence, in English,", worker.call_args.args[0])
            self.assertIn("user-language=English@300", titles.read("main-observation")["source"])

    def test_old_main_hydration_cannot_overwrite_a_newer_human_language(self):
        titles.write("recent-main", "최근 한국어 요청", harness="codex",
                     source="refresher:codex|user-language=Korean@200")
        path = Path(self._tmp.name) / "old-main.jsonl"
        path.write_text(json.dumps({"type":"user", "timestamp":"1970-01-01T00:01:40Z",
                                   "message":{"role":"user", "content":"Earlier English request"}}))
        with mock.patch.dict(os.environ, {"LANG":"en_US.UTF-8", "LC_ALL":"",
                                          "LC_MESSAGES":"", "FLEET_NOW_LANG":""}), \
             mock.patch.object(rt, "run_worker", return_value=""):
            rt.main(["--sid", "old-main", "--transcript", str(path),
                     "--slotdir", str(Path(self._tmp.name) / "slot")])
            self.assertIn("user-language=English@100", titles.read("old-main")["source"])
            self.assertEqual(rt._observed_main_language(), "Korean")

    def test_language_limits_and_localized_failure_outputs(self):
        for language, title in (("Korean", "세션 제목 일관성"), ("Japanese", "セッションのタイトル"),
                                ("Chinese", "会话标题一致性"), ("French", "Résumé des sessions")):
            with self.subTest(language=language):
                self.assertEqual(rt.validate_title(title, language), title)
        self.assertEqual(len(rt.validate_title("가" * 30, "Korean")), 20)
        for bad in ("진행 중 제목 갱신", "제목 없음", "알 수 없습니다", "Awaiting worker result"):
            self.assertIsNone(rt.validate_title(bad, "Korean"))

    def test_auto_uses_user_task_language_under_english_locale_not_worker_delta(self):
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "",
                                          "LC_MESSAGES": "", "FLEET_NOW_LANG": ""}):
            prompt = rt._prompt("Registered worker: scan implementation logs", anchor="제목 언어를 맞춰줘")
            self.assertIn("in Korean.", prompt)
            self.assertIn("NOW: one sentence, in Korean,", prompt)
            config.ensure()
            config.config_path().write_text('{"title_language":"en"}\n')
            self.assertEqual(rt._title_lang("제목 언어를 맞춰줘"), "English")

    def test_wrong_language_prior_is_regenerated_without_new_delta_and_preserved_on_failure(self):
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "",
                                          "LC_MESSAGES": "", "FLEET_NOW_LANG": ""}):
            for harness in ("claude", "codex", "opencode"):
                with self.subTest(harness=harness):
                    path = self._transcript(harness)
                    sid = harness + "-old-language"
                    titles.write(sid, "Old English Subject", harness=harness,
                                 offset=path.stat().st_size, summary="이전 요약", now=time.time() - 700)
                    before = titles.read(sid, harness=harness)
                    with mock.patch.object(rt, "run_worker", return_value="") as worker:
                        rt.main(["--harness", harness, "--sid", sid, "--transcript", str(path),
                                 "--slotdir", str(Path(self._tmp.name) / "slot")])
                    self.assertEqual(worker.call_count, 1)
                    self.assertIn("in Korean.", worker.call_args.args[0])
                    self.assertNotIn("PRIOR TITLE (data", worker.call_args.args[0])
                    after = titles.read(sid, harness=harness)
                    for key in ("title", "title_ts", "summary", "summary_ts", "offset"):
                        self.assertEqual(after[key], before[key], key)

    def test_language_change_reaches_existing_scheduler_despite_recent_unchanged_source(self):
        path = self._transcript("claude")
        titles.write("changed-language", "Old English Subject", now=time.time(),
                     offset=path.stat().st_size, summary="이전 요약")
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "",
                                          "LC_MESSAGES": "", "FLEET_NOW_LANG": ""}), \
             mock.patch.object(rt, "worker_argv", return_value=["missing"]) as probe, \
             mock.patch.object(rt, "_executable_available", return_value=False):
            self.assertFalse(rt.maybe_spawn("claude", "changed-language", str(path)))
            probe.assert_called_once_with("probe")

    def test_title_and_now_normalize_the_same_language(self):
        for code, name in (("ko", "Korean"), ("en", "English"), ("ja", "Japanese")):
            with self.subTest(code=code), mock.patch.dict(os.environ, {"FLEET_NOW_LANG": code}):
                self.assertEqual(rt._title_lang(), name)
                self.assertIn("NOW: one sentence, in " + name + ",", rt._prompt("d"))

    def test_declarations_precede_locale_and_stale_observations_for_all_harnesses(self):
        root = Path(os.environ["CLAUDE_CONFIG_DIR"])
        root.mkdir(parents=True, exist_ok=True)
        (root / "settings.json").write_text('{"language":"korean"}')
        config.ensure()
        with mock.patch.dict(os.environ, {"LANG":"ja_JP.UTF-8", "LC_ALL":"",
                                          "LC_MESSAGES":"", "FLEET_NOW_LANG":""}):
            for harness in ("claude", "codex", "opencode"):
                prompt = rt._prompt(harness + " English delta", anchor="English request",
                                    observed_language="English")
                self.assertIn("in Korean.", prompt)
                self.assertIn("NOW: one sentence, in Korean,", prompt)
            config.config_path().write_text('{"title_language":"fr"}')
            self.assertEqual(rt._title_lang(), "French")
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG":"de"}):
                self.assertEqual(rt._title_lang(), "German")
            config.config_path().write_text('{"title_language":"auto"}')
            (root / "settings.json").write_text('{"language":"Spanish"}')
            self.assertEqual(rt._title_lang(), "Spanish")
            (root / "settings.json").write_text('invalid')
            self.assertEqual(rt._title_lang(), "Japanese")

    def test_generated_inputs_do_not_replace_the_last_real_user_language(self):
        producer = Path(__file__).resolve().parents[3] / "utilities" / "dispatch_session_sweep.py"
        current_header = next(
            ast.literal_eval(node.value) for node in ast.parse(producer.read_text()).body
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "COMPLETION_DELIVERY_HEADER"
                for target in node.targets)
        )
        automated = ("[notice] Your peer message ref abc is still pending",
                     "Hearting runtime delivery (SD-111 durable pending records)",
                     current_header,
                     "Hearting notice about work you started (a remote run ended)",
                     "Hearting human gate awaiting your decision (SD-123/129)",
                     "Hearting supervision needs attention. This is not workflow completion. reason=join-deadline owner=att-example.",
                     "/quit", "<system-reminder>Hook output</system-reminder>",
                     "<hook-output>English output</hook-output>",
                     "Peer task\n(peer-from: codex [ab] peer ; ref=12345678)")
        for harness in ("claude", "codex", "opencode"):
            for text in automated:
                with self.subTest(harness=harness, text=text):
                    content = ["한국어 사용자 요청", text]
                    rows = [{"type":"user", "message":{"role":"user", "content":t}}
                            for t in content]
                    if harness == "codex":
                        rows = [{"type":"response_item", "payload":{"type":"message",
                                 "role":"user", "content":[{"type":"input_text", "text":t}]}}
                                for t in content]
                    if harness == "opencode":
                        rows = [{"role":"user", "content":t} for t in content]
                    raw = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
                    self.assertEqual(rt._origin_text(raw, harness, latest=True), "한국어 사용자 요청")
                    self.assertEqual(rt._human_language_text(text), "")

    def test_wrong_language_now_alone_triggers_regeneration_without_new_delta(self):
        root = Path(os.environ["CLAUDE_CONFIG_DIR"])
        root.mkdir(parents=True, exist_ok=True)
        (root / "settings.json").write_text('{"language":"korean"}')
        for harness in ("claude", "codex", "opencode"):
            path = self._transcript(harness)
            sid = harness + "-wrong-now"
            titles.write(sid, "한국어 제목", harness=harness, offset=path.stat().st_size,
                         summary="Inspecting request evidence")
            with mock.patch.object(rt, "run_worker", return_value="TITLE: 한국어 제목\nNOW: 요청 근거 확인 중") as worker:
                rt.main(["--harness", harness, "--sid", sid, "--transcript", str(path),
                         "--slotdir", str(Path(self._tmp.name) / "slot")])
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(titles.read(sid, harness=harness)["summary"], "요청 근거 확인 중")

    def test_config_seed_is_once_and_invalid_file_degrades_without_rewriting(self):
        self.assertEqual(config.ensure(dry_run=True)["status"], "would-create")
        self.assertFalse(config.config_path().exists())
        self.assertEqual(config.ensure()["status"], "created")
        self.assertEqual(config.config_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(config.validate()["status"], "valid")
        path = config.config_path()
        path.write_text('{"title_language":"ko"}\n')
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(config.ensure()["status"], "preserved")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(rt._title_lang(), "Korean")
        path.write_text('{"title_language": ["ko"]}')
        self.assertEqual(config.validate()["status"], "invalid")
        self.assertEqual(config.title_language(), "auto")
        self.assertEqual(path.read_text(), '{"title_language": ["ko"]}')

    def test_installer_registry_uses_the_same_preference_reader(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "install"))
        import fleet_config
        import user_config
        self.assertEqual(fleet_config.ensure()["path"], str(config.config_path()))
        config.config_path().write_text('{"title_language": "ko"}\n')
        row = user_config.status(["fleet"])[0]
        self.assertEqual((row["status"], row["path"]), ("valid", str(config.config_path())))
        self.assertIn("title_language=ko", row["detail"])

    def test_installer_reader_belongs_to_running_package_not_activation_target(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "install"))
        import fleet_config
        target = Path(self._tmp.name) / "minimal-source"
        (target / "core").mkdir(parents=True)
        (target / "core/CORE.md").write_text("# activation target\n")
        with mock.patch.dict(os.environ, {"AGENT_HOME": str(target)}):
            self.assertEqual(fleet_config.ensure()["status"], "created")
            self.assertEqual(fleet_config.validate()["status"], "valid")
        self.assertFalse((target / "tools").exists())

    def test_visible_title_gap_is_blank_in_both_layouts_for_three_harnesses(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                sess = Session(harness=harness, pid=99999999, cwd="/native-cwd-name",
                               session_id="sid", slug="native-slug", registry_name="native-registry",
                               session_tag="ab", model="fixture", title=None)
                self.assertEqual(render._session_name(sess), "")
                wide = "".join(text for text, _ in render._session_row(sess, narrow=False, name_width=40))
                narrow = "".join(text for text, _ in render._session_row_2line(sess, term_width=100)[0])
                for text in (wide, narrow):
                    self.assertIn("[ab]", text)
                    self.assertNotIn("native-", text)
                sess.title = "세션 제목 일관성"
                self.assertEqual(render._session_name(sess), sess.title)
                sess.runtime_name = "사용자가 정한 이름"
                self.assertEqual(render._session_name(sess), sess.runtime_name)

    def test_shared_subject_selection_reaches_fleet_and_herdr(self):
        self.assertEqual(current_session_subject("  사용자 이름  ", "성공 제목"),
                         "사용자 이름")
        self.assertEqual(current_session_subject(None, "  마지막 성공 제목  "),
                         "마지막 성공 제목")
        self.assertEqual(current_session_subject(None, None), "")
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                session = Session(harness=harness, pid=1, session_id="subject",
                                  runtime_name=None, title="마지막 성공 제목")
                self.assertEqual(render._session_name(session), "마지막 성공 제목")
                with mock.patch.object(herdr_projection, "resolve_display_inputs",
                                       return_value={"runtime_name": "사용자 이름"}), \
                     mock.patch.object(titles, "last_title", return_value="마지막 성공 제목"):
                    self.assertEqual(herdr_projection.session_title(harness, "subject"),
                                     "사용자 이름")
                with mock.patch.object(herdr_projection, "resolve_display_inputs",
                                       return_value={"runtime_name": None}), \
                     mock.patch.object(titles, "last_title", return_value=None):
                    self.assertEqual(herdr_projection.session_title(harness, "subject"), "")

    def test_codex_first_message_name_cannot_override_title_or_fill_gap(self):
        path = self._transcript("codex")
        sid = "codex-native-name"
        home = Path(self._tmp.name) / "codex-name-home"
        home.mkdir()
        (home / "session_index.jsonl").write_text(json.dumps({
            "id": sid, "thread_name": "이어서해", "updated_at": "2026-10-07T00:00:00Z"}) + "\n")
        with mock.patch.object(codex, "_home", return_value=str(home)), \
             mock.patch.object(codex.session_registry, "read", return_value=None), \
             mock.patch.dict(codex._PROC_PATHS, {99999999: str(path)}), \
             mock.patch("fleet.session_handle.resolve_display_inputs", return_value={"runtime_name": None}):
            self.assertEqual(codex._thread_runtime_names(str(home))[sid], "이어서해")
            sess = Session(harness="codex", pid=99999999, cwd="/fixture", session_id=sid)
            codex.enrich(sess)
            self.assertEqual(render._session_name(sess), "")
            self.assertIsNone(sess.runtime_name)
            titles.write(sid, "세션 제목 일관성", harness="codex", now=time.time() - 90000)
            codex.enrich(sess)
            self.assertEqual(render._session_name(sess), "세션 제목 일관성")
        with mock.patch.object(codex, "_home", return_value=str(home)), \
             mock.patch.object(codex.session_registry, "read", return_value=None), \
             mock.patch.dict(codex._PROC_PATHS, {99999999: str(path)}), \
             mock.patch("fleet.session_handle.resolve_display_inputs", return_value={"runtime_name": "직접 정한 이름"}):
            codex.enrich(sess)
            self.assertEqual(render._session_name(sess), "직접 정한 이름")

    def test_failed_refresh_keeps_title_or_blank_with_one_existing_call(self):
        for harness in ("claude", "codex", "opencode"):
            for prior in ("세션 제목 일관성", ""):
                with self.subTest(harness=harness, prior=prior):
                    path = self._transcript(harness)
                    sid = harness + ("-prior" if prior else "-blank")
                    titles.write(sid, prior, harness=harness, now=time.time() - 90000)
                    with mock.patch.object(rt, "run_worker", return_value="") as worker, \
                         mock.patch.object(rt, "_provider_source", return_value="refresher:fixture"):
                        rt.main(["--harness", harness, "--sid", sid, "--transcript", str(path),
                                 "--slotdir", str(Path(self._tmp.name) / "slot")])
                    self.assertEqual(worker.call_count, 1)
                    self.assertEqual(titles.last_title(sid, harness=harness), prior or None)

    def test_same_korean_title_contract_for_three_harnesses_in_one_call_each(self):
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            for harness in ("claude", "codex", "opencode"):
                with self.subTest(harness=harness):
                    path = self._transcript(harness)
                    sid = harness + "-korean"
                    with mock.patch.object(rt, "run_worker", return_value="TITLE: 세션 제목 일관성\nNOW: 회귀 확인 중") as worker, \
                         mock.patch.object(rt, "_provider_source", return_value="refresher:fixture"):
                        rt.main(["--harness", harness, "--sid", sid, "--transcript", str(path),
                                 "--slotdir", str(Path(self._tmp.name) / "slot")])
                    self.assertEqual(worker.call_count, 1)
                    self.assertIn("in Korean.", worker.call_args.args[0])
                    self.assertEqual(titles.last_title(sid, harness=harness), "세션 제목 일관성")

    def test_opencode_native_title_never_fills_gap_and_stale_success_survives(self):
        db = Path(self._tmp.name) / "opencode.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE session (id, slug, agent, model, cost, tokens_input, tokens_output, "
                        "tokens_reasoning, time_updated, parent_id, directory, time_created, title)")
            con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "ses_title", "fixture", None, None, 0, 0, 0, 0, 1000, None, "/fixture", 1000, "지엽적인 자체 제목"))
        before = db.read_bytes()
        with mock.patch.object(opencode, "_db", return_value=str(db)), \
             mock.patch.object(opencode.session_registry, "read", return_value=None), \
             mock.patch.object(opencode, "session_of_process",
                               return_value=("ses_title", "opencode-tui-selection")), \
             mock.patch.object(opencode, "_proc_start_ticks", return_value="55"):
            sess = Session(harness="opencode", pid=99999999, proc_start="55", cwd="/fixture")
            opencode.enrich(sess)
            self.assertIsNone(sess.title)
            titles.write("ses_title", "세션 제목 일관성", harness="opencode", now=time.time() - 90000)
            opencode.enrich(sess)
            self.assertEqual(sess.title, "세션 제목 일관성")
        self.assertEqual(db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
