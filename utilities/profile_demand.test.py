#!/usr/bin/env python3
"""SD-88 behavioral regressions; fixtures never launch a model."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import model_profile as P
import model_config as C
import replica_batch_contract as B


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

F = load("route_fixtures", "capability_route.test.py")
R = F.R
A = load("adhoc", "compose-route.py")
D = load("defaults", "dispatch-defaults.py")


def demand(judgment="predetermined", scope="short-local", **extra):
    return {"schema_version": 1, "judgment_requirement": judgment,
            "execution_scope": scope, "judgment_reason": "Approved decision recorded in the task.",
            "execution_reason": "Execute the declared fixture steps.",
            "evidence_refs": ["decision.md"], **extra}


class DemandSchema(unittest.TestCase):
    def test_six_cells_and_exact_floor_schema(self):
        for judgment, profiles, floor in (("predetermined", ("light", "balanced"), "none"),
                ("important", ("balanced-deep",)*2, "balanced-deep"),
                ("difficult-uncertain", ("deep",)*2, "deep")):
            for scope, profile in zip(P.DEMAND_SCOPES, profiles):
                row = P.resolve_profile_demand(demand(judgment, scope))
                self.assertEqual(row["resolved_profile"], profile)
                self.assertEqual(row["judgment_floor"], floor)
                P.validate_profile_selection(row, demand(judgment, scope), profile=profile)
                for field, value in (("reason", "forged"), ("judgment_floor", "unknown"),
                                     ("resolver_version", "future"), ("demand_digest", "sha256:bad")):
                    with self.subTest(field=field), self.assertRaises(P.ModelProfileError):
                        P.validate_profile_selection({**row, field: value}, demand(judgment, scope))

    def test_invalid_partial_inputs_cannot_become_legacy(self):
        invalid = [None, {}, {"schema_version": 1}, demand(schema_version=True),
                   demand("unknown"), demand(scope="unknown"), demand(judgment_reason=" "),
                   demand(execution_reason=""), demand(evidence_refs=[]), demand(evidence_refs=[1])]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(P.ModelProfileError):
                P.resolve_profile_demand(value)
        for value in (None, demand(), demand("important"), demand("difficult-uncertain")):
            for profile in P.KNOWN_PROFILES:
                row = P.resolve_profile_demand(value, explicit_profile=profile)
                self.assertEqual(row["resolved_profile"], profile)
                P.validate_profile_selection(row, value, profile=profile)
        with self.assertRaises(P.ModelProfileError):
            P.resolve_profile_demand(demand(), explicit_profile="unknown")
        row = P.resolve_profile_demand(None, explicit_profile="light", legacy=True, existing_versioned_stage=True)
        self.assertEqual(row["resolved_profile"], "light")
        self.assertIsNone(row["demand_digest"])
        self.assertEqual(row["reason"], "unannotated-existing-stage")

    def test_judgment_execution_handoff(self):
        decision = {"decision": "approved replacement", "evidence": "review.md",
                    "approved_scope": ["module-a", "module-b"], "stop_conditions": ["new policy choice"]}
        stages = [demand("important"), demand(scope="extended-multistep", evidence_refs=["decision.md"]),
                  demand("difficult-uncertain", evidence_refs=["new-policy-choice.md"]),
                  demand(evidence_refs=["redecision.md"])]
        self.assertTrue(decision["stop_conditions"])
        self.assertEqual([P.resolve_profile_demand(d)["resolved_profile"] for d in stages],
                         ["balanced-deep", "balanced", "deep", "light"])
        self.assertEqual(P.resolve_profile_demand(stages[2], explicit_profile="balanced")["resolved_profile"], "balanced")

    def test_user_whole_file_derivation_and_no_writeback(self):
        for adapter in C.ADAPTERS:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td); user = root / "agent-config/models.conf"; user.parent.mkdir()
                shipped = C.parse_config(C.shipped_path(adapter))
                original = {k:v for k,v in shipped.items() if k not in
                            {"CFG_MODEL_PROFILE_BALANCED", "CFG_MODEL_PROFILE_GRANULARITY_BALANCED"}}
                original["CFG_MODEL_PROFILE_LIGHT"] = "custom:medium" if adapter != "opencode" else "custom:budget-user"
                original["CFG_TIER_CUSTOM_MODEL"] = "my-model"
                original["CFG_TIER_CUSTOM_" + ("VARIANT" if adapter == "opencode" else "EFFORT")] = "different-tier-default"
                original["CFG_USER_CATALOG"] = "private-catalog"
                def write(values):
                    user.write_text("".join(k + "=" + json.dumps(v) + "\n" for k,v in values.items()))
                write(original); before=(user.read_bytes(), user.stat().st_mtime_ns)
                selected, receipt = C.resolve_config(adapter, runtime=root)
                self.assertEqual(receipt.source, "user")
                self.assertEqual(receipt.balanced_provenance, "derived-from-user-light")
                for key, value in original.items(): self.assertEqual(selected[key], value)
                point=P.resolve_profile_values(adapter, selected, "balanced")
                self.assertEqual(point["model"], "my-model")
                self.assertEqual(point["budget"], "budget-user" if adapter == "opencode" else "high")
                if adapter == "opencode": self.assertEqual(point["granularity"], "collapsed-balanced-to-light")
                self.assertEqual(before,(user.read_bytes(),user.stat().st_mtime_ns))
                explicit={**original,"CFG_MODEL_PROFILE_BALANCED":"custom:explicit-budget"}
                write(explicit); selected, receipt=C.resolve_config(adapter,runtime=root)
                self.assertEqual(receipt.balanced_provenance,"explicit")
                self.assertEqual(P.resolve_profile_values(adapter,selected,"balanced")["budget"],"explicit-budget")
                broken=dict(original); del broken["CFG_TIER_DEEP_MODEL"]
                write(broken); selected,receipt=C.resolve_config(adapter,runtime=root)
                self.assertEqual(receipt.source,"shipped")
                self.assertEqual(selected,shipped)

    def test_user_policy_inheritance_explicit_and_invalid(self):
        config={"schema_version":3,"harnesses":{"enabled":["codex"]},
                "profiles":{name:{"primary":["codex"],"relief":[],"last_resort":[],
                                   "promote_relief_below":37} for name in ("deep","balanced-deep","light","mini")},
                "allocation":{"strategy":"balanced","window":30},"capabilities":{}}
        capmap=D.load_topology_capabilities(D.default_topology_path())
        self.assertEqual(D.validate(config,capmap),[])
        before=copy.deepcopy(config)
        self.assertEqual(D.query_profile_policy(config,"balanced"),D.query_profile_policy(config,"light"))
        self.assertEqual(config,before)
        config["profiles"]["balanced"]={**config["profiles"]["light"],"promote_relief_below":11}
        self.assertEqual(D.validate(config,capmap),[])
        self.assertEqual(D.query_profile_policy(config,"balanced")["promote_relief_below"],11)
        for value in (None, {}, {**config["profiles"]["balanced"], "primary":["claude"]}):
            config["profiles"]["balanced"]=value
            self.assertTrue(D.validate(config,capmap))

    def test_batch_selection_pair_seal_and_legacy_digest(self):
        members=[{"assignment_sha256":"sha256:"+"a"*64,"attempt_id":f"att-{i}",
            "route_node":f"review-{i}","harness":h,"fallback_hop":"same-harness-headless",
            "fallback_ordinal":1,"model_profile":"light","perspective":f"perspective-{i}",
            "parallel_leg_index":i,"leg_class":"peer"} for i,h in enumerate(("codex","claude"))]
        def build(rows):
            return B.build_manifest(parallel_group="review",route_id="rt-fixture",
                parent_attempt_id="att-parent",independence="cross-harness",members=rows,
                realized_independence_axes=["cross-harness","perspective"])
        legacy,digest,_=build(members); before=json.dumps(legacy,sort_keys=True)
        self.assertEqual(B.verify_manifest(legacy)[1],digest)
        self.assertEqual(json.dumps(legacy,sort_keys=True),before)
        annotated=copy.deepcopy(members)
        for row in annotated:
            row["profile_demand"]=demand()
            row["profile_selection"]=P.resolve_profile_demand(row["profile_demand"])
        sealed,new_digest,_=build(annotated)
        self.assertNotEqual(digest,new_digest)
        self.assertEqual(B.verify_manifest(sealed)[1],new_digest)
        for broken in ("resolver_version","demand_digest","judgment_floor"):
            forged=copy.deepcopy(sealed);forged["members"][0]["profile_selection"][broken]="forged"
            with self.assertRaises(B.ReplicaBatchContractError): B.verify_manifest(forged)
        del annotated[0]["profile_demand"]
        with self.assertRaises(B.ReplicaBatchContractError): build(annotated)


class RouteDemand(unittest.TestCase):
    setUp=F.TestRoute.setUp
    _restore_agent_home=F.TestRoute._restore_agent_home
    args=F.TestRoute.args
    dispatch=F.TestRoute.dispatch
    nested=F.TestRoute.nested

    def compile(self, **kwargs):
        return R.compile_route(**self.args(requested_intensity="standard", predicates=[],
            signals=["shared-contract"], transport="headless", inline_reason=None,
            dispatch_evidence=self.dispatch(self.nested()), **kwargs))

    def test_compile_compose_same_resolver_and_hash_sensitivity(self):
        demands={"execute":demand("important")}
        route=self.compile(profile_demands=demands)
        R.verify_route(route,R.ROOT)
        # Both frame legs raise `frame-review`, and compose emits one binding
        # per raising node, so a graph naming both is refused as a gate bound
        # twice. This test is about the resolver, not the graph, so it composes
        # the widest graph compose can currently express.
        graph=",".join(n["id"] for n in R.TOPO.resolve_recipe(R.TOPO.load_registry(),"autopilot-code","dev")["standard_plus"]["nodes"]
                       if n["id"]!="frame-alternative")
        composed=R.compose_route(capability="autopilot-code",capability_mode="dev",shape="staged",graph=graph,
            slug="sd88",cwd=R.ROOT,artifact_root=R.ROOT, spec_read="fixture",profile_demands=demands,
            dispatch_evidence=self.dispatch(self.nested()))
        R.verify_route(composed,R.ROOT)
        pick=lambda r:next(n for n in r["nodes"] if n["id"]=="execute")
        self.assertEqual(pick(route)["profile_selection"],pick(composed)["profile_selection"])
        for changed in (demand("important","extended-multistep"), demand("important",judgment_reason="A different important decision."), demand("important",execution_reason="A different approved procedure."), demand("important",evidence_refs=["another-decision.md"])):
            other=self.compile(profile_demands={"execute":changed})
            self.assertEqual(pick(other)["model_profile"],"balanced-deep")
            self.assertNotEqual(other["route_hash"],route["route_hash"])
        self.assertEqual(route["owner_profile_selection"]["source"],"legacy")

    def test_owner_floor_unknown_target_and_partial_reject(self):
        for demands in ({"__owner__":{}},{"execute":{}},{"absent":demand()}):
            with self.assertRaises(ValueError): self.compile(profile_demands=demands)
        route = self.compile(profile_demands={"execute":demand("important")},explicit_profiles={"execute":"light"})
        self.assertEqual(next(n for n in route["nodes"] if n["id"] == "execute")["model_profile"], "light")
        R.verify_route(route, R.ROOT)

    def test_tamper_rejected_before_wrapper_spawn_and_legacy_hash_stays(self):
        route=self.compile(profile_demands={"execute":demand("important")})
        for field,value in (("judgment_floor","none"),("resolver_version","unsupported"),("reason","forged")):
            other=copy.deepcopy(route); node=next(n for n in other["nodes"] if n["id"]=="execute")
            node["profile_selection"][field]=value
            other["route_hash"]=R.route_hash(other);other["route_id"]="rt-"+other["route_hash"].split(":")[1][:16]
            with self.assertRaises(ValueError): R.verify_route(other,R.ROOT)
        legacy=copy.deepcopy(route)
        for key in ("profile_selection_contract_version","profile_demands","explicit_profiles","owner_profile_selection","owner_profile_demand"):
            legacy.pop(key,None)
        for node in legacy["nodes"]:
            node.pop("profile_selection",None);node.pop("profile_demand",None)
        legacy["route_hash"]=R.route_hash(legacy);legacy["route_id"]="rt-"+legacy["route_hash"].split(":")[1][:16]
        before=json.dumps(legacy,sort_keys=True)
        R.verify_route(legacy,R.ROOT)
        self.assertEqual(json.dumps(legacy,sort_keys=True),before)

    def test_ad_hoc_requires_full_annotation(self):
        units=[{"id":"write","unit":"editorial/report","write_scope":["pipeline_summary.md"],"gate":"code-report"}]
        kwargs=dict(topology_class="staged",quick_write_scope=[],quick_model_profile="balanced-deep",
                    gate_index=A.unit_io_gate_index(R.TOPO.load_registry()),cycle_anchors=["plans/<cycle>"])
        with self.assertRaisesRegex(ValueError,"profile-demand-required"):
            A.build_recipe("autopilot-code","dev",units,**kwargs)
        units[0]["profile_demand"]=demand(scope="extended-multistep")
        recipe=A.build_recipe("autopilot-code","dev",units,**kwargs)
        route=R.compile_composed_route(recipe,"dev","standard",R.ROOT,R.ROOT,
                tracked_gate_evidence=self.args()["tracked_gate_evidence"],
                dispatch_evidence=self.dispatch(self.nested()))
        self.assertEqual(route["nodes"][0]["model_profile"],"balanced")
        R.verify_route(route,R.ROOT)

    def test_forged_subgraph_marker_cannot_grant_legacy(self):
        recipe=copy.deepcopy(R.TOPO.resolve_recipe(R.TOPO.load_registry(),"autopilot-code","dev"))
        recipe["compose"]={"base_capability":"autopilot-code","shape":"staged",
                           "graph":["execute"],"unit_overrides":{}}
        self.assertFalse(R._versioned_subgraph(R.TOPO.load_registry(),recipe))
        with self.assertRaises(P.ModelProfileError):
            R.compile_composed_route(recipe,"dev","standard",R.ROOT,R.ROOT,
                tracked_gate_evidence=self.args()["tracked_gate_evidence"],
                dispatch_evidence=self.dispatch(self.nested()))

    def test_maps_and_declared_selection_cannot_disagree(self):
        route=self.compile(profile_demands={"execute":demand("important")})
        mutations=[lambda r:r["profile_demands"].update(execute=demand("difficult-uncertain")),
                   lambda r:r["explicit_profiles"].update(execute="deep"),
                   lambda r:r.update(owner_profile_demand=demand("difficult-uncertain"))]
        for mutate in mutations:
            forged=copy.deepcopy(route);mutate(forged)
            forged["route_hash"]=R.route_hash(forged)
            forged["route_id"]="rt-"+forged["route_hash"].split(":")[1][:16]
            with self.assertRaises(ValueError): R.verify_route(forged,R.ROOT)
        # Removing only the input map also cannot silently replace a versioned stage's annotation.
        forged=copy.deepcopy(route);forged["profile_demands"]={}
        forged["route_hash"]=R.route_hash(forged);forged["route_id"]="rt-"+forged["route_hash"].split(":")[1][:16]
        with self.assertRaisesRegex(ValueError,"node-profile-declaration-mismatch"):
            R.verify_route(forged,R.ROOT)

    def test_real_compile_and_compose_cli_demands(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); evidence=root/"evidence.json"; demands=root/"demands.json"; explicit=root/"explicit.json"
            evidence.write_text(json.dumps(self.dispatch(self.nested())))
            demands.write_text(json.dumps({"execute":demand("important")}))
            explicit.write_text(json.dumps({"execute":"deep"}))
            env={k:v for k,v in os.environ.items() if not k.startswith(
                ("AGENT_DISPATCH_","AGENT_ARTIFACT_","AGENT_OWNER_ROUTE_","AGENT_ROUTE_"))}
            env.update(AGENT_HOME=str(ROOT),XDG_STATE_HOME=str(root/"state"),PYTHONDONTWRITEBYTECODE="1")
            common=["--slug","sd88-cli","--capability","autopilot-code","--capability-mode","dev",
                "--cwd",str(ROOT),"--artifact-root",str(root/"artifacts"),"--spec-read","fixture",
                "--drift-verdict","within-spec","--artifact-guard","fixture",
                "--dispatch-evidence",str(evidence),"--profile-demands",str(demands)]
            for action,extra,expected in (("compile",["--intensity","standard","--signal","shared-contract",
                    "--transport","headless","--tracking","tracked","--workflow-mode","tracked"],"balanced-deep"),
                    ("compose",["--shape","staged","--graph","execute,report","--explicit-profiles",str(explicit)],"deep")):
                result=subprocess.run([sys.executable,str(ROOT/"utilities/capability-route.py"),action,*common,*extra],
                    capture_output=True,text=True,env=env)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                route=json.loads(result.stdout);node=next(n for n in route["nodes"] if n["id"]=="execute")
                self.assertEqual(node["model_profile"],expected)
                self.assertEqual(node["profile_selection"]["source"],"explicit" if action=="compose" else "matrix")
            result = subprocess.run([sys.executable, str(ROOT/"utilities/capability-route.py"), "compose",
                *common[:-2], "--shape", "staged", "--graph", "frame,frame-alternative,test,report",
                "--profile", "light"], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            route = json.loads(result.stdout)
            self.assertEqual(route["owner_model_profile"], "light")
            self.assertEqual({n["model_profile"] for n in route["nodes"]}, {"light"})
            self.assertEqual(route["profile_demands"], {})
            R.verify_route(route, ROOT)

    def test_continuation_keeps_only_remaining_demand_inputs(self):
        fixture=F.TestContinuation();fixture.setUp()
        try:
            with tempfile.TemporaryDirectory() as td:
                original=R.compile_route
                def compile_with_demands(*args, **kwargs):
                    return original(*args, **kwargs, profile_demands={"plan":demand("important"),"test":demand("important")})
                with mock.patch.object(R,"compile_route",side_effect=compile_with_demands):
                    source=fixture._source(Path(td))
                fixture._complete_prefix(source,"test",Path(td)/"evidence")
                continuation=fixture._build(source)
                self.assertEqual(set(continuation["profile_demands"]),{"test"})
                self.assertEqual(set(source["profile_demands"]),{"plan","test"})
                R.verify_route(continuation,R.ROOT)
        finally:
            fixture.doCleanups()



class TopExceptionRoute(unittest.TestCase):
    """The `top` exception profile at the route layer, on real routes: the same
    compile -> verify_route -> compose -> verify_route round trip the portable
    profiles take (review R1 M2), for both shapes that can carry an owner."""

    args = F.TestRoute.args
    registered_headless = F.TestRoute.registered_headless
    dispatch = F.TestRoute.dispatch
    nested = F.TestRoute.nested
    TOP = {"__owner__": "top"}

    def test_owner_demand_replaces_default_for_quick_and_standard(self):
        for judgment, scope, profile in (("predetermined", "short-local", "light"),
                ("predetermined", "extended-multistep", "balanced"),
                ("important", "short-local", "balanced-deep"),
                ("difficult-uncertain", "short-local", "deep")):
            for compile_route in (self.quick, self.staged):
                for explicit in ({}, {"__owner__": profile}):
                    with self.subTest(profile=profile, shape=compile_route.__name__, explicit=explicit):
                        route = compile_route(profile_demands={"__owner__": demand(judgment, scope)},
                                              explicit_profiles=explicit)
                        self.assertEqual(route["owner_model_profile"], profile)
                        R.verify_route(route, R.ROOT)
                        for node in route["nodes"]:
                            if node["id"] == "one-shot":
                                self.assertEqual(node["model_profile"], profile)
                        if profile == "light":
                            self.assertEqual({n["model_profile"] for n in route["nodes"]
                                              if n["unit"] == "plan/frame"}, {"balanced"})

    def test_light_compose_owner_and_semantic_stages_round_trip(self):
        for shape, graph in (("solo", None), ("staged", "plan,plan-check,test,report")):
            route = R.compose_route(capability="autopilot-code", capability_mode="dev", shape=shape,
                graph=graph, slug="light-owner", cwd=R.ROOT, artifact_root=R.ROOT,
                spec_read="fixture", registered_headless_evidence=self.registered_headless(),
                dispatch_evidence=self.dispatch(self.nested()),
                profile_demands={"__owner__": demand()}, explicit_profiles={"__owner__": "light"})
            self.assertEqual(route["owner_model_profile"], "light")
            R.verify_route(route, R.ROOT)
        route = self.staged(capability="autopilot-spec", capability_mode="app",
            profile_demands={"__owner__": demand(), "prd-transaction": demand()},
            explicit_profiles={"__owner__": "light", "prd-transaction": "light"})
        R.verify_route(route, R.ROOT)

    def test_one_profile_option_carries_explicit_light_without_demand_files(self):
        for shape, graph in (("solo", None), ("staged", "frame,frame-alternative,test,report"),
                             ("staged", "test,report")):
            route = R.compose_route(capability="autopilot-code", capability_mode="dev", shape=shape,
                graph=graph, slug="explicit-light", cwd=R.ROOT, artifact_root=R.ROOT,
                spec_read="fixture", registered_headless_evidence=self.registered_headless(),
                dispatch_evidence=self.dispatch(self.nested()), profile="light")
            self.assertEqual(route["owner_model_profile"], "light")
            self.assertEqual({n["model_profile"] for n in route["nodes"]}, {"light"})
            self.assertEqual(route["owner_profile_selection"]["source"], "explicit")
            R.verify_route(route, R.ROOT)
            self.assertNotIn("plan-check", {n["id"] for n in route["nodes"]})

    def test_owner_default_drift_and_quick_dual_selection_are_refused(self):
        route = self.staged()
        route["owner_model_profile"] = "light"
        route["owner_profile_selection"] = P.resolve_profile_demand(
            None, explicit_profile="light", legacy=True, existing_versioned_stage=True)
        route["route_hash"] = R.route_hash(route)
        route["route_id"] = "rt-" + route["route_hash"].split(":")[1][:16]
        with self.assertRaises(ValueError):
            R.verify_route(route, R.ROOT)
        with self.assertRaisesRegex(ValueError, "owner-node-profile-selection-conflict"):
            self.quick(profile_demands={"__owner__": demand(), "one-shot": demand("important")},
                       explicit_profiles={"one-shot": "balanced-deep"})

    def test_inline_demand_does_not_invent_an_owner_or_break_verification(self):
        route = R.compose_route(capability="autopilot-code", capability_mode="dev", shape="direct",
            graph=None, slug="inline-demand", cwd=R.ROOT, artifact_root=R.ROOT, spec_read="fixture",
            profile_demands={"__owner__": demand("important")})
        self.assertIsNone(route["owner_model_profile"])
        R.verify_route(route, R.ROOT)

    def owner_demand(self, judgment="difficult-uncertain"):
        return {"__owner__": demand(judgment)}

    def quick(self, **kw):
        return R.compile_route(**self.args(predicates=[], transport=None, inline_reason=None,
            registered_headless_evidence=self.registered_headless(), **kw))

    def staged(self, **kw):
        return R.compile_route(**self.args(requested_intensity="standard", predicates=[],
            signals=["shared-contract"], transport="headless", inline_reason=None,
            dispatch_evidence=self.dispatch(self.nested()), **kw))

    def assert_top_owner(self, route):
        self.assertEqual(route["owner_model_profile"], "top")
        selection = route["owner_profile_selection"]
        self.assertEqual((selection["source"], selection["resolved_profile"], selection["reason"]),
                         ("explicit", "top", "explicit-top-exception"))
        R.verify_route(route, R.ROOT)

    def test_quick_route_seals_top_on_owner_and_node_and_verifies(self):
        route = self.quick(profile_demands=self.owner_demand(), explicit_profiles=self.TOP)
        self.assert_top_owner(route)
        node = next(n for n in route["nodes"] if n["id"] == "one-shot")
        self.assertEqual(node["model_profile"], "top")
        self.assertEqual(node["profile_selection"], route["owner_profile_selection"])
        # a plain quick route is untouched
        plain = self.quick()
        self.assertEqual(plain["owner_model_profile"], "balanced-deep")
        self.assertEqual(next(n for n in plain["nodes"] if n["id"] == "one-shot")["model_profile"], "balanced-deep")
        R.verify_route(plain, R.ROOT)
        # a route that claims top for the owner but not the node is refused by verify
        forged = json.loads(json.dumps(route))
        next(n for n in forged["nodes"] if n["id"] == "one-shot")["model_profile"] = "balanced-deep"
        forged["route_hash"] = R.route_hash(forged)  # re-sealed, so the hash check is not what refuses
        forged["route_id"] = "rt-" + forged["route_hash"].split(":", 1)[1][:16]
        # the profile-contract check (selection vs node) is muted so the
        # owner-node equality check is the one under test (review R2 M1)
        with mock.patch.object(R, "_verify_profile_contract"), \
             self.assertRaisesRegex(ValueError, "owner node one-shot profile differs from owner_model_profile"):
            R.verify_route(forged, R.ROOT)

    def test_staged_route_seals_top_on_the_owner_only_and_verifies(self):
        route = self.staged(profile_demands=self.owner_demand("important"), explicit_profiles=self.TOP)
        self.assert_top_owner(route)
        # The invariant is that a `top` OWNER does not spread `top` onto the
        # recipe's stage nodes. The frame anchor is the one deliberate
        # exception and is not an instance of that spreading at all: it is
        # raised by the frame tier ladder, which keys on the owner's resolved
        # profile rather than copying it. Everything else must still be off
        # `top`.
        self.assertNotIn("top", {n["model_profile"] for n in route["nodes"]
                                 if n["id"] != "frame"})
        plain = self.staged()
        self.assertEqual(plain["owner_model_profile"], "deep")
        R.verify_route(plain, R.ROOT)
        # Proof the anchor's `top` comes from the ladder and not from the
        # owner: a plain staged route asked for no `top` anywhere, its owner is
        # `deep`, and the anchor is `top` regardless -- while the alternative
        # leg stays at the owner's own working tier.
        by_id = {n["id"]: n for n in plain["nodes"]}
        self.assertEqual(by_id["frame"]["model_profile"], "top")
        self.assertEqual(by_id["frame-alternative"]["model_profile"], "deep")
        self.assertNotIn("top", {n["model_profile"] for n in plain["nodes"]
                                 if n["id"] != "frame"})

    def test_a_recipe_with_depth_one_stage_nodes_keeps_them_off_top(self):
        # Review R2 B1: autopilot-spec's `prd-transaction` (and refine's
        # `transaction`, apply's `handback`, ...) are depth-1 `_kernel/owner`
        # STAGE nodes, not the owner. A `top` owner must not spread onto
        # them, and the route must still verify.
        for capability, mode, stage in (("autopilot-spec", "app", "prd-transaction"),
                                        ("autopilot-refine", "default", "transaction"),
                                        ("autopilot-apply", "default", "handback")):
            with self.subTest(capability=capability):
                route = self.staged(capability=capability, capability_mode=mode,
                                    profile_demands=self.owner_demand(), explicit_profiles=self.TOP)
                self.assert_top_owner(route)
                node = next(n for n in route["nodes"] if n["id"] == stage)
                self.assertEqual((node["dispatch_depth"], node["unit"]), (1, "_kernel/owner"))
                plain = self.staged(capability=capability, capability_mode=mode)
                self.assertEqual(node["model_profile"],
                                 next(n for n in plain["nodes"] if n["id"] == stage)["model_profile"])
                self.assertNotEqual(node["model_profile"], "top")

    def test_compose_takes_the_same_round_trip(self):
        composed = R.compose_route(capability="autopilot-code", capability_mode="dev", shape="solo",
            graph=None, slug="top-solo", cwd=R.ROOT, artifact_root=R.ROOT, spec_read="fixture",
            profile_demands=self.owner_demand(), explicit_profiles=self.TOP,
            registered_headless_evidence=self.registered_headless())
        self.assert_top_owner(composed)
        self.assertEqual(next(n for n in composed["nodes"] if n["id"] == "one-shot")["model_profile"], "top")
        with self.assertRaises(ValueError) as refused:
            R.compose_route(capability="autopilot-code", capability_mode="dev", shape="direct",
                graph=None, slug="top-direct", cwd=R.ROOT, artifact_root=R.ROOT, spec_read="fixture",
                profile_demands=self.owner_demand(), explicit_profiles=self.TOP)
        self.assertEqual(str(refused.exception), "owner-profile-top-requires-owner")

    def test_the_owner_selector_reads_top_from_a_real_route_file(self):
        route = self.quick(profile_demands=self.owner_demand(), explicit_profiles=self.TOP)
        O = load("owner_under_test", "dispatch-owner.py")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "route.json"
            path.write_text(json.dumps(route), encoding="utf-8")
            # the fixture route carries no slug; an explicit flag wins and the route fills the rest
            _, values, forwarded, _, derived = O._parse(["--start", "--route-evidence", str(path),
                                                        "--slug", "top-review", "--prompt-file", "/p.md"])
        self.assertEqual(values["--model-profile"], "top")
        self.assertEqual(values["--intensity"], "quick")
        self.assertIn("--model-profile", derived)
        self.assertEqual(forwarded[forwarded.index("--model-profile") + 1], "top")

    def test_explicit_top_is_accepted_for_the_owner_only(self):
        nodes = [{"id": "execute", "kind": "pipeline-stage", "dispatch_depth": 2, "model_profile": "light"}]
        demands = {"__owner__": demand("important"), "execute": demand("important")}
        _, explicit = R._profile_input_maps(nodes, demands, self.TOP)
        self.assertEqual(explicit, self.TOP)
        with self.assertRaises(ValueError) as refused:
            R._profile_input_maps(nodes, demands, {"execute": "top"})
        self.assertEqual(str(refused.exception), "profile-explicit-top-owner-only:execute")
        route = self.staged(profile_demands=self.owner_demand("predetermined"), explicit_profiles=self.TOP)
        self.assertEqual(route["owner_model_profile"], "top")
        R.verify_route(route, R.ROOT)
        with self.assertRaisesRegex(ValueError, "profile-explicit-input-invalid:__owner__"):
            R._profile_input_maps(nodes, demands, {"__owner__": "summit"})


if __name__ == "__main__":
    unittest.main()
