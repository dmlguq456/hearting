"""D4 #3: a project's status line on its OVERVIEW rule and a four-line block on its page.

The source is the project's status document (core/ARTIFACT_META.md "The project status
document"); without one the line is built from routes, questions and results Fleet holds.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.fleet import details  # noqa: E402
from tools.fleet.collectors import project_status  # noqa: E402
from tools.fleet.explorer import tables  # noqa: E402
from tools.fleet.explorer.frame import draw  # noqa: E402
from tools.fleet.explorer.live import build_view_model  # noqa: E402
from tools.fleet.explorer.state import NavState  # noqa: E402
from tools.fleet.explorer.view_model import load_fixture  # noqa: E402
from tools.fleet.model import Session  # noqa: E402
from tools.fleet.refresh import LiveSnapshot  # noqa: E402

FIXTURE = ROOT / "tools" / "fleet" / "tests" / "fixtures" / "explorer" / "view_model.json"

STATUS_KO = """# 하팅 v4.0 캠페인 현황

갱신 2026-11-02 21:5x · 계획은 `PLAN.md` · 근거는 `inputs/`

**한 줄**: 백로그를 묶어 **v4.0.0** 정식 공개까지 간다.

## PR
| PR | 내용 |
|---|---|
| #1 | 결정 아님 |

## 결정 필요
- 케언 PR 병합(= 운영 배포)
- artifact-relocation.py 이동 위치
  - 들여 쓴 줄은 세지 않는다
- W8을 v4에 넣을지

## 사용자 결정 기록
- 이미 내린 결정은 세지 않는다

## 다음
1. [50] 정리 뒤 계획 확정
2. 현황 문서 자동 갱신
"""

STATUS_EN = """# Project status

Updated 2026-11-01 · see PLAN.md

Shipping the importer rewrite this week.

## Decisions needed
* Pick the default backend

