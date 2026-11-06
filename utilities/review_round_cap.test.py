#!/usr/bin/env python3
"""SD-153: the one round-budget admission/closure decision.

`classify_round_row`/`round_budget`/`RoundBudget` replace the three separate
`len(prior)+1 > max_round` comparisons (dispatch-node.py, dispatch-batch.py,
stage-dispatch-fallback.py) and the `len(terminated) < max_round` check
`capability-route.py`'s `_owner_closure_eligibility` used to keep. A round
with no verdict at all (a crashed or capacity-dead attempt, never a PASS/
FAIL/blocking review) does not spend the exhaustion budget by itself; two of
them in a row bind the node to the sealed fallback chain's remaining
unregistered hop instead (`VERDICTLESS_BOUND`).
"""
import base64
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import review_round_cap as CAP  # noqa: E402
import route_authority as AUTH  # noqa: E402


def route(effective_intensity="standard", cap=None):
    r = {"effective_intensity": effective_intensity, "capability": "autopilot-code"}
    if cap is not None:
        r["continuation_budget"] = {"review_round_cap": cap}
    return r


def review_node():
    return {"id": "plan-check", "kind": "review-worker"}


def stage_node():
    return {"id": "execute", "kind": "pipeline-stage"}


def row(status, note, *, worker_type="review", failure_class=None, extra=None):
    meta = {"note": note, "worker_type": worker_type}
    if failure_class is not None:
        meta["failure_class"] = failure_class
    if extra:
        meta.update(extra)
    return (status, meta)


class ClassifyRoundRowTest(unittest.TestCase):
    def test_open_or_running_is_live_regardless_of_note(self):
        self.assertEqual(CAP.classify_round_row("open", {"note": "anything"}, worker_type="review"), "live")
        self.assertEqual(CAP.classify_round_row("running", {}, worker_type="test"), "live")

    def test_completion_deferred_pending_is_unsettled(self):
        meta = {"classifier_source": "registered-wrapper-completion-transient-v1"}
        self.assertEqual(CAP.classify_round_row("done", meta, worker_type="review"), "unsettled")

    def test_terminal_conflict_pending_is_unsettled(self):
        meta = {"terminal_conflict": "1", "conflicting_terminal_note": "x",
                "conflicting_failure_class": "y", "conflicting_classifier_source": "z"}
        self.assertEqual(CAP.classify_round_row("done", meta, worker_type="review"), "unsettled")

    def test_success_note_is_a_verdict(self):
        self.assertEqual(CAP.classify_round_row("done", {"note": "completed-marker"}, worker_type="review"), "verdict")

    def test_completed_review_blocking_is_a_verdict(self):
        self.assertEqual(
            CAP.classify_round_row("done", {"note": "completed-review-blocking"}, worker_type="review"),
            "verdict",
        )

    def test_review_worker_crash_is_verdict_less_even_with_failure_class_fail(self):
        # A review worker that crashed never produced a review verdict --
        # unlike a non-review worker's genuine FAIL, this never counts.
        meta = {"note": "dead-worker-fail", "failure_class": "fail"}
        self.assertEqual(CAP.classify_round_row("done", meta, worker_type="review"), "verdict-less")

    def test_non_review_worker_fail_with_failure_class_fail_is_a_verdict(self):
        meta = {"note": "dead-worker-fail", "failure_class": "fail"}
        self.assertEqual(CAP.classify_round_row("done", meta, worker_type="test"), "verdict")

    def test_non_review_worker_fail_without_failure_class_fail_is_verdict_less(self):
        meta = {"note": "dead-worker-fail"}
        self.assertEqual(CAP.classify_round_row("done", meta, worker_type="test"), "verdict-less")

    def test_dead_invalid_envelope_is_always_verdict_less(self):
        meta = {"note": "dead-invalid-envelope", "failure_class": "fail"}
        self.assertEqual(CAP.classify_round_row("done", meta, worker_type="test"), "verdict-less")

    def test_dead_capacity_is_verdict_less(self):
        meta = {"note": "dead-capacity"}
        self.assertEqual(CAP.classify_round_row("done", meta, worker_type="review"), "verdict-less")

    def test_killed_or_cancelled_status_is_verdict_less(self):
        self.assertEqual(CAP.classify_round_row("killed", {"note": "dead-worker-fail"}, worker_type="test"),
                          "verdict-less")
        self.assertEqual(CAP.classify_round_row("cancelled", {}, worker_type="review"), "verdict-less")


