#!/usr/bin/env python3
"""Raw registry reuse preserves current owner identity and fresh proof inputs."""
import base64
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import dispatch_registry_cache as cache
import dispatch_terminal_commit as terminal
import owner_route_binding

route_module = owner_route_binding.ROUTE


class OwnerRegistryInputsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.jobs = self.root / "jobs.log"
        self.report = self.root / "report.md"
        self.report.write_text("exact report\n")
        self.node = {"id": "one-shot", "terminal_gate": "code-report"}
        self.route = {"artifact_root": str(self.root), "cwd": str(self.root),
                      "nodes": [self.node]}
        digest = terminal.route_identity.route_hash(self.route)
        self.route.update(route_hash=digest,
                          route_id=terminal.route_identity.route_id_from_hash(digest))
        self.request = SimpleNamespace(jobs=self.jobs, artifact_root=self.root,
                                       owner_attempt_id="att-owner",
                                       route_file=self.root / "route.json")
        self.binding = SimpleNamespace(route_id=self.route["route_id"],
                                       route_hash=digest)
        self.validate = self.enterContext(mock.patch.object(
            terminal, "validate_owner_route", return_value=self.binding))
        self.enterContext(mock.patch.object(
            owner_route_binding, "resolve_owner_route_lifecycle",
            return_value=(self.binding, "current")))
        self.process = self.enterContext(mock.patch.object(
            route_module, "attempt_process_quiescence",
            return_value=SimpleNamespace(state="quiescent")))
        self.enterContext(mock.patch.object(
            route_module, "owner_terminal_prerequisites", return_value={}))
        self.enterContext(mock.patch.object(route_module, "owner_operation_fence"))
        encoded = base64.urlsafe_b64encode(str(self.report).encode()).decode().rstrip("=")
        self.enterContext(mock.patch.object(
            route_module, "inspect_terminal_attempt",
            return_value={"state": "valid", "verdict": "PASS",
                          "artifact_state": "readable", "artifact_path_b64": encoded}))
        cache.clear_cache()
        cache.reset_metrics()
        self.addCleanup(cache.clear_cache)
        self.jobs.write_text(self.row())

    def row(self, attempt="att-owner", status="done", **changes):
        meta = {"attempt_id": attempt, "worker_type": "owner",
                "dispatch_depth": "1", "registered_worker": "1",
                "harness": "claude", "note": "completed-supervisor",
                "failure_class": "pass", "route_id": self.route["route_id"],
                "route_hash": self.route["route_hash"], "route_node": self.node["id"]}
        meta.update(changes)
        return ("now\t" + status + "\t/repo\t/repo\towner\t"
                + ",".join(key + "=" + value for key, value in meta.items()) + "\n")

    def observe(self):
        return route_module._owner_terminal_observation(
            self.route, self.node, jobs=self.jobs)

    def test_repeated_identity_reads_share_parsing_but_validate_current_bytes_and_proof(self):
        unrelated = self.row(attempt="att-unrelated", route_id="rt-other",
                             route_hash="sha256:" + "f" * 64)
        self.jobs.write_text(unrelated * 300 + self.row())
        direct_bytes = [0]
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            data = original_read(path, *args, **kwargs)
            if path == self.jobs:
                direct_bytes[0] += len(data.encode("utf-8"))
            return data

        with mock.patch.object(Path, "read_text", read):
            for _ in range(8):
                terminal.verify_request_identity(self.request, self.route)
                result = self.observe()
                self.assertTrue(result["passed"], result)
        metrics = cache.metrics_snapshot()
        self.assertEqual(direct_bytes[0], 0)
        # Both readers share one cold content snapshot and parse the 301 rows once.
        self.assertEqual(metrics["content_reads"], 1)
        self.assertEqual(metrics["bytes_read"], self.jobs.stat().st_size)
        self.assertEqual(metrics["full_metadata_parses"], 1)
        self.assertEqual(metrics["metadata_rows_parsed"], 301)
        # Cached raw rows cannot replace a changed non-registry proof.
        self.validate.side_effect = terminal.TerminalCommitError(
            "route-identity-unverified", "fixture-changed-binding")
        with self.assertRaises(terminal.TerminalCommitError):
            terminal.verify_request_identity(self.request, self.route)
        self.process.return_value = SimpleNamespace(state="alive")
        self.assertEqual(self.observe()["reason"], "owner-not-quiescent")

    def test_append_and_inode_replacement_observe_the_latest_owner(self):
        terminal.verify_request_identity(self.request, self.route)
        self.assertTrue(self.observe()["passed"])
        with self.jobs.open("a") as stream:
            stream.write(self.row(attempt="att-new", status="open"))
        with self.assertRaisesRegex(terminal.TerminalCommitError, "owner-not-current"):
            terminal.verify_request_identity(self.request, self.route)
        self.assertEqual(self.observe()["reason"], "owner-terminal-not-pass")
        replacement = self.root / "replacement"
        replacement.write_text(self.row(attempt="att-new"))
        replacement.replace(self.jobs)
        self.assertEqual(self.observe()["attempt_id"], "att-new")
        with self.assertRaisesRegex(terminal.TerminalCommitError, "owner-not-current"):
            terminal.verify_request_identity(self.request, self.route)

    def test_same_size_atomic_replacement_with_restored_mtime_does_not_reuse_owner_axes(self):
        terminal.verify_request_identity(self.request, self.route)
        self.assertTrue(self.observe()["passed"])
        stamp = self.jobs.stat()
        before = self.jobs.read_bytes()
        changed = before.replace(b"registered_worker=1", b"registered_worker=0")
        self.assertEqual(len(before), len(changed))
        staged = self.root / "replacement"
        staged.write_bytes(changed)
        staged.replace(self.jobs)
        os.utime(self.jobs, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with self.assertRaisesRegex(terminal.TerminalCommitError, "owner-axes"):
            terminal.verify_request_identity(self.request, self.route)
        self.assertEqual(self.observe()["reason"], "owner-route-identity-mismatch")

    def test_foreign_route_node_and_conflicting_alias_do_not_replace_the_owner(self):
        self.jobs.write_text(self.row() + self.row(attempt="att-foreign",
            route_id="rt-other", route_hash="sha256:" + "f" * 64)
            + self.row(attempt="att-other-node", route_node="other"))
        with self.assertRaisesRegex(terminal.TerminalCommitError, "owner-not-current"):
            terminal.verify_request_identity(self.request, self.route)
        self.assertEqual(self.observe()["attempt_id"], "att-owner")
        with self.jobs.open("a") as stream:
            stream.write(self.row(attempt="att-conflict",
                owner_route_id=self.route["route_id"], owner_route_hash="sha256:" + "f" * 64))
        self.assertEqual(self.observe()["attempt_id"], "att-owner")
        # The broader identity verifier still selects this later related owner,
        # preserving its prior refusal instead of hiding a conflicting candidate.
        with self.assertRaisesRegex(terminal.TerminalCommitError, "owner-not-current"):
            terminal.verify_request_identity(self.request, self.route)

    def test_legacy_final_line_and_strict_decoding_keep_their_existing_behavior(self):
        self.jobs.write_text(self.row().rstrip("\n"))
        terminal.verify_request_identity(self.request, self.route)
        self.assertTrue(self.observe()["passed"])
        self.jobs.write_bytes(self.row().encode() + b"\xff")
        with self.assertRaises(UnicodeError):
            terminal.verify_request_identity(self.request, self.route)
        self.assertEqual(self.observe()["reason"], "owner-terminal-evidence-unverified")

    def test_report_digest_is_observed_again_when_registry_does_not_change(self):
        first = self.observe()
        self.assertTrue(first["passed"])
        self.report.write_text("changed exact report\n")
        second = self.observe()
        self.assertTrue(second["passed"])
        self.assertNotEqual(first["evidence_digest"], second["evidence_digest"])


if __name__ == "__main__":
    unittest.main()
