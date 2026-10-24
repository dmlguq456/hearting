#!/usr/bin/env python3
"""SD-66 fix-forward: dispatch-node.py record -> wrapper-argument binding."""
import contextlib
import importlib.util
import io
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
P = Path(__file__).with_name("dispatch-node.py")
S = importlib.util.spec_from_file_location("dispatch_node", P)
N = importlib.util.module_from_spec(S)
S.loader.exec_module(N)
import route_identity as ROUTE_IDENTITY
REVIEW_ROUND_CAP = N.REVIEW_ROUND_CAP
_FIXTURE_TEMP = tempfile.TemporaryDirectory(prefix="hearting-dispatch-node-")
_FIXTURE_ROOT = Path(_FIXTURE_TEMP.name)
FIXTURE_WORKTREE = _FIXTURE_ROOT / "fixture-worktree"
FIXTURE_WORKTREE.mkdir()
subprocess.run(["git", "init", "-q", str(FIXTURE_WORKTREE)], check=True)
subprocess.run(["git", "-C", str(FIXTURE_WORKTREE), "config", "user.email", "fixture@example.com"], check=True)
subprocess.run(["git", "-C", str(FIXTURE_WORKTREE), "config", "user.name", "Fixture"], check=True)
(FIXTURE_WORKTREE / "fixture.py").write_text("fixture = True\n", encoding="utf-8")
subprocess.run(["git", "-C", str(FIXTURE_WORKTREE), "add", "fixture.py"], check=True)
subprocess.run(["git", "-C", str(FIXTURE_WORKTREE), "commit", "-qm", "fixture"], check=True)
FIXTURE_HEAD = subprocess.check_output(["git", "-C", str(FIXTURE_WORKTREE), "rev-parse", "HEAD"], text=True).strip()
FIXTURE_LINKED = _FIXTURE_ROOT / "fixture-linked-worktree"
subprocess.run(["git", "-C", str(FIXTURE_WORKTREE), "worktree", "add", "-q", "--detach",
                str(FIXTURE_LINKED), FIXTURE_HEAD], check=True)


def seal_route_hash(route):
    """Recompute `route_hash` after a fixture mutates the route dict.

    SD-155 (`verified_route_lineage`) recomputes and checks the route's own
    hash even for a single-generation lineage, so a fixture's `route_hash`
    must match its final field values -- including a post-construction
    `effective_intensity` override -- once a review-worker node's round
    census walks the verified lineage (`prior_round_attempts(..., route=...)`).
    """
    route["route_hash"] = ROUTE_IDENTITY.route_hash(route)
    return route


def base_tuple(child_harness, status="supported", probe_source="fixture-check", failure_class="", parent=None):
    row = {
        "child_harness": child_harness,
        "checked_worktree": str(FIXTURE_WORKTREE),
        "codex_command": "ok" if child_harness == "codex" else "not-applicable",
        "failure_class": failure_class,
        "failure_scope": "none" if status == "supported" else "runtime-global",
        "launch_authority": "conductor",
        "parent_harness": "claude",
        "parent_sandbox": "default",
        "parent_transport": "headless",
        "probe_source": probe_source,
        "probe_time": "2026-07-17T00:00:00Z",
        "status": status,
        "retry_on_isolated_worktree": 0,
    }
    if parent:
        row.update(parent)
    return row


def make_fallback(claude=None, codex=None, opencode=None):
    fallback = [{"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [claude or base_tuple("claude")]}]
    cross = [c for c in (codex, opencode) if c is not None] or [base_tuple("codex"), base_tuple("opencode")]
    fallback.append({"ordinal": 2, "fallback_hop": "cross-harness-headless", "candidates": cross})
    return fallback


def fixture_state_environ(tmpdir):
    """The minimum env a scrubbed (`clear=True`) run still has to declare.

    `state_root()` reads only the mapping it is handed and fail-closes when none
    of HARNESS_STATE_ROOT/XDG_STATE_HOME/HOME is set (SD-112 §13.33.2-(1)), so a
    literal `{}` is not "an isolated environment", it is an unresolvable one. A
    fixture-owned HARNESS_STATE_ROOT keeps the isolation these tests are about --
    nothing ambient leaks in, no real user home is touched -- while giving the
    registry/completion derivation a resolvable root inside the test's tempdir.
    """
    return {"HARNESS_STATE_ROOT": str(Path(tmpdir) / "state")}


def make_node(depth=2, dispatch_fallback=None):
    return {
        "id": "execute",
        "kind": "pipeline-stage",
        "role": "fast implementer",
        "model_profile": "light",
        "unit": "dev/backend",
        "dispatch_depth": depth,
        "write_scope": ["source/**"],
        "completion_gate": "code-execute",
        "fallback_hops": dispatch_fallback if dispatch_fallback is not None else make_fallback(),
    }


def make_route(node, tuples=None):
    route = {
        "schema_version": 2,
        "dispatch_contract_version": 3,
        "cwd": str(FIXTURE_WORKTREE),
        "source_commit": FIXTURE_HEAD,
        "capability": "autopilot-code",
        "capability_mode": "dev",
        "effective_intensity": "standard",
        "route_id": "rt-fixture",
        "route_hash": "sha256:fixture",
        "registry_digest": "sha256:fixture-digest",
        "nodes": [node],
        "dispatch_evidence": {
            "tuples": tuples if tuples is not None else [
                base_tuple("claude"), base_tuple("codex"), base_tuple("opencode"),
            ],
        },
    }
    return seal_route_hash(route)


class SelectCheckedTupleTest(unittest.TestCase):
    def test_supported_candidate_with_top_level_counterpart_selected(self):
        node = make_node()
        route = make_route(node)
        selected = N.select_checked_tuple(route, node, "claude")
        self.assertEqual(selected["child_harness"], "claude")
        self.assertEqual(selected["status"], "supported")

    def test_fallback_ordinal_and_adapter_select_deterministic_tuple(self):
        node = make_node()
        route = make_route(node)
        self.assertEqual(N.select_checked_tuple(route, node, "codex")["child_harness"], "codex")
        self.assertEqual(N.select_checked_tuple(route, node, "opencode")["child_harness"], "opencode")

    def test_unsupported_candidate_fails_loudly(self):
        node = make_node(dispatch_fallback=make_fallback(claude=base_tuple("claude", status="unsupported")))
        route = make_route(node, tuples=[base_tuple("claude", status="unsupported")])
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.select_checked_tuple(route, node, "claude")
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-candidate-unsupported")

    def test_ambiguous_candidate_fails_loudly(self):
        node = make_node(dispatch_fallback=[
            {"ordinal": 2, "fallback_hop": "cross-harness-headless",
             "candidates": [base_tuple("codex"), base_tuple("codex", probe_source="second-check")]},
        ])
        route = make_route(node)
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.select_checked_tuple(route, node, "codex")
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-ambiguous-candidate")

    def test_missing_top_level_counterpart_fails_loudly(self):
        node = make_node()
        route = make_route(node, tuples=[base_tuple("claude"), base_tuple("opencode")])
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.select_checked_tuple(route, node, "codex")
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-no-top-level-counterpart")

    def test_conflicting_top_level_counterparts_fail_loudly(self):
        node = make_node()
        route = make_route(node, tuples=[base_tuple("claude"), base_tuple("claude"), base_tuple("codex"), base_tuple("opencode")])
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.select_checked_tuple(route, node, "claude")
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-conflicting-counterparts")

    def test_no_eligible_fallback_for_adapter_fails_loudly(self):
        node = make_node(dispatch_fallback=[
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [base_tuple("claude")]},
        ])
        route = make_route(node)
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.select_checked_tuple(route, node, "codex")
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-no-eligible-fallback")

    def test_probe_time_does_not_distinguish_otherwise_identical_evidence(self):
        node = make_node()
        drifted = dict(base_tuple("claude"), probe_time="2026-07-18T00:00:00Z")
        route = make_route(node, tuples=[drifted, base_tuple("codex"), base_tuple("opencode")])
        selected = N.select_checked_tuple(route, node, "claude")
        self.assertEqual(selected["probe_time"], "2026-07-18T00:00:00Z")


CODEX_PARENT = {"parent_harness": "codex", "parent_transport": "headless", "parent_sandbox": "default"}
CLAUDE_PARENT = {"parent_harness": "claude", "parent_transport": "headless", "parent_sandbox": "default"}
CODEX_PARENT_WORKSPACE_WRITE = {
    "parent_harness": "codex", "parent_transport": "headless", "parent_sandbox": "workspace-write",
}


class ResolveCheckedTupleParentAwareTest(unittest.TestCase):
    def test_foreign_same_harness_row_no_longer_shadows_this_parents_cross_row(self):
        codex_parent_codex_child = base_tuple("codex", parent=CODEX_PARENT)
        claude_parent_codex_child = base_tuple("codex", probe_source="second-check")
        node = make_node(dispatch_fallback=[
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [codex_parent_codex_child]},
            {"ordinal": 2, "fallback_hop": "cross-harness-headless", "candidates": [claude_parent_codex_child]},
        ])
        route = make_route(node, tuples=[codex_parent_codex_child, claude_parent_codex_child])
        selection = N.resolve_checked_tuple(route, node, "codex", parent_identity=CLAUDE_PARENT)
        self.assertEqual(selection.fallback_hop, "cross-harness-headless")
        self.assertEqual(selection.ordinal, 2)
        self.assertEqual(selection.tuple_row["parent_harness"], "claude")

    def test_codex_parent_reaches_the_supported_cross_harness_claude_tuple(self):
        claude_parent_claude_child = base_tuple("claude")
        codex_parent_claude_child = base_tuple(
            "claude", probe_source="second-check", parent=CODEX_PARENT_WORKSPACE_WRITE
        )
        node = make_node(dispatch_fallback=[
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [claude_parent_claude_child]},
            {"ordinal": 2, "fallback_hop": "cross-harness-headless", "candidates": [codex_parent_claude_child]},
        ])
        route = make_route(node, tuples=[claude_parent_claude_child, codex_parent_claude_child])
        selection = N.resolve_checked_tuple(
            route, node, "claude", parent_identity=CODEX_PARENT_WORKSPACE_WRITE
        )
        self.assertEqual(selection.fallback_hop, "cross-harness-headless")
        self.assertEqual(selection.ordinal, 2)
        self.assertEqual(selection.tuple_row["parent_harness"], "codex")

    def test_only_foreign_parent_rows_report_parent_runtime_mismatch(self):
        node = make_node()
        route = make_route(node)
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.resolve_checked_tuple(route, node, "claude", parent_identity=CODEX_PARENT_WORKSPACE_WRITE)
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-parent-runtime-mismatch")
        with self.assertRaises(N.DispatchNodeError) as expected_ctx:
            N.validate_parent_identity(base_tuple("claude"), CODEX_PARENT_WORKSPACE_WRITE)
        self.assertEqual(ctx.exception.fields["mismatch"], expected_ctx.exception.fields["mismatch"])

    def test_adapter_with_no_rows_reports_no_eligible_fallback_even_with_parent(self):
        node = make_node(dispatch_fallback=[
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [base_tuple("claude")]},
        ])
        route = make_route(node)
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.resolve_checked_tuple(route, node, "codex", parent_identity=CODEX_PARENT_WORKSPACE_WRITE)
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-no-eligible-fallback")

    def test_parent_identity_none_preserves_todays_walk(self):
        codex_parent_codex_child = base_tuple("codex", parent=CODEX_PARENT)
        claude_parent_codex_child = base_tuple("codex", probe_source="second-check")
        node = make_node(dispatch_fallback=[
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [codex_parent_codex_child]},
            {"ordinal": 2, "fallback_hop": "cross-harness-headless", "candidates": [claude_parent_codex_child]},
        ])
        route = make_route(node, tuples=[codex_parent_codex_child, claude_parent_codex_child])
        selection = N.resolve_checked_tuple(route, node, "codex", parent_identity=None)
        self.assertEqual(selection.ordinal, 1)
        self.assertEqual(selection.tuple_row["parent_harness"], "codex")

    def test_ambiguity_is_evaluated_after_parent_filtering(self):
        claude_row = base_tuple("codex")
        codex_row = base_tuple("codex", probe_source="second-check", parent=CODEX_PARENT)
        node = make_node(dispatch_fallback=[
            {"ordinal": 2, "fallback_hop": "cross-harness-headless", "candidates": [claude_row, codex_row]},
        ])
        route = make_route(node, tuples=[claude_row, codex_row])
        selection = N.resolve_checked_tuple(route, node, "codex", parent_identity=CLAUDE_PARENT)
        self.assertEqual(selection.tuple_row["parent_harness"], "claude")
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.resolve_checked_tuple(route, node, "codex", parent_identity=None)
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-ambiguous-candidate")

    def test_two_rows_for_the_same_parent_and_adapter_still_ambiguous(self):
        node = make_node(dispatch_fallback=[
            {"ordinal": 2, "fallback_hop": "cross-harness-headless",
             "candidates": [base_tuple("codex"), base_tuple("codex", probe_source="second-check")]},
        ])
        route = make_route(node)
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.resolve_checked_tuple(route, node, "codex", parent_identity=CLAUDE_PARENT)
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-ambiguous-candidate")

    def test_resolution_reports_the_hop_and_ordinal_of_the_bound_tuple(self):
        node = make_node()
        route = make_route(node)
        selection = N.resolve_checked_tuple(route, node, "codex")
        self.assertEqual(selection.fallback_hop, "cross-harness-headless")
        self.assertEqual(selection.ordinal, 2)
        self.assertEqual(selection.tuple_row["child_harness"], "codex")

    def test_bind_dispatch_evidence_binds_this_parents_tuple(self):
        claude_parent_claude_child = base_tuple("claude")
        codex_parent_claude_child = base_tuple(
            "claude", probe_source="second-check", parent=CODEX_PARENT_WORKSPACE_WRITE
        )
        node = make_node(dispatch_fallback=[
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [claude_parent_claude_child]},
            {"ordinal": 2, "fallback_hop": "cross-harness-headless", "candidates": [codex_parent_claude_child]},
        ])
        route = make_route(node, tuples=[claude_parent_claude_child, codex_parent_claude_child])
        extra = N.bind_dispatch_evidence(
            route, node, "claude", [], parent_identity=CODEX_PARENT_WORKSPACE_WRITE
        )
        as_dict = dict(zip(extra[0::2], extra[1::2]))
        self.assertEqual(as_dict["--parent-harness"], "codex")


