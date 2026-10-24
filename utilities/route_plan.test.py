#!/usr/bin/env python3
"""Section 8 extraction and validation, `--route-plan` compile and verify, and `next_leg`.

The parser tests are pure. Validation and compile tests use the real compose/compile code with
fixed readiness evidence, and assert that validating writes nothing, starts nothing and records
nothing.
"""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


S = _load("framed_start_for_route_plan", "framed_start.test.py")
F, T, R, P = S.F, S.T, S.R, S.P
import route_plan as RP  # noqa: E402

DIRECT, CODE_STAGED, LAB_SETUP, brief = S.DIRECT, S.CODE_STAGED, S.LAB_SETUP, S.brief


def fenced(text):
    return "## 8. 경로 조립 제안\n\n```yaml\n" + text + "```\n"


class ExtractionTest(unittest.TestCase):
    def test_frame_leg_decision_defaults_guards_and_normalizes_reasons(self):
        decide = RP.frame_leg_decision
        self.assertEqual(decide(None), (2, "default", "기본"))
        self.assertEqual(decide("strong"), (2, "explicit", "--intensity strong 표시"))
        self.assertEqual(decide("quick", "utilities/x.py 확인"),
                         (1, "explicit", "간단 표시 --intensity quick"))
        self.assertEqual(decide("quick", "utilities/x.py 확인\ncore/WORKFLOW.md"),
                         (2, "automatic", "자동 지킴: --intensity quick이지만 과제에 core/WORKFLOW.md"))
        self.assertEqual(decide("quick", "adapters/claude/a.py adapters/codex/b.py"),
                         (2, "automatic", "자동 지킴: --intensity quick이지만 과제에 어댑터 둘 이상"))
        self.assertEqual(decide("quick", "clean", {"capability": "autopilot-spec"}),
                         (2, "automatic", "자동 지킴: --intensity quick이지만 과제에 autopilot-spec 작업"))
        self.assertEqual(decide("direct", "adapters/claude/a.py adapters/codex/b.py")[0], 2)
        self.assertEqual(decide("quick", "clean", {"capability": "autopilot-spec"})[0], 2)
        self.assertEqual(decide("quick", "score/a.py hardcore/b.py adapters/codex-extra/a.py notprd.md")[0], 1)
        self.assertEqual(decide("quick", "/tmp/work/prd.md")[0], 2)

    def parse(self, text):
        return RP.parse_proposal(text)

    def reason(self, text):
        with self.assertRaises(RP.ProposalError) as caught:
            RP.parse_proposal(text)
        return str(caught.exception)

    def test_a_valid_block_is_read_and_normalized(self):
        proposal = self.parse(brief([DIRECT, CODE_STAGED], summary="Two steps", approvals=[
            {"key": "full-run", "leg": 1, "question": "run-ok"}]))
        self.assertEqual(proposal["summary"], "Two steps")
        self.assertEqual([leg["shape"] for leg in proposal["legs"]], ["direct", "staged"])
        self.assertEqual(proposal["legs"][0], {"capability": "autopilot-code", "mode": None, "shape": "direct",
                                                "graph": None, "intensity": None, "why": "one small edit"})
        self.assertEqual(proposal["legs"][1]["graph"], ["plan", "execute", "test", "report"])
        self.assertEqual(proposal["entry_approvals"], [{"key": "full-run", "leg": 1, "question": "run-ok"}])

    def test_plan_fields_are_sealed_with_stable_ids_and_a_malformed_one_is_left_out(self):
        leg = {**DIRECT, "done_when": ["tests pass", {"text": "report written", "check": "test -f report.md"}],
               "verify": "run the abort tests", "hands_over": ["reports/latency.md"],
               "parallel": ["slice a", "slice b"], "extra_stages": [{"id": "measure", "unit": "qa/test", "after": "test"}]}
        proposal, _, notes = RP.parse_proposal_with_notes(brief([leg]))
        sealed = proposal["legs"][0]
        self.assertEqual(sealed["done_when"], [{"id": "d1", "text": "tests pass"},
                                               {"id": "d2", "text": "report written", "check": "test -f report.md"}])
        self.assertEqual((sealed["verify"], sealed["hands_over"], sealed["parallel"]),
                         ("run the abort tests", ["reports/latency.md"], ["slice a", "slice b"]))
        self.assertEqual(sealed["extra_stages"], [{"id": "measure", "unit": "qa/test", "after": "test"}])
        self.assertEqual(notes, [])
        bad = {**DIRECT, "done_when": [], "verify": "", "hands_over": [{"x": 1}], "extra_stages": [{"unit": "qa/test"}]}
        proposal, _, notes = RP.parse_proposal_with_notes(brief([bad]))
        self.assertEqual(set(proposal["legs"][0]), {"capability", "mode", "shape", "graph", "intensity", "why"})
        self.assertEqual(sorted(notes), ["ignored:legs[0].done_when", "ignored:legs[0].extra_stages",
                                         "ignored:legs[0].hands_over", "ignored:legs[0].verify"])
        self.assertEqual(RP.parse_proposal_with_notes(brief([{**DIRECT, "done_when": "one line"}]))[0]
                         ["legs"][0]["done_when"], [{"id": "d1", "text": "one line"}])

    def approvals_of(self, *questions, key="full-run"):
        rows = "".join(f"    - {{key: {key}, leg: 0, question: {q}}}\n" for q in questions)
        text = ("route_proposal_v1:\n  summary: s\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"
                "  entry_approvals:\n" + rows)
        return fenced(text)

    def test_a_question_id_that_is_not_a_slug_is_normalized_not_refused(self):
        cases = {
            "문서시작": "full-run-leg0",                       # nothing ASCII left: <key>-leg<leg>
            "Docs Start": "docs-start",                      # lower-cased, runs of other characters -> -
            "  Docs__Start!! ": "docs-start",
            "2nd-run": "q-2nd-run",                          # a leading digit gets a q- prefix
            "run-ok": "run-ok",                              # a slug is kept
            "a" * 40: "a" * 32,                              # 32 characters at most
            "a" * 31 + "-" + "b" * 5: "a" * 31,              # the cut never leaves a trailing -
        }
        for original, expected in cases.items():
            with self.subTest(original):
                proposal = self.parse(self.approvals_of(f"'{original}'"))
                self.assertEqual(proposal["entry_approvals"], [{"key": "full-run", "leg": 0, "question": expected}])
                self.assertTrue(RP._SLUG.fullmatch(expected), expected)

    def test_normalized_ids_that_collide_get_a_numeric_suffix_inside_32_characters(self):
        long_a, long_b = "x" * 40, "x" * 36 + "-zzzz"      # both cut to the same 32 characters
        proposal = self.parse(self.approvals_of(f"'{long_a}'", f"'{long_b}'", "'xx'", "'xx'"))
        ids = [row["question"] for row in proposal["entry_approvals"]]
        self.assertEqual(ids, ["x" * 32, "x" * 30 + "-2", "xx", "xx"])   # a slug the brief repeats is left as written
        self.assertTrue(all(RP._SLUG.fullmatch(i) for i in ids), ids)
        korean = self.parse(self.approvals_of("'문서'", "'시작'", "'full-run-leg0'"))
        self.assertEqual([r["question"] for r in korean["entry_approvals"]],
                         ["full-run-leg0-2", "full-run-leg0-3", "full-run-leg0"])

    def test_the_original_question_id_stays_in_the_source_and_is_listed_for_display(self):
        text = self.approvals_of("'문서시작'", "run-ok", "'Docs Start'")
        proposal, source = RP.parse_proposal_with_source(text)
        self.assertIn("문서시작", source)
        self.assertEqual(proposal["entry_approvals"][0], {"key": "full-run", "leg": 0, "question": "full-run-leg0"})
        self.assertEqual(RP.question_renames(source), [
            {"key": "full-run", "leg": 0, "question": "full-run-leg0", "original": "문서시작"},
            {"key": "full-run", "leg": 0, "question": "docs-start", "original": "Docs Start"}])
        self.assertEqual(RP.question_renames(self.approvals_of("run-ok").split("```")[1].replace("yaml\n", "", 1)), [])
        self.assertEqual(RP.question_renames("not: [yaml"), [])

    def test_a_non_string_question_or_a_bad_row_is_still_refused(self):
        for bad in ("123", "[a]", "{a: b}", "null", "true"):
            with self.subTest(bad):
                self.assertEqual(self.reason(self.approvals_of(bad)), "schema-invalid:entry_approvals")
        for row in ("{key: full-run, leg: 5, question: a}", "{key: full-run, leg: true, question: a}",
                    "{key: full-run, leg: 0}", "{key: full-run, leg: 0, question: a, extra: 1}"):
            with self.subTest(row):
                text = ("route_proposal_v1:\n  summary: s\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"
                        f"  entry_approvals:\n    - {row}\n")
                self.assertEqual(self.reason(fenced(text)), "schema-invalid:entry_approvals")

    def test_every_none_reason(self):
        ok = "route_proposal_v1:\n  summary: s\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"
        cases = {
            "section-missing": "## 1. Problem\n\nx\n" + "",
            "block-missing": "## 8. 경로 조립 제안\n\nno block here\n",
            "yaml-invalid": fenced("route_proposal_v1: [unclosed\n"),
            "yaml-alias": fenced("route_proposal_v1:\n  summary: &a s\n  legs:\n    - {capability: autopilot-code, shape: direct}\n  x: *a\n"),
            "yaml-tag": fenced("route_proposal_v1:\n  summary: !custom s\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"),
            "yaml-invalid-duplicate": fenced("route_proposal_v1:\n  summary: a\n  summary: b\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"),
            "schema-invalid:document": fenced("route_proposal_v1: [not, a, mapping]\n"),
            "schema-invalid:legs": fenced("route_proposal_v1:\n  summary: s\n  legs: []\n"),
            "schema-invalid:summary": fenced("route_proposal_v1:\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"),
            "schema-invalid:legs[0].shape": fenced("route_proposal_v1:\n  summary: s\n  legs:\n    - {capability: autopilot-code, shape: batch}\n"),
            "schema-invalid:legs[0].graph-only-staged": fenced(
                "route_proposal_v1:\n  summary: s\n  legs:\n    - {capability: autopilot-code, shape: direct, graph: [execute]}\n"),
            "leg-invalid:0:frame-stage-not-allowed": fenced(
                "route_proposal_v1:\n  summary: s\n  legs:\n    - {capability: autopilot-code, shape: staged, graph: [frame, plan]}\n"),
            "schema-invalid:entry_approvals": fenced(
                ok + "  entry_approvals:\n    - {key: bogus, leg: 0, question: q}\n"),
            "section-too-large": fenced(ok + "  # " + "x" * RP.MAX_SECTION_BYTES + "\n"),
        }
        for expected, text in cases.items():
            with self.subTest(expected):
                reason = self.reason(text)
                self.assertTrue(reason.startswith(expected.split("-duplicate")[0]), (expected, reason))
        self.assertEqual(self.reason("x" * (RP.MAX_BRIEF_BYTES + 1)), "brief-too-large")

    def test_five_legs_and_five_approvals_are_refused(self):
        legs = "".join("    - {capability: autopilot-code, shape: direct}\n" for _ in range(5))
        self.assertEqual(self.reason(fenced("route_proposal_v1:\n  summary: s\n  legs:\n" + legs)), "schema-invalid:legs")

    def test_only_a_yaml_fence_with_the_schema_counts_and_only_section_8(self):
        body = "route_proposal_v1:\n  summary: s\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"
        self.assertEqual(self.reason("## 8. 경로 조립 제안\n\n```json\n" + body + "```\n"), "block-missing")
        self.assertEqual(self.reason("## 8. 경로 조립 제안\n\n```yaml\nsomething: else\n```\n"), "block-missing")
        elsewhere = "## 7. Questions\n\n```yaml\n" + body + "```\n\n## 8. 경로 조립 제안\n\nnone\n"
        self.assertEqual(self.reason(elsewhere), "block-missing")
        after = "## 8. 경로 조립 제안\n\nnone\n\n## 9. Notes\n\n```yaml\n" + body + "```\n"
        self.assertEqual(self.reason(after), "block-missing")
        heading_variants = ("### 8. 경로 조립 제안", "8. **경로 조립 제안**", "## 8) 경로 조립 제안 (proposal)",
                            "## 8. Route proposal", "**8. Route proposal**", "8) Route", "## 8 Route")
        for heading in heading_variants:
            with self.subTest(heading):
                self.assertEqual(self.parse(f"{heading}\n\n```yaml\n{body}```\n")["legs"][0]["shape"], "direct")
        self.assertEqual(self.reason("## 80. Route proposal\n\nnone\n"), "section-missing")

    def test_a_numbered_list_running_past_8_does_not_hide_section_8(self):
        body = "route_proposal_v1:\n  summary: s\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"
        risks = "## 6. Open Risks\n\n" + "".join(f"{n}. risk {n}\n" for n in range(1, 10))
        listed = "## 8. Route proposal\n\n1. first read this\n9. then this\n\n```yaml\n" + body + "```\n"
        for text in (risks + "\n## 7. Questions\n\nnone\n\n## 8. Route proposal\n\n```yaml\n" + body + "```\n",
                     risks + "\n8) Route\n\n```yaml\n" + body + "```\n",
                     listed):
            with self.subTest(text[:40]):
                self.assertEqual(self.parse(text)["legs"][0]["shape"], "direct")
        self.assertEqual(self.reason(risks + "\n## 8. Route proposal\n\nnone\n"), "block-missing")

    def test_numbered_risks_do_not_adopt_a_proposal_from_section_7(self):
        body = "route_proposal_v1:\n  summary: actual section 8\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"
        risks = "## 6. Open Risks\n\n" + "".join(f"{n}. risk {n}\n" for n in range(1, 10))
        elsewhere = (risks + "\n## 7. Questions\n\n```yaml\n"
                     + body.replace("actual section 8", "wrong section 7") + "```\n\n")
        for heading in ("## 8. Route proposal", "## 8. 경로 조립 제안",
                        "**8. Route proposal**", "8) Route"):
            with self.subTest(heading):
                self.assertEqual(self.reason(elsewhere + heading + "\n\nnone\n"), "block-missing")
                actual = elsewhere + heading + "\n\n```yaml\n" + body + "```\n"
                self.assertEqual(self.parse(actual)["summary"], "actual section 8")

    def test_a_brief_with_no_section_8_heading_is_read_whole_and_says_so(self):
        body = "route_proposal_v1:\n  summary: s\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"
        proposal, _, notes = RP.parse_proposal_with_notes("## Route\n\n```yaml\n" + body + "```\n")
        self.assertEqual(proposal["legs"][0]["shape"], "direct")
        self.assertEqual(notes, ["section-8-heading-missing:read-whole-brief"])

    def test_several_blocks_read_the_first_one_that_holds_a_proposal(self):
        ok = "route_proposal_v1:\n  summary: first\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"
        other = ok.replace("first", "second")
        proposal, _, notes = RP.parse_proposal_with_notes(fenced(ok) + "\n```yaml\n" + other + "```\n")
        self.assertEqual((proposal["summary"], notes), ("first", ["blocks:2:read-1"]))
        broken = fenced("route_proposal_v1: [unclosed\n") + "\n```yaml\n" + other + "```\n"
        proposal, _, notes = RP.parse_proposal_with_notes(broken)
        self.assertEqual((proposal["summary"], notes), ("second", ["blocks:2:read-2"]))

    def test_unknown_fields_are_ignored_and_named(self):
        text = fenced("route_proposal_v1:\n  summary: s\n  done_when: [tests pass]\n  legs:\n"
                      "    - {capability: autopilot-code, shape: direct, budget: 2h}\nnote: x\n")
        proposal, _, notes = RP.parse_proposal_with_notes(text)
        self.assertEqual(proposal["legs"][0]["capability"], "autopilot-code")
        self.assertNotIn("budget", proposal["legs"][0])
        self.assertEqual(notes, ["ignored:note", "ignored:done_when", "ignored:legs[0]:budget"])

    def test_cwd_is_optional_and_an_interview_cannot_silently_change_the_worktree(self):
        one = self.parse(brief([{**DIRECT, "cwd": "/existing/worktree one"}]))
        two = self.parse(brief([{**DIRECT, "cwd": "/existing/worktree two"}]))
        self.assertEqual(one["legs"][0]["cwd"], "/existing/worktree one")
        self.assertTrue(RP.same_proposal(one, one))
        self.assertFalse(RP.same_proposal(one, two))
        self.assertNotIn("cwd", self.parse(brief([DIRECT]))["legs"][0])

    def test_an_indented_fence_under_a_list_item_is_read(self):
        body = "route_proposal_v1:\n  summary: s\n  legs:\n    - {capability: autopilot-code, shape: direct}\n"
        indented = "".join("   " + line + "\n" for line in body.splitlines())
        text = "8. **경로 조립 제안**\n\n   ```yaml\n" + indented + "   ```\n"
        self.assertEqual(self.parse(text)["legs"][0]["capability"], "autopilot-code")

    def test_a_runtime_without_yaml_reports_it(self):
        text = brief([DIRECT])
        with mock.patch.dict(sys.modules, {"yaml": None}):
            self.assertEqual(self.reason(text), "yaml-unavailable")
            self.assertFalse(RP.yaml_available())        # what the framed start asks before any frame leg
        self.assertTrue(RP.yaml_available())

    def test_none_text(self):
        self.assertEqual(RP.none_text("block-missing"), "proposal:none(block-missing)")


