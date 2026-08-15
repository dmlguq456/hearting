import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


FLEET_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FLEET_ROOT.parent))

from fleet.collectors import dispatch  # noqa: E402
from fleet import render  # noqa: E402


class DispatchModeAxesTest(unittest.TestCase):
    @staticmethod
    def _opts_text(job):
        segments, _width = render._opts_segs(job)
        return "".join(text for text, _style in segments)

    def _registry_job(self, metadata):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            timestamp = datetime.now(timezone.utc).isoformat()
            path.write_text(
                "\t".join((
                    timestamp,
                    "open",
                    "repo",
                    "/tmp/f40-wt",
                    "f40-owner",
                    metadata,
                )) + "\n",
                encoding="utf-8",
            )
            jobs, malformed = dispatch._scan_jobs_log(path, set())
        self.assertEqual(0, malformed)
        self.assertEqual(1, len(jobs))
        return jobs[0]

    def test_current_owner_displays_only_capability_mode(self):
        job = self._registry_job(
            "capability=autopilot-code,capability_mode=dev,worker_type=owner,"
            "unit=_kernel/owner,assigned_contract=autopilot-code,intensity=strong"
        )
        self.assertEqual("dev", job.capability_mode)
        self.assertIsNone(job.worker_mode)
        self.assertFalse(job.mode_axis_conflict)
        rendered = self._opts_text(job)
        self.assertIn("dev", rendered)
        self.assertNotIn("plan/plan-author", rendered)

    def test_stage_preserves_both_axes_without_owner_mode_knob(self):
        job = self._registry_job(
            "capability=autopilot-code,capability_mode=dev,"
            "worker_mode=plan/plan-author,worker_type=stage,unit=plan/plan-author,"
            "assigned_contract=code-plan,intensity=strong,dispatch_depth=2"
        )
        self.assertEqual("dev", job.capability_mode)
        self.assertEqual("plan/plan-author", job.worker_mode)
        self.assertFalse(job.mode_axis_conflict)
        rendered = self._opts_text(job)
        self.assertNotIn("(dev", rendered)
        # F-78: the unit's GROUP (`plan`) already appears in the entry skill (`code-plan`),
        # so the dial keeps only the leaf. Both axes still survive — this case exists to pin
        # that the worker mode is not swallowed by the owner knob, and it is not.
        self.assertEqual(1, rendered.count("plan-author"))
        self.assertNotIn("plan/plan-author", rendered)

    def test_legacy_owner_stage_mode_is_conflict_not_capability(self):
        job = self._registry_job(
            "capability=autopilot-code,mode=plan/plan-author,worker_type=owner,"
            "unit=_kernel/owner,assigned_contract=autopilot-code,intensity=strong"
        )
        self.assertIsNone(job.capability_mode)
        self.assertIsNone(job.worker_mode)
        self.assertTrue(job.mode_axis_conflict)
        rendered = self._opts_text(job)
        self.assertIn("mode!", rendered)
        self.assertNotIn("plan/plan-author", rendered)

    def test_owner_conflict_keeps_canonical_capability_mode(self):
        job = self._registry_job(
            "capability=autopilot-code,capability_mode=dev,"
            "worker_mode=plan/plan-author,worker_type=owner,"
            "unit=_kernel/owner,assigned_contract=autopilot-code,intensity=strong"
        )
        rendered = self._opts_text(job)
        self.assertIn("dev", rendered)
        self.assertIn("mode!", rendered)
        self.assertNotIn("plan/plan-author", rendered)

    def test_legacy_stage_worker_mode_without_unit_remains_visible_once(self):
        job = self._registry_job(
            "capability=autopilot-code,capability_mode=dev,"
            "mode=plan/plan-author,worker_type=stage,assigned_contract=code-plan,"
            "intensity=strong,dispatch_depth=2"
        )
        self.assertEqual("plan/plan-author", job.worker_mode)
        self.assertFalse(job.mode_axis_conflict)
        rendered = self._opts_text(job)
        self.assertEqual(1, rendered.count("plan-author"))   # F-78: leaf only, see above

    def test_proc_env_typed_axes_override_legacy_argv_absence(self):
        lines = ["123 claude 00:08 claude -p /autopilot-code"]
        env = {
            "AGENT_SESSION_ROLE": "worker",
            "AGENT_DISPATCH_CAPABILITY_MODE": "debug",
            "AGENT_DISPATCH_WORKER_TYPE": "owner",
            "AGENT_DISPATCH_UNIT": "_kernel/owner",
        }
        with mock.patch("fleet.collectors.procscan._ps_lines", return_value=lines), \
             mock.patch("fleet.collectors.dispatch.os.readlink", return_value="/tmp/f40-wt"), \
             mock.patch("fleet.collectors.procscan.read_environ", return_value=env), \
             mock.patch("fleet.collectors.procscan.read_proc_start", return_value="11"), \
             mock.patch.object(dispatch, "_claude_job_model", return_value="claude-test"):
            job = dispatch._scan_processes()[0]
        self.assertEqual("debug", job.capability_mode)
        self.assertIsNone(job.worker_mode)


if __name__ == "__main__":
    unittest.main()