class BindDispatchEvidenceTest(unittest.TestCase):
    def test_generated_route_and_depth_arguments_cannot_be_overridden(self):
        for values, flag in (
            (["--route-file", "/tmp/forged.json"], "--route-file"),
            (["--dispatch-depth=1"], "--dispatch-depth"),
            (["--start"], "--start"),
        ):
            with self.subTest(flag=flag), self.assertRaises(N.DispatchNodeError) as ctx:
                N.reject_generated_argument_overrides(values)
            self.assertEqual(ctx.exception.reason, "dispatch-generated-argument-override")
            self.assertEqual(ctx.exception.fields["flag"], flag)

    def test_supported_record_emits_six_flags_and_nonempty_failure_class(self):
        claude = base_tuple("claude", failure_class="minor-warning")
        node = make_node(dispatch_fallback=make_fallback(claude=claude))
        route = make_route(node, tuples=[claude, base_tuple("codex"), base_tuple("opencode")])
        extra = N.bind_dispatch_evidence(route, node, "claude", [])
        as_dict = dict(zip(extra[0::2], extra[1::2]))
        self.assertEqual(as_dict["--launch-authority"], "conductor")
        self.assertEqual(as_dict["--parent-harness"], "claude")
        self.assertEqual(as_dict["--parent-transport"], "headless")
        self.assertEqual(as_dict["--parent-sandbox"], "default")
        self.assertEqual(as_dict["--nested-eligibility"], "supported")
        self.assertEqual(as_dict["--eligibility-source"], "fixture-check")
        self.assertEqual(as_dict["--eligibility-failure-class"], "minor-warning")
        self.assertEqual(len(as_dict), 7)

    def test_empty_failure_class_is_not_forwarded(self):
        node = make_node()
        route = make_route(node)
        extra = N.bind_dispatch_evidence(route, node, "claude", [])
        self.assertNotIn("--eligibility-failure-class", extra)

    def test_equal_explicit_values_pass_without_duplication(self):
        node = make_node()
        route = make_route(node)
        adapter_args = ["--launch-authority", "conductor", "--nested-eligibility=supported"]
        extra = N.bind_dispatch_evidence(route, node, "claude", adapter_args)
        self.assertNotIn("--launch-authority", extra)
        self.assertNotIn("--nested-eligibility", extra)
        self.assertIn("--parent-harness", extra)

    def test_explicit_conflict_fails_before_wrapper_invocation(self):
        node = make_node()
        route = make_route(node)
        adapter_args = ["--nested-eligibility", "unsupported"]
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.bind_dispatch_evidence(route, node, "claude", adapter_args)
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-explicit-conflict")
        self.assertEqual(ctx.exception.fields["flag"], "--nested-eligibility")
        self.assertEqual(ctx.exception.fields["explicit"], "unsupported")
        self.assertEqual(ctx.exception.fields["record"], "supported")

    def test_conflicting_duplicate_explicit_occurrences_fail_and_show_both_values(self):
        node = make_node()
        route = make_route(node)
        adapter_args = ["--parent-harness", "claude", "--parent-harness", "codex"]
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.bind_dispatch_evidence(route, node, "claude", adapter_args)
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-explicit-conflict")
        self.assertEqual(ctx.exception.fields["explicit"], "claude,codex")
        self.assertEqual(ctx.exception.fields["record"], "claude")

    def test_flag_equals_form_is_recognized(self):
        node = make_node()
        route = make_route(node)
        extra = N.bind_dispatch_evidence(route, node, "claude", ["--parent-sandbox=default"])
        self.assertNotIn("--parent-sandbox", extra)

    def test_matching_actual_parent_runtime_is_accepted(self):
        node = make_node()
        route = make_route(node)
        actual = {
            "parent_harness": "claude",
            "parent_transport": "headless",
            "parent_sandbox": "default",
        }
        extra = N.bind_dispatch_evidence(route, node, "claude", [], parent_identity=actual)
        self.assertIn("--parent-harness", extra)

    def test_mismatched_actual_parent_runtime_fails_before_wrapper(self):
        node = make_node()
        route = make_route(node)
        actual = {
            "parent_harness": "codex",
            "parent_transport": "headless",
            "parent_sandbox": "workspace-write-network-enabled",
        }
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.bind_dispatch_evidence(route, node, "claude", [], parent_identity=actual)
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-parent-runtime-mismatch")
        self.assertIn("parent_harness:record=claude:actual=codex", ctx.exception.fields["mismatch"])

    def test_partial_actual_parent_runtime_fails_closed(self):
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.current_parent_identity({"AGENT_DISPATCH_CURRENT_HARNESS": "claude"})
        self.assertEqual(ctx.exception.reason, "dispatch-evidence-parent-runtime-incomplete")
        self.assertIn("AGENT_DISPATCH_CURRENT_TRANSPORT", ctx.exception.fields["missing"])


