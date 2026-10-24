"""SD-157: one proof-bound successor per proven-dead execution.

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
from dispatch_registry_cache import registry_view
import route_authority

ROOT = Path(__file__).resolve().parents[1]
DEATH_NOTES = frozenset({
    'dead-exact-pid', 'dead-namespace-absent', 'dead-worker-silent-exit',
    'dead-missing-result', 'dead-parent-orphaned', 'dead-governor-reservation-transfer',
    'dead-no-progress', 'dead-timeout',
})
# An owner whose runtime died under it: the process exited (`dead-runtime-exit`) or the runtime
# returned an error envelope such as a provider 5xx (`dead-runtime-error`). Only an explicit `start`
# replaces each settled failed attempt through the same checked path.
RUNTIME_DEATH_NOTES = frozenset({'dead-runtime-exit', 'dead-runtime-error'})
SCHEMA = 'automatic-dead-replacement-v1'
# D2: a frame leg whose `top` the frame rule assigned is replaced once, one profile lower, when it
# stops at a usage limit. The claim carries exactly this value; nothing else may.
FRAME_CAPACITY = 'frame-capacity'
# Stops whose existing explicit-resume behavior is retained: a usage limit, an owner its launcher
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
    def canonical(item, path=()):
        if isinstance(item, dict):
            preserve = any(path[index:index + 2] in {
                               ("applied_permissions", "opencode_permission"),
                               ("applied_permissions", "opencode_selected_agent_permission"),
                           }
                           for index in range(max(0, len(path) - 1)))
            keys = list(item) if preserve else sorted(item)
            return {key: canonical(item[key], (*path, key)) for key in keys}
        if isinstance(item, list):
            return [canonical(value, path) for value in item]
        return item

    return (json.dumps(canonical(value), sort_keys=False, ensure_ascii=False,
                       separators=(',', ':'))+'\n').encode()


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'att-[A-Za-z0-9._-]{1,240}', value):
        raise DC.DispatchContractError('replacement-attempt-invalid')
    return value


def never_started(status, metadata):
    """A terminal row whose launch fence proves no payload ever began.

    Some standard owners retain the launcher/carrier PID after the payload was
    refused. Keep that identity usable only when its existing exact teardown
    receipt proves the process group and attempt descendants were cleaned up.
    The launch and process guards still reobserve liveness before replacement.
    """
    return (status == 'done' and metadata.get('launch_outcome') == 'never-launched'
            and metadata.get('launch_started') != '1'
            and (not metadata.get('pid') or DC._settled_cleanup_receipt(metadata)))


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


_ROWS_MEMO = [None, None]


def _rows(lines):
    """Registry rows by attempt id. Callers re-read the same registry many times in one
    process, so the last parse is reused for identical content; each call gets copies."""
    lines = tuple(lines)
    key, cached = _ROWS_MEMO
    if cached is None or key != lines:
        cached = _parse_rows(lines)
        _ROWS_MEMO[:] = [lines, cached]
    return {aid: (list(fields), dict(meta)) for aid, (fields, meta) in cached.items()}


def _parse_rows(lines):
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
        import opencode_runtime
        result['opencode_permission'] = opencode_runtime.opencode_permission_from_config(config)
        selected_agent = getattr(args, 'agent', None)
        if selected_agent:
            selected_permission = opencode_runtime.opencode_agent_permission_from_config(
                config, selected_agent)
            result['opencode_selected_agent_permission'] = {
                'agent': selected_agent,
                'permission': opencode_runtime.opencode_permission_ordered_view(
                    selected_permission),
                # Keep the native ordered mapping as well: recovery address
                # checks must compose global rules followed by this override.
                'local_policy': selected_permission,
            }
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
        detail = route_authority.permission_mismatch_detail(previous, stored)
        reservation = source_reservation(jobs, prior) if prior else None
        record = _read(_record_path(jobs, reservation['family_id'])) if reservation else None
        if not record or record.get('replacement_attempt_id') != aid:
            if detail:
                raise DC.DispatchContractError('replacement-input-tuple-mismatch', detail)
            return False
        _, source, replay = validate_claim_source(jobs, lines, record)
        _, current_route = _route(jobs, prior, source)
        same_work = route_authority.same_sealed_work(
            previous, stored, recovery=(record, source, replay, current_route))
        if not same_work:
            if detail:
                raise DC.DispatchContractError('replacement-input-tuple-mismatch', detail)
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


def terminal_observation(fields, meta):
    from codex_dispatch_terminal import inspect_terminal_attempt
    return inspect_terminal_attempt(meta.get('log_file'), worktree=fields[3],
                                    artifact_root_metadata=meta.get('artifact_root'),
                                    artifact_binding=meta)


def _terminal_absent(fields, meta, *, capacity=False, runtime=False):
    return route_authority._replacement_terminal_absent(fields, meta, capacity=capacity, runtime=runtime)


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


def _frame_pair_rotation_verified(jobs, meta):
    """Verify the exact launch input and sibling behind a rotated alternative."""
    if (meta.get('worker_type') != 'frame' or meta.get('dispatch_depth') != '1'
            or meta.get('route_node') != 'frame-alternative'
            or meta.get('model_diversity') != 'rotated'
            or not meta.get('route_file') or not meta.get('attempt_id')
            or not meta.get('harness') or not meta.get('model_profile')):
        return False
    try:
        route = _read(Path(meta['route_file']))
        if not route or route.get('route_id') != meta.get('route_id') or route.get('route_hash') != meta.get('route_hash'):
            return False
        node = next((n for n in route.get('nodes', [])
                     if isinstance(n, dict) and n.get('id') == 'frame-alternative'), None)
        if not node or node.get('worker_type') != 'frame':
            return False
        pins = route_authority.selection_pin_rows(route)
        frame_pin = pins.get('frame') or {}
        if frame_pin.get('model'):
            return False
        source = launch_input(jobs, meta['attempt_id'], meta)
        argv = source.get('argv')
        if not isinstance(argv, list):
            return False
        hint = None
        for index, value in enumerate(argv[:-1]):
            if value == '--pair-model-profile':
                hint = argv[index + 1]
                break
            if value.startswith('--pair-model-profile='):
                hint = value.split('=', 1)[1]
                break
        if not hint:
            return False
        siblings = []
        for fields, row in _rows(Path(jobs).read_text().splitlines()).values():
            if (row.get('route_id') == meta.get('route_id')
                    and row.get('route_hash') == meta.get('route_hash')
                    and row.get('route_node') == 'frame'
                    and row.get('worker_type') == 'frame'
                    and row.get('harness') == meta.get('harness') and row.get('model')):
                siblings.append(row)
        if len(siblings) != 1:
            return False
        from model_config import resolve_config
        from model_profile import pair_model_rotation
        config, _ = resolve_config(meta['harness'], source_root=ROOT)
        candidate = pair_model_rotation(
            meta['harness'], config, str(node.get('model_profile')), str(siblings[0]['model']))
        return bool(candidate and candidate['profile'] == hint == meta.get('model_profile'))
    except (DispatchContractError, OSError, ValueError, KeyError, TypeError):
        return False


def death_kind(fields, meta, *, jobs=None, lines=None):
    return route_authority._replacement_kind(fields[1], meta, jobs=jobs, lines=lines)


def death_proof(fields, meta, *, jobs=None, lines=None):
    """No broad dead-* permission; a semantic result still owns settlement."""
    kind = death_kind(fields, meta, jobs=jobs, lines=lines)
    from route_parent_close import owner_continuation_processes
    proof, preserved = owner_continuation_processes(meta, jobs)
    decision = route_authority.decide_continuation(action="replacement", jobs=jobs,
        status=fields[1], metadata=meta,
        facts={"process": proof, "death_kind": kind, "fields": fields, "terminal_conflict": DC.terminal_conflict_pending(meta)})
    if not decision.allowed and decision.reason.startswith("replacement-process-"):
        raise _process_error(proof, meta)
    route_authority.require_continuation(decision)
    result = {'state': proof.state, 'reason': proof.reason, 'death_kind': kind,
              'note': meta.get('note', ''), 'cleanup_receipt_digest': meta.get('cleanup_receipt_digest', ''),
              'cancellation_receipt_digest': meta.get('cancellation_receipt_digest', '')}
    if preserved:
        result['preserved_resources'] = preserved
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
        elif route_authority.owner_result_format_only_end(fields[1], meta):
            result['source_result'] = 'CONTRACT'
    elif kind == 'unlaunched' and jobs is not None:
        carried = _unstarted_replacement_claim(jobs, meta.get('attempt_id'))
        carried_proof = (carried or {}).get('proof') or {}
        if carried and (carried_proof.get('death_kind') == CORRECTED or carried_proof.get('parked_gate')):
            source_lines = Path(jobs).read_text().splitlines() if lines is None else lines
            validate_claim_source(jobs, source_lines, carried)
        if carried and carried_proof.get('death_kind') == CORRECTED:
            source_attempt = carried['original_attempt_id']
            _correction_context(jobs, source_attempt, carried_proof)
            result['carried_continuation'] = {
                'attempt_id': source_attempt,
                'family_id': carried['family_id'],
                'claim_digest': _digest(carried),
                'corrections': [
                    {'id': item['id'], 'digest': item['digest']}
                    for item in _retained_corrections(jobs, source_attempt)
                ],
            }
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
                                          worker_type='owner', artifact_binding=meta)
        encoded = result.get('artifact_path_b64') if result.get('artifact_state') == 'readable' else None
        if not encoded:
            return None
        return base64.urlsafe_b64decode(str(encoded) + '=' * (-len(str(encoded)) % 4)).decode()
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def _process_error(proof, meta, attempt_id=None):
    """`replacement-process-<state>`; a live one says what is still alive."""
    error = DC.DispatchContractError('replacement-process-'+proof.state, proof.reason)
    error.next_action = 'same-command'
    error.retry_when = ('The exact owner and unregistered children are observed quiescent; '
                        'registered payloads remain bound and running.')
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
    unsettled = None
    for aid in _owned_children(rows, owner):
        fields, meta = rows[aid]
        proof = DC.attempt_process_quiescence(meta, terminal_receipt=fields[1] in {'done','cancelled','killed'})
        if fields[1] in {'open','running'} or proof.state != 'quiescent':
            unsettled = aid
            break
    route_authority.require_continuation(route_authority.decide_continuation(action='replacement-budget',
        status='done', facts={'death_kind': 'claimed', 'child_unsettled': unsettled}))


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


def _source_scope_matches(path, scopes):
    import fnmatch
    from artifact_context import classify_scope
    path = path.replace('\\', '/')
    if any(part in {'.agent_reports', '.claude_reports', '.codex_reports',
                    '.opencode_reports', 'artifacts'} for part in path.split('/')):
        return False
    in_tests = path.startswith('tests/')
    for scope in scopes or ():
        if classify_scope(scope) != 'source':
            continue
        if scope.startswith('tests/'):
            if in_tests and fnmatch.fnmatchcase(path, scope):
                return True
        elif not in_tests:
            # `source/**` and `source-alternative/**` denote source files in
            # the route worktree; they are scope vocabulary, not disk paths.
            return True
    return False


def _git_readonly(worktree, *args):
    import artifact_context
    try:
        return subprocess.run(
            ['git', '-C', str(worktree), *args], capture_output=True,
            env=artifact_context._readonly_git_env(), timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _stage_progress_block(jobs, aid, fields, meta, lines):
    """Bounded factual observations for a failed stage attempt; never a completion claim."""
    if (meta.get('worker_type') != 'stage'
            or fields[1] not in {'done', 'cancelled', 'killed'}
            or meta.get('note') not in {'dead-timeout', 'dead-no-progress'}):
        return ''
    observations = [f'- prior attempt: {aid}']
    for key in ('note', 'failure_class', 'process_exit', 'exit_code', 'status'):
        if meta.get(key) is not None:
            observations.append(f'- recorded {key}: {str(meta[key])[:240]}')
    start = None
    try:
        start = _instant(fields[0])
    except (ValueError, TypeError, IndexError):
        pass
    end = None
    for key in ('terminal_event_at', 'terminal_receipt_at', 'ended_at', 'completed_at'):
        if meta.get(key):
            try:
                end = _instant(meta[key])
                break
            except (ValueError, TypeError):
                continue
    observed = datetime.now(timezone.utc)
    if end:
        observations.append(f'- recorded terminal time: {end.astimezone(timezone.utc).isoformat()}')
    else:
        observations.append(f'- end unavailable; observed at {observed.isoformat()}')
    if start:
        upper = end or observed
        elapsed = max(0.0, (upper - start).total_seconds())
        label = 'elapsed to recorded terminal time' if end else 'interval since recorded start'
        observations.append(f'- {label}: {elapsed:.1f} seconds')
    else:
        observations.append('- elapsed unavailable: recorded start time is invalid or absent')

    route = None
    route_path = meta.get('route_file')
    try:
        if route_path:
            route = _read(Path(route_path))
        if not isinstance(route, dict):
            raise ValueError('route unavailable')
        DC._route_module().verify_route(route)
    except Exception:
        route = None
    node = next((item for item in (route or {}).get('nodes', [])
                 if item.get('id') == meta.get('route_node')), None)
    worktree = (route or {}).get('cwd')
    scopes = (node or {}).get('write_scope') or []
    unversioned = bool(route and route.get('source_commit') == 'unversioned')
    head = None
    if unversioned:
        observations.append('- git 아닌 폴더 — 커밋·미커밋 관찰 해당 없음')
    elif worktree:
        result = _git_readonly(worktree, 'rev-parse', 'HEAD')
        if result is not None and result.returncode == 0:
            head = result.stdout.decode('ascii', 'replace').strip()
            observations.append(f'- current worktree HEAD: {head or "(empty response)"}')
        else:
            observations.append('- current worktree HEAD unavailable (Git observation failed)')
    else:
        observations.append('- worktree unavailable from the sealed route')

    launch_head = meta.get('launch_head')
    if unversioned:
        observations.append('- commit range: 해당 없음 (unversioned route)')
    elif not launch_head:
        observations.append('- launch_head unavailable; commit range not observed')
    elif not worktree or not head:
        observations.append(f'- launch_head recorded ({launch_head}); commit range unavailable')
    else:
        result = _git_readonly(worktree, 'log', '--format=%H%x00%s%x1e',
                               f'{launch_head}..HEAD')
        if result is None or result.returncode != 0:
            observations.append(f'- commit range {launch_head}..HEAD unavailable (Git observation failed)')
        else:
            commits = []
            for record in result.stdout.split(b'\x1e'):
                fields = record.strip(b'\r\n').split(b'\0')
                if len(fields) != 2:
                    continue
                commit, subject = fields
                if not re.fullmatch(rb'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', commit):
                    continue
                commits.append((commit.decode('ascii'),
                                subject.decode('utf-8', 'replace').replace('\n', ' ')[:160]))
            observations.append(f'- commits after launch_head: {len(commits)} observed')
            for commit, subject in commits[:20]:
                observations.append(f'  - {commit} {subject}')
            if len(commits) > 20:
                observations.append(f'  - truncated; {len(commits) - 20} more commit(s)')

    if worktree and not unversioned:
        result = _git_readonly(worktree, 'status', '--porcelain=v1', '-z',
                               '--untracked-files=all')
        if result is None or result.returncode != 0:
            observations.append('- scoped uncommitted paths unavailable (Git status failed)')
        else:
            records = [item for item in result.stdout.split(b'\0') if item]
            paths = []
            index = 0
            while index < len(records):
                entry = records[index]
                index += 1
                status = entry[:2].decode('ascii', 'replace')
                path = entry[3:].decode('utf-8', 'replace')
                candidates = [path]
                if 'R' in status or 'C' in status:
                    if index < len(records):
                        candidates.append(records[index].decode('utf-8', 'replace'))
                        index += 1
                for candidate in candidates:
                    if _source_scope_matches(candidate, scopes):
                        paths.append((status, candidate))
            paths.sort(key=lambda item: item[1])
            observations.append(f'- scoped uncommitted paths: {len(paths)} observed')
            for status, path in paths[:40]:
                observations.append(f'  - {status} {path}')
            if len(paths) > 40:
                observations.append(f'  - truncated; {len(paths) - 40} more path(s)')
    elif unversioned:
        observations.append('- scoped uncommitted paths: 해당 없음 (unversioned route)')

    try:
        import artifact_context
        context = artifact_context.resolve_context(
            environ={}, route=route, node_id=meta.get('route_node'),
            explicit={'artifact_root': (route or {}).get('artifact_root')},
        ) if route else None
        output = context.output_dir if context else None
        if output is None:
            observations.append('- cycle outputs unavailable (no bound canonical cycle output)')
        elif output.is_symlink():
            observations.append('- cycle outputs unavailable (canonical output path is a symlink)')
        elif not start:
            observations.append('- cycle outputs unavailable (attempt start time unavailable)')
        else:
            declarations = [value for value in [*(node or {}).get('outputs', ()), *scopes]
                            if artifact_context.classify_scope(value) == 'artifact'
                            and not artifact_context.is_semantic_output(value)]
            changed = []
            seen = 0
            truncated_scan = False
            for base, dirs, files in os.walk(output, followlinks=False):
                dirs[:] = sorted(name for name in dirs
                                 if not (Path(base) / name).is_symlink())
                for name in sorted(files):
                    path = Path(base) / name
                    seen += 1
                    if seen > 5000:
                        truncated_scan = True
                        break
                    if path.is_symlink():
                        continue
                    relative = path.relative_to(output).as_posix()
                    if not artifact_context.scope_matches(relative, declarations):
                        continue
                    try:
                        mtime = datetime.fromtimestamp(path.stat(follow_symlinks=False).st_mtime,
                                                       timezone.utc)
                    except OSError:
                        continue
                    if start <= mtime <= (end or observed):
                        changed.append((mtime.isoformat(), relative))
                if truncated_scan:
                    break
            changed.sort(key=lambda item: (item[0], item[1]))
            observations.append(f'- scoped cycle output paths changed in observed interval: {len(changed)}')
            for timestamp, relative in changed[:40]:
                observations.append(f'  - {timestamp} {relative}')
            if len(changed) > 40:
                observations.append(f'  - truncated; {len(changed) - 40} more path(s)')
            if truncated_scan:
                observations.append('- cycle scan incomplete after 5000 files')
            if not end:
                observations.append('- mtime upper bound is observation time; mtime is not authorship proof and cannot show deleted or timestamp-preserved changes')
    except Exception as exc:
        observations.append(f'- cycle output observation unavailable: {type(exc).__name__}')
    observations.extend([
        '- These are observations only; commits are not a test/pass/completion or authorship claim.',
        '- Empty observations describe only the current readable state; unavailable observations are stated separately.',
    ])
    return '\n\n## Observed prior-attempt progress\n' + '\n'.join(observations) + '\n'


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
                  or attention is not None or bool(include_family and meta.get('automatic_retry_of')
                                                    and not meta.get('replacement_family_id')))
        family_hint = (reservation or {}).get('family_id')
        if (family_hint and not include_family and other != aid
                and _read(_record_path(jobs, family_hint)) is None):
            index_error = DC.DispatchContractError('replacement-claim-pending')
        if family_hint and _is_capacity_record(_read(_record_path(jobs, family_hint))):
            if include_family and other == aid:
                return True  # SD106 must not add a second successor to a resumed source.
            family_hint = None
        # SD157's own reservation is a replay, not a second consumption.
        # Its own SD106 exhaustion, however, is always a veto.
        # Automatic transport succession has no cumulative family budget.
        # SD106 cancellation recovery cannot add a successor to a reserved source.
        family_consumes = bool(family_hint) and include_family
        if (legacy or family_consumes or index_error or other in reference_ids
                or meta.get('automatic_retry_of') == aid):
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
    if (source.get('retry_ordinal') == '1'
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
        lines = Path(jobs).read_text().splitlines() if Path(jobs).exists() else []
    return legacy_budget_exhausted(jobs, lines, source, route=route)


def _plain_retry_successor(rows, aid):
    """Does an ordinary same-node retry, not a replacement claim, already continue this attempt?"""
    return any((meta.get('automatic_retry_of') == aid or meta.get('prior_attempt_id') == aid)
               and not meta.get('replacement_family_id') for _, meta in rows.values())


def _no_competing_successor(rows, aid, replacement=None):
    competing = any(other != replacement and (candidate.get('automatic_retry_of') == aid
                    or candidate.get('prior_attempt_id') == aid) for other, (_, candidate) in rows.items())
    route_authority.require_continuation(route_authority.decide_continuation(action='replacement-budget',
        status='done', facts={'death_kind': 'claimed', 'competing_successor': competing}))


def claim(jobs: Path, aid: str, *, resume_capacity=False) -> dict:
    """Lock-held death, children and terminal-fence checks precede one claim."""
    aid = _id(aid)
    with _locked(jobs) as lines:
        rows = _rows(lines)
        if aid not in rows:
            raise DC.DispatchContractError('replacement-source-missing', aid)
        fields, meta = rows[aid]
        DC.validate_attempt_metadata(meta)
        binding = source_binding(jobs, meta)
        if not binding and not resume_capacity and meta.get('worker_type') == 'owner':
            carried = _unstarted_replacement_claim(jobs, aid)
            if carried and _identity_refusal_reentry(
                    jobs, lines, carried['original_attempt_id'], carried):
                raise DC.DispatchContractError('replacement-duty-settled')
        if binding:
            old_family, _, claim_digest = binding
            record = _check_record(_read(_record_path(jobs, old_family)), old_family,
                                   {'replacement_claim_digest': claim_digest or None})
            if not record or aid not in {record['original_attempt_id'], record['replacement_attempt_id']}:
                raise DC.DispatchContractError('replacement-lineage-unproven')
            if aid != record['original_attempt_id']:
                raise DC.DispatchContractError('replacement-lineage-unproven')
            _no_competing_successor(rows, aid, record['replacement_attempt_id'])
            return record
        proof = death_proof(fields, meta, jobs=jobs, lines=lines)
        capacity = proof.get('death_kind') in PAUSE_KINDS
        path, route = _route(jobs, aid, meta)
        if proof.get('death_kind') == CORRECTED and route_authority.answerable_owner_end(fields[1], meta) == 'FAIL':
            # The approved fix answers each failed check's last FAIL: one closure-check round each,
            # within the verdict ceiling cap + 1. A check that already used it gets no new round.
            proof['answers'], proof['fallback_nodes'] = route_authority.fix_answers(route, lines, jobs)
            proof['source_result'] = 'FAIL'
        logical = _logical_key(route, meta)
        if capacity:
            # Every usage-limit stop opens its own family without changing old pause keys.
            logical = {**logical, 'after_capacity': aid}
        family = _digest(logical)
        old = _read(_record_path(jobs, family))
        if old and old.get('original_attempt_id') != aid:
            # Retain the old logical-node claim and give the next exact
            # predecessor its own edge. The by-source reservation arbitrates
            # repeated calls and crash recovery without a family count cutoff.
            _check_record(old, family)
            logical = {**logical, 'after_death': aid}
            family = _digest(logical)
            old = _read(_record_path(jobs, family))
        if old:
            _check_record(old, family)
            route_authority.require_continuation(route_authority.decide_continuation(action='replacement-budget',
                facts={'budget_exhausted': old.get('original_attempt_id') != aid}))
            _no_competing_successor(rows, aid, old['replacement_attempt_id'])
            _reserve_source(jobs, aid, family)
            _bind_source(jobs, lines, aid, old)
            return old
        route_authority.require_continuation(route_authority.decide_continuation(route,
            action="replacement", status=fields[1], metadata=meta,
            facts={"death_kind": proof['death_kind'],
                   "budget_exhausted": _budget_exhausted(jobs, meta, lines=lines, route=route, capacity=capacity)}))
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


def accept_terminal_delivery_duty(jobs, aid):
    """Publish an eligible automatic recovery claim before its notice is materialized.

    The caller runs after the terminal registry transaction releases its lock.
    Capacity pauses remain user resumed; a resultless death (including an
    answered correction) gets the same exact-edge claim the existing controller
    will later launch or settle.
    """
    jobs = Path(jobs)
    try:
        rows = _rows(jobs.read_text().splitlines())
    except (OSError, DC.DispatchContractError):
        return None
    row = rows.get(aid)
    if row is None or not row[1].get('delivery_intent'):
        return None
    try:
        kind = death_kind(row[0], row[1], jobs=jobs)
        if kind is None or kind in PAUSE_KINDS:
            return None
        return claim(jobs, aid)
    except DC.DispatchContractError:
        # Ineligible or unproven terminal rows retain their ordinary notice;
        # the controller owns any later recovery attempt.
        return None


def recovery_delivery_state(jobs, aid):
    """Return the existing exact-edge duty state used by terminal delivery.

    ``accepted`` is revocable and never an acknowledgement: the controller may
    still launch or re-enter the same stable claim. ``settled`` requires the
    existing replacement-attention record to name that exact claim. Missing or
    conflicting evidence stays ``unknown`` for the normal recovery path.
    """
    try:
        rows = _rows(Path(jobs).read_text().splitlines())
        index = source_reservation(jobs, aid)
        row = rows.get(aid)
        if row is None:
            return 'unknown' if index else 'none'
        _, meta = row
        binding = source_binding(jobs, meta)
        family = (index or {}).get('family_id') or (binding or ('',))[0]
        if not family:
            return 'none'
        record = _check_record(_read(_record_path(jobs, family)), family)
        if record.get('original_attempt_id') != aid:
            return 'unknown'
        saved = _read(_directory(jobs)/'attention/by-source'/(_id(aid)+'.json'))
        if saved is not None:
            proof = saved.get('proof') or {}
            attention = saved.get('attention') or {}
            reason = attention.get('reason')
            if (set(saved) == {'attention', 'proof'}
                    and set(proof) == {'family_id', 'claim_digest'}
                    and proof.get('family_id') == family
                    and proof.get('claim_digest') == _digest(record)
                    and set(attention) == {'source_attempt_id', 'state', 'reason', 'node', 'failure'}
                    and attention.get('source_attempt_id') == aid
                    and attention.get('state') == 'needs-attention'
                    and isinstance(reason, str)
                    and re.fullmatch(r'[a-z0-9][a-z0-9:-]{0,159}', reason)
                    and attention.get('node') == (meta.get('route_node') or '__owner__')
                    and isinstance(attention.get('failure'), str)):
                return 'settled'
            return 'unknown'
        return 'accepted'
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, DC.DispatchContractError):
        return 'unknown'


def _identity_refusal_reentry(jobs, lines, aid, record=None, *, allow_registered=False,
                              allow_launch_claimed=False):
    """Whether the same exact replacement duty may be re-entered after an
    identity-only refusal. This proves lineage and source eligibility; it does
    not grant launch authority or replace the current parent check.
    """
    try:
        rows = _rows(lines)
        if aid not in rows or recovery_delivery_state(jobs, aid) != 'settled':
            return False
        fields, source = rows[aid]
        binding = source_binding(jobs, source)
        reservation = source_reservation(jobs, aid)
        family = (reservation or {}).get('family_id') or (binding or ('',))[0]
        if not family or not binding or (reservation or {}).get('family_id') != family:
            return False
        canonical = _read(_record_path(jobs, family))
        if record is not None and record != canonical:
            return False
        record = _check_record(canonical, family)
        digest = _digest(record)
        if (record.get('original_attempt_id') != aid
                or binding != (family, record.get('replacement_attempt_id'), digest)):
            return False
        target = rows.get(record.get('replacement_attempt_id'))
        exact_successor = target is None
        successor_unstarted = target is None
        if target is not None:
            target_fields, target_meta = target
            exact_successor = (
                target_meta.get('attempt_id') == record['replacement_attempt_id']
                and target_meta.get('automatic_retry_of') == aid
                and target_meta.get('replacement_original_attempt_id') == aid
                and target_meta.get('replacement_family_id') == family
                and target_meta.get('replacement_ordinal') == '1'
                and target_meta.get('replacement_claim_digest') == digest
                and route_authority.replacement_parent_matches(source, target_meta, jobs, lineage=True)
                and all(target_meta.get(key) == source.get(key) for key in ('worker_type', 'dispatch_depth'))
            )
            successor_unstarted = exact_successor and (
                never_started(target_fields[1], target_meta)
                or allow_registered and target_fields[1] in {'open', 'running'}
                and target_meta.get('launch_started') != '1'
                and not target_meta.get('pid')
                and (allow_launch_claimed or target_meta.get('launch_claimed') != '1'))
        if not exact_successor:
            return False
        _no_competing_successor(rows, aid, record['replacement_attempt_id'])
        saved = _read(_directory(jobs) / 'attention/by-source' / (_id(aid) + '.json'))
        attention = (saved or {}).get('attention') or {}
        proof = (saved or {}).get('proof') or {}
        expected_node = source.get('route_node') or '__owner__'
        exact_attention = (
            set(saved or {}) == {'attention', 'proof'}
            and set(proof) == {'family_id', 'claim_digest'}
            and proof.get('family_id') == family
            and proof.get('claim_digest') == digest
            and set(attention) == {'source_attempt_id', 'state', 'reason', 'node', 'failure'}
            and attention.get('source_attempt_id') == aid
            and attention.get('state') == 'needs-attention'
            and attention.get('reason') == 'replacement-parent-identity-unproven'
            and attention.get('node') == expected_node
            and isinstance(attention.get('failure'), str)
        )
        if not exact_attention:
            return False
        source_proof = death_proof(fields, source, jobs=jobs, lines=lines)
        decision = route_authority.decide_continuation(action='identity-refusal-reentry',
            status=fields[1], metadata=source, facts={
                'exact_claim': True,
                'identity_refusal': True,
                'source_replacement_eligible': bool(source_proof.get('death_kind')),
                'successor_unstarted_or_absent': successor_unstarted,
            })
        return decision.allowed
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, DC.DispatchContractError):
        return False


def _settle_recovery_attention(jobs, aid, result):
    """Settle one accepted duty in the existing attention ledger after a known refusal."""
    try:
        jobs = Path(jobs)
        with _locked(jobs) as lines:
            rows = _rows(lines)
            row = rows.get(aid)
            if row is None:
                return
            _, meta = row
            index = source_reservation(jobs, aid)
            binding = source_binding(jobs, meta)
            family = (index or {}).get('family_id') or (binding or ('',))[0]
            if not family:
                return
            record = _check_record(_read(_record_path(jobs, family)), family)
            if record.get('original_attempt_id') != aid:
                return
            target_row = rows.get(record['replacement_attempt_id'])
            if target_row is not None:
                target = target_row[1]
                if (target.get('replacement_claim_digest') != _digest(record)
                        or target.get('replacement_family_id') != family
                        or target.get('automatic_retry_of') != aid
                        or target.get('launch_started') == '1'):
                    return
            reason = result.get('reason') or 'replacement-observation-unavailable'
            if not isinstance(reason, str) or not re.fullmatch(r'[a-z0-9][a-z0-9:-]{0,159}', reason):
                reason = 'replacement-observation-unavailable'
            attention = {
                'source_attempt_id': aid,
                'state': 'needs-attention',
                'reason': reason,
                'node': meta.get('route_node') or '__owner__',
                'failure': meta.get('note') or meta.get('failure_class') or 'terminal-failure',
            }
            proof = {'family_id': family, 'claim_digest': _digest(record)}
            path = _directory(jobs)/'attention/by-source'/(_id(aid)+'.json')
            existing = _read(path)
            candidate = {'attention': attention, 'proof': proof}
            if existing is not None:
                if existing.get('proof') == proof:
                    return
                raise DC.DispatchContractError('replacement-attention-conflict')
            _once(path, candidate)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, DC.DispatchContractError):
        # A failed settlement write leaves the accepted claim visible and retryable.
        return


def _approved_fix_answers(record, jobs, rows=None):
    """Read the FAILs an approved claim already pinned, including owner-closed reviews.

    Older claims omitted those reviews because their completion note masked the FAIL.
    Their exact reuse snapshot still binds the historical marker and verdict; it can
    restore an admissible target without editing the claim or adopting any later failed round.
    """
    proof = record.get('proof') or {}
    answers = list(proof.get('answers') or [])
    if (proof.get('death_kind') != CORRECTED or proof.get('source_result') != 'FAIL'
            or not (record.get('reuse') or {}).get('completed')):
        return answers
    try:
        rows = _rows(Path(jobs).read_text().splitlines()) if rows is None else rows
        source = rows[record['original_attempt_id']][1]
        _, route = _route(jobs, record['original_attempt_id'], source)
        eligible, _ = route_authority.fix_answers(route, ['\t'.join(fields) for fields, _ in rows.values() if fields[1] not in ('open', 'running')], jobs)
    except (OSError, ValueError, KeyError, DC.DispatchContractError):
        # Supplemental historical targets cannot break an existing approved claim.
        return answers
    import review_round_cap
    directory = DC._route_module().completion_dir(record['route_id'], jobs=jobs)
    for pin in (record.get('reuse') or {}).get('completed') or []:
        attempt = pin.get('attempt_id')
        if not attempt or attempt in answers or attempt not in eligible:
            continue
        fields, meta = rows[attempt]
        if (meta.get('route_id') != record['route_id']
                or meta.get('route_hash') != record['route_hash']
                or meta.get('route_node') != pin.get('node')
                or meta.get('worker_type') != 'review'
                or not review_round_cap.last_verdict_blocking([(fields[1], meta)], 'review')):
            continue
        for path in directory.glob(f"{pin['node']}*.json"):
            try:
                marker = _read(path)
            except DC.DispatchContractError:
                continue
            if (marker and _digest(marker) == pin.get('marker_digest')
                    and marker.get('route_id') == record['route_id']
                    and marker.get('route_hash') == record['route_hash']
                    and marker.get('node_id') == pin['node']
                    and marker.get('attempt_id') == attempt
                    and DC.owner_closure_shape(marker) == 'registered-review'):
                answers.append(attempt)
                break
    return answers


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
    rows = None
    for path in paths:
        try:
            record = _read(path)
            if record:
                _check_record(record, record.get('family_id'))
        except DC.DispatchContractError:
            continue
        proof = (record or {}).get('proof') or {}
        if (record and record.get('route_id') == route_id and proof.get('death_kind') == CORRECTED
                and proof.get('source_result') == 'FAIL'):
            if rows is None:
                try:
                    rows = _rows(Path(jobs).read_text().splitlines())
                except (OSError, DC.DispatchContractError):
                    rows = {}  # Keep the claim's original answers when enrichment is unavailable.
            answers = _approved_fix_answers(record, jobs, rows)
            if not answers:
                continue
            found.append({'basis': 'user-direction', 'answers': answers,
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


def _completion_unproven(route, node, marker, currency):
    """A completed node's evidence judgement blocks reuse; retrying cannot change it.

    Carry the judgement and the existing supported action so the caller does
    not have to rediagnose the marker.
    """
    node_id = str(node['id'])
    error = DC.DispatchContractError('replacement-completion-unproven', node_id)
    evidence = str(((marker or {}).get('evidence') or {}).get('path') or '')
    error.completion = {'completion_node': node_id, 'completion_currency': currency.state,
                        'completion_reason': currency.reason,
                        **({'completion_evidence': evidence} if evidence else {})}
    action = DC.route_state_next_action(currency.reason, node_id, route.get('route_id'), node,
                                        evidence=evidence or None)
    judged = (f"Completed node {node_id} reads {currency.state} ({currency.reason}); "
              "running the same start again cannot change that.")
    if action != '-':
        error.next_step = (f"{judged} Run: {action} (outside an owner add "
                           "--author-attempt-id <attempt that made the change>), then resume.")
    else:
        error.next_step = f"{judged} Inspect that node's completion marker and evidence, then resume."
    return error


def _reuse_snapshot(jobs, route, lines):
    """Pin settled current completions and identify only provenance-proved stale results."""
    module = DC._route_module()
    directory = module.completion_dir(route['route_id'], jobs=Path(jobs))
    completed = []
    invalidated = {}
    observations = {}
    for node in route.get('nodes', []):
        path = directory/(str(node['id'])+'.json')
        if not path.exists():
            continue
        marker = _read(path)
        currency = DC.gate_currency(route, node, path, marker, observe=True, jobs=jobs)
        merge_unobservable = (currency.state == 'completion-evidence-unreadable'
                              and currency.reason == 'review-source-index-unmerged')
        if merge_unobservable:
            import commit_policy
            merge_unobservable = commit_policy.git_operation(route.get('cwd', '')) == 'merge'
        if currency.state not in {'current', 'superseded'} and not merge_unobservable:
            raise _completion_unproven(route, node, marker, currency)
        ready = DC.completion_attempt_readiness(route, node, marker, Path(jobs), registry_lines=lines)
        if ready.state != 'ready':
            raise DC.DispatchContractError('replacement-completion-unsettled', str(node['id']))
        observations[str(node['id'])] = (node, path, marker, currency)
        if currency.state == 'superseded' or merge_unobservable:
            invalidated[str(node['id'])] = {
                'node': node['id'], 'marker_digest': _digest(marker),
                'attempt_id': marker.get('attempt_id', ''),
                'currency': currency.state, 'currency_reason': currency.reason,
                'depends_on_invalid': [],
            }
    # A settled marker downstream of a stale result remains historical too.
    # Compute the dependency closure from the sealed route, preserving unrelated
    # non-prefix siblings and their exact marker bytes.
    changed = True
    while changed:
        changed = False
        for node_id, (node, _path, marker, currency) in observations.items():
            if node_id in invalidated:
                continue
            invalid_parents = sorted(set(str(parent) for parent in node.get('depends_on') or ())
                                     .intersection(invalidated))
            if invalid_parents:
                invalidated[node_id] = {
                    'node': node['id'], 'marker_digest': _digest(marker),
                    'attempt_id': marker.get('attempt_id', ''),
                    'currency': currency.state, 'currency_reason': currency.reason,
                    'depends_on_invalid': invalid_parents,
                }
                changed = True
    for node in route.get('nodes', []):
        node_id = str(node.get('id', ''))
        observation = observations.get(node_id)
        if observation is None or node_id in invalidated:
            continue
        _node, path, marker, _currency = observation
        # A gates-off evidence edit is recorded only when this completion is
        # actually part of the successor's reusable evidence.
        DC.note_evidence_change(route, node, path, marker)
        completed.append({'node': node['id'], 'marker_digest': _digest(marker),
                          'attempt_id': marker.get('attempt_id', '')})
    pending = [str(node.get('id')) for node in route.get('nodes', [])
               if str(node.get('id')) not in {row['node'] for row in completed}]
    root = Path(route['artifact_root'])
    from artifact_producer import route_cycle_for, cycle_route_admission
    cycle = route_cycle_for(root, route)
    if not cycle or not cycle_route_admission(root, cycle, route).allow:
        raise DC.DispatchContractError('replacement-producer-not-open')
    return {'completed': completed, 'cycle_id': cycle['cycle_id'],
            'producer_id': cycle.get('producer_id', ''), 'gates': _gate_snapshot(jobs, route),
            'invalidated': [invalidated[key] for key in sorted(invalidated)],
            'pending': pending}


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
    previous_completed = {r['node']: r for r in previous.get('completed', [])}
    now = {r['node']: r for r in current.get('completed', [])}
    previous_invalidated = {row.get('node'): row for row in previous.get('invalidated', [])}
    if len(previous_completed) != len(previous.get('completed', [])):
        return False
    if len(previous_invalidated) != len(previous.get('invalidated', [])):
        return False

    def reobserved_invalidation(row):
        return ((row.get('currency') == 'completion-evidence-unreadable'
                 and row.get('currency_reason') == 'review-source-index-unmerged')
                or (bool(row.get('depends_on_invalid'))
                    and all(parent in now for parent in row.get('depends_on_invalid') or ())))

    for node, row in now.items():
        if previous_completed.get(node) == row:
            continue
        old_invalid = previous_invalidated.get(node)
        if old_invalid is None:
            # A settled current node can finish after claim while the same
            # successor is preparing to launch. _reuse_snapshot has already
            # revalidated its exact marker, currency, and attempt readiness.
            if node in previous_completed:
                return False
            continue
        if (not old_invalid or old_invalid.get('marker_digest') != row.get('marker_digest')
                or old_invalid.get('attempt_id', '') != row.get('attempt_id', '')
                or not reobserved_invalidation(old_invalid)):
            return False
    invalidated = {row.get('node'): row for row in current.get('invalidated', [])}
    if len(invalidated) != len(current.get('invalidated', [])):
        return False
    for node, old in previous_completed.items():
        if now.get(node) == old:
            continue
        current_invalid = invalidated.get(node)
        if (not current_invalid
                or current_invalid.get('marker_digest') != old.get('marker_digest')
                or current_invalid.get('attempt_id', '') != old.get('attempt_id', '')
                or current_invalid.get('currency') not in {'current', 'superseded',
                                                            'completion-evidence-unreadable'}
                or (current_invalid.get('currency') == 'completion-evidence-unreadable'
                    and current_invalid.get('currency_reason') != 'review-source-index-unmerged')):
            return False
    for node, old in previous_invalidated.items():
        new = invalidated.get(node)
        if new:
            if (new.get('marker_digest') != old.get('marker_digest')
                    or new.get('attempt_id', '') != old.get('attempt_id', '')
                    or new.get('currency') not in {'current', 'superseded',
                                                   'completion-evidence-unreadable'}
                    or (new.get('currency') == 'completion-evidence-unreadable'
                        and new.get('currency_reason') != 'review-source-index-unmerged')
                    or not set(old.get('depends_on_invalid') or ()).issubset(
                        set(new.get('depends_on_invalid') or ()))):
                return False
            continue
        now_current = now.get(node)
        if (not now_current
                or now_current.get('marker_digest') != old.get('marker_digest')
                or now_current.get('attempt_id', '') != old.get('attempt_id', '')
                or not reobserved_invalidation(old)):
            return False
    if any(node not in previous_completed and node not in previous_invalidated
           for node in invalidated):
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
    if (recovery_delivery_state(jobs, prior) == 'settled'
            and not _identity_refusal_reentry(jobs, lines, prior, record, allow_registered=True,
                                              allow_launch_claimed=True)):
        raise DC.DispatchContractError('replacement-duty-settled')
    if prior not in rows:
        raise DC.DispatchContractError('replacement-source-missing')
    fields, source = rows[prior]
    if (source_binding(jobs, source) != (family, record['replacement_attempt_id'], _digest(record))
            or (source_reservation(jobs, prior) or {}).get('family_id') != family):
        raise DC.DispatchContractError('replacement-claim-pending')
    _no_competing_successor(rows, prior, record['replacement_attempt_id'])
    route_authority.require_continuation(route_authority.decide_continuation(action='replacement-budget',
        status=fields[1], metadata=source, facts={'death_kind': record['proof']['death_kind'],
            'budget_exhausted': _budget_exhausted(jobs, source, lines=lines, capacity=_is_capacity_record(record))}))
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


def parent_resume_obligation(jobs, aid, *, route_id=None):
    """Read-only structural proof that an exact replacement edge remains unstarted.

    This keeps a no-progress parent notice alive even when the source cannot
    automatically resume (for example, an unanswered semantic BLOCKED result).
    It grants no launch authority; the stricter eligibility check and ordinary
    start decide whether execution may proceed.
    """
    try:
        jobs = Path(jobs).resolve()
        lines = jobs.read_text().splitlines()
        rows = _rows(lines)
        if aid not in rows:
            return None
        fields, source = rows[aid]
        if fields[1] != 'done' or source.get('worker_type') != 'owner' or source.get('dispatch_depth') != '1':
            return None
        import route_parent_close
        if route_parent_close.row_requested(source, jobs):
            return None
        path, route = _route(jobs, aid, source)
        if (route_id and route.get('route_id') != route_id) or path.with_suffix('.outcome.json').exists():
            return None
        family = (source_reservation(jobs, aid) or {}).get('family_id')
        binding = source_binding(jobs, source)
        if not family or not binding or binding[0] != family:
            return None
        record = _check_record(_read(_record_path(jobs, family)), family)
        digest = _digest(record)
        if (record.get('original_attempt_id') != aid
                or binding != (family, record.get('replacement_attempt_id'), digest)):
            return None
        target = rows.get(record['replacement_attempt_id'])
        if target is not None:
            target_fields, target_meta = target
            if (target_meta.get('attempt_id') != record['replacement_attempt_id']
                    or target_meta.get('automatic_retry_of') != aid
                    or target_meta.get('replacement_original_attempt_id') != aid
                    or target_meta.get('replacement_family_id') != family
                    or target_meta.get('replacement_ordinal') != '1'
                    or target_meta.get('replacement_claim_digest') != digest
                    or not route_authority.replacement_parent_matches(source, target_meta, jobs, lineage=True)
                    or any(target_meta.get(key) != source.get(key) for key in ('worker_type', 'dispatch_depth'))
                    or not never_started(target_fields[1], target_meta)):
                return None
        _no_competing_successor(rows, aid, record['replacement_attempt_id'])
        return {'source_attempt_id': aid, 'route_id': route['route_id'],
                'route_hash': route['route_hash'], 'route_file': str(path),
                'replacement_attempt_id': record['replacement_attempt_id'], 'claim': record,
                'jobs': jobs, 'lines': lines, 'rows': rows, 'fields': fields, 'source': source}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, DC.DispatchContractError):
        return None


def parent_resume_eligibility(jobs, aid, *, route_id=None, environ=None, require_parent=False):
    """Read-only proof that a parent may arm the exact resultless replacement.

    This is scheduling eligibility, not launch authority. The normal start
    repeats admission, access, capacity, and parent checks before execution.
    """
    obligation = parent_resume_obligation(jobs, aid, route_id=route_id)
    if obligation is None:
        return None
    jobs, lines, rows = obligation['jobs'], obligation['lines'], obligation['rows']
    fields, source, record = obligation['fields'], obligation['source'], obligation['claim']
    try:
        if death_kind(fields, source, jobs=jobs, lines=lines) is None:
            return None
        death_proof(fields, source, jobs=jobs, lines=lines)
        validate_claim_source(jobs, lines, record)
        _children_quiescent(rows, aid)
        if require_parent:
            from session_identity import identity
            caller = identity(environ)
            if not caller.known or not caller.session_id:
                return None
            route_authority.require_replacement_parent(
                jobs, rows, source, current_session=lambda: caller.session_id)
        return {key: obligation[key] for key in
                ('source_attempt_id', 'route_id', 'route_hash', 'route_file',
                 'replacement_attempt_id', 'claim')}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, DC.DispatchContractError):
        return None


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
        # Legacy pre-registered candidates converge at their first launch claim;
        # the shared admission under this same jobs lock rejects a committed
        # sibling without letting unclaimed candidates deadlock one another.
        DC._automatic_retry_admission(peers, metadata)
        if source.get('replacement_original_attempt_id'):
            # A completed content result or frame's already-applied profile
            # descent cannot become transport failure merely by retry labeling.
            death_proof(source_fields, source, jobs=jobs, lines=lines)
        route_authority.require_continuation(route_authority.decide_continuation(action='replacement-budget',
            status=source_fields[1], metadata=source, facts={
                'budget_exhausted': _budget_exhausted(jobs, source, lines=peers)}))
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
    with its digest. Compute-stage replacements also prepare the current runtime's
    normal compute defaults instead of replaying an older owner's request."""
    change = route_authority.access_in_force(route)
    import execution_access as EA
    runtime_defaults = ((change is None or change.get('source') == 'derived')
                        and EA.compute_execution_scope(route, owner=True))
    if not runtime_defaults and (change is None or change.get('source') == 'derived'):
        return None
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
            'source': 'compute-runtime-defaults' if runtime_defaults else change['source'],
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
            hold = harness_hold(jobs, result['harness'], model=source.get('model'), recheck=True)
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
        return harness_hold(jobs, pin, model=model, recheck=True) if pin else None
    if not harness:
        return None
    hold = harness_hold(jobs, harness, model=model or source.get('model'), recheck=True)
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
    states = usage_states(jobs, models={harness: model or source.get('model')}, recheck=True)
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


