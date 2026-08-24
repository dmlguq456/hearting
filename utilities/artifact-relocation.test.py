#!/usr/bin/env python3
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).with_name("artifact-relocation.py")
SPEC = Path(os.environ.get(
    "HEARTING_W7_SPEC",
    "/home/nas/user/Uihyeop/personal/hearting/.agent_reports/spec/artifact-path-contract",
))
RUNTIME_ROOT = Path("/home/nas/user/Uihyeop/personal/hearting/.agent_reports")

spec = importlib.util.spec_from_file_location("artifact_relocation_tested", TOOL)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

REAL_EVIDENCE_AVAILABLE = (
    (SPEC / "_internal/research/w6-relocation-baseline.jsonl").is_file()
    and (SPEC / "_internal/research/w6-relocation-manifest.jsonl").is_file()
)


def run(*args):
    return subprocess.run(["python3", str(TOOL), *args], text=True, capture_output=True)


def replay_args(base: Path):
    return [
        "replay",
        "--baseline", str(SPEC / "_internal/research/w6-relocation-baseline.jsonl"),
        "--manifest", str(SPEC / "_internal/research/w6-relocation-manifest.jsonl"),
        "--verification", str(SPEC / "_internal/research/w6-relocation-verification.json"),
        "--decision-table", str(SPEC / "_internal/research/w6-relocation-decision-table.json"),
        "--corrected-brief", str(SPEC / "_internal/research/w6-relocation.md"),
        "--authority-route", str(RUNTIME_ROOT / ".runtime/routes/rt-f356e0d8f0eda6e2.json"),
        "--corrected-review", str(SPEC / "_internal/reviews/w6-relocation-corrected-review.md"),
        "--corrected-verdict", str(SPEC / "_internal/reviews/verdict.rt-f356e0d8f0eda6e2.json"),
        "--prd", str(SPEC / "prd.md"),
        "--w6-commit", "9b902985da508f9ce808d15f134239d613a5ce31",
        "--output", str(base / "replay.json"),
    ]


@unittest.skipUnless(REAL_EVIDENCE_AVAILABLE, "frozen W6 evidence not present in this environment")
class GoldenReplayTest(unittest.TestCase):
    def test_golden_replay_exact_bindings_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            result = run(*replay_args(base))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            body = json.loads((base / "replay.json").read_text())
            self.assertEqual(body["status"], "pass")
            self.assertEqual(body["manifest_rows"], 19148)
            self.assertEqual(body["baseline_rows"], 19148)
            self.assertEqual(body["corrected_rows"], 9)
            self.assertEqual(body["decision_class_count"], 21)
            self.assertEqual(body["locator_state_counts"], {"exact": 390, "template": 5631, "none": 13127})
            self.assertEqual(body["preservation_exact_rows"], 390)
            self.assertEqual(body["approved_moving_row_count"], 0)
            self.assertEqual(body["reconstruction_sha256"],
                              "995b182680ddad507cb8a1f421db59f115c57cc2fe9c49d8ed693cd76c6eb0f1")
            self.assertEqual(body["reconstruction_bytes"], 1999918)
            self.assertEqual(body["population_comparison"],
                              {"missing": 0, "extra": 0, "duplicate_baseline": 0,
                               "duplicate_manifest": 0, "kind_mismatch": 0})

    def test_two_replays_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            out1, out2 = base / "r1.json", base / "r2.json"
            args1 = replay_args(base)[:-1] + [str(out1)]
            args2 = replay_args(base)[:-1] + [str(out2)]
            self.assertEqual(run(*args1).returncode, 0)
            self.assertEqual(run(*args2).returncode, 0)
            self.assertEqual(out1.read_bytes(), out2.read_bytes())

    def test_manifest_digest_mismatch_is_evidence_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            bad_manifest = base / "manifest.jsonl"
            bad_manifest.write_bytes((SPEC / "_internal/research/w6-relocation-manifest.jsonl").read_bytes() + b"\n")
            args = replay_args(base)
            args[args.index("--manifest") + 1] = str(bad_manifest)
            result = run(*args)
            self.assertEqual(result.returncode, 66, result.stdout)


