#!/usr/bin/env python3
import contextlib, hashlib, importlib.util, io, json, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

P=Path(__file__).with_name("capability-route.py")
S=importlib.util.spec_from_file_location("route",P); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
GUARD_P=P.with_name("worker-route-guard.py")
GUARD_S=importlib.util.spec_from_file_location("guard",GUARD_P); G=importlib.util.module_from_spec(GUARD_S); GUARD_S.loader.exec_module(G)
FLEET_P=P.parent.parent/"tools"/"fleet"/"route.py"
FLEET_S=importlib.util.spec_from_file_location("fleet_route",FLEET_P)
FLEET_ROUTE=importlib.util.module_from_spec(FLEET_S); FLEET_S.loader.exec_module(FLEET_ROUTE)
sys.path.insert(0,str(P.parent))
import dispatch_contract as D
import dispatch_runtime_support as RUNTIME_SUPPORT
ALL=["atomic-outcome","known-scope","no-shared-contract","no-resource-run","no-artifact-handoff","no-independent-verifier","focused-verification"]

DD_CONFIG_A="""schema_version: 1
depth1_owner: [claude, codex]
opencode:
  relief_only: true
capabilities:
  autopilot-code:
    execute: codex
    test: diverse
    report: claude
"""
DD_CONFIG_B="""schema_version: 1
depth1_owner: [claude, codex]
opencode:
  relief_only: true
capabilities:
  autopilot-code:
    execute: claude
    test: diverse
    report: codex
"""
DD_CONFIG_A_COMMENTED="""# scaffold comment only, no semantic change
schema_version: 1
depth1_owner: [claude, codex]
opencode:
  relief_only: true
capabilities:
  autopilot-code:
    execute: codex
    test: diverse
    report: claude
"""
DD_CONFIG_CORRUPT="""schema_version: 1
depth1_owner: [claude, codex]
opencode:
  relief_only: true
capabilities:
  autopilot-code:
    execute: gpt
"""

@contextlib.contextmanager
def dispatch_defaults_config(text):
 with tempfile.TemporaryDirectory() as td:
  p=Path(td)/"dispatch-defaults.yaml"; p.write_text(text)
  old=os.environ.get("DISPATCH_DEFAULTS_CONFIG")
  os.environ["DISPATCH_DEFAULTS_CONFIG"]=str(p)
  try: yield p
  finally:
   if old is None: os.environ.pop("DISPATCH_DEFAULTS_CONFIG",None)
   else: os.environ["DISPATCH_DEFAULTS_CONFIG"]=old

@contextlib.contextmanager
def dispatch_defaults_config_path(path):
 old=os.environ.get("DISPATCH_DEFAULTS_CONFIG")
 os.environ["DISPATCH_DEFAULTS_CONFIG"]=str(path)
 try: yield
 finally:
  if old is None: os.environ.pop("DISPATCH_DEFAULTS_CONFIG",None)
  else: os.environ["DISPATCH_DEFAULTS_CONFIG"]=old

class TestRoute(unittest.TestCase):
 def setUp(self):
  # `close_route` now reads completion markers through `resolve_agent_home()`; pin
  # AGENT_HOME to an isolated temp dir per test so gate-observation reads/writes never
  # touch the real installed home or leak state between tests via a shared route_id.
  # `resolve_agent_home()` only honors AGENT_HOME when `<AGENT_HOME>/core/CORE.md`
  # exists -- without this marker file it silently falls through to the real
  # `~/hearting` (or legacy `~/agent_setting`), which is exactly the leak this isolation prevents.
  self._tmp_home=tempfile.TemporaryDirectory()
  (Path(self._tmp_home.name)/"core").mkdir(parents=True,exist_ok=True)
  (Path(self._tmp_home.name)/"core"/"CORE.md").write_text("fixture\n",encoding="utf-8")
  self._previous_agent_home=os.environ.get("AGENT_HOME")
  os.environ["AGENT_HOME"]=self._tmp_home.name
  # completion_dir()/write_completion_marker() now resolve the dispatch state
  # root ahead of agent-home-relative state (I-2 unification), preferring an
  # inherited AGENT_DISPATCH_JOBS over AGENT_HOME/.dispatch -- clear it too so
  # a developer/CI shell's real registry never leaks into these fixtures.
  self._guard_env={key:os.environ.get(key) for key in (
   "AGENT_DISPATCH_JOBS","AGENT_DISPATCH_ATTEMPT_ID",
   "AGENT_DISPATCH_REGISTERED_WORKER","AGENT_DISPATCH_DEPTH",
   "AGENT_OWNER_ROUTE_FILE","AGENT_OWNER_ROUTE_ID","AGENT_OWNER_ROUTE_HASH",
   "AGENT_WORKFLOW_ROOT","XDG_STATE_HOME",
  )}
  for key in self._guard_env: os.environ.pop(key,None)
  os.environ["XDG_STATE_HOME"]=self._tmp_home.name+"/state"
  self._fixture_jobs=Path(self._tmp_home.name)/"state"/"jobs.log"
  self._fixture_jobs.parent.mkdir(parents=True,exist_ok=True)
  self._fixture_jobs.write_text("",encoding="utf-8")
  os.environ["AGENT_DISPATCH_JOBS"]=str(self._fixture_jobs)
  self.addCleanup(self._restore_agent_home)
 def _restore_agent_home(self):
  if self._previous_agent_home is None: os.environ.pop("AGENT_HOME",None)
  else: os.environ["AGENT_HOME"]=self._previous_agent_home
  for key,value in self._guard_env.items():
   if value is None: os.environ.pop(key,None)
   else: os.environ[key]=value
  self._tmp_home.cleanup()
 def dispatch(self,*rows):
  return {"tuples":list(rows),"native_subagent":[{
   "harness":"codex","transport":"headless",
   "execution_surface":"codex-native-subagent","registered_worker":False,
   "status":"supported","check_source":"fixture-native-check"}]}
 def nested(self,parent="codex",child="codex",authority="conductor",status="supported",failure=""):
  # parent_sandbox follows the parent harness's real wrapper export; a claude
  # parent never exports the Codex `workspace-write` label.
  sandbox=R.WRAPPER_PARENT_SANDBOXES[parent][0] if parent in R.WRAPPER_PARENT_SANDBOXES else "workspace-write"
  local=failure in {
   "invalid-worktree-codex-mount-target","not-a-git-worktree","worktree-not-found",
  }
  scope="none" if status=="supported" else "exact-worktree" if local else "runtime-global"
  return {"parent_harness":parent,"parent_transport":"headless","parent_sandbox":sandbox,"child_harness":child,"launch_authority":authority,"status":status,"probe_source":"fixture-probe","probe_time":"2026-07-16T00:00:00Z","failure_class":failure,"checked_worktree":str(R.ROOT.resolve()),"failure_scope":scope,"codex_command":"ok" if child=="codex" else "not-applicable","retry_on_isolated_worktree":1 if local else 0}
 def args(self,**kw):
  gate={"spec_read":{"satisfied":True,"source":"canonical-prd-sha256"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"conductor-prechecked"}}
  d=dict(capability="autopilot-code",capability_mode="dev",requested_intensity="direct",cwd=R.ROOT,artifact_root=R.ROOT,predicates=ALL,transport=None,inline_reason="atomic-direct",tracking="tracked",tracked_gate_evidence=gate); d.update(kw); return d
 def compile_v3(self,evidence):
  return R.compile_route(**self.args(requested_intensity="strong",predicates=[],signals=["shared-contract"],transport="headless",inline_reason=None,dispatch_evidence=evidence))
 def registered_headless(self,status="supported"):
  # Two supported harnesses, not one: quick now compiles a cross-harness frame
  # pair, and a single supported harness is sealed as `single-harness:<h>` on both legs of a
  # candidate list at compile. `status` still drives BOTH rows so the
  # unsupported fixture keeps naming `quick-headless-unavailable`.
  return {"candidates":[
   {"harness":"codex","transport":"headless","surface":"registered-headless","status":status,"probe_source":"fixture-probe","probe_time":"2026-07-20T00:00:00Z"},
   {"harness":"claude","transport":"headless","surface":"registered-headless","status":status,"probe_source":"fixture-probe","probe_time":"2026-07-20T00:00:00Z"}]}
 def legacy_v2(self,route):
  legacy=json.loads(json.dumps(route)); legacy.pop("dispatch_contract_version",None); legacy["broker_contract_version"]=2
  for row in legacy["dispatch_evidence"]["tuples"]:
   row["launch_authority"]="ancestor-broker"; row["broker_root"]="/tmp/fixture-broker"
  for node in legacy["nodes"]:
   for hop in node.get("fallback_hops",[])[:2]:
    for row in hop.get("candidates",[]): row["launch_authority"]="ancestor-broker"; row["broker_root"]="/tmp/fixture-broker"
  legacy["route_hash"]=R.route_hash(legacy); legacy["route_id"]="rt-"+legacy["route_hash"].split(":",1)[1][:16]
  return legacy
 def test_direct_all_and_stable(self):
  a=R.compile_route(**self.args()); b=R.compile_route(**self.args()); self.assertEqual(a,b); self.assertEqual(a["effective_intensity"],"direct"); self.assertEqual(a["owner_dispatch_depth"],0); self.assertEqual(a["max_dispatch_depth"],0); self.assertEqual(a["nodes"][0]["dispatch_depth"],0); self.assertEqual(a["nodes"][0]["execution_surface"],"inline"); self.assertFalse(a["nodes"][0]["registered_worker"]); self.assertEqual(a["conditional_extensions"][0]["after"],["inline"]); R.verify_route(a,R.ROOT)
  # A6/SD-123: capability-route.py already gates human_gate_bindings to
  # standard+ (compile payload sets [] for direct/quick regardless of the
  # recipe's declared bindings) -- assert that explicitly now that
  # autopilot-code's recipe declares a non-empty frame-review binding.
  self.assertEqual(a["human_gate_bindings"],[])
 def test_slug_is_canonicalized_and_sealed_while_legacy_absence_verifies(self):
  legacy=R.compile_route(**self.args())
  self.assertNotIn("slug",legacy)
  self.assertNotIn("slug_truncated",legacy)
  R.verify_route(legacy,R.ROOT)
  raw="Cycle A "+("Deterministic Locator "*4)
  expected,truncated=R.ARTIFACT_LOCATOR.slugify(raw)
  route=R.compile_route(**self.args(slug=raw))
  other=R.compile_route(**self.args(slug="cycle-b"))
  self.assertEqual(route["slug"],expected)
  self.assertEqual(route["slug_truncated"],truncated)
  self.assertTrue(route["slug_truncated"])
  self.assertNotEqual(route["route_hash"],other["route_hash"])
  R.verify_route(route,R.ROOT)
  marker_tampered=json.loads(json.dumps(route))
  marker_tampered["slug_truncated"]=False
  with self.assertRaisesRegex(ValueError,"stale or modified route hash"):
   R.verify_route(marker_tampered,R.ROOT)
 def test_verify_rejects_resealed_noncanonical_or_incomplete_slug(self):
  route=R.compile_route(**self.args(slug="cycle-a"))
  for key,value,message in (
      ("slug","Cycle A","route slug is not canonical"),
      ("slug_truncated",None,"invalid route slug metadata"),
  ):
   with self.subTest(key=key):
    invalid=json.loads(json.dumps(route))
    if value is None: invalid.pop(key)
    else: invalid[key]=value
    invalid["route_hash"]=R.route_hash(invalid)
    invalid["route_id"]="rt-"+invalid["route_hash"].split(":",1)[1][:16]
    with self.assertRaisesRegex(ValueError,message):
     R.verify_route(invalid,R.ROOT)
 def test_ambiguous_quick(self):
  a=R.compile_route(**self.args(predicates=[],transport=None,inline_reason=None,registered_headless_evidence=self.registered_headless()))
  self.assertEqual(a["effective_intensity"],"quick")
  # Quick is now a three-node route: the depth-1 frame pair the depth-0
  # session launches itself, then the owner. The owner axes this test has
  # always protected are read off `one-shot` by id, not by position.
  self.assertEqual([n["id"] for n in a["nodes"]],["frame","frame-alternative","one-shot"])
  owner=next(n for n in a["nodes"] if n["id"]=="one-shot")
  self.assertEqual(owner["dispatch_depth"],1)
  self.assertEqual(owner["execution_surface"],"registered-headless")
  self.assertTrue(owner["registered_worker"])
  self.assertEqual(owner["depends_on"],["frame","frame-alternative"])
  for leg in a["nodes"][:2]:
   self.assertEqual((leg["dispatch_depth"],leg["unit"],leg["worker_type"]),(1,"plan/frame","frame"))
   self.assertEqual(leg["launch_authority"],"depth-0")
   self.assertTrue(leg["registered_worker"])
   self.assertNotIn("fallback_hops",leg)
  self.assertEqual(a["conditional_extensions"][0]["after"],["one-shot"])
  # A6/SD-123 used to read "quick binds nothing". Quick now fences its owner
  # behind the frame gate -- only `direct` still binds nothing.
  self.assertEqual(a["human_gate_bindings"],
   [{"gate":"frame-review","node":"one-shot","position":"entry"}])
  self.assertIn("frame-review",a["human_gates"])
  self.assertEqual(a["completion_gates"],["quick-frame","quick-complete"])
 def test_quick_missing_eligibility_fails_closed(self):
  with self.assertRaisesRegex(ValueError,"quick-headless-unavailable"):
   R.compile_route(**self.args(predicates=[],transport=None,inline_reason=None,requested_intensity="quick"))
 def test_h6_explicit_direct_predicate_gap_refusal_names_missing(self):
  # H6: `--intensity direct` whose 7 predicates do not all hold used to be
  # silently promoted to quick and died as an opaque
  # `quick-headless-unavailable`. The no-evidence refusal must name the
  # missing predicates; with checked quick evidence the compile still
  # promotes (`test_ambiguous_quick`), and a true quick request keeps the
  # quick eligibility enum.
  partial=[p for p in ALL if p!="no-shared-contract"]
  with self.assertRaisesRegex(ValueError,"direct-predicate-gap:no-shared-contract"):
   R.compile_route(**self.args(predicates=partial,transport=None,inline_reason=None))
  with self.assertRaisesRegex(ValueError,"direct-predicate-gap:"):
   R.compile_route(**self.args(predicates=[],transport=None,inline_reason=None))
  with self.assertRaisesRegex(ValueError,"quick-headless-unavailable"):
   R.compile_route(**self.args(predicates=[],transport=None,inline_reason=None,requested_intensity="quick"))
 def test_quick_invalid_transport_fails_closed(self):
  with self.assertRaisesRegex(ValueError,"invalid quick transport"):
   R.compile_route(**self.args(predicates=[],transport="interactive",inline_reason=None,registered_headless_evidence=self.registered_headless()))
 def test_every_recipe_mode_has_one_registered_headless_quick_owner(self):
  for recipe in R.TOPO.load_registry()["recipes"]:
   for mode in recipe["modes"]:
    with self.subTest(capability=recipe["capability"],mode=mode):
     route=R.compile_route(
      recipe["capability"],mode,"quick",R.ROOT,R.ROOT,predicates=[],transport=None,
      tracking="tracked",tracked_gate_evidence=self.args()["tracked_gate_evidence"],
      registered_headless_evidence=self.registered_headless())
     # Still exactly ONE registered-headless quick owner per recipe mode --
     # that is what this test protects. The route now also carries the two
     # depth-1 frame legs ahead of it, which are not owners.
     framed = recipe["capability"] in {
      "autopilot-code","autopilot-design","autopilot-draft","autopilot-refine","autopilot-spec"}
     self.assertEqual([n["id"] for n in route["nodes"]],
                      ["frame","frame-alternative","one-shot"] if framed else ["one-shot"])
     owners=[n for n in route["nodes"] if n.get("unit")=="_kernel/owner"]
     self.assertEqual([n["id"] for n in owners],["one-shot"])
     owner=owners[0]
     self.assertEqual(route["owner_dispatch_depth"],1)
     self.assertEqual(route["max_dispatch_depth"],1)
     self.assertEqual(owner["dispatch_depth"],1)
     self.assertEqual(route["owner_model_profile"],"balanced-deep")
     self.assertEqual(owner["model_profile"],"balanced-deep")
     self.assertEqual(owner["execution_surface"],"registered-headless")
     self.assertTrue(owner["registered_worker"])
     for leg in route["nodes"][:2]:
      self.assertEqual(leg["dispatch_depth"],1)
      self.assertEqual(leg["execution_surface"],"registered-headless")
      self.assertTrue(leg["registered_worker"])
     R.verify_route(route,R.ROOT)
 def test_promotion_standard(self):
  evidence=self.dispatch(self.nested())
  a=R.compile_route(**self.args(signals=["public-api"],transport="headless",inline_reason=None,dispatch_evidence=evidence)); self.assertEqual([x["id"] for x in a["nodes"]],["frame","frame-alternative","plan","plan-check","execute","impl-review","test","report"])
  self.assertEqual(a["owner_model_profile"],"deep")
  self.assertEqual(a["conditional_extensions"][0]["after"],["report"])
  self.assertEqual(a["conditional_extensions"][0]["source_outputs"],[{"node":"report","output":"final_report.md"}])
 def test_recipe_without_artifact_sink_seals_empty_list(self):
  route=R.compile_route(**self.args(
   capability="autopilot-spec",capability_mode="update"))
  self.assertEqual(route["conditional_extensions"],[])
  R.verify_route(route,R.ROOT)
 def test_rehashed_conditional_extension_drift_is_rejected(self):
  route=R.compile_route(**self.args())
  route["conditional_extensions"][0]["after"]=["missing"]
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"conditional extensions"):
   R.verify_route(route,R.ROOT)
 def test_complete_recipe_mode_intensity_owner_and_realized_node_census(self):
  registry=R.TOPO.load_registry()
  evidence=self.dispatch(self.nested())
  compiled=0
  for recipe in registry["recipes"]:
   expected_owner_ids=[
    node["id"] for node in recipe["standard_plus"]["nodes"]
    if node.get("kind")=="capability-owner" and node.get("unit")=="_kernel/owner"
   ]
   for mode in recipe["modes"]:
    direct=R.compile_route(
     recipe["capability"],mode,"direct",R.ROOT,R.ROOT,predicates=recipe["direct_predicates"],
     transport=None,inline_reason="atomic-direct",tracking="tracked",
     tracked_gate_evidence=self.args()["tracked_gate_evidence"])
    self.assertIsNone(direct["owner_model_profile"])
    R.verify_route(direct,R.ROOT); compiled+=1
    quick=R.compile_route(
     recipe["capability"],mode,"quick",R.ROOT,R.ROOT,predicates=[],
     transport=None,tracking="tracked",
     tracked_gate_evidence=self.args()["tracked_gate_evidence"],
     registered_headless_evidence=self.registered_headless())
    self.assertEqual(quick["owner_model_profile"],"balanced-deep")
    self.assertEqual(
     next(n for n in quick["nodes"] if n["id"]=="one-shot")["model_profile"],
     "balanced-deep")
    R.verify_route(quick,R.ROOT); compiled+=1
    for intensity in ("standard","strong","thorough","adversarial"):
     with self.subTest(capability=recipe["capability"],mode=mode,intensity=intensity):
      route=R.compile_route(
       recipe["capability"],mode,intensity,R.ROOT,R.ROOT,predicates=[],
       transport="headless",tracking="tracked",
       tracked_gate_evidence=self.args()["tracked_gate_evidence"],
       dispatch_evidence=evidence)
      self.assertEqual(route["owner_model_profile"],"deep")
      owners=[
       node for node in route["nodes"]
       if node.get("kind")=="capability-owner" and node.get("unit")=="_kernel/owner"
      ]
      self.assertEqual([node["id"] for node in owners],expected_owner_ids)
      for owner in owners:
       self.assertEqual(owner["dispatch_depth"],1)
       self.assertEqual(owner["model_profile"],"deep")
       self.assertEqual(owner["role"],"deep orchestrator")
      expected=json.loads(json.dumps(recipe["standard_plus"]["nodes"]))
      expected=R._expand_parallel_groups(
       expected,recipe["standard_plus"].get("parallel_groups"),intensity,
       recipe["capability"],
       auxiliary_check_units=registry.get("auxiliary_check_units"))
      # Same two steps, same order, as the compiler and the verifier: the
      # frame tier ladder is stamped from the RESOLVED owner profile first,
      # and only then are demands sealed. The recipe's own static
      # `model_profile` on a frame leg is a placeholder that this stamp
      # overwrites, so rebuilding `expected` without it compares the compiled
      # route against a value nothing is supposed to keep.
      R._stamp_frame_profiles(expected, route["owner_model_profile"],
                              route.get("owner_profile_demand"))
      R._seal_profile_demands(expected, legacy=True)
      for node in expected: node.pop("fallback_hops",None)
      def stable(nodes):
       return [
        {key:value for key,value in node.items()
         if key not in ("fallback_hops","harness_affinity","harness_policy")}
        for node in nodes
       ]
      self.assertEqual(stable(route["nodes"]),stable(expected))
      R.verify_route(route,R.ROOT); compiled+=1
  self.assertEqual(compiled,162)  # 27 recipes x 6 intensities (W7C added the 3 pre/ops entries)
 def test_verify_rejects_rehashed_executable_owner_profile_drift(self):
  quick=R.compile_route(**self.args(
   requested_intensity="quick",predicates=[],transport=None,inline_reason=None,
   registered_headless_evidence=self.registered_headless()))
  quick["nodes"][0]["model_profile"]="light"
  quick["route_hash"]=R.route_hash(quick)
  quick["route_id"]="rt-"+quick["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"sealed profile differs from selection"):
   R.verify_route(quick,R.ROOT)
  standard=R.compile_route(**self.args(
   capability="autopilot-spec",capability_mode="update",
   requested_intensity="standard",predicates=[],transport="headless",
   inline_reason=None,dispatch_evidence=self.dispatch(self.nested())))
  owner=next(node for node in standard["nodes"] if node["id"]=="prd-transaction")
  owner["model_profile"]="balanced-deep"
  standard["route_hash"]=R.route_hash(standard)
  standard["route_id"]="rt-"+standard["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"sealed profile differs from selection"):
   R.verify_route(standard,R.ROOT)
 def test_composed_verify_rejects_rehashed_semantic_owner_profile_drift(self):
  recipe=json.loads(json.dumps(
   R.TOPO.resolve_recipe(R.TOPO.load_registry(),"autopilot-spec","update")))
  recipe["modes"]=["composed-fixture"]
  for node in recipe["standard_plus"]["nodes"]:
   if node.get("kind") == "resource-runner": continue
   profile=node["model_profile"]
   node["profile_demand"]={"schema_version":1,
    "judgment_requirement":"difficult-uncertain" if profile=="deep" else "important" if profile=="balanced-deep" else "predetermined",
    "execution_scope":"extended-multistep" if profile=="balanced" else "short-local",
    "judgment_reason":"Fixture preserves the declared judgment.",
    "execution_reason":"Fixture performs its declared steps.","evidence_refs":["fixture.md"]}
  for group in recipe["standard_plus"].get("parallel_groups",[]):
   for leg in group.get("legs",[]):
    profile=leg["model_profile"]
    leg["profile_demand"]={"schema_version":1,
     "judgment_requirement":"difficult-uncertain" if profile=="deep" else "important" if profile=="balanced-deep" else "predetermined",
     "execution_scope":"short-local","judgment_reason":"Fixture declared judgment.",
     "execution_reason":"Fixture bounded steps.","evidence_refs":["fixture.md"]}
  route=self._composed(recipe)
  route_owner=next(node for node in route["nodes"] if node["id"]=="prd-transaction")
  embedded_owner=next(
   node for node in route["composed_recipe"]["standard_plus"]["nodes"]
   if node["id"]=="prd-transaction")
  route_owner["model_profile"]="balanced-deep"
  embedded_owner["model_profile"]="balanced-deep"
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"sealed profile differs from selection"):
   R.verify_route(route,R.ROOT)
 def test_strong_expands_asymmetric_parallel_groups(self):
  evidence=self.dispatch(self.nested())
  standard=R.compile_route(**self.args(signals=["public-api"],transport="headless",inline_reason=None,dispatch_evidence=evidence))
  self.assertNotIn("impl-review-alternative",[x["id"] for x in standard["nodes"]])
  self.assertNotIn("plan-alternative",[x["id"] for x in standard["nodes"]])
  strong=self.compile_v3(evidence)
  # The frame pair is no longer a parallel_group, so it no longer widens with
  # intensity: `frame-contrarian` does not exist at any intensity now. The
  # asymmetric-group expansion this test protects is still read off
  # plan/plan-check/impl-review.
  self.assertEqual([x["id"] for x in strong["nodes"]],
   ["frame","frame-alternative","plan","plan-alternative","plan-check","plan-check-alternative","execute","impl-review","impl-review-alternative","test","report"])
  base=next(n for n in strong["nodes"] if n["id"]=="impl-review")
  alternative=next(n for n in strong["nodes"] if n["id"]=="impl-review-alternative")
  self.assertEqual(base["parallel_group"],"impl-review")
  self.assertEqual(alternative["parallel_group"],"impl-review")
  self.assertEqual(alternative["parallel_independence_axes"],["cross-harness","model-profile","perspective"])
  self.assertEqual(alternative["dispatch_depth"],2)
  self.assertEqual(alternative["unit"],base["unit"])
  self.assertEqual(alternative["outputs"],["_internal/dev_reviews-alternative/phase_review.md"])
  self.assertEqual(alternative["write_scope"],["_internal/dev_reviews-alternative/**"])
  self.assertNotEqual(alternative["outputs"],base["outputs"])
  self.assertEqual((base["model_profile"],alternative["model_profile"]),("balanced-deep","light"))
  self.assertNotEqual(base["perspective"],alternative["perspective"])
  test_node=next(n for n in strong["nodes"] if n["id"]=="test")
  self.assertIn("impl-review",test_node["depends_on"])
  self.assertIn("impl-review-alternative",test_node["depends_on"])
  R.verify_route(strong,R.ROOT)
 def test_framing_anchor_expands_from_standard_and_feeds_plan(self):
  # user directive 2026-07-24: direction-setting points get independent
  # cross-model 2-way exploration from `standard`; the plan synthesizer reads
  # BOTH legs' briefs, and at `strong` the plan itself replicates with
  # plan-check as the arbiter reading both plans.
  #
  # The two-way frame exploration is now carried by an EXPLICITLY declared
  # pair of depth-1 nodes rather than by a parallel_group replica, so the
  # independence is asserted directly (distinct model profiles, distinct
  # outputs and write scopes) instead of through group metadata. The
  # downstream half -- plan reads both briefs, plan replicates at strong --
  # is unchanged and is what the rest of this test still checks.
  evidence=self.dispatch(self.nested())
  standard=R.compile_route(**self.args(signals=["public-api"],transport="headless",inline_reason=None,dispatch_evidence=evidence))
  frame=next(n for n in standard["nodes"] if n["id"]=="frame")
  frame_replica=next(n for n in standard["nodes"] if n["id"]=="frame-alternative")
  for leg in (frame,frame_replica):
   self.assertNotIn("parallel_group",leg)
   self.assertNotIn("fallback_hops",leg)
   self.assertEqual((leg["unit"],leg["dispatch_depth"],leg["worker_type"]),("plan/frame",1,"frame"))
   self.assertEqual(leg["launch_authority"],"depth-0")
  self.assertNotEqual(frame["model_profile"],frame_replica["model_profile"])
  self.assertEqual(frame["outputs"],["shards/frame/direction-brief.md"])
  self.assertEqual(frame["write_scope"],["shards/frame/**"])
  self.assertEqual(frame_replica["outputs"],["shards/frame-alternative/direction-brief.md"])
  self.assertEqual(frame_replica["write_scope"],["shards/frame-alternative/**"])
  plan=next(n for n in standard["nodes"] if n["id"]=="plan")
  self.assertIn("frame",plan["depends_on"]); self.assertIn("frame-alternative",plan["depends_on"])
  self.assertIn("shards/frame/direction-brief.md",plan["inputs"])
  self.assertIn("shards/frame-alternative/direction-brief.md",plan["inputs"])
  strong=self.compile_v3(evidence)
  plan_replica=next(n for n in strong["nodes"] if n["id"]=="plan-alternative")
  self.assertEqual(plan_replica["outputs"],["plan.alternative.md","checklist.alternative.md"])
  self.assertIn("shards/frame-alternative/direction-brief.md",plan_replica["inputs"])
  check=next(n for n in strong["nodes"] if n["id"]=="plan-check")
  self.assertIn("plan",check["depends_on"]); self.assertIn("plan-alternative",check["depends_on"])
  self.assertIn("plan.alternative.md",check["inputs"]); self.assertIn("checklist.alternative.md",check["inputs"])
  for node in strong["nodes"]:
   self.assertEqual(
    R.TOPO._uncovered_path_outputs(
     node.get("outputs",[]),node.get("write_scope",[])),[],node["id"])
  R.verify_route(strong,R.ROOT)
 def test_code_execute_and_test_can_write_cycle_evidence(self):
  # 실측(cairn 2026-08-20_step7-apply-prep): assignment가 evidence/…를 요구했지만
  # execute/test write_scope에 evidence/**가 없어 워커가 우회 기록을 해야 했다.
  # report는 다른 스테이지의 evidence class를 다시 쓰지 않으므로(capabilities/code-report.md)
  # 의도적으로 제외한다.
  route=self.compile_v3(self.dispatch(self.nested()))
  for node_id in ("execute","test"):
   node=next(n for n in route["nodes"] if n["id"]==node_id)
   self.assertIn("evidence/**",node["write_scope"],node_id)
  report=next(n for n in route["nodes"] if n["id"]=="report")
  self.assertNotIn("evidence/**",report["write_scope"])
  R.verify_route(route,R.ROOT)
 def test_compile_and_verify_reject_outputs_outside_write_scope(self):
  recipe=self._composed_recipe()
  frame=next(node for node in recipe["standard_plus"]["nodes"] if node["id"]=="frame")
  frame["outputs"]=["shards/elsewhere/direction-brief.md"]
  with self.assertRaisesRegex(ValueError,"outputs outside write_scope"):
   self._composed(recipe)
  route=self.compile_v3(self.dispatch(self.nested()))
  replica=next(node for node in route["nodes"] if node["id"]=="frame-alternative")
  replica["outputs"]=["shards/frame/escaped.md"]
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"outputs outside write_scope"):
   R.verify_route(route,R.ROOT)
 def test_map_worker_shard_replica_gets_disjoint_tree(self):
  # spec research shards replicate as a sibling '-replica' tree; the review
  # arbiter reads both trees.
  route=R.compile_route(**self.args(
   capability="autopilot-spec",capability_mode="update",requested_intensity="standard",
   predicates=[],signals=["shared-contract"],transport="headless",inline_reason=None,
   dispatch_evidence=self.dispatch(self.nested())))
  replica=next(n for n in route["nodes"] if n["id"]=="research-alternative")
  self.assertEqual(
   replica["outputs"],
   [
    "spec/_internal/research-alternative/**",
    "spec/<component>/_internal/research-alternative/**",
   ],
  )
  review=next(n for n in route["nodes"] if n["id"]=="review")
  self.assertIn("research-alternative",review["depends_on"])
  self.assertIn("spec/_internal/research-alternative/**",review["inputs"])
  R.verify_route(route,R.ROOT)
 def test_replica_carries_fallback_chain_and_seal(self):
  strong=self.compile_v3(self.dispatch(self.nested()))
  replica=next(n for n in strong["nodes"] if n["id"]=="impl-review-alternative")
  self.assertEqual([h["fallback_hop"] for h in replica["fallback_hops"]],
   ["same-harness-headless","cross-harness-headless","native-subagent","inline"])
  self.assertIn(replica.get("harness_affinity"),{"claude","codex","opencode","diverse","unspecified"})
 def test_thorough_and_adversarial_expand_three_way_groups(self):
  for tier in ("thorough","adversarial"):
   route=R.compile_route(**self.args(requested_intensity=tier,predicates=[],signals=["shared-contract"],transport="headless",inline_reason=None,dispatch_evidence=self.dispatch(self.nested())))
   self.assertIn("impl-review-failure-mode",[x["id"] for x in route["nodes"]])
   self.assertIn("plan-implementation-risk",[x["id"] for x in route["nodes"]])
 def test_nodes_carry_sealed_unit_refs(self):
  route=self.compile_v3(self.dispatch(self.nested()))
  units={n["id"]:n.get("unit") for n in route["nodes"]}
  self.assertEqual(units,{
   # `frame-contrarian` is gone: the frame pair is declared explicitly and no
   # longer widens with intensity.
   "frame":"plan/frame","frame-alternative":"plan/frame",
   "plan":"plan/plan-author","plan-alternative":"plan/plan-author",
   "plan-check":"qa/plan-review","plan-check-alternative":"qa/plan-review","execute":"dev/backend",
   "impl-review":"qa/code-review","impl-review-alternative":"qa/code-review",
   "test":"qa/test","report":"editorial/report"})
  tampered=json.loads(json.dumps(route)); tampered["nodes"][0]["unit"]="dev/backend"
  with self.assertRaisesRegex(ValueError,"stale or modified route hash"):
   R.verify_route(tampered,R.ROOT)
 def test_ac20_new_groups_realize_exactly_declared_legs(self):
  evidence=self.dispatch(self.nested())
  expectations={
   ("autopilot-code","dev","strong"):["plan-check","plan-check-alternative"],
   ("autopilot-code","dev","thorough"):["plan-check","plan-check-alternative","plan-check-simplicity"],
   ("autopilot-design","default","strong"):["visual-verify","visual-verify-alternative"],
   ("autopilot-draft","doc","strong"):["strategy-review","strategy-review-alternative","quality-review","quality-review-alternative"],
   ("autopilot-draft","doc","thorough"):["strategy-review","strategy-review-alternative","quality-review","quality-review-alternative","quality-review-assumption"],
   ("autopilot-lab","setup","strong"):["run-verify","run-verify-alternative"],
   ("autopilot-ship","default","strong"):["security-review","security-review-alternative"],
   ("autopilot-ship","default","adversarial"):["security-review","security-review-alternative","security-review-failure-mode"],
  }
  for (cap,mode,intensity),ids in expectations.items():
   with self.subTest(capability=cap,mode=mode,intensity=intensity):
    route=R.compile_route(cap,mode,intensity,R.ROOT,R.ROOT,predicates=[],transport="headless",
     tracking="tracked",tracked_gate_evidence=self.args()["tracked_gate_evidence"],dispatch_evidence=evidence)
    realized=[n["id"] for n in route["nodes"]]
    for node_id in ids:
     self.assertIn(node_id,realized)
    # exactly the declared legs realize for the new groups — no extra siblings
    suffixes=("anchor","alternative","simplicity","assumption","test-gap","edge-case","failure-mode")
    new_group_ids={node_id for node_id in ids for _ in [0]}
    derived=set()
    for node_id in ids:
     base=node_id
     for suffix in suffixes:
      if node_id.endswith("-"+suffix):
       base=node_id[:-(len(suffix)+1)]
       break
     derived.add(base)
    for n in route["nodes"]:
     if n.get("parallel_group") in derived:
      self.assertIn(n["id"],ids)
 def test_ac2_width_two_and_three_realize_disjoint_peer_and_aux(self):
  evidence=self.dispatch(self.nested())
  strong=R.compile_route(**self.args(requested_intensity="strong",predicates=[],signals=["shared-contract"],transport="headless",inline_reason=None,dispatch_evidence=evidence))
  thorough=R.compile_route(**self.args(requested_intensity="thorough",predicates=[],signals=["shared-contract"],transport="headless",inline_reason=None,dispatch_evidence=evidence))
  strong_ids={n["id"] for n in strong["nodes"] if n.get("parallel_group")=="plan-check"}
  thorough_ids={n["id"] for n in thorough["nodes"] if n.get("parallel_group")=="plan-check"}
  self.assertEqual(strong_ids,{"plan-check","plan-check-alternative"})
  self.assertEqual(thorough_ids,{"plan-check","plan-check-alternative","plan-check-simplicity"})
  peer=[n for n in thorough["nodes"] if n["id"] in ("plan-check","plan-check-alternative")]
  aux=[n for n in thorough["nodes"] if n["id"]=="plan-check-simplicity"]
  self.assertEqual({n.get("leg_class") for n in peer},{"peer"})
  self.assertEqual(aux[0]["leg_class"],"auxiliary")
  self.assertEqual(aux[0]["auxiliary_check"],"simplicity-check")
  scopes=[set(n["write_scope"]) for n in peer+aux]
  for i,left in enumerate(scopes):
   for right in scopes[i+1:]:
    self.assertTrue(left.isdisjoint(right),f"overlap {left} {right}")
  recompiled=R.compile_route(**self.args(requested_intensity="thorough",predicates=[],signals=["shared-contract"],transport="headless",inline_reason=None,dispatch_evidence=evidence))
  self.assertEqual(thorough["route_hash"],recompiled["route_hash"])
 def test_ac21_terminal_gate_duplication_is_rejected(self):
  registry=R.TOPO.load_registry()
  recipe=R.TOPO.resolve_recipe(registry,"autopilot-code","dev")
  nodes=json.loads(json.dumps(recipe["standard_plus"]["nodes"]))
  nodes[0]["terminal"]=True; nodes[0]["terminal_gate"]="dup-terminal"
  nodes[1]["terminal"]=True; nodes[1]["terminal_gate"]="dup-terminal"
  with self.assertRaisesRegex(ValueError,"terminal gate dup-terminal held by both"):
   R._workflow_contract(registry,nodes,[])
  evidence=self.dispatch(self.nested())
  research=R.compile_route("autopilot-research","academic","thorough",R.ROOT,R.ROOT,predicates=[],transport="headless",
   tracking="tracked",tracked_gate_evidence=self.args()["tracked_gate_evidence"],dispatch_evidence=evidence)
  self.assertEqual(research["workflow_contract"]["terminal_nodes"],["claim-verify"])
 def test_ac22_terminal_anchor_auxiliary_and_pipeline_rejects(self):
  # post-deploy-verify is terminal:true. G6/AC 21 now rejects ANY parallel
  # group declared on a non-grandfathered terminal node at declaration, which
  # strictly subsumes D4's narrower "terminal anchor has no arbiter for
  # auxiliary findings" case -- the G6 message fires first.
  r=R.TOPO.load_registry()
  broken=json.loads(json.dumps(r))
  ship=next(x for x in broken["recipes"] if x["capability"]=="autopilot-ship")
  ship["standard_plus"]["parallel_groups"].append({
   "id":"post-deploy-verify","node":"post-deploy-verify","kind":"verify","min_intensity":"strong",
   "width_by_intensity":{"strong":2,"thorough":3,"adversarial":3},"join_policy":"all",
   "independence_axes":["cross-harness","model-profile","perspective"],
   "legs":[
    {"suffix":"anchor","perspective":"primary-post-deploy-verify","model_profile":"light","leg_class":"peer"},
    {"suffix":"alternative","perspective":"independent-post-deploy-verify","model_profile":"balanced-deep","leg_class":"peer"},
    {"suffix":"failure-mode","perspective":"failure-mode-check","model_profile":"light","leg_class":"auxiliary","auxiliary_check":"failure-mode-check"},
   ]})
  with self.assertRaisesRegex(R.TOPO.TopologyError,"parallel group on terminal node"):
   R.TOPO.validate_registry(broken)
 def test_d4_terminal_anchor_cannot_arbitrate_auxiliary_findings(self):
  # D4 replacement fixture. The assertion above was repurposed to G6's
  # message, leaving nothing guarding D4's own rule: a terminal anchor has no
  # downstream verdict that can carry `auxiliary_findings_considered`, so it
  # structurally has no arbiter. G6 masks it for every ordinary terminal node,
  # but NOT for the recorded `autopilot-research claim-verify` grandfather --
  # which is precisely where the rule still has to bite on its own. That is
  # also why PRD 13.30.4's `edge-case-check` placement has zero realized slot.
  r=R.TOPO.load_registry()
  broken=json.loads(json.dumps(r))
  research=next(x for x in broken["recipes"] if x["capability"]=="autopilot-research")
  self.assertTrue(next(n for n in research["standard_plus"]["nodes"]
                       if n["id"]=="claim-verify").get("terminal"))
  group=next(g for g in research["standard_plus"]["parallel_groups"]
             if g["id"]=="claim-verify")
  group["width_by_intensity"]["thorough"]=3
  group["width_by_intensity"]["adversarial"]=3
  group["legs"]=group["legs"]+[{
   "suffix":"edge-case","perspective":"edge-case-check","model_profile":"light",
   "leg_class":"auxiliary","auxiliary_check":"edge-case-check"}]
  with self.assertRaisesRegex(
   R.TOPO.TopologyError,
   r"terminal anchor claim-verify has no arbiter for auxiliary findings"):
   R.TOPO.validate_registry(broken)
  # the grandfather without an auxiliary leg still validates and compiles
  R.TOPO.validate_registry(R.TOPO.load_registry())
 def test_g6_parallel_group_on_terminal_node_rejected_unless_grandfathered(self):
  # G6/AC 21: a parallel group on a terminal node compile-rejects unless the
  # (capability, group id) pair is the recorded autopilot-research claim-verify
  # grandfather. Declaring one on report (autopilot-code) must reject.
  r=R.TOPO.load_registry()
  broken=json.loads(json.dumps(r))
  code=next(x for x in broken["recipes"] if x["capability"]=="autopilot-code")
  self.assertTrue(next(n for n in code["standard_plus"]["nodes"] if n["id"]=="report").get("terminal"))
  code["standard_plus"]["parallel_groups"].append({
   "id":"report","node":"report","kind":"verify","min_intensity":"strong",
   "width_by_intensity":{"strong":2,"thorough":2,"adversarial":2},"join_policy":"all",
   "independence_axes":["cross-harness","model-profile","perspective"],
   "legs":[
    {"suffix":"anchor","perspective":"primary-report","model_profile":"light","leg_class":"peer"},
    {"suffix":"alternative","perspective":"independent-report","model_profile":"balanced-deep","leg_class":"peer"},
   ]})
  with self.assertRaisesRegex(R.TOPO.TopologyError,"parallel group on terminal node 'report' is rejected"):
   R.TOPO.validate_registry(broken)
  # The grandfathered claim-verify group itself must still validate and compile.
  registry=R.TOPO.load_registry(); R.TOPO.validate_registry(registry)
  evidence=self.dispatch(self.nested())
  route=R.compile_route(
   "autopilot-research","market","strong",R.ROOT,R.ROOT,predicates=[],
   transport="headless",tracking="tracked",
   tracked_gate_evidence=self.args()["tracked_gate_evidence"],
   dispatch_evidence=evidence)
  ids=[node["id"] for node in route["nodes"]]
  self.assertIn("claim-verify",ids)
  self.assertIn("claim-verify-alternative",ids)
  anchor=next(n for n in route["nodes"] if n["id"]=="claim-verify")
  alt=next(n for n in route["nodes"] if n["id"]=="claim-verify-alternative")
  self.assertTrue(anchor.get("terminal"))
  self.assertNotIn("terminal",alt)
  # autopilot-code test / autopilot-research synthesis: pipeline-stage anchor
  # without a direct downstream review-worker arbiter already rejects the group.
  for capability,node in (("autopilot-code","test"),("autopilot-research","synthesis")):
   broken=json.loads(json.dumps(r))
   recipe=next(x for x in broken["recipes"] if x["capability"]==capability)
   recipe["standard_plus"]["parallel_groups"].append({
    "id":node,"node":node,"kind":"verify","min_intensity":"strong",
    "width_by_intensity":{"strong":2,"thorough":2,"adversarial":2},"join_policy":"all",
    "independence_axes":["cross-harness","model-profile","perspective"],
    "legs":[
     {"suffix":"anchor","perspective":"primary-"+node,"model_profile":"light","leg_class":"peer"},
     {"suffix":"alternative","perspective":"independent-"+node,"model_profile":"balanced-deep","leg_class":"peer"},
    ]})
   with self.subTest(capability=capability):
    with self.assertRaisesRegex(R.TOPO.TopologyError,"requires a direct review arbiter"):
     R.TOPO.validate_registry(broken)
  # registry-level guard: no new group may target post-deploy-verify at all.
  for recipe in r["recipes"]:
   for group in recipe["standard_plus"].get("parallel_groups",[]):
    self.assertNotEqual(group.get("node"),"post-deploy-verify")
 def test_a47_4_reserved_node_id_prefix_rejected(self):
  # A47-4: a `_`-prefixed standard_plus node id must fail-closed at
  # `_validate_recipe` -- before `capability-route.py` ever reaches a
  # `compile_route`/`write_once` call, so no route file can be written.
  r=R.TOPO.load_registry()
  broken=json.loads(json.dumps(r))
  code=next(x for x in broken["recipes"] if x["capability"]=="autopilot-code")
  code["standard_plus"]["nodes"][0]["id"]="_reserved"
  with self.assertRaisesRegex(R.TOPO.TopologyError,"route-node-id-reserved-prefix"):
   R.TOPO.validate_registry(broken)
  with tempfile.TemporaryDirectory() as td:
   self.assertEqual(list(Path(td).glob("*.json")),[])
  # the unmodified registry still validates and compiles (no regression).
  R.TOPO.validate_registry(R.TOPO.load_registry())
 def test_a47_4_reserved_prefix_conditional_extension(self):
  # A47-4: same predicate applied to conditional_extensions ids, reached
  # from within the same `_validate_recipe` call via
  # `_validate_conditional_extensions`.
  r=R.TOPO.load_registry()
  broken=json.loads(json.dumps(r))
  code=next(x for x in broken["recipes"] if x["capability"]=="autopilot-code")
  code["conditional_extensions"][0]["id"]="_reserved-extension"
  with self.assertRaisesRegex(R.TOPO.TopologyError,"route-node-id-reserved-prefix"):
   R.TOPO.validate_registry(broken)
  with tempfile.TemporaryDirectory() as td:
   self.assertEqual(list(Path(td).glob("*.json")),[])
  R.TOPO.validate_registry(R.TOPO.load_registry())
 def test_a47_5_complete_marker_path_stamps_intent(self):
  # A47-5: every `open|running -> done` close edge stamps the delivery
  # intent -- including the completion-marker (W1) route. dispatch_contract
  # .test.py's own W1 test hand-crafts a synthetic marker shape; this proves
  # capability-route.py's REAL `write_completion_marker()` output (the same
  # shape `_join_group` above publishes) round-trips through dispatch_
  # contract.marker_bound_delivery_transaction end-to-end.
  route=self.compile_v3(self.dispatch(self.nested()))
  node=next(n for n in route["nodes"] if n["id"]=="execute")
  with tempfile.TemporaryDirectory() as td:
   base=Path(td)
   jobs=base/"jobs.log"
   # Chain-(1) explicit AGENT_DISPATCH_JOBS override so completion_dir()'s
   # resolve_dispatch_state_root() lands inside this test's own tempdir --
   # never the real installed ~/.local/state/hearting/dispatch tree.
   previous_dispatch_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   self.addCleanup(lambda: (
    os.environ.pop("AGENT_DISPATCH_JOBS",None) if previous_dispatch_jobs is None
    else os.environ.__setitem__("AGENT_DISPATCH_JOBS",previous_dispatch_jobs)
   ))
   evidence=base/"evidence.md"; evidence.write_text("verified\n",encoding="utf-8")
   attempt_id="att-a47-5-fixture"
   metadata={
     "attempt_schema_version":2,"dispatch_depth":node["dispatch_depth"],
     "transport":"headless","execution_surface":"registered-headless",
     "registered_worker":"1","fallback_hop":"same-harness-headless",
   }
   marker=R.write_completion_marker(
    route,node,"execute",evidence,attempt_id=attempt_id,attempt_metadata=metadata)
   completion=R.completion_dir(route["route_id"])
   R.atomic_write(completion/f"execute.{attempt_id}.attempt.json",{
     "schema_version":2,"route_id":route["route_id"],"node_id":"execute",
     "attempt_id":attempt_id,"dispatch_depth":marker["dispatch_depth"],
     "transport":marker["transport"],"execution_surface":marker["execution_surface"],
     "registered_worker":marker["registered_worker"],"fallback_hop":marker["fallback_hop"],
     "evidence_sha256":marker["evidence"]["sha256"],
     "completion_marker":str(completion/"execute.json"),
     "completion_marker_history":str(completion/f"execute.{marker['sequence']}.json"),
   })
   route_path=base/"route.json"
   route_path.write_text(json.dumps(route),encoding="utf-8")
   marker_path=completion/"execute.json"
   metadata_pipe=(
     f"attempt_schema_version=2,dispatch_depth={node['dispatch_depth']},"
     "transport=headless,execution_surface=registered-headless,registered_worker=1,"
     f"fallback_hop=same-harness-headless,attempt_id={attempt_id},"
     f"route_id={route['route_id']},route_hash={route['route_hash']},route_node=execute,"
     f"route_file={route_path},completion_marker={marker_path},"
     "launch_outcome=never-launched,"
     "parent_completion_delivery=claude-parent-runtime,"
     f"parent_sid=sess-a47-5,parent_attempt_id=att-a47-5-owner,harness=claude"
   )
   raw=f"2026-08-29T00:00:00Z\topen\t/r\t/w\texecute\t{metadata_pipe}"
   jobs.write_text(raw+"\n",encoding="utf-8")
   parsed=D.parse_registry_metadata(metadata_pipe)
   result=D.marker_bound_delivery_transaction(
     jobs,attempt_id,parent_attempt_id=attempt_id,
     expected_row_revision=hashlib.sha256(raw.encode()).hexdigest(),
     expected_process_identity=D.marker_bound_process_identity(parsed),
     process_observation=D.ProcessQuiescence("quiescent","fixture"),
   )
   self.assertTrue(result.advanced)
   after=D.parse_registry_metadata(jobs.read_text(encoding="utf-8").splitlines()[0].split("\t")[5])
   self.assertEqual(after.get("delivery_intent"),"1")
   self.assertEqual(after.get("delivery_recipient_kind"),"claude-parent-runtime")
 def test_ac24_plan_check_two_way_is_read_only_arbiter(self):
  # AC 24: the 2-way plan-check group merges under the stricter-wins review
  # merge contract; the check itself stays read-only (writes only its own
  # review bucket, never plan.md) and its unit is read_only.
  evidence=self.dispatch(self.nested())
  strong=R.compile_route(**self.args(requested_intensity="strong",predicates=[],signals=["shared-contract"],transport="headless",inline_reason=None,dispatch_evidence=evidence))
  plan_checks=[n for n in strong["nodes"] if n.get("parallel_group")=="plan-check"]
  self.assertEqual({n["id"] for n in plan_checks},{"plan-check","plan-check-alternative"})
  for n in plan_checks:
   self.assertEqual(n["unit"],"qa/plan-review")
   self.assertNotIn("plan.md",n["write_scope"])
   self.assertEqual(n.get("leg_class"),"peer")
 def research_route(self,intensity="thorough"):
  return R.compile_route(
   "autopilot-research","market",intensity,R.ROOT,R.ROOT,predicates=[],
   transport="headless",tracking="tracked",
   tracked_gate_evidence=self.args()["tracked_gate_evidence"],
   dispatch_evidence=self.dispatch(self.nested()))
 def test_g1_auxiliary_arbiter_is_never_the_anchor(self):
  # G1 root cause: the gate used to fire on the group's ANCHOR, a leg that runs
  # concurrently with the auxiliary and therefore cannot have considered its
  # findings. PRD 13.30.4 names a different arbiter per anchor kind, and in no
  # case is it the anchor. All six realized auxiliary-bearing groups must
  # resolve -- an unresolvable arbiter is a typed failure, never a silent pass.
  expected={
   ("autopilot-code","dev","thorough","plan-check"):("owner-merge",None),
   ("autopilot-draft","paper","thorough","quality-review"):("owner-merge",None),
   ("autopilot-ship","default","adversarial","security-review"):("owner-merge",None),
   ("autopilot-spec","app","thorough","review"):("owner-merge",None),
   ("autopilot-research","market","thorough","retrieval"):("node","synthesis"),
   ("autopilot-spec","app","thorough","research"):("node","review"),
  }
  evidence=self.dispatch(self.nested())
  seen=set()
  for (capability,mode,intensity,group),arbiter in expected.items():
   with self.subTest(capability=capability,group=group):
    route=R.compile_route(
     capability,mode,intensity,R.ROOT,R.ROOT,predicates=[],transport="headless",
     tracking="tracked",tracked_gate_evidence=self.args()["tracked_gate_evidence"],
     dispatch_evidence=evidence)
    self.assertTrue(R._realized_auxiliary_nodes(route,group))
    self.assertEqual(R._resolve_auxiliary_arbiter(route,group),arbiter)
    anchor=next(n for n in route["nodes"]
                if n.get("parallel_group")==group and n.get("parallel_leg_index")==0)
    self.assertNotEqual(arbiter,("node",anchor["id"]))
    # M1: the registry's `auxiliary_arbiter` declaration must sit where this
    # resolution says the arbiter is, not on the anchor. The topology guard and
    # the runtime resolver are two implementations of the same proposition, and
    # a registry that declares the pre-G1 world reads as the pre-G1 world even
    # when the runtime no longer does.
    contracts=R.TOPO.load_registry()["completion_gate_contracts"]
    anchor_gate=contracts.get(anchor["completion_gate"],{})
    if arbiter[0]=="owner-merge":
     # no route node arbitrates THIS group, so its anchor's gate carries the
     # flag only if that same node arbitrates some OTHER group. `spec-review`
     # is exactly that overlap: node `review` anchors the owner-merge `review`
     # group and is the node arbiter of `research`.
     if anchor_gate.get("auxiliary_arbiter") is True:
      self.assertTrue(R._auxiliary_groups_arbitrated_by(route,anchor["id"]),
                      f"{anchor['completion_gate']} declares auxiliary_arbiter "
                      "but that node arbitrates no group")
    else:
     arbiter_node=next(n for n in route["nodes"] if n["id"]==arbiter[1])
     self.assertIs(contracts[arbiter_node["completion_gate"]].get("auxiliary_arbiter"),
                   True)
    seen.add((capability,group))
  # F1: `len(seen)` compared this dict against itself, so a SEVENTH
  # auxiliary-bearing group added to the registry would have passed in silence.
  # Ask the registry for the set instead and hold the expectation to it.
  declared={
   (recipe["capability"],group["id"])
   for recipe in R.TOPO.load_registry()["recipes"]
   for group in (recipe.get("standard_plus") or {}).get("parallel_groups",[])
   if any(leg.get("leg_class")=="auxiliary" for leg in group.get("legs",[]))
  }
  self.assertEqual(seen,declared)
 def unresolvable_arbiter_route(self):
  # SD-102 does not cap a map-worker anchor's consumer count and the topology
  # check does not count it, so one registry edit reaches this shape.
  def leg(i,node_id,cls):
   return {"id":node_id,"depends_on":["seed"],"kind":"map-worker",
           "completion_gate":f"gate-{node_id}","dispatch_depth":2,
           "parallel_group":"map","parallel_leg_index":i,"parallel_anchor":"map",
           "leg_class":cls}
  return {"dispatch_contract_version":3,"route_id":"rt-m3",
          "route_hash":"sha256:"+"c"*64,"registry_digest":"sha256:"+"d"*64,
          "nodes":[
           {"id":"seed","depends_on":[],"kind":"pipeline-stage",
            "completion_gate":"gate-seed","dispatch_depth":2},
           leg(0,"map","peer"),leg(1,"map-alt","peer"),leg(2,"map-aux","auxiliary"),
           {"id":"consumer-a","depends_on":["map"],"kind":"pipeline-stage",
            "completion_gate":"gate-a","dispatch_depth":2},
           {"id":"consumer-b","depends_on":["map"],"kind":"pipeline-stage",
            "completion_gate":"gate-b","dispatch_depth":2},
           {"id":"unrelated","depends_on":["seed"],"kind":"pipeline-stage",
            "completion_gate":"gate-u","dispatch_depth":2}]}
 def test_m3_unresolvable_arbiter_does_not_block_unrelated_completions(self):
  # M3: `_validate_auxiliary_arbiter` runs on EVERY node's completion, so a
  # single group's declaration error used to refuse the completion of nodes that
  # arbitrate nothing -- one local error became a route-wide halt. The read-only
  # observer already degraded it to a failing row; only the writer raised, and
  # that asymmetry was the defect.
  route=self.unresolvable_arbiter_route()
  with self.assertRaisesRegex(ValueError,"auxiliary-arbiter-ambiguous"):
   R._resolve_auxiliary_arbiter(route,"map")
  self.assertIn("map",R.owner_merge_auxiliary_groups(route))
  with tempfile.TemporaryDirectory() as td:
   evidence=Path(td)/"out.md"; evidence.write_text("done\n",encoding="utf-8")
   for node_id in ("unrelated","consumer-a"):
    with self.subTest(node=node_id):
     node=next(n for n in route["nodes"] if n["id"]==node_id)
     R._validate_auxiliary_arbiter(route,node,evidence)
   # a group with no resolvable arbiter is arbitrated by nobody
   self.assertEqual(R._auxiliary_groups_arbitrated_by(route,"consumer-a"),([],0))
   # and a RESOLVABLE node arbiter is still gated, so this narrowed the raise
   # rather than removing it
   good=self.research_route()
   synthesis=next(n for n in good["nodes"] if n["id"]=="synthesis")
   with self.assertRaisesRegex(ValueError,"auxiliary_findings_considered"):
    R._validate_auxiliary_arbiter(good,synthesis,evidence)
 def test_ac5_auxiliary_arbiter_verdict_length_gate(self):
  # AC 5 (front half): a NODE arbiter's verdict must carry
  # auxiliary_findings_considered with one entry per realized auxiliary leg it
  # arbitrates. G1 regression assertion: the group's own anchor is NOT gated --
  # it is a concurrent sibling of the auxiliary, and gating it made all six
  # realized groups uncompletable. The evidence surface is the sealed markdown
  # output, so frontmatter is read as well as JSON.
  route=self.research_route()
  arbiter=next(n for n in route["nodes"] if n["id"]=="synthesis")
  anchor=next(n for n in route["nodes"] if n["id"]=="retrieval")
  auxiliary=next(n for n in route["nodes"] if n["id"]=="retrieval-assumption")
  import tempfile
  with tempfile.TemporaryDirectory() as td:
   good=Path(td)/"evidence.json"
   good.write_text(json.dumps({"auxiliary_findings_considered":["accepted"]}),encoding="utf-8")
   R._validate_auxiliary_arbiter(route,arbiter,good)
   # markdown frontmatter (inline list) is the real sealed output surface
   md_inline=Path(td)/"round_1.md"
   md_inline.write_text("---\nauxiliary_findings_considered: [accepted]\n---\n# synthesis\n\nverdict: clean\n",encoding="utf-8")
   R._validate_auxiliary_arbiter(route,arbiter,md_inline)
   # markdown frontmatter (yaml block list) is accepted as well; length still counts
   md_block=Path(td)/"round_block.md"
   md_block.write_text("---\nauxiliary_findings_considered:\n  - accepted\n  - noted\n---\nbody\n",encoding="utf-8")
   with self.assertRaisesRegex(ValueError,"auxiliary_findings_considered length 1"):
    R._validate_auxiliary_arbiter(route,arbiter,md_block)
   bad=Path(td)/"bad.json"
   bad.write_text(json.dumps({"auxiliary_findings_considered":["accepted","missing"]}),encoding="utf-8")
   with self.assertRaisesRegex(ValueError,"auxiliary_findings_considered length 1"):
    R._validate_auxiliary_arbiter(route,arbiter,bad)
   missing=Path(td)/"missing.json"
   missing.write_text(json.dumps({"verdict":"clean"}),encoding="utf-8")
   with self.assertRaisesRegex(ValueError,"auxiliary_findings_considered"):
    R._validate_auxiliary_arbiter(route,arbiter,missing)
   # G1 regression: the anchor and every other leg of the arbitrated group
   # complete with NO key at all. This is the assertion that fails if the gate
   # is ever moved back onto the anchor.
   for node in (anchor,auxiliary):
    R._validate_auxiliary_arbiter(route,node,missing)
   for node in route["nodes"]:
    if node.get("parallel_group")=="retrieval":
     R._validate_auxiliary_arbiter(route,node,missing)
 def _arbitration_evidence(self,directory,name,entries):
  path=Path(directory)/name
  body="".join(f"  - {item}\n" for item in entries)
  path.write_text(f"---\nauxiliary_findings_considered:\n{body}---\nowner merge record\n",encoding="utf-8")
  return path
 def _join_group(self,route,group_id,directory,*,skip=(),link=True):
  """Publish a canonical completion marker for every realized leg of a group.

  M7: `write_completion_marker` alone leaves a marker that passes the identity
  row but NOT `completion_marker_is_current` -- no attempt-link sidecar. That is
  exactly the gap `arbitrate` used to accept, so the join here publishes the
  sidecar too and `link=False` reproduces the weaker marker on demand.
  """
  markers=[]
  for node in route["nodes"]:
   if node.get("parallel_group")!=group_id or node["id"] in skip: continue
   evidence=Path(directory)/f"{node['id']}.md"
   evidence.write_text(f"{node['id']} leg output\n",encoding="utf-8")
   attempt_id=f"att-fixture-{node['id']}"
   metadata={
     "attempt_schema_version":2,
     "dispatch_depth":node["dispatch_depth"],
     "transport":"headless",
     "execution_surface":"registered-headless",
     "registered_worker":"1",
     "fallback_hop":"same-harness-headless",
   }
   marker=R.write_completion_marker(
    route,node,node["id"],evidence,attempt_id=attempt_id,attempt_metadata=metadata)
   markers.append(marker)
   if not link: continue
   completion=R.completion_dir(route["route_id"])
   safe="".join(c if c.isalnum() or c in "._-" else "_" for c in attempt_id)
   R.atomic_write(completion/f"{node['id']}.{safe}.attempt.json",{
    "schema_version":2,"route_id":route["route_id"],"node_id":node["id"],
    "attempt_id":attempt_id,"dispatch_depth":marker["dispatch_depth"],
    "transport":marker["transport"],"execution_surface":marker["execution_surface"],
    "registered_worker":marker["registered_worker"],"fallback_hop":marker["fallback_hop"],
    "evidence_sha256":marker["evidence"]["sha256"],
    "completion_marker":str(completion/f"{node['id']}.json"),
    "completion_marker_history":str(completion/f"{node['id']}.{marker['sequence']}.json"),
   })
  return markers
 def test_ac5_owner_merge_arbitration_transaction(self):
  # G1 (c)/(f): the owner-merge arbiter registers the merge record through the
  # `arbitrate` transaction, and it is structurally impossible to satisfy while
  # the group's legs are still running -- which is exactly why gating the
  # concurrently-running anchor could never work.
  route=R.compile_route(**self.args(requested_intensity="thorough",predicates=[],signals=["shared-contract"],transport="headless",inline_reason=None,dispatch_evidence=self.dispatch(self.nested())))
  self.assertEqual(R._resolve_auxiliary_arbiter(route,"plan-check"),("owner-merge",None))
  import tempfile
  with tempfile.TemporaryDirectory() as td:
   merge=self._arbitration_evidence(td,"merge_record.md",["simplicity finding adopted"])
   # 4. before join: some leg has no canonical completion marker yet
   with self.assertRaisesRegex(ValueError,"auxiliary-arbitration-before-join:"):
    R.arbitrate_group(route,"plan-check",merge)
   # M7: a marker that passes the identity row but NOT the canonical
   # `completion_marker_is_current` contract is not a join either. Accepting it
   # let the arbitration record be written over a marker a dependent's
   # start-gate then refuses as an absent canonical marker, so the record
   # attested a join that downstream did not recognize. (Prose, not the literal
   # refusal token -- `dispatch_completion_marker.test.py`'s static guardian
   # scans this tree for it and each allowlist entry weakens that guardian.)
   self._join_group(route,"plan-check",td,link=False)
   for member in R._group_members(route,"plan-check"):
    node_id=str(member["id"])
    self.assertTrue(R._marker_identity_row(
     route,member,node_id,member.get("completion_gate"))["passed"])
    self.assertFalse(R.completion_marker_is_current(
     route,member,R.completion_dir(route["route_id"])/f"{node_id}.json"))
   with self.assertRaisesRegex(ValueError,"auxiliary-arbitration-before-join:"):
    R.arbitrate_group(route,"plan-check",merge)
   self._join_group(route,"plan-check",td)
   # 5. length mismatch and key absence are each their own refusal
   wrong=self._arbitration_evidence(td,"wrong.md",["a","b"])
   with self.assertRaisesRegex(ValueError,"auxiliary_findings_considered length 1"):
    R.arbitrate_group(route,"plan-check",wrong)
   keyless=Path(td)/"keyless.md"; keyless.write_text("no frontmatter\n",encoding="utf-8")
   with self.assertRaisesRegex(ValueError,"auxiliary_findings_considered"):
    R.arbitrate_group(route,"plan-check",keyless)
   # 6. write-once: one record, and an identical re-call is idempotent
   record=R.arbitrate_group(route,"plan-check",merge)
   self.assertEqual(record["arbiter"],"owner-merge")
   self.assertEqual(record["anchor_node"],"plan-check")
   self.assertEqual(record["auxiliary_nodes"],["plan-check-simplicity"])
   self.assertEqual(record["auxiliary_findings_considered"],["simplicity finding adopted"])
   path=R.arbitration_path(route["route_id"],"plan-check")
   self.assertTrue(path.is_file())
   again=R.arbitrate_group(route,"plan-check",merge)
   self.assertEqual(again,record)
   self.assertEqual(len(list(path.parent.glob("*.arbitration.json"))),1)
   # a different merge record for the same group is a conflict, never a rewrite
   other=self._arbitration_evidence(td,"other.md",["different judgement"])
   with self.assertRaisesRegex(ValueError,"auxiliary-arbitration-identity-conflict"):
    R.arbitrate_group(route,"plan-check",other)
 def test_ac5_arbitrate_refuses_unknown_and_node_arbiter_groups(self):
  # G1 (c) 1..3: each precondition has its own typed refusal.
  route=self.research_route()
  import tempfile
  with tempfile.TemporaryDirectory() as td:
   merge=self._arbitration_evidence(td,"merge_record.md",["x"])
   with self.assertRaisesRegex(ValueError,"auxiliary-group-unknown:not-a-group"):
    R.arbitrate_group(route,"not-a-group",merge)
   # `claim-verify` is a realized group with no auxiliary leg
   self.assertTrue(R._group_members(route,"claim-verify"))
   self.assertFalse(R._realized_auxiliary_nodes(route,"claim-verify"))
   with self.assertRaisesRegex(ValueError,"auxiliary-group-has-no-auxiliary-leg:claim-verify"):
    R.arbitrate_group(route,"claim-verify",merge)
   # a node-arbitrated group refuses the owner transaction and says who owns it
   with self.assertRaisesRegex(ValueError,"auxiliary-arbiter-is-node:synthesis"):
    R.arbitrate_group(route,"retrieval",merge)
 def test_ac5_terminal_gate_observation_covers_unarbitrated_groups(self):
  # G1 (d) 2: an owner-merge group that was never arbitrated lands as a failed
  # row in the route's completion truth, so `terminal_gate_proven` is false --
  # and `close_route` still closes, honestly, without raising.
  route=R.compile_route(**self.args(requested_intensity="thorough",predicates=[],signals=["shared-contract"],transport="headless",inline_reason=None,dispatch_evidence=self.dispatch(self.nested())))
  gates=R.terminal_gate_observation(route)
  self.assertIn("parallel_group:plan-check",gates)
  self.assertFalse(gates["parallel_group:plan-check"]["passed"])
  self.assertEqual(gates["parallel_group:plan-check"]["reason"],"completion-marker-absent")
  self.assertIs(R.terminal_gate_proven(gates),False)
  import tempfile
  with tempfile.TemporaryDirectory() as td:
   self._join_group(route,"plan-check",td)
   merge=self._arbitration_evidence(td,"merge_record.md",["adopted"])
   R.arbitrate_group(route,"plan-check",merge)
   passed=R.terminal_gate_observation(route)["parallel_group:plan-check"]
   self.assertTrue(passed["passed"])
   self.assertEqual(passed["reason"],"completion-marker-verified")
   # tampering with the merge record after registration is caught by hash
   merge.write_text("---\nauxiliary_findings_considered:\n  - rewritten\n---\n",encoding="utf-8")
   tampered=R.terminal_gate_observation(route)["parallel_group:plan-check"]
   self.assertFalse(tampered["passed"])
   self.assertEqual(tampered["reason"],"completion-evidence-hash-mismatch")
 def test_d3a_terminal_and_continuation_regressions(self):
  # resource-runner terminal stays forbidden under the new classification, and a
  # non-terminal node without a continuation still fails closed.
  registry=R.TOPO.load_registry()
  recipe=R.TOPO.resolve_recipe(registry,"autopilot-code","dev")
  nodes=json.loads(json.dumps(recipe["standard_plus"]["nodes"]))
  runner=dict(id="detached",kind="resource-runner",dispatch_depth=0,resource_transport="detached-process",
   terminal=True,terminal_gate="detached-gate",inputs=["x"],outputs=["y"],write_scope=["source/**"],
   completion_gate="detached-gate")
  with self.assertRaisesRegex(ValueError,"detached resource run"):
   R._workflow_contract(registry,nodes+[runner],[])
  stripped=json.loads(json.dumps(nodes))
  for node in stripped:
   if node.get("kind") not in ("capability-owner","resource-runner"):
    node["continuation"]=None
  with self.assertRaisesRegex(ValueError,"declares no valid continuation"):
   R._workflow_contract(registry,stripped,[])
 def test_standard_plus_without_checked_headless_evidence_fails_closed(self):
  with self.assertRaisesRegex(ValueError,"checked dispatch evidence required"):
   R.compile_route(**self.args(signals=["public-api"],inline_reason=None))
 def test_tracking_gate(self):
  self.assertRaisesRegex(ValueError,"tracked gate evidence",R.compile_route,**self.args(tracked_gate_evidence={}))
 def test_hash_detects_mutation(self):
  a=R.compile_route(**self.args()); a["cwd"]="/tmp"; self.assertRaises(ValueError,R.verify_route,a)
 def test_verify_rejects_declared_max_below_realized_dispatch_depth(self):
  route=self.compile_v3(self.dispatch(self.nested()))
  route["max_dispatch_depth"]=1
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"max_dispatch_depth"):
   R.verify_route(route,R.ROOT)
 def test_write_once(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/"route.json"; a=R.compile_route(**self.args()); R.write_once(p,a); R.write_once(p,a)
 def test_v3_direct_surface_and_fallback_order(self):
  evidence=self.dispatch(self.nested(status="unsupported",failure="nested-network-unconfirmed"),self.nested(child="claude"))
  # nodes[0] is now the depth-1 `frame` leg, which carries no fallback chain
  # by design (recovery from a dead frame leg is an explicit depth-0
  # re-launch). Read the chain off the first node that has one.
  route=self.compile_v3(evidence)
  chain=next(n for n in route["nodes"] if n.get("fallback_hops"))["fallback_hops"]
  self.assertEqual(route["dispatch_contract_version"],3)
  self.assertEqual(route["dispatch_evidence_scope_version"],1)
  self.assertNotIn("broker_contract_version",route)
  self.assertEqual([row["fallback_hop"] for row in chain],R.FALLBACK_ORDER)
  self.assertEqual(chain[1]["candidates"][0]["launch_authority"],"conductor")
  self.assertNotIn("broker_root",route["dispatch_evidence"]["tuples"][0])
  R.verify_route(route,R.ROOT)
 def test_checked_worktree_must_equal_route_cwd(self):
  row=self.nested(); row["checked_worktree"]="/tmp/not-the-route-worktree"
  with self.assertRaisesRegex(ValueError,"dispatch-evidence-worktree-mismatch"):
   self.compile_v3(self.dispatch(row))
 def test_pre_scope_v3_route_remains_verifiable_for_migration_close(self):
  route=json.loads(json.dumps(self.compile_v3(self.dispatch(self.nested()))))
  route.pop("dispatch_evidence_scope_version")
  for row in route["dispatch_evidence"]["tuples"]:
   for field in R.NESTED_SCOPE_FIELDS: row.pop(field,None)
  for node in route["nodes"]:
   for hop in node.get("fallback_hops",[]):
    for row in hop.get("candidates",[]):
     for field in R.NESTED_SCOPE_FIELDS: row.pop(field,None)
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  R.verify_route(route,R.ROOT)
 def test_worktree_local_unsupported_requires_reprobe_before_fallback(self):
  local=self.nested(
   status="unsupported", failure="invalid-worktree-codex-mount-target")
  global_fallback=self.nested(parent="codex",child="claude")
  with self.assertRaisesRegex(
   ValueError,"dispatch-evidence-exact-worktree-reprobe-required"):
   self.compile_v3(self.dispatch(local,global_fallback))
 def test_unknown_nested_tuple_fails_closed(self):
  with self.assertRaisesRegex(ValueError,"no supported direct headless tuple"):
   self.compile_v3(self.dispatch(self.nested(status="unknown",failure="unprobed-tuple")))
 def test_native_subagent_prohibition_never_authorizes_inline_execution(self):
  evidence={
   "tuples":[self.nested(
    status="unsupported",failure="nested-network-unconfirmed")],
   "native_subagent":[{
    "harness":"codex","transport":"headless",
    "execution_surface":"codex-native-subagent","registered_worker":False,
    "status":"unsupported","check_source":"user-policy",
    "failure_class":"user-disabled",
   }],
  }
  with self.assertRaisesRegex(ValueError,"no supported direct headless tuple"):
   self.compile_v3(evidence)
 def test_rehashed_undeclared_fanout_node_is_rejected(self):
  route=self.compile_v3(self.dispatch(self.nested()))
  rogue=json.loads(json.dumps(route["nodes"][0]))
  rogue["id"]="undeclared-fanout"
  rogue["parallel_group"]="undeclared-group"
  route["nodes"].append(rogue)
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"route nodes differ from the declared recipe"):
   R.verify_route(route,R.ROOT)
 def test_depth0_parent_transport_is_rejected_at_compile(self):
  # 2026-08-04 cairn: an interactive main session probed with its OWN
  # transport, so every dispatch-depth-2 hop failed at launch and the whole
  # standard cycle ran inline. The tuple describes the depth-1 owner, never
  # the probing caller, so it can only ever be headless.
  row=self.nested(); row["parent_transport"]="interactive"
  with self.assertRaisesRegex(ValueError,"dispatch-evidence-parent-transport-mismatch"):
   self.compile_v3(self.dispatch(row))
 def test_parent_identity_axes_close_symmetrically(self):
  # sandbox (2026-07-31) and harness are the other two fields of the same
  # tuple; a per-axis patch is what let this recur.
  for field,value,expected in (
    ("parent_sandbox","none","dispatch-evidence-parent-sandbox-unknown"),
    ("parent_sandbox","adapter-default","dispatch-evidence-parent-sandbox-unknown"),
    ("parent_harness","gemini","dispatch-evidence-parent-harness-unknown"),
    ("child_harness","gemini","dispatch-evidence-child-harness-unknown"),
  ):
   row=self.nested(); row[field]=value
   with self.subTest(field=field,value=value),self.assertRaisesRegex(ValueError,expected):
    self.compile_v3(self.dispatch(row))
 def test_headless_evidence_compiles_and_verifies_unchanged(self):
  route=self.compile_v3(self.dispatch(self.nested(),self.nested(child="claude")))
  R.verify_route(route,R.ROOT)
  self.assertEqual(
   {row["parent_transport"] for row in route["dispatch_evidence"]["tuples"]},{"headless"})
 def test_sealed_route_cannot_be_edited_into_a_depth0_parent(self):
  # verify() must reach the same verdict as compile(); otherwise a route
  # sealed before this gate stays launchable.
  route=self.compile_v3(self.dispatch(self.nested()))
  for row in route["dispatch_evidence"]["tuples"]: row["parent_transport"]="interactive"
  for node in route["nodes"]:
   for hop in node.get("fallback_hops",[])[:2]:
    for row in hop.get("candidates",[]): row["parent_transport"]="interactive"
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"dispatch-evidence-parent-transport-mismatch"):
   R.verify_route(route,R.ROOT)
 def test_quick_registered_headless_evidence_is_untouched_by_parent_identity(self):
  # quick candidates carry no parent_* fields at all and must not be dragged
  # through the depth-2 validator.
  route=R.compile_route(**self.args(
   predicates=[],transport=None,inline_reason=None,
   registered_headless_evidence=self.registered_headless()))
  self.assertEqual(route["effective_intensity"],"quick")
  self.assertIsNone(route["dispatch_evidence"])
  self.assertTrue(all(
   "parent_transport" not in row for row in route["registered_headless_candidates"]))
  R.verify_route(route,R.ROOT)
 def test_native_evidence_cannot_masquerade_as_teammate_or_wrong_surface(self):
  for bad in (
   {"harness":"claude","transport":"headless",
    "execution_surface":"claude-agent-team-teammate","registered_worker":False,
    "status":"supported","check_source":"fixture"},
   {"harness":"codex","transport":"interactive",
    "execution_surface":"codex-native-subagent","registered_worker":False,
    "status":"supported","check_source":"fixture"},
  ):
   evidence={"tuples":[self.nested()],"native_subagent":[bad]}
   with self.subTest(surface=bad["execution_surface"]),self.assertRaisesRegex(
    ValueError,"invalid native subagent evidence"
   ):
    self.compile_v3(evidence)
 def test_v3_rejects_broker_fields(self):
  row=self.nested(); row["broker_root"]="/tmp/broker"
  with self.assertRaisesRegex(ValueError,"must not carry broker fields"): self.compile_v3(self.dispatch(row))
 def test_fallback_candidates_must_exactly_match_checked_evidence(self):
  route=self.compile_v3(self.dispatch(self.nested(parent="claude",child="claude")))
  # nodes[0] is the depth-1 `frame` leg now and carries no fallback chain;
  # take the first node that has one.
  candidate=next(n for n in route["nodes"] if n.get("fallback_hops"))["fallback_hops"][0]["candidates"][0]
  candidate["child_harness"]="opencode"
  route["route_hash"]=R.route_hash(route); route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"differs from checked evidence"):
   R.verify_route(route,R.ROOT)
 def test_legacy_v2_and_v1_are_read_only(self):
  v3=self.compile_v3(self.dispatch(self.nested()))
  v2=self.legacy_v2(v3)
  with self.assertRaises(ValueError): R.verify_route(v2,R.ROOT)
  v1=json.loads(json.dumps(v2)); v1["broker_contract_version"]=1
  for row in v1["dispatch_evidence"]["tuples"]: row["broker_instance"]="brk-fixture"
  for node in v1["nodes"]:
   for hop in node.get("fallback_hops",[])[:2]:
    for row in hop.get("candidates",[]): row["broker_instance"]="brk-fixture"
  v1["route_hash"]=R.route_hash(v1); v1["route_id"]="rt-"+v1["route_hash"].split(":",1)[1][:16]
  with self.assertRaises(ValueError): R.verify_route(v1,R.ROOT)
 def _standard(self):
  return R.compile_route(**self.args(
   signals=["public-api"],transport="headless",inline_reason=None,
   dispatch_evidence=self.dispatch(self.nested())))
 def test_seal_stamps_valid_affinity_and_digest(self):
  with dispatch_defaults_config(DD_CONFIG_A):
   route=self._standard()
  # harness_affinity is a dispatch-depth-2 stage allocation. The frame pair is
  # now depth-1 and launched by the depth-0 session itself, so it is outside
  # that table and carries no affinity cell at all -- assert that rather than
  # silently dropping the two ids from the census.
  for leg in ("frame","frame-alternative"):
   node=next(n for n in route["nodes"] if n["id"]==leg)
   self.assertEqual(node["dispatch_depth"],1)
   self.assertNotIn("harness_affinity",node)
  by_id={n["id"]:n["harness_affinity"] for n in route["nodes"] if n.get("dispatch_depth")==2}
  self.assertEqual(set(by_id),{"plan","plan-check","execute","impl-review","test","report"})
  for value in by_id.values(): self.assertIn(value,R.VALID_AFFINITY)
  # DD_CONFIG_A leaves these cells sparse; the shipped
  # profiles/dispatch-defaults.yaml capability baseline now merges beneath
  # the user file, so they answer "diverse" instead of "unspecified".
  self.assertEqual(by_id["plan"],"diverse")
  self.assertEqual(by_id["plan-check"],"diverse")
  self.assertEqual(by_id["impl-review"],"diverse")
  # execute/test/report are explicit user cells in DD_CONFIG_A and stay put —
  # this is the in-suite proof that a user cell outranks the baseline.
  self.assertEqual(by_id["execute"],"codex")
  self.assertEqual(by_id["test"],"diverse")
  self.assertEqual(by_id["report"],"claude")
  self.assertIsNotNone(route["dispatch_defaults_digest"])
  self.assertEqual(route["dispatch_allocation"]["strategy"],"config-order")
  self.assertEqual(route["dispatch_allocation"]["harness_order"],["claude","codex"])
 def test_seal_hash_changes_with_config_value_not_formatting(self):
  with dispatch_defaults_config(DD_CONFIG_A):
   a=self._standard()
  with dispatch_defaults_config(DD_CONFIG_B):
   b=self._standard()
  self.assertNotEqual(a["route_hash"],b["route_hash"])
  with dispatch_defaults_config(DD_CONFIG_A_COMMENTED):
   a2=self._standard()
  self.assertEqual(a["route_hash"],a2["route_hash"])
  self.assertEqual(a["dispatch_defaults_digest"],a2["dispatch_defaults_digest"])
 def test_seal_survives_post_compile_config_change(self):
  with dispatch_defaults_config(DD_CONFIG_A):
   route=self._standard()
  with dispatch_defaults_config(DD_CONFIG_B):
   R.verify_route(route,R.ROOT)
 def test_verify_accepts_legacy_three_key_allocation_and_defaults_gate(self):
  with dispatch_defaults_config(DD_CONFIG_A):
   route=self._standard()
  legacy=json.loads(json.dumps(route))
  legacy["dispatch_allocation"].pop("usage_gate_used_percent",None)
  legacy["route_hash"]=R.route_hash(legacy)
  legacy["route_id"]="rt-"+legacy["route_hash"].split(":",1)[1][:16]
  R.verify_route(legacy,R.ROOT)

 def test_seal_round_trip_carries_depth_affinity_policy_and_accepts_legacy_shapes(self):
  with dispatch_defaults_config(DD_CONFIG_A):
   route=self._standard()
  allocation=route["dispatch_allocation"]
  allocation.update({"depth_affinity":{"owner":"claude","worker":"codex"},
                     "depth_affinity_weight":.65,"usage_headroom_exponent":2})
  route["route_hash"]=R.route_hash(route); route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  R.verify_route(route,R.ROOT)
  for keys in (("strategy","window","harness_order"),
               ("strategy","window","usage_gate_used_percent","harness_order")):
   legacy=json.loads(json.dumps(route)); legacy["dispatch_allocation"]={k:allocation[k] for k in keys}
   legacy["route_hash"]=R.route_hash(legacy); legacy["route_id"]="rt-"+legacy["route_hash"].split(":",1)[1][:16]
   R.verify_route(legacy,R.ROOT)

 def test_verify_rejects_invalid_new_allocation_fields(self):
  with dispatch_defaults_config(DD_CONFIG_A): route=self._standard()
  for key,value in (("depth_affinity_weight",True),("depth_affinity_weight",1.2),
                    ("depth_affinity",{"stage":"codex"}),
                    ("depth_affinity",{"owner":"not-a-harness"}),
                    ("usage_headroom_exponent",0)):
   bad=json.loads(json.dumps(route)); bad["dispatch_allocation"][key]=value
   bad["route_hash"]=R.route_hash(bad); bad["route_id"]="rt-"+bad["route_hash"].split(":",1)[1][:16]
   with self.assertRaisesRegex(ValueError,"invalid dispatch_allocation"):
    R.verify_route(bad,R.ROOT)

 def test_verify_rejects_short_balanced_window(self):
  with dispatch_defaults_config(DD_CONFIG_A):
   route=self._standard()
  route["dispatch_allocation"]={
   "strategy":"balanced", "window":2, "harness_order":["claude","codex"]}
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"invalid dispatch_allocation window"):
   R.verify_route(route,R.ROOT)
 def test_seal_backcompat_old_route_without_fields(self):
  with dispatch_defaults_config(DD_CONFIG_A):
   route=self._standard()
  legacy=json.loads(json.dumps(route))
  for node in legacy["nodes"]: node.pop("harness_affinity",None)
  legacy.pop("dispatch_defaults_digest",None)
  legacy.pop("dispatch_allocation",None)
  legacy["route_hash"]=R.route_hash(legacy); legacy["route_id"]="rt-"+legacy["route_hash"].split(":",1)[1][:16]
  R.verify_route(legacy,R.ROOT)
 def test_seal_forged_vocabulary_fails(self):
  with dispatch_defaults_config(DD_CONFIG_A):
   route=self._standard()
  route["nodes"][0]["harness_affinity"]="gpt"
  route["route_hash"]=R.route_hash(route); route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"invalid harness_affinity vocabulary"):
   R.verify_route(route,R.ROOT)
 def test_seal_absent_config_all_unspecified_digest_none(self):
  with tempfile.TemporaryDirectory() as td:
   with dispatch_defaults_config_path(Path(td)/"does-not-exist.yaml"):
    route=self._standard()
  self.assertIsNone(route["dispatch_defaults_digest"])
  self.assertIsNone(route["dispatch_allocation"])
  # Only depth-2 stage nodes carry an affinity cell; the depth-1 frame pair is
  # allocated by the depth-0 launcher, not by this table.
  for node in route["nodes"]:
   if node.get("dispatch_depth")==2: self.assertEqual(node["harness_affinity"],"unspecified")
   else: self.assertNotIn("harness_affinity",node)
  # T-3: confirmation_mode must NOT ride _seal_dispatch_defaults's
  # (None, None, None) early return for an absent config file -- that would
  # seal confirmation_mode=None instead of the "hybrid" default for exactly
  # the user this config is absent for.
  self.assertEqual(route["confirmation_mode"],"hybrid")
 def test_seal_confirmation_mode_reads_v4_config(self):
  v4_config=(
   "schema_version: 4\n"
   "harnesses:\n  enabled: [claude, codex]\n"
   "profiles:\n"
   + "".join(
     f"  {profile}:\n    primary: [claude, codex]\n    relief: []\n"
     "    last_resort: []\n    promote_relief_below: 0\n"
     for profile in ("deep","balanced-deep","light","mini")
   )
   + "allocation:\n  strategy: capacity-aware\n  window: 30\n"
   "confirmation:\n  mode: post-frame-only\n"
   "capabilities:\n"
  )
  with dispatch_defaults_config(v4_config):
   route=self._standard()
  self.assertEqual(route["confirmation_mode"],"post-frame-only")
 def test_seal_confirmation_mode_defaults_for_v1_config_without_block(self):
  with dispatch_defaults_config(DD_CONFIG_A):
   route=self._standard()
  self.assertEqual(route["confirmation_mode"],"hybrid")
 def test_seal_corrupt_config_fails_loud(self):
  with dispatch_defaults_config(DD_CONFIG_CORRUPT):
   with self.assertRaisesRegex(ValueError,"corrupt dispatch-defaults config"):
    self._standard()
 def _composed_recipe(self):
  recipe=json.loads(json.dumps(R.TOPO.resolve_recipe(R.TOPO.load_registry(),"autopilot-code","dev")))
  recipe["modes"]=["composed-fixture"]
  # This copied/modified recipe is ad-hoc, so each stage supplies its demand.
  for node in recipe["standard_plus"]["nodes"]:
   if node.get("kind") == "resource-runner": continue
   profile=node["model_profile"]
   node["profile_demand"]={"schema_version":1,
    "judgment_requirement":"difficult-uncertain" if profile=="deep" else "important" if profile=="balanced-deep" else "predetermined",
    "execution_scope":"extended-multistep" if profile=="balanced" else "short-local",
    "judgment_reason":"Fixture preserves the declared judgment.",
    "execution_reason":"Fixture performs its declared steps.","evidence_refs":["fixture.md"]}
  for group in recipe["standard_plus"].get("parallel_groups",[]):
   for leg in group.get("legs",[]):
    profile=leg["model_profile"]
    leg["profile_demand"]={"schema_version":1,
     "judgment_requirement":"difficult-uncertain" if profile=="deep" else "important" if profile=="balanced-deep" else "predetermined",
     "execution_scope":"short-local","judgment_reason":"Fixture declared judgment.",
     "execution_reason":"Fixture bounded steps.","evidence_refs":["fixture.md"]}
  return recipe
 def _composed(self,recipe=None):
  return R.compile_composed_route(
   recipe or self._composed_recipe(),"composed-fixture","strong",R.ROOT,R.ROOT,
   predicates=[],signals=["shared-contract"],transport="headless",
   tracking="tracked",tracked_gate_evidence=self.args()["tracked_gate_evidence"],
   dispatch_evidence=self.dispatch(self.nested()))
 def test_composed_round_trip_and_tamper_rejection(self):
  route=self._composed()
  self.assertIs(route["composed"],True)
  self.assertEqual(route["composed_recipe"]["modes"],["composed-fixture"])
  R.verify_route(route,R.ROOT)
  tampered=json.loads(json.dumps(route))
  tampered["nodes"][0]["unit"]="dev/backend"
  tampered["route_hash"]=R.route_hash(tampered)
  tampered["route_id"]="rt-"+tampered["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"composed route nodes differ"):
   R.verify_route(tampered,R.ROOT)
 def test_composed_requires_standard_plus(self):
  with self.assertRaisesRegex(ValueError,"standard\\+ effective intensity"):
   R.compile_composed_route(
    self._composed_recipe(),"composed-fixture","direct",R.ROOT,R.ROOT,
    predicates=ALL,tracking="tracked",tracked_gate_evidence=self.args()["tracked_gate_evidence"])
 def test_composed_spec_touch_gate(self):
  recipe=self._composed_recipe()
  execute=next(n for n in recipe["standard_plus"]["nodes"] if n["id"]=="execute")
  execute["write_scope"]=["spec/**","checklist.md","dev_logs/**"]
  execute["guard_preconditions"]=["artifact-order-prechecked"]
  route=self._composed(recipe)
  self.assertTrue(route["spec_touch"])
  R.verify_route(route,R.ROOT)
 def test_composed_invalid_recipe_fails_closed(self):
  recipe=self._composed_recipe()
  recipe["standard_plus"]["nodes"][0]["unit"]="dev/does-not-exist"
  with self.assertRaisesRegex(ValueError,"unknown unit"):
   self._composed(recipe)
 def test_unit_catalog_digest_staleness(self):
  route=self._standard()
  self.assertTrue(route["unit_catalog_digest"].startswith("sha256:"))
  stale=json.loads(json.dumps(route))
  stale["unit_catalog_digest"]="sha256:"+"0"*64
  stale["route_hash"]=R.route_hash(stale)
  stale["route_id"]="rt-"+stale["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"stale unit catalog digest"):
   R.verify_route(stale,R.ROOT)
  legacy=json.loads(json.dumps(route))
  legacy.pop("unit_catalog_digest")
  legacy["route_hash"]=R.route_hash(legacy)
  legacy["route_id"]="rt-"+legacy["route_hash"].split(":",1)[1][:16]
  R.verify_route(legacy,R.ROOT)
 def test_stale_route_close_can_record_outcome_after_output_scope_rule_changes(self):
  route=self.compile_v3(self.dispatch(self.nested()))
  replica=next(node for node in route["nodes"] if node["id"]=="frame-alternative")
  replica["outputs"]=["shards/frame/direction-brief.alternative.md"]
  route["registry_digest"]="sha256:"+"0"*64
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"stale registry digest"):
   R.verify_route(route,R.ROOT)
  verified=R.verify_route(route,R.ROOT,allow_stale_registry=True)
  self.assertIs(verified["_registry_current"],False)
 def test_close_writes_an_idempotent_outcome_sidecar(self):
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp); route=dict(route); route["artifact_root"]=str(artifact_root)
   path=artifact_root/"demo-route.json"; path.write_text(json.dumps(route),encoding="utf-8")
   outcome,created=R.close_route(route,path,commit="0"*40,summary="demo",allow_unproven=True)
   self.assertTrue(created); self.assertTrue(R.outcome_path(path).is_file())
   self.assertEqual(outcome["route_hash"],route["route_hash"]); self.assertEqual(outcome["route_id"],route["route_id"])
   self.assertEqual(outcome["head_commit"],"0"*40); self.assertEqual(outcome["summary"],"demo")
   self.assertEqual(outcome["schema_version"],3)
   self.assertFalse(outcome["terminal_gate_proven"])
   self.assertEqual(outcome["terminal_gates"]["inline"]["reason"],"completion-marker-absent")
   before=R.outcome_path(path).read_bytes()
   again,created_again=R.close_route(route,path,commit="1"*40)
   self.assertFalse(created_again); self.assertEqual(again["head_commit"],"0"*40)
   self.assertEqual(again["schema_version"],3)
   self.assertEqual(again["terminal_gate_proven"],False)
   # Idempotent re-close must not recompute: the sidecar's exact bytes are unchanged.
   self.assertEqual(R.outcome_path(path).read_bytes(),before)
 def test_close_before_complete_is_refused_by_default(self):
  # C-25c: `close` used to seal `terminal_gate_proven=false` permanently for
  # any route closed before its terminal node completed -- finalize could
  # never prove the gate afterward even once `complete` actually ran. The
  # default contract is now a typed refusal that writes no sidecar at all,
  # so `complete` (which must run first) is not locked out by an early close.
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   compiled=self._run_compile_cli(self._compile_cli_args(artifact_root))
   self.assertEqual(compiled.returncode,0,compiled.stderr)
   route=json.loads(compiled.stdout)
   route_path=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   result=subprocess.run([sys.executable,str(P),"close","--route",str(route_path)],
                         capture_output=True,text=True,cwd=str(R.ROOT))
   self.assertEqual(result.returncode,64,result.stdout)
   self.assertIn("route-close-before-complete",result.stderr)
   self.assertFalse(R.outcome_path(route_path).exists())
 def test_close_records_false_and_warns_for_direct_unproven_gate_with_override(self):
  # Red before P2: schema 2 outcomes carry neither `terminal_gate_proven` nor
  # `terminal_gates`, and `close` never printed a warning at all -- this exercises the
  # real CLI so the stderr contract, not just the in-process dict, is covered. Direct
  # routes declare an `inline` terminal but nothing writes its marker in this test, so
  # the aggregate must be `False`, never `None` -- a direct/inline close that silently
  # reported "no terminal node" would hide every unproven direct closure.
  # C-25c: this is now the explicit `--allow-unproven` override path; the
  # default (no flag) is covered by test_close_before_complete_is_refused_by_default.
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   compiled=self._run_compile_cli(self._compile_cli_args(artifact_root))
   self.assertEqual(compiled.returncode,0,compiled.stderr)
   route=json.loads(compiled.stdout)
   route_path=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   result=subprocess.run(
    [sys.executable,str(P),"close","--route",str(route_path),"--allow-unproven"],
    capture_output=True,text=True,cwd=str(R.ROOT))
   self.assertEqual(result.returncode,0,result.stderr)
   outcome=json.loads(result.stdout)
   self.assertEqual(outcome["schema_version"],3)
   self.assertFalse(outcome["terminal_gate_proven"])
   self.assertEqual(outcome["terminal_gates"]["inline"]["reason"],"completion-marker-absent")
   self.assertIn("terminal-gate-unproven",result.stderr)
   self.assertIn(route["route_id"],result.stderr)
   before=R.outcome_path(route_path).read_bytes()
   # Re-close (idempotent) must not recompute: exact bytes unchanged, override
   # not required the second time since the sidecar already exists.
   again=subprocess.run([sys.executable,str(P),"close","--route",str(route_path)],
                        capture_output=True,text=True,cwd=str(R.ROOT))
   self.assertEqual(again.returncode,0,again.stderr)
   self.assertEqual(R.outcome_path(route_path).read_bytes(),before)
 def test_exact_terminal_identity_tracks_bytes_and_latest_attempt(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); jobs=root/"jobs.log"; evidence=root/"result.md"
   route=R.compile_route(**self.args(artifact_root=root, requested_intensity="quick", predicates=[],
       inline_reason=None, registered_headless_evidence=self.registered_headless()))
   # §13.53.2: the flag is no longer a hardcoded constant but a sealed verdict
   # from the runtime-capability census. This test is about terminal identity,
   # not the gate, so it only pins the type -- recomputing the value with the
   # same helper would be a tautology, and comparing against a `config=None`
   # probe would fail whenever an operator has set `runtime.terminal_commit`.
   # The gate's real behavior is pinned in TerminalCommitSupportTests.
   self.assertIsInstance(route["runtime_support"]["terminal_commit"],bool)
   # quick is a three-node route now; the terminal node this test is about is
   # `one-shot`, not nodes[0] (which is the `frame` leg).
   node=next(n for n in route["nodes"] if n.get("terminal")); attempt="att-terminal-current"
   self.assertEqual(node["id"],"one-shot")
   subprocess.run([sys.executable,"-c","pass"],check=True)
   meta=dict(attempt_schema_version=2,dispatch_depth=1,transport="headless",
       execution_surface="registered-headless",registered_worker="1",fallback_hop="same-harness-headless",
       route_id=route["route_id"],route_hash=route["route_hash"],route_node=node["id"],
       attempt_id=attempt,failure_class="pass",note="completed-marker",launch_outcome="reaped-before-publish")
   def row(values):
    return "2026-09-08T00:00:00Z\tdone\t/repo\t/wt\towner\t"+",".join(f"{k}={v}" for k,v in values.items())+"\n"
   jobs.write_text(row(meta)); evidence.write_text("first")
   with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}):
    R._publish_completion_locked(route,node,node["id"],evidence,attempt_id=attempt,attempt_metadata=meta,jobs=jobs)
    first=R.terminal_gate_observation(route,jobs=jobs,exact_terminal=True)[node["id"]]
    self.assertTrue(first["passed"],first)
    before=R.dispatch_terminal_commit.terminal_marker_digest([first])
    evidence.write_text("replaced")
    directory=R.completion_dir(route["route_id"],jobs=jobs)
    for path in directory.glob(f"{node['id']}*.json"):
     record=json.loads(path.read_text())
     if "evidence" in record:record["evidence"]["sha256"]=R.evidence_digest(evidence)
     if "evidence_sha256" in record:record["evidence_sha256"]=R.evidence_digest(evidence)
     path.write_text(json.dumps(record))
    second=R.terminal_gate_observation(route,jobs=jobs,exact_terminal=True)[node["id"]]
    self.assertTrue(second["passed"],second)
    self.assertNotEqual(before,R.dispatch_terminal_commit.terminal_marker_digest([second]))
    jobs.write_text(row(meta)+row(dict(meta,attempt_id="att-terminal-replaced")))
    stale=R.terminal_gate_observation(route,jobs=jobs,exact_terminal=True)[node["id"]]
    self.assertFalse(stale["passed"])
    self.assertEqual(stale["reason"],"completion-attempt-not-current")

 def test_close_records_true_for_verified_terminal_marker(self):
  # Red before P2: the outcome had no gate observation at all, so there was nothing to
  # assert `True` against.
  route=R.compile_route(**self.args())
  node=route["nodes"][0]
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp); route=dict(route); route["artifact_root"]=str(artifact_root)
   path=artifact_root/"demo-route.json"; path.write_text(json.dumps(route),encoding="utf-8")
   evidence=Path(tmp)/"evidence.txt"; evidence.write_text("terminal evidence",encoding="utf-8")
   R.write_completion_marker(route,node,node["id"],evidence)
   outcome,created=R.close_route(route,path,commit="6"*40)
   self.assertTrue(created)
   self.assertEqual(outcome["schema_version"],3)
   self.assertTrue(outcome["terminal_gate_proven"])
   self.assertTrue(outcome["terminal_gates"][node["id"]]["passed"])
   self.assertEqual(outcome["terminal_gates"][node["id"]]["reason"],"completion-marker-verified")
 def test_close_records_null_only_without_terminal_nodes(self):
  # Red before P2: the field was absent entirely, so `None` and `False` were
  # indistinguishable -- this pins that a historical terminal-less route reports `None`,
  # never folded into the `False` used for a declared-but-unproven gate.
  route=json.loads(json.dumps(R.compile_route(**self.args())))
  for node in route["nodes"]:
   node.pop("terminal",None); node.pop("terminal_gate",None)
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp); route["artifact_root"]=str(artifact_root)
   path=artifact_root/"demo-route.json"; path.write_text(json.dumps(route),encoding="utf-8")
   outcome,created=R.close_route(route,path,commit="7"*40)
   self.assertTrue(created)
   self.assertIsNone(outcome["terminal_gate_proven"])
   self.assertEqual(outcome["terminal_gates"],{})
 def test_status_splits_open_from_closed_and_ignores_sidecars(self):
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   route=dict(route); route["artifact_root"]=str(root)
   (root/"open-route.json").write_text(json.dumps(route),encoding="utf-8")
   closed=root/"closed-route.json"; closed.write_text(json.dumps(route),encoding="utf-8")
   (root/"unrelated.json").write_text(json.dumps({"note":"not a route"}),encoding="utf-8")
   R.close_route(route,closed,commit="2"*40,allow_unproven=True)
   rows={Path(row["route_file"]).name:row for row in R.route_status(root)}
   self.assertEqual(set(rows),{"open-route.json","closed-route.json"})
   self.assertFalse(rows["open-route.json"]["closed"]); self.assertTrue(rows["closed-route.json"]["closed"])
   self.assertFalse(rows["closed-route.json"]["stale_closure"])
   self.assertEqual(rows["closed-route.json"]["head_commit"],"2"*40)
 def test_sd_open_54_gate_ledger_and_foreign_basenames_in_the_canonical_dir_are_not_routes(self):
  # #15 (hearting root rt-5d862a3d/rt-94b7f5a5..., cairn W15d): `rt-*.gate-release.json`
  # (workflow-supervisor ledger) was read as a route -> route-malformed -> the
  # quiescence observation failed closed. Only `rt-<16 hex>.json` is a route
  # candidate in the canonical directory; typed sidecars are never candidates.
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); canonical=root/".runtime"/"routes"; canonical.mkdir(parents=True)
   route=dict(route); route["artifact_root"]=str(root); rid=route["route_id"]
   (canonical/f"{rid}.json").write_text(json.dumps(route),encoding="utf-8")
   (canonical/f"{rid}.gate-release.json").write_text(json.dumps({"schema_version":1,"route_id":rid,"gate_releases":[]}),encoding="utf-8")
   (canonical/f"{rid}.superseded-20260907T000000Z.outcome.json").write_text("{}",encoding="utf-8")
   (canonical/"notes.json").write_text(json.dumps({"kind":"not-a-route"}),encoding="utf-8")
   diagnostics=[]
   rows=R.route_status(root,diagnostics=diagnostics)
   self.assertEqual([Path(r["route_file"]).name for r in rows],[f"{rid}.json"])
   self.assertEqual(R.route_sidecar_kind(canonical/f"{rid}.gate-release.json"),"gate-release")
   self.assertEqual(R.route_sidecar_kind(canonical/f"{rid}.superseded-20260907T000000Z.outcome.json"),"outcome")
   self.assertIsNone(R.route_sidecar_kind(canonical/f"{rid}.json"))
   self.assertEqual([(Path(d["path"]).name,d["reason"],d["blocking"]) for d in diagnostics],
                    [("notes.json","route-candidate-foreign-basename",False)])
   # a truly malformed route record still blocks, as before
   (canonical/"rt-0123456789abcdef.json").write_text("{",encoding="utf-8")
   diagnostics=[]; R.route_status(root,diagnostics=diagnostics)
   self.assertTrue(any(d["reason"].startswith("route-unreadable") and d.get("blocking",True) for d in diagnostics))
 def test_status_flags_a_closure_left_behind_by_a_recompiled_route(self):
  first=R.compile_route(**self.args())
  second=R.compile_route(**self.args(artifact_root=R.ROOT/"other"))
  self.assertNotEqual(first["route_hash"],second["route_hash"])
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   first=dict(first); first["artifact_root"]=str(root)
   path=root/"demo-route.json"; path.write_text(json.dumps(first),encoding="utf-8")
   R.close_route(first,path,commit="3"*40,allow_unproven=True)
   second=dict(second); second["artifact_root"]=str(root)
   path.write_text(json.dumps(second),encoding="utf-8")
   row=R.route_status(root)[0]
   self.assertTrue(row["closed"]); self.assertTrue(row["stale_closure"])
 # regression ②: D-2 route-record canonical location enforcement.
 def test_classify_route_location_covers_all_six_buckets(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   cases={
    root/".runtime"/"routes"/"a.json":"canonical",
    root/"a-route.json":"legacy-root",
    root/"routes"/"a.json":"legacy-routes",
    root/"_routes"/"a.json":"legacy-_routes",
    root/".routes"/"a.json":"legacy-.routes",
    root/"nested"/"a.json":"outside",
    (root.parent/"elsewhere"/"a.json"):"outside",
   }
   for path,expected in cases.items():
    self.assertEqual(R.classify_route_location(path,root),expected,path)
 def test_classify_route_location_follows_symlink_escape(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/"root"; root.mkdir()
   outside=Path(tmp)/"outside"; outside.mkdir()
   escape=root/".runtime"/"routes-escape"
   escape.parent.mkdir(parents=True)
   escape.symlink_to(outside)
   self.assertEqual(R.classify_route_location(escape/"a.json",root),"outside")
 def _compile_cli_args(self,artifact_root,*,output=None):
  args=["--capability","autopilot-code","--capability-mode","dev","--slug","CLI Route",
        "--intensity","direct",
        "--cwd",str(R.ROOT),"--artifact-root",str(artifact_root)]
  for predicate in ALL: args+=["--predicate",predicate]
  args+=["--tracking","tracked","--spec-read","true","--drift-verdict","within-spec",
         "--workflow-mode","tracked","--artifact-guard","true"]
  if output is not None: args+=["--output",str(output)]
  return args
 def _run_compile_cli(self,argv,*,env=None):
  import subprocess,sys
  child_env=os.environ.copy()
  child_env["AGENT_HOME"]=str(R.ROOT)
  child_env.pop("CLAUDE_HOME",None)
  if env: child_env.update(env)
  return subprocess.run(
   [sys.executable,str(P),"compile",*argv],capture_output=True,text=True,
   cwd=str(R.ROOT),env=child_env)
 def test_compile_output_omitted_writes_canonical_default(self):
  # F1: the previous version of this test never invoked the CLI at all -- it
  # called `write_once` on a path it built itself, so it exercised nothing
  # about `main()`'s actual default-output behavior. This subprocess call
  # exercises the real enforcement: deleting `main()`'s canonical-default
  # block makes this fail because no file is created at the expected path.
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   result=self._run_compile_cli(self._compile_cli_args(artifact_root))
   self.assertEqual(result.returncode,0,result.stderr)
   route=json.loads(result.stdout)
   self.assertEqual(route["slug"],"cli-route")
   self.assertFalse(route["slug_truncated"])
   launch=route["launch_compatibility_tuple"]
   self.assertEqual(launch["contract_version"],R.LAUNCH_COMPATIBILITY_TUPLE_VERSION)
   self.assertEqual(launch["tuple_version"],R.LAUNCH_COMPATIBILITY_TUPLE_VERSION)
   expected=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   self.assertTrue(expected.is_file())
   self.assertIn(f"route_file={expected.resolve()}",result.stderr)
   self.assertEqual(json.loads(expected.read_text(encoding="utf-8"))["route_id"],route["route_id"])
 def test_compile_cli_requires_slug(self):
  with tempfile.TemporaryDirectory() as tmp:
   argv=self._compile_cli_args(Path(tmp))
   slug_index=argv.index("--slug")
   del argv[slug_index:slug_index+2]
   result=self._run_compile_cli(argv)
   self.assertEqual(result.returncode,2,result.stderr)
   self.assertIn("--slug",result.stderr)
 def test_complete_output_collision_is_refused_and_preserves_original(self):
  # C-25b: `complete --output` used to overwrite any existing file at that
  # path unconditionally (`if a.output: atomic_write(a.output, marker)`),
  # which could silently destroy a pre-existing owner artifact that happened
  # to share the resolved path. The check must run before completion itself,
  # so refusing it does not also touch the completion registry state.
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   compiled=self._run_compile_cli(self._compile_cli_args(artifact_root))
   self.assertEqual(compiled.returncode,0,compiled.stderr)
   route=json.loads(compiled.stdout)
   route_path=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   node_id=route["nodes"][0]["id"]
   evidence=artifact_root/"evidence.txt"
   evidence.write_text("evidence\n",encoding="utf-8")
   existing_output=artifact_root/"existing-artifact.md"
   existing_output.write_text("pre-existing owner artifact\n",encoding="utf-8")
   before=existing_output.read_bytes()
   result=subprocess.run(
    [sys.executable,str(P),"complete","--route",str(route_path),"--node",node_id,
     "--evidence",str(evidence),"--output",str(existing_output)],
    capture_output=True,text=True,cwd=str(R.ROOT))
   self.assertEqual(result.returncode,64,result.stderr)
   self.assertIn("completion-output-exists",result.stderr)
   self.assertEqual(existing_output.read_bytes(),before)
 def test_complete_output_absent_still_writes_the_marker_copy(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   compiled=self._run_compile_cli(self._compile_cli_args(artifact_root))
   self.assertEqual(compiled.returncode,0,compiled.stderr)
   route=json.loads(compiled.stdout)
   route_path=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   node_id=route["nodes"][0]["id"]
   evidence=artifact_root/"evidence.txt"
   evidence.write_text("evidence\n",encoding="utf-8")
   output=artifact_root/"marker-copy.json"
   child_env=os.environ.copy()
   child_env["AGENT_HOME"]=str(R.ROOT)
   child_env.pop("CLAUDE_HOME",None)
   child_env["AGENT_DISPATCH_JOBS"]=str(Path(tmp)/"jobs.log")
   result=subprocess.run(
    [sys.executable,str(P),"complete","--route",str(route_path),"--node",node_id,
     "--evidence",str(evidence),"--output",str(output)],
    capture_output=True,text=True,cwd=str(R.ROOT),env=child_env)
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertTrue(output.is_file())
 def test_compile_runtime_root_mismatch_writes_nothing(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   runtime=root/"runtime"; (runtime/"core").mkdir(parents=True)
   (runtime/"core"/"CORE.md").write_text("other release\n",encoding="utf-8")
   artifact_root=root/"artifacts"
   jobs=root/"state"/"jobs.log"
   result=self._run_compile_cli(
    self._compile_cli_args(artifact_root),
    env={"AGENT_HOME":str(runtime),"AGENT_DISPATCH_JOBS":str(jobs)},
   )
   self.assertEqual(result.returncode,64,result.stderr)
   self.assertEqual(result.stdout,"")
   self.assertIn("launch-runtime-root-mismatch",result.stderr)
   self.assertIn("route_file_written=0 registered=0 started=0 child_spawned=0",result.stderr)
   self.assertFalse((artifact_root/".runtime"/"routes").exists())
   self.assertFalse(jobs.exists())
 def test_compile_output_outside_canonical_is_rejected(self):
  # F1: the previous version of this test asserted only that
  # `classify_route_location` returns "legacy-routes" for this path -- it
  # never called the CLI, so it could not detect the enforcement block being
  # deleted from `main()`. This subprocess call exercises the actual rejection.
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   outside=artifact_root/"routes"/"demo-route.json"
   result=self._run_compile_cli(self._compile_cli_args(artifact_root,output=outside))
   self.assertEqual(result.returncode,64,result.stderr)
   self.assertIn("route-output-outside-canonical",result.stderr)
   self.assertFalse(outside.exists())
 def test_compile_output_alias_basename_inside_canonical_is_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   alias=R.canonical_routes_dir(artifact_root)/"autopilot-2026-node.json"
   result=self._run_compile_cli(self._compile_cli_args(artifact_root,output=alias))
   self.assertEqual(result.returncode,64,result.stderr)
   self.assertIn("route-output-alias-basename",result.stderr)
   self.assertFalse(alias.exists())
 def test_compile_output_canonical_basename_is_accepted(self):
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   first=self._run_compile_cli(self._compile_cli_args(artifact_root))
   self.assertEqual(first.returncode,0,first.stderr)
   route=json.loads(first.stdout)
   canonical=R.canonical_route_path(artifact_root,route["route_id"])
   second=self._run_compile_cli(self._compile_cli_args(artifact_root,output=canonical))
   self.assertEqual(second.returncode,0,second.stderr)
   self.assertEqual(json.loads(second.stdout),route)
 def test_status_reports_alias_basename_drift(self):
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); route=dict(route); route["artifact_root"]=str(root)
   alias=R.canonical_routes_dir(root)/"dated-capability-alias.json"
   R.write_once(alias,route)
   row=R.route_status(root)[0]
   self.assertTrue(row["alias_basename"])
   self.assertTrue(row["drift"])
   self.assertFalse(row["read_only"])
 def test_close_route_publication_absent_keeps_schema_v3(self):
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); route=dict(route); route["artifact_root"]=str(root)
   path=R.canonical_route_path(root,route["route_id"]); R.write_once(path,route)
   outcome,_=R.close_route(route,path,commit="8"*40,allow_unproven=True)
   self.assertEqual(outcome["schema_version"],3)
   self.assertNotIn("publication",outcome)
 def test_close_route_publication_present_bumps_schema_v4(self):
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); route=dict(route); route["artifact_root"]=str(root)
   path=R.canonical_route_path(root,route["route_id"]); R.write_once(path,route)
   outcome,_=R.close_route(route,path,commit="9"*40,publication="failed",allow_unproven=True)
   self.assertEqual(outcome["schema_version"],4)
   self.assertEqual(outcome["publication"],"failed")
 def test_close_route_on_alias_record_still_succeeds_with_drift_warning(self):
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); route=dict(route); route["artifact_root"]=str(root)
   alias=R.canonical_routes_dir(root)/"existing-alias.json"; R.write_once(alias,route)
   stderr=io.StringIO()
   with contextlib.redirect_stderr(stderr):
    outcome,created=R.close_route(route,alias,commit="a"*40,allow_unproven=True)
   self.assertTrue(created)
   self.assertEqual(outcome["route_location"],"canonical")
   self.assertTrue(R.outcome_path(alias).is_file())
   self.assertIn("alias_basename=true",stderr.getvalue())
 def test_status_reports_location_drift_and_duplicate_locations(self):
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   canonical=R.canonical_routes_dir(root)/f"{route['route_id']}.json"; R.write_once(canonical,route)
   legacy=root/"routes"/f"{route['route_id']}.json"; R.write_once(legacy,route)
   rows={row["route_file"]:row for row in R.route_status(root)}
   c_row=rows[str(canonical)]; l_row=rows[str(legacy)]
   self.assertEqual(c_row["location"],"canonical"); self.assertFalse(c_row["drift"]); self.assertFalse(c_row["read_only"])
   self.assertEqual(l_row["location"],"legacy-routes"); self.assertTrue(l_row["drift"]); self.assertTrue(l_row["read_only"])
   self.assertIn("duplicate_locations",c_row); self.assertIn("duplicate_locations",l_row)
   self.assertEqual(set(c_row["duplicate_locations"]),{str(canonical),str(legacy)})
 def test_close_of_legacy_location_route_records_route_location_and_keeps_sidecar_beside_it(self):
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp); artifact_root.mkdir(exist_ok=True)
   route=dict(route); route["artifact_root"]=str(artifact_root)
   legacy=artifact_root/"_routes"/"demo-route.json"; legacy.parent.mkdir(parents=True)
   legacy.write_text(json.dumps(route),encoding="utf-8")
   outcome,created=R.close_route(route,legacy,commit="4"*40,summary="legacy close",allow_unproven=True)
   self.assertTrue(created)
   self.assertEqual(outcome["route_location"],"legacy-_routes")
   self.assertTrue(R.outcome_path(legacy).is_file())
   self.assertEqual(R.outcome_path(legacy).parent,legacy.parent)
 def test_close_rejects_a_route_file_outside_canonical_and_legacy_locations(self):
  # F7: compile's canonical-output enforcement is worthless if close can still
  # write an outcome sidecar next to a route file living anywhere at all.
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp); route=dict(route); route["artifact_root"]=str(artifact_root)
   outside=artifact_root/"nested"/"rogue-route.json"; outside.parent.mkdir(parents=True)
   outside.write_text(json.dumps(route),encoding="utf-8")
   with self.assertRaisesRegex(ValueError,"route-close-outside-canonical-or-legacy"):
    R.close_route(route,outside,commit="5"*40)
   self.assertFalse(R.outcome_path(outside).exists())
 def test_f47_1_new_route_records_carry_owner_attempt_and_family_key(self):
  previous=os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID",None)
  try:
   unowned=R.compile_route(**self.args())
   self.assertEqual(unowned["owner_attempt_id"],"-")
   self.assertEqual(
    unowned["route_family_key"],
    R.route_family_key(
     unowned["capability"],unowned["cwd"],unowned["capability_mode"],"-"),
   )
   os.environ["AGENT_DISPATCH_ATTEMPT_ID"]="att-fixture-owner"
   owned=R.compile_route(**self.args())
   self.assertEqual(owned["owner_attempt_id"],"att-fixture-owner")
   self.assertEqual(
    owned["route_family_key"],
    R.route_family_key(
     owned["capability"],owned["cwd"],owned["capability_mode"],"att-fixture-owner"),
   )
   self.assertNotEqual(unowned["route_family_key"],owned["route_family_key"])
  finally:
   if previous is None: os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID",None)
   else: os.environ["AGENT_DISPATCH_ATTEMPT_ID"]=previous
 def test_f47_2_route_hash_exclusion_parity_with_fleet(self):
  # `base` is a genuine on-disk record shape: no `_fleet_*` key (fleet only
  # ever adds that to its own in-memory copy after loading, never to what
  # capability-route.py writes), no owner_attempt_id/route_family_key yet.
  base={
   "route_id":"rt-fixture0000000","route_hash":"sha256:"+"a"*64,
   "capability":"autopilot-code","capability_mode":"dev","schema_version":2,
   "nodes":[{"id":"execute"}],
  }
  digest=R.route_hash(base)
  self.assertEqual(digest,FLEET_ROUTE.route_hash(base))  # ① genuine-record parity
  sealed=dict(base,owner_attempt_id="att-fixture",route_family_key="sha256:"+"b"*64)
  self.assertEqual(R.route_hash(sealed),digest)           # ② new exclusion, capability-route.py
  self.assertEqual(FLEET_ROUTE.route_hash(sealed),digest) # ② new exclusion, fleet replica
  annotated=dict(sealed,_fleet_schema_status="current")
  self.assertEqual(FLEET_ROUTE.route_hash(annotated),digest)  # `_fleet_` exception preserved (R6)
  schema_varied=dict(annotated,_fleet_schema_status="legacy-read-only")
  self.assertEqual(FLEET_ROUTE.route_hash(schema_varied),digest)
  owner_varied=dict(sealed,owner_attempt_id="att-other")
  self.assertEqual(R.route_hash(owner_varied),digest)
  self.assertEqual(FLEET_ROUTE.route_hash(owner_varied),digest)
 def test_f47_2_selection_structurally_identical(self):
  previous=os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID",None)
  try:
   a=R.compile_route(**self.args())
   os.environ["AGENT_DISPATCH_ATTEMPT_ID"]="att-fixture-owner"
   b=R.compile_route(**self.args())
  finally:
   if previous is None: os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID",None)
   else: os.environ["AGENT_DISPATCH_ATTEMPT_ID"]=previous
  self.assertNotEqual(a["owner_attempt_id"],b["owner_attempt_id"])
  self.assertNotEqual(a["route_family_key"],b["route_family_key"])
  a_reduced={k:v for k,v in a.items() if k not in ("owner_attempt_id","route_family_key")}
  b_reduced={k:v for k,v in b.items() if k not in ("owner_attempt_id","route_family_key")}
  self.assertEqual(a_reduced,b_reduced)
  self.assertEqual(a["route_hash"],b["route_hash"])
  self.assertEqual(a["route_id"],b["route_id"])
 def test_f47_4_legacy_route_record_interpretation_unchanged(self):
  route=R.compile_route(**self.args())
  legacy=json.loads(json.dumps(route))
  legacy.pop("owner_attempt_id",None); legacy.pop("route_family_key",None)
  self.assertEqual(R.route_hash(legacy),route["route_hash"])
  legacy["route_hash"]=R.route_hash(legacy)
  legacy["route_id"]="rt-"+legacy["route_hash"].split(":",1)[1][:16]
  self.assertEqual(legacy["route_id"],route["route_id"])
  R.verify_route(legacy,R.ROOT)
  diag=R.legacy_route_diagnostic(legacy)
  self.assertEqual(diag["route_id"],legacy["route_id"])
  self.assertEqual(FLEET_ROUTE.route_hash(legacy),route["route_hash"])
 def test_f47_5_scope_overrun_detector(self):
  """F47-5: SD-118's v47 scope excludes an `operation=recompile` edge, any
  lineage-based compile rejection branch, and an exact-waste-formula output
  symbol (plan.md §7.3). Scanned: the two files this package touches --
  utilities/capability-route.py and tools/fleet/route.py. This test file is
  excluded from its own scan (it must name the forbidden strings to assert
  their absence elsewhere)."""
  scanned=(
   R.ROOT/"utilities"/"capability-route.py",
   R.ROOT/"tools"/"fleet"/"route.py",
  )
  for path in scanned:
   text=path.read_text(encoding="utf-8")
   self.assertNotIn('"operation":"recompile"',text.replace(" ",""))
   self.assertNotIn("'operation':'recompile'",text.replace(" ",""))
   self.assertNotRegex(text,r"lineage.*reject|reject.*lineage")
   self.assertNotIn("waste_exact",text)
   self.assertNotIn("exact_waste",text)

class TestContinuation(unittest.TestCase):
 def setUp(self):
  self._tmp_home=tempfile.TemporaryDirectory()
  (Path(self._tmp_home.name)/"core").mkdir(parents=True)
  (Path(self._tmp_home.name)/"core"/"CORE.md").write_text(
   "continuation fixture\n",encoding="utf-8")
  self._guard_env={key:os.environ.get(key) for key in (
   "AGENT_HOME","AGENT_DISPATCH_JOBS","AGENT_DISPATCH_ATTEMPT_ID",
   "AGENT_DISPATCH_REGISTERED_WORKER","AGENT_DISPATCH_DEPTH",
   "AGENT_OWNER_ROUTE_FILE","AGENT_OWNER_ROUTE_ID","AGENT_OWNER_ROUTE_HASH",
   "AGENT_WORKFLOW_ROOT",
  )}
  for key in self._guard_env: os.environ.pop(key,None)
  os.environ["AGENT_HOME"]=self._tmp_home.name
  # Not popped: with no AGENT_DISPATCH_JOBS the state root resolves from
  # XDG_STATE_HOME/HOME, not from AGENT_HOME, so every fixture route sealed the
  # operator's *live* jobs.log -- and once completing a node registered an
  # attempt, the suite appended fake rows there (measured: 660). Bind it to this
  # test's own tmpdir so a fixture registry is always a fixture registry.
  self._jobs=Path(self._tmp_home.name)/"state"/"jobs.log"
  self._jobs.parent.mkdir(parents=True,exist_ok=True)
  os.environ["AGENT_DISPATCH_JOBS"]=str(self._jobs)
  self.addCleanup(self._restore)
 def _restore(self):
  for key,value in self._guard_env.items():
   if value is None: os.environ.pop(key,None)
   else: os.environ[key]=value
  self._tmp_home.cleanup()
 def _dispatch(self,worktree=None):
  row={
   "parent_harness":"codex","parent_transport":"headless",
   "parent_sandbox":R.WRAPPER_PARENT_SANDBOXES["codex"][0],
   "child_harness":"codex","launch_authority":"conductor","status":"supported",
   "probe_source":"continuation-fixture","probe_time":"2026-08-25T00:00:00Z",
   "failure_class":"","checked_worktree":str((worktree or R.ROOT).resolve()),
   "failure_scope":"none","codex_command":"ok","retry_on_isolated_worktree":0,
  }
  return {"tuples":[row],"native_subagent":[{
   "harness":"codex","transport":"headless",
   "execution_surface":"codex-native-subagent","registered_worker":False,
   "status":"supported","check_source":"continuation-fixture",
  }]}
 def _source(self,artifact_root,cwd=None,slug=None,**selection):
  gate={
   "spec_read":{"satisfied":True,"source":"canonical-prd-sha256"},
   "drift_verdict":"within-spec","workflow_mode":"tracked",
   "artifact_guard":{"satisfied":True,"source":"conductor-prechecked"},
  }
  route=R.compile_route(
   "autopilot-code","dev","strong",cwd or R.ROOT,artifact_root,
   predicates=[],signals=["shared-contract"],transport="headless",
   tracking="tracked",tracked_gate_evidence=gate,
   dispatch_evidence=self._dispatch(cwd),
   slug=slug, **selection,
  )
  route["runtime_lineage"]={
   "runtime":"codex","thread_id":"thread-source",
   "node_turn_ids":{
    str(node["id"]):f"turn-{node['id']}" for node in route["nodes"]
   },
  }
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  return route
 def _complete_node(self,route,node,evidence_root,attempt_id=None):
  attempt_id=attempt_id or f"att-continuation-{node['id']}"
  evidence=Path(evidence_root)/f"{node['id']}.md"
  evidence.parent.mkdir(parents=True,exist_ok=True)
  evidence.write_text(f"{node['id']} exact output\n",encoding="utf-8")
  metadata={
   "attempt_schema_version":2,"dispatch_depth":node["dispatch_depth"],
   "transport":"headless","execution_surface":"registered-headless",
   "registered_worker":"1","fallback_hop":"same-harness-headless",
  }
  R.complete_node(
   route,node,node["id"],evidence,attempt_id=attempt_id,
   explicit_attempt_metadata=metadata,
  )
  jobs=Path(route["launch_compatibility_tuple"]["jobs_path"]["path"])
  link_path=R._attempt_completion_path(
   route,node["id"],attempt_id,jobs=jobs
  )
  link=json.loads(link_path.read_text(encoding="utf-8"))
  link.update({
   "verdict":"PASS",
   "quiescence_proof_digest":"sha256:"+re.sub("[^0-9a-f]","0",node["id"])[:1].ljust(64,"a"),
   "last_turn_id":f"turn-{node['id']}",
  })
  R.atomic_write(link_path,link)
  # A completed node has a registry row: production cannot reach a completion
  # marker without one. Fixtures that skipped this were the only reason the
  # rebind ever saw a missing registry, which B2 now (correctly) declines.
  self._write_attempt_row(jobs,route["route_id"],node["id"],attempt_id)
  return evidence
 def _complete_prefix(self,route,resume_from,evidence_root,skip=()):
  evidence={}
  for node in route["nodes"][:next(
      index for index,row in enumerate(route["nodes"]) if row["id"]==resume_from
  )]:
   if node["id"] in skip: continue
   evidence[node["id"]]=self._complete_node(route,node,evidence_root)
  # A reused human-gate predecessor proves more than source completion. Seal a
  # real raise+proceed pair in the fixture ledger, matching production.
  prefix_ids={node["id"] for node in route["nodes"][:next(
      index for index,row in enumerate(route["nodes"]) if row["id"]==resume_from
  )]}
  import workflow_state as WS
  jobs=Path(route["launch_compatibility_tuple"]["jobs_path"]["path"])
  ledger=WS.WorkflowLedger(route["route_id"],route["route_hash"],jobs=jobs)
  for node in route["nodes"]:
   continuation=node.get("continuation") or {}
   if node["id"] not in prefix_ids or continuation.get("kind")!="human-gate": continue
   gate=continuation["gate"]
   if WS.human_gate_resolution(ledger.journal(),gate)["status"]!="not-raised": continue
   with ledger.lock():
    if ledger.state()["workflow_state"]=="CREATED":
     ledger.set_workflow_state("READY",evidence={},actor="fixture")
    ledger.set_workflow_state("BLOCKED_HUMAN_GATE",
     evidence={"gate":gate,"artifact":str(evidence_root)},actor="fixture")
    ledger.set_workflow_state("RUNNING",evidence={"released_gate":gate,
     "decision":"proceed","released_by":"fixture-user","actor_kind":"user"},
     actor="fixture")
  return evidence
 def _build(self,source,**overrides):
  args={
   "resume_from_node":"test","requested_boundary":"test",
   "reason":"resume-after-impl-review",
   "artifact_root":source["artifact_root"],
  }
  args.update(overrides)
  return R.build_continuation_route(source,**args)
 def _assert_no_alias_key(self,value):
  if isinstance(value,dict):
   self.assertNotIn("evidence_digest",value)
   for item in value.values(): self._assert_no_alias_key(item)
  elif isinstance(value,list):
   for item in value: self._assert_no_alias_key(item)
 def test_a_directory_artifact_completes_and_then_passes_the_terminal_gate(self):
  # Review B1: the envelope inspector and the marker writer were converted but the
  # two VERIFICATION sites still did `Path(evidence["path"]).read_bytes()`, so a
  # directory artifact wrote a canonical marker and was then refused
  # `completion-evidence-unreadable` at the gate — the same "finished work booked
  # as failure", later and under a stranger reason. Completing is not enough; the
  # gate has to agree.
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact)
   node=next(row for row in source["nodes"] if row["id"]=="frame")
   bucket=Path(tmp)/"evidence"/"documents"/"bucket"
   bucket.mkdir(parents=True)
   (bucket/"REPORT.md").write_text("body\n",encoding="utf-8")
   R.complete_node(
    source,node,"frame",bucket,attempt_id="att-directory-artifact",
    explicit_attempt_metadata={
     "attempt_schema_version":2,"dispatch_depth":node["dispatch_depth"],
     "transport":"headless","execution_surface":"registered-headless",
     "registered_worker":"1","fallback_hop":"same-harness-headless",
    },
   )
   row=R._marker_identity_row(source,node,"frame",node.get("completion_gate"))
   self.assertTrue(row["passed"], row)
   # and the digest the marker stored is the directory digest, not a file hash
   marker=json.loads(
    (R.completion_dir(source["route_id"])/"frame.json").read_text(encoding="utf-8")
   )
   self.assertEqual(marker["evidence"]["sha256"],R.evidence_digest(bucket))
   # a member changing invalidates the gate, so the attestation is real
   (bucket/"REPORT.md").write_text("tampered\n",encoding="utf-8")
   self.assertFalse(
    R._marker_identity_row(source,node,"frame",node.get("completion_gate"))["passed"]
   )

 def test_an_empty_directory_cannot_complete_a_node(self):
  # Review R2: the non-empty rule lived only on the inspector path, so the
  # documented manual `capability-route.py complete --evidence` surface still
  # completed a node on the cycle's pre-created, empty `artifacts/` directory and
  # the terminal gate passed it. One rule, applied at every door.
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact)
   node=next(row for row in source["nodes"] if row["id"]=="frame")
   metadata={
    "attempt_schema_version":2,"dispatch_depth":node["dispatch_depth"],
    "transport":"headless","execution_surface":"registered-headless",
    "registered_worker":"1","fallback_hop":"same-harness-headless",
   }
   empty=Path(tmp)/"evidence"/"artifacts"
   empty.mkdir(parents=True)
   with self.assertRaisesRegex(ValueError,"evidence-empty-directory"):
    R.complete_node(source,node,"frame",empty,attempt_id="att-empty",
                    explicit_attempt_metadata=metadata)
   # no marker was written, so the gate cannot pass on it either
   self.assertFalse((R.completion_dir(source["route_id"])/"frame.json").exists())
   # the same directory completes once it actually holds output
   (empty/"REPORT.md").write_text("body\n",encoding="utf-8")
   R.complete_node(source,node,"frame",empty,attempt_id="att-empty",
                   explicit_attempt_metadata=metadata)
   self.assertTrue(
    R._marker_identity_row(source,node,"frame",node.get("completion_gate"))["passed"]
   )

 def test_evidence_digest_refuses_what_it_cannot_attest(self):
  # Review B2/S4: a missing path used to return the empty-directory constant, so
  # "deleted before the gate" and "empty at completion" verified as one artifact.
  # A FIFO member blocked forever; a non-UTF-8 name raised on encode.
  import os
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   with self.assertRaisesRegex(ValueError,"evidence-not-a-file-or-directory"):
    R.evidence_digest(root/"absent")
   empty=root/"empty"; empty.mkdir()
   with self.assertRaisesRegex(ValueError,"evidence-empty-directory"):
    R.evidence_digest(empty)
   link=root/"link.md"; (root/"real.md").write_text("x\n",encoding="utf-8")
   link.symlink_to(root/"real.md")
   with self.assertRaisesRegex(ValueError,"evidence-symlink-not-attestable"):
    R.evidence_digest(link)
   # A FIFO is never read (it would block forever). With a real file beside it
   # the tree is a deliverable, and the pipe is refused as a member.
   fifo_dir=root/"fifo"; fifo_dir.mkdir(); os.mkfifo(fifo_dir/"pipe")
   with self.assertRaisesRegex(ValueError,"evidence-no-regular-file"):
    R.evidence_digest(fifo_dir)
   (fifo_dir/"REPORT.md").write_text("body\n",encoding="utf-8")
   with self.assertRaisesRegex(ValueError,"evidence-member-not-regular"):
    R.evidence_digest(fifo_dir)
   odd=root/"odd"; odd.mkdir()
   (odd/os.fsdecode(b"na\xffme.md")).write_bytes(b"body\n")
   self.assertNotEqual(R.evidence_digest(odd),"")     # non-UTF-8 name digests

 def test_evidence_digest_names_a_file_or_a_directory(self):
  # A worker's artifact is legitimately either shape. Before this, the envelope
  # inspector rejected a directory and `write_completion_marker` would have died
  # `IsADirectoryError` on `read_bytes()` had it got that far.
  import hashlib
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   single=root/"report.md"; single.write_text("body\n",encoding="utf-8")
   self.assertEqual(R.evidence_digest(single),
                    hashlib.sha256(b"body\n").hexdigest())
   bucket=root/"documents"; (bucket/"sub").mkdir(parents=True)
   (bucket/"b.md").write_text("b\n",encoding="utf-8")
   (bucket/"a.md").write_text("a\n",encoding="utf-8")
   (bucket/"sub"/"c.md").write_text("c\n",encoding="utf-8")
   first=R.evidence_digest(bucket)
   self.assertEqual(first,R.evidence_digest(bucket))          # stable
   self.assertNotEqual(first,R.evidence_digest(single))
   (bucket/"a.md").write_text("a2\n",encoding="utf-8")
   self.assertNotEqual(first,R.evidence_digest(bucket))       # content-sensitive
   renamed=root/"documents2"; bucket.rename(renamed)
   moved=R.evidence_digest(renamed)
   (renamed/"a.md").write_text("a\n",encoding="utf-8")
   self.assertEqual(first,R.evidence_digest(renamed))         # path-independent
   self.assertNotEqual(first,moved)
   # A symlink is recorded by its target text, never followed: a marker must
   # describe the tree it was handed.
   outside=root/"outside.md"; outside.write_text("secret\n",encoding="utf-8")
   (renamed/"link.md").symlink_to(outside)
   with_link=R.evidence_digest(renamed)
   outside.write_text("changed\n",encoding="utf-8")
   self.assertEqual(with_link,R.evidence_digest(renamed))

 def test_slug_metadata_is_inherited_by_continuation(self):
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact,slug="Cycle A")
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   continuation=self._build(source)
   self.assertEqual(continuation["slug"],"cycle-a")
   self.assertFalse(continuation["slug_truncated"])
   R.verify_route(continuation,R.ROOT)
 def test_campaign_selection_survives_continuation(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts",slug="Cycle A",
                       campaign_key="tts-v6-release",parent_cycle_id="cyc_"+"a"*32)
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   continuation=self._build(source)
   self.assertEqual(continuation["campaign_key"],source["campaign_key"])
   self.assertEqual(continuation["parent_cycle_id"],source["parent_cycle_id"])
   R.verify_route(continuation,R.ROOT)
 def test_at1_reuses_exact_prefix_and_publishes_suffix_only(self):
  from unittest import mock
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact)
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   with mock.patch.object(
       R,"compile_route",side_effect=AssertionError("generic compile forbidden")):
    continuation=self._build(source)
   reused_ids=[row["node_id"] for row in continuation["reused_nodes"]]
   self.assertEqual(reused_ids,[
    node["id"] for node in source["nodes"]
    if source["nodes"].index(node)<next(
     i for i,row in enumerate(source["nodes"]) if row["id"]=="test")
   ])
   self.assertTrue(all(row["new_attempt_count"]==0 for row in continuation["reused_nodes"]))
   self.assertEqual(continuation["first_runnable_node"],"test")
   self.assertEqual([node["id"] for node in continuation["nodes"]],["test","report"])
   self.assertEqual(continuation["new_nodes"][0]["attempt_authority"],"granted")
   self.assertEqual(continuation["new_nodes"][1]["attempt_authority"],"pending-dependency")
   test_node=continuation["nodes"][0]
   self.assertEqual(test_node["depends_on"],[])
   self.assertEqual(
    [row["node_id"] for row in test_node["reused_dependencies"]],
    ["impl-review","impl-review-alternative"],
   )
   self.assertEqual(
    continuation["source_evidence_digest"],
    R.source_evidence_digest(source,reused_ids),
   )
   self._assert_no_alias_key(continuation)
   self.assertTrue(continuation["source_route_supersession"]["source_verdict_preserved"])
   self.assertEqual(len(continuation["supersession_edges"]),1)
   R.verify_route(continuation,R.ROOT)
   output=R.canonical_route_path(artifact,continuation["route_id"])
   R.publish_continuation_route(continuation,source,output)
   self.assertTrue(output.is_file())
   self.assertFalse(R.completion_dir(continuation["route_id"]).exists())
 def test_continuation_drops_a_binding_whose_entry_node_was_cut(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts")
   source["human_gates"].append("intent-confirmation")
   source["human_gate_bindings"].append({
    "gate":"intent-confirmation","node":"frame","position":"entry"})
   source["route_hash"]=R.route_hash(source)
   source["route_id"]="rt-"+source["route_hash"].split(":",1)[1][:16]
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   built=self._build(source)
   self.assertNotIn("intent-confirmation",built["human_gates"])
   self.assertFalse(any(row["gate"]=="intent-confirmation"
                        for row in built["human_gate_bindings"]))
 def test_cut_raiser_requires_exact_source_release_not_source_completion(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts")
   plan_index=next(i for i,node in enumerate(source["nodes"]) if node["id"]=="plan")
   for node in source["nodes"][:plan_index]:
    self._complete_node(source,node,Path(tmp)/"evidence")
   with self.assertRaisesRegex(ValueError,"continuation-human-gate-release-unproven"):
    self._build(source,resume_from_node="plan",requested_boundary="plan")
 def test_cut_raiser_seals_and_revalidates_exact_proceed_evidence(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts")
   self._complete_prefix(source,"plan",Path(tmp)/"evidence")
   built=self._build(source,resume_from_node="plan",requested_boundary="plan")
   proof=built["reused_human_gate_releases"][0]
   self.assertEqual((proof["gate"],proof["decision"],proof["epoch"]),
                    ("frame-review","proceed",1))
   self.assertNotIn("frame-review",built["human_gates"])
   R._verify_continuation_route(built)
   tampered=json.loads(json.dumps(built))
   tampered["reused_human_gate_releases"][0]["decision"]="revise"
   with self.assertRaisesRegex(ValueError,"release-proof-invalid"):
    R._verify_continuation_route(tampered)

 def test_legacy_interview_release_requires_user_in_builder_and_verifier(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); jobs=root/"jobs.log"; jobs.write_text("fixture\n")
   source={"route_id":"rt-legacy","route_hash":"sha256:"+"a"*64,
           "launch_compatibility_tuple":{"jobs_path":{"path":str(jobs)}},
           "human_gate_bindings":[{"gate":"review","node":"plan"}]}
   ledger_dir=jobs.parent/"workflow"/source["route_id"]; ledger_dir.mkdir(parents=True)
   raised={"workflow_state":"BLOCKED_HUMAN_GATE",
           "evidence":{"gate":"review","interview":True,"questions":["q"]}}
   def pair(actor_kind, released_by):
    released={"workflow_state":"RUNNING",
              "evidence":{"released_gate":"review","decision":"proceed",
                          "actor_kind":actor_kind,"released_by":released_by}}
    journal=ledger_dir/"journal.jsonl"
    journal.write_text("\n".join(json.dumps(v) for v in (raised,released))+"\n")
    proof=R._continuation_gate_release_proof(source,"review")
    route={"source_route_id":source["route_id"],
           "source_route_hash":source["route_hash"],
           "human_gate_bindings":source["human_gate_bindings"],
           "reused_human_gate_releases":[proof]}
    return route
   with self.assertRaisesRegex(ValueError,"release-unauthorized"):
    pair("headless-owner", "owner")
   R._verify_continuation_gate_release_proofs(pair("user", "operator"))

 def test_plain_non_interview_any_headless_release_remains_allowed(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); jobs=root/"jobs.log"; jobs.write_text("fixture\n")
   source={"route_id":"rt-plain","route_hash":"sha256:"+"b"*64,
           "launch_compatibility_tuple":{"jobs_path":{"path":str(jobs)}},
           "human_gate_bindings":[{"gate":"review","node":"plan"}]}
   directory=jobs.parent/"workflow"/source["route_id"]; directory.mkdir(parents=True)
   entries=[{"workflow_state":"BLOCKED_HUMAN_GATE",
             "evidence":{"gate":"review","interview":False,"questions":0}},
            {"workflow_state":"RUNNING",
             "evidence":{"released_gate":"review","decision":"proceed",
                         "actor_kind":"headless-owner","released_by":"owner"}}]
   (directory/"journal.jsonl").write_text("\n".join(json.dumps(v) for v in entries)+"\n")
   proof=R._continuation_gate_release_proof(source,"review")
   route={"source_route_id":source["route_id"],"source_route_hash":source["route_hash"],
          "human_gate_bindings":source["human_gate_bindings"],
          "reused_human_gate_releases":[proof]}
   R._verify_continuation_gate_release_proofs(route)
 def test_retained_human_gate_continuation_without_binding_fails_closed(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts")
   source["human_gates"]=[]; source["human_gate_bindings"]=[]
   source["route_hash"]=R.route_hash(source)
   source["route_id"]="rt-"+source["route_hash"].split(":",1)[1][:16]
   with self.assertRaisesRegex(ValueError,"continuation-human-gate-unrepresentable"):
    self._build(source,resume_from_node="frame",requested_boundary="frame")
 def test_confirmation_mode_survives_continuation(self):
  # T-5: confirmation_mode must ride inherited_keys, or a continuation route
  # silently drops it and the A5 drift check sees None after the first advance.
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact)
   self.assertEqual(source["confirmation_mode"],"hybrid")
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   continuation=self._build(source)
   self.assertEqual(continuation["confirmation_mode"],"hybrid")

 def test_sd_open_46_composed_route_continuation_inherits_composition(self):
  # SD-OPEN-46: a continuation of a composed (compose-on-demand) route must
  # carry the source's composition fields. Without `composed`/`composed_recipe`
  # in inherited_keys the suffix looks like a preset route to every consumer
  # (`compose_card`, CLI status, guards) and the embedded recipe loses its
  # tamper seal. `route_origin`/`shape` ride `selection`, which is inherited
  # wholesale -- asserted here so a future refactor cannot drop them.
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   recipe=json.loads(json.dumps(
    R.TOPO.resolve_recipe(R.TOPO.load_registry(),"autopilot-code","dev")))
   recipe["modes"]=["composed-fixture"]
   for node in recipe["standard_plus"]["nodes"]:
    if node.get("kind") == "resource-runner": continue
    profile=node["model_profile"]
    node["profile_demand"]={"schema_version":1,
     "judgment_requirement":"difficult-uncertain" if profile=="deep" else "important" if profile=="balanced-deep" else "predetermined",
     "execution_scope":"extended-multistep" if profile=="balanced" else "short-local",
     "judgment_reason":"Fixture preserves the declared judgment.",
     "execution_reason":"Fixture performs its declared steps.","evidence_refs":["fixture.md"]}
   for group in recipe["standard_plus"].get("parallel_groups",[]):
    for leg in group.get("legs",[]):
     profile=leg["model_profile"]
     leg["profile_demand"]={"schema_version":1,
      "judgment_requirement":"difficult-uncertain" if profile=="deep" else "important" if profile=="balanced-deep" else "predetermined",
      "execution_scope":"short-local","judgment_reason":"Fixture declared judgment.",
      "execution_reason":"Fixture bounded steps.","evidence_refs":["fixture.md"]}
   gate={
    "spec_read":{"satisfied":True,"source":"canonical-prd-sha256"},
    "drift_verdict":"within-spec","workflow_mode":"tracked",
    "artifact_guard":{"satisfied":True,"source":"conductor-prechecked"},
   }
   source=R.compile_composed_route(
    recipe,"composed-fixture","strong",R.ROOT,artifact,
    predicates=[],signals=["shared-contract"],transport="headless",
    tracking="tracked",tracked_gate_evidence=gate,
    dispatch_evidence=self._dispatch(),
    route_origin="compose",shape="staged",
   )
   self.assertIs(source["composed"],True)
   self.assertEqual(source["selection"]["route_origin"],"compose")
   self.assertEqual(source["selection"]["shape"],"staged")
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   continuation=self._build(source)
   self.assertIs(continuation["composed"],True)
   self.assertEqual(continuation["composed_recipe"],source["composed_recipe"])
   self.assertEqual(continuation["selection"]["route_origin"],"compose")
   self.assertEqual(continuation["selection"]["shape"],"staged")
   R.verify_route(continuation,R.ROOT)

 def test_at2_boundary_and_first_runnable_blockers_are_disjoint(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts-request")
   self._complete_prefix(source,"test",Path(tmp)/"evidence-request")
   requested=self._build(source,requested_boundary="missing-boundary")
   self.assertEqual(requested["requested_boundary_blocker"],"requested-boundary-unknown")
   self.assertIsNone(requested["first_runnable_blocker"])
   output=Path(tmp)/"artifacts-request"/".runtime"/"routes"/"blocked.json"
   with self.assertRaisesRegex(ValueError,"continuation-boundary-blocked"):
    R.publish_continuation_route(requested,source,output)
   self.assertFalse(output.exists())

   source=self._source(Path(tmp)/"artifacts-first")
   self._complete_prefix(
    source,"test",Path(tmp)/"evidence-first",skip={"execute"})
   first=self._build(source)
   self.assertIsNone(first["requested_boundary_blocker"])
   self.assertIn("continuation-source-node-unverified:execute",first["first_runnable_blocker"])
   output=Path(tmp)/"artifacts-first"/".runtime"/"routes"/"blocked.json"
   with self.assertRaisesRegex(ValueError,"continuation-boundary-blocked"):
    R.publish_continuation_route(first,source,output)
   self.assertFalse(output.exists())
   self.assertTrue(all(row["new_attempt_count"]==0 for row in first["reused_nodes"]))
   self.assertFalse((Path(self._tmp_home.name)/".dispatch"/"jobs.log").exists())
 def test_at3_marker_evidence_and_contract_drift_never_publish(self):
  for mutation in ("marker","evidence","contract"):
   with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
    source=self._source(Path(tmp)/f"artifacts-{mutation}")
    evidence=self._complete_prefix(
     source,"test",Path(tmp)/f"evidence-{mutation}")
    continuation=self._build(source)
    if mutation=="marker":
     jobs=Path(source["launch_compatibility_tuple"]["jobs_path"]["path"])
     marker_path=R.completion_dir(
      source["route_id"],jobs=jobs
     )/"impl-review.json"
     marker=json.loads(marker_path.read_text(encoding="utf-8"))
     marker["tampered"]=True
     R.atomic_write(marker_path,marker)
    elif mutation=="evidence":
     evidence["impl-review"].write_text("changed output\n",encoding="utf-8")
    else:
     next(
      node for node in source["nodes"] if node["id"]=="impl-review"
     )["contract_tampered"]=True
    output=R.canonical_route_path(source["artifact_root"],continuation["route_id"])
    with self.assertRaisesRegex(ValueError,"continuation-source-evidence-drift"):
     R.publish_continuation_route(continuation,source,output)
    self.assertFalse(output.exists())
    self.assertTrue(all(row["new_attempt_count"]==0 for row in continuation["reused_nodes"]))
 def test_at4_resume_fork_and_ephemeral_lineage(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts")
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   resumed=self._build(source)
   self.assertEqual(resumed["lineage_operation"],"resume")
   self.assertEqual(resumed["runtime_lineage"]["thread_id"],"thread-source")
   self.assertEqual(
    resumed["runtime_lineage"]["lastTurnId"],"turn-impl-review-alternative")
   forked=self._build(
    source,lineage_operation="fork",thread_id="thread-source",
    new_thread_id="thread-fork",forked_from_id="thread-source",
    last_turn_id="turn-impl-review-alternative",
   )
   self.assertEqual(forked["runtime_lineage"],{
    "operation":"fork","thread_id":"thread-fork",
    "forkedFromId":"thread-source",
    "lastTurnId":"turn-impl-review-alternative","ephemeral":False,
   })
   with self.assertRaisesRegex(ValueError,"continuation-last-turn-mismatch"):
    self._build(
     source,lineage_operation="fork",thread_id="thread-source",
     new_thread_id="thread-fork",forked_from_id="thread-source",
     last_turn_id="turn-wrong",
    )
   with self.assertRaisesRegex(ValueError,"continuation-ephemeral-forbidden"):
    self._build(source,ephemeral=True)
 def test_source_evidence_uses_sealed_jobs_not_ambient_registry(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts")
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   sealed=source["launch_compatibility_tuple"]["jobs_path"]["path"]
   other=str(Path(tmp)/"other-state"/"jobs.log")
   self.assertNotEqual(sealed,other)
   os.environ["AGENT_DISPATCH_JOBS"]=other
   continuation=self._build(source)
   self.assertEqual(continuation["first_runnable_node"],"test")
   self.assertIsNone(continuation["first_runnable_blocker"])
   self.assertFalse(Path(other).exists())
 def test_partial_group_continuation_seals_exact_peer_set(self):
  from replica_batch_contract import build_manifest
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts")
   members=[
    node for node in source["nodes"] if node.get("parallel_group")=="plan-check"
   ]
   attempts={members[0]["id"]:"att-peer",members[1]["id"]:"att-gap"}
   self._complete_node(
    source,members[0],Path(tmp)/"evidence",attempt_id=attempts[members[0]["id"]])
   raw_members=[]
   harnesses=("codex","claude")
   for index,node in enumerate(members):
    raw_members.append({
     "assignment_sha256":"sha256:"+"a"*64,
     "attempt_id":attempts[node["id"]],"route_node":node["id"],
     "harness":harnesses[index],"fallback_hop":"same-harness-headless",
     "fallback_ordinal":1,"model_profile":node["model_profile"],
     "perspective":node["perspective"],"parallel_leg_index":index,
     "leg_class":node.get("leg_class") or "peer",
    })
   realized=["cross-harness"]
   if len({row["model_profile"] for row in raw_members})>1:
    realized.append("model-profile")
   if len({row["perspective"] for row in raw_members})==len(raw_members):
    realized.append("perspective")
   manifest,_digest,_legs=build_manifest(
    parallel_group="plan-check",route_id=source["route_id"],
    parent_attempt_id="att-parent",independence="cross-harness",
    members=raw_members,required_independence_axes=["cross-harness"],
    realized_independence_axes=realized,
   )
   partial=R.partial_group_continuation(
    source,source_group_id="plan-check",source_batch_manifest=manifest,
    failed_source_attempt_id="att-gap",gap_leg_id=members[1]["id"],
   )
   self.assertEqual(partial["original_group_cardinality"],2)
   self.assertEqual(len(partial["realized_peer_set"]),1)
   self.assertEqual(partial["realized_peer_set"][0]["terminal_attempt_id"],"att-peer")
   self.assertTrue(partial["reused_peer_set_proof_digest"].startswith("sha256:"))
   self.assertTrue(partial["replacement_attempt_id"].startswith("att-"))

 def test_compile_cli_attaches_generation_zero_to_route_less_owner(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"; jobs=Path(tmp)/"state"/"jobs.log"
   jobs.parent.mkdir(parents=True)
   attempt="att-cli-post-launch"
   jobs.write_text(
    "2099-01-01T00:00:00Z\topen\t%s\t%s\towner\t"
    "attempt_schema_version=2,worker_type=owner,unit=_kernel/owner,"
    "dispatch_depth=1,registered_worker=1,execution_surface=registered-headless,"
    "capability=autopilot-code,capability_mode=dev,intensity=strong,"
    "artifact_root=%s,parent_sid=parent-session,owner_harness=codex,attempt_id=%s\n"
    % (R.ROOT,R.ROOT,artifact,attempt), encoding="utf-8",
   )
   dispatch_path=Path(tmp)/"dispatch.json"
   dispatch_path.write_text(json.dumps(self._dispatch()),encoding="utf-8")
   env=os.environ.copy()
   for key in ("AGENT_OWNER_ROUTE_FILE","AGENT_OWNER_ROUTE_ID","AGENT_OWNER_ROUTE_HASH"):
    env.pop(key,None)
   env.update({
    "AGENT_HOME":str(R.ROOT), "AGENT_DISPATCH_JOBS":str(jobs),
    "AGENT_DISPATCH_ATTEMPT_ID":attempt, "AGENT_DISPATCH_WORKER_TYPE":"owner",
    "AGENT_DISPATCH_DEPTH":"1", "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION":"2",
    "AGENT_DISPATCH_EXECUTION_SURFACE":"registered-headless",
    "AGENT_DISPATCH_REGISTERED_WORKER":"1",
    "AGENT_DISPATCH_PARENT_SESSION_ID":"parent-session",
    "AGENT_DISPATCH_OWNER_HARNESS":"codex",
   })
   result=subprocess.run([
    sys.executable,str(P),"compile","--capability","autopilot-code",
    "--capability-mode","dev","--slug","CLI Owner Route",
    "--intensity","strong","--cwd",str(R.ROOT),
    "--artifact-root",str(artifact),"--signal","shared-contract",
    "--transport","headless","--tracking","tracked",
    "--dispatch-evidence",str(dispatch_path),"--spec-read","true",
    "--drift-verdict","within-spec","--workflow-mode","tracked",
    "--artifact-guard","fixture",
   ],capture_output=True,text=True,cwd=str(R.ROOT),env=env)
   self.assertEqual(result.returncode,0,result.stderr)
   route=json.loads(result.stdout)
   self.assertEqual(route["owner_attempt_id"],attempt)
   self.assertIn("owner_route_binding_written=1",result.stderr)
   records=list((jobs.parent/"owner-route-bindings").glob("*.json"))
   self.assertEqual(len(records),1)
   attachment=json.loads(records[0].read_text(encoding="utf-8"))
   self.assertEqual(attachment["route_id"],route["route_id"])
   self.assertEqual(attachment["route_hash"],route["route_hash"])

 def test_compile_cli_stage_attempt_does_not_publish_owner_binding(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"; jobs=Path(tmp)/"state"/"jobs.log"
   jobs.parent.mkdir(parents=True)
   jobs.write_text(
    "2099-01-01T00:00:00Z\topen\t%s\t%s\tstage\t"
    "attempt_schema_version=2,worker_type=stage,unit=dev/backend,"
    "dispatch_depth=2,registered_worker=1,execution_surface=registered-headless,"
    "attempt_id=att-cli-stage-compile\n" % (R.ROOT,R.ROOT), encoding="utf-8",
   )
   dispatch_path=Path(tmp)/"dispatch.json"
   dispatch_path.write_text(json.dumps(self._dispatch()),encoding="utf-8")
   env=os.environ.copy()
   env.update({
    "AGENT_HOME":str(R.ROOT), "AGENT_DISPATCH_JOBS":str(jobs),
    "AGENT_DISPATCH_ATTEMPT_ID":"att-cli-stage-compile",
    "AGENT_DISPATCH_WORKER_TYPE":"stage", "AGENT_DISPATCH_DEPTH":"2",
    "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION":"2",
    "AGENT_DISPATCH_EXECUTION_SURFACE":"registered-headless",
    "AGENT_DISPATCH_REGISTERED_WORKER":"1",
   })
   result=subprocess.run([
    sys.executable,str(P),"compile","--capability","autopilot-code",
    "--capability-mode","dev","--slug","CLI Stage Route",
    "--intensity","strong","--cwd",str(R.ROOT),
    "--artifact-root",str(artifact),"--signal","shared-contract",
    "--transport","headless","--tracking","tracked",
    "--dispatch-evidence",str(dispatch_path),"--spec-read","true",
    "--drift-verdict","within-spec","--workflow-mode","tracked",
    "--artifact-guard","fixture",
   ],capture_output=True,text=True,cwd=str(R.ROOT),env=env)
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertFalse((jobs.parent/"owner-route-bindings").exists())

 def test_real_postlaunch_binding_and_two_child_adopted_continuations(self):
  import owner_route_binding as O
  from unittest import mock
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); artifact=root/"artifacts"; jobs=root/"state"/"jobs.log"
   jobs.parent.mkdir(parents=True)
   attempt="att-real-owner-lifecycle"
   owner_meta=",".join((
    "attempt_schema_version=2","worker_type=owner","unit=_kernel/owner",
    "dispatch_depth=1","registered_worker=1",
    "execution_surface=registered-headless","capability=autopilot-code",
    "capability_mode=dev","intensity=strong",f"artifact_root={artifact}",
    "parent_sid=launch-parent","owner_harness=codex",f"attempt_id={attempt}",
   ))
   owner_row="\t".join((
    "2099-01-01T00:00:00Z","open",str(R.ROOT),str(R.ROOT),"owner",owner_meta,
   ))
   jobs.write_text(owner_row+"\n",encoding="utf-8")
   lifecycle_env={
    "AGENT_DISPATCH_JOBS":str(jobs),"AGENT_DISPATCH_ATTEMPT_ID":attempt,
    "AGENT_DISPATCH_WORKER_TYPE":"owner","AGENT_DISPATCH_DEPTH":"1",
    "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION":"2",
    "AGENT_DISPATCH_EXECUTION_SURFACE":"registered-headless",
    "AGENT_DISPATCH_REGISTERED_WORKER":"1",
    "AGENT_DISPATCH_PARENT_SESSION_ID":"launch-parent",
    "AGENT_DISPATCH_OWNER_HARNESS":"codex",
   }

   def child_row(route,path,suffix):
    meta=",".join((
     f"attempt_id=att-real-child-{suffix}",f"parent_attempt_id={attempt}",
     "attempt_schema_version=2","dispatch_depth=2","registered_worker=1",
     "execution_surface=registered-headless","worker_type=stage","unit=dev/backend",
     "route_node=execute",f"route_file={path}",f"route_id={route['route_id']}",
     f"route_hash={route['route_hash']}","capability=autopilot-code",
     "capability_mode=dev",f"artifact_root={artifact}","launch_started=1",
    ))
    return "\t".join(("2099-01-01T00:00:00Z","open",str(R.ROOT),str(R.ROOT),
                       f"child-{suffix}",meta))+"\n"

   with mock.patch.dict(os.environ,lifecycle_env,clear=False):
    source=self._source(artifact)
    source_path=R.canonical_route_path(artifact,source["route_id"])
    R.write_once(source_path,source)
    attached=O.publish_owner_route_attachment_from_environment(
     jobs,target_route={**source,"route_file":str(source_path)},environ=os.environ,
    )
    self.assertEqual(attached.route_id,source["route_id"])
    self._complete_prefix(source,"test",root/"evidence-r0")
    r1=self._build(source)
    r1_path=R.canonical_route_path(artifact,r1["route_id"])
    R.publish_continuation_route(r1,source,r1_path)
    O.publish_owner_route_advance_from_environment(
     jobs,source_route={**source,"route_file":str(source_path)},
     target_route={**r1,"route_file":str(r1_path)},environ=os.environ,
    )
    pending,pending_status=O.resolve_owner_route_lifecycle(
     jobs,owner_attempt_id=attempt,
    )
    self.assertEqual((pending.route_id,pending_status),
                     (source["route_id"],"owner-route-advance-pending"))
    with jobs.open("a",encoding="utf-8") as stream:
     stream.write(child_row(r1,r1_path,"r1"))
    current,current_status=O.resolve_owner_route_lifecycle(
     jobs,owner_attempt_id=attempt,
    )
    self.assertEqual((current.route_id,current_status),
                     (r1["route_id"],"owner-route-advance-current"))

    self._complete_prefix(r1,"report",root/"evidence-r1")
    r2=R.build_continuation_route(
     r1,resume_from_node="report",requested_boundary="report",
     reason="second-generation",artifact_root=artifact,
    )
    r2_path=R.canonical_route_path(artifact,r2["route_id"])
    R.publish_continuation_route(r2,r1,r2_path)
    O.publish_owner_route_advance_from_environment(
     jobs,source_route={**r1,"route_file":str(r1_path)},
     target_route={**r2,"route_file":str(r2_path)},environ=os.environ,
    )
    with jobs.open("a",encoding="utf-8") as stream:
     stream.write(child_row(r2,r2_path,"r2"))
    final,final_status=O.resolve_owner_route_lifecycle(
     jobs,owner_attempt_id=attempt,
    )
    self.assertEqual((final.route_id,final_status),
                     (r2["route_id"],"owner-route-advance-current"))
    self.assertTrue(r1["reused_nodes"])
    self.assertTrue(r2["reused_nodes"])

 def test_continuation_cli_publishes_and_reports_zero_write_blocker(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   previous_home=os.environ.get("AGENT_HOME")
   previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   jobs=str(Path(tmp)/"state"/"jobs.log")
   os.environ["AGENT_HOME"]=str(R.ROOT)
   os.environ["AGENT_DISPATCH_JOBS"]=jobs
   try:
    artifact=Path(tmp)/"artifacts"
    source=self._source(artifact)
    self._complete_prefix(source,"test",Path(tmp)/"evidence")
    source_path=Path(tmp)/"source-route.json"
    source_path.write_text(json.dumps(source),encoding="utf-8")
    env=os.environ.copy()
    command=[
     sys.executable,str(P),"continuation","--source-route",str(source_path),
     "--resume-from-node","test","--requested-boundary","test",
     "--reason","cli-fixture","--artifact-root",str(artifact),
    ]
    success=subprocess.run(
     command,capture_output=True,text=True,cwd=str(R.ROOT),env=env)
    self.assertEqual(success.returncode,0,success.stderr)
    route=json.loads(success.stdout)
    output=R.canonical_route_path(artifact,route["route_id"])
    self.assertTrue(output.is_file())
    self.assertIn(f"route_file={output.resolve()}",success.stderr)
    blocked=subprocess.run(
     [*command[:command.index("--requested-boundary")+1],"missing",
      *command[command.index("--requested-boundary")+2:]],
     capture_output=True,text=True,cwd=str(R.ROOT),env=env,
    )
    self.assertEqual(blocked.returncode,64,blocked.stderr)
    self.assertIn('"requested_boundary_blocker": "requested-boundary-unknown"',blocked.stderr)
    self.assertIn(
     "route_file_written=0 predecessor_attempts=0 registered=0 "
     "started=0 child_spawned=0",blocked.stderr,
    )
   finally:
    if previous_home is None: os.environ.pop("AGENT_HOME",None)
    else: os.environ["AGENT_HOME"]=previous_home
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs

 def _git_repo(self,root):
  import subprocess
  repo=Path(root)/"repo"; repo.mkdir(parents=True)
  subprocess.run(["git","init","-q",str(repo)],check=True)
  subprocess.run(["git","-C",str(repo),"config","user.email","fixture@example.com"],check=True)
  subprocess.run(["git","-C",str(repo),"config","user.name","Fixture"],check=True)
  (repo/"x").write_text("a"); subprocess.run(["git","-C",str(repo),"add","x"],check=True)
  subprocess.run(["git","-C",str(repo),"commit","-qm","a"],check=True)
  return repo
 def _git_head(self,repo):
  import subprocess
  return subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"],text=True,capture_output=True,check=True).stdout.strip()

 def test_c_continuation_cli_rebinds_source_commit_after_fast_forward(self):
  # Defect C end-to-end through the checked CLI: the source route was compiled at
  # commit A, depth-0 fast-forwarded the worktree to B, and the published
  # continuation must pin B (what its grounding tuple seals), not A.
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   previous_home=os.environ.get("AGENT_HOME")
   previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   jobs=str(Path(tmp)/"state"/"jobs.log")
   os.environ["AGENT_HOME"]=str(R.ROOT)
   os.environ["AGENT_DISPATCH_JOBS"]=jobs
   try:
    repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
    source=self._source(artifact,cwd=repo)
    pinned=source["source_commit"]; self.assertEqual(pinned,self._git_head(repo))
    self._complete_prefix(source,"plan",Path(tmp)/"evidence")
    (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-qam","fast-forward"],check=True)
    head=self._git_head(repo); self.assertNotEqual(head,pinned)
    source_path=Path(tmp)/"source-route.json"
    source_path.write_text(json.dumps(source),encoding="utf-8")
    child_env=os.environ.copy()
    # The caller's attempt id must not reach the child: inherited, owner-route
    # binding fails `owner-route-owner-row-not-unique`, so this suite fails when
    # it is run from inside a registered worker -- which is how the harness runs
    # it (round 3, S3).
    child_env.pop("AGENT_DISPATCH_ATTEMPT_ID",None)
    result=subprocess.run([
     sys.executable,str(P),"continuation","--source-route",str(source_path),
     "--resume-from-node","plan","--requested-boundary","plan",
     "--reason","resume-after-fast-forward","--artifact-root",str(artifact),
    ],capture_output=True,text=True,cwd=str(R.ROOT),env=child_env)
    self.assertEqual(result.returncode,0,result.stderr)
    route=json.loads(result.stdout)
    self.assertEqual(route["source_commit"],head)
    self.assertEqual(route["source_commit_rebind"]["inherited_source_commit"],pinned)
    self.assertEqual(route["launch_compatibility_tuple"]["grounding_roots"]["cwd"]["release_id"],head)
    published=json.loads(R.canonical_route_path(artifact,route["route_id"]).read_text(encoding="utf-8"))
    self.assertEqual(R.verify_route(published)["source_commit"],head)
    # `plan` is pre-mutation, so it gets no lineage exemption from the guard: on
    # main this exact call raises route-source-commit-mismatch. It passes here
    # only because the pin was rebound.
    _,node,_=G.validate_route_contract(R.canonical_route_path(artifact,route["route_id"]),"plan",repo,artifact)
    self.assertEqual(node["id"],"plan")
   finally:
    if previous_home is None: os.environ.pop("AGENT_HOME",None)
    else: os.environ["AGENT_HOME"]=previous_home
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs

 def test_c_rebind_declines_an_sd67_mutation_retry(self):
  # Review blocking #1: rebinding unconditionally would let a continuation resume
  # at `execute` after execute already ran and committed, pinning the new HEAD and
  # walking past SD-67's retry-evidence gate. When a node this continuation
  # re-runs mutates the worktree AND the source route already has an attempt on
  # it, the pin is kept. The guard then refuses that node (it looks retry
  # evidence up under the continuation's own route_id, where the prior rows are
  # not) -- which is exactly what main does, so declining is never a regression.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   jobs=Path(tmp)/"state"/"jobs.log"; jobs.parent.mkdir(parents=True)
   os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   try:
    repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
    source=self._source(artifact,cwd=repo)
    pinned=source["source_commit"]
    self._complete_prefix(source,"execute",Path(tmp)/"evidence")
    execute=next(node for node in source["nodes"] if node["id"]=="execute")
    self.assertTrue(R._node_mutates_worktree(execute))
    # execute already ran once under this route, and its commit advanced HEAD.
    self._write_attempt_row(jobs,source["route_id"],"execute","att-execute-prior")
    (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
    declined=self._build(source,resume_from_node="execute",requested_boundary="execute")
    self.assertEqual(declined["source_commit"],pinned)
    self.assertNotIn("source_commit_rebind",declined)
    # Same route, same moved HEAD, but no prior attempt on the mutation node:
    # this route never mutated the tree, so the move is external and rebinds.
    # The completed prefix's rows stay -- emptying the file instead would be the
    # truncation case, which now (correctly) declines (round 3, B2b).
    self._drop_attempt_rows(jobs,node_id="execute")
    rebound=self._build(source,resume_from_node="execute",requested_boundary="execute")
    self.assertEqual(rebound["source_commit"],self._git_head(repo))
   finally:
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs


 def test_c_decline_looks_past_the_resume_node_to_every_node_it_reruns(self):
  # Review round 2, B1 -- the regression this fix exists to close. Asking only
  # about the resume node let a `plan`-time resume re-pin the route: in a real
  # autopilot-code route `execute` is the only worktree-mutating node, so `plan`
  # answered "not a mutation" and the rebind went through. `execute` then ran
  # later against `head == source_commit` and passed the guard on the trivial
  # branch, never reaching SD-67's evidence gate. main refuses both nodes.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   jobs=Path(tmp)/"state"/"jobs.log"; jobs.parent.mkdir(parents=True)
   os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   try:
    repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
    source=self._source(artifact,cwd=repo)
    pinned=source["source_commit"]
    self._complete_prefix(source,"plan",Path(tmp)/"evidence")
    execute=next(node for node in source["nodes"] if node["id"]=="execute")
    self.assertTrue(R._node_mutates_worktree(execute))
    plan=next(node for node in source["nodes"] if node["id"]=="plan")
    # The resume node itself is innocent; that is the whole point.
    self.assertFalse(R._node_mutates_worktree(plan))
    self._write_attempt_row(jobs,source["route_id"],"execute","att-execute-prior")
    (repo/"x").write_text("b")
    subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
    declined=self._build(source,resume_from_node="plan",requested_boundary="plan")
    self.assertEqual(declined["source_commit"],pinned)
    self.assertNotIn("source_commit_rebind",declined)
    # `execute` is carried by this continuation, which is why it counts.
    self.assertIn("execute",[node["id"] for node in declined["nodes"]])
    # Remove the prior attempt on the mutation node and the same resume rebinds:
    # the decline is about that evidence, not about resuming at `plan`.
    self._drop_attempt_rows(jobs,node_id="execute")
    rebound=self._build(source,resume_from_node="plan",requested_boundary="plan")
    self.assertEqual(rebound["source_commit"],self._git_head(repo))
   finally:
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs

 def test_c_unreadable_registry_declines_the_rebind(self):
  # Review round 2, B2 -- the read was fail-open, so deleting or truncating
  # jobs.log turned "this node already ran" into "no rows found" and re-pinned
  # the route past the SD-67 gate. `registry_rows` returns [] for a missing file
  # rather than raising, so absence of rows can never prove absence of attempts.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   jobs=Path(tmp)/"state"/"jobs.log"; jobs.parent.mkdir(parents=True)
   os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   try:
    repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
    source=self._source(artifact,cwd=repo)
    pinned=source["source_commit"]
    self._complete_prefix(source,"execute",Path(tmp)/"evidence")
    (repo/"x").write_text("b")
    subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
    moved=self._git_head(repo)
    self.assertNotEqual(moved,pinned)
    sealed=Path(source["launch_compatibility_tuple"]["jobs_path"]["path"])
    for name,wreck in (
     ("registry deleted",lambda: sealed.unlink()),
     ("registry is a directory",lambda: (sealed.unlink(),sealed.mkdir())),
    ):
     with self.subTest(name):
      if sealed.exists() or sealed.is_dir():
       if sealed.is_dir(): sealed.rmdir()
       elif sealed.exists(): sealed.unlink()
      sealed.parent.mkdir(parents=True,exist_ok=True)
      sealed.write_text("",encoding="utf-8")
      wreck()
      declined=self._build(source,resume_from_node="execute",requested_boundary="execute")
      if name == "registry is a directory":
       self.assertEqual(declined["first_runnable_blocker"],
        "continuation-source-node-unverified:frame:registry-unreadable")
       self.assertEqual(declined["new_nodes"],[])
       self.assertNotIn("source_commit",declined)
      else:
       self.assertEqual(declined["source_commit"],pinned)
      self.assertEqual(source["source_commit"],pinned)
      self.assertNotIn("source_commit_rebind",declined)
    # An unresolved jobs binding is unreadable in the same sense.
    if sealed.is_dir(): sealed.rmdir()
    unbound=json.loads(json.dumps(source))
    unbound["launch_compatibility_tuple"]["jobs_path"]={"path":None,"unresolved":"fixture"}
    self.assertTrue(R._prior_registry_attempt(unbound,"execute"))
   finally:
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs

 def test_c_route_and_guard_share_one_mutating_scope_definition(self):
  # S1: the builder's decline and the guard that adjudicates the same node must
  # answer one question one way. A copied predicate would drift silently, so
  # there is no copy -- the guard calls this function.
  #
  # Scope of this claim (round 3, Q2): the route/guard pair, not the repo.
  # `artifact-postscan.py` and `hooks/artifact-guard.sh` map the same scope
  # words to artifact-root path patterns, which is an adjacent question ("what
  # may this node write"), and both predate this branch verbatim.
  # Within the guard process there is one object: the name it uses *is* the
  # route module's function. (This test file execs its own copy of the route
  # module, so `R`'s function is a different instance of the same definition --
  # comparing against it would only prove the two files were loaded twice.)
  self.assertIs(G._worktree_mutating_scope,G.ROUTE.worktree_mutating_scope)
  guard_source=(Path(R.ROOT)/"utilities"/"worker-route-guard.py").read_text(encoding="utf-8")
  self.assertNotIn("def _worktree_mutating_scope",guard_source)
  self.assertEqual(
   G._worktree_mutating_scope.__code__.co_code,
   R.worktree_mutating_scope.__code__.co_code,
  )
  for scope,mutating in (
   ("target-artifact",True),("source-scoped",True),("source",True),
   ("source/**",True),("target-artifact-adjacent",False),
   ("artifact",False),("artifact/**",False),("spec",False),
  ):
   self.assertEqual(G._worktree_mutating_scope(scope),mutating,scope)
   self.assertEqual(R.worktree_mutating_scope(scope),mutating,scope)

 def test_c_refreshed_cwd_leaves_one_revision_per_path_in_the_tuple(self):
  # S2: the targeted refresh re-read `grounding_cwd` while `runtime_root` /
  # `launch_home` answered from cache. Under dev activation those name the same
  # path, so one tuple carried two release ids for one path -- defect C's own
  # shape, reintroduced by the fix for defect C.
  #
  # `resolve_agent_home()` validates a harness root and would reject a fixture
  # repo (an earlier version of this test set AGENT_HOME and silently tested
  # nothing, because runtime_root then resolved to the installed release and
  # never shared a path with the cwd). Patch the resolver so the two genuinely
  # are one path, which is what dev activation means.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp)
   previous=R.resolve_agent_home
   R.resolve_agent_home=lambda *a,**k: str(repo)
   try:
    first=R.launch_compatibility_tuple(artifact_root=str(Path(tmp)/"artifacts"),cwd=repo)
    stale=self._git_head(repo)
    self.assertEqual(first["runtime_root"]["path"],str(repo))
    self.assertEqual(first["grounding_roots"]["cwd"]["path"],str(repo))
    self.assertEqual(first["runtime_root"]["release_id"],stale)
    (repo/"x").write_text("moved")
    subprocess.run(["git","-C",str(repo),"commit","-qam","moved"],check=True)
    fresh=self._git_head(repo)
    self.assertNotEqual(fresh,stale)
    tuple_=R.launch_compatibility_tuple(
     artifact_root=str(Path(tmp)/"artifacts"),cwd=repo,refresh_cwd=True,
    )
    by_path={}
    for identity in (
     tuple_["runtime_root"],tuple_["launch_home"],tuple_["registry_root"],
     tuple_["grounding_roots"]["cwd"],
    ):
     by_path.setdefault(identity["path"],set()).add(identity["release_id"])
    self.assertIn(str(repo),by_path)
    for path,revisions in by_path.items():
     self.assertEqual(len(revisions),1,f"{path} carries {revisions}")
    self.assertEqual(by_path[str(repo)],{fresh})
   finally:
    R.resolve_agent_home=previous

 def test_c_lineage_recheck_reaches_a_repo_subdirectory(self):
  # S3: `(cwd/".git").exists()` answers only for a repository root, so a route
  # whose cwd is any subdirectory skipped the recheck entirely -- the tamper case
  # below passed verification. Ask git whether it is inside a worktree instead.
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp)
   nested=repo/"pkg"/"sub"; nested.mkdir(parents=True)
   self.assertFalse((nested/".git").exists())
   self.assertTrue(R._inside_git_worktree(nested))
   self.assertTrue(R._inside_git_worktree(repo))
   self.assertFalse(R._inside_git_worktree(Path(tmp)/"not-a-repo"))
   # And the call site: a rebind claiming a lineage the tree denies must be
   # refused when the route cwd is that subdirectory, not waved through.
   previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   jobs=Path(tmp)/"state"/"jobs.log"; jobs.parent.mkdir(parents=True)
   os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   try:
    import subprocess
    artifact=Path(tmp)/"artifacts"
    source=self._source(artifact,cwd=repo)
    self._complete_prefix(source,"plan",Path(tmp)/"evidence")
    (repo/"x").write_text("b")
    subprocess.run(["git","-C",str(repo),"commit","-qam","fast-forward"],check=True)
    built=self._build(source,resume_from_node="plan",requested_boundary="plan")
    self.assertIn("source_commit_rebind",built)
    R._verify_continuation_route(json.loads(json.dumps(built)))
    tampered=json.loads(json.dumps(built))
    tampered["cwd"]=str(nested)
    tampered["source_commit_rebind"]["cwd"]=str(nested)
    tampered["source_commit_rebind"]["inherited_source_commit"]="c"*40
    with self.assertRaisesRegex(
     ValueError,"continuation-source-commit-rebind-lineage-unproven"
    ):
     R._verify_continuation_route(tampered)
   finally:
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs

 def test_c_builder_asserts_pin_against_grounding(self):
  # S5: round 1 added the assertion but nothing covered its call site, so the
  # builder could have stopped calling it without a test noticing.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   jobs=Path(tmp)/"state"/"jobs.log"; jobs.parent.mkdir(parents=True)
   os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   try:
    repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
    source=self._source(artifact,cwd=repo)
    self._complete_prefix(source,"plan",Path(tmp)/"evidence")
    (repo/"x").write_text("b")
    subprocess.run(["git","-C",str(repo),"commit","-qam","fast-forward"],check=True)
    calls=[]
    real=R._assert_pin_matches_grounding
    def spy(source_commit,launch):
     calls.append((source_commit,launch))
     return real(source_commit,launch)
    R._assert_pin_matches_grounding=spy
    try:
     built=self._build(source,resume_from_node="plan",requested_boundary="plan")
    finally:
     R._assert_pin_matches_grounding=real
    self.assertEqual(len(calls),1)
    pin,launch=calls[0]
    self.assertEqual(pin,built["source_commit"])
    self.assertIs(launch,built["launch_compatibility_tuple"])
   finally:
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs


 def test_c_decline_survives_another_continuation_generation(self):
  # Review round 3, B1 -- the decline was defeated by building one more
  # continuation. A declined continuation records no attempts of its own (the
  # guard refuses its whole pre-mutation prefix), so a continuation built FROM
  # it saw a clean registry, did not decline, and re-pinned. `execute` then met
  # `head == source_commit` and was accepted on the guard's trivial branch --
  # round 2's defect, one generation later. main refuses at every generation.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
   jobs=Path(os.environ["AGENT_DISPATCH_JOBS"])
   source=self._source(artifact,cwd=repo)
   pinned=source["source_commit"]
   self._complete_prefix(source,"plan",Path(tmp)/"evidence")
   self._write_attempt_row(jobs,source["route_id"],"execute","att-execute-prior")
   (repo/"x").write_text("b")
   subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
   moved=self._git_head(repo)
   first=self._build(source,resume_from_node="plan",requested_boundary="plan")
   self.assertEqual(first["source_commit"],pinned)
   # The declined continuation is a valid source route with its own new id, and
   # it wrote no rows -- which is exactly why asking only about it is not enough.
   self.assertNotEqual(first["route_id"],source["route_id"])
   self.assertEqual(
    self._stage_rows(jobs,first["route_id"]),[],
    "a declined continuation should have no registry rows of its own",
   )
   second=self._build(first,resume_from_node="plan",requested_boundary="plan")
   self.assertEqual(
    second["source_commit"],pinned,
    "the decline must survive a second continuation generation",
   )
   self.assertNotIn("source_commit_rebind",second)
   self.assertIn("execute",[node["id"] for node in second["nodes"]])
   # And the ancestor whose rows carry the evidence is reachable from the
   # second-generation source route.
   self.assertIn(source["route_id"],R.continuation_lineage_route_ids(first))
   self.assertNotEqual(moved,pinned)

 def _stage_rows(self,jobs,route_id):
  fallback=R._stage_fallback()
  return fallback.registry_route_rows(Path(jobs),[route_id])

 def test_c_unprovable_registry_declines_the_rebind(self):
  # Review round 3, B2. `registry_rows` returns [] for a missing file, for a
  # truncated one, and for a registry that simply never saw this route -- three
  # different truths, one signal. Absence is only evidence once the registry is
  # proved to be the lineage's own and proved to hold that lineage.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
   jobs=Path(os.environ["AGENT_DISPATCH_JOBS"])
   source=self._source(artifact,cwd=repo)
   pinned=source["source_commit"]
   self._complete_prefix(source,"execute",Path(tmp)/"evidence")
   self._write_attempt_row(jobs,source["route_id"],"execute","att-execute-prior")
   (repo/"x").write_text("b")
   subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
   moved=self._git_head(repo)
   self.assertNotEqual(moved,pinned)
   sealed=Path(source["launch_compatibility_tuple"]["jobs_path"]["path"])
   intact=sealed.read_text(encoding="utf-8")

   def rebuild():
    return self._build(source,resume_from_node="execute",requested_boundary="execute")

   # Positive control (S2): with the registry intact and readable, this same
   # fixture rebinds once the mutation node's row is gone -- so a decline below
   # is attributable to the registry state and not to anything else.
   self._drop_attempt_rows(sealed,node_id="execute")
   self.assertEqual(rebuild()["source_commit"],moved)
   sealed.write_text(intact,encoding="utf-8")
   self.assertEqual(rebuild()["source_commit"],pinned)

   cases={}
   def restore():
    if sealed.is_dir(): sealed.rmdir()
    elif sealed.exists(): sealed.unlink()
    sealed.parent.mkdir(parents=True,exist_ok=True)
    sealed.write_text(intact,encoding="utf-8")

   restore(); sealed.unlink(); cases["deleted"]=rebuild()
   restore(); sealed.unlink(); sealed.mkdir(); cases["directory"]=rebuild()
   # B2b: truncation. The docstring named it; only deletion was covered, and two
   # tests actively asserted that an empty registry rebinds.
   restore(); sealed.write_text("",encoding="utf-8"); cases["truncated"]=rebuild()
   # A registry that is readable but never saw this lineage: same empty read,
   # same ambiguity.
   restore(); self._write_attempt_row(sealed,"rt-someone-elses","execute","att-other")
   sealed.write_text("".join(
    f"{line}\n" for line in sealed.read_text(encoding="utf-8").splitlines()
    if source["route_id"] not in line
   ),encoding="utf-8")
   cases["foreign-lineage"]=rebuild()
   restore()
   for name,route in cases.items():
    with self.subTest(name):
     if name == "directory":
      self.assertEqual(route["first_runnable_blocker"],
       "continuation-source-node-unverified:frame:registry-unreadable")
      self.assertEqual(route["new_nodes"],[])
      self.assertNotIn("source_commit",route)
     else:
      self.assertEqual(route["source_commit"],pinned)
     self.assertEqual(source["source_commit"],pinned)
     self.assertNotIn("source_commit_rebind",route)

 def test_c_pruned_release_registry_is_not_authoritative(self):
  # Review round 3, B2a. When the sealed dispatch root's parent is gone,
  # `_continuation_source_jobs` falls through to the LIVE canonical registry so
  # that migrated markers stay readable. That substitution is safe for markers
  # (each carries its own route binding) and unsafe here, where the evidence is
  # an absent row: the live file exists, is a regular file, and holds no rows
  # for the old route id.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
   sealed_root=Path(tmp)/"releases"/"v1"/".dispatch"
   sealed_root.mkdir(parents=True)
   sealed=sealed_root/"jobs.log"
   previous=os.environ.get("AGENT_DISPATCH_JOBS")
   os.environ["AGENT_DISPATCH_JOBS"]=str(sealed)
   try:
    source=self._source(artifact,cwd=repo)
    self.assertEqual(source["launch_compatibility_tuple"]["jobs_path"]["path"],str(sealed))
    pinned=source["source_commit"]
    # Resume at the first node, so no reused-evidence markers are needed and
    # pruning the sealed tree cannot block the build for an unrelated reason.
    self._write_attempt_row(sealed,source["route_id"],"execute","att-execute-prior")
    (repo/"x").write_text("b")
    subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
    # Control: the sealed registry is intact, so the decline is provable.
    self.assertEqual(
     self._build(source,resume_from_node="frame",requested_boundary="frame")["source_commit"],
     pinned,
    )
    self.assertIsNotNone(R._authoritative_lineage_registry(source))
    # Prune the release tree the sealed root lived in, and stand up a live
    # canonical registry that never saw this route -- the observed shape the
    # compat window exists for (release pruning, 2026-08-27).
    import shutil
    shutil.rmtree(Path(tmp)/"releases")  # reason=fixture boundary=test-tmpdir
    live=Path(tmp)/"state"/"jobs.log"; live.parent.mkdir(parents=True,exist_ok=True)
    live.write_text("",encoding="utf-8")
    os.environ["AGENT_DISPATCH_JOBS"]=str(live)
    # The fall-through registry is a real, readable, regular file with no rows
    # for this route -- which the old read scored as "never ran".
    resolved=R._continuation_source_jobs(source)
    self.assertTrue(Path(resolved).is_file())
    self.assertEqual(self._stage_rows(resolved,source["route_id"]),[])
    self.assertIsNone(
     R._authoritative_lineage_registry(source),
     "a compat-window substitution is not the lineage's own registry",
    )
    declined=self._build(source,resume_from_node="frame",requested_boundary="frame")
    self.assertEqual(declined["source_commit"],pinned)
    self.assertNotIn("source_commit_rebind",declined)
   finally:
    if previous is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous

 def _drop_attempt_rows(self,jobs,node_id):
  """Remove one node's rows, keeping the rest of the lineage's.

  Emptying the registry is a different case entirely: an empty read cannot
  distinguish "this node never ran" from "someone truncated the file", so it
  declines. A fixture that wants "no attempt on this node" has to say exactly
  that.
  """
  jobs=Path(jobs)
  kept=[line for line in jobs.read_text(encoding="utf-8").splitlines()
        if f"route_node={node_id}," not in line+"," ]
  jobs.write_text("".join(f"{line}\n" for line in kept),encoding="utf-8")

 def _write_attempt_row(self,jobs,route_id,node_id,attempt_id,status="done"):
  # A fixture registry is a tmpdir registry. A route built without
  # AGENT_DISPATCH_JOBS pointed at one seals the *canonical* jobs path, and
  # appending there writes fake attempt rows into the operator's live registry
  # -- measured, 660 rows, after `_complete_node` started registering every
  # completed node. Refuse loudly instead of polluting shared state.
  jobs=Path(jobs)
  if not self._is_fixture_registry(jobs):
   raise AssertionError(
    f"refusing to write a fixture attempt row to a non-tmpdir registry: {jobs}. "
    "Set AGENT_DISPATCH_JOBS to a path under the test's TemporaryDirectory "
    "before building the route."
   )
  meta=f"route_id={route_id},route_node={node_id},attempt_id={attempt_id}"
  jobs.parent.mkdir(parents=True,exist_ok=True)
  with jobs.open("a",encoding="utf-8") as handle:
   handle.write("\t".join(["2026-09-04T00:00:00Z",status,"repo","worktree","slug",meta])+"\n")

 @staticmethod
 def _is_fixture_registry(jobs):
  # Every scratch root a fixture may legitimately live under, not just the one
  # `tempfile.gettempdir()` names right now. `tools/run-tests.py`'s isolated
  # profile repoints TMPDIR at a per-invocation directory, while the F47-3
  # golden fixture hardcodes `/tmp` on purpose (its path is hashed into route
  # identity, so it must not drift). With one source this guard refused that
  # fixture under the runner and passed it everywhere else -- the same
  # two-sources-for-one-value shape it was written to catch. The live registry
  # is under XDG state and can be under none of these.
  candidates={tempfile.gettempdir(),"/tmp"}
  candidates.add(os.environ.get("TMPDIR") or "/tmp")
  target=Path(jobs).resolve(strict=False)
  for candidate in candidates:
   try:
    target.relative_to(Path(candidate).resolve(strict=False))
   except ValueError:
    continue
   return True
  return False

 def test_c_pin_and_grounding_must_name_one_commit(self):
  # S5: the pin (`git rev-parse HEAD`) and the grounding (`source_revision`) are
  # read by different functions at different moments -- exactly defect C's shape.
  # The builder asserts they agree instead of arguing that they do.
  def launch(release_id):
   return {"grounding_roots":{"cwd":{"release_id":release_id}}}
  head="a"*40; other="b"*40
  R._assert_pin_matches_grounding(head,launch(head))
  R._assert_pin_matches_grounding(head,launch(head+"+dirty:0123456789ab"))
  with self.assertRaisesRegex(ValueError,"continuation-source-commit-grounding-mismatch"):
   R._assert_pin_matches_grounding(head,launch(other))
  with self.assertRaisesRegex(ValueError,"continuation-source-commit-grounding-mismatch"):
   R._assert_pin_matches_grounding(head,launch(other+"+dirty:0123456789ab"))
  # Non-git grounding shapes carry no commit to compare and are left alone.
  for shape in ("release:v2.109.0:3b081cf992a6","tree:88a3ef6ba1f9a6071812",None,17):
   R._assert_pin_matches_grounding(head,launch(shape))
  R._assert_pin_matches_grounding(None,launch(head))

 def test_c_continuation_rebind_record_is_verified(self):
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
   source=self._source(artifact,cwd=repo)
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-qam","fast-forward"],check=True)
   continuation=self._build(source)
   self.assertEqual(continuation["source_commit"],self._git_head(repo))
   R.verify_route(continuation)
   for tamper in (
    lambda route:route["source_commit_rebind"].update(rebound_source_commit=route["source_commit_rebind"]["inherited_source_commit"]),
    lambda route:route["source_commit_rebind"].update(basis="cherry-pick"),
    lambda route:route["source_commit_rebind"].update(contract_version=2),
    lambda route:route["source_commit_rebind"].pop("inherited_source_commit"),
    lambda route:route["source_commit_rebind"].update(cwd="/nonexistent/elsewhere"),
   ):
    tampered=json.loads(json.dumps(continuation)); tamper(tampered)
    tampered["route_hash"]=R.route_hash(tampered)
    tampered["route_id"]="rt-"+tampered["route_hash"].split(":",1)[1][:16]
    with self.assertRaisesRegex(ValueError,"continuation-source-commit-rebind-invalid"):
     R.verify_route(tampered)
   # A diverged HEAD (the pinned root commit itself rewritten) is refused typed,
   # with no route built. Amending only the fast-forward commit would still
   # descend from the pin and is legitimately a rebind, not a divergence.
   subprocess.run(["git","-C",str(repo),"reset","-q","--hard",source["source_commit"]],check=True)
   subprocess.run(["git","-C",str(repo),"commit","--amend","-qm","rewritten"],check=True)
   with self.assertRaisesRegex(ValueError,"continuation-source-commit-diverged"):
    self._build(source)

 def test_continuation_cli_requires_exact_registered_depth1_owner(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"; jobs=Path(tmp)/"state"/"jobs.log"
   previous_home=os.environ.get("AGENT_HOME"); previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   os.environ["AGENT_HOME"]=str(R.ROOT); os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   try:
    source=self._source(artifact); self._complete_prefix(source,"test",Path(tmp)/"evidence")
    source_path=Path(tmp)/"source-route.json"; source_path.write_text(json.dumps(source),encoding="utf-8")
    jobs.parent.mkdir(parents=True, exist_ok=True); jobs.write_text(
     "2099-01-01T00:00:00Z\topen\t%s\t%s\towner\t"
     "attempt_schema_version=2,worker_type=owner,unit=_kernel/owner,"
     "dispatch_depth=1,registered_worker=1,execution_surface=registered-headless,"
     "capability=autopilot-code,capability_mode=dev,intensity=strong,artifact_root=%s,"
     "owner_harness=codex,"
     "parent_sid=thread-source,"
     "attempt_id=att-cli-owner,owner_route_file=%s,owner_route_id=%s,owner_route_hash=%s\n"
     % (R.ROOT, R.ROOT, source["artifact_root"], source_path, source["route_id"], source["route_hash"]), encoding="utf-8")
    env=os.environ.copy()
    for key in ("AGENT_DISPATCH_OWNER_HARNESS","AGENT_DISPATCH_CURRENT_HARNESS",
                "AGENT_DISPATCH_WORKER_TYPE","AGENT_DISPATCH_DEPTH",
                "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION",
                "AGENT_DISPATCH_EXECUTION_SURFACE","AGENT_DISPATCH_REGISTERED_WORKER",
                "CODEX_THREAD_ID","CLAUDE_CODE_SESSION_ID","AGENT_DISPATCH_PARENT_SESSION_ID"):
     env.pop(key,None)
    env["AGENT_DISPATCH_ATTEMPT_ID"]="att-cli-owner"; env["AGENT_OWNER_ROUTE_FILE"]=str(source_path)
    env["AGENT_OWNER_ROUTE_ID"]=source["route_id"]
    env["AGENT_OWNER_ROUTE_HASH"]=source["route_hash"]
    env["AGENT_DISPATCH_DEPTH"]="1"
    env["AGENT_DISPATCH_REGISTERED_WORKER"]="1"
    env["AGENT_DISPATCH_WORKER_TYPE"]="owner"
    env["AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION"]="2"
    env["AGENT_DISPATCH_EXECUTION_SURFACE"]="registered-headless"
    command=[sys.executable,str(P),"continuation","--source-route",str(source_path),"--resume-from-node","test",
             "--requested-boundary","test","--reason","cli-owner","--artifact-root",str(artifact)]
    result=subprocess.run(command,capture_output=True,text=True,cwd=str(R.ROOT),env=env)
    self.assertEqual(result.returncode,0,result.stderr)
    self.assertIn("owner_route_advance_written=1",result.stderr)
   finally:
    if previous_home is None: os.environ.pop("AGENT_HOME",None)
    else: os.environ["AGENT_HOME"]=previous_home
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs

 def test_continuation_cli_reports_route_written_without_owner_advance(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"; jobs=Path(tmp)/"state"/"jobs.log"
   previous_home=os.environ.get("AGENT_HOME"); previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   os.environ["AGENT_HOME"]=str(R.ROOT); os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   try:
    source=self._source(artifact); self._complete_prefix(source,"test",Path(tmp)/"evidence")
    source_path=Path(tmp)/"source-route.json"; source_path.write_text(json.dumps(source),encoding="utf-8")
    jobs.parent.mkdir(parents=True, exist_ok=True); jobs.write_text(
     "2099-01-01T00:00:00Z\topen\trepo\t%s\tstage\t"
     "attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
     "worker_type=stage,unit=dev/backend,attempt_id=att-cli-stage\n" % R.ROOT,
     encoding="utf-8")
    env=os.environ.copy(); env["AGENT_DISPATCH_ATTEMPT_ID"]="att-cli-stage"
    env["AGENT_DISPATCH_DEPTH"]="2"
    env["AGENT_DISPATCH_REGISTERED_WORKER"]="1"
    env["AGENT_DISPATCH_WORKER_TYPE"]="stage"
    for key in ("AGENT_OWNER_ROUTE_FILE", "AGENT_OWNER_ROUTE_ID", "AGENT_OWNER_ROUTE_HASH"):
     env.pop(key, None)
    command=[sys.executable,str(P),"continuation","--source-route",str(source_path),"--resume-from-node","test",
             "--requested-boundary","test","--reason","cli-stage","--artifact-root",str(artifact)]
    result=subprocess.run(command,capture_output=True,text=True,cwd=str(R.ROOT),env=env)
    self.assertEqual(result.returncode,0,result.stderr)
    self.assertIn("route_file=",result.stderr)
    self.assertTrue(any((artifact/".runtime"/"routes").glob("*.json")))
   finally:
    if previous_home is None: os.environ.pop("AGENT_HOME",None)
    else: os.environ["AGENT_HOME"]=previous_home
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs

 def test_continuation_cli_owner_advance_is_atomic_on_exact_replay(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"; jobs=Path(tmp)/"state"/"jobs.log"
   previous_home=os.environ.get("AGENT_HOME"); previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   os.environ["AGENT_HOME"]=str(R.ROOT); os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   try:
    source=self._source(artifact); self._complete_prefix(source,"test",Path(tmp)/"evidence")
    source_path=Path(tmp)/"source-route.json"; source_path.write_text(json.dumps(source),encoding="utf-8")
    jobs.parent.mkdir(parents=True, exist_ok=True); jobs.write_text(
     "2099-01-01T00:00:00Z\topen\t%s\t%s\towner\t"
     "attempt_schema_version=2,worker_type=owner,unit=_kernel/owner,"
     "dispatch_depth=1,registered_worker=1,execution_surface=registered-headless,"
     "capability=autopilot-code,capability_mode=dev,intensity=strong,artifact_root=%s,"
     "owner_harness=codex,"
     "parent_sid=thread-source,"
     "attempt_id=att-cli-replay,"
     "owner_route_file=%s,owner_route_id=%s,owner_route_hash=%s\n"
     % (R.ROOT, R.ROOT, source["artifact_root"], source_path, source["route_id"], source["route_hash"]), encoding="utf-8")
    env=os.environ.copy()
    for key in ("AGENT_DISPATCH_OWNER_HARNESS","AGENT_DISPATCH_CURRENT_HARNESS",
                "AGENT_DISPATCH_WORKER_TYPE","AGENT_DISPATCH_DEPTH",
                "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION",
                "AGENT_DISPATCH_EXECUTION_SURFACE","AGENT_DISPATCH_REGISTERED_WORKER",
                "CODEX_THREAD_ID","CLAUDE_CODE_SESSION_ID","AGENT_DISPATCH_PARENT_SESSION_ID"):
     env.pop(key,None)
    env["AGENT_DISPATCH_ATTEMPT_ID"]="att-cli-replay"; env["AGENT_OWNER_ROUTE_FILE"]=str(source_path)
    env["AGENT_OWNER_ROUTE_ID"]=source["route_id"]
    env["AGENT_OWNER_ROUTE_HASH"]=source["route_hash"]
    env["AGENT_DISPATCH_DEPTH"]="1"
    env["AGENT_DISPATCH_REGISTERED_WORKER"]="1"
    env["AGENT_DISPATCH_WORKER_TYPE"]="owner"
    env["AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION"]="2"
    env["AGENT_DISPATCH_EXECUTION_SURFACE"]="registered-headless"
    command=[sys.executable,str(P),"continuation","--source-route",str(source_path),"--resume-from-node","test",
             "--requested-boundary","test","--reason","cli-replay","--artifact-root",str(artifact)]
    first=subprocess.run(command,capture_output=True,text=True,cwd=str(R.ROOT),env=env)
    second=subprocess.run(command,capture_output=True,text=True,cwd=str(R.ROOT),env=env)
    self.assertEqual(first.returncode,0,first.stderr); self.assertEqual(second.returncode,0,second.stderr)
    records=list((jobs.parent/"owner-route-advances").rglob("*.json"))
    self.assertEqual(len(records),1)
    self.assertIn("owner_route_advance_written=1",second.stderr)
   finally:
    if previous_home is None: os.environ.pop("AGENT_HOME",None)
    else: os.environ["AGENT_HOME"]=previous_home
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs
 def test_f47_3_continuation_outputs_byte_identical(self):
  """SD-118 (F47-3): the 6 SD-104 continuation outputs are byte-identical to a
  golden captured before SD-118 touched capability-route.py/tools/fleet/route.py
  (plan.md §7.2). Fixture is generated once via F47_3_EMIT_GOLDEN=1 and never
  regenerated afterward -- this test only compares."""
  golden_path=R.ROOT/"utilities"/"fixtures"/"f47-3-continuation-golden.json"
  keys=(
   "source_route_supersession","supersession_edges","continuation_id",
   "reused_nodes","source_evidence_digest","continuation_contract_version",
  )
  # A random per-run tmp dir would make artifact_root/cwd part of the
  # hashed identity differ run-to-run (route_hash/continuation_id inputs,
  # not just printed paths), so this fixed root is reused and wiped every
  # run instead -- only its string form gets tokenized out below.
  # A hardcoded /tmp path, not tempfile.gettempdir(): tools/run-tests.py's
  # isolated profile sets TMPDIR to a unique per-invocation directory, which
  # would make gettempdir() (and therefore artifact_root/cwd, which are
  # hashed into route identity, not just printed) drift on every isolated
  # subprocess run and defeat the whole point of a fixed root.
  root=Path("/tmp")/"hearting-f47-3-golden-fixture"
  if root.exists(): shutil.rmtree(root)
  root.mkdir(parents=True)
  jobs=root/"state"/"jobs.log"
  # `validation_basis.runtime_root` seals `resolve_agent_home()` into the
  # route payload (route_hash input), so the class setUp()'s per-test random
  # AGENT_HOME tmp dir must be pinned to this fixed path too, or route_hash
  # (and everything downstream: route_id, continuation_id, edge ids) differs
  # every run even with a frozen clock and a fixed artifact/evidence root.
  agent_home=root/"agent-home"
  (agent_home/"core").mkdir(parents=True)
  (agent_home/"core"/"CORE.md").write_text("continuation fixture\n",encoding="utf-8")
  previous_agent_home=os.environ.get("AGENT_HOME")
  os.environ["AGENT_HOME"]=str(agent_home)
  previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
  os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
  # `_seal_dispatch_defaults()` reads dispatch-defaults config from
  # `$XDG_CONFIG_HOME/hearting/dispatch-defaults.yaml` (or `~/.config/...`),
  # NOT from AGENT_HOME -- an interactive dev shell with a real user config
  # (e.g. an opencode depth-affinity override) computes a different
  # `dispatch_defaults_digest`/`harness_affinity`/`last_resort` set than the
  # sandboxed HOME the isolated test runner uses, which changes every hash
  # downstream. Pin explicitly to the shipped repo default so the golden
  # comparison is identical in both environments.
  previous_defaults_config=os.environ.get("DISPATCH_DEFAULTS_CONFIG")
  os.environ["DISPATCH_DEFAULTS_CONFIG"]=str(R.ROOT/"profiles"/"dispatch-defaults.yaml")
  import datetime as _datetime_module
  from unittest import mock
  class _FrozenDatetime(_datetime_module.datetime):
   @classmethod
   def now(cls,tz=None):
    return _datetime_module.datetime(2026,8,29,0,0,0,tzinfo=tz)
  # `_launch_root_identity()` memoizes `_launch_source_revision()`/
  # `_launch_content_digest()` results per resolved path at module scope, so
  # an earlier test in the same process that already compiled a route for
  # this same repo path (R.ROOT) leaves a REAL (dirty-state-sensitive) entry
  # cached -- the mock.patch.object() below would then never even be called.
  # Clearing these three caches for the duration guarantees the frozen
  # values are what actually gets cached and used here, regardless of test
  # execution order; they are restored afterward so no other test's cached
  # identity is disturbed.
  saved_caches=(
   dict(R._LAUNCH_ROOT_IDENTITY_CACHE),
   dict(R._LAUNCH_CONTENT_DIGEST_CACHE),
   dict(R._LAUNCH_SOURCE_REVISION_CACHE),
  )
  R._LAUNCH_ROOT_IDENTITY_CACHE.clear()
  R._LAUNCH_CONTENT_DIGEST_CACHE.clear()
  R._LAUNCH_SOURCE_REVISION_CACHE.clear()
  # `_git_commit()` shells out to `git rev-parse HEAD` and silently falls
  # back to the literal string "unversioned" on any nonzero exit -- which is
  # exactly what happens under a sandboxed HOME with no global gitconfig
  # (git's "detected dubious ownership" safe.directory check, reproduced
  # directly: `env -i PATH="$PATH" git -C <this worktree> rev-parse HEAD`
  # exits 128). `source_commit` feeds route_hash, so this must be frozen too.
  previous_owner_attempt=os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID",None)
  try:
   with mock.patch("datetime.datetime",_FrozenDatetime), \
        mock.patch.object(R,"_launch_source_revision",lambda path:"golden-fixed-revision"), \
        mock.patch.object(R,"_launch_content_digest",lambda path:"sha256:"+"0"*64), \
        mock.patch.object(R,"_git_commit",lambda cwd:"golden-fixed-commit"):
    # write_completion_marker() stamps a real wall clock into `completed_at`
    # (frozen above), and launch_compatibility_tuple() seals the *current
    # uncommitted git diff* of the whole worktree into release_id/
    # binding_digest via _launch_source_revision() -- both flow into
    # route_hash and therefore into every hash in the 6 compared keys. Since
    # this worktree is edited continuously across the SD-113/114/118 cycle
    # (by this round and by sibling files), that diff is guaranteed to differ
    # between golden capture and any later comparison unless frozen here too.
    # Neither is an SD-118 concern -- both are pre-existing properties of
    # shared helpers this test reuses, not of the two files SD-118 touches.
    # `cwd` is hashed into route identity (route_hash -> continuation_id);
    # the checkout path (R.ROOT) would bind the golden to one worktree, so
    # compile from a fixed cwd under the fixture root instead.
    (root/"cwd").mkdir(parents=True,exist_ok=True)
    source=self._source(root/"artifacts",cwd=root/"cwd")
    self._complete_prefix(source,"test",root/"evidence")
    continuation=self._build(source)
  finally:
   if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
   else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs
   if previous_agent_home is None: os.environ.pop("AGENT_HOME",None)
   else: os.environ["AGENT_HOME"]=previous_agent_home
   if previous_defaults_config is None: os.environ.pop("DISPATCH_DEFAULTS_CONFIG",None)
   else: os.environ["DISPATCH_DEFAULTS_CONFIG"]=previous_defaults_config
   if previous_owner_attempt is None: os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID",None)
   else: os.environ["AGENT_DISPATCH_ATTEMPT_ID"]=previous_owner_attempt
   shutil.rmtree(root,ignore_errors=True)
   R._LAUNCH_ROOT_IDENTITY_CACHE.clear(); R._LAUNCH_ROOT_IDENTITY_CACHE.update(saved_caches[0])
   R._LAUNCH_CONTENT_DIGEST_CACHE.clear(); R._LAUNCH_CONTENT_DIGEST_CACHE.update(saved_caches[1])
   R._LAUNCH_SOURCE_REVISION_CACHE.clear(); R._LAUNCH_SOURCE_REVISION_CACHE.update(saved_caches[2])
  payload={key:continuation[key] for key in keys}
  serialized=json.dumps(
   payload,sort_keys=True,separators=(",",":"),ensure_ascii=False)
  serialized=serialized.replace(str(root),"<state-root>")
  # The checkout path itself is sealed into the route payload too
  # (`validation_basis.registry_root`/`runtime_root` = R.ROOT), so a golden
  # captured in one worktree failed in every other checkout and in CI
  # (2026-08-30). Tokenize it the same way as the fixed state root.
  serialized=serialized.replace(str(R.ROOT),"<repo-root>")
  # route_hash seals the checkout-bound `validation_basis` roots (registry/
  # unit-catalog/runtime), so every derived identity (route_id, continuation
  # id, edge ids, marker digests) legitimately differs per checkout. Replace
  # each such identity with a stable ordinal placeholder in order of first
  # appearance: the golden then pins structure, ordering, and identity
  # *relations* (same id -> same placeholder) instead of one worktree's hashes.
  _ordinals={}
  def _placeholder(match):
    key=match.group(0)
    kind=key.split(":",1)[0] if key.startswith("sha256:") else key.split("-",1)[0]
    if key not in _ordinals: _ordinals[key]=f"<{kind}#{len(_ordinals)}>"
    return _ordinals[key]
  serialized=re.sub(r"sha256:[0-9a-f]{64}|rt-[0-9a-f]{16}|cont-[0-9a-f]{32}",_placeholder,serialized)
  if os.environ.get("F47_3_EMIT_GOLDEN")=="1":
   golden_path.parent.mkdir(parents=True,exist_ok=True)
   golden_path.write_text(json.dumps({
    "base_commit":R._git_commit(R.ROOT),
    "generated_before_sd118":False,
    "note":"regenerated after tokenizing the checkout path; guards drift from the regeneration commit onward",
    "payload":serialized,
   },indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
   return
  if not golden_path.is_file():
   self.fail(
    "utilities/fixtures/f47-3-continuation-golden.json missing -- "
    "run once with F47_3_EMIT_GOLDEN=1 before touching capability-route.py "
    "or tools/fleet/route.py (plan.md §7.2)")
  golden=json.loads(golden_path.read_text(encoding="utf-8"))
  self.assertEqual(serialized,golden["payload"])


class TestValidationBasis(unittest.TestCase):
 """B-2: sealed `validation_basis` provenance and its classifier (task-brief §2, plan §5.1)."""
 def setUp(self):
  self._tmp_home=tempfile.TemporaryDirectory()
  (Path(self._tmp_home.name)/"core").mkdir(parents=True,exist_ok=True)
  (Path(self._tmp_home.name)/"core"/"CORE.md").write_text("fixture\n",encoding="utf-8")
  self._previous_agent_home=os.environ.get("AGENT_HOME")
  os.environ["AGENT_HOME"]=self._tmp_home.name
  self._previous_dispatch_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
  os.environ.pop("AGENT_DISPATCH_JOBS",None)
  self.addCleanup(self._restore_agent_home)
 def _restore_agent_home(self):
  if self._previous_agent_home is None: os.environ.pop("AGENT_HOME",None)
  else: os.environ["AGENT_HOME"]=self._previous_agent_home
  if self._previous_dispatch_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
  else: os.environ["AGENT_DISPATCH_JOBS"]=self._previous_dispatch_jobs
  self._tmp_home.cleanup()
 def args(self,**kw):
  gate={"spec_read":{"satisfied":True,"source":"canonical-prd-sha256"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"conductor-prechecked"}}
  d=dict(capability="autopilot-code",capability_mode="dev",requested_intensity="direct",cwd=R.ROOT,artifact_root=R.ROOT,predicates=ALL,transport=None,inline_reason="atomic-direct",tracking="tracked",tracked_gate_evidence=gate); d.update(kw); return d
 def _reseal(self,route):
  route=json.loads(json.dumps(route))
  route["route_hash"]=R.route_hash(route); route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  return route
 def _release(self,root,version="v9.8.7",archive="a"*64,marker_updates=None):
  root=Path(root); root.mkdir(parents=True,exist_ok=True)
  for relative in R._LAUNCH_CODE_ANCHORS:
   path=root/relative; path.parent.mkdir(parents=True,exist_ok=True)
   path.write_text(relative+"\n",encoding="utf-8")
  extra=root/"adapters"/"codex"/"bin"/"preflight.sh"
  extra.parent.mkdir(parents=True,exist_ok=True)
  extra.write_text("#!/bin/sh\nexit 0\n",encoding="utf-8")
  (root/"RELEASE_VERSION").write_text(version+"\n",encoding="utf-8")
  marker={
   "schema":1,"version":version,"archive_sha256":archive,
   "published_at":"2026-09-01T00:00:00+00:00",
  }
  if marker_updates: marker.update(marker_updates)
  (root/".hearting-release.json").write_text(
   json.dumps(marker,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
  return root
 def test_immutable_code_root_symlink_alias_passes(self):
  with tempfile.TemporaryDirectory() as tmp:
   release=self._release(Path(tmp)/"release")
   alias=Path(tmp)/"alias"; alias.symlink_to(release,target_is_directory=True)
   self.assertTrue(R.immutable_code_root_equivalent(release,alias))
 def test_distinct_verified_immutable_release_copy_passes(self):
  with tempfile.TemporaryDirectory() as tmp:
   left=self._release(Path(tmp)/"bundle")
   right=self._release(Path(tmp)/"managed-release")
   self.assertNotEqual(left.resolve(),right.resolve())
   self.assertTrue(R.immutable_code_root_equivalent(left,right))
   with mock.patch.object(R.TOPO,"ROOT",left), \
        mock.patch.object(R,"resolve_agent_home",return_value=right):
    basis=R._validation_basis()
   self.assertTrue(basis["runtime_root_validated"])
   self.assertTrue(basis["runtime_root_match"])
 def test_same_release_marker_with_modified_anchor_fails(self):
  with tempfile.TemporaryDirectory() as tmp:
   left=self._release(Path(tmp)/"left")
   right=self._release(Path(tmp)/"right")
   (right/"core"/"CORE.md").write_text("tampered\n",encoding="utf-8")
   self.assertFalse(R.immutable_code_root_equivalent(left,right))
 def test_different_release_or_non_anchor_content_fails(self):
  with tempfile.TemporaryDirectory() as tmp:
   left=self._release(Path(tmp)/"left")
   other_release=self._release(Path(tmp)/"other-release",version="v9.8.8")
   self.assertFalse(R.immutable_code_root_equivalent(left,other_release))
   same_marker=self._release(Path(tmp)/"other-content")
   (same_marker/"adapters"/"codex"/"bin"/"preflight.sh").write_text(
    "#!/bin/sh\nexit 7\n",encoding="utf-8")
   self.assertFalse(R.immutable_code_root_equivalent(left,same_marker))
 def test_incomplete_or_symlinked_release_marker_fails(self):
  with tempfile.TemporaryDirectory() as tmp:
   left=self._release(Path(tmp)/"left")
   incomplete=self._release(Path(tmp)/"incomplete",marker_updates={"archive_sha256":None})
   self.assertFalse(R.immutable_code_root_equivalent(left,incomplete))
   linked=self._release(Path(tmp)/"linked")
   marker=linked/".hearting-release.json"
   marker_bytes=marker.read_bytes(); marker.unlink()
   external=Path(tmp)/"external-marker.json"; external.write_bytes(marker_bytes)
   marker.symlink_to(external)
   self.assertFalse(R.immutable_code_root_equivalent(left,linked))
 def test_mutable_state_roots_remain_path_bound(self):
  with tempfile.TemporaryDirectory() as tmp:
   left=self._release(Path(tmp)/"release-a")
   right=self._release(Path(tmp)/"release-b")
   left_jobs=left/"state"/"jobs.log"; right_jobs=right/"state"/"jobs.log"
   left_jobs.parent.mkdir(); right_jobs.parent.mkdir()
   left_jobs.write_text("same\n",encoding="utf-8")
   right_jobs.write_text("same\n",encoding="utf-8")
   self.assertTrue(R.immutable_code_root_equivalent(left,right))
   self.assertFalse(D.agent_home_equivalent(left,right))
   self.assertFalse(D.agent_home_equivalent(left_jobs,right_jobs))
 def test_launch_revalidation_keeps_identical_replica_paths_exact(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   runtime_a=self._release(root/"runtime-a")
   runtime_b=self._release(root/"runtime-b")
   artifact_root=root/"artifacts"; cwd=root/"cwd"
   artifact_root.mkdir(); cwd.mkdir()
   jobs=root/"state"/"jobs.log"
   env={"AGENT_DISPATCH_JOBS":str(jobs)}
   with mock.patch.dict(os.environ,env,clear=False), \
        mock.patch.object(R,"resolve_agent_home",return_value=runtime_a):
    sealed={
     "contract_version":R.LAUNCH_COMPATIBILITY_TUPLE_VERSION,
     **R.launch_compatibility_tuple(artifact_root=artifact_root,cwd=cwd),
    }
   route={
    "launch_compatibility_tuple":sealed,
    "artifact_root":str(artifact_root),"cwd":str(cwd),
   }
   with mock.patch.dict(os.environ,env,clear=False), \
        mock.patch.object(R,"resolve_agent_home",return_value=runtime_b):
    compatible,mismatches=R.revalidate_launch_compatibility(route)
   self.assertFalse(compatible)
   self.assertIn("runtime_root",mismatches)
   self.assertEqual(mismatches["runtime_root"]["fields"],["binding_digest","path"])
 def test_runtime_preflights_pin_bundle_with_release_copy_present(self):
  with tempfile.TemporaryDirectory() as tmp:
   base=Path(tmp); home=base/"home"; codex_home=home/".codex"
   bundle=codex_home/".harness"/"bundles"/"fixture"/"source"
   ignored=shutil.ignore_patterns(
    ".git",".agent_reports",".claude_reports","__pycache__","*.pyc")
   shutil.copytree(R.ROOT,bundle,symlinks=True,ignore=ignored)
   marker={
    "schema":1,"version":"v9.8.7","archive_sha256":"b"*64,
    "published_at":"2026-09-01T00:00:00+00:00",
   }
   marker_bytes=json.dumps(marker,sort_keys=True,separators=(",",":"))+"\n"
   (bundle/"RELEASE_VERSION").write_text("v9.8.7\n",encoding="utf-8")
   (bundle/".hearting-release.json").write_text(marker_bytes,encoding="utf-8")
   release=base/"xdg"/"hearting"/"releases"/"v9.8.7"
   shutil.copytree(bundle,release,symlinks=True)
   codex_home.mkdir(parents=True,exist_ok=True)
   (codex_home/"hearting").symlink_to(bundle,target_is_directory=True)
   opencode_home=base/"config"/"opencode"
   opencode_home.mkdir(parents=True)
   (opencode_home/"hearting").symlink_to(bundle,target_is_directory=True)
   current=base/"xdg"/"hearting"/"current"
   current.symlink_to(release,target_is_directory=True)
   workspace=base/"workspace"; workspace.mkdir()
   env=os.environ.copy()
   for key in (
    "AGENT_HOME","CLAUDE_HOME","AGENT_DISPATCH_ATTEMPT_ID",
    "AGENT_ROUTE_FILE","AGENT_ROUTE_ID","AGENT_ROUTE_NODE",
    "OPENCODE_SESSION_ID",
   ): env.pop(key,None)
   env.update({
    "HOME":str(home),"CODEX_HOME":str(codex_home),
    "XDG_CONFIG_HOME":str(base/"config"),"XDG_DATA_HOME":str(base/"xdg"),
    "AGENT_DISPATCH_JOBS":str(base/"state"/"jobs.log"),
    "DISPATCH_DEFAULTS_CONFIG":str(bundle/"profiles"/"dispatch-defaults.yaml"),
   })
   common=["route",
    "--capability","autopilot-code","--capability-mode","debug",
    "--slug","Runtime Preflight Route",
    "--intensity","direct","--cwd",str(workspace),
    "--tracking","tracked","--spec-read","true",
    "--drift-verdict","within-spec","--workflow-mode","tracked",
    "--artifact-guard","true","--inline-reason","atomic-direct",
   ]
   for predicate in ALL: common.extend(["--predicate",predicate])
   for adapter in ("codex","opencode"):
    preflight=bundle/"adapters"/adapter/"bin"/"preflight.sh"
    for label,agent_home in (("default",None),("explicit",str(bundle))):
     artifact_root=base/f"artifacts-{adapter}-{label}"
     run_env=env.copy()
     if agent_home is not None: run_env["AGENT_HOME"]=agent_home
     result=subprocess.run(
      [str(preflight),*common,"--artifact-root",str(artifact_root)],
      capture_output=True,text=True,cwd=str(workspace),env=run_env,
     )
     self.assertEqual(result.returncode,0,result.stderr)
     route=json.loads(result.stdout)
     self.assertTrue((R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json").is_file())
     self.assertEqual(route["validation_basis"]["runtime_root"],str(bundle.resolve()))
     self.assertTrue(route["validation_basis"]["runtime_root_match"])
 def test_fresh_compile_seals_validation_basis(self):
  route=R.compile_route(**self.args())
  vb=route["validation_basis"]
  self.assertEqual(vb["basis_version"],1)
  self.assertEqual(vb["registry_root"],str(R.TOPO.ROOT))
  self.assertEqual(vb["unit_catalog_root"],str(R.ROOT))
  self.assertTrue(Path(vb["runtime_root"]).is_absolute())
  self.assertIsInstance(vb["runtime_root_validated"],bool)
  self.assertIsInstance(vb["runtime_root_match"],bool)
  R.verify_route(route,R.ROOT)
 def test_fresh_compile_seals_and_revalidates_launch_tuple(self):
  route=R.compile_route(**self.args())
  launch=route["launch_compatibility_tuple"]
  self.assertEqual(launch["contract_version"],1)
  self.assertEqual(launch["tuple_version"],1)
  self.assertEqual(set(launch),{
   "contract_version","tuple_version","registry_root","launch_home",
   "runtime_root","grounding_roots","wrapper_root","jobs_path",
  })
  for identity in [
   launch["registry_root"],launch["launch_home"],launch["runtime_root"],
   launch["grounding_roots"]["cwd"],launch["grounding_roots"]["artifact_root"],
   launch["wrapper_root"],launch["jobs_path"],
  ]:
   self.assertEqual(set(identity),{
    "kind","path","release_id","content_digest","binding_digest",
   })
   self.assertTrue(identity["content_digest"].startswith("sha256:"))
   self.assertTrue(identity["binding_digest"].startswith("sha256:"))
  self.assertEqual(route["route_hash"],R.route_hash(route))
  R.verify_route(route,R.ROOT)
  self.assertEqual(R.revalidate_launch_compatibility(route),(True,{}))
 def test_plain_verify_is_unchanged_and_launch_phase_rejects_tamper(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   # Fixed grounding roots: compiling against the live checkout let a
   # concurrent suite's working-tree mutation drift grounding_roots.cwd
   # between compile and verify, so the launch-phase mismatch surfaced as
   # grounding_roots.cwd instead of the tampered runtime_root.
   fixed_cwd=Path(tmp)/"cwd"; fixed_root=Path(tmp)/"artifacts"
   fixed_cwd.mkdir(); fixed_root.mkdir()
   route=R.compile_route(**self.args(cwd=fixed_cwd,artifact_root=fixed_root))
   tampered=json.loads(json.dumps(route))
   tampered["launch_compatibility_tuple"]["runtime_root"]["release_id"]="release:tampered"
   tampered=self._reseal(tampered)
   route_path=Path(tmp)/"route.json"
   route_path.write_text(json.dumps(tampered),encoding="utf-8")
   env=os.environ.copy(); env["AGENT_HOME"]=self._tmp_home.name
   env.pop("AGENT_DISPATCH_JOBS",None)
   plain=subprocess.run(
    [sys.executable,str(P),"verify","--route",str(route_path),"--cwd",str(fixed_cwd)],
    capture_output=True,text=True,cwd=str(R.ROOT),env=env,
   )
   self.assertEqual(plain.returncode,0,plain.stderr)
   self.assertEqual(
    plain.stdout,
    f"route_id={tampered['route_id']}\nroute_hash={tampered['route_hash']}\n",
   )
   self.assertEqual(plain.stderr,"")
   launch=subprocess.run(
    [sys.executable,str(P),"verify","--route",str(route_path),"--cwd",str(fixed_cwd),
     "--launch-phase","start"],
    capture_output=True,text=True,cwd=str(R.ROOT),env=env,
   )
   self.assertEqual(launch.returncode,64,launch.stderr)
   self.assertIn("launch-runtime-root-mismatch phase=start mismatch=runtime_root",launch.stderr)
   self.assertIn("registered=0 started=0 child_spawned=0",launch.stderr)
 def test_malformed_runtime_root_keeps_typed_launch_refusal(self):
  roots=(7,[],None,{"path":7},{"path":[]},{"path":"relative/root"})
  with tempfile.TemporaryDirectory() as tmp:
   fixed_cwd=Path(tmp)/"cwd"; fixed_root=Path(tmp)/"artifacts"
   fixed_cwd.mkdir(); fixed_root.mkdir()
   original=R.compile_route(**self.args(cwd=fixed_cwd,artifact_root=fixed_root))
   env=os.environ.copy(); env["AGENT_HOME"]=self._tmp_home.name
   env.pop("AGENT_DISPATCH_JOBS",None)
   expected=str(Path(env.get("XDG_DATA_HOME",str(Path.home()/".local/share")))/"hearting/current")
   for runtime in roots:
    with self.subTest(runtime=runtime):
     route=json.loads(json.dumps(original))
     route["launch_compatibility_tuple"]["runtime_root"]=runtime
     route=self._reseal(route)
     R.verify_route(route,fixed_cwd)
     compatible,mismatches=R.revalidate_launch_compatibility(route)
     self.assertFalse(compatible); self.assertIn("runtime_root",mismatches)
     route_path=Path(tmp)/"route.json"; route_path.write_text(json.dumps(route))
     result=subprocess.run(
      [sys.executable,str(P),"verify","--route",str(route_path),"--cwd",str(fixed_cwd),
       "--launch-phase","start"],capture_output=True,text=True,cwd=str(R.ROOT),env=env,
     )
     self.assertEqual(result.returncode,64,result.stderr)
     self.assertIn("launch-runtime-root-mismatch",result.stderr)
     self.assertIn("registered=0 started=0 child_spawned=0",result.stderr)
     self.assertIn(expected,result.stderr)
     self.assertNotIn("Traceback",result.stderr)
 def test_runtime_root_hint_is_total_for_json_shapes(self):
  for value in (None,7,True,"scalar",[],{}, {"path":7},{"path":[]},{"path":"relative"}):
   with self.subTest(value=value):
    for route in (value,{"launch_compatibility_tuple":value},
                  {"launch_compatibility_tuple":{"runtime_root":value}}):
     hint=R.runtime_root_hint(route)
     self.assertIn("hearting/current",hint)
     self.assertIn("AGENT_HOME=",hint)
  hint=R.runtime_root_hint({"launch_compatibility_tuple":{"runtime_root":{"path":"/sealed root"}}})
  self.assertIn("AGENT_HOME='/sealed root'",hint)
 def test_legacy_tuple_absence_is_read_only_compatible(self):
  import subprocess,sys
  route=R.compile_route(**self.args())
  legacy=json.loads(json.dumps(route)); legacy.pop("launch_compatibility_tuple")
  legacy=self._reseal(legacy)
  self.assertEqual(
   R.revalidate_launch_compatibility(legacy),(True,{"tuple":"absent-legacy"}),
  )
  with tempfile.TemporaryDirectory() as tmp:
   route_path=Path(tmp)/"legacy.json"
   route_path.write_text(json.dumps(legacy),encoding="utf-8")
   env=os.environ.copy(); env["AGENT_HOME"]=self._tmp_home.name
   env.pop("AGENT_DISPATCH_JOBS",None)
   plain=subprocess.run(
    [sys.executable,str(P),"verify","--route",str(route_path)],
    capture_output=True,text=True,cwd=str(R.ROOT),env=env,
   )
   self.assertEqual(plain.returncode,0,plain.stderr)
   launch=subprocess.run(
    [sys.executable,str(P),"verify","--route",str(route_path),"--launch-phase","dry-run"],
    capture_output=True,text=True,cwd=str(R.ROOT),env=env,
   )
   self.assertEqual(launch.returncode,64,launch.stderr)
   self.assertIn("launch-compatibility-tuple-required",launch.stderr)
   self.assertIn("registered=0 started=0 child_spawned=0",launch.stderr)
 def test_close_still_accepts_launch_tuple_mismatch(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)/"artifacts"
   route=R.compile_route(**self.args(artifact_root=artifact_root))
   route["launch_compatibility_tuple"]["runtime_root"]["content_digest"]="sha256:"+"f"*64
   route=self._reseal(route)
   route_path=R.canonical_route_path(artifact_root,route["route_id"])
   R.write_once(route_path,route)
   env=os.environ.copy(); env["AGENT_HOME"]=self._tmp_home.name
   env.pop("AGENT_DISPATCH_JOBS",None)
   result=subprocess.run(
    [sys.executable,str(P),"close","--route",str(route_path),"--commit","d"*40,"--allow-unproven"],
    capture_output=True,text=True,cwd=str(R.ROOT),env=env,
   )
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertTrue(R.outcome_path(route_path).is_file())
 def test_relative_runtime_root_candidate_seals_an_absolute_path(self):
  previous=os.environ.get("AGENT_HOME")
  os.environ["AGENT_HOME"]=os.path.relpath(self._tmp_home.name,os.getcwd())
  try:
   route=R.compile_route(**self.args())
  finally:
   if previous is None: os.environ.pop("AGENT_HOME",None)
   else: os.environ["AGENT_HOME"]=previous
  vb=route["validation_basis"]
  self.assertTrue(Path(vb["runtime_root"]).is_absolute())
  self.assertEqual(Path(vb["runtime_root"]),Path(self._tmp_home.name).resolve())
  R.verify_route(route,R.ROOT)
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp); route=dict(route); route["artifact_root"]=str(artifact_root)
   path=artifact_root/"demo-route.json"; path.write_text(json.dumps(route),encoding="utf-8")
   outcome,created=R.close_route(route,path,commit="b"*40,allow_unproven=True)
   self.assertTrue(created)
 def test_same_root_digest_change_still_reads_as_stale(self):
  route=R.compile_route(**self.args())
  stale=self._reseal({**json.loads(json.dumps(route)),"registry_digest":"sha256:"+"0"*64})
  with self.assertRaisesRegex(ValueError,"stale registry digest"):
   R.verify_route(stale,R.ROOT)
  stale=self._reseal({**json.loads(json.dumps(route)),"unit_catalog_digest":"sha256:"+"0"*64})
  with self.assertRaisesRegex(ValueError,"stale unit catalog digest"):
   R.verify_route(stale,R.ROOT)
 def test_legacy_route_without_basis_keeps_stale_wording(self):
  route=R.compile_route(**self.args())
  legacy=json.loads(json.dumps(route)); legacy.pop("validation_basis")
  legacy["registry_digest"]="sha256:"+"0"*64
  legacy=self._reseal(legacy)
  with self.assertRaisesRegex(ValueError,"stale registry digest"):
   R.verify_route(legacy,R.ROOT)
  unmutated=json.loads(json.dumps(route)); unmutated.pop("validation_basis")
  unmutated=self._reseal(unmutated)
  R.verify_route(unmutated,R.ROOT)
 def test_cross_root_digest_mismatch_reports_typed_skew(self):
  route=R.compile_route(**self.args())
  skewed=json.loads(json.dumps(route))
  skewed["validation_basis"]["registry_root"]="/tmp/b2-fixture-other-registry-root"
  skewed["registry_digest"]="sha256:"+"1"*64
  skewed=self._reseal(skewed)
  with self.assertRaisesRegex(ValueError,r"^registry-digest-skew\(compiled="):
   R.verify_route(skewed,R.ROOT)
  try:
   R.verify_route(skewed,R.ROOT)
  except ValueError as exc:
   msg=str(exc)
   self.assertIn(skewed["registry_digest"],msg)
   self.assertIn(R.TOPO.registry_digest(R.TOPO.load_registry()),msg)
   # SD-OPEN-47 (H7-d): the operator is told which root to re-run through.
   self.assertIn("re-run via the tooling under /tmp/b2-fixture-other-registry-root",msg)
   self.assertIn("/tmp/b2-fixture-other-registry-root",msg)
   self.assertIn(str(R.TOPO.ROOT),msg)
  skewed=json.loads(json.dumps(route))
  skewed["validation_basis"]["unit_catalog_root"]="/tmp/b2-fixture-other-unit-root"
  skewed["unit_catalog_digest"]="sha256:"+"1"*64
  skewed=self._reseal(skewed)
  with self.assertRaisesRegex(ValueError,r"^unit-catalog-digest-skew\(compiled="):
   R.verify_route(skewed,R.ROOT)
 def test_validation_classifier_does_not_read_immutable_release_content(self):
  route={
   "registry_digest":"sha256:"+"1"*64,
   "unit_catalog_digest":"sha256:"+"2"*64,
   "validation_basis":{
    "registry_root":"/tmp/compiled-registry",
    "unit_catalog_root":"/tmp/current-units",
   },
  }
  with mock.patch.object(
   R,"immutable_code_root_equivalent",
   side_effect=AssertionError("pure classifier touched release content"),
  ):
   result=R.classify_validation_basis(
    route,registry_digest_now="sha256:"+"3"*64,
    units_digest_now=route["unit_catalog_digest"],
    registry_root_now="/tmp/current-registry",
    unit_catalog_root_now="/tmp/current-units",
   )
  self.assertEqual(result["verdict"],"skew")
 def test_cross_root_equal_digest_passes(self):
  route=R.compile_route(**self.args())
  moved=json.loads(json.dumps(route))
  moved["validation_basis"]["registry_root"]="/tmp/b2-fixture-other-registry-root"
  moved["validation_basis"]["unit_catalog_root"]="/tmp/b2-fixture-other-unit-root"
  moved=self._reseal(moved)
  R.verify_route(moved,R.ROOT)
 def test_registry_axis_precedes_unit_catalog_axis(self):
  route=R.compile_route(**self.args())
  both=json.loads(json.dumps(route))
  both["registry_digest"]="sha256:"+"0"*64
  both["validation_basis"]["unit_catalog_root"]="/tmp/b2-fixture-other-unit-root"
  both["unit_catalog_digest"]="sha256:"+"1"*64
  both=self._reseal(both)
  with self.assertRaisesRegex(ValueError,r"^stale registry digest$"):
   R.verify_route(both,R.ROOT)
 def test_malformed_validation_basis_fails_closed(self):
  route=R.compile_route(**self.args())
  def check(mutate,token):
   for allow in (False,True):
    broken=json.loads(json.dumps(route)); mutate(broken)
    broken=self._reseal(broken)
    with self.assertRaisesRegex(ValueError,re.escape(token)):
     R.verify_route(broken,R.ROOT,allow_stale_registry=allow)
  check(lambda r: r.__setitem__("validation_basis","not-a-dict"),"invalid-validation-basis(field=validation_basis)")
  check(lambda r: r["validation_basis"].pop("basis_version"),"invalid-validation-basis(field=basis_version)")
  check(lambda r: r["validation_basis"].__setitem__("basis_version","1"),"invalid-validation-basis(field=basis_version)")
  check(lambda r: r["validation_basis"].__setitem__("basis_version",0),"invalid-validation-basis(field=basis_version)")
  for field in ("registry_root","unit_catalog_root","runtime_root"):
   check(lambda r,field=field: r["validation_basis"].pop(field),f"invalid-validation-basis(field={field})")
   check(lambda r,field=field: r["validation_basis"].__setitem__(field,""),f"invalid-validation-basis(field={field})")
   check(lambda r,field=field: r["validation_basis"].__setitem__(field,"relative/path"),f"invalid-validation-basis(field={field})")
   check(lambda r,field=field: r["validation_basis"].__setitem__(field,7),f"invalid-validation-basis(field={field})")
  for field in ("runtime_root_validated","runtime_root_match"):
   check(lambda r,field=field: r["validation_basis"].__setitem__(field,"yes"),f"invalid-validation-basis(field={field})")
 def test_explicit_null_and_boolean_version_fail_closed(self):
  route=R.compile_route(**self.args())
  def check(mutate,token):
   for allow in (False,True):
    broken=json.loads(json.dumps(route)); mutate(broken)
    broken=self._reseal(broken)
    with self.assertRaisesRegex(ValueError,re.escape(token)):
     R.verify_route(broken,R.ROOT,allow_stale_registry=allow)
  check(lambda r: r.__setitem__("validation_basis",None),"invalid-validation-basis(field=validation_basis)")
  check(lambda r: r["validation_basis"].__setitem__("basis_version",True),"invalid-validation-basis(field=basis_version)")
 def test_malformed_basis_is_an_intentional_close_blocker(self):
  # A structurally malformed basis has no legitimate producer -- route_hash is
  # checked first, so reaching this branch means a hand-edited, resealed record.
  # It is therefore an intentional close blocker, not a bug to relax later.
  route=R.compile_route(**self.args())
  broken=json.loads(json.dumps(route)); broken["validation_basis"]["registry_root"]=""
  broken=self._reseal(broken)
  with self.assertRaisesRegex(ValueError,r"^invalid-validation-basis\(field=registry_root\)$"):
   R.verify_route(broken,R.ROOT,allow_stale_registry=True)
 def test_unsupported_basis_version_raises_for_launch(self):
  route=R.compile_route(**self.args())
  newer=json.loads(json.dumps(route))
  newer["validation_basis"]["basis_version"]=R.VALIDATION_BASIS_VERSION+1
  newer=self._reseal(newer)
  with self.assertRaisesRegex(ValueError,r"^unsupported-validation-basis-version\(basis_version=2\)$"):
   R.verify_route(newer,R.ROOT,allow_stale_registry=False)
 def test_unsupported_basis_version_degrades_at_close(self):
  route=R.compile_route(**self.args())
  newer=json.loads(json.dumps(route))
  newer["validation_basis"]["basis_version"]=R.VALIDATION_BASIS_VERSION+1
  newer=self._reseal(newer)
  verified=R.verify_route(newer,R.ROOT,allow_stale_registry=True)
  self.assertIs(verified["_registry_current"],False)
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp); verified=dict(verified); verified["artifact_root"]=str(artifact_root)
   path=artifact_root/"demo-route.json"; path.write_text(json.dumps(verified),encoding="utf-8")
   outcome,created=R.close_route(verified,path,commit="c"*40,allow_unproven=True)
   self.assertTrue(created)
   self.assertIs(outcome["registry_current"],False)
 def test_unknown_basis_keys_are_tolerated(self):
  route=R.compile_route(**self.args())
  extended=json.loads(json.dumps(route))
  extended["validation_basis"]["future_field"]="x"
  extended=self._reseal(extended)
  R.verify_route(extended,R.ROOT)
 def test_skew_message_fits_the_batch_clip(self):
  route=R.compile_route(**self.args())
  registry_skew=json.loads(json.dumps(route))
  registry_skew["validation_basis"]["registry_root"]="/tmp/b2-fixture-other-registry-root"
  registry_skew["registry_digest"]="sha256:"+"1"*64
  registry_skew=self._reseal(registry_skew)
  unit_skew=json.loads(json.dumps(route))
  unit_skew["validation_basis"]["unit_catalog_root"]="/tmp/b2-fixture-other-unit-root"
  unit_skew["unit_catalog_digest"]="sha256:"+"1"*64
  unit_skew=self._reseal(unit_skew)
  for candidate,token in ((registry_skew,"registry-digest-skew"),(unit_skew,"unit-catalog-digest-skew")):
   try:
    R.verify_route(candidate,R.ROOT)
    self.fail("expected ValueError")
   except ValueError as exc:
    msg=str(exc)
    self.assertTrue(msg.startswith(token))
    self.assertLessEqual(len(f"capability-route: {msg}"[:512]),512)
    self.assertLessEqual(len(f"capability-route: {msg}"),512)
 def test_skewed_route_can_still_be_closed(self):
  route=R.compile_route(**self.args())
  skewed=json.loads(json.dumps(route))
  skewed["validation_basis"]["registry_root"]="/tmp/b2-fixture-other-registry-root"
  skewed["registry_digest"]="sha256:"+"1"*64
  skewed=self._reseal(skewed)
  verified=R.verify_route(skewed,R.ROOT,allow_stale_registry=True)
  self.assertIs(verified["_registry_current"],False)
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp); verified=dict(verified); verified["artifact_root"]=str(artifact_root)
   path=artifact_root/"demo-route.json"; path.write_text(json.dumps(verified),encoding="utf-8")
   outcome,created=R.close_route(verified,path,commit="d"*40,allow_unproven=True)
   self.assertTrue(created)

class GroundingCwdLineageTest(unittest.TestCase):
 """SD-107 × SD-67/69: the mutation worktree's HEAD may move along its first-parent line."""
 def _repo(self,tmp):
  import subprocess
  root=Path(tmp)/"wt"; root.mkdir()
  def git(*a): subprocess.run(["git","-C",str(root),*a],check=True,capture_output=True,text=True)
  git("init","-q"); git("config","user.email","t@t"); git("config","user.name","t")
  (root/"a").write_text("1"); git("add","."); git("commit","-q","-m","a")
  base=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
  return root,git,base
 def test_same_head_with_dirty_suffix_is_accepted(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,_,base=self._repo(tmp)
   self.assertTrue(R._grounding_cwd_lineage_ok(root,base,base+"+dirty:abc"))
 def test_first_parent_descendant_is_accepted(self):
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   root,git,base=self._repo(tmp)
   (root/"b").write_text("2"); git("add","."); git("commit","-q","-m","b")
   head=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
   self.assertTrue(R._grounding_cwd_lineage_ok(root,base,head))
   self.assertFalse(R._grounding_cwd_lineage_ok(root,head,base))
 def test_foreign_or_non_git_revision_stays_mismatch(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,_,base=self._repo(tmp)
   self.assertFalse(R._grounding_cwd_lineage_ok(root,base,"f"*40))
   self.assertFalse(R._grounding_cwd_lineage_ok(root,"tree:abc",base))
   self.assertFalse(R._grounding_cwd_lineage_ok(root,base,"release:v1:abc"))
 def test_revalidate_accepts_cwd_drift_only_with_lineage(self):
  route={"artifact_root":str(R.ROOT),"cwd":str(R.ROOT)}
  route["launch_compatibility_tuple"]={"contract_version":1,**R.launch_compatibility_tuple(artifact_root=R.ROOT,cwd=R.ROOT)}
  ok,_=R.revalidate_launch_compatibility(route); self.assertTrue(ok)
  drift=json.loads(json.dumps(route))
  drift["launch_compatibility_tuple"]["grounding_roots"]["cwd"]["release_id"]="f"*40
  ok,mismatches=R.revalidate_launch_compatibility(drift)
  self.assertFalse(ok); self.assertIn("grounding_roots.cwd",mismatches)


class ContinuationSealedJobsFallbackTest(unittest.TestCase):
 """A pruned release tree must not strand a continuation on its sealed jobs root."""
 def test_missing_sealed_root_falls_back_to_canonical(self):
  # SD-112 §13.33.2-(8): the env-less canonical answer is the stable state
  # root (`.../hearting/dispatch/jobs.log`), no longer a release-relative
  # `.dispatch/jobs.log`. Pin the resolution chain so this asserts the
  # resolver's canonical answer rather than whichever root the ambient
  # environment happens to name.
  with tempfile.TemporaryDirectory() as tmp:
   home=Path(tmp)/"home"; home.mkdir()
   prior={
    key:os.environ.get(key)
    for key in (
     "HOME","XDG_STATE_HOME","HARNESS_STATE_ROOT","AGENT_HOME",
     "AGENT_DISPATCH_JOBS",
    )
   }
   try:
    for key in (
     "XDG_STATE_HOME","HARNESS_STATE_ROOT","AGENT_HOME","AGENT_DISPATCH_JOBS",
    ):
     os.environ.pop(key,None)
    os.environ["HOME"]=str(home)
    route={"launch_compatibility_tuple":{"jobs_path":{"path":"/nonexistent-release/.dispatch/jobs.log"}}}
    resolved=R._continuation_source_jobs(route)
    self.assertEqual(
     resolved,home/".local"/"state"/"hearting"/"dispatch"/"jobs.log")
    self.assertNotIn("nonexistent-release",str(resolved))
   finally:
    for key,value in prior.items():
     if value is None: os.environ.pop(key,None)
     else: os.environ[key]=value
 def test_existing_sealed_root_is_preserved(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=Path(tmp)/".dispatch"; d.mkdir()
   route={"launch_compatibility_tuple":{"jobs_path":{"path":str(d/"jobs.log")}}}
   self.assertEqual(R._continuation_source_jobs(route),d/"jobs.log")
 def test_unresolved_binding_still_fails_closed(self):
  with self.assertRaises(ValueError):
   R._continuation_source_jobs({"launch_compatibility_tuple":{"jobs_path":{"path":"relative/jobs.log"}}})


class MigrationAliasContinuationTest(unittest.TestCase):
 """SD-112 §13.33.2-(3)/(6) decision 1/4: a completed, structurally-valid
 migration-alias record relieves a `jobs_path`-only mismatch -- B-3 (pruned
 release continuation), B-4 (sealed-tuple alias/forgery), B-5 (pre-start
 route, no completion marker ever touched by any fixture here)."""
 def setUp(self):
  self._tmp=tempfile.TemporaryDirectory()
  self._home=Path(self._tmp.name)/"home"; self._home.mkdir()
  self._stable_jobs=self._home/".local"/"state"/"hearting"/"dispatch"/"jobs.log"
  self._stable_jobs.parent.mkdir(parents=True)
  self._journal=self._stable_jobs.parent/"migration-journal.jsonl"
  # After SD-112 the compat shim resolves to this fixture's OWN stable root,
  # so the shim target and `self._stable_jobs` coincide. Point the alias
  # record at a second, distinct fixture-owned stable jobs.log -- the shape a
  # record written against a different installer-owned state root has -- so
  # the precedence assertions below stay real proofs instead of coincidences.
  self._alias_jobs=(
   Path(self._tmp.name)/"migrated-state"/"hearting"/"dispatch"/"jobs.log")
  self._alias_jobs.parent.mkdir(parents=True)
  # `resolve_dangling_registry` requires the alias target to be a live file,
  # not merely a live parent directory -- an alias that names a registry
  # which is not there resolves nothing.
  self._alias_jobs.write_text("",encoding="utf-8")
  # The legacy (pruned-release) directory is deliberately never created --
  # a live directory here would defeat the "dangling" fixture shape.
  self._legacy_jobs=Path(self._tmp.name)/"pruned-release"/".dispatch"/"jobs.log"
  # The alias journal is looked up through `stable_state_root(os.environ)`,
  # which reads HARNESS_STATE_ROOT -> XDG_STATE_HOME -> HOME. Pinning HOME
  # alone leaves an ambient XDG_STATE_HOME pointing the lookup at a journal
  # this fixture never wrote, so every positive-alias assertion would fail
  # for an environment reason. Pin the whole chain, per the isolation
  # pattern in `dispatch_state_root_rotation.test.py`.
  self._prev_env={
   key:os.environ.get(key)
   for key in ("HOME","XDG_STATE_HOME","HARNESS_STATE_ROOT","AGENT_DISPATCH_JOBS")
  }
  os.environ.pop("XDG_STATE_HOME",None)
  os.environ.pop("HARNESS_STATE_ROOT",None)
  os.environ["HOME"]=str(self._home)
  self.addCleanup(self._restore)
 def _restore(self):
  for key,value in self._prev_env.items():
   if value is None: os.environ.pop(key,None)
   else: os.environ[key]=value
  self._tmp.cleanup()
 def _write_journal(self,record):
  with self._journal.open("a",encoding="utf-8") as fh:
   fh.write(json.dumps(record)+"\n")
 def _completed_record(self,**overrides):
  record={
   "record_version":1,"migration_id":"mig-fixture-1","status":"completed",
   "legacy_jobs_identity":{
    "path":str(self._legacy_jobs.resolve()),"content_digest":"sha256:"+"a"*64,
   },
   "stable_jobs_identity":{
    "path":str(self._alias_jobs.resolve()),"content_digest":"sha256:"+"b"*64,
   },
   "source_digest":"sha256:"+"c"*64,"target_digest":"sha256:"+"d"*64,
  }
  record.update(overrides)
  return record

 # --- B-3: continuation resolves a pruned source via the completed alias,
 # checked before -- and independent of the coincidence of -- the compat shim.
 def test_continuation_resolves_pruned_source_via_completed_alias(self):
  self._write_journal(self._completed_record())
  route={"launch_compatibility_tuple":{"jobs_path":{"path":str(self._legacy_jobs)}}}
  resolved=R._continuation_source_jobs(route)
  self.assertEqual(resolved,self._alias_jobs.resolve())
  shim_target=R.resolve_dispatch_state_root(R.resolve_agent_home(),None)/"jobs.log"
  self.assertNotEqual(shim_target,resolved,
   "fixture must prove alias precedence, not shim coincidence")
 def test_continuation_falls_back_to_shim_when_no_alias(self):
  route={"launch_compatibility_tuple":{"jobs_path":{"path":str(self._legacy_jobs)}}}
  resolved=R._continuation_source_jobs(route)
  self.assertEqual(
   resolved,R.resolve_dispatch_state_root(R.resolve_agent_home(),None)/"jobs.log")
 def test_continuation_ignores_alias_whose_target_file_is_absent(self):
  # The record is completely well formed and its target directory exists;
  # only the registry file is missing. A stale or forged record naming any
  # live directory must not resurrect a registry that is not there.
  self._alias_jobs.unlink()
  self._write_journal(self._completed_record())
  route={"launch_compatibility_tuple":{"jobs_path":{"path":str(self._legacy_jobs)}}}
  resolved=R._continuation_source_jobs(route)
  self.assertNotEqual(resolved,self._alias_jobs.resolve())
  self.assertEqual(
   resolved,R.resolve_dispatch_state_root(R.resolve_agent_home(),None)/"jobs.log")
 def test_continuation_ignores_malformed_digest_alias(self):
  # `completed` plus a filled-in field is not a digest check.
  self._write_journal(self._completed_record(
   source_digest="not-a-digest",migration_id="mig-fixture-bad-digest"))
  route={"launch_compatibility_tuple":{"jobs_path":{"path":str(self._legacy_jobs)}}}
  resolved=R._continuation_source_jobs(route)
  self.assertNotEqual(resolved,self._alias_jobs.resolve())
 def test_continuation_ignores_incomplete_or_forged_alias(self):
  self._write_journal(self._completed_record(status="open"))
  self._write_journal(self._completed_record(
   migration_id="mig-fixture-2",source_digest=None))
  route={"launch_compatibility_tuple":{"jobs_path":{"path":str(self._legacy_jobs)}}}
  resolved=R._continuation_source_jobs(route)
  self.assertNotEqual(resolved,self._alias_jobs.resolve())
  self.assertEqual(
   resolved,R.resolve_dispatch_state_root(R.resolve_agent_home(),None)/"jobs.log")

 # --- B-4/B-5: revalidate_launch_compatibility jobs_path-only alias relief.
 def _route_with_sealed_jobs_path(self,legacy_path):
  sealed=json.loads(json.dumps(
   R.launch_compatibility_tuple(artifact_root=R.ROOT,cwd=R.ROOT)))
  jobs_path=dict(sealed["jobs_path"])
  jobs_path["path"]=str(legacy_path)
  jobs_path["binding_digest"]=R._sha256_record({
   "kind":"jobs_path","path":str(legacy_path),
   "release_id":jobs_path["release_id"],"content_digest":jobs_path["content_digest"],
  })
  sealed["jobs_path"]=jobs_path
  return {
   "artifact_root":str(R.ROOT),"cwd":str(R.ROOT),
   "route_hash":"sha256:"+"e"*64,
   "launch_compatibility_tuple":{"contract_version":1,**sealed},
  }
 def test_revalidate_accepts_jobs_path_only_via_completed_alias(self):
  # B-5: pre-start route -- no completion marker file exists anywhere in
  # this fixture, and revalidate/alias never look for one.
  os.environ["AGENT_DISPATCH_JOBS"]=str(self._stable_jobs)
  actual=R.launch_compatibility_tuple(artifact_root=R.ROOT,cwd=R.ROOT)
  self._write_journal(self._completed_record(
   stable_jobs_identity={
    "path":actual["jobs_path"]["path"],"content_digest":"sha256:"+"b"*64,
   },
  ))
  route=self._route_with_sealed_jobs_path(self._legacy_jobs)
  ok,mismatches=R.revalidate_launch_compatibility(route)
  self.assertTrue(ok,mismatches)
  self.assertNotIn("jobs_path",mismatches)
 def test_revalidate_rejects_incomplete_or_forged_alias(self):
  os.environ["AGENT_DISPATCH_JOBS"]=str(self._stable_jobs)
  actual=R.launch_compatibility_tuple(artifact_root=R.ROOT,cwd=R.ROOT)
  self._write_journal(self._completed_record(
   status="open",
   stable_jobs_identity={
    "path":actual["jobs_path"]["path"],"content_digest":"sha256:"+"b"*64,
   },
  ))
  route=self._route_with_sealed_jobs_path(self._legacy_jobs)
  ok,mismatches=R.revalidate_launch_compatibility(route)
  self.assertFalse(ok)
  self.assertIn("jobs_path",mismatches)
 def test_revalidate_rejects_completed_alias_with_wrong_route_hash(self):
  os.environ["AGENT_DISPATCH_JOBS"]=str(self._stable_jobs)
  actual=R.launch_compatibility_tuple(artifact_root=R.ROOT,cwd=R.ROOT)
  self._write_journal(self._completed_record(
   stable_jobs_identity={
    "path":actual["jobs_path"]["path"],"content_digest":"sha256:"+"b"*64,
   },
   route_hash="sha256:"+"f"*64,
  ))
  route=self._route_with_sealed_jobs_path(self._legacy_jobs)
  ok,mismatches=R.revalidate_launch_compatibility(route)
  self.assertFalse(ok)
  self.assertIn("jobs_path",mismatches)
 def test_revalidate_accepts_completed_alias_with_matching_route_hash(self):
  os.environ["AGENT_DISPATCH_JOBS"]=str(self._stable_jobs)
  actual=R.launch_compatibility_tuple(artifact_root=R.ROOT,cwd=R.ROOT)
  route=self._route_with_sealed_jobs_path(self._legacy_jobs)
  self._write_journal(self._completed_record(
   stable_jobs_identity={
    "path":actual["jobs_path"]["path"],"content_digest":"sha256:"+"b"*64,
   },
   route_hash=route["route_hash"],
  ))
  ok,mismatches=R.revalidate_launch_compatibility(route)
  self.assertTrue(ok,mismatches)


class ContinuationBudgetSealedBlockTest(unittest.TestCase):
 """SD-116 WP4: `compile_route()` seals a `continuation_budget` block into
 the payload before `route_hash` is computed."""
 setUp=TestRoute.setUp
 _restore_agent_home=TestRoute._restore_agent_home
 dispatch=TestRoute.dispatch
 nested=TestRoute.nested
 args=TestRoute.args

 def test_compiled_route_carries_a_well_formed_continuation_budget_block(self):
  route=R.compile_route(**self.args())
  block=route["continuation_budget"]
  self.assertEqual(1,block["contract_version"])
  self.assertEqual(len(route["nodes"]),block["declared_nodes"])
  self.assertEqual(1,block["gap"])
  self.assertEqual(1,block["retry"])
  self.assertGreaterEqual(block["reserved"],1)
  self.assertEqual(block["limit"],block["ordinary"]+block["reserved"])
  self.assertGreaterEqual(block["ordinary"],12)
  self.assertIsInstance(block["review_round_cap"],int)
  self.assertGreaterEqual(block["review_round_cap"],1)

 def test_block_is_sealed_into_route_hash(self):
  route=R.compile_route(**self.args())
  tampered=json.loads(json.dumps(route))
  tampered["continuation_budget"]["ordinary"]+=1000
  self.assertNotEqual(R.route_hash(tampered),route["route_hash"])

 def test_sealed_route_resolves_as_sealed_block_with_the_compiled_values(self):
  import dispatch_continuation_budget as BUDGET
  route=R.compile_route(**self.args(requested_intensity="strong",predicates=[],
   signals=["shared-contract"],transport="headless",inline_reason=None,
   dispatch_evidence=self.dispatch(self.nested())))
  with tempfile.TemporaryDirectory() as raw:
   route_file=Path(raw)/"route.json"
   route_file.write_text(json.dumps(route),encoding="utf-8")
   budget=BUDGET.resolve_continuation_budget(
    route_file=route_file,route_id=route["route_id"],route_hash=route["route_hash"],
   )
  self.assertEqual("sealed-block",budget.source)
  self.assertEqual(route["continuation_budget"]["ordinary"],budget.ordinary)
  self.assertEqual(route["continuation_budget"]["limit"],budget.limit)


class FrameSummaryContractTest(unittest.TestCase):
 """A50-9 / N3 / R2-3: shards/frame/frame-summary.json contract, documented in
 skills/autopilot-code/references/owner-execution.md and
 capabilities/autopilot-code.md. `utilities/dispatch_completion_join.py`
 (the module owning the actual v2/v3 receipt body -- StageAdvanceReceiptNegotiationTest,
 dispatch_completion_join.test.py:1908-2032) is outside this slice's fixed-file
 fence, so this pins the two independently-checkable halves of the contract:
 the summary's own shape, and a static guard that the receipt module's source
 never gains a literal frame-summary field name (a real regression signal
 for a future edit, without requiring this test to drive that module's
 stateful multi-argument API)."""

 FRAME_SUMMARY_FIELDS = ("방향", "대안", "위험", "범위 변경", "비용")

 def _summary(self, **overrides):
  summary = {
   "방향": "표준 실행 경로를 그대로 진행합니다.",
   "대안": "대안 A, 대안 B",
   "위험": "낮음 - 기존 계약 변경 없음",
   "범위 변경": "없음",
   "비용": "추가 비용 없음",
  }
  summary.update(overrides)
  return summary

 def test_frame_summary_has_exactly_the_five_declared_fields(self):
  summary = self._summary()
  self.assertEqual(set(summary), set(self.FRAME_SUMMARY_FIELDS))
  extra = self._summary(**{"required_action": "human-gate:frame-review"})
  self.assertNotEqual(set(extra), set(self.FRAME_SUMMARY_FIELDS))

 def test_frame_summary_serializes_at_or_under_one_kilobyte(self):
  encoded = json.dumps(self._summary(), ensure_ascii=False).encode("utf-8")
  self.assertLessEqual(len(encoded), 1024)

 def test_receipt_module_source_never_gains_a_literal_frame_summary_field(self):
  # Seam 3: a schema_version 2/3 receipt body is returned/extended by
  # identity; frame-summary.json is referenced by PATH from outside it.
  # This is a source-text guard, not a call into the module's stateful API
  # (out of this slice's fixed-file fence) -- it fails loudly if a future
  # edit to that module starts building any of these literal keys into a
  # receipt dict.
  join_module = Path(__file__).with_name("dispatch_completion_join.py")
  source = join_module.read_text(encoding="utf-8")
  for field in self.FRAME_SUMMARY_FIELDS:
   self.assertNotIn(field, source)
  self.assertNotIn("frame_summary", source)
  self.assertNotIn("frame-summary", source)


class InlineStageCompletionRecipeTest(unittest.TestCase):
 """The documented inline-completion recipe must keep working (defect H).

 Defect H looked like "the writer refuses an inline depth-2 stage". It is not:
 the refusal fires only when the caller supplies NO attempt metadata, and an
 inline owner can state the axes it actually had. Nobody invoked it that way
 because the recipe appeared in no document -- `rt-b2d68cbf14d31c62` has eight
 nodes and zero markers for that reason, not because `complete` said no.

 This pins both halves: the command works, and the document still shows it.
 """

 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name)
  self.jobs=self.base/"state"/"jobs.log"
  self.jobs.parent.mkdir(parents=True,exist_ok=True); self.jobs.touch()
  self.previous=os.environ.get("AGENT_DISPATCH_JOBS")
  os.environ["AGENT_DISPATCH_JOBS"]=str(self.jobs)
  self.addCleanup(self._restore)

 def _restore(self):
  if self.previous is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
  else: os.environ["AGENT_DISPATCH_JOBS"]=self.previous

 def _route(self,artifact_root):
  gate={"spec_read":{"satisfied":True,"source":"canonical-prd-sha256"},
        "drift_verdict":"within-spec","workflow_mode":"tracked",
        "artifact_guard":{"satisfied":True,"source":"conductor-prechecked"}}
  evidence={"tuples":[{
    "parent_harness":"codex","parent_transport":"headless",
    "parent_sandbox":R.WRAPPER_PARENT_SANDBOXES["codex"][0],
    "child_harness":"codex","launch_authority":"conductor","status":"supported",
    "probe_source":"inline-recipe","probe_time":"2026-09-06T00:00:00Z",
    "failure_class":"","checked_worktree":str(R.ROOT.resolve()),
    "failure_scope":"none","codex_command":"ok","retry_on_isolated_worktree":0,
   }],"native_subagent":[{
    "harness":"codex","transport":"headless",
    "execution_surface":"codex-native-subagent","registered_worker":False,
    "status":"supported","check_source":"inline-recipe"}]}
  return R.compile_route(
   "autopilot-code","dev","strong",R.ROOT,artifact_root,
   predicates=[],signals=["shared-contract"],transport="headless",
   tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=evidence)

 def _axes(self,depth=2):
  return {"attempt_schema_version":2,"dispatch_depth":depth,"transport":"headless",
          "execution_surface":"inline","registered_worker":False,
          "fallback_hop":"inline"}

 def test_the_documented_inline_recipe_publishes_a_current_marker(self):
  artifact=self.base/"artifacts"
  route=self._route(artifact)
  node=next(n for n in route["nodes"] if n["id"]=="execute")
  out=artifact/"evidence"/"execute.md"
  out.parent.mkdir(parents=True,exist_ok=True); out.write_text("ran inline\n",encoding="utf-8")
  marker,row=R.complete_node(route,node,"execute",out,
   attempt_id="att-inline-execute",explicit_attempt_metadata=self._axes())
  # No registry row is closed; the returned row is the typed
  # `unregistered-complete` receipt saying so.
  self.assertEqual(row.get("status"),"unregistered-complete")
  self.assertEqual(self.jobs.read_text(encoding="utf-8"),"",
                   "an inline completion must not write to the registry")
  path=R.completion_dir(route["route_id"],jobs=self.jobs)/"execute.json"
  self.assertTrue(path.is_file())
  self.assertTrue(D.completion_marker_is_current(route,node,path))
  self.assertEqual(marker["execution_surface"],"inline")
  self.assertIs(marker["registered_worker"],False)
  # `registered_worker=0` is what makes readiness answerable without a process.
  self.assertEqual(D.completion_attempt_readiness(route,node,marker,self.jobs).state,"ready")

 def test_inline_surface_at_depth_requires_the_inline_hop(self):
  # The combination is contract-checked, so a run cannot record a surface it
  # did not have. This is why the recipe names both flags.
  artifact=self.base/"artifacts"
  route=self._route(artifact)
  node=next(n for n in route["nodes"] if n["id"]=="execute")
  out=artifact/"evidence"/"execute.md"
  out.parent.mkdir(parents=True,exist_ok=True); out.write_text("x\n",encoding="utf-8")
  axes=dict(self._axes(),fallback_hop="same-harness-headless")
  with self.assertRaises((ValueError,D.DispatchContractError)):
   R.complete_node(route,node,"execute",out,attempt_id="att-bad",
                   explicit_attempt_metadata=axes)

 def test_the_recipe_is_actually_documented(self):
  # Defect H's real cause: the flags exist and appear in no document, so the
  # one published recipe (`--jobs --attempt-id`) is the only one an owner sees
  # -- and an inline run cannot satisfy it.
  for relative in (
   "skills/autopilot-code/references/dev-pipeline.md",
   "adapters/claude/skills/autopilot-code/references/dev-pipeline.md",
   "adapters/claude/plugin-marketplace/plugins/hearting-claude/skills/"
   "autopilot-code/references/dev-pipeline.md",
  ):
   text=(Path(R.ROOT)/relative).read_text(encoding="utf-8")
   with self.subTest(relative):
    # The recipe must be a runnable block, not a passing mention: find the
    # fenced command that completes without `--jobs` and check its flags.
    flags=("--execution-surface inline","--fallback-hop inline",
           "--registered-worker 0","--dispatch-depth")
    recipes=[block for block in text.split("```")
             if "capability-route.py complete" in block
             and "--jobs" not in block
             and all(flag in block for flag in flags)]
    self.assertEqual(
     len(recipes),1,
     "the inline completion recipe must appear exactly once, as a runnable "
     "block that states every axis and passes no --jobs",
    )



class ReviewIndependenceTest(InlineStageCompletionRecipeTest):
 """SD-OPEN-41(b): a review node's marker has to name who produced the verdict.

 Measured on main, 2026-09-06, over 2,911 production review markers: the axes
 already separate registered (2,899) from inline (12), so "indistinguishable"
 was too strong. What was actually missing is narrower and worse:

 * among the inline ones nothing told "the owner ruled on its own work" apart
   from "a native subagent reviewed it", and the user's rule counts the second
   as independent review;
 * nothing bound a *registered* completer to `worker_type=review`, so
   `registered_worker=true` was never proof a review worker produced the
   verdict; and
 * a route could close with a self-reviewed gate and say nothing about it.

 Every failure here downgrades and records; none of them refuse. SD-132 tried
 refusing and would have deadlocked production, because every review node seals
 `native-subagent` and `inline` as its last two fallback hops.
 """

 def _review_node(self,route,node_id="impl-review"):
  node=next(n for n in route["nodes"] if n["id"]==node_id)
  self.assertEqual(node["kind"],"review-worker")
  return node

 def _evidence(self,name="review.md",body="findings\n"):
  out=self.base/"artifacts"/"evidence"/name
  out.parent.mkdir(parents=True,exist_ok=True)
  out.write_text(body,encoding="utf-8")
  return out

 def _registered_row(self,attempt_id,route,node_id,worker_type,status="open"):
  """Append one contract-valid registered depth-2 row to the fixture registry."""
  meta=",".join([
   "attempt_schema_version=2","dispatch_depth=2","transport=headless",
   "execution_surface=registered-headless","registered_worker=1",
   "fallback_hop=same-harness-headless",
   f"attempt_id={attempt_id}",f"worker_type={worker_type}",
   f"route_id={route['route_id']}",f"route_hash={route['route_hash']}",
   f"route_node={node_id}",
  ])
  with self.jobs.open("a",encoding="utf-8") as handle:
   handle.write("\t".join(
    ["2026-09-06T00:00:00Z",status,"repo","worktree","slug",meta])+"\n")

 @staticmethod
 def _registered_axes():
  return {"attempt_schema_version":2,"dispatch_depth":2,"transport":"headless",
          "execution_surface":"registered-headless","registered_worker":"1",
          "fallback_hop":"same-harness-headless"}

 def _inline_axes(self):
  return self._axes()

 # -- the degraded default ------------------------------------------------

 def test_an_owner_reviewing_its_own_work_is_recorded_degraded(self):
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  marker,_=R.complete_node(
   route,node,"impl-review",self._evidence(),
   attempt_id="att-owner-inline",explicit_attempt_metadata=self._inline_axes())
  self.assertEqual(marker["reviewer_kind"],"owner-inline")
  self.assertEqual(marker["review_independence"],"degraded")
  self.assertEqual(marker["reviewer_downgrade_reason"],"review-completed-inline")
  # Recorded, never refused: the node completed and the next one may proceed.
  self.assertTrue(
   (R.completion_dir(route["route_id"],jobs=self.jobs)/"impl-review.json").is_file())

 def test_a_native_subagent_transcript_counts_as_independent_review(self):
  # The user's rule: a native subagent IS independent review, provided its
  # identity is recorded. The transcript digest is what makes it checkable
  # later instead of a claim that decays into "trust the owner".
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  transcript=self._evidence("subagent.jsonl",'{"role":"reviewer"}\n')
  marker,_=R.complete_node(
   route,node,"impl-review",self._evidence(),
   attempt_id="att-owner-inline",explicit_attempt_metadata=self._inline_axes(),
   review_claim={"kind":"native-subagent","transcript":str(transcript)})
  self.assertEqual(marker["reviewer_kind"],"native-subagent")
  self.assertEqual(marker["review_independence"],"independent")
  self.assertEqual(marker["reviewer_identity"],str(transcript.resolve()))
  self.assertEqual(
   marker["reviewer_identity_sha256"],
   hashlib.sha256(transcript.read_bytes()).hexdigest())

 def test_an_unreadable_transcript_downgrades_instead_of_refusing(self):
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  marker,_=R.complete_node(
   route,node,"impl-review",self._evidence(),
   attempt_id="att-owner-inline",explicit_attempt_metadata=self._inline_axes(),
   review_claim={"kind":"native-subagent",
                 "transcript":str(self.base/"absent.jsonl")})
  self.assertEqual(marker["reviewer_kind"],"owner-inline")
  self.assertEqual(marker["reviewer_downgrade_reason"],"reviewer-transcript-unreadable")

 # -- the self-declared registered reviewer -------------------------------

 def test_a_claimed_review_attempt_must_exist_and_be_a_review_worker(self):
  # Requirement (4): the claim is adjudicated by the registry, and a claim that
  # does not check out is DOWNGRADED, not refused -- a refusal here would make
  # a mistyped attempt id unrecoverable without reopening the gate.
  for label,setup,expected in (
   ("row absent",lambda route:None,"reviewer-attempt-row-absent"),
   ("wrong worker_type",
    lambda route:self._registered_row(
     "att-claimed",route,"impl-review","owner"),
    "reviewer-attempt-not-review-worker"),
  ):
   with self.subTest(label):
    self.setUp()
    route=self._route(self.base/"artifacts"); node=self._review_node(route)
    self._registered_row("att-completer",route,"impl-review","owner")
    setup(route)
    marker,_=R.complete_node(
     route,node,"impl-review",self._evidence(),
     jobs=str(self.jobs),attempt_id="att-completer",
     explicit_attempt_metadata=None,
     review_claim={"kind":"registered-worker","attempt_id":"att-claimed"})
    self.assertEqual(marker["reviewer_kind"],"owner-inline")
    self.assertEqual(marker["reviewer_downgrade_reason"],expected)

 def test_a_verified_review_attempt_is_independent_review(self):
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  self._registered_row("att-reviewer",route,"impl-review","review",status="done")
  self._registered_row("att-completer",route,"impl-review","owner")
  marker,row=R.complete_node(
   route,node,"impl-review",self._evidence(),
   jobs=str(self.jobs),attempt_id="att-completer",
   review_claim={"kind":"registered-worker","attempt_id":"att-reviewer"})
  self.assertEqual(marker["reviewer_kind"],"registered-worker")
  self.assertEqual(marker["review_independence"],"independent")
  self.assertEqual(marker["reviewer_identity"],"att-reviewer")
  self.assertEqual(row["status"],"closed")

 def test_a_claim_cannot_be_verified_without_a_registry(self):
  # No `--jobs` means no adjudicator. Believing the claim on the caller's word
  # is exactly the self-certification this gate exists to end.
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  marker,_=R.complete_node(
   route,node,"impl-review",self._evidence(),
   attempt_id="att-owner-inline",explicit_attempt_metadata=self._inline_axes(),
   review_claim={"kind":"registered-worker","attempt_id":"att-reviewer"})
  self.assertEqual(marker["reviewer_kind"],"owner-inline")
  self.assertEqual(
   marker["reviewer_downgrade_reason"],"reviewer-claim-unverifiable-no-registry")

 # -- the reviewer completing its own node --------------------------------

 def test_a_review_worker_completing_its_own_node_is_independent(self):
  # The 2,879-marker production shape. No claim flag is involved: the row that
  # closes the node says `worker_type=review`, and that is the evidence.
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  self._registered_row("att-selfclose",route,"impl-review","review")
  marker,row=R.complete_node(
   route,node,"impl-review",self._evidence(),
   jobs=str(self.jobs),attempt_id="att-selfclose")
  self.assertEqual(marker["reviewer_kind"],"registered-worker")
  self.assertEqual(marker["review_independence"],"independent")
  self.assertEqual(marker["reviewer_identity"],"att-selfclose")

 def test_a_registered_non_review_worker_is_still_a_self_review(self):
  # `registered_worker=true` was never proof of a review worker, and this is
  # the gap SD-OPEN-40 exists to close from the other side: before it, an
  # ad-hoc independent reviewer could only be launched as `worker_type=owner`.
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  self._registered_row("att-owner-worker",route,"impl-review","owner")
  marker,_=R.complete_node(
   route,node,"impl-review",self._evidence(),
   jobs=str(self.jobs),attempt_id="att-owner-worker")
  self.assertEqual(marker["reviewer_kind"],"owner-inline")
  self.assertEqual(marker["reviewer_downgrade_reason"],"completer-not-review-worker")

 # -- blast radius --------------------------------------------------------

 def test_a_non_review_node_marker_keeps_its_exact_shape(self):
  # 2,911 review markers gain fields; every other marker must gain none, or
  # this change would be a schema migration for the whole completion store.
  route=self._route(self.base/"artifacts")
  node=next(n for n in route["nodes"] if n["id"]=="execute")
  marker,_=R.complete_node(
   route,node,"execute",self._evidence("execute.md"),
   attempt_id="att-execute",explicit_attempt_metadata=self._inline_axes())
  for key in ("reviewer_kind","review_independence","reviewer_identity",
              "reviewer_downgrade_reason","reviewer_identity_sha256"):
   self.assertNotIn(key,marker)

 def test_a_reviewer_claim_on_a_non_review_node_is_refused(self):
  # Refusal, not downgrade: naming a reviewer for a node that has no review
  # verdict is a caller error with no correct interpretation.
  route=self._route(self.base/"artifacts")
  node=next(n for n in route["nodes"] if n["id"]=="execute")
  with self.assertRaises(ValueError) as caught:
   R.complete_node(
    route,node,"execute",self._evidence("execute.md"),
    attempt_id="att-execute",explicit_attempt_metadata=self._inline_axes(),
    review_claim={"kind":"native-subagent","transcript":"/dev/null"})
  self.assertIn("reviewer-claim-on-non-review-node",str(caught.exception))

 def test_provenance_is_not_part_of_marker_identity(self):
  # A replay must stay a replay. If `reviewer_kind` joined the identity keys,
  # re-running `complete` after the transcript moved would raise a conflict
  # instead of returning the same marker.
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  evidence=self._evidence()
  first,_=R.complete_node(
   route,node,"impl-review",evidence,
   attempt_id="att-owner-inline",explicit_attempt_metadata=self._inline_axes())
  second,_=R.complete_node(
   route,node,"impl-review",evidence,
   attempt_id="att-owner-inline",explicit_attempt_metadata=self._inline_axes(),
   review_claim={"kind":"native-subagent",
                 "transcript":str(self._evidence("late.jsonl","x\n"))})
  self.assertEqual(first,second)

 # -- the registry row and the closed outcome -----------------------------

 def test_the_row_records_the_degradation_and_still_says_completed_marker(self):
  # The deliberate deviation from "close it as `completed-review-degraded`":
  # two gates read `note == "completed-marker"` as "this row terminated with a
  # marker" (`dispatch_contract.marker_attempt_readiness`, and `complete`'s own
  # already-closed branch). Spelling the degradation into `note` would make the
  # idempotent second `complete` refuse the row it had just closed, so the
  # degradation is a typed axis beside the note instead.
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  self._registered_row("att-owner-worker",route,"impl-review","owner")
  R.complete_node(route,node,"impl-review",self._evidence(),
                  jobs=str(self.jobs),attempt_id="att-owner-worker")
  row=next(line for line in self.jobs.read_text(encoding="utf-8").splitlines()
           if "att-owner-worker" in line)
  metadata=D.parse_registry_metadata(row.split("\t")[5])
  self.assertEqual(metadata["note"],"completed-marker")
  self.assertEqual(metadata["reviewer_kind"],"owner-inline")
  self.assertEqual(metadata["review_independence"],"degraded")
  self.assertEqual(metadata["reviewer_downgrade_reason"],"completer-not-review-worker")
  # and the second call is still the idempotent no-op it was before
  marker,again=R.complete_node(route,node,"impl-review",self._evidence(),
                               jobs=str(self.jobs),attempt_id="att-owner-worker")
  self.assertEqual(again["status"],"already-closed")

 def test_the_closed_outcome_names_every_self_reviewed_gate(self):
  # Requirement (2): the route still closes, and its own sidecar carries the
  # fact that a gate was not independently reviewed -- so a later reader never
  # infers independence from "the route closed".
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  R.complete_node(route,node,"impl-review",self._evidence(),
                  attempt_id="att-owner-inline",
                  explicit_attempt_metadata=self._inline_axes())
  route_file=R.canonical_route_path(self.base/"artifacts",route["route_id"])
  route_file.parent.mkdir(parents=True,exist_ok=True)
  R.atomic_write(route_file,route)
  outcome,_=R.close_route(route,route_file,commit="0"*40,summary="x")
  self.assertEqual(outcome["review_independence"]["impl-review"]["reviewer_kind"],
                   "owner-inline")
  self.assertEqual(outcome["review_independence_degraded"],["impl-review"])
  # A review node completed before this field existed is reported honestly as
  # unrecorded, never guessed at.
  self.assertEqual(
   outcome["review_independence"]["plan-check"]["review_independence"],"unrecorded")



 # -- review round 1 fixes -------------------------------------------------

 def test_a_claimed_reviewer_must_have_reviewed_this_node(self):
  # Round 1 (1): the job title is not the assignment. Verifying only
  # `worker_type=review` collapsed the check to "does any review worker exist
  # anywhere in this registry", and the canonical registry answers yes
  # thousands of times. A stale attempt id from a previous cycle is the likely
  # case, not an attack.
  for label,route_id,node in (
   ("foreign route","rt-someotherroute","impl-review"),
   ("foreign node",None,"some-other-node"),
  ):
   with self.subTest(label):
    self.setUp()
    route=self._route(self.base/"artifacts"); rnode=self._review_node(route)
    self._registered_row("att-completer",route,"impl-review","owner")
    meta=",".join([
     "attempt_schema_version=2","dispatch_depth=2","transport=headless",
     "execution_surface=registered-headless","registered_worker=1",
     "fallback_hop=same-harness-headless","attempt_id=att-foreign",
     "worker_type=review","note=completed-marker",
     f"route_id={route_id or route['route_id']}",f"route_node={node}",
    ])
    with self.jobs.open("a",encoding="utf-8") as handle:
     handle.write("\t".join(["2026-09-06T00:00:00Z","done","r","w","s",meta])+"\n")
    marker,_=R.complete_node(
     route,rnode,"impl-review",self._evidence(),
     jobs=str(self.jobs),attempt_id="att-completer",
     review_claim={"kind":"registered-worker","attempt_id":"att-foreign"})
    self.assertEqual(marker["reviewer_kind"],"owner-inline")
    self.assertEqual(marker["reviewer_downgrade_reason"],
                     "reviewer-attempt-foreign-route-node")

 def test_a_route_less_ad_hoc_reviewer_is_still_admissible(self):
  # The predicate has to stay conditional: an SD-OPEN-40 reviewer is
  # deliberately route-less, and its row carries no route_id at all.
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  self._registered_row("att-completer",route,"impl-review","owner")
  meta=",".join([
   "attempt_schema_version=2","dispatch_depth=1","transport=headless",
   "execution_surface=registered-headless","registered_worker=1",
   "fallback_hop=same-harness-headless","attempt_id=att-adhoc",
   "worker_type=review","unit=qa/code-review","note=completed-marker",
  ])
  with self.jobs.open("a",encoding="utf-8") as handle:
   handle.write("\t".join(["2026-09-06T00:00:00Z","done","r","w","s",meta])+"\n")
  marker,_=R.complete_node(
   route,node,"impl-review",self._evidence(),
   jobs=str(self.jobs),attempt_id="att-completer",
   review_claim={"kind":"registered-worker","attempt_id":"att-adhoc"})
  self.assertEqual(marker["reviewer_kind"],"registered-worker")
  self.assertEqual(marker["review_independence"],"independent")

 def test_a_claimed_reviewer_that_produced_no_verdict_is_not_independent(self):
  # Round 1 (2): a review worker that was launched and died was
  # indistinguishable from one that reviewed. `open` qualified identically --
  # the passing test happened to set done, nothing required it.
  cases=(
   ("still running","open","",  "reviewer-attempt-no-terminal-verdict"),
   ("died","done","dead-worker-fail","reviewer-attempt-no-terminal-verdict"),
   ("api error","done","dead-api-error","reviewer-attempt-no-terminal-verdict"),
   ("blocking verdict","done","completed-review-blocking",None),
  )
  for label,status,note,expected in cases:
   with self.subTest(label):
    self.setUp()
    route=self._route(self.base/"artifacts"); node=self._review_node(route)
    self._registered_row("att-completer",route,"impl-review","owner")
    meta=",".join([
     "attempt_schema_version=2","dispatch_depth=2","transport=headless",
     "execution_surface=registered-headless","registered_worker=1",
     "fallback_hop=same-harness-headless","attempt_id=att-r",
     "worker_type=review",f"route_id={route['route_id']}",
     "route_node=impl-review",
    ]+([f"note={note}"] if note else []))
    with self.jobs.open("a",encoding="utf-8") as handle:
     handle.write("\t".join(["2026-09-06T00:00:00Z",status,"r","w","s",meta])+"\n")
    marker,_=R.complete_node(
     route,node,"impl-review",self._evidence(),
     jobs=str(self.jobs),attempt_id="att-completer",
     review_claim={"kind":"registered-worker","attempt_id":"att-r"})
    if expected is None:
     # A FAIL verdict is a produced verdict.
     self.assertEqual(marker["reviewer_kind"],"registered-worker")
    else:
     self.assertEqual(marker["reviewer_downgrade_reason"],expected)

 def test_a_subsession_slice_cannot_certify_a_review_gate(self):
  # Round 1 (3): `_complete_node_locked` already refuses a sub-session row as
  # the COMPLETER (`subsession-has-no-stage-gate-authority`); the reviewer path
  # simply never asked. A slice that may not satisfy the marker may not be the
  # evidence that the marker is independent either.
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  self._registered_row("att-completer",route,"impl-review","owner")
  meta=",".join([
   "attempt_schema_version=2","dispatch_depth=2","transport=headless",
   "execution_surface=registered-headless","registered_worker=1",
   "fallback_hop=same-harness-headless","attempt_id=att-slice",
   "worker_type=review","subsession_id=sub-1","stage_authority=0",
   f"route_id={route['route_id']}","route_node=impl-review",
   "note=completed-marker",
  ])
  with self.jobs.open("a",encoding="utf-8") as handle:
   handle.write("\t".join(["2026-09-06T00:00:00Z","done","r","w","s",meta])+"\n")
  marker,_=R.complete_node(
   route,node,"impl-review",self._evidence(),
   jobs=str(self.jobs),attempt_id="att-completer",
   review_claim={"kind":"registered-worker","attempt_id":"att-slice"})
  self.assertEqual(marker["reviewer_downgrade_reason"],"reviewer-attempt-subsession")

 def test_owner_closure_is_the_opposite_of_independent(self):
  # Round 1 (4), the sharpest one. SD-94 owner-closure hands the marker the
  # BLOCKING REVIEWER's row, so the ordinary rules read `worker_type=review`
  # and called the gate independent -- the exact inversion of what happened,
  # which is that the owner ruled over a review that returned FAIL. Proven at
  # the function boundary with the inputs that branch constructs (the full
  # owner-closure fixture needs an exhausted round budget and a re-inspectable
  # FAIL handoff; the code between here and the marker adds no reviewer logic).
  node={"kind":"review-worker"}
  axes={"attempt_id":"att-blocking-reviewer","registered_worker":True}
  plain=R.resolve_review_identity(node,axes,{"worker_type":"review"})
  self.assertEqual(plain["review_independence"],"independent")
  overridden=R.resolve_review_identity(
   node,axes,{"worker_type":"review"},owner_override=True)
  self.assertEqual(overridden["review_independence"],"owner-overridden")
  self.assertEqual(overridden["review_gate_closure"],"owner-closure")
  self.assertEqual(overridden["reviewer_identity"],"att-blocking-reviewer")

 def test_an_owner_overridden_gate_is_listed_with_the_degraded_ones(self):
  # The §0.5 card rule reads one list, so both non-independent verdicts have to
  # be in it -- otherwise the obligation never fires for the owner-closure case.
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  R.complete_node(route,node,"impl-review",self._evidence(),
                  attempt_id="att-inline",
                  explicit_attempt_metadata=self._inline_axes())
  path=R.completion_dir(route["route_id"],jobs=self.jobs)/"impl-review.json"
  marker=json.loads(path.read_text(encoding="utf-8"))
  marker["review_independence"]="owner-overridden"
  marker["review_gate_closure"]="owner-closure"
  marker.pop("reviewer_downgrade_reason",None)
  R.atomic_write(path,marker)
  route_file=R.canonical_route_path(self.base/"artifacts",route["route_id"])
  route_file.parent.mkdir(parents=True,exist_ok=True)
  R.atomic_write(route_file,route)
  outcome,_=R.close_route(route,route_file,commit="0"*40,summary="x")
  self.assertEqual(outcome["review_independence_degraded"],["impl-review"])
  self.assertEqual(
   outcome["review_independence"]["impl-review"]["review_gate_closure"],
   "owner-closure")

 def test_a_marker_written_before_provenance_existed_is_named_as_such(self):
  # Round 1 (5): the assertion that claimed to cover this took the
  # `marker-unreadable` branch instead, because the node had never completed at
  # all. `marker-predates-provenance` is the whole back-compat story for the
  # existing corpus and appeared in no test.
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  R.complete_node(route,node,"impl-review",self._evidence(),
                  attempt_id="att-inline",
                  explicit_attempt_metadata=self._inline_axes())
  path=R.completion_dir(route["route_id"],jobs=self.jobs)/"impl-review.json"
  marker=json.loads(path.read_text(encoding="utf-8"))
  for key in ("reviewer_kind","review_independence","reviewer_identity",
              "reviewer_downgrade_reason"):
   marker.pop(key,None)
  R.atomic_write(path,marker)
  route_file=R.canonical_route_path(self.base/"artifacts",route["route_id"])
  route_file.parent.mkdir(parents=True,exist_ok=True)
  R.atomic_write(route_file,route)
  outcome,_=R.close_route(route,route_file,commit="0"*40,summary="x")
  rows=outcome["review_independence"]
  self.assertEqual(rows["impl-review"],
                   {"review_independence":"unrecorded",
                    "reason":"marker-predates-provenance"})
  # and the never-completed node takes the OTHER branch -- the two are
  # distinguishable now, which is what the old assertion could not do.
  self.assertEqual(rows["plan-check"]["reason"],"marker-unreadable")
  self.assertNotIn("impl-review",outcome.get("review_independence_degraded",[]))

 def test_a_reviewer_claim_arriving_on_a_replay_says_it_was_ignored(self):
  # Round 1 (6): provenance stays out of marker identity (correct), so naming
  # the reviewer after the node completed is a no-op with exit 0. Honest, but
  # it read as "still degraded" rather than "your evidence was dropped".
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  evidence=self._evidence()
  R.complete_node(route,node,"impl-review",evidence,
                  attempt_id="att-inline",
                  explicit_attempt_metadata=self._inline_axes())
  stream=io.StringIO()
  with contextlib.redirect_stderr(stream):
   again,_=R.complete_node(
    route,node,"impl-review",evidence,
    attempt_id="att-inline",explicit_attempt_metadata=self._inline_axes(),
    review_claim={"kind":"native-subagent",
                  "transcript":str(self._evidence("late.jsonl","x\n"))})
  self.assertEqual(again["reviewer_kind"],"owner-inline")
  self.assertIn("reviewer-claim-ignored-on-replay",stream.getvalue())

 def test_an_owner_chain_aggregation_names_its_own_mechanism(self):
  # Round 1 (8): the owner-chain gate carries no per-slice worker_type, so it
  # is conservatively degraded -- but calling that `review-completed-inline`
  # described the wrong mechanism.
  node={"kind":"review-worker"}
  axes={"attempt_id":"att-chain","registered_worker":False}
  identity=R.resolve_review_identity(node,axes,{},owner_chain=True)
  self.assertEqual(identity["reviewer_downgrade_reason"],
                   "review-completed-owner-chain")

class FixtureRegistryGuardTest(unittest.TestCase):
    """The guard that keeps fixture rows out of the operator's live registry.

    It exists because 660 fixture rows were once written to the real
    `jobs.log`. It then drifted the other way: it asked
    `tempfile.gettempdir()` while the F47-3 golden fixture hardcodes `/tmp`
    (its path is hashed into route identity), so under
    `tools/run-tests.py --profile isolated`, which repoints TMPDIR, the guard
    refused a legitimate fixture and main was red on that one test while every
    direct run passed. Both directions are pinned here; neither was before.
    """

    _guard = staticmethod(TestContinuation._is_fixture_registry)

    def test_a_hardcoded_tmp_fixture_is_accepted_even_when_tmpdir_moved(self):
        with tempfile.TemporaryDirectory() as moved:
            with mock.patch.dict(os.environ, {"TMPDIR": moved}):
                self.assertTrue(self._guard("/tmp/hearting-f47-3-golden-fixture/state/jobs.log"))

    def test_a_fixture_under_the_current_tmpdir_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self._guard(Path(tmp)/"state"/"jobs.log"))

    def test_the_live_registry_is_still_refused(self):
        # The whole point. XDG state is where the real registry lives, and no
        # widening of the accepted scratch roots may reach it.
        for live in (
            "/home/someone/.local/state/hearting/dispatch/jobs.log",
            "/var/lib/hearting/dispatch/jobs.log",
            "/home/someone/hearting/.dispatch/jobs.log",
        ):
            with self.subTest(live):
                self.assertFalse(self._guard(live))

    def test_a_path_that_only_looks_like_tmp_is_refused(self):
        self.assertFalse(self._guard("/tmpfoo/jobs.log"))
        self.assertFalse(self._guard("/var/tmpish/jobs.log"))


class ComposeRouteTest(TestRoute):
 """SD-135: `compose` seals a preset-free shape/subgraph through the same sealer."""
 def evidence(self):
  return self.dispatch(self.nested(parent="claude",child="claude"),self.nested(parent="claude",child="codex"))
 def compose(self,**kw):
  d=dict(capability="autopilot-code",capability_mode="dev",shape="staged",graph="execute,test,report",slug="compose-fixture",cwd=R.ROOT,artifact_root=R.ROOT,dispatch_evidence=self.evidence())
  d.update(kw); return R.compose_route(**d)
 def test_campaign_selection_is_optional_validated_and_sealed(self):
  old=self.compose()
  self.assertNotIn("campaign_key",old); self.assertNotIn("parent_cycle_id",old)
  R.verify_route(old,R.ROOT)
  selected=self.compose(campaign_key="tts-v6-release",parent_cycle_id="cyc_"+"a"*32)
  self.assertEqual(selected["campaign_key"],"tts-v6-release")
  self.assertEqual(selected["parent_cycle_id"],"cyc_"+"a"*32)
  self.assertNotEqual(old["route_hash"],selected["route_hash"])
  R.verify_route(selected,R.ROOT)
  selected["campaign_key"]="stream-b"
  with self.assertRaisesRegex(ValueError,"modified route hash"): R.verify_route(selected,R.ROOT)
  for values in ({"campaign_key":""},{"campaign_key":"_unassigned"},{"campaign_key":"a/b"},
                 {"campaign_key":"a"*129},{"parent_cycle_id":"cyc_invalid"}):
   with self.assertRaises(ValueError): self.compose(**values)
 def test_graph_spec_parsing(self):
  self.assertEqual(R.parse_graph_spec("execute,test:qa/test , report"),[("execute",None),("test","qa/test"),("report",None)])
  for bad in ("", " , ", "execute,execute", "Bad!"):
   with self.assertRaises(ValueError): R.parse_graph_spec(bad)
 def test_staged_subgraph_is_composed_verified_and_linear(self):
  route=self.compose()
  self.assertTrue(route["composed"]); self.assertEqual(route["effective_intensity"],"standard")
  self.assertEqual([n["id"] for n in route["nodes"]],["execute","test","report"])
  self.assertEqual(route["nodes"][1]["depends_on"],["execute"]); self.assertEqual(route["nodes"][2]["depends_on"],["test"])
  self.assertTrue(route["nodes"][2]["terminal"]); self.assertEqual(route["nodes"][2]["terminal_gate"],"code-report")
  self.assertEqual(route["nodes"][0]["continuation"],{"kind":"inline-next"})
  self.assertEqual(route["nodes"][0]["unit"],"dev/backend"); self.assertEqual(route["nodes"][0]["completion_gate"],"code-execute")
  self.assertEqual(route["human_gates"],[]); self.assertEqual(route["human_gate_bindings"],[]); self.assertEqual(route["parallel_groups"],[])
  self.assertEqual(route["selection"]["route_origin"],"compose"); self.assertEqual(route["selection"]["shape"],"staged")
  self.assertEqual(route["composed_recipe"]["compose"]["graph"],["execute","test","report"])
  self.assertEqual(route["resume_retry_boundaries"],["execute","test","report"])
  self.assertEqual(route["conditional_extensions"][0]["after"],["report"])
  self.assertIn("small_work_confirmation",route)
  R.verify_route(route,R.ROOT)
 def test_staged_without_graph_uses_recipe_without_another_cli(self):
  route=self.compose(graph=None)
  self.assertFalse(route.get("composed",False))
  self.assertEqual(route["selection"]["shape"],"staged")
  self.assertEqual([n["id"] for n in route["nodes"]],
   ["frame","frame-alternative","plan","plan-check","execute","impl-review","test","report"])
  R.verify_route(route,R.ROOT)
 def test_chosen_graph_does_not_require_presets_review_stage(self):
  for intensity in ("standard","strong"):
   route=self.compose(graph="plan,test,report",intensity=intensity)
   self.assertEqual([n["id"] for n in route["nodes"]],["plan","test","report"])
   self.assertEqual(route["parallel_groups"],[])
   self.assertEqual(route["composed_recipe"]["compose"]["omitted_parallel_presets"],
    [{"id":"plan","reason":"review-consumer-not-selected"}])
   R.verify_route(route,R.ROOT)
 def test_frame_pair_stays_independent_and_gates_the_following_work(self):
  route=self.compose(graph="frame,frame-alternative,plan,test,report")
  nodes={n["id"]:n for n in route["nodes"]}
  self.assertEqual(nodes["frame"]["depends_on"],[])
  self.assertEqual(nodes["frame-alternative"]["depends_on"],[])
  self.assertEqual(nodes["plan"]["depends_on"],["frame","frame-alternative"])
  self.assertEqual(route["human_gate_bindings"],
   [{"gate":"frame-review","node":"plan","position":"entry"}])
  R.verify_route(route,R.ROOT)
 def test_subgraph_inputs_keep_the_base_contract(self):
  """Round-1 B1: a kept node keeps every declared input it can still get; a dropped producer's file is not promised."""
  route=self.compose(graph="execute,test,report")
  by_id={n["id"]:n for n in route["nodes"]}
  self.assertEqual(by_id["execute"]["inputs"],["task"])  # plan.md/checklist.md come from the dropped plan node
  self.assertEqual(by_id["test"]["inputs"],["source-diff"])  # semantic token kept; nothing appended (round 2 M2)
  self.assertEqual(by_id["report"]["inputs"],["dev_logs/**","test_logs/**"])
  # The widest graph compose can express today: `frame-alternative` cannot be
  # named alongside `frame`, because compose_subgraph_recipe emits one
  # `frame-review` binding per frame leg and the registry validator refuses a
  # gate bound twice. So the comparison is against the preset MINUS that leg,
  # with `plan` losing exactly the dropped producer's brief -- which is rule B1
  # itself, applied to the whole graph rather than to a three-node cut.
  full=self.compose(graph="frame,plan,plan-check,execute,impl-review,test,report")
  preset=R.compile_route(**self.args(requested_intensity="standard",predicates=[],signals=["shared-contract"],inline_reason=None,dispatch_evidence=self.evidence()))
  expected={n["id"]:list(n["inputs"]) for n in preset["nodes"] if n["id"]!="frame-alternative"}
  expected["plan"]=[i for i in expected["plan"] if i!="shards/frame-alternative/direction-brief.md"]
  self.assertEqual({n["id"]:n["inputs"] for n in full["nodes"]},expected)  # a full-graph compose equals the preset (group expansion included)
  self.assertEqual({n["id"]:n["inputs"] for n in self.compose(graph="impl-review,test")["nodes"]},{"impl-review":["source-diff"],"test":["source-diff"]})
 def test_frame_gate_rebinds_to_the_node_that_follows(self):
  route=self.compose(graph="frame,execute,test")
  self.assertEqual(route["human_gate_bindings"],[{"gate":"frame-review","node":"execute","position":"entry"}])
  frame=next(n for n in route["nodes"] if n["id"]=="frame")
  self.assertEqual(frame["continuation"],{"kind":"human-gate","gate":"frame-review"})
  # The second frame leg used to arrive for free, as a parallel-group replica
  # of `frame`. It is an explicitly declared node now, so a graph that names
  # only `frame` gets only `frame` -- and no realized group at all.
  self.assertEqual(route["parallel_groups"],[])
  self.assertNotIn("frame-alternative",[n["id"] for n in route["nodes"]])
  R.verify_route(route,R.ROOT)
 def test_source_node_entry_gate_is_kept(self):
  """autopilot-spec's `frame-review` binds the entry of the first node after `frame`; a full-graph compose keeps it."""
  # W5 retired `intent-confirmation` entirely -- autopilot-spec now raises only
  # `frame-review` from its `frame`/`frame-alternative` legs, bound at
  # `research@entry`. The rule under test is unchanged: an entry gate on a kept
  # node survives verbatim across compose.
  route=self.compose(capability="autopilot-spec",capability_mode="update",graph="frame,research,review,prd-transaction",signals=["shared-contract"])
  self.assertEqual(route["human_gate_bindings"],[
   {"gate":"frame-review","node":"research","position":"entry"}])
  self.assertEqual(route["human_gates"],["frame-review"])
  self.assertEqual([n["id"] for n in route["nodes"] if not n.get("parallel_leg_index")],["frame","research","review","prd-transaction"])
  self.assertTrue(route["nodes"][-1]["terminal"]); self.assertEqual(route["nodes"][-1]["dispatch_depth"],1)
  R.verify_route(route,R.ROOT)
  # ...and dropping the `frame` raiser drops the gate: nothing else in the
  # subgraph raises `frame-review`, so it is not promised even though `research`
  # (the bound node) is still present.
  without=self.compose(capability="autopilot-spec",capability_mode="update",graph="research,review,prd-transaction",signals=["shared-contract"])
  self.assertEqual(without["human_gate_bindings"],[]); self.assertEqual(without["human_gates"],[])
  R.verify_route(without,R.ROOT)
  # A review unit can be reused without selecting its preset's producer group.
  review_only=self.compose(capability="autopilot-spec",capability_mode="update",graph="review,prd-transaction",signals=["shared-contract"])
  R.verify_route(review_only,R.ROOT)
 def test_terminal_frame_drops_its_group_and_gate(self):
  # `frame` is no longer a parallel-group anchor, and it is a dispatch-depth-1
  # node, so a `frame`-only subgraph has no depth-2 evidence consumer and can
  # no longer be composed at all. The two rules this test protects are
  # unchanged and are pinned on the shapes that can still carry them.
  # (1) G6: a parallel group whose anchor became the terminal is dropped.
  grouped=self.compose(graph="plan,impl-review",intensity="strong")
  self.assertEqual([g["id"] for g in grouped["parallel_groups"]],["plan"])
  self.assertNotIn("impl-review-alternative",[n["id"] for n in grouped["nodes"]])
  self.assertTrue(grouped["nodes"][-1]["terminal"])
  R.verify_route(grouped,R.ROOT)
  # (2) a terminal frame raises no gate (nothing follows it) and is forced to
  # model-required; a dropped sink drops the conditional extension with it.
  route=self.compose(graph="plan,plan-check,frame")
  self.assertEqual([n["id"] for n in route["nodes"]],["plan","plan-check","frame"])
  self.assertTrue(route["nodes"][-1]["terminal"])
  self.assertEqual(route["parallel_groups"],[]); self.assertEqual(route["human_gates"],[])
  self.assertEqual(route["conditional_extensions"],[])
  self.assertEqual(route["nodes"][-1]["advance_class"],"model-required")
  R.verify_route(route,R.ROOT)
 def test_unit_override_must_be_a_declared_choice(self):
  route=self.compose(graph="execute:dev/refactor,test")
  self.assertEqual(route["nodes"][0]["unit"],"dev/refactor"); self.assertEqual(route["nodes"][0]["role"],"fast implementer")
  self.assertEqual(route["composed_recipe"]["compose"]["unit_overrides"],{"execute":"dev/refactor"})
  with self.assertRaisesRegex(ValueError,"compose-unit-not-in-choices"): self.compose(graph="execute:qa/test,test")
 def test_typed_refusals(self):
  with self.assertRaisesRegex(ValueError,"compose-graph-unknown-node"): self.compose(graph="execute,deploy")
  with self.assertRaisesRegex(ValueError,"compose-graph-only-staged"): self.compose(shape="direct",graph="execute")
  with self.assertRaisesRegex(ValueError,"compose-shape-intensity-mismatch"): self.compose(intensity="quick")
  with self.assertRaisesRegex(ValueError,"compose-shape-intensity-mismatch"): self.compose(shape="direct",graph=None,intensity="standard")
  with self.assertRaisesRegex(ValueError,"compose-capability-unknown"): self.compose(capability="autopilot-nope")
  with self.assertRaisesRegex(ValueError,"compose-mode-unknown"): self.compose(capability_mode="deploy")
  with self.assertRaisesRegex(ValueError,"compose-shape-invalid"): self.compose(shape="huge")
  with self.assertRaisesRegex(ValueError,"compose-direct-signals-conflict"): self.compose(shape="direct",graph=None,signals=["public-api"])
 def test_direct_shape_is_the_inline_node_with_compose_origin(self):
  route=self.compose(shape="direct",graph=None,dispatch_evidence=None)
  self.assertFalse(route.get("composed")); self.assertEqual(route["effective_intensity"],"direct")
  self.assertEqual(route["nodes"][0]["id"],"inline"); self.assertEqual(route["tracking"],"untracked")
  self.assertEqual(route["selection"],dict(route["selection"],route_origin="compose",shape="direct"))
  self.assertEqual(sorted(route["selection"]["direct_predicates"]),sorted(ALL))
  self.assertEqual(route["tracked_gate_evidence"]["spec_read"]["source"],"compose-auto: no spec/prd.md under cwd or artifact root")
  R.verify_route(route,R.ROOT)
  card=R.compose_card(route); self.assertIn("direct(direct)",card); self.assertIn(route["route_id"],card); self.assertIn("사람 게이트 없음",card)
 def test_solo_shape_is_one_registered_owner(self):
  # Two supported harnesses: solo compiles quick, and quick now refuses a
  # single-harness candidate list because its frame pair is cross-harness.
  cands={"candidates":[
   {"harness":"claude","transport":"headless","surface":"registered-headless","status":"supported","probe_source":"fixture","probe_time":"2026-09-07T00:00:00Z"},
   {"harness":"codex","transport":"headless","surface":"registered-headless","status":"supported","probe_source":"fixture","probe_time":"2026-09-07T00:00:00Z"}]}
  route=self.compose(shape="solo",graph=None,dispatch_evidence=None,registered_headless_evidence=cands)
  self.assertEqual(route["effective_intensity"],"quick")
  # Still exactly one owner -- the frame pair ahead of it are not owners.
  self.assertEqual([n["id"] for n in route["nodes"] if n.get("unit")=="_kernel/owner"],["one-shot"])
  self.assertEqual([n["id"] for n in route["nodes"]],["frame","frame-alternative","one-shot"])
  self.assertEqual(route["selection"]["shape"],"solo"); self.assertEqual(route["selection"]["route_origin"],"compose")
  R.verify_route(route,R.ROOT)
 def test_staged_accepts_strong_and_expands_declared_groups(self):
  route=self.compose(graph="plan,plan-check,execute",intensity="strong")
  self.assertEqual(route["effective_intensity"],"strong")
  self.assertEqual(sorted(g["id"] for g in route["parallel_groups"]),["plan","plan-check"])
  R.verify_route(route,R.ROOT)
 def test_spec_read_auto_refuses_when_a_spec_exists(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); (root/"spec").mkdir(); (root/"spec"/"prd.md").write_text("# prd\n",encoding="utf-8")
   with self.assertRaisesRegex(ValueError,"compose-spec-read-required"): R.compose_spec_read(root,root,"auto")
   self.assertEqual(R.compose_spec_read(root,root,"read spec/prd.md v3")["source"],"read spec/prd.md v3")
   self.assertTrue(R.compose_spec_read(R.ROOT,R.ROOT,None)["satisfied"])
 def test_preset_compile_records_preset_origin_and_derived_shape(self):
  route=R.compile_route(**self.args())
  self.assertEqual(route["selection"]["route_origin"],"preset"); self.assertEqual(route["selection"]["shape"],"direct")
  self.assertEqual(R.shape_for_intensity("quick"),"solo"); self.assertEqual(R.shape_for_intensity("thorough"),"staged")
  with self.assertRaisesRegex(ValueError,"invalid route origin"): R.compile_route(**self.args(route_origin="guess"))

class OwnerRegisteredCompletionTest(unittest.TestCase):
 """노드 키가 없는 실제 depth-1 오너 행도 등록 완료로 결속한다."""
 setUp=InlineStageCompletionRecipeTest.setUp
 _restore=InlineStageCompletionRecipeTest._restore

 def fixture(self, **overrides):
  t=TestRoute()
  route=R.compile_route(**t.args(capability="autopilot-spec",capability_mode="update",
   artifact_root=self.base/"artifacts",requested_intensity="standard",predicates=[],
   signals=["shared-contract"],transport="headless",inline_reason=None,
   dispatch_evidence=t.dispatch(t.nested())))
  node=next(n for n in route["nodes"] if n["id"]=="prd-transaction")
  path=Path(route["artifact_root"])/".runtime"/"routes"/(route["route_id"]+".json")
  path.parent.mkdir(parents=True); path.write_text(json.dumps(route))
  evidence=self.base/"artifacts"/"report.md"; evidence.write_text("검증 완료\n")
  meta={"attempt_schema_version":"2","dispatch_depth":"1","transport":"headless",
   "execution_surface":"registered-headless","registered_worker":"1",
   "fallback_hop":"same-harness-headless","attempt_id":"att-terminal-owner",
   "worker_type":"owner","unit":"_kernel/owner",
   "owner_route_id":route["route_id"],"owner_route_hash":route["route_hash"],
   "owner_route_file":str(path),"pid":"2147483647","pid_start":"1"}
  meta.update(overrides)
  self.jobs.write_text("\t".join(["2026-09-07T00:00:00Z","open","repo","worktree","owner",
   ",".join(k+"="+v for k,v in meta.items())])+"\n")
  return route,node,path,evidence

 def test_owner_complete_cli_uses_registered_row_and_replays(self):
  route,node,path,evidence=self.fixture()
  command=[str(P),"complete","--route",str(path),"--node",node["id"],
   "--evidence",str(evidence),"--jobs",str(self.jobs),"--attempt-id","att-terminal-owner"]
  for status in ("closed","already-closed"):
   output=io.StringIO()
   with mock.patch.object(sys,"argv",command),contextlib.redirect_stdout(output):
    R.main()
   marker,row=map(json.loads,output.getvalue().splitlines())
   self.assertEqual(row["status"],status)
   self.assertTrue(marker["registered_worker"])
   self.assertEqual(marker["attempt_id"],"att-terminal-owner")
   self.assertTrue(D.completion_marker_is_current(route,node,
    R.completion_dir(route["route_id"])/"prd-transaction.json"))
   # Identity lookup and the real registry writer are exercised; OS liveness
   # is isolated so this fixture never claims anything about host processes.
   for process,state in ((D.ProcessQuiescence("quiescent","fixture"),"ready"),
                         (D.ProcessQuiescence("live","fixture"),"draining")):
    with mock.patch.object(D,"attempt_process_quiescence",return_value=process) as probe:
     self.assertEqual(D.completion_attempt_readiness(route,node,marker,self.jobs).state,state)
     probe.assert_called_once()
  metadata=D.parse_registry_metadata(self.jobs.read_text().split("\t")[5])
  self.assertNotIn("route_id",metadata,"읽는 쪽 수정이며 등록 신원 재작성은 금지")
  self.assertEqual(metadata["owner_route_id"],route["route_id"])

 def test_foreign_owner_identity_cannot_publish_or_close(self):
  route,node,path,evidence=self.fixture(owner_route_hash="sha256:"+"f"*64)
  before=self.jobs.read_bytes()
  with self.assertRaisesRegex(ValueError,"route identity"):
   R.complete_node(route,node,node["id"],evidence,jobs=self.jobs,attempt_id="att-terminal-owner")
  self.assertEqual(self.jobs.read_bytes(),before)
  self.assertFalse((R.completion_dir(route["route_id"])/"prd-transaction.json").exists())



class TerminalCommitSupportTests(unittest.TestCase):
 """§13.53.2: activation is sealed as checked support, never a remembered switch.

 The regression these pin: before this cycle the route emitted a hardcoded
 `False` here, the Claude adapter only passes `--enable-terminal-commit` when
 the route says `True`, and no surface could set it -- so the SD-120/121 fast
 path was unreachable and A49-14 could not be run at all."""

 def _compose(self,runtime_root,config_path="/nonexistent-dispatch-defaults"):
  # Isolate from whatever the operator has configured on this machine: these
  # fixtures assert what the *census* decides, so a real `runtime.terminal_commit`
  # in the user's config must not reach them. Pointing at an absent path is the
  # documented "no config" case.
  with mock.patch.object(R.DEFAULTS,"default_config_path",return_value=config_path), \
       mock.patch.object(R,"_validation_basis",
                         return_value={"basis_version":R.VALIDATION_BASIS_VERSION,
                                       "registry_root":str(R.TOPO.ROOT),
                                       "unit_catalog_root":str(R.ROOT),
                                       "runtime_root":str(runtime_root),
                                       "runtime_root_validated":True,
                                       "runtime_root_match":True}):
   with tempfile.TemporaryDirectory() as artifacts:
    return R.compose_route(slug="gate",capability="autopilot-code",capability_mode="dev",
                           shape="direct",graph=None,cwd=str(R.ROOT),
                           artifact_root=artifacts,spec_read="fixture",
                           drift_verdict="fixture")

 def test_a_runtime_publishing_the_whole_contract_opens_the_gate(self):
  route=self._compose(R.ROOT)
  self.assertIs(route["runtime_support"]["terminal_commit"],True)

 def test_a_runtime_missing_the_lock_order_table_keeps_it_closed(self):
  """§13.53.4(3): the fence contract may not be claimed before the table is
  registered, so a runtime without it must not activate."""
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   for relative,_ in RUNTIME_SUPPORT.REQUIRED_SURFACES:
    target=root/relative; target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(R.ROOT/relative,target)
   (root/"utilities/dispatch_lock_order.py").unlink()
   self.assertIs(self._compose(root)["runtime_support"]["terminal_commit"],False)

 def test_an_unreadable_runtime_root_fails_closed(self):
  with tempfile.TemporaryDirectory() as tmp:
   self.assertIs(self._compose(Path(tmp)/"absent")["runtime_support"]["terminal_commit"],False)

 def test_the_contract_names_come_from_one_source(self):
  route=self._compose(R.ROOT)
  support=route["runtime_support"]
  self.assertEqual(support["terminal_commit_contract"],RUNTIME_SUPPORT.TERMINAL_COMMIT_CONTRACT)
  self.assertEqual(support["terminal_handoff_contract"],RUNTIME_SUPPORT.TERMINAL_HANDOFF_CONTRACT)
  self.assertEqual(support["producer_binding_contract"],RUNTIME_SUPPORT.PRODUCER_BINDING_CONTRACT)

 def _compose_with_config(self,text):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/"dispatch-defaults.yaml"; path.write_text(text,encoding="utf-8")
   return self._compose(R.ROOT,config_path=str(path))

 def _shipped_v4(self):
  base=Path(R.ROOT/"profiles"/"dispatch-defaults.yaml").read_text(encoding="utf-8")
  return base.replace("schema_version: 3","schema_version: 4",1)

 def test_operator_off_closes_the_gate_on_a_complete_runtime(self):
  route=self._compose_with_config(self._shipped_v4()+"\nruntime:\n  terminal_commit: off\n")
  self.assertIs(route["runtime_support"]["terminal_commit"],False)

 def _gate_never_opens(self,text,label):
  """A damaged config must never produce an open gate.

  Two refusals are acceptable and both are safe: `_seal_dispatch_defaults`
  rejects the whole compile (no route exists at all), or the seal itself
  returns `False`. What must never happen is a route sealed `True`."""
  try:
   route=self._compose_with_config(text)
  except ValueError as exc:
   self.assertIn("dispatch-defaults",str(exc),label)
   return "refused-compile"
  self.assertIs(route["runtime_support"]["terminal_commit"],False,label)
  return "sealed-false"

 def test_an_off_switch_survives_an_unrelated_invalid_key_in_the_same_file(self):
  """B1 regression. A config that exists but fails validation must not be
  treated as *no* config: falling back to the default would discard the
  operator's `off` precisely when the file is damaged -- the one moment they
  are most likely to have reached for the switch."""
  self._gate_never_opens(
      self._shipped_v4()+"\nruntime:\n  terminal_commit: off\nbogus_top_level: 1\n",
      "off + unrelated invalid key")

 def test_an_unrecognised_switch_value_closes_rather_than_opens(self):
  """B1 regression, second shape."""
  self._gate_never_opens(self._shipped_v4()+"\nruntime:\n  terminal_commit: bogus\n",
                         "unrecognised switch value")

 def test_an_unreadable_config_file_closes_the_gate(self):
  self._gate_never_opens("schema_version: [\n","unparsable config")

 def test_the_seal_helper_itself_is_fail_closed_on_an_invalid_config(self):
  """The compile-level refusal above is one layer; pin the helper directly too,
  since it is reachable independently of `_seal_dispatch_defaults`."""
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/"dispatch-defaults.yaml"
   path.write_text(self._shipped_v4()+"\nruntime:\n  terminal_commit: bogus\n",encoding="utf-8")
   with mock.patch.object(R.DEFAULTS,"default_config_path",return_value=str(path)):
    self.assertFalse(R._seal_terminal_commit_support({"runtime_root":str(R.ROOT)}))

 def test_the_gate_value_is_sealed_into_the_route_hash(self):
  """A route whose declared support differs is a different route: the adapter
  reads the flag from the file, so it must not be mutable after sealing."""
  route=self._compose(R.ROOT)
  self.assertEqual(route["route_hash"],R.route_hash(route))
  forged=json.loads(json.dumps(route)); forged["runtime_support"]["terminal_commit"]=False
  self.assertNotEqual(R.route_hash(forged),route["route_hash"])


class FrameBootstrapLayerTest(unittest.TestCase):
 """The depth-1 frame layer: quick's new three-node shape, the standard+ frame
 pair, and the five neighbouring properties that had to stay EXACTLY as they
 were. The second half is the point -- a route-shape change this wide is only
 safe if the things it did not touch are pinned as loudly as the things it did.

 Borrows `TestRoute`'s hermetic AGENT_HOME/registry isolation and its evidence
 fixtures by reference rather than by subclassing, so the whole `TestRoute`
 suite is not re-run a second time under this class's name.
 """
 setUp=TestRoute.setUp
 _restore_agent_home=TestRoute._restore_agent_home
 args=TestRoute.args
 dispatch=TestRoute.dispatch
 nested=TestRoute.nested
 FRAME_IDS=("frame","frame-alternative")
 FRAME_CAPABILITIES=(("autopilot-code","dev"),("autopilot-design","default"),
                     ("autopilot-draft","doc"),("autopilot-refine","default"),
                     ("autopilot-spec","api"))

 def quick(self,harnesses=("codex","claude"),**kw):
  candidates={"candidates":[
   {"harness":harness,"transport":"headless","surface":"registered-headless",
    "status":"supported","probe_source":"fixture-probe",
    "probe_time":"2026-07-20T00:00:00Z"} for harness in harnesses]}
  return R.compile_route(**self.args(requested_intensity="quick",predicates=[],
   transport=None,inline_reason=None,registered_headless_evidence=candidates,**kw))

 def standard(self,capability="autopilot-code",mode="dev"):
  return R.compile_route(capability,mode,"standard",R.ROOT,R.ROOT,predicates=[],
   transport="headless",tracking="tracked",
   tracked_gate_evidence=self.args()["tracked_gate_evidence"],
   dispatch_evidence=self.dispatch(self.nested()))

 # -- quick's three-node shape ---------------------------------------------
 def test_quick_frame_scope_is_exactly_the_five_portable_recipes(self):
  registry=R.TOPO.load_registry()
  framed=[]
  for recipe in registry["recipes"]:
   route=self.quick(capability=recipe["capability"],capability_mode=recipe["modes"][0])
   R.verify_route(route,R.ROOT)
   frames=[n["id"] for n in route["nodes"] if n.get("worker_type")=="frame"]
   if frames:
    framed.append(recipe["capability"])
    self.assertEqual(frames,["frame","frame-alternative"])
   else:
    self.assertEqual([n["id"] for n in route["nodes"]],["one-shot"])
    self.assertEqual(route["human_gate_bindings"],[])
  self.assertEqual(set(framed),{c for c,m in self.FRAME_CAPABILITIES})

 def test_orphaned_quick_preview_gate_is_rejected_even_when_rehashed(self):
  route=self.quick(capability="autopilot-refine",capability_mode="default")
  self.assertEqual(route["effective_intensity"],"quick")
  node=next(n for n in route["nodes"] if n["id"]=="one-shot")
  self.assertEqual(node["inline_human_gates"],["preview-disposition"])
  self.assertIn({"gate":"preview-disposition","node":"one-shot","position":"terminal"},route["human_gate_bindings"])
  R.verify_route(route,R.ROOT)
  node.pop("inline_human_gates")
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"preview-approval-boundary-missing"):
   R.verify_route(route,R.ROOT)

 def test_serial_attempt_survives_the_extra_two_nodes(self):
  """`serial-attempt` is a per-(route_id, route_node) attempt budget, so three
  nodes each get their own budget from the same policy word. If it had had to
  change, quick's registration budget would have changed with it."""
  route=self.quick()
  self.assertEqual(len(route["nodes"]),3)
  self.assertEqual(route["registered_headless_policy"],"serial-attempt")
  R.verify_route(route,R.ROOT)

 def test_the_frame_legs_do_not_raise_quicks_dispatch_depth(self):
  """The frame pair is depth 1, like the owner -- not a depth-2 stage. Quick
  gaining a depth-2 node would give it a fallback-chain obligation it has no
  evidence for."""
  route=self.quick()
  self.assertEqual(route["max_dispatch_depth"],1)
  self.assertEqual(route["owner_dispatch_depth"],1)
  self.assertEqual({node["dispatch_depth"] for node in route["nodes"]},{1})

 def test_the_conditional_extension_anchors_on_the_terminal_not_a_frame_leg(self):
  """autopilot-code's `offer-artifact` extension follows whatever ends the
  route. `frame` is nodes[0] now, so an anchor read by position would name it."""
  route=self.quick()
  self.assertEqual([row["after"] for row in route["conditional_extensions"]],[["one-shot"]])
  self.assertEqual([node["id"] for node in route["nodes"] if node.get("terminal")],["one-shot"])

 def test_only_the_one_shot_node_is_the_owner(self):
  """`_owner_node` is what lets a node inherit the owner's sealed profile and
  its `top` exception. A frame leg answering True here would silently take the
  owner's model on every quick route."""
  route=self.quick()
  by_id={node["id"]:node for node in route["nodes"]}
  self.assertTrue(R._owner_node(by_id["one-shot"],"quick"))
  for node_id in self.FRAME_IDS:
   with self.subTest(node_id=node_id):
    self.assertFalse(R._owner_node(by_id[node_id],"quick"))
    self.assertTrue(R._frame_node(by_id[node_id]))
  self.assertFalse(R._frame_node(by_id["one-shot"]))

 def test_quick_single_harness_compiles_as_a_recorded_degradation(self):
  """Cross-harness is the default, and one supported harness is a recorded
  degradation rather than a refusal (user decision, 2026-09-10): both legs run
  there with their two perspectives, and both frame nodes say so. Compile and
  verify read ONE helper, so a sealed route can never be compilable but
  unverifiable (or the reverse)."""
  route=self.quick(harnesses=("codex","claude"))
  self.assertEqual(len(route["nodes"]),3)
  by_id={n["id"]:n for n in route["nodes"]}
  for node_id in self.FRAME_IDS:
   self.assertEqual(by_id[node_id]["harness_diversity"],"cross-harness")
  R.verify_route(route,R.ROOT)
  single=self.quick(harnesses=("codex",))
  by_id={n["id"]:n for n in single["nodes"]}
  for node_id in self.FRAME_IDS:
   self.assertEqual(by_id[node_id]["harness_diversity"],"single-harness:codex")
  R.verify_route(single,R.ROOT)
  # A sealed cross-harness route that later loses a harness is refused: the
  # stamp says "cross-harness", the candidates now say one.
  forged=json.loads(json.dumps(route))
  forged["registered_headless_candidates"]=[
   row for row in forged["registered_headless_candidates"] if row["harness"]=="codex"]
  forged["route_hash"]=R.route_hash(forged)
  forged["route_id"]="rt-"+forged["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"harness diversity mismatch|not canonical"):
   R.verify_route(forged,R.ROOT)
  # No supported harness at all still cannot frame.
  with self.assertRaisesRegex(ValueError,"quick-frame-harness-unavailable"):
   R._quick_frame_diversity([{"harness":"codex","status":"unsupported"}])

 # -- the standard+ frame pair ---------------------------------------------
 def test_every_frame_capability_declares_the_same_pair_of_legs(self):
  for capability,mode in self.FRAME_CAPABILITIES:
   with self.subTest(capability=capability):
    route=self.standard(capability,mode)
    legs=[node for node in route["nodes"] if node["id"] in self.FRAME_IDS]
    self.assertEqual([node["id"] for node in legs],list(self.FRAME_IDS))
    for leg in legs:
     self.assertEqual(leg["kind"],"map-worker")
     self.assertEqual(leg["unit"],"plan/frame")
     self.assertEqual(leg["worker_type"],"frame")
     self.assertEqual(leg["dispatch_depth"],1)
     self.assertEqual(leg["launch_authority"],"depth-0")
     self.assertEqual(leg["continuation"],{"kind":"human-gate","gate":"frame-review"})
     self.assertEqual(leg["depends_on"],[])
     # a depth-1 leg has no fallback chain and no depth-2 affinity cell, and
     # it is not a parallel-group replica -- three separate ways the old
     # shape could leak back in
     self.assertNotIn("fallback_hops",leg)
     self.assertNotIn("harness_affinity",leg)
     self.assertNotIn("parallel_group",leg)
     self.assertNotIn(leg["id"],[group["id"] for group in route["parallel_groups"]])
    self.assertNotEqual(legs[0]["model_profile"],legs[1]["model_profile"])
    work=[node for node in route["nodes"] if node["id"] not in self.FRAME_IDS]
    self.assertEqual(work[0]["depends_on"],["frame","frame-alternative"])
    # membership, not position: refine also binds its preview approval
    # (`preview-disposition` at `transaction`) after the frame gate
    self.assertIn({"gate":"frame-review","node":work[0]["id"],"position":"entry"},
                  route["human_gate_bindings"])
    R.verify_route(route,R.ROOT)

 # -- what did NOT change ---------------------------------------------------
 def test_the_evidence_consumer_depth_is_still_a_constant_two(self):
  """Deliberately untouched. `EVIDENCE_CONSUMER_DISPATCH_DEPTH` is read by five
  call sites plus fallback-chain attachment; making it configurable so a
  depth-1 frame leg could consume evidence was rejected as far wider than the
  guarded early return that was shipped instead. Pin the constant AND its use,
  so a later edit cannot quietly soften either."""
  self.assertEqual(R.EVIDENCE_CONSUMER_DISPATCH_DEPTH,2)
  route=self.standard()
  nodes=route["nodes"]
  self.assertEqual(R._evidence_parent_dispatch_depth(nodes,1),1)
  # the derivation reads depth-2 nodes, and nothing else: strip them and the
  # frame pair (depth 1) does not stand in for them
  depth1_only=[node for node in nodes if node.get("dispatch_depth")!=2]
  self.assertTrue(any(R._frame_node(node) for node in depth1_only))
  with self.assertRaisesRegex(ValueError,"dispatch-evidence-without-consumer-node"):
   R._evidence_parent_dispatch_depth(depth1_only,1)
  # and the parent depth it derives is still owner depth, not the frame leg's
  with self.assertRaisesRegex(ValueError,"dispatch-evidence-parent-depth-mismatch"):
   R._evidence_parent_dispatch_depth(nodes,2)
  # the USE, not just the constant: compile and verify both still route their
  # checked evidence through this derivation, with the whole node list and the
  # owner's depth -- so the frame pair cannot become an evidence consumer by
  # anyone quietly rewiring a call site instead of the constant
  with mock.patch.object(R,"_evidence_parent_dispatch_depth",
                         wraps=R._evidence_parent_dispatch_depth) as spy:
   compiled=self.standard()
   R.verify_route(compiled,R.ROOT)
  self.assertTrue(spy.call_args_list)
  for call in spy.call_args_list:
   observed_nodes,owner_depth=call.args
   self.assertEqual(owner_depth,1)
   self.assertTrue(any(node.get("dispatch_depth")==2 for node in observed_nodes))

 def test_the_top_exception_widened_to_frame_ids_and_nothing_else(self):
  """Part C. `top` is a depth-1 decision: the owner, and now a frame anchor
  leg. A depth-2 stage node must still be refused by name."""
  nodes=[{"id":"frame","unit":"plan/frame","dispatch_depth":1,"worker_type":"frame",
          "model_profile":"balanced-deep"},
         {"id":"frame-alternative","unit":"plan/frame","dispatch_depth":1,
          "worker_type":"frame","model_profile":"light"},
         {"id":"execute","kind":"pipeline-stage","dispatch_depth":2,"model_profile":"light"}]
  demand={"schema_version":1,"judgment_requirement":"important",
          "execution_scope":"short-local",
          "judgment_reason":"Approved decision recorded in the task.",
          "execution_reason":"Execute the declared fixture steps.",
          "evidence_refs":["decision.md"]}
  demands={key:dict(demand) for key in ("__owner__","frame","frame-alternative","execute")}
  for node_id in self.FRAME_IDS:
   with self.subTest(node_id=node_id):
    _demands,explicit=R._profile_input_maps(nodes,demands,{node_id:"top"})
    self.assertEqual(explicit,{node_id:"top"})
  _demands,explicit=R._profile_input_maps(nodes,demands,{"__owner__":"top"})
  self.assertEqual(explicit,{"__owner__":"top"})
  with self.assertRaises(ValueError) as refused:
   R._profile_input_maps(nodes,demands,{"execute":"top"})
  self.assertEqual(str(refused.exception),"profile-explicit-top-owner-only:execute")
  # a node that only LOOKS like a frame leg does not get the exception either
  impostor=[dict(nodes[0],id="frame",worker_type="stage")]+nodes[1:]
  with self.assertRaises(ValueError) as refused:
   R._profile_input_maps(impostor,demands,{"frame":"top"})
  self.assertEqual(str(refused.exception),"profile-explicit-top-owner-only:frame")

 # -- the frame tier ladder is stamped, never inherited from the recipe -----
 def test_the_recipe_placeholder_profile_never_reaches_a_compiled_frame_leg(self):
  """THE POINT OF THIS TEST: the same decision must not live in two homes.

  `capabilities/topologies.json` declares a static `model_profile` on each
  standard+ frame leg, and the quick node builder hardcodes two more. None of
  them can be correct, because the right value depends on the owner profile the
  route resolves at compile time, which no static field can see. The compiler
  stamps every frame leg from `model_profile.FRAME_PROFILE_LADDER`, so the
  static values are placeholders. Without this test that is a silent latent
  bug: the placeholders would keep drifting and nothing would notice.
  """
  registry=R.TOPO.load_registry()
  ladder=R.PROFILE.FRAME_PROFILE_LADDER
  # The declared placeholders really do disagree with the ladder -- otherwise
  # this test would pass for the wrong reason (nothing to overwrite).
  disagreeing=0
  for capability,mode in self.FRAME_CAPABILITIES:
   recipe=R.TOPO.resolve_recipe(registry,capability,mode)
   owner=registry["owner_profile_by_intensity"]["standard"]
   rungs=ladder[owner]
   for node in recipe["standard_plus"]["nodes"]:
    if node.get("unit")!="plan/frame": continue
    rung="anchor" if node["id"]=="frame" else "others"
    if node.get("model_profile")!=rungs[rung]: disagreeing+=1
  self.assertGreater(disagreeing,0,
   "the static placeholders match the ladder, so this test cannot prove the stamp happens")

  # standard+: owner is `deep` at every intensity, so anchor `top` / other `deep`
  for capability,mode in self.FRAME_CAPABILITIES:
   with self.subTest(capability=capability):
    route=self.standard(capability=capability,mode=mode)
    rungs=ladder[route["owner_model_profile"]]
    by_id={n["id"]:n for n in route["nodes"]}
    self.assertEqual(by_id["frame"]["model_profile"],rungs["anchor"])
    self.assertEqual(by_id["frame-alternative"]["model_profile"],rungs["others"])
    R.verify_route(route,R.ROOT)

  # quick: owner is `balanced-deep`, so both legs land on `deep`
  route=self.quick()
  rungs=ladder[route["owner_model_profile"]]
  by_id={n["id"]:n for n in route["nodes"]}
  self.assertEqual(route["owner_model_profile"],"balanced-deep")
  self.assertEqual(by_id["frame"]["model_profile"],rungs["anchor"])
  self.assertEqual(by_id["frame-alternative"]["model_profile"],rungs["others"])
  self.assertEqual((rungs["anchor"],rungs["others"]),("deep","deep"))
  R.verify_route(route,R.ROOT)

 def test_a_top_anchor_carries_a_real_demand_rather_than_an_unsealed_label(self):
  """`top` is not portable, so it cannot be sealed through the legacy
  "explicit profile, no demand" path. If the compiler stamped the label without
  a demand, every standard+ compile would die `profile-demand-required` -- and
  if it stamped a demand that did not resolve to `top`, verify would refuse the
  route. Pin both halves."""
  route=self.standard()
  anchor=next(n for n in route["nodes"] if n["id"]=="frame")
  self.assertEqual(anchor["model_profile"],"top")
  self.assertEqual(anchor["profile_selection"]["resolved_profile"],"top")
  self.assertEqual(anchor["profile_selection"]["source"],"explicit")
  self.assertEqual(anchor["profile_selection"]["reason"],"explicit-top-exception")
  self.assertEqual(anchor["profile_demand"],R.PROFILE.FRAME_ANCHOR_SHAPE_DEMAND)
  # the sibling leg stays portable and needs no demand of its own
  other=next(n for n in route["nodes"] if n["id"]=="frame-alternative")
  self.assertEqual(other["model_profile"],"deep")
  self.assertIsNone(other["profile_demand"])
  R.verify_route(route,R.ROOT)

 def test_the_owners_own_demand_is_reused_when_the_caller_supplied_one(self):
  """One decision, one place: when the caller already recorded WHY this route
  needs top-tier judgment, the anchor reuses that demand rather than inventing
  a second, differently worded one."""
  demand={"schema_version":1,"judgment_requirement":"difficult-uncertain",
          "execution_scope":"extended-multistep",
          "judgment_reason":"caller supplied judgment reason",
          "execution_reason":"caller supplied execution reason",
          "evidence_refs":["fixture://caller"]}
  route=R.compile_route("autopilot-code","dev","standard",R.ROOT,R.ROOT,predicates=[],
   transport="headless",tracking="tracked",
   tracked_gate_evidence=self.args()["tracked_gate_evidence"],
   dispatch_evidence=self.dispatch(self.nested()),
   profile_demands={"__owner__":demand},explicit_profiles={"__owner__":"top"})
  self.assertEqual(route["owner_model_profile"],"top")
  anchor=next(n for n in route["nodes"] if n["id"]=="frame")
  self.assertEqual(anchor["model_profile"],"top")
  self.assertEqual(anchor["profile_demand"],demand)
  self.assertNotEqual(anchor["profile_demand"],R.PROFILE.FRAME_ANCHOR_SHAPE_DEMAND)
  R.verify_route(route,R.ROOT)


if __name__=="__main__": unittest.main()
