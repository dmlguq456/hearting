#!/usr/bin/env python3
"""F-14 — session display title (harness title/ai-title → render name zone, slug fallback).
Display layer only: Session.slug is never overwritten, title is additive. Hermetic — no
real filesystem/process access; transcript reads use tempfile-backed .jsonl fixtures.
"""
import json
import os
import sys
import tempfile
import unittest

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render               # noqa: E402
from fleet.model import Session        # noqa: E402
from fleet.collectors import claude    # noqa: E402


class ClipWTest(unittest.TestCase):

    def test_clip_w_ascii_tailcut(self):
        out = render._clip_w("abcdefgh", 5)
        self.assertTrue("abcdefgh".startswith(out[:-1]))    # head preserved
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(render._dw(out), 5)

    def test_clip_w_hangul_boundary(self):
        s = "한글가나다라마바사"
        out = render._clip_w(s, 6)
        self.assertLessEqual(render._dw(out), 6)
        # no half-cell cut: every char in the result must be one that appeared whole in s
        for ch in out.rstrip("…"):
            self.assertIn(ch, s)

    def test_clip_w_noop_under_limit(self):
        self.assertEqual(render._clip_w("abc", 10), "abc")


class SessionRowTitleTest(unittest.TestCase):

    def _row_text_and_width(self, sess):
        segs = render._session_row(sess, narrow=False)
        # name zone = everything before the branch segment; reconstruct from the first
        # run of segments up to (and including) the trailing pad, which _session_row
        # always appends right after the name zone.
        text = "".join(t for t, _k in segs if isinstance(t, str))
        return text, segs

    def test_session_row_title_present_shows_title_not_slug(self):
        sess = Session(harness="claude", pid=1, cwd="",
                        slug="repo-ab12cd34", title="개발 서버 시작", liveness="idle")
        text, _segs = self._row_text_and_width(sess)
        self.assertIn("개발 서버 시작", text)
        self.assertNotIn("repo-ab12cd34", text)

    def test_session_row_title_absent_falls_back_to_slug(self):
        sess = Session(harness="claude", pid=1, cwd="",
                        slug="repo-ab12cd34", title=None, liveness="idle")
        text, _segs = self._row_text_and_width(sess)
        self.assertIn("repo-ab12cd34", text)

    def test_session_row_name_zone_width_is_display_width_aligned(self):
        # Hangul title (2-cell chars) must not overrun the shared name-zone width — the
        # segment immediately after the name zone (branch) should still start at the
        # expected column when measured in display cells, not char count.
        sess = Session(harness="claude", pid=1, cwd="",
                        slug="s", title="한글제목입니다한글제목입니다한글제목입니다",
                        liveness="idle")
        segs = render._session_row(sess, narrow=False)
        # Select by segment KEY — index 4 is the harness-field padding, not the name
        # (it merely happened to satisfy this bound while _HMW was 33).
        name_seg = next(t for t, k in segs
                        if k in render.NAME_KEYS)
        self.assertLessEqual(render._dw(name_seg), render._NW_S - 1)

    def test_session_row_2line_title_clipped_to_name2_max(self):
        long_title = "가" * 60     # 60 * 2 = 120 cells, far over _NAME2_MAX
        sess = Session(harness="claude", pid=1, cwd="",
                        slug="s", title=long_title, liveness="idle")
        l1 = render._session_row_2line(sess)[0]
        name_seg = next(t for t, k in l1 if k in render.NAME_KEYS)
        self.assertLessEqual(render._dw(name_seg), render._NAME2_MAX)


class SessionToDictAdditiveTest(unittest.TestCase):

    def test_json_title_additive_slug_unchanged(self):
        sess = Session(harness="claude", pid=1, slug="repo-ab12cd34", title="제목")
        d = sess.to_dict()
        self.assertEqual(d["title"], "제목")
        self.assertEqual(d["slug"], "repo-ab12cd34")

    def test_json_title_defaults_to_none(self):
        sess = Session(harness="claude", pid=1, slug="repo-ab12cd34")
        d = sess.to_dict()
        self.assertIsNone(d["title"])


class TailAiTitleTest(unittest.TestCase):

    def _write(self, tmp, name, lines):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        return path

    def test_tail_ai_title_picks_last_of_several(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.jsonl", [
                {"type": "ai-title", "aiTitle": "First title", "sessionId": "a"},
                {"type": "user", "message": "hi"},
                {"type": "ai-title", "aiTitle": "Second title", "sessionId": "b"},
            ])
            self.assertEqual(claude._tail_ai_title(path), "Second title")

    def test_tail_ai_title_filters_new_session_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.jsonl", [
                {"type": "ai-title", "aiTitle": "New session - 2026-07-10T10:00:00Z"},
            ])
            self.assertIsNone(claude._tail_ai_title(path))

    def test_tail_ai_title_tolerant_of_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"type":"ai-title","aiTitle":"Good title"\n')     # truncated/broken json
                f.write('not even json at all\n')
                f.write(json.dumps({"type": "ai-title", "aiTitle": "Good title"}) + "\n")
            self.assertEqual(claude._tail_ai_title(path), "Good title")

    def test_tail_ai_title_absent_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.jsonl", [{"type": "user", "message": "hi"}])
            self.assertIsNone(claude._tail_ai_title(path))

    def test_tail_ai_title_beyond_first_window_is_found(self):
        # 2026-07-10 실측 회귀: 긴 세션은 제목 뒤로 수십 KB 의 메시지가 쌓여
        # 고정 8KB tail 윈도우가 제목을 놓쳤다 — growing backward scan 으로 도달해야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            lines = [{"type": "ai-title", "aiTitle": "Early title", "sessionId": "a"}]
            filler = "x" * 200
            lines += [{"type": "assistant", "message": filler} for _ in range(400)]  # ~80KB tail
            path = self._write(tmp, "t.jsonl", lines)
            self.assertGreater(os.path.getsize(path), 8192 * 8)
            self.assertEqual(claude._tail_ai_title(path), "Early title")

    def test_tail_ai_title_junk_beyond_window_stops_scan(self):
        # 마지막 제목이 placeholder 면 (윈도우 확장으로 그 줄을 찾았더라도) None fallback.
        with tempfile.TemporaryDirectory() as tmp:
            lines = [{"type": "ai-title", "aiTitle": "New session - 2026-07-10T10:00:00Z"}]
            lines += [{"type": "assistant", "message": "x" * 200} for _ in range(100)]
            path = self._write(tmp, "t.jsonl", lines)
            self.assertIsNone(claude._tail_ai_title(path))

    def test_tail_ai_title_cache_invalidated_on_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.jsonl", [
                {"type": "ai-title", "aiTitle": "First title", "sessionId": "a"},
            ])
            self.assertEqual(claude._tail_ai_title(path), "First title")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "ai-title", "aiTitle": "Renamed title"}) + "\n")
            self.assertEqual(claude._tail_ai_title(path), "Renamed title")


if __name__ == "__main__":
    unittest.main(verbosity=2)