class ReconstructionUnitTest(unittest.TestCase):
    def test_reconstruction_excludes_root_summary_and_sorts_by_utf8_bytes(self):
        rows = [
            {"record_type": "dir", "path": "b"},
            {"record_type": "file", "path": "a"},
            {"record_type": "root_summary", "path": "."},
        ]
        body, count = M.reconstruct(rows)
        self.assertEqual(count, 2)
        self.assertEqual(
            body,
            b'{"kind":"file","source_locator":"a"}\n{"kind":"directory","source_locator":"b"}\n',
        )

    def test_reconstruction_rejects_unknown_record_type(self):
        with self.assertRaises(ValueError):
            M.reconstruct([{"record_type": "weird", "path": "x"}])


class PopulationCompareUnitTest(unittest.TestCase):
    def test_missing_extra_and_kind_mismatch(self):
        baseline = [{"record_type": "file", "path": "a"}, {"record_type": "dir", "path": "b"}]
        manifest = [
            {"source_locator": {"root_relative_path": "a"}, "before": {"kind": "directory"}},
            {"source_locator": {"root_relative_path": "c"}, "before": {"kind": "file"}},
        ]
        result = M.population_compare(baseline, manifest)
        self.assertEqual(result["counts"]["missing"], 1)
        self.assertEqual(result["counts"]["extra"], 1)
        self.assertEqual(result["counts"]["kind_mismatch"], 1)

    def test_duplicate_rows_are_counted_not_silently_merged(self):
        baseline = [{"record_type": "file", "path": "a"}, {"record_type": "file", "path": "a"}]
        manifest = [
            {"source_locator": {"root_relative_path": "a"}, "before": {"kind": "file"}},
            {"source_locator": {"root_relative_path": "a"}, "before": {"kind": "file"}},
        ]
        result = M.population_compare(baseline, manifest)
        self.assertEqual(result["counts"]["duplicate_baseline"], 1)
        self.assertEqual(result["counts"]["duplicate_manifest"], 1)


class DecisionTableUnitTest(unittest.TestCase):
    def _valid_table(self):
        classes = []
        names = list(M.EXPECTED_CORRECTED_DISPOSITIONS.keys())  # placeholder, replaced below
        names = [
            "live_runtime", "open_runtime", "locked_runtime", "external_symlink_containment",
            "destination_path_collision", "case_collision", "unicode_normalization_collision",
            "parent_child_overlap", "destination_preexistence", "digest_drift", "kind_drift",
            "mode_drift", "broken_link", "orphan_ownership", "duplicate_ownership",
            "ambiguous_ownership", "empty_directory", "after_cutoff_arrival", "partial_execution",
            "rollback_conflict", "unclassified",
        ]
        for name in names:
            outcome, retryability, evidence, tombstone, rollback = M.DECISION_CLASSES[name]
            classes.append({
                "class": name, "outcome": outcome, "apply_eligible": False,
                "retryability": retryability, "required_evidence_or_receipt": evidence,
                "tombstone_rule": tombstone, "rollback_action": rollback,
            })
        return {
            "classes": classes, "outcome_enum": ["hold", "refuse", "quarantine", "escalate"],
            "schema_version": 1, "silent_delete_or_overwrite_allowed": False,
            "table_id": "w6-exception-decision-v1",
            "unknown_input": {"apply_eligible": False, "outcome": "refuse", "reason": "refuse_unclassified_exception",
                              "required_evidence_or_receipt": "unknown-class refusal receipt with raw enum preserved",
                              "retryability": "taxonomy_update_required", "rollback_action": "no-op; source remains byte-identical",
                              "tombstone_rule": "required"},
        }

    def test_valid_table_passes(self):
        names = M.decision_table_check(self._valid_table())
        self.assertEqual(len(names), 21)

    def test_duplicate_class_name_refused(self):
        table = self._valid_table()
        table["classes"][0]["class"] = table["classes"][1]["class"]
        with self.assertRaises(ValueError):
            M.decision_table_check(table)

    def test_apply_eligible_true_refused(self):
        table = self._valid_table()
        table["classes"][0]["apply_eligible"] = True
        with self.assertRaises(ValueError):
            M.decision_table_check(table)

    def test_missing_unclassified_refused(self):
        table = self._valid_table()
        table["classes"] = [c for c in table["classes"] if c["class"] != "unclassified"]
        table["classes"].append({**table["classes"][0], "class": "extra_filler"})
        with self.assertRaises(ValueError):
            M.decision_table_check(table)

    def test_arbitrary_required_value_refused(self):
        table = self._valid_table()
        table["classes"][0]["retryability"] = "x"
        with self.assertRaises(ValueError):
            M.decision_table_check(table)


