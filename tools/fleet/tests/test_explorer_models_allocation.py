"""DISPATCH ALLOCATION edits: current values, what a strategy ignores, preview, Apply, failure, reread."""
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


class AllocationEditor(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="fleet-allocation-")
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

    def vm(self):
        values = load_fixture(FIXTURES / "view_model.json").to_mapping()
        values["models"] = dict(values["models"], allocation=focus.read_model_config()["allocation"])
        return ExplorerViewModel.from_mapping(values)

    @staticmethod
    def click(state, frame, action):
        hit = next(hit for hit in frame.hits if hit.action == action)
        state, effects = reduce(state, {"type": "mouse", "phase": "press", "button": 0,
                                        "x": hit.x0, "y": hit.y}, frame)
        return state, effects

    def test_current_values_preview_apply_and_reread(self):
        vm = self.vm()
        allocation = vm.models["allocation"]
        self.assertTrue(allocation["user_file"])
        self.assertEqual(allocation["values"]["usage_gate_used_percent"], 85)
        state = NavState(view="MODELS", title="MODELS")
        frame = draw(vm, state, 160, 90, beat=1)
        state, _effects = self.click(state, frame, ("ui", "alloc:usage_gate_used_percent", "80"))
        frame = draw(vm, state, 160, 90, beat=1)
        state, _effects = self.click(state, frame, ("ui", "alloc:harness_weights.codex", "0.7"))
        frame = draw(vm, state, 160, 90, beat=1)
        text = "\n".join(frame.text_rows())
        self.assertIn("usage gate % 85 → 80  ·  share codex 1.0 → 0.7", text)
        payload = (("usage_gate_used_percent", "80"), ("harness_weights.codex", "0.7"))
        state, effects = self.click(state, frame, ("alloc.apply", payload, True))
        self.assertEqual(effects, (("alloc.apply", payload, True),))

        runtime = ExplorerRuntime()
        planned = runtime.apply_effects(effects, vm)
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(self.path.read_bytes(), self.original)
        applied = runtime.apply_effects([("alloc.apply", payload, False)], vm)
        self.assertEqual(applied["status"], "changed")
        self.assertEqual(Path(str(self.path) + ".bak").read_bytes(), self.original)
        reread = applied["models"]["allocation"]["values"]
        self.assertEqual(reread["usage_gate_used_percent"], 80)
        self.assertEqual(reread["harness_weights"]["codex"], 0.7)
        changed = [line for line in self.path.read_text().splitlines()
                   if line not in self.original.decode().splitlines()]
        self.assertEqual(changed, ["  usage_gate_used_percent: 80", "    codex: 0.7"])

        vm = replace(vm, models=applied["models"])
        state = replace(state, status=applied["message"])
        text = "\n".join(draw(vm, state, 160, 90, beat=1).text_rows())
        self.assertNotIn("[ dry-run ]  [ apply ]", text.split("DISPATCH ALLOCATION", 1)[1])
        self.assertIn("allocation: set usage_gate_used_percent: 85 -> 80", text)

    def test_strategy_choice_marks_the_keys_it_ignores(self):
        vm = self.vm()
        state = NavState(view="MODELS", title="MODELS", folds=(("ui:alloc:strategy", "capacity-aware"),))
        rows = draw(vm, state, 200, 90, beat=1).text_rows()
        gate = next(row for row in rows if "usage gate %" in row)
        self.assertIn("ignored under capacity-aware", gate)
        strength = next(row for row in rows if "prefer strength" in row)
        self.assertIn("tie-break only", strength)
        state = NavState(view="MODELS", title="MODELS")
        rows = draw(vm, state, 200, 90, beat=1).text_rows()
        self.assertNotIn("ignored under", "\n".join(rows))

    def test_invalid_value_and_write_failure_are_shown_and_leave_the_file(self):
        vm = self.vm()
        runtime = ExplorerRuntime()
        bad = runtime.apply_effects([("alloc.apply", (("usage_gate_used_percent", "101"),), False)], vm)
        self.assertEqual(bad["status"], "invalid-arguments")
        self.assertIn("0 to 100", bad["message"])
        self.assertEqual(self.path.read_bytes(), self.original)
        state = NavState(view="MODELS", title="MODELS", status=bad["message"])
        frame = draw(vm, state, 160, 90, beat=1)
        y, x = next((index, row.find("allocation: invalid-arguments"))
                    for index, row in enumerate(frame.text_rows()) if "allocation: invalid-arguments" in row)
        self.assertEqual(frame.cells[y][x][1], "g_blocked")

        self.path.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(self.path.parent.chmod, stat.S_IRWXU)
        failed = runtime.apply_effects([("alloc.apply", (("window", "60"),), False)], vm)
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["message"].startswith("allocation: failed:"))
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_demo_is_read_only(self):
        with mock.patch.dict(os.environ, {"FLEET_DEMO": "1"}):
            result = ExplorerRuntime().apply_effects([("alloc.apply", (("window", "60"),), False)], self.vm())
        self.assertEqual(result["status"], "read-only")
        self.assertEqual(self.path.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
