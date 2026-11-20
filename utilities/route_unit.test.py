#!/usr/bin/env python3
"""The `unit` route (design 2a): compose one read-only unit as a sealed route with no owner.

Real compose, real registry, real unit heads. The route is planned and sealed here; its launch and end
are `unit_route_launch.test.py`'s. The four compose cases (unit alone, unit with a field, a
unit with no default field and no `--field`, a unit that writes), the recipe's shape check, the
verifier's one extra branch and the `stages` view.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import importlib.util
import json
import pty
import re
import subprocess
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


F = _load("framed_route_for_unit", "framed_route.test.py")
T, R = F.T, F.R
import capability_topology as CT  # noqa: E402
from route_plan import is_depth0_leg  # noqa: E402

UNITS = HERE.parent / "roles" / "units"
# The units whose head names a default field (design 5): only where the field is clear.
FIELD_HEADS = {
    "dev/backend": "code", "dev/frontend": "code", "dev/new-lib": "code", "dev/refactor": "code",
    "qa/code-review": "code", "qa/security-review": "code", "qa/ml-debug": "lab",
    "design/critic": "design", "design/maker": "design", "design/verifier": "design",
    "editorial/polish": "docs", "editorial/review": "docs", "editorial/translate": "docs",
    "research/research-survey": "research",
    "general/explore": "other",     # a lookup has no field of its own: the caller's work does (`--field` changes it)
}


def head_field(unit):
    text = (UNITS / f"{unit}.md").read_text(encoding="utf-8").split("\n---\n")[0]
    found = re.search(r"^field:\s*(\S+)", text, re.MULTILINE)
    return found.group(1) if found else None


class UnitHeadTest(unittest.TestCase):
    def test_only_the_clear_units_name_a_default_field(self):
        written = {path.relative_to(UNITS).with_suffix("").as_posix(): head_field(path.relative_to(UNITS).with_suffix("").as_posix())
                   for path in UNITS.glob("*/*.md") if not path.name.startswith("_")}
        self.assertEqual({unit: field for unit, field in written.items() if field}, FIELD_HEADS)
        for unit in ("plan/frame", "plan/plan-author", "editorial/report", "qa/test", "qa/plan-review",
                     "qa/test-gap-check", "material/data-script", "ops/session-tidy-memory"):
            self.assertIsNone(written[unit], unit)


class UnitRecipeShapeTest(unittest.TestCase):
    def registry(self):
        return copy.deepcopy(CT.load_registry())

    def recipe(self, registry):
        return next(r for r in registry["recipes"] if r["capability"] == "unit")

    def test_the_shipped_registry_validates_with_one_unit_leg(self):
        registry = self.registry()
        CT.validate_registry(registry)
        node, = self.recipe(registry)["standard_plus"]["nodes"]
        self.assertTrue(is_depth0_leg(node))
        self.assertEqual((node["id"], node["write_scope"]), ("unit", ["units/unit/**"]))
        self.assertNotIn("unit", node)
        self.assertNotIn("owner", {n.get("worker_type") for n in self.recipe(registry)["standard_plus"]["nodes"]})

    def test_a_drifted_unit_recipe_is_refused(self):
        cases = (
            ("an owner node added", lambda r: r["standard_plus"]["nodes"].append(
                {**r["standard_plus"]["nodes"][0], "id": "owner"}), "nodes must be exactly"),
            ("not a depth-0 leg", lambda r: r["standard_plus"]["nodes"][0].pop("launch_authority"), "unit leg shape"),
            ("a fixed unit in the registry", lambda r: r["standard_plus"]["nodes"][0].update(unit="qa/code-review"),
             "come from the unit head"),
            ("a source scope", lambda r: r["standard_plus"]["nodes"][0].update(write_scope=["source/**"]), "unit leg shape"),
            ("a human gate", lambda r: r.update(human_gates=["frame-review"]), "no human gate"),
            ("a field of its own", lambda r: r.update(field="code"), "declares none"),
        )
        for label, mutate, message in cases:
            registry = self.registry()
            mutate(self.recipe(registry))
            with self.subTest(label), self.assertRaisesRegex(CT.TopologyError, message):
                CT.validate_registry(registry)


class UnitComposeTest(F.FramedBase):
    def unit(self, unit, **kw):
        arguments = dict(capability=None, capability_mode=None, shape=None, graph=None, slug="unit-fixture",
                         cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="unit-key",
                         unit=unit, registered_headless_evidence=T.registered_headless())
        arguments.update(kw)
        return R.compose_route(**arguments)

    def sealed(self, route):
        """What a reader of the route file sees: the verifier accepts it."""
        return R.verify_route(json.loads(json.dumps(route)), R.ROOT)

    def test_a_unit_alone_seals_one_depth_0_leg_and_its_default_field(self):
        route = self.unit("qa/code-review")
        self.assertEqual((route["capability"], route["capability_mode"]), ("unit", "default"))
        self.assertEqual((route["requested_intensity"], route["effective_intensity"]), ("quick", "quick"))
        self.assertEqual((route["field"], route["field_source"]), ("code", "preset"))
        self.assertEqual(route["selection"]["shape"], "solo")
        node, = route["nodes"]
        self.assertEqual((node["id"], node["unit"], node["worker_type"], node["dispatch_depth"],
                          node["launch_authority"], node["kind"]),
                         ("unit", "qa/code-review", "review", 1, "depth-0", "map-worker"))
        self.assertTrue(is_depth0_leg(node))
        self.assertEqual(node["write_scope"], ["units/unit/**"])
        self.assertEqual(node["model_profile"], "light")             # the grade a staged route gives qa/code-review
        self.assertNotIn("owner", [n.get("worker_type") for n in route["nodes"]])
        self.assertEqual(route["workflow_contract"]["terminal_nodes"], ["unit"])
        self.sealed(route)

    def test_a_unit_with_a_field_records_it_as_explicit(self):
        route = self.unit("qa/test", field="code")
        self.assertEqual((route["field"], route["field_source"]), ("code", "explicit"))
        self.assertEqual(route["nodes"][0]["model_profile"], "light")  # floor: near-zero
        self.sealed(route)
        # A named field wins over the head's own default.
        route = self.unit("qa/ml-debug", field="research")
        self.assertEqual((route["field"], route["field_source"]), ("research", "explicit"))
        self.assertEqual(route["nodes"][0]["model_profile"], "balanced-deep")  # floor: high
        self.sealed(route)

    def test_general_explore_opens_light_in_other_and_needs_no_field(self):
        # G1 (11-20): the lookup unit is read-only, light by its `low` floor, and its field is `other` unless `--field` says
        # otherwise; `--field` is not a required input for it, and `--profile` raises the depth. No intensity is recorded
        # beyond the unit form's own compatibility value.
        route = self.unit("general/explore")
        self.assertEqual((route["capability"], route["field"], route["field_source"]), ("unit", "other", "preset"))
        node, = route["nodes"]
        self.assertEqual((node["unit"], node["role"], node["worker_type"], node["kind"], node["dispatch_depth"],
                          node["launch_authority"]),
                         ("general/explore", "fast tool worker", "support", "map-worker", 1, "depth-0"))
        self.assertEqual(node["model_profile"], "light")
        self.assertEqual(node["write_scope"], ["units/unit/**"])     # its own report folder only: it is read-only
        self.assertNotIn("owner", [n.get("worker_type") for n in route["nodes"]])
        self.sealed(route)
        named = self.unit("general/explore", field="research")
        self.assertEqual((named["field"], named["field_source"]), ("research", "explicit"))
        self.assertEqual(named["nodes"][0]["model_profile"], "light")
        self.sealed(named)
        for profile in ("balanced", "balanced-deep", "deep"):
            with self.subTest(profile=profile):
                deeper = self.unit("general/explore", profile=profile)
                self.assertEqual(deeper["nodes"][0]["model_profile"], profile)
                self.assertEqual(deeper["field"], "other")
                self.sealed(deeper)
        with self.assertRaises(ValueError):
            self.unit("general/explore", profile="top")              # `top` is not available to a unit leg

    def test_general_explore_head_is_read_only_and_ends_all_three_ways_as_a_finished_job(self):
        head = CT._unit_frontmatter("general/explore")
        self.assertEqual((head["role"], head["worker_type"], head["floor"], head["field"], head["read_only"]),
                         ("fast tool worker", "support", "low", "other", "true"))
        self.assertEqual(head["verdict"], ["answered", "partial", "not-found"])
        text = (UNITS / "general" / "explore.md").read_text(encoding="utf-8")
        self.assertIn("## 지킬 것", text)
        self.assertIn("## 참고", text)
        must, advice = text.split("## 지킬 것")[1].split("## 참고")
        flat = " ".join(must.split())
        for word in ("answered", "partial", "not-found", "verdict: PASS", "BLOCKED", "Read-only", "No further workers"):
            self.assertIn(word, flat, word)
        # The answer reaches the caller only through the node's report, so the text says to write it there and that
        # it is the one file written (not "write nothing"); the adapters' mode-map requirement says the same.
        for phrase in ("Write it to this node's one declared report", "never end `artifact: -`",
                       "That report is the only file you write"):
            self.assertIn(phrase, flat, phrase)
        self.assertNotIn("Write nothing", flat)
        for harness in ("codex", "opencode"):
            mode_map = (UNITS.parent.parent / "adapters" / harness / "bin" / "mode-map.sh").read_text(encoding="utf-8")
            self.assertIn("the node's one report is the only file written", mode_map)
            self.assertNotIn("write nothing", mode_map)
        self.assertIn("`tools/memory/recall.sh \"<query>\" --no-touch`", advice)
        self.assertIn("`mem show <id> --no-touch`", advice)
        # Nothing here says Hearting runs a session's subagent calls: that is a later piece.
        self.assertNotRegex(text.lower(), r"native subagent|hook")

    def test_the_three_harnesses_open_the_same_general_explore_leg(self):
        seen = {}
        for harness in ("claude", "codex", "opencode"):
            route = self.unit("general/explore", parent_harness=harness)
            node, = route["nodes"]
            seen[harness] = (route["field"], route["field_source"], node["unit"], node["worker_type"], node["kind"],
                             node["model_profile"], node["write_scope"], node["launch_authority"])
            self.sealed(route)
        self.assertEqual(len(set(map(repr, seen.values()))), 1, seen)

    def test_a_unit_without_a_default_field_names_what_is_missing(self):
        with self.assertRaises(ValueError) as refused:
            self.unit("qa/test")
        message = str(refused.exception)
        self.assertTrue(message.startswith("compose-field-required:--unit qa/test "), message)
        self.assertIn("--field <field>", message)
        self.assertIn("fields: plan,design,code", message)

    def test_a_unit_that_writes_is_refused_with_its_reason(self):
        for unit in ("dev/backend", "design/maker", "editorial/polish"):
            with self.subTest(unit=unit), self.assertRaisesRegex(
                    ValueError, rf"^compose-unit-writes:{unit} \(this unit writes; a unit route opens read-only"):
                self.unit(unit)

    def test_an_unknown_unit_is_refused_with_the_list(self):
        for unit in ("qa/no-such", "code-review", "_kernel/owner", "../x/y"):
            with self.subTest(unit=unit), self.assertRaisesRegex(ValueError, r"^compose-unit-unknown:.* \(units: .*qa/code-review"):
                self.unit(unit)

    def test_arguments_that_contradict_the_kind_are_refused_by_name(self):
        for kwargs, needle in (({"capability": "autopilot-code"}, "--capability autopilot-code"),
                               ({"shape": "staged"}, "--shape staged"),
                               ({"shape": "framed"}, "--shape framed"),
                               ({"intensity": "standard"}, "--intensity standard"),
                               ({"graph": "execute"}, "--graph"),
                               ({"signals": ["shared-contract"]}, "--signal")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, rf"^compose-unit-conflict:{needle}"):
                self.unit("qa/code-review", **kwargs)
        # What agrees with it is accepted: the kind itself, the solo shape, quick, an explicit profile.
        route = self.unit("qa/code-review", capability="unit", shape="solo", intensity="quick", profile="deep")
        self.assertEqual(route["nodes"][0]["model_profile"], "deep")
        self.sealed(route)

    def test_the_refusal_of_an_intensity_says_a_unit_route_has_no_intensity(self):
        # 11-20: "unit에는 강도를 표시안하는게 맞겠네". An intensity other than the old `quick` is refused in a text that
        # tells where checking is opened; `quick` (an old command) is accepted and ignored; another flag's text says no `quick`.
        why = ("a unit route has no intensity: one unit does one job and nothing checks it; to have the work checked open "
               "--shape solo or staged with --intensity, and to judge more deeply use --profile")
        for value in ("standard", "strong", "direct"):
            with self.subTest(intensity=value), self.assertRaises(ValueError) as refused:
                self.unit("qa/code-review", intensity=value)
            self.assertEqual(str(refused.exception), f"compose-unit-conflict:--intensity {value} ({why})")
        with self.assertRaises(ValueError) as refused:
            self.unit("qa/code-review", shape="staged")
        self.assertEqual(str(refused.exception), "compose-unit-conflict:--shape staged (--unit is the kind unit: one leg of that unit)")
        self.assertNotIn("quick", str(refused.exception))
        # The route an old command opens is the very route the plain `--unit` opens: the same record.
        plain, old = self.unit("qa/code-review"), self.unit("qa/code-review", intensity="quick")
        for key in ("capability", "requested_intensity", "effective_intensity", "nodes", "field", "workflow_contract"):
            self.assertEqual(plain[key], old[key], key)
        self.assertEqual((plain["requested_intensity"], plain["effective_intensity"]), ("quick", "quick"))

    def test_a_hand_built_unit_request_at_another_intensity_is_refused_without_the_word_quick(self):
        recipe_args = dict(cwd=R.ROOT, artifact_root=self.root, unit="qa/code-review", field="code", field_source="preset")
        for intensity in ("standard", "strong"):
            with self.subTest(intensity=intensity), self.assertRaises(ValueError) as refused:
                R.compile_route("unit", "default", intensity, **recipe_args)
            self.assertEqual(str(refused.exception), f"compose-unit-conflict:{intensity} (a unit route has no intensity)")

    def test_the_recorded_value_keeps_every_reader_working(self):
        # 11-20 (design 3.2, 3.3): the record keeps `quick`, named `UNIT_RECORDED_INTENSITY`, so an old record and a new one
        # read as the same unit route, and the readers of `effective_intensity` (the round cap, the continuation budget, the
        # worker's QA) are unchanged. Nothing but the choosable intensities left the table.
        import review_round_cap as ROUND_CAP
        import route_form as RF
        from dispatch_mode_contract import QA_FROM_INTENSITY
        self.assertEqual(RF.UNIT_RECORDED_INTENSITY, "quick")
        self.assertEqual(RF.FORM_INTENSITIES["unit"], (RF.UNIT_RECORDED_INTENSITY,))
        route = self.unit("qa/code-review")
        for record in (route, {**route, "selection": {k: v for k, v in route["selection"].items() if k != "shape"}}):
            self.assertEqual(RF.route_form(record), "unit")
            self.assertTrue(RF.unit_route(record))
            self.assertTrue(RF.serial_attempt_route(record))
            self.assertFalse(RF.one_shot_owner(record))
            self.assertTrue(RF.alone_without_stages(record))
        self.assertEqual(ROUND_CAP.max_review_rounds(route["effective_intensity"]), 1)
        self.assertIn(route["effective_intensity"], QA_FROM_INTENSITY)
        # A unit record is no unit route at any other value (nothing else is read as one).
        self.assertFalse(RF.unit_route({**route, "effective_intensity": "standard"}))
        self.assertEqual(RF.form_of("unit", "standard"), "staged")

    def test_the_receipt_has_no_standard_plus_answer_for_a_unit_route(self):
        # 11-20: "is it standard+" has no answer for a route with no intensity; the others answer as before.
        unit = {q["name"]: q["answer"] for q in R.compose_observations(self.unit("qa/code-review"))["pre_execution_answers"]}
        self.assertEqual(unit["standard+"], "해당 없음")
        solo = self.unit(None, capability="autopilot-code", shape="solo")
        answers = {q["name"]: q["answer"] for q in R.compose_observations(solo)["pre_execution_answers"]}
        self.assertEqual(answers["standard+"], "아니오")

    def test_top_is_not_available_to_a_unit_leg(self):
        with self.assertRaises(ValueError):
            R.compose_route(**{**dict(capability=None, capability_mode=None, shape=None, graph=None, slug="unit-fixture",
                                      cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="unit-key",
                                      unit="qa/code-review", registered_headless_evidence=T.registered_headless()),
                              "profile": "top"})

    def test_the_kind_is_not_reachable_without_a_unit(self):
        for shape in ("solo", "staged"):
            with self.subTest(shape=shape), self.assertRaisesRegex(ValueError, "^compose-capability-unknown:unit"):
                self.unit(None, capability="unit", shape=shape)

    def test_a_sealed_unit_route_that_is_tampered_is_refused(self):
        def reseal(route):
            route["route_hash"] = R.route_hash(route)
            route["route_id"] = R.ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
            return route
        for label, mutate in (
                ("a unit swapped", lambda r: r["nodes"][0].update(unit="research/research-survey")),
                ("the depth-0 mark removed", lambda r: r["nodes"][0].pop("launch_authority")),
                ("a second node", lambda r: r["nodes"].append({**r["nodes"][0], "id": "second"})),
                ("a wider scope", lambda r: r["nodes"][0].update(write_scope=["units/**"]))):
            route = self.unit("qa/code-review")
            mutate(route)
            with self.subTest(label), self.assertRaises(ValueError):
                self.sealed(reseal(route))

    def test_the_existing_routes_are_untouched_by_the_new_kind(self):
        for kwargs in (dict(capability="autopilot-code", shape="solo"), dict(capability="autopilot-code", shape="direct")):
            with self.subTest(kwargs=kwargs):
                route = self.unit(None, **kwargs)
                self.assertNotEqual(route["capability"], "unit")
                self.sealed(route)

    def test_the_card_shows_the_unit_route(self):
        card = R.compose_card(self.unit("qa/code-review"), include_decisions=False)
        self.assertIn("unit", card)
        self.assertIn("\n  field code (preset)\n", card)


class UnitCliTest(F.FramedBase):
    def run_cli(self, *extra, task="Review the change\n", language=None, stdin=None, stdin_bytes=None):
        """`compose` with the brief as a file, or (`stdin`/`stdin_bytes` given) read once from standard input."""
        brief = Path(self._tmp.name) / "brief.md"
        brief.write_text(task, encoding="utf-8")
        given = {} if stdin is None and stdin_bytes is None else {
            "input": stdin_bytes if stdin_bytes is not None else stdin.encode("utf-8")}
        text = not given
        evidence = Path(self._tmp.name) / "headless.json"
        evidence.write_text(json.dumps(T.registered_headless()), encoding="utf-8")
        env = {**F.clean_environment(), "AGENT_HOME": str(R.ROOT), "AGENT_DISPATCH_JOBS": str(self.jobs),
               "FLEET_ROUTE_CHAIN_DIR": str(Path(self._tmp.name) / "chains")}
        if language:
            env["FLEET_NOW_LANG"] = language
        done = subprocess.run(
            [sys.executable, str(HERE / "capability-route.py"), "compose", "--cwd", str(R.ROOT),
             "--artifact-root", str(self.root), "--spec-read", "fixture",
             "--prompt-file", "-" if given else str(brief),
             "--registered-headless-evidence", str(evidence), "--unassigned", *extra],
            text=text, capture_output=True, env=env, check=False, **given)
        if text:
            return done
        return subprocess.CompletedProcess(done.args, done.returncode, done.stdout.decode("utf-8", "replace"),
                                           done.stderr.decode("utf-8", "replace"))

    def test_the_card_names_the_unit_and_tells_no_intensity_while_the_record_keeps_it(self):
        # 11-20 (the person: "unit에는 강도를 표시안하는게 맞겠네"): the `[경로]` header of a unit route was `unit · solo(quick) unit` (shape, intensity
        # and the node id); it names the unit and its decision lines carry no `intensity=` line; the sealed route keeps `effective_intensity`.
        # Another form's header and its `intensity=` line are exactly what they were.
        for language, marker, word in (("en", "[Route]", "human gate"), ("ko", "[경로]", "사람 게이트")):
            with self.subTest(language=language):
                opened = self.run_cli("--unit", "qa/code-review", "--explain", language=language)
                self.assertEqual(opened.returncode, 0, opened.stderr)
                header = next(line for line in opened.stderr.splitlines() if line.startswith(marker))
                self.assertRegex(header, r"^\%s unit · qa/code-review · route rt-[0-9a-f]{16} · origin compose · %s " % (marker, word))
                self.assertNotIn("quick", header)
                self.assertNotIn("solo", header)
                self.assertNotIn("intensity=", opened.stderr)
                self.assertEqual(json.loads(opened.stdout.strip().splitlines()[-1])["effective_intensity"], "quick")
                for form, shape, shown in (("solo", "solo", "solo(quick) one-shot"), ("direct", "direct", "direct(direct) inline")):
                    other = self.run_cli("--capability", "autopilot-code", "--shape", shape, "--explain", language=language)
                    self.assertEqual(other.returncode, 0, other.stderr)
                    line = next(row for row in other.stderr.splitlines() if row.startswith(marker))
                    self.assertRegex(line, r"^\%s autopilot-code · %s · route rt-" % (marker, re.escape(shown)), form)
                    self.assertIn("intensity=%s (source=shape-default" % ("quick" if shape == "solo" else "direct"), other.stderr)

    def test_explain_opens_the_unit_alone_and_refuses_in_the_existing_form(self):
        opened = self.run_cli("--unit", "qa/code-review", "--explain")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        explained = json.loads(opened.stdout.strip().splitlines()[-1])
        self.assertEqual((explained["capability"], explained["shape"], explained["effective_intensity"]),
                         ("unit", "solo", "quick"))
        self.assertEqual([(n["id"], n["unit"], n["dispatch_depth"], n["terminal"]) for n in explained["nodes"]],
                         [("unit", "qa/code-review", 1, True)])
        self.assertIn("\n  field code (preset)\n", opened.stderr)
        for extra, needle in ((["--unit", "qa/test", "--explain"], "capability-route: compose-field-required:--unit qa/test"),
                              (["--unit", "dev/backend", "--explain"], "capability-route: compose-unit-writes:dev/backend"),
                              (["--unit", "qa/nope", "--explain"], "capability-route: compose-unit-unknown:qa/nope"),
                              (["--unit", "qa/code-review", "--shape", "staged", "--explain"],
                               "capability-route: compose-unit-conflict:--shape staged"),
                              (["--unit", "qa/code-review", "--capability", "autopilot-code", "--explain"],
                               "capability-route: compose-unit-conflict:--capability autopilot-code")):
            with self.subTest(extra=extra):
                refused = self.run_cli(*extra)
                self.assertEqual(refused.returncode, 64, refused.stderr)
                self.assertIn(needle, refused.stderr)
        for extra, shown in ((["--unit", "general/explore", "--explain"], "\n  field other (preset)\n"),
                             (["--unit", "general/explore", "--field", "research", "--explain"], "\n  field research (explicit)\n"),
                             (["--unit", "general/explore", "--profile", "deep", "--explain"], "\n  field other (preset)\n")):
            with self.subTest(extra=extra):
                lookup = self.run_cli(*extra)
                self.assertEqual(lookup.returncode, 0, lookup.stderr)
                self.assertIn(shown, lookup.stderr)
                self.assertEqual(json.loads(lookup.stdout.strip().splitlines()[-1])["nodes"][0]["unit"], "general/explore")
        named = self.run_cli("--unit", "qa/test", "--field", "code", "--explain")
        self.assertEqual(named.returncode, 0, named.stderr)
        self.assertIn("\n  field code (explicit)\n", named.stderr)

    def test_compose_without_start_writes_a_sealed_route_that_verifies(self):
        written = self.run_cli("--unit", "qa/code-review")
        self.assertEqual(written.returncode, 0, written.stderr)
        printed = json.loads(written.stdout.strip().splitlines()[-1])
        path = Path(printed["route_file"])
        route = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((route["capability"], route["field"], route["field_source"]), ("unit", "code", "preset"))
        self.assertEqual([(n["id"], n["unit"], n["launch_authority"]) for n in route["nodes"]],
                         [("unit", "qa/code-review", "depth-0")])
        verified = subprocess.run([sys.executable, str(HERE / "capability-route.py"), "verify", "--route", str(path)],
                                  text=True, capture_output=True, check=False,
                                  env={**F.clean_environment(), "AGENT_HOME": str(R.ROOT)})
        self.assertEqual(verified.returncode, 0, verified.stderr + verified.stdout)

    def test_start_needs_the_brief_like_every_start(self):
        # `--unit --start` is no longer refused by name: the leg launches, so it asks for what any start asks for.
        refused = self.run_cli("--unit", "qa/code-review", "--start", "--explain")
        self.assertEqual(refused.returncode, 64, refused.stderr)
        self.assertIn("capability-route: compose-start-requires-task:", refused.stderr)
        self.assertNotIn("compose-unit-start-unavailable", refused.stderr)

    def test_the_brief_on_standard_input_is_the_brief_a_file_would_be(self):
        # G2 (11-20): `--prompt-file -` reads standard input once; the route keeps that text exactly as it
        # keeps a file's, so every later reader (the leg's start, a repeat, a compose-again) finds it there.
        brief = "Where does the fixture say what it is for?\nAnswer with the file and line.\n"
        routes = {}
        for how, kwargs in (("file", {"task": brief}), ("stdin", {"stdin": brief})):
            done = self.run_cli("--unit", "general/explore", "--slug", f"g2-{how}", language="en", **kwargs)
            self.assertEqual(done.returncode, 0, done.stderr)
            routes[how] = json.loads(Path(json.loads(done.stdout.strip().splitlines()[-1])["route_file"])
                                     .read_text(encoding="utf-8"))
            self.assertEqual(routes[how]["work_request"]["text"], brief)
            self.assertIn("[Intent] Where does the fixture say what it is for?", done.stderr)
        for key in ("capability", "field", "field_source", "effective_intensity"):
            self.assertEqual(routes["file"].get(key), routes["stdin"].get(key), key)
        self.assertEqual([(n["id"], n["unit"], n["model_profile"], n["write_scope"]) for n in routes["file"]["nodes"]],
                         [(n["id"], n["unit"], n["model_profile"], n["write_scope"]) for n in routes["stdin"]["nodes"]])

    def test_every_compose_form_that_takes_a_brief_takes_standard_input_the_same_way(self):
        brief = "Review the change in the fixture\n"
        for label, extra in (("solo", ("--shape", "solo", "--capability", "autopilot-code")),
                             ("unit", ("--unit", "qa/code-review")),
                             ("unit with a field", ("--unit", "general/explore", "--field", "research"))):
            with self.subTest(form=label):
                done = self.run_cli(*extra, "--explain", "--slug", "g2-form", language="en", stdin=brief)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("[Intent] Review the change in the fixture", done.stderr)
                self.assertNotIn("compose-prompt-stdin", done.stderr)

    def test_standard_input_that_cannot_be_a_brief_ends_the_command_in_one_line_and_starts_nothing(self):
        cases = {
            "compose-prompt-stdin-empty": [dict(stdin=""), dict(stdin="  \n\t\n"), dict(stdin_bytes=b"")],
            "compose-prompt-stdin-too-large": [dict(stdin_bytes=b"a" * (65536 + 1))],
            "compose-prompt-stdin-not-utf8": [dict(stdin_bytes=b"\xff\xfe\x00")],
        }
        before = sorted(p.name for p in Path(self.root).rglob("*") if p.is_file())
        for reason, inputs in cases.items():
            for kwargs in inputs:
                with self.subTest(reason=reason, input=str(kwargs)[:30]):
                    done = self.run_cli("--unit", "general/explore", "--ephemeral", "--start", **kwargs)
                    self.assertEqual(done.returncode, 64, done.stderr)
                    last = [line for line in done.stderr.splitlines() if line.strip()]
                    self.assertEqual(len(last), 1, done.stderr)
                    self.assertTrue(last[0].startswith(f"capability-route: {reason}:"), last[0])
                    self.assertEqual(done.stdout, "")
        self.assertEqual(sorted(p.name for p in Path(self.root).rglob("*") if p.is_file()), before)   # nothing was sealed or started

    def test_a_closed_or_dev_null_standard_input_is_an_empty_brief_not_a_wait(self):
        for label, stdin in (("devnull", subprocess.DEVNULL), ("closed", None)):
            with self.subTest(stdin=label):
                brief = Path(self._tmp.name) / "brief.md"
                brief.write_text("x\n", encoding="utf-8")
                done = subprocess.run(
                    [sys.executable, str(HERE / "capability-route.py"), "compose", "--cwd", str(R.ROOT),
                     "--artifact-root", str(self.root), "--spec-read", "fixture", "--prompt-file", "-",
                     "--unit", "general/explore", "--unassigned", "--explain"],
                    text=True, capture_output=True, check=False, timeout=30,
                    env={**F.clean_environment(), "AGENT_HOME": str(R.ROOT)}, stdin=stdin,
                    **({} if stdin is not None else {"close_fds": True, "preexec_fn": lambda: os.close(0)}))
                self.assertEqual(done.returncode, 64, done.stderr)
                self.assertIn("capability-route: compose-prompt-stdin-empty:", done.stderr)

    def test_a_terminal_on_standard_input_is_refused_at_once(self):
        master, slave = pty.openpty()
        try:
            done = subprocess.run(
                [sys.executable, str(HERE / "capability-route.py"), "compose", "--cwd", str(R.ROOT),
                 "--artifact-root", str(self.root), "--spec-read", "fixture", "--prompt-file", "-",
                 "--unit", "general/explore", "--unassigned", "--explain"],
                text=True, capture_output=True, check=False, timeout=30, stdin=slave,
                env={**F.clean_environment(), "AGENT_HOME": str(R.ROOT)})
        finally:
            os.close(master)
            os.close(slave)
        self.assertEqual(done.returncode, 64, done.stderr)
        self.assertIn("capability-route: compose-prompt-stdin-terminal:", done.stderr)

    def test_a_pipe_that_sends_nothing_is_not_waited_for_without_end(self):
        import capability_route_compose as C
        read_end, write_end = os.pipe()
        try:
            with mock.patch.object(sys, "stdin", os.fdopen(read_end, "r", closefd=False)), \
                    mock.patch.object(C, "PROMPT_STDIN_IDLE_SECONDS", 0.2):
                with self.assertRaises(ValueError) as caught:
                    C.prompt_from_stdin()
        finally:
            os.close(read_end)
            os.close(write_end)
        self.assertTrue(str(caught.exception).startswith("compose-prompt-stdin-timeout:"), str(caught.exception))

    def test_the_prepared_worktree_key_does_not_name_a_file_for_standard_input(self):
        import capability_route_compose as C
        args = {"work_request": {"text": "same task"}, "cwd": str(R.ROOT), "shape": "solo"}
        from_stdin = C._compose_preparation_key(args, None, Path("-"))
        self.assertIsNone(from_stdin["request_path"])
        again = C._compose_preparation_key(args, None, Path("-"))
        self.assertEqual(from_stdin, again)        # a retried start of the same brief keeps its identity
        named = C._compose_preparation_key(args, None, Path(self._tmp.name) / "brief.md")
        self.assertTrue(named["request_path"].endswith("brief.md"))

    def test_stages_shows_the_unit_route_only_when_asked(self):
        shown = subprocess.run([sys.executable, str(HERE / "capability-route.py"), "stages", "--capability", "unit"],
                               text=True, capture_output=True, check=False)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        first, node = shown.stdout.splitlines()[:2]
        self.assertTrue(first.startswith("capability=unit field=- modes=default topology=staged"), first)
        self.assertIn("unit_choices=", node)
        self.assertIn("qa/code-review=code", node)
        self.assertIn("qa/test=needs --field", node)
        self.assertIn("general/explore=other", node)
        self.assertNotIn("dev/backend", node)                      # a writing unit is not offered
        blocks = json.loads(subprocess.run([sys.executable, str(HERE / "capability-route.py"), "stages", "--json"],
                                           text=True, capture_output=True, check=True).stdout)
        self.assertNotIn("unit", [block["capability"] for block in blocks])
        self.assertNotIn("route-frame", [block["capability"] for block in blocks])


if __name__ == "__main__":
    unittest.main()