class ValidationBase(F.FramedBase):
    """Real compose with fixed readiness evidence; a real frame route so its cycle exists."""

    def setUp(self):
        super().setUp()
        self.route, self.path = self.admitted()
        self.output = self.begin_cycle(self.path)
        self.cycle = P.list_cycle_records(self.root)[0]["cycle_id"]
        self.probes = 0

    def readiness(self):
        """Memoized like the runtime's probe: one probe serves every leg that needs evidence."""
        if not self.probes:
            self.probes = 1
            self.evidence = {"tuples": [T.nested("claude", "codex")],
                             "candidates": T.registered_headless()["candidates"]}
        return self.evidence

    def compile(self, leg, index=0):
        return R.compile_proposal_leg(leg, index, frame_route=self.route, frame_cycle_id=self.cycle,
                                      readiness=self.readiness)

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


class EntryExecutionScopeTest(ValidationBase):
    def test_proposal_scope_is_optional_and_validated(self):
        proposal = {"legs": [LAB_SETUP], "entry_approvals": [], "execution_scope": "report"}
        facts = RP.validate_proposal(proposal, compile_leg=self.compile,
                                     start_approvals=R.route_start_approvals)
        self.assertEqual(facts["execution_scope"], "report")
        self.assertEqual(RP.validate_proposal({"legs": [LAB_SETUP], "entry_approvals": []}, compile_leg=self.compile,
                                              start_approvals=R.route_start_approvals)["execution_scope"], "complete")
        with self.assertRaises(RP.ProposalError):
            RP.validate_proposal({"legs": [LAB_SETUP], "entry_approvals": [], "execution_scope": "deploy"},
                                 compile_leg=self.compile, start_approvals=R.route_start_approvals)


