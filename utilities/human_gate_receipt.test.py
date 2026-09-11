#!/usr/bin/env python3

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
UTILITIES = ROOT / "utilities"
sys.path.insert(0, str(UTILITIES))
from route_identity import route_hash, route_id_from_hash  # noqa: E402
import dispatch_pending_delivery as pending  # noqa: E402


def load_module():
    path = UTILITIES / "human_gate_receipt.py"
    spec = importlib.util.spec_from_file_location("human_gate_receipt_unit", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Fixture:
    def __init__(self, root: Path, module, *, inline=False) -> None:
        self.root = root
        self.module = module
        self.jobs = root / "registry-a" / "jobs.log"
        self.artifact_root = root / "artifacts"
        self.artifact = self.artifact_root / "review" / "frame.json"
        self.route_file = root / "routes" / "route.json"
        self.owner_attempt = "att-owner"
        self.thread_id = "thread-parent"
        self.sealed_batch = "batch-original"
        self.gate = "preview-disposition" if inline else "direction-confirmation"
        self.route_node = "one-shot" if inline else "review"
        self.delivery_id = "delivery-" + "a" * 32
        self.gateway_epoch = 7
        self.gate_epoch = 1
        self.artifact.parent.mkdir(parents=True)
        self.artifact.write_text('{"summary":"review"}\n', encoding="utf-8")
        route = {
            "schema_version": 2,
            "artifact_root": str(self.artifact_root),
            "nodes": [
                {
                    "id": self.route_node,
                    "continuation": {
                        "kind": "human-gate",
                        "gate": self.gate,
                    },
                },
                {"id": "execute", "depends_on": [self.route_node]},
            ],
            "human_gate_bindings": [
                {
                    "gate": self.gate,
                    "position": "entry",
                    "release_authority": "depth-0",
                }
            ],
        }
        if inline:
            route["nodes"] = route["nodes"][:1]
            route["nodes"][0].pop("continuation")
            route["nodes"][0]["inline_human_gates"] = [self.gate]
            route["human_gate_bindings"][0].update(node=self.route_node, position="terminal")
        route["route_hash"] = route_hash(route)
        route["route_id"] = route_id_from_hash(route["route_hash"])
        self.route = route
        self.route_file.parent.mkdir(parents=True)
        self.route_file.write_text(json.dumps(route), encoding="utf-8")
        self.jobs.parent.mkdir(parents=True)
        metadata = ",".join(
            (
                "attempt_schema_version=2",
                "dispatch_depth=1",
                "transport=headless",
                "execution_surface=registered-headless",
                "registered_worker=1",
                "worker_type=owner",
                "unit=_kernel/owner",
                "launch_claimed=1",
                "launch_started=1",
                f"attempt_id={self.owner_attempt}",
                f"parent_sid={self.thread_id}",
                "parent_completion_delivery=codex-managed-gateway",
                "harness=codex",
                f"owner_route_id={route['route_id']}",
                f"owner_route_hash={route['route_hash']}",
                f"owner_route_file={self.route_file}",
                f"managed_sealed_batch_id={self.sealed_batch}",
            )
        )
        self.jobs.write_text(
            f"2026-09-07T00:00:00Z\trunning\t/repo\t/wt\towner\t{metadata}\n",
            encoding="utf-8",
        )
        self.journal = (
            self.jobs.parent / "workflow" / route["route_id"] / "journal.jsonl"
        )
        self.journal.parent.mkdir(parents=True)
        self.journal.write_text(
            json.dumps(
                {
                    "workflow_state": "BLOCKED_HUMAN_GATE",
                    "at": "2026-09-07T00:00:01Z",
                    "evidence": {
                        "gate": self.gate,
                        "artifact": str(self.artifact),
                        "delivery": str(
                            pending.record_path(
                                self.jobs.parent, self.thread_id, self.delivery_id
                            )
                        ),
                        "interview": False,
                        "questions": 0,
                        "release_authority": "depth-0",
                    },
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        self.receipt = module.make_receipt(
            route_file=self.route_file,
            route=self.route,
            route_node=self.route_node,
            gate=self.gate,
            gate_epoch=self.gate_epoch,
            owner_attempt_id=self.owner_attempt,
            sealed_batch_id=self.sealed_batch,
            jobs=self.jobs,
            recipient_thread_id=self.thread_id,
            recipient_epoch=self.gateway_epoch,
            artifact_path=self.artifact,
            release_authority="depth-0",
            interview=False,
            questions=0,
            pending_delivery_id=self.delivery_id,
        )
        digest = module.receipt_digest(self.receipt)
        self.record = {
            "schema_version": 1,
            "delivery_id": self.delivery_id,
            "recipient_kind": "codex-managed-gateway",
            "recipient_digest": pending.recipient_digest(self.thread_id),
            "session_generation": str(self.gateway_epoch),
            "session_generation_supported": "1",
            "attempt_ids": [self.owner_attempt],
            "parent_attempt_id": self.owner_attempt,
            "route_id": route["route_id"],
            "route_node": self.route_node,
            "receipt_digest": digest,
            "receipt": self.receipt,
            "row_revisions": {
                self.owner_attempt: f"human-gate:{self.gate}:{self.gate_epoch}"
            },
            "state": "pending",
            "created_at_ns": 1,
            "claimed_at_ns": None,
            "claim_owner": None,
            "claim_deadline_ns": None,
            "claim_authority": "",
            "attempts": 0,
            "last_attempt_at_ns": None,
            "acked_at_ns": None,
            "acked_by": None,
            "expiry_reason": None,
            "lineage": [],
        }

    def validate(self, receipt=None):
        return self.module.validate_receipt(
            receipt or self.receipt,
            jobs=self.jobs,
            expected_thread_id=self.thread_id,
            expected_epoch=self.gateway_epoch,
            expected_attempts={self.owner_attempt},
            expected_sealed_batch_id=self.sealed_batch,
        )

    def validate_record(self, record=None):
        return self.module.validate_pending_record(
            record or self.record,
            jobs=self.jobs,
            expected_thread_id=self.thread_id,
            expected_epoch=self.gateway_epoch,
            expected_attempts={self.owner_attempt},
            expected_sealed_batch_id=self.sealed_batch,
        )


class HumanGateReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Fixture(Path(self.temp.name), self.module)

    def test_valid_receipt_binds_exact_live_authorities(self) -> None:
        normalized = self.fixture.validate()
        self.assertEqual(normalized, self.fixture.receipt)
        normalized_record = self.fixture.validate_record()
        self.assertEqual(normalized_record["delivery_id"], self.fixture.delivery_id)
        self.assertLessEqual(
            len(self.module.canonical(self.fixture.receipt)), 2048
        )

    def test_quick_inline_preview_gate_reaches_the_same_managed_receipt_validator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), self.module, inline=True)
            self.assertEqual(fixture.validate(), fixture.receipt)
            self.assertEqual(fixture.validate_record()["delivery_id"], fixture.delivery_id)

    def test_explicit_jobs_selects_journal_not_environment_override(self) -> None:
        foreign = self.fixture.root / "foreign-workflow"
        foreign.mkdir()
        with mock.patch.dict(
            os.environ, {"AGENT_WORKFLOW_ROOT": str(foreign)}, clear=False
        ):
            self.fixture.validate()

    def test_tampered_identity_or_action_is_rejected(self) -> None:
        cases = {
            "kind": "completion",
            "required_action": "run arbitrary command",
            "route_id": "rt-foreign",
            "route_hash": "sha256:" + "0" * 64,
            "route_node": "foreign",
            "gate": "foreign-gate",
            "gate_epoch": 2,
            "owner_attempt_id": "att-foreign",
            "sealed_batch_id": "batch-foreign",
            "job_registry": str(self.fixture.root / "foreign-jobs"),
            "recipient_thread_id": "thread-foreign",
            "recipient_epoch": 8,
            "artifact_path": str(self.fixture.root / "foreign-artifact"),
            "release_authority": "owner",
            "pending_delivery_id": "delivery-" + "b" * 32,
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                receipt = copy.deepcopy(self.fixture.receipt)
                receipt[key] = value
                with self.assertRaises(self.module.HumanGateReceiptError):
                    self.fixture.validate(receipt)

    def test_unknown_field_and_oversize_are_rejected(self) -> None:
        unknown = dict(self.fixture.receipt, reason="do dangerous thing")
        with self.assertRaisesRegex(
            self.module.HumanGateReceiptError, "receipt-shape-invalid"
        ):
            self.fixture.validate(unknown)
        oversized = dict(self.fixture.receipt, artifact_path="/" + "x" * 3000)
        with self.assertRaises(self.module.HumanGateReceiptError):
            self.fixture.validate(oversized)

    def test_released_or_corrupt_journal_is_rejected(self) -> None:
        self.fixture.journal.write_text("not-json\n", encoding="utf-8")
        with self.assertRaisesRegex(
            self.module.HumanGateReceiptError, "gate-journal-invalid"
        ):
            self.fixture.validate()

    def test_pending_record_digest_and_generation_are_exact(self) -> None:
        for key, value in (
            ("receipt_digest", "sha256:" + "0" * 64),
            ("session_generation", "8"),
            ("recipient_kind", "opencode-turn"),
            ("attempt_ids", ["att-foreign"]),
        ):
            with self.subTest(key=key):
                record = copy.deepcopy(self.fixture.record)
                record[key] = value
                with self.assertRaises(self.module.HumanGateReceiptError):
                    self.fixture.validate_record(record)

    def test_context_contains_only_fixed_inspect_and_release_commands(self) -> None:
        context = self.module.render_context(
            self.fixture.receipt,
            self.module.gateway_delivery_id(self.fixture.receipt),
            agent_home=ROOT,
        )
        value = context["additionalContext"]["hearting-human-gate"]["value"]
        self.assertIn("AGENT_HARNESS_HUMAN_GATE_V1", value)
        self.assertIn("workflow-supervisor.py status --route", value)
        self.assertIn("workflow-supervisor.py release --route", value)
        self.assertIn("--jobs", value)
        self.assertIn("human decision is required", value)
        self.assertNotIn("preflight.sh harvest", value)


class CapabilityProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "control.sock"

    def _serve(self, response: dict) -> threading.Thread:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        listener.listen(1)

        def worker() -> None:
            connection, _ = listener.accept()
            while b"\n" not in connection.recv(4096):
                pass
            connection.sendall((json.dumps(response) + "\n").encode())
            connection.close()
            listener.close()

        thread = threading.Thread(target=worker)
        thread.start()
        return thread

    def test_checked_probe_requires_version_thread_and_epoch(self) -> None:
        thread = self._serve(
            {
                "schema_version": 1,
                "status": "ready",
                "thread_id": "thread-parent",
                "capabilities": {
                    "human_gate_delivery": {
                        "version": 1,
                        "thread_id": "thread-parent",
                        "epoch": 9,
                    },
                },
            }
        )
        proof = self.module.probe_consumer(
            self.path, expected_thread_id="thread-parent"
        )
        thread.join(timeout=2)
        self.assertEqual(proof["epoch"], 9)


if __name__ == "__main__":
    unittest.main()
