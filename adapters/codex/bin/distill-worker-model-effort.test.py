#!/usr/bin/env python3
"""Run the complete distill worker with real memory/governor and a local CLI.

Only the model boundary is synthetic in normal cases. Resolver-fault cases
replace model-config.sh in a private source projection; they still execute the
complete unchanged worker, not an extracted validation fragment. Child HOME,
configuration, memory, telemetry, registry, and governor never use live state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
WORKER = ROOT / "adapters/codex/bin/distill-worker.sh"
sys.path.insert(0, str(ROOT / "utilities"))
import model_config  # noqa: E402


class DistillWorkerModelEffortTest(unittest.TestCase):
    def setUp(self):
        # The isolated runner's TMPDIR is already proven outside every Git
        # worktree (tools/run-tests.py choose_suite_temp_parent); a redundant
        # hardcoded parent here only breaks under isolation profiles where
        # /var/tmp is read-only, without adding protection.
        self.temp = tempfile.TemporaryDirectory(prefix="distill-effort-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.base.chmod(0o700)
        self.home = self.base / "home"
        self.codex_home = self.home / ".codex"
        self.project = self.base / "project"
        self.sessions = self.codex_home / "sessions"
        self.bin = self.base / "bin"
        self.store = self.base / "memory"
        self.source = self.base / "source"
        for path in (self.home, self.codex_home, self.project, self.sessions,
                     self.bin, self.source / "adapters/codex/bin", self.source / "utilities"):
            path.mkdir(parents=True, exist_ok=True)
        self.worker = self.source / "adapters/codex/bin/distill-worker.sh"
        shutil.copyfile(WORKER, self.worker)
        for relative in ("core", "tools", "adapters/codex/config"):
            (self.source / relative).symlink_to(ROOT / relative, target_is_directory=True)
        for path in (ROOT / "utilities").iterdir():
            if path.name != "__pycache__":
                (self.source / "utilities" / path.name).symlink_to(path, target_is_directory=path.is_dir())
        self.calls = self.base / "model-calls.jsonl"
        self.resolver_receipt = self.base / "resolver-called"
        self.sid_counter = 0
        self.env = {
            "HOME": str(self.home), "CODEX_HOME": str(self.codex_home),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "SHELL": "/bin/sh", "PATH": str(self.bin) + os.pathsep + os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(self.base),
            "AGENT_HOME": str(self.source), "AGENT_RUNTIME_ROOT": str(self.source),
            "XDG_CONFIG_HOME": str(self.base / "config"),
            "XDG_DATA_HOME": str(self.base / "data"),
            "XDG_STATE_HOME": str(self.base / "state"),
            "XDG_CACHE_HOME": str(self.base / "cache"),
            "XDG_RUNTIME_DIR": str(self.base / "runtime"),
            "HARNESS_STATE_ROOT": str(self.base / "harness-state"),
            "MEM_STORE": str(self.store), "MEM_STATE_DIR": str(self.base / "mem-state"),
            "MEM_TELEMETRY_ROOT": str(self.base / "telemetry"),
            "MEM_PROJECTS": str(self.base / "projects"),
            "MEM_PROFILE": str(self.base / "profile"),
            "MEM_WRITE_EVENTS": str(self.base / "telemetry/write.jsonl"),
            "MEM_RECALL_EVENTS": str(self.base / "telemetry/recall.jsonl"),
            "MEM_RECALL_RECEIPTS": str(self.base / "telemetry/receipts"),
            "MEM_SYNC_REMOTE": "0", "MEM_DUMP_PUSH": "0", "MEM_DUMP_COMMIT": "0",
            "MEM_SYNC_DIR": str(self.base / "exchange"),
            "AGENT_DISPATCH_JOBS": str(self.base / "dispatch/jobs.log"),
            "AGENT_MODEL_GOVERNOR_ROOT": str(self.base / "governor"),
            "AGENT_ARTIFACT_ROOT": str(self.project / ".agent_reports"),
            "AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_CHILD": "1",
            # MEM_DISTILL is added by the worker at the model boundary. Setting
            # it on this controller would deliberately short-circuit all work.
            "CODEX_SESSIONS": str(self.sessions), "CODEX_DISTILL_ENABLE": "1",
            "CODEX_DISTILL_APPLY": "1", "CODEX_DISTILL_CONTRACT_ACCEPTED": "1",
            "CODEX_DISTILL_TIMEOUT": "8", "CODEX_DISTILL_TIMEOUT_CURATE": "8",
            "DISTILL_FIXTURE_CALLS": str(self.calls),
        }
        Path(self.env["AGENT_DISPATCH_JOBS"]).parent.mkdir()
        Path(self.env["AGENT_DISPATCH_JOBS"]).write_text("")
        codex = self.bin / "codex"
        codex.write_text("#!" + sys.executable + "\n" + '''import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
keys = ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_STATE_HOME", "MEM_STORE",
        "MEM_STATE_DIR", "MEM_TELEMETRY_ROOT", "MEM_SYNC_REMOTE", "MEM_DUMP_PUSH",
        "MEM_DUMP_COMMIT", "MEM_SYNC_DIR", "AGENT_DISPATCH_JOBS", "AGENT_MODEL_GOVERNOR_ROOT",
        "AGENT_SESSION_ROLE", "MEM_DISTILL", "CODEX_DISTILL_EFFORT", "CODEX_REASONING_LUNA")
with Path(os.environ["DISTILL_FIXTURE_CALLS"]).open("a") as out:
    out.write(json.dumps({"argv": argv, "env": {k: os.environ.get(k) for k in keys}}) + "\\n")
if "--output-last-message" in argv and os.environ.get("DISTILL_FIXTURE_NO_OUTPUT") != "1":
    Path(argv[argv.index("--output-last-message") + 1]).write_text(
        os.environ.get("DISTILL_FIXTURE_OUTPUT", ""))
status = int(os.environ.get("DISTILL_FIXTURE_EXIT", "0"))
if status:
    print("fixture CLI rejected this model/effort pair", file=sys.stderr)
sys.exit(status)
''')
        codex.chmod(0o755)
        check = subprocess.run(["git", "-C", str(self.source), "rev-parse", "--show-toplevel"],
                               env=self.env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(check.returncode, 0, "fixture source must not inherit a Git root")
        self.shipped = model_config.parse_config(ROOT / "adapters/codex/config/models.conf")
        self.values = {**self.shipped,
            "CFG_TIER_DEEP_MODEL": "fixture-deep", "CFG_TIER_DEEP_EFFORT": "ultra",
            "CFG_TIER_LIGHT_MODEL": "fixture-light", "CFG_TIER_LIGHT_EFFORT": "medium",
            "CFG_TIER_MINI_MODEL": "fixture-mini", "CFG_TIER_MINI_EFFORT": "low"}
        self.user_config = self.codex_home / "agent-config/models.conf"
        self.user_config.parent.mkdir()
        self.write_config()
        init = self.memory("index")
        self.assertEqual(init.returncode, 0, init.stderr)

    def write_config(self, changes=None):
        values = {**self.values, **(changes or {})}
        self.user_config.write_text(model_config.assignments(values))
        return values

    def memory(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools/memory/mem.py"), *args],
                              env=self.env, cwd=self.project, capture_output=True,
                              text=True, timeout=10)

    def controlled_resolver(self, values, *, exit_code=0, output=None):
        path = self.source / "utilities/model-config.sh"
        path.unlink()  # exact fixture-owned link; never changes its source
        payload = model_config.assignments(values) if output is None else output
        path.write_text("#!" + sys.executable + "\n" +
            "import sys\nfrom pathlib import Path\n" +
            f"Path({str(self.resolver_receipt)!r}).write_text('called')\n" +
            f"sys.stdout.write({payload!r})\n" +
            ("sys.stderr.write('fixture resolver failed\\n')\n" if exit_code else "") +
            f"sys.exit({exit_code})\n")
        path.chmod(0o755)

    def run_worker(self, mode="increment", extra_env=None):
        # Each invocation gets a fresh transcript identity; do not delete real
        # distill markers to make repeated calls look like fresh sessions.
        self.sid_counter += 1
        sid = f"distill-effort-{self.sid_counter}"
        self.last_sid = sid
        (self.sessions / f"{sid}.jsonl").write_text(json.dumps({
            "type": "event_msg", "timestamp": "2026-09-08T00:00:00Z",
            "payload": {"type": "user_message", "id": f"synthetic-u{self.sid_counter}",
                        "message": "Synthetic lifecycle effort fixture."}}) + "\n")
        result = subprocess.run(["sh", str(self.worker), sid, str(self.project), mode],
            env={**self.env, **(extra_env or {})}, cwd=self.project,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        return result

    def receipts(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def assert_call(self, result, model, effort):
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        receipt = self.receipts()[-1]
        argv = receipt["argv"]
        self.assertEqual(argv[0], "exec")
        self.assertEqual(argv.count("-m"), 1)
        self.assertEqual(argv[argv.index("-m") + 1], model)
        self.assertEqual(argv.count("-c"), 1)
        self.assertEqual(argv[argv.index("-c") + 1], f'model_reasoning_effort="{effort}"')
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--ephemeral", argv)
        for key in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_STATE_HOME", "MEM_STORE",
                    "MEM_STATE_DIR", "MEM_TELEMETRY_ROOT", "MEM_SYNC_DIR", "AGENT_DISPATCH_JOBS",
                    "AGENT_MODEL_GOVERNOR_ROOT"):
            self.assertEqual(receipt["env"][key], self.env[key])
        for key in ("MEM_SYNC_REMOTE", "MEM_DUMP_PUSH", "MEM_DUMP_COMMIT"):
            self.assertEqual(receipt["env"][key], "0")
        self.assertEqual(receipt["env"]["AGENT_SESSION_ROLE"], "worker")
        self.assertEqual(receipt["env"]["MEM_DISTILL"], "1")

    def assert_no_model(self, result, reason):
        self.assertEqual(result.returncode, 70, (result.stdout, result.stderr))
        self.assertIn(reason, result.stderr)
        self.assertFalse(self.receipts())
        self.assertFalse((self.store / f".distill-state-{self.last_sid}").exists())
        self.assertFalse((self.store / f".codex-distill-lock-{self.last_sid}").exists())

    def test_increment_uses_mini_and_curate_uses_light(self):
        self.assert_call(self.run_worker(), "fixture-mini", "low")
        self.assert_call(self.run_worker("curate"), "fixture-light", "medium")
        self.assertEqual(len(self.receipts()), 2)

    def test_lifecycle_tier_remapping_selects_model_and_effort_together(self):
        self.write_config({"CFG_LIFECYCLE_NUDGE": "deep", "CFG_LIFECYCLE_CURATE": "mini"})
        self.assert_call(self.run_worker(), "fixture-deep", "ultra")
        self.assert_call(self.run_worker("curate"), "fixture-mini", "low")

    def test_unknown_nonempty_lifecycle_tier_preserves_light_compatibility(self):
        self.write_config({"CFG_LIFECYCLE_NUDGE": "future-tier"})
        self.assert_call(self.run_worker(), "fixture-light", "medium")

    def test_every_checked_effort_reaches_actual_cli(self):
        for effort in ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            with self.subTest(effort=effort):
                self.write_config({"CFG_TIER_MINI_EFFORT": effort})
                self.assert_call(self.run_worker(), "fixture-mini", effort)
        self.assertEqual(len(self.receipts()), 7)

    def test_curate_forwards_ultra_and_max(self):
        for effort in ("ultra", "max"):
            self.write_config({"CFG_TIER_LIGHT_EFFORT": effort})
            self.assert_call(self.run_worker("curate"), "fixture-light", effort)

    def test_global_override_wins_both_modes_without_changing_effort(self):
        overrides = {"CODEX_DISTILL_MODEL": "global-model", "CODEX_DISTILL_MODEL_INCREMENT": "inc-model",
                     "CODEX_DISTILL_MODEL_CURATE": "curate-model"}
        self.assert_call(self.run_worker(extra_env=overrides), "global-model", "low")
        self.assert_call(self.run_worker("curate", overrides), "global-model", "medium")

    def test_cli_rejection_preserves_exit_partial_output_and_pending_delta(self):
        # A failed CLI can still create output. This must neither turn the
        # worker's exit into 0 nor apply/emit a partial model result.
        for mode in ("increment", "curate"):
            result = self.run_worker(mode, {"DISTILL_FIXTURE_EXIT": "64", "DISTILL_FIXTURE_OUTPUT": "UNTRUSTED PARTIAL RESULT"})
            self.assertEqual(result.returncode, 64, (result.stdout, result.stderr))
            self.assertIn("fixture CLI rejected", result.stderr)
            self.assertNotIn("UNTRUSTED", result.stdout)
            self.assertFalse((self.store / f".distill-state-{self.last_sid}").exists())
            delta = self.memory("distill", self.last_sid, "--source", "codex")
            self.assertIn("Synthetic lifecycle effort fixture.", delta.stdout)
        self.assertEqual(len(self.receipts()), 2, "no retry or effort-dropping fallback")
        for receipt in self.receipts():
            self.assertIn("-c", receipt["argv"])

    def test_missing_output_preserves_failure_and_pending_delta(self):
        # A successful process alone cannot acknowledge a distilled window.
        # The supported no-action result is an existing empty output file.
        for mode in ("increment", "curate"):
            result = self.run_worker(mode, {"DISTILL_FIXTURE_NO_OUTPUT": "1"})
            self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
            self.assertIn("model-output-missing", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse((self.store / f".distill-state-{self.last_sid}").exists())
            delta = self.memory("distill", self.last_sid, "--source", "codex")
            self.assertEqual(delta.returncode, 0, delta.stderr)
            self.assertIn("Synthetic lifecycle effort fixture.", delta.stdout)
        self.assertEqual(len(self.receipts()), 2, "missing output must not trigger model retry")

    def test_per_mode_overrides_do_not_cross_modes(self):
        self.assert_call(self.run_worker(extra_env={"CODEX_DISTILL_MODEL_INCREMENT": "inc-model"}), "inc-model", "low")
        self.assert_call(self.run_worker("curate", {"CODEX_DISTILL_MODEL_INCREMENT": "inc-model"}), "fixture-light", "medium")
        self.assert_call(self.run_worker("curate", {"CODEX_DISTILL_MODEL_CURATE": "curate-model"}), "curate-model", "medium")
        self.assert_call(self.run_worker(extra_env={"CODEX_DISTILL_MODEL_CURATE": "curate-model"}), "fixture-mini", "low")

    def test_empty_overrides_use_configured_model(self):
        overrides = {"CODEX_DISTILL_MODEL": "", "CODEX_DISTILL_MODEL_INCREMENT": "", "CODEX_DISTILL_MODEL_CURATE": ""}
        self.assert_call(self.run_worker(extra_env=overrides), "fixture-mini", "low")
        self.assert_call(self.run_worker("curate", overrides), "fixture-light", "medium")

    def test_role_effort_knobs_are_preserved_but_do_not_override_distill(self):
        self.assert_call(self.run_worker(extra_env={"CODEX_REASONING_LUNA": "ultra", "CODEX_DISTILL_EFFORT": "max"}),
                         "fixture-mini", "low")
        self.assertEqual(self.receipts()[0]["env"]["CODEX_REASONING_LUNA"], "ultra")
        self.assertEqual(self.receipts()[0]["env"]["CODEX_DISTILL_EFFORT"], "max")

    def test_user_file_failure_keeps_whole_valid_shipped_fallback(self):
        cases = (None, "not a config\n", "CFG_TIER_MINI_EFFORT=ultra\n")
        for body in cases:
            if body is None:
                self.user_config.unlink()
            else:
                self.user_config.write_text(body)
            self.assert_call(self.run_worker(), self.shipped["CFG_TIER_MINI_MODEL"], self.shipped["CFG_TIER_MINI_EFFORT"])

    def test_actual_user_config_invalid_effort_never_calls_model(self):
        for effort in ("medium high", "midium", "MEDIUM"):
            self.write_config({"CFG_TIER_MINI_EFFORT": effort})
            self.assert_no_model(self.run_worker(), "reasoning-effort-invalid")

    def test_controlled_empty_whitespace_and_unsafe_efforts_fail_in_full_worker(self):
        sentinel = self.base / "must-not-exist"
        for effort in ("", "   ", "medium\nhigh", f"medium; touch {sentinel}", f"$(touch {sentinel})"):
            with self.subTest(effort=effort):
                self.controlled_resolver({**self.values, "CFG_TIER_MINI_EFFORT": effort})
                self.assert_no_model(self.run_worker(), "reasoning-effort-invalid")
                self.assertTrue(self.resolver_receipt.is_file())
                self.assertFalse(sentinel.exists())

    def test_resolver_failure_cannot_reuse_inherited_cfg_or_model_override(self):
        for output in ("", model_config.assignments(self.values)):
            self.controlled_resolver(self.values, exit_code=65, output=output)
            inherited = {**self.values, "CODEX_DISTILL_MODEL": "explicit-model"}
            self.assert_no_model(self.run_worker(extra_env=inherited), "model-config-resolution-failed")

    def test_missing_effective_effort_cannot_reuse_inherited_cfg(self):
        values = dict(self.values)
        del values["CFG_TIER_MINI_EFFORT"]
        self.controlled_resolver(values)
        self.assert_no_model(self.run_worker(extra_env={"CFG_TIER_MINI_EFFORT": "medium"}), "reasoning-effort-invalid")

    def test_missing_effective_tier_cannot_reuse_inherited_cfg(self):
        values = dict(self.values)
        del values["CFG_LIFECYCLE_NUDGE"]
        self.controlled_resolver(values)
        self.assert_no_model(self.run_worker(extra_env={"CFG_LIFECYCLE_NUDGE": "mini"}), "lifecycle-tier-unresolved")

    def test_missing_effective_model_cannot_reuse_inherited_cfg(self):
        values = dict(self.values)
        del values["CFG_TIER_MINI_MODEL"]
        self.controlled_resolver(values)
        self.assert_no_model(self.run_worker(extra_env={"CFG_TIER_MINI_MODEL": "inherited-model"}), "model-unresolved")

    def test_success_advances_marker_and_worker_reentry_is_silent(self):
        self.assert_call(self.run_worker(), "fixture-mini", "low")
        self.assertTrue((self.store / f".distill-state-{self.last_sid}").is_file())
        result = self.run_worker(extra_env={"MEM_DISTILL": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.receipts()), 1)


if __name__ == "__main__":
    unittest.main()
