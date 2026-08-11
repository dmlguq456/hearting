from __future__ import annotations

"""Admission-layer contract tests (D-8 / D-9 / D-7 index authority).

Complements `artifact_admission_atomicity.test.py`, which owns the concurrency,
crash-recovery, and lock-contract surface. This module owns the single-process
admission contract: no-durable-output, idempotency, staging safety, append-only
refusal, index rebuild equivalence, and the non-scope invariants.

Every negative case asserts **zero commit** through `_root_fingerprint()`: a
recursive digest of the whole artifact root taken before and after the call.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm
import artifact_identity as idm
import artifact_index as idx
import artifact_manifest as m

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_artifact_root() -> Path:
    script = _REPO_ROOT / "utilities" / "artifact-root.sh"
    if script.is_file():
        try:
            out = subprocess.check_output(["bash", str(script)], cwd=str(_REPO_ROOT))
            resolved = out.decode("utf-8").strip()
            if resolved:
                return Path(resolved)
        except Exception:
            pass
    return _REPO_ROOT / ".agent_reports"


_ARTIFACT_ROOT = _resolve_artifact_root()


def _scratch_root():
    """Returns (path, is_local_fallback). Prefers the real artifact root."""
    env_root = os.environ.get("ARTIFACT_ADMISSION_TEST_ROOT")
    if env_root:
        Path(env_root).mkdir(parents=True, exist_ok=True)
        return Path(env_root), False

    if _ARTIFACT_ROOT.is_dir():
        selftest = _ARTIFACT_ROOT / ".runtime" / "_selftest" / "adm-{0}-{1}".format(
            os.getpid(), int(time.time() * 1000) % 1000000
        )
        try:
            selftest.mkdir(parents=True)
            return selftest, False
        except OSError:
            pass

    return Path(tempfile.mkdtemp(prefix="artifact-admission-contract-")), True


def _make_valid_document(alloc, identity, *, camp_id=None, cyc_id=None, content=b"hello"):
    """The A-1 positive shape, kept byte-compatible with the atomicity suite."""
    camp_id = camp_id or alloc.allocate("campaign")
    cyc_id = cyc_id or alloc.allocate("cycle")
    art_id = alloc.allocate("artifact")
    arev_id = alloc.allocate("artifact_revision")
    man_id = alloc.allocate("manifest")
    mrev_id = alloc.allocate("manifest_revision")
    prod_id = alloc.allocate("producer")
    evt_id = alloc.allocate("event")
    strm_id = alloc.allocate("stream")
    digest = m.digest_bytes(content)
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
            "input_digest": "sha256:" + "0" * 64,
            "outcome_criterion": {"required_artifact_roles": [], "decision_required": False},
            "state": "active",
        },
        "artifacts": [
            {
                "artifact_id": art_id,
                "cycle_id": cyc_id,
                "role": "primary",
                "type": "doc",
                "capability": "autopilot-code",
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
                "provenance": {
                    "source_manifest_id": man_id,
                    "source_revision_id": mrev_id,
                    "producer_route_id": "r",
                    "algorithm_version": "v1",
                    "schema_version": 1,
                    "source_digest": "sha256:" + "2" * 64,
                },
            }
        ],
        "shared_references": [],
        "shared_reference_revisions": [],
        "routes": [],
        "events": [
            {
                "event_id": evt_id,
                "stream_id": strm_id,
                "stream_sequence": 1,
                "event_type": "artifact.revision.recorded",
                "target_id": art_id,
                "actor": {"kind": "producer", "id": "p"},
                "recorded_at": "2026-08-11T00:00:00Z",
                "provenance": {
                    "source_manifest_id": man_id,
                    "source_revision_id": mrev_id,
                    "producer_route_id": "r",
                    "algorithm_version": "v1",
                    "schema_version": 1,
                    "source_digest": "sha256:" + "6" * 64,
                },
                "evidence_ids": [],
                "payload": {},
            }
        ],
        "producer": {
            "producer_id": prod_id,
            "contract_version": "artifact-cycle-manifest/v2",
            "source_revision": "abc",
        },
    }
    return doc, content


class AdmissionContractBase(unittest.TestCase):
    def setUp(self):
        self.scratch, self.is_local_fallback = _scratch_root()
        self.root = self.scratch / "root"
        self.root.mkdir(parents=True, exist_ok=True)
        self.alloc = idm.IdAllocator()

    def tearDown(self):
        shutil.rmtree(str(self.scratch), ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def _identity(self):
        return adm.ensure_root_identity(
            self.root, allocator=idm.IdAllocator(entropy=lambda n: b"\x22" * n)
        )

    def _stage_source(self, content: bytes) -> Path:
        src = self.scratch / "src-{0}".format(idm.IdAllocator().allocate("evidence"))
        src.mkdir()
        (src / "plan.md").write_bytes(content)
        return src

    def _root_fingerprint(self) -> str:
        """Recursive digest of every entry under the root: name, type, bytes."""
        h = hashlib.sha256()
        for dirpath, dirnames, filenames in os.walk(str(self.root)):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, str(self.root)).replace(os.sep, "/")
            h.update(("D:" + rel_dir + "\n").encode("utf-8"))
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, str(self.root)).replace(os.sep, "/")
                if os.path.islink(full):
                    h.update(("L:" + rel + "\n").encode("utf-8"))
                    h.update(os.readlink(full).encode("utf-8"))
                    continue
                h.update(("F:" + rel + "\n").encode("utf-8"))
                with open(full, "rb") as fh:
                    h.update(fh.read())
        return h.hexdigest()

    def _admit(self, doc, src, key, allocator=None):
        request = adm.AdmissionRequest(
            idempotency_key=key,
            document=doc,
            staging_source=src,
            allocator=allocator,
        )
        return adm.admit(self.root, request)

    def _admit_valid(self, key="k-1", content=b"hello"):
        identity = self._identity()
        doc, content = _make_valid_document(self.alloc, identity, content=content)
        src = self._stage_source(content)
        outcome = self._admit(doc, src, key)
        self.assertEqual(outcome.status, "admitted", outcome.to_payload())
        return doc, outcome

    def _assert_rejected_and_unchanged(self, doc, src, key, *, expect_code=None):
        before = self._root_fingerprint()
        outcome = self._admit(doc, src, key)
        after = self._root_fingerprint()
        self.assertEqual(outcome.status, "rejected", outcome.to_payload())
        self.assertEqual(before, after, "rejected admission mutated the artifact root")
        if expect_code is not None:
            codes = [v.code for v in outcome.violations]
            self.assertIn(expect_code, codes, codes)
        return outcome


# ---------------------------------------------------------------------------
# D-9 -- no durable output means no lineage
# ---------------------------------------------------------------------------


class TestNoDurableOutput(AdmissionContractBase):
    def test_no_durable_output_creates_nothing_and_touches_no_file(self):
        before = self._root_fingerprint()
        outcome = adm.admit(self.root, adm.AdmissionRequest(idempotency_key="k-none"))
        after = self._root_fingerprint()

        self.assertEqual(outcome.status, "no-lineage")
        self.assertIsNone(outcome.cycle_path)
        self.assertIsNone(outcome.manifest_digest)
        self.assertFalse(outcome.index_changed)
        self.assertEqual(before, after)
        # D-9: zero campaign / cycle / manifest / folder, and no runtime state
        # bootstrapped either -- not even the lock, journal, or root identity.
        self.assertFalse((self.root / "campaigns").exists())
        self.assertFalse((self.root / adm.ADMISSION_REL).exists())


# ---------------------------------------------------------------------------
# D-8 -- idempotency and determinism
# ---------------------------------------------------------------------------


class TestIdempotencyAndDeterminism(AdmissionContractBase):
    def test_second_admission_with_same_key_and_digest_is_noop(self):
        doc, first = self._admit_valid(key="k-same")
        src = self._stage_source(b"hello")

        before = self._root_fingerprint()
        second = self._admit(doc, src, "k-same")
        after = self._root_fingerprint()

        self.assertEqual(second.status, "noop-idempotent", second.to_payload())
        self.assertEqual(second.manifest_digest, first.manifest_digest)
        self.assertFalse(second.index_changed)
        self.assertEqual(before, after, "idempotent retry must not mutate the root")

    def test_canonical_manifest_bytes_identical_across_two_admissions(self):
        doc, outcome = self._admit_valid(key="k-bytes")
        published = self.root / outcome.cycle_path / "manifest.json"
        first_bytes = published.read_bytes()

        # the canonical serializer is the single source of the digested bytes
        self.assertEqual(first_bytes, m.canonical_bytes(doc))

        src = self._stage_source(b"hello")
        second = self._admit(doc, src, "k-bytes")
        self.assertEqual(second.status, "noop-idempotent")
        self.assertEqual(published.read_bytes(), first_bytes)

    def test_conflicting_retry_same_identity_different_digest_rejects(self):
        doc, _ = self._admit_valid(key="k-conflict")

        mutated = json.loads(json.dumps(doc))
        mutated["campaign"]["title"] = "a different title"
        src = self._stage_source(b"hello")

        self._assert_rejected_and_unchanged(mutated, src, "k-conflict")


# ---------------------------------------------------------------------------
# D-11 -- append-only refusal on re-admission
# ---------------------------------------------------------------------------


class TestAppendOnlyRefusal(AdmissionContractBase):
    def test_readmit_with_mutated_event_payload_rejects_and_changes_nothing(self):
        doc, _ = self._admit_valid(key="k-evt-mut")

        mutated = json.loads(json.dumps(doc))
        mutated["events"][0]["payload"] = {"tampered": True}
        src = self._stage_source(b"hello")

        self._assert_rejected_and_unchanged(mutated, src, "k-evt-mut")

    def test_readmit_with_deleted_event_rejects(self):
        doc, _ = self._admit_valid(key="k-evt-del")

        stripped = json.loads(json.dumps(doc))
        stripped["events"] = []
        src = self._stage_source(b"hello")

        self._assert_rejected_and_unchanged(stripped, src, "k-evt-del")


# ---------------------------------------------------------------------------
# lineage and root identity
# ---------------------------------------------------------------------------


class TestLineageAndRootIdentity(AdmissionContractBase):
    def test_rejects_partial_lineage_missing_first_revision(self):
        identity = self._identity()
        doc, content = _make_valid_document(self.alloc, identity)
        doc["artifact_revisions"] = []
        src = self._stage_source(content)

        self._assert_rejected_and_unchanged(doc, src, "k-partial")

    def test_rejects_manifest_revision_append_out_of_scope(self):
        doc, _ = self._admit_valid(key="k-mrev")

        appended = json.loads(json.dumps(doc))
        appended["manifest_revision_id"] = self.alloc.allocate("manifest_revision")
        appended["cycle"]["cycle_id"] = self.alloc.allocate("cycle")
        appended["artifacts"][0]["cycle_id"] = appended["cycle"]["cycle_id"]
        src = self._stage_source(b"hello")

        self._assert_rejected_and_unchanged(appended, src, "k-mrev-2")

    def test_rejects_root_identity_mismatch(self):
        identity = self._identity()
        doc, content = _make_valid_document(self.alloc, identity)
        doc["artifact_root_id"] = idm.IdAllocator().allocate("artifact_root")
        src = self._stage_source(content)

        self._assert_rejected_and_unchanged(
            doc, src, "k-rootid", expect_code="index-root-identity-mismatch"
        )

    def test_root_identity_is_stable_after_root_relocation(self):
        first = self._identity()

        moved = self.scratch / "relocated-root"
        shutil.move(str(self.root), str(moved))
        second = adm.ensure_root_identity(moved)

        self.assertEqual(second.artifact_root_id, first.artifact_root_id)
        self.assertEqual(second.repository_id, first.repository_id)
        # restore so tearDown and any later assertions see a consistent tree
        shutil.move(str(moved), str(self.root))


# ---------------------------------------------------------------------------
# D-7 -- the index is derived and rebuildable
# ---------------------------------------------------------------------------


class TestIndexAuthority(AdmissionContractBase):
    def test_rebuild_index_matches_incremental_bytes(self):
        identity = self._identity()
        for n, key in enumerate(("k-idx-1", "k-idx-2")):
            doc, content = _make_valid_document(
                self.alloc, identity, content=b"payload-%d" % n
            )
            src = self._stage_source(content)
            outcome = self._admit(doc, src, key)
            self.assertEqual(outcome.status, "admitted", outcome.to_payload())

        incremental = idx.canonical_bytes(adm.load_index(self.root))
        rebuilt = idx.canonical_bytes(adm.rebuild_index(self.root))
        self.assertEqual(incremental, rebuilt)

    def test_verify_index_detects_hand_edit(self):
        self._admit_valid(key="k-verify")
        self.assertTrue(adm.verify_index(self.root).ok)

        index_path = self.root / adm.ADMISSION_REL / "index.json"
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        payload["stable_ids"]["art_handedited"] = "tampered"
        index_path.write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )

        report = adm.verify_index(self.root)
        self.assertFalse(report.ok, report.to_payload())


# ---------------------------------------------------------------------------
# staging safety (D-8 step 2 -- locators, digests, completeness)
# ---------------------------------------------------------------------------


class TestStagingSafety(AdmissionContractBase):
    def _doc_and_src(self, content=b"hello"):
        identity = self._identity()
        doc, content = _make_valid_document(self.alloc, identity, content=content)
        return doc, self._stage_source(content)

    def test_rejects_symlink_in_staging(self):
        doc, src = self._doc_and_src()
        target = self.scratch / "outside.txt"
        target.write_bytes(b"outside")
        os.symlink(str(target), str(src / "link.md"))

        self._assert_rejected_and_unchanged(
            doc, src, "k-symlink", expect_code="staging-symlink-forbidden"
        )

    def test_rejects_non_regular_file_in_staging(self):
        doc, src = self._doc_and_src()
        fifo = src / "pipe"
        try:
            os.mkfifo(str(fifo))
        except (AttributeError, OSError):
            self.skipTest("os.mkfifo is unavailable on this platform/filesystem")

        self._assert_rejected_and_unchanged(
            doc, src, "k-fifo", expect_code="staging-non-regular-file-forbidden"
        )

    def test_rejects_digest_mismatch(self):
        doc, src = self._doc_and_src()
        (src / "plan.md").write_bytes(b"hello-tampered-same-length"[: len(b"hello")])
        (src / "plan.md").write_bytes(b"HELLO")  # same byte size, different digest

        self._assert_rejected_and_unchanged(
            doc, src, "k-digest", expect_code="staging-digest-mismatch"
        )

    def test_rejects_byte_size_mismatch(self):
        doc, src = self._doc_and_src()
        (src / "plan.md").write_bytes(b"hello-and-then-some-more")

        self._assert_rejected_and_unchanged(
            doc, src, "k-size", expect_code="staging-byte-size-mismatch"
        )

    def test_rejects_missing_declared_file(self):
        doc, src = self._doc_and_src()
        (src / "plan.md").unlink()

        self._assert_rejected_and_unchanged(
            doc, src, "k-missing", expect_code="staging-missing-declared-file"
        )

    def test_rejects_undeclared_extra_file(self):
        doc, src = self._doc_and_src()
        (src / "stowaway.md").write_bytes(b"not declared by any locator")

        self._assert_rejected_and_unchanged(
            doc, src, "k-extra", expect_code="staging-undeclared-extra-file"
        )


# ---------------------------------------------------------------------------
# F-1 defence (graft G-2) and the non-scope invariants
# ---------------------------------------------------------------------------


class TestPublishNoReplace(AdmissionContractBase):
    def test_admit_does_not_replace_existing_empty_canonical_directory(self):
        """Graft G-2 -- the admission layer's own defence against owner fact F-1.

        F-1: on this artifact root a bare `os.rename` silently REPLACES an
        existing empty directory, and `renameat2(RENAME_NOREPLACE)` is EINVAL.
        `test_environment_records_filesystem_facts` only records that raw OS
        behaviour; this test asserts that `admit()` itself refuses to publish
        over an already-present empty canonical cycle directory, and leaves a
        marker file inside it untouched.
        """
        identity = self._identity()
        doc, content = _make_valid_document(self.alloc, identity)
        camp_id = doc["campaign"]["campaign_id"]
        cyc_id = doc["cycle"]["cycle_id"]

        squatter = self.root / "campaigns" / camp_id / "cycles" / cyc_id
        squatter.mkdir(parents=True)
        self.assertEqual(sorted(os.listdir(str(squatter))), [])

        src = self._stage_source(content)
        outcome = self._admit(doc, src, "k-squat")

        self.assertNotEqual(
            outcome.status, "admitted", "admit() published over an existing empty canonical dir"
        )
        # the pre-existing directory is still the empty one we created: the
        # staged manifest never landed on top of it
        self.assertTrue(squatter.is_dir())
        self.assertFalse((squatter / "manifest.json").exists())
        self.assertFalse((squatter / "plan.md").exists())


class TestNonScopeInvariants(AdmissionContractBase):
    def test_admit_never_writes_under_runtime_routes(self):
        routes_dir = self.root / ".runtime" / "routes"
        routes_dir.mkdir(parents=True)
        (routes_dir / "rt-existing.json").write_bytes(b"{}")
        before = sorted(os.listdir(str(routes_dir)))

        self._admit_valid(key="k-routes")

        self.assertEqual(sorted(os.listdir(str(routes_dir))), before)
        self.assertEqual((routes_dir / "rt-existing.json").read_bytes(), b"{}")

    def test_admit_creates_no_symlink(self):
        self._admit_valid(key="k-nosymlink")

        found = []
        for dirpath, dirnames, filenames in os.walk(str(self.root)):
            for name in list(dirnames) + list(filenames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    found.append(os.path.relpath(full, str(self.root)))
        self.assertEqual(found, [], "admission created symlinks: {0}".format(found))


class TestReviewFindingRegressions(AdmissionContractBase):
    """Named regressions for the 2026-08-11 independent-review findings."""

    def test_noop_idempotent_requires_existing_cycle_folder(self):
        # F1b: a forged/drifted index row must not fake an admission.
        doc, outcome = self._admit_valid(key="k-noop-folder")
        cycle_dir = self.root / outcome.cycle_path
        shutil.rmtree(str(self.root / "campaigns"))
        src = self._stage_source(b"hello")
        retry = self._admit(doc, src, "k-noop-folder")
        self.assertEqual(retry.status, "rejected", retry.to_payload())
        self.assertIn(
            "index-idempotent-target-missing", [v.code for v in retry.violations]
        )

    def test_rejects_foreign_repository_id(self):
        # F3a: repository identity is part of admission tenancy (D-4).
        identity = self._identity()
        doc, content = _make_valid_document(self.alloc, identity)
        doc["repository_id"] = "repo_" + "f" * 32
        src = self._stage_source(content)
        self._assert_rejected_and_unchanged(
            doc, src, "k-foreign-repo", expect_code="index-repository-identity-mismatch"
        )

    def test_second_cycle_in_same_campaign_admits(self):
        # F2b: one campaign owns many cycles (D-1); the second admission of the
        # same campaign must not be a duplicate-stable-id rejection.
        identity = self._identity()
        doc1, content1 = _make_valid_document(self.alloc, identity)
        src1 = self._stage_source(content1)
        out1 = self._admit(doc1, src1, "k-camp-c1")
        self.assertEqual(out1.status, "admitted", out1.to_payload())
        doc2, content2 = _make_valid_document(
            self.alloc, identity, camp_id=doc1["campaign"]["campaign_id"]
        )
        doc2["cycle"]["parent_cycle_id"] = doc1["cycle"]["cycle_id"]
        src2 = self._stage_source(content2)
        out2 = self._admit(doc2, src2, "k-camp-c2")
        self.assertEqual(out2.status, "admitted", out2.to_payload())

    def test_rejects_orphan_parent_cycle(self):
        # F2a: A-1 orphan-parent refusal at the admission surface.
        identity = self._identity()
        doc, content = _make_valid_document(self.alloc, identity)
        doc["cycle"]["parent_cycle_id"] = self.alloc.allocate("cycle")
        src = self._stage_source(content)
        self._assert_rejected_and_unchanged(
            doc, src, "k-orphan-parent", expect_code="index-orphan-parent-cycle"
        )

    def test_recover_waits_for_admission_lock(self):
        # F6: public recover() takes the same mutex as admit() so it cannot
        # quarantine a live admission's staging.
        self._admit_valid(key="k-recover-lock")
        fd = adm._acquire_lock(self.root, timeout=5.0)
        try:
            with self.assertRaises(adm.AdmissionBusy):
                adm.recover(self.root, lock_timeout=0.3)
        finally:
            adm._release_lock(self.root, fd)

    def test_rollforward_verifies_declared_payload(self):
        # F7ii: recovery roll-forward must verify declared file digests, not
        # only manifest.json.
        doc, outcome = self._admit_valid(key="k-rollforward")
        cycle_dir = self.root / outcome.cycle_path
        (cycle_dir / "plan.md").write_bytes(b"corrupted-payload")
        rel_target = os.path.relpath(str(cycle_dir), str(self.root))
        adm._write_journal(
            self.root,
            "k-rollforward",
            state="published",
            publish_target=rel_target,
            staging_path=".admitting-gone",
            manifest_digest=outcome.manifest_digest,
            cycle_relative="",
        )
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            adm.recover(self.root)

    def test_verify_index_passes_for_multi_cycle_campaign(self):
        # Round-2 regression: incremental admission order vs lexical rebuild
        # order must produce byte-identical indexes for shared entities.
        identity = self._identity()
        doc1, content1 = _make_valid_document(self.alloc, identity)
        out1 = self._admit(doc1, self._stage_source(content1), "k-mc-1")
        self.assertEqual(out1.status, "admitted", out1.to_payload())
        doc2, content2 = _make_valid_document(
            self.alloc, identity, camp_id=doc1["campaign"]["campaign_id"]
        )
        out2 = self._admit(doc2, self._stage_source(content2), "k-mc-2")
        self.assertEqual(out2.status, "admitted", out2.to_payload())
        report = adm.verify_index(self.root)
        self.assertTrue(report.ok, [v.to_payload() for v in report.violations])

    def test_noop_idempotent_rejects_foreign_repository_even_with_index_row(self):
        # Round-2: repository tenancy is checked before the idempotent verdict.
        doc, outcome = self._admit_valid(key="k-tenancy-noop")
        forged = dict(doc)
        forged["repository_id"] = "repo_" + "e" * 32
        src = self._stage_source(b"hello")
        retry = self._admit(forged, src, "k-tenancy-noop")
        self.assertEqual(retry.status, "rejected", retry.to_payload())
        self.assertIn(
            "index-repository-identity-mismatch", [v.code for v in retry.violations]
        )

    def test_rebuild_records_manifest_id_fallback(self):
        # F8i: the manifest-id fallback is a recorded machine-readable fact.
        doc, outcome = self._admit_valid(key="custom-key-original")
        os.unlink(str(adm._index_path(self.root)))
        adm.rebuild_index(self.root)
        report = adm._read_json(adm._admission_dir(self.root) / "rebuild-report.json")
        self.assertIsNotNone(report)
        self.assertEqual(
            report["fallback_idempotency_keys"], [doc["manifest_id"]]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
