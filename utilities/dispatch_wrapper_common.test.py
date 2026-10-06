#!/usr/bin/env python3
"""The helpers the three dispatch wrappers shared letter for letter now live once (audit §4 #10)."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_wrapper_common as C  # noqa: E402

ALIASES = {
    "fail": "fail", "jobs_lock": "jobs_lock", "process_start_ticks": "process_start_ticks",
    "read_launch_fence_failure": "read_launch_fence_failure", "resolve_artifact_root": "resolve_artifact_root",
    "_is_report_bundle_publish_stage": "is_report_bundle_publish_stage",
    "resolve_report_bundle_root": "resolve_report_bundle_root", "seed_launch_heartbeat": "seed_launch_heartbeat",
    "_route_node_leg_fields": "route_node_leg_fields", "_supervisor_route": "supervisor_route",
    "prepare_review_output_request": "prepare_review_output_request",
    "watch_early_death": "watch_early_death",
    "write_reset_cache": "write_reset_cache",
    "diff_attribution_prompt": "diff_attribution_prompt",
}


def load(harness: str):
    spec = importlib.util.spec_from_file_location(
        f"{harness}_dispatch_headless_common", ROOT / "adapters" / harness / "bin" / "dispatch-headless.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WrapperCommonTest(unittest.TestCase):
    def test_every_wrapper_keeps_the_old_names_as_the_shared_functions(self):
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            for old, new in ALIASES.items():
                with self.subTest(harness=harness, name=old):
                    self.assertIs(getattr(wrapper, old), getattr(C, new))

    def test_a_few_behaviors(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(C.fail("x-reason", 64, detail="d"), 64)
        self.assertEqual(out.getvalue(), "check=failed\nreason=x-reason\ndetail=d\n")
        self.assertEqual(C.process_start_ticks(-1), "")
        owner = argparse.Namespace(owner_route_binding=None, route_file="r", route_id="i",
                                   route_hash="h", route_node="one-shot")
        self.assertEqual(C.supervisor_route(owner), ("r", "i", "h"))
        owner.route_node = "execute"
        self.assertIsNone(C.supervisor_route(owner))
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            with C.jobs_lock(jobs):
                self.assertTrue(Path(f"{jobs}.lock").exists())


    def test_the_harness_name_is_the_only_translation(self):
        from unittest import mock
        for harness in ("claude", "codex", "opencode"):
            wrapper = load(harness)
            with self.subTest(harness=harness):
                with mock.patch.object(C, "model_config_state", return_value=("s", "r")) as state:
                    self.assertEqual(wrapper._model_config_state(), ("s", "r"))
                state.assert_called_once_with(harness)
                args = argparse.Namespace()
                with mock.patch.object(C, "bind_internal_eligibility_probe") as probe:
                    wrapper.bind_internal_eligibility_probe(args)
                probe.assert_called_once_with(args, harness)

    def test_owner_input_opens_only_for_a_supervised_owner(self):
        from unittest import mock
        args = argparse.Namespace(attempt_id="att-1")
        with mock.patch("dispatch_owner_input.initialize_owner_input") as init:
            C.initialize_owner_input_when(args, Path("/j"), supervised=False, input_kind="k")
            init.assert_not_called()
            C.initialize_owner_input_when(args, Path("/j"), supervised=True, input_kind="k")
            init.assert_called_once_with(Path("/j"), "att-1", "k")

    def test_diff_attribution_reads_the_exact_registry_it_was_given(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / "route.json"
            route.write_text(json.dumps({"nodes": [{"id": "test"}]}), encoding="utf-8")
            custom = Path(tmp) / "custom-registry.log"
            args = argparse.Namespace(route_file=str(route), route_node="test", jobs=str(custom),
                                      agent_home=Path(tmp))
            with mock.patch.object(C, "diff_attribution_lines", return_value=["diff_base: x"]) as lines:
                self.assertEqual(C.diff_attribution_prompt(args), "- diff_base: x\n")
            self.assertEqual(lines.call_args.args[2], custom)
            args.jobs = None
            with mock.patch.dict("os.environ", {}, clear=False), \
                 mock.patch.dict("os.environ", {"AGENT_DISPATCH_JOBS": ""}), \
                 mock.patch.object(C, "resolve_dispatch_state_root",
                                   side_effect=C.DispatchContractError("registry-ambiguous", "x")):
                self.assertEqual(C.diff_attribution_prompt(args), "")

    def test_the_reset_cache_is_best_effort(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "state" / "jobs.log"
            C.write_reset_cache(Path(tmp), "codex", "usage-limit", "12:00", jobs)
            self.assertTrue((jobs.parent / "usage-reset.codex").read_text().endswith(" usage-limit 12:00\n"))
            with mock.patch.object(C, "dispatch_state_roots",
                                   side_effect=C.DispatchContractError("registry-ambiguous", "x")):
                C.write_reset_cache(Path(tmp), "claude", "usage-limit", "12:00")  # no raise


class RegistrationFenceTest(unittest.TestCase):
    def test_every_wrapper_registers_behind_the_terminal_claim_fence(self):
        import ast
        for harness in ("claude", "codex", "opencode"):
            tree = ast.parse((ROOT / "adapters" / harness / "bin" / "dispatch-headless.py").read_text(encoding="utf-8"))
            append_job = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "append_job")
            claims = [n for n in ast.walk(append_job) if isinstance(n, ast.Call)
                      and getattr(n.func, "id", None) == "claim_attempt_row"]
            with self.subTest(harness=harness):
                self.assertEqual(len(claims), 1)
                self.assertIn("mutation_precheck", {k.arg for k in claims[0].keywords})
                self.assertIn("ensure_terminal_claim_absent", ast.unparse(append_job))

if __name__ == "__main__":
    unittest.main()
