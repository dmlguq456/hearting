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

    def test_a_piece_with_a_running_attempt_on_any_open_route_is_doing_whatever_its_age(self):
        old = NOW - 20 * HOUR
        units = cd.link_units([], [self.open_route("a", old, pr=9), dict(self.open_route("b", old, pr=9, kind="review"), alive=True),
                                   self.open_route("c", old), dict(self.open_route("d", old), alive=False)], {}, now=NOW)
        self.assertEqual([u["key"] for u in units["active"]], ["pr:9"])
        self.assertEqual(units["active_folded"], 2)

    def test_a_piece_of_work_with_an_open_route_is_doing_even_with_closed_members(self):
        units = cd.link_units([self.fact("a", pr=7)], [self.open_route("b", NOW - HOUR, pr=7)], {}, now=NOW)
        self.assertEqual([(u["key"], u["count"]) for u in units["active"]], [("pr:7", 2)])
        self.assertEqual(units["done"], [])

    def test_a_published_piece_is_finished_though_a_review_route_of_its_pr_stays_open(self):
        """Round 3 (11-21): `#1785 · v4.66.2` and `#1775 · v4.66.3` stood under "하는 중" with a blocked review left open."""
        facts = [self.fact("impl85", pr=1785, c=NOW - 3 * HOUR, st=1),
                 self.fact("pub85", kind="publish", prs=[1785], v="v4.66.2", c=NOW - 2 * HOUR),
                 self.fact("pub75", kind="publish", prs=[1775], v="v4.66.3", c=NOW - HOUR)]
        opened = [self.open_route("rv85", NOW - 600, pr=1785, kind="review", st=5),
                  self.open_route("rv75", NOW - 600, pr=1775, kind="review", st=5)]
        units = cd.link_units(facts, opened, {}, now=NOW)
        self.assertEqual(units["active"], [])
        self.assertEqual(units["active_folded"], 0)
        # listed by the latest close, else by the publish's close; the order is the done order as ever
        self.assertEqual([(u["key"], u["v"], u["closed_at"], u["count"]) for u in units["done"]],
                         [("pr:1775", "v4.66.3", NOW - HOUR, 2), ("pr:1785", "v4.66.2", NOW - 3 * HOUR, 3)])
        self.assertEqual(units["done_folded"], 0)

    def test_a_published_piece_stays_doing_only_for_work_opened_after_the_publish(self):
        publish = self.fact("pub", kind="publish", prs=[7], v="v1.0.0", c=NOW - 2 * HOUR)
        done = self.fact("impl", pr=7, c=NOW - 3 * HOUR)
        next_piece = self.open_route("next", NOW - 600, pr=7, st=NOW - HOUR)       # started after the publish closed
        older = self.open_route("stuck", NOW - 600, pr=7, st=NOW - 5 * HOUR)       # started before it
        unknown = self.open_route("nostart", NOW - 600, pr=7, st=None)
        for label, opened, doing in (("next piece", [next_piece], True), ("stuck older work", [older], False),
                                     ("start unknown", [unknown], True), ("work and review", [older, dict(
                                         next_piece, rid="rv", kind="review")], False)):
            with self.subTest(label):
                units = cd.link_units([done, publish], opened, {}, now=NOW)
                self.assertEqual([u["key"] for u in units["active"]], ["pr:7"] if doing else [])
                self.assertEqual([u["key"] for u in units["done"]], [] if doing else ["pr:7"])
        # no published version: an open review is still doing (nothing says it is over)
        bare = self.fact("pub", kind="publish", prs=[7], v=None, c=NOW - 2 * HOUR)
        units = cd.link_units([done, bare], [dict(older, kind="review")], {}, now=NOW)
        self.assertEqual([u["key"] for u in units["active"]], ["pr:7"])

    def test_twenty_lines_of_done_and_a_count_of_the_rest(self):
        facts = [self.fact(f"r{i:02d}", c=NOW - i * 60) for i in range(21)]
        units = cd.link_units(facts, [], {}, now=NOW, closed_total=1500)
        self.assertEqual(len(units["done"]), 20)
        self.assertEqual(units["done"][0]["key"], "rt:r00")
        self.assertEqual(units["done_folded"], 1480)

    def render(self, units=None, title="Demo", summary="One line."):
        units = units or {"active": [], "active_folded": 0, "done": [], "done_folded": 0}
        return cd.render_document(title, summary, units, now=NOW, tz=UTC)

    def test_the_document_has_the_agreed_shape_and_no_forbidden_words(self):
        unit = {"key": "pr:7", "pr": 7, "v": "v4.1.0", "title": "Do a thing", "impl": {"slug": "x"}, "count": 3,
                "closed_at": NOW - HOUR, "routes": 2, "activity": 0}
        text = self.render({"active": [unit], "active_folded": 2, "done": [unit], "done_folded": 1354})
        lines = text.split("\n")
        self.assertEqual(lines[0], cd.MARKER)
        self.assertEqual(lines[1], "# Demo")
        self.assertEqual(lines[3], "갱신 2026-11-20 12:00")
        self.assertEqual(lines[5], "**한 줄**: One line.")
        headings = [line for line in lines if line.startswith("## ")]
        self.assertEqual(headings, ["## 하는 중", "## 최근에 끝난 것"])   # 11-22: a question stands on the question screens only
        self.assertIn("- Do a thing · #7 · v4.1.0 · 기록 3", lines)
        self.assertIn("- 11-20 Do a thing · #7 · v4.1.0 · 기록 3".replace("11-20", "11-20"), lines)
        self.assertIn("6시간 넘게 움직임 없는 열린 일 2개는 접어 둠.", lines)
        self.assertIn("앞서 끝난 기록 1,354개는 접어 둠.", lines)
        self.assertNotIn("나중에 할 것", text)
        for word in ("기다리는 것", "질문판"):
            self.assertNotIn(word, text)
        for word in ("사이클", "주기", "현황판", "보드", "cycle"):
            self.assertNotIn(word, text)
        for line in lines:  # every description line is not a bullet
            if line.startswith("- "):
                continue
            self.assertFalse(re.match(r"^\s*([-*+]|\d+[.)])\s", line), line)

    def test_an_empty_campaign_says_none_in_its_two_sections_and_names_no_question(self):
        text = self.render()
        self.assertEqual(text.count("지금 없음."), 1)
        self.assertIn("아직 없음.", text)
        self.assertEqual([line for line in text.split("\n") if line.startswith("## ")], ["## 하는 중", "## 최근에 끝난 것"])

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

    def test_a_long_finished_list_stays_inside_the_read_limit(self):
        units = [{"key": f"rt:{i}", "pr": None, "v": None, "title": "t" * 90, "impl": {"slug": "x"}, "count": 1,
                  "closed_at": NOW - i, "routes": 1, "activity": 0} for i in range(400)]
        big = self.render({"active": [], "active_folded": 0, "done": units, "done_folded": 5})
        self.assertLessEqual(len(big.encode()), cd.SIZE_LIMIT)
        self.assertIn("앞서 끝난 기록", big)

    def unit(self, index, title="가" * 89):
        return {"key": f"rt:{index}", "pr": None, "v": None, "title": title, "impl": {"slug": "x"}, "count": 1,
                "closed_at": NOW - index, "routes": 1, "activity": NOW - index}

    def test_a_document_never_passes_the_limit_whatever_section_is_long(self):
        """Review round 2 (11-21): 330 open routes with long titles made 72 KB, which the readers (65,536) refuse."""
        for label, args in {
            "doing": ({"active": [self.unit(i) for i in range(330)], "active_folded": 0, "done": [], "done_folded": 0},
                      "Demo", "One line."),
            "done": ({"active": [], "active_folded": 0, "done": [self.unit(i) for i in range(400)], "done_folded": 0},
                     "Demo", "One line."),
            "everything": ({"active": [self.unit(i) for i in range(500)], "active_folded": 0,
                            "done": [self.unit(i) for i in range(400)], "done_folded": 9}, "제" * 5000, "요" * 30000),
        }.items():
            with self.subTest(label):
                units, title, summary = args
                text = cd.render_document(title, summary, units, now=NOW, tz=UTC)
                self.assertLessEqual(len(text.encode("utf-8")), cd.SIZE_LIMIT)
                self.assertLess(cd.SIZE_LIMIT, 65536)
                self.assertTrue(text.startswith(cd.MARKER + "\n# "))
                self.assertEqual([line for line in text.split("\n") if line.startswith("## ")],
                                 ["## 하는 중", "## 최근에 끝난 것"])

    def test_what_is_folded_is_counted_on_a_line_that_is_not_a_bullet(self):
        units = {"active": [self.unit(i) for i in range(330)], "active_folded": 0, "done": [], "done_folded": 0}
        text = cd.render_document("Demo", "One line.", units, now=NOW, tz=UTC)
        shown = len([line for line in text.split("\n") if line.startswith("- ")])
        folded = re.search(r"^크기 한도로 접은 열린 일 ([\d,]+)개\.$", text, re.MULTILINE)
        self.assertIsNotNone(folded)
        self.assertEqual(shown + int(folded.group(1).replace(",", "")), 330)
        self.assertIn("- " + "가" * 89 + " · 기록 1", text)   # the freshest lines stay

    def test_the_size_limit_folds_finished_lines_first_and_then_open_work(self):
        units = {"active": [self.unit(i) for i in range(400)], "active_folded": 0,
                 "done": [self.unit(i) for i in range(20)], "done_folded": 3}
        text = cd.render_document("Demo", "One line.", units, now=NOW, tz=UTC)
        self.assertLessEqual(len(text.encode("utf-8")), cd.SIZE_LIMIT)
        self.assertEqual(self.bullets_of(text, "최근에 끝난 것"), [])             # the finished lines went first
        shown = len(self.bullets_of(text, "하는 중"))
        self.assertLess(shown, 400)
        self.assertIn(f"크기 한도로 접은 열린 일 {400 - shown}개.", text)

    def bullets_of(self, text, section):
        body = text.split("## " + section + "\n", 1)[1].split("\n## ", 1)[0]
        return [line for line in body.split("\n") if line.startswith("- ")]

    def test_a_title_and_a_summary_are_clipped(self):
        text = cd.render_document("제" * 5000, "요" * 30000, {"active": [], "done": []}, now=NOW, tz=UTC)
        self.assertEqual(text.split("\n")[1], "# " + "제" * (cd.HEADING_CHARS - 1) + "…")
        self.assertIn("**한 줄**: " + "요" * (cd.SUMMARY_CHARS - 1) + "…\n", text)

    def test_an_unpaired_surrogate_is_dropped_and_the_text_can_be_written_as_utf8(self):
        unit = self.unit(1, title="앞\ud800뒤")
        text = cd.render_document("제\udc00목", "요\ud83d약", {"active": [unit], "done": []}, now=NOW, tz=UTC)
        text.encode("utf-8")
        for word in ("제목", "요약", "앞뒤"):
            self.assertIn(word, text)


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
        written = touched or closed or started   # the record is written at the begin, or again at the seal
        os.utime(record_file, (written, written))
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
        err = io.StringIO()
        with redirect_stderr(err):
            written = cd.catch_up(self.root, now=kw.pop("now", NOW), **kw)
        self.err = err.getvalue()
        return written

    def document(self, folder=None):
        return ((folder or self.folder) / "campaign.md").read_text()

    def raw_bullets(self, text, section):
        body = text.split("## " + section + "\n", 1)[1].split("\n## ", 1)[0]
        return [line for line in body.split("\n") if line.startswith("- ")]

    def bullets(self, text, section):
        """The lines as a person reads them: the tail `[기록 N ›](<records/…>)` shows as `기록 N`."""
        return [re.sub(r"\[(기록 \d+) ›\]\(<[^>]*>\)", r"\1", line) for line in self.raw_bullets(text, section)]


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
        self.assertIn("앞서 끝난 기록 10개는 접어 둠.", text)
        self.assertEqual(len(reads), 20)
        self.assertLessEqual(len(reads), cd.READ_CAP)
        cache = json.loads((self.runtime / "campaign-document" / f"{self.campaign_id}.json").read_text())
        self.assertIsNotNone(cache["horizon_ns"])
        self.route("job-new", closed=NOW + HOUR - 60)
        reads.clear()
        with mock.patch.object(cd, "_read_fact", side_effect=lambda *a: reads.append(a[1]) or original(*a)):
            self.catch_up(now=NOW + HOUR)
        self.assertEqual(len(reads), 1)
        self.assertIn("앞서 끝난 기록 11개는 접어 둠.", self.document())

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
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것"), ["- 11-20 Fix the thing now. · #1700 · 기록 1"])

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
        self.catch_up()
        text = self.document()
        parsed = project_status.parse(text)
        self.assertEqual(parsed["title"], "Demo")
        self.assertEqual(parsed["line"], "Demo")  # no summary: the title stands in, never the marker
        self.assertEqual(parsed["decisions"], [])
        self.assertNotIn("hearting", parsed["line"])
        self.meta(summary="Demo summary.")
        self.catch_up(cycle_id=cycle, now=NOW + 5)
        self.assertEqual(project_status.parse(self.document())["line"], "Demo summary.")
        from artifact_campaign_documents import campaign_status
        self.assertEqual(campaign_status(self.folder)["path"], self.folder / "campaign.md")

    def test_fleet_counts_no_decision_in_a_generated_document_even_an_older_one_with_the_waiting_section(self):
        old = cd.MARKER + "\n# Old\n\n## 하는 중\n- a job\n\n## 기다리는 것\n- First?\n- Second?\n\n## 최근에 끝난 것\n아직 없음.\n"
        self.assertEqual(project_status.parse(old)["decisions"], [])
        by_hand = old.split("\n", 1)[1]                                  # the same words with no marker: a person's page
        self.assertEqual(project_status.parse(by_hand)["decisions"], ["First?", "Second?"])

    def test_the_entrance_points_at_the_generated_document(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        self.catch_up()
        self.assertEqual(project_status.parse(self.document())["decisions"], [])
        self.assertEqual(project_status.find_documents(str(self.root)), [str(self.folder / "campaign.md")])
        snapshot = entrance.scan(self.root)
        self.assertEqual(snapshot["all_campaigns"][0]["target"], "campaigns/2026-11-01_demo/campaign.md")

    def test_the_entrance_draws_and_counts_no_question_from_an_older_page_with_the_waiting_section(self):
        (self.folder / "campaign.md").write_text(
            cd.MARKER + "\n# Demo\n\n## 하는 중\n지금 없음.\n\n## 기다리는 것\n- Old question?\n질문판 기준 11:59\n\n## 최근에 끝난 것\n아직 없음.\n")
        snapshot = entrance.scan(self.root)
        self.assertEqual(snapshot["all_campaigns"][0]["target"], "campaigns/2026-11-01_demo/campaign.md")
        drawn = entrance.render_markdown(snapshot, entrance._translator()) + entrance.render_recent_markdown(snapshot, entrance._translator()) \
            + entrance.render_html(snapshot, entrance._translator()) + json.dumps(snapshot, ensure_ascii=False, default=str)
        for word in ("Old question?", "기다리는 것", "질문판"):
            self.assertNotIn(word, drawn)

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


class ReviewFixesTest(Fixture):
    """The independent review of PR #1784 (11-19): each fix has a test that fails on the code before it."""

    def other_campaign(self):
        self.other_id = "camp_" + "2" * 32
        return self.campaign("2026-11-02_other", self.other_id, "Other")

    def record_path(self, cycle):
        return self.runtime / "artifact-producer/v1/cycles" / f"{cycle}.json"

    def test_a_route_moved_to_another_campaign_leaves_the_old_document(self):
        other = self.other_campaign()
        self.route("another-a", closed=NOW - 4 * HOUR)
        _moved, cycle = self.route("moved-work", closed=NOW - 3 * HOUR)
        self.catch_up(now=NOW)
        self.assertIn("moved-work", self.document())
        record = json.loads(self.record_path(cycle).read_text())
        self.record_path(cycle).write_text(json.dumps(dict(record, campaign_id=self.other_id)))
        os.utime(self.record_path(cycle), (NOW + 60, NOW + 60))
        os.utime(self.runtime / "routes" / f"{record['route_id']}.json", (NOW + 60, NOW + 60))
        self.catch_up(now=NOW + 120)
        self.assertIn("moved-work", self.document(other))
        self.assertNotIn("moved-work", self.document())          # the old document is written again in that pass
        self.assertIn("another-a", self.document())

    def test_a_move_seen_before_the_old_campaign_has_any_event_is_still_dropped_from_it(self):
        other = self.other_campaign()
        self.route("another-a", closed=NOW - 4 * HOUR)
        _moved, cycle = self.route("moved-work", closed=NOW - 3 * HOUR)
        self.catch_up(now=NOW)
        record = json.loads(self.record_path(cycle).read_text())
        self.record_path(cycle).write_text(json.dumps(dict(record, campaign_id=self.other_id)))
        os.utime(self.record_path(cycle), (NOW + 60, NOW + 60))
        self.catch_up(now=NOW + 120)
        self.route("another-a2", closed=NOW + 300)               # a later event of the old campaign
        self.catch_up(now=NOW + 400)
        self.assertNotIn("moved-work", self.document())
        self.assertIn("another-a2", self.document())

    def test_a_pass_reads_the_cycle_records_that_changed_and_no_others(self):
        for index in range(60):
            self.route(f"old-{index}", closed=NOW - 5 * HOUR - index)
        self.catch_up(now=NOW)
        self.route("fresh-work", closed=NOW + 100)
        reads = []
        real = cd.store._read_json
        cycles = str(self.runtime / "artifact-producer/v1/cycles")
        with mock.patch.object(cd.store, "_read_json", side_effect=lambda path, *a, **k: (reads.append(str(path)), real(path, *a, **k))[1]):
            self.catch_up(now=NOW + 120)
        self.assertEqual(len([path for path in reads if path.startswith(cycles)]), 1, reads)
        self.assertIn("fresh-work", self.document())

    def test_a_close_that_changed_no_record_still_reaches_the_document(self):
        rid, _cycle = self.route("late-1701", closed=None)
        self.catch_up(now=NOW)
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것"), [])
        self.close(rid, NOW + 100)                               # only the close result is written; the record is not
        self.catch_up(now=NOW + 120)
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것"), ["- 11-20 late-1701 · #1701 · 기록 1"])

    def test_a_still_moving_route_that_names_a_pr_in_its_intent_is_one_line_with_the_closed_one(self):
        self.route("closed-work", closed=NOW - 2 * HOUR, evidence="# r\n\nPR #1779 opened\n")
        rid, _cycle = self.route("continue-pieces", state="open")
        os.utime(self.runtime / "routes" / f"{rid}.json", (NOW - 60, NOW - 60))
        with mock.patch.object(cd, "_intent_title", return_value="조각 S2 PR #1779 이어가기"), \
                mock.patch.object(cd, "_activity", return_value=NOW - 60):
            self.catch_up(now=NOW)
        text = self.document()
        self.assertEqual(self.bullets(text, "하는 중"), ["- 조각 S2 PR #1779 이어가기 · #1779 · 기록 2"])
        self.assertEqual(self.bullets(text, "최근에 끝난 것"), [])

    def test_a_date_or_a_bare_number_in_an_intent_is_not_a_pr_and_two_prs_are_none(self):
        for text, expected in [("PR #1779 이어가기", 1779), ("PR #1779의 이어가기", 1779), ("PR#12 고침", 12), ("PR #123456", None), ("XPR #5", None), ("2026-11-20 정리 #1120", None),
                               ("11-20 PR 정리", None), ("PR #1 과 PR #2", None), ("PR #7 와 PR #7", 7), ("", None), (None, None)]:
            with self.subTest(text=text):
                self.assertEqual(cd.pr_from_title(text), expected)

    def test_a_long_idle_open_route_does_not_read_its_intent(self):
        rid, _cycle = self.route("idle-work", state="open")
        with mock.patch.object(cd, "_intent_title", side_effect=AssertionError("folded work reads nothing")), \
                mock.patch.object(cd, "_activity", return_value=NOW - 7 * HOUR):
            self.catch_up(now=NOW)
        self.assertIn("6시간 넘게 움직임 없는 열린 일 1개는 접어 둠.", self.document())

    def test_the_folded_count_says_records_because_it_counts_routes(self):
        text = cd.render_document("T", "s", {"active": [], "active_folded": 0, "done": [], "done_folded": 1354}, now=NOW, tz=UTC)
        self.assertIn("앞서 끝난 기록 1,354개는 접어 둠.", text)
        self.assertNotIn("앞서 끝난 것", text)

    def test_control_characters_are_not_written_to_the_document(self):
        title = "고침\x1b[31m 빨강\x07\x00 끝\x85\x9f"
        text = cd.render_document(title, "요약\x1b 줄", {"active": [], "done": []}, now=NOW, tz=UTC)
        self.assertEqual(re.findall("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", text), [])
        self.assertIn("# 고침\\[31m 빨강 끝", text)
        self.assertIn("**한 줄**: 요약 줄", text)
        unit = {"key": "rt:x", "pr": None, "v": "v1\x1b", "title": "t\x1bu", "impl": {"slug": "x"}, "count": 1,
                "closed_at": NOW, "routes": 1, "activity": NOW}
        drawn = cd.render_document("T", "s", {"active": [unit], "done": []}, now=NOW, tz=UTC)
        self.assertEqual(re.findall("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", drawn), [])
        self.assertIn("- tu · v1 · 기록 1", drawn)

    def test_a_campaign_md_that_appears_between_the_look_and_the_write_is_never_overwritten(self):
        page = self.folder / cd.DOCUMENT
        real_link = os.link

        def late_link(source, target, *args, **kwargs):
            Path(target).write_text("# written by hand, no marker\n")      # the other file appears first
            return real_link(source, target, *args, **kwargs)

        with mock.patch.object(cd.os, "link", side_effect=late_link):
            self.assertFalse(cd._write_document(self.folder, cd.MARKER + "\n# generated\n"))
        self.assertEqual(page.read_text(), "# written by hand, no marker\n")
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.startswith(".campaign.md.tmp")], [])

    def test_one_that_is_the_generators_own_when_it_appears_meanwhile_is_judged_as_any_other(self):
        page = self.folder / cd.DOCUMENT
        real_link = os.link

        def late_link(source, target, *args, **kwargs):
            Path(target).write_text(cd.MARKER + "\n# older generated\n")
            return real_link(source, target, *args, **kwargs)

        with mock.patch.object(cd.os, "link", side_effect=late_link):
            self.assertTrue(cd._write_document(self.folder, cd.MARKER + "\n# generated\n"))
        self.assertEqual(page.read_text(), cd.MARKER + "\n# generated\n")

    def test_a_new_document_is_made_whole_and_a_fresh_folder_has_no_temporary_file_left(self):
        self.assertTrue(cd._write_document(self.folder, cd.MARKER + "\n# generated\n"))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir() if p.name != "campaign.json"), ["campaign.md"])
        self.assertEqual((self.folder / cd.DOCUMENT).stat().st_mode & 0o777, 0o644)


    def test_a_close_alone_makes_the_first_document_of_a_campaign_that_has_none(self):
        """On e31cf21 this wrote nothing: the record is old and no saved list held the route yet."""
        rid, _cycle = self.route("late-1701", closed=None)           # begun and sealed long ago
        (self.runtime / "campaign-document" / "watermark.json").write_text(json.dumps({"v": 1, "ns": int(NOW * 10 ** 9)}))
        self.assertFalse((self.folder / "campaign.md").exists())
        self.close(rid, NOW + 100)                                   # after the mark only the close is written
        self.assertEqual(self.catch_up(now=NOW + 120), ["2026-11-01_demo"])
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것"), ["- 11-20 late-1701 · #1701 · 기록 1"])

    def test_a_close_that_a_saved_list_holds_does_not_search_by_route_id(self):
        rid, _cycle = self.route("late-1701", closed=None)
        self.catch_up(now=NOW)
        self.close(rid, NOW + 100)
        with mock.patch.object(cd.store, "list_cycle_records", side_effect=AssertionError("found through the list")):
            self.catch_up(now=NOW + 120)
        self.assertIn("late-1701", self.document())

    def test_330_open_routes_with_long_titles_write_a_document_the_readers_can_read(self):
        for index in range(330):
            self.route(f"open-{index}", started=NOW - HOUR, touched=NOW - 600)
        (self.folder / "meta.json").write_text(json.dumps({
            "schema_version": 1, "campaign": {"title": "제" * 89, "summary": "요" * 23000}, "cycles": {}}))
        self.catch_up(now=NOW)
        raw = (self.folder / "campaign.md").read_bytes()
        self.assertLessEqual(len(raw), cd.SIZE_LIMIT)
        import artifact_campaign_documents as documents
        self.assertEqual(documents.campaign_status(self.folder)["status"], "known")

    def test_a_title_with_an_unpaired_surrogate_does_not_stop_the_document(self):
        self.route("alpha-1700", closed=NOW - 3 * HOUR)
        (self.folder / "meta.json").write_text(json.dumps({
            "schema_version": 1, "campaign": {"title": "제\udc00목", "summary": "요\ud800약"}, "cycles": {}}))
        self.assertEqual(self.catch_up(now=NOW), ["2026-11-01_demo"])
        self.assertNotIn("skipped", self.err)
        self.assertIn("# 제목", self.document())


