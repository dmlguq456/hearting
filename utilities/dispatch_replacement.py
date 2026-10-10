"""SD-157: one proof-bound replacement of a dead logical execution.

The jobs lock owns the claim and lineage. Launching stays in the checked adapter;
its existing registration/spawn fence handles a lost launcher response. Original
terminal rows are evidence and are never rewritten into replacement outcomes.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import dataclasses
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Callable

import dispatch_contract as DC
from artifact_receipt import _write_once
import route_authority

ROOT = Path(__file__).resolve().parents[1]
DEATH_NOTES = frozenset({
    'dead-exact-pid', 'dead-namespace-absent', 'dead-worker-silent-exit',
    'dead-missing-result', 'dead-parent-orphaned', 'dead-governor-reservation-transfer',
    'dead-no-progress', 'dead-timeout',
})
# An owner whose runtime died under it: the process exited (`dead-runtime-exit`) or the runtime
# returned an error envelope such as a provider 5xx (`dead-runtime-error`). Only an explicit `start`
# replaces it, once, from the node's one replacement budget.
RUNTIME_DEATH_NOTES = frozenset({'dead-runtime-exit', 'dead-runtime-error'})
SCHEMA = 'automatic-dead-replacement-v1'
# D2: a frame leg whose `top` the frame rule assigned is replaced once, one profile lower, when it
# stops at a usage limit. The claim carries exactly this value; nothing else may.
FRAME_CAPACITY = 'frame-capacity'
# Stops that are pauses, not the one silent replacement: a usage limit, an owner its launcher
# closed before spawning, or an owner that ended BLOCKED and has since been answered. Each pause
# opens its own family (`after_capacity` keeps the digests of existing capacity families unchanged).
CORRECTED = 'corrected'
PAUSE_KINDS = frozenset({'capacity', 'unlaunched', CORRECTED})
FRAME_TRANSITION = {'from': 'top', 'to': 'deep', 'reason': 'capacity', 'ordinal': 1, 'origin': 'frame-rule'}
CONTINUATION_WAIT_NOTE = (
    'This route has already used its one automatic continuation. If you raise a human gate later, '
    'wait for the person: call the bounded workflow-supervisor.py await-release again after each timeout, '
    'and do not end the turn at the gate; ending there leaves the work for a person to restart.\n')


def _bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))+'\n').encode()


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'att-[A-Za-z0-9._-]{1,240}', value):
        raise DC.DispatchContractError('replacement-attempt-invalid')
    return value


def _once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _bytes(value)
    if path.is_symlink() or (not _write_once(path.parent, path, raw) and path.read_bytes() != raw):
        raise DC.DispatchContractError('replacement-record-conflict', str(path))


def _read(path):
    if path.is_symlink():
        raise DC.DispatchContractError('replacement-record-symlink', str(path))
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DC.DispatchContractError('replacement-record-unreadable', str(path)) from exc
    if not isinstance(value, dict):
        raise DC.DispatchContractError('replacement-record-invalid', str(path))
    return value


def _directory(jobs):
    return Path(jobs).resolve().parent / 'automatic-replacements'


def _rows(lines):
    result = {}
    for line in lines:
        fields = line.split('\t')
        if len(fields) != 6:
            continue
        meta = DC.parse_registry_metadata(fields[5])
        aid = meta.get('attempt_id')
        if aid:
            if aid in result:
                raise DC.DispatchContractError('replacement-attempt-ambiguous', aid)
            result[aid] = (fields, meta)
    return result


@contextmanager
def _locked(jobs):
    jobs = Path(jobs).resolve()
    DC.ensure_global_registry_writable(jobs)
    with Path(str(jobs)+'.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield jobs.read_text().splitlines()


INPUT_OPTIONS = frozenset({'--start', '--register', '--dry-run', '--attempt-id',
    '--jobs', '--worktree', '--prompt-file', '--prompt-text', '--automatic-retry-of'})
RESOLVED_INPUT_KEYS = ('capability', 'capability_mode', 'unit', 'worker_type',
    'worker_mode', 'assigned_contract', 'dispatch_depth', 'intensity', 'qa',
    'sandbox', 'permission_mode', 'parent_harness', 'parent_transport',
    'parent_sandbox', 'launch_authority', 'parent_session_id', 'parent_attempt_id',
    'execution_surface', 'registered_worker', 'fallback_hop', 'model_role',
    'model_profile', 'model', 'reasoning', 'resolved_model_settings',
    'resolved_completion_delivery', 'parent_completion_delivery')


def _applied_permissions(args):
    result = {}
    posture = getattr(args, 'resolved_permission_posture', None)
    if isinstance(posture, dict):
        result['claude'] = {key:posture.get(key) for key in
                           ('mode','mode_flag','allowed_tools','inherited_default_mode')}
    if hasattr(args, 'replacement_runtime_sandbox'):
        result['runtime_sandbox'] = args.replacement_runtime_sandbox
    grant = getattr(args, 'execution_access_grant', None)
    if grant is not None:
        result['execution_access'] = json.loads(json.dumps(dataclasses.asdict(grant), default=str))
    config = getattr(args, 'opencode_config_content', None)
    if config:
        parsed = json.loads(config)
        result['opencode_permission'] = parsed.get('permission')
    for key in ('launch_lifecycle','nested_headless_network'):
        if hasattr(args,key): result[key] = getattr(args,key)
    return result


def seal_launch_input(args, harness: str, task: str) -> str:
    """Called after wrapper validation, before its first registry mutation.

    Only raw caller input is retained: the rendered worker prompt embeds old
    identities and must never be replayed. No credentials/environment snapshot.
    """
    argv = getattr(args, 'replacement_input_argv', None)
    if argv is None or not getattr(args, 'attempt_id', None):
        return ''  # Legacy callers remain observable but cannot invent replay input.
    aid = _id(args.attempt_id)
    if harness not in {'claude', 'codex', 'opencode'} or not isinstance(task, str):
        raise DC.DispatchContractError('replacement-input-invalid')
    jobs = Path(args.jobs_path).resolve()
    defaults = {'--'+key.replace('_','-'): str(getattr(args,key))
                for key in ('parent_session_id','parent_attempt_id','parent_harness',
                            'parent_transport','parent_sandbox','launch_authority',
                            'sandbox','permission_mode','reviewed_evidence') if getattr(args,key,None)}
    normalized = _canonical_argv(_replace_options(list(argv), defaults, remove=INPUT_OPTIONS))
    payload = {
        'schema': SCHEMA, 'attempt_id': aid, 'harness': harness,
        'jobs': str(jobs), 'worktree': str(Path(args.worktree).resolve()),
        'argv': normalized, 'task': task,
        'resolved': {key: getattr(args, key) for key in RESOLVED_INPUT_KEYS
                     if hasattr(args, key)},
        'launch_home': str(ROOT), 'applied_permissions': _applied_permissions(args),
        'route_id': getattr(args, 'route_id', None) or '',
        'route_node': getattr(args, 'route_node', None) or '',
        'owner_route_id': getattr(getattr(args, 'owner_route_binding', None), 'route_id', ''),
    }
    if getattr(args, 'replacement_retry_brief', ''):
        payload['retry_brief'] = args.replacement_retry_brief
    path = _directory(jobs)/'inputs'/(aid+'.json')
    try:
        _once(path, payload)
    except DC.DispatchContractError as exc:
        if exc.reason != 'replacement-record-conflict':
            raise
        with _locked(jobs) as lines:
            if not _reseal_allowed(jobs, aid, path, payload, lines=lines,
                                   prior=getattr(args, 'automatic_retry_of', None)):
                raise
            _replace_record(path, payload)
    return ',replacement_input_digest='+_digest(payload)


# A later launcher of the same attempt may run from a newer release or another place (inside
# the parent's sandbox or on the host) and resolve afresh (admission still checks that against
# the source). The work and the permissions it is granted must not change
# (`route_authority.same_sealed_work`).
_RESEAL_STABLE_KEYS = route_authority.RESEAL_STABLE_KEYS


def _reseal_allowed(jobs, aid, path, payload, *, lines=None, prior=None):
    """A launcher stopped before its claim sealed this input; the next one may reseal it."""
    previous = _read(path)
    stored = json.loads(_bytes(payload))  # compare in the stored form: a tuple reads back as a list
    if not previous:
        return False
    lines = jobs.read_text(encoding='utf-8', errors='replace').splitlines() if lines is None else lines
    same_work = route_authority.same_sealed_work(previous, stored)
    if not same_work:
        reservation = source_reservation(jobs, prior) if prior else None
        record = _read(_record_path(jobs, reservation['family_id'])) if reservation else None
        if not record or record.get('replacement_attempt_id') != aid:
            return False
        _, source, replay = validate_claim_source(jobs, lines, record)
        _, current_route = _route(jobs, prior, source)
        if not route_authority.same_sealed_work(previous, stored,
                recovery=(record, source, replay, current_route)):
            return False
    rows = []
    for line in lines:
        fields = line.split('\t')
        if len(fields) == 6:
            meta = DC.parse_registry_metadata(fields[5])
            if meta.get('attempt_id') == aid:
                rows.append((fields[1], meta))
    if not rows:
        return True
    if len(rows) != 1:
        return False
    status, meta = rows[0]
    return (status == 'open' and meta.get('launch_claimed') == '0'
            and meta.get('launch_started') != '1' and not meta.get('pid'))


def _replace_record(path, value):
    temporary = path.with_name('.'+path.name+'.'+os.urandom(8).hex()+'.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def launch_input(jobs, aid, meta):
    value = _read(_directory(jobs)/'inputs'/(_id(aid)+'.json'))
    if (value is None or value.get('schema') != SCHEMA or value.get('attempt_id') != aid
            or value.get('jobs') != str(Path(jobs).resolve())
            or _digest(value) != meta.get('replacement_input_digest')):
        raise DC.DispatchContractError('replacement-input-unproven', aid)
    return value


def _terminal_absent(fields, meta, *, capacity=False, runtime=False):
    from codex_dispatch_terminal import inspect_terminal_attempt
    result = inspect_terminal_attempt(meta.get('log_file'), worktree=fields[3],
                                      artifact_root_metadata=meta.get('artifact_root'))
    if capacity and result.get('state') == 'invalid' and result.get('failure_class') == 'capacity':
        return True  # A usage-limit result is the stop itself, not a handoff to settle.
    if runtime and result.get('state') == 'invalid' and result.get('failure_class') == 'runtime':
        return True  # So is a runtime error envelope (a provider 5xx): no handoff was ever written.
    return result.get('state') == 'absent'


def _frame_rule_top(jobs, meta):
    """Did the frame rule itself assign this leg's `top`, and did the leg run at it?

    A `top` the person chose (a pin carrying a model or effort, or a per-node explicit profile) is
    theirs and is never lowered automatically; a pin naming only a harness chooses no profile.
    Anything unreadable answers no, which leaves the ordinary needs-attention in place."""
    if (meta.get('worker_type') != 'frame' or meta.get('dispatch_depth') != '1'
            or meta.get('model_profile') != 'top' or meta.get('replacement_original_attempt_id')
            or not meta.get('route_file') or not meta.get('route_node')):
        return False
    try:
        route = _read(Path(meta['route_file']))
    except DC.DispatchContractError:
        return False
    if not route or route.get('route_id') != meta.get('route_id'):
        return False
    node = next((n for n in route.get('nodes') or [] if isinstance(n, dict) and n.get('id') == meta['route_node']), None)
    if not node or node.get('model_profile') != 'top' or node.get('worker_type') != 'frame':
        return False
    if (route.get('explicit_profiles') or {}).get(meta['route_node']):
        return False
    pins = route_authority.route_in_force(route).get('selection_pins') or {}
    pin = pins.get('frame') or pins.get('owner') or {}
    return not (pin.get('model') or pin.get('effort'))


def death_kind(fields, meta, *, jobs=None, lines=None):
    """The one place that says why a terminal row may be replaced.

    'parked' is a released human gate, 'capacity' an owner stopped at a usage limit,
    'unlaunched' an owner its launcher closed before spawning, 'corrected' an owner that ended
    BLOCKED, or with a readable FAIL, and has since received a person's answer through `correct`,
    'silent' a proven silent death, 'frame-capacity' a frame leg at its frame-rule `top` stopped at
    a usage limit (replaced once at `deep`). Anything else, including a user cancel, is None.
    Only a route owner pauses on capacity: a stage worker's limit stays with its owner's
    own fallback, so one row never has two successors.
    """
    import route_parent_close
    if (meta.get('parent_close_requested') == '1' or
            jobs is not None and route_parent_close.row_requested(meta, jobs)):
        return None
    if jobs is not None and meta.get('note') == 'dead-worker-blocked':
        found = owner_parked_gate(jobs, meta.get('attempt_id'), lines=lines)
        if found and found['status'] == 'proceed':
            return 'parked'
        if found is not None:
            return None  # a declared gate keeps its own answer path
    if (jobs is not None and meta.get('worker_type') == 'owner'
            and route_authority.answerable_owner_end(fields[1], meta)
            and _retained_corrections(jobs, meta.get('attempt_id'))):
        return CORRECTED  # an answered stop is a pause; no automatic retry of an unanswered FAIL
    if (jobs is not None and route_authority.runtime_owner_can_resume(fields[1], meta)
            and _retained_corrections(jobs, meta.get('attempt_id'))):
        return CORRECTED
    if (meta.get('note') == 'cancelled-receipt-unavailable'
            and meta.get('classifier_source') == DC.AUTOMATIC_RECEIPTLESS_CLASSIFIER):
        return 'silent'
    if meta.get('note') in DEATH_NOTES and fields[1] not in {'cancelled', 'killed'}:
        return 'silent'
    if (meta.get('worker_type') == 'owner' and fields[1] == 'done'
            and (meta.get('note') == 'dead-capacity' or meta.get('failure_class') == 'capacity')):
        return 'capacity'
    if (meta.get('worker_type') == 'owner' and fields[1] == 'done'
            and meta.get('launch_outcome') == 'never-launched' and meta.get('launch_claimed') == '0'
            and meta.get('launch_started') != '1' and not meta.get('pid')):
        return 'unlaunched'  # nothing ran: the log never existed and no process was ever bound
    if meta.get('worker_type') == 'owner' and fields[1] == 'done' and meta.get('note') in RUNTIME_DEATH_NOTES:
        return 'runtime'  # the owner's runtime died under it; only an explicit `start` replaces it
    if (jobs is not None and meta.get('worker_type') == 'frame' and fields[1] == 'done'
            and (meta.get('note') == 'dead-capacity' or meta.get('failure_class') == 'capacity')
            and _frame_rule_top(jobs, meta)):
        return FRAME_CAPACITY
    return None


def death_proof(fields, meta, *, jobs=None, lines=None):
    """No broad dead-* permission; a semantic result still owns settlement."""
    if fields[1] not in {'done', 'cancelled', 'killed'}:
        raise DC.DispatchContractError('replacement-terminal-unsettled')
    if DC.terminal_conflict_pending(meta):
        raise DC.DispatchContractError('replacement-terminal-conflict')
    kind = death_kind(fields, meta, jobs=jobs, lines=lines)
    if kind is None:
        raise DC.DispatchContractError('replacement-not-silent-death')
    proof = DC.attempt_process_quiescence(meta, terminal_receipt=True)
    if proof.state != 'quiescent':
        raise _process_error(proof, meta)
    if kind not in {'parked', 'unlaunched', CORRECTED} and not _terminal_absent(
            fields, meta, capacity=kind in {'capacity', FRAME_CAPACITY}, runtime=kind == 'runtime'):
        raise DC.DispatchContractError('replacement-result-settlement-required')
    result = {'state': proof.state, 'reason': proof.reason, 'death_kind': kind,
              'note': meta.get('note', ''), 'cleanup_receipt_digest': meta.get('cleanup_receipt_digest', ''),
              'cancellation_receipt_digest': meta.get('cancellation_receipt_digest', '')}
    if kind == 'parked':
        parked = owner_parked_gate(jobs, meta.get('attempt_id'), lines=lines)
        if not parked:
            raise DC.DispatchContractError('replacement-not-silent-death')
        result.update({'parked_gate': parked['gate'], 'gate_epoch': parked['epoch']})
    if kind == CORRECTED:
        # The answers the continuation receives are pinned here, by id and digest.
        result['corrections'] = [{'id': item['id'], 'digest': item['digest']}
                                 for item in _retained_corrections(jobs, meta.get('attempt_id'))]
        handoff = _blocked_handoff(fields, meta)
        if handoff:
            result['handoff'] = handoff
        if route_authority.runtime_owner_can_resume(fields[1], meta):
            result['source_result'] = 'EXITED'
    return result


def _retained_corrections(jobs, aid):
    from dispatch_owner_input import retained
    return retained(Path(jobs), aid) if jobs is not None and aid else []


def _blocked_handoff(fields, meta):
    """The report a BLOCKED owner named in its own terminal result, or None."""
    try:
        import base64
        from codex_dispatch_terminal import inspect_terminal_attempt
        result = inspect_terminal_attempt(meta.get('log_file'), worktree=fields[3],
                                          artifact_root_metadata=meta.get('artifact_root'),
                                          worker_type='owner')
        encoded = result.get('artifact_path_b64') if result.get('artifact_state') == 'readable' else None
        if not encoded:
            return None
        return base64.urlsafe_b64decode(str(encoded) + '=' * (-len(str(encoded)) % 4)).decode()
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def _process_error(proof, meta, attempt_id=None):
    """`replacement-process-<state>`; a live one says what is still alive."""
    error = DC.DispatchContractError('replacement-process-'+proof.state, proof.reason)
    if proof.state == 'live':
        reason = str(proof.reason)
        kind = ('tagged-descendant' if 'descendant' in reason
                else 'pgid' if 'group' in reason or 'pgid' in reason else 'pid')
        pid = getattr(proof, 'pid', None)
        error.live = {'live_attempt_id': attempt_id or meta.get('attempt_id', ''), 'live_kind': kind,
                      'live_pids': ([f"{pid}:{meta['pid_start']}" if pid and str(pid) == meta.get('pid')
                                     and meta.get('pid_start') else str(pid)] if pid else [])[:8]}
    return error


def _owned_children(rows, owner):
    owned = {owner}
    changed = True
    while changed:
        changed = False
        for aid, (_, meta) in rows.items():
            if meta.get('parent_attempt_id') in owned and aid not in owned:
                owned.add(aid); changed = True
    return sorted(owned-{owner})


def _settle_terminal_cleanup(jobs, rows, aid, source):
    """Let `start` finish a cleanup receipt the runtime could already prove.

    The same signal-free, compare-and-set authority the join and reconcile use.
    A live process, an open row or an unproven cleanup is left untouched; the lock-held
    `death_proof` and `_children_quiescent` still make the only decision.
    """
    targets = [aid] + (_owned_children(rows, aid) if source.get('worker_type') == 'owner' else [])
    for target in targets:
        fields, meta = rows[target]
        if fields[1] not in {'done', 'cancelled', 'killed'}:
            continue
        if DC.attempt_process_quiescence(meta, terminal_receipt=True).state in {'quiescent', 'live'}:
            continue
        DC.resolve_attempt_cleanup(jobs, target, apply=True)


def _children_quiescent(rows, owner):
    for aid in _owned_children(rows, owner):
        fields, meta = rows[aid]
        proof = DC.attempt_process_quiescence(meta, terminal_receipt=fields[1] in {'done','cancelled','killed'})
        if fields[1] in {'open','running'} or proof.state != 'quiescent':
            raise DC.DispatchContractError('replacement-owner-child-unsettled', aid)


def _route(jobs, aid, meta):
    from owner_route_binding import resolve_owner_route_lifecycle
    if meta.get('worker_type') == 'owner':
        binding, _ = resolve_owner_route_lifecycle(jobs, owner_attempt_id=aid)
        if binding is None and not meta.get('route_file'):
            raise DC.DispatchContractError('replacement-owner-route-unproven')
        path = Path(binding.route_file if binding else meta['route_file'])
    else:
        path = Path(meta.get('route_file') or '')
    if not path.is_file():
        raise DC.DispatchContractError('replacement-route-unreadable')
    route = _read(path)
    DC._route_module().verify_route(route)
    # The closed route is immutable history, not an executable obligation.
    if path.with_suffix('.outcome.json').exists():
        raise DC.DispatchContractError('replacement-route-closed')
    return path, route


def _instant(value):
    moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('instant-without-timezone')
    return moment


def owner_parked_gate(jobs, aid, *, lines=None):
    """Read-only: the human gate a BLOCKED owner is resting at, or None.

    A typed BLOCKED owner handoff is a pause, not a failure, when this owner raised
    a human gate after it started and the nodes the gate holds back have not begun.
    So is a PASS owner whose gate still holds the operation it executed itself (refine's
    `transaction` behind `preview-disposition`): its result is a proposal until the person
    answers. That recognition only describes the wait; a passed owner is never replaced.
    Never raises; every unproven condition answers None.
    """
    try:
        rows = _rows(lines if lines is not None else Path(jobs).read_text().splitlines())
        fields, meta = rows[aid]
        blocked = meta.get('note') == 'dead-worker-blocked'
        proposal = (not blocked and DC.verdict_pass(meta) and meta.get('workflow_completion') == 'runtime-v1')
        if (fields[1] != 'done' or meta.get('worker_type') != 'owner'
                or not (blocked or proposal) or DC.terminal_conflict_pending(meta)):
            return None
        path, route = _route(jobs, aid, meta)
        import workflow_state as WS
        ledger = WS.WorkflowLedger(route['route_id'], route['route_hash'], jobs=jobs)
        entries = ledger.journal()
        started = _instant(fields[0])
        chosen = None
        for gate in sorted({b['gate'] for b in route.get('human_gate_bindings', [])}):
            raisers = [n for n in route['nodes'] if WS.node_raises_human_gate(n, gate)]
            if not raisers or any(n.get('worker_type') == 'frame' for n in raisers):
                continue
            res = WS.human_gate_resolution(entries, gate)
            if res['status'] == 'not-raised':
                continue
            try:
                raised = _instant(res['raised_at'])
            except (ValueError, TypeError):
                continue
            if raised <= started:
                continue
            if chosen is None or raised > chosen[0]:
                chosen = (raised, gate, res, raisers)
        if chosen is None:
            return None
        _, gate, res, raisers = chosen
        gated = sorted({s for n in raisers for s in WS.route_successors(route, str(n['id']))})
        if proposal and not any(DC._route_module().owner_executed_terminal(n) for n in route['nodes']
                                if n.get('id') in gated):
            return None
        inline = [str(n['id']) for n in raisers if gate in n.get('inline_human_gates', [])]
        completion = DC.dispatch_state_root(jobs)/'completion'/route['route_id']
        if any((completion/(node+'.json')).exists() for node in gated+inline):
            return None
        if any(node in ledger._rebuild(entries)['nodes'] for node in gated):
            return None
        for _, (_, other) in rows.items():
            if (other.get('route_id') == route['route_id'] and other.get('route_node') in gated
                    and other.get('worker_type') != 'owner'):
                return None
        return {'gate': gate, 'status': res['status'], 'epoch': res['epoch'],
                'raised_at': res['raised_at'], 'artifact': res.get('artifact'),
                'route_file': str(path), 'route_id': route['route_id'],
                'route_hash': route['route_hash'], 'gated_nodes': gated}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _logical_key(route, meta):
    # Sealed continuation lineage carries its original route through generations.
    from route_lineage import verified_route_lineage
    lineage = verified_route_lineage(route)
    origin = lineage[-1]['route_id']
    node = '__owner__' if meta.get('worker_type') == 'owner' else meta.get('route_node')
    if not origin or not node:
        raise DC.DispatchContractError('replacement-logical-node-unproven')
    return {'root_route_id': origin, 'node': node}


def _record_path(jobs, family):
    if not re.fullmatch(r'[0-9a-f]{64}', family):
        raise DC.DispatchContractError('replacement-family-invalid')
    return _directory(jobs)/'claims'/(family+'.json')


def source_reservation(jobs, aid):
    """The first durable write consumes the budget even before row annotation."""
    index = _read(_directory(jobs)/'by-source'/(_id(aid)+'.json'))
    if index is not None and (index.get('schema') != SCHEMA
            or index.get('original_attempt_id') != aid
            or not re.fullmatch(r'[0-9a-f]{64}', index.get('family_id', ''))):
        raise DC.DispatchContractError('replacement-claim-invalid')
    return index


def _reserve_source(jobs, aid, family):
    _once(_directory(jobs)/'by-source'/(_id(aid)+'.json'),
          {'schema': SCHEMA, 'original_attempt_id': aid, 'family_id': family})


def source_binding(jobs, meta):
    """(family_id, replacement_attempt_id, claim_digest) of the claim this row is the source of.

    A replacement row's own family fields describe how it was created, so when such a
    row becomes a source again the by-source index and claim record are the evidence.
    """
    if not meta.get('replacement_original_attempt_id'):
        family = meta.get('replacement_family_id')
        return (family, meta.get('replacement_attempt_id', ''),
                meta.get('replacement_claim_digest', '')) if family else None
    index = source_reservation(jobs, _id(meta.get('attempt_id')))
    if not index:
        return None
    record = _read(_record_path(jobs, index['family_id']))
    if record is None:
        return index['family_id'], '', ''
    return index['family_id'], record.get('replacement_attempt_id', ''), _digest(record)


def _is_capacity_record(record):
    return 'after_capacity' in ((record or {}).get('logical_node') or {})


def _transition_of(record):
    """The one profile transition a claim may carry, or None. A claim that says more or less than the
    frame rule is refused: the value is also what lets a launch run below its sealed profile."""
    kind = (record.get('proof') or {}).get('death_kind')
    transition = record.get('profile_transition')
    if transition is None:
        if kind == FRAME_CAPACITY:
            raise DC.DispatchContractError('replacement-profile-transition-invalid')
        return None
    if transition != FRAME_TRANSITION or kind != FRAME_CAPACITY or _is_capacity_record(record):
        raise DC.DispatchContractError('replacement-profile-transition-invalid')
    return dict(transition)


def read_profile_transition(jobs, *, route, node, attempt_id, lines=None):
    """The verified frame-rule `top`->`deep` transition this attempt is the replacement for, or None.

    The one reader every seam that lets a launch run below its node's sealed profile asks. It finds the
    claim through the source row's own binding, then runs the admission-time source proof
    (`validate_claim_source`), so a claim only counts while its source still is a proven capacity death
    on this exact route, node and claim. No claim means None and the sealed profile stays the only one
    allowed; a malformed claim raises. Nothing is written."""
    lines = Path(jobs).read_text().splitlines() if lines is None else lines
    sources = [(fields, meta) for fields, meta in _rows(lines).values()
               if meta.get('replacement_attempt_id') == attempt_id]
    if len(sources) != 1:
        return None
    _, source = sources[0]
    binding = source_binding(jobs, source)
    if not binding or not binding[0]:
        return None
    record = _check_record(_read(_record_path(jobs, binding[0])), binding[0],
                           {'replacement_claim_digest': binding[2] or None})
    transition = _transition_of(record)
    if transition is None:
        return None
    if (record['replacement_attempt_id'] != attempt_id
            or (record.get('logical_node') or {}).get('node') != node
            or (route or {}).get('route_id') != record['route_id']
            or (route or {}).get('route_hash') != record['route_hash']):
        raise DC.DispatchContractError('replacement-profile-transition-mismatch')
    validate_claim_source(jobs, lines, record)
    return transition


def _replacement_in_flight(jobs, rows, source):
    """Has this source's replacement already launched and is it still running or done well?"""
    binding = source_binding(jobs, source)
    row = rows.get(binding[1]) if binding and binding[1] else None
    if not row or row[1].get('launch_claimed') != '1':
        return False
    return row[0][1] in {'open','running'} or DC.verdict_pass(row[1])


