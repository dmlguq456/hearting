#!/usr/bin/env python3
"""F-19 (메모리 관측 패널) — hermetic unit tests.

Runnable directly or via `python3 -m unittest fleet.tests.test_f19_memory -v` (from `tools/`).
"""
import datetime
import json
import os
import re
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

    def test_yesterday_events_stay_in_by_repo_but_not_in_today(self):
        # 2026-11-02 사용자 결정: by_repo shows each project's latest journal rows, not
        # only today's. The `today` aggregate keeps its midnight boundary.
        now = datetime.datetime(2026, 7, 16, 0, 1, 0)
        _write_lines(self.journal, [
            _event("2026-07-15T23:59:00", "add", cwd="/home/demo/agent_setting"),
        ])
        result = memcol.collect(now=now)
        self.assertEqual(len(result["by_repo"]["agent_setting"]), 1)
        self.assertEqual(result["by_repo"]["agent_setting"][0]["ts"], "2026-07-15T23:59:00")
        self.assertEqual(result["today"]["added"], 0)

    def test_by_repo_keeps_only_the_latest_k_per_repo(self):
        now = datetime.datetime(2026, 7, 31, 12, 0, 0)
        days = range(10, 10 + memcol.RECENT_LIMIT + 3)
        _write_lines(self.journal, [
            _event("2026-07-%02dT09:00:00" % day, "add", cwd="/home/demo/agent_setting")
            for day in days
        ] + [
            _event("2026-07-01T09:00:00", "add", cwd="/home/demo/other-repo"),
        ])
        result = memcol.collect(now=now)
        rows = result["by_repo"]["agent_setting"]
        self.assertEqual(len(rows), memcol.RECENT_LIMIT)
        stamps = [row["ts"] for row in rows]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(stamps[0], "2026-07-%02dT09:00:00" % (10 + memcol.RECENT_LIMIT + 2))
        self.assertEqual(len(result["by_repo"]["other-repo"]), 1)


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


