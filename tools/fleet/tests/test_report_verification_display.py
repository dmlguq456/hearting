"""Fleet detail-only report status attachment and render output."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from fleet import projection, render, route  # noqa: E402
from fleet.model import DispatchJob  # noqa: E402
# The same module object the Fleet projection resolves, so the patches below reach it.
report_verification_projection = projection._report_verification()


COMPOSED = os.path.join(os.path.dirname(__file__), "fixtures", "route",
                        "synth_composed_survey.json")


class ReportVerificationDisplayTests(unittest.TestCase):
    def test_report_projection_import_resolves_with_fleet_sys_path(self):
        # The real entry (tools/fleet/fleet.py) puts only `tools/` on sys.path.
        tools = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        script = (
            "import sys; "
            f"sys.path[:0] = [{tools!r}]; "
            "from fleet import projection; "
            "module = projection._report_verification(); "
            "assert callable(module.project_route) and callable(module.report_detail_payload)"
        )
        result = __import__("subprocess").run(
            [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        self.record = route.load(COMPOSED)
        self.job = DispatchJob(
            key="code-execute", slug="report-display", cwd="/tmp",
            artifact_root="/tmp", depth=2, route_file=COMPOSED,
            route_id=self.record["route_id"], route_hash=self.record["route_hash"],
            route_node="claim-a", assigned_contract="autopilot-code",
            liveness="working", harness="codex")
        self.payload = {
            "schema_version": 1,
            "verification": {"verdict": "FAIL", "reason": "required-input-digest-mismatch",
                             "peers": [], "history": []},
            "completion": {"state": "complete"},
            "required_input_observation": {"state": "failed",
                                            "reasons": ["required-input-digest-mismatch"]},
            "display": {"verification_label": "검증 실패", "completion_label": "작업 완료",
                        "required_input_label": "필수 입력 확인 실패"},
        }

    def test_basic_refresh_stays_fast_and_detail_serializes_one_common_payload(self):
        with mock.patch.object(report_verification_projection, "project_route",
                               side_effect=AssertionError("basic refresh read detailed artifacts")) as resolver:
            projection.attach_projections([], [self.job], route_records={self.record["route_id"]: self.record},
                                          artifact_root="/tmp", now=100.0, fast_first=True)
        resolver.assert_not_called()
        self.assertIsNone(self.job.work_projection.report_verification)
        with mock.patch.object(report_verification_projection, "project_route",
                               return_value=self.payload) as resolver:
            projection.attach_projections([], [self.job], route_records={self.record["route_id"]: self.record},
                                          artifact_root="/tmp", now=100.0)
        self.assertEqual(resolver.call_count, 1)
        public = self.job.to_dict()["work_projection"]
        self.assertEqual(public["report_verification"], self.payload)
        json.dumps(self.job.to_dict(), ensure_ascii=False)

    def test_render_uses_payload_only_clips_reasons_and_performs_no_resolution(self):
        projection.attach_projections([], [self.job], route_records={self.record["route_id"]: self.record},
                                      artifact_root="/tmp", now=100.0)
        self.job.work_projection = self.job.work_projection.__class__(
            **{**self.job.work_projection.__dict__, "report_verification": self.payload})
        render.set_process_view(False)
        try:
            with mock.patch.object(report_verification_projection, "project_route",
                                   side_effect=AssertionError("render performed report I/O")):
                for width in (120, 60, 36):
                    lines = render._build_lines([], [self.job], section="dispatch", narrow=width < 70,
                                                malformed=0, layout="wide", term_width=width)
                    output = "\n".join("".join(token for token, _kind in line)
                                       for line in lines if line)
                    self.assertIn("FAIL", output)
                    detail = "".join(token for token, _kind in
                                     render._report_verification_detail_row(
                                         self.job, depth=1, term_width=width)[0])
                    if width == 120:
                        self.assertIn("complete", detail)
                        self.assertIn("input failed", detail)
                    else:
                        self.assertTrue(detail.endswith("…"), detail)
        finally:
            render.set_process_view(False)

    def test_absent_report_is_not_attached_but_integrity_failures_remain_visible(self):
        for reason in ("report-source-unavailable", "route-hash-binding-mismatch",
                       "artifact-revision-stale", "report-source-kind-invalid"):
            payload = report_verification_projection._display(
                report_verification_projection._unresolved(reason))
            with self.subTest(reason=reason), mock.patch.object(
                    report_verification_projection, "project_route", return_value=payload):
                projection.attach_projections(
                    [], [self.job], route_records={self.record["route_id"]: self.record},
                    artifact_root="/tmp", now=100.0)
                rows = render._report_verification_detail_row(self.job)
                if reason == "report-source-unavailable":
                    self.assertIsNone(self.job.work_projection.report_verification)
                    self.assertEqual(rows, [])
                else:
                    self.assertNotIn(reason, "".join(t for t, _ in rows[0]))
                    self.assertEqual(self.job.work_projection.report_verification["verification"]["reason"], reason)

    def test_report_detail_stays_inside_owner_card_and_uses_short_reason(self):
        self.job.depth = self.job.dispatch_depth = 1
        self.job.worker_type = "owner"
        payload = report_verification_projection._display(
            report_verification_projection._unresolved("report-cycle-unadmitted"))
        with mock.patch.object(report_verification_projection, "project_route", return_value=payload):
            projection.attach_projections([], [self.job], route_records={self.record["route_id"]: self.record},
                                          artifact_root="/tmp", now=100.0)
        for width in (60, 100, 180):
            lines = render._build_lines([], [self.job], section="dispatch", narrow=width < 70,
                                        malformed=0, layout="wide", term_width=width)
            detail = next(render._plain(line) for line in lines if line and "↳ report" in render._plain(line))
            self.assertIn("│", detail[:detail.index("report")])
            self.assertNotIn("report-cycle-unadmitted", detail)
            header = next(render._plain(line) for line in lines if line and "╭" in render._plain(line))
            self.assertLessEqual(render._dw(detail), render._dw(header))


if __name__ == "__main__":
    unittest.main(verbosity=2)
