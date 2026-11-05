"""Guard against stale cross-tick projection reuse without a complete input key."""

from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest import mock

from tools.fleet import projection, route
from tools.fleet.model import DispatchJob, Session, WorkProjection


class ProjectionReuseBoundaryTests(unittest.TestCase):
    def test_owner_candidates_keep_evidence_order_and_skip_unloaded_histories(self):
        class Unloaded(dict):
            def values(self):
                raise AssertionError('unloaded route history was scanned')
        owner=SimpleNamespace(session_id='sid',slug='owner')
        evidence={'unloaded':Unloaded(),
                  'second':{'work':{'parent':'owner'}},
                  'first':{'work':{'parent':'sid'}},
                  'unrelated':{'work':{'parent':'other'}}}
        records={'first':{'route_id':'first'},'second':{'route_id':'second'},
                 'unrelated':{'route_id':'unrelated'}}
        result=projection._evidence_owner_candidates(owner,evidence,records)
        self.assertEqual([rid for rid,_record,_nodes in result],['second','first'])
        self.assertIs(result[0][1],records['second'])

    def test_each_tick_resolves_changed_route_evidence_artifacts_registry_and_clock(self):
        route_id = "rt-projection-test"
        route_hash = "sha256:" + "a" * 64
        session = Session(
            harness="codex", pid=101, cwd="/project", session_id="session-1",
            slug="owner", liveness="working",
        )
        state = {"artifact_revision": 1}
        seen = []
        report_reader = SimpleNamespace(
            project_route=mock.Mock(
                side_effect=lambda *_args, **_kwargs: {
                    "artifact_revision": state["artifact_revision"]
                }
            ),
            report_detail_payload=lambda payload: payload,
        )

        def resolve(_entity, **kwargs):
            record = kwargs["route_records"][route_id]
            evidence = kwargs["node_evidence"][route_id]["execute"]
            jobs = kwargs["jobs"]
            seen.append((record["revision"], evidence["revision"], len(jobs), kwargs["now"]))
            return WorkProjection(
                source="route-exact", route_id=route_id, route_hash=route_hash,
                _route_view={"record": record},
            )

        ticks = [
            ({"route_id": route_id, "route_hash": route_hash, "revision": 1},
             {"status": "working", "revision": 1}, [], 100.0, 1),
            # Model a route-file rewrite, evidence update, appended registry row,
            # changed report artifact, and a later collection clock.
            ({"route_id": route_id, "route_hash": route_hash, "revision": 2},
             {"status": "done", "revision": 2},
             [DispatchJob(key="execute", slug="child", route_id=route_id)],
             160.0, 2),
        ]
        with mock.patch.object(projection, "resolve_work_projection", side_effect=resolve), \
                mock.patch.object(projection, "_report_verification", return_value=report_reader):
            for record, evidence, jobs, now, artifact_revision in ticks:
                state["artifact_revision"] = artifact_revision
                projection.attach_projections(
                    [session], jobs, route_records={route_id: record},
                    node_evidence={route_id: {"execute": evidence}},
                    artifact_root="/artifacts", now=now,
                    spec_markers={}, capability_groundings={},
                )
                self.assertEqual(
                    session.work_projection.report_verification,
                    {"artifact_revision": artifact_revision},
                )

        self.assertEqual(seen, [
            (1, 1, 0, 100.0),
            (2, 2, 1, 160.0),  # session row
            (2, 2, 1, 160.0),  # newly registered child row
        ])
        self.assertEqual(report_reader.project_route.call_count, 2)

    def test_gate_marks_are_memoized_per_projection_pass_and_returned_defensively(self):
        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "route",
                               "synth_composed_survey.json")
        record = route.load(fixture)
        route_id = record["route_id"]
        scope = route.RoundScope({route_id: record}, {}, ())
        with mock.patch.object(route, "_route_state_roots", return_value=("/state",)), \
                mock.patch.object(route, "gate_mark", return_value=None) as gate_mark:
            token = projection._ROUND_SCOPE.set(scope)
            try:
                projection._record_view(record, route_id, [], node_evidence={}, now=100.0)
                projection._record_view(record, route_id, [], node_evidence={}, now=100.0)
            finally:
                projection._ROUND_SCOPE.reset(token)
            expected = len(record.get("nodes") or ())
            self.assertEqual(gate_mark.call_count, expected)
            marks = scope.gate_marks_for(record, route_id)
            marks["caller-mutation"] = True
            self.assertNotIn("caller-mutation", scope.gate_marks_for(record, route_id))

            next_pass = route.RoundScope({route_id: record}, {}, ())
            next_pass.gate_marks_for(record, route_id)
            self.assertEqual(gate_mark.call_count, expected * 2)

    def test_owner_lifecycle_memo_is_anchor_scoped_and_fresh_next_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = os.path.join(directory, "jobs.log")
            with open(registry, "w", encoding="utf-8") as handle:
                handle.write("generation one\n")
            owner = SimpleNamespace(_registry_path=registry, attempt_id="owner-attempt")
            anchor = {"route_file": "/routes/one.json", "route_id": "route-one",
                      "route_hash": "sha256:" + "1" * 64}
            resolved = ({"route_file": "/routes/one.json", "route_id": "route-one",
                         "route_hash": anchor["route_hash"]}, "current")
            with mock.patch.object(projection, "_owner_route_advance_binding",
                                   return_value=resolved) as lifecycle:
                scope = route.RoundScope({}, {}, ())
                first = projection._owner_route_advance_for_pass(owner, anchor, scope)
                first[0]["route_id"] = "mutated-by-caller"
                second = projection._owner_route_advance_for_pass(owner, anchor, scope)
                self.assertEqual(second[0]["route_id"], "route-one")
                self.assertEqual(lifecycle.call_count, 1)

                other_anchor = dict(anchor, route_id="route-two")
                projection._owner_route_advance_for_pass(owner, other_anchor, scope)
                self.assertEqual(lifecycle.call_count, 2)

                next_pass = route.RoundScope({}, {}, ())
                projection._owner_route_advance_for_pass(owner, anchor, next_pass)
                self.assertEqual(lifecycle.call_count, 3)


if __name__ == "__main__":
    unittest.main()