class RoundBudgetTest(unittest.TestCase):
    def test_no_start_census_retains_pending_and_reviewed_terminal_conflicts(self):
        import json
        for reviewed in (False, True):
            history = {"late-result": {"note": "completed-marker", "failure_class": "pass"}}
            if reviewed:
                history["late-result"]["review_sha256"] = "f" * 64
            metadata = {"launch_outcome": "never-launched", "launch_claimed": "0",
                        "terminal_conflict": "1", "terminal_conflicts_b64":
                        base64.b64encode(json.dumps(history).encode()).decode()}
            rows = [row("done", "dead-capacity", failure_class="capacity", extra={
                **metadata, "attempt_id": f"att-conflict-{index}"}) for index in (1, 2)]
            physical = [(["now", status, "/repo", "/wt", "review", ""], meta)
                        for status, meta in rows]
            self.assertEqual(CAP.logical_round_records(physical), physical)
            expected = "verdictless-bound" if reviewed else "blocked-unsettled"
            self.assertEqual(CAP.round_budget(route(), review_node(), rows).state, expected)
            self.assertEqual(AUTH.decide_continuation(
                route(), action="round", node=review_node(), rows=rows).budget.state, expected)

    def test_owner_closed_fail_gets_one_approved_closure_without_becoming_pass(self):
        closed = row('done', 'completed-marker', failure_class='fail', extra={
            'attempt_id': 'att-review-2', 'review_gate_closure': 'owner-closure',
            'review_independence': 'owner-overridden'})
        rows = [row('done', 'completed-review-blocking', extra={'attempt_id': 'att-review-1'}), closed]
        revisions = [{'answers': ['att-review-2']}]
        self.assertTrue(CAP.last_verdict_blocking(rows, 'review'))
        self.assertTrue(CAP.gate_unmet(rows, 'review'))
        self.assertEqual(CAP.round_budget(route(), review_node(), rows).state, 'exhausted')
        budget = CAP.round_budget(route(), review_node(), rows, revisions=revisions)
        self.assertEqual((budget.state, budget.round_kind, budget.verdict_rounds),
                         ('admit', 'closure-check', 2))
        self.assertEqual(CAP.marker_round_census(route(), review_node(), rows,
                         site='registered', revisions=revisions)['closure_class'], 'closure-check')
        for note in ('completed-review-blocking', 'completed-marker'):
            later = row('done', note, failure_class='fail' if note.endswith('blocking') else 'pass',
                        extra={'attempt_id': 'att-review-3'})
            self.assertEqual(CAP.round_budget(route(), review_node(), rows + [later],
                                             revisions=revisions).state, 'exhausted')
        for delta in ({'failure_class': 'pass'}, {'worker_type': 'stage'}, {'review_independence': 'independent'},
                      {'review_gate_closure': ''}):
            ordinary = (closed[0], {**closed[1], **delta})
            self.assertFalse(CAP.last_verdict_blocking([ordinary], 'review'))
            self.assertEqual(CAP.round_budget(route(), review_node(), rows[:1] + [ordinary],
                                             revisions=revisions).state, 'exhausted')
        for status, expected in (('open', 'blocked-live'), ('done', 'blocked-unsettled')):
            tail = row(status, '', extra={'terminal_conflict': '1',
                                         'conflicting_terminal_note': 'x'})
            self.assertEqual(CAP.round_budget(route(), review_node(), rows + [tail],
                                             revisions=revisions).state, expected)

    def test_superseded_pass_gets_one_recheck_within_ceiling(self):
        rows = [row('done', 'completed-review-blocking', extra={'attempt_id': 'att-review-1'}),
                row('done', 'completed-marker', extra={'attempt_id': 'att-review-2'})]
        reopened = [{'supersedes': ['att-review-2']}]
        self.assertEqual(CAP.round_budget(route(), review_node(), rows).state, 'exhausted')
        for other in ([{'supersedes': ['att-review-1']}], [{'answers': ['att-review-2']}]):
            self.assertEqual(CAP.round_budget(route(), review_node(), rows, revisions=other).state, 'exhausted')
        budget = CAP.round_budget(route(), review_node(), rows, revisions=reopened)
        self.assertEqual((budget.state, budget.round_kind, budget.verdict_rounds),
                         ('admit', 'closure-check', 2))
        for note in ('completed-review-blocking', 'completed-marker'):
            later = row('done', note, extra={'attempt_id': 'att-review-3'})
            self.assertEqual(CAP.round_budget(route(), review_node(), rows + [later],
                                             revisions=reopened + [{'supersedes': ['att-review-3']}]).state,
                             'exhausted')

    def test_source_bound_revision_closure_stays_inside_its_subject_cap(self):
        node = {"id": "impl-review", "kind": "review-worker", "unit": "qa/code-review",
                "completion_gate": "code-impl-review"}
        facts = {"source_subject_digest": "subject-A"}
        for note, failure_class, revision_key in (
                ("completed-marker", "pass", "supersedes"),
                ("completed-review-blocking", "fail", "answers")):
            rows = [row("done", note, failure_class=failure_class, extra={
                "attempt_id": f"att-{note}-{number}", "review_subject_digest": "subject-A"})
                for number in (1, 2)]
            decision = AUTH.decide_continuation(
                route(cap=2), action="round", node=node, rows=rows,
                revisions=[{revision_key: [rows[-1][1]["attempt_id"]]}], facts=facts,
            )
            self.assertEqual((decision.budget.state, decision.budget.verdict_rounds), ("exhausted", 2))
            self.assertFalse(decision.budget.closure_check_used)

        one_pass = [row("done", "completed-marker", extra={
            "attempt_id": "att-subject-A-pass-1", "review_subject_digest": "subject-A"})]
        within_cap = AUTH.decide_continuation(
            route(cap=2), action="round", node=node, rows=one_pass,
            revisions=[{"supersedes": ["att-subject-A-pass-1"]}], facts=facts,
        ).budget
        self.assertEqual((within_cap.state, within_cap.round_kind, within_cap.verdict_rounds),
                         ("admit", "closure-check", 1))

    def test_corrected_subject_after_blocking_review_is_a_closure_check_in_its_own_budget(self):
        node = {"id": "impl-review", "kind": "review-worker", "unit": "qa/code-review",
                "completion_gate": "code-impl-review"}
        corrected = {"source_subject_digest": "subject-B"}
        blocked_a = row("done", "completed-review-blocking", failure_class="fail", extra={
            "attempt_id": "att-review-A", "review_subject_digest": "subject-A"})
        budget = AUTH.decide_continuation(route(cap=2), action="round", node=node,
                                          rows=[blocked_a], facts=corrected).budget
        # The corrected content keeps its own verdict budget; only the round
        # kind follows the lineage's open blocking verdict.
        self.assertEqual((budget.state, budget.verdict_rounds, budget.round_kind),
                         ("admit", 0, "closure-check"))
        self.assertFalse(budget.closure_check_used)
        crashed = row("done", "dead-invalid-envelope", extra={
            "attempt_id": "att-review-B-crash", "review_subject_digest": "subject-B"})
        self.assertEqual(AUTH.decide_continuation(route(cap=2), action="round", node=node,
            rows=[blocked_a, crashed], facts=corrected).budget.round_kind, "closure-check")
        # Two blocking corrections in a row never exhaust the next subject.
        blocked_b = row("done", "completed-review-blocking", failure_class="fail", extra={
            "attempt_id": "att-review-B", "review_subject_digest": "subject-B"})
        third = AUTH.decide_continuation(route(cap=2), action="round", node=node,
            rows=[blocked_a, blocked_b], facts={"source_subject_digest": "subject-C"}).budget
        self.assertEqual((third.state, third.verdict_rounds, third.round_kind),
                         ("admit", 0, "closure-check"))

        # A list the owner already disposed by owner-closure is not open: the
        # corrected subject gets an independent first review again.
        disposed_a = row("done", "completed-marker", failure_class="fail", extra={
            "attempt_id": "att-review-A-closed", "review_subject_digest": "subject-A",
            "review_gate_closure": "owner-closure", "review_independence": "owner-overridden"})
        self.assertTrue(CAP.last_verdict_blocking([blocked_a, disposed_a], "review"))
        self.assertIsNone(CAP.open_blocking_verdict([blocked_a, disposed_a], "review"))
        self.assertEqual(AUTH.decide_continuation(route(cap=2), action="round", node=node,
            rows=[blocked_a, disposed_a], facts=corrected).budget.round_kind, "first")
        self.assertEqual(CAP.open_blocking_verdict([blocked_a, crashed], "review"), blocked_a[1])

        passed_a = row("done", "completed-marker", extra={
            "attempt_id": "att-review-A-pass", "review_subject_digest": "subject-A"})
        after_pass = AUTH.decide_continuation(route(cap=2), action="round", node=node,
                                              rows=[blocked_a, passed_a], facts=corrected).budget
        self.assertEqual((after_pass.state, after_pass.round_kind), ("admit", "first"))
        crash_only = AUTH.decide_continuation(route(cap=2), action="round", node=node,
                                              rows=[crashed], facts=corrected).budget
        self.assertEqual(crash_only.round_kind, "first")

        # A source-bound test reruns its whole check on changed content.
        test_node = {"id": "test", "kind": "pipeline-stage", "unit": "qa/test",
                     "completion_gate": "code-test"}
        failed_test = row("done", "dead-worker-fail", worker_type="stage", failure_class="fail",
                          extra={"attempt_id": "att-test-A", "review_subject_digest": "subject-A"})
        self.assertEqual(AUTH.decide_continuation(route(cap=2), action="round", node=test_node,
            rows=[failed_test], facts=corrected).budget.round_kind, "first")

    def test_legacy_non_source_review_and_test_keep_cap_plus_one_closure(self):
        for node, worker_type in ((review_node(), "review"), (stage_node(), "test")):
            rows = [row("done", "completed-marker", worker_type=worker_type,
                        extra={"attempt_id": f"att-legacy-{worker_type}-{number}"})
                    for number in (1, 2)]
            budget = AUTH.decide_continuation(
                route(cap=2), action="round", node=node, rows=rows,
                revisions=[{"supersedes": [rows[-1][1]["attempt_id"]]}],
            ).budget
            self.assertEqual((budget.state, budget.round_kind, budget.verdict_rounds),
                             ("admit", "closure-check", 2))

    def test_sd161_parallel_review_legs_keep_capped_identity_without_broadening_other_nodes(self):
        for node_id in CAP.ROUND_CAPPED_NODE_IDS:
            self.assertTrue(CAP.is_round_capped_node({"id": node_id}))
        for node_id in ("plan-check", "plan-check-alternative", "plan-check-replica-2"):
            node = {"id": node_id, "kind": "review-worker", "unit": "qa/plan-review"}
            self.assertTrue(CAP.is_round_capped_node(node))
            rows = [row("done", "completed-review-blocking", extra={"attempt_id": f"att-{i}"}) for i in (1, 2)]
            self.assertEqual(CAP.round_budget(route(), node, rows).state, "exhausted")
        self.assertFalse(CAP.is_round_capped_node({"id": "custom-review", "kind": "review-worker", "unit": "qa/custom"}))
        self.assertFalse(CAP.is_round_capped_node({"id": "custom-stage", "kind": "pipeline-stage", "unit": "qa/plan-review"}))

    def test_sd161_within_cap_revision_does_not_spend_extra_verdict(self):
        rows = []
        revisions = []
        for number in (1, 2, 3):
            attempt = f"att-review-{number}"
            rows.append(row("done", "completed-review-blocking", extra={
                "attempt_id": attempt, "round_kind": "closure-check"}))
            revisions.append({"answers": [attempt]})
            budget = CAP.round_budget(route(cap=2), review_node(), rows, revisions=revisions)
            self.assertEqual(budget.state, "admit" if number < 3 else "exhausted")
            self.assertEqual(budget.verdict_rounds, number)

    def test_sd161_verdictless_tail_keeps_revision_but_bound_and_live_win(self):
        rows = [row("done", "completed-review-blocking", extra={"attempt_id": f"att-r{i}"})
                for i in (1, 2)]
        revisions = [{"answers": ["att-r2"]}]
        rows.append(row("dead", "dead-worker-silent-exit"))
        self.assertEqual(CAP.round_budget(route(), review_node(), rows, revisions=revisions).state, "admit")
        for status, note, expected in (("dead", "dead-worker-silent-exit", "verdictless-bound"),
                                       ("open", "", "blocked-live")):
            self.assertEqual(CAP.round_budget(route(), review_node(), rows + [row(status, note)],
                                             revisions=revisions).state, expected)

    def test_empty_history_is_admit_at_round_one(self):
        budget = CAP.round_budget(route(cap=2), review_node(), [])
        self.assertEqual(budget.state, "admit")
        self.assertEqual(budget.next_round, 1)
        self.assertEqual(budget.correction_round, 0)
        self.assertEqual(budget.round_kind, "first")
        self.assertEqual(budget.verdict_rounds, 0)
        self.assertEqual(budget.cap, 2)

    def test_verdictless_attempts_do_not_turn_the_first_review_into_a_correction(self):
        for note, failure_class in (("dead-worker-blocked", "blocked"),
                                    ("dead-invalid-envelope", "contract"),
                                    ("dead-capacity", "capacity")):
            with self.subTest(note=note):
                budget = CAP.round_budget(
                    route("standard", cap=2), review_node(),
                    [row("done", note, failure_class=failure_class,
                         extra={"attempt_id": "att-" + note})],
                )
                self.assertEqual((budget.state, budget.verdict_rounds,
                                  budget.round_kind, budget.next_round),
                                 ("admit", 0, "first", 2))

        # A verdict remains the boundary: a later dead attempt does not erase
        # the correction obligation established by the earlier blocking review.
        budget = CAP.round_budget(
            route("standard", cap=2), review_node(),
            [row("done", "completed-review-blocking", extra={"attempt_id": "att-block"}),
             row("done", "dead-capacity", failure_class="capacity",
                 extra={"attempt_id": "att-capacity"})],
        )
        self.assertEqual((budget.state, budget.verdict_rounds, budget.round_kind),
                         ("admit", 1, "correction"))

    def test_a_sd153_1_envelope_death_then_fail_admits_round_three(self):
        # A-SD153-1 (test node): r1 dead-invalid-envelope (verdict-less), r2
        # dead-worker-fail/failure_class=fail (a real FAIL verdict) -- only
        # r2 spends the budget, so a standard cap of 2 still admits round 3.
        rows = [
            row("done", "dead-invalid-envelope", worker_type="test", failure_class="fail"),
            row("done", "dead-worker-fail", worker_type="test", failure_class="fail"),
        ]
        budget = CAP.round_budget(route("standard"), stage_node(), rows)
        self.assertEqual(budget.state, "admit")
        self.assertEqual(budget.verdict_rounds, 1)
        self.assertEqual(budget.next_round, 3)
        self.assertEqual(budget.correction_round, 3)
        self.assertEqual(budget.round_kind, "correction")

    def test_rows_a_person_stopped_are_not_verdictless_rounds(self):
        # RV-BC-1179 🟡7: two review workers a person stopped (close or Fleet) did
        # not fail to produce a result; they must not bind the node.
        stopped = [row("done", "fleet-kill", failure_class="cancelled"),
                   row("done", "fleet-kill", failure_class="cancelled")]
        budget = CAP.round_budget(route("standard", cap=2), review_node(), stopped)
        self.assertEqual((budget.state, budget.verdictless_streak), ("admit", 0))
        mixed = [row("done", "dead-capacity"), row("done", "fleet-kill", failure_class="cancelled"),
                 row("done", "dead-invalid-envelope")]
        budget = CAP.round_budget(route("standard", cap=2), review_node(), mixed)
        self.assertEqual((budget.state, budget.verdictless_streak), ("verdictless-bound", 2))

    def test_a_sd153_2_two_blocking_rounds_exhaust_a_strong_cap(self):
        rows = [row("done", "completed-review-blocking"), row("done", "completed-review-blocking")]
        budget = CAP.round_budget(route("strong", cap=2), review_node(), rows)
        self.assertEqual(budget.state, "exhausted")
        self.assertEqual(budget.verdict_rounds, 2)
        self.assertEqual(budget.next_round, 3)

    def test_a_sd153_3_two_verdictless_rounds_in_a_row_bind_the_node(self):
        rows = [
            row("done", "dead-capacity"),
            row("done", "dead-invalid-envelope"),
        ]
        budget = CAP.round_budget(route("standard", cap=2), review_node(), rows)
        self.assertEqual(budget.state, "verdictless-bound")
        self.assertEqual(budget.verdict_rounds, 0)
        self.assertEqual(budget.verdictless_streak, 2)
        self.assertIn(budget.next_action, ("native-subagent", "owner-inline"))

    def test_verdictless_streak_resets_after_a_verdict(self):
        rows = [
            row("done", "dead-capacity"),
            row("done", "completed-review-blocking"),
            row("done", "dead-invalid-envelope"),
        ]
        budget = CAP.round_budget(route("thorough", cap=3), review_node(), rows)
        self.assertEqual(budget.verdictless_streak, 1)
        self.assertEqual(budget.verdict_rounds, 1)
        self.assertEqual(budget.state, "admit")

    def test_content_changes_do_not_restart_the_verdictless_lineage_bound(self):
        node = {"id": "post-deploy-verify", "kind": "review-worker", "unit": "qa/test"}
        rows = [row("done", "dead-worker-blocked", failure_class="blocked",
                    extra={"attempt_id": f"att-blocked-{index}",
                           "_review_subject_digest": str(index) * 64}) for index in (1, 2)]
        budget = CAP.round_budget(route("standard", cap=2), node, rows,
                                  source_subject_digest="3" * 64)
        self.assertEqual((budget.state, budget.verdictless_streak, budget.verdict_rounds),
                         ("verdictless-bound", 2, 0))

    def test_a_sd153_6_live_and_unsettled_rows_block_admission(self):
        live = [row("open", "")]
        unsettled = [(("done"), {"classifier_source": "registered-wrapper-completion-transient-v1"})]
        self.assertEqual(CAP.round_budget(route("standard", cap=2), review_node(), live).state, "blocked-live")
        self.assertEqual(
            CAP.round_budget(route("standard", cap=2), review_node(), unsettled).state, "blocked-unsettled"
        )

    def test_a_sd153_7_sealed_cap_wins_over_effective_intensity(self):
        # A route's sealed `continuation_budget.review_round_cap` is durable:
        # a legacy-shaped route (no such field) still re-derives from
        # `effective_intensity`, but a sealed one is authoritative even if
        # `effective_intensity` is later mutated to something unknown.
        sealed = route("mythic", cap=2)
        budget = CAP.round_budget(sealed, review_node(), [])
        self.assertEqual(budget.cap, 2)
        with self.assertRaises(ValueError):
            CAP.round_budget(route("mythic"), review_node(), [])

    def test_legacy_route_without_continuation_budget_derives_from_intensity(self):
        budget = CAP.round_budget(route("thorough"), review_node(), [])
        self.assertEqual(budget.cap, 3)

    def test_next_action_by_state(self):
        self.assertEqual(CAP.round_budget(route("standard", cap=2), review_node(), []).next_action, "dispatch")
        exhausted = CAP.round_budget(
            route("standard", cap=1), review_node(), [row("done", "completed-review-blocking")]
        )
        self.assertEqual(exhausted.next_action, "owner-report")