## Next steps
- Land the parser
"""


def _touch(path, text="", mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _age(path, mtime):
    os.utime(path, (mtime, mtime))


class ParseTest(unittest.TestCase):
    def test_korean_status_document(self):
        parsed = project_status.parse(STATUS_KO)
        self.assertEqual(parsed["title"], "하팅 v4.0 캠페인 현황")
        self.assertEqual(parsed["line"], "백로그를 묶어 v4.0.0 정식 공개까지 간다.")
        self.assertEqual(parsed["decisions"], ["케언 PR 병합(= 운영 배포)", "artifact-relocation.py 이동 위치",
                                               "W8을 v4에 넣을지"])
        self.assertEqual(parsed["next"], ["[50] 정리 뒤 계획 확정", "현황 문서 자동 갱신"])

    def test_english_headings_and_first_paragraph(self):
        parsed = project_status.parse(STATUS_EN)
        self.assertEqual(parsed["line"], "Shipping the importer rewrite this week.")   # not the Updated stamp
        self.assertEqual(parsed["decisions"], ["Pick the default backend"])
        self.assertEqual(parsed["next"], ["Land the parser"])

    def test_unrecognized_shape_falls_back_to_the_title(self):
        parsed = project_status.parse("# Only a title\n\n## Notes\n- free form\n")
        self.assertEqual(parsed, {"title": "Only a title", "line": "Only a title", "decisions": [], "next": []})


class ArtifactRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _git(self, *args, cwd):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                       cwd=cwd, check=True, capture_output=True)

    def _script(self, cwd):
        env = {key: value for key, value in os.environ.items() if key != "AGENT_ARTIFACT_ROOT"}
        result = subprocess.run(["sh", str(ROOT / "utilities" / "artifact-root.sh"), str(cwd)],
                                capture_output=True, text=True, env=env, check=True)
        return result.stdout.strip()

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_linked_worktree_reads_the_primary_checkout_root_like_the_script(self):
        primary = self.tmp / "repo"
        primary.mkdir()
        self._git("init", "-q", cwd=primary)
        (primary / ".agent_reports").mkdir()
        (primary / "f").write_text("x")
        self._git("add", "f", cwd=primary)
        self._git("commit", "-qm", "init", cwd=primary)
        linked = self.tmp / "repo-wt" / "task"
        self._git("worktree", "add", "-q", "-b", "task", str(linked), cwd=primary)
        (linked / "sub").mkdir()
        expected = os.path.realpath(primary / ".agent_reports")
        for cwd in (primary, linked, linked / "sub"):
            with self.subTest(cwd=str(cwd)):
                self.assertEqual(project_status.artifact_root(cwd), expected)
                self.assertEqual(self._script(cwd), expected)

    def test_plain_folder_inherits_only_a_marked_ancestor(self):
        parent = self.tmp / "work"
        child = parent / "project"
        child.mkdir(parents=True)
        (parent / ".agent_reports").mkdir()
        self.assertIsNone(project_status.artifact_root(child))
        (parent / ".agent-workspace").write_text("")
        self.assertEqual(project_status.artifact_root(child), os.path.realpath(parent / ".agent_reports"))
        (child / ".agent_reports").mkdir()
        self.assertEqual(project_status.artifact_root(child), os.path.realpath(child / ".agent_reports"))


class FindTest(unittest.TestCase):
    def setUp(self):
        project_status.clear_cache()
        self.addCleanup(project_status.clear_cache)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        self.root = self.project / ".agent_reports"
        self.now = time.time()

    def test_newest_status_document_wins_and_old_or_nested_copies_are_not_read(self):
        cycle = self.root / "campaigns" / "2026-11-02_v4" / "2026-11-02_status" / "artifacts"
        _touch(cycle / "현황.md", STATUS_KO, self.now - 60)
        _touch(cycle / "_internal" / "STATUS.md", STATUS_EN, self.now)          # a copy, not the document
        _touch(self.root / "research" / "2026-10-01_old" / "STATUS.md", STATUS_EN, self.now - 3600)
        stale = self.root / "campaigns" / "2026-09-01_quiet" / "2026-09-01_c" / "artifacts" / "STATUS.md"
        _touch(stale, STATUS_EN, self.now)
        _age(stale.parents[2], self.now - 30 * 86400)                          # campaign untouched for 30 days
        record = project_status.newest(str(self.root), self.now)
        self.assertEqual(record["relative"], "campaigns/2026-11-02_v4/2026-11-02_status/artifacts/현황.md")
        self.assertEqual(len(record["decisions"]), 3)
        _touch(self.root / "research" / "2026-10-01_old" / "STATUS.md", STATUS_EN, self.now)
        self.assertEqual(project_status.newest(str(self.root), self.now)["relative"],
                         "campaigns/2026-11-02_v4/2026-11-02_status/artifacts/현황.md")

    def test_research_log_is_not_a_status_board_without_campaign_or_cycle_pages(self):
        _touch(self.root / "research" / "2026-10-01_old" / "STATUS.md", STATUS_EN, self.now)
        self.assertIsNone(project_status.newest(str(self.root), self.now))

    def test_names_match_the_producer_rule_and_hangul_name_comes_first(self):
        campaign = self.root / "campaigns" / "2026-11-02_v4"
        both = campaign / "2026-11-02_both" / "artifacts"
        _touch(both / "STATUS.md", STATUS_EN, self.now)
        _touch(both / "현황.md", STATUS_KO, self.now - 600)              # the cycle's primary, though older
        found = project_status.find_documents(str(self.root), self.now)
        self.assertEqual([Path(path).name for path in found], ["현황.md"])
        project_status.clear_cache()
        lower = campaign / "2026-11-02_lower" / "artifacts" / "status.md"
        decomposed = campaign / "2026-11-02_nfd" / "artifacts" / unicodedata.normalize("NFD", "현황.md")
        at_root = campaign / "2026-11-02_root" / "현황.md"                   # never sealed: not read
        for path in (lower, decomposed, at_root):
            _touch(path, STATUS_EN, self.now)
        found = sorted(os.path.relpath(path, self.root) for path in project_status.find_documents(
            str(self.root), self.now))
        self.assertEqual(found, sorted([
            "campaigns/2026-11-02_v4/2026-11-02_both/artifacts/현황.md",
            "campaigns/2026-11-02_v4/2026-11-02_lower/artifacts/status.md",
            os.path.join("campaigns/2026-11-02_v4/2026-11-02_nfd/artifacts",
                         unicodedata.normalize("NFD", "현황.md"))]))

    def test_collect_scans_one_root_once_per_interval(self):
        doc = self.root / "campaigns" / "2026-11-02_x" / "STATUS.md"
        _touch(doc, STATUS_EN, self.now - 10)
        (self.project / "a").mkdir()
        cwds = [str(self.project), str(self.project / "a"), str(self.tmp)]
        spent = project_status.collect(cwds, now=self.now, budget=0)          # no time left: scanned later
        self.assertIs(spent[str(self.project)], None)
        first = project_status.collect(cwds, now=self.now)
        self.assertEqual(first[str(self.project)]["line"], "Shipping the importer rewrite this week.")
        self.assertIs(first[str(self.project / "a")], None)                    # its own folder has no root
        self.assertIs(first[str(self.tmp)], None)
        _touch(doc, STATUS_KO, self.now)
        cached = project_status.collect(cwds, now=self.now + 5)
        self.assertEqual(cached[str(self.project)]["line"], "Shipping the importer rewrite this week.")
        later = project_status.collect(cwds, now=self.now + project_status.INTERVAL + 1)
        self.assertEqual(later[str(self.project)]["line"], "백로그를 묶어 v4.0.0 정식 공개까지 간다.")

    def test_campaign_documents_win_over_newer_legacy_copies_and_keep_three_campaigns(self):
        for index in range(5):
            campaign = self.root / "campaigns" / ("2026-11-06_stream-" + str(index))
            _touch(campaign / "STATUS.md", "# Campaign " + str(index) + "\n\nSummary " + str(index),
                   self.now - index * 10)
            _touch(campaign / "2026-11-06_old" / "artifacts/STATUS.md", "# Cycle copy", self.now + 100)
        _touch(self.root / "research/x/STATUS.md", "# Chronological log", self.now + 100)
        record = project_status.newest(str(self.root), now=self.now)
        self.assertEqual(record["line"], "Summary 0")
        self.assertEqual([row["campaign"] for row in record["campaigns"]],
                         ["stream-0", "stream-1", "stream-2"])
        self.assertTrue(all("campaigns" not in row for row in record["campaigns"]))

    def test_campaign_status_reuses_scan_cache_and_new_files_appear_after_interval(self):
        campaign = self.root / "campaigns/2026-11-06_stream"
        _touch(campaign / "STATUS.md", "# Initial", self.now - 1)
        first = project_status.collect([str(self.project)], now=self.now)
        self.assertEqual(first[str(self.project)]["line"], "Initial")
        _touch(campaign / "STATUS.md", "# Updated", self.now + 1)
        held = project_status.collect([str(self.project)], now=self.now + 1)
        self.assertEqual(held[str(self.project)]["line"], "Initial")
        later = project_status.collect([str(self.project)], now=self.now + project_status.INTERVAL + 1)
        self.assertEqual(later[str(self.project)]["line"], "Updated")

    def test_editing_status_does_not_require_a_new_file_in_the_campaign_folder(self):
        campaign = self.root / "campaigns/2026-09-01_stream"
        _touch(campaign / "STATUS.md", "# Updated in place", self.now)
        _age(campaign, self.now - 30 * 86400)
        record = project_status.newest(str(self.root), now=self.now)
        self.assertEqual(record["line"], "Updated in place")
        self.assertEqual(record["campaigns"][0]["campaign"], "stream")


class ProjectionTest(unittest.TestCase):
    def test_repo_page_gets_only_first_lines_from_existing_current_question_material(self):
        now = time.time()
        sessions = [Session(harness="claude", pid=10, session_tag="a1", session_id="sid-a", cwd="/w/alpha",
                            title="t", liveness="idle")]
        waits = {"complete": True, "total": 4, "older": 0, "ownerless": 0,
                 "items": [{"id": f"q{index}", "source": "S2", "kind": "human gate",
                            "since": now, "stale": "current", "harness": "claude",
                            "session_id": "sid-a", "question": f"Decision {index}\nsecond line"}
                           for index in range(4)]}
        snapshot = {"sessions": sessions, "jobs": [], "resources": [], "answer_waits": waits,
                    "project_of": lambda cwd: os.path.basename(cwd)}
        repos = {repo["id"]: repo for repo in build_view_model(snapshot, now=now).repos}
        status = repos["alpha"]["status"]
        self.assertEqual(tuple(status["question_lines"]), ("Decision 0", "Decision 1", "Decision 2"))

    def test_document_and_derived_status_on_repo_records(self):
        now = time.time()
        sessions = [Session(harness="claude", pid=10, session_tag="a1", session_id="sid-a", cwd="/w/alpha",
                            title="t", liveness="idle"),
                    Session(harness="claude", pid=11, session_tag="b1", session_id="sid-b", cwd="/w/beta",
                            title="t", liveness="idle")]
        record = dict(project_status.parse(STATUS_KO), name="현황.md", mtime=now - 5 * 86400,
                      relative="campaigns/c/cy/artifacts/현황.md")
        snapshot = {"sessions": sessions, "jobs": [], "resources": [],
                    "project_of": lambda cwd: os.path.basename(cwd),
                    "project_status": {"/w/alpha": record, "/w/beta": None}}
        repos = {repo["id"]: repo for repo in build_view_model(snapshot, now=now).repos}
        status = repos["alpha"]["status"]
        self.assertEqual((status["line"], status["decisions"], status["decision"], status["next"]),
                         ("백로그를 묶어 v4.0.0 정식 공개까지 간다.", 3, "케언 PR 병합(= 운영 배포)",
                          "[50] 정리 뒤 계획 확정"))
        self.assertEqual((status["doc"], status["doc_age"], status["stale"]),
                         ("campaigns/c/cy/artifacts/현황.md", "5d ago", True))
        self.assertNotIn("status", repos["beta"])          # no document, no route, question or result


class RenderTest(unittest.TestCase):
    DOC = {"routes": 0, "questions": 2, "done": "importer", "done_age": "41m ago",
           "line": "백로그를 묶어 v4.0.0 정식 공개까지 간다.", "decisions": 3,
           "decision": "케언 PR 병합", "next": "[50] 정리 뒤 계획 확정",
           "doc": "campaigns/x/현황.md", "doc_age": "12m ago", "stale": False, "changed": "12m ago",
           "question_lines": ["보관 기간을 정할까요?", "두 번째 질문", "세 번째 질문", "숨길 네 번째 질문"]}
    DERIVED = {"routes": 2, "questions": 0, "done": "csi bundle", "done_age": "53m ago", "changed": "53m ago"}

    def setUp(self):
        self.vm = load_fixture(FIXTURE)
        self.first, self.second = (str(repo["id"]) for repo in self.vm.repos[:2])

    def _with(self, **statuses):
        repos = tuple(dict(repo, status=statuses[str(repo["id"])]) if str(repo["id"]) in statuses else repo
                      for repo in self.vm.repos)
        return replace(self.vm, repos=repos)

    @staticmethod
    def _text(frame):
        return ["".join(cell[0] for cell in row) for row in frame.cells]

    def test_segments(self):
        segments = tables.repo_status_segments(self.DOC, 120)
        text = "".join(text for text, _key in segments)
        self.assertEqual(text, "◑ to answer 2 │ 3 to decide")
        self.assertIn(("3 to decide", "lvl_y"), segments)
        self.assertEqual("".join(text for text, _ in tables.repo_status_segments(self.DOC, 120, narrow=True)),
                         "◑ to answer 2 │ 3 to decide")
        self.assertEqual("".join(text for text, _ in tables.repo_status_segments(self.DERIVED, 120)), "route 2")
        self.assertEqual(tables.repo_status_segments(dict(self.DOC, decisions=0), 120, narrow=True),
                         [("◑ to answer 2", "blocked")])
        # A narrow screen keeps what waits on the person; the route count gives way, not them.
        busy = dict(self.DOC, routes=3, routes_running=1)
        self.assertEqual("".join(text for text, _ in tables.repo_status_segments(busy, 120)),
                         "route 3 ⠏1 │ ◑ to answer 2 │ 3 to decide")
        self.assertEqual("".join(text for text, _ in tables.repo_status_segments(busy, 30, narrow=True)),
                         "◑ to answer 2 │ 3 to decide")
        self.assertEqual(tables.repo_status_segments(self.DERIVED, 120, narrow=True), [])
        stale = dict(self.DOC, stale=True, doc_age="5d ago", changed="5d ago", questions=0)
        self.assertEqual("".join(text for text, _ in tables.repo_status_segments(stale, 200)), "3 to decide")
        tight = tables.repo_status_segments(self.DOC, 18)
        self.assertLessEqual(sum(tables._seg_width(text) for text, _ in tight), 18)

    def test_status_rides_on_the_rule_without_adding_rows(self):
        plain = self._text(draw(self.vm, NavState(), 140, 45, beat=1))
        shown = self._text(draw(self._with(**{self.first: self.DOC, self.second: self.DERIVED}),
                                NavState(), 140, 45, beat=1))
        changed = [y for y, (a, b) in enumerate(zip(plain, shown)) if a != b]
        self.assertEqual(len(changed), 2, changed)                     # exactly the two group rules
        self.assertIn(self.first + "/ │ ◑ to answer 2 │ 3 to decide", shown[changed[0]])
        self.assertNotIn("백로그를 묶어", shown[changed[0]])
        self.assertIn(self.second + "/ │ route 2", shown[changed[1]])
        self.assertNotIn("csi bundle", shown[changed[1]])

    def test_repo_page_opens_with_four_status_lines(self):
        state = NavState(scope="repo:" + self.first)
        plain = self._text(draw(self.vm, state, 140, 45, beat=1))
        shown = self._text(draw(self._with(**{self.first: self.DOC}), state, 140, 45, beat=1))
        top = next(y for y, row in enumerate(shown) if "status:" in row)
        self.assertIn("백로그를 묶어 v4.0.0 정식 공개까지 간다.", shown[top])
        self.assertIn("3 · 케언 PR 병합 · +2 more", shown[top + 1])
        self.assertIn("◑ to answer 2", shown[top + 1])
        self.assertIn("[50] 정리 뒤 계획 확정", shown[top + 2])
        self.assertIn("12m ago campaigns/x/현황.md · 41m ago ✓ importer", shown[top + 3])
        self.assertIn("? 보관 기간을 정할까요?", "\n".join(shown[top + 4:top + 8]))
        self.assertNotIn("숨길 네 번째 질문", "\n".join(shown))
        header = next(y for y, row in enumerate(plain) if " ID " in row and "MODEL" in row)
        pane = 32                                                     # the sidebar left of the pane is unchanged
        new_header = next(y for y, row in enumerate(shown) if " ID " in row and "MODEL" in row)
        self.assertEqual(shown[new_header][pane:], plain[header][pane:])
        self.assertTrue(any("OPEN WORK" in row or "백로그·진행" in row for row in shown[top + 4:new_header]))


class SnapshotPlumbingTest(unittest.TestCase):
    def test_merge_carries_the_detail_value_and_last_good_survives_a_pass_without_it(self):
        found = {"/w/a": {"line": "x"}}
        basic = LiveSnapshot()
        detail = details.DetailSnapshot(details.state_key(basic), LiveSnapshot(project_status=found))
        self.assertIs(details.merge(basic, detail).project_status, found)
        later = details.DetailSnapshot(details.state_key(basic), LiveSnapshot())
        kept = details.retain_optional_last_good(detail, later)
        self.assertIs(kept.snapshot.project_status, found)


if __name__ == "__main__":
    unittest.main()
