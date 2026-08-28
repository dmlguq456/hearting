#!/usr/bin/env python3
"""SD-111 P4: dispatch_session_sweep unit tests.

Every fixture injects HOME/XDG_STATE_HOME/HARNESS_STATE_ROOT into an isolated
temp tree and appends the actual values to evidence/sd111/fixture_env.tsv
(plan §10.1 hard gate), matching P1/P2's own fixture pattern.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PD = _load("dispatch_pending_delivery", HERE / "dispatch_pending_delivery.py")
SWEEP = _load("dispatch_session_sweep", HERE / "dispatch_session_sweep.py")

FIXTURE_ENV_LOG = os.environ.get("SD111_FIXTURE_ENV_LOG")


def _log_fixture_env(test_file: str, home: str, xdg: str, harness: str) -> None:
    if not FIXTURE_ENV_LOG:
        return
    with open(FIXTURE_ENV_LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{test_file}\t{home}\t{xdg}\t{harness}\n")


class IsolatedRootMixin:
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sd111-p4-")
        base = Path(self._tmp.name)
        home = base / "home"
        xdg = base / "xdg-state"
        harness = base / "harness-state"
        for d in (home, xdg, harness):
            d.mkdir(parents=True, exist_ok=True)
        self._env_patch = {
            "HOME": str(home),
            "XDG_STATE_HOME": str(xdg),
            "HARNESS_STATE_ROOT": str(harness),
        }
        self._saved_env = {k: os.environ.get(k) for k in self._env_patch}
        os.environ.update(self._env_patch)
        _log_fixture_env(
            "dispatch_session_sweep.test.py", str(home), str(xdg), str(harness)
        )
        self.root = harness / "dispatch"

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()
        super().tearDown()


def _receipt(**overrides):
    base = {
        "schema_version": 2,
        "state": "delivered",
        "parent_attempt_id": "att-0000000000000000000000000000aaaa",
        "job_registry": "/tmp/sd111p4/jobs.log",
        "children": [
            {
                "attempt_id": "att-0000000000000000000000000000bbbb",
                "status": "done",
                "readiness": "ready",
                "reason": "terminal-failure-or-unclosed",
                "required_action": "inspect-done-failure",
                "harness": "claude",
                "delivery_classification": "attention",
            }
        ],
        "delivery_classification": "attention",
    }
    base.update(overrides)
    return base


class SweepTest(IsolatedRootMixin, unittest.TestCase):
    def _seed(self, session_id="sess-owner", **overrides):
        receipt = overrides.pop("receipt", _receipt())
        kwargs = dict(
            root=self.root,
            recipient_kind="claude-parent-runtime",
            recipient_key=session_id,
            delivery_id="delivery-" + "a" * 32,
            session_generation="",
            session_generation_supported="0",
            attempt_ids=["att-0000000000000000000000000000bbbb"],
            parent_attempt_id="att-0000000000000000000000000000aaaa",
            route_id="rt-example",
            route_node="execute",
            receipt=receipt,
            receipt_digest=PD._canonical_receipt_digest(receipt),
            row_revisions={"att-0000000000000000000000000000bbbb": "deadbeef"},
        )
        kwargs.update(overrides)
        return PD.create(**kwargs)

    # -- A-21: generation-unproven claim refused, state unchanged. ---------

    def test_a21_sentinel_record_refused_generation_unproven_state_unchanged(self):
        self._seed("sess-owner")
        outcome, count = SWEEP.sweep(
            self.root, "claude-parent-runtime", "sess-owner", "unsupported"
        )
        self.assertEqual((outcome, count), ("refused", 1))
        record = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 0)
        self.assertIsNone(record["claim_owner"])

    def test_a21_refusal_reason_is_generation_fence_not_absence(self):
        # Distinguishes carrier 2's refusal (generation fence, this test)
        # from carrier 1's refusal (incarnation-binding mismatch, covered in
        # hooks/dispatch_owner_rewake.test.py) per round 2 C-3.
        self._seed("sess-owner")
        with self.assertRaises(PD.PendingDeliveryError) as ctx:
            PD.claim(
                self.root, "sess-owner", "delivery-" + "a" * 32,
                claim_owner="probe", lease_seconds=30.0,
                require_generation_proof=True,
            )
        self.assertEqual(ctx.exception.reason, "pending-delivery-generation-unproven")

    # -- A-11(i): different session_id -> different digest -> dir absent. --

    def test_a11_foreign_session_reads_zero_claims_zero(self):
        self._seed("sess-owner")
        outcome, count = SWEEP.sweep(
            self.root, "claude-parent-runtime", "sess-foreign", "unsupported"
        )
        self.assertEqual((outcome, count), ("refused", 0))
        self.assertFalse(
            PD.record_directory(self.root, "sess-foreign").is_dir()
        )

    # -- A-11(ii)/A-21 second incarnation: carrier 2 has no process binding, -
    # -- so a second incarnation of the *same* session_id is indistinguish- -
    # -- able from the first by digest; the generation fence refuses both. -

    def test_second_incarnation_same_session_id_still_refused_by_fence(self):
        self._seed("sess-owner")
        first = SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        second = SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        self.assertEqual(first, ("refused", 1))
        self.assertEqual(second, ("refused", 1))
        record = PD.read(self.root, "sess-owner", "delivery-" + "a" * 32)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 0)

    # -- empty / absent directory: no entries, no exception. ---------------

    def test_no_records_for_session_returns_refused_zero(self):
        outcome, count = SWEEP.sweep(
            self.root, "claude-parent-runtime", "sess-empty", "unsupported"
        )
        self.assertEqual((outcome, count), ("refused", 0))

    # -- fail-open: an unreadable directory must not raise. -----------------

    def test_unreadable_directory_fails_open(self):
        self._seed("sess-locked")
        directory = PD.record_directory(self.root, "sess-locked")
        original_mode = directory.stat().st_mode
        try:
            os.chmod(directory, 0)
            if os.access(directory, os.R_OK):
                self.skipTest("running as a user that bypasses directory permissions")
            outcome, count = SWEEP.sweep(
                self.root, "claude-parent-runtime", "sess-locked", "unsupported"
            )
            self.assertEqual((outcome, count), ("refused", 0))
        finally:
            os.chmod(directory, original_mode)

    # -- self-instrumentation: observation only, never a gate. -------------

    def test_self_instrumentation_appends_one_line_per_sweep(self):
        self._seed("sess-owner")
        log_path = self.root / "logs" / SWEEP.LOG_FILENAME
        self.assertFalse(log_path.exists())
        SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        self.assertTrue(log_path.is_file())
        lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(set(payload), {"ts_ns", "elapsed_ns", "entries", "claimed"})
        self.assertEqual(payload["entries"], 1)
        self.assertEqual(payload["claimed"], 0)
        SWEEP.sweep(self.root, "claude-parent-runtime", "sess-owner", "unsupported")
        self.assertEqual(
            len(log_path.read_text(encoding="utf-8").splitlines()), 2
        )

class OpenCodeStaticScanTest(unittest.TestCase):
    """A-22: OpenCode has 0 runtime credentials on this machine -- static
    source-scan only. PASS-by-execution is never claimed for this fixture
    (plan §7.1-3)."""

    def setUp(self):
        self.source = (
            ROOT / "adapters" / "opencode" / "plugins" / "hearting-guards.js"
        ).read_text(encoding="utf-8")

    def _handler_body(self, handler_key: str) -> str:
        marker = f'"{handler_key}": async ('
        start = self.source.index(marker)
        # Slice to the next top-level handler key at the same indent, or EOF.
        rest = self.source[start + len(marker):]
        end = rest.index('\n  "', 0) if '\n  "' in rest else len(rest)
        return rest[:end]

    def test_transform_handler_never_calls_the_sweep(self):
        body = self._handler_body("experimental.chat.system.transform")
        self.assertNotIn("sd111SessionSweep", body)

    def test_chat_message_handler_calls_the_sweep(self):
        body = self._handler_body("chat.message")
        self.assertIn("sd111SessionSweep(sid)", body)

    def test_sweep_helper_has_exactly_one_definition_and_one_call_site(self):
        # `function sd111SessionSweep(sid) {` (the definition) plus exactly
        # one `sd111SessionSweep(sid)` call expression (inside "chat.message",
        # asserted separately above) -- two occurrences of the name total.
        occurrences = self.source.count("sd111SessionSweep(sid)")
        self.assertEqual(occurrences, 2)
        self.assertEqual(self.source.count("function sd111SessionSweep(sid)"), 1)


if __name__ == "__main__":
    unittest.main()
