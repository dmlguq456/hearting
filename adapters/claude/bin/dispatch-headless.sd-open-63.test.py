#!/usr/bin/env python3
"""SD-OPEN-63 regression: `claude --help` supervision probe must not degrade
silently on a PIPE-truncated substring match. Every branch spawns the real
`probe_claude_session_resume()` -> real `subprocess.run` -> a fake `claude`
binary placed first on PATH; no `WH.probe_claude_session_resume`/
`claude_session_resume_available` mock is used anywhere in this file.
"""
import argparse
import ast
import importlib.util
import os
import stat
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WH_S = importlib.util.spec_from_file_location(
    "claude_dispatch_headless_sd63", Path(__file__).with_name("dispatch-headless.py")
)
WH = importlib.util.module_from_spec(WH_S)
WH_S.loader.exec_module(WH)


_FAKE_CLAUDE_TEMPLATE = """#!/usr/bin/env python3
import sys, os, stat, time

def _is_pipe():
    try:
        return stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode)
    except OSError:
        return True

if len(sys.argv) >= 2 and sys.argv[1] == "--help":
{body}
    sys.exit(0)
sys.exit(1)
"""

_HELP_BODY_TAIL = (
    "  --session-id <uuid>   Use a specific session ID for the conversation\n"
    "  update|upgrade        Check for updates and install if available\n"
)
_HELP_BODY_HEAD = "  -r, --resume [value]  Resume a conversation by session ID, or\n"
_PAD = "pad " * 5200  # pushes the complete-help fixture size past the min-bytes sentinel


def _write_fake_claude(bindir: Path, body: str) -> None:
    indented_body = textwrap.indent(body.rstrip("\n"), "    ")
    script = _FAKE_CLAUDE_TEMPLATE.format(body=indented_body)
    target = bindir / "claude"
    target.write_text(script)
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _complete_help_source(has_resume: bool, has_session_id: bool) -> str:
    head = _HELP_BODY_HEAD if has_resume else ""
    tail = _HELP_BODY_TAIL if has_session_id else "  update|upgrade  Check for updates and install if available\n"
    full = f"Usage: claude [options]\n{head}{_PAD}\n{tail}"
    return textwrap.dedent(f"""\
    full = {full!r}
    data = full.encode()
    sys.stdout.buffer.write(data)
    """)


def _truncated_help_source() -> str:
    # SD-OPEN-63's fix captures help through a regular file (never a PIPE), which
    # removes PIPE buffering as a truncation source entirely -- so this fixture
    # models the residual failure class the fix still has to fail closed on: the
    # captured output is short for *some* reason (binary crash mid-write, disk
    # pressure, a vendor build that emits less than expected), the same 8192-byte
    # boundary measured against the real PIPE-truncated `claude --help` in this
    # cycle's D47(e) evidence. `--session-id` never appears in the truncated slice.
    full = f"Usage: claude [options]\n{_HELP_BODY_HEAD}{_PAD}\n{_HELP_BODY_TAIL}"
    return textwrap.dedent(f"""\
    full = {full!r}
    data = full.encode()[:8192]
    sys.stdout.buffer.write(data)
    """)


_NONZERO_TEMPLATE = """#!/usr/bin/env python3
import sys
if len(sys.argv) >= 2 and sys.argv[1] == "--help":
    sys.stdout.write("nope\\n")
    sys.exit(3)
sys.exit(1)
"""