class RecoveryFieldsTest(unittest.TestCase):
    def test_owner_closed_review_does_not_repeat_its_completion_commands(self):
        r = {**route(), 'route_id': 'rt-test', 'route_hash': 'sha256:test'}
        node = review_node()
        r['nodes'] = [node]
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                'route_lineage.verified_route_lineage', return_value=[r]):
            r['artifact_root'] = directory
            jobs = Path(directory)/'jobs.log'
            review_artifact = Path(directory)/'review.md'
            review_artifact.write_text('review findings', encoding='utf-8')
            encoded = base64.urlsafe_b64encode(str(review_artifact).encode()).decode().rstrip('=')
            for latest_note in ('completed-review-blocking', 'completed-marker'):
                rows = []
                for index, note in enumerate(('completed-review-blocking', latest_note), 1):
                    metadata = {'route_id': 'rt-test', 'route_hash': 'sha256:test',
                                'route_node': node['id'], 'attempt_id': f'att-review-{index}',
                                'worker_type': 'review', 'note': note,
                                'failure_class': 'fail' if note == 'completed-review-blocking' else 'pass',
                                'review_artifact_b64': encoded}
                    if note == 'completed-marker':
                        metadata.update(review_gate_closure='owner-closure',
                                        review_independence='owner-overridden')
                    rows.append('now\tdone\twt\troot\ttask\t' + ','.join(
                        f'{key}={value}' for key, value in metadata.items()))
                jobs.write_text('\n'.join(rows) + '\n', encoding='utf-8')
                with mock.patch('dispatch_contract.observe_terminal_review_failure', return_value={}):
                    fields = CAP.recovery_fields('review-worker', route=r, node=node, jobs=jobs,
                                                 route_file=Path(directory)/'route.json')
                expected = latest_note == 'completed-review-blocking'
                if expected:
                    self.assertEqual(fields['next_action'], 'owner-closure')
                else:
                    self.assertNotIn('next_action', fields)
                for key in ('recovery_complete_command', 'recovery_check_command'):
                    self.assertEqual(key in fields, expected)

    def test_all_pass_exhaustion_has_no_owner_closure_surface(self):
        fields = CAP.recovery_fields("review-worker")
        self.assertEqual(fields["recovery_surface"], "owner-report")
        self.assertEqual(fields["required_action"], "report-unresolved-verification")
        self.assertNotIn("next_action", fields)

    def test_blocking_review_without_sealed_artifact_does_not_advertise_closure(self):
        r = {**route(), 'route_id': 'rt-no-artifact', 'route_hash': 'sha256:no-artifact'}
        node = review_node()
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                'route_lineage.verified_route_lineage', return_value=[r]):
            r['artifact_root'] = directory
            jobs = Path(directory)/'jobs.log'
            rows = []
            for index in (1, 2):
                metadata = {'route_id': r['route_id'], 'route_hash': r['route_hash'],
                            'route_node': node['id'], 'attempt_id': f'att-review-{index}',
                            'worker_type': 'review', 'note': 'completed-review-blocking'}
                rows.append('now\tdone\twt\troot\ttask\t' + ','.join(
                    f'{key}={value}' for key, value in metadata.items()))
            jobs.write_text('\n'.join(rows) + '\n', encoding='utf-8')
            fields = CAP.recovery_fields('review-worker', route=r, node=node, jobs=jobs,
                                         route_file=Path(directory)/'route.json')
            self.assertEqual(fields['recovery_surface'], 'owner-report')
            self.assertNotIn('recovery_complete_command', fields)
            self.assertNotIn('recovery_check_command', fields)

    def test_exhausted_non_review_names_existing_inline_completion(self):
        fields = CAP.recovery_fields("pipeline-stage")
        self.assertEqual(fields["recovery_surface"], "capability-route complete --inline")

    def test_verdictless_bound_review_worker_names_native_subagent(self):
        fields = CAP.recovery_fields("review-worker", "verdictless-bound")
        self.assertEqual(fields["next_action"], "native-subagent")
        self.assertEqual(fields["recovery_owner"], "owner")

    def test_verdictless_bound_non_review_names_owner_inline(self):
        fields = CAP.recovery_fields("pipeline-stage", "verdictless-bound")
        self.assertEqual(fields["next_action"], "owner-inline")


