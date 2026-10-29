#!/usr/bin/env python3
"""SD-163: a partial compose graph keeps inputs found in the prior cycle, and the
route artifact env names that cycle's output folder (stage-dispatch §13.62)."""
import contextlib
import hashlib
import json
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


T = _load("producer_test_for_sd163", "artifact_producer.test.py")
OWNER_T = _load("owner_test_for_sd163", "dispatch_owner.test.py")
R, P, TOPO = T.R, T.P, T.R.TOPO
import dispatch_stage_advance as ADVANCE  # noqa: E402
import route_plan as RP  # noqa: E402

PARENT_VAR = "AGENT_ARTIFACT_PARENT_OUTPUT_DIR"
SIX = ["AGENT_ARTIFACT_ROOT", "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID",
       "AGENT_ARTIFACT_PRODUCER_ID", "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR"]
FRESH = {"execute": ["task"], "test": ["source-diff"], "report": ["dev_logs/**", "test_logs/**"]}


class SourceBase(T.ProducerTestBase):
    def setUp(self):
        super().setUp()
        self.activate()
        self.count = 0

    def cycle(self, files=("plan.md", "checklist.md"), key="k163", parent=None):
        """Begin a direct cycle and leave `files` (relative to artifacts/) in it."""
        self.count += 1
        route, route_file = self.route("direct", slug=f"prior-{self.count}",
                                       campaign_key=None if parent else key, parent_cycle_id=parent)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for rel in files:
            self.write_output(result, rel=rel, data=b"x\n")
        return result

    def compose(self, graph="execute,test,report", **kw):
        return R.compose_route(
            capability="autopilot-code", capability_mode="dev", shape="staged", graph=graph,
            slug="sd163", cwd=R.ROOT, artifact_root=self.root, spec_read="fixture",
            dispatch_evidence={"tuples": [T.nested("claude", "codex")]}, **kw)

    def without_finder(self, **kw):
        with mock.patch.object(sys.modules["artifact_producer"], "input_source_finder", return_value=None):
            return self.compose(**kw)

    @staticmethod
    def node(route, node_id):
        return next(n for n in route["nodes"] if n["id"] == node_id)

    def inputs(self, route):
        return {n["id"]: n["inputs"] for n in route["nodes"]}

    def assert_not_found(self, **kw):
        """The compose result is byte-identical to a compose that had no finder at all."""
        route = self.compose(**kw)
        baseline = self.without_finder(**kw)
        self.assertEqual(self.inputs(route), FRESH)
        self.assertTrue(all("input_sources" not in n for n in route["nodes"]))
        self.assertEqual(route["route_hash"], baseline["route_hash"])
        self.assertEqual(R.compose_card(route), R.compose_card(baseline))


class ClassificationFreshnessTest(SourceBase):
    def test_display_snapshot_does_not_freeze_selected_cycle_or_writer(self):
        first = self.cycle()
        module = sys.modules["artifact_producer"]
        with R.compose_campaign_snapshot():
            with mock.patch.object(module, "read_cycle_record", side_effect=AssertionError("summary cycle census")):
                selection = R.compose_campaign_selection({"artifact_root": str(self.root), "campaign_key": "k163"})
            self.assertEqual(selection["mode"], "join")
            second = self.cycle()
            self.assertNotEqual(first["cycle_id"], second["cycle_id"])
            route = self.compose(campaign_key="k163")
            sources = {s["cycle_id"] for node in route["nodes"]
                       for s in (node.get("input_sources") or {}).values()}
            self.assertEqual(sources, {second["cycle_id"]})
            self.assertEqual(R.compose_campaign_selection(route)["mode"], "join")


