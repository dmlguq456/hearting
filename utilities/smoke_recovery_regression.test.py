#!/usr/bin/env python3
"""PR #1594 regressions from Claude probes p05a2/p05b/p05c/p07/p14. No model or GPU."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import shlex
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

BASE = _load("rv_integration_fixture", "utilities/review_source_subject_integration.test.py")
DC, CAP = BASE.DC, BASE.CAP
ROUTE_WRITER, DISPATCH_NODE = BASE.ROUTE_WRITER, BASE.DISPATCH_NODE
route_hash, route_id_from_hash = BASE.route_hash, BASE.route_id_from_hash
SMOKE = _load("rv_smoke_tool", "tools/smoke-attestation.py")
def clean_env():
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("AGENT_", "CLAUDE", "CODEX", "OPENCODE", "HEARTING"))}

class SmokeRecoveryRegressionTest(unittest.TestCase):
    setUp = BASE.SourceSubjectIntegrationTest.setUp

    git = BASE.SourceSubjectIntegrationTest.git

    _metadata = BASE.SourceSubjectIntegrationTest._metadata

    complete_node = BASE.SourceSubjectIntegrationTest.complete_node

    _row = BASE.SourceSubjectIntegrationTest._row

    prepare_review = BASE.SourceSubjectIntegrationTest.prepare_review

    get_node = BASE.SourceSubjectIntegrationTest.get_node

    def smoke_route(self, inputs=("experiment-scaffold", "config")):
        producer = {"id": "scaffold", "kind": "pipeline-stage", "dispatch_depth": 2,
                    "completion_gate": "experiment-scaffold", "depends_on": [],
                    "write_scope": ["experiment-scaffold/**", "config/**", "configs/**"]}
        node = {"id": "smoke", "kind": "review-worker", "unit": "qa/ml-debug", "dispatch_depth": 2,
                "completion_gate": "hash-bound-smoke", "depends_on": ["scaffold"],
                "inputs": list(inputs), "write_scope": ["reviews/smoke-attestation.json"]}
        review = {"id": "impl-review", "kind": "review-worker", "unit": "qa/code-review",
                  "completion_gate": "code-impl-review", "dispatch_depth": 2,
                  "write_scope": ["_internal/dev_reviews/**"], "depends_on": ["scaffold"]}
        self.route["nodes"] = [producer, node, review, {"id": "full-run", "kind": "resource-runner",
            "completion_gate": "resource-exit", "dispatch_depth": 2, "depends_on": ["smoke"]}]
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.route_file.write_text(json.dumps(self.route))
        self.scaffold = self.artifact_root / "experiment-scaffold"
        self.scaffold.mkdir(exist_ok=True)
        self.batch = self.scaffold / "batch.py"
        self.batch.write_text("from pathlib import Path\nimport sys\n"
                              "arg = sys.argv[1] if len(sys.argv) > 1 else None\n"
                              "Path('seen.txt').write_text(Path(arg).read_text() if arg else 'none')\n")
        cfg_dir = self.artifact_root / ("configs" if "configs" in inputs else "config")
        cfg_dir.mkdir(exist_ok=True)
        self.config = cfg_dir / "run.yaml"
        self.config.write_text("steps: 1\n")
        self.complete_node("scaffold", "att-scaffold", "prepared")
        self.evidence = self.artifact_root / "reviews/smoke-attestation.json"
        self.evidence.parent.mkdir(exist_ok=True)
        return node

    def attest(self, inputs, command, manifest=None):
        argv = [sys.executable, str(ROOT / "tools/smoke-attestation.py"), "attest"]
        for item in inputs:
            argv += ["--input", str(item)]
        if manifest:
            argv += ["--config-manifest", str(manifest)]
        argv += ["--cwd", str(self.repo), "--output", str(self.evidence), "--", *command]
        done = subprocess.run(argv, env=clean_env(), capture_output=True, text=True, timeout=60)
        return done

    def inline_marker(self, node, aid):
        metadata = {**self._metadata(node, aid, "review"), "registered_worker": "0",
                    "execution_surface": "inline", "transport": "interactive", "fallback_hop": "inline"}
        return ROUTE_WRITER._publish_completion_locked(self.route, node, node["id"], self.evidence,
            jobs=self.jobs, attempt_id=aid, attempt_metadata=metadata)

    def registered_verdict(self, node, aid, passed=True, marker=False):
        self.review_node = node
        if passed:
            md = self.prepare_review(aid)
        else:
            md = self.prepare_review(aid, note="completed-review-blocking", extra={"failure_class": "fail"})
        if marker:
            return md, ROUTE_WRITER._publish_completion_locked(self.route, node, node["id"], self.evidence,
                jobs=self.jobs, attempt_id=aid, attempt_metadata=md)
        return md, None

    def budget(self, node):
        return DISPATCH_NODE.admit_round(self.route, node, self.jobs, record_auto_revisions=False).budget

    def recovery(self, node, state="exhausted"):
        return CAP.recovery_fields("review-worker", state, route=self.route, node=node,
                                   jobs=self.jobs, route_file=self.route_file)

    def _outside_input_setup(self, registered_pass):
        node = self.smoke_route()
        outside = self.repo / "engine.py"   # attestation input outside node inputs
        done = self.attest([self.scaffold, self.config.parent, outside], [sys.executable, str(self.batch)])
        self.assertEqual(done.returncode, 0, done.stderr)
        if registered_pass:
            self.registered_verdict(node, "att-A1-pass", passed=True, marker=True)
        else:
            self.inline_marker(node, "att-inline-A")
        return node, outside

    def _manifest_setup(self, kind):
        node = self.smoke_route(inputs=("experiment-scaffold", "configs"))
        def seal():
            sealed = subprocess.run([sys.executable, str(ROOT / "tools/lab-config-provenance.py"), "seal",
                "--repo", str(self.artifact_root), "--config", "run.yaml", "--slug", "ami",
                "--artifact-root", str(self.artifact_root)], capture_output=True, text=True,
                env=clean_env(), timeout=60)
            self.assertEqual(sealed.returncode, 0, sealed.stdout + sealed.stderr)
            run_id = json.loads(sealed.stdout)["run_id"]
            manifest = self.artifact_root / "experiments/ami/_internal/configs" / (run_id + ".manifest.json")
            return manifest, json.loads(manifest.read_text())
        manifest, m = seal()
        target = m["snapshot_path"] if kind else m["source_path"]
        if kind == "relative":
            target = os.path.relpath(target, self.repo)
        elif kind == "option":
            target = "--config=" + target
            self.batch.write_text(self.batch.read_text().replace(
                "Path(arg).read_text()", "Path(arg.split('=', 1)[-1]).read_text()"))
        elif kind == "manifest":
            target = str(manifest)
            self.batch.write_text("import json\n" + self.batch.read_text().replace(
                "Path(arg).read_text()", "Path(json.loads(Path(arg).read_text())['snapshot_path']).read_text()"))
        command = [sys.executable, str(self.batch), target]
        if kind == "embedded":
            command = [sys.executable, "-c", "from pathlib import Path; "
                       f"Path('seen.txt').write_text(Path({target!r}).read_text())"]
        done = self.attest([self.scaffold, self.config.parent], command, manifest=manifest)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((self.repo / "seen.txt").read_text(), "steps: 1\n")
        self.inline_marker(node, "att-inline-cfg1")
        return node, seal

    def _quiescent(self):
        from types import SimpleNamespace
        return BASE.patch_split(DC, "attempt_process_quiescence",
            return_value=SimpleNamespace(state="quiescent", reason="fixture-finished-worker"))

    def reuse(self, node):
        return CAP.reuse_deterministic_pass(self.route, node, self.jobs, self.budget(node))

    def replay(self, node, expected=0):
        fields = self.recovery(node)
        self.assertNotIn('recovery_unavailable', fields)
        argv = shlex.split(fields['recovery_run_command'])
        output = Path(argv[argv.index('--output') + 1])
        result = subprocess.run(argv, env=clean_env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(shlex.split(fields['recovery_check_command'])[-1], str(output))
        self.assertEqual(shlex.split(fields['recovery_complete_command'])[-1], str(output))
        return output

    def test_p05a2_registered_pass_outside_input_aba_fail(self):
        with self._quiescent():
            node, outside = self._outside_input_setup(registered_pass=True)
            self.assertIsNotNone(self.reuse(node))
            outside.write_text("value = 'B'\n")
            self.registered_verdict(node, 'att-B-fail', passed=False)
            outside.write_text("value = 'A'\n")
            self.assertEqual(self.budget(node).state, 'exhausted')
            self.assertIsNone(self.reuse(node), 'old PASS must not erase the newer FAIL')

    def test_p05b_inline_pass_outside_input_aba_two_fails(self):
        node, outside = self._outside_input_setup(registered_pass=False)
        self.assertIsNotNone(self.reuse(node))
        outside.write_text("value = 'B'\n")
        self.registered_verdict(node, 'att-B-fail-1', passed=False)
        self.registered_verdict(node, 'att-B-fail-2', passed=False)
        outside.write_text("value = 'A'\n")
        self.assertEqual(self.budget(node).state, 'exhausted')
        self.assertIsNone(self.reuse(node), 'inline PASS must not erase newer FAILs')

    def test_p05c_registered_pass_environment_aba_fail(self):
        with self._quiescent():
            node, _ = self._outside_input_setup(registered_pass=True)
            self.assertIsNotNone(self.reuse(node))
            with mock.patch.dict(os.environ, {'CONDA_PREFIX': '/fixture/other-env'}):
                self.assertIsNone(self.reuse(node))
                self.registered_verdict(node, 'att-E2-fail', passed=False)
            self.assertIsNone(self.reuse(node), 'restoring E1 must not erase E2 FAIL')

    def config_replay(self, snapshot):
        node, seal = self._manifest_setup(snapshot)
        self.config.write_text('steps: 2\n')
        self.assertIsNone(self.reuse(node))
        unavailable = self.recovery(node)
        self.assertEqual(unavailable.get('recovery_unavailable'), 'current-sealed-config-manifest-unavailable')
        self.assertNotIn('recovery_run_command', unavailable)
        _, current = seal()
        self.evidence = self.replay(node)
        fresh = json.loads(self.evidence.read_text())
        self.assertTrue(SMOKE.verify(fresh))
        self.assertEqual(fresh['config_sha256'], current['snapshot_sha256'])
        self.assertEqual((self.repo / 'seen.txt').read_text(), 'steps: 2\n',
                         'the batch must read the configuration that was attested')
        self.inline_marker(node, 'att-current-config')
        DC.completion_marker_gate(str(self.route_file), 'full-run', 'start', self.agent_home, self.jobs)

    def test_p07_replay_snapshot_argv(self):
        self.config_replay(True)

    def test_snapshot_relative_operand(self):
        self.config_replay("relative")

    def test_snapshot_option_operand(self):
        self.config_replay("option")

    def test_manifest_operand(self):
        self.config_replay("manifest")

    def test_embedded_snapshot_refuses_unsafe_rewrite(self):
        node, seal = self._manifest_setup("embedded")
        self.config.write_text('steps: 2\n')
        seal()
        fields = self.recovery(node)
        self.assertEqual(fields.get('recovery_unavailable'), 'recorded-smoke-command-binds-old-config')
        self.assertNotIn('recovery_run_command', fields)

    def test_p07b_replay_source_argv(self):
        self.config_replay(False)

    def test_p14_failed_replay_preserves_command_for_next_attempt(self):
        with self._quiescent():
            node, outside = self._outside_input_setup(registered_pass=True)
            self.registered_verdict(node, 'att-A2-pass', passed=True)
            old_path, old_bytes = self.evidence, self.evidence.read_bytes()
            outside.write_text("value = 'fixed'\n")
            original = self.batch.read_text()
            self.batch.write_text('raise SystemExit(7)\n' + original)
            failed = self.replay(node, expected=7)
            with self.assertRaisesRegex(ValueError, 'smoke did not pass'):
                SMOKE.verify(json.loads(failed.read_text()))
            self.batch.write_text(original)
            self.assertIsNone(self.reuse(node))
            second = self.recovery(node)
            self.assertIn('recovery_run_command', second, 'failed replay lost its recorded command')
            self.assertEqual(old_path.read_bytes(), old_bytes)
            self.assertNotEqual(failed, old_path)
            before = self.jobs.read_bytes()
            self.evidence = self.replay(node)
            self.assertTrue(SMOKE.verify(json.loads(self.evidence.read_text())))
            self.inline_marker(node, 'att-fixed-inline')
            self.assertEqual(self.jobs.read_bytes(), before)
            self.assertIsNotNone(self.reuse(node))
            DC.completion_marker_gate(str(self.route_file), 'full-run', 'start', self.agent_home, self.jobs)
            # A second failure after publishing the recheck must preserve that
            # marker too, not merely protect the original fixed filename once.
            current_path, current_bytes = self.evidence, self.evidence.read_bytes()
            outside.write_text("value = 'next'\n")
            self.batch.write_text('raise SystemExit(7)\n' + original)
            failed_again = self.replay(node, expected=7)
            self.assertNotEqual(failed_again, current_path)
            self.assertEqual(current_path.read_bytes(), current_bytes)
            self.assertIn('recovery_run_command', self.recovery(node))

    def test_p01_p04_inline_reuse_is_read_only_without_intervening_fail(self):
        node, outside = self._outside_input_setup(registered_pass=False)
        before = self.jobs.read_bytes()
        for _ in range(3):
            self.assertEqual(self.reuse(node)['required_action'], 'use-current-pass')
        outside.write_text("value = 'B'\n")
        self.assertIsNone(self.reuse(node))
        outside.write_text("value = 'A'\n")
        self.assertIsNotNone(self.reuse(node))
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_p13b_latest_registered_pass_reusable_at_cap(self):
        with self._quiescent():
            node, _ = self._outside_input_setup(registered_pass=True)
            self.registered_verdict(node, 'att-A2-pass', marker=True)
            before = self.jobs.read_bytes()
            self.assertEqual(self.budget(node).state, 'exhausted')
            self.assertEqual(self.reuse(node)['attempt_id'], 'att-A2-pass')
            self.assertEqual(self.jobs.read_bytes(), before)

    def test_p06_foreign_route_and_missing_registry_row_refuse_reuse(self):
        with self._quiescent():
            node, _ = self._outside_input_setup(registered_pass=True)
            other = dict(self.route, route_id='rt-other')
            self.assertIsNone(CAP.reuse_deterministic_pass(other, node, self.jobs, self.budget(node)))
            self.jobs.write_text('\n'.join(line for line in self.jobs.read_text().splitlines()
                                            if 'att-A1-pass' not in line) + '\n')
            self.assertIsNone(self.reuse(node))

    def test_p09_no_record_and_p11_model_review_keep_existing_refusal(self):
        node = self.smoke_route()
        fields = self.recovery(node)
        self.assertEqual(fields['recovery_unavailable'], 'recorded-smoke-command-unavailable')
        self.assertNotIn('recovery_run_command', fields)
        review = self.get_node('impl-review')
        self.registered_verdict(review, 'att-model-fail1', passed=False)
        self.registered_verdict(review, 'att-model-fail2', passed=False)
        self.assertEqual(self.budget(review).state, 'exhausted')
        self.assertIsNone(self.reuse(review))
        fields = self.recovery(review)
        self.assertEqual(fields['required_action'], 'report-unresolved-verification')
        self.assertNotIn('recovery_run_command', fields)


if __name__ == '__main__':
    unittest.main()
