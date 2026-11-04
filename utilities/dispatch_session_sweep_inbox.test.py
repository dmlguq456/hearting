#!/usr/bin/env python3
"""Slice-2 contract tests: inbox discovery, effect authority, carriers, d9.

Every fixture lives under an isolated HOME/XDG/HARNESS_STATE_ROOT tree and no
test writes to the live per-user state. The module under test is the real
``utilities/dispatch_session_sweep.py``; only the detached runner request and
the external recovery entry are stubbed (a test must not start a real runner).

Covered here:

* a current derived inbox names the open records and no global scope is derived
* a stale/missing inbox never re-runs ``storage_recipients``; the previous inbox
  plus the session's own scope answer, and the runner request remains
* the effect path calls ``storage_recipients`` zero times and re-reads authority
  per record (delivery/claim/ACK still work, closed records stay closed)
* the effect group observes a registry append made after discovery
* ``recipients`` answers from a current inbox without reading a ledger and
  publishes owed ``delivery_ids``
* ``delivery_context`` refreshes only ``status=…; exit=…`` from
  ``run-states.json`` for one render and never rewrites the record
* the pass-wide deadline stops discovery without claiming
* prune keeps open, unknown and unreadable records and tombstones terminals
* the fixed performance fixture meets every required size and format
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import dispatch_pending_delivery as PD  # noqa: E402
import dispatch_recipient_inbox as INBOX  # noqa: E402
import dispatch_session_sweep as SWEEP  # noqa: E402
import dispatch_seat_handover as HANDOVER  # noqa: E402
import session_tidy as TIDY  # noqa: E402
from peer_read_context import PeerReadContext  # noqa: E402
from split_module_fixture import patch_split  # noqa: E402

_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "peer_steward_sweep_inbox_test", str(HERE / "peer-steward.py"))
STEWARD = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(STEWARD)

KIND = "claude-parent-runtime"
SESSION = "sess-inbox-target"
PREDECESSOR = "sess-inbox-before"


def _receipt(jobs: Path, attempt: str = "att-inbox-1") -> dict:
    return {
        "schema_version": 2,
        "state": "ready",
        "parent_attempt_id": "att-inbox-parent",
        "job_registry": str(jobs),
        "children": [{
            "attempt_id": attempt,
            "status": "done",
            "readiness": "ready",
            "required_action": "advance-completed",
            "delivery_classification": "success",
        }],
    }


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sweep-inbox-")
        base = Path(self._tmp.name)
        home, xdg, harness, peer = (base / name for name in ("home", "xdg", "harness", "peer"))
        for directory in (home, xdg, harness, peer):
            directory.mkdir(parents=True, exist_ok=True)
        self.root = harness / "dispatch"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs.log").touch()
        self.jobs = self.root / "jobs.log"
        self.env = {
            "HOME": str(home),
            "XDG_STATE_HOME": str(xdg),
            "HARNESS_STATE_ROOT": str(harness),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_PEER_LEDGER_ROOT": str(peer),
            "AGENT_HOME": str(ROOT),
            "AGENT_SESSION_ROLE": "",
            "AGENT_DISPATCH_CHILD": "",
            "AGENT_DISPATCH_DEPTH": "",
        }
        self._saved = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        self._patches = [
            mock.patch.object(SWEEP, "request_obligation_reconciliation"),
            mock.patch.object(SWEEP, "_reconcile_canonical_dispatch_state"),
            mock.patch("peer_obligations.ensure_normal_runner", return_value=False),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def seed(self, *, session: str = SESSION, delivery_id: str | None = None,
             attempt: str = "att-inbox-1", receipt: dict | None = None,
             kind: str = KIND) -> dict:
        receipt = receipt or _receipt(self.jobs, attempt)
        delivery_id = delivery_id or "delivery-" + "a" * 32
        return PD.create(
            self.root, recipient_kind=kind, recipient_key=session,
            delivery_id=delivery_id, session_generation="", session_generation_supported="0",
            attempt_ids=[attempt] if attempt else [], parent_attempt_id="att-inbox-parent",
            route_id="rt-inbox", route_node="execute", receipt=receipt,
            receipt_digest=PD._canonical_receipt_digest(receipt),
            row_revisions={attempt: "rev-1"} if attempt else {})

    def current_inbox(self, *, session: str = SESSION) -> dict:
        payload = INBOX.read_inbox(self.root, "claude", session)
        self.assertIsNotNone(payload)
        self.assertTrue(INBOX.is_current(payload, self.root))
        return payload

    def assert_no_global_scope(self):
        return mock.patch.object(HANDOVER, "storage_recipients",
                                 side_effect=AssertionError("global scope derived"))


class DiscoveryTest(IsolatedCase):
    def test_current_inbox_names_open_records_and_skips_global_scope(self):
        record = self.seed()
        first, count = SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertEqual([item["delivery_id"] for item in first], [record["delivery_id"]])
        payload = self.current_inbox()
        self.assertEqual([item["delivery_id"] for item in payload["delivery_ids"]],
                         [record["delivery_id"]])
        # Warm pass: the inbox names the record; no scope is re-derived.
        with self.assert_no_global_scope():
            second, warm_count = SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertEqual([item["delivery_id"] for item in second], [record["delivery_id"]])
        self.assertEqual(warm_count, count)

    def test_stale_inbox_uses_previous_names_and_own_scope_without_global_scope(self):
        record = self.seed()
        SWEEP.addressed_records(self.root, KIND, SESSION)
        payload = self.current_inbox()
        # An edge change is the exact stale signal; the previous names survive.
        INBOX.bump_edge_generation(self.root)
        self.assertFalse(INBOX.is_current(payload, self.root))
        with self.assert_no_global_scope():
            records, count = SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertEqual([item["delivery_id"] for item in records], [record["delivery_id"]])
        self.assertEqual(count, 1)
        self.assertEqual(INBOX.read_inbox(self.root, "claude", SESSION), payload)

    def test_stale_inbox_reads_every_recorded_scope_and_republishes(self):
        # A predecessor scope exists and is named by the inbox; a later record
        # under that key changes only that directory.
        old = self.seed(session=PREDECESSOR, delivery_id="delivery-" + "d" * 32,
                        attempt="att-pred-old")
        PD.claim_ready_batch(self.root, PREDECESSOR, old["delivery_id"],
                             claim_owner="pred-fixture", lease_seconds=30,
                             require_generation_proof=False, jobs=self.jobs)
        PD.ack(self.root, PREDECESSOR, old["delivery_id"], acked_by="pred-fixture")
        ledger = TIDY.state_root() / "sessions" / f"{SESSION}.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({
            "ts": 1, "event": "succession", "source": "tidy-clear",
            "harness": "claude", "sid": SESSION, "from": PREDECESSOR,
            "bindings": [
                {"jobs": str(self.jobs), "route": "rt-inbox",
                 "hash": "sha256:inbox", "node": "execute",
                 "attempt": "att-pred-old", "parent": PREDECESSOR},
                {"jobs": str(self.jobs), "route": "rt-inbox",
                 "hash": "sha256:inbox", "node": "execute",
                 "attempt": "att-pred-new", "parent": PREDECESSOR},
            ],
        }, sort_keys=True) + "\n", encoding="utf-8")
        HANDOVER._LINEAGE_GRAPH_CACHE.clear()
        SWEEP.addressed_records(self.root, KIND, SESSION)
        payload = self.current_inbox()
        self.assertIn(PREDECESSOR,
                      [item["storage_key"] for item in payload["candidates"]])
        new = self.seed(session=PREDECESSOR, delivery_id="delivery-" + "e" * 32,
                        attempt="att-pred-new")
        self.assertFalse(INBOX.is_current(payload, self.root))
        with self.assert_no_global_scope():
            records, _count = SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertEqual([item["delivery_id"] for item in records],
                         [new["delivery_id"]])
        # The pass completed under the same edge generation, so it republished
        # a warm inbox naming the predecessor record.
        republished = self.current_inbox()
        self.assertIn(new["delivery_id"],
                      [item["delivery_id"] for item in republished["delivery_ids"]])
        claimed, _count = SWEEP.sweep_deliver(self.root, KIND, SESSION)
        self.assertEqual([item["delivery_id"] for item in claimed],
                         [new["delivery_id"]])

    def test_missing_inbox_prefills_once_and_never_materializes_a_missing_root(self):
        self.seed()
        SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertTrue((self.root / "recipient-inbox" / "claude" / f"{SESSION}.json").is_file())
        missing = Path(self._tmp.name) / "absent" / "dispatch"
        self.assertEqual(SWEEP.addressed_records(missing, KIND, "sess-absent"), ([], 0))
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())

    def _succession_bindings(self, attempts):
        return [{"jobs": str(self.jobs), "route": "rt-inbox",
                 "hash": "sha256:inbox", "node": "execute",
                 "attempt": attempt, "parent": PREDECESSOR}
                for attempt in attempts]

    def test_a_record_created_where_the_scope_was_absent_at_publication_is_seen(self):
        # The granted predecessor scope does not exist yet at publication.
        ledger = TIDY.state_root() / "sessions" / f"{SESSION}.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({
            "ts": 1, "event": "succession", "source": "tidy-clear",
            "harness": "claude", "sid": SESSION, "from": PREDECESSOR,
            "bindings": self._succession_bindings(["att-pred-new"]),
        }, sort_keys=True) + "\n", encoding="utf-8")
        HANDOVER._LINEAGE_GRAPH_CACHE.clear()
        predecessor_dir = PD.record_directory(self.root, PREDECESSOR)
        self.assertFalse(predecessor_dir.exists())
        SWEEP.addressed_records(self.root, KIND, SESSION)
        payload = self.current_inbox()
        self.assertIn(str(predecessor_dir), payload["dir_identities"])
        self.assertIsNone(payload["dir_identities"][str(predecessor_dir)])
        # Creating the directory is a publication-relevant change even before
        # a record lands in it, and the new authorized record is then read.
        new = self.seed(session=PREDECESSOR, delivery_id="delivery-" + "e" * 32,
                        attempt="att-pred-new")
        self.assertFalse(INBOX.is_current(payload, self.root))
        with self.assert_no_global_scope():
            records, _count = SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertEqual([item["delivery_id"] for item in records], [new["delivery_id"]])

    def test_payload_refuses_a_generation_that_moved_since_the_caller_captured_it(self):
        self.seed()
        captured = INBOX.read_edge_generation(self.root)
        INBOX.bump_edge_generation(self.root)
        with self.assertRaises(OSError):
            SWEEP._inbox_payload(self.root, SESSION, "claude", [], [],
                                 edge_generation=captured, folded_edge_digest="d")
        self.assertIsNone(INBOX.read_inbox(self.root, "claude", SESSION))

    def test_first_prefill_is_not_published_when_a_writer_bumps_during_it(self):
        self.seed()
        real = HANDOVER.lineage_graph

        def racing(*args, **kwargs):
            INBOX.bump_edge_generation(self.root)
            return real(*args, **kwargs)

        with mock.patch.object(HANDOVER, "lineage_graph", side_effect=racing):
            SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertIsNone(INBOX.read_inbox(self.root, "claude", SESSION))
        SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertIsNotNone(INBOX.read_inbox(self.root, "claude", SESSION))

    def test_effect_path_calls_storage_recipients_zero_times_and_keeps_claim_ack(self):
        record = self.seed()
        SWEEP.addressed_records(self.root, KIND, SESSION)  # warm the derived inbox
        with self.assert_no_global_scope():
            claimed, _count = SWEEP.sweep_deliver(self.root, KIND, SESSION)
        self.assertEqual([item["delivery_id"] for item in claimed], [record["delivery_id"]])
        self.assertEqual(claimed[0]["claim_owner"].split(":")[0], "session-sweep")
        self.assertEqual(SWEEP.ack_delivered(self.root, SESSION, claimed, acked_by="inbox-test"), 1)
        self.assertEqual(PD.read(self.root, SESSION, record["delivery_id"])["state"], "acked")
        with self.assert_no_global_scope():
            self.assertEqual(SWEEP.sweep_deliver(self.root, KIND, SESSION)[0], [])

    def test_effect_group_observes_a_registry_append_after_discovery(self):
        record = self.seed()
        SWEEP.addressed_records(self.root, KIND, SESSION)

        def move_after_discovery(*args, **kwargs):
            records = original(*args, **kwargs)
            # A torn/damaged row is not six fields, so the claim's own value
            # parse ignores it; only a fresh complete observation can see that
            # the registry is no longer safe to project a recipient from.
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write("now\trunning\t/w\n")
            return records

        original = SWEEP.addressed_records
        with mock.patch.object(SWEEP, "addressed_records", side_effect=move_after_discovery):
            claimed, _count = SWEEP.sweep_deliver(self.root, KIND, SESSION)
        # The pre-claim check belongs to the discovery context, so the record is
        # claimed; the post-claim effect group re-observes and refuses to hand
        # it over. The established singleton recovery lease is preserved.
        self.assertEqual(claimed, [])
        stored = PD.read(self.root, SESSION, record["delivery_id"])
        self.assertEqual(stored["state"], "claimed")
        self.assertTrue(stored["claim_owner"].startswith("session-sweep:"))

    def test_claim_observes_a_registry_append_after_discovery(self):
        # The attempt has no row at discovery time, so only a claim that takes
        # its own observation under the record lock can retire this record.
        record = self.seed(attempt="att-late")
        SWEEP.addressed_records(self.root, KIND, SESSION)

        def move_after_discovery(*args, **kwargs):
            records = original(*args, **kwargs)
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write(
                    "now\tdone\t/w\t/w\ttask\t"
                    "attempt_id=att-late,parent_sid=sess-inbox-target,"
                    "replacement_original_attempt_id=att-before,"
                    "route_id=rt-inbox,route_hash=sha256:inbox,route_node=execute\n")
            return records

        original = SWEEP.addressed_records
        with mock.patch.object(SWEEP, "addressed_records", side_effect=move_after_discovery):
            claimed, _count = SWEEP.sweep_deliver(self.root, KIND, SESSION)
        self.assertEqual(claimed, [])
        stored = PD.read(self.root, SESSION, record["delivery_id"])
        self.assertEqual(stored["state"], "rejected")
        self.assertEqual(stored["expiry_reason"], "replacement-successor-not-launched")

    def test_sweep_claims_outside_any_registry_read_scope(self):
        """A claim must take its own observation, never an older caller pin."""
        import dispatch_registry_cache

        self.seed()
        SWEEP.addressed_records(self.root, KIND, SESSION)
        real = PD._claim_ready_batch_snapshot
        scopes = []

        def checked(*args, **kwargs):
            scopes.append(dispatch_registry_cache.registry_scope_active())
            return real(*args, **kwargs)

        with mock.patch.object(PD, "_claim_ready_batch_snapshot", side_effect=checked):
            claimed, _count = SWEEP.sweep_deliver(self.root, KIND, SESSION)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(scopes, [False])

    def test_shared_deadline_stops_discovery_without_claiming(self):
        record = self.seed()
        claimed, _count = SWEEP.sweep_deliver(self.root, KIND, SESSION, deadline_ns=1)
        self.assertEqual(claimed, [])
        self.assertEqual(PD.read(self.root, SESSION, record["delivery_id"])["state"], "pending")
        records, _count = SWEEP.addressed_records(self.root, KIND, SESSION, deadline_ns=1)
        self.assertFalse(getattr(records, "complete", True))


def write_succession(case, attempts, *, bump: bool, harness: str = "claude"):
    """The confirmed edge writer: append the succession row, then bump."""
    ledger = TIDY.state_root() / "sessions" / f"{SESSION}.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": 1, "event": "succession", "source": "tidy-clear",
           "harness": harness, "sid": SESSION, "from": PREDECESSOR,
           "bindings": [{"jobs": str(case.jobs), "route": "rt-inbox",
                         "hash": "sha256:inbox", "node": "execute",
                         "attempt": attempt, "parent": PREDECESSOR}
                        for attempt in attempts]}
    ledger.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    HANDOVER._LINEAGE_GRAPH_CACHE.clear()
    if bump:
        INBOX.bump_edge_generation(case.root)


def run_runner_prefill(case):
    with patch_split(STEWARD, "_live_recipients",
                           return_value={("claude", SESSION)}):
        return STEWARD._prefill_live_inboxes(
            case.root, deadline_ns=time.monotonic_ns() + 30_000_000_000)


class PublicationRaceTest(IsolatedCase):
    """An edge writer completing between scope enumeration and publication.

    The published inbox must never pair a newer edge generation/digest with
    scopes enumerated from an older lineage observation.
    """

    def assert_old_scopes_not_current(self, harness):
        payload = INBOX.read_inbox(self.root, harness, SESSION)
        if payload is None:
            return
        pred_dir = str(PD.record_directory(self.root, PREDECESSOR))
        self.assertFalse(
            INBOX.is_current(payload, self.root) and pred_dir not in payload["dir_identities"],
            "an inbox carrying the old scopes was published as current")

    def test_hook_first_prefill_refuses_old_scopes_after_a_completed_edge_writer(self):
        self.seed()
        real = HANDOVER.storage_recipients

        def scopes_then_writer(*args, **kwargs):
            result = real(*args, **kwargs)
            write_succession(self, ["att-pred-new"], bump=True)
            return result

        with mock.patch.object(HANDOVER, "storage_recipients",
                               side_effect=scopes_then_writer):
            SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assert_old_scopes_not_current("claude")
        new = self.seed(session=PREDECESSOR, delivery_id="delivery-" + "e" * 32,
                        attempt="att-pred-new")
        records, _count = SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertIn(new["delivery_id"], [item["delivery_id"] for item in records])

    def test_hook_stale_republish_refuses_when_a_writer_completes_before_publication(self):
        self.seed()
        SWEEP.addressed_records(self.root, KIND, SESSION)  # warm inbox, no predecessor
        self.seed(delivery_id="delivery-" + "d" * 32, attempt="att-inbox-2")  # makes it stale
        real = SWEEP._inbox_payload

        def writer_then_payload(*args, **kwargs):
            write_succession(self, ["att-pred-new"], bump=True)
            return real(*args, **kwargs)

        with mock.patch.object(SWEEP, "_inbox_payload", side_effect=writer_then_payload):
            SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assert_old_scopes_not_current("claude")
        new = self.seed(session=PREDECESSOR, delivery_id="delivery-" + "e" * 32,
                        attempt="att-pred-new")
        run_runner_prefill(self)
        records, _count = SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertIn(new["delivery_id"], [item["delivery_id"] for item in records])

    def test_recipients_slow_path_refuses_old_scopes_after_a_completed_edge_writer(self):
        self.seed(kind="opencode-turn", attempt="att-oc")
        real = SWEEP._read_addressed

        def read_then_writer(*args, **kwargs):
            result = real(*args, **kwargs)
            write_succession(self, ["att-oc-pred"], bump=True, harness="opencode")
            return result

        with mock.patch.object(SWEEP, "_read_addressed", side_effect=read_then_writer):
            SWEEP._recipients_view([self.root], SESSION, "opencode-turn")
        self.assert_old_scopes_not_current("opencode")
        new = self.seed(session=PREDECESSOR, delivery_id="delivery-" + "9" * 32,
                        attempt="att-oc-pred", kind="opencode-turn")
        view = SWEEP._recipients_view([self.root], SESSION, "opencode-turn")
        self.assertIn(new["delivery_id"], view["delivery_ids"])

    def test_stale_republish_keeps_the_recorded_digest_so_the_runner_repairs_a_missed_bump(self):
        self.seed(delivery_id="delivery-" + "a" * 32, attempt="att-b1")
        SWEEP.addressed_records(self.root, KIND, SESSION)
        warm = INBOX.read_inbox(self.root, "claude", SESSION)
        # A confirmed edge writer whose best-effort bump failed.
        write_succession(self, ["att-missed-new"], bump=False)
        self.seed(delivery_id="delivery-" + "d" * 32, attempt="att-b2")  # stale pass trigger
        SWEEP.addressed_records(self.root, KIND, SESSION)
        republished = INBOX.read_inbox(self.root, "claude", SESSION)
        self.assertEqual(republished["edge_generation"], warm["edge_generation"])
        self.assertEqual(republished["folded_edge_digest"], warm["folded_edge_digest"])
        fresh = INBOX.edge_digest(HANDOVER.lineage_graph().edges)
        self.assertNotEqual(republished["folded_edge_digest"], fresh)
        self.assertFalse(INBOX.is_current(republished, self.root, folded_edge_digest=fresh))
        new = self.seed(session=PREDECESSOR, delivery_id="delivery-" + "f" * 32,
                        attempt="att-missed-new")
        result = run_runner_prefill(self)
        self.assertEqual(result["written"], 1)
        with self.assert_no_global_scope():
            records, _count = SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertIn(new["delivery_id"], [item["delivery_id"] for item in records])


class StaleReadOrderTest(IsolatedCase):
    def test_a_cut_stale_pass_reads_the_new_record_before_the_old_prefix(self):
        attempts = [f"att-order-{index}" for index in range(3)]
        ledger = TIDY.state_root() / "sessions" / f"{SESSION}.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({
            "ts": 1, "event": "succession", "source": "tidy-clear",
            "harness": "claude", "sid": SESSION, "from": PREDECESSOR,
            "bindings": [{"jobs": str(self.jobs), "route": "rt-inbox",
                          "hash": "sha256:inbox", "node": "execute",
                          "attempt": attempt, "parent": PREDECESSOR}
                         for attempt in attempts + ["att-order-new"]],
        }, sort_keys=True) + "\n", encoding="utf-8")
        HANDOVER._LINEAGE_GRAPH_CACHE.clear()
        for index, attempt in enumerate(attempts):
            self.seed(session=PREDECESSOR,
                      delivery_id="delivery-" + f"{0xAA00 + index:064x}",
                      attempt=attempt)
        SWEEP.addressed_records(self.root, KIND, SESSION)
        self.assertEqual(len(self.current_inbox()["delivery_ids"]), 3)
        # Twelve terminal records sort between the previous names and the new
        # record, so the old sorted prefix would consume a cut pass first.
        now = time.time()
        for index in range(12):
            record = self.seed(session=PREDECESSOR,
                               delivery_id="delivery-" + f"{0xBB00 + index:064x}",
                               attempt=f"att-old-{index}")
            PD.claim_ready_batch(self.root, PREDECESSOR, record["delivery_id"],
                                 claim_owner="old-fixture", lease_seconds=30,
                                 require_generation_proof=False, jobs=self.jobs)
            PD.ack(self.root, PREDECESSOR, record["delivery_id"], acked_by="old-fixture")
            path = PD.record_path(self.root, PREDECESSOR, record["delivery_id"])
            stamp = now - 3600 - index
            os.utime(path, (stamp, stamp))
        new = self.seed(session=PREDECESSOR, delivery_id="delivery-" + "f" * 64,
                        attempt="att-order-new")
        INBOX.bump_edge_generation(self.root)
        calls = {"n": 0}

        def clock():
            calls["n"] += 1
            return 0 if calls["n"] <= 6 else 10 ** 15

        with mock.patch.object(SWEEP, "time",
                               SimpleNamespace(monotonic_ns=clock,
                                               time_ns=time.time_ns)):
            records, _count = SWEEP.addressed_records(
                self.root, KIND, SESSION, deadline_ns=1)
        self.assertFalse(getattr(records, "complete", True))
        # Named previous records (3) then the newest scope entry: the new
        # record is reached before the terminal prefix.
        self.assertIn(new["delivery_id"], [record["delivery_id"] for record in records])


class RecipientsActionTest(IsolatedCase):
    def test_recipients_answers_from_a_current_inbox_without_a_ledger_read(self):
        record = self.seed(kind="opencode-turn")
        SWEEP.addressed_records(self.root, "opencode-turn", SESSION)
        output = io.StringIO()
        with mock.patch.object(SWEEP, "_state_roots", return_value=[self.root]), \
                mock.patch.object(HANDOVER, "lineage_graph",
                                  side_effect=AssertionError("ledger read")), \
                mock.patch.object(HANDOVER, "storage_recipients",
                                  side_effect=AssertionError("global scope")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(SWEEP.main(["recipients", "--recipient-kind", "opencode-turn",
                                         "--session", SESSION]), 0)
        result = json.loads(output.getvalue())
        self.assertIn(record["delivery_id"], result["delivery_ids"])
        self.assertTrue(result["generation"].startswith("0:"))

    def test_recipients_generation_changes_when_the_edge_generation_changes(self):
        self.seed()
        SWEEP.addressed_records(self.root, KIND, SESSION)
        first = SWEEP._recipients_view([self.root], SESSION, "opencode-turn")
        INBOX.bump_edge_generation(self.root)
        second = SWEEP._recipients_view([self.root], SESSION, "opencode-turn")
        self.assertNotEqual(first["generation"], second["generation"])

    def test_recipients_slow_path_bootstraps_with_published_compact_rows(self):
        # The compact builder does not seed the full-read cache, so the slow
        # path must compare the observation, not object identity. Generation 0
        # (a fresh install) must still publish the first OpenCode inbox.
        record = self.seed(kind="opencode-turn")
        HANDOVER.publish_lineage_rows(self.root)
        self.assertIsNotNone(INBOX.read_lineage_rows(self.root))
        self.assertIsNone(INBOX.read_inbox(self.root, "opencode", SESSION))
        self.assertEqual(INBOX.read_edge_generation(self.root), 0)
        view = SWEEP._recipients_view([self.root], SESSION, "opencode-turn")
        self.assertIn(record["delivery_id"], view["delivery_ids"])
        payload = INBOX.read_inbox(self.root, "opencode", SESSION)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["edge_generation"], 0)
        self.assertTrue(INBOX.is_current(payload, self.root))
        self.assertEqual(view["generation"],
                         f"{payload['edge_generation']}:{payload['folded_edge_digest'] or ''}")

    def test_recipients_slow_path_refuses_a_writer_change_during_discovery(self):
        self.seed(kind="opencode-turn")
        real = HANDOVER.storage_recipients
        sessions = TIDY.state_root() / "sessions"

        def racing(*args, **kwargs):
            # A confirmed edge writer appends a ledger while discovery runs.
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "race.jsonl").write_text(json.dumps({
                "ts": 9, "harness": "claude", "sid": "race-target",
                "event": "handover", "source": "clear",
                "from": {"harness": "claude", "sid": "race-origin"},
                "bindings": [],
            }, sort_keys=True) + "\n", encoding="utf-8")
            HANDOVER._LINEAGE_GRAPH_CACHE.clear()
            return real(*args, **kwargs)

        with mock.patch.object(HANDOVER, "storage_recipients", side_effect=racing):
            with self.assertRaises(OSError):
                SWEEP._recipients_view([self.root], SESSION, "opencode-turn")
        self.assertIsNone(INBOX.read_inbox(self.root, "opencode", SESSION))


class DeliveryContextRefreshTest(IsolatedCase):
    def notice(self) -> dict:
        receipt = {
            "schema_version": 1,
            "kind": "notice",
            "subject": "experiment final",
            "text": f"experiment perf-run finished; status=running; exit=None; result path artifacts/run.json",
            "required_action": "report-to-user",
            "key": "experiment:perf-run:final:1",
        }
        return PD.create(
            self.root, recipient_kind=KIND, recipient_key=SESSION,
            delivery_id="delivery-" + "b" * 32, session_generation="",
            session_generation_supported="0", attempt_ids=[], parent_attempt_id="",
            route_id="route-free", route_node="notice", receipt=receipt,
            receipt_digest=PD._canonical_receipt_digest(receipt), row_revisions={})

    def test_newer_public_run_state_rewrites_only_the_status_part(self):
        record = self.notice()
        before = PD.read(self.root, SESSION, record["delivery_id"])
        INBOX.write_run_states(self.root, {
            "generated_at": time.time() + 60,
            "runs": {"perf-run": {"status": "success", "exit_code": 0}},
        })
        text = SWEEP.delivery_context([(self.root, [{**before, SWEEP.STORAGE_KEY: SESSION}])])
        self.assertIn("status=success; exit=0", text)
        self.assertNotIn("status=running", text)
        self.assertIn("artifacts/run.json", text)
        self.assertIn("HEARTING_RECEIPT_V1", text)
        self.assertEqual(PD.read(self.root, SESSION, record["delivery_id"]), before)

    def test_missing_older_or_unknown_state_leaves_the_notice_text_unchanged(self):
        record = self.notice()
        stored = PD.read(self.root, SESSION, record["delivery_id"])
        view = [{**stored, SWEEP.STORAGE_KEY: SESSION}]
        self.assertIn("status=running; exit=None", SWEEP.delivery_context([(self.root, view)]))
        INBOX.write_run_states(self.root, {
            "generated_at": time.time() - 3600,
            "runs": {"perf-run": {"status": "success", "exit_code": 0}},
        })
        self.assertIn("status=running; exit=None", SWEEP.delivery_context([(self.root, view)]))
        INBOX.write_run_states(self.root, {
            "generated_at": time.time() + 60,
            "runs": {"other-run": {"status": "success", "exit_code": 0}},
        })
        self.assertIn("status=running; exit=None", SWEEP.delivery_context([(self.root, view)]))

    def test_hook_refresh_opens_no_registry_file(self):
        record = self.notice()
        stored = PD.read(self.root, SESSION, record["delivery_id"])
        INBOX.write_run_states(self.root, {
            "generated_at": time.time() + 60,
            "runs": {"perf-run": {"status": "success", "exit_code": 0}},
        })
        with mock.patch.object(HANDOVER, "lineage_graph",
                               side_effect=AssertionError("registry/ledger read")):
            text = SWEEP.delivery_context([(self.root, [{**stored, SWEEP.STORAGE_KEY: SESSION}])])
        self.assertIn("status=success; exit=0", text)


class PruneRetentionTest(IsolatedCase):
    def test_prune_keeps_open_unknown_and_unreadable_records(self):
        open_record = self.seed()
        terminal = self.seed(delivery_id="delivery-" + "c" * 32, attempt="att-inbox-2")
        PD.claim_ready_batch(self.root, SESSION, terminal["delivery_id"],
                             claim_owner="prune-fixture", lease_seconds=30,
                             require_generation_proof=False, jobs=self.jobs)
        PD.ack(self.root, SESSION, terminal["delivery_id"], acked_by="prune-fixture")
        old = time.time() - 9 * 86400
        for path in (self.root / "pending-delivery").glob("*/delivery-*.json"):
            os.utime(path, (old, old))
        unknown = self.root / "pending-delivery" / ("f" * 64) / ("delivery-" + "d" * 32 + ".json")
        unknown.parent.mkdir(parents=True, exist_ok=True)
        unknown.write_text("{not-json", encoding="utf-8")
        result = PD.prune(self.root, apply=True)
        self.assertGreaterEqual(result["pruned_records"], 1)
        self.assertEqual(PD.read(self.root, SESSION, open_record["delivery_id"])["state"], "pending")
        self.assertTrue(unknown.is_file())
        self.assertIsNone(PD.read(self.root, SESSION, terminal["delivery_id"]))
        self.assertTrue(list((self.root / "pending-delivery").glob("*/delivery-*.json.pruned")))


class LockChurnTest(IsolatedCase):
    def test_lock_and_tombstone_files_are_never_named_in_the_inbox(self):
        record = self.seed()
        SWEEP.addressed_records(self.root, KIND, SESSION)
        directory = PD.record_directory(self.root, SESSION)
        (directory / "delivery-lock-only.json.lock").write_text("", encoding="utf-8")
        (directory / ("delivery-" + "e" * 32 + ".json.pruned")).write_text("{}", encoding="utf-8")
        payload = INBOX.read_inbox(self.root, "claude", SESSION)
        self.assertIn(record["delivery_id"], [item["delivery_id"] for item in payload["delivery_ids"]])
        self.assertNotIn("delivery-lock-only", [item["delivery_id"] for item in payload["delivery_ids"]])
        self.assertNotIn("delivery-" + "e" * 32,
                         [item["delivery_id"] for item in payload["delivery_ids"]])


class AdapterWiringTest(unittest.TestCase):
    def test_opencode_carrier_follows_delivery_ids_and_keeps_legacy_fallback(self):
        source = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text(encoding="utf-8")
        self.assertIn("Array.isArray(current.delivery_ids)", source)
        self.assertIn("cached.deliveryIds", source)
        # The normal branch never scans the delivery directories.
        primary = source.split("Array.isArray(current.delivery_ids)", 1)[1].split(
            "Array.isArray(current.digests)", 1)[0]
        self.assertNotIn("readdirSync", primary)

    def test_codex_first_prompt_uses_the_inbox_before_the_own_dir_scan(self):
        source = (ROOT / "adapters/codex/hooks/userprompt-lifecycle.py").read_text(encoding="utf-8")
        self.assertIn("_sd111_inbox_delivery_ids", source)
        self.assertIn("delivery_ids=delivery_ids", source)


class HookPerfFixtureTest(unittest.TestCase):
    def test_fixed_fixture_meets_every_required_size(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import hook_perf_fixture
        with tempfile.TemporaryDirectory(prefix="hook-fixture-") as base:
            fixture = hook_perf_fixture.build(base)
            try:
                counts = hook_perf_fixture.self_check(fixture)
            finally:
                import shutil
                shutil.rmtree(base, ignore_errors=True)
        self.assertGreaterEqual(counts["ledgers"], 550)
        self.assertGreaterEqual(counts["bindings"], 1000)
        self.assertGreaterEqual(counts["open_pending_seen"], 3000)
        self.assertGreaterEqual(counts["peer_rows"], 6000)
        self.assertGreaterEqual(counts["jobs_rows_seen"], 9391)
        self.assertGreaterEqual(counts["jobs_bytes"], 49 * 1024 * 1024)
        self.assertEqual(counts["target_scopes"], [hook_perf_fixture.PREDECESSOR, "perf-target"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
