#!/usr/bin/env python3
"""SD-165: the stage parts catalog (stage-dispatch §13.64).

A route that borrows nothing and uses no optional part or widened choice keeps
the node bytes it had before the catalog existed (golden fixture below); a
`capability:stage` token borrows a shareable part into the host route.
"""
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


T = _load("producer_test_for_sd165", "artifact_producer.test.py")
R, P, TOPO = T.R, T.P, T.R.TOPO
import dispatch_stage_advance as ADVANCE  # noqa: E402

GOLDEN = HERE / "fixtures" / "sd165-route-golden.json"
GPU_EVAL_ADVISORY = (
    "  Codex GPU lab: danger-full-access (gpu-lab-resource); 대상 owner 및 GPU 실행·검증 노드 eval-run. "
    "filesystem/network OS enforcement 없음; 요청 root와 child≤parent는 논리 경계입니다. "
    "외부 sandbox·관리된 runtime 제약은 유지되며 GPU 조회 성공을 보증하지 않습니다."
)
GOLDEN_KEYS = (
    "parallel_groups", "completion_gates", "human_gates", "human_gate_bindings",
    "workflow_contract", "conditional_extensions", "resume_retry_boundaries", "composed_recipe",
)
PARTIAL_GRAPHS = (
    ("autopilot-code", "dev", "execute,test,report", "standard"),
    ("autopilot-code", "dev", "execute:dev/refactor,test", "standard"),
    ("autopilot-lab", "eval", "eval-run,metrics,report", "standard"),
    ("autopilot-lab", "eval", "report,independent-verify,publish,sync", "strong"),
    ("autopilot-lab", "setup", "smoke,full-run,run-verify", "standard"),
    ("autopilot-research", "technology", "retrieval,synthesis,report", "thorough"),
    ("audit", "default", "inspect,report", "standard"),
)


class CatalogBase(T.ProducerTestBase):
    """Isolated artifact root plus pinned dispatch defaults (machine independent)."""

    def setUp(self):
        super().setUp()
        self._pinned = {k: os.environ.get(k) for k in (
            "DISPATCH_DEFAULTS_CONFIG", "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_MODEL_GOVERNOR_ROOT")}
        os.environ["DISPATCH_DEFAULTS_CONFIG"] = str(R.ROOT / "profiles" / "dispatch-defaults.yaml")
        os.environ["AGENT_MODEL_GOVERNOR_ROOT"] = str(self.root / ".runtime" / "model-worker-governor")
        os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID", None)
        self.addCleanup(self._unpin)
        self.activate()
        self.count = 0

    def _unpin(self):
        for key, value in self._pinned.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def cycle(self, files, key, capability="autopilot-code", mode="dev"):
        """Begin a direct cycle in campaign `key` and leave `files` in its artifacts/."""
        self.count += 1
        route, route_file = self.route("direct", capability, mode, slug=f"prior-{self.count}", campaign_key=key)
        result = P.begin(self.root, route_file=route_file, capability=capability, intensity="direct")
        for rel in files:
            self.write_output(result, rel=rel, data=b"x\n")
        return result

    def compose(self, capability="autopilot-code", mode="dev", graph="execute,test,report",
                intensity=None, **kw):
        if kw.get("campaign_key") is None and kw.get("parent_cycle_id") is None:
            kw.setdefault("unassigned", True)
        return R.compose_route(
            capability=capability, capability_mode=mode, shape="staged", graph=graph,
            slug="sd165", cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", intensity=intensity,
            dispatch_evidence={"tuples": [T.nested("claude", "codex")]},
            registered_headless_evidence=T.registered_headless(), **kw)

    def compose_framed(self, **kw):
        return R.compose_route(
            capability=None, capability_mode=None, shape="framed", graph=None, slug="sd165", cwd=R.ROOT,
            artifact_root=self.root, spec_read="fixture", unassigned=True,
            dispatch_evidence={"tuples": [T.nested("claude", "codex")]}, **kw)

    @staticmethod
    def node(route, node_id):
        return next(n for n in route["nodes"] if n["id"] == node_id)


def _normalize(text, case):
    """Drop everything that names this checkout, this temp root, or this run."""
    for needle, token in ((str(case.root.resolve()), "<TMP>"), (str(case.root), "<TMP>"),
                          (str(R.ROOT.resolve()), "<ROOT>"), (str(R.ROOT), "<ROOT>")):
        text = text.replace(needle, token)
    text = re.sub(
        r"자동 선택에서 Codex owner의 workspace-write가 선택되면: workspace-write는 .*?실제 쓰기 성공을 보증하지는 않습니다\.",
        "자동 선택에서 Codex owner의 workspace-write가 선택되면: <WORKTREE-SCOPE-ADVISORY>.",
        text,
    )
    text = re.sub(r"rt-[0-9a-f]{16}", "<rt>", text)
    text = re.sub(r"cyc_[0-9a-f]{32}", "<cyc>", text)
    text = re.sub(r"camp_[0-9a-f]{32}", "<camp>", text)
    return re.sub(r"\d{4}-\d{2}-\d{2}_", "<date>_", text)


def _recipes():
    """The recipes a person can compose or preset; the compiler-internal framed recipe is reached
    only through `--shape framed` and has its own scenario."""
    return [(r["capability"], sorted(r["modes"])[0], [n["id"] for n in r["standard_plus"]["nodes"]])
            for r in TOPO.load_registry()["recipes"] if r["capability"] != R.ROUTE_FRAME_CAPABILITY]


def _historical_setup_projection(recipe, case):
    if recipe.get("capability") != "autopilot-lab" or "setup" not in recipe.get("modes", []):
        return recipe
    signals = list(recipe["promotion_signals"])
    case.assertEqual(signals.count("gpu"), 1)
    signals.remove("gpu")
    return {**recipe, "promotion_signals": signals}


