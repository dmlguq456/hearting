import tempfile
from pathlib import Path
import unittest

from tools.fleet.collectors import dispatch


CURRENT = (
    "attempt_schema_version=2,transport=headless,"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless"
)


def row(stamp, status, slug, attempt, *, dispatch_depth=1,
        intensity="quick", parent="", unit="", route_node="one-shot",
        worker_type="", fallback_hop="same-harness-headless"):
    pipe = (
        f"{CURRENT},dispatch_depth={dispatch_depth},"
        "capability=autopilot-code,"
        f"intensity={intensity},harness=codex,route_id=rt-v20,"
        f"route_node={route_node},attempt_id={attempt}"
        + (f",parent={parent}" if parent else "")
        + (f",unit={unit}" if unit else "")
        + (f",worker_type={worker_type}" if worker_type else "")
    )
    if fallback_hop != "same-harness-headless":
        pipe = pipe.replace("fallback_hop=same-harness-headless",
                            f"fallback_hop={fallback_hop}")
    return f"{stamp}\t{status}\t/repo\t/wt\t{slug}\t{pipe}\n"


class FleetV20DispatchContractTest(unittest.TestCase):
    def test_current_quick_axes_are_separate_model_fields(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(
                row("2026-07-20T00:00:00+00:00", "open",
                    "quick-a", "att-quick-a", unit="code-execute")
            )
            values, malformed = dispatch._scan_jobs_log(str(jobs), set())
        self.assertEqual(malformed, 0)
        self.assertEqual(len(values), 1)
        job = values[0]
        self.assertEqual(job.dispatch_depth, 1)
        self.assertEqual(job.transport, "headless")
        self.assertEqual(job.execution_surface, "registered-headless")
        self.assertTrue(job.registered_worker)
        self.assertEqual(job.fallback_hop, "same-harness-headless")
        self.assertEqual(job.attempt_contract_status, "current")
        self.assertEqual(job.unit, "code-execute")

    def test_two_live_quick_attempts_are_contract_violations(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(
                row("2026-07-20T00:00:00+00:00", "open",
                    "quick-a", "att-quick-a")
                + row("2026-07-20T00:00:01+00:00", "open",
                      "quick-b", "att-quick-b")
            )
            values, _ = dispatch._scan_jobs_log(str(jobs), set())
        self.assertEqual(len(values), 2)
        self.assertTrue(all(
            job.attempt_contract_status == "invalid:quick-multiple-live"
            for job in values
        ))

    def test_quick_child_row_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(
                row("2026-07-20T00:00:00+00:00", "open",
                    "quick-child", "att-quick-child",
                    dispatch_depth=2, parent="owner")
            )
            values, _ = dispatch._scan_jobs_log(str(jobs), set())
        self.assertIn("quick-shape", values[0].attempt_contract_status)

    def test_terminal_retry_history_preserves_attempt_axes(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(
                row("2026-07-20T00:00:00+00:00", "done",
                    "quick-a", "att-quick-a")
                + row("2026-07-20T00:00:01+00:00", "done",
                      "quick-b", "att-quick-b")
            )
            evidence = dispatch._scan_route_nodes(str(jobs))
        history = evidence["rt-v20"]["one-shot"]["attempt_history"]
        self.assertEqual(
            [item["attempt_id"] for item in history],
            ["att-quick-a", "att-quick-b"],
        )
        self.assertTrue(all(
            item["registered_worker"] is True for item in history
        ))

    def test_legacy_retry_is_diagnostic_history_not_authoritative_node_state(self):
        legacy = (
            "2026-07-20T00:00:02+00:00\topen\t/repo\t/wt\tlegacy-retry\t"
            "capability=autopilot-code,depth=2,route_id=rt-v20,"
            "route_node=one-shot,attempt_id=att-legacy\n"
        )
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(
                row("2026-07-20T00:00:00+00:00", "done",
                    "quick-current", "att-current") + legacy
            )
            evidence = dispatch._scan_route_nodes(str(jobs))["rt-v20"]["one-shot"]
        self.assertEqual(evidence["status"], "done")
        self.assertEqual(evidence["slug"], "quick-current")
        self.assertEqual(
            [item["contract_status"] for item in evidence["attempt_history"]],
            ["current", "legacy-read-only"],
        )

    def test_legacy_and_invalid_schema_are_distinct(self):
        legacy = (
            "2026-07-20T00:00:00+00:00\topen\t/repo\t/wt\tlegacy\t"
            "capability=code,depth=2\n"
        )
        invalid = (
            "2026-07-20T00:00:01+00:00\topen\t/repo\t/wt\tinvalid\t"
            "attempt_schema_version=bogus,capability=code\n"
        )
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(legacy + invalid)
            values, _ = dispatch._scan_jobs_log(str(jobs), set())
        by_slug = {job.slug: job for job in values}
        self.assertTrue(by_slug["legacy"].legacy_read_only)
        self.assertEqual(
            by_slug["legacy"].attempt_contract_status, "legacy-read-only"
        )
        self.assertFalse(by_slug["invalid"].legacy_read_only)
        self.assertEqual(
            by_slug["invalid"].attempt_contract_status,
            "invalid:schema-version",
        )
        self.assertIsNone(by_slug["legacy"].unit)

    def test_teammate_is_visible_but_not_a_dispatch_attempt(self):
        contract = {
            "attempt_schema_version": "2",
            "dispatch_depth": "2",
            "transport": "interactive",
            "execution_surface": "claude-agent-team-teammate",
            "registered_worker": "0",
            "fallback_hop": "native-subagent",
        }
        projected = dispatch._attempt_contract(contract)
        self.assertIn(
            "teammate_not_dispatch_attempt",
            projected["attempt_contract_status"],
        )

    def test_current_attempt_rejects_every_bare_depth_alias(self):
        base = {
            "attempt_schema_version": "2",
            "dispatch_depth": "1",
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": "1",
            "fallback_hop": "same-harness-headless",
        }
        for field in ("depth", "owner_depth", "max_depth"):
            with self.subTest(field=field):
                projected = dispatch._attempt_contract({
                    **base,
                    field: "1",
                })
                self.assertIn(
                    "dispatch_depth",
                    projected["attempt_contract_status"],
                )


class FleetQuickFrameLegDisplayTest(unittest.TestCase):
    """N4: the display must mirror the registration contract, not a stale copy.

    Quick's same-harness pin applies to the WORK node only; its two frame legs
    are an advisory pair whose one MANDATORY independence axis is cross-harness.
    Painting a healthy cross-harness frame leg as broken is how an operator ends
    up cancelling a working round trip.
    """

    def scan(self, *rows):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text("".join(rows))
            values, malformed = dispatch._scan_jobs_log(str(jobs), set())
        self.assertEqual(malformed, 0)
        return {job.slug: job for job in values}

    def test_a_cross_harness_frame_leg_is_not_a_contract_violation(self):
        for node in ("frame", "frame-alternative"):
            with self.subTest(node=node):
                jobs = self.scan(row("2026-07-20T00:00:00+00:00", "open", "leg",
                                     "att-leg", route_node=node, worker_type="frame",
                                     fallback_hop="cross-harness-headless"))
                job = jobs["leg"]
                self.assertEqual(job.fallback_hop, "cross-harness-headless")
                self.assertEqual(job.attempt_contract_status, "current")

    def test_the_work_node_keeps_its_same_harness_pin(self):
        """The exception is scoped to the frame pair. `one-shot` on a
        cross-harness hop is still `invalid:quick-fallback`."""
        jobs = self.scan(row("2026-07-20T00:00:00+00:00", "open", "work", "att-work",
                             worker_type="owner", fallback_hop="cross-harness-headless"))
        self.assertEqual(jobs["work"].attempt_contract_status, "invalid:quick-fallback")
        # ...and the exception is keyed on BOTH axes: a frame worker_type on the
        # work node, or the frame node id without the frame worker type, is not
        # enough to earn it
        mismatched = self.scan(
            row("2026-07-20T00:00:00+00:00", "open", "type-only", "att-type-only",
                worker_type="frame", fallback_hop="cross-harness-headless"),
            row("2026-07-20T00:00:01+00:00", "open", "node-only", "att-node-only",
                route_node="frame", worker_type="owner",
                fallback_hop="cross-harness-headless"))
        self.assertEqual(mismatched["type-only"].attempt_contract_status,
                         "invalid:quick-fallback")
        self.assertEqual(mismatched["node-only"].attempt_contract_status,
                         "invalid:quick-fallback")

    def test_multiple_live_is_keyed_per_route_node_not_per_route(self):
        """Three live quick rows on one route is the NORMAL shape now. Only two
        live rows sharing a `(route_id, route_node)` are a real double-claim."""
        distinct = self.scan(
            row("2026-07-20T00:00:00+00:00", "open", "leg-a", "att-leg-a",
                route_node="frame", worker_type="frame",
                fallback_hop="cross-harness-headless"),
            row("2026-07-20T00:00:01+00:00", "open", "leg-b", "att-leg-b",
                route_node="frame-alternative", worker_type="frame"),
            row("2026-07-20T00:00:02+00:00", "open", "work", "att-work",
                worker_type="owner"))
        self.assertEqual({slug: job.attempt_contract_status
                          for slug, job in distinct.items()},
                         {"leg-a": "current", "leg-b": "current", "work": "current"})
        collided = self.scan(
            row("2026-07-20T00:00:00+00:00", "open", "leg-a", "att-leg-a",
                route_node="frame", worker_type="frame"),
            row("2026-07-20T00:00:01+00:00", "open", "leg-a2", "att-leg-a2",
                route_node="frame", worker_type="frame"),
            row("2026-07-20T00:00:02+00:00", "open", "work", "att-work",
                worker_type="owner"))
        self.assertEqual(collided["leg-a"].attempt_contract_status,
                         "invalid:quick-multiple-live")
        self.assertEqual(collided["leg-a2"].attempt_contract_status,
                         "invalid:quick-multiple-live")
        # the uncontested node in the same route is untouched
        self.assertEqual(collided["work"].attempt_contract_status, "current")


if __name__ == "__main__":
    unittest.main()
