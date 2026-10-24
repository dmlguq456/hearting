"""Guard against stale cross-tick projection reuse without a complete input key."""

from types import SimpleNamespace
import unittest
from unittest import mock

from tools.fleet import projection
from tools.fleet.model import DispatchJob, Session, WorkProjection


class ProjectionReuseBoundaryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
