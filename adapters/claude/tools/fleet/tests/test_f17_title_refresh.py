#!/usr/bin/env python3
"""F-17 — live-title refresher (fleet-owned sidecar + no-tools title worker).
Hermetic: real fs access is confined to tempfile dirs; title state and Claude config are
pointed at tmp roots. No live provider is invoked — run_worker() is monkeypatched for
security/behavior assertions.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import titles                       # noqa: E402
from fleet import refresh_title as rt           # noqa: E402
from fleet.model import DispatchJob, Session    # noqa: E402
from fleet.collectors import claude, codex      # noqa: E402

_REPO_ROOT = os.path.dirname(_TOOLS_DIR)
_STATUSLINE = os.path.join(_REPO_ROOT, "adapters", "claude", "statusline.sh")


class _ConfigHomeMixin:
    """Points runtime/config state at a fresh tmp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_env = os.environ.get("CLAUDE_CONFIG_DIR")
        self._old_title_state = os.environ.get("FLEET_TITLE_STATE_DIR")
        self._old_safety_env = {key: os.environ.get(key) for key in (
            "FLEET_TITLE_DISABLE", "FLEET_TITLE_CONCURRENCY", "FLEET_TITLE_MAX_STARTS",
            "FLEET_TITLE_PRIORITY_MAX_STARTS", "FLEET_TITLE_COMMAND",
            "AGENT_MODEL_GOVERNOR_ROOT", "HARNESS_CAPACITY_SCORES",
            "HARNESS_CAPACITY_BIAS", "DISPATCH_DEFAULTS_CONFIG",
        )}
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self._tmp.name, "claude")
        os.environ["FLEET_TITLE_STATE_DIR"] = os.path.join(self._tmp.name, "state")
        for key in self._old_safety_env:
            os.environ.pop(key, None)
        os.environ["AGENT_MODEL_GOVERNOR_ROOT"] = os.path.join(self._tmp.name, "governor")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._old_env
        if self._old_title_state is None:
            os.environ.pop("FLEET_TITLE_STATE_DIR", None)
        else:
            os.environ["FLEET_TITLE_STATE_DIR"] = self._old_title_state
        for key, value in self._old_safety_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()


class TitlesHelperTest(_ConfigHomeMixin, unittest.TestCase):

    def test_write_then_read_roundtrip(self):
        titles.write("sid1", "Fix login bug", source="refresher", offset=42, now=100.0)
        d = titles.read("sid1")
        self.assertEqual(d, {"title": "Fix login bug", "ts": 100.0, "source": "refresher", "offset": 42})

    def test_write_atomic_no_partial(self):
        titles.write("sid2", "Some title", now=100.0)
        d = titles.read("sid2")
        self.assertIsInstance(d, dict)
        self.assertEqual(d["title"], "Some title")
        leftovers = [n for n in os.listdir(titles.titles_dir()) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_fresh_title_within_window(self):
        titles.write("sid3", "Recent title", now=1000.0)
        self.assertEqual(titles.fresh_title("sid3", now=1000.0 + 60), "Recent title")

    def test_fresh_title_stale_beyond_24h(self):
        titles.write("sid4", "Old title", now=1000.0)
        self.assertIsNone(titles.fresh_title("sid4", now=1000.0 + 25 * 3600))

    def test_fresh_title_empty_title_is_none(self):
        titles.write("sid5", "", now=1000.0)
        self.assertIsNone(titles.fresh_title("sid5", now=1000.0 + 1))

    def test_read_malformed_json_passthrough(self):
        p = titles.sidecar_path("sid6")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertIsNone(titles.read("sid6"))

    def test_read_missing_returns_none(self):
        self.assertIsNone(titles.read("sid-does-not-exist"))

    def test_sweep_deletes_old_keeps_fresh(self):
        titles.write("old", "Old", now=1000.0)
        titles.write("fresh", "Fresh", now=1000.0)
        old_p = titles.sidecar_path("old")
        fresh_p = titles.sidecar_path("fresh")
        old_mtime = time.time() - (8 * 24 * 3600)
        os.utime(old_p, (old_mtime, old_mtime))
        n = titles.sweep()
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(old_p))
        self.assertTrue(os.path.exists(fresh_p))

    def test_summary_roundtrip(self):
        titles.write("sidS", "A title", summary="지금 렌더 그룹 루프를 분석 중", now=100.0)
        d = titles.read("sidS")
        self.assertEqual(d["summary"], "지금 렌더 그룹 루프를 분석 중")

    def test_summary_omitted_when_none(self):
        titles.write("sidS2", "A title", now=100.0)
        d = titles.read("sidS2")
        self.assertNotIn("summary", d)
        self.assertIsNone(titles.fresh_summary("sidS2", now=101.0))

    def test_old_sidecar_without_summary_key_is_compatible(self):
        p = titles.sidecar_path("sidOld")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"title": "Old-shape title", "ts": time.time(), "source": "refresher",
                      "offset": 0}, f)
        self.assertEqual(titles.fresh_title("sidOld"), "Old-shape title")
        self.assertIsNone(titles.fresh_summary("sidOld"))

    def test_fresh_summary_survives_the_old_fifteen_minute_cutoff(self):
        # F-63: an aged summary stays visible (render tags it with its age) and
        # only the 24h title-window still expires it.
        titles.write("sidF", "t", summary="working on it", now=1000.0)
        self.assertEqual(titles.fresh_summary("sidF", now=1000.0 + 14 * 60), "working on it")
        self.assertEqual(titles.fresh_summary("sidF", now=1000.0 + 16 * 60), "working on it")
        self.assertEqual(titles.fresh_summary("sidF", now=1000.0 + 23 * 3600), "working on it")
        self.assertIsNone(titles.fresh_summary("sidF", now=1000.0 + 25 * 3600))

    def test_fresh_summary_window_matches_title_window(self):
        # F-63: honesty moved from hiding to age-tagging, so the windows align.
        self.assertEqual(titles._FRESH_SUMMARY_SEC, titles._FRESH_SEC)

    def test_fresh_summary_with_ts_returns_sidecar_ts(self):
        titles.write("sidTs", "t", summary="harvesting children", now=2000.0)
        self.assertEqual(
            titles.fresh_summary_with_ts("sidTs", now=2000.0 + 3600),
            ("harvesting children", 2000.0))
        self.assertEqual(
            titles.fresh_summary_with_ts("sidTs", now=2000.0 + 25 * 3600),
            (None, None))
        self.assertEqual(titles.fresh_summary_with_ts("sidMissing"), (None, None))

    def test_fresh_summary_must_cover_latest_user_boundary(self):
        titles.write("sidB", "t", summary="previous turn", offset=42, now=1000.0)
        self.assertIsNone(titles.fresh_summary(
            "sidB", now=1001.0, after_offset=42))
        self.assertEqual(titles.fresh_summary(
            "sidB", now=1001.0, after_offset=41), "previous turn")

    def test_codex_user_boundary_is_exact_byte_offset(self):
        path = os.path.join(self._tmp.name, "rollout.jsonl")
        first = json.dumps({"type": "session_meta", "payload": {}}).encode() + b"\n"
        user = json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": []},
        }).encode() + b"\n"
        with open(path, "wb") as handle:
            handle.write(first + user)
        self.assertEqual(codex._latest_user_message_offset(path), len(first))
        self.assertEqual(
            codex._latest_user_message_offset(path, chunk=len(user)), len(first))


