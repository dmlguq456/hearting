from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm
import artifact_identity as idm
import artifact_index as ix
import artifact_lifecycle as lc
import artifact_manifest as m
import artifact_receipt as r

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_FIXTURES = _HERE / "fixtures" / "artifact-receipt"
_V2_EXAMPLE = _REPO_ROOT / "capabilities" / "report-bundle-receipt.v2.example.json"


def _sha(n):
    return "sha256:" + (str(n) * 64)[:64]


def _document(identity, alloc, content=b"hello"):
    camp_id = alloc.allocate("campaign")
    cyc_id = alloc.allocate("cycle")
    art_id = alloc.allocate("artifact")
    arev_id = alloc.allocate("artifact_revision")
    man_id = alloc.allocate("manifest")
    mrev_id = alloc.allocate("manifest_revision")
    prod_id = alloc.allocate("producer")
    digest = m.digest_bytes(content)
    provenance = {
        "source_manifest_id": man_id,
        "source_revision_id": mrev_id,
        "producer_route_id": "r",
        "algorithm_version": "v1",
        "schema_version": 1,
        "source_digest": _sha(2),
    }
    doc = {
        "schema_version": 2,
        "manifest_kind": "artifact.cycle",
        "manifest_id": man_id,
        "manifest_revision_id": mrev_id,
        "repository_id": identity.repository_id,
        "artifact_root_id": identity.artifact_root_id,
        "campaign": {
            "campaign_id": camp_id,
            "goal": "g",
            "completion_criterion": {"statement": "s"},
            "title": "t",
            "state": "active",
        },
        "cycle": {
            "cycle_id": cyc_id,
            "campaign_id": camp_id,
            "parent_cycle_id": None,
            "started_on": "2026-08-11T00:00:00Z",
            "input_digest": _sha(0),
            "outcome_criterion": {"required_artifact_roles": [], "decision_required": False},
            "state": "active",
        },
        "artifacts": [
            {
                "artifact_id": art_id,
                "cycle_id": cyc_id,
                "role": "primary",
                "type": "doc",
                "capability": "c",
                "title": "t",
            }
        ],
        "artifact_revisions": [
            {
                "artifact_revision_id": arev_id,
                "artifact_id": art_id,
                "revision_sequence": 1,
                "content_digest": digest,
                "byte_size": len(content),
                "media_type": "text/plain",
                "locator": {"kind": "cycle-relative", "path": "plan.md"},
                "provenance": provenance,
            }
        ],
        "shared_references": [],
        "shared_reference_revisions": [],
        "routes": [],
        "events": [],
        "producer": {
            "producer_id": prod_id,
            "contract_version": "artifact-cycle-manifest/v2",
            "source_revision": "abc",
        },
    }
    ids = {
        "repository_id": identity.repository_id,
        "campaign_id": camp_id,
        "cycle_id": cyc_id,
        "artifact_id": art_id,
        "artifact_revision_id": arev_id,
        "manifest_id": man_id,
        "manifest_revision_id": mrev_id,
    }
    return doc, ids


def _stage(content=b"hello"):
    staging = tempfile.mkdtemp(prefix="artifact-receipt-stage-")
    with open(os.path.join(staging, "plan.md"), "wb") as handle:
        handle.write(content)
    return staging


def _admit_fixture_root(seed=b"artifact-receipt-fixture-seed-0000"):
    """Admit one manifest into a fresh temp artifact root via the real step-1 API.

    Returns (root: str, ids: dict, cycle_path: str, alloc, identity, doc).
    Never touches the canonical artifact root or canonical registry/routes.
    """
    root = tempfile.mkdtemp(prefix="artifact-receipt-root-")
    alloc = idm.IdAllocator(idm.FixedEntropy(seed))
    identity = adm.ensure_root_identity(root, allocator=alloc)
    doc, ids = _document(identity, alloc)
    staging = _stage()
    outcome = adm.admit(
        root,
        adm.AdmissionRequest(idempotency_key=doc["manifest_id"], document=doc, staging_source=staging, allocator=alloc),
    )
    if outcome.status != "admitted":
        raise AssertionError("fixture admission failed: {0} {1}".format(outcome.status, outcome.violations))
    return root, ids, outcome.cycle_path, alloc, identity, doc


def _v3_receipt(ids, completed_at="2026-08-11T00:00:00Z"):
    return r.build_v3(completed_at=completed_at, **ids)


