#!/usr/bin/env python3
"""A route's field (routing/prd.md R5, R8): the registry check, compose, slug, stages, card, old routes.

Real compose and the real registry. A decided shape (direct, solo, staged) names its kind, its
field, or both; naming neither is refused with the missing option named. A framed route takes the
field as a hint only, and a frame proposal's leg names it (`field_source=frame`).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import argparse
import copy
import importlib.util
import json
import subprocess
import tempfile
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


F = _load("framed_route_for_field", "framed_route.test.py")
T, R = F.T, F.R
import capability_topology as CT  # noqa: E402
import route_plan as RP  # noqa: E402

FIELDS = ["plan", "design", "code", "lab", "research", "docs", "admin", "other"]


class FieldRegistryTest(unittest.TestCase):
    """A registry check, not a user gate: vocabulary, recipe defaults and field defaults agree."""

    def registry(self):
        return copy.deepcopy(CT.load_registry())

    def test_the_registry_declares_eight_fields_and_each_recipes_default(self):
        registry = self.registry()
        CT.validate_registry(registry)
        self.assertEqual(registry["fields"], FIELDS)
        self.assertEqual(list(CT.FIELDS), FIELDS)
        defaults = {(r["capability"], mode): CT.recipe_field(r, mode)
                    for r in registry["recipes"] for mode in r["modes"]}
        for (capability, mode), field in defaults.items():
            expected = {"autopilot-spec": "plan", "autopilot-design": "design", "autopilot-lab": "lab",
                        "autopilot-research": "research", "autopilot-draft": "docs", "autopilot-refine": "docs",
                        "autopilot-apply": "docs", "analyze-user": "other", "route-frame": None, "unit": None,
                        "analyze-project": "code" if mode == "code" else "docs"}.get(capability, "code")
            with self.subTest(capability=capability, mode=mode):
                self.assertEqual(field, expected)
        self.assertEqual({field: CT.field_default_recipe(registry, field) for field in FIELDS}, {
            "plan": ("autopilot-spec", None), "design": ("autopilot-design", None),
            "code": ("autopilot-code", None), "lab": ("autopilot-lab", "setup"),
            "research": ("autopilot-research", None), "docs": ("autopilot-draft", None),
            "admin": None, "other": None})

    def test_a_drifted_vocabulary_or_pairing_is_refused(self):
        def recipe(registry, capability):
            return next(r for r in registry["recipes"] if r["capability"] == capability)

        cases = (
            ("vocabulary renamed", lambda r: r.update(fields=FIELDS[:-1] + ["misc"]), "fields must declare"),
            ("default table incomplete", lambda r: r["field_default_recipes"].pop("other"), "field_default_recipes"),
            ("recipe field unknown", lambda r: recipe(r, "audit").update(field="dev"), "audit: field must be"),
            ("recipe field missing", lambda r: recipe(r, "autopilot-design").pop("field"), "autopilot-design: field"),
            ("frame given a field", lambda r: recipe(r, "route-frame").update(field="other"), "route-frame"),
            ("per-mode field misses a mode", lambda r: recipe(r, "analyze-project")["field"].pop("paper"),
             "per-mode field"),
            ("default recipe disagrees", lambda r: r["field_default_recipes"].update(
                plan={"capability": "autopilot-code"}), r"field_default_recipes\[plan\]"),
            ("default recipe unknown", lambda r: r["field_default_recipes"].update(
                lab={"capability": "autopilot-lab", "mode": "nope"}), "unknown recipe"),
            ("weights table missing", lambda r: r.pop("field_harness_weights"), "field_harness_weights must be"),
            ("weights table extra key", lambda r: r["field_harness_weights"].update(note="x"),
             "field_harness_weights must be"),
            ("weights checked_on malformed", lambda r: r["field_harness_weights"].update(checked_on="Nov 20"),
             "checked_on"),
            ("weights name an unknown field", lambda r: r["field_harness_weights"]["weights"].update(
                misc={"claude": 1.0, "codex": 1.0, "opencode": 1.0}), "declared fields"),
            ("weights row misses a harness", lambda r: r["field_harness_weights"]["weights"]["code"].pop("codex"),
             r"weights\[code\]"),
            ("weights cell is zero", lambda r: r["field_harness_weights"]["weights"]["code"].update(codex=0),
             r"weights\[code\]"),
            ("weights cell above one", lambda r: r["field_harness_weights"]["weights"]["code"].update(codex=1.1),
             r"weights\[code\]"),
            ("weights cell is a bool", lambda r: r["field_harness_weights"]["weights"]["code"].update(codex=True),
             r"weights\[code\]"),
            ("basis names a vendor model", lambda r: r["field_harness_weights"]["basis"]["profiles"].update(
                claude=["opus"]), "never a vendor model"),
            ("basis misses a harness", lambda r: r["field_harness_weights"]["basis"]["profiles"].pop("codex"),
             "never a vendor model"),
        )
        for label, mutate, message in cases:
            registry = self.registry()
            mutate(registry)
            with self.subTest(label), self.assertRaisesRegex(CT.TopologyError, message):
                CT.validate_registry(registry)


    def test_the_shipped_default_table_is_the_chosen_one(self):
        table = self.registry()["field_harness_weights"]
        self.assertEqual(table["checked_on"], "2026-11-20")
        self.assertEqual(table["weights"], {
            "code": {"claude": 0.95, "codex": 0.85, "opencode": 1.0},
            "design": {"claude": 1.0, "codex": 0.95, "opencode": 0.85},
            "docs": {"claude": 0.95, "codex": 1.0, "opencode": 0.95},
            "research": {"claude": 0.9, "codex": 1.0, "opencode": 1.0},
            "lab": {"claude": 1.0, "codex": 1.0, "opencode": 0.9},
            "plan": {"claude": 0.9, "codex": 0.8, "opencode": 1.0}})
        self.assertNotIn("admin", table["weights"])
        self.assertNotIn("other", table["weights"])


class FieldComposeTest(F.FramedBase):
    def decided(self, **kw):
        arguments = dict(capability=None, capability_mode=None, shape="direct", graph=None,
                         slug="field-fixture", cwd=R.ROOT, artifact_root=self.root, spec_read="fixture",
                         campaign_key="field-key")
        arguments.update(kw)
        return R.compose_route(**arguments)

    def basis(self, route, axis):
        return next(row for row in route["selection"]["selection_basis"] if row["axis"] == axis)

    def test_a_route_seals_the_default_row_of_its_field_and_a_field_without_a_row_seals_none(self):
        table = R.TOPO.load_registry()["field_harness_weights"]["weights"]
        for capability, mode, field in (("autopilot-code", "dev", "code"), ("autopilot-spec", None, "plan"),
                                        ("autopilot-design", None, "design"), ("autopilot-draft", None, "docs"),
                                        ("autopilot-research", None, "research"), ("autopilot-lab", "setup", "lab"),
                                        ("analyze-user", "init", "other")):
            with self.subTest(field=field):
                route = self.decided(capability=capability, capability_mode=mode)
                self.assertEqual(route["field"], field)
                allocation = route["dispatch_allocation"]
                if field in table:
                    self.assertEqual(allocation["field_weights"], table[field])
                else:
                    self.assertNotIn("field_weights", allocation)

    def test_a_kind_alone_records_its_default_field_as_preset(self):
        for capability, mode, field in (("autopilot-code", "dev", "code"), ("autopilot-spec", None, "plan"),
                                        ("analyze-project", "doc", "docs"), ("analyze-project", "code", "code"),
                                        ("autopilot-draft", None, "docs"), ("analyze-user", "init", "other")):
            with self.subTest(capability=capability, mode=mode):
                route = self.decided(capability=capability, capability_mode=mode)
                self.assertEqual((route["capability"], route["field"], route["field_source"]),
                                 (capability, field, "preset"))
                self.assertEqual(self.basis(route, "compose-capability")["source"], "explicit")
                R.verify_route(json.loads(json.dumps(route)), R.ROOT)

    def test_a_field_alone_opens_its_default_recipe(self):
        for field, capability, mode in (("plan", "autopilot-spec", "api"), ("design", "autopilot-design", "default"),
                                        ("code", "autopilot-code", "dev"), ("lab", "autopilot-lab", "setup"),
                                        ("research", "autopilot-research", "academic"),
                                        ("docs", "autopilot-draft", "doc")):
            with self.subTest(field=field):
                route = self.decided(field=field)
                self.assertEqual((route["capability"], route["capability_mode"]), (capability, mode))
                self.assertEqual((route["field"], route["field_source"]), (field, "explicit"))
                self.assertEqual(self.basis(route, "compose-capability"),
                                 {"axis": "compose-capability", "signal": capability, "source": "field-default"})
        # A field with a named mode keeps the mode.
        route = self.decided(field="lab", capability_mode="eval", shape="solo",
                             registered_headless_evidence=T.registered_headless())
        self.assertEqual((route["capability"], route["capability_mode"], route["field"]),
                         ("autopilot-lab", "eval", "lab"))

    def test_a_kind_and_a_field_keep_both(self):
        route = self.decided(capability="autopilot-draft", capability_mode="doc", field="admin")
        self.assertEqual((route["capability"], route["field"], route["field_source"]),
                         ("autopilot-draft", "admin", "explicit"))
        R.verify_route(json.loads(json.dumps(route)), R.ROOT)
        # The existing command is unchanged apart from the recorded field.
        plain = self.decided(capability="autopilot-draft", capability_mode="doc")
        self.assertEqual(plain["nodes"], route["nodes"])

    def test_naming_neither_is_refused_with_the_missing_option_named(self):
        for shape in ("direct", "solo", "staged"):
            with self.subTest(shape=shape):
                with self.assertRaises(ValueError) as refused:
                    self.decided(shape=shape)
                message = str(refused.exception)
                self.assertTrue(message.startswith(f"compose-capability-required:{shape} "), message)
                self.assertIn("--capability", message)
                self.assertIn("--field", message)

    def test_admin_or_other_alone_asks_for_the_kind(self):
        for field in ("admin", "other"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, rf"^compose-capability-required:--field {field} .*--capability"):
                    self.decided(field=field)
        route = self.decided(capability="analyze-user", capability_mode="init", field="other")
        self.assertEqual((route["field"], route["field_source"]), ("other", "explicit"))

    def test_an_unknown_field_is_refused(self):
        for value in ("dev", "Code", "개발"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, rf"^compose-field-unknown:{value} \(fields: plan,"):
                    self.decided(capability="autopilot-code", field=value)

    def test_a_framed_route_takes_the_field_as_a_hint_only(self):
        route = self.compose(field="docs", capability="autopilot-draft")
        self.assertEqual(route["capability"], "route-frame")
        self.assertNotIn("field", route)
        self.assertNotIn("field_source", route)
        self.assertEqual(route["work_request"]["routing_hints"], {"capability": "autopilot-draft", "field": "docs"})
        self.assertEqual(CT.route_field(route), (None, None))
        self.assertEqual(self.compose()["work_request"].get("routing_hints"), None)

    def test_the_card_shows_one_field_line(self):
        route = self.decided(capability="autopilot-draft", capability_mode="doc", field="admin")
        card = R.compose_card(route, include_decisions=False)
        self.assertIn("\n  field admin (explicit)\n", card)
        self.assertEqual(card.count("\n  field "), 1)
        decided = R.compose_card(self.decided(field="docs"))
        self.assertIn("\n  field docs (explicit)\n", decided)
        self.assertIn("  capability=autopilot-draft (source=field-default)", decided)
        self.assertNotIn("\n  field ", R.compose_card(self.compose()))

    def test_an_old_route_reads_its_field_from_the_recipe_without_rewriting_it(self):
        route = self.decided(capability="analyze-project", capability_mode="doc")
        for key in ("field", "field_source"):
            route.pop(key)
        route["route_hash"] = R.route_hash(route)
        route["route_id"] = R.ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
        before = json.dumps(route, sort_keys=True)
        self.assertEqual(CT.route_field(route), ("docs", "preset"))
        self.assertIn("\n  field docs (preset)\n", R.compose_card(route, include_decisions=False))
        R.verify_route(json.loads(before), R.ROOT)
        self.assertEqual(json.dumps(route, sort_keys=True), before)
        self.assertEqual(CT.route_field({**route, "route_plan": {"decision": "d", "digest": "x", "index": 0}}),
                         ("docs", "frame"))
        self.assertEqual(CT.route_field({**route, "capability": "autopilot-ship", "capability_mode": "package"}),
                         ("code", "preset"))
        self.assertEqual(CT.route_field({"capability": "no-such-kind"}), (None, None))

    def test_the_default_slug_starts_with_the_field(self):
        self.assertRegex(R.compose_default_slug("Fix the window sim", field="docs"), r"^docs-fix-window-sim-[0-9a-f]{10}$")
        self.assertRegex(R.compose_default_slug("", field="plan"), r"^plan$")
        self.assertEqual(R.compose_default_slug("", shape="framed"), "framed")

        def named(**kw):
            values = dict(field=None, capability=None, capability_mode=None)
            values.update(kw)
            return argparse.Namespace(**values)
        cases = ((named(capability="autopilot-spec"), None, "plan"),
                 (named(capability="analyze-project", capability_mode="doc"), None, "docs"),
                 (named(capability="analyze-project"), None, "code"),
                 (named(capability="autopilot-draft", field="admin"), None, "admin"),
                 (named(field="lab"), None, "lab"),
                 (named(capability="autopilot-code"), {"leg": {"field": "docs"}}, "docs"),
                 (named(), None, None),
                 (named(capability="no-such-kind"), None, None))
        for arguments, binding, expected in cases:
            with self.subTest(arguments=arguments, binding=binding):
                self.assertEqual(R._compose_slug_field(arguments, "direct", binding), expected)

    def test_the_cli_names_the_field_and_refuses_in_the_existing_form(self):
        task = Path(self._tmp.name) / "task.md"
        task.write_text("Record a correction already made\n", encoding="utf-8")
        env = {**F.clean_environment(), "AGENT_HOME": str(R.ROOT), "AGENT_DISPATCH_JOBS": str(self.jobs),
               "FLEET_ROUTE_CHAIN_DIR": str(Path(self._tmp.name) / "chains")}
        base = [sys.executable, str(HERE / "capability-route.py"), "compose", "--shape", "direct",
                "--cwd", str(R.ROOT), "--artifact-root", str(self.root), "--spec-read", "fixture",
                "--prompt-file", str(task), "--unassigned", "--explain"]

        def run(*extra):
            return subprocess.run(base + list(extra), text=True, capture_output=True, env=env, check=False)
        for extra, needle in ((["--field", "nope"], "capability-route: compose-field-unknown:nope (fields: plan,"),
                              (["--field", "admin"], "capability-route: compose-capability-required:--field admin"),
                              ([], "capability-route: compose-capability-required:direct")):
            with self.subTest(extra=extra):
                refused = run(*extra)
                self.assertEqual(refused.returncode, 64, refused.stderr)
                self.assertIn(needle, refused.stderr)
        named = run("--field", "plan")
        self.assertEqual(named.returncode, 0, named.stderr)
        explained = json.loads(named.stdout.strip().splitlines()[-1])
        self.assertEqual((explained["capability"], explained["shape"]), ("autopilot-spec", "direct"))
        self.assertIn("\n  field plan (explicit)\n", named.stderr)
        self.assertIn(" · slug plan-record-correction", named.stderr)
        kind = run("--capability", "autopilot-code")
        self.assertEqual(kind.returncode, 0, kind.stderr)
        self.assertIn("\n  field code (preset)\n", kind.stderr)
        self.assertIn(" · slug code-record-correction", kind.stderr)

    def test_a_non_git_direct_with_a_named_field_skips_the_task_classification(self):
        with tempfile.TemporaryDirectory(prefix="field-non-git-") as folder:
            task = Path(folder) / "task.md"
            # The classifier would read this as academic research; the named field decides instead.
            task.write_text("논문 조사 결과를 정리한 문서의 오탈자만 고친다\n", encoding="utf-8")
            env = {**F.clean_environment(), "AGENT_HOME": str(R.ROOT), "AGENT_DISPATCH_JOBS": str(self.jobs),
                   "FLEET_ROUTE_CHAIN_DIR": str(Path(self._tmp.name) / "chains")}
            base = [sys.executable, str(HERE / "capability-route.py"), "compose", "--shape", "direct",
                    "--cwd", folder, "--artifact-root", str(self.root), "--spec-read", "fixture",
                    "--prompt-file", str(task), "--unassigned", "--explain"]
            named = subprocess.run(base + ["--field", "docs"], text=True, capture_output=True, env=env, check=False)
            self.assertEqual(named.returncode, 0, named.stderr)
            self.assertEqual(json.loads(named.stdout.strip().splitlines()[-1])["capability"], "autopilot-draft")
            # Nothing named: the existing non-Git path is unchanged (recorded for the supervisor, not fixed here).
            unnamed = subprocess.run(base, text=True, capture_output=True, env=env, check=False)
            self.assertEqual(unnamed.returncode, 0, unnamed.stderr)
            self.assertEqual(json.loads(unnamed.stdout.strip().splitlines()[-1])["capability"], "autopilot-code")


class FieldStagesTest(unittest.TestCase):
    def stages(self, *extra):
        return subprocess.run([sys.executable, str(HERE / "capability-route.py"), "stages", *extra],
                              text=True, capture_output=True, check=False)

    def test_each_block_names_its_field_on_its_first_line(self):
        draft = self.stages("--capability", "autopilot-draft")
        self.assertEqual(draft.returncode, 0, draft.stderr)
        self.assertTrue(draft.stdout.splitlines()[0].startswith("capability=autopilot-draft field=docs modes="),
                        draft.stdout.splitlines()[0])
        analyze = self.stages("--capability", "analyze-project").stdout.splitlines()[0]
        self.assertIn(" field=code:code,doc:docs,paper:docs ", analyze)
        every = [line for line in self.stages().stdout.splitlines() if line.startswith("capability=")]
        self.assertTrue(every and all(" field=" in line for line in every))
        blocks = json.loads(self.stages("--capability", "autopilot-spec", "--json").stdout)
        self.assertEqual([block["field"] for block in blocks], ["plan"])


class FieldProposalLegTest(unittest.TestCase):
    """The frame proposal's optional `field`: kept when known, left out and named otherwise."""

    def test_a_leg_keeps_a_known_field_and_names_an_unknown_one(self):
        notes = []
        kept = RP._normal_leg({"capability": "autopilot-draft", "mode": "doc", "field": "admin",
                               "shape": "direct"}, 0, notes)
        self.assertEqual(kept["field"], "admin")
        self.assertEqual(notes, [])
        dropped = RP._normal_leg({"capability": "autopilot-draft", "field": "paperwork", "shape": "direct"}, 1, notes)
        self.assertNotIn("field", dropped)
        self.assertEqual(notes, ["ignored:legs[1].field"])
        self.assertNotIn("field", RP._normal_leg({"capability": "autopilot-code", "shape": "direct"}, 2))
        self.assertEqual(RP.field_vocabulary(), tuple(FIELDS))


if __name__ == "__main__":
    unittest.main()
