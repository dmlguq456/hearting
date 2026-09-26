#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_metadata_amendment as A


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


class AmendmentTests(unittest.TestCase):
    ROOT_ID = "root_" + "1" * 32
    REPO_ID = "repo_" + "2" * 32
    CAMPAIGN_ID = "camp_" + "3" * 32
    CYCLES = ("cyc_" + "4" * 32, "cyc_" + "5" * 32)
    REVISIONS = ("mrev_" + "6" * 32, "mrev_" + "7" * 32)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".agent_reports"
        self.campaign_dir = self.root / "campaigns" / "2026-09-13_unassigned"
        self.campaign = {
            "schema_version": 1, "contract": "artifact-producer/v1", "campaign_id": self.CAMPAIGN_ID,
            "key": "_unassigned", "goal": "Work stream not proposed", "title": "IF-CorrNet ICASSP 재제출 준비",
            "state": "active", "degraded": True, "degraded_reason": "campaign-unassigned",
            "cycles": list(self.CYCLES), "locator": self.campaign_dir.name,
        }
        write_json(self.campaign_dir / "campaign.json", self.campaign)
        write_json(self.root / A.ROOT_IDENTITY_REL, {
            "schema_version": 1, "artifact_root_id": self.ROOT_ID, "repository_id": self.REPO_ID,
            "issued_at": "2026-09-13T00:00:00Z", "producer_contract_version": "artifact-cycle-manifest/v2",
        })
        self.manifests = []
        for index, cycle_id in enumerate(self.CYCLES):
            locator = f"2026-09-13_cycle-{index+1}"
            cycle_dir = self.campaign_dir / locator
            # First cycle: binding written before `started_on` existed; second: current shape.
            write_json(cycle_dir / ".cycle.json", {"schema_version": 1, "kind": "artifact-cycle-binding",
                                                    "campaign_id": self.CAMPAIGN_ID, "cycle_id": cycle_id,
                                                    **({"started_on": "2026-09-13T01:00:00Z"} if index else {})})
            (cycle_dir / "artifacts" / "analysis").mkdir(parents=True)
            (cycle_dir / "artifacts" / "analysis" / "REPORT.md").write_text(f"report {index}\n")
            manifest = {
                "schema_version": 2, "artifact_root_id": self.ROOT_ID, "repository_id": self.REPO_ID,
                "manifest_id": "man_" + str(8 + index) * 32,
                "manifest_revision_id": self.REVISIONS[index],
                "campaign": {"campaign_id": self.CAMPAIGN_ID, "title": "_unassigned", "goal": "Work stream not proposed"},
                "cycle": {"campaign_id": self.CAMPAIGN_ID, "cycle_id": cycle_id,
                          "parent_cycle_id": None if index == 0 else self.CYCLES[0], "state": "completed"},
                "artifacts": [],
            }
            write_json(cycle_dir / "manifest.json", manifest)
            self.manifests.append(manifest)
            route = self.root / ".runtime" / "routes" / f"rt-{index}.json"
            write_json(route, {"route_id": f"rt-{index}", "sealed": True})
            record = {
                "schema_version": 1, "contract": "artifact-producer/v1", "cycle_id": cycle_id,
                "campaign_id": self.CAMPAIGN_ID, "state": "sealed", "cycle_state": "completed",
                "locator": locator, "title": locator, "route_file": str(route),
                "parent_cycle_id": None if index == 0 else self.CYCLES[0],
                "manifest_digest": A.digest_bytes((cycle_dir / "manifest.json").read_bytes()),
            }
            write_json(self.root / A.PRODUCER_REL / "cycles" / f"{cycle_id}.json", record)
        write_json(self.root / A.DISPLAY_TITLES_REL, {
            "schema": "hearting-campaign-display-titles/v2", "artifact_root_id": self.ROOT_ID,
            "ruleset": "test", "entries": [{"campaign_id": self.CAMPAIGN_ID,
                "campaign_locator": self.campaign_dir.name, "display_title": self.campaign["title"],
                "manifest_bindings": [], "manifest_revision_ids": [], "manifest_digests": []}],
        })
        (self.root / "shared" / "analysis" / "ref" / "revisions" / "rrev" / "paper").mkdir(parents=True)
        (self.root / "shared" / "analysis" / "ref" / "revisions" / "rrev" / "paper" / "REPORT.md").write_text("shared\n")
        self.titles = {self.CYCLES[0]: "재제출 진단", self.CYCLES[1]: "심사 의견·Mapping 추가 진단"}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def package(self) -> dict:
        return A.prepare(self.root, campaign_id=self.CAMPAIGN_ID, key="ifcorrnet-icassp-resubmission",
                         goal="ICASSP 재제출 원고를 진단한다.", cycle_titles=self.titles)

    def protected_snapshot(self, package: dict) -> dict[str, bytes]:
        return {row["path"]: (self.root / row["path"]).read_bytes() for row in package["protected_files"]}

    def test_prepare_apply_verify_preserves_sealed_sources_and_is_idempotent(self) -> None:
        package = self.package()
        before = self.protected_snapshot(package)
        result = A.apply(package, expected_package_digest=A.package_digest(package))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(A.verify(package)["status"], "verified")
        campaign = json.loads((self.campaign_dir / "campaign.json").read_text())
        expected = dict(self.campaign)
        expected.update(key="ifcorrnet-icassp-resubmission", goal="ICASSP 재제출 원고를 진단한다.")
        expected.pop("degraded")
        expected.pop("degraded_reason")
        self.assertEqual(campaign, expected)
        self.assertEqual(before, self.protected_snapshot(package))
        campaign_sidecar = json.loads((self.root / A.CAMPAIGN_METADATA_REL).read_text())
        cycle_sidecar = json.loads((self.root / A.CYCLE_TITLES_REL).read_text())
        self.assertEqual(set(campaign_sidecar), {"schema", "artifact_root_id", "repository_id", "entries"})
        self.assertEqual(set(cycle_sidecar), {"schema", "artifact_root_id", "repository_id", "entries"})
        revisions = [row["manifest_revision_id"] for row in campaign_sidecar["entries"][0]["manifest_bindings"]]
        self.assertEqual(revisions, sorted(set(revisions)))
        self.assertEqual(A.apply(package, expected_package_digest=A.package_digest(package))["status"], "already-applied")

    def test_prepare_uses_folded_state_for_closed_campaign_refusal(self) -> None:
        with mock.patch.object(A.artifact_campaign, "campaign_state",
                               return_value=SimpleNamespace(state="satisfied")):
            with self.assertRaisesRegex(A.AmendmentError, "campaign-not-active-unassigned"):
                self.package()

    def test_preimage_drift_and_key_collision_refuse_without_target_writes(self) -> None:
        package = self.package()
        (self.campaign_dir / "campaign.json").write_text((self.campaign_dir / "campaign.json").read_text() + " ")
        with self.assertRaisesRegex(A.AmendmentError, "prepared-preimage-drift"):
            A.apply(package, expected_package_digest=A.package_digest(package))
        self.assertFalse((self.root / A.CAMPAIGN_METADATA_REL).exists())
        other = self.root / "campaigns" / "other" / "campaign.json"
        write_json(other, {"campaign_id": "camp_" + "a" * 32, "key": "ifcorrnet-icassp-resubmission"})
        write_json(self.campaign_dir / "campaign.json", self.campaign)
        with self.assertRaisesRegex(A.AmendmentError, "campaign-key-collision"):
            self.package()

    def test_fault_rolls_back_and_replay_succeeds(self) -> None:
        package = self.package()
        target_before = {row["path"]: A._read_bytes(self.root / row["path"], missing_ok=True) for row in package["targets"]}
        with self.assertRaisesRegex(A.AmendmentError, "injected-apply-failure"):
            A.apply(package, expected_package_digest=A.package_digest(package), fault_after_writes=2)
        self.assertEqual(target_before, {row["path"]: A._read_bytes(self.root / row["path"], missing_ok=True) for row in package["targets"]})
        self.assertEqual(A.apply(package, expected_package_digest=A.package_digest(package))["status"], "applied")

    def test_foreign_cycle_binding_is_rejected_before_transaction(self) -> None:
        package = self.package()
        bad = copy.deepcopy(package)
        target = next(row for row in bad["targets"] if row["path"] == A.CYCLE_TITLES_REL.as_posix())
        doc = json.loads(A._unb64(target["post_bytes_b64"]).decode())
        doc["entries"][0]["manifest_bindings"][0]["manifest_digest"] = "sha256:" + "f" * 64
        post = A.canonical(doc) + b"\n"
        target["post_bytes_b64"] = A._b64(post)
        target["post_digest"] = A.digest_bytes(post)
        with self.assertRaisesRegex(A.AmendmentError, "package-cycle-sidecar-binding-mismatch"):
            A.apply(bad, expected_package_digest=A.package_digest(bad))
        self.assertFalse((self.root / A.CAMPAIGN_METADATA_REL).exists())

    def test_duplicate_campaign_binding_is_rejected_before_transaction(self) -> None:
        package = self.package()
        bad = copy.deepcopy(package)
        target = next(row for row in bad["targets"] if row["path"] == A.CAMPAIGN_METADATA_REL.as_posix())
        doc = json.loads(A._unb64(target["post_bytes_b64"]).decode())
        doc["entries"][0]["manifest_bindings"].insert(1, copy.deepcopy(doc["entries"][0]["manifest_bindings"][0]))
        post = A.canonical(doc) + b"\n"
        target["post_bytes_b64"] = A._b64(post)
        target["post_digest"] = A.digest_bytes(post)
        with self.assertRaisesRegex(A.AmendmentError, "duplicate-revision"):
            A.apply(bad, expected_package_digest=A.package_digest(bad))
        self.assertFalse((self.root / A.CAMPAIGN_METADATA_REL).exists())

    def test_concurrent_identical_apply_serializes_to_apply_and_replay(self) -> None:
        package = self.package()
        package_path = Path(self.temp.name) / "package.json"
        write_json(package_path, package)
        digest = A.package_digest(package)
        command = [sys.executable, str(Path(A.__file__)), "apply", "--package", str(package_path),
                   "--expect-package-digest", digest]
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        outputs = []
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            outputs.append(json.loads(stdout)["status"])
        self.assertEqual(sorted(outputs), ["already-applied", "applied"])


if __name__ == "__main__":
    unittest.main()
