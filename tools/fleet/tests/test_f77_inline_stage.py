"""F-77 — a main session's third column names the SKILL it is inside, and nothing more.

The column stopped meaning "stage" some time ago (user 2026-08-14: "이미 stages의 위치가
가지는 의미가 fleet에서 모호해졌고, depth=1,2도 그 자리에 stage라기 보다는 unit, capa, node
정보를 남고 있으니까"). depth-1 shows `code(dev·thr·owner)`, depth-2 shows `code-execute(thr)`,
and since F-75 the real route breadcrumb lives on the card's close rail. So a main session
answers the same question the other rows answer — which unit of work is this — with the
capability it is inside: "정확히는 무슨 skill 지금 쓰는 중인지 정도로만".

Why no inferred stage trails it: the only inline stage signal available is artifact
FILENAMES, whose vocabulary belongs to the code cycle. An autopilot-spec session therefore
rendered `spec(direct) : exec` — a code-pipeline stage word under a spec capability, observed
live while building this. A tag that can contradict its own capability is worse than none.

`_artifact_stage` itself still matters for the slug-matched path (dispatch rows), so the
recency fix and the marker vocabulary it grew here are pinned below.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import projection as P                             # noqa: E402
from fleet import render                                      # noqa: E402
from fleet.model import Session                               # noqa: E402


def _text(segs):
    return "".join(t for t, _k in segs)


def _session(**kw):
    kw.setdefault("harness", "claude")
    kw.setdefault("pid", 1)
    kw.setdefault("proc_start", "r")
    kw.setdefault("cwd", "/tmp/f77")
    kw.setdefault("session_id", "sid-f77")
    kw.setdefault("slug", "proj")
    kw.setdefault("liveness", "working")
    return Session(**kw)


class SkillOnlyCellTest(unittest.TestCase):

    def _cell(self, cap, projection=None):
        s = _session()
        s.cap_grounding = cap
        if projection is not None:
            s.work_projection = projection
        return _text(render._session_stage_segs(s, True, 80))

    def test_capability_and_knobs_only(self):
        cell = self._cell({"capability": "autopilot-code", "mode": "dev",
                           "intensity": "standard"})
        self.assertEqual(cell, "code(dev·std)")

    def test_no_stage_trails_the_capability(self):
        """The regression this suite exists for: an artifact-derived stage must not be
        appended to the skill tag, whatever the projection happens to carry."""
        cell = self._cell({"capability": "autopilot-spec", "intensity": "direct"},
                          projection=P.WorkProjection(source="artifact-inferred",
                                                      stage_label="exec"))
        self.assertEqual(cell, "spec(direct)")
        self.assertNotIn("exec", cell)
        self.assertNotIn(":", cell)

    def test_capability_without_knobs(self):
        self.assertEqual(self._cell({"capability": "autopilot-research"}), "research")

    def test_no_capability_falls_back_to_the_projection_text(self):
        """Without a marker the cell is unchanged from before — a route-backed session
        still shows its own projection, and a bare session shows the honest dash."""
        self.assertEqual(self._cell({}), "-")


class NoColumnHeaderTest(unittest.TestCase):
    """The column lost its label and then the whole header row (user: "위에 column 헤더도
    제거해버리자" → "아니 헤더 자체를 통째로 날려")."""

    def test_wide_board_has_no_header_row(self):
        lines = render._build_lines([], [], "fleet", False, 0,
                                    layout="wide", term_width=168)
        text = "\n".join("".join(t for t, _k in ln) for ln in lines if ln)
        for label in ("stages", "harness (model·effort)", "session (branch)"):
            self.assertNotIn(label, text)

    def test_product_name_carries_its_own_hue(self):
        """F-77 (user "hearting이라는 글자에 컬러 넣어줘"): with the header gone the board's
        own name is its first line, and it is no longer generic `head` grey."""
        render.set_hearting({"version": "v1.2.3", "install_method": "packaged"})
        try:
            row = render._hearting_header_row()
        finally:
            render.set_hearting(None)
        name_key = next(k for t, k in row if t == "hearting")
        self.assertEqual(name_key, "hearting_name")
        # The palette reserves green/yellow/red for status and cyan/magenta/blue for
        # harness identity, so the product name must sit on neither axis.
        self.assertEqual(render._HUE_OF["hearting_name"][0], "v")


class ArtifactStageRecencyTest(unittest.TestCase):
    """`_artifact_stage` still serves the slug-matched (dispatch) path.

    It used to answer from mere EXISTENCE in a fixed report>test>exec>plan order, so a
    directory that had ever produced a `report` name reported `report` forever, and a cycle
    that pre-created its outputs reported its LAST stage from its first minute.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cycle = os.path.join(self._tmp.name, "2026-08-14_work")
        os.makedirs(self.cycle)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, pause=0.02):
        time.sleep(pause)
        target = os.path.join(self.cycle, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")

    def test_stage_follows_the_most_recent_write(self):
        for rel, expected in (("plan.md", "plan"),
                              ("checklist.md", "exec"),
                              ("dev_logs/w1.md", "exec"),
                              ("test_logs/t1.md", "test"),
                              ("pipeline_summary.md", "report"),
                              ("final_report.md", "report")):
            self._write(rel)
            self.assertEqual(P._artifact_stage(self.cycle), expected, rel)

    def test_rework_moves_the_stage_back(self):
        self._write("plan.md")
        self._write("test_logs/t1.md")
        self.assertEqual(P._artifact_stage(self.cycle), "test")
        self._write("dev_logs/w2.md")
        self.assertEqual(P._artifact_stage(self.cycle), "exec")

    def test_a_pre_created_report_does_not_win(self):
        self._write("final_report.md")
        os.utime(os.path.join(self.cycle, "final_report.md"), (time.time() - 600,) * 2)
        self._write("plan.md")
        self.assertEqual(P._artifact_stage(self.cycle), "plan")


class StageMarkerVocabularyTest(unittest.TestCase):
    """The marker table names what capabilities actually write. `dev_logs` and
    `pipeline_summary` were missing, so a live cycle sat at `plan` through its whole
    execute stage — invisible in tests that used `execute*` filenames nothing writes.
    Taken from the code recipe's per-node `write_scope`."""

    def test_execute_artifacts_read_as_exec(self):
        for name in ("dev_logs", "checklist.md", "execute.md"):
            self.assertEqual(P._artifact_stage_label(name), "exec", name)

    def test_report_artifacts_read_as_report(self):
        for name in ("final_report.md", "pipeline_summary.md", "verification.md"):
            self.assertEqual(P._artifact_stage_label(name), "report", name)

    def test_plan_and_test_artifacts(self):
        self.assertEqual(P._artifact_stage_label("plan.md"), "plan")
        self.assertEqual(P._artifact_stage_label("test_logs"), "test")

    def test_unrelated_names_are_not_stages(self):
        for name in ("shards", "_internal", "notes.md", "140.txt"):
            self.assertIsNone(P._artifact_stage_label(name), name)


if __name__ == "__main__":
    unittest.main()