class DryRunCompletionMarkerPathTest(unittest.TestCase):
    """Plan-check round-1 T5: dispatch-node.py's dry-run completion_marker=
    line must match capability-route.py's own completion_dir() output
    string-for-string -- it used to reimplement the derivation inline as
    Path(os.environ.get("AGENT_HOME", ROOT))/".dispatch/completion"/..., which
    could silently diverge from the writer's actual resolved state root."""

    def test_dry_run_completion_marker_matches_completion_dir(self):
        import contextlib
        import io

        node = make_node(depth=1, dispatch_fallback=[])
        route = make_route(node, tuples=[])
        with tempfile.TemporaryDirectory() as td:
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            full_argv = [
                "dispatch-node.py", "--route", str(route_path),
                "--node", "execute", "--adapter", "claude", "--slug", "dry-marker",
                "--action", "dry-run",
            ]
            environ = fixture_state_environ(td)
            captured_stdout = io.StringIO()
            with mock.patch.object(sys, "argv", full_argv), \
                 mock.patch.dict(N.os.environ, environ, clear=True), \
                 mock.patch.object(N.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                 contextlib.redirect_stdout(captured_stdout):
                try:
                    N.main()
                except SystemExit:
                    pass
                jobs = N.resolve_global_registry(
                    N.ROOT, None, 1, "dry-run", environ
                ).path
                expected = str(
                    N.ROUTE.completion_dir(route["route_id"], jobs=jobs)
                    / (node["id"] + ".json")
                )
        lines = captured_stdout.getvalue().splitlines()
        # The default action launches nothing, and its receipt says so first.
        self.assertEqual(lines[0], "started=0", lines)
        self.assertIn("nothing was registered or started", lines[1])
        marker_lines = [line for line in lines if line.startswith("completion_marker=")]
        self.assertEqual(len(marker_lines), 1, lines)
        printed = marker_lines[0][len("completion_marker="):]
        self.assertEqual(printed, expected)


class MainMaterializationTest(unittest.TestCase):
    def test_checked_codex_dispatch_node_entry_builds_wrapper_command_without_spawning_child(self):
        node = make_node(depth=2)
        route = make_route(node)
        jobs = _FIXTURE_ROOT / "dispatch" / "jobs.log"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        argv = self._run_main(
            ["--node", "execute", "--adapter", "codex", "--slug", "checked-entry",
             "--parent", "owner", "--action", "start"],
            route,
            environ={"AGENT_DISPATCH_JOBS": str(jobs)},
        )
        self.assertIsNotNone(argv)
        self.assertEqual(argv[1], str(ROOT / "adapters/codex/bin/dispatch-headless.py"))
        self.assertIn("--start", argv)
        self.assertEqual(argv[argv.index("--route-node") + 1], "execute")
        self.assertEqual(argv[argv.index("--dispatch-depth") + 1], "2")
        for flag, value in (("--parent-harness", "claude"),
                            ("--parent-transport", "headless"),
                            ("--parent-sandbox", "default"),
                            ("--launch-authority", "conductor"),
                            ("--nested-eligibility", "supported"),
                            ("--eligibility-source", "fixture-check")):
            self.assertEqual(argv[argv.index(flag) + 1], value)

    def test_frame_public_entry_uses_depth1_selector_for_each_adapter(self):
        node = make_node(depth=1, dispatch_fallback=[])
        node.update(id="frame", kind="map-worker", unit="plan/frame")
        for adapter in ("claude", "codex", "opencode"):
            with self.subTest(adapter=adapter):
                argv = self._run_main(["--node", "frame", "--adapter", adapter,
                    "--slug", "frame-probe", "--action", "start", "--prompt-text", "bounded task"],
                    make_route(node, tuples=[]))
                self.assertEqual(argv[1], str(ROOT / "utilities/dispatch-owner.py"))
                self.assertEqual(argv[argv.index("--route-node") + 1], "frame")
                self.assertEqual(argv[argv.index("--adapter") + 1], adapter)
                self.assertEqual(argv[argv.index("--prompt-text") + 1], "bounded task")
                self.assertNotIn("support", argv)
                self.assertNotIn("--dispatch-depth", argv)

    def _run_main(self, argv, route, environ=None):
        captured = {}

        def fake_run(cmd, **kwargs):
            if "verify" in cmd:
                return mock.Mock(returncode=0)
            captured["argv"] = cmd
            captured["env"] = kwargs.get("env")
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as td:
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            full_argv = ["dispatch-node.py", "--route", str(route_path)] + argv + [
                "--", "--launch-lifecycle", "detached"]
            with mock.patch.object(sys, "argv", full_argv), \
                 mock.patch.dict(
                     N.os.environ,
                     {**fixture_state_environ(td), **(environ or {})},
                     clear=True,
                 ), \
                 mock.patch.object(N.ROUTE_AUTHORITY, "route_in_force", return_value=route), \
                 mock.patch.object(N.subprocess, "run", side_effect=fake_run):
                try:
                    N.main()
                except SystemExit:
                    pass
        self.last_wrapper_env = captured.get("env")
        return captured.get("argv")

    def test_wrapper_launch_scrubs_owner_binding_and_keeps_node_binding(self):
        node = make_node(depth=1, dispatch_fallback=[])
        route = make_route(node, tuples=[])
        argv = self._run_main(
            ["--node", "execute", "--adapter", "claude", "--slug", "env"],
            route,
            environ={
                "AGENT_OWNER_ROUTE_FILE": "/tmp/owner.json",
                "AGENT_OWNER_ROUTE_ID": "rt-owner",
                "AGENT_OWNER_ROUTE_HASH": "sha256:owner",
                "AGENT_DISPATCH_BROKER_TOKEN": "retired",
                "AGENT_ROUTE_FILE": "/tmp/node.json",
                "AGENT_ROUTE_ID": "rt-node",
            },
        )
        self.assertIsNotNone(argv)
        self.assertNotIn("AGENT_OWNER_ROUTE_FILE", self.last_wrapper_env)
        self.assertNotIn("AGENT_OWNER_ROUTE_ID", self.last_wrapper_env)
        self.assertNotIn("AGENT_OWNER_ROUTE_HASH", self.last_wrapper_env)
        self.assertNotIn("AGENT_DISPATCH_BROKER_TOKEN", self.last_wrapper_env)
        self.assertEqual(self.last_wrapper_env["AGENT_ROUTE_FILE"], "/tmp/node.json")
        self.assertEqual(self.last_wrapper_env["AGENT_ROUTE_ID"], "rt-node")

    def test_depth1_materialization_emits_no_evidence_and_preserves_safe_adapter_args(self):
        node = make_node(depth=1, dispatch_fallback=[])
        route = make_route(node, tuples=[])
        argv = self._run_main(
            ["--node", "execute", "--adapter", "claude", "--slug", "s1", "--", "--log-dir", "/tmp/logs"],
            route,
        )
        self.assertIsNotNone(argv)
        for flag in N.EVIDENCE_FLAG_MAP.values():
            self.assertNotIn(flag, argv)
        self.assertNotIn(N.FAILURE_CLASS_FLAG, argv)
        self.assertIn("--log-dir", argv)
        self.assertIn("/tmp/logs", argv)
        self.assertEqual(argv[argv.index("--model-profile") + 1], "light")

    def test_depth2_materialization_binds_evidence_into_wrapper_argv(self):
        node = make_node()
        route = make_route(node)
        argv = self._run_main(
            ["--node", "execute", "--adapter", "claude", "--slug", "s2", "--parent", "owner"],
            route,
        )
        self.assertIsNotNone(argv)
        self.assertIn("--nested-eligibility", argv)
        self.assertIn("supported", argv)
        self.assertIn("--parent", argv)
        self.assertIn("owner", argv)
        self.assertEqual(argv[argv.index("--worker-type") + 1], "stage")
        self.assertEqual(argv[argv.index("--capability-mode") + 1], "dev")
        self.assertEqual(argv[argv.index("--worker-mode") + 1], "dev/backend")
        self.assertEqual(argv[argv.index("--assigned-contract") + 1], "code-execute")
        self.assertEqual(argv[argv.index("--model-role") + 1], "fast implementer")
        self.assertEqual(argv[argv.index("--model-profile") + 1], "light")
        self.assertNotIn("--worker-role", argv)

    def test_harness_affinity_field_forwarded_into_wrapper_argv(self):
        node = make_node()
        node["harness_affinity"] = "codex"
        route = make_route(node)
        argv = self._run_main(
            ["--node", "execute", "--adapter", "claude", "--slug", "s4", "--parent", "owner"],
            route,
        )
        self.assertIsNotNone(argv)
        idx = argv.index("--harness-affinity")
        self.assertEqual(argv[idx + 1], "codex")

    # -- a sealed worker pin beats the requested adapter ---------------------------------
    def _pinned(self, adapter, pin="claude", *, target="worker", claude_status="supported",
                limits=None, extra=(), subject="execute"):
        node = make_node(dispatch_fallback=make_fallback(claude=base_tuple("claude", status=claude_status)))
        node["harness_affinity"] = pin
        route = make_route(node, tuples=[base_tuple("claude", status=claude_status),
                                         base_tuple("codex"), base_tuple("opencode")])
        if pin:
            route["selection_pins"] = {"contract_version": 1,
                                       target: {"harness": pin, "model": None, "effort": None}}
        route = seal_route_hash(route)
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("dispatch_capacity_evidence.active_limits", return_value=limits or {}):
            jobs = Path(td) / "jobs.log"
            jobs.touch()
            argv = self._run_main(["--node", subject, "--adapter", adapter, "--slug", "pin-probe",
                                   "--parent", "owner", "--action", "start", *extra], route,
                                  environ={"AGENT_DISPATCH_JOBS": str(jobs)})
        self.assertIsNotNone(argv)
        flag = argv[argv.index("--explicit-adapter") + 1] if "--explicit-adapter" in argv else None
        return Path(argv[1]).parts[-3], flag, argv

    def test_sealed_worker_pin_beats_an_explicit_adapter_and_records_the_request(self):
        wrapper, flag, argv = self._pinned("codex")
        self.assertEqual((wrapper, flag), ("claude", "codex"))
        self.assertEqual(argv[argv.index("--harness-affinity") + 1], "claude")
        # the checked evidence bound into the wrapper argv is the pinned harness's, not the request's
        self.assertEqual(argv[argv.index("--nested-eligibility") + 1], "supported")

    def test_requesting_the_pinned_harness_records_no_override(self):
        wrapper, flag, _argv = self._pinned("claude")
        self.assertEqual((wrapper, flag), ("claude", None))

    def test_unavailable_worker_pin_keeps_the_request(self):
        for label, kwargs in (
                ("claude has no supported checked tuple", dict(claude_status="unsupported")),
                ("claude is at an active limit", dict(limits={"claude": {"reset_epoch": 9999999999}}))):
            with self.subTest(label):
                wrapper, flag, _argv = self._pinned("codex", **kwargs)
                self.assertEqual((wrapper, flag), ("codex", None))

    def test_no_worker_pin_keeps_the_explicit_adapter(self):
        for label, kwargs in (("no pin at all", dict(pin=None)),
                              ("an owner pin only", dict(target="owner")),
                              ("a frame pin only", dict(target="frame"))):
            with self.subTest(label):
                wrapper, flag, _argv = self._pinned("codex", **kwargs)
                self.assertEqual((wrapper, flag), ("codex", None))

    def test_a_replay_keeps_its_predecessor_harness(self):
        # `replacement_task` ties a replay to the harness of the row it replays
        wrapper, flag, argv = self._pinned("codex", extra=("--", "--automatic-retry-of", "att-prior"))
        self.assertEqual((wrapper, flag), ("codex", None))
        self.assertIn("att-prior", argv)

    def test_the_override_record_cannot_be_forged_through_adapter_args(self):
        with self.assertRaises(N.DispatchNodeError):
            N.reject_generated_argument_overrides(["--explicit-adapter", "codex"])

    def test_harness_affinity_absent_field_omits_flag(self):
        node = make_node()
        self.assertNotIn("harness_affinity", node)
        route = make_route(node)
        argv = self._run_main(
            ["--node", "execute", "--adapter", "claude", "--slug", "s5", "--parent", "owner"],
            route,
        )
        self.assertIsNotNone(argv)
        self.assertNotIn("--harness-affinity", argv)

    def test_stage_argv_omits_qa(self):
        # --qa is not a user-facing axis (CONVENTIONS §1.1): the wrapper
        # derives it from --intensity (dispatch_mode_contract.resolve_qa), so
        # dispatch-node.py must not forward a hardcoded default anymore.
        node = make_node()
        route = make_route(node)
        route["effective_intensity"] = "thorough"
        argv = self._run_main(
            ["--node", "execute", "--adapter", "claude", "--slug", "s7", "--parent", "owner"],
            route,
        )
        self.assertIsNotNone(argv)
        self.assertNotIn("--qa", argv)
        self.assertEqual(argv[argv.index("--intensity") + 1], "thorough")

    def test_explicit_adapter_differs_from_affinity_launch_still_passes(self):
        node = make_node()
        node["harness_affinity"] = "codex"
        route = make_route(node)
        argv = self._run_main(
            ["--node", "execute", "--adapter", "claude", "--slug", "s6", "--parent", "owner"],
            route,
        )
        self.assertIsNotNone(argv)
        self.assertIn("--harness-affinity", argv)
        self.assertIn("codex", argv)

    def test_depth2_materialization_exits_65_on_missing_evidence(self):
        node = make_node(dispatch_fallback=[
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [base_tuple("claude")]},
        ])
        route = make_route(node, tuples=[])
        with tempfile.TemporaryDirectory() as td:
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            full_argv = ["dispatch-node.py", "--route", str(route_path), "--node", "execute",
                         "--adapter", "codex", "--slug", "s3", "--parent", "owner"]
            with mock.patch.object(sys, "argv", full_argv), \
                 mock.patch.object(N.subprocess, "run", side_effect=lambda cmd, **kw: mock.Mock(returncode=0)):
                with self.assertRaises(SystemExit) as ctx:
                    N.main()
        self.assertEqual(ctx.exception.code, 65)

    def test_replica_start_requires_batch_token_before_wrapper_invocation(self):
        first = make_node()
        first["replica_group"] = "execute"
        second = {**make_node(), "id": "execute-replica", "replica_group": "execute"}
        route = make_route(first)
        route["nodes"].append(second)
        with tempfile.TemporaryDirectory() as td:
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            argv = [
                "dispatch-node.py", "--route", str(route_path), "--node", "execute",
                "--adapter", "claude", "--slug", "replica", "--parent", "owner",
                "--action", "start",
            ]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(N.os.environ, {}, clear=True), \
                 mock.patch.object(
                     N.subprocess, "run", return_value=mock.Mock(returncode=0)
                 ) as run:
                with self.assertRaises(SystemExit) as ctx:
                    N.main()
        self.assertEqual(ctx.exception.code, 65)
        self.assertEqual(run.call_count, 1)

    def test_an_owner_route_and_start_need_no_route_or_slug_argument(self):
        first = make_node()
        first["replica_group"] = "execute"
        second = {**make_node(), "id": "execute-replica", "replica_group": "execute"}
        route = make_route(first)
        route["nodes"].append(second)
        with tempfile.TemporaryDirectory() as td:
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            out = io.StringIO()
            argv = ["dispatch-node.py", "--node", "execute", "--adapter", "claude", "--start"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(N.os.environ, {}, clear=True), \
                 mock.patch("owner_route_binding.default_owner_route_file", return_value=str(route_path)), \
                 mock.patch.object(N.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                 contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as ctx:
                    N.main()
        # The route came from the owner's current route and --start meant start.
        self.assertEqual(ctx.exception.code, 65)
        self.assertIn("reason=parallel-group-batch-required", out.getvalue())

    def test_replica_register_is_forbidden_even_with_batch_token(self):
        node = make_node()
        node["replica_group"] = "execute"
        route = make_route(node)
        with tempfile.TemporaryDirectory() as td:
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            argv = [
                "dispatch-node.py", "--route", str(route_path), "--node", "execute",
                "--adapter", "claude", "--slug", "replica", "--parent", "owner",
                "--action", "register",
            ]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(
                     N.os.environ,
                     {N.GOVERNOR_RESERVATION_ENV: "a" * 32},
                     clear=True,
                 ), \
                 mock.patch.object(
                     N.subprocess, "run", return_value=mock.Mock(returncode=0)
                 ) as run:
                with self.assertRaises(SystemExit) as ctx:
                    N.main()
        self.assertEqual(ctx.exception.code, 65)
        self.assertEqual(run.call_count, 1)


class RoundProtocolTest(unittest.TestCase):
    """Verdict-less history gets full-scope first-pass guidance; real verdicts
    continue to scope review corrections and stage work to their prior findings."""

    def _rows(self, node, n, extra=""):
        # SD-153: `failure_class=fail` makes this a genuine FAIL verdict for a
        # non-review worker, so it still spends the round budget and renders
        # with its literal note (not "판정 없음(...)") -- unlike a review
        # worker's `dead-worker-fail`, which is a crash, not a verdict.
        pipe = ("capability=autopilot-code,attempt_schema_version=2,registered_worker=1,"
                "route_id=rt-fixture,route_node=" + node + ",note=dead-worker-fail,failure_class=fail" + extra)
        return "".join(
            f"2026-08-24T00:00:0{i}Z\tdone\t/repo\t/wt\tslug-r{i}\t{pipe},attempt_id=att-{node}-{i}\n"
            for i in range(1, n + 1)
        )

    def _launch(self, node, rows, slug="slug-next", extra_argv=()):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["argv"] = cmd
            return mock.Mock(returncode=0)

        route = seal_route_hash(make_route(node, tuples=[]))
        printed = []
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / ".dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text(rows)
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            argv = ["dispatch-node.py", "--route", str(route_path), "--node", node["id"],
                    "--adapter", "claude", "--slug", slug, "--action", "dry-run",
                    "--prompt-text", "Perform a fresh independent pass.", *extra_argv]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(N.os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}, clear=True), \
                 mock.patch.object(N.subprocess, "run", side_effect=fake_run), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                try:
                    N.main()
                except SystemExit:
                    pass
        cmd = captured["argv"]
        return cmd[cmd.index("--prompt-text") + 1], printed

    def test_first_round_has_no_block(self):
        node = make_node(depth=1, dispatch_fallback=[])
        prompt, printed = self._launch(node, "")
        self.assertEqual(prompt, "Perform a fresh independent pass.")
        self.assertFalse([l for l in printed if l.startswith("correction_round=")])

    def test_stage_round_is_batched_fix(self):
        node = make_node(depth=1, dispatch_fallback=[])
        prompt, printed = self._launch(node, self._rows("execute", 2))
        self.assertIn("Round protocol (round 3 of route node `execute`", prompt)
        self.assertIn("slug-r1 (dead-worker-fail); slug-r2 (dead-worker-fail)", prompt)
        self.assertIn("close every 🔴 it lists together", prompt)
        self.assertNotIn("closed/open/regressed", prompt)
        self.assertIn("correction_round=3", printed)

    def test_review_round_is_closure_check(self):
        node = dict(make_node(depth=1, dispatch_fallback=[]), id="impl-review",
                    kind="review-worker", unit="qa/custom-review", completion_gate="code-review-fixture")
        substantive = self._rows("impl-review", 1).replace(
            "note=dead-worker-fail,failure_class=fail", "note=completed-review-blocking")
        prompt, _ = self._launch(node, substantive + self._rows("execute", 5))
        self.assertIn("Round protocol (round 2 of route node `impl-review`", prompt)
        self.assertIn("closed/open/regressed", prompt)
        self.assertIn("even if the assignment above asks for a fresh independent review", prompt)
        self.assertTrue(prompt.startswith("Perform a fresh independent pass."))

    def test_verdictless_review_history_gets_a_full_first_review(self):
        node = dict(make_node(depth=1, dispatch_fallback=[]), id="impl-review",
                    kind="review-worker", unit="qa/custom-review",
                    completion_gate="code-review-fixture")
        for note, failure in (("dead-worker-blocked", "blocked"),
                              ("dead-invalid-envelope", "contract"),
                              ("dead-capacity", "capacity")):
            with self.subTest(note=note):
                row = (
                    "2026-08-24T00:00:01Z\tdone\t/repo\t/wt\tslug-dead\t"
                    f"capability=autopilot-code,attempt_schema_version=2,registered_worker=1,"
                    f"route_id=rt-fixture,route_node=impl-review,worker_type=review,note={note},"
                    f"failure_class={failure},attempt_id=att-{note}\n"
                )
                prompt, _ = self._launch(node, row)
                self.assertIn("Round protocol (round 2 of route node `impl-review`", prompt)
                self.assertIn("full independent review", prompt)
                self.assertIn("full assigned scope", prompt)
                self.assertIn(f"판정 없음({note})", prompt)
                self.assertNotIn("This is a correction round", prompt)
                self.assertNotIn("Implementation source changed", prompt)

        # A substantive blocking verdict remains a correction, even when a
        # later attempt died without producing a second verdict.
        prompt, _ = self._launch(
            node,
            self._rows("impl-review", 1).replace(
                "note=dead-worker-fail,failure_class=fail",
                "note=completed-review-blocking",
            ) + (
                "2026-08-24T00:00:02Z\tdone\t/repo\t/wt\tslug-dead\t"
                "capability=autopilot-code,attempt_schema_version=2,registered_worker=1,"
                "route_id=rt-fixture,route_node=impl-review,worker_type=review,"
                "note=dead-capacity,failure_class=capacity,attempt_id=att-dead-capacity\n"
            ),
        )
        self.assertIn("This is a correction round", prompt)

    def test_verdictless_stage_history_gets_full_original_task_scope(self):
        node = make_node(depth=1, dispatch_fallback=[])
        row = (
            "2026-08-24T00:00:01Z\tdone\t/repo\t/wt\tslug-dead\t"
            "capability=autopilot-code,attempt_schema_version=2,registered_worker=1,"
            "route_id=rt-fixture,route_node=execute,worker_type=stage,note=dead-capacity,"
            "failure_class=capacity,attempt_id=att-dead-capacity\n"
        )
        prompt, _ = self._launch(node, row)
        self.assertIn("Round protocol (round 2 of route node `execute`", prompt)
        self.assertIn("slug-dead (판정 없음(dead-capacity))", prompt)
        self.assertIn("full execution of the original assigned task and scope", prompt)
        self.assertIn("verdict-less attempts provide execution context only", prompt)
        self.assertNotIn("This is a full independent review", prompt)
        self.assertNotIn("This is a correction round", prompt)
        self.assertNotIn("Read the failing review artifact first", prompt)

    def test_own_slug_subsession_and_other_routes_are_not_rounds(self):
        node = make_node(depth=1, dispatch_fallback=[])
        rows = (self._rows("execute", 1).replace("slug-r1", "slug-next")
                + self._rows("execute", 1, extra=",stage_authority=0").replace("att-execute-1", "att-sub")
                + self._rows("execute", 1).replace("route_id=rt-fixture", "route_id=rt-other").replace("att-execute-1", "att-other"))
        prompt, _ = self._launch(node, rows)
        self.assertNotIn("Round protocol", prompt)


class ReviewRoundCapTest(unittest.TestCase):
    """C-14 + SD-153: plan-check/impl-review/test rounds are capped by
    tier-derived retry budget (CONVENTIONS §1.1), not left unbounded -- but
    only a real verdict spends that budget (SD-153 rule 1). `execute`/
    `report` carry no cap here -- their retry mechanism is HEAD-lineage
    based, not round-counted."""

    def _rows(self, node, n):
        # A review verdict must be a real blocking finding, not a crash --
        # `completed-review-blocking` spends the round budget the way two
        # prior `dead-worker-fail` rows used to (and no longer do, since a
        # review worker's crash never produced a verdict). `test`'s own
        # worker is not a review worker (kind=pipeline-stage, C-14's declared
        # exception), so a genuine FAIL (`dead-worker-fail` +
        # `failure_class=fail`) still spends its budget the same way a
        # stage's does.
        note = "dead-worker-fail,failure_class=fail" if node == "test" else "completed-review-blocking"
        pipe = ("capability=autopilot-code,attempt_schema_version=2,registered_worker=1,"
                "route_id=rt-fixture,route_node=" + node + ",note=" + note)
        if node == "test":
            pipe += (",review_subject_digest=sha256:test-source"
                     ",review_subject_binding_digest=sha256:fixture-binding")
        return "".join(
            f"2026-08-24T00:00:0{i}Z\tdone\t/repo\t/wt\tslug-r{i}\t{pipe},attempt_id=att-{node}-{i}\n"
            for i in range(1, n + 1)
        )

    def _run(self, node_id, prior_count, *, effective_intensity="standard", slug="slug-next", rows=None):
        if node_id == "test":
            node = dict(make_node(depth=1, dispatch_fallback=[]), id=node_id,
                        kind="pipeline-stage", unit="qa/test", completion_gate="code-" + node_id)
        else:
            node = dict(make_node(depth=1, dispatch_fallback=[]), id=node_id,
                        kind="review-worker", unit="qa/custom-review",
                        completion_gate="code-review-fixture" if node_id == "impl-review" else "code-" + node_id)
        route = make_route(node, tuples=[])
        route["effective_intensity"] = effective_intensity
        route = seal_route_hash(route)
        printed = []
        code = None
        launched = []
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd and str(cmd[0]) == "git":
                # Source-bound code-test admission snapshots this isolated Git
                # fixture while wrapper launches remain mocked below.
                return real_run(cmd, **kwargs)
            launched.append(cmd)
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / ".dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text(rows if rows is not None else self._rows(node_id, prior_count))
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            argv = ["dispatch-node.py", "--route", str(route_path), "--node", node_id,
                    "--adapter", "claude", "--slug", slug, "--action", "dry-run",
                    "--prompt-text", "Perform a fresh independent pass."]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(N.os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}, clear=True), \
                 mock.patch.object(N.subprocess, "run", side_effect=fake_run), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                try:
                    if node_id == "test":
                        with mock.patch("review_source_subject.observe",
                                        return_value={"digest": "sha256:test-source"}), \
                             mock.patch("review_source_subject.read_binding",
                                        return_value={"subject_digest": "sha256:test-source"}):
                            N.main()
                    else:
                        N.main()
                except SystemExit as exc:
                    code = exc.code
        # A capped rejection must stop before the wrapper launch (only the
        # earlier route "verify" subprocess call, if any, may have run).
        if code == 65:
            self.assertTrue(
                all("dispatch-headless.py" not in str(part) for cmd in launched for part in cmd),
                launched,
            )
        return code, printed

    def test_direct_and_quick_reject_the_second_round(self):
        for intensity in ("direct", "quick"):
            code, printed = self._run("plan-check", 1, effective_intensity=intensity)
            self.assertEqual(code, 65)
            self.assertIn("reason=review-round-budget-exhausted", printed)
            self.assertIn("max_round=1", printed)
            self.assertIn("round=2", printed)
            self.assertIn("child_spawned=0", printed)

    def test_direct_and_quick_allow_the_first_round(self):
        for intensity in ("direct", "quick"):
            code, printed = self._run("plan-check", 0, effective_intensity=intensity)
            self.assertEqual(code, 0)
            self.assertFalse([l for l in printed if l.startswith("reason=review-round-budget-exhausted")])

    def test_standard_and_strong_reject_the_third_round(self):
        for intensity in ("standard", "strong"):
            code, printed = self._run("impl-review", 2, effective_intensity=intensity)
            self.assertEqual(code, 65)
            self.assertIn("reason=review-round-budget-exhausted", printed)
            self.assertIn("max_round=2", printed)
            self.assertIn("round=3", printed)

    def test_standard_and_strong_allow_the_second_round(self):
        for intensity in ("standard", "strong"):
            code, printed = self._run("impl-review", 1, effective_intensity=intensity)
            self.assertEqual(code, 0)

    def test_thorough_and_adversarial_reject_the_fourth_round(self):
        for intensity in ("thorough", "adversarial"):
            code, printed = self._run("test", 3, effective_intensity=intensity)
            self.assertEqual(code, 65)
            self.assertIn("reason=review-round-budget-exhausted", printed)
            self.assertIn("max_round=3", printed)
            self.assertIn("round=4", printed)

    def test_thorough_and_adversarial_allow_the_third_round(self):
        for intensity in ("thorough", "adversarial"):
            code, printed = self._run("test", 2, effective_intensity=intensity)
            self.assertEqual(code, 0)

    def test_finished_blocking_review_rounds_still_spend_the_budget(self):
        # OPERATIONS §5.10: a review that recorded blocking findings ends
        # `completed-review-blocking`, not `dead-worker-fail` -- but it is a
        # spent round all the same. The cap counts it exactly as before, so the
        # owner-closure completion (capability-route complete) is the only way
        # forward once the budget is gone, never a third dispatch.
        rows = self._rows("plan-check", 2).replace("note=dead-worker-fail", "note=completed-review-blocking")
        self.assertEqual(rows.count("completed-review-blocking"), 2)
        code, printed = self._run("plan-check", 2, rows=rows)
        self.assertEqual(code, 65)
        self.assertIn("reason=review-round-budget-exhausted", printed)
        self.assertIn("round=3", printed)
        one = self._rows("plan-check", 1).replace("note=dead-worker-fail", "note=completed-review-blocking")
        code, printed = self._run("plan-check", 1, rows=one)
        self.assertEqual(code, 0)
        self.assertIn("correction_round=2", printed)

    def test_round_protocol_history_names_the_finished_review_note(self):
        cols = ["2026-08-24T00:00:01Z", "done", "/repo", "/wt", "slug-r1"]
        meta = {"note": "completed-review-blocking"}
        budget = REVIEW_ROUND_CAP.round_budget(
            {"effective_intensity": "standard"}, {"kind": "review-worker"}, [("done", meta)], revisions=())
        block = N.round_protocol_block(budget, [(cols, meta)], "review", "plan-check")
        self.assertIn("Round protocol (round 2", block)
        self.assertIn("slug-r1 (completed-review-blocking)", block)
        self.assertNotIn("dead-worker", block)

    # The cap set is derived from a kind, not curated by hand. This is the gate:
    # if a recipe gains a review node and the constant is not updated, this test
    # fails rather than the node silently escaping the cap.
    DECLARED_NON_REVIEW_CAPS = frozenset({"test"})

    def test_capped_ids_equal_every_review_worker_node_in_topologies(self):
        registry = json.loads((ROOT / "capabilities" / "topologies.json").read_text())
        found = set()

        def walk(obj):
            if isinstance(obj, dict):
                if obj.get("kind") == "review-worker" and "id" in obj:
                    found.add(obj["id"])
                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)

        walk(registry)
        self.assertEqual(
            N.ROUND_CAPPED_NODE_IDS - self.DECLARED_NON_REVIEW_CAPS,
            found,
            "ROUND_CAPPED_NODE_IDS must equal the set of review-worker recipe "
            "nodes, plus only the explicitly declared exceptions "
            f"{sorted(self.DECLARED_NON_REVIEW_CAPS)}",
        )

    def test_every_capped_id_rejects_an_over_budget_round(self):
        # Deliberately exhaustive rather than a representative sample: the
        # defect being fixed was an id quietly missing from the cap set.
        for node_id in sorted(N.ROUND_CAPPED_NODE_IDS):
            with self.subTest(node_id=node_id):
                code, printed = self._run(node_id, 2, effective_intensity="standard")
                self.assertEqual(code, 65)
                self.assertIn("reason=review-round-budget-exhausted", printed)
                self.assertIn(f"route_node={node_id}", printed)

    def test_cap_counts_rows_written_with_the_production_route_key(self):
        # prior_round_attempts() filtered on `route=`, a key the registry writer
        # never emits (it writes `route_id=`), so round_no was always 1 and the
        # cap never fired anywhere. The old fixture minted `route=` too and was
        # therefore green against the bug.
        rows = self._rows("plan-check", 2)
        self.assertIn("route_id=rt-fixture", rows)
        self.assertNotIn("route=rt-fixture", rows)
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(rows)
            prior = N.prior_round_attempts(jobs, "rt-fixture", "plan-check")
        self.assertEqual(len(prior), 2)

    def test_legacy_route_key_still_counts_as_compatibility(self):
        legacy = self._rows("plan-check", 2).replace("route_id=rt-fixture", "route=rt-fixture")
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(legacy)
            prior = N.prior_round_attempts(jobs, "rt-fixture", "plan-check")
        self.assertEqual(len(prior), 2)

    def test_unstarted_registration_residue_does_not_spend_or_bind_rounds(self):
        registrations = "".join(
            f"2026-10-01T00:00:0{i}Z\tdone\t/repo\t/wt\tstage\t"
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            f"route_id=rt-fixture,route_node=plan-check,attempt_id=att-unstarted-{i},"
            "launch_claimed=0,launch_outcome=never-launched,"
            "failure_class=cancelled,note=cancelled-by-parent\n"
            for i in (1, 2)
        )
        code, printed = self._run("plan-check", 0, rows=registrations)
        self.assertEqual(code, 0, printed)
        self.assertNotIn("review-round-budget-exhausted", "\n".join(printed))
        self.assertNotIn("review-verdictless-bound", "\n".join(printed))

        # The residue adds no rounds, while the real blocking verdict still
        # consumes exactly one unit and opens the ordinary correction round.
        rows = self._rows("plan-check", 1) + registrations
        code, printed = self._run("plan-check", 1, rows=rows)
        self.assertEqual(code, 0, printed)
        self.assertIn("correction_round=2", printed)

    def test_round_admission_distinguishes_live_withdrawn_and_failed_rows(self):
        node = dict(make_node(depth=1, dispatch_fallback=[]), id="plan-check",
                    kind="review-worker", unit="qa/custom-review",
                    completion_gate="code-review-fixture")
        route = make_route(node, tuples=[])
        route["effective_intensity"] = "standard"
        route = seal_route_hash(route)

        def rows(status, attempt_prefix, extra):
            return "".join(
                f"2026-10-01T00:00:0{i}Z\t{status}\t/repo\t/wt\tstage\t"
                "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                f"route_id={route['route_id']},route_node={node['id']},"
                f"attempt_id={attempt_prefix}-{i},launch_claimed=0,"
                f"launch_outcome=never-launched{extra}\n"
                for i in (1, 2)
            )

        cells = (
            (rows("open", "att-live", ""), "blocked-live", 0, 0),
            (rows("done", "att-withdrawn", ",failure_class=cancelled"), "admit", 0, 0),
            (rows("done", "att-failed", ""), "verdictless-bound", 0, 2),
        )
        for registry, state, verdicts, streak in cells:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as td:
                jobs = Path(td) / "jobs.log"
                jobs.write_text(registry, encoding="utf-8")
                result = N.admit_round(route, node, jobs, owner_attempt_id="att-owner").budget
                self.assertEqual((result.state, result.verdict_rounds,
                                  result.verdictless_streak), (state, verdicts, streak))

        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            registry = rows("open", "att-live-exclusion", "")
            jobs.write_text(registry, encoding="utf-8")
            result = N.admit_round(route, node, jobs, owner_attempt_id="att-live-exclusion-1",
                                   exclude_attempt="att-live-exclusion-1").budget
            self.assertEqual(result.state, "blocked-live")

    def test_real_adapter_close_failures_remain_verdictless_rounds(self):
        import dispatch_wrapper_common as wrapper_common
        import dispatch_contract as contract

        route = {"route_id": "rt-real-close", "effective_intensity": "standard", "nodes": []}
        node = {"id": "test", "kind": "pipeline-stage", "unit": "qa/test"}
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            rows = []
            for index, reason in enumerate(("launch-error", "capacity"), start=1):
                attempt = f"att-real-close-{index}"
                metadata = (
                    "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    "fallback_hop=same-harness-headless,"
                    f"route_id={route['route_id']},route_node={node['id']},attempt_id={attempt},"
                    "launch_claimed=0,launch_outcome=never-launched"
                )
                rows.append(f"now\topen\t/repo\t/wt\tstage-{index}\t{metadata}")
            jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")
            for index, reason in enumerate(("launch-error", "capacity"), start=1):
                attempt = f"att-real-close-{index}"
                self.assertTrue(wrapper_common.close_job_row(
                    jobs, f"stage-{index}", "/wt", reason, "", attempt,
                    materialize=lambda *_args: None,
                ))
                fields = next(line.split("\t") for line in jobs.read_text().splitlines()
                              if attempt in line)
                metadata = N.parse_registry_metadata(fields[5])
                self.assertEqual(fields[1], "done")
                self.assertEqual(metadata.get("launch_outcome"), "never-launched")
                self.assertFalse(N.ROUTE.attempt_row_withdrawn_before_launch(fields))
                self.assertFalse(contract.close_attempt_row(
                    jobs, attempt, "cancelled-by-parent",
                    evidence={"failure_class": "cancelled"},
                ))

            admitted = N.admit_round(route, node, jobs, owner_attempt_id="att-parent").budget
            self.assertEqual((admitted.state, admitted.verdict_rounds,
                              admitted.verdictless_streak), ("verdictless-bound", 0, 2))

    def test_exact_attempt_exclusion_keeps_older_rounds_with_the_same_slug(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            rows = self._rows("plan-check", 2).splitlines()
            first, current = [row.split("\t") for row in rows]
            first[4] = current[4] = "same-display-slug"
            jobs.write_text("\n".join("\t".join(row) for row in (first, current)) + "\n")
            attempt = N.parse_registry_metadata(current[5])["attempt_id"]
            prior = N.prior_round_attempts(jobs, "rt-fixture", "plan-check",
                exclude_slug="same-display-slug", exclude_attempt=attempt)
            self.assertEqual(len(prior), 1)
            self.assertEqual(prior[0][0], first)
            self.assertEqual(N.admit_round(
                {"route_id": "rt-fixture", "effective_intensity": "strong", "nodes": []},
                {"id": "plan-check", "kind": "pipeline-stage"}, jobs,
                exclude_slug="same-display-slug", exclude_attempt=attempt,
            ).budget.next_round, 2)

    def test_execute_and_report_are_not_capped(self):
        # Reproduces the rt-08dd7ba8 shape (12 execute rounds) for the anchor
        # nodes that stay outside the review budget: execute's own retry
        # mechanism is HEAD-lineage based, not round-counted (owner-scope-pack
        # §C-14 "정합 요구").
        for node_id in ("execute", "report"):
            code, printed = self._run(node_id, 5, effective_intensity="direct")
            self.assertEqual(code, 0)
            self.assertFalse([l for l in printed if l.startswith("reason=review-round-budget-exhausted")])

    def test_subsession_leg_is_excluded_from_the_cap(self):
        node = dict(make_node(depth=1, dispatch_fallback=[]), id="plan-check",
                    kind="review-worker", unit="qa/code-review", completion_gate="code-plan-check")
        route = make_route(node, tuples=[])
        route["effective_intensity"] = "direct"
        route = seal_route_hash(route)
        route["route_id"] = ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
        printed = []
        code = None
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / ".dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text(self._rows("plan-check", 5))
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            argv = ["dispatch-node.py", "--route", str(route_path), "--node", "plan-check",
                    "--adapter", "claude", "--slug", "slug-sub", "--action", "dry-run",
                    "--prompt-text", "Perform a fresh independent pass.",
                    "--subsession-id", "sub-1", "--subsession-index", "1",
                    "--subsession-count", "1", "--subsession-mode", "serial",
                    "--session-chain-id", "chain-1", "--phase-brief", "brief",
                    "--narrow-verify", "true", "--expected-round-trips", "1",
                    "--stage-authority", "0", "--attempt-id", "att-sub-1"]
            def fake_run(cmd, **kwargs):
                return mock.Mock(returncode=0)

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(N.os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}, clear=True), \
                 mock.patch.object(N.subprocess, "run", side_effect=fake_run), \
                 mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                try:
                    N.main()
                except SystemExit as exc:
                    code = exc.code
        self.assertEqual(code, 0)
        self.assertFalse([l for l in printed if l.startswith("reason=review-round-budget-exhausted")])

    def _subsession_purpose(self, rows):
        """One `test` phase dry-run over `rows`; returns (exit, forwarded purpose)."""
        node = dict(make_node(depth=1, dispatch_fallback=[]), id="test",
                    kind="pipeline-stage", unit="qa/test", completion_gate="code-test")
        route = make_route(node, tuples=[])
        launched = []
        code = None
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / ".dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text(rows)
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            argv = ["dispatch-node.py", "--route", str(route_path), "--node", "test",
                    "--adapter", "claude", "--slug", "test-gap-g1", "--action", "dry-run",
                    "--prompt-text", "Finish only the unfinished gate items.",
                    "--subsession-id", "ss-gap-1", "--subsession-index", "1",
                    "--subsession-count", "2", "--subsession-mode", "serial",
                    "--session-chain-id", "ssc-gap-1", "--phase-brief", "brief",
                    "--narrow-verify", "true", "--expected-round-trips", "1",
                    "--stage-authority", "0", "--attempt-id", "att-gap-1"]

            def fake_run(cmd, **kwargs):
                launched.append(cmd)
                return mock.Mock(returncode=0)

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(N.os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}, clear=True), \
                 mock.patch.object(N.subprocess, "run", side_effect=fake_run), \
                 mock.patch("builtins.print"):
                try:
                    N.main()
                except SystemExit as exc:
                    code = exc.code
        wrapper = [cmd for cmd in launched if any("dispatch-headless.py" in str(part) for part in cmd)]
        self.assertEqual(len(wrapper), 1, launched)
        return code, wrapper[0][wrapper[0].index("--subsession-purpose") + 1]

    def test_subsession_after_a_gate_failure_is_a_gap_retry_and_the_full_stage_stays_exhausted(self):
        # BC rt-96bab699: two settled test FAILs spent the standard cap. A
        # stage_authority=0 phase that finishes the unfinished items is still
        # admitted and recorded as the gap retry it is; a new full-stage round
        # on the same node stays exhausted.
        failed = self._rows("test", 2)
        self.assertEqual(self._subsession_purpose(failed), (0, "gap-retry"))
        code, printed = self._run("test", 2)
        self.assertEqual(code, 65)
        self.assertIn("reason=review-round-budget-exhausted", printed)
        # No gate failure yet, or a later verdict answered it: planned.
        self.assertEqual(self._subsession_purpose(""), (0, "planned"))
        passed = failed + failed.splitlines(True)[-1].replace(
            "note=dead-worker-fail,failure_class=fail", "note=completed-marker").replace("att-test-2", "att-test-3")
        self.assertEqual(self._subsession_purpose(passed), (0, "planned"))
        # A chain keeps the purpose it was admitted with: a FAIL recorded after
        # its first row does not relabel its later starts.
        chain_row = ("2026-08-24T00:00:00Z\topen\t/repo\t/wt\ttest-gap-g0\troute_id=rt-fixture,route_node=test,"
                     "stage_authority=0,session_chain_id=ssc-gap-1,attempt_id=att-gap-0\n")
        self.assertEqual(self._subsession_purpose(chain_row + failed), (0, "planned"))

    def test_subsession_after_a_blocked_round_is_a_gap_retry_too(self):
        # A BLOCKED (unfinished) round spends no round, yet the phases that finish its
        # remaining items are the gap retry of that round, not planned subdivision.
        blocked = self._rows("test", 1).replace("note=dead-worker-fail,failure_class=fail",
                                                "note=dead-worker-blocked,failure_class=blocked")
        self.assertEqual(self._subsession_purpose(blocked), (0, "gap-retry"))
        crash = blocked.splitlines(True)[-1].replace(
            "note=dead-worker-blocked,failure_class=blocked", "note=dead-exact-pid,failure_class=contract"
        ).replace("att-test-1", "att-test-crash")
        self.assertEqual(self._subsession_purpose(blocked + crash), (0, "gap-retry"))   # a death answers nothing
        passed = blocked.splitlines(True)[-1].replace(
            "note=dead-worker-blocked,failure_class=blocked", "note=completed-marker"
        ).replace("att-test-1", "att-test-pass")
        self.assertEqual(self._subsession_purpose(blocked + passed), (0, "planned"))

    def test_a_sd154_10_admit_round_is_the_one_admission_entry_on_every_surface(self):
        """A-SD154-10 parity: `dispatch-node.py`'s own `main()`, `dispatch-
        batch.py` and `stage-dispatch-fallback.py` all call the SAME
        `admit_round` (this module's live, auto-recording implementation
        `CompletionMarkerTest::test_a_sd154_10_admit_round_auto_records_once`
        exercises end to end) -- none keeps a separate `round_budget(...,
        revisions=())` call of its own that would silently never auto-record.
        """
        root = Path(__file__).resolve().parents[1]
        surfaces = {
            "utilities/dispatch-node.py": root / "utilities" / "dispatch-node.py",
            "utilities/dispatch-batch.py": root / "utilities" / "dispatch-batch.py",
            "utilities/stage-dispatch-fallback.py": root / "utilities" / "stage-dispatch-fallback.py",
        }
        missing = []
        stale = []
        for label, path in surfaces.items():
            text = path.read_text(encoding="utf-8")
            has_admit_round_call = "admit_round(" in text
            has_own_revisions_literal = "revisions=()" in text and label != "utilities/dispatch-node.py"
            if not has_admit_round_call:
                missing.append(label)
            if has_own_revisions_literal:
                stale.append(label)
        self.assertEqual(missing, [], missing)
        self.assertEqual(stale, [], stale)


