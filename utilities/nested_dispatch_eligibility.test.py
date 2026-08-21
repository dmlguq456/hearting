#!/usr/bin/env python3
import argparse
import importlib.util
import os
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

P = Path(__file__).with_name("nested-dispatch-eligibility.py")
S = importlib.util.spec_from_file_location("nested_eligibility", P)
N = importlib.util.module_from_spec(S)
S.loader.exec_module(N)

ROOT = Path(__file__).resolve().parents[1]


class NestedEligibilityTest(unittest.TestCase):
    def args(self, worktree):
        return argparse.Namespace(
            parent_harness="codex",
            parent_transport="headless",
            parent_sandbox="workspace-write",
            child_harness="codex",
            launch_authority="conductor",
            worktree=worktree,
            jobs=str(Path(worktree) / ".dispatch" / "jobs.log"),
            user_disabled=False,
            prospective_standard_owner=False,
        )

    def test_codex_auth_status_is_required_without_leaking_output(self):
        result = mock.Mock(returncode=1, stdout="private account metadata", stderr="")
        with mock.patch.object(N.shutil, "which", return_value="/bin/codex"), \
             mock.patch.object(N.subprocess, "run", return_value=result):
            self.assertEqual(N.auth_check("codex"), (False, "auth-unavailable"))

    def test_nested_auth_probe_runs_inside_checked_worktree(self):
        result = mock.Mock(returncode=0, stdout="", stderr="Logged in using ChatGPT\n")
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.object(N.shutil, "which", return_value="/bin/codex"), \
             mock.patch.object(N.subprocess, "run", return_value=result) as run:
            self.assertEqual(N.auth_check("codex", worktree), (True, ""))
        self.assertEqual(run.call_args.kwargs["cwd"], Path(worktree).resolve())

    def test_codex_auth_ignores_warnings_before_valid_login_line(self):
        result = mock.Mock(
            returncode=0,
            stdout="",
            stderr=(
                "WARNING: failed to clean up stale arg0 temp dirs\n"
                "Logged in using ChatGPT\n"
            ),
        )
        with mock.patch.object(N.shutil, "which", return_value="/bin/codex"), \
             mock.patch.object(N.subprocess, "run", return_value=result):
            self.assertEqual(N.auth_check("codex"), (True, ""))

    def test_codex_auth_still_requires_zero_exit_with_valid_status_line(self):
        result = mock.Mock(
            returncode=1,
            stdout="Logged in using ChatGPT\n",
            stderr="transient failure\n",
        )
        with mock.patch.object(N.shutil, "which", return_value="/bin/codex"), \
             mock.patch.object(N.subprocess, "run", return_value=result):
            self.assertEqual(N.auth_check("codex"), (False, "auth-unavailable"))

    def test_auth_and_headless_checks_are_bounded(self):
        with mock.patch.object(N.shutil, "which", return_value="/bin/codex"), \
             mock.patch.object(
                 N.subprocess,
                 "run",
                 side_effect=subprocess.TimeoutExpired(["codex"], 20),
             ):
            self.assertEqual(N.auth_check("codex"), (False, "auth-check-timeout"))
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.object(N, "auth_check", return_value=(True, "")), \
             mock.patch.object(
                 N.subprocess,
                 "run",
                 side_effect=subprocess.TimeoutExpired(["preflight"], 180),
             ):
            self.assertEqual(
                N.command_check("codex", worktree),
                ("unsupported", "direct-headless-check", "headless-check-timeout"),
            )

    def test_codex_owner_requires_network_profile_before_command_check(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(N, "command_check") as checked:
            row = N.evaluate(self.args(worktree))
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["failure_class"], "prospective-owner-check-required")
        self.assertEqual(row["probe_scope"], "active-owner-runtime")
        self.assertEqual(row["next_check"], "--prospective-standard-owner")
        checked.assert_not_called()

    def test_user_disabled_child_is_recorded_without_runtime_probe(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(N, "command_check") as checked:
            args = self.args(worktree)
            args.child_harness = "claude"
            args.user_disabled = True
            row = N.evaluate(args)
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["probe_source"], "user-policy")
        self.assertEqual(row["failure_class"], "user-disabled")
        self.assertEqual(row["failure_scope"], "runtime-global")
        checked.assert_not_called()

    def test_prospective_codex_owner_uses_launcher_contract_without_spoofing_marker(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(
                 N, "command_check",
                 return_value=("supported", "direct-auth+headless-check", ""),
             ) as checked:
            args = self.args(worktree)
            args.prospective_standard_owner = True
            row = N.evaluate(args)
            self.assertNotIn("AGENT_NESTED_HEADLESS_NETWORK", os.environ)
        self.assertEqual(row["status"], "supported")
        self.assertEqual(
            row["probe_source"],
            "codex-prospective-standard-owner-contract+"
            "codex-prospective-standard-owner-registry-contract+"
            "direct-auth+headless-check",
        )
        self.assertEqual(row["probe_scope"], "prospective-standard-owner")
        checked.assert_called_once_with("codex", worktree)

    def test_prospective_owner_requires_the_exact_registry_path(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(N, "command_check") as checked:
            args = self.args(worktree)
            args.jobs = None
            args.prospective_standard_owner = True
            row = N.evaluate(args)
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["failure_class"], "owner-registry-path-required")
        checked.assert_not_called()

    def test_prospective_owner_rejects_an_unwritable_registry(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(N, "command_check") as checked:
            args = self.args(worktree)
            args.jobs = "/proc/1/hearting-dispatch/jobs.log"
            args.prospective_standard_owner = True
            row = N.evaluate(args)
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["failure_class"], "owner-registry-unwritable")
        checked.assert_not_called()

    def test_prospective_owner_mode_rejects_a_non_codex_owner_tuple(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(N, "command_check") as checked:
            args = self.args(worktree)
            args.parent_harness = "claude"
            args.parent_sandbox = "adapter-default"
            args.prospective_standard_owner = True
            row = N.evaluate(args)
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["failure_class"], "prospective-owner-codex-only")
        checked.assert_not_called()

    def test_prospective_owner_flag_alias_shares_the_same_dest(self):
        p = argparse.ArgumentParser()
        p.add_argument(
            "--prospective-standard-owner", "--prospective-codex-standard-owner",
            dest="prospective_standard_owner", action="store_true",
        )
        parsed = p.parse_args(["--prospective-codex-standard-owner"])
        self.assertTrue(parsed.prospective_standard_owner)

    def test_prospective_owner_probe_is_typed_codex_only_for_every_parent_harness(self):
        expectations = {
            "codex": "prospective-owner-codex-only",
            "claude": "prospective-owner-codex-only",
            "opencode": "prospective-owner-codex-only",
        }
        for parent_harness, expected_failure in expectations.items():
            with self.subTest(parent_harness=parent_harness):
                with tempfile.TemporaryDirectory() as worktree, \
                     mock.patch.dict(os.environ, {}, clear=True), \
                     mock.patch.object(
                         N, "command_check",
                         return_value=("supported", "direct-auth+headless-check", ""),
                     ) as checked:
                    args = self.args(worktree)
                    args.parent_harness = parent_harness
                    args.parent_sandbox = (
                        "workspace-write" if parent_harness == "codex" else "adapter-default"
                    )
                    args.prospective_standard_owner = True
                    row = N.evaluate(args)
                if parent_harness == "codex":
                    # the codex owner tuple is well-formed here, so the prospective
                    # check proceeds past the codex-only gate into command_check.
                    self.assertNotEqual(row["failure_class"], expected_failure)
                else:
                    self.assertEqual(row["status"], "unsupported")
                    self.assertEqual(row["failure_class"], expected_failure)
                    checked.assert_not_called()

    def test_prospective_owner_mode_cannot_bypass_an_active_dispatch_marker(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {"AGENT_DISPATCH_DEPTH": "1"}, clear=True), \
             mock.patch.object(N, "command_check") as checked:
            args = self.args(worktree)
            args.prospective_standard_owner = True
            row = N.evaluate(args)
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["failure_class"], "prospective-owner-check-inside-dispatch")
        checked.assert_not_called()

    def test_checked_owner_profile_and_auth_surface_is_supported(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {"AGENT_NESTED_HEADLESS_NETWORK": "1"}, clear=True), \
             mock.patch.object(N, "command_check", return_value=("supported", "direct-auth+headless-check", "")):
            row = N.evaluate(self.args(worktree))
        self.assertEqual(row["status"], "supported")
        self.assertEqual(
            row["probe_source"],
            "codex-owner-network-contract+direct-auth+headless-check",
        )
        self.assertEqual(row["checked_worktree"], str(Path(worktree).resolve()))
        self.assertEqual(row["failure_scope"], "none")
        self.assertEqual(row["codex_command"], "ok")
        self.assertEqual(row["retry_on_isolated_worktree"], 0)

    def test_worktree_local_failure_is_retryable_without_hiding_codex(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {"AGENT_NESTED_HEADLESS_NETWORK": "1"}, clear=True), \
             mock.patch.object(
                 N, "command_check",
                 return_value=(
                     "unsupported", "direct-headless-check",
                     "invalid-worktree-codex-mount-target",
                 ),
             ):
            row = N.evaluate(self.args(worktree))
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["failure_scope"], "exact-worktree")
        self.assertEqual(row["codex_command"], "ok")
        self.assertEqual(row["retry_on_isolated_worktree"], 1)

    def test_command_unavailable_is_runtime_global_not_worktree_local(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {"AGENT_NESTED_HEADLESS_NETWORK": "1"}, clear=True), \
             mock.patch.object(
                 N, "command_check",
                 return_value=("unsupported", "direct-auth-check", "command-unavailable"),
             ):
            row = N.evaluate(self.args(worktree))
        self.assertEqual(row["failure_scope"], "runtime-global")
        self.assertEqual(row["codex_command"], "unavailable")
        self.assertEqual(row["retry_on_isolated_worktree"], 0)

    def test_preflight_reason_word_becomes_the_failure_class(self):
        # A route reads `failure_class` back to decide whether another hop is
        # worth attempting, so it must carry the preflight's own enum rather
        # than a joined diagnostic blob.
        result = mock.Mock(
            returncode=65,
            stdout=("check=failed\nreason=invalid-worktree-codex-mount-target\n"
                    "detail=.codex must be a directory while the Codex sandbox is enabled\n"),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.object(N, "auth_check", return_value=(True, "")), \
             mock.patch.object(N.subprocess, "run", return_value=result):
            self.assertEqual(
                N.command_check("codex", worktree),
                ("unsupported", "direct-headless-check",
                 "invalid-worktree-codex-mount-target"),
            )

    def test_unstructured_preflight_failure_keeps_the_joined_detail(self):
        result = mock.Mock(returncode=69, stdout="", stderr="boom\nsecond line")
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.object(N, "auth_check", return_value=(True, "")), \
             mock.patch.object(N.subprocess, "run", return_value=result):
            self.assertEqual(
                N.command_check("codex", worktree),
                ("unsupported", "direct-headless-check", "boom;second line"),
            )

    def test_runtime_surface_label_is_not_a_transport_tuple_value(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.object(N, "command_check") as checked:
            args = self.args(worktree)
            args.parent_transport = "codex-exec-headless"
            row = N.evaluate(args)
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["failure_class"], "noncanonical-parent-transport")
        checked.assert_not_called()

    def test_opencode_depth2_child_reaches_the_same_runtime_probe(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(
                 N, "command_check",
                 return_value=("supported", "direct-auth+headless-check", ""),
             ) as checked:
            args = self.args(worktree)
            args.parent_harness = "claude"
            args.parent_sandbox = "adapter-default"
            args.child_harness = "opencode"
            row = N.evaluate(args)
        self.assertEqual(row["status"], "supported")
        self.assertEqual(row["probe_source"], "direct-auth+headless-check")
        checked.assert_called_once_with("opencode", args.worktree)

    def test_unknown_parent_sandbox_label_fails_before_runtime_probe(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.object(N, "command_check") as checked:
            args = self.args(worktree)
            args.parent_harness = "claude"
            args.parent_sandbox = "none"
            row = N.evaluate(args)
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["probe_source"], "parent-sandbox-vocabulary")
        self.assertEqual(row["failure_class"], "parent-sandbox-label-unknown")
        checked.assert_not_called()

    def test_auto_parent_sandbox_resolves_the_wrapper_export(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.object(
                 N, "command_check",
                 return_value=("supported", "direct-command-check", ""),
             ):
            args = self.args(worktree)
            args.parent_harness = "claude"
            args.parent_sandbox = "auto"
            row = N.evaluate(args)
        self.assertEqual(row["status"], "supported")
        self.assertEqual(row["parent_sandbox"], "adapter-default")

    def test_callers_own_interactive_transport_fails_before_runtime_probe(self):
        # 2026-08-04 cairn: the depth-0 session filled in its own
        # transport. Canonical vocabulary, wrong subject.
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.object(N, "command_check") as checked:
            args = self.args(worktree)
            args.parent_transport = "interactive"
            row = N.evaluate(args)
        self.assertEqual(row["status"], "unsupported")
        self.assertEqual(row["probe_source"], "parent-transport-vocabulary")
        self.assertEqual(row["failure_class"], "parent-transport-not-registered-headless")
        checked.assert_not_called()

    def test_auto_parent_transport_resolves_the_depth1_owner_not_the_caller(self):
        # No wrapper export means a depth-0 caller about to launch the depth-1
        # owner; that owner is registered headless by construction.
        self.assertEqual(N.resolve_parent_transport("auto", {}), ("headless", ""))
        self.assertEqual(
            N.resolve_parent_transport("auto", {"AGENT_DISPATCH_CURRENT_TRANSPORT": "headless"}),
            ("headless", ""),
        )
        self.assertEqual(N.resolve_parent_transport("headless", {}), ("headless", ""))

    def test_auto_parent_harness_needs_a_wrapper_export(self):
        # Unlike transport, the owner's adapter is a later dispatch-owner
        # decision, so `auto` fails closed instead of guessing the caller's.
        self.assertEqual(
            N.resolve_parent_harness("auto", {"AGENT_DISPATCH_CURRENT_HARNESS": "codex"}),
            ("codex", ""),
        )
        self.assertEqual(
            N.resolve_parent_harness("auto", {}), ("auto", "parent-harness-underivable")
        )
        self.assertEqual(N.resolve_parent_harness("claude", {}), ("claude", ""))

    def test_underivable_parent_harness_is_reported_before_sandbox_lookup(self):
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(N, "command_check") as checked:
            args = self.args(worktree)
            args.parent_harness = "auto"
            row = N.evaluate(args)
        self.assertEqual(row["probe_source"], "parent-harness-vocabulary")
        self.assertEqual(row["failure_class"], "parent-harness-underivable")
        checked.assert_not_called()

    def test_codex_dynamic_sandbox_labels_stay_accepted(self):
        for label in ("workspace-write", "danger-full-access", "read-only"):
            self.assertEqual(
                N.resolve_parent_sandbox("codex", label), (label, "")
            )
        self.assertEqual(
            N.resolve_parent_sandbox("codex", "auto"), ("workspace-write", "")
        )


class SandboxUsernsProbeTest(unittest.TestCase):
    """SD-48: `headless --check` must prove sandbox init, not just auth+shape.

    Item 3 of installer-probes-dispatch-fixes -- observed 2026-08-21 that a
    supported/direct-auth+headless-check tuple still died dead-worker-blocked
    because bwrap userns setup failed on the host. The reason word
    `sandbox-userns-unavailable` is deliberately absent from
    `WORKTREE_LOCAL_FAILURES`, so no eligibility code change is required --
    the fallthrough in `failure_diagnostics` alone must classify it
    `runtime-global`/`retry_on_isolated_worktree=0`.
    """

    def test_sealed_probe_becomes_unsupported_direct_headless_check(self):
        result = mock.Mock(
            returncode=69,
            stdout=(
                "check=failed\nreason=sandbox-userns-unavailable\n"
                "detail=bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted\n"
                "failure_scope=runtime-global\ncodex_command=ok\n"
                "retry_on_isolated_worktree=0\n"
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as worktree, \
             mock.patch.object(N, "auth_check", return_value=(True, "")), \
             mock.patch.object(N.subprocess, "run", return_value=result):
            self.assertEqual(
                N.command_check("codex", worktree),
                ("unsupported", "direct-headless-check", "sandbox-userns-unavailable"),
            )

    def test_sandbox_userns_unavailable_is_runtime_global_not_worktree_local(self):
        # WORKTREE_LOCAL_FAILURES must NOT gain this reason -- that omission is
        # the mechanism by which the fallthrough yields runtime-global.
        self.assertNotIn("sandbox-userns-unavailable", N.WORKTREE_LOCAL_FAILURES)
        self.assertEqual(
            N.failure_diagnostics(
                "codex", "unsupported", "direct-headless-check",
                "sandbox-userns-unavailable",
            ),
            ("runtime-global", "ok", 0),
        )

    def _worktree(self, tmp):
        worktree = Path(tmp) / "repo"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        return worktree

    def _stub(self, bin_dir, name, body):
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
        return stub

    def _path_hiding_bwrap(self, worktree):
        """Real PATH dirs, minus any directory that itself contains `bwrap`.

        `command -v bwrap` only fails to find the binary if no PATH directory
        has it -- prepending a stub dir is not enough when the real `bwrap`
        also sits in an inherited PATH directory (e.g. `/usr/bin`), so this
        drops any such directory wholesale from the search path instead.
        """
        kept = []
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            if (Path(entry) / "bwrap").exists():
                continue
            kept.append(entry)
        return os.pathsep.join(kept)

    def _preflight_check(self, worktree, bwrap_rc=None, extra_env=None):
        """Run the real `preflight.sh headless --check` with stub `codex`/`bwrap`."""
        bin_dir = worktree.parent / "stub-bin"
        self._stub(bin_dir, "codex", "#!/bin/sh\nexit 0\n")
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENT_DISPATCH_CHILD", "CODEX_DISPATCH_SANDBOX_FORCE")}
        env["AGENT_HOME"] = str(ROOT)
        if bwrap_rc is None:
            env["PATH"] = "%s:%s" % (bin_dir, self._path_hiding_bwrap(worktree))
        else:
            self._stub(
                bin_dir, "bwrap",
                f"#!/bin/sh\necho 'stub bwrap failure line' >&2\nexit {bwrap_rc}\n"
                if bwrap_rc != 0 else "#!/bin/sh\nexit 0\n",
            )
            env["PATH"] = "%s:%s" % (bin_dir, env.get("PATH", ""))
        env.update(extra_env or {})
        return subprocess.run(
            [str(ROOT / "adapters/codex/bin/preflight.sh"), "headless", "--check", str(worktree)],
            text=True, capture_output=True, env=env, timeout=60,
        )

    def test_real_probe_seals_when_stub_bwrap_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = self._worktree(tmp)
            result = self._preflight_check(worktree, bwrap_rc=1)
        self.assertEqual(result.returncode, 69, result.stdout + result.stderr)
        self.assertIn("reason=sandbox-userns-unavailable", result.stdout)

    def test_real_probe_passes_when_stub_bwrap_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = self._worktree(tmp)
            result = self._preflight_check(worktree, bwrap_rc=0)
        self.assertNotIn("sandbox-userns-unavailable", result.stdout)

    def test_real_probe_passes_when_bwrap_binary_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = self._worktree(tmp)
            result = self._preflight_check(worktree, bwrap_rc=None)
        self.assertNotIn("sandbox-userns-unavailable", result.stdout)

    def test_real_probe_honors_suppression_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = self._worktree(tmp)
            result = self._preflight_check(
                worktree, bwrap_rc=1, extra_env={"AGENT_DISPATCH_CHILD": "1"},
            )
        self.assertNotIn("sandbox-userns-unavailable", result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            worktree = self._worktree(tmp)
            result = self._preflight_check(
                worktree, bwrap_rc=1,
                extra_env={"CODEX_DISPATCH_SANDBOX_FORCE": "danger-full-access"},
            )
        self.assertNotIn("sandbox-userns-unavailable", result.stdout)


if __name__ == "__main__":
    unittest.main()