def _historical_gpu_card(route, card, case):
    lines = card.splitlines()
    typed_gpu_nodes = [n["id"] for n in route["nodes"] if n.get("resource_class") == "gpu"]
    if route["capability"] == "autopilot-lab" and typed_gpu_nodes:
        case.assertEqual(typed_gpu_nodes, ["eval-run"])
        case.assertEqual(lines.count(GPU_EVAL_ADVISORY), 1)
        lines.remove(GPU_EVAL_ADVISORY)
    elif route["capability"] == "autopilot-lab":
        # This pre-catalog snapshot also predates signal-free lab execution
        # sandboxing. Its graph bytes stay pinned; GPU policy is tested separately.
        execution_nodes = [n["id"] for n in route["nodes"]
                           if (n.get("parallel_anchor") or n["id"])
                           in {"smoke", "full-run", "run-verify"}]
        if execution_nodes:
            advisory = GPU_EVAL_ADVISORY.replace("eval-run", ", ".join(execution_nodes))
            case.assertEqual(lines.count(advisory), 1)
            lines.remove(advisory)
    # The later confirmation display is independent of the pre-catalog graph.
    confirmation = [i for i, line in enumerate(lines) if line.startswith("  확인 방식 ")]
    if confirmation:
        case.assertEqual(len(confirmation), 1)
        start = confirmation[0]
        prefixes = ("  확인 방식 ", "  1. 주 capability: ", "  2. 새 실측: ",
                    "  3. standard+: ", "  4. 분리 단계: ", "  5. inline 예외: ",
                    "  6. 위임 표면: ", "  7. lineage·RUNLOG: ")
        block = lines[start:start + len(prefixes)]
        case.assertEqual(len(block), len(prefixes))
        for line, prefix in zip(block, prefixes):
            case.assertTrue(line.startswith(prefix), line)
        del lines[start:start + len(prefixes)]
    return "\n".join(lines)


def golden_payload(case):
    """Everything a catalog-free route seals, for every recipe and a few subgraphs.

    Nodes and sealed sections are pinned by digest of their normalized JSON (the
    fixture stays small); node ids, cards and SD-163 sources stay readable.
    """
    scenarios = {}

    def _digest(value):
        # The historical catalog snapshot predates the existing GPU signal's
        # admission for setup. Compare the catalog's graph bytes while the GPU
        # policy suite checks that new selection and its warning separately.
        if isinstance(value, dict) and value.get("capability") == "autopilot-lab" and "setup" in value.get("modes", []):
            value = _historical_setup_projection(value, case)
        text = _normalize(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False), case)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]

    def record(name, build):
        try:
            route = build()
        except Exception as exc:  # a refusal is behavior too; pin its text
            scenarios[name] = {"error": f"{type(exc).__name__}: {exc}"}
            return
        R.verify_route(route, R.ROOT)
        entry = {"node_ids": [n["id"] for n in route["nodes"]],
                 "nodes": {n["id"]: _digest(n) for n in route["nodes"]},
                 "input_sources": {n["id"]: n["input_sources"] for n in route["nodes"] if "input_sources" in n},
                 "card": _historical_gpu_card(route, R.compose_card(route), case),
                 # The brief names the prior cycle's dated folder; pin its normalized text, not the raw digest.
                 "briefs": {n["id"]: _digest(ADVANCE.render_stage_brief(route, n)[0]) for n in route["nodes"]
                            if n.get("kind") != "runtime-terminal"}}
        entry.update({key: _digest(route[key]) for key in GOLDEN_KEYS if key in route})
        scenarios[name] = entry

    for capability, mode, ids in _recipes():
        for intensity in ("standard", "strong", "thorough"):
            record(f"preset:{capability}:{mode}:{intensity}", lambda: R.compile_route(
                capability, mode, intensity, cwd=R.ROOT, artifact_root=case.root, predicates=[],
                transport="headless", tracking="tracked", tracked_gate_evidence=T.gate_evidence(),
                dispatch_evidence=T.dispatch_evidence(), registered_headless_evidence=T.registered_headless(),
                slug="sd165"))
        record(f"full:{capability}:{mode}", lambda: case.compose(capability, mode, ",".join(ids)))
    for capability, mode, graph, intensity in PARTIAL_GRAPHS:
        record(f"partial:{capability}:{mode}:{graph}:{intensity}",
               lambda: case.compose(capability, mode, graph, intensity))
    # A prior cycle exists: SD-163 fills the code subgraph, and the lab eval FULL graph stays untouched.
    case.cycle(("plan.md", "checklist.md"), "sd165-code")
    record("prior:code:execute,test,report", lambda: case.compose(campaign_key="sd165-code"))
    case.cycle(("eval-spec.md", "reviews/smoke-attestation.json", "metrics.jsonl"), "sd165-lab",
               "autopilot-lab", "eval")
    lab_ids = next(ids for capability, mode, ids in _recipes() if (capability, mode) == ("autopilot-lab", "eval"))
    record("prior:lab-eval:full", lambda: case.compose("autopilot-lab", "eval", ",".join(lab_ids),
                                                        campaign_key="sd165-lab"))
    # The compiler-internal framed route: its own scenario, never part of the user-preset enumeration.
    # The golden holds the two-leg framed route; one leg is the default now, so ask for both.
    record("framed:route-frame", lambda: case.compose_framed(intensity="strong"))
    registry = TOPO.load_registry()
    registry = json.loads(json.dumps(registry))
    registry["recipes"] = [_historical_setup_projection(recipe, case) for recipe in registry["recipes"]]
    digests = {capability: TOPO.capability_registry_digest(registry, capability)
               for capability in sorted({r["capability"] for r in registry["recipes"]})}
    return _normalize(json.dumps({"scenarios": scenarios, "capability_registry_digest": digests},
                                 sort_keys=True, indent=1, ensure_ascii=False), case)


class GoldenTest(CatalogBase):
    """A165-5: catalog-free routes keep their pre-catalog bytes and capability digest."""

    maxDiff = None

    def test_catalog_free_routes_match_the_pre_catalog_golden(self):
        payload = golden_payload(self)
        if os.environ.get("SD165_GOLDEN_WRITE") == "1":
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(payload + "\n", encoding="utf-8")
            return
        if not GOLDEN.is_file():
            self.fail("utilities/fixtures/sd165-route-golden.json missing; generate it on the "
                      "pre-catalog tree with SD165_GOLDEN_WRITE=1")
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        actual = json.loads(payload)
        self.assertEqual(sorted(actual["scenarios"]), sorted(expected["scenarios"]))
        for name in sorted(expected["scenarios"]):
            with self.subTest(scenario=name):
                self.assertEqual(actual["scenarios"][name], expected["scenarios"][name])
        self.assertEqual(actual["capability_registry_digest"], expected["capability_registry_digest"])