class SubsessionChainSealTest(unittest.TestCase):
    """F3: a slice may only start once its chain manifest is sealed.

    W7G produced a `stage_authority=0` row carrying a chain identity that no
    manifest named. Nothing could ever aggregate it -- `complete_subsession_stage`
    reads the manifest, finds no such chain, and the stage never closes -- so the
    slice's work was done and permanently unusable. The seal is written by
    `stage-session-chain.py` after its register loop and before it starts index 1,
    so requiring it here refuses exactly the orphan and nothing legitimate.
    """

    def _start(self, *, seal, subsession_id="ss-fixture", attempt_id="att-sub-1",
               index=1):
        node = dict(make_node(depth=1, dispatch_fallback=[]), id="plan-check",
                    kind="review-worker", unit="qa/code-review",
                    completion_gate="code-plan-check")
        route = make_route(node, tuples=[])
        route["effective_intensity"] = "direct"
        printed = []
        code = None
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / ".dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text("")
            if seal is not None:
                pointer = jobs.parent / "session_chains" / "ssc-fixture.json"
                pointer.parent.mkdir(parents=True, exist_ok=True)
                pointer.write_text(json.dumps(seal), encoding="utf-8")
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            argv = ["dispatch-node.py", "--route", str(route_path), "--node",
                    "plan-check", "--adapter", "claude", "--slug", "slug-sub",
                    "--action", "start",
                    "--prompt-text", "Perform a fresh independent pass.",
                    "--subsession-id", subsession_id,
                    "--subsession-index", str(index),
                    "--subsession-count", "5", "--subsession-mode", "serial",
                    "--session-chain-id", "ssc-fixture", "--phase-brief", "brief",
                    "--narrow-verify", "true", "--expected-round-trips", "1",
                    "--stage-authority", "0", "--attempt-id", attempt_id]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(N.os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)},
                                 clear=True), \
                 mock.patch.object(N.subprocess, "run",
                                   side_effect=lambda cmd, **kw: mock.Mock(returncode=0)), \
                 mock.patch("builtins.print",
                            side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                try:
                    N.main()
                except SystemExit as exc:
                    code = exc.code
        return code, printed

    @staticmethod
    def _manifest(**session):
        base = {"subsession_id": "ss-fixture", "index": 1, "attempt_id": "att-sub-1"}
        base.update(session)
        return {"chain_id": "ssc-fixture", "mode": "serial", "sessions": [base]}

    def _assert_refused(self, code, printed):
        self.assertEqual(code, 64)
        self.assertIn("reason=subsession-chain-manifest-unsealed", printed)
        self.assertIn("child_spawned=0", printed)

    def test_orphan_slice_start_is_refused(self):
        self._assert_refused(*self._start(seal=None))

    def test_manifest_that_does_not_name_this_slice_is_refused(self):
        self._assert_refused(
            *self._start(seal=self._manifest(subsession_id="ss-someone-else"))
        )

    def test_manifest_naming_a_different_attempt_is_refused(self):
        # The chain is sealed and names this subsession, but against another
        # attempt identity -- a replacement row must not ride an existing seal.
        self._assert_refused(
            *self._start(seal=self._manifest(attempt_id="att-other"))
        )

    def test_manifest_naming_a_different_index_is_refused(self):
        self._assert_refused(*self._start(seal=self._manifest(index=3)))

    def test_manifest_with_a_null_index_is_refused_typed(self):
        # The manifest is on-disk JSON. `"index": null` used to raise TypeError
        # and print a traceback where every other refusal in main() prints a
        # typed envelope (review round 1, item 8).
        code, printed = self._start(seal=self._manifest(index=None))
        self._assert_refused(code, printed)

    def test_sealed_slice_starts(self):
        # Control. Without this the gate could refuse everything and the three
        # refusal tests above would still pass.
        code, printed = self._start(seal=self._manifest())
        self.assertFalse(
            [line for line in printed
             if line.startswith("reason=subsession-chain-manifest-unsealed")],
            printed,
        )

    def test_register_action_is_not_gated(self):
        # `stage-session-chain.py` persists the manifest AFTER its register loop,
        # so gating `register` would refuse the legitimate path outright.
        node = dict(make_node(depth=1, dispatch_fallback=[]), id="plan-check",
                    kind="review-worker", unit="qa/code-review",
                    completion_gate="code-plan-check")
        route = make_route(node, tuples=[])
        route["effective_intensity"] = "direct"
        printed = []
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / ".dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text("")
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            argv = ["dispatch-node.py", "--route", str(route_path), "--node",
                    "plan-check", "--adapter", "claude", "--slug", "slug-sub",
                    "--action", "register",
                    "--prompt-text", "Perform a fresh independent pass.",
                    "--subsession-id", "ss-fixture", "--subsession-index", "1",
                    "--subsession-count", "5", "--subsession-mode", "serial",
                    "--session-chain-id", "ssc-fixture", "--phase-brief", "brief",
                    "--narrow-verify", "true", "--expected-round-trips", "1",
                    "--stage-authority", "0", "--attempt-id", "att-sub-1"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(N.os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)},
                                 clear=True), \
                 mock.patch.object(N.subprocess, "run",
                                   side_effect=lambda cmd, **kw: mock.Mock(returncode=0)), \
                 mock.patch("builtins.print",
                            side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                try:
                    N.main()
                except SystemExit:
                    pass
        self.assertFalse(
            [line for line in printed
             if line.startswith("reason=subsession-chain-manifest-unsealed")],
            printed,
        )

    def test_parallel_admission_persists_before_start(self):
        # SD-103 (routing-flex): parallel admission is reachable for
        # worktree-base slices and seals the chain manifest at the pointer this
        # gate reads BEFORE the first slice start. If either half regresses,
        # every parallel slice is refused here for a reason that looks nothing
        # like its cause.
        import subdivision_batch_admission as SBA
        import dispatch_subsession_advance as SUBSESSION
        self.assertIsNone(SBA.raise_if_parallel_entry_fail_closed())
        source = (Path(N.ROOT) / "utilities" / "subdivision_batch_admission.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("persist_chain_manifest(Path(jobs), admission.manifest)", source)
        self.assertEqual(
            SBA.chain_manifest_pointer_path(Path("/x/jobs.log"), "ssc-a"),
            SUBSESSION.chain_manifest_pointer_path(Path("/x/jobs.log"), "ssc-a"),
        )


class SubsessionWorktreeLaunchTest(unittest.TestCase):
    """A slice launches in the worktree its sealed manifest names; the route is
    still verified at the route cwd, and every other worker keeps the route cwd."""

    LINKED = str(FIXTURE_LINKED)

    def _launch(self, *, action="start", subsession=True, sealed_worktree=LINKED,
                declared_worktree=LINKED, adapter_args=()):
        node = dict(make_node(depth=1, dispatch_fallback=[]), id="plan-check",
                    kind="review-worker", unit="qa/code-review",
                    completion_gate="code-plan-check")
        route = make_route(node, tuples=[])
        route["effective_intensity"] = "direct"
        printed, calls = [], []
        route = seal_route_hash(route)
        route["route_id"] = ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
        code = None
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / ".dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text("")
            pointer = jobs.parent / "session_chains" / "ssc-fixture.json"
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(json.dumps({
                "chain_id": "ssc-fixture", "mode": "parallel", "worktree": sealed_worktree,
                "sessions": [{"subsession_id": "ss-fixture", "index": 1, "attempt_id": "att-sub-1"}],
            }), encoding="utf-8")
            route_path = Path(td) / "route.json"
            route_path.write_text(json.dumps(route))
            argv = ["dispatch-node.py", "--route", str(route_path), "--node", "plan-check",
                    "--adapter", "claude", "--slug", "slug-sub", "--action", action,
                    "--prompt-text", "Perform a fresh independent pass."]
            if subsession:
                argv += ["--subsession-id", "ss-fixture", "--subsession-index", "1",
                         "--subsession-count", "2", "--subsession-mode", "parallel",
                         "--session-chain-id", "ssc-fixture", "--phase-brief", "brief",
                         "--narrow-verify", "true", "--expected-round-trips", "1",
                         "--stage-authority", "0", "--attempt-id", "att-sub-1"]
                if declared_worktree:
                    argv += ["--subsession-worktree", declared_worktree]
            argv += list(adapter_args)
            argv += ["--", "--launch-lifecycle", "detached"]
            def run(cmd, **_kw):
                calls.append(list(cmd))
                return mock.Mock(returncode=0)
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(N.os.environ, {"AGENT_DISPATCH_JOBS": str(jobs)}, clear=True), \
                 mock.patch.object(N.subprocess, "run", side_effect=run), \
                 mock.patch("builtins.print",
                            side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
                try:
                    N.main()
                except SystemExit as exc:
                    code = exc.code
        return code, printed, calls

    @staticmethod
    def _value(command, flag):
        return command[command.index(flag) + 1]

    def test_subsession_start_launches_wrapper_in_sealed_manifest_worktree(self):
        code, printed, calls = self._launch()
        self.assertEqual(code, 0, printed)
        wrapper = calls[-1]
        self.assertTrue(wrapper[1].endswith("adapters/claude/bin/dispatch-headless.py"))
        self.assertEqual(self._value(wrapper, "--worktree"), self.LINKED)

    def test_subsession_start_verifies_route_at_route_cwd(self):
        _code, _printed, calls = self._launch()
        verify = [c for c in calls if "verify" in c and c[1].endswith("capability-route.py")]
        self.assertEqual(len(verify), 1)
        self.assertEqual(self._value(verify[0], "--cwd"), str(FIXTURE_WORKTREE))

    def test_subsession_start_refuses_worktree_disagreeing_with_sealed_manifest(self):
        code, printed, calls = self._launch(declared_worktree="/tmp/some-other-worktree")
        self.assertEqual(code, 64)
        self.assertIn("reason=subsession-worktree-mismatch", printed)
        self.assertIn("child_spawned=0", printed)
        self.assertFalse([c for c in calls if c[1].endswith("dispatch-headless.py")])

    def test_subsession_start_without_declared_worktree_follows_the_sealed_one(self):
        code, _printed, calls = self._launch(declared_worktree=None)
        self.assertEqual(code, 0)
        self.assertEqual(self._value(calls[-1], "--worktree"), self.LINKED)

    def test_subsession_register_uses_the_declared_worktree(self):
        code, _printed, calls = self._launch(action="register")
        self.assertEqual(code, 0)
        self.assertEqual(self._value(calls[-1], "--worktree"), self.LINKED)

    def test_subsession_worktree_is_a_protected_adapter_flag(self):
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.reject_generated_argument_overrides(["--", "--subsession-worktree", "/tmp/x"])
        self.assertEqual(ctx.exception.reason, "dispatch-generated-argument-override")
        self.assertEqual(ctx.exception.fields["flag"], "--subsession-worktree")

    def test_pair_profile_cannot_be_overridden_in_adapter_arguments(self):
        with self.assertRaises(N.DispatchNodeError) as ctx:
            N.reject_generated_argument_overrides(["--", "--pair-model-profile", "light"])
        self.assertEqual(ctx.exception.reason, "dispatch-generated-argument-override")
        self.assertEqual(ctx.exception.fields["flag"], "--pair-model-profile")

    def test_non_subsession_launch_keeps_route_cwd(self):
        code, _printed, calls = self._launch(subsession=False)
        self.assertEqual(code, 0)
        self.assertEqual(self._value(calls[-1], "--worktree"), str(FIXTURE_WORKTREE))


class ForegroundLaunchTest(unittest.TestCase):
    """A foreground-scoped start hosts the worker inside this call.

    The wrapper gets the caller's stop request and this call waits for it to
    stop the worker and close the row (5th Codex run: the launcher killed the
    wrapper mid-cleanup and the row stayed open).
    """

    def forwarded(self, **result):
        L = sys.modules["dispatch_lifecycle"]
        return L.ForwardedRun(**{"returncode": 0, "stdout": None, "stderr": None, **result})

    def test_lifecycle_is_the_explicit_adapter_argument_or_this_scope(self):
        with mock.patch.object(N, "select_launch_lifecycle", return_value="foreground-scoped") as select:
            self.assertEqual(N.requested_launch_lifecycle(["--", "--launch-lifecycle", "detached"]), "detached")
            self.assertEqual(N.requested_launch_lifecycle(["--launch-lifecycle=detached"]), "detached")
            select.assert_not_called()
            self.assertEqual(N.requested_launch_lifecycle(["--", "--log-dir", "/tmp/x"]), "foreground-scoped")

    def test_foreground_start_forwards_and_reports_a_stop(self):
        import contextlib, io, signal
        calls = []

        def forward(argv, **kwargs):
            calls.append((argv, kwargs))
            return self.forwarded(received_signal=signal.SIGINT, cleanup_incomplete=True, returncode=-9)

        out = io.StringIO()
        with mock.patch.object(N, "run_forwarding_termination", side_effect=forward), \
             mock.patch.object(N.subprocess, "run", side_effect=AssertionError("plain run")), \
             mock.patch.dict(N.os.environ, {"AGENT_OWNER_ROUTE_FILE": "/tmp/owner.json"}), \
             contextlib.redirect_stdout(out):
            code = N.run_launcher(["wrapper", "--start"], "start", "foreground-scoped")
        self.assertEqual(code, 128 + signal.SIGINT)
        self.assertEqual(out.getvalue().splitlines(), ["interrupted=1", "cleanup=incomplete"])
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["wrapper", "--start"])
        self.assertEqual((kwargs["capture"], kwargs["timeout"]), (False, None))
        self.assertNotIn("AGENT_OWNER_ROUTE_FILE", kwargs["env"])

    def test_foreground_start_without_a_stop_returns_the_wrapper_exit(self):
        import contextlib, io
        out = io.StringIO()
        with mock.patch.object(N, "run_forwarding_termination", return_value=self.forwarded(returncode=7)), \
             contextlib.redirect_stdout(out):
            self.assertEqual(N.run_launcher(["wrapper"], "start", "foreground-scoped"), 7)
        self.assertEqual(out.getvalue(), "")

    def test_other_launches_keep_the_plain_run(self):
        for action, lifecycle in (("start", "detached"), ("dry-run", "foreground-scoped"),
                                  ("register", "foreground-scoped")):
            with self.subTest(action=action, lifecycle=lifecycle), \
                 mock.patch.object(N, "run_forwarding_termination", side_effect=AssertionError("forwarded")), \
                 mock.patch.object(N.subprocess, "run", return_value=mock.Mock(returncode=3)) as run:
                self.assertEqual(N.run_launcher(["wrapper"], action, lifecycle), 3)
                run.assert_called_once()

    def test_main_prints_the_foreground_notice_first_on_a_start_only(self):
        import contextlib, io
        node = make_node(depth=1, dispatch_fallback=[])
        route = make_route(node, tuples=[])
        L = sys.modules["dispatch_lifecycle"]
        for action, expected in (("start", True), ("dry-run", False)):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as td:
                route_path = Path(td) / "route.json"
                route_path.write_text(json.dumps(route))
                argv = ["dispatch-node.py", "--route", str(route_path), "--node", "execute",
                        "--adapter", "claude", "--slug", "fg", "--action", action]
                launched = []
                err = io.StringIO()
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch.dict(N.os.environ, fixture_state_environ(td), clear=True), \
                     mock.patch.object(N, "select_launch_lifecycle", return_value="foreground-scoped"), \
                     mock.patch.object(N.subprocess, "run", side_effect=lambda cmd, **kw: (
                         launched.append(("run", cmd)) or mock.Mock(returncode=0))), \
                     mock.patch.object(N, "run_forwarding_termination", side_effect=lambda cmd, **kw: (
                         launched.append(("forwarded", cmd)) or self.forwarded())), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit):
                        N.main()
                lines = err.getvalue().splitlines()
                if expected:
                    self.assertEqual(lines[0], L.FOREGROUND_NOTICE)
                    self.assertEqual(launched[-1][0], "forwarded")
                    self.assertTrue(str(launched[-1][1][1]).endswith("adapters/claude/bin/dispatch-headless.py"))
                else:
                    self.assertNotIn(L.FOREGROUND_NOTICE, lines)
                    self.assertEqual(launched[-1][0], "run")


if __name__ == "__main__":
    unittest.main()