def _in_capacity_family(jobs, meta):
    """Is this replacement row the launch of a capacity family (a pause, not a budget)?"""
    family = meta.get('replacement_family_id')
    return bool(family and meta.get('replacement_original_attempt_id')
                and _is_capacity_record(_read(_record_path(jobs, family))))


def _check_record(record, family, source=None):
    if (not record or record.get('schema') != SCHEMA or record.get('family_id') != family
            or _digest(record.get('logical_node')) != family
            or record.get('replacement_attempt_id') != 'att-'+hashlib.sha256(
                ('replacement:'+family).encode()).hexdigest()[:48]
            or record.get('ordinal') != 1):
        raise DC.DispatchContractError('replacement-lineage-unproven')
    if source and source.get('replacement_claim_digest') not in {None, _digest(record)}:
        raise DC.DispatchContractError('replacement-claim-drift')
    return record


def legacy_budget_exhausted(jobs, lines, source, *, route=None, include_family=False):
    """Read SD106 and SD157 consumption from the caller's jobs-lock snapshot.

    Do not move this check ahead of SD106's exact existing-recovery-id replay.
    SD157 permits its own by-source reservation to resume; SD106 calls with
    include_family=True and must not consume even that reservation again.
    No registry, route, or index is written and no second jobs lock is taken.
    """
    rows = _rows(lines)
    aid = _id(source.get('attempt_id'))
    references = [(fields, meta) for fields, meta in rows.values()
                  if meta.get('retry_attempt_id') == aid]
    if len(references) > 1:
        raise DC.DispatchContractError('replacement-legacy-budget-ambiguous')
    if references:
        _, prior = references[0]
        if (prior.get('retry_ordinal') != '1' or not prior.get('recovery_id')
                or DC._stable_recovery_attempt_id(prior['recovery_id']) != aid
                or not route_authority.replacement_parent_matches(prior, source, jobs, lineage=True)
                or any(prior.get(key) != source.get(key) for key in
                       ('route_node',
                        'worker_type', 'dispatch_depth'))):
            raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
        # The current SD104/106 gap executes on the original source route.
        if (prior.get('route_id') and prior.get('route_hash')
                and (prior['route_id'], prior['route_hash'])
                == (source.get('route_id'), source.get('route_hash'))):
            return True

    reference_ids = {meta.get('attempt_id') for _, meta in references}
    candidates = []
    for fields, meta in rows.values():
        other = meta.get('attempt_id')
        same_node = (meta.get('worker_type') == 'owner'
                     if source.get('worker_type') == 'owner'
                     else meta.get('worker_type') != 'owner'
                     and meta.get('route_node') == source.get('route_node'))
        if not same_node and other not in reference_ids and meta.get('automatic_retry_of') != aid:
            continue
        # The original row can be unchanged after an index-first crash.
        # Delay unrelated index errors until the row's lineage is established.
        attention = reservation = None
        index_error = None
        try:
            attention = _read(Path(jobs).parent / 'recovery-attention' /
                              'by-source' / (_id(other) + '.json'))
            if attention is not None and (
                    attention.get('original_attempt_id') != other
                    or not attention.get('recovery_id')):
                raise DC.DispatchContractError('replacement-recovery-attention-invalid')
            reservation = source_reservation(jobs, other)
        except (DC.DispatchContractError, OSError, ValueError, TypeError) as exc:
            index_error = exc
        legacy = (meta.get('retry_ordinal') == '1'
                  or meta.get('recovery_exhausted') == '1'
                  or (meta.get('start_permitted') == '0' and meta.get('recovery_id'))
                  or attention is not None or bool(meta.get('automatic_retry_of')
                                                    and not meta.get('replacement_family_id')))
        family_hint = (reservation or {}).get('family_id')
        if family_hint and _is_capacity_record(_read(_record_path(jobs, family_hint))):
            if include_family and other == aid:
                return True  # SD106 must not add a second successor to a resumed source.
            family_hint = None
        # SD157's own reservation is a replay, not a second consumption.
        # Its own SD106 exhaustion, however, is always a veto.
        family_consumes = bool(family_hint) and (other != aid or include_family)
        if legacy or family_consumes or index_error or other in reference_ids:
            candidates.append((fields, meta, bool(legacy), family_hint,
                               family_consumes, index_error))
    if not candidates and not references:
        # Preserve legacy route-less first-claim callers: no consumption
        # evidence means no reason to require a historical route here.
        return False

    from route_lineage import verified_route_lineage

    def historical_route(meta):
        path = meta.get('owner_route_file') or meta.get('route_file')
        if not path:
            return None
        value = _read(Path(path))
        if value is None:
            return None
        expected_id = meta.get('owner_route_id') or meta.get('route_id')
        expected_hash = meta.get('owner_route_hash') or meta.get('route_hash')
        if value.get('route_id') != expected_id or value.get('route_hash') != expected_hash:
            raise DC.DispatchContractError('replacement-legacy-budget-route-unproven')
        verified_route_lineage(value)
        return value

    if route is None:
        route = historical_route(source)
    if route is None:
        # An exact predecessor reference cannot be waved away as unrelated.
        if references or any(meta.get('attempt_id') == aid and
                             (legacy or family_consumes or error)
                             for _, meta, legacy, _, family_consumes, error in candidates):
            raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
        return False
    logical = _logical_key(route, source)
    family = _digest(logical)
    lineage = {r['route_id']: r for r in verified_route_lineage(route)}
    if include_family:
        record = _read(_record_path(jobs, family))
        if record is not None:
            _check_record(record, family)
            return True

    for _, prior, legacy, family_hint, family_consumes, index_error in candidates:
        other = prior.get('attempt_id')
        # A checked by-source index is already a durable exact family binding;
        # no family record or original-row annotation need exist yet.
        if family_consumes and family_hint == family:
            if index_error:
                raise DC.DispatchContractError('replacement-legacy-budget-link-unproven') from index_error
            return True
        prior_id = prior.get('owner_route_id') or prior.get('route_id')
        prior_hash = prior.get('owner_route_hash') or prior.get('route_hash')
        previous = lineage.get(prior_id)
        direct = (previous is not None or other in reference_ids or other == aid
                  or prior.get('automatic_retry_of') == aid)
        if prior.get('automatic_retry_of') == aid and (
                not route_authority.replacement_parent_matches(source, prior, jobs, lineage=True)
                or any(prior.get(key) != source.get(key) for key in
                       ('route_id', 'route_hash', 'route_node', 'worker_type', 'dispatch_depth'))):
            raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
        try:
            if previous is not None:
                if previous['route_hash'] != prior_hash:
                    raise DC.DispatchContractError('replacement-legacy-budget-route-unproven')
            else:
                # Other streams share both a registry and common node names.
                # Their unavailable/corrupt history does not poison this stream.
                previous = historical_route(prior)
            if previous is None:
                if direct:
                    raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
                continue
            same_logical = _logical_key(previous, prior) == logical
        except (DC.DispatchContractError, OSError, ValueError, TypeError) as exc:
            if direct:
                raise DC.DispatchContractError('replacement-legacy-budget-link-unproven') from exc
            continue
        if not same_logical:
            if other in reference_ids:
                raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
            continue
        # From here on the prior row belongs to this exact logical family.
        if index_error:
            raise DC.DispatchContractError('replacement-legacy-budget-link-unproven') from index_error
        if family_consumes and family_hint != family:
            raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
        if legacy or family_consumes:
            return True
    if references:
        raise DC.DispatchContractError('replacement-legacy-budget-link-unproven')
    return False


