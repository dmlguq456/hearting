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


class _Isolated(unittest.TestCase):
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


class AllocationEditor(_Isolated):

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


USER_ROWS = """    claude: 0.5
    # kept comment
    design:
      codex: 1.0
      claude: 0.7
    docs: 0.5
    opencode: 0.3
"""


class FieldRows(_Isolated):
    """The per-field line, its table, and the editor leaving the user's field blocks alone."""

    def policy(self, rows=None, strategy=None):
        text = self.original.decode()
        if rows is not None:
            head, _sep, tail = text.partition("  harness_weights:\n    claude: 0.5\n    opencode: 0.3\n")
            text = head + "  harness_weights:\n" + rows + tail
        if strategy:
            text = text.replace("\n  strategy: balanced\n", f"\n  strategy: {strategy}\n", 1)
        self.path.write_text(text)
        self.original = self.path.read_bytes()

    def screen(self, width=200, opened=False):
        folds = (("ui:fieldtable", "open"),) if opened else ()
        state = NavState(view="MODELS", title="MODELS", folds=folds)
        frame = draw(self.vm(), state, width, 90, beat=1)
        text = frame.text_rows()
        column = next(row.index("DISPATCH ALLOCATION") for row in text if "DISPATCH ALLOCATION" in row)
        start = next(index for index, row in enumerate(text) if "DISPATCH ALLOCATION" in row)
        return frame, [row[column - 1:].rstrip() for row in text[start + 1:] if row[column - 1:].strip()]

    @staticmethod
    def line(rows):
        return next(row for row in rows if row.lstrip().startswith("field:"))

    def test_closed_line_names_the_top_harness_of_each_leaning_row(self):
        _frame, rows = self.screen()
        line = self.line(rows)
        self.assertIn("table 11-20 · code→opencode  design→claude  docs→codex  "
                      "research→codex·opencode  lab→claude·codex  plan→opencode  ▸", line)
        self.assertNotIn("admin", line)
        self.assertNotIn("field ", "\n".join(rows).replace("field rows", "").replace("field:", ""))

    def test_clicking_the_toggle_opens_and_closes_the_table(self):
        vm = self.vm()
        state = NavState(view="MODELS", title="MODELS")
        frame = draw(vm, state, 200, 90, beat=1)
        state, _effects = self.click(state, frame, ("ui", "fieldtable", "open"))
        frame = draw(vm, state, 200, 90, beat=1)
        text = "\n".join(frame.text_rows())
        self.assertIn("▾", text)
        self.assertRegex(text, r"code\s+0\.95\s+0\.85\s+1\.00")
        self.assertRegex(text, r"admin·other\s+1\.00\s+1\.00\s+1\.00\s+table 11-20 · basis: shipped model table")
        self.assertNotIn("this install differs", text)
        state, _effects = self.click(state, frame, ("ui", "fieldtable", "closed"))
        self.assertNotIn("0.95", "\n".join(draw(vm, state, 200, 90, beat=1).text_rows()))

    def test_block_override_is_marked_and_a_number_switches_the_row_off(self):
        self.policy(USER_ROWS)
        frame, rows = self.screen()
        line = self.line(rows)
        self.assertIn("design→codex", line)
        self.assertNotIn("docs", line)                                 # one number: all cells equal, off
        y, x = next((index, row.find("design→")) for index, row in enumerate(frame.text_rows())
                    if "design→" in row)
        self.assertEqual(frame.cells[y][x][1], "g_idle")                # overridden field: the lvl_y key
        self.assertEqual(frame.cells[y][x + len("design→")][1], "hb_codex")
        y, x = next((index, row.find("code→")) for index, row in enumerate(frame.text_rows()) if "code→" in row)
        self.assertEqual(frame.cells[y][x][1], "loc_repo")
        frame, rows = self.screen(opened=True)
        design = next(row for row in rows if row.lstrip().startswith("design"))
        self.assertRegex(design, r"design\s+0\.70\s+1\.00\s+0\.85")
        docs = next(row for row in rows if row.lstrip().startswith("docs"))
        self.assertRegex(docs, r"docs\s+0\.50\s+0\.50\s+0\.50")
        text = frame.text_rows()
        y = next(index for index, row in enumerate(text) if " docs " in row and "0.50" in row)
        self.assertEqual(frame.cells[y][text[y].index("0.50")][1], "dim")        # an off row is dim
        y = next(index for index, row in enumerate(text) if " design " in row and "0.70" in row)
        self.assertEqual(frame.cells[y][text[y].index("0.70")][1], "g_idle")     # the overridden cell
        self.assertEqual(frame.cells[y][text[y].index("0.85")][1], "loc_repo")

    def test_narrow_width_drops_trailing_fields_but_keeps_the_toggle(self):
        _frame, rows = self.screen(width=100)
        line = self.line(rows)
        self.assertTrue(line.rstrip().endswith("…  ▸"), line)
        self.assertIn("code→opencode", line)
        self.assertNotIn("plan→opencode", line)

    def test_other_strategy_has_no_field_line(self):
        self.policy(strategy="least-recent-attempts")
        _frame, rows = self.screen()
        self.assertFalse(any(row.lstrip().startswith("field:") for row in rows))
        self.assertFalse(any("field rows" in row and "registry table" in row for row in rows))

    def test_editor_shows_field_overrides_and_no_field_names_in_share_rows(self):
        _frame, rows = self.screen()
        self.assertTrue(any("field rows" in row and "registry table 11-20" in row for row in rows))
        self.policy(USER_ROWS)
        _frame, rows = self.screen()
        note = next(row for row in rows if "field rows" in row)
        self.assertIn("your rows: design, docs", note)
        shares = [row for row in rows if row.lstrip().startswith("share ")]
        self.assertEqual([row.split()[1] for row in shares], ["claude", "codex", "opencode"])

    def test_saving_another_value_keeps_the_field_blocks_comments_and_order(self):
        self.policy(USER_ROWS)
        runtime = ExplorerRuntime()
        for update in (("usage_gate_used_percent", "80"), ("harness_weights.codex", "0.7"),
                       ("harness_weights.claude", "0.3")):
            applied = runtime.apply_effects([("alloc.apply", (update,), False)], self.vm())
            self.assertEqual(applied["status"], "changed", applied)
        before, after = self.original.decode().splitlines(), self.path.read_text().splitlines()
        self.assertEqual([line for line in before if line not in after],
                         ["  usage_gate_used_percent: 85", "    claude: 0.5"])
        for kept in ("    # kept comment", "    design:", "      codex: 1.0", "      claude: 0.7", "    docs: 0.5"):
            self.assertIn(kept, after)
        block = after[after.index("  harness_weights:"):]
        order = [line.strip() for line in block[:9]]
        self.assertEqual(order[:8], ["harness_weights:", "claude: 0.3", "# kept comment", "design:",
                                     "codex: 1.0", "claude: 0.7", "docs: 0.5", "opencode: 0.3"])
        reread = applied["models"]["allocation"]
        self.assertEqual(reread["field_overrides"], ["design", "docs"])
        self.assertEqual(sorted(reread["values"]["harness_weights"]), ["claude", "codex", "opencode"])


    def test_read_only_lines_put_the_field_line_between_weights_and_affinity(self):
        allocation = focus.read_model_config()["allocation"]
        self.assertEqual([label for label, _parts in allocation["fields"]][:4],
                         ["strategy:", "weights:", "field:", "affinity:"])
        values = load_fixture(FIXTURES / "view_model.json").to_mapping()
        read_only = {key: value for key, value in allocation.items() if key != "values"}
        values["models"] = dict(values["models"], allocation=read_only)
        text = draw(ExplorerViewModel.from_mapping(values), NavState(view="MODELS", title="MODELS"),
                    200, 90, beat=1).text_rows()
        order = [next(index for index, row in enumerate(text) if label in row)
                 for label in ("strategy:", "weights:", "field:", "affinity:")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(order[3] - order[1], 2)

    def test_a_model_table_unlike_the_shipped_one_adds_a_dim_note_to_the_open_table(self):
        shipped = (ROOT / "adapters" / "codex" / "config" / "models.conf").read_text()
        user = Path(os.environ["CODEX_HOME"]) / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(shipped.replace("CFG_TIER_DEEP_MODEL=gpt-6.1-sol", "CFG_TIER_DEEP_MODEL=gpt-6-invented"))
        frame, rows = self.screen(opened=True)
        note = next(row for row in rows if "this install differs" in row)
        self.assertIn("codex deep gpt-6-invented, codex balanced-deep gpt-6-invented", note)
        text = frame.text_rows()
        y = next(index for index, row in enumerate(text) if "this install differs" in row)
        self.assertEqual(frame.cells[y][text[y].index("this install")][1], "dim")
        _frame, closed = self.screen()
        self.assertFalse(any("this install differs" in row for row in closed))


if __name__ == "__main__":
    unittest.main()
