#!/usr/bin/env python3
"""Revision generation selection through the public start path; no model calls."""
import contextlib
import importlib.util
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_producer as PRODUCER
import dispatch_contract as DC
import dispatch_replacement as REPLACEMENT
import dispatch_replacement_batch as REPLACEMENT_BATCH
import dispatch_owner_input as OWNER_INPUT
import review_source_subject as SUBJECT

spec = importlib.util.spec_from_file_location("revision_route", ROOT / "utilities/capability-route.py")
ROUTE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ROUTE)
spec = importlib.util.spec_from_file_location("revision_node", ROOT / "utilities/dispatch-node.py")
NODE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(NODE)


class RevisionBatchFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.jobs = self.base / "dispatch/jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.touch()
        self.art = self.base / ".agent_reports"
        self.art.mkdir()
        fake_bin = self.base / "bin"
        fake_bin.mkdir()
        self.env = {
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.base / "home"),
            "XDG_CONFIG_HOME": str(self.base / "config"),
            "XDG_STATE_HOME": str(self.base / "state"),
            "XDG_DATA_HOME": str(self.base / "data"),
            "XDG_CACHE_HOME": str(self.base / "cache"),
            "CODEX_HOME": str(self.base / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.base / "claude"),
            "AGENT_HOME": str(ROOT),
            "AGENT_ARTIFACT_ROOT": str(self.art),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_MODEL_GOVERNOR_ROOT": str(self.art / ".runtime/model-worker-governor"),
            "AGENT_ARTIFACT_CHECKPOINT": "off",
            "DISPATCH_DEFAULTS_CONFIG": str(ROOT / "profiles/dispatch-defaults.yaml"),
            # Normal title work must use the fixture provider too, rather than
            # probing/calling host runtimes before reaching the fake Claude.
            "FLEET_TITLE_PROVIDER": "claude",
            "HEARTING_GATES": "off",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for command in (["init", "-q"], ["config", "user.email", "fixture@example.com"],
                        ["config", "user.name", "Fixture"]):
            self.run_cmd(["git", "-C", str(self.repo), *command])
        (self.repo / "x").write_text("initial\n")
        self.run_cmd(["git", "-C", str(self.repo), "add", "x"])
        self.run_cmd(["git", "-C", str(self.repo), "commit", "-qm", "init"])
        # Only the outer CLI transport is fake. All wrappers, admissions,
        # reservation/claim fences, immutable inputs and completion are real.
        runtime = fake_bin / "claude"
        runtime.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
if '--help' in sys.argv:
    print('--permission-mode --setting-sources --settings --tools --strict-mcp-config')
    raise SystemExit(0)
if os.environ.get('FLEET_TITLE_REFRESH') == '1':
    print('TITLE: Fixture review batch')
    print('NOW: Fixture review has returned its verdict.')
    raise SystemExit(0)
route=json.loads(pathlib.Path(os.environ['AGENT_ROUTE_FILE']).read_text())
node=next(n for n in route['nodes'] if n['id']==os.environ['AGENT_ROUTE_NODE'])
trace=pathlib.Path(os.environ['FIXTURE_TRANSPORT_TRACE'])
with trace.open('a') as handle:
    handle.write(json.dumps({'attempt':os.environ['AGENT_DISPATCH_ATTEMPT_ID'],
        'jobs':os.environ['AGENT_DISPATCH_JOBS'], 'node':node})+'\\n')
if node['id']==os.environ.get('FIXTURE_DEAD_NODE'):
    once=pathlib.Path(os.environ['FIXTURE_DEATH_ONCE'])
    if not once.exists():
        once.write_text('first physical attempt only')
        print('{}')
        raise SystemExit(0)