class PriorityTest(_ConfigHomeMixin, unittest.TestCase):

    def _make_transcript(self, home, cwd, sid, ai_title):
        proj = os.path.join(home, "projects", claude._enc_cwd(cwd))
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "ai-title", "aiTitle": ai_title}) + "\n")
        return path

    def _sess(self, cwd, sid):
        return Session(harness="claude", pid=999999, cwd=cwd, session_id=sid, slug="repo-ab12cd34")

    def test_priority_sidecar_fresh_beats_ai_title(self):
        home = os.environ["CLAUDE_CONFIG_DIR"]
        self._make_transcript(home, "/proj/a", "sidA", "AI Title Wins Not")
        titles.write("sidA", "Sidecar Title", now=time.time())
        sess = self._sess("/proj/a", "sidA")
        claude.enrich(sess)
        self.assertEqual(sess.title, "Sidecar Title")

    def test_priority_sidecar_stale_falls_to_ai_title(self):
        home = os.environ["CLAUDE_CONFIG_DIR"]
        self._make_transcript(home, "/proj/b", "sidB", "AI Title Fallback")
        titles.write("sidB", "Stale Sidecar", now=time.time() - 25 * 3600)
        sess = self._sess("/proj/b", "sidB")
        claude.enrich(sess)
        self.assertEqual(sess.title, "AI Title Fallback")

    def test_priority_no_sidecar_uses_ai_title(self):
        home = os.environ["CLAUDE_CONFIG_DIR"]
        self._make_transcript(home, "/proj/c", "sidC", "Only AI Title")
        sess = self._sess("/proj/c", "sidC")
        claude.enrich(sess)
        self.assertEqual(sess.title, "Only AI Title")

    def test_priority_all_absent_title_none_slug_fallback(self):
        sess = self._sess("/proj/d-empty", "sidD")
        claude.enrich(sess)
        self.assertIsNone(sess.title)

    def test_sidecar_malformed_falls_through(self):
        home = os.environ["CLAUDE_CONFIG_DIR"]
        self._make_transcript(home, "/proj/e", "sidE", "Fallback After Bad Sidecar")
        p = titles.sidecar_path("sidE")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{broken")
        sess = self._sess("/proj/e", "sidE")
        claude.enrich(sess)
        self.assertEqual(sess.title, "Fallback After Bad Sidecar")

    def test_slug_never_overwritten(self):
        home = os.environ["CLAUDE_CONFIG_DIR"]
        self._make_transcript(home, "/proj/f", "sidF", "Some AI Title")
        titles.write("sidF", "Some Sidecar Title", now=time.time())
        sess = self._sess("/proj/f", "sidF")
        claude.enrich(sess)
        self.assertEqual(sess.slug, "repo-ab12cd34")


class ValidateTitleTest(unittest.TestCase):

    def test_validate_len_cap(self):
        out = rt.validate_title("alpha bravo " + "x" * 120)
        self.assertEqual(len(out), rt.TITLE_MAXLEN)

    def test_validate_newline_strip_takes_first_line(self):
        out = rt.validate_title("\n\n  Fix login flow  \nsecond line")
        self.assertEqual(out, "Fix login flow")

    def test_validate_empty_returns_none(self):
        self.assertIsNone(rt.validate_title(""))
        self.assertIsNone(rt.validate_title("   \n  \n"))

    def test_validate_non_printable_reject(self):
        out = rt.validate_title("Fix\x00\x01 login flow")
        self.assertEqual(out, "Fix login flow")
        self.assertIsNone(rt.validate_title("\x00\x01\x02"))

    def test_validate_strips_quotes_and_period(self):
        self.assertEqual(rt.validate_title('"Fix login bug."'), "Fix login bug")

    def test_validate_clips_at_new_40_char_cap(self):
        out = rt.validate_title("alpha bravo " + "x" * 41)
        self.assertEqual(len(out), 40)
        self.assertEqual(rt.TITLE_MAXLEN, 40)

    def test_validate_rejects_seven_words(self):
        self.assertIsNone(rt.validate_title("one two three four five six seven"))

    def test_validate_prefers_labeled_title_line(self):
        out = rt.validate_title("TITLE: Fleet strip revamp\nNOW: writing code")
        self.assertEqual(out, "Fleet strip revamp")

    def test_validate_bare_line_still_works_without_a_label(self):
        # A custom/legacy provider that only ever emits a bare title stays supported.
        self.assertEqual(rt.validate_title("Bare title no label"), "Bare title no label")


class ValidateSummaryTest(unittest.TestCase):

    def test_korean_summary_accepted(self):
        out = rt.validate_summary("지금 render.py 그룹 루프의 틴트 적용 경로를 분석 중")
        self.assertEqual(out, "지금 render.py 그룹 루프의 틴트 적용 경로를 분석 중")

    def test_meta_response_rejected(self):
        self.assertIsNone(rt.validate_summary("Cannot determine current activity"))
        self.assertIsNone(rt.validate_summary("unknown"))
        self.assertIsNone(rt.validate_summary("Unknown"))

    def test_multiline_rejected(self):
        self.assertIsNone(rt.validate_summary("Doing thing one\nDoing thing two"))

    def test_empty_rejected(self):
        self.assertIsNone(rt.validate_summary(""))
        self.assertIsNone(rt.validate_summary("   \n  "))

    def test_over_length_clips_to_summary_maxlen(self):
        out = rt.validate_summary("x" * 200)
        self.assertEqual(len(out), rt.SUMMARY_MAXLEN)

    def test_labeled_line_extraction(self):
        raw = "TITLE: Fleet strip revamp\nNOW: 지금 그룹 루프를 분석 중"
        self.assertEqual(rt._labeled_line(raw, rt._NOW_LINE_RE), "지금 그룹 루프를 분석 중")
        self.assertEqual(rt._labeled_line(raw, rt._TITLE_LINE_RE), "Fleet strip revamp")

    def test_missing_now_line_extracts_nothing(self):
        self.assertIsNone(rt._labeled_line("TITLE: only a title here", rt._NOW_LINE_RE))


class StormGuardTest(_ConfigHomeMixin, unittest.TestCase):
    """No provider is invoked: detached workers are replaced with inert process stubs."""

    def setUp(self):
        super().setUp()
        self.transcript = os.path.join(self._tmp.name, "transcript.jsonl")
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"message": "title this session"}) + "\n")
        os.environ["FLEET_TITLE_COMMAND"] = sys.executable + " -c pass"

    def _fake_spawns(self, sessions):
        captured = []
        original = rt.subprocess.Popen
        rt.subprocess.Popen = lambda argv, **kwargs: captured.append(list(argv)) or object()
        try:
            started = rt.schedule_sessions(sessions)
        finally:
            rt.subprocess.Popen = original
        return started, captured

    def _session(self, sid, **flags):
        session = Session(
            harness="claude", pid=100, cwd="/repo", session_id=sid, liveness="working",
            **flags,
        )
        session._transcript_path = self.transcript
        return session

    def test_two_global_slots_bound_two_hundred_session_backlog(self):
        sessions = [self._session("sid-%03d" % index) for index in range(200)]
        started, captured = self._fake_spawns(sessions)
        self.assertEqual(started, rt.DEFAULT_CONCURRENCY)
        self.assertEqual(len(captured), rt.DEFAULT_CONCURRENCY)

    def test_rolling_start_budget_bounds_sequential_backlog(self):
        os.environ["FLEET_TITLE_CONCURRENCY"] = "1"
        os.environ["FLEET_TITLE_MAX_STARTS"] = "3"
        captured = []

        def fake_popen(argv, **_kwargs):
            captured.append(list(argv))
            rt._remove_empty_dir(argv[argv.index("--slotdir") + 1])
            rt._remove_empty_dir(argv[argv.index("--lockdir") + 1])
            return object()

        original = rt.subprocess.Popen
        rt.subprocess.Popen = fake_popen
        try:
            results = [rt.maybe_spawn("claude", "seq-%d" % index, self.transcript)
                       for index in range(20)]
        finally:
            rt.subprocess.Popen = original
        self.assertEqual(sum(results), 3)
        self.assertEqual(len(captured), 3)

    def test_direct_provider_call_cannot_bypass_start_budget(self):
        os.environ["FLEET_TITLE_CONCURRENCY"] = "1"
        os.environ["FLEET_TITLE_MAX_STARTS"] = "1"
        calls = []

        class _Done:
            stdout = "Bounded title"
            returncode = 0

        original = rt.subprocess.run
        rt.subprocess.run = lambda argv, **kwargs: calls.append(list(argv)) or _Done()
        try:
            self.assertEqual(rt.run_worker("prompt"), "Bounded title")
            self.assertEqual(rt.run_worker("prompt"), "")
        finally:
            rt.subprocess.run = original
        self.assertEqual(len(calls), 1)

    def test_rolling_start_budget_expires(self):
        os.environ["FLEET_TITLE_MAX_STARTS"] = "1"
        first = rt._acquire_start_budget(now=1000.0)
        self.assertIsNotNone(first)
        self.assertIsNone(rt._acquire_start_budget(now=1001.0))
        second = rt._acquire_start_budget(now=1000.0 + rt.START_WINDOW_SEC + 1)
        self.assertIsNotNone(second)
        self.assertFalse(os.path.exists(first))

    def test_scheduler_targets_children_but_never_internal_sessions(self):
        # Dispatched children are first-class title targets (user 2026-07-16);
        # only fleet-internal workers (mem/title refreshers, app-servers) stay out.
        sessions = [
            self._session("normal"),
            self._session("memory", mem_worker=True),
            self._session("child", is_child=True),
            self._session("server", app_server=True),
        ]
        seen = []
        original = rt.maybe_spawn
        rt.maybe_spawn = lambda harness, sid, transcript, debounce=rt.DEBOUNCE_SEC, priority=False: (
            seen.append((sid, debounce, priority)) or True)
        try:
            self.assertEqual(rt.schedule_sessions(sessions), 2)
        finally:
            rt.maybe_spawn = original
        self.assertEqual([sid for sid, _debounce, _priority in seen], ["child", "normal"])
        self.assertEqual(seen[0], ("child", rt.CHILD_DEBOUNCE_SEC, True))

    def test_child_sessions_use_shorter_debounce(self):
        # 사용자 확정 2026-07-19: dispatched children move faster than main sessions,
        # so their title/subtitle debounce is much shorter (90s vs 600s) while the
        # shared storm-guard budget (concurrency/start limits) stays one pool for both.
        sessions = [self._session("normal"), self._session("child", is_child=True)]
        seen = {}
        original = rt.maybe_spawn
        rt.maybe_spawn = lambda harness, sid, transcript, debounce=rt.DEBOUNCE_SEC, priority=False: (
            seen.__setitem__(sid, (debounce, priority)) or True)
        try:
            rt.schedule_sessions(sessions)
        finally:
            rt.maybe_spawn = original
        self.assertEqual(seen["normal"], (rt.WORKING_DEBOUNCE_SEC, True))
        self.assertEqual(seen["child"], (rt.CHILD_DEBOUNCE_SEC, True))
        self.assertLess(rt.CHILD_DEBOUNCE_SEC, rt.WORKING_DEBOUNCE_SEC)

    def test_working_main_without_summary_uses_priority_lane_and_120s_debounce(self):
        session = self._session("working-main")
        seen = []
        original = rt.maybe_spawn
        rt.maybe_spawn = lambda **kwargs: seen.append(kwargs) or True
        try:
            rt.schedule_sessions([session])
        finally:
            rt.maybe_spawn = original
        self.assertEqual(seen[0]["debounce"], rt.WORKING_DEBOUNCE_SEC)
        self.assertTrue(seen[0]["priority"])

    def test_registered_job_without_child_session_uses_attempt_log_fallback(self):
        job = DispatchJob(
            key="code-test", slug="child-test", cwd="/repo", harness="codex",
            is_child=True, liveness="working", attempt_id="att-child-test",
        )
        job._summary_sid = "dispatch-att-child-test"
        job._transcript_path = self.transcript
        seen = []
        original = rt.maybe_spawn
        rt.maybe_spawn = lambda **kwargs: seen.append(kwargs) or True
        try:
            self.assertEqual(rt.schedule_sessions([], [job]), 1)
        finally:
            rt.maybe_spawn = original
        self.assertEqual(seen[0]["sid"], "dispatch-att-child-test")
        self.assertEqual(seen[0]["transcript"], self.transcript)
        self.assertEqual(seen[0]["debounce"], rt.CHILD_DEBOUNCE_SEC)
        self.assertTrue(seen[0]["priority"])

    def test_attempt_log_fallback_does_not_double_an_associated_child(self):
        job = DispatchJob(
            key="code-test", slug="child-test", cwd="/repo", harness="claude",
            is_child=True, liveness="working", attempt_id="att-child-test",
        )
        job._summary_sid = "dispatch-att-child-test"
        job._transcript_path = self.transcript
        job._child_session_associated = True
        job._child_refresh_associated = True
        original = rt.maybe_spawn
        rt.maybe_spawn = lambda **_kwargs: self.fail("associated job scheduled twice")
        try:
            self.assertEqual(rt.schedule_sessions([], [job]), 0)
        finally:
            rt.maybe_spawn = original

    def test_nonpersistent_child_identity_keeps_attempt_log_fallback(self):
        job = DispatchJob(
            key="code-test", slug="child-test", cwd="/repo", harness="claude",
            is_child=True, liveness="working", attempt_id="att-child-test",
        )
        job._summary_sid = "dispatch-att-child-test"
        job._transcript_path = self.transcript
        job._child_session_associated = True
        job._child_refresh_associated = False
        seen = []
        original = rt.maybe_spawn
        rt.maybe_spawn = lambda **kwargs: seen.append(kwargs) or True
        try:
            self.assertEqual(rt.schedule_sessions([], [job]), 1)
        finally:
            rt.maybe_spawn = original
        self.assertEqual(seen[0]["sid"], "dispatch-att-child-test")

    def test_disable_marker_and_environment_fail_closed(self):
        os.makedirs(rt.disable_marker_path(), exist_ok=True)
        original = rt.subprocess.Popen
        rt.subprocess.Popen = lambda *_args, **_kwargs: self.fail("disabled refresh spawned")
        try:
            self.assertFalse(rt.maybe_spawn("claude", "disabled", self.transcript))
            self.assertEqual(rt.run_worker("prompt"), "")
        finally:
            rt.subprocess.Popen = original
        os.rmdir(rt.disable_marker_path())
        os.environ["FLEET_TITLE_DISABLE"] = "true"
        self.assertTrue(rt.refresh_disabled())

    def test_zero_and_invalid_limits_are_safe(self):
        os.environ["FLEET_TITLE_CONCURRENCY"] = "0"
        self.assertTrue(rt.refresh_disabled())
        os.environ["FLEET_TITLE_CONCURRENCY"] = "invalid"
        os.environ["FLEET_TITLE_MAX_STARTS"] = "999999"
        self.assertEqual(rt.concurrency_limit(), rt.DEFAULT_CONCURRENCY)
        self.assertEqual(rt.start_limit(), rt.MAX_START_LIMIT)

    def test_stale_slot_is_reclaimed_after_worker_timeout(self):
        os.environ["FLEET_TITLE_CONCURRENCY"] = "1"
        first = rt._acquire_slot(now=1000.0)
        self.assertIsNotNone(first)
        os.utime(first, (800.0, 800.0))
        second = rt._acquire_slot(now=1000.0)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertFalse(os.path.exists(first))
        rt._remove_empty_dir(second)


