"""Real PTY coverage for explorer tree drag and persisted ordering."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock as patching


TOOLS = Path(__file__).resolve().parents[2]
VIEWPORT = TOOLS / "fleet" / "tests" / "pty_repo_viewport.py"
REFERENCE_DIR = os.environ.get("FLEET_EXPLORER_REFERENCE_COPY")


class ExplorerDragPtyTests(unittest.TestCase):
    def run_viewer(self, evidence, env):
        result = subprocess.run(
            [sys.executable, str(VIEWPORT), "--explorer", "--evidence-dir", str(evidence)],
            cwd=str(TOOLS.parent), env=env, capture_output=True, text=True, timeout=45)
        report_path = evidence / "run.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        event_path = evidence / "events.jsonl"
        events = ([json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
                  if event_path.exists() else [])
        effects_path = evidence / "effects.jsonl"
        effects = ([json.loads(line) for line in effects_path.read_text(encoding="utf-8").splitlines()]
                   if effects_path.exists() else [])
        failed = [(item.get("label"), item.get("detail")) for item in report.get("assertions", [])
                  if not item.get("passed")]
        diagnostics = {"failed": failed,
                       "mouse_dispatch": [row for row in events if row.get("kind") == "explorer-mouse-dispatch"],
                       "recent_reductions": effects[-8:]}
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr + json.dumps(diagnostics))
        self.assertTrue(report["passed"], report["assertions"])
        return (json.loads((evidence / "state.json").read_text(encoding="utf-8")), report)

    def compare_reference(self, first_state, first_report, root):
        if not REFERENCE_DIR or not (Path(REFERENCE_DIR) / "view.py").is_file():
            self.skipTest("set FLEET_EXPLORER_REFERENCE_COPY to run the cycle reference comparison")
        reference_dir = Path(REFERENCE_DIR)
        reference_view_path = reference_dir / "view.py"
        spec = importlib.util.spec_from_file_location("fleet_cycle_reference_view", reference_view_path)
        viewer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(viewer)
        sys.path.insert(0, str(reference_dir))
        try:
            reference_data = importlib.import_module("mock13")
            defaults = {"repos": [project[0] for project in reference_data.PROJECTS],
                        "sessions": {project[0]: [session["tag"] for session in project[3]]
                                     for project in reference_data.PROJECTS},
                        "session_repo": {session["tag"]: project[0]
                                         for project in reference_data.PROJECTS for session in project[3]}}
            ref_repo_by_tag = defaults["session_repo"]
            fleet_repo_by_tag = first_state["explorer"]["order"]["repo_by_tag"]
            ref_to_fleet = {}
            fleet_to_ref = {}
            for tag, fleet_repo in fleet_repo_by_tag.items():
                ref_repo = ref_repo_by_tag.get(tag)
                if ref_repo:
                    ref_to_fleet[ref_repo] = fleet_repo
                    fleet_to_ref[fleet_repo] = ref_repo
            self.assertEqual(set(fleet_to_ref), set(first_state["explorer"]["order"]["repos"]))
            with patching.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "reference-state")}):
                ordering = viewer.load_order(defaults)
                preview_order = {"repo_order": list(ordering["repo_order"]),
                                 "session_order": {key: list(value) for key, value in
                                                   ordering["session_order"].items()}}
                actions = first_report["drag_actions"]
                repo_action = next(item for item in actions if item["kind"] == "repo")
                session_action = next(item for item in actions if item["kind"] == "session")
                viewer.reorder(ordering, {"kind": "repo", "id": fleet_to_ref[repo_action["source"]],
                                          "target": fleet_to_ref[repo_action["target"]]})
                viewer.reorder(ordering, {"kind": "session", "repo": ref_repo_by_tag[session_action["source"]],
                                          "id": session_action["source"], "target": session_action["target"]})
                self.assertEqual([fleet_to_ref[key] for key in first_state["explorer"]["order"]["repos"]],
                                 ordering["repo_order"])
                fleet_tags = [value.partition(":")[2]
                              for value in first_state["explorer"]["order"]["sessions"]]
                reference_tags = [tag for repo in ordering["repo_order"]
                                  for tag in ordering["session_order"][repo]]
                self.assertEqual(fleet_tags, reference_tags)

                preview = first_report["drag_previews"][0]
                source_ref = fleet_to_ref[repo_action["source"]]
                target_ref = fleet_to_ref[repo_action["target"]]
                ui = {"repo_order": preview_order["repo_order"],
                      "session_order": preview_order["session_order"],
                      "grabbed": "repo:" + source_ref,
                      "drop_before": "repo:" + target_ref}
                st = dict(scope=None, tab="OVERVIEW", uopen=0, gitv=0, design=4, gen=-1, ui=ui, note="")
                with patching.patch.object(viewer.os, "get_terminal_size", return_value=os.terminal_size((120, 40))):
                    result, _cols, _rows = viewer.draw(st, 1)
                self.assertEqual(result.returncode, 0, result.stderr)
                frame = json.loads(result.stdout)
                reference_cells = [viewer.cells(row) for row in frame["rows"]]
                ref_grab = next(hit[0] for hit in frame["hits"]
                                if hit[3][:2] == ["scope", "repo:" + source_ref])
                ref_drop = next(hit[0] for hit in frame["hits"]
                                if hit[3][:2] == ["scope", "repo:" + target_ref])
                fleet_grab = preview["grabbed"]
                fleet_drop = preview["drop"]
                self.assertEqual((fleet_grab["row"], fleet_drop["row"]), (ref_grab, ref_drop))
                rgb = reference_data.mock2.TINT[236]
                tint_sgr = "48;2;%d;%d;%d" % tuple(rgb)
                self.assertTrue(all(tint_sgr in cell[0] for cell in reference_cells[ref_grab][:32]),
                                reference_cells[ref_grab][:32])
                self.assertEqual([tint_sgr in cell[0] for cell in reference_cells[ref_grab][:32]],
                                 [cell[2] == 236 for cell in fleet_grab["cells"][:32]])
                self.assertEqual([cell[1] for cell in reference_cells[ref_grab][:4]],
                                 [cell[0] for cell in fleet_grab["cells"][:4]])
                self.assertEqual(reference_cells[ref_drop][1][1], "↳")
                self.assertEqual(fleet_drop["cells"][1][0], "↳")
                self.assertEqual([cell[1] for cell in reference_cells[ref_drop][:4]],
                                 [cell[0] for cell in fleet_drop["cells"][:4]])
        finally:
            sys.path.remove(str(reference_dir))

    def test_drag_reorders_in_pty_and_survives_a_new_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ)
            env["XDG_STATE_HOME"] = str(root / "state")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["TERM"] = "xterm-256color"
            env["FLEET_PTY_DRAG"] = "1"
            first, first_report = self.run_viewer(root / "first", env)
            saved = json.loads((root / "state" / "hearting" / "fleet2-order.json")
                               .read_text(encoding="utf-8"))
            self.assertEqual(saved.get("schema"), 1)
            self.assertTrue(all(str(key).startswith("sid:")
                                for rows in saved.get("sessions", {}).values() for key in rows))
            env.pop("FLEET_PTY_DRAG")
            second, _second_report = self.run_viewer(root / "second", env)
            self.assertEqual(second["explorer"]["order"]["repos"], saved["repos"])
            self.assertEqual(second["explorer"]["order"]["session_keys"], saved["sessions"])
            self.compare_reference(first, first_report, root)


if __name__ == "__main__":
    unittest.main()
