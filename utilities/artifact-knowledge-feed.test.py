#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "artifact_knowledge_feed",
    Path(__file__).with_name("artifact-knowledge-feed.py"),
)
feed = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(feed)


def fixture_root(base: Path, *, all_d23: bool = False) -> Path:
    root = base / "artifacts"
    (root / "plans").mkdir(parents=True)
    if all_d23:
        for name in ("research", "spec", "documents"):
            (root / name).mkdir()
    return root


def cycle(
    root: Path,
    name: str,
    *,
    summary_name: str | None = None,
    summary: bytes = b"summary",
    manifest: bool = False,
    source: str = "file",
) -> Path:
    path = root / "plans" / name
    path.mkdir(parents=True)
    if source == "file":
        (path / "plan.md").write_text("plan", encoding="utf-8")
    elif source == "directory":
        (path / "plan.md").mkdir()
    if summary_name is not None:
        (path / summary_name).write_bytes(summary)
    if manifest:
        (path / "manifest.json").write_text("{}", encoding="utf-8")
    return path


def fixed_mapping(root: Path, selection: str = "plans") -> bytes:
    locators = feed.d23_population(root, selection)
    records = [
        feed._record(locator, "lk_" + f"{ordinal:032x}")
        for ordinal, locator in enumerate(locators, 1)
    ]
    return feed.mapping_bytes(records)