class HistoricalGpuExceptionTest(unittest.TestCase):
    """Fixed counterexamples keep the historical comparison exception narrow."""

    OLD_SIGNALS = ["resource-run", "session-independent-lifecycle", "smoke-required",
                   "human-gate", "stage-resume"]
    OLD_CARD = "CPU unchanged\napproval required\nunit lab/eval\ngate eval-verify"

    def route(self, target="eval-run", capability="autopilot-lab", resource_class="gpu"):
        return {"capability": capability, "nodes": [{"id": target, "resource_class": resource_class}]}

    def recipe(self, signals=None, **changes):
        value = {"capability": "autopilot-lab", "modes": ["setup"],
                 "promotion_signals": ["resource-run", "gpu", *self.OLD_SIGNALS[1:]],
                 "approval": "required", "unit": "lab/setup", "gate": "smoke-verify"}
        if signals is not None:
            value["promotion_signals"] = signals
        value.update(changes)
        return value

    def test_exact_notice_only_and_input_is_unchanged(self):
        card = self.OLD_CARD + "\n" + GPU_EVAL_ADVISORY
        self.assertEqual(_historical_gpu_card(self.route(), card, self), self.OLD_CARD)
        self.assertEqual(card, self.OLD_CARD + "\n" + GPU_EVAL_ADVISORY)
        for old, new in (("danger-full-access", "workspace-write"),
                ("gpu-lab-resource", "caller-cli"), ("gpu-lab-resource", "forced-env"),
                ("노드 eval-run", "노드 full-run"),
                ("OS enforcement 없음", "OS enforcement enforced"),
                ("논리 경계입니다", "OS 경계입니다")):
            changed = card.replace(old, new)
            self.assertNotEqual(changed, card, (old, new))
            with self.subTest(old=old, new=new), self.assertRaises(AssertionError):
                _historical_gpu_card(self.route(), changed, self)
        with self.assertRaises(AssertionError):
            _historical_gpu_card(self.route("full-run"), card, self)
        with self.assertRaises(AssertionError):
            _historical_gpu_card(self.route(), card + "\n" + GPU_EVAL_ADVISORY, self)

    def test_additional_notice_and_cpu_approval_unit_gate_changes_remain_visible(self):
        card = self.OLD_CARD + "\n" + GPU_EVAL_ADVISORY
        extra = GPU_EVAL_ADVISORY.replace("gpu-lab-resource", "caller-env")
        projected = _historical_gpu_card(self.route(), card + "\n" + extra, self)
        self.assertNotEqual(projected, self.OLD_CARD)
        self.assertIn(extra, projected)
        for old, new in (("CPU unchanged", "CPU changed"), ("approval required", "approval skipped"),
                         ("unit lab/eval", "unit code/test"), ("gate eval-verify", "gate removed")):
            with self.subTest(old=old):
                self.assertNotEqual(_historical_gpu_card(self.route(), card.replace(old, new), self), self.OLD_CARD)
        self.assertEqual(_historical_gpu_card(self.route(capability="autopilot-code"), card, self), card)
        self.assertEqual(_historical_gpu_card(self.route(resource_class="normal"), card, self), card)

    def test_single_setup_signal_only_and_other_signal_differences_remain_visible(self):
        recipe = self.recipe()
        original = json.loads(json.dumps(recipe))
        expected = self.recipe(signals=list(self.OLD_SIGNALS))
        self.assertEqual(_historical_setup_projection(recipe, self), expected)
        self.assertEqual(recipe, original)
        for signals in ([*recipe["promotion_signals"], "gpu"], list(self.OLD_SIGNALS)):
            with self.subTest(signals=signals), self.assertRaises(AssertionError):
                _historical_setup_projection(self.recipe(signals=signals), self)
        for extra in ("network", "resource-run"):
            with self.subTest(extra=extra):
                actual = _historical_setup_projection(self.recipe(signals=[*recipe["promotion_signals"], extra]), self)
                self.assertNotEqual(actual, expected)
                self.assertEqual(actual["promotion_signals"], [*self.OLD_SIGNALS, extra])
        for changes in ({"approval": "skipped"}, {"unit": "code/test"}, {"gate": "removed"},
                        {"graph": ["execute", "report"]}):
            with self.subTest(changes=changes):
                self.assertNotEqual(_historical_setup_projection(self.recipe(**changes), self), expected)
        for changes in ({"modes": ["eval"]}, {"capability": "autopilot-code"}):
            unchanged = self.recipe(**changes)
            self.assertEqual(_historical_setup_projection(unchanged, self), unchanged)


RETRIEVAL = "shards/parts/autopilot-research/retrieval/shards/retrieval/**"
AUDIT_BORROW = "inspect,autopilot-research:retrieval,autopilot-research:synthesis,report"
LAB_EVAL_PARTS = "eval-spec,eval-smoke,eval-run,metrics,diagnose,report"
CODE_RESOURCE = "execute,autopilot-lab:smoke,autopilot-lab:full-run,test,report"


