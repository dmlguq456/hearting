#!/usr/bin/env python3
"""2026-11-27: a stage the person selected at `top` that runs below top keeps going, and the lines that
already say how it ran carry `tier_note=selected top, ran <what ran>`.

Covered here: the shared rule (`model_profile.tier_note`), each way down in the stage launcher (another
harness, native-subagent, inline, an owner-inline repeat, a harness whose config declares no top), a
batch leg, `workflow-supervisor status` and the completion summary the route's parent receives. A stage
not selected at top, or one that ran a top model, prints what it printed before. Fixtures never launch
a model: the launcher's wrapper is a fixture or a real `--dry-run`."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import model_profile as P  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The stage launcher's own fixture (route compile, registry parent, in-process chain); only its helpers
# are bound below, so its tests do not run again here.
T = load("stage_dispatch_fallback_fixture", "utilities/stage_dispatch_fallback.test.py")
F = T.F
BATCH = load("dispatch_batch_tier_note", "utilities/dispatch-batch.py")
SUPERVISOR = load("workflow_supervisor_tier_note", "utilities/workflow-supervisor.py")
import work_intent  # noqa: E402
import dispatch_terminal_commit as COMMIT  # noqa: E402


def setUpModule():
    T.setUpModule()


def tearDownModule():
    T.tearDownModule()


def stage(node_id="impl-review", profile="top", kind="review-worker", depth=2):
    return {"id": node_id, "kind": kind, "dispatch_depth": depth, "model_profile": profile}


class SharedRule(unittest.TestCase):
    def test_the_note_names_what_ran_and_only_below_top(self):
        top = stage()
        self.assertEqual(P.tier_note(top, harness="opencode", tier="balanced-deep"),
                         "selected top, ran opencode balanced-deep")
        self.assertEqual(P.tier_note(top, harness="codex", surface="native"), "selected top, ran codex native-subagent")
        self.assertEqual(P.tier_note(top, surface="inline", owner_harness="claude", owner_tier="deep"),
                         "selected top, ran inline (owner claude deep)")
        # A top model ran: the selected tier was kept, so nothing is said.
        for tier in ("top", "model/fixture-named-top"):
            self.assertEqual(P.tier_note(top, harness="claude", tier=tier), "")
        self.assertEqual(P.tier_note(top, surface="inline", owner_harness="claude", owner_tier="top"), "")
        # Not a person-selected stage top: an ordinary stage, a depth-1 owner, a frame leg, a fan-out leg.
        for node in (stage(profile="deep"), stage(kind="capability-owner", depth=1), {**stage(), "worker_type": "frame",
                     "dispatch_depth": 1}, stage(kind="map-worker")):
            with self.subTest(node=node):
                self.assertEqual(P.tier_note(node, harness="opencode", tier="balanced-deep"), "")
                self.assertEqual(P.tier_note(node, harness="codex", surface="native"), "")
        self.assertEqual(P.tier_note_ran("selected top, ran opencode balanced-deep"), "opencode balanced-deep")
        self.assertEqual(P.tier_note_ran("ran opencode"), "")

    def test_the_shipped_adapter_configs_say_which_harness_runs_top_below_top(self):
        # OpenCode declares its top on the balanced-deep tier; an older whole-file copy without the key
        # collapses the same way. Claude and Codex resolve top to their top tier.
        expected = {"claude": "", "codex": "", "opencode": "selected top, ran opencode balanced-deep"}
        for adapter, note in expected.items():
            with self.subTest(adapter=adapter):
                config = P.load_config(ROOT / "adapters" / adapter / "config" / "models.conf")
                resolved = P.resolve_profile_values(adapter, config, "top")
                self.assertEqual(P.tier_note(stage(), harness=adapter, tier=resolved["tier"]), note)
        older = {key: value for key, value in P.load_config(ROOT / "adapters/opencode/config/models.conf").items()
                 if not key.startswith("CFG_MODEL_PROFILE_TOP") and key != "CFG_MODEL_PROFILE_GRANULARITY_TOP"}
        resolved = P.resolve_profile_values("opencode", older, "top")
        self.assertEqual(resolved["granularity"], "collapsed-top-to-balanced-deep")
        self.assertEqual(P.tier_note(stage(), harness="opencode", tier=resolved["tier"]),
                         "selected top, ran opencode balanced-deep")

    def test_the_route_reader_keeps_only_this_routes_selected_top_stages_that_ran_below(self):
        route = {"route_id": "rt-a", "route_hash": "sha256:a",
                 "nodes": [stage("impl-review"), stage("plan", profile="deep", kind="pipeline-stage")]}
        row = {"route_id": "rt-a", "route_hash": "sha256:a", "route_node": "impl-review", "attempt_id": "att-1",
               "harness": "opencode", "model_tier": "balanced-deep", "launch_started": "1"}
        rows = [row,
                {**row, "attempt_id": "att-top", "harness": "claude", "model_tier": "top"},        # ran top
                {**row, "attempt_id": "att-plan", "route_node": "plan"},                          # not selected
                {**row, "attempt_id": "att-other", "route_hash": "sha256:b"},                     # another route
                {**row, "attempt_id": "att-unstarted", "launch_started": "0"}]                    # never ran
        ledger = [{"route_id": "rt-a", "route_hash": "sha256:a", "route_node": "impl-review", "fallback_hop": "inline",
                   "tier_note": "selected top, ran inline (owner claude deep)"},
                  {"route_id": "rt-a", "route_hash": "sha256:a", "route_node": "impl-review", "fallback_hop": "inline"},
                  {"route_id": "rt-a", "route_hash": "sha256:b", "route_node": "impl-review", "fallback_hop": "inline",
                   "tier_note": "selected top, ran inline (owner claude deep)"}]
        self.assertEqual(P.top_stage_tier_notes(route, rows=rows, degradations=ledger), [
            {"node": "impl-review", "attempt_id": "att-1", "tier_note": "selected top, ran opencode balanced-deep"},
            {"node": "impl-review", "fallback_hop": "inline", "tier_note": "selected top, ran inline (owner claude deep)"},
        ])
        plain = {**route, "nodes": [stage("impl-review", profile="deep")]}
        self.assertEqual(P.top_stage_tier_notes(plain, rows=rows, degradations=ledger), [])


class StageLauncher(unittest.TestCase):
    """The stage fallback chain on the fixture `plan` stage, sealed `top` by the person at strong."""
    # The fallback fixture's own setup and helpers.
    setUp = T.FallbackTest.setUp
    tearDown = T.FallbackTest.tearDown
    seed_parent = T.FallbackTest.seed_parent
    fixture_subprocess_env = T.FallbackTest.fixture_subprocess_env
    tuple = T.FallbackTest.tuple
    cli_verdict = T.FallbackTest.cli_verdict
    launch_roots_env = T.FallbackTest.launch_roots_env
    route = T.FallbackTest.route
    seed_predecessor_markers = T.FallbackTest.seed_predecessor_markers
    dispatch_env = T.FallbackTest.dispatch_env
    run_inline = T.FallbackTest.run_inline
    degradation_rows = T.FallbackTest.degradation_rows
    run_chain = T.FallbackTest.run_chain

    OPENCODE = ("check=ok\nadapter=opencode\nstatus=start\nmodel_source=profile\nmodel_profile=top\n"
                "model_tier=balanced-deep\nprofile_granularity=collapsed-top-to-balanced-deep\nmodel=fixture-model\n")
    CLAUDE_TOP = "check=ok\nadapter=claude\nstatus=start\nmodel_profile=top\nmodel_tier=top\nmodel=fixture-model\n"
    REFUSED = "check=failed\nreason=fixture-runtime-refusal\n"
    UNDECLARED = "check=failed\nreason=profile-top-undeclared\nchild_spawned=0\n"

    def hop(self, ordinal, kind, harness=None):
        if kind == "inline":
            return {"ordinal": ordinal, "fallback_hop": "inline", "reason_enum": "runtime-unavailable"}
        if kind == "native":
            return {"ordinal": ordinal, "fallback_hop": "native-subagent",
                    "candidates": [{"harness": harness, "status": "supported"}]}
        return {"ordinal": ordinal, "fallback_hop": kind, "candidates": [self.tuple(harness, "supported")]}

    def chain(self, profile, hops, outputs, *, proof=None, patches=()):
        """One in-process chain; returns (exit, stdout, ledger rows this chain wrote, route)."""
        with self.dispatch_env():
            path = self.route(same_status="supported", explicit_profiles={"plan": profile} if profile else None)
            before = len(self.degradation_rows(path))
            results = [SimpleNamespace(returncode=0 if text.startswith("check=ok") else 64, stdout=text, stderr="")
                       for text in outputs]
            out = io.StringIO()
            with mock.patch.object(F, "ordered_fallback_hops", return_value=(hops, None)), \
                 mock.patch.object(F, "native_child_proof", return_value=proof), \
                 mock.patch.object(F, "run_wrapper", side_effect=results), contextlib.ExitStack() as stack, \
                 contextlib.redirect_stdout(out):
                for patch in patches:
                    stack.enter_context(patch)
                code, _ = self.run_inline(path)
            return code, out.getvalue(), self.degradation_rows(path)[before:], json.loads(path.read_text())

    @staticmethod
    def own_block(stdout):
        """The launcher's own lines (a relayed wrapper receipt follows the second `check=`)."""
        lines = stdout.splitlines()
        starts = [index for index, line in enumerate(lines) if line.startswith("check=")]
        return lines[:starts[1]] if len(starts) > 1 else lines

    def test_each_way_down_says_what_was_selected_and_what_ran_and_the_work_goes_on(self):
        inline = "selected top, ran inline (owner codex deep)"
        cases = (
            # path, hops, wrapper outputs, native proof, exit, receipt note, ledger reason (None: no ledger row)
            ("another harness: OpenCode resolves top to balanced-deep",
             [self.hop(1, "cross-harness-headless", "opencode")], [self.OPENCODE], None, 0,
             "selected top, ran opencode balanced-deep", None),
            ("native-subagent: the native catalog has no top",
             [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "native", "codex")], [self.REFUSED],
             "fixture-native-proof", 78, "selected top, ran codex native-subagent", "native-subagent-degraded"),
            ("inline: the owner's own tier",
             [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "inline")], [self.REFUSED], None, 79,
             inline, "runtime-unavailable"),
            ("profile-top-undeclared: the harness is refused and the chain goes down",
             [self.hop(1, "cross-harness-headless", "claude"), self.hop(2, "inline")], [self.UNDECLARED], None, 79,
             inline, "runtime-unavailable"),
        )
        for label, hops, outputs, proof, expected_code, note, reason in cases:
            with self.subTest(path=label):
                code, out, ledger, route = self.chain("top", hops, outputs, proof=proof)
                plan = next(node for node in route["nodes"] if node["id"] == "plan")
                self.assertEqual((plan["model_profile"], route["effective_intensity"]), ("top", "strong"))
                self.assertEqual(code, expected_code, out)
                own = self.own_block(out)
                # One note, on the receipt's own lines, right beside the check it qualifies.
                self.assertEqual(own.count(f"tier_note={note}"), 1, out)
                self.assertEqual(own[0], "check=ok" if code == 0 else "check=degraded")
                if code:
                    self.assertEqual(own[1], f"tier_note={note}")
                else:
                    self.assertEqual(own[own.index("child_harness=opencode") + 1], f"tier_note={note}")
                noted = [(row["reason"], row["tier_note"]) for row in ledger if "tier_note" in row]
                self.assertEqual(noted, [] if reason is None else [(reason, note)])
                if "undeclared" in label:
                    self.assertEqual(json.loads(self.cli_verdict(out)["candidate_outcomes"])[0]["reason"],
                                     "profile-top-undeclared")

    def test_an_owner_inline_repeat_of_a_top_stage_says_the_owner_ran_it(self):
        escalation = {"kind": "owner-inline", "from": "top", "launch_profile": "top", "streak": 2,
                      "class": "scope", "attempts": ["a", "b"]}
        code, out, ledger, _route = self.chain(
            "top", [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "inline")], [],
            patches=(mock.patch("model_profile.repeat_escalation", return_value=escalation),))
        self.assertEqual(code, 79, out)
        self.assertIn("repeat_escalation=owner-inline\n", out)
        self.assertIn("check=degraded\ntier_note=selected top, ran inline (owner codex deep)\nselected_hop=inline\n", out)
        self.assertIn("reason=repeat-failure\n", out)
        self.assertEqual([(row["reason"], row["tier_note"]) for row in ledger if "tier_note" in row],
                         [("repeat-failure", "selected top, ran inline (owner codex deep)")])

    def test_an_owner_running_a_top_model_inline_adds_no_note(self):
        with mock.patch.object(F, "attempt_metadata", return_value={"harness": "claude", "model_tier": "top"}):
            code, out, ledger, _route = self.chain(
                "top", [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "inline")], [self.REFUSED])
        self.assertEqual(code, 79, out)
        self.assertNotIn("tier_note", out)
        self.assertFalse([row for row in ledger if "tier_note" in row])

    def test_no_note_where_top_was_not_selected_or_a_top_model_ran_and_the_bytes_are_unchanged(self):
        cases = (
            ("top ran top on claude", "top", [self.hop(1, "cross-harness-headless", "claude")], [self.CLAUDE_TOP], None),
            ("default profile on opencode", None, [self.hop(1, "cross-harness-headless", "opencode")],
             [self.OPENCODE.replace("model_profile=top", "model_profile=deep")], None),
            ("default profile, native", None,
             [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "native", "codex")], [self.REFUSED],
             "fixture-native-proof"),
            ("default profile, inline", None,
             [self.hop(1, "same-harness-headless", "codex"), self.hop(2, "inline")], [self.REFUSED], None),
        )
        strip = lambda rows: [{key: value for key, value in row.items() if key not in {"ts", "event_id"}}
                              for row in rows]
        for label, profile, hops, outputs, proof in cases:
            with self.subTest(case=label):
                code, out, ledger, route = self.chain(profile, hops, outputs, proof=proof)
                if profile is None:
                    self.assertNotEqual(next(n for n in route["nodes"] if n["id"] == "plan")["model_profile"], "top")
                self.assertNotIn("tier_note", out)
                self.assertFalse([row for row in ledger if "tier_note" in row], ledger)
                # The same chain with the note switched off -- what the launcher printed before it existed.
                with mock.patch.object(F, "tier_note", return_value=""):
                    again, before, ledger_before, _ = self.chain(profile, hops, outputs, proof=proof)
                self.assertEqual((code, out), (again, before))
                self.assertEqual(strip(ledger), strip(ledger_before))

    def test_a_real_dry_run_on_a_harness_band_that_admits_opencode_resolves_top_to_balanced_deep(self):
        # The shipped dispatch defaults keep OpenCode out of the deep band a `top` stage borrows, so a top
        # stage reaches OpenCode only through a user band, a harness pin or an OpenCode owner. Here a user
        # band admits it; the real OpenCode `--dry-run` wrapper resolves the shipped top and the chain says so.
        # A Claude top resolves to its top tier and the chain says nothing.
        shipped = (ROOT / "profiles/dispatch-defaults.yaml").read_text(encoding="utf-8")
        band = "  deep:\n    primary: [claude, codex]\n"
        self.assertEqual(shipped.count(band), 1)
        widened = shipped.replace(band, "  deep:\n    primary: [claude, codex, opencode]\n")
        for harness, note in (("opencode", "tier_note=selected top, ran opencode balanced-deep\n"), ("claude", None)):
            with self.subTest(harness=harness), T.dispatch_defaults_config_text(widened):
                path = self.route(tuples=[self.tuple("codex", "unsupported"), self.tuple(harness, "supported")],
                                  explicit_profiles={"plan": "top"})
                result = self.run_chain(path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"child_harness={harness}\n", result.stdout)
                self.assertIn("model_profile=top\n", result.stdout)
                if note:
                    self.assertIn("model_tier=balanced-deep\n", result.stdout)
                    self.assertIn(f"child_harness={harness}\n{note}", result.stdout)
                else:
                    self.assertIn("model_tier=top\n", result.stdout)
                    self.assertNotIn("tier_note", result.stdout)


