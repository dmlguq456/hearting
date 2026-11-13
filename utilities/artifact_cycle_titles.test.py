#!/usr/bin/env python3
"""Tests for `artifact_cycle_titles.py`: rule set, validator, eligibility,
seal-time emission, and backfill.

Reuses `artifact_producer.test.py`'s `ProducerTestBase` (real activate/begin/
close/finalize fixtures) via importlib, exactly as `tools/artifact_w8_handoff.test.py`
does, rather than duplicating producer setup here.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_cycle_titles as CT  # noqa: E402
import artifact_manifest  # noqa: E402
import artifact_producer as P  # noqa: E402
import campaign_title_repair as CTR  # noqa: E402

_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "cycle_titles_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
producer_fixture = importlib.util.module_from_spec(_FIXTURE_SPEC)
_FIXTURE_SPEC.loader.exec_module(producer_fixture)


# ---------------------------------------------------------------------------
# RuleTest -- pure functions, no producer fixture
# ---------------------------------------------------------------------------


class RuleTest(unittest.TestCase):
    def _ctx(self, *, record=None, manifest=None, cycle_dir=None, campaign=None, v2_title=None, route_text=None):
        return CT.CycleContext(
            record=record if record is not None else {"locator": "2026-09-17_some-other-slug"},
            manifest=manifest if manifest is not None else {"artifacts": [], "artifact_revisions": []},
            cycle_dir=cycle_dir if cycle_dir is not None else Path("/nonexistent"),
            campaign=campaign if campaign is not None else {"title": "Campaign Title", "key": "campaign-key",
                                                             "slug": "campaign-slug"},
            v2_title=v2_title, route_text=route_text,
        )

    def test_normalize_candidate_collapses_whitespace(self):
        self.assertEqual(CT.normalize_candidate("  a   b\tc\n"), "a b c")

    def test_reject_empty_and_control_char(self):
        ctx = self._ctx()
        self.assertEqual(CT._reject_code("", ctx, set()), "empty")
        self.assertEqual(CT._reject_code("a\x01b", ctx, set()), "control-char")

    def test_reject_too_long_but_exactly_eighty_accepted(self):
        ctx = self._ctx()
        title_81 = ("word " * 16 + "x").ljust(81, "x")
        title_80 = title_81[:80]
        self.assertEqual(len(title_81), 81)
        self.assertEqual(len(title_80), 80)
        self.assertEqual(CT._reject_code(title_81, ctx, set()), "too-long")
        self.assertIsNone(CT._reject_code(title_80, ctx, set()))

    def test_reject_slug_like_equal_and_prefix(self):
        record = {"locator": "2026-09-17_my-fixture-slug"}
        ctx = self._ctx(record=record)
        self.assertEqual(CT._reject_code("My Fixture Slug", ctx, set()), "slug-like")
        self.assertEqual(CT._reject_code("My Fixture", ctx, set()), "slug-like")

    def test_record_title_rejects_literal_folder_label_but_keeps_human_title(self):
        record = {"locator": "2026-09-17_num-kws-tf-first-code", "slug": "num-kws-tf-first-code",
                  "title": "NUM_KWS TF-first 구현 및 CPU 검증"}
        ctx = self._ctx(record=record)
        self.assertIsNone(CT._reject_code(record["title"], ctx, set(), source="record-title"))
        for label in (record["slug"], record["locator"]):
            self.assertEqual(CT._reject_code(label, ctx, set(), source="record-title"), "slug-like")
        self.assertEqual(CT.derive_display_title(ctx, existing_title=None, reserved=set()).title,
                         record["title"])

    def test_reject_campaign_title_key_slug_and_v2(self):
        ctx = self._ctx(campaign={"title": "Campaign Title", "key": "campaign-key", "slug": "campaign-slug"},
                        v2_title="V2 Display Title")
        self.assertEqual(CT._reject_code("Campaign Title", ctx, set()), "campaign-title")
        self.assertEqual(CT._reject_code("campaign-key", ctx, set()), "campaign-title")
        self.assertEqual(CT._reject_code("V2 Display Title", ctx, set()), "campaign-title")

    def test_reject_token_only(self):
        ctx = self._ctx()
        self.assertEqual(CT._reject_code("token_only-value.ok", ctx, set()), "token-only")
        self.assertIsNone(CT._reject_code("two words", ctx, set()))

    def test_reject_filename_like(self):
        ctx = self._ctx()
        self.assertEqual(CT._reject_code("a/b title", ctx, set()), "filename-like")
        self.assertEqual(CT._reject_code("plan report.md", ctx, set()), "filename-like")
        for candidate in ("docs/9/21 report", "9/21/summary", "2026/09/21 report",
                          "9/99 report", "9/21 report.md", "9/21 foo/bar"):
            with self.subTest(candidate=candidate):
                self.assertEqual(CT._reject_code(candidate, ctx, set()), "filename-like")

    def test_accept_month_day_in_natural_title(self):
        ctx = self._ctx(record={"title": "9/21 현장 요청 — 최종 분석 및 평가 보고서"})
        decision = CT.derive_display_title(ctx, existing_title=None, reserved=set())
        self.assertEqual(decision.title, "9/21 현장 요청 — 최종 분석 및 평가 보고서")
        self.assertEqual(decision.source, "record-title")

    def test_reject_generic(self):
        # A single-word candidate like "Report" is caught earlier by R-TOKEN
        # (plan's fixed rejection order: slug, campaign-title, token, filename,
        # generic, ...) -- generic is only reachable by a multi-word phrase.
        ctx = self._ctx()
        self.assertEqual(CT._reject_code("final report", ctx, set()), "generic")
        self.assertEqual(CT._reject_code("최종 구현 보고서", ctx, set()), "generic")

    def test_reject_generic_forbidden_by_containment_not_just_exact(self):
        # Gap G1 / owner addendum item 14: FORBIDDEN_GENERIC_TITLES entries reject
        # by containment (a residue label carried inside a longer candidate is
        # still a residue label), while the extra exact-match list is unchanged.
        ctx = self._ctx()
        self.assertEqual(
            CT._reject_code("2026-08-10_ax_command_phase_c1 — legacy support residue", ctx, set()), "generic")
        self.assertEqual(CT._reject_code("legacy support residue (sr-modela)", ctx, set()), "generic")
        # A title merely containing the word "report" or "요약" (not equal to one
        # of the exact-match extra entries) is still accepted.
        self.assertIsNone(CT._reject_code("quarterly progress report addendum", ctx, set()))
        self.assertIsNone(CT._reject_code("모델 성능 요약 노트", ctx, set()))

    def test_reject_markdown_link(self):
        ctx = self._ctx()
        self.assertEqual(CT._reject_code("See [here](x) for detail", ctx, set()), "markdown-link")

    def test_reject_title_not_distinct(self):
        ctx = self._ctx()
        self.assertEqual(CT._reject_code("Shared Title", ctx, {"shared title"}), "title-not-distinct")

    def test_route_task_first_line_strips_known_prefixes(self):
        self.assertEqual(CT._route_task_first_line("\n# Task: Do the thing\nmore text"), "Do the thing")
        self.assertEqual(CT._route_task_first_line("autopilot-code (dev) — Fix the widget\nmore"), "Fix the widget")
        self.assertIsNone(CT._route_task_first_line(None))
        self.assertIsNone(CT._route_task_first_line("   \n   \n"))

    def test_route_task_first_line_strips_korean_task_head(self):
        # Gap G2: same rule family as "Task:", for route-task text produced in Korean.
        self.assertEqual(
            CT._route_task_first_line("작업: 9pt 전환·복원 cheatsheet 정정본 (v2)"),
            "9pt 전환·복원 cheatsheet 정정본 (v2)")
        self.assertEqual(CT._route_task_first_line("작업 : spaced colon variant"), "spaced colon variant")

    def test_first_heading_skips_frontmatter_and_code_fence(self):
        raw = b"---\ntitle: x\n---\n```\n# not a heading\n```\n# Real Heading\nbody\n"
        self.assertEqual(CT._first_heading(raw), "Real Heading")

    def test_first_heading_trims_trailing_hashes(self):
        self.assertEqual(CT._first_heading(b"# Heading Text ##\nbody\n"), "Heading Text")

    def test_first_heading_none_when_absent(self):
        self.assertIsNone(CT._first_heading(b"no heading here\njust body\n"))
        self.assertIsNone(CT._first_heading(b"## not exactly one hash\n"))

    def test_primary_heading_missing_and_ambiguous(self):
        ctx = self._ctx(manifest={"artifacts": [], "artifact_revisions": []})
        self.assertEqual(CT._primary_heading_candidate(ctx), (None, "primary-missing"))
        manifest = {"artifacts": [{"artifact_id": "a1", "role": "primary"},
                                  {"artifact_id": "a2", "role": "primary"}],
                   "artifact_revisions": []}
        ctx2 = self._ctx(manifest=manifest)
        self.assertEqual(CT._primary_heading_candidate(ctx2), (None, "primary-ambiguous"))

    def test_primary_heading_reads_the_file_as_it_is_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            cycle_dir = Path(tmp)
            (cycle_dir / "artifacts").mkdir()
            (cycle_dir / "artifacts" / "report.md").write_bytes(b"# Heading\nbody\n")
            manifest = {
                "artifacts": [{"artifact_id": "art_x", "role": "primary"}],
                "artifact_revisions": [{"artifact_id": "art_x", "media_type": "text/markdown",
                                       "locator": {"kind": "cycle-relative", "path": "artifacts/report.md"},
                                       "content_digest": "sha256:" + "0" * 64}],
            }
            ctx = self._ctx(manifest=manifest, cycle_dir=cycle_dir)
            # The primary was edited after the manifest was written: the current bytes answer (D-123/D-127).
            self.assertEqual(CT._primary_heading_candidate(ctx), ("Heading", None))

    def test_primary_heading_reads_real_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            cycle_dir = Path(tmp)
            (cycle_dir / "artifacts").mkdir()
            data = b"# Real Report Heading\nbody\n"
            (cycle_dir / "artifacts" / "report.md").write_bytes(data)
            digest = artifact_manifest.digest_bytes(data)
            manifest = {
                "artifacts": [{"artifact_id": "art_x", "role": "primary"}],
                "artifact_revisions": [{"artifact_id": "art_x", "media_type": "text/markdown",
                                       "locator": {"kind": "cycle-relative", "path": "artifacts/report.md"},
                                       "content_digest": digest}],
            }
            ctx = self._ctx(manifest=manifest, cycle_dir=cycle_dir)
            self.assertEqual(CT._primary_heading_candidate(ctx), ("Real Report Heading", None))

    def test_derive_display_title_prefers_existing_and_skips_rules_for_it(self):
        ctx = self._ctx()
        decision = CT.derive_display_title(ctx, existing_title="Whatever It Is", reserved=set())
        self.assertEqual(decision, CT.Decision("Whatever It Is", "existing-declaration", None, ()))

    def test_derive_display_title_terminal_reason_is_primary_heading_outcome(self):
        # Gap G3: a folder-name record title (rejected as slug-like, true for every
        # W7-migrated cycle) must not mask the primary-heading outcome as
        # "no-source" -- the terminal reason is the primary step's own outcome, and
        # the rejected chain keeps every attempted source.
        record = {"locator": "2026-09-17_my-fixture-slug", "slug": "my-fixture-slug",
                  "title": "my-fixture-slug"}
        manifest = {
            "artifacts": [{"artifact_id": "art_x", "role": "primary"}],
            "artifact_revisions": [{"artifact_id": "art_x", "media_type": "application/json",
                                    "locator": {"kind": "cycle-relative", "path": "data.json"}}],
        }
        ctx = self._ctx(record=record, manifest=manifest)
        decision = CT.derive_display_title(ctx, existing_title=None, reserved=set())
        self.assertIsNone(decision.title)
        self.assertEqual(decision.reason, "primary-not-markdown")
        self.assertEqual(decision.rejected, (("record-title", "slug-like"),))

    def test_derive_display_title_terminal_reason_uses_primary_rejection_when_present(self):
        # When the primary heading itself is attempted and rejected, its own
        # rejection code is the terminal reason (not a synthesized no-source).
        record = {"locator": "2026-09-17_my-fixture-slug"}
        manifest = {
            "artifacts": [{"artifact_id": "art_x", "role": "primary"}],
            "artifact_revisions": [{"artifact_id": "art_x", "media_type": "text/markdown",
                                    "locator": {"kind": "cycle-relative", "path": "artifacts/report.md"},
                                    "content_digest": "sha256:" + "0" * 64}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cycle_dir = Path(tmp)
            (cycle_dir / "artifacts").mkdir()
            data = b"# legacy support residue\nbody\n"
            (cycle_dir / "artifacts" / "report.md").write_bytes(data)
            digest = artifact_manifest.digest_bytes(data)
            manifest["artifact_revisions"][0]["content_digest"] = digest
            ctx = self._ctx(record=record, manifest=manifest, cycle_dir=cycle_dir)
            decision = CT.derive_display_title(ctx, existing_title=None, reserved=set())
        self.assertIsNone(decision.title)
        self.assertEqual(decision.reason, "generic")
        self.assertEqual(decision.rejected, (("primary-heading", "generic"),))

    def test_reason_cell_shows_chain_for_unassigned_rows_only(self):
        # The markdown report's reason column shows the full chain for unassigned
        # rows (gap G3) but a plain reason for other verdicts.
        row = {"verdict": "unassigned", "reason": "primary-not-markdown",
              "rejected": [("record-title", "slug-like")]}
        self.assertEqual(CT._reason_cell(row), "record-title:slug-like")
        row_multi = {"verdict": "unassigned", "reason": "generic",
                    "rejected": [("record-title", "slug-like"), ("primary-heading", "generic")]}
        self.assertEqual(CT._reason_cell(row_multi), "record-title:slug-like, primary-heading:generic")
        row_no_chain = {"verdict": "unassigned", "reason": "primary-missing", "rejected": []}
        self.assertEqual(CT._reason_cell(row_no_chain), "primary-missing")
        row_ineligible = {"verdict": "ineligible", "reason": "record-digest-mismatch", "rejected": []}
        self.assertEqual(CT._reason_cell(row_ineligible), "record-digest-mismatch")


# ---------------------------------------------------------------------------
# ValidatorTest -- declaration validation, no producer fixture
# ---------------------------------------------------------------------------


class ValidatorTest(unittest.TestCase):
    ROOT_ID = "root_" + "b" * 32
    REPO_ID = "repo_" + "a" * 32
    CAMPAIGN_ID = "camp_" + "1" * 32
    CYCLE_ID = "cyc_" + "1" * 32
    REVISION_ID = "mrev_" + "1" * 32

    def _entry(self, **overrides):
        entry = {"campaign_id": self.CAMPAIGN_ID, "cycle_id": self.CYCLE_ID, "display_title": "Some Title",
                 "manifest_bindings": [{"manifest_revision_id": self.REVISION_ID, "manifest_digest": "sha256:" + "a" * 64}]}
        entry.update(overrides)
        return entry

    def _doc(self, entries):
        return {"schema": CT.CYCLE_SCHEMA, "artifact_root_id": self.ROOT_ID, "repository_id": self.REPO_ID,
               "entries": entries}

    def _bytes(self, doc):
        return json.dumps(doc).encode("utf-8")

    def test_valid_declaration_round_trips(self):
        doc = self._doc([self._entry()])
        parsed = CT.validate_declaration(self._bytes(doc), root_id=self.ROOT_ID, repository_id=self.REPO_ID)
        self.assertEqual(parsed["entries"], [self._entry()])

    def test_missing_key_rejected(self):
        doc = self._doc([self._entry()])
        del doc["repository_id"]
        with self.assertRaises(CT.CycleTitlesError):
            CT.validate_declaration(self._bytes(doc), root_id=self.ROOT_ID, repository_id=self.REPO_ID)

    def test_extra_key_rejected(self):
        doc = self._doc([self._entry()])
        doc["extra"] = 1
        with self.assertRaises(CT.CycleTitlesError):
            CT.validate_declaration(self._bytes(doc), root_id=self.ROOT_ID, repository_id=self.REPO_ID)

    def test_zero_bindings_rejected(self):
        doc = self._doc([self._entry(manifest_bindings=[])])
        with self.assertRaises(CT.CycleTitlesError):
            CT.validate_declaration(self._bytes(doc), root_id=self.ROOT_ID, repository_id=self.REPO_ID)

    def test_two_bindings_rejected(self):
        binding_a = {"manifest_revision_id": self.REVISION_ID, "manifest_digest": "sha256:" + "a" * 64}
        binding_b = {"manifest_revision_id": "mrev_" + "2" * 32, "manifest_digest": "sha256:" + "b" * 64}
        doc = self._doc([self._entry(manifest_bindings=[binding_a, binding_b])])
        with self.assertRaises(CT.CycleTitlesError):
            CT.validate_declaration(self._bytes(doc), root_id=self.ROOT_ID, repository_id=self.REPO_ID)

    def test_blank_title_rejected(self):
        doc = self._doc([self._entry(display_title="   ")])
        with self.assertRaises(CT.CycleTitlesError):
            CT.validate_declaration(self._bytes(doc), root_id=self.ROOT_ID, repository_id=self.REPO_ID)

    def test_duplicate_cycle_id_rejected(self):
        doc = self._doc([self._entry(), self._entry()])
        with self.assertRaises(CT.CycleTitlesError):
            CT.validate_declaration(self._bytes(doc), root_id=self.ROOT_ID, repository_id=self.REPO_ID)

    def test_unsorted_entries_rejected(self):
        entry_a = self._entry(cycle_id="cyc_" + "1" * 32)
        entry_b = self._entry(cycle_id="cyc_" + "2" * 32)
        doc = self._doc([entry_b, entry_a])
        with self.assertRaises(CT.CycleTitlesError):
            CT.validate_declaration(self._bytes(doc), root_id=self.ROOT_ID, repository_id=self.REPO_ID)

    def test_symlinked_declaration_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real.json"
            target.write_bytes(self._bytes(self._doc([])))
            link = Path(tmp) / "link.json"
            link.symlink_to(target)
            with self.assertRaises(CT.CycleTitlesError):
                CT.check_declaration_file(link, root_id=self.ROOT_ID, repository_id=self.REPO_ID)

    def test_missing_file_is_empty_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone.json"
            doc = CT.check_declaration_file(missing, root_id=self.ROOT_ID, repository_id=self.REPO_ID)
            self.assertEqual(doc["entries"], [])

    def test_render_declaration_matches_amendment_module_canonical_bytes(self):
        doc = self._doc([self._entry()])
        raw = CTR.canonical(doc) + b"\n"
        parsed = CT.validate_declaration(raw, root_id=self.ROOT_ID, repository_id=self.REPO_ID)
        rendered = CT.render_declaration(self.ROOT_ID, self.REPO_ID, parsed["entries"])
        self.assertEqual(rendered, raw)


# ---------------------------------------------------------------------------
# Fixture base for tests that need real sealed cycles
# ---------------------------------------------------------------------------


class CycleTitlesTestBase(producer_fixture.ProducerTestBase):
    def setUp(self):
        # A real dispatched attempt (this worker) leaves AGENT_DISPATCH_ATTEMPT_ID
        # set in the ambient shell; `begin()`'s owner/route binding check reads it
        # and resolves against the live registry, not this fixture root, so an
        # attempt actually running under dispatch fails with
        # `owner-route-owner-row-not-unique` on every fixture route. Strip it for
        # the fixture's duration like `ProducerTestBase` already does for the
        # AGENT_ARTIFACT_* variables.
        self._dispatch_attempt_env = os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID", None)
        self.addCleanup(self._restore_dispatch_attempt_env)
        super().setUp()
        self.activate()

    def _restore_dispatch_attempt_env(self):
        if self._dispatch_attempt_env is not None:
            os.environ["AGENT_DISPATCH_ATTEMPT_ID"] = self._dispatch_attempt_env

    def seal(self, *, title=None, slug="cycle-titles-fixture", primary_rel="reports/final_report.md",
             primary_body=b"# Fixture Primary Heading\n\nbody\n", intensity="direct", now=None):
        route, route_file = self.route(intensity=intensity, slug=slug)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity=intensity, title=title, now=now)
        self.write_output(result, rel=primary_rel, data=primary_body)
        self.close(route, route_file)
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"], primary=primary_rel, now=now)
        return result, outcome

    def _declaration_path(self):
        return self.root / CT.CYCLE_TITLES_REL

    def _read_declaration(self):
        path = self._declaration_path()
        if not path.is_file():
            return {"entries": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def _unlink_declaration(self):
        try:
            self._declaration_path().unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _accept_all_reader(candidate_bytes, root_id, repository_id, *, reader_dir=None):
        doc = json.loads(candidate_bytes.decode("utf-8"))
        ids = frozenset(entry["cycle_id"] for entry in doc["entries"])
        return CT.ReaderResult("accepted", ids, "", "fixture")


# ---------------------------------------------------------------------------
# EligibilityTest
# ---------------------------------------------------------------------------


class EligibilityTest(CycleTitlesTestBase):
    def test_marked_record_is_judged_like_any_finished_cycle(self):
        result, _outcome = self.seal(title="Superseded Candidate Title")
        self.assertIn(result["cycle_id"], {e["cycle_id"] for e in self._read_declaration()["entries"]})
        P.mark_cycle_superseded(self.root, result["cycle_id"], superseded_by=[],
                                superseded_event_id="evt_" + "1" * 32)
        backfill_result = CT.backfill(self.root, reader=self._accept_all_reader)
        self.assertEqual(backfill_result["counts"]["ineligible"], {})
        self.assertEqual(backfill_result["counts"]["eligible"], 1)
        self.assertEqual(backfill_result["counts"]["existing_dropped"], 0)
        self.assertIn(result["cycle_id"], {e["cycle_id"] for e in self._read_declaration()["entries"]})

    def test_manifest_digest_mismatch_excludes(self):
        result, outcome = self.seal(title="Digest Mismatch Title")
        manifest_path = Path(outcome["manifest_path"])
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        backfill_result = CT.backfill(self.root, reader=self._accept_all_reader)
        self.assertIn("record-digest-mismatch", backfill_result["counts"]["ineligible"])

    def test_symlink_campaign_alias_not_double_counted_real_duplicate_is_ambiguous(self):
        result, outcome = self.seal(title="Alias Walk Title")
        cycle_dir = Path(outcome["manifest_path"]).parent
        campaign_dir = cycle_dir.parent
        alias = campaign_dir.parent / "alias-campaign"
        alias.symlink_to(campaign_dir, target_is_directory=True)
        found = CT._walk_manifests(self.root)
        self.assertEqual(found.get(result["cycle_id"], []), [Path(outcome["manifest_path"])])

        payload_manifest = cycle_dir / "artifacts" / "_internal" / "candidate" / "manifest.json"
        payload_manifest.parent.mkdir(parents=True, exist_ok=True)
        payload_manifest.write_text(json.dumps({
            "cycle": {"cycle_id": result["cycle_id"]},
            "campaign": {"campaign_id": result["campaign_id"]},
        }), encoding="utf-8")
        found_with_payload = CT._walk_manifests(self.root)
        self.assertEqual(found_with_payload.get(result["cycle_id"], []),
                         [Path(outcome["manifest_path"])])

        clone_dir = campaign_dir / "clone-of-cycle"
        shutil.copytree(cycle_dir, clone_dir)
        found_after_clone = CT._walk_manifests(self.root)
        self.assertEqual(len(found_after_clone[result["cycle_id"]]), 2)
        backfill_result = CT.backfill(self.root, reader=self._accept_all_reader)
        self.assertIn("manifest-ambiguous", backfill_result["counts"]["ineligible"])


# ---------------------------------------------------------------------------
# SealTest
# ---------------------------------------------------------------------------


class SealTest(CycleTitlesTestBase):
    def _marker_path(self, cycle_id):
        return self.root / CT.EMISSIONS_REL / f"{cycle_id}.json"

    def test_normal_seal_creates_entry(self):
        result, _outcome = self.seal(title="Normal Seal Distinct Title")
        entries = {e["cycle_id"]: e for e in self._read_declaration()["entries"]}
        self.assertIn(result["cycle_id"], entries)
        self.assertEqual(entries[result["cycle_id"]]["display_title"], "Normal Seal Distinct Title")

    def test_binding_digest_differs_from_record_manifest_digest(self):
        result, outcome = self.seal(title="Binding Digest Title")
        entry = next(e for e in self._read_declaration()["entries"] if e["cycle_id"] == result["cycle_id"])
        parsed = json.loads(Path(outcome["manifest_path"]).read_bytes().decode("utf-8"))
        expected = CT.binding_digest(parsed)
        self.assertEqual(entry["manifest_bindings"][0]["manifest_digest"], expected)
        record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertNotEqual(expected, record["manifest_digest"])

    def test_injected_exception_defers_without_blocking_seal(self):
        with mock.patch.object(CT, "_emit_cycle_locked", side_effect=RuntimeError("boom")):
            result, outcome = self.seal(title="Injected Failure Title")
        self.assertEqual(outcome["status"], "sealed")
        record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertEqual(record["state"], "sealed")
        marker = json.loads(self._marker_path(result["cycle_id"]).read_text(encoding="utf-8"))
        self.assertEqual(marker["reason"], "deferred:RuntimeError")

    def test_declaration_path_is_directory_defers_without_blocking(self):
        self._declaration_path().parent.mkdir(parents=True, exist_ok=True)
        self._declaration_path().mkdir()
        result, outcome = self.seal(title="Directory Sidecar Title")
        self.assertEqual(outcome["status"], "sealed")
        marker = json.loads(self._marker_path(result["cycle_id"]).read_text(encoding="utf-8"))
        self.assertEqual(marker["reason"], "deferred:sidecar-invalid")
        self.assertTrue(self._declaration_path().is_dir())

    def test_broken_json_declaration_defers_and_stays_byte_identical(self):
        self._declaration_path().parent.mkdir(parents=True, exist_ok=True)
        self._declaration_path().write_text("{not json", encoding="utf-8")
        before = self._declaration_path().read_bytes()
        result, outcome = self.seal(title="Broken Sidecar Title")
        self.assertEqual(outcome["status"], "sealed")
        marker = json.loads(self._marker_path(result["cycle_id"]).read_text(encoding="utf-8"))
        self.assertEqual(marker["reason"], "deferred:sidecar-invalid")
        self.assertEqual(self._declaration_path().read_bytes(), before)

    def test_finalize_return_key_set_unchanged_by_injected_failure(self):
        with mock.patch.object(CT, "_emit_cycle_locked", side_effect=RuntimeError("boom")):
            _, outcome_failed = self.seal(title="Return Keys Failure Title", slug="return-keys-failure")
        _, outcome_ok = self.seal(title="Return Keys OK Title", slug="return-keys-ok")
        self.assertEqual(set(outcome_failed.keys()), set(outcome_ok.keys()))

    def test_crash_after_manifest_recover_creates_entry(self):
        route, route_file = self.route(intensity="direct", slug="crash-recover-fixture")
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                         title="Crash Recover Title")
        self.write_output(result, rel="reports/final_report.md", data=b"# Crash Recover Heading\nbody\n")
        self.close(route, route_file)
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=result["cycle_id"], primary="reports/final_report.md",
                      crash_after_manifest=True)
        recovered = P.recover(self.root)
        self.assertIn(result["cycle_id"], recovered["producer"]["rolled_forward"])
        self.assertIn(result["cycle_id"], {e["cycle_id"] for e in self._read_declaration()["entries"]})


# ---------------------------------------------------------------------------
# BackfillTest
# ---------------------------------------------------------------------------


class BackfillTest(CycleTitlesTestBase):
    def test_idempotent_apply_then_new_seal_adds_one_entry(self):
        self.seal(title="Idempotent Apply Title")
        self._declaration_path().unlink()  # simulate a sealed cycle predating this feature
        first = CT.backfill(self.root, apply=True, reader=self._accept_all_reader)
        self.assertEqual(first["status"], "applied")
        journal_dir = self.root / CT.BACKFILL_JOURNAL_REL
        self.assertEqual(len(list(journal_dir.glob("*.json"))), 1)
        second = CT.backfill(self.root, apply=True, reader=self._accept_all_reader)
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["post_digest"], first["post_digest"])
        self.assertEqual(len(list(journal_dir.glob("*.json"))), 1)
        before_count = len(self._read_declaration()["entries"])
        self.seal(title="Idempotent Apply Second Title", slug="idempotent-second")
        after_count = len(self._read_declaration()["entries"])
        self.assertEqual(after_count, before_count + 1)

    def test_replay_equivalence_sequential_seal_vs_backfill(self):
        # Distinct `now` values: `sealed_on` has 1-second resolution, and the
        # backfill replay order key is `(sealed_on, cycle_id)` -- two cycles
        # sealed within the same wall-clock second would tie-break on a random
        # cycle_id instead of true seal order, breaking this equivalence.
        self.seal(title=None, slug="replay-one", primary_body=b"# Shared Report Heading\nbody one\n", now=1_700_000_000)
        self.seal(title=None, slug="replay-two", primary_body=b"# Shared Report Heading\nbody two\n", now=1_700_000_100)
        sequential_bytes = self._declaration_path().read_bytes()
        self._declaration_path().unlink()
        result = CT.backfill(self.root, apply=True, reader=self._accept_all_reader)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self._declaration_path().read_bytes(), sequential_bytes)

    def test_dup_title_only_first_sealed_gets_title_kept_across_later_duplicates(self):
        result1, _o1 = self.seal(title=None, slug="dup-first", primary_body=b"# Shared Heading\nbody one\n",
                                 now=1_700_000_000)
        result2, _o2 = self.seal(title=None, slug="dup-second", primary_body=b"# Shared Heading\nbody two\n",
                                 now=1_700_000_100)
        ids = {e["cycle_id"] for e in self._read_declaration()["entries"]}
        self.assertIn(result1["cycle_id"], ids)
        self.assertNotIn(result2["cycle_id"], ids)
        result3, _o3 = self.seal(title=None, slug="dup-third", primary_body=b"# Shared Heading\nbody three\n",
                                 now=1_700_000_200)
        doc2 = self._read_declaration()
        entry1 = next(e for e in doc2["entries"] if e["cycle_id"] == result1["cycle_id"])
        self.assertEqual(entry1["display_title"], "Shared Heading")
        self.assertNotIn(result3["cycle_id"], {e["cycle_id"] for e in doc2["entries"]})

    def test_reader_gate_unavailable_rejected_drift_and_digest_mismatch(self):
        self.seal(title="Reader Gate Title")
        self._unlink_declaration()

        def unavailable_reader(candidate_bytes, root_id, repository_id, *, reader_dir=None):
            return CT.ReaderResult("unavailable", frozenset(), "no-app-dir", None)

        result = CT.backfill(self.root, apply=True, reader=unavailable_reader)
        self.assertEqual(result["status"], "refused:reader-unavailable")
        self.assertFalse(self._declaration_path().exists())

        def rejected_reader(candidate_bytes, root_id, repository_id, *, reader_dir=None):
            return CT.ReaderResult("rejected", frozenset(), "bad", "fixture")

        result2 = CT.backfill(self.root, apply=True, reader=rejected_reader)
        self.assertEqual(result2["status"], "refused:reader-rejected")

        def drifting_reader(candidate_bytes, root_id, repository_id, *, reader_dir=None):
            self._declaration_path().parent.mkdir(parents=True, exist_ok=True)
            self._declaration_path().write_bytes(b'{"schema":"drift","artifact_root_id":"","repository_id":"","entries":[]}')
            doc = json.loads(candidate_bytes.decode("utf-8"))
            ids = frozenset(entry["cycle_id"] for entry in doc["entries"])
            return CT.ReaderResult("accepted", ids, "", "fixture")

        self._unlink_declaration()
        result3 = CT.backfill(self.root, apply=True, reader=drifting_reader)
        self.assertEqual(result3["status"], "refused:declaration-drift")

        self._unlink_declaration()
        result4 = CT.backfill(self.root, apply=True, reader=self._accept_all_reader,
                              expect_post_digest="sha256:" + "0" * 64)
        self.assertEqual(result4["status"], "refused:post-digest-mismatch")

    def test_real_reader_accepts_fixture_candidate_when_deployed(self):
        app_dir = CT._resolve_reader_dir(None)
        if app_dir is None:
            self.skipTest("no deployed cairn-sync reader available")
        self.seal(title="Real Reader Fixture Title")
        self._declaration_path().unlink()
        result = CT.backfill(self.root, apply=False)
        self.assertEqual(result["reader"]["status"], "accepted")

    def test_dry_run_does_not_write_anything(self):
        self.seal(title="Dry Run Snapshot Title")
        before = self._snapshot()
        CT.backfill(self.root, apply=False, reader=self._accept_all_reader)
        after = self._snapshot()
        self.assertEqual(before, after)
        self.assertFalse((self.root / CT.EMISSIONS_REL).exists())

    def _snapshot(self):
        rows = {}
        for path in self.root.rglob("*"):
            if path.is_file():
                info = path.stat()
                rows[path.relative_to(self.root).as_posix()] = (info.st_size, info.st_mtime_ns)
        return rows

    def test_restore_backfill_returns_to_pre_state(self):
        self.seal(title="Restore Target Title")
        self._declaration_path().unlink()
        result = CT.backfill(self.root, apply=True, reader=self._accept_all_reader)
        self.assertEqual(result["status"], "applied")
        restored = CT.restore_backfill(self.root, Path(result["journal"]))
        self.assertEqual(restored["status"], "restored")
        self.assertFalse(self._declaration_path().exists())

    def test_restore_backfill_refuses_on_drift(self):
        self.seal(title="Restore Drift Title")
        self._declaration_path().unlink()
        result = CT.backfill(self.root, apply=True, reader=self._accept_all_reader)
        self._declaration_path().write_bytes(b"tampered")
        restored = CT.restore_backfill(self.root, Path(result["journal"]))
        self.assertEqual(restored["status"], "refused:restore-drift")


# ---------------------------------------------------------------------------
# CliTest
# ---------------------------------------------------------------------------


class CliTest(CycleTitlesTestBase):
    def test_cli_dry_run_and_apply_without_reader(self):
        self.seal(title="CLI Dry Run Title")
        self._declaration_path().unlink()
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = P.main(["cycle-display-titles-backfill", "--artifact-root", str(self.root)])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "dry-run")

        empty_reader_dir = Path(self._tmp.name) / "empty-reader-dir"
        empty_reader_dir.mkdir()
        out2 = io.StringIO()
        with mock.patch("sys.stdout", out2):
            code2 = P.main(["cycle-display-titles-backfill", "--artifact-root", str(self.root),
                           "--apply", "--reader-dir", str(empty_reader_dir)])
        self.assertEqual(code2, P.BLOCKED)
        payload2 = json.loads(out2.getvalue())
        self.assertEqual(payload2["status"], "refused:reader-unavailable")


if __name__ == "__main__":
    unittest.main()