def run_main(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = feed.main(argv)
    return status, stdout.getvalue(), stderr.getvalue()


class FeedUnitTests(unittest.TestCase):
    def test_opaque_migration_identity_does_not_use_locator(self):
        key = "lk_" + "ab" * 16
        self.assertEqual(feed.migration_id(key), feed.migration_id(key))
        self.assertNotEqual(feed.migration_id(key), feed.migration_id("lk_" + "cd" * 16))
        with self.assertRaises(feed.FeedError):
            feed.migration_id("plans/a")

    def test_d23_bucket_table_fallbacks_and_exclusions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary), all_d23=True)
            (root / "plans/a").mkdir()
            (root / "plans/_scratch/ignored").mkdir(parents=True)
            (root / "research/r").mkdir()
            (root / "spec/component").mkdir()
            (root / "spec/_internal").mkdir()
            (root / "spec/prd.md").write_text("spec", encoding="utf-8")
            (root / "documents/d").mkdir()
            (root / "documents/loose.md").write_text("doc", encoding="utf-8")
            (root / "plans/link").symlink_to(root / "plans/a", target_is_directory=True)

            self.assertEqual(feed.d23_population(root, "plans"), ["plans/a/"])
            self.assertEqual(
                feed.d23_population(root, "all-d23"),
                [
                    "documents/_loose-documents/",
                    "documents/d/",
                    "plans/a/",
                    "research/r/",
                    "spec/_unscoped-legacy-component/",
                    "spec/component/",
                ],
            )

    def test_exact_d25_pattern_precedence_and_a114_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            cycle(root, "missing")
            nested = cycle(root, "nested")
            (nested / "child").mkdir()
            (nested / "child/summary.md").write_text("nested", encoding="utf-8")
            cycle(
                root,
                "complete",
                summary_name="PIPELINE_SUMMARY.MD",
                manifest=True,
            )
            cycle(root, "empty", summary_name="summary.yaml", summary=b" \n")
            cycle(root, "unreadable", summary_name="report.json", summary=b"\xff")
            cycle(root, "manifest", summary_name="overview.md")
            cycle(
                root,
                "source",
                summary_name="index.yml",
                manifest=True,
                source="directory",
            )

            result = feed.build_feed(root, "plans", fixed_mapping(root), "fixture-seal")
            by_name = {
                row["canonical_source_key"].split("/")[1]: row
                for row in result["rows"]
            }
            self.assertEqual((result["G"], result["E"]), (2, 2))
            self.assertEqual(result["row_count"], result["population_count"])
            self.assertEqual(result["outcome"], "partial")
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(by_name["missing"]["reason"], "entry-summary-missing")
            self.assertEqual(by_name["nested"]["reason"], "entry-summary-missing")
            self.assertEqual(by_name["empty"]["reason"], "summary-generation-failed")
            self.assertEqual(by_name["unreadable"]["reason"], "source-unreadable")
            self.assertEqual(by_name["manifest"]["reason"], "manifest-missing")
            self.assertEqual(by_name["source"]["reason"], "source-unreadable")
            self.assertFalse(by_name["complete"]["degraded"])
            self.assertEqual(by_name["complete"]["reason"], None)
            self.assertEqual(
                result["entry_summary_pattern"],
                feed.PATTERN_TEXT,
            )

    def test_rows_expose_producer_evidence_without_consumer_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            cycle(root, "missing")
            result = feed.build_feed(root, "plans", fixed_mapping(root), "seal")
            row = result["rows"][0]
            self.assertEqual(row["entry_kind"], "path_only_degraded")
            self.assertEqual(row["canonical_raw_locator"], "plans/missing/")
            self.assertEqual(row["mapping_version"], feed.MAPPING_VERSION)
            self.assertIn("source_mtime", row["freshness"])
            self.assertRegex(row["integrity"]["source_digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(result["consumer_required"], sorted(feed.CONSUMER_REQUIRED))
            self.assertTrue(feed.CONSUMER_REQUIRED.isdisjoint(result))

    def test_same_mapping_and_seal_are_byte_identical_without_randomness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            cycle(root, "a")
            mapping = fixed_mapping(root)
            with mock.patch.object(feed.secrets, "token_bytes", side_effect=AssertionError("random scan")):
                first = feed.build_feed(root, "plans", mapping, "same")
                second = feed.build_feed(root, "plans", mapping, "same")
            self.assertEqual(feed.canonical(first), feed.canonical(second))

    def test_source_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            cycle(root, "a")
            mapping = fixed_mapping(root)
            rows, observed_digest = feed._inventory(root, "plans")
            with mock.patch.object(
                feed,
                "_inventory",
                side_effect=[(rows, observed_digest), (rows, "sha256:" + "0" * 64)],
            ):
                with self.assertRaises(feed.FeedError) as caught:
                    feed.build_feed(root, "plans", mapping, "seal")
            self.assertEqual(caught.exception.exit_code, feed.EXIT_DRIFT)

    def test_mapping_validation_rejects_collisions_drift_and_noncanonical_bytes(self):
        a = feed._record("plans/a/", "lk_" + "01" * 16)
        b = feed._record("plans/b/", "lk_" + "02" * 16)
        encoded = feed.mapping_bytes([b, a])
        parsed, mapping_digest = feed.validate_mapping(encoded, ["plans/a/", "plans/b/"])
        self.assertEqual(len(parsed), 2)
        self.assertEqual(mapping_digest, feed.digest(encoded))
        with self.assertRaises(feed.FeedError):
            feed.validate_mapping(encoded[:-1])
        with self.assertRaises(feed.FeedError):
            feed.validate_mapping(encoded, ["plans/a/"])
        duplicate = copy.deepcopy(b)
        duplicate["legacy_key_id"] = a["legacy_key_id"]
        duplicate["migration_id"] = a["migration_id"]
        with self.assertRaises(feed.FeedError):
            feed.validate_mapping(feed.mapping_bytes([a, duplicate]))

    def test_validator_rejects_consumer_values_and_conflict_degraded_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixture_root(Path(temporary))
            cycle(root, "a")
            result = feed.build_feed(root, "plans", fixed_mapping(root), "seal")
            consumer = copy.deepcopy(result)
            consumer["namespace_id"] = "ns-forbidden"
            with self.assertRaises(feed.FeedError):
                feed.validate_feed(consumer)
            conflict = copy.deepcopy(result)
            conflict["rows"][0]["conflict"] = True
            with self.assertRaises(feed.FeedError):
                feed.validate_feed(conflict)
            forged = copy.deepcopy(result)
            forged["rows"][0]["producer_evidence"]["summary_state"] = "usable"
            with self.assertRaises(feed.FeedError):
                feed.validate_feed(forged)


class LayeredLayoutTests(unittest.TestCase):
    """W7D: buckets are observed at cycle, shared, and legacy layouts."""

    def _cycle_root(self, base: Path) -> tuple[Path, Path]:
        root = base / "artifacts"
        campaign_id = "camp_" + "a" * 32
        cycle_id = "cyc_" + "b" * 32
        campaign = root / "campaigns" / campaign_id
        cyc_root = campaign / "cycles" / cycle_id
        cyc = cyc_root / "artifacts"
        (cyc / "plans" / "2026-08-26_relocated").mkdir(parents=True)
        (cyc / "plans" / "2026-08-26_relocated" / "plan.md").write_text("plan", encoding="utf-8")
        (cyc / "plans" / "2026-08-26_relocated" / "final_report.md").write_text("done", encoding="utf-8")
        (campaign / "campaign.json").write_text(
            json.dumps({"campaign_id": campaign_id, "cycles": [cycle_id]}),
            encoding="utf-8",
        )
        (cyc_root / "manifest.json").write_text(
            json.dumps({"cycle": {"cycle_id": cycle_id}}),
            encoding="utf-8",
        )
        return root, cyc

    def test_cycle_layout_alone_satisfies_the_required_plans_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self._cycle_root(Path(tmp))
            population = feed.d23_population(root, "plans")
            self.assertEqual(
                population,
                ["campaigns/camp_" + "a" * 32 + "/cycles/cyc_" + "b" * 32 + "/artifacts/plans/2026-08-26_relocated/"],
            )
            produced = feed.build_feed(root, "plans", fixed_mapping(root), "2026-08-26T00:00:00Z")
            self.assertEqual(produced["population_count"], 1)
            self.assertEqual(produced["rows"][0]["producer_evidence"]["summary_state"], "usable")

    def test_legacy_bucket_is_a_read_only_fallback_after_cycle_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self._cycle_root(Path(tmp))
            (root / "plans" / "2026-01-01_legacy").mkdir(parents=True)
            (root / "plans" / "_scratch" / "ignored").mkdir(parents=True)
            population = feed.d23_population(root, "plans")
            self.assertEqual(len(population), 2)
            self.assertTrue(population[0].startswith("campaigns/"))
            self.assertEqual(population[1], "plans/2026-01-01_legacy/")
            rows, _digest = feed._inventory(root, "plans")
            self.assertFalse(any("_scratch" in row["locator"] for row in rows))

    def test_shared_spec_revision_is_observed_for_the_spec_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, cyc = self._cycle_root(Path(tmp))
            for name in ("research", "documents"):
                (cyc / name).mkdir()
            ref = root / "shared" / "spec" / ("ref_" + "c" * 32)
            rev = ref / "revisions" / ("rrev_" + "d" * 32)
            (rev / "component").mkdir(parents=True)
            (rev / "component" / "prd.md").write_text("prd", encoding="utf-8")
            (rev / "prd.md").write_text("root prd", encoding="utf-8")
            (rev / "_internal").mkdir()
            (ref / "reference.json").write_text(
                json.dumps({"latest_revision_id": "rrev_" + "d" * 32, "updated_on": "2026-08-26T00:00:00Z"}),
                encoding="utf-8",
            )
            population = feed.d23_population(root, "all-d23")
            shared_prefix = "shared/spec/ref_" + "c" * 32 + "/revisions/rrev_" + "d" * 32 + "/"
            self.assertIn(shared_prefix + "component/", population)
            self.assertIn(shared_prefix + "_unscoped-legacy-component/", population)
            self.assertFalse(any(loc.endswith("/_internal/") for loc in population))
            produced = feed.build_feed(root, "all-d23", fixed_mapping(root, "all-d23"), "2026-08-26T00:00:00Z")
            self.assertEqual(sorted(produced["declared_absent_buckets"]), ["designs", "experiments"])

    def test_missing_bucket_at_every_layout_is_inventory_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self._cycle_root(Path(tmp))
            with self.assertRaises(feed.FeedError) as caught:
                feed.d23_population(root, "all-d23")
            self.assertEqual(caught.exception.code, "inventory-incomplete")


class FeedCliTests(unittest.TestCase):
    def test_closed_grammar_returns_canonical_usage_error(self):
        status, stdout, stderr = run_main(["ingest"])
        self.assertEqual(status, feed.EXIT_USAGE)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["error"], "usage")
        self.assertNotIn(str(Path.cwd()), stderr)

    def test_mapping_init_is_exclusive_and_scan_is_repeatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            cycle(root, "a")
            mapping = base / "mapping.jsonl"
            first = base / "first.json"
            second = base / "second.json"
            with mock.patch.object(feed.secrets, "token_bytes", return_value=b"x" * 16):
                status, stdout, stderr = run_main(
                    [
                        "mapping-init",
                        "--artifact-root",
                        str(root),
                        "--bucket",
                        "plans",
                        "--output",
                        str(mapping),
                        "--seal-epoch",
                        "fixed",
                    ]
                )
            self.assertEqual((status, stderr), (0, ""))
            self.assertEqual(json.loads(stdout)["population_count"], 1)
            mapping_before = mapping.read_bytes()
            status, _, stderr = run_main(
                [
                    "mapping-init",
                    "--artifact-root",
                    str(root),
                    "--bucket",
                    "plans",
                    "--output",
                    str(mapping),
                    "--seal-epoch",
                    "fixed",
                ]
            )
            self.assertEqual(status, feed.EXIT_WRITE)
            self.assertEqual(json.loads(stderr)["error"], "output-exists")
            self.assertEqual(mapping.read_bytes(), mapping_before)

            command = [
                "scan",
                "--artifact-root",
                str(root),
                "--bucket",
                "plans",
                "--identity-map",
                str(mapping),
                "--seal-epoch",
                "fixed",
            ]
            with mock.patch.object(feed.secrets, "token_bytes", side_effect=AssertionError("random scan")):
                first_status, first_stdout, _ = run_main(command + ["--output", str(first)])
                self.assertEqual(first_status, 0)
                self.assertEqual(run_main(command + ["--output", str(second)])[0], 0)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            receipt = json.loads(first_stdout)
            self.assertEqual(receipt["command"], "scan")
            self.assertNotIn("rows", receipt)
            self.assertLess(len(first_stdout.encode("utf-8")), 1024)

    def test_output_and_input_containment_fail_without_source_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            cycle(root, "a")
            mapping_inside = root / "mapping.jsonl"
            mapping_inside.write_bytes(fixed_mapping(root))
            output_inside = root / "feed.json"
            status, _, stderr = run_main(
                [
                    "scan",
                    "--artifact-root",
                    str(root),
                    "--bucket",
                    "plans",
                    "--identity-map",
                    str(mapping_inside),
                    "--output",
                    str(output_inside),
                    "--seal-epoch",
                    "fixed",
                ]
            )
            self.assertIn(status, {feed.EXIT_IDENTITY, feed.EXIT_WRITE})
            self.assertIn(json.loads(stderr)["error"], {"input-containment", "output-containment"})
            self.assertFalse(output_inside.exists())

            mapping = base / "mapping.jsonl"
            mapping.write_bytes(fixed_mapping(root))
            produced = feed.build_feed(root, "plans", mapping.read_bytes(), "fixed")
            feed_path = base / "feed.json"
            feed_path.write_bytes(feed.canonical(produced) + b"\n")
            cairn = base / "cairn"
            cairn.mkdir()
            status, _, stderr = run_main(
                [
                    "export-cairn-degraded",
                    "--artifact-root",
                    str(root),
                    "--feed",
                    str(feed_path),
                    "--output",
                    str(root / "compat.json"),
                    "--cairn-root",
                    str(cairn),
                    "--cairn-commit",
                    feed.PINNED_CAIRN_COMMIT,
                ]
            )
            self.assertEqual(status, feed.EXIT_WRITE)
            self.assertEqual(json.loads(stderr)["error"], "output-containment")
            self.assertFalse((root / "compat.json").exists())

    def test_malformed_mapping_does_not_create_or_replace_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            cycle(root, "a")
            mapping = base / "mapping.jsonl"
            mapping.write_text("not-json\n", encoding="utf-8")
            output = base / "feed.json"
            output.write_bytes(b"sentinel")
            status, _, stderr = run_main(
                [
                    "scan",
                    "--artifact-root",
                    str(root),
                    "--bucket",
                    "plans",
                    "--identity-map",
                    str(mapping),
                    "--output",
                    str(output),
                    "--seal-epoch",
                    "fixed",
                ]
            )
            self.assertEqual(status, feed.EXIT_IDENTITY)
            self.assertEqual(json.loads(stderr)["error"], "mapping-schema")
            self.assertEqual(output.read_bytes(), b"sentinel")

    def test_relative_paths_are_rejected_as_usage(self):
        status, _, stderr = run_main(
            [
                "mapping-init",
                "--artifact-root",
                "relative",
                "--bucket",
                "plans",
                "--output",
                "relative-map",
                "--seal-epoch",
                "fixed",
            ]
        )
        self.assertEqual(status, feed.EXIT_USAGE)
        self.assertEqual(json.loads(stderr)["error"], "absolute-root-required")

    def test_mapping_collision_and_output_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            cycle(root, "a")
            cycle(root, "b")
            mapping = base / "mapping.jsonl"
            with mock.patch.object(feed.secrets, "token_bytes", return_value=b"x" * 16):
                status, _, stderr = run_main(
                    [
                        "mapping-init",
                        "--artifact-root",
                        str(root),
                        "--bucket",
                        "plans",
                        "--output",
                        str(mapping),
                        "--seal-epoch",
                        "fixed",
                    ]
                )
            self.assertEqual(status, feed.EXIT_IDENTITY)
            self.assertEqual(json.loads(stderr)["error"], "identity-collision")
            self.assertFalse(mapping.exists())

            target = base / "target.jsonl"
            target.write_bytes(b"sentinel")
            mapping.symlink_to(target)
            status, _, stderr = run_main(
                [
                    "mapping-init",
                    "--artifact-root",
                    str(root),
                    "--bucket",
                    "plans",
                    "--output",
                    str(mapping),
                    "--seal-epoch",
                    "fixed",
                ]
            )
            self.assertEqual(status, feed.EXIT_WRITE)
            self.assertEqual(json.loads(stderr)["error"], "output-symlink")
            self.assertEqual(target.read_bytes(), b"sentinel")


class CairnCompatibilityTests(unittest.TestCase):
    def setUp(self):
        value = os.environ.get("CAIRN_ROOT")
        if not value:
            self.skipTest("CAIRN_ROOT not supplied")
        self.cairn = Path(value).resolve()
        self.commit = os.environ.get("CAIRN_REQUIRED_COMMIT", feed.PINNED_CAIRN_COMMIT)
        self.assertEqual(self.commit, feed.PINNED_CAIRN_COMMIT)

    def test_pinned_fold_degraded_sources_read_only_e2e(self):
        subprocess.run(
            ["git", "-C", str(self.cairn), "cat-file", "-e", self.commit + "^{commit}"],
            check=True,
        )
        blob = subprocess.check_output(
            ["git", "-C", str(self.cairn), "show", f"{self.commit}:{feed.PINNED_CAIRN_MODULE}"]
        )
        cairn_before = subprocess.check_output(
            ["git", "-C", str(self.cairn), "status", "--porcelain=v1", "-z"]
        )
        tsx = self.cairn / "node_modules/.bin/tsx"
        self.assertTrue(tsx.is_file())

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            cycle(root, "missing", manifest=True)
            cycle(root, "empty", summary_name="summary.md", summary=b"", manifest=True)
            cycle(
                root,
                "source",
                summary_name="summary.md",
                manifest=True,
                source="directory",
            )
            cycle(root, "manifest", summary_name="summary.md")
            cycle(root, "complete", summary_name="summary.md", manifest=True)
            sentinel = root / "plans/complete/plan.md"
            sentinel_before = sentinel.read_bytes()
            built = feed.build_feed(root, "plans", fixed_mapping(root), "compat")
            compatibility_path = base / "compat.json"
            compatibility = feed.export_cairn(
                built,
                compatibility_path,
                self.cairn,
                self.commit,
            )
            self.assertEqual(compatibility["module_digest"], feed.digest(blob))

            module = base / "degraded.ts"
            module.write_bytes(blob)
            runner = base / "runner.ts"
            runner.write_text(
                """import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {foldDegradedSources} from './degraded.ts';
async function main() {
  const [compatPath, root] = process.argv.slice(2);
  const compat = JSON.parse(await readFile(compatPath, 'utf8'));
  const writes: unknown[] = [];
  const db = {execute: async (query: unknown) => { writes.push(query); return {rows: []}; }};
  const sources = compat.sources.map((source: Record<string,string>) => ({
    stable_id: source.stable_id,
    manifest_path: path.join(root, source.manifest_path),
    source_path: path.join(root, source.source_path),
    summary_path: path.join(root, source.summary_path),
  }));
  const entries = await foldDegradedSources(db as never, 'w4-run', sources);
  console.log(JSON.stringify({entries, writes: writes.length}));
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(tsx), str(runner), str(compatibility_path), str(root)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            folded = json.loads(result.stdout)
            reasons = sorted(entry["reason"] for entry in folded["entries"])
            self.assertEqual(
                reasons,
                [
                    "manifest-missing",
                    "source-unreadable",
                    "summary-generation-failed",
                    "summary-generation-failed",
                ],
            )
            self.assertEqual(folded["writes"], 4)
            self.assertTrue(all(entry["model_calls"] == 0 for entry in folded["entries"]))
            self.assertEqual(sentinel.read_bytes(), sentinel_before)

        cairn_after = subprocess.check_output(
            ["git", "-C", str(self.cairn), "status", "--porcelain=v1", "-z"]
        )
        self.assertEqual(cairn_after, cairn_before)

    def test_wrong_cairn_commit_fails_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = fixture_root(base)
            cycle(root, "a")
            built = feed.build_feed(root, "plans", fixed_mapping(root), "compat")
            output = base / "compat.json"
            with self.assertRaises(feed.FeedError) as caught:
                feed.export_cairn(built, output, self.cairn, "0" * 40)
            self.assertEqual(caught.exception.exit_code, feed.EXIT_CAIRN)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
