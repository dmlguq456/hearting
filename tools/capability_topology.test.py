#!/usr/bin/env python3
import copy, hashlib, importlib.util, json, unittest
from pathlib import Path

P = Path(__file__).with_name("capability_topology.py")
S = importlib.util.spec_from_file_location("topology", P); T = importlib.util.module_from_spec(S); S.loader.exec_module(T)

PRESERVED_FULL_FIELD_DIGESTS = {
    ("autopilot-apply", ('default',)): (
        "8b5adb03d56bf8b6e68c4ff78f35cde2e0076dcc3db46841658f4cb85645bf8b",
        "926c9eff35134529d23574f9052da464493b09fd52cb98da03785ff7798669d7",
    ),
    ("autopilot-code", ('audit', 'debug', 'dev')): (
        "a76e32172c0cc35b1ce1e31e464bd6bdda8f824ecdb50bb5cbbcc7c7e7627d92",
        "6999e2b826a3f458169cf5d54906f4da79e33090815b7439504a39aa6d4cf341",
    ),
    ("autopilot-design", ('default',)): (
        "c75c56b11affed41560aebf57faba71a07b9903e2f24224ce3207cbf290c9168",
        "523b32502063400fd601697545d5cd4ae859308588b7176c0fa648f525e5be5e",
    ),
    ("autopilot-draft", ('doc', 'paper', 'presentation')): (
        "962db29f856dd3f6a9a8aa9743fc57bc8aa691dad014a9cef7a432e50cea32c5",
        "1bb17c28bdb34877667242530f1f0af2c3a330caa77620f1b60734f663f5f72f",
    ),
    ("autopilot-lab", ('setup',)): (
        "502f66344295ad67f2d3e09499efcc91d47d1caf8ef3b03863f0c3b37549c2a5",
        "dd5e1116e2b49489adc69f022cc69f8f91337688de5ab08e4963a70c20e1f85a",
    ),
    ("autopilot-lab", ('eval',)): (
        "f3b5dbd108b70b96c29f0af3e76c4cb10f9ce31aed215aa6f0f4ea2f1edac920",
        "07c9f4e193ff843ba33a1da2a7d4af662070b4d3ebf235f07173802d2e928b51",
    ),
    ("autopilot-refine", ('default',)): (
        "74d2f582f1395d07caf42fb3c4f849f2cca00e9f81f19e7b051a15fc83ca829f",
        "17e5d03f2aaba86c476743ee29b453e0961455973c3c347cac6a9634c217b529",
    ),
    ("autopilot-research", ('academic', 'market', 'technology')): (
        "aeca7dd3b3a3557038b8033a80ce66a23ad4be647f1bc1efc4a2314eddb2bf57",
        "637d726f855db89ed54a3fd48362488d5e90a4d3a1b59919447f5b040075807f",
    ),
    ("autopilot-ship", ('default',)): (
        "648616df104927558bc5cca6a65e9455f48b30be63d16d9b7b47adc26d80a313",
        "913de8c5f6200a539e6fe19ec488c42120dc6e4e0a0cb2149f33a3aa8cd4f326",
    ),
    ("autopilot-spec", ('api', 'app', 'cli', 'library', 'research', 'update')): (
        "096a33a46adf1886561c032019301c7dbc64ec94729acee715c74ed3f4af302a",
        "f7bf589ba369a08a7031c71db8a2523b250af84be5ef6e0e4d9b00a1cdcb897c",
    ),
}


def full_field_digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


