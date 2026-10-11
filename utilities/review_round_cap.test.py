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
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import review_round_cap as CAP  # noqa: E402


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
        self.assertEqual(exhausted.next_action, "owner-closure")


class RecoveryFieldsTest(unittest.TestCase):
    def test_owner_closed_review_does_not_repeat_its_completion_commands(self):
        r = {**route(), 'route_id': 'rt-test', 'route_hash': 'sha256:test'}
        node = review_node()
        with tempfile.TemporaryDirectory() as directory, mock.patch(
                'route_lineage.verified_route_lineage', return_value=[r]):
            jobs = Path(directory)/'jobs.log'
            r['artifact_root'] = directory
            for note in ('completed-review-blocking', 'completed-marker'):
                metadata = {'route_id': 'rt-test', 'route_hash': 'sha256:test', 'route_node': node['id'],
                            'attempt_id': 'att-review-2', 'worker_type': 'review', 'note': note,
                            'failure_class': 'fail', 'review_gate_closure': 'owner-closure',
                            'review_independence': 'owner-overridden'}
                jobs.write_text('now\tdone\twt\troot\ttask\t' + ','.join(
                    f'{key}={value}' for key, value in metadata.items()) + '\n')
                fields = CAP.recovery_fields('review-worker', route=r, node=node, jobs=jobs,
                                             route_file=Path(directory)/'route.json')
                for key in ('recovery_complete_command', 'recovery_check_command'):
                    self.assertEqual(key in fields, note == 'completed-review-blocking')

    def test_exhausted_review_worker_points_at_owner_closure(self):
        fields = CAP.recovery_fields("review-worker")
        self.assertEqual(fields["recovery_surface"], "capability-route complete")
        self.assertEqual(fields["required_action"], "resolve-review-findings")

    def test_exhausted_non_review_points_at_continuation(self):
        fields = CAP.recovery_fields("pipeline-stage")
        self.assertEqual(fields["recovery_surface"], "capability-route continuation")

    def test_verdictless_bound_review_worker_names_native_subagent(self):
        fields = CAP.recovery_fields("review-worker", "verdictless-bound")
        self.assertEqual(fields["next_action"], "native-subagent")
        self.assertEqual(fields["recovery_owner"], "owner")

    def test_verdictless_bound_non_review_names_owner_inline(self):
        fields = CAP.recovery_fields("pipeline-stage", "verdictless-bound")
        self.assertEqual(fields["next_action"], "owner-inline")
        self.assertEqual(fields["recovery_surface"], "capability-route complete --inline")
        self.assertIn("checks first", fields["recovery_hint"])


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

    def test_first_round_renders_no_block(self):
        dispatch_node_spec = importlib.util.spec_from_file_location(
            "review_round_cap_protocol_first_round", ROOT / "utilities/dispatch-node.py")
        dispatch_node = importlib.util.module_from_spec(dispatch_node_spec)
        dispatch_node_spec.loader.exec_module(dispatch_node)
        budget = CAP.round_budget(route("standard", cap=2), review_node(), [])
        self.assertEqual(dispatch_node.round_protocol_block(budget, [], "review", "plan-check"), "")


if __name__ == "__main__":
    unittest.main()