def _budget_exhausted(jobs, source, *, lines=None, route=None, capacity=False):
    """`capacity`: the source stopped at a usage limit; only its SD106 signals count."""
    aid = _id(source.get('attempt_id'))
    index = _read(Path(jobs).parent/'recovery-attention/by-source'/(aid+'.json'))
    if index is not None:
        if index.get('original_attempt_id') != aid or not index.get('recovery_id'):
            raise DC.DispatchContractError('replacement-recovery-attention-invalid')
        return True
    paused = source.get('replacement_original_attempt_id') and (capacity or _in_capacity_family(jobs, source))
    if ((source.get('automatic_retry_of') and not paused) or source.get('retry_ordinal') == '1'
            or source.get('recovery_exhausted') == '1' or source.get('start_permitted') == '0'):
        return True
    recovery = source.get('recovery_id')
    if recovery:
        path = Path(jobs).parent/'recovery-attention'/(hashlib.sha256(recovery.encode()).hexdigest()+'.json')
        record = _read(path)
        if record is not None:
            if record.get('recovery_id') != recovery or record.get('original_attempt_id') != source.get('attempt_id'):
                raise DC.DispatchContractError('replacement-recovery-attention-invalid')
            return True
    if capacity:
        return False
    if lines is None:
        lines = Path(jobs).read_text().splitlines()
    return legacy_budget_exhausted(jobs, lines, source, route=route)