class BorrowTest(CatalogBase):
    """A165-2: a shareable part is relocated inside the host's own artifact scope."""

    def ids(self, route):
        return [n["id"] for n in route["nodes"]]

    def test_audit_borrows_research_map_reduce(self):
        route = self.compose("audit", "default", AUDIT_BORROW)
        R.verify_route(route, R.ROOT)
        self.assertEqual(self.ids(route), [
            "inspect", "autopilot-research-retrieval", "autopilot-research-retrieval-alternative",
            "autopilot-research-synthesis", "report"])
        retrieval = self.node(route, "autopilot-research-retrieval")
        self.assertEqual(retrieval["part"], "autopilot-research:retrieval")
        self.assertEqual(retrieval["write_scope"], [RETRIEVAL])
        self.assertEqual(retrieval["part_io"], {"shards/retrieval/**": RETRIEVAL})
        self.assertEqual(retrieval["unit"], "research/research-survey")
        leg = self.node(route, "autopilot-research-retrieval-alternative")
        self.assertEqual(leg["write_scope"],
                         ["shards/parts/autopilot-research/retrieval/shards/retrieval-alternative/**"])
        self.assertEqual(leg["part_io"], {"shards/retrieval/**": leg["outputs"][0]})
        synthesis = self.node(route, "autopilot-research-synthesis")
        self.assertEqual(synthesis["inputs"], [RETRIEVAL, leg["outputs"][0]])
        self.assertEqual(len(synthesis["inputs"]), len(set(synthesis["inputs"])))
        self.assertEqual(synthesis["write_scope"], [
            "parts/autopilot-research/synthesis/analysis_summary.md",
            "parts/autopilot-research/synthesis/cards/**"])
        self.assertEqual(synthesis["part_io"], {
            "shards/retrieval/**": RETRIEVAL,
            "analysis_summary.md": "parts/autopilot-research/synthesis/analysis_summary.md",
            "cards/**": "parts/autopilot-research/synthesis/cards/**"})
        # The host stage after a borrowed part reads its result; host ownership is unchanged.
        report = self.node(route, "report")
        self.assertEqual(report["inputs"], ["reviews/audit/**", *synthesis["outputs"]])
        self.assertNotIn("part", report)
        self.assertNotIn("part_io", report)
        self.assertEqual(route["capability"], "audit")
        self.assertEqual(route["completion_gates"],
                         ["audit-inspect", "audit-report", "research-retrieval", "research-synthesis"])
        meta = route["composed_recipe"]["compose"]
        self.assertEqual(meta["graph"], AUDIT_BORROW.split(","))
        self.assertEqual(meta["parts"], ["autopilot-research:retrieval", "autopilot-research:synthesis"])
        # The anchor audit lacks is declared in the catalog and sealed only on a route that needs it.
        self.assertEqual(route["composed_recipe"]["artifact_scope"]["map_anchor"], "shards")
        plain = self.compose("audit", "default", "inspect,report")
        self.assertNotIn("map_anchor", plain["composed_recipe"]["artifact_scope"])
        self.assertNotIn("parts", plain["composed_recipe"]["compose"])
        self.assertIn("빌린 부품 autopilot-research:retrieval·autopilot-research:synthesis", R.compose_card(route))
        self.assertNotIn("빌린 부품", R.compose_card(plain))

    def test_auxiliary_leg_needs_a_selected_arbiter(self):
        arbitrated = self.compose("audit", "default", AUDIT_BORROW, "thorough")
        R.verify_route(arbitrated, R.ROOT)
        self.assertIn("autopilot-research-retrieval-assumption", self.ids(arbitrated))
        self.assertNotIn("omitted_parallel_presets", arbitrated["composed_recipe"]["compose"])
        alone = self.compose("audit", "default", "inspect,autopilot-research:retrieval,report", "thorough")
        R.verify_route(alone, R.ROOT)
        self.assertEqual(self.ids(alone), [
            "inspect", "autopilot-research-retrieval", "autopilot-research-retrieval-alternative", "report"])
        self.assertEqual(alone["composed_recipe"]["compose"]["omitted_parallel_presets"], [{
            "id": "autopilot-research-retrieval", "reason": "auxiliary-arbiter-not-selected",
            "legs": ["assumption"]}])
        report = self.node(alone, "report")
        self.assertEqual(report["inputs"], [
            "reviews/audit/**", RETRIEVAL,
            "shards/parts/autopilot-research/retrieval/shards/retrieval-alternative/**"])

    def test_every_host_scope_class_takes_a_borrowed_part(self):
        cases = {
            # implicit cycle anchor
            ("autopilot-code", "dev", "execute,autopilot-research:synthesis,test,report"):
                ["parts/autopilot-research/synthesis/analysis_summary.md",
                 "parts/autopilot-research/synthesis/cards/**"],
            # literal: every cycle anchor is kept
            ("autopilot-design", "default", "refs,autopilot-research:synthesis,build,visual-verify,handoff"):
                ["designs/<cycle>/parts/autopilot-research/synthesis/analysis_summary.md",
                 "spec/design/parts/autopilot-research/synthesis/analysis_summary.md",
                 "designs/<cycle>/parts/autopilot-research/synthesis/cards/**",
                 "spec/design/parts/autopilot-research/synthesis/cards/**"],
            ("autopilot-spec", "api", "research,autopilot-research:synthesis,review,prd-transaction"):
                ["spec/parts/autopilot-research/synthesis/analysis_summary.md",
                 "spec/<component>/parts/autopilot-research/synthesis/analysis_summary.md",
                 "spec/parts/autopilot-research/synthesis/cards/**",
                 "spec/<component>/parts/autopilot-research/synthesis/cards/**"],
            # target_relative
            ("audit", "default", "inspect,autopilot-research:synthesis,report"):
                ["parts/autopilot-research/synthesis/analysis_summary.md",
                 "parts/autopilot-research/synthesis/cards/**"],
        }
        for (capability, mode, graph), scope in cases.items():
            for intensity in ("standard", "thorough"):
                with self.subTest(capability=capability, intensity=intensity):
                    route = self.compose(capability, mode, graph, intensity)
                    R.verify_route(route, R.ROOT)
                    node = self.node(route, "autopilot-research-synthesis")
                    self.assertEqual(node["write_scope"], scope)
                    self.assertEqual(node["outputs"], [scope[0], next(s for s in scope if s.endswith("cards/**"))])
        literal_map = self.compose("autopilot-spec", "api", "autopilot-research:retrieval,research,review,prd-transaction",
                                   "thorough")
        R.verify_route(literal_map, R.ROOT)
        self.assertEqual(self.node(literal_map, "autopilot-research-retrieval")["write_scope"], [
            "spec/_internal/research/parts/autopilot-research/retrieval/shards/retrieval/**",
            "spec/<component>/_internal/research/parts/autopilot-research/retrieval/shards/retrieval/**"])
        review_part = self.compose("autopilot-draft", "doc", "draft-production,autopilot-lab:diagnose,finalize")
        R.verify_route(review_part, R.ROOT)
        self.assertEqual(self.node(review_part, "autopilot-lab-diagnose")["write_scope"], [
            "reviews/parts/autopilot-lab/diagnose/reviews/diagnosis/**",
            "reviews/parts/autopilot-lab/diagnose/reviews/diagnosis.md"])

    def test_host_qualified_token_is_the_local_stage(self):
        local = self.compose(graph="execute,test,report")
        qualified = self.compose(graph="autopilot-code:execute,autopilot-code:test,report")
        self.assertEqual(qualified["nodes"], local["nodes"])
        self.assertEqual(qualified["composed_recipe"], local["composed_recipe"])
        self.assertEqual(qualified["route_hash"], local["route_hash"])

    def test_borrowed_node_reads_its_origin_contract(self):
        import worker_bootstrap
        route = self.compose("audit", "default", AUDIT_BORROW)
        node = self.node(route, "autopilot-research-retrieval")
        text, digest = ADVANCE.render_stage_brief(route, node)
        self.assertIn("part: autopilot-research:retrieval\n", text)
        self.assertIn(f"part_io: shards/retrieval/**={RETRIEVAL}\n", text)
        self.assertIn("capability: audit\n", text)
        contract = worker_bootstrap.assigned_contract(
            capability=(node.get("part") or "").partition(":")[0] or route["capability"],
            worker_type="support", route_node=node["id"], completion_gate=node["completion_gate"], root=R.ROOT)
        self.assertEqual(contract, "autopilot-research")
        for source in ("dispatch-node.py", "stage-dispatch-fallback.py"):
            self.assertIn('(node.get("part") or "").partition(":")[0] or route["capability"]',
                          (HERE / source).read_text(encoding="utf-8"), source)


