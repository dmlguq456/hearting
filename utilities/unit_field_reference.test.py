#!/usr/bin/env python3
"""A common unit's reference follows the route's field (R31, routing fragment 9 follow-up).

The worker text of a unit whose `## 참고` keys sections by field (`### 참고 — <field>`) carries the
field's fundamentals from `roles/fields/<field>.md`, the common reference and that field's sections
only. An unknown field (None) renders the body as written, as before; `other` carries the common
reference. Every other unit renders as written. The three
harnesses share the one render, in a stage's bootstrap and in a unit route's first instruction.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import argparse
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import worker_bootstrap as W  # noqa: E402

FIELDS = (None, "plan", "design", "code", "lab", "research", "docs", "admin", "other")
FIELD_UNITS = ("qa/code-review", "plan/plan-author", "analysis/analyze", "editorial/write")


def _raw(unit):
    return W._FRONTMATTER_RE.sub("", (ROOT / "roles" / "units" / f"{unit}.md").read_text(encoding="utf-8"),
                                 count=1).strip()


def _must_layer(unit):
    body = _raw(unit)
    return body[body.index("## 지킬 것"):body.index("\n## 참고")].strip()


def _catalog():
    return sorted(f"{p.parent.name}/{p.stem}" for p in (ROOT / "roles" / "units").glob("*/*.md")
                  if not p.name.startswith("_") and not p.parent.name.startswith("_"))


class FieldReferenceSelectionTest(unittest.TestCase):
    def test_only_units_with_field_sections_change(self):
        keyed = []
        for unit in _catalog():
            if re.search(r"^### 참고 — ", _raw(unit), re.MULTILINE):
                keyed.append(unit)
                continue
            for field in FIELDS:
                self.assertEqual(W.unit_persona_body(ROOT, unit, field), _raw(unit), (unit, field))
        self.assertEqual(sorted(keyed), sorted(FIELD_UNITS))

    def test_the_field_gets_its_fundamentals_and_its_sections_only(self):
        text = W.unit_persona_body(ROOT, "qa/code-review", "code")
        self.assertIn("## 갈래의 기본 — code", text)
        for line in ("- A change that makes things better is approved.", "- Change in small pieces.",
                     "- Tests come with the change.", "- A review after an incident does not blame."):
            self.assertEqual(text.count(line), 1, line)
        self.assertNotIn("[1–4]", text)                     # source numbers stay on the page
        self.assertNotIn("even if it is not perfect", text)  # and so do the glosses
        self.assertIn("### 참고 — code", text)
        self.assertIn("PyTorch: missing `.detach()`", text)
        # Order: the must layer, the fundamentals, then the reference with the common part before the field's.
        self.assertLess(text.index("### Limits"), text.index("## 갈래의 기본 — code"))
        self.assertLess(text.index("## 갈래의 기본 — code"), text.index("## 참고"))
        self.assertLess(text.index("**Scope and cost**"), text.index("### 참고 — code"))

    def test_a_field_with_no_section_of_its_own_gets_the_common_reference_and_its_fundamentals(self):
        docs = W.unit_persona_body(ROOT, "qa/code-review", "docs")
        self.assertIn("## 갈래의 기본 — docs", docs)
        self.assertIn("- The reader comes first.", docs)
        self.assertNotIn("### 참고 — code", docs)
        self.assertNotIn("PyTorch", docs)
        self.assertIn("**Scope and cost**", docs)
        lab = W.unit_persona_body(ROOT, "plan/plan-author", "lab")
        self.assertIn("- Set a baseline and change one thing at a time.", lab)
        self.assertNotIn("code-plan/code-refine stages", lab)
        self.assertIn("You are a technical planning specialist.", lab)
        self.assertIn("Dispatched as `autopilot-lab:eval-spec`", lab)    # the must line about lab stays
        code = W.unit_persona_body(ROOT, "plan/plan-author", "code")
        self.assertIn("This unit\nauthors the plan artifact inside the code-plan/code-refine stages", code)

    def test_an_unknown_field_renders_the_body_as_written(self):
        for unit in FIELD_UNITS:
            self.assertEqual(W.unit_persona_body(ROOT, unit, None), _raw(unit), unit)
        text = W.unit_persona_body(ROOT, "qa/code-review", None)
        self.assertNotIn("갈래의 기본", text)
        self.assertEqual(text.count("### 참고 — code, lab"), 1)       # one section for both fields, as written
        self.assertEqual(text.count("**Framework-specific pitfalls**"), 1)

    def test_other_or_a_field_with_no_section_carries_the_common_reference_only(self):
        common = W.unit_persona_body(ROOT, "qa/code-review", "other")
        self.assertNotIn("갈래의 기본", common)
        self.assertNotIn("### 참고 — ", common)
        self.assertIn("**Project convention adherence**", common)
        for field in ("nope", "Bad Value", 3):
            self.assertEqual(W.unit_persona_body(ROOT, "qa/code-review", field), common, field)

    def test_a_missing_page_drops_only_the_fundamentals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "roles" / "units" / "qa").mkdir(parents=True)
            shutil.copy(ROOT / "roles" / "units" / "qa" / "code-review.md", root / "roles" / "units" / "qa")
            # no roles/fields at all: the code section stays, no fundamentals, nothing raised
            no_page = W.unit_persona_body(root, "qa/code-review", "code")
            self.assertIn("### 참고 — code", no_page)
            self.assertNotIn("갈래의 기본", no_page)
            (root / "roles" / "fields").mkdir()
            (root / "roles" / "fields" / "code.md").write_text("# code\n\n## Not decided here\n\n- x\n",
                                                                encoding="utf-8")
            no_fundamentals = W.unit_persona_body(root, "qa/code-review", "code")
            self.assertNotIn("갈래의 기본", no_fundamentals)
            self.assertIn("### 참고 — code", no_fundamentals)

    def test_a_lab_review_keeps_the_five_code_criteria_once(self):
        lab = W.unit_persona_body(ROOT, "qa/code-review", "lab")
        self.assertIn("### 참고 — code, lab", lab)
        for name in ("Bug potential", "Performance", "Code quality", "Maintainability", "Framework-specific pitfalls"):
            self.assertEqual(lab.count(f"**{name}**"), 1, name)
        self.assertEqual(W.unit_persona_body(ROOT, "qa/code-review", "code").count("**Framework-specific pitfalls**"), 1)

    def test_a_dispatch_line_stays_common_for_plan_author(self):
        for field in ("code", "lab", "docs", "other"):
            text = W.unit_persona_body(ROOT, "plan/plan-author", field)
            self.assertIn("It is dispatched, never user-invoked directly.", text, field)
        self.assertNotIn("code-plan/code-refine stages", W.unit_persona_body(ROOT, "plan/plan-author", "lab"))

    def test_a_wrapped_fundamental_is_joined_before_it_is_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "roles" / "fields").mkdir(parents=True)
            (root / "roles" / "fields" / "code.md").write_text(
                "# code\n\n## Fundamentals\n\n- A change that makes things\n  better is approved. — gloss\n  more gloss [1–4]\n"
                "- Second one [2]\n- Third — gloss [3]\n\n## Sources\n\n  1. not an item\n", encoding="utf-8")
            self.assertEqual(W.field_fundamentals(root, "code"),
                             ["A change that makes things better is approved.", "Second one", "Third"])

    def test_a_section_may_name_several_fields_and_is_carried_once(self):
        body = ("## 지킬 것\n\nmust\n\n## 참고\n\ncommon\n\n### 참고 — code, lab\n\nshared\n\n"
                "### 참고 — docs\n\ndocs only\n\n### Style\n\ncommon again\n")
        for field, shared, docs in (("code", True, False), ("lab", True, False), ("docs", False, True),
                                    ("design", False, False)):
            text = W.unit_field_reference(ROOT, body, field)
            self.assertEqual(("shared" in text, "docs only" in text), (shared, docs), field)
            self.assertIn("common again", text)
        self.assertEqual(W.unit_field_reference(ROOT, body, None), body)

    def test_a_tilde_fence_hides_a_section_heading(self):
        body = "## 지킬 것\n\nmust\n\n## 참고\n\ncommon\n\n~~~\n### 참고 — code\nfenced\n~~~\n\n### 참고 — docs\n\ndocs only\n"
        text = W.unit_field_reference(ROOT, body, "lab")
        self.assertIn("fenced", text)
        self.assertNotIn("docs only", text)

    def test_every_must_line_and_the_stance_stay_for_every_field(self):
        stance = W.unit_stance_text(ROOT, "qa/code-review")
        for unit in FIELD_UNITS:
            must = _must_layer(unit)
            for field in FIELDS:
                text = W.unit_persona_body(ROOT, unit, field)
                self.assertIn(must, text, (unit, field))
                self.assertEqual(text.count("## 지킬 것"), 1)
                self.assertEqual(text.count("## 참고\n"), 1)
                lines = [line for line in text.splitlines() if line.strip().startswith("- ")]
                self.assertEqual(len(lines), len(set(lines)), (unit, field))   # no line carried twice
        for field in FIELDS:
            boot = W.render_worker_bootstrap(ROOT, "review", unit="qa/code-review", field=field)
            self.assertEqual(boot.count(stance), 1)
            self.assertIn("Review stance: the shared stance text carried just above this body.",
                          boot[boot.index("## 지킬 것"):boot.index("## 참고\n")])
            self.assertIn(W.worker_terminal_contract(ROOT).splitlines()[0], boot)

    def test_a_section_heading_inside_a_fence_is_text(self):
        body = ("## 지킬 것\n\nmust\n\n## 참고\n\ncommon\n\n```\n### 참고 — code\nfenced\n```\n\n"
                "### 참고 — docs\n\ndocs only\n")
        self.assertIn("fenced", W.unit_field_reference(ROOT, body, "lab"))
        self.assertNotIn("docs only", W.unit_field_reference(ROOT, body, "lab"))
        fenced_only = "## 지킬 것\n\nmust\n\n## 참고\n\n```\n### 참고 — code\n```\n"
        self.assertEqual(W.unit_field_reference(ROOT, fenced_only, "lab"), fenced_only)


class AnalysisUnitTest(unittest.TestCase):
    """Routing fragment 11: the analysis unit has one must layer and a reference line per field."""

    UNIT = "analysis/analyze"
    LINES = {"code": "look at the contributing factors, not at a person",
             "lab": "Split the sources of variation before reading a difference",
             "design": "rate each problem's severity",
             "docs": "The reader, the structure, and each claim with its evidence",
             "research": "Keep the evidence apart from the assumptions",
             "admin": "Read the requirements one by one"}

    def test_the_head_is_a_read_only_stage_with_no_default_field(self):
        head = (ROOT / "roles" / "units" / "analysis" / "analyze.md").read_text(encoding="utf-8").split("---")[1]
        for line in ("unit: analysis/analyze", "family: analysis", "worker_type: stage", "floor: high",
                     "read_only: true", "stance: none"):
            self.assertIn(f"\n{line}\n", head)
        self.assertNotIn("\nfield:", head)   # a common unit: the route's field picks the reference

    def test_each_field_carries_its_own_line_and_the_whole_must_layer(self):
        must = _must_layer(self.UNIT)
        for line in ("Do not change the target.", "Do not bring in new outside material",
                     "name it as work for a research stage", "No conclusion without evidence."):
            self.assertIn(line, must)
        for field in FIELDS:
            text = W.unit_persona_body(ROOT, self.UNIT, field)
            self.assertIn(must, text, field)
            for other, line in self.LINES.items():
                self.assertEqual(line in text, field in (None, other), (field, other))
            self.assertEqual("## 갈래의 기본" in text, field not in (None, "other"), field)
        lab = W.unit_persona_body(ROOT, self.UNIT, "lab")
        self.assertIn("follow the result diagnosis in `qa/ml-debug`", lab)
        self.assertIn("`autopilot-lab:diagnose` is the review that judges it", lab)
        self.assertIn("## 갈래의 기본 — lab", lab)
        self.assertEqual(W.unit_persona_body(ROOT, self.UNIT, None), _raw(self.UNIT))

    def test_the_rendered_text_stays_small(self):
        sizes = {field: len(W.unit_persona_body(ROOT, self.UNIT, field).encode("utf-8")) for field in FIELDS}
        self.assertLess(max(sizes.values()), 4500, sizes)
        self.assertLess(sizes["other"], sizes[None])   # one field's line, not six


class WriteUnitTest(unittest.TestCase):
    """Routing fragment 11, writing half: one must layer, and reference lines for docs and admin."""

    UNIT = "editorial/write"
    LINES = {"docs": "One point per paragraph; keep each claim next to the evidence it rests on.",
             "admin": "Answer the form's requirements one by one"}

    def test_the_head_is_a_writing_stage_with_no_default_field(self):
        head = (ROOT / "roles" / "units" / "editorial" / "write.md").read_text(encoding="utf-8").split("---")[1]
        for line in ("unit: editorial/write", "family: editorial", "role: deep editor", "worker_type: stage",
                     "read_only: false", "stance: none", "  verdict: [done, blocked]"):
            self.assertIn(f"\n{line}\n", head)
        self.assertNotIn("\nfield:", head)      # docs and admin both use it: the route's field picks
        self.assertNotIn("\nbootstrap:", head)  # the work-style slot is fragment 12's, not made here

    def test_the_must_layer_keeps_every_line_the_design_names(self):
        must = _must_layer(self.UNIT)
        for line in ("**Input.** The settled outline and the evidence it rests on",
                     "the receiving side's form and\n  deadline", "also the apply list",
                     "**Output.** The draft, or the revised text,", "Outside the body",
                     "a list of the places written or changed",
                     "**Change nothing outside the settled outline and the apply list.**",
                     "**Invent no fact the evidence does not hold**",
                     "**A form keeps the receiving side's layout, fields, limits and deadline exactly as given.**",
                     "**Submitting and sending are the person's.**",
                     "Write nothing into mail, messengers or any\n  other outside communication tool",
                     "submit\n  nothing through a portal or service"):
            self.assertIn(line, must)
        # An entry the brief skips is listed, not a stop: the apply stage skips unmatched and STRUCT mutations.
        self.assertIn("listed, never guessed): end `verdict: PASS`", must)
        self.assertIn("`blocked`\n  when there is nothing to write from", must)
        self.assertIn("or a precondition the contract names stops the work", must)

    def test_each_field_carries_its_own_lines_and_the_whole_must_layer(self):
        must = _must_layer(self.UNIT)
        for field in FIELDS:
            text = W.unit_persona_body(ROOT, self.UNIT, field)
            self.assertIn(must, text, field)
            self.assertIn("This unit drafts and applies.", text)   # the boundary with polish, review, report
            for other, line in self.LINES.items():
                self.assertEqual(line in text, field in (None, other), (field, other))
        self.assertIn("## 갈래의 기본 — admin", W.unit_persona_body(ROOT, self.UNIT, "admin"))
        self.assertEqual(W.unit_persona_body(ROOT, self.UNIT, None), _raw(self.UNIT))

    def test_the_common_guidance_preloads_writing_profiles(self):
        for field in ("docs", "admin"):
            with self.subTest(field=field):
                text = W.unit_persona_body(ROOT, self.UNIT, field)
                self.assertIn("roles/units/_shared/profile-preload.md", text)
                self.assertIn("02_paper_writing_style", text)
                self.assertIn("04_analysis_methodology", text)
                self.assertIn("wording prevail", text)   # the outline and apply list come before a profile

    def test_the_units_own_text_stays_small(self):
        # The unit's own text is its body as written, every field's section included. A field's render
        # keeps fewer sections and adds `roles/fields/<field>.md`'s fundamentals (plan's run to 1 KB);
        # that page owns them, so they are not weighed against this unit.
        own = len(_raw(self.UNIT).encode("utf-8"))
        self.assertLess(own, 4000, own)


class RouteFieldTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def route(self, value, name="route.json"):
        path = self.tmp / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return str(path)

    def field(self, route_file, unit="qa/code-review", binding=False):
        args = argparse.Namespace(unit=unit, route_file=None if binding else route_file,
                                  owner_route_binding=argparse.Namespace(route_file=route_file) if binding else None)
        return W.unit_route_field(args)

    def test_the_sealed_field_and_an_old_routes_recipe_field(self):
        self.assertEqual(self.field(self.route({"field": "docs", "capability": "autopilot-code"})), "docs")
        self.assertEqual(self.field(self.route({"field": "lab"}), binding=True), "lab")
        # sealed before fields existed: the recipe's default field, read only
        old = {"capability": "autopilot-apply", "capability_mode": "default"}
        path = self.route(old)
        self.assertEqual(self.field(path), "docs")
        self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), old)
        self.assertEqual(self.field(self.route({"capability": "autopilot-code", "capability_mode": "dev"})), "code")

    def test_unknown_is_none_and_never_raises(self):
        for value in ({"field": None, "capability": "autopilot-code", "capability_mode": "dev"},
                      {"capability": "unit"}, {"capability": "route-frame"}, {"field": "Bad Value"},
                      {"capability": "no-such-kind"}, "{not json", "[1, 2]"):
            self.assertIsNone(self.field(self.route(value)), value)
        self.assertIsNone(self.field(str(self.tmp / "missing.json")))
        self.assertIsNone(self.field(None))
        self.assertIsNone(self.field(self.route({"field": "docs"}), unit=None))   # an owner has no unit text


def _adapter(harness):
    spec = importlib.util.spec_from_file_location(
        f"{harness}_dispatch_headless_field", ROOT / "adapters" / harness / "bin" / "dispatch-headless.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ThreeHarnessRenderTest(unittest.TestCase):
    """Claude, Codex and OpenCode build the same unit text from the route's field."""

    HARNESSES = ("claude", "codex", "opencode")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.adapters = {harness: _adapter(harness) for harness in self.HARNESSES}

    def route(self, route):
        path = self.tmp / f"route-{len(list(self.tmp.glob('route-*.json')))}.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        return str(path)

    def stage_args(self, route_file, unit="qa/code-review", worker_type="review"):
        return argparse.Namespace(worker_type=worker_type, dispatch_depth=2, worker_role=None, route_node="impl-review",
                                  capability="autopilot-code", completion_gate=None, assigned_contract="code-review",
                                  unit=unit, route_file=route_file, owner_route_binding=None)

    def stage_text(self, harness, args):
        adapter = self.adapters[harness]
        return adapter.DISPATCH_MAIN.bind_worker_contract(adapter._ADAPTER, args, profile_type=None)

    def test_a_stage_bootstrap_is_one_render_across_the_harnesses(self):
        for route, field in (({"field": "docs"}, "docs"), ({"field": "code"}, "code"),
                             ({"capability": "autopilot-apply", "capability_mode": "default"}, "docs"),   # old route
                             ({"field": None}, None), ({"capability": "unit"}, None)):
            path = self.route(route)
            texts = {harness: self.stage_text(harness, self.stage_args(path)) for harness in self.HARNESSES}
            self.assertEqual(len(set(texts.values())), 1, route)
            self.assertEqual(texts["claude"], W.render_worker_bootstrap(ROOT, "review", unit="qa/code-review",
                                                                        field=field))
            if field is None:                      # an unknown field is the unit text as written
                self.assertIn("### 참고 — code, lab", texts["claude"])
                self.assertNotIn("갈래의 기본", texts["claude"])
        docs = self.stage_text("codex", self.stage_args(self.route({"field": "docs"})))
        self.assertIn("## 갈래의 기본 — docs", docs)
        self.assertNotIn("PyTorch", docs)

    def test_the_analysis_stage_is_one_render_across_the_harnesses(self):
        for field in ("code", "lab", "docs", None):
            path = self.route({"field": field})
            args = self.stage_args(path, unit="analysis/analyze", worker_type="stage")
            texts = {harness: self.stage_text(harness, args) for harness in self.HARNESSES}
            self.assertEqual(len(set(texts.values())), 1, field)
            self.assertEqual(texts["claude"], W.render_worker_bootstrap(ROOT, "stage", unit="analysis/analyze",
                                                                        field=field))
            self.assertIn(_must_layer("analysis/analyze"), texts["claude"])
            self.assertEqual("### 참고 — code" in texts["claude"], field in ("code", None), field)

    def test_the_write_stage_is_one_render_across_the_harnesses(self):
        # The draft and apply nodes are docs routes; an old route reads its recipe's field.
        for route, field in (({"field": "docs"}, "docs"), ({"field": "admin"}, "admin"),
                             ({"capability": "autopilot-draft", "capability_mode": "doc"}, "docs"),
                             ({"capability": "autopilot-apply", "capability_mode": "default"}, "docs")):
            path = self.route(route)
            args = self.stage_args(path, unit="editorial/write", worker_type="stage")
            texts = {harness: self.stage_text(harness, args) for harness in self.HARNESSES}
            self.assertEqual(len(set(texts.values())), 1, route)
            self.assertEqual(texts["claude"], W.render_worker_bootstrap(ROOT, "stage", unit="editorial/write",
                                                                        field=field))
            self.assertIn("**Submitting and sending are the person's.**", texts["claude"])
            self.assertEqual("### 참고 — admin" in texts["claude"], field == "admin", route)

    def test_an_unreadable_route_still_renders_the_unit_text_as_written(self):
        for path in (str(self.tmp / "missing.json"), self.route({"field": "docs"}).replace("route-", "gone-")):
            text = self.stage_text("opencode", self.stage_args(path))
            self.assertEqual(text, W.render_worker_bootstrap(ROOT, "review", unit="qa/code-review"))
            self.assertIn("Portable Worker Kernel", text)

    def test_a_unit_without_field_sections_renders_as_before(self):
        path = self.route({"field": "lab"})
        for unit, worker_type in (("qa/test", "review"), ("editorial/report", "stage"), ("dev/backend", "stage")):
            text = self.stage_text("claude", self.stage_args(path, unit=unit, worker_type=worker_type))
            self.assertEqual(text, W.render_worker_bootstrap(ROOT, worker_type, unit=unit))
            self.assertNotIn("갈래의 기본", text)

    def unit_leg(self, harness, field_route, unit="plan/plan-author", worker_type="stage"):
        adapter = self.adapters[harness]
        args = argparse.Namespace(
            capability="unit", capability_mode="default", worker_mode=unit, qa="quick", intensity="quick",
            dispatch_depth=1, worker_type=worker_type, attempt_id="att-field", assigned_contract="unit",
            route_node="unit", route_file=self.route(field_route), route_id="rt-field", owner_route_binding=None,
            unit=unit, worktree=str(self.tmp), artifact_root=str(self.tmp), parent_attempt_id=None,
            continues_attempt_id=None, completion_gate=None)
        with mock.patch.object(W, "resolve_node_scope", return_value={}), \
                mock.patch.object(W, "_child_scope_environment", return_value={}), \
                mock.patch.object(W, "node_scope_prompt", return_value="Node scope: fixed.\n"), \
                mock.patch.object(W, "_bootstrap_memory_block", return_value="WS-ONE keep plans short."), \
                mock.patch.object(adapter.DISPATCH_MAIN, "dispatch_metadata_prompt",
                                  return_value="Dispatch metadata:\n- unit: x\n"):
            return adapter.DISPATCH_MAIN.unit_leg_prompt(adapter._ADAPTER, args, "Write the eval spec.\n")

    def test_a_unit_routes_first_instruction_carries_the_same_unit_text(self):
        for field in ("lab", "code", None):
            prompts = {harness: self.unit_leg(harness, {"capability": "unit", "field": field})
                       for harness in self.HARNESSES}
            self.assertEqual(len(set(prompts.values())), 1, field)
            prompt = prompts["claude"]
            self.assertTrue(prompt.startswith(W.unit_persona_body(ROOT, "plan/plan-author", field)), field)
            self.assertEqual(prompt.count("WS-ONE keep plans short."), 1)     # the work style, once
            self.assertIn("after this turn's instruction and the project's conventions", prompt)
            self.assertLess(prompt.index("WS-ONE"), prompt.index("Assignment:\nWrite the eval spec."))
            self.assertIn("exactly these three newline-delimited", prompt)
        lab = self.unit_leg("codex", {"capability": "unit", "field": "lab"})
        self.assertIn("- Record every run.", lab)
        self.assertNotIn("code-plan/code-refine stages", lab)
        old = self.unit_leg("opencode", {"capability": "unit"})       # an old unit route, sealed without a field
        self.assertNotIn("갈래의 기본", old)
        self.assertIn("### 참고 — code", old)            # the body as written, as before the selection

    def review_leg(self, harness, field):
        adapter = self.adapters[harness]
        args = argparse.Namespace(
            capability="unit", capability_mode="default", worker_mode="qa/code-review", qa="quick", intensity="quick",
            dispatch_depth=1, worker_type="review", attempt_id="att-field", assigned_contract="unit",
            route_node="unit", route_file=self.route({"capability": "unit", "field": field}), route_id="rt-field",
            owner_route_binding=None, unit="qa/code-review", worktree=str(self.tmp), artifact_root=str(self.tmp),
            parent_attempt_id=None, continues_attempt_id=None, completion_gate=None)
        with mock.patch.object(W, "resolve_node_scope", return_value={}), \
                mock.patch.object(W, "_child_scope_environment", return_value={}), \
                mock.patch.object(W, "node_scope_prompt", return_value="Node scope: fixed.\n"), \
                mock.patch.object(W, "_bootstrap_memory_block", return_value="WS-ONE keep reviews short."), \
                mock.patch.object(adapter.DISPATCH_MAIN, "dispatch_metadata_prompt",
                                  return_value="Dispatch metadata:\n- unit: x\n"):
            return adapter.DISPATCH_MAIN.unit_leg_prompt(adapter._ADAPTER, args, "Review the diff.\n")

    def test_the_review_unit_a_unit_route_really_runs(self):
        # qa/code-review (a review worker, read-only) is the unit that runs as a unit route: stance first, once.
        stance = W.unit_stance_text(ROOT, "qa/code-review")
        for field in ("code", None):
            prompts = {harness: self.review_leg(harness, field) for harness in self.HARNESSES}
            self.assertEqual(len(set(prompts.values())), 1, field)
            prompt = prompts["claude"]
            self.assertTrue(prompt.startswith(stance), field)               # the stance leads the text
            self.assertEqual(prompt.count(stance), 1, field)                 # and is carried once
            self.assertIn(_must_layer("qa/code-review"), prompt)
            self.assertEqual(prompt.count("WS-ONE keep reviews short."), 1)
            self.assertIn("exactly these three newline-delimited", prompt)
            self.assertLess(len(prompt.encode("utf-8")), 15000)
        code = self.review_leg("claude", "code")
        self.assertIn("## 갈래의 기본 — code", code)
        self.assertIn("Hold these in view; they set no order, tool or depth.", code)
        for name in ("Bug potential", "Performance", "Code quality", "Maintainability", "Framework-specific pitfalls"):
            self.assertEqual(code.count(f"**{name}**"), 1, name)
        unknown = self.review_leg("claude", None)                           # unknown: the old text, as written
        self.assertNotIn("갈래의 기본", unknown)
        raw = W.unit_persona_body(ROOT, "qa/code-review", None)
        self.assertIn(raw, unknown)
        self.assertEqual(unknown.count("**Framework-specific pitfalls**"), 1)


if __name__ == "__main__":
    unittest.main()