def _no_competing_successor(rows, aid, replacement=None):
    for other, (_, candidate) in rows.items():
        if other != replacement and (candidate.get('automatic_retry_of') == aid
                or candidate.get('prior_attempt_id') == aid):
            raise DC.DispatchContractError('automatic-replacement-exhausted', aid)


def claim(jobs: Path, aid: str) -> dict:
    """Lock-held death, children and terminal-fence checks precede one claim."""
    aid = _id(aid)
    with _locked(jobs) as lines:
        rows = _rows(lines)
        if aid not in rows:
            raise DC.DispatchContractError('replacement-source-missing', aid)
        fields, meta = rows[aid]
        DC.validate_attempt_metadata(meta)
        binding = source_binding(jobs, meta)
        if binding:
            old_family, _, claim_digest = binding
            record = _check_record(_read(_record_path(jobs, old_family)), old_family,
                                   {'replacement_claim_digest': claim_digest or None})
            if not record or aid not in {record['original_attempt_id'], record['replacement_attempt_id']}:
                raise DC.DispatchContractError('replacement-lineage-unproven')
            if aid == record['replacement_attempt_id']:
                raise DC.DispatchContractError('automatic-replacement-exhausted', record['logical_node']['node'])
            _no_competing_successor(rows, aid, record['replacement_attempt_id'])
            return record
        proof = death_proof(fields, meta, jobs=jobs, lines=lines)
        capacity = proof.get('death_kind') in PAUSE_KINDS
        path, route = _route(jobs, aid, meta)
        if proof.get('death_kind') == CORRECTED and route_authority.answerable_owner_end(fields[1], meta) == 'FAIL':
            # The approved fix answers each failed check's last FAIL: one closure-check round each,
            # within the verdict ceiling cap + 1. A check that already used it gets no new round.
            answers, spent = route_authority.fix_answers(route, lines, jobs)
            if not answers and spent:
                raise DC.DispatchContractError('replacement-fix-round-spent', ','.join(spent))
            proof['answers'] = answers
            proof['source_result'] = 'FAIL'
        logical = _logical_key(route, meta)
        if capacity:
            # Every usage-limit stop opens its own family: a pause, not the one silent replacement.
            logical = {**logical, 'after_capacity': aid}
        family = _digest(logical)
        old = _read(_record_path(jobs, family))
        if old:
            _check_record(old, family)
            if old.get('original_attempt_id') != aid:
                raise DC.DispatchContractError('automatic-replacement-exhausted', logical['node'])
            _no_competing_successor(rows, aid, old['replacement_attempt_id'])
            _reserve_source(jobs, aid, family)
            _bind_source(jobs, lines, aid, old)
            return old
        if _budget_exhausted(jobs, meta, lines=lines, route=route, capacity=capacity):
            raise DC.DispatchContractError('automatic-replacement-exhausted', logical['node'])
        _no_competing_successor(rows, aid)
        owner = aid if meta.get('worker_type') == 'owner' else meta.get('parent_attempt_id') or aid
        _source_fences(jobs, meta, route['route_id'], aid)
        if meta.get('worker_type') == 'owner':
            _children_quiescent(rows, aid)
        replay = launch_input(jobs, aid, meta)
        replacement = 'att-'+hashlib.sha256(('replacement:'+family).encode()).hexdigest()[:48]
        record = {'schema': SCHEMA, 'family_id': family, 'logical_node': logical,
                  'original_attempt_id': aid, 'replacement_attempt_id': replacement, 'ordinal': 1,
                  'route_file': str(path), 'route_id': route['route_id'], 'route_hash': route['route_hash'],
                  'input_digest': _digest(replay), 'proof': proof, 'proof_digest': _digest(proof),
                  'reuse': _reuse_snapshot(jobs, route, lines)}
        if proof.get('death_kind') == FRAME_CAPACITY:
            record['profile_transition'] = dict(FRAME_TRANSITION)
        moved = route_authority.moved_owner_harness(route, replay.get('harness')) \
            if meta.get('worker_type') == 'owner' else None
        if moved:
            # Historical observation; the ordinary owner selector reads the pin at launch.
            record['harness'] = moved
        access = _access_in_force(jobs, route) if meta.get('worker_type') == 'owner' else None
        if access:
            # The route's parent handed the next owner an access request: the replacement runs with it.
            record['execution_access'] = access
        # Index first: every retry admission sees the consumed budget after a crash.
        _reserve_source(jobs, aid, family)
        _once(_record_path(jobs, family), record)
        _bind_source(jobs, lines, aid, record)
        return record


def answered_fix_revisions(jobs, route_id):
    """The approved fixes that answer a route's failed checks: one revision-like entry per
    claim that continued a FAIL-ended owner of this route with a person's answer, naming the
    FAIL attempts that claim pinned. Round admission reads them with the node's revisions, so
    each pinned FAIL gets its one closure-check round."""
    try:
        paths = sorted((_directory(jobs)/'claims').glob('*.json'))
    except OSError:
        return []
    found = []
    for path in paths:
        try:
            record = _read(path)
        except DC.DispatchContractError:
            continue
        proof = (record or {}).get('proof') or {}
        if (record and record.get('route_id') == route_id and proof.get('death_kind') == CORRECTED
                and proof.get('source_result') == 'FAIL' and proof.get('answers')):
            found.append({'basis': 'user-direction', 'answers': list(proof['answers']),
                          'corrections': [item.get('id') for item in proof.get('corrections') or []],
                          'family_id': record.get('family_id')})
    return found


def _bind_source(jobs, lines, aid, record):
    values = {'replacement_family_id': record['family_id'],
              'replacement_attempt_id': record['replacement_attempt_id'], 'replacement_ordinal': '1',
              'replacement_claim_digest': _digest(record)}
    for i,line in enumerate(lines):
        fields=line.split('\t')
        if len(fields)!=6 or not DC.row_has_attempt(fields[5],aid):
            continue
        meta=DC.parse_registry_metadata(fields[5])
        if meta.get('replacement_original_attempt_id'):
            return  # Its own family fields stay; the by-source index and claim record bind it.
        for key,value in values.items():
            if key in meta and meta[key]!=value:
                raise DC.DispatchContractError('replacement-lineage-conflict')
        fields[5]+=''.join(','+key+'='+value for key,value in values.items() if key not in meta)
        lines[i]='\t'.join(fields)
        DC._atomic_registry_replace(Path(jobs),lines)
        return
    raise DC.DispatchContractError('replacement-source-missing')


def _reuse_snapshot(jobs, route, lines):
    """Pin every current completion, including non-prefix successful siblings."""
    module = DC._route_module()
    directory = module.completion_dir(route['route_id'], jobs=Path(jobs))
    completed = []
    for node in route.get('nodes', []):
        path = directory/(str(node['id'])+'.json')
        if not path.exists():
            continue
        marker = _read(path)
        if not DC.completion_marker_is_current(route, node, path, marker):
            raise DC.DispatchContractError('replacement-completion-unproven', str(node['id']))
        ready = DC.completion_attempt_readiness(route, node, marker, Path(jobs), registry_lines=lines)
        if ready.state != 'ready':
            raise DC.DispatchContractError('replacement-completion-unsettled', str(node['id']))
        # The replacement owner reuses this completion: a gates-off evidence edit is kept as history.
        DC.note_evidence_change(route, node, path, marker)
        completed.append({'node': node['id'], 'marker_digest': _digest(marker),
                          'attempt_id': marker.get('attempt_id', '')})
    root = Path(route['artifact_root'])
    from artifact_producer import route_cycle_for, cycle_route_admission
    cycle = route_cycle_for(root, route)
    if not cycle or not cycle_route_admission(root, cycle, route).allow:
        raise DC.DispatchContractError('replacement-producer-not-open')
    return {'completed': completed, 'cycle_id': cycle['cycle_id'],
            'producer_id': cycle.get('producer_id', ''), 'gates': _gate_snapshot(jobs, route)}