class LabEvalPartsTest(CatalogBase):
    """A165-3: eval-spec / eval-smoke / diagnose are optional parts of the lab eval recipe."""

    def test_optional_parts_seal_and_link_by_name(self):
        route = self.compose("autopilot-lab", "eval", LAB_EVAL_PARTS)
        R.verify_route(route, R.ROOT)
        self.assertEqual([n["id"] for n in route["nodes"]], LAB_EVAL_PARTS.split(","))
        run = self.node(route, "eval-run")
        self.assertEqual(run["inputs"], ["checkpoint", "eval-spec", "smoke-attestation"])
        self.assertEqual(run["part_io"], {"smoke-attestation": "reviews/smoke-attestation.json",
                                          "eval-spec": "eval-spec.md"})
        self.assertEqual(run["continuation"], {"kind": "supervised"})
        smoke = self.node(route, "eval-smoke")
        self.assertEqual((smoke["unit"], smoke["completion_gate"]), ("qa/ml-debug", "hash-bound-smoke"))
        self.assertEqual(smoke["part_io"], {"eval-spec": "eval-spec.md"})
        self.assertNotIn("part", smoke)  # the host's own part is not borrowed
        diagnose = self.node(route, "diagnose")
        self.assertEqual((diagnose["kind"], diagnose["unit"], diagnose["completion_gate"]),
                         ("review-worker", "qa/ml-debug", "lab-diagnose"))
        self.assertEqual(diagnose["inputs"], ["raw-results/**", "summary-stats.json"])
        self.assertEqual(self.node(route, "report")["inputs"],
                         ["metrics.jsonl", "summary-stats.json", "reviews/diagnosis.md"])
        self.assertEqual(route["human_gates"], [])
        self.assertEqual(route["composed_recipe"]["compose"]["parts"], [
            "autopilot-lab:diagnose", "autopilot-lab:eval-run", "autopilot-lab:eval-smoke",
            "autopilot-lab:eval-spec"])
        text, _ = ADVANCE.render_stage_brief(route, run)
        self.assertIn("part_io: eval-spec=eval-spec.md,smoke-attestation=reviews/smoke-attestation.json\n", text)

    def test_recipe_order_holds_for_optional_parts(self):
        with self.assertRaisesRegex(ValueError, "compose-graph-order:eval-run-before-eval-smoke"):
            self.compose("autopilot-lab", "eval", "eval-run,eval-smoke,metrics")
        with self.assertRaisesRegex(ValueError, "compose-graph-order:diagnose-before-metrics"):
            self.compose("autopilot-lab", "eval", "eval-run,diagnose,metrics")
        with self.assertRaisesRegex(ValueError, "compose-graph-unknown-node:diagnose"):
            self.compose("autopilot-lab", "setup", "scaffold,diagnose")  # an eval part, not a setup one

    def test_missing_parts_are_filled_from_the_prior_cycle_by_name(self):
        self.cycle(("eval-spec.md", "reviews/smoke-attestation.json"), "lab-fill", "autopilot-lab", "eval")
        route = self.compose("autopilot-lab", "eval", "eval-run,metrics,report", campaign_key="lab-fill")
        R.verify_route(route, R.ROOT)
        run = self.node(route, "eval-run")
        self.assertEqual(run["inputs"], ["checkpoint", "eval-spec", "smoke-attestation"])
        self.assertEqual(sorted(run["input_sources"]), ["eval-spec", "smoke-attestation"])
        self.assertTrue(run["input_sources"]["smoke-attestation"]["path"].endswith(
            "/artifacts/reviews/smoke-attestation.json"))
        self.assertTrue(run["input_sources"]["eval-spec"]["path"].endswith("/artifacts/eval-spec.md"))
        self.assertEqual(route["composed_recipe"]["compose"]["parts"], ["autopilot-lab:eval-run"])
        self.assertIn("입력 ", R.compose_card(route))

    def test_a_borrowed_attestation_is_found_under_its_relocated_name(self):
        # SD-163 correction (spec 13.65): a prior cycle of another capability is another flow, so the
        # cycle that holds the borrowed file is one of the composing capability.
        self.cycle(("_internal/parts/autopilot-lab/smoke/reviews/smoke-attestation.json",), "code-fill",
                   "autopilot-lab", "eval")
        route = self.compose("autopilot-lab", "eval", "eval-run,metrics,report", campaign_key="code-fill")
        source = self.node(route, "eval-run")["input_sources"]["smoke-attestation"]
        self.assertTrue(source["path"].endswith(
            "/artifacts/_internal/parts/autopilot-lab/smoke/reviews/smoke-attestation.json"))
        R.verify_route(route, R.ROOT)

    def test_without_a_prior_file_the_partial_graph_is_unchanged(self):
        self.cycle(("unrelated.md",), "lab-empty", "autopilot-lab", "eval")
        found = self.compose("autopilot-lab", "eval", "eval-run,metrics,report", campaign_key="lab-empty")
        plain = self.compose("autopilot-lab", "eval", "eval-run,metrics,report", campaign_key="lab-empty-2")
        self.assertTrue(all("input_sources" not in n and "part_io" not in n for n in found["nodes"]))
        self.assertEqual(found["nodes"], plain["nodes"])
        self.assertNotIn("parts", found["composed_recipe"]["compose"])

    def test_widened_choice_appears_only_when_picked(self):
        picked = self.compose("autopilot-lab", "eval", "eval-run,metrics:qa/ml-debug,report")
        R.verify_route(picked, R.ROOT)
        metrics = self.node(picked, "metrics")
        self.assertEqual((metrics["unit"], metrics["role"]), ("qa/ml-debug", "deep reviewer"))
        self.assertEqual(metrics["unit_choices"], ["material/data-script", "qa/ml-debug"])
        self.assertEqual(picked["composed_recipe"]["compose"]["parts"], ["autopilot-lab:metrics"])
        plain = self.compose("autopilot-lab", "eval", "eval-run,metrics,report")
        named = self.compose("autopilot-lab", "eval", "eval-run,metrics:material/data-script,report")
        for route in (plain, named):
            self.assertNotIn("unit_choices", self.node(route, "metrics"))
            self.assertNotIn("parts", route["composed_recipe"]["compose"])
        self.assertEqual(plain["nodes"], named["nodes"])
        audit = self.compose("audit", "default", "inspect,report:research/research-survey")
        R.verify_route(audit, R.ROOT)
        self.assertEqual(self.node(audit, "report")["unit"], "research/research-survey")
        # A declared recipe choice under a unit-io gate now seals too.
        scaffold = self.compose("autopilot-lab", "setup", "scaffold:dev/backend,smoke")
        R.verify_route(scaffold, R.ROOT)
        self.assertEqual(self.node(scaffold, "scaffold")["unit"], "dev/backend")