def _advance(jobs, aid, *, run=subprocess.run, authority_check=None, resume_capacity=False):
    """A bound runtime checkpoint, never a read-only observer, calls this.

    `resume_capacity` is set only by an explicit `start`: a usage-limit stop is replaced
    when the person resumes, never by a supervisor tick, so no loop of automatic launches exists.
    """
    lines = Path(jobs).read_text().splitlines()
    rows = _rows(lines)
    if aid not in rows:
        return {'state':'unavailable','reason':'replacement-source-missing'}
    if (recovery_delivery_state(jobs, aid) == 'settled'
            and not (resume_capacity and _identity_refusal_reentry(
                jobs, lines, aid, allow_registered=True))):
        return {'state': 'not-applicable'}
    fields, source = rows[aid]
    kind = death_kind(fields, source, jobs=jobs)
    progress = route_authority.decide_continuation(action='replacement-progress', jobs=jobs,
        status=fields[1], metadata=source, facts={'kind': kind, 'resume_capacity': resume_capacity, 'registry_rows': rows})
    if progress.state == 'advance':
        return advance(jobs, source['replacement_original_attempt_id'], run=run,
                       authority_check=authority_check, resume_capacity=resume_capacity)
    if progress.state == 'exhausted':
        return exhausted_attention(jobs, aid, source)
    if progress.state == 'not-applicable':
        parked = owner_parked_gate(jobs, aid) if source.get('note') == 'dead-worker-blocked' else None
        return {'state': 'not-applicable', 'parked_gate': parked} if parked else {'state': 'not-applicable'}
    if progress.state == 'capacity':
        return _capacity_wait(jobs, aid, source)
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
        record = claim(Path(jobs), aid, resume_capacity=resume_capacity)
        rows = _rows(Path(jobs).read_text().splitlines())
        source = rows[aid][1]
        replacement = record['replacement_attempt_id']
        if replacement in rows:
            replacement_fields, replacement_meta = rows[replacement]
            pinned = None
            if source.get('worker_type') == 'owner' and replacement_fields[1] in {'open', 'running'} and replacement_meta.get('launch_claimed') == '1':
                _, route = _route(jobs, aid, source)
                pinned = route_authority.sealed_pin_harness(route_authority.route_in_force(route), worker_type='owner')
            progress = route_authority.decide_continuation(action='replacement-progress', jobs=jobs,
                status=replacement_fields[1], metadata=replacement_meta, facts={'record': record, 'pin': pinned})
            if progress.state == 'advance':
                return advance(jobs, replacement, run=run, authority_check=authority_check,
                               resume_capacity=resume_capacity)
            if progress.state == 'exhausted':
                return exhausted_attention(jobs, replacement, replacement_meta)
            if progress.state in {'reused', 'running'}:
                return {'state': progress.state, 'attempt_id': replacement, 'record': record}
            if progress.state == 'not-applicable':
                return {'state': 'not-applicable', 'attempt_id': replacement, 'record': record}
            if not progress.allowed:
                return {'state': 'needs-attention', 'reason': progress.reason, 'attempt_id': replacement,
                        'harness': replacement_meta.get('harness'), 'requested_harness': pinned, 'detail': progress.detail,
                        'source_attempt_id': aid, 'node': source.get('route_node') or '__owner__'}
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
            reason = ('replacement-launch-timeout' if getattr(completed, 'timed_out', False)
                      else 'replacement-launch-interrupted' if getattr(completed, 'received_signal', None)
                      else 'replacement-launch-output-incomplete')
            return {'state':'needs-attention','reason':reason,
                    'attempt_id':replacement,'record':record,
                    'cleanup_incomplete':bool(getattr(completed, 'cleanup_incomplete', False)),
                    'launcher_returncode':getattr(completed, 'returncode', None),
                    'launcher_stdout':getattr(completed, 'stdout', None),
                    'launcher_stderr':getattr(completed, 'stderr', None),
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
            holds = [(h, harness_hold(jobs, h, recheck=True)) for h in candidates]
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
        result.update(getattr(exc,'completion',None) or {})
        if getattr(exc, 'retry_when', ''):
            result.update(next_action=exc.next_action, retry_when=exc.retry_when)
        elif getattr(exc, 'next_step', ''):
            result['next_step'] = exc.next_step
        return result


def advance(jobs, aid, *, run=subprocess.run, authority_check=None, resume_capacity=False):
    """Run the existing controller and settle only conclusive duty refusals."""
    result = _advance(jobs, aid, run=run, authority_check=authority_check,
                      resume_capacity=resume_capacity)
    if result.get('state') == 'needs-attention':
        reason = result.get('reason', '')
        diagnostic = dict(
            line.split('=', 1) for line in result.get('launcher_diagnostic', '').splitlines()
            if '=' in line
        )
        definitive = (
            reason in {
                'replacement-human-gate-pending',
                'replacement-workflow-not-open',
                'pin-ignored-for-replacement',
            }
            or diagnostic.get('reason') in {
                'replacement-registration-refused',
                'replacement-launch-refused',
                'replacement-spawn-refused',
            }
        )
        if definitive:
            for source in {aid, result.get('source_attempt_id') or aid}:
                _settle_recovery_attention(jobs, source, result)
    return result


def effective_attempts(jobs, attempts):
    """Project each input slot to its final launched successor, if any.

    Every hop is checked against its immutable claim and registry row. The
    receipt stays bounded to one original-to-final projection per slot; the
    first edge's family and digest retain the historical receipt contract.
    A claim, registration, or launch reservation without ``launch_started``
    is not execution and cannot take delivery of the source failure.
    """
    rows = _rows(Path(jobs).read_text().splitlines())
    effective = set(attempts)
    projected = []
    for aid in sorted(attempts):
        if aid not in rows:
            continue
        current = aid
        last_launched = aid
        seen = {aid}
        chain = [aid]
        first = None
        root_edge = None
        while current in rows:
            _, source = rows[current]
            binding = source_binding(jobs, source)
            if not binding or not binding[0] or not binding[1]:
                break
            family, replacement, claim_digest = binding
            record = _check_record(_read(_record_path(jobs, family)), family,
                                   {'replacement_claim_digest': claim_digest or None})
            if (not record or record.get('original_attempt_id') != current
                    or record.get('replacement_attempt_id') != replacement
                    or record.get('family_id') != family):
                raise DC.DispatchContractError('replacement-lineage-unproven', current)
            _no_competing_successor(rows, current, replacement)
            if replacement not in rows:
                break  # A durable claim alone is not a launch receipt.
            _, target = rows[replacement]
            if (target.get('replacement_claim_digest') != _digest(record)
                    or target.get('replacement_family_id') != family
                    or target.get('automatic_retry_of') != current
                    or target.get('replacement_original_attempt_id') != current
                    or not route_authority.replacement_parent_matches(source, target, jobs, lineage=True)
                    or any(target.get(k) != source.get(k) for k in ('worker_type', 'dispatch_depth'))):
                raise DC.DispatchContractError('replacement-lineage-unproven', replacement)
            if replacement in seen:
                raise DC.DispatchContractError('replacement-lineage-cycle', replacement)
            if root_edge is None:
                root_edge = {'original_attempt_id': aid, 'replacement_attempt_id': replacement,
                             'family_id': family, 'claim_digest': _digest(record)}
            seen.add(replacement)
            chain.append(replacement)
            current = replacement
            if target.get('launch_started') == '1':
                last_launched = replacement
                if first is None:
                    first = dict(root_edge)
            elif not never_started(rows[replacement][0][1], target):
                # Open, legacy, and unobservable reservations remain the current
                # owner. Only an exact terminal never-launched row can be skipped.
                break
        if first is not None:
            effective.discard(aid)
            effective.add(last_launched)
            projected.append((chain, {**first, 'replacement_attempt_id': last_launched}))
    covered = {attempt for chain, _edge in projected for attempt in chain[1:]}
    mapping = [edge for chain, edge in projected if chain[0] not in covered]
    return effective, mapping


def advance_batch(jobs, attempts, *, authority_check=None, run=subprocess.run):
    """Runtime-only checkpoint; no model turn or new polling service."""
    jobs_path = Path(jobs)
    if not attempts:
        return set(), [], []
    try:
        view = registry_view(jobs_path)
    except OSError:
        view = None
    attention = []
    needs_projection = False
    for aid in sorted(attempts):
        if view is not None:
            records = view.completion_attempt_records(aid)
            if len(records) == 1:
                record = records[0]
                metadata = record.completion_metadata()
                has_lineage = bool(source_binding(jobs_path, metadata) or any(
                    metadata.get(key) for key in (
                        "replacement_original_attempt_id", "replacement_family_id",
                        "replacement_attempt_id", "replacement_claim_digest")))
                if (record.status in {"open", "running"} and not has_lineage
                        and death_kind(list(record.fields), metadata, jobs=jobs_path) is None):
                    # A live exact attempt with no replacement evidence has no
                    # replacement work. The completion join already performed
                    # its fresh process/recipient checks for this checkpoint.
                    continue
                needs_projection = True
        step=advance(jobs,aid,authority_check=authority_check,run=run)
        if step.get('state')=='needs-attention':
            attention.append(step)
        else:
            needs_projection = True
    if not needs_projection:
        return set(attempts), [], attention
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
    stage_progress = ''
    if prior and getattr(args, 'worker_type', '') == 'stage':
        try:
            progress_lines = Path(args.jobs_path).read_text(encoding='utf-8', errors='replace').splitlines()
            progress_rows = _rows(progress_lines)
            if prior in progress_rows:
                source_fields, source_meta = progress_rows[prior]
                stage_progress = _stage_progress_block(
                    Path(args.jobs_path), prior, source_fields, source_meta, progress_lines)
        except (OSError, ValueError, DC.DispatchContractError):
            stage_progress = ('\n\n## Observed prior-attempt progress\n'
                              f'- predecessor {prior} could not be read; no progress was inferred.\n')
    if not record or getattr(args, 'worker_type', '') != 'owner':
        return retry_context + stage_progress
    lines = jobs.read_text(encoding='utf-8', errors='replace').splitlines()
    source = _rows(lines)[prior][1]
    _route_path, route = _route(jobs, prior, source)
    current_reuse = _reuse_snapshot(jobs, route, lines)
    if not _reuse_preserved(record['reuse'], current_reuse):
        raise DC.DispatchContractError('replacement-reuse-evidence-drift')
    completed = ', '.join(str(row['node']) for row in current_reuse['completed']) or '(none)'
    invalidated = ', '.join(str(row['node']) for row in current_reuse.get('invalidated', []))
    pending = ', '.join(str(node) for node in current_reuse.get('pending', [])) or '(none)'
    source_fields, source = _rows(lines)[prior]
    kind = death_kind(source_fields, source, jobs=jobs, lines=lines)
    fix = kind == CORRECTED and (record.get('proof') or {}).get('source_result') == 'FAIL'
    if fix:
        opening = (f'The previous owner {prior} ended FAIL and a person approved a fix for it; this continues '
                   f'the same work on the existing route {record["route_id"]}.\n')
    elif kind == CORRECTED:
        ended = ('exited before settlement and received a correction'
                 if record['proof'].get('source_result') == 'EXITED' else
                 'ended with a handoff-format error and received an answer'
                 if record['proof'].get('source_result') == 'CONTRACT' else
                 'ended BLOCKED and a person has answered it')
        opening = (f'The previous owner {prior} {ended}; this continues '
                   f'the same work on the existing route {record["route_id"]}.\n')
    elif kind == 'capacity':
        opening = f'The previous attempt {prior} stopped at a usage limit; this resumes it on the existing route {record["route_id"]}.\n'
    elif kind == 'unlaunched':
        opening = f'The previous attempt {prior} never started (its launcher stopped before spawning); this starts the same work on the existing route {record["route_id"]}.\n'
    else:
        opening = f'You replace exact-dead attempt {prior} once, on the existing route {record["route_id"]}.\n'
    answers = _approved_fix_answers(record, jobs) if fix else []
    check_fix = fix and bool(answers)
    rerun = ('Rerun the stage that makes the approved fix and every check after it; reuse everything '
             'else. ' if check_fix else
             'Continue only unfinished work. Do not rerun completed nodes, successful siblings, or completed prefixes. ')
    text = ('\n\n## Verified recovery context\n'
            + opening +
            f'Reuse the existing cycle {record["reuse"]["cycle_id"]} and completion evidence for: {completed}.\n'
            + (f'Reopen these stages because their former result is historical: {invalidated}.\n'
               if invalidated else '')
            + f'Unfinished route stages: {pending}.\n'
            + rerun +
            'Keep existing human answers and gate releases; do not ask the same scope again. '
            'Preserve the original failure and report any second failure as needs-attention.\n')
    git_dir = ''
    if route.get('cwd'):
        try:
            probe = subprocess.run(['git', '-C', str(route['cwd']), 'rev-parse', '--git-dir'],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   check=False, timeout=5)
            if probe.returncode == 0:
                git_dir = probe.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            git_dir = ''
    if git_dir:
        git_path = Path(git_dir)
        if not git_path.is_absolute():
            git_path = Path(route['cwd']) / git_path
        if (git_path / 'MERGE_HEAD').is_file():
            text += ('A merge is in progress in the bound worktree. Observe it and retain responsibility '
                     'for the existing route. Leave the merge, source files, and index untouched; do not '
                     'start stages, commit, or publish until the merge is settled.\n')
    preserved = (record.get('proof') or {}).get('preserved_resources', [])
    if preserved:
        text += ('Registered payloads from the previous owner are preserved under their existing '
                 'run records. Continue supervising those runs; do not relaunch a resource node '
                 'because its owner changed. Inspect these records for their current identities '
                 'and state; this list is not completion or launch authority.\n')
        for resource in preserved:
            text += ('- ' + resource['kind'] + ' ' + resource['run_id'] + ': '
                     + (resource.get('registry') or resource.get('record') or '') + '\n')
    if check_fix:
        text += ('The failed checks this fix answers ('
                 + ', '.join(answers) +
                 ') each get one more verdict round (closure-check) once the fix is in; there is no '
                 'further round after it.\n')
    # A replacement that never started showed its answers and gate to no model,
    # so the attempt that takes its place carries them as they were.
    carried = _unstarted_replacement_claim(jobs, prior) if kind == 'unlaunched' else None
    carried_proof = (carried or {}).get('proof') or {}
    if not (carried_proof.get('parked_gate') or carried_proof.get('death_kind') == CORRECTED):
        carried = None
    pinned_continuation = (record.get('proof') or {}).get('carried_continuation')
    if pinned_continuation:
        if (carried is None
                or pinned_continuation.get('attempt_id') != carried.get('original_attempt_id')
                or pinned_continuation.get('family_id') != carried.get('family_id')
                or pinned_continuation.get('claim_digest') != _digest(carried)
                or carried_proof.get('death_kind') != CORRECTED):
            raise DC.DispatchContractError('replacement-lineage-unproven', prior)
        _correction_context(jobs, carried['original_attempt_id'], carried_proof)
        carried_proof = {**carried_proof, 'corrections': pinned_continuation.get('corrections') or []}
    if carried is not None:
        text += (f'{prior} was itself the continuation of {carried["original_attempt_id"]}; '
                 'what that continuation was given follows.\n')
    for claim, answered in ((record, prior), (carried, carried and carried['original_attempt_id'])):
        proof = carried_proof if claim is carried and pinned_continuation else (claim or {}).get('proof') or {}
        if proof.get('parked_gate'):
            text += _gate_context(jobs, claim)
        if proof.get('death_kind') == CORRECTED:
            if claim is carried:
                ended = ('ended with a handoff-format error and received an answer'
                         if proof.get('source_result') == 'CONTRACT' else
                         'ended BLOCKED and a person has answered it')
                text += (f'The owner {answered} {ended}; '
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
    through a run of such replacements, the first source row that actually ran."""
    try:
        rows = _rows(Path(jobs).read_text().splitlines())
    except (OSError, DC.DispatchContractError):
        return None
    seen = set()
    while aid not in seen:
        seen.add(aid)
        row = rows.get(aid)
        if row is None:
            return None
        fields, meta = row
        family = meta.get('replacement_family_id', '')
        if (not meta.get('replacement_original_attempt_id')
                or not never_started(fields[1], meta)
                or not re.fullmatch(r'[0-9a-f]{64}', family)):
            return None
        claim = _check_record(_read(_record_path(jobs, family)), family)
        original = rows.get(claim.get('original_attempt_id'))
        if (claim['replacement_attempt_id'] != aid
                or meta.get('automatic_retry_of') != claim.get('original_attempt_id')
                or meta.get('replacement_claim_digest') != _digest(claim)
                or original is None
                or not route_authority.replacement_parent_matches(original[1], meta, jobs, lineage=True)
                or any(meta.get(key) != original[1].get(key) for key in ('worker_type', 'dispatch_depth'))):
            return None
        original_id = claim['original_attempt_id']
        original_fields, original_meta = original
        if not never_started(original_fields[1], original_meta):
            return claim
        # The launch fence on the source row owns execution truth. Legacy
        # claims may have recorded `silent` before the successor's
        # never-launched disposition was settled.
        aid = original_id
    return None


def _correction_context(jobs, prior, proof):
    """All currently unconsumed answers, after verifying the claim's pinned floor."""
    from dispatch_owner_input import OwnerInput
    retained = _retained_corrections(jobs, prior)
    kept = {item['id']: item for item in retained}
    seen = {}
    for pinned in proof.get('corrections') or []:
        item = kept.get(pinned.get('id'))
        if item is None or item['digest'] != pinned.get('digest'):
            raise DC.DispatchContractError('replacement-correction-drift', str(pinned.get('id')))
    items = []
    for item in retained:
        old = seen.get(item['id'])
        if old is not None:
            if old != item['digest']:
                raise DC.DispatchContractError('replacement-correction-drift', str(item['id']))
            continue
        seen[item['id']] = item['digest']
        items.append(item)
    handoff = proof.get('handoff')
    if proof.get('source_result') == 'EXITED':
        return ('The previous owner exited before settlement. The correction below names the remaining work; '
                'preserve completed checks and continue the existing route.' + OwnerInput.text(items) + '\n')
    if proof.get('source_result') == 'CONTRACT':
        return ('The previous owner\'s final result had a handoff-format error. The answer below gives the '
                'direction for continuing the existing route; preserve completed work and checks.'
                + OwnerInput.text(items) + '\n')
    answer = ('The answer below is the fix a person approved for the failure it reported. Treat it as '
              'given: do not ask for it again; apply it, then continue through the remaining declared stages.'
              if proof.get('source_result') == 'FAIL' else
              'The answer below is the reply to what it was waiting for (for example an approval it asked '
              'for). Treat it as given: do not ask for it again, and continue from where the previous '
              'owner stopped through the remaining declared stages.')
    if proof.get('fallback_nodes'):
        answer += (' The following checks have no registered rounds left: '
                   + ', '.join(proof['fallback_nodes'])
                   + '. Use their existing owner-inline/native fallback and finish disposition and reporting; '
                     'preserve earlier FAILs and do not register another check round.')
    return ((f'The previous owner reported why it stopped in {handoff}. ' if handoff else '')
            + answer + OwnerInput.text(items) + '\n')
