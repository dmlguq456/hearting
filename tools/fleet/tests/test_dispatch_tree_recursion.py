"""Dispatch slug collisions must not turn depth-2 leaves into recursive owners."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fleet import render
from fleet.model import DispatchJob, WorkProjection


class DispatchTreeRecursionTest(unittest.TestCase):
    def job(self, pid, slug, depth=1, parent=None):
        return DispatchJob(
            key="code", pid=pid, slug=slug, depth=depth, parent_slug=parent,
            attempt_id="att-%d" % pid, liveness="working", cwd="/tmp/fleet-recursion",
            work_projection=WorkProjection(),
        )

    def emit(self, jobs, layout="wide", show_all=False):
        with mock.patch.object(render, "_PROCESS_VIEW", False), \
                mock.patch.object(render, "_SHOW_ALL", show_all):
            lines = render._build_lines(
                [], jobs, "dispatch", False, 0, layout=layout,
                term_width=160, governor=None,
            )
        pids = [entry["pid"] for entry in render._SELECTABLE]
        self.assertCountEqual(pids, [job.pid for job in jobs])
        self.assertLess(len(lines), 10 * len(jobs) + 30)
        return pids

    def test_owner_and_child_can_share_slug(self):
        # 2026-09-07: codex-claude-permission-parity reused its owner's slug.
        jobs = [self.job(1, "same"), self.job(2, "same", 2, "same")]
        for layout in ("wide", "narrow", "stack"):
            for show_all in (False, True):
                with self.subTest(layout=layout, show_all=show_all):
                    self.assertEqual(self.emit(jobs, layout, show_all), [1, 2])

    def test_child_slug_matching_another_owner_does_not_expand_that_tree(self):
        jobs = [self.job(1, "a"), self.job(2, "b"),
                self.job(3, "b", 2, "a"), self.job(4, "a", 2, "b")]
        self.assertEqual(self.emit(jobs), [1, 3, 2, 4])

    def test_many_children_including_same_slug_are_each_rendered_once(self):
        jobs = [self.job(1, "owner"), self.job(2, "owner", 2, "owner")]
        jobs.extend(self.job(i, "child-%d" % i, 2, "owner")
                    for i in range(3, 1502))
        self.emit(jobs)


if __name__ == "__main__":
    unittest.main()
