#!/usr/bin/env python3
"""End-to-end wiring checks for the shared spec-sync nudge."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "spec-sync-nudge.sh"
BRIDGE = ROOT / "adapters" / "codex" / "hooks" / "posttooluse-spec-sync.py"
NODE = shutil.which("node")
BASELINE = "6040880f02cc904ef18b81e9c493dbe5ddf33ff5"
PATCH = "*** Begin Patch Patch\n*** Update File: source.py\n@@\n-epoch = 30\n+epoch = 50\n*** End Patch\n"
EXPECTED = "spec/prd.md:2  «epoch = 30»"


def run(command, *, cwd=None, env=None, input=None, timeout=20):
    return subprocess.run(command, cwd=cwd, env=env, input=input, text=True,
                          capture_output=True, timeout=timeout)


def clean_env(home: Path, **updates):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("AGENT_ARTIFACT_", "AGENT_ROUTE_", "AGENT_OWNER_ROUTE_")):
            env.pop(key, None)
    for key in ("AGENT_ROUTE_FILE", "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE", "AGENT_DISPATCH_CHILD",
                "AGENT_DISPATCH_DEPTH", "AGENT_SESSION_ROLE", "OPENCODE_DISPATCH_SLUG"):
        env.pop(key, None)
    env.update({"AGENT_HOME": str(ROOT), "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".data"), "XDG_STATE_HOME": str(home / ".state"),
                "PYTHONDONTWRITEBYTECODE": "1"})
    env.update({key: str(value) for key, value in updates.items()})
    return env


def make_linked_fixture(base: Path):
    primary = base / "primary"
    linked = base / "linked-checkout"
    (primary / ".agent_reports" / "spec").mkdir(parents=True)
    (primary / "source.py").write_text("epoch = 30\n", encoding="utf-8")
    (primary / ".agent_reports/spec/pipeline_state.yaml").write_text("mode: [code]\n", encoding="utf-8")
    (primary / ".agent_reports/spec/prd.md").write_text(
        "# Canonical\n- CANONICAL_ONLY epoch = 30 and a_postedit_spec_sync\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(primary)], check=True)
    subprocess.run(["git", "-C", str(primary), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(primary), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(primary), "add", "."], check=True)
    subprocess.run(["git", "-C", str(primary), "commit", "-qm", "fixture"], check=True)
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "--detach", str(linked), "HEAD"], check=True)
    (primary / ".agent_reports/spec/prd.md").write_text(
        "# Canonical\n- CANONICAL_ONLY epoch = 30 and a_postedit_spec_sync\n", encoding="utf-8")
    (linked / ".agent_reports/spec/prd.md").write_text(
        "# Shadow\n- SHADOW_ONLY epoch = 30 and a_postedit_spec_sync\n", encoding="utf-8")
    (linked / "source.py").write_text("epoch = 50\n", encoding="utf-8")
    tracked = subprocess.run(["git", "-C", str(linked), "ls-files", ".agent_reports/spec/prd.md"],
                             check=True, text=True, capture_output=True).stdout.strip()
    assert tracked == ".agent_reports/spec/prd.md"
    assert (primary / ".agent_reports/spec/prd.md").read_text() != (linked / tracked).read_text()
    return primary, linked


def payload(tool_input, *, cwd, tool_name="edit"):
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": tool_name,
                       "tool_input": tool_input, "cwd": str(cwd)})


def instrumented_hook(base: Path, root: Path, *, resolver: str, reader: str, agent_home: str = ""):
    hook_dir = base / "instrumented" / "hooks"
    utilities = base / "instrumented" / "utilities"
    hook_dir.mkdir(parents=True)
    utilities.mkdir(parents=True)
    shutil.copy2(HOOK, hook_dir / HOOK.name)
    (utilities / "artifact-root.sh").write_text(resolver, encoding="utf-8")
    (utilities / "artifact_reader.py").write_text(reader, encoding="utf-8")
    if agent_home:
        (utilities / "agent-home.sh").write_text(agent_home, encoding="utf-8")
        (utilities / "agent-home.sh").chmod(0o755)
    (utilities / "artifact-root.sh").chmod(0o755)
    return hook_dir / HOOK.name


class SpecSyncNudgeWiringTest(unittest.TestCase):
    def test_linked_worktree_native_payloads_resolve_identical_canonical_lines(self):
        with tempfile.TemporaryDirectory(prefix="spec-sync-wiring-") as raw:
            base = Path(raw)
            _, linked = make_linked_fixture(base)
            env = clean_env(base / "home")
            inputs = [
                ("Claude Edit", {"file_path": "source.py", "old_string": "epoch = 30", "new_string": "epoch = 50"}),
                ("Claude MultiEdit", {"edits": [{"file_path": "source.py", "old_string": "epoch = 30", "new_string": "epoch = 50"}]}),
                ("Codex command patch", {"command": ["apply_patch", PATCH]}),
                ("Codex input patch", {"input": PATCH}),
                ("OpenCode edit", {"filePath": "source.py", "oldString": "epoch = 30", "newString": "epoch = 50"}),
                ("OpenCode patch", {"patchText": PATCH}),
            ]
            outputs = []
            for label, tool_input in inputs:
                result = run(["bash", str(HOOK)], cwd=linked, env=env,
                             input=payload(tool_input, cwd=linked))
                self.assertEqual(result.returncode, 0, label + result.stderr)
                data = json.loads(result.stdout)
                body = data["hookSpecificOutput"]["additionalContext"]
                self.assertIn("CANONICAL_ONLY", body, label)
                self.assertIn(EXPECTED, body, label)
                self.assertNotIn("SHADOW_ONLY", body, label)
                outputs.append(body)
            self.assertTrue(all(body == outputs[0] for body in outputs), outputs)

            for tool_input in (inputs[1][1], inputs[2][1]):
                result = run(["python3", str(BRIDGE)], cwd=linked, env=env,
                             input=payload(tool_input, cwd=linked, tool_name="apply_patch"))
                self.assertEqual(result.returncode, 0, result.stderr)
                decoded = json.JSONDecoder().raw_decode(result.stdout)
                self.assertEqual(result.stdout[decoded[1]:].strip(), "")
                data = decoded[0]
                self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "PostToolUse")
                self.assertIsInstance(data["hookSpecificOutput"]["additionalContext"], str)
                self.assertEqual(data["hookSpecificOutput"]["additionalContext"], outputs[0])
                self.assertEqual(result.stderr, "")

            launched = run(["sh", str(ROOT / "adapters/codex/hooks/run-hook.sh"), "posttooluse-spec-sync.py"],
                           cwd=linked, env=env, input=payload(inputs[1][1], cwd=linked, tool_name="apply_patch"))
            self.assertEqual(launched.returncode, 0, launched.stderr)
            self.assertEqual(json.loads(launched.stdout), json.loads(run(
                ["python3", str(BRIDGE)], cwd=linked, env=env,
                input=payload(inputs[1][1], cwd=linked, tool_name="apply_patch")).stdout))

    def test_real_opencode_plugin_appends_shared_context_for_edits_and_workers(self):
        if not NODE:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory(prefix="spec-sync-opencode-") as raw:
            base = Path(raw)
            _, linked = make_linked_fixture(base)
            script = r'''import assert from "node:assert/strict";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";
childProcess.spawnSync = (command, args, options = {}) => {
  return { status: 0, stdout: "", stderr: "" };
};
syncBuiltinESMExports();
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.PLUGIN_PATH));
const ctx = { directory: process.env.FIXTURE_CWD, worktree: process.env.FIXTURE_CWD, client: {} };
const hooks = await AgentHarnessGuards(ctx, { initialize: false });
for (const [tool, args] of [["edit", { filePath: "source.py", oldString: "epoch = 30", newString: "epoch = 50" }],
  ["apply_patch", { patchText: process.env.PATCH_TEXT }]]) {
  const output = { output: "TOOL-OUTPUT", metadata: {} };
  await hooks["tool.execute.after"]({ tool, args, sessionID: "fixture-session" }, output);
  assert.ok(output.output.startsWith("TOOL-OUTPUT\n"), JSON.stringify(output.output));
  assert.ok(output.output.includes("CANONICAL_ONLY"));
  assert.ok(output.output.includes("epoch = 30"));
  assert.ok(!output.output.includes("SHADOW_ONLY"));
  assert.equal(output.output.split("Spec synchronization nudge").length - 1, 1);
}
hooks.dispose?.({ silent: true });
'''
            for worker_env in ({}, {"AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_CHILD": "1",
                                  "AGENT_DISPATCH_DEPTH": "2"}):
                env = clean_env(base / "home", PLUGIN_PATH=ROOT / "adapters/opencode/plugins/hearting-guards.js",
                                FIXTURE_CWD=linked, PATCH_TEXT=PATCH, **worker_env)
                result = run([NODE, "--input-type=module", "-e", script], cwd=ROOT, env=env, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_opencode_v2_returns_context_in_the_native_tool_result(self):
        if not NODE:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory(prefix="spec-sync-v2-") as raw:
            base = Path(raw)
            _, linked = make_linked_fixture(base)
            script = r'''import assert from "node:assert/strict";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";
childProcess.spawnSync = () => ({ status: 0, stdout: "", stderr: "" });
syncBuiltinESMExports();
const plugin = (await import(pathToFileURL(process.env.PLUGIN_PATH))).default;
const hooks = new Map();
const ctx = {
  location: { directory: process.env.FIXTURE_CWD },
  session: {
    get: async ({ sessionID }) => ({ id: sessionID,
      title: sessionID === "worker" ? "hearting-worker-att-fixture" : "Main work" }),
    hook: async (name, fn) => hooks.set("session." + name, fn),
  },
  tool: { hook: async (name, fn) => hooks.set("tool." + name, fn) },
  shell: { hook: async () => {} },
  permission: { hook: async () => {} },
  event: { subscribe: async function* ({ signal }) {
    await new Promise(resolve => signal.addEventListener("abort", resolve, { once: true }));
  } },
};
const cleanup = await plugin.setup(ctx);
try {
  for (const sessionID of ["main", "worker"]) {
    for (const [tool, input] of [
      ["edit", { filePath: "source.py", oldString: "epoch = 30", newString: "epoch = 50" }],
      ["apply_patch", { patchText: process.env.PATCH_TEXT }],
    ]) {
      const original = { content: [{ type: "text", text: "TOOL-OUTPUT" },
        { type: "image", data: "preserve-image", mimeType: "image/png" }],
        metadata: { unchanged: true }, title: "original title" };
      const event = { sessionID, tool, input, status: "completed", result: original };
      await hooks.get("tool.execute.after")(event);
      assert.deepEqual(event.result.content.slice(0, 2), original.content);
      assert.deepEqual(event.result.metadata, original.metadata);
      assert.equal(event.result.title, original.title);
      const context = event.result.content.slice(2).map(part => part.text || "").join("\n");
      assert.ok(context.includes("CANONICAL_ONLY"), `${sessionID}/${tool}: ${JSON.stringify(event)}`);
      assert.ok(!context.includes("SHADOW_ONLY"));
      assert.equal(context.split("Spec synchronization nudge").length - 1, 1);
      const failure = { sessionID, tool, input, status: "error", error: { message: "edit failed" } };
      const before = JSON.stringify(failure);
      await hooks.get("tool.execute.after")(failure);
      assert.equal(JSON.stringify(failure), before, "failed tool result must remain unchanged");
    }
  }
} finally { await cleanup(); }
'''
            env = clean_env(base / "home", PLUGIN_PATH=ROOT / "adapters/opencode/plugins/hearting-guards.js",
                            FIXTURE_CWD=linked, PATCH_TEXT=PATCH)
            result = run([NODE, "--input-type=module", "-e", script], cwd=ROOT, env=env, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_noise_filter_caps_fail_open_and_reader_priority(self):
        with tempfile.TemporaryDirectory(prefix="spec-sync-noise-") as raw:
            base = Path(raw)
            primary, linked = make_linked_fixture(base)
            env = clean_env(base / "home")
            source = linked / "source.py"
            source.write_text("epoch = 50\nnoise_probe = True\n", encoding="utf-8")
            noise = run(["bash", str(HOOK)], cwd=linked, env=env,
                        input=payload({"file_path": "source.py", "old_string": "noise_probe", "new_string": "gone"}, cwd=linked))
            self.assertEqual(noise.returncode, 0)
            self.assertEqual(noise.stdout, "")
            capped_env = {**env, "SPEC_SYNC_HITS": "1", "SPEC_SYNC_TOKENS": "1"}
            capped = run(["bash", str(HOOK), "--format", "text"], cwd=linked, env=capped_env,
                         input=payload({"file_path": "source.py", "old_string": "epoch = 30 SD-31", "new_string": "epoch = 50 SD-32"}, cwd=linked))
            self.assertEqual(capped.returncode, 0)
            self.assertLessEqual(capped.stdout.count("  spec/"), 1)
            zero = run(["bash", str(HOOK), "--format", "text"], cwd=linked,
                       env={**env, "SPEC_SYNC_HITS": "0"},
                       input=payload({"file_path": "source.py", "old_string": "epoch = 30", "new_string": "epoch = 50"}, cwd=linked))
            self.assertEqual(zero.stdout, "")
            failed = run(["bash", str(HOOK)], cwd=linked, env={**env, "SPEC_SYNC_NUDGE": "1"}, input="{")
            self.assertEqual((failed.returncode, failed.stdout, failed.stderr), (0, "", ""))

            shared = primary / ".agent_reports/shared/spec/ref_fixture/revisions/rrev_fixture"
            shared.mkdir(parents=True)
            (shared / "prd.md").write_text("# Shared\n- SHARED_ONLY epoch = 30\n", encoding="utf-8")
            resolved = run(["python3", str(ROOT / "utilities/artifact_reader.py"), "spec-dir",
                            "--artifact-root", str(primary / ".agent_reports")], cwd=ROOT, env=env)
            self.assertEqual(json.loads(resolved.stdout)["path"], str(shared))
            shared_hit = run(["bash", str(HOOK), "--format", "text"], cwd=linked,
                             env={**env, "AGENT_ARTIFACT_ROOT": str(primary / ".agent_reports")},
                             input=payload({"file_path": "source.py", "old_string": "epoch = 30", "new_string": "epoch = 50"}, cwd=linked))
            self.assertIn("SHARED_ONLY", shared_hit.stdout)
            self.assertNotIn("CANONICAL_ONLY", shared_hit.stdout)
            # Add a separate open-cycle tree; reader selection remains the sole authority.
            cycle = base / "cycle"
            cycle_spec = cycle / "artifacts/spec"
            cycle_spec.mkdir(parents=True)
            (cycle_spec / "prd.md").write_text("# Cycle\n- CYCLE_ONLY epoch = 30\n", encoding="utf-8")
            resolved_cycle = run(["python3", str(ROOT / "utilities/artifact_reader.py"), "spec-dir",
                                  "--artifact-root", str(primary / ".agent_reports"), "--cycle-dir", str(cycle)], cwd=ROOT, env=env)
            self.assertEqual(json.loads(resolved_cycle.stdout)["path"], str(cycle_spec))
            cycle_hit = run(["bash", str(HOOK), "--format", "text"], cwd=linked,
                            env={**env, "AGENT_ARTIFACT_ROOT": str(primary / ".agent_reports"),
                                 "AGENT_ARTIFACT_CYCLE_DIR": str(cycle)},
                            input=payload({"file_path": "source.py", "old_string": "epoch = 30", "new_string": "epoch = 50"}, cwd=linked))
            self.assertIn("CYCLE_ONLY", cycle_hit.stdout)
            self.assertNotIn("SHARED_ONLY", cycle_hit.stdout)

    def test_failed_reader_never_falls_back_to_legacy_spec(self):
        for reader_body in ("import sys\nsys.exit(7)\n", "print('{not-json')\n"):
            with self.subTest(reader=reader_body), tempfile.TemporaryDirectory(prefix="spec-sync-reader-fail-") as raw:
                base = Path(raw)
                root = base / "legacy-root"
                legacy = root / "spec"
                legacy.mkdir(parents=True)
                (legacy / "prd.md").write_text("# Legacy\n- epoch = 30\n", encoding="utf-8")
                source = base / "source.py"
                source.write_text("epoch = 50\n", encoding="utf-8")
                resolver = "#!/bin/sh\nprintf '%s\\n' \"$FIXTURE_ARTIFACT_ROOT\"\n"
                hook = instrumented_hook(base, root, resolver=resolver, reader=reader_body)
                env = clean_env(base / "home", FIXTURE_ARTIFACT_ROOT=root)
                result = run(["bash", str(hook), "--format", "text"], cwd=base, env=env,
                             input=payload({"file_path": str(source), "old_string": "epoch = 30",
                                            "new_string": "epoch = 50"}, cwd=base))
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_no_candidates_launch_no_helper_subprocesses(self):
        with tempfile.TemporaryDirectory(prefix="spec-sync-no-subprocess-") as raw:
            base = Path(raw)
            root = base / "root"
            calls = base / "calls"
            source = base / "source.py"
            source.write_text("noise_probe = True\n", encoding="utf-8")
            resolver = "#!/bin/sh\necho resolver >> \"$FIXTURE_CALLS\"\nprintf '%s\\n' \"$FIXTURE_ARTIFACT_ROOT\"\n"
            reader = "from pathlib import Path\nimport os\nPath(os.environ['FIXTURE_CALLS']).write_text('reader\\n', encoding='utf-8')\n"
            agent_home = "#!/bin/sh\necho agent-home >> \"$FIXTURE_CALLS\"\n"
            hook = instrumented_hook(base, root, resolver=resolver, reader=reader, agent_home=agent_home)
            env = clean_env(base / "home", FIXTURE_ARTIFACT_ROOT=root, FIXTURE_CALLS=calls)
            result = run(["bash", str(hook), "--format", "text"], cwd=base, env=env,
                         input=payload({"file_path": str(source), "old_string": "noise_probe",
                                        "new_string": "noise_probe"}, cwd=base))
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertFalse(calls.exists())

    def test_deadline_bounds_lookup_and_returns_silently(self):
        with tempfile.TemporaryDirectory(prefix="spec-sync-deadline-") as raw:
            base = Path(raw)
            root = base / "root"
            calls = base / "calls"
            source = base / "source.py"
            source.write_text("epoch = 50\n", encoding="utf-8")
            resolver = "#!/bin/sh\necho resolver >> \"$FIXTURE_CALLS\"\nsleep 2\nprintf '%s\\n' \"$FIXTURE_ARTIFACT_ROOT\"\n"
            reader = "from pathlib import Path\nimport os\nPath(os.environ['FIXTURE_CALLS']).write_text('reader\\n', encoding='utf-8')\n"
            hook = instrumented_hook(base, root, resolver=resolver, reader=reader)
            env = clean_env(base / "home", FIXTURE_ARTIFACT_ROOT=root, FIXTURE_CALLS=calls,
                            SPEC_SYNC_DEADLINE_MS="100")
            started = time.monotonic()
            result = run(["bash", str(hook), "--format", "text"], cwd=base, env=env,
                         input=payload({"file_path": str(source), "old_string": "epoch = 30",
                                        "new_string": "epoch = 50"}, cwd=base), timeout=3)
            elapsed = time.monotonic() - started
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertLess(elapsed, 1.0, f"deadline did not bound resolver: {elapsed:.3f}s")
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["resolver"])

    def test_slow_reader_with_multiple_files_and_candidates_obeys_aggregate_deadline(self):
        with tempfile.TemporaryDirectory(prefix="spec-sync-reader-deadline-") as raw:
            base = Path(raw)
            root = base / "root"
            spec = root / "spec"
            spec.mkdir(parents=True)
            (spec / "prd.md").write_text("# Spec\n- epoch = 30 and a_postedit_spec_sync\n", encoding="utf-8")
            source_a = base / "source-a.py"
            source_b = base / "source-b.py"
            source_a.write_text("epoch = 50\n", encoding="utf-8")
            source_b.write_text("value = True\n", encoding="utf-8")
            calls = base / "calls"
            resolver = "#!/bin/sh\nprintf '%s\\n' \"$FIXTURE_ARTIFACT_ROOT\"\n"
            reader = ("import json, os, time\nfrom pathlib import Path\n"
                      "with Path(os.environ['FIXTURE_CALLS']).open('a', encoding='utf-8') as f: f.write('reader\\n')\n"
                      "time.sleep(2)\nprint(json.dumps({'path': os.path.join(os.environ['FIXTURE_ARTIFACT_ROOT'], 'spec')}))\n")
            hook = instrumented_hook(base, root, resolver=resolver, reader=reader)
            env = clean_env(base / "home", FIXTURE_ARTIFACT_ROOT=root, FIXTURE_CALLS=calls,
                            SPEC_SYNC_DEADLINE_MS="120")
            started = time.monotonic()
            result = run(["bash", str(hook), "--format", "text"], cwd=base, env=env,
                         input=payload({"edits": [
                             {"file_path": str(source_a), "old_string": "epoch = 30", "new_string": "epoch = 50"},
                             {"file_path": str(source_b), "old_string": "a_postedit_spec_sync", "new_string": "removed_identifier"},
                         ]}, cwd=base), timeout=3)
            elapsed = time.monotonic() - started
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertLess(elapsed, 1.0, f"slow reader escaped aggregate deadline: {elapsed:.3f}s")
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["reader"])

    def test_deadline_override_is_clamped_without_waiting_for_timeout(self):
        source = HOOK.read_text(encoding="utf-8")
        embedded = source.split("NUDGE_PY=$(cat <<'PY'\n", 1)[1].split("\nPY\n)", 1)[0]
        namespace = {"__name__": "spec_sync_deadline_contract"}
        exec(embedded.split("def main():", 1)[0], namespace)
        with mock.patch.dict(os.environ, {"SPEC_SYNC_DEADLINE_MS": "9000"}):
            self.assertEqual(namespace["deadline_ms"](), 6000)

    def test_patch_move_destination_and_add_delete_blocks(self):
        with tempfile.TemporaryDirectory(prefix="spec-sync-patch-blocks-") as raw:
            base = Path(raw)
            primary, linked = make_linked_fixture(base)
            canonical_spec = primary / ".agent_reports/spec/prd.md"
            canonical_spec.write_text(canonical_spec.read_text(encoding="utf-8") +
                                     "\n- ADD_ONLY = True\n- DELETE_ONLY = True\n", encoding="utf-8")
            (linked / "destination.py").write_text("epoch = 30\n", encoding="utf-8")
            patch = ("*** Begin Patch\n"
                     "*** Update File: old-name.py\n*** Move to: destination.py\n"
                     "@@\n-epoch = 30\n+epoch = 50\n"
                     "*** Add File: added.py\n@@\n+ADD_ONLY = True\n"
                     "*** Delete File: deleted.py\n@@\n-DELETE_ONLY = True\n"
                     "*** End Patch\n")
            result = run(["bash", str(HOOK), "--format", "text"], cwd=linked,
                         env=clean_env(base / "home"),
                         input=payload({"patchText": patch}, cwd=linked, tool_name="apply_patch"))
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_old_payload_removing_postedit_spec_identifier_is_detected(self):
        with tempfile.TemporaryDirectory(prefix="spec-sync-postedit-signal-") as raw:
            base = Path(raw)
            _, linked = make_linked_fixture(base)
            result = run(["bash", str(HOOK), "--format", "text"], cwd=linked,
                         env=clean_env(base / "home"),
                         input=payload({"file_path": "source.py", "old_string": "a_postedit_spec_sync",
                                        "new_string": "removed_identifier"}, cwd=linked))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("a_postedit_spec_sync", result.stdout)

    def test_codex_posttooluse_array_preserves_original_five_entries(self):
        baseline = json.loads(subprocess.run(["git", "show", f"{BASELINE}:adapters/codex/hooks/hooks.json"],
                                             cwd=ROOT, check=True, text=True, capture_output=True).stdout)
        current = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(current["hooks"]["PostToolUse"][:-1], baseline["hooks"]["PostToolUse"])
        self.assertEqual(len(current["hooks"]["PostToolUse"]), len(baseline["hooks"]["PostToolUse"]) + 1)
        self.assertEqual(current["hooks"]["PostToolUse"][-1]["matcher"],
                         r"Edit|Write|MultiEdit|apply_patch|functions\.apply_patch")
        self.assertIn("posttooluse-spec-sync.py", current["hooks"]["PostToolUse"][-1]["hooks"][0]["command"])
        for event in set(baseline["hooks"]) - {"PostToolUse"}:
            self.assertEqual(current["hooks"][event], baseline["hooks"][event], event)


if __name__ == "__main__":
    unittest.main(verbosity=2)