class BatchLeg(unittest.TestCase):
    def result(self, node, tier):
        leg = {"node": "impl-review", "adapter": "opencode", "attempt_id": "att-leg", "model_profile": "top"}
        stdout = ("check=ok\nadapter=opencode\nstatus=start\nattempt_id=att-leg\nregistered=1\nstarted=1\n"
                  f"child_spawned=1\nduplicate_attempt=0\nmodel_profile=top\nmodel_tier={tier}\nmodel=fixture\n")
        return BATCH.wrapper_result(leg, SimpleNamespace(returncode=0), stdout, "", node=node)

    def test_a_selected_top_leg_that_ran_below_top_carries_the_note_and_nothing_else_changes(self):
        noted = self.result(stage(), "balanced-deep")
        self.assertEqual(noted["launch_state"], "started")
        self.assertEqual(noted["tier_note"], "selected top, ran opencode balanced-deep")
        baseline = self.result(None, "balanced-deep")
        self.assertNotIn("tier_note", baseline)
        self.assertEqual({key: value for key, value in noted.items() if key != "tier_note"}, baseline)
        for node, tier in ((stage(), "top"), (stage(profile="deep"), "balanced-deep")):
            with self.subTest(node=node["model_profile"], tier=tier):
                self.assertEqual(self.result(node, tier), self.result(None, tier))