def _owner_args(**overrides):
    base = dict(
        completion_delivery="auto", dispatch_depth=1, worker_type="owner", intensity="strong",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class SD63TruncationProbe(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bindir = Path(self._tmp.name)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bindir}:{self._old_path}"

    def tearDown(self):
        os.environ["PATH"] = self._old_path
        self._tmp.cleanup()

    def test_unavailable_probe_storage_is_typed_indeterminate(self):
        _write_fake_claude(self.bindir, _complete_help_source(True, True))
        # Both named and anonymous regular-file capture must handle a missing
        # temporary directory as an uncertain probe, before spawning the CLI.
        with unittest.mock.patch.object(WH.tempfile, "tempdir", str(self.bindir / "absent")):
            probe = WH.probe_claude_session_resume()
            self.assertEqual((probe.status, probe.reason), ("indeterminate", "probe-error"))
            with self.assertRaises(WH.DispatchContractError) as caught:
                WH.resolve_completion_delivery(_owner_args())
            self.assertEqual(caught.exception.reason, "claude-session-resume-indeterminate")

    # A: complete help, both flags present -> supported
    def test_a_complete_help_both_flags_is_supported(self):
        _write_fake_claude(self.bindir, _complete_help_source(True, True))
        probe = WH.probe_claude_session_resume()
        self.assertEqual(probe.status, "supported")
        args = _owner_args()
        self.assertEqual(WH.resolve_completion_delivery(args), "session-resume-supervised")
        self.assertEqual(args.completion_delivery_reason, "ok")

    # B: PIPE-truncated help -> typed refusal (the core SD-63 fix)
    def test_b_pipe_truncated_help_is_typed_refusal_not_poll_fallback(self):
        _write_fake_claude(self.bindir, _truncated_help_source())
        probe = WH.probe_claude_session_resume()
        self.assertEqual(probe.status, "indeterminate")
        self.assertEqual(probe.reason, "help-truncated")
        args = _owner_args()
        with self.assertRaises(WH.DispatchContractError) as ctx:
            WH.resolve_completion_delivery(args)
        self.assertEqual(ctx.exception.reason, "claude-session-resume-indeterminate")

    # C: complete help, flags absent -> poll-fallback + sealed reason
    def test_c_complete_help_flags_absent_is_poll_fallback_with_reason(self):
        _write_fake_claude(self.bindir, _complete_help_source(False, False))
        args = _owner_args()
        self.assertEqual(WH.resolve_completion_delivery(args), "poll-fallback")
        self.assertEqual(args.completion_delivery_reason, "claude-session-resume-unsupported")

    # D: claude binary absent -> typed refusal, no launch
    def test_d_binary_absent_is_typed_refusal(self):
        os.environ["PATH"] = self.bindir.as_posix()  # no claude on PATH at all
        probe = WH.probe_claude_session_resume()
        self.assertEqual(probe.status, "indeterminate")
        self.assertEqual(probe.reason, "binary-absent")
        args = _owner_args()
        with self.assertRaises(WH.DispatchContractError) as ctx:
            WH.resolve_completion_delivery(args)
        self.assertEqual(ctx.exception.reason, "claude-session-resume-indeterminate")

    # E: probe timeout -> typed refusal (indeterminate). `probe_claude_session_resume`
    # hardcodes a 10s subprocess timeout; raise it directly at the `subprocess.run`
    # boundary (still the real function under test, real code path) rather than
    # actually sleeping 10s+ in the test suite.
    def test_e_probe_timeout_is_indeterminate(self):
        _write_fake_claude(self.bindir, _complete_help_source(True, True))
        with unittest.mock.patch.object(
            WH.subprocess, "run", side_effect=WH.subprocess.TimeoutExpired(cmd="claude", timeout=10)
        ):
            probe = WH.probe_claude_session_resume()
        self.assertEqual(probe.status, "indeterminate")
        self.assertEqual(probe.reason, "timeout")
        args = _owner_args()
        args.completion_probe = None
        with unittest.mock.patch.object(
            WH.subprocess, "run", side_effect=WH.subprocess.TimeoutExpired(cmd="claude", timeout=10)
        ):
            with self.assertRaises(WH.DispatchContractError) as ctx:
                WH.resolve_completion_delivery(args)
        self.assertEqual(ctx.exception.reason, "claude-session-resume-indeterminate")

    # F: nonzero exit -> indeterminate
    def test_f_nonzero_exit_is_indeterminate(self):
        target = self.bindir / "claude"
        target.write_text(_NONZERO_TEMPLATE)
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        probe = WH.probe_claude_session_resume()
        self.assertEqual(probe.status, "indeterminate")
        self.assertEqual(probe.reason, "nonzero-exit")
        args = _owner_args()
        with self.assertRaises(WH.DispatchContractError) as ctx:
            WH.resolve_completion_delivery(args)
        self.assertEqual(ctx.exception.reason, "claude-session-resume-indeterminate")

    # G: explicit operator poll + truncated help -> poll-fallback, probe never run
    def test_g_explicit_poll_bypasses_probe_even_when_truncated(self):
        _write_fake_claude(self.bindir, _truncated_help_source())
        args = _owner_args(completion_delivery="poll")
        with unittest.mock.patch.object(WH, "probe_claude_session_resume") as probe_fn:
            self.assertEqual(WH.resolve_completion_delivery(args), "poll-fallback")
        probe_fn.assert_not_called()

    # H: non-owner -> one-shot, probe never run
    def test_h_non_owner_is_one_shot_without_probing(self):
        _write_fake_claude(self.bindir, _truncated_help_source())
        for overrides in (
            dict(dispatch_depth=2),
            dict(worker_type="stage"),
            dict(dispatch_depth=2, intensity="quick"),
            dict(worker_type="stage", intensity="quick"),
        ):
            with self.subTest(**overrides):
                args = _owner_args(**overrides)
                with unittest.mock.patch.object(WH, "probe_claude_session_resume") as probe_fn:
                    self.assertEqual(WH.resolve_completion_delivery(args), "one-shot")
                probe_fn.assert_not_called()
        with self.assertRaises(WH.DispatchContractError) as caught:
            WH.resolve_completion_delivery(_owner_args(worker_type="stage", completion_delivery="supervised"))
        self.assertEqual(caught.exception.reason, "completion-delivery-ineligible")

    # H2: a quick owner is supervised only on a proven resume surface and otherwise
    # keeps the one-shot launch it always had
    def test_h2_quick_owner_is_supervised_only_when_resume_is_proven(self):
        _write_fake_claude(self.bindir, _complete_help_source(True, True))
        args = _owner_args(intensity="quick")
        self.assertEqual(WH.resolve_completion_delivery(args), "session-resume-supervised")
        self.assertEqual(args.completion_delivery_reason, "ok")
        for label, source in (("indeterminate", _truncated_help_source()),
                              ("unsupported", _complete_help_source(False, False))):
            with self.subTest(probe=label):
                _write_fake_claude(self.bindir, source)
                args = _owner_args(intensity="quick")
                args.completion_delivery_reason = "not-applicable"
                self.assertEqual(WH.resolve_completion_delivery(args), "one-shot")
                self.assertEqual(args.completion_delivery_reason, "not-applicable")
                args = _owner_args(intensity="quick", completion_delivery="supervised")
                with self.assertRaises(WH.DispatchContractError) as caught:
                    WH.resolve_completion_delivery(args)
                self.assertEqual(caught.exception.reason, "claude-session-resume-unavailable"
                                 if label == "unsupported" else "claude-session-resume-indeterminate")
        args = _owner_args(intensity="quick", completion_delivery="poll")
        with unittest.mock.patch.object(WH, "probe_claude_session_resume") as probe_fn:
            self.assertEqual(WH.resolve_completion_delivery(args), "one-shot")
        probe_fn.assert_not_called()

    # H3: standard+ keeps its refusal and poll-fallback rules
    def test_h3_standard_owner_rules_are_unchanged(self):
        _write_fake_claude(self.bindir, _complete_help_source(False, False))
        self.assertEqual(WH.resolve_completion_delivery(_owner_args(intensity="standard")), "poll-fallback")
        _write_fake_claude(self.bindir, _truncated_help_source())
        with self.assertRaises(WH.DispatchContractError) as caught:
            WH.resolve_completion_delivery(_owner_args(intensity="standard"))
        self.assertEqual(caught.exception.reason, "claude-session-resume-indeterminate")
        self.assertEqual(WH.resolve_completion_delivery(_owner_args(intensity="standard", completion_delivery="poll")),
                         "poll-fallback")

    # I: probe computed once, dry-run and start (same args instance) agree
    def test_i_probe_computed_once_and_cached_on_args(self):
        _write_fake_claude(self.bindir, _complete_help_source(True, True))
        args = _owner_args()
        with unittest.mock.patch.object(
            WH, "probe_claude_session_resume", wraps=WH.probe_claude_session_resume
        ) as probe_fn:
            first = WH.resolve_completion_delivery(args)
            second = WH.resolve_completion_delivery(args)
        self.assertEqual(first, second)
        self.assertEqual(probe_fn.call_count, 1)


class QuickSupervisedCommand(unittest.TestCase):
    """The supervisor command a quick owner gets differs from a standard+ owner's only in its route source."""

    def args(self, **overrides):
        base = dict(
            worker_type="owner", intensity="quick", dispatch_depth=1, artifact_root="/tmp/fixture-artifacts",
            worktree="/tmp/fixture-worktree", agent_home=Path("/tmp/fixture-agent-home"),
            jobs_path=Path("/tmp/jobs.log"), attempt_id="att-quick", capability_mode="dev",
            resolved_completion_delivery="session-resume-supervised",
            resolved_model_settings={"source": "profile", "role": "r", "model": "m", "effort": "low"},
            route_file=None, route_id=None, route_hash=None, route_node=None,
            owner_route_binding=None, permission_mode=None,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def command(self, **overrides):
        return WH.shell_command(self.args(**overrides), Path("/tmp/p.txt"), Path("/tmp/l.log"))

    def test_quick_owner_runs_under_the_session_supervisor_with_its_own_model(self):
        command = self.command()
        self.assertIn("claude-session-supervisor.py", command)
        self.assertIn("--parent-attempt-id att-quick", command)
        self.assertIn("--model m --effort low", command)
        self.assertIn("--permission-mode", command)
        self.assertNotIn("--route-file", command)

    def test_route_arguments_travel_only_as_a_complete_one_shot_tuple(self):
        full = dict(route_file="/tmp/route.json", route_id="rt-1", route_hash="sha256:a", route_node="one-shot")
        command = self.command(**full)
        self.assertIn("--route-file /tmp/route.json --route-id rt-1 --route-hash sha256:a", command)
        for missing in ("route_file", "route_id", "route_hash"):
            with self.subTest(missing=missing):
                self.assertNotIn("--route-", self.command(**{**full, missing: None}))
        self.assertNotIn("--route-", self.command(**{**full, "route_node": "execute"}))

    def test_standard_owner_binding_still_supplies_the_route(self):
        binding = argparse.Namespace(route_file="/tmp/bound.json", route_id="rt-b", route_hash="sha256:b")
        command = self.command(intensity="standard", owner_route_binding=binding,
                               route_file="/tmp/other.json", route_id="rt-o", route_hash="sha256:o", route_node="one-shot")
        self.assertIn("--route-file /tmp/bound.json --route-id rt-b --route-hash sha256:b", command)

    def test_registration_opens_input_only_for_a_supervised_delivery(self):
        with unittest.mock.patch("dispatch_owner_input.initialize_owner_input") as init:
            WH.initialize_supervised_owner_input(self.args(), Path("/tmp/jobs.log"))
            init.assert_called_once_with(Path("/tmp/jobs.log"), "att-quick", "claude-next-turn")
        for delivery in ("one-shot", "poll-fallback"):
            with unittest.mock.patch("dispatch_owner_input.initialize_owner_input") as init:
                WH.initialize_supervised_owner_input(self.args(resolved_completion_delivery=delivery), Path("/tmp/jobs.log"))
                init.assert_not_called()
        with unittest.mock.patch("dispatch_owner_input.initialize_owner_input", side_effect=OSError("disk")):
            WH.initialize_supervised_owner_input(self.args(), Path("/tmp/jobs.log"))


class SD63CodexParity(unittest.TestCase):
    """Codex's probe is already a feature probe (exit-code only); only the
    reason-sealing behavior is new. No PATH/binary fixture needed here -- this
    exercises the resolver contract via mocking `codex_app_server_available`,
    since that function's own judgment mechanism is unchanged by this cycle."""

    def setUp(self):
        codex_spec = importlib.util.spec_from_file_location(
            "codex_dispatch_headless_sd63", ROOT / "adapters/codex/bin/dispatch-headless.py"
        )
        self.CODEX = importlib.util.module_from_spec(codex_spec)
        codex_spec.loader.exec_module(self.CODEX)

    def test_auto_unavailable_seals_reason(self):
        CODEX = self.CODEX
        args = argparse.Namespace(
            completion_delivery="auto", dispatch_depth=1, worker_type="owner", intensity="strong",
        )
        args.completion_delivery_reason = "not-applicable"
        with unittest.mock.patch.object(CODEX, "codex_app_server_available", return_value=False):
            self.assertEqual(CODEX.resolve_completion_delivery(args), "poll-fallback")
        self.assertEqual(args.completion_delivery_reason, "codex-app-server-unavailable")

    def test_auto_available_seals_ok(self):
        CODEX = self.CODEX
        args = argparse.Namespace(
            completion_delivery="auto", dispatch_depth=1, worker_type="owner", intensity="strong",
        )
        args.completion_delivery_reason = "not-applicable"
        with unittest.mock.patch.object(CODEX, "codex_app_server_available", return_value=True):
            self.assertEqual(CODEX.resolve_completion_delivery(args), "app-server-supervised")
        self.assertEqual(args.completion_delivery_reason, "ok")


class SD63OpenCodeParityLock(unittest.TestCase):
    """Only a bound or quick depth-1 owner may stamp supervised delivery."""

    def test_opencode_supervised_delivery_requires_a_supervised_owner(self):
        # The row writes the supervised delivery and its lease from
        # `resolved_completion_delivery` (dispatch_wrapper_common.append_job);
        # OpenCode sets that value from `_supervised_owner(args)` before the row.
        opencode_path = ROOT / "adapters/opencode/bin/dispatch-headless.py"
        tree = ast.parse(opencode_path.read_text())
        assigns = [ast.unparse(node.value) for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   and any(ast.unparse(target) == "args.resolved_completion_delivery" for target in node.targets)]
        self.assertIn("'session-resume-supervised' if _supervised_owner(args) else 'one-shot'", assigns)
        common = ast.parse((ROOT / "utilities/dispatch_wrapper_common.py").read_text())
        supervised = next(node for node in common.body if isinstance(node, ast.Assign)
                          and ast.unparse(node.targets[0]) == "SUPERVISED_DELIVERIES")
        self.assertIn("session-resume-supervised", ast.unparse(supervised.value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
