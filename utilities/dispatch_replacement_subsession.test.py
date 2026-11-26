#!/usr/bin/env python3
"""SD-157 replacement keeps declared sub-session cardinality and prefix evidence."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import dispatch_replacement as R
import dispatch_replacement_subsession as RS


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


SERIAL = load('_replacement_serial_fixture', 'serial_chain_supervisor.test.py')
SUBDIVISION = load('_replacement_subdivision_fixture', 'stage_session_subdivision.test.py')


def replace_row(jobs, route, original, *, status='done', note='completed-subsession'):
    lines = jobs.read_text().splitlines()
    fields, source = R._rows(lines)[original]
    fields[1] = 'done'; source['note'] = 'dead-exact-pid'; source['failure_class'] = 'contract'
    logical = {'root_route_id': route['route_id'], 'node': source['route_node']}
    family = R._digest(logical)
    target = 'att-'+hashlib.sha256(('replacement:'+family).encode()).hexdigest()[:48]
    record = {'schema': R.SCHEMA, 'family_id': family, 'logical_node': logical,
              'original_attempt_id': original, 'replacement_attempt_id': target, 'ordinal': 1}
    R._once(R._record_path(jobs, family), record)
    R._reserve_source(jobs, original, family)
    source.update(replacement_family_id=family, replacement_claim_digest=R._digest(record),
                  replacement_attempt_id=target, replacement_ordinal='1')
    fields[5] = ','.join(key+'='+value for key,value in source.items())
    for i,line in enumerate(lines):
        if R.DC.parse_registry_metadata(line.split('\t')[-1]).get('attempt_id') == original:
            lines[i] = '\t'.join(fields)
    candidate = dict(source, attempt_id=target, replacement_original_attempt_id=original,
                     automatic_retry_of=original, note=note, failure_class='pass',
                     launch_claimed='1', launch_started='1')
    candidate.pop('replacement_attempt_id')
    fresh = list(fields); fresh[1] = status
    fresh[5] = ','.join(key+'='+value for key,value in candidate.items())
    jobs.write_text('\n'.join([*lines, '\t'.join(fresh)])+'\n')
    return target, record


class ReplacementSubsessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.harness = SimpleNamespace(base=self.root, artifact_root=self.root/'.agent_reports',
                                      jobs=self.root/'jobs.log', lease=self.root/'lease')
        self.harness.artifact_root.mkdir(); self.harness.jobs.touch()
        self.route, self.manifest = SERIAL._real_chain_fixture(self.harness, SERIAL.CLAUDE, 3)
        self.jobs = self.harness.jobs

    def test_serial_successor_uses_effective_attempt_without_changing_manifest(self):
        original = self.manifest['sessions'][0]['attempt_id']
        manifest_bytes = Path(self.manifest['_manifest_path']).read_bytes()
        target, _record = replace_row(self.jobs, self.route, original)
        proof = SERIAL.ADVANCE.prove_serial_chain(self.jobs, self.manifest['chain_id'], parent_attempt_id='att-parent')
        self.assertIsInstance(proof, SERIAL.ADVANCE.ProvenSerialChain)
        self.assertEqual(len(proof.rows_by_index), 3)
        self.assertEqual(proof.rows_by_index[1]['metadata']['attempt_id'], target)
        self.assertEqual(SERIAL.ADVANCE.resume_index(self.jobs, self.manifest), 2)
        self.assertEqual(Path(self.manifest['_manifest_path']).read_bytes(), manifest_bytes)
        self.assertEqual(R._rows(self.jobs.read_text().splitlines())[original][1]['note'], 'dead-exact-pid')

    def test_live_replacement_keeps_original_slot_unfinished(self):
        original = self.manifest['sessions'][0]['attempt_id']
        replace_row(self.jobs, self.route, original, status='open', note='')
        self.assertEqual(SERIAL.ADVANCE.resume_index(self.jobs, self.manifest), 1)

    def test_changed_slice_scope_and_claim_fail_closed(self):
        original = self.manifest['sessions'][0]['attempt_id']
        target, record = replace_row(self.jobs, self.route, original)
        lines = self.jobs.read_text().splitlines()
        lines[-1] = lines[-1].replace('subsession_index=1', 'subsession_index=2')
        self.jobs.write_text('\n'.join(lines)+'\n')
        proof = SERIAL.ADVANCE.prove_serial_chain(self.jobs, self.manifest['chain_id'], parent_attempt_id='att-parent')
        self.assertIsInstance(proof, SERIAL.ADVANCE.ChainProofRefusal)
        self.assertEqual(proof.reason, 'serial-chain-replacement-invalid')
        R._record_path(self.jobs, record['family_id']).write_text('{}')
        with self.assertRaises(R.DC.DispatchContractError):
            SERIAL.ADVANCE.resume_index(self.jobs, self.manifest)

    def test_projection_follows_replaced_parent_and_route_hash(self):
        # The slot's own work binds the replacement. Who answers for the parent, the route file it runs
        # from and the harness it was launched under follow the lineage claim admission binds, so a
        # replacement differing only there projects.
        original = self.manifest['sessions'][0]['attempt_id']
        target, _record = replace_row(self.jobs, self.route, original)
        sealed = self.jobs.read_text().splitlines()
        meta = R.DC.parse_registry_metadata(sealed[-1].split('\t')[-1])

        def project(key, value):
            lines = list(sealed)
            lines[-1] = (lines[-1].replace(f"{key}={meta[key]}", f"{key}={value}") if key in meta
                         else lines[-1] + f",{key}={value}")
            self.jobs.write_text('\n'.join(lines)+'\n')
            rows = SERIAL.ADVANCE._registry_rows_for_chain(self.jobs, self.manifest['chain_id'])
            return RS.project(self.jobs, self.manifest, rows)

        for key, value in {'harness': 'opencode', 'parent': 'replacement-owner-label',
                           'route_file': '/elsewhere/route.json'}.items():
            with self.subTest(key=key):
                _rows, mapping = project(key, value)
                self.assertEqual(mapping[original], target)
        # The slot's own work, inputs and purpose still bind it.
        for key in ('phase_brief_sha256', 'fixed_files_sha256', 'subsession_index', 'route_node'):
            with self.subTest(binding=key), self.assertRaises(R.DC.DispatchContractError) as refused:
                project(key, 'other')
            self.assertEqual(refused.exception.reason, 'replacement-subsession-scope-mismatch')

    def test_later_failed_slice_still_requires_its_own_launch_input(self):
        replace_row(self.jobs, self.route, self.manifest['sessions'][0]['attempt_id'])
        second = self.manifest['sessions'][1]['attempt_id']
        lines = self.jobs.read_text().splitlines()
        for i,line in enumerate(lines):
            fields=line.split('\t'); meta=R.DC.parse_registry_metadata(fields[-1])
            if meta.get('attempt_id') == second:
                fields[1]='done'
                fields[5]+=(',note=dead-exact-pid,fallback_hop=same-harness-headless,'
                            'phase_brief_sha256='+'a'*64+',state_ledger='+str(self.root/'second.md'))
                lines[i]='\t'.join(fields)
        self.jobs.write_text('\n'.join(lines)+'\n')
        with mock.patch.object(R, '_route', return_value=(self.root/'route.json', self.route)), \
             mock.patch.object(R, 'death_proof', return_value={'state':'quiescent','death_kind':'silent'}):
            with self.assertRaises(R.DC.DispatchContractError) as caught:
                R.claim(self.jobs, second)
        self.assertEqual(caught.exception.reason, 'replacement-input-unproven')

    def _row_outcome(self, attempt, *, failed=False):
        lines=self.jobs.read_text().splitlines()
        for i,line in enumerate(lines):
            fields=line.split('\t'); meta=R.DC.parse_registry_metadata(fields[-1])
            if meta.get('attempt_id') != attempt:
                continue
            fields[1]='done'
            meta.update(note='dead-exact-pid' if failed else 'completed-subsession',
                        failure_class='contract' if failed else 'pass')
            fields[-1]=','.join(key+'='+value for key,value in meta.items())
            lines[i]='\t'.join(fields)
        self.jobs.write_text('\n'.join(lines)+'\n')

    def _refresh(self, selected):
        rows=R._rows(self.jobs.read_text().splitlines())
        return [SimpleNamespace(attempt_id=aid,status=rows[aid][0][1],metadata=rows[aid][1])
                for aid in selected]

    def test_predecessor_replay_waits_existing_replacement_without_starting_original(self):
        first, second = [s['attempt_id'] for s in self.manifest['sessions'][:2]]
        self._row_outcome(first)
        target, _record=replace_row(self.jobs,self.route,second,status='open',note='')
        first_row=self._refresh({first})[0]
        with mock.patch.object(SERIAL.ADVANCE,'flush_chain_handoff_for_row'), \
             mock.patch.object(SERIAL.ADVANCE,'coordinate_subsession_advance') as start:
            observed=SERIAL.ADVANCE.coordinate_chain_advance_from_joined_rows(
                self.jobs,'att-parent',{first:first_row})
        self.assertEqual(observed,target)
        start.assert_not_called()

    def _later_slice_driver(self, *, replacement_fails):
        first, second, third=[s['attempt_id'] for s in self.manifest['sessions']]
        self._row_outcome(first)
        launches=[]; joins=[]; advanced=[]; target=None
        def join(selected):
            joins.append(set(selected))
            for aid in selected:
                self._row_outcome(aid,failed=aid==second or (replacement_fails and aid==target))
            return {'state':'ready','children':[{'attempt_id':aid} for aid in selected]}
        def checkpoint(selected):
            nonlocal target
            if second in selected and target is None:
                target,_record=replace_row(self.jobs,self.route,second)
                launches.append(target)
                return (*R.effective_attempts(self.jobs,selected),[])
            return R.advance_batch(self.jobs,selected)
        def step(_jobs,_parent,joined):
            row=next(iter(joined.values()))
            advanced.append(row.attempt_id)
            index=int(row.metadata['subsession_index'])
            return SERIAL.ADVANCE.ChainAdvanceStep(
                'complete' if index==3 else 'advanced',chain_id=self.manifest['chain_id'],
                predecessor_index=index,successor_index=index+1,
                attempt_id=third if index==2 else second)
        with mock.patch.object(SERIAL.ADVANCE,'advance_chain_step',side_effect=step):
            result=SERIAL.ADVANCE.drive_serial_chain(
                jobs=self.jobs,parent_attempt_id='att-parent',attempts={first},
                receipt={'state':'ready','children':[{'attempt_id':first}]},
                refresh=self._refresh,join=join,reconcile=lambda *_:False,max_reparks=1,
                replacement_checkpoint=checkpoint)
        return result,launches,joins,advanced,target,third

    def test_later_slice_death_replaced_once_before_next_slice(self):
        result,launches,joins,advanced,target,third=self._later_slice_driver(replacement_fails=False)
        self.assertEqual(launches,[target])
        self.assertEqual(result.attempts,frozenset({third}))
        self.assertIn(target,advanced)
        self.assertIn({target},joins)
        self.assertIn(target,result.traversed)

    def test_later_replacement_keeps_parent_authority_protection(self):
        result,launches,joins,advanced,target,third=self._later_slice_driver(replacement_fails=True)
        self.assertEqual(launches,[target])
        self.assertNotIn(target,advanced)
        self.assertNotIn({third},joins)
        self.assertEqual(result.refusal.reason,'replacement-parent-identity-unproven')
        self.assertEqual(result.attempts,frozenset({target}))
        self.assertIn('replacement_attention',result.receipt)

    def test_serial_and_parallel_aggregation_use_effective_pass_and_preserve_source_failure(self):
        fixture = SUBDIVISION.SubdivisionContractTest()
        for mode in ('serial', 'parallel'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                worktree, route, node, manifest_path = fixture._fixture(td, mode=mode)
                manifest = SUBDIVISION.SSC.load_manifest(manifest_path, route=route, node=node)
                jobs = Path(td)/'jobs.log'; fixture._write_jobs(jobs, route, manifest, mode=mode)
                original = manifest['sessions'][0]['attempt_id']
                target, _record = replace_row(jobs, route, original, note='completed-marker')
                evidence = Path(td)/'evidence.md'; evidence.write_text('complete\n')
                with mock.patch.dict(os.environ, {'AGENT_DISPATCH_JOBS': str(jobs)}):
                    fixture._admit(route, node, manifest)
                    marker, result = SUBDIVISION.CR.complete_subsession_stage(
                        route, node, 'execute', evidence, manifest_path, jobs)
                    self.assertEqual(result['sessions'], 2)
                    self.assertEqual(marker['session_chain_id'], manifest['chain_id'])
                    self.assertEqual(R._rows(jobs.read_text().splitlines())[original][1]['note'], 'dead-exact-pid')
                    # Repeating aggregation preserves the single stage gate.
                    repeated, status = SUBDIVISION.CR.complete_subsession_stage(
                        route, node, 'execute', evidence, manifest_path, jobs)
                    self.assertEqual(marker, repeated)
                    self.assertTrue(status['resumed'])


if __name__ == '__main__':
    unittest.main()