class RoundBudgetParityTest(unittest.TestCase):
    """A-SD153-7: every registered launch surface reads the exact same
    `round_budget` function object, so admission cannot silently diverge
    between dispatch-node.py, dispatch-batch.py and stage-dispatch-fallback.py."""

    def _load(self, name, filename):
        spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_sd153_7_twelve_registries_same_budget_on_every_surface(self):
        dispatch_node = self._load("review_round_cap_parity_dispatch_node", "dispatch-node.py")
        dispatch_batch = self._load("review_round_cap_parity_dispatch_batch", "dispatch-batch.py")
        stage_fallback = self._load("review_round_cap_parity_stage_fallback", "stage-dispatch-fallback.py")
        # `review_round_cap` is a normal (cached) import in every module, so
        # this identity holds even though each surface below is loaded fresh
        # by file path (`dispatch-node.py` cannot be `import`ed by its
        # hyphenated name) -- the one place that could silently diverge is
        # exactly the leaf this proves is shared.
        self.assertIs(dispatch_node.REVIEW_ROUND_CAP.round_budget, CAP.round_budget)
        self.assertIs(dispatch_batch.DISPATCH_NODE.REVIEW_ROUND_CAP.round_budget, CAP.round_budget)
        self.assertIs(stage_fallback.DISPATCH_NODE.REVIEW_ROUND_CAP.round_budget, CAP.round_budget)
        self.assertTrue(hasattr(dispatch_node, "admit_round"))
        self.assertTrue(hasattr(dispatch_batch.DISPATCH_NODE, "admit_round"))
        self.assertTrue(hasattr(stage_fallback.DISPATCH_NODE, "admit_round"))

        combinations = [
            ("live", [row("open", "")]),
            ("live-among-verdicts", [row("done", "completed-review-blocking"), row("open", "")]),
            ("unsettled", [(("done"), {"classifier_source": "registered-wrapper-completion-transient-v1"})]),
            ("unsettled-among-verdicts",
             [row("done", "completed-review-blocking"),
              (("done"), {"terminal_conflict": "1", "conflicting_terminal_note": "x",
                          "conflicting_failure_class": "y", "conflicting_classifier_source": "z"})]),
            ("admit-empty", []),
            ("admit-one-verdict", [row("done", "completed-review-blocking")]),
            ("exhausted-two-verdicts",
             [row("done", "completed-review-blocking"), row("done", "completed-review-blocking")]),
            ("verdictless-bound-two-crashes", [row("done", "dead-capacity"), row("done", "dead-invalid-envelope")]),
            ("verdict-then-crash", [row("done", "completed-review-blocking"), row("done", "dead-capacity")]),
            ("crash-then-verdict", [row("done", "dead-capacity"), row("done", "completed-review-blocking")]),
            ("three-crashes-still-bound",
             [row("done", "dead-capacity"), row("done", "dead-invalid-envelope"), row("done", "dead-capacity")]),
            ("blocking-then-fail-review",
             [row("done", "completed-review-blocking"),
              row("done", "dead-worker-fail", failure_class="fail")]),
        ]
        self.assertEqual(len(combinations), 12)
        for label, rows in combinations:
            with self.subTest(registry=label):
                node_budget = dispatch_node.REVIEW_ROUND_CAP.round_budget(route("standard", cap=2), review_node(), rows)
                batch_budget = dispatch_batch.DISPATCH_NODE.REVIEW_ROUND_CAP.round_budget(
                    route("standard", cap=2), review_node(), rows)
                fallback_budget = stage_fallback.DISPATCH_NODE.REVIEW_ROUND_CAP.round_budget(
                    route("standard", cap=2), review_node(), rows)
                self.assertEqual(node_budget, batch_budget)
                self.assertEqual(node_budget, fallback_budget)


