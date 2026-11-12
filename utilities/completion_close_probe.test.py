#!/usr/bin/env python3
"""The completion-close probe renders each tree's own first-close prompts and measures only replies present."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import completion_close_probe as probe  # noqa: E402


class PromptsTest(unittest.TestCase):
    def test_this_tree_renders_24_prompts_with_the_short_directive_and_the_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            manifest = probe.cmd_prompts(ROOT, ROOT, out)
            prompts = sorted(out.glob("*/*.prompt.txt"))
            self.assertEqual(len(prompts), 24)
            self.assertEqual(len(manifest["prompts"]), 24)
            self.assertEqual({(p["harness"], p["event"], p["side"]) for p in manifest["prompts"]},
                             {(h, e, s) for h in probe.HARNESSES for e in probe.EVENTS for s in probe.SIDES})
            for entry in manifest["prompts"]:
                with self.subTest(**{k: entry[k] for k in ("harness", "event", "side")}):
                    text = (out / entry["prompt"]).read_text(encoding="utf-8")
                    self.assertEqual(entry["carrier_directive"], "current-short")
                    self.assertTrue(entry["summary_in_carrier"])
                    self.assertIn("[완료 요약] autopilot-code · route rt-probe", text)
                    self.assertIn(probe.FIXTURES[entry["event"]]["request"], text)
                    self.assertIn("### 0.5. Post-Execution Completion Report", text)
                    self.assertIn(probe.BOOTSTRAPS[entry["harness"]].rsplit("/", 1)[-1], text)
                    self.assertNotIn(probe.OLD_DIRECTIVE_MARK, text.split("=== The runtime now delivers")[1])
                    self.assertTrue(text.endswith(probe.TASK + "\n"))
            failure = (out / "claude" / "failure.after.prompt.txt").read_text(encoding="utf-8")
            self.assertIn("이 변경은 아직 쓰면 안 된다", failure)
            self.assertNotIn(tmp, failure)
            # A stand-in model command fills one harness; the rest stay unmeasured.
            probe.cmd_run(out, "claude", 'printf "%s 완료했습니다. 기록: final_report.md" "$PROBE_EVENT"', 60)
            results = probe.cmd_measure(out)
            measured = [r for r in results["rows"] if r["status"] == "measured"]
            self.assertEqual({r["harness"] for r in measured}, {"claude"})
            self.assertEqual(len(measured), 8)
            self.assertEqual(len(results["unmeasured"]), 16)
            self.assertTrue(all(r["facts"]["record_pointer"] for r in measured))

    def test_an_old_as_written_carrier_is_recognized(self):
        stub = ("No harvest command is required; the registered owner completed.\n"
                "First close: show this runtime summary to the person first, as written; then add what it "
                "does not show of changes/why, before/after, checks run/reused/skipped/unrun, "
                "commit/PR/deployment/serving, limits.\n[완료 요약] autopilot-code · route rt-x\n결과: 완료")
        self.assertEqual(probe.directive(stub), "old-as-written")
        self.assertEqual(probe.directive("First close: changes/why, before/after."), "other")


class MeasureTest(unittest.TestCase):
    def write(self, out: Path, replies: dict[tuple[str, str], str]):
        entries = [{"harness": "codex", "event": event, "side": side, "prompt": f"codex/{event}.{side}.prompt.txt",
                    "carrier_directive": "current-short" if side == "after" else "old-as-written",
                    "summary_chars": 300} for event in ("failure", "partial") for side in probe.SIDES]
        (out / "codex").mkdir(parents=True)
        (out / "manifest.json").write_text(json.dumps({
            "trees": {"before": {"path": "/b", "head": "b"}, "after": {"path": "/a", "head": "a"}},
            "heads": ["[Completion summary]", "[완료 요약]"], "prompts": entries}), encoding="utf-8")
        for (event, side), text in replies.items():
            probe.reply_path(out, "codex", event, side).write_text(text, encoding="utf-8")

    def test_a_missing_failure_fact_is_detected_and_an_absent_reply_is_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            before_text = "[완료 요약] autopilot-code · route rt-probepart\n결과: 완료\n\n덧붙임"
            self.write(out, {
                ("failure", "after"): "내보내기 이름 규칙을 바꿨지만 시험 3개가 실패했습니다.\n기록: <기록>/final_report.md\n",
                ("partial", "after"): "정렬을 추가했고 부하 시험은 못 했습니다.\n",
                ("partial", "before"): before_text + "\n",
            })
            results = probe.cmd_measure(out)
            rows = {(r["event"], r["side"]): r for r in results["rows"]}
            failure = rows[("failure", "after")]
            self.assertEqual(failure["status"], "measured")
            self.assertTrue(failure["facts"]["failure_stated"])
            self.assertFalse(failure["facts"]["not_usable"])      # negative control: the "not usable" fact is gone
            self.assertTrue(failure["facts"]["record_pointer"])
            self.assertFalse(failure["head_reproduced"])
            self.assertEqual(rows[("failure", "before")]["status"], "unmeasured")
            self.assertNotIn("facts", rows[("failure", "before")])
            self.assertEqual(results["unmeasured"], ["codex/failure.before"])
            self.assertTrue(rows[("partial", "after")]["facts"]["unrun_check"])
            self.assertFalse(rows[("partial", "after")]["facts"]["record_pointer"])
            before = rows[("partial", "before")]
            self.assertTrue(before["head_reproduced"])
            self.assertEqual((before["chars"], before["lines"]), (len(before_text), 4))
            markdown = (out / "results.md").read_text(encoding="utf-8")
            self.assertIn("not_usable=NO", markdown)
            self.assertIn("Unmeasured (1): codex/failure.before", markdown)
            self.assertEqual(json.loads((out / "results.json").read_text(encoding="utf-8"))["unmeasured"],
                             ["codex/failure.before"])
            fixed = "작업은 끝났지만 이 변경은 아직 쓰면 안 됩니다. 시험 3개가 실패했습니다."
            self.assertTrue(probe.measure_reply(fixed, "failure", [])["facts"]["not_usable"])


if __name__ == "__main__":
    unittest.main()