class WhereThePersonReads(unittest.TestCase):
    """`workflow-supervisor status --json` and the completion summary the route's parent receives."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        self.jobs.write_text("")
        self.route = {"route_id": "rt-fixture", "route_hash": "sha256:fixture", "capability": "autopilot-code",
                      "nodes": [stage("impl-review"), stage("plan", profile="deep", kind="pipeline-stage")]}
        ledger = self.jobs.parent / "degradations" / "rt-fixture.jsonl"
        ledger.parent.mkdir()
        ledger.write_text(json.dumps({"route_id": "rt-fixture", "route_hash": "sha256:fixture",
                                      "route_node": "impl-review", "fallback_hop": "inline",
                                      "tier_note": "selected top, ran inline (owner claude deep)"}) + "\n")
        self.row = {"route_id": "rt-fixture", "route_hash": "sha256:fixture", "route_node": "impl-review",
                    "attempt_id": "att-leg", "harness": "opencode", "model_tier": "balanced-deep", "launch_started": "1"}

    def status(self, route, rows, *, as_json=True):
        state = {"nodes": {}, "workflow_state": "CREATED", "updated_at": None}
        ledger = mock.Mock()
        ledger.read_only_state.return_value = state
        ledger.claims.return_value = []
        rows = [{"time": "now", "status": "done", "meta": meta} for meta in rows]
        with contextlib.ExitStack() as stack:
            for name, value in [("route_module", SimpleNamespace(resolve_route_argument=lambda *a: "route.json")),
                                ("load_route", route), ("ledger_for", ledger), ("read_armed", {}),
                                ("terminal_gate_state", {}), ("_closed_terminal_projection", False),
                                ("read_resource_watches", []), ("resource_children", []), ("ledger_metadata", {}),
                                ("_registry_rows", rows)]:
                stack.enter_context(mock.patch.object(SUPERVISOR, name, return_value=value))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                SUPERVISOR.cmd_status(SimpleNamespace(route="route.json", jobs=str(self.jobs), json=as_json))
        return json.loads(out.getvalue()) if as_json else out.getvalue()

    def test_status_names_each_selected_top_stage_that_ran_below_and_only_those(self):
        payload = self.status(self.route, [self.row, {**self.row, "attempt_id": "att-top", "model_tier": "top",
                                                      "harness": "claude"}])
        self.assertEqual(payload["tier_notes"], [
            {"node": "impl-review", "attempt_id": "att-leg", "tier_note": "selected top, ran opencode balanced-deep"},
            {"node": "impl-review", "fallback_hop": "inline", "tier_note": "selected top, ran inline (owner claude deep)"},
        ])
        text = self.status(self.route, [self.row], as_json=False)
        self.assertIn("tier       impl-review selected top, ran opencode balanced-deep\n", text)
        # No selected-top stage, or one that ran top: the key and the line are absent, as before.
        plain = {**self.route, "nodes": [stage("impl-review", profile="deep")]}
        self.assertNotIn("tier_notes", self.status(plain, [self.row]))
        self.assertNotIn("tier ", self.status(plain, [self.row], as_json=False))
        with mock.patch("dispatch_degradation.route_rows", return_value=[]):
            self.assertNotIn("tier_notes", self.status(self.route, [{**self.row, "model_tier": "top"}]))

    def test_the_completion_summary_has_one_line_in_the_persons_language(self):
        notes = P.top_stage_tier_notes(self.route, rows=[self.row], degradations=[
            {"route_id": "rt-fixture", "route_hash": "sha256:fixture", "route_node": "impl-review",
             "fallback_hop": "inline", "tier_note": "selected top, ran inline (owner claude deep)"}])
        report = Path(self.tmp.name) / "final_report.md"
        report.write_text("## 5. Unverified\nnone\n")
        for language, line in (
                ("en", "Ran below the selected tier: impl-review (selected top, ran opencode balanced-deep); "
                       "impl-review (selected top, ran inline (owner claude deep))"),
                ("ko", "고른 등급보다 낮게 돈 단계: impl-review (고른 등급 top · 실제 opencode balanced-deep); "
                       "impl-review (고른 등급 top · 실제 inline (owner claude deep))")):
            with self.subTest(language=language), mock.patch.dict(os.environ, {"FLEET_NOW_LANG": language}):
                t = work_intent.ui_translator()
                with_notes = work_intent.completion_summary(self.route, str(report), t=t, tier_notes=notes)
                without = work_intent.completion_summary(self.route, str(report), t=t)
                self.assertIn(line + "\n", with_notes)
                self.assertEqual(with_notes.replace(line + "\n", ""), without)
                self.assertEqual(work_intent.completion_summary(self.route, str(report), t=t, tier_notes=[]), without)

    def test_the_seal_reads_the_routes_registry_rows_and_ledger(self):
        fields = ",".join(f"{key}={value}" for key, value in self.row.items())
        self.jobs.write_text(f"2026-11-27T00:00:00Z\tdone\t/work\t/work\tleg\t{fields}\n")
        request = SimpleNamespace(jobs=str(self.jobs), owner_attempt_id="att-owner")
        self.assertEqual(COMMIT._tier_notes(request, self.route), [
            {"node": "impl-review", "attempt_id": "att-leg", "tier_note": "selected top, ran opencode balanced-deep"},
            {"node": "impl-review", "fallback_hop": "inline", "tier_note": "selected top, ran inline (owner claude deep)"},
        ])
        plain = {**self.route, "nodes": [stage("impl-review", profile="deep")]}
        self.assertEqual(COMMIT._tier_notes(request, plain), [])


if __name__ == "__main__":
    unittest.main()