class ProposalValidationTest(ValidationBase):
    def write_brief(self, legs, **kw):
        path = self.output / "shards/frame/direction-brief.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(brief(legs, **kw), encoding="utf-8")
        return path

    def evaluate(self, legs, **kw):
        path = self.write_brief(legs, **kw)
        return RP.evaluate_brief(path, root=self.root, node="frame", compile_leg=self.compile,
                                 start_approvals=R.route_start_approvals)

    def test_a164_5_partial_graphs_unit_overrides_and_shapes_compile_by_the_ordinary_rules(self):
        cases = {
            "code plan-execute-test-report": (CODE_STAGED, ["plan", "execute", "test", "report"]),
            "lab eval": ({"capability": "autopilot-lab", "mode": "eval", "shape": "staged",
                          "graph": ["eval-run", "metrics", "report"]}, ["eval-run", "metrics", "report"]),
            "unit override": ({"capability": "autopilot-code", "shape": "staged",
                               "graph": ["execute:dev/refactor", "test"]}, ["execute", "test"]),
        }
        for label, (leg, graph) in cases.items():
            with self.subTest(label):
                row = self.evaluate([leg])
                self.assertEqual(row["reason"], "valid", row)
                self.assertEqual([n for n in row["facts"]["legs"][0]["graph"]], graph)
        direct = self.evaluate([DIRECT])
        self.assertEqual(direct["facts"]["legs"][0], {"capability": "autopilot-code", "mode": "dev",
                                                      "shape": "direct", "graph": None, "intensity": "direct"})
        solo = self.evaluate([{"capability": "autopilot-code", "shape": "solo"}])
        self.assertEqual(solo["facts"]["legs"][0]["intensity"], "quick")

    def test_validation_writes_nothing_starts_nothing_and_records_no_route_chain_line(self):
        path = self.write_brief([CODE_STAGED, DIRECT, {"capability": "autopilot-code", "shape": "solo"}])
        before, rows = self.tree(), self.jobs.read_text()
        with mock.patch.object(R, "_record_route_chain", side_effect=AssertionError("ledger line")), \
                mock.patch.object(R, "_emit_compiled_route", side_effect=AssertionError("published")), \
                mock.patch.object(R, "write_once", side_effect=AssertionError("written")), \
                mock.patch.object(P, "begin", side_effect=AssertionError("producer")), \
                mock.patch.object(P, "prepare_route_artifact_env", side_effect=AssertionError("producer env")), \
                mock.patch("work_start.start_work", side_effect=AssertionError("started")):
            row = RP.evaluate_brief(path, root=self.root, node="frame", compile_leg=self.compile,
                                    start_approvals=R.route_start_approvals)
        self.assertEqual(row["reason"], "valid", row)
        self.assertEqual(self.tree(), before)                      # not one file, route or ledger line appeared
        self.assertEqual(self.jobs.read_text(), rows)
        self.assertEqual(self.probes, 1)                           # one probe served every leg that needed evidence

    def test_the_compile_has_no_frame_nodes_and_none_of_the_frame_gate(self):
        for leg in (CODE_STAGED, {"capability": "autopilot-code", "shape": "staged"},
                    {"capability": "autopilot-code", "shape": "solo"}):
            with self.subTest(leg.get("graph") or leg["shape"]):
                route = self.compile(leg)
                self.assertFalse([n for n in route["nodes"] if R._frame_node(n)])
                self.assertNotIn("frame-review", route["human_gates"])
                self.assertFalse([b for b in route["human_gate_bindings"] if b["gate"] == "frame-review"])
                self.assertNotIn("route_plan", route)          # validating seals nothing

    def test_an_invalid_later_leg_ends_the_proposal_before_it_and_an_invalid_first_leg_makes_it_none(self):
        bad_graph = {"capability": "autopilot-code", "shape": "staged", "graph": ["test", "execute"]}   # order
        row = self.evaluate([DIRECT, bad_graph, dict(DIRECT)], approvals=[{"key": "full-run", "leg": 2, "question": "q"}])
        self.assertEqual(row["reason"], "valid", row)
        self.assertEqual([leg["shape"] for leg in row["proposal"]["legs"]], ["direct"])
        self.assertEqual((len(row["facts"]["legs"]), row["proposal"]["entry_approvals"]), (1, []))
        self.assertRegex(row["read_notes"][0], r"^leg-invalid:1:compose-graph-order:test-before-execute")
        self.assertEqual(row["read_notes"][1:], ["entry-approval-mismatch:full-run@2"])
        self.assertIsNone(self.evaluate([bad_graph, DIRECT])["proposal"])
        unknown = self.evaluate([{"capability": "autopilot-nope", "shape": "direct"}])
        self.assertRegex(unknown["reason"], r"^leg-invalid:0:compose-capability-unknown")
        mode = self.evaluate([{"capability": "autopilot-code", "mode": "nope", "shape": "direct"}])
        self.assertRegex(mode["reason"], r"^leg-invalid:0:compose-mode-unknown")
        unit = self.evaluate([{"capability": "autopilot-code", "shape": "staged", "graph": ["execute:qa/ml-debug"]}])
        self.assertRegex(unit["reason"], r"^leg-invalid:0:compose-unit-not-in-choices")
        tier = self.evaluate([{"capability": "autopilot-code", "shape": "direct", "intensity": "strong"}])
        self.assertRegex(tier["reason"], r"^leg-invalid:0:compose-shape-intensity-mismatch:direct")
        for row in (bad_graph and self.evaluate([bad_graph]),):
            self.assertIsNone(row["proposal"])

    def test_a_readiness_probe_that_fails_makes_the_leg_none_not_an_error(self):
        def broken():
            raise ValueError("compose-readiness-unavailable:no harness")
        path = self.output / "shards/frame/direction-brief.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(brief([CODE_STAGED]), encoding="utf-8")
        compile_leg = lambda leg, index: R.compile_proposal_leg(
            leg, index, frame_route=self.route, frame_cycle_id=self.cycle, readiness=broken)
        row = RP.evaluate_brief(path, root=self.root, node="frame", compile_leg=compile_leg,
                                start_approvals=R.route_start_approvals)
        self.assertRegex(row["reason"], r"^leg-invalid:0:compose-readiness-unavailable")

    def test_entry_approvals_are_reconciled_with_the_parts_the_legs_carry(self):
        ok = self.evaluate([LAB_SETUP], approvals=[{"key": "full-run", "leg": 0, "question": "run-ok"}])
        self.assertEqual(ok["reason"], "valid", ok)
        self.assertEqual([(a["leg"], a["start_approval"], a["part"]) for a in ok["facts"]["start_approvals"]],
                         [(0, "full-run", "autopilot-lab:full-run")])
        # A row for a part its leg does not carry is left out and named; the proposal stays valid.
        wrong_leg = self.evaluate([DIRECT, LAB_SETUP], approvals=[{"key": "full-run", "leg": 0, "question": "q"}])
        self.assertEqual((wrong_leg["reason"], wrong_leg["proposal"]["entry_approvals"]), ("valid", []))
        self.assertEqual(wrong_leg["read_notes"], ["entry-approval-mismatch:full-run@0"])
        absent = self.evaluate([CODE_STAGED], approvals=[{"key": "deploy", "leg": 0, "question": "q"}])
        self.assertEqual(absent["read_notes"], ["entry-approval-mismatch:deploy@0"])
        # No approval is asked for: the proposal is valid and the decision gates it later.
        bare = self.evaluate([LAB_SETUP])
        self.assertEqual(bare["reason"], "valid")
        self.assertEqual(len(bare["facts"]["start_approvals"]), 1)

    def test_a_borrowed_full_run_part_shows_its_approval(self):
        borrowed = {"capability": "autopilot-code", "shape": "staged",
                    "graph": ["execute", "autopilot-lab:smoke", "autopilot-lab:full-run", "report"]}
        row = self.evaluate([borrowed], approvals=[{"key": "full-run", "leg": 0, "question": "q"}])
        self.assertEqual(row["reason"], "valid", row)
        self.assertEqual([a["part"] for a in row["facts"]["start_approvals"]], ["autopilot-lab:full-run"])
        self.assertEqual(R.declared_start_approvals(row["proposal"]["legs"][0]),
                         [("full-run", "autopilot-lab:full-run")])

    def test_equality_follows_normalized_legs_and_approval_scope_not_wording(self):
        first = self.evaluate([CODE_STAGED], summary="One wording")
        second = self.evaluate([{**CODE_STAGED, "why": "another reason"}], summary="Another wording")
        third = self.evaluate([{**CODE_STAGED, "graph": ["plan", "execute", "test"]}])
        self.assertTrue(RP.proposals_equal([first, second]))
        self.assertTrue(RP.wording_differs([first, second]))
        self.assertFalse(RP.proposals_equal([first, third]))
        none = self.evaluate([{"capability": "nope", "shape": "direct"}])
        self.assertFalse(RP.proposals_equal([first, none]))
        self.assertFalse(RP.wording_differs([first, none]))
        self.assertTrue(RP.same_proposal(first["proposal"], second["proposal"]))
        self.assertFalse(RP.same_proposal(first["proposal"], third["proposal"]))
        # D11: an interview may copy either form the review shows -- the brief's own legs or their compiled legs
        refine = self.evaluate([{"capability": "autopilot-refine", "shape": "staged", "graph": ["review", "transaction"]},
                                {"capability": "autopilot-code", "shape": "staged", "graph": ["execute:dev/refactor", "test"]}])
        shown = refine["facts"]["legs"]
        self.assertEqual([(leg["mode"], leg["intensity"], leg["graph"]) for leg in shown],
                         [("default", "standard", ["review", "transaction"]), ("dev", "standard", ["execute", "test"])])
        self.assertFalse(RP.same_proposal(refine["proposal"], {"legs": shown}))     # exact compare: the D11 ending
        self.assertTrue(RP.same_proposal(refine["proposal"], {"legs": shown}, resolved=shown))
        self.assertTrue(RP.same_proposal(refine["proposal"], refine["proposal"], resolved=shown))
        for index, change in ((0, {"capability": "autopilot-draft"}), (0, {"mode": "dev"}), (1, {"mode": "debug"}),
                              (0, {"shape": "solo"}), (0, {"graph": ["review"]}), (1, {"graph": ["execute:qa/ml-debug", "test"]}),
                              (0, {"intensity": "strong"})):
            with self.subTest(index=index, change=change):
                legs = [dict(leg) for leg in shown]
                legs[index].update(change)
                self.assertFalse(RP.same_proposal(refine["proposal"], {"legs": legs}, resolved=shown))
        # a copy is one of the two complete legs, never a per-key blend of them
        for index in (0, 1):
            with self.subTest(index=index, blend="own+resolved"):
                own = refine["proposal"]["legs"][index]
                differing = [key for key in RP._LEG_KEYS if own.get(key) != shown[index].get(key)]
                self.assertGreaterEqual(len(differing), 2, differing)
                for key in differing:
                    blend = [dict(leg) for leg in shown]
                    blend[index][key] = own.get(key)
                    self.assertFalse(RP.same_proposal(refine["proposal"], {"legs": blend}, resolved=shown), key)
        pure = [dict(own) for own in refine["proposal"]["legs"]]
        self.assertTrue(RP.same_proposal(refine["proposal"], {"legs": pure}, resolved=shown))   # all own: still matches
        pure[1] = dict(shown[1])
        self.assertTrue(RP.same_proposal(refine["proposal"], {"legs": pure}, resolved=shown))   # one leg each: still matches
        self.assertFalse(RP.same_proposal(refine["proposal"], {"legs": shown[:1]}, resolved=shown))   # leg count
        self.assertFalse(RP.same_proposal(refine["proposal"], {"legs": shown + shown[:1]}, resolved=shown))
        # an omitted mode/intensity equals its explicit default once compiled
        implicit = self.evaluate([{"capability": "autopilot-code", "shape": "staged", "graph": ["execute", "test"]}])
        explicit = self.evaluate([{"capability": "autopilot-code", "mode": "dev", "shape": "staged",
                                   "graph": ["execute", "test"], "intensity": "standard"}])
        self.assertTrue(RP.proposals_equal([implicit, explicit]))