class TestTopology(unittest.TestCase):
    def setUp(self): self.r = T.load_registry()
    def test_exact_coverage_and_digest(self):
        result = T.validate_registry(self.r); self.assertEqual((9, 21), (result["capabilities"], result["recipes"])); self.assertEqual(T.registry_digest(self.r), T.registry_digest(json.loads(json.dumps(self.r, sort_keys=True))))
    def test_missing_coverage(self):
        r=copy.deepcopy(self.r); r["recipes"].pop(); self.assertRaises(T.TopologyError, T.validate_registry, r)
    def test_cycle(self):
        r=copy.deepcopy(self.r); n=r["recipes"][0]["standard_plus"]["nodes"]; n[0]["depends_on"]=[n[-1]["id"]]; self.assertRaisesRegex(T.TopologyError,"cycle",T.validate_registry,r)
    def test_dispatch_depth_and_resource_boundary(self):
        r=copy.deepcopy(self.r); r["recipes"][0]["standard_plus"]["nodes"][0]["dispatch_depth"]=3; self.assertRaises(T.TopologyError,T.validate_registry,r)
        r=copy.deepcopy(self.r); lab=next(x for x in r["recipes"] if x["capability"]=="autopilot-lab"); lab["standard_plus"]["nodes"][-1]["dispatch_depth"]=2; self.assertRaises(T.TopologyError,T.validate_registry,r)
    def test_every_bare_depth_key_and_wrong_max_are_rejected(self):
        for location in ("recipe","quick","standard_plus","node"):
            for key in ("depth","owner_depth","max_depth"):
                r=copy.deepcopy(self.r); recipe=r["recipes"][0]
                target={
                    "recipe":recipe,
                    "quick":recipe["quick"],
                    "standard_plus":recipe["standard_plus"],
                    "node":recipe["standard_plus"]["nodes"][0],
                }[location]
                target[key]=2
                with self.subTest(location=location,key=key):
                    self.assertRaises(T.TopologyError,T.validate_registry,r)
        r=copy.deepcopy(self.r)
        r["recipes"][0]["standard_plus"]["max_dispatch_depth"]=1
        self.assertRaisesRegex(T.TopologyError,"max_dispatch_depth",T.validate_registry,r)
    def test_namespace_vocabularies_fail_closed(self):
        r=copy.deepcopy(self.r); r["execution_surfaces"].append("mystery")
        self.assertRaisesRegex(T.TopologyError,"execution-surface",T.validate_registry,r)
        r=copy.deepcopy(self.r); r["recipes"][0]["standard_plus"]["nodes"][0]["fallback_hops"]=["mystery"]
        self.assertRaisesRegex(T.TopologyError,"fallback hops",T.validate_registry,r)
        r=copy.deepcopy(self.r)
        r["recipes"][0]["standard_plus"]["nodes"][0]["runtime_requirements"]=["mystery"]
        self.assertRaisesRegex(T.TopologyError,"runtime requirements",T.validate_registry,r)

    def test_bare_filename_output_is_checked_as_a_path(self):
        r=copy.deepcopy(self.r)
        code=next(x for x in r["recipes"] if x["capability"]=="autopilot-code")
        plan=next(n for n in code["standard_plus"]["nodes"] if n["id"]=="plan")
        plan["write_scope"]=["plan/**"]
        self.assertRaises(T.TopologyError,T.validate_registry,r)
    def test_reviewer_and_map_scopes(self):
        r=copy.deepcopy(self.r); r["recipes"][0]["standard_plus"]["nodes"][1]["write_scope"]=["source/**"]; self.assertRaises(T.TopologyError,T.validate_registry,r)
        r=copy.deepcopy(self.r); d=next(x for x in r["recipes"] if x["capability"]=="autopilot-design"); d["standard_plus"]["nodes"][0]["write_scope"]=["design/**"]; self.assertRaises(T.TopologyError,T.validate_registry,r)
    def test_concurrent_overlap(self):
        r=copy.deepcopy(self.r); d=next(x for x in r["recipes"] if x["capability"]=="autopilot-design"); critic=next(n for n in d["standard_plus"]["nodes"] if n["id"]=="critic-review"); critic["depends_on"]=[]; critic["outputs"]=["designs/<cycle>/04_review/verify/critic-verdict.json"]; critic["write_scope"]=["designs/<cycle>/04_review/verify/**"]; self.assertRaisesRegex(T.TopologyError,"overlap",T.validate_registry,r)
    def test_spec_scope_requires_owner_or_precondition(self):
        r=copy.deepcopy(self.r); code=next(x for x in r["recipes"] if x["capability"]=="autopilot-code")
        execute=next(n for n in code["standard_plus"]["nodes"] if n["id"]=="execute")
        execute["write_scope"]=["spec/**","checklist.md","dev_logs/**"]
        self.assertRaisesRegex(T.TopologyError,"spec write scope requires",T.validate_registry,r)
        execute["guard_preconditions"]=["artifact-order-prechecked"]
        T.validate_registry(r)
    def test_tracking_and_rollout_schema_fail_closed(self):
        r=copy.deepcopy(self.r); r["tracking_values"]=["tracked"]
        self.assertRaisesRegex(T.TopologyError,"tracking_values",T.validate_registry,r)
        r=copy.deepcopy(self.r); r["rollout"]["route_compiler"]="report-only"
        self.assertRaisesRegex(T.TopologyError,"enforced",T.validate_registry,r)
        r=copy.deepcopy(self.r); r["rollout"]["legacy_low_level_dispatch"]=True
        self.assertRaisesRegex(T.TopologyError,"retired",T.validate_registry,r)
        for legacy in (2,3,4,5,6,7,8):
            r=copy.deepcopy(self.r); r["schema_version"]=legacy
            self.assertRaisesRegex(T.TopologyError,"read-only",T.validate_registry,r)
    def test_conditional_artifact_sink_coverage(self):
        expected={
            ("autopilot-code",("audit","debug","dev")):("report","report","final_report.md"),
            ("autopilot-draft",("doc","paper","presentation")):("finalize","finalize","final-artifact"),
            # The setup sink moved off the detached run: a training process exiting is
            # not a workflow completing, so the sink offer anchors on the handoff that
            # records what happens next (2026-08-04 BC_ResNet_tf).
            ("autopilot-lab",("setup",)):("handoff","full-run","experiment-artifact"),
            ("autopilot-lab",("eval",)):("sync","publish","bundle-publication.json"),
            ("autopilot-refine",("default",)):("transaction","transaction","revised-artifact"),
            ("autopilot-research",("academic","market","technology")):("claim-verify","report","research-artifact"),
        }
        observed={}
        for recipe in self.r["recipes"]:
            rows=recipe.get("conditional_extensions",[])
            if not rows: continue
            self.assertEqual(len(rows),1)
            row=rows[0]; source=row["source_outputs"][0]
            observed[(recipe["capability"],tuple(recipe["modes"]))]=(
                row["after"][0],source["node"],source["output"])
            self.assertEqual(row["activation_condition"],"artifact-sink-available")
            self.assertEqual(row["extension"],"artifact-sink")
            self.assertEqual(row["on_unavailable"],"skip")
        self.assertEqual(observed,expected)
    def test_conditional_extension_validation_fails_closed(self):
        def code_recipe(registry):
            return next(x for x in registry["recipes"] if x["capability"]=="autopilot-code")
        r=copy.deepcopy(self.r); code_recipe(r)["conditional_extensions"][0]["activation_condition"]="mystery"
        self.assertRaisesRegex(T.TopologyError,"unknown activation",T.validate_registry,r)
        r=copy.deepcopy(self.r); code_recipe(r)["conditional_extensions"][0]["after"]=["execute"]
        self.assertRaisesRegex(T.TopologyError,"terminal nodes",T.validate_registry,r)
        r=copy.deepcopy(self.r); code_recipe(r)["conditional_extensions"][0]["source_outputs"][0]["output"]="missing.md"
        self.assertRaisesRegex(T.TopologyError,"not declared",T.validate_registry,r)
        r=copy.deepcopy(self.r); code_recipe(r)["conditional_extensions"][0]["extension"]="unknown-sink"
        self.assertRaisesRegex(T.TopologyError,"must target artifact-sink",T.validate_registry,r)
        r=copy.deepcopy(self.r); code_recipe(r)["conditional_extensions"][0]["on_unavailable"]="fail"
        self.assertRaisesRegex(T.TopologyError,"must be skip",T.validate_registry,r)
        r=copy.deepcopy(self.r); r["activation_conditions"]["artifact-sink-available"]["success_state"]="configured"
        self.assertRaisesRegex(T.TopologyError,"activation contract mismatch",T.validate_registry,r)
    def test_unknown_unit_ref_fails_closed(self):
        r=copy.deepcopy(self.r); r["recipes"][0]["standard_plus"]["nodes"][0]["unit"]="dev/does-not-exist"
        self.assertRaisesRegex(T.TopologyError,"unknown unit",T.validate_registry,r)
        r=copy.deepcopy(self.r); del r["recipes"][0]["standard_plus"]["nodes"][0]["unit"]
        self.assertRaisesRegex(T.TopologyError,"unit ref required",T.validate_registry,r)
    def test_kind_worker_type_mismatch(self):
        r=copy.deepcopy(self.r); verify=r["recipes"][0]["standard_plus"]["nodes"][1]
        self.assertEqual(verify["kind"],"review-worker")
        verify["unit"]="dev/backend"; verify["role"]="fast implementer"
        self.assertRaisesRegex(T.TopologyError,"incompatible",T.validate_registry,r)
    def test_node_role_must_match_unit_role(self):
        r=copy.deepcopy(self.r); r["recipes"][0]["standard_plus"]["nodes"][0]["role"]="deep maker"
        self.assertRaisesRegex(T.TopologyError,"differs from",T.validate_registry,r)
    def test_reserved_unit_pins(self):
        r=copy.deepcopy(self.r); handback=r["recipes"][0]["standard_plus"]["nodes"][2]
        self.assertEqual(handback["kind"],"capability-owner")
        handback["unit"]="qa/code-review"
        self.assertRaisesRegex(T.TopologyError,"reserved unit",T.validate_registry,r)
        r=copy.deepcopy(self.r); r["recipes"][0]["standard_plus"]["nodes"][0]["unit"]="_kernel/owner"
        self.assertRaisesRegex(T.TopologyError,"reserved unit",T.validate_registry,r)
    def test_review_worker_requires_read_only_unit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake=Path(td)/"qa"; fake.mkdir()
            (fake/"fake.md").write_text(
                "---\nunit: qa/fake\nrole: fast reviewer\nworker_type: review\nread_only: false\n---\nbody\n",
                encoding="utf-8")
            old=T.UNITS; T.UNITS=Path(td); T._UNIT_CACHE.clear()
            try:
                node={"id":"x","kind":"review-worker","role":"fast reviewer","unit":"qa/fake"}
                with self.assertRaisesRegex(T.TopologyError,"read_only"):
                    T._validate_unit_ref({"capability":"t"},node,self.r)
            finally:
                T.UNITS=old; T._UNIT_CACHE.clear()
    def test_parallel_group_declarations(self):
        code=next(x for x in self.r["recipes"] if x["capability"]=="autopilot-code")
        groups=code["standard_plus"]["parallel_groups"]
        self.assertEqual([g["id"] for g in groups],["frame","plan","impl-review","plan-check"])
        self.assertEqual(groups[0]["width_by_intensity"],{
            "standard":2,"strong":3,"thorough":3,"adversarial":3})
        self.assertEqual(groups[1]["width_by_intensity"],{
            "strong":2,"thorough":3,"adversarial":3})
        self.assertEqual(groups[2]["width_by_intensity"],{
            "strong":2,"thorough":3,"adversarial":3})
        self.assertEqual(groups[3]["width_by_intensity"],{
            "strong":2,"thorough":3,"adversarial":3})
        for group in groups:
            self.assertEqual(group["join_policy"],"all")
            self.assertEqual(group["independence_axes"],["cross-harness","model-profile","perspective"])
            self.assertEqual(group["legs"][0]["suffix"],"anchor")
        # Framing anchors (2-way from standard) exist exactly on the generative
        # recipes whose direction is set in-pipeline; prescriptive/bounded
        # recipes keep review-only strong anchors (user directive 2026-07-24).
        framing={"autopilot-code":"frame","autopilot-spec":"research","autopilot-draft":"material-strategy",
                 "autopilot-design":"refs","autopilot-research":"retrieval"}
        for recipe in self.r["recipes"]:
            anchors=recipe["standard_plus"].get("parallel_groups",[])
            standard_anchors=[a["node"] for a in anchors if a["min_intensity"]=="standard"]
            expected=framing.get(recipe["capability"])
            with self.subTest(capability=recipe["capability"],modes=recipe["modes"]):
                self.assertNotIn("replications",recipe["standard_plus"])
                self.assertEqual(standard_anchors,[expected] if expected else [])
    def test_parallel_group_validation_fails_closed(self):
        def broken(mutate,capability="autopilot-code"):
            r=copy.deepcopy(self.r)
            recipe=next(x for x in r["recipes"] if x["capability"]==capability)
            mutate(recipe["standard_plus"])
            return r
        def legacy_singular(g): g["replication"]=g.pop("parallel_groups")[2]
        def report_as_non_terminal_anchor(g):
            # "report" is the only node in this graph with no dependents, but
            # it is also `terminal: true`, and G6/AC 21 now rejects a parallel
            # group on a terminal node before this check ever runs. Strip the
            # terminal flag on this deep copy so the fixture still isolates
            # the "requires a downstream consumer" rule it targets.
            next(n for n in g["nodes"] if n["id"]=="report").pop("terminal",None)
            g["parallel_groups"][0].update(node="report")
        code_cases={
            "legacy replication keys": legacy_singular,
            "non-empty list": lambda g: g.update(parallel_groups=[]),
            "require exactly": lambda g: g["parallel_groups"][2].update(extra=True),
            "duplicate parallel group/anchor": lambda g: g["parallel_groups"].append(dict(g["parallel_groups"][0])),
            "not in graph": lambda g: g["parallel_groups"][2].update(node="missing-node"),
            "requires a downstream consumer": report_as_non_terminal_anchor,
            "requires a direct review arbiter": lambda g: g["parallel_groups"][0].update(node="test"),
            "standard\\+ tier": lambda g: g["parallel_groups"][2].update(min_intensity="quick"),
            "widths must be monotonic integers": lambda g: g["parallel_groups"][2]["width_by_intensity"].update(strong=5),
            "cross-harness axis required":
                lambda g: g["parallel_groups"][2].update(independence_axes=["model-profile","perspective"]),
        }
        for pattern,mutate in code_cases.items():
            with self.subTest(pattern=pattern):
                self.assertRaisesRegex(T.TopologyError,pattern,T.validate_registry,broken(mutate))
        # kind vocabulary: a capability-owner node can never anchor a parallel group
        r=broken(lambda g: g["parallel_groups"][0].update(node="handback"),capability="autopilot-apply")
        self.assertRaisesRegex(T.TopologyError,"review, map, or pipeline worker",
            T.validate_registry,r)
        # anchor output shape: concrete files for stage anchors, '<dir>/**' only for map anchors
        r=broken(lambda g: next(n for n in g["nodes"] if n["id"]=="plan").update(outputs=["plan/**"],write_scope=["plan/**"]))
        self.assertRaisesRegex(T.TopologyError,"concrete",T.validate_registry,r)
        r=broken(lambda g: next(n for n in g["nodes"] if n["id"]=="research").update(
            outputs=["spec/_internal/research/spec-*/**"]),capability="autopilot-spec")
        self.assertRaisesRegex(T.TopologyError,"concrete",T.validate_registry,r)
    def test_registered_nodes_reject_mini_profile(self):
        r=copy.deepcopy(self.r)
        r["recipes"][0]["standard_plus"]["nodes"][0]["model_profile"]="mini"
        self.assertRaisesRegex(T.TopologyError,"mini/unregistered",T.validate_registry,r)
    def test_leg_class_schema_rejections_single_assertion(self):
        # AC 1: six rejection cases, each a distinct single-assertion fixture.
        # The plan group is peer legs [anchor(deep), alternative(balanced-deep),
        # implementation-risk(light)] at widths [2,3,3]; fixtures that change leg
        # shape are built from those real legs so unrelated axis checks stay silent.
        def broken(mutate,capability="autopilot-code"):
            r=copy.deepcopy(self.r)
            recipe=next(x for x in r["recipes"] if x["capability"]==capability)
            mutate(recipe["standard_plus"])
            return r
        def plan_group(g):
            return next(group for group in g["parallel_groups"] if group["id"]=="plan")
        def peer_legs(g):
            return [dict(leg) for leg in plan_group(g)["legs"]]
        def aux(suffix,check="simplicity-check",profile="light"):
            return {"suffix":suffix,"perspective":f"{suffix}-check","model_profile":profile,
                    "leg_class":"auxiliary","auxiliary_check":check}
        # 1: leg_class missing
        self.assertRaisesRegex(T.TopologyError,"leg requires leg_class",
            T.validate_registry,
            broken(lambda g: plan_group(g)["legs"][1].pop("leg_class")))
        # 2: leg_class outside vocabulary
        self.assertRaisesRegex(T.TopologyError,"invalid leg_class",
            T.validate_registry,
            broken(lambda g: plan_group(g)["legs"][1].update(leg_class="scout")))
        # 3: auxiliary without auxiliary_check
        self.assertRaisesRegex(T.TopologyError,"auxiliary legs require exactly",
            T.validate_registry,
            broken(lambda g: plan_group(g).update(legs=peer_legs(g)[:2]+[
                {"suffix":"simplicity","perspective":"simplicity-check","model_profile":"light",
                 "leg_class":"auxiliary"}])))
        # 4: peer with auxiliary_check
        self.assertRaisesRegex(T.TopologyError,"peer leg must not carry auxiliary_check",
            T.validate_registry,
            broken(lambda g: plan_group(g)["legs"][1].update(auxiliary_check="simplicity-check")))
        # 5: non-light auxiliary
        self.assertRaisesRegex(T.TopologyError,"must use model_profile light",
            T.validate_registry,
            broken(lambda g: plan_group(g).update(legs=peer_legs(g)[:2]+
                [aux("simplicity",profile="balanced-deep")])))
        # 6: auxiliary before a peer
        self.assertRaisesRegex(T.TopologyError,"must not precede a peer",
            T.validate_registry,
            broken(lambda g: plan_group(g).update(legs=[aux("simplicity")]+peer_legs(g)[:2])))
        # AC 3: duplicate auxiliary_check kinds inside one group (2 peers + 2 aux
        # with the same check; adversarial width widened to 4).
        def dup_aux(g):
            group=plan_group(g)
            group.update(legs=peer_legs(g)[:2]+[aux("simplicity"),aux("edge-case",check="simplicity-check")])
            group["width_by_intensity"]["adversarial"]=4
        self.assertRaisesRegex(T.TopologyError,"auxiliary_check kinds must be unique",
            T.validate_registry,broken(dup_aux))
        # AC 4: single merged rejection when declared peers + auxiliaries exceed
        # parallel_group_max_width (2 peers + 3 auxiliaries = 5 legs).
        def over_max(g):
            plan_group(g).update(legs=peer_legs(g)[:2]+
                [aux("simplicity"),aux("edge-case"),aux("failure-mode")])
        self.assertRaisesRegex(T.TopologyError,"legs exceed parallel_group_max_width",
            T.validate_registry,broken(over_max))
    def test_auxiliary_group_width_cap_and_peer_floor(self):
        # AC 23 / D6: auxiliary-bearing groups keep declared max width <= 3 even
        # though parallel_group_max_width stays 4 at the schema level.
        def broken(mutate,capability="autopilot-code"):
            r=copy.deepcopy(self.r)
            recipe=next(x for x in r["recipes"] if x["capability"]==capability)
            mutate(recipe["standard_plus"])
            return r
        def aux(suffix,check="simplicity-check",profile="light"):
            return {"suffix":suffix,"perspective":f"{suffix}-check","model_profile":profile,
                    "leg_class":"auxiliary","auxiliary_check":check}
        def plan_group(g):
            return next(x for x in g["parallel_groups"] if x["id"]=="plan")
        # widen the plan group to adversarial width 4 with one auxiliary leg
        def widen(g):
            group=plan_group(g)
            group["width_by_intensity"]["adversarial"]=4
            group["legs"]=group["legs"]+[aux("simplicity")]
        r=broken(widen)
        self.assertRaisesRegex(T.TopologyError,"declared width at most 3",T.validate_registry,r)
        # a valid 2-peer + 1-auxiliary 3-way compiles. AC 5 requires the
        # ARBITER's gate to declare it, and `plan` is a pipeline-stage anchor,
        # so that is its downstream review-worker `plan-check` -- never `plan`'s
        # own gate, which is the proposition G1 disproved.
        def add_three(g):
            group=plan_group(g)
            group["width_by_intensity"]["thorough"]=3
            group["legs"]=group["legs"][:2]+[aux("simplicity")]
        r=broken(add_three)
        recipe=next(x for x in r["recipes"] if x["capability"]=="autopilot-code")
        r["completion_gate_contracts"]["code-plan-check"]["auxiliary_arbiter"]=True
        T.validate_registry(r)
        # and declaring it on the anchor's own gate does not satisfy the rule
        r=broken(add_three)
        r["completion_gate_contracts"]["code-plan"]["auxiliary_arbiter"]=True
        self.assertRaisesRegex(T.TopologyError,"must declare auxiliary_arbiter",
                               T.validate_registry,r)
        # a single-peer group violates the declared-width peer floor
        def single_peer(g):
            group=plan_group(g)
            group["legs"]=group["legs"][:1]+[aux("simplicity"),aux("edge-case")]
        r=broken(single_peer)
        self.assertRaisesRegex(T.TopologyError,"at least two peer legs",T.validate_registry,r)
    def test_ac5_auxiliary_verdict_enum_cannot_hold_a_blocking_value(self):
        # AC 5 (back half): an auxiliary leg's advisory semantics are enforced by
        # the SHAPE of its verdict enum, not by convention. Every registered
        # auxiliary-check unit may only say "findings"/"none"; nothing in its
        # vocabulary can hold a stage's gate. Contrast with the arbiter units,
        # whose enums do carry a blocking-capable value.
        import re as _re
        from pathlib import Path as _P
        units=_P(__file__).resolve().parents[1]/"roles"/"units"
        blocking={"issues","changes-required","blocked","fail","failed","error",
                  "partial","reject","rejected","memos-added"}
        registered=self.r["auxiliary_check_units"]
        self.assertEqual(sorted(registered),
            ["assumption-check","edge-case-check","failure-mode-check",
             "simplicity-check","test-gap-check"])
        for check,unit in sorted(registered.items()):
            with self.subTest(auxiliary_check=check):
                text=(units/f"{unit}.md").read_text(encoding="utf-8")
                found=_re.search(r"^  verdict:\s*\[([^\]]*)\]",text,_re.MULTILINE)
                self.assertIsNotNone(found,f"{unit} declares no verdict enum")
                values={token.strip() for token in found.group(1).split(",") if token.strip()}
                self.assertEqual(values,{"findings","none"})
                self.assertFalse(values & blocking)
        for arbiter in ("qa/plan-review","research/plan-review"):
            text=(units/f"{arbiter}.md").read_text(encoding="utf-8")
            found=_re.search(r"^  verdict:\s*\[([^\]]*)\]",text,_re.MULTILINE)
            values={token.strip() for token in found.group(1).split(",") if token.strip()}
            self.assertTrue(values & blocking,f"{arbiter} carries no blocking verdict")
    def test_leg_class_stamped_on_realized_nodes(self):
        # The compiler stamps leg_class (and auxiliary_check for auxiliary legs)
        # onto realized nodes in _expand_parallel_groups.
        r=copy.deepcopy(self.r); T.validate_registry(r)
        code=next(x for x in r["recipes"] if x["capability"]=="autopilot-code")
        nodes=json.loads(json.dumps(code["standard_plus"]["nodes"]))
        from pathlib import Path as _P
        _S=importlib.util.spec_from_file_location("capability_route",_P(__file__).resolve().parents[1]/"utilities"/"capability-route.py")
        _CR=importlib.util.module_from_spec(_S); _S.loader.exec_module(_CR)
        nodes=_CR._expand_parallel_groups(nodes,code["standard_plus"]["parallel_groups"],"strong",
            code["capability"],
            auxiliary_check_units=r.get("auxiliary_check_units"))
        stamped={n["id"]:n.get("leg_class") for n in nodes if "parallel_group" in n}
        self.assertEqual(stamped["plan"],"peer")
        self.assertEqual(stamped["plan-alternative"],"peer")
        for group in code["standard_plus"]["parallel_groups"]:
            for leg in group["legs"]:
                self.assertIn(leg["leg_class"],("peer","auxiliary"))
        aux_nodes=_CR._expand_parallel_groups(
            json.loads(json.dumps(code["standard_plus"]["nodes"])),
            code["standard_plus"]["parallel_groups"], "thorough", code["capability"],
            auxiliary_check_units=r.get("auxiliary_check_units"))
        aux_nodes={n["id"]:n for n in aux_nodes if n.get("leg_class")=="auxiliary"}
        self.assertEqual(aux_nodes["plan-check-simplicity"]["auxiliary_check"],"simplicity-check")
        self.assertEqual(aux_nodes["plan-check-simplicity"]["unit"],"qa/simplicity-check")
        self.assertEqual(aux_nodes["plan-check-simplicity"]["role"],"fast reviewer")
    def test_owner_profile_policy_and_semantic_owner_census(self):
        self.assertEqual(self.r["owner_profile_by_intensity"], {
            "quick": "balanced-deep", "standard": "deep", "strong": "deep",
            "thorough": "deep", "adversarial": "deep",
        })
        expected = {
            ("autopilot-apply", ("default",)): ["handback"],
            ("autopilot-lab", ("eval",)): ["publish", "sync"],
            ("autopilot-lab", ("setup",)): ["handoff"],
            ("autopilot-refine", ("default",)): ["transaction"],
            ("autopilot-ship", ("default",)): ["release-setup", "deploy"],
            ("autopilot-spec", ("api", "app", "cli", "library", "research", "update")): [
                "prd-transaction"
            ],
        }
        observed = {}
        for recipe in self.r["recipes"]:
            key = (recipe["capability"], tuple(recipe["modes"]))
            self.assertEqual(recipe["quick"]["model_profile"], "balanced-deep")
            owners = [
                node for node in recipe["standard_plus"]["nodes"]
                if node.get("kind") == "capability-owner"
                and node.get("unit") == "_kernel/owner"
            ]
            if owners:
                observed[key] = [node["id"] for node in owners]
            for node in owners:
                self.assertEqual(node["dispatch_depth"], 1)
                self.assertEqual(node["model_profile"], "deep")
                self.assertEqual(node["role"], "deep orchestrator")
        self.assertEqual(observed, expected)
    def test_owner_profile_policy_drift_fails_closed(self):
        r=copy.deepcopy(self.r); r["recipes"][0]["quick"]["model_profile"]="light"
        self.assertRaisesRegex(T.TopologyError,"must match",T.validate_registry,r)
        r=copy.deepcopy(self.r); r["owner_profile_by_intensity"]["strong"]="balanced-deep"
        self.assertRaisesRegex(T.TopologyError,"must be uniform",T.validate_registry,r)
        r=copy.deepcopy(self.r); owner=r["recipes"][0]["standard_plus"]["nodes"][2]
        owner["model_profile"]="balanced-deep"
        self.assertRaisesRegex(T.TopologyError,"semantic capability owner",T.validate_registry,r)
        r=copy.deepcopy(self.r); owner=r["recipes"][0]["standard_plus"]["nodes"][2]
        owner["dispatch_depth"]=2
        self.assertRaisesRegex(T.TopologyError,"semantic capability owner",T.validate_registry,r)
        r=copy.deepcopy(self.r); owner=r["recipes"][0]["standard_plus"]["nodes"][2]
        owner["role"]="deep maker"
        self.assertRaisesRegex(T.TopologyError,"reserved unit",T.validate_registry,r)
    def test_non_owner_nodes_and_parallel_groups_match_frozen_full_field_census(self):
        observed = {}
        for recipe in self.r["recipes"]:
            key = (recipe["capability"], tuple(recipe["modes"]))
            non_owners = [
                node for node in recipe["standard_plus"]["nodes"]
                if not (
                    node.get("kind") == "capability-owner"
                    and node.get("unit") == "_kernel/owner"
                )
            ]
            groups = recipe["standard_plus"].get("parallel_groups", [])
            observed[key] = (
                full_field_digest(non_owners),
                full_field_digest(groups),
            )
        self.assertEqual(observed, PRESERVED_FULL_FIELD_DIGESTS)
    def test_gate_contract_missing_entry(self):
        r=copy.deepcopy(self.r); del r["completion_gate_contracts"]["apply-hash"]
        self.assertRaisesRegex(T.TopologyError,"completion_gate_contracts entry",T.validate_registry,r)
        r=copy.deepcopy(self.r); r["completion_gate_contracts"]["apply-verify"]["unit"]="qa/test"
        self.assertRaisesRegex(T.TopologyError,"carrying node's unit",T.validate_registry,r)
    # regression ①: every recipe's write_scope/outputs must classify into a
    # declared artifact_buckets anchor; a new top-level directory scope, a
    # recipe with no artifact_scope, and a parallel leg that escapes its
    # anchor must all fail closed.
    def test_bucket_anchor_rejects_top_level_directory_escape(self):
        r=copy.deepcopy(self.r)
        d=next(x for x in r["recipes"] if x["capability"]=="autopilot-design")
        next(n for n in d["standard_plus"]["nodes"] if n["id"]=="build")["write_scope"]=["design/**","tokens/**","components/**"]
        self.assertRaisesRegex(T.TopologyError,"matches no declared cycle_anchor",T.validate_registry,r)
        r=copy.deepcopy(self.r)
        s=next(x for x in r["recipes"] if x["capability"]=="autopilot-spec")
        next(n for n in s["standard_plus"]["nodes"] if n["id"]=="review")["write_scope"]=["reviews/spec/**"]
        self.assertRaisesRegex(T.TopologyError,"matches no declared cycle_anchor",T.validate_registry,r)
    def test_bucket_anchor_requires_artifact_scope_on_every_recipe(self):
        r=copy.deepcopy(self.r); del r["recipes"][0]["artifact_scope"]
        self.assertRaisesRegex(T.TopologyError,"artifact_scope required",T.validate_registry,r)
    def test_bucket_anchor_rejects_missing_artifact_buckets_table(self):
        r=copy.deepcopy(self.r); del r["artifact_buckets"]
        self.assertRaisesRegex(T.TopologyError,"artifact_buckets",T.validate_registry,r)
    def test_bucket_anchor_rejects_parallel_leg_escaping_a_root_anchor(self):
        # `_parallel_path` expansion is exercised directly against
        # `_validate_bucket_anchor` (rather than through the full registry,
        # whose other nodes would need matching artifact_scope changes too):
        # a root_anchor is a fixed, wildcard-free literal, so a leg suffix
        # that pushes it outside that literal prefix is a real, detectable
        # escape -- the concrete shape D-6 regression ① guards after
        # `_parallel_path` expansion.
        recipe = {"capability": "fixture-cap", "artifact_scope": {
            "root_anchors": ["analysis_project/code"],
        }}
        registry = {"artifact_buckets": {"analysis": "analysis_project"}}
        base_scope = "analysis_project/code/**"
        expanded = [T._parallel_path(base_scope, "alternative")]
        self.assertEqual(expanded, ["analysis_project/code-alternative/**"])
        T._validate_bucket_anchor(recipe, registry, [base_scope], None, "node")  # base scope is fine
        self.assertRaisesRegex(
            T.TopologyError, "does not classify",
            T._validate_bucket_anchor, recipe, registry, expanded, None, "node-alternative",
        )
    def test_bucket_anchor_target_relative_recipes_pass_with_bare_scopes(self):
        # advisory-3: a positive fixture for the `target_relative` domain (ship,
        # apply, refine) so the classifier's fallback path is exercised, not
        # just the failure paths above.
        for capability in ("autopilot-apply", "autopilot-refine", "autopilot-ship"):
            with self.subTest(capability=capability):
                recipe = next(x for x in self.r["recipes"] if x["capability"] == capability)
                self.assertTrue(recipe["artifact_scope"].get("target_relative"))
                T.validate_registry(copy.deepcopy(self.r))
    def test_bucket_anchor_implicit_mode_rejects_reserved_top_segments(self):
        # F2: `anchor_mode: implicit` recipes had no negative check at all --
        # any bare scope that spelled a top-level pollution name used by a
        # different track's literal anchor (`reviews/`, `handoff/`, ...) was
        # accepted unexamined. These are the exact injections the review
        # measured as [ACCEPTED] before this fix.
        r=copy.deepcopy(self.r)
        code=next(x for x in r["recipes"] if x["capability"]=="autopilot-code")
        execute=next(n for n in code["standard_plus"]["nodes"] if n["id"]=="execute")
        execute["write_scope"]=["reviews/visual/**","dev_logs/**"]
        self.assertRaisesRegex(T.TopologyError,"reserved top-level segment",T.validate_registry,r)
        r=copy.deepcopy(self.r)
        draft=next(x for x in r["recipes"] if x["capability"]=="autopilot-draft")
        draft["quick"]["write_scope"]=["handoff/**"]
        self.assertRaisesRegex(T.TopologyError,"reserved top-level segment",T.validate_registry,r)
        # a recipe's own map_anchor/review_anchor top segment stays legal (draft
        # legitimately owns a top-level `reviews/` segment).
        r=copy.deepcopy(self.r)
        draft=next(x for x in r["recipes"] if x["capability"]=="autopilot-draft")
        self.assertEqual(draft["artifact_scope"]["review_anchor"],"reviews")
        T.validate_registry(r)
    def test_bucket_anchor_checks_path_shaped_outputs(self):
        # F3: `outputs` carried no anchor check at all -- write_scope and
        # outputs could silently diverge, and outputs feeds compiled `inputs`
        # for downstream nodes (F9), so a top-level escape there is just as
        # real as one in write_scope.
        r=copy.deepcopy(self.r)
        d=next(x for x in r["recipes"] if x["capability"]=="autopilot-design")
        refs=next(n for n in d["standard_plus"]["nodes"] if n["id"]=="refs")
        refs["outputs"]=["reviews/visual/x.json"]
        self.assertRaisesRegex(T.TopologyError,"matches no declared cycle_anchor",T.validate_registry,r)
        # bare symbol tokens (no "/") are compiler-substituted elsewhere and
        # must not be routed through the path-anchor check.
        r=copy.deepcopy(self.r)
        d=next(x for x in r["recipes"] if x["capability"]=="autopilot-design")
        build=next(n for n in d["standard_plus"]["nodes"] if n["id"]=="build")
        self.assertIn("tokens",build["outputs"])
        T.validate_registry(r)
    def test_bucket_anchor_rejects_parallel_leg_exhausting_the_cycle_wildcard(self):
        # F4: `_parallel_path` suffixing a scope that is exactly `<cycle>/**`
        # (no subdirectory) appends the leg suffix directly onto the cycle
        # placeholder (`designs/<cycle>-alternative/**`), which resolves to a
        # sibling *cycle*, not a sibling subdirectory inside the owning cycle.
        # This must be rejected even though the un-expanded base scope
        # (`designs/<cycle>/**`, tail == "") is a legitimate write to the
        # cycle root itself.
        recipe = {"capability": "fixture-cap", "artifact_scope": {
            "cycle_anchors": ["designs/<cycle>"], "anchor_mode": "literal",
        }}
        registry = {"artifact_buckets": {"designs": "designs/<cycle>"}}
        base_scope = "designs/<cycle>/**"
        expanded = [T._parallel_path(base_scope, "alternative")]
        self.assertEqual(expanded, ["designs/<cycle>-alternative/**"])
        T._validate_bucket_anchor(recipe, registry, [base_scope], None, "node")  # base tail=="" is fine
        self.assertRaisesRegex(
            T.TopologyError, "exhausts its anchor",
            T._validate_bucket_anchor, recipe, registry, expanded, None, "node-alternative",
            require_anchor_tail=True,
        )
    def test_bucket_anchor_real_design_leg_expansion_still_validates(self):
        # Positive companion to the fixture above: the actual registry's
        # design parallel legs (suffixing a leaf directory, not the cycle
        # placeholder) must keep validating after the F4 fix.
        T.validate_registry(copy.deepcopy(self.r))
    def test_bucket_anchor_parallel_leg_recheck_call_site_requires_anchor_tail(self):
        # No live registry recipe can reach `tail == ""` through the real
        # call site (every map/review-worker parallel target already forces a
        # non-empty tail via its own map_anchor/review_anchor containment
        # check, before the leg re-check ever runs) -- so the only way to
        # prove `_validate_recipe`'s actual parallel-leg call site (not just
        # the fixture above) is wired with `require_anchor_tail=True` is to
        # observe the live call, not construct a registry that trips it.
        calls = []
        original = T._validate_bucket_anchor
        def spy(*args, **kwargs):
            calls.append(kwargs.get("require_anchor_tail", False))
            return original(*args, **kwargs)
        T._validate_bucket_anchor = spy
        try:
            T.validate_registry(copy.deepcopy(self.r))
        finally:
            T._validate_bucket_anchor = original
        self.assertIn(True, calls)
    def test_bucket_anchor_rejects_undeclared_cycle_anchor(self):
        # F10: a cycle_anchor must itself be a declared artifact_buckets value,
        # exactly like root_anchors already required -- otherwise a recipe can
        # declare an anchor for a bucket nobody registered.
        r=copy.deepcopy(self.r)
        spec=next(x for x in r["recipes"] if x["capability"]=="autopilot-spec")
        spec["artifact_scope"]["cycle_anchors"]=["rogue/<cycle>"]
        self.assertRaisesRegex(T.TopologyError,"not a declared artifact bucket",T.validate_registry,r)
    def test_bucket_anchor_component_spec_resolves_without_swallowing_internal(self):
        # F10: restoring `spec/<component>` must not let the `<component>`
        # wildcard absorb the reserved `_internal` segment (which would make
        # the flat `spec/_internal/**` form indistinguishable from a component
        # path and break map/review containment for the common flat case).
        spec=next(x for x in self.r["recipes"] if x["capability"]=="autopilot-spec")
        self.assertEqual(spec["artifact_scope"]["cycle_anchors"], ["spec", "spec/<component>"])
        T._validate_bucket_anchor(
            spec, self.r, ["spec/mycomp/_internal/research/**"], "map-worker", "research"
        )
        T._validate_bucket_anchor(
            spec, self.r, ["spec/_internal/research/**"], "map-worker", "research"
        )
    def test_unit_choices_membership(self):
        r=copy.deepcopy(self.r); code=next(x for x in r["recipes"] if x["capability"]=="autopilot-code")
        execute=next(n for n in code["standard_plus"]["nodes"] if n["id"]=="execute")
        execute["unit_choices"]=[c for c in execute["unit_choices"] if c!=execute["unit"]]
        self.assertRaisesRegex(T.TopologyError,"unit_choices",T.validate_registry,r)

if __name__ == "__main__": unittest.main()
