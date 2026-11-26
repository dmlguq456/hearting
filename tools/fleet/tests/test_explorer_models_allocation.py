"""DISPATCH ALLOCATION edits: current values, what a strategy ignores, preview, Apply, failure, reread."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import builtins
import json
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
        return next(row for row in rows if row.lstrip().startswith(("field tilt:", "field tilt ")))

    def test_closed_line_names_the_top_harness_of_each_leaning_row(self):
        _frame, rows = self.screen()
        line = self.line(rows)
        self.assertIn("table 11-20 · code→opencode  design→claude  docs→codex  "
                      "research→codex·opencode  lab→claude·codex  plan→opencode  ▸", line)
        self.assertNotIn("admin", line)
        self.assertEqual(sum("field tilt" in row for row in rows), 1)          # one line, in any form
        self.assertFalse(any(row.lstrip().startswith(("field:", "field rows")) for row in rows))

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
        self.assertFalse(any("field tilt" in row for row in rows))

    def test_the_tilt_line_sits_under_the_share_rows_and_is_the_only_one(self):
        _frame, rows = self.screen()
        at = next(i for i, row in enumerate(rows) if row.lstrip().startswith("field tilt "))
        self.assertTrue(rows[at - 1].lstrip().startswith("share opencode"), rows[at - 1])
        self.assertTrue(rows[at + 1].lstrip().startswith("worker prefers"), rows[at + 1])
        self.assertEqual(sum("field tilt" in row for row in rows), 1)
        self.assertFalse(any("read only" in row for row in rows))          # that wording lives in the open table
        self.policy(USER_ROWS)
        _frame, rows = self.screen()
        shares = [row for row in rows if row.lstrip().startswith("share ")]
        self.assertEqual([row.split()[1] for row in shares], ["claude", "codex", "opencode"])
        self.assertEqual(sum("field tilt" in row for row in self.screen(opened=True)[1]), 1)

    def test_the_open_table_ends_with_what_the_arrow_means_and_that_it_is_read_only(self):
        frame, rows = self.screen(opened=True)
        text = frame.text_rows()
        arrow = next(row for row in rows if "highest cell" in row)
        self.assertIn("→ is a row's highest cell · a row only multiplies the share above, never a band or a limit", arrow)
        note = next(row for row in rows if "read only" in row)
        self.assertEqual(note.strip(), "read only, edit the file to change")
        for needle in ("highest cell", "read only"):
            y = next(index for index, row in enumerate(text) if needle in row)
            self.assertEqual(frame.cells[y][text[y].index(needle)][1], "dim")
        self.assertLess(rows.index(arrow), rows.index(note))
        self.assertLess(next(i for i, row in enumerate(rows) if row.lstrip().startswith("admin")), rows.index(arrow))
        self.assertFalse(any("highest cell" in row for row in self.screen()[1]))      # the closed line adds nothing
        self.policy(USER_ROWS)
        _frame, rows = self.screen(opened=True)
        self.assertIn("your rows: design, docs · read only, edit the file to change",
                      next(row for row in rows if "read only" in row))

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
                         ["strategy:", "weights:", "field tilt:", "affinity:"])
        values = load_fixture(FIXTURES / "view_model.json").to_mapping()
        read_only = {key: value for key, value in allocation.items() if key != "values"}
        values["models"] = dict(values["models"], allocation=read_only)
        text = draw(ExplorerViewModel.from_mapping(values), NavState(view="MODELS", title="MODELS"),
                    200, 90, beat=1).text_rows()
        order = [next(index for index, row in enumerate(text) if label in row)
                 for label in ("strategy:", "weights:", "field tilt:", "affinity:")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(order[3] - order[1], 2)
        self.assertEqual(sum("field tilt" in row for row in text), 1)             # not drawn a second time
        self.assertFalse(any("field rows" in row or "field:" in row for row in text))

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


    # -- an unreadable field table must leave only its own line out ---------------------------

    BROKEN = {
        "a cell that is not a number": lambda t: t["weights"]["code"].update(claude="bad"),
        "basis a string": lambda t: t.update(basis="bad"),
        "basis a list": lambda t: t.update(basis=["bad"]),
        "basis profiles a string": lambda t: t["basis"].update(profiles="bad"),
        "basis profiles a list": lambda t: t["basis"].update(profiles=["bad"]),
        "basis profiles holding a non-list": lambda t: t["basis"].update(profiles={"claude": "bad", "codex": 1}),
        "weights a string": lambda t: t.update(weights="bad"),
        "a weights row that is not an object": lambda t: t["weights"].update(code="bad"),
        "a weights row that is a list": lambda t: t["weights"].update(code=[1, 2]),
    }

    def read_with_registry(self, change):
        registry = json.loads((ROOT / "capabilities" / "topologies.json").read_text())
        if change:
            change(registry["field_harness_weights"])
        redirected = Path(self.tmp.name) / "registry.json"
        redirected.write_text(json.dumps(registry))
        real_open = builtins.open

        def opening(path, *args, **kwargs):
            if str(path).endswith("/capabilities/topologies.json"):
                path = redirected
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", opening):
            return focus.read_model_config()

    def test_a_field_table_that_cannot_be_read_leaves_allocation_pools_and_steward_as_before(self):
        whole = self.read_with_registry(None)
        self.assertTrue(whole["allocation"].get("field_view") and whole["pools"] and whole["steward"])
        for name, change in self.BROKEN.items():
            with self.subTest(name):
                models = self.read_with_registry(change)
                for section in ("allocation", "pools", "steward"):
                    self.assertTrue(models[section], f"{name}: {section} came back empty")
                self.assertEqual(models["pools"], whole["pools"])
                self.assertEqual(models["steward"], whole["steward"])
                allocation = models["allocation"]
                # what the screen showed before this line existed is untouched; only the line may be missing
                self.assertEqual([label for label, _ in allocation["fields"] if label != "field tilt:"],
                                 [label for label, _ in whole["allocation"]["fields"] if label != "field tilt:"])
                self.assertEqual(allocation["values"], whole["allocation"]["values"])
                if name in ("a cell that is not a number", "basis a string", "basis a list", "weights a string"):
                    self.assertNotIn("field_view", allocation, name)
                    self.assertFalse(any("field tilt" in row for row in self.drawn(models)), name)

    def drawn(self, models, width=200):
        values = load_fixture(FIXTURES / "view_model.json").to_mapping()
        values["models"] = dict(values["models"], allocation=models["allocation"])
        frame = draw(ExplorerViewModel.from_mapping(values), NavState(view="MODELS", title="MODELS"), width, 150, beat=1)
        return frame.text_rows()

    # -- the toggle stays on the board and clickable at every narrow width ----------------------

    def frames(self, width, opened=False, with_view=True):
        models = focus.read_model_config()
        allocation = dict(models["allocation"])
        if not with_view:
            allocation.pop("field_view", None)
            allocation["fields"] = [field for field in allocation["fields"] if field[0] != "field tilt:"]
        values = load_fixture(FIXTURES / "view_model.json").to_mapping()
        values["models"] = dict(values["models"], allocation=allocation)
        state = NavState(view="MODELS", title="MODELS", folds=(("ui:fieldtable", "open"),) if opened else ())
        return ExplorerViewModel.from_mapping(values), state, draw(ExplorerViewModel.from_mapping(values), state, width, 150, beat=1)

    def test_the_toggle_is_drawn_inside_the_pane_with_its_click_target_on_that_cell_from_40_to_60(self):
        for width in range(40, 61):
            for opened in (False, True):
                with self.subTest(width=width, opened=opened):
                    vm, state, frame = self.frames(width, opened)
                    hits = [hit for hit in frame.hits if hit.action[:2] == ("ui", "fieldtable")]
                    self.assertEqual(len(hits), 1)
                    hit = hits[0]
                    self.assertEqual(hit.action[2], "closed" if opened else "open")
                    cell = frame.text_rows()[hit.y][hit.x0:hit.x1]
                    self.assertEqual(cell, "▾" if opened else "▸")        # the target is exactly the drawn cell
                    self.assertEqual(hit.x1 - hit.x0, 1)
                    self.assertLessEqual(hit.x1, width - 1)                 # inside the pane
                    marks = [(y, x) for y, row in enumerate(frame.text_rows()) if y == hit.y
                             for x, char in enumerate(row) if char in "▸▾"]
                    self.assertEqual(marks, [(hit.y, hit.x0)])
                    state, _effects = self.click(state, frame, hit.action)
                    again = draw(vm, state, width, 150, beat=1)
                    flipped = [h for h in again.hits if h.action[:2] == ("ui", "fieldtable")]
                    self.assertEqual(flipped[0].action[2], "open" if opened else "closed")
                    self.assertEqual(again.text_rows()[flipped[0].y][flipped[0].x0:flipped[0].x1], "▸" if opened else "▾")

    def test_the_chips_below_the_tilt_line_keep_their_click_targets(self):
        for width in (40, 45, 50, 55, 56, 60, 70, 80, 100, 120, 170):
            for opened in (False, True):
                with self.subTest(width=width, opened=opened):
                    _vm, _state, frame = self.frames(width, opened)
                    _vm, _state, before = self.frames(width, with_view=False)
                    line = next(hit.y for hit in frame.hits if hit.action[:2] == ("ui", "fieldtable"))
                    pane = lambda hits: sorted((hit.y, hit.x0, hit.x1, hit.action) for hit in hits
                                               if hit.x0 >= 33 and hit.action[:2] != ("ui", "fieldtable"))
                    now, reference = pane(frame.hits), pane(before.hits)
                    self.assertEqual([hit for hit in now if hit[0] < line], [hit for hit in reference if hit[0] < line])
                    below_now = [hit for hit in now if hit[0] > line]
                    below_ref = [hit for hit in reference if hit[0] >= line]
                    self.assertEqual(len(below_now), len(below_ref))
                    shifts = {a[0] - b[0] for a, b in zip(below_now, below_ref)}
                    self.assertLessEqual(len(shifts), 1, shifts)                 # the whole block moved by one amount
                    if not opened and shifts:
                        self.assertEqual(shifts, {1})                             # the one added line
                    self.assertEqual([hit[1:] for hit in below_now], [hit[1:] for hit in below_ref])


if __name__ == "__main__":
    unittest.main()
