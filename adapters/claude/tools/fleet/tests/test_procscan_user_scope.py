#!/usr/bin/env python3
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet.collectors import dispatch, procscan  # noqa: E402


class CurrentUserProcessScopeTest(unittest.TestCase):
    def test_all_ps_surfaces_select_current_effective_uid(self):
        outputs = [
            "101 codex 00:01 codex --yolo\n",
            "101 1 61 codex\n",
            "101 pts/2\n",
            "",
        ]

        def run(argv, **_kwargs):
            return SimpleNamespace(stdout=outputs.pop(0))

        with mock.patch.object(procscan.os, "geteuid", return_value=4242), \
             mock.patch.object(procscan.subprocess, "run", side_effect=run) as runner:
            self.assertEqual(len(procscan._ps_lines()), 1)
            self.assertIn(101, procscan.proc_tree())
            self.assertEqual(procscan._pid_ttys(), {101: "pts/2"})
            # The dispatch scanner shares the same central process-table boundary.
            self.assertEqual(dispatch._scan_processes(), [])

        expected_columns = (
            "pid=,comm=,etime=,args=",
            "pid=,ppid=,etimes=,comm=",
            "pid=,tty=",
            "pid=,comm=,etime=,args=",
        )
        self.assertEqual(len(runner.call_args_list), len(expected_columns))
        for call, columns in zip(runner.call_args_list, expected_columns):
            argv = call.args[0]
            self.assertEqual(argv, ["ps", "-u", "4242", "-o", columns])
            self.assertNotIn("-e", argv)
            self.assertNotIn("-eo", argv)

    def test_ps_failures_return_empty_without_global_retry(self):
        with mock.patch.object(procscan.os, "geteuid", return_value=os.geteuid()), \
             mock.patch.object(procscan.subprocess, "run", side_effect=OSError("ps failed")) as runner:
            self.assertEqual(procscan._ps_lines(), [])
            self.assertEqual(procscan.proc_tree(), {})
            self.assertEqual(procscan._pid_ttys(), {})

        self.assertEqual(runner.call_count, 3)
        self.assertTrue(all("-u" in call.args[0] for call in runner.call_args_list))

    def test_missing_effective_uid_fails_closed_before_ps(self):
        with mock.patch.object(procscan.os, "geteuid", side_effect=OSError("no uid")), \
             mock.patch.object(procscan.subprocess, "run") as runner:
            self.assertEqual(procscan._ps_lines(), [])
            self.assertEqual(procscan.proc_tree(), {})
            self.assertEqual(procscan._pid_ttys(), {})
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