class GoldenAcceptTests(unittest.TestCase):
    def test_golden_v1_accepts(self):
        payload = json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8"))
        verdict = r.decode(payload)
        self.assertEqual(verdict.state, "accepted")
        self.assertEqual(verdict.schema_version, 1)

    def test_golden_v2_example_file_accepts(self):
        payload = json.loads(_V2_EXAMPLE.read_text(encoding="utf-8"))
        verdict = r.decode(payload)
        self.assertEqual(verdict.state, "accepted")
        self.assertEqual(verdict.schema_version, 2)

    def test_golden_v3_accepts_against_admitted_root(self):
        root, ids, _cycle_path, _alloc, _identity, _doc = _admit_fixture_root()
        receipt = _v3_receipt(ids)
        decoded = r.decode(receipt)
        self.assertEqual(decoded.state, "accepted")
        resolved = r.resolve(root, decoded.receipt)
        self.assertEqual(resolved.state, "accepted", resolved.reason)


class CanonicalBytesTests(unittest.TestCase):
    def _run_sink_emit(self, extra_args):
        sink = _REPO_ROOT / "utilities" / "artifact-sink.sh"
        capture_dir = tempfile.mkdtemp(prefix="artifact-receipt-sink-")
        handler = Path(capture_dir) / "handler.sh"
        captured = Path(capture_dir) / "captured.json"
        handler.write_text(
            "#!/bin/sh\n"
            "set -u\n"
            "case \"${1:-}\" in\n"
            "  --check) printf '{\"status\":\"connected\"}\\n'; exit 0 ;;\n"
            "  --receipt) cp -- \"$2\" \"" + str(captured) + "\"; printf '{\"status\":\"created\"}\\n'; exit 0 ;;\n"
            "  *) exit 64 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        handler.chmod(0o700)
        env = dict(os.environ)
        env["AGENT_ARTIFACT_SINK_COMMAND"] = str(handler)
        result = subprocess.run(
            ["sh", str(sink), "emit"] + extra_args,
            capture_output=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return captured.read_bytes()

    def test_sink_v1_emit_roundtrip_is_byte_canonical(self):
        staging = tempfile.mkdtemp(prefix="artifact-receipt-v1src-")
        source = Path(staging) / "report.md"
        source.write_text("hello", encoding="utf-8")
        raw = self._run_sink_emit(
            [
                "--source",
                str(source),
                "--capability",
                "dev-backend",
                "--project-root",
                staging,
                "--completed-at",
                "2026-08-11T00:00:00Z",
            ]
        )
        decoded = r.decode(json.loads(raw))
        self.assertEqual(decoded.state, "accepted")
        self.assertEqual(r.canonical_bytes(decoded.receipt), raw)

    def test_sink_v2_emit_roundtrip_is_byte_canonical(self):
        raw = self._run_sink_emit(
            [
                "--bundle-id",
                "project/experiment",
                "--bundle-version",
                "v1",
                "--entrypoint",
                "report/index.html",
                "--completed-at",
                "2026-08-11T00:00:00Z",
            ]
        )
        decoded = r.decode(json.loads(raw))
        self.assertEqual(decoded.state, "accepted")
        self.assertEqual(r.canonical_bytes(decoded.receipt), raw)

    def test_canonical_bytes_preserves_declared_order(self):
        payload = json.loads(_V2_EXAMPLE.read_text(encoding="utf-8"))
        data = r.canonical_bytes(payload)
        self.assertTrue(data.startswith(b'{"schema_version":2,"event":'))


class CorpusTests(unittest.TestCase):
    def test_case_corpus(self):
        cases = json.loads((_FIXTURES / "cases.json").read_text(encoding="utf-8"))
        self.assertGreater(len(cases), 0)
        for case in cases:
            with self.subTest(name=case["name"]):
                verdict = r.decode(case["input"])
                self.assertEqual(verdict.state, case["expected_state"], case["name"])
                self.assertEqual(verdict.reason, case["expected_reason"], case["name"])

    def test_case_corpus_covers_every_reason(self):
        cases = json.loads((_FIXTURES / "cases.json").read_text(encoding="utf-8"))
        corpus_reasons = {c["expected_reason"] for c in cases if c["expected_reason"] is not None}
        decode_reasons = {
            "value-invalid",
            "unknown-schema-version",
            "key-set-mismatch",
            "partial-bundle-identity",
            "partial-manifest-identity",
            "mixed-version-fields",
            "unknown-field",
        }
        lineage_reasons = {"local-manifest-unregistered", "local-lineage-mismatch", "local-state-unreadable"}
        self.assertEqual(r.REASONS, decode_reasons | lineage_reasons | {"identity-conflict"})
        missing = decode_reasons - corpus_reasons
        self.assertEqual(missing, set(), "decode-stage reasons missing from cases.json: {0}".format(missing))

    def test_case_corpus_covers_every_ladder_branch(self):
        cases = json.loads((_FIXTURES / "cases.json").read_text(encoding="utf-8"))
        declared_branches = {
            "R0",
            "R1",
            "R2a-exact-version-swap",
            "R2a-mixed-foreign",
            "R2a-else",
            "R2b-partial-bundle",
            "R2b-partial-manifest",
            "R2b-else",
            "R2c-mixed",
            "R2c-unknown-field",
            "R3",
        }
        seen_branches = {c["ladder_branch"] for c in cases}
        missing = declared_branches - seen_branches
        self.assertEqual(missing, set(), "ladder branches never exercised by cases.json: {0}".format(missing))

    def test_case_corpus_key_deltas_are_derived_not_declared(self):
        """A self-reported `ladder_branch` cannot be trusted on its own.

        Each case declares the exact key delta it claims to exercise; this
        recomputes it from the input and the version key table, then checks the
        branch family against the (missing, extra) quadrant the ladder uses.
        """

        cases = json.loads((_FIXTURES / "cases.json").read_text(encoding="utf-8"))
        quadrant = {
            (True, True): "R2a",
            (True, False): "R2b",
            (False, True): "R2c",
            (False, False): "R3",
        }
        checked = 0
        for case in cases:
            with self.subTest(name=case["name"]):
                payload = case["input"]
                version = payload.get("schema_version") if isinstance(payload, dict) else None
                usable = (
                    isinstance(payload, dict)
                    and isinstance(version, int)
                    and not isinstance(version, bool)
                    and version in r.KEY_SETS
                )
                if not usable:
                    # R0/R1 refuse before any key set applies.
                    self.assertIsNone(case["expected_missing"], case["name"])
                    self.assertIsNone(case["expected_extra"], case["name"])
                    self.assertIn(case["ladder_branch"], ("R0", "R1"), case["name"])
                    continue
                keys = set(payload)
                actual_missing = sorted(r.KEY_SETS[version] - keys)
                actual_extra = sorted(keys - r.KEY_SETS[version])
                self.assertEqual(actual_missing, case["expected_missing"], case["name"])
                self.assertEqual(actual_extra, case["expected_extra"], case["name"])
                family = quadrant[(bool(actual_missing), bool(actual_extra))]
                self.assertTrue(
                    case["ladder_branch"].startswith(family),
                    "{0}: key delta implies {1}, label says {2}".format(
                        case["name"], family, case["ladder_branch"]
                    ),
                )
                checked += 1
        self.assertGreater(checked, 0)

    def test_case_corpus_covers_every_a4_row(self):
        """`a4_row` is a coverage claim, so something has to count it."""

        cases = json.loads((_FIXTURES / "cases.json").read_text(encoding="utf-8"))
        declared_rows = {
            "partial bundle field reject",
            "partial manifest identity reject",
            "v1/v2/v3 mixed reject",
            "unknown/extra field reject",
            "wrong version/key set reject",
        }
        seen_rows = {c["a4_row"] for c in cases}
        missing = declared_rows - seen_rows
        self.assertEqual(missing, set(), "A-4 rows never exercised by cases.json: {0}".format(missing))
        self.assertTrue(all(c["a4_row"] for c in cases))

    # -- lineage mutation vocabulary, shared by test_lineage_case_corpus --

    def _foreign_id(self, kind, tag):
        alloc = idm.IdAllocator(idm.FixedEntropy(("foreign-" + kind + "-" + tag).encode("utf-8")))
        return alloc.allocate(kind)

    def _apply_lineage_mutation(self, fixture, action):
        root = fixture["root"]
        ids = dict(fixture["ids"])
        cycle_path = fixture["cycle_path"]
        second_ids = fixture["second_ids"]
        index_path = Path(root) / adm.ADMISSION_REL / "index.json"
        identity_path = Path(root) / adm.ADMISSION_REL / "root-identity.json"
        manifest_path = Path(root) / cycle_path / "manifest.json"

        if action == "delete-root-identity":
            identity_path.unlink()
        elif action == "corrupt-root-identity":
            identity_path.write_text("{not valid json", encoding="utf-8")
        elif action == "foreign-repository-id":
            ids["repository_id"] = self._foreign_id("repository", "s2")
        elif action == "delete-index":
            index_path.unlink()
        elif action == "corrupt-index":
            index_path.write_text("{not valid json", encoding="utf-8")
        elif action == "tamper-index-artifact-root-id":
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            payload["artifact_root_id"] = self._foreign_id("artifact_root", "s4")
            index_path.write_text(json.dumps(payload), encoding="utf-8")
        elif action == "unregistered-artifact-id":
            ids["artifact_id"] = self._foreign_id("artifact", "s5")
        elif action == "swap-cycle-id-with-manifest-id":
            ids["cycle_id"] = ids["manifest_id"]
        elif action == "cross-document-cycle-id":
            ids["cycle_id"] = second_ids["cycle_id"]
        elif action == "foreign-campaign-id":
            ids["campaign_id"] = self._foreign_id("campaign", "s7")
        elif action == "unregistered-manifest-revision-id":
            ids["manifest_revision_id"] = self._foreign_id("manifest_revision", "s8")
        elif action == "duplicate-manifest-triple":
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            matches = [
                row for row in payload["manifests"].values()
                if row.get("manifest_id") == ids["manifest_id"]
                and row.get("manifest_revision_id") == ids["manifest_revision_id"]
                and row.get("cycle_id") == ids["cycle_id"]
            ]
            self.assertEqual(len(matches), 1)
            payload["manifests"]["duplicate-s8-key"] = dict(matches[0])
            index_path.write_text(json.dumps(payload), encoding="utf-8")
        elif action == "tamper-cycle-manifest-digest":
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            payload["cycles"][ids["cycle_id"]]["manifest_digest"] = _sha(999)
            index_path.write_text(json.dumps(payload), encoding="utf-8")
        elif action == "tamper-cycle-path-non-str":
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            payload["cycles"][ids["cycle_id"]]["cycle_path"] = None
            index_path.write_text(json.dumps(payload), encoding="utf-8")
        elif action == "delete-manifest-file":
            manifest_path.unlink()
        elif action == "corrupt-manifest-file":
            manifest_path.write_text("{not valid json", encoding="utf-8")
        elif action == "strip-manifest-top-level-key":
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            del document["producer"]
            self._resync_manifest(root, ids, cycle_path, document)
        elif action == "tamper-manifest-content-only":
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["campaign"]["title"] = "tampered-without-resync"
            manifest_path.write_text(json.dumps(document), encoding="utf-8")
        elif action == "resync-repository-id-mismatch":
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["repository_id"] = self._foreign_id("repository", "s13")
            self._resync_manifest(root, ids, cycle_path, document)
        elif action == "resync-drop-artifact-row":
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["artifacts"] = []
            self._resync_manifest(root, ids, cycle_path, document)
        elif action == "resync-drop-artifact-revision-row":
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["artifact_revisions"] = []
            self._resync_manifest(root, ids, cycle_path, document)
        else:
            raise AssertionError("unknown lineage mutation action: {0!r}".format(action))

        return r.build_v3(completed_at="2026-08-11T00:00:00Z", **ids)

    def _resync_manifest(self, root, ids, cycle_path, document):
        manifest_path = Path(root) / cycle_path / "manifest.json"
        manifest_path.write_text(json.dumps(document), encoding="utf-8")
        new_digest = m.manifest_digest(document)
        index_path = Path(root) / adm.ADMISSION_REL / "index.json"
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        payload["cycles"][ids["cycle_id"]]["manifest_digest"] = new_digest
        for row in payload["manifests"].values():
            if row.get("manifest_id") == ids["manifest_id"] and row.get("cycle_id") == ids["cycle_id"]:
                row["manifest_digest"] = new_digest
        index_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_lineage_case_corpus(self):
        cases = json.loads((_FIXTURES / "lineage-cases.json").read_text(encoding="utf-8"))
        self.assertGreater(len(cases), 0)
        for case in cases:
            with self.subTest(name=case["name"]):
                root, ids, cycle_path, _alloc, _identity, _doc = _admit_fixture_root(
                    seed=("lineage-" + case["name"]).encode("utf-8")
                )
                second_alloc = idm.IdAllocator()  # real entropy: avoids FixedEntropy period collisions
                second_doc, second_ids = _document(_identity_for(root), second_alloc)
                staging2 = _stage()
                second_outcome = adm.admit(
                    root,
                    adm.AdmissionRequest(
                        idempotency_key=second_doc["manifest_id"],
                        document=second_doc,
                        staging_source=staging2,
                        allocator=second_alloc,
                    ),
                )
                self.assertEqual(second_outcome.status, "admitted")
                fixture = {
                    "root": root,
                    "ids": ids,
                    "cycle_path": cycle_path,
                    "second_ids": second_ids,
                }
                receipt = self._apply_lineage_mutation(fixture, case["action"])
                verdict = r.resolve(root, receipt)
                self.assertEqual(verdict.state, case["expected_state"], case["name"])
                self.assertEqual(verdict.reason, case["expected_reason"], case["name"])


def _identity_for(root):
    identity = lc.read_root_identity(Path(root))
    if identity is None:
        raise AssertionError("expected root identity to already be issued")
    return identity


class ReadOnlyTests(unittest.TestCase):
    def _fingerprint(self, root):
        out = {}
        for path in Path(root).rglob("*"):
            if path.is_file():
                out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out

    def test_resolve_never_writes_to_root(self):
        root, ids, _cycle_path, _alloc, _identity, _doc = _admit_fixture_root(seed=b"read-only-1")
        receipt = _v3_receipt(ids)
        before = self._fingerprint(root)
        r.resolve(root, receipt)
        after = self._fingerprint(root)
        self.assertEqual(before, after)

    def test_missing_index_is_unregistered_not_rebuild(self):
        root = tempfile.mkdtemp(prefix="artifact-receipt-noindex-")
        receipt = r.build_v3(
            completed_at="2026-08-11T00:00:00Z",
            repository_id="repo_" + "1" * 32,
            campaign_id="camp_" + "2" * 32,
            cycle_id="cyc_" + "3" * 32,
            artifact_id="art_" + "4" * 32,
            artifact_revision_id="arev_" + "5" * 32,
            manifest_id="man_" + "6" * 32,
            manifest_revision_id="mrev_" + "7" * 32,
        )
        adm.ensure_root_identity(root, allocator=idm.IdAllocator(idm.FixedEntropy(b"noindex")))
        # override repository_id to match the freshly issued identity so S1/S2 pass through to S3.
        identity = lc.read_root_identity(Path(root))
        receipt = dict(receipt, repository_id=identity.repository_id)
        verdict = r.resolve(root, receipt)
        self.assertEqual(verdict.reason, "local-manifest-unregistered")
        self.assertFalse((Path(root) / adm.ADMISSION_REL / "index.json").exists())

    def test_corrupt_index_is_unavailable(self):
        root, ids, _cycle_path, _alloc, _identity, _doc = _admit_fixture_root(seed=b"corrupt-index")
        index_path = Path(root) / adm.ADMISSION_REL / "index.json"
        index_path.write_text("{not valid json", encoding="utf-8")
        receipt = _v3_receipt(ids)
        verdict = r.resolve(root, receipt)
        self.assertEqual(verdict.state, "unavailable")
        self.assertEqual(verdict.reason, "local-state-unreadable")


class IdempotencyConflictTests(unittest.TestCase):
    def test_v2_identity_separator_injection_does_not_collide(self):
        receipt_a = json.loads(_V2_EXAMPLE.read_text(encoding="utf-8"))
        receipt_a.update(bundle_id="p", version="e\x1fv", entrypoint="index.html")
        receipt_b = dict(receipt_a, bundle_id="p\x1fe", version="v")
        self.assertNotEqual(r._identity_key(receipt_a), r._identity_key(receipt_b))

    def test_duplicate_exact_delivery_is_noop_v3(self):
        root, ids, _cycle_path, _alloc, _identity, _doc = _admit_fixture_root(seed=b"dup-v3")
        receipt = _v3_receipt(ids)
        first = r.register(root, receipt)
        records = Path(root) / r.RECEIPT_LEDGER_REL / "records"
        snapshot = {path.name: path.read_bytes() for path in records.glob("*.json")}
        self.assertEqual(len(snapshot), 1)
        second = r.register(root, receipt)
        self.assertEqual(first.state, "accepted")
        self.assertEqual(second.state, "noop-idempotent")
        self.assertEqual(snapshot, {path.name: path.read_bytes() for path in records.glob("*.json")})

    def test_duplicate_exact_delivery_is_noop_v2(self):
        root = tempfile.mkdtemp(prefix="artifact-receipt-dup-v2-")
        receipt = json.loads(_V2_EXAMPLE.read_text(encoding="utf-8"))
        first = r.register(root, receipt)
        records = Path(root) / r.RECEIPT_LEDGER_REL / "records"
        snapshot = {path.name: path.read_bytes() for path in records.glob("*.json")}
        self.assertEqual(len(snapshot), 1)
        second = r.register(root, receipt)
        self.assertEqual(first.state, "accepted")
        self.assertEqual(second.state, "noop-idempotent")
        self.assertEqual(snapshot, {path.name: path.read_bytes() for path in records.glob("*.json")})

    def test_duplicate_exact_delivery_is_noop_v1(self):
        root = tempfile.mkdtemp(prefix="artifact-receipt-dup-v1-")
        receipt = json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8"))
        first = r.register(root, receipt)
        records = Path(root) / r.RECEIPT_LEDGER_REL / "records"
        snapshot = {path.name: path.read_bytes() for path in records.glob("*.json")}
        self.assertEqual(len(snapshot), 1)
        second = r.register(root, receipt)
        self.assertEqual(first.state, "accepted")
        self.assertEqual(second.state, "noop-idempotent")
        self.assertEqual(snapshot, {path.name: path.read_bytes() for path in records.glob("*.json")})

    def test_conflicting_identity_reuse_rejected_v3(self):
        root, ids, _cycle_path, _alloc, _identity, _doc = _admit_fixture_root(seed=b"conflict-v3")
        receipt = _v3_receipt(ids, completed_at="2026-08-11T00:00:00Z")
        r.register(root, receipt)
        conflicting = _v3_receipt(ids, completed_at="2026-08-11T00:00:01Z")
        verdict = r.register(root, conflicting)
        self.assertEqual(verdict.state, "rejected")
        self.assertEqual(verdict.reason, "identity-conflict")

    def test_conflicting_identity_reuse_rejected_v2(self):
        root = tempfile.mkdtemp(prefix="artifact-receipt-conflict-v2-")
        base = json.loads(_V2_EXAMPLE.read_text(encoding="utf-8"))
        r.register(root, base)
        conflicting = dict(base, completed_at="2026-08-11T00:00:01Z")
        verdict = r.register(root, conflicting)
        self.assertEqual(verdict.state, "rejected")
        self.assertEqual(verdict.reason, "identity-conflict")

    def test_v1_has_no_identity_conflict(self):
        root = tempfile.mkdtemp(prefix="artifact-receipt-v1-noconflict-")
        base = json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8"))
        first = r.register(root, base)
        different = dict(base, completed_at="2026-08-11T00:00:01Z")
        second = r.register(root, different)
        self.assertEqual(first.state, "accepted")
        self.assertEqual(second.state, "accepted")
        records_dir = Path(root) / r.RECEIPT_LEDGER_REL / "records"
        files = list(records_dir.glob("*.json"))
        self.assertEqual(len(files), 2)
        parsed = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        self.assertEqual(len({item["receipt_digest"] for item in parsed}), 2)
        self.assertEqual(len({path.name for path in files}), 2)


class LedgerBoundaryTests(unittest.TestCase):
    def test_concurrent_register_is_idempotent_and_cleans_temps(self):
        """A race is probabilistic, so one round can miss the regression.

        Each round uses a fresh root; three rounds keep the detection rate of
        the pre-fix TOCTOU near one while staying well under a tenth of a
        second in total.
        """

        receipt = json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8"))
        context = multiprocessing.get_context("fork")
        for round_index in range(3):
            with self.subTest(round=round_index):
                root = tempfile.mkdtemp(prefix="artifact-receipt-concurrent-")
                with context.Pool(8) as pool:
                    verdicts = pool.starmap(r.register, [(root, receipt)] * 8)
                states = [verdict.state for verdict in verdicts]
                self.assertEqual(states.count("error"), 0)
                self.assertEqual(states.count("accepted"), 1)
                self.assertEqual(states.count("noop-idempotent"), 7)
                records_dir = Path(root) / r.RECEIPT_LEDGER_REL / "records"
                self.assertEqual(list(records_dir.glob(".*.tmp")), [])
                self.assertEqual(len(list(records_dir.glob("*.json"))), 1)

    def test_ledger_lives_outside_admission_dir(self):
        root, ids, _cycle_path, _alloc, _identity, _doc = _admit_fixture_root(seed=b"boundary-1")
        admission_dir = Path(root) / adm.ADMISSION_REL
        before = {
            str(p.relative_to(admission_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in admission_dir.rglob("*")
            if p.is_file()
        }
        r.register(root, _v3_receipt(ids))
        after = {
            str(p.relative_to(admission_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in admission_dir.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)
        self.assertTrue((Path(root) / r.RECEIPT_LEDGER_REL).is_dir())
        self.assertNotIn(r.RECEIPT_LEDGER_REL, str(admission_dir))

    def test_ledger_record_is_0600_and_write_once(self):
        root = tempfile.mkdtemp(prefix="artifact-receipt-ledger-mode-")
        receipt = json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8"))
        r.register(root, receipt)
        records_dir = Path(root) / r.RECEIPT_LEDGER_REL / "records"
        files = list(records_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        mode = files[0].stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        before = files[0].read_bytes()
        duplicate = dict(receipt, event="artifact.completed")  # v1: identity == digest, so conflict is impossible
        r.register(root, duplicate)
        after = files[0].read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(list(records_dir.glob(".*.tmp")), [])
        # OD-15: 0700 on both ledger directories is an owner-sealed invariant,
        # so it is asserted here rather than left to a silent chmod.
        ledger_root = Path(root) / r.RECEIPT_LEDGER_REL
        self.assertEqual(records_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(ledger_root.stat().st_mode & 0o777, 0o700)

    def test_register_unwritable_ledger_returns_typed_error(self):
        root = Path(tempfile.mkdtemp(prefix="artifact-receipt-read-only-"))
        (root / ".runtime").write_text("read-only placeholder", encoding="utf-8")
        receipt = json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8"))
        verdict = r.register(root, receipt)
        self.assertEqual(verdict.state, "error")
        self.assertEqual(verdict.reason, "local-state-unreadable")
        self.assertEqual(verdict.exit_code(), r.EXIT_INTERNAL)

    def test_register_ledger_chmod_failure_is_typed_error(self):
        root = tempfile.mkdtemp(prefix="artifact-receipt-chmod-fail-")
        receipt = json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8"))
        with mock.patch.object(r.os, "chmod", side_effect=PermissionError(13, "denied")):
            verdict = r.register(root, receipt)
        self.assertEqual(verdict.state, "error")
        self.assertEqual(verdict.reason, "local-state-unreadable")
        self.assertEqual(verdict.exit_code(), r.EXIT_INTERNAL)

    def test_register_ledger_dir_fsync_failure_is_typed_error(self):
        root = tempfile.mkdtemp(prefix="artifact-receipt-fsync-fail-")
        receipt = json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8"))
        real_fsync = r.os.fsync
        records_dir = Path(root) / r.RECEIPT_LEDGER_REL / "records"

        def only_dir_fsync_fails(fd):
            if os.path.samestat(os.fstat(fd), os.stat(records_dir)):
                raise OSError(5, "input/output error")
            return real_fsync(fd)

        with mock.patch.object(r.os, "fsync", side_effect=only_dir_fsync_fails):
            verdict = r.register(root, receipt)
        self.assertEqual(verdict.state, "error")
        self.assertEqual(verdict.reason, "local-state-unreadable")
        self.assertEqual(verdict.exit_code(), r.EXIT_INTERNAL)

    def test_register_unreadable_existing_record_is_typed_error(self):
        root = tempfile.mkdtemp(prefix="artifact-receipt-read-fail-")
        receipt = json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(r.register(root, receipt).state, "accepted")
        records_dir = Path(root) / r.RECEIPT_LEDGER_REL / "records"
        record = next(iter(records_dir.glob("*.json")))
        record.write_text("{not valid json", encoding="utf-8")
        verdict = r.register(root, receipt)
        self.assertEqual(verdict.state, "error")
        self.assertEqual(verdict.reason, "local-state-unreadable")
        self.assertEqual(verdict.exit_code(), r.EXIT_INTERNAL)

    def test_ledger_read_and_write_failures_share_one_publication_word(self):
        """OD-14: one failure class must not split into `skipped` and `failed`."""

        write_root = Path(tempfile.mkdtemp(prefix="artifact-receipt-word-write-"))
        (write_root / ".runtime").write_text("read-only placeholder", encoding="utf-8")
        receipt = json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8"))
        write_verdict = r.register(write_root, receipt)

        read_root = tempfile.mkdtemp(prefix="artifact-receipt-word-read-")
        self.assertEqual(r.register(read_root, receipt).state, "accepted")
        record = next(iter((Path(read_root) / r.RECEIPT_LEDGER_REL / "records").glob("*.json")))
        record.write_text("{not valid json", encoding="utf-8")
        read_verdict = r.register(read_root, receipt)

        self.assertEqual(write_verdict.reason, read_verdict.reason)
        self.assertEqual(write_verdict.exit_code(), read_verdict.exit_code())
        self.assertEqual(
            r.publication_result_for_sink_exit(write_verdict.exit_code()),
            r.publication_result_for_sink_exit(read_verdict.exit_code()),
        )


class MiscTests(unittest.TestCase):
    def test_publication_result_mapping_is_closed(self):
        words = {r.publication_result_for_sink_exit(code) for code in (None, 69, 0, 1, 64, 70)}
        self.assertLessEqual(words, set(lc.PUBLICATION_RESULTS))
        self.assertEqual(words, {"not-offered", "skipped", "succeeded", "failed"})

    def test_cli_decode_reports_typed_reason(self):
        good = subprocess.run(
            [sys.executable, str(_HERE / "artifact_receipt.py"), "--decode", str(_FIXTURES / "golden.v1.json")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(good.returncode, 0)
        self.assertIn("state=accepted", good.stdout)

        bad_dir = tempfile.mkdtemp(prefix="artifact-receipt-cli-")
        bad_path = Path(bad_dir) / "bad.json"
        bad_payload = dict(
            json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8")), status="pending"
        )
        bad_path.write_text(json.dumps(bad_payload), encoding="utf-8")
        bad = subprocess.run(
            [sys.executable, str(_HERE / "artifact_receipt.py"), "--decode", str(bad_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(bad.returncode, 64)
        self.assertIn("state=rejected", bad.stdout)
        self.assertIn("reason=value-invalid", bad.stdout)

    def test_cli_decode_lone_surrogate_is_typed_refusal_not_crash(self):
        """D-12: a decoder refuses; it does not escape the 0/64/69/70 vocabulary.

        A lone surrogate is legal JSON syntax but cannot be encoded as UTF-8,
        so the digest step used to raise `UnicodeEncodeError` and exit 1 with a
        traceback. R3 now screens it as `value-invalid` at the offending key.
        """

        payload = dict(
            json.loads((_FIXTURES / "golden.v1.json").read_text(encoding="utf-8")),
            source_path="/tmp/\ud800",
        )
        bad_path = Path(tempfile.mkdtemp(prefix="artifact-receipt-surrogate-")) / "bad.json"
        bad_path.write_text(json.dumps(payload), encoding="utf-8")

        verdict = r.decode(payload)
        self.assertEqual(verdict.state, "rejected")
        self.assertEqual(verdict.reason, "value-invalid")
        self.assertEqual(verdict.detail, "$.source_path")

        result = subprocess.run(
            [sys.executable, str(_HERE / "artifact_receipt.py"), "--decode", str(bad_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, r.EXIT_REFUSED, result.stderr)
        self.assertIn("state=rejected", result.stdout)
        self.assertIn("reason=value-invalid", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_emit_v3_unwritable_out_is_typed_error(self):
        """OD-14: the receipt file write stays inside the 0/64/69/70 vocabulary."""

        root, ids, _cycle_path, _alloc, _identity, _doc = _admit_fixture_root(seed=b"emit-out-fail")
        out_path = Path(tempfile.mkdtemp(prefix="artifact-receipt-out-")) / "missing" / "out.json"
        command = [
            sys.executable,
            str(_HERE / "artifact_receipt.py"),
            "--emit-v3",
            "--out",
            str(out_path),
            "--artifact-root",
            str(root),
            "--completed-at",
            "2026-08-11T00:00:00Z",
        ]
        for key, value in ids.items():
            command += ["--" + key.replace("_", "-"), value]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, r.EXIT_INTERNAL, result.stderr)
        self.assertIn("state=error", result.stdout)
        self.assertIn("reason=local-state-unreadable", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_key_tables_are_exact_and_disjointly_identified(self):
        self.assertEqual(len(r.V1_KEY_SET), 7)
        self.assertEqual(len(r.V2_KEY_SET), 7)
        self.assertEqual(len(r.V3_KEY_SET), 11)
        union = r.V1_KEY_SET | r.V2_KEY_SET | r.V3_KEY_SET
        foreign_v1 = union - r.V1_KEY_SET
        foreign_v2 = union - r.V2_KEY_SET
        foreign_v3 = union - r.V3_KEY_SET
        self.assertTrue(foreign_v1)
        self.assertTrue(foreign_v2)
        self.assertTrue(foreign_v3)
        # every foreign-field detection used by decode() is exactly this complement
        self.assertEqual(foreign_v2 & {"source_path", "bundle_id"}, {"source_path"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
