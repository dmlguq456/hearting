#!/usr/bin/env python3
"""F-19 (메모리 관측 패널) — hermetic unit tests.

Runnable directly or via `python3 -m unittest fleet.tests.test_f19_memory -v` (from `tools/`).
"""
import datetime
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render                        # noqa: E402
from fleet.collectors import memory as memcol   # noqa: E402


def _write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")


class _TmpStoreCase(unittest.TestCase):
    """Isolates every test behind its own MEM_STORE/MEM_WRITE_EVENTS — never touches the
    real ~/.local/state or ~/hearting journal (mirrors the 2026-07-11 실유출 회귀 fix)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "memory"
        self.store.mkdir(parents=True)
        self._env = mock.patch.dict(os.environ, {
            "MEM_STORE": str(self.store),
            "MEM_WRITE_EVENTS": str(self.store / "write-events.jsonl"),
        }, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    @property
    def journal(self):
        return self.store / "write-events.jsonl"

    @property
    def graveyard(self):
        return self.store / "deleted-records.jsonl"


def _event(ts, action, tier="working", rtype="fact", actor="manual", snippet="x", **extra):
    d = {"ts": ts, "action": action, "id": "id1", "tier": tier, "scope": "project",
         "type": rtype, "actor": actor, "sid": "", "snippet": snippet}
    d.update(extra)
    return json.dumps(d)


def _grave(deleted_at, action):
    return json.dumps({"id": "id1", "tier": "working", "scope": "project", "type": "fact",
                        "body": "x", "_deleted_at": deleted_at, "_action": action,
                        "_canonical": None})


class JournalParseTest(_TmpStoreCase):
    def test_absent_files_return_none(self):
        self.assertIsNone(memcol.collect())

    def test_journal_only_aggregates_today(self):
        now = datetime.datetime(2026, 7, 11, 18, 0, 0)
        _write_lines(self.journal, [
            _event("2026-07-11T09:00:00", "add", tier="working"),
            _event("2026-07-11T09:05:00", "add", tier="durable"),
            _event("2026-07-11T09:06:00", "note", tier="working"),
            _event("2026-07-11T10:00:00", "lifecycle-expire"),
            _event("2026-07-11T11:00:00", "prune"),
            _event("2026-07-10T23:00:00", "add", tier="working"),  # yesterday — excluded
            _event("2026-07-11T12:00:00", "add", tier="working", actor="distiller"),
        ])
        result = memcol.collect(now=now)
        self.assertIsNotNone(result)
        today = result["today"]
        self.assertEqual(today["added_working"], 3)
        self.assertEqual(today["added_durable"], 1)
        self.assertEqual(today["added"], 4)
        self.assertEqual(today["expired"], 1)
        self.assertEqual(today["pruned"], 1)
        self.assertEqual(result["last_distill_min"], 360)   # 18:00 - 12:00 = 6h

    def test_malformed_lines_are_skipped(self):
        now = datetime.datetime(2026, 7, 11, 12, 0, 0)
        _write_lines(self.journal, [
            "{not json",
            _event("2026-07-11T09:00:00", "add"),
            "",
            "null",
            "42",
        ])
        result = memcol.collect(now=now)
        self.assertEqual(result["today"]["added"], 1)

    def test_journal_absent_graveyard_only_degrades(self):
        now = datetime.datetime(2026, 7, 11, 12, 0, 0)
        _write_lines(self.graveyard, [
            _grave("2026-07-11T09:00:00", "prune"),
            _grave("2026-07-11T10:00:00", "lifecycle-expire"),
            _grave("2026-07-10T09:00:00", "prune"),  # yesterday — excluded
        ])
        result = memcol.collect(now=now)
        self.assertIsNotNone(result)
        self.assertFalse(result["journal_available"])
        self.assertTrue(result["graveyard_available"])
        self.assertEqual(result["today"]["added"], 0)   # unknowable without the journal
        self.assertEqual(result["today"]["expired"], 1)
        self.assertEqual(result["today"]["pruned"], 1)
        self.assertIsNone(result["last_distill_min"])   # actor not in graveyard rows

    def test_recent_events_newest_first_capped_at_8(self):
        now = datetime.datetime(2026, 7, 11, 12, 0, 0)
        _write_lines(self.journal, [_event("2026-07-11T%02d:00:00" % h, "add") for h in range(10)])
        result = memcol.collect(now=now)
        self.assertEqual(len(result["recent"]), 8)
        self.assertEqual(result["recent"][0]["ts"], "2026-07-11T09:00:00")   # last written = newest

    def test_distill_stale_flags_when_over_threshold(self):
        now = datetime.datetime(2026, 7, 12, 12, 0, 0)
        _write_lines(self.journal, [_event("2026-07-11T00:00:00", "add", actor="distiller")])
        result = memcol.collect(now=now)
        self.assertTrue(result["alerts"]["distill_stale"])

    def test_distill_not_stale_within_threshold(self):
        now = datetime.datetime(2026, 7, 11, 12, 0, 0)
        _write_lines(self.journal, [_event("2026-07-11T09:00:00", "add", actor="distiller")])
        result = memcol.collect(now=now)
        self.assertFalse(result["alerts"]["distill_stale"])


class RepoGroupingTest(_TmpStoreCase):
    """F-19 repo extension (2026-07-16) — additive `by_repo`, honest-omission when the
    journal carries neither `cwd` nor `project`."""

    def test_no_cwd_or_project_field_yields_empty_by_repo(self):
        now = datetime.datetime(2026, 7, 16, 12, 0, 0)
        _write_lines(self.journal, [_event("2026-07-16T09:00:00", "add")])
        result = memcol.collect(now=now)
        self.assertEqual(result["by_repo"], {}, "추측 금지 — 필드 부재는 정직 생략")

    def test_cwd_field_groups_via_project_of(self):
        now = datetime.datetime(2026, 7, 16, 12, 0, 0)
        _write_lines(self.journal, [
            _event("2026-07-16T09:00:00", "add", cwd="/home/demo/agent_setting"),
            _event("2026-07-16T09:05:00", "lifecycle-expire", cwd="/home/demo/agent_setting"),
            _event("2026-07-16T09:10:00", "add", cwd="/home/demo/worklog-board"),
        ])
        result = memcol.collect(now=now)
        self.assertEqual(set(result["by_repo"].keys()), {"agent_setting", "worklog-board"})
        self.assertEqual(len(result["by_repo"]["agent_setting"]), 2)
        # most-recent-first within a repo
        self.assertEqual(result["by_repo"]["agent_setting"][0]["action"], "lifecycle-expire")

    def test_literal_project_field_is_used_directly(self):
        now = datetime.datetime(2026, 7, 16, 12, 0, 0)
        _write_lines(self.journal, [_event("2026-07-16T09:00:00", "add", project="myrepo")])
        result = memcol.collect(now=now)
        self.assertEqual(set(result["by_repo"].keys()), {"myrepo"})

    def test_yesterday_events_excluded_from_by_repo(self):
        now = datetime.datetime(2026, 7, 16, 12, 0, 0)
        _write_lines(self.journal, [
            _event("2026-07-15T09:00:00", "add", cwd="/home/demo/agent_setting"),
        ])
        result = memcol.collect(now=now)
        self.assertEqual(result["by_repo"], {})


class DurableOverTest(_TmpStoreCase):
    def _make_db(self, rows):
        db = self.store / "memory.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE records (id TEXT, tier TEXT, scope TEXT, cwd_origin TEXT)")
        con.executemany("INSERT INTO records VALUES (?,?,?,?)", rows)
        con.commit()
        con.close()

    def test_over_ceiling_project_surfaces(self):
        rows = [("id%d" % i, "durable", "project", "proj-a") for i in range(85)]
        rows += [("id%d" % i, "durable", "project", "proj-b") for i in range(5)]
        self._make_db(rows)
        _write_lines(self.journal, [_event("2026-07-11T09:00:00", "add")])
        result = memcol.collect(now=datetime.datetime(2026, 7, 11, 12, 0, 0))
        over = dict(result["alerts"]["durable_over"])
        self.assertEqual(over.get("proj-a"), 85)
        self.assertNotIn("proj-b", over)

    def test_db_absent_degrades_to_empty(self):
        _write_lines(self.journal, [_event("2026-07-11T09:00:00", "add")])
        result = memcol.collect(now=datetime.datetime(2026, 7, 11, 12, 0, 0))
        self.assertEqual(result["alerts"]["durable_over"], [])

    def test_corrupt_db_degrades_to_empty(self):
        (self.store / "memory.db").write_text("not a sqlite file", encoding="utf-8")
        _write_lines(self.journal, [_event("2026-07-11T09:00:00", "add")])
        result = memcol.collect(now=datetime.datetime(2026, 7, 11, 12, 0, 0))
        self.assertEqual(result["alerts"]["durable_over"], [])


class RenderIntegrationTest(unittest.TestCase):
    """render._build_lines consumes the F-19 `memory` snapshot — purely in-memory, no I/O."""

    def tearDown(self):
        render.set_show_all(False)

    def _text(self, lines):
        return "\n".join("".join(t for t, _k in ln) for ln in lines if ln)

    def _snapshot(self, **over):
        base = {
            "journal_available": True, "graveyard_available": True,
            "today": {"added_working": 3, "added_durable": 1, "added": 4,
                      "expired": 2, "pruned": 1},
            "last_distill_min": 45,
            "recent": [{"ts": "2026-07-11T09:00:00", "action": "add", "tier": "working",
                       "type": "fact", "actor": "manual", "sid": "", "snippet": "hello"}],
            "alerts": {"durable_over": [], "distill_stale": False},
        }
        base.update(over)
        return base

    def test_none_memory_omits_row_and_never_raises(self):
        lines = render._build_lines([], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide", memory=None)
        self.assertNotIn("mem  +", self._text(lines))

    def test_healthy_zero_event_no_alert_row_omitted(self):
        snap = self._snapshot(today={"added_working": 0, "added_durable": 0, "added": 0,
                                      "expired": 0, "pruned": 0})
        lines = render._build_lines([], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide", memory=snap)
        self.assertNotIn("mem  +", self._text(lines))

    def test_summary_row_renders_counts(self):
        lines = render._build_lines([], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide", memory=self._snapshot())
        text = self._text(lines)
        self.assertIn("+4 added(3w·1d)", text)
        self.assertIn("2 expired", text)
        self.assertIn("1 pruned", text)
        self.assertIn("last distill 45m", text)

    def test_a_toggle_reveals_recent_events(self):
        render.set_show_all(True)
        lines = render._build_lines([], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide", memory=self._snapshot())
        text = self._text(lines)
        self.assertIn("hello", text)

    def test_a_toggle_off_hides_recent_events(self):
        render.set_show_all(False)
        lines = render._build_lines([], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide", memory=self._snapshot())
        self.assertNotIn("hello", self._text(lines))

    def test_durable_over_alert_bucket_appears(self):
        snap = self._snapshot(alerts={"durable_over": [["proj-a", 85]], "distill_stale": False})
        lines = render._build_lines([], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide", memory=snap)
        text = self._text(lines)
        self.assertIn("durable-over", text)
        self.assertIn("proj-a=85", text)

    def test_distill_stale_alert_bucket_appears(self):
        snap = self._snapshot(alerts={"durable_over": [], "distill_stale": True})
        lines = render._build_lines([], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide", memory=snap)
        self.assertIn("distill stale", self._text(lines))


class RepoRowsRenderTest(unittest.TestCase):
    """F-19 repo rows (2026-07-16) — per-group card bottom divider + today mem events."""

    def tearDown(self):
        render.set_show_all(False)

    def _text(self, lines):
        return "\n".join("".join(t for t, _k in ln) for ln in lines if ln)

    def _session(self, cwd="/home/demo/agent_setting", sid="s1",
                title="fleet UI two-plane demo"):
        from fleet.model import Session
        return Session(harness="claude", pid=1, cwd=cwd, session_id=sid, slug="x",
                       title=title, liveness="idle")

    def _memory(self, by_repo):
        return {
            "journal_available": True, "graveyard_available": True,
            "today": {"added_working": 0, "added_durable": 0, "added": 0,
                      "expired": 0, "pruned": 0},
            "last_distill_min": None, "recent": [],
            "by_repo": by_repo,
            "alerts": {"durable_over": [], "distill_stale": False},
        }

    def test_repo_with_events_shows_divider_and_rows(self):
        memory = self._memory({"agent_setting": [
            {"ts": "2026-07-16T14:02:00", "action": "add", "tier": "durable",
             "type": "project", "actor": "distiller", "sid": "s1", "snippet": "hello world"},
        ]})
        lines = render._build_lines([self._session()], [], section="fleet", narrow=False,
                                    malformed=0, layout="wide", memory=memory, term_width=168)
        text = self._text(lines)
        self.assertIn("hello world", text)
        self.assertIn("fleet UI two-plane demo", text)   # sid resolved to source session title
        self.assertIn("14:02", text)
        self.assertIn("─", text)   # in-band divider

    def test_repo_without_events_is_completely_silent(self):
        memory = self._memory({})
        lines = render._build_lines([self._session()], [], section="fleet", narrow=False,
                                    malformed=0, layout="wide", memory=memory, term_width=168)
        self.assertNotIn("🧠", self._text(lines))

    def test_unresolvable_sid_omits_title_not_the_row(self):
        memory = self._memory({"agent_setting": [
            {"ts": "2026-07-16T13:47:00", "action": "prune", "tier": "working",
             "type": "expired", "actor": "curator", "sid": "unknown-sid", "snippet": "gone"},
        ]})
        lines = render._build_lines([self._session()], [], section="fleet", narrow=False,
                                    malformed=0, layout="wide", memory=memory, term_width=168)
        text = self._text(lines)
        self.assertIn("gone", text)
        self.assertNotIn("⟵", text, "resolve 불가한 sid는 출처 태그 자체를 생략")

    def test_add_action_sign_is_green_expire_falls_back_to_dim(self):
        rows = render._mem_repo_rows([
            {"ts": "2026-07-16T14:02:00", "action": "add", "tier": "durable",
             "type": "project", "actor": "distiller", "sid": None, "snippet": "a"},
            {"ts": "2026-07-16T13:47:00", "action": "lifecycle-expire", "tier": "working",
             "type": "fact", "actor": "curator", "sid": None, "snippet": "b"},
        ], {})
        add_sign = next(k for t, k in rows[0] if t == "+")
        expire_sign = next(k for t, k in rows[1] if t == "−")
        self.assertEqual(add_sign, "lvl_g")
        self.assertEqual(expire_sign, "dim")

    def test_other_groups_repo_events_do_not_leak_into_this_card(self):
        memory = self._memory({"worklog-board": [
            {"ts": "2026-07-16T14:02:00", "action": "add", "tier": "working",
             "type": "fact", "actor": "manual", "sid": None, "snippet": "wrong card"},
        ]})
        lines = render._build_lines([self._session()], [], section="fleet", narrow=False,
                                    malformed=0, layout="wide", memory=memory, term_width=168)
        self.assertNotIn("wrong card", self._text(lines))


if __name__ == "__main__":
    unittest.main()