class FirstLegInputsTest(ValidationBase):
    """A164-12: a leg's brief inputs come from the frame cycle, which stays open and unchanged."""

    CANONICAL = ("shards/frame/direction-brief.md", "shards/frame-alternative/direction-brief.md")

    def test_a164_12_each_capability_reads_the_frame_cycles_two_briefs_without_copying_them(self):
        F.FramedBase.write_frame_outputs(self.output)
        before = self.tree()
        cases = (("autopilot-code", None, "plan", self.CANONICAL),
                 ("autopilot-draft", "doc", "material-strategy", self.CANONICAL),
                 ("autopilot-refine", None, "review", self.CANONICAL),
                 ("autopilot-design", None, "refs", (
                     "designs/<cycle>/01_refs/frame/direction-brief.md",
                     "designs/<cycle>/01_refs/frame-alternative/direction-brief.md")),
                 ("autopilot-spec", "api", "research", (
                     "spec/_internal/research/frame/direction-brief.md",
                     "spec/_internal/research/frame-alternative/direction-brief.md")))
        for capability, mode, consumer, names in cases:
            with self.subTest(capability):
                route = self.compile({"capability": capability, "mode": mode, "shape": "staged", "graph": None,
                                      "intensity": None})
                node = next(n for n in route["nodes"] if n["id"] == consumer)
                self.assertEqual(sorted(node["input_sources"]), sorted(names))
                for name, source in node["input_sources"].items():
                    self.assertEqual(source["cycle_id"], self.cycle)
                    self.assertTrue(source["path"].endswith(self.CANONICAL[names.index(name)]), source)
                    self.assertTrue((self.root / source["path"]).is_file())
                self.assertEqual(route["parent_cycle_id"], self.cycle)
                self.assertFalse([n for n in route["nodes"] if R._frame_node(n)])
        self.assertEqual(self.tree(), before)                      # nothing was copied anywhere
        self.assertEqual(P.read_cycle_record(self.root, self.cycle)["state"], "open")   # an open parent: a link only

    def test_a_leg_that_needs_no_brief_gets_no_source_and_a_missing_brief_leaves_the_input_out(self):
        route = self.compile({"capability": "autopilot-code", "mode": None, "shape": "staged",
                              "graph": ["execute", "test"], "intensity": None})
        self.assertFalse([n["id"] for n in route["nodes"] if n.get("input_sources")])
        gone = self.compile({"capability": "autopilot-code", "mode": None, "shape": "staged", "graph": None,
                             "intensity": None})        # the frame outputs were never written in this fixture
        self.assertFalse([n["id"] for n in gone["nodes"] if n.get("input_sources")])


