#!/usr/bin/env python3
"""Functional tests for the inactive proposal lifecycle."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools.improvement import proposals


class ProposalLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_STATE_HOME": str(self.root / "state"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "CLAUDE_CONFIG_DIR": str(self.root / "runtime" / "claude"),
                "CODEX_HOME": str(self.root / "runtime" / "codex"),
                "OPENCODE_CONFIG_DIR": str(self.root / "runtime" / "opencode"),
            },
            clear=False,
        )
        self.env.start()
        self.store = proposals.default_store()
        self.context = self._context("source-a", "runtime-a")
        self.context_b = self._context("source-b", "runtime-b")
        self.context_path = self._json("context.json", self.context)
        self.context_b_path = self._json("context-b.json", self.context_b)
        self.evidence = self.root / "incident.md"
        self.evidence.write_text("baseline failed\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def _context(self, source: str, runtime_version: str) -> dict:
        return {
            "source_revision": source,
            "source_dirty": False,
            "portable_fingerprint": f"portable-{source}",
            "runtimes": [
                {
                    "name": "codex",
                    "version": runtime_version,
                    "plugin": {
                        "name": "hearting-codex",
                        "version": "1.0.0",
                        "fingerprint": f"plugin-{source}",
                    },
                }
            ],
            "docs_fingerprint": f"docs-{runtime_version}",
            "fixture_fingerprints": [],
            "active_providers": {"skill:autopilot-code": "native-symlink"},
        }

    def _json(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _observe(
        self,
        incident_key: str | None = None,
        context_path: Path | None = None,
    ) -> dict:
        return proposals.observe(
            self.store,
            "Runtime update conflict",
            "A generated skill and plugin cache disagree.",
            context_path or self.context_path,
            self.evidence,
            incident_key=incident_key,
        )

    def _advance_to_adopted(self) -> dict:
        record = self._observe()
        proposal_id = record["id"]
        for state in ("reproduced", "proposed"):
            record = proposals.transition(
                self.store, proposal_id, state, self.evidence
            )
        record = proposals.transition(
            self.store,
            proposal_id,
            "reviewed",
            self.evidence,
            self.context_path,
            "human:owner",
            "session:review-1",
        )
        return proposals.transition(
            self.store,
            proposal_id,
            "adopted",
            self.evidence,
            self.context_path,
            "human:owner",
            "session:adopt-1",
        )

    def test_observe_uses_xdg_state_and_copies_evidence(self) -> None:
        record = self._observe()
        self.assertEqual(record["state"], "observed")
        self.assertTrue(str(self.store).startswith(str(self.root / "state")))
        stored = self.store / record["evidence"][0]["stored_path"]
        self.assertEqual(stored.read_text(encoding="utf-8"), "baseline failed\n")
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual(record["ingest_result"], "created")

    def test_named_oncall_collector_requires_incident_key(self) -> None:
        with self.assertRaises(proposals.ProposalError) as raised:
            proposals.observe(
                self.store,
                "Missing identity",
                "On-call collectors must choose semantic identity before ingestion.",
                self.context_path,
                self.evidence,
                actor="loop:oncall",
            )
        self.assertEqual(raised.exception.reason, "incident-key-required")

    def test_invalid_incident_keys_are_rejected(self) -> None:
        for value in ("   ", "line-one\nline-two", "x" * 513):
            with self.subTest(value=value[:20]):
                with self.assertRaises(proposals.ProposalError) as raised:
                    self._observe(value)
                self.assertEqual(raised.exception.reason, "invalid-incident-key")

    def test_exact_incident_key_appends_recurrence_without_state_change(self) -> None:
        key = "agent-setting:projection-drift:autopilot-code"
        first = self._observe(key)
        second = proposals.observe(
            self.store,
            "Same incident, newer context",
            "A repeated observation must not create another proposal.",
            self.context_b_path,
            self.evidence,
            actor="loop:oncall",
            incident_key=key,
        )
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["ingest_result"], "evidence-appended")
        self.assertEqual(second["state"], "observed")
        self.assertEqual(second["occurrences"], 2)
        self.assertEqual(second["base_fingerprint"], first["base_fingerprint"])
        self.assertNotEqual(
            second["latest_observation_fingerprint"], second["base_fingerprint"]
        )
        self.assertTrue(second["history"][-1]["context_changed"])

    def test_different_incident_keys_create_distinct_proposals(self) -> None:
        first = self._observe("agent-setting:incident:a")
        second = self._observe("agent-setting:incident:b")
        self.assertNotEqual(first["id"], second["id"])
        records = proposals.list_records(self.store)
        self.assertEqual(len(records), 2)
        self.assertEqual({item["occurrences"] for item in records}, {1})

    def test_concurrent_exact_key_observations_create_one_proposal(self) -> None:
        command = [
            sys.executable,
            str(Path(proposals.__file__).resolve()),
            "--store",
            str(self.store),
            "observe",
            "--actor",
            "loop:oncall",
            "--incident-key",
            "agent-setting:concurrent-incident",
            "--title",
            "Concurrent incident",
            "--summary",
            "The inbox lock must serialize exact-key observation.",
            "--context",
            str(self.context_path),
            "--evidence",
            str(self.evidence),
        ]
        workers = [
            subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = [worker.communicate(timeout=10) for worker in workers]
        for worker, (stdout, stderr) in zip(workers, results):
            self.assertEqual(worker.returncode, 0, stdout + stderr)
        records = proposals.list_records(self.store)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["occurrences"], 2)

    def test_ambiguous_exact_key_fails_closed(self) -> None:
        key = "agent-setting:ambiguous"
        self._observe(key)
        second = self._observe()
        root = proposals.validate_store_path(self.store)
        duplicate = proposals._load_record(root, second["id"])
        duplicate["incident_key"] = key
        proposals._atomic_json(proposals._record_path(root, second["id"]), duplicate)
        with self.assertRaises(proposals.ProposalError) as raised:
            self._observe(key)
        self.assertEqual(raised.exception.reason, "ambiguous-incident-key")

    def test_recurrence_does_not_reopen_adopted_state(self) -> None:
        key = "agent-setting:adopted-recurrence"
        record = self._observe(key)
        proposal_id = record["id"]
        for state in ("reproduced", "proposed"):
            proposals.transition(self.store, proposal_id, state, self.evidence)
        proposals.transition(
            self.store,
            proposal_id,
            "reviewed",
            self.evidence,
            self.context_path,
            "human:owner",
            "session:review-terminal",
        )
        proposals.transition(
            self.store,
            proposal_id,
            "adopted",
            self.evidence,
            self.context_path,
            "human:owner",
            "session:adopt-terminal",
        )
        recurrence = proposals.observe(
            self.store,
            "Regression after adoption",
            "Evidence may accumulate but the decision is not reopened.",
            self.context_b_path,
            self.evidence,
            actor="loop:oncall",
            incident_key=key,
        )
        self.assertEqual(recurrence["state"], "adopted")
        self.assertEqual(recurrence["history"][-1]["from"], "adopted")
        self.assertEqual(recurrence["history"][-1]["to"], "adopted")

    def test_stale_recurrence_rebases_only_after_bound_reproduction(self) -> None:
        key = "agent-setting:runtime-reconciliation"
        first = self._observe(key)
        recurrence = proposals.observe(
            self.store,
            "Runtime changed",
            "A fresh reproduction is required before review.",
            self.context_b_path,
            self.evidence,
            actor="loop:oncall",
            incident_key=key,
        )
        self.assertEqual(recurrence["base_fingerprint"], first["base_fingerprint"])
        self.assertFalse(
            proposals.check(self.store, first["id"], self.context_b_path)["fresh"]
        )
        with self.assertRaisesRegex(proposals.ProposalError, "current context"):
            proposals.transition(
                self.store,
                first["id"],
                "reproduced",
                self.evidence,
                actor="loop:oncall",
            )
        reproduced = proposals.transition(
            self.store,
            first["id"],
            "reproduced",
            self.evidence,
            context_path=self.context_b_path,
            actor="loop:oncall",
        )
        self.assertTrue(reproduced["history"][-1]["base_rebased"])
        self.assertTrue(
            proposals.check(self.store, first["id"], self.context_b_path)["fresh"]
        )
        proposals.transition(
            self.store,
            first["id"],
            "proposed",
            self.evidence,
            actor="loop:oncall",
        )
        reviewed = proposals.transition(
            self.store,
            first["id"],
            "reviewed",
            self.evidence,
            self.context_b_path,
            "human:owner",
            "session:review-after-reproduction",
        )
        self.assertEqual(reviewed["state"], "reviewed")

    def test_manual_reproduction_keeps_existing_base_semantics(self) -> None:
        record = self._observe()
        reproduced = proposals.transition(
            self.store,
            record["id"],
            "reproduced",
            self.evidence,
            context_path=self.context_b_path,
            actor="operator",
        )
        self.assertEqual(reproduced["base_fingerprint"], record["base_fingerprint"])
        self.assertNotIn("base_rebased", reproduced["history"][-1])

    def test_named_collector_cannot_cross_human_owned_state_boundary(self) -> None:
        record = self._observe("agent-setting:human-owned")
        proposal_id = record["id"]
        proposals.transition(
            self.store,
            proposal_id,
            "reproduced",
            self.evidence,
            self.context_path,
            "loop:oncall",
        )
        proposals.transition(
            self.store,
            proposal_id,
            "proposed",
            self.evidence,
            actor="loop:oncall",
        )
        proposals.transition(
            self.store,
            proposal_id,
            "reviewed",
            self.evidence,
            self.context_path,
            "human:owner",
            "session:review-human-owned",
        )
        proposals.transition(
            self.store,
            proposal_id,
            "deferred",
            self.evidence,
            actor="operator",
        )
        with self.assertRaises(proposals.ProposalError) as raised:
            proposals.transition(
                self.store,
                proposal_id,
                "reproduced",
                self.evidence,
                self.context_path,
                "loop:oncall",
            )
        self.assertEqual(raised.exception.reason, "collector-state-forbidden")

    def test_named_collector_cannot_use_non_promotion_transitions(self) -> None:
        record = self._observe("agent-setting:no-auto-defer")
        with self.assertRaises(proposals.ProposalError) as raised:
            proposals.transition(
                self.store,
                record["id"],
                "deferred",
                self.evidence,
                actor="loop:oncall",
            )
        self.assertEqual(raised.exception.reason, "collector-state-forbidden")

    def test_recurrence_fails_before_copy_when_record_is_full(self) -> None:
        key = "agent-setting:bounded-recurrence"
        record = self._observe(key)
        root = proposals.validate_store_path(self.store)
        stored = proposals._load_record(root, record["id"])
        stored["evidence"] = [{} for _ in range(proposals.MAX_EVIDENCE_ITEMS)]
        proposals._atomic_json(proposals._record_path(root, record["id"]), stored)
        with self.assertRaises(proposals.ProposalError) as raised:
            self._observe(key)
        self.assertEqual(raised.exception.reason, "proposal-full")

    def test_read_only_list_does_not_create_store(self) -> None:
        other = self.root / "other-state" / "improvement"
        self.assertEqual(proposals.list_records(other), [])
        self.assertFalse(other.exists())

    def test_transition_requires_order_and_human_approval(self) -> None:
        record = self._observe()
        proposal_id = record["id"]
        with self.assertRaisesRegex(proposals.ProposalError, "not allowed"):
            proposals.transition(
                self.store,
                proposal_id,
                "adopted",
                self.evidence,
                self.context_path,
                "human:owner",
                "session:bad-order",
            )
        proposals.transition(self.store, proposal_id, "reproduced", self.evidence)
        proposals.transition(self.store, proposal_id, "proposed", self.evidence)
        with self.assertRaisesRegex(proposals.ProposalError, "human"):
            proposals.transition(
                self.store,
                proposal_id,
                "reviewed",
                self.evidence,
                self.context_path,
            )

    def test_stale_context_blocks_review(self) -> None:
        record = self._observe()
        proposal_id = record["id"]
        proposals.transition(self.store, proposal_id, "reproduced", self.evidence)
        proposals.transition(self.store, proposal_id, "proposed", self.evidence)
        with self.assertRaises(proposals.ProposalError) as raised:
            proposals.transition(
                self.store,
                proposal_id,
                "reviewed",
                self.evidence,
                self.context_b_path,
                "human:owner",
                "session:review-stale",
            )
        self.assertEqual(raised.exception.reason, "stale-context")
        result = proposals.check(self.store, proposal_id, self.context_b_path)
        self.assertFalse(result["fresh"])

    def test_adopted_proposal_has_version_bound_realization(self) -> None:
        record = self._advance_to_adopted()
        proposal_id = record["id"]
        record = proposals.realization(
            self.store,
            proposal_id,
            "codex",
            "active",
            "runtime-a",
            "hearting-codex",
            "1.0.0",
            self.context_path,
            self.evidence,
            "human:owner",
            "release:1.0.0",
        )
        self.assertEqual(record["realizations"]["codex"]["state"], "active")
        self.assertTrue(
            proposals.check(self.store, proposal_id, self.context_path, "codex")["fresh"]
        )
        self.assertFalse(
            proposals.check(self.store, proposal_id, self.context_b_path, "codex")["fresh"]
        )

    def test_runtime_update_forces_revalidation_before_reactivation(self) -> None:
        record = self._advance_to_adopted()
        proposal_id = record["id"]
        proposals.realization(
            self.store,
            proposal_id,
            "codex",
            "active",
            "runtime-a",
            "hearting-codex",
            "1.0.0",
            self.context_path,
            self.evidence,
            "human:owner",
            "release:1.0.0",
        )
        with self.assertRaisesRegex(proposals.ProposalError, "has not changed"):
            proposals.realization(
                self.store,
                proposal_id,
                "codex",
                "needs-revalidation",
                "runtime-a",
                "hearting-codex",
                "1.0.0",
                self.context_path,
                self.evidence,
            )
        proposals.realization(
            self.store,
            proposal_id,
            "codex",
            "needs-revalidation",
            "runtime-b",
            "hearting-codex",
            "1.1.0",
            self.context_b_path,
            self.evidence,
        )
        with self.assertRaisesRegex(proposals.ProposalError, "human"):
            proposals.realization(
                self.store,
                proposal_id,
                "codex",
                "active",
                "runtime-b",
                "hearting-codex",
                "1.1.0",
                self.context_b_path,
                self.evidence,
            )
        record = proposals.realization(
            self.store,
            proposal_id,
            "codex",
            "active",
            "runtime-b",
            "hearting-codex",
            "1.1.0",
            self.context_b_path,
            self.evidence,
            "human:owner",
            "release:1.1.0",
        )
        self.assertEqual(record["realizations"]["codex"]["runtime_version"], "runtime-b")

    def test_realization_cannot_activate_before_adoption(self) -> None:
        proposal_id = self._observe()["id"]
        with self.assertRaises(proposals.ProposalError) as raised:
            proposals.realization(
                self.store,
                proposal_id,
                "codex",
                "active",
                "runtime-a",
                "hearting-codex",
                "1.0.0",
                self.context_path,
                self.evidence,
                "human:owner",
                "release:early",
            )
        self.assertEqual(raised.exception.reason, "proposal-not-adopted")

    def test_protected_store_paths_are_rejected(self) -> None:
        repo_store = Path(proposals.__file__).resolve().parents[2] / ".proposal-inbox"
        with self.assertRaises(proposals.ProposalError) as repo_error:
            proposals.validate_store_path(repo_store)
        self.assertEqual(repo_error.exception.reason, "protected-store")
        runtime_store = Path(os.environ["CODEX_HOME"]) / "proposals"
        with self.assertRaises(proposals.ProposalError) as runtime_error:
            proposals.validate_store_path(runtime_store)
        self.assertEqual(runtime_error.exception.reason, "protected-store")

    def test_symlink_store_and_oversized_evidence_are_rejected(self) -> None:
        target = self.root / "target"
        target.mkdir()
        link = self.root / "store-link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(proposals.ProposalError) as link_error:
            proposals.validate_store_path(link)
        self.assertEqual(link_error.exception.reason, "unsafe-store")

        oversized = self.root / "oversized.bin"
        with oversized.open("wb") as handle:
            handle.truncate(proposals.MAX_EVIDENCE_BYTES + 1)
        with self.assertRaises(proposals.ProposalError) as size_error:
            proposals.observe(
                self.store,
                "Oversized evidence",
                "The evidence copy must be bounded.",
                self.context_path,
                oversized,
            )
        self.assertEqual(size_error.exception.reason, "file-too-large")

    def test_context_schema_is_required(self) -> None:
        bad = self._json("bad.json", {"source_revision": "only-one-field"})
        with self.assertRaises(proposals.ProposalError) as raised:
            proposals.observe(
                self.store,
                "Bad context",
                "Incomplete fingerprints must not enter the inbox.",
                bad,
                self.evidence,
            )
        self.assertEqual(raised.exception.reason, "invalid-context")

    def test_cli_check_returns_stale_exit_code(self) -> None:
        record = self._observe()
        with redirect_stdout(io.StringIO()):
            code = proposals.main(
                [
                    "--store",
                    str(self.store),
                    "check",
                    record["id"],
                    "--context",
                    str(self.context_b_path),
                ]
            )
        self.assertEqual(code, 4)


class OncallPromotionContractTest(unittest.TestCase):
    def test_oncall_requires_full_read_live_evidence_and_human_ceiling(self) -> None:
        root = Path(__file__).resolve().parents[2]
        guide = (root / "loops" / "oncall.md").read_text(encoding="utf-8")
        self.assertIn("mem.py log --limit 100 --json", guide)
        self.assertIn("mem.py show <id>", guide)
        self.assertIn("Memory alone is never evidence", guide)
        self.assertIn("--actor loop:oncall --incident-key <key>", guide)
        self.assertRegex(guide, r"transition to\s+`proposed`\.\s+Stop there\.")
        self.assertIn("report the unchanged state", guide)
        self.assertIn("never supply `human:*` actors", guide)


if __name__ == "__main__":
    unittest.main()