class DeltaOffsetTest(_ConfigHomeMixin, unittest.TestCase):

    def test_read_delta_from_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": "first"}) + "\n")
            size1 = os.path.getsize(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"message": "second"}) + "\n")
            delta, new_offset = rt.read_delta(path, size1)
            self.assertIn("second", delta)
            self.assertNotIn("first", delta)
            self.assertEqual(new_offset, os.path.getsize(path))

    def test_read_delta_offset_beyond_size_resyncs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": "hello"}) + "\n")
            delta, new_offset = rt.read_delta(path, 999999)
            self.assertIn("hello", delta)
            self.assertEqual(new_offset, os.path.getsize(path))

    def test_read_delta_caps_to_4kb(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for _ in range(2000):
                    f.write(json.dumps({"message": "x" * 20}) + "\n")
            delta, _new_offset = rt.read_delta(path, 0)
            self.assertLessEqual(len(delta.encode("utf-8")), rt.DELTA_CAP)

    def test_main_empty_delta_advances_ts_keeps_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": "hi"}) + "\n")
            size = os.path.getsize(path)
            titles.write("sidM", "Existing Title", offset=size, now=time.time() - 700)
            rt.main(["--sid", "sidM", "--transcript", path])
            d = titles.read("sidM")
            self.assertEqual(d["title"], "Existing Title")
            self.assertGreater(d["ts"], time.time() - 5)

    def test_main_empty_delta_preserves_previous_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": "hi"}) + "\n")
            size = os.path.getsize(path)
            titles.write("sidE", "Existing Title", summary="still doing the same thing",
                         offset=size, now=time.time() - 700)
            rt.main(["--sid", "sidE", "--transcript", path])
            d = titles.read("sidE")
            self.assertEqual(d["title"], "Existing Title")
            self.assertEqual(d["summary"], "still doing the same thing")

    def test_main_two_line_output_saves_title_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": "please rename this session"}) + "\n")
            original = rt.run_worker
            rt.run_worker = lambda *a, **k: "TITLE: Fleet strip revamp\nNOW: 지금 그룹 루프를 분석 중"
            try:
                rt.main(["--sid", "sidTS", "--transcript", path])
            finally:
                rt.run_worker = original
            d = titles.read("sidTS")
            self.assertEqual(d["title"], "Fleet strip revamp")
            self.assertEqual(d["summary"], "지금 그룹 루프를 분석 중")

    def test_main_missing_now_line_degrades_to_title_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": "please rename this session"}) + "\n")
            titles.write("sidTO", "Old title", summary="stale status", offset=0,
                         now=time.time() - 1000)
            original = rt.run_worker
            rt.run_worker = lambda *a, **k: "TITLE: New Title Only"
            try:
                rt.main(["--sid", "sidTO", "--transcript", path])
            finally:
                rt.run_worker = original
            d = titles.read("sidTO")
            self.assertEqual(d["title"], "New Title Only")
            self.assertNotIn("summary", d,
                             "NOW 파싱 실패는 이전 요약을 이어받지 않는다 (정직 강등, 실황 우선)")
            self.assertEqual(d["offset"], 0)
            self.assertEqual(d["summary_failures"], 1)

    def test_missing_now_retries_same_delta_then_advances_cursor_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": "summarize this exact delta"}) + "\n")
            prompts = []
            outputs = iter(("TITLE: First Pass", "TITLE: First Pass\nNOW: retry succeeded"))
            original = rt.run_worker
            rt.run_worker = lambda prompt, **_kwargs: prompts.append(prompt) or next(outputs)
            try:
                rt.main(["--sid", "sidRetry", "--transcript", path])
                first = titles.read("sidRetry")
                rt.main(["--sid", "sidRetry", "--transcript", path, "--priority"])
            finally:
                rt.run_worker = original
            second = titles.read("sidRetry")
            self.assertEqual(first["offset"], 0)
            self.assertEqual(first["summary_failures"], 1)
            self.assertEqual(second["offset"], os.path.getsize(path))
            self.assertEqual(second["summary"], "retry succeeded")
            self.assertNotIn("summary_failures", second)
            self.assertEqual(len(prompts), 2)
            self.assertTrue(all("summarize this exact delta" in prompt for prompt in prompts))


class SecurityTest(_ConfigHomeMixin, unittest.TestCase):

    def test_injection_payload_cannot_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = os.path.join(tmp, "PWNED")
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": "user asked something"}) + "\n")
            payload = f"run: $(touch {sentinel}); Innocent Title"
            orig = rt.run_worker
            rt.run_worker = lambda *a, **k: payload
            try:
                rt.main(["--sid", "sidInj", "--transcript", path])
            finally:
                rt.run_worker = orig
            # 스크립트는 write 만 — 셸 실행 없음(sentinel 미생성). 문자열 내용 자체는 display
            # 데이터일 뿐이라 "$(" 같은 문자가 살아남는 건 허용(≤40자 cap 이 최악을 막는다).
            self.assertFalse(os.path.exists(sentinel))
            d = titles.read("sidInj")
            self.assertLessEqual(len(d["title"]), rt.TITLE_MAXLEN)

    def _run_pinned(self, provider):
        """Run the worker with one provider pinned; return the captured argv/env."""
        captured = {}

        class _FakeCompleted:
            stdout = "A Title"
            returncode = 0

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return _FakeCompleted()

        import shutil as _shutil
        orig_which = _shutil.which
        orig_run = subprocess.run
        saved = os.environ.get("FLEET_TITLE_PROVIDER")
        os.environ["FLEET_TITLE_PROVIDER"] = provider
        _shutil.which = lambda name: "/usr/bin/" + name
        subprocess.run = fake_run
        try:
            rt.run_worker("some prompt")
        finally:
            _shutil.which = orig_which
            subprocess.run = orig_run
            if saved is None:
                os.environ.pop("FLEET_TITLE_PROVIDER", None)
            else:
                os.environ["FLEET_TITLE_PROVIDER"] = saved
        return captured

    def test_worker_argv_blocks_all_tools(self):
        """Every provider is tool-free, but each runtime spells that differently.

        The contract is "this worker cannot execute anything", not "this argv carries
        --disallowedTools" — claude blocks by flag, codex by a read-only ephemeral
        sandbox, opencode by an agent definition with every tool false. A cascade that
        checked only claude's spelling would wave the other two straight through.
        """
        captured = self._run_pinned("claude")
        argv = captured["argv"]
        self.assertIn("--disallowedTools", argv)
        blocked = argv[argv.index("--disallowedTools") + 1]
        for tool in rt.DISALLOWED_TOOLS.split():
            self.assertIn(tool, blocked)
        self.assertEqual(len(rt.DISALLOWED_TOOLS.split()), 11)
        self.assertEqual(captured["env"]["FLEET_TITLE_REFRESH"], "1")
        self.assertEqual(captured["env"]["AGENT_SESSION_ROLE"], "worker")

    def test_codex_provider_runs_read_only_and_ephemeral(self):
        argv = self._run_pinned("codex")["argv"]
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--ephemeral", argv)

    def test_opencode_provider_runs_a_tool_free_agent(self):
        argv = self._run_pinned("opencode")["argv"]
        self.assertEqual(argv[:2], ["opencode", "run"])
        self.assertIn("--pure", argv)                      # no external plugins
        self.assertIn("--agent", argv)
        agent = argv[argv.index("--agent") + 1]
        self.assertEqual(agent, "fleet-titler")
        # The agent definition IS the tool-free contract for this runtime, so it has to
        # exist on disk where --dir can find it, with every tool actually disabled.
        workdir = Path(argv[argv.index("--dir") + 1])
        definition = workdir / ".opencode" / "agent" / (agent + ".md")
        self.assertTrue(definition.is_file())
        body = definition.read_text(encoding="utf-8")
        for tool in ("bash", "edit", "write", "read", "webfetch", "task"):
            self.assertIn("%s: false" % tool, body)

    def test_no_provider_installed_degrades_to_empty(self):
        import shutil as _shutil
        orig_which = _shutil.which
        _shutil.which = lambda name: None
        try:
            self.assertEqual(rt.run_worker("some prompt"), "")
        finally:
            _shutil.which = orig_which

    def test_cascade_skips_an_uninstalled_leader(self):
        """An absent first quality peer must fall through, not go blank."""
        import shutil as _shutil
        orig_which = _shutil.which
        os.environ["HARNESS_CAPACITY_SCORES"] = "claude:80,codex:50"
        _shutil.which = lambda name: None if name == "opencode" else "/usr/bin/" + name
        try:
            self.assertEqual(rt.active_provider(), "claude")
        finally:
            _shutil.which = orig_which

    def test_capacity_does_not_make_opencode_a_quality_peer(self):
        os.environ["HARNESS_CAPACITY_SCORES"] = "claude:70,codex:80,opencode:100"
        self.assertEqual(rt.selected_providers()[0], "codex")

    def test_tight_primary_capacity_promotes_declared_opencode_relief(self):
        os.environ["HARNESS_CAPACITY_SCORES"] = "claude:20,codex:30"
        self.assertEqual(rt.selected_providers()[0], "opencode")

    def test_validate_caps_injected_long_string(self):
        payload = "rm -rf / ; " * 20
        out = rt.validate_title(payload)
        # 강화(2026-07-10): 문장형 긴 주입은 클립이 아니라 거부(None) — 둘 다 오염 차단
        self.assertTrue(out is None or len(out) <= rt.TITLE_MAXLEN)


@unittest.skipUnless(os.path.exists(_STATUSLINE), "adapters/claude/statusline.sh not found")
class TriggerLogicTest(unittest.TestCase):
    """Drives the real statusline.sh with stubbed `setsid` (spawn observation point) and a
    tmp AGENT_HOME so the F-17 trigger block's debounce/lock/no-delay/recursion-guard logic
    is exercised without a live `claude` process or real ~/.claude state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.agent_home = os.path.join(self.root, "home")
        os.makedirs(self.agent_home, exist_ok=True)
        refresher_dir = os.path.join(self.root, "agent_setting", "tools", "fleet")
        os.makedirs(refresher_dir, exist_ok=True)
        self.refresher = os.path.join(refresher_dir, "refresh_title.py")
        with open(self.refresher, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\n")
        self.transcript = os.path.join(self.root, "transcript.jsonl")
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"message": "hi"}) + "\n")
        self.stubdir = os.path.join(self.root, "stub")
        os.makedirs(self.stubdir, exist_ok=True)
        self.sentinel = os.path.join(self.root, "spawned.marker")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_setsid_stub(self, sleep_s=0):
        p = os.path.join(self.stubdir, "setsid")
        with open(p, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n")
            if sleep_s:
                f.write(f"sleep {sleep_s}\n")
            f.write(f"echo spawned >> {json.dumps(self.sentinel)}\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _run(self, sid="sidT", extra_env=None, wait=0.6):
        self._write_setsid_stub()
        env = dict(os.environ)
        env["AGENT_HOME"] = self.agent_home
        env["FLEET_TITLE_STATE_DIR"] = os.path.join(self.root, "title-state")
        env["PATH"] = self.stubdir + os.pathsep + env.get("PATH", "")
        if extra_env:
            env.update(extra_env)
        stdin_json = json.dumps({
            "session_id": sid,
            "transcript_path": self.transcript,
            "cwd": self.root,
            "model": {"display_name": "Test"},
        })
        t0 = time.time()
        proc = subprocess.run(["bash", _STATUSLINE], input=stdin_json, capture_output=True,
                               text=True, env=env, timeout=15)
        elapsed = time.time() - t0
        time.sleep(wait)   # let the detached `&` subshell finish its (stubbed, near-instant) work
        return proc, elapsed

    def test_trigger_debounce_fresh_sidecar_no_spawn(self):
        sc_dir = os.path.join(self.root, "title-state", "claude")
        os.makedirs(sc_dir, exist_ok=True)
        with open(os.path.join(sc_dir, "sidT.json"), "w", encoding="utf-8") as f:
            json.dump({"title": "x", "ts": time.time(), "source": "refresher", "offset": 0}, f)
        self._run()
        self.assertFalse(os.path.exists(self.sentinel))

    def test_trigger_stale_and_grown_spawns_once(self):
        sc_dir = os.path.join(self.root, "title-state", "claude")
        os.makedirs(sc_dir, exist_ok=True)
        sc_path = os.path.join(sc_dir, "sidT.json")
        with open(sc_path, "w", encoding="utf-8") as f:
            json.dump({"title": "x", "ts": 1, "source": "refresher", "offset": 0}, f)
        old = time.time() - 3600
        os.utime(sc_path, (old, old))
        self._run()
        self.assertTrue(os.path.exists(self.sentinel))
        with open(self.sentinel) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)

    def test_trigger_summary_failure_retries_without_new_transcript_mtime(self):
        sc_dir = os.path.join(self.root, "title-state", "claude")
        os.makedirs(sc_dir, exist_ok=True)
        sc_path = os.path.join(sc_dir, "sidT.json")
        with open(sc_path, "w", encoding="utf-8") as f:
            json.dump({"title": "x", "ts": 1, "source": "refresher", "offset": 0,
                       "summary_failures": 1}, f)
        now = time.time()
        os.utime(self.transcript, (now - 300, now - 300))
        os.utime(sc_path, (now - 31, now - 31))
        self._run()
        self.assertTrue(os.path.exists(self.sentinel))

    def test_trigger_lock_prevents_double_spawn(self):
        lockdir = os.path.join(self.root, "title-state", "claude", ".lock-sidT")
        os.makedirs(lockdir, exist_ok=True)
        self._run()
        self.assertFalse(os.path.exists(self.sentinel))

    def test_trigger_no_delay(self):
        self._write_setsid_stub(sleep_s=3)
        env = dict(os.environ)
        env["AGENT_HOME"] = self.agent_home
        env["FLEET_TITLE_STATE_DIR"] = os.path.join(self.root, "title-state")
        env["PATH"] = self.stubdir + os.pathsep + env.get("PATH", "")
        stdin_json = json.dumps({
            "session_id": "sidT", "transcript_path": self.transcript,
            "cwd": self.root, "model": {"display_name": "Test"},
        })
        t0 = time.time()
        subprocess.run(["bash", _STATUSLINE], input=stdin_json, capture_output=True,
                        text=True, env=env, timeout=15)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 2.0)

    def test_trigger_recursion_guard(self):
        self._run(extra_env={"FLEET_TITLE_REFRESH": "1"})
        self.assertFalse(os.path.exists(self.sentinel))

    def test_trigger_kill_switch_marker_prevents_python_spawn(self):
        os.makedirs(os.path.join(self.root, "title-state", rt.DISABLE_MARKER), exist_ok=True)
        self._run()
        self.assertFalse(os.path.exists(self.sentinel))

    def test_trigger_kill_switch_environment_prevents_python_spawn(self):
        self._run(extra_env={"FLEET_TITLE_DISABLE": "1"})
        self.assertFalse(os.path.exists(self.sentinel))


class ValidatorHardeningTest(unittest.TestCase):
    """2026-07-10 라이브 실측 회귀: raw jsonl 조각이 DATA 로 흘러가 haiku 가 한국어
    오류 문장을 냈고 검증이 통과시킴 — 영어-우세·단어수 캡·raw fallback 제거 강제."""

    def test_korean_error_sentence_rejected(self):
        self.assertIsNone(rt.validate_title(
            "대화 발췌가 인코딩되어 읽을 수 없습니다. 원본 텍스트를 제공하거나 구체"))

    def test_sentence_over_word_cap_rejected(self):
        self.assertIsNone(rt.validate_title(
            "this is a very long chatty sentence about the current session and its generic status"))

    def test_specific_responsive_title_accepted(self):
        title = "Shorten Fleet session title length caps"
        self.assertEqual(rt.validate_title(title), title)

    def test_prompt_requests_responsive_length(self):
        # F-16/F-17 merge (2026-07-19): the title shrinks since NOW carries the detail.
        self.assertIn("3-6 words", rt.PROMPT_TEMPLATE)
        self.assertIn("40 characters", rt.PROMPT_TEMPLATE)
        self.assertIn("NOW:", rt.PROMPT_TEMPLATE)

    def test_meta_response_rejected(self):
        for s in ("No conversation excerpt provided", "Cannot determine title",
                  "I cannot read this", "untitled", "Error reading excerpt"):
            self.assertIsNone(rt.validate_title(s), s)

    def test_short_english_title_accepted(self):
        self.assertEqual(rt.validate_title("Fleet UI Optimization"),
                         "Fleet UI Optimization")

    def test_delta_text_drops_unparseable_lines(self):
        txt = rt._delta_text('{"broken json fragment...\nnot json either\n')
        self.assertEqual(txt, "")

    def test_read_delta_drops_partial_first_line(self):
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as tmp:
            path = _os.path.join(tmp, "t.jsonl")
            big = json.dumps({"type": "assistant",
                              "message": {"content": [{"type": "text", "text": "x" * 100000}]}})
            tail = json.dumps({"type": "user", "message": "short readable line"})
            with open(path, "w") as f:
                f.write(big + "\n" + tail + "\n")
            text, off = rt.read_delta(path, 10)   # offset 이 big 라인 중간
            self.assertIn("short readable line", text)
            self.assertNotIn("x" * 200, text)


class NowLangTest(unittest.TestCase):
    """user 2026-07-20: "요약이 언제는 영어고 언제는 한글" — the NOW subtitle language is
    pinned per operator (env → non-en locale), not per transcript."""

    def _with_env(self, **env):
        from unittest import mock
        cleared = {k: None for k in ("FLEET_NOW_LANG", "LC_ALL", "LC_MESSAGES", "LANG")}
        cleared.update(env)
        patch = {k: v for k, v in cleared.items() if v is not None}
        drop = [k for k, v in cleared.items() if v is None]
        ctx = mock.patch.dict(os.environ, patch)
        ctx.start()
        for k in drop:
            os.environ.pop(k, None)
        self.addCleanup(ctx.stop)

    def test_env_override_wins(self):
        self._with_env(FLEET_NOW_LANG="Korean", LANG="en_US.UTF-8")
        self.assertEqual(rt._now_lang(), "Korean")
        self.assertIn("in Korean,", rt._prompt("d"))

    def test_non_english_locale_decides(self):
        self._with_env(LANG="ko_KR.UTF-8")
        self.assertEqual(rt._now_lang(), "Korean")

    def test_english_locale_is_no_signal_keeps_conversation_rule(self):
        self._with_env(LANG="en_US.UTF-8")
        self.assertEqual(rt._now_lang(), "")
        self.assertIn("in the conversation's own language,", rt._prompt("d"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
