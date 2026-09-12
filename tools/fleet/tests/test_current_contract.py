"""Fleet consumes current completion obligations without changing their evidence."""
import base64
import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.fleet import render, route
from tools.fleet.model import DispatchJob
from tools.fleet.collectors import dispatch
from tools.fleet.tests.test_v20_dispatch_contract import row
from dispatch_attempt_policy import terminal_conflict_pending


class CompletionDisplayTest(unittest.TestCase):
    def evidence(self, *metadata, harness="codex"):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.log"
            rows = []
            for i, meta in enumerate(metadata):
                value = row(f"2026-09-13T00:00:0{i}+00:00", "done", "review", "att-review")
                value = value.replace("harness=codex", f"harness={harness}").rstrip()
                value += "," + ",".join(f"{key}={val}" for key, val in meta.items()) + "\n"
                rows.append(value)
            path.write_text("".join(rows))
            evidence, _ = dispatch._scan_registry_evidence([str(path)])
            self.assertEqual(path.read_text(), "".join(rows))
        return evidence["rt-v20"]

    def state(self, evidence):
        return route._node_state("one-shot", [], evidence, 1, completion_marked=True)

    def test_registry_conflict_outlives_marker_on_every_harness(self):
        meta = {"note": "completed-marker", "failure_class": "pass", "terminal_conflict": "1"}
        self.assertTrue(terminal_conflict_pending(meta))
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                ev = self.evidence(meta, harness=harness)
                st = self.state(ev)
                self.assertEqual(st["state"], "attention")
                self.assertEqual(st["note"], "terminal-evidence-conflict")
                self.assertEqual(ev["one-shot"]["note"], "completed-marker")
                self.assertEqual(ev["one-shot"]["attempt_history"][0]["status"], "done")
                record = {"route_hash": "sha256:test", "nodes": [{"id": "one-shot", "depends_on": []}]}
                view = route._record_view(record, "rt-v20", [], ev, 1,
                                          gate_marks_for_route={"one-shot": True})
                self.assertTrue(view["nodes"][0]["gate_passed"])
                self.assertEqual(view["nodes"][0]["state"], "attention")
                label, color, mark = render._route_node_text(view["nodes"][0])
                self.assertIn("확인 필요", label)
                self.assertEqual(color, "lvl_y")
                self.assertTrue(mark)
                self.assertEqual(view["progress"]["done"], 0)

    def test_reviewed_conflict_restores_completion_on_the_same_attempt(self):
        pending = {"note": "completed-marker", "terminal_conflict": "1"}
        reviewed = {"note": "completed-marker", "terminal_conflicts_b64":
                    base64.b64encode(json.dumps({"observation": {"review_sha256": "proof"}}).encode()).decode()}
        self.assertFalse(terminal_conflict_pending(reviewed))
        ev = self.evidence(pending, reviewed)
        self.assertEqual(self.state(ev)["state"], "done")
        self.assertIsNone(ev["one-shot"]["attention_reason"])

    def test_unreadable_conflict_is_not_clean_completion(self):
        ev = self.evidence({"note": "completed-marker", "terminal_conflicts_b64": "invalid!"})
        self.assertEqual(self.state(ev)["state"], "attention")
        self.assertEqual(self.state(ev)["note"], "terminal-evidence-unreadable")

    def test_job_projection_and_fresh_resolution_use_the_same_current_attempt(self):
        job = DispatchJob(key="code", route_node="one-shot", attempt_id="att-review", liveness="dead",
                          attention_reason="terminal-evidence-conflict")
        self.assertEqual(route._node_state("one-shot", [job], {}, 1, completion_marked=True)["state"],
                         "attention")
        resolved = self.evidence({"note": "completed-marker"})
        # The newer registry snapshot cleared the obligation; an older collected
        # job object must not resurrect it.
        self.assertEqual(route._node_state("one-shot", [job], resolved, 1, completion_marked=True)["state"],
                         "done")

    def test_attention_is_visible_without_a_false_success_or_failure_glyph(self):
        for width in (30, 60, 100, 168):
            segs = render._route_stage_segs([("review", "attention"), ("report", "pending")],
                                            working=False, max_width=width)
            text = "".join(s for s, _ in segs)
            self.assertIn("review !", text)
            self.assertNotIn("✓", text)
            self.assertNotIn("✕", text)
            self.assertLessEqual(render._dw(text), width)


class ParallelCompletionDisplayTest(unittest.TestCase):
    def group(self, states, field="parallel_group"):
        return render._collapse_parallel_nodes([
            {"id": f"leg{i}", "state": state, field: "review", "depends_on": [],
             "gate_passed": state == "done"}
            for i, state in enumerate(states)
        ])[0]

    def test_complete_only_when_every_member_completed(self):
        for count in (2, 3, 4):
            for states in itertools.product(("done", "pending"), repeat=count):
                for field in ("parallel_group", "replica_group"):
                    with self.subTest(states=states, field=field):
                        node = self.group(states, field)
                        self.assertEqual(node["state"], "done" if all(s == "done" for s in states) else "pending")
                        self.assertEqual(bool(node["gate_passed"]), all(s == "done" for s in states))

    def test_outstanding_work_and_inspection_survive_grouping(self):
        for state in ("active", "attention", "failed", "recovering", "reconciling"):
            for states in (("done", state), (state, "done")):
                with self.subTest(states=states):
                    self.assertEqual(self.group(states)["state"], state)


if __name__ == "__main__":
    unittest.main()
