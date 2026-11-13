"""The shell panel's LAB section (draft A): its projection from the shared experiment board.

Fixtures are synthetic projects in a ``TemporaryDirectory`` with their own HOME and without
``AGENT_ARTIFACT_ROOT`` (the catalog test's ``Base``); run records come from the real
``resource_progress`` writer.  No real artifact root, session, or account is read.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for _path in (str(ROOT / "utilities"), str(ROOT / "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
_spec = importlib.util.spec_from_file_location("experiment_catalog_test_base_for_shell", ROOT / "utilities/experiment_catalog.test.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

import experiment_board as B  # noqa: E402
import experiment_catalog as E  # noqa: E402
from fleet import session_lab  # noqa: E402  (the module the shell backend loads)
from fleet.explorer import lab_table  # noqa: E402

NOW = T.NOW
GROUP = "AMI 가변 화자 개선"
HEALTHY = {"state": "ok", "seconds_since_anchor": 60.0}
STUCK = {"state": "stagnant", "seconds_since_anchor": 9000.0}


def condition(name, run_id):
    return {"display_name": name, "group": GROUP, "refs": {"runs": [{"run_id": run_id}]}}


def texts(parts):
    return "".join(part["text"] for part in parts)


class ShellLab(T.Base):
    def setUp(self):
        super().setUp()
        for clear in (session_lab.clear_cache, B.clear_cache):
            clear()
            self.addCleanup(clear)

    def project(self, *, failed=True):
        root = self.make_root()
        self.register(root, {"groups": [T.group_spec()], "conditions": [
            condition("0.3초 이하 짧은 발화 유지", "run-a"), condition("짧은 발화 제외", "run-b"),
            condition("화자 수 고정", "run-c")]})
        self.write_run(root, "run-a", stagnation=HEALTHY, series={"wer": [(10, 21.0), (12, 18.5)]})
        self.write_run(root, "run-b", status="failed" if failed else "running", stagnation=HEALTHY)
        self.write_run(root, "run-c", stagnation=STUCK)
        return root

    def test_the_projection_is_the_boards_in_fleets_own_words(self):
        root = self.project()
        board, watch = session_lab.read(root.parent, now=NOW)
        self.assertEqual(watch[:2], [str(E.catalog_path(root)), str(root / "resource-runs")])
        self.assertIn(str(root / "resource-runs" / "run-b.json"), watch)
        view = session_lab.view(board, NOW)
        group = board["groups"][0]
        self.assertEqual((view["key"], view["count"]), ("lab:" + group["id"], "3"))
        self.assertEqual(view["clock"], B.clock_text(board["generated_at"], NOW))
        # The same cells Fleet's LAB table draws for the same rows: one source, one wording.
        for row, shown in zip(group["rows"], view["rows"]):
            fleet = lab_table.cells(row, group, NOW)
            self.assertEqual(shown["name"]["text"], fleet["name"][0])
            for column in ("latest", "delta", "state"):
                self.assertEqual(shown["cells"][column]["text"], fleet[column][0], column)
            self.assertEqual(shown["cells"]["progress"]["text"], "" if fleet["progress"][0] == "?" else fleet["progress"][0])
            for piece in shown["more"]:
                self.assertIn(texts(piece["parts"]).split(" ")[-1], fleet[piece["col"]][0])
                self.assertNotIn(fleet[piece["col"]][0], ("·", "?"))  # an unknown value is no piece
        # The head counts the failed and the stuck conditions; the board's headline names the failed one.
        self.assertEqual([texts(c) for c in view["counts"]],
                         [lab_table.ui_text("fleet.lab.n_failed", n=1), lab_table.ui_text("fleet.lab.n_stagnant", n=1)])
        self.assertEqual(view["counts"][0][0]["key"], "lvl_r")
        self.assertEqual(view["headline"]["left"][0]["text"], "짧은 발화 제외")
        self.assertEqual(view["goal"][0]["text"], T.group_spec()["goal"])
        self.assertEqual(([p["text"] for p in view["strip"]], view["strip_attention"]), (["!2", "LB"], True))
        # A healthy board: the running count, the best latest value against its baseline, `3 LB`.
        B.clear_cache()
        healthy = self.make_root("ok", "b")
        self.register(healthy, {"groups": [T.group_spec()], "conditions": [condition("짧은 발화 유지", "run-a")]})
        self.write_run(healthy, "run-a", stagnation=HEALTHY, series={"wer": [(10, 21.0), (12, 18.5)]})
        view = session_lab.view(session_lab.read(healthy.parent, now=NOW)[0], NOW)
        self.assertEqual([texts(c) for c in view["counts"]], [lab_table.ui_text("fleet.lab.n_running", n=1)])
        self.assertEqual(texts(view["headline"]["left"]), "짧은 발화 유지 18.50%@12")
        self.assertEqual(texts(view["headline"]["right"]), "fix 19.99%")
        self.assertEqual([p["text"] for p in view["strip"]], ["1", " LB"])

    def test_no_experiments_draw_no_section_and_a_folder_without_a_root_reads_nothing(self):
        blank = self.make_root("blank", "c")
        board, watch = session_lab.read(blank.parent, now=NOW)
        self.assertFalse(board["visible"])
        self.assertIsNone(session_lab.view(board, NOW))
        self.assertIn(str(E.catalog_path(blank)), watch)   # a catalog made later still redraws
        plain = self.base / "plain"
        plain.mkdir()
        self.assertEqual(session_lab.read(plain, now=NOW), (None, []))
        self.assertEqual(session_lab.read(None, now=NOW), (None, []))
        self.assertIsNone(session_lab.view(None, NOW))

    def test_a_renamed_condition_redraws_with_its_new_name(self):
        root = self.project()
        first = session_lab.view(session_lab.read(root.parent, now=NOW)[0], NOW)
        self.assertEqual(first["rows"][1]["name"]["text"], "짧은 발화 제외")
        E.run_write(root, lambda ctx: E.op_rename(ctx, "짧은 발화 제외", "짧은 발화를 뺀 학습"), now=NOW + 1)
        board, watch = session_lab.read(root.parent, now=NOW + 2)    # inside the board's 60 s limit
        second = session_lab.view(board, NOW + 2)
        self.assertEqual(second["rows"][1]["name"]["text"], "짧은 발화를 뺀 학습")
        self.assertEqual(second["headline"]["left"][0]["text"], "짧은 발화를 뺀 학습")
        self.assertIn(str(E.catalog_path(root)), watch)

    def test_a_broken_catalog_or_unnamed_runs_are_unconfirmed_never_no_experiments(self):
        missing = self.make_root()
        self.write_run(missing, "run-x")
        self.write_run(missing, "run-y")
        view = session_lab.view(session_lab.read(missing.parent, now=NOW)[0], NOW)
        self.assertEqual((view["count"], [p["text"] for p in view["strip"]]), ("", ["?2", "LB"]))
        self.assertEqual(texts(view["goal"]), lab_table.ui_text("fleet.lab.catalog_missing"))
        self.assertEqual(texts(view["headline"]["left"]), lab_table.ui_text("fleet.lab.unnamed_runs"))
        self.assertIn("run-x", texts(view["unlinked"]))
        self.assertEqual([texts(c) for c in view["counts"]], [lab_table.ui_text("fleet.lab.n_unnamed", n=2)])
        broken = self.make_root("broken", "b")
        path = E.catalog_path(broken)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        view = session_lab.view(session_lab.read(broken.parent, now=NOW)[0], NOW)
        self.assertEqual([p["text"] for p in view["strip"]], ["?1", "LB"])
        self.assertEqual(texts(view["goal"]), lab_table.ui_text("fleet.lab.unreadable", code=B.read(broken, now=NOW, cache={})[0]["catalog"]["code"]))
        # A read that fails is the same: unconfirmed, never an empty board.
        B.clear_cache()
        with mock.patch.object(E, "status", side_effect=RuntimeError("boom")):
            view = session_lab.view(session_lab.read(missing.parent, now=NOW + 5)[0], NOW + 5)
        self.assertEqual([p["text"] for p in view["strip"]], ["?1", "LB"])

    def project_panel(self, harness, cwd):
        import session_shell as shell
        import human_wait_state
        from fleet import session_view
        observed = lambda *args, **kwargs: ({"cwd": str(cwd)}, [])
        with mock.patch.object(human_wait_state, "cached_statusline", return_value=(None, "")), \
                mock.patch.object(session_view, "observe", observed), \
                mock.patch.object(shell, "_codex_footer_plan", return_value=(None, None)):
            return shell.panel_projection(harness, "00000000-0000-4000-8000-00000000lab1", native_pid=os.getpid(),
                                          native_start="1", columns=40, rows=30)

    def test_the_panel_projection_carries_lab_in_every_harness_and_watches_its_files_last(self):
        root = self.project()
        lab_watch = session_lab.read(root.parent, now=NOW)[1]
        results = {harness: self.project_panel(harness, root.parent) for harness in ("claude", "codex", "opencode")}
        labs = {harness: result["panel"]["lab"] for harness, result in results.items()}
        for harness, result in results.items():
            self.assertEqual(result["watch"][-len(lab_watch):], lab_watch, harness)
            self.assertTrue(labs[harness]["key"].startswith("lab:"), harness)
        strip = lambda lab: [part["text"] for part in lab["strip"]]
        self.assertEqual({harness: strip(lab) for harness, lab in labs.items()},
                         {harness: ["!2", "LB"] for harness in labs})
        drop_clock = lambda lab: json.dumps({**lab, "clock": ""}, sort_keys=True, ensure_ascii=False)
        self.assertEqual(drop_clock(labs["codex"]), drop_clock(labs["claude"]))
        self.assertEqual(drop_clock(labs["opencode"]), drop_clock(labs["claude"]))
        # A project without experiments: no LAB in the projection at all.
        blank = self.make_root("blank", "c")
        self.assertIsNone(self.project_panel("claude", blank.parent)["panel"]["lab"])

    def test_projection_and_render_draw_the_same_section_in_every_harness(self):
        root = self.project()
        view = session_lab.view(session_lab.read(root.parent, now=NOW)[0], NOW)
        script = r"""
const assert=require('node:assert/strict'),v=require(process.argv[1]),lab=JSON.parse(process.argv[2]);
const draw=(harness,width,open,folded=false)=>v.render({width:folded?8:width,height:60,side:true,fill:true,folded,harness,state:'idle',configuration:{},
 model:{lab,repo:[{text:'proj'}]},expandedKeys:open?[lab.key]:[],questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join('')).filter(s=>!s.startsWith('┌'));
for(const width of [40,32])for(const open of [false,true])for(const folded of [false,true]){
 const claude=draw('claude',width,open,folded);
 for(const h of ['codex','opencode'])assert.deepEqual(draw(h,width,open,folded),claude,h);
 if(folded)assert(claude.includes('│ !2LB │'),claude.join('\n'));
 else{assert(claude.some(s=>/^│ 3 LAB /.test(s)),claude.join('\n'));
  // The failed condition is the one to look at, by its catalog name (cut with `…` only when narrow).
  assert(claude.some(s=>/짧은 발화( 제외|…)/.test(s)),claude.join('\n'));}
}
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs"),
                                 json.dumps(view, ensure_ascii=False)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