class PlanFixture(S.StartBase):
    """A real decision record (two legs) produced by the real flow, for the `--route-plan` tests."""

    LEGS = [DIRECT, {"capability": "autopilot-code", "shape": "staged", "graph": ["execute", "test", "report"]}]

    def setUp(self):
        super().setUp()
        self.legs = self.LEGS
        self.set_briefs(self.legs, self.legs)
        self.set_interview({"legs": self.legs})
        self.first = self.settle()
        self.decision_path = self.record_path()
        self.record_data = self.record()

    def binding(self, index=0):
        return RP.read_route_plan(f"{self.decision_path}#{index}", self.root)


class RoutePlanArgumentTest(PlanFixture):
    def test_the_argument_reads_a_selected_record_and_its_leg(self):
        binding = self.binding(1)
        self.assertEqual((binding["index"], binding["leg"]["shape"]), (1, "staged"))
        self.assertEqual(binding["digest"], self.record_data["digest"])
        self.assertEqual(binding["decision"], self.decision_path.relative_to(self.root).as_posix())
        self.assertEqual(RP.sealed_form(binding), {"decision": binding["decision"], "digest": binding["digest"], "index": 1})
        relative = RP.read_route_plan(f"{binding['decision']}#0", self.root)
        self.assertEqual(relative["digest"], binding["digest"])

    def test_an_unreadable_record_or_index_is_a_reason_never_a_crash(self):
        for label, argument in (("no separator", str(self.decision_path)), ("index out of range", f"{self.decision_path}#2"),
                                ("not a number", f"{self.decision_path}#x"), ("missing file", f"{self.root}/nope.json#0"),
                                ("outside the root", f"{HERE / 'route_plan.py'}#0")):
            with self.subTest(label), self.assertRaises(ValueError):
                RP.read_route_plan(argument, self.root)
        bad = self.root / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            RP.read_route_plan(f"{bad}#0", self.root)

    def test_a_none_record_is_not_a_route_plan(self):
        none = RP.build_record(RP.none_decision(frame_route=self.record_data["decision"]["frame_route"],
                                                briefs=self.record_data["decision"]["briefs"],
                                                intent=self.record_data["decision"]["intent"]))
        path = self.root / "none-record.json"
        path.write_bytes(RP.render(none))
        with self.assertRaisesRegex(ValueError, "route-plan-invalid:none"):
            RP.read_route_plan(f"{path}#0", self.root)

    def test_the_sealed_field_is_format_checked_only(self):
        good = RP.sealed_form(self.binding())
        self.assertEqual(RP.validate_sealed(good), good)
        for bad in ({**good, "index": 4}, {**good, "digest": "md5:x"}, {**good, "decision": "/abs/path"},
                    {**good, "decision": "../x"}, {"decision": good["decision"]}, "x", {**good, "extra": 1}):
            with self.subTest(bad), self.assertRaises(ValueError):
                RP.validate_sealed(bad)

    def test_display_plan_is_the_capability_order_with_adjacent_repeats_shown_once(self):
        self.assertEqual(RP.display_plan([{"capability": "autopilot-code"}, {"capability": "autopilot-code"},
                                          {"capability": "audit"}, {"capability": "autopilot-lab"}]),
                         ["code", "audit", "lab"])


