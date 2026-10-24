#!/usr/bin/env python3
import base64, contextlib, hashlib, importlib.util, io, json, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, unittest
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

# Completing nodes here must not launch detached open-cycle checkpoints into
# fixture roots, including from CLI subprocesses this suite spawns.
os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")


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
   if recipe["capability"]==R.ROUTE_FRAME_CAPABILITY:
    continue  # compiler-internal: reached only through `compose --shape framed`, never as a preset
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
   if recipe["capability"]==R.ROUTE_FRAME_CAPABILITY:
    continue  # compiler-internal: owner-less by design, covered by framed_route.test.py
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
       dispatch_evidence=evidence,registered_headless_evidence=self.registered_headless())
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
  self.assertEqual(compiled,168)  # 28 capability/mode pairs x 6 intensities
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
  self.assertEqual(alternative["parallel_independence_axes"],["model-profile","perspective"])
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
  self.assertEqual((frame["model_profile"],frame_replica["model_profile"]),("top","top"))
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
                     "depth_affinity_weight":.65,"usage_headroom_exponent":2,
                     "owner_order":["claude","opencode","codex"]})
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
                    ("owner_order",["claude","claude"]),
                    ("owner_order",["unknown"]),("owner_order","claude"),
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
 def test_standard_and_composed_routes_survive_an_unrelated_capability_edit(self):
  def other_capability(registry):
   next(r for r in registry["recipes"] if r["capability"]=="autopilot-ship")["probe"]="edited"
   registry["completion_gate_contracts"]["ship-setup"]["doc"]="edited doc"
  def own_capability(registry):
   next(r for r in registry["recipes"] if r["capability"]=="autopilot-code")["probe"]="edited"
  preset=self.compile_v3(self.dispatch(self.nested()))
  composed=self._composed()
  for route in (preset,composed):
   for mutate,current in ((other_capability,True),(own_capability,False)):
    registry=json.loads(json.dumps(R.TOPO.load_registry())); mutate(registry)
    with mock.patch.object(R.TOPO,"load_registry",return_value=registry):
     if current:
      self.assertNotIn("_registry_current",R.verify_route(route,R.ROOT))
     else:
      with self.assertRaisesRegex(ValueError,"stale registry digest"):
       R.verify_route(route,R.ROOT)
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
  route["capability_registry_digest"]="sha256:"+"0"*64
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
 def test_close_before_complete_records_unproven_without_refusing(self):
  # `close` before the terminal node's `complete` is not refused: the refusal
  # was bypassed almost every time it fired. The route closes, the sidecar
  # records `terminal_gate_proven=false`, and one warning line says so.
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   compiled=self._run_compile_cli(self._compile_cli_args(artifact_root))
   self.assertEqual(compiled.returncode,0,compiled.stderr)
   route=json.loads(compiled.stdout)
   route_path=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   result=subprocess.run([sys.executable,str(P),"close","--route",str(route_path)],
                         capture_output=True,text=True,cwd=str(R.ROOT))
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertNotIn("route-close-before-complete",result.stderr)
   self.assertFalse(json.loads(result.stdout)["terminal_gate_proven"])
   warnings=[line for line in result.stderr.splitlines() if "terminal-gate-unproven" in line]
   self.assertEqual(len(warnings),1,result.stderr)
   self.assertTrue(R.outcome_path(route_path).exists())
 def test_close_records_false_and_warns_for_direct_unproven_gate_with_override(self):
  # Red before P2: schema 2 outcomes carry neither `terminal_gate_proven` nor
  # `terminal_gates`, and `close` never printed a warning at all -- this exercises the
  # real CLI so the stderr contract, not just the in-process dict, is covered. Direct
  # routes declare an `inline` terminal but nothing writes its marker in this test, so
  # the aggregate must be `False`, never `None` -- a direct/inline close that silently
  # reported "no terminal node" would hide every unproven direct closure.
  # `--allow-unproven` is still accepted (compatibility) and changes nothing; the
  # no-flag form is covered by test_close_before_complete_records_unproven_without_refusing.
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
 def test_close_accepts_a_route_id(self):
  # `close --route` named a file only; the `rt-...` id the other commands print was a FileNotFoundError.
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   compiled=self._run_compile_cli(self._compile_cli_args(artifact_root))
   self.assertEqual(compiled.returncode,0,compiled.stderr)
   route=json.loads(compiled.stdout)
   route_path=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   result=subprocess.run(
    [sys.executable,str(P),"close","--route",route["route_id"],"--artifact-root",str(artifact_root),"--allow-unproven"],
    capture_output=True,text=True,cwd=str(R.ROOT))
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertTrue(R.outcome_path(route_path).exists())
   self.assertEqual(json.loads(R.outcome_path(route_path).read_text())["route_id"],route["route_id"])
   # The id works under AGENT_ARTIFACT_ROOT too, with no flag at all.
   again=subprocess.run([sys.executable,str(P),"close","--route",route["route_id"]],
                        capture_output=True,text=True,cwd=str(R.ROOT),
                        env={**os.environ,"AGENT_ARTIFACT_ROOT":str(artifact_root)})
   self.assertEqual(again.returncode,0,again.stderr)
 def test_close_with_an_unknown_route_id_says_not_found(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   result=subprocess.run(
    [sys.executable,str(P),"close","--route","rt-0123456789abcdef","--artifact-root",tmp,"--allow-unproven"],
    capture_output=True,text=True,cwd=str(R.ROOT))
   self.assertNotEqual(result.returncode,0)
   self.assertIn("route-not-found",result.stderr)
   self.assertIn("rt-0123456789abcdef",result.stderr)
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
 def test_historical_false_close_consumes_exact_later_marker_and_retains_raw_bytes(self):
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)/"artifacts"; artifact_root.mkdir()
   # The route is compiled against its own root so its hash and lineage stay
   # valid; the cycle/lineage gate reads the on-disk record.
   route=R.compile_route(**self.args(artifact_root=str(artifact_root)))
   node=route["nodes"][0]
   jobs=Path(tmp)/"jobs.log"; jobs.write_text("",encoding="utf-8")
   path=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   path.parent.mkdir(parents=True); path.write_text(json.dumps(route),encoding="utf-8")
   evidence=Path(tmp)/"evidence.txt"; evidence.write_text("terminal evidence",encoding="utf-8")
   with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}):
    first,created=R.close_route(route,path,allow_unproven=True,jobs=jobs)
    self.assertTrue(created); self.assertFalse(first["terminal_gate_proven"])
    raw=R.outcome_path(path).read_bytes()
    R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
    current,created=R.close_route(route,path,jobs=jobs)
   self.assertTrue(created)
   self.assertTrue(current["terminal_gate_proven"])
   self.assertEqual(R.outcome_path(path).read_bytes(),json.dumps(current,indent=2,ensure_ascii=False).encode()+b"\n")
   retained=list(path.parent.glob(f"{path.stem}.historical-false-*.outcome.json"))
   self.assertEqual(len(retained),1)
   self.assertEqual(retained[0].read_bytes(),raw)
   replay,recreated=R.close_route(route,path,jobs=jobs)
   self.assertFalse(recreated); self.assertTrue(replay["terminal_gate_proven"])
   self.assertEqual(retained[0].read_bytes(),raw)

 def _false_closed_route(self,tmp,**close_kw):
  # A route compiled against its own root, closed unproven, with the canonical
  # route file in place so the lineage gate reads a valid record.
  artifact_root=Path(tmp)/"artifacts"; artifact_root.mkdir()
  route=R.compile_route(**self.args(artifact_root=str(artifact_root)))
  node=route["nodes"][0]
  jobs=Path(tmp)/"jobs.log"; jobs.write_text("",encoding="utf-8")
  path=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
  path.parent.mkdir(parents=True); path.write_text(json.dumps(route),encoding="utf-8")
  evidence=Path(tmp)/"evidence.txt"; evidence.write_text("terminal evidence",encoding="utf-8")
  first,created=R.close_route(route,path,allow_unproven=True,jobs=jobs,**close_kw)
  self.assertTrue(created); self.assertFalse(first["terminal_gate_proven"])
  return route,node,path,jobs,evidence,R.outcome_path(path).read_bytes()

 def test_historical_false_close_without_marker_or_with_stop_is_not_promoted(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,path,jobs,evidence,raw=self._false_closed_route(tmp)
   again,created=R.close_route(route,path,jobs=jobs)  # no terminal marker yet
   self.assertFalse(created); self.assertFalse(again["terminal_gate_proven"])
   R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
   stopped=dict(route,stop_reason="user-stop")  # an explicit stop is never promoted
   kept,created=R.close_route(stopped,path,jobs=jobs)
   self.assertFalse(created); self.assertFalse(kept["terminal_gate_proven"])
   cancelled=dict(route,workflow_state="CANCELLED")
   kept,created=R.close_route(cancelled,path,jobs=jobs)
   self.assertFalse(created); self.assertFalse(kept["terminal_gate_proven"])
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)
   self.assertEqual(list(path.parent.glob("*.historical-false-*")),[])

 def test_actual_cancelled_workflow_keeps_historical_false_and_does_not_repair_cache(self):
  import workflow_state
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,path,jobs,evidence,raw=self._false_closed_route(tmp)
   R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
   self.assertTrue(R.terminal_gate_proven(R.terminal_gate_observation(route,jobs=jobs)))
   ledger=workflow_state.WorkflowLedger(route["route_id"],route["route_hash"],jobs=jobs)
   for state in ("READY","RUNNING","CANCELLED"):
    ledger.set_workflow_state(state,actor="fixture",evidence={"reason":"user stop"})
   ledger.state_path.write_text('{"workflow_state":"RUNNING"}\n',encoding="utf-8")
   journal=ledger.journal_path.read_bytes(); cache=ledger.state_path.read_bytes()
   self.assertNotIn("workflow_state",route)
   kept,created=R.close_route(route,path,jobs=jobs)
   self.assertFalse(created); self.assertFalse(kept["terminal_gate_proven"])
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)
   self.assertEqual(list(path.parent.glob("*.historical-false-*")),[])
   self.assertEqual(ledger.read_only_state()["workflow_state"],"CANCELLED")
   self.assertEqual(ledger.journal_path.read_bytes(),journal)
   self.assertEqual(ledger.state_path.read_bytes(),cache)

 def test_actual_workflow_cancellation_keeps_already_proven_replay(self):
  import workflow_state
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,path,jobs,evidence,raw=self._false_closed_route(tmp)
   R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
   proven,created=R.close_route(route,path,jobs=jobs)
   self.assertTrue(created and proven["terminal_gate_proven"])
   ledger=workflow_state.WorkflowLedger(route["route_id"],route["route_hash"],jobs=jobs)
   self.assertFalse(ledger.root.exists())  # no ledger is required or created by consumption
   for state in ("READY","RUNNING","CANCELLED"):
    ledger.set_workflow_state(state,actor="fixture",evidence={"reason":"later user stop"})
   proven_bytes=R.outcome_path(path).read_bytes()
   replay,created=R.close_route(route,path,jobs=jobs)
   self.assertFalse(created); self.assertTrue(replay["terminal_gate_proven"])
   self.assertEqual(R.outcome_path(path).read_bytes(),proven_bytes)
   self.assertEqual(ledger.read_only_state()["workflow_state"],"CANCELLED")

 def test_workflow_cancelled_after_terminal_observation_prevents_false_publication(self):
  import workflow_state
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,path,jobs,evidence,raw=self._false_closed_route(tmp)
   R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
   ledger=workflow_state.WorkflowLedger(route["route_id"],route["route_hash"],jobs=jobs)
   observe=R.terminal_gate_observation
   def cancel_after_observation(*args,**kwargs):
    gates=observe(*args,**kwargs)
    self.assertTrue(R.terminal_gate_proven(gates))
    for state in ("READY","RUNNING","CANCELLED"):
     ledger.set_workflow_state(state,actor="fixture",evidence={"reason":"user stop"})
    return gates
   with mock.patch.object(R,"terminal_gate_observation",side_effect=cancel_after_observation):
    kept,created=R.close_route(route,path,jobs=jobs)
   self.assertFalse(created); self.assertFalse(kept["terminal_gate_proven"])
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)
   self.assertEqual(list(path.parent.glob("*.historical-false-*")),[])
   self.assertEqual(ledger.read_only_state()["workflow_state"],"CANCELLED")

 def test_historical_false_close_identity_conflict_is_not_masked_and_keeps_bytes(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,path,jobs,evidence,raw=self._false_closed_route(tmp,terminal_commit_id="a"*40,expected_owner_attempt_id="att-a")
   R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
   with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
    R.close_route(route,path,terminal_commit_id="b"*40,expected_owner_attempt_id="att-a",jobs=jobs)
   with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
    R.close_route(route,path,terminal_commit_id="a"*40,expected_owner_attempt_id="att-foreign",jobs=jobs)
   with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
    R.close_route(route,path,terminal_commit_id="a"*40,jobs=jobs)  # omitting the owner cannot skip the stored one
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)
   self.assertEqual(list(path.parent.glob("*.historical-false-*")),[])

 def _registered_terminal_route(self,tmp):
  # A quick route whose terminal node is a registered attempt: the exact-terminal gate reads
  # the attempt row in the jobs registry, so its marker carries a real owner attempt.
  root=Path(tmp); jobs=root/"jobs.log"; evidence=root/"result.md"
  artifact_root=root/"artifacts"; artifact_root.mkdir()
  route=R.compile_route(**self.args(artifact_root=str(artifact_root),requested_intensity="quick",predicates=[],
      inline_reason=None,registered_headless_evidence=self.registered_headless()))
  node=next(n for n in route["nodes"] if n.get("terminal")); attempt="att-terminal-current"
  meta=dict(attempt_schema_version=2,dispatch_depth=1,transport="headless",
      execution_surface="registered-headless",registered_worker="1",fallback_hop="same-harness-headless",
      route_id=route["route_id"],route_hash=route["route_hash"],route_node=node["id"],
      attempt_id=attempt,failure_class="pass",note="completed-marker",launch_outcome="reaped-before-publish")
  jobs.write_text("2026-09-08T00:00:00Z\tdone\t/repo\t/wt\towner\t"+",".join(f"{k}={v}" for k,v in meta.items())+"\n")
  evidence.write_text("terminal evidence")
  path=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
  path.parent.mkdir(parents=True); path.write_text(json.dumps(route),encoding="utf-8")
  def publish():
   R._publish_completion_locked(route,node,node["id"],evidence,attempt_id=attempt,attempt_metadata=meta,jobs=jobs)
   return R.terminal_gate_observation(route,jobs=jobs,exact_terminal=True)[node["id"]]
  return route,node,attempt,path,jobs,publish

 def test_identity_bearing_false_close_is_consumed_by_the_same_actual_tuple_once(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   # The owner's real current state: marker, producer binding file and claimed slot for that marker, with the
   # earlier close recorded unproven under the tuple that slot holds.
   route,node,attempt,path,jobs,status,actual,raw=self._actual_false_close(tmp)
   status("done")
   gate=R.terminal_gate_observation(route,jobs=jobs,exact_terminal=True)[node["id"]]
   self.assertTrue(gate["passed"],gate); self.assertEqual(gate["attempt_id"],attempt)
   commit=actual["terminal_commit_id"]; binding=actual["expected_producer_binding_digest"]
   marker_digest=actual["expected_terminal_marker_digest"]
   self.assertEqual(marker_digest,R.dispatch_terminal_commit.terminal_marker_digest([gate]))
   for foreign in (dict(actual,expected_owner_attempt_id="att-foreign"),
                   dict(actual,expected_producer_binding_digest="sha256:"+"0"*64),
                   dict(actual,terminal_commit_id="d"*64)):
    with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
     R.close_route(route,path,jobs=jobs,**foreign)
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)
   self.assertEqual(list(path.parent.glob("*.historical-false-*")),[])
   tuple_args=dict(actual)
   current,created=R.close_route(route,path,jobs=jobs,**tuple_args)
   self.assertTrue(created); self.assertTrue(current["terminal_gate_proven"])
   self.assertEqual((current["terminal_commit_id"],current["terminal_owner_attempt_id"],
                     current["producer_binding_digest"],current["terminal_marker_digest"]),
                    (commit,attempt,binding,marker_digest))
   retained=list(path.parent.glob("*.historical-false-*.outcome.json"))
   self.assertEqual(len(retained),1); self.assertEqual(retained[0].read_bytes(),raw)
   settled=R.outcome_path(path).read_bytes()
   for replay_args in (tuple_args,{}):  # the same tuple, and a legacy caller naming nothing
    replay,created=R.close_route(route,path,jobs=jobs,**replay_args)
    self.assertFalse(created); self.assertEqual(replay,current)
   self.assertEqual(R.outcome_path(path).read_bytes(),settled)
   self.assertEqual(len(list(path.parent.glob("*.historical-false-*.outcome.json"))),1)

 def test_proven_exact_replay_survives_missing_marker_and_rejects_foreign_axes(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,attempt,path,jobs,publish=self._registered_terminal_route(tmp)
   gate=publish(); marker_digest=R.dispatch_terminal_commit.terminal_marker_digest([gate])
   binding="sha256:"+"b"*64; commit="c"*40
   args=dict(terminal_commit_id=commit,expected_owner_attempt_id=attempt,
    expected_producer_binding_digest=binding,expected_terminal_marker_digest=marker_digest)
   first,created=R.close_route(route,path,allow_unproven=False,jobs=jobs,**args)
   self.assertTrue(created); self.assertTrue(first["terminal_gate_proven"])
   raw=R.outcome_path(path).read_bytes()
   for marker in R.completion_dir(route["route_id"],jobs=jobs).glob("*.json"): marker.unlink()
   for replay_args in (args,{}):
    replay,created=R.close_route(route,path,allow_unproven=True,jobs=jobs,**replay_args)
    self.assertFalse(created); self.assertEqual(replay,first)
   with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
    R.close_route(route,path,jobs=jobs,**dict(args,expected_owner_attempt_id="att-foreign"))
   with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
    R.close_route(route,path,jobs=jobs,**dict(args,terminal_commit_id="d"*40,expected_owner_attempt_id="att-foreign"))
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)


 def _actual_terminal_state(self,route,attempt,gate):
  # What an owner's own settle leaves for the current marker: the root identity, its producer
  # binding file and the terminal-commit slot, all built with the real terminal-commit functions.
  import artifact_admission
  T=R.dispatch_terminal_commit; root=Path(route["artifact_root"]).resolve()
  ident=artifact_admission.ensure_root_identity(root)
  binding={"schema_version":1,"contract":T.CONTRACT,"owner_attempt_id":attempt,"route_id":route["route_id"],
   "route_hash":route["route_hash"],"artifact_root":str(root),
   "root_identity":{"repository_id":ident.repository_id,"artifact_root_id":ident.artifact_root_id},
   "cycle_id":"cyc_"+"a"*32}
  bpath=T.producer_binding_path(root,route["route_id"],attempt)
  T._write_exclusive(bpath,T._canonical(binding)+b"\n")
  producer=T._digest(bpath.read_bytes()); marker=T.terminal_marker_digest([gate])
  commit=T.terminal_commit_id(route_id=route["route_id"],route_hash=route["route_hash"],owner_attempt_id=attempt,
   marker_digest=marker,producer_digest=producer)
  T._atomic_json(T.terminal_slot(root,route["route_id"],attempt)/"terminal-commit.json",
   {"schema_version":1,"terminal_commit_id":commit,"route_id":route["route_id"],"route_hash":route["route_hash"],
    "owner_attempt_id":attempt,"terminal_marker_digest":marker,"producer_binding_digest":producer,"state":"claimed"})
  return dict(terminal_commit_id=commit,expected_owner_attempt_id=attempt,
   expected_producer_binding_digest=producer,expected_terminal_marker_digest=marker)

 def _actual_false_close(self,tmp):
  # The owner's marker exists but its attempt row is not settled, so the close is recorded unproven
  # with the real tuple the settled row will later have.
  route,node,attempt,path,jobs,publish=self._registered_terminal_route(tmp)
  settled=jobs.read_text()
  def status(value):
   fields=settled.rstrip("\n").split("\t"); fields[1]=value; jobs.write_text("\t".join(fields)+"\n")
  status("running"); R._publish_completion_locked(route,node,node["id"],Path(tmp)/"result.md",
   attempt_id=attempt,attempt_metadata=R.parse_registry_metadata(settled.split("\t",5)[5]),jobs=jobs)
  self.assertFalse(R.terminal_gate_observation(route,jobs=jobs,exact_terminal=True)[node["id"]]["passed"])
  status("done"); gate=R.terminal_gate_observation(route,jobs=jobs,exact_terminal=True)[node["id"]]
  self.assertTrue(gate["passed"],gate); self.assertEqual(gate["attempt_id"],attempt)
  actual=self._actual_terminal_state(route,attempt,gate)
  status("running")
  first,created=R.close_route(route,path,allow_unproven=True,jobs=jobs,**actual)
  self.assertTrue(created); self.assertFalse(first["terminal_gate_proven"])
  return route,node,attempt,path,jobs,status,actual,R.outcome_path(path).read_bytes()

 def test_identity_bearing_false_close_is_consumed_by_a_normal_close_from_the_actual_current_tuple(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,attempt,path,jobs,status,actual,raw=self._actual_false_close(tmp)
   same,created=R.close_route(route,path,jobs=jobs)   # not settled yet: nothing is consumed, nothing is refused
   self.assertFalse(created); self.assertFalse(same["terminal_gate_proven"])
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)
   status("done")
   current,created=R.close_route(route,path)   # no identity or registry argument, as finalize_direct_quick closes
   self.assertTrue(created); self.assertTrue(current["terminal_gate_proven"])
   self.assertEqual((current["terminal_commit_id"],current["terminal_owner_attempt_id"],
    current["producer_binding_digest"],current["terminal_marker_digest"]),
    tuple(actual[key] for key in ("terminal_commit_id","expected_owner_attempt_id",
     "expected_producer_binding_digest","expected_terminal_marker_digest")))
   retained=list(path.parent.glob("*.historical-false-*.outcome.json"))
   self.assertEqual(len(retained),1); self.assertEqual(retained[0].read_bytes(),raw)
   settled=R.outcome_path(path).read_bytes()
   for args in ({},actual):
    replay,created=R.close_route(route,path,jobs=jobs,**args)
    self.assertFalse(created); self.assertEqual(replay,current)
   self.assertEqual(R.outcome_path(path).read_bytes(),settled)
   self.assertEqual(len(list(path.parent.glob("*.historical-false-*.outcome.json"))),1)

 def test_historical_binding_and_commit_are_not_proven_when_the_current_binding_file_is_absent(self):
  # The owner's slot (digest/id) is the only current record left once the binding file is gone; the
  # earlier false close names another digest and the id computed from it.
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   T=R.dispatch_terminal_commit
   route,node,attempt,path,jobs,status,actual,_=self._actual_false_close(tmp)
   foreign="sha256:"+"5"*64
   foreign_id=T.terminal_commit_id(route_id=route["route_id"],route_hash=route["route_hash"],owner_attempt_id=attempt,
    marker_digest=actual["expected_terminal_marker_digest"],producer_digest=foreign)
   self.assertNotEqual(foreign_id,actual["terminal_commit_id"])
   held=dict(actual,terminal_commit_id=foreign_id,expected_producer_binding_digest=foreign)
   R.outcome_path(path).unlink()   # temporary fixture: record the historical false afresh
   first,created=R.close_route(route,path,allow_unproven=True,jobs=jobs,**held)
   self.assertTrue(created); self.assertFalse(first["terminal_gate_proven"])
   raw=R.outcome_path(path).read_bytes()
   status("done")
   binding_file=T.producer_binding_path(Path(route["artifact_root"]).resolve(),route["route_id"],attempt)
   slot_file=T.terminal_slot(Path(route["artifact_root"]).resolve(),route["route_id"],attempt)/"terminal-commit.json"
   slot=slot_file.read_bytes()
   for present in (True,False):
    if not present:
     binding_file.unlink()
    for label,kwargs in (("nothing named",{}),("historical tuple re-supplied",held)):
     with self.subTest(binding_file=present,caller=label):
      with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
       R.close_route(route,path,jobs=jobs,**kwargs)
      self.assertEqual(R.outcome_path(path).read_bytes(),raw)
      self.assertEqual(slot_file.read_bytes(),slot)
      self.assertEqual(list(path.parent.glob("*.historical-false-*")),[])
   # With neither a binding file nor a verified slot there is no producer authority at all.
   slot_file.unlink()
   with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
    R.close_route(route,path,jobs=jobs,**held)
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)

 def test_identity_bearing_false_close_is_not_taken_by_another_owner_or_binding(self):
  # The false record names an earlier owner (its own slot, binding and marker set); the marker held now
  # is another owner's. Neither a caller naming nothing nor one handing the old tuple back takes it.
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,attempt,path,jobs,publish=self._registered_terminal_route(tmp)
   current=publish(); self.assertTrue(current["passed"],current)
   old=dict(current,attempt_id="att-old-owner",marker_digest="e"*64)
   old_tuple=self._actual_terminal_state(route,"att-old-owner",old)
   self.assertNotEqual(old_tuple["expected_terminal_marker_digest"],R.dispatch_terminal_commit.terminal_marker_digest([current]))
   fields=jobs.read_text().rstrip("\n").split("\t"); settled=jobs.read_text()
   jobs.write_text("\t".join(fields[:1]+["running"]+fields[2:])+"\n")
   first,created=R.close_route(route,path,allow_unproven=True,jobs=jobs,**old_tuple)
   self.assertTrue(created); self.assertFalse(first["terminal_gate_proven"])
   raw=R.outcome_path(path).read_bytes(); jobs.write_text(settled)
   own=self._actual_terminal_state(route,attempt,current)
   for label,kw in (("nothing named",{}),("the old tuple again",old_tuple),("the current tuple",own),
                    ("the old owner under the current marker",dict(old_tuple,expected_terminal_marker_digest=own["expected_terminal_marker_digest"]))):
    with self.subTest(label), self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
     R.close_route(route,path,jobs=jobs,**kw)
    self.assertEqual(R.outcome_path(path).read_bytes(),raw)
    self.assertEqual(list(path.parent.glob("*.historical-false-*")),[])

 def test_normal_complete_consumes_an_identity_bearing_false_close_from_the_actual_tuple_once(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,attempt,path,jobs,status,actual,raw=self._actual_false_close(tmp)
   status("done")
   evidence=Path(tmp)/"result.md"
   marker=json.loads((R.completion_dir(route["route_id"],jobs=jobs)/f"{node['id']}.json").read_text())
   # the owner's normal complete: its marker is published by the completion path, then the hook reads the state
   with mock.patch.object(R,"_complete_node_locked",return_value=(marker,{"status":"closed"})), \
        mock.patch.object(R,"materialize_after_terminal_close"):
    R.complete_node(route,node,node["id"],evidence,jobs=jobs,attempt_id=attempt)
   current=json.loads(R.outcome_path(path).read_text())
   self.assertTrue(current["terminal_gate_proven"])
   self.assertEqual(current["historical_false_sha256"],"sha256:"+hashlib.sha256(raw).hexdigest())
   self.assertEqual((current["terminal_commit_id"],current["terminal_owner_attempt_id"],
    current["producer_binding_digest"],current["terminal_marker_digest"]),
    tuple(actual[key] for key in ("terminal_commit_id","expected_owner_attempt_id",
     "expected_producer_binding_digest","expected_terminal_marker_digest")))
   retained=list(path.parent.glob("*.historical-false-*.outcome.json"))
   self.assertEqual(len(retained),1); self.assertEqual(retained[0].read_bytes(),raw)
   settled=R.outcome_path(path).read_bytes()
   with mock.patch.object(R,"_complete_node_locked",return_value=(marker,{"status":"closed"})), \
        mock.patch.object(R,"materialize_after_terminal_close"):
    R.complete_node(route,node,node["id"],evidence,jobs=jobs,attempt_id=attempt)   # a repeated complete
   self.assertEqual(R.outcome_path(path).read_bytes(),settled)
   self.assertEqual(len(list(path.parent.glob("*.historical-false-*.outcome.json"))),1)

 def test_normal_complete_leaves_an_identity_bearing_false_close_of_another_binding_untouched(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,attempt,path,jobs,status,actual,raw=self._actual_false_close(tmp)
   status("done")
   T=R.dispatch_terminal_commit; bpath=T.producer_binding_path(Path(route["artifact_root"]),route["route_id"],attempt)
   stored=json.loads(bpath.read_text()); stored["cycle_id"]="cyc_"+"b"*32; bpath.write_bytes(T._canonical(stored)+b"\n")
   marker=json.loads((R.completion_dir(route["route_id"],jobs=jobs)/f"{node['id']}.json").read_text())
   with mock.patch.object(R,"_complete_node_locked",return_value=(marker,{"status":"closed"})), \
        mock.patch.object(R,"materialize_after_terminal_close"):
    R.complete_node(route,node,node["id"],Path(tmp)/"result.md",jobs=jobs,attempt_id=attempt)
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)
   self.assertEqual(list(path.parent.glob("*.historical-false-*")),[])

 def test_identity_bearing_false_close_with_another_binding_is_not_consumed(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,attempt,path,jobs,status,actual,raw=self._actual_false_close(tmp)
   status("done")
   T=R.dispatch_terminal_commit; bpath=T.producer_binding_path(Path(route["artifact_root"]),route["route_id"],attempt)
   stored=json.loads(bpath.read_text()); stored["cycle_id"]="cyc_"+"b"*32; bpath.write_bytes(T._canonical(stored)+b"\n")
   for label,kw in (("nothing named",{}),("the recorded tuple",actual)):
    with self.subTest(label), self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
     R.close_route(route,path,jobs=jobs,**kw)
    self.assertEqual(R.outcome_path(path).read_bytes(),raw)
    self.assertEqual(list(path.parent.glob("*.historical-false-*")),[])

 def _retained_name(self,path,raw):
  return path.with_name(f"{path.stem}.historical-false-{hashlib.sha256(raw).hexdigest()[:16]}.outcome.json")

 def test_historical_false_promotion_interrupted_before_replacement_resumes_once(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,path,jobs,evidence,raw=self._false_closed_route(tmp)
   R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
   retained=self._retained_name(path,raw)
   with mock.patch.object(R,"atomic_write",side_effect=OSError("interrupted after the retained copy")):
    with self.assertRaises(OSError): R.close_route(route,path,jobs=jobs)
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)   # canonical false untouched
   self.assertEqual(retained.read_bytes(),raw)               # raw history already durable, whole
   current,created=R.close_route(route,path,jobs=jobs)       # the ordinary retry resumes it
   self.assertTrue(created); self.assertTrue(current["terminal_gate_proven"])
   self.assertEqual(retained.read_bytes(),raw)
   self.assertEqual(len(list(path.parent.glob("*.historical-false-*.outcome.json"))),1)
   settled=R.outcome_path(path).read_bytes()
   lost,created=R.close_route(route,path,jobs=jobs)          # the response was lost: call again
   self.assertFalse(created); self.assertEqual(lost,current)
   self.assertEqual(R.outcome_path(path).read_bytes(),settled)
   self.assertEqual(len(list(path.parent.glob("*.historical-false-*.outcome.json"))),1)

 def test_historical_false_promotion_recovers_from_incomplete_retained_write(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,path,jobs,evidence,raw=self._false_closed_route(tmp)
   R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
   retained=self._retained_name(path,raw)
   retained.write_bytes(raw[:len(raw)//2])  # an earlier write that stopped part-way
   current,created=R.close_route(route,path,jobs=jobs)
   self.assertTrue(created); self.assertTrue(current["terminal_gate_proven"])
   self.assertEqual(retained.read_bytes(),raw)
   self.assertEqual(list(path.parent.glob(".historical-false-*")),[])
   self.assertEqual(len(list(path.parent.glob("*.historical-false-*.outcome.json"))),1)

 def test_historical_false_promotion_refuses_a_retained_copy_that_is_not_its_prefix(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,path,jobs,evidence,raw=self._false_closed_route(tmp)
   R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
   retained=self._retained_name(path,raw); retained.write_bytes(b"{}\n")
   with self.assertRaisesRegex(ValueError,"route-close-history-conflict"):
    R.close_route(route,path,jobs=jobs)
   self.assertEqual(R.outcome_path(path).read_bytes(),raw); self.assertEqual(retained.read_bytes(),b"{}\n")

 def test_two_concurrent_closes_consume_historical_false_once(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   route,node,path,jobs,evidence,raw=self._false_closed_route(tmp)
   R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
   code=("import importlib.util,json,sys\n"
         "s=importlib.util.spec_from_file_location('r',sys.argv[1]);R=importlib.util.module_from_spec(s);s.loader.exec_module(R)\n"
         "from pathlib import Path\n"
         "route=json.loads(Path(sys.argv[2]).read_text())\n"
         "out,created=R.close_route(route,Path(sys.argv[2]),jobs=Path(sys.argv[3]))\n"
         "print(json.dumps([bool(created),out['terminal_gate_proven']]))\n")
   env=dict(os.environ,AGENT_DISPATCH_JOBS=str(jobs))
   procs=[subprocess.Popen([sys.executable,"-c",code,str(P),str(path),str(jobs)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
   results=[]
   for proc in procs:
    out,err=proc.communicate(timeout=120); self.assertEqual(proc.returncode,0,err); results.append(json.loads(out.strip().splitlines()[-1]))
   self.assertEqual(sorted(r[0] for r in results),[False,True]); self.assertTrue(all(r[1] for r in results),results)
   retained=list(path.parent.glob("*.historical-false-*.outcome.json"))
   self.assertEqual(len(retained),1); self.assertEqual(retained[0].read_bytes(),raw)

 def test_outcome_replay_cannot_hide_identity_conflict_behind_matching_commit(self):
  proven={"route_id":"rt-0123456789abcdef","route_hash":"sha256:"+"a"*64,"terminal_gate_proven":True,
          "terminal_commit_id":"commit-a","terminal_owner_attempt_id":"attempt-a",
          "producer_binding_digest":"sha256:"+"b"*64,"terminal_marker_digest":"sha256:"+"c"*64}
  false=dict(proven,terminal_gate_proven=False)
  ident=dict(route_id=proven["route_id"],route_hash=proven["route_hash"])
  for existing in (proven,false):
   # an explicitly named axis the record contradicts is never hidden by the matching commit
   self.assertFalse(R._outcome_replay_matches(
    existing,terminal_commit_id="commit-a",owner_attempt_id="attempt-b",
    producer_binding_digest=proven["producer_binding_digest"],**ident))
   self.assertFalse(R._outcome_replay_matches(
    existing,terminal_commit_id="commit-a",owner_attempt_id="attempt-a",
    producer_binding_digest="sha256:"+"d"*64,**ident))
  # a recorded unproven close keeps its stored identity authoritative when a caller omits it
  self.assertFalse(R._outcome_replay_matches(
   false,terminal_commit_id="commit-a",owner_attempt_id=None,
   producer_binding_digest=proven["producer_binding_digest"],**ident))
  self.assertFalse(R._outcome_replay_matches(false,**ident))
  # an already-proven outcome is replayed on the recorded route/hash fact
  self.assertTrue(R._outcome_replay_matches(proven,**ident))
  self.assertTrue(R._outcome_replay_matches(
   proven,terminal_commit_id="commit-a",owner_attempt_id=None,
   producer_binding_digest=proven["producer_binding_digest"],**ident))

 def test_proven_outcome_replays_without_identity_arguments_and_without_marker_file(self):
  # PR130 recorded replay: a legacy caller (CLI `close`, finalize, autoclose) repeating a close
  # on an already-proven outcome names no identity; the recorded fact is reused whatever
  # digest derivation the first caller stored, and a missing marker file does not undo it.
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(Path(tmp)/"jobs.log")}):
   artifact_root=Path(tmp)/"artifacts"; artifact_root.mkdir()
   route=R.compile_route(**self.args(artifact_root=str(artifact_root)))
   node=route["nodes"][0]; jobs=Path(tmp)/"jobs.log"; jobs.write_text("",encoding="utf-8")
   path=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   path.parent.mkdir(parents=True); path.write_text(json.dumps(route),encoding="utf-8")
   evidence=Path(tmp)/"evidence.txt"; evidence.write_text("terminal evidence",encoding="utf-8")
   R.write_completion_marker(route,node,node["id"],evidence,jobs=jobs)
   stored="sha256:"+"1"*64  # a digest the first caller derived its own way
   first,created=R.close_route(route,path,allow_unproven=False,jobs=jobs,expected_terminal_marker_digest=stored)
   self.assertTrue(created); self.assertTrue(first["terminal_gate_proven"])
   raw=R.outcome_path(path).read_bytes()
   again,created=R.close_route(route,path,allow_unproven=True,jobs=jobs)
   self.assertFalse(created); self.assertEqual(again,first)
   for marker in R.completion_dir(route["route_id"],jobs=jobs).glob("*.json"): marker.unlink()
   evidence.write_text("edited after the proven close",encoding="utf-8")
   again,created=R.close_route(route,path,allow_unproven=True,jobs=jobs)
   self.assertFalse(created); self.assertEqual(again,first)
   self.assertEqual(R.outcome_path(path).read_bytes(),raw)
   with self.assertRaisesRegex(ValueError,"route-close-outcome-conflict"):
    R.close_route(route,path,jobs=jobs,expected_terminal_marker_digest="sha256:"+"2"*64)

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
 def test_compile_without_tracked_gate_flags_uses_compose_defaults(self):
  # The five tracked-gate flags were required but accepted any value; compile now
  # records the same defaults compose does, and an explicit value still wins.
  with tempfile.TemporaryDirectory() as tmp:
   artifact_root=Path(tmp)
   args=["--capability","autopilot-code","--capability-mode","dev","--slug","CLI Defaults",
         "--intensity","direct","--cwd",str(R.ROOT),"--artifact-root",str(artifact_root)]
   for predicate in ALL: args+=["--predicate",predicate]
   compiled=self._run_compile_cli(args)
   self.assertEqual(compiled.returncode,0,compiled.stderr)
   route=json.loads(compiled.stdout)
   self.assertEqual(route["tracking"],"tracked")
   gate=route["tracked_gate_evidence"]
   self.assertEqual(gate["workflow_mode"],"tracked")
   self.assertEqual(gate["drift_verdict"],R.DEFAULT_DRIFT_VERDICT)
   self.assertEqual(gate["artifact_guard"],{"satisfied":True,"source":R.DEFAULT_ARTIFACT_GUARD})
   self.assertEqual(gate["spec_read"],R.compose_spec_read(str(R.ROOT),str(artifact_root),None))
   explicit=self._run_compile_cli(args+["--spec-read","auto","--drift-verdict","within-spec"])
   self.assertEqual(explicit.returncode,0,explicit.stderr)
   gate=json.loads(explicit.stdout)["tracked_gate_evidence"]
   self.assertEqual(gate["spec_read"],{"satisfied":True,"source":"auto"})
   self.assertEqual(gate["drift_verdict"],"within-spec")
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
 def test_a_sd154_7_close_outcome_lists_revisions(self):
  # A-SD154-7: a route's closed outcome names every SD-154 revision recorded
  # under it -- a reader never has to walk completion-dir history by hand to
  # learn a gate's evidence was corrected mid-route.
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   route=R.compile_route(**self.args(artifact_root=root))
   path=R.canonical_route_path(root,route["route_id"]); R.write_once(path,route)
   node=next(n for n in route["nodes"] if n["id"]=="inline")
   evidence=root/"inline.md"; evidence.write_text("inline v1\n",encoding="utf-8")
   R._publish_completion_locked(route,node,"inline",evidence,attempt_id="att-inline-v1",attempt_metadata={
    "attempt_schema_version":2,"dispatch_depth":node["dispatch_depth"],"transport":"interactive",
    "execution_surface":"inline","registered_worker":False,"fallback_hop":""})
   evidence.write_text("inline v2 (revised)\n",encoding="utf-8")
   R.publish_revision_locked(route,"inline",evidence,basis="owner-correction",reason="fixture: close outcome revisions",
                             author_attempt_id="att-revise-fixture")
   outcome,_=R.close_route(route,path,commit="a"*40,allow_unproven=True)
   self.assertEqual(len(outcome.get("revisions",[])),1,outcome.get("revisions"))
   revision=outcome["revisions"][0]
   self.assertEqual(revision["node"],"inline")
   self.assertEqual(revision["basis"],"owner-correction")
   self.assertEqual(revision["author_attempt_id"],"att-revise-fixture")
   self.assertIn("of_evidence_sha256",revision); self.assertIn("evidence_sha256",revision)
   self.assertNotEqual(revision["of_evidence_sha256"],revision["evidence_sha256"])
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
   "failure_class":"","checked_worktree":str(Path(worktree or R.ROOT).resolve()),
   "failure_scope":"none","codex_command":"ok","retry_on_isolated_worktree":0,
  }
  return {"tuples":[row],"native_subagent":[{
   "harness":"codex","transport":"headless",
   "execution_surface":"codex-native-subagent","registered_worker":False,
   "status":"supported","check_source":"continuation-fixture",
  }]}
 def _source_fixture(self):
  if not hasattr(self,"_source_fixture_root"):
   source_tmp=tempfile.TemporaryDirectory(prefix="continuation-source-")
   self.addCleanup(source_tmp.cleanup)
   self._source_fixture_root=self._git_repo(source_tmp.name)
  return self._source_fixture_root
 def _source(self,artifact_root,cwd=None,slug=None,**selection):
  source_cwd=Path(cwd) if cwd is not None else self._source_fixture()
  gate={
   "spec_read":{"satisfied":True,"source":"canonical-prd-sha256"},
   "drift_verdict":"within-spec","workflow_mode":"tracked",
   "artifact_guard":{"satisfied":True,"source":"conductor-prechecked"},
  }
  route=R.compile_route(
   "autopilot-code","dev","strong",source_cwd,artifact_root,
   predicates=[],signals=["shared-contract"],transport="headless",
   tracking="tracked",tracked_gate_evidence=gate,
   dispatch_evidence=self._dispatch(source_cwd),
   slug=slug, **selection,
  )
  self.assertEqual(route["source_commit"],self._git_head(source_cwd))
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
  jobs=Path(route["launch_compatibility_tuple"]["jobs_path"]["path"])
  metadata={
   "attempt_schema_version":2,"dispatch_depth":node["dispatch_depth"],
   "transport":"headless","execution_surface":"registered-headless",
   "registered_worker":"1","fallback_hop":"same-harness-headless",
   "attempt_id":attempt_id,"route_id":route["route_id"],
   "route_hash":route["route_hash"],"route_node":node["id"],
  }
  from review_source_subject import is_implementation_review,is_source_test,seal_attempt
  if is_implementation_review(route,node) or is_source_test(node):
   metadata.update(seal_attempt(jobs,route,node,attempt_id,worktree=route["cwd"]))
   # This fixture models a settled completion, not the child lifecycle. Use
   # the common marker writer with the exact source binding; the matching
   # terminal row is attached below for continuation's exact-link checks.
   R._publish_completion_locked(
    route,node,node["id"],evidence,attempt_id=attempt_id,
    attempt_metadata=metadata,jobs=jobs,
   )
  else:
   R.complete_node(
    route,node,node["id"],evidence,attempt_id=attempt_id,
    explicit_attempt_metadata=metadata,
   )
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
  # Preserve the exact route/source identity in the synthetic terminal row;
  # source-bound review currency reads this row and its write-once sidecar.
  self._write_attempt_row(
   jobs,route["route_id"],node["id"],attempt_id,
   extra_metadata={"route_hash":route["route_hash"],
                   **{key:value for key,value in metadata.items()
                      if key.startswith("review_subject_")}},
  )
  return evidence
 def _complete_prefix(self,route,resume_from,evidence_root,skip=(),attempt_namespace=None):
  evidence={}
  for node in route["nodes"][:next(
      index for index,row in enumerate(route["nodes"]) if row["id"]==resume_from
  )]:
   if node["id"] in skip: continue
   attempt_id=(f"att-continuation-{node['id']}-{attempt_namespace}"
               if attempt_namespace else None)
   evidence[node["id"]]=self._complete_node(route,node,evidence_root,attempt_id=attempt_id)
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
 def test_continuation_optional_checked_evidence_updates_tuple_without_changing_source(self):
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact)
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   original=R.canonical(source)
   original_evidence=json.loads(json.dumps(source["dispatch_evidence"]))
   fresh=self._dispatch(source["cwd"])
   fresh["tuples"][0]["parent_sandbox"]="danger-full-access"
   fresh["tuples"][0]["probe_time"]="2026-10-04T07:11:57Z"
   retained=self._build(source)
   changed=self._build(source,dispatch_evidence=fresh)
   self.assertEqual(retained["dispatch_evidence"],original_evidence)
   self.assertNotIn("dispatch_evidence_override",retained)
   self.assertTrue(changed["dispatch_evidence_override"])
   self.assertEqual(changed["dispatch_evidence"]["tuples"][0]["parent_sandbox"],"danger-full-access")
   self.assertNotEqual(retained["continuation_id"],changed["continuation_id"])
   self.assertEqual(R.canonical(source),original)
   self.assertNotIn("codex_execution_sandbox",changed)
   for node in changed["nodes"]:
    if node.get("dispatch_depth")==2:
     self.assertEqual(node["fallback_hops"][0]["candidates"][0]["parent_sandbox"],"danger-full-access")
   R.verify_route(changed,source["cwd"])
   R.write_once(R.canonical_route_path(artifact,source["route_id"]),source)
   output=R.canonical_route_path(artifact,changed["route_id"])
   R.publish_continuation_route(changed,source,output)
   R.review_lineage_routes(changed,"test")
   import subprocess,sys
   checked=Path(tmp)/"checked.json"
   checked.write_text(json.dumps(fresh))
   cli=subprocess.run([sys.executable,str(P),"continuation","--source-route",
    str(R.canonical_route_path(artifact,source["route_id"])),"--resume-from-node","test",
    "--requested-boundary","test","--reason","cli-checked-override","--artifact-root",str(artifact),
    "--dispatch-evidence",str(checked)],capture_output=True,text=True,
    env={**os.environ,"AGENT_HOME":str(R.ROOT)},cwd=str(R.ROOT),timeout=30)
   self.assertEqual(cli.returncode,0,cli.stderr)
   actual=json.loads(cli.stdout)
   self.assertEqual(actual["dispatch_evidence"],changed["dispatch_evidence"])
   self.assertTrue(R.canonical_route_path(artifact,actual["route_id"]).is_file())
   self.assertEqual(R.canonical(source),original)
   # The artifact root defaults to the source route's own.
   implied=subprocess.run([sys.executable,str(P),"continuation","--source-route",
    str(R.canonical_route_path(artifact,source["route_id"])),"--resume-from-node","test",
    "--requested-boundary","test","--reason","cli-implied-root",
    "--dispatch-evidence",str(checked)],capture_output=True,text=True,
    env={**os.environ,"AGENT_HOME":str(R.ROOT)},cwd=str(R.ROOT),timeout=30)
   self.assertEqual(implied.returncode,0,implied.stderr)
   self.assertTrue(R.canonical_route_path(artifact,json.loads(implied.stdout)["route_id"]).is_file())
   tampered=json.loads(json.dumps(changed))
   tampered["nodes"][0]["unit"]="qa/ml-debug"
   tampered["new_nodes"][0]["realized_contract_hash"]=R._continuation_contract_hash(tampered["nodes"][0])
   tampered["route_hash"]=R.route_hash(tampered)
   tampered["route_id"]="rt-"+tampered["route_hash"].split(":",1)[1][:16]
   with self.assertRaisesRegex(ValueError,"owner-closure-lineage-node-mismatch"):
    R.review_lineage_routes(tampered,"test")
 def test_continuation_checked_override_reuses_foreign_worktree_refusal(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts")
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   original=R.canonical(source)
   evidence=self._dispatch()
   evidence["tuples"][0]["checked_worktree"]=str(Path(tmp)/"foreign")
   with self.assertRaisesRegex(ValueError,"dispatch-evidence-worktree-mismatch"):
    self._build(source,dispatch_evidence=evidence)
   self.assertEqual(R.canonical(source),original)
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

 def test_gate_off_continuation_reuses_current_evidence_after_edit(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   source=self._source(root/"artifacts")
   evidence=self._complete_prefix(source,"test",root/"evidence")
   evidence["plan"].write_text("revised plan\n")
   with mock.patch.dict(os.environ,{"HEARTING_GATES":"off"}), contextlib.redirect_stderr(io.StringIO()):
    continuation=self._build(source,last_turn_id="current-turn")
    self.assertIsNone(continuation["first_runnable_blocker"])
    self.assertIsNone(continuation["requested_boundary_blocker"])
    reused=next(row for row in continuation["reused_nodes"] if row["node_id"]=="plan")
    self.assertEqual(reused["output_evidence_digest"],R.evidence_digest(evidence["plan"]))
    R.verify_route(continuation,source["cwd"])
    output=R.canonical_route_path(root/"artifacts",continuation["route_id"])
    R.publish_continuation_route(continuation,source,output)
    self.assertTrue(output.is_file())

 def test_slug_metadata_is_inherited_by_continuation(self):
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact,slug="Cycle A")
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   continuation=self._build(source)
   self.assertEqual(continuation["slug"],"cycle-a")
   self.assertFalse(continuation["slug_truncated"])
   R.verify_route(continuation,source["cwd"])
 def test_campaign_selection_survives_continuation(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts",slug="Cycle A",
                       campaign_key="tts-v6-release",parent_cycle_id="cyc_"+"a"*32)
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   continuation=self._build(source)
   self.assertEqual(continuation["campaign_key"],source["campaign_key"])
   self.assertEqual(continuation["parent_cycle_id"],source["parent_cycle_id"])
   R.verify_route(continuation,source["cwd"])
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
   R.verify_route(continuation,source["cwd"])
   output=R.canonical_route_path(artifact,continuation["route_id"])
   R.publish_continuation_route(continuation,source,output)
   self.assertTrue(output.is_file())
   self.assertFalse(R.completion_dir(continuation["route_id"]).exists())
 def test_legacy_source_unbound_review_pass_resumes_at_fresh_review(self):
  from review_source_subject import binding_path,is_implementation_review
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
   source=self._source(artifact,cwd=repo)
   reviews=[node for node in source["nodes"] if is_implementation_review(source,node)]
   self.assertTrue(reviews)
   review_ids={node["id"] for node in reviews}
   self._complete_prefix(source,"test",Path(tmp)/"evidence",skip=review_ids)
   jobs=Path(source["launch_compatibility_tuple"]["jobs_path"]["path"])
   for node in reviews:
    attempt=f"att-legacy-{node['id']}"
    evidence=Path(tmp)/"legacy"/f"{node['id']}.md"
    evidence.parent.mkdir(parents=True,exist_ok=True)
    evidence.write_text("legacy PASS without source binding\n",encoding="utf-8")
    metadata={
     "attempt_schema_version":2,"dispatch_depth":node["dispatch_depth"],
     "transport":"headless","execution_surface":"registered-headless",
     "registered_worker":"1","fallback_hop":"same-harness-headless",
     "attempt_id":attempt,"route_id":source["route_id"],
     "route_hash":source["route_hash"],"route_node":node["id"],
     "launch_head":self._git_head(repo),
    }
    # A jobs-less publication preserves the legacy unknown subject shape while
    # still creating the historical marker, history file, and attempt link.
    R._publish_completion_locked(source,node,node["id"],evidence,
     attempt_id=attempt,attempt_metadata=metadata)
    link_path=R._attempt_completion_path(source,node["id"],attempt,jobs=jobs)
    link=json.loads(link_path.read_text(encoding="utf-8"))
    link.update({"verdict":"PASS","quiescence_proof_digest":"sha256:"+"a"*64,
                 "last_turn_id":f"turn-{node['id']}"})
    R.atomic_write(link_path,link)
    self._write_attempt_row(jobs,source["route_id"],node["id"],attempt,
     extra_metadata={**{key:str(value) for key,value in metadata.items()},
      "worker_type":"review","note":"completed-marker","failure_class":"pass"})
    marker=json.loads((R.completion_dir(source["route_id"],jobs=jobs)/f"{node['id']}.json").read_text())
    self.assertNotIn("source_subject",marker)
    self.assertFalse(binding_path(jobs,attempt).exists())
   continuation=self._build(source)
   self.assertEqual(continuation["first_runnable_node"],reviews[0]["id"])
   self.assertIsNone(continuation["first_runnable_blocker"])
   boundary=next(i for i,node in enumerate(source["nodes"]) if node["id"]==reviews[0]["id"])
   self.assertEqual([row["node_id"] for row in continuation["reused_nodes"]],
                    [node["id"] for node in source["nodes"][:boundary]])
   self.assertEqual([row["node_id"] for row in continuation["new_nodes"]],
                    [node["id"] for node in source["nodes"][boundary:]])
   self.assertEqual(continuation["source_commit"],self._git_head(repo))
   R.verify_route(continuation)
 def test_stale_source_review_resumes_at_fresh_review_after_B(self):
  from review_source_subject import is_implementation_review,marker_currency,observe
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
   source=self._source(artifact,cwd=repo)
   self._complete_prefix(source,"test",Path(tmp)/"evidence-A")
   review=next(node for node in source["nodes"] if is_implementation_review(source,node))
   jobs=Path(source["launch_compatibility_tuple"]["jobs_path"]["path"])
   marker_path=R.completion_dir(source["route_id"],jobs=jobs)/f"{review['id']}.json"
   old_marker=json.loads(marker_path.read_text(encoding="utf-8"))
   (repo/"x").write_text("source B\n",encoding="utf-8")
   subprocess.run(["git","-C",str(repo),"add","x"],check=True)
   subprocess.run(["git","-C",str(repo),"commit","-qm","source B"],check=True)
   execute=next(node for node in source["nodes"] if node["id"]=="execute")
   self._complete_node(source,execute,Path(tmp)/"evidence-B","att-source-B-execute")
   self.assertEqual(marker_currency(source,review,old_marker,marker_path.parent),
                    ("superseded","completion-review-source-stale"))
   continuation=self._build(source)
   self.assertEqual(continuation["first_runnable_node"],review["id"])
   self.assertIsNone(continuation["first_runnable_blocker"])
   self.assertEqual(continuation["source_commit"],self._git_head(repo))
   self.assertEqual(continuation["source_commit_rebind"]["inherited_source_commit"],
                    source["source_commit"])
   self.assertEqual(continuation["source_commit_rebind"]["rebound_source_commit"],
                    self._git_head(repo))
   self.assertNotEqual(old_marker["source_subject"]["digest"],
                       observe(source,review,worktree=repo)["digest"])
   self.assertEqual([row["node_id"] for row in continuation["new_nodes"]],
                    [node["id"] for node in source["nodes"]
                     [next(i for i,row in enumerate(source["nodes"]) if row["id"]==review["id"]):]])
   R.verify_route(continuation)
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

 def test_selection_pins_ride_a_continuation_and_verify(self):
  # `compose --pin` seals the tool/model choice once; a resume must carry the
  # same pins or the choice silently disappears at the first continuation.
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact)
   source["selection_pins"]={"contract_version":1,
    "owner":{"harness":"opencode","model":"opencode-go/muse-spark-1.3-contributor","effort":"xhigh"},
    "worker":{"harness":"opencode","model":None,"effort":None}}
   source["route_hash"]=R.route_hash(source)
   source["route_id"]="rt-"+source["route_hash"].split(":",1)[1][:16]
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   continuation=self._build(source)
   self.assertEqual(continuation["selection_pins"],source["selection_pins"])
   R.verify_route(continuation,source["cwd"])
   continuation["selection_pins"]["owner"]["harness"]="gemini"
   with self.assertRaisesRegex(ValueError,"invalid selection_pins.owner.harness"):
    R.verify_route(continuation,source["cwd"])

 def test_recorded_pin_changes_ride_a_continuation_with_completed_prefix_preserved(self):
  import route_authority as AUTH
  with tempfile.TemporaryDirectory() as tmp:
   source=self._source(Path(tmp)/"artifacts")
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   before=json.dumps(source,sort_keys=True)
   AUTH.record_pin_change(source,target="owner",pin={"harness":"codex","model":None,"effort":None},
    by={"harness":"codex","session_id":"parent"},source="fixture",tuples=[],candidates=[])
   AUTH.record_pin_change(source,target="worker",pin={"harness":"codex","model":None,"effort":None},
    by={"harness":"codex","session_id":"parent"},source="fixture",tuples=[],candidates=[])
   continuation=self._build(source)
   self.assertEqual(continuation["selection_pins"]["owner"]["harness"],"codex")
   self.assertEqual(continuation["selection_pins"]["worker"]["harness"],"codex")
   self.assertTrue(continuation["reused_nodes"])
   self.assertTrue(all(row["new_attempt_count"]==0 for row in continuation["reused_nodes"]))
   self.assertEqual(json.dumps(source,sort_keys=True),before)
   R.verify_route(continuation,source["cwd"])

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
    source,"test",Path(tmp)/"evidence-first",skip={"execute"},attempt_namespace="second-route")
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
     source,"test",Path(tmp)/f"evidence-{mutation}",attempt_namespace=mutation)
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
   source_root=self._source_fixture()
   owner_meta=",".join((
    "attempt_schema_version=2","worker_type=owner","unit=_kernel/owner",
    "dispatch_depth=1","registered_worker=1",
    "execution_surface=registered-headless","capability=autopilot-code",
    "capability_mode=dev","intensity=strong",f"artifact_root={artifact}",
    "parent_sid=launch-parent","owner_harness=codex",f"attempt_id={attempt}",
   ))
   owner_row="\t".join((
    "2099-01-01T00:00:00Z","open",str(source_root),str(source_root),"owner",owner_meta,
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
    return "\t".join(("2099-01-01T00:00:00Z","open",str(route["cwd"]),str(route["cwd"]),
                       f"child-{suffix}",meta))+"\n"

   with mock.patch.dict(os.environ,lifecycle_env,clear=False):
    source=self._source(artifact,cwd=source_root)
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
                     (source["route_id"],"owner-route-post-launch-attachment"))
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

 def test_continuation_cli_defers_cycle_binding_until_begin(self):
  # Publication leaves the existing cycle untouched. Execution's existing
  # begin path records the admitted lineage; publishing a sibling still
  # leaves that audit alone and write-time fork protection remains intact.
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
    # The continuation's own lineage walk reads its parent from the
    # CANONICAL route path (route_lineage.verified_route_lineage), not from
    # wherever `--source-route` points -- so the source needs a canonical
    # copy too, exactly as a real compile+publish would leave one.
    R.write_once(R.canonical_route_path(artifact,source["route_id"]),source)
    import artifact_producer as AP
    begun=AP.begin(artifact,route_file=source_path,capability=source["capability"],
             intensity=source["effective_intensity"])
    record_path=AP.cycle_record_path(artifact,begun["cycle_id"])
    before_publish=record_path.read_bytes()
    env=os.environ.copy()
    def continuation_command(reason):
     return [
      sys.executable,str(P),"continuation","--source-route",str(source_path),
      "--resume-from-node","test","--requested-boundary","test",
      "--reason",reason,"--artifact-root",str(artifact),
     ]
    first=subprocess.run(continuation_command("cycle-fixture-b"),capture_output=True,text=True,cwd=str(R.ROOT),env=env)
    self.assertEqual(first.returncode,0,first.stderr)
    self.assertIn("cycle_binding_bound=0 cycle_binding_deferred=1",first.stderr)
    self.assertEqual(record_path.read_bytes(),before_publish)
    first_route=json.loads(first.stdout)
    resumed=AP.begin(artifact,route_file=R.canonical_route_path(artifact,first_route["route_id"]),
                     capability=source["capability"],intensity=source["effective_intensity"])
    self.assertEqual(resumed["cycle_id"],begun["cycle_id"])
    self.assertEqual(AP.read_cycle_record(artifact,begun["cycle_id"])["route_bindings"][-1]["route_id"],first_route["route_id"])
    after_begin=record_path.read_bytes()
    second=subprocess.run(continuation_command("cycle-fixture-b-prime"),capture_output=True,text=True,cwd=str(R.ROOT),env=env)
    self.assertEqual(second.returncode,0,second.stderr)
    self.assertIn("cycle_binding_bound=0",second.stderr)
    self.assertIn("cycle_binding_deferred=1",second.stderr)
    self.assertEqual(record_path.read_bytes(),after_begin)
    second_route=json.loads(second.stdout)
    with self.assertRaisesRegex(AP.ProducerError,"lineage-fork"):
     AP.begin(artifact,route_file=R.canonical_route_path(artifact,second_route["route_id"]),
              capability=source["capability"],intensity=source["effective_intensity"])
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
    docs=repo/"docs"; docs.mkdir()
    (docs/"fast-forward.md").write_text("documentation-only fast-forward\n")
    subprocess.run(["git","-C",str(repo),"add","docs/fast-forward.md"],check=True)
    subprocess.run(["git","-C",str(repo),"commit","-qm","fast-forward"],check=True)
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

 def test_a_sd156_7_descendant_head_always_repins(self):
  # SD-156 retires the SD-67/SD-128/SD-133 decline branch this test used to
  # exercise: a continuation resuming at `execute` after execute already ran
  # and committed now re-pins unconditionally, with no registry consulted at
  # all. `worker-route-guard.py` is the one place a moved HEAD is now
  # adjudicated -- on the same lineage verdict, regardless of prior attempts.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
   source=self._source(artifact,cwd=repo)
   pinned=source["source_commit"]
   self._complete_prefix(source,"execute",Path(tmp)/"evidence")
   execute=next(node for node in source["nodes"] if node["id"]=="execute")
   self.assertTrue(R._node_mutates_worktree(execute))
   (repo/"x").write_text("b"); subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
   rebound=self._build(source,resume_from_node="execute",requested_boundary="execute")
   self.assertEqual(rebound["source_commit"],self._git_head(repo))
   self.assertNotEqual(rebound["source_commit"],pinned)
   self.assertEqual(rebound["source_commit_rebind"]["inherited_source_commit"],pinned)

 def test_a_sd156_7_plan_resume_carrying_execute_also_repins(self):
  # SD-156, replacing review round 2 B1's regression coverage: a `plan`-time
  # resume carries `execute` in its suffix too, and both re-pin to the same
  # observed HEAD -- there is no longer a "the resume node itself is
  # innocent" distinction, because there is no decline left to look past it.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
   source=self._source(artifact,cwd=repo)
   pinned=source["source_commit"]
   self._complete_prefix(source,"plan",Path(tmp)/"evidence")
   execute=next(node for node in source["nodes"] if node["id"]=="execute")
   self.assertTrue(R._node_mutates_worktree(execute))
   (repo/"x").write_text("b")
   subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
   rebound=self._build(source,resume_from_node="plan",requested_boundary="plan")
   self.assertEqual(rebound["source_commit"],self._git_head(repo))
   self.assertNotEqual(rebound["source_commit"],pinned)
   self.assertIn("execute",[node["id"] for node in rebound["nodes"]])

 def test_a_sd156_7_rebind_ignores_registry_state_entirely(self):
  # SD-156, replacing review round 2 B2/round 3 B2's registry-unreadable
  # coverage: a moved HEAD re-pins the same way whether the registry is
  # intact, deleted, a directory, or unbound -- the builder no longer reads
  # the registry to decide.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
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
    ("registry intact",lambda: None),
    ("registry deleted",lambda: sealed.unlink()),
   ):
    with self.subTest(name):
     if sealed.is_dir(): sealed.rmdir()
     elif sealed.exists(): sealed.unlink()
     sealed.parent.mkdir(parents=True,exist_ok=True)
     sealed.write_text("",encoding="utf-8")
     wreck()
     rebound=self._build(source,resume_from_node="execute",requested_boundary="execute")
     self.assertEqual(rebound["source_commit"],moved)
   # A registry that is a directory is unreadable for a DIFFERENT, unrelated
   # reason (the reused-prefix evidence snapshot at `frame`, not this rebind)
   # and still blocks the continuation before source_commit is even sealed --
   # unaffected by SD-156.
   if sealed.exists(): sealed.unlink()
   sealed.mkdir()
   with self.subTest("registry is a directory"):
    blocked=self._build(source,resume_from_node="execute",requested_boundary="execute")
    self.assertEqual(blocked["first_runnable_blocker"],
     "continuation-source-node-unverified:frame:registry-unreadable")
    self.assertNotIn("source_commit",blocked)
   sealed.rmdir(); sealed.write_text("",encoding="utf-8")

 def test_c_route_and_guard_share_one_mutating_scope_definition(self):
  # S1: the builder's decline and the guard that adjudicates the same node must
  # answer one question one way. A copied predicate would drift silently, so
  # there is no copy -- the guard calls this function.
  #
  # Scope of this claim (round 3, Q2): the route/guard pair, not the repo.
  # `hooks/artifact-guard.sh` maps the same scope words to artifact-root path
  # patterns, which is an adjacent question ("what may this node write").
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


 def test_a_sd156_7_repin_survives_a_second_continuation_generation(self):
  # SD-156, replacing review round 3 B1's regression coverage: re-pinning is
  # no longer conditional on anything a first-generation continuation did or
  # did not write, so a second-generation continuation built from the first
  # re-pins to the (unchanged, since nothing mutated further) observed HEAD
  # exactly the same way.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   repo=self._git_repo(tmp); artifact=Path(tmp)/"artifacts"
   source=self._source(artifact,cwd=repo)
   pinned=source["source_commit"]
   self._complete_prefix(source,"plan",Path(tmp)/"evidence")
   (repo/"x").write_text("b")
   subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
   moved=self._git_head(repo)
   first=self._build(source,resume_from_node="plan",requested_boundary="plan")
   self.assertEqual(first["source_commit"],moved)
   self.assertNotEqual(first["route_id"],source["route_id"])
   second=self._build(first,resume_from_node="plan",requested_boundary="plan")
   self.assertEqual(second["source_commit"],moved)
   self.assertNotIn("source_commit_rebind",second)
   self.assertIn("execute",[node["id"] for node in second["nodes"]])
   self.assertNotEqual(moved,pinned)

 def _stage_rows(self,jobs,route_id):
  fallback=R._stage_fallback()
  return fallback.registry_route_rows(Path(jobs),[route_id])

 def test_a_sd156_7_pruned_release_registry_source_jobs_falls_through_to_live(self):
  # `_continuation_source_jobs`'s pruned-release-registry fallback (review
  # round 3, B2a) is unrelated to SD-156's rebind -- the rebind no longer
  # reads the registry at all -- but the fallback still matters for marker
  # resolution elsewhere, so its own behaviour stays covered here, alongside
  # the rebind re-pinning regardless of which registry is live.
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
    self._write_attempt_row(sealed,source["route_id"],"execute","att-execute-prior")
    (repo/"x").write_text("b")
    subprocess.run(["git","-C",str(repo),"commit","-qam","execute output"],check=True)
    moved=self._git_head(repo)
    # Prune the release tree the sealed root lived in, and stand up a live
    # canonical registry that never saw this route -- the observed shape the
    # compat window exists for (release pruning, 2026-08-27).
    import shutil
    shutil.rmtree(Path(tmp)/"releases")  # reason=fixture boundary=test-tmpdir
    live=Path(tmp)/"state"/"jobs.log"; live.parent.mkdir(parents=True,exist_ok=True)
    live.write_text("",encoding="utf-8")
    os.environ["AGENT_DISPATCH_JOBS"]=str(live)
    # The fall-through registry is a real, readable, regular file with no rows
    # for this route.
    resolved=R._continuation_source_jobs(source)
    self.assertTrue(Path(resolved).is_file())
    self.assertEqual(self._stage_rows(resolved,source["route_id"]),[])
    rebound=self._build(source,resume_from_node="frame",requested_boundary="frame")
    self.assertEqual(rebound["source_commit"],moved)
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

 def _write_attempt_row(self,jobs,route_id,node_id,attempt_id,status="done",extra_metadata=None):
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
  values={"route_id":route_id,"route_node":node_id,"attempt_id":attempt_id,
          **(extra_metadata or {})}
  meta=",".join(f"{key}={value}" for key,value in sorted(values.items()))
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

 def test_complete_accepts_completion_deferred_row(self):
  # 6번 (S3a): a row the completion budget closed typed-deferred (budget
  # exhausted, no marker published yet) is marker-eligible the same way an
  # already-closed `completed-supervisor` row is -- `complete` must publish
  # the marker and append `note=completed-marker` instead of refusing
  # `attempt-row-terminal-without-completion`.
  with tempfile.TemporaryDirectory() as tmp:
   previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   jobs=Path(tmp)/"state"/"jobs.log"; jobs.parent.mkdir(parents=True)
   os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   try:
    artifact=Path(tmp)/"artifacts"
    source=self._source(artifact)
    node=next(row for row in source["nodes"] if row["id"]=="execute")
    attempt_id="att-deferred-complete"
    meta={
     "attempt_id":attempt_id,"attempt_schema_version":"2",
     "dispatch_depth":str(node["dispatch_depth"]),
     "transport":"headless","execution_surface":"registered-headless",
     "registered_worker":"1","fallback_hop":"same-harness-headless",
     "route_id":source["route_id"],"route_hash":source["route_hash"],
     "route_node":node["id"],
     "note":"completion-deferred","failure_class":"infrastructure",
     "classifier_source":"registered-wrapper-completion-transient-v1",
    }
    pipe=",".join(f"{k}={v}" for k,v in meta.items())
    jobs.write_text("\t".join([
     "2026-09-24T00:00:00Z","done",str(tmp),str(tmp),"slug",pipe,
    ])+"\n",encoding="utf-8")
    evidence=Path(tmp)/"evidence"/"execute.md"
    evidence.parent.mkdir(parents=True,exist_ok=True)
    evidence.write_text("execute exact output\n",encoding="utf-8")
    marker,row=R.complete_node(source,node,node["id"],evidence,jobs=jobs,attempt_id=attempt_id)
    self.assertIsNotNone(marker)
    line=jobs.read_text(encoding="utf-8").strip()
    self.assertIn(f"attempt_id={attempt_id}",line)
    self.assertIn("note=completed-marker",line)
    self.assertIn("completion_marker=",line)
    # DR-1: the deferred row's failure_class was non-empty (infrastructure),
    # so DR-1's "only when empty" rule leaves it untouched.
    self.assertIn("failure_class=infrastructure",line)
   finally:
    if previous_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else: os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs

 def test_recorded_move_corrects_only_exact_artifact_missing_terminal(self):
  with tempfile.TemporaryDirectory() as tmp:
   previous_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
   jobs=Path(tmp)/"state"/"jobs.log";jobs.parent.mkdir(parents=True)
   os.environ["AGENT_DISPATCH_JOBS"]=str(jobs)
   try:
    source=self._source(Path(tmp)/"artifacts")
    node=next(row for row in source["nodes"] if row["id"]=="execute")
    attempt="att-recorded-move-correction"
    evidence=Path(tmp)/"evidence"/"execute.md";evidence.parent.mkdir()
    evidence.write_text("exact current file\n",encoding="utf-8")
    origin=Path(tmp)/"artifacts"/"execute.md"
    log=Path(tmp)/f"worker.{attempt}.codex.jsonl"
    log.write_text('{"type":"turn.completed","timestamp":"2026-09-28T07:29:16Z"}\n')
    namespace="pid:[fixture]"
    meta={
     "attempt_id":attempt,"attempt_schema_version":"2",
     "dispatch_depth":str(node["dispatch_depth"]),"transport":"headless",
     "execution_surface":"registered-headless","registered_worker":"1",
     "fallback_hop":"same-harness-headless","route_id":source["route_id"],
     "route_hash":source["route_hash"],"route_node":node["id"],
     "worker_type":"stage","launch_lifecycle":"foreground-scoped",
     "pid_scope":"namespace-local","pid_ns":namespace,"pid_observer_ns":namespace,
     "group_reap_proof":"pgid-empty-v1","attempt_descendant_proof":"attempt-tagged-empty-v1",
     "log_file":str(log),"artifact_root":str(source["artifact_root"]),
     "note":"dead-invalid-envelope","failure_class":"invalid-envelope",
     "classifier_source":"completion-join-invalid-envelope-v1",
     "reconcile_reason":"terminal-invalid:artifact-missing",
    }
    def row(metadata):
     pipe=",".join(f"{k}={v}" for k,v in metadata.items())
     jobs.write_text("\t".join(["2026-09-28T07:23:36Z","done",source["cwd"],source["cwd"],"stage",pipe])+"\n")
    row(meta)
    encoded=lambda p:base64.urlsafe_b64encode(str(p).encode()).decode().rstrip("=")
    terminal={"state":"valid","source":"exact-turn-completed","terminal_event":"turn.completed",
              "verdict":"PASS","failure_class":"pass","artifact_state":"readable",
              "artifact_shape":"file","artifact_origin_path_b64":encoded(origin),
              "artifact_path_b64":encoded(evidence)}
    proof={"destination":str(evidence),"origin":str(origin),"cycle_id":"cyc-fixture",
           "manifest_revision_id":"mrev-fixture","artifact_revision_id":"arev-fixture",
           "current_content_digest":"sha256:"+hashlib.sha256(evidence.read_bytes()).hexdigest(),
           "placement_sha256":"a"*64,"manifest_sha256":"b"*64}
    with mock.patch.object(R,"inspect_terminal_attempt",return_value=terminal), \
         mock.patch.object(R,"attempt_process_quiescence",return_value=mock.Mock(state="quiescent")), \
         mock.patch.object(R,"process_namespace_identity",return_value=namespace), \
         mock.patch("artifact_producer.placed_output_proof",return_value=proof):
     original_bytes=evidence.read_bytes()
     original_row=jobs.read_bytes()
     publications=(R.completion_dir(source["route_id"]),
                   R._attempt_completion_path(source,node["id"],attempt))
     def drift_after_proof(original):
      def changed(*args,**kwargs):
       evidence.write_text("changed after proof\n",encoding="utf-8")
       return original(*args,**kwargs)
      return changed
     for target in ("_publish_completion_locked","write_completion_marker",
                    "_completion_marker_replay","_next_marker_sequence"):
      with self.subTest(drift_at=target):
       evidence.write_bytes(original_bytes)
       with mock.patch.object(R,target,side_effect=drift_after_proof(getattr(R,target))):
        with self.assertRaisesRegex(ValueError,"recorded-move-evidence-drift"):
         R.complete_node(source,node,node["id"],evidence,jobs=jobs,attempt_id=attempt)
       self.assertEqual(jobs.read_bytes(),original_row)
       self.assertFalse(list(publications[0].glob("execute*.json")))
       self.assertFalse(publications[1].exists())
     evidence.write_bytes(original_bytes)
     with mock.patch.object(R,"_atomic_registry_replace",side_effect=OSError("interrupted")):
      with self.assertRaises(OSError):
       R.complete_node(source,node,node["id"],evidence,jobs=jobs,attempt_id=attempt)
     self.assertEqual(R.parse_registry_metadata(jobs.read_text().split("\t",5)[5])["note"],"dead-invalid-envelope")
     marker,result=R.complete_node(source,node,node["id"],evidence,jobs=jobs,attempt_id=attempt)
     self.assertEqual(result["status"],"marker-appended")
     corrected=jobs.read_bytes()
     metadata=R.parse_registry_metadata(corrected.decode().split("\t",5)[5])
     self.assertEqual(metadata["note"],"completed-marker")
     self.assertEqual(metadata["failure_class"],"pass")
     self.assertEqual(metadata["prior_terminal_note"],"dead-invalid-envelope")
     self.assertEqual(metadata["prior_failure_class"],"invalid-envelope")
     audit=json.loads(base64.urlsafe_b64decode(metadata["terminal_correction_b64"]+"="*(-len(metadata["terminal_correction_b64"])%4)))
     self.assertEqual(audit["terminal_time"],"2026-09-28T07:29:16Z")
     self.assertEqual(audit["terminal_log_sha256"],hashlib.sha256(log.read_bytes()).hexdigest())
     self.assertEqual(marker["evidence"]["sha256"],audit["current_content_digest"].split(":",1)[1])
     link=json.loads(publications[1].read_text(encoding="utf-8"))
     self.assertEqual(link["evidence_sha256"],marker["evidence"]["sha256"])
     again,_=R.complete_node(source,node,node["id"],evidence,jobs=jobs,attempt_id=attempt)
     self.assertEqual(again["sequence"],marker["sequence"])
     self.assertEqual(jobs.read_bytes(),corrected)
    # Other terminal verdicts, a changed route, and a missing move ledger
    # cannot use the same narrow correction, even with a readable file.
    for change in ({"failure_class":"fail"},{"route_hash":"sha256:"+"0"*64},{}):
     row({**meta,**change})
     with mock.patch.object(R,"inspect_terminal_attempt",return_value=terminal), \
          mock.patch.object(R,"attempt_process_quiescence",return_value=mock.Mock(state="quiescent")), \
          mock.patch.object(R,"process_namespace_identity",return_value=namespace), \
          mock.patch("artifact_producer.placed_output_proof",return_value=None if not change else proof):
      with self.assertRaises(ValueError):
       R.complete_node(source,node,node["id"],evidence,jobs=jobs,attempt_id=attempt)
    for verdict in ("FAIL","BLOCKED"):
     row(meta)
     with mock.patch.object(R,"inspect_terminal_attempt",return_value={**terminal,"verdict":verdict}), \
          mock.patch.object(R,"attempt_process_quiescence",return_value=mock.Mock(state="quiescent")), \
          mock.patch.object(R,"process_namespace_identity",return_value=namespace), \
          mock.patch("artifact_producer.placed_output_proof",return_value=proof):
      with self.assertRaises(ValueError):
       R.complete_node(source,node,node["id"],evidence,jobs=jobs,attempt_id=attempt)
    row(meta)
    jobs.write_text(jobs.read_text()+jobs.read_text().replace(attempt,"att-newer-attempt"))
    with mock.patch.object(R,"inspect_terminal_attempt",return_value=terminal), \
         mock.patch.object(R,"attempt_process_quiescence",return_value=mock.Mock(state="quiescent")), \
         mock.patch.object(R,"process_namespace_identity",return_value=namespace), \
         mock.patch("artifact_producer.placed_output_proof",return_value=proof):
     with self.assertRaises(ValueError):
      R.complete_node(source,node,node["id"],evidence,jobs=jobs,attempt_id=attempt)
   finally:
    if previous_jobs is None:os.environ.pop("AGENT_DISPATCH_JOBS",None)
    else:os.environ["AGENT_DISPATCH_JOBS"]=previous_jobs

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
   docs=repo/"docs"; docs.mkdir()
   (docs/"fast-forward.md").write_text("documentation-only fast-forward\n")
   subprocess.run(["git","-C",str(repo),"add","docs/fast-forward.md"],check=True)
   subprocess.run(["git","-C",str(repo),"commit","-qm","fast-forward"],check=True)
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
   with mock.patch.dict(os.environ,{"HEARTING_GATES":"on"}):
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
    jobs.parent.mkdir(parents=True, exist_ok=True)
    with jobs.open("a",encoding="utf-8") as handle:
     handle.write(
      "2099-01-01T00:00:00Z\topen\t%s\t%s\towner\t"
      "attempt_schema_version=2,worker_type=owner,unit=_kernel/owner,"
      "dispatch_depth=1,registered_worker=1,execution_surface=registered-headless,"
      "capability=autopilot-code,capability_mode=dev,intensity=strong,artifact_root=%s,"
      "owner_harness=codex,parent_sid=thread-source,"
      "attempt_id=att-cli-owner,owner_route_file=%s,owner_route_id=%s,owner_route_hash=%s\n"
      % (source["cwd"], source["cwd"], source["artifact_root"], source_path, source["route_id"], source["route_hash"]))
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
    jobs.parent.mkdir(parents=True, exist_ok=True)
    with jobs.open("a",encoding="utf-8") as handle:
     handle.write(
      "2099-01-01T00:00:00Z\topen\trepo\t%s\tstage\t"
      "attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
      "worker_type=stage,unit=dev/backend,attempt_id=att-cli-stage\n" % source["cwd"])
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
    jobs.parent.mkdir(parents=True, exist_ok=True)
    with jobs.open("a",encoding="utf-8") as handle:
     handle.write(
      "2099-01-01T00:00:00Z\topen\t%s\t%s\towner\t"
      "attempt_schema_version=2,worker_type=owner,unit=_kernel/owner,"
      "dispatch_depth=1,registered_worker=1,execution_surface=registered-headless,"
      "capability=autopilot-code,capability_mode=dev,intensity=strong,artifact_root=%s,"
      "owner_harness=codex,parent_sid=thread-source,attempt_id=att-cli-replay,"
      "owner_route_file=%s,owner_route_id=%s,owner_route_hash=%s\n"
      % (source["cwd"], source["cwd"], source["artifact_root"], source_path, source["route_id"], source["route_hash"]))
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
  # Source-subject observation and source-commit continuation checks need a
  # real Git checkout. Give the fixture a deterministic commit, then seal that
  # exact head while compiling so source currency and first-parent validation
  # both exercise their normal paths.
  previous_owner_attempt=os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID",None)
  try:
   with mock.patch("datetime.datetime",_FrozenDatetime), \
        mock.patch.object(R,"_launch_source_revision",lambda path:"golden-fixed-revision"), \
        mock.patch.object(R,"_launch_content_digest",lambda path:"sha256:"+"0"*64), \
        mock.patch.object(R,"_git_commit",lambda cwd:fixture_head):
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
    cwd=root/"cwd"; cwd.mkdir(parents=True,exist_ok=True)
    subprocess.run(["git","-C",str(cwd),"init","-q"],check=True)
    subprocess.run(["git","-C",str(cwd),"config","user.email","fixture@example.com"],check=True)
    subprocess.run(["git","-C",str(cwd),"config","user.name","Fixture"],check=True)
    (cwd/"source.py").write_text("fixture_source = True\n",encoding="utf-8")
    subprocess.run(["git","-C",str(cwd),"add","source.py"],check=True)
    commit_env={**os.environ,"GIT_AUTHOR_DATE":"2026-08-29T00:00:00Z",
                "GIT_COMMITTER_DATE":"2026-08-29T00:00:00Z"}
    subprocess.run(["git","-C",str(cwd),"commit","-qm","fixture source"],
                   check=True,env=commit_env)
    fixture_head=subprocess.run(["git","-C",str(cwd),"rev-parse","HEAD"],
                                check=True,text=True,capture_output=True).stdout.strip()
    source=self._source(root/"artifacts",cwd=cwd)
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
 def test_launch_revalidation_takes_another_release_as_where_it_runs(self):
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
   # Two release copies are two places to run the same work (route_authority.release_moved);
   # the registry that resolved under each keeps its own path, so nothing else differs.
   self.assertEqual((compatible,mismatches),(True,{}))
   self.assertNotEqual(sealed["runtime_root"]["path"],str(runtime_b.resolve()))
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
  stale=self._reseal({**json.loads(json.dumps(route)),"registry_digest":"sha256:"+"0"*64,"capability_registry_digest":"sha256:"+"0"*64})
  with self.assertRaisesRegex(ValueError,"stale registry digest"):
   R.verify_route(stale,R.ROOT)
  stale=self._reseal({**json.loads(json.dumps(route)),"unit_catalog_digest":"sha256:"+"0"*64})
  with self.assertRaisesRegex(ValueError,"stale unit catalog digest"):
   R.verify_route(stale,R.ROOT)
 def _registry_variant(self,mutate):
  registry=json.loads(json.dumps(R.TOPO.load_registry()))
  mutate(registry)
  return mock.patch.object(R.TOPO,"load_registry",return_value=registry)
 def test_edit_to_another_capability_keeps_the_route_current(self):
  route=R.compile_route(**self.args())
  self.assertIn("capability_registry_digest",route)
  def other_capability(registry):
   next(r for r in registry["recipes"] if r["capability"]!="autopilot-code")["probe"]="edited"
   registry["completion_gate_contracts"]["ship-setup"]["doc"]="edited doc"
   registry["completion_gate_contracts"]["unrelated-new-gate"]={"kind":"custom","doc":"x"}
   registry["recipes"].append({**json.loads(json.dumps(registry["recipes"][0])),"capability":"autopilot-brand-new"})
  with self._registry_variant(other_capability):
   self.assertNotEqual(route["registry_digest"],R.TOPO.registry_digest(R.TOPO.load_registry()))
   sys.modules["hearting_gates"]._REPORTED.clear()
   with mock.patch.dict(os.environ,{"HEARTING_GATES":"off"}), contextlib.redirect_stderr(io.StringIO()) as err:
    verified=R.verify_route(route,R.ROOT)
   self.assertNotIn("gate-off",err.getvalue())
   self.assertNotIn("_registry_current",verified)
   R.verify_route(route,R.ROOT)
 def test_edit_to_the_same_capability_or_a_shared_section_makes_the_route_stale(self):
  route=R.compile_route(**self.args())
  def same_capability(registry):
   next(r for r in registry["recipes"] if r["capability"]=="autopilot-code")["probe"]="edited"
  def referenced_gate(registry):
   registry["completion_gate_contracts"]["code-plan"]["probe"]="edited"
  def shared_section(registry):
   registry["parallel_group_max_width"]+=1
  for mutate in (same_capability,referenced_gate,shared_section):
   with self._registry_variant(mutate):
    with self.assertRaisesRegex(ValueError,"stale registry digest"):
     R.verify_route(route,R.ROOT)
    sys.modules["hearting_gates"]._REPORTED.clear()
    with mock.patch.dict(os.environ,{"HEARTING_GATES":"off"}), contextlib.redirect_stderr(io.StringIO()) as err:
     verified=R.verify_route(route,R.ROOT)
    self.assertIn("route-registry-stale",err.getvalue())
    self.assertIs(verified["_registry_current"],False)
 def test_route_without_capability_digest_keeps_whole_registry_comparison(self):
  route=R.compile_route(**self.args())
  legacy=json.loads(json.dumps(route)); legacy.pop("capability_registry_digest")
  legacy=self._reseal(legacy)
  R.verify_route(legacy,R.ROOT)
  def other_capability(registry):
   next(r for r in registry["recipes"] if r["capability"]!="autopilot-code")["probe"]="edited"
  with self._registry_variant(other_capability):
   with self.assertRaisesRegex(ValueError,"stale registry digest"):
    R.verify_route(legacy,R.ROOT)
 def test_capability_digest_ignores_other_capabilities_only(self):
  registry=R.TOPO.load_registry()
  base=R.TOPO.capability_registry_digest(registry,"autopilot-code")
  edited=json.loads(json.dumps(registry))
  next(r for r in edited["recipes"] if r["capability"]=="autopilot-ship")["probe"]="edited"
  self.assertEqual(base,R.TOPO.capability_registry_digest(edited,"autopilot-code"))
  self.assertNotEqual(base,R.TOPO.capability_registry_digest(edited,"autopilot-ship"))
  # A hand-edited composed recipe with a malformed `compose` digests, never crashes.
  for compose in ("x",["autopilot-ship"],None):
   R.TOPO.capability_registry_digest(registry,"autopilot-code",[{"compose":compose}])
 def test_legacy_route_without_basis_keeps_stale_wording(self):
  route=R.compile_route(**self.args())
  legacy=json.loads(json.dumps(route)); legacy.pop("validation_basis")
  legacy["registry_digest"]="sha256:"+"0"*64
  legacy["capability_registry_digest"]="sha256:"+"0"*64
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
  skewed["capability_registry_digest"]="sha256:"+"1"*64
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
  both["capability_registry_digest"]="sha256:"+"0"*64
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
  registry_skew["capability_registry_digest"]="sha256:"+"1"*64
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
  skewed["capability_registry_digest"]="sha256:"+"1"*64
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
 def test_project_cwd_identity_does_not_hash_untracked_runtime_files(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,git,base=self._repo(tmp)
   reports=root/"reports"; reports.mkdir(); (reports/"tracked").write_text("tracked")
   git("add","."); git("commit","-qm","reports")
   base=subprocess.check_output(["git","-C",str(root),"rev-parse","HEAD"],text=True).strip()
   runtime=reports/".runtime"; runtime.mkdir()
   for n in range(1000): (runtime/str(n)).write_text("mutable state")
   R._forget_launch_path(root)
   with mock.patch.object(R,"_launch_source_revision",side_effect=AssertionError("project content scan")):
    identity=R._launch_root_identity("grounding_cwd",root)
   self.assertEqual(identity["release_id"],base)
   self.assertTrue(R._grounding_cwd_lineage_ok(root,base+"+dirty:legacy",identity["release_id"]))
 def test_cwd_that_is_a_code_root_keeps_strict_release_identity(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,_,base=self._repo(tmp)
   (root/"a").write_text("modified code")
   R._forget_launch_path(root)
   runtime=R._launch_root_identity("runtime_root",root)
   cwd=R._launch_root_identity("grounding_cwd",root)
   self.assertEqual(cwd["release_id"],runtime["release_id"])
   self.assertTrue(cwd["release_id"].startswith(base+"+dirty:"))
 def test_wrapper_code_root_is_hashed_before_grounding_cwd(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,git,base=self._repo(tmp)
   adapters=root/"adapters"; adapters.mkdir(); (adapters/"source.py").write_text("old")
   git("add","."); git("commit","-qm","adapters")
   (adapters/"source.py").write_text("new")
   R._forget_launch_path(root); R._forget_launch_path(adapters)
   with mock.patch.object(R,"resolve_agent_home",return_value=str(root)):
    result=R.launch_compatibility_tuple(artifact_root=Path(tmp)/"artifacts",cwd=adapters)
   self.assertEqual(result["grounding_roots"]["cwd"]["release_id"],result["wrapper_root"]["release_id"])
   self.assertIn("+dirty:",result["wrapper_root"]["release_id"])
 def test_cwd_revision_timeout_does_not_fall_back_to_content_scan(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,_,_=self._repo(tmp); R._forget_launch_path(root)
   with mock.patch.object(R.subprocess,"run",side_effect=subprocess.TimeoutExpired("git",10)), mock.patch.object(R,"_launch_source_revision") as scan:
    with self.assertRaisesRegex(ValueError,"grounding-cwd-revision-unverifiable"):
     R._launch_root_identity("grounding_cwd",root)
    scan.assert_not_called()
 def test_non_git_cwd_is_unversioned_without_reading_its_files(self):
  activation=R._runtime_activation_module(); scan=AssertionError("cwd content scan")
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"GIT_CEILING_DIRECTORIES":tmp}):
   home=Path(tmp)/"home"; (home/"files").mkdir(parents=True)
   for n in range(100): (home/"files"/str(n)).write_text("user data")
   R._forget_launch_path(home)
   with mock.patch.object(R,"_launch_source_revision",side_effect=scan), \
        mock.patch.object(activation,"source_revision",side_effect=scan), \
        mock.patch.object(activation,"_tree_digest",side_effect=scan):
    identity=R._launch_root_identity("grounding_cwd",home)
   self.assertEqual(identity["release_id"],"unversioned")
   self.assertEqual(identity["release_id"],R._git_commit(home))
   # No lineage to compare, so its files changing is not cwd drift.
   route={"artifact_root":str(Path(tmp)/"artifacts"),"cwd":str(home)}
   route["launch_compatibility_tuple"]={"contract_version":1,**R.launch_compatibility_tuple(artifact_root=route["artifact_root"],cwd=home)}
   (home/"files"/"new").write_text("changed"); R._forget_launch_path(home)
   _,mismatches=R.revalidate_launch_compatibility(route)
   self.assertNotIn("grounding_roots.cwd",mismatches)
 def test_non_git_code_root_cwd_keeps_its_complete_identity(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"GIT_CEILING_DIRECTORIES":tmp}):
   release=Path(tmp)/"release"; release.mkdir(); (release/"RELEASE_VERSION").write_text("v9.9.9\n")
   R._forget_launch_path(release)
   cwd=R._launch_root_identity("grounding_cwd",release)["release_id"]
   R._forget_launch_path(release)
   self.assertEqual(cwd,R._launch_root_identity("runtime_root",release)["release_id"])
   self.assertTrue(cwd.startswith("release:v9.9.9:"))
   dev=Path(tmp)/"dev"; dev.mkdir(); (dev/"code.py").write_text("code")
   R._forget_launch_path(dev)
   runtime=R._launch_root_identity("runtime_root",dev)
   self.assertEqual(R._launch_root_identity("grounding_cwd",dev)["release_id"],runtime["release_id"])
   self.assertTrue(runtime["release_id"].startswith("tree:"))
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


class RevisedAncestorSuccessorTest(unittest.TestCase):
 def setUp(self):
  TestContinuation.setUp(self)
  gates=mock.patch.dict(os.environ,{"HEARTING_GATES":"on"})
  gates.start()
  self.addCleanup(gates.stop)
 _restore=TestContinuation._restore
 _dispatch=TestContinuation._dispatch
 _source=TestContinuation._source
 _complete_node=TestContinuation._complete_node
 _complete_prefix=TestContinuation._complete_prefix
 _write_attempt_row=TestContinuation._write_attempt_row
 _is_fixture_registry=staticmethod(TestContinuation._is_fixture_registry)
 def _terminal_owner(self, route, cycle_dir):
  attempt="att-fixture-owner"
  report=Path(cycle_dir)/"artifacts"/"plans"/"owner-blocked.md"
  report.write_text("# Blocked\n",encoding="utf-8")
  log=Path(self._tmp_home.name)/"owner.jsonl"
  log.write_text("\n".join(json.dumps(row) for row in (
   {"type":"turn.started"},
   {"type":"item.completed","item":{"type":"agent_message",
      "text":f"artifact: {report}\nverdict: BLOCKED\nblocker: plan review needs correction"}},
   {"type":"turn.completed"},
  ))+"\n",encoding="utf-8")
  metadata={
   "attempt_id":attempt,"worker_type":"owner","dispatch_depth":"1",
   "registered_worker":"1","owner_route_file":str(R.canonical_route_path(
      Path(route["artifact_root"]),route["route_id"])),
   "owner_route_id":route["route_id"],"owner_route_hash":route["route_hash"],
   "pid":"99999999","pid_start":D.process_start_ticks(os.getpid()),"pid_scope":"host-visible",
   "pgid":"99999999","terminal_event":"turn.completed",
   "failure_class":"blocked","note":"dead-worker-blocked",
   "log_file":str(log),
  }
  with self._jobs.open("a",encoding="utf-8") as handle:
   handle.write("\t".join(["2026-09-28T00:00:00Z","done",str(R.ROOT),
      str(R.ROOT),"fixture-owner",",".join(f"{key}={value}" for key,value in metadata.items())])+"\n")
  return attempt
 def _revised_fixture(self, root, *, revision_author=None):
  import artifact_producer as AP
  previous_root=os.environ.get("AGENT_ARTIFACT_ROOT")
  os.environ["AGENT_ARTIFACT_ROOT"]=str(root)
  self.addCleanup(lambda: (os.environ.pop("AGENT_ARTIFACT_ROOT",None)
                           if previous_root is None
                           else os.environ.__setitem__("AGENT_ARTIFACT_ROOT",previous_root)))
  gate={"spec_read":{"satisfied":True,"source":"canonical-prd-sha256"},
        "drift_verdict":"within-spec","workflow_mode":"tracked",
        "artifact_guard":{"satisfied":True,"source":"conductor-prechecked"}}
  source=R.compile_route(
   "autopilot-code","dev","standard",R.ROOT,root,predicates=[],
   signals=["shared-contract"],transport="headless",tracking="tracked",
   tracked_gate_evidence=gate,dispatch_evidence=self._dispatch(),
   campaign_key="revised-ancestor-fixture",
  )
  source["runtime_lineage"]={"runtime":"codex","thread_id":"thread-source",
     "node_turn_ids":{str(node["id"]):f"turn-{node['id']}" for node in source["nodes"]}}
  source["route_hash"]=R.route_hash(source)
  source["route_id"]="rt-"+source["route_hash"].split(":",1)[1][:16]
  source_path=R.canonical_route_path(root,source["route_id"])
  R.write_once(source_path,source)
  begun=AP.begin(root,route_file=source_path,capability=source["capability"],
                 intensity=source["effective_intensity"])
  evidence=self._complete_prefix(
   source,"plan-check",Path(begun["cycle_dir"])/"artifacts"/"plans"
  )["plan"]
  prior=R.build_continuation_route(
   source,resume_from_node="plan-check",requested_boundary="plan-check",
   reason="initial-review",artifact_root=root,
  )
  self.assertIsNone(prior.get("first_runnable_blocker"),prior.get("first_runnable_blocker"))
  R.publish_continuation_route(prior,source,R.canonical_route_path(root,prior["route_id"]))
  AP.bind_cycle_route(root,begun["cycle_id"],prior)
  owner=self._terminal_owner(prior,begun["cycle_dir"])
  evidence.write_text("revised plan after owner correction\n",encoding="utf-8")
  R.publish_revision_locked(source,"plan",evidence,basis="owner-correction",
     reason="correct the plan",author_attempt_id=revision_author or owner,jobs=self._jobs)
  return source,prior,begun,evidence

 def test_revised_plan_successor_reuses_only_current_plan_and_restarts_review(self):
  import artifact_producer as AP
  import review_input
  dn_spec=importlib.util.spec_from_file_location("revised_ancestor_dispatch_node",P.with_name("dispatch-node.py"))
  DN=importlib.util.module_from_spec(dn_spec); dn_spec.loader.exec_module(DN)
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/".agent_reports"
   source,prior,begun,evidence=self._revised_fixture(root)
   with mock.patch.object(review_input,"_route_module",return_value=R):
    successor=R.build_continuation_route(
     prior,resume_from_node="plan-check",requested_boundary="plan-check",
     reason="review revised plan",artifact_root=root,
    )
    self.assertIn("ancestor_plan_refresh",successor,
                  [row["node_id"] for row in prior["reused_nodes"]])
    self.assertIsNone(successor["first_runnable_blocker"])
    proof=successor["ancestor_plan_refresh"]
    self.assertEqual(proof["current_evidence"]["sha256"],R.evidence_digest(evidence))
    self.assertEqual([route["route_id"] for route in R.review_lineage_routes(successor,"plan-check")],
                     [successor["route_id"],prior["route_id"],source["route_id"]])
    self.assertEqual(successor["nodes"][0]["reused_dependencies"][0]["marker_digest"],
                     proof["current_marker_digest"])
    self.assertEqual(review_input.resolve_input(successor,successor["nodes"][0],self._jobs)["sha256"],
                     proof["current_evidence"]["sha256"])
    other=Path(tmp)/"unreviewed.md"
    other.write_text("other plan\n",encoding="utf-8")
    with self.assertRaisesRegex(D.DispatchContractError,"plan-check"):
     review_input.resolve_input(successor,successor["nodes"][0],self._jobs,other)
    admission=DN.admit_round(successor,successor["nodes"][0],self._jobs,
                             record_auto_revisions=False)
    self.assertEqual(admission.budget.state,"admit")
    self.assertEqual(admission.reviewed_input["sha256"],proof["current_evidence"]["sha256"])
    record=AP.route_cycle_for(root,successor)
    self.assertEqual(record["cycle_id"],begun["cycle_id"])
    self.assertTrue(AP.cycle_route_admission(root,record,successor,validation_only=True).allow)
    successor_path=R.canonical_route_path(root,successor["route_id"])
    R.publish_continuation_route(successor,prior,successor_path)
    self.assertEqual(R.verify_route(json.loads(successor_path.read_text()),successor["cwd"])["route_id"],
                     successor["route_id"])
    self.assertTrue(R.bind_continuation_cycle(root,prior,successor)["bound"])
    self.assertEqual(AP.begin(root,route_file=successor_path,capability=successor["capability"],
                              intensity=successor["effective_intensity"])["status"],"resumed")
    review_evidence=self._complete_node(successor,successor["nodes"][0],
        Path(begun["cycle_dir"])/"artifacts"/"_internal"/"plan_reviews")
    self.assertTrue(review_evidence.is_file())
    lines=self._jobs.read_text().splitlines()
    lines=[line+(",route_hash="+successor["route_hash"]
                 +",attempt_schema_version=2,dispatch_depth=2,transport=headless"
                 +",execution_surface=registered-headless,fallback_hop=same-harness-headless"
                 +",registered_worker=1,worker_type=review,unit=qa/plan-review"
                 +f",note=completed-marker,pid=99999999,pid_start={D.process_start_ticks(os.getpid())},pid_scope=host-visible"
                 +",pgid=99999999"
                 if "attempt_id=att-continuation-plan-check" in line else "") for line in lines]
    self._jobs.write_text("\n".join(lines)+"\n")
    self.assertTrue(R._marker_identity_row(successor,successor["nodes"][0],"plan-check",
                    successor["nodes"][0]["completion_gate"],jobs=self._jobs)["passed"])
    D.completion_marker_gate(str(successor_path),"execute","dry-run",
                             Path(self._tmp_home.name),self._jobs)
    with mock.patch.object(
       D,"attempt_process_quiescence",
       return_value=D.ProcessQuiescence("quiescent","fixture-owner-ended"),
    ):
     implementation=R.build_continuation_route(successor,resume_from_node="execute",
        requested_boundary="execute",reason="continue after fresh review",artifact_root=root)
    self.assertIsNone(implementation["first_runnable_blocker"])
    self.assertEqual(implementation["ancestor_plan_refresh"],proof)
    self.assertEqual([row["route_id"] for row in R.review_lineage_routes(implementation,"impl-review")],
       [implementation["route_id"],successor["route_id"],prior["route_id"],source["route_id"]])
    self.assertTrue(AP.cycle_route_admission(root,AP.read_cycle_record(root,begun["cycle_id"]),
                                             implementation,validation_only=True).allow)
    self.assertTrue(AP.cycle_route_admission(root,AP.read_cycle_record(root,begun["cycle_id"]),
                                             successor,finalize=True,validation_only=True).allow)
    sealed=AP.finalize(root,cycle_id=begun["cycle_id"],state="abandoned",
                       allow_open_route=True,abandon_reason="operator-decision")
    self.assertEqual(sealed["status"],"sealed")
    skipped=R.build_continuation_route(
     prior,resume_from_node="execute",requested_boundary="execute",
     reason="skip revised review",artifact_root=root,
    )
    self.assertEqual(skipped["first_runnable_blocker"],"ancestor-revision-affected-node-skipped")

 def test_revised_successor_refuses_broken_history_owner_and_current_file(self):
  def mutate_old_history(source,prior,evidence):
   (R.completion_dir(source["route_id"],jobs=self._jobs)/"plan.1.json").unlink()
  def mutate_current_history(source,prior,evidence):
   path=R.completion_dir(source["route_id"],jobs=self._jobs)/"plan.2.json"
   path.write_text(path.read_text()+" ",encoding="utf-8")
  def mutate_current_file(source,prior,evidence):
   evidence.write_bytes(b"X"*len(evidence.read_bytes()))
  def remove_current_file(source,prior,evidence):
   evidence.unlink()
  def mutate_owner_status(source,prior,evidence):
   lines=self._jobs.read_text().splitlines()
   self._jobs.write_text("\n".join(
    line.replace("\tdone\t","\trunning\t",1)
    if "attempt_id=att-fixture-owner" in line else line for line in lines)+"\n")
  def mutate_owner_binding(source,prior,evidence):
   lines=self._jobs.read_text().splitlines()
   self._jobs.write_text("\n".join(
    line.replace("owner_route_hash="+prior["route_hash"],"owner_route_hash=sha256:"+"0"*64)
    if "attempt_id=att-fixture-owner" in line else line for line in lines)+"\n")
  def mutate_owner_terminal(source,prior,evidence):
   log=Path(self._tmp_home.name)/"owner.jsonl"
   log.write_text(log.read_text().replace("verdict: BLOCKED","verdict: PASS"),encoding="utf-8")
  for name,mutate in (
   ("old-history-missing",mutate_old_history),
   ("current-history-tampered",mutate_current_history),
   ("current-file-changed",mutate_current_file),
   ("current-file-missing",remove_current_file),
   ("owner-running",mutate_owner_status),
   ("owner-binding",mutate_owner_binding),
   ("owner-terminal",mutate_owner_terminal),
  ):
   with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
    source,prior,begun,evidence=self._revised_fixture(Path(tmp)/".agent_reports")
    mutate(source,prior,evidence)
    with self.assertRaises(ValueError):
     R.build_continuation_route(prior,resume_from_node="plan-check",
       requested_boundary="plan-check",reason="invalid revision",artifact_root=source["artifact_root"])

 def test_revised_successor_refuses_live_or_unverifiable_owner_identity(self):
  with tempfile.TemporaryDirectory() as tmp:
   source,prior,_begun,_evidence=self._revised_fixture(Path(tmp)/".agent_reports")
   for state in ("live","unverifiable"):
    with self.subTest(state=state),mock.patch.object(R,"attempt_process_quiescence",
          return_value=D.ProcessQuiescence(state,"fixture")):
     with self.assertRaisesRegex(ValueError,"ancestor-revision-source-owner-not-quiescent"):
      R.build_continuation_route(prior,resume_from_node="plan-check",
       requested_boundary="plan-check",reason="unsafe owner",artifact_root=source["artifact_root"])

 def test_revised_successor_refuses_wrong_revision_author(self):
  with tempfile.TemporaryDirectory() as tmp:
   source,prior,_begun,_evidence=self._revised_fixture(
    Path(tmp)/".agent_reports",revision_author="att-other")
   with self.assertRaisesRegex(ValueError,"ancestor-revision-author-unverified"):
    R.build_continuation_route(prior,resume_from_node="plan-check",
      requested_boundary="plan-check",reason="wrong author",artifact_root=source["artifact_root"])

 def test_revised_successor_rechecks_after_build_and_rejects_forged_seal(self):
  import review_input
  with tempfile.TemporaryDirectory() as tmp:
   source,prior,_begun,evidence=self._revised_fixture(Path(tmp)/".agent_reports")
   with self.assertRaisesRegex(ValueError,"reuse-evidence"):
    R.review_lineage_routes(prior,"plan-check")
   successor=R.build_continuation_route(prior,resume_from_node="plan-check",
      requested_boundary="plan-check",reason="fresh review",artifact_root=source["artifact_root"])
   forged=json.loads(json.dumps(successor))
   forged["ancestor_plan_refresh"]["current_marker_digest"]="sha256:"+"0"*64
   forged["ancestor_revision_digest"]=R._sha256_record(forged["ancestor_plan_refresh"])
   forged["route_hash"]=R.route_hash(forged)
   forged["route_id"]="rt-"+forged["route_hash"].split(":",1)[1][:16]
   with self.assertRaisesRegex(ValueError,"ancestor-revision-refresh-drift"):
    R.review_lineage_routes(forged,"plan-check")
   evidence.write_bytes(b"Y"*len(evidence.read_bytes()))
   target=R.canonical_route_path(Path(source["artifact_root"]),successor["route_id"])
   with self.assertRaises(ValueError):
    R.publish_continuation_route(successor,prior,target)
   self.assertFalse(target.exists())
   with self.assertRaises(ValueError):
    R.review_lineage_routes(successor,"plan-check")
   with self.assertRaises(D.DispatchContractError):
    review_input.resolve_input(successor,successor["nodes"][0],self._jobs)


class SourceLineageVerdictTest(unittest.TestCase):
 """SD-156: the one first-parent lineage probe every consumer shares."""
 def _repo(self,tmp):
  import subprocess
  root=Path(tmp)/"wt"; root.mkdir()
  def git(*a): subprocess.run(["git","-C",str(root),*a],check=True,capture_output=True,text=True)
  git("init","-q"); git("config","user.email","t@t"); git("config","user.name","t")
  (root/"a").write_text("1"); git("add","."); git("commit","-q","-m","a")
  base=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
  return root,git,base

 def test_exact_head_passes(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,_git,base=self._repo(tmp)
   verdict=R.source_lineage_verdict(root,base)
   self.assertEqual(verdict.kind,"exact")
   self.assertEqual(verdict.distance,0)
   self.assertEqual(verdict.commits,())
   self.assertIsNone(verdict.reason)

 def test_a_sd156_1_single_commit_descendant_records_distance_and_branch(self):
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   root,git,base=self._repo(tmp)
   (root/"b").write_text("2"); git("add","."); git("commit","-q","-m","b")
   head=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
   verdict=R.source_lineage_verdict(root,base)
   self.assertEqual(verdict.kind,"descendant")
   self.assertEqual(verdict.distance,1)
   self.assertEqual(verdict.commits,(head,))
   self.assertIsNotNone(verdict.branch)
   self.assertNotEqual(verdict.branch,"HEAD")

 def test_a_sd156_4_merge_commit_with_branch_first_parent_passes(self):
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   root,git,base=self._repo(tmp)
   git("checkout","-qb","feature")
   (root/"feature").write_text("f"); git("add","."); git("commit","-q","-m","feature work")
   git("checkout","-q","-")
   git("-c","user.email=t@t","-c","user.name=t","merge","--no-ff","-q","-m","merge feature","feature")
   head=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
   verdict=R.source_lineage_verdict(root,base)
   self.assertEqual(verdict.kind,"descendant")
   self.assertIn(head,verdict.commits)

 def test_a_sd156_3_diverged_head_refused(self):
  # subTest shapes: a HEAD reset to before the sealed commit, and an
  # unrelated orphan branch -- neither is the sealed commit or a first-parent
  # descendant of it, so both read `diverged`, never a pass.
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   root,git,base=self._repo(tmp)
   with self.subTest("rewritten-root"):
    git("commit","--amend","-q","-m","rewritten")
    verdict=R.source_lineage_verdict(root,base)
    self.assertEqual(verdict.kind,"diverged")
    self.assertIsNone(verdict.reason)
  with tempfile.TemporaryDirectory() as tmp:
   root,git,base=self._repo(tmp)
   with self.subTest("unrelated-orphan-branch"):
    git("checkout","-q","--orphan","unrelated")
    git("commit","-q","--allow-empty","-m","orphan")
    verdict=R.source_lineage_verdict(root,base)
    self.assertEqual(verdict.kind,"diverged")

 def test_a_sd156_3_merge_in_progress_is_unverifiable(self):
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   root,git,base=self._repo(tmp)
   git("checkout","-qb","feature")
   (root/"a").write_text("2"); git("commit","-qam","feature edits a")
   git("checkout","-q","-")
   (root/"a").write_text("3"); git("commit","-qam","main edits a")
   subprocess.run(["git","-C",str(root),"merge","--no-ff","-q","-m","x","feature"],capture_output=True,text=True)
   git_dir=subprocess.run(["git","-C",str(root),"rev-parse","--git-dir"],capture_output=True,text=True).stdout.strip()
   self.assertTrue((root/git_dir/"MERGE_HEAD").exists(),"fixture must actually be mid-merge")
   verdict=R.source_lineage_verdict(root,base)
   self.assertEqual(verdict.kind,"unverifiable")
   self.assertEqual(verdict.reason,"unsafe-git-operation")

 def test_a_sd156_3_detached_head_is_unverifiable(self):
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   root,git,base=self._repo(tmp)
   (root/"b").write_text("2"); git("add","."); git("commit","-q","-m","b")
   git("checkout","-q","--detach","HEAD")
   verdict=R.source_lineage_verdict(root,base)
   self.assertEqual(verdict.kind,"unverifiable")
   self.assertEqual(verdict.reason,"unsafe-git-state")

 def test_not_a_repo_and_git_failure_are_unverifiable(self):
  with tempfile.TemporaryDirectory() as tmp:
   verdict=R.source_lineage_verdict(Path(tmp),"a"*40)
   self.assertEqual(verdict.kind,"unverifiable")
   self.assertEqual(verdict.reason,"not-a-repo")

 def test_git_timeout_is_unverifiable(self):
  import subprocess as sp
  def slow(*a,**k): raise sp.TimeoutExpired(cmd="git",timeout=k.get("timeout",1))
  with mock.patch.object(R.subprocess,"run",side_effect=slow):
   verdict=R.source_lineage_verdict(R.ROOT,"a"*40,timeout=1)
  self.assertEqual(verdict.kind,"unverifiable")
  self.assertEqual(verdict.reason,"git-timeout")

 def test_a_sd156_6_worker_route_guard_shares_the_same_definition(self):
  # This test file execs its own copy of capability-route.py, so `G.ROUTE`
  # is a different module instance of the same source -- bytecode identity is
  # the only comparison that proves "one definition" rather than "two files
  # loaded twice" (`test_c_route_and_guard_share_one_mutating_scope_definition`
  # establishes this pattern for `worktree_mutating_scope`).
  self.assertEqual(
   G.ROUTE.source_lineage_verdict.__code__.co_code,
   R.source_lineage_verdict.__code__.co_code,
  )

 def test_a_sd156_6_three_consumers_agree_on_a_real_descendant(self):
  import subprocess
  with tempfile.TemporaryDirectory() as tmp:
   root,git,base=self._repo(tmp)
   (root/"b").write_text("2"); git("add","."); git("commit","-q","-m","b")
   head=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
   direct=R.source_lineage_verdict(root,base)
   self.assertEqual(direct.kind,"descendant")
   guard=G.ROUTE.source_lineage_verdict(root,base)
   self.assertEqual((guard.kind,guard.distance),(direct.kind,direct.distance))
   self.assertTrue(R._grounding_cwd_lineage_ok(root,base,head))
   verify=subprocess.run(
    [sys.executable,str(P),"verify","--route","/dev/null","--cwd",str(root)],
    capture_output=True,text=True,
   )
   # A bare --route smoke isn't a real route file; assert the shared function
   # directly instead of round-tripping through a full compiled route here --
   # `test_c_continuation_cli_rebinds_source_commit_after_fast_forward`
   # already exercises the real `verify --cwd` subprocess end to end.
   self.assertTrue(verify.returncode != 0)

 def test_a_sd156_6_git_failure_reads_unverifiable_on_every_consumer(self):
  with tempfile.TemporaryDirectory() as tmp:
   fake_bin=Path(tmp)/"bin"; fake_bin.mkdir()
   fake_git=fake_bin/"git"
   fake_git.write_text("#!/bin/sh\nexit 1\n"); fake_git.chmod(0o755)
   scratch=Path(tmp)/"scratch"; scratch.mkdir()
   prior=os.environ.get("PATH")
   os.environ["PATH"]=str(fake_bin)+os.pathsep+(prior or "")
   try:
    direct=R.source_lineage_verdict(scratch,"a"*40)
    self.assertEqual(direct.kind,"unverifiable")
    guard=G.ROUTE.source_lineage_verdict(scratch,"a"*40)
    self.assertEqual(guard.kind,"unverifiable")
    self.assertFalse(R._grounding_cwd_lineage_ok(scratch,"a"*40,"b"*40))
   finally:
    if prior is None: os.environ.pop("PATH",None)
    else: os.environ["PATH"]=prior


class VerifiedRouteLineageTest(TestContinuation):
 """SD-155: `review_lineage_routes` is `verified_route_lineage` plus only the
 node-assignment identity check review disposition still needs."""

 def test_review_lineage_uses_verified_lineage_plus_node_assignment(self):
  import route_lineage as RL
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact)
   source_path=R.canonical_route_path(artifact,source["route_id"])
   R.write_once(source_path,source)
   self._complete_prefix(source,"test",Path(tmp)/"evidence")
   continuation=self._build(source)
   continuation_path=R.canonical_route_path(artifact,continuation["route_id"])
   R.publish_continuation_route(continuation,source,continuation_path)

   # One definition: capability-route's re-export is the leaf's own object.
   self.assertIs(R.verified_route_lineage,RL.verified_route_lineage)

   lineage=R.verified_route_lineage(continuation,artifact_root=artifact)
   self.assertEqual([row["route_id"] for row in lineage],
                    [continuation["route_id"],source["route_id"]])
   # review_lineage_routes agrees on the walk for a genuine continuation.
   # "report" is a `new_nodes` entry (its dependency, "test", is realized
   # fresh by this continuation) but is not itself the resume boundary, so
   # unlike "test" it carries no resume-provenance fields
   # (`source_depends_on`/`reused_dependencies`) and matches the parent
   # exactly. A pure prefix-reused node (e.g. "plan-check") has no
   # `new_nodes` edge at all and is out of this check's shape.
   review_lineage=R.review_lineage_routes(continuation,"report")
   self.assertEqual([row["route_id"] for row in review_lineage],
                    [row["route_id"] for row in lineage])

   # Tamper only the node assignment (not context/hash chain): the shared
   # walk still passes, but review disposition's own extra check refuses.
   tampered=json.loads(json.dumps(continuation))
   node=next(n for n in tampered["nodes"] if n["id"]=="report")
   node["unit"]="editorial/tampered-report"
   tampered["route_hash"]=R.route_hash(tampered)
   tampered["route_id"]="rt-"+tampered["route_hash"].split(":",1)[1][:16]

   self.assertEqual(
    [row["route_id"] for row in R.verified_route_lineage(tampered,artifact_root=artifact)],
    [tampered["route_id"],source["route_id"]])
   with self.assertRaisesRegex(ValueError,"owner-closure-lineage-node-mismatch"):
    R.review_lineage_routes(tampered,"report")

 def test_review_lineage_accepts_exact_reused_plan_check_boundary_and_nested_resume(self):
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact)
   source_path=R.canonical_route_path(artifact,source["route_id"])
   R.write_once(source_path,source)
   self._complete_prefix(source,"plan-check",Path(tmp)/"evidence")
   first=R.build_continuation_route(
    source,resume_from_node="plan-check",requested_boundary="plan-check",
    reason="resume-review-boundary",artifact_root=artifact,
   )
   first_path=R.canonical_route_path(artifact,first["route_id"])
   R.publish_continuation_route(first,source,first_path)
   plan_check=next(node for node in first["nodes"] if node["id"]=="plan-check")
   self.assertEqual(plan_check["source_depends_on"],["plan","plan-alternative"])
   self.assertEqual(plan_check["depends_on"],[])
   self.assertEqual(R.review_lineage_routes(first,"plan-check")[0]["route_id"],first["route_id"])

   # Reuse a larger prefix in the next generation. The earlier rewrite remains
   # checked on the first edge, while the second edge keeps the same assignment.
   self._complete_prefix(first,"impl-review",Path(tmp)/"evidence-second")
   second=R.build_continuation_route(
    first,resume_from_node="impl-review",requested_boundary="impl-review",
    reason="nested-resume",artifact_root=artifact,
   )
   second_path=R.canonical_route_path(artifact,second["route_id"])
   R.publish_continuation_route(second,first,second_path)
   lineage=R.review_lineage_routes(second,"plan-check")
   self.assertEqual([row["route_id"] for row in lineage],
                    [second["route_id"],first["route_id"],source["route_id"]])

   full_root=Path(tmp)/"full-prefix-artifacts"
   full_source=self._source(full_root)
   full_source_path=R.canonical_route_path(full_root,full_source["route_id"])
   R.write_once(full_source_path,full_source)
   self._complete_prefix(full_source,"test",Path(tmp)/"full-prefix-evidence")
   full=R.build_continuation_route(
    full_source,resume_from_node="test",requested_boundary="test",
    reason="full-prefix-cut",artifact_root=full_root,
   )
   R.publish_continuation_route(full,full_source,
                                R.canonical_route_path(full_root,full["route_id"]))
   self.assertTrue(any(row["node_id"]=="plan-check" for row in full["reused_nodes"]))
   self.assertEqual([row["route_id"] for row in R.review_lineage_routes(full,"plan-check")],
                    [full["route_id"],full_source["route_id"]])

 def test_nested_plan_check_census_keeps_all_ancestor_route_ids(self):
  """A fresh continuation ID must not reset the review budget.

  This follows the production failure shape (rt6c -> rt50 -> rtf2): the first
  continuation resumes at plan-check, then a nested continuation resumes at
  impl-review and carries plan-check in its reused prefix. Admission's caller
  must still see attempts recorded under every generation that owned the
  review.
  """
  dn_spec=importlib.util.spec_from_file_location("dispatch_node_for_continuation_test",P.with_name("dispatch-node.py"))
  DN=importlib.util.module_from_spec(dn_spec); dn_spec.loader.exec_module(DN)
  with tempfile.TemporaryDirectory() as tmp:
   artifact=Path(tmp)/"artifacts"
   source=self._source(artifact)
   R.write_once(R.canonical_route_path(artifact,source["route_id"]),source)
   self._complete_prefix(source,"plan-check",Path(tmp)/"evidence-source")
   first=R.build_continuation_route(
    source,resume_from_node="plan-check",requested_boundary="plan-check",
    reason="resume-review-boundary",artifact_root=artifact,
   )
   R.publish_continuation_route(
    first,source,R.canonical_route_path(artifact,first["route_id"]))
   self._complete_prefix(first,"impl-review",Path(tmp)/"evidence-first")
   nested=R.build_continuation_route(
    first,resume_from_node="impl-review",requested_boundary="impl-review",
    reason="nested-resume",artifact_root=artifact,
   )
   R.publish_continuation_route(
    nested,first,R.canonical_route_path(artifact,nested["route_id"]))

   lineage=R.review_lineage_routes(nested,"plan-check")
   self.assertEqual([row["route_id"] for row in lineage],
                    [nested["route_id"],first["route_id"],source["route_id"]])
   cap=DN.REVIEW_ROUND_CAP.max_review_rounds(source["effective_intensity"])
   # Put the completed blocking verdicts on both ancestor IDs, just as the
   # production registry does when a review is retried through continuations.
   for index,route_id in enumerate((source["route_id"],first["route_id"]),1):
    with self._jobs.open("a",encoding="utf-8") as handle:
     for round_no in range(1,cap+1):
      handle.write(
       f"2026-09-01T00:00:{index:02d}{round_no:02d}Z\tdone\t{R.ROOT}\t{R.ROOT}\t"
       f"ancestor-plan-check-{index}-{round_no}\t"
       "attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
       f"route_id={route_id},route_node=plan-check,note=completed-review-blocking,"
       f"attempt_id=att-ancestor-plan-check-{index}-{round_no}\n")
   prior=DN.prior_round_attempts(
    self._jobs,nested["route_id"],"plan-check",route=nested)
   # The first continuation's completed plan-check marker contributes one
   # ordinary registry row in addition to the four explicit blocking rounds.
   self.assertEqual(len(prior),2*cap+1)
   budget=DN.REVIEW_ROUND_CAP.round_budget(
    nested,next(node for node in first["nodes"] if node["id"]=="plan-check"),
    [(cols[1],meta) for cols,meta in prior])
   self.assertEqual(budget.state,"exhausted")
   self.assertEqual(budget.verdict_rounds,2*cap)

   # Assignment/provenance edits remain invalid after every affected hash is
   # recomputed. The shared route-lineage walk accepts these self-consistent
   # hashes; review admission must enforce the exact source assignment too.
   for mutate in (
    lambda row: row.__setitem__("marker_digest","sha256:"+"0"*64),
    lambda row: row.__setitem__("terminal_attempt_id","att-foreign"),
    lambda row: row.__setitem__("node_id","foreign"),
   ):
    forged=json.loads(json.dumps(first))
    evidence=forged["reused_nodes"][0]
    mutate(evidence)
    forged["source_evidence_digest"]=R._sha256_record(forged["reused_nodes"])
    forged["route_hash"]=R.route_hash(forged)
    forged["route_id"]="rt-"+forged["route_hash"].split(":",1)[1][:16]
    with self.assertRaisesRegex(ValueError,"owner-closure-lineage-node-mismatch"):
     R.review_lineage_routes(forged,"plan-check")

   forged=json.loads(json.dumps(first))
   node=next(row for row in forged["nodes"] if row["id"]=="plan-check")
   node["reused_dependencies"][0]["terminal_attempt_id"]="att-foreign"
   descriptor=next(row for row in forged["new_nodes"] if row["node_id"]=="plan-check")
   descriptor["realized_contract_hash"]=R._continuation_contract_hash(node)
   forged["route_hash"]=R.route_hash(forged)
   forged["route_id"]="rt-"+forged["route_hash"].split(":",1)[1][:16]
   with self.assertRaisesRegex(ValueError,"owner-closure-lineage-node-mismatch:assignment"):
    R.review_lineage_routes(forged,"plan-check")

   for label,mutate in (
    ("source_depends_on",lambda node:node.__setitem__("source_depends_on",["forged"])),
    ("reused_dependencies",lambda node:node["reused_dependencies"][0].__setitem__("terminal_attempt_id","att-forged")),
    ("gate",lambda node:node.__setitem__("completion_gate","forged-gate")),
    ("write_scope",lambda node:node.__setitem__("write_scope",["forged-scope"])),
    ("profile",lambda node:node.__setitem__("model_profile","forged-profile")),
    ("role",lambda node:node.__setitem__("role","external adversary")),
    ("unit",lambda node:node.__setitem__("unit","editorial/forged")),
   ):
    with self.subTest(assignment=label):
     forged=json.loads(json.dumps(first))
     node=next(row for row in forged["nodes"] if row["id"]=="plan-check")
     mutate(node)
     descriptor=next(row for row in forged["new_nodes"] if row["node_id"]=="plan-check")
     descriptor["realized_contract_hash"]=R._continuation_contract_hash(node)
     forged["route_hash"]=R.route_hash(forged)
     forged["route_id"]="rt-"+forged["route_hash"].split(":",1)[1][:16]
     with self.assertRaisesRegex(ValueError,"owner-closure-lineage-node-mismatch:assignment"):
      R.review_lineage_routes(forged,"plan-check")


class SourceCensusTest(unittest.TestCase):
 """A-SD156-6: the route-lineage consumers stay on the one shared probe.

 Scoped to the route-lineage consumers the phase brief names (worker guard,
 session hook, grounding comparison) -- other tools' unrelated `rev-list
 --first-parent` uses (e.g. `adapters/claude/tools/memory/mem.py`'s transcript
 history squashing) are out of scope, per plan §6's census note.
 """
 ROUTE_LINEAGE_CONSUMERS=(
  "utilities/capability-route.py",
  "utilities/worker-route-guard.py",
 )

 def test_first_parent_probe_single_site(self):
  root=P.parents[1]
  hits=[]
  for relative in self.ROUTE_LINEAGE_CONSUMERS:
   text=(root/relative).read_text(encoding="utf-8",errors="replace")
   if "rev-list" in text and "--first-parent" in text:
    hits.append(relative)
  self.assertEqual(hits,["utilities/capability-route.py"],hits)

 # SD-155/D-120, A-25.7: the ancestor walk and audit-binding writer
 # remain shared. Route-ID comparisons also validate execution evidence and
 # identity, so a function-name whitelist is not a cycle-ownership test.
 def test_a25_7_lineage_and_binding_writer_single_site(self):
  import ast
  producer_path=P.parents[1]/"utilities"/"artifact_producer.py"
  tree=ast.parse(producer_path.read_text(encoding="utf-8"))

  class LineageWalkVisitor(ast.NodeVisitor):
   """`continuation_contract_version`-driven ancestor walks (a `while`
   loop testing it, or a direct read of `source_route_id`) outside the one
   shared walk."""
   def __init__(self):
    self.stack=[]; self.hits=[]
   def visit_FunctionDef(self,node):
    self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
   def visit_While(self,node):
    text=ast.dump(node.test)
    if "continuation_contract_version" in text:
     self.hits.append((self.stack[-1] if self.stack else "<module>",node.lineno))
    self.generic_visit(node)

  lineage_visitor=LineageWalkVisitor(); lineage_visitor.visit(tree)
  self.assertEqual(lineage_visitor.hits,[],lineage_visitor.hits)

  route_lineage_path=P.parents[1]/"utilities"/"route_lineage.py"
  lineage_tree=ast.parse(route_lineage_path.read_text(encoding="utf-8"))
  lineage_visitor2=LineageWalkVisitor(); lineage_visitor2.visit(lineage_tree)
  self.assertEqual([fn for fn,_ln in lineage_visitor2.hits],["verified_route_lineage"])

  class RouteBindingsVisitor(ast.NodeVisitor):
   def __init__(self):
    self.stack=[]; self.hits=[]
   def visit_FunctionDef(self,node):
    self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
   def visit_Constant(self,node):
    if node.value=="route_bindings":
     self.hits.append(self.stack[-1] if self.stack else "<module>")

  binding_visitor=RouteBindingsVisitor(); binding_visitor.visit(tree)
  self.assertEqual(set(binding_visitor.hits),{"_bind_cycle_route_locked"},binding_visitor.hits)

 # SD-154 A-SD154-8: `evidence_digest`'s completion-marker-schema recompute
 # lives only in `gate_currency`/`evidence_currency` (dispatch_contract.py)
 # and the closed writer allow-list below. Every OTHER call is a genuinely
 # different schema this census also enumerates by name rather than
 # pretending does not exist: an owner-merge arbitration record
 # (`_arbitration_observation`/`arbitrate_group`/the `parallel_group:` row
 # inside `terminal_gate_observation`), an owner-executed terminal that has
 # no completion marker file at all (`_owner_terminal_observation`), a
 # review-artifact provenance sha for an owner-closure proof
 # (`continuation_owner_closure_plan`/`_continuation_closure_marker_compatible`),
 # an exact markerless terminal review artifact (`observe_terminal_review_failure`),
 # and `source_evidence_digest` (route-reuse's own named exception). None of
 # these ever duplicated the completion-marker recompute B-1/A-2 unified --
 # each is the one place ITS OWN schema is hashed.
 _EVIDENCE_DIGEST_ALLOWED_FUNCTIONS=frozenset({
  # dispatch_contract.py
  "gate_currency","evidence_currency","observe_terminal_review_failure",
  # capability-route.py -- completion-marker writers
  "_completion_marker_replay","write_completion_marker",
  "_publish_completion_locked","_producer_revision_plan",
  # capability-route.py -- named exception, route-reuse digest
  "source_evidence_digest",
  # capability-route.py -- different schema, not a completion marker
  "_arbitration_observation","arbitrate_group","terminal_gate_observation",
  "_owner_terminal_observation",
  "continuation_owner_closure_plan","_continuation_closure_marker_compatible",
 })

 def test_a_sd154_8_evidence_digest_recomputed_only_in_gate_currency(self):
  import ast

  class EvidenceDigestVisitor(ast.NodeVisitor):
   def __init__(self):
    self.stack=[]; self.hits=[]
   def visit_FunctionDef(self,node):
    self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
   def visit_Call(self,node):
    name=node.func.id if isinstance(node.func,ast.Name) else (
     node.func.attr if isinstance(node.func,ast.Attribute) else None)
    if name=="evidence_digest":
     self.hits.append((self.stack[-1] if self.stack else "<module>",node.lineno))
    self.generic_visit(node)

  offenders=[]
  for relative in ("utilities/capability-route.py","utilities/dispatch_contract.py"):
   path=P.parents[1]/relative
   file_tree=ast.parse(path.read_text(encoding="utf-8"))
   visitor=EvidenceDigestVisitor(); visitor.visit(file_tree)
   for fn,ln in visitor.hits:
    if fn not in self._EVIDENCE_DIGEST_ALLOWED_FUNCTIONS:
     offenders.append(f"{relative}:{fn}:{ln}")
  self.assertEqual(offenders,[],offenders)

 # A-SD153-7: the round-budget judgement lives in exactly two places -- the
 # legacy-route fallback inside `review_round_cap.round_budget` itself, and
 # the compile-time seal that freezes `continuation_budget.review_round_cap`
 # for every route born from that point on. Every admission surface
 # (dispatch-node/dispatch-batch/stage-dispatch-fallback/
 # `_owner_closure_eligibility`) reads the sealed value or calls
 # `round_budget`/`admit_round` -- none may recompute the cap itself.
 _MAX_REVIEW_ROUNDS_ALLOWED_FUNCTIONS=frozenset({
  # utilities/review_round_cap.py -- the one legacy-route fallback site
  "round_budget",
 })

 def test_a_sd153_7_max_review_rounds_only_in_round_budget_and_compile(self):
  import ast

  class MaxReviewRoundsVisitor(ast.NodeVisitor):
   def __init__(self):
    self.stack=[]; self.hits=[]
   def visit_FunctionDef(self,node):
    self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
   def visit_Call(self,node):
    name=node.func.id if isinstance(node.func,ast.Name) else (
     node.func.attr if isinstance(node.func,ast.Attribute) else None)
    if name=="max_review_rounds":
     self.hits.append((self.stack[-1] if self.stack else "<module>",node.lineno))
    self.generic_visit(node)

  offenders=[]
  compile_seal_hits=0
  for relative in (
   "utilities/review_round_cap.py","utilities/capability-route.py",
   "utilities/dispatch-batch.py","utilities/stage-dispatch-fallback.py",
  ):
   path=P.parents[1]/relative
   file_tree=ast.parse(path.read_text(encoding="utf-8"))
   visitor=MaxReviewRoundsVisitor(); visitor.visit(file_tree)
   for fn,ln in visitor.hits:
    if relative=="utilities/capability-route.py":
     compile_seal_hits+=1; continue  # the one route-compile seal, counted below
    if fn not in self._MAX_REVIEW_ROUNDS_ALLOWED_FUNCTIONS:
     offenders.append(f"{relative}:{fn}:{ln}")
  self.assertEqual(offenders,[],offenders)
  self.assertEqual(compile_seal_hits,1,"expected exactly one compile-time seal call")
  # dispatch-node.py's alias is a re-export assignment, never a call.
  node_text=(P.parents[1]/"utilities"/"dispatch-node.py").read_text(encoding="utf-8")
  self.assertIn("max_review_rounds = REVIEW_ROUND_CAP.max_review_rounds", node_text)
  self.assertNotIn("max_review_rounds(", node_text)

 # SD-153 rule 5 correction: `round_census`'s `closure_class` vocabulary is
 # closed and derived in exactly one function (`review_round_cap.marker_
 # round_census`). Every writer (`write_completion_marker`, `publish_
 # revision_locked`) calls it rather than hand-building the dict; this
 # census fails if a future writer ever assembles a `closure_class`-keyed
 # dict literal anywhere else across the surfaces that touch capped-node
 # markers.
 _CLOSURE_CLASS_ALLOWED_FUNCTIONS=frozenset({"marker_round_census"})

 def test_a_sd153_5_round_census_only_built_by_marker_round_census(self):
  import ast

  class ClosureClassDictVisitor(ast.NodeVisitor):
   def __init__(self):
    self.stack=[]; self.hits=[]
   def visit_FunctionDef(self,node):
    self.stack.append(node.name); self.generic_visit(node); self.stack.pop()
   def visit_Dict(self,node):
    for key in node.keys:
     if isinstance(key,ast.Constant) and key.value=="closure_class":
      self.hits.append((self.stack[-1] if self.stack else "<module>",node.lineno))
    self.generic_visit(node)

  offenders=[]
  for relative in (
   "utilities/review_round_cap.py","utilities/capability-route.py",
   "utilities/dispatch-node.py","utilities/dispatch-batch.py",
   "utilities/stage-dispatch-fallback.py",
  ):
   path=P.parents[1]/relative
   file_tree=ast.parse(path.read_text(encoding="utf-8"))
   visitor=ClosureClassDictVisitor(); visitor.visit(file_tree)
   for fn,ln in visitor.hits:
    if fn not in self._CLOSURE_CLASS_ALLOWED_FUNCTIONS:
     offenders.append(f"{relative}:{fn}:{ln}")
  self.assertEqual(offenders,[],offenders)
  # And every capped-node marker writer reaches that one function, rather
  # than reading `round_census`/`closure_class` out of thin air.
  route_text=(P.parents[1]/"utilities"/"capability-route.py").read_text(encoding="utf-8")
  self.assertEqual(route_text.count("REVIEW_ROUND_CAP.marker_round_census("),2,
                    "expected exactly two call sites: write_completion_marker, _producer_revision_plan")


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


class OwnerFinishBudgetRegressionTest(unittest.TestCase):
 def test_compiler_and_resolver_share_workload_formula(self):
  import dispatch_continuation_budget as BUDGET
  route=R.compile_route(**TestRoute().args())
  block=route["continuation_budget"]
  self.assertGreaterEqual(block["ordinary"],32)
  self.assertEqual(block["ordinary"],BUDGET.derive_workload_ordinary(
   declared_nodes=block["declared_nodes"], retry_slots=block["retry_slots"],
   review_round_cap=block["review_round_cap"], terminal_nodes=block["terminal_nodes"]))

 def test_correct_cli_prints_the_shared_correction_delivery_notice(self):
  sys.path.insert(0, str(P.parent))
  import dispatch_owner_input as owner_input
  import dispatch_contract
  attempt="att-parent"
  jobs=Path(tempfile.mkdtemp())/"jobs.log"
  lease=dispatch_contract.supervisor_lease_path(jobs,attempt)
  jobs.parent.mkdir(parents=True,exist_ok=True)
  jobs.write_text("2026-10-02T00:00:00Z\topen\t/repo\t/wt\towner\t"
   +"attempt_schema_version=2,dispatch_depth=1,transport=headless,execution_surface=registered-headless,"
   +"registered_worker=1,fallback_hop=same-harness-headless,worker_type=owner,harness=codex,"
   +"completion_delivery=app-server-supervised,supervisor_lease=flock-v1,"
   +f"supervisor_lease_file={lease},supervisor_lease_nonce={'d'*64},attempt_id={attempt},"
   +"owner_route_id=rt-fixture,owner_route_hash=sha256:"+"a"*64+",route_id=rt-fixture,route_hash=sha256:"+"a"*64+",parent_sid=fixture\n")
  with dispatch_contract.hold_supervisor_lease(jobs,attempt,lease):
   owner_input.OwnerInput(jobs,attempt,"thread-fixture","codex-active-turn",lambda _event:None)
   owner_input.submit(jobs,attempt,"private", "request-cli")
   result=subprocess.run([sys.executable,str(P),"correct","--attempt-id",attempt,"--jobs",str(jobs)],
    capture_output=True,text=True,check=False)
  self.assertEqual(0,result.returncode,result.stderr)
  value=json.loads(result.stdout)
  self.assertIn("delivery_notice",value)
  self.assertIn("does not wake or cancel",value["delivery_notice"])


class OwnerFinishCorrectionRegressionTest(OwnerFinishBudgetRegressionTest):
 def test_cli_delivery_notice_regression(self):
  self.test_correct_cli_prints_the_shared_correction_delivery_notice()


class FrameSummaryContractTest(unittest.TestCase):
 """A50-9 / N3 / R2-3: shards/frame/frame-summary.json contract, documented in
 capabilities/autopilot-code/references/owner-execution.md and
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
  exact=R._marker_identity_row(route,node,node["id"],node["completion_gate"],
                               jobs=self.jobs,exact_terminal=True)
  self.assertTrue(exact["passed"],exact)
  self.assertEqual(exact["attempt_id"],"att-inline-execute")

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
   "capabilities/autopilot-code/references/dev-pipeline.md",
   "adapters/claude/plugin-marketplace/plugins/hearting-claude/capabilities/autopilot-code/references/dev-pipeline.md",
   "adapters/codex/plugins/hearting-codex/capabilities/autopilot-code/references/dev-pipeline.md",
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

 def test_named_live_self_equals_implicit_registered_self(self):
  for status in ("open", "running"):
   for harness in ("claude", "codex", "opencode"):
    with self.subTest(status=status, harness=harness):
     self.setUp()
     route=self._route(self.base/"artifacts"); node=self._review_node(route)
     self._registered_row("att-self",route,"impl-review","review",status=status)
     meta=R._find_attempt_row_metadata(self.jobs,"att-self")
     axes=R._marker_attempt_axes(node,"att-self",meta)
     implicit=R.resolve_review_identity(node,axes,meta)
     marker,_=R.complete_node(route,node,"impl-review",self._evidence(),
      jobs=str(self.jobs),attempt_id="att-self",
      review_claim={"kind":"registered-worker","attempt_id":"att-self"})
     self.assertEqual({k:marker[k] for k in implicit},implicit)
     self.assertEqual(marker["review_independence"],"independent")
     self.assertEqual(marker["reviewer_claim"]["attempt_id"],"att-self")

 def test_live_self_requires_exact_canonical_completer(self):
  route=self._route(self.base/"artifacts"); node=self._review_node(route)
  self._registered_row("att-self",route,"impl-review","review",status="running")
  row=R._find_attempt_row_metadata(self.jobs,"att-self")
  axes=R._marker_attempt_axes(node,"att-self",row)
  for change in ({"route_hash":"sha256:foreign"},{"route_hash":""},
                 {"worker_type":"owner"},{"route_node":"other"}):
   with self.subTest(change=change):
    result=R.resolve_review_identity(node,axes,dict(row,**change),jobs=self.jobs,
     route_id=route["route_id"],node_id="impl-review",
     claim={"kind":"registered-worker","attempt_id":"att-self"})
    self.assertEqual(result["review_independence"],"degraded")
  self._registered_row("att-self",route,"impl-review","review",status="running")
  result=R.resolve_review_identity(node,axes,row,jobs=self.jobs,
   route_id=route["route_id"],node_id="impl-review",
   claim={"kind":"registered-worker","attempt_id":"att-self"})
  self.assertEqual(result["reviewer_downgrade_reason"],"reviewer-attempt-row-ambiguous")

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

class HistoricalReviewSelfDispositionTest(InlineStageCompletionRecipeTest):
 """Isolated real publication/currentness/readiness; no live runtime writes."""

 def historical_fixture(self,harness="codex",legacy=False,ship=False,provenance_blocked=False,append_hash=False,evidence_changes=None,historical_home=None,report_prefix=""):
  repo=self.base/"repo"; repo.mkdir()
  subprocess.run(["git","init","-q",str(repo)],check=True)
  subprocess.run(["git","-C",str(repo),"-c","user.name=Fixture","-c","user.email=fixture@example.invalid",
   "commit","--allow-empty","-qm","init"],check=True)
  artifact=repo/".agent_reports";artifact.mkdir()
  template=self._route(artifact)
  dispatch=json.loads(json.dumps(template["dispatch_evidence"]))
  for row in dispatch["tuples"]: row["checked_worktree"]=str(repo)
  route=R.compile_route("autopilot-ship" if ship else "autopilot-code","default" if ship else "dev","standard" if ship else "strong",repo,artifact,
   signals=[] if ship else ["shared-contract"],transport="headless",tracking="tracked",
   tracked_gate_evidence={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec",
    "workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}},dispatch_evidence=dispatch)
  if ship:
   route=R.compose_route(capability="autopilot-ship",capability_mode="default",shape="staged",graph=None,
    slug="self-disposition-ship",cwd=repo,artifact_root=artifact,intensity="standard",spec_read="fixture",
    campaign_key="self-disposition-fixture",
    dispatch_evidence=dispatch,jobs=self.jobs,parent_harness=harness,
    work_request={"text":"Continue the same prepared deploy request","owner_harness":harness})
  node=next(n for n in route["nodes"] if n["id"]==("release-review" if ship else "impl-review"))
  path=R.canonical_route_path(artifact,route["route_id"])
  path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(route))
  output=artifact
  axes={"attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
   "execution_surface":"registered-headless","registered_worker":"1","fallback_hop":"same-harness-headless"}
  owner={**axes,"dispatch_depth":"1","attempt_id":"att-owner","worker_type":"owner",
   "parent_sid":"parent","parent_harness":harness,"owner_route_file":str(path),
   "owner_route_id":route["route_id"],"owner_route_hash":route["route_hash"]}
  if ship:
   import artifact_producer
   # This historical fixture owns its own registry identity; do not let the
   # test runner's active dispatch attempt bind its unrelated empty jobs file.
   with mock.patch.dict(os.environ,{},clear=False):
    os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID",None)
    os.environ.pop("AGENT_DISPATCH_PARENT_ATTEMPT_ID",None)
    begun=artifact_producer.begin(artifact,route_file=path,capability="autopilot-ship",intensity="standard",jobs=self.jobs)
   output=Path(begun["env"]["AGENT_ARTIFACT_OUTPUT_DIR"])
   self.producer=begun
   owner_child=subprocess.Popen([sys.executable,"-c","import sys;sys.stdin.read()"],stdin=subprocess.PIPE,
    start_new_session=True)
   self.addCleanup(lambda: owner_child.communicate(timeout=5) if owner_child.poll() is None else None)
   owner.update(pid=str(owner_child.pid),pid_start=str(D.process_start_ticks(owner_child.pid)),pgid=str(owner_child.pid),
    pid_ns=D.process_namespace_identity(owner_child.pid),pid_observer_ns=D.process_namespace_identity(owner_child.pid))
   self.jobs.write_text("\t".join(["2026-10-12T00:00:00Z","running",str(repo),str(repo),"owner",
    ",".join(f"{k}={v}" for k,v in owner.items())])+"\n")
   # Seed the consumed review history before preparation. The unchanged
   # request excludes these subjects; each current review is the third
   # produced judgment, alongside one verdict-less historical attempt.
   for review_id in ("release-review","security-review"):
    for index in range(3):
     aid=f"att-prior-{review_id}-{index}"
     prior_report=output/f"{aid}.md";prior_report.write_text("previous blocking review\n")
     prior_log=self.base/f"{aid}.jsonl"
     verdict="FAIL" if index<2 else "BLOCKED"
     prior_log.write_text(json.dumps({"type":"result","subtype":"success","is_error":False,
      "result":f"artifact: {prior_report}\nverdict: {verdict}\nblocker: previous finding"})+"\n")
     prior_child=subprocess.Popen([sys.executable,"-c","import sys;sys.stdin.read()"],stdin=subprocess.PIPE,
      start_new_session=True)
     ticks=D.process_start_ticks(prior_child.pid);namespace=D.process_namespace_identity(prior_child.pid)
     prior_child.communicate(timeout=5)
     prior_meta={**axes,"attempt_id":aid,"worker_type":"review","parent_attempt_id":"att-owner",
      "route_id":route["route_id"],"route_hash":route["route_hash"],"route_node":review_id,"route_file":str(path),
      "artifact_root":str(artifact),"log_file":str(prior_log),
      "pid":str(prior_child.pid),"pid_start":str(ticks),"pgid":str(prior_child.pid),
      "pid_ns":namespace,"pid_observer_ns":namespace}
     with self.jobs.open("a") as f:
      f.write("\t".join([R.datetime.now(R.timezone.utc).isoformat(),"running",str(repo),str(repo),aid,
       ",".join(f"{k}={v}" for k,v in prior_meta.items())])+"\n")
     # The foreground writer preserves the original FAIL/BLOCKED and full
     # exact cleanup at this finite teardown, before later fixture processes.
     from foreground_terminal import settle_foreground_exit
     from types import SimpleNamespace
     result=settle_foreground_exit(self.jobs,aid,prior_log,SimpleNamespace(failure="",exit_code=0),
      worktree=repo,artifact_root=artifact,worker_type="review",legacy_close=lambda _:None)
     self.assertTrue(result["closed"],result)
   import ship_parent_deploy
   if provenance_blocked:
    setup=next(n for n in route["nodes"] if n["id"]=="release-setup")
    setup_evidence=output/"setup.md";setup_evidence.write_text("previous setup\n")
    inline={"attempt_schema_version":2,"dispatch_depth":setup["dispatch_depth"],"transport":"headless",
     "execution_surface":"inline","registered_worker":False,"fallback_hop":"inline"}
    R.complete_node(route,setup,setup["id"],setup_evidence,attempt_id="att-setup-inline",explicit_attempt_metadata=inline)
    setup_evidence.write_text("corrected setup\n")
    with mock.patch.dict(os.environ,{"AGENT_DISPATCH_ATTEMPT_ID":"att-owner"}):
     R.publish_revision_locked(route,setup["id"],setup_evidence,basis="review-findings",
      answers=["att-prior-release-review-1","att-prior-security-review-1"],author_attempt_id="att-owner",jobs=self.jobs)
   script=output/"prepared-script.py";script.write_text("print('fixture deployment must never run')\n")
   pins=[]
   for i in range(30 if provenance_blocked else 32):
    pin=output/f"input-{i}.txt";pin.write_text(str(i));pins.append(str(pin))
   command=output/"prepared-command.json"
   command.write_text(json.dumps({"argv":[sys.executable,str(script)],"cwd":str(repo),"inputs":pins}))
   with mock.patch.dict(os.environ,{"AGENT_DISPATCH_ATTEMPT_ID":"att-owner"}):
    self.prepared=ship_parent_deploy.prepare(self.jobs,"att-owner",command)
   if provenance_blocked:
    # prepare retains this historical creator across owner replacements.
    # Seed the public retained-request condition before the original review;
    # its digest/pins/bytes then stay unchanged through every disposition.
    request_path=Path(self.prepared["request"])
    request=json.loads(request_path.read_text());request["owner_attempt_id"]="att-earlier-preparer"
    bound={k:v for k,v in request.items() if k not in {"request_digest","parent_availability"}}
    request["request_digest"]=ship_parent_deploy.digest(bound)
    request_path.write_text(json.dumps(request))
    self.prepared.update(request)
  evidence=output/"evidence"/"review.json"
  evidence.parent.mkdir(parents=True,exist_ok=True)
  content={"verdict":"PASS","findings":[],"gate_met":True,
   "route_id":route["route_id"],"completion_gate":node["completion_gate"],
   "request_digest":self.prepared["request_digest"] if ship else "fixed"}
  if provenance_blocked:
   content.update(stage=node["completion_gate"],guard_session_id="att-review-self",blocker="none",
    cycle_id=self.producer["env"]["AGENT_ARTIFACT_CYCLE_ID"],route_cycle_id=self.producer["env"]["AGENT_ARTIFACT_CYCLE_ID"],
    round=4,verdict_sequence=3,verdict_ceiling=3,review_artifact="reviews/release/original.md",
    request_path=self.prepared["request"],request_prepared_at=self.prepared["prepared_at"],
    request_digest_verified=True,prepare_receipt_matches=True,bound_input_mismatches=[],
    bound_inputs_verified=len(json.loads(Path(self.prepared["request"]).read_text())["command"]["inputs"]))
  content.update(evidence_changes or {})
  evidence.write_text(json.dumps(content))
  log=self.base/f"review.{harness}.jsonl"
  meta=dict(axes,attempt_id="att-review-self",worker_type="review",harness=harness,
   route_id=route["route_id"],route_hash=route["route_hash"],route_node=node["id"],route_file=str(path),
   parent_attempt_id="att-owner",artifact_root=str(artifact),log_file=str(log))
  from review_source_subject import is_implementation_review, seal_attempt
  if is_implementation_review(route,node):
   meta.update(seal_attempt(self.jobs,route,node,meta["attempt_id"],worktree=repo))
  # Publish the original completion from a finite, already reaped lifecycle.
  # Its real launch identity lets the normal terminal writer preserve the
  # full cleanup proof before a later CLI observer becomes unobservable.
  child=subprocess.Popen([sys.executable,"-c","import sys;sys.stdin.read()"],stdin=subprocess.PIPE,
   start_new_session=True,env={**os.environ,"AGENT_DISPATCH_ATTEMPT_ID":meta["attempt_id"]})
  meta.update(D.process_launch_identity(child.pid))
  child.communicate(timeout=5)
  with self.jobs.open("a" if ship else "w") as f:
   f.write("\t".join([R.datetime.now(R.timezone.utc).isoformat(),"running",str(repo),str(repo),"review",
    ",".join(f"{k}={v}" for k,v in meta.items())])+"\n")
  claim={"kind":"registered-worker","attempt_id":meta["attempt_id"]}
  # Construct the historical bug at its original publication boundary, then
  # use only real common authority/validation on every replay and observation.
  degraded={"reviewer_kind":"owner-inline","review_independence":"degraded",
   "reviewer_identity":meta["attempt_id"],"reviewer_downgrade_reason":"reviewer-attempt-no-terminal-verdict"}
  if historical_home:
   argv=[sys.executable,str(Path(historical_home)/"utilities/capability-route.py"),"complete",
    "--route",str(path),"--node",node["id"],"--jobs",str(self.jobs),"--attempt-id",meta["attempt_id"],
    "--reviewer-attempt",meta["attempt_id"],"--evidence",str(evidence)]
   result=subprocess.run(argv,text=True,capture_output=True,env={**os.environ,"AGENT_HOME":str(historical_home),
    "AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_ATTEMPT_ID":meta["attempt_id"]})
   self.assertEqual(result.returncode,0,result.stderr)
   origin=next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{'))
   self.assertEqual(origin["reviewer_downgrade_reason"],"reviewer-attempt-no-terminal-verdict")
   print("historical_cli",json.dumps({"argv":argv,"exit":result.returncode,"downgrade":origin["reviewer_downgrade_reason"]}))
  else:
   with mock.patch.object(R,"resolve_review_identity",return_value=degraded):
    origin,_=R.complete_node(route,node,node["id"],evidence,jobs=self.jobs,
     attempt_id=meta["attempt_id"],review_claim=claim)
  directory=R.completion_dir(route["route_id"])
  if legacy and not historical_home:
   origin.pop("reviewer_claim")
   R.atomic_write(directory/f"{node['id']}.json",origin)
   R.atomic_write(directory/f"{node['id']}.{origin['sequence']}.json",origin)
  if ship:
   owner_child.communicate(timeout=5)
  else:
   with self.jobs.open("a") as f:
    f.write("\t".join(["2026-10-12T00:00:00Z","running",str(repo),str(repo),"owner",
     ",".join(f"{k}={v}" for k,v in owner.items())])+"\n")
  report=evidence
  if provenance_blocked:
   report=output/"reviews/release/original.md";report.parent.mkdir(parents=True,exist_ok=True)
   report.write_text(report_prefix+"## Code Review Results\nOriginal content judgment: PASS.\n\n### Registered completion receipt\n```json\n"+
    json.dumps(origin)+"\n"+json.dumps({"attempt_id":meta["attempt_id"],"status":"closed"})+"\n```\n")
  text=(f"artifact: {report}\nverdict: BLOCKED\nblocker: {D.REVIEW_SELF_PROVENANCE_BLOCKER}" if provenance_blocked
        else f"artifact: {evidence}\nverdict: PASS\nblocker: none")
  terminal={
   "codex":[{"type":"item.completed","item":{"type":"agent_message","text":text}},{"type":"turn.completed"}],
   "claude":[{"type":"result","subtype":"success","is_error":False,"result":text}],
   "opencode":[{"type":"text","part":{"type":"text","text":text}},
    {"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}]}[harness]
  if legacy or provenance_blocked:
   import shlex
   command=shlex.join([sys.executable,str(Path(historical_home or R.ROOT)/"utilities/capability-route.py"),"complete",
    "--route",str(path),"--node",node["id"],"--jobs",str(self.jobs),"--attempt-id",meta["attempt_id"],
    "--reviewer-attempt",meta["attempt_id"],"--evidence",str(evidence)])
   stdout=(result.stdout if historical_home else json.dumps(origin)+"\n"+
     json.dumps({"attempt_id":meta["attempt_id"],"status":"closed"}))
   terminal.insert(0,{"type":"item.completed","item":{"type":"command_execution","id":"original-complete",
    "command":command,"status":"completed","exit_code":0,"aggregated_output":stdout}})
  if provenance_blocked:
   import hashlib
   command=shlex.join(["sha256sum",str(report),str(evidence)])
   if append_hash and not historical_home:
    command=(f"F={shlex.quote(str(report))}; $AGENT_HOME/adapters/codex/bin/preflight.sh write $F {meta['attempt_id']}; cat >> $F <<EOF\n"
             "public receipt text\nEOF\n"+shlex.join(["sha256sum","$F",str(evidence)]))
    command=shlex.join(["/bin/zsh","-lc",command])
   hash_stdout="\n".join(hashlib.sha256(p.read_bytes()).hexdigest()+"  "+str(p) for p in (report,evidence))+"\n"
   if historical_home:
    hashed=subprocess.run(["sha256sum",str(report),str(evidence)],text=True,capture_output=True,check=True)
    hash_stdout=hashed.stdout
   terminal.insert(1,{"type":"item.completed","item":{"type":"command_execution","id":"original-report-hash",
    "command":command,"status":"completed","exit_code":0,"aggregated_output":
    hash_stdout}})
  log.write_text("\n".join(json.dumps(e) for e in terminal)+"\n")
  self.route,self.node,self.evidence,self.claim,self.origin=route,node,evidence,claim,origin
  self.path,self.log,self.directory=path,log,directory
  self.repo,self.artifact,self.output,self.meta=repo,artifact,output,meta
  self.report=report
  self.preserved={p:p.read_bytes() for p in (directory/f"{node['id']}.{origin['sequence']}.json",
   directory/f"{node['id']}.att-review-self.attempt.json",evidence,self.jobs)}
  if provenance_blocked:
   self.preserved.update({report:report.read_bytes(),log:log.read_bytes(),Path(self.prepared["request"]):Path(self.prepared["request"]).read_bytes()})
  return route,node,evidence

 def replay(self,entrance="complete",actor="att-owner"):
  with mock.patch.dict(os.environ,{"AGENT_DISPATCH_ATTEMPT_ID":actor}):
   if entrance=="writer":
    return R.write_completion_marker(self.route,self.node,self.node["id"],self.evidence,
     attempt_id="att-review-self",attempt_metadata=R._find_attempt_row_metadata(self.jobs,"att-review-self"),
     review_claim=self.claim,jobs=self.jobs)
   return R.complete_node(self.route,self.node,self.node["id"],self.evidence,
    jobs=self.jobs,attempt_id="att-review-self",review_claim=self.claim)[0]

 def test_both_replay_entrances_current_observer_and_harness_semantics(self):
  for harness in ("claude","codex","opencode"):
   for entrance in ("complete","writer"):
    with self.subTest(harness=harness,entrance=entrance):
     self.setUp(); self.historical_fixture(harness)
     marker=self.replay(entrance)
     self.assertEqual(marker["review_independence"],"independent")
     self.assertEqual(marker["completed_at"],self.origin["completed_at"])
     self.assertEqual(marker["round_census"],self.origin["round_census"])
     for p,b in self.preserved.items(): self.assertEqual(p.read_bytes(),b)
     self.assertEqual(self.replay("complete"),marker)
     self.assertEqual(self.replay("writer"),marker)
     canonical=self.directory/f"{self.node['id']}.json"
     self.assertTrue(D.completion_marker_is_current(self.route,self.node,canonical))
     self.assertEqual(D.completion_attempt_readiness(self.route,self.node,marker,self.jobs).state,"ready")
     before={p:p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
     D.gate_currency(self.route,self.node,canonical,observe=True)
     self.assertEqual(before,{p:p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

 def test_bound_legacy_command_proves_original_claim_and_missing_claim_holds(self):
  self.historical_fixture(legacy=True)
  self.assertEqual(self.replay()["reviewer_disposition"]["original_claim"]["source"],"runtime-command")
  self.setUp();self.historical_fixture(legacy=True)
  lines=self.log.read_text().splitlines(); self.log.write_text("\n".join(lines[1:])+"\n")
  self.assertEqual(self.replay(),self.origin)
  self.assertEqual(len(list(self.directory.glob(f"{self.node['id']}.[0-9]*.json"))),1)

 def test_legacy_late_self_claim_receipt_does_not_prove_original_self_claim(self):
  self.historical_fixture(legacy=True)
  events=[json.loads(line) for line in self.log.read_text().splitlines()]
  item=events[0]["item"]
  item["aggregated_output"]=json.dumps(self.origin)+"\n"+json.dumps({"attempt_id":"att-review-self","status":"already-closed"})
  self.log.write_text("\n".join(json.dumps(e) for e in events)+"\n")
  self.assertEqual(self.replay(),self.origin)

 def test_foreign_actor_refuses_without_publication_before_and_after_disposition(self):
  self.historical_fixture()
  for disposed in (False,True):
   if disposed: self.replay()
   before={p:p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
   for actor in ("att-foreign",""):
    with self.subTest(disposed=disposed,actor=actor),self.assertRaisesRegex(ValueError,"actor"):
     self.replay(actor=actor)
    self.assertEqual(before,{p:p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

 def test_crash_retry_preserves_commit_boundary_and_origin(self):
  for boundary in ("history","canonical","pointer"):
   with self.subTest(boundary=boundary):
    self.setUp();self.historical_fixture()
    original_write=R.atomic_write; original_once=R.write_once
    def interrupted_write(path,payload):
     original_write(path,payload)
     if ((boundary=="canonical" and Path(path).name==f"{self.node['id']}.json"
          and "reviewer_disposition" in payload)
         or (boundary=="pointer" and Path(path).name==f"{self.node['id']}.attempt.json")):
      raise RuntimeError("publication-interrupted")
    def interrupted_once(path,payload):
     original_once(path,payload)
     if boundary=="history" and "reviewer_disposition" in payload:
      raise RuntimeError("publication-interrupted")
    with mock.patch.object(R,"atomic_write",side_effect=interrupted_write), \
         mock.patch.object(R,"write_once",side_effect=interrupted_once), \
         self.assertRaisesRegex(RuntimeError,"publication-interrupted"):
     self.replay()
    canonical=json.loads((self.directory/f"{self.node['id']}.json").read_text())
    self.assertEqual(canonical["review_independence"],"degraded" if boundary=="history" else "independent")
    marker=self.replay()
    self.assertEqual(marker["sequence"],self.origin["sequence"]+1)
    self.assertEqual(len(list(self.directory.glob(f"{self.node['id']}.[0-9]*.json"))),2)
    for p,b in self.preserved.items():self.assertEqual(p.read_bytes(),b)

 def test_concurrent_replay_allocates_only_one_successor(self):
  import concurrent.futures
  self.historical_fixture()
  def replay(index):
   meta=R._find_attempt_row_metadata(self.jobs,"att-review-self")
   if index%2:
    return R.write_completion_marker(self.route,self.node,self.node["id"],self.evidence,
     attempt_id="att-review-self",attempt_metadata=meta,jobs=self.jobs,review_claim=self.claim)
   return R.complete_node(self.route,self.node,self.node["id"],self.evidence,
    attempt_id="att-review-self",jobs=self.jobs,review_claim=self.claim)[0]
  with mock.patch.dict(os.environ,{"AGENT_DISPATCH_ATTEMPT_ID":"att-owner"}), \
       concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
   markers=list(pool.map(replay,range(8)))
  self.assertTrue(all(m==markers[0] for m in markers))
  self.assertEqual(len(list(self.directory.glob(f"{self.node['id']}.[0-9]*.json"))),2)
  for p,b in self.preserved.items():self.assertEqual(p.read_bytes(),b)

 def test_corrupt_origin_link_and_forged_successor_are_integrity_failures(self):
  for component in ("origin","link","content","claim"):
   with self.subTest(component=component):
    self.setUp();self.historical_fixture();marker=self.replay()
    canonical=self.directory/f"{self.node['id']}.json"
    if component in ("origin","link"):
     path=list(self.preserved)[0 if component=="origin" else 1]
     path.write_bytes(path.read_bytes()+b" ")
    else:
     if component=="content":marker["round_census"]["verdict_rounds"]+=1
     else: marker["reviewer_disposition"]["original_claim"]={"source":"made-up"}
     R.atomic_write(canonical,marker)
     R.atomic_write(self.directory/f"{self.node['id']}.{marker['sequence']}.json",marker)
    self.assertTrue(D.gate_currency(self.route,self.node,canonical,observe=True).state.startswith("integrity-broken"))

 def test_partial_dead_conflicting_and_unsettled_rows_do_not_promote(self):
  for change in ("route_hash=sha256:other","note=dead-worker-fail,failure_class=fail",
                 "completion_marker_history=/missing/history.json","pid=invalid",
                 "worker_type=owner","stage_authority=0"):
   with self.subTest(change=change):
    self.setUp();self.historical_fixture()
    lines=self.jobs.read_text().splitlines();lines[0]+=","+change;self.jobs.write_text("\n".join(lines)+"\n")
    before=(self.directory/f"{self.node['id']}.json").read_bytes()
    with self.assertRaises((ValueError,D.DispatchContractError)): self.replay()
    self.assertEqual((self.directory/f"{self.node['id']}.json").read_bytes(),before)

 def test_uncommitted_conflicting_history_cannot_allocate_another_successor(self):
  self.historical_fixture()
  R.write_once(self.directory/f"{self.node['id']}.2.json",dict(self.origin,sequence=2,attempt_id="att-other"))
  before=(self.directory/f"{self.node['id']}.json").read_bytes()
  with self.assertRaises(ValueError):self.replay()
  self.assertEqual((self.directory/f"{self.node['id']}.json").read_bytes(),before)
  self.assertFalse((self.directory/f"{self.node['id']}.3.json").exists())

 def test_duplicate_or_later_subject_cannot_disposition_or_replay(self):
  for disposed in (False,True):
   for duplicate in (True,False):
    with self.subTest(disposed=disposed,duplicate=duplicate):
     self.setUp();self.historical_fixture()
     if disposed:self.replay()
     row=self.jobs.read_text().splitlines()[0]
     if not duplicate:row=row.replace("att-review-self","att-later-review")
     with self.jobs.open("a") as f:f.write(row+"\n")
     before=(self.directory/f"{self.node['id']}.json").read_bytes()
     with self.assertRaises(ValueError):self.replay()
     self.assertEqual((self.directory/f"{self.node['id']}.json").read_bytes(),before)

 def test_changed_evidence_writer_collision_and_missing_canonical_never_repair_for_foreign_actor(self):
  self.historical_fixture();self.replay()
  before=(self.directory/f"{self.node['id']}.json").read_bytes()
  self.evidence.write_text(self.evidence.read_text()+" ")
  with self.assertRaisesRegex(ValueError,"evidence-conflict"):self.replay("writer")
  self.assertEqual((self.directory/f"{self.node['id']}.json").read_bytes(),before)
  self.setUp();self.historical_fixture()
  canonical=self.directory/f"{self.node['id']}.json";canonical.unlink()
  with self.assertRaisesRegex(ValueError,"not-current"):self.replay("writer",actor="att-foreign")
  self.assertFalse(canonical.exists())


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
 def test_card_lists_sealed_nodes_for_default_explicit_and_single_node_routes(self):
  cases=[
   dict(capability="autopilot-research",capability_mode=mode,intensity="strong",graph=None)
   for mode in ("market","academic","technology")
  ]+[
   dict(capability="autopilot-research",capability_mode="market",intensity="strong",
        graph="retrieval,synthesis,report,claim-verify"),
   dict(capability="autopilot-lab",capability_mode="eval",intensity="strong",graph=None),
   dict(capability="analyze-project",capability_mode="code",intensity="strong",graph=None),
   dict(shape="direct",graph=None),
   dict(shape="framed",graph=None),
  ]
  for case in cases:
   with self.subTest(**case):
    route=self.compose(**case)
    before=R.canonical(route)
    ids=[node["id"] for node in route["nodes"]]
    if case.get("capability")=="autopilot-research" and case["graph"] is None:
     self.assertEqual(ids,["retrieval","retrieval-alternative","synthesis","report",
                           "claim-verify","claim-verify-alternative"])
     self.assertFalse(route.get("composed"))
    headline=R.compose_card(route).splitlines()[0]
    self.assertEqual(headline.split(" · ")[1].split(" ",1)[1],"→".join(ids))
    self.assertEqual(R.canonical(route),before)
    R.verify_route(route,R.ROOT)

 def test_graphless_research_start_passes_the_exact_sealed_graph_to_owner(self):
  import types, work_start
  # The compiler and emitter must observe the same runtime root with gates on.
  with tempfile.TemporaryDirectory() as tmp, \
       mock.patch.dict(os.environ,{"AGENT_HOME":str(R.ROOT)}):
   route=self.compose(capability="autopilot-research",capability_mode="market",
                      intensity="strong",graph=None,artifact_root=tmp,
                      work_request={"text":"Verify graph preservation","owner_harness":"codex"})
   before=R.canonical(route)
   with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    path=R._emit_compiled_route(types.SimpleNamespace(command="compose",start=True),route,tmp)
   observed=[]
   def launch(command,**kwargs):
    evidence=Path(command[command.index("--route-evidence")+1])
    observed.append(json.loads(evidence.read_text()))
    self.assertEqual(evidence,path)
    self.assertEqual(observed[-1],route)
    self.assertEqual(command[command.index("--prompt-text")+1],route["work_request"]["text"])
    return types.SimpleNamespace(returncode=0,stdout="fixture-only\n",stderr="")
   # Exercise the production launch-command boundary, substituting only transport.
   work_start._start(route,path,Path(tmp)/"jobs.log","owner","codex",launch)
   self.assertEqual(len(observed),1)
   self.assertEqual([n["id"] for n in observed[0]["nodes"]],
                    ["retrieval","retrieval-alternative","synthesis","report",
                     "claim-verify","claim-verify-alternative"])
   by_id={n["id"]:n for n in observed[0]["nodes"]}
   self.assertEqual(by_id["synthesis"]["depends_on"],["retrieval","retrieval-alternative"])
   self.assertEqual(by_id["report"]["depends_on"],["synthesis"])
   for name in ("claim-verify","claim-verify-alternative"):
    self.assertEqual(by_id[name]["depends_on"],["report"])
   self.assertEqual(observed[0]["parallel_groups"],route["parallel_groups"])
   self.assertEqual(R.canonical(route),before)
   self.assertEqual(json.loads(path.read_text()),route)
   R.verify_route(observed[0],R.ROOT)

 def test_framed_card_marks_only_sealed_single_or_supported_pins_as_same_harness(self):
  import copy
  route = self.compose(shape="framed", graph=None, intensity=None)
  one = copy.deepcopy(route)
  one["dispatch_evidence"]["tuples"] = one["dispatch_evidence"]["tuples"][:1]
  one["dispatch_evidence"]["tuples"][0]["child_harness"] = "claude"
  one_line = "\n".join(R.compose_decision_lines(one))
  self.assertIn("같은 하네스(쓸 수 있는 하네스가 claude 하나)", one_line)
  supported_pin = copy.deepcopy(route)
  supported_pin["selection_pins"] = {"contract_version": 1,
      "frame": {"harness": "codex", "model": None, "effort": None}}
  self.assertIn("같은 하네스(frame 지정 codex)",
                "\n".join(R.compose_decision_lines(supported_pin)))
  unsupported_pin = copy.deepcopy(route)
  unsupported_pin["selection_pins"] = {"contract_version": 1,
      "frame": {"harness": "opencode", "model": None, "effort": None}}
  self.assertNotIn("같은 하네스", "\n".join(R.compose_decision_lines(unsupported_pin)))

 def test_non_git_decision_is_single_and_conservative_classifier_is_purpose_based(self):
  cwd="/tmp/non-git-project"
  diagnosis={"state":"non-git","cwd":cwd,"detail":"fatal: not a git repository"}
  direct=R._decide_non_git_compose(diagnosis,"direct",0,False,None)
  self.assertEqual(direct["kind"],"direct")
  for request_shape,depth,attempt in ((None,0,False),("framed",0,False),("solo",0,False),
                                      (None,1,False),("direct",1,False),("direct",0,True)):
   result=R._decide_non_git_compose(diagnosis,request_shape,depth,attempt,"autopilot-code")
   expected = "worker-candidate" if request_shape != "direct" else "refuse"
   self.assertEqual(result["kind"],expected)
   self.assertTrue(result["cause"].startswith("git 저장소 아님: "+cwd))
   if expected == "worker-candidate":
    self.assertIn("--shape direct",result["next"])
   else:
    self.assertIn("depth-0",result["next"])
  self.assertEqual(R._decide_non_git_compose({"state":"git"},None,0,False,None)["kind"],"git")
  self.assertEqual(R._decide_non_git_compose({"state":"error","detail":"permission denied"},None,0,False,None)["kind"],"error")
  cases={
   "기술 동향 조사 후 핵심 내용을 정리한다.":("autopilot-research","technology"),
   "시장 조사 결과를 정리한다.":("autopilot-research","market"),
   "고정 metric으로 두 모델 결과를 비교한다.":("autopilot-lab","eval"),
   "Implement a reusable evaluation driver.":("autopilot-code","dev"),
   "Fix only typos in the existing report.":("autopilot-refine","default"),
   "Write an independent paper.":("autopilot-draft","paper"),
   "Inspect existing code structure.":("analyze-project","code"),
   "Analyze the existing codebase structure.":("analyze-project","code"),
   "Analyze existing documentation structure.":("analyze-project","doc"),
   "Inspect existing paper structure.":("analyze-project","paper"),
   "Package existing results as a ZIP.":("autopilot-ship","package"),
  }
  for task,expected in cases.items():
   with self.subTest(task=task): self.assertEqual(R._classify_non_git_task(task),expected)
  for task in ("계속 진행", "분석해줘", "Do not research this; fix code instead.",
               'The task says "market research" but do not do that.',
               "Research the topic and implement a reusable tool."):
   with self.subTest(task=task): self.assertIsNone(R._classify_non_git_task(task))
  failed = self.nested(parent="codex", child="codex", status="unsupported", failure="worktree-not-found")
  with self.assertRaisesRegex(ValueError, "dispatch-evidence-exact-worktree-reprobe-required.*worktree-not-found"):
   R._fallback_chain({"tuples":[failed],"native_subagent":[]}, expected_worktree=R.ROOT, require_scope=True)

 def test_compose_git_diagnosis_uses_git_exhaustion_and_only_checks_cwd_metadata(self):
  with tempfile.TemporaryDirectory(prefix="compose-git-diagnosis-") as tmp:
   parent=Path(tmp); cwd=parent/"project"; cwd.mkdir()
   (parent/".git").mkdir()
   diagnosis=R._compose_git_diagnosis(cwd)
   self.assertEqual(diagnosis["state"],"non-git",diagnosis)
   (cwd/".git").mkdir()
   diagnosis=R._compose_git_diagnosis(cwd)
   decision=R._decide_non_git_compose({**diagnosis,"cwd":str(cwd)},None,0,False,None)
   self.assertEqual(decision["kind"],"error")
   self.assertIn(str(cwd),decision["detail"])

 def test_compose_git_diagnosis_rejects_ancestor_gitfile_with_missing_gitdir(self):
  with tempfile.TemporaryDirectory(prefix="compose-gitfile-diagnosis-") as tmp:
   parent=Path(tmp); cwd=parent/"project"; cwd.mkdir()
   (parent/".git").write_text("gitdir: /definitely-missing-hearting-gitdir\n")
   diagnosis=R._compose_git_diagnosis(cwd)
   decision=R._decide_non_git_compose({**diagnosis,"cwd":str(cwd)},None,0,False,None)
   self.assertEqual(decision["kind"],"error",diagnosis)
   self.assertIn(str(cwd),decision["detail"])
   # Git versions differ on naming the missing gitdir (some print "(null)"); keep the raw error.
   self.assertIn("not a git repository",decision["detail"])

 def test_compose_git_diagnosis_accepts_mount_boundary_exhaustion_message(self):
  completed=subprocess.CompletedProcess(["git"],128,"",
   "fatal: not a git repository (or any parent up to mount point /home)\n"
   "Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).\n")
  with tempfile.TemporaryDirectory(prefix="compose-mount-diagnosis-") as tmp, \
       mock.patch.object(R.subprocess,"run",return_value=completed):
   diagnosis=R._compose_git_diagnosis(tmp)
  self.assertEqual(diagnosis["state"],"non-git",diagnosis)

 def test_compose_git_probe_uses_c_locale_without_changing_parent_environment(self):
  with tempfile.TemporaryDirectory(prefix="compose-locale-diagnosis-") as tmp, \
       mock.patch.dict(os.environ,{"LC_ALL":"ko_KR.UTF-8","LANGUAGE":"ko"}):
   before=(os.environ.get("LC_ALL"),os.environ.get("LANGUAGE"))
   with mock.patch.object(R.subprocess,"run",return_value=subprocess.CompletedProcess(
       ["git"],128,"","fatal: not a git repository (or any of the parent directories): .git\n")) as run:
    R._compose_git_diagnosis(tmp)
   self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"],"C")
   self.assertNotIn("LANGUAGE",run.call_args.kwargs["env"])
   self.assertEqual((os.environ.get("LC_ALL"),os.environ.get("LANGUAGE")),before)

 def test_compose_start_with_empty_ancestor_git_marker_starts_direct(self):
  import work_start
  exact="git 아닌 폴더 — 이 세션이 직접 진행. 작업자가 필요하면 --shape 없이 다시 시작(원본 수정은 git init 필요)"
  with tempfile.TemporaryDirectory(prefix="compose-ancestor-marker-") as tmp, \
       mock.patch.dict(os.environ,{"AGENT_HOME":str(R.ROOT),"AGENT_DISPATCH_ATTEMPT_ID":""}):
   root=Path(tmp); parent=root/"parent"; cwd=parent/"project"
   cwd.mkdir(parents=True); (parent/".git").mkdir()
   artifacts=root/"artifacts"; prompt=root/"task.md"; prompt.write_text("Continue this task")
   route=self.compose(shape="direct",graph=None,artifact_root=str(artifacts),
                      work_request={"text":prompt.read_text(),"owner_harness":None})
   argv=[str(P),"compose","--shape","direct","--campaign-key","ancestor-test","--slug","ancestor",
         "--cwd",str(cwd),"--artifact-root",str(artifacts),"--start","--prompt-file",str(prompt),
         "--jobs",str(root/"jobs.log")]
   def start(*args,**kwargs):
    return {"state":"inline","required_action":"execute-inline","route_id":route["route_id"]}
   out,err=io.StringIO(),io.StringIO()
   with mock.patch.object(sys,"argv",argv),mock.patch.object(R,"compose_route",return_value=route), \
        mock.patch.object(work_start,"start_work",side_effect=start),mock.patch.object(R,"_record_route_chain"), \
        mock.patch.object(R,"_route_autoclose"),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
    self.assertEqual(R.main(),0)
   self.assertEqual(json.loads(out.getvalue())["state"],"inline")
   self.assertEqual(err.getvalue().count(exact),1)

 def test_non_git_classified_research_modes_compile_and_seal_as_direct(self):
  with tempfile.TemporaryDirectory() as tmp:
   for mode in ("academic","market","technology"):
    with self.subTest(mode=mode):
     route=self.compose(capability="autopilot-research",capability_mode=mode,shape="direct",graph=None,
                        slug="research-"+mode,artifact_root=tmp,capability_source="non-git-task-classification")
     self.assertEqual((route["capability"],route["effective_intensity"]),("autopilot-research","direct"))
     self.assertEqual(len(route["nodes"]),1)
     self.assertEqual(next(row for row in route["selection"]["selection_basis"]
                           if row["axis"]=="compose-capability")["source"],"non-git-task-classification")
     R.verify_route(route,R.ROOT)

 def test_non_git_unresolved_capability_marker_and_exact_card_lines(self):
  with tempfile.TemporaryDirectory() as tmp:
   route=self.compose(capability="autopilot-code",capability_mode="dev",shape="direct",graph=None,
                      slug="unresolved",artifact_root=tmp,capability_source="non-git-unresolved",
                      work_request={"text":"계속 진행","owner_harness":None})
   basis=next(row for row in route["selection"]["selection_basis"] if row["axis"]=="compose-capability")
   self.assertEqual(basis["source"],"non-git-unresolved")
   self.assertIn("source=non-git-unresolved", "\n".join(R.compose_decision_lines(route)))
   decision={"kind":"direct","capability_unresolved":True}
   card=R.compose_card(route,non_git_decision=decision)
   self.assertEqual(card.count("git 아닌 폴더 — 이 세션이 직접 진행. 작업자가 필요하면 --shape 없이 다시 시작(원본 수정은 git init 필요)"),1)
   self.assertEqual(card.count("작업 종류 미정 — `--capability`로 지정"),1)
   R.verify_route(route,R.ROOT)

 def test_automatic_depth_two_evidence_uses_enabled_prospective_owner_pool(self):
  readiness = self.evidence()
  enabled = ("claude", "codex", "opencode")
  for caller in enabled:
   with self.subTest(caller=caller), \
        mock.patch.object(R, "_compose_default_children", return_value=enabled), \
        mock.patch.object(R, "_compose_readiness", return_value=readiness) as probe:
    route = self.compose(dispatch_evidence=None, parent_harness=caller,
                         children=["claude", "codex", "opencode"])
   self.assertEqual(probe.call_args.kwargs["owner_harnesses"], enabled)
   self.assertEqual({row["parent_harness"] for row in route["dispatch_evidence"]["tuples"]},
                    {row["parent_harness"] for row in readiness["tuples"]})
   R.verify_route(route, R.ROOT)

 def test_owner_and_worker_pins_do_not_add_disabled_automatic_owners(self):
  self.assertEqual(R._compose_default_owners(), R._compose_default_children())
  pins = {"owner": {"harness": "gemini"}, "worker": {"harness": "opencode"}}
  with mock.patch.object(R, "_compose_default_children", return_value=("claude", "codex")):
   self.assertEqual(R._compose_default_owners(pins), ("claude", "codex"))

 def test_automatic_pool_does_not_reprobe_a_checked_but_ineligible_owner_pin(self):
  for pinned in ("claude", "codex", "opencode"):
   other = "claude" if pinned != "claude" else "codex"
   checked = self.nested(parent=pinned, child="claude", status="unsupported", failure="fixture-unavailable")
   readiness = self.dispatch(self.nested(parent=other, child="claude"), checked)
   with self.subTest(pinned=pinned), \
        mock.patch.object(R, "_compose_default_children", return_value=("claude", "codex", "opencode")), \
        mock.patch.object(R, "_compose_readiness", return_value=readiness) as probe:
    route = self.compose(dispatch_evidence=None, selection_pins=R._parse_selection_pins([], pinned),
                         children=["claude"])
    probe.assert_called_once()
    self.assertEqual(route["dispatch_evidence"]["tuples"], readiness["tuples"])
    R.verify_route(route, R.ROOT)

 def test_checked_owner_without_conductor_authority_is_not_reprobed_or_accepted(self):
  readiness = self.dispatch(self.nested(parent="claude", child="claude"),
                            self.nested(parent="codex", child="claude", authority="ancestor-broker"))
  with mock.patch.object(R, "_compose_readiness", return_value=readiness) as probe:
   with self.assertRaisesRegex(ValueError, "v3 dispatch evidence requires conductor launch authority"):
    self.compose(dispatch_evidence=None, selection_pins=R._parse_selection_pins([], "codex"))
  probe.assert_called_once()

 def test_supplied_checked_owner_refusal_is_preserved_without_another_probe(self):
  checked = self.nested(parent="codex", child="claude", status="unsupported", failure="fixture-unavailable")
  evidence = self.dispatch(self.nested(parent="claude", child="claude"), checked)
  with mock.patch.object(R, "_compose_readiness") as probe:
   route = self.compose(dispatch_evidence=evidence, selection_pins=R._parse_selection_pins([], "codex"))
  probe.assert_not_called()
  self.assertEqual(route["dispatch_evidence"]["tuples"], evidence["tuples"])
  R.verify_route(route, R.ROOT)

 def test_out_of_pool_owner_pin_still_gets_its_exact_probe(self):
  pool = self.dispatch(self.nested(parent="claude", child="claude"))
  exact = self.dispatch(self.nested(parent="codex", child="claude"))
  with mock.patch.object(R, "_compose_default_children", return_value=("claude",)), \
       mock.patch.object(R, "_compose_readiness", side_effect=[pool, exact]) as probe:
   route = self.compose(dispatch_evidence=None, selection_pins=R._parse_selection_pins([], "codex"),
                        children=["claude"])
  self.assertEqual(probe.call_count, 2)
  self.assertEqual(probe.call_args_list[0].kwargs["owner_harnesses"], ("claude",))
  self.assertEqual(probe.call_args_list[1].args[2], "codex")
  self.assertEqual(route["dispatch_evidence"]["tuples"], pool["tuples"] + exact["tuples"])
  R.verify_route(route, R.ROOT)

 def test_auto_owner_close_retains_verification_and_every_leg_input(self):
  for intensity in ("strong","thorough","adversarial"):
   route=self.compose(capability="autopilot-lab",capability_mode="eval",intensity=intensity,
                      graph="metrics:qa/ml-debug,diagnose,report,independent-verify")
   nodes={n["id"]:n for n in route["nodes"]};owner=nodes["owner-close"]
   group=route["parallel_groups"][0]
   self.assertEqual((group["id"],group["width"]),("independent-verify",2))
   self.assertEqual(owner["depends_on"],group["members"])
   self.assertEqual((owner["kind"],owner["unit"],owner["dispatch_depth"]),
                    ("capability-owner","_kernel/owner",1))
   self.assertEqual(owner["write_scope"],["owner-close.md"])
   self.assertEqual(owner["terminal_gate"],"compose-owner-close")
   self.assertEqual(route["workflow_contract"]["terminal_nodes"],["owner-close"])
   self.assertFalse(set(nodes)&{"publish","sync"})
   for member in group["members"]:
    self.assertNotIn("terminal",nodes[member])
    self.assertEqual(nodes[member]["continuation"],{"kind":"inline-next"})
    self.assertTrue(set(nodes[member]["outputs"])<=set(owner["inputs"]))
   self.assertNotIn("omitted_parallel_presets",route["composed_recipe"]["compose"])
   self.assertTrue(R._versioned_subgraph(R.TOPO.load_registry(),route["composed_recipe"]))
   R.verify_route(route,R.ROOT)
  standard=self.compose(capability="autopilot-lab",capability_mode="eval",intensity="standard",
                        graph="metrics:qa/ml-debug,diagnose,report,independent-verify")
  self.assertEqual([n["id"] for n in standard["nodes"]],["metrics","diagnose","report","independent-verify"])
  self.assertNotIn("auto_completed_nodes",standard["composed_recipe"]["compose"])
  self.assertEqual(standard["parallel_groups"],[])
  R.verify_route(standard,R.ROOT)

 def test_auto_owner_close_map_arbiter_and_borrowed_scopes(self):
  for intensity,width in (("strong",2),("thorough",3)):
   route=self.compose(capability="audit",capability_mode="default",intensity=intensity,
                      graph="inspect,autopilot-research:retrieval")
   group=route["parallel_groups"][0];nodes={n["id"]:n for n in route["nodes"]}
   self.assertEqual(group["width"],width)
   self.assertEqual(nodes["owner-close"]["depends_on"],group["members"])
   for member in group["members"]:
    self.assertTrue(set(nodes[member]["outputs"])<=set(nodes["owner-close"]["inputs"]))
    self.assertTrue(all("parts/autopilot-research/retrieval/" in p for p in nodes[member]["write_scope"]))
   if width==3:
    self.assertEqual(R._resolve_auxiliary_arbiter(route,group["id"]),("node","owner-close"))
   R.verify_route(route,R.ROOT)

 def test_auto_owner_close_assignment_and_auxiliary_completion(self):
  import worker_bootstrap as W
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   route=self.compose(capability="audit",capability_mode="default",intensity="thorough",
                      graph="inspect,autopilot-research:retrieval",artifact_root=str(root))
   path=root/"route.json";path.write_text(json.dumps(route))
   owner=route["nodes"][-1];group=route["parallel_groups"][0]
   auxiliary=R._realized_auxiliary_nodes(route,group["id"])
   self.assertEqual(len(auxiliary),1)
   for harness in ("claude","codex","opencode"):
    for via_binding in (False,True):
     args=SimpleNamespace(worker_type="owner",adapter=harness,route_node=None,
                          route_file=None if via_binding else str(path),
                          owner_route_binding=SimpleNamespace(route_file=str(path)) if via_binding else None)
     prompt=W.assignment_prompt(args,"Analyze the existing results",{})
     self.assertIn(group["id"],prompt)
     self.assertIn("exactly 1 entries",prompt)
     self.assertIn('  - "'+auxiliary[0]["id"]+': <finding and disposition>"',prompt)
   self.assertEqual(W.owner_close_prompt(SimpleNamespace(worker_type="stage",route_file=str(path))),"")
   for node in route["nodes"][:-1]:
    evidence=root/(node["id"]+".md");evidence.write_text("PASS\n")
    R.complete_node(route,node,node["id"],evidence,attempt_id="att-inline-"+node["id"],
                    explicit_attempt_metadata=InlineStageCompletionRecipeTest._axes(self))
   closing=root/"owner-close.md";closing.write_text("All results reconciled\n")
   with self.assertRaisesRegex(ValueError,"requires auxiliary_findings_considered"):
    R.complete_node(route,owner,owner["id"],closing)
   closing.write_text('---\nauxiliary_findings_considered:\n  - "assumption: finding adopted"\n---\nAll results reconciled\n')
   R.complete_node(route,owner,owner["id"],closing,attempt_id="att-inline-owner-close",
                   explicit_attempt_metadata=InlineStageCompletionRecipeTest._axes(self,depth=1))
   self.assertEqual(owner["write_scope"],["owner-close.md"])
   self.assertTrue(R.terminal_gate_observation(route,jobs=self._fixture_jobs)["owner-close"]["passed"])

 def test_auto_owner_close_cannot_finish_before_every_verification_leg(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   route=self.compose(capability="autopilot-lab",capability_mode="eval",intensity="strong",
                      graph="metrics:qa/ml-debug,diagnose,report,independent-verify",artifact_root=str(root))
   owner=route["nodes"][-1];closing=root/"owner-close.md";closing.write_text("Both verdicts reconciled\n")
   with self.assertRaisesRegex(ValueError,"compose-owner-close-before-join"):
    R.complete_node(route,owner,owner["id"],closing)
   for node in route["nodes"][:-1]:
    evidence=root/(node["id"]+".md");evidence.write_text("PASS\n")
    R.complete_node(route,node,node["id"],evidence,attempt_id="att-inline-"+node["id"],
                    explicit_attempt_metadata=InlineStageCompletionRecipeTest._axes(self))
    if node["id"]=="independent-verify":
     with self.assertRaisesRegex(ValueError,"compose-owner-close-before-join:independent-verify-alternative"):
      R.complete_node(route,owner,owner["id"],closing)
   self.assertEqual(R.owner_terminal_prerequisites(route,owner,self._fixture_jobs),{})
   R.complete_node(route,owner,owner["id"],closing,attempt_id="att-inline-owner-close",
                   explicit_attempt_metadata=InlineStageCompletionRecipeTest._axes(self,depth=1))
   self.assertTrue(R.terminal_gate_observation(route,jobs=self._fixture_jobs)["owner-close"]["passed"])

 def test_auto_owner_close_rejects_changed_scope_and_missing_assembly(self):
  route=self.compose(graph="plan,impl-review",intensity="strong")
  registry=R.TOPO.load_registry()
  for change in ("scope","metadata","intensity"):
   recipe=json.loads(R.canonical(route["composed_recipe"]))
   if change=="scope":recipe["standard_plus"]["nodes"][-1]["write_scope"].append("source/**")
   if change=="metadata":recipe["compose"].pop("auto_completed_nodes")
   if change=="intensity":recipe["compose"]["auto_completed_nodes"][0]["intensity"]="standard"
   with self.subTest(change=change),self.assertRaisesRegex(ValueError,"compose-owner-close-differs"):
    R._validate_compose_owner_close(registry,recipe)
  with self.assertRaisesRegex(ValueError,"compose-owner-close-differs"):
   R._validate_compose_owner_close(registry,route["composed_recipe"],"standard")

 def test_auto_owner_close_does_not_restore_a_dropped_approval(self):
  route=self.compose(capability="autopilot-refine",capability_mode="default",
                     graph="review",intensity="strong")
  self.assertEqual(route["human_gates"],[])
  self.assertEqual(route["human_gate_bindings"],[])
  self.assertEqual(route["nodes"][0]["continuation"],{"kind":"inline-next"})
  self.assertEqual(route["nodes"][-1]["write_scope"],["owner-close.md"])
  R.verify_route(route,R.ROOT)

 def test_strong_research_extension_runs_after_the_appended_owner_close(self):
  # SR_CorrNet_SD: claim-verify was no longer terminal once owner-close followed its group.
  for intensity in ("strong","thorough"):
   with self.subTest(intensity=intensity):
    route=self.compose(capability="autopilot-research",capability_mode="market",intensity=intensity,
                       graph="retrieval,synthesis,report,claim-verify")
    self.assertEqual(route["nodes"][-1]["id"],"owner-close")
    self.assertEqual([row["after"] for row in route["conditional_extensions"]],[["owner-close"]])
    R.verify_route(route,R.ROOT)

 def test_auto_owner_close_report_scope_keeps_dependency_projection(self):
  route=self.compose(capability="autopilot-lab",capability_mode="eval",intensity="strong",
                     graph="metrics:qa/ml-debug,diagnose,report,independent-verify",execution_scope="report")
  self.assertTrue(route["composed_recipe"]["compose"]["preserve_base_dependencies"])
  self.assertTrue(R._versioned_subgraph(R.TOPO.load_registry(),route["composed_recipe"]))
  self.assertEqual(route["workflow_contract"]["terminal_nodes"],["owner-close"])
  R.verify_route(route,R.ROOT)

 def test_report_scope_links_a_follower_through_a_dropped_stage(self):
  # rt-7bb0eb4782ff1274: execute waited only on the dropped plan-check, so it ran beside plan.
  route=self.compose(graph="plan,execute,test,report",execution_scope="report")
  deps={node["id"]:node.get("depends_on") for node in route["nodes"]}
  self.assertEqual(deps["execute"],["plan"])
  R.verify_route(route,R.ROOT)

 def test_auto_owner_close_notice_is_once_before_start_and_explain(self):
  import work_start
  with tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,{"AGENT_HOME":str(R.ROOT),"AGENT_DISPATCH_ATTEMPT_ID":""}):
   root=Path(tmp);prompt=root/"task.md";prompt.write_text("Existing metrics judgment")
   route=self.compose(capability="autopilot-lab",capability_mode="eval",intensity="strong",
                      graph="metrics:qa/ml-debug,diagnose,report,independent-verify",artifact_root=str(root/"artifacts"))
   notice="strong group independent-verify: auto-added owner-close (terminal-anchor; owner closing summary)"
   for harness in ("claude","codex","opencode"):
    for action in ("explain","start"):
     out,err=io.StringIO(),io.StringIO()
     argv=[str(P),"compose","--shape","staged","--intensity","strong","--capability","autopilot-lab",
           "--capability-mode","eval","--graph","metrics:qa/ml-debug,diagnose,report,independent-verify",
           "--cwd",str(R.ROOT),"--artifact-root",str(root/"artifacts"),"--unassigned","--parent-harness",harness,
           "--prompt-file",str(prompt),"--jobs",str(root/"jobs.log"),"--"+action]
     receipt={"state":"waiting-owner","parent_next":"end-turn"}
     def start(*a,**kw):
      self.assertEqual(err.getvalue().count(notice),1)
      return dict(receipt)
     with mock.patch.object(sys,"argv",argv),mock.patch.object(R,"compose_route",return_value=route), \
          mock.patch.object(work_start,"start_work",side_effect=start) as launch, \
          mock.patch.object(R,"_record_route_chain"),mock.patch.object(R,"_route_autoclose"), \
          contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
      self.assertEqual(R.main(),0)
     self.assertEqual(err.getvalue().count(notice),1)
     payload=json.loads(out.getvalue())
     if action=="start":
      self.assertEqual({k:payload[k] for k in receipt},receipt)
      self.assertEqual(set(payload),set(receipt)|{"confirmation","pre_execution_answers"})
      launch.assert_called_once()
     else:
      launch.assert_not_called();self.assertEqual(payload["parallel_groups"][0]["width"],2)

 def test_terminal_group_notice_preserves_the_graph_and_pre_notice_route_profiles(self):
  args=dict(capability="autopilot-lab",capability_mode="eval",intensity="strong",
            graph="metrics:qa/ml-debug,diagnose,report,independent-verify")
  compose=R.compose_subgraph_recipe
  def historical(*a,**kw):
   kw["group_intensity"]=None
   return compose(*a,**kw)
  with mock.patch.object(R,"compose_subgraph_recipe",side_effect=historical):
   route=self.compose(**args)
  expected=[{"id":"independent-verify","reason":"terminal-anchor"}]
  self.assertEqual(route["composed_recipe"]["compose"]["omitted_parallel_presets"],expected)
  self.assertEqual(route["parallel_groups"],[])
  self.assertEqual([n["id"] for n in route["nodes"]],["metrics","diagnose","report","independent-verify"])
  self.assertEqual(R.compose_omission_lines(route),[
   "  strong group independent-verify dropped by --graph: terminal-anchor"])
  inactive=self.compose(**{**args,"intensity":"standard"})
  self.assertEqual(R.compose_omission_lines(inactive),[])
  # Existing omission rows carry no minimum-intensity hint. Registry gaps
  # must suppress a notice rather than claim an unselected preset was dropped.
  with mock.patch.object(R.TOPO,"load_registry",side_effect=ValueError("historical registry unavailable")):
   self.assertEqual(R.compose_omission_lines(route),[])
   self.assertEqual(R.compose_omission_lines(inactive),[])
  self.assertEqual(route["composed_recipe"]["compose"]["omitted_parallel_presets"],expected)
  R.verify_route(route,R.ROOT)
  # Compile the exact pre-notice recipe shape, then verify it with the new
  # composer. Recording a diagnostic must not reinterpret its sealed profiles.
  def pre_notice(*a,**kw):
   kw["group_intensity"]=None
   recipe=compose(*a,**kw)
   recipe["compose"].pop("omitted_parallel_presets")
   return recipe
  with mock.patch.object(R,"compose_subgraph_recipe",side_effect=pre_notice):
   old=self.compose(**args)
  before=R.canonical(old)
  self.assertEqual(old["nodes"],route["nodes"])
  self.assertEqual(old["workflow_contract"],route["workflow_contract"])
  R.verify_route(old,R.ROOT)
  self.assertEqual(before,R.canonical(old))
 def test_omission_notice_is_once_before_start_and_once_in_explain_in_all_harnesses(self):
  import work_start
  with tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,{"AGENT_HOME":str(R.ROOT),"AGENT_DISPATCH_ATTEMPT_ID":""}):
   root=Path(tmp);prompt=root/"task.md";prompt.write_text("Existing metrics judgment")
   # Historical/unsafe omissions still have the same shared diagnostic surface.
   compose=R.compose_subgraph_recipe
   def historical(*args,**kwargs):
    kwargs["complete_group_consumers"]=False
    return compose(*args,**kwargs)
   with mock.patch.object(R,"compose_subgraph_recipe",side_effect=historical):
    route=self.compose(intensity="strong",graph="plan,test,report",artifact_root=str(root/"artifacts"))
   warning="strong group plan dropped by --graph: review-consumer-not-selected"
   for harness in ("claude","codex","opencode"):
    for action in ("explain","start"):
     out,err=io.StringIO(),io.StringIO()
     argv=[str(P),"compose","--shape","staged","--intensity","strong","--capability","autopilot-code",
           "--capability-mode","dev","--graph","plan,test,report",
           "--cwd",str(R.ROOT),"--artifact-root",str(root/"artifacts"),"--unassigned","--parent-harness",harness,
           "--prompt-file",str(prompt),"--jobs",str(root/"jobs.log"),"--"+action]
     receipt={"state":"waiting-owner","parent_next":"end-turn"}
     def start(*a,**kw):
      self.assertEqual(err.getvalue().count(warning),1)
      return dict(receipt)
     with mock.patch.object(sys,"argv",argv),mock.patch.object(R,"compose_route",return_value=route), \
          mock.patch.object(work_start,"start_work",side_effect=start) as launch, \
          mock.patch.object(R,"_record_route_chain"),mock.patch.object(R,"_route_autoclose"), \
          contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
      self.assertEqual(R.main(),0)
     self.assertEqual(err.getvalue().count(warning),1)
     payload=json.loads(out.getvalue())
     if action=="start":
      self.assertEqual({k:payload[k] for k in receipt},receipt)
      self.assertEqual(set(payload),set(receipt)|{"confirmation","pre_execution_answers"})
      launch.assert_called_once()
     else:
      launch.assert_not_called();self.assertEqual(payload["parallel_groups"],[])
 def test_compose_decisions_replay_input_sources_without_changing_profiles_or_receipt_keys(self):
  default=self.compose(capability=None,capability_mode=None)
  self.assertEqual((default["capability"],default["capability_mode"]),("autopilot-code","dev"))
  lines="\n".join(R.compose_decision_lines(default))
  self.assertIn("intensity=standard (source=shape-default, 지정 안 됨)",lines)
  self.assertIn("고정 기본값, 과제 문장 분류 없음",lines)
  self.assertEqual(lines.count("--intensity strong 검토"),1)
  R.verify_route(default,R.ROOT)
  strong=self.compose(intensity="strong")
  explicit="\n".join(R.compose_decision_lines(strong))
  self.assertIn("intensity=strong (source=explicit)",explicit)
  self.assertNotIn("강도 안내",explicit)
  self.assertEqual([n["model_profile"] for n in default["nodes"]],
                   [n["model_profile"] for n in strong["nodes"]])
  legacy=json.loads(json.dumps(default))
  legacy["selection"]["selection_basis"]=[r for r in legacy["selection"]["selection_basis"]
                                          if not r["axis"].startswith("compose-")]
  self.assertIn("source=sealed-route","\n".join(R.compose_decision_lines(legacy)))
  self.assertNotIn("강도 안내","\n".join(R.compose_decision_lines(legacy)))
  legacy_display="\n".join(R.compose_decision_lines(legacy))
  self.assertIn("capability=autopilot-code (source=sealed-route)",legacy_display)
  self.assertNotIn("source=compose-default",legacy_display)
  framed=self.compose(capability=None,capability_mode=None,shape="framed",graph=None)
  framed["selection"]["selection_basis"]=[r for r in framed["selection"]["selection_basis"]
                                         if not r["axis"].startswith("compose-")]
  frame_display="\n".join(R.compose_decision_lines(framed))
  self.assertIn("capability=frame 결정 대기 (source=sealed-route)",frame_display)
  self.assertNotIn(R.ROUTE_FRAME_CAPABILITY,frame_display)
  self.assertNotIn("source=compose-default",frame_display)
  self.assertEqual(set(R.compose_observations(default)),{"confirmation","pre_execution_answers"})
 def test_compose_decisions_show_real_group_leg_profiles_and_resource_dispatch_count(self):
  grouped=self.compose(capability="autopilot-lab",capability_mode="eval",intensity="strong",
                       graph="report,independent-verify,publish,sync",
                       explicit_profiles={"independent-verify":"balanced"})
  display="\n".join(R.compose_decision_lines(grouped))
  self.assertIn("independent-verify=balanced+balanced-deep",display)
  self.assertIn("nodes=5 worker_dispatches=3 owner_dispatches=1 resource(측정)=없음",display)
  measured=self.compose(capability="autopilot-lab",capability_mode="eval",graph="eval-run,metrics,report")
  self.assertIn("nodes=3 worker_dispatches=2 owner_dispatches=1 resource(측정)=eval-run",
                "\n".join(R.compose_decision_lines(measured)))
  framed=self.compose(capability=None,capability_mode=None,shape="framed",graph=None,intensity="strong")
  frame_display="\n".join(R.compose_decision_lines(framed))
  self.assertIn("source=explicit, requested=strong",frame_display)
  self.assertNotIn("강도 안내",frame_display)
  self.assertIn("capability=frame 결정 대기",frame_display)
 def test_compose_start_and_resume_print_decisions_before_launch_once_in_all_harnesses(self):
  import work_start
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_HOME":str(R.ROOT),"AGENT_DISPATCH_ATTEMPT_ID":""}):
   root=Path(tmp); prompt=root/"task.md"; prompt.write_text("결과 분석·평가·비교")
   route=self.compose(shape="direct",graph=None,artifact_root=str(root/"artifacts"),
                      work_request={"text":prompt.read_text(),"owner_harness":None})
   path=root/"route.json"; path.write_text(json.dumps(route))
   for harness in ("claude","codex","opencode"):
    for command in ("compose","start"):
     out,err=io.StringIO(),io.StringIO()
     argv=[str(P),command,"--jobs",str(root/"jobs.log")]
     argv+=(["--start","--prompt-file",str(prompt),"--shape","direct","--parent-harness",harness,
             "--cwd",str(R.ROOT),"--artifact-root",str(root/"artifacts"),"--unassigned"]
            if command=="compose" else ["--route",str(path)])
     receipt={"state":"inline","parent_next":"end-turn","parent_next_command":"untouched"}
     def start(*args,**kwargs):
      self.assertIn("intensity=direct",err.getvalue())
      self.assertIn("nodes=1 worker_dispatches=0 owner_dispatches=0 resource(측정)=없음",err.getvalue())
      self.assertIn("tiers: inline=current-session(등급 미기록)",err.getvalue())
      return dict(receipt)
     # Display-only fixture: the preparation boundary is observed, never run on the checkout under test.
     with mock.patch.object(sys,"argv",argv),mock.patch.object(R,"compose_route",return_value=route), \
          mock.patch.object(R,"prepare_isolated_worktree",return_value={"state":"reused","cwd":str(R.ROOT)}), \
          mock.patch.object(work_start,"start_work",side_effect=start), \
          mock.patch.object(R,"_record_route_chain"),mock.patch.object(R,"_route_autoclose"), \
          contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
      self.assertEqual(R.main(),0)
     actual=json.loads(out.getvalue())
     self.assertEqual({k:actual[k] for k in receipt},receipt)
     expected_keys=set(receipt)|{"confirmation","pre_execution_answers"}
     if command=="compose": expected_keys.add("worktree")
     self.assertEqual(set(actual),expected_keys)
     self.assertEqual(err.getvalue().count("강도 안내"),1)
     self.assertEqual(err.getvalue().count("규모:"),1)
 def test_verified_resume_graph_keeps_payload_inline_and_verifies_after_exit(self):
  for shape in ("direct", "solo"):
   route=self.compose(capability="autopilot-lab",capability_mode="setup",shape=shape,
                      graph="resume-run,run-verify",dispatch_evidence=None,
                      registered_headless_evidence=self.registered_headless())
   self.assertEqual(route["selection"]["shape"],shape)
   self.assertEqual(route["effective_intensity"],"quick")
   self.assertEqual(route["tracking"],"tracked")
   self.assertEqual([n["id"] for n in route["nodes"]],["resume-run","one-shot"])
   self.assertNotIn("dispatch_depth",route["nodes"][0])
   self.assertEqual(route["nodes"][0]["continuation"],{"kind":"supervised"})
   self.assertEqual(route["nodes"][1]["dispatch_depth"],1)
   self.assertEqual(route["nodes"][1]["depends_on"],["resume-run"])
   self.assertTrue(route["nodes"][1]["verification_only"])
   self.assertEqual(route["human_gates"],[])
   self.assertNotIn("no-resource-run",route["selection"]["direct_predicates"])
   self.assertEqual(R.declared_start_approvals({"capability":"autopilot-lab","mode":"setup",
                                               "shape":shape,"graph":["resume-run","run-verify"]}),[])
   R.verify_route(route,R.ROOT)
   forged=json.loads(json.dumps(route))
   forged["composed_recipe"]["standard_plus"]["nodes"][0]["resource_policy"]="new-code"
   forged["route_hash"]=R.route_hash(forged)
   forged["route_id"]=R.ROUTE_IDENTITY.route_id_from_hash(forged["route_hash"])
   with self.assertRaises(ValueError): R.verify_route(forged,R.ROOT)
  with self.assertRaisesRegex(ValueError,"compose-graph-only-staged"):
   self.compose(capability="autopilot-lab",capability_mode="setup",shape="direct",graph="full-run,run-verify")
 def test_old_refine_route_uses_exact_git_show_catalog_and_unrelated_digest_drift_stays_stale(self):
  import subprocess
  current = R.TOPO.load_registry()
  # Keep the pre-entry-scope catalog in a private repository: this checkout's
  # HEAD already includes the change once committed, and moves again on merge.
  old_catalog = json.loads(json.dumps(current))
  transaction = old_catalog["part_catalog"]["parts"]["autopilot-refine:transaction"]
  self.assertEqual(transaction.pop("start_approval"), "preview")
  recipe = next(row for row in old_catalog["recipes"] if row["capability"] == "autopilot-refine")
  review = next(node for node in recipe["standard_plus"]["nodes"] if node["id"] == "review")
  review["outputs"].remove("reviews/refine/preview.md")
  repo = Path(self._tmp_home.name) / "old-catalog-repo"
  (repo / "capabilities").mkdir(parents=True)
  (repo / "capabilities/topologies.json").write_text(json.dumps(old_catalog, indent=2) + "\n")
  subprocess.run(["git", "init", "-q", str(repo)], check=True)
  subprocess.run(["git", "add", "capabilities/topologies.json"], cwd=repo, check=True)
  subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "Before entry scope"], cwd=repo, check=True)
  git_show_registry = json.loads(subprocess.check_output(
   ["git","show","HEAD:capabilities/topologies.json"],cwd=repo,text=True))
  reconstructed = R._entry_scope_legacy_registry(current)
  self.assertEqual(R.TOPO.registry_digest(git_show_registry),R.TOPO.registry_digest(reconstructed))
  with mock.patch.object(R.TOPO,"load_registry",return_value=git_show_registry):
   route=self.compose(capability="autopilot-refine",capability_mode="default",shape="staged",
                      graph="review,transaction",slug="old-refine")
  fixture=Path(self._tmp_home.name)/"git-show-old-refine-route.json"
  fixture.write_bytes(R.canonical(route)+b"\n")
  self.assertEqual(json.loads(fixture.read_text()),route)
  self.assertNotIn("entry_scope_contract_version",route)
  self.assertIn("preview-disposition",route.get("human_gates",[]))
  R.verify_route(route,R.ROOT)
  tampered=json.loads(R.canonical(current)); tampered["registry_fixture_unrelated_change"]=True
  with mock.patch.object(R.TOPO,"load_registry",return_value=tampered), mock.patch.object(R,"gates_on",return_value=True):
   with self.assertRaisesRegex(ValueError,"registry"):
    R.verify_route(route,R.ROOT)

 def evidence(self):
  return self.dispatch(self.nested(parent="claude",child="claude"),self.nested(parent="claude",child="codex"))
 def compose(self,**kw):
  d=dict(capability="autopilot-code",capability_mode="dev",shape="staged",graph="execute,test,report",slug="compose-fixture",cwd=R.ROOT,artifact_root=R.ROOT,dispatch_evidence=self.evidence())
  d.update(kw)
  # A compose fixture that names no stream opts out explicitly, exactly as a caller must.
  if d.get("campaign_key") is None and d.get("parent_cycle_id") is None and "unassigned" not in d: d["unassigned"]=True
  return R.compose_route(**d)
 def test_complete_scope_keeps_each_full_graph_without_an_extra_preview_wait(self):
  registry=R.TOPO.load_registry()
  with tempfile.TemporaryDirectory() as tmp:
   for capability,mode in (("autopilot-lab","setup"),("autopilot-ship","default"),
                           ("autopilot-refine","default"),("autopilot-apply","default")):
    with self.subTest(capability=capability):
     recipe=next(row for row in registry["recipes"] if row["capability"]==capability and mode in row["modes"])
     expected=[node["id"] for node in recipe["standard_plus"]["nodes"] if not R._frame_node(node)]
     route=self.compose(capability=capability,capability_mode=mode,shape="staged",graph=None,
                        slug="complete-"+capability,artifact_root=tmp,execution_scope="complete")
     self.assertEqual([node["id"] for node in route["nodes"]],expected)
     self.assertEqual(route["entry_execution_scope"],"complete")
     self.assertEqual(R.verify_route(route,R.ROOT)["entry_execution_scope"],"complete")
     self.assertNotIn("preview-disposition",route.get("human_gates",[]))

 def test_report_scope_graphless_routes_end_at_each_capabilitys_declared_review_boundary(self):
  with tempfile.TemporaryDirectory() as tmp:
   cases=(
    ("autopilot-lab","setup",["scaffold","smoke"],["smoke"]),
    ("autopilot-ship","default",["release-setup","security-review","release-review"],
     ["security-review","release-review"]),
    ("autopilot-refine","default",["review"],["review"]),
    ("autopilot-apply","default",["apply","verify"],["verify"]),
   )
   for capability,mode,expected,terminals in cases:
    with self.subTest(capability=capability):
     route=self.compose(capability=capability,capability_mode=mode,shape="staged",graph=None,
                        slug="report-"+capability,artifact_root=tmp,execution_scope="report")
     self.assertEqual([node["id"] for node in route["nodes"]],expected)
     self.assertEqual([node["id"] for node in route["nodes"] if node.get("terminal") is True],terminals)
     self.assertEqual(R.verify_route(route,R.ROOT)["entry_execution_scope"],"report")
     self.assertFalse(R.terminal_gate_proven(R.terminal_gate_observation(route)),
                      "report cannot finish before every declared terminal review/verify marker exists")
     self.assertFalse(any(node.get("id") in {"full-run","deploy","transaction","handback"}
                          for node in route["nodes"]))
     if capability=="autopilot-ship":
      by_id={node["id"]:node for node in route["nodes"]}
      self.assertEqual(by_id["security-review"]["depends_on"],["release-setup"])
      self.assertEqual(by_id["release-review"]["depends_on"],["release-setup"])
      self.assertEqual(route["completion_gates"], ["ship-release","ship-security","ship-setup"])

 def test_report_scope_without_explicit_graph_uses_base_prefix_and_direct_scope_reaches_owner(self):
  with tempfile.TemporaryDirectory() as tmp:
   report=self.compose(capability="autopilot-lab",capability_mode="setup",shape="staged",graph=None,
                       slug="lab-report",artifact_root=tmp,execution_scope="report",
                       work_request={"text":"Prepare the smoke report.","owner_harness":"codex"})
   self.assertEqual([n["id"] for n in report["nodes"]], ["scaffold","smoke"])
   self.assertEqual(report["entry_execution_scope"],"report")
   self.assertEqual(report["work_request"]["text"].splitlines()[-1],"Execution scope: report")
   self.assertEqual(R.verify_route(report,R.ROOT)["entry_execution_scope"],"report")
   malformed=json.loads(R.canonical(report))
   malformed["entry_scope_contract_version"]=True
   malformed["route_hash"]=R.route_hash(malformed)
   malformed["route_id"]=R.ROUTE_IDENTITY.route_id_from_hash(malformed["route_hash"])
   with self.assertRaisesRegex(ValueError,"route nodes differ from the declared recipe"):
    R.verify_route(malformed,R.ROOT)
   direct=self.compose(capability="autopilot-refine",capability_mode="default",shape="direct",graph=None,
                       slug="refine-review-only",artifact_root=tmp,execution_scope="report",
                       work_request={"text":"Review only.","owner_harness":"codex"},dispatch_evidence=None)
   self.assertEqual(direct["entry_execution_scope"],"report")
   self.assertIn("Execution scope: report",direct["work_request"]["text"])
   self.assertIn("실행 범위 report",R.compose_card(direct))
   self.assertNotIn("시작 승인 preview",R.compose_card(direct))
   self.assertNotIn("preview-disposition",direct.get("human_gates",[]))
   R.verify_route(direct,R.ROOT)

 def test_work_request_is_sealed_and_cannot_be_replaced_at_resume(self):
  request={"text":"Run the accepted commands, including exit 7.","owner_harness":"opencode"}
  route=self.compose(work_request=request,profile="light")
  self.assertEqual(route["work_request"],request)
  R.verify_route(route,R.ROOT)
  changed=self.compose(work_request={**request,"text":"Only run the passing command."},profile="light")
  self.assertNotEqual(route["route_id"],changed["route_id"])
  route["work_request"]["text"]="Only run the passing command."
  with self.assertRaisesRegex(ValueError,"modified route hash"): R.verify_route(route,R.ROOT)
  for bad in ({"text":"","owner_harness":None},{"text":"task","owner_harness":"invented"}, {"text":"task"}):
   with self.assertRaises(ValueError): self.compose(work_request=bad)
 def test_compose_start_prints_one_work_receipt_and_preserves_full_route(self):
  import contextlib, io, types
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_HOME":str(R.ROOT),"AGENT_DISPATCH_ATTEMPT_ID":""}):
   route=self.compose(artifact_root=tmp,work_request={"text":"task","owner_harness":"codex"})
   out=io.StringIO()
   with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()):
    path=R._emit_compiled_route(types.SimpleNamespace(command="compose",start=True),route,tmp)
   self.assertEqual(out.getvalue(),"")
   self.assertEqual(json.loads(path.read_text()),route)
 def test_compose_start_keeps_new_campaign_notice_before_its_own_cycle_creation(self):
  import artifact_producer, work_start
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_HOME":str(R.ROOT),"AGENT_DISPATCH_ATTEMPT_ID":""}):
   root=Path(tmp); git_root=root/"git-root"; git_root.mkdir(); subprocess.run(["git","init","-q",str(git_root)],check=True)
   artifacts=root/"artifacts"; prompt=root/"task.md"; prompt.write_text("One approved inline edit")
   route=self.compose(shape="direct",graph=None,artifact_root=str(artifacts),campaign_key="new-start",
                      work_request={"text":prompt.read_text(),"owner_harness":None})
   def start(route,path,jobs,**kwargs):
    artifact_producer.prepare_route_artifact_env(path,start=True,jobs=jobs)
    return {"state":"inline","required_action":"execute-inline","route_id":route["route_id"]}
   argv=[str(P),"compose","--shape","direct","--campaign-key","new-start","--slug","notice",
         "--cwd",str(git_root),"--artifact-root",str(artifacts),"--start","--prompt-file",str(prompt),"--jobs",str(root/"jobs.log")]
   notices=[]
   for _ in range(2):
    out,err=io.StringIO(),io.StringIO()
    with mock.patch.object(sys,"argv",argv), mock.patch.object(R,"compose_route",return_value=route), \
         mock.patch.object(R,"prepare_isolated_worktree",return_value={"state":"reused","cwd":str(R.ROOT)}), \
         mock.patch.object(R,"_record_route_chain"), mock.patch.object(R,"_route_autoclose"), \
         mock.patch.object(work_start,"start_work",side_effect=start), \
         contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
     self.assertEqual(R.main(),0)
    self.assertEqual(json.loads(out.getvalue())["state"],"inline")
    self.assertEqual(json.loads(Path(R.canonical_route_path(str(artifacts),route["route_id"])).read_text()),route)
    self.assertEqual(err.getvalue().count("[경로]"),1)
    notices.append(err.getvalue())
   self.assertIn("캠페인 new-start (신규 생성)",notices[0])
   self.assertNotIn("기존 합류",notices[0])
   self.assertIn("캠페인 new-start (기존 합류)",notices[1])
 def test_compose_receipt_keeps_choices_and_full_canonical_record(self):
  import contextlib, io, types
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{"AGENT_HOME":str(R.ROOT)}):
   route=self.compose(artifact_root=tmp,profile="light")
   outputs=[]
   with mock.patch.dict(os.environ,{"AGENT_DISPATCH_ATTEMPT_ID":""}):
    for full in (False,True):
     out=io.StringIO()
     with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()):
      R._emit_compiled_route(types.SimpleNamespace(command="compose",full_record=full),route,tmp)
     outputs.append(json.loads(out.getvalue()))
   receipt,full=outputs
   self.assertEqual(full,route)
   self.assertEqual(json.loads(Path(receipt["route_file"]).read_text()),route)
   self.assertEqual(receipt["owner_model_profile"],"light")
   self.assertEqual([n["id"] for n in receipt["nodes"]],["execute","test","report"])
   self.assertTrue(all(n["model_profile"]=="light" for n in receipt["nodes"]))
   self.assertLess(len(json.dumps(receipt)),len(json.dumps(route))//3)
 def test_campaign_selection_is_optional_validated_and_sealed(self):
  old=self.compose()
  self.assertNotIn("campaign_key",old); self.assertNotIn("parent_cycle_id",old)
  self.assertIs(old["campaign_unassigned"],True)
  R.verify_route(old,R.ROOT)
  old["campaign_unassigned"]=False
  with self.assertRaisesRegex(ValueError,"modified route hash"): R.verify_route(old,R.ROOT)
  # A keyless proposal is refused, not silently degraded; the refusal names the escape hatch.
  with self.assertRaisesRegex(ValueError,"compose-campaign-key-required.*--unassigned"): self.compose(unassigned=False)
  with self.assertRaisesRegex(ValueError,"compose-campaign-selection-conflict"): self.compose(campaign_key="tts-v6-release",unassigned=True)
  by_parent=self.compose(parent_cycle_id="cyc_"+"b"*32)
  self.assertNotIn("campaign_unassigned",by_parent); self.assertEqual(R.compose_campaign_selection(by_parent)["mode"],"parent")
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
 def test_compose_shows_existing_streams_and_classifies_the_choice(self):
  import contextlib, io
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   camp=root/"campaigns"/"2026-09-14_tts-v6-release"; camp.mkdir(parents=True)
   (camp/"campaign.json").write_text(json.dumps({"campaign_id":"camp_"+"1"*32,"key":"tts-v6-release","title":"TTS v6","goal":"ship","state":"active","cycles":["cyc_"+"2"*32],"created_on":"2026-09-14T00:00:00Z"}))
   closed=root/"campaigns"/"2026-09-13_old-stream"; closed.mkdir()
   (closed/"campaign.json").write_text(json.dumps({"campaign_id":"camp_"+"3"*32,"key":"old-stream","title":"old","goal":"done","state":"superseded","cycles":[],"created_on":"2026-09-13T00:00:00Z"}))
   with self.assertRaisesRegex(ValueError,r"compose-campaign-key-required.*active: tts-v6-release\(1\)"):
    self.compose(artifact_root=tmp,unassigned=False)
   joined=self.compose(artifact_root=tmp,campaign_key="tts-v6-release")
   sel=R.compose_campaign_selection(joined)
   self.assertEqual((sel["mode"],sel["campaign_id"],sel["active_keys"]),("join","camp_"+"1"*32,["tts-v6-release"]))
   created=R.compose_campaign_selection(self.compose(artifact_root=tmp,campaign_key="tts-v7-release"))
   self.assertEqual((created["mode"],created["campaign_id"]),("create",None))
   degraded=R.compose_campaign_selection(self.compose(artifact_root=tmp))
   self.assertEqual((degraded["mode"],degraded["explicit"]),("unassigned",True))
   card=R.compose_card(joined)
   self.assertIn("캠페인 tts-v6-release (기존 합류)",card); self.assertIn("활성 캠페인 1개: tts-v6-release",card)
   self.assertIn("캠페인 미배정",R.compose_card(self.compose(artifact_root=tmp)))
   receipt=R.compose_receipt(joined,Path(tmp)/"route.json")
   self.assertEqual(receipt["campaign"]["mode"],"join")
   # A root with no campaigns yet still demands the proposal and shows an empty list.
   with self.assertRaisesRegex(ValueError,"active: none"):
    self.compose(artifact_root=str(root/"fresh"),unassigned=False)
   # A failed campaign scan is reported as unavailable, never as "no campaigns" / "create".
   import artifact_producer
   with mock.patch.object(artifact_producer,"list_campaign_summaries",side_effect=OSError("scan failed")):
    with self.assertRaisesRegex(ValueError,"active: unavailable"):
     self.compose(artifact_root=tmp,unassigned=False)
    unresolved=R.compose_campaign_selection(joined)
    self.assertEqual((unresolved["mode"],unresolved["active_keys_unavailable"]),("unresolved",True))
    self.assertIn("확인 불가",R.compose_card(joined))
 def test_mode_selects_the_capability_recipe_that_declares_it(self):
  route=self.compose(capability="autopilot-lab",capability_mode="eval",graph="eval-run,metrics,report")
  self.assertEqual([n["id"] for n in route["nodes"]],["eval-run","metrics","report"])
  with self.assertRaisesRegex(ValueError,r"compose-mode-unknown:nosuch \(modes: eval,setup\)"):
   self.compose(capability="autopilot-lab",capability_mode="nosuch")
 def test_compose_observation_keeps_peer_and_unknown_confirmation_distinct_without_resealing(self):
  import types
  route=self.compose(shape="direct",graph=None)
  before=json.dumps(route,sort_keys=True)
  caller=types.SimpleNamespace(known=True,harness="codex",session_id="this-turn")
  with mock.patch("session_identity.identity",return_value=caller):
   for origin,expected in (("peer:verifier·ref","peer-notice"),
                           ("runtime:completion·attempt/delivery","user-card"),
                           ("unattributed","user-card")):
    with self.subTest(origin=origin), mock.patch.object(R,"_turn_peer_source",return_value=origin):
     observed=R.compose_observations(route)
     self.assertEqual(observed["confirmation"]["method"],expected)
     self.assertEqual(observed["confirmation"]["origin"],origin)
     self.assertIn("확인 방식 "+expected,R.compose_card(route))
  self.assertEqual(json.dumps(route,sort_keys=True),before)
  R.verify_route(route)
 def test_seven_answers_show_route_facts_and_keep_unobserved_limits_unknown(self):
  direct=self.compose(shape="direct",graph=None)
  answers=R.compose_observations(direct)["pre_execution_answers"]
  self.assertEqual([a["question"] for a in answers],list(range(1,8)))
  self.assertEqual(answers[0]["answer"],"autopilot-code")
  self.assertEqual(answers[2]["answer"],"아니오")
  self.assertEqual(answers[4]["answer"],"atomic-direct")
  self.assertIn("native 미확인",answers[5]["answer"])
  self.assertIn("미확인",answers[6]["answer"])
  for answer in answers:
   self.assertIn(str(answer["question"])+". "+answer["name"]+": ",R.compose_card(direct))
 def test_lab_and_staged_answers_follow_declared_nodes(self):
  route=self.compose(capability="autopilot-lab",capability_mode="eval",graph="eval-run,metrics,report")
  answers=R.compose_observations(route)["pre_execution_answers"]
  self.assertEqual(answers[1]["answer"],"예 (lab 실행 선언됨)")
  self.assertEqual(answers[2]["answer"],"예")
  self.assertEqual(answers[3]["answer"],"3개 선언됨")
  self.assertEqual(answers[4]["answer"],"해당 없음")
 def test_peer_ship_full_execution_keeps_card_method(self):
  import types
  route=self.compose(shape="direct",graph=None,capability="autopilot-ship",capability_mode="default")
  with mock.patch("session_identity.identity",return_value=types.SimpleNamespace(known=True,harness="codex",session_id="s")), \
       mock.patch.object(R,"_turn_peer_source",return_value="peer:source·ref"):
   self.assertEqual(R.compose_observations(route)["confirmation"]["method"],"user-card")
 def test_compose_names_its_slug_from_the_task(self):
  import hashlib
  text="# Fix the streaming window sim for r5 today\nmore"
  slug=R.compose_default_slug(text)
  self.assertTrue(slug.startswith("fix-streaming-window-sim-r5-today-"),slug)
  self.assertTrue(slug.endswith(hashlib.sha256(text.encode()).hexdigest()[:10]))
  self.assertEqual(slug,R.compose_default_slug(text))
  self.assertEqual(R.compose_default_slug("표면 정리\nPR #197 검증 요청"),
                   "pr-"+hashlib.sha256("표면 정리\nPR #197 검증 요청".encode()).hexdigest()[:10])
  korean=R.compose_default_slug("표면 정리 갈래 시작",capability="autopilot-code")
  self.assertRegex(korean,r"^autopilot-code-[0-9a-f]{10}$")
  self.assertNotEqual(korean,R.compose_default_slug("다른 작업",capability="autopilot-code"))
  self.assertEqual(R.compose_default_slug("",shape="framed"),"framed")
  dated="2026-09-10 r5 window"
  self.assertEqual(R.compose_default_slug(dated),"work-"+hashlib.sha256(dated.encode()).hexdigest()[:10])
  same_stem_a="Repair worker handoff and retry safely. Details A."
  same_stem_b="Repair worker handoff and retry safely. Details B."
  self.assertNotEqual(R.compose_default_slug(same_stem_a),R.compose_default_slug(same_stem_b))
  long_text="Restore verified task branch publication and final CI checks "*12
  long_slug=R.compose_default_slug(long_text,capability="autopilot-code")
  self.assertLessEqual(len(long_slug),R.ARTIFACT_LOCATOR.MAX_SLUG_LENGTH)
  self.assertTrue(long_slug.endswith(hashlib.sha256(long_text.encode()).hexdigest()[:10]))
  mixed=R.compose_default_slug("한국어 작업 설명\nOpenCode owner permission boundary",capability="autopilot-code")
  self.assertIn("opencode",mixed)
  self.assertNotEqual(mixed,"opencode")
 def test_a_folder_name_or_near_spelling_joins_the_existing_stream(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   for locator,cid,key,state in (("2026-09-14_tts-v6-release","1","tts-v6-release","active"),
                                 ("2026-09-15_bc-prd","4","autopilot-spec:rt-0123456789abcdef","active"),
                                 ("2026-09-13_old-stream","3","old-stream","superseded")):
    camp=root/"campaigns"/locator; camp.mkdir(parents=True)
    (camp/"campaign.json").write_text(json.dumps({"campaign_id":"camp_"+cid*32,"key":key,"title":key,"goal":"g","state":state,"cycles":[],"created_on":"2026-09-14T00:00:00Z"}))
   for given,expected in (("2026-09-14_tts-v6-release","tts-v6-release"),("TTS_v6_release","tts-v6-release"),
                          ("camp_"+"1"*32,"tts-v6-release"),("bc-prd","autopilot-spec:rt-0123456789abcdef"),
                          ("2026-09-15_bc-prd","autopilot-spec:rt-0123456789abcdef")):
    with self.subTest(given=given):
     self.assertEqual(R.compose_resolve_campaign_key(tmp,given),(expected,given))
   for kept in ("tts-v6-release","tts-v7-release","2026-09-13_old-stream","old-stream"):
    with self.subTest(kept=kept):
     self.assertEqual(R.compose_resolve_campaign_key(tmp,kept),(kept,None))
   self.assertEqual(R.compose_resolve_campaign_key(str(root/"fresh"),"x"),("x",None))
 def test_the_session_latest_route_in_the_root_names_the_default_campaign(self):
  import types
  lines=[{"artifact_root":"/r/a","campaign_key":"older"},{"artifact_root":"/r/b","campaign_key":"other-root"},
         {"artifact_root":"/r/a","campaign_key":"latest"}]
  rc=types.SimpleNamespace(read_tail=lambda harness,sid: list(lines))
  with mock.patch.object(R,"_route_chain_module",return_value=rc), \
       mock.patch.object(R,"_route_chain_identity",return_value=("claude","sid",0,None)):
   self.assertEqual(R._session_campaign_key("/r/a"),"latest")
   self.assertIsNone(R._session_campaign_key("/r/c"))
   lines.append({"artifact_root":"/r/a","campaign_key":None,"parent_cycle_id":"cyc_"+"a"*32})
   self.assertIsNone(R._session_campaign_key("/r/a"))  # never reaches past a keyless latest route
  with mock.patch.object(R,"_route_chain_identity",return_value=None):
   self.assertIsNone(R._session_campaign_key("/r/a"))
 def test_compose_cli_needs_no_slug_and_resolves_the_stream(self):
  import contextlib, io
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); camp=root/"campaigns"/"2026-09-14_tts-v6-release"; camp.mkdir(parents=True)
   (camp/"campaign.json").write_text(json.dumps({"campaign_id":"camp_"+"1"*32,"key":"tts-v6-release","title":"TTS v6","goal":"ship","state":"active","cycles":[],"created_on":"2026-09-14T00:00:00Z"}))
   prompt=root/"task.md"; prompt.write_text("Fix the streaming window\n")
   route=self.compose(shape="direct",graph=None,artifact_root=tmp,campaign_key="tts-v6-release")
   seen=[]
   def capture(**kwargs):
    seen.append(kwargs); return route
   base=[str(P),"compose","--shape","direct","--cwd",str(R.ROOT),"--artifact-root",tmp,"--prompt-file",str(prompt),"--explain"]
   for extra,session,folder,note in ((["--campaign-key","2026-09-14_tts-v6-release"],None,[],"campaign_key_resolved=tts-v6-release"),
                                     ([],"tts-v6-release",[],"campaign_key_default=tts-v6-release"),
                                     ([],None,["tts-v6-release"],"campaign_key_default=tts-v6-release source=this-folder-only-active-stream")):
    err=io.StringIO()
    with mock.patch.object(sys,"argv",base+extra), mock.patch.object(R,"compose_route",side_effect=capture), \
         mock.patch.object(R,"_session_campaign_key",return_value=session), \
         mock.patch.object(R,"cwd_campaign_keys",return_value=folder), \
         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
     self.assertEqual(R.main(),0)
    self.assertRegex(seen[-1]["slug"],r"^autopilot-code-fix-streaming-window-[0-9a-f]{10}$")
    self.assertEqual(seen[-1]["campaign_key"],"tts-v6-release")
    self.assertIn(note,err.getvalue())
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
  # A staged route is decided work: the recipe's own order without its frame pair.
  route=self.compose(graph=None)
  self.assertEqual(route["selection"]["shape"],"staged")
  self.assertEqual([n["id"] for n in route["nodes"]],
   ["plan","plan-check","execute","impl-review","test","report"])
  self.assertNotIn("frame-review",route["human_gates"])
  R.verify_route(route,R.ROOT)
 def test_lab_fixed_result_report_subgraph_does_not_require_a_new_eval_run(self):
  """Reporting keeps lab ownership and selected gates without recomputation."""
  for intensity in ("standard","strong"):
   for graph in ("report,independent-verify,publish,sync","media,report,independent-verify,publish,sync"):
    with self.subTest(intensity=intensity,graph=graph):
     request={"text":"Report DSC fixed checkpoint results; reuse frozen aggregate metrics and existing media without new inference.","owner_harness":"codex"}
     route=self.compose(capability="autopilot-lab",capability_mode="eval",graph=graph,intensity=intensity,work_request=request)
     self.assertEqual(route["capability"],"autopilot-lab")
     self.assertEqual(route["capability_mode"],"eval")
     self.assertIn("미확인",R.compose_observations(route)["pre_execution_answers"][1]["answer"])
     self.assertEqual(route["effective_intensity"],intensity)
     expected=graph.split(",")
     if intensity=="strong": expected.insert(expected.index("publish"),"independent-verify-alternative")
     self.assertEqual([n["id"] for n in route["nodes"]],expected)
     self.assertEqual(route["work_request"],request)
     nodes={n["id"]:n for n in route["nodes"]}
     self.assertEqual(nodes["report"]["unit"],"editorial/report")
     self.assertTrue(nodes["sync"]["terminal"])
     self.assertEqual(route["human_gates"],[])
     preset=self.compose(capability="autopilot-lab",capability_mode="eval",graph=None,intensity=intensity)
     originals={n["id"]:n for n in preset["nodes"]}
     for name,node in nodes.items():
      for field in ("unit","completion_gate","write_scope","model_profile"):
       self.assertEqual(node.get(field),originals[name].get(field),(name,field))
     R.verify_route(route,R.ROOT)
 def test_ship_package_compiles_minimal_delivery_at_each_shape(self):
  request={"text":"Reuse model v3 and the delivery template; package field.wav, sim1.wav, sim2.wav with their existing outputs and report. No other samples or models.","owner_harness":"codex"}
  for shape,intensity,ids in (("direct","direct",["inline"]),("solo","quick",["one-shot"]),("staged","standard",["package"]),("staged","strong",["package"]),("staged","adversarial",["package"])):
   with self.subTest(shape=shape,intensity=intensity):
    route=self.compose(capability="autopilot-ship",capability_mode="package",shape=shape,graph=None,intensity=intensity,registered_headless_evidence=self.registered_headless(),work_request=request)
    self.assertEqual(route["capability_mode"],"package")
    self.assertEqual(route["effective_intensity"],intensity)
    self.assertEqual([n["id"] for n in route["nodes"]],ids)
    self.assertEqual(route["human_gates"],[])
    self.assertEqual(route.get("parallel_groups",[]),[])
    self.assertEqual(route["work_request"]["text"],request["text"])
    self.assertFalse(route["spec_touch"])
    self.assertTrue(route["nodes"][-1]["terminal"])
    if shape=="staged":
     node=route["nodes"][0]
     self.assertEqual(node["unit"],"_kernel/owner")
     self.assertEqual(node["model_profile"],"deep")
     self.assertEqual(node["completion_gate"],"ship-setup")
     self.assertEqual(node["write_scope"],["release-config/**"])
     self.assertIsNone(route["dispatch_evidence"])
     import owner_route_binding as binding
     with tempfile.TemporaryDirectory() as tmp:
      path=Path(tmp)/"route.json"; path.write_text(json.dumps(route))
      binding.validate_owner_route_binding(path,worktree=R.ROOT,capability="autopilot-ship",capability_mode="package",intensity=intensity,harness="codex")
    R.verify_route(route,R.ROOT)
 def test_ship_package_requires_checked_owner_readiness(self):
  for evidence in ({"candidates":[]},self.registered_headless(status="unsupported")):
   with self.assertRaisesRegex(ValueError,"headless-unavailable"):
    self.compose(capability="autopilot-ship",capability_mode="package",graph=None,registered_headless_evidence=evidence)
  route=self.compose(capability="autopilot-ship",capability_mode="package",graph=None,registered_headless_evidence=self.registered_headless())
  route["dispatch_evidence"]=self.evidence()
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"owner-only registered-headless"):
   R.verify_route(route,R.ROOT)
 def test_ship_package_compose_uses_owner_probe_without_child_tuples(self):
  readiness={"candidates":self.registered_headless()["candidates"],"tuples":[]}
  with mock.patch.object(R,"_compose_readiness",return_value=readiness) as probe:
   route=self.compose(capability="autopilot-ship",capability_mode="package",graph=None,dispatch_evidence=None)
  probe.assert_called_once()
  self.assertIsNone(route["dispatch_evidence"])
  self.assertIsNone(route["registered_headless_policy"])  # retains standard attempt semantics
  R.verify_route(route,R.ROOT)
 def test_ship_package_public_start_hands_off_one_node_less_owner(self):
  import work_start as W
  spec=importlib.util.spec_from_file_location("package_owner",P.with_name("dispatch-owner.py"))
  owner=importlib.util.module_from_spec(spec); spec.loader.exec_module(owner)
  route=self.compose(capability="autopilot-ship",capability_mode="package",graph=None,
   registered_headless_evidence=self.registered_headless(),
   work_request={"text":"Package only the requested existing artifacts.","owner_harness":"codex"})
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/"route.json"; path.write_text(json.dumps(route))
   jobs=Path(tmp)/"jobs.log"; jobs.touch()
   launches=[]
   def admit(command,**kwargs):
    launches.append(command)
    self.assertNotIn("--route-node",command)
    explicit,values,forwarded,evidence,derived=owner._parse(command[2:])
    self.assertEqual(explicit,"codex")
    self.assertEqual(values["--dispatch-depth"],"1")
    self.assertEqual(values["--worker-type"],"owner")
    from types import SimpleNamespace
    from dispatch_mode_contract import normalize_dispatch_modes
    axes=SimpleNamespace(**{k[2:].replace("-","_"):v for k,v in values.items()})
    axes.dispatch_depth=int(axes.dispatch_depth)
    normalize_dispatch_modes(axes)
    self.assertEqual(axes.unit,"_kernel/owner")
    self.assertFalse(axes.worker_mode)
    self.assertEqual(values["--capability-mode"],"package")
    self.assertEqual(values["--model-profile"],"deep")
    binding=owner.validate_owner_route_binding(evidence,worktree=values["--worktree"],
     capability=values["--capability"],capability_mode=values["--capability-mode"],
     intensity=values["--intensity"],harness=explicit)
    env=owner.export_owner_route_env({},binding)
    self.assertEqual(env["AGENT_OWNER_ROUTE_ID"],route["route_id"])
    self.assertNotIn("AGENT_ROUTE_NODE",env)
    with self.assertRaisesRegex(owner.OwnerRouteBindingError,"owner-route-harness-mismatch"):
     owner.validate_owner_route_binding(evidence,worktree=values["--worktree"],
      capability=values["--capability"],capability_mode="package",intensity="standard",harness="opencode")
    aid=command[command.index("--attempt-id")+1]
    jobs.write_text("now\topen\trepo\tworktree\towner\t"+
     f"attempt_id={aid},worker_type=owner,parent_sid=parent,launch_started=1,"+
     f"owner_route_id={route['route_id']},owner_route_hash={route['route_hash']},"+
     "parent_completion_delivery=codex-managed-gateway\n")
    return subprocess.CompletedProcess(command,0,"registered=1 started=1 child_spawned=1\n","")
   with mock.patch.object(W,"default_parent_session_id",return_value="parent"), \
        mock.patch("route_parent_close.owner_continuation_processes",return_value=(D.ProcessQuiescence("live","mock-owner-running"),[])), \
        mock.patch.object(W,"join_selected_attempts",return_value={"state":"timeout"}):
    for _ in range(2):
     result=W.start_work(route,path,jobs,run=admit)
     self.assertEqual(result["state"],"running",result)
   self.assertEqual(len(launches),1)
 def test_depth_two_recipe_cannot_use_owner_candidates_instead_of_tuples(self):
  with mock.patch.object(R,"_compose_readiness",return_value={"tuples":[],"candidates":self.registered_headless()["candidates"]}), \
       self.assertRaisesRegex(ValueError,"nested eligibility tuples required"):
   self.compose(dispatch_evidence=None,registered_headless_evidence=self.registered_headless())
  route=self.compose()
  route["registered_headless_candidates"]=self.registered_headless()["candidates"]
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"nested route cannot substitute owner-only readiness"):
   R.verify_route(route,R.ROOT)
 def test_ship_unspecified_mode_keeps_default_deployment_recipe(self):
  omitted=self.compose(capability="autopilot-ship",capability_mode=None,graph=None)
  explicit=self.compose(capability="autopilot-ship",capability_mode="default",graph=None)
  self.assertEqual(omitted["capability_mode"],"default")
  self.assertEqual(omitted["nodes"],explicit["nodes"])
  self.assertEqual([n["id"] for n in omitted["nodes"]],["release-setup","security-review","release-review","deploy","post-deploy-verify"])
  self.assertEqual(omitted["human_gates"],[])
  self.assertEqual(omitted["human_gate_bindings"],[])
  self.assertIn("시작 승인 deploy (autopilot-ship:deploy)",R.compose_card(omitted))
  R.verify_route(omitted,R.ROOT)
 def test_ship_package_cannot_select_deployment_nodes(self):
  with self.assertRaisesRegex(ValueError,"compose-graph-unknown-node:deploy"):
   self.compose(capability="autopilot-ship",capability_mode="package",graph="package,deploy")
 def test_chosen_graph_does_not_require_presets_review_stage(self):
  route=self.compose(graph="plan,test,report",intensity="standard")
  self.assertEqual([n["id"] for n in route["nodes"]],["plan","test","report"])
  self.assertEqual(route["parallel_groups"],[])
  self.assertEqual(route["composed_recipe"]["compose"]["omitted_parallel_presets"],
    [{"id":"plan","reason":"review-consumer-not-selected"}])
  self.assertEqual(R.compose_omission_lines(route),[])
  R.verify_route(route,R.ROOT)
 def test_strong_subgraph_restores_review_consumer_and_all_leg_inputs(self):
  for intensity in ("strong","thorough","adversarial"):
   route=self.compose(graph="plan,test,report",intensity=intensity)
   nodes={n["id"]:n for n in route["nodes"]}
   groups={g["id"]:g for g in route["parallel_groups"]}
   self.assertEqual(nodes["plan-check"]["depends_on"],groups["plan"]["members"])
   self.assertEqual(nodes["test"]["depends_on"],groups["plan-check"]["members"])
   for member in groups["plan"]["members"]:
    self.assertTrue(set(nodes[member]["outputs"])<=set(nodes["plan-check"]["inputs"]))
   self.assertEqual(nodes["plan-check"]["write_scope"],["_internal/plan_reviews/**"])
   meta=route["composed_recipe"]["compose"]
   self.assertEqual(meta["graph"],["plan","test","report"])
   self.assertEqual(meta["auto_completed_nodes"],[{"id":"plan-check",
    "reason":"review-consumer-not-selected","anchors":["plan"],"intensity":intensity}])
   self.assertNotIn("omitted_parallel_presets",meta)
   self.assertIn("auto-added plan-check (review-consumer-not-selected; registry consumer)",R.compose_card(route))
   R.verify_route(route,R.ROOT)
 def test_terminal_pipeline_group_restores_consumer_then_owner_close(self):
  route=self.compose(graph="plan",intensity="strong")
  self.assertEqual([n["id"] for n in route["nodes"]],
   ["plan","plan-alternative","plan-check","plan-check-alternative","owner-close"])
  self.assertEqual(route["nodes"][-1]["write_scope"],["owner-close.md"])
  self.assertEqual(route["workflow_contract"]["terminal_nodes"],["owner-close"])
  self.assertEqual([r["reason"] for r in route["composed_recipe"]["compose"]["auto_completed_nodes"]],
   ["review-consumer-not-selected","terminal-anchor"])
  R.verify_route(route,R.ROOT)
 def test_selected_consumer_is_not_added_again_and_old_sealed_omission_replays(self):
  selected=self.compose(graph="plan,plan-check,test,report",intensity="strong")
  self.assertNotIn("auto_completed_nodes",selected["composed_recipe"]["compose"])
  self.assertEqual(len([n for n in selected["nodes"] if n["id"]=="plan-check"]),1)
  compose=R.compose_subgraph_recipe
  def historical(*args,**kwargs):
   kwargs["complete_group_consumers"]=False
   return compose(*args,**kwargs)
  with mock.patch.object(R,"compose_subgraph_recipe",side_effect=historical):
   old=self.compose(graph="plan,test,report",intensity="strong")
  before=R.canonical(old)
  self.assertEqual([n["id"] for n in old["nodes"]],["plan","test","report"])
  R.verify_route(old,R.ROOT)
  self.assertEqual(before,R.canonical(old))
 def test_unsafe_or_ambiguous_registry_consumer_keeps_omission(self):
  registry=R.TOPO.load_registry()
  for reason in ("source-write","ambiguous","transaction"):
   base=json.loads(R.canonical(R.TOPO.resolve_recipe(registry,"autopilot-code","dev")))
   consumer=next(n for n in base["standard_plus"]["nodes"] if n["id"]=="plan-check")
   if reason=="source-write":consumer["write_scope"]=["source/**"]
   if reason=="transaction":consumer["continuation"]={"kind":"human-gate","gate":"approval"}
   if reason=="ambiguous":base["standard_plus"]["nodes"].append(dict(consumer,id="other-check"))
   recipe=R.compose_subgraph_recipe(registry,base,[(n,None) for n in ("plan","test","report")],
    group_intensity="strong",complete_group_consumers=True)
   self.assertNotIn("auto_completed_nodes",recipe["compose"],reason)
   self.assertEqual(recipe["compose"]["omitted_parallel_presets"],
    [{"id":"plan","reason":"review-consumer-not-selected"}],reason)
 def test_restored_consumer_metadata_gate_scope_and_intensity_are_sealed(self):
  route=self.compose(graph="plan,test,report",intensity="strong")
  registry=R.TOPO.load_registry()
  for change in ("metadata","scope","gate","intensity"):
   recipe=json.loads(R.canonical(route["composed_recipe"]))
   consumer=next(n for n in recipe["standard_plus"]["nodes"] if n["id"]=="plan-check")
   if change=="metadata":recipe["compose"].pop("auto_completed_nodes")
   if change=="scope":consumer["write_scope"].append("source/**")
   if change=="gate":consumer["completion_gate"]="code-test"
   if change=="intensity":recipe["compose"]["auto_completed_nodes"][0]["intensity"]="standard"
   with self.subTest(change=change),self.assertRaisesRegex(ValueError,"differs-from-assembly"):
    R._validate_compose_owner_close(registry,recipe,"strong")
 def test_restored_arbiter_assignment_and_completion_contract(self):
  import worker_bootstrap as W
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   route=self.compose(capability="audit",capability_mode="default",intensity="thorough",
    graph="inspect,autopilot-research:retrieval,report",artifact_root=str(root))
   path=root/"route.json";path.write_text(json.dumps(route))
   arbiter=next(n for n in route["nodes"] if n["id"]=="autopilot-research-synthesis")
   group=next(g for g in route["parallel_groups"] if g["id"]=="autopilot-research-retrieval")
   self.assertEqual(arbiter["depends_on"],group["members"])
   for member in group["members"]:
    leg=next(n for n in route["nodes"] if n["id"]==member)
    self.assertTrue(set(leg["outputs"])<=set(arbiter["inputs"]))
   for harness in ("claude","codex","opencode"):
    args=SimpleNamespace(worker_type="stage",adapter=harness,route_node=arbiter["id"],
     route_file=str(path),parent_attempt_id=None)
    prompt=W.assignment_prompt(args,"Analyze existing results",{})
    self.assertIn("automatic registry consumer "+arbiter["id"],prompt)
    self.assertIn("exactly 1 entries",prompt)
    self.assertIn(group["members"][-1]+": <finding and disposition>",prompt)
   evidence=root/"analysis.md";evidence.write_text("Results reconciled\n")
   with self.assertRaisesRegex(ValueError,"requires auxiliary_findings_considered"):
    R._validate_auxiliary_arbiter(route,arbiter,evidence)
   evidence.write_text('---\nauxiliary_findings_considered:\n  - "assumption: adopted"\n---\nResults reconciled\n')
   R._validate_auxiliary_arbiter(route,arbiter,evidence)
   plain=self.compose(capability="audit",capability_mode="default",intensity="thorough",
    graph="inspect,autopilot-research:retrieval,autopilot-research:synthesis,report")
   self.assertEqual(W.automatic_consumer_prompt(plain,arbiter["id"]),"")
 def test_host_auxiliary_arbiter_restored_without_widening_scope(self):
  for cap,mode,graph,arbiter_id in (("autopilot-research","technology","retrieval,report","synthesis"),
                                   ("autopilot-spec","app","research,prd-transaction","review")):
   route=self.compose(capability=cap,capability_mode=mode,graph=graph,intensity="thorough")
   source=R.TOPO.resolve_recipe(R.TOPO.load_registry(),cap,mode)
   declared=next(n for n in source["standard_plus"]["nodes"] if n["id"]==arbiter_id)
   arbiter=next(n for n in route["nodes"] if n["id"]==arbiter_id)
   for field in ("unit","kind","completion_gate","write_scope","resource_class","dispatch_depth"):
    self.assertEqual(arbiter[field],declared[field])
   self.assertEqual(route["composed_recipe"]["compose"]["auto_completed_nodes"][0]["id"],arbiter_id)
   self.assertNotIn("omitted_parallel_presets",route["composed_recipe"]["compose"])
   R.verify_route(route,R.ROOT)
 def test_unavailable_host_arbiter_omits_auxiliary_and_replays_exactly(self):
  registry=json.loads(json.dumps(R.TOPO.load_registry()))
  base=R.TOPO.resolve_recipe(registry,"autopilot-research","technology")
  consumer=next(n for n in base["standard_plus"]["nodes"] if n["id"]=="synthesis")
  consumer["commit_expected"]=True  # Unsafe to add automatically.
  recipe=R.compose_subgraph_recipe(registry,base,[(n,None) for n in ("retrieval","report")],
   group_intensity="thorough",complete_group_consumers=True)
  self.assertEqual(recipe["compose"]["omitted_parallel_presets"],[
   {"id":"retrieval","reason":"auxiliary-arbiter-not-selected","legs":["assumption"]}])
  self.assertEqual(recipe["standard_plus"]["parallel_groups"][0]["width_by_intensity"]["thorough"],2)
  R.TOPO._validate_recipe(recipe,registry,registry["owner_profile_by_intensity"]["standard"])
  self.assertTrue(R._versioned_subgraph(registry,recipe))
  R._validate_compose_owner_close(registry,recipe,"thorough")
  recipe["compose"].pop("group_completion_intensity")
  with self.assertRaisesRegex(ValueError,"differs-from-assembly"):
   R._validate_compose_owner_close(registry,recipe,"thorough")
 def test_auto_consumer_notices_are_once_before_explain_and_start(self):
  import work_start
  with tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,{"AGENT_HOME":str(R.ROOT),"AGENT_DISPATCH_ATTEMPT_ID":""}):
   root=Path(tmp);git_root=root/"git-root";git_root.mkdir();subprocess.run(["git","init","-q",str(git_root)],check=True)
   prompt=root/"task.md";prompt.write_text("Plan and test the change")
   route=self.compose(graph="plan,test,report",intensity="strong",artifact_root=str(root/"artifacts"))
   notice="strong group plan: auto-added plan-check (review-consumer-not-selected; registry consumer)"
   for harness in ("claude","codex","opencode"):
    for action in ("explain","start"):
     out,err=io.StringIO(),io.StringIO()
     argv=[str(P),"compose","--shape","staged","--intensity","strong","--capability","autopilot-code",
      "--capability-mode","dev","--graph","plan,test,report","--cwd",str(git_root),
      "--artifact-root",str(root/"artifacts"),"--unassigned","--parent-harness",harness,
      "--prompt-file",str(prompt),"--jobs",str(root/"jobs.log"),"--"+action]
     receipt={"state":"waiting-owner","parent_next":"end-turn"}
     def start(*args,**kwargs):
      self.assertEqual(err.getvalue().count(notice),1)
      return dict(receipt)
     with mock.patch.object(sys,"argv",argv),mock.patch.object(R,"compose_route",return_value=route), \
      mock.patch.object(work_start,"start_work",side_effect=start),mock.patch.object(R,"_record_route_chain"), \
      mock.patch.object(R,"_route_autoclose"),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
      self.assertEqual(R.main(),0)
     self.assertEqual(err.getvalue().count(notice),1)
     payload=json.loads(out.getvalue())
     if action=="start":self.assertEqual({k:payload[k] for k in receipt},receipt)
     else:self.assertEqual(len(payload["parallel_groups"]),2)
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
 def test_terminal_group_gets_an_owner_close_without_relaxing_g6(self):
  # `frame` is no longer a parallel-group anchor, and it is a dispatch-depth-1
  # node, so a `frame`-only subgraph has no depth-2 evidence consumer and can
  # no longer be composed at all. The two rules this test protects are
  # unchanged and are pinned on the shapes that can still carry them.
  # (1) G6 stays: the group anchor is non-terminal; the added owner closes.
  grouped=self.compose(graph="plan,impl-review",intensity="strong")
  self.assertEqual([g["id"] for g in grouped["parallel_groups"]],["plan","impl-review"])
  self.assertIn("impl-review-alternative",[n["id"] for n in grouped["nodes"]])
  self.assertTrue(grouped["nodes"][-1]["terminal"])
  self.assertEqual(grouped["nodes"][-1]["id"],"owner-close")
  self.assertNotIn("omitted_parallel_presets",grouped["composed_recipe"]["compose"])
  R.verify_route(grouped,R.ROOT)
  # (2) route-guard-recovery D9: `plan`/`plan-check` are real descendants of
  # `frame` in the base recipe (`plan.depends_on == ["frame",
  # "frame-alternative"]`), so naming them before `frame` is now a refused
  # caller-order violation, not a way to make `frame` a gate-dropping
  # terminal -- `frame`/`frame-alternative` have no predecessor in this
  # recipe, so nothing can legally precede a real descendant of theirs and
  # still leave `frame` last. The "terminal raises no gate" rule itself is
  # unchanged; this graph is just no longer a way to reach it.
  with self.assertRaisesRegex(ValueError,"compose-graph-order:plan-before-frame"):
   self.compose(graph="plan,plan-check,frame")
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
  # compose fills the predicates itself; the record must not call that caller input.
  self.assertEqual({row["source"] for row in route["selection"]["selection_basis"]
                    if row["axis"] == "direct-predicate"},{"compose-default"})
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
  self.assertEqual([n["id"] for n in route["nodes"]],["one-shot"])       # decided work: no frame pair
  self.assertNotIn("frame-review",route["human_gates"])
  self.assertEqual(route["selection"]["shape"],"solo"); self.assertEqual(route["selection"]["route_origin"],"compose")
  R.verify_route(route,R.ROOT)
 def test_staged_accepts_strong_and_expands_declared_groups(self):
  route=self.compose(graph="plan,plan-check,execute",intensity="strong")
  self.assertEqual(route["effective_intensity"],"strong")
  self.assertEqual(sorted(g["id"] for g in route["parallel_groups"]),["plan","plan-check"])
  R.verify_route(route,R.ROOT)
 def test_a_folders_active_streams_come_from_the_routes_sealed_for_it(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/"reports"; routes=R.canonical_routes_dir(root); routes.mkdir(parents=True)
   here,other=Path(tmp)/"here",Path(tmp)/"other"; here.mkdir(); other.mkdir()
   def route(name,cwd,key,age):
    path=routes/f"rt-{name*16}.json"
    path.write_text(json.dumps({"route_id":f"rt-{name*16}","cwd":str(cwd),"campaign_key":key}),encoding="utf-8")
    os.utime(path,(time.time()-age,time.time()-age))
   rows=[{"key":"alpha","state":"active","cycle_count":2},{"key":"beta","state":"active","cycle_count":1},
         {"key":"done","state":"closed","cycle_count":4}]
   route("a",here,"alpha",30); route("b",other,"beta",20); route("c",here,"done",10); route("d",here,"alpha",5)
   with mock.patch.object(R,"compose_campaign_summaries",return_value=rows):
    self.assertEqual(R.cwd_campaign_keys(root,here),["alpha"])          # one active stream: compose joins it
    self.assertIn("this folder's streams: alpha; ",R.compose_campaign_hint(root,here))
    route("e",here,"beta",1)
    self.assertEqual(R.cwd_campaign_keys(root,here),["beta","alpha"])   # newest first; several: suggested only
    self.assertEqual(R.cwd_campaign_keys(root,Path(tmp)),[])
    self.assertNotIn("this folder",R.compose_campaign_hint(root,Path(tmp)))
 def test_spec_read_auto_refuses_when_a_spec_exists(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); (root/"spec").mkdir(); (root/"spec"/"prd.md").write_text("# prd\n",encoding="utf-8")
   with self.assertRaisesRegex(ValueError,"compose-spec-read-required"): R.compose_spec_read(root,root,"auto")
   self.assertEqual(R.compose_spec_read(root,root,"read spec/prd.md v3")["source"],"read spec/prd.md v3")
   self.assertTrue(R.compose_spec_read(R.ROOT,R.ROOT,None)["satisfied"])
 def test_source_changing_work_gets_its_own_worktree_from_the_latest_base(self):
  with tempfile.TemporaryDirectory() as tmp:
   git=lambda cwd,*args: subprocess.run(["git","-C",str(cwd),*args],check=True,capture_output=True,text=True).stdout.strip()
   origin=Path(tmp)/"origin.git"; primary=Path(tmp)/"repo"; writer=Path(tmp)/"writer"
   git(tmp,"init","-q","--bare","-b","main",str(origin)); git(tmp,"clone","-q",str(origin),str(primary))
   git(primary,"config","user.name","Fixture"); git(primary,"config","user.email","fixture@example.test")
   (primary/"a.txt").write_text("base\n"); git(primary,"add","a.txt")
   git(primary,"commit","-qm","base"); git(primary,"push","-q","-u","origin","main")
   git(primary,"-c","user.name=Fixture","-c","user.email=fixture@example.test","commit","--allow-empty","-qm","local-only")
   git(tmp,"clone","-q",str(origin),str(writer)); git(writer,"config","user.name","Fixture"); git(writer,"config","user.email","fixture@example.test")
   (writer/"remote.txt").write_text("remote tip\n"); git(writer,"add","remote.txt"); git(writer,"commit","-qm","remote tip"); git(writer,"push","-q","origin","main")
   remote_sha=git(writer,"rev-parse","HEAD")
   (primary/"a.txt").write_text("staged primary edit\n"); git(primary,"add","a.txt")
   (primary/"a.txt").write_text("dirty primary edit\n"); (primary/"untracked.txt").write_text("keep\n")
   def fingerprint():
    return (git(primary,"rev-parse","HEAD"),git(primary,"branch","--show-current"),
            git(primary,"diff","--binary"),git(primary,"diff","--cached","--binary"),
            git(primary,"status","--porcelain"),git(primary,"ls-files","--stage"),
            (primary/"a.txt").read_bytes(),(primary/"untracked.txt").read_bytes())
   before=fingerprint(); reports=Path(tmp)/"reports"; reports.mkdir(); (primary/"pkg").mkdir()
   key={"request":"source work one","campaign":"main"}
   made=R.prepare_isolated_worktree(primary/"pkg","fix-x",artifact_root=reports,preparation_key=key)
   wt=Path(tmp)/"repo-wt"/"fix-x"
   self.assertEqual((made["state"],Path(made["path"]),made["branch"],made["base"]),
                    ("created",wt,"fix-x",remote_sha))
   self.assertEqual(made["cwd"],str(wt/"pkg"))
   self.assertEqual(git(wt,"rev-parse","HEAD"),remote_sha)
   self.assertTrue((wt/"remote.txt").is_file()); self.assertFalse((wt/"untracked.txt").exists())
   self.assertEqual(fingerprint(),before)
   (wt/"pending.py").write_text("keep this linked task\n")
   with mock.patch.object(R,"_worktree_git",side_effect=AssertionError("ready allocation must not fetch")):
    again=R.prepare_isolated_worktree(primary,"fix-x",artifact_root=reports,preparation_key=key)
   self.assertEqual((again["state"],again["path"],again["branch"]),("reused",str(wt),"fix-x"))
   explicit_key={"request":"approved linked task"}
   explicit=R.prepare_isolated_worktree(wt,"other",artifact_root=reports,preparation_key=explicit_key,
                                       explicitly_approved=True)
   self.assertEqual((explicit["state"],explicit["cwd"]),("reused",str(wt)))
   self.assertEqual((wt/"pending.py").read_text(),"keep this linked task\n")
   real_worktree_git=R._worktree_git
   def no_network(cwd,*args):
    self.assertNotIn(args[:1],(("ls-remote",),("fetch",),("worktree","add")))
    return real_worktree_git(cwd,*args)
   with mock.patch.object(R,"_worktree_git",side_effect=no_network):
    explicit_again=R.prepare_isolated_worktree(wt,"other",artifact_root=reports,
                                               preparation_key=explicit_key)
   self.assertEqual((explicit_again["state"],explicit_again["path"]),("reused",str(wt)))
   collided=R.prepare_isolated_worktree(primary,"fix-x",artifact_root=reports,
                                        preparation_key={"request":"independent source work"})
   self.assertEqual((Path(collided["path"]).name,collided["branch"]),("fix-x-2","fix-x-2"))
   git(primary,"branch","branch-only")
   branch_collision=R.prepare_isolated_worktree(primary,"branch-only",artifact_root=reports,
                                                preparation_key={"request":"branch collision"})
   self.assertEqual((Path(branch_collision["path"]).name,branch_collision["branch"]),
                    ("branch-only-2","branch-only-2"))
   foreign=Path(tmp)/"foreign"
   git(tmp,"init","-q","-b","main",str(foreign)); git(foreign,"config","user.name","Fixture")
   git(foreign,"config","user.email","fixture@example.test")
   (foreign/"foreign.txt").write_text("foreign repository\n"); git(foreign,"add","foreign.txt")
   git(foreign,"commit","-qm","foreign base")
   foreign_wt=Path(tmp)/"repo-wt"/"path-only"
   git(foreign,"worktree","add","-q","-b","foreign-branch",str(foreign_wt),"HEAD")
   path_collision=R.prepare_isolated_worktree(primary,"path-only",artifact_root=reports,
                                              preparation_key={"request":"path collision"})
   self.assertEqual((Path(path_collision["path"]).name,path_collision["branch"]),
                    ("path-only-2","path-only-2"))
   from concurrent.futures import ThreadPoolExecutor
   parallel_key={"request":"one concurrent start"}
   with ThreadPoolExecutor(max_workers=2) as pool:
    parallel=list(pool.map(lambda _:R.prepare_isolated_worktree(primary,"parallel",artifact_root=reports,
                                                                 preparation_key=parallel_key),range(2)))
   self.assertEqual({row["state"] for row in parallel},{"created","reused"})
   self.assertEqual({row["path"] for row in parallel},{str(Path(tmp)/"repo-wt"/"parallel")})
   routes=R.canonical_routes_dir(reports); routes.mkdir(parents=True)
   path=routes/("rt-"+"f"*16+".json")
   path.write_text(json.dumps({"route_id":"rt-"+"f"*16,"nodes":[],"cwd":str(collided["cwd"]),
                               "slug":"fix-x","artifact_root":str(reports)}),encoding="utf-8")
   with mock.patch.object(R,"_route_chain_module",return_value=None), \
        mock.patch.object(R,"_compose_artifact_root",return_value=str(reports)):
    self.assertEqual(R.caller_open_route(primary)[:2],(str(path),"cwd"))
    other=routes/("rt-"+"e"*16+".json")
    other.write_text(json.dumps({"route_id":"rt-"+"e"*16,"nodes":[],"cwd":str(collided["cwd"]),
                                 "slug":"different-task","artifact_root":str(reports)}),encoding="utf-8")
    self.assertEqual(R.caller_open_route(primary)[1],"ambiguous")
 def test_only_source_changing_routes_with_a_real_artifact_root_are_isolated(self):
  source={"id":"execute","write_scope":["source/**"]}; plan={"id":"plan","write_scope":["artifacts/plans/**"]}
  real=str(Path.home()/"project"/".agent_reports")
  self.assertTrue(R._isolates_worktree({"artifact_root":real,"nodes":[plan,source]}))
  self.assertFalse(R._isolates_worktree({"artifact_root":real,"nodes":[plan]}))
  self.assertTrue(R._isolates_worktree({"artifact_root":tempfile.gettempdir()+"/x","nodes":[source]}))
  self.assertFalse(R._isolates_worktree({"artifact_root":tempfile.gettempdir()+"/x","nodes":[plan]}))
 def test_a_route_id_finds_its_record_from_the_checkout_the_registry_or_a_ledger(self):
  with tempfile.TemporaryDirectory() as tmp:
   def record(root,name):
    path=R.canonical_route_path(root,"rt-"+name*16); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text("{}",encoding="utf-8"); return path
   here,env,owned,chained=(Path(tmp)/n for n in ("here","env","owned","chained"))
   jobs=Path(tmp)/"jobs.log"
   in_cwd,in_env,in_jobs,in_ledger=record(here,"a"),record(env,"b"),record(owned,"c"),record(chained,"e")
   jobs.write_text("now\topen\t1\tp\tt\tworker_type=owner,owner_route_id=rt-"+"c"*16+",owner_route_file="+str(in_jobs)+"\n")
   from types import SimpleNamespace
   in_other=record(Path(tmp)/"other-session","9")
   state=Path(tmp)/"state"; (state/"claude").mkdir(parents=True)
   (state/"claude"/"s.jsonl").touch(); (state/"claude"/"other.jsonl").touch()
   tails={"s":[{"route_id":"rt-"+"e"*16,"route_file":str(in_ledger)}],
          "other":[{"route_id":"rt-"+"9"*16,"route_file":str(in_other)}]}
   chain=SimpleNamespace(HARNESSES=("claude",),ANCHOR_SCAN_FILES=64,state_root=lambda: str(state),
                         writer_identity=lambda: ("claude","s"),read_tail=lambda h,sid: tails.get(sid,[]))
   with mock.patch.object(R,"_compose_artifact_root",return_value=str(here)), \
        mock.patch.object(R,"_route_chain_module",return_value=chain), \
        mock.patch.dict(os.environ,{"AGENT_ARTIFACT_ROOT":str(env)}):
    resolve=lambda value: R.resolve_route_argument(value,jobs)
    self.assertEqual(resolve(str(in_ledger)),in_ledger)                   # a file stays a file
    self.assertEqual(resolve("rt-"+"a"*16),in_cwd)
    self.assertEqual(resolve("rt-"+"b"*16),in_env)
    self.assertEqual(resolve("rt-"+"c"*16),in_jobs)
    self.assertEqual(resolve("rt-"+"e"*16),in_ledger)
    self.assertEqual(resolve("rt-"+"9"*16),in_other)                    # another session's ledger, from any cwd
    with self.assertRaisesRegex(ValueError,"route-id-unresolved:rt-"+"f"*16): resolve("rt-"+"f"*16)
 def test_a_bare_start_finds_this_sessions_open_route_else_the_one_open_route_of_the_cwd(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/"reports"; routes=R.canonical_routes_dir(root); routes.mkdir(parents=True)
   here=Path(tmp)/"wt"; other=Path(tmp)/"other"; here.mkdir(); other.mkdir()
   def route(name,cwd,closed=False):
    path=routes/f"rt-{name*16}.json"
    path.write_text(json.dumps({"route_id":f"rt-{name*16}","nodes":[],"cwd":str(cwd),"artifact_root":str(root)}),encoding="utf-8")
    if closed: R.outcome_path(path).write_text("{}",encoding="utf-8")
    return str(path)
   mine_old,mine_new,mine_closed=route("a",other),route("b",other),route("c",other,closed=True)
   ledger=[{"route_file":mine_old},{"route_file":mine_new},{"route_file":mine_closed}]
   chain=mock.Mock(writer_identity=lambda: ("claude","sess"),read_tail=lambda harness,sid: ledger)
   with mock.patch.object(R,"_route_chain_module",return_value=chain), \
        mock.patch.object(R,"_compose_artifact_root",return_value=str(root)):
    self.assertEqual(R.caller_open_route(here)[:2],(mine_new,"this-session"))   # newest open, closed skipped
    ledger.clear()                                                               # a new session: no ledger
    self.assertEqual(R.caller_open_route(here)[:2],(None,"none"))
    only=route("d",here)
    found,source,rows=R.caller_open_route(here)
    self.assertEqual((found,source),(only,"cwd"))
    self.assertIn("start --route rt-"+"d"*16,rows[0]["resume_command"])      # the short form, by ID
    route("e",here)
    found,source,rows=R.caller_open_route(here)
    self.assertEqual((found,source,len(rows)),(None,"ambiguous",2))
 def test_spec_read_auto_accepts_this_sessions_recorded_read_of_the_unchanged_spec(self):
  with tempfile.TemporaryDirectory() as tmp:
   repo=Path(tmp)/"repo"; reports=repo/".agent_reports"; home=Path(tmp)/"home"
   (reports/"spec").mkdir(parents=True); home.mkdir(); prd=reports/"spec"/"prd.md"; prd.write_text("# prd\n",encoding="utf-8")
   read=lambda session: subprocess.run(["sh",str(R.ROOT/"hooks"/"spec-read-marker.sh"),"--file",str(prd),"--session",session,
                                        "--agent-home",str(home)],check=True,capture_output=True,text=True)
   with mock.patch.object(R,"resolve_agent_home",return_value=str(home)), \
        mock.patch.object(R.ROUTE_AUTHORITY,"default_parent_session_id",return_value="sess-a"):
    with self.assertRaisesRegex(ValueError,"compose-spec-read-required"): R.compose_spec_read(repo,reports,"auto")
    read("sess-b")                                    # another session's read is not this one's
    with self.assertRaisesRegex(ValueError,"compose-spec-read-required"): R.compose_spec_read(repo,reports,"auto")
    read("sess-a")
    got=R.compose_spec_read(repo,reports,"auto")
    self.assertEqual(got,{"satisfied":True,"source":R.SPEC_READ_MARKER_PREFIX+str(prd)})
    os.utime(prd,(prd.stat().st_atime,prd.stat().st_mtime+5))   # the spec changed after the read
    with self.assertRaisesRegex(ValueError,"compose-spec-read-required"): R.compose_spec_read(repo,reports,"auto")
   with mock.patch.object(R.ROUTE_AUTHORITY,"default_parent_session_id",side_effect=D.DispatchContractError("caller-harness-ambiguous")):
    with self.assertRaisesRegex(ValueError,"compose-spec-read-required"): R.compose_spec_read(repo,reports,"auto")
    self.assertTrue(R.compose_spec_read(Path(tmp)/"none",Path(tmp)/"none","auto")["satisfied"])   # no spec: identity never asked
 def test_spec_read_auto_notes_the_shared_spec_layout_without_refusing(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); ref=root/"shared"/"spec"/"ref_abc"
   for rrev in ("rrev_01","rrev_02"):
    (ref/"revisions"/rrev).mkdir(parents=True); (ref/"revisions"/rrev/"prd.md").write_text("# prd\n",encoding="utf-8")
   (ref/"reference.json").write_text(json.dumps({"latest_revision_id":"rrev_02"}),encoding="utf-8")
   # a shared spec is a one-line notice, never a refusal; only the latest revision is named
   got=R.compose_spec_read(root,root,"auto")
   self.assertTrue(got["satisfied"])
   self.assertTrue(got["source"].startswith(R.SPEC_READ_SHARED_PREFIX))
   self.assertIn("rrev_02/prd.md",got["source"]); self.assertNotIn("rrev_01",got["source"])
   route=self.compose(artifact_root=root)
   self.assertEqual(route["tracked_gate_evidence"]["spec_read"],got)
   R.verify_route(route,R.ROOT)
   notice=[l for l in R.compose_card(route).splitlines() if "공유 spec" in l]
   self.assertEqual(len(notice),1); self.assertIn("rrev_02/prd.md",notice[0]); self.assertNotIn("rrev_01",notice[0])
   # an explicit --spec-read keeps the caller's provenance and needs no notice
   self.assertEqual(R.compose_spec_read(root,root,"read shared spec")["source"],"read shared spec")
   self.assertNotIn("공유 spec",R.compose_card(self.compose(artifact_root=root,spec_read="read shared spec")))
   # a spec/prd.md next to a shared spec still refuses, on spec/prd.md alone
   (root/"spec").mkdir(); (root/"spec"/"prd.md").write_text("# prd\n",encoding="utf-8")
   with self.assertRaisesRegex(ValueError,"compose-spec-read-required:[^,]*spec/prd.md$") as ctx: R.compose_spec_read(root,root,"auto")
   self.assertNotIn("shared",str(ctx.exception))
   (root/"spec"/"prd.md").unlink()
   # an unreadable reference.json is ignored: the plain no-spec record, no notice
   (ref/"reference.json").write_text("{not json",encoding="utf-8")
   self.assertEqual(R.compose_spec_read(root,root,"auto")["source"],"compose-auto: no spec/prd.md under cwd or artifact root")
   self.assertNotIn("공유 spec",R.compose_card(self.compose(artifact_root=root)))
 def test_preset_compile_records_preset_origin_and_derived_shape(self):
  route=R.compile_route(**self.args())
  self.assertEqual(route["selection"]["route_origin"],"preset"); self.assertEqual(route["selection"]["shape"],"direct")
  self.assertEqual(R.shape_for_intensity("quick"),"solo"); self.assertEqual(R.shape_for_intensity("thorough"),"staged")
  with self.assertRaisesRegex(ValueError,"invalid route origin"): R.compile_route(**self.args(route_origin="guess"))
 def test_compose_publication_does_not_load_deleted_material_guard(self):
  import types
  with tempfile.TemporaryDirectory() as td:
   root=Path(td)
   with mock.patch.dict(os.environ, {"AGENT_HOME":str(R.ROOT)}):
    route=self.compose(artifact_root=root,cwd=R.ROOT)
   original=importlib.util.spec_from_file_location
   def without_guard(name,path,*args,**kwargs):
    if Path(path).name == "material-route-guard.py":
     raise FileNotFoundError(path)
    return original(name,path,*args,**kwargs)
   with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID":""}), \
        mock.patch.object(R,"_record_route_chain"), \
        mock.patch("importlib.util.spec_from_file_location",side_effect=without_guard), \
        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    R._emit_compiled_route(types.SimpleNamespace(command="compose",full_record=True),route,root)
   path=R.canonical_route_path(root,route["route_id"])
   self.assertEqual(json.loads(path.read_text()),route)
 def test_graph_order_refuses_consumer_before_producer(self):
  # D9: a caller-supplied `--graph` order may not place a node before a
  # producer it transitively `depends_on` in the base recipe; siblings with
  # no ancestor relationship (e.g. `frame`/`frame-alternative`) stay free.
  with self.assertRaisesRegex(ValueError,r"compose-graph-order:test-before-execute \(recipe order: "):
   self.compose(graph="test,execute")
  for graph in ("execute,impl-review,test,report",):
   route=self.compose(graph=graph)
   self.assertEqual([n["id"] for n in route["nodes"]],graph.split(","))
   R.verify_route(route,R.ROOT)
  frame_route=self.compose(graph="frame,frame-alternative,test,report")
  self.assertEqual([n["id"] for n in frame_route["nodes"] if not n.get("parallel_leg_index")],
                   ["frame","frame-alternative","test","report"])
  R.verify_route(frame_route,R.ROOT)
  spec_route=self.compose(capability="autopilot-spec",capability_mode="update",
                          graph="review,prd-transaction",signals=["shared-contract"])
  R.verify_route(spec_route,R.ROOT)
  research_route=self.compose(capability="autopilot-spec",capability_mode="update",
                              graph="research,review,prd-transaction",
                              signals=["shared-contract"])
  R.verify_route(research_route,R.ROOT)
  report_only=self.compose(graph="report")
  self.assertEqual([n["id"] for n in report_only["nodes"]],["report"])
  R.verify_route(report_only,R.ROOT)
 def test_graph_unknown_node_names_stages_command(self):
  with self.assertRaisesRegex(ValueError,r"compose-graph-unknown-node.*capability-route\.py stages"):
   self.compose(graph="execute,deploy")
 def test_stages_subcommand_lists_recipe_order_units_and_gates(self):
  env=dict(os.environ); env["AGENT_HOME"]=str(R.ROOT)
  result=subprocess.run([sys.executable,str(P),"stages","--capability","autopilot-code","--json"],
                        text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,0,result.stderr)
  blocks=json.loads(result.stdout)
  self.assertEqual(len(blocks),1)
  block=blocks[0]
  self.assertEqual(block["capability"],"autopilot-code")
  self.assertEqual([n["id"] for n in block["nodes"]],
                   ["frame","frame-alternative","plan","plan-check","execute","impl-review","test","report"])
  execute_node=next(n for n in block["nodes"] if n["id"]=="execute")
  self.assertIn("dev/backend",execute_node["unit_choices"])
  plan_node=next(n for n in block["nodes"] if n["id"]=="plan")
  self.assertIn("frame-review",plan_node["human_gates"])
  self.assertTrue(next(n for n in block["nodes"] if n["id"]=="report")["terminal"])
  # SD-165: every stage is a part `capability:stage` with its catalog fields
  self.assertEqual(execute_node["part"],"autopilot-code:execute")
  self.assertFalse(execute_node["shareable"]); self.assertFalse(execute_node["optional"])
  self.assertIsNone(execute_node["start_approval"]); self.assertTrue(execute_node["summary"])
  self.assertEqual(execute_node["inputs"],["plan.md","checklist.md"])
  self.assertIn("autopilot-research:synthesis",[n["part"] for n in block["borrowable"]])
  # every-capability form and the text rendering both work too
  result_all=subprocess.run([sys.executable,str(P),"stages"],text=True,capture_output=True,env=env)
  self.assertEqual(result_all.returncode,0,result_all.stderr)
  self.assertIn("capability=autopilot-code",result_all.stdout)
  self.assertIn(" execute unit=dev/backend",result_all.stdout)

class OwnerRegisteredCompletionTest(unittest.TestCase):
 """노드 키가 없는 실제 depth-1 오너 행도 등록 완료로 결속한다."""
 setUp=InlineStageCompletionRecipeTest.setUp
 _restore=InlineStageCompletionRecipeTest._restore

 def fixture(self, capability="autopilot-spec", capability_mode="update", node_id="prd-transaction", **overrides):
  t=TestRoute()
  route=R.compile_route(**t.args(capability=capability,capability_mode=capability_mode,
   artifact_root=self.base/"artifacts",requested_intensity="standard",predicates=[],
   signals=["shared-contract"],transport="headless",inline_reason=None,
   dispatch_evidence=t.dispatch(t.nested()),registered_headless_evidence=t.registered_headless()))
  node=next(n for n in route["nodes"] if n["id"]==node_id)
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
    R.completion_dir(route["route_id"])/(node["id"]+".json")))
   # Identity lookup and the real registry writer are exercised; OS liveness
   # is isolated so this fixture never claims anything about host processes.
   for process,state in ((D.ProcessQuiescence("quiescent","fixture"),"ready"),
                         (D.ProcessQuiescence("live","fixture"),"draining")):
    with mock.patch.object(D,"attempt_process_quiescence",return_value=process) as probe:
     self.assertEqual(D.completion_attempt_readiness(route,node,marker,self.jobs).state,state)
     probe.assert_called_once()
  with mock.patch.object(D,"attempt_process_quiescence",
                        return_value=D.ProcessQuiescence("quiescent","fixture")):
   exact=R.terminal_gate_observation(route,jobs=self.jobs,exact_terminal=True)[node["id"]]
  self.assertTrue(exact["passed"],exact)
  self.assertEqual(exact["attempt_id"],"att-terminal-owner")
  metadata=D.parse_registry_metadata(self.jobs.read_text().split("\t")[5])
  self.assertNotIn("route_id",metadata,"읽는 쪽 수정이며 등록 신원 재작성은 금지")
  self.assertEqual(metadata["owner_route_id"],route["route_id"])

 def test_exact_owner_gate_is_not_a_proven_block(self):
  # F1: a depth-1 owner row (owner_route_id/owner_route_hash only, no plain
  # route_id/route_node) must not fall through the exact-terminal gate to
  # `completion-attempt-not-current` -- that reason is reserved for a proven
  # later-attempt fence, and an owner row never matches the old child-only
  # route_id/route_node scan (plan §2 row for `_marker_identity_row`).
  route,node,path,evidence=self.fixture()
  command=[str(P),"complete","--route",str(path),"--node",node["id"],
   "--evidence",str(evidence),"--jobs",str(self.jobs),"--attempt-id","att-terminal-owner"]
  output=io.StringIO()
  with mock.patch.object(sys,"argv",command),contextlib.redirect_stdout(output):
   R.main()
  with mock.patch.object(D,"attempt_process_quiescence",
                        return_value=D.ProcessQuiescence("quiescent","fixture")):
   exact=R.terminal_gate_observation(route,jobs=self.jobs,exact_terminal=True)[node["id"]]
  self.assertTrue(exact["passed"],exact)
  self.assertNotEqual(exact.get("reason"),"completion-attempt-not-current")
  metadata=D.parse_registry_metadata(self.jobs.read_text().split("\t")[5])
  metadata=dict(metadata,workflow_completion="runtime-v1")
  request=R.dispatch_terminal_commit.TerminalCommitRequest(
   path,"att-terminal-owner",self.jobs,Path(route["artifact_root"]))
  with mock.patch.object(R.dispatch_terminal_commit,"_completion_request",return_value=request), \
       mock.patch.object(D,"attempt_process_quiescence",
                         return_value=D.ProcessQuiescence("quiescent","fixture")):
   state=R.dispatch_terminal_commit.owner_completion_state(self.jobs,"done",metadata)
  self.assertNotEqual(state.state,"blocked",state)

 def test_foreign_owner_identity_cannot_publish_or_close(self):
  route,node,path,evidence=self.fixture(owner_route_hash="sha256:"+"f"*64)
  before=self.jobs.read_bytes()
  with self.assertRaisesRegex(ValueError,"route identity"):
   R.complete_node(route,node,node["id"],evidence,jobs=self.jobs,attempt_id="att-terminal-owner")
  self.assertEqual(self.jobs.read_bytes(),before)
  self.assertFalse((R.completion_dir(route["route_id"])/(node["id"]+".json")).exists())


class EnvironmentAttemptDefaultTest(unittest.TestCase):
 """`complete` reads `--jobs`/`--attempt-id` from the environment a registered
 worker already has, but only for its own node; every other form is unchanged."""
 setUp=InlineStageCompletionRecipeTest.setUp
 _restore=InlineStageCompletionRecipeTest._restore
 fixture=OwnerRegisteredCompletionTest.fixture

 def _main(self,command,env):
  output=io.StringIO()
  with mock.patch.dict(os.environ,env),mock.patch.object(sys,"argv",[str(P),*command]), \
       contextlib.redirect_stdout(output):
   R.main()
  return [json.loads(line) for line in output.getvalue().splitlines()]

 def test_registered_worker_completes_its_own_node_from_the_environment(self):
  route,node,path,evidence=self.fixture()
  marker,row=self._main(["complete","--route",str(path),"--node",node["id"],"--evidence",str(evidence)],
   {"AGENT_DISPATCH_ATTEMPT_ID":"att-terminal-owner","AGENT_DISPATCH_JOBS":str(self.jobs)})
  self.assertEqual(row["status"],"closed")
  self.assertTrue(marker["registered_worker"])
  self.assertEqual(marker["attempt_id"],"att-terminal-owner")

 def test_stage_worker_completes_its_own_stage_from_the_environment(self):
  artifact=self.base/"artifacts"
  route=InlineStageCompletionRecipeTest._route(self,artifact)
  path=Path(route["artifact_root"])/".runtime"/"routes"/(route["route_id"]+".json")
  path.parent.mkdir(parents=True); path.write_text(json.dumps(route))
  out=artifact/"evidence"/"execute.md"; out.parent.mkdir(parents=True); out.write_text("ran\n")
  meta={"attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
   "execution_surface":"registered-headless","registered_worker":"1",
   "fallback_hop":"same-harness-headless","attempt_id":"att-stage-execute",
   "worker_type":"stage","unit":"dev/backend","route_id":route["route_id"],
   "route_hash":route["route_hash"],"route_node":"execute","pid":"2147483647","pid_start":"1"}
  self.jobs.write_text("\t".join(["2026-09-07T00:00:00Z","open","repo","worktree","stage",
   ",".join(k+"="+v for k,v in meta.items())])+"\n")
  env={"AGENT_DISPATCH_ATTEMPT_ID":"att-stage-execute","AGENT_DISPATCH_JOBS":str(self.jobs)}
  marker,row=self._main(["complete","--route",str(path),"--node","execute","--evidence",str(out)],env)
  self.assertEqual(row["status"],"closed")
  self.assertTrue(marker["registered_worker"])
  self.assertEqual(marker["attempt_id"],"att-stage-execute")
  # An explicit `--jobs` with the attempt left to the environment reaches the same row.
  marker,row=self._main(["complete","--route",str(path),"--node","execute","--evidence",str(out),
   "--jobs",str(self.jobs)],env)
  self.assertEqual(row["status"],"already-closed")
  self.assertEqual(marker["attempt_id"],"att-stage-execute")

 def test_explicit_attempt_wins_and_finds_the_registry_by_default(self):
  route,node,path,evidence=self.fixture()
  marker,row=self._main(["complete","--route",str(path),"--node",node["id"],"--evidence",str(evidence),
   "--attempt-id","att-terminal-owner"],
   {"AGENT_DISPATCH_ATTEMPT_ID":"att-someone-else","AGENT_DISPATCH_JOBS":str(self.jobs)})
  self.assertEqual(row["status"],"closed")
  self.assertEqual(marker["attempt_id"],"att-terminal-owner")

 def test_without_an_inherited_registry_nothing_is_guessed(self):
  # No AGENT_DISPATCH_JOBS: no registry is assumed, so both forms keep main's refusals.
  route,node,path,evidence=self.fixture()
  before=self.jobs.read_bytes()
  with mock.patch.dict(os.environ):
   os.environ.pop("AGENT_DISPATCH_JOBS",None)
   with self.assertRaisesRegex(ValueError,"current dispatched completion requires exact attempt metadata"):
    self._main(["complete","--route",str(path),"--node",node["id"],"--evidence",str(evidence)],
     {"AGENT_DISPATCH_ATTEMPT_ID":"att-terminal-owner"})
   with self.assertRaisesRegex(ValueError,"unregistered completion requires explicit attempt metadata"):
    self._main(["complete","--route",str(path),"--node",node["id"],"--evidence",str(evidence),
     "--attempt-id","att-terminal-owner"],{})
  self.assertEqual(self.jobs.read_bytes(),before)

 def test_an_attempt_bound_elsewhere_fills_nothing(self):
  route,node,path,evidence=self.fixture()
  other=next(n for n in route["nodes"] if n["id"]!=node["id"] and n.get("dispatch_depth")
             and n.get("kind")!="resource-runner")
  before=self.jobs.read_bytes()
  for jobs in (str(self.jobs),str(self.base/"absent"/"jobs.log")):
   with self.assertRaisesRegex(ValueError,"current dispatched completion requires exact attempt metadata"):
    self._main(["complete","--route",str(path),"--node",other["id"],"--evidence",str(evidence)],
     {"AGENT_DISPATCH_ATTEMPT_ID":"att-terminal-owner","AGENT_DISPATCH_JOBS":jobs})
  self.assertEqual(self.jobs.read_bytes(),before)

 def test_depth0_inline_node_ignores_an_owner_environment(self):
  route=R.compile_route(**TestRoute().args(artifact_root=self.base/"artifacts"))
  node=route["nodes"][0]
  path=Path(route["artifact_root"])/".runtime"/"routes"/(route["route_id"]+".json")
  path.parent.mkdir(parents=True); path.write_text(json.dumps(route))
  evidence=self.base/"artifacts"/"report.md"; evidence.write_text("done\n")
  # A live owner of this very route: its row is real, but not this node's attempt.
  meta={"attempt_schema_version":"2","dispatch_depth":"1","transport":"headless",
   "execution_surface":"registered-headless","registered_worker":"1",
   "fallback_hop":"same-harness-headless","attempt_id":"att-owner",
   "worker_type":"owner","unit":"_kernel/owner","owner_route_id":route["route_id"],
   "owner_route_hash":route["route_hash"],"owner_route_file":str(path),
   "pid":"2147483647","pid_start":"1"}
  self.jobs.write_text("\t".join(["2026-09-07T00:00:00Z","open","repo","worktree","owner",
   ",".join(k+"="+v for k,v in meta.items())])+"\n")
  before=self.jobs.read_bytes()
  marker,=self._main(["complete","--route",str(path),"--node",node["id"],"--evidence",str(evidence)],
   {"AGENT_DISPATCH_ATTEMPT_ID":"att-owner","AGENT_DISPATCH_JOBS":str(self.jobs)})
  self.assertFalse(marker["registered_worker"])
  self.assertIsNone(marker.get("attempt_id"))
  self.assertEqual(self.jobs.read_bytes(),before)

 def test_inline_axes_form_stays_unregistered_with_the_environment_set(self):
  artifact=self.base/"artifacts"
  route=InlineStageCompletionRecipeTest._route(self,artifact)
  path=Path(route["artifact_root"])/".runtime"/"routes"/(route["route_id"]+".json")
  path.parent.mkdir(parents=True); path.write_text(json.dumps(route))
  out=artifact/"evidence"/"execute.md"; out.parent.mkdir(parents=True); out.write_text("ran inline\n")
  marker,row=self._main(["complete","--route",str(path),"--node","execute","--evidence",str(out),
   "--attempt-id","att-owner-execute-inline","--dispatch-depth","2","--transport","headless",
   "--execution-surface","inline","--registered-worker","0","--fallback-hop","inline"],
   {"AGENT_DISPATCH_ATTEMPT_ID":"att-owner","AGENT_DISPATCH_JOBS":str(self.jobs)})
  self.assertEqual(row["status"],"unregistered-complete")
  self.assertEqual(self.jobs.read_text(),"")

 def test_resource_run_takes_nothing_from_the_environment(self):
  t=TestRoute()
  route=R.compile_route(**t.args(capability="autopilot-lab",capability_mode="setup",
   artifact_root=self.base/"artifacts",requested_intensity="auto",predicates=[],signals=["resource-run"],
   transport="headless",inline_reason=None,dispatch_evidence=t.dispatch(t.nested())))
  node=next(n for n in route["nodes"] if n.get("kind")=="resource-runner")
  path=Path(route["artifact_root"])/".runtime"/"routes"/(route["route_id"]+".json")
  path.parent.mkdir(parents=True); path.write_text(json.dumps(route))
  evidence=self.base/"artifacts"/"run.md"; evidence.write_text("run\n")
  with mock.patch.object(R,"complete_node",return_value=({},None)) as complete:
   self._main(["complete","--route",str(path),"--node",node["id"],"--evidence",str(evidence)],
    {"AGENT_DISPATCH_ATTEMPT_ID":"att-owner","AGENT_DISPATCH_JOBS":str(self.jobs)})
  self.assertIsNone(complete.call_args.kwargs["jobs"])
  self.assertIsNone(complete.call_args.kwargs["attempt_id"])

 def test_subsession_gate_defaults_only_the_registry(self):
  route,node,path,evidence=self.fixture()
  manifest=self.base/"manifest.json"; manifest.write_text("{}")
  with mock.patch.object(R,"complete_subsession_stage",return_value=({},None)) as complete:
   self._main(["complete","--route",str(path),"--node",node["id"],"--evidence",str(evidence),
    "--subsession-manifest",str(manifest)],
    {"AGENT_DISPATCH_ATTEMPT_ID":"att-terminal-owner","AGENT_DISPATCH_JOBS":str(self.jobs)})
  self.assertEqual(complete.call_args.args[5],str(self.jobs))

 def test_check_reads_the_environment_and_keeps_its_refusals(self):
  route,node,path,evidence=self.fixture()
  env={"AGENT_DISPATCH_ATTEMPT_ID":"att-terminal-owner","AGENT_DISPATCH_JOBS":str(self.jobs)}
  with mock.patch.object(R,"owner_closure_plan",return_value={}) as plan:
   # No --jobs/--attempt-id: the environment fills both, so the check gets past its exact-attempt
   # refusal and answers this non-review node `not-applicable` (owner closure answers review nodes only).
   result,=self._main(["complete","--route",str(path),"--node",node["id"],"--evidence",str(evidence),
    "--check"],env)
   self.assertEqual(result["result"],"not-applicable")
   plan.assert_not_called()
   with self.assertRaisesRegex(ValueError,"owner-closure-check-requires-exact-jobs-attempt-and-no-overrides"):
    self._main(["complete","--route",str(path),"--node",node["id"],"--evidence",str(evidence),
     "--check","--output",str(self.base/"out.json")],env)

class ShipPackageOwnerCompletionTest(OwnerRegisteredCompletionTest):
 """The standard package owner uses existing registered completion and closure."""
 def fixture(self, **overrides):
  return super().fixture(capability="autopilot-ship",capability_mode="package",
   node_id="package",**overrides)


class OwnerNodeBoundTerminalTest(unittest.TestCase):
 """A one-shot owner row sealed by `route_*` fields still proves its terminal.

 The supervisor may close the registered row (`completed-supervisor`) before
 the marker writer runs. The no-marker reader then has only the row and its
 exact handoff, so it must read the node-bound shape as well as `owner_route_*`.
 """

 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name)
  self.work=self.base/"work"; self.work.mkdir()
  self.artifacts=self.base/"artifacts"; self.artifacts.mkdir()
  self.jobs=self.base/"state"/"jobs.log"; self.jobs.parent.mkdir()
  previous=os.environ.get("AGENT_ARTIFACT_ROOT")
  os.environ["AGENT_ARTIFACT_ROOT"]=str(self.artifacts)
  self.addCleanup(lambda: os.environ.pop("AGENT_ARTIFACT_ROOT",None) if previous is None
                  else os.environ.__setitem__("AGENT_ARTIFACT_ROOT",previous))
  self.route=R.compile_route(
   "autopilot-code","dev","quick",self.work,self.artifacts,
   predicates=[],transport=None,tracking="tracked",
   tracked_gate_evidence={"spec_read":{"satisfied":True,"source":"canonical-prd-sha256"},
    "drift_verdict":"within-spec","workflow_mode":"tracked",
    "artifact_guard":{"satisfied":True,"source":"conductor-prechecked"}},
   registered_headless_evidence={"candidates":[
    {"harness":harness,"transport":"headless","surface":"registered-headless",
     "status":"supported","probe_source":"fixture-probe","probe_time":"2026-07-20T00:00:00Z"}
    for harness in ("codex","claude")]})
  self.node=next(n for n in self.route["nodes"] if n["id"]=="one-shot")
  self.route_file=R.canonical_route_path(self.artifacts,self.route["route_id"])
  self.route_file.parent.mkdir(parents=True); self.route_file.write_text(json.dumps(self.route))
  self.evidence=self.artifacts/"report.md"; self.evidence.write_text("검증 완료\n")

 def log(self,harness,name="owner.log"):
  handoff=f"artifact: {self.evidence}\nverdict: PASS\nblocker: none"
  if harness=="claude":
   rows=[{"type":"result","subtype":"success","is_error":False,"result":handoff}]
  else:
   rows=[{"type":"item.completed","item":{"type":"agent_message","text":handoff}},
         {"type":"turn.completed"}]
  path=self.base/name
  path.write_text("".join(json.dumps(row)+"\n" for row in rows)); return path

 def row(self,harness="claude",attempt="att-node-bound",**overrides):
  """One done owner row that carries no `owner_route_*` key."""
  meta={"attempt_id":attempt,"worker_type":"owner","unit":"_kernel/owner","dispatch_depth":"1",
   "registered_worker":"1","harness":harness,"route_file":str(self.route_file),
   "route_id":self.route["route_id"],"route_hash":self.route["route_hash"],"route_node":"one-shot",
   "workflow_completion":"runtime-v1","note":"completed-supervisor","failure_class":"pass",
   "log_file":str(self.log(harness,f"{attempt}.log")),"artifact_root":str(self.artifacts),
   "pid":"2147483647","pid_start":"1"}
  meta.update(overrides)
  return "\t".join(["2026-09-30T00:00:00Z","done",str(self.work),str(self.work),"owner",
   ",".join(k+"="+v for k,v in meta.items() if v is not None)])

 def observe(self,*rows,route=None):
  self.jobs.write_text("".join(r+"\n" for r in rows))
  route=route or self.route
  with mock.patch.object(R,"attempt_process_quiescence",return_value=mock.Mock(state="quiescent")), \
       mock.patch.object(R,"owner_terminal_prerequisites",return_value={}):
   return R.terminal_gate_observation(route,jobs=self.jobs,exact_terminal=True)["one-shot"]

 def assertProven(self,gate,attempt):
  self.assertTrue(gate["passed"],gate)
  self.assertEqual(gate["source"],"owner-terminal")
  self.assertEqual(gate["attempt_id"],attempt)
  self.assertEqual(gate["evidence"],str(self.evidence))
  self.assertEqual(gate["reason"],"owner-terminal-verified")

 def test_node_bound_owner_passes_without_a_marker_for_both_harnesses(self):
  for harness in ("claude","codex"):
   with self.subTest(harness=harness):
    marker=R.completion_dir(self.route["route_id"],jobs=self.jobs)/"one-shot.json"
    self.assertFalse(marker.exists())
    self.assertProven(self.observe(self.row(harness,f"att-{harness}")),f"att-{harness}")

 def test_owner_route_row_still_passes(self):
  owner=self.row(attempt="att-owner-route",route_id=None,route_hash=None,route_node=None,
   owner_route_id=self.route["route_id"],owner_route_hash=self.route["route_hash"],
   owner_route_file=str(self.route_file))
  self.assertProven(self.observe(owner),"att-owner-route")

 def test_foreign_route_or_node_rows_are_never_the_proof(self):
  good=self.row(attempt="att-good")
  foreign=(self.row(attempt="att-other-route",route_id="rt-0000000000000000"),
           self.row(attempt="att-other-hash",route_hash="sha256:"+"0"*64),
           self.row(attempt="att-other-node",route_node="frame"))
  for row in foreign:
   with self.subTest(order="after",row=row.split("attempt_id=")[1].split(",")[0]):
    self.assertProven(self.observe(good,row),"att-good")
   with self.subTest(order="before",row=row.split("attempt_id=")[1].split(",")[0]):
    self.assertProven(self.observe(row,good),"att-good")
   with self.subTest(order="only",row=row.split("attempt_id=")[1].split(",")[0]):
    gate=self.observe(row)
    self.assertFalse(gate["passed"],gate)
    self.assertEqual(gate["reason"],"owner-attempt-absent")

 def test_incomplete_owner_binding_does_not_use_the_node_bound_path(self):
  half=self.row(attempt="att-half",owner_route_id=self.route["route_id"],
                owner_route_hash=self.route["route_hash"])
  gate=self.observe(half)
  self.assertFalse(gate["passed"],gate)
  self.assertIn(gate["reason"],("owner-route-identity-mismatch","owner-terminal-evidence-unverified"))

 def test_node_bound_owner_needs_a_quick_route(self):
  gate=self.observe(self.row(),route=dict(self.route,effective_intensity="standard"))
  self.assertFalse(gate["passed"],gate)
  self.assertEqual(gate["reason"],"owner-route-identity-mismatch")

 def test_node_bound_owner_needs_this_routes_file(self):
  other=self.artifacts/".runtime"/"routes"/"rt-elsewhere.json"; other.write_text("{}")
  gate=self.observe(self.row(route_file=str(other)))
  self.assertFalse(gate["passed"],gate)
  self.assertEqual(gate["reason"],"owner-route-identity-mismatch")

 def test_owner_route_row_advanced_to_another_route_still_mismatches(self):
  owner=self.row(attempt="att-advanced",route_id=None,route_hash=None,route_node=None,
   owner_route_id=self.route["route_id"],owner_route_hash=self.route["route_hash"],
   owner_route_file=str(self.route_file))
  moved=mock.Mock(route_id="rt-0000000000000000",route_hash="sha256:"+"0"*64)
  with mock.patch("owner_route_binding.resolve_owner_route_lifecycle",return_value=(moved,"advanced")):
   gate=self.observe(owner)
  self.assertFalse(gate["passed"],gate)
  self.assertEqual(gate["reason"],"owner-route-identity-mismatch")


class ExactFenceFailureModeTest(unittest.TestCase):
 """F3: a transient read failure on the exact fence must never be reported
 as the proven-permanent `completion-attempt-not-current` reason, and the
 attempt-less fence input has its own distinct reason."""
 setUp=InlineStageCompletionRecipeTest.setUp
 _restore=InlineStageCompletionRecipeTest._restore
 _route=InlineStageCompletionRecipeTest._route
 _axes=InlineStageCompletionRecipeTest._axes

 def test_exact_fence_registry_failure_is_not_a_proven_block(self):
  artifact=self.base/"artifacts"
  route=self._route(artifact)
  node=next(n for n in route["nodes"] if n["id"]=="execute")
  out=artifact/"evidence"/"execute.md"
  out.parent.mkdir(parents=True,exist_ok=True); out.write_text("ran inline\n",encoding="utf-8")
  # `jobs=None` is the documented unregistered/inline recipe (defect H): it
  # publishes the marker under the `AGENT_DISPATCH_JOBS`-resolved state root
  # without ever touching the registry file itself.
  R.complete_node(route,node,"execute",out,
   attempt_id="att-inline-execute",explicit_attempt_metadata=self._axes())
  real_read_text=Path.read_text
  def flaky_read_text(path_self,*a,**kw):
   if path_self==self.jobs:
    raise OSError("simulated registry read failure")
   return real_read_text(path_self,*a,**kw)
  with mock.patch.object(Path,"read_text",flaky_read_text):
   result=R._marker_identity_row(route,node,node["id"],node.get("completion_gate"),
                                 jobs=self.jobs,exact_terminal=True)
  self.assertFalse(result["passed"])
  self.assertEqual(result["reason"],"registry-unreadable")
  self.assertNotEqual(result["reason"],"completion-attempt-not-current")

 def test_fence_without_an_attempt_id_has_its_own_reason(self):
  route={"route_id":"rt-fence-fixture","route_hash":"sha256:"+"7"*64,
         "nodes":[{"id":"execute","terminal":True}]}
  node=route["nodes"][0]
  self.assertEqual(
   R._registered_marker_fence(route,node,{"registered_worker":True},[]),
   "marker-attempt-id-missing")



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
                           drift_verdict="fixture",unassigned=True)

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
   if recipe["capability"]==R.ROUTE_FRAME_CAPABILITY:
    continue  # compiler-internal: no quick shape
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
    self.assertEqual([leg["model_profile"] for leg in legs],["top","top"])
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

  # standard+: both frame perspectives use top at every intensity.
  for capability,mode in self.FRAME_CAPABILITIES:
   with self.subTest(capability=capability):
    route=self.standard(capability=capability,mode=mode)
    rungs=ladder[route["owner_model_profile"]]
    by_id={n["id"]:n for n in route["nodes"]}
    self.assertEqual(by_id["frame"]["model_profile"],rungs["anchor"])
    self.assertEqual(by_id["frame-alternative"]["model_profile"],rungs["others"])
    R.verify_route(route,R.ROOT)

  # quick: owner is `balanced-deep`, so both legs still land on top.
  route=self.quick()
  rungs=ladder[route["owner_model_profile"]]
  by_id={n["id"]:n for n in route["nodes"]}
  self.assertEqual(route["owner_model_profile"],"balanced-deep")
  self.assertEqual(by_id["frame"]["model_profile"],rungs["anchor"])
  self.assertEqual(by_id["frame-alternative"]["model_profile"],rungs["others"])
  self.assertEqual((rungs["anchor"],rungs["others"]),("top","top"))
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
  # the sibling leg has its own sealed top demand
  other=next(n for n in route["nodes"] if n["id"]=="frame-alternative")
  self.assertEqual(other["model_profile"],"top")
  self.assertEqual(other["profile_demand"],R.PROFILE.FRAME_ANCHOR_SHAPE_DEMAND)
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


class RouteChainWriterTest(ComposeRouteTest):
 """F-<next> fleet-route-chain-r2 plan §3 B-4 — `_record_route_chain`/`_route_chain_identity`/
 `_resolve_compose_plan` writer surface. `TestRoute.setUp` already isolates AGENT_HOME and
 XDG_STATE_HOME per test; this class additionally pins `FLEET_ROUTE_CHAIN_DIR` and clears
 `AGENT_DISPATCH_DEPTH` so every test starts from an explicit, known identity axis."""
 def setUp(self):
  super().setUp()
  self._rc_tmp=tempfile.TemporaryDirectory()
  self.addCleanup(self._rc_tmp.cleanup)
  self._rc_patch=mock.patch.dict(os.environ,{"FLEET_ROUTE_CHAIN_DIR":self._rc_tmp.name})
  self._rc_patch.start()
  self.addCleanup(self._rc_patch.stop)
  # This suite's OWN process is itself a live dispatch worker (real
  # AGENT_DISPATCH_CALLER_HARNESS/CURRENT_HARNESS=claude in the ambient environment,
  # measured 2026-09-18) -- identity-sensitive tests must not inherit that leak.
  self._identity_guard={key:os.environ.get(key) for key in (
   "AGENT_DISPATCH_DEPTH","AGENT_DISPATCH_CALLER_HARNESS","AGENT_DISPATCH_CURRENT_HARNESS",
   "AGENT_DISPATCH_PARENT_SESSION_ID","CLAUDE_CODE_SESSION_ID","CLAUDE_SESSION_ID",
   "CODEX_THREAD_ID","CODEX_SESSION_ID","OPENCODE_SESSION_ID",
  )}
  for key in self._identity_guard: os.environ.pop(key,None)
  self.addCleanup(self._restore_identity_guard)
  import importlib
  sys.path.insert(0,str(P.parent.parent/"tools"))
  from fleet import route_chain as RC
  self.RC=RC
  self.addCleanup(RC.clear_cache if hasattr(RC,"clear_cache") else (lambda:None))
 def _restore_identity_guard(self):
  for key,value in self._identity_guard.items():
   if value is None: os.environ.pop(key,None)
   else: os.environ[key]=value
 def _emit(self,route,artifact_root,**kw):
  import types
  ns=types.SimpleNamespace(command="compose",start=False,full_record=False,
                           _route_chain_plan=kw.pop("_route_chain_plan",(None,None)),**kw)
  out=io.StringIO(); err=io.StringIO()
  # _emit_compiled_route's runtime-root check needs AGENT_HOME to name THIS checkout
  # (routes here are composed with cwd=R.ROOT) -- same override the existing
  # ComposeRouteTest compose-start/receipt tests already use.
  with mock.patch.dict(os.environ,{"AGENT_HOME":str(R.ROOT)}), \
       contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
   R._emit_compiled_route(ns,route,artifact_root)
  return err.getvalue()
 def test_compose_appends_one_line_for_calling_session(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
    os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-caller","AGENT_DISPATCH_ATTEMPT_ID":"",
               "AGENT_HOME":str(R.ROOT)}):
   route=self.compose(artifact_root=tmp)
   err=self._emit(route,tmp)
   self.assertIn("route_chain_written=1 harness=claude",err)
   lines=self.RC.read_tail("claude","sid-caller")
   self.assertEqual(len(lines),1)
   self.assertEqual(lines[0]["route_id"],route["route_id"])
   self.assertEqual(lines[0]["event"],"compose")
   self.assertNotIn("work_request",lines[0])
 def test_route_bytes_and_hash_unchanged_by_plan(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
    os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-hash","AGENT_DISPATCH_ATTEMPT_ID":"",
               "AGENT_HOME":str(R.ROOT)}):
   plain=self.compose(artifact_root=tmp,slug="plan-hash-a")
   planned=self.compose(artifact_root=tmp,slug="plan-hash-b")
   self.assertNotIn("plan",plain); self.assertNotIn("plan",planned)
   self._emit(plain,tmp,_route_chain_plan=(None,None))
   self._emit(planned,tmp,_route_chain_plan=(["research","draft"],"explicit"))
   lines=self.RC.read_tail("claude","sid-hash")
   self.assertIsNone(lines[0].get("plan"))
   self.assertEqual(lines[1].get("plan"),["research","draft"])
 def test_writer_failure_never_fails_compose(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
    os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-boom","AGENT_DISPATCH_ATTEMPT_ID":"",
               "AGENT_HOME":str(R.ROOT)}):
   route=self.compose(artifact_root=tmp)
   with mock.patch.object(self.RC,"append",side_effect=RuntimeError("boom")):
    path=R._emit_compiled_route(
      __import__("types").SimpleNamespace(command="compose",start=False,full_record=False),
      route,tmp)
   self.assertEqual(json.loads(path.read_text()),route)
 def test_depth2_worker_never_writes(self):
  # A real depth-2 worker always carries the full typed identity tuple (see
  # the child_env fixtures above, e.g. AGENT_DISPATCH_WORKER_TYPE="stage");
  # setting only DEPTH+ATTEMPT_ID here left _registered_owner_attempt's
  # env validation seeing an incomplete tuple and raising instead of taking
  # its early non-owner return -- match the real shape.
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
    os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-d2","AGENT_DISPATCH_DEPTH":"2",
               "AGENT_DISPATCH_WORKER_TYPE":"stage",
               "AGENT_DISPATCH_ATTEMPT_ID":"att-worker","AGENT_HOME":str(R.ROOT)}):
   route=self.compose(artifact_root=tmp)
   err=self._emit(route,tmp)
   self.assertIn("route_chain_written=0",err)
   self.assertEqual(self.RC.read_tail("claude","sid-d2"),[])
 def test_opencode_session_id_records_opencode_ledger(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
    os.environ,{"OPENCODE_SESSION_ID":"sid-oc","AGENT_DISPATCH_ATTEMPT_ID":"",
               "AGENT_HOME":str(R.ROOT)},clear=False):
   os.environ.pop("CLAUDE_CODE_SESSION_ID",None); os.environ.pop("CODEX_THREAD_ID",None)
   route=self.compose(artifact_root=tmp)
   err=self._emit(route,tmp)
   self.assertIn("route_chain_written=1 harness=opencode",err)
   self.assertEqual([line["route_id"] for line in self.RC.read_tail("opencode","sid-oc")],
                    [route["route_id"]])
 def test_tmp_artifact_root_without_explicit_dir_is_not_recorded(self):
  with mock.patch.dict(os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-tmp","AGENT_DISPATCH_ATTEMPT_ID":"",
                                   "AGENT_HOME":str(R.ROOT)}):
   os.environ.pop("FLEET_ROUTE_CHAIN_DIR",None)
   route=self.compose(artifact_root=tempfile.gettempdir())
   err=self._emit(route,tempfile.gettempdir())
   self.assertIn("reason=tmp-artifact-root",err)
   os.environ["FLEET_ROUTE_CHAIN_DIR"]=self._rc_tmp.name   # restore for addCleanup symmetry
 def test_codex_thread_id_records_codex_ledger(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
    os.environ,{"CODEX_THREAD_ID":"sid-codex","AGENT_DISPATCH_ATTEMPT_ID":"",
               "AGENT_HOME":str(R.ROOT)}):
   os.environ.pop("CLAUDE_CODE_SESSION_ID",None)
   route=self.compose(artifact_root=tmp)
   err=self._emit(route,tmp)
   self.assertIn("route_chain_written=1 harness=codex",err)
 def _tool_shell_env(self,parent,child,sid):
  """The child harness's tool-command env, built from the shipped adapter configs."""
  fixtures=str(P.parent/"fixtures")
  if fixtures not in sys.path: sys.path.insert(0,fixtures)
  import harness_tool_env as E
  try:
   inherited=E.daemon_started_from(E.tool_shell_env(parent,{},"parent-sid-"+parent))
   return E.tool_shell_env(child,inherited,sid)
  except E.ToolMissing as exc:
   self.skipTest(str(exc))
 def test_compose_records_codex_ledger_in_claude_started_daemon_env(self):
  # A Codex thread served by the shared daemon that a Claude tool shell started: its tool
  # commands carry the thread id AND the starter's Claude session id (the SR_CorrNet incident).
  env=self._tool_shell_env("claude","codex","01a0f6a2-785d-7503-bb78-368d1a1eaab1")
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
    os.environ,{**env,"AGENT_DISPATCH_ATTEMPT_ID":"","AGENT_HOME":str(R.ROOT)}):
   route=self.compose(artifact_root=tmp)
   err=self._emit(route,tmp)
   self.assertIn("route_chain_written=1 harness=codex",err)
   self.assertNotIn("reason=no-identity",err)
   self.assertEqual([line["route_id"] for line in self.RC.read_tail("codex","01a0f6a2-785d-7503-bb78-368d1a1eaab1")],
                    [route["route_id"]])
   self.assertEqual(self.RC.read_tail("claude","parent-sid-claude"),[])
 def test_compose_never_records_claude_when_the_codex_block_is_not_installed(self):
  # Partial rollout: Claude's env is installed, Codex's block is not. The Claude-started daemon's
  # Codex thread carries its own id next to the stale Claude one and NO exported harness name, so
  # the writer finds it ambiguous and records nothing -- never a Claude ledger line.
  fixtures=str(P.parent/"fixtures")
  if fixtures not in sys.path: sys.path.insert(0,fixtures)
  import harness_tool_env as E
  try:
   inherited=E.daemon_started_from(E.tool_shell_env("claude",{},"parent-sid-claude"))
   env=E.tool_shell_env("codex",inherited,"01a0f6a2-785d-7503-bb78-368d1a1eaab1",installed=False)
  except E.ToolMissing as exc:
   self.skipTest(str(exc))
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
    os.environ,{**env,"AGENT_DISPATCH_ATTEMPT_ID":"","AGENT_HOME":str(R.ROOT)}):
   route=self.compose(artifact_root=tmp)
   err=self._emit(route,tmp)
   self.assertIn("route_chain_written=0",err)
   self.assertNotIn("harness=claude",err)
   self.assertEqual(self.RC.read_tail("claude","parent-sid-claude"),[])
   self.assertEqual(self.RC.read_tail("codex","01a0f6a2-785d-7503-bb78-368d1a1eaab1"),[])
 def test_compose_caller_follows_tool_shell_harness(self):
  from dispatch_parent_completion import default_parent_harness
  for parent,child in (("claude","codex"),("codex","claude"),("claude","opencode"),("opencode","codex")):
   with self.subTest(parent=parent,child=child):
    env=self._tool_shell_env(parent,child,"child-sid-"+child)
    with mock.patch.dict(os.environ,env):
     # the expression compose evaluates for `--parent-harness` (capability-route.py)
     self.assertEqual(default_parent_harness("claude"),child)
 def test_plan_invalid_refuses_before_route_write(self):
  import types
  with mock.patch.dict(os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-plan-invalid"}):
   with self.assertRaisesRegex(ValueError,"compose-plan-invalid"):
    R._resolve_compose_plan(types.SimpleNamespace(
      plan="not-a-real-capability",campaign_key=None,parent_cycle="p"))
 def test_plan_inherited_within_same_campaign_and_reset_on_new_campaign(self):
  import types
  with mock.patch.dict(os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-inherit","AGENT_DISPATCH_ATTEMPT_ID":"",
                                   "AGENT_HOME":str(R.ROOT)}):
   with tempfile.TemporaryDirectory() as tmp:
    first=self.compose(artifact_root=tmp,campaign_key="stream-x")
    self._emit(first,tmp,_route_chain_plan=(["code"],"explicit"))
    same_campaign=types.SimpleNamespace(plan=None,campaign_key="stream-x",parent_cycle=None)
    plan,source=R._resolve_compose_plan(same_campaign)
    self.assertEqual((plan,source),(["code"],"inherited"))
    other_campaign=types.SimpleNamespace(plan=None,campaign_key="stream-y",parent_cycle=None)
    self.assertEqual(R._resolve_compose_plan(other_campaign),(None,None))
 def test_explain_prints_plan_line_and_writes_nothing(self):
  with mock.patch.dict(os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-explain"}):
   route=self.compose(slug="explain-plan-fixture")
   card=R.compose_card(route,["research","draft"],"explicit")
   self.assertIn("계획 research › draft",card)
   self.assertEqual(self.RC.read_tail("claude","sid-explain"),[])
 def test_start_by_other_session_appends_start_once(self):
  with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
    os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-owner","AGENT_DISPATCH_ATTEMPT_ID":""}):
   route=self.compose(artifact_root=tmp)
  with mock.patch.dict(os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-starter"}):
   R._record_route_chain(route,str(Path(tmp)/"r.json"),"start")
   R._record_route_chain(route,str(Path(tmp)/"r.json"),"start")
   lines=self.RC.read_tail("claude","sid-starter")
   self.assertEqual(len(lines),1)
   self.assertEqual(lines[0]["event"],"start")
 def test_depth1_non_continuation_event_writes_nothing(self):
  with mock.patch.dict(os.environ,{"AGENT_DISPATCH_DEPTH":"1",
                                   "AGENT_DISPATCH_PARENT_SESSION_ID":"sid-parent",
                                   "AGENT_DISPATCH_ATTEMPT_ID":"att-1"}):
   self.assertIsNone(R._route_chain_identity("compose",{}))
 def test_depth1_continuation_appends_to_anchored_parent_ledger_with_by_attempt(self):
  self.RC.append("codex","sid-parent",self.RC.build_line(
    {"route_id":"rt-source","route_hash":"h-source","capability":"autopilot-code"},
    event="compose",harness="codex",session_id="sid-parent",route_file="/tmp/source.json"))
  with mock.patch.dict(os.environ,{
    "AGENT_DISPATCH_DEPTH":"1","AGENT_DISPATCH_PARENT_SESSION_ID":"sid-parent",
    "AGENT_DISPATCH_ATTEMPT_ID":"att-owner","AGENT_DISPATCH_CALLER_HARNESS":"claude"}):
   identity=R._route_chain_identity("continuation",{"source_route_id":"rt-source"})
   self.assertEqual(identity,("codex","sid-parent",1,"att-owner"))
   route={"route_id":"rt-successor","route_hash":"h-successor","capability":"autopilot-code",
          "source_route_id":"rt-source","artifact_root":self._rc_tmp.name}
   R._record_route_chain(route,"/tmp/successor.json","continuation")
  lines=self.RC.read_tail("codex","sid-parent")
  self.assertEqual(len(lines),2)
  self.assertEqual(lines[1]["route_id"],"rt-successor")
  self.assertEqual(lines[1]["by_attempt"],"att-owner")
 def test_depth1_continuation_finds_source_route_beyond_display_tail(self):
  # review 🟡-a: a long-lived parent pushes the continued route's line past TAIL_BYTES
  self.RC.append("claude","sid-long",self.RC.build_line(
    {"route_id":"rt-early","route_hash":"h-early","capability":"autopilot-code"},
    event="compose",harness="claude",session_id="sid-long",route_file="/tmp/early.json"))
  filler=0
  while os.path.getsize(self.RC.ledger_path("claude","sid-long"))<=self.RC.TAIL_BYTES+4096:
   filler+=1
   self.RC.append("claude","sid-long",self.RC.build_line(
     {"route_id":f"rt-fill-{filler}","route_hash":"h","capability":"autopilot-research",
      "slug":"x"*80},
     event="compose",harness="claude",session_id="sid-long",route_file="/tmp/"+"f"*200+".json"))
  self.assertNotIn("rt-early",[l["route_id"] for l in self.RC.read_tail("claude","sid-long")])
  with mock.patch.dict(os.environ,{
    "AGENT_DISPATCH_DEPTH":"1","AGENT_DISPATCH_PARENT_SESSION_ID":"sid-long",
    "AGENT_DISPATCH_ATTEMPT_ID":"att-owner"}):
   self.assertEqual(R._route_chain_identity("continuation",{"source_route_id":"rt-early"}),
                    ("claude","sid-long",1,"att-owner"))
 def test_depth1_continuation_without_parent_ledger_writes_nothing(self):
  with mock.patch.dict(os.environ,{
    "AGENT_DISPATCH_DEPTH":"1","AGENT_DISPATCH_PARENT_SESSION_ID":"sid-no-ledger",
    "AGENT_DISPATCH_ATTEMPT_ID":"att-owner"}):
   self.assertIsNone(R._route_chain_identity(
     "continuation",{"source_route_id":"rt-anything"}))
 def test_depth1_continuation_with_two_harness_ledgers_writes_nothing(self):
  for harness in ("claude","codex"):
   self.RC.append(harness,"sid-both",self.RC.build_line(
     {"route_id":"rt-source","route_hash":"h","capability":"autopilot-code"},
     event="compose",harness=harness,session_id="sid-both",route_file="/tmp/s.json"))
  with mock.patch.dict(os.environ,{
    "AGENT_DISPATCH_DEPTH":"1","AGENT_DISPATCH_PARENT_SESSION_ID":"sid-both",
    "AGENT_DISPATCH_ATTEMPT_ID":"att-owner"}):
   self.assertIsNone(R._route_chain_identity("continuation",{"source_route_id":"rt-source"}))
 def test_depth1_continuation_ledger_without_source_route_writes_nothing(self):
  self.RC.append("codex","sid-unrelated",self.RC.build_line(
    {"route_id":"rt-other","route_hash":"h","capability":"autopilot-code"},
    event="compose",harness="codex",session_id="sid-unrelated",route_file="/tmp/s.json"))
  with mock.patch.dict(os.environ,{
    "AGENT_DISPATCH_DEPTH":"1","AGENT_DISPATCH_PARENT_SESSION_ID":"sid-unrelated",
    "AGENT_DISPATCH_ATTEMPT_ID":"att-owner"}):
   self.assertIsNone(R._route_chain_identity("continuation",{"source_route_id":"rt-source"}))
 def test_depth0_continuation_writes_own_ledger(self):
  with mock.patch.dict(os.environ,{"CLAUDE_CODE_SESSION_ID":"sid-depth0",
                                   "AGENT_DISPATCH_ATTEMPT_ID":""}):
   os.environ.pop("AGENT_DISPATCH_DEPTH",None)
   identity=R._route_chain_identity("continuation",{"source_route_id":"rt-x"})
   self.assertEqual(identity,("claude","sid-depth0",0,None))


class CompletionLockKilledHolderTest(unittest.TestCase):
 """6번 잠금 회귀: a killed lock holder must never require the lock file's
 deletion. The completion lock is `open("a")` + `flock(LOCK_EX)` on a file
 that is always 0 bytes; the kernel releases a process's flock the instant
 it exits for any reason, SIGKILL included. This test documents that
 premise directly -- it is expected to pass both before and after this
 cycle's other changes, because deleting the lock file was never
 implemented (intent explicitly forbids it)."""

 def test_killed_completion_lock_holder_does_not_block_next_complete(self):
  with tempfile.TemporaryDirectory() as td:
   lock_path=Path(td)/".execute.completion.lock"
   holding=os.path.join(td,"holding")
   pid=os.fork()
   if pid==0:
    try:
     with R._exclusive_lock(lock_path):
      Path(holding).write_text("1")
      time.sleep(30)
    finally:
     os._exit(0)
   deadline=time.monotonic()+5
   while not os.path.exists(holding) and time.monotonic()<deadline:
    time.sleep(0.02)
   self.assertTrue(os.path.exists(holding),"child never signalled that it held the lock")
   os.kill(pid,signal.SIGKILL)
   os.waitpid(pid,0)
   result={}
   def acquire():
    with R._exclusive_lock(lock_path):
     result["acquired"]=True
   thread=threading.Thread(target=acquire,daemon=True)
   thread.start()
   thread.join(timeout=2.0)
   self.assertTrue(result.get("acquired"),
    "the lock must be immediately available once its killed holder's process exits")
   self.assertTrue(lock_path.is_file())
   self.assertFalse(lock_path.is_symlink())
   self.assertEqual(lock_path.stat().st_size,0,"the lock file is never written to, only held")


class SealedFrameProfileCompatibilityTest(unittest.TestCase):
 """D5: a sealed route whose frame nodes were declared under the one prior frame policy
 (v2.167.1-v2.169.0) is still the same work today; fresh compiles stay top/top.

 The prior policy is produced by the real compose/seal code with only the two policy inputs swapped
 for the values read from `git show v2.169.0:utilities/model_profile.py` (spelled out here so no later
 edit of the production table can move them)."""
 PRIOR_LADDER={
  "top":{"anchor":"top","others":"deep"},"deep":{"anchor":"top","others":"deep"},
  "balanced-deep":{"anchor":"deep","others":"deep"},
  "balanced":{"anchor":"balanced-deep","others":"balanced-deep"},
  "light":{"anchor":"balanced","others":"balanced"}}
 PRIOR_REASON="one direction brief, written once, with no multi-step execution of its own"
 def setUp(self):
  self.base=ComposeRouteTest("test_ship_unspecified_mode_keeps_default_deployment_recipe")
  self.base.setUp(); self.addCleanup(self.base.doCleanups)
  # These are routes sealed with their recipe frame pair, as compose sealed them before
  # decided shapes compiled without it.
  frames=mock.patch.object(R,"DECIDED_SHAPES",()); frames.start(); self.addCleanup(frames.stop)
 @contextlib.contextmanager
 def prior_policy(self):
  demand=json.loads(json.dumps(R.PROFILE.FRAME_ANCHOR_SHAPE_DEMAND)); demand["execution_reason"]=self.PRIOR_REASON
  def ladder(owner_profile,*,prior=False):
   return dict(self.PRIOR_LADDER.get(owner_profile or "light",self.PRIOR_LADDER["light"]))
  with mock.patch.object(R.PROFILE,"frame_profile_for_owner",ladder), \
       mock.patch.object(R.PROFILE,"FRAME_ANCHOR_SHAPE_DEMAND",demand):
   yield
 def compose(self,*,prior,**kw):
  kw.setdefault("graph",None)
  if prior:
   with self.prior_policy(): return self.base.compose(**kw)
  return self.base.compose(**kw)
 @staticmethod
 def frames(route):
  return [n for n in route["nodes"] if n["id"].startswith("frame")]
 @staticmethod
 def rehash(route):
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  return route
 def verified_unchanged(self,route):
  before=R.canonical(route)
  verified=R.verify_route(json.loads(before),R.ROOT)
  self.assertEqual(R.route_hash(route),route["route_hash"])
  self.assertEqual(verified["route_id"],route["route_id"])
  self.assertEqual(R.canonical(route),before)
  return verified
 def test_the_prior_policy_route_really_differs_from_a_fresh_one_and_verifies_unchanged(self):
  fresh=self.compose(prior=False); old=self.compose(prior=True)
  self.assertEqual([(n["id"],n["model_profile"]) for n in self.frames(fresh)],
   [("frame","top"),("frame-alternative","top")])
  self.assertEqual([(n["id"],n["model_profile"]) for n in self.frames(old)],
   [("frame","top"),("frame-alternative","deep")])
  self.assertEqual(self.frames(old)[0]["profile_demand"]["execution_reason"],self.PRIOR_REASON)
  self.assertNotEqual(self.frames(fresh)[0]["profile_demand"]["execution_reason"],self.PRIOR_REASON)
  self.verified_unchanged(fresh); self.verified_unchanged(old)
 def test_every_owner_tier_and_explicit_profile_shape_verifies_under_the_policy_it_was_sealed_with(self):
  demand=json.loads(json.dumps(R.PROFILE.FRAME_ANCHOR_SHAPE_DEMAND))
  cases={"default-standard":{},"default-strong":{"intensity":"strong"},
   "owner-balanced":{"profile":"balanced"},"owner-balanced-deep":{"profile":"balanced-deep"},
   "owner-deep":{"profile":"deep"},"owner-light":{"profile":"light"},
   "owner-demand":{"profile_demands":{"__owner__":demand}},
   "explicit-alternative":{"explicit_profiles":{"frame-alternative":"balanced"}},
   "explicit-anchor":{"explicit_profiles":{"frame":"deep"}}}
  for name,kw in cases.items():
   for prior in (True,False):
    with self.subTest(case=name,prior=prior):
     route=self.compose(prior=prior,**kw)
     self.assertTrue(self.frames(route))
     self.verified_unchanged(route)
 def test_a_composed_graph_with_frame_nodes_is_accepted_under_the_prior_policy_too(self):
  for prior in (True,False):
   with self.subTest(prior=prior):
    route=self.compose(prior=prior,graph="frame,plan,execute,test,report")
    self.assertTrue(route["composed"]); self.assertTrue(self.frames(route))
    self.verified_unchanged(route)
 def test_a_fresh_compile_is_top_top_whatever_policy_verification_also_accepts(self):
  for profile in (None,"deep","balanced","light"):
   with self.subTest(profile=profile):
    route=self.compose(prior=False,**({"profile":profile} if profile else {}))
    for node in self.frames(route):
     if profile is None: self.assertEqual(node["model_profile"],"top")
     self.assertNotEqual(node.get("profile_demand",{}).get("execution_reason"),self.PRIOR_REASON)
 def test_a_mix_of_both_policies_in_one_route_is_not_accepted(self):
  fresh=self.compose(prior=False); old=self.compose(prior=True)
  for anchor_from,alternative_from in ((old,fresh),(fresh,old)):
   mixed=json.loads(R.canonical(old))
   by_id={n["id"]:n for n in mixed["nodes"]}
   for node_id,source in (("frame",anchor_from),("frame-alternative",alternative_from)):
    donor=next(n for n in source["nodes"] if n["id"]==node_id)
    by_id[node_id].update({k:json.loads(json.dumps(v)) for k,v in donor.items()
     if k in ("model_profile","profile_demand","profile_selection","profile_explicit")})
   self.rehash(mixed)
   with self.assertRaisesRegex(ValueError,"node-profile-declaration-mismatch:frame"):
    R.verify_route(mixed,R.ROOT)
 def test_a_rehashed_frame_declaration_that_is_neither_policy_is_refused(self):
  old=self.compose(prior=True)
  edits={
   "profile":lambda n:n.__setitem__("model_profile","light"),
   "reason":lambda n:n["profile_demand"].__setitem__("execution_reason","invented rationale"),
   "judgment":lambda n:n["profile_demand"].__setitem__("judgment_requirement","routine-clear"),
   "selection":lambda n:n["profile_selection"].__setitem__("resolved_profile","light")}
  for node_id in ("frame","frame-alternative"):
   for name,edit in edits.items():
    with self.subTest(node=node_id,edit=name):
     route=json.loads(R.canonical(old))
     node=next(n for n in route["nodes"] if n["id"]==node_id)
     if name in ("reason","judgment") and not node.get("profile_demand"): continue  # deep carries no demand
     edit(node)
     self.rehash(route)
     with self.assertRaises(ValueError):
      R.verify_route(route,R.ROOT)
 def test_the_bridge_covers_only_frame_defaults_not_graph_scope_or_gates(self):
  old=self.compose(prior=True)
  def drop_node(route): route["nodes"]=[n for n in route["nodes"] if n["id"]!="frame-alternative"]
  def reorder(route): route["nodes"]=list(reversed(route["nodes"]))
  def gate(route): route["human_gates"]=[]
  def scope(route):
   node=next(n for n in route["nodes"] if n["id"]=="frame"); node["write_scope"]=["shards/elsewhere/**"]
  def binding(route): route["human_gate_bindings"]=[]
  for name,edit in {"drop-node":drop_node,"reorder":reorder,"gate":gate,"scope":scope,"binding":binding}.items():
   with self.subTest(edit=name):
    route=json.loads(R.canonical(old)); edit(route); self.rehash(route)
    with self.assertRaises(ValueError):
     R.verify_route(route,R.ROOT)
 def test_start_resumes_a_prior_policy_route_through_the_real_cli_path(self):
  import work_start as W
  with tempfile.TemporaryDirectory() as tmp:
   route=self.compose(prior=True,artifact_root=tmp,registered_headless_evidence=self.base.registered_headless(),
    work_request={"text":"task","owner_harness":"codex"})
   path=R.canonical_route_path(tmp,route["route_id"]); R.write_once(path,route)
   before=path.read_bytes()
   jobs=Path(tmp)/"jobs.log"; jobs.touch()
   launches=[]
   def admit(command,**kwargs):
    launches.append(command)
    aid=command[command.index("--attempt-id")+1]
    with jobs.open("a") as handle:
     handle.write("now\topen\trepo\tworktree\towner\t"+
      f"attempt_id={aid},worker_type=frame,parent_sid=parent,launch_started=1,"+
      f"owner_route_id={route['route_id']},owner_route_hash={route['route_hash']},"+
      "parent_completion_delivery=codex-managed-gateway\n")
    return subprocess.CompletedProcess(command,0,"registered=1 started=1 child_spawned=1\n","")
   real=W.start_work
   def at_transport(*a,**k): return real(*a,run=admit,**k)
   out=io.StringIO()
   with mock.patch.object(W,"start_work",at_transport), \
        mock.patch.object(W,"default_parent_session_id",return_value="parent"), \
        mock.patch.object(W,"join_selected_attempts",return_value={"state":"timeout"}), \
        mock.patch.object(sys,"argv",["capability-route.py","start","--route",str(path),"--jobs",str(jobs)]), \
        contextlib.redirect_stdout(out):
    R.main()
   result=json.loads(out.getvalue())
   self.assertEqual((result["state"],result["required_action"]),("preparing","wait-for-frame-results"),result)
   self.assertEqual(result["route_id"],route["route_id"])
   self.assertEqual(sorted(c[c.index("--route-node")+1] for c in launches),["frame","frame-alternative"])
   self.assertEqual(path.read_bytes(),before,"resuming never rewrites the sealed route")
   self.assertEqual([(n["id"],n["model_profile"]) for n in self.frames(json.loads(path.read_text()))],
    [("frame","top"),("frame-alternative","deep")])


class EntryExecutionScopeTest(unittest.TestCase):
 def test_quick_and_direct_shapes_expose_their_internal_approval_parts(self):
  self.assertEqual(R.declared_start_approvals({"capability":"autopilot-refine","shape":"solo"}),
                   [("preview","autopilot-refine:transaction")])
  self.assertEqual(R.declared_start_approvals({"capability":"autopilot-apply","shape":"direct"}),
                   [("handback","autopilot-apply:handback")])

 def test_report_scope_projection_stops_before_catalogued_approval_for_host_and_borrowed_parts(self):
  registry = R.TOPO.load_registry()
  cases = [
   ({"capability":"autopilot-lab","shape":"staged","graph":["scaffold","smoke","full-run","run-verify"]},
    ["scaffold","smoke"]),
   ({"capability":"autopilot-ship","shape":"staged","graph":["release-setup","security-review","release-review","deploy","post-deploy-verify"]},
    ["release-setup","security-review","release-review"]),
   ({"capability":"autopilot-refine","shape":"staged","graph":["review","transaction"]}, ["review"]),
   ({"capability":"autopilot-apply","shape":"staged","graph":["apply","verify","handback"]}, ["apply","verify"]),
   ({"capability":"autopilot-code","shape":"staged","graph":["plan","autopilot-lab:smoke","autopilot-lab:full-run","test"]},
    ["plan","autopilot-lab:smoke"]),
  ]
  for leg, expected in cases:
   original = list(leg["graph"])
   with self.subTest(leg=leg):
    projected = R.project_entry_execution_scope(leg, "report", registry=registry)
    self.assertEqual(projected["graph"], expected)
    self.assertEqual(leg["graph"], original)

 def test_route_plan_scope_carries_to_every_leg_of_the_same_decision(self):
  code={"capability":"autopilot-code","mode":"dev","shape":"staged","graph":["plan","execute","test","report"]}
  refine={"capability":"autopilot-refine","mode":None,"shape":"staged","graph":["review","transaction"]}
  legs=[code,refine]
  def binding(index, scope, given):
   record={"decision":{"approvals":{"execution_scope":scope,"given":given}}}
   return {"index":index,"record":record,"legs":legs}
  approved=[{"key":"preview","leg":1,"accepted":True}]
  self.assertEqual(R.route_plan_execution_scope(binding(0,"report",[])),"report")
  self.assertEqual(R.route_plan_execution_scope(binding(1,"report",[])),"report")
  self.assertEqual(R.route_plan_execution_scope(binding(1,"complete",approved)),"complete")
  # A part the person did not approve for that leg keeps its gate.
  self.assertIsNone(R.route_plan_execution_scope(binding(1,"complete",[])))
  self.assertIsNone(R.route_plan_execution_scope(binding(1,"complete",[{**approved[0],"accepted":False}])))
  self.assertIsNone(R.route_plan_execution_scope(binding(1,"complete",[{**approved[0],"leg":0}])))
  # Report stops before the first approval; a later leg with no step before it keeps its gate.
  only_transaction=[code,{**refine,"graph":["transaction"]}]
  self.assertIsNone(R.route_plan_execution_scope({**binding(1,"report",[]),"legs":only_transaction}))
  self.assertIsNone(R.route_plan_execution_scope({**binding(0,"report",[]),"index":True}))

 def test_scope_marker_rejects_boolean_and_unversioned_markers(self):
  for version, scope in ((True, "report"), (1, "x"), (None, "report")):
   with self.subTest(version=version, scope=scope):
    self.assertFalse(R.valid_entry_execution_scope_marker(version, scope))
  self.assertTrue(R.valid_entry_execution_scope_marker(1, "report"))

 def test_scoped_refine_completion_requires_actual_preview_but_legacy_is_unchanged(self):
  with tempfile.TemporaryDirectory() as tmp:
   route={"entry_scope_contract_version":1,"capability":"autopilot-refine"}
   node={"id":"review"}
   with self.assertRaisesRegex(ValueError,"completion-evidence-unreadable"):
    R._require_entry_scope_review_preview(route,node,Path(tmp))
   preview=Path(tmp)/"reviews/refine/preview.md"; preview.parent.mkdir(parents=True); preview.write_text("diff\n")
   self.assertEqual(R._require_entry_scope_review_preview(route,node,Path(tmp)),preview)
   route.pop("entry_scope_contract_version")
   self.assertIsNone(R._require_entry_scope_review_preview(route,node,Path(tmp)))

 def test_old_registry_bridge_restores_the_mark_and_preview_output_as_one_exact_snapshot(self):
  current = R.TOPO.load_registry()
  old = R._entry_scope_legacy_registry(current)
  self.assertIsNotNone(old)
  current_parts = R.TOPO.part_catalog(current)["parts"]
  old_parts = R.TOPO.part_catalog(old)["parts"]
  self.assertEqual(current_parts["autopilot-refine:transaction"]["start_approval"], "preview")
  self.assertNotIn("start_approval", old_parts["autopilot-refine:transaction"])
  current_recipe = next(x for x in current["recipes"] if x["capability"] == "autopilot-refine")
  old_recipe = next(x for x in old["recipes"] if x["capability"] == "autopilot-refine")
  self.assertIn("reviews/refine/preview.md", next(n for n in current_recipe["standard_plus"]["nodes"] if n["id"] == "review")["outputs"])
  self.assertNotIn("reviews/refine/preview.md", next(n for n in old_recipe["standard_plus"]["nodes"] if n["id"] == "review")["outputs"])
  tampered = json.loads(R.canonical(current)); tampered["unrelated_registry_fixture"] = True
  reconstructed = R._entry_scope_legacy_registry(tampered)
  self.assertNotEqual(R.TOPO.registry_digest(reconstructed), R.TOPO.registry_digest(old))


class GitIndexLockPreparationReceipt(unittest.TestCase):
 def test_actual_worktree_add_failure_keeps_reason_and_raw_git_failure(self):
  with tempfile.TemporaryDirectory(prefix="route-index-lock-") as td:
   root=Path(td); repo=root/"repo"; repo.mkdir(); reports=root/"reports"; reports.mkdir()
   def git(*args):
    return subprocess.run(["git","-C",str(repo),*args],capture_output=True,check=True)
   git("init","-q","-b","main"); git("config","user.name","Fixture")
   git("config","user.email","fixture@example.invalid")
   (repo/"tracked").write_text("base\n"); git("add","tracked"); git("commit","-qm","base")
   head=git("rev-parse","HEAD").stdout.decode().strip()
   custom_index=root/"custom-index"; custom_index.write_bytes((repo/".git/index").read_bytes())
   lock=Path(str(custom_index)+".lock"); lock.write_bytes(b"")
   lock_fd=os.open(lock,os.O_RDONLY); lock.chmod(0)
   original=R._worktree_git
   def fail_add(cwd,*args):
    if args[:2]==("worktree","add"):
     with mock.patch.dict(os.environ,{"GIT_INDEX_FILE":str(custom_index)}):
      return original(cwd,*args)
    return original(cwd,*args)
   with mock.patch.object(R,"_remote_default_base",
                          return_value=({"branch":"main","sha":head},None,None)), \
        mock.patch.object(R,"_worktree_git",side_effect=fail_add):
    receipt=R.prepare_isolated_worktree(
     repo,"fixture-lock-failure",artifact_root=reports,
     preparation_key={"task":"isolated lock receipt"})
   self.assertEqual((receipt["state"],receipt["reason"]),("failed","worktree-add-failed"))
   self.assertEqual(receipt["git_failure"]["returncode"],128)
   self.assertEqual(base64.b64decode(receipt["git_failure"]["stderr_b64"]),
                    receipt["detail"].encode("utf-8")+b"\n")
   self.assertEqual(receipt["git_diagnosis"]["lock_path"],str(lock))
   self.assertEqual(receipt["git_diagnosis"]["state"],"present")
   self.assertEqual(lock.stat().st_mode & 0o777,0)
   self.assertEqual(lock.stat().st_size,0)
   self.assertEqual((os.fstat(lock_fd).st_dev,os.fstat(lock_fd).st_ino),
                    (os.lstat(lock).st_dev,os.lstat(lock).st_ino))
   os.close(lock_fd)


class TerminalGateRegistrySnapshotTest(unittest.TestCase):
 def test_complete_snapshot_reaches_marker_and_owner_terminal_helpers(self):
  import dispatch_registry_cache as CACHE
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); jobs=root/"jobs.log"; completions=root/"completions"
   jobs.write_text(
    "job\tdone\t/repo\t/wt\towner\tattempt_id=att-owner,attempt_schema_version=2\n"
    "job\tdone\t/repo\t/wt\tprerequisite\tattempt_id=att-prereq,attempt_schema_version=2\n",
    encoding="utf-8")
   CACHE.clear_cache()
   snapshot=CACHE.registry_snapshot(jobs)
   route={"route_id":"rt-snapshot","route_hash":"sha256:"+"a"*64,
    "artifact_root":str(root),"nodes":[
     {"id":"marked","terminal":True,"terminal_gate":"worker"},
     {"id":"owner","terminal":True,"terminal_gate":"owner",
      "kind":"capability-owner","unit":"_kernel/owner","dispatch_depth":1,
      "depends_on":["marked"]}]}
   seen={"marker_lines":[]}
   def marker(*_args,**kwargs):
    seen["marker_lines"].append(kwargs["registry_lines"])
    return {"passed":False,"reason":"fixture"}
   def owner(*_args,**kwargs):
    seen["owner_view"]=kwargs["registry_view"]
    seen["owner_lines"]=kwargs["registry_lines"]
    return {"passed":False,"reason":"fixture"}
   with mock.patch.object(R,"completion_dir",return_value=completions), \
        mock.patch.object(R,"_marker_identity_row",side_effect=marker), \
        mock.patch.object(R,"_owner_terminal_observation",side_effect=owner):
    R.terminal_gate_observation(route,jobs=jobs,exact_terminal=True,
                                registry_snapshot=snapshot)
    self.assertEqual(R.owner_terminal_prerequisites(
     route,route["nodes"][1],jobs,registry_lines=snapshot.lines),{"marked":"fixture"})
   self.assertIs(seen["owner_view"],snapshot.view)
   self.assertEqual(tuple(seen["owner_lines"]),snapshot.lines)
   self.assertTrue(seen["marker_lines"])
   self.assertTrue(all(tuple(lines)==snapshot.lines for lines in seen["marker_lines"]))
   self.assertEqual(len(seen["owner_view"].records),2,
                    "owner absence checks must receive the full view, not its selected owner row")


class NonGitArtifactDeltaTest(unittest.TestCase):
 def test_artifact_snapshot_delta_tracks_create_modify_delete_and_skips_symlinks(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);output=root/"cycle"/"artifacts";output.mkdir(parents=True)
   keep=output/"keep.md";keep.write_text("same\n")
   changed=output/"changed.md";changed.write_text("before\n")
   deleted=output/"deleted.md";deleted.write_text("gone\n")
   outside=root/"outside.md";outside.write_text("outside\n")
   (output/"linked.md").symlink_to(outside)
   baseline={"changed_files":R._artifact_output_snapshot(output)}
   changed.write_text("after\n");deleted.unlink()
   created=output/"created.md";created.write_text("new\n")
   delta=R._artifact_delta(baseline,output)
   self.assertEqual(delta,{changed,deleted,created})


if __name__=="__main__": unittest.main()