class ProjectMemoryReadTest(_TmpStoreCase):
    """The Explorer's per-project DB summary: headline-only, read-only, per project not global."""

    COLUMNS = ("id", "tier", "scope", "cwd_origin", "type", "source", "tags", "links", "body",
               "headline", "artifact_refs", "status", "canonical_id", "superseded_by", "created",
               "updated", "delivery_state", "injection_flag")

    def _make_db(self, rows):
        db = self.store / "memory.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE records (%s)" % ", ".join(
            name + (" INTEGER" if name == "injection_flag" else " TEXT") for name in self.COLUMNS))
        defaults = {"tier": "durable", "scope": "project", "source": "manual", "tags": "[]",
                    "links": "[]", "body": "PRIVATE-BODY-SENTINEL", "artifact_refs": "[]",
                    "status": "active", "superseded_by": None, "delivery_state": "ordinary",
                    "injection_flag": 0}
        for row in rows:
            values = dict(defaults, **row)
            values.setdefault("canonical_id", values["id"])
            values.setdefault("created", values["updated"])
            con.execute("INSERT INTO records VALUES (%s)" % ", ".join("?" * len(self.COLUMNS)),
                        [values.get(name) for name in self.COLUMNS])
        con.commit()
        con.close()
        return db

    def test_database_extension_is_opt_in_and_default_journal_contract_is_unchanged(self):
        with mock.patch.object(memcol, "_read_project_memory",
                               side_effect=AssertionError("default collector must not query DB")):
            self.assertIsNone(memcol.collect(include_summary=False))

    def test_every_project_keeps_its_own_newest_decisions_without_reading_bodies(self):
        busy = "git:github.com/team/busy"
        rows = [{"id": "busy-%03d" % i, "cwd_origin": busy, "type": "decision",
                 "headline": "busy decision %d" % i, "updated": "2026-11-%02d" % (1 + i % 2)}
                for i in range(600)]
        rows += [
            {"id": "quiet-new", "cwd_origin": "git:github.com/team/quiet", "type": "decision",
             "headline": "quiet newest", "updated": "2026-08-10"},
            {"id": "quiet-old", "cwd_origin": "git:github.com/team/quiet", "type": "decision",
             "headline": "quiet older", "updated": "2026-08-01"},
            {"id": "quiet-gone", "cwd_origin": "git:github.com/team/quiet", "type": "decision",
             "headline": "quiet superseded", "updated": "2026-08-20", "status": "superseded"},
            {"id": "encoded", "cwd_origin": "id:5e3e0f2ec365009e", "type": "decision",
             "headline": "no project name", "updated": "2026-11-02"},
            {"id": "fact", "cwd_origin": "git:github.com/team/quiet", "type": "fact",
             "headline": "not read", "updated": "2026-11-02"},
        ]
        db = self._make_db(rows)
        before = db.read_bytes()

        database = memcol._read_project_memory("2026-11-02")

        self.assertEqual(database["state"], "success")
        self.assertEqual(set(database["by_repo"]), {"busy", "quiet"})
        quiet = database["by_repo"]["quiet"]
        self.assertEqual([item["headline"] for item in quiet["decisions"]], ["quiet newest", "quiet older"])
        self.assertEqual(quiet["open"], [])
        busy_rows = database["by_repo"]["busy"]
        self.assertEqual(len(busy_rows["decisions"]), memcol.DECISIONS_PER_PROJECT)
        self.assertTrue(all(item["updated"] == "2026-11-02" for item in busy_rows["decisions"]))
        self.assertEqual(busy_rows["decisions"][0]["id"], "busy-599")      # newest write first
        self.assertEqual(busy_rows["decisions_today"], 300)
        self.assertNotIn("PRIVATE-BODY-SENTINEL", repr(database))
        self.assertNotIn("not read", repr(database))
        self.assertEqual(db.read_bytes(), before)

    def test_open_candidates_carry_markers_tags_and_route_links(self):
        self._make_db([
            {"id": "ob", "cwd_origin": "git:github.com/team/repo", "type": "unresolved-obligation",
             "headline": "fix the repo view", "updated": "2026-10-30",
             "links": '["rt-0123456789abcdef"]'},
            {"id": "tagged", "cwd_origin": "git:github.com/team/repo", "type": "project",
             "headline": "tagged task", "updated": "2026-10-29", "tags": '["fleet","backlog"]'},
            {"id": "marked", "cwd_origin": "/home/demo/repo", "type": "project",
             "headline": "[진행 중] marked task", "updated": "2026-10-28"},
            {"id": "plain", "cwd_origin": "git:github.com/team/repo", "type": "project",
             "headline": "plain project fact", "updated": "2026-10-31"},
        ])
        open_items = memcol._read_project_memory("2026-11-02")["by_repo"]["repo"]["open"]
        self.assertEqual([item["id"] for item in open_items], ["ob", "tagged", "marked"])
        self.assertEqual(open_items[0]["links"], ("rt-0123456789abcdef",))
        self.assertIn("backlog", open_items[1]["tags"])

    def test_parity_register_rows_stay_open_hearting_work(self):
        self._make_db([{"id": "ob", "cwd_origin": "git:github.com/team/repo",
                        "type": "unresolved-obligation", "headline": "repo work",
                        "updated": "2026-10-30"}])
        home = self.store / "agent-home"
        (home / "core").mkdir(parents=True)
        (home / "core" / "ADAPTATION.md").write_text(
            "| PG-1 | outside | not in the register | open | - |\n"
            "<!-- parity-register:start -->\n"
            "| ID | Harness | Effect | State | Next |\n|---|---|---|---|---|\n"
            "| PG-1 | Codex | no wake on receipt | open | fix in hooks |\n"
            "| PG-2 | OpenCode | herdr only | needs-user-decision | ask |\n"
            "| PG-3 | Claude | fixed | resolved | - |\n"
            "| PG-4 | Codex | platform | limit | official docs (https://example.test/docs) |\n"
            "| PG-5 | Codex | typo | opne | - |\n"
            "| PG-6 | Codex | still fixable | limit | - |\n"
            "| PG-7 | Codex | still fixable | limit | official docs |\n"
            "<!-- parity-register:end -->\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENT_HOME": str(home)}):
            memcol._project_cache.clear()
            full = memcol._read_project_memory("2026-11-02")["by_repo"]
            scoped = memcol._read_project_memory("2026-11-02", repo="repo")["by_repo"]
            own = memcol._read_project_memory("2026-11-02", repo="hearting")["by_repo"]
        rows = full["hearting"]["open"]
        # Only resolved and a linked limit close a row; an unknown state (a typo) and a
        # limit whose Next carries no link stay open.
        self.assertEqual(sorted(row["id"] for row in rows),
                         ["parity:PG-1", "parity:PG-2", "parity:PG-5", "parity:PG-6", "parity:PG-7"])
        headlines = {row["id"]: row["headline"] for row in rows}
        self.assertTrue(headlines["parity:PG-1"].startswith("[미결] PG-1 Codex"))
        self.assertTrue(headlines["parity:PG-2"].startswith("[결정 필요] PG-2"))
        self.assertTrue(headlines["parity:PG-5"].startswith("[미결] PG-5"))
        self.assertTrue(headlines["parity:PG-6"].startswith("[limit 근거 없음] PG-6"))
        self.assertTrue(headlines["parity:PG-7"].startswith("[limit 근거 없음] PG-7"))
        self.assertIn("user-decision", {row["id"]: row for row in rows}["parity:PG-2"]["tags"])
        self.assertTrue(all(memcol.open_stage(row) == "open" for row in rows))
        self.assertEqual(full["hearting"]["open_total"], 5)
        self.assertNotIn("hearting", scoped)
        self.assertEqual(len(own["hearting"]["open"]), 5)
        self.assertEqual([row["id"] for row in full["repo"]["open"]], ["ob"])

    def test_shipped_parity_register_parses_with_known_states(self):
        """The real core/ADAPTATION.md register: rows exist and every state is a known value."""
        register = Path(__file__).resolve().parents[3] / "core" / "ADAPTATION.md"
        text = register.read_text(encoding="utf-8")
        body = text[text.index("<!-- parity-register:start -->"):text.index("<!-- parity-register:end -->")]
        states = {}
        for line in body.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 5 and re.fullmatch(r"PG-\d{1,4}", cells[0]):
                states[cells[0]] = (cells[3].strip("`").casefold(), cells[4])
        self.assertTrue(states)
        known = set(memcol._REGISTER_STATES) | set(memcol._REGISTER_CLOSED)
        self.assertEqual({k: v for k, (v, _) in states.items() if v not in known}, {})
        # Every shipped limit row links its evidence, so none is listed as unlinked.
        self.assertEqual({k for k, (v, n) in states.items()
                          if v == "limit" and not memcol.register_row_closed(v, n)}, set())
        listed = {row["id"] for row in memcol.parity_open_items(register)}
        self.assertEqual(listed, {f"parity:{k}" for k, (v, n) in states.items()
                                  if not memcol.register_row_closed(v, n)})

    def test_session_block_counts_only_the_records_the_repo_page_counts(self):
        origin = "git:github.com/team/repo"
        rows = [{"id": "work-%02d" % i, "cwd_origin": origin, "type": "backlog",
                 "headline": "work %d" % i, "updated": "2026-11-%02d" % (1 + i % 9)} for i in range(40)]
        rows += [
            {"id": "closed", "cwd_origin": origin, "type": "completion", "headline": "17 closed",
             "updated": "2026-11-09", "tags": '["backlog"]'},
            {"id": "tidy", "cwd_origin": origin, "type": "project", "headline": "tidy note",
             "updated": "2026-11-09", "tags": '["[\\"resolved\\"", "\\"backlog\\"]"]'},
            {"id": "seen", "cwd_origin": origin, "type": "observation", "headline": "[관찰] seen",
             "updated": "2026-11-09"},
            {"id": "fixed", "cwd_origin": origin, "type": "artifact-pointer",
             "headline": "[해결 확인] fixed", "updated": "2026-11-09", "tags": '["backlog"]'},
        ]
        self._make_db(rows)
        from fleet import session_work
        with mock.patch.object(session_work, "project_of", return_value="repo"):
            work, _paths = session_work.observe("/home/demo/repo")
        self.assertEqual(sorted(row["id"] for row in work), sorted("work-%02d" % i for i in range(40)))

    def test_open_work_behind_many_finished_records_is_not_hidden(self):
        origin = "git:github.com/team/repo"
        rows = [{"id": "done-%03d" % i, "cwd_origin": origin, "type": "completion",
                 "headline": "closed %d" % i, "updated": "2026-11-09", "tags": '["backlog"]'}
                for i in range(memcol.OPEN_PER_PROJECT)]
        rows.append({"id": "real", "cwd_origin": origin, "type": "unresolved-obligation",
                     "headline": "the real open item", "updated": "2026-10-01"})
        self._make_db(rows)
        from fleet import session_work
        with mock.patch.object(session_work, "project_of", return_value="repo"):
            work, _paths = session_work.observe("/home/demo/repo")
        self.assertEqual([row["id"] for row in work], ["real"])
        self.assertEqual(session_work.open_count(work), 1)

    def test_open_total_is_not_the_bounded_list_length(self):
        origin = "git:github.com/team/repo"
        count = memcol.OPEN_PER_PROJECT + 1
        self._make_db([{"id": "work-%03d" % i, "cwd_origin": origin, "type": "backlog",
                        "headline": "work %d" % i, "updated": "2026-11-%02d" % (1 + i % 9)}
                       for i in range(count)])
        database = memcol._read_project_memory("2026-11-10", decisions=0, repo="repo")
        project = database["by_repo"]["repo"]
        self.assertEqual(len(project["open"]), memcol.OPEN_PER_PROJECT)
        self.assertEqual(project["open_total"], count)
        self.assertTrue(project["limited"])
        from fleet import session_work
        with mock.patch.object(session_work, "project_of", return_value="repo"):
            work, _paths = session_work.observe("/home/demo/repo")
        self.assertEqual(len(work), memcol.OPEN_PER_PROJECT)
        self.assertEqual(session_work.open_count(work), count)
        self.assertNotIn("PRIVATE-BODY-SENTINEL", repr(database))

    def test_journal_rows_take_their_stored_headline(self):
        self._make_db([{"id": "rec-1", "cwd_origin": "git:github.com/team/repo", "type": "note",
                        "headline": "the stored one-liner", "updated": "2026-11-02"}])
        _write_lines(self.journal, [
            _event("2026-11-02T09:00:00", "add", snippet="2026-11-02 [aa](claude x): long body",
                   cwd="/home/demo/repo", id="rec-1"),
            _event("2026-11-02T09:01:00", "add", snippet="no record", cwd="/home/demo/repo",
                   id="rec-2"),
        ])
        result = memcol.collect(now=datetime.datetime(2026, 11, 2, 12, 0, 0),
                                include_summary=False, include_backlog=True)
        rows = {row["id"]: row for row in result["by_repo"]["repo"]}
        self.assertEqual(rows["rec-1"]["headline"], "the stored one-liner")
        self.assertNotIn("headline", rows["rec-2"])
        self.assertNotIn("headlines", result["database"])

    def test_selected_repo_reuses_the_same_projection_without_materializing_other_repos(self):
        self._make_db([
            {"id": "own", "cwd_origin": "git:github.com/team/repo", "type": "task",
             "headline": "own task", "updated": "2026-11-02"},
            {"id": "other", "cwd_origin": "git:github.com/team/other", "type": "task",
             "headline": "other task", "updated": "2026-11-02"},
        ])
        full = memcol._read_project_memory("2026-11-02")
        selected = memcol._read_project_memory("2026-11-02", repo="repo")
        self.assertEqual(selected["by_repo"], {"repo": full["by_repo"]["repo"]})
        with mock.patch.object(memcol.sqlite3, "connect", side_effect=AssertionError("unchanged scope must reuse")):
            self.assertEqual(memcol._read_project_memory("2026-11-02", repo="repo"), selected)

    def test_latest_saved_memory_reads_the_journal_once_and_rides_the_open_work_query(self):
        # Spec E9 rule 37 (b): the panel's MEM row is this repo's newest add/supersede journal
        # row; its stored headline comes from the open-work query the panel already runs (one
        # connection, as before), and an unchanged journal is not read again.
        self._make_db([{"id": "rec-new", "cwd_origin": "git:github.com/team/repo", "type": "decision",
                        "headline": "the newest decision", "updated": "2026-11-12"},
                       {"id": "rec-next", "cwd_origin": "git:github.com/team/repo", "type": "decision",
                        "headline": "the replacement", "updated": "2026-11-12"}])
        _write_lines(self.journal, [
            _event("2026-11-12T09:00:00", "add", snippet="older", cwd="/home/demo/repo", id="rec-old"),
            _event("2026-11-12T10:00:00", "add", rtype="decision", snippet="first line", cwd="/home/demo/repo", id="rec-new"),
            _event("2026-11-12T11:00:00", "lifecycle-expire", snippet="expired", cwd="/home/demo/repo", id="rec-x"),
            _event("2026-11-12T12:00:00", "add", snippet="other repo", cwd="/home/demo/other", id="rec-o"),
        ])
        from fleet import session_work
        memcol._latest_cache.clear()
        reads = mock.Mock(wraps=memcol._read_saved_journal)
        with mock.patch.object(memcol, "_read_saved_journal", reads):
            row, paths = session_work.latest_memory("/home/demo/repo")
            again, _paths = session_work.latest_memory("/home/demo/repo")
        self.assertEqual(reads.call_count, 1)
        self.assertEqual((row["id"], row["type"], row["action"], row["headline_id"]),
                         ("rec-new", "decision", "add", "rec-new"))
        self.assertEqual(again, row)
        self.assertEqual(paths, [str(self.journal)])
        connects = mock.Mock(wraps=memcol.sqlite3.connect)
        with mock.patch.object(session_work, "project_of", return_value="repo"), \
                mock.patch.object(memcol.sqlite3, "connect", connects):
            work, _paths = session_work.observe("/home/demo/repo", (row["headline_id"],))
        self.assertEqual(connects.call_count, 1)
        self.assertEqual(work.headlines.get("rec-new"), "the newest decision")
        self.assertNotIn("PRIVATE-BODY-SENTINEL", repr(work.headlines))
        # A changed journal is read again; a supersede asks for its replacement's headline.
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(_event("2026-11-12T13:00:00", "supersede", snippet="superseded_by=rec-next",
                                cwd="/home/demo/repo", id="rec-new") + "\n")
        row, _paths = session_work.latest_memory("/home/demo/repo")
        self.assertEqual((row["action"], row["headline_id"]), ("supersede", "rec-next"))
        # Another repo's newest row, none for a repo without one, none without a journal.
        self.assertEqual(memcol.latest_saved("other")[0]["id"], "rec-o")
        self.assertIsNone(memcol.latest_saved("nothing-here")[0])
        self.journal.unlink()
        self.assertIsNone(session_work.latest_memory("/home/demo/repo")[0])

    def test_latest_saved_asks_each_cwds_repository_once_per_read(self):
        # The journal is shared by every repository and every panel reads it again on any
        # write; a cwd's key walks the directories up to `.git` (a stat each, slow on NFS),
        # so one read asks it once per cwd, not once per row.
        cwds = ["/home/demo/repo/src", "/home/demo/other"]
        _write_lines(self.journal, [_event("2026-11-12T10:%02d:00" % i, "add", cwd=cwds[i % 2], id="rec-%d" % i)
                                    for i in range(40)] + [_event("2026-11-12T23:00:00", "add", project="literal", id="rec-p")])
        from fleet import gitinfo
        memcol._latest_cache.clear()
        asks = mock.Mock(wraps=gitinfo.resolve_gitdir)
        with mock.patch.object(gitinfo, "resolve_gitdir", asks):
            row, _path = memcol.latest_saved("src")
        self.assertEqual(sorted(call.args[0] for call in asks.call_args_list), sorted(cwds))
        self.assertEqual(row["id"], "rec-38")
        self.assertEqual(memcol.latest_saved("other")[0]["id"], "rec-39")
        self.assertEqual(memcol.latest_saved("literal")[0]["id"], "rec-p")

    def test_a_broken_byte_in_the_journal_costs_only_its_own_line(self):
        # A writer that died mid-line leaves undecodable bytes in the journal every panel
        # reads; the other rows still answer, and nothing raises into the projection.
        def row(ts, snippet, rid):   # as mem.py writes it: UTF-8, not \u escapes
            return json.dumps(json.loads(_event(ts, "add", snippet=snippet, cwd="/home/demo/repo", id=rid)),
                              ensure_ascii=False).encode()
        older, newer = row("2026-11-12T09:00:00", "앞 기록", "rec-old"), row("2026-11-12T10:00:00", "뒤 기록", "rec-new")
        cut = row("2026-11-12T11:00:00", "한글 기록", "rec-cut")
        cut = cut[:cut.index("한".encode()) + 1]   # ends inside a three-byte character
        from fleet import session_work
        for name, data, newest in (
                ("the last line cut inside a character", older + b"\n" + cut, "rec-old"),
                ("a broken line in the middle", older + b"\n\xff\xfe" + cut + b"\n" + newer + b"\n", "rec-new"),
                ("a broken byte inside a value", older + b"\n" + newer.replace("뒤".encode(), b"\xff") + b"\n", "rec-new")):
            with self.subTest(name):
                self.journal.write_bytes(data)
                memcol._latest_cache.clear()
                row, paths = session_work.latest_memory("/home/demo/repo")
                self.assertEqual(paths, [str(self.journal)])
                self.assertEqual(row["id"], newest)

    def test_unchanged_database_is_not_queried_again(self):
        self._make_db([{"id": "d", "cwd_origin": "git:github.com/team/repo", "type": "decision",
                        "headline": "kept", "updated": "2026-11-02"}])
        first = memcol._read_project_memory("2026-11-02")
        with mock.patch.object(memcol.sqlite3, "connect",
                               side_effect=AssertionError("cached result must be reused")):
            again = memcol._read_project_memory("2026-11-02")
        self.assertEqual(again["by_repo"], first["by_repo"])

    def test_database_unavailable_is_not_reported_as_empty(self):
        result = memcol.collect(include_summary=False, include_backlog=True)
        self.assertEqual(result["database"]["state"], "unavailable")
        self.assertIsNone(result["database"]["record_count"])

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

    def test_summary_row_is_removed(self):
        lines = render._build_lines([], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide", memory=self._snapshot())
        text = self._text(lines)
        self.assertNotIn("+4 added(3w·1d)", text)
        self.assertNotIn("2 expired", text)
        self.assertNotIn("1 pruned", text)
        self.assertNotIn("last distill 45m", text)

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

    def test_durable_over_is_not_rendered_in_the_header(self):
        snap = self._snapshot(alerts={"durable_over": [["proj-a", 85]], "distill_stale": False})
        lines = render._build_lines([], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide", memory=snap)
        text = self._text(lines)
        self.assertNotIn("durable 1 over", text)
        self.assertNotIn("  alert ", text)

    def test_distill_stale_is_not_rendered_in_the_header(self):
        snap = self._snapshot(alerts={"durable_over": [], "distill_stale": True})
        lines = render._build_lines([], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide", memory=snap)
        text = self._text(lines)
        self.assertNotIn("distill stale", text)
        self.assertNotIn("  alert ", text)


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