class NoSourceRegressionTest(SourceBase):
    def test_implicit_frame_filter_uses_catalog_names_and_borrowed_relocations(self):
        registry = TOPO.load_registry()
        catalog = TOPO.part_catalog(registry)
        frame = catalog["frame"]
        names = set(frame["brief_outputs"])
        names.update(name for brief in frame["briefs"].values() for name in brief["inputs"])
        part_capability = next(iter(catalog["parts"])).partition(":")[0]
        borrowed_name = f"parts/{part_capability}/{frame['aliases'][0]}/{next(iter(names))}"
        found = {name: {"cycle_id": "cyc_" + "1" * 32, "path": name}
                 for name in (*names, borrowed_name, "ordinary/frame-context.md")}
        finder = R._without_implicit_frame_briefs(registry, found.get)
        for name in (*names, borrowed_name):
            self.assertIsNone(finder(name), name)
        self.assertEqual(finder("ordinary/frame-context.md"), found["ordinary/frame-context.md"])

    def test_patch_target_is_the_module_compose_imports(self):
        self.assertIs(P, sys.modules["artifact_producer"])
        self.cycle()
        source = {"cycle_id": "cyc_" + "1" * 32, "path": "campaigns/x/artifacts/plan.md"}
        find = lambda name: dict(source) if name == "plan.md" else None
        with mock.patch.object(P, "input_source_finder", return_value=find) as patched:
            route = self.compose(campaign_key="k163")
        patched.assert_called_once()
        self.assertEqual(self.node(route, "execute")["input_sources"], {"plan.md": source})

    def test_a163_1a_full_graph_is_unchanged(self):
        self.cycle()
        graph = ",".join(n["id"] for n in TOPO.resolve_recipe(
            TOPO.load_registry(), "autopilot-code", "dev")["standard_plus"]["nodes"]
            if n["id"] != "frame-alternative")
        route = self.compose(graph=graph, campaign_key="k163")
        baseline = self.without_finder(graph=graph, campaign_key="k163")
        self.assertTrue(all("input_sources" not in n for n in route["nodes"]))
        self.assertEqual(route["route_hash"], baseline["route_hash"])
        self.assertEqual(R.compose_card(route), R.compose_card(baseline))

    def test_a163_1b_preset_compile_never_asks_for_a_source(self):
        self.cycle()
        with mock.patch.object(P, "input_source_finder", wraps=P.input_source_finder) as spy:
            route = T.compile_for("standard", self.root, campaign_key="k163")
        spy.assert_not_called()
        self.assertTrue(all("input_sources" not in n for n in route["nodes"]))

    def test_a163_1c_partial_graph_without_a_source_is_unchanged(self):
        self.cycle()
        self.assert_not_found(campaign_key="brand-new-stream")

    def test_a163_1d_brief_without_input_sources_is_the_eight_line_template(self):
        route = self.compose(campaign_key="brand-new-stream")
        text, digest = ADVANCE.render_stage_brief(route, self.node(route, "execute"))
        self.assertEqual(text, (
            "capability: autopilot-code\ncapability_mode: dev\nintensity: standard\nunit: dev/backend\n"
            "inputs: task\noutputs: source-diff,dev_logs/**\n"
            "write_scope: source/**,spec/**,checklist.md,dev_logs/**,evidence/**\ncompletion_gate: code-execute\n"))
        self.assertEqual(digest, "sha256:" + hashlib.sha256(
            (ADVANCE.BRIEF_TEMPLATE_ID + "\n" + text).encode("utf-8")).hexdigest())


