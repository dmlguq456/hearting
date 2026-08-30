#!/usr/bin/env python3
"""SD-66 fix-forward: dispatch-node.py record -> wrapper-argument binding."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
P = Path(__file__).with_name("dispatch-node.py")
S = importlib.util.spec_from_file_location("dispatch_node", P)
N = importlib.util.module_from_spec(S)
S.loader.exec_module(N)


def base_tuple(child_harness, status="supported", probe_source="fixture-check", failure_class="", parent=None):
    row = {
        "child_harness": child_harness,
        "checked_worktree": "/tmp/fixture-worktree",
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
    return {
        "cwd": "/tmp/fixture-worktree",
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
            captured_stdout = io.StringIO()
            with mock.patch.object(sys, "argv", full_argv), \
                 mock.patch.dict(N.os.environ, {}, clear=True), \
                 mock.patch.object(N.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                 contextlib.redirect_stdout(captured_stdout):
                try:
                    N.main()
                except SystemExit:
                    pass
                jobs = N.resolve_global_registry(
                    N.ROOT, None, 1, "dry-run", {}
                ).path
                expected = str(
                    N.ROUTE.completion_dir(route["route_id"], jobs=jobs)
                    / (node["id"] + ".json")
                )
        lines = captured_stdout.getvalue().splitlines()
        marker_lines = [line for line in lines if line.startswith("completion_marker=")]
        self.assertEqual(len(marker_lines), 1, lines)
        printed = marker_lines[0][len("completion_marker="):]
        self.assertEqual(printed, expected)


class MainMaterializationTest(unittest.TestCase):
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
            full_argv = ["dispatch-node.py", "--route", str(route_path)] + argv
            with mock.patch.object(sys, "argv", full_argv), \
                 mock.patch.dict(N.os.environ, environ or {}, clear=True), \
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
    """Batched-correction rounds: a node redispatched after prior attempts of the
    same route/node gets a `Round protocol` block stamped into its assignment,
    scoping a review to a closure check and a stage to one batched fix."""

    def _rows(self, node, n, extra=""):
        pipe = ("capability=autopilot-code,attempt_schema_version=2,registered_worker=1,"
                "route_id=rt-fixture,route_node=" + node + ",note=dead-worker-fail" + extra)
        return "".join(
            f"2026-08-24T00:00:0{i}Z\tdone\t/repo\t/wt\tslug-r{i}\t{pipe},attempt_id=att-{node}-{i}\n"
            for i in range(1, n + 1)
        )

    def _launch(self, node, rows, slug="slug-next", extra_argv=()):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["argv"] = cmd
            return mock.Mock(returncode=0)

        route = make_route(node, tuples=[])
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
                    kind="review-worker", unit="qa/code-review", completion_gate="code-impl-review")
        prompt, _ = self._launch(node, self._rows("impl-review", 1) + self._rows("execute", 5))
        self.assertIn("Round protocol (round 2 of route node `impl-review`", prompt)
        self.assertIn("closed/open/regressed", prompt)
        self.assertIn("even if the assignment above asks for a fresh independent review", prompt)
        self.assertTrue(prompt.startswith("Perform a fresh independent pass."))

    def test_own_slug_subsession_and_other_routes_are_not_rounds(self):
        node = make_node(depth=1, dispatch_fallback=[])
        rows = (self._rows("execute", 1).replace("slug-r1", "slug-next")
                + self._rows("execute", 1, extra=",stage_authority=0").replace("att-execute-1", "att-sub")
                + self._rows("execute", 1).replace("route_id=rt-fixture", "route_id=rt-other").replace("att-execute-1", "att-other"))
        prompt, _ = self._launch(node, rows)
        self.assertNotIn("Round protocol", prompt)


class ReviewRoundCapTest(unittest.TestCase):
    """C-14: plan-check/impl-review/test rounds are capped by tier-derived retry
    budget (CONVENTIONS §1.1), not left unbounded. `execute`/`report` carry no
    cap here -- their retry mechanism is HEAD-lineage based, not round-counted."""

    def _rows(self, node, n):
        pipe = ("capability=autopilot-code,attempt_schema_version=2,registered_worker=1,"
                "route_id=rt-fixture,route_node=" + node + ",note=dead-worker-fail")
        return "".join(
            f"2026-08-24T00:00:0{i}Z\tdone\t/repo\t/wt\tslug-r{i}\t{pipe},attempt_id=att-{node}-{i}\n"
            for i in range(1, n + 1)
        )

    def _run(self, node_id, prior_count, *, effective_intensity="standard", slug="slug-next"):
        node = dict(make_node(depth=1, dispatch_fallback=[]), id=node_id,
                    kind="review-worker", unit="qa/code-review", completion_gate="code-" + node_id)
        route = make_route(node, tuples=[])
        route["effective_intensity"] = effective_intensity
        printed = []
        code = None
        launched = []

        def fake_run(cmd, **kwargs):
            launched.append(cmd)
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / ".dispatch" / "jobs.log"
            jobs.parent.mkdir()
            jobs.write_text(self._rows(node_id, prior_count))
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


if __name__ == "__main__":
    unittest.main()