class RoutePlanCompileTest(PlanFixture):
    def leg(self, **kw):
        arguments = dict(capability="autopilot-code", capability_mode=None, shape="staged", graph=None,
                         slug="plan-leg", cwd=R.ROOT, artifact_root=self.root, spec_read="fixture",
                         campaign_key="framed-key", parent_cycle_id=self.record_data["decision"]["frame_route"]["cycle_id"],
                         dispatch_evidence={"tuples": [T.nested("claude", "codex")]},
                         registered_headless_evidence=T.registered_headless(),
                         work_request={"text": "do it", "owner_harness": None})
        arguments.update(kw)
        return R.compose_route(**arguments)

    def test_a164_5_a_staged_graph_is_compiled_as_given_without_frames_and_seals_the_reference(self):
        route = self.leg(graph="execute,test,report", route_plan=self.binding(1))
        self.assertEqual(route["route_plan"], RP.sealed_form(self.binding(1)))
        self.assertEqual([n["id"] for n in route["nodes"]], ["execute", "test", "report"])
        verified = R.verify_route(json.loads(json.dumps(route)), R.ROOT)
        self.assertEqual(verified["route_plan"]["index"], 1)

    def test_a_staged_route_without_a_graph_is_the_recipe_order_minus_frame_nodes(self):
        with mock.patch.object(R, "DECIDED_SHAPES", ()):     # the recipe as sealed before decided shapes
            framed_recipe = self.leg()
        plain = self.leg()
        planned = self.leg(route_plan=self.binding(1))
        self.assertIn("frame", [n["id"] for n in framed_recipe["nodes"]])
        ids = [n["id"] for n in planned["nodes"]]
        self.assertEqual(ids, [i for i in [n["id"] for n in framed_recipe["nodes"]] if i not in ("frame", "frame-alternative")])
        self.assertEqual([n["id"] for n in plain["nodes"]], ids)        # a decided staged route: the same order
        self.assertEqual(planned["composed"], True)
        self.assertNotIn("frame-review", planned["human_gates"])
        self.assertEqual(planned["nodes"][0]["depends_on"], [])
        R.verify_route(json.loads(json.dumps(planned)), R.ROOT)
        self.assertNotIn("route_plan", plain)                     # no field on a route that has no plan

    def test_a_capability_with_no_frame_keeps_its_preset_when_planned(self):
        lab = dict(capability="autopilot-lab", capability_mode="setup")
        plain = self.leg(**lab)
        planned = self.leg(**lab, route_plan=self.binding(1))
        self.assertNotIn("composed", planned)
        self.assertEqual([n["id"] for n in planned["nodes"]], [n["id"] for n in plain["nodes"]])
        self.assertEqual(planned["route_plan"]["index"], 1)
        R.verify_route(json.loads(json.dumps(planned)), R.ROOT)

    def test_a_solo_route_runs_its_one_shot_without_the_quick_frame_bootstrap(self):
        with mock.patch.object(R, "DECIDED_SHAPES", ()):
            self.assertEqual([n["id"] for n in self.leg(shape="solo")["nodes"]], ["frame", "frame-alternative", "one-shot"])
        plain = self.leg(shape="solo")
        planned = self.leg(shape="solo", route_plan=self.binding(1))
        self.assertEqual([n["id"] for n in plain["nodes"]], ["one-shot"])
        R.verify_route(json.loads(json.dumps(plain)), R.ROOT)
        self.assertEqual([n["id"] for n in planned["nodes"]], ["one-shot"])
        self.assertEqual(planned["nodes"][0]["depends_on"], [])
        self.assertNotIn("frame-review", planned["human_gates"])
        self.assertEqual(planned["human_gate_bindings"], [])
        R.verify_route(json.loads(json.dumps(planned)), R.ROOT)

    def test_a_direct_route_keeps_its_inline_shape_and_seals_the_reference(self):
        route = self.leg(shape="direct", route_plan=self.binding(1))
        self.assertEqual([n["id"] for n in route["nodes"]], ["inline"])
        self.assertEqual(route["route_plan"]["index"], 1)
        R.verify_route(json.loads(json.dumps(route)), R.ROOT)

    def test_a_route_without_a_plan_keeps_its_bytes(self):
        for shape, graph in (("direct", None), ("solo", None), ("staged", "execute,test"), ("staged", None)):
            with self.subTest(shape, graph=graph):
                one = self.leg(shape=shape, graph=graph)
                two = self.leg(shape=shape, graph=graph, route_plan=None)
                self.assertEqual(R.route_hash(one), R.route_hash(two))
                self.assertNotIn("route_plan", one)

    def test_a_continuation_inherits_the_plan_reference_and_verifies(self):
        source = self.leg(graph="execute,test,report", route_plan=self.binding(1))
        continuation = R.build_continuation_route(source, resume_from_node="execute", requested_boundary="execute",
                                                  reason="resume the planned leg", artifact_root=self.root)
        self.assertIsNone(continuation.get("requested_boundary_blocker"))
        self.assertEqual(continuation["route_plan"], source["route_plan"])
        verified = R.verify_route(json.loads(json.dumps(continuation)), R.ROOT)
        self.assertEqual(verified["route_plan"], RP.sealed_form(self.binding(1)))
        plain = self.leg(graph="execute,test,report")
        again = R.build_continuation_route(plain, resume_from_node="execute", requested_boundary="execute",
                                           reason="resume", artifact_root=self.root)
        self.assertNotIn("route_plan", again)                     # a route that never had a plan gains none

    def test_a_tampered_sealed_field_does_not_verify_and_a_missing_record_does(self):
        route = self.leg(shape="direct", route_plan=self.binding(1))
        forged = {**route, "route_plan": {**route["route_plan"], "index": 9}}
        forged["route_hash"] = R.route_hash(forged)
        forged["route_id"] = "rt-" + forged["route_hash"].split(":", 1)[1][:16]
        with self.assertRaisesRegex(ValueError, "route-plan-invalid:sealed"):
            R.verify_route(forged, R.ROOT)
        self.decision_path.unlink()
        R.verify_route(json.loads(json.dumps(route)), R.ROOT)       # a vanished record is not a route-hash error
        self.assertIsNone(RP.project_next_leg(route, "cyc_" + "a" * 32))

    def test_the_card_names_an_unreadable_plan_with_one_line(self):
        route = self.leg(shape="direct")
        self.assertNotIn("경로 계획을 읽지 못함", R.compose_card(route))
        card = R.compose_card(route, route_plan_unreadable=True)
        self.assertEqual(card.count("경로 계획을 읽지 못함"), 1)

    def test_the_compose_cli_treats_an_unreadable_plan_as_absent_plus_one_card_line(self):
        common = [sys.executable, str(HERE / "capability-route.py"), "compose", "--shape", "direct", "--slug", "cli-leg",
                  "--capability", "autopilot-code", "--cwd", str(R.ROOT), "--artifact-root", str(self.root),
                  "--campaign-key", "framed-key", "--spec-read", "fixture", "--explain"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_DISPATCH_")}
        plain = subprocess.run(common, text=True, capture_output=True, env=env)
        bad = subprocess.run([*common, "--route-plan", f"{self.root}/nope.json#0"], text=True, capture_output=True, env=env)
        self.assertEqual((plain.returncode, bad.returncode), (0, 0), (plain.stderr, bad.stderr))
        self.assertEqual(json.loads(plain.stdout)["route_id"], json.loads(bad.stdout)["route_id"])
        self.assertNotIn("경로 계획을 읽지 못함", plain.stderr)
        self.assertEqual(bad.stderr.count("경로 계획을 읽지 못함"), 1)

    def test_fleet_plan_is_the_legs_capability_order_and_not_sealed(self):
        ns = mock.Mock(plan=None, campaign_key="k", parent_cycle=None)
        plan, source = R._resolve_compose_plan(ns, self.binding(0))
        self.assertEqual((plan, source), (["code"], "explicit"))
        route = self.leg(shape="direct", route_plan=self.binding(0))
        self.assertNotIn("plan", route)


class NextLegTest(PlanFixture):
    def test_the_projection_names_the_next_leg_with_its_parent_cycle_and_campaign(self):
        route = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        finished = "cyc_" + "b" * 32
        leg = RP.project_next_leg(route, finished)
        self.assertEqual(set(leg), {"index", "leg", "compose_command"})
        self.assertEqual((leg["index"], leg["leg"]["shape"]), (1, "staged"))
        argv = shlex.split(leg["compose_command"])
        self.assertEqual(argv[argv.index("--route-plan") + 1], f"{self.decision_path}#1")
        self.assertEqual(argv[argv.index("--parent-cycle") + 1], finished)
        self.assertEqual(argv[argv.index("--campaign-key") + 1], "framed-key")
        self.assertEqual(argv[argv.index("--graph") + 1], "execute,test,report")
        self.assertEqual(argv[argv.index("--shape") + 1], "staged")
        # Run as printed, the command seals and starts the leg.
        self.assertEqual(argv[argv.index("compose") + 1], "--start")
        self.assertTrue(Path(argv[argv.index("--prompt-file") + 1]).is_file())

    def test_a_leg_without_cwd_prints_the_previous_legs_worktree(self):
        route = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        previous_cwd = self.root.parent / "prepared-worktree"
        leg = RP.project_next_leg({**route, "cwd": str(previous_cwd)}, "cyc_" + "b" * 32)
        argv = shlex.split(leg["compose_command"])
        self.assertEqual(argv[argv.index("--cwd") + 1], str(previous_cwd))

    def test_the_last_leg_an_unreadable_record_and_a_lost_prompt_have_no_key(self):
        route = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        last = {**route, "route_plan": {**route["route_plan"], "index": 1}}
        self.assertIsNone(RP.project_next_leg(last, "cyc_" + "b" * 32))
        self.assertIsNone(RP.project_next_leg({**route, "route_plan": {**route["route_plan"], "digest": "sha256:" + "0" * 64}},
                                              "cyc_" + "b" * 32))
        self.assertIsNone(RP.project_next_leg({k: v for k, v in route.items() if k != "route_plan"}, "cyc_" + "b" * 32))
        self.assertIsNone(RP.project_next_leg(route, ""))
        prompt = Path(self.record_data["decision"]["first_leg_compose"]["context"]["prompt_file"])
        prompt.rename(prompt.with_suffix(".gone"))
        self.assertIsNone(RP.project_next_leg(route, "cyc_" + "b" * 32))


class SecondLegCommandTest(S.OwnCheckout, PlanFixture):
    # A direct second leg needs no readiness probe, so the real CLI compiles the printed command.
    # It is a source start: its child process prepares in the frame's own checkout.
    LEGS = [DIRECT, {"capability": "autopilot-code", "shape": "direct", "why": "second"}]

    def test_route_plan_cli_without_cwd_uses_the_legs_folder_relative_to_the_original_frame(self):
        record = self.record()
        context = record["decision"]["first_leg_compose"]["context"]
        target = self.root.parent
        record["decision"]["proposal"]["legs"][1]["cwd"] = os.path.relpath(target, context["cwd"])
        record["digest"] = RP.decision_digest(record["decision"])
        self.decision_path.write_bytes(RP.render(record))
        command = [sys.executable, str(HERE / "capability-route.py"), "compose", "--start", "--shape", "direct",
                   "--slug", "chosen-cwd", "--capability", "autopilot-code", "--artifact-root", str(self.root),
                   "--campaign-key", "framed-key", "--spec-read", "fixture", "--prompt-file", context["prompt_file"],
                   "--route-plan", f"{self.decision_path}#1"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_DISPATCH_")}
        done = subprocess.run(command, cwd=self.root, text=True, capture_output=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout)
        route = json.loads(Path(receipt["route_file"]).read_text(encoding="utf-8"))
        self.assertEqual(route["cwd"], str(target))
        self.assertEqual(route["route_plan"]["index"], 1)
        self.assertEqual(receipt["state"], "inline")

    def test_a164_7_the_printed_command_seals_the_next_route_with_index_plus_one_and_the_parent_cycle(self):
        route = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        parent = self.record()["decision"]["frame_route"]["cycle_id"]
        leg = RP.project_next_leg(route, parent)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_DISPATCH_")}
        done = subprocess.run(shlex.split(leg["compose_command"]), text=True, capture_output=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout)
        second = json.loads(Path(receipt["route_file"]).read_text(encoding="utf-8"))
        self.assertEqual(second["route_plan"], {**route["route_plan"], "index": 1})
        self.assertEqual((second["parent_cycle_id"], second["campaign_key"]), (parent, "framed-key"))
        self.assertEqual(second["selection"]["shape"], "direct")
        # Both legs run in the one worktree the first leg prepared from the frame's checkout.
        self.assertEqual(Path(route["cwd"]).parent, (self.primary.parent / "repo-wt").resolve())
        self.assertEqual(second["cwd"], route["cwd"])
        # Every leg of the same decision carries the frozen task and the start's execution scope.
        self.assertEqual(second["work_request"]["text"], route["work_request"]["text"])
        self.assertEqual(route["entry_execution_scope"], "complete")
        self.assertEqual((second["entry_execution_scope"], second["entry_scope_contract_version"]), ("complete", 1))
        self.assertNotIn("next_leg", json.dumps(receipt))
        # The printed command also started the leg: a direct leg answers with its inline task.
        self.assertEqual((receipt["state"], receipt["required_action"]), ("inline", "execute-inline"))


class PinnedPlanFixture(S.PinnedStartBase):
    """A framed route composed with pins, its decision record and its first leg, from the real flow."""

    LEGS = [DIRECT, {"capability": "autopilot-code", "shape": "direct", "why": "second"},
            {"capability": "autopilot-code", "shape": "direct", "why": "third"}]

    def setUp(self):
        super().setUp()
        self.set_briefs(self.LEGS, self.LEGS)
        self.set_interview({"legs": self.LEGS})
        self.first = self.settle()
        self.decision_path = self.record_path()
        self.record_data = self.record()
        self.parent = self.record_data["decision"]["frame_route"]["cycle_id"]
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_DISPATCH_")}

    def leg_route(self):
        return json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))

    def printed(self, route, cycle="cyc_" + "b" * 32):
        return shlex.split(RP.project_next_leg(route, cycle)["compose_command"])

    def run_compose(self, argv):
        # The printed command also starts the leg; these fixtures name a placeholder parent
        # cycle, so they run only the compose part and check what it seals.
        argv = [token for token in argv if token != "--start"]
        done = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(Path(json.loads(done.stdout)["route_file"]).read_text(encoding="utf-8"))

    @staticmethod
    def pin_values(argv):
        return [argv[i + 1] for i, token in enumerate(argv) if token == "--pin"]