def _gate_snapshot(jobs, route):
    import workflow_state as WS
    ledger = WS.WorkflowLedger(route['route_id'], route['route_hash'], jobs=jobs)
    try:
        raw = ledger.journal_path.read_text()
    except FileNotFoundError:
        raw = ''
    try:
        entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except ValueError as exc:
        raise DC.DispatchContractError('replacement-workflow-journal-unreadable') from exc
    if any(not isinstance(e,dict) or e.get('route_id') != route['route_id']
           or e.get('route_hash') != route['route_hash'] for e in entries):
        raise DC.DispatchContractError('replacement-workflow-journal-mismatch')
    if ledger._rebuild(entries)['workflow_state'] in {'CANCELLED','COMPLETE'}:
        raise DC.DispatchContractError('replacement-workflow-not-open')
    names = {str(b['gate']) for b in route.get('human_gate_bindings',[]) if b.get('gate')}
    names.update(str((e.get('evidence') or {})['gate']) for e in entries
                 if e.get('workflow_state') == 'BLOCKED_HUMAN_GATE' and (e.get('evidence') or {}).get('gate'))
    gates = [WS.human_gate_resolution(entries,name) for name in sorted(names)]
    if any(g['status'] in {'revise','stop'} for g in gates):
        raise DC.DispatchContractError('replacement-human-gate-changed-scope')
    return gates


def _reuse_preserved(previous, current):
    if any(previous.get(key) != current.get(key) for key in ('cycle_id','producer_id')):
        return False
    now = {r['node']:r for r in current.get('completed',[])}
    if any(now.get(r['node']) != r for r in previous.get('completed',[])):
        return False
    gates = {r['gate']:r for r in current.get('gates',[])}
    for old in previous.get('gates',[]):
        new = gates.get(old['gate'])
        if new is None:
            return False
        if old['status'] == 'not-raised':
            continue
        if old['status'] == 'blocked' and new['status'] == 'proceed':
            # The same raise acquired its answer while launch was interrupted.
            if any(old.get(k) != new.get(k) for k in
                   ('epoch','raised_at','artifact','artifact_sha256','interview','questions','release_authority')):
                return False
        elif old != new:
            return False
    return True


def _source_fences(jobs, source, route_id, aid):
    owner = aid if source.get('worker_type') == 'owner' else source.get('parent_attempt_id') or aid
    for rid in {route_id, source.get('owner_route_id'), source.get('route_id')} - {None, ''}:
        DC.ensure_terminal_claim_absent(jobs, rid, owner)


def validate_claim_source(jobs, lines, record):
    """Shared lock-held source proof for admission and partial-batch reservation."""
    family = record.get('family_id', '')
    _check_record(record, family)
    canonical = _read(_record_path(jobs, family))
    if canonical != record:
        raise DC.DispatchContractError('replacement-claim-drift')
    rows = _rows(lines); prior = record['original_attempt_id']
    if prior not in rows:
        raise DC.DispatchContractError('replacement-source-missing')
    fields, source = rows[prior]
    if (source_binding(jobs, source) != (family, record['replacement_attempt_id'], _digest(record))
            or (source_reservation(jobs, prior) or {}).get('family_id') != family):
        raise DC.DispatchContractError('replacement-claim-pending')
    _no_competing_successor(rows, prior, record['replacement_attempt_id'])
    if _budget_exhausted(jobs, source, lines=lines, capacity=_is_capacity_record(record)):
        raise DC.DispatchContractError('automatic-replacement-exhausted', prior)
    death_proof(fields, source, jobs=jobs, lines=lines)
    _source_fences(jobs, source, record['route_id'], prior)
    if source.get('worker_type') == 'owner':
        _children_quiescent(rows, prior)
    replay = launch_input(jobs, prior, source)
    if _digest(replay) != record['input_digest']:
        raise DC.DispatchContractError('replacement-input-drift')
    path, route = _route(jobs, prior, source)
    if str(path) != record['route_file'] or route.get('route_hash') != record['route_hash']:
        raise DC.DispatchContractError('replacement-route-drift')
    current_reuse = _reuse_snapshot(jobs, route, lines)
    if not _reuse_preserved(record['reuse'], current_reuse):
        raise DC.DispatchContractError('replacement-reuse-evidence-drift')
    if source.get('worker_type') == 'owner' and any(g['status'] == 'blocked' for g in current_reuse.get('gates',[])):
        raise DC.DispatchContractError('replacement-human-gate-pending')
    return fields, source, replay


def admission(jobs, lines, metadata):
    """Called with the jobs lock at both registration and actual spawn."""
    prior = metadata.get('automatic_retry_of') or metadata.get('prior_attempt_id')
    if not prior:
        return None
    rows = _rows(lines)
    if prior not in rows:
        raise DC.DispatchContractError('retry-predecessor-missing', prior)
    source_fields, source = rows[prior]
    reservation = source_reservation(jobs, prior)
    binding = source_binding(jobs, source)
    family = binding[0] if binding else (reservation or {}).get('family_id')
    if reservation and not (binding and binding[2]):
        raise DC.DispatchContractError('replacement-claim-pending', prior)
    if not family:
        # The candidate's own row is not a consumed retry: it is present once spawn admits it.
        own = metadata.get('attempt_id')
        peers = [line for line in lines
                 if not (own and len(line.split('\t')) == 6 and DC.row_has_attempt(line.split('\t')[5], own))]
        if _budget_exhausted(jobs, source, lines=peers):
            raise DC.DispatchContractError('automatic-replacement-exhausted', prior)
        return None
    record = _check_record(_read(_record_path(jobs, family)), family,
                           {'replacement_claim_digest': (binding[2] if binding else '') or None})
    if (not record or record.get('original_attempt_id') != prior
            or record.get('replacement_attempt_id') != metadata.get('attempt_id')):
        raise DC.DispatchContractError('automatic-replacement-exhausted', prior)
    route_authority.require_recovery_binding(record, source, metadata, jobs)
    _, source, replay = validate_claim_source(jobs, lines, record)
    candidate = launch_input(jobs, metadata['attempt_id'], metadata)
    parent_values = {}
    if source.get('dispatch_depth') == '1' and metadata.get('parent_sid') != source.get('parent_sid'):
        from dispatch_seat_handover import effective_parent_harness
        parent_values = {'parent_session_id': metadata['parent_sid'],
                         'parent_harness': effective_parent_harness(source, jobs)}
        if 'parent_completion_delivery' in (candidate.get('resolved') or {}):
            from types import SimpleNamespace
            from dispatch_parent_completion import resolve_parent_completion_delivery
            parent_args = SimpleNamespace(**{**candidate['resolved'], **parent_values,
                                            'action': 'start', 'dispatch_depth': 1})
            parent_values['parent_completion_delivery'] = resolve_parent_completion_delivery(parent_args)
    expected_task = route_authority.recovery_task(record, source, replay)
    _, current_route = _route(jobs, prior, source)
    route_authority.require_recovery_work(
        candidate, replay, route=current_route, worker_type=source.get('worker_type'),
        transition=_transition_of(record), access=record.get('execution_access'),
        parent_values=parent_values)
    if candidate.get('task') != expected_task:
        raise DC.DispatchContractError('replacement-task-mismatch')
    if Path(record['route_file']).with_suffix('.outcome.json').exists():
        raise DC.DispatchContractError('replacement-route-closed')
    route = _read(Path(record['route_file']))
    if not route or route.get('route_hash') != record['route_hash']:
        raise DC.DispatchContractError('replacement-route-drift')
    return record


# What the installed runtime derives; a release change may move these, a different task may not.
RUNTIME_DERIVED_KEYS = route_authority.RELEASE_DERIVED_VALUES


def _check_tuple(candidate, replay, transition=None, access=None, parent_values=None):
    """Compatibility entry for stored standalone inputs; policy lives in route authority."""
    return route_authority.require_recovery_work(
        candidate, replay, transition=transition, access=access, parent_values=parent_values)


def replacement_row(jobs, lines, row):
    row = row.rstrip('\n')
    fields = row.split('\t'); metadata = DC.parse_registry_metadata(fields[5])
    record = admission(jobs, lines, metadata)
    if not record:
        return row, False
    values = {'replacement_family_id': record['family_id'],
              'replacement_original_attempt_id': record['original_attempt_id'], 'replacement_ordinal': '1',
              'replacement_claim_digest': _digest(record)}
    for key, value in values.items():
        if key in metadata and metadata[key] != value:
            raise DC.DispatchContractError('replacement-lineage-conflict')
        if key not in metadata:
            fields[5] += ','+key+'='+value
    return '\t'.join(fields), True


def _replace_options(argv, replacements, remove=()):
    """Parse long options without ever passing through a shell."""
    result = []; i = 0
    while i < len(argv):
        arg = argv[i]; key = arg.split('=',1)[0]
        if key in replacements or key in remove:
            if '=' not in arg and i+1 < len(argv) and not argv[i+1].startswith('--'):
                i += 1
        else:
            result.append(arg)
        i += 1
    for key, value in replacements.items():
        result.append(key)
        if value is not None:
            result.append(str(value))
    return result


def _canonical_argv(argv):
    """Order-independent option groups; repeated values retain their own order."""
    groups=[]; i=0
    while i<len(argv):
        arg=argv[i]
        if not arg.startswith('--'):
            raise DC.DispatchContractError('replacement-argv-invalid')
        if '=' in arg:
            key,value=arg.split('=',1); group=[key,value]
        else:
            group=[arg]
            if i+1<len(argv) and not argv[i+1].startswith('--'):
                group.append(argv[i+1]); i+=1
        groups.append(group);i+=1
    return [value for group in sorted(groups,key=lambda g:g[0]) for value in group]


def _authorized(jobs, rows, meta):
    def current_session():
        from work_start import _current_parent_session_id
        return _current_parent_session_id()
    route_authority.require_replacement_parent(jobs, rows, meta, current_session=current_session)


def _replacement_task(record, source, replay):
    return route_authority.recovery_task(record, source, replay)


def _access_in_force(jobs, route):
    """The prepared request the route's parent last handed its owner (`route_authority.access_in_force`),
    with its digest. GPU lab replacements also prepare the current runtime's
    normal compute defaults instead of replaying an older owner's request."""
    change = route_authority.access_in_force(route)
    from gpu_execution_sandbox import select as gpu_selection
    runtime_defaults = ((change is None or change.get('source') == 'derived')
                        and route.get('capability') == 'autopilot-lab'
                        and gpu_selection(route, environ={})['gpu_scope'])
    if not runtime_defaults and (change is None or change.get('source') == 'derived'):
        return None
    import execution_access as EA
    try:
        path = EA.prepare_task_request(route, jobs, node='owner', environment=False)
        context = EA.AccessContext.build(
            worktree=str(route.get('cwd') or ''), artifact_root=str(route.get('artifact_root') or ''),
            dispatch_state_root=Path(jobs).resolve().parent, agent_home=ROOT)
        request = EA.load_request(path, context=context) if path is not None else None
    except (EA.ExecutionAccessError, OSError, ValueError):
        return None
    if request is None:
        return None
    return {'request_path': str(path), 'request_sha256': request.request_sha256,
            'source': 'lab-runtime-defaults' if runtime_defaults else change['source'],
            'at': change['at'] if change else None}


