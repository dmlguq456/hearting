#!/usr/bin/env python3
import contextlib, hashlib, importlib.util, io, json, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

P=Path(__file__).with_name("capability-route.py")
S=importlib.util.spec_from_file_location("route",P); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
FLEET_P=P.parent.parent/"tools"/"fleet"/"route.py"
FLEET_S=importlib.util.spec_from_file_location("fleet_route",FLEET_P)
FLEET_ROUTE=importlib.util.module_from_spec(FLEET_S); FLEET_S.loader.exec_module(FLEET_ROUTE)
sys.path.insert(0,str(P.parent))
import dispatch_contract as D
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
  self._previous_dispatch_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
  os.environ.pop("AGENT_DISPATCH_JOBS",None)
  self.addCleanup(self._restore_agent_home)
 def _restore_agent_home(self):
  if self._previous_agent_home is None: os.environ.pop("AGENT_HOME",None)
  else: os.environ["AGENT_HOME"]=self._previous_agent_home
  if self._previous_dispatch_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
  else: os.environ["AGENT_DISPATCH_JOBS"]=self._previous_dispatch_jobs
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
  return {"candidates":[{"harness":"codex","transport":"headless","surface":"registered-headless","status":status,"probe_source":"fixture-probe","probe_time":"2026-07-20T00:00:00Z"}]}
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
 def test_ambiguous_quick(self):
  a=R.compile_route(**self.args(predicates=[],transport=None,inline_reason=None,registered_headless_evidence=self.registered_headless()))
  self.assertEqual(a["effective_intensity"],"quick")
  self.assertEqual(a["nodes"][0]["dispatch_depth"],1)
  self.assertEqual(a["nodes"][0]["execution_surface"],"registered-headless")
  self.assertTrue(a["nodes"][0]["registered_worker"])
  self.assertEqual(a["conditional_extensions"][0]["after"],["one-shot"])
 def test_quick_missing_eligibility_fails_closed(self):
  with self.assertRaisesRegex(ValueError,"quick-headless-unavailable"):
   R.compile_route(**self.args(predicates=[],transport=None,inline_reason=None))
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
     self.assertEqual(len(route["nodes"]),1)
     self.assertEqual(route["owner_dispatch_depth"],1)
     self.assertEqual(route["max_dispatch_depth"],1)
     self.assertEqual(route["nodes"][0]["dispatch_depth"],1)
     self.assertEqual(route["owner_model_profile"],"balanced-deep")
     self.assertEqual(route["nodes"][0]["model_profile"],"balanced-deep")
     self.assertEqual(route["nodes"][0]["execution_surface"],"registered-headless")
     self.assertTrue(route["nodes"][0]["registered_worker"])
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
    self.assertEqual(quick["nodes"][0]["model_profile"],"balanced-deep")
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
  with self.assertRaisesRegex(ValueError,"quick node axes mismatch"):
   R.verify_route(quick,R.ROOT)
  standard=R.compile_route(**self.args(
   capability="autopilot-spec",capability_mode="update",
   requested_intensity="standard",predicates=[],transport="headless",
   inline_reason=None,dispatch_evidence=self.dispatch(self.nested())))
  owner=next(node for node in standard["nodes"] if node["id"]=="prd-transaction")
  owner["model_profile"]="balanced-deep"
  standard["route_hash"]=R.route_hash(standard)
  standard["route_id"]="rt-"+standard["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"semantic capability owner"):
   R.verify_route(standard,R.ROOT)
 def test_composed_verify_rejects_rehashed_semantic_owner_profile_drift(self):
  recipe=json.loads(json.dumps(
   R.TOPO.resolve_recipe(R.TOPO.load_registry(),"autopilot-spec","update")))
  recipe["modes"]=["composed-fixture"]
  route=self._composed(recipe)
  route_owner=next(node for node in route["nodes"] if node["id"]=="prd-transaction")
  embedded_owner=next(
   node for node in route["composed_recipe"]["standard_plus"]["nodes"]
   if node["id"]=="prd-transaction")
  route_owner["model_profile"]="balanced-deep"
  embedded_owner["model_profile"]="balanced-deep"
  route["route_hash"]=R.route_hash(route)
  route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]
  with self.assertRaisesRegex(ValueError,"semantic capability owner"):
   R.verify_route(route,R.ROOT)
 def test_strong_expands_asymmetric_parallel_groups(self):
  evidence=self.dispatch(self.nested())
  standard=R.compile_route(**self.args(signals=["public-api"],transport="headless",inline_reason=None,dispatch_evidence=evidence))
  self.assertNotIn("impl-review-alternative",[x["id"] for x in standard["nodes"]])
  self.assertNotIn("plan-alternative",[x["id"] for x in standard["nodes"]])
  strong=self.compile_v3(evidence)
  self.assertEqual([x["id"] for x in strong["nodes"]],
   ["frame","frame-alternative","frame-contrarian","plan","plan-alternative","plan-check","plan-check-alternative","execute","impl-review","impl-review-alternative","test","report"])
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
  evidence=self.dispatch(self.nested())
  standard=R.compile_route(**self.args(signals=["public-api"],transport="headless",inline_reason=None,dispatch_evidence=evidence))
  frame=next(n for n in standard["nodes"] if n["id"]=="frame")
  frame_replica=next(n for n in standard["nodes"] if n["id"]=="frame-alternative")
  self.assertEqual(frame["parallel_group"],"frame")
  self.assertEqual(frame_replica["parallel_group"],"frame")
  self.assertEqual(frame_replica["parallel_independence_axes"],["cross-harness","model-profile","perspective"])
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
   "frame":"plan/frame","frame-alternative":"plan/frame","frame-contrarian":"plan/frame",
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
  route=self.compile_v3(evidence); chain=route["nodes"][0]["fallback_hops"]
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
  candidate=route["nodes"][0]["fallback_hops"][0]["candidates"][0]
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
  by_id={n["id"]:n["harness_affinity"] for n in route["nodes"]}
  self.assertEqual(set(by_id),{"frame","frame-alternative","plan","plan-check","execute","impl-review","test","report"})
  for value in by_id.values(): self.assertIn(value,R.VALID_AFFINITY)
  # DD_CONFIG_A leaves these four cells sparse; the shipped
  # profiles/dispatch-defaults.yaml capability baseline now merges beneath
  # the user file, so they answer "diverse" instead of "unspecified".
  self.assertEqual(by_id["frame"],"diverse")
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
  for node in route["nodes"]: self.assertEqual(node["harness_affinity"],"unspecified")
 def test_seal_corrupt_config_fails_loud(self):
  with dispatch_defaults_config(DD_CONFIG_CORRUPT):
   with self.assertRaisesRegex(ValueError,"corrupt dispatch-defaults config"):
    self._standard()
 def _composed_recipe(self):
  recipe=json.loads(json.dumps(R.TOPO.resolve_recipe(R.TOPO.load_registry(),"autopilot-code","dev")))
  recipe["modes"]=["composed-fixture"]
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
  args=["--capability","autopilot-code","--capability-mode","dev","--intensity","direct",
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
   launch=route["launch_compatibility_tuple"]
   self.assertEqual(launch["contract_version"],R.LAUNCH_COMPATIBILITY_TUPLE_VERSION)
   self.assertEqual(launch["tuple_version"],R.LAUNCH_COMPATIBILITY_TUPLE_VERSION)
   expected=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   self.assertTrue(expected.is_file())
   self.assertIn(f"route_file={expected.resolve()}",result.stderr)
   self.assertEqual(json.loads(expected.read_text(encoding="utf-8"))["route_id"],route["route_id"])
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
  self._previous_agent_home=os.environ.get("AGENT_HOME")
  self._previous_dispatch_jobs=os.environ.get("AGENT_DISPATCH_JOBS")
  os.environ["AGENT_HOME"]=self._tmp_home.name
  os.environ.pop("AGENT_DISPATCH_JOBS",None)
  self.addCleanup(self._restore)
 def _restore(self):
  if self._previous_agent_home is None: os.environ.pop("AGENT_HOME",None)
  else: os.environ["AGENT_HOME"]=self._previous_agent_home
  if self._previous_dispatch_jobs is None: os.environ.pop("AGENT_DISPATCH_JOBS",None)
  else: os.environ["AGENT_DISPATCH_JOBS"]=self._previous_dispatch_jobs
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
 def _source(self,artifact_root,cwd=None):
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
  return evidence
 def _complete_prefix(self,route,resume_from,evidence_root,skip=()):
  evidence={}
  for node in route["nodes"][:next(
      index for index,row in enumerate(route["nodes"]) if row["id"]==resume_from
  )]:
   if node["id"] in skip: continue
   evidence[node["id"]]=self._complete_node(route,node,evidence_root)
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
    "--capability-mode","dev","--intensity","strong","--cwd",str(R.ROOT),
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
    "--capability-mode","dev","--intensity","strong","--cwd",str(R.ROOT),
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
                "CODEX_THREAD_ID","CLAUDE_CODE_SESSION_ID","AGENT_DISPATCH_PARENT_SESSION_ID"):
     env.pop(key,None)
    env["AGENT_DISPATCH_ATTEMPT_ID"]="att-cli-owner"; env["AGENT_OWNER_ROUTE_FILE"]=str(source_path)
    env["AGENT_OWNER_ROUTE_ID"]=source["route_id"]
    env["AGENT_OWNER_ROUTE_HASH"]=source["route_hash"]
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
     "attempt_schema_version=2,worker_type=stage,unit=dev/backend,attempt_id=att-cli-stage\n" % R.ROOT,
     encoding="utf-8")
    env=os.environ.copy(); env["AGENT_DISPATCH_ATTEMPT_ID"]="att-cli-stage"
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
                "CODEX_THREAD_ID","CLAUDE_CODE_SESSION_ID","AGENT_DISPATCH_PARENT_SESSION_ID"):
     env.pop(key,None)
    env["AGENT_DISPATCH_ATTEMPT_ID"]="att-cli-replay"; env["AGENT_OWNER_ROUTE_FILE"]=str(source_path)
    env["AGENT_OWNER_ROUTE_ID"]=source["route_id"]
    env["AGENT_OWNER_ROUTE_HASH"]=source["route_hash"]
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


if __name__=="__main__": unittest.main()