class SelectionPinContinuationTest(PinnedPlanFixture):
    """The user's compose-time pins survive into every continuation leg (defect 1)."""

    FRAME_PINS = {"owner=claude", "worker=claude:sonnet@high", "frame=claude"}

    def test_next_leg_command_reads_the_current_leg_pin_changes(self):
        import route_authority as AUTH
        leg = self.leg_route()
        AUTH.record_pin_change(leg, target="owner", pin={"harness": "codex", "model": None, "effort": None},
                               by={"harness": "claude", "session_id": "parent"},
                               source="fixture", tuples=[], candidates=[])
        self.assertEqual(set(self.pin_values(self.printed(leg))),
                         {"owner=codex", "worker=claude:sonnet@high", "frame=claude"})

    def test_the_printed_command_carries_every_pin_the_leg_was_sealed_with(self):
        leg0 = self.leg_route()
        self.assertEqual(leg0["selection_pins"], self.route["selection_pins"])
        argv = self.printed(leg0)
        self.assertEqual(set(self.pin_values(argv)), self.FRAME_PINS)
        self.assertEqual(len(self.pin_values(argv)), 3)

    def test_the_printed_command_seals_the_same_pins_on_the_next_leg_and_names_the_owner(self):
        second = self.run_compose(self.printed(self.leg_route()))
        self.assertEqual(second["selection_pins"], self.route["selection_pins"])
        self.assertEqual(second["work_request"]["owner_harness"], "claude")
        self.assertEqual(second["route_plan"]["index"], 1)

    def test_a_command_without_pin_tokens_inherits_them_from_the_frame_the_plan_names(self):
        argv = self.printed(self.leg_route())
        bare = []
        skip = False
        for token in argv:
            if skip:
                skip = False
            elif token == "--pin":
                skip = True
            else:
                bare.append(token)
        self.assertEqual(self.pin_values(bare), [])
        second = self.run_compose(bare)
        self.assertEqual(second["selection_pins"], self.route["selection_pins"])
        self.assertEqual(second["work_request"]["owner_harness"], "claude")

    def test_a_pin_given_on_the_command_replaces_only_its_own_target(self):
        argv = [("worker=codex" if token == "worker=claude:sonnet@high" else token) for token in self.printed(self.leg_route())]
        second = self.run_compose(argv)
        pins = second["selection_pins"]
        self.assertEqual(pins["worker"], {"harness": "codex", "model": None, "effort": None})
        self.assertEqual(pins["owner"], self.route["selection_pins"]["owner"])
        self.assertEqual(pins["frame"], self.route["selection_pins"]["frame"])
        self.assertEqual(second["work_request"]["owner_harness"], "claude")

    def test_a_command_that_repeats_only_some_pins_still_gets_the_rest_from_the_frame(self):
        bare = shlex.split(" ".join(shlex.quote(t) for t in self.printed(self.leg_route())))
        kept = []
        index = 0
        while index < len(bare):
            if bare[index] == "--pin" and bare[index + 1] != "owner=claude":
                index += 2
                continue
            kept.append(bare[index])
            index += 1
        second = self.run_compose(kept)
        self.assertEqual(second["selection_pins"], self.route["selection_pins"])

    def test_an_owner_flag_that_contradicts_the_inherited_owner_replaces_it(self):
        argv = [t for t in self.printed(self.leg_route())]
        argv += ["--owner", "codex"]
        stripped, skip = [], False
        for token in argv:
            if skip:
                skip = False
            elif token == "--pin":
                skip = True
            else:
                stripped.append(token)
        second = self.run_compose(stripped)
        self.assertEqual(second["selection_pins"]["owner"]["harness"], "codex")
        self.assertEqual(second["selection_pins"]["worker"], self.route["selection_pins"]["worker"])

    def test_the_chain_keeps_the_pins_through_three_legs(self):
        second = self.run_compose(self.printed(self.leg_route()))
        third = self.run_compose(self.printed(second, "cyc_" + "c" * 32))
        self.assertEqual(third["selection_pins"], self.route["selection_pins"])
        self.assertEqual(third["route_plan"]["index"], 2)
        self.assertEqual(third["work_request"]["owner_harness"], "claude")
        self.assertIsNone(RP.project_next_leg(third, "cyc_" + "d" * 32))

    def test_a_leg_sealed_before_pins_were_inherited_projects_the_frames_pins_for_the_next_command(self):
        old = {k: v for k, v in self.leg_route().items() if k != "selection_pins"}
        self.assertEqual(set(self.pin_values(self.printed(old))), self.FRAME_PINS)

    def test_a_forged_or_changed_frame_route_contributes_no_pin(self):
        old = {k: v for k, v in self.leg_route().items() if k != "selection_pins"}
        frame_file = self.root / ".runtime" / "routes" / f"{self.route['route_id']}.json"
        frame = json.loads(frame_file.read_text(encoding="utf-8"))
        frame["selection_pins"]["worker"]["harness"] = "codex"
        frame_file.write_text(json.dumps(frame, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.pin_values(self.printed(old)), [])

    def test_the_pin_tokens_are_shell_safe_and_leave_out_what_the_pin_does_not_name(self):
        pins = {"owner": {"harness": "claude", "model": None, "effort": None},
                "worker": {"harness": "codex", "model": "gpt-5.1/x:y", "effort": "high"},
                "frame": {"harness": "claude", "model": "opus", "effort": None}}
        tokens = RP.pin_tokens(pins)
        self.assertEqual(tokens, ["owner=claude", "frame=claude:opus", "worker=codex:gpt-5.1/x:y@high"])
        self.assertEqual(R._parse_selection_pins(shlex.split(shlex.join(tokens))), pins)
        self.assertEqual(RP.pin_tokens(None), [])
        self.assertEqual(RP.pin_tokens({}), [])


class SelectionPinFilteredTopModelTest(PinnedPlanFixture):
    PINS = ["worker=claude:opus@high", "frame=claude:opus@high"]

    def setUp(self):
        patch = mock.patch.object(R, "_main_session_only_models", return_value="opus")
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def test_a_model_the_compose_filter_dropped_is_not_revived_by_the_next_leg(self):
        self.assertEqual(self.route["selection_pins"]["worker"], {"harness": "claude", "model": None, "effort": None})
        self.assertEqual(self.route["selection_pins"]["frame"]["model"], "opus")
        argv = self.printed(self.leg_route())
        self.assertEqual(set(self.pin_values(argv)), {"worker=claude", "frame=claude:opus@high"})
        second = self.run_compose(argv)
        self.assertEqual(second["selection_pins"], self.route["selection_pins"])


class UnpinnedContinuationTest(S.OwnCheckout, PlanFixture):
    LEGS = [DIRECT, {"capability": "autopilot-code", "shape": "direct", "why": "second"}]

    def test_a_route_with_no_pin_prints_none_and_seals_none(self):
        route = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        self.assertNotIn("selection_pins", route)
        argv = shlex.split(RP.project_next_leg(route, self.record_data["decision"]["frame_route"]["cycle_id"])["compose_command"])
        self.assertNotIn("--pin", argv)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_DISPATCH_")}
        done = subprocess.run(argv, text=True, capture_output=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        second = json.loads(Path(json.loads(done.stdout)["route_file"]).read_text(encoding="utf-8"))
        self.assertNotIn("selection_pins", second)


class PinnedFourReceiptSurfacesTest(PinnedPlanFixture):
    """Every place a finished leg reports its next leg prints the same command, pins included."""

    def test_start_resume_and_the_route_plan_reader_print_the_same_pinned_command(self):
        leg_path = self.leg_routes()[0]
        route = json.loads(leg_path.read_text(encoding="utf-8"))
        self.finish_leg(route, leg_path)
        read = RP.next_leg_for_route(route)
        self.assertIsNotNone(read)
        self.assertEqual(set(self.pin_values(shlex.split(read["compose_command"]))), SelectionPinContinuationTest.FRAME_PINS)
        # The resume starts that next leg itself (plan cursor), sealed with the same pins the command prints.
        resumed = S.W.start_work(route, leg_path, self.jobs)
        self.assertEqual((resumed["state"], resumed["plan_advanced"]["leg"]), ("inline", read["index"]), resumed)
        started = json.loads(Path(resumed["plan_advanced"]["route_file"]).read_text(encoding="utf-8"))
        self.assertEqual(set(RP.pin_tokens(started["selection_pins"])), SelectionPinContinuationTest.FRAME_PINS)

    def test_the_other_receipt_surfaces_read_through_the_same_projection(self):
        for name in ("capability-route.py", "dispatch_completion_join.py", "inline_finish.py", "work_start.py"):
            text = (HERE / name).read_text(encoding="utf-8")
            self.assertRegex(text, r"next_leg_for_route\(", name)
            self.assertNotIn("compose_argv(", text, name)


class NonGitRoutePlanScopeTest(unittest.TestCase):
    def test_mutating_first_leg_is_refused_before_worktree_preparation(self):
        with tempfile.TemporaryDirectory(prefix="route-plan-non-git-") as temp:
            cwd = Path(temp).resolve()
            leg = {"shape": "solo"}
            selection = {"nodes": [{"write_scope": ["source/**"]}]}
            kwargs = {"cwd": str(cwd), "selection_pins": None, "owner": "claude"}
            with mock.patch.object(RP, "leg_arguments", return_value={}), \
                 mock.patch.object(RP, "leg_cwd", return_value=str(cwd)), \
                 mock.patch.object(R, "_leg_compose_kwargs", return_value=kwargs), \
                 mock.patch.object(R, "compose_route", return_value=selection), \
                 mock.patch.object(R, "prepare_isolated_worktree") as prepare:
                with self.assertRaisesRegex(ValueError, "git 저장소 아님: source-mutating worker route"):
                    R.compile_first_leg(
                        leg, frame_route={"cwd": str(cwd), "artifact_root": str(cwd / ".agent_reports")},
                        frame_cycle_id="cyc_fixture", context={"slug": "fixture", "cwd": str(cwd),
                                                               "owner": "claude"},
                        binding={}, work_request=None, readiness=lambda _cwd: {},
                    )
            prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
