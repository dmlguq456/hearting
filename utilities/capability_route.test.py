#!/usr/bin/env python3
import contextlib, importlib.util, io, json, os, tempfile, unittest
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
       expected,recipe["standard_plus"].get("parallel_groups"),intensity)
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
   ["frame","frame-alternative","frame-contrarian","plan","plan-alternative","plan-check","execute","impl-review","impl-review-alternative","test","report"])
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
   "plan-check":"qa/plan-review","execute":"dev/backend",
   "impl-review":"qa/code-review","impl-review-alternative":"qa/code-review",
   "test":"qa/test","report":"editorial/report"})
  tampered=json.loads(json.dumps(route)); tampered["nodes"][0]["unit"]="dev/backend"
  with self.assertRaisesRegex(ValueError,"stale or modified route hash"):
   R.verify_route(tampered,R.ROOT)
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

if __name__=="__main__": unittest.main()