out=pathlib.Path(os.environ['AGENT_ARTIFACT_OUTPUT_DIR']) / node['outputs'][0]
out.parent.mkdir(parents=True,exist_ok=True)
verdict='FAIL' if node['id']==os.environ.get('FIXTURE_FAIL_NODE') else 'PASS'
out.write_text(f'Fixture independent review: {verdict}.\\n')
text=f'artifact: {out}\\nverdict: {verdict}\\nblocker: '+('fixture finding' if verdict=='FAIL' else 'none')
# Supply the ordinary stream text consumed by the real summary observer.
print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':text}]}}))
print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':text}))
''')
        runtime.chmod(0o755)
        self.trace = self.base / "transports.jsonl"
        self.trace.touch()
        self.env['FIXTURE_TRANSPORT_TRACE'] = str(self.trace)
        tuples = [{"parent_harness": "codex", "parent_transport": "headless",
                   "parent_sandbox": "workspace-write", "child_harness": "claude",
                   "launch_authority": "conductor", "status": "supported",
                   "probe_source": "fixture", "probe_time": "2026-10-12T00:00:00Z",
                   "failure_class": "", "checked_worktree": str(self.repo),
                   "failure_scope": "none", "codex_command": "not-applicable",
                   "retry_on_isolated_worktree": 0}]
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.route = ROUTE.compile_route("autopilot-code", "debug", "strong",
                self.repo, self.art, signals=["shared-contract"], transport="headless",
                tracking="tracked", tracked_gate_evidence={
                    "spec_read": {"satisfied": True, "source": "fixture"},
                    "drift_verdict": "within-spec", "workflow_mode": "tracked",
                    "artifact_guard": {"satisfied": True, "source": "fixture"}},
                dispatch_evidence={"tuples": tuples, "native_subagent": []})
        self.path = self.art / ".runtime/routes" / (self.route["route_id"] + ".json")
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.route))
        with mock.patch.dict(os.environ, self.env, clear=True):
            issued = PRODUCER.begin(self.art, route_file=self.path,
                capability="autopilot-code", intensity="strong", require_cycle=True)
        self.route = json.loads(self.path.read_text())
        self.env.update(issued["env"])
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertIsNotNone(PRODUCER.route_cycle_for(self.art, self.route), issued)
        self.output = Path(issued["cycle_dir"]) / "artifacts"
        self.output.mkdir(parents=True, exist_ok=True)
        self.evidence = self.output / "dev_logs/execute.md"
        self.evidence.parent.mkdir(parents=True)
        self.evidence.write_text("initial implementation\n")
        self.owner = "att-fixture-owner-" + self.base.name
        identity = DC.process_launch_identity(os.getpid())
        meta = {"attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "fallback_hop": "same-harness-headless", "worker_type": "owner",
                "capability": "autopilot-code", "intensity": "strong", "harness": "codex",
                "runtime_sandbox": "workspace-write", "owner_route_id": self.route["route_id"],
                "owner_route_file": str(self.path),
                "owner_route_hash": self.route["route_hash"], "parent_sid": "fixture-parent",
                "attempt_id": self.owner, "pid": str(os.getpid()),
                "pid_start": identity["pid_start"]}
        self.jobs.write_text("now\topen\t" + str(self.repo) + "\t" + str(self.repo)
                             + "\towner\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        self.env.update({"AGENT_DISPATCH_SELF_SLUG": "owner",
            "AGENT_DISPATCH_ATTEMPT_ID": self.owner, "CODEX_THREAD_ID": "fixture-owner-session",
            "AGENT_DISPATCH_CURRENT_HARNESS": "codex", "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write", "AGENT_DISPATCH_DEPTH": "1",
            "AGENT_DISPATCH_CHILD": "1"})
        # Publish prerequisite inline completions using the normal writer.
        # This retains the compiled recipe and its execute -> review group.
        def seed(node_id):
            node = next(n for n in self.route["nodes"] if n["id"] == node_id)
            for dependency in node.get("depends_on", []):
                seed(dependency)
            if node_id in seeded:
                return
            evidence = self.output / ("fixture-" + node_id + ".md")
            evidence.write_text("fixture predecessor\n")
            with mock.patch.dict(os.environ, self.env, clear=True):
                ROUTE._publish_completion_locked(self.route, node, node_id, evidence,
                    jobs=self.jobs, attempt_id="att-inline-" + node_id + "-1",
                    attempt_metadata={"attempt_schema_version": 2,
                        "dispatch_depth": node["dispatch_depth"], "transport": "interactive",
                        "execution_surface": "inline", "registered_worker": False, "fallback_hop": "inline"})
            seeded.add(node_id)
        seeded = set()
        for dependency in next(n for n in self.route["nodes"] if n["id"] == "execute")["depends_on"]:
            seed(dependency)
        self.inline_execute("att-inline-execute-1")
        self.prompt = self.base / "prompt.md"
        self.prompt.write_text("Review original implementation.")
        self.owner_slug = "owner"

    def tearDown(self):
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None)
        failures = [tb for test, tb in (getattr(result, "failures", []) +
                                       getattr(result, "errors", [])) if test is self]
        if not failures:
            return
        target = Path('/tmp/hearting-review-batch-failures/outcomes')
        target.mkdir(parents=True, exist_ok=True)
        payload = {
            "test": self.id(),
            "tracebacks": failures,
            "jobs": self.jobs.read_text(encoding="utf-8", errors="replace"),
            "batch_inputs": [
                str(path) for path in (self.jobs.parent / "automatic-replacements" / "batch-inputs").glob("*.json")
            ],
            "partial_seals": [
                str(path) for path in (self.jobs.parent / "recovery" / "partial-group").glob("*.json")
            ],
        }
        filename = self.id().split(".")[-1] + ".json"
        (target / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def run_cmd(self, command, expected=0):
        for retry in range(3):
            stage_start = str(ROOT / 'utilities/stage-dispatch-fallback.py') in command
            before = self.atomic_state() if stage_start else None
            result = subprocess.run(command, env=self.env, text=True, capture_output=True,
                                    timeout=120 if stage_start else 45)
            if result.returncode and (stage_start or
                    str(ROOT / 'utilities/dispatch_completion_join.py') in command):
                self.failure_snapshot(command, result)
            receipts = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            refusal = receipts[-1] if receipts else {}
            reobserve = (expected == 0 and result.returncode == 78
                and refusal.get('reason') == 'prior-attempt-unverifiable'
                and str(refusal.get('detail', '')).endswith(':attempt-descendant-unverifiable'))
            if not stage_start or not reobserve or retry == 2:
                break
            self.assertEqual(self.atomic_state(), before)
            print('Public CLI atomic procfs refusal before re-observation: ' +
                  json.dumps(refusal, sort_keys=True), file=sys.stderr)
            time.sleep(0.2)
        self.assertIn(result.returncode, expected if isinstance(expected, tuple) else (expected,),
                      result.stdout + result.stderr)
        return result

    def failure_snapshot(self, command, result):
        """Keep raw refusal and immediate observations; never diagnose production."""
        # The isolated suite deliberately filters caller environment. Keep a
        # bounded diagnostic snapshot under /tmp when that opt-in is absent.
        root = os.environ.get('REVIEW_BATCH_EVIDENCE_DIR') or '/tmp/hearting-review-batch-failures'
        target = Path(root) / self.id().split('.')[-1] / str(time.time_ns())
        target.mkdir(parents=True)
        if os.environ.get('REVIEW_BATCH_EVIDENCE_DIR'):
            guard = Path(os.environ['AGENT_HOME']) / 'adapters/codex/bin/preflight.sh'
            subprocess.run([str(guard), 'write', str(target / 'failure.json')], check=True)
        observations = []
        attempts = {command[index + 1] for index, value in enumerate(command[:-1])
                    if value == '--attempt-id'}
        with mock.patch.dict(os.environ, self.env, clear=True):
            for attempt, (fields, meta) in REPLACEMENT._rows(
                    self.jobs.read_text().splitlines()).items():
                if attempts and attempt not in attempts:
                    continue
                item = {'attempt_id': attempt, 'status': fields[1],
                        'metadata': {key: value for key, value in meta.items()
                                     if key in ('route_node', 'pid', 'pid_start', 'pgid',
                                                'note', 'failure_class', 'classifier_source',
                                                'summary_owner_pid', 'summary_owner_pid_start',
                                                'reap_watch_pid', 'terminal_conflict')}}
                try:
                    item['quiescence'] = dataclasses.asdict(DC.attempt_process_quiescence(meta))
                    item['tagged_descendants'] = dataclasses.asdict(DC.attempt_tagged_descendants(meta))
                    if str(meta.get('pgid', '')).isdigit():
                        item['process_group'] = dataclasses.asdict(
                            DC.process_group_observation(int(meta['pgid'])))
                    item['writers'] = {}
                    for key in ('log_file', 'summary_state_file'):
                        path = Path(meta.get(key, ''))
                        if path.is_file() and path.is_relative_to(self.base):
                            item['writers'][key] = {'path': str(path),
                                'mtime_ns': path.stat().st_mtime_ns,
                                'tail': path.read_bytes()[-8192:].decode(errors='replace')}
                    marker = self.jobs.parent / 'completion' / self.route['route_id'] / (str(meta.get('route_node')) + '.json')
                    if marker.is_file():
                        item['marker'] = {'mtime_ns': marker.stat().st_mtime_ns,
                                          'value': json.loads(marker.read_text())}
                except Exception as exc:
                    item['observation_error'] = repr(exc)
                observations.append(item)
        payload = {'scope': 'isolated fixture only; immediate post-receipt snapshot, '
                   'not a refusal-time production process snapshot',
                   'observed_at': time.time(), 'command': command,
                   'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
                   'observations': observations}
        (target / 'failure.json').write_text(json.dumps(payload, indent=2, default=str))

    def _case_test_one_started_one_missing_admitted_resume(self):
        self.admitted_resume_scenario('off', 1)

    def _case_test_terminal_no_start_reuses_group_start_for_the_exact_gap(self):
        first = self.launch()
        self.assertEqual((first['state'], first['newly_started']), ('launched', 2), first)
        gap = next(leg for leg in first['legs'] if leg['node'] == 'impl-review')
        peers = [leg for leg in first['legs'] if leg['node'] != 'impl-review']
        self.settle(first)
        completion = ROUTE.completion_dir(self.route['route_id'], jobs=self.jobs)
        for path in completion.glob('impl-review.*.json'):
            path.unlink()
        (completion / 'impl-review.json').unlink(missing_ok=True)
        ROUTE._attempt_completion_path(
            self.route, gap['node'], gap['attempt_id'], jobs=self.jobs).unlink(missing_ok=True)
        self.fixture_row(gap['attempt_id'], status='done',
            # Counterfactual registration-only input: a previously settled
            # transport is not evidence that this synthetic gap ran. Retaining
            # its reap/group identity would correctly contradict no-start.
            remove=('pid', 'pid_start', 'pid_scope', 'pid_host', 'pid_host_start',
                    'pid_host_ns', 'pid_host_proof', 'pid_ns', 'pid_observer_ns',
                    'pgid', 'pgid_host', 'launch_cgroup_b64', 'launch_cgroup_ns',
                    'group_reap_proof', 'group_reap_pgid',
                    'attempt_descendant_proof', 'attempt_descendant_observer_ns',
                    'attempt_descendant_residue', 'attempt_descendant_residue_count',
                    'attempt_descendant_residue_basis', 'attempt_descendant_residue_at',
                    'reap_watch', 'reap_watch_pid', 'post_exit_receipt_substitute',
                    'cleanup_receipt_b64', 'cleanup_receipt_digest',
                    'process_exit', 'terminal_event', 'detected_by', 'failure_class',
                    'completion_marker', 'completion_marker_history'),
            launch_claimed='0', launch_started='0', launch_outcome='never-launched',
            note='opencode-context-limit-unavailable')
        peer_rows = {leg['attempt_id']: next(line for line in self.jobs.read_text().splitlines()
            if 'attempt_id=' + leg['attempt_id'] + ',' in line) for leg in peers}
        peer_markers = {leg['node']: (self.jobs.parent / 'completion' / self.route['route_id'] /
            (leg['node'] + '.json')).read_bytes() for leg in peers}

        resumed = self.launch()
        # The existing partial receipt: the PASS peer is reused, only the gap starts.
        self.assertEqual((resumed['state'], resumed['requested'], resumed['newly_started'],
                          resumed['existing']), ('idempotent-mixed', 2, 1, 1), resumed)
        relaunched = next(leg for leg in resumed['legs'] if leg['node'] == gap['node'])
        self.assertNotEqual(relaunched['attempt_id'], gap['attempt_id'])
        self.assertEqual(relaunched['adapter'], gap['adapter'])
        self.assertEqual(relaunched['child_spawned'], '1')
        for leg in resumed['legs']:
            if leg['node'] != gap['node']:
                self.assertEqual(leg['reason'], 'reused-successful-peer', resumed)
        rows = self.jobs.read_text().splitlines()
        for attempt, original in peer_rows.items():
            self.assertIn(original, rows)
        for node, original in peer_markers.items():
            self.assertEqual((self.jobs.parent / 'completion' / self.route['route_id'] /
                              (node + '.json')).read_bytes(), original)

    def _case_test_both_zero_admitted_resume_with_gates_on(self):
        self.admitted_resume_scenario('on', 2)

    def _case_test_closure_capped_peer_pending_sibling(self):
        self.admitted_resume_scenario('on', 1, closure=True)

    def _case_test_live_peer_preserved_and_unknown_unsettled_refused(self):
        self.admitted_resume_scenario('off', 1, peer_live=True)

    def admitted_resume_scenario(self, gates, missing_count, closure=False, peer_live=False):
        self.env['HEARTING_GATES'] = gates
        first = self.launch()
        self.settle(first)
        self.revise_execute(first, 2)
        if closure:
            self.prompt.write_text('Second settled generation')
            first = self.launch()
            self.settle(first)
            self.revise_execute(first, 4)
            same_source_rows = self.jobs.read_bytes()
            exhausted = self.launch(expected=65)
            self.assertEqual(exhausted['reason'], 'review-round-budget-exhausted', exhausted)
            self.assertEqual(self.jobs.read_bytes(), same_source_rows)
            # The partial-start/resume case needs an eligible generation. Move
            # the source to B and reopen it through the ordinary execute path.
            (self.repo / 'x').write_text('source B\n')
            self.revise_execute(first, 6)
        self.prompt.write_text('Exact admitted pending generation ' + gates)
        # Inject a refusal at the outer child-command boundary, after full
        # group admission/reservation. Shared selection/admission/readiness are
        # real; subsequent resume launches the real dispatch-node and wrapper.
        fault_dir = self.base / 'fault'
        fault_dir.mkdir()
        (fault_dir / 'sitecustomize.py').write_text('import os, subprocess, sys\nOriginal = subprocess.Popen\nclass FaultPopen(Original):\n    def __init__(self, args, *a, **kw):\n        if isinstance(args, list) and any(str(x).endswith(\'/utilities/dispatch-node.py\') for x in args):\n            node = args[args.index(\'--node\') + 1]\n            if node in os.environ.get(\'FIXTURE_REFUSE_NODES\', \'\').split(\',\'):\n                args = [sys.executable, \'-c\', "print(\'check=failed\'); print(\'reason=prior-attempt-unverifiable\'); print(\'detail=fixture-injected-after-reservation\'); print(\'child_spawned=0\'); print(\'registered=0\'); print(\'started=0\'); raise SystemExit(78)"]\n        super().__init__(args, *a, **kw)\nsubprocess.Popen = FaultPopen\n')
        self.env['PYTHONPATH'] = str(fault_dir)
        self.env['FIXTURE_REFUSE_NODES'] = ','.join(
            leg['node'] for leg in first['legs'][:missing_count])
        partial = self.launch(expected=70)
        self.assertEqual(partial['newly_started'], 2 - missing_count, partial)
        self.assertTrue(all(leg['exit_code'] == 78 for leg in partial['legs']
                            if leg['child_spawned'] == '0'), partial)
        ids = {leg['attempt_id'] for leg in partial['legs']}
        self.env.pop('FIXTURE_REFUSE_NODES')
        self.env.pop('PYTHONPATH')
        started = [leg for leg in partial['legs'] if leg['child_spawned'] == '1']
        if started:
            self.settle({'legs': started})
            budget = self.admission(started[0]['node']).budget
            expected = ('admit', 1) if closure else ('exhausted', 2)
            self.assertEqual((budget.state, budget.verdict_rounds), expected, repr(budget))
        changed = self.prompt.read_text()
        self.prompt.write_text(changed + ' changed without revision')
        self.assert_atomic_refusal(reason='replacement-batch-source-drift')
        self.prompt.write_text(changed)
        settled_rows = self.jobs.read_bytes()
        if peer_live:
            peer = started[0]
            self.fixture_row(peer['attempt_id'], terminal_conflict='1',
                             conflicting_terminal_note='terminal-observation-unknown',
                             conflicting_failure_class='unknown')
            self.assert_atomic_refusal(reason='round-unsettled')
            self.jobs.write_bytes(settled_rows)
            self.fixture_row(peer['attempt_id'], status='open', remove=('pid_start',), pid='999999999')
            before = self.atomic_state()
            unknown = self.launch(expected=70)
            self.assertEqual(unknown['newly_started'], 0, unknown)
            self.assertTrue(any('existing-active-attempt-' in leg.get('reason', '')
                                for leg in unknown['legs']), unknown)
            self.assertEqual(self.atomic_state(), before)
            self.jobs.write_bytes(settled_rows)
        with self.live_fixture_attempt(peer['attempt_id']) if peer_live else contextlib.nullcontext():
            live_row = next((line for line in self.jobs.read_text().splitlines()
                             if peer_live and 'attempt_id=' + peer['attempt_id'] + ',' in line), None)
            resumed = self.launch()
            self.assertEqual({leg['attempt_id'] for leg in resumed['legs']}, ids)
            self.assertEqual(resumed['newly_started'], missing_count, resumed)
            if peer_live:
                self.assertIn(live_row, self.jobs.read_text().splitlines())
        # The disposable live process has consumed EOF and been reaped; only
        # its exact row was restored. The missing leg's normal writers remain.
        self.settle(resumed)
        self.assertEqual(self.launch()['newly_started'], 0)

    def route_cmd(self, action, *args):
        return self.run_cmd([sys.executable, str(ROOT / "utilities/capability-route.py"), action,
                            "--route", str(self.path), "--jobs", str(self.jobs), *args])

    def inline_execute(self, attempt):
        return self.inline("execute", self.evidence, attempt)

    def inline(self, node, evidence, attempt):
        depth = next(n for n in self.route["nodes"] if n["id"] == node)["dispatch_depth"]
        # Inline completion names axes, so --jobs would request a registered
        # row close. Its common readers still inherit the exact canonical jobs.
        return self.run_cmd([sys.executable, str(ROOT / "utilities/capability-route.py"),
            "complete", "--route", str(self.path), "--inline", "--node", node, "--evidence", str(evidence),
            "--attempt-id", attempt, "--dispatch-depth", str(depth), "--transport", "interactive",
            "--execution-surface", "inline", "--registered-worker", "0", "--fallback-hop", "inline"])

    def launch(self, expected=0, slug="fixture-review"):
        result = self.run_cmd([sys.executable, str(ROOT / "utilities/stage-dispatch-fallback.py"),
            "--route", str(self.path), "--node", "impl-review", "--slug", slug,
            "--parent", self.owner_slug, "--jobs", str(self.jobs), "--prompt-file", str(self.prompt),
            "--start"], expected)
        receipts = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        return receipts[-1] if receipts else result.stdout

    def _case_test_two_pass_revision_inline_next_generation_and_cap_replay(self):
        self.revision_scenario()

    def _case_test_revision_generation_and_replay_with_gates_on(self):
        self.env["HEARTING_GATES"] = "on"
        self.revision_scenario()

    def _case_test_foreign_registry_cannot_override_the_sealed_jobs(self):
        foreign = self.base / "foreign/jobs.log"
        foreign.parent.mkdir()
        foreign.touch()
        original = self.jobs.read_bytes()
        result = self.run_cmd([sys.executable, str(ROOT / "utilities/stage-dispatch-fallback.py"),
            "--route", str(self.path), "--node", "impl-review", "--parent", "owner",
            "--jobs", str(foreign), "--prompt-file", str(self.prompt), "--start"], expected=65)
        self.assertIn("noncanonical-nested-jobs", result.stdout + result.stderr)
        self.assertEqual(self.jobs.read_bytes(), original)
        self.assertEqual(foreign.read_bytes(), b"")

    def marker(self, node):
        path = self.jobs.parent / "completion" / self.route["route_id"] / (node + ".json")
        return json.loads(path.read_text())

    def fixture_row(self, attempt, *, status=None, remove=(), **changes):
        """Seed a physical observation, never a native transport claim."""
        with REPLACEMENT._locked(self.jobs) as lines:
            for index, line in enumerate(lines):
                fields = line.split("\t")
                meta = DC.parse_registry_metadata(fields[5])
                if meta.get("attempt_id") != attempt:
                    continue
                for key in remove:
                    meta.pop(key, None)
                meta.update(changes)
                fields[1] = status or fields[1]
                fields[5] = ",".join(k + "=" + str(v) for k, v in meta.items())
                lines[index] = "\t".join(fields)
                DC._atomic_registry_replace(self.jobs, lines)
                return meta
        self.fail("missing fixture row " + attempt)

    @contextlib.contextmanager
    def live_fixture_attempt(self, attempt):
        original = next(line for line in self.jobs.read_text().splitlines()
                        if DC.parse_registry_metadata(line.split('\t')[5]).get('attempt_id') == attempt)
        process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**self.env, 'AGENT_DISPATCH_ATTEMPT_ID': attempt}, start_new_session=True)
        try:
            identity = DC.process_launch_identity(process.pid)
            self.assertTrue(identity['pid_start'])
            meta = REPLACEMENT._rows(self.jobs.read_text().splitlines())[attempt][1]
            remove = tuple(key for key in meta if key.startswith(('cleanup_receipt_', 'group_reap_',
                'attempt_descendant_')) or key in ('note', 'failure_class', 'launch_outcome'))
            self.fixture_row(attempt, status='open', remove=remove, **identity)
            with mock.patch.dict(os.environ, self.env, clear=True):
                self.assertEqual(DC.attempt_process_quiescence(
                    REPLACEMENT._rows(self.jobs.read_text().splitlines())[attempt][1]).state, 'live')
            yield
        finally:
            process.stdin.close()
            process.wait(timeout=5)
            # A concurrently resumed sibling may publish after our snapshot.
            # Restore only this settled peer, under the normal registry lock.
            with REPLACEMENT._locked(self.jobs) as lines:
                for index, line in enumerate(lines):
                    meta = DC.parse_registry_metadata(line.split('\t')[5])
                    if meta.get('attempt_id') == attempt:
                        self.assertEqual((meta.get('pid'), meta.get('pid_start')),
                                         (str(process.pid), identity['pid_start']))
                        lines[index] = original
                        DC._atomic_registry_replace(self.jobs, lines)
                        break
                else:
                    self.fail('missing disposable fixture row ' + attempt)

    def replace_owner(self):
        # The controlled process crosses the real launch-start writer, exits,
        # and is closed as an executed BLOCKED owner before its correction.
        # Reservation, reciprocal binding and successor claim remain real.
        args = SimpleNamespace(attempt_id=self.owner, jobs_path=self.jobs,
            worktree=str(self.repo), owner_route_binding=SimpleNamespace(route_id=self.route['route_id']),
            replacement_input_argv=['--start', '--attempt-id', self.owner,
                                    '--prompt-text', 'Continue this route'],
            worker_type='owner', capability='autopilot-code', intensity='strong',
            dispatch_depth=1, parent_session_id='fixture-parent')
        with mock.patch.dict(os.environ, self.env, clear=True):
            fragment = REPLACEMENT.seal_launch_input(args, 'codex', 'Continue this route')
            self.fixture_row(self.owner, **DC.parse_registry_metadata(fragment))
            owner_process = subprocess.Popen(
                [sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**self.env, 'AGENT_DISPATCH_ATTEMPT_ID': self.owner},
                start_new_session=True,
            )

            def close_fixture_owner():
                if owner_process.poll() is None:
                    if owner_process.stdin and not owner_process.stdin.closed:
                        owner_process.stdin.close()
                    try:
                        owner_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        owner_process.terminate()
                        owner_process.wait(timeout=5)

            self.addCleanup(close_fixture_owner)
            identity = DC.process_launch_identity(owner_process.pid)
            self.fixture_row(self.owner, status='open',
                remove=('pid', 'pid_start', 'pgid', 'pid_ns', 'pid_observer_ns'),
                **identity, launch_claimed='1', launch_started='0',
                launch_fence='registry-v1')
            DC.mark_attempt_launch_started(self.jobs, self.owner, owner_process.pid)
            OWNER_INPUT.initialize_owner_input(self.jobs, self.owner, 'codex-app-server')
            owner_process.stdin.close()
            self.assertEqual(owner_process.wait(timeout=5), 0)
            self.assertTrue(DC.close_attempt_row(
                self.jobs, self.owner, 'dead-worker-blocked',
                evidence={'failure_class': 'blocked'}))
            correction = OWNER_INPUT.submit(self.jobs, self.owner,
                'Preserve completed reviews; correct execute and run bounded closure.', 'correction-1')
            self.assertTrue(correction['retained'])
            # Summary observers inherit the original owner tag and drain their
            # final refresh after the peer joins. Observe their actual exit;
            # do not waive quiescence or terminate a live observer.
            deadline = time.monotonic() + 90
            while True:
                probe = DC.attempt_tagged_descendants(REPLACEMENT._rows(
                    self.jobs.read_text().splitlines())[self.owner][1])
                if probe.state == 'empty' or time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
            self.assertEqual(probe.state, 'empty', repr(probe))
            record = self.owner_operation(lambda: REPLACEMENT.claim(self.jobs, self.owner))
            self.assertEqual(record['proof']['death_kind'], REPLACEMENT.CORRECTED)
            source = REPLACEMENT._rows(self.jobs.read_text().splitlines())[self.owner][1]
            replay = REPLACEMENT.launch_input(self.jobs, self.owner, source)
            original_rows = self.jobs.read_bytes()
            original = self.historical_bytes()
            successor = record['replacement_attempt_id']
            args.attempt_id = successor
            args.replacement_input_argv = REPLACEMENT._replacement_argv(record, source, replay)
            candidate = {**source, 'attempt_id': successor, 'automatic_retry_of': self.owner,
                         **DC.process_launch_identity(os.getpid())}
            for key in ('note', 'failure_class', 'launch_outcome', 'replacement_input_digest',
                        'replacement_family_id', 'replacement_claim_digest', 'replacement_attempt_id'):
                candidate.pop(key, None)
            candidate.update(DC.parse_registry_metadata(REPLACEMENT.seal_launch_input(
                args, 'codex', REPLACEMENT.route_authority.recovery_task(record, source, replay))))
            self.assertEqual(self.owner_operation(lambda: REPLACEMENT.admission(
                self.jobs, self.jobs.read_text().splitlines(), candidate)), record)
            row = 'now\topen\t' + str(self.repo) + '\t' + str(self.repo) + '\tcurrent-owner\t' + ",".join(
                k + '=' + str(v) for k, v in candidate.items())
            self.assertTrue(self.owner_operation(lambda: DC.claim_attempt_row(self.jobs, successor, row, launch=False)))
            self.assertFalse(self.owner_operation(lambda: DC.claim_attempt_row(self.jobs, successor, row, launch=False)))
            registered = REPLACEMENT._rows(self.jobs.read_text().splitlines())[successor][1]
            self.assertEqual(registered['launch_claimed'], '0')
            self.assertEqual(self.owner_operation(lambda: REPLACEMENT.admission(
                self.jobs, self.jobs.read_text().splitlines(), registered)), record)
            # The fixture takes over the already-created row as its active owner.
            self.fixture_row(successor, status='open', launch_claimed='1', launch_started='1')
            effective, lineage = REPLACEMENT.effective_attempts(self.jobs, {self.owner})
            self.assertEqual(effective, {successor})
            self.assertEqual(lineage[0]['replacement_attempt_id'], successor)
            self.assertEqual(REPLACEMENT.launch_input(self.jobs, self.owner, source), replay)
            self.assertTrue(self.jobs.read_bytes().startswith(original_rows))
            for path, data in original.items():
                self.assertEqual(path.read_bytes(), data, str(path))
        self.owner = successor
        self.owner_slug = 'current-owner'
        self.env.update(AGENT_DISPATCH_ATTEMPT_ID=successor, AGENT_DISPATCH_SELF_SLUG=self.owner_slug)

    def owner_operation(self, operation):
        # Re-observe a procfs refusal with exactly the same authority/input.
        # A refusal remains recorded and must have made no durable mutation.
        for retry in range(3):
            before = self.atomic_state()
            try:
                return operation()
            except DC.DispatchContractError as exc:
                if exc.reason != 'replacement-owner-child-unsettled' or retry == 2:
                    raise
                self.assertEqual(self.atomic_state(), before)
                rows = REPLACEMENT._rows(self.jobs.read_text().splitlines())
                for child in REPLACEMENT._owned_children(rows, self.owner):
                    fields, meta = rows[child]
                    self.assertEqual(fields[1], 'done')
                    proof = DC.attempt_process_quiescence(meta, terminal_receipt=True)
                    self.assertNotEqual(proof.state, 'live', repr(proof))
                print('Owner authority re-observation after atomic refusal: ' + str(exc), file=sys.stderr)
                time.sleep(0.2)

    def assert_selection(self, receipt):
        rows = REPLACEMENT._rows(self.jobs.read_text().splitlines())
        traces = {item['attempt']: item for item in map(json.loads, self.trace.read_text().splitlines())}
        self.assertEqual(self.route['continuation_budget']['review_round_cap'], 2)
        for leg in receipt['legs']:
            node = next(n for n in self.route['nodes'] if n['id'] == leg['node'])
            meta = rows[leg['attempt_id']][1]
            self.assertEqual(meta['parent_attempt_id'], self.owner)
            self.assertEqual(meta['harness'], leg['adapter'])
            for key in ('model_profile', 'intensity', 'dispatch_depth'):
                self.assertEqual(meta[key], str(node.get(key, self.route['effective_intensity'])), key)
            self.assertEqual(leg['perspective'], node['perspective'])
            self.assertEqual(meta['batch_perspective'], node['perspective'])
            self.assertIn(meta['fallback_hop'], [h['fallback_hop'] for h in node['fallback_hops']])
            self.assertEqual(traces[leg['attempt_id']]['jobs'], str(self.jobs))
            self.assertEqual(traces[leg['attempt_id']]['node'], node)

    def historical_bytes(self):
        # Canonical completion files advance; immutable history/input/links do
        # not. Exclude lock files and live transport observations.
        return {p: p.read_bytes() for directory in ("automatic-replacements", "review-inputs", "completion")
                for p in (self.jobs.parent / directory).rglob("*.json")
                if directory != "completion" or (".att-" in p.name and p.name.endswith(".attempt.json"))
                or (p.stem.rsplit(".", 1)[-1].isdigit())}

    def admission(self, node_id):
        node = next(n for n in self.route["nodes"] if n["id"] == node_id)
        with mock.patch.dict(os.environ, self.env, clear=True):
            return NODE.admit_round(self.route, node, self.jobs, owner_attempt_id=self.owner)

    def revise_execute(self, receipt, sequence):
        self.evidence.write_text("implementation correction " + str(sequence) + "\n")
        prior_markers = {}
        for leg in receipt["legs"]:
            path = self.jobs.parent / 'completion' / self.route['route_id'] / (leg['node'] + '.json')
            if path.is_file():
                prior_markers[leg['node']] = (path.read_bytes(), json.loads(path.read_text()))
        self.route_cmd("revise", "--node", "execute", "--evidence", str(self.evidence),
            "--basis", "review-findings", "--answers", ",".join(leg["attempt_id"] for leg in receipt["legs"]),
            "--author-attempt-id", self.owner)
        self.assertEqual(self.marker("execute")["sequence"], sequence)
        for leg in receipt["legs"]:
            path = self.jobs.parent / 'completion' / self.route['route_id'] / (leg['node'] + '.json')
            if leg['node'] not in prior_markers:
                continue  # A real FAIL has a verdict row, not a successful marker.
            prior_bytes, prior = prior_markers[leg['node']]
            tombstone = self.marker(leg["node"])
            if prior.get("state") == "superseded-by-upstream-revision":
                self.assertEqual(path.read_bytes(), prior_bytes)
            else:
                self.assertEqual(tombstone["state"], "superseded-by-upstream-revision")
                self.assertEqual(tombstone["superseded_by"], {"node": "execute", "sequence": sequence})
        self.inline_execute("att-inline-execute-" + str(sequence + 1))
        self.assertEqual(self.marker("execute")["sequence"], sequence + 1)

    def revision_scenario(self):
        first = self.launch()
        self.assertEqual(first["newly_started"], 2, first)
        self.assertEqual(len(first["legs"]), 2)
        for leg in first["legs"]:
            self.assertEqual(leg["adapter"], "claude")
        self.settle(first)
        second = self.launch()
        self.assertEqual(second["newly_started"], 0, second)
        original_rows = self.jobs.read_bytes()
        from recovery_history import batch_input_path
        metadata = REPLACEMENT._rows(self.jobs.read_text().splitlines())[first["legs"][0]["attempt_id"]][1]
        input_path = batch_input_path(self.jobs, metadata["batch_manifest_sha256"])
        original_input = input_path.read_bytes()
        input_path.write_bytes(b"{")
        corrupt = self.launch(expected=65)
        self.assertEqual(corrupt["reason"], "replacement-record-unreadable", corrupt)
        self.assertEqual(self.jobs.read_bytes(), original_rows)
        input_path.write_bytes(original_input)
        original = self.historical_bytes()
        self.prompt.write_text("Closure review after corrected implementation.")
        refused = self.launch(expected=65)
        self.assertEqual(refused["reason"], "replacement-batch-source-drift", refused)
        self.assertEqual(self.jobs.read_bytes(), original_rows)
        peer_rows = [line for line in self.jobs.read_text().splitlines()
                     if any('attempt_id=' + leg['attempt_id'] + ',' in line for leg in first['legs'])]
        self.replace_owner()
        for row in peer_rows:
            self.assertIn(row, self.jobs.read_text().splitlines())
        original_rows = self.jobs.read_bytes()
        self.revise_execute(first, 2)
        for leg in first["legs"]:
            admission = self.admission(leg["node"])
            self.assertFalse(admission.gate_unmet)
            self.assertTrue(admission.requires_next_round)
            self.assertEqual((admission.budget.verdict_rounds, admission.budget.next_round), (1, 2))
        round_two = self.launch()
        self.assertEqual((round_two["admitted"], round_two["newly_started"]), (2, 2), round_two)
        self.assertTrue({leg["attempt_id"] for leg in first["legs"]}.isdisjoint(
            {leg["attempt_id"] for leg in round_two["legs"]}))
        # Finish both physical launches and their normal terminal writers
        # before taking the fixture backup. Otherwise a delayed writer can
        # settle either real leg during the duplicate's atomic byte check, and
        # restoring that backup would erase its legitimate final settlement.
        self.settle(round_two)
        for leg in round_two['legs']:
            fields, meta = REPLACEMENT._rows(self.jobs.read_text().splitlines())[leg['attempt_id']]
            self.assertEqual(fields[1], 'done')
            self.assertEqual(meta['note'], 'completed-marker')
            self.assertTrue(DC.verdict_pass(meta), repr(meta))
            marker = self.marker(leg['node'])
            self.assertEqual(marker['attempt_id'], leg['attempt_id'])
            node = next(n for n in self.route['nodes'] if n['id'] == leg['node'])
            with mock.patch.dict(os.environ, self.env, clear=True):
                readiness = DC.completion_attempt_readiness(self.route, node, marker, self.jobs)
            self.assertEqual(readiness.state, 'ready', repr(readiness))
        # A current PASS marker does not waive a genuinely live physical
        # observation. Claims, generations, inputs and admission remain real.
        with self.live_fixture_attempt(round_two['legs'][0]['attempt_id']):
            before_duplicate = self.atomic_state()
            live = self.launch(expected=78, slug="another-display")
            self.assertEqual(live["reason"], "prior-attempt-still-live")
            self.assertEqual(self.atomic_state(), before_duplicate)
        self.assert_selection(round_two)
        settled_rows = self.jobs.read_bytes()
        before_replay = self.atomic_state()
        replay = self.launch(slug="another-display")
        self.assertEqual((replay["admitted"], replay["newly_started"]), (0, 0), replay)
        self.assertEqual([leg["attempt_id"] for leg in replay["legs"]],
                         [leg["attempt_id"] for leg in round_two["legs"]])
        self.assertEqual(self.jobs.read_bytes(), settled_rows)
        self.assertEqual(self.atomic_state(), before_replay)
        for leg in round_two["legs"]:
            admission = self.admission(leg["node"])
            self.assertEqual(admission.budget.state, "exhausted")
            self.assertFalse(admission.requires_next_round)
        self.assertTrue(self.jobs.read_bytes().startswith(original_rows))
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data, str(path))
        # An evidence-only revision keeps the same source subject and cannot
        # buy a third verdict beyond that subject's cap.
        self.revise_execute(round_two, 4)
        self.prompt.write_text("A third verdict for source A cannot be admitted.")
        rows = self.jobs.read_bytes()
        self.assert_atomic_refusal(reason="review-round-budget-exhausted")
        self.assertEqual(self.jobs.read_bytes(), rows)

        # A real source change opens the ordinary independent-review path for
        # B. Its two verdicts then exhaust B's own budget; a same-source
        # revision still cannot admit another one.
        (self.repo / "x").write_text("source B\n")
        self.revise_execute(round_two, 6)
        self.prompt.write_text("Independent review for source B.")
        round_three = self.launch()
        self.settle(round_three)
        self.assertEqual(self.launch()["newly_started"], 0)
        self.revise_execute(round_three, 8)
        for leg in round_three["legs"]:
            budget = self.admission(leg["node"]).budget
            self.assertEqual((budget.state, budget.verdict_rounds, budget.round_kind),
                             ("admit", 1, "closure-check"), repr(budget))
        self.prompt.write_text("Second in-cap verdict for source B.")
        round_four = self.launch()
        self.settle(round_four)
        self.assertEqual(self.launch()["newly_started"], 0)
        for leg in round_four["legs"]:
            budget = self.admission(leg["node"]).budget
            self.assertEqual((budget.state, budget.verdict_rounds), ("exhausted", 2), repr(budget))
        self.revise_execute(round_four, 10)
        self.prompt.write_text("A third verdict for source B cannot be admitted.")
        rows = self.jobs.read_bytes()
        spent = self.assert_atomic_refusal(reason="review-round-budget-exhausted")
        self.assertEqual(self.jobs.read_bytes(), rows)

    def _case_test_death_partial_replacement_then_revision_and_cap_replay(self):
        self.env.update(FIXTURE_DEAD_NODE="impl-review", FIXTURE_DEATH_ONCE=str(self.base / "death-once"))
        first = self.launch()
        gap = next(leg for leg in first["legs"] if leg["node"] == "impl-review")
        sibling = next(leg for leg in first["legs"] if leg["node"] != "impl-review")
        self.settle({"legs": [sibling]})
        self.run_cmd([sys.executable, str(ROOT / "utilities/dispatch-registry.py"),
            "reconcile", "--jobs", str(self.jobs), "--attempt", gap["attempt_id"],
            "--only-exact-dead", "--apply", "--agent-home", str(ROOT)])
        sibling_row = next(line for line in self.jobs.read_text().splitlines()
                           if "attempt_id=" + sibling["attempt_id"] + "," in line)
        with mock.patch.dict(os.environ, self.env, clear=True):
            record = REPLACEMENT.claim(self.jobs, gap["attempt_id"])
            source = REPLACEMENT._rows(self.jobs.read_text().splitlines())[gap["attempt_id"]][1]
            replay = REPLACEMENT.launch_input(self.jobs, gap["attempt_id"], source)
            command = REPLACEMENT_BATCH.command(self.jobs, record, source, replay)
        replaced = self.run_cmd(command)
        receipt = json.loads(next(line for line in reversed(replaced.stdout.splitlines()) if line.startswith("{")))
        self.assertEqual((receipt["admitted"], receipt["newly_started"]), (1, 1), receipt)
        replacement = next(leg for leg in receipt["legs"] if leg["node"] == gap["node"])
        self.assertEqual(replacement["attempt_id"], record["replacement_attempt_id"])
        self.settle({"legs": [replacement]})
        self.assertIn(sibling_row, self.jobs.read_text().splitlines())
        membership = self.admission(gap["node"])
        self.assertEqual(membership.budget.verdict_rounds, 1)
        self.assertEqual(membership.physical_attempt_ids, frozenset((gap["attempt_id"], replacement["attempt_id"])))
        original_rows = self.jobs.read_bytes()
        original = self.historical_bytes()
        current = {"legs": [replacement, sibling]}
        self.revise_execute(current, 2)
        self.prompt.write_text("Closure review after partial death replacement and correction.")
        round_two = self.launch()
        self.assertEqual(round_two["newly_started"], 2, round_two)
        self.settle(round_two)
        replay = self.launch(slug="changed-label")
        self.assertEqual((replay["admitted"], replay["newly_started"]), (0, 0), replay)
        self.assertEqual([leg["attempt_id"] for leg in replay["legs"]],
                         [leg["attempt_id"] for leg in round_two["legs"]])
        self.assertTrue(self.jobs.read_bytes().startswith(original_rows))
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data, str(path))

    def atomic_state(self):
        return (self.jobs.read_bytes(), self.trace.read_bytes(),
                {p: p.read_bytes()
                 for directory in ('automatic-replacements', 'batch-reservations',
                                   'review-inputs', 'batch-inputs')
                 for p in (self.jobs.parent / directory).rglob('*')
                 if p.is_file() and not p.name.endswith('.lock')},
                {p: p.read_bytes() for p in (self.art / '.runtime/model-worker-governor').rglob('*.json')})

    def assert_atomic_refusal(self, *, expected=65, reason):
        before = self.atomic_state()
        refused = self.launch(expected=expected)
        self.assertEqual(refused['reason'], reason, refused)
        self.assertEqual(self.atomic_state(), before)
        return refused

    def _case_test_r2_unknown_deferred_and_verdictless_bound_are_atomic(self):
        first = self.launch()
        self.settle(first)
        self.revise_execute(first, 2)
        self.prompt.write_text('Second generation safety matrix.')
        second = self.launch()
        self.settle(second)
        rows = self.jobs.read_bytes()
        target = second['legs'][0]['attempt_id']
        # Synthetic observations are isolated fixture setup; all refusal readers
        # and writers below are the public CLI, with no dependency mocks.
        self.fixture_row(target, status='open', remove=('pid_start',), pid='999999999')
        meta = REPLACEMENT._rows(self.jobs.read_text().splitlines())[target][1]
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(DC.attempt_process_quiescence(meta).state, 'unverifiable')
        self.assert_atomic_refusal(expected=78, reason='prior-attempt-still-live')
        self.jobs.write_bytes(rows)
        self.fixture_row(target, terminal_conflict='1',
                         conflicting_terminal_note='terminal-observation-unknown',
                         conflicting_failure_class='unknown')
        self.assert_atomic_refusal(reason='round-unsettled')
        self.jobs.write_bytes(rows)
        self.fixture_row(target, remove=('completion_marker',), note='completion-deferred',
                         classifier_source='registered-wrapper-completion-transient-v1')
        self.assert_atomic_refusal(reason='round-unsettled')
        self.jobs.write_bytes(rows)
        self.fixture_row(target, note='dead-invalid-envelope', failure_class='contract')
        fields, unknown = REPLACEMENT._rows(self.jobs.read_text().splitlines())[target]
        unknown = {**unknown, 'attempt_id': 'att-fixture-unknown-binding-2',
                   'note': 'dead-missing-result'}
        # Unknown history stays unknown under a new identity and refuses before
        # it can be counted against the current source subject.
        unknown.pop('review_subject_digest', None)
        unknown.pop('review_subject_binding_digest', None)
        unknown.update(launch_claimed='0', launch_started='0', launch_outcome='never-launched')
        for key in ('pid', 'pid_start', 'pgid', 'log_file', 'completion_marker'):
            unknown.pop(key, None)
        fields[5] = ','.join(k + '=' + v for k, v in unknown.items())
        unknown_row = '\t'.join(fields)
        settled = self.jobs.read_bytes()
        with self.jobs.open('a') as handle:
            handle.write(unknown_row + '\n')
        refusal = self.assert_atomic_refusal(reason='batch-prior-binding-drift')
        self.assertTrue(refusal)
        self.jobs.write_bytes(settled)

        fields, bound = REPLACEMENT._rows(self.jobs.read_text().splitlines())[target]
        attempt_id = 'att-fixture-verdictless-bound-2'
        node = next(item for item in self.route['nodes'] if item['id'] == bound['route_node'])
        bound = {**bound, 'attempt_id': attempt_id, 'note': 'dead-missing-result'}
        bound.pop('review_subject_digest', None)
        bound.pop('review_subject_binding_digest', None)
        for key in ('batch_group', 'batch_manifest_sha256', 'batch_leg_sha256', 'batch_harness'):
            bound.pop(key, None)
        # This row models a launched, verdict-less attempt at the B=2 limit.
        # Never-started rows are excluded from the round census by design.
        # Seal this attempt from the current source with the production writer;
        # never transfer another attempt's digest or sidecar.
        bound.update(SUBJECT.seal_attempt(
            self.jobs, self.route, node, attempt_id, worktree=self.repo))
        bound.update(launch_claimed='1', launch_started='1', pid='424242',
                     launch_outcome='started')
        for key in ('pid_start', 'pgid', 'log_file', 'completion_marker'):
            bound.pop(key, None)
        fields[5] = ','.join(k + '=' + v for k, v in bound.items())
        with self.jobs.open('a') as handle:
            handle.write('\t'.join(fields) + '\n')
        refusal = self.assert_atomic_refusal(reason='review-verdictless-bound')
        self.assertIn('next_action', refusal)

    def _case_test_mixed_pass_fail_revision_through_public_batch(self):
        self.mixed_scenario(one_sided=False)

    def _case_test_mixed_one_sided_supersession_through_public_batch(self):
        self.mixed_scenario(one_sided=True)

    def mixed_scenario(self, *, one_sided):
        self.env['FIXTURE_FAIL_NODE'] = 'impl-review'
        first = self.launch(expected=(0, 70))
        self.settle(first)
        rows = REPLACEMENT._rows(self.jobs.read_text().splitlines())
        fail = next(leg for leg in first['legs'] if leg['node'] == 'impl-review')
        sibling = next(leg for leg in first['legs'] if leg['node'] != 'impl-review')
        self.assertEqual(rows[fail['attempt_id']][1]['note'], 'completed-review-blocking')
        self.assertEqual(rows[sibling['attempt_id']][1]['note'], 'completed-marker')
        original = self.historical_bytes()
        original_rows = self.jobs.read_bytes()
        self.env.pop('FIXTURE_FAIL_NODE')
        # A FAIL has no successful marker. Answering just its successful peer
        # produces a genuine one-sided supersession using the canonical writer;
        # the unaddressed FAIL still needs a round through its own admission.
        self.revise_execute({'legs': [sibling]} if one_sided else first, 2)
        if one_sided:
            self.assertEqual(self.marker(sibling['node'])['state'], 'superseded-by-upstream-revision')
            self.assertFalse((self.jobs.parent / 'completion' / self.route['route_id'] /
                              (fail['node'] + '.json')).exists())
            self.assertTrue(self.admission(fail['node']).gate_unmet)
            self.assertFalse(self.admission(sibling['node']).gate_unmet)
        for leg in first['legs']:
            self.assertEqual(self.admission(leg['node']).budget.verdict_rounds, 1)
        self.prompt.write_text('Closure after an actual mixed PASS/FAIL group.')
        second = self.launch()
        self.assertEqual(second['newly_started'], 2, second)
        self.settle(second)
        self.assert_selection(second)
        self.assertEqual(self.launch()['newly_started'], 0)
        self.assertTrue(self.jobs.read_bytes().startswith(original_rows))
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data, str(path))

    def _case_test_one_sided_revision_uses_canonical_writer_and_reader(self):
        first = self.launch()
        self.settle(first)
        original = self.historical_bytes()
        # Revising just one review output is a supported canonical writer
        # operation; its peer remains a successful current result.
        leg = first['legs'][0]
        sibling = first['legs'][1]
        node = next(n for n in self.route['nodes'] if n['id'] == leg['node'])
        evidence = self.output / node['outputs'][0]
        evidence.write_text('Correct this one review result.\n')
        self.route_cmd('revise', '--node', leg['node'], '--evidence', str(evidence),
                       '--basis', 'owner-correction', '--reason', 'One-sided review correction',
                       '--author-attempt-id', self.owner)
        self.inline(leg['node'], evidence, 'att-inline-one-sided')
        sibling_marker = self.marker(sibling['node'])
        self.assertEqual(sibling_marker['attempt_id'], sibling['attempt_id'])
        self.prompt.write_text('One-sided correction cannot authorize new whole-group input.')
        self.assert_atomic_refusal(reason='replacement-batch-source-drift')
        self.assertEqual(self.marker(sibling['node']), sibling_marker)
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data, str(path))

    def settle(self, receipt):
        command = [sys.executable, str(ROOT / "utilities/dispatch_completion_join.py"),
                   "--jobs", str(self.jobs), "--parent-attempt-id", self.owner,
                   "--timeout", "20", "--interval", "0.1", "--recover-receiptless"]
        for leg in receipt["legs"]:
            command += ["--attempt-id", leg["attempt_id"]]
        result = self.run_cmd(command)
        for leg in receipt["legs"]:
            node = next(n for n in self.route["nodes"] if n["id"] == leg["node"])
            self.assertTrue((self.output / node["outputs"][0]).is_file(),
                "\n".join(p.name + ": " + p.read_text(errors="replace")
                          for p in (self.jobs.parent / "logs").glob("*" ) if p.is_file()))
            meta = REPLACEMENT._rows(self.jobs.read_text().splitlines())[leg['attempt_id']][1]
            if meta.get('note') == 'completed-review-blocking':
                if meta.get('classifier_source') == 'completion-join-terminal-verdict-v1':
                    self.assertNotIn('failure_class', meta)
                    self.assertEqual(meta['reconcile_reason'], 'typed-review-blocking')
                else:
                    self.assertEqual(meta['failure_class'], 'fail')
                self.assertTrue(meta.get('review_artifact_b64'))
                continue
            self.route_cmd("complete", "--node", node["id"],
                "--evidence", str(self.output / node["outputs"][0]),
                "--attempt-id", leg["attempt_id"], "--reviewer-attempt", leg["attempt_id"])
            row = next(line for line in self.jobs.read_text().splitlines()
                       if "attempt_id=" + leg["attempt_id"] + "," in line)
            self.assertEqual(row.split("\t")[1], "done", result.stdout + self.jobs.read_text())
        # Completion publication may start ordinary summary observers. Join
        # the same exact attempts after publication too; a done row alone is
        # not current process readiness for the replay that follows.
        deadline = time.monotonic() + 20
        result = self.run_cmd(command)
        # A normal join settles completion writers, not the separately started
        # summary/title work. Finish it within the same finite join window
        # before byte-atomic checks.
        while True:
            observations = []
            rows = REPLACEMENT._rows(self.jobs.read_text().splitlines())
            for leg in receipt['legs']:
                meta = rows[leg['attempt_id']][1]
                pid = meta.get('summary_owner_pid', '')
                start = meta.get('summary_owner_pid_start', '')
                observation = {'attempt': leg['attempt_id'], 'pid': pid,
                               'pid_start': start, 'disposition': 'unresolved'}
                try:
                    path = Path(meta['summary_state_file'])
                    self.assertTrue(path.is_relative_to(self.base), str(path))
                    state = json.loads(path.read_text())
                    observation['state'] = state
                    if (pid.isdigit() and start
                            and meta.get('summary_owner_pid_observer_ns') == DC.process_namespace_identity()
                            and state.get('attempt_id') == leg['attempt_id']
                            and state.get('pid') == int(pid)
                            and state.get('proc_start') == start):
                        observation['process'] = DC.process_observation(int(pid))
                        observation['disposition'] = DC.process_identity_disposition(int(pid), start)
                    observation['settled'] = (observation['disposition'] == 'dead'
                        and state.get('status') == 'terminal'
                        and state.get('final_refresh_complete') is True)
                except (KeyError, OSError, ValueError) as exc:
                    observation.update(settled=False, error=str(exc))
                observations.append(observation)
            # Read the normal governor state without status/reclaim transactions.
            # A retained lease is capacity bookkeeping, not writer liveness.
            # Observe its exact process and group; leave the record untouched.
            try:
                governor = json.loads((Path(self.env['AGENT_MODEL_GOVERNOR_ROOT']) / 'state.json').read_text())
                titles = {}
                for token, lease in governor['leases'].items():
                    if lease['class'] != 'title':
                        continue
                    item = {key: lease.get(key) for key in ('pid', 'starttime', 'pgid')}
                    item.update(disposition='unresolved', settled=False)
                    identity = lease.get('claimant_identity', {})
                    if (identity.get('pid') == item['pid']
                            and identity.get('starttime') == item['starttime']
                            and DC.process_namespace_identity() ==
                                'pid:[' + str(identity.get('pid_namespace')) + ']'):
                        item['process'] = DC.process_observation(int(item['pid']))
                        item['disposition'] = DC.process_identity_disposition(
                            int(item['pid']), item['starttime'])
                        item['group'] = dataclasses.asdict(
                            DC.process_group_observation(int(item['pgid'])))
                        item['settled'] = (item['disposition'] == 'dead'
                                           and item['group']['state'] == 'empty')
                    titles[token] = item
            except (KeyError, OSError, ValueError) as exc:
                titles = {'unknown': {'settled': False, 'error': str(exc)}}
            self.summary_settlement_observation = {'summaries': observations, 'title_leases': titles}
            if (all(item['settled'] for item in observations)
                    and all(item['settled'] for item in titles.values())):
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                diagnostic = json.dumps(self.summary_settlement_observation, sort_keys=True)
                self.failure_snapshot(command, SimpleNamespace(returncode=3,
                    stdout=result.stdout, stderr='fixture summary settlement: ' + diagnostic))
                self.fail('fixture summary unknown/unsettled at deadline: ' + diagnostic)
            time.sleep(min(0.1, remaining))


class CurrentDesignPeerSelectionTest(unittest.TestCase):
    def _case_test_current_owner_closure_peer_precedes_round_admission(self, *, replacement_owner=False):
        import importlib.util
        from types import SimpleNamespace
        import replica_batch_contract as BATCH_CONTRACT
        from recovery_history import INPUT_SCHEMA, batch_input_path

        spec = importlib.util.spec_from_file_location(
            "review_batch_current_peer_dispatch", ROOT / "utilities" / "dispatch-batch.py")
        batch = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = batch
        spec.loader.exec_module(batch)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            jobs = base / "dispatch" / "jobs.log"
            jobs.parent.mkdir(parents=True)
            route_id, route_hash, group = "rt-design-peer-fixture", "sha256:" + "a" * 64, "visual-verify"
            nodes = [
                {"id": "visual-verify", "kind": "review-worker", "completion_gate": "design-verify",
                 "unit": "design/verifier", "dispatch_depth": 2, "parallel_group": group,
                 "depends_on": ["build"], "perspective": "visual", "parallel_leg_index": 0,
                 "parallel_leg_count": 2, "parallel_independence_axes": ["cross-harness", "perspective"],
                 "model_profile": "light", "write_scope": ["designs/04_review/verify/**"]},
                {"id": "visual-verify-alternative", "kind": "review-worker",
                 "unit": "design/verifier", "completion_gate": "design-verify", "dispatch_depth": 2,
                 "parallel_group": group, "depends_on": ["build"],
                 "perspective": "visual-alternative", "parallel_leg_index": 1,
                 "parallel_leg_count": 2, "parallel_independence_axes": ["cross-harness", "perspective"],
                 "model_profile": "light", "write_scope": ["designs/04_review/verify-alternative/**"]},
            ]
            critic_group = "critic-review"
            critic_nodes = [
                {"id": "critic-review", "kind": "review-worker", "completion_gate": "design-critic",
                 "unit": "design/critic", "dispatch_depth": 2, "parallel_group": critic_group,
                 "depends_on": ["visual-verify", "visual-verify-alternative"],
                 "perspective": "critic", "parallel_leg_index": 0, "parallel_leg_count": 2,
                 "parallel_independence_axes": ["cross-harness", "perspective"],
                 "model_profile": "light", "write_scope": ["designs/04_review/critic/**"]},
                {"id": "critic-review-alternative", "kind": "review-worker",
                 "completion_gate": "design-critic", "unit": "design/critic", "dispatch_depth": 2,
                 "parallel_group": critic_group,
                 "depends_on": ["visual-verify", "visual-verify-alternative"],
                 "perspective": "critic-alternative", "parallel_leg_index": 1,
                 "parallel_leg_count": 2, "parallel_independence_axes": ["cross-harness", "perspective"],
                 "model_profile": "light", "write_scope": ["designs/04_review/critic-alternative/**"]},
            ]
            handoff = {"id": "handoff", "kind": "pipeline-stage", "completion_gate": "design-handoff",
                       "depends_on": ["critic-review", "critic-review-alternative"],
                       "dispatch_depth": 1,
                       "write_scope": ["designs/05_handoff/**"]}
            route = {"schema_version": 2, "dispatch_contract_version": 3,
                     "route_id": route_id, "route_hash": route_hash,
                     "registry_digest": ROUTE.TOPO.registry_digest(ROUTE.TOPO.load_registry()),
                     "cwd": str(base), "artifact_root": str(base / ".agent_reports"),
                     "capability": "autopilot-design", "capability_mode": "default",
                     "effective_intensity": "standard", "nodes": [
                         {"id": "build", "kind": "pipeline-stage", "completion_gate": "design-build",
                          "write_scope": ["designs/02_tokens/**", "designs/03_components/**"],
                          "depends_on": []}, *nodes, *critic_nodes, handoff]}
            route_file = base / "route.json"
            route_file.write_text(json.dumps(route), encoding="utf-8")
            source_parent = "att-owner-original" if replacement_owner else "att-owner-current"
            attempts = ("att-visual-primary", "att-visual-alternative")
            prompt_text = "Verify the current design payload."
            assignment_digest = "sha256:" + hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
            members = []
            for index, (node, attempt, harness, perspective) in enumerate(zip(
                    nodes, attempts, ("codex", "claude"), ("visual", "visual-alternative"))):
                members.append({
                    "assignment_sha256": assignment_digest,
                    "attempt_id": attempt, "route_node": node["id"], "harness": harness,
                    "fallback_hop": "same-harness-headless", "fallback_ordinal": 1,
                    "model_profile": "light", "perspective": perspective,
                    "parallel_leg_index": index, "leg_class": "peer",
                })
            manifest, digest, leg_digests = BATCH_CONTRACT.build_manifest(
                parallel_group=group, route_id=route_id, parent_attempt_id=source_parent,
                independence="cross-harness", members=members,
                required_independence_axes=["cross-harness", "perspective"],
                realized_independence_axes=["cross-harness", "perspective"],
            )
            rows = []
            for node, attempt, member in zip(nodes, attempts, members):
                metadata = {
                    "attempt_schema_version": 2, "dispatch_depth": 2,
                    "transport": "headless", "execution_surface": "registered-headless",
                    "registered_worker": "1", "fallback_hop": member["fallback_hop"],
                    "attempt_id": attempt, "route_id": route_id, "route_hash": route_hash,
                    "route_node": node["id"], "parent_attempt_id": source_parent,
                    "parent": "owner", "route_file": str(route_file.resolve()),
                    "harness": member["harness"], "child_harness": member["harness"],
                    "fallback_ordinal": member["fallback_ordinal"],
                    "reservation_kind": "parallel-batch", "batch_declared_size": len(nodes),
                    "parallel_group": group, "replica_group": group, "batch_group": group,
                    "batch_route_id": route_id, "batch_parent_attempt_id": source_parent,
                    "batch_attempt_id": attempt, "batch_route_node": node["id"],
                    "batch_harness": member["harness"],
                    "batch_fallback_hop": member["fallback_hop"],
                    "batch_fallback_ordinal": member["fallback_ordinal"],
                    "batch_independence": manifest["independence"],
                    "batch_assignment_sha256": member["assignment_sha256"],
                    "batch_manifest_sha256": digest, "batch_leg_sha256": leg_digests[attempt],
                    "batch_model_profile": member["model_profile"],
                    "batch_perspective": member["perspective"],
                    "batch_parallel_leg_index": member["parallel_leg_index"],
                    "batch_leg_class": member["leg_class"], "batch_auxiliary_check": "-",
                    "note": ("completed-review-blocking" if node["id"] == nodes[0]["id"]
                             else "completed-marker"), "launch_claimed": "1",
                    "failure_class": ("review" if node["id"] == nodes[0]["id"]
                                      else "fail" if replacement_owner else "pass"),
                    "launch_outcome": "reaped-before-publish",
                }
                fields = ["fixture", "done", str(base), str(base), attempt,
                          ",".join(f"{key}={value}" for key, value in metadata.items())]
                rows.append("\t".join(fields))
            jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")
            input_path = batch_input_path(jobs, digest)
            input_path.parent.mkdir(parents=True, exist_ok=True)
            input_path.write_text(json.dumps({
                "schema": INPUT_SCHEMA, "jobs": str(jobs.resolve()),
                "route_id": route_id, "route_hash": route_hash,
                "manifest": manifest, "manifest_digest": digest,
                "options": {"parallel_group": group, "route": str(route_file.resolve()),
                            "parent": "owner", "prompt_text": prompt_text},
            }, separators=(",", ":"), sort_keys=True), encoding="utf-8")
            state = base / "completion-state"
            marker_dir = state / "completion" / route_id
            marker_dir.mkdir(parents=True)
            closure = {"route_id": route_id, "route_hash": route_hash,
                       "node_id": nodes[1]["id"], "attempt_id": attempts[1],
                       "review_gate_closure": "owner-closure",
                       "review_independence": "owner-overridden"}
            (marker_dir / f"{nodes[1]['id']}.json").write_text(
                json.dumps(closure), encoding="utf-8")
            closure_path = marker_dir / f"{nodes[1]['id']}.json"
            closure_bytes = closure_path.read_bytes()
            build_marker = {"route_id": route_id, "route_hash": route_hash,
                            "node_id": "build", "attempt_id": "att-build-current"}
            (marker_dir / "build.json").write_text(json.dumps(build_marker), encoding="utf-8")
            current_marker_attempts = {attempts[1], "att-build-current"}
            original_fail_row = next(line for line in jobs.read_text().splitlines()
                                     if "attempt_id=" + attempts[0] + "," in line)
            original_input_bytes = input_path.read_bytes()
            with mock.patch.object(batch, "dispatch_state_roots", return_value=[state]), \
                 mock.patch.object(batch, "resolve_dispatch_state_root", return_value=state), \
                 mock.patch.object(batch, "completion_marker_is_current",
                                   side_effect=lambda _route, _node, _path, marker, **_kw:
                                   marker.get("attempt_id") == attempts[1]), \
                 mock.patch.object(batch, "completion_attempt_readiness",
                                   return_value=SimpleNamespace(state="ready", reason="settled")):
                peers = batch.current_completed_group_peers(
                    jobs, route, nodes, group, base, prompt_text)
                closure_path.write_text(json.dumps({"attempt_id": attempts[1]}), encoding="utf-8")
                ordinary_pass = batch.current_completed_group_peers(
                    jobs, route, nodes, group, base, prompt_text)
                closure_path.write_bytes(closure_bytes)
            self.assertEqual(set(peers), {nodes[1]["id"]})
            self.assertEqual(ordinary_pass, {})
            self.assertEqual(peers[nodes[1]["id"]]["attempt_id"], attempts[1])
            self.assertEqual(peers[nodes[1]["id"]]["metadata"]["note"], "completed-marker")
            pending = batch.pending_round_admission_nodes(nodes, peers)
            self.assertEqual([node["id"] for node in pending], [nodes[0]["id"]])
            self.assertIn('"review_independence": "owner-overridden"',
                          (marker_dir / f"{nodes[1]['id']}.json").read_text())
            self.assertEqual(batch.current_completed_group_peers(
                jobs, route, nodes, group, base, "a different review request"), {})

            agent_home = base / "agent-home"
            agent_home.mkdir()
            parent_identity = {"parent_harness": "codex", "parent_transport": "headless",
                               "parent_sandbox": "workspace-write"}
            round_calls = []
            reserved = {"calls": []}

            def admission(_route, node, _jobs, **_kwargs):
                round_calls.append(node["id"])
                return SimpleNamespace(
                    budget=SimpleNamespace(next_round=3, cap=2, state="admit", round_kind="first",
                                            correction_round=False),
                    planned_revision_nodes=frozenset(), requires_next_round=True,
                    physical_attempt_ids=frozenset((attempts[0],)), current_attempt_ids=frozenset(),
                    prior_attempt_ids=frozenset((attempts[0],)), reviewed_input=None,
                    source_subject_digest="sha256:" + "c" * 64,
                )

            def assign(_route, selected_nodes, **kwargs):
                fixed = kwargs.get("fixed", {})
                assignments = []
                for index, node in enumerate(selected_nodes):
                    if node["id"] in fixed:
                        harness, hop, ordinal = fixed[node["id"]]
                    else:
                        harness, hop, ordinal = ("codex" if index % 2 == 0 else "claude",
                                                 "same-harness-headless", 1)
                    assignments.append((node, harness, hop, ordinal))
                if selected_nodes and selected_nodes[0].get("parallel_group") == group:
                    self.assertEqual(fixed[nodes[1]["id"]], ("claude", "same-harness-headless", 1))
                return (assignments,
                        "cross-harness", {"degradation_cause": "", "degradation_detail": "",
                            "sole_gate": "not-applicable", "allocation": {}, "capacity": {}})

            def reserve(_governor, _root, pending, *, manifest, manifest_digest, peers=None, **_kw):
                current_manifest, current_digest, current_leg_digests = BATCH_CONTRACT.verify_manifest(manifest)
                call = {"attempts": [leg["attempt_id"] for leg in pending],
                        "pending": pending, "peers": peers,
                        "members": [member["route_node"] for member in manifest["members"]],
                        "manifest": current_manifest, "digest": current_digest,
                        "leg_digests": current_leg_digests}
                reserved["calls"].append(call)
                governor_spec = importlib.util.spec_from_file_location(
                    "model_worker_governor_visual_fixture",
                    ROOT / "utilities" / "model-worker-governor.py")
                governor = importlib.util.module_from_spec(governor_spec)
                governor_spec.loader.exec_module(governor)
                if peers:
                    with mock.patch.object(DC, "completion_marker_is_current", return_value=True), \
                         mock.patch.object(DC, "completion_attempt_readiness",
                                           return_value=SimpleNamespace(state="ready", reason="settled")), \
                         mock.patch.object(DC, "dispatch_state_roots", return_value=[state]), \
                         mock.patch.object(DC, "resolve_dispatch_state_root", return_value=state):
                        with contextlib.ExitStack() as ancestry:
                            if replacement_owner:
                                ancestry.enter_context(mock.patch.object(DC, "review_self_disposition_lineage",
                                    return_value=[source_parent, "att-owner-current"]))
                            call["owner_closure_proof"] = governor._validate_legacy_visual_closure_peer(
                                peers[0], current_manifest, current_digest, current_leg_digests,
                                [leg["attempt_id"] for leg in pending])
                return [f"reservation-fixture-{index}" for index, _leg in enumerate(pending)]

            def collect(item):
                leg, token, proc = item
                call = reserved["calls"][-1]
                route_group = call["manifest"]["parallel_group"]
                metadata = {
                    "attempt_schema_version": "2", "dispatch_depth": "2",
                    "transport": "headless", "execution_surface": "registered-headless",
                    "registered_worker": "1", "fallback_hop": leg["hop"],
                    "attempt_id": leg["attempt_id"], "route_id": route_id,
                    "route_hash": route_hash, "route_node": leg["node"],
                    "parent_attempt_id": "att-owner-current", "parent": "owner",
                    "route_file": str(route_file.resolve()), "harness": leg["adapter"],
                    "child_harness": leg["adapter"], "fallback_ordinal": str(leg["ordinal"]),
                    "parallel_group": route_group, "replica_group": route_group,
                    "reservation_kind": "parallel-batch",
                    "batch_declared_size": str(call["manifest"]["declared_size"]),
                    "batch_admission_count": str(call["manifest"]["declared_size"]),
                    "batch_group": route_group, "batch_route_id": route_id,
                    "batch_parent_attempt_id": "att-owner-current",
                    "batch_attempt_id": leg["attempt_id"], "batch_route_node": leg["node"],
                    "batch_harness": leg["adapter"], "batch_fallback_hop": leg["hop"],
                    "batch_fallback_ordinal": str(leg["ordinal"]),
                    "batch_independence": call["manifest"]["independence"],
                    "batch_assignment_sha256": leg["assignment_sha256"],
                    "batch_manifest_sha256": call["digest"],
                    "batch_leg_sha256": call["leg_digests"][leg["attempt_id"]],
                    "batch_model_profile": leg["model_profile"],
                    "batch_perspective": leg["perspective"],
                    "batch_parallel_leg_index": str(leg["parallel_leg_index"]),
                    "batch_leg_class": leg["leg_class"], "batch_auxiliary_check": "-",
                    "launch_claimed": "1", "launch_started": "1",
                    "launch_outcome": "reaped-before-publish",
                    "failure_class": "pass", "note": "completed-marker",
                }
                fields = ["fixture", "done", str(base), str(base), leg["attempt_id"],
                          ",".join(f"{key}={value}" for key, value in sorted(metadata.items()))]
                with jobs.open("a", encoding="utf-8") as stream:
                    stream.write("\t".join(fields) + "\n")
                current_marker_attempts.add(leg["attempt_id"])
                (marker_dir / f"{leg['node']}.json").write_text(json.dumps({
                    "route_id": route_id, "route_hash": route_hash,
                    "node_id": leg["node"], "attempt_id": leg["attempt_id"],
                }), encoding="utf-8")
                stdout = ("check=ok\n" f"adapter={leg['adapter']}\nstatus=start\n"
                          f"attempt_id={leg['attempt_id']}\nregistered=1\nstarted=1\n"
                          "child_spawned=1\nduplicate_attempt=0\n")
                proc.returncode = 0
                return leg, token, proc, stdout, ""

            spawned = []

            def fake_popen(*_args, **_kwargs):
                spawned.append("launch")
                return SimpleNamespace(pid=91234, returncode=None, poll=lambda: None)

            def public_start(parallel_group):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = batch.main(["--route", str(route_file), "--parallel-group", parallel_group,
                        "--start", "--parent", "owner", "--jobs", str(jobs),
                        "--prompt-text", prompt_text])
                return exit_code, json.loads(stdout.getvalue().splitlines()[-1])

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {
                    "AGENT_DISPATCH_SELF_SLUG": "owner",
                    "AGENT_DISPATCH_ATTEMPT_ID": "att-owner-current",
                    "AGENT_DISPATCH_JOBS": str(jobs), "AGENT_HOME": str(agent_home),
                }, clear=False))
                stack.enter_context(mock.patch.object(batch, "load_route", return_value=route))
                stack.enter_context(mock.patch.object(batch, "resolve_agent_home", return_value=agent_home))
                stack.enter_context(mock.patch.object(batch, "resolve_global_registry",
                                                       return_value=SimpleNamespace(path=jobs)))
                stack.enter_context(mock.patch.object(batch.DISPATCH_NODE, "current_parent_identity",
                                                       return_value=parent_identity))
                stack.enter_context(mock.patch.object(batch, "resolve_live_parent_attempt"))
                stack.enter_context(mock.patch.object(batch, "adopt_self_parent"))
                stack.enter_context(mock.patch("dispatch_contract.work_root_identity", return_value=str(base)))
                stack.enter_context(mock.patch.object(batch.DISPATCH_NODE, "admit_round", side_effect=admission))
                stack.enter_context(mock.patch.object(batch, "decide_continuation",
                    return_value=SimpleNamespace(allowed=True, state="admit", next_action="", retry_when="")))
                stack.enter_context(mock.patch.object(batch, "assign_harnesses", side_effect=assign))
                stack.enter_context(mock.patch.object(batch, "resolve_model_governor_root",
                                                       return_value=base / "governor"))
                stack.enter_context(mock.patch.object(batch, "dispatch_state_roots", return_value=[state]))
                stack.enter_context(mock.patch.object(batch, "resolve_dispatch_state_root", return_value=state))
                marker_is_current = lambda _route, _node, _path, marker, **_kw: (
                    marker.get("attempt_id") in current_marker_attempts)
                stack.enter_context(mock.patch.object(batch, "completion_marker_is_current",
                                                       side_effect=marker_is_current))
                stack.enter_context(mock.patch.object(batch, "completion_attempt_readiness",
                    return_value=SimpleNamespace(state="ready", reason="settled")))
                stack.enter_context(mock.patch.object(DC, "completion_marker_is_current",
                                                       side_effect=marker_is_current))
                stack.enter_context(mock.patch.object(DC, "completion_attempt_readiness",
                    return_value=SimpleNamespace(state="ready", reason="settled")))
                stack.enter_context(mock.patch.object(DC, "dispatch_state_roots", return_value=[state]))
                stack.enter_context(mock.patch.object(DC, "resolve_dispatch_state_root", return_value=state))
                stack.enter_context(mock.patch("dispatch_capacity_evidence.active_limits", return_value={}))
                stack.enter_context(mock.patch.object(batch, "reserve_batch", side_effect=reserve))
                stack.enter_context(mock.patch.object(batch, "cancel_unclaimed"))
                stack.enter_context(mock.patch.object(batch, "node_launch_command", return_value=["fixture-launch"]))
                stack.enter_context(mock.patch.object(batch.subprocess, "Popen", side_effect=fake_popen))
                stack.enter_context(mock.patch.object(batch, "collect_wrapper", side_effect=collect))
                stack.enter_context(mock.patch.object(batch, "record_allocation_receipt", return_value=None))
                stack.enter_context(mock.patch.object(batch, "record_degradation", return_value=None))
                exit_code, receipt = public_start(group)
                self.assertEqual(exit_code, 0, receipt)
                self.assertEqual(receipt["newly_started"], 1, receipt)
                self.assertEqual(receipt["existing"], 1, receipt)
                first_attempt = receipt["legs"][0]["attempt_id"]
                first_reservation = reserved["calls"][0]
                self.assertEqual(first_reservation["attempts"], [first_attempt])
                self.assertEqual(first_reservation["members"], [node["id"] for node in nodes])
                self.assertEqual(first_reservation["peers"][0]["attempt_id"], attempts[1])
                closure_proof = first_reservation["owner_closure_proof"]["batch_peer_proof"]
                self.assertTrue(closure_proof["legacy_owner_closure"])
                self.assertEqual(closure_proof["source_manifest_sha256"], digest)
                self.assertEqual(closure_proof["attempt_id"], attempts[1])
                self.assertEqual(len(spawned), 1)
                self.assertEqual(first_reservation["manifest"]["parent_attempt_id"], "att-owner-current")
                if replacement_owner:
                    self.assertNotEqual(manifest["parent_attempt_id"], "att-owner-current")
                    self.assertEqual(closure_proof["source_manifest_sha256"], digest)
                    self.assertIn("failure_class=fail", next(line for line in jobs.read_text().splitlines()
                        if "attempt_id=" + attempts[1] + "," in line))

                repeat_exit, repeat_receipt = public_start(group)
                self.assertEqual(repeat_exit, 0, repeat_receipt)
                self.assertEqual(repeat_receipt["newly_started"], 0, repeat_receipt)
                self.assertEqual(repeat_receipt["existing"], 2, repeat_receipt)
                self.assertEqual(len(spawned), 1)

                critic_exit, critic_receipt = public_start(critic_group)
                self.assertEqual(critic_exit, 0, critic_receipt)
                self.assertEqual(critic_receipt["newly_started"], 2, critic_receipt)
                self.assertEqual(len(critic_receipt["legs"]), 2)
                self.assertEqual(len(spawned), 3)
                for leg in critic_receipt["legs"]:
                    current_marker_attempts.add(leg["attempt_id"])
                    marker = {"route_id": route_id, "route_hash": route_hash,
                              "node_id": leg["node"], "attempt_id": leg["attempt_id"]}
                    (marker_dir / f"{leg['node']}.json").write_text(
                        json.dumps(marker), encoding="utf-8")

                with mock.patch.object(DC, "_frame_pair_attempt_gate"), \
                     mock.patch.object(DC, "_human_gate_entry_fence"), \
                     mock.patch.object(DC, "_auxiliary_arbitration_gate"), \
                     mock.patch.object(DC, "_sibling_attempt_gate"):
                    DC.completion_marker_gate(str(route_file), "handoff", "dry-run", agent_home, jobs)

                artifact_root = Path(route["artifact_root"])
                artifact_root.mkdir(parents=True, exist_ok=True)
                handoff_evidence = artifact_root / "handoff.md"
                handoff_evidence.write_text("fixture handoff complete\n", encoding="utf-8")
                handoff_attempt = "att-handoff-complete"
                handoff_metadata = {
                    "attempt_schema_version": 2, "dispatch_depth": 1,
                    "transport": "interactive", "execution_surface": "inline",
                    "registered_worker": False, "fallback_hop": "inline",
                    "route_id": route_id, "route_hash": route_hash,
                    "route_node": "handoff", "attempt_id": handoff_attempt,
                    "failure_class": "pass", "note": "completed-marker",
                }
                # This public-batch fixture has no artifact-producer cycle;
                # keep its scope on the registered group and handoff gates.
                with mock.patch.object(PRODUCER, "require_cycle_output", return_value=None), \
                     mock.patch.object(ROUTE, "dispatch_state_roots", return_value=[state]), \
                     mock.patch.object(ROUTE, "resolve_agent_home", return_value=agent_home):
                    handoff_marker = ROUTE._publish_completion_locked(
                        route, handoff, "handoff", handoff_evidence,
                        attempt_id=handoff_attempt, attempt_metadata=handoff_metadata, jobs=jobs)
                self.assertEqual(handoff_marker["attempt_id"], handoff_attempt)
                self.assertTrue((ROUTE.completion_dir(route_id, jobs=jobs) / "handoff.json").is_file())

            self.assertIn(original_fail_row, jobs.read_text(encoding="utf-8"))
            self.assertEqual(closure_path.read_bytes(), closure_bytes)
            self.assertEqual(input_path.read_bytes(), original_input_bytes)


class AdmissionSelectionFixture(unittest.TestCase):
    def _case_test_logical_budget_and_physical_generation_selection(self):
        node = {"id": "impl-review", "kind": "review-worker", "worker_type": "review"}
        route = {"route_id": "rt-selection", "effective_intensity": "strong",
                 "continuation_budget": {"review_round_cap": 2}, "nodes": [node]}
        def row(attempt, status="done", note="completed-marker", **extra):
            return (["now", status, "repo", "cwd", "slug", ""],
                    {"attempt_id": attempt, "worker_type": "review", "note": note, **extra})
        scenarios = [
            ([row("r1")], [], "admit", False, 1),
            ([row("r1")], [{"supersedes": ["r1"]}], "admit", True, 1),
            ([row("r1"), row("r2")], [{"supersedes": ["r1"]}], "exhausted", False, 2),
            ([row("r1"), row("r2")], [{"supersedes": ["r2"]}], "admit", True, 2),
            ([row("r1"), row("r2"), row("r3")], [{"supersedes": ["r3"]}], "exhausted", True, 3),
            ([row("r1"), row("r2", "open")], [{"supersedes": ["r1"]}], "blocked-live", True, 1),
            ([row("r1"), row("r2", note="completion-deferred",
                  classifier_source="registered-wrapper-completion-transient-v1")], [], "blocked-unsettled", False, 1),
            ([row("death1", note="dead-invalid-envelope"), row("death2", note="dead-missing-result")],
             [], "verdictless-bound", False, 0),
            ([row("r1", note="completed-review-blocking")], [], "admit", True, 1),
        ]
        for rows, revisions, state, next_required, verdicts in scenarios:
            with self.subTest(state=state, rows=rows), \
                 mock.patch.object(NODE, "prior_round_attempts", return_value=rows), \
                 mock.patch.object(NODE.ROUTE, "_dependency_revisions", return_value=revisions):
                admission = NODE.admit_round(route, node, Path("unused"), record_auto_revisions=False)
                self.assertEqual(admission.budget.state, state)
                self.assertEqual(admission.budget.verdict_rounds, verdicts)
                self.assertEqual(admission.requires_next_round, next_required)
                self.assertEqual(admission.current_attempt_ids, frozenset((rows[-1][1]["attempt_id"],)))
        # Projection carries both physical ids, but only one verdict. A raw
        # registry string that resembles this field has no exclusion authority.
        for members, expected in ((frozenset(("original", "successor")), frozenset(("original", "successor"))),
                                  ("arbitrary-hidden", frozenset(("successor",)))):
            rows = [row("successor", logical_round_attempt_ids=members)]
            with mock.patch.object(NODE, "prior_round_attempts", return_value=rows), \
                 mock.patch.object(NODE.ROUTE, "_dependency_revisions", return_value=[]):
                admission = NODE.admit_round(route, node, Path("unused"), record_auto_revisions=False)
            self.assertEqual(admission.physical_attempt_ids, expected)
            self.assertEqual(admission.prior_attempt_ids, frozenset(("successor",)))
            self.assertEqual(admission.budget.verdict_rounds, 1)
        default = NODE.RoundAdmission(budget=object())
        self.assertFalse(default.requires_next_round)
        self.assertEqual(default.current_attempt_ids, frozenset())
