#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "owner_route_binding", ROOT / "utilities" / "owner_route_binding.py"
)
M = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = M
SPEC.loader.exec_module(M)


class OwnerRouteBindingTest(unittest.TestCase):
    def route(self, cwd: str) -> dict:
        return {
            "schema_version": 2,
            "route_id": "rt-test",
            "route_hash": "sha256:test",
            "cwd": cwd,
            "capability": "autopilot-code",
            "capability_mode": "dev",
            "effective_intensity": "strong",
            "owner_dispatch_depth": 1,
            "dispatch_evidence": {
                "tuples": [
                    {
                        "status": "supported",
                        "parent_harness": "codex",
                    }
                ]
            },
            "nodes": [{"id": "test", "runtime_requirements": []}],
        }

    def test_owner_binding_is_node_less_and_sealed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "route.json"
            route = self.route(td)
            path.write_text(json.dumps(route), encoding="utf-8")
            with mock.patch.object(M.ROUTE, "verify_route", return_value=route):
                result = M.validate_owner_route_binding(
                    path,
                    worktree=td,
                    capability="autopilot-code",
                    capability_mode="dev",
                    intensity="strong",
                    harness="codex",
                )
            self.assertEqual(result.route_id, "rt-test")
            self.assertFalse(hasattr(result, "route_node"))

    def test_owner_harness_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "route.json"
            route = self.route(td)
            path.write_text(json.dumps(route), encoding="utf-8")
            with mock.patch.object(M.ROUTE, "verify_route", return_value=route):
                with self.assertRaisesRegex(
                    M.OwnerRouteBindingError, "owner-route-harness-mismatch"
                ):
                    M.validate_owner_route_binding(
                        path,
                        worktree=td,
                        capability="autopilot-code",
                        capability_mode="dev",
                        intensity="strong",
                        harness="claude",
                    )

    def test_loopback_requirement_fails_closed(self):
        route = {"nodes": [{"id": "gate", "runtime_requirements": ["loopback-listen"]}]}
        with self.assertRaisesRegex(
            M.OwnerRouteBindingError, "loopback-only-unsupported"
        ):
            M.validate_runtime_requirements(route, "gate")

    def test_three_wrappers_project_owner_binding(self):
        for adapter in ("codex", "claude", "opencode"):
            text = (
                ROOT / "adapters" / adapter / "bin" / "dispatch-headless.py"
            ).read_text(encoding="utf-8")
            self.assertIn("AGENT_OWNER_ROUTE_FILE", (ROOT / "utilities" / "owner_route_binding.py").read_text(encoding="utf-8"))
            self.assertIn("args.owner_route_binding.route_file", text)
            self.assertIn('f",owner_route_file={args.owner_route_binding.route_file}"', text)
            self.assertIn('"AGENT_ROUTE_NODE": args.route_node or ""', text)


