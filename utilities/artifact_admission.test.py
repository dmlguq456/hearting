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
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm
import artifact_identity as idm
import artifact_index as idx
import artifact_lifecycle as lifecycle
import artifact_locator
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
    """Returns an isolated default root, or the explicit caller override."""
    env_root = os.environ.get("ARTIFACT_ADMISSION_TEST_ROOT")
    if env_root:
        Path(env_root).mkdir(parents=True, exist_ok=True)
        return Path(env_root), False, None
    private = tempfile.TemporaryDirectory(prefix="artifact-admission-contract-")
    return Path(private.name), True, private


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
        self.scratch, self.is_local_fallback, self._scratch_temp = _scratch_root()
        self.root = self.scratch / "root"
        self.root.mkdir(parents=True, exist_ok=True)
        self.alloc = idm.IdAllocator()

    def tearDown(self):
        if self._scratch_temp is not None:
            self._scratch_temp.cleanup()
        else:
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
    def test_shared_classifier_corpus_matches_admission_payload_contract(self):
        _CORPUS_SPEC = importlib.util.spec_from_file_location(
            "shared_artifact_path_corpus_admission",
            Path(__file__).with_name("artifact_manifest.test.py"))
        corpus = importlib.util.module_from_spec(_CORPUS_SPEC)
        _CORPUS_SPEC.loader.exec_module(corpus)
        rows = [row for row in corpus.SHARED_PATH_CORPUS if row[0] in {"payload", "locator"}]
        for index, (namespace, relative, kind, _prospective, allowed, reason, public_reasons) in enumerate(rows):
            with self.subTest(namespace=namespace, path=relative), tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch) / "root"
                root.mkdir()
                identity = adm.ensure_root_identity(
                    root, allocator=idm.IdAllocator(entropy=lambda n: b"\x22" * n))
                data = b"corpus payload\n"
                doc, _ = _make_valid_document(self.alloc, identity, content=data)
                if namespace == "payload" and relative.startswith("campaigns/camp/cyc/"):
                    locator = relative[len("campaigns/camp/cyc/"):]
                else:
                    locator = relative
                doc["artifact_revisions"][0]["locator"]["path"] = locator
                source = Path(scratch) / "staging-source"
                target = source / locator
                target.parent.mkdir(parents=True, exist_ok=True)
                if kind == "symlink":
                    outside = Path(scratch) / "outside"
                    outside.write_bytes(data)
                    target.symlink_to(outside)
                else:
                    target.write_bytes(data)
                outcome = adm.admit(
                    root, adm.AdmissionRequest(
                        idempotency_key="corpus-{}".format(index), document=doc, staging_source=source,
                    ))
                self.assertEqual(outcome.status == "admitted", allowed, outcome.to_payload())
                if not allowed:
                    codes = [violation.code for violation in outcome.violations]
                    expected = public_reasons.get("admission", reason)
                    self.assertIn(expected, codes, (relative, codes))

    def test_nested_manifest_payload_is_admitted_and_preserved(self):
        identity = self._identity()
        data = b'{"ordinary":"admitted"}\n'
        doc, _ = _make_valid_document(self.alloc, identity, content=data)
        locator = "artifacts/_internal/candidate/round_1/manifest.json"
        doc["artifact_revisions"][0]["locator"]["path"] = locator
        source = self.scratch / "nested-manifest-source"
        payload = source / locator
        payload.parent.mkdir(parents=True)
        payload.write_bytes(data)
        outcome = self._admit(doc, source, "k-nested-manifest")
        self.assertEqual(outcome.status, "admitted", outcome.to_payload())
        published = self.root / outcome.cycle_path / locator
        self.assertEqual(published.read_bytes(), data)
        sealed = json.loads((self.root / outcome.cycle_path / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(m.validate(sealed).ok)

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
    def test_manifest_only_legacy_admission_is_resolved_by_stable_ids(self):
        document, outcome = self._admit_valid(key="k-manifest-only-locator")
        expected_cycle = self.root / outcome.cycle_path
        expected_campaign = expected_cycle.parent.parent
        self.assertFalse((expected_campaign / "campaign.json").exists())
        self.assertEqual(
            artifact_locator.find_path_by_id(self.root, document["campaign"]["campaign_id"]),
            expected_campaign,
        )
        self.assertEqual(
            artifact_locator.resolve_path(self.root, document["cycle"]["cycle_id"]),
            expected_cycle,
        )
        admitted = lifecycle.read_admitted_cycle(
            self.root, document["campaign"]["campaign_id"], document["cycle"]["cycle_id"])
        self.assertEqual(admitted["cycle_id"], document["cycle"]["cycle_id"])

    def test_rebuild_scans_readable_and_legacy_layouts_without_locator_cache_authority(self):
        identity = self._identity()
        legacy, legacy_content = _make_valid_document(self.alloc, identity, content=b"legacy")
        readable, readable_content = _make_valid_document(self.alloc, identity, content=b"readable")
        campaigns = self.root / "campaigns"
        legacy_dir = (
            campaigns / legacy["campaign"]["campaign_id"] / "cycles" /
            legacy["cycle"]["cycle_id"]
        )
        readable_campaign = campaigns / "2026-09-04_readable"
        readable_dir = readable_campaign / "2026-09-04_readable"
        for directory, document in ((legacy_dir, legacy), (readable_dir, readable)):
            directory.mkdir(parents=True)
            (directory / "manifest.json").write_bytes(
                m.canonical_bytes(document)
            )
        (campaigns / "INDEX.json").write_text(json.dumps({
            readable["cycle"]["cycle_id"]: "campaigns/hostile/cycles/hostile",
        }), encoding="utf-8")
        rebuilt = adm.rebuild_index(self.root)
        self.assertIn(legacy["cycle"]["cycle_id"], rebuilt.cycles)
        self.assertIn(readable["cycle"]["cycle_id"], rebuilt.cycles)

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

    def test_directory_bundle_coverage_joins_source_and_staged_inventory(self):
        import artifact_directory_bundle as bundles

        doc, base_src = self._doc_and_src()
        src = self.scratch / "bundle-source"
        src.mkdir()
        (src / "plan.md").write_bytes((base_src / "plan.md").read_bytes())
        raw = src / "artifacts" / "raw-results"
        raw.mkdir(parents=True)
        (raw / "hidden.tmp").write_bytes(b"covered")
        tree = bundles.scan_tree(src, "artifacts/raw-results")
        descriptor_path = "artifacts/_internal/directory-bundles/raw-results.json"
        descriptor = src / descriptor_path
        descriptor.parent.mkdir(parents=True)
        descriptor.write_bytes(tree.descriptor)
        bundle_id = "a_" + "b" * 32
        doc["artifacts"].append({"artifact_id": bundle_id, "cycle_id": doc["cycle"]["cycle_id"],
                                 "role": "output", "type": "directory-bundle",
                                 "capability": "autopilot-code", "title": "raw-results"})
        doc["artifact_revisions"].append({"artifact_revision_id": "ar_" + "c" * 32,
            "artifact_id": bundle_id, "revision_sequence": 1,
            "content_digest": tree.descriptor_digest, "byte_size": len(tree.descriptor),
            "media_type": bundles.MEDIA_TYPE,
            "locator": {"kind": "cycle-relative", "path": descriptor_path},
            "provenance": doc["artifact_revisions"][0]["provenance"]})

        covered, violations = adm._bundle_coverage(src, doc)
        self.assertEqual(covered, {"artifacts/raw-results/hidden.tmp"})
        self.assertEqual(violations, [])
        self.assertEqual(adm._verify_staging_source(src, doc), [])
        stage = self.scratch / "stage"
        published = self.scratch / "published"
        staged_cycle = adm._stage_tree(stage, published, published, doc, src)
        self.assertEqual((staged_cycle / "artifacts/raw-results/hidden.tmp").read_bytes(), b"covered")
        self.assertEqual(adm._verify_staged_files(staged_cycle, doc), [])
        doc["artifact_revisions"][-1]["content_digest"] = "sha256:" + "0" * 64
        coverage, violations = adm._bundle_coverage(src, doc)
        self.assertEqual(coverage, set())
        self.assertEqual(violations[0].code, "bundle-descriptor-revision-mismatch")
        source_codes = {row.code for row in adm._verify_staging_source(src, doc)}
        self.assertIn("staging-undeclared-extra-file", source_codes)
        doc["artifact_revisions"][-1]["content_digest"] = tree.descriptor_digest
        doc["artifact_revisions"][-1]["media_type"] = "application/json"
        doc["artifacts"][-1]["type"] = "doc"
        coverage, violations = adm._bundle_coverage(src, doc)
        self.assertEqual((coverage, violations), (set(), []))
        generic_codes = {row.code for row in adm._verify_staging_source(src, doc)}
        self.assertIn("staging-undeclared-extra-file", generic_codes)

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
        # Round-2/3: repository tenancy is checked before the idempotent
        # verdict. Changing repository_id also changes the manifest digest, so
        # reaching the no-op branch requires the forged-index scenario itself:
        # an index row whose stored digest matches the foreign document.
        doc, outcome = self._admit_valid(key="k-tenancy-noop")
        forged = dict(doc)
        forged["repository_id"] = "repo_" + "e" * 32
        forged_digest = m.manifest_digest(forged)
        index_path = adm._index_path(self.root)
        payload = json.loads(index_path.read_bytes().decode("utf-8"))
        payload["manifests"]["k-tenancy-noop"]["manifest_digest"] = forged_digest
        index_path.write_bytes(m.canonical_bytes(payload))
        src = self._stage_source(b"hello")
        retry = self._admit(forged, src, "k-tenancy-noop")
        self.assertEqual(retry.status, "rejected", retry.to_payload())
        self.assertIn(
            "index-repository-identity-mismatch", [v.code for v in retry.violations]
        )

    def test_rebuild_keeps_the_idempotency_key_after_a_cycle_directory_rename(self):
        # A locator amendment or a hand rename (D-89) moves the folder; the
        # sealed manifest still carries cycle_id, so the caller's key survives.
        doc, outcome = self._admit_valid(key="custom-key-after-rename")
        cycle_dir = self.root / outcome.cycle_path
        # Legacy admission publishes `campaigns/<camp>/cycles/<cyc>`; keep the
        # `cycles/` container and rename the campaign and cycle folders around it.
        container = cycle_dir.parent
        campaign_dir = container.parent if container.name == "cycles" else container
        renamed_campaign = campaign_dir.with_name(campaign_dir.name + "-renamed")
        os.rename(str(campaign_dir), str(renamed_campaign))
        moved_parent = renamed_campaign / "cycles" if container.name == "cycles" else renamed_campaign
        moved_cycle = moved_parent / (cycle_dir.name + "-moved")
        os.rename(str(moved_parent / cycle_dir.name), str(moved_cycle))
        rebuilt = adm.rebuild_index(self.root)
        report = adm._read_json(adm._admission_dir(self.root) / "rebuild-report.json")
        self.assertEqual(report["fallback_idempotency_keys"], [])
        self.assertIn("custom-key-after-rename", rebuilt.manifests)
        self.assertEqual(rebuilt.cycles[doc["cycle"]["cycle_id"]]["cycle_path"],
                         os.path.relpath(str(moved_cycle), str(self.root)))
        self.assertTrue(adm.verify_index(self.root).ok)

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


class TestIndexGenerationCache(AdmissionContractBase):
    """One index generation may be answered without re-reading its bytes."""

    def setUp(self):
        super().setUp()
        adm.clear_index_cache()
        self.addCleanup(adm.clear_index_cache)

    def _doc(self, marker="a"):
        root_id = self._identity().artifact_root_id
        cycle = "cyc_" + "c" * 32
        digest = "sha256:" + "d" * 64
        return idx.IndexDocument(
            schema_version=1, artifact_root_id=root_id,
            stable_ids={"camp_" + "e" * 32: {"kind": "campaign", "cycle_id": None,
                                             "manifest_id": None},
                        cycle: {"kind": "cycle", "cycle_id": cycle,
                                "manifest_id": None}},
            routes={root_id: {"rt_" + "f" * 32: {"cycle_id": cycle, "route_hash": None}}},
            event_ids={}, streams={},
            manifests={marker: {"manifest_id": "man_" + "1" * 32,
                                "manifest_revision_id": "mrev_" + "2" * 32,
                                "cycle_id": cycle, "manifest_digest": digest,
                                "idempotency_key": marker}},
            cycles={cycle: {"campaign_id": "camp_" + "e" * 32,
                            "cycle_path": "campaigns/a/cycles/b",
                            "manifest_digest": digest}},
        )

    def _settle(self):
        index = adm._index_path(self.root)
        stat = index.stat()
        os.utime(index, ns=(stat.st_atime_ns, time.time_ns() - 5_000_000_000))

    def _write_settled(self, doc):
        adm._write_index(self.root, doc)
        self._settle()
        adm.clear_index_cache()

    def test_settled_generation_is_parsed_once(self):
        self._write_settled(self._doc())
        with mock.patch.object(idx, "_parse_owned", wraps=idx._parse_owned) as parse:
            first = adm.load_index(self.root)
            second = adm.load_index(self.root)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(first.manifests, second.manifests)

    def test_hit_sections_are_isolated_from_caller_edits(self):
        self._write_settled(self._doc())
        first = adm.load_index(self.root)
        first.manifests["evil"] = dict(first.manifests["a"])
        first.stable_ids.clear()
        second = adm.load_index(self.root)
        self.assertNotIn("evil", second.manifests)
        self.assertTrue(second.stable_ids)

    def test_seed_hits_only_inside_the_lock_interval_that_wrote_it(self):
        doc = self._doc()
        fd = adm._acquire_lock(self.root, 5)
        try:
            adm._write_index(self.root, doc)
            with mock.patch.object(idx, "_parse_owned", wraps=idx._parse_owned) as parse:
                self.assertTrue(adm.load_index(self.root).manifests)
            self.assertEqual(parse.call_count, 0)
        finally:
            adm._release_lock(self.root, fd)
        adm.clear_index_cache()
        fd = adm._acquire_lock(self.root, 5)
        try:
            adm._write_index(self.root, doc)
        finally:
            adm._release_lock(self.root, fd)
        with mock.patch.object(idx, "_parse_owned", wraps=idx._parse_owned) as parse:
            self.assertTrue(adm.load_index(self.root).manifests)
        # A different lock interval, and the file is still inside the settle window.
        self.assertEqual(parse.call_count, 1)

    def test_editing_the_written_document_afterwards_does_not_poison_the_seed(self):
        doc = self._doc()
        fd = adm._acquire_lock(self.root, 5)
        try:
            adm._write_index(self.root, doc)
            doc.manifests["late"] = dict(doc.manifests["a"])
            doc.stable_ids.clear()
            loaded = adm.load_index(self.root)
        finally:
            adm._release_lock(self.root, fd)
        self.assertNotIn("late", loaded.manifests)
        self.assertTrue(loaded.stable_ids)

    def test_head_is_created_by_a_reader_and_a_stale_head_is_ignored(self):
        doc = self._doc()
        self._write_settled(doc)
        head_path = adm._index_head_path(self.root)
        if head_path.exists():
            head_path.unlink()
        adm.load_index(self.root)
        self.assertTrue(head_path.exists())
        head = adm.load_index_head(self.root)
        self.assertIsNotNone(head)
        self.assertTrue(head.has_prefixed_stable_id("camp_" + "e" * 32))
        self.assertTrue(head.has_prefixed_stable_id("cyc_" + "c" * 32))
        self.assertFalse(head.has_prefixed_stable_id("art_" + "0" * 32))
        old_key = list(head.key)
        # A writer replaces the index without a head (an older release, or a
        # crash between the two writes): the stale head must be ignored.
        replacement = self._doc(marker="b")
        adm._atomic_write_bytes(adm._index_path(self.root), idx.canonical_bytes(replacement))
        self._settle()
        self.assertIsNone(adm.load_index_head(self.root))
        # A racing reader can rewrite a head for the generation it parsed; a
        # head whose key is not the current index key is still ignored.
        adm._write_index_head(self.root, doc, old_key)
        self.assertIsNone(adm.load_index_head(self.root))
        current_key = adm._file_key(adm._index_path(self.root))
        adm._write_index_head(self.root, replacement, current_key)
        self.assertIsNotNone(adm.load_index_head(self.root))

    def test_corrupt_head_falls_back_and_the_next_reader_recovers_it(self):
        self._write_settled(self._doc())
        head_path = adm._index_head_path(self.root)
        head_path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(adm.load_index_head(self.root))
        loaded = adm.load_index(self.root)
        self.assertIn("a", loaded.manifests)
        self.assertIsNotNone(adm.load_index_head(self.root))

    def test_verify_index_fast_path_accepts_canonical_non_canonical_and_rejects_drift(self):
        self._admit_valid(key="k-generation-cache")
        report = adm.verify_index(self.root)
        self.assertTrue(report.ok, report)
        index_path = adm._index_path(self.root)
        payload = json.loads(index_path.read_bytes())
        adm._atomic_write_bytes(index_path, json.dumps(payload, indent=1).encode("utf-8"))
        self.assertTrue(adm.verify_index(self.root).ok)
        payload["artifact_root_id"] = "root_" + "9" * 32
        adm._atomic_write_bytes(index_path, json.dumps(payload).encode("utf-8"))
        report = adm.verify_index(self.root)
        self.assertFalse(report.ok)
        self.assertIn("index-drift", [violation.code for violation in report.violations])


# ---------------------------------------------------------------------------
# 3366fe -- a large root keeps its artifact and event rows beside index.json
# ---------------------------------------------------------------------------

_OLD_RELEASE_WRITER = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import artifact_admission as adm
import artifact_identity as idm
import artifact_manifest as m
mode, root, count, seed = sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
spec = json.loads(Path(sys.argv[6]).read_text())
identity = adm.ensure_root_identity(root)
if mode == "old":
    # An older release: it knows only index.json (no layout beside it).
    adm._bulk_store = lambda _root: None
    adm.BULK_THRESHOLD_ROWS = 10 ** 9
else:
    adm.BULK_THRESHOLD_ROWS = 0
for n in range(count):
    adm.clear_index_cache()
    alloc = idm.IdAllocator()
    doc = json.loads(json.dumps(spec))
    for path, kind in (("manifest_id", "manifest"), ("manifest_revision_id", "manifest_revision")):
        doc[path] = alloc.allocate(kind)
    doc["campaign"]["campaign_id"] = doc["cycle"]["campaign_id"] = alloc.allocate("campaign")
    doc["cycle"]["cycle_id"] = alloc.allocate("cycle")
    doc["artifacts"][0]["artifact_id"] = doc["artifact_revisions"][0]["artifact_id"] = alloc.allocate("artifact")
    doc["artifacts"][0]["cycle_id"] = doc["cycle"]["cycle_id"]
    doc["artifact_revisions"][0]["artifact_revision_id"] = alloc.allocate("artifact_revision")
    doc["events"][0]["event_id"] = alloc.allocate("event")
    doc["events"][0]["stream_id"] = alloc.allocate("stream")
    doc["events"][0]["target_id"] = doc["artifacts"][0]["artifact_id"]
    for row in (doc["artifact_revisions"][0]["provenance"], doc["events"][0]["provenance"]):
        row["source_manifest_id"], row["source_revision_id"] = doc["manifest_id"], doc["manifest_revision_id"]
    doc["repository_id"], doc["artifact_root_id"] = identity.repository_id, identity.artifact_root_id
    src = root.parent / "src-{0}-{1}-{2}".format(mode, seed, n)
    src.mkdir()
    (src / "plan.md").write_bytes(b"hello")
    outcome = adm.admit(root, adm.AdmissionRequest(idempotency_key="k-{0}-{1}-{2}".format(mode, seed, n),
                                                    document=doc, staging_source=src))
    assert outcome.status == "admitted", outcome.to_payload()
    print(doc["cycle"]["cycle_id"], flush=True)
"""


class TestCycleRowsBesideIndex(AdmissionContractBase):
    """3366fe: above the threshold, index.json keeps its shape and the moved rows live per cycle."""

    MOVED = ("art_", "arev_", "evt_")

    def setUp(self):
        super().setUp()
        adm.clear_index_cache()
        self._threshold = mock.patch.object(adm, "BULK_THRESHOLD_ROWS", 0)
        self._threshold.start()

    def tearDown(self):
        self._threshold.stop()
        adm.clear_index_cache()
        super().tearDown()

    def _raw(self):
        return json.loads(adm._index_path(self.root).read_bytes())

    def _bulk(self):
        return self.root / adm.ADMISSION_REL / adm.BULK_REL

    def _whole(self):
        adm.clear_index_cache()
        return idx.canonical_bytes(adm.load_index(self.root))

    def _as_old_release(self):
        """What an older release does: index.json is the only file it knows."""
        return mock.patch.multiple(adm, _bulk_store=lambda _root: None, BULK_THRESHOLD_ROWS=10 ** 9)

    def test_index_json_keeps_its_closed_shape_and_rebuild_matches_the_whole_view(self):
        docs = [self._admit_valid(key="k-bulk-%d" % n, content=b"c-%d" % n)[0] for n in range(3)]
        raw = self._raw()
        parsed = idx.parse(raw)  # the reader every older release has
        self.assertEqual(parsed.event_ids, {})
        self.assertEqual(parsed.streams, {})
        self.assertFalse([key for key in parsed.stable_ids if key.startswith(self.MOVED)])
        for doc in docs:
            self.assertIn(doc["cycle"]["cycle_id"], parsed.cycles)
            self.assertIn(doc["campaign"]["campaign_id"], parsed.stable_ids)
            self.assertTrue((self._bulk() / "cycles" / (doc["cycle"]["cycle_id"] + ".json")).is_file())
        self.assertTrue((self._bulk() / "format.json").is_file())
        whole = adm.load_index(self.root)
        for doc in docs:
            self.assertEqual(whole.event_ids[doc["events"][0]["event_id"]]["cycle_id"], doc["cycle"]["cycle_id"])
            self.assertIn(doc["artifacts"][0]["artifact_id"], whole.stable_ids)
        rebuilt, _keys = adm._compute_rebuilt_index(self.root)
        self.assertEqual(self._whole(), idx.canonical_bytes(rebuilt))
        self.assertTrue(adm.verify_index(self.root).ok)
        self.assertEqual(idx.canonical_bytes(adm.rebuild_index(self.root)), idx.canonical_bytes(rebuilt))
        self.assertEqual(self._whole(), idx.canonical_bytes(rebuilt))

    def test_a_write_rewrites_only_its_own_cycle_file(self):
        first, _ = self._admit_valid(key="k-own-1", content=b"one")
        second, _ = self._admit_valid(key="k-own-2", content=b"two")
        cycles = self._bulk() / "cycles"
        before = {path.name: adm._file_key(path) for path in cycles.iterdir()}
        reads = []
        original = adm._BulkStore.shard
        with mock.patch.object(adm._BulkStore, "shard",
                               lambda store, name, fresh=False: reads.append(name) or original(store, name, fresh=fresh)):
            third, _ = self._admit_valid(key="k-own-3", content=b"three")
        after = {path.name: adm._file_key(path) for path in cycles.iterdir()}
        own = third["cycle"]["cycle_id"] + ".json"
        self.assertEqual(set(after) - set(before), {own})
        self.assertEqual({name: key for name, key in after.items() if name != own}, before)
        # Fresh IDs are absent from keys.bin: no other cycle's file is read to know it.
        self.assertEqual(set(reads), {third["cycle"]["cycle_id"]})
        self.assertNotIn(first["cycle"]["cycle_id"], reads)
        self.assertNotIn(second["cycle"]["cycle_id"], reads)

    def test_duplicate_ids_are_still_refused_from_the_cycle_files(self):
        doc, _ = self._admit_valid(key="k-dup-1", content=b"one")
        identity = self._identity()
        clash, content = _make_valid_document(self.alloc, identity, content=b"two")
        clash["events"][0]["event_id"] = doc["events"][0]["event_id"]
        adm.clear_index_cache()
        self._assert_rejected_and_unchanged(clash, self._stage_source(content), "k-dup-2",
                                            expect_code="index-event-id-reused")

    def test_an_older_release_writer_keeps_working_and_its_rows_are_folded_in(self):
        self._admit_valid(key="k-mixed-new-1", content=b"n1")
        with self._as_old_release():
            adm.clear_index_cache()
            old_doc, _ = self._admit_valid(key="k-mixed-old", content=b"o1")
            # The older reader sees only index.json; the row it added stays there.
            raw = self._raw()
            self.assertIn(old_doc["events"][0]["event_id"], raw["event_ids"])
        adm.clear_index_cache()
        self.assertIn(old_doc["events"][0]["event_id"], adm.load_index(self.root).event_ids)
        later, _ = self._admit_valid(key="k-mixed-new-2", content=b"n2")
        raw = self._raw()
        self.assertEqual(raw["event_ids"], {}, "the next writer folds the older writer's rows in")
        whole = adm.load_index(self.root)
        for doc in (old_doc, later):
            self.assertIn(doc["events"][0]["event_id"], whole.event_ids)
            self.assertIn(doc["artifact_revisions"][0]["artifact_revision_id"], whole.stable_ids)
        rebuilt, _keys = adm._compute_rebuilt_index(self.root)
        self.assertEqual(self._whole(), idx.canonical_bytes(rebuilt))

    def test_an_older_release_refresh_of_a_moved_cycle_is_not_refused(self):
        doc, outcome = self._admit_valid(key="k-refresh", content=b"r")
        digest = outcome.manifest_digest
        refreshed = json.loads(json.dumps(doc))
        refreshed["manifest_revision_id"] = self.alloc.allocate("manifest_revision")
        extra = json.loads(json.dumps(doc["events"][0]))
        extra.update(event_id=self.alloc.allocate("event"), stream_id=self.alloc.allocate("stream"))
        refreshed["events"].append(extra)
        old_view = idx.parse(self._raw())  # what an older release loads
        report = idx.check(old_view, refreshed, idempotency_key="k-refresh",
                           manifest_digest=m.manifest_digest(refreshed), replaces_manifest_digest=digest)
        self.assertTrue(report.ok, report.violations)
        report = idx.check(adm.load_index(self.root), refreshed, idempotency_key="k-refresh",
                           manifest_digest=m.manifest_digest(refreshed), replaces_manifest_digest=digest)
        self.assertTrue(report.ok, report.violations)

    def test_migration_crash_before_format_leaves_index_json_whole(self):
        with mock.patch.object(adm, "BULK_THRESHOLD_ROWS", 10 ** 9):
            docs = [self._admit_valid(key="k-mig-%d" % n, content=b"m-%d" % n)[0] for n in range(2)]
        whole_before = self._whole()
        original = adm._atomic_write_bytes

        def crash(path, data):
            if Path(path).name == "format.json":
                raise OSError("simulated crash before the layout is turned on")
            return original(path, data)

        adm.clear_index_cache()
        with mock.patch.object(adm, "_atomic_write_bytes", crash):
            with self.assertRaises(OSError):
                adm._write_index(self.root, adm.load_index(self.root))
        self.assertFalse((self._bulk() / "format.json").exists())
        self.assertEqual(self._whole(), whole_before)
        self.assertTrue([key for key in self._raw()["stable_ids"] if key.startswith(self.MOVED)])
        adm.clear_index_cache()
        adm._write_index(self.root, adm.load_index(self.root))
        self.assertTrue((self._bulk() / "format.json").is_file())
        self.assertEqual(self._raw()["event_ids"], {})
        self.assertEqual(self._whole(), whole_before)
        self.assertTrue(adm.verify_index(self.root).ok)
        for doc in docs:
            self.assertIn(doc["cycle"]["cycle_id"], adm.load_index(self.root).cycles)

    def test_a_crash_between_cycle_files_and_index_json_loses_nothing(self):
        self._admit_valid(key="k-crash-1", content=b"a")
        identity = self._identity()
        doc, content = _make_valid_document(self.alloc, identity, content=b"b")
        original = adm._atomic_write_bytes

        def crash(path, data):
            if Path(path).name == "index.json":
                raise OSError("simulated crash after the cycle files")
            return original(path, data)

        adm.clear_index_cache()
        with mock.patch.object(adm, "_atomic_write_bytes", crash):
            with self.assertRaises(adm.AdmissionRecoveryRequired):
                self._admit(doc, self._stage_source(content), "k-crash-2")
        adm.clear_index_cache()
        adm.recover(self.root)
        whole = adm.load_index(self.root)
        self.assertIn(doc["cycle"]["cycle_id"], whole.cycles)
        self.assertIn(doc["events"][0]["event_id"], whole.event_ids)
        rebuilt, _keys = adm._compute_rebuilt_index(self.root)
        self.assertEqual(self._whole(), idx.canonical_bytes(rebuilt))

    def test_receipt_reader_sees_moved_rows_read_only(self):
        doc, _ = self._admit_valid(key="k-receipt", content=b"r")
        before = self._root_fingerprint()
        view = adm.attach_bulk_rows(self.root, idx.parse(self._raw()))
        row = view.stable_ids.get(doc["artifacts"][0]["artifact_id"])
        self.assertEqual(row["cycle_id"], doc["cycle"]["cycle_id"])
        self.assertEqual(self._root_fingerprint(), before)

    def test_old_and_new_release_writers_racing_lose_no_rows(self):
        self._admit_valid(key="k-race-seed", content=b"seed")
        spec_doc, _ = _make_valid_document(self.alloc, self._identity())
        spec = self.scratch / "spec.json"
        spec.write_text(json.dumps(spec_doc))
        script = self.scratch / "writer.py"
        script.write_text(_OLD_RELEASE_WRITER)
        utilities = str(Path(__file__).resolve().parent)
        procs = [subprocess.Popen([sys.executable, str(script), utilities, mode, str(self.root), "4", str(n),
                                   str(spec)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for n, mode in enumerate(("old", "new", "old", "new"))]
        cycles = []
        for proc in procs:
            out, err = proc.communicate(timeout=300)
            self.assertEqual(proc.returncode, 0, err.decode())
            cycles.extend(out.decode().split())
        self.assertEqual(len(cycles), 16)
        adm.clear_index_cache()
        adm._write_index(self.root, adm.load_index(self.root))  # one writer of this release folds the rest
        whole = adm.load_index(self.root)
        for cycle_id in cycles:
            self.assertIn(cycle_id, whole.cycles)
            self.assertIn(cycle_id, {row["cycle_id"] for row in whole.event_ids.values()})
        rebuilt, _keys = adm._compute_rebuilt_index(self.root)
        self.assertEqual(self._whole(), idx.canonical_bytes(rebuilt))
        self.assertTrue(adm.verify_index(self.root).ok)

    def _inline_cycles(self):
        """Per cycle, which moved sections index.json still holds rows of."""
        raw = self._raw()
        held = {}
        for name in ("stable_ids", "event_ids", "streams"):
            for key, row in raw[name].items():
                if name != "stable_ids" or key.startswith(self.MOVED):
                    held.setdefault(row["cycle_id"], set()).add(name)
        return held

    def test_rows_move_out_a_few_whole_cycles_per_write(self):
        with mock.patch.object(adm, "BULK_THRESHOLD_ROWS", 10 ** 9):
            docs = [self._admit_valid(key="k-fold-%d" % n, content=b"f-%d" % n)[0] for n in range(4)]
        whole_before = self._whole()
        cycles = {doc["cycle"]["cycle_id"] for doc in docs}
        writes = 0
        with mock.patch.object(adm, "BULK_FOLD_ROWS", 1):
            while self._inline_cycles():
                adm.clear_index_cache()
                adm._write_index(self.root, adm.load_index(self.root))
                writes += 1
                held = self._inline_cycles()
                # An older reader sees each cycle's rows all together or not at all.
                for cycle_id, sections in held.items():
                    self.assertEqual(sections, {"stable_ids", "event_ids", "streams"}, cycle_id)
                self.assertEqual(len(held), len(cycles) - writes)
                self.assertEqual(self._whole(), whole_before)
                old_view = idx.parse(self._raw())
                for doc in docs:
                    refreshed = json.loads(json.dumps(doc))
                    refreshed["manifest_revision_id"] = self.alloc.allocate("manifest_revision")
                    key = next(k for k, row in old_view.manifests.items()
                               if row["cycle_id"] == doc["cycle"]["cycle_id"])
                    previous = old_view.manifests[key]["manifest_digest"]
                    report = idx.check(old_view, refreshed, idempotency_key=key,
                                       manifest_digest=m.manifest_digest(refreshed),
                                       replaces_manifest_digest=previous)
                    self.assertTrue(report.ok, report.violations)
        self.assertTrue((self._bulk() / "format.json").is_file())
        self.assertEqual(writes, len(cycles))
        self.assertTrue(adm.verify_index(self.root).ok)

    def test_a_write_moves_every_row_of_the_cycle_it_changes(self):
        with mock.patch.object(adm, "BULK_THRESHOLD_ROWS", 10 ** 9):
            docs = [self._admit_valid(key="k-touch-%d" % n, content=b"t-%d" % n)[0] for n in range(3)]
        with mock.patch.object(adm, "BULK_FOLD_ROWS", 0):
            adm.clear_index_cache()
            adm._write_index(self.root, adm.load_index(self.root))  # layout on, nothing changed: all stay
            self.assertEqual(len(self._inline_cycles()), 3)
            target = docs[1]
            refreshed = json.loads(json.dumps(target))
            refreshed["manifest_revision_id"] = self.alloc.allocate("manifest_revision")
            adm.clear_index_cache()
            index = adm.load_index(self.root)
            key = next(k for k, row in index.manifests.items() if row["cycle_id"] == target["cycle"]["cycle_id"])
            cycle_path = index.cycles[target["cycle"]["cycle_id"]]["cycle_path"]
            index = idx.apply(index, refreshed, cycle_path=cycle_path,
                              manifest_digest=m.manifest_digest(refreshed), idempotency_key=key)
            adm._write_index(self.root, index)
        held = self._inline_cycles()
        self.assertNotIn(target["cycle"]["cycle_id"], held)
        self.assertEqual(set(held), {docs[0]["cycle"]["cycle_id"], docs[2]["cycle"]["cycle_id"]})
        self.assertTrue((self._bulk() / "cycles" / (target["cycle"]["cycle_id"] + ".json")).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
