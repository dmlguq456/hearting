#!/usr/bin/env python3
"""Sequential supervised resources retain one stage and its output requirement."""
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('sequence_fixture', HERE / 'workflow_supervisor.test.py')
FIX = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = FIX
spec.loader.exec_module(FIX)
SUP, WS = FIX.SUP, FIX.WS
ROOT = FIX.ROOT
import dispatch_resource_wait as WAIT
import dispatch_contract as DC
import route_authority as RA


V3532_READ_ARMED_SOURCE = '''def read_armed(ledger):
    rows = {}
    directory = armed_dir(ledger)
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict) and row.get("node"):
            rows[row["node"]] = row
    return rows
'''
V3532_READ_ARMED_SHA256 = "3887ab00b30ef79bcbdc4a09fa9a624efbce9bd606eead194cf540a62ea4b883"


class ResourceSequenceTest(FIX.WorkflowFixture):
    EMPTY_LAUNCH_REQUEST = {"smoke_attestation": None, "smoke_attestation_sha256": None,
                            "config_manifest": None, "config_manifest_sha256": None}
    _resume_fixture = FIX.TestSupervisorAdvance._resume_fixture
    _settle_resource_owner = FIX.TestSupervisorAdvance._settle_resource_owner

    def fixture(self, *, multistage=False):
        if multistage:
            def compile_multistage(capability, capability_mode, cwd, signals):
                router = SUP.route_module()
                registry = router.TOPO.load_registry()
                recipe = copy.deepcopy(router.TOPO.resolve_recipe(
                    registry, capability, capability_mode))
                resource = next(node for node in recipe['standard_plus']['nodes']
                                if node['id'] == 'full-run')
                verify = next(node for node in recipe['standard_plus']['nodes']
                              if node['id'] == 'run-verify')
                metrics = copy.deepcopy(next(node for node in router.TOPO.resolve_recipe(
                    registry, capability, 'eval')['standard_plus']['nodes']
                    if node['id'] == 'metrics'))
                resource['depends_on'] = []
                metrics.update(depends_on=['full-run'], inputs=['raw.json', 'run.json'],
                    outputs=['summary.json', 'metrics.jsonl', 'correction.json'],
                    write_scope=['summary.json', 'metrics.jsonl', 'correction.json'])
                verify.update(id='verify', depends_on=['metrics'],
                    inputs=['summary.json', 'metrics.jsonl', 'raw.json', 'run.json'],
                    outputs=['reviews/verdict.json'], write_scope=['reviews/**'], terminal=True,
                    terminal_gate=verify['completion_gate'])
                verify.pop('continuation', None)
                # Seal the fixture graph and its profiles together, including when
                # its artifact-only cwd has no enclosing Git repository.
                for node in (metrics, verify):
                    node['profile_demand'] = {
                        'schema_version': 1, 'judgment_requirement': 'predetermined',
                        'execution_scope': 'short-local',
                        'judgment_reason': 'Hash-check fixed saved CPU output.',
                        'execution_reason': 'One bounded fixture consumer.',
                        'evidence_refs': ['fixture:saved-output-settlement']}
                recipe['standard_plus']['nodes'] = [resource, metrics, verify]
                recipe['standard_plus'].pop('parallel_groups', None)
                recipe.pop('conditional_extensions', None)
                recipe.update(completion_gates=[node['completion_gate']
                    for node in (resource, metrics, verify)], human_gates=[],
                    human_gate_bindings=[], resume_retry_boundaries=['full-run', 'metrics', 'verify'])
                route_doc = router.compile_composed_route(
                    recipe, capability_mode, 'standard', cwd, cwd,
                    predicates=[], signals=signals, transport='headless',
                    transport_evidence='fixture', tracking='tracked',
                    tracked_gate_evidence=copy.deepcopy(FIX.GATE),
                    dispatch_evidence=FIX.dispatch_evidence(cwd))
                router.verify_route(route_doc, expected_cwd=cwd)
                return route_doc
            with mock.patch.object(FIX, 'compile_fixture', side_effect=compile_multistage):
                route, path, jobs, registry, output = self._resume_fixture(ordinary=True)
        else:
            route, path, jobs, registry, output = self._resume_fixture(ordinary=True)
        row = json.loads(registry.read_text())['runs']['fixture-run']
        row['parent_attempt_id'] = 'att-parent'
        registry.write_text(json.dumps({'schema_version': 1, 'runs': {row['run_id']: row}}))
        ledger = SUP.ledger_for(route, jobs)
        arm = ledger.root / 'armed/full-run.json'
        armed = json.loads(arm.read_text())
        armed['resource_binding'] = WAIT.resource_body_digest(row)
        armed['successor_log'] = str(ledger.root / 'resource/verification-start.log')
        arm.write_text(json.dumps(armed))
        (output / 'run.json').unlink()
        return route, path, jobs, registry, output, ledger

    def failed_saved_output_fixture(self, *, multistage=False):
        route, path, jobs, registry, output, ledger = self.fixture(multistage=multistage)
        owner_attempt, owner_input = self.live_owner(route, jobs, registry, harness='claude')
        config_sha = 'a' * 64
        body = self.next_body(registry, 'saved-output-run', output)
        body.update(parent_attempt_id=owner_attempt, config_sha256=config_sha)
        payload = (
            "from pathlib import Path\nimport hashlib,json,sys\n"
            "base=Path(sys.argv[1]); route_id=sys.argv[2]; config_sha=sys.argv[3]\n"
            "raw=base/'raw.json'; data=b'{\\\"scientific_verdict\\\":\\\"FAIL\\\",\\\"scores\\\":[1,2,3]}\\n'; raw.write_bytes(data)\n"
            "run={'route_id':route_id,'exit':1,'config_sha256':config_sha,'outputs':["
            "{'path':'raw.json','sha256':hashlib.sha256(data).hexdigest()}]}\n"
            "(base/'run.json').write_text(json.dumps(run))\nraise SystemExit(1)\n"
        )
        body['command'] = [sys.executable, '-c', payload, str(output), route['route_id'], config_sha]
        data = json.loads(registry.read_text())
        data['runs'].pop('fixture-run')
        registry.write_text(json.dumps(data))
        (ledger.root / 'armed' / 'full-run.json').unlink()
        self.launch(route, path, jobs, registry, output, body, exit_code=1)
        result = SUP.poll_once(route, ledger)
        self.assertEqual(result[0]['action'], 'halt-failed')
        current = json.loads(registry.read_text())['runs']['saved-output-run']
        self.assertEqual((current['status'], current['exit_code']), ('failed', 1))
        return (route, path, jobs, registry, output, ledger, owner_attempt, owner_input,
                output / 'raw.json', output / 'run.json')

    def test_saved_nonzero_outputs_enter_verification_before_any_pass_marker(self):
        route, path, jobs, registry, output, ledger, owner_attempt, _owner_input, raw, receipt = \
            self.failed_saved_output_fixture()
        completion = SUP.route_module().completion_dir(route['route_id'], jobs=jobs)
        self.assertFalse((completion / 'full-run.json').exists())
        self.assertFalse((completion / 'run-verify.json').exists())
        registry_before = registry.read_bytes()
        raw_before = raw.read_bytes()
        receipt_before = receipt.read_bytes()
        start_args = SimpleNamespace(route_file=str(path), route_node='run-verify',
            attempt_id='att-saved-verifier', jobs=str(jobs), agent_home=ROOT,
            worktree=str(self.base), dispatch_depth=2, parent_slug='fixture-owner',
            parent_attempt_id=owner_attempt, parent_harness='claude',
            parent_transport='headless', parent_sandbox='workspace-write', subsession_id=None)
        with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
             mock.patch('subprocess.check_output', return_value=str(self.base)):
            result = RA.completion_gate(start_args, 'start', ROOT, jobs,
                                        gate=DC.completion_marker_gate)
        self.assertIsNone(result, result)
        verification = output / 'saved-output-verification.json'
        verifier_code = (
            "import hashlib,json,sys; from pathlib import Path; "
            "raw=Path(sys.argv[1]).read_bytes(); receipt=json.loads(Path(sys.argv[2]).read_text()); "
            "expected=receipt['outputs'][0]['sha256']; actual=hashlib.sha256(raw).hexdigest(); "
            "assert actual==expected; Path(sys.argv[3]).write_text(json.dumps({"
            "'verdict':'PASS','consumed_sha256':actual}))")
        verified = subprocess.run([sys.executable, '-B', '-c', verifier_code,
            str(raw), str(receipt), str(verification)], cwd=self.base,
            text=True, capture_output=True, check=False)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verification.read_text())['consumed_sha256'],
                         hashlib.sha256(raw_before).hexdigest())
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'FAILED_RETRYABLE')
        self.assertEqual(ledger.state()['nodes']['full-run']['evidence']['exit_code'], 1)
        self.assertFalse((completion / 'full-run.json').exists())
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(registry.read_bytes(), registry_before)
        self.assertEqual(raw.read_bytes(), raw_before)
        self.assertEqual(receipt.read_bytes(), receipt_before)
        journal_before = ledger.journal_path.read_bytes()
        with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
             mock.patch('subprocess.check_output', return_value=str(self.base)):
            preview = RA.completion_gate(start_args, 'dry-run', ROOT, jobs,
                                          gate=DC.completion_marker_gate)
        self.assertIsNone(preview, preview)
        self.assertEqual(ledger.journal_path.read_bytes(), journal_before)
        self.assertEqual(ledger.claims(), {})
        self.assertFalse((completion / 'full-run.json').exists())

    def test_saved_output_start_refuses_changed_bytes_config_and_pending_correction(self):
        for mutation in ('changed-output', 'changed-config', 'missing-output',
                         'cancelled', 'pending-correction'):
            with self.subTest(mutation=mutation), self.subfixture():
                route, path, jobs, registry, _output, ledger, owner_attempt, owner_input, raw, receipt = \
                    self.failed_saved_output_fixture()
                if mutation == 'changed-output':
                    raw.write_bytes(raw.read_bytes() + b'changed')
                elif mutation == 'changed-config':
                    data = json.loads(receipt.read_text())
                    data['config_sha256'] = 'b' * 64
                    receipt.write_text(json.dumps(data))
                elif mutation == 'missing-output':
                    raw.unlink()
                elif mutation == 'cancelled':
                    data = json.loads(registry.read_text())
                    data['runs']['saved-output-run']['cancel_requested'] = True
                    registry.write_text(json.dumps(data))
                else:
                    import dispatch_owner_input as INPUT
                    with mock.patch.object(RA, 'correction_source_session',
                                           return_value='fixture-user-session'):
                        INPUT.submit(jobs, owner_attempt, 'preserve the new correction',
                                     request_id='saved-output-correction')
                start_args = SimpleNamespace(route_file=str(path), route_node='run-verify',
                    attempt_id='att-saved-verifier', jobs=str(jobs), agent_home=ROOT,
                    worktree=str(self.base), dispatch_depth=2, parent_slug='fixture-owner',
                    parent_attempt_id=owner_attempt, parent_harness='claude',
                    parent_transport='headless', parent_sandbox='workspace-write', subsession_id=None)
                with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                     mock.patch('subprocess.check_output', return_value=str(self.base)):
                    refused = RA.completion_gate(start_args, 'start', ROOT, jobs,
                                                 gate=DC.completion_marker_gate)
                self.assertIsNotNone(refused)
                self.assertEqual(refused[0], 'completion-marker-missing')
                self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'FAILED_RETRYABLE')
                self.assertEqual(ledger.claims(), {})
                self.assertFalse((SUP.route_module().completion_dir(route['route_id'], jobs=jobs)
                                  / 'full-run.json').exists())

    def test_saved_outputs_reach_first_verifier_then_existing_independent_pass_settlement(self):
        import resource_failure_resolution as RESOLUTION

        route, path, jobs, registry, output, ledger, owner_attempt, owner_input, raw, receipt = \
            self.failed_saved_output_fixture(multistage=True)
        raw_before = raw.read_bytes()
        receipt_before = receipt.read_bytes()
        completion = SUP.route_module().completion_dir(route['route_id'], jobs=jobs)
        start_args = SimpleNamespace(route_file=str(path), route_node='metrics',
            attempt_id='att-saved-metrics', jobs=str(jobs), agent_home=ROOT,
            worktree=str(self.base), dispatch_depth=2, parent_slug='fixture-owner',
            parent_attempt_id=owner_attempt, parent_harness='claude',
            parent_transport='headless', parent_sandbox='workspace-write', subsession_id=None)
        with mock.patch('review_input.preview_request_nodes', return_value=frozenset()):
            admitted = RA.completion_gate(start_args, 'start', ROOT, jobs,
                                           gate=DC.completion_marker_gate)
        self.assertIsNone(admitted, admitted)
        self.assertFalse((completion / 'full-run.json').exists())
        self.assertFalse((completion / 'metrics.json').exists())
        self.assertFalse((completion / 'verify.json').exists())

        metrics_path = output / 'metrics.jsonl'
        summary_path = output / 'summary.json'
        correction_path = output / 'correction.json'
        metrics_code = (
            "import hashlib,json,sys; from pathlib import Path; "
            "raw=Path(sys.argv[1]); receipt=Path(sys.argv[2]); metrics=Path(sys.argv[3]); "
            "summary=Path(sys.argv[4]); correction=Path(sys.argv[5]); route_id=sys.argv[6]; "
            "data=raw.read_bytes(); run=receipt.read_bytes(); "
            "record=json.loads(run); actual=hashlib.sha256(data).hexdigest(); "
            "assert record['outputs'][0]['sha256']==actual; "
            "metrics.write_text(json.dumps({'evidence_path':str(raw),'input_sha256':actual})+'\\n'); "
            "summary.write_text(json.dumps({'run_sha256':hashlib.sha256(run).hexdigest(),"
            "'metrics_sha256':hashlib.sha256(metrics.read_bytes()).hexdigest()})); "
            "correction.write_text(json.dumps({'route_id':route_id,'node':'full-run',"
            "'gate_evidence_verdict':'PASS','artifact_sha256':{'raw.json':actual}}))"
        )
        metrics_run = subprocess.run([sys.executable, '-B', '-c', metrics_code,
            str(raw), str(receipt), str(metrics_path), str(summary_path),
            str(correction_path), route['route_id']], cwd=self.base,
            text=True, capture_output=True, check=False)
        self.assertEqual(metrics_run.returncode, 0, metrics_run.stderr)
        self.assertFalse((completion / 'full-run.json').exists(),
                         'first verifier output must not mint a success marker')

        verdict_path = output / 'reviews/verdict.json'
        verdict_path.parent.mkdir()
        transcript = output / 'reviews/independent-review.txt'
        verify_code = (
            "import hashlib,json,sys; from pathlib import Path; "
            "raw=Path(sys.argv[1]).read_bytes(); receipt=Path(sys.argv[2]).read_bytes(); "
            "summary_path=Path(sys.argv[3]); summary=json.loads(summary_path.read_text()); "
            "metrics=Path(sys.argv[4]).read_text(); "
            "expected=json.loads(metrics)['input_sha256']; actual=hashlib.sha256(raw).hexdigest(); "
            "assert expected==actual; assert summary['run_sha256']==hashlib.sha256(receipt).hexdigest(); "
            "Path(sys.argv[5]).write_text(json.dumps({'verdict':'PASS',"
            "'summary':{'path':str(summary_path),"
            "'sha256':hashlib.sha256(summary_path.read_bytes()).hexdigest()}})); "
            "Path(sys.argv[6]).write_text('CPU verifier consumed exact saved output '+actual+'\\n')"
        )
        verify_run = subprocess.run([sys.executable, '-B', '-c', verify_code,
            str(raw), str(receipt), str(summary_path), str(metrics_path), str(verdict_path),
            str(transcript)], cwd=self.base, text=True, capture_output=True, check=False)
        self.assertEqual(verify_run.returncode, 0, verify_run.stderr)
        self.assertEqual(json.loads(verdict_path.read_text())['verdict'], 'PASS')
        self.assertFalse((completion / 'full-run.json').exists())

        module = SUP.route_module()
        module.complete_node(route, WS.route_node(route, 'full-run'), 'full-run',
                             correction_path, jobs=None)
        inline_axes = {'attempt_schema_version': 2, 'dispatch_depth': 2,
            'transport': 'interactive', 'execution_surface': 'inline',
            'registered_worker': False, 'fallback_hop': 'inline'}
        module.complete_node(route, WS.route_node(route, 'metrics'), 'metrics',
            summary_path, jobs=None, attempt_id='att-saved-metrics',
            explicit_attempt_metadata=inline_axes)
        module.complete_node(route, WS.route_node(route, 'verify'), 'verify',
            verdict_path, jobs=None, attempt_id='att-saved-verify',
            explicit_attempt_metadata=inline_axes,
            review_claim={'kind': 'native-subagent', 'transcript': str(transcript)})
        for node_id in ('full-run', 'metrics', 'verify'):
            node = WS.route_node(route, node_id)
            marker_path = completion / f'{node_id}.json'
            marker_data = json.loads(marker_path.read_text())
            currency = module.evidence_currency(route, node, marker_path, marker_data, jobs=jobs)
            self.assertEqual(currency.state, 'current', f'{node_id}: {currency}')
            if node.get('kind') != 'resource-runner':
                gate_currency = DC.gate_currency(route, node, marker_path, marker_data, jobs=jobs)
                self.assertEqual(gate_currency.state, 'current', f'{node_id}: {gate_currency}')
            proof = module._marker_identity_row(route, node, node_id,
                node.get('terminal_gate') if node.get('terminal') else node.get('completion_gate'),
                jobs=jobs, exact_terminal=node.get('kind') != 'resource-runner')
            self.assertTrue(proof.get('passed'), f'{node_id}: {proof}')
        gates = module.terminal_gate_observation(route, jobs=jobs)
        self.assertIs(gates['verify']['passed'], True, gates)
        journal_before = ledger.journal_path.read_bytes()
        resolved = RESOLUTION.reconcile(route, ledger, gates, jobs, module)
        self.assertEqual(resolved, ['full-run'])
        state = ledger.state()
        self.assertEqual(state['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        proof = state['nodes']['full-run']['evidence']['resolved_resource_failure']
        self.assertEqual(proof['original_failure']['exit_code'], 1)
        self.assertEqual(proof['independent_nodes'], ['verify'])
        self.assertEqual(proof['consumed_artifacts'][0]['path'], str(raw.resolve()))
        self.assertEqual(raw.read_bytes(), raw_before)
        self.assertEqual(receipt.read_bytes(), receipt_before)
        journal_after = ledger.journal_path.read_bytes()
        self.assertTrue(journal_after.startswith(journal_before))
        self.assertEqual(RESOLUTION.reconcile(route, ledger, gates, jobs, module), ['full-run'])
        self.assertEqual(ledger.journal_path.read_bytes(), journal_after)

    def live_owner(self, route, jobs, registry, *, harness='codex'):
        import dispatch_owner_input as INPUT
        identity = SUP.RR.proc_identity(os.getpid())
        resource_snapshot = json.loads(registry.read_text())
        resource_route_file = resource_snapshot['runs']['fixture-run']['route']
        attempt = 'att-owner'
        lease = DC.supervisor_lease_path(jobs, attempt)
        nonce = 'd' * 64
        metadata = {
            'attempt_schema_version': '2', 'dispatch_depth': '1',
            'transport': 'headless', 'execution_surface': 'registered-headless',
            'registered_worker': '1', 'fallback_hop': 'same-harness-headless',
            'worker_type': 'owner', 'unit': '_kernel/owner', 'harness': harness,
            'runtime_sandbox': 'workspace-write', 'completion_delivery':
                'app-server-supervised' if harness == 'codex' else 'session-resume-supervised',
            'supervisor_lease': DC.SUPERVISOR_LEASE_KIND,
            'supervisor_lease_file': str(lease), 'supervisor_lease_nonce': nonce,
            'attempt_id': attempt, 'pid': str(identity['pid']), 'pid_start': identity['starttime'],
            'owner_route_file': str(Path(resource_route_file).resolve()),
            'artifact_root': str(Path(route.get('artifact_root', self.base)).resolve()),
            'owner_route_id': route['route_id'], 'owner_route_hash': route['route_hash'],
            'route_id': route['route_id'], 'route_hash': route['route_hash'],
            'parent_sid': 'fixture-parent-session',
            'capability': route['capability'], 'capability_mode': route['capability_mode'],
            'intensity': route['effective_intensity'],
        }
        row = '\t'.join(['2026-10-20T00:00:00Z', 'open', str(self.base), str(self.base),
                         'fixture-owner', ','.join(f'{key}={value}' for key, value in metadata.items())])
        with Path(jobs).open('a', encoding='utf-8') as stream:
            stream.write(row + '\n')
        lease_context = DC.hold_supervisor_lease(jobs, attempt, lease)
        lease_context.__enter__()
        self.addCleanup(lease_context.__exit__, None, None, None)
        transport = 'codex-active-turn' if harness == 'codex' else f'{harness}-session-resume'
        owner_input = INPUT.OwnerInput(jobs, attempt, 'same-native', transport, lambda _: None)
        self.addCleanup(owner_input.close)
        resource_data = json.loads(registry.read_text())
        armed_path = SUP.ledger_for(route, jobs).root / 'armed/full-run.json'
        armed = json.loads(armed_path.read_text())
        resource_row = resource_data['runs'][armed['predecessor_id']]
        resource_row['parent_attempt_id'] = attempt
        resource_row['owner_wait'].update(
            parent_attempt_id=attempt, session_id='same-native', route_id=route['route_id'],
            route_hash=route['route_hash'], jobs=str(Path(jobs).resolve()),
            owner_pid=str(identity['pid']), owner_start=identity['starttime'],
        )
        registry.write_text(json.dumps(resource_data))
        armed['resource_binding'] = WAIT.resource_body_digest(resource_row)
        armed_path.write_text(json.dumps(armed))
        return attempt, owner_input

    def test_compute_adopted_row_passes_normal_owner_successor_admission_once(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(owner_harness=harness), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                (output / 'run.json').write_bytes(b'{"state":"compute-result"}\n')
                attempt, _owner_input = self.live_owner(route, jobs, registry, harness=harness)
                data = json.loads(registry.read_text())
                row = data['runs']['fixture-run']
                row.update(status='succeeded', workflow_state='STAGE_SUCCEEDED', exit_code=0)
                compute_dir = self.base / 'compute' / 'compute-fixture'
                compute_dir.mkdir(parents=True)
                (compute_dir / 'log').write_text('remote fixture complete\n')
                (compute_dir / 'exit_code').write_text('0\n')
                row.update(compute_run_id='compute-fixture', log=str(compute_dir / 'log'),
                           sentinel=str(compute_dir / 'exit_code'))
                row['execution'] = {
                    'kind': 'compute-hosts', 'run_id': 'compute-fixture',
                    'run_root': str(self.base / 'compute'), 'meta_sha256': 'm' * 64,
                    'exit_sha256': 'e' * 64, 'receipt_sha256': 'r' * 64,
                    'outputs_sha256': 'o' * 64,
                    'contract_sha256': 'c' * 64,
                    'provenance': {'route_id': route['route_id'], 'route_hash': route['route_hash']},
                }
                compute_identity = SUP.RR.compute_execution_identity(row)
                registry.write_text(json.dumps(data))
                armed = SUP.read_armed(ledger)['full-run']
                armed['resource_binding'] = WAIT.resource_body_digest(row)
                armed_path = ledger.root / 'armed/full-run.json'
                armed_path.write_text(json.dumps(armed))
                args = SimpleNamespace(
                    route_file=str(path), route_node='run-verify', attempt_id='att-verify',
                    jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
                    parent_slug='fixture-owner', parent_attempt_id=attempt,
                    parent_harness=harness, parent_transport='headless',
                    parent_sandbox='workspace-write', subsession_id=None,
                )
                marker = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
                observer_calls = []
                def observe_outside_writer_locks(_row):
                    observer_calls.append(True)
                    for lock_path in (ledger.lock_path, Path(str(registry) + '.lock')):
                        with lock_path.open('a+') as lock_file:
                            try:
                                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                self.fail(f'compute observation ran while writer lock held: {lock_path}')
                            finally:
                                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    return {'state': 'quiescent', 'reason': 'fixture-remote-empty'}
                with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                     mock.patch('subprocess.check_output', return_value=str(self.base)), \
                     mock.patch.object(SUP.RR, 'compute_execution_observation',
                                       side_effect=observe_outside_writer_locks), \
                     mock.patch.object(
                         SUP, 'reconcile_legacy_supervised_resource_success',
                         wraps=SUP.reconcile_legacy_supervised_resource_success,
                     ) as reconcile:
                    self.assertIsNone(RA.completion_gate(
                        args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                    ))
                    marker_before = marker.read_bytes()
                    journal_before = ledger.journal_path.read_bytes()
                    claims_before = ledger.claims()
                    claim_history_before = ledger.claim_history()
                    self.assertIsNone(RA.completion_gate(
                        args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                    ))
                    with mock.patch('route_parent_close.ledger_intent', return_value=None), \
                         mock.patch('artifact_checkpoint_trigger.launch_for_route'):
                        polled = SUP.poll_once(route, ledger)
                self.assertEqual(len(observer_calls), 1)
                self.assertEqual(reconcile.call_count, 0)
                self.assertEqual(polled, [{'node': 'full-run', 'action': 'settled',
                                           'state': 'STAGE_SUCCEEDED'}])
                self.assertTrue(marker.is_file())
                self.assertEqual(marker.read_bytes(), marker_before)
                self.assertEqual(json.loads(marker.read_bytes())['resource_execution_identity'],
                                 compute_identity)
                self.assertEqual(ledger.journal_path.read_bytes(), journal_before)
                self.assertEqual(ledger.claims(), claims_before)
                self.assertEqual(ledger.claim_history(), claim_history_before)
                self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
                self.assertEqual(len(ledger.claims()), 1)
                self.assertEqual(len(ledger.claim_history()), 1)

    def test_changed_captured_owner_settlement_refuses_without_reobserve_or_claim(self):
        with self.subfixture():
            route, path, jobs, registry, output, ledger = self.fixture()
            (output / 'run.json').write_bytes(b'{"state":"compute-result"}\n')
            attempt, _owner_input = self.live_owner(route, jobs, registry, harness='codex')
            data = json.loads(registry.read_text())
            row = data['runs']['fixture-run']
            row.update(status='succeeded', workflow_state='STAGE_SUCCEEDED', exit_code=0)
            compute_dir = self.base / 'compute' / 'compute-fixture'
            compute_dir.mkdir(parents=True)
            (compute_dir / 'log').write_text('remote fixture complete\n')
            (compute_dir / 'exit_code').write_text('0\n')
            row.update(compute_run_id='compute-fixture', log=str(compute_dir / 'log'),
                       sentinel=str(compute_dir / 'exit_code'))
            row['execution'] = {
                'kind': 'compute-hosts', 'run_id': 'compute-fixture',
                'run_root': str(self.base / 'compute'), 'meta_sha256': 'm' * 64,
                'exit_sha256': 'e' * 64, 'receipt_sha256': 'r' * 64,
                'outputs_sha256': 'o' * 64, 'contract_sha256': 'c' * 64,
                'provenance': {'route_id': route['route_id'], 'route_hash': route['route_hash']},
            }
            registry.write_text(json.dumps(data))
            armed_path = ledger.root / 'armed/full-run.json'
            armed = SUP.read_armed(ledger)['full-run']
            armed['resource_binding'] = WAIT.resource_body_digest(row)
            armed_path.write_text(json.dumps(armed))
            args = SimpleNamespace(
                route_file=str(path), route_node='run-verify', attempt_id='att-verify',
                jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
                parent_slug='fixture-owner', parent_attempt_id=attempt,
                parent_harness='codex', parent_transport='headless',
                parent_sandbox='workspace-write', subsession_id=None,
            )
            marker = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
            observer_calls = []

            def observe_outside_writer_locks(_row):
                observer_calls.append(True)
                for lock_path in (ledger.lock_path, Path(str(registry) + '.lock')):
                    with lock_path.open('a+') as lock_file:
                        try:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            self.fail(f'compute observation ran while writer lock held: {lock_path}')
                        finally:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                return {'state': 'quiescent', 'reason': 'fixture-remote-empty'}

            with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                 mock.patch('subprocess.check_output', return_value=str(self.base)), \
                 mock.patch.object(SUP.RR, 'compute_execution_observation',
                                   side_effect=observe_outside_writer_locks):
                self.assertIsNone(RA.completion_gate(
                    args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                ))
                marker_before = marker.read_bytes()
                journal_before = ledger.journal_path.read_bytes()
                claims_before = ledger.claims()
                owner_supervisor = WAIT.supervisor()
                real_snapshot = owner_supervisor._owner_settlement_snapshot
                mutated = False

                def mutate_after_settlement_capture(*snapshot_args):
                    nonlocal mutated
                    snapshot = real_snapshot(*snapshot_args)
                    if snapshot is not None and not mutated:
                        changed = json.loads(registry.read_text())
                        changed['runs']['fixture-run']['owner_wait']['session_id'] = 'changed-session'
                        registry.write_text(json.dumps(changed))
                        mutated = True
                    return snapshot

                with mock.patch.object(owner_supervisor, '_owner_settlement_snapshot',
                                       side_effect=mutate_after_settlement_capture):
                    refused = RA.completion_gate(
                        args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                    )
            self.assertTrue(mutated)
            self.assertIsNotNone(refused)
            self.assertEqual(refused[0], 'resource-owner-stage-admission-blocked')
            self.assertEqual(observer_calls, [True])
            self.assertEqual(marker.read_bytes(), marker_before)
            self.assertEqual(ledger.journal_path.read_bytes(), journal_before)
            self.assertEqual(ledger.claims(), claims_before)

    def test_compute_run_start_then_normal_owner_run_verify_start(self):
        import artifact_producer
        import resource_run_registry as RR
        spec = importlib.util.spec_from_file_location('resource_runner_e2e', HERE / 'resource-runner.py')
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, _owner_input = self.live_owner(route, jobs, registry, harness='codex')
        (output / 'run.json').write_bytes(b'{"state":"compute-result"}\n')
        (output / 'logs').mkdir()
        (output / 'logs/nested').mkdir()
        (output / 'logs/train.log').write_text('train completed\n')
        (output / 'logs/nested/metrics.json').write_text('{"loss":0.1}\n')
        declared_outputs = list(WS.route_node(route, 'full-run').get('outputs') or [])
        self.assertEqual(declared_outputs, ['run.json', 'logs/**', 'experiment-artifact'])
        attestation = self.base / 'smoke-attestation.json'
        attestation.write_text('{"fixture":true}\n')
        attestation_hash = __import__('hashlib').sha256(attestation.read_bytes()).hexdigest()
        old = json.loads(registry.read_text())['runs']['fixture-run']
        command = ['compute-hosts', 'run', 'gpu-a', '--cwd', '/lab', '--env', 'env-a', '--',
                   'python', 'train.py']
        launch_request = {'smoke_attestation': str(attestation),
                          'smoke_attestation_sha256': attestation_hash,
                          'config_manifest': None, 'config_manifest_sha256': None}
        old.update(status='failed', workflow_state='FAILED_RETRYABLE', failure_class='payload-exit',
                   exit_code=3, command=command, launch_request=launch_request,
                   config_ref='fixture-config', config_sha256='c' * 64,
                   source_commit='fixture-commit', source_dirty=False,
                   source_git_state='clean', config_layout='fixture-layout')
        Path(old['sentinel']).write_text('3\n')
        registry.write_text(json.dumps({'schema_version': 1, 'runs': {'fixture-run': old}}))
        armed = SUP.read_armed(ledger)['full-run']
        armed['resource_binding'] = WAIT.resource_body_digest(old)
        (ledger.root / 'armed/full-run.json').write_text(json.dumps(armed))

        compute_root = self.base / 'compute-runs'
        compute_dir = compute_root / 'compute-fixture'
        compute_dir.mkdir(parents=True)
        (compute_dir / 'log').write_text('remote compute completed\n')
        (compute_dir / 'exit_code').write_text('0\n')
        sealed = {key: old.get(key) for key in ('command', 'cwd', 'config_ref', 'config_sha256',
                 'source_commit', 'source_dirty', 'source_git_state', 'config_layout', 'launch_request')}
        parent = {'route_id': route['route_id'], 'route_file': str(path.resolve()),
                  'route_hash': route['route_hash'], 'node': 'full-run', 'run_id': 'fixture-run',
                  'attempt_id': owner_attempt, 'sealed': sealed, 'artifact_base': str(output),
                  'declared_outputs': declared_outputs,
                  'inputs': [{'name': 'smoke_attestation', 'path': str(attestation),
                              'sha256': attestation_hash, 'verified': True}]}
        provenance = {'run_id': 'fixture-run', 'registry': str(registry.resolve()),
                      'route_id': route['route_id'], 'route_file': str(path.resolve()),
                      'route_hash': route['route_hash'], 'node': 'full-run',
                      'parent_attempt_id': owner_attempt,
                      'contract': parent}
        execution_contract = {'command': ['python', 'train.py'], 'cwd': '/lab', 'env': 'env-a',
            'share': False, 'resource_parent': parent,
            'route': {'route_id': route['route_id'], 'route_file': str(path.resolve()),
                      'route_hash': route['route_hash']}}
        compute_meta = {'run_id': 'compute-fixture', 'host': 'gpu-a',
            'provenance': {'resource': provenance}, 'execution_contract': execution_contract,
            'contract_sha256': __import__('hashlib').sha256(json.dumps(
                execution_contract, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode('utf-8')).hexdigest()}
        (compute_dir / 'meta.json').write_text(json.dumps(compute_meta), encoding='utf-8')
        (compute_dir / 'launch-receipt.json').write_text(json.dumps({
            'schema_version': 1, 'run_id': 'compute-fixture', 'host': 'fixture-host',
            'identity': {'pid': 4, 'starttime': '4', 'process_group': 4,
                         'pid_namespace': 'pid:[77]', 'boot_id': 'boot-fixture'},
            'state': 'finished', 'exit_code': 0, 'started_at': 1.0, 'ended_at': 2.0}),
            encoding='utf-8')
        compute_spec_real = importlib.util.spec_from_file_location(
            'compute_hosts_sequence_e2e', HERE / 'compute-hosts.py')
        compute = importlib.util.module_from_spec(compute_spec_real)
        compute_spec_real.loader.exec_module(compute)
        compute.remote = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, '{"state":"empty","reason":"fixture-empty-group"}\n', '')
        compute._record_compute_outputs(compute_dir, compute_meta)
        compute_config_file = self.base / 'compute-hosts.yaml'
        compute_config_file.write_text(
            'schema_version: 1\n'
            f'run_root: {compute_root}\n'
            'hosts:\n  gpu-a:\n    ssh_host: fixture.invalid\n', encoding='utf-8')
        candidate = compute.execution_record(compute.load_config(compute_config_file), 'compute-fixture')
        self.assertEqual(candidate['state'], 'quiescent', candidate)
        execution = candidate['execution']
        spec_loader = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _module: None))
        real_spec = importlib.util.spec_from_file_location
        real_module = importlib.util.module_from_spec
        def load_spec(name, location, *args, **kwargs):
            if name == 'resource_runner_compute_hosts':
                return spec_loader
            return real_spec(name, location, *args, **kwargs)
        def load_module(specification):
            if specification is spec_loader:
                return compute
            return real_module(specification)
        owner_wait = old['owner_wait']
        owner_wait.update(parent_attempt_id=owner_attempt, session_id='same-native',
                          route_id=route['route_id'], route_hash=route['route_hash'], jobs=str(jobs))
        args = SimpleNamespace(jobs=str(jobs), parent_attempt_id=owner_attempt)
        arm_attempts = 0
        def arm_command(argv, **_kwargs):
            nonlocal arm_attempts
            if len(argv) > 1 and argv[1] == str(HERE / 'workflow-supervisor.py'):
                arm_attempts += 1
                if arm_attempts == 1:
                    raise subprocess.TimeoutExpired(argv, 30)
                self.assertEqual(SUP.main(argv[2:]), 0)
                return subprocess.CompletedProcess(argv, 0)
            raise AssertionError(f'unexpected child process from adoption: {argv!r}')
        with mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
             mock.patch.object(WAIT, 'start_binding', return_value=owner_wait), \
             mock.patch.object(RR, 'classify_identity', return_value=('exited', None, 'process-absent')), \
             mock.patch.object(RR, 'resource_process_group_quiescent', return_value=True), \
             mock.patch('importlib.util.spec_from_file_location', side_effect=load_spec), \
             mock.patch('importlib.util.module_from_spec', side_effect=load_module), \
             mock.patch.object(runner, 'verify_launch_input'), \
             mock.patch.object(runner.subprocess, 'run', side_effect=arm_command), \
             mock.patch.object(runner.subprocess, 'Popen') as spawned, \
             mock.patch.object(artifact_producer, 'prepare_route_artifact_env', return_value={
                 'AGENT_ARTIFACT_OUTPUT_DIR': str(output)}), \
             mock.patch.dict(os.environ, {'AGENT_DISPATCH_JOBS': str(jobs),
                 'AGENT_DISPATCH_COMPLETION_MODE': 'supervised',
                 'COMPUTE_HOSTS_CONFIG': str(compute_config_file)}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as before_arm:
                runner.adopt_compute_run(registry, args, route, path, 'compute-fixture')
            self.assertEqual(before_arm.exception.code, 65)
            self.assertEqual(set(json.loads(registry.read_text())['runs']), {'fixture-run', 'fixture-run__a1'})

            output_receipt_path = compute_dir / 'outputs.json'
            original_output_receipt = output_receipt_path.read_bytes()
            changed_receipt = json.loads(original_output_receipt)
            changed_receipt['outputs'].append({'path': 'logs/injected.log', 'sha256': '0' * 64,
                                               'size': 1, 'state': 'present'})
            output_receipt_path.write_text(json.dumps(changed_receipt, ensure_ascii=False,
                                                       sort_keys=True) + '\n', encoding='utf-8')
            marker = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
            def protected_snapshot():
                paths = [registry, Path(old['sentinel']), jobs, ledger.journal_path, marker]
                files = {str(item): item.read_bytes() if item.is_file() else None for item in paths}
                claims = {item.name: item.read_bytes() for item in ledger.claims_dir.glob('*.claim')}
                return files, claims
            before_refusal = protected_snapshot()
            with self.assertRaises(SystemExit) as changed_receipt_refusal:
                runner.adopt_compute_run(registry, args, route, path, 'compute-fixture')
            self.assertEqual(changed_receipt_refusal.exception.code, 65)
            refusal = json.loads(changed_receipt_refusal.exception.resource_message)
            self.assertEqual(refusal['reason'], 'resource-compute-output-mismatch')
            self.assertTrue(refusal['differences'][0].get('expected') is not None)
            self.assertTrue(refusal['differences'][0].get('actual') is not None)
            self.assertTrue(refusal['next_action'])
            self.assertFalse(refusal['retryable'])
            self.assertEqual(protected_snapshot(), before_refusal)
            self.assertEqual(set(json.loads(registry.read_text())['runs']), {'fixture-run', 'fixture-run__a1'})
            output_receipt_path.write_bytes(original_output_receipt)

            replay_before_arm = runner.adopt_compute_run(registry, args, route, path, 'compute-fixture')
            self.assertTrue(replay_before_arm['replayed'])
            self.assertEqual(spawned.call_count, 0)
            replay_after_arm = runner.adopt_compute_run(registry, args, route, path, 'compute-fixture')
            self.assertTrue(replay_after_arm['replayed'])
            self.assertEqual(arm_attempts, 2)
            self.assertEqual(spawned.call_count, 0)
            owner_args = SimpleNamespace(route_file=str(path), route_node='run-verify',
                attempt_id='att-verify', jobs=str(jobs), agent_home=ROOT, worktree=str(self.base),
                dispatch_depth=2, parent_slug='fixture-owner', parent_attempt_id=owner_attempt,
                parent_harness='codex', parent_transport='headless', parent_sandbox='workspace-write',
                subsession_id=None)
            armed_outputs = SUP.read_armed(ledger)['full-run']
            import resource_run_evidence as RUN_EVIDENCE
            self.assertEqual(RUN_EVIDENCE.outputs(armed_outputs), ['run.json'])
            self.assertEqual(SUP.artifact_evidence(armed_outputs), {
                'checked': True, 'missing': [], 'reason': 'artifacts-present'})
            with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                 mock.patch('subprocess.check_output', return_value=str(self.base)), \
                 mock.patch.object(DC, 'canonical_repository_identity', return_value='fixture-repository'), \
                 mock.patch.object(RR, 'compute_execution_observation', return_value={
                     'state': 'quiescent', 'reason': 'fixture-remote-empty'}):
                self.assertIsNone(RA.completion_gate(
                    owner_args, 'start', ROOT, jobs, gate=DC.completion_marker_gate))
        rows = json.loads(registry.read_text())['runs']
        adopted = rows['fixture-run__a1']
        self.assertEqual(adopted['execution']['run_id'], 'compute-fixture')
        marker = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
        self.assertEqual(json.loads(marker.read_bytes())['resource_execution_identity'],
                         RR.compute_execution_identity(adopted))
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual(len(ledger.claim_history()), 1)

    def next_body(self, registry, name, output, *, final=False):
        old = json.loads(registry.read_text())['runs']['fixture-run']
        keep = ('cwd', 'route', 'node', 'jobs', 'parent_attempt_id', 'owner_wait', 'config_ref',
                'config_sha256', 'source_commit', 'source_dirty', 'source_git_state', 'config_layout')
        log = self.base / (name + '.log')
        code = (f'from pathlib import Path; Path({str(output / "run.json")!r}).write_text({json.dumps({"phase":"final"})!r})'
                if final else 'pass')
        return {**{key: old[key] for key in keep if key in old}, 'run_id': name,
                'command': [sys.executable, '-c', code], 'log': str(log), 'sentinel': str(log) + '.exit',
                'status': 'launching', 'workflow_state': 'READY', 'resource_policy': 'supervised-owner',
                'launch_request': dict(self.EMPTY_LAUNCH_REQUEST)}

    def authorized_body(self, route, path, jobs, registry, owner_attempt, owner_input,
                        name, output, *, scope="independent approved resource operation",
                        payload_code="import time; time.sleep(20)", payload_args=(),
                        provenance=None, approved_text=None, require_binding=True):
        import dispatch_owner_input as INPUT
        body = self.next_body(registry, name, output)
        body.update(parent_attempt_id=owner_attempt)
        request_path = self.base / f'{name}-request.json'
        admission_dir = self.base / f'{name}-admission'
        admission_dir.mkdir(parents=True, exist_ok=True)
        command = ['env', 'HEARTING_TEST_PAYLOAD=approved', sys.executable, '-c', payload_code,
                   *map(str, payload_args), '--cwd', str(Path(body['cwd']).resolve()),
                   str(request_path)]
        request = {
            'user_direction_id': f'input-{name}', 'parent_attempt_id': owner_attempt,
            'run_id': name, 'requested_scope': scope,
            'compute_argv': command,
            'config_manifest': None, 'smoke_attestation': None,
            'Dev_admission_evidence': str(admission_dir / 'admission-evidence.json'),
            'provenance': provenance or {},
        }
        request_bytes = json.dumps(request, sort_keys=True).encode()
        request_path.write_bytes(request_bytes)
        body['command'] = command
        argv = ['python3', str(HERE / 'resource-runner.py'),
            '--registry', str(Path(registry).resolve()), 'start',
            '--run-id', name, '--cwd', str(Path(body['cwd']).resolve()),
            '--log', str(Path(body['log']).resolve()), '--route', str(Path(path).resolve()),
            '--node', body['node'], '--parent-attempt-id', owner_attempt,
            '--jobs', str(Path(jobs).resolve()), '--', *command]
        (admission_dir / f'{name}-resource-start-command.json').write_text(json.dumps({
            'argv': argv, 'request_path': str(request_path),
            'request_sha256': hashlib.sha256(request_bytes).hexdigest()}, sort_keys=True))
        with mock.patch.object(RA, 'correction_source_session', return_value='fixture-user-session'):
            INPUT.submit(jobs, owner_attempt,
                         request_bytes.decode() if approved_text is None else approved_text,
                         request_id=f'input-{name}')
            owner_input.prepare('continue the approved work')
            turn = f'turn-{name}'
            owner_input.started(turn)
            owner_input.completed(turn)
        operation = SUP.runner().resource_operation_binding(
            route, path, body['node'], name, command, body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log'])
        if require_binding:
            self.assertIsNotNone(operation)
        if operation is not None:
            body['resource_operation'] = operation
        return body

    def authorized_bridge_body(self, route, path, jobs, registry, owner_attempt,
                               owner_input, name, output, *, approved_text=None,
                               payload_path=None, payload_result='cpu-bridge-payload-complete\n',
                               artifact_output_path=None, artifact_output_result=None,
                               fixed_resume=False):
        """Build the recorded local-bridge / remote-compute-hosts request shape."""
        import dispatch_owner_input as INPUT
        import shlex

        body = self.next_body(registry, name, output)
        body.update(parent_attempt_id=owner_attempt)
        body['owner_wait']['launch_scope'] = 'codex-owner-controller'
        request_path = self.base / f'{name}-request.json'
        admission_dir = self.base / f'{name}-admission'
        admission_dir.mkdir(parents=True, exist_ok=True)
        bridge_path = self.base / f'{name}-remote-production-bridge-var-rir512.py'
        parser_path = ROOT / 'utilities' / 'compute-hosts.py'
        bridge_path.write_text(
            'import importlib.util, json, subprocess, sys\n'
            f'spec = importlib.util.spec_from_file_location("bridge_compute_hosts", {str(parser_path)!r})\n'
            'module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n'
            'request = json.load(open(sys.argv[1], encoding="utf-8"))\n'
            'run = module.parse_run_invocation(request["compute_argv"])\n'
            'subprocess.run(run.command, cwd=run.cwd, check=True)\n'
        )
        config = self.base / f'{name}-config.yaml'
        data_lock = self.base / f'{name}-data.lock'
        smoke = self.base / f'{name}-smoke.json'
        config.write_text('model: approved\n')
        data_lock.write_text('dataset: approved\n')
        smoke.write_text('{"smoke": "approved"}\n')
        input_paths = {'config_sha256': config, 'data_lock_sha256': data_lock,
                       'gpu_smoke_sha256': smoke}
        provenance = {field: hashlib.sha256(file.read_bytes()).hexdigest()
                      for field, file in input_paths.items()}
        provenance['bridge_sha256'] = hashlib.sha256(bridge_path.read_bytes()).hexdigest()
        target = Path(payload_path or (self.base / f'{name}-payload-result.json')).resolve()
        payload_script = ('from pathlib import Path; '
                          f'Path({str(target)!r}).write_text({payload_result!r})')
        if artifact_output_path is not None:
            artifact_output_path = Path(artifact_output_path).resolve()
            if artifact_output_result is None:
                artifact_output_result = payload_result
            payload_script += (f'; Path({str(artifact_output_path)!r}).write_text('
                               f'{artifact_output_result!r})')
        remote_payload = ['env', 'REMOTE_TRAINING_ENV=approved', sys.executable,
                          '-c', payload_script, '--config', str(config),
                          '--data-lock', str(data_lock), '--smoke', str(smoke)]
        if fixed_resume:
            checkpoint = self.base / f'{name}-last.pt'
            checkpoint.write_bytes(b'fixture checkpoint: epoch 4 / 80000 updates\n')
            checkpoint_hash = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
            remote_payload.extend(['--resume', str(checkpoint)])
            provenance.update(source_bridge_sha256='a' * 64,
                              cache_preflight_sha256='b' * 64,
                              resume_checkpoint_sha256=checkpoint_hash)
        remote_argv = [str(ROOT / 'utilities' / 'compute-hosts'), 'run', 'gpu-fixture',
                       '--cwd', str(self.base.resolve()), '--env', 'fixture-env',
                       '--gpus', '0', '--name', name, '--', 'bash', '-c',
                       shlex.join(remote_payload)]
        command = ['env', 'HEARTING_LOCAL_BRIDGE=approved', sys.executable,
                   str(bridge_path), str(request_path)]
        request = {
            'user_direction_id': f'input-{name}', 'parent_attempt_id': owner_attempt,
            'run_id': name, 'requested_scope': 'run the variable model and preserve the fixed model',
            'compute_argv': remote_argv, 'config_manifest': None, 'smoke_attestation': None,
            'Dev_admission_evidence': str(admission_dir / 'admission-evidence.json'),
            'provenance': provenance,
        }
        if fixed_resume:
            request.pop('Dev_admission_evidence')
            preflight = admission_dir / 'cpu-preflight-receipt.json'
            destination = admission_dir / 'destination-data-evidence.json'
            preflight.write_text('{"cpu_preflight": "passed"}\n')
            destination.write_text('{"destination": "fixture"}\n')
            request.update(preflight_receipt=str(preflight),
                           destination_data_evidence=str(destination),
                           resume_checkpoint=str(checkpoint),
                           resume_checkpoint_sha256=checkpoint_hash,
                           requested_scope='resume the fixed model and preserve the live variable model')
        request_bytes = json.dumps(request, sort_keys=True).encode()
        request_path.write_bytes(request_bytes)
        body['command'] = command
        runner_path = HERE / 'fixtures' / 'resource-v3532' / 'resource-runner.py'
        argv = ['python3', str(runner_path), '--registry', str(Path(registry).resolve()),
            'start', '--run-id', name, '--cwd', str(Path(body['cwd']).resolve()),
            '--log', str(Path(body['log']).resolve()), '--route', str(Path(path).resolve()),
            '--node', body['node'], '--parent-attempt-id', owner_attempt,
            '--jobs', str(Path(jobs).resolve()), '--', *command]
        record_path = admission_dir / f'{name}-resource-start-command.json'
        record_path.write_text(json.dumps({'argv': argv, 'request_path': str(request_path),
            'request_sha256': hashlib.sha256(request_bytes).hexdigest()}, sort_keys=True))
        approved_text = approved_text or (
            'Resume the fixed model from its checkpoint and preserve the live variable model.'
            if fixed_resume else 'Run the variable model while preserving the fixed model.')
        with mock.patch.object(RA, 'correction_source_session', return_value='fixture-user-session'):
            INPUT.submit(jobs, owner_attempt, approved_text, request_id=f'input-{name}')
            owner_input.prepare('continue the approved work')
            turn = f'turn-{name}'
            owner_input.started(turn)
            owner_input.completed(turn)
        operation = SUP.runner().resource_operation_binding(
            route, path, body['node'], name, command, body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log'])
        body['resource_operation'] = operation
        body['launch_state'] = 'queued'
        return body

    def test_fixed_resume_public_start_restores_same_never_started_id_beside_live_legacy_var(self):
        """No Dev locator; restore the original unarmed __a1, not a new run id."""
        import artifact_producer
        import resource_run_registry as RR
        import session_identity

        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        state_path = DC.dispatch_state_root(jobs) / 'supervisor-state' / f'{owner_attempt}.json'
        WAIT.JOIN.write_supervisor_state(state_path, owner_attempt, set(), phase='running-turn')
        environment = dict(os.environ)
        for key in ('AGENT_DISPATCH_WORKER_TYPE', 'AGENT_DISPATCH_DEPTH',
                    'AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION', 'AGENT_DISPATCH_EXECUTION_SURFACE',
                    'AGENT_DISPATCH_REGISTERED_WORKER'):
            environment.pop(key, None)
        environment.update(CUDA_VISIBLE_DEVICES='', AGENT_DISPATCH_COMPLETION_MODE='supervised',
            AGENT_DISPATCH_ATTEMPT_ID=owner_attempt, AGENT_OWNER_ROUTE_FILE=str(path),
            AGENT_DISPATCH_JOBS=str(jobs), AGENT_DISPATCH_COMPLETION_STATE_FILE=str(state_path))
        var = self.authorized_bridge_body(route, path, jobs, registry, owner_attempt,
                                         owner_input, 'legacy-var', output)
        run_id = 'fixed-resume__a1'
        result_path = self.base / 'fixed-resume-result.txt'
        body = self.authorized_bridge_body(route, path, jobs, registry, owner_attempt,
            owner_input, run_id, output, fixed_resume=True, payload_path=result_path)
        request_path = Path(body['command'][-1])
        request = json.loads(request_path.read_text())
        self.assertNotIn('Dev_admission_evidence', request)
        self.assertTrue(Path(request['preflight_receipt']).is_file())
        self.assertTrue(Path(request['destination_data_evidence']).is_file())
        checkpoint = Path(request['resume_checkpoint'])
        checkpoint_bytes = checkpoint.read_bytes()
        self.assertEqual(hashlib.sha256(checkpoint_bytes).hexdigest(),
                         request['resume_checkpoint_sha256'])

        smoke = self.base / 'fixed-public-smoke.json'
        smoke.write_text('{"fixture": true}\n')
        smoke_hash = hashlib.sha256(smoke.read_bytes()).hexdigest()
        request.update(smoke_attestation=str(smoke), smoke_attestation_sha256=smoke_hash)
        request['provenance']['smoke_attestation_sha256'] = smoke_hash
        request_bytes = json.dumps(request, sort_keys=True).encode()
        request_path.write_bytes(request_bytes)
        record_path = Path(request['preflight_receipt']).parent / f'{run_id}-resource-start-command.json'
        command_record = json.loads(record_path.read_text())
        command_record['request_sha256'] = hashlib.sha256(request_bytes).hexdigest()
        divider = command_record['argv'].index('--')
        command_record['argv'][divider:divider] = ['--smoke-attestation', str(smoke)]
        record_path.write_text(json.dumps(command_record, sort_keys=True))
        runner = SUP.runner()
        cli = command_record['argv'][2:]
        binding_args = SimpleNamespace(jobs=str(jobs), parent_attempt_id=owner_attempt)
        with mock.patch.dict(os.environ, environment, clear=True):
            owner_wait = WAIT.start_binding(route, path, binding_args, os.environ)
        body['owner_wait'] = owner_wait
        body['launch_request'] = runner.launch_request(SimpleNamespace(
            smoke_attestation=str(smoke), config_manifest=None))
        operation = runner.resource_operation_binding(route, path, body['node'], run_id,
            body['command'], body['cwd'], body['launch_request'], owner_wait,
            registry_path=registry, log_path=body['log'])
        self.assertIsNotNone(operation, 'the existing preflight/destination record must bind the resume')
        self.assertEqual(operation['payload']['input_hashes']['resume_checkpoint_sha256'],
                         request['resume_checkpoint_sha256'])
        body['resource_operation'] = operation

        # Private live-process stand-in for the protected legacy variable run.
        var_ready = self.base / 'private-var-ready'
        var_process = subprocess.Popen([sys.executable, '-c',
            'import sys,time; from pathlib import Path; Path(sys.argv[1]).write_text("ready"); time.sleep(120)',
            str(var_ready)], start_new_session=True)
        def stop_fixture_var():
            if var_process.poll() is None:
                var_process.terminate()
            var_process.wait(timeout=5)
        self.addCleanup(stop_fixture_var)
        deadline = time.monotonic() + 5
        while not var_ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(var_ready.exists())
        var_identity = RR.proc_identity(var_process.pid)
        self.assertIsNotNone(var_identity)
        var.update(owner_wait=owner_wait, route_id=route['route_id'], status='running', launch_state='started',
                   workflow_state='RUNNING', process_group=var_process.pid,
                   pid_namespace=os.readlink('/proc/self/ns/pid'), **var_identity)
        var.pop('resource_operation', None)
        seed = json.loads((ledger.root / 'armed/full-run.json').read_bytes())
        seed.update(predecessor_id=var['run_id'], resource_binding=WAIT.resource_legacy_body_digest(var))
        seed.pop('resource_current_binding', None)
        seed.pop('resource_operation', None)
        primary = ledger.root / 'armed/full-run.json'
        primary.write_text(json.dumps(seed))
        var_watch_bytes = primary.read_bytes()
        var_snapshot = copy.deepcopy(var)
        failed = {**body, 'status': 'failed', 'workflow_state': 'FAILED_RETRYABLE',
                  'route_id': route['route_id'],
                  'launch_state': 'not-started', 'failure_class': 'resource-launch-incomplete',
                  'ended_at': time.time(), 'launch_error': {'message': 'resource-watch-binding-conflict'}}
        launcher = session_identity.identity(environment)
        failed['session'] = ({'harness': launcher.harness, 'id': launcher.session_id}
                             if launcher.known and launcher.session_id else None)
        failed.pop('resource_operation')
        # The owner's consumer was restored after this failure: same attempt, new PID/start.
        registry_text = jobs.read_text()
        self.assertEqual(registry_text.count(',attempt_id=' + owner_attempt), 1)
        jobs.write_text(registry_text.replace(',attempt_id=' + owner_attempt,
            ',owner_identity_lineage=4000000001:7,attempt_id=' + owner_attempt))
        failed['owner_wait'] = {**owner_wait, 'owner_pid': 4000000001, 'owner_start': '7'}
        registry.write_text(json.dumps({'schema_version': 1,
            'runs': {var['run_id']: var, run_id: failed}}))
        self.assertTrue(RR.resource_never_started(failed))
        self.assertNotIn('payload_spawned', failed)  # Old runner persisted no such field.
        self.assertFalse(Path(failed['sentinel']).exists())
        self.assertEqual(SUP.resource_watch_for_run(ledger, body['node'], registry, run_id), None)

        payloads = []
        launch_failures = []
        actual_launch_error = runner.launch_error
        def describe_launch_error(error):
            import traceback
            launch_failures.append(''.join(traceback.format_exception(
                type(error), error, error.__traceback__)))
            return actual_launch_error(error)
        real_run, real_popen = runner.subprocess.run, subprocess.Popen
        def arm_transport(argv, *args, **kwargs):
            if (isinstance(argv, list) and len(argv) > 2
                    and Path(argv[1]).name == 'workflow-supervisor.py' and argv[2] == 'arm'):
                # In-process transport stand-in still executes the real arm operation.
                code = SUP.main(argv[2:])
                if kwargs.get('check') and code:
                    raise subprocess.CalledProcessError(code, argv)
                return subprocess.CompletedProcess(argv, code)
            return real_run(argv, *args, **kwargs)
        def capture_payload(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            if args and isinstance(args[0], list) and args[0][:2] == ['/bin/bash', '-p']:
                payloads.append(proc)
            return proc
        def public_start():
            out = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=True), \
                 contextlib.redirect_stdout(out):
                runner.main(cli)
            return json.loads(out.getvalue().splitlines()[-1])

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(WAIT, 'supervisor', return_value=SUP))
            # Source/smoke verifier and artifact environment are existing fixture boundaries.
            stack.enter_context(mock.patch.object(runner, 'verify_launch_input'))
            stack.enter_context(mock.patch.object(artifact_producer, 'prepare_route_artifact_env',
                return_value={'AGENT_ARTIFACT_OUTPUT_DIR': str(output)}))
            stack.enter_context(mock.patch.object(runner, 'register_registry'))
            stack.enter_context(mock.patch.object(runner, 'start_watch', return_value=(
                mock.Mock(poll=lambda: None), RR.proc_identity(os.getpid()))))
            stack.enter_context(mock.patch.object(runner.gpu_leases, 'resource_admission',
                return_value=(None, None, {})))
            stack.enter_context(mock.patch.object(runner.subprocess, 'run', side_effect=arm_transport))
            stack.enter_context(mock.patch.object(runner.subprocess, 'Popen', side_effect=capture_payload))
            stack.enter_context(mock.patch.object(runner, 'launch_error', side_effect=describe_launch_error))
            initial_registry = registry.read_bytes()
            initial_journal = ledger.journal_path.read_bytes()
            for invalid, changes in (
                    ('changed-command', {'command': [*body['command'], '--changed']}),
                    ('changed-launch-input', {'launch_request': {
                        **body['launch_request'], 'smoke_attestation_sha256': 'c' * 64}}),
                    ('claimed', {'launch_state': 'claimed', 'launch_controller': {
                        'pid': 4_000_000_000, 'starttime': '1'}}),
                    ('unknown-process', {'pid': 4_000_000_000, 'starttime': '1',
                                         'command_hash': 'c' * 64}),
                    ('foreign-owner', {'owner_wait': {
                        **owner_wait, 'owner_pid': 4000000002, 'owner_start': '7'}}),
                    ('sentinel', {}), ('already-armed', {})):
                with self.subTest(restore_refusal=invalid):
                    unsafe = {**failed, **changes}
                    registry.write_text(json.dumps({'schema_version': 1,
                        'runs': {var['run_id']: var, run_id: unsafe}}))
                    if invalid == 'sentinel':
                        Path(unsafe['sentinel']).write_text('0\n')
                    if invalid == 'already-armed':
                        self_watch = {**seed, 'predecessor_id': run_id,
                                      'resource_binding': WAIT.resource_legacy_body_digest(unsafe)}
                        primary.write_text(json.dumps(self_watch))
                    before = registry.read_bytes(), primary.read_bytes()
                    try:
                        with self.assertRaises((ValueError, SystemExit)):
                            public_start()
                        self.assertEqual((registry.read_bytes(), primary.read_bytes()), before)
                        self.assertEqual(ledger.journal_path.read_bytes(), initial_journal)
                        self.assertEqual(payloads, [])
                    finally:
                        if invalid == 'sentinel':
                            Path(unsafe['sentinel']).unlink()
                        primary.write_bytes(var_watch_bytes)
                        registry.write_bytes(initial_registry)
            queued = public_start()
            self.assertEqual(queued['run_id'], run_id)
            self.assertEqual(queued['launch_state'], 'queued')
            self.assertEqual(queued['resource_operation'], operation)
            self.assertEqual(queued['owner_wait'], owner_wait)
            self.assertFalse(queued['payload_spawned'])
            self.assertEqual(payloads, [])
            self.assertEqual(json.loads(registry.read_text())['runs'][var['run_id']], var_snapshot)
            self.assertEqual(SUP.resource_watch_storage_path(ledger, seed).read_bytes(), var_watch_bytes)
            self.assertTrue(any((entry.get('evidence') or {}).get('previous_resource') == failed
                                for entry in ledger.journal()))
            args = SimpleNamespace(parent_attempt_id=owner_attempt, route_file=str(path),
                route_id=route['route_id'], route_hash=route['route_hash'], jobs=str(jobs),
                resource_launch_command=lambda row: (row['command'], 'fixture-local-cpu'))
            control = SimpleNamespace(thread_id='same-native', pending=lambda: False)
            found = WAIT.context(args, control)
            candidates = [pair for pair in found[3] if WAIT.controller_intent(pair[1])]
            self.assertEqual([row['run_id'] for _, row in candidates], [run_id])
            armed, row = candidates[0]
            WAIT.admit_controller_launch(args, control, armed, row, [])
            self.assertEqual(len(payloads), 1, launch_failures or
                             json.loads(registry.read_text())['runs'][run_id])
            self.assertEqual(payloads[0].wait(timeout=10), 0)
            self.assertEqual(result_path.read_text(), 'cpu-bridge-payload-complete\n')
            replay = public_start()
            self.assertTrue(replay['replayed'])
            self.assertFalse(replay['payload_spawned'])
            self.assertEqual(len(payloads), 1)
        self.assertEqual(json.loads(registry.read_text())['runs'][var['run_id']], var_snapshot)
        self.assertEqual(SUP.resource_watch_storage_path(ledger, seed).read_bytes(), var_watch_bytes)
        self.assertEqual(checkpoint.read_bytes(), checkpoint_bytes)
        self.assertIsNone(var_process.poll())

    def _queue_current_legacy_bridge_operation(self, route, path, jobs, registry,
                                               owner_attempt, body, output):
        """Use the current normal owner start to publish one old-readable flat row."""
        run_id = body['run_id']
        attestation_path = self.base / f'{run_id}-smoke-attestation.json'
        smoke_spec = importlib.util.spec_from_file_location(
            f'legacy_fixture_smoke_attestation_{run_id}', ROOT / 'tools' / 'smoke-attestation.py')
        smoke_tool = importlib.util.module_from_spec(smoke_spec)
        smoke_spec.loader.exec_module(smoke_tool)
        smoke_inputs = [self.base / f'{run_id}-config.yaml',
                        self.base / f'{run_id}-data.lock',
                        self.base / f'{run_id}-smoke.json']
        attestation = smoke_tool.payload(smoke_inputs, body['command'], body['cwd'])
        attestation.update(exit_code=0, status='passed')
        attestation['attestation_hash'] = 'sha256:' + hashlib.sha256(json.dumps(
            attestation, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        attestation_path.write_text(json.dumps(attestation, sort_keys=True))
        request_path = Path(body['command'][-1])
        request = json.loads(request_path.read_text())
        attestation_hash = hashlib.sha256(attestation_path.read_bytes()).hexdigest()
        request['smoke_attestation'] = str(attestation_path)
        request['smoke_attestation_sha256'] = attestation_hash
        request['provenance']['smoke_attestation_sha256'] = attestation_hash
        request_bytes = json.dumps(request, sort_keys=True).encode()
        request_path.write_bytes(request_bytes)
        record_path = Path(request['Dev_admission_evidence']).parent / \
            f'{run_id}-resource-start-command.json'
        command_record = json.loads(record_path.read_text())
        command_record['request_sha256'] = hashlib.sha256(request_bytes).hexdigest()
        separator = command_record['argv'].index('--')
        command_record['argv'][separator:separator] = ['--smoke-attestation', str(attestation_path)]
        record_path.write_text(json.dumps(command_record, sort_keys=True))
        body['launch_request'] = {
            'smoke_attestation': str(attestation_path),
            'smoke_attestation_sha256': attestation_hash,
            'config_manifest': None, 'config_manifest_sha256': None,
        }
        body['resource_operation'] = SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            dict(body['launch_request']), body['owner_wait'],
            registry_path=registry, log_path=body['log'])
        self.assertIsNotNone(body['resource_operation'])
        body.pop('launch_state', None)
        current_runner = SUP.runner()
        current_args = SimpleNamespace(
            jobs=str(jobs), run_id=body['run_id'], node=body['node'],
            smoke_attestation=str(attestation_path), config_manifest=None)
        import artifact_producer
        with mock.patch.object(artifact_producer, 'prepare_route_artifact_env',
                               return_value={'AGENT_ARTIFACT_OUTPUT_DIR': str(output)}), \
             mock.patch.object(current_runner, 'register_registry'), \
             contextlib.redirect_stdout(io.StringIO()):
            current_runner.start_verified(registry, current_args, route, path, dict(body))
        return json.loads((SUP.ledger_for(route, jobs).root / 'armed' / 'full-run.json').read_bytes())

    def arm(self, path, registry, *, node='full-run', run_id='fixture-run', extra=()):
        argv = ['arm', '--route', str(path), '--node', node, '--predecessor-kind', 'resource',
                '--predecessor-id', run_id, '--resource-registry', str(registry),
                '--successor-external', '--successor-log',
                str(SUP.ledger_for(SUP.load_route(path), self.base / 'jobs.log').root / 'resource/verification-start.log'), *extra]
        # The initial inherited fixture uses its own original arm helper.
        if run_id == 'fixture-run':
            return FIX.WorkflowFixture.arm(self, path, registry, node=node, extra=extra)
        with contextlib.redirect_stdout(io.StringIO()):
            return SUP.main(argv)

    def launch(self, route, path, jobs, registry, output, body, *, controller=None,
               close_at=None, exit_code=0, wait_for_exit=True):
        import artifact_producer
        runner = SUP.runner()
        args = SimpleNamespace(jobs=str(jobs), run_id=body['run_id'], node=body['node'])
        payloads = []
        real_popen = subprocess.Popen
        def popen(*a, **kw):
            proc = real_popen(*a, **kw)
            payloads.append(proc)
            if close_at == 'fence':
                close()
            return proc
        def close():
            ledger = SUP.ledger_for(route, jobs)
            with ledger.lock():
                ledger.record(body['node'], 'RUNNING', evidence={'parent_close': {'preserve_resource': True}})
        def arm(argv, **kwargs):
            if close_at == 'reservation':
                close()
            result = SUP.main(argv[2:])
            if close_at == 'arm':
                close()
            return subprocess.CompletedProcess(argv, result)
        watch = mock.Mock()
        watch.poll.return_value = None
        with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": ""}), \
                mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
                mock.patch.object(artifact_producer, 'prepare_route_artifact_env',
                    return_value={'AGENT_ARTIFACT_OUTPUT_DIR': str(output)}), \
                mock.patch.object(runner, 'register_registry'), \
                mock.patch.object(runner, 'start_watch', return_value=(watch, SUP.RR.proc_identity(os.getpid()))), \
                mock.patch.object(runner.subprocess, 'run', side_effect=arm), \
                mock.patch.object(runner.subprocess, 'Popen', side_effect=popen), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            runner.start_verified(registry, args, route, path, dict(body), controller=controller)
        if wait_for_exit:
            for proc in payloads:
                self.assertEqual(proc.wait(timeout=5), exit_code)
        return payloads, json.loads(out.getvalue().splitlines()[-1])

    def test_verified_sequential_launch_release_accepts_multidigit_fd(self):
        with self.subfixture():
            route, path, jobs, registry, output, ledger = self.fixture()
            body = self.next_body(registry, 'highfd-next', output)
            marker = self.base / 'highfd-payload'
            body['command'] = [sys.executable, '-c',
                'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])',
                str(marker), 'argv-preserved']
            held = []
            try:
                for _ in range(6):
                    held.extend(os.pipe())
                self.assertGreaterEqual(max(held), 10)
                payloads, receipt = self.launch(route, path, jobs, registry, output, body)
            finally:
                for fd in held:
                    os.close(fd)
            self.assertTrue(receipt['payload_spawned'])
            self.assertEqual(len(payloads), 1)
            self.assertEqual(marker.read_text(), 'argv-preserved')
            self.assertEqual(SUP.runner().read_sentinel(receipt['sentinel']), 0)

    def test_guard_exit_same_route_resume_preserves_checkpoint_until_owner_successor_start(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        registry.write_text(json.dumps({'schema_version': 1, 'runs': {}}))
        (ledger.root / 'armed/full-run.json').unlink()
        checkpoint = self.base / 'checkpoint'
        def body(name, code):
            log = self.base / (name + '.log')
            return {'run_id': name, 'route': str(path), 'node': 'full-run', 'jobs': str(jobs),
                    'cwd': str(self.base), 'log': str(log), 'sentinel': str(log) + '.exit',
                    'command': [sys.executable, '-c', code],
                    'status': 'launching', 'workflow_state': 'READY',
                    'parent_attempt_id': 'att-parent', 'resource_policy': 'supervised-owner',
                    'owner_wait': {'parent_attempt_id': 'att-parent', 'session_id': 'same-native'}}
        first = body('paused', f'from pathlib import Path; Path({str(checkpoint)!r}).write_text("saved"); raise SystemExit(3)')
        self.launch(route, path, jobs, registry, output, first, exit_code=3)
        failed = SUP.poll_once(route, ledger)[0]
        self.assertEqual(failed['action'], 'halt-failed')
        self.assertEqual(failed['evidence']['exit_code'], 3)
        self.assertEqual(ledger.claims(), {})
        old = json.loads(registry.read_text())['runs']['paused']
        old_bytes = Path(old['sentinel']).read_bytes(), Path(old['log']).read_bytes()
        old_arm = SUP.read_armed(ledger)['full-run']
        resumed = body('resumed',
            f'from pathlib import Path; assert Path({str(checkpoint)!r}).read_text() == "saved"; '
            f'Path({str(output / "run.json")!r}).write_text({json.dumps({"phase":"resumed"})!r})')
        procs, receipt = self.launch(route, path, jobs, registry, output, resumed)
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
        self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], 'resumed')
        self.assertTrue(any((entry.get('evidence') or {}).get('previous_resource') == old_arm
                            for entry in ledger.journal()))
        result = SUP.poll_once(route, ledger)[0]
        self.assertEqual(result['action'], 'wait-next-resource')
        self.assertTrue(result['evidence']['succeeded'])
        self.assertEqual(len(ledger.claims()), 0)
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertFalse((jobs.parent / 'completion' / route['route_id'] / 'full-run.json').exists())
        self.assertEqual(json.loads(registry.read_text())['runs']['paused'], old)
        self.assertEqual((Path(old['sentinel']).read_bytes(), Path(old['log']).read_bytes()), old_bytes)
        self.assertEqual(json.loads((output / 'run.json').read_text()), {"phase":"resumed"})

    def test_rebooted_running_resource_without_sentinel_is_settled_before_real_successor(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        data = json.loads(registry.read_text())
        old = data['runs']['fixture-run']
        Path(old['sentinel']).unlink()
        old.update(status='running', workflow_state='RUNNING', exit_code=None,
                   boot_id='83f954bc-4963-4dfa-9f2f-c8f3597900a6', boot_host='local')
        data['runs']['fixture-run'] = old
        registry.write_text(json.dumps(data))
        checkpoint = self.base / 'checkpoint'
        checkpoint.write_text('epoch-26')
        Path(old['log']).write_text('epoch-26 saved\n')
        log_before = Path(old['log']).read_bytes()
        body = self.next_body(registry, 'fixture-run__a1', output)
        body['command'] = [sys.executable, '-c',
            f'from pathlib import Path; assert Path({str(checkpoint)!r}).read_text() == "epoch-26"']
        current = SUP.RR.boot_identity()
        # New payload capture must retain the real boot; only the old row belongs to the prior one.
        old['boot_host'] = current['boot_host']
        registry.write_text(json.dumps(data))
        procs, receipt = self.launch(route, path, jobs, registry, output, body)
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        settled = json.loads(registry.read_text())['runs']['fixture-run']
        self.assertEqual((settled['status'], settled['failure_class']), ('failed', 'host-reboot'))
        self.assertIsNone(settled['exit_code'])
        self.assertFalse(Path(old['sentinel']).exists())
        self.assertEqual(Path(old['log']).read_bytes(), log_before)
        self.assertEqual(checkpoint.read_text(), 'epoch-26')
        again, repeated = self.launch(route, path, jobs, registry, output, body)
        self.assertEqual(again, [])
        self.assertFalse(repeated['payload_spawned'])

    def unstarted_fixture(self, **changes):
        route, path, jobs, registry, output, ledger = self.fixture()
        data = json.loads(registry.read_text())
        row = data['runs']['fixture-run']
        Path(row['sentinel']).unlink()
        for key in ('pid', 'starttime', 'command_hash', 'process_group', 'pid_namespace',
                    'launch_argv', 'supervision', 'exit_code', 'ended_at'):
            row.pop(key, None)
        row.update(status='failed', workflow_state='FAILED_RETRYABLE',
                   failure_class='resource-launch-incomplete', **changes)
        registry.write_text(json.dumps(data))
        return route, path, jobs, registry, output, ledger

    def test_legacy_armed_admission_failure_settles_and_retries_same_node_once(self):
        route, path, jobs, registry, output, ledger = self.unstarted_fixture()
        result = SUP.poll_once(route, ledger)[0]
        self.assertEqual(result['action'], 'halt-failed')
        self.assertTrue(result['evidence']['never_started'])
        self.assertIsNone(result['evidence']['exit_code'])
        self.assertEqual(ledger.state()['workflow_state'], 'FAILED_RETRYABLE')
        self.assertEqual(ledger.claims(), {})
        old = json.loads(registry.read_text())['runs']['fixture-run']
        self.assertTrue(old['ended_at'])
        retry = self.next_body(registry, 'fixture-run__a1', output)
        payloads, receipt = self.launch(route, path, jobs, registry, output, retry)
        self.assertEqual(len(payloads), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
        self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], retry['run_id'])
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)
        payloads, receipt = self.launch(route, path, jobs, registry, output, retry)
        self.assertEqual(payloads, [])
        self.assertFalse(receipt['payload_spawned'])
        self.assertEqual(ledger.claims(), {})

    def test_unproven_claim_and_post_start_crash_do_not_admit_retry(self):
        route, path, jobs, registry, output, ledger = self.unstarted_fixture()
        original = json.loads(registry.read_text())['runs']['fixture-run']
        for changes in ({'launch_state': 'claimed', 'launch_controller': {'pid': 123}},
                        {'launch_state': 'started'}, {'pid': 999999999, 'starttime': '1', 'command_hash': 'x'},
                        {'failure_class': 'no-exit-sentinel'}, {'exit_code': 1},
                        {'cancel_requested': True}, {'parent_close_requested': True},
                        {'launch_state': 'not-started', **SUP.RR.proc_identity(os.getpid())}):
            with self.subTest(changes=changes):
                registry.write_text(json.dumps({'schema_version': 1, 'runs': {
                    'fixture-run': {**original, **changes}}}))
                old = json.loads(registry.read_text())['runs']['fixture-run']
                self.assertFalse(SUP.RR.resource_never_started(old))
                self.assertFalse(WAIT.resource_execution_finished(old))
                retry = self.next_body(registry, 'fixture-run__a1', output)
                with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
                    self.launch(route, path, jobs, registry, output, retry)
                self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)

    def test_runner_failure_after_arm_settles_without_payload_and_opens_retry(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        body = self.next_body(registry, 'fixture-run__a1', output)
        forbidden = self.base / 'payload-must-not-start'
        body['command'] = [sys.executable, '-c',
            f'from pathlib import Path; Path({str(forbidden)!r}).write_text("wrong")']
        # Fail the watch identity check after real arm and wrapper spawn,
        # before the private fence permits the payload to execute.
        with mock.patch.object(SUP.runner().RESOURCE_RESUME, 'supervisor_alive', return_value=False):
            self.launch(route, path, jobs, registry, output, body, exit_code=125)
        row = json.loads(registry.read_text())['runs'][body['run_id']]
        self.assertEqual(row['launch_state'], 'not-started')
        self.assertFalse(Path(row['sentinel']).exists())
        self.assertFalse(forbidden.exists())
        self.assertEqual(ledger.state()['workflow_state'], 'FAILED_RETRYABLE')
        self.assertEqual(ledger.claims(), {})
        retry = self.next_body(registry, 'fixture-run__a2', output)
        payloads, receipt = self.launch(route, path, jobs, registry, output, retry)
        self.assertEqual(len(payloads), 1)
        self.assertTrue(receipt['payload_spawned'])

    def test_native_owner_reads_exact_never_started_failure_without_pid(self):
        import owner_route_binding as OWNER
        route, path, jobs, registry, output, ledger = self.unstarted_fixture(launch_state='not-started')
        data = json.loads(registry.read_text())
        row = data['runs']['fixture-run']
        ident = SUP.RR.proc_identity(os.getpid())
        row['owner_wait'].update(route_id=route['route_id'], route_hash=route['route_hash'],
            jobs=str(jobs), owner_pid=ident['pid'], owner_start=ident['starttime'])
        registry.write_text(json.dumps(data))
        arm_path = ledger.root / 'armed/full-run.json'
        armed = json.loads(arm_path.read_text())
        armed.update(resource_binding=WAIT.resource_body_digest(row), successor_external=True,
                     successor_command=None)
        arm_path.write_text(json.dumps(armed))
        args = SimpleNamespace(parent_attempt_id='att-parent', route_id=route['route_id'],
            route_hash=route['route_hash'], route_file=str(path), jobs=str(jobs))
        binding = SimpleNamespace(route_file=str(path), route_id=route['route_id'], route_hash=route['route_hash'])
        parent = SimpleNamespace(status='open', raw='time\topen\trepo\tworktree\tslug\tmeta',
            metadata={'pid':str(ident['pid']), 'pid_start':ident['starttime']})
        with mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
             mock.patch.object(OWNER, 'resolve_owner_route_lifecycle', return_value=(binding, None)), \
             mock.patch.object(OWNER, '_owner_row_proof'), \
             mock.patch.object(WAIT.JOIN, 'exact_attempt_row', return_value=parent):
            found = WAIT.context(args, SimpleNamespace(thread_id='same-native'))
            self.assertEqual(found[3][0][1]['run_id'], 'fixture-run')
            self.assertFalse(found[3][0][1].get('pid'))
            row['failure_class'] = 'no-exit-sentinel'
            registry.write_text(json.dumps(data))
            with self.assertRaisesRegex(WAIT.JOIN.JoinContractError, 'resource-owner-binding-invalid'):
                WAIT.context(args, SimpleNamespace(thread_id='same-native'))

    def test_gpu_admission_failure_precedes_arm_and_can_retry(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        body = self.next_body(registry, 'fixture-run__a1', output)
        with mock.patch.object(SUP.runner().gpu_leases, 'resource_admission',
                               side_effect=SUP.runner().gpu_leases.GPUUnavailable('unknown GPU 99')), \
             mock.patch.object(SUP, 'main') as arm:
            self.launch(route, path, jobs, registry, output, body)
        arm.assert_not_called()
        row = json.loads(registry.read_text())['runs'][body['run_id']]
        self.assertTrue(SUP.RR.resource_never_started(row))
        retry = self.next_body(registry, 'fixture-run__a2', output)
        payloads, receipt = self.launch(route, path, jobs, registry, output, retry)
        self.assertEqual(len(payloads), 1)
        self.assertTrue(receipt['payload_spawned'])

    def receipt_fixture(self, *, queued_controller=False, failed=False):
        route, path, jobs, registry, output, ledger = self.fixture()
        if queued_controller:
            data = json.loads(registry.read_text())
            row = data['runs']['fixture-run']
            row['owner_wait']['launch_scope'] = 'codex-owner-controller'
            registry.write_text(json.dumps(data))
            arm_path = ledger.root / 'armed/full-run.json'
            armed = json.loads(arm_path.read_text())
            armed['resource_binding'] = WAIT.resource_body_digest(row)
            arm_path.write_text(json.dumps(armed))
        if failed:
            data = json.loads(registry.read_text())
            row = data['runs']['fixture-run']
            Path(row['sentinel']).write_text('3')
            row.update(status='failed', exit_code=3)
            registry.write_text(json.dumps(data))
        SUP.poll_once(route, ledger)
        args = SimpleNamespace(parent_attempt_id='att-parent', route_id=route['route_id'],
            route_hash=route['route_hash'], route_file=str(path), jobs=str(jobs))
        control = SimpleNamespace(thread_id='same-native', pending=lambda: False)
        state = self.base / 'state.json'
        WAIT.JOIN.write_supervisor_state(state, 'att-parent', set(), phase='running-turn')
        def context(*_):
            armed = SUP.read_armed(ledger)['full-run']
            row = json.loads(Path(armed['resource_registry']).read_text())['runs'][armed['predecessor_id']]
            return SUP, route, ledger, [(armed, row)]
        with mock.patch.object(WAIT, 'context', side_effect=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP):
            prompt = WAIT.wait(args, state, control, set(), lambda _: None, sleep=lambda _: self.fail('extra wait'))
        return route, path, jobs, registry, output, ledger, args, control, state, context, prompt

    def test_failed_predecessor_can_resume_before_poll_and_with_new_owner_or_registry(self):
        for observed, new_owner, new_registry in ((False, False, False), (True, True, False), (True, False, True)):
            with self.subTest(observed=observed, new_owner=new_owner, new_registry=new_registry), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                data = json.loads(registry.read_text())
                old = data['runs']['fixture-run']
                Path(old['sentinel']).write_text('3')
                old.update(status='failed', exit_code=3)
                registry.write_text(json.dumps(data))
                if observed:
                    self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'halt-failed')
                body = self.next_body(registry, 'next', output, final=True)
                if new_owner:
                    body.update(parent_attempt_id='att-resumed',
                                owner_wait={'parent_attempt_id': 'att-resumed', 'session_id': 'resumed-native'})
                target = registry
                if new_registry:
                    target = self.base / 'resumed-registry.json'
                    target.write_text(json.dumps({'runs': {}}))
                self.launch(route, path, jobs, target, output, body)
                self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], 'next')
                self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
                self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)
                self.assertEqual(Path(old['sentinel']).read_text(), '3')

    def test_settled_cancellation_rearms_normal_start_without_rewriting_history(self):
        for compute in (False, True):
            with self.subTest(compute=compute), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                data = json.loads(registry.read_text())
                old = data['runs']['fixture-run']
                Path(old['sentinel']).unlink()
                old.update(status='failed', workflow_state='CANCELLED', exit_code=None,
                           failure_class='cancelled', cancel_requested=True, ended_at=time.time(),
                           pid_namespace=os.readlink('/proc/self/ns/pid'))
                if compute:
                    compute_root = self.base / 'compute'
                    old.update(compute_run_id='remote-cancelled',
                               log=str(compute_root / 'remote-cancelled/log'),
                               sentinel=str(compute_root / 'remote-cancelled/exit_code'))
                    old['execution'] = {
                        'kind': 'compute-hosts', 'run_id': 'remote-cancelled',
                        'run_root': str(compute_root), 'meta_sha256': 'm' * 64,
                        'exit_sha256': 'e' * 64, 'receipt_sha256': 'r' * 64,
                        'outputs_sha256': 'o' * 64, 'contract_sha256': 'c' * 64,
                        'provenance': {'route_id': route['route_id'], 'route_hash': route['route_hash']},
                    }
                registry.write_text(json.dumps(data))
                armed = SUP.read_armed(ledger)['full-run']
                armed['resource_binding'] = WAIT.resource_body_digest(old)
                (ledger.root / 'armed/full-run.json').write_text(json.dumps(armed))
                body = self.next_body(registry, 'next', output, final=True)
                body['owner_wait'] = {'parent_attempt_id': 'att-parent', 'session_id': 'same-native'}
                def observe_outside_locks(_row):
                    for lock_path in (ledger.lock_path, Path(str(registry) + '.lock')):
                        with lock_path.open('a+') as stream:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    return {'state': 'quiescent'}
                with mock.patch.object(SUP.RR, 'compute_execution_observation',
                                       side_effect=observe_outside_locks):
                    payloads, receipt = self.launch(route, path, jobs, registry, output, body)
                self.assertTrue(receipt['payload_spawned'], receipt)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], 'next')
                self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)
                self.assertFalse(Path(old['sentinel']).exists())

    def test_verified_resume_failed_predecessor_rearms_and_claims_verification_once(self):
        route, path, jobs, registry, output = self._resume_fixture()
        ledger = SUP.ledger_for(route, jobs)
        armed_path = ledger.root / 'armed/resume-run.json'
        armed = json.loads(armed_path.read_text())
        armed['successor_command'] = [sys.executable, str(HERE / 'capability-route.py'),
                                     'start', '--route', str(path), '--jobs', str(jobs)]
        armed['successor_log'] = str(ledger.root / 'resource/verification-start.log')
        armed_path.write_text(json.dumps(armed))
        data = json.loads(registry.read_text())
        old = data['runs']['fixture-run']
        Path(old['sentinel']).write_text('3')
        old.update(status='failed', exit_code=3)
        registry.write_text(json.dumps(data))
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'halt-failed')
        self.launch(route, path, jobs, registry, output, self.next_body(registry, 'resumed', output, final=True))
        with mock.patch.object(SUP, '_start_successor', return_value={'started': True}) as start:
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'advanced')
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'settled')
            self.assertEqual(start.call_count, 1)
        claim = next(iter(ledger.claims().values()))
        self.assertEqual(claim['successor'], 'one-shot')
        self.assertTrue(WS.route_node(route, 'one-shot')['verification_only'])
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)

    def test_failed_exit_in_another_pid_namespace_waits_for_group_observation_before_retry(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        data = json.loads(registry.read_text())
        old = data['runs']['fixture-run']
        self.assertIsNotNone(old.get('process_group'))
        Path(old['sentinel']).write_text('1')
        old.update(status='failed', exit_code=1, pid_namespace='pid:[foreign]')
        registry.write_text(json.dumps(data))
        before = copy.deepcopy(old), Path(old['sentinel']).read_bytes(), registry.read_bytes()
        self.assertEqual(SUP.RR.classify_identity(old),
                         ('stale', None, 'process-namespace-mismatch'))
        self.assertFalse(SUP.RR.resource_process_group_quiescent(old))
        retry = self.next_body(registry, 'namespace-retry', output)
        with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
            self.launch(route, path, jobs, registry, output, retry)
        self.assertEqual(registry.read_bytes(), before[2])
        self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], 'fixture-run')

        # The runner's ordinary retry path admits once its group helper has a
        # positive observation that the foreign recorded group has ended.
        with mock.patch.object(SUP.RR, 'resource_process_group_quiescent', return_value=True) as observe:
            procs, receipt = self.launch(route, path, jobs, registry, output, retry)
        self.assertGreaterEqual(observe.call_count, 1)
        self.assertTrue(all(call.args[0] == old for call in observe.call_args_list))
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(SUP.read_armed(ledger)['full-run']['predecessor_id'], 'namespace-retry')
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], before[0])
        self.assertEqual(Path(old['sentinel']).read_bytes(), before[1])

    def test_cross_role_and_aliased_evidence_paths_refuse_without_changing_bytes(self):
        for collision in ('log-sentinel', 'sentinel-log', 'symlink', 'hardlink', 'partial', 'progress'):
            with self.subTest(collision=collision), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                SUP.poll_once(route, ledger)
                body = self.next_body(registry, 'next', output)
                old = json.loads(registry.read_text())['runs']['fixture-run']
                if collision == 'log-sentinel':
                    body['log'] = old['sentinel']
                elif collision == 'sentinel-log':
                    body['sentinel'] = old['log']
                elif collision == 'partial':
                    body['log'] = old['sentinel'] + '.partial'
                elif collision == 'progress':
                    body['progress_file'] = old['sentinel']
                else:
                    alias = self.base / 'alias'
                    if collision == 'symlink':
                        alias.symlink_to(old['sentinel'])
                    else:
                        os.link(old['sentinel'], alias)
                    body['log'] = str(alias)
                before = registry.read_bytes(), Path(old['sentinel']).read_bytes(), SUP.read_armed(ledger)
                with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
                    self.launch(route, path, jobs, registry, output, body)
                self.assertEqual((registry.read_bytes(), Path(old['sentinel']).read_bytes(), SUP.read_armed(ledger)), before)

    def test_third_registry_cannot_reuse_first_resource_evidence(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        middle_registry = self.base / 'middle-registry.json'
        middle_registry.write_text(json.dumps({'runs': {}}))
        self.launch(route, path, jobs, middle_registry, output, self.next_body(registry, 'middle', output))
        SUP.poll_once(route, ledger)
        body = self.next_body(registry, 'last', output)
        old = json.loads(registry.read_text())['runs']['fixture-run']
        body['log'] = old['sentinel']
        last_registry = self.base / 'last-registry.json'
        last_registry.write_text(json.dumps({'runs': {}}))
        before = last_registry.read_bytes(), Path(old['sentinel']).read_bytes()
        with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
            self.launch(route, path, jobs, last_registry, output, body)
        self.assertEqual((last_registry.read_bytes(), Path(old['sentinel']).read_bytes()), before)
        last_registry.write_text(json.dumps({'runs': {body['run_id']: body}}))
        prior = SUP.read_armed(ledger)
        with self.assertRaisesRegex(ValueError, 'resource-watch-binding-conflict'):
            self.arm(path, last_registry, run_id=body['run_id'], extra=('--jobs', str(jobs), '--artifact-base', str(output)))
        self.assertEqual(SUP.read_armed(ledger), prior)

    def test_unacknowledged_receipt_survives_next_registration_and_queued_intent(self):
        for other_registry, queued in ((False, False), (True, False), (True, True)):
            with self.subTest(other_registry=other_registry, queued=queued), self.subfixture():
                route, path, jobs, registry, output, ledger, args, control, state, context, prompt = self.receipt_fixture(queued_controller=queued)
                body = self.next_body(registry, 'next', output)
                target = registry
                if other_registry:
                    target = self.base / 'next-registry.json'
                    target.write_text(json.dumps({'runs': {}}))
                if queued:
                    body.update(launch_state='queued', status='launching',
                                launch_request=dict(self.EMPTY_LAUNCH_REQUEST))
                    self.assertTrue(WAIT.controller_intent(body))
                    target.write_text(json.dumps({'runs': {'next': body}}))
                    self.arm(path, target, run_id='next', extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                else:
                    self.launch(route, path, jobs, target, output, body)
                with mock.patch.object(WAIT, 'context', side_effect=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
                        mock.patch.object(WAIT, 'admit_controller_launch') as admit:
                    self.assertEqual(WAIT.pending_prompt(state, 'att-parent', args, control), prompt)
                    self.assertEqual(WAIT.wait(args, state, control, set(), lambda _: None), prompt)
                    admit.assert_not_called()
                saved = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource
                self.assertEqual(saved['delivered'], [])
                self.assertTrue(WAIT.acknowledge(state, 'att-parent', saved['outbox']['receipt_id']))
                self.assertFalse(WAIT.acknowledge(state, 'att-parent', saved['outbox']['receipt_id']))
                self.assertIsNone(WAIT.pending_prompt(state, 'att-parent', args, control))

    def test_unacknowledged_failure_receipt_survives_retry_and_receiving_turn_interruption(self):
        for other_registry, queued in ((False, False), (True, False), (True, True)):
            with self.subTest(other_registry=other_registry, queued=queued), self.subfixture():
                route, path, jobs, registry, output, ledger, args, control, state, context, prompt = self.receipt_fixture(queued_controller=queued, failed=True)
                saved = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource
                self.assertEqual(saved['outbox']['receipt']['exit_code'], 3)
                self.assertEqual(saved['outbox']['receipt']['reason'], 'FAILED_RETRYABLE')
                body = self.next_body(registry, 'retry', output)
                target = registry
                if other_registry:
                    target = self.base / 'retry-registry.json'
                    target.write_text(json.dumps({'runs': {}}))
                if queued:
                    body.update(launch_state='queued', status='launching',
                                launch_request=dict(self.EMPTY_LAUNCH_REQUEST))
                    target.write_text(json.dumps({'runs': {'retry': body}}))
                    self.arm(path, target, run_id='retry', extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                else:
                    self.launch(route, path, jobs, target, output, body)
                with mock.patch.object(WAIT, 'context', side_effect=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
                        mock.patch.object(WAIT, 'admit_controller_launch') as admit:
                    self.assertEqual(WAIT.pending_prompt(state, 'att-parent', args, control), prompt)
                    self.assertEqual(WAIT.wait(args, state, control, set(), lambda _: None), prompt)
                    admit.assert_not_called()
                self.assertEqual(WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource, saved)
                self.assertTrue(WAIT.acknowledge(state, 'att-parent', saved['outbox']['receipt_id']))
                self.assertFalse(WAIT.acknowledge(state, 'att-parent', saved['outbox']['receipt_id']))
                self.assertIsNone(WAIT.pending_prompt(state, 'att-parent', args, control))
                if not queued:
                    with mock.patch.object(WAIT, 'context', side_effect=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP):
                        next_prompt = WAIT.wait(args, state, control, set(), lambda _: None,
                                                sleep=lambda _: self.fail('completed retry should not wait'))
                    self.assertIn('"run_id":"retry"', next_prompt)
                    current = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource
                    self.assertEqual(current['delivered'], [saved['outbox']['key']])
                    self.assertTrue(WAIT.acknowledge(state, 'att-parent', current['outbox']['receipt_id']))

    def test_historical_receipt_rejects_mutated_identity_sentinel_and_session(self):
        for failed, mutation in ((failed, mutation) for failed in (False, True)
                                 for mutation in ('identity', 'sentinel', 'session')):
            with self.subTest(failed=failed, mutation=mutation), self.subfixture():
                route, path, jobs, registry, output, ledger, args, control, state, context, prompt = self.receipt_fixture(failed=failed)
                self.launch(route, path, jobs, registry, output, self.next_body(registry, 'next', output))
                data = json.loads(registry.read_text())
                old = data['runs']['fixture-run']
                if mutation == 'identity':
                    old['starttime'] = 'changed'
                    registry.write_text(json.dumps(data))
                elif mutation == 'sentinel':
                    Path(old['sentinel']).write_text('7')
                else:
                    control.thread_id = 'foreign-native'
                with mock.patch.object(WAIT, 'context', side_effect=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP):
                    with self.assertRaisesRegex((ValueError, WAIT.JOIN.JoinContractError), 'binding'):
                        WAIT.pending_prompt(state, 'att-parent', args, control)
                self.assertIsNotNone(WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource['outbox'])

    def test_close_between_reservation_arm_and_release_never_runs_payload(self):
        for controller_mode in (False, True):
            for close_at in ('reservation', 'arm', 'fence'):
                with self.subTest(controller=controller_mode, close_at=close_at), self.subfixture():
                    route, path, jobs, registry, output, ledger = self.fixture()
                    SUP.poll_once(route, ledger)
                    body = self.next_body(registry, 'next', output)
                    effect = self.base / 'payload-effect'
                    body['command'] = [sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(effect)) + ').touch()']
                    controller = None
                    if controller_mode:
                        body['launch_state'] = 'queued'
                        body['launch_request'] = dict(self.EMPTY_LAUNCH_REQUEST)
                        data = json.loads(registry.read_text())
                        data['runs']['next'] = body
                        registry.write_text(json.dumps(data))
                        controller = SimpleNamespace(expected=copy.deepcopy(body), identity={**SUP.RR.proc_identity(os.getpid()),
                            'pid_namespace': os.readlink('/proc/self/ns/pid')}, command=body['command'],
                            sandbox='fixture', guard=contextlib.nullcontext)
                    self.launch(route, path, jobs, registry, output, body, controller=controller,
                                close_at=close_at, exit_code=125)
                    self.assertFalse(effect.exists())
                    self.assertEqual(ledger.claims(), {})
                    self.assertEqual(json.loads(registry.read_text())['runs']['next']['status'], 'failed')

    def test_next_registration_preserves_existing_active_workflow_progress(self):
        for target_state in ('STAGE_SUCCEEDED', 'NEXT_REGISTERED', 'NEXT_RUNNING'):
            with self.subTest(state=target_state), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                SUP.poll_once(route, ledger)
                for step in ('STAGE_SUCCEEDED', 'NEXT_REGISTERED', 'NEXT_RUNNING'):
                    ledger.set_workflow_state(step)
                    if step == target_state:
                        break
                self.launch(route, path, jobs, registry, output, self.next_body(registry, 'next', output))
                self.assertEqual(ledger.state()['workflow_state'], target_state)
                self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')

    def test_65th_receipt_acknowledges_once_without_lifetime_count_limit(self):
        route, path, jobs, registry, output, ledger, args, control, state, context, prompt = self.receipt_fixture()
        saved = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent')
        resource = copy.deepcopy(saved.resource)
        resource['delivered'] = [f'{i:064x}' for i in range(64)]
        WAIT._write(state, 'att-parent', set(), resource, 'deliverable')
        receipt_id = resource['outbox']['receipt_id']
        self.assertTrue(WAIT.acknowledge(state, 'att-parent', receipt_id))
        self.assertFalse(WAIT.acknowledge(state, 'att-parent', receipt_id))
        resource = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource
        self.assertEqual(len(resource['delivered']), 65)
        self.assertIsNone(resource['outbox'])
        with mock.patch.object(WAIT, 'context', side_effect=context):
            self.assertIsNone(WAIT.wait(args, state, control, set(), lambda _: None, sleep=lambda _: self.fail('already delivered')))
        for invalid in ([resource['delivered'][0]] * 2, ['invalid-digest']):
            self.assertFalse(WAIT.JOIN.valid_resource_state({**resource, 'delivered': invalid}))

    def test_intermediate_success_waits_without_marker_claim_or_completion(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        result = SUP.poll_once(route, ledger)[0]
        self.assertEqual(result['action'], 'wait-next-resource')
        self.assertTrue(result['evidence']['succeeded'])
        self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertEqual(ledger.claims(), {})
        self.assertFalse((jobs.parent / 'completion' / route['route_id'] / 'full-run.json').exists())
        journal = ledger.journal_path.read_bytes()
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        self.assertEqual(ledger.journal_path.read_bytes(), journal)
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, 'completed')
        self.assertTrue(ledger.journal_path.read_bytes().startswith(journal))

    def test_prepared_owner_manifest_stays_provisional_until_existing_successor_start(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        producer = output / 'run.json'
        prepared = b'{"state":"prepared","runs":[{"status":"prepared"}]}\n'
        producer.write_bytes(prepared)
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        attempt = 'att-verify'
        args = SimpleNamespace(
            route_file=str(path), route_node='run-verify', attempt_id=attempt,
            jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
            parent_slug='fixture-owner', parent_attempt_id=owner_attempt,
            parent_harness='codex', parent_transport='headless',
            parent_sandbox='workspace-write', subsession_id=None,
        )
        marker_path = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
        before = ledger.journal_path.read_bytes()
        with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
             mock.patch('subprocess.check_output', return_value=str(self.base)):
            self.assertIsNone(RA.completion_gate(
                args, 'register', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
            self.assertFalse(marker_path.exists())
            self.assertEqual(ledger.journal_path.read_bytes(), before)
            refused = RA.completion_gate(
                args, 'dry-run', ROOT, jobs, gate=DC.completion_marker_gate,
            )
            self.assertIsNotNone(refused)
            self.assertFalse(marker_path.exists())
            self.assertEqual(ledger.journal_path.read_bytes(), before)
            import dispatch_owner_input as INPUT
            queued = INPUT.submit(jobs, owner_attempt, 'apply the pending correction first', 'pending')
            self.assertEqual(queued['requests'][0]['state'], 'queued')
            blocked = RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            )
            self.assertIsNotNone(blocked)
            self.assertEqual(blocked[0], 'resource-owner-stage-admission-blocked')
            self.assertFalse(marker_path.exists())
            owner_state = INPUT._path(jobs, owner_attempt)
            correction_state = json.loads(owner_state.read_text())
            correction_state['requests'][0]['state'] = 'delivered'
            owner_state.write_text(json.dumps(correction_state))
            self.assertIsNone(RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
            marker_bytes = marker_path.read_bytes()
            self.assertIsNone(RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
        self.assertEqual(producer.read_bytes(), prepared)
        self.assertEqual(json.loads(marker_bytes)['resource_execution_identity'],
                         ledger.state()['nodes']['full-run']['evidence']['identity'])
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual(marker_path.read_bytes(), marker_bytes)

    def test_old_poll_false_success_and_unstarted_claim_recover_on_same_route(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        producer = output / 'run.json'
        producer.write_bytes(b'{"state":"prepared","runs":[{"status":"prepared"}]}\n')
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        polled = SUP.poll_once(route, ledger)[0]
        self.assertEqual(polled['action'], 'wait-next-resource')
        armed = SUP.read_armed(ledger)['full-run']
        evidence = dict(polled['evidence'])
        evidence.pop('awaiting_next_resource', None)
        evidence.pop('runtime_only_output', None)
        marker_path = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
        marker = SUP.route_module().write_completion_marker(
            route, WS.route_node(route, 'full-run'), 'full-run', producer, jobs=jobs,
        )
        ledger.record('full-run', 'STAGE_SUCCEEDED', evidence=evidence, actor='poll')
        old_claims = SUP._claim_successors(route, ledger, armed, 'full-run',
                                           armed['successors'], evidence)
        self.assertTrue(old_claims[0]['created'])
        old_marker_bytes = marker_path.read_bytes()
        old_claim_key = old_claims[0]['claim']
        old_claim_bytes = ledger.claim_path(old_claim_key).read_bytes()

        failed_next = self.next_body(registry, 'validation__a2', output)
        prior_row = json.loads(registry.read_text())['runs']['fixture-run']
        failed_next.update(
            parent_attempt_id=owner_attempt, owner_wait=dict(prior_row['owner_wait']),
            started_at=float(prior_row['ended_at']) + 1.0, status='failed',
            workflow_state='FAILED_RETRYABLE', failure_class='resource-launch-incomplete',
            launch_state='not-started', exit_code=None, ended_at=time.time(),
        )
        data = json.loads(registry.read_text())
        data['runs'][failed_next['run_id']] = failed_next
        registry.write_text(json.dumps(data))

        with ledger.lock():
            self.assertTrue(SUP.reconcile_legacy_supervised_resource_success(route, ledger, armed))
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(ledger.claim_history()[old_claim_key]['successor'], 'run-verify')
        self.assertEqual(ledger.claim_path(old_claim_key).read_bytes(), old_claim_bytes)
        self.assertEqual(marker_path.read_bytes(), old_marker_bytes)
        self.assertFalse(DC.completion_marker_is_current(
            route, WS.route_node(route, 'full-run'), marker_path, marker, jobs=jobs,
        ))

        actual = self.next_body(registry, 'full-run__a3', output)
        processes, receipt = self.launch(route, path, jobs, registry, output, actual)
        self.assertEqual(len(processes), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], prior_row)
        self.assertEqual(producer.read_bytes(), b'{"state":"prepared","runs":[{"status":"prepared"}]}\n')
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        args = SimpleNamespace(
            route_file=str(path), route_node='run-verify', attempt_id='att-verify',
            jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
            parent_slug='fixture-owner', parent_attempt_id=owner_attempt,
            parent_harness='codex', parent_transport='headless',
            parent_sandbox='workspace-write', subsession_id=None,
        )
        with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
             mock.patch('subprocess.check_output', return_value=str(self.base)):
            self.assertIsNone(RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
        current_marker = json.loads(marker_path.read_text())
        self.assertGreater(current_marker['sequence'], marker['sequence'])
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual(ledger.claim_path(old_claim_key).read_bytes(), old_claim_bytes)
        self.assertEqual(len(ledger.claim_history()), 2)
        self.assertTrue(DC.completion_marker_is_current(
            route, WS.route_node(route, 'full-run'), marker_path, current_marker, jobs=jobs,
        ))

    def test_owner_successor_start_retries_marker_journal_and_claim_crash_boundaries(self):
        for crash_after in ('marker', 'journal', 'claim'):
            with self.subTest(crash_after=crash_after):
                case = ResourceSequenceTest()
                case.setUp()
                try:
                    route, path, jobs, registry, output, ledger = case.fixture()
                    (output / 'run.json').write_bytes(b'{"state":"done"}\n')
                    case.live_owner(route, jobs, registry)
                    args = SimpleNamespace(
                        route_file=str(path), route_node='run-verify', attempt_id='att-verify',
                        jobs=str(jobs), agent_home=ROOT, worktree=str(case.base), dispatch_depth=2,
                        parent_slug='fixture-owner', parent_attempt_id='att-owner',
                        parent_harness='codex', parent_transport='headless',
                        parent_sandbox='workspace-write', subsession_id=None,
                    )
                    marker_path = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
                    record = WS.WorkflowLedger.record
                    claim = WS.WorkflowLedger.claim
                    def interrupted_record(instance, node, state, **kwargs):
                        if crash_after == 'marker' and node == 'full-run' and state == 'STAGE_SUCCEEDED':
                            raise OSError('crash after marker before journal')
                        result = record(instance, node, state, **kwargs)
                        if crash_after == 'journal' and node == 'full-run' and state == 'STAGE_SUCCEEDED':
                            raise OSError('crash after stage journal')
                        return result
                    def interrupted_claim(instance, key, payload):
                        result = claim(instance, key, payload)
                        if crash_after == 'claim' and payload.get('predecessor') == 'full-run':
                            raise OSError('crash after exclusive successor claim')
                        return result
                    with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                         mock.patch('subprocess.check_output', return_value=str(case.base)), \
                         mock.patch.object(WS.WorkflowLedger, 'record', interrupted_record), \
                         mock.patch.object(WS.WorkflowLedger, 'claim', interrupted_claim):
                        refused = RA.completion_gate(
                            args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                        )
                        self.assertIsNotNone(refused)
                        self.assertEqual(refused[0], 'resource-owner-start-authority-unavailable')
                    self.assertTrue(marker_path.is_file())
                    marker_bytes = marker_path.read_bytes()
                    claim_files = {item.name: item.read_bytes() for item in ledger.claims_dir.glob('*.claim')}
                    if crash_after == 'claim':
                        self.assertEqual(len(claim_files), 1)
                    else:
                        self.assertEqual(claim_files, {})
                    with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                         mock.patch('subprocess.check_output', return_value=str(case.base)):
                        self.assertIsNone(RA.completion_gate(
                            args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
                        ))
                    self.assertEqual(marker_path.read_bytes(), marker_bytes)
                    claim_files_after = {
                        item.name: item.read_bytes() for item in ledger.claims_dir.glob('*.claim')
                    }
                    self.assertEqual(len(claim_files_after), 1)
                    if crash_after == 'claim':
                        self.assertEqual(claim_files_after, claim_files)
                    self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
                    self.assertEqual(len(ledger.claim_history()), 1)
                    self.assertEqual(ledger.state()['workflow_state'], 'NEXT_REGISTERED')
                finally:
                    case.doCleanups()

    def test_three_sequential_runs_and_old_replay_preserve_history_then_advance_once(self):
        import dispatch_owner_input as INPUT
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        old = json.loads(registry.read_text())['runs']['fixture-run']
        old_sentinel = Path(old['sentinel']).read_bytes()
        first = self.next_body(registry, 'middle', output)
        procs, receipt = self.launch(route, path, jobs, registry, output, first)
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        first_armed = SUP.read_armed(ledger)['full-run']
        procs, receipt = self.launch(route, path, jobs, registry, output, first)
        self.assertEqual(procs, [])
        self.assertTrue(receipt['replayed'])
        self.assertEqual(SUP.read_armed(ledger)['full-run'], first_armed)
        final = self.next_body(registry, 'final', output, final=True)
        procs, receipt = self.launch(route, path, jobs, registry, output, final)
        self.assertEqual(len(procs), 1)
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)
        self.assertEqual(Path(old['sentinel']).read_bytes(), old_sentinel)
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertEqual(ledger.claims(), {})
        owner_attempt, _owner_input = self.live_owner(route, jobs, registry)
        args = SimpleNamespace(
            route_file=str(path), route_node='run-verify', attempt_id='att-verify',
            jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
            parent_slug='fixture-owner', parent_attempt_id=owner_attempt,
            parent_harness='codex', parent_transport='headless',
            parent_sandbox='workspace-write', subsession_id=None,
        )
        with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
             mock.patch('subprocess.check_output', return_value=str(self.base)):
            self.assertIsNone(RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
            marker_bytes = (jobs.parent / 'completion' / route['route_id'] / 'full-run.json').read_bytes()
            self.assertIsNone(RA.completion_gate(
                args, 'start', ROOT, jobs, gate=DC.completion_marker_gate,
            ))
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual((jobs.parent / 'completion' / route['route_id'] / 'full-run.json').read_bytes(), marker_bytes)
        self.assertEqual(json.loads((output / 'run.json').read_text()), {"phase":"final"})
        self.assertNotEqual(ledger.state()['workflow_state'], 'COMPLETE')
        final_armed = SUP.read_armed(ledger)['full-run']
        procs, receipt = self.launch(route, path, jobs, registry, output, first)
        self.assertEqual(procs, [])
        self.assertTrue(receipt['replayed'])
        self.assertEqual(SUP.read_armed(ledger)['full-run'], final_armed)

    def test_watch_armed_before_node_scope_keys_admits_next_run_and_replays(self):
        # f7 2026-10-17: a watch armed by a release before write_scope/part_io existed refused
        # the next run (`__a2`) after an update as resource-watch-binding-conflict.
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        arm_file = ledger.root / 'armed/full-run.json'
        def strip():
            legacy = json.loads(arm_file.read_text())
            for key in ('write_scope', 'part_io'):
                legacy.pop(key, None)
            arm_file.write_text(json.dumps(legacy))
            return legacy
        legacy = strip()
        body = self.next_body(registry, 'next', output)
        procs, receipt = self.launch(route, path, jobs, registry, output, body)
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        armed = SUP.read_armed(ledger)['full-run']
        self.assertEqual(armed['predecessor_id'], 'next')
        self.assertTrue('write_scope' in armed and 'part_io' in armed)
        self.assertTrue(any((entry.get('evidence') or {}).get('previous_resource') == legacy
                            for entry in ledger.journal()))
        legacy = strip()
        procs, receipt = self.launch(route, path, jobs, registry, output, body)
        self.assertEqual(procs, [])
        self.assertTrue(receipt['replayed'])
        self.assertEqual(SUP.read_armed(ledger)['full-run'], legacy)

    def test_new_registry_can_follow_but_live_or_unverifiable_prior_cannot(self):
        for invalid in (None, 'live', 'reused', 'sentinel', 'failed'):
            with self.subTest(invalid=invalid), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                SUP.poll_once(route, ledger)
                body = self.next_body(registry, 'next', output)
                old = json.loads(registry.read_text())['runs']['fixture-run']
                if invalid == 'live':
                    old.update(SUP.RR.proc_identity(os.getpid()), status='running')
                elif invalid == 'reused':
                    old.update(SUP.RR.proc_identity(os.getpid()), starttime='wrong')
                elif invalid == 'sentinel':
                    Path(old['sentinel']).write_text('7')
                elif invalid == 'failed':
                    old.update(status='failed', exit_code=7)
                if invalid:
                    registry.write_text(json.dumps({'schema_version': 1, 'runs': {old['run_id']: old}}))
                target = self.base / 'other-registry.json'
                target.write_text(json.dumps({'schema_version': 1, 'runs': {body['run_id']: body}}))
                prior_arm = SUP.read_armed(ledger)['full-run']
                if invalid:
                    with self.assertRaisesRegex(ValueError, 'resource-watch-binding-conflict'):
                        self.arm(path, target, run_id=body['run_id'], extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                    self.assertEqual(SUP.read_armed(ledger)['full-run'], prior_arm)
                else:
                    self.arm(path, target, run_id=body['run_id'], extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                    self.assertEqual(SUP.read_armed(ledger)['full-run']['resource_registry'], str(target))

    @contextlib.contextmanager
    def subfixture(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=self.base) as directory, mock.patch.object(self, 'base', Path(directory)):
            yield

    @contextlib.contextmanager
    def retained_subfixture(self):
        """Retire owner leases before removing their fixture directory."""
        import tempfile
        directory = tempfile.TemporaryDirectory(dir=self.base)
        self.addCleanup(directory.cleanup)
        with mock.patch.object(self, 'base', Path(directory.name)):
            yield

    def test_existing_missing_output_failure_can_follow_but_other_failure_cannot(self):
        for other in (False, True):
            with self.subTest(other=other), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                evidence = SUP.poll_once(route, ledger)[0]['evidence']
                evidence.pop('awaiting_next_resource', None)
                evidence['artifacts'] = {'checked': True, 'reason': 'declared-artifact-missing',
                                         'missing': ['run.json']}
                ledger.record('full-run', 'FAILED_RETRYABLE', evidence=evidence, actor='old-watch')
                ledger.set_workflow_state('FAILED_RETRYABLE', evidence={'node': 'full-run'}, actor='old-watch')
                if other:
                    ledger.record('run-verify', 'FAILED_TERMINAL', evidence={'reason': 'real-failure'})
                body = self.next_body(registry, 'next', output)
                if other:
                    self.launch(route, path, jobs, registry, output, body)
                    failed = json.loads(registry.read_text())['runs']['next']
                    self.assertEqual(failed['launch_error']['message'], 'resource-watch-binding-conflict')
                    self.assertTrue(SUP.RR.resource_never_started(failed))
                    self.assertEqual(ledger.state()['nodes']['run-verify']['state'], 'FAILED_TERMINAL')
                else:
                    self.launch(route, path, jobs, registry, output, body)
                    self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
                    self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')

    def test_parent_close_blocks_registration_without_changing_prior_binding(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        body = self.next_body(registry, 'next', output)
        ledger.record('full-run', 'RUNNING', evidence={'parent_close': {'preserve_resource': True}})
        before = registry.read_bytes(), SUP.read_armed(ledger)
        with self.assertRaisesRegex(ValueError, 'resource-parent-close-requested'):
            self.launch(route, path, jobs, registry, output, body)
        self.assertEqual((registry.read_bytes(), SUP.read_armed(ledger)), before)

    def test_same_registry_live_reused_inconsistent_failure_and_reused_log_refuse_before_reservation(self):
        for invalid in ('live', 'reused', 'failed', 'log'):
            with self.subTest(invalid=invalid), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                SUP.poll_once(route, ledger)
                body = self.next_body(registry, 'next', output)
                old = json.loads(registry.read_text())['runs']['fixture-run']
                if invalid in ('live', 'reused'):
                    old.update(SUP.RR.proc_identity(os.getpid()), status='running')
                    if invalid == 'reused':
                        old['starttime'] = 'wrong'
                elif invalid == 'failed':
                    old['status'] = 'failed'
                else:
                    body.update(log=old['log'], sentinel=old['sentinel'])
                registry.write_text(json.dumps({'schema_version': 1, 'runs': {old['run_id']: old}}))
                before = registry.read_bytes(), SUP.read_armed(ledger)
                with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
                    self.launch(route, path, jobs, registry, output, body)
                self.assertEqual((registry.read_bytes(), SUP.read_armed(ledger)), before)

    def test_concurrent_different_registries_admit_only_one_next_predecessor(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        def arm_next(name):
            body = self.next_body(registry, name, output)
            target = self.base / (name + '-registry.json')
            target.write_text(json.dumps({'schema_version': 1, 'runs': {name: body}}))
            try:
                self.arm(path, target, run_id=name, extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(arm_next, ['next-a', 'next-b']))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(ledger.claims(), {})

    def test_intermediate_controller_receipt_resumes_same_owner_without_successors(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        armed = SUP.read_armed(ledger)['full-run']
        row = json.loads(registry.read_text())['runs']['fixture-run']
        args = SimpleNamespace(parent_attempt_id='att-parent', route_id=route['route_id'], route_hash=route['route_hash'], jobs=str(jobs))
        control = SimpleNamespace(thread_id='same-native', pending=lambda: False)
        state = self.base / 'state.json'
        WAIT.JOIN.write_supervisor_state(state, 'att-parent', set(), phase='running-turn')
        context = (SUP, route, ledger, [(armed, row)])
        with mock.patch.object(WAIT, 'context', return_value=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP):
            prompt = WAIT.wait(args, state, control, set(), lambda _: None,
                               sleep=lambda _: self.fail('intermediate result must return without another wait'))
        receipt = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource['outbox']['receipt']
        self.assertEqual(receipt['state'], 'succeeded')
        self.assertEqual(receipt['reason'], 'awaiting-next-resource')
        self.assertFalse(receipt['verification_pass'])
        self.assertEqual(receipt['successors'], [])
        self.assertIn('same-native', prompt)

    def test_late_final_output_resolves_running_wait_without_claiming_successor(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        original = registry.read_bytes()
        (output / 'run.json').write_text('{"phase":"final"}')
        with ledger.lock():
            SUP.reconcile_resource_artifacts(route, ledger, 'att-parent', jobs)
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(registry.read_bytes(), original)

    def test_two_real_resources_without_owner_run_record_settle_full_run(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        first_record = json.loads((output / 'run.json').read_text())['hearting_resource_runs']['runs']['fixture-run']
        for name in ('arm-t', 'arm-b'):
            body = self.next_body(registry, name, output)
            wrong = output / 'experiments' / name / 'run.json'
            body['command'] = [sys.executable, '-c',
                f'from pathlib import Path; p=Path({str(wrong)!r}); p.parent.mkdir(parents=True); '
                'p.write_text(\'{"metric": 1}\')']
            procs, receipt = self.launch(route, path, jobs, registry, output, body)
            self.assertEqual(len(procs), 1)
            self.assertEqual(receipt['runtime_output'], str(output / 'run.json'))
            self.assertIn(str(output / 'run.json'), receipt['expected_outputs'])
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        doc = json.loads((output / 'run.json').read_text())['hearting_resource_runs']['runs']
        self.assertEqual(set(doc), {'fixture-run', 'arm-t', 'arm-b'})
        self.assertEqual(doc['fixture-run'], first_record)
        self.assertEqual([doc[name]['exit_code'] for name in ('arm-t', 'arm-b')], [0, 0])
        registry_bytes = registry.read_bytes()
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, 'completed')
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual(registry.read_bytes(), registry_bytes)
        self.assertEqual(ledger.claims(), {})

    def test_v3532_flat_reader_keeps_fix_bytes_and_var_receipt_is_independent(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        identity = SUP.RR.proc_identity(os.getpid())
        data = json.loads(registry.read_text())
        data['runs']['fixture-run'].update(identity, process_group=os.getpgrp(),
            pid_namespace=os.readlink('/proc/self/ns/pid'), status='running', exit_code=None)
        registry.write_text(json.dumps(data))
        primary_path = ledger.root / 'armed' / 'full-run.json'
        primary_bytes = primary_path.read_bytes()
        old_fix = json.loads(registry.read_text())['runs']['fixture-run']
        body = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
                                    'var-run', output, scope='var run; preserve running fix',
                                    payload_code='pass')
        processes, _receipt = self.launch(route, path, jobs, registry, output, body)
        self.assertEqual(len(processes), 1)
        self.assertEqual(primary_path.read_bytes(), primary_bytes)
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old_fix)

        self.assertEqual(hashlib.sha256(V3532_READ_ARMED_SOURCE.encode()).hexdigest(),
                         V3532_READ_ARMED_SHA256)
        namespace = {'json': json, 'Path': Path,
                     'armed_dir': lambda current: current.root / 'armed'}
        exec(compile(V3532_READ_ARMED_SOURCE, 'v3.53.2-workflow-supervisor.py', 'exec'), namespace)
        old_view = namespace['read_armed'](ledger)
        self.assertEqual(old_view['full-run'], json.loads(primary_bytes))
        current = SUP.read_resource_watches(ledger, 'full-run')
        self.assertEqual({row['predecessor_id'] for row in current}, {'fixture-run', 'var-run'})

        phase = self.base / 'owner-state.json'
        WAIT.JOIN.write_supervisor_state(phase, owner_attempt, set(), phase='running-turn')
        control = SimpleNamespace(thread_id='same-native', pending=lambda: False)
        args = SimpleNamespace(parent_attempt_id=owner_attempt, route_file=str(path),
            route_id=route['route_id'], route_hash=route['route_hash'], jobs=str(jobs))
        pairs = [(armed, json.loads(Path(armed['resource_registry']).read_text())['runs'][armed['predecessor_id']])
                 for armed in current]
        var_armed, var_row = next((armed, row) for armed, row in pairs
                                  if row['run_id'] == 'var-run')
        var_key = WAIT.resource_key(var_row)
        actual_resource_evidence = SUP.resource_evidence

        def temporarily_unknown(armed):
            if armed['predecessor_id'] == 'var-run':
                return {'terminal': False, 'reason': 'fixture-observation-unknown',
                        'liveness': 'unknown'}
            return actual_resource_evidence(armed)

        with mock.patch.object(WAIT, 'context', return_value=(SUP, route, ledger, pairs)), \
             mock.patch.object(SUP, 'resource_evidence', side_effect=temporarily_unknown), \
             mock.patch.object(SUP, 'poll_once'):
            prompt = WAIT.wait(args, phase, control, set(), lambda _event: None,
                              sleep=lambda _seconds: self.fail('unknown var observation must return attention'))
        state = WAIT.JOIN.read_supervisor_phase_state(phase, owner_attempt)
        attention = state.resource['outbox']['receipt']
        self.assertIn('var-run', prompt)
        self.assertEqual((attention['run_id'], attention['state']), ('var-run', 'needs-attention'))
        self.assertEqual(attention['reason'], WAIT.NONTERMINAL_RESOURCE_REASON)
        self.assertFalse(attention['verification_pass'])
        attention_id = WAIT.resource_attention_delivery_key(var_key)
        self.assertTrue(WAIT.acknowledge(phase, owner_attempt, state.resource['outbox']['receipt_id']))
        state = WAIT.JOIN.read_supervisor_phase_state(phase, owner_attempt)
        self.assertEqual(state.resource['delivered'], [attention_id])
        self.assertNotIn(var_key, state.resource['delivered'])

        # ACKing attention leaves the live aggregate closed; it does not consume
        # this execution's resource_key or claim its successor.
        with mock.patch.object(SUP, 'resource_evidence', side_effect=temporarily_unknown):
            with self.assertRaisesRegex(SUP.SupervisorError, 'resource-owner-operation-unsettled'):
                SUP.admit_owner_successor_start(route, 'run-verify', jobs, owner_attempt, 'same-native')
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertEqual(ledger.claims(), {})

        class WaitPaused(Exception):
            pass

        with mock.patch.object(WAIT, 'context', return_value=(SUP, route, ledger, pairs)), \
             mock.patch.object(SUP, 'resource_evidence', side_effect=temporarily_unknown), \
             mock.patch.object(SUP, 'poll_once'):
            with self.assertRaises(WaitPaused):
                WAIT.wait(args, phase, control, set(), lambda _event: None,
                          sleep=lambda _seconds: (_ for _ in ()).throw(WaitPaused()))
        state = WAIT.JOIN.read_supervisor_phase_state(phase, owner_attempt)
        self.assertIsNone(state.resource['outbox'])
        self.assertEqual(state.resource['delivered'], [attention_id])

        # The same bound execution later has real terminal/quiescent evidence.
        terminal_var_evidence = actual_resource_evidence(var_armed)
        self.assertTrue(terminal_var_evidence['terminal'])
        self.assertTrue(terminal_var_evidence['quiescent'])

        def restored_observation(armed):
            if armed['predecessor_id'] == 'var-run':
                return terminal_var_evidence
            return actual_resource_evidence(armed)

        with mock.patch.object(WAIT, 'context', return_value=(SUP, route, ledger, pairs)), \
             mock.patch.object(SUP, 'resource_evidence', side_effect=restored_observation), \
             mock.patch.object(SUP, 'poll_once'):
            prompt = WAIT.wait(args, phase, control, set(), lambda _event: None,
                              sleep=lambda _seconds: self.fail('terminal var receipt must return immediately'))
        receipt = WAIT.JOIN.read_supervisor_phase_state(phase, owner_attempt).resource['outbox']['receipt']
        self.assertIn('var-run', prompt)
        self.assertEqual((receipt['run_id'], receipt['state']), ('var-run', 'succeeded'))
        self.assertEqual(receipt['resource_key'], var_key)
        self.assertFalse(receipt['verification_pass'])
        self.assertTrue(WAIT.acknowledge(phase, owner_attempt,
                                         WAIT.JOIN.read_supervisor_phase_state(phase, owner_attempt)
                                         .resource['outbox']['receipt_id']))
        self.assertIn(var_key, WAIT.JOIN.read_supervisor_phase_state(
            phase, owner_attempt).resource['delivered'])
        with self.assertRaisesRegex(SUP.SupervisorError, 'resource-owner-operation-unsettled'):
            SUP.admit_owner_successor_start(route, 'run-verify', jobs, owner_attempt, 'same-native')
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertEqual(ledger.claims(), {})

    def test_two_approved_cpu_operations_run_together_then_admit_once(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(owner_harness=harness), self.subfixture():
                self._exercise_two_approved_operations(harness)

    def _exercise_two_approved_operations(self, harness):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry, harness=harness)
        release_a, release_b = self.base / 'a.release', self.base / 'b.release'
        barrier = ('import sys,time\nfrom pathlib import Path\np=Path(sys.argv[1])\n'
                   'while not p.exists(): time.sleep(.01)\n')
        body_a = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
            'operation-a', output, scope='authorized operation A',
            payload_code=barrier, payload_args=[str(release_a)])
        body_b = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
            'operation-b', output, scope='authorized operation B',
            payload_code=barrier, payload_args=[str(release_b)])
        data = json.loads(registry.read_text())
        data['runs'].pop('fixture-run')
        registry.write_text(json.dumps(data))
        (ledger.root / 'armed' / 'full-run.json').unlink()
        processes = []
        try:
            first, receipt_a = self.launch(route, path, jobs, registry, output, body_a, wait_for_exit=False)
            processes.extend(first)
            second, receipt_b = self.launch(route, path, jobs, registry, output, body_b, wait_for_exit=False)
            processes.extend(second)
            self.assertTrue(all(proc.poll() is None for proc in processes))
            watches = SUP.read_resource_watches(ledger, 'full-run')
            self.assertEqual({row['predecessor_id'] for row in watches}, {'operation-a', 'operation-b'})
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-resource-operation')
            self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
            self.assertEqual(ledger.claims(), {})

            release_a.write_text('go')
            release_b.write_text('go')
            for proc in processes:
                self.assertEqual(proc.wait(timeout=5), 0)
            SUP.poll_once(route, ledger)
            runtime_runs = json.loads((output / 'run.json').read_text())['hearting_resource_runs']['runs']
            self.assertEqual(set(runtime_runs), {'operation-a', 'operation-b'})
            self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
            self.assertEqual(ledger.claims(), {})

            self.assertTrue(SUP.admit_owner_successor_start(
                route, 'run-verify', jobs, owner_attempt, 'same-native'))
            stage = ledger.state()['nodes']['full-run']
            self.assertEqual(stage['state'], 'STAGE_SUCCEEDED')
            self.assertEqual({row['run_id'] for row in stage['evidence']['resource_operations']},
                             {'operation-a', 'operation-b'})
            self.assertEqual(len(ledger.claims()), 1)
            self.assertEqual(len(ledger.claim_history()), 1)
        finally:
            for release in (release_a, release_b):
                release.write_text('cleanup')
            for proc in processes:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)

    def test_sibling_pass_does_not_erase_an_unresolved_operation_failure(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        release_a, release_b = self.base / 'fail.release', self.base / 'pass.release'
        barrier_fail = ('import sys,time\nfrom pathlib import Path\np=Path(sys.argv[1])\n'
                        'while not p.exists(): time.sleep(.01)\nsys.exit(7)\n')
        barrier_pass = ('import sys,time\nfrom pathlib import Path\np=Path(sys.argv[1])\n'
                        'while not p.exists(): time.sleep(.01)\n')
        failed = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
            'operation-fail', output, scope='authorized failing operation',
            payload_code=barrier_fail, payload_args=[str(release_a)])
        passed = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
            'operation-pass', output, scope='authorized successful operation',
            payload_code=barrier_pass, payload_args=[str(release_b)])
        data = json.loads(registry.read_text())
        data['runs'].pop('fixture-run')
        registry.write_text(json.dumps(data))
        (ledger.root / 'armed' / 'full-run.json').unlink()
        processes = []
        try:
            first, _ = self.launch(route, path, jobs, registry, output, failed, wait_for_exit=False)
            second, _ = self.launch(route, path, jobs, registry, output, passed, wait_for_exit=False)
            processes.extend(first + second)
            release_a.write_text('go')
            release_b.write_text('go')
            self.assertEqual(first[0].wait(timeout=5), 7)
            self.assertEqual(second[0].wait(timeout=5), 0)
            observations = SUP.poll_once(route, ledger)
            self.assertEqual(len(observations), 2)
            self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
            current = json.loads(registry.read_text())['runs']
            self.assertEqual(current['operation-fail']['exit_code'], 7)
            self.assertEqual(current['operation-pass']['exit_code'], 0)
            with self.assertRaisesRegex(SUP.SupervisorError, 'resource-owner-operation-unsettled'):
                SUP.admit_owner_successor_start(route, 'run-verify', jobs, owner_attempt, 'same-native')
            self.assertEqual(ledger.claims(), {})
            self.assertFalse((jobs.parent / 'completion' / route['route_id'] / 'full-run.json').exists())
        finally:
            for release in (release_a, release_b):
                release.write_text('cleanup')
            for proc in processes:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)

    def test_public_start_with_live_leased_owner_runs_cpu_then_uses_existing_successor_gate(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, _owner_input = self.live_owner(route, jobs, registry, harness='claude')
        import dispatch_contract as CONTRACT
        state_path = CONTRACT.dispatch_state_root(jobs) / 'supervisor-state' / f'{owner_attempt}.json'
        WAIT.JOIN.write_supervisor_state(state_path, owner_attempt, set(), phase='running-turn')
        smoke = self.base / 'public-smoke.json'
        smoke.write_text('{"fixture":true}\n')
        command = [sys.executable, '-c', 'print("public current cpu launch")']
        import artifact_producer
        runner = SUP.runner()
        payloads = []
        real_popen = subprocess.Popen
        def capture_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            command_line = args[0] if args else kwargs.get('args')
            if isinstance(command_line, list) and command_line[:2] == ['/bin/bash', '-p']:
                payloads.append(proc)
            return proc
        env = {
            'CUDA_VISIBLE_DEVICES': '',
            'AGENT_DISPATCH_COMPLETION_MODE': 'supervised',
            'AGENT_DISPATCH_ATTEMPT_ID': owner_attempt,
            'AGENT_OWNER_ROUTE_FILE': str(path),
            'AGENT_DISPATCH_JOBS': str(jobs),
            'AGENT_DISPATCH_COMPLETION_STATE_FILE': str(state_path),
        }
        log = self.base / 'public-current.log'
        argv = ['--registry', str(registry), 'start', '--run-id', 'public-current-run',
            '--cwd', str(self.base), '--log', str(log), '--route', str(path),
            '--node', 'full-run', '--smoke-attestation', str(smoke),
            '--parent-attempt-id', owner_attempt, '--jobs', str(jobs), '--', *command]
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(runner, 'verify_launch_input'), \
                 mock.patch.object(artifact_producer, 'prepare_route_artifact_env',
                    return_value={'AGENT_ARTIFACT_OUTPUT_DIR': str(output)}), \
                 mock.patch.object(runner, 'register_registry'), \
                 mock.patch.object(runner, 'start_watch',
                    return_value=(mock.Mock(poll=lambda: None), SUP.RR.proc_identity(os.getpid()))), \
                 mock.patch.object(runner.subprocess, 'Popen', side_effect=capture_popen), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout:
                runner.main(argv)
            self.assertEqual(len(payloads), 1)
            receipt = json.loads(stdout.getvalue().splitlines()[-1])
            self.assertEqual(receipt['status'], 'running')
            self.assertEqual(receipt['pid'], payloads[0].pid)
            self.assertEqual(receipt['parent_attempt_id'], owner_attempt)
            self.assertEqual(payloads[0].wait(timeout=5), 0)
            result = SUP.poll_once(route, ledger)
            self.assertEqual(result[0]['action'], 'wait-next-resource')
            self.assertTrue(SUP.admit_owner_successor_start(
                route, 'run-verify', jobs, owner_attempt, 'same-native'))
            self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
            self.assertEqual(len(ledger.claims()), 1)
        finally:
            for proc in payloads:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)

    def test_codex_public_intent_arms_without_tool_observation_and_controller_releases_once(self):
        """Exercise public intent, real arm/discovery/claim, and bounded refusals."""
        import artifact_producer
        import resource_run_registry as RR

        def recorded_failure(ledger, prior):
            evidence = {
                'identity': f"{prior['run_id']}:{prior.get('pid')}:{prior.get('starttime')}:{prior.get('exit_code')}",
                'succeeded': False,
                'exit_code': prior.get('exit_code'),
                'resource_sha256': SUP.RESOURCE_RESUME.row_digest(prior),
            }
            ledger.record('full-run', 'FAILED_RETRYABLE', evidence=evidence,
                          actor='fixture-recorded-prior-failure')
            ledger.set_workflow_state('FAILED_RETRYABLE', evidence={'node': 'full-run'},
                                      actor='fixture-recorded-prior-failure')

        def make_world(*, live_group=False, contradictory=False, claimed_crash=False):
            route, path, jobs, registry, output, ledger = self.fixture()
            owner_attempt, owner_input = self.live_owner(route, jobs, registry, harness='codex')
            live_proc = None
            identity = None
            if live_group:
                # The group leader exits while its forked child remains in the group.
                code = 'import os,time\nchild=os.fork()\nif child: os._exit(0)\ntime.sleep(120)\n'
                live_proc = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
                identity = RR.proc_identity(live_proc.pid)
                self.assertIsNotNone(identity)
                live_proc.wait(timeout=5)

                def stop_group(proc=live_proc):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGTERM)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=5)
                self.addCleanup(stop_group)

            data = json.loads(registry.read_text())
            prior = data['runs']['fixture-run']
            prior.update(status='failed', workflow_state='FAILED_RETRYABLE',
                         failure_class='resource-exit', ended_at=time.time(),
                         launch_state='started', exit_code=1,
                         pid=identity['pid'] if identity else 4_000_000_000,
                         starttime=identity['starttime'] if identity else '1',
                         process_group=identity['pid'] if identity else 4_000_000_000,
                         pid_namespace=os.readlink('/proc/self/ns/pid'))
            prior['parent_attempt_id'] = 'att-old-owner'
            prior['owner_wait'].update(parent_attempt_id='att-old-owner', session_id='old-native')
            sentinel = Path(prior['sentinel'])
            if claimed_crash:
                with contextlib.suppress(FileNotFoundError):
                    sentinel.unlink()
                prior.update(exit_code=None, launch_state='claimed',
                             launch_controller={'pid': 70001, 'starttime': 'old-claim'},
                             failure_class='resource-launch-incomplete')
            else:
                sentinel.write_text('0\n' if contradictory else '1\n')
            registry.write_text(json.dumps(data))
            armed_path = ledger.root / 'armed' / 'full-run.json'
            armed = json.loads(armed_path.read_bytes())
            armed['resource_binding'] = WAIT.resource_body_digest(prior)
            armed_path.write_text(json.dumps(armed))

            if live_group:
                self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-draining')
                recorded_failure(ledger, prior)
            elif contradictory or claimed_crash:
                recorded_failure(ledger, prior)
            else:
                self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'halt-failed')

            prior = json.loads(registry.read_text())['runs']['fixture-run']
            state_path = DC.dispatch_state_root(jobs) / 'supervisor-state' / f'{owner_attempt}.json'
            WAIT.JOIN.write_supervisor_state(state_path, owner_attempt, set(), phase='running-turn')
            env = dict(os.environ)
            for key in ('AGENT_DISPATCH_WORKER_TYPE', 'AGENT_DISPATCH_DEPTH',
                        'AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION', 'AGENT_DISPATCH_EXECUTION_SURFACE',
                        'AGENT_DISPATCH_REGISTERED_WORKER'):
                env.pop(key, None)
            env.update({
                'CUDA_VISIBLE_DEVICES': '',
                'AGENT_DISPATCH_COMPLETION_MODE': 'supervised',
                'AGENT_DISPATCH_ATTEMPT_ID': owner_attempt,
                'AGENT_OWNER_ROUTE_FILE': str(path),
                'AGENT_DISPATCH_JOBS': str(jobs),
                'AGENT_DISPATCH_COMPLETION_STATE_FILE': str(state_path),
            })
            smoke = self.base / 'public-smoke.json'
            smoke.write_text('{"fixture":true}\n')
            return SimpleNamespace(route=route, path=path, jobs=jobs, registry=registry,
                output=output, ledger=ledger, owner_attempt=owner_attempt, owner_input=owner_input,
                state_path=state_path, env=env, smoke=smoke, prior=copy.deepcopy(prior),
                prior_row_bytes=json.dumps(prior, sort_keys=True, separators=(',', ':')).encode(),
                sentinel_bytes=sentinel.read_bytes() if sentinel.exists() else None,
                live_proc=live_proc)

        def public_start(world, run_id, log_path=None, command=None, *, observe_ids=()):
            runner = SUP.runner()
            log_path = Path(log_path or (self.base / f'{run_id}.log'))
            command = command or [sys.executable, '-c', 'pass']
            argv = ['--registry', str(world.registry), 'start', '--run-id', run_id,
                    '--cwd', str(self.base), '--log', str(log_path), '--route', str(world.path),
                    '--node', 'full-run', '--smoke-attestation', str(world.smoke),
                    '--parent-attempt-id', world.owner_attempt, '--jobs', str(world.jobs), '--', *command]
            output = io.StringIO()
            payloads = []
            real_run, real_popen = runner.subprocess.run, subprocess.Popen
            real_readlink = os.readlink

            def arm_transport(argv, *args, **kwargs):
                command_argv = argv[0] if isinstance(argv, tuple) and len(argv) == 1 else argv
                if (isinstance(command_argv, list) and len(command_argv) > 2
                        and Path(command_argv[1]).name == 'workflow-supervisor.py'
                        and command_argv[2] == 'arm'):
                    # arm-transport stand-in; SUP.main executes the real arm implementation.
                    code = SUP.main(command_argv[2:])
                    result = subprocess.CompletedProcess(command_argv, code)
                    if kwargs.get('check') and code:
                        raise subprocess.CalledProcessError(code, command_argv)
                    return result
                return real_run(argv, *args, **kwargs)

            def capture_popen(*args, **kwargs):
                proc = real_popen(*args, **kwargs)
                command_argv = args[0] if args else kwargs.get('args')
                if isinstance(command_argv, list) and command_argv[:2] == ['/bin/bash', '-p']:
                    payloads.append(proc)
                return proc

            def tool_namespace(path_value):
                # tool-namespace stand-in applies to public intent and in-process arm only.
                if path_value == '/proc/self/ns/pid':
                    return 'pid:[codex-tool-sandbox-stand-in]'
                return real_readlink(path_value)

            error = None
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, world.env, clear=True))
                # source-proof stand-in: command and smoke verifiers are outside this fixture's scope.
                stack.enter_context(mock.patch.object(runner, 'verify_launch_input'))
                # artifact-env stand-in: keep route artifact setup inside the test fixture.
                stack.enter_context(mock.patch.object(artifact_producer, 'prepare_route_artifact_env',
                    return_value={'AGENT_ARTIFACT_OUTPUT_DIR': str(world.output)}))
                stack.enter_context(mock.patch.object(runner, 'register_registry'))
                # watch stand-in: the CPU payload and supervisor arm remain real.
                stack.enter_context(mock.patch.object(runner, 'start_watch', return_value=(
                    mock.Mock(poll=lambda: None), RR.proc_identity(os.getpid()))))
                stack.enter_context(mock.patch.object(runner.os, 'readlink', side_effect=tool_namespace))
                stack.enter_context(mock.patch.object(runner.subprocess, 'run', side_effect=arm_transport))
                stack.enter_context(mock.patch.object(runner.subprocess, 'Popen', side_effect=capture_popen))
                if observe_ids:
                    def track(name, row):
                        if isinstance(row, dict) and row.get('run_id') in observe_ids:
                            calls.append((name, row['run_id']))
                    settle = runner.settle
                    classify = RR.classify_identity
                    quiescent = RR.resource_process_group_quiescent

                    def spy_settle(registry_arg, run_id_arg, row_arg, *args, **kwargs):
                        track('settle', row_arg)
                        return settle(registry_arg, run_id_arg, row_arg, *args, **kwargs)

                    def spy_classify(row_arg, *args, **kwargs):
                        track('classify_identity', row_arg)
                        return classify(row_arg, *args, **kwargs)

                    def spy_quiescent(row_arg, *args, **kwargs):
                        track('resource_process_group_quiescent', row_arg)
                        return quiescent(row_arg, *args, **kwargs)

                    stack.enter_context(mock.patch.object(runner, 'settle', side_effect=spy_settle))
                    stack.enter_context(mock.patch.object(RR, 'classify_identity', side_effect=spy_classify))
                    stack.enter_context(mock.patch.object(RR, 'resource_process_group_quiescent',
                                                          side_effect=spy_quiescent))
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    try:
                        runner.main(argv)
                    except (Exception, SystemExit) as caught:
                        error = caught
            for proc in payloads:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=10)
            receipt = None
            for line in output.getvalue().splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get('run_id') == run_id:
                    receipt = value
            return SimpleNamespace(error=error, output=output.getvalue(), receipt=receipt,
                                   payloads=payloads, observation_calls=calls)

        def discover(world):
            args = SimpleNamespace(parent_attempt_id=world.owner_attempt,
                route_file=str(world.path), route_id=world.route['route_id'],
                route_hash=world.route['route_hash'], jobs=str(world.jobs),
                # command-builder stand-in labels a local CPU command for the existing controller.
                resource_launch_command=lambda row: (row['command'], 'fixture-local-cpu'))
            control = SimpleNamespace(thread_id='same-native', pending=lambda: False)
            return args, control, WAIT.context(args, control)

        def controller_release(world, *, found=None, unknown_prior=None):
            args, control, context_value = found if found is not None else discover(world)
            runner = WAIT.supervisor().runner()
            payloads = []
            output = io.StringIO()
            real_run, real_popen = runner.subprocess.run, subprocess.Popen

            def arm_transport(argv, *args, **kwargs):
                command_argv = argv[0] if isinstance(argv, tuple) and len(argv) == 1 else argv
                if (isinstance(command_argv, list) and len(command_argv) > 2
                        and Path(command_argv[1]).name == 'workflow-supervisor.py'
                        and command_argv[2] == 'arm'):
                    # arm-transport stand-in; the actual controller is still calling SUP.main.
                    code = SUP.main(command_argv[2:])
                    result = subprocess.CompletedProcess(command_argv, code)
                    if kwargs.get('check') and code:
                        raise subprocess.CalledProcessError(code, command_argv)
                    return result
                return real_run(argv, *args, **kwargs)

            def capture_popen(*args, **kwargs):
                proc = real_popen(*args, **kwargs)
                command_argv = args[0] if args else kwargs.get('args')
                if isinstance(command_argv, list) and command_argv[:2] == ['/bin/bash', '-p']:
                    payloads.append(proc)
                return proc

            real_quiescent = RR.resource_process_group_quiescent
            def unknown_observation(row, *args, **kwargs):
                # unknown-observation stand-in is confined to this controller claim.
                if row.get('run_id') == unknown_prior:
                    return False
                return real_quiescent(row, *args, **kwargs)

            error = None
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': ''}, clear=False))
                # source-proof stand-in for verifier subprocesses, not the payload.
                stack.enter_context(mock.patch.object(runner, 'verify_launch_input'))
                # artifact-env stand-in keeps only fixture artifact paths.
                stack.enter_context(mock.patch.object(artifact_producer, 'prepare_route_artifact_env',
                    return_value={'AGENT_ARTIFACT_OUTPUT_DIR': str(world.output)}))
                stack.enter_context(mock.patch.object(runner, 'register_registry'))
                # watch stand-in avoids a separate observer process.
                stack.enter_context(mock.patch.object(runner, 'start_watch', return_value=(
                    mock.Mock(poll=lambda: None), RR.proc_identity(os.getpid()))))
                stack.enter_context(mock.patch.object(runner.subprocess, 'run', side_effect=arm_transport))
                stack.enter_context(mock.patch.object(runner.subprocess, 'Popen', side_effect=capture_popen))
                if unknown_prior is not None:
                    stack.enter_context(mock.patch.object(RR, 'resource_process_group_quiescent',
                                                          side_effect=unknown_observation))
                try:
                    if context_value is None or not context_value[3]:
                        raise AssertionError('controller discovery returned no queued candidate')
                    candidates = [pair for pair in context_value[3] if WAIT.controller_intent(pair[1])]
                    if len(candidates) != 1:
                        raise AssertionError(f'expected one queued controller candidate, got '
                                             f'{[r[1]["run_id"] for r in candidates]}')
                    armed, row = candidates[0]
                    WAIT.admit_controller_launch(args, control, armed, row, [])
                except Exception as caught:
                    error = caught
            for proc in payloads:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=10)
            actual = json.loads(world.registry.read_text())['runs']
            return SimpleNamespace(error=error, payloads=payloads,
                row=actual.get(candidates[0][1]['run_id']) if context_value and context_value[3]
                    and candidates else None,
                context=context_value, output=output.getvalue())

        def assert_never_started(world, run_id, reason):
            row = json.loads(world.registry.read_text())['runs'][run_id]
            self.assertTrue(RR.resource_never_started(row), row)
            self.assertEqual(row.get('launch_state'), 'not-started')
            self.assertEqual(row.get('failure_class'), 'resource-launch-incomplete')
            self.assertIn(reason, row.get('launch_error', {}).get('message', ''))
            return row

        def assert_prior_unchanged(world):
            row = json.loads(world.registry.read_text())['runs']['fixture-run']
            self.assertEqual(json.dumps(row, sort_keys=True, separators=(',', ':')).encode(),
                             world.prior_row_bytes)
            sentinel = Path(world.prior['sentinel'])
            self.assertEqual(sentinel.read_bytes() if sentinel.exists() else None,
                             world.sentinel_bytes)

        # d1: public queue -> real in-process arm -> existing WAIT.context -> fenced host release.
        world = make_world()
        h0_bytes = world.prior_row_bytes
        h0_sentinel = world.sentinel_bytes
        calls = []
        intent = public_start(world, 'h0-a2', observe_ids={'fixture-run'})
        self.assertIsNone(intent.error, intent.output)
        self.assertEqual(intent.payloads, [])
        self.assertEqual(intent.observation_calls, [])
        queued = intent.receipt
        self.assertIsNotNone(queued, intent.output)
        self.assertEqual(queued['launch_state'], 'queued')
        self.assertFalse(queued['payload_spawned'])
        self.assertEqual(queued['required_action'], 'yield-owner-turn')
        self.assertEqual(SUP.read_armed(world.ledger)['full-run']['predecessor_id'], 'h0-a2')
        history = SUP.resource_predecessors(world.ledger, 'full-run')
        self.assertEqual({row['run_id'] for _, row in history}, {'fixture-run', 'h0-a2'})
        self.assertTrue(any(((entry.get('evidence') or {}).get('previous_resource') or {}).get(
            'predecessor_id') == 'fixture-run' for entry in world.ledger.journal()))
        assert_prior_unchanged(world)

        replay = public_start(world, 'h0-a2', observe_ids={'fixture-run'})
        self.assertIsNone(replay.error, replay.output)
        self.assertTrue(replay.receipt['replayed'])
        self.assertFalse(replay.receipt['payload_spawned'])
        self.assertEqual(replay.payloads, [])
        self.assertEqual(replay.observation_calls, [])
        queued_registry = world.registry.read_bytes()
        queued_arm = (world.ledger.root / 'armed' / 'full-run.json').read_bytes()
        queued_journal_len = len(world.ledger.journal())
        changed = public_start(world, 'h0-a2', command=[sys.executable, '-c', 'print("changed")'])
        self.assertIsNotNone(changed.error)
        self.assertIn('resource-route-body-conflict', str(changed.error) + changed.output)
        self.assertEqual(changed.payloads, [])
        self.assertEqual(world.registry.read_bytes(), queued_registry)
        self.assertEqual((world.ledger.root / 'armed' / 'full-run.json').read_bytes(), queued_arm)
        self.assertEqual(len(world.ledger.journal()), queued_journal_len)

        discovery = discover(world)
        self.assertEqual([row['run_id'] for _, row in discovery[2][3]], ['h0-a2'])
        released = controller_release(world, found=discovery)
        self.assertIsNone(released.error, released.output)
        self.assertEqual(len(released.payloads), 1)
        self.assertEqual(released.payloads[0].wait(timeout=10), 0)
        started = json.loads(world.registry.read_text())['runs']['h0-a2']
        self.assertEqual(started['launch_state'], 'started')
        self.assertTrue(started.get('launch_controller'))
        replay_args, replay_control, replay_context = discover(world)
        self.assertEqual(len(replay_context[3]), 1)
        WAIT.admit_controller_launch(replay_args, replay_control, *replay_context[3][0], [])
        self.assertEqual(len(released.payloads), 1)
        self.assertEqual(SUP.poll_once(world.route, world.ledger)[0]['action'], 'wait-next-resource')

        next_intent = public_start(world, 'h0-a3', observe_ids={'fixture-run', 'h0-a2'})
        self.assertIsNone(next_intent.error, next_intent.output)
        self.assertEqual(next_intent.receipt['launch_state'], 'queued')
        self.assertEqual(next_intent.payloads, [])
        self.assertEqual(next_intent.observation_calls, [])
        self.assertEqual(SUP.read_armed(world.ledger)['full-run']['predecessor_id'], 'h0-a3')
        next_discovery = discover(world)
        self.assertEqual([row['run_id'] for _, row in next_discovery[2][3]
                          if WAIT.controller_intent(row)], ['h0-a3'])
        next_release = controller_release(world, found=next_discovery)
        self.assertIsNone(next_release.error, next_release.output)
        self.assertEqual(len(next_release.payloads), 1)
        self.assertEqual(next_release.payloads[0].wait(timeout=10), 0)
        self.assertEqual(SUP.poll_once(world.route, world.ledger)[0]['action'], 'wait-next-resource')
        self.assertEqual(len(released.payloads) + len(next_release.payloads), 2)
        assert_prior_unchanged(world)
        self.assertTrue(any(row['run_id'] == 'fixture-run'
                            for _, row in SUP.resource_predecessors(world.ledger, 'full-run')))

        # C1 live residual group: intent arms, host claim refuses, and only a new explicit intent retries.
        with self.retained_subfixture():
            live = make_world(live_group=True)
            live_intent = public_start(live, 'h0-c1')
            self.assertIsNone(live_intent.error, live_intent.output)
            self.assertEqual(live_intent.receipt['launch_state'], 'queued')
            failed_claim = controller_release(live)
            self.assertIsNone(failed_claim.error, failed_claim.output)
            self.assertEqual(failed_claim.payloads, [])
            assert_never_started(live, 'h0-c1', 'resource-route-body-conflict')
            assert_prior_unchanged(live)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(live.live_proc.pid, signal.SIGTERM)
            for _ in range(100):
                if RR.resource_process_group_quiescent(live.prior):
                    break
                time.sleep(0.01)
            self.assertTrue(RR.resource_process_group_quiescent(live.prior))
            retry_intent = public_start(live, 'h0-c1-retry')
            self.assertIsNone(retry_intent.error, retry_intent.output)
            self.assertEqual(retry_intent.receipt['launch_state'], 'queued')
            retry_release = controller_release(live)
            self.assertIsNone(retry_release.error, retry_release.output)
            self.assertEqual(len(retry_release.payloads), 1)
            self.assertEqual(retry_release.payloads[0].wait(timeout=10), 0)
            assert_prior_unchanged(live)

        # C2 unknown host observation is a typed refusal at claim, with no payload.
        with self.retained_subfixture():
            unknown = make_world()
            queued_unknown = public_start(unknown, 'h0-c2')
            self.assertIsNone(queued_unknown.error, queued_unknown.output)
            unknown_claim = controller_release(unknown, unknown_prior='fixture-run')
            self.assertIsNone(unknown_claim.error, unknown_claim.output)
            self.assertEqual(unknown_claim.payloads, [])
            assert_never_started(unknown, 'h0-c2', 'resource-route-body-conflict')
            assert_prior_unchanged(unknown)

        # C3 evidence path aliases (symlink and .partial) fail before changing owned records.
        for alias_kind in ('symlink', 'partial'):
            with self.subTest(alias=alias_kind), self.retained_subfixture():
                aliased = make_world()
                before_registry = aliased.registry.read_bytes()
                before_arm = (aliased.ledger.root / 'armed' / 'full-run.json').read_bytes()
                before_journal = len(aliased.ledger.journal())
                target = Path(aliased.prior['sentinel'])
                log = self.base / f'{alias_kind}-candidate.log'
                if alias_kind == 'symlink':
                    log.symlink_to(target)
                else:
                    log = Path(str(target) + '.partial')
                refusal = public_start(aliased, f'h0-c3-{alias_kind}', log_path=log)
                self.assertIsNotNone(refusal.error)
                self.assertIn('resource-route-body-conflict', str(refusal.error) + refusal.output)
                self.assertEqual(refusal.payloads, [])
                self.assertEqual(aliased.registry.read_bytes(), before_registry)
                self.assertEqual((aliased.ledger.root / 'armed' / 'full-run.json').read_bytes(), before_arm)
                self.assertEqual(len(aliased.ledger.journal()), before_journal)
                assert_prior_unchanged(aliased)

        # C5 queued arm refusal settles its exact row instead of stranding queued.
        with self.retained_subfixture():
            arm_blocked = make_world()
            arm_blocked.ledger.record('run-verify', 'FAILED_TERMINAL', evidence={'reason': 'fixture-failure'},
                                      actor='fixture-other-stage-failure')
            result = public_start(arm_blocked, 'h0-c5')
            self.assertIsNone(result.error, result.output)
            self.assertEqual(result.payloads, [])
            assert_never_started(arm_blocked, 'h0-c5', 'resource-watch-binding-conflict')
            args, control, found = discover(arm_blocked)
            self.assertFalse(found and any(row.get('run_id') == 'h0-c5' for _, row in found[3]))
            assert_prior_unchanged(arm_blocked)

        # C6 claim-time stage failure and claimed successor remain blockers after a successful arm.
        for blocker in ('other-stage-failure', 'claimed-successor'):
            with self.subTest(blocker=blocker), self.retained_subfixture():
                blocked = make_world()
                queued_blocked = public_start(blocked, f'h0-c6-{blocker}')
                self.assertIsNone(queued_blocked.error, queued_blocked.output)
                if blocker == 'other-stage-failure':
                    blocked.ledger.record('run-verify', 'FAILED_TERMINAL', evidence={'reason': 'fixture-failure'},
                                          actor='fixture-other-stage-failure')
                else:
                    blocked.ledger.claim('c' * 64, {
                        'route_id': blocked.route['route_id'], 'predecessor': 'full-run',
                        'successor': 'run-verify'})
                claimed = controller_release(blocked)
                self.assertIsNone(claimed.error, claimed.output)
                self.assertEqual(claimed.payloads, [])
                assert_never_started(blocked, f'h0-c6-{blocker}', 'resource-watch-binding-conflict')
                assert_prior_unchanged(blocked)

        # C7 contradictory sentinel and C8 claimed crash are not record-terminal.
        for case, options in (('contradictory-sentinel', {'contradictory': True}),
                              ('claimed-crash', {'claimed_crash': True})):
            with self.subTest(case=case), self.retained_subfixture():
                nonterminal = make_world(**options)
                result = public_start(nonterminal, f'h0-{case}')
                self.assertIsNone(result.error, result.output)
                self.assertEqual(result.payloads, [])
                assert_never_started(nonterminal, f'h0-{case}', 'resource-watch-binding-conflict')
                args, control, found = discover(nonterminal)
                self.assertFalse(found and any(row.get('run_id') == f'h0-{case}'
                                               for _, row in found[3]))
                assert_prior_unchanged(nonterminal)

        print('STAND_INS=tool-namespace stand-in; arm-transport stand-in; source-proof stand-in; '
              'command-builder stand-in; watch stand-in; artifact-env stand-in; '
              'unknown-observation stand-in (C2 only).')

    def test_same_operation_body_with_a_new_request_id_cannot_overlap(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        release = self.base / 'same.release'
        barrier = ('import sys,time\nfrom pathlib import Path\np=Path(sys.argv[1])\n'
                   'while not p.exists(): time.sleep(.01)\n')
        first = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
            'first-id', output, payload_code=barrier, payload_args=[str(release)])
        second = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
            'second-id', output, payload_code=barrier, payload_args=[str(release)])
        processes, _ = self.launch(route, path, jobs, registry, output, first, wait_for_exit=False)
        before_registry = registry.read_bytes()
        before_watches = SUP.read_resource_watches(ledger, 'full-run')
        try:
            self.assertEqual(first['resource_operation']['operation_id'],
                             second['resource_operation']['operation_id'])
            with self.assertRaisesRegex(ValueError, 'resource-operation-duplicate'):
                self.launch(route, path, jobs, registry, output, second, wait_for_exit=False)
            self.assertEqual(registry.read_bytes(), before_registry)
            self.assertEqual(SUP.read_resource_watches(ledger, 'full-run'), before_watches)
        finally:
            release.write_text('cleanup')
            for proc in processes:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)

    def test_full_approved_request_binds_payload_environment_cwd_and_replay(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        release = self.base / 'approved.release'
        body = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
            'bound-payload', output,
            scope='run the isolated approved resource operation',
            payload_code=('import sys,time\nfrom pathlib import Path\n'
                          'p=Path(sys.argv[1])\nwhile not p.exists(): time.sleep(.01)\n'),
            payload_args=[str(release)])
        binding = body['resource_operation']
        self.assertEqual(binding['requested_scope'], 'run the isolated approved resource operation')
        self.assertEqual(binding['payload']['cwd'], str(Path(body['cwd']).resolve()))
        self.assertEqual(binding['payload']['input_hashes'], {})
        self.assertEqual(len(binding['payload']['argv_sha256']), 64)
        self.assertEqual(len(binding['payload']['environment_sha256']), 64)
        replay = SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log'])
        self.assertEqual(replay['operation_id'], binding['operation_id'])
        self.assertIsNone(SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], str(self.base.parent),
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log']))

    def test_legacy_freeform_approval_is_retained_as_provenance(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        body = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
            'legacy-freeform', output, payload_code='pass',
            approved_text='Run the variable model while preserving the fixed model.')
        self.assertIn('resource_operation', body)
        self.assertEqual(body['resource_operation']['input']['digest'],
                         __import__('dispatch_owner_input')._digest(
                             'Run the variable model while preserving the fixed model.'))

    def test_changed_payload_argv_after_selection_changes_the_binding(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        body = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
                                    'changed-payload', output, payload_code='pass')
        request_path = Path(body['command'][-1])
        request = json.loads(request_path.read_text())
        receipt_path = Path(request['Dev_admission_evidence']).parent / \
            'changed-payload-resource-start-command.json'
        receipt = json.loads(receipt_path.read_text())
        body['command'][1] = 'HEARTING_TEST_PAYLOAD=changed'
        request['compute_argv'] = body['command']
        request_bytes = json.dumps(request, sort_keys=True).encode()
        request_path.write_bytes(request_bytes)
        receipt['request_sha256'] = hashlib.sha256(request_bytes).hexdigest()
        separator = receipt['argv'].index('--')
        receipt['argv'][separator + 1:] = body['command']
        receipt_path.write_text(json.dumps(receipt, sort_keys=True))

        changed = SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log'])
        self.assertIsNotNone(changed)
        self.assertNotEqual(changed['operation_id'], body['resource_operation']['operation_id'])

    def test_freeform_approval_does_not_need_to_repeat_request_scope_as_json(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        body = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
            'uncovered-scope', output, payload_code='pass',
            approved_text='Run the variable model while preserving the fixed model.')
        request_path = Path(body['command'][-1])
        request = json.loads(request_path.read_text())
        request['requested_scope'] = 'different operation scope'
        request_bytes = json.dumps(request, sort_keys=True).encode()
        request_path.write_bytes(request_bytes)
        receipt_path = Path(request['Dev_admission_evidence']).parent / \
            'uncovered-scope-resource-start-command.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['request_sha256'] = hashlib.sha256(request_bytes).hexdigest()
        receipt_path.write_text(json.dumps(receipt, sort_keys=True))

        changed = SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log'])
        self.assertIsNotNone(changed)
        self.assertNotEqual(changed['operation_id'], body['resource_operation']['operation_id'])

    def test_freeform_approval_binds_local_bridge_and_remote_compute_hosts_layers(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        approved_text = 'Run the variable model while preserving the fixed model.'
        body = self.authorized_bridge_body(route, path, jobs, registry, owner_attempt,
                                           owner_input, 'bridge-var', output,
                                           approved_text=approved_text)
        binding = body['resource_operation']
        self.assertIsNotNone(binding)
        request_path = Path(body['command'][-1])
        request = json.loads(request_path.read_text())
        self.assertNotEqual(request['compute_argv'], body['command'])
        self.assertEqual(binding['payload']['cwd'], str(self.base.resolve()))
        self.assertEqual(binding['input']['digest'],
                         __import__('dispatch_owner_input')._digest(approved_text))
        hashes = binding['payload']['input_hashes']
        self.assertEqual(set(hashes), {
            'config_sha256', 'data_lock_sha256', 'gpu_smoke_sha256', 'bridge_sha256'})
        self.assertEqual(binding['payload']['environment_sha256'].__len__(), 64)

        bridge = self.base / 'bridge-var-remote-production-bridge-var-rir512.py'
        bridge_bytes = bridge.read_bytes()
        bridge.write_text(bridge.read_text() + '# changed after selection\n')
        self.assertIsNone(SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log']))
        bridge.write_bytes(bridge_bytes)
        (self.base / 'bridge-var-config.yaml').write_text('model: changed-after-selection\n')
        self.assertIsNone(SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log']))

    def test_bridge_binding_refuses_owner_route_node_and_target_mismatch(self):
        import dispatch_owner_input as INPUT

        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        body = self.authorized_bridge_body(route, path, jobs, registry, owner_attempt,
                                           owner_input, 'bridge-boundaries', output)
        runner = SUP.runner()
        launch_request = dict(self.EMPTY_LAUNCH_REQUEST)
        self.assertIsNotNone(runner.resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            launch_request, body['owner_wait'], registry_path=registry, log_path=body['log']))

        wrong_owner = dict(body['owner_wait'], parent_attempt_id='att-other-owner')
        self.assertIsNone(runner.resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            launch_request, wrong_owner, registry_path=registry, log_path=body['log']))
        other_route_path = self.base / 'other-route.json'
        other_route_path.write_text(json.dumps(route))
        self.assertIsNone(runner.resource_operation_binding(
            route, other_route_path, body['node'], body['run_id'], body['command'], body['cwd'],
            launch_request, body['owner_wait'], registry_path=registry, log_path=body['log']))
        self.assertIsNone(runner.resource_operation_binding(
            route, path, 'run-verify', body['run_id'], body['command'], body['cwd'],
            launch_request, body['owner_wait'], registry_path=registry, log_path=body['log']))
        parent, _target = INPUT._target(jobs, owner_attempt)
        with mock.patch.object(INPUT, '_target', return_value=(parent, 'changed-owner-target')):
            self.assertIsNone(runner.resource_operation_binding(
                route, path, body['node'], body['run_id'], body['command'], body['cwd'],
                launch_request, body['owner_wait'], registry_path=registry, log_path=body['log']))

    def test_changed_bridge_request_is_refused_before_resource_reservation(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        body = self.authorized_bridge_body(route, path, jobs, registry, owner_attempt,
                                           owner_input, 'bridge-mutation', output)
        original_binding = body['resource_operation']
        registry_data = json.loads(registry.read_text())
        registry_data['runs'][body['run_id']] = body
        registry.write_text(json.dumps(registry_data))
        self.arm(path, registry, run_id=body['run_id'], extra=("--jobs", str(jobs),
            "--artifact-base", str(output)))

        request_path = Path(body['command'][-1])
        request = json.loads(request_path.read_text())
        request['compute_argv'][-1] += '; true'
        request_bytes = json.dumps(request, sort_keys=True).encode()
        request_path.write_bytes(request_bytes)
        record_path = Path(request['Dev_admission_evidence']).parent / \
            'bridge-mutation-resource-start-command.json'
        receipt = json.loads(record_path.read_text())
        receipt['request_sha256'] = hashlib.sha256(request_bytes).hexdigest()
        record_path.write_text(json.dumps(receipt, sort_keys=True))
        before = registry.read_bytes()
        args = SimpleNamespace(jobs=str(jobs), run_id=body['run_id'], node=body['node'],
                                smoke_attestation=None, config_manifest=None)
        with self.assertRaisesRegex(ValueError, 'resource-operation-authorization-changed'):
            SUP.runner().start_verified(registry, args, route, path, dict(body))
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual(body['resource_operation']['operation_id'], original_binding['operation_id'])

    def test_v3532_context_controller_launch_and_wait_consume_flat_queued_cpu_bridge(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        seed_armed = SUP.read_armed(ledger)['full-run']
        seed_evidence = SUP.resource_evidence(seed_armed)
        self.assertTrue(seed_evidence['terminal'])
        self.assertTrue(seed_evidence['succeeded'])
        self.assertEqual(seed_evidence['exit_code'], 0)
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        primary_path = ledger.root / 'armed' / 'full-run.json'
        original_bytes = primary_path.read_bytes()
        original_armed = json.loads(original_bytes)
        body_a = self.authorized_bridge_body(
            route, path, jobs, registry, owner_attempt, owner_input, 'legacy-var', output,
            payload_path=self.base / 'legacy-var-result.json',
            payload_result='var-operation-result\n')
        flat_a = self._queue_current_legacy_bridge_operation(
            route, path, jobs, registry, owner_attempt, body_a, output)
        flat_a_bytes = primary_path.read_bytes()
        self.assertEqual(flat_a['predecessor_id'], body_a['run_id'])
        self.assertEqual(flat_a['resource_binding'], WAIT.resource_legacy_body_digest(body_a))
        self.assertEqual(flat_a['resource_current_binding'], WAIT.resource_body_digest(body_a))
        self.assertEqual(SUP.resource_watch_storage_path(ledger, original_armed).read_bytes(),
                         original_bytes)
        self.assertEqual({row['predecessor_id'] for row in SUP.read_resource_watches(
            ledger, 'full-run')}, {'fixture-run', body_a['run_id']})

        fixture_dir = HERE / 'fixtures' / 'resource-v3532'
        old_path = fixture_dir / 'dispatch_resource_wait.py'
        legacy_spec = importlib.util.spec_from_file_location('legacy_v3532_resource_wait_fixture', old_path)
        legacy_wait = importlib.util.module_from_spec(legacy_spec)
        legacy_spec.loader.exec_module(legacy_wait)
        legacy_wait.__getattr__ = lambda name: getattr(WAIT, name)
        legacy_supervisor = legacy_wait.supervisor()
        legacy_supervisor.bind_runtime_support(SUP)
        legacy_runner = legacy_supervisor.runner()
        # Match the old install layout for its relative utility/tool lookups.
        legacy_runner.__file__ = str(HERE / 'resource-runner.py')
        self.assertEqual(hashlib.sha256(V3532_READ_ARMED_SOURCE.encode()).hexdigest(),
                         V3532_READ_ARMED_SHA256)
        legacy_read_armed = {}
        exec(compile(V3532_READ_ARMED_SOURCE, 'v3.53.2-workflow-supervisor.py', 'exec'),
             {'json': json, 'armed_dir': legacy_supervisor.armed_dir}, legacy_read_armed)
        legacy_supervisor.read_armed = legacy_read_armed['read_armed']
        phase = self.base / 'legacy-owner-phase.json'
        control = SimpleNamespace(thread_id='same-native', pending=lambda: False)
        events = []
        result_path = self.base / 'legacy-var-payload-result.json'
        args = SimpleNamespace(
            parent_attempt_id=owner_attempt, route_file=str(path),
            route_id=route['route_id'], route_hash=route['route_hash'], jobs=str(jobs),
            resource_launch_command=lambda row: (row['command'], 'fixture-local-cpu'),
        )
        children = []

        def deliver_from_old_consumer(body):
            with mock.patch.dict(sys.modules, {'dispatch_resource_wait': legacy_wait}):
                _old_supervisor, old_route, _old_ledger, found = legacy_wait.context(args, control)
                self.assertEqual(old_route['route_id'], route['route_id'])
                self.assertEqual([row['run_id'] for _, row in found], [body['run_id']])
                armed, queued = found[0]
                observer = mock.Mock()
                observer.poll.return_value = None
                import artifact_producer
                with mock.patch.object(artifact_producer, 'prepare_route_artifact_env',
                                       return_value={'AGENT_ARTIFACT_OUTPUT_DIR': str(output)}), \
                     mock.patch.object(legacy_runner.gpu_leases, 'resource_admission',
                                       return_value=(None, None, {})), \
                     mock.patch.object(legacy_runner, 'start_watch', return_value=(
                        observer, SUP.RR.proc_identity(os.getpid()))):
                    legacy_wait.admit_controller_launch(args, control, armed, queued, [])
                launched = json.loads(registry.read_text())['runs'][body['run_id']]
                child = args.resource_children[legacy_wait.resource_key(launched)]
                children.append(child)
                payload_proc, _observer = child
                self.assertEqual(payload_proc.wait(timeout=10), 0)

                sleeps = [0]
                def bounded_sleep(_seconds):
                    sleeps[0] += 1
                    if sleeps[0] > 80:
                        raise TimeoutError(f"legacy resource receipt did not become ready: {body['run_id']}")
                    time.sleep(0.01)
                prompt = legacy_wait.wait(args, phase, control, set(), events.append,
                                          sleep=bounded_sleep)
                state = legacy_wait.JOIN.read_supervisor_phase_state(phase, owner_attempt)
                receipt = copy.deepcopy(state.resource['outbox']['receipt'])
                receipt_id = state.resource['outbox']['receipt_id']
                self.assertIn(body['run_id'], prompt)
                self.assertEqual(receipt['run_id'], body['run_id'])
                self.assertFalse(receipt['verification_pass'])
                self.assertTrue(legacy_wait.acknowledge(phase, owner_attempt, receipt_id))
            return receipt, receipt_id

        successor_args = SimpleNamespace(
            route_file=str(path), route_node='run-verify', attempt_id='att-verify',
            jobs=str(jobs), agent_home=ROOT, worktree=str(self.base), dispatch_depth=2,
            parent_slug='fixture-owner', parent_attempt_id=owner_attempt,
            parent_harness='codex', parent_transport='headless',
            parent_sandbox='workspace-write', subsession_id=None,
        )

        def current_successor_start():
            with mock.patch('review_input.preview_request_nodes', return_value=frozenset()), \
                 mock.patch('subprocess.check_output', return_value=str(self.base)):
                return RA.completion_gate(
                    successor_args, 'start', ROOT, jobs, gate=DC.completion_marker_gate)

        try:
            receipt_a, receipt_id_a = deliver_from_old_consumer(body_a)
            self.assertEqual((self.base / 'legacy-var-result.json').read_text(),
                             'var-operation-result\n')
            current = SUP.read_resource_watches(ledger, 'full-run')
            armed_a = next(row for row in current if row['predecessor_id'] == body_a['run_id'])
            evidence_a = SUP.resource_evidence(armed_a)
            self.assertTrue(evidence_a['terminal'])
            self.assertTrue(evidence_a['succeeded'])
            self.assertEqual(evidence_a['exit_code'], 0)
            self.assertEqual(evidence_a['liveness'], 'exited')
            self.assertTrue(evidence_a['quiescent'])

            body_b = self.authorized_bridge_body(
                route, path, jobs, registry, owner_attempt, owner_input, 'legacy-fix-resume', output,
                payload_path=self.base / 'legacy-fix-result.json',
                payload_result='fix-resume-result\n',
                artifact_output_path=output / 'run.json',
                artifact_output_result=json.dumps({'result': 'declared-output', 'run_id': 'legacy-fix-resume'}) + '\n')
            flat_b = self._queue_current_legacy_bridge_operation(
                route, path, jobs, registry, owner_attempt, body_b, output)
            flat_b_bytes = primary_path.read_bytes()
            self.assertEqual(flat_b['predecessor_id'], body_b['run_id'])
            self.assertEqual(flat_b['resource_binding'], WAIT.resource_legacy_body_digest(body_b))
            self.assertEqual(flat_b['resource_current_binding'], WAIT.resource_body_digest(body_b))
            self.assertNotEqual(body_a['resource_operation']['operation_id'],
                                body_b['resource_operation']['operation_id'])
            self.assertEqual(primary_path.read_bytes(), flat_b_bytes)
            self.assertEqual(SUP.resource_watch_storage_path(ledger, original_armed).read_bytes(),
                             original_bytes)
            self.assertEqual(SUP.resource_watch_storage_path(ledger, json.loads(flat_a_bytes)).read_bytes(),
                             flat_a_bytes)
            self.assertEqual({row['predecessor_id'] for row in SUP.read_resource_watches(
                ledger, 'full-run')}, {'fixture-run', body_a['run_id'], body_b['run_id']})

            blocked = current_successor_start()
            self.assertIsNotNone(blocked)
            self.assertEqual(blocked[0], 'resource-owner-stage-admission-blocked')
            self.assertIn('resource-owner-operation-unsettled', blocked[2]['detail'])
            self.assertEqual(ledger.claims(), {})

            receipt_b, receipt_id_b = deliver_from_old_consumer(body_b)
            self.assertEqual((self.base / 'legacy-fix-result.json').read_text(),
                             'fix-resume-result\n')
            self.assertTrue((output / 'run.json').is_file())
            self.assertEqual(receipt_a['run_id'], body_a['run_id'])
            self.assertEqual(receipt_b['run_id'], body_b['run_id'])
            self.assertNotEqual(receipt_id_a, receipt_id_b)

            state = legacy_wait.JOIN.read_supervisor_phase_state(phase, owner_attempt)
            self.assertIsNone(state.resource['outbox'])
            self.assertEqual(len(state.resource['delivered']), 2)
            self.assertEqual(len(set(state.resource['delivered'])), 2)
            current = SUP.read_resource_watches(ledger, 'full-run')
            evidence_by_run = {}
            for run_id in (body_a['run_id'], body_b['run_id']):
                armed = next(row for row in current if row['predecessor_id'] == run_id)
                evidence = SUP.resource_evidence(armed)
                self.assertTrue(evidence['terminal'], evidence)
                self.assertTrue(evidence['succeeded'], evidence)
                self.assertEqual(evidence['exit_code'], 0)
                self.assertEqual(evidence['liveness'], 'exited')
                self.assertTrue(evidence['quiescent'])
                evidence_by_run[run_id] = evidence
            self.assertNotEqual(evidence_by_run[body_a['run_id']]['identity'],
                                evidence_by_run[body_b['run_id']]['identity'])
            run_rows = json.loads(registry.read_text())['runs']
            for body in (body_a, body_b):
                row = run_rows[body['run_id']]
                self.assertEqual(row['status'], 'succeeded')
                self.assertEqual(row['exit_code'], 0)
                self.assertTrue(row.get('pid'))
                self.assertTrue(row.get('starttime'))
                self.assertEqual(Path(row['sentinel']).read_text(), '0')

            self.assertIsNone(current_successor_start())
            stage = ledger.state()['nodes']['full-run']
            operation_results = {row['run_id']: row for row in stage['evidence']['resource_operations']
                                 if row['run_id'] in {body_a['run_id'], body_b['run_id']}}
            self.assertEqual(set(operation_results), {body_a['run_id'], body_b['run_id']})
            for body in (body_a, body_b):
                result = operation_results[body['run_id']]
                self.assertEqual(result['operation_id'], body['resource_operation']['operation_id'])
                self.assertEqual(result['identity'], evidence_by_run[body['run_id']]['identity'])
                self.assertEqual(result['exit_code'], 0)
                self.assertEqual(result['request_sha256'], body['resource_operation']['request_sha256'])
                self.assertEqual(len(result['resource_sha256']), 64)
            self.assertEqual(stage['state'], 'STAGE_SUCCEEDED')
            self.assertEqual(len(ledger.claims()), 1)
            self.assertEqual(len(ledger.claim_history()), 1)
            marker = jobs.parent / 'completion' / route['route_id'] / 'full-run.json'
            marker_bytes = marker.read_bytes()
            self.assertIsNone(current_successor_start())
            self.assertEqual(marker.read_bytes(), marker_bytes)
            self.assertEqual(len(ledger.claims()), 1)
            self.assertEqual(len(ledger.claim_history()), 1)
            self.assertEqual(SUP.resource_watch_storage_path(ledger, original_armed).read_bytes(),
                             original_bytes)
            self.assertEqual(SUP.resource_watch_storage_path(ledger, json.loads(flat_a_bytes)).read_bytes(),
                             flat_a_bytes)
        finally:
            for process, _observer in children:
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, 15)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=5)

    def test_payload_input_hash_must_match_the_effective_argv_input(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        config = self.base / 'approved-config.yaml'
        config.write_text('model: approved\n')
        config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
        body = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
            'payload-input-hash', output, payload_code='pass',
            payload_args=['--config', str(config)],
            provenance={'config_sha256': config_hash})
        binding = SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log'])
        self.assertIsNotNone(binding)
        self.assertEqual(binding['payload']['input_hashes'], {'config_sha256': config_hash})
        config.write_text('model: changed\n')
        self.assertIsNone(SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log']))

    def test_user_direction_id_without_exact_command_receipt_does_not_authorize_overlap(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        owner_attempt, owner_input = self.live_owner(route, jobs, registry)
        body = self.authorized_body(route, path, jobs, registry, owner_attempt, owner_input,
                                    'unreceipted-var', output, payload_code='pass')
        request_path = Path(body['command'][-1])
        request = json.loads(request_path.read_text())
        command_receipt = Path(request['Dev_admission_evidence']).parent / \
            'unreceipted-var-resource-start-command.json'
        receipt = json.loads(command_receipt.read_text())
        receipt['argv'][1] = '/installed/releases/v3.53.2/utilities/resource-runner.py'
        command_receipt.write_text(json.dumps(receipt))
        self.assertIsNone(SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log']))
        command_receipt.unlink()
        self.assertIsNone(SUP.runner().resource_operation_binding(
            route, path, body['node'], body['run_id'], body['command'], body['cwd'],
            dict(self.EMPTY_LAUNCH_REQUEST), body['owner_wait'],
            registry_path=registry, log_path=body['log']))
        body.pop('resource_operation')
        identity = SUP.RR.proc_identity(os.getpid())
        data = json.loads(registry.read_text())
        data['runs']['fixture-run'].update(identity, process_group=os.getpgrp(),
            pid_namespace=os.readlink('/proc/self/ns/pid'), status='running', exit_code=None)
        registry.write_text(json.dumps(data))
        before_registry = registry.read_bytes()
        before_armed = SUP.read_armed(ledger)
        with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
            self.launch(route, path, jobs, registry, output, body)
        self.assertEqual(registry.read_bytes(), before_registry)
        self.assertEqual(SUP.read_armed(ledger), before_armed)


if __name__ == '__main__':
    unittest.main()