class OwnerRouteAdvanceTest(unittest.TestCase):
    def _route(self, td, route_id, generation, from_id=None, from_hash=None):
        path = Path(td) / f"{route_id}.json"
        route = {
            "route_id": route_id,
            "route_hash": f"sha256:{route_id}",
            "advance_generation": generation,
            "route_family_key": "fam-1",
            "owner_attempt_id": "att-1",
            "cwd": td,
            "capability": "autopilot-code",
            "capability_mode": "debug",
            "artifact_root": str(Path(td) / "artifacts"),
        }
        if from_id:
            route["source_route_supersession"] = {
                "from_route_id": from_id, "from_route_hash": from_hash,
            }
            route["source_route_id"] = from_id
            route["source_route_hash"] = from_hash
        path.write_text(json.dumps(route), encoding="utf-8")
        return path, route

    def _binding(self, path, route):
        return M.OwnerRouteBinding(str(path), route["route_id"], route["route_hash"])

    def _verify_route_stub(self, routes_by_id):
        def _verify(raw, *_a, **_k):
            return raw
        return _verify

    def _child_row(self, td, path, route, suffix="1"):
        meta = {
            "attempt_id": f"att-child-{suffix}",
            "parent_attempt_id": "att-1",
            "attempt_schema_version": "2",
            "dispatch_depth": "2",
            "registered_worker": "1",
            "execution_surface": "registered-headless",
            "worker_type": "stage",
            "route_file": str(path),
            "route_id": route["route_id"],
            "route_hash": route["route_hash"],
            "route_node": "execute",
            "capability": route["capability"],
            "capability_mode": route["capability_mode"],
            "artifact_root": route["artifact_root"],
            "launch_started": "1",
        }
        fields = ["1", "open", td, td, f"child-{suffix}", ""]
        return fields, meta

    def test_generation_zero_to_one_publish_and_resolve(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = str(Path(td) / "jobs.tsv")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r1_path, r1 = self._route(td, "rt-r1", 1, "rt-r0", "sha256:rt-r0")
            anchor = self._binding(r0_path, r0)
            target = self._binding(r1_path, r1)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                M.publish_owner_route_advance(
                    jobs, owner_attempt_id="att-1", source=anchor, target=target,
                    from_generation=0, to_generation=1,
                )
                current, status = M.resolve_owner_route_advance(
                    jobs, owner_attempt_id="att-1", anchor=anchor, anchor_generation=0,
                    registry_rows=[self._child_row(td, r1_path, r1)],
                )
            self.assertEqual(status, "owner-route-advance-current")
            self.assertEqual(current.route_id, "rt-r1")

    def test_multi_hop_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = str(Path(td) / "jobs.tsv")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r1_path, r1 = self._route(td, "rt-r1", 1, "rt-r0", "sha256:rt-r0")
            r2_path, r2 = self._route(td, "rt-r2", 2, "rt-r1", "sha256:rt-r1")
            anchor = self._binding(r0_path, r0)
            mid = self._binding(r1_path, r1)
            end = self._binding(r2_path, r2)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                M.publish_owner_route_advance(jobs, owner_attempt_id="att-1", source=anchor,
                                              target=mid, from_generation=0, to_generation=1)
                M.publish_owner_route_advance(jobs, owner_attempt_id="att-1", source=mid,
                                              target=end, from_generation=1, to_generation=2)
                current, status = M.resolve_owner_route_advance(
                    jobs, owner_attempt_id="att-1", anchor=anchor, anchor_generation=0,
                    registry_rows=[
                        self._child_row(td, r1_path, r1, "1"),
                        self._child_row(td, r2_path, r2, "2"),
                    ],
                )
            self.assertEqual(status, "owner-route-advance-current")
            self.assertEqual(current.route_id, "rt-r2")

    def test_exact_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = str(Path(td) / "jobs.tsv")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r1_path, r1 = self._route(td, "rt-r1", 1, "rt-r0", "sha256:rt-r0")
            anchor = self._binding(r0_path, r0)
            target = self._binding(r1_path, r1)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                first = M.publish_owner_route_advance(
                    jobs, owner_attempt_id="att-1", source=anchor, target=target,
                    from_generation=0, to_generation=1,
                )
                second = M.publish_owner_route_advance(
                    jobs, owner_attempt_id="att-1", source=anchor, target=target,
                    from_generation=0, to_generation=1,
                )
            self.assertEqual(first.record_id, second.record_id)

    def test_only_child_adopted_successor_advances_and_real_competition_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = str(Path(td) / "jobs.tsv")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r1_path, r1 = self._route(td, "rt-r1", 1, "rt-r0", "sha256:rt-r0")
            r1b_path, r1b = self._route(td, "rt-r1b", 1, "rt-r0", "sha256:rt-r0")
            anchor = self._binding(r0_path, r0)
            target = self._binding(r1_path, r1)
            other = self._binding(r1b_path, r1b)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                M.publish_owner_route_advance(jobs, owner_attempt_id="att-1", source=anchor,
                                              target=target, from_generation=0, to_generation=1)
                M.publish_owner_route_advance(jobs, owner_attempt_id="att-1", source=anchor,
                                              target=other, from_generation=0, to_generation=1)
                pending, pending_status = M.resolve_owner_route_advance(
                    jobs, owner_attempt_id="att-1", anchor=anchor,
                    anchor_generation=0, registry_rows=[],
                )
                adopted, adopted_status = M.resolve_owner_route_advance(
                    jobs, owner_attempt_id="att-1", anchor=anchor,
                    anchor_generation=0,
                    registry_rows=[self._child_row(td, r1_path, r1)],
                )
                with self.assertRaisesRegex(
                    M.OwnerRouteBindingError, "owner-route-advance-competing-successor"
                ):
                    M.resolve_owner_route_advance(
                        jobs, owner_attempt_id="att-1", anchor=anchor,
                        anchor_generation=0,
                        registry_rows=[
                            self._child_row(td, r1_path, r1, "1"),
                            self._child_row(td, r1b_path, r1b, "2"),
                        ],
                    )
            self.assertEqual((pending.route_id, pending_status),
                             ("rt-r0", "owner-route-advance-pending"))
            self.assertEqual((adopted.route_id, adopted_status),
                             ("rt-r1", "owner-route-advance-current"))

    def test_publish_rejects_generation_downgrade(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = str(Path(td) / "jobs.tsv")
            r0_path, r0 = self._route(td, "rt-r0", 1)
            r1_path, r1 = self._route(td, "rt-r1", 1, "rt-r0", "sha256:rt-r0")
            anchor = self._binding(r0_path, r0)
            target = self._binding(r1_path, r1)
            with self.assertRaisesRegex(
                M.OwnerRouteBindingError, "owner-route-advance-generation-invalid"
            ):
                M.publish_owner_route_advance(jobs, owner_attempt_id="att-1", source=anchor,
                                              target=target, from_generation=1, to_generation=1)

    def test_resolve_missing_target_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = str(Path(td) / "jobs.tsv")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r1_path, r1 = self._route(td, "rt-r1", 1, "rt-r0", "sha256:rt-r0")
            anchor = self._binding(r0_path, r0)
            target = self._binding(r1_path, r1)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                M.publish_owner_route_advance(jobs, owner_attempt_id="att-1", source=anchor,
                                              target=target, from_generation=0, to_generation=1)
                r1_path.unlink()
                with self.assertRaisesRegex(
                    M.OwnerRouteBindingError, "owner-route-advance-target-invalid"
                ):
                    M.resolve_owner_route_advance(
                        jobs, owner_attempt_id="att-1", anchor=anchor, anchor_generation=0,
                        registry_rows=[self._child_row(td, r1_path, r1)],
                    )

    def test_resolve_tampered_target_hash_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = str(Path(td) / "jobs.tsv")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r1_path, r1 = self._route(td, "rt-r1", 1, "rt-r0", "sha256:rt-r0")
            anchor = self._binding(r0_path, r0)
            target = self._binding(r1_path, r1)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                M.publish_owner_route_advance(jobs, owner_attempt_id="att-1", source=anchor,
                                              target=target, from_generation=0, to_generation=1)
                tampered = dict(r1); tampered["route_hash"] = "sha256:tampered"
                r1_path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.assertRaisesRegex(
                    M.OwnerRouteBindingError, "owner-route-advance-target-invalid"
                ):
                    M.resolve_owner_route_advance(
                        jobs, owner_attempt_id="att-1", anchor=anchor, anchor_generation=0,
                        registry_rows=[self._child_row(td, r1_path, r1)],
                    )

    def test_resolve_unrelated_source_supersession_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = str(Path(td) / "jobs.tsv")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r1_path, r1 = self._route(td, "rt-r1", 1, "rt-unrelated", "sha256:rt-unrelated")
            anchor = self._binding(r0_path, r0)
            target = self._binding(r1_path, r1)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                M.publish_owner_route_advance(jobs, owner_attempt_id="att-1", source=anchor,
                                              target=target, from_generation=0, to_generation=1)
                with self.assertRaisesRegex(
                    M.OwnerRouteBindingError, "owner-route-advance-supersession-mismatch"
                ):
                    M.resolve_owner_route_advance(
                        jobs, owner_attempt_id="att-1", anchor=anchor, anchor_generation=0,
                        registry_rows=[self._child_row(td, r1_path, r1)],
                    )

    def test_resealed_record_with_invalid_field_types_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = str(Path(td) / "jobs.tsv")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r1_path, r1 = self._route(td, "rt-r1", 1, "rt-r0", "sha256:rt-r0")
            anchor = self._binding(r0_path, r0)
            target = self._binding(r1_path, r1)
            advance = M.publish_owner_route_advance(
                jobs, owner_attempt_id="att-1", source=anchor, target=target,
                from_generation=0, to_generation=1,
            )
            path = Path(advance.path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["to_generation"] = "1"
            payload["record_id"] = M._advance_record_id(payload)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw
            ), self.assertRaisesRegex(
                M.OwnerRouteBindingError, "owner-route-advance-record-invalid"
            ):
                M.resolve_owner_route_advance(
                    jobs, owner_attempt_id="att-1", anchor=anchor,
                    anchor_generation=0, registry_rows=[],
                )

    def test_publish_from_environment_no_attempt_id_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = str(Path(td) / "jobs.tsv")
            result = M.publish_owner_route_advance_from_environment(
                jobs, source_route={"route_id": "rt-r0", "route_hash": "sha256:rt-r0"},
                target_route={"route_id": "rt-r1", "route_hash": "sha256:rt-r1",
                              "route_file": str(Path(td) / "rt-r1.json")},
                environ={},
            )
            self.assertIsNone(result)

    def test_publish_from_environment_stage_attempt_without_owner_context_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            result = M.publish_owner_route_advance_from_environment(
                "",
                source_route={"route_id": "rt-r0", "route_hash": "sha256:rt-r0"},
                target_route={"route_id": "rt-r1", "route_hash": "sha256:rt-r1",
                              "route_file": str(Path(td) / "rt-r1.json")},
                environ={"AGENT_DISPATCH_ATTEMPT_ID": "att-stage"},
            )
            self.assertIsNone(result)

    def test_publish_from_environment_rejects_unreadable_jobs_path(self):
        with tempfile.TemporaryDirectory() as td:
            r0_path, r0 = self._route(td, "rt-r0", 0)
            target = {"route_id": "rt-r1", "route_hash": "sha256:rt-r1",
                      "route_file": str(Path(td) / "rt-r1.json")}
            environ = {"AGENT_DISPATCH_ATTEMPT_ID": "att-owner",
                       "AGENT_OWNER_ROUTE_FILE": str(r0_path)}
            for jobs in ("", str(Path(td) / "missing.log"), td):
                with self.subTest(jobs=jobs), self.assertRaisesRegex(
                    M.OwnerRouteBindingError, "owner-route-jobs-unreadable"
                ):
                    M.publish_owner_route_advance_from_environment(
                        jobs, source_route=r0, target_route=target, environ=environ,
                    )

    def test_publish_from_environment_rejects_missing_owner_row(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_path = Path(td) / "jobs.tsv"
            jobs_path.write_text("", encoding="utf-8")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            environ = {
                "AGENT_DISPATCH_ATTEMPT_ID": "att-missing",
                "AGENT_OWNER_ROUTE_FILE": str(r0_path),
                "AGENT_OWNER_ROUTE_ID": r0["route_id"],
                "AGENT_OWNER_ROUTE_HASH": r0["route_hash"],
                "AGENT_DISPATCH_JOBS": str(jobs_path),
            }
            with self.assertRaisesRegex(
                M.OwnerRouteBindingError, "owner-route-owner-row-not-unique"
            ):
                M.publish_owner_route_advance_from_environment(
                    str(jobs_path), source_route=r0,
                    target_route={"route_id": "rt-r1", "route_hash": "sha256:rt-r1",
                                  "route_file": str(Path(td) / "rt-r1.json")},
                    environ=environ,
                )

    def test_publish_from_environment_rejects_owner_binding_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            jobs_path = Path(td) / "jobs.tsv"
            r0_path, r0 = self._route(td, "rt-r0", 0)
            meta = ("attempt_id=att-1,worker_type=owner,unit=_kernel/owner,"
                    "attempt_schema_version=2,owner_route_file=/other/route.json,"
                    "owner_route_id=rt-other,owner_route_hash=sha256:rt-other")
            jobs_path.write_text(
                "\t".join(["1", "open", "-", "-", "-", meta]) + "\n", encoding="utf-8",
            )
            with self.assertRaisesRegex(
                M.OwnerRouteBindingError, "owner-route-owner-binding-mismatch"
            ):
                M.publish_owner_route_advance_from_environment(
                    str(jobs_path), source_route=r0,
                    target_route={"route_id": "rt-r1", "route_hash": "sha256:rt-r1",
                                  "route_file": str(Path(td) / "rt-r1.json")},
                    environ={"AGENT_DISPATCH_ATTEMPT_ID": "att-1",
                             "AGENT_OWNER_ROUTE_FILE": str(r0_path),
                             "AGENT_OWNER_ROUTE_ID": r0["route_id"],
                             "AGENT_OWNER_ROUTE_HASH": r0["route_hash"]},
                )


class OwnerRouteLifecycleTest(unittest.TestCase):
    ATTEMPT = "att-owner-post-launch"

    def _row(self, td: str, *, status: str = "open", session: str = "parent-session") -> str:
        meta = ",".join((
            f"attempt_id={self.ATTEMPT}",
            "worker_type=owner", "unit=_kernel/owner", "attempt_schema_version=2",
            "dispatch_depth=1", "registered_worker=1",
            "execution_surface=registered-headless", "capability=autopilot-code",
            "capability_mode=debug", "intensity=standard",
            f"artifact_root={Path(td) / 'artifacts'}", f"parent_sid={session}",
            "owner_harness=codex",
        ))
        return "\t".join(("1", status, td, td, "owner", meta)) + "\n"

    def _route(self, td: str, route_id: str, generation: int, *,
               source: dict | None = None, worktree: str | None = None,
               owner_attempt: str | None = None) -> tuple[Path, dict]:
        cwd = worktree or td
        route = {
            "schema_version": 2,
            "route_id": route_id,
            "route_hash": f"sha256:{route_id}",
            "advance_generation": generation,
            "owner_attempt_id": owner_attempt or self.ATTEMPT,
            "route_family_key": "family-owner-post-launch",
            "cwd": cwd,
            "repo": td,
            "capability": "autopilot-code",
            "capability_mode": "debug",
            "effective_intensity": "standard",
            "artifact_root": str(Path(td) / "artifacts"),
            "reused_nodes": [],
        }
        if source is not None:
            edge = {
                "from_route_id": source["route_id"],
                "from_route_hash": source["route_hash"],
            }
            route.update({
                "source_route_id": source["route_id"],
                "source_route_hash": source["route_hash"],
                "source_route_supersession": edge,
                "supersession_edges": [edge],
            })
        path = Path(td) / f"{route_id}.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        return path, route

    def _payload(self, path: Path, route: dict) -> dict:
        return {**route, "route_file": str(path)}

    def _child_row(self, td: str, path: Path, route: dict, suffix: str) -> str:
        meta = ",".join((
            f"attempt_id=att-child-{suffix}", f"parent_attempt_id={self.ATTEMPT}",
            "attempt_schema_version=2", "dispatch_depth=2",
            "registered_worker=1", "execution_surface=registered-headless",
            "worker_type=stage", "unit=dev/backend", "route_node=execute",
            f"route_file={path}", f"route_id={route['route_id']}",
            f"route_hash={route['route_hash']}", "capability=autopilot-code",
            "capability_mode=debug", f"artifact_root={Path(td) / 'artifacts'}",
            "launch_started=1",
        ))
        return "\t".join(("1", "open", td, td, f"child-{suffix}", meta)) + "\n"

    def _env(self, jobs: Path, *, session: str = "parent-session") -> dict[str, str]:
        return {
            "AGENT_DISPATCH_ATTEMPT_ID": self.ATTEMPT,
            "AGENT_DISPATCH_WORKER_TYPE": "owner",
            "AGENT_DISPATCH_DEPTH": "1",
            "AGENT_DISPATCH_ATTEMPT_SCHEMA_VERSION": "2",
            "AGENT_DISPATCH_EXECUTION_SURFACE": "registered-headless",
            "AGENT_DISPATCH_REGISTERED_WORKER": "1",
            "AGENT_DISPATCH_PARENT_SESSION_ID": session,
            "AGENT_DISPATCH_OWNER_HARNESS": "codex",
            "AGENT_DISPATCH_JOBS": str(jobs),
        }

    def _attach(self, td: str, jobs: Path, path: Path, route: dict):
        return M.publish_owner_route_attachment_from_environment(
            jobs, target_route=self._payload(path, route), environ=self._env(jobs)
        )

    def test_post_launch_generation_zero_attachment_and_restart_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row(td), encoding="utf-8")
            path, route = self._route(td, "rt-r0", 0)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                attachment = self._attach(td, jobs, path, route)
                current, status = M.resolve_owner_route_lifecycle(
                    jobs, owner_attempt_id=self.ATTEMPT
                )
            self.assertEqual(attachment.route_id, "rt-r0")
            self.assertEqual(current.route_id, "rt-r0")
            self.assertEqual(status, "owner-route-post-launch-attachment")

            # A fresh module instance has no in-memory publication state; it
            # must recover solely from jobs.log + the immutable side record.
            spec = importlib.util.spec_from_file_location(
                "owner_route_binding_restart", ROOT / "utilities" / "owner_route_binding.py"
            )
            restarted = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = restarted
            spec.loader.exec_module(restarted)
            with mock.patch.object(restarted.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                recovered, recovered_status = restarted.resolve_owner_route_lifecycle(
                    jobs, owner_attempt_id=self.ATTEMPT
                )
            self.assertEqual(recovered.route_id, "rt-r0")
            self.assertEqual(recovered_status, "owner-route-post-launch-attachment")

    def test_unbound_owner_advances_generation_zero_one_two_and_reuses_nodes(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row(td), encoding="utf-8")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r1_path, r1 = self._route(td, "rt-r1", 1, source=r0)
            r1["reused_nodes"] = [{"node_id": "plan", "completion_marker": "plan.done"}]
            r1_path.write_text(json.dumps(r1), encoding="utf-8")
            r2_path, r2 = self._route(td, "rt-r2", 2, source=r1)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                self._attach(td, jobs, r0_path, r0)
                M.publish_owner_route_advance_from_environment(
                    jobs, source_route=self._payload(r0_path, r0),
                    target_route=self._payload(r1_path, r1), environ=self._env(jobs),
                )
                with jobs.open("a", encoding="utf-8") as stream:
                    stream.write(self._child_row(td, r1_path, r1, "r1"))
                M.publish_owner_route_advance_from_environment(
                    jobs, source_route=self._payload(r1_path, r1),
                    target_route=self._payload(r2_path, r2), environ=self._env(jobs),
                )
                with jobs.open("a", encoding="utf-8") as stream:
                    stream.write(self._child_row(td, r2_path, r2, "r2"))
                current, status = M.resolve_owner_route_lifecycle(
                    jobs, owner_attempt_id=self.ATTEMPT
                )
            self.assertEqual(current.route_id, "rt-r2")
            self.assertEqual(status, "owner-route-advance-current")
            self.assertEqual(json.loads(r1_path.read_text())["reused_nodes"][0]["node_id"], "plan")

    def test_childless_candidate_is_inert_and_started_successor_wins(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row(td), encoding="utf-8")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            abandoned_path, abandoned = self._route(
                td, "rt-abandoned", 1, source=r0
            )
            active_path, active = self._route(td, "rt-active", 1, source=r0)
            with mock.patch.object(
                M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw
            ):
                self._attach(td, jobs, r0_path, r0)
                M.publish_owner_route_advance_from_environment(
                    jobs, source_route=self._payload(r0_path, r0),
                    target_route=self._payload(abandoned_path, abandoned),
                    environ=self._env(jobs),
                )
                M.publish_owner_route_advance_from_environment(
                    jobs, source_route=self._payload(r0_path, r0),
                    target_route=self._payload(active_path, active),
                    environ=self._env(jobs),
                )
                before, before_status = M.resolve_owner_route_lifecycle(
                    jobs, owner_attempt_id=self.ATTEMPT
                )
                with jobs.open("a", encoding="utf-8") as stream:
                    stream.write(self._child_row(td, active_path, active, "active"))
                after, after_status = M.resolve_owner_route_lifecycle(
                    jobs, owner_attempt_id=self.ATTEMPT
                )
            self.assertEqual((before.route_id, before_status),
                             ("rt-r0", "owner-route-advance-pending"))
            self.assertEqual((after.route_id, after_status),
                             ("rt-active", "owner-route-advance-current"))

    def test_internal_runtime_thread_is_not_launch_parent_session(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row(td, session="launch-parent"), encoding="utf-8")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r0["runtime_lineage"] = {
                "operation": "resume", "thread_id": "owner-internal-thread",
                "lastTurnId": None, "ephemeral": False,
            }
            r0_path.write_text(json.dumps(r0), encoding="utf-8")
            env = self._env(jobs, session="launch-parent")
            with mock.patch.object(
                M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw
            ):
                attached = M.publish_owner_route_attachment_from_environment(
                    jobs, target_route=self._payload(r0_path, r0), environ=env,
                )
            self.assertEqual(attached.route_id, "rt-r0")

    def test_atomic_child_start_race_resolves_only_before_or_after_state(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            owner_row = self._row(td).rstrip("\n")
            jobs.write_text(owner_row + "\n", encoding="utf-8")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            r1_path, r1 = self._route(td, "rt-r1", 1, source=r0)
            gate = threading.Event()

            def publish_child():
                gate.wait()
                with M._jobs_lock(jobs):
                    child = self._child_row(td, r1_path, r1, "race").rstrip("\n")
                    fd, temp_name = tempfile.mkstemp(
                        prefix=".jobs-race-", dir=str(jobs.parent)
                    )
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as stream:
                            stream.write(owner_row + "\n" + child + "\n")
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temp_name, jobs)
                    finally:
                        if os.path.exists(temp_name):
                            os.unlink(temp_name)

            def resolve_once(_index):
                gate.wait()
                current, status = M.resolve_owner_route_lifecycle(
                    jobs, owner_attempt_id=self.ATTEMPT
                )
                return current.route_id, status

            with mock.patch.object(
                M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw
            ):
                self._attach(td, jobs, r0_path, r0)
                M.publish_owner_route_advance_from_environment(
                    jobs, source_route=self._payload(r0_path, r0),
                    target_route=self._payload(r1_path, r1),
                    environ=self._env(jobs),
                )
                with ThreadPoolExecutor(max_workers=9) as pool:
                    writer = pool.submit(publish_child)
                    readers = [pool.submit(resolve_once, index) for index in range(8)]
                    gate.set()
                    observed = [future.result() for future in readers]
                    writer.result()
                final = resolve_once(99)
            allowed = {
                ("rt-r0", "owner-route-advance-pending"),
                ("rt-r1", "owner-route-advance-current"),
            }
            self.assertTrue(set(observed) <= allowed, observed)
            self.assertEqual(final, ("rt-r1", "owner-route-advance-current"))

    def test_competing_first_route_and_wrong_session_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row(td), encoding="utf-8")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            other_path, other = self._route(td, "rt-other", 0)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                self._attach(td, jobs, r0_path, r0)
                with self.assertRaisesRegex(
                    M.OwnerRouteBindingError, "owner-route-attachment-competing-route"
                ):
                    self._attach(td, jobs, other_path, other)
            with tempfile.TemporaryDirectory() as td2:
                jobs2 = Path(td2) / "jobs.log"
                jobs2.write_text(self._row(td2), encoding="utf-8")
                path2, route2 = self._route(td2, "rt-session", 0)
                with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw), \
                     self.assertRaisesRegex(M.OwnerRouteBindingError, "owner-route-owner-session-mismatch"):
                    M.publish_owner_route_attachment_from_environment(
                        jobs2, target_route=self._payload(path2, route2),
                        environ=self._env(jobs2, session="unrelated-session"),
                    )

    def test_unrelated_worktree_downgrade_and_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as unrelated:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row(td), encoding="utf-8")
            r0_path, r0 = self._route(td, "rt-r0", 0)
            wrong_path, wrong = self._route(td, "rt-wrong", 1, source=r0, worktree=unrelated)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                self._attach(td, jobs, r0_path, r0)
                with self.assertRaisesRegex(
                    M.OwnerRouteBindingError, "owner-route-advance-worktree-mismatch"
                ):
                    M.publish_owner_route_advance_from_environment(
                        jobs, source_route=self._payload(r0_path, r0),
                        target_route=self._payload(wrong_path, wrong), environ=self._env(jobs),
                    )
                with self.assertRaisesRegex(
                    M.OwnerRouteBindingError, "owner-route-advance-generation-invalid"
                ):
                    M.publish_owner_route_advance(
                        jobs, owner_attempt_id=self.ATTEMPT,
                        source=M.OwnerRouteBinding(str(r0_path), r0["route_id"], r0["route_hash"]),
                        target=M.OwnerRouteBinding(str(wrong_path), wrong["route_id"], wrong["route_hash"]),
                        from_generation=1, to_generation=1,
                    )
                tampered = dict(r0); tampered["route_hash"] = "sha256:tampered"
                r0_path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.assertRaisesRegex(
                    M.OwnerRouteBindingError, "owner-route-binding-hash-mismatch"
                ):
                    M.resolve_owner_route_lifecycle(jobs, owner_attempt_id=self.ATTEMPT)

    def test_concurrent_attachment_is_atomic_and_owner_close_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(self._row(td), encoding="utf-8")
            path, route = self._route(td, "rt-r0", 0)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(
                        lambda _n: self._attach(td, jobs, path, route), range(16)
                    ))
            self.assertEqual({item.record_id for item in results}, {results[0].record_id})
            records = list((jobs.parent / M.OWNER_ROUTE_ATTACHMENT_DIR).glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text())["route_id"], "rt-r0")

            # Registry writers use the same lock. Once the exact row is
            # terminal, a new attachment cannot race it back into eligibility.
            with M._jobs_lock(jobs):
                jobs.write_text(self._row(td, status="done"), encoding="utf-8")
            other_path, other = self._route(td, "rt-after-close", 0)
            with mock.patch.object(M.ROUTE, "verify_route", side_effect=lambda raw, *a, **k: raw), \
                 self.assertRaisesRegex(M.OwnerRouteBindingError, "owner-route-owner-row-ineligible"):
                self._attach(td, jobs, other_path, other)


if __name__ == "__main__":
    unittest.main()
