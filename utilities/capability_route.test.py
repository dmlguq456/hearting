#!/usr/bin/env python3
import contextlib, importlib.util, io, json, os, re, tempfile, unittest
from pathlib import Path

P=Path(__file__).with_name("capability-route.py")
S=importlib.util.spec_from_file_location("route",P); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
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
  self.assertEqual(compiled,126)
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
  self.assertEqual(replica["outputs"],["spec/_internal/research-alternative/**"])
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
   outcome,created=R.close_route(route,path,commit="0"*40,summary="demo")
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
 def test_close_records_false_and_warns_for_direct_unproven_gate(self):
  # Red before P2: schema 2 outcomes carry neither `terminal_gate_proven` nor
  # `terminal_gates`, and `close` never printed a warning at all -- this exercises the
  # real CLI so the stderr contract, not just the in-process dict, is covered. Direct
  # routes declare an `inline` terminal but nothing writes its marker in this test, so
  # the aggregate must be `False`, never `None` -- a direct/inline close that silently
  # reported "no terminal node" would hide every unproven direct closure.
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
   outcome=json.loads(result.stdout)
   self.assertEqual(outcome["schema_version"],3)
   self.assertFalse(outcome["terminal_gate_proven"])
   self.assertEqual(outcome["terminal_gates"]["inline"]["reason"],"completion-marker-absent")
   self.assertIn("terminal-gate-unproven",result.stderr)
   self.assertIn(route["route_id"],result.stderr)
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
   R.close_route(route,closed,commit="2"*40)
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
   R.close_route(first,path,commit="3"*40)
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
 def _run_compile_cli(self,argv):
  import subprocess,sys
  return subprocess.run(
   [sys.executable,str(P),"compile",*argv],capture_output=True,text=True,cwd=str(R.ROOT))
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
   expected=R.canonical_routes_dir(artifact_root)/f"{route['route_id']}.json"
   self.assertTrue(expected.is_file())
   self.assertIn(f"route_file={expected.resolve()}",result.stderr)
   self.assertEqual(json.loads(expected.read_text(encoding="utf-8"))["route_id"],route["route_id"])
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
   outcome,_=R.close_route(route,path,commit="8"*40)
   self.assertEqual(outcome["schema_version"],3)
   self.assertNotIn("publication",outcome)
 def test_close_route_publication_present_bumps_schema_v4(self):
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); route=dict(route); route["artifact_root"]=str(root)
   path=R.canonical_route_path(root,route["route_id"]); R.write_once(path,route)
   outcome,_=R.close_route(route,path,commit="9"*40,publication="failed")
   self.assertEqual(outcome["schema_version"],4)
   self.assertEqual(outcome["publication"],"failed")
 def test_close_route_on_alias_record_still_succeeds_with_drift_warning(self):
  route=R.compile_route(**self.args())
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); route=dict(route); route["artifact_root"]=str(root)
   alias=R.canonical_routes_dir(root)/"existing-alias.json"; R.write_once(alias,route)
   stderr=io.StringIO()
   with contextlib.redirect_stderr(stderr):
    outcome,created=R.close_route(route,alias,commit="a"*40)
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
   outcome,created=R.close_route(route,legacy,commit="4"*40,summary="legacy close")
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
   outcome,created=R.close_route(route,path,commit="b"*40)
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
   outcome,created=R.close_route(verified,path,commit="c"*40)
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
   outcome,created=R.close_route(verified,path,commit="d"*40)
   self.assertTrue(created)

if __name__=="__main__": unittest.main()
