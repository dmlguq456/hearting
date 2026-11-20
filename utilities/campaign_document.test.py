"""The campaign document generator: joining rules, sections, writing rules and the readers' view of it."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import artifact_entrance as entrance  # noqa: E402
import campaign_document as cd  # noqa: E402
from tools.fleet.collectors import project_status  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC).timestamp()
HOUR = 3600
WAITS_OK = {"status": "ok", "at": NOW - 60, "items": []}


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class PureTest(unittest.TestCase):
    def test_a_slug_names_one_pr_and_a_date_is_not_a_pr(self):
        for slug, expected in [("rv-1726-unit", 1726), ("fix-pr1555-x", 1555), ("review-1655-claude-1115", 1655),
                               ("campaign-doc-writer-1120", None), ("x-1117", None), ("a-1234-b-5678", None),
                               ("fix-14cf1419e7-1726", 1726), ("pr1117-x", 1117), ("big-20261120-x", None),
                               ("a-123-b", 123), ("", None), (None, None)]:
            with self.subTest(slug=slug):
                self.assertEqual(cd.pr_from_slug(slug), expected)

    def test_a_report_names_a_pr_only_in_its_first_thirty_lines_and_only_one(self):
        self.assertEqual(cd.pr_from_report("# r\n\nPR #1726 opened"), 1726)
        self.assertEqual(cd.pr_from_report("PR: #1726\n"), 1726)
        self.assertEqual(cd.pr_from_report("PR：#1726\n"), 1726)
        self.assertEqual(cd.pr_from_report("PR #5\nPR #5 again\n"), 5)
        self.assertIsNone(cd.pr_from_report("PR #5\nPR #6\n"))
        self.assertIsNone(cd.pr_from_report("\n" * 30 + "PR #5\n"))
        self.assertEqual(cd.pr_from_report("\n" * 29 + "PR #5\n"), 5)
        self.assertIsNone(cd.pr_from_report(""))

    def test_a_publish_report_gives_its_version_and_the_prs_it_names(self):
        text = "# v4.62.3 — #1755 fix thing\n\n병합된 PR: #1750, #1751\n\nother #9\n"
        self.assertEqual(cd.publish_facts(text), ("v4.62.3", [1755, 1750, 1751]))
        self.assertEqual(cd.publish_facts("no heading\n병합된 PR: #7"), (None, [7]))
        self.assertEqual(cd.publish_facts(""), (None, []))

    def test_route_kinds(self):
        for capability, slug, kind in [("route-frame", "x", "frame"), ("autopilot-ship", "x", "publish"),
                                       ("autopilot-code", "publish-v4", "publish"), ("autopilot-code", "rv-1-unit", "review"),
                                       ("autopilot-code", "x-close-unit", "review"), ("audit", "x", "review"),
                                       ("autopilot-code", "work-1720", "work")]:
            with self.subTest(slug=slug):
                self.assertEqual(cd.route_kind(capability, slug), kind)

    def fact(self, rid, **kw):
        base = {"rid": rid, "kind": "work", "slug": rid, "cy": "cyc_" + rid, "st": 1.0, "c": NOW - HOUR, "ok": True,
                "pr": None, "prs": [], "v": None, "t": None}
        return {**base, **kw}

    def test_routes_of_one_pr_make_one_line_and_a_route_without_a_number_its_own(self):
        facts = [self.fact("a", pr=7, st=3, c=NOW - 3 * HOUR), self.fact("b", pr=7, st=1, c=NOW - 2 * HOUR, kind="review"),
                 self.fact("c", pr=7, st=2, c=NOW - HOUR), self.fact("d", c=NOW - 5 * HOUR)]
        units = cd.link_units(facts, [], {"cyc_c": "Impl title", "cyc_d": "Alone"}, now=NOW)
        self.assertEqual([(u["key"], u["count"], u["title"]) for u in units["done"]],
                         [("pr:7", 3, "Impl title"), ("rt:d", 1, "Alone")])
        self.assertEqual(units["done_folded"], 0)

    def test_frames_and_unproven_closes_are_neither_lines_nor_counts(self):
        facts = [self.fact("a", pr=7), self.fact("f", kind="frame", pr=7), self.fact("u", pr=7, ok=False)]
        unit = cd.link_units(facts, [], {}, now=NOW)["done"][0]
        self.assertEqual(unit["count"], 1)
        self.assertEqual(cd.link_units([self.fact("u", ok=False)], [], {}, now=NOW)["done"], [])

    def test_a_publish_route_adds_its_version_and_a_record_but_no_line(self):
        facts = [self.fact("a", pr=9, c=NOW - 3 * HOUR),
                 self.fact("p1", kind="publish", prs=[9], v="v1.0.0", c=NOW - 2 * HOUR),
                 self.fact("p2", kind="publish", prs=[9, 10], v="v1.0.1", c=NOW - HOUR)]
        done = cd.link_units(facts, [], {}, now=NOW)["done"]
        self.assertEqual([(u["key"], u["v"], u["count"]) for u in done], [("pr:9", "v1.0.1", 3)])

    def open_route(self, rid, activity, **kw):
        return {"rid": rid, "kind": "work", "slug": rid, "cy": "cyc_" + rid, "st": 1.0, "pr": None, "activity": activity, **kw}

    def test_doing_is_six_hours_of_movement_and_the_rest_is_a_count(self):
        units = cd.link_units([], [self.open_route("a", NOW - (6 * HOUR - 60)), self.open_route("b", NOW - (6 * HOUR + 60)),
                                   self.open_route("c", NOW - 7 * HOUR)], {}, now=NOW)
        self.assertEqual([u["key"] for u in units["active"]], ["rt:a"])
        self.assertEqual(units["active_folded"], 2)

    def test_a_piece_of_work_with_an_open_route_is_doing_even_with_closed_members(self):
        units = cd.link_units([self.fact("a", pr=7)], [self.open_route("b", NOW - HOUR, pr=7)], {}, now=NOW)
        self.assertEqual([(u["key"], u["count"]) for u in units["active"]], [("pr:7", 2)])
        self.assertEqual(units["done"], [])

    def test_twenty_lines_of_done_and_a_count_of_the_rest(self):
        facts = [self.fact(f"r{i:02d}", c=NOW - i * 60) for i in range(21)]
        units = cd.link_units(facts, [], {}, now=NOW, closed_total=1500)
        self.assertEqual(len(units["done"]), 20)
        self.assertEqual(units["done"][0]["key"], "rt:r00")
        self.assertEqual(units["done_folded"], 1480)

    def render(self, units=None, waits=WAITS_OK, title="Demo", summary="One line."):
        units = units or {"active": [], "active_folded": 0, "done": [], "done_folded": 0}
        return cd.render_document(title, summary, units, waits, now=NOW, tz=UTC)

    def test_the_document_has_the_agreed_shape_and_no_forbidden_words(self):
        unit = {"key": "pr:7", "pr": 7, "v": "v4.1.0", "title": "Do a thing", "impl": {"slug": "x"}, "count": 3,
                "closed_at": NOW - HOUR, "routes": 2, "activity": 0}
        text = self.render({"active": [unit], "active_folded": 2, "done": [unit], "done_folded": 1354},
                           {"status": "ok", "at": NOW - 120, "items": ["Which one?"]})
        lines = text.split("\n")
        self.assertEqual(lines[0], cd.MARKER)
        self.assertEqual(lines[1], "# Demo")
        self.assertEqual(lines[3], "갱신 2026-11-20 12:00")
        self.assertEqual(lines[5], "**한 줄**: One line.")
        headings = [line for line in lines if line.startswith("## ")]
        self.assertEqual(headings, ["## 하는 중", "## 기다리는 것", "## 최근에 끝난 것"])
        self.assertIn("- Do a thing · #7 · v4.1.0 · 기록 3", lines)
        self.assertIn("- 11-20 Do a thing · #7 · v4.1.0 · 기록 3".replace("11-20", "11-20"), lines)
        self.assertIn("6시간 넘게 움직임 없는 열린 일 2개는 접어 둠.", lines)
        self.assertIn("앞서 끝난 것 1,354개는 접어 둠.", lines)
        self.assertIn("- Which one?", lines)
        self.assertIn("질문판 기준 11:58", lines)
        self.assertNotIn("나중에 할 것", text)
        for word in ("사이클", "주기", "현황판", "보드", "cycle"):
            self.assertNotIn(word, text)
        for line in lines:  # every description line is not a bullet
            if line.startswith("- "):
                continue
            self.assertFalse(re.match(r"^\s*([-*+]|\d+[.)])\s", line), line)

    def test_empty_sections_and_an_unreadable_board(self):
        text = self.render(waits={"status": "unreadable"})
        self.assertEqual(text.count("지금 없음."), 1)
        self.assertIn("\n질문판을 읽지 못했습니다.\n", text)
        self.assertNotIn("- 질문판", text)  # a plain line: it is not a bullet, so it is not a question
        self.assertIn("아직 없음.", text)
        self.assertIn("지금 없음.\n질문판 기준 11:59", self.render())

    def test_a_copy_from_another_day_shows_its_date(self):
        old = {"status": "ok", "at": NOW - 3 * 24 * HOUR, "items": ["Q?"]}
        self.assertIn("- Q?\n질문판 기준 11-17 12:00\n", self.render(waits=old))
        self.assertIn("질문판 기준 11:59\n", self.render(waits=dict(old, at=NOW - 60)))

    def test_the_summary_falls_back_to_the_title_and_markup_shows_as_typed(self):
        self.assertIn("**한 줄**: Demo", self.render(summary=""))
        text = self.render(title="a*b_c[d]<e>&`f`", summary="")
        self.assertIn("# a\\*b_c\\[d\\]\\<e\\>\\&`f`", text)

    def test_a_paired_backtick_stays_a_code_span_and_a_lone_one_is_escaped(self):
        esc = cd._escape
        self.assertEqual(esc("`campaign.md` fix"), "`campaign.md` fix")
        self.assertEqual(esc("`a_b` and `<x>`"), "`a_b` and `<x>`")      # nothing inside a code span changes
        self.assertEqual(esc("fix `a"), "fix \\`a")
        self.assertEqual(esc("`a` and `b"), "`a` and \\`b")
        self.assertEqual(esc("a ``b`` c"), "a \\`\\`b\\`\\` c")
        self.assertEqual(esc("<b> & *c* [d]"), "\\<b\\> \\& \\*c\\* \\[d\\]")
        self.assertEqual(esc("campaign_document _x_"), "campaign_document \\_x\\_")
        self.assertEqual(esc("a\\b"), "a\\\\b")
        text = self.render(title="Fix `campaign.md` and `x", summary="")
        self.assertIn("# Fix `campaign.md` and \\`x", text)
        unit = {"key": "rt:x", "pr": None, "v": None, "title": "Write `campaign.md` <now>", "impl": {"slug": "x"},
                "count": 1, "closed_at": NOW, "routes": 1, "activity": NOW}
        self.assertIn("- Write `campaign.md` \\<now\\> · 기록 1",
                      self.render({"active": [unit], "active_folded": 0, "done": [], "done_folded": 0}))

    def test_the_popup_draws_the_generated_document_without_its_marker_or_backslashes(self):
        from tools.fleet.explorer import transcript
        text = self.render(title="a*b_c[d]<e>&`f` and `g", summary="")
        rows = transcript.doc_rows(text.split("\n"), 60)
        drawn = "\n".join("".join(part for part, _key in row) for row in rows)
        self.assertNotIn("hearting:campaign-document", drawn)
        self.assertNotIn("\\", drawn)
        self.assertIn("a*b_c[d]<e>&f and `g", drawn)

    def test_a_long_title_is_clipped(self):
        long_title = "가" * 200
        unit = {"key": "rt:x", "pr": None, "v": None, "title": long_title, "impl": {"slug": "x"}, "count": 1,
                "closed_at": NOW, "routes": 1, "activity": NOW}
        self.assertIn("- " + "가" * 89 + "…" + " · 기록 1", self.render({"active": [unit], "active_folded": 0, "done": [], "done_folded": 0}))

    def test_long_questions_are_clipped_and_the_text_stays_inside_the_read_limit(self):
        text = self.render(waits={"status": "ok", "at": NOW, "items": ["질" * 300]})
        self.assertIn("- " + "질" * 119 + "…", text)
        units = [{"key": f"rt:{i}", "pr": None, "v": None, "title": "t" * 90, "impl": {"slug": "x"}, "count": 1,
                  "closed_at": NOW - i, "routes": 1, "activity": 0} for i in range(400)]
        big = self.render({"active": [], "active_folded": 0, "done": units, "done_folded": 5})
        self.assertLessEqual(len(big.encode()), cd.SIZE_LIMIT)
        self.assertIn("앞서 끝난 것", big)


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="campaign-document-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / ".agent_reports"
        self.campaign_id = "camp_" + "1" * 32
        self.folder = self.campaign("2026-11-01_demo", self.campaign_id, "Demo")
        self.runtime = self.root / ".runtime"
        (self.runtime / "routes").mkdir(parents=True)
        (self.runtime / "artifact-producer/v1/cycles").mkdir(parents=True)
        self.serial = 0
        (self.runtime / "campaign-document").mkdir()
        (self.runtime / "campaign-document" / "watermark.json").write_text('{"v": 1, "ns": 1}')  # everything is new
        project_status.clear_cache()
        self.addCleanup(project_status.clear_cache)

    def campaign(self, locator, campaign_id, title, state="active"):
        folder = self.root / "campaigns" / locator
        folder.mkdir(parents=True)
        (folder / "campaign.json").write_text(json.dumps({
            "campaign_id": campaign_id, "key": locator, "title": title, "goal": "g", "state": state,
            "cycles": [], "created_on": "2026-11-01T00:00:00Z"}))
        return folder

    def meta(self, titles=None, summary="Demo summary."):
        cycles = {cy: {"title": title} for cy, title in (titles or {}).items()}
        (self.folder / "meta.json").write_text(json.dumps({
            "schema_version": 1, "campaign": {"title": "Demo campaign", "summary": summary}, "cycles": cycles}))

    def route(self, slug, *, started=NOW - 10 * HOUR, closed=None, proven=True, capability="autopilot-code",
              evidence=None, campaign_id=None, state="sealed", rid=None, touched=None, record_dup=None):
        self.serial += 1
        rid = rid or f"rt-{self.serial:016x}"
        cycle_id = f"cyc_{self.serial:032x}"
        record = {"campaign_id": campaign_id or self.campaign_id, "cycle_id": cycle_id, "route_id": rid, "slug": slug,
                  "route_capability": capability, "started_on": stamp(started), "state": state,
                  "locator": f"2026-11-20_{slug}", "title": slug}
        record_file = self.runtime / "artifact-producer/v1/cycles" / f"{cycle_id}.json"
        record_file.write_text(json.dumps(record))
        os.utime(record_file, (touched or started, touched or started))
        route_file = self.runtime / "routes" / f"{rid}.json"
        route_file.write_text(json.dumps({"route_id": rid, "slug": slug}))
        os.utime(route_file, (touched or started, touched or started))
        if closed is not None:
            self.close(rid, closed, proven=proven, evidence=evidence)
        return rid, cycle_id

    def close(self, rid, closed, *, proven=True, evidence=None, mtime=None):
        gates = {}
        if evidence is not None:
            report = self.root / f"reports/{rid}.md"
            report.parent.mkdir(exist_ok=True)
            report.write_text(evidence)
            gates = {"unit": {"passed": True, "evidence": str(report)}}
        path = self.runtime / "routes" / f"{rid}.outcome.json"
        path.write_text(json.dumps({"route_id": rid, "closed_at": stamp(closed), "terminal_gate_proven": proven,
                                    "terminal_gates": gates}))
        os.utime(path, (mtime or closed, mtime or closed))
        return path

    def catch_up(self, **kw):
        kw.setdefault("waits", WAITS_OK)
        err = io.StringIO()
        with redirect_stderr(err):
            written = cd.catch_up(self.root, now=kw.pop("now", NOW), **kw)
        self.err = err.getvalue()
        return written

    def document(self, folder=None):
        return ((folder or self.folder) / "campaign.md").read_text()

    def bullets(self, text, section):
        body = text.split("## " + section + "\n", 1)[1].split("\n## ", 1)[0]
        return [line for line in body.split("\n") if line.startswith("- ")]


class GeneratorTest(Fixture):
    def test_a_closed_route_makes_the_document_and_the_next_one_adds_a_line(self):
        a, ca = self.route("alpha-1700", closed=NOW - 3 * HOUR)
        self.meta({ca: "Alpha work"})
        self.assertEqual(self.catch_up(now=NOW), ["2026-11-01_demo"])
        text = self.document()
        self.assertEqual(text.split("\n")[0], cd.MARKER)
        self.assertEqual(self.bullets(text, "최근에 끝난 것"), ["- 11-20 Alpha work · #1700 · 기록 1"])
        b, cb = self.route("rv-1700-unit", closed=NOW + HOUR - 60)
        self.meta({ca: "Alpha work", cb: "Review"})
        self.catch_up(now=NOW + HOUR)
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것"), ["- 11-20 Alpha work · #1700 · 기록 2"])

    def test_the_same_input_writes_the_same_bytes_and_does_not_rewrite(self):
        rid, cycle = self.route("alpha-1700", closed=NOW - 3 * HOUR)
        self.catch_up()
        page = self.folder / "campaign.md"
        os.utime(page, (5, 5))
        first = page.read_bytes()
        self.catch_up(cycle_id=cycle, now=NOW + 600)  # the pass runs again, the body is the same
        self.assertEqual(page.stat().st_mtime, 5)
        self.assertEqual(page.read_bytes(), first)

    def test_a_foreign_campaign_md_a_link_a_directory_and_hand_written_pages_stay_as_they_are(self):
        self.route("alpha-1700", closed=NOW - 3 * HOUR)
        (self.folder / "현황.md").write_text("# mine\n\n## Next\n- x\n")
        (self.folder / "status.md").write_text("# also mine\n")
        (self.folder / "campaign.md").write_text("# hand written\n")
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.folder.iterdir()}
        self.catch_up()
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.folder.iterdir()}, before)
        self.assertIn("campaign_document skipped foreign=2026-11-01_demo", self.err)
        (self.folder / "campaign.md").unlink()
        outside = self.root.parent / "outside.md"
        outside.write_text("outside")
        (self.folder / "campaign.md").symlink_to(outside)
        self.route("beta-1701", closed=NOW + HOUR)
        self.catch_up(now=NOW + 2 * HOUR)
        self.assertEqual(outside.read_text(), "outside")
        self.assertTrue((self.folder / "campaign.md").is_symlink())
        (self.folder / "campaign.md").unlink()
        (self.folder / "campaign.md").mkdir()
        self.route("gamma-1702", closed=NOW + 3 * HOUR)
        self.catch_up(now=NOW + 4 * HOUR)
        self.assertTrue((self.folder / "campaign.md").is_dir())
        self.assertEqual((self.folder / "현황.md").read_text(), "# mine\n\n## Next\n- x\n")

    def test_only_campaigns_that_moved_and_are_active_get_a_document(self):
        other = self.campaign("2026-11-02_other", "camp_" + "2" * 32, "Other")
        closed = self.campaign("2026-11-03_closed", "camp_" + "3" * 32, "Closed", state="closed")
        (self.runtime / "campaign-document" / "watermark.json").write_text(json.dumps({"v": 1, "ns": int((NOW - 2 * HOUR) * 1e9)}))
        self.route("alpha-1700", closed=NOW - HOUR)
        old = self.route("old-1600", started=NOW - 20 * 86400, closed=NOW - 19 * 86400, campaign_id="camp_" + "2" * 32)
        self.route("shut-1500", closed=NOW - HOUR, campaign_id="camp_" + "3" * 32)
        self.catch_up(now=NOW)
        self.assertTrue((self.folder / "campaign.md").exists())
        self.assertFalse((other / "campaign.md").exists())
        self.assertFalse((closed / "campaign.md").exists())
        self.assertIsNotNone(old)

    def test_no_routes_folder_returns_at_once(self):
        empty = Path(tempfile.mkdtemp(prefix="cd-empty-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        self.assertEqual(cd.catch_up(empty), [])
        self.assertEqual(list(empty.iterdir()), [])

    def test_an_open_route_is_doing_until_six_hours_pass_and_an_unproven_close_is_nothing(self):
        self.route("work-1710", started=NOW - HOUR, touched=NOW - 5 * HOUR)
        self.route("stale-x", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        self.route("fail-1711", closed=NOW - HOUR, proven=False)
        self.route("frame-x", capability="route-frame", closed=NOW - HOUR)
        self.catch_up()
        text = self.document()
        self.assertEqual(self.bullets(text, "하는 중"), ["- work-1710 · #1710 · 기록 1"])
        self.assertIn("6시간 넘게 움직임 없는 열린 일 1개는 접어 둠.", text)
        self.assertEqual(self.bullets(text, "최근에 끝난 것"), [])
        self.assertIn("아직 없음.", text)

    def test_the_first_drawing_fills_twenty_pieces_and_counts_the_rest(self):
        for i in range(30):
            self.route(f"job-x{i}", started=NOW - 40 * HOUR + i, closed=NOW - 30 * HOUR + i * 60)
        reads = []
        original = cd._read_fact
        with mock.patch.object(cd, "_read_fact", side_effect=lambda *a: reads.append(a[1]) or original(*a)):
            self.catch_up()
        text = self.document()
        self.assertEqual(len(self.bullets(text, "최근에 끝난 것")), 20)
        self.assertIn("앞서 끝난 것 10개는 접어 둠.", text)
        self.assertEqual(len(reads), 20)
        self.assertLessEqual(len(reads), cd.READ_CAP)
        cache = json.loads((self.runtime / "campaign-document" / f"{self.campaign_id}.json").read_text())
        self.assertIsNotNone(cache["horizon_ns"])
        self.route("job-new", closed=NOW + HOUR - 60)
        reads.clear()
        with mock.patch.object(cd, "_read_fact", side_effect=lambda *a: reads.append(a[1]) or original(*a)):
            self.catch_up(now=NOW + HOUR)
        self.assertEqual(len(reads), 1)
        self.assertIn("앞서 끝난 것 11개는 접어 둠.", self.document())

    def test_a_piece_of_work_still_open_does_not_use_up_the_twenty_lines(self):
        for i in range(26):
            self.route(f"job-pr{2000 + i}", started=NOW - 40 * HOUR + i, closed=NOW - 30 * HOUR + i * 60)
        self.route("more-pr2025", started=NOW - HOUR, touched=NOW - HOUR)   # the newest piece is open again
        self.catch_up()
        text = self.document()
        self.assertEqual(len(self.bullets(text, "최근에 끝난 것")), 20)
        self.assertIn("#2025", "\n".join(self.bullets(text, "하는 중")))
        self.assertNotIn("#2025", "\n".join(self.bullets(text, "최근에 끝난 것")))

    def test_a_deleted_cache_is_rebuilt(self):
        self.route("alpha-1700", closed=NOW - 3 * HOUR)
        self.catch_up()
        before = self.document()
        os.remove(self.runtime / "campaign-document" / f"{self.campaign_id}.json")
        self.route("beta-x", closed=NOW + HOUR - 60)
        self.catch_up(now=NOW + HOUR)
        self.assertEqual(len(self.bullets(self.document(), "최근에 끝난 것")), 2)
        self.assertEqual(len(self.bullets(before, "최근에 끝난 것")), 1)

    def test_a_broken_cache_a_broken_meta_and_an_unreadable_outcome_do_not_raise_or_spoil_the_document(self):
        self.route("alpha-1700", closed=NOW - 3 * HOUR)
        self.catch_up()
        good = self.document()
        (self.runtime / "campaign-document" / f"{self.campaign_id}.json").write_text("{not json")
        (self.folder / "meta.json").write_text("{broken")
        rid, _ = self.route("beta-1701", closed=NOW + HOUR - 60)
        (self.runtime / "routes" / f"{rid}.outcome.json").write_text("{broken")
        self.catch_up(now=NOW + HOUR)
        text = self.document()
        self.assertEqual(self.bullets(text, "최근에 끝난 것"), ["- 11-20 alpha-1700 · #1700 · 기록 1"])
        self.assertEqual(text.split("\n")[0], cd.MARKER)
        self.assertIn("# Demo", text)
        self.assertNotEqual(good, "")

    def test_a_collect_failure_in_one_campaign_does_not_stop_another(self):
        other = self.campaign("2026-11-02_other", "camp_" + "2" * 32, "Other")
        self.route("alpha-1700", closed=NOW - HOUR)
        self.route("beta-1701", closed=NOW - HOUR, campaign_id="camp_" + "2" * 32)
        original = cd.collect

        def collect(root, folder, **kw):
            if Path(folder).name == "2026-11-01_demo":
                raise RuntimeError("boom")
            return original(root, folder, **kw)
        with mock.patch.object(cd, "collect", side_effect=collect):
            written = self.catch_up()
        self.assertEqual(written, ["2026-11-02_other"])
        self.assertTrue((other / "campaign.md").exists())
        self.assertFalse((self.folder / "campaign.md").exists())
        self.assertIn("campaign_document skipped error=RuntimeError", self.err)

    def test_the_hint_cycle_brings_its_campaign_even_when_no_route_file_moved(self):
        rid, cycle = self.route("alpha-1700", started=NOW - 30 * 86400, closed=NOW - 29 * 86400)
        os.remove(self.runtime / "campaign-document" / "watermark.json")  # the first pass looks back ten minutes
        self.assertEqual(self.catch_up(now=NOW), [])  # nothing moved: no document yet
        self.assertEqual(self.catch_up(cycle_id=cycle, now=NOW), ["2026-11-01_demo"])

    def test_two_cycle_records_for_one_route_use_the_sealed_then_the_latest(self):
        rid, cycle = self.route("alpha-1700", closed=NOW - HOUR, state="sealed")
        twin = {"campaign_id": "camp_" + "9" * 32, "cycle_id": "cyc_" + "f" * 32, "route_id": rid, "slug": "alpha-1700",
                "route_capability": "autopilot-code", "started_on": stamp(NOW), "state": "open", "locator": "x"}
        (self.runtime / "artifact-producer/v1/cycles" / ("cyc_" + "f" * 32 + ".json")).write_text(json.dumps(twin))
        self.catch_up()
        self.assertEqual(len(self.bullets(self.document(), "최근에 끝난 것")), 1)

    def test_only_exact_route_and_outcome_names_are_counted(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        (self.runtime / "routes" / "rt-0000000000000001.outcome.json.superseded-1").write_text("{}")
        (self.runtime / "routes" / "rt-0000000000000001.historical-false-2.outcome.json").write_text("{}")
        (self.runtime / "routes" / "rt-0000000000000001.gate-release.json").write_text("{}")
        listing = cd._list_routes(self.root)
        self.assertEqual(list(listing), ["rt-0000000000000001"])

    def test_the_title_comes_from_meta_then_the_route_intent_and_is_never_the_slug(self):
        rid, cycle = self.route("alpha-1700", closed=NOW - HOUR)
        route_file = self.runtime / "routes" / f"{rid}.json"
        route_file.write_text(json.dumps({"route_id": rid, "work_request": {"text": "Fix the thing now. Then more."}}))
        self.meta({cycle: "alpha-1700"})  # a title equal to the slug is not a title
        with mock.patch("work_intent.route_intent", return_value={"intent": "# Fix the thing now. Then more."}):
            self.catch_up()
        self.assertIn("- 11-20 Fix the thing now. · #1700 · 기록 1", self.document())

    def test_the_pr_comes_from_the_report_when_the_slug_has_none(self):
        self.route("alpha-x", closed=NOW - HOUR, evidence="# done\n\nPR #1800 opened\n")
        self.route("beta-x", closed=NOW - 2 * HOUR, evidence="PR #1 and PR #2\n")
        self.catch_up()
        lines = self.bullets(self.document(), "최근에 끝난 것")
        self.assertEqual(lines, ["- 11-20 alpha-x · #1800 · 기록 1", "- 11-20 beta-x · 기록 1"])

    def test_an_evidence_path_outside_the_root_or_a_link_is_not_read(self):
        outside = self.root.parent / "elsewhere.md"
        outside.write_text("PR #1900\n")
        rid, _ = self.route("alpha-x", closed=NOW - HOUR)
        self.close(rid, NOW - HOUR)
        path = self.runtime / "routes" / f"{rid}.outcome.json"
        value = json.loads(path.read_text())
        value["terminal_gates"] = {"u": {"evidence": str(outside)}}
        path.write_text(json.dumps(value))
        link = self.root / "reports-link.md"
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(outside)
        rid2, _ = self.route("beta-x", closed=NOW - 2 * HOUR)
        path2 = self.runtime / "routes" / f"{rid2}.outcome.json"
        path2.write_text(json.dumps({"closed_at": stamp(NOW - 2 * HOUR), "terminal_gate_proven": True,
                                     "terminal_gates": {"u": {"evidence": str(link)}}}))
        self.catch_up()
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것"),
                         ["- 11-20 alpha-x · 기록 1", "- 11-20 beta-x · 기록 1"])

    def test_a_publish_route_puts_its_version_on_the_prs_line(self):
        self.route("work-1750", closed=NOW - 3 * HOUR)
        self.route("publish-v4-62-3", capability="autopilot-ship", closed=NOW - HOUR,
                   evidence="# v4.62.3 — #1750\n\n병합된 PR: #1750\n")
        self.catch_up()
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것"), ["- 11-20 work-1750 · #1750 · v4.62.3 · 기록 2"])

    def test_promotion_makes_an_unproven_close_a_line_and_fills_a_publish_route(self):
        rid, _ = self.route("job-1760", closed=NOW - 3 * HOUR, proven=False)
        pid, _ = self.route("publish-v1", capability="autopilot-ship", closed=NOW - 2 * HOUR, proven=False,
                            evidence="# v9.9.9 — #1760\n")
        self.catch_up()
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것"), [])
        self.close(rid, NOW - 3 * HOUR, proven=True, mtime=NOW + HOUR - 60)
        self.close(pid, NOW - 2 * HOUR, proven=True, evidence="# v9.9.9 — #1760\n", mtime=NOW + HOUR - 50)
        self.catch_up(now=NOW + HOUR)
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것"), ["- 11-20 job-1760 · #1760 · v9.9.9 · 기록 2"])

    def test_a_marked_page_is_replaced_but_unmarked_text_before_it_is_not_kept(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        (self.folder / "campaign.md").write_text(cd.MARKER + "\n# old\n\nhand added\n")
        self.catch_up()
        self.assertNotIn("hand added", self.document())


class ReadersTest(Fixture):
    def test_fleet_and_the_common_reader_read_the_generated_document(self):
        rid, cycle = self.route("alpha-1700", closed=NOW - HOUR)
        self.route("open-x", started=NOW - HOUR)
        waits = {"status": "ok", "at": NOW, "items": ["First?", "Second?"]}
        self.catch_up(waits=waits)
        text = self.document()
        parsed = project_status.parse(text)
        self.assertEqual(parsed["title"], "Demo")
        self.assertEqual(parsed["line"], "Demo")  # no summary: the title stands in, never the marker
        self.assertEqual(len(parsed["decisions"]), 2)
        self.assertNotIn("hearting", parsed["line"])
        self.meta(summary="Demo summary.")
        self.catch_up(waits=waits, cycle_id=cycle, now=NOW + 5)
        self.assertEqual(project_status.parse(self.document())["line"], "Demo summary.")
        from artifact_campaign_documents import campaign_status
        self.assertEqual(campaign_status(self.folder)["path"], self.folder / "campaign.md")

    def test_fleet_counts_nothing_for_an_empty_board_and_the_entrance_points_at_the_document(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        self.catch_up()
        self.assertEqual(project_status.parse(self.document())["decisions"], [])
        self.assertEqual(project_status.find_documents(str(self.root)), [str(self.folder / "campaign.md")])
        snapshot = entrance.scan(self.root)
        self.assertEqual(snapshot["all_campaigns"][0]["target"], "campaigns/2026-11-01_demo/campaign.md")

    def test_a_worker_turn_makes_the_document_before_the_entrance_points_at_it(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        with redirect_stderr(io.StringIO()):
            entrance.request_refresh(self.root)
        self.assertTrue((self.folder / "campaign.md").exists())
        self.assertIn("campaigns/2026-11-01_demo/campaign.md", (self.root / "README.md").read_text())

    def test_a_failing_generator_never_stops_the_entrance(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        err = io.StringIO()
        with mock.patch.object(cd, "catch_up", side_effect=RuntimeError("boom")), redirect_stderr(err):
            entrance.request_refresh(self.root)
        self.assertTrue((self.root / "README.md").exists())
        self.assertIn("campaign_document skipped error=RuntimeError", err.getvalue())


class RemovalAndWorkerTest(Fixture):
    def test_only_the_generators_own_document_is_removed_for_a_dropped_campaign(self):
        page = self.folder / "campaign.md"
        page.write_text(cd.MARKER + "\n# x\n")
        cd.remove_generated(self.folder)
        self.assertFalse(page.exists())
        page.write_text("# hand written\n")
        cd.remove_generated(self.folder)
        self.assertTrue(page.exists())
        page.unlink()
        outside = self.root.parent / "o.md"
        outside.write_text(cd.MARKER + "\n")
        page.symlink_to(outside)
        cd.remove_generated(self.folder)
        self.assertTrue(outside.exists() and page.is_symlink())
        page.unlink()
        page.mkdir()
        cd.remove_generated(self.folder)
        self.assertTrue(page.is_dir())
        cd.remove_generated(self.root / "nowhere")  # a missing folder is not an error

    def test_the_detached_worker_loop_makes_the_document_then_the_entrance(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        (self.runtime / "artifact-entrance-pending.json").write_text(json.dumps({"cycle_id": ""}))
        fd = os.open(os.devnull, os.O_RDONLY)
        entrance._refresh_requested(self.root, fd)
        self.assertTrue((self.folder / "campaign.md").exists())
        self.assertIn("campaigns/2026-11-01_demo/campaign.md", (self.root / "README.md").read_text())
        self.assertFalse((self.runtime / "artifact-entrance-pending.json").exists())


class WaitsTest(Fixture):
    """The board's saved copy and its question files sit in an isolated state folder."""

    def setUp(self):
        super().setUp()
        import human_wait_state
        self.hws = human_wait_state
        state = tempfile.TemporaryDirectory(prefix="cd-state-")
        self.addCleanup(state.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_STATE_HOME": state.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.other = Path(tempfile.mkdtemp(prefix="cd-other-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.other, ignore_errors=True))
        (self.other / ".agent_reports").mkdir()
        self.mine = self.root.parent

    def copy(self, items, observed=NOW - 30):
        rows = [dict({"since": NOW - 100, "owner": {}, "stale": False}, **item) for item in items]
        self.hws._write(self.hws._cache_path(), {
            "schema_version": 3, "observed_at": observed, "items": rows, "by_session": {},
            "total": len(rows), "older": 0})

    def question(self, name, cwd, text, lifecycle="open"):
        item_id = "S4:" + name * 64
        self.hws._write(self.hws.state_root() / (name * 8 + ".json"), {
            "schema_version": 1, "mode": "message", "source": "S4", "id": item_id, "harness": "claude",
            "session_id": "s-" + name, "seat_key": "seat-" + name, "sequence": 1, "question": text,
            "since": NOW - 100, "lifecycle": lifecycle, "cwd": str(cwd)})
        return item_id

    def asked(self, name, source="S4"):
        return {"id": "S4:" + name * 64, "source": source}

    def test_this_repos_open_s4_questions_come_from_their_files_one_line_each(self):
        self.question("a", self.mine, "Which one?\nsecond line")
        self.question("b", self.mine, "Second?")
        self.copy([self.asked("a"), self.asked("b")])
        waits = cd.read_waits(self.root, NOW)
        self.assertEqual(waits, {"status": "ok", "at": NOW - 30, "items": ["Which one?", "Second?"]})

    def test_another_repo_an_answered_a_folded_and_a_fileless_question_are_left_out(self):
        self.question("a", self.other, "Other repo")
        self.question("b", self.mine, "Answered one", lifecycle="answered")
        self.question("c", self.mine, "Old one")
        self.question("d", self.mine, "Blocked owner")
        self.question("f", self.mine, "No place on the copy")
        self.copy([self.asked("a"), self.asked("b"),
                   dict(self.asked("c"), stale=True), dict(self.asked("c"), id="S4:" + "9" * 64, stale="older"),
                   dict(self.asked("d"), source="S3"),
                   self.asked("e"),                                   # no file at all
                   dict(self.asked("g"), cwd=str(self.mine), question="Only on the copy")])  # the copy's own fields are not read
        self.assertEqual(cd.read_waits(self.root, NOW)["items"], [])

    def test_a_route_item_is_split_by_its_route_as_before(self):
        rid, _ = self.route("alpha-1700")
        self.copy([
            {"id": "a", "source": "S1", "route_id": rid, "question": "By route?\nsecond line"},
            {"id": "e", "source": "S3", "route_id": rid, "question": "Blocked owner"},
            {"id": "f", "source": "S1", "route_id": rid, "question": "Old one", "stale": True},
            {"id": "g", "source": "S1", "route_id": rid, "question": "Older", "stale": "older"},
            {"id": "h", "source": "S1", "route_id": rid},
            {"id": "i", "source": "S1", "route_id": "rt-" + "0" * 16, "question": "Not this root's route"}])
        self.assertEqual(cd.read_waits(self.root, NOW)["items"], ["By route?", "질문 (본문 없음)"])

    def test_an_old_copy_is_used_and_its_time_is_written(self):
        self.question("a", self.mine, "Still open?")
        self.copy([self.asked("a")], observed=NOW - 3 * 24 * HOUR)
        waits = cd.read_waits(self.root, NOW)
        self.assertEqual(waits, {"status": "ok", "at": NOW - 3 * 24 * HOUR, "items": ["Still open?"]})
        text = cd.render_document("Demo", "", {"active": [], "active_folded": 0, "done": [], "done_folded": 0},
                                  waits, now=NOW, tz=UTC)
        self.assertIn("- Still open?\n질문판 기준 11-17 12:00\n", text)

    def test_no_copy_or_a_failing_read_is_unreadable(self):
        self.assertEqual(cd.read_waits(self.root, NOW), {"status": "unreadable"})
        with mock.patch.object(self.hws, "cached_view", side_effect=OSError):
            self.assertEqual(cd.read_waits(self.root, NOW), {"status": "unreadable"})

    def test_reading_changes_no_question_file_and_no_copy(self):
        self.question("a", self.mine, "Q?")
        self.copy([self.asked("a")])
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.hws.state_root().iterdir()}
        cd.read_waits(self.root, NOW)
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.hws.state_root().iterdir()}
        self.assertEqual(before, after)

    def test_the_count_fleet_reads_is_the_number_of_open_questions(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        for name in "abc":
            self.question(name, self.mine, f"Q{name}?")
        self.question("d", self.other, "Not ours")
        self.copy([self.asked(name) for name in "abcd"])
        waits = cd.read_waits(self.root, NOW)
        self.assertEqual(waits["items"], ["Qa?", "Qb?", "Qc?"])
        self.catch_up(waits=waits)
        self.assertEqual(len(project_status.parse(self.document())["decisions"]), 3)


if __name__ == "__main__":
    unittest.main()