class RoundProtocolParityTest(unittest.TestCase):
    """Plan-correction B2: one `round_protocol_block` producer, attached
    identically regardless of which launch surface calls it."""

    def test_verdictless_row_renders_as_no_verdict_history(self):
        dispatch_node_spec = importlib.util.spec_from_file_location(
            "review_round_cap_protocol_dispatch_node", ROOT / "utilities/dispatch-node.py")
        dispatch_node = importlib.util.module_from_spec(dispatch_node_spec)
        dispatch_node_spec.loader.exec_module(dispatch_node)
        cols = ["2026-09-26T00:00:00Z", "done", "/repo", "/wt", "slug-r1"]
        meta = {"note": "dead-invalid-envelope"}
        rows = [(cols, meta)]
        budget = CAP.round_budget(route("standard", cap=2), review_node(), [("done", meta)])
        block = dispatch_node.round_protocol_block(budget, rows, "review", "plan-check")
        self.assertIn("판정 없음(dead-invalid-envelope)", block)

    def test_first_retry_keeps_shared_history_and_uses_worker_role_guidance(self):
        dispatch_node_spec = importlib.util.spec_from_file_location(
            "review_round_cap_protocol_first_retry", ROOT / "utilities/dispatch-node.py")
        dispatch_node = importlib.util.module_from_spec(dispatch_node_spec)
        dispatch_node_spec.loader.exec_module(dispatch_node)
        cols = ["2026-09-26T00:00:00Z", "done", "/repo", "/wt", "slug-r1"]

        for node, worker_type in ((review_node(), "review"), (stage_node(), "stage")):
            with self.subTest(worker_type=worker_type):
                meta = {"note": "dead-launch-exit-1", "failure_class": "launch",
                        "worker_type": worker_type}
                rows = [(cols, meta)]
                budget = CAP.round_budget(route("standard", cap=2), node, [("done", meta)])
                block = dispatch_node.round_protocol_block(budget, rows, worker_type, node["id"])
                self.assertIn(f"Round protocol (round 2 of route node `{node['id']}`", block)
                self.assertIn("slug-r1 (판정 없음(dead-launch-exit-1))", block)
                self.assertIn("verdict-less attempts provide execution context only", block)
                self.assertNotIn("This is a correction round", block)
                if worker_type == "review":
                    self.assertIn("independent first review", block)
                    self.assertIn("no blocking verdict is open in this node's lineage", block)
                    self.assertIn("Front-load every blocker", block)
                    self.assertNotIn("full execution of the original assigned task", block)
                else:
                    self.assertIn("full execution of the original assigned task and scope", block)
                    self.assertNotIn("independent first review", block)

    def test_closure_materials_skip_a_disposed_list_and_name_an_unrecorded_delta(self):
        dispatch_node_spec = importlib.util.spec_from_file_location(
            "review_round_cap_closure_materials", ROOT / "utilities/dispatch-node.py")
        dispatch_node = importlib.util.module_from_spec(dispatch_node_spec)
        dispatch_node_spec.loader.exec_module(dispatch_node)
        cols = ["2026-11-02T00:00:00Z", "done", "/repo", "/wt", "slug-r1"]
        blocked = {"note": "completed-review-blocking", "attempt_id": "att-review-1", "worker_type": "review"}
        closed = {"note": "completed-marker", "failure_class": "fail", "attempt_id": "att-review-2",
                  "worker_type": "review", "review_gate_closure": "owner-closure",
                  "review_independence": "owner-overridden"}
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            self.assertEqual(dispatch_node.closure_materials(
                [(cols, blocked), (cols, closed)], "review", jobs=jobs, current_manifest=()), "")
            unrecorded = dispatch_node.closure_materials([(cols, blocked)], "review", jobs=jobs,
                                                         current_manifest=())
        self.assertIn("Delta since that review: not recorded for that attempt", unrecorded)

    def test_first_round_renders_no_block(self):
        dispatch_node_spec = importlib.util.spec_from_file_location(
            "review_round_cap_protocol_first_round", ROOT / "utilities/dispatch-node.py")
        dispatch_node = importlib.util.module_from_spec(dispatch_node_spec)
        dispatch_node_spec.loader.exec_module(dispatch_node)
        budget = CAP.round_budget(route("standard", cap=2), review_node(), [])
        self.assertEqual(dispatch_node.round_protocol_block(budget, [], "review", "plan-check"), "")


if __name__ == "__main__":
    unittest.main()