def _replacement_argv(record, source, replay, parent_values=None):
    options = {}
    for key in ('parent_session_id', 'parent_harness'):
        if key in (parent_values or {}):
            options['--'+key.replace('_','-')] = parent_values[key]
    access = record.get('execution_access')
    if access:
        options['--execution-access-file'] = access['request_path']
    transition = _transition_of(record)
    if transition:
        options['--model-profile'] = transition['to']
    if source.get('worker_type') != 'owner' or source.get('route_file'):
        options.update({'--route-file': record['route_file'], '--route-id': record['route_id'],
                        '--route-hash': record['route_hash']})
    return _canonical_argv(_replace_options(replay['argv'], options, remove=INPUT_OPTIONS))


def _command(jobs, record, source, replay):
    root = ROOT.resolve()
    if replay['harness'] not in {'codex','claude','opencode'}:
        raise DC.DispatchContractError('replacement-runtime-mismatch')
    task = _replacement_task(record, source, replay)
    prompt = _directory(jobs)/'tasks'/(record['replacement_attempt_id']+'.txt')
    prompt.parent.mkdir(parents=True, exist_ok=True)
    raw = task.encode()
    if prompt.is_symlink() or (not _write_once(prompt.parent,prompt,raw) and prompt.read_bytes()!=raw):
        raise DC.DispatchContractError('replacement-task-conflict')
    options = {'--start': None, '--attempt-id': record['replacement_attempt_id'],
               '--automatic-retry-of': record['original_attempt_id'], '--prompt-file': prompt,
               '--jobs': Path(jobs).resolve(), '--worktree': replay['worktree']}
    argv = _replace_options(_replacement_argv(record, source, replay), options)
    return [sys.executable,str(root/f'adapters/{replay["harness"]}/bin/dispatch-headless.py'),*argv]


def _argv_value(argv, flag):
    for index, arg in enumerate(argv):
        if arg == flag and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith(flag + '='):
            return arg.split('=', 1)[1]
    return None


def _owner_command(jobs, record, source, replay):
    """Resolve a replacement through the ordinary owner launch, including an old claim.

    The source command may belong to another harness, so it is not replayed: the same work (task,
    route, worktree, access request, parent session) takes the ordinary owner launch path, which
    builds the new harness's own command from the route in force."""
    task = _replacement_task(record, source, replay)
    prompt = _directory(jobs)/'tasks'/(record['replacement_attempt_id']+'.txt')
    prompt.parent.mkdir(parents=True, exist_ok=True)
    raw = task.encode()
    if prompt.is_symlink() or (not _write_once(prompt.parent,prompt,raw) and prompt.read_bytes()!=raw):
        raise DC.DispatchContractError('replacement-task-conflict')
    command = [sys.executable, str(ROOT.resolve()/'utilities/dispatch-owner.py'), '--start',
               '--route-evidence', record['route_file'],
               '--jobs', str(Path(jobs).resolve()), '--worktree', replay['worktree'],
               '--slug', _argv_value(replay['argv'], '--slug') or record['replacement_attempt_id'],
               '--attempt-id', record['replacement_attempt_id'],
               '--automatic-retry-of', record['original_attempt_id'], '--prompt-file', str(prompt)]
    for flag in ('--execution-access-file', '--parent-session-id'):
        value = ((record.get('execution_access') or {}).get('request_path') if flag == '--execution-access-file'
                 else None) or _argv_value(replay['argv'], flag)
        if value:
            command += [flag, value]
    return command


# The detached launcher returns once the owner is claimed; slow shared storage needs room.
LAUNCHER_TIMEOUT_SECONDS = 600


