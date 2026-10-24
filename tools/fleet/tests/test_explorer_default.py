"""The default CLI remains byte-identical to the pre-explorer base snapshot."""
import contextlib
import io
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet import fleet, render
from tools.fleet.refresh import LiveSnapshot


FIXTURE = Path(__file__).parent / "fixtures" / "explorer" / "default-once.json"


class CoreDefault(unittest.TestCase):
    def test_once_matches_base_cli_bytes_at_all_widths(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(data["baseline_commit"], "00b4ba0168efc35eaf74603b8c9364f259c5adc4")
        self.assertEqual(set(data["outputs"]), {"80", "100", "140", "168"})
        with mock.patch.object(fleet, "collect_all", return_value=LiveSnapshot()), \
             mock.patch.object(fleet, "_arm_stall_dump", return_value=None), \
             mock.patch.object(fleet.installinfo, "collect", return_value={}), \
             mock.patch.object(fleet.compute_hosts, "collect", return_value=[]), \
             mock.patch.object(render, "_collect_governor", return_value=None), \
             mock.patch.object(render, "_collect_memory", return_value=None):
            for width in data["widths"]:
                stdout, stderr = io.StringIO(), io.StringIO()
                size = shutil.os.terminal_size((width, 45))
                with self.subTest(width=width), \
                     mock.patch("shutil.get_terminal_size", return_value=size), \
                     mock.patch.object(render.time, "time", return_value=1_800_000_000), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = fleet.main(["--once"])
                expected = data["outputs"][str(width)]
                self.assertEqual(code, expected["exit"])
                self.assertEqual(stderr.getvalue(), expected["stderr"])
                self.assertEqual(stdout.getvalue().encode(), expected["stdout"].encode())
