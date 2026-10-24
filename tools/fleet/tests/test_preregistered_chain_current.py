"""A pre-registered chain successor never hides the attempt that is actually running.

Incident shape (fleet2 owner rt-e65b4565910c72de, 2026-10-23): a gap-retry chain registered
``cont-8494ab15-1/-2/-3`` as open rows at once. While ``-2`` ran, Fleet and fleet2 picked the
last registered row of the ``execute`` node, ``-3``, which had not launched yet, so the
running worker was drawn as ``· … : -`` with no context and no summary. The rows below keep
the registry fields that matter for the incident verbatim.
"""
import os
import sys
import tempfile
import unittest

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render  # noqa: E402
from fleet.collectors import dispatch  # noqa: E402
from fleet.explorer.focus import read_attempts  # noqa: E402
from fleet.explorer.types import AttemptRow  # noqa: E402
from fleet.model import DispatchJob, current_attempt_rank  # noqa: E402
from fleet.route import _node_state  # noqa: E402
from fleet.tests.snapshot_fixture import build_observed_lines  # noqa: E402


ROUTE = "rt-e65b4565910c72de"
OWNER = "att-167e32cca4f62d9f51b2de31e353ed24"
PARENT_SID = "3eb4d557-c10c-4e06-8ac0-ec291acd7a27"
WT = "/home/nas/user/Uihyeop/personal/hearting-wt/fleet2-explorer-r13"
COMMON = (
    "capability=autopilot-code,capability_mode=dev,qa=standard,intensity=standard,"
    "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=cross-harness-headless,harness=codex,"
    "parent=fleet2-explorer-r13-leg0-owner,parent_attempt_id=" + OWNER + ","
    "parent_sid=" + PARENT_SID + ",worker_type=stage,unit=dev/backend,"
    "assigned_contract=code-execute,completion_gate=code-execute,"
    "route_id=" + ROUTE + ",route_node=execute,"
    "session_chain_id=ssc-cont-8494ab15d3a2f3d0,subsession_mode=serial,"
    "subsession_purpose=gap-retry,subsession_count=3,stage_authority=0"
)


def chain_row(index, ts, status, launch):
    attempt = "att-cont-8494ab15d3a2f3d0-%d" % index
    pipe = "%s,attempt_id=%s,subsession_index=%d,log_file=/logs/cont-8494ab15-%d.%s.codex.jsonl,%s" % (
        COMMON, attempt, index, index, attempt, launch)
    return "%s\t%s\t%s\t%s\tcont-8494ab15-%d\t%s\n" % (ts, status, WT, WT, index, pipe)


# -1 failed and is done; -2 is launched and running; -3 is registered only (never claimed).
INCIDENT = [
    chain_row(1, "2026-10-23T06:38:03.779822Z", "done",
              "launch_claimed=1,launch_started=1,pid=2462138,pid_start=110765503,"
              "launch_outcome=governed-process-group-drained,note=dead-worker-fail"),
    chain_row(2, "2026-10-23T06:38:11.463013Z", "open",
              "launch_claimed=1,launch_started=1,pid=2923610,pid_start=111064750"),
    chain_row(3, "2026-10-23T06:38:14.963890Z", "open", "launch_claimed=0"),
]
OWNER_ROW = (
    "2026-10-23T05:00:00Z\topen\t%s\t%s\tfleet2-explorer-r13-leg0-owner\t"
    "capability=autopilot-code,attempt_schema_version=2,dispatch_depth=1,harness=claude,"
    "worker_type=owner,attempt_id=%s,owner_route_id=%s,parent_sid=%s,"
    "launch_claimed=1,launch_started=1,pid=3591546\n" % (WT, WT, OWNER, ROUTE, PARENT_SID)
)


class _Registry(unittest.TestCase):
    rows = INCIDENT

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs_log = os.path.join(self.tmp.name, "jobs.log")
        with open(self.jobs_log, "w", encoding="utf-8") as f:
            f.writelines([OWNER_ROW] + list(self.rows))


