#!/usr/bin/env python3
"""A begin's layout scan reads each cycle folder's marker once, with one lstat.

Every route start runs the D-126 scan over every cycle folder of the artifact root. On a
network filesystem each extra stat or read of a folder's `.cycle.json` is a round trip, so
the count of those per folder is the cost of a start on a large root (2026-11-27: a real
root of 3,352 cycle folders spent most of its 30-70 s before registration in this scan).
The marker's answers must stay what they were: absent is None, anything but a regular
file or a well-formed binding is `locator-cycle-binding-invalid`, and an invalid marker
still stops the scan where `artifact_request.members` read it before.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_locator as L  # noqa: E402
import artifact_producer_cycle_ops as OPS  # noqa: E402

spec = importlib.util.spec_from_file_location("scan_reads_fixture", Path(__file__).with_name("artifact_producer.test.py"))
FX = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FX)
P = FX.P


def _marker_calls(calls):
    return [c for c in calls if str(c).endswith(L.CYCLE_BINDING)]


class ScanReadsTest(FX.ProducerTestBase):
    def setUp(self):
        inherited = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("AGENT_")}
        self.addCleanup(os.environ.update, inherited)
        super().setUp()
        patcher = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(self._tmp.name) / "config"),
            "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
            "HEARTING_WORKFLOW_GROUP_REVIEW": "off",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.activate()

    def begin_cycles(self, count):
        folders = []
        for index in range(count):
            _route, route_file = self.route(slug=f"scan-{index}", campaign_key="scan")
            begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
            folders.append(L.resolve_path(self.root, begun["cycle_id"]))
        return folders

    def test_scan_reads_each_cycle_marker_once(self):
        folders = self.begin_cycles(3)
        real = L.read_cycle_binding
        seen = []

        def counting(path):
            seen.append(Path(path))
            return real(path)

        with mock.patch.object(L, "read_cycle_binding", side_effect=counting):
            scan = OPS._scan_layout(self.root)
        self.assertTrue(scan.complete, scan.blockers)
        self.assertEqual(sorted(scan.cycles.values()), sorted(folders))
        per_folder = {folder: seen.count(folder) for folder in folders}
        self.assertEqual(per_folder, {folder: 1 for folder in folders})

    def test_marker_read_takes_one_path_check(self):
        folder = self.begin_cycles(1)[0]
        stats, lstats = [], []
        real_stat, real_lstat = os.stat, os.lstat

        def stat(path, *args, **kwargs):
            stats.append(path)
            return real_stat(path, *args, **kwargs)

        def lstat(path, *args, **kwargs):
            lstats.append(path)
            return real_lstat(path, *args, **kwargs)

        with mock.patch("os.stat", side_effect=stat), mock.patch("os.lstat", side_effect=lstat):
            binding = L.read_cycle_binding(folder)
        self.assertTrue(L._CYCLE_ID.fullmatch(binding["cycle_id"]), binding)
        self.assertEqual(len(_marker_calls(stats)) + len(_marker_calls(lstats)), 1)

    def test_marker_answers_are_unchanged(self):
        folder = self.begin_cycles(1)[0]
        marker = folder / L.CYCLE_BINDING
        good = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(L.read_cycle_binding(folder), good)
        self.assertIsNone(L.read_cycle_binding(folder / "missing-child"))
        self.assertIsNone(L.read_cycle_binding(marker))  # a path below a file: not-a-directory
        target = folder / "elsewhere.json"
        target.write_text(json.dumps(good), encoding="utf-8")
        for shape in ("symlink", "dangling", "directory", "not-json", "not-object"):
            marker.unlink() if marker.is_symlink() or marker.is_file() else marker.rmdir()
            if shape == "symlink":
                marker.symlink_to(target)
            elif shape == "dangling":
                marker.symlink_to(folder / "nowhere.json")
            elif shape == "directory":
                marker.mkdir()
            elif shape == "not-json":
                marker.write_text("{", encoding="utf-8")
            else:
                marker.write_text("[]", encoding="utf-8")
            with self.subTest(shape=shape), self.assertRaises(L.LocatorError) as caught:
                L.read_cycle_binding(folder)
            self.assertEqual(caught.exception.code, "locator-cycle-binding-invalid")
        marker.rmdir() if marker.is_dir() and not marker.is_symlink() else marker.unlink()
        marker.write_text(json.dumps(good), encoding="utf-8")

    def test_invalid_marker_still_stops_the_scan(self):
        folder = self.begin_cycles(1)[0]
        cycle_id = L.read_cycle_binding(folder)["cycle_id"]
        # A sealed cycle's manifest names the cycle, so the folder is still read as one.
        if not (folder / "manifest.json").exists():
            (folder / "manifest.json").write_text(json.dumps({"cycle": {"cycle_id": cycle_id}}), encoding="utf-8")
        (folder / L.CYCLE_BINDING).write_text("[]", encoding="utf-8")
        with self.assertRaises(L.LocatorError) as caught:
            OPS._scan_layout(self.root)
        self.assertEqual(caught.exception.code, "locator-cycle-binding-invalid")


if __name__ == "__main__":
    unittest.main()