class RefusalTest(CatalogBase):
    """A165-6: what cannot be borrowed keeps today's typed answers."""

    def test_unshared_and_unregistered_tokens_are_unknown_nodes(self):
        for graph in ("inspect,autopilot-code:execute,report",      # writes source/**: never shareable
                      "inspect,autopilot-lab:nope,report",          # not registered
                      "inspect,autopilot-research:report,report",   # registered, not shareable
                      "inspect,autopilot-lab:full-run,report",      # shareable, but not to this host
                      "inspect,autopilot-lab:eval-spec,report"):    # optional part of another recipe
            with self.subTest(graph=graph):
                with self.assertRaisesRegex(ValueError, r"compose-graph-unknown-node:.*capability-route\.py stages"):
                    self.compose("audit", "default", graph)
        with self.assertRaisesRegex(ValueError, "compose-graph-unknown-node:autopilot-research:retrieval"):
            # a map part needs a host map anchor; apply declares none
            self.compose("autopilot-apply", "default", "apply,autopilot-research:retrieval,verify,handback")

    def test_reserved_units_and_existing_refusals_are_unchanged(self):
        with self.assertRaisesRegex(ValueError, "compose-unit-override-reserved:autopilot-lab-full-run"):
            self.compose(graph="execute,autopilot-lab:full-run:dev/backend,test")
        with self.assertRaisesRegex(ValueError, "compose-unit-override-reserved:eval-run"):
            self.compose("autopilot-lab", "eval", "eval-run:dev/backend,metrics")
        with self.assertRaisesRegex(ValueError, "compose-unit-not-in-choices:execute:bogus"):
            self.compose(graph="execute:bogus,test")
        with self.assertRaisesRegex(ValueError, "compose-graph-duplicate-node"):
            self.compose(graph="execute,autopilot-code:execute")
        with self.assertRaisesRegex(ValueError, "compose-graph-unknown-node:autopilot-lab "):
            self.compose(graph="execute,autopilot-lab:,test")  # no stage named: the pre-catalog answer
        with self.assertRaises(TOPO.TopologyError):  # a resource run still cannot end a workflow
            self.compose(graph="execute,autopilot-lab:smoke,autopilot-lab:full-run")


