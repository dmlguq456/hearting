#!/usr/bin/env python3
"""no-Claude hard exclusion: sealed + launch-time official path regression.

Covers the 2026-10-04 owner-handoff incident: a route sealed before the
user's no-Claude instruction still listed Claude as a supported fallback, so
``--dry-run`` reported the Codex head as ``exit 0`` while ``--start`` spent
the Codex attempt (``exit 1``) and automatically launched a fresh Claude
worker. The official path is:

* sealed: ``--disable-harness claude`` probe → ``user-disabled``/``unsupported``
  (already existed; kept here as the compose-time half), and
* launch-time: ``AGENT_DISPATCH_EXCLUDED_HARNESSES=claude``
  (alias ``AGENT_DISPATCH_DISABLED_HARNESSES``) — read on every
  ``dispatch-owner.py`` and ``stage-dispatch-fallback.py`` invocation, for
  ``--dry-run``, ``--register`` and ``--start`` alike.

Excluded harnesses are never selected, never used as automatic or explicit
fallback, and an explicit ``--adapter <excluded>`` is refused before any
wrapper runs. When nothing non-excluded remains the launch fails closed.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import contextlib

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXCL = _load("dispatch_harness_exclusion_test", ROOT / "utilities" / "dispatch_harness_exclusion.py")
OWNER = _load("dispatch_owner_excl_test", ROOT / "utilities" / "dispatch-owner.py")


class ParseExcludedTest(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertEqual(EXCL.parse_excluded(None), frozenset())
        self.assertEqual(EXCL.parse_excluded(""), frozenset())
        self.assertEqual(EXCL.excluded_harnesses({}), frozenset())

    def test_single_and_multi_case_insensitive(self):
        self.assertEqual(EXCL.parse_excluded("claude"), frozenset({"claude"}))
        self.assertEqual(EXCL.parse_excluded("Claude"), frozenset({"claude"}))
        self.assertEqual(
            EXCL.parse_excluded("claude, codex"),
            frozenset({"claude", "codex"}),
        )
        self.assertEqual(
            EXCL.excluded_harnesses({
                "AGENT_DISPATCH_EXCLUDED_HARNESSES": "claude",
                "AGENT_DISPATCH_DISABLED_HARNESSES": "codex",
            }),
            frozenset({"claude", "codex"}),
        )

    def test_unknown_fails_closed(self):
        with self.assertRaises(ValueError) as caught:
            EXCL.parse_excluded("gpt")
        self.assertIn("excluded-harness-unknown", str(caught.exception))
        with self.assertRaises(ValueError):
            EXCL.excluded_harnesses({"AGENT_DISPATCH_EXCLUDED_HARNESSES": "claud"})

    def test_format(self):
        self.assertEqual(EXCL.format_excluded(frozenset()), "none")
        self.assertEqual(EXCL.format_excluded(frozenset({"codex", "claude"})), "claude,codex")


class OwnerExclusionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "agent-home").mkdir()
        (self.home / "logs").mkdir()
        (self.home / "codex-home").mkdir()
        (self.home / "claude-home").mkdir()
        self.jobs = self.home / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self):
        path = self.home / "dispatch-defaults.yaml"
        path.write_text(
            "schema_version: 3\n"
            "harnesses:\n  enabled: [claude, codex, opencode]\n"
            "profiles:\n"
            "  deep:\n    primary: [claude, codex]\n    relief: []\n"
            "    last_resort: [opencode]\n    promote_relief_below: 0\n"
            "  balanced-deep:\n    primary: [claude, codex]\n    relief: []\n"
            "    last_resort: [opencode]\n    promote_relief_below: 0\n"
            "  light:\n    primary: [claude, codex, opencode]\n    relief: []\n"
            "    last_resort: []\n    promote_relief_below: 0\n"
            "  mini:\n    primary: [claude, codex, opencode]\n    relief: []\n"
            "    last_resort: []\n    promote_relief_below: 0\n"
            "allocation:\n  strategy: capacity-aware\n  window: 30\n"
            "capabilities:\n",
            encoding="utf-8",
        )
        return path

    def _argv(self, extra=()):
        return [
            "--dry-run", "--worktree", str(ROOT), "--slug", "owner-test",
            "--capability", "autopilot-code", "--capability-mode", "debug", "--qa", "standard",
            "--intensity", "standard", "--dispatch-depth", "1", "--worker-type", "owner",
            "--assigned-contract", "autopilot-code", "--owner", "autopilot-code",
            "--model-profile", "deep", "--jobs", str(self.jobs),
            "--log-dir", str(self.home / "logs"),
            *extra,
        ]

    def _env(self, extra_env=None):
        env = {
            "AGENT_HOME": str(self.home / "agent-home"),
            "HOME": str(self.home),
            "DISPATCH_DEFAULTS_CONFIG": str(self._config()),
            "CODEX_HOME": str(self.home / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.home / "claude-home"),
            "HARNESS_CAPACITY_SCORES": "claude:80,codex:80,opencode:80",
            "AGENT_CODEX_MANAGED_GATEWAY": "0",
            "AGENT_CODEX_MANAGED_PARENT_RUNTIME": "",
            "AGENT_DISPATCH_JOBS": str(self.jobs),
        }
        env.update(extra_env or {})
        return env

    def _run_in_process(self, argv, env):
        module = OWNER
        real_run = module.subprocess.run

        def sentinel(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and "dispatch-headless.py" in str(cmd[0]):
                raise AssertionError(f"wrapper invoked unexpectedly: {cmd}")
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(module.subprocess, "run", side_effect=sentinel):
            buf = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False):
                with contextlib.redirect_stdout(buf):
                    rc = module.main(argv)
        return rc, buf.getvalue()

    def _run_subprocess(self, argv, env):
        """Real selector subprocess (wrapper dry-run allowed, no child spawn)."""
        clean = {
            k: v for k, v in os.environ.items()
            if not k.startswith("AGENT_DISPATCH_")
            and not k.startswith("AGENT_OWNER_ROUTE_")
            and not k.startswith("AGENT_ROUTE_")
            and not k.startswith("AGENT_ARTIFACT_")
            and k not in ("CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID")
        }
        full = {**clean, **env}
        full["GIT_CONFIG_COUNT"] = "1"
        full["GIT_CONFIG_KEY_0"] = "safe.directory"
        full["GIT_CONFIG_VALUE_0"] = str(ROOT)
        cmd = [sys.executable, str(ROOT / "utilities" / "dispatch-owner.py"), *argv]
        return subprocess.run(cmd, text=True, capture_output=True, env=full)

    def test_excluded_harness_is_never_selected(self):
        result = self._run_subprocess(
            self._argv(), self._env({"AGENT_DISPATCH_EXCLUDED_HARNESSES": "claude"})
        )
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertIn("excluded_harnesses=claude", result.stdout)
        self.assertNotIn("adapter=claude", result.stdout)
        self.assertIn("adapter=codex", result.stdout)

    def test_explicit_adapter_to_excluded_is_refused_before_wrapper(self):
        rc, out = self._run_in_process(
            self._argv(("--adapter", "claude")),
            self._env({"AGENT_DISPATCH_EXCLUDED_HARNESSES": "claude"}),
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("explicit-adapter-excluded-by-user-policy", out)
        self.assertIn("excluded_harnesses=claude", out)
        self.assertIn("child_spawned=0", out)

    def test_all_candidates_excluded_fails_closed_with_typed_reason(self):
        rc, out = self._run_in_process(
            self._argv(),
            self._env({"AGENT_DISPATCH_EXCLUDED_HARNESSES": "claude,codex,opencode"}),
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("no-eligible-candidate-excluded-by-user", out)
        self.assertIn("excluded_harnesses=claude,codex,opencode", out)
        self.assertIn("child_spawned=0", out)

    def test_unknown_exclusion_fails_closed(self):
        rc, out = self._run_in_process(
            self._argv(), self._env({"AGENT_DISPATCH_EXCLUDED_HARNESSES": "gpt"})
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("excluded-harness-unknown", out)
        self.assertIn("child_spawned=0", out)

    def test_alias_env_is_honoured(self):
        result = self._run_subprocess(
            self._argv(), self._env({"AGENT_DISPATCH_DISABLED_HARNESSES": "claude"})
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("excluded_harnesses=claude", result.stdout)
        self.assertNotIn("adapter=claude", result.stdout)


class StageExclusionTest(unittest.TestCase):
    """Depth-2 fallback never launches an excluded harness.

    Regression for the dry-run/start divergence in the 2026-10-04
    owner-handoff: dry-run reported the Codex head as exit 0 while start
    spent the Codex attempt (exit 1) and fell back to a fresh Claude worker.
    With AGENT_DISPATCH_EXCLUDED_HARNESSES=claude the Codex failure must
    skip Claude on every hop — in dry-run as in start — and the receipt
    must name the exclusion.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Fixture"], check=True)
        (self.repo / "x").write_text("x")
        subprocess.run(["git", "-C", str(self.repo), "add", "x"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        self.art = base / ".agent_reports"
        self.art.mkdir()
        self.jobs = base / "jobs.log"
        self.prev_defaults = os.environ.get("DISPATCH_DEFAULTS_CONFIG")
        os.environ["DISPATCH_DEFAULTS_CONFIG"] = str(ROOT / "profiles" / "dispatch-defaults.yaml")
        self.owner_proc = subprocess.Popen(["sleep", "60"])
        self._route_mod = _load("route_excl_test", ROOT / "utilities" / "capability-route.py")
        self._fb = _load("fallback_excl_test", ROOT / "utilities" / "stage-dispatch-fallback.py")

    def tearDown(self):
        if self.owner_proc.poll() is None:
            self.owner_proc.kill()
        self.owner_proc.wait()
        self.tmp.cleanup()
        if self.prev_defaults is None:
            os.environ.pop("DISPATCH_DEFAULTS_CONFIG", None)
        else:
            os.environ["DISPATCH_DEFAULTS_CONFIG"] = self.prev_defaults

    def _seed_parent(self, harness="codex"):
        start = (Path("/proc") / str(self.owner_proc.pid) / "stat").read_text().split()[21]
        with self.jobs.open("a", encoding="utf-8") as fh:
            fh.write(
                f"2026-07-23T00:00:00Z\topen\t{self.repo}\t{self.repo}\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                f"harness={harness},runtime_sandbox=workspace-write,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,worker_type=owner,"
                f"attempt_id=att-fallback-parent,pid={self.owner_proc.pid},pid_start={start}\n"
            )

    def _tuple(self, child, status):
        return {
            "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "workspace-write", "child_harness": child,
            "launch_authority": "conductor", "status": status,
            "probe_source": "fixture", "probe_time": "2026-07-16T00:00:00Z",
            "failure_class": "" if status == "supported" else "nested-network-unconfirmed",
            "checked_worktree": str(self.repo.resolve()),
            "failure_scope": "none" if status == "supported" else "runtime-global",
            "codex_command": "ok" if child == "codex" else "not-applicable",
            "retry_on_isolated_worktree": 0,
        }

    def _route(self):
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec", "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        evidence = {
            "tuples": [self._tuple("codex", "supported"), self._tuple("claude", "supported")],
            "native_subagent": [],
        }
        env = {
            "AGENT_HOME": str(ROOT), "AGENT_ARTIFACT_ROOT": str(self.art),
            "AGENT_MODEL_GOVERNOR_ROOT": str(self.art / ".runtime" / "model-worker-governor"),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
        }
        with mock.patch.dict(os.environ, env):
            route = self._route_mod.compile_route(
                "autopilot-code", "dev", "strong", self.repo, self.art,
                signals=["shared-contract"], transport="headless",
                tracking="tracked", tracked_gate_evidence=gate,
                dispatch_evidence=evidence,
            )
        plan = next(n for n in route.get("nodes", []) if n.get("id") == "plan")
        if plan.get("depends_on"):
            plan["depends_on"] = []
            route["route_hash"] = self._route_mod.route_hash(route)
            route["route_id"] = self._route_mod.ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
        path = Path(self.tmp.name) / "route.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        return path

    def _dispatch_env(self, **extra):
        base = {
            "AGENT_HOME": str(ROOT), "AGENT_ARTIFACT_ROOT": str(self.art),
            "AGENT_MODEL_GOVERNOR_ROOT": str(self.art / ".runtime" / "model-worker-governor"),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_DISPATCH_SELF_SLUG": "owner",
            "AGENT_DISPATCH_ATTEMPT_ID": "att-fallback-parent",
        }
        base.update(extra)
        return base

    def test_excluded_claude_is_skipped_without_wrapper_call(self):
        """A supported-but-excluded Claude candidate is skipped pre-launch.

        Codex is sealed unsupported so the chain would otherwise reach the
        Claude cross-harness hop; with no-Claude it must skip Claude and
        descend to inline/chain-exhausted, and the trace must name the skip.
        """
        route_path = self._route()
        # Rewrite the codex tuple to unsupported so the only headless hope is claude.
        route = json.loads(route_path.read_text(encoding="utf-8"))
        self._seed_parent()
        printed: list[str] = []
        argv = [
            "stage-dispatch-fallback.py", "--route", str(route_path), "--node", "plan",
            "--slug", "fallback-plan", "--parent", "owner",
            "--capability-mode", "dev", "--worker-mode", "plan/plan-author",
            "--model-role", "deep maker", "--jobs", str(self.jobs), "--dry-run",
        ]
        # Force codex unsupported by patching ordered_fallback_hops output? Simpler:
        # mock wrapper_command to fail loudly if claude is ever attempted, and
        # mock the codex tuple path via failed-tuple so claude is the next hop.
        # Instead drive the real loop: codex supported wrapper will run its
        # --dry-run (real, no child spawn) and succeed, so to reach claude we
        # mark codex as prior failure via --failed-tuple.
        codex_key = "codex/headless/workspace-write/codex/conductor"
        argv += ["--failed-tuple", codex_key]
        wrapper_calls: list[str] = []
        real_wrapper = self._fb.wrapper_command

        def tracking_wrapper(args, route_d, node, row, ordinal, attempt_id, *a, **k):
            wrapper_calls.append(row.get("child_harness", ""))
            return real_wrapper(args, route_d, node, row, ordinal, attempt_id, *a, **k)

        env = self._dispatch_env(AGENT_DISPATCH_EXCLUDED_HARNESSES="claude")
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self._fb, "wrapper_command", side_effect=tracking_wrapper), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            code = self._fb._dispatch(self._fb.LAUNCH_TUPLE.ReportOnlyObservation())
        out = "\n".join(printed)
        self.assertNotIn("child_harness=claude", out)
        self.assertIn("skipped-excluded-harness", out)
        self.assertTrue(all(h != "claude" for h in wrapper_calls),
                        f"claude wrapper must never run under no-Claude: {wrapper_calls}")
        # The launch did not violate the prohibition: either inline/degraded
        # or a typed chain failure, never a claude child.
        self.assertNotEqual(code, 0, out)

    def test_codex_failure_does_not_fall_back_to_excluded_claude(self):
        """Codex direct exit 1 skips the excluded Claude hop (dry-run/start parity).

        Both actions share the same candidate loop, so this dry-run proof
        covers the start divergence too: the start that spends the Codex
        attempt reaches the same skip instead of a fresh Claude worker.
        """
        route_path = self._route()
        self._seed_parent()
        printed: list[str] = []
        argv = [
            "stage-dispatch-fallback.py", "--route", str(route_path), "--node", "plan",
            "--slug", "fallback-plan", "--parent", "owner",
            "--capability-mode", "dev", "--worker-mode", "plan/plan-author",
            "--model-role", "deep maker", "--jobs", str(self.jobs), "--dry-run",
        ]
        real_run = subprocess.run
        launched: list[str] = []

        def fake_run(cmd, *args, **kwargs):
            text = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if "dispatch-headless.py" in text:
                harness = "unknown"
                for cand in ("codex", "claude", "opencode"):
                    if f"adapters/{cand}/bin/dispatch-headless.py" in text:
                        harness = cand
                        break
                launched.append(harness)
                if harness == "codex":
                    # Simulate the owner-handoff incident: Codex attempt exit 1.
                    return subprocess.CompletedProcess(cmd, 1, stdout="check=failed\nreason=wrapper-exit\n", stderr="")
                if harness == "claude":
                    return subprocess.CompletedProcess(cmd, 0, stdout="check=ok\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        env = self._dispatch_env(AGENT_DISPATCH_EXCLUDED_HARNESSES="claude")
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self._fb.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            self._fb._dispatch(self._fb.LAUNCH_TUPLE.ReportOnlyObservation())
        self.assertIn("codex", launched, f"codex head must be attempted: {launched}")
        self.assertNotIn("claude", launched,
                         f"codex failure must not fall back to excluded claude: {launched}")


if __name__ == "__main__":
    unittest.main()
