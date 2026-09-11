#!/usr/bin/env python3
"""The same parent/child matrix exercises all three real adapter boundaries."""
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from contextlib import ExitStack, redirect_stdout
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_parent_completion as P


def adapter(name):
    spec = importlib.util.spec_from_file_location(
        "parent_delivery_" + name, ROOT / "adapters" / name / "bin" / "dispatch-headless.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ADAPTERS = {name: adapter(name) for name in ("claude", "codex", "opencode")}


def args(parent="codex", **values):
    return SimpleNamespace(**{
        "action": "start", "dispatch_depth": 1, "launch_lifecycle": "detached",
        "execution_surface": "registered-headless", "registered_worker": True,
        "parent_session_id": "thread-parent", "parent_harness": parent,
        "attempt_id": "att-parent-contract", "attempt_claimed": True,
        **values,
    })


class ParentDeliveryContract(unittest.TestCase):
    def test_parent_runtime_selects_delivery_for_every_child_and_worker_type(self):
        for child, wrapper in ADAPTERS.items():
            for parent, delivery in (("codex", P.MANAGED_PARENT_DELIVERY),
                                     ("claude", "claude-parent-runtime"),
                                     ("opencode", "poll-fallback")):
                for worker in ("owner", "review", "frame", "stage", "support"):
                    with self.subTest(child=child, parent=parent, worker=worker):
                        request = args(parent, worker_type=worker)
                        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-parent"}, clear=True), \
                             mock.patch.object(wrapper, "probe_managed_codex_parent",
                                               return_value=SimpleNamespace(thread_advanced=False)) as probe:
                            self.assertEqual(wrapper.resolve_parent_completion_delivery(request), delivery)
                            self.assertEqual(probe.call_count, int(parent == "codex"))

    def test_witnessed_thread_successor_is_used_by_every_child(self):
        for child, wrapper in ADAPTERS.items():
            with self.subTest(child=child), \
                 mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-parent"}, clear=True), \
                 mock.patch.object(wrapper, "probe_managed_codex_parent",
                    return_value=SimpleNamespace(thread_advanced=True, thread_id="thread-successor")):
                request = args()
                self.assertEqual(wrapper.resolve_parent_completion_delivery(request), P.MANAGED_PARENT_DELIVERY)
                self.assertEqual(request.parent_session_id, "thread-successor")
                self.assertEqual(request.parent_completion_reason, "managed-thread-advanced")

    def test_registered_parent_retains_responsibility(self):
        for child, wrapper in ADAPTERS.items():
            with self.subTest(child=child), \
                 mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}, clear=True), \
                 mock.patch.object(wrapper, "probe_managed_codex_parent") as probe:
                self.assertEqual(wrapper.resolve_parent_completion_delivery(args(dispatch_depth=2)),
                                 "parent-runtime-supervised")
                probe.assert_not_called()

    def test_unproved_codex_parent_cannot_silently_launch_without_a_carrier(self):
        for child, wrapper in ADAPTERS.items():
            with self.subTest(child=child), \
                 mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-parent"}, clear=True), \
                 mock.patch.object(wrapper, "probe_managed_codex_parent",
                                   side_effect=P.ManagedDispatchError("managed-control-unavailable")):
                request = args()
                request.parent_completion_delivery = wrapper.resolve_parent_completion_delivery(request)
                with self.assertRaises(P.DispatchContractError) as caught:
                    wrapper.validate_interactive_parent_launch(request)
                self.assertEqual(caught.exception.reason, "managed-entry-required")
                request.allow_unmanaged_parent_poll = True
                wrapper.validate_interactive_parent_launch(request)
                self.assertEqual(request.parent_completion_reason, "operator-authorized-unmanaged-poll")

    def test_sidecar_owns_exact_attempt_before_spawn_and_keeps_unrecorded_obligation(self):
        for child, wrapper in ADAPTERS.items():
            for recorded in (True, False):
                with self.subTest(child=child, recorded=recorded), \
                     mock.patch.object(wrapper, "launch_managed_completion_sidecar",
                        return_value=SimpleNamespace(pid=123, sealed_batch_id="batch-exact", log_file="/tmp/log")) as launch, \
                     mock.patch.object(wrapper, "annotate_attempt_row", return_value=recorded):
                    request = args(parent_completion_delivery=P.MANAGED_PARENT_DELIVERY,
                                   managed_gateway_binding=object())
                    wrapper.launch_parent_completion_sidecar(request, Path("/tmp/jobs"))
                    self.assertEqual(launch.call_args.kwargs["attempt_ids"], {request.attempt_id})
                    self.assertEqual(launch.call_args.kwargs["parent_session_id"], "thread-parent")
                    self.assertEqual(request.managed_sidecar_state,
                                     "running" if recorded else "running-unrecorded")

    def test_sidecar_failure_cannot_be_reported_as_delivery_ready(self):
        for child, wrapper in ADAPTERS.items():
            with self.subTest(child=child), \
                 mock.patch.object(wrapper, "launch_managed_completion_sidecar",
                                   side_effect=P.ManagedDispatchError("managed-control-unavailable")), \
                 mock.patch.object(wrapper, "annotate_attempt_row", return_value=True):
                request = args(parent_completion_delivery=P.MANAGED_PARENT_DELIVERY,
                               managed_gateway_binding=object())
                wrapper.launch_parent_completion_sidecar(request, Path("/tmp/jobs"))
                self.assertEqual(request.managed_sidecar_state, "launch-failed")

    def test_registration_transport_is_immutable_at_start(self):
        request = args(parent_completion_delivery=P.MANAGED_PARENT_DELIVERY)
        with self.assertRaises(P.DispatchContractError) as caught:
            P.validate_registered_delivery(request, Path("/tmp/jobs"), read=lambda *_: "poll-fallback")
        self.assertEqual(caught.exception.reason, "attempt-parent-delivery-changed")

    def test_real_adapter_start_claims_then_arms_carrier_before_any_worker_spawn(self):
        """Exercise each actual parser/main, registry claim and failure cleanup."""
        for child, wrapper in ADAPTERS.items():
            with self.subTest(child=child), tempfile.TemporaryDirectory() as td, ExitStack() as stack:
                root = Path(td)
                worktree = root / "repo"
                worktree.mkdir()
                subprocess.run(["git", "init", "-q", str(worktree)], check=True)
                jobs = root / "jobs.log"
                artifacts = root / "artifacts"
                artifacts.mkdir()
                attempt = "att-carrier-" + child
                environment = {
                    "PATH": os.environ["PATH"], "HOME": str(root),
                    "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(jobs),
                    "AGENT_DISPATCH_PARENT_SESSION_ID": "thread-parent",
                    "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                    "AGENT_DISPATCH_CURRENT_TRANSPORT": "interactive",
                    "AGENT_DISPATCH_CURRENT_SANDBOX": "default",
                    "CODEX_THREAD_ID": "thread-parent",
                }
                stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
                stack.enter_context(mock.patch.object(wrapper, "resolve_artifact_root", return_value=str(artifacts)))
                if hasattr(wrapper, "check_runtime_projection"):
                    stack.enter_context(mock.patch.object(wrapper, "check_runtime_projection", return_value=0))
                stack.enter_context(mock.patch.object(wrapper.shutil, "which", return_value="/bin/runtime"))
                stack.enter_context(mock.patch.object(wrapper, "shell_command", return_value="true"))
                stack.enter_context(mock.patch.object(wrapper, "reserve_governor_token", return_value=("reserved", {})))
                cancel = stack.enter_context(mock.patch.object(wrapper, "cancel_governor_reservation"))
                spawn = stack.enter_context(mock.patch.object(wrapper, "spawn_claimed_attempt",
                    side_effect=RuntimeError("worker spawn must not precede its completion carrier")))
                stack.enter_context(mock.patch.object(wrapper, "probe_managed_codex_parent",
                    return_value=SimpleNamespace(thread_advanced=False)))

                def launch(**values):
                    self.assertEqual(values["attempt_ids"], {attempt})
                    row = jobs.read_text()
                    self.assertIn("attempt_id=" + attempt, row)
                    self.assertIn("parent_completion_delivery=" + P.MANAGED_PARENT_DELIVERY, row)
                    self.assertNotIn("launch_started=1", row)
                    raise P.ManagedDispatchError("fixture-carrier-unavailable")

                sidecar = stack.enter_context(mock.patch.object(wrapper, "launch_managed_completion_sidecar", side_effect=launch))
                cli = [
                    "dispatch-headless.py", "--start", "--worktree", str(worktree),
                    "--jobs", str(jobs), "--log-dir", str(root / "logs"),
                    "--slug", "carrier-test", "--capability", "autopilot-code",
                    "--capability-mode", "debug", "--worker-mode", "dev/backend",
                    "--worker-type", "review", "--dispatch-depth", "1",
                    "--parent-harness", "codex", "--parent-session-id", "thread-parent",
                    "--model", "test", "--attempt-id", attempt,
                ]
                if child == "codex":
                    cli += ["--reasoning", "low", "--completion-delivery", "poll"]
                elif child == "opencode":
                    cli += ["--variant", "low"]
                else:
                    cli += ["--effort", "low"]
                output = io.StringIO()
                with redirect_stdout(output):
                    result = wrapper.main(cli)
                self.assertEqual(result, 75, output.getvalue())
                self.assertIn("reason=managed-sidecar-launch-failed", output.getvalue())
                self.assertIn("child_spawned=0", output.getvalue())
                sidecar.assert_called_once()
                spawn.assert_not_called()
                cancel.assert_called_once()
                row = jobs.read_text()
                self.assertIn("launch_outcome=never-launched", row)
                self.assertIn("note=dead-managed-sidecar-launch-failed", row)
                self.assertNotIn("launch_started=1", row)


if __name__ == "__main__":
    unittest.main()
