#!/usr/bin/env python3
"""Regression boundary for pending publication plus a live v2 review lease.

The recovery fixtures acquire the lease through the public API from the exact
9db757bb source revision.  That is intentional: the candidate admission fence
must not make the historical pending state impossible to construct, because
roots admitted by that revision still need safe recovery.  Every fixture root,
jobs file, governed lease, and manifest-only journal removal is isolated under
a TemporaryDirectory; no runtime registry or canonical artifact is edited.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

from contextlib import contextmanager
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UTILITIES = PROJECT_ROOT / "utilities"
sys.path.insert(0, str(UTILITIES))

import artifact_admission as adm  # noqa: E402
import artifact_producer as P  # noqa: E402
import dispatch_contract as D  # noqa: E402


_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "artifact_producer_fixture_for_recovery_fence",
    UTILITIES / "artifact_producer.test.py",
)
FIXTURE = importlib.util.module_from_spec(_FIXTURE_SPEC)
assert _FIXTURE_SPEC.loader is not None
_FIXTURE_SPEC.loader.exec_module(FIXTURE)

BASELINE_SHA = "9db757bbe14ea964653d02eaa57aa0e212ee0065"
ADMISSION_BLOCK = "review-lease-admission-after-publication"


class ReviewerReportRecoveryFenceTest(FIXTURE.ProducerTestBase):
    def setUp(self):
        super().setUp()
        self._cycle_ordinal = 0
        source = subprocess.run(
            ["git", "-c", f"safe.directory={PROJECT_ROOT}", "show",
             f"{BASELINE_SHA}:utilities/artifact_producer.py"],
            cwd=PROJECT_ROOT, text=True, capture_output=True, check=True,
        ).stdout
        baseline_path = Path(self._tmp.name) / "artifact_producer_9db757bb.py"
        baseline_path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            f"artifact_producer_9db757bb_{id(self)}", baseline_path,
        )
        self.baseline = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(self.baseline)

    def _cycle(self, *, capability="autopilot-code", mode="debug", rel="plans/cycle/evidence.md"):
        self._cycle_ordinal += 1
        route, route_file = self.route(
            capability=capability, mode=mode,
            slug=f"recovery-fence-{self._cycle_ordinal}",
        )
        result = self.baseline.begin(
            self.root, route_file=route_file, capability=capability,
            intensity="direct",
        )
        evidence = Path(result["cycle_dir"]) / "artifacts" / rel
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(f"evidence for {result['cycle_id']}\n", encoding="utf-8")
        self.close(route, route_file)
        return result, evidence

    def _review_authority(self, result, *, attempt="att-baseline-public-review"):
        output = Path(result["cycle_dir"]) / "artifacts/plans/reviewer/report.md"
        binding = self.baseline.prepare_review_output_binding(
            self.root, cycle_id=result["cycle_id"],
            producer_id=result["producer_id"], attempt_id=attempt,
            review_output=output, capability="autopilot-code",
            unit="qa/code-review", worktree=PROJECT_ROOT,
        )
        identity = D.process_launch_identity(os.getpid())
        nonce = (attempt.encode("utf-8").hex() + "0" * 64)[:64]
        metadata = {
            "attempt_schema_version": "2",
            "dispatch_depth": "1",
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": "1",
            "worker_type": "review",
            "unit": "qa/code-review",
            "capability": "autopilot-code",
            "attempt_id": attempt,
            "artifact_root": str(self.root),
            "review_cycle_id": result["cycle_id"],
            "review_producer_id": result["producer_id"],
            "review_output_locator_b64": binding["locator_b64"],
            "review_output_digest": binding["digest"],
            "review_governed_lease": D.REVIEW_GOVERNED_LEASE_KIND,
            "review_governed_lease_nonce": nonce,
            **identity,
        }
        self.jobs.write_text(
            "ts\topen\t{0}\t{0}\treview\t{1}\n".format(
                PROJECT_ROOT,
                ",".join(f"{key}={value}" for key, value in metadata.items()),
            ),
            encoding="utf-8",
        )
        governed = D.review_governed_lease_path(
            self.root, result["cycle_id"], attempt,
        )
        governed.parent.mkdir(parents=True, exist_ok=True)
        governed.write_bytes(D.review_governed_lease_payload(
            attempt, result["cycle_id"], nonce,
        ))
        return output, binding, identity, governed

    @contextmanager
    def _baseline_pending_live_v2(self, *, journal=True, preceding=None):
        self.baseline.activate(
            self.root, repository_id=FIXTURE.REPO_ID,
            artifact_root_id=FIXTURE.ROOT_ID,
            w7={"campaign_id": "camp_" + "c" * 32},
        )
        other = None
        if preceding == "open":
            other, _ = self._cycle(rel="plans/other/evidence.md")
        elif preceding == "sealed":
            other, _ = self._cycle(rel="plans/shared/evidence.md")
            self.baseline.finalize(self.root, cycle_id=other["cycle_id"])
        result, evidence = self._cycle()
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            self.baseline.finalize(
                self.root, cycle_id=result["cycle_id"],
                crash_after_manifest=True,
            )
        if not journal:
            # Isolated manifest-only preservation fixture.  Production
            # journals are never deleted by this regression.
            self.baseline.journal_path(self.root, result["cycle_id"]).unlink()
        output, binding, identity, governed = self._review_authority(result)
        with governed.open("r+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = self.baseline.review_lease_acquire(
                self.root, cycle_id=result["cycle_id"],
                attempt_id=binding["attempt_id"], review_output=output,
                binding=binding, governed_identity=identity, jobs=self.jobs,
            )
            self.assertEqual(acquired["status"], "acquired")
            self.assertTrue(P.review_lease_status(
                self.root, cycle_id=result["cycle_id"],
                attempt_id=binding["attempt_id"],
            )["live"])
            yield result, evidence, binding, other

    @staticmethod
    def _optional_bytes(path):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return None
        return path.read_bytes() if stat.S_ISREG(mode) else ("entry", mode)

    def _snapshot(self, result, evidence):
        cycle_id = result["cycle_id"]
        index = adm.load_index(self.root)
        locator_json = self.root / "campaigns/INDEX.json"
        locator_md = self.root / "campaigns/INDEX.md"
        return {
            "record": P.cycle_record_path(self.root, cycle_id).read_bytes(),
            "index_row": copy.deepcopy(index.manifests.get(cycle_id)),
            "journal": self._optional_bytes(P.journal_path(self.root, cycle_id)),
            "manifest": (Path(result["cycle_dir"]) / "manifest.json").read_bytes(),
            "evidence": evidence.read_bytes(),
            "locator_json": self._optional_bytes(locator_json),
            "locator_md": self._optional_bytes(locator_md),
        }

    def _assert_snapshot(self, result, evidence, before):
        self.assertEqual(self._snapshot(result, evidence), before)
        cycle_id = result["cycle_id"]
        self.assertEqual(P.read_cycle_record(self.root, cycle_id)["state"], "open")
        self.assertNotIn(cycle_id, adm.load_index(self.root).manifests)

    def _recovery_case(self, action, *, journal):
        preceding = "open" if action == "other-finalize" else (
            "sealed" if action == "admit-shared" else None
        )
        with self._baseline_pending_live_v2(
            journal=journal, preceding=preceding,
        ) as (result, evidence, binding, other):
            cycle_id = result["cycle_id"]
            before = self._snapshot(result, evidence)

            with self.assertRaises(P.ProducerError) as caught:
                if action == "same-finalize":
                    P.finalize(self.root, cycle_id=cycle_id)
                elif action == "other-finalize":
                    P.finalize(self.root, cycle_id=other["cycle_id"])
                elif action == "recover":
                    P.recover(self.root)
                else:
                    P.admit_shared(
                        self.root, cycle_id=other["cycle_id"], kind="analysis",
                        source="plans/shared/evidence.md", key="fence-source",
                    )
            self.assertEqual(caught.exception.code, "cycle-finalize-blocked-live-review")
            self._assert_snapshot(result, evidence, before)
            self.assertTrue(P.review_lease_status(
                self.root, cycle_id=cycle_id,
                attempt_id=binding["attempt_id"],
            )["live"])

            released = P.review_lease_release(
                self.root, cycle_id=cycle_id,
                attempt_id=binding["attempt_id"],
            )
            self.assertEqual(released["status"], "released")
            if action == "same-finalize":
                forwarded = P.finalize(self.root, cycle_id=cycle_id)
                self.assertEqual(forwarded["status"], "already-sealed")
            elif action == "other-finalize":
                forwarded = P.finalize(self.root, cycle_id=other["cycle_id"])
                self.assertEqual(forwarded["status"], "sealed")
            elif action == "recover":
                forwarded = P.recover(self.root)
                self.assertIn(cycle_id, forwarded["producer"]["rolled_forward"])
            else:
                forwarded = P.admit_shared(
                    self.root, cycle_id=other["cycle_id"], kind="analysis",
                    source="plans/shared/evidence.md", key="fence-source",
                )
                self.assertEqual(forwarded["status"], "admitted")
            self.assertEqual(P.read_cycle_record(self.root, cycle_id)["state"], "sealed")

    def test_journal_same_finalize_preserves_pending_cycle(self):
        self._recovery_case("same-finalize", journal=True)

    def test_pending_v2_blocks_terminal_exact_recovery_and_verification(self):
        for journal in (True, False):
            with self.subTest(journal=journal):
                fixture = ReviewerReportRecoveryFenceTest()
                fixture.setUp()
                try:
                    with fixture._baseline_pending_live_v2(journal=journal) as (result, evidence, lease, _):
                        cycle_id = result["cycle_id"]
                        record = P.read_cycle_record(fixture.root, cycle_id)
                        binding = {key: record[key] for key in
                                   ("campaign_id", "cycle_id", "producer_id", "route_hash")}
                        binding["cycle_record_digest"] = P.dispatch_terminal_commit.cycle_identity_digest(record)
                        before = fixture._snapshot(result, evidence)
                        for operation in (P.finalize_exact_cycle, P.verify_finalized_cycle):
                            with self.assertRaises(P.ProducerError) as caught:
                                operation(fixture.root, cycle_id=cycle_id, expected_binding=binding)
                            self.assertEqual(caught.exception.code, "cycle-finalize-blocked-live-review")
                            fixture._assert_snapshot(result, evidence, before)
                        P.review_lease_release(fixture.root, cycle_id=cycle_id, attempt_id=lease["attempt_id"])
                        if journal:
                            P.finalize_exact_cycle(fixture.root, cycle_id=cycle_id, expected_binding=binding)
                        else:
                            # Manifest-only compatibility repair remains the
                            # normal recovery authority, then exact verifies it.
                            P.recover(fixture.root)
                        self.assertEqual(P.verify_finalized_cycle(fixture.root,
                            cycle_id=cycle_id, expected_binding=binding)["status"], "already-sealed")
                finally:
                    fixture.doCleanups()

    def test_journal_other_finalize_preserves_pending_cycle(self):
        self._recovery_case("other-finalize", journal=True)

    def test_journal_public_recover_preserves_pending_cycle(self):
        self._recovery_case("recover", journal=True)

    def test_journal_admit_shared_preserves_pending_cycle(self):
        self._recovery_case("admit-shared", journal=True)

    def test_manifest_only_same_finalize_preserves_pending_cycle(self):
        self._recovery_case("same-finalize", journal=False)

    def test_manifest_only_other_finalize_preserves_pending_cycle(self):
        self._recovery_case("other-finalize", journal=False)

    def test_manifest_only_public_recover_preserves_pending_cycle(self):
        self._recovery_case("recover", journal=False)

    def test_manifest_only_admit_shared_preserves_pending_cycle(self):
        self._recovery_case("admit-shared", journal=False)

    def test_prepare_rejects_real_published_journal_and_manifest(self):
        self.baseline.activate(
            self.root, repository_id=FIXTURE.REPO_ID,
            artifact_root_id=FIXTURE.ROOT_ID,
            w7={"campaign_id": "camp_" + "c" * 32},
        )
        result, _ = self._cycle()
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            self.baseline.finalize(
                self.root, cycle_id=result["cycle_id"],
                crash_after_manifest=True,
            )
        output = Path(result["cycle_dir"]) / "artifacts/plans/reviewer/report.md"
        with self.assertRaises(P.ProducerError) as caught:
            P.prepare_review_output_binding(
                self.root, cycle_id=result["cycle_id"],
                producer_id=result["producer_id"], attempt_id="att-late",
                review_output=output, capability="autopilot-code",
                unit="qa/code-review", worktree=PROJECT_ROOT,
            )
        self.assertEqual(caught.exception.code, ADMISSION_BLOCK)

    def test_prepare_treats_every_manifest_entry_kind_as_publication(self):
        makers = {
            "dangling-symlink": lambda path: path.symlink_to("missing-manifest"),
            "directory": lambda path: path.mkdir(),
            "fifo": lambda path: os.mkfifo(path),
            "unreadable-regular": lambda path: (
                path.write_text("not a manifest\n", encoding="utf-8"),
                path.chmod(0),
            ),
        }
        self.baseline.activate(
            self.root, repository_id=FIXTURE.REPO_ID,
            artifact_root_id=FIXTURE.ROOT_ID,
            w7={"campaign_id": "camp_" + "c" * 32},
        )
        for label, make in makers.items():
            with self.subTest(entry_kind=label):
                result, _ = self._cycle()
                manifest = Path(result["cycle_dir"]) / "manifest.json"
                make(manifest)
                output = Path(result["cycle_dir"]) / "artifacts/plans/reviewer/report.md"
                with self.assertRaises(P.ProducerError) as caught:
                    P.prepare_review_output_binding(
                        self.root, cycle_id=result["cycle_id"],
                        producer_id=result["producer_id"], attempt_id="att-entry",
                        review_output=output, capability="autopilot-code",
                        unit="qa/code-review", worktree=PROJECT_ROOT,
                    )
                self.assertEqual(caught.exception.code, ADMISSION_BLOCK)

    def test_acquire_rechecks_after_prepare_publication_race_under_lock(self):
        self.baseline.activate(
            self.root, repository_id=FIXTURE.REPO_ID,
            artifact_root_id=FIXTURE.ROOT_ID,
            w7={"campaign_id": "camp_" + "c" * 32},
        )
        result, _ = self._cycle()
        output, binding, identity, governed = self._review_authority(
            result, attempt="att-prepare-race",
        )
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            self.baseline.finalize(
                self.root, cycle_id=result["cycle_id"],
                crash_after_manifest=True,
            )
        with governed.open("r+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(P.ProducerError) as caught:
                P.review_lease_acquire(
                    self.root, cycle_id=result["cycle_id"],
                    attempt_id=binding["attempt_id"], review_output=output,
                    binding=binding, governed_identity=identity, jobs=self.jobs,
                )
        self.assertEqual(caught.exception.code, ADMISSION_BLOCK)
        self.assertFalse(P._review_lease_path(
            self.root, result["cycle_id"], binding["attempt_id"],
        ).exists())

    def test_v1_recovery_and_force_abandon_policy_stay_unchanged(self):
        self.baseline.activate(
            self.root, repository_id=FIXTURE.REPO_ID,
            artifact_root_id=FIXTURE.ROOT_ID,
            w7={"campaign_id": "camp_" + "c" * 32},
        )
        pending, _ = self._cycle()
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            self.baseline.finalize(
                self.root, cycle_id=pending["cycle_id"],
                crash_after_manifest=True,
            )
        self.assertEqual(P.review_lease_acquire(
            self.root, cycle_id=pending["cycle_id"], attempt_id="att-v1",
        )["status"], "acquired")
        self.assertIn(
            pending["cycle_id"], P.recover(self.root)["producer"]["rolled_forward"],
        )

        active, _ = self._cycle()
        output, binding, identity, governed = self._review_authority(
            active, attempt="att-force-abandon",
        )
        with governed.open("r+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(P.review_lease_acquire(
                self.root, cycle_id=active["cycle_id"],
                attempt_id=binding["attempt_id"], review_output=output,
                binding=binding, governed_identity=identity, jobs=self.jobs,
            )["status"], "acquired")
            forced = P.finalize(
                self.root, cycle_id=active["cycle_id"], state="abandoned",
                force_abandon_ignoring_lease=True, allow_open_route=True,
            )
        self.assertEqual(forced["status"], "sealed")
        self.assertEqual(forced["cycle_state"], "abandoned")


if __name__ == "__main__":
    unittest.main()
