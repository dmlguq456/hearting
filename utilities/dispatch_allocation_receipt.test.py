#!/usr/bin/env python3
"""Allocation receipt ledger: durable twin of the stdout allocation verdict."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "utilities" / "dispatch_allocation_receipt.py"


def _load():
    spec = importlib.util.spec_from_file_location("dispatch_allocation_receipt", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AllocationReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {
            "AGENT_HOME": str(ROOT),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.receipt = _load()

    def _policy(self, strategy="capacity-aware"):
        return {
            "strategy": strategy, "window": 30, "usage_gate_used_percent": 85,
            "depth_affinity": {"owner": "claude", "worker": "codex"},
            "depth_affinity_weight": 0.65, "usage_headroom_exponent": 2,
            "harness_order": ["claude", "codex"],
        }

    def test_record_writes_a_row_keyed_by_attempt_with_policy_and_inert_keys(self):
        result = self.receipt.record_allocation_receipt(
            route_id="rt-test", route_node="execute", route_hash="sha256:x",
            writer="stage-dispatch-fallback.py", action="start",
            attempt_id="att-1", slug="w-execute", unit="dev/backend",
            child_harness="claude", fallback_hop="same-harness-headless",
            allocation=self._policy(), preferred="codex",
            rank=["claude", "codex"], capacity={"claude": 79.0, "codex": 74.0, "opencode": None},
            counts={"claude": 3, "codex": 4}, states={"claude": "ok", "codex": "ok"},
            quality_band="primary", relief_promoted=False,
            parent_cross="not-applicable", sole_gate="ok", affinity="diverse",
            owner_family="claude", jobs=self.jobs,
        )
        self.assertIsNotNone(result)
        path = Path(result["path"])
        self.assertEqual(path, Path(self.tmp.name) / "allocation" / "rt-test.jsonl")
        self.assertTrue(result["event_id"].startswith("al-"))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["attempt_id"], "att-1")
        self.assertEqual(row["child_harness"], "claude")
        self.assertEqual(row["preferred"], "codex")
        self.assertIs(row["preferred_honored"], False)
        self.assertEqual(row["strategy"], "capacity-aware")
        self.assertEqual(row["depth_affinity"], {"owner": "claude", "worker": "codex"})
        self.assertEqual(row["rank"], ["claude", "codex"])
        self.assertEqual(row["capacity"]["codex"], 74.0)
        self.assertNotIn("capacity_raw_headroom", row)
        self.assertEqual(row["counts"], {"claude": 3, "codex": 4, "opencode": 0})
        # The receipt names the keys the sealed strategy never read: this is
        # the evidence that was missing when depth-affinity looked "applied".
        self.assertEqual(
            sorted(row["inert_keys"]),
            ["depth_affinity_weight", "usage_gate_used_percent", "usage_headroom_exponent"],
        )
        self.assertEqual(row["event_id"], result["event_id"])

    def test_balanced_policy_has_no_inert_keys_and_honors_preferred(self):
        result = self.receipt.record_allocation_receipt(
            route_id="rt-bal", route_node="plan-check", writer="dispatch-batch.py",
            child_harness="codex", allocation=self._policy("balanced"), preferred="codex",
            parallel_group="pg-1", parallel_leg_index=1, parallel_leg_count=2, jobs=self.jobs,
        )
        row = json.loads(Path(result["path"]).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["inert_keys"], {})
        self.assertIs(row["preferred_honored"], True)
        self.assertEqual(row["parallel_leg_count"], 2)

    def test_receipt_carries_a_copy_of_harness_weights_or_none(self):
        weighted = {**self._policy("balanced"), "harness_weights": {"opencode": 0.3}}
        result = self.receipt.record_allocation_receipt(
            route_id="rt-weights", route_node="plan-check", writer="dispatch-batch.py",
            child_harness="codex", allocation=weighted, jobs=self.jobs,
        )
        row = json.loads(Path(result["path"]).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["harness_weights"], {"opencode": 0.3})
        self.assertEqual(row["inert_keys"], {})
        plain = self.receipt.record_allocation_receipt(
            route_id="rt-noweights", route_node="plan-check", writer="dispatch-batch.py",
            child_harness="codex", allocation=self._policy("balanced"), jobs=self.jobs,
        )
        row = json.loads(Path(plain["path"]).read_text(encoding="utf-8").splitlines()[0])
        self.assertIsNone(row["harness_weights"])

    def test_the_row_and_its_line_carry_the_field_and_the_weights_the_choice_used(self):
        sealed = {**self._policy("balanced"), "harness_weights": {"opencode": 0.4, "codex": 0.7},
                  "field_weights": {"claude": 0.95, "codex": 0.85, "opencode": 1.0}}
        result = self.receipt.record_allocation_receipt(
            route_id="rt-field", route_node="execute", writer="stage-dispatch-fallback.py",
            child_harness="claude", allocation=sealed, field="code", jobs=self.jobs,
        )
        row = json.loads(Path(result["path"]).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["field"], "code")
        self.assertEqual(row["field_weights"], {"claude": 0.95, "codex": 0.85, "opencode": 1.0})
        self.assertEqual(row["inert_keys"], {})
        line = self.receipt.format_row(row)
        self.assertTrue(line.endswith("field=code w=claude:0.95|codex:0.595|opencode:0.4"), line)
        # a field with no row of its own still names the field and the user's flat weights
        neutral = self.receipt.record_allocation_receipt(
            route_id="rt-neutral", route_node="execute", writer="stage-dispatch-fallback.py",
            child_harness="claude", field="admin",
            allocation={**self._policy("balanced"), "harness_weights": {"opencode": 0.4}}, jobs=self.jobs,
        )
        row = json.loads(Path(neutral["path"]).read_text(encoding="utf-8").splitlines()[0])
        self.assertIsNone(row["field_weights"])
        self.assertTrue(self.receipt.format_row(row).endswith("field=admin w=opencode:0.4"))
        # no field (frame, an older route): `field=-` and the row still reads
        bare = self.receipt.record_allocation_receipt(
            route_id="rt-bare", route_node="execute", writer="stage-dispatch-fallback.py",
            child_harness="claude", allocation=self._policy("balanced"), jobs=self.jobs,
        )
        row = json.loads(Path(bare["path"]).read_text(encoding="utf-8").splitlines()[0])
        self.assertIsNone(row["field"])
        self.assertTrue(self.receipt.format_row(row).endswith(" field=-"))
        old_row = {k: v for k, v in row.items() if k not in ("field", "field_weights")}
        self.assertTrue(self.receipt.format_row(old_row).endswith(" field=-"))
        # a continuation route keeps the sealed row but not the field name: the line still says
        # what the choice multiplied (`field=- w=...`), not "no row"
        kept = {**row, "field_weights": {"claude": 0.95, "codex": 0.85, "opencode": 1.0},
                "harness_weights": {"opencode": 0.4, "codex": 0.7}}
        self.assertTrue(self.receipt.format_row(kept).endswith(
            " field=- w=claude:0.95|codex:0.595|opencode:0.4"), self.receipt.format_row(kept))

    def test_an_observed_affinity_names_its_source_and_a_sealed_one_adds_nothing(self):
        observed = self.receipt.record_allocation_receipt(
            route_id="rt-handover", route_node="test", writer="stage-dispatch-fallback.py",
            child_harness="claude", allocation=self._policy("balanced"), affinity="codex",
            handed_over_affinity="claude", affinity_source="handed-over-checks", jobs=self.jobs,
        )
        row = json.loads(Path(observed["path"]).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((row["affinity"], row["handed_over_affinity"], row["affinity_source"]),
                         ("codex", "claude", "handed-over-checks"))
        sealed = self.receipt.record_allocation_receipt(
            route_id="rt-sealed", route_node="test", writer="stage-dispatch-fallback.py",
            child_harness="codex", allocation=self._policy("balanced"), affinity="codex", jobs=self.jobs,
        )
        row = json.loads(Path(sealed["path"]).read_text(encoding="utf-8").splitlines()[0])
        self.assertNotIn("affinity_source", row)
        self.assertNotIn("handed_over_affinity", row)

    def test_unknown_writer_or_harness_is_refused_without_raising(self):
        self.assertIsNone(self.receipt.record_allocation_receipt(
            route_id="rt-x", route_node="n", writer="someone-else.py",
            child_harness="claude", jobs=self.jobs,
        ))
        self.assertIsNone(self.receipt.record_allocation_receipt(
            route_id="rt-x", route_node="n", writer="dispatch-batch.py",
            child_harness="gemini", jobs=self.jobs,
        ))
        self.assertFalse((Path(self.tmp.name) / "allocation").exists())

    def test_read_rows_filters_by_route_and_since_and_summary_counts(self):
        for harness, node in (("claude", "execute"), ("codex", "impl-review"), ("codex", "test")):
            self.receipt.record_allocation_receipt(
                route_id="rt-a", route_node=node, writer="stage-dispatch-fallback.py",
                child_harness=harness, unit=f"u/{node}", allocation=self._policy("balanced"),
                preferred="codex", jobs=self.jobs,
            )
        self.receipt.record_allocation_receipt(
            route_id="rt-b", route_node="execute", writer="stage-dispatch-fallback.py",
            child_harness="claude", allocation=self._policy(), preferred="codex", jobs=self.jobs,
        )
        rows = self.receipt.read_rows(jobs=self.jobs)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(self.receipt.read_rows(route_id="rt-a", jobs=self.jobs)), 3)
        self.assertEqual(self.receipt.read_rows(since=rows[-1]["ts"] + 1, jobs=self.jobs), [])
        summary = self.receipt.summarize(rows)
        self.assertEqual(summary["rows"], 4)
        self.assertEqual(summary["by_harness"], {"claude": 2, "codex": 2})
        self.assertEqual(summary["by_strategy"], {"balanced": 3, "capacity-aware": 1})
        self.assertEqual(summary["preferred_honored"], {"yes": 2, "no": 2, "n/a": 0})
        self.assertEqual(summary["rows_with_inert_keys"], 1)
        self.assertEqual(summary["by_unit"]["u/execute"], {"claude": 1})

    def test_receipt_preserves_the_adjusted_score_and_adds_only_different_raw_quota(self):
        spec = importlib.util.spec_from_file_location("capacity", MODULE.with_name("harness-capacity.py"))
        capacity = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(capacity)
        codex = capacity._headroom([(90, 1600.0, 18000)], now=1000.0)
        self.receipt.record_allocation_receipt(
            route_id="rt-timed", route_node="execute", writer="stage-dispatch-fallback.py",
            child_harness="claude", capacity={"claude": 79.0, "codex": codex, "opencode": None},
            jobs=self.jobs,
        )
        row, = self.receipt.read_rows(route_id="rt-timed", jobs=self.jobs)
        self.assertEqual(row["capacity"], {"claude": 79.0, "codex": float(codex), "opencode": None})
        self.assertEqual(row.pop("capacity_raw_headroom"), {"codex": 10.0})
        raw_row = {**row, "capacity_raw_headroom": {"codex": 10.0}}
        self.assertEqual(self.receipt.format_row(raw_row).splitlines(), [
            self.receipt.format_row(row) + " raw=codex:10.0",
        ])

    def test_cli_list_and_summary_are_one_line_per_answer(self):
        self.receipt.record_allocation_receipt(
            route_id="rt-cli", route_node="execute", writer="stage-dispatch-fallback.py",
            child_harness="claude", unit="dev/backend", allocation=self._policy(),
            preferred="codex", rank=["claude", "codex"],
            capacity={"claude": 79.0, "codex": 74.0}, counts={"claude": 1, "codex": 2},
            attempt_id="att-cli", jobs=self.jobs,
        )
        env = {**os.environ, "AGENT_DISPATCH_JOBS": str(self.jobs)}
        listed = subprocess.run(
            [sys.executable, str(MODULE), "list", "--jobs", str(self.jobs)],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        line = listed.stdout.strip()
        self.assertEqual(line.count("\n"), 0)
        for token in ("rt-cli execute", "unit=dev/backend", "child=claude", "preferred=codex",
                      "honored=0", "rank=claude>codex", "headroom=claude:79.0|codex:74.0",
                      "inert=depth_affinity_weight,usage_gate_used_percent,usage_headroom_exponent",
                      "attempt=att-cli"):
            self.assertIn(token, line)
        summary = subprocess.run(
            [sys.executable, str(MODULE), "summary", "--jobs", str(self.jobs), "--json"],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(summary.returncode, 0, summary.stderr)
        payload = json.loads(summary.stdout)
        self.assertEqual(payload["by_harness"], {"claude": 1})
        self.assertEqual(payload["rows_with_inert_keys"], 1)
        path = subprocess.run(
            [sys.executable, str(MODULE), "path", "--jobs", str(self.jobs)],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(path.stdout.strip(), str(Path(self.tmp.name) / "allocation"))


if __name__ == "__main__":
    unittest.main()
