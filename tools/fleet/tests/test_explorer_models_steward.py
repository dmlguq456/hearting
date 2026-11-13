"""SUPERVISION edits: current interval and per-repo limit, preview, Apply, failure, reread."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import shutil
import stat
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tools.fleet.explorer import focus
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import ExplorerViewModel, load_fixture

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "explorer"


class StewardEditor(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="fleet-steward-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        environment = {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"),
                       "XDG_STATE_HOME": str(root / "state"), "XDG_DATA_HOME": str(root / "data"),
                       "XDG_CACHE_HOME": str(root / "cache"), "CLAUDE_CONFIG_DIR": str(root / "claude"),
                       "CODEX_HOME": str(root / "codex"), "AGENT_HOME": str(ROOT), "FLEET_DEMO": "",
                       "TMPDIR": str(root)}
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
        self.path = root / "config" / "hearting" / "dispatch-defaults.yaml"
        self.path.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "profiles" / "dispatch-defaults.yaml", self.path)
        self.original = self.path.read_bytes()

        patch = mock.patch("tools.fleet.collectors.steward._last_supervision", None)
        patch.start()
        self.addCleanup(patch.stop)

    def vm(self):
        values = load_fixture(FIXTURES / "view_model.json").to_mapping()
        values["models"] = dict(values["models"], steward=focus.read_model_config()["steward"])
        return ExplorerViewModel.from_mapping(values)

    @staticmethod
    def click(state, frame, action):
        hit = next(hit for hit in frame.hits if hit.action == action)
        return reduce(state, {"type": "mouse", "phase": "press", "button": 0, "x": hit.x0, "y": hit.y}, frame)

    def test_current_values_preview_apply_and_reread(self):
        vm = self.vm()
        self.assertEqual(vm.models["steward"]["values"],
                         {"check_interval_seconds": 7200, "max_supervisors_per_repo": 2})
        state = NavState(view="MODELS", title="MODELS")
        frame = draw(vm, state, 160, 120, beat=1)
        text = "\n".join(frame.text_rows())
        self.assertIn("SUPERVISION", text)
        self.assertIn("[ 2h ]", text)
        self.assertIn("· / 2 per repo (not counted yet)", text)
        self.assertIn("affects new checks and new supervisor grants only", text)
        state, _effects = self.click(state, frame, ("ui", "steward:check_interval_seconds", "1800"))
        frame = draw(vm, state, 160, 120, beat=1)
        state, _effects = self.click(state, frame, ("ui", "steward:max_supervisors_per_repo", "3"))
        frame = draw(vm, state, 160, 120, beat=1)
        self.assertIn("check every 2h → 30m  ·  per repo 2 → 3", "\n".join(frame.text_rows()))
        payload = (("steward.check_interval_seconds", "1800"), ("steward.max_supervisors_per_repo", "3"))
        state, effects = self.click(state, frame, ("alloc.apply", payload, True))
        self.assertEqual(effects, (("alloc.apply", payload, True),))

        runtime = ExplorerRuntime()
        planned = runtime.apply_effects(effects, vm)
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(self.path.read_bytes(), self.original)
        applied = runtime.apply_effects([("alloc.apply", payload, False)], vm)
        self.assertEqual(applied["status"], "changed")
        self.assertEqual(Path(str(self.path) + ".bak").read_bytes(), self.original)
        self.assertEqual(applied["models"]["steward"]["values"],
                         {"check_interval_seconds": 1800, "max_supervisors_per_repo": 3})
        changed = [line.split("#", 1)[0].rstrip() for line in self.path.read_text().splitlines()
                   if line not in self.original.decode().splitlines()]
        self.assertEqual(changed, ["  check_interval_seconds: 1800", "  max_supervisors_per_repo: 3"])

        vm = replace(vm, models=applied["models"])
        state = replace(state, status=applied["message"])
        text = "\n".join(draw(vm, state, 160, 120, beat=1).text_rows())
        self.assertNotIn("[ dry-run ]  [ apply ]", text.split("SUPERVISION", 1)[1])
        self.assertIn("steward: set check_interval_seconds: 7200 -> 1800", text)

    def test_out_of_range_and_write_failure_are_shown_and_leave_the_file(self):
        vm = self.vm()
        runtime = ExplorerRuntime()
        for payload in ((("steward.check_interval_seconds", "300"),),
                        (("steward.max_supervisors_per_repo", "9"),),
                        (("steward.max_supervisors_per_repo", "two"),)):
            bad = runtime.apply_effects([("alloc.apply", payload, False)], vm)
            self.assertEqual(bad["status"], "invalid-arguments")
            self.assertTrue(bad["message"].startswith("steward: invalid-arguments:"))
        self.assertEqual(self.path.read_bytes(), self.original)
        state = NavState(view="MODELS", title="MODELS", status=bad["message"])
        frame = draw(vm, state, 160, 120, beat=1)
        y, x = next((index, row.find("steward: invalid-arguments"))
                    for index, row in enumerate(frame.text_rows()) if "steward: invalid-arguments" in row)
        self.assertEqual(frame.cells[y][x][1], "g_blocked")

        self.path.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(self.path.parent.chmod, stat.S_IRWXU)
        failed = runtime.apply_effects([("alloc.apply", (("steward.max_supervisors_per_repo", "1"),), False)], vm)
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["message"].startswith("steward: failed:"))
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_supervisor_census_comes_from_the_collector_pass(self):
        here = {"repository": "hearting", "repository_key": "/r/hearting/.git", "counted": 3, "limit": 2,
                "over_limit": True,
                "holders": [{"harness": "claude", "session_id": "a" * 36, "name": "alpha", "liveness": "live",
                             "basis": "role", "counted": True},
                            {"harness": "codex", "session_id": "b" * 36, "name": None, "liveness": "unknown",
                             "basis": "grant", "counted": True, "seat_key": "PRIVATE-SEAT-HASH"},
                            {"harness": "claude", "session_id": "c" * 36, "name": "gamma", "liveness": "ended",
                             "basis": "role", "counted": False},
                            {"harness": "claude", "session_id": "d" * 36, "name": "delta", "liveness": "live",
                             "basis": "role", "counted": True}]}
        other = dict(here, repository="beta", repository_key="/r/beta/.git", counted=1, over_limit=False,
                     holders=[])
        census = {"/r/beta/.git": other, "/r/hearting/.git": here}
        with mock.patch("tools.fleet.collectors.steward._last_supervision", census), \
                mock.patch("tools.fleet.collectors.steward._repository_key", return_value="/r/hearting/.git"), \
                mock.patch("tools.fleet.collectors.steward.read_markers") as markers:
            vm = self.vm()
            markers.assert_not_called()
        self.assertEqual([repo["repository"] for repo in vm.models["steward"]["supervisors"]],
                         ["hearting", "beta"])
        frame = draw(vm, NavState(view="MODELS", title="MODELS"), 200, 120, beat=1)
        rows = frame.text_rows()
        y, row = next((index, row) for index, row in enumerate(rows) if "hearting · 3 / 2" in row)
        self.assertIn("alpha, bbbbbbbb, gamma (ended), delta · over limit", row)
        self.assertEqual(frame.cells[y][row.find("hearting · 3")][1], "g_blocked")
        self.assertTrue(any("beta · 1 / 2 supervising" in row for row in rows))
        self.assertTrue(any("bbbbbbbb: unknown" in row and "role marker" in row for row in rows))
        self.assertNotIn("PRIVATE-SEAT-HASH", "\n".join(rows))
        self.assertFalse(any("· seat " in row for row in rows))

        with mock.patch("tools.fleet.collectors.steward._last_supervision", {}), \
                mock.patch("tools.fleet.collectors.steward._repository_key", return_value="/r/hearting/.git"):
            vm = self.vm()
        text = "\n".join(draw(vm, NavState(view="MODELS", title="MODELS"), 200, 120, beat=1).text_rows())
        self.assertIn("hearting · 0 / 2 supervising", text)


if __name__ == "__main__":
    unittest.main()