class SealedSourceTest(SourceBase):
    FRAME_BRIEFS = ("shards/frame/direction-brief.md",
                    "shards/frame-alternative/direction-brief.md")

    def frame_cycle(self):
        return self.cycle(files=self.FRAME_BRIEFS)

    def assert_frame_sources(self, route, cycle):
        node = self.node(route, "plan")
        folder = Path(cycle["cycle_dir"]).relative_to(self.root) / "artifacts"
        expected = {name: {"cycle_id": cycle["cycle_id"], "path": (folder / name).as_posix()}
                    for name in self.FRAME_BRIEFS}
        self.assertEqual(node["input_sources"], expected)

    def test_a_fresh_frameless_graph_drops_campaign_frame_briefs_but_keeps_partial_outputs(self):
        unrelated = self.cycle(files=(*self.FRAME_BRIEFS, "plan.md", "checklist.md"))
        with_campaign = self.compose(graph="plan,execute,test,report", campaign_key="k163")
        self.assertNotIn("input_sources", self.node(with_campaign, "plan"))
        self.assertTrue(all(source["cycle_id"] != unrelated["cycle_id"]
                            for node in with_campaign["nodes"]
                            for source in node.get("input_sources", {}).values()))

        partial = self.compose(graph="execute,test,report", campaign_key="k163")
        sources = {name: source for node in partial["nodes"]
                   for name, source in node.get("input_sources", {}).items()}
        self.assertEqual(sources["plan.md"]["cycle_id"], unrelated["cycle_id"])
        self.assertEqual(sources["checklist.md"]["cycle_id"], unrelated["cycle_id"])

    def test_an_explicit_parent_keeps_its_frame_briefs_ahead_of_unrelated_campaign_briefs(self):
        parent = self.frame_cycle()
        self.frame_cycle()  # Newer campaign history must not replace the selected parent.
        route = self.compose(graph="plan,execute,test,report", campaign_key="k163",
                             parent_cycle_id=parent["cycle_id"])
        self.assert_frame_sources(route, parent)

    def test_route_plan_proposal_keeps_its_frame_briefs_ahead_of_unrelated_campaign_briefs(self):
        parent = self.frame_cycle()
        self.frame_cycle()  # Newer campaign history must not replace the proposal's frame.
        cycle_record = P.read_cycle_record(self.root, parent["cycle_id"])
        decision = RP.build_decision(
            frame_route={"route_id": cycle_record["route_id"], "route_hash": cycle_record["route_hash"],
                         "cycle_id": parent["cycle_id"]},
            selected="proposal", reason="fixture proposal", briefs=[], intent={},
            proposal={"summary": "fixture", "legs": [{"capability": "autopilot-code", "mode": "dev",
                                                          "shape": "staged", "graph": [
                                                              "plan", "execute", "test", "report"]}],
                      "entry_approvals": []},
            first_leg_compose={})
        record_path = self.root / "shards" / "frame" / "route-decision.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_bytes(RP.render(RP.build_record(decision)))
        binding = RP.read_route_plan("shards/frame/route-decision.json#0", self.root)
        route = self.compose(graph="plan,execute,test,report", campaign_key="k163",
                             parent_cycle_id=parent["cycle_id"], route_plan=binding)
        self.assertEqual(route["route_plan"], RP.sealed_form(binding))
        self.assert_frame_sources(route, parent)

    def test_a163_2_parent_cycle_fills_inputs_card_brief_and_reproduces(self):
        prior = self.cycle()
        route = self.compose(parent_cycle_id=prior["cycle_id"])
        folder = Path(prior["cycle_dir"]).relative_to(self.root) / "artifacts"
        expected = {name: {"cycle_id": prior["cycle_id"], "path": (folder / name).as_posix()}
                    for name in ("plan.md", "checklist.md")}
        for node_id in ("execute", "test", "report"):
            node = self.node(route, node_id)
            self.assertLessEqual({"plan.md", "checklist.md"}, set(node["inputs"]), node_id)
            self.assertEqual(node["input_sources"], expected, node_id)
        R.verify_route(route, R.ROOT)
        card = R.compose_card(route)
        lines = [line for line in card.splitlines() if line.startswith("  입력 ")]
        self.assertEqual(lines, [f"  입력 plan.md·checklist.md ← {prior['cycle_id']} "
                                 f"{Path(prior['cycle_dir']) / 'artifacts'}"])
        execute = self.node(route, "execute")
        text, _digest = ADVANCE.render_stage_brief(route, execute)
        self.assertEqual(text.splitlines()[-1],
                         f"input_sources: plan.md={expected['plan.md']['path']},"
                         f"checklist.md={expected['checklist.md']['path']}")
        self.assertTrue(R._versioned_subgraph(TOPO.load_registry(), route["composed_recipe"]))
        self.assertEqual(R._continuation_node_projection(execute, [])["input_sources"], expected)
        baseline = self.without_finder(campaign_key="brand-new-stream")
        for field in ("model_profile", "profile_selection", "profile_demand"):
            self.assertEqual(self.node(route, "execute").get(field), self.node(baseline, "execute").get(field))

    def test_a_plan_check_without_its_plan_node_reviews_the_sealed_earlier_plan(self):
        prior = self.cycle()
        route = self.compose(graph="plan-check,execute,test,report", parent_cycle_id=prior["cycle_id"])
        check = self.node(route, "plan-check")
        self.assertIn("plan.md", check["input_sources"])
        import review_input
        candidate = review_input.resolve_input(route, check, self.root / "jobs.log")      # no --reviewed-evidence
        self.assertEqual(candidate["path"], str((Path(prior["cycle_dir"]) / "artifacts" / "plan.md").resolve()))

    def test_a163_3a_campaign_latest_cycle_is_the_source(self):
        self.cycle(files=("a/plan.md",))
        latest = self.cycle(files=("plan.md",))
        route = self.compose(campaign_key="k163")
        source = self.node(route, "execute")["input_sources"]["plan.md"]
        self.assertEqual(source["cycle_id"], latest["cycle_id"])
        self.assertTrue(source["path"].endswith("/artifacts/plan.md"))
        self.assertIn(Path(latest["cycle_dir"]).name, source["path"])

    def test_a163_3b_latest_cycle_without_the_file_does_not_fall_back(self):
        self.cycle(files=("plan.md",))
        self.cycle(files=("notes.md",))
        self.assert_not_found(campaign_key="k163")

    def test_unassigned_compose_has_no_source(self):
        self.cycle()
        self.assert_not_found(unassigned=True)

    def test_a163_4f_directory_input_is_sealed(self):
        prior = self.cycle(files=("dev_logs/execute.md", "test_logs/run.md"))
        route = self.compose(graph="report", parent_cycle_id=prior["cycle_id"])
        node = self.node(route, "report")
        self.assertEqual(node["inputs"], ["dev_logs/**", "test_logs/**"])
        folder = (Path(prior["cycle_dir"]).relative_to(self.root) / "artifacts").as_posix()
        self.assertEqual(node["input_sources"]["dev_logs/**"]["path"], folder + "/dev_logs")
        self.assertEqual(node["input_sources"]["test_logs/**"]["path"], folder + "/test_logs")


