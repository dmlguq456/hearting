"""Read the effective SD-157 attempt for each immutable sub-session slot."""
from __future__ import annotations

import dispatch_replacement as R

# The slot's own work: what it does, where, and the immutable inputs it was given. Who answers for the
# parent now (the attempt, its session, the label, the route file/hash it runs from, the harness) follows
# the replacement lineage that claim admission binds (`route_authority.require_recovery_binding`).
SCOPE_KEYS = (
    'route_id', 'route_node', 'worker_type', 'dispatch_depth',
    'session_chain_id', 'subsession_id', 'subsession_index', 'subsession_count',
    'subsession_mode', 'subsession_purpose', 'stage_authority', 'phase_brief',
    'phase_brief_sha256', 'fixed_files_sha256', 'narrow_verify_sha256',
    'expected_round_trips',
)


def project(jobs, manifest, rows):
    """Keep one row per declared slot; neither manifest nor history is rewritten.

    ``rows`` are one registry snapshot's dict rows (metadata/fields/status).
    A concurrent publication absent from that snapshot refuses this observation
    rather than synthesizing a row or dropping an unproved failed original.
    """
    declared = {session['attempt_id'] for session in manifest['sessions']}
    _effective, edges = R.effective_attempts(jobs, declared)
    mapping = {aid: aid for aid in declared}
    if not edges:
        return rows, mapping
    by_id = {}
    for row in rows:
        by_id.setdefault(row['metadata'].get('attempt_id'), []).append(row)
    consumed = set()
    for edge in edges:
        original = edge['original_attempt_id']; target = edge['replacement_attempt_id']
        old_rows = by_id.get(original, []); new_rows = by_id.get(target, [])
        if len(old_rows) != 1 or len(new_rows) != 1:
            raise R.DC.DispatchContractError('replacement-subsession-snapshot-mismatch')
        old, new = old_rows[0], new_rows[0]
        if (old['metadata'].get('session_chain_id') != manifest['chain_id']
                or any(old['metadata'].get(key) != new['metadata'].get(key) for key in SCOPE_KEYS)
                or old['fields'][2:5] != new['fields'][2:5]
                or old['metadata'].get('replacement_claim_digest') != edge['claim_digest']):
            raise R.DC.DispatchContractError('replacement-subsession-scope-mismatch')
        mapping[original] = target
        consumed.add(original)
    return [row for row in rows if row['metadata'].get('attempt_id') not in consumed], mapping