class StagesTest(unittest.TestCase):
    """A165-1: `stages` is the catalog a frame assembles from."""

    def blocks(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
        env["AGENT_HOME"] = str(R.ROOT)
        result = subprocess.run([sys.executable, str(HERE / "capability-route.py"), "stages", *args],
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_every_part_carries_the_catalog_fields(self):
        blocks = json.loads(self.blocks("--json"))
        self.assertEqual(len(blocks), len(_recipes()))  # the internal framed recipe is never listed
        self.assertNotIn("route-frame", {block["capability"] for block in blocks})
        keys = {"id", "unit", "unit_choices", "parallel_group", "human_gates", "terminal", "part", "summary",
                "kind", "inputs", "external_inputs", "optional_inputs", "outputs", "shareable",
                "start_approval", "optional", "after", "before", "frame_alias"}
        for block in blocks:
            for node in block["nodes"] + block["borrowable"]:
                self.assertEqual(set(node), keys, node.get("part"))
                self.assertTrue(node["summary"], node["part"])
                self.assertRegex(node["part"], r"^[a-z][a-z0-9-]*:[a-z][a-z0-9-]*$")
        parts = {node["part"]: node for block in blocks for node in block["nodes"]}
        self.assertEqual({part: node["start_approval"] for part, node in parts.items() if node["start_approval"]}, {
            "autopilot-lab:full-run": "full-run", "autopilot-ship:deploy": "deploy",
            "autopilot-apply:handback": "handback",
            "autopilot-refine:transaction": "preview"})
        self.assertEqual(sorted(part for part, node in parts.items() if node["shareable"]), [
            "autopilot-lab:diagnose", "autopilot-lab:full-run", "autopilot-lab:smoke",
            "autopilot-research:retrieval", "autopilot-research:synthesis"])
        self.assertEqual(sorted(part for part, node in parts.items() if node["optional"]), [
            "autopilot-lab:diagnose", "autopilot-lab:eval-smoke", "autopilot-lab:eval-spec",
            "autopilot-lab:resume-run"])
        self.assertFalse(parts["autopilot-code:execute"]["shareable"])
        self.assertEqual(parts["autopilot-lab:metrics"]["unit_choices"], ["material/data-script", "qa/ml-debug"])
        self.assertEqual(parts["audit:report"]["unit_choices"], ["editorial/polish", "research/research-survey"])
        self.assertEqual(parts["autopilot-lab:diagnose"]["optional_inputs"], ["field-samples", "repro-conditions"])
        self.assertEqual(parts["autopilot-lab:eval-run"]["external_inputs"],
                         ["checkpoint", "eval-spec", "smoke-attestation"])
        self.assertEqual(parts["autopilot-lab:eval-smoke"]["before"], ["eval-run"])
        self.assertTrue(parts["autopilot-code:frame"]["frame_alias"])
        by_host = {(b["capability"], tuple(b["modes"])): [n["part"] for n in b["borrowable"]] for b in blocks}
        self.assertEqual(by_host[("audit", ("default",))], [
            "autopilot-research:retrieval", "autopilot-research:synthesis", "autopilot-lab:diagnose"])
        self.assertIn("autopilot-lab:full-run", by_host[("autopilot-code", ("audit", "debug", "dev"))])
        self.assertNotIn("autopilot-lab:full-run", by_host[("autopilot-draft", ("doc", "paper", "presentation"))])
        design = next(b for b in blocks if b["capability"] == "autopilot-design")
        self.assertEqual(design["frame_brief_inputs"], [
            "designs/<cycle>/01_refs/frame/direction-brief.md",
            "designs/<cycle>/01_refs/frame-alternative/direction-brief.md"])

    def test_text_form_keeps_the_stage_line_prefix(self):
        text = self.blocks("--capability", "autopilot-lab")
        self.assertIn("  full-run unit=_kernel/resource "
                      "part=autopilot-lab:full-run shareable=1 optional=0 start_approval=full-run ", text)
        self.assertIn("  diagnose unit=qa/ml-debug part=autopilot-lab:diagnose shareable=1 optional=1 ", text)
        self.assertIn("  borrow autopilot-lab:smoke unit=qa/ml-debug ", text)


class StartApprovalTest(CatalogBase):
    """13.63.7 (declaration half): the mark is data for the card and the catalog, never a gate."""

    def test_own_parts_are_reported_without_changing_the_route(self):
        expected = {("autopilot-lab", "setup"): ("full-run", "autopilot-lab:full-run", "full-run"),
                    ("autopilot-ship", "default"): ("deploy", "autopilot-ship:deploy", "deploy"),
                    ("autopilot-apply", "default"): ("handback", "autopilot-apply:handback", "handback"),
                    ("autopilot-refine", "default"): ("transaction", "autopilot-refine:transaction", "preview")}
        for (capability, mode), (node, part, approval) in expected.items():
            with self.subTest(capability=capability):
                route = R.compile_route(
                    capability, mode, "strong", cwd=R.ROOT, artifact_root=self.root, predicates=[],
                    transport="headless", tracking="tracked", tracked_gate_evidence=T.gate_evidence(),
                    dispatch_evidence=T.dispatch_evidence(),
                    registered_headless_evidence=T.registered_headless(), slug="sd165")
                self.assertEqual(R.route_start_approvals(route), [
                    {"node": node, "part": part, "start_approval": approval, "borrowed": False}])
                self.assertTrue(all("start_approval" not in n and "part" not in n for n in route["nodes"]))
        code = self.compose()
        self.assertEqual(R.route_start_approvals(code), [])


class ResourceShareTest(CatalogBase):
    """A165-4: a code route borrows the lab smoke and the detached full run."""

    def test_code_route_borrows_smoke_and_full_run(self):
        route = self.compose(graph=CODE_RESOURCE)
        R.verify_route(route, R.ROOT)
        smoke = self.node(route, "autopilot-lab-smoke")
        full = self.node(route, "autopilot-lab-full-run")
        attestation = "_internal/parts/autopilot-lab/smoke/reviews/smoke-attestation.json"
        self.assertEqual(smoke["outputs"], [attestation])
        self.assertEqual((full["kind"], full["unit"], full["resource_transport"]),
                         ("resource-runner", "_kernel/resource", "detached-process"))
        self.assertEqual(full["continuation"], {"kind": "supervised"})
        self.assertEqual(full["inputs"], [attestation, "config"])
        self.assertEqual(full["part_io"]["reviews/smoke-attestation.json"], attestation)
        self.assertEqual(full["write_scope"], [
            "parts/autopilot-lab/full-run/run.json", "parts/autopilot-lab/full-run/logs/**",
            "parts/autopilot-lab/full-run/checkpoints/**"])
        self.assertEqual(full["start_approval"], "full-run")
        self.assertNotIn("start_approval", smoke)
        # The borrowed smoke has no human gate; the borrowed full-run part carries the start approval.
        self.assertEqual(route["human_gates"], [])
        self.assertEqual(route["human_gate_bindings"], [])
        self.assertEqual(self.node(route, "test")["inputs"], [
            "source-diff", "parts/autopilot-lab/full-run/run.json", "parts/autopilot-lab/full-run/logs/**"])
        self.assertEqual(R.route_start_approvals(route), [{
            "node": "autopilot-lab-full-run", "part": "autopilot-lab:full-run",
            "start_approval": "full-run", "borrowed": True}])
        card = R.compose_card(route)
        self.assertIn("빌린 부품 autopilot-lab:smoke·autopilot-lab:full-run", card)
        self.assertIn("시작 승인 full-run (autopilot-lab:full-run)", card)

    def test_host_parts_show_start_approval_and_unmarked_routes_do_not(self):
        for capability, mode, mark, part in (
            ("autopilot-lab", "setup", "full-run", "autopilot-lab:full-run"),
            ("autopilot-ship", "default", "deploy", "autopilot-ship:deploy"),
            ("autopilot-apply", "default", "handback", "autopilot-apply:handback"),
        ):
            route = R.compile_route(
                capability, mode, "strong", cwd=R.ROOT, artifact_root=self.root, predicates=[],
                transport="headless", tracking="tracked", tracked_gate_evidence=T.gate_evidence(),
                dispatch_evidence=T.dispatch_evidence(),
                registered_headless_evidence=T.registered_headless(), slug="sd165")
            self.assertIn(f"시작 승인 {mark} ({part})", R.compose_card(route))
        self.assertNotIn("시작 승인", R.compose_card(self.compose()))

    def test_lab_partial_graph_keeps_inline_smoke_and_supervised_full_run(self):
        route = self.compose("autopilot-lab", "setup", "smoke,full-run,run-verify")
        nodes = {node["id"]: node for node in route["nodes"]}
        self.assertEqual(nodes["smoke"]["continuation"], {"kind": "inline-next"})
        self.assertEqual(nodes["full-run"]["continuation"], {"kind": "supervised"})
        self.assertEqual(route["human_gates"], [])
        self.assertEqual(route["human_gate_bindings"], [])

    def test_smoke_omitted_attestation_comes_from_the_prior_cycle(self):
        # SD-163 correction (spec 13.65): the prior cycle is one of the composing (code) capability.
        self.cycle(("experiments/reviews/smoke-attestation.json",), "res-fill")
        route = self.compose(graph="execute,autopilot-lab:full-run,test,report", campaign_key="res-fill")
        R.verify_route(route, R.ROOT)
        full = self.node(route, "autopilot-lab-full-run")
        self.assertEqual(full["inputs"], ["reviews/smoke-attestation.json", "config"])
        self.assertTrue(full["input_sources"]["reviews/smoke-attestation.json"]["path"].endswith(
            "/artifacts/experiments/reviews/smoke-attestation.json"))

    def test_resource_runner_starts_the_borrowed_full_run_with_its_attestation(self):
        runner, smoke_tool = HERE / "resource-runner.py", R.ROOT / "tools" / "smoke-attestation.py"
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
        base = Path(self._tmp.name) / "resource"
        repo, artifacts = base / "repo", base / "artifacts"
        repo.mkdir(parents=True)
        artifacts.mkdir()
        env.update(AGENT_HOME=str(R.ROOT), AGENT_DISPATCH_JOBS=str(base / "jobs.log"),
                   AGENT_RESOURCE_RUN_INDEX=str(base / "resource-runs.index.json"),
                   DISPATCH_DEFAULTS_CONFIG=str(R.ROOT / "profiles" / "dispatch-defaults.yaml"))
        (base / "jobs.log").write_text("", encoding="utf-8")
        for argv in (["git", "init", "-q", str(repo)],
                     ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
                     ["git", "-C", str(repo), "config", "user.name", "Test"]):
            subprocess.run(argv, check=True)
        (repo / "config").write_text("ok\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        evidence = base / "dispatch-evidence.json"
        evidence.write_text(json.dumps({"tuples": [dict(T.nested("claude", "codex"),
                                                        checked_worktree=str(repo.resolve()))]}))
        composed = subprocess.run([
            sys.executable, str(HERE / "capability-route.py"), "compose", "--shape", "staged",
            "--graph", CODE_RESOURCE, "--slug", "sd165-resource", "--unassigned", "--cwd", str(repo),
            "--artifact-root", str(artifacts), "--dispatch-evidence", str(evidence),
            "--parent-harness", "claude", "--spec-read", "not-applicable"],
            text=True, capture_output=True, env=env)
        self.assertEqual(composed.returncode, 0, composed.stderr)
        self.assertIn("시작 승인 full-run (autopilot-lab:full-run)", composed.stderr)
        route_file = Path(json.loads(composed.stdout)["route_file"])
        route = json.loads(route_file.read_text())
        full = next(n for n in route["nodes"] if n["id"] == "autopilot-lab-full-run")
        attestation = base / "cycle" / full["part_io"]["reviews/smoke-attestation.json"]
        subprocess.run([sys.executable, str(smoke_tool), "attest", "--input", str(repo / "config"),
                        "--cwd", str(repo), "--output", str(attestation), "--", sys.executable, "-c", "pass"],
                       check=True, stdout=subprocess.DEVNULL, env=env)
        registry, launched = base / "registry.json", base / "launched"

        def start(run_id, *extra):
            return subprocess.run([
                sys.executable, str(runner), "--registry", str(registry), "start", "--run-id", run_id,
                "--cwd", str(repo), "--log", str(base / "logs" / f"{run_id}.log"), "--route", str(route_file),
                "--node", "autopilot-lab-full-run", *extra, "--", sys.executable, "-c",
                f"from pathlib import Path; import time; Path({str(launched)!r}).write_text('x'); time.sleep(30)"],
                text=True, capture_output=True, env=env)

        def stop_runs():
            try:
                runs = json.loads(registry.read_text()).get("runs", {})
            except (OSError, ValueError):
                return
            for run in runs.values():
                try:
                    group = int(run.get("process_group"))
                    if group > 1 and group != os.getpgrp():
                        os.killpg(group, signal.SIGKILL)
                except (OSError, TypeError, ValueError):
                    continue
        self.addCleanup(stop_runs)
        missing = start("no-attestation")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("hash-bound smoke attestation required", missing.stderr)
        started = start("with-attestation", "--smoke-attestation", str(attestation))
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(json.loads(started.stdout.strip().splitlines()[-1])["node"], "autopilot-lab-full-run")
        for _ in range(100):
            if launched.exists():
                break
            time.sleep(0.05)
        self.assertTrue(launched.exists())
        (repo / "config").write_text("changed after the smoke\n")
        stale = start("stale-attestation", "--smoke-attestation", str(attestation))
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("stale smoke input", stale.stderr)


if __name__ == "__main__":
    unittest.main()