class LookupLimitsTest(SourceBase):
    def find(self, prior, name="plan.md"):
        return P.input_source_finder(self.root, parent_cycle_id=prior["cycle_id"])(name)

    def test_a163_4a_shortest_path_then_lexicographic(self):
        prior = self.cycle(files=("b/plan.md", "a/plan.md"))
        self.assertTrue(self.find(prior)["path"].endswith("/artifacts/a/plan.md"))
        self.write_output(prior, rel="plan.md")
        self.assertTrue(self.find(prior)["path"].endswith("/artifacts/plan.md"))
        # Character length decides, not directory depth: deeper `a/b/plan.md` (11) beats `zzzzzz/plan.md` (14).
        other = self.cycle(files=("zzzzzz/plan.md", "a/b/plan.md"))
        self.assertTrue(self.find(other)["path"].endswith("/artifacts/a/b/plan.md"))

    def test_a163_4b_symlinks_are_not_found(self):
        prior = self.cycle(files=("real.md", "elsewhere/plan.md"))
        artifacts = Path(prior["cycle_dir"]) / "artifacts"
        (artifacts / "plan.md").symlink_to(artifacts / "real.md")
        (artifacts / "linked").symlink_to(artifacts / "elsewhere")
        (artifacts / "elsewhere" / "plan.md").rename(artifacts / "elsewhere" / "other.md")
        self.assertIsNone(self.find(prior))
        self.assert_not_found(parent_cycle_id=prior["cycle_id"])

    def test_a163_4c_artifacts_folder_outside_the_root_is_not_found(self):
        prior = self.cycle()
        artifacts = Path(prior["cycle_dir"]) / "artifacts"
        outside = Path(self._tmp.name) / "outside"
        shutil.move(str(artifacts), str(outside))
        artifacts.symlink_to(outside)
        self.assertIsNone(self.find(prior))
        self.assert_not_found(parent_cycle_id=prior["cycle_id"])

    def test_a163_4d_caps_mean_not_found(self):
        prior = self.cycle(files=("plan.md", "one.md", "two.md", "three.md"))
        with mock.patch.object(P, "INPUT_SOURCE_MAX_ENTRIES", 3):
            self.assertIsNone(self.find(prior))
        self.assertIsNotNone(self.find(prior))
        many = self.cycle(files=("a/plan.md", "b/plan.md"))
        with mock.patch.object(P, "INPUT_SOURCE_MAX_CANDIDATES", 1):
            self.assertIsNone(self.find(many))
        deep = self.cycle(files=("a/b/c/plan.md",))
        with mock.patch.object(P, "INPUT_SOURCE_MAX_DEPTH", 1):
            self.assertIsNone(self.find(deep))
        self.assertIsNotNone(self.find(deep))

    def test_a163_4e_unreadable_records_and_errors_are_not_found(self):
        prior = self.cycle()
        real_scandir = os.scandir

        def deny(path, *args, **kwargs):
            if "artifacts" in str(path):
                raise PermissionError(13, "denied")
            return real_scandir(path, *args, **kwargs)

        with mock.patch.object(P.os, "scandir", side_effect=deny):
            self.assert_not_found(parent_cycle_id=prior["cycle_id"])
        P.cycle_record_path(self.root, prior["cycle_id"]).write_text("{broken", encoding="utf-8")
        self.assert_not_found(parent_cycle_id=prior["cycle_id"])
        self.assert_not_found(campaign_key="a-stream-with-no-record")

    def test_a163_4g_only_plain_paths_are_targets(self):
        self.assertEqual(P._input_target("plan.md"), (("plan.md",), False))
        self.assertEqual(P._input_target("reviews/smoke-attestation.json"),
                         (("reviews", "smoke-attestation.json"), False))
        self.assertEqual(P._input_target("dev_logs/**"), (("dev_logs",), True))
        for name in ("designs/<cycle>/…", "spec/<component>/x.md", "", "/plan.md", "a/../b", "./a", "a//b",
                     "**", "/**", "report/*.md", None):
            self.assertIsNone(P._input_target(name), name)