class JsonlSchemaTest(unittest.TestCase):
    def test_bom_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.jsonl"
            path.write_bytes(b"\xef\xbb\xbf" + b'{"a":1}\n')
            with self.assertRaises(ValueError):
                M.read_jsonl_rows(path)

    def test_missing_trailing_lf_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.jsonl"
            path.write_bytes(b'{"a":1}')
            with self.assertRaises(ValueError):
                M.read_jsonl_rows(path)

    def test_blank_line_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.jsonl"
            path.write_bytes(b'{"a":1}\n\n{"a":2}\n')
            with self.assertRaises(ValueError):
                M.read_jsonl_rows(path)

    def test_non_object_row_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.jsonl"
            path.write_bytes(b'[1,2]\n')
            with self.assertRaises(ValueError):
                M.read_jsonl_rows(path)


class IdentityResolveTest(unittest.TestCase):
    def _manifest(self, base: Path, n=3):
        path = base / "manifest.jsonl"
        rows = [
            json.dumps({"identity": {"state": "unissued"}, "source_locator": {"root_relative_path": f"row-{i}"}})
            for i in range(n)
        ]
        path.write_text("\n".join(rows) + "\n")
        return path

    def test_missing_ledger_is_typed_blocker_with_exact_unresolved_count(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest = self._manifest(base, 5)
            result = run("resolve", "--manifest", str(manifest),
                          "--identity-ledger", str(base / "missing.json"),
                          "--output", str(base / "out.json"))
            self.assertEqual(result.returncode, 66, result.stderr)
            body = json.loads((base / "out.json").read_text())
            self.assertEqual(body["blocker"], "identity_ledger_missing")
            self.assertEqual(body["resolved_count"], 0)
            self.assertEqual(body["unresolved_count"], 5)
            self.assertNotIn("target_digest", body)

    def test_wrong_schema_ledger_refused_as_identity_error(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest = self._manifest(base, 1)
            ledger = base / "ledger.json"
            ledger.write_text(json.dumps({"schema_version": "wrong", "entries": []}))
            result = run("resolve", "--manifest", str(manifest), "--identity-ledger", str(ledger),
                          "--output", str(base / "out.json"))
            self.assertEqual(result.returncode, 65, result.stderr)

    def test_empty_ledger_is_incomplete_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest = self._manifest(base, 2)
            ledger = base / "ledger.json"
            ledger.write_text(json.dumps({"schema_version": M.IDENTITY_LEDGER_SCHEMA, "namespace": "x",
                                          "authority_receipt_sha256": "a" * 64,
                                          "source_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                                          "entries": []}))
            result = run("resolve", "--manifest", str(manifest), "--identity-ledger", str(ledger), "--output", str(base / "out.json"))
            self.assertEqual(result.returncode, 65)

    def test_valid_ledger_resolves_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest = self._manifest(base, 1)
            manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
            entry = {
                "id_kind": "artifact_root", "stable_id": "root_" + ("a" * 32),
                "state": "issued", "authority_receipt_sha256": "b" * 64, "source_row_id": "row-0",
            }
            ledger = base / "ledger.json"
            ledger.write_text(json.dumps({
                "schema_version": "artifact-relocation-identity-ledger/v1",
                "namespace": "w7-fixture", "authority_receipt_sha256": "c" * 64,
                "source_manifest_sha256": manifest_sha, "entries": [entry],
            }))
            out1, out2 = base / "out1.json", base / "out2.json"
            r1 = run("resolve", "--manifest", str(manifest), "--identity-ledger", str(ledger), "--output", str(out1))
            r2 = run("resolve", "--manifest", str(manifest), "--identity-ledger", str(ledger), "--output", str(out2))
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertEqual(out1.read_bytes(), out2.read_bytes())
            body = json.loads(out1.read_text())
            self.assertEqual(body["resolved_count"], 1)
            self.assertEqual(body["unresolved_count"], 0)

    def test_duplicate_stable_id_with_different_binding_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest = self._manifest(base, 2)
            manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
            stable = "root_" + ("a" * 32)
            entries = [
                {"id_kind": "artifact_root", "stable_id": stable, "state": "issued",
                 "authority_receipt_sha256": "b" * 64, "source_row_id": "row-0"},
                {"id_kind": "artifact_root", "stable_id": stable, "state": "issued",
                 "authority_receipt_sha256": "b" * 64, "source_row_id": "row-1"},
            ]
            ledger = base / "ledger.json"
            ledger.write_text(json.dumps({
                "schema_version": "artifact-relocation-identity-ledger/v1",
                "namespace": "w7-fixture", "authority_receipt_sha256": "c" * 64,
                "source_manifest_sha256": manifest_sha, "entries": entries,
            }))
            result = run("resolve", "--manifest", str(manifest), "--identity-ledger", str(ledger),
                          "--output", str(base / "out.json"))
            self.assertEqual(result.returncode, 65, result.stderr)

    def test_migration_id_must_match_reused_d30_formula(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest = self._manifest(base, 1)
            manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
            legacy = "lk_" + ("f" * 32)
            entry = {
                "id_kind": "artifact_root", "stable_id": "root_" + ("a" * 32),
                "state": "issued", "authority_receipt_sha256": "b" * 64, "source_row_id": "row-0",
                "legacy_key_id": legacy, "migration_id": "migration:not-the-real-formula",
            }
            ledger = base / "ledger.json"
            ledger.write_text(json.dumps({
                "schema_version": "artifact-relocation-identity-ledger/v1",
                "namespace": "w7-fixture", "authority_receipt_sha256": "c" * 64,
                "source_manifest_sha256": manifest_sha, "entries": [entry],
            }))
            result = run("resolve", "--manifest", str(manifest), "--identity-ledger", str(ledger),
                          "--output", str(base / "out.json"))
            self.assertEqual(result.returncode, 65, result.stderr)


class CompareCheckTest(unittest.TestCase):
    def test_identical_bytes_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            left, right = base / "l", base / "r"
            left.write_bytes(b"same")
            right.write_bytes(b"same")
            result = run("check", "--compare-label", "x", "--left", str(left), "--right", str(right),
                         "--output", str(base / "out.json"))
            self.assertEqual(result.returncode, 0)

    def test_differing_bytes_is_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            left, right, out = base / "left", base / "right", base / "out"
            left.write_bytes(b"a")
            right.write_bytes(b"b")
            result = run("check", "--compare-label", "x", "--left", str(left), "--right", str(right),
                         "--output", str(out))
            self.assertEqual(result.returncode, 75)


class RehearsalEffectTest(unittest.TestCase):
    def test_dry_run_is_blocked_zero_row(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out.json"
            result = run("rehearse", "--mode", "dry-run", "--output", str(out))
            self.assertEqual(result.returncode, 78)
            body = json.loads(out.read_text())
            self.assertEqual(body["approved_moving_row_count"], 0)

    def test_fixture_apply_is_repeatable_source_preserved_and_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            results = []
            for i in (1, 2):
                work = base / f"work{i}"
                backup = base / f"backup{i}"
                work.mkdir()
                backup.mkdir()
                out = base / f"out{i}.json"
                journal = base / f"journal{i}.jsonl"
                inverse = base / f"inverse{i}.jsonl"
                seal = base / f"seal{i}.json"
                result = run("rehearse", "--mode", "apply", "--fixture-template", "synthetic-nonempty-v1",
                             "--work-root", str(work), "--backup-root", str(backup), "--output", str(out),
                             "--journal", str(journal), "--inverse-journal", str(inverse), "--backup-seal", str(seal))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((work / "fixture-source" / "payload.txt").is_file())
                results.append((out.read_bytes(), journal.read_bytes(), inverse.read_bytes(), seal.read_bytes()))
            self.assertEqual(results[0], results[1])

    def test_apply_never_overwrites_preexisting_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            work, backup = base / "work", base / "backup"
            work.mkdir()
            backup.mkdir()
            (work / "fixture-destination").mkdir()
            (work / "fixture-destination" / "payload.txt").write_bytes(b"preexisting")
            result = run("rehearse", "--mode", "apply", "--fixture-template", "synthetic-nonempty-v1",
                         "--work-root", str(work), "--backup-root", str(backup),
                         "--output", str(base / "out.json"), "--journal", str(base / "j.jsonl"),
                         "--inverse-journal", str(base / "i.jsonl"), "--backup-seal", str(base / "s.json"))
            self.assertEqual(result.returncode, 73)
            self.assertEqual((work / "fixture-destination" / "payload.txt").read_bytes(), b"preexisting")

    def test_rollback_replays_inverse_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            work, backup = base / "work", base / "backup"
            work.mkdir()
            backup.mkdir()
            run("rehearse", "--mode", "apply", "--fixture-template", "synthetic-nonempty-v1",
                "--work-root", str(work), "--backup-root", str(backup), "--output", str(base / "a.json"),
                "--journal", str(base / "journal.jsonl"), "--inverse-journal", str(base / "inverse.jsonl"),
                "--backup-seal", str(base / "seal.json"))
            out1, out2 = base / "r1.json", base / "r2.json"
            r1 = run("rehearse", "--mode", "rollback", "--work-root", str(work),
                     "--journal", str(base / "journal.jsonl"), "--inverse-journal", str(base / "inverse.jsonl"),
                     "--backup-seal", str(base / "seal.json"), "--output", str(out1))
            r2 = run("rehearse", "--mode", "rollback", "--work-root", str(work),
                     "--journal", str(base / "journal.jsonl"), "--inverse-journal", str(base / "inverse.jsonl"),
                     "--backup-seal", str(base / "seal.json"), "--output", str(out2))
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertNotEqual(r2.returncode, 0)
            self.assertIn("restore_authority_required", r2.stdout)

    def test_rollback_reconstructs_sealed_fixture_in_fresh_work_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            apply_work, backup = base / "apply-work", base / "backup"
            apply_work.mkdir()
            backup.mkdir()
            journal = base / "journal.jsonl"
            inverse = base / "inverse.jsonl"
            seal = base / "seal.json"
            applied = run(
                "rehearse", "--mode", "apply",
                "--fixture-template", "synthetic-nonempty-v1",
                "--work-root", str(apply_work), "--backup-root", str(backup),
                "--output", str(base / "apply.json"),
                "--journal", str(journal), "--inverse-journal", str(inverse),
                "--backup-seal", str(seal),
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)

            receipts = []
            for ordinal in (1, 2):
                rollback_work = base / f"rollback-{ordinal}"
                rollback_work.mkdir()
                output = base / f"rollback-{ordinal}.json"
                result = run(
                    "rehearse", "--mode", "rollback",
                    "--fixture-template", "synthetic-nonempty-v1",
                    "--work-root", str(rollback_work),
                    "--journal", str(journal), "--inverse-journal", str(inverse),
                    "--backup-seal", str(seal), "--output", str(output),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((rollback_work / "fixture-source" / "payload.txt").is_file())
                self.assertFalse((rollback_work / "fixture-destination").exists())
                receipts.append(output.read_bytes())
            self.assertEqual(receipts[0], receipts[1])

    def test_live_hearting_root_rejected_by_fixture_rehearsal(self):
        result = run("rehearse", "--mode", "apply", "--fixture-template", "synthetic-nonempty-v1",
                     "--work-root", str(RUNTIME_ROOT), "--backup-root", "/tmp",
                     "--output", "/tmp/w7-reject-out.json", "--journal", "/tmp/w7-reject-j.jsonl",
                     "--inverse-journal", "/tmp/w7-reject-i.jsonl", "--backup-seal", "/tmp/w7-reject-s.json")
        self.assertEqual(result.returncode, 64)
        self.assertIn("live-root-rejected", result.stdout)

    def test_fresh_rollback_rejects_tampered_seal_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            apply_work, backup = base / "apply-work", base / "backup"
            apply_work.mkdir()
            backup.mkdir()
            journal, inverse, seal = base / "j.jsonl", base / "i.jsonl", base / "s.json"
            applied = run(
                "rehearse", "--mode", "apply", "--fixture-template", "synthetic-nonempty-v1",
                "--work-root", str(apply_work), "--backup-root", str(backup),
                "--output", str(base / "apply.json"), "--journal", str(journal),
                "--inverse-journal", str(inverse), "--backup-seal", str(seal),
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            bound = json.loads(seal.read_text())
            bound["backup_sha256"] = "0" * 64
            seal.write_text(json.dumps(bound))
            rollback_work = base / "rollback"
            rollback_work.mkdir()
            result = run(
                "rehearse", "--mode", "rollback", "--fixture-template", "synthetic-nonempty-v1",
                "--work-root", str(rollback_work), "--journal", str(journal),
                "--inverse-journal", str(inverse), "--backup-seal", str(seal),
                "--output", str(base / "rollback.json"),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((rollback_work / "fixture-source").exists())

    def test_fresh_rollback_rejects_tampered_exact_fixture_records(self):
        mutations = (
            ("before_lstat", lambda row: row["before_lstat"].update(mode=0)),
            ("after_lstat", lambda row: row["after_lstat"].update(mode=0)),
            ("inverse_missing", lambda row: row.pop("action")),
            ("inverse_extra", lambda row: row.update(extra=True)),
            ("seal_missing", lambda row: row.pop("exclusive")),
            ("seal_extra", lambda row: row.update(extra=True)),
        )
        for name, mutate in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                apply_work, backup = base / "apply-work", base / "backup"
                apply_work.mkdir()
                backup.mkdir()
                journal, inverse, seal = base / "j.jsonl", base / "i.jsonl", base / "s.json"
                applied = run(
                    "rehearse", "--mode", "apply", "--fixture-template", "synthetic-nonempty-v1",
                    "--work-root", str(apply_work), "--backup-root", str(backup),
                    "--output", str(base / "apply.json"), "--journal", str(journal),
                    "--inverse-journal", str(inverse), "--backup-seal", str(seal),
                )
                self.assertEqual(applied.returncode, 0, applied.stderr)
                if name.startswith("before") or name.startswith("after"):
                    record = json.loads(journal.read_text().splitlines()[0])
                    mutate(record)
                    journal.write_text(json.dumps(record) + "\n")
                elif name.startswith("inverse"):
                    record = json.loads(inverse.read_text().splitlines()[0])
                    mutate(record)
                    inverse.write_text(json.dumps(record) + "\n")
                else:
                    record = json.loads(seal.read_text())
                    mutate(record)
                    seal.write_text(json.dumps(record))
                rollback_work = base / "rollback"
                rollback_work.mkdir()
                result = run(
                    "rehearse", "--mode", "rollback", "--fixture-template", "synthetic-nonempty-v1",
                    "--work-root", str(rollback_work), "--journal", str(journal),
                    "--inverse-journal", str(inverse), "--backup-seal", str(seal),
                    "--output", str(base / "rollback.json"),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((rollback_work / "fixture-source").exists())


class LiveRootSafetyTest(unittest.TestCase):
    def test_status_pass_without_authority_is_typed_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); package = base / "package.json"
            package.write_text(json.dumps({"status": "pass"}))
            result = run("apply", "--artifact-root", str(RUNTIME_ROOT), "--package", str(package), "--receipt-stdout")
            self.assertEqual(result.returncode, 78)
            body = json.loads(result.stdout)
            self.assertEqual(body["blocker"], "apply_authority_invalid")
            self.assertEqual(body["mutations"], 0)
            self.assertEqual(body["write_audit"]["write_attempt_count"], 0)

    def test_apply_against_live_root_constructs_no_effect_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package = base / "package.json"
            package.write_text(json.dumps({"status": "blocked"}))
            jobs = base / "jobs.log"
            jobs.write_text("")
            lock = base / "jobs.log.lock"
            lock.write_text("")
            result = run("apply", "--artifact-root", str(RUNTIME_ROOT), "--package", str(package),
                         "--dispatch-jobs", str(jobs), "--dispatch-lock", str(lock), "--receipt-stdout")
            self.assertIn(result.returncode, (78, 75))
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["mutations"], 0)
            self.assertEqual(receipt["write_audit"]["effect_factory_calls"], 0)
            self.assertEqual(receipt["write_audit"]["effect_calls"], 0)
            self.assertEqual(receipt["write_audit"]["write_attempt_count"], 0)
            if result.returncode == 78:
                self.assertEqual(receipt["write_audit"]["scope_before_sha256"],
                                  receipt["write_audit"]["scope_after_sha256"])
            else:
                self.assertNotEqual(receipt["write_audit"]["scope_before_sha256"],
                                     receipt["write_audit"]["scope_after_sha256"])

    def test_apply_with_no_package_file_is_still_write_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            result = run("apply", "--artifact-root", str(RUNTIME_ROOT),
                         "--package", str(base / "does-not-exist.json"), "--receipt-stdout")
            self.assertIn(result.returncode, (78, 75))
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["mutations"], 0)
            self.assertIsNone(receipt["package_status"])


class HandoffTest(unittest.TestCase):
    def test_blocked_package_cannot_open_terminal_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package = base / "package.json"
            package.write_text(json.dumps({"status": "blocked", "terminal": False}))
            result = run("handoff", "--package", str(package), "--receipt-stdout")
            self.assertEqual(result.returncode, 78)
            body = json.loads(result.stdout)
            self.assertFalse(body["terminal"])
            self.assertFalse(body["terminal_marker_present"])
            self.assertEqual(body["w8_status"], "blocked")


class DeltaTest(unittest.TestCase):
    def _baseline(self, base: Path):
        path = base / "baseline.jsonl"
        path.write_text('{"path":"a.txt","record_type":"file"}\n{"path":"gone.txt","record_type":"file"}\n')
        return path

    def test_freeze_then_replay_cutoff_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            (root / "a.txt").write_bytes(b"unchanged")
            baseline = self._baseline(base)
            cutoff = base / "cutoff.json"
            out1 = base / "delta1.jsonl"
            out2 = base / "delta2.jsonl"
            r1 = run("delta", "--baseline", str(baseline), "--artifact-root", str(root),
                     "--freeze-cutoff", str(cutoff), "--output", str(out1))
            self.assertIn(r1.returncode, (0, 75))
            r2 = run("delta", "--baseline", str(baseline), "--artifact-root", str(root),
                     "--cutoff", str(cutoff), "--output", str(out2))
            self.assertIn(r2.returncode, (0, 75))
            self.assertEqual(out1.read_bytes(), out2.read_bytes())

    def test_arrival_is_typed_and_self_write_vs_third_party_distinguished(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            self_dir = root / "self"
            root.mkdir()
            self_dir.mkdir()
            (root / "a.txt").write_bytes(b"unchanged")
            (root / "third-party-new.txt").write_bytes(b"arrived")
            (self_dir / "self-new.txt").write_bytes(b"arrived")
            baseline = self._baseline(base)
            cutoff = base / "cutoff.json"
            out = base / "delta.jsonl"
            run("delta", "--baseline", str(baseline), "--artifact-root", str(root),
                "--self-write-root", str(self_dir), "--freeze-cutoff", str(cutoff), "--output", str(out))
            frozen = json.loads(cutoff.read_text())
            by_path = {row["path"]: row for row in frozen["rows"]}
            self.assertEqual(by_path["third-party-new.txt"]["producer_class"], "third_party_arrival")
            self.assertEqual(by_path["self/self-new.txt"]["producer_class"], "self_write")
            self.assertEqual(by_path["gone.txt"]["classification"], "after_cutoff_missing")

    def test_all_delta_rows_are_exactly_one_of_five_typed_classes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            baseline = self._baseline(base)
            out = base / "delta.jsonl"
            run("delta", "--baseline", str(baseline), "--artifact-root", str(root),
                "--freeze-cutoff", str(base / "cutoff.json"), "--output", str(out))
            for line in out.read_text().splitlines():
                row = json.loads(line)
                self.assertIn(row["classification"], M.DELTA_CLASSES)
                self.assertIn(row["producer_class"], ("self_write", "third_party_arrival"))

    def test_same_size_byte_change_is_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = base / "root"; root.mkdir()
            (root / "a.txt").write_bytes(b"aa")
            baseline = base / "baseline.jsonl"; baseline.write_text('{"path":"a.txt","record_type":"file"}\n')
            cutoff = base / "cutoff.json"
            run("delta", "--baseline", str(baseline), "--artifact-root", str(root), "--freeze-cutoff", str(cutoff), "--output", str(base / "one"))
            (root / "a.txt").write_bytes(b"bb")
            result = run("delta", "--baseline", str(baseline), "--artifact-root", str(root), "--cutoff", str(cutoff), "--output", str(base / "two"))
            self.assertEqual(result.returncode, 69)


if __name__ == "__main__":
    unittest.main()