def _capacity_wait(jobs, aid, source, hold=None):
    """Nothing is written: a usage limit is a pause the person resumes with `start`."""
    result = {'state': 'needs-attention', 'reason': 'replacement-capacity-wait',
              'source_attempt_id': aid, 'node': source.get('route_node') or '__owner__',
              'harness': source.get('harness') or source.get('owner_harness') or ''}
    if hold is None:
        hold = _capacity_hold(jobs, source)
        if hold is None and source.get('worker_type') == 'owner':
            # Preserve reset evidence for a capacity-dead owner before an explicit resume.
            from dispatch_capacity_evidence import harness_hold
            hold = harness_hold(jobs, result['harness'], model=source.get('model'))
    if hold:
        result['usage_state'] = hold['label']
        for key in ('headroom', 'usage_gate_used_percent', 'capacity_source'):
            if key in hold:
                result[key] = hold[key]
        if hold.get('until_epoch'):
            result['retry_at'] = datetime.fromtimestamp(hold['until_epoch'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return result


def _capacity_reader():
    spec = importlib.util.spec_from_file_location('replacement_harness_capacity', ROOT/'utilities/harness-capacity.py')
    capacity = importlib.util.module_from_spec(spec); spec.loader.exec_module(capacity)
    return capacity


def _capacity_hold(jobs, source, model=None):
    from dispatch_capacity_evidence import harness_hold, usage_states
    _, route = _route(jobs, source.get('attempt_id'), source)
    from model_profile import sealed_pin_harness
    pin = sealed_pin_harness(route_authority.route_in_force(route), worker_type=source.get('worker_type'))
    harness = source.get('harness') or source.get('owner_harness')
    if source.get('worker_type') == 'owner':
        # The ordinary selector judges all candidates; the old owner's limit or
        # soft allocation gate cannot prevent that selection. A named pin waits
        # only at a real limit, and admission refuses a silently ignored pin.
        return harness_hold(jobs, pin, model=model) if pin else None
    if not harness:
        return None
    hold = harness_hold(jobs, harness, model=model or source.get('model'))
    if hold:
        return hold
    if pin == harness:
        # A sealed pin moves only for a real usage limit (harness_hold above), as at its
        # first launch and on the stage path; the soft allocation gate does not move it.
        return None
    allocation = route.get('dispatch_allocation') or {}
    if allocation.get('strategy') not in {'balanced', 'capacity-aware'}:
        return None  # legacy routes keep their existing hard-quota contract
    policy = next(
        (node.get('harness_policy') for node in route.get('nodes', [])
         if node.get('id') == source.get('route_node')), None)
    if not isinstance(policy, dict):
        return None
    capacity = _capacity_reader()
    report = capacity.capacity_report()
    scores = report['scores']; score = scores.get(harness)
    gate = allocation.get('usage_gate_used_percent', 90)
    if (allocation['strategy'] == 'balanced' and not capacity.is_gated(
            scores, harness, usage_gate_used_percent=gate)):
        return None  # balanced unknown remains optimistic, as in the normal selector
    if allocation['strategy'] == 'capacity-aware' and score is not None and score > 0 and not capacity.is_gated(
            scores, harness, usage_gate_used_percent=gate):
        return None
    states = usage_states(jobs, models={harness: model or source.get('model')})
    from dispatch_allocation import attempt_counts
    counts = attempt_counts(jobs, window=allocation.get('window', 30))
    selected, _, _, _ = capacity.select(
        policy, states, counts, allocation.get('harness_order', []), scores,
        strategy=allocation['strategy'], usage_gate_used_percent=gate,
        preferred=capacity.preferred_for_depth(allocation, int(source.get('dispatch_depth', '1'))),
        affinity_weight=allocation.get('depth_affinity_weight', .5),
        headroom_exponent=allocation.get('usage_headroom_exponent', 1),
        harness_weights=allocation.get('harness_weights'),
    )
    if selected == harness:
        return None  # original all-gated recovery, quality bands and relief remain intact
    source_of_score = report['sources'].get(harness)
    return {'label': 'allocation-usage-gate',
            'until_epoch': capacity.gate_release_epoch(
                harness, source_of_score, usage_gate_used_percent=gate),
            'headroom': score, 'usage_gate_used_percent': gate,
            'capacity_source': source_of_score}


def _launcher_budget(command, source):
    from dispatch_lifecycle import (FOREGROUND_SCOPED, FOREGROUND_TIMEOUT_DEFAULT,
                                    FORWARDED_TERMINATION_GRACE, bounded_foreground_timeout,
                                    reconcile_launch_lifecycle)
    def option(name, default):
        value = default
        for index, token in enumerate(command):
            if token.startswith(name+'='):
                value = token.split('=', 1)[1]
            elif token == name and index+1 < len(command):
                value = command[index+1]
        return value
    requested = option('--launch-lifecycle', source.get('launch_lifecycle', 'detached'))
    foreground = reconcile_launch_lifecycle(requested).effective == FOREGROUND_SCOPED
    if not foreground:
        return False, LAUNCHER_TIMEOUT_SECONDS
    timeout = bounded_foreground_timeout(float(option('--foreground-timeout', FOREGROUND_TIMEOUT_DEFAULT)))
    return True, LAUNCHER_TIMEOUT_SECONDS + timeout + FORWARDED_TERMINATION_GRACE


def _retry_model(source, kind):
    """The model the replacement will run: the source's own, except a frame-rule transition's lower one."""
    if kind != FRAME_CAPACITY:
        return None
    from model_profile import resolve_runtime_profile
    return resolve_runtime_profile(source.get('harness'), FRAME_TRANSITION['to'])[0]['model']


def advance(jobs, aid, *, run=subprocess.run, authority_check=None, resume_capacity=False):
    """A bound runtime checkpoint, never a read-only observer, calls this.

    `resume_capacity` is set only by an explicit `start`: a usage-limit stop is replaced
    when the person resumes, never by a supervisor tick, so no loop of automatic launches exists.
    """
    rows = _rows(Path(jobs).read_text().splitlines())
    if aid not in rows:
        return {'state':'unavailable','reason':'replacement-source-missing'}
    fields, source = rows[aid]
    if (source.get('replacement_original_attempt_id') and fields[1] in {'open','running'}
            and source.get('launch_claimed') != '1'):
        return advance(jobs, source['replacement_original_attempt_id'], run=run,
                       authority_check=authority_check, resume_capacity=resume_capacity)
    kind = death_kind(fields, source, jobs=jobs)
    # A usage-limit resume is a pause, not the node's one silent replacement: when that
    # resumed attempt dies silently, claim() still judges the node's silent budget.
    if (source.get('replacement_original_attempt_id') and fields[1] not in {'open','running'}
            and not DC.verdict_pass(source) and kind not in PAUSE_KINDS
            and not _in_capacity_family(jobs, source)):
        return exhausted_attention(jobs, aid, source)
    # Avoid side effects or errors on ordinary success/live observations.
    if kind is None:
        parked = owner_parked_gate(jobs, aid) if source.get('note') == 'dead-worker-blocked' else None
        return {'state': 'not-applicable', 'parked_gate': parked} if parked else {'state':'not-applicable'}
    if kind == 'capacity' and not resume_capacity and not _replacement_in_flight(jobs, rows, source):
        return _capacity_wait(jobs, aid, source)
    if (kind in {'runtime', 'unlaunched', CORRECTED} and not resume_capacity
            and not _replacement_in_flight(jobs, rows, source)):
        return {'state': 'not-applicable'}  # never a supervisor tick: no loop of relaunches
    try:
        if authority_check is None:
            _authorized(jobs, rows, source)
        elif authority_check(jobs, aid, source) is not True:
            raise DC.DispatchContractError('replacement-parent-identity-unproven')
        binding = source_binding(jobs, source)
        if not (binding and binding[1] in rows and (rows[binding[1]][0][1] not in {'open','running'}
                or rows[binding[1]][1].get('launch_claimed') == '1')):
            # Nothing is launched yet: limit, drift and cleanup are judged before anything durable is written.
            _, current_route = _route(jobs, aid, source)
            moved = (route_authority.moved_owner_harness(current_route, launch_input(jobs, aid, source).get('harness'))
                     if source.get('worker_type') == 'owner' else None)
            hold_source = {**source, 'harness': moved, 'model': None} if moved else source
            hold = _capacity_hold(jobs, hold_source, None if moved else _retry_model(source, kind))
            if hold:
                return _capacity_wait(jobs, aid, hold_source, hold)
            _settle_terminal_cleanup(jobs, rows, aid, source)
        record = claim(Path(jobs), aid)
        rows = _rows(Path(jobs).read_text().splitlines())
        source = rows[aid][1]
        replacement = record['replacement_attempt_id']
        if replacement in rows:
            replacement_fields, replacement_meta = rows[replacement]
            if replacement_fields[1] not in {'open','running'}:
                if not DC.verdict_pass(replacement_meta):
                    next_kind = death_kind(replacement_fields, replacement_meta, jobs=jobs)
                    if next_kind in PAUSE_KINDS or (next_kind and _is_capacity_record(record)):
                        # The replacement stopped at a limit too, or a limit resume died on its
                        # own: it is the next source, and claim() judges the node's budget.
                        return advance(jobs, replacement, run=run, authority_check=authority_check,
                                       resume_capacity=resume_capacity)
                    return exhausted_attention(jobs, replacement, replacement_meta)
                return {'state':'reused','attempt_id':replacement,'record':record}
            if replacement_meta.get('launch_claimed') == '1':
                if source.get('worker_type') == 'owner':
                    _, route = _route(jobs, aid, source)
                    from model_profile import sealed_pin_harness
                    pinned = sealed_pin_harness(route_authority.route_in_force(route), worker_type='owner')
                    if pinned and replacement_meta.get('harness') != pinned:
                        return {'state': 'needs-attention', 'reason': 'pin-ignored-for-replacement',
                                'attempt_id': replacement, 'harness': replacement_meta.get('harness'),
                                'source_attempt_id': aid, 'node': source.get('route_node') or '__owner__',
                                'requested_harness': pinned, 'detail': 'replacement already launched; its execution identity is retained'}
                return {'state':'running','attempt_id':replacement,'record':record}
        replay = launch_input(jobs,aid,source)
        from dispatch_replacement_batch import command as batch_command
        command = (_owner_command(jobs, record, source, replay) if source.get('worker_type') == 'owner'
                   else batch_command(jobs, record, source, replay) or _command(jobs,record,source,replay))
        env = dict(os.environ)
        # The old launcher reservation/owner tuple is not a grant for its successor.
        for key in list(env):
            if key.startswith('AGENT_OWNER_ROUTE_') or key in {
                    DC.GOVERNOR_RESERVATION_ENV}:
                env.pop(key,None)
        if source.get('worker_type') == 'owner' and not source.get('route_file'):
            env.update(AGENT_OWNER_ROUTE_FILE=record['route_file'],
                       AGENT_OWNER_ROUTE_ID=record['route_id'],AGENT_OWNER_ROUTE_HASH=record['route_hash'])
        if source.get('worker_type') == 'owner':
            # The launch seam publishes the owner's producer binding only from the
            # route's own open cycle; the caller's environment is not that cycle.
            try:
                from artifact_producer import ProducerError, prepare_route_artifact_env
                env.pop('AGENT_ARTIFACT_PARENT_OUTPUT_DIR', None)  # SD-163: no stale source
                env.update(prepare_route_artifact_env(Path(record['route_file']), start=False,
                                                      jobs=Path(jobs)))
            except (ProducerError, OSError, ValueError):
                pass
        try:
            foreground, timeout = _launcher_budget(command, source)
            if foreground and run is subprocess.run:
                from dispatch_lifecycle import run_forwarding_termination
                completed = run_forwarding_termination(command, env=env, capture=True,
                                                      timeout=timeout, terminate_on_timeout=True)
            else:
                completed = run(command,env=env,text=True,capture_output=True,check=False,timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # A slow disk after a usage-limit reset can hold the launcher past its budget.
            # The unclaimed successor row stays; the next `start` relaunches that same attempt.
            output = ''.join(part.decode(errors='replace') if isinstance(part, bytes) else str(part or '')
                             for part in (exc.stdout, exc.stderr))
            return {'state':'needs-attention','reason':'replacement-launch-timeout',
                    'attempt_id':replacement,'record':record,
                    'launcher_diagnostic':'\n'.join(output.splitlines()[-20:]),
                    'source_attempt_id':aid,'node':source.get('route_node') or '__owner__'}
        current = _rows(Path(jobs).read_text().splitlines()).get(replacement)
        if getattr(completed, 'received_signal', None) or getattr(completed, 'cleanup_incomplete', False):
            return {'state':'needs-attention','reason':'replacement-launch-timeout' if getattr(completed, 'timed_out', False)
                    else 'replacement-launch-interrupted', 'attempt_id':replacement,'record':record,
                    'cleanup_incomplete':bool(getattr(completed, 'cleanup_incomplete', False)),
                    'source_attempt_id':aid,'node':source.get('route_node') or '__owner__'}
        if current and current[1].get('launch_claimed') == '1':
            return {'state':'running','attempt_id':replacement,'record':record}
        output = '\n'.join(str(getattr(completed, name, '') or '') for name in ('stdout', 'stderr'))
        diagnostic = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if source.get('worker_type') == 'owner' and diagnostic.get('reason') in {
                'no-eligible-candidate', 'no-eligible-route-evidence-candidate'}:
            # Use the ordinary selector's candidates. Only real limits on all of
            # them mean a pause; auth/unknown/policy failures keep their diagnostic.
            from dispatch_capacity_evidence import harness_hold
            candidates = [h for h in diagnostic.get('configured_candidates', '').split(',') if h]
            holds = [(h, harness_hold(jobs, h)) for h in candidates]
            if holds and all(hold for _, hold in holds):
                harness, hold = min(holds, key=lambda item: item[1].get('until_epoch') or float('inf'))
                return _capacity_wait(jobs, aid, {**source, 'harness': harness}, hold)
        reason = ('pin-ignored-for-replacement' if diagnostic.get('reason') == 'pin-ignored-for-replacement'
                  else 'replacement-launch-pending')
        return {'state':'needs-attention','reason':reason,
                'attempt_id':replacement,'record':record,'launcher_exit':completed.returncode,
                'launcher_diagnostic':'\n'.join(output.splitlines()[-20:]),
                'source_attempt_id':aid,'node':source.get('route_node') or '__owner__'}
    except (DC.DispatchContractError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        reason = getattr(exc,'reason','replacement-observation-unavailable')
        if reason == 'automatic-replacement-exhausted':
            family_record = None
            binding = source_binding(jobs, source)
            family = binding[0] if binding else source.get('replacement_family_id')
            if family:
                # The exact existing claim owns the allowance. A corrected FAIL
                # may belong to an ordinary family, so do not reconstruct it as a pause.
                digest = binding[2] if binding else source.get('replacement_claim_digest')
                family_record = _check_record(_read(_record_path(jobs, family)), family,
                                             {'replacement_claim_digest': digest or None})
            elif not _budget_exhausted(jobs, source, capacity=kind in PAUSE_KINDS):
                _, route = _route(jobs, aid, source)
                logical = _logical_key(route, source)
                if kind in PAUSE_KINDS:
                    logical = {**logical, 'after_capacity': aid}
                family_record = _check_record(_read(_record_path(jobs,_digest(logical))),_digest(logical))
            return exhausted_attention(jobs, aid, source, family_record=family_record)
        result = {'state':'needs-attention','reason':reason,'source_attempt_id':aid,
                  'node': source.get('route_node') or '__owner__'}
        if (reason.startswith('replacement-') or reason == 'pin-ignored-for-replacement') and getattr(exc,'detail',reason) != reason:
            result['detail'] = str(exc.detail)[:240]
        elif not isinstance(exc, DC.DispatchContractError):
            result['detail'] = f'{type(exc).__name__}: {exc}'[:240]
        result.update(getattr(exc,'live',None) or {})
        return result


def effective_attempts(jobs, attempts):
    """Read a verified one-edge mapping; failed originals remain in the ledger."""
    rows = _rows(Path(jobs).read_text().splitlines())
    effective = set(attempts); mapping = []
    for aid in sorted(attempts):
        if aid not in rows:
            continue
        _, source = rows[aid]
        binding = source_binding(jobs, source)
        if not binding or not binding[0] or not binding[1]:
            continue
        family, replacement, claim_digest = binding
        record = _check_record(_read(_record_path(jobs,family)), family,
                               {'replacement_claim_digest': claim_digest or None})
        if (not record or record.get('original_attempt_id') != aid
                or record.get('replacement_attempt_id') != replacement
                or record.get('family_id') != family):
            raise DC.DispatchContractError('replacement-lineage-unproven',aid)
        if replacement not in rows:
            continue  # A claim is not a launch receipt.
        _, target = rows[replacement]
        if (target.get('replacement_claim_digest') != _digest(record)
                or target.get('replacement_family_id') != family or target.get('automatic_retry_of') != aid
                or target.get('replacement_original_attempt_id') != aid
                or not route_authority.replacement_parent_matches(source, target, jobs, lineage=True)
                or any(target.get(k) != source.get(k) for k in ('worker_type','dispatch_depth'))):
            raise DC.DispatchContractError('replacement-lineage-unproven',replacement)
        effective.discard(aid); effective.add(replacement)
        mapping.append({'original_attempt_id':aid,'replacement_attempt_id':replacement,
                        'family_id':family,'claim_digest':_digest(record)})
    return effective,mapping


def advance_batch(jobs, attempts, *, authority_check=None, run=subprocess.run):
    """Runtime-only checkpoint; no model turn or new polling service."""
    rows = _rows(Path(jobs).read_text().splitlines())
    attention=[]
    for aid in sorted(attempts):
        pair=rows.get(aid)
        if pair is None:
            continue
        if pair[0][1] in {'open','running'}:
            if pair[1].get('replacement_original_attempt_id') and pair[1].get('launch_claimed') != '1':
                step=advance(jobs,aid,authority_check=authority_check,run=run)
                if step.get('state')=='needs-attention': attention.append(step)
            continue
        meta=pair[1]
        if meta.get('replacement_original_attempt_id'):
            if not DC.verdict_pass(meta):
                if death_kind(pair[0], meta, jobs=jobs) in {'capacity', CORRECTED}:
                    step=advance(jobs,aid,authority_check=authority_check,run=run)
                    if step.get('state')=='needs-attention': attention.append(step)
                else:
                    attention.append(exhausted_attention(jobs, aid, meta))
            continue
        step=advance(jobs,aid,authority_check=authority_check,run=run)
        if step.get('state')=='needs-attention':
            attention.append(step)
    effective,mapping=effective_attempts(jobs,attempts)
    return effective,mapping,attention


def adopt_receipt(jobs, original_attempts, receipt):
    """A supervisor accepts only the registry-bound mapping, never a free ID."""
    mapping=receipt.get('replacement_lineage') or []
    if not mapping:
        return set(original_attempts),set()
    effective,expected=effective_attempts(jobs,set(original_attempts))
    if mapping!=expected:
        raise DC.DispatchContractError('replacement-receipt-lineage-mismatch')
    children={child.get('attempt_id') for child in receipt.get('children',[])}
    if children!=effective and not (receipt.get('state') == 'watch-expired' and not children):
        raise DC.DispatchContractError('replacement-receipt-attempt-mismatch')
    return effective,{row['original_attempt_id'] for row in mapping}


def exhausted_attention(jobs, aid, meta, *, family_record=None):
    result={'source_attempt_id':aid,'state':'needs-attention',
            'reason':'automatic-replacement-exhausted',
            'node':meta.get('route_node') or '__owner__',
            'failure':meta.get('note') or meta.get('failure_class') or 'terminal-failure'}
    family=meta.get('replacement_family_id') or (family_record or {}).get('family_id')
    if family:
        record=_check_record(_read(_record_path(jobs,family)),family)
        if (record['logical_node']['node'] != (meta.get('route_node') or '__owner__')
                and meta.get('worker_type') != 'owner'):
            raise DC.DispatchContractError('replacement-lineage-unproven')
        if record['replacement_attempt_id']==aid:
            _once(_directory(jobs)/'attention'/(family+'.json'), result)
        proof={'family_id':family,'claim_digest':_digest(record)}
    elif _budget_exhausted(jobs,meta):
        proof={'legacy_budget_exhausted':True}
    else:
        raise DC.DispatchContractError('replacement-exhaustion-unproven')
    _once(_directory(jobs)/'attention/by-source'/(_id(aid)+'.json'),
          {'attention':result,'proof':proof})
    return result


def validate_attention(jobs, attention, *, allowed_attempts=None):
    """Validate diagnostic association; this never grants execution authority."""
    if not isinstance(attention, list):
        raise DC.DispatchContractError('replacement-attention-invalid')
    rows = _rows(Path(jobs).read_text().splitlines())
    for item in attention:
        if not isinstance(item, dict):
            raise DC.DispatchContractError('replacement-attention-invalid')
        aid = item.get('source_attempt_id')
        if aid not in rows:
            raise DC.DispatchContractError('replacement-attention-source-missing')
        if allowed_attempts is not None and aid not in allowed_attempts:
            raise DC.DispatchContractError('replacement-attention-scope-mismatch')
        fields, meta = rows[aid]
        reason = item.get('reason')
        if (fields[1] not in {'done','cancelled','killed'} or DC.verdict_pass(meta)
                or item.get('state') != 'needs-attention'
                or item.get('node') != (meta.get('route_node') or '__owner__')
                or not isinstance(reason, str) or not re.fullmatch(r'[a-z0-9][a-z0-9:-]{0,159}', reason)):
            raise DC.DispatchContractError('replacement-attention-invalid')
        if reason == 'automatic-replacement-exhausted':
            saved = _read(_directory(jobs)/'attention/by-source'/(_id(aid)+'.json'))
            if not saved or saved.get('attention') != item:
                raise DC.DispatchContractError('replacement-attention-drift')
            proof = saved.get('proof') or {}
            family = proof.get('family_id')
            if family:
                record = _check_record(_read(_record_path(jobs,family)),family)
                if proof.get('claim_digest') != _digest(record):
                    raise DC.DispatchContractError('replacement-attention-lineage-invalid')
            elif not proof.get('legacy_budget_exhausted') or not _budget_exhausted(jobs,meta):
                raise DC.DispatchContractError('replacement-attention-lineage-invalid')
    return [{key: (str(item[key])[:240] if key == 'failure' else item[key])
             for key in ('source_attempt_id','state','reason','node','failure') if key in item}
            for item in attention]


def recovery_instructions(args):
    """Fresh rendering adds resume context without modifying the sealed raw task."""
    brief = os.environ.pop('AGENT_DISPATCH_RETRY_BRIEF', '')
    prior = getattr(args, 'automatic_retry_of', None)
    record = None
    if prior:
        jobs = Path(args.jobs_path)
        index = source_reservation(jobs, prior)
        if index:
            record = _check_record(_read(_record_path(jobs, index['family_id'])),index['family_id'])
            if record['replacement_attempt_id'] != args.attempt_id:
                raise DC.DispatchContractError('replacement-instructions-binding-mismatch')
            # The caller's environment is transient; replay guidance from the
            # original digest-checked input, just like its raw task.
            source = _rows(jobs.read_text().splitlines())[prior][1]
            brief = launch_input(jobs, prior, source).get('retry_brief', brief)
    args.replacement_retry_brief = brief
    retry_context = ('\n\n## Partial-group retry brief\n' + brief + '\n') if brief else ''
    if not record or getattr(args, 'worker_type', '') != 'owner':
        return retry_context
    completed = ', '.join(str(row['node']) for row in record['reuse']['completed']) or '(none)'
    kind = (record.get('proof') or {}).get('death_kind')
    fix = kind == CORRECTED and (record.get('proof') or {}).get('source_result') == 'FAIL'
    if fix:
        opening = (f'The previous owner {prior} ended FAIL and a person approved a fix for it; this continues '
                   f'the same work on the existing route {record["route_id"]}.\n')
    elif kind == CORRECTED:
        ended = ('exited before settlement and received a correction'
                 if record['proof'].get('source_result') == 'EXITED'
                 else 'ended BLOCKED and a person has answered it')
        opening = (f'The previous owner {prior} {ended}; this continues '
                   f'the same work on the existing route {record["route_id"]}.\n')
    elif kind == 'capacity':
        opening = f'The previous attempt {prior} stopped at a usage limit; this resumes it on the existing route {record["route_id"]}.\n'
    elif kind == 'unlaunched':
        opening = f'The previous attempt {prior} never started (its launcher stopped before spawning); this starts the same work on the existing route {record["route_id"]}.\n'
    else:
        opening = f'You replace exact-dead attempt {prior} once, on the existing route {record["route_id"]}.\n'
    check_fix = fix and bool(record['proof'].get('answers'))
    rerun = ('Rerun the stage that makes the approved fix and every check after it; reuse everything '
             'else. ' if check_fix else
             'Continue only unfinished work. Do not rerun completed nodes, successful siblings, or completed prefixes. ')
    text = ('\n\n## Verified recovery context\n'
            + opening +
            f'Reuse the existing cycle {record["reuse"]["cycle_id"]} and completion evidence for: {completed}.\n'
            + rerun +
            'Keep existing human answers and gate releases; do not ask the same scope again. '
            'Preserve the original failure and report any second failure as needs-attention.\n')
    if check_fix:
        text += ('The failed checks this fix answers ('
                 + ', '.join(record['proof']['answers']) +
                 ') each get one more verdict round (closure-check) once the fix is in; there is no '
                 'further round after it.\n')
    # A replacement that never started showed its answers and gate to no model,
    # so the attempt that takes its place carries them as they were.
    carried = _unstarted_replacement_claim(jobs, prior) if kind == 'unlaunched' else None
    carried_proof = (carried or {}).get('proof') or {}
    if not (carried_proof.get('parked_gate') or carried_proof.get('death_kind') == CORRECTED):
        carried = None
    if carried is not None:
        text += (f'{prior} was itself the continuation of {carried["original_attempt_id"]}; '
                 'what that continuation was given follows.\n')
    for claim, answered in ((record, prior), (carried, carried and carried['original_attempt_id'])):
        proof = (claim or {}).get('proof') or {}
        if proof.get('parked_gate'):
            text += _gate_context(jobs, claim)
        if proof.get('death_kind') == CORRECTED:
            if claim is carried:
                text += (f'The owner {answered} ended BLOCKED and a person has answered it; '
                         'that answer is for this work.\n')
            text += _correction_context(jobs, answered, proof)
    return text + CONTINUATION_WAIT_NOTE + retry_context


def _gate_context(jobs, record):
    gate = record['proof']['parked_gate']
    from parent_next_directive import entrypoint
    read = shlex.join([sys.executable, entrypoint(ROOT, 'utilities/workflow-supervisor.py'), 'await-release',
                       '--route', record['route_file'], '--gate', gate, '--jobs', str(jobs),
                       '--max', '0', '--answers-out']) + ' <file>'
    return (f'The original owner stopped at human gate {gate} (raise epoch {record["proof"].get("gate_epoch")}); '
            'a person released it with proceed. '
            f'Read the recorded answers with: {read} '
            f'Do not raise {gate} again. Continue from the node it gated through the remaining declared stages.\n')


def _unstarted_replacement_claim(jobs, aid):
    """The claim that created replacement ``aid``, when ``aid`` closed before it spawned;
    through a run of such replacements, the first claim that is not itself `unlaunched`."""
    try:
        rows = _rows(Path(jobs).read_text().splitlines())
    except (OSError, DC.DispatchContractError):
        return None
    for _ in range(8):
        meta = (rows.get(aid) or ((), {}))[1]
        family = meta.get('replacement_family_id', '')
        if (not meta.get('replacement_original_attempt_id') or meta.get('pid')
                or meta.get('launch_outcome') != 'never-launched' or meta.get('launch_claimed') != '0'
                or not re.fullmatch(r'[0-9a-f]{64}', family)):
            return None
        claim = _check_record(_read(_record_path(jobs, family)), family)
        if claim['replacement_attempt_id'] != aid:
            return None
        if (claim.get('proof') or {}).get('death_kind') != 'unlaunched':
            return claim
        aid = claim['original_attempt_id']
    return None


def _correction_context(jobs, prior, proof):
    """The pinned answers, exactly as sent, and what they answer."""
    from dispatch_owner_input import OwnerInput
    kept = {item['id']: item for item in _retained_corrections(jobs, prior)}
    items = []
    for pinned in proof.get('corrections') or []:
        item = kept.get(pinned.get('id'))
        if item is None or item['digest'] != pinned.get('digest'):
            raise DC.DispatchContractError('replacement-correction-drift', str(pinned.get('id')))
        items.append(item)
    handoff = proof.get('handoff')
    if proof.get('source_result') == 'EXITED':
        return ('The previous owner exited before settlement. The correction below names the remaining work; '
                'preserve completed checks and continue the existing route.' + OwnerInput.text(items) + '\n')
    answer = ('The answer below is the fix a person approved for the failure it reported. Treat it as '
              'given: do not ask for it again; apply it, then continue through the remaining declared stages.'
              if proof.get('source_result') == 'FAIL' else
              'The answer below is the reply to what it was waiting for (for example an approval it asked '
              'for). Treat it as given: do not ask for it again, and continue from where the previous '
              'owner stopped through the remaining declared stages.')
    return ((f'The previous owner reported why it stopped in {handoff}. ' if handoff else '')
            + answer + OwnerInput.text(items) + '\n')