class RoundThreeFixesTest(Fixture):
    def test_a_published_pr_with_a_review_route_left_open_is_under_the_recent_ones(self):
        self.route("work-1785", closed=NOW - 3 * HOUR)
        self.route("rv-1785-close-unit", started=NOW - 2 * HOUR, touched=NOW - 600)            # never closed
        self.route("publish-v4-66-2", capability="autopilot-ship", closed=NOW - 2 * HOUR,
                   evidence="# v4.66.2 — #1785\n\n병합된 PR: #1785\n")
        self.catch_up()
        text = self.document()
        self.assertEqual(self.bullets(text, "하는 중"), [])
        self.assertEqual(self.bullets(text, "최근에 끝난 것"), ["- 11-20 work-1785 · #1785 · v4.66.2 · 기록 3"])


class RunningAttemptTest(Fixture):
    """An open route with a running attempt is doing at any age; the runtime's own liveness says what is running."""

    def setUp(self):
        super().setUp()
        self.jobs = self.root.parent / "state" / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir(parents=True)
        self.jobs.write_text("")
        patch = mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)})
        patch.start()
        self.addCleanup(patch.stop)
        self.sleeper = None

    def row(self, status, attempt, rid, *, alive, at=NOW - 7 * HOUR, log=None, extra="", lineage="",
            worker_type="owner"):
        """A registry row. `alive`: the attempt's process is this test's own; otherwise a pid no one has.
        `lineage`: fields the real writer puts before the row's own `attempt_id` (`parent_attempt_id=…,`)."""
        if alive:
            start = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
            process = f"pid={os.getpid()},pid_start={start}"
        else:
            process = "pid=4000000,pid_start=1,pgid=4000000,pgid_host=4000000"
        meta = (f"{lineage}attempt_id={attempt},worker_type={worker_type},dispatch_depth=1,route_id={rid},"
                f"{process}{extra}")
        if log is not None:
            meta += f",log_file={log}"
        with self.jobs.open("a") as handle:
            handle.write(f"{stamp(at)}\t{status}\t/w\t/w\tslug\t{meta}\n")

    def log(self, moved):
        path = self.root.parent / f"attempt-{self.serial}.log"
        path.write_text("x")
        os.utime(path, (moved, moved))
        return path

    def doing(self):
        self.catch_up()
        text = self.document()
        return self.bullets(text, "하는 중"), text

    def test_a_route_older_than_six_hours_with_a_running_attempt_stays_doing_and_an_idle_one_folds(self):
        busy, _ = self.route("busy-1720", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        self.route("idle-1721", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        self.row("open", "att-busy", busy, alive=True)
        bullets, text = self.doing()
        self.assertEqual(bullets, ["- busy-1720 · #1720 · 기록 1"])
        self.assertIn("6시간 넘게 움직임 없는 열린 일 1개는 접어 둠.", text)

    def test_a_replacement_attempt_counts_when_the_first_one_ended(self):
        busy, _ = self.route("handoff-1722", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        self.row("open", "att-first", busy, alive=False, at=NOW - 9 * HOUR)
        self.row("done", "att-first", busy, alive=False, at=NOW - 8 * HOUR, extra=",failure_class=capacity,note=dead-capacity")
        self.row("open", "att-second", busy, alive=True, at=NOW - 2 * HOUR)
        bullets, _ = self.doing()
        self.assertEqual(bullets, ["- handoff-1722 · #1722 · 기록 1"])

    def test_only_ended_or_gone_attempts_leave_an_old_open_route_folded(self):
        gone, _ = self.route("gone-1723", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        done, _ = self.route("done-1724", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        self.row("open", "att-gone", gone, alive=False)                 # open row, its process is gone
        self.row("open", "att-done", done, alive=True)                   # later closed: the row says it ended
        self.row("done", "att-done", done, alive=False, at=NOW - 6 * HOUR)
        bullets, text = self.doing()
        self.assertEqual(bullets, [])
        self.assertIn("6시간 넘게 움직임 없는 열린 일 2개는 접어 둠.", text)

    def test_one_running_route_of_a_pr_keeps_the_whole_piece_doing_and_its_log_is_its_movement(self):
        old = self.route("work-1725", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)[0]
        review, _ = self.route("rv-1725-unit", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        other, _ = self.route("other-1726", started=NOW - HOUR, touched=NOW - 2 * HOUR)
        self.row("open", "att-review", review, alive=True, log=self.log(NOW - HOUR))
        bullets, _ = self.doing()
        self.assertEqual(bullets, ["- work-1725 · #1725 · 기록 2", "- other-1726 · #1726 · 기록 1"])   # newest movement first
        self.assertNotEqual(old, review)

    def test_a_finished_stage_worker_written_after_its_owner_leaves_the_owner_doing(self):
        busy, _ = self.route("owner-1730", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        self.row("open", "att-owner", busy, alive=True)
        self.row("done", "att-stage", busy, alive=False, at=NOW - 3 * HOUR, worker_type="worker",
                 lineage="parent_attempt_id=att-owner,")
        bullets, _ = self.doing()
        self.assertEqual(bullets, ["- owner-1730 · #1730 · 기록 1"])

    def test_a_finished_unit_call_written_after_a_running_worker_leaves_the_worker_doing(self):
        busy, _ = self.route("worker-1731", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        self.row("open", "att-worker", busy, alive=True, worker_type="worker")
        self.row("done", "att-unit", busy, alive=False, at=NOW - 3 * HOUR, worker_type="worker",
                 lineage="caller_attempt_id=att-worker,")
        bullets, _ = self.doing()
        self.assertEqual(bullets, ["- worker-1731 · #1731 · 기록 1"])

    def test_a_live_child_with_a_finished_sibling_of_the_same_parent_is_doing(self):
        busy, _ = self.route("child-1732", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        self.row("open", "att-child", busy, alive=True, worker_type="worker", lineage="parent_attempt_id=att-p,")
        self.row("done", "att-sibling", busy, alive=False, at=NOW - 3 * HOUR, worker_type="worker",
                 lineage="parent_attempt_id=att-p,")
        bullets, _ = self.doing()
        self.assertEqual(bullets, ["- child-1732 · #1732 · 기록 1"])

    def test_a_last_row_still_being_written_is_not_read(self):
        busy, _ = self.route("tail-1733", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        self.row("open", "att-tail", busy, alive=True)
        with self.jobs.open("a") as handle:   # half of a later row for the same attempt, no newline yet
            handle.write(f"{stamp(NOW - HOUR)}\tdone\t/w\t/w\tslug\tattempt_id=att-tail,worker_ty")
        bullets, _ = self.doing()
        self.assertEqual(bullets, ["- tail-1733 · #1733 · 기록 1"])

    def test_the_registry_is_read_once_per_pass_for_every_campaign_and_nothing_is_written_to_it(self):
        second_id = "camp_" + "2" * 32
        second = self.campaign("2026-11-02_second", second_id, "Second")
        a, _ = self.route("a-1727", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        b, _ = self.route("b-1728", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR, campaign_id=second_id)
        self.row("open", "att-a", a, alive=True)
        self.row("open", "att-b", b, alive=True)
        before = self.jobs.read_bytes()
        import route_autoclose
        with mock.patch.object(route_autoclose, "live_routes", wraps=route_autoclose.live_routes) as spy:
            self.catch_up()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(self.bullets(self.document(), "하는 중"), ["- a-1727 · #1727 · 기록 1"])
        self.assertEqual(self.bullets(self.document(second), "하는 중"), ["- b-1728 · #1728 · 기록 1"])
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_an_unreadable_registry_leaves_the_six_hour_rule_alone(self):
        busy, _ = self.route("busy-1729", started=NOW - 9 * HOUR, touched=NOW - 8 * HOUR)
        self.jobs.write_bytes(b"\xff\xfe not a row\n\t\t\n")
        self.assertEqual(self.doing()[0], [])
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs.parent / "missing.log")
        self.assertEqual(self.doing()[0], [])


class NoQuestionsTest(Fixture):
    """11-22: what waits for a person's answer is a question and stands on the question screens only. The document
    neither reads the question board nor is written again when its questions change."""

    def test_the_module_has_no_way_to_read_the_questions(self):
        for name in ("read_waits", "_waits_signature", "_open_question_files", "_folder_root", "WAIT_CHARS"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(cd, name))

    def test_a_new_document_has_two_sections_and_no_question_line_whatever_the_board_holds(self):
        import human_wait_state
        state = tempfile.TemporaryDirectory(prefix="cd-state-")
        self.addCleanup(state.cleanup)
        digest = "a" * 64
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": state.name}):
            human_wait_state._write(human_wait_state.state_root() / (digest + ".json"), {
                "schema_version": 1, "mode": "message", "source": "S4", "id": "S4:" + digest, "harness": "claude",
                "session_id": "s-a", "seat_key": "seat-a", "sequence": 1, "question": "Which one?\nsecond line",
                "since": NOW - 100, "lifecycle": "open", "cwd": str(self.root.parent)})
            self.route("alpha-1700", closed=NOW - 3 * HOUR)
            self.route("open-x", started=NOW - HOUR)
            self.catch_up()
        text = self.document()
        self.assertEqual([line for line in text.split("\n") if line.startswith("## ")], ["## 하는 중", "## 최근에 끝난 것"])
        for word in ("기다리는 것", "질문판", "Which one?"):
            self.assertNotIn(word, text)

    def test_a_changed_question_set_writes_nothing_and_the_pass_mark_carries_no_question_state(self):
        import human_wait_state
        state = tempfile.TemporaryDirectory(prefix="cd-state-")
        self.addCleanup(state.cleanup)
        self.route("alpha-1700", closed=NOW - 3 * HOUR)
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": state.name}):
            self.catch_up(now=NOW)
            human_wait_state._write(human_wait_state.state_root() / ("b" * 64 + ".json"), {
                "schema_version": 1, "mode": "message", "source": "S4", "id": "S4:" + "b" * 64, "harness": "claude",
                "session_id": "s-b", "seat_key": "seat-b", "sequence": 1, "question": "Just asked?",
                "since": NOW + 5, "lifecycle": "open", "cwd": str(self.root.parent)})
            with mock.patch.object(cd, "collect", side_effect=AssertionError("a question is no reason to write")):
                self.assertEqual(self.catch_up(now=NOW + 60), [])
        self.assertEqual(json.loads((self.runtime / "campaign-document" / "watermark.json").read_text()).keys(), {"v", "ns"})

    def test_an_older_page_that_still_has_the_waiting_section_is_written_again_without_it(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        (self.folder / "campaign.md").write_text(
            cd.MARKER + "\n# old\n\n## 하는 중\n지금 없음.\n\n## 기다리는 것\n- Old question?\n질문판 기준 11:59\n\n## 최근에 끝난 것\n아직 없음.\n")
        self.catch_up()
        text = self.document()
        self.assertNotIn("기다리는 것", text)
        self.assertNotIn("Old question?", text)
        self.assertEqual(self.bullets(text, "최근에 끝난 것"), ["- 11-20 alpha-1700 · #1700 · 기록 1"])


class RecordFixture(Fixture):
    """Routes with cycle folders, reports and the other things a record page reads."""

    def day(self, moment):
        return datetime.fromtimestamp(moment).strftime("%m-%d")

    def cycle_dir(self, slug):
        return self.folder / f"2026-11-20_{slug}"

    def files(self, slug, **names):
        """Write `artifacts/<name>` files (`__` stands for `/`) into the route's cycle folder."""
        for name, text in names.items():
            path = self.cycle_dir(slug) / "artifacts" / name.replace("__", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def bump(self, number):
        """Something that closes after the last pass: a new route, and the clock of the next pass."""
        self.route(f"tick-{chr(96 + number)}", closed=NOW + 1000 * number - 10)
        return NOW + 1000 * number

    def more_cycle(self, rid, slug, *, started, locator=None, state="sealed", written=None):
        """Another cycle of the same route (a review's later round)."""
        self.serial += 1
        cycle_id = f"cyc_{self.serial:032x}"
        record = {"campaign_id": self.campaign_id, "cycle_id": cycle_id, "route_id": rid, "slug": slug,
                  "route_capability": "audit", "started_on": stamp(started), "state": state,
                  "locator": locator or f"2026-11-20_{slug}-r{self.serial}", "title": slug}
        path = self.runtime / "artifact-producer/v1/cycles" / f"{cycle_id}.json"
        path.write_text(json.dumps(record))
        os.utime(path, (written or started, written or started))
        return cycle_id, record["locator"]

    def page(self, key):
        return (self.folder / "records" / f"{key}.md").read_text()

    def page_lines(self, key):
        return [line for line in self.page(key).split("\n") if line.startswith("- ")]

    def work_and_review(self):
        """A PR's work, its two-round review and its publish: the shape of a real page."""
        self.route("alpha-1700", started=NOW - 9 * HOUR, closed=NOW - 8 * HOUR)
        self.files("alpha-1700", **{"final_report.md": "# done\n"})
        rid, _cycle = self.route("rv-1700-unit", started=NOW - 7 * HOUR, closed=NOW - 5 * HOUR, capability="audit")
        self.files("rv-1700-unit", **{"units__unit__report.md": "# Review\n최종 판정: 수정 후 병합 — head abc1234\n"})
        return rid


class RecordLinkTest(RecordFixture):
    def test_the_tail_links_to_the_page_named_by_the_pr_or_by_the_first_routes_folder(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        self.route("solo-work", closed=NOW - 2 * HOUR)
        self.catch_up()
        lines = self.raw_bullets(self.document(), "최근에 끝난 것")
        self.assertEqual(lines, ["- 11-20 alpha-1700 · #1700 · [기록 1 ›](<records/pr-1700.md>)",
                                 "- 11-20 solo-work · [기록 1 ›](<records/2026-11-20_solo-work.md>)"])

    def test_a_key_with_a_character_outside_the_set_is_no_link(self):
        for good in ("pr-1", "2026-11-20_a.b-c"):
            self.assertEqual(cd.record_key(None, good), good)
        for bad in ("a b", "a/b", "..", ".hidden", "a)b", "a\n", "", None, "é", "a" * 201):
            self.assertIsNone(cd.record_key(None, bad), bad)
        self.assertEqual(cd.record_key(1798, "a b"), "pr-1798")
        self.route("odd name", closed=NOW - HOUR)
        self.catch_up()
        self.assertEqual(self.raw_bullets(self.document(), "최근에 끝난 것"), ["- 11-20 odd name · 기록 1"])
        self.assertFalse((self.folder / "records").exists())

    def test_a_file_of_that_name_that_is_not_the_generators_is_untouched_and_not_linked(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        self.route("beta-1701", closed=NOW - 2 * HOUR)
        (self.folder / "records").mkdir()
        foreign = self.folder / "records" / "pr-1700.md"
        foreign.write_text("a person wrote this\n")
        os.utime(foreign, (1_700_000_000, 1_700_000_000))
        self.catch_up()
        self.assertEqual(foreign.read_text(), "a person wrote this\n")
        self.assertEqual(foreign.stat().st_mtime, 1_700_000_000)
        self.assertEqual(self.raw_bullets(self.document(), "최근에 끝난 것"),
                         ["- 11-20 alpha-1700 · #1700 · 기록 1", "- 11-20 beta-1701 · #1701 · [기록 1 ›](<records/pr-1701.md>)"])
        self.assertTrue((self.folder / "records" / "pr-1701.md").is_file())

    def test_a_page_of_the_generator_is_replaced(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        (self.folder / "records").mkdir()
        old = self.folder / "records" / "pr-1700.md"
        old.write_text(cd.RECORD_MARKER + "\nold words\n")
        self.catch_up()
        self.assertIn(f"#1700 · {self.day(NOW - HOUR)} · 기록 1 · 파일 0개", old.read_text())

    def test_a_records_that_is_a_link_is_never_written_into_and_no_line_links(self):
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        (self.folder / "records").symlink_to(elsewhere, target_is_directory=True)
        self.route("alpha-1700", closed=NOW - HOUR)
        self.catch_up()
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertNotIn("›", self.document())

    def test_a_records_that_is_a_file_is_left_as_it_is_and_no_line_links(self):
        (self.folder / "records").write_text("file")
        self.route("alpha-1700", closed=NOW - HOUR)
        self.catch_up()
        self.assertEqual((self.folder / "records").read_text(), "file")
        self.assertNotIn("›", self.document())

    def test_a_pure_unit_with_no_record_draws_as_it_always_did(self):
        unit = {"key": "rt:x", "pr": 7, "v": "v1", "title": "t", "impl": {"slug": "x"}, "count": 2,
                "closed_at": NOW, "routes": 1, "activity": NOW}
        drawn = cd.render_document("T", "s", {"active": [], "done": [unit]}, now=NOW, tz=UTC)
        self.assertIn("- 11-20 t · #7 · v1 · 기록 2\n", drawn)
        drawn = cd.render_document("T", "s", {"active": [], "done": [dict(unit, record="pr-7")]}, now=NOW, tz=UTC)
        self.assertIn("- 11-20 t · #7 · v1 · [기록 2 ›](<records/pr-7.md>)\n", drawn)

    def test_the_same_key_twice_links_only_the_first_line(self):
        self.route("same-a", closed=NOW - HOUR)
        self.route("same-b", closed=NOW - 2 * HOUR)
        for path in sorted((self.runtime / "artifact-producer/v1/cycles").glob("*.json")):   # both routes name one folder
            value = json.loads(path.read_text())
            value["locator"] = "shared-folder"
            path.write_text(json.dumps(value))
        self.catch_up()
        tails = [line.rsplit(" · ", 1)[1] for line in self.raw_bullets(self.document(), "최근에 끝난 것")]
        self.assertEqual(sorted(tails), ["[기록 1 ›](<records/shared-folder.md>)", "기록 1"])


class RecordPageTest(RecordFixture):
    def test_a_page_has_the_marker_the_head_and_one_line_per_record(self):
        self.work_and_review()
        self.files("alpha-1700", **{"extra.md": "x"})
        self.route("fix-pr1700", started=NOW - 4 * HOUR, closed=NOW - 3 * HOUR)
        self.files("fix-pr1700", **{"final_report.md": "# fix\n"})
        pub, _c = self.route("publish-v4.1.0", started=NOW - 2 * HOUR, closed=NOW - 2 * HOUR, capability="autopilot-ship",
                             evidence="# v4.1.0 — #1700\n")
        self.catch_up()
        text = self.page("pr-1700")
        lines = text.split("\n")
        self.assertEqual(lines[0], cd.RECORD_MARKER)
        self.assertEqual(lines[1], "# alpha-1700")
        total = int(re.search(r"기록 (\d+)", lines[3]).group(1))
        self.assertEqual(total, len(self.page_lines("pr-1700")))   # one line is one record
        self.assertEqual(lines[3], f"#1700 · v4.1.0 · {self.day(NOW - 2 * HOUR)} · 기록 4 · 파일 5개")
        kinds = [line.split(" · ")[0] for line in self.page_lines("pr-1700")]
        self.assertEqual(kinds, ["- 구현", "- 독립 검토", "- 고침", "- 출판"])
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것")[0].split(" · ")[-1], "기록 4")

    def test_the_head_says_doing_for_open_work_and_drops_a_missing_pr_and_version(self):
        self.route("solo-open", started=NOW - HOUR)
        self.catch_up()
        lines = self.page("2026-11-20_solo-open").split("\n")
        self.assertEqual(lines[3], "하는 중 · 기록 1 · 파일 0개")
        self.assertEqual(self.page_lines("2026-11-20_solo-open"), [f"- 구현 · {self.day(NOW - HOUR)} · solo-open · 보고 아직 없음 · 파일 0개"])

    def test_a_closed_route_with_no_report_has_no_link_and_an_open_one_says_so(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        self.catch_up()
        self.assertEqual(self.page_lines("pr-1700"), [f"- 구현 · {self.day(NOW - HOUR)} · alpha-1700 · 파일 0개"])

    def test_the_same_input_makes_the_same_bytes_and_a_changed_page_alone_is_written(self):
        self.work_and_review()
        self.route("other-1701", closed=NOW - 2 * HOUR)
        self.catch_up()
        page, other = self.folder / "records" / "pr-1700.md", self.folder / "records" / "pr-1701.md"
        for path in (page, other):
            os.utime(path, (1_700_000_000, 1_700_000_000))
        before = page.read_bytes()
        self.catch_up(now=NOW + 600)   # a later clock changes the document's stamp, not the pages
        self.assertEqual(page.read_bytes(), before)
        self.assertEqual((page.stat().st_mtime, other.stat().st_mtime), (1_700_000_000, 1_700_000_000))
        self.route("alpha-1700-again", closed=NOW + 1190)   # a new route of the PR changes its page
        self.catch_up(now=NOW + 1200)
        self.assertNotEqual(page.stat().st_mtime, 1_700_000_000)
        self.assertEqual(other.stat().st_mtime, 1_700_000_000)

    def test_a_page_is_made_only_for_a_line_the_document_shows_and_one_that_folded_stays(self):
        for index in range(4):
            self.route(f"work-{1700 + index}", closed=NOW - (index + 1) * HOUR)
        with mock.patch.object(cd, "DONE_LINES", 3):
            self.catch_up()
        pages = sorted(os.listdir(self.folder / "records"))
        self.assertEqual(pages, ["pr-1700.md", "pr-1701.md", "pr-1702.md"])   # the fourth line is folded
        self.route("work-1704", closed=NOW + 50)
        with mock.patch.object(cd, "DONE_LINES", 3):
            self.catch_up(now=NOW + 60)
        self.assertEqual(sorted(os.listdir(self.folder / "records")),
                         ["pr-1700.md", "pr-1701.md", "pr-1702.md", "pr-1704.md"])   # none removed

    def test_a_foreign_campaign_md_stops_the_pages_too(self):
        (self.folder / "campaign.md").write_text("by hand\n")
        self.route("alpha-1700", closed=NOW - HOUR)
        with redirect_stderr(io.StringIO()):
            self.catch_up()
        self.assertFalse((self.folder / "records").exists())
        self.assertEqual((self.folder / "campaign.md").read_text(), "by hand\n")

    def test_no_line_means_no_records_folder(self):
        self.catch_up()
        self.route("frame-x", closed=NOW + 50, capability="route-frame")
        self.catch_up(now=NOW + 60)
        self.assertFalse((self.folder / "records").exists())

    def test_one_page_failing_does_not_stop_the_others(self):
        self.route("alpha-1700", closed=NOW - 2 * HOUR)
        self.route("beta-1701", closed=NOW - HOUR)
        real = cd._write_document

        def flaky(folder, text, **kw):
            if kw.get("name") == "pr-1700.md":
                raise OSError("disk")
            return real(folder, text, **kw)
        with mock.patch.object(cd, "_write_document", side_effect=flaky):
            self.catch_up()
        self.assertIn("skipped error=OSError", self.err)
        self.assertTrue((self.folder / "records" / "pr-1701.md").is_file())
        self.assertTrue((self.folder / "campaign.md").is_file())

    def test_a_campaign_folder_a_begin_made_and_nothing_filled_can_be_cleared_of_the_generators_files(self):
        self.route("open-x", started=NOW - HOUR)
        self.catch_up()
        records = self.folder / "records"
        self.assertTrue(any(records.iterdir()))
        (records / "by-hand.md").write_text("mine")
        cd.remove_generated_records(self.folder)
        self.assertEqual(os.listdir(records), ["by-hand.md"])   # only the generator's pages go, and the folder stays with a file in it
        (records / "by-hand.md").unlink()
        cd.remove_generated_records(self.folder)
        self.assertFalse(records.exists())   # nothing else in it: the folder goes too
        (self.folder / "campaign.md").write_text("by hand\n")
        cd.remove_generated(self.folder)
        self.assertEqual((self.folder / "campaign.md").read_text(), "by hand\n")

    def test_the_page_writer_has_no_way_to_remove_anything(self):
        import inspect
        source = inspect.getsource(cd._write_records) + inspect.getsource(cd._Pages) + inspect.getsource(cd._GitPass)
        for word in ("unlink", "rmdir", "rmtree", "os.remove", "os.rename", "shutil"):
            self.assertNotIn(word, source)

    def test_the_markup_in_a_title_cannot_break_a_page_line_or_a_link(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        self.meta({self.cycle_of("alpha-1700"): "A [b](c) <x> `y` *z*"})
        self.catch_up()
        line = self.page_lines("pr-1700")[0]
        self.assertIn(r"A \[b\](c) \<x\> `y` \*z\*", line)

    def cycle_of(self, slug):
        for path in (self.runtime / "artifact-producer/v1/cycles").glob("*.json"):
            value = json.loads(path.read_text())
            if value["slug"] == slug:
                return value["cycle_id"]


class RecordLabelTest(unittest.TestCase):
    def unit(self, kinds, pubs=0):
        facts = [{"rid": f"rt-{i:04d}", "kind": kind, "slug": f"s{i}", "cy": f"cy{i}", "st": 100 + i, "c": 200 + i, "ok": True,
                  "pr": 5, "prs": [], "v": None, "t": None, "loc": f"loc{i}"} for i, kind in enumerate(kinds)]
        out = cd.link_units(facts, [], {}, now=NOW)
        unit = out["done"][0]
        unit["pubs"] = [{"rid": f"rt-p{i}", "st": 500 + i, "c": 600 + i, "v": "v1", "kind": "publish"} for i in range(pubs)]
        unit["count"] += pubs
        return unit

    def labels(self, unit):
        return [label for label, _kind, _member in cd.record_labels(unit)]

    def test_the_first_work_is_the_build_and_the_later_ones_fix_numbered_from_two(self):
        self.assertEqual(self.labels(self.unit(["work"])), ["구현"])
        self.assertEqual(self.labels(self.unit(["work", "work"])), ["구현", "고침"])
        self.assertEqual(self.labels(self.unit(["work", "work", "work", "work"])), ["구현", "고침 1", "고침 2", "고침 3"])

    def test_the_first_review_is_independent_and_the_later_ones_confirm_numbered_from_two(self):
        self.assertEqual(self.labels(self.unit(["work", "review"])), ["구현", "독립 검토"])
        self.assertEqual(self.labels(self.unit(["work", "review", "review"])), ["구현", "독립 검토", "닫힘 확인"])
        self.assertEqual(self.labels(self.unit(["review", "review", "review"])), ["독립 검토", "닫힘 확인 1", "닫힘 확인 2"])

    def test_lines_follow_the_start_then_the_route_id_and_a_publish_is_a_line(self):
        unit = self.unit(["work", "review", "work"], pubs=1)
        self.assertEqual(self.labels(unit), ["구현", "독립 검토", "고침", "출판"])
        self.assertEqual(len(cd.record_labels(unit)), unit["count"])
        tied = self.unit(["work", "work"])
        for member in tied["closed"]:
            member["st"] = 100
        tied["closed"].sort(key=lambda m: m["rid"], reverse=True)
        self.assertEqual([m["rid"] for _l, _k, m in cd.record_labels(tied)], ["rt-0000", "rt-0001"])

    def test_the_order_follows_the_start_of_the_first_cycle_a_route_is_given(self):
        unit = self.unit(["work", "review"])   # rt-0000 starts at 101, rt-0001 at 102
        self.assertEqual(self.labels(unit), ["구현", "독립 검토"])
        order = [(label, member["rid"]) for label, _kind, member in cd.record_labels(unit, {"rt-0001": 1})]
        self.assertEqual(order, [("독립 검토", "rt-0001"), ("구현", "rt-0000")])
        order = [label for label, _kind, _member in cd.record_labels(unit, {"rt-0001": 1, "rt-0000": 0})]
        self.assertEqual(order, ["구현", "독립 검토"])
        self.assertEqual(self.labels(unit), ["구현", "독립 검토"])   # none given: as before

    def test_a_piece_with_no_pr_has_its_first_route_as_the_build(self):
        facts = [{"rid": "rt-1", "kind": "work", "slug": "s", "cy": "c", "st": 1, "c": 2, "ok": True, "pr": None, "prs": [],
                  "v": None, "t": None, "loc": "l"}]
        unit = cd.link_units(facts, [], {}, now=NOW)["done"][0]
        self.assertEqual(self.labels(unit), ["구현"])


class RecordRoundTest(RecordFixture):
    def review_with_rounds(self, rounds=3, *, verdicts=None, closed=True):
        self.route("alpha-1700", started=NOW - 9 * HOUR, closed=NOW - 8 * HOUR)
        rid, _cycle = self.route("rv-1700-unit", started=NOW - 3 * HOUR, closed=(NOW - HOUR) if closed else None,
                                 capability="audit")
        locators = [f"2026-11-20_rv-1700-unit"]
        for index in range(1, rounds):
            _cy, loc = self.more_cycle(rid, "rv-1700-unit", started=NOW - 3 * HOUR + index * 600)
            locators.append(loc)
        verdicts = verdicts or ["수정 후 병합", "수정 후 병합", "병합 가능"]
        for index, loc in enumerate(locators):
            path = self.folder / loc / "artifacts" / "units" / "unit" / "report.md"
            path.parent.mkdir(parents=True)
            if index < len(verdicts) and verdicts[index]:
                path.write_text(f"# r\n최종 판정: {verdicts[index]}\n")
            else:
                path.write_text("# r\n")
        return rid, locators

    def review_line(self):
        return next(line for line in self.page_lines("pr-1700") if line.startswith("- 독립 검토"))

    def test_the_cycles_of_one_review_route_are_its_rounds_on_one_line(self):
        _rid, locators = self.review_with_rounds()
        self.catch_up()
        line = self.review_line()
        self.assertEqual(len(self.page_lines("pr-1700")), 2)
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것")[0].split(" · ")[-1], "기록 2")
        self.assertIn("[1라운드 \"수정 후 병합\"]", line)
        self.assertIn("[2라운드 \"수정 후 병합\"]", line)
        self.assertIn("[3라운드 \"병합 가능\"]", line)
        self.assertTrue(line.endswith("파일 3개"))
        self.assertIn("(<../" + locators[0] + "/artifacts/units/unit/report.md>)", line)

    def test_one_cycle_is_a_verdict_link_and_no_verdict_is_a_plain_report_link(self):
        self.route("rv-1700-unit", closed=NOW - HOUR, capability="audit")
        self.files("rv-1700-unit", **{"units__unit__report.md": "# r\n최종 판정: **병합 가능** — head abc123\n"})
        self.catch_up()
        self.assertEqual(self.page_lines("pr-1700"), [
            f'- 독립 검토 · {self.day(NOW - HOUR)} · ["병합 가능"](<../2026-11-20_rv-1700-unit/artifacts/units/unit/report.md>) · 파일 1개'])
        self.files("rv-1700-unit", **{"units__unit__report.md": "# r\nno verdict here\n"})
        self.cache_reset()
        self.catch_up(now=NOW + 5)
        self.assertIn("· [보고](<../2026-11-20_rv-1700-unit/artifacts/units/unit/report.md>) ·", self.page_lines("pr-1700")[0])

    def cache_reset(self):
        for path in (self.runtime / "campaign-document").glob("camp_*.json"):
            path.unlink()
        (self.runtime / "campaign-document" / "watermark.json").write_text('{"v": 1, "ns": 1}')

    def test_the_verdict_is_read_from_the_first_eight_lines_only_and_loses_the_head_tail(self):
        self.assertEqual(cd.verdict_of("# a\n최종 판정: 병합 가능 — head abc\n"), "병합 가능")
        self.assertEqual(cd.verdict_of("# a\n최종 판정 : `수정 후 병합` – head 12ab\n"), "수정 후 병합")
        self.assertEqual(cd.verdict_of("\n" * 8 + "최종 판정: 늦음\n"), None)
        self.assertEqual(cd.verdict_of("최종 판정: " + "가" * 100).__len__(), cd.VERDICT_CHARS)
        self.assertIsNone(cd.verdict_of("no verdict\n"))

    def test_an_open_last_round_with_no_report_says_so_and_an_earlier_round_with_none_is_plain(self):
        rid, locators = self.review_with_rounds(verdicts=["수정 후 병합", None, None], closed=False)
        import shutil
        shutil.rmtree(self.folder / locators[2] / "artifacts")
        (self.folder / locators[1] / "artifacts" / "units" / "unit" / "report.md").unlink()
        self.catch_up()
        line = self.review_line()
        self.assertIn("[1라운드 \"수정 후 병합\"]", line)
        self.assertIn(" · 2라운드 · ", line)
        self.assertIn(" · 3라운드 보고 아직 없음 · ", line)

    def test_a_fix_between_two_rounds_of_a_review_stands_after_the_review_s_first_round(self):
        self.route("alpha-1700", started=NOW - 9 * HOUR, closed=NOW - 8 * HOUR)
        rid, _cy = self.route("rv-1700-unit", started=NOW - 7 * HOUR, closed=NOW - 6 * HOUR, capability="audit")
        self.route("fix-1700", started=NOW - 5 * HOUR, closed=NOW - 4 * HOUR)
        self.more_cycle(rid, "rv-1700-unit", started=NOW - 3 * HOUR)   # the second round, after the fix
        self.catch_up()
        lines = self.page_lines("pr-1700")
        self.assertEqual([line.split(" · ")[0] for line in lines], ["- 구현", "- 독립 검토", "- 고침"])
        self.assertIn(" · 1라운드 · 2라운드 · ", lines[1])

    def test_the_review_that_began_first_is_the_independent_one_even_when_another_round_ran_later(self):
        self.route("alpha-1700", started=NOW - 9 * HOUR, closed=NOW - 8 * HOUR)
        first, _c = self.route("rv-1700-unit", started=NOW - 7 * HOUR, closed=NOW - 6 * HOUR, capability="audit")
        self.route("rv-1700-close-unit", started=NOW - 4 * HOUR, closed=NOW - 3 * HOUR, capability="audit")
        self.more_cycle(first, "rv-1700-unit", started=NOW - 2 * HOUR)
        self.catch_up()
        lines = self.page_lines("pr-1700")
        self.assertEqual([line.split(" · ")[0] for line in lines], ["- 구현", "- 독립 검토", "- 닫힘 확인"])
        self.assertIn("2라운드", lines[1])

    def aged_cache(self):
        """The saved list as an older version left it: a member it had drawn again is whole and plain (no
        `cycles`, no `rk`), and `rounds` says its reviews' cycles were read."""
        cache_path = next((self.runtime / "campaign-document").glob("camp_*.json"))
        saved = json.loads(cache_path.read_text())
        for member in saved["members"].values():
            member.pop("cycles", None)
            member.pop("rk", None)
        saved["rounds"] = True
        cache_path.write_text(json.dumps(saved))
        return cache_path

    def test_a_list_an_older_version_replaced_a_review_in_gets_its_rounds_back_once(self):
        rid, locators = self.review_with_rounds()
        self.catch_up()
        cache_path = self.aged_cache()
        (self.folder / "records" / "pr-1700.md").unlink()
        self.catch_up(now=self.bump(1))   # something moved, so the campaign is drawn again
        self.assertIn("[3라운드", (self.folder / "records" / "pr-1700.md").read_text())
        saved = json.loads(cache_path.read_text())
        self.assertEqual(len(saved["members"][rid]["cycles"]), 3)
        self.assertTrue(all("rk" in member for member in saved["members"].values() if member["cap"] == "audit"))
        with mock.patch.object(cd.store, "list_cycle_records", wraps=cd.store.list_cycle_records) as spy:
            self.catch_up(now=self.bump(2))
        self.assertEqual(spy.call_count, 0)   # the saved list is whole now: no record is listed again

    def test_a_review_with_one_cycle_is_read_once_and_then_marked_read(self):
        self.route("rv-1700-unit", closed=NOW - HOUR, capability="audit")
        self.catch_up()
        cache_path = self.aged_cache()
        with mock.patch.object(cd.store, "list_cycle_records", wraps=cd.store.list_cycle_records) as spy:
            self.catch_up(now=self.bump(1))
        self.assertEqual(spy.call_count, 1)
        member = next(m for m in json.loads(cache_path.read_text())["members"].values() if m["cap"] == "audit")
        self.assertEqual(len(member["rk"]), 2)
        with mock.patch.object(cd.store, "list_cycle_records", wraps=cd.store.list_cycle_records) as spy:
            self.catch_up(now=self.bump(2))
        self.assertEqual(spy.call_count, 0)

    def test_a_new_cycle_joins_the_route_and_the_cycle_a_line_stands_on_stays(self):
        rid, locators = self.review_with_rounds(rounds=2, verdicts=["수정 후 병합", "병합 가능"])
        self.catch_up()
        cache_path = next((self.runtime / "campaign-document").glob("camp_*.json"))
        before = json.loads(cache_path.read_text())["members"][rid]["cy"]
        _cy, loc = self.more_cycle(rid, "rv-1700-unit", started=NOW - 3 * HOUR - 30, state="draft", written=NOW + 30)   # older and not sealed
        path = self.folder / loc / "artifacts" / "units" / "unit" / "report.md"
        path.parent.mkdir(parents=True)
        path.write_text("# r\n최종 판정: 병합 불가\n")
        self.catch_up(now=NOW + 60)
        member = json.loads(cache_path.read_text())["members"][rid]
        self.assertEqual(len(member["cycles"]), 3)
        self.assertEqual(member["cy"], before)
        self.assertIn("[1라운드 \"병합 불가\"]", self.review_line())


class RecordReportTest(RecordFixture):
    def test_the_evidence_wins_then_the_final_report_then_a_unit_report_then_nothing(self):
        self.route("a-1701", closed=NOW - HOUR, evidence="# evidence\n")
        self.files("a-1701", **{"final_report.md": "f", "units__u__report.md": "u"})
        self.route("b-1702", closed=NOW - 2 * HOUR)
        self.files("b-1702", **{"final_report.md": "f", "units__u__report.md": "u"})
        self.route("c-1703", closed=NOW - 3 * HOUR)
        self.files("c-1703", **{"units__b__report.md": "u", "units__a__report.md": "u"})
        self.route("d-1704", closed=NOW - 4 * HOUR)
        self.files("d-1704", **{"note.md": "n"})
        self.catch_up()
        self.assertRegex(self.page_lines("pr-1701")[0], r"\[보고\]\(<\.\./\.\./\.\./reports/rt-\w+\.md>\)")
        self.assertIn("(<../2026-11-20_b-1702/artifacts/final_report.md>)", self.page_lines("pr-1702")[0])
        self.assertIn("(<../2026-11-20_c-1703/artifacts/units/a/report.md>)", self.page_lines("pr-1703")[0])
        self.assertNotIn("](<", self.page_lines("pr-1704")[0])
        self.assertNotIn("보고 아직 없음", self.page_lines("pr-1704")[0])

    def test_a_link_stands_only_on_a_regular_file_inside_the_root(self):
        outside = self.root.parent / "outside.md"
        outside.write_text("out")
        self.route("a-1701", closed=NOW - HOUR)
        self.route("b-1702", closed=NOW - 2 * HOUR)
        self.files("a-1701", **{"x.md": "x"})
        (self.cycle_dir("a-1701") / "artifacts" / "final_report.md").symlink_to(outside)   # a link to a file outside
        (self.cycle_dir("b-1702") / "artifacts").mkdir(parents=True)
        (self.cycle_dir("b-1702") / "artifacts" / "final_report.md").mkdir()   # a folder with the name
        self.catch_up()
        for key in ("pr-1701", "pr-1702"):
            self.assertNotIn("](<", self.page_lines(key)[0])
        self.assertNotIn("outside", self.page("pr-1701"))

    def test_the_path_is_relative_to_the_page_folder(self):
        self.route("a-1701", closed=NOW - HOUR)
        self.files("a-1701", **{"final_report.md": "f"})
        self.catch_up()
        self.assertIn("[보고](<../2026-11-20_a-1701/artifacts/final_report.md>)", self.page("pr-1701"))
        target = (self.folder / "records" / "../2026-11-20_a-1701/artifacts/final_report.md").resolve()
        self.assertTrue(target.is_file())

    def test_a_target_with_a_character_a_link_cannot_carry_is_left_unlinked(self):
        self.assertEqual(cd._link("보고", "a>b"), "보고")
        self.assertEqual(cd._link("보고", "a\nb"), "보고")
        self.assertEqual(cd._link("보고", "a b/c.md"), "[보고](<a b/c.md>)")

    def test_the_files_of_a_closed_cycle_are_counted_once_and_an_open_one_every_pass(self):
        self.route("a-1701", closed=NOW - HOUR)
        self.files("a-1701", **{"one.md": "1", "sub__two.md": "2"})
        (self.cycle_dir("a-1701") / "artifacts" / "link.md").symlink_to(self.cycle_dir("a-1701") / "artifacts" / "one.md")
        self.route("open-1702", started=NOW - HOUR)
        self.files("open-1702", **{"one.md": "1"})
        with mock.patch.object(cd, "_count_files", wraps=cd._count_files) as spy:
            self.catch_up()
            first = spy.call_count
            self.route("c-1703", closed=NOW + 50)
            self.files("c-1703", **{"one.md": "1"})
            self.catch_up(now=NOW + 60)
        self.assertEqual(self.page_lines("pr-1701")[0].rsplit(" · ", 1)[1], "파일 2개")   # a link is not a file
        self.assertEqual(first, 2)
        self.assertEqual(spy.call_count - first, 2)   # the open route and the new one; the closed one is not counted again


class RecordFolderMakingTest(RecordFixture):
    def test_the_records_folder_is_made_inside_a_campaign_folder_and_never_the_folders_above_it(self):
        gone = self.root / "campaigns" / "gone-campaign"
        cd._write_records(gone, {"pr-1": "text"})
        self.assertFalse(gone.exists())
        cd._write_records(self.folder, {"pr-1": f"{cd.RECORD_MARKER}\ntext\n"})   # the campaign folder is there: records is made
        self.assertTrue((self.folder / "records" / "pr-1.md").is_file())

    def test_a_campaign_folder_that_goes_away_during_the_pass_is_not_made_again(self):
        self.route("alpha-1700", closed=NOW - HOUR)
        real = cd.collect

        def collect_then_lose_the_folder(*a, **kw):
            result = real(*a, **kw)
            import shutil
            shutil.rmtree(self.folder)
            return result
        with mock.patch.object(cd, "collect", side_effect=collect_then_lose_the_folder):
            self.catch_up()
        self.assertFalse(self.folder.exists())


class RecordSupervisorTest(RecordFixture):
    def inputs(self, **names):
        folder = self.root / "research" / self.folder.name / "inputs"
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in names.items():
            (folder / name).write_text(text)
        return folder

    def test_the_names_that_carry_the_pr_with_no_digit_beside_it(self):
        names = ["RV-PR1798-1119.md", "RV-PR17980-1.md", "RV-XPR1798.md", "RV-CLOSE-PR1798-R3.md", "RV-PR1798.txt",
                 "CP-PR1798.md", "RV-PR179.md", "RV-1798.md"]
        self.assertEqual(cd.supervisor_names(names, 1798), ["RV-CLOSE-PR1798-R3.md", "RV-PR1798-1119.md"])
        self.assertEqual(cd.supervisor_names(names, None), [])

    def test_the_supervisors_records_are_lines_of_the_page_but_not_in_the_count(self):
        self.route("alpha-1798", closed=NOW - HOUR)
        self.route("beta-1799", closed=NOW - 2 * HOUR)
        self.inputs(**{"RV-PR1798-1119.md": "# a\n최종 판정: 병합 가능\n", "RV-CLOSE-PR1798-R3.md": "# b\n", "RV-PR1799.md": "x"})
        self.catch_up()
        lines = self.page_lines("pr-1798")
        self.assertEqual(lines[1:], [
            '- 감독의 검토 기록 · [RV-CLOSE-PR1798-R3.md](<../../../research/2026-11-01_demo/inputs/RV-CLOSE-PR1798-R3.md>)',
            '- 감독의 검토 기록 · "병합 가능" · [RV-PR1798-1119.md](<../../../research/2026-11-01_demo/inputs/RV-PR1798-1119.md>)'])
        self.assertIn("기록 1 · 파일 2개", self.page("pr-1798"))   # the lines are files of the page, not records
        self.assertEqual(self.bullets(self.document(), "최근에 끝난 것")[0].split(" · ")[-1], "기록 1")

    def test_no_folder_is_no_line_and_a_piece_with_no_pr_has_none(self):
        self.route("alpha-1798", closed=NOW - HOUR)
        self.route("solo", closed=NOW - 2 * HOUR)
        self.catch_up()
        self.assertEqual(len(self.page_lines("pr-1798")), 1)
        self.inputs(**{"RV-PR1798.md": "x"})
        self.catch_up(now=NOW + 5)
        self.assertEqual(len(self.page_lines("pr-1798")), 1)   # nothing moved: the pages are as they were
        self.route("zzz-1798b", closed=NOW + 50)
        self.catch_up(now=NOW + 60)
        self.assertEqual(len(self.page_lines("pr-1798")), 2)
        self.assertEqual(len(self.page_lines("2026-11-20_solo")), 1)

    def test_the_folder_is_listed_once_per_campaign_pass(self):
        self.inputs(**{"RV-PR1798.md": "x"})
        for number in (1798, 1799, 1800):
            self.route(f"w-{number}", closed=NOW - number)
        with mock.patch.object(cd, "_supervisor_listing", wraps=cd._supervisor_listing) as spy:
            self.catch_up()
        self.assertEqual(spy.call_count, 1)


import subprocess  # noqa: E402


class GitFixture(RecordFixture):
    GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]

    def setUp(self):
        super().setUp()
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_AUTHOR_DATE": "2026-11-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-11-01T00:00:00Z"}
        self.repo = self.root.parent / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.commit("start")
        self.start = self.rev("HEAD")

    def git(self, *args, repo=None, date=None):
        env = self.env if date is None else {**self.env, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        done = subprocess.run([*self.GIT, "-C", str(repo or self.repo), *args], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def commit(self, name, date=None, repo=None):
        (Path(repo or self.repo) / name).write_text(name)
        self.git("add", name, repo=repo, date=date)
        self.git("commit", "-q", "-m", name, repo=repo, date=date)

    def rev(self, ref):
        return self.git("rev-parse", ref)

    def branch_commit(self, branch, name):
        """A commit on `branch` (made from `main`'s tip when new); returns its hash."""
        if branch in self.git("branch", "--list", branch):
            self.git("checkout", "-q", branch)
        else:
            self.git("checkout", "-q", "-b", branch, "main")
        self.commit(name)
        head = self.rev("HEAD")
        self.git("checkout", "-q", "main")
        return head

    def merge(self, branch, subject, date=None):
        self.git("merge", "--no-ff", "-q", "-m", subject, branch, date=date)

    def asked(self, calls, subcommand):
        return [c for c in calls if subcommand in c]

    def pr_of(self, rid):
        return self.cache()["git"][rid]["pr"]

    def closed_work(self, slug, head, *, source=None, repo="same", closed=None, **kw):
        rid, cycle = self.route(slug, closed=closed or NOW - HOUR, **kw)
        self.link_commits(rid, head, source if source is not None else self.start, repo)
        return rid, cycle

    def link_commits(self, rid, head, source, repo="same"):
        path = self.runtime / "routes" / f"{rid}.json"
        route = json.loads(path.read_text())
        route["source_commit"] = source
        route["source_repository"] = {"worktree": {"path": str(self.repo if repo == "same" else repo)}}
        path.write_text(json.dumps(route))
        out = self.runtime / "routes" / f"{rid}.outcome.json"
        value = json.loads(out.read_text())
        value["head_commit"] = head
        mtime = out.stat().st_mtime
        out.write_text(json.dumps(value))
        os.utime(out, (mtime, mtime))

    def recorded(self):
        """The git subcommands one pass runs."""
        calls = []
        real = subprocess.run

        def spy(args, *a, **kw):
            calls.append(args)
            return real(args, *a, **kw)
        return calls, mock.patch.object(cd.subprocess, "run", side_effect=spy)

    def cache(self):
        return json.loads(next((self.runtime / "campaign-document").glob("camp_*.json")).read_text())

    def lines(self):
        return self.bullets(self.document(), "최근에 끝난 것")


class GitLinkTest(GitFixture):
    def test_a_route_that_closed_before_its_pr_is_joined_to_it_and_the_first_build_names_the_line(self):
        head = self.branch_commit("feature", "impl")
        self.closed_work("build-it", head, started=NOW - 9 * HOUR, closed=NOW - 8 * HOUR)
        self.branch_commit("feature", "fix")
        self.merge("feature", "Merge pull request #1777 from owner/feature")
        self.route("fix-pr1777", started=NOW - 5 * HOUR, closed=NOW - 4 * HOUR)
        self.catch_up()
        self.assertEqual(self.lines(), ["- 11-20 build-it · #1777 · 기록 2"])
        self.assertEqual(self.cache()["git"][next(iter(self.cache()["git"]))]["pr"], 1777)
        self.assertEqual(self.page_lines("pr-1777")[0].split(" · ")[0:1], ["- 구현"])
        self.assertEqual(len(self.page_lines("pr-1777")), 2)

    def test_the_first_pull_request_merge_is_taken_and_other_merges_are_skipped(self):
        head = self.branch_commit("feature", "impl")
        self.closed_work("build-it", head)
        self.git("checkout", "-q", "feature")
        self.merge("main", "Merge branch 'main' into feature")
        self.git("checkout", "-q", "-q", "main")
        self.commit("main-moves")
        self.git("checkout", "-q", "feature")
        self.git("merge", "--no-ff", "-q", "-m", "Merge remote-tracking branch 'origin/main' into feature", "main")
        self.git("checkout", "-q", "main")
        self.merge("feature", "Merge pull request #1778")   # a subject with nothing after the number
        self.catch_up()
        self.assertEqual(self.lines(), ["- 11-20 build-it · #1778 · 기록 1"])

    def test_pr_from_merges_reads_only_the_pull_request_merge_that_brought_the_head_in(self):
        log = ("a1 p0 p1\tMerge branch 'x' into y\n"
               "b1 p0 p1\tMerge remote-tracking branch 'o/m'\n"
               "c1 p0 p1\tMerge pull request #12 from a/b\n"
               "d1 p0 p1\tMerge pull request #13 from a/c")
        self.assertEqual(cd.pr_from_merges(log, "H"), 12)
        self.assertIsNone(cd.pr_from_merges("a\tMerge pull request #x\nb\tMerge branch #5", "H"))
        self.assertIsNone(cd.pr_from_merges("", "H"))
        self.assertIsNone(cd.pr_from_merges("c1 p0\tMerge pull request #12", "H"))   # one parent: not a merge
        # the first parent is the head or a commit already seen (a descendant of the head): not the merge that brought it in
        self.assertIsNone(cd.pr_from_merges("c1 H p1\tMerge pull request #12", "H"))
        stacked = "k1 H\tfix\nm1 k1 z1\tMerge pull request #61 from o/f2\nm2 base0 m1\tMerge pull request #60 from o/f1"
        self.assertEqual(cd.pr_from_merges(stacked, "H"), 60)

    def test_a_route_not_merged_yet_is_not_joined_and_is_not_asked_again_until_the_base_moves(self):
        head = self.branch_commit("feature", "impl")
        rid, _c = self.closed_work("build-it", head)
        self.catch_up()
        self.assertEqual(self.lines(), ["- 11-20 build-it · 기록 1"])
        entry = self.cache()["git"][rid]
        self.assertEqual((entry["pr"], entry["base"]), (None, self.rev("main")))
        calls, patch = self.recorded()
        with patch:
            self.catch_up(now=self.bump(1))
        self.assertTrue(calls)   # the base is looked up
        self.assertEqual([c for c in calls if "log" in c], [])   # the base is the same: no `git log`
        self.commit("main-moves")
        calls, patch = self.recorded()
        with patch:
            self.catch_up(now=self.bump(2))
        self.assertEqual(len([c for c in calls if "log" in c]), 1)   # the base moved: asked again, still not merged
        self.merge("feature", "Merge pull request #1779 from o/feature")
        self.catch_up(now=self.bump(3))
        self.assertIn("- 11-20 build-it · #1779 · 기록 1", self.lines())
        calls, patch = self.recorded()
        with patch:
            self.catch_up(now=self.bump(4))
        self.assertEqual([c for c in calls if "log" in c], [])   # a found number is for good
        self.assertEqual(self.cache()["git"][rid]["pr"], 1779)

    def test_a_route_that_made_no_commit_is_not_asked(self):
        self.branch_commit("feature", "impl")
        self.merge("feature", "Merge pull request #1777 from o/feature")
        self.closed_work("design-it", self.start, source=self.start)
        calls, patch = self.recorded()
        with patch:
            self.catch_up()
        self.assertEqual(calls, [])
        self.assertEqual(self.lines(), ["- 11-20 design-it · 기록 1"])

    def test_a_number_the_route_already_has_is_never_changed_and_never_asked(self):
        head = self.branch_commit("feature", "impl")
        self.merge("feature", "Merge pull request #1777 from o/feature")
        self.closed_work("build-pr1700", head)
        calls, patch = self.recorded()
        with patch:
            self.catch_up()
        self.assertEqual(calls, [])
        self.assertEqual(self.lines(), ["- 11-20 build-pr1700 · #1700 · 기록 1"])

    def test_only_a_few_are_asked_in_a_pass_newest_first_and_the_next_pass_asks_the_rest(self):
        heads = []
        for index in range(4):
            head = self.branch_commit(f"f{index}", f"impl{index}")
            heads.append(head)
            self.merge(f"f{index}", f"Merge pull request #{1780 + index} from o/f{index}")
        for index, head in enumerate(heads):
            self.closed_work(f"w{index}-x", head, closed=NOW - (index + 1) * HOUR)
        with mock.patch.object(cd, "GIT_BUDGET", 5):   # the base lookup (3 `rev-parse` here) counts too: then 2 `git log`
            self.catch_up()
        joined = [line for line in self.lines() if "#178" in line]
        self.assertEqual(sorted(re.search(r"#(\d+)", line).group(1) for line in joined), ["1780", "1781"])   # the two newest closes
        with mock.patch.object(cd, "GIT_BUDGET", 5):
            self.catch_up(now=self.bump(1))
        self.assertEqual(len([line for line in self.lines() if "#178" in line]), 4)

    def test_a_working_tree_that_is_gone_is_asked_in_the_repository_that_holds_the_root(self):
        head = self.branch_commit("feature", "impl")
        self.merge("feature", "Merge pull request #1779 from o/feature")
        outer = self.root.parent   # a repository around the artifact root, holding the same history
        self.git("init", "-q", "-b", "other", repo=outer)
        self.git("fetch", "-q", str(self.repo), "main:main", repo=outer)
        rid, _c = self.closed_work("build-it", head, repo=str(outer / "gone-worktree"))
        calls, patch = self.recorded()
        with patch:
            self.catch_up()
        self.assertTrue(calls)
        self.assertTrue(all(c[3] == str(self.root) for c in calls))
        self.assertEqual(self.cache()["git"][rid]["pr"], 1779)

    def test_git_missing_or_not_a_repository_leaves_the_document_as_it_was(self):
        head = "a" * 40
        rid, _c = self.closed_work("build-it", head)
        with mock.patch.object(cd.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.catch_up()
        self.assertEqual(self.lines(), ["- 11-20 build-it · 기록 1"])
        self.assertIsNone(self.cache()["git"][rid]["base"])
        shutil = __import__("shutil")
        shutil.rmtree(self.repo / ".git")
        self.catch_up(now=self.bump(1))
        self.assertIn("- 11-20 build-it · 기록 1", self.lines())

    def test_a_timeout_is_not_remembered_and_the_next_pass_asks_again(self):
        head = self.branch_commit("feature", "impl")
        rid, _c = self.closed_work("build-it", head)
        real = cd.subprocess.run

        def slow(args, *a, **kw):
            if "log" in args:
                raise subprocess.TimeoutExpired(args, 5)
            return real(args, *a, **kw)
        with mock.patch.object(cd.subprocess, "run", side_effect=slow):
            self.catch_up()
        self.assertIsNone(self.cache()["git"][rid]["base"])
        self.merge("feature", "Merge pull request #1779 from o/feature")
        self.catch_up(now=self.bump(1))
        self.assertIn("- 11-20 build-it · #1779 · 기록 1", self.lines())

    def test_a_commit_git_does_not_know_is_remembered_like_a_route_not_merged(self):
        rid, _c = self.closed_work("build-it", "b" * 40)
        self.catch_up()
        self.assertEqual(self.cache()["git"][rid]["base"], self.rev("main"))
        self.assertEqual(self.lines(), ["- 11-20 build-it · 기록 1"])

    def test_only_rev_parse_and_log_are_run_nothing_is_fetched_and_no_lock_is_left(self):
        head = self.branch_commit("feature", "impl")
        self.closed_work("build-it", head)
        calls, patch = self.recorded()
        with patch:
            self.catch_up()
        self.assertTrue(calls)
        self.assertEqual({c[4] for c in calls}, {"rev-parse", "log"})
        self.assertTrue(all(c[:3] == ["git", "--no-pager", "-C"] for c in calls))
        self.assertEqual(list((self.repo / ".git").glob("*.lock")), [])

    def outer_git(self, present):
        """`os.path.lexists` as it is, except that a `.git` outside this test's temporary folder is there or
        not as `present` says: whatever repository the machine has above the temporary folders (a CI's
        checkout, this machine's `/tmp/.git`) is made to show, or hidden."""
        real, inside = os.path.lexists, str(self.root.parent) + os.sep

        def lexists(path):
            path = str(path)
            return present if path.endswith(os.sep + ".git") and not path.startswith(inside) else real(path)
        return mock.patch.object(cd.os.path, "lexists", side_effect=lexists)

    def test_a_folder_with_no_git_in_or_above_it_is_not_asked_and_a_fatal_answer_stops_the_search(self):
        nowhere = self.root.parent / "nowhere"
        nowhere.mkdir()
        with self.outer_git(True):   # a machine whose temporary folders sit inside a repository: the folder is "inside" it
            self.assertTrue(cd._inside_repository(str(nowhere)))
        with self.outer_git(False), mock.patch.object(cd.subprocess, "run", side_effect=AssertionError("git was started")):
            self.assertEqual(cd._GitPass().base(str(nowhere)), "")
            self.assertFalse(cd._inside_repository(str(nowhere)))
            self.assertTrue(cd._inside_repository(str(self.repo / "sub" / "folder")))   # this test's own repository is seen
        fatal = mock.Mock(returncode=128, stdout="")
        with mock.patch.object(cd.subprocess, "run", return_value=fatal) as run:
            git = cd._GitPass()
            self.assertEqual(git.base(str(self.repo)), "")
        self.assertEqual(run.call_count, 1)   # not a repository for one ref is not a repository for the next
        missing = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(cd.subprocess, "run", return_value=missing) as run:
            self.assertEqual(cd._GitPass().base(str(self.repo)), "")
        self.assertEqual(run.call_count, 3)   # no such ref: the next one is tried

    def test_the_environment_git_runs_in_is_clean_and_read_only(self):
        seen = {}

        def look(args, *a, **kw):
            seen.update(kw["env"])
            raise FileNotFoundError("git")
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nowhere", "GIT_WORK_TREE": "/nowhere", "GIT_INDEX_FILE": "/nowhere"}):
            with mock.patch.object(cd.subprocess, "run", side_effect=look):
                cd._GitPass().base(str(self.repo))
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.assertNotIn(name, seen)
        self.assertEqual((seen["GIT_OPTIONAL_LOCKS"], seen["GIT_NO_LAZY_FETCH"], seen["GIT_TERMINAL_PROMPT"]), ("0", "1", "0"))

    def test_the_base_is_the_first_of_origin_head_origin_main_main_that_resolves_once_per_repository(self):
        self.git("update-ref", "refs/remotes/origin/main", self.start)
        self.commit("main-moves")
        git = cd._GitPass()
        self.assertEqual(git.base(str(self.repo)), self.start)   # origin/main wins over main
        calls = git.calls
        git.base(str(self.repo))
        self.assertEqual(git.calls, calls)

    def test_a_commit_pushed_straight_to_main_has_no_pr_even_when_a_pull_request_merges_later(self):
        self.commit("pushed-straight")   # on main, after `start`
        head = self.rev("HEAD")
        self.git("checkout", "-q", "-b", "other", self.start)
        self.commit("other-work")
        self.git("checkout", "-q", "main")
        self.merge("other", "Merge pull request #50 from o/other")   # its first parent is the head itself
        rid, _c = self.closed_work("build-it", head)
        self.catch_up()
        self.assertEqual(self.lines(), ["- 11-20 build-it · 기록 1"])
        self.assertIsNone(self.pr_of(rid))

    def test_a_stacked_pull_request_joins_the_one_that_reached_main_not_the_one_beneath_it(self):
        self.git("checkout", "-q", "-b", "f1", "main")
        self.commit("impl")
        head = self.rev("HEAD")
        self.commit("more-on-f1")
        self.git("checkout", "-q", "-b", "f2")
        self.commit("on-top")
        self.git("checkout", "-q", "f1")
        self.merge("f2", "Merge pull request #61 from o/f2")   # a merge into f1, after the head
        self.git("checkout", "-q", "main")
        self.merge("f1", "Merge pull request #60 from o/f1")
        rid, _c = self.closed_work("build-it", head)
        self.catch_up()
        self.assertEqual(self.pr_of(rid), 60)

    def test_a_merge_of_main_into_the_branch_on_the_way_does_not_hide_the_pull_request(self):
        head = self.branch_commit("feature", "impl")
        self.git("checkout", "-q", "feature")
        self.git("merge", "--no-ff", "-q", "-m", "Merge branch 'main' into feature", "main")
        self.git("checkout", "-q", "main")
        self.merge("feature", "Merge pull request #70 from o/feature")
        rid, _c = self.closed_work("build-it", head)
        self.catch_up()
        self.assertEqual(self.pr_of(rid), 70)

    def test_a_merge_dated_before_the_commit_it_was_made_on_is_still_judged_after_it(self):
        self.commit("pushed-straight", date="2026-11-01T00:00:00Z")
        head = self.rev("HEAD")
        side = self.branch_commit("feature", "work-for-a-pr")
        self.commit("x-on-main", date="2026-11-25T00:00:00Z")   # the commit the pull request merge is made on
        x = self.rev("HEAD")
        self.commit("y-on-main", date="2026-11-26T00:00:00Z")
        self.git("checkout", "-q", "-b", "side", x)
        self.merge(side, "Merge pull request #50 from o/feature", date="2026-11-20T00:00:00Z")   # older than its first parent
        self.git("checkout", "-q", "main")
        self.merge("side", "Merge branch 'side'", date="2026-11-27T00:00:00Z")
        rid, _c = self.closed_work("build-it", head)
        self.catch_up()
        self.assertIsNone(self.pr_of(rid))   # by date alone X would come after the merge and #50 would be taken

    def test_a_value_that_is_not_a_full_commit_hash_is_never_handed_to_git(self):
        out = self.root.parent / "written-by-git"
        bad = {"option": f"--output={out}", "name": "HEAD", "branch": "main", "short": "a" * 39, "upper": "A" * 40,
               "newline": "a" * 40 + "\n", "long": "a" * 41}
        for index, (label, value) in enumerate(bad.items()):
            self.closed_work(f"w{index}-{label}", value, closed=NOW - (index + 1) * HOUR)
        self.closed_work("w-source", "a" * 40, source="HEAD")
        calls, patch = self.recorded()
        with patch:
            self.catch_up()
        self.assertEqual(calls, [])
        self.assertFalse(out.exists())
        self.assertFalse([line for line in self.lines() if "#" in line])

    def test_the_log_is_asked_with_the_range_last_after_the_end_of_options_and_no_signature_or_merge_filter(self):
        head = self.branch_commit("feature", "impl")
        self.merge("feature", "Merge pull request #1777 from o/feature")
        self.closed_work("build-it", head)
        calls, patch = self.recorded()
        with patch:
            self.catch_up()
        (log,) = self.asked(calls, "log")
        self.assertEqual(log[log.index("--end-of-options") + 1:], [f"{head}..{self.rev('main')}"])
        for flag in ("--ancestry-path", "--topo-order", "--reverse", "--no-show-signature"):
            self.assertIn(flag, log)
        self.assertNotIn("--merges", log)

    def test_a_timeout_ends_git_for_the_pass_and_the_next_pass_asks_again(self):
        heads = []
        for index in range(2):
            head = self.branch_commit(f"f{index}", f"impl{index}")
            heads.append(head)
            self.merge(f"f{index}", f"Merge pull request #{1790 + index} from o/f{index}")
        rids = [self.closed_work(f"w{index}-x", head, closed=NOW - (index + 1) * HOUR)[0] for index, head in enumerate(heads)]
        real = cd.subprocess.run
        calls = []

        def slow(args, *a, **kw):
            calls.append(args)
            if "log" in args:
                raise subprocess.TimeoutExpired(args, 5)
            return real(args, *a, **kw)
        with mock.patch.object(cd.subprocess, "run", side_effect=slow):
            self.catch_up()
        self.assertEqual(len(self.asked(calls, "log")), 1)   # the second route was not asked after the first timed out
        self.assertEqual([self.cache()["git"][rid]["base"] for rid in rids], [None, None])   # a timeout is not remembered
        self.catch_up(now=self.bump(1))
        self.assertEqual(sorted(self.pr_of(rid) for rid in rids), [1790, 1791])

    def test_a_timeout_of_the_base_lookup_ends_git_for_the_pass_across_repositories(self):
        other = self.root.parent / "repo-two"
        other.mkdir()
        self.git("init", "-q", "-b", "main", repo=other)
        self.commit("start-two", repo=other)
        source = self.git("rev-parse", "HEAD", repo=other)
        self.commit("work-two", repo=other)
        head = self.git("rev-parse", "HEAD", repo=other)
        self.git("checkout", "-q", "-b", "f", source, repo=other)
        self.commit("pr-work", repo=other)
        self.git("checkout", "-q", "main", repo=other)
        self.git("merge", "--no-ff", "-q", "-m", "Merge pull request #1800 from o/f", "f", repo=other)
        own = self.branch_commit("feature", "impl")
        self.merge("feature", "Merge pull request #1801 from o/feature")
        a, _ca = self.closed_work("w0-x", own, closed=NOW - HOUR)
        b, _cb = self.closed_work("w1-x", head, source=source, repo=str(other), closed=NOW - 2 * HOUR)
        real = cd.subprocess.run
        calls = []

        def slow(args, *a, **kw):
            calls.append(args)
            if "rev-parse" in args:
                raise subprocess.TimeoutExpired(args, 5)
            return real(args, *a, **kw)
        with mock.patch.object(cd.subprocess, "run", side_effect=slow):
            self.catch_up()
        self.assertEqual(len(self.asked(calls, "rev-parse")), 1)
        self.assertEqual(self.asked(calls, "log"), [])
        self.catch_up(now=self.bump(1))
        self.assertEqual((self.pr_of(a), self.pr_of(b)), (1801, None))   # `b` was merged on `other`'s main, not the one asked first

    def test_the_budget_counts_every_git_start_and_none_left_starts_nothing(self):
        git = cd._GitPass(budget=2)
        self.assertEqual(git.base(str(self.repo)), "")   # three refs to try, two starts allowed
        self.assertEqual(git.calls, 2)
        with mock.patch.object(cd.subprocess, "run", side_effect=AssertionError("git was started")):
            off = cd._GitPass(budget=0)
            self.assertEqual(off.base(str(self.repo)), "")
            self.assertEqual(off.first_pr(str(self.repo), "a" * 40, "b" * 40), ("timeout", None))
        self.assertEqual(off.calls, 0)

    def test_a_pass_in_a_test_process_starts_no_git_and_one_called_directly_still_does(self):
        head = self.branch_commit("feature", "impl")
        self.merge("feature", "Merge pull request #1777 from o/feature")
        rid, _c = self.closed_work("build-it", head)
        calls, patch = self.recorded()
        with patch, mock.patch.object(entrance, "refresh"):
            entrance.request_refresh(self.root)   # a test process refreshes in place, and starts no child
        self.assertEqual(calls, [])
        self.assertEqual(self.lines(), ["- 11-20 build-it · 기록 1"])
        (self.runtime / "campaign-document" / "watermark.json").write_text('{"v": 1, "ns": 1}')   # the pass above ran on the real clock
        self.catch_up(now=self.bump(1))   # the generator's own entry keeps asking git
        self.assertEqual(self.pr_of(rid), 1777)


class RecordReadersTest(RecordFixture):
    def test_the_readers_show_the_tail_as_words_and_never_the_link(self):
        from tools.fleet import session_status
        from tools.fleet.explorer import transcript
        self.route("alpha-1700", closed=NOW - HOUR)
        self.route("open-x", started=NOW - HOUR)
        self.catch_up()
        text = self.document()
        self.assertIn("[기록 1 ›](<records/pr-1700.md>)", text)
        lines = text.split("\n")
        parsed = session_status.parse_listing(lines)
        shown = [item for group in parsed["groups"] for item in group["items"]]
        self.assertTrue(shown)
        for item in shown:
            self.assertIn("기록 1 ›", item)
            for hidden in ("records/", "](", "[기록", "<"):
                self.assertNotIn(hidden, item)
        rows = " ".join("".join(text for text, _key in row) for row in transcript.doc_rows(lines, 100))
        self.assertIn("기록 1 ›", rows)
        for hidden in ("records/", "](", "[기록"):
            self.assertNotIn(hidden, rows)
        plain = project_status._plain("- t · [기록 1 ›](<records/pr-1700.md>)")
        self.assertIn("기록 1 ›", plain)
        self.assertNotIn("records/", plain)


if __name__ == "__main__":
    unittest.main()
