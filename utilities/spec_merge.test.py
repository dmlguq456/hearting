#!/usr/bin/env python3
"""Pure D-122 merge fixtures; never read a real shared revision or write state."""
import copy
import hashlib
import json
import unittest

import spec_merge as M


class SpecMergeTest(unittest.TestCase):
    def merge(self, base, ours, latest, path="x/prd.md"):
        return M.merge_trees({path: base}, {path: ours}, {path: latest})

    def conflict(self, base, ours, latest, reason, path="x/prd.md"):
        with self.assertRaises(M.MergeConflict) as caught:
            self.merge(base, ours, latest, path)
        self.assertIn(reason, {row["reason"] for row in caught.exception.conflicts})
        self.assertTrue(all(row["path"] == path for row in caught.exception.conflicts))
        json.dumps(caught.exception.conflicts)
        return caught.exception.conflicts

    def test_distinct_files_and_latest_new_component_preserved_without_input_mutation(self):
        base = {"a/prd.md": b"a", "b/prd.md": b"b"}
        ours = base | {"a/prd.md": b"a changed"}
        latest = base | {"b/prd.md": b"b changed", "c/prd.md": b"new"}
        before = copy.deepcopy((base, ours, latest))
        result, evidence = M.merge_trees(base, ours, latest)
        self.assertEqual(result, {"a/prd.md": b"a changed", "b/prd.md": b"b changed", "c/prd.md": b"new"})
        self.assertEqual(before, (base, ours, latest))
        self.assertEqual(evidence["policy_version"], M.POLICY_VERSION)
        self.assertEqual(json.loads(json.dumps(evidence)), evidence)

    def test_evidence_is_deterministic_and_hashes_actual_bytes(self):
        b={"b":b"b", "a":b"a"};o={"b":b"b", "a":b"ours"};l={"b":b"latest", "a":b"a"}
        first=M.merge_trees(b,o,l)
        reversed_maps=[dict(reversed(list(d.items()))) for d in (b,o,l)]
        self.assertEqual(first,M.merge_trees(*reversed_maps))
        unit=first[1]["units"][0]
        self.assertEqual(unit["path"],"a")
        self.assertEqual(unit["merged_sha256"],hashlib.sha256(b"ours").hexdigest())
        self.assertEqual(unit["decision"],"ours")

    def test_exact_same_change_coalesces(self):
        merged,evidence=self.merge(b"base",b"same",b"same",path="raw.bin")
        self.assertEqual(merged["raw.bin"],b"same")
        self.assertEqual(evidence["units"][0]["decision"],"coalesced")

    def test_generic_file_has_no_line_merge_fallback(self):
        self.conflict(b"a\nb\n",b"A\nb\n",b"a\nB\n","same-unit-changed",path="raw.txt")

    def test_binary_one_sided_changes_and_binary_conflict(self):
        self.assertEqual(self.merge(b"\xff",b"\x00",b"\xff","raw.bin")[0]["raw.bin"],b"\x00")
        self.conflict(b"\xff",b"\x00",b"\x01","same-unit-changed",path="raw.bin")

    def test_file_delete_vs_modify_and_identical_delete(self):
        with self.assertRaises(M.MergeConflict) as raised:
            M.merge_trees({"x":b"b"},{},{"x":b"changed"})
        self.assertEqual(raised.exception.conflicts[0]["reason"],"delete-versus-modify")
        self.assertEqual(M.merge_trees({"x":b"b"},{},{})[0],{})

    def test_receipt_exception_is_exact_path_and_ours_absence_stays_absent(self):
        p=M.RECEIPT
        self.assertEqual(M.merge_trees({p:b"B"},{p:b"O"},{p:b"L"})[0],{p:b"O"})
        self.assertEqual(M.merge_trees({p:b"B"},{},{p:b"L"})[0],{})
        self.conflict(b"B",b"O",b"L","same-unit-changed",path="component/"+p)

    def test_snapshot_new_names_union_but_same_locator_different_bytes_refuse(self):
        p="c/_internal/versions/v9/prd.md";q="c/_internal/versions/v10/prd.md"
        self.assertEqual(M.merge_trees({}, {p:b"old"},{q:b"new"})[0],{p:b"old",q:b"new"})
        self.conflict(b"before",b"rewritten",b"before","immutable-snapshot-conflict",path=p)
        with self.assertRaises(M.MergeConflict) as caught:
            M.merge_trees({}, {p:b"ours"},{p:b"latest"})
        self.assertEqual(caught.exception.conflicts[0]["reason"],"immutable-snapshot-conflict")

    def test_snapshot_path_match_does_not_spread_to_other_directories(self):
        p="_internal/versions-other/file.md"
        result,_=self.merge(b"# A\na\n# B\nb\n",b"# A\nA\n# B\nb\n",b"# A\na\n# B\nB\n",p)
        self.assertEqual(result[p],b"# A\nA\n# B\nB\n")

    def test_markdown_independent_headings_merge(self):
        b=b"# Project\nIntro\n## A\na\n## B\nb\n"
        result,evidence=self.merge(b,b.replace(b"\na\n",b"\nA\n"),b.replace(b"\nb\n",b"\nB\n"))
        self.assertEqual(result["x/prd.md"],b.replace(b"\na\n",b"\nA\n").replace(b"\nb\n",b"\nB\n"))
        self.assertEqual([row["section"] for row in evidence["units"]],[["# Project","## A"],["# Project","## B"]])

    def blueprint(self):
        return "# Project\n## ✦ 청사진 요약 ⟨v10, 2026-11-01⟩\nsummary\n## Contract\noriginal\n".encode()

    def merge_blueprint(self, base, ours, latest):
        try:
            return self.merge(base, ours, latest)
        except M.MergeConflict as exc:
            self.fail("independent blueprint label/body changes must merge: " + str(exc))

    def test_blueprint_label_update_on_either_branch_keeps_independent_contract(self):
        base = self.blueprint()
        label = base.replace(b"v10, 2026-11-01", b"v11, 2026-11-02")
        contract = base + b"## Added\nindependent requirement\n"
        for ours, latest in ((label, contract), (contract, label)):
            with self.subTest(label_on_ours=ours == label):
                result, _ = self.merge_blueprint(base, ours, latest)
                self.assertEqual(result["x/prd.md"], label + b"## Added\nindependent requirement\n")

    def test_blueprint_label_and_other_branch_body_are_separate_units(self):
        base = self.blueprint()
        ours = base.replace(b"v10, 2026-11-01", b"v11, 2026-11-02")
        latest = base.replace(b"\nsummary\n", b"\nreviewed summary\n")
        result, evidence = self.merge_blueprint(base, ours, latest)
        self.assertEqual(result["x/prd.md"], ours.replace(b"\nsummary\n", b"\nreviewed summary\n"))
        self.assertIn("merged", [row["decision"] for row in evidence["units"]])

    def test_blueprint_label_update_keeps_child_identity_and_order(self):
        base = self.blueprint().replace(b"## Contract", b"### Detail\nchild\n## Contract")
        ours = base.replace(b"v10, 2026-11-01", b"v11, 2026-11-02")
        latest = base.replace(b"\nchild\n", b"\nupdated child\n")
        result, _ = self.merge_blueprint(base, ours, latest)
        self.assertEqual(result["x/prd.md"], ours.replace(b"\nchild\n", b"\nupdated child\n"))

    def test_blueprint_conflicting_labels_or_bodies_are_not_synthesized(self):
        base = self.blueprint()
        ours = base.replace(b"v10, 2026-11-01", b"v11, 2026-11-02")
        for latest in (base.replace(b"v10, 2026-11-01", b"v12, 2026-11-03"),
                       base.replace(b"\nsummary\n", b"\nlatest body\n")):
            candidate = ours if b"v12" in latest else ours.replace(b"\nsummary\n", b"\nours body\n")
            with self.subTest(latest=latest), self.assertRaises(M.MergeConflict):
                self.merge(base, candidate, latest)

    def test_blueprint_reorder_and_delete_modify_remain_conflicts(self):
        base = self.blueprint()
        prefix, contract = base.split(b"## Contract", 1)
        updated = prefix.replace(b"v10, 2026-11-01", b"v11, 2026-11-02")
        latest = base.replace(b"\noriginal\n", b"\nchanged contract\n")
        with self.assertRaises(M.MergeConflict):
            self.merge(base, b"# Project\n## Contract" + contract + updated.split(b"\n", 1)[1], latest)
        self.conflict(base, b"# Project\n## Contract" + contract,
                      updated + b"## Contract" + contract, "delete-versus-modify")

    def test_blueprint_duplicate_versioned_identity_is_ambiguous(self):
        base = self.blueprint()
        heading = "## ✦ 청사진 요약 ⟨v11, 2026-11-02⟩\nother\n".encode()
        candidate = base.replace(b"v10, 2026-11-01", b"v12, 2026-11-03") + heading
        self.conflict(base, candidate, base.replace(b"original", b"latest"),
                      "markdown-heading-rename-ambiguous")

    def test_other_numbered_headings_and_non_prd_files_keep_rename_protection(self):
        for base, path in ((b"# Project\n## Release v10\nbody\n## Other\nold\n", "x/prd.md"),
                           (self.blueprint(), "x/notes.md")):
            with self.subTest(path=path):
                self.conflict(base, base.replace(b"v10", b"v11"),
                              base.replace(b"old", b"new") if b"old" in base else base + b"## Added\nnew\n",
                              "markdown-heading-rename-ambiguous", path)

    def test_unchanged_blueprint_label_keeps_existing_whole_unit_evidence(self):
        base = self.blueprint()
        ours = base.replace(b"summary", b"ours summary")
        latest = base.replace(b"original", b"latest contract")
        _, evidence = self.merge(base, ours, latest)
        self.assertEqual({row["section"][-1]: row["decision"] for row in evidence["units"]},
                         {"## Contract": "latest", "## ✦ 청사진 요약 ⟨v10, 2026-11-01⟩": "ours"})

    def test_published_descendant_covers_exact_contract_delta_with_newer_bookkeeping(self):
        base = {"c/prd.md": self.blueprint(), "c/policy.yaml": b"policy: old\n"}
        source = base | {"c/prd.md": self.blueprint().replace(b"original", b"adopted"),
                         "c/policy.yaml": b"policy: adopted\n",
                         "c/pipeline_state.yaml": b"version: 10\nchange: {version: 10, status: implementation-in-progress}\n"}
        published = {"c/prd.md": source["c/prd.md"].replace(b"v10, 2026-11-01", b"v12, 2026-11-03")
                     + b"## Later\nindependent\n", "c/policy.yaml": b"policy: adopted\n",
                     "c/pipeline_state.yaml": b"version: 12\n"}
        before = copy.deepcopy((base, source, published))
        self.assertTrue(M.changes_covered(base, source, published))
        self.assertEqual(before, (base, source, published))
        self.assertFalse(M.changes_covered(base, source, published | {
            "c/policy.yaml": b"policy: old\n"}))
        for actual in (published["c/prd.md"].replace(b"adopted", b"partially adopted"),
                       self.blueprint()):
            self.assertFalse(M.changes_covered(base, source, published | {"c/prd.md": actual}))

    def test_contract_coverage_ignores_only_blank_separators_not_body_or_code(self):
        base = {"c/prd.md": b"# P\n## Existing\nold\n"}
        source = {"c/prd.md": base["c/prd.md"] + b"\n\n## Added\ncontract\n"}
        published = {"c/prd.md": base["c/prd.md"] + b"\n## Added\ncontract\n\n"}
        self.assertTrue(M.changes_covered(base, source, published))
        self.assertFalse(M.changes_covered(base, source, {"c/prd.md": published["c/prd.md"].replace(b"contract", b"changed")}))
        fenced = b"# P\n## Added\n```\ncontract\n\n```\n"
        self.assertFalse(M.changes_covered(base, {"c/prd.md": fenced}, {"c/prd.md": fenced.replace(b"contract\n\n", b"contract\n")}))

    def test_coverage_keeps_order_deletions_and_other_payloads_exact(self):
        base = {"c/prd.md": b"# P\n## A\na\n## B\nb\n", "c/input.json": b"old"}
        source = {"c/prd.md": b"# P\n## A\nchanged\n## B\nb\n", "c/input.json": b"new"}
        self.assertTrue(M.changes_covered(base, source, source))
        self.assertFalse(M.changes_covered(base, source, source | {"c/input.json": b"old"}))
        self.assertFalse(M.changes_covered(base, source, source | {
            "c/prd.md": b"# P\n## B\nb\n## A\nchanged\n"}))
        removed = {"c/prd.md": b"# P\n## B\nb\n", "c/input.json": b"old"}
        self.assertTrue(M.changes_covered(base, removed, removed))
        self.assertFalse(M.changes_covered(base, removed, base))
        self.assertFalse(M.changes_covered(base, base | {"c/pipeline_state.yaml": b"version: 12\n"}, base))

    def test_markdown_same_heading_disjoint_lines_still_conflict(self):
        b=b"# A\none\ntwo\n"
        rows=self.conflict(b,b.replace(b"one",b"ONE"),b.replace(b"two",b"TWO"),"same-unit-changed")
        self.assertEqual(rows[0]["section"],["# A"])

    def test_markdown_distinct_nested_paths_with_same_title(self):
        b=b"# A\n## Detail\none\n# B\n## Detail\ntwo\n"
        merged,_=self.merge(b,b.replace(b"one",b"ONE"),b.replace(b"two",b"TWO"))
        self.assertIn(b"ONE",merged["x/prd.md"]);self.assertIn(b"TWO",merged["x/prd.md"])

    def test_markdown_code_fences_hide_heading_syntax(self):
        b=b"# A\n```md\n## Duplicate\n## Duplicate\n```\n# B\nb\n"
        merged,_=self.merge(b,b.replace(b"```md",b"```text"),b.replace(b"\nb\n",b"\nB\n"))
        self.assertEqual(merged["x/prd.md"].count(b"## Duplicate"),2)
        self.assertIn(b"# B\nB",merged["x/prd.md"])

    def test_markdown_duplicate_heading_refused(self):
        b=b"# A\na\n# B\nb\n"
        self.conflict(b,b+b"# A\nagain\n",b.replace(b"\nb\n",b"\nB\n"),"markdown-heading-duplicate")

    def test_markdown_reorder_and_rename_refused(self):
        b=b"# A\na\n# B\nb\n"
        l=b.replace(b"\nb\n",b"\nB\n")
        self.conflict(b,b"# B\nb\n# A\na\n",l,"markdown-heading-reordered")
        self.conflict(b,b.replace(b"# A",b"# Renamed"),l,"markdown-heading-rename-ambiguous")

    def test_markdown_deleted_heading_vs_edit_refused(self):
        b=b"# A\na\n# B\nb\n"
        self.conflict(b,b"# B\nb\n",b.replace(b"\na\n",b"\nA\n"),"delete-versus-modify")

    def test_markdown_deleted_parent_vs_new_child_refused(self):
        b=b"# A\na\n# B\nb\n"
        self.conflict(b,b"# B\nb\n",b.replace(b"# B",b"## New\nnew\n# B"),"markdown-parent-deleted")

    def test_markdown_new_headings_in_same_gap_have_stable_insertion(self):
        b=b"# A\na\n# Z\nz\n"
        o=b.replace(b"# Z",b"# D\nd\n# Z")
        l=b.replace(b"# Z",b"# C\nc\n# Z")
        merged,evidence=self.merge(b,o,l)
        self.assertEqual(merged["x/prd.md"],b"# A\na\n# C\nc\n# D\nd\n# Z\nz\n")
        self.assertEqual(merged,self.merge(b,l,o)[0])
        self.assertEqual(len(evidence["units"]),2)

    def test_markdown_same_new_heading_content_conflicts(self):
        b=b"# A\na\n"
        self.conflict(b,b+b"# B\none\n",b+b"# B\ntwo\n","same-unit-changed")

    def test_markdown_new_headings_without_final_newline_do_not_merge_tokens(self):
        base = b"# A\nold\n"
        self.conflict(base, base + b"# B\nours", base + b"# C\ntheirs",
                      "unit-boundary-no-newline")

    def test_markdown_last_unit_may_keep_missing_final_newline(self):
        base = b"# A\nold\n"
        result, _ = self.merge(base, base + b"# B\nours\n", base + b"# C\ntheirs")
        self.assertEqual(result["x/prd.md"], b"# A\nold\n# B\nours\n# C\ntheirs")

    def test_markdown_parent_without_final_newline_cannot_swallow_new_child(self):
        base = b"# A\nold\n"
        self.conflict(base, b"# A\nchanged", base + b"## Child\nchild\n",
                      "unit-boundary-no-newline")

    def test_preamble_edit_without_newline_cannot_swallow_independent_append(self):
        base = b"> Author: old\n"
        self.conflict(base, b"> Author: new", base + b"> Date: today\n",
                      "unit-boundary-no-newline")

    def test_markdown_ambiguous_fence_and_setext_refused(self):
        b=b"# A\na\n# B\nb\n"
        self.conflict(b,b+b"```\n",b.replace(b"\na\n",b"\nA\n"),"markdown-fence-unclosed")
        self.conflict(b,b+b"Title\n=====\n",b.replace(b"\na\n",b"\nA\n"),"markdown-setext-unsupported")

    def test_preamble_independent_metadata_lines_merge(self):
        b=b"> Author: a\n> Date: old\n# A\nbody\n"
        result,_=self.merge(b,b.replace(b"Author: a",b"Author: b"),b.replace(b"Date: old",b"Date: new"))
        self.assertEqual(result["x/prd.md"],b"> Author: b\n> Date: new\n# A\nbody\n")

    def test_preamble_same_wrapped_sentence_refuses_disjoint_line_edits(self):
        b=b"The policy allows\none review.\n# A\nbody\n"
        self.conflict(b,b.replace(b"allows",b"requires"),b.replace(b"one",b"two"),"metadata-text-overlap")

    def test_preamble_same_metadata_line_is_not_synthesized(self):
        b=b"> Summary: one rule\n# A\nbody\n"
        self.conflict(b,b.replace(b"one",b"two"),b.replace(b"rule",b"policy"),"metadata-text-overlap")

    def test_yaml_distinct_keys_nested_bodies_and_version_max(self):
        b=b"version: 10\na:\n  state: old\nb: old\n"
        o=b.replace(b"version: 10",b"version: 12").replace(b"state: old",b"state: ours")
        l=b.replace(b"version: 10",b"version: 11").replace(b"b: old",b"b: latest")
        merged,evidence=self.merge(b,o,l,"c/pipeline_state.yaml")
        self.assertEqual(merged["c/pipeline_state.yaml"],b"version: 12\na:\n  state: ours\nb: latest\n")
        self.assertIn("version-max",[row["decision"] for row in evidence["units"]])

    def test_yaml_same_top_level_key_nested_independent_changes_still_refused(self):
        b=b"a:\n  x: 1\n  y: 1\n"
        self.conflict(b,b.replace(b"x: 1",b"x: 2"),b.replace(b"y: 1",b"y: 2"),"same-unit-changed","pipeline_state.yaml")

    def test_yaml_duplicate_key_anchor_alias_tag_and_multidoc_refused(self):
        b=b"a: 1\nb: 1\n";l=b.replace(b"b: 1",b"b: 2")
        for o,reason in ((b+b"a: 2\n","yaml-key-duplicate"),
                         (b"a: &source 2\nb: 1\n","yaml-indirection-unsupported"),
                         (b"a: *source\nb: 1\n","yaml-indirection-unsupported"),
                         (b"a: !!str 2\nb: 1\n","yaml-indirection-unsupported"),
                         (b"---\n"+b,"yaml-document-directive-unsupported")):
            with self.subTest(reason=reason):self.conflict(b,o,l,reason,"pipeline_state.yaml")

    def test_yaml_unbalanced_flow_and_quoted_scalar_refused(self):
        b=b"a: 1\nb: 1\n";l=b.replace(b"b: 1",b"b: 2")
        for o,reason in ((b"a: [one\nb: 1\n","yaml-flow-unbalanced"),
                         (b"a: 'unclosed\nb: 1\n","yaml-multiline-or-unclosed-quote")):
            with self.subTest(reason=reason):self.conflict(b,o,l,reason,"pipeline_state.yaml")

    def test_yaml_quoted_special_characters_and_block_scalars_remain_opaque(self):
        b=b'a: "*literal &not-anchor"\nb: |\n  # text\n  anchor: &text\nc: 1\n'
        o=b.replace(b"literal",b"quoted")
        l=b.replace(b"c: 1",b"c: 2")
        result,_=self.merge(b,o,l,"pipeline_state.yaml")
        self.assertIn(b"anchor: &text",result["pipeline_state.yaml"])
        self.assertIn(b"c: 2",result["pipeline_state.yaml"])

    def test_yaml_realistic_folded_text_and_indentationless_multiline_quoted_list(self):
        b = (b"scope: long description\n  wrapped plain scalar\n"
             b"inputs:\n- 'first line\n  second line with *literal and ''quoted'' words'\n"
             b"status:\n  scope: nested description\n    continued description\n"
             b"version: 10\n")
        o = b.replace(b"long description", b"updated description")
        l = b.replace(b"version: 10", b"version: 11")
        merged, _ = self.merge(b, o, l, "pipeline_state.yaml")
        self.assertEqual(merged["pipeline_state.yaml"],
                         o.replace(b"version: 10", b"version: 11"))

    def test_yaml_multiline_quote_cannot_swallow_another_root_key(self):
        b = b"a: 'old'\nb: 1\n"
        self.conflict(b, b"a: 'unterminated\nb: swallowed'\n",
                      b.replace(b"b: 1", b"b: 2"),
                      "yaml-multiline-or-unclosed-quote", "pipeline_state.yaml")

    def test_yaml_multiline_flow_list_is_one_key(self):
        b = b"a: [\n  one,\n  'two',\n]\nb: 1\n"
        # Closed flow values remain one unit; conflicting edits inside it
        # cannot be merged just because they occupy distinct list lines.
        self.conflict(b, b.replace(b"one", b"ONE"), b.replace(b"two", b"TWO"),
                      "same-unit-changed", "pipeline_state.yaml")

    def test_yaml_version_regression_nondecimal_and_comment_changes_refused(self):
        b=b"version: 10\na: 1\n"
        l=b.replace(b"version: 10",b"version: 11")
        for version,reason in ((b"9","yaml-version-not-monotone"),
                               (b"012","yaml-version-not-decimal"),
                               (b"12 # new comment","yaml-version-not-monotone")):
            with self.subTest(version=version):
                self.conflict(b,b.replace(b"10",version),l,reason,"pipeline_state.yaml")

    def test_yaml_new_keys_merge_in_deterministic_order(self):
        b=b"a: 1\nz: 9\n"
        o=b.replace(b"z:",b"d: 4\nz:");l=b.replace(b"z:",b"c: 3\nz:")
        result,_=self.merge(b,o,l,"pipeline_state.yaml")
        self.assertEqual(result["pipeline_state.yaml"],b"a: 1\nc: 3\nd: 4\nz: 9\n")

    def test_yaml_new_keys_without_final_newline_do_not_merge_tokens(self):
        base = b"a: 0\n"
        self.conflict(base, base + b"b: 1", base + b"c: 2",
                      "unit-boundary-no-newline", "pipeline_state.yaml")

    def test_yaml_last_key_may_keep_missing_final_newline(self):
        base = b"a: 0\n"
        result, _ = self.merge(base, base + b"b: 1\n", base + b"c: 2", "pipeline_state.yaml")
        self.assertEqual(result["pipeline_state.yaml"], b"a: 0\nb: 1\nc: 2")

    def test_unknown_yaml_filename_does_not_gain_semantic_merge(self):
        self.conflict(b"a: 1\nb: 1\n",b"a: 2\nb: 1\n",b"a: 1\nb: 2\n","same-unit-changed","other.yaml")

    def test_all_conflict_paths_and_sections_are_reported_sorted(self):
        b={"z":b"z","a":b"a"};o={"z":b"Z","a":b"A"};l={"z":b"zz","a":b"aa"}
        with self.assertRaises(M.MergeConflict) as caught:M.merge_trees(b,o,l)
        self.assertEqual([row["path"] for row in caught.exception.conflicts],["a","z"])
        self.assertEqual(caught.exception.code,"spec-merge-conflict")

    def test_bad_tree_entries_refused(self):
        for path in ("../x","/x","a//b","./a","a\\b"):
            with self.subTest(path=path),self.assertRaises(M.MergeConflict):
                M.merge_trees({}, {path:b"x"},{})


if __name__ == "__main__":
    unittest.main()