class ParentOutputEnvTest(SourceBase):
    def env(self, route_file, start=True):
        return P.prepare_route_artifact_env(route_file, start=start, jobs=self.jobs)

    def test_a163_5a_parent_cycle_names_its_output_dir(self):
        prior = self.cycle()
        _route, route_file = self.route("direct", slug="child", parent_cycle_id=prior["cycle_id"])
        env = self.env(route_file)
        self.assertEqual(env[PARENT_VAR], str((Path(prior["cycle_dir"]) / "artifacts").resolve()))
        self.assertEqual(list(env)[:6], SIX)
        record = P.route_cycle_for(self.root, P.load_route(self.root, route_file))
        self.assertEqual(env["AGENT_ARTIFACT_CYCLE_ID"], record["cycle_id"])
        self.assertEqual(env["AGENT_ARTIFACT_OUTPUT_DIR"],
                         str(P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record) / "artifacts"))
        self.assertEqual(env, self.env(route_file, start=False))

    def test_a163_5b_campaign_previous_cycle_else_no_variable(self):
        first = self.cycle()
        _route, route_file = self.route("direct", slug="second", campaign_key="k163")
        fresh_file = self.route("direct", slug="first-of-fresh", campaign_key="fresh")[1]
        self.assertNotIn(PARENT_VAR, self.env(fresh_file))
        second = self.env(route_file)
        self.assertEqual(second[PARENT_VAR], str((Path(first["cycle_dir"]) / "artifacts").resolve()))
        # The campaign's first cycle has no predecessor.
        record = P.read_cycle_record(self.root, first["cycle_id"])
        self.assertNotIn(PARENT_VAR, P._env_for(self.root, record))
        # The degraded `_unassigned` container asserts no shared goal.
        for slug in ("loose-1", "loose-2"):
            env = self.env(self.route("direct", slug=slug)[1])
            self.assertNotIn(PARENT_VAR, env)

    def test_a163_5c_priority_parent_then_input_sources_then_campaign(self):
        c1, c2 = self.cycle(), self.cycle()
        c3 = self.cycle()
        record = P.read_cycle_record(self.root, c3["cycle_id"])
        artifacts = lambda cycle: str((Path(cycle["cycle_dir"]) / "artifacts").resolve())
        sealed = {"nodes": [{"input_sources": {"plan.md": {"cycle_id": c1["cycle_id"], "path": "x"}}}]}
        self.assertEqual(P._env_for(self.root, record)[PARENT_VAR], artifacts(c2))
        self.assertEqual(P._env_for(self.root, record, sealed)[PARENT_VAR], artifacts(c1))
        with_parent = {**record, "parent_cycle_id": c2["cycle_id"]}
        self.assertEqual(P._env_for(self.root, with_parent, sealed)[PARENT_VAR], artifacts(c2))

    def test_a163_5d_staged_owner_launch_env_has_the_variable(self):
        prior = self.cycle()
        route = self.compose(campaign_key="k163")
        from artifact_lifecycle import admit_runtime_route
        route_file = Path(admit_runtime_route(self.root, route).route_file)
        env = self.env(route_file)
        self.assertEqual(env[PARENT_VAR], str((Path(prior["cycle_dir"]) / "artifacts").resolve()))

    def test_a163_5e_closed_env_list_is_untouched(self):
        self.assertEqual(TOPO.PRODUCER_LIFECYCLE_ENV, [
            "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_PRODUCER_ID",
            "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR"])
        TOPO.validate_registry(TOPO.load_registry())

    def test_a163_5f_owner_launch_drops_a_stale_inherited_variable(self):
        """A leftover value from an earlier route must not reach an owner with no source."""
        holder = OWNER_T.RouteDefaultsReceiptTest()
        path = holder._quick_route()
        jobs = path.parent / "jobs.log"
        jobs.touch()
        binding = SimpleNamespace(route_file=str(path), route_id="rt-x", route_hash="sha256:x",
                                  route_node="one-shot", registry_digest="sha256:r",
                                  write_scope="source-scoped", completion_gate="quick-complete")
        seen = []
        OWNER = OWNER_T.OWNER
        # Keep the mocked version probe independent of the host's CLI path.
        stale = {"AGENT_DISPATCH_JOBS": str(jobs), PARENT_VAR: "/stale/other-route/artifacts",
                 "OPENCODE_BIN": "opencode"}
        with mock.patch.object(OWNER.subprocess, "run", side_effect=lambda cmd, **kw: (
                seen.append((cmd, kw.get("env"))), SimpleNamespace(returncode=0))[1]), \
             mock.patch.object(OWNER, "_usage", return_value={"claude": "ok", "codex": "ok", "opencode": "ok"}), \
             mock.patch.object(OWNER._capacity, "capacity_scores",
                               return_value={"claude": 80.0, "codex": 80.0, "opencode": 80.0}), \
             mock.patch.object(OWNER, "derive_quick_owner_binding", return_value=binding), \
             mock.patch("artifact_producer.prepare_route_artifact_env", return_value={}), \
             mock.patch.dict(os.environ, OWNER_T._isolated_env(stale), clear=True), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(OWNER.main(["--dry-run", "--route-evidence", str(path), "--prompt-text", "probe"]), 0)
        version_probes = [(cmd, env) for cmd, env in seen
                          if isinstance(cmd, (list, tuple)) and "--version" in cmd]
        dispatch_calls = [(cmd, env) for cmd, env in seen
                          if not (isinstance(cmd, (list, tuple)) and "--version" in cmd)]
        self.assertTrue(all(cmd == ["opencode", "--version"] for cmd, _env in version_probes))
        self.assertEqual(len(dispatch_calls), 1, [cmd for cmd, _env in seen])
        self.assertEqual(len(seen), len(version_probes) + 1, [cmd for cmd, _env in seen])
        self.assertNotIn(PARENT_VAR, dispatch_calls[0][1])


