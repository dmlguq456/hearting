#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import artifact_index
import artifact_admission
import artifact_locator
import artifact_locator_amendment as L
import artifact_metadata_amendment as A

SPEC = importlib.util.spec_from_file_location(
    "artifact_metadata_amendment_tests", HERE / "artifact_metadata_amendment.test.py"
)
assert SPEC is not None and SPEC.loader is not None
META_TESTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(META_TESTS)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


class LocatorAmendmentTests(unittest.TestCase):
    ROOT_ID = META_TESTS.AmendmentTests.ROOT_ID
    REPO_ID = META_TESTS.AmendmentTests.REPO_ID
    CAMPAIGN_ID = META_TESTS.AmendmentTests.CAMPAIGN_ID
    CYCLES = META_TESTS.AmendmentTests.CYCLES
    REVISIONS = META_TESTS.AmendmentTests.REVISIONS

    def setUp(self) -> None:
        META_TESTS.AmendmentTests.setUp(self)
        metadata = META_TESTS.AmendmentTests.package(self)
        A.apply(metadata, expected_package_digest=A.package_digest(metadata))
        self.old_campaign = self.campaign_dir
        self.new_campaign = self.root / "campaigns" / "2026-09-13_ifcorrnet-icassp-resubmission"
        self.requests = {
            self.CYCLES[0]: {"locator": "2026-09-13_v1-resubmission-diagnosis", "title": "v1 · 재제출 진단"},
            self.CYCLES[1]: {"locator": "2026-09-13_v2-review-mapping-diagnosis", "title": "v2 · 심사 의견·Mapping 추가 진단"},
        }
        artifact_locator.rebuild_indexes(self.root)
        index = artifact_index.to_payload(artifact_index.empty(self.ROOT_ID))
        for cycle_id, locator in zip(self.CYCLES, ("2026-09-13_cycle-1", "2026-09-13_cycle-2")):
            manifest = self.old_campaign / locator / "manifest.json"
            index["cycles"][cycle_id] = {
                "campaign_id": self.CAMPAIGN_ID,
                "cycle_path": f"campaigns/{self.old_campaign.name}/{locator}",
                "manifest_digest": A.digest_bytes(manifest.read_bytes()),
            }
        admission = self.root / artifact_admission.ADMISSION_REL / "index.json"
        admission.parent.mkdir(parents=True, exist_ok=True)
        admission.write_bytes(A.canonical(index) + b"\n")
        for index_number in range(2):
            route = self.root / ".runtime" / "routes" / f"rt-{index_number}.json"
            payload = json.loads(route.read_text())
            payload["evidence_path"] = str(
                self.old_campaign / f"2026-09-13_cycle-{index_number + 1}" / "artifacts" / "analysis" / "REPORT.md"
            )
            write_json(route, payload)

    def tearDown(self) -> None:
        META_TESTS.AmendmentTests.tearDown(self)

    def package(self) -> dict:
        return L.prepare(
            self.root,
            campaign_id=self.CAMPAIGN_ID,
            campaign_locator=self.new_campaign.name,
            cycles=self.requests,
        )

    def test_prepare_uses_folded_state_for_closed_campaign_refusal(self) -> None:
        with mock.patch.object(L.artifact_campaign, "campaign_state",
                               return_value=SimpleNamespace(state="satisfied")):
            with self.assertRaisesRegex(L.LocatorAmendmentError, "campaign-state-locator-mismatch"):
                self.package()

    def test_apply_preserves_ids_lineage_seals_and_old_evidence_paths(self) -> None:
        package = self.package()
        protected = {row["path"]: (self.root / row["path"]).read_bytes() for row in package["protected_files"]}
        manifests = {row["cycle_id"]: (self.root / row["path"]).read_bytes() for row in package["manifest_sources"]}
        result = L.apply(package, expected_package_digest=L.package_digest(package))
        self.assertEqual(result["status"], "applied")
        self.assertTrue(self.new_campaign.is_dir())
        self.assertTrue(self.old_campaign.is_symlink())
        self.assertEqual(os.readlink(self.old_campaign), self.new_campaign.name)
        for cycle_id, request in self.requests.items():
            record = json.loads((self.root / A.PRODUCER_REL / "cycles" / f"{cycle_id}.json").read_text())
            self.assertEqual(record["cycle_id"], cycle_id)
            self.assertEqual(record["locator"], request["locator"])
            self.assertEqual(record["title"], request["title"])
        second = json.loads((self.root / A.PRODUCER_REL / "cycles" / f"{self.CYCLES[1]}.json").read_text())
        self.assertEqual(second["parent_cycle_id"], self.CYCLES[0])
        self.assertEqual(protected, {path: (self.root / path).read_bytes() for path in protected})
        self.assertEqual(manifests, {row["cycle_id"]: (self.root / row["path"]).read_bytes() for row in package["manifest_sources"]})
        for index_number in range(2):
            route = json.loads((self.root / ".runtime" / "routes" / f"rt-{index_number}.json").read_text())
            self.assertTrue(Path(route["evidence_path"]).is_file())
        self.assertEqual(L.verify(package)["status"], "verified")
        self.assertEqual(
            L.apply(package, expected_package_digest=L.package_digest(package))["status"],
            "already-applied",
        )

    def test_root_without_title_repair_sidecar_and_unmoved_cycle_still_amends(self) -> None:
        """A fresh root never ran the fleet title repair (no display-title v2
        sidecar), and a cycle whose locator is already right only needs its
        title; neither is a refusal (TF-Rehancer replay, 2026-09-15)."""
        (self.root / A.DISPLAY_TITLES_REL).unlink()
        kept_locator = "2026-09-13_cycle-2"
        self.requests[self.CYCLES[1]] = {"locator": kept_locator, "title": "v2 · 제목만 바뀜"}
        package = self.package()
        rows = {row["cycle_id"]: row for row in package["cycles"]}
        self.assertTrue(rows[self.CYCLES[0]]["move"])
        self.assertFalse(rows[self.CYCLES[1]]["move"])
        self.assertEqual(len(package["file_targets"]), 5 + len(self.CYCLES))
        record_pre = json.loads((self.root / A.PRODUCER_REL / "cycles" / f"{self.CYCLES[1]}.json").read_text())
        result = L.apply(package, expected_package_digest=L.package_digest(package))
        self.assertEqual(result["status"], "applied")
        self.assertFalse((self.root / A.DISPLAY_TITLES_REL).exists())
        self.assertTrue((self.new_campaign / kept_locator).is_dir())
        self.assertFalse((self.new_campaign / kept_locator).is_symlink())
        self.assertTrue((self.old_campaign).is_symlink())
        self.assertTrue((self.new_campaign / "2026-09-13_cycle-1").is_symlink())
        record_post = json.loads((self.root / A.PRODUCER_REL / "cycles" / f"{self.CYCLES[1]}.json").read_text())
        self.assertEqual(record_post["title"], "v2 · 제목만 바뀜")
        for field in ("locator", "slug", "locator_suffix", "slug_source"):
            self.assertEqual(record_post.get(field), record_pre.get(field), field)
        titles = json.loads((self.root / A.CYCLE_TITLES_REL).read_text())
        self.assertEqual({row["cycle_id"]: row["display_title"] for row in titles["entries"]}[self.CYCLES[1]],
                         "v2 · 제목만 바뀜")
        mapping, _ = artifact_locator.scan_index(self.root)
        self.assertEqual(mapping[self.CYCLES[1]], f"campaigns/{self.new_campaign.name}/{kept_locator}")
        admission = json.loads((self.root / artifact_admission.ADMISSION_REL / "index.json").read_text())
        self.assertEqual(admission["cycles"][self.CYCLES[1]]["cycle_path"],
                         f"campaigns/{self.new_campaign.name}/{kept_locator}")
        again = L.apply(package, expected_package_digest=L.package_digest(package))
        self.assertEqual(again["status"], "already-applied")

    def test_fault_on_a_mixed_package_restores_the_unmoved_cycle_too(self) -> None:
        (self.root / A.DISPLAY_TITLES_REL).unlink()
        self.requests[self.CYCLES[1]] = {"locator": "2026-09-13_cycle-2", "title": "v2 · 제목만 바뀜"}
        package = self.package()
        targets = {row["pre_path"]: (self.root / row["pre_path"]).read_bytes() for row in package["file_targets"]}
        record_path = self.root / A.PRODUCER_REL / "cycles" / f"{self.CYCLES[1]}.json"
        # One campaign rename + one cycle rename + N file writes: fault after
        # the record writes so the rollback must undo files and moves alike.
        for fault in (2, 3 + len(package["file_targets"])):
            with self.assertRaisesRegex(L.LocatorAmendmentError, "injected-locator-failure"):
                L.apply(package, expected_package_digest=L.package_digest(package), fault_after_steps=fault)
            self.assertTrue(self.old_campaign.is_dir() and not self.old_campaign.is_symlink())
            self.assertFalse(self.new_campaign.exists())
            self.assertTrue((self.old_campaign / "2026-09-13_cycle-1").is_dir())
            self.assertTrue((self.old_campaign / "2026-09-13_cycle-2").is_dir())
            self.assertFalse((self.old_campaign / "2026-09-13_cycle-2").is_symlink())
            self.assertEqual(targets, {path: (self.root / path).read_bytes() for path in targets})
            self.assertEqual(json.loads(record_path.read_text())["title"], "2026-09-13_cycle-2")
        self.assertEqual(L.apply(package, expected_package_digest=L.package_digest(package))["status"], "applied")
        self.assertEqual(json.loads(record_path.read_text())["title"], "v2 · 제목만 바뀜")

    def test_fault_rolls_back_then_replay_succeeds(self) -> None:
        package = self.package()
        targets = {row["pre_path"]: (self.root / row["pre_path"]).read_bytes() for row in package["file_targets"]}
        with self.assertRaisesRegex(L.LocatorAmendmentError, "injected-locator-failure"):
            L.apply(package, expected_package_digest=L.package_digest(package), fault_after_steps=4)
        self.assertTrue(self.old_campaign.is_dir())
        self.assertFalse(self.old_campaign.is_symlink())
        self.assertFalse(self.new_campaign.exists())
        self.assertEqual(targets, {path: (self.root / path).read_bytes() for path in targets})
        self.assertEqual(L.apply(package, expected_package_digest=L.package_digest(package))["status"], "applied")

    def test_collision_and_preimage_drift_refuse_before_rename(self) -> None:
        collision = self.root / "campaigns" / self.new_campaign.name
        collision.mkdir()
        with self.assertRaisesRegex(L.LocatorAmendmentError, "campaign-target-collision"):
            self.package()
        collision.rmdir()
        package = self.package()
        campaign_record = self.old_campaign / "campaign.json"
        campaign_record.write_text(campaign_record.read_text() + " ")
        with self.assertRaisesRegex(L.LocatorAmendmentError, "file-preimage-drift"):
            L.apply(package, expected_package_digest=L.package_digest(package))
        self.assertTrue(self.old_campaign.is_dir())
        self.assertFalse(self.new_campaign.exists())

    def test_tampered_foreign_manifest_binding_is_rejected(self) -> None:
        package = self.package()
        bad = copy.deepcopy(package)
        bad["manifest_sources"][0]["manifest_digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(L.LocatorAmendmentError, "manifest-binding-drift"):
            L.apply(bad, expected_package_digest=L.package_digest(bad))
        self.assertTrue(self.old_campaign.is_dir())

    def test_concurrent_apply_serializes_to_apply_and_replay(self) -> None:
        package = self.package()
        package_path = Path(self.temp.name) / "locator-package.json"
        write_json(package_path, package)
        digest = L.package_digest(package)
        command = [sys.executable, str(Path(L.__file__)), "apply", "--package", str(package_path),
                   "--expect-package-digest", digest]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        statuses = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            statuses.append(json.loads(stdout)["status"])
        self.assertEqual(sorted(statuses), ["already-applied", "applied"])


if __name__ == "__main__":
    unittest.main()