class ClassicFleetPicksTheRunningAttempt(_Registry):
    def _scanned(self):
        jobs, malformed = dispatch._scan_jobs_log(self.jobs_log, set())
        self.assertEqual(malformed, 0)
        return {job.slug: job for job in jobs if job.route_node == "execute"}

    def test_collector_marks_only_the_unlaunched_successor_registered_only(self):
        jobs = self._scanned()
        self.assertEqual(sorted(jobs), ["cont-8494ab15-2", "cont-8494ab15-3"])  # -1 is past afterglow
        self.assertFalse(jobs["cont-8494ab15-2"]._registered_only)
        self.assertTrue(jobs["cont-8494ab15-3"]._registered_only)
        self.assertNotIn("_registered_only", jobs["cont-8494ab15-3"].to_dict())  # --json unchanged

    def test_unlaunched_successor_is_unknown_and_hidden_behind_the_running_attempt(self):
        jobs = self._scanned()
        running, waiting = jobs["cont-8494ab15-2"], jobs["cont-8494ab15-3"]
        self.assertEqual(dispatch._dispatch_liveness(waiting, now=0.0, track=False), "unknown")
        running.liveness = "working"
        render.set_show_all(False)
        self.assertEqual([j.slug for j in render._current_attempt_jobs([running, waiting])],
                         ["cont-8494ab15-2"])
        lines = build_observed_lines([], [running, waiting], section="dispatch", narrow=False,
                                     malformed=0, layout="wide")
        text = "\n".join("".join(part for part, _key in line) for line in lines if line)
        self.assertIn("running", text)

    def test_show_all_still_lists_every_attempt(self):
        jobs = list(self._scanned().values())
        render.set_show_all(True)
        try:
            self.assertEqual(len(render._current_attempt_jobs(jobs)), 2)
        finally:
            render.set_show_all(False)

    def test_route_node_reads_the_started_attempt_not_the_registered_successor(self):
        jobs = self._scanned()
        started, waiting = jobs["cont-8494ab15-2"], jobs["cont-8494ab15-3"]
        started.liveness, waiting.liveness = "idle", "unknown"
        started.attention_reason = "terminal-conflict"
        state = _node_state("execute", [started, waiting], {}, now=0.0)
        self.assertEqual((state["state"], state["job"]), ("attention", started))


class SupersessionIsUnchanged(unittest.TestCase):
    def _job(self, slug, order, *, registered_only=False, liveness="working"):
        return DispatchJob(key="code-test", slug=slug, cwd="/work/wt", source="jobs", status="open",
                           harness="codex", depth=2, route_id="rt-a", route_node="test",
                           liveness=liveness, attempt_id="att-%s" % slug, registry_order=order,
                           _registered_only=registered_only)

    def test_a_newer_launched_attempt_still_supersedes_an_older_one(self):
        older, newer = self._job("r4", 4), self._job("r5", 5, liveness="dead")
        render.set_show_all(False)
        self.assertEqual([j.slug for j in render._current_attempt_jobs([older, newer])], ["r5"])

    def test_a_node_with_only_registered_rows_keeps_registry_order(self):
        first = self._job("r1", 1, registered_only=True, liveness="unknown")
        second = self._job("r2", 2, registered_only=True, liveness="unknown")
        render.set_show_all(False)
        self.assertEqual([j.slug for j in render._current_attempt_jobs([first, second])], ["r2"])

    def test_legacy_rows_without_launch_markers_rank_exactly_as_before(self):
        legacy = "2026-07-16T00:00:00+00:00\topen\trepo\t/w\tt\tcapability=autopilot-code,harness=codex\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write(legacy)
            jobs, _ = dispatch._scan_jobs_log(path, set())
        self.assertFalse(jobs[0]._registered_only)
        self.assertEqual(current_attempt_rank(jobs[0])[0], True)


class Fleet2PicksTheRunningAttempt(_Registry):
    """fleet2 draws route stages from the shared route view (``route._node_state`` above);
    its own attempt rows only carry the same verdict."""

    def setUp(self):
        super().setUp()
        live = {"att-cont-8494ab15d3a2f3d0-2": {"liveness": "working"}}
        self.attempts = read_attempts(self.jobs_log, PARENT_SID, live)

    def test_read_attempts_carries_the_runtime_verdict(self):
        flags = {row.attempt_id[-2:]: row.registered_only for row in self.attempts
                 if row.route_node == "execute"}
        self.assertEqual(flags, {"-1": False, "-2": False, "-3": True})

    def test_a_newer_launched_attempt_still_supersedes_an_older_row(self):
        old = AttemptRow("att-old", 10, "done", "s", "codex", worker_type="stage", depth=2,
                         route_id=ROUTE, route_node="execute")
        new = AttemptRow("att-new", 11, "open", "s", "codex", worker_type="stage", depth=2,
                         route_id=ROUTE, route_node="execute")
        waiting = AttemptRow("att-wait", 12, "open", "s", "codex", worker_type="stage", depth=2,
                             route_id=ROUTE, route_node="execute", registered_only=True)
        self.assertEqual(max([old, new, waiting], key=lambda r: r.rank).attempt_id, "att-new")
        self.assertEqual(max([old, new], key=lambda r: r.rank).attempt_id, "att-new")


if __name__ == "__main__":
    unittest.main()