NATIVE_ENV = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
              "OPENCODE_SESSION_ID")


class SameFlowLedgerTest(SourceBase):
    """A163F-1..4: real route-chain ledger lines and real cycle records, no mocked selector."""

    def setUp(self):
        super().setUp()
        self.ledger_dir = Path(self._tmp.name) / "route-chain"
        self.tools = str(HERE.parent / "tools")
        if self.tools not in sys.path:
            sys.path.insert(0, self.tools)
        from fleet import route_chain
        self.chain = route_chain

    @contextlib.contextmanager
    def session(self, sid=None, depth=None):
        """This process as one declared native session (or none); everything else inherited."""
        env = {k: v for k, v in os.environ.items()
               if k not in NATIVE_ENV and not k.startswith("AGENT_DISPATCH_")
               and k != "AGENT_DISPATCH_CHILD"}
        env["FLEET_ROUTE_CHAIN_DIR"] = str(self.ledger_dir)
        if sid:
            env["CLAUDE_CODE_SESSION_ID"] = sid
        if depth is not None:
            env["AGENT_DISPATCH_DEPTH"] = str(depth)
        with mock.patch.dict(os.environ, env, clear=True):
            yield

    def flow_cycle(self, sid, capability="autopilot-code", mode="dev", key="mixed", slug=None):
        """A session composes a direct route (real ledger line) and begins its cycle with a marker plan.md."""
        self.count += 1
        slug = slug or f"flow-{self.count}"
        with self.session(sid):
            route, route_file = self.route("direct", capability, mode, slug=slug, campaign_key=key)
            R._record_route_chain(route, str(route_file), "compose")
            result = P.begin(self.root, route_file=route_file, capability=capability, intensity="direct")
        for rel in ("plan.md", "checklist.md"):
            self.write_output(result, rel=rel, data=f"{slug}\n".encode())
        return result

    def source_cycle(self, route):
        sources = self.node(route, "execute")["input_sources"]
        return {source["cycle_id"] for source in sources.values()}

    def test_a163f_0_the_ledger_line_and_the_cycle_record_share_the_route_id(self):
        y = self.flow_cycle("sess-A")
        record = P.read_cycle_record(self.root, y["cycle_id"])
        with self.session("sess-A"):
            lines = self.chain.read_tail("claude", "sess-A")
        self.assertEqual([line["event"] for line in lines], ["compose"])
        self.assertEqual(lines[0]["route_id"], record["route_id"])
        self.assertEqual(lines[0]["campaign_key"], "mixed")

    def test_a163f_1_another_sessions_newer_cycle_loses_to_this_sessions_older_one(self):
        y = self.flow_cycle("sess-A")
        x = self.flow_cycle("sess-B")
        with self.session("sess-A"):
            route = self.compose(campaign_key="mixed")
        self.assertEqual(self.source_cycle(route), {y["cycle_id"]})
        # control: a session with no ledger gets the newest cycle of the capability
        with self.session("sess-C"):
            self.assertEqual(self.source_cycle(self.compose(campaign_key="mixed")), {x["cycle_id"]})
        with self.session(None):
            self.assertEqual(self.source_cycle(self.compose(campaign_key="mixed")), {x["cycle_id"]})

    def test_a163f_1b_the_sessions_own_cycle_wins_even_for_another_capability(self):
        y = self.flow_cycle("sess-A")
        self.flow_cycle("sess-B")
        z = self.flow_cycle("sess-A", capability="autopilot-refine", mode="default")
        with self.session("sess-A"):
            route = self.compose(campaign_key="mixed")
        self.assertEqual(self.source_cycle(route), {z["cycle_id"]})
        self.assertNotEqual(z["cycle_id"], y["cycle_id"])

    def test_a163f_1c_a_ledger_line_of_another_campaign_or_root_is_not_this_flow(self):
        self.flow_cycle("sess-A", key="elsewhere")
        x = self.flow_cycle("sess-B")
        with self.session("sess-A"):
            route = self.compose(campaign_key="mixed")
        self.assertEqual(self.source_cycle(route), {x["cycle_id"]})

    def test_a163f_1d_a_continuation_the_session_composed_maps_to_the_cycle_of_its_source_route(self):
        """A ledger line names a route; a continuation's cycle is the one its verified source began."""
        self.count += 1
        with self.session("sess-B"):
            source, source_file = self.route("standard", slug="cont-source", campaign_key="mixed")
            R._record_route_chain(source, str(source_file), "compose")
            began = P.begin(self.root, route_file=source_file, capability="autopilot-code", intensity="standard")
        for rel in ("plan.md", "checklist.md"):
            self.write_output(began, rel=rel, data=b"cont-source\n")
        first = next(n for n in source["nodes"])
        continuation = R.build_continuation_route(
            source, resume_from_node=first["id"], requested_boundary=first["id"],
            reason="same-flow-lineage", artifact_root=self.root)
        continuation_file = R.canonical_route_path(self.root, continuation["route_id"])
        R.publish_continuation_route(continuation, source, continuation_file)
        with self.session("sess-A"):
            R._record_route_chain(continuation, str(continuation_file), "continuation")
        newer = self.flow_cycle("sess-C")
        self.assertNotEqual(newer["cycle_id"], began["cycle_id"])
        with self.session("sess-A"):
            route = self.compose(campaign_key="mixed")
        self.assertEqual(self.source_cycle(route), {began["cycle_id"]})
        with self.session("sess-D"):   # no ledger of its own: the capability rule names the newest code cycle
            self.assertEqual(self.source_cycle(self.compose(campaign_key="mixed")), {newer["cycle_id"]})

    def test_a163f_2_no_session_record_takes_the_same_capability_not_the_newest_cycle(self):
        x = self.flow_cycle("sess-B")
        self.flow_cycle("sess-B", capability="autopilot-refine", mode="default")
        with self.session("sess-C"):
            route = self.compose(campaign_key="mixed")
        self.assertEqual(self.source_cycle(route), {x["cycle_id"]})

    def test_a163f_2b_no_same_capability_cycle_means_no_source_and_the_old_bytes(self):
        self.flow_cycle("sess-B", capability="autopilot-refine", mode="default")
        with self.session("sess-C"):
            self.assert_not_found(campaign_key="mixed")
        with self.session("sess-B"):
            # the same campaign, but sess-B composed the refine cycle itself: its own flow
            route = self.compose(campaign_key="mixed")
        self.assertEqual(self.source_cycle(route), {P.list_cycle_records(self.root)[0]["cycle_id"]})

    def test_a163f_2c_an_unreadable_ledger_falls_to_the_capability_rule(self):
        self.flow_cycle("sess-A")
        x = self.flow_cycle("sess-B")
        with self.session("sess-A"):
            ledger = Path(self.chain.ledger_path("claude", "sess-A"))
        ledger.write_text("{broken\n", encoding="utf-8")
        with self.session("sess-A"):
            route = self.compose(campaign_key="mixed")
        self.assertEqual(self.source_cycle(route), {x["cycle_id"]})
        ledger.unlink()
        with self.session("sess-A"):
            self.assertEqual(self.source_cycle(self.compose(campaign_key="mixed")), {x["cycle_id"]})

    def test_a163f_2d_a_partial_graph_with_no_source_is_byte_identical_to_the_old_compose(self):
        self.flow_cycle("sess-B", capability="autopilot-refine", mode="default")
        with self.session("sess-C"):
            route = self.compose(campaign_key="mixed")
            baseline = self.without_finder(campaign_key="mixed")
        self.assertEqual(route["route_hash"], baseline["route_hash"])
        self.assertEqual(json.dumps(route, sort_keys=True), json.dumps(baseline, sort_keys=True))
        self.assertEqual(self.inputs(route), FRESH)

    def test_a163f_3_parent_cycle_beats_the_session_rule(self):
        self.flow_cycle("sess-A")
        x = self.flow_cycle("sess-B")
        with self.session("sess-A"):
            route = self.compose(parent_cycle_id=x["cycle_id"])
        self.assertEqual(self.source_cycle(route), {x["cycle_id"]})

    def test_a163f_4_the_parent_output_env_follows_the_same_rule_at_start(self):
        y = self.flow_cycle("sess-A")
        x = self.flow_cycle("sess-B")
        with self.session("sess-A"):
            route, route_file = self.route("direct", slug="next-a", campaign_key="mixed")
            R._record_route_chain(route, str(route_file), "compose")
            env = P.prepare_route_artifact_env(route_file, start=True, jobs=self.jobs)
        self.assertEqual(env[PARENT_VAR], str((Path(y["cycle_dir"]) / "artifacts").resolve()))
        # a route composed by nobody we can see falls to the capability rule: the newest code
        # cycle before it, which is now sess-A's `next-a` -- not sess-A's older Y by session
        with self.session("sess-C"):
            other, other_file = self.route("direct", slug="next-c", campaign_key="mixed")
            env = P.prepare_route_artifact_env(other_file, start=True, jobs=self.jobs)
        record = P.read_cycle_record(self.root, env["AGENT_ARTIFACT_CYCLE_ID"])
        previous = P.read_campaign(self.root, record["campaign_id"])["cycles"][-2]
        self.assertNotIn(previous, (y["cycle_id"], x["cycle_id"]))
        self.assertEqual(env[PARENT_VAR], str(P._cycle_artifacts_dir(self.root, previous)))

    def test_a163f_4b_the_composing_anchor_is_the_same_whichever_process_prepares_the_env(self):
        """Start (depth 0), an owner launch, a worker and a bare supervisor all read the composing session."""
        y = self.flow_cycle("sess-A")
        self.flow_cycle("sess-B")
        with self.session("sess-A"):
            route, route_file = self.route("direct", slug="next-a", campaign_key="mixed")
            R._record_route_chain(route, str(route_file), "compose")
        expected = str((Path(y["cycle_dir"]) / "artifacts").resolve())
        seen = {}
        for label, sid, depth in (("start", "sess-A", None), ("owner-launch", "sess-A", 0),
                                  ("worker-session", "worker-D", 1), ("bare-supervisor", None, None),
                                  ("other-session", "sess-B", None)):
            with self.session(sid, depth):
                seen[label] = P.prepare_route_artifact_env(route_file, start=True, jobs=self.jobs)[PARENT_VAR]
        self.assertEqual(seen, {label: expected for label in seen})
        with self.session("worker-D", 1):
            self.assertIsNone(self.chain.writer_identity())
            self.assertEqual(self.chain.composing_anchor(route["route_id"]), ("claude", "sess-A"))

    def test_a163f_4d_an_older_composing_ledger_is_found_past_more_than_sixty_four_newer_unrelated_ones(self):
        """The composing session is found by the route's own creation time, not by how many ledgers are newer."""
        y = self.flow_cycle("sess-A")
        self.flow_cycle("sess-B")                      # a newer same-capability cycle: the wrong answer if the anchor is missed
        with self.session("sess-A"):
            route, route_file = self.route("direct", slug="next-a", campaign_key="mixed")
            R._record_route_chain(route, str(route_file), "compose")
        now = time.time()
        os.utime(route_file, (now - 2000, now - 2000))                         # the route was written long ago
        with self.session("sess-A"):
            os.utime(self.chain.ledger_path("claude", "sess-A"), (now - 1000, now - 1000))
        unrelated = {"route_id": "rt-" + "9" * 32, "artifact_root": str(self.root), "campaign_key": "mixed"}
        for n in range(self.chain.ANCHOR_SCAN_FILES + 6):
            sid = f"busy-{n}"
            with self.session(sid):
                self.assertTrue(self.chain.append("claude", sid, self.chain.build_line(
                    unrelated, event="compose", harness="claude", session_id=sid, route_file="/x/route.json")))
        expected = str((Path(y["cycle_dir"]) / "artifacts").resolve())
        with self.session(None):
            self.assertEqual(P.prepare_route_artifact_env(route_file, start=True, jobs=self.jobs)[PARENT_VAR], expected)

    def test_a163f_4c_two_sessions_claiming_one_route_and_a_start_only_line_give_no_anchor(self):
        route = {"route_id": "rt-" + "1" * 32, "artifact_root": str(self.root), "campaign_key": "mixed"}
        for sid, event in (("sess-A", "compose"), ("sess-B", "compose"), ("sess-C", "start")):
            with self.session(sid):
                line = self.chain.build_line(route, event=event, harness="claude", session_id=sid,
                                             route_file="/x/route.json")
                self.assertTrue(self.chain.append("claude", sid, line))
        with self.session("sess-C"):
            self.assertIsNone(self.chain.composing_anchor(route["route_id"]))
        lone = {**route, "route_id": "rt-" + "2" * 32}
        with self.session("sess-A"):
            self.assertTrue(self.chain.append("claude", "sess-A", self.chain.build_line(
                lone, event="start", harness="claude", session_id="sess-A", route_file="/x/route.json")))
            self.assertIsNone(self.chain.composing_anchor(lone["route_id"]))


if __name__ == "__main__":
    unittest.main()
