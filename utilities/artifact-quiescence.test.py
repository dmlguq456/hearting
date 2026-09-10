#!/usr/bin/env python3
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import fcntl
from unittest.mock import patch

P = Path(__file__).with_name("artifact-quiescence.py")
S = importlib.util.spec_from_file_location("artifact_quiescence_tested", P)
Q = importlib.util.module_from_spec(S)
S.loader.exec_module(Q)


class QuiescenceTest(unittest.TestCase):
    def indexed(self, config, *paths):
        Path(config["resource_index"]).write_text(json.dumps({"schema_version": 1,
            "registries": {str(i): {"path": str(p)} for i, p in enumerate(paths)}}))

    def artifact_root(self, base: Path, name: str) -> Path:
        root = base / name
        (root / ".runtime" / "routes").mkdir(parents=True)
        return root

    def sealed_route(self, root: Path, node: str = "test") -> tuple[dict, Path]:
        quick = node == "one-shot"
        cwd = str(root.parent)
        # Two supported harnesses: a quick route now compiles a cross-harness
        # frame pair and refuses a single-harness candidate list at compile.
        candidates = [{"harness": harness, "surface": "registered-headless",
                       "transport": "headless", "status": "supported",
                       "probe_source": "hermetic-fixture",
                       "probe_time": "2026-09-07T00:00:00Z"}
                      for harness in ("codex", "claude")]
        evidence = {"tuples": [{"parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "workspace-write", "child_harness": "codex",
            "launch_authority": "conductor", "status": "supported", "failure_class": "",
            "probe_source": "hermetic-fixture", "probe_time": "2026-09-07T00:00:00Z",
            "checked_worktree": cwd, "codex_command": "ok", "failure_scope": "none",
            "retry_on_isolated_worktree": 0}], "native_subagent": []}
        if node == "eval-run":
            route = Q.ROUTES.compile_route(
                capability="autopilot-lab", capability_mode="eval", requested_intensity="standard",
                cwd=cwd, artifact_root=str(root), slug="fixture", transport="headless",
                dispatch_evidence=evidence, tracked_gate_evidence={
                    "spec_read": {"satisfied": True, "source": "hermetic-fixture"},
                    "drift_verdict": "within-spec", "workflow_mode": "tracked",
                    "artifact_guard": {"satisfied": True, "source": "hermetic-fixture"}})
        else:
            route = Q.ROUTES.compose_route(
                capability="autopilot-code",
                capability_mode="debug", slug="fixture",
                shape="solo" if quick else "staged", graph=None if quick else node,
                intensity="quick" if quick else "standard", cwd=cwd, artifact_root=str(root),
                spec_read="hermetic-fixture", drift_verdict="hermetic-fixture",
                dispatch_evidence=None if quick else evidence,
                registered_headless_evidence={"candidates": candidates} if quick else None)
        Q.ROUTES.verify_route(route, allow_stale_registry=True)
        path = root / ".runtime" / "routes" / f"{route['route_id']}.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        return route, path

    def dispatch_row(self, *, slug: str, attempt: str, metadata: dict,
                     status: str = "open") -> str:
        base = {
            "attempt_schema_version": "2",
            "dispatch_depth": "2",
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": "1",
            "fallback_hop": "same-harness-headless",
            "attempt_id": attempt,
        }
        base.update({key: str(value) for key, value in metadata.items()})
        pipe = ",".join(f"{key}={value}" for key, value in base.items())
        return f"2026-09-07T00:00:00Z\t{status}\t/repo\t/worktree\t{slug}\t{pipe}\n"

    def write_resource_runs(self, config: dict, base: Path, runs: dict) -> Path:
        registry = base / "resource.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": runs}), encoding="utf-8")
        self.indexed(config, registry)
        return registry

    def test_review_quick_owner_route_tuple_is_attributable(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            root = Path(config["artifact_root"])
            route, path = self.sealed_route(root, "one-shot")
            Path(config["dispatch_jobs"]).write_text(self.dispatch_row(
                slug="quick", attempt="att-quick", metadata={
                    "dispatch_depth": "1", "worker_type": "owner", "unit": "_kernel/owner",
                    "artifact_root": root, "route_file": path, "route_id": route["route_id"],
                    "route_hash": route["route_hash"], "route_node": "one-shot"}))
            value = Q.collect(config)
            self.assertTrue(value["observation_valid"], value.get("source_diagnostics"))
            self.assertEqual(value["open_dispatch_attempts"], 1)

    def test_review_frame_leg_route_tuple_is_attributable_standard(self):
        # N2 site :212/:303 -- a depth-1 frame leg (worker_type=frame,
        # unit=plan/frame) on a standard+ route binds `frame-route`.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            root = Path(config["artifact_root"])
            route, path = self.sealed_route(root, "frame,plan")
            Path(config["dispatch_jobs"]).write_text(self.dispatch_row(
                slug="frame-leg", attempt="att-frame-leg", metadata={
                    "dispatch_depth": "1", "worker_type": "frame", "unit": "plan/frame",
                    "artifact_root": root, "route_file": path, "route_id": route["route_id"],
                    "route_hash": route["route_hash"], "route_node": "frame"}))
            value = Q.collect(config)
            self.assertTrue(value["observation_valid"], value.get("source_diagnostics"))
            self.assertEqual(value["open_dispatch_attempts"], 1)

    def test_review_frame_leg_route_tuple_is_attributable_quick(self):
        # Same axis on the quick three-node route (frame, frame-alternative,
        # one-shot) -- both frame legs bind, not just the owner node.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            root = Path(config["artifact_root"])
            route, path = self.sealed_route(root, "one-shot")
            for slug, node in (("frame-primary", "frame"), ("frame-alt", "frame-alternative")):
                with self.subTest(node=node):
                    Path(config["dispatch_jobs"]).write_text(self.dispatch_row(
                        slug=slug, attempt=f"att-{slug}", metadata={
                            "dispatch_depth": "1", "worker_type": "frame", "unit": "plan/frame",
                            "artifact_root": root, "route_file": path, "route_id": route["route_id"],
                            "route_hash": route["route_hash"], "route_node": node}))
                    value = Q.collect(config)
                    self.assertTrue(value["observation_valid"], value.get("source_diagnostics"))
                    self.assertEqual(value["open_dispatch_attempts"], 1)

    def test_review_malformed_frame_leg_route_tuple_is_refused(self):
        # A frame row that fails either half of the N2 axis is refused with a
        # typed reason, never silently accepted as an ordinary stage row.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            root = Path(config["artifact_root"])
            quick_route, quick_path = self.sealed_route(root, "one-shot")
            with self.subTest("unit-mismatch"):
                Path(config["dispatch_jobs"]).write_text(self.dispatch_row(
                    slug="wrong-unit", attempt="att-wrong-unit", metadata={
                        "dispatch_depth": "1", "worker_type": "frame", "unit": "dev/backend",
                        "artifact_root": root, "route_file": quick_path,
                        "route_id": quick_route["route_id"], "route_hash": quick_route["route_hash"],
                        "route_node": "frame"}))
                value = Q.collect(config)
                self.assertFalse(value["observation_valid"])
                self.assertIn("stage-route-binding-axis-invalid", json.dumps(value["source_diagnostics"]))
            standard_route, standard_path = self.sealed_route(root, "frame,plan")
            with self.subTest("depth-2-target"):
                Path(config["dispatch_jobs"]).write_text(self.dispatch_row(
                    slug="depth2-target", attempt="att-depth2-target", metadata={
                        "dispatch_depth": "1", "worker_type": "frame", "unit": "plan/frame",
                        "artifact_root": root, "route_file": standard_path,
                        "route_id": standard_route["route_id"], "route_hash": standard_route["route_hash"],
                        "route_node": "plan"}))
                value = Q.collect(config)
                self.assertFalse(value["observation_valid"])
                self.assertIn("frame-route-axis-invalid", json.dumps(value["source_diagnostics"]))

    def test_review_quick_owner_route_binds_regardless_of_node_count(self):
        # N2: the quick-owner-route binding asserts the `one-shot` node's own
        # identity, never the route's node count -- a three-node quick route
        # (two frame legs plus one-shot) still binds. No `len(nodes) == 1`
        # assumption survives.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            root = Path(config["artifact_root"])
            route, path = self.sealed_route(root, "one-shot")
            self.assertEqual(len(route["nodes"]), 3)
            Path(config["dispatch_jobs"]).write_text(self.dispatch_row(
                slug="owner-oneshot", attempt="att-owner-oneshot", metadata={
                    "dispatch_depth": "1", "worker_type": "owner", "unit": "_kernel/owner",
                    "artifact_root": root, "route_file": path, "route_id": route["route_id"],
                    "route_hash": route["route_hash"], "route_node": "one-shot"}))
            value = Q.collect(config)
            self.assertTrue(value["observation_valid"], value.get("source_diagnostics"))
            self.assertEqual(value["open_dispatch_attempts"], 1)

    def test_dispatch_requires_explicit_root_while_resource_accepts_sealed_route(self):
        for binding in ("owner", "stage", "quick-owner"):
            for root_value in (None, ""):
                with self.subTest(binding=binding, root_value=root_value), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory); config = self.fixture(base)
                    external = self.artifact_root(base, "external")
                    node = "one-shot" if binding == "quick-owner" else "test"
                    route, path = self.sealed_route(external, node)
                    if binding == "owner":
                        metadata = {
                            "dispatch_depth": "1", "worker_type": "owner", "unit": "_kernel/owner",
                            "owner_route_file": path, "owner_route_id": route["route_id"],
                            "owner_route_hash": route["route_hash"],
                        }
                    else:
                        metadata = {"route_file": path, "route_id": route["route_id"],
                                    "route_hash": route["route_hash"], "route_node": node}
                        if binding == "quick-owner":
                            metadata.update(dispatch_depth="1", worker_type="owner", unit="_kernel/owner")
                    if root_value is not None:
                        metadata["artifact_root"] = root_value
                    jobs = Path(config["dispatch_jobs"])
                    jobs.write_text(self.dispatch_row(slug="route-only", attempt="att-route-only",
                                                      metadata=metadata))
                    evidence = base / "dispatch.json"
                    value = Q.publish(str(evidence), config)
                    self.assertFalse(value["observation_valid"], value)
                    self.assertFalse(value["proven"], value)
                    self.assertEqual(value["unattributable_open_items"], 1)
                    self.assertEqual(value["pending"], 1)
                    self.assertIn("dispatch-artifact-root-required", json.dumps(value["sources"]["attribution"]))
                    self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])

                    metadata["artifact_root"] = external
                    jobs.write_text(self.dispatch_row(slug="explicit", attempt="att-explicit",
                                                      metadata=metadata))
                    self.assertTrue(Q.publish(str(base / "explicit.json"), config)["proven"])
                    jobs.write_text("")
                    self.write_resource_runs(config, base, {"route-only": {
                        "status": "running", "route": str(path), "node": node}})
                    resource = Q.publish(str(base / "resource.json"), config)
                    self.assertTrue(resource["proven"], resource)
                    self.assertEqual(resource["sources"]["attribution"]["summary"]["external"]["resource"], 1)

    def test_review_duplicate_dispatch_identity_is_not_silently_external(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            other = self.artifact_root(base, "external")
            row = self.dispatch_row(slug="dup", attempt="att-dup",
                                    metadata={"artifact_root": config["artifact_root"]})
            Path(config["dispatch_jobs"]).write_text(row.rstrip()+f",artifact_root={other}\n")
            value = Q.publish(str(base / "evidence.json"), config)
            self.assertFalse(value["observation_valid"])
            self.assertEqual(value["unattributable_open_items"], 1)

    def test_review_resource_alias_and_json_duplicate_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            route, path = self.sealed_route(Path(config["artifact_root"]))
            registry = self.write_resource_runs(config, base, {"run": {
                "status": "running", "route": str(path), "node": "test", "route_node": "wrong"}})
            value = Q.publish(str(base / "aliases.json"), config)
            self.assertFalse(value["observation_valid"])
            registry.write_text('{"schema_version":1,"runs":{"run":{"status":"running",'
                '"artifact_root":"/unknown","artifact_root":'+json.dumps(config["artifact_root"])+'}}}')
            value = Q.publish(str(base / "duplicate.json"), config)
            self.assertFalse(value["observation_valid"])

    def test_review_incomplete_route_is_not_external_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            root = self.artifact_root(base, "external")
            route = {"schema_version": 2, "artifact_root": str(root),
                     "cwd": str(base), "nodes": [{"id": "test"}]}
            route["route_hash"] = Q.ROUTES.route_hash(route)
            route["route_id"] = "rt-" + route["route_hash"].split(":")[1][:16]
            path = root / ".runtime/routes" / (route["route_id"] + ".json")
            path.write_text(json.dumps(route))
            self.write_resource_runs(config, base, {"run": {
                "status": "running", "route": str(path), "node": "test"}})
            value = Q.publish(str(base / "evidence.json"), config)
            self.assertFalse(value["observation_valid"])
            self.assertEqual(value["unattributable_open_items"], 1)

    def test_review_route_semantics_and_digest_share_one_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.artifact_root(Path(directory), "artifacts")
            route, path = self.sealed_route(root)
            first = path.read_bytes()
            original = Q._file_row
            def replace_before_second_read(file):
                path.write_bytes(first + b" ")
                return original(file)
            with patch.object(Q, "_file_row", replace_before_second_read):
                proof = Q._sealed_route(str(path), expected_node="test")
            self.assertEqual(proof["file"]["sha256"], Q._digest_bytes(first))

    def test_review_stale_route_graph_and_unknown_basis_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.artifact_root(Path(directory), "artifacts")
            route, path = self.sealed_route(root)
            route["registry_digest"] = "sha256:" + "0" * 64
            def seal(value):
                value["route_hash"] = Q.ROUTES.route_hash(value)
                value["route_id"] = "rt-" + value["route_hash"].split(":")[1][:16]
                target = path.with_name(value["route_id"] + ".json")
                target.write_text(json.dumps(value))
                return target
            self.assertEqual(Q._sealed_route(str(seal(route)))["artifact_root"]["resolved_path"], str(root))
            bad = json.loads(json.dumps(route)); bad["nodes"][0]["depends_on"] = ["test"]
            with self.assertRaises(ValueError):
                Q._sealed_route(str(seal(bad)))
            bad = json.loads(json.dumps(route)); bad["validation_basis"]["basis_version"] = 999
            with self.assertRaises(ValueError):
                Q._sealed_route(str(seal(bad)))

    def test_review_stale_workflow_contract_must_be_complete(self):
        for mutation in ("terminal-gate", "terminal-resource", "duplicate-gate", "continuation", "workflow-map"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); config = self.fixture(base)
                root = self.artifact_root(base, "external")
                route, path = self.sealed_route(root)
                route["registry_digest"] = "sha256:" + "0" * 64
                node = route["nodes"][0]
                if mutation == "terminal-gate": node.pop("terminal_gate")
                elif mutation == "terminal-resource": node["kind"] = "resource-runner"
                elif mutation in ("duplicate-gate", "continuation"):
                    extra = json.loads(json.dumps(node)); extra["id"] = "extra"
                    if mutation == "continuation": extra["terminal"] = False
                    route["nodes"].append(extra)
                    route["workflow_contract"]["terminal_nodes"] = sorted(n["id"] for n in route["nodes"] if n.get("terminal"))
                else: route["workflow_contract"]["continuations"] = {"test": "supervised"}
                route["route_hash"] = Q.ROUTES.route_hash(route)
                route["route_id"] = "rt-" + route["route_hash"].split(":")[1][:16]
                path = path.with_name(route["route_id"] + ".json"); path.write_text(json.dumps(route))
                self.write_resource_runs(config, base, {"run": {"status": "running", "route": str(path), "node": "test"}})
                value = Q.publish(str(base / "bad-workflow.json"), config)
                self.assertFalse(value["observation_valid"])
                self.assertEqual(value["unattributable_open_items"], 1)

    def test_review_real_resource_route_remains_attributable_when_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            root = self.artifact_root(base, "external")
            route, path = self.sealed_route(root, "eval-run")
            for stale in (False, True):
                with self.subTest(stale=stale):
                    if stale:
                        route["registry_digest"] = "sha256:" + "0" * 64
                        route["route_hash"] = Q.ROUTES.route_hash(route)
                        route["route_id"] = "rt-" + route["route_hash"].split(":")[1][:16]
                        path = path.with_name(route["route_id"] + ".json"); path.write_text(json.dumps(route))
                    self.write_resource_runs(config, base, {"run": {"status": "running", "route": str(path), "node": "eval-run"}})
                    value = Q.publish(str(base / f"resource-{stale}.json"), config)
                    self.assertTrue(value["proven"], value)
                    self.assertEqual(value["sources"]["attribution"]["summary"]["external"]["resource"], 1)

    def test_review_route_read_rejects_file_and_ancestor_races(self):
        for mutation in ("replace", "content", "ancestor"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); root = self.artifact_root(base, "artifacts")
                _, path = self.sealed_route(root)
                alias = base / "alias"; alias.symlink_to(root, target_is_directory=True)
                selected = alias / path.relative_to(root) if mutation == "ancestor" else path
                original = Q.RESOURCES.os.fstat
                calls = 0
                def raced(fd):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        if mutation == "replace":
                            replacement = path.with_suffix(".new")
                            replacement.write_bytes(path.read_bytes()); replacement.replace(path)
                        elif mutation == "content":
                            with path.open("ab") as stream: stream.write(b" ")
                        else:
                            other = self.artifact_root(base, "external")
                            alias.unlink(); alias.symlink_to(other, target_is_directory=True)
                    return original(fd)
                with patch.object(Q.RESOURCES.os, "fstat", side_effect=raced), self.assertRaises((ValueError, OSError)):
                    Q._sealed_route(str(selected))

    def test_scopes_mixed_owner_stage_and_resource_rows_by_physical_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            target = Path(config["artifact_root"])
            external = self.artifact_root(base, "external-artifacts")
            target_route, target_route_path = self.sealed_route(target)
            external_route, external_route_path = self.sealed_route(external)
            owner = {
                "dispatch_depth": "1", "worker_type": "owner", "unit": "_kernel/owner",
                "owner_route_file": target_route_path,
                "owner_route_id": target_route["route_id"],
                "owner_route_hash": target_route["route_hash"],
                "artifact_root": target,
            }
            external_stage = {
                "route_file": external_route_path, "route_id": external_route["route_id"],
                "route_hash": external_route["route_hash"], "route_node": "test",
                "artifact_root": external,
            }
            Path(config["dispatch_jobs"]).write_text(
                self.dispatch_row(slug="target-owner", attempt="att-target-owner", metadata=owner)
                + self.dispatch_row(slug="external-stage", attempt="att-external-stage",
                                    metadata=external_stage)
                + self.dispatch_row(slug="target-adhoc", attempt="att-target-adhoc",
                                    metadata={"artifact_root": target})
                + self.dispatch_row(slug="external-adhoc", attempt="att-external-adhoc",
                                    metadata={"artifact_root": external}),
                encoding="utf-8",
            )
            identity = Q.RESOURCES.proc_identity(os.getpid())
            self.write_resource_runs(config, base, {
                "target-route": {**identity, "status": "running", "route": str(target_route_path),
                                 "node": "test"},
                "external-route": {**identity, "status": "running", "route": str(external_route_path),
                                   "node": "test"},
                "target-explicit": {**identity, "status": "running", "artifact_root": str(target)},
            })
            value = Q.publish(str(base / "mixed.json"), config)
            self.assertTrue(value["observation_valid"], value)
            self.assertEqual(value["open_routes"], 1)
            self.assertEqual(value["open_dispatch_attempts"], 2)
            self.assertEqual(value["open_jobs"], 2)
            self.assertEqual(value["unattributable_open_items"], 0)
            summary = value["sources"]["attribution"]["summary"]
            self.assertEqual(summary["external"]["dispatch"], 2)
            self.assertEqual(summary["external"]["resource"], 1)
            self.assertEqual(summary["target"]["dispatch"], 2)
            self.assertEqual(summary["target"]["resource"], 2)

    def test_open_unattributable_fails_closed_but_closed_historical_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            missing = self.dispatch_row(
                slug="unknown", attempt="att-unknown", metadata={})
            Path(config["dispatch_jobs"]).write_text(missing, encoding="utf-8")
            opened = Q.publish(str(base / "open.json"), config)
            self.assertFalse(opened["observation_valid"], opened)
            self.assertEqual(opened["reason"], "open-item-unattributable")
            self.assertEqual(opened["unattributable_open_items"], 1)
            self.assertEqual(opened["pending"], 1)

            Path(config["dispatch_jobs"]).write_text(
                self.dispatch_row(slug="unknown", attempt="att-unknown", metadata={}, status="done"),
                encoding="utf-8",
            )
            closed = Q.publish(str(base / "closed.json"), config)
            self.assertTrue(closed["observation_valid"], closed)
            self.assertTrue(closed["proven"], closed)
            self.assertEqual(closed["sources"]["attribution"]["summary"]["historical"]["dispatch"], 1)

    def test_route_conflict_tamper_missing_and_relative_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            target = Path(config["artifact_root"])
            external = self.artifact_root(base, "external")
            route, route_path = self.sealed_route(external)
            cases = {
                "root-conflict": {
                    "artifact_root": target, "route_file": route_path,
                    "route_id": route["route_id"], "route_hash": route["route_hash"],
                    "route_node": "test",
                },
                "missing": {
                    "artifact_root": external,
                    "route_file": external / ".runtime/routes/absent.json",
                    "route_id": route["route_id"], "route_hash": route["route_hash"],
                    "route_node": "test",
                },
                "relative": {
                    "artifact_root": external,
                    "route_file": "relative-route.json", "route_id": route["route_id"],
                    "route_hash": route["route_hash"], "route_node": "test",
                },
                "hash-mismatch": {
                    "artifact_root": external,
                    "route_file": route_path, "route_id": route["route_id"],
                    "route_hash": "sha256:" + "0" * 64, "route_node": "test",
                },
                "node-mismatch": {
                    "artifact_root": external,
                    "route_file": route_path, "route_id": route["route_id"],
                    "route_hash": route["route_hash"], "route_node": "absent-node",
                },
                "relative-root": {"artifact_root": "relative-artifacts"},
            }
            for name, metadata in cases.items():
                with self.subTest(name=name):
                    Path(config["dispatch_jobs"]).write_text(
                        self.dispatch_row(slug=name, attempt=f"att-{name}", metadata=metadata),
                        encoding="utf-8",
                    )
                    value = Q.publish(str(base / f"{name}.json"), config)
                    self.assertFalse(value["observation_valid"], value)
                    self.assertEqual(value["unattributable_open_items"], 1)
            route["tampered"] = True
            route_path.write_text(json.dumps(route), encoding="utf-8")
            Path(config["dispatch_jobs"]).write_text(
                self.dispatch_row(slug="tampered", attempt="att-tampered", metadata={
                    "artifact_root": external,
                    "route_file": route_path, "route_id": route["route_id"],
                    "route_hash": route["route_hash"], "route_node": "test",
                }), encoding="utf-8")
            tampered = Q.publish(str(base / "tampered.json"), config)
            self.assertFalse(tampered["observation_valid"], tampered)
            self.assertEqual(tampered["unattributable_open_items"], 1)

    def test_external_route_dependency_is_sealed_and_revalidated(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            external = self.artifact_root(base, "external")
            route, route_path = self.sealed_route(external)
            Path(config["dispatch_jobs"]).write_text(
                self.dispatch_row(slug="external", attempt="att-external", metadata={
                    "artifact_root": external,
                    "route_file": route_path, "route_id": route["route_id"],
                    "route_hash": route["route_hash"], "route_node": "test",
                }), encoding="utf-8")
            evidence = base / "external.json"
            self.assertTrue(Q.publish(str(evidence), config)["proven"])
            replacement = base / "same-route.json"
            replacement.write_bytes(route_path.read_bytes())
            replacement.replace(route_path)
            checked = Q.validate(str(evidence), allow_fixture=True)
            self.assertFalse(checked["proven"], checked)

    def test_explicit_root_symlink_alias_is_physical_and_retarget_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            target = Path(config["artifact_root"])
            external = self.artifact_root(base, "external")
            alias = base / "root-alias"
            alias.symlink_to(external, target_is_directory=True)
            Path(config["dispatch_jobs"]).write_text(
                self.dispatch_row(slug="alias", attempt="att-alias",
                                  metadata={"artifact_root": alias}), encoding="utf-8")
            evidence = base / "alias.json"
            value = Q.publish(str(evidence), config)
            self.assertTrue(value["proven"], value)
            alias.unlink(); alias.symlink_to(target, target_is_directory=True)
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])

    def test_root_retarget_during_observation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            target = Path(config["artifact_root"])
            external = self.artifact_root(base, "external")
            alias = base / "root-alias"
            alias.symlink_to(external, target_is_directory=True)
            Path(config["dispatch_jobs"]).write_text(
                self.dispatch_row(slug="alias", attempt="att-alias",
                                  metadata={"artifact_root": alias}), encoding="utf-8")
            original = Q._attribution_snapshot
            calls = [0]

            def retarget(*args, **kwargs):
                value = original(*args, **kwargs)
                calls[0] += 1
                if calls[0] == 1:
                    alias.unlink(); alias.symlink_to(target, target_is_directory=True)
                return value

            with patch.object(Q, "_attribution_snapshot", side_effect=retarget):
                value = Q.publish(str(base / "retarget.json"), config)
            self.assertFalse(value["observation_valid"], value)
            self.assertEqual(value["reason"], "source-changed-during-observation")

    def test_live_cwd_target_ignores_ambient_artifact_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = self.artifact_root(base, "target")
            ambient = self.artifact_root(base, "ambient")
            jobs = base / "jobs.log"; jobs.write_text("")
            index = base / "resource-runs.index.json"
            index.write_text('{"schema_version":1,"registries":{}}')

            def resolve(_argv, **kwargs):
                self.assertNotIn("AGENT_ARTIFACT_ROOT", kwargs["env"])
                return str(target) + "\n"

            env = {"AGENT_ARTIFACT_ROOT": str(ambient), "AGENT_DISPATCH_JOBS": str(jobs),
                   "AGENT_RESOURCE_RUN_INDEX": str(index), "HOME": str(base)}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(Q.subprocess, "check_output", side_effect=resolve), \
                    patch.object(Q.RESOURCES, "agent_home", return_value=base / "agent"):
                live = Q.live_config(str(base))
            self.assertEqual(live["artifact_root"], str(target))
            self.assertEqual(live["cwd"], str(base.resolve()))

    def test_missing_registry_is_sealed_skip_and_preserves_live_neighbor(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            missing = base / "gone" / "registry.json"
            self.indexed(config, missing)
            evidence = base / "missing.json"
            value = Q.publish(str(evidence), config)
            self.assertTrue(value["proven"], value)
            self.assertIn(str(missing), [r["path"] for r in value["sources"]["jobs"]["files"]
                                        if r["kind"] == "missing"])
            self.assertEqual(value["sources"]["jobs"]["diagnostics"][0]["kind"], "missing-registry")
            self.assertTrue(Q.validate(str(evidence), allow_fixture=True)["proven"])
            missing.parent.mkdir(); missing.write_text('{"schema_version":1,"runs":{}}')
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])
            identity = Q.RESOURCES.proc_identity(os.getpid())
            missing.write_text(json.dumps({"schema_version": 1, "runs": {
                "active": {**identity, "status": "running",
                           "artifact_root": config["artifact_root"]}}}))
            self.indexed(config, missing, base / "another-missing.json")
            live = Q.publish(str(base / "live.json"), config)
            self.assertTrue(live["observation_valid"], live)
            self.assertEqual(live["open_jobs"], 1)
            self.assertFalse(live["proven"])

    def test_dangling_registry_is_not_missing_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            link = base / "link"; link.symlink_to(base / "absent")
            for path in (link, link / "child.json"):
                self.indexed(config, path)
                self.assertFalse(Q.publish(str(base / "bad.json"), config)["observation_valid"])

    def test_registry_seen_only_by_scan_cannot_disappear_between_bookends(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            source = base / "transient.json"; self.indexed(config, source)
            original = Q.RESOURCES.scan
            def transient(*args, **kwargs):
                source.write_text('{"schema_version":1,"runs":{}}')
                try:
                    return original(*args, **kwargs)
                finally:
                    source.unlink()
            with patch.object(Q.RESOURCES, "scan", transient):
                value = Q.publish(str(base / "transient-evidence.json"), config)
            self.assertFalse(value["observation_valid"], value)
            self.assertEqual(value["reason"], "source-changed-during-observation")

    def test_registry_permission_corruption_and_fifo_fail_closed_with_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            source = base / "source.json"; self.indexed(config, source)
            for text in ('{', '[]', '{"schema_version":99,"runs":{}}'):
                source.write_text(text)
                value = Q.publish(str(base / "bad.json"), config)
                self.assertFalse(value["observation_valid"], value)
                self.assertEqual(value["source_diagnostics"][0]["path"], str(source))
            original = os.open
            def denied(path, *args, **kwargs):
                if Path(path) == source:
                    raise PermissionError("fixture denied")
                return original(path, *args, **kwargs)
            with patch.object(os, "open", denied):
                value = Q.publish(str(base / "denied.json"), config)
            self.assertFalse(value["proven"])
            self.assertEqual(value["source_diagnostics"][0]["path"], str(source))
            source.unlink(); os.mkfifo(source)
            value = Q.publish(str(base / "fifo.json"), config)
            self.assertFalse(value["proven"])

    def test_valid_registry_symlink_is_sealed_and_retargeting_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            a = base / "a.json"; b = base / "b.json"
            for p in (a, b):
                p.write_text('{"schema_version":1,"runs":{}}')
            link = base / "link.json"; link.symlink_to(a); self.indexed(config, link)
            evidence = base / "evidence.json"
            self.assertTrue(Q.publish(str(evidence), config)["proven"])
            link.unlink(); link.symlink_to(b)
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])

    def authority_fixture(self, base):
        config = self.fixture(base)
        peer = base / "peer"; peer.mkdir()
        (peer / "jobs.log").write_text("")
        (peer / "resource-runs.index.json").write_text('{"schema_version":1,"registries":{}}')
        config["authority_roots"] = [str(base), str(peer)]
        return config, peer

    def test_both_harnesses_reject_distinct_authorities_even_when_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config, peer = self.authority_fixture(base)
            answers = []
            for selected in (base, peer):
                local = {**config, "dispatch_jobs": str(selected / "jobs.log"),
                         "resource_index": str(selected / "resource-runs.index.json")}
                value = Q.publish(str(base / (selected.name + "-evidence.json")), local)
                self.assertFalse(value["proven"], value)
                self.assertEqual(value["reason"], "observation-authority-mismatch")
                self.assertTrue(value["sources"]["authority"]["roots_read"])
                answers.append(value["source_diagnostics"])
            self.assertEqual(answers[0], answers[1])
            # A nonempty peer must not become invisible to the empty observer.
            (peer / "jobs.log").write_text("malformed but not empty")
            self.assertFalse(Q.publish(str(base / "nonempty.json"), config)["proven"])

    def test_authority_alias_is_one_source_but_new_peer_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            alias = base / "alias"; alias.symlink_to(base, target_is_directory=True)
            absent = base / "later"
            config["authority_roots"] = [str(base), str(alias), str(absent)]
            evidence = base / "proof.json"
            self.assertTrue(Q.publish(str(evidence), config)["proven"])
            self.assertTrue(Q.validate(str(evidence), allow_fixture=True)["proven"])
            absent.mkdir(); (absent / "jobs.log").write_text("")
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])

    def test_authority_equal_bytes_replacement_invalidates_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            evidence = base / "proof.json"
            self.assertTrue(Q.publish(str(evidence), config)["proven"])
            replacement = base / "replacement.log"; replacement.write_text("")
            replacement.replace(config["dispatch_jobs"])
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])

    def test_live_authority_candidates_include_both_harnesses_and_keep_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            env = {"HOME": str(base / "user"), "CODEX_HOME": str(base / "private-codex"),
                   "XDG_STATE_HOME": str(base / "state"),
                   "AGENT_DISPATCH_JOBS": config["dispatch_jobs"],
                   "AGENT_RESOURCE_RUN_INDEX": config["resource_index"]}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(Q.subprocess, "check_output", return_value=config["artifact_root"]), \
                    patch.object(Q.RESOURCES, "agent_home", return_value=base / "agent"):
                live = Q.live_config()
                self.assertEqual(live["dispatch_jobs"], config["dispatch_jobs"])
                roots = set(live["authority_roots"])
                self.assertIn(str(base / "state" / "hearting" / "dispatch"), roots)
                self.assertIn(str(base / "user" / ".codex" / ".harness" / "dispatch"), roots)
                self.assertIn(str(base / "private-codex" / ".harness" / "dispatch"), roots)
                self.assertEqual(os.environ["AGENT_DISPATCH_JOBS"], config["dispatch_jobs"])

    def test_authority_unreadable_peer_and_malformed_roots_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config, peer = self.authority_fixture(base)
            original = os.open
            def denied(path, *args, **kwargs):
                if Path(path) == peer / "jobs.log":
                    raise PermissionError("fixture peer denied")
                return original(path, *args, **kwargs)
            with patch.object(os, "open", denied):
                result = Q.publish(str(base / "denied.json"), config)
            self.assertEqual(result["reason"], "observation-authority-unverifiable")
            self.assertEqual(result["source_diagnostics"][0]["path"], str(peer / "jobs.log"))
            for roots in ([], ["relative"], [str(base / "..")], "not-a-list"):
                bad = {**config, "authority_roots": roots}
                self.assertFalse(Q.publish(str(base / "bad.json"), bad)["proven"])

    def test_authority_change_during_observation_and_scope_forgery_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); config = self.fixture(base)
            original = Q._dispatch_rows
            def replace_after_read(jobs):
                rows = original(jobs)
                replacement = base / "replace.log"; replacement.write_text("")
                replacement.replace(jobs)
                return rows
            with patch.object(Q, "_dispatch_rows", replace_after_read):
                value = Q.publish(str(base / "changed.json"), config)
            self.assertEqual(value["reason"], "source-changed-during-observation")
            evidence = base / "proof.json"; value = Q.publish(str(evidence), config)
            value["scope"] = "live"; evidence.write_text(json.dumps(value))
            self.assertFalse(Q.validate(str(evidence), allow_fixture=True)["proven"])

    def fixture(self, base: Path):
        artifact_root = base / "artifacts"
        (artifact_root / ".runtime" / "routes").mkdir(parents=True)
        index = base / "resource-runs.index.json"
        index.write_text(json.dumps({"schema_version": 1, "registries": {}}), encoding="utf-8")
        jobs = base / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        return Q.fixture_config(str(artifact_root), str(index), str(jobs))

    def test_lock_is_ownership_not_path_presence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            lock = Path(config["lock_path"])
            lock.touch()
            empty = Q.publish(str(base / "empty.json"), config)
            self.assertFalse(empty["lock_present"])
            self.assertTrue(empty["proven"])
            lock.write_text("stale-owner\n", encoding="utf-8")
            stale = Q.publish(str(base / "stale.json"), config)
            self.assertFalse(stale["lock_present"])
            self.assertTrue(stale["proven"])
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = Q.publish(str(base / "held.json"), config)
                self.assertTrue(held["lock_present"])
                self.assertFalse(held["proven"])
                self.assertEqual(held["pending"], 1)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def test_lock_malformed_or_changing_observation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            lock = Path(config["lock_path"])
            lock.write_bytes(b"bad\x00owner")
            result = Q.publish(str(base / "bad.json"), config)
            self.assertFalse(result["observation_valid"])
            self.assertFalse(result["proven"])

            original = Q._source_snapshots
            calls = [0]
            def changing(current):
                calls[0] += 1
                value = original(current)
                if calls[0] == 2:
                    Path(current["lock_path"]).touch()
                return value
            Q._source_snapshots = changing
            try:
                changed = Q.publish(str(base / "changed.json"), config)
            finally:
                Q._source_snapshots = original
            self.assertFalse(changed["observation_valid"])
            self.assertFalse(changed["proven"])

    def test_zero_pair_is_independent_atomic_and_brackets_fold(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            now = datetime.now(timezone.utc)
            before = base / "before.json"
            after = base / "after.json"
            first = Q.publish(str(before), config, now - timedelta(seconds=2))
            second = Q.publish(str(after), config, now)
            self.assertTrue(first["proven"] and second["proven"])
            self.assertEqual(first["pending"], sum(first[key] for key in Q.COUNT_KEYS))
            self.assertNotEqual(first["observation_id"], second["observation_id"])
            self.assertFalse(list(base.glob("*.tmp")))
            proof = Q.pair(
                str(before), str(after),
                (now - timedelta(seconds=1.5)).isoformat(),
                (now - timedelta(seconds=.5)).isoformat(),
                now=now, allow_fixture=True,
            )
            self.assertTrue(proof["proven"], proof)
            self.assertFalse(Q.pair(str(before), str(before), now.isoformat(), now.isoformat(),
                                    now=now, allow_fixture=True)["proven"])

    def test_each_open_dimension_prevents_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            now = datetime.now(timezone.utc)
            route = Path(config["artifact_root"]) / ".runtime" / "routes" / "rt-open.json"
            route.write_text(json.dumps({"route_id": "rt-open", "nodes": []}), encoding="utf-8")
            route_payload = Q.publish(str(base / "route.json"), config, now)
            self.assertEqual(route_payload["open_routes"], 1)
            self.assertFalse(Q.validate(str(base / "route.json"), now=now, allow_fixture=True)["proven"])
            route.unlink()

            identity = Q.RESOURCES.proc_identity(os.getpid())
            registry = base / "resource.json"
            registry.write_text(json.dumps({"schema_version": 1, "runs": {
                "unrelated": {**identity, "status": "running", "started_at": now.timestamp(),
                              "artifact_root": config["artifact_root"]}
            }}), encoding="utf-8")
            Path(config["resource_index"]).write_text(json.dumps({"schema_version": 1, "registries": {
                "fixture": {"path": str(registry), "registered_at": now.timestamp(), "updated_at": now.timestamp()}
            }}), encoding="utf-8")
            job_payload = Q.publish(str(base / "job.json"), config, now)
            self.assertEqual(job_payload["open_jobs"], 1)
            self.assertFalse(job_payload["proven"])
            Path(config["resource_index"]).write_text(json.dumps({"schema_version": 1, "registries": {}}), encoding="utf-8")

            Path(config["dispatch_jobs"]).write_text(
                "2026-08-21T00:00:00Z\topen\t/repo\t/worktree\tfixture\t"
                "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,"
                f"attempt_id=att-fixture-open,artifact_root={config['artifact_root']}\n",
                encoding="utf-8")
            dispatch_payload = Q.publish(str(base / "dispatch.json"), config, now)
            self.assertEqual(dispatch_payload["open_dispatch_attempts"], 1, dispatch_payload)
            self.assertFalse(dispatch_payload["proven"])

    def test_missing_malformed_stale_future_offset_counts_and_changes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            now = datetime.now(timezone.utc)
            evidence = base / "evidence.json"
            original = Q.publish(str(evidence), config, now)
            self.assertTrue(Q.validate(str(evidence), now=now, allow_fixture=True)["proven"])

            cases = []
            legacy = dict(original); legacy["schema_version"] = 3; cases.append(legacy)
            malformed = dict(original); malformed["schema_version"] = 999; cases.append(malformed)
            stale = dict(original); stale["observed_at"] = (now - timedelta(hours=1)).isoformat(); cases.append(stale)
            future = dict(original); future["observed_at"] = (now + timedelta(minutes=2)).isoformat(); cases.append(future)
            offsetless = dict(original); offsetless["observed_at"] = now.replace(tzinfo=None).isoformat(); cases.append(offsetless)
            negative = dict(original); negative["open_jobs"] = -1; cases.append(negative)
            wrong_sum = dict(original); wrong_sum["pending"] = 1; cases.append(wrong_sum)
            for payload in cases:
                evidence.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(Q.validate(str(evidence), now=now, allow_fixture=True)["proven"], payload)

            evidence.write_text(json.dumps(original), encoding="utf-8")
            Path(config["dispatch_jobs"]).write_text("malformed\n", encoding="utf-8")
            self.assertFalse(Q.validate(str(evidence), now=now, allow_fixture=True)["proven"])
            self.assertFalse(Q.validate(str(base / "missing.json"), now=now, allow_fixture=True)["proven"])

    def test_missing_authoritative_source_publishes_false(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            Path(config["resource_index"]).unlink()
            result = Q.publish(str(base / "evidence.json"), config)
            self.assertFalse(result["observation_valid"])
            self.assertFalse(result["proven"])
            self.assertEqual(result["pending"], 0)

    def test_unrelated_root_json_is_evidence_but_malformed_route_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            artifact_root = Path(config["artifact_root"])
            (artifact_root / "inventory.json").write_text('{"kind":"not-a-route"}', encoding="utf-8")
            valid = Q.publish(str(base / "valid.json"), config)
            self.assertTrue(valid["observation_valid"], valid)
            self.assertTrue(valid["proven"], valid)
            # SD-OPEN-54 (#15): the gate ledger beside a route record is a typed
            # sidecar, and a stray non-route basename is non-blocking evidence.
            (artifact_root / ".runtime" / "routes" / "rt-0123456789abcdef.gate-release.json").write_text(
                '{"schema_version":1,"route_id":"rt-0123456789abcdef","gate_releases":[]}', encoding="utf-8")
            (artifact_root / ".runtime" / "routes" / "notes.json").write_text('{"kind":"note"}', encoding="utf-8")
            still_valid = Q.publish(str(base / "still-valid.json"), config)
            self.assertTrue(still_valid["observation_valid"], still_valid)
            self.assertTrue(still_valid["proven"], still_valid)
            (artifact_root / ".runtime" / "routes" / "rt-fedcba9876543210.json").write_text('{', encoding="utf-8")
            invalid = Q.publish(str(base / "invalid.json"), config)
            self.assertFalse(invalid["observation_valid"], invalid)
            self.assertFalse(invalid["proven"], invalid)


if __name__ == "__main__":
    unittest.main()
