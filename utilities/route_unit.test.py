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
import re
import subprocess
import unittest
from pathlib import Path

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
        route = self.unit("qa/code-review")
        card = R.compose_card(route, include_decisions=False)
        self.assertIn("unit", card)
        self.assertIn("\n  field code (preset)\n", card)
        # 11-19: the line a person reads names the route's form (`unit(quick)`, as Fleet's bracket does), never the sealed `solo`; what is sealed does not change
        self.assertIn(" · unit(quick) ", card.splitlines()[0])
        self.assertNotIn("solo(", card.splitlines()[0])
        self.assertEqual(route["selection"]["shape"], "solo")

    def test_the_card_of_a_solo_route_keeps_its_own_word(self):
        card = R.compose_card(self.unit(None, capability="autopilot-code", shape="solo"), include_decisions=False)
        self.assertIn(" · solo(quick) ", card.splitlines()[0])


class UnitCliTest(F.FramedBase):
    def run_cli(self, *extra, task="Review the change\n"):
        brief = Path(self._tmp.name) / "brief.md"
        brief.write_text(task, encoding="utf-8")
        evidence = Path(self._tmp.name) / "headless.json"
        evidence.write_text(json.dumps(T.registered_headless()), encoding="utf-8")
        env = {**F.clean_environment(), "AGENT_HOME": str(R.ROOT), "AGENT_DISPATCH_JOBS": str(self.jobs),
               "FLEET_ROUTE_CHAIN_DIR": str(Path(self._tmp.name) / "chains")}
        return subprocess.run(
            [sys.executable, str(HERE / "capability-route.py"), "compose", "--cwd", str(R.ROOT),
             "--artifact-root", str(self.root), "--spec-read", "fixture", "--prompt-file", str(brief),
             "--registered-headless-evidence", str(evidence), "--unassigned", *extra],
            text=True, capture_output=True, env=env, check=False)

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

    def test_stages_shows_the_unit_route_only_when_asked(self):
        shown = subprocess.run([sys.executable, str(HERE / "capability-route.py"), "stages", "--capability", "unit"],
                               text=True, capture_output=True, check=False)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        first, node = shown.stdout.splitlines()[:2]
        self.assertTrue(first.startswith("capability=unit field=- modes=default topology=staged"), first)
        self.assertIn("unit_choices=", node)
        self.assertIn("qa/code-review=code", node)
        self.assertIn("qa/test=needs --field", node)
        self.assertNotIn("dev/backend", node)                      # a writing unit is not offered
        blocks = json.loads(subprocess.run([sys.executable, str(HERE / "capability-route.py"), "stages", "--json"],
                                           text=True, capture_output=True, check=True).stdout)
        self.assertNotIn("unit", [block["capability"] for block in blocks])
        self.assertNotIn("route-frame", [block["capability"] for block in blocks])


if __name__ == "__main__":
    unittest.main()
