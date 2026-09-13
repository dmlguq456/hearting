#!/usr/bin/env python3
"""W7C producer lifecycle tests for `artifact_producer.py`.

Every fixture uses an isolated temporary artifact root and `AGENT_HOME`; the
real canonical root, registry, and routes directory are never touched.
"""
import importlib.util
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_identity as idm  # noqa: E402
import artifact_lifecycle as L  # noqa: E402
import artifact_manifest as m  # noqa: E402
import artifact_producer as P  # noqa: E402
import dispatch_contract as D  # noqa: E402

_P = Path(__file__).with_name("capability-route.py")
_S = importlib.util.spec_from_file_location("route_for_producer_test", _P)
R = importlib.util.module_from_spec(_S)
_S.loader.exec_module(R)

ALL = [
    "atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
    "no-artifact-handoff", "no-independent-verifier", "focused-verification",
]
REPO_ID = "repo_" + "a" * 32
ROOT_ID = "root_" + "b" * 32


def gate_evidence():
    return {
        "spec_read": {"satisfied": True, "source": "fixture"},
        "drift_verdict": "within-spec", "workflow_mode": "tracked",
        "artifact_guard": {"satisfied": True, "source": "fixture"},
    }


def registered_headless():
    # Two supported harnesses, not one: quick now compiles a cross-harness
    # frame pair, so a single-candidate evidence set is no longer a valid quick
    # route at all (`quick-frame-cross-harness-unavailable`). Same shape the
    # canary's own fixture uses in `tools/artifact-producer-canary.py`.
    return {"candidates": [{
        "harness": "codex", "transport": "headless", "surface": "registered-headless",
        "status": "supported", "probe_source": "fixture-probe", "probe_time": "2026-07-20T00:00:00Z",
    }, {
        "harness": "claude", "transport": "headless", "surface": "registered-headless",
        "status": "supported", "probe_source": "fixture-probe", "probe_time": "2026-07-20T00:00:00Z",
    }]}


def nested(parent="codex", child="codex"):
    sandbox = R.WRAPPER_PARENT_SANDBOXES[parent][0] if parent in R.WRAPPER_PARENT_SANDBOXES else "workspace-write"
    return {
        "parent_harness": parent, "parent_transport": "headless", "parent_sandbox": sandbox,
        "child_harness": child, "launch_authority": "conductor", "status": "supported",
        "probe_source": "fixture-probe", "probe_time": "2026-07-16T00:00:00Z", "failure_class": "",
        "checked_worktree": str(R.ROOT.resolve()), "failure_scope": "none",
        "codex_command": "ok" if child == "codex" else "not-applicable", "retry_on_isolated_worktree": 0,
    }


def dispatch_evidence():
    return {"tuples": [nested()], "native_subagent": [{
        "harness": "codex", "transport": "headless", "execution_surface": "codex-native-subagent",
        "registered_worker": False, "status": "supported", "check_source": "fixture-native-check",
    }]}


def compile_for(intensity, root, capability="autopilot-code", mode="dev", *,
                slug="w7i-test", gate_source="fixture", campaign_key=None, parent_cycle_id=None):
    gate = gate_evidence()
    gate["spec_read"]["source"] = gate_source
    common = dict(cwd=R.ROOT, artifact_root=root, tracking="tracked",
                  tracked_gate_evidence=gate, slug=slug,
                  campaign_key=campaign_key, parent_cycle_id=parent_cycle_id)
    if intensity == "direct":
        return R.compile_route(capability, mode, "direct", predicates=ALL, transport=None,
                               inline_reason="atomic-direct", **common)
    if intensity == "quick":
        return R.compile_route(capability, mode, "quick", predicates=[], transport=None,
                               registered_headless_evidence=registered_headless(), **common)
    return R.compile_route(capability, mode, intensity, predicates=[], transport="headless",
                           dispatch_evidence=dispatch_evidence(), **common)


class ProducerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "artifact-root"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs = Path(self._tmp.name) / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        home = Path(self._tmp.name) / "agent-home"
        (home / "core").mkdir(parents=True, exist_ok=True)
        (home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self._env = {k: os.environ.get(k) for k in (
            "AGENT_HOME", "AGENT_DISPATCH_JOBS", "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_ROOT")}
        os.environ["AGENT_HOME"] = str(home)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        for key in ("AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_ROOT"):
            os.environ.pop(key, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def activate(self):
        return P.activate(self.root, repository_id=REPO_ID, artifact_root_id=ROOT_ID,
                          w7={"campaign_id": "camp_" + "c" * 32})

    def route(self, intensity="direct", capability="autopilot-code", mode="dev", **compile_kw):
        route = compile_for(intensity, self.root, capability, mode, **compile_kw)
        binding = L.admit_runtime_route(self.root, route)
        return route, Path(binding.route_file)

    def close(self, route, route_file):
        evidence = Path(self._tmp.name) / f"evidence-{route['route_id']}.txt"
        evidence.write_text("terminal evidence\n", encoding="utf-8")
        for node in route["nodes"]:
            if node.get("terminal") is not True:
                continue
            if node.get("dispatch_depth", 0) == 0:
                R.write_completion_marker(route, node, node["id"], evidence)
                continue
            metadata = {
                "attempt_schema_version": 2, "dispatch_depth": node["dispatch_depth"],
                "transport": "headless", "execution_surface": "registered-headless",
                "registered_worker": "1", "fallback_hop": "same-harness-headless",
            }
            R.write_completion_marker(route, node, node["id"], evidence,
                                      attempt_id=f"att-fixture-{node['id']}", attempt_metadata=metadata)
        outcome, _ = R.close_route(route, route_file, commit="a" * 40, summary="fixture")
        self.assertTrue(outcome["terminal_gate_proven"], outcome)

    def begin(self, intensity="direct", capability="autopilot-code", mode="dev", **kw):
        route, route_file = self.route(intensity, capability, mode)
        result = P.begin(self.root, route_file=route_file, capability=capability, intensity=intensity, **kw)
        return route, route_file, result

    def write_output(self, result, rel="plans/cycle/plan.md", data=b"plan body\n"):
        target = Path(result["cycle_dir"]) / "artifacts" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def seed_legacy(self, rel="plans/legacy.md", data="legacy body\n"):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding="utf-8")
        return target


class ActivateAndBeginTest(ProducerTestBase):
    def test_begin_before_activation_is_legacy_compat(self):
        self.seed_legacy()
        route, route_file, result = self.begin()
        self.assertEqual(result["status"], "legacy-compat")
        self.assertEqual(result["layout"], "legacy")
        self.assertFalse((self.root / "campaigns").exists())
        self.assertEqual(result["legacy_fallback"]["level"], "warn")
        self.assertEqual(result["legacy_fallback"]["reason"], "cutover-inactive-legacy-root")
        self.assertEqual(result["legacy_fallback"]["override"]["status"], "absent")

    def test_begin_require_cycle_fails_closed_when_inactive(self):
        route, route_file = self.route()
        self.seed_legacy()
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                    require_cycle=True)
        self.assertEqual(ctx.exception.code, "cutover-inactive")

    def test_activate_freezes_identity_and_is_idempotent(self):
        first = self.activate()
        self.assertEqual(first["status"], "activated")
        self.assertEqual(first["identity"], "created")
        identity = L.read_root_identity(self.root)
        self.assertEqual((identity.repository_id, identity.artifact_root_id), (REPO_ID, ROOT_ID))
        again = self.activate()
        self.assertEqual(again["status"], "already-active")
        with self.assertRaises(P.ProducerError) as ctx:
            P.activate(self.root, repository_id=REPO_ID, artifact_root_id="root_" + "f" * 32)
        self.assertEqual(ctx.exception.code, "identity-conflict")

    def test_begin_issues_ids_before_first_write(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="w7i-naming")
        self.assertEqual(result["status"], "begun")
        self.assertTrue(idm.is_well_formed(result["campaign_id"], "campaign"))
        self.assertTrue(idm.is_well_formed(result["cycle_id"], "cycle"))
        self.assertTrue(idm.is_well_formed(result["producer_id"], "producer"))
        cycle_dir = Path(result["cycle_dir"])
        campaign = P.read_campaign(self.root, result["campaign_id"])
        self.assertEqual(cycle_dir.parent, self.root / "campaigns" / campaign["locator"])
        self.assertNotIn("cycles", cycle_dir.relative_to(self.root).parts)
        record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertEqual(cycle_dir.name, f"{record['started_on'][:10]}_w7i-test")
        self.assertTrue((cycle_dir / "artifacts").is_dir())
        self.assertEqual(sorted(os.listdir(cycle_dir)), [".cycle.json", "artifacts"])
        binding = json.loads((cycle_dir / ".cycle.json").read_text())
        self.assertEqual(
            (binding["campaign_id"], binding["cycle_id"]),
            (result["campaign_id"], result["cycle_id"]),
        )
        self.assertEqual(result["env"]["AGENT_ARTIFACT_OUTPUT_DIR"], str(cycle_dir / "artifacts"))
        record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertEqual(record["state"], "open")
        self.assertEqual(record["route_id"], route["route_id"])
        self.assertEqual(campaign["cycles"], [result["cycle_id"]])
        for named in (campaign, record):
            self.assertEqual(named["slug"], "w7i-test")
            self.assertEqual(named["title"], "w7i-test")
            self.assertEqual(named["slug_source"], "route")
            self.assertEqual(named["locator_suffix"], "")
        self.assertEqual(json.loads((self.root / "campaigns" / "INDEX.json").read_text())[result["cycle_id"]],
                         cycle_dir.relative_to(self.root).as_posix())

    def test_slug_normalization_truncation_and_legacy_derivation_are_recorded(self):
        self.activate()
        raw = "  W7I /// " + "Very Long Name " * 8
        route, route_file = self.route(slug=raw)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertEqual(len(record["slug"]), 48)
        self.assertTrue(record["slug_truncated"])
        self.assertRegex(record["slug"], r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        legacy, legacy_file = self.route(slug=None, gate_source="legacy-route")
        derived = P.begin(
            self.root, route_file=legacy_file, capability="autopilot-code", intensity="direct",
            campaign_key="legacy-derived", goal="Legacy Goal For Naming",
        )
        legacy_record = P.read_cycle_record(self.root, derived["cycle_id"])
        legacy_campaign = P.read_campaign(self.root, derived["campaign_id"])
        for named in (legacy_campaign, legacy_record):
            self.assertEqual(named["slug"], "legacy-goal-for-naming")
            self.assertEqual(named["slug_source"], "derived-legacy-route")

    def test_collision_suffix_is_smallest_and_resume_keeps_it(self):
        self.activate()
        results = []
        routes = []
        for ordinal in range(3):
            route, route_file = self.route(slug="same name", gate_source=f"fixture-{ordinal}")
            result = P.begin(
                self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                campaign_key="same-campaign",
            )
            routes.append((route, route_file))
            results.append(result)
        records = [P.read_cycle_record(self.root, row["cycle_id"]) for row in results]
        self.assertEqual([row["locator_suffix"] for row in records], ["", "-2", "-3"])
        day = records[0]["started_on"][:10]
        self.assertEqual([Path(row["cycle_dir"]).name for row in results], [
            f"{day}_same-name", f"{day}_same-name-2", f"{day}_same-name-3",
        ])
        resumed = P.begin(
            self.root, route_file=routes[2][1], capability="autopilot-code", intensity="direct")
        self.assertEqual(resumed["cycle_id"], results[2]["cycle_id"])
        self.assertEqual(P.read_cycle_record(self.root, resumed["cycle_id"])["locator_suffix"], "-3")

    def test_locator_index_recovers_deleted_corrupt_stale_and_renamed_path(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        original = Path(result["cycle_dir"])
        index_path = self.root / "campaigns" / "INDEX.json"
        markdown_path = self.root / "campaigns" / "INDEX.md"
        for payload in (None, "{", json.dumps({result["cycle_id"]: "campaigns/not-real"})):
            if payload is None:
                index_path.unlink()
            else:
                index_path.write_text(payload, encoding="utf-8")
            self.assertEqual(P.artifact_locator.resolve_path(self.root, result["cycle_id"]), original)
        for payload in (None, "corrupt\n", "# stale\n"):
            if payload is None:
                markdown_path.unlink()
            else:
                markdown_path.write_text(payload, encoding="utf-8")
            self.assertEqual(P.artifact_locator.resolve_path(self.root, result["cycle_id"]), original)
        renamed = original.with_name(original.name + "-manual")
        original.rename(renamed)
        self.assertEqual(P.artifact_locator.resolve_path(self.root, result["cycle_id"]), renamed)
        rebuilt = json.loads(index_path.read_text())
        self.assertEqual(rebuilt[result["cycle_id"]], renamed.relative_to(self.root).as_posix())

    def test_open_cycle_rename_resolves_writes_and_resume_without_reidentifying(self):
        self.activate()
        route, route_file, result = self.begin()
        original = Path(result["cycle_dir"])
        renamed = original.with_name(original.name + "-operator-name")
        original.rename(renamed)
        (self.root / "campaigns" / "INDEX.json").unlink()
        (self.root / "campaigns" / "INDEX.md").write_text("broken\n", encoding="utf-8")

        self.assertEqual(P.artifact_locator.resolve_path(self.root, result["cycle_id"]), renamed)
        target = renamed / "artifacts" / "spec" / "prd.md"
        verdict = P.check_write(self.root, target)
        self.assertEqual((verdict["verdict"], verdict["cycle_id"]), ("allow", result["cycle_id"]))
        self.assertEqual(P.cycle_bucket(self.root, target), ("spec", result["cycle_id"]))
        resumed = P.begin(
            self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(resumed["cycle_id"], result["cycle_id"])
        self.assertEqual(Path(resumed["cycle_dir"]), renamed)
        self.assertEqual(resumed["env"]["AGENT_ARTIFACT_CYCLE_DIR"], str(renamed))

    def test_multiple_open_cycle_renames_and_name_swap_preserve_each_id(self):
        self.activate()
        first_route, first_file = self.route(slug="first", gate_source="first")
        first = P.begin(
            self.root, route_file=first_file, capability="autopilot-code", intensity="direct",
            campaign_key="rename-set")
        second_route, second_file = self.route(slug="second", gate_source="second")
        second = P.begin(
            self.root, route_file=second_file, capability="autopilot-code", intensity="direct",
            campaign_key="rename-set")
        first_path, second_path = Path(first["cycle_dir"]), Path(second["cycle_dir"])
        temporary = first_path.with_name("temporary-swap")
        first_path.rename(temporary)
        second_path.rename(first_path)
        temporary.rename(second_path)

        self.assertEqual(P.artifact_locator.resolve_path(self.root, first["cycle_id"]), second_path)
        self.assertEqual(P.artifact_locator.resolve_path(self.root, second["cycle_id"]), first_path)
        self.assertEqual(
            Path(P.begin(
                self.root, route_file=first_file, capability="autopilot-code", intensity="direct",
            )["cycle_dir"]),
            second_path,
        )
        self.assertEqual(
            Path(P.begin(
                self.root, route_file=second_file, capability="autopilot-code", intensity="direct",
            )["cycle_dir"]),
            first_path,
        )

    def test_index_pair_rolls_back_when_second_replace_fails(self):
        self.activate()
        _route, _route_file, result = self.begin()
        index_path = self.root / "campaigns" / "INDEX.json"
        markdown_path = self.root / "campaigns" / "INDEX.md"
        before = (index_path.read_bytes(), markdown_path.read_bytes())
        cycle_dir = Path(result["cycle_dir"])
        renamed = cycle_dir.with_name(cycle_dir.name + "-during-write")
        cycle_dir.rename(renamed)
        real_atomic_write = P.artifact_locator._atomic_write

        def fail_markdown(path, data):
            if Path(path).name == "INDEX.md":
                raise OSError("injected second index write failure")
            return real_atomic_write(path, data)

        with mock.patch.object(P.artifact_locator, "_atomic_write", side_effect=fail_markdown):
            with self.assertRaises(OSError):
                P.artifact_locator.rebuild_indexes(self.root)
        self.assertEqual((index_path.read_bytes(), markdown_path.read_bytes()), before)
        self.assertEqual(P.artifact_locator.resolve_path(self.root, result["cycle_id"]), renamed)

    def test_modified_route_slug_is_rejected_before_artifact_creation(self):
        self.activate()
        route, route_file = self.route(slug="original")
        route["slug"] = "tampered"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(ctx.exception.code, "route-invalid")
        self.assertEqual(list(P.artifact_locator.iter_campaign_dirs(self.root)), [])

    def test_begin_refuses_an_active_root_resplit_claim(self):
        self.activate()
        _route, route_file = self.route()
        lock_path = P.producer_dir(self.root) / "resplit.lock"
        lock_path.write_text(json.dumps({"lump_cycle_id": "cyc_fixture"}), encoding="utf-8")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(ctx.exception.code, "resplit-in-progress")
        self.assertEqual(list(P.artifact_locator.iter_campaign_dirs(self.root)), [])

    def test_mutated_record_locators_cannot_escape_artifact_root(self):
        self.activate()
        route, route_file, result = self.begin()
        cycle_record = P.read_cycle_record(self.root, result["cycle_id"])
        cycle_record["locator"] = "../../escaped-cycle"
        P.cycle_record_path(self.root, result["cycle_id"]).write_text(
            json.dumps(cycle_record), encoding="utf-8")
        campaign = P.read_campaign(self.root, result["campaign_id"])
        campaign["locator"] = "../../escaped-campaign"
        campaign_path = Path(result["cycle_dir"]).parent / "campaign.json"
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")

        resumed = P.begin(
            self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(Path(resumed["cycle_dir"]), Path(result["cycle_dir"]))
        self.assertFalse((self.root.parent / "escaped-cycle").exists())
        self.assertFalse((self.root.parent / "escaped-campaign").exists())

    def test_record_lookups_reject_mismatched_and_path_like_ids(self):
        self.activate()
        requested = "camp_" + "1" * 32
        different = "camp_" + "2" * 32
        fallback = self.root / "campaigns" / requested
        fallback.mkdir(parents=True)
        (fallback / "campaign.json").write_text(
            json.dumps({"campaign_id": different, "cycles": []}), encoding="utf-8")
        self.assertIsNone(P.read_campaign(self.root, requested))
        self.assertIsNone(P.read_cycle_record(self.root, "../outside"))
        self.assertIsNone(P.artifact_locator.read_cycle_record(self.root, "../outside"))
        with self.assertRaises(P.ProducerError) as ctx:
            P.cycle_record_path(self.root, "../outside")
        self.assertEqual(ctx.exception.code, "cycle-id-invalid")

    def test_begin_is_idempotent_per_route(self):
        self.activate()
        route, route_file, first = self.begin()
        second = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(second["status"], "resumed")
        self.assertEqual(second["cycle_id"], first["cycle_id"])

    def test_begin_accepts_a_bare_route_id(self):
        """A quick owner is told only the route id; it must not have to guess the path."""
        self.activate()
        route, route_file = self.route()
        result = P.begin(
            self.root, route_file=route["route_id"],
            capability="autopilot-code", intensity="direct",
        )
        self.assertEqual(result["status"], "begun")
        self.assertEqual(
            P.resolve_route_argument(self.root, route["route_id"]).resolve(),
            route_file.resolve(),
        )

    def test_unresolvable_route_id_still_reports_route_unreadable(self):
        self.activate()
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(
                self.root, route_file="rt-deadbeefdeadbeef",
                capability="autopilot-code", intensity="direct",
            )
        self.assertEqual(ctx.exception.code, "route-unreadable")

    def test_an_explicit_path_is_never_reinterpreted_as_an_id(self):
        self.activate()
        route, route_file = self.route()
        self.assertEqual(
            P.resolve_route_argument(self.root, route_file), Path(route_file)
        )

    def test_begin_rejects_capability_and_intensity_mismatch(self):
        self.activate()
        route, route_file = self.route("direct")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-spec", intensity="direct")
        self.assertEqual(ctx.exception.code, "route-capability-mismatch")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="quick")
        self.assertEqual(ctx.exception.code, "route-intensity-mismatch")

    def test_begin_rejects_closed_route(self):
        self.activate()
        route, route_file = self.route()
        self.close(route, route_file)
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(ctx.exception.code, "route-already-closed")

    def test_stage_worker_joins_owner_cycle_by_node(self):
        self.activate()
        route, route_file, owner = self.begin("standard")
        node_id = route["nodes"][0]["id"]
        stage_capability = "code-plan"
        stage = P.begin(self.root, route_file=route_file, capability=stage_capability, intensity="standard",
                        node_id=node_id)
        # Same route => same open cycle; the stage worker never issues a second lineage.
        self.assertEqual(stage["status"], "resumed")
        self.assertEqual(stage["cycle_id"], owner["cycle_id"])
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability=stage_capability, intensity="standard",
                    node_id="no-such-node")
        self.assertEqual(ctx.exception.code, "route-node-unknown")

    def test_campaign_key_reuse_and_parent_cycle(self):
        self.activate()
        route, route_file, first = self.begin(campaign_key="w7c-key")
        self.write_output(first)
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=first["cycle_id"])
        self.assertEqual(sealed["status"], "sealed")
        route2, route_file2 = self.route("quick")
        second = P.begin(self.root, route_file=route_file2, capability="autopilot-code", intensity="quick",
                         campaign_key="w7c-key", parent_cycle_id=first["cycle_id"])
        self.assertEqual(second["campaign_id"], first["campaign_id"])
        self.assertFalse(second["campaign_created"])
        self.assertEqual(P.read_cycle_record(self.root, second["cycle_id"])["parent_cycle_id"], first["cycle_id"])
        parent_before = P.read_cycle_record(self.root, second["cycle_id"])
        third = P.begin(self.root, route_file=self.route("direct", mode="debug")[1], capability="autopilot-code",
                        intensity="direct", parent_cycle_id=second["cycle_id"])
        self.assertEqual(third["campaign_id"], second["campaign_id"])
        self.assertEqual(P.read_cycle_record(self.root, third["cycle_id"])["parent_cycle_state_at_begin"], "open")
        self.assertEqual(P.read_cycle_record(self.root, second["cycle_id"]), parent_before)
        self.assertEqual(P.read_cycle_record(self.root, second["cycle_id"])["parent_cycle_state_at_begin"], "sealed")

    def test_keyless_routes_share_degraded_container_and_resume_reports_it(self):
        self.activate()
        _, first_file, first = self.begin()
        second_file = self.route(slug="another-task")[1]
        second = P.begin(self.root, route_file=second_file, capability="autopilot-code", intensity="direct")
        resumed = P.begin(self.root, route_file=first_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(first["campaign_id"], second["campaign_id"])
        self.assertNotEqual(first["cycle_id"], second["cycle_id"])
        for result in (first, second, resumed):
            self.assertTrue(result["degraded"])
            self.assertEqual(result["degraded_reason"], "campaign-unassigned")
        self.assertEqual(P.read_campaign(self.root, first["campaign_id"])["key"], "_unassigned")
        self.assertEqual(P.read_campaign(self.root, first["campaign_id"])["title"], "_unassigned")

    def test_open_parent_child_seals_only_after_parent_admission(self):
        self.activate()
        parent_route, parent_file, parent = self.begin(campaign_key="causal-stream")
        child_route, child_file = self.route(slug="followup", parent_cycle_id=parent["cycle_id"])
        child = P.begin(self.root, route_file=child_file, capability="autopilot-code", intensity="direct")
        self.write_output(parent)
        self.write_output(child)
        self.close(child_route, child_file)
        with self.assertRaises(P.ProducerError) as ctx:
            P.finalize(self.root, cycle_id=child["cycle_id"])
        self.assertEqual(ctx.exception.code, "parent-cycle-not-sealed")
        self.assertFalse((Path(child["cycle_dir"]) / "manifest.json").exists())
        self.assertEqual(P.read_cycle_record(self.root, child["cycle_id"])["state"], "open")
        self.close(parent_route, parent_file)
        self.assertEqual(P.finalize(self.root, cycle_id=parent["cycle_id"])["status"], "sealed")
        self.assertEqual(P.finalize(self.root, cycle_id=child["cycle_id"])["status"], "sealed")

    def test_route_delivers_key_and_open_parent_across_capabilities(self):
        self.activate()
        _, first_file = self.route(campaign_key="tts-v6-release")
        first = P.begin(self.root, route_file=first_file, capability="autopilot-code", intensity="direct")
        route, second_file = self.route(capability="autopilot-spec", mode="update", slug="endpoint-finding",
                                       campaign_key="tts-v6-release", parent_cycle_id=first["cycle_id"])
        before = P.read_cycle_record(self.root, first["cycle_id"])
        second = P.begin(self.root, route_file=second_file, capability="autopilot-spec", intensity="direct")
        self.assertEqual(second["campaign_id"], first["campaign_id"])
        child = P.read_cycle_record(self.root, second["cycle_id"])
        self.assertEqual(child["parent_cycle_id"], first["cycle_id"])
        self.assertEqual(child["parent_cycle_state_at_begin"], "open")
        self.assertEqual(P.read_cycle_record(self.root, first["cycle_id"]), before)
        self.assertNotIn("degraded", second)
        route["campaign_key"] = "tampered"
        with self.assertRaisesRegex(ValueError, "modified route hash"):
            R.verify_route(route)

    def test_campaign_choices_cannot_be_silently_overridden_or_rebound(self):
        self.activate()
        _, route_file = self.route(campaign_key="stream-a")
        first = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for key in ("stream-b", ""):
            with self.assertRaises(P.ProducerError) as ctx:
                P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct", campaign_key=key)
            self.assertEqual(ctx.exception.code, "route-campaign-selection-conflict")
        _, old_file = self.route(slug="old-route")
        P.begin(self.root, route_file=old_file, capability="autopilot-code", intensity="direct", campaign_key="stream-a")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=old_file, capability="autopilot-code", intensity="direct", campaign_key="new-stream")
        self.assertEqual(ctx.exception.code, "cycle-campaign-selection-conflict")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=self.route(slug="child")[1], capability="autopilot-code", intensity="direct",
                    campaign_key="nonexistent-new-key", parent_cycle_id=first["cycle_id"])
        self.assertEqual(ctx.exception.code, "campaign-key-mismatch")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=self.route(slug="id-key-conflict")[1], capability="autopilot-code", intensity="direct",
                    campaign_id=first["campaign_id"], campaign_key="nonexistent-new-key")
        self.assertEqual(ctx.exception.code, "campaign-key-mismatch")

    def test_parent_and_campaign_lifecycle_rejections(self):
        self.activate()
        _, _, first = self.begin(campaign_key="stream-a")
        parent = P.read_cycle_record(self.root, first["cycle_id"])
        route_file = self.route(slug="lifecycle-child")[1]
        for state in ("cancelled", "superseded"):
            parent["state"] = state
            P._write_cycle_record(self.root, parent, exclusive=False)
            with self.assertRaises(P.ProducerError) as ctx:
                P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct", parent_cycle_id=first["cycle_id"])
            self.assertEqual(ctx.exception.code, "parent-cycle-not-joinable")
        parent["state"] = "open"
        P._write_cycle_record(self.root, parent, exclusive=False)
        campaign = P.read_campaign(self.root, first["campaign_id"])
        campaign["state"] = "superseded"
        P._write_campaign(self.root, campaign, exclusive=False)
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct", parent_cycle_id=first["cycle_id"])
        self.assertEqual(ctx.exception.code, "campaign-not-active")


class BootstrapTest(ProducerTestBase):
    def test_begin_against_empty_root_bootstraps(self):
        route, route_file, result = self.begin()
        self.assertEqual(result["status"], "begun")
        self.assertEqual(result["layout"], "cycle")
        cutover = P.read_cutover(self.root)
        self.assertEqual(cutover["activation_kind"], "bootstrap-empty-root")
        self.assertIsNone(cutover["approval_receipt_sha256"])
        self.assertEqual(cutover["state"], "active")
        self.assertIsNotNone(L.read_root_identity(self.root))
        self.assertTrue(idm.is_well_formed(result["campaign_id"], "campaign"))
        self.assertTrue(idm.is_well_formed(result["cycle_id"], "cycle"))
        self.assertTrue(idm.is_well_formed(result["producer_id"], "producer"))

    def test_bootstrap_creates_no_legacy_bucket_directory(self):
        self.begin()
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(names, [".runtime", "campaigns"])

    def test_bootstrapped_root_denies_legacy_top_level_write(self):
        self.begin()
        verdict = P.check_write(self.root, self.root / "plans" / "x.md")
        self.assertEqual((verdict["verdict"], verdict["reason"]),
                         ("deny", "legacy-top-level-write-denied"))

    def test_begin_require_cycle_bootstraps_empty_root(self):
        route, route_file, result = self.begin(require_cycle=True)
        self.assertEqual(result["status"], "begun")

    def test_bootstrap_adopts_existing_frozen_identity(self):
        identity_path = adm._root_identity_path(self.root)
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        payload = idm.RootIdentity(
            schema_version=1, artifact_root_id=ROOT_ID, repository_id=REPO_ID,
            issued_at="2026-09-02T00:00:00Z", producer_contract_version=m.CONTRACT_VERSION,
        ).to_payload()
        identity_path.write_text(json.dumps(payload), encoding="utf-8")
        route, route_file, result = self.begin()
        self.assertEqual(result["status"], "begun")
        self.assertEqual(P.read_cutover(self.root)["identity"]["artifact_root_id"], ROOT_ID)

    def test_explicit_activate_after_bootstrap_is_already_active_without_kind_promotion(self):
        self.begin()
        identity = L.read_root_identity(self.root)
        result = P.activate(self.root, repository_id=identity.repository_id,
                            artifact_root_id=identity.artifact_root_id)
        self.assertEqual(result["status"], "already-active")
        self.assertEqual(P.read_cutover(self.root)["activation_kind"], "bootstrap-empty-root")

    def test_malformed_cutover_record_blocks_begin(self):
        route, route_file = self.route()
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text("{", encoding="utf-8")
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(ctx.exception.code, "cutover-record-malformed")

    def test_read_only_oracles_never_bootstrap(self):
        P.check_write(self.root, self.root / "plans" / "x.md")
        P.status(self.root)
        P.resolve_output_dir(self.root, "spec")
        self.assertFalse(P.cutover_path(self.root).exists())


class ClassifyRootTest(ProducerTestBase):
    def test_empty_root_is_inactive_empty(self):
        self.assertEqual(P.classify_root(self.root)["state"], "inactive-empty")

    def test_root_with_only_empty_directories_is_inactive_empty(self):
        (self.root / "plans").mkdir()
        (self.root / "spec").mkdir()
        self.assertEqual(P.classify_root(self.root)["state"], "inactive-empty")

    def test_nested_regular_file_is_inactive_with_legacy(self):
        target = self.root / "plans" / "a" / "b" / "c.md"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")
        klass = P.classify_root(self.root)
        self.assertEqual(klass["state"], "inactive-with-legacy")
        self.assertEqual(klass["legacy_top_level"], ["plans"])

    def test_campaigns_only_root_without_cutover_is_inactive_with_legacy(self):
        target = self.root / "campaigns" / "x" / "y.md"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")
        self.assertEqual(P.classify_root(self.root)["state"], "inactive-with-legacy")

    def test_symlink_counts_as_content_and_is_not_followed(self):
        outside = Path(self._tmp.name) / "outside-target"
        outside.mkdir()
        for i in range(100):
            (outside / f"f{i}.md").write_text("x", encoding="utf-8")
        (self.root / "plans").mkdir()
        os.symlink(outside, self.root / "plans" / "link")
        klass = P.classify_root(self.root, collect_legacy_top_level=True)
        self.assertEqual(klass["state"], "inactive-with-legacy")
        self.assertEqual(klass["legacy_top_level"], ["plans"])

    def test_missing_root_directory_is_inactive_empty(self):
        missing = self.root / "does-not-exist"
        self.assertEqual(P.classify_root(missing)["state"], "inactive-empty")

    def test_unreadable_cutover_is_malformed(self):
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text("{", encoding="utf-8")
        klass = P.classify_root(self.root)
        self.assertEqual((klass["state"], klass["reason"]), ("malformed", "cutover-record-unreadable"))

    def test_unknown_cutover_state_is_malformed(self):
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text(json.dumps({"state": "paused"}), encoding="utf-8")
        klass = P.classify_root(self.root)
        self.assertEqual((klass["state"], klass["reason"]), ("malformed", "cutover-schema-unknown"))

    def test_identity_conflict_is_malformed(self):
        self.activate()
        identity_path = adm._root_identity_path(self.root)
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
        payload["artifact_root_id"] = "root_" + "f" * 32
        identity_path.write_text(json.dumps(payload), encoding="utf-8")
        klass = P.classify_root(self.root)
        self.assertEqual((klass["state"], klass["reason"]), ("malformed", "identity-conflict"))

    def test_active_classification_does_not_walk_content(self):
        self.activate()
        self.seed_legacy()
        klass = P.classify_root(self.root)
        self.assertEqual(klass["state"], "active")
        self.assertEqual(klass["legacy_top_level"], [])


class LegacyFallbackTest(ProducerTestBase):
    def _override_payload(self, **overrides):
        payload = {
            "schema_version": 1, "contract": P.CONTRACT, "canonical_root": str(self.root),
            "reason": "test override", "issuer": "test", "created_at": "2026-09-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        payload.update(overrides)
        return payload

    def _write_override(self, payload):
        path = P.compat_override_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_warn_is_the_default_on_all_three_surfaces(self):
        self.seed_legacy()
        route, route_file, result = self.begin()
        self.assertEqual(result["legacy_fallback"]["level"], "warn")
        write_verdict = P.check_write(self.root, self.root / "plans" / "x.md")
        self.assertEqual(write_verdict["verdict"], "allow")
        self.assertEqual(write_verdict["legacy_fallback"]["level"], "warn")
        directory, layout = P.resolve_output_dir(self.root, "spec")
        self.assertEqual(layout, "legacy")

    def test_deny_without_override_blocks_all_three_surfaces(self):
        self.seed_legacy()
        os.environ[P.INACTIVE_FALLBACK_ENV] = "deny"
        try:
            write_verdict = P.check_write(self.root, self.root / "plans" / "x.md")
            self.assertEqual((write_verdict["verdict"], write_verdict["reason"]),
                             ("deny", "cutover-inactive-fallback-denied"))
            route, route_file = self.route()
            with self.assertRaises(P.ProducerError) as ctx:
                P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
            self.assertEqual(ctx.exception.code, "cutover-inactive-fallback-denied")
            with self.assertRaises(P.ProducerError) as ctx:
                P.resolve_output_dir(self.root, "spec")
            self.assertEqual(ctx.exception.code, "cutover-inactive-fallback-denied")
        finally:
            os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_unknown_fallback_value_fails_closed_to_deny(self):
        self.seed_legacy()
        for value in ("WARN", "maybe"):
            os.environ[P.INACTIVE_FALLBACK_ENV] = value
            try:
                write_verdict = P.check_write(self.root, self.root / "plans" / "x.md")
                self.assertEqual(write_verdict["verdict"], "deny")
            finally:
                os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_valid_override_allows_and_records(self):
        self.seed_legacy()
        self._write_override(self._override_payload())
        os.environ[P.INACTIVE_FALLBACK_ENV] = "deny"
        try:
            verdict = P.check_write(self.root, self.root / "plans" / "x.md")
            self.assertEqual(verdict["verdict"], "allow")
            self.assertEqual(verdict["legacy_fallback"]["override"]["status"], "accepted")
            self.assertEqual(verdict["legacy_fallback"]["override"]["expires_at"], "2099-01-01T00:00:00Z")
        finally:
            os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_expired_override_is_rejected(self):
        self.seed_legacy()
        self._write_override(self._override_payload(expires_at="2000-01-01T00:00:00Z"))
        os.environ[P.INACTIVE_FALLBACK_ENV] = "deny"
        try:
            verdict = P.check_write(self.root, self.root / "plans" / "x.md")
            self.assertEqual(verdict["verdict"], "deny")
            self.assertEqual(verdict["legacy_fallback"]["override"]["reason"], "override-expired")
        finally:
            os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_malformed_override_is_rejected(self):
        self.seed_legacy()
        os.environ[P.INACTIVE_FALLBACK_ENV] = "deny"
        try:
            self._write_override(self._override_payload(issuer=None))
            self.assertEqual(P.check_write(self.root, self.root / "plans" / "x.md")["legacy_fallback"]
                             ["override"]["reason"], "override-malformed")
            self._write_override(self._override_payload(schema_version=2))
            self.assertEqual(P.check_write(self.root, self.root / "plans" / "x.md")["legacy_fallback"]
                             ["override"]["reason"], "override-malformed")
            P.compat_override_path(self.root).write_text("{", encoding="utf-8")
            self.assertEqual(P.check_write(self.root, self.root / "plans" / "x.md")["legacy_fallback"]
                             ["override"]["reason"], "override-malformed")
        finally:
            os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_foreign_root_override_is_rejected(self):
        self.seed_legacy()
        self._write_override(self._override_payload(canonical_root=str(Path(self._tmp.name) / "elsewhere")))
        os.environ[P.INACTIVE_FALLBACK_ENV] = "deny"
        try:
            verdict = P.check_write(self.root, self.root / "plans" / "x.md")
            self.assertEqual(verdict["legacy_fallback"]["override"]["reason"], "override-foreign-root")
            self.assertEqual(verdict["verdict"], "deny")
        finally:
            os.environ.pop(P.INACTIVE_FALLBACK_ENV, None)

    def test_warn_keeps_level_warn_for_rejected_override(self):
        self.seed_legacy()
        self._write_override(self._override_payload(expires_at="2000-01-01T00:00:00Z"))
        verdict = P.check_write(self.root, self.root / "plans" / "x.md")
        self.assertEqual(verdict["legacy_fallback"]["level"], "warn")
        self.assertEqual(verdict["legacy_fallback"]["override"]["status"], "rejected")
        self.assertEqual(verdict["verdict"], "allow")

    def test_resolve_output_dir_signature_stays_a_two_tuple(self):
        self.seed_legacy()
        result = P.resolve_output_dir(self.root, "spec")
        self.assertEqual(len(result), 2)
        self.assertEqual(result, (self.root / "spec", "legacy"))

    def test_status_reports_classification_and_fallback(self):
        self.seed_legacy()
        result = P.status(self.root)
        self.assertEqual(result["root_classification"], "inactive-with-legacy")
        self.assertIsNone(result["activation_kind"])
        self.assertEqual(result["legacy_fallback"]["level"], "warn")
        for key in ("artifact_root", "cutover", "identity", "cycle_counts", "open_cycles", "pending_journals"):
            self.assertIn(key, result)

    def test_unrelated_verdicts_are_byte_identical_to_pre_change(self):
        self.seed_legacy()
        targets = {
            "runtime": self.root / ".runtime" / "x.json",
            "scratch": self.root / "_scratch" / "x",
            "outside": Path(self._tmp.name) / "elsewhere.md",
            "shared": self.root / "shared" / "spec" / ("ref_" + "1" * 32) / "revisions" / ("rrev_" + "2" * 32) / "prd.md",
            "campaigns": self.root / "campaigns" / ("camp_" + "9" * 32) / "campaign.json",
        }
        for name, target in targets.items():
            verdict = P.check_write(self.root, target)
            self.assertNotIn("legacy_fallback", verdict, name)

    def test_inactive_empty_root_check_write_is_unchanged(self):
        verdict = P.check_write(self.root, self.root / "plans" / "x.md")
        self.assertEqual((verdict["verdict"], verdict["reason"]), ("allow", "legacy-compat-window"))
        self.assertNotIn("legacy_fallback", verdict)

    def test_malformed_cutover_with_legacy_denies_check_write(self):
        self.seed_legacy()
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text("{", encoding="utf-8")
        verdict = P.check_write(self.root, self.root / "plans" / "x.md")
        self.assertEqual((verdict["verdict"], verdict["reason"]), ("deny", "cutover-record-malformed"))
        self.assertNotIn("legacy_fallback", verdict)

    def test_malformed_cutover_with_legacy_blocks_resolve_output_dir(self):
        self.seed_legacy()
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text("{", encoding="utf-8")
        with self.assertRaises(P.ProducerError) as ctx:
            P.resolve_output_dir(self.root, "spec")
        self.assertEqual(ctx.exception.code, "cutover-record-malformed")

    def test_malformed_cutover_with_legacy_cli_check_write_exits_65(self):
        self.seed_legacy()
        P.producer_dir(self.root).mkdir(parents=True, exist_ok=True)
        P.cutover_path(self.root).write_text("{", encoding="utf-8")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = P.main(["check-write", "--artifact-root", str(self.root),
                                "--file", str(self.root / "plans" / "x.md")])
        self.assertEqual(exit_code, P.BLOCKED)
        self.assertEqual(json.loads(buf.getvalue())["reason"], "cutover-record-malformed")


class CheckWriteTest(ProducerTestBase):
    def test_legacy_allowed_in_compat_window_and_denied_when_active(self):
        target = self.root / "plans" / "2026-08-25_x" / "plan.md"
        before = P.check_write(self.root, target)
        self.assertEqual((before["verdict"], before["reason"]), ("allow", "legacy-compat-window"))
        self.activate()
        after = P.check_write(self.root, target)
        self.assertEqual((after["verdict"], after["reason"]), ("deny", "legacy-top-level-write-denied"))
        self.assertEqual(after["bucket"], "plans")

    def test_runtime_and_outside_are_never_gated(self):
        self.activate()
        self.assertEqual(P.check_write(self.root, self.root / ".runtime" / "x.json")["verdict"], "allow")
        self.assertEqual(P.check_write(self.root, self.root / "_scratch" / "x")["verdict"], "allow")
        outside = P.check_write(self.root, Path(self._tmp.name) / "elsewhere.md")
        self.assertEqual((outside["verdict"], outside["reason"]), ("allow", "outside-artifact-root"))

    def test_shared_is_immutable_in_both_states(self):
        target = self.root / "shared" / "spec" / ("ref_" + "1" * 32) / "revisions" / ("rrev_" + "2" * 32) / "prd.md"
        self.assertEqual(P.check_write(self.root, target)["reason"], "shared-revision-immutable")
        self.activate()
        self.assertEqual(P.check_write(self.root, target)["verdict"], "deny")

    def test_cycle_paths(self):
        self.activate()
        route, route_file, result = self.begin()
        camp, cyc = result["campaign_id"], result["cycle_id"]
        base = Path(result["cycle_dir"])
        self.assertEqual(P.check_write(self.root, base.parent / "campaign.json")["reason"], "campaign-record-machine-managed")
        self.assertEqual(P.check_write(self.root, base / "manifest.json")["reason"], "outside-cycle-artifacts")
        ok = P.check_write(self.root, base / "artifacts" / "plans" / "plan.md")
        self.assertEqual((ok["verdict"], ok["reason"], ok["bucket"]), ("allow", "open-cycle-artifacts", "plans"))
        unknown = P.check_write(self.root, base.parent / "2026-09-04_unknown" / "artifacts" / "x.md")
        self.assertEqual(unknown["reason"], "cycle-unknown")
        self.assertEqual(P.cycle_bucket(self.root, base / "artifacts" / "spec" / "prd.md"), ("spec", cyc))

    def test_sealed_cycle_denies_new_writes(self):
        self.activate()
        route, route_file, result = self.begin()
        target = self.write_output(result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        verdict = P.check_write(self.root, target)
        self.assertEqual((verdict["verdict"], verdict["reason"]), ("deny", "cycle-not-open"))

    def test_worker_write_is_bound_to_issued_cycle_and_returns_its_output_path(self):
        self.activate()
        route, _, first = self.begin(title="current")
        _, _, other = self.begin(capability="autopilot-draft", mode="doc", title="neighbour")
        current = self.write_output(first, "shards/frame/direction-brief.md", b"current\n")
        neighbour = self.write_output(other, "shards/frame/direction-brief.md", b"neighbour\n")
        # Both cycles are open, with exactly the same node-relative filename.
        # Neither a false output hint nor an omitted cycle env overrides the route.
        for use_cycle_env in (True, False):
            env = {"AGENT_ROUTE_ID": route["route_id"], "AGENT_ARTIFACT_OUTPUT_DIR": str(neighbour.parent)}
            if use_cycle_env:
                env["AGENT_ARTIFACT_CYCLE_ID"] = first["cycle_id"]
            with self.subTest(cycle_env=use_cycle_env), mock.patch.dict(os.environ, env):
                self.assertEqual(P.check_write(self.root, current)["verdict"], "allow")
                verdict = P.check_write(self.root, neighbour)
                self.assertEqual(verdict["reason"], "artifact-outside-bound-cycle")
                self.assertIn(first["env"]["AGENT_ARTIFACT_OUTPUT_DIR"], verdict["detail"])
        self.assertEqual(neighbour.read_bytes(), b"neighbour\n")

    def test_registered_completion_reuses_cycle_scope_before_marker_publication(self):
        self.activate()
        route, _, result = self.begin()
        foreign = Path(self._tmp.name) / "foreign.md"
        foreign.write_text("other cycle result\n")
        with mock.patch.object(R, "_marker_attempt_axes") as axes:
            with self.assertRaisesRegex(ValueError, "artifact-outside-bound-cycle"):
                R._publish_completion_locked(route, {}, "frame", foreign,
                                             attempt_id="att-wrong-cycle", attempt_metadata={})
            axes.assert_not_called()
        self.assertEqual(P.require_cycle_output(self.root, self.write_output(result), route_id=route["route_id"]),
                         Path(result["cycle_dir"]) / "artifacts")

    def test_resolve_output_dir(self):
        self.assertEqual(P.resolve_output_dir(self.root, "spec"), (self.root / "spec", "legacy"))
        self.activate()
        with self.assertRaises(P.ProducerError) as ctx:
            P.resolve_output_dir(self.root, "spec")
        self.assertEqual(ctx.exception.code, "legacy-top-level-write-denied")
        route, route_file, result = self.begin()
        directory, layout = P.resolve_output_dir(self.root, "spec", cycle_dir_hint=result["cycle_dir"])
        self.assertEqual((directory, layout), (Path(result["cycle_dir"]) / "artifacts" / "spec", "cycle"))
        os.environ["AGENT_ARTIFACT_CYCLE_DIR"] = result["cycle_dir"]
        self.assertEqual(P.resolve_output_dir(self.root, "experiments")[1], "cycle")


class FinalizeTest(ProducerTestBase):
    def test_finalize_seals_manifest_index_and_record(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result, "plans/cycle/plan.md")
        self.write_output(result, "plans/cycle/final_report.md", b"report\n")
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sealed["artifact_count"], 2)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(m.validate(document).ok)
        self.assertEqual(document["cycle"]["state"], "completed")
        self.assertEqual(document["cycle"]["cycle_id"], result["cycle_id"])
        self.assertEqual(document["campaign"]["campaign_id"], result["campaign_id"])
        self.assertEqual(document["artifact_root_id"], ROOT_ID)
        roles = {row["title"]: row["role"] for row in document["artifacts"]}
        self.assertEqual(roles["plans/cycle/final_report.md"], "primary")
        self.assertEqual(roles["plans/cycle/plan.md"], "output")
        self.assertNotEqual(document["routes"][0]["terminal_marker"], "pending")
        completion = L.evaluate_cycle_completion(
            document, content_root=Path(result["cycle_dir"]), route_file=route_file, expected_root_id=ROOT_ID)
        self.assertEqual(completion.status, "complete", completion.to_payload())
        index = adm.load_index(self.root)
        self.assertIn(result["cycle_id"], index.manifests)
        record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertEqual(record["state"], "sealed")
        self.assertEqual(record["manifest_digest"], sealed["manifest_digest"])
        self.assertFalse(P.journal_path(self.root, result["cycle_id"]).exists())
        again = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(again["status"], "already-sealed")
        self.assertEqual(P.status(self.root)["cycle_counts"], {"sealed": 1})

    def test_sealed_replay_requires_matching_index_row(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        index = adm.load_index(self.root)
        adm._write_index(self.root, index.__class__(
            schema_version=index.schema_version, artifact_root_id=index.artifact_root_id,
            stable_ids=index.stable_ids, routes=index.routes, event_ids=index.event_ids,
            streams=index.streams, manifests={}, cycles=index.cycles,
        ))
        before = (P.read_cycle_record(self.root, result["cycle_id"]),
                  (Path(result["cycle_dir"]) / "manifest.json").read_bytes())
        with self.assertRaises(P.ProducerError) as ctx:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(ctx.exception.code, "already-sealed-mismatch")
        self.assertEqual(before[0], P.read_cycle_record(self.root, result["cycle_id"]))
        self.assertEqual(before[1], (Path(result["cycle_dir"]) / "manifest.json").read_bytes())

    def test_finalize_requires_closed_route_unless_allowed(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        with self.assertRaises(P.ProducerError) as ctx:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(ctx.exception.code, "route-not-closed")
        self.assertIn("complete -> close -> finalize -> admit-shared", str(ctx.exception))
        self.assertIn(route["route_id"], str(ctx.exception))
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"], allow_open_route=True)
        self.assertEqual(sealed["status"], "sealed")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(document["routes"][0]["terminal_marker"], "pending")
        self.assertEqual(document["cycle"]["state"], "active")
        self.assertEqual(sealed["cycle_state"], "active")

    def test_empty_output_leaves_no_lineage(self):
        self.activate()
        route, route_file, result = self.begin()
        self.close(route, route_file)
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "no-lineage")
        self.assertFalse(Path(result["cycle_dir"]).exists())
        self.assertFalse((self.root / "campaigns" / result["campaign_id"]).exists())
        self.assertNotIn(result["cycle_id"], adm.load_index(self.root).manifests)
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "no-lineage")

    def test_abandoned_state_is_recorded(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        outcome, created = R.close_route(
            route, route_file, commit="a" * 40, summary="abandoned fixture"
        )
        self.assertTrue(created)
        self.assertFalse(outcome["terminal_gate_proven"])
        outcome = P.finalize(
            self.root, cycle_id=result["cycle_id"], state="abandoned",
            abandon_reason="route-unrecoverable",
        )
        self.assertEqual(outcome["cycle_state"], "abandoned")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(document["routes"][0]["terminal_marker"], "pending")
        self.assertEqual(document["routes"][0]["terminal_evidence_id"], "")
        self.assertFalse(any(e["event_type"] == "route.terminal.recorded" for e in document["events"]))

    def test_completed_still_rejects_closed_route_without_terminal_evidence(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        outcome, _ = R.close_route(route, route_file, commit="a" * 40, summary="incomplete fixture")
        self.assertFalse(outcome["terminal_gate_proven"])
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(caught.exception.code, "completion-terminal-marker-unverified")

    def test_abandoned_finalize_can_adopt_an_explicit_legacy_root_output(self):
        self.activate()
        route, route_file, result = self.begin()
        legacy = Path(result["cycle_dir"]) / "owner_brief.md"
        legacy.write_text("owner brief\n", encoding="utf-8")
        R.close_route(route, route_file, commit="a" * 40, summary="abandoned fixture")
        outcome = P.finalize(
            self.root,
            cycle_id=result["cycle_id"],
            state="abandoned",
            abandon_reason="route-unrecoverable",
            adopt_root_outputs=["owner_brief.md"],
        )
        self.assertEqual(outcome["adopted_root_outputs"], ["owner_brief.md"])
        self.assertFalse(legacy.exists())
        self.assertTrue((Path(result["cycle_dir"]) / "artifacts" / "owner_brief.md").is_file())

    def test_root_output_adoption_validates_all_sources_before_moving(self):
        self.activate()
        route, route_file, result = self.begin()
        first = Path(result["cycle_dir"]) / "first.md"
        first.write_text("first\n", encoding="utf-8")
        R.close_route(route, route_file, commit="a" * 40, summary="abandoned fixture")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(
                self.root,
                cycle_id=result["cycle_id"],
                state="abandoned",
                abandon_reason="route-unrecoverable",
                adopt_root_outputs=["first.md", "missing.md"],
            )
        self.assertEqual(caught.exception.code, "root-output-adoption-source-invalid")
        self.assertTrue(first.is_file())
        self.assertFalse((Path(result["cycle_dir"]) / "artifacts" / "first.md").exists())

    def test_finalize_rejects_symlink_and_out_of_artifacts_files(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        (Path(result["cycle_dir"]) / "stray.md").write_text("x", encoding="utf-8")
        self.close(route, route_file)
        with self.assertRaises(P.ProducerError) as ctx:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(ctx.exception.code, "output-invalid")
        self.assertIn("file-outside-artifacts:stray.md", ctx.exception.detail)

    def test_every_intensity_shares_one_lifecycle(self):
        self.activate()
        for intensity in ("direct", "quick", "standard"):
            with self.subTest(intensity=intensity):
                route, route_file, result = self.begin(intensity)
                self.assertEqual(result["status"], "begun")
                self.write_output(result, f"plans/{intensity}/plan.md")
                self.close(route, route_file)
                sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
                self.assertEqual(sealed["status"], "sealed")


class RecoveryTest(ProducerTestBase):
    def _sealing_crash(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=result["cycle_id"], crash_after_manifest=True)
        return result

    def test_crash_after_manifest_rolls_forward(self):
        result = self._sealing_crash()
        self.assertTrue((Path(result["cycle_dir"]) / "manifest.json").is_file())
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "open")
        self.assertEqual(P.status(self.root)["pending_journals"], [result["cycle_id"]])
        recovered = P.recover(self.root)
        self.assertEqual(recovered["producer"]["rolled_forward"], [result["cycle_id"]])
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "sealed")
        self.assertIn(result["cycle_id"], adm.load_index(self.root).manifests)
        self.assertEqual(P.status(self.root)["pending_journals"], [])

    def test_crash_before_manifest_rolls_back_and_cycle_stays_open(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        P._write_journal(self.root, result["cycle_id"], state="sealing", manifest_digest="sha256:" + "0" * 64,
                         cycle_path=os.path.relpath(result["cycle_dir"], self.root))
        recovered = P.recover(self.root)
        self.assertEqual(recovered["producer"]["rolled_back"], [result["cycle_id"]])
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "open")
        self.assertEqual(P.check_write(self.root, Path(result["cycle_dir"]) / "artifacts" / "a.md")["verdict"], "allow")

    def test_missing_cycle_dir_is_dropped(self):
        self.activate()
        route, route_file, result = self.begin()
        import shutil
        shutil.rmtree(result["cycle_dir"])
        recovered = P.recover(self.root)
        self.assertEqual(recovered["producer"]["dropped"], [result["cycle_id"]])
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "dropped")

    def test_finalize_runs_recovery_first(self):
        result = self._sealing_crash()
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "already-sealed")


class SharedAdmissionTest(ProducerTestBase):
    def _sealed_cycle(self, capability="autopilot-spec", mode="update", extra=()):
        self.activate()
        route, route_file, result = self.begin("direct", capability, mode)
        self.write_output(result, "spec/prd.md", b"# PRD\n")
        for rel, data in extra:
            self.write_output(result, rel, data)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        return result

    def test_second_spec_reference_is_explicit_never_a_key_miss(self):
        # Defect K (cairn 2026-09-03): `--key cairn-spec` missed the canonical
        # `spec` reference and silently minted a second one.
        result = self._sealed_cycle()
        first = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec", key="spec")
        with self.assertRaises(P.ProducerError) as ctx:
            P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec", key="cairn-spec")
        self.assertEqual(ctx.exception.code, "shared-reference-exists")
        self.assertIn(first["shared_reference_id"], str(ctx.exception))
        self.assertEqual(len(P.list_references(self.root, "spec")), 1)
        # The documented keyless flow keeps landing on the single canonical reference.
        keyless = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec")
        self.assertEqual(keyless["shared_reference_id"], first["shared_reference_id"])
        again = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec", key="spec")
        self.assertEqual(again["shared_reference_id"], first["shared_reference_id"])
        forced = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec",
                                key="cairn-spec", allow_new_reference=True)
        self.assertNotEqual(forced["shared_reference_id"], first["shared_reference_id"])
        self.assertEqual(len(P.list_references(self.root, "spec")), 2)
        with self.assertRaises(P.ProducerError) as ctx:
            P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec")
        self.assertEqual(ctx.exception.code, "shared-reference-ambiguous")

    def test_keyless_first_admit_then_keyless_repeat_reuses_the_reference(self):
        result = self._sealed_cycle()
        first = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec")
        self.assertIsNone(P.list_references(self.root, "spec")[0].get("key"))
        second = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec")
        self.assertEqual(second["shared_reference_id"], first["shared_reference_id"])
        self.assertEqual(len(P.list_references(self.root, "spec")), 1)

    def test_admit_spec_creates_immutable_revision(self):
        result = self._sealed_cycle()
        admitted = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec", key="prd")
        self.assertEqual(admitted["status"], "admitted")
        self.assertTrue(admitted["reference_created"])
        revision_dir = Path(admitted["revision_dir"])
        self.assertTrue((revision_dir / "prd.md").is_file())
        revision = json.loads((revision_dir / "revision.json").read_text())
        self.assertEqual(revision["source"]["cycle_id"], result["cycle_id"])
        self.assertEqual(revision["sequence"], 1)
        reference = json.loads((revision_dir.parent.parent / "reference.json").read_text())
        self.assertEqual(reference["latest_revision_id"], admitted["shared_reference_revision_id"])
        self.assertEqual(P.check_write(self.root, revision_dir / "prd.md")["reason"], "shared-revision-immutable")
        # Second admission under the same key appends a revision; never rewrites.
        second = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec", key="prd")
        self.assertFalse(second["reference_created"])
        self.assertEqual(second["shared_reference_id"], admitted["shared_reference_id"])
        self.assertNotEqual(second["shared_reference_revision_id"], admitted["shared_reference_revision_id"])
        self.assertEqual(json.loads((Path(second["revision_dir"]) / "revision.json").read_text())["sequence"], 2)

    def test_admit_requires_sealed_cycle(self):
        self.activate()
        route, route_file, result = self.begin("direct", "autopilot-spec", "update")
        with self.assertRaises(P.ProducerError) as ctx:
            P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="spec")
        self.assertEqual(ctx.exception.code, "cycle-not-sealed")

    def test_research_requires_explicit_promotion(self):
        result = self._sealed_cycle("autopilot-research", "academic",
                                    extra=[("research/topic/report.md", b"r\n"),
                                           ("research/topic/promotion.md", b"approved\n")])
        with self.assertRaises(P.ProducerError) as ctx:
            P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="research", source="research/topic")
        self.assertEqual(ctx.exception.code, "research-promotion-required")
        with self.assertRaises(P.ProducerError) as ctx:
            P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="research", source="research/topic",
                           promote_research=True)
        self.assertEqual(ctx.exception.code, "research-promotion-evidence-required")
        admitted = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="research", source="research/topic",
                                  promote_research=True, promotion_evidence="research/topic/promotion.md")
        self.assertEqual(admitted["promotion"]["kind"], "explicit")
        self.assertTrue(admitted["promotion"]["evidence_digest"].startswith("sha256:"))

    def test_only_declared_kinds_are_admissible(self):
        result = self._sealed_cycle()
        with self.assertRaises(P.ProducerError) as ctx:
            P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="plans", source="spec")
        self.assertEqual(ctx.exception.code, "shared-kind-not-admissible")


class CliTest(ProducerTestBase):
    def run_cli(self, *argv):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = P.main(list(argv))
        return code, json.loads(buffer.getvalue().strip().splitlines()[-1])

    def test_check_write_exit_codes(self):
        code, payload = self.run_cli("check-write", "--artifact-root", str(self.root), "--file", str(self.root / "plans" / "x.md"))
        self.assertEqual((code, payload["verdict"]), (P.OK, "allow"))
        self.activate()
        code, payload = self.run_cli("check-write", "--artifact-root", str(self.root), "--file", str(self.root / "plans" / "x.md"))
        self.assertEqual((code, payload["reason"]), (P.BLOCKED, "legacy-top-level-write-denied"))

    def test_begin_env_file(self):
        self.activate()
        route, route_file = self.route()
        env_file = Path(self._tmp.name) / "env"
        code, payload = self.run_cli("begin", "--artifact-root", str(self.root), "--route", str(route_file),
                                     "--capability", "autopilot-code", "--intensity", "direct",
                                     "--env-file", str(env_file))
        self.assertEqual(code, P.OK)
        lines = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
        self.assertEqual(lines["AGENT_ARTIFACT_CYCLE_ID"], payload["cycle_id"])
        self.assertEqual(lines["AGENT_ARTIFACT_CYCLE_DIR"], payload["cycle_dir"])

    def test_finalize_state_conflict_exits_blocked_with_requested_and_actual_state(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        code, payload = self.run_cli(
            "finalize", "--artifact-root", str(self.root), "--cycle", cycle_id,
            "--state", "abandoned", "--abandon-reason", "operator-decision",
        )
        self.assertEqual(code, P.BLOCKED)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["reason"], "finalize-state-conflict")
        self.assertIn("requested=abandoned", payload["detail"])
        self.assertIn("published_cycle_state=active", payload["detail"])

    def test_malformed_manifest_cycle_state_exits_blocked_not_traceback(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["cycle"]["state"] = ["active"]
        manifest_path.write_text(json.dumps(document), encoding="utf-8")
        code, payload = self.run_cli("finalize", "--artifact-root", str(self.root), "--cycle", cycle_id)
        self.assertEqual(code, P.BLOCKED)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["reason"], "sealed-cycle-state-unreadable")
        self.assertIn("cycle_state=['active']", payload["detail"])


class ReviewPublicationLeaseTest(ProducerTestBase):
    """SD-117 §13.34.5-(2): L1 review-publication-lease enforcement."""

    def live_v2_holder(self, cycle_id, attempt, nonce):
        witness = D.review_governed_lease_path(self.root, cycle_id, attempt)
        witness.parent.mkdir(parents=True, exist_ok=True)
        witness.write_bytes(D.review_governed_lease_payload(attempt, cycle_id, nonce))
        process = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1],'r+b'); "
             "fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()", str(witness)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True,
        )
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdin.close(); process.stdout.close()
        self.addCleanup(cleanup)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        namespace = os.readlink(f"/proc/{process.pid}/ns/pid")
        return process, {"pid": process.pid, "pid_start": D.process_start_ticks(process.pid),
                         "pgid": process.pid, "pid_ns": namespace, "pid_observer_ns": namespace}

    def test_review_round_one_fail_keeps_cycle_open_and_registered_publication_verdict_is_allow(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        # A review round-1 FAIL is not itself a finalize call -- SD-117 L3
        # says the cycle stays `open` until something explicitly abandons or
        # completes it, so a synthetic FAIL round leaves the record alone.
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "open")
        self.close(route, route_file)
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "sealed")
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "sealed")

    def test_abandon_with_live_lease_refuses_with_zero_event_and_record_delta(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.review_lease_acquire(self.root, cycle_id=cycle_id, attempt_id="att-reviewer")
        before = P.read_cycle_record(self.root, cycle_id)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "cycle-abandon-blocked-live-review")
        after = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual(before, after)
        self.assertTrue(Path(result["cycle_dir"]).exists())

    def test_abandon_succeeds_after_deadline_and_later_write_is_cycle_not_open(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        past = 1_000_000_000.0
        P.review_lease_acquire(
            self.root, cycle_id=cycle_id, attempt_id="att-reviewer",
            deadline_seconds=1, now=past,
        )
        outcome = P.finalize(
            self.root, cycle_id=cycle_id, state="abandoned",
            abandon_reason="lease-expired-no-publisher", allow_open_route=True,
        )
        self.assertEqual(outcome["status"], "sealed")
        # Storage sealing is not task completion (D-10): the published cycle
        # state is `abandoned`, so a later `completed` request is a typed
        # conflict, not a silent idempotent short-circuit.
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")

    def test_corrupt_lease_record_blocks_abandon(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        lease_dir = P._review_lease_dir(self.root, cycle_id)
        lease_dir.mkdir(parents=True, exist_ok=True)
        (lease_dir / "att-corrupt.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "cycle-abandon-blocked-live-review")

    def test_double_acquire_and_double_release_are_idempotent(self):
        self.activate()
        route, route_file, result = self.begin()
        cycle_id = result["cycle_id"]
        first = P.review_lease_acquire(self.root, cycle_id=cycle_id, attempt_id="att-r")
        second = P.review_lease_acquire(self.root, cycle_id=cycle_id, attempt_id="att-r")
        self.assertEqual(first["status"], "acquired")
        self.assertEqual(second["status"], "already-held")
        release1 = P.review_lease_release(self.root, cycle_id=cycle_id, attempt_id="att-r")
        release2 = P.review_lease_release(self.root, cycle_id=cycle_id, attempt_id="att-r")
        self.assertEqual(release1["status"], "released")
        self.assertEqual(release2["status"], "already-released")

    def test_mixed_v1_corruption_cannot_hide_live_v2_and_v2_exit_unblocks(self):
        self.activate()
        _route, _route_file, result = self.begin()
        cycle_id = result["cycle_id"]
        lease_dir = P._review_lease_dir(self.root, cycle_id)
        lease_dir.mkdir(parents=True, exist_ok=True)
        corrupt = lease_dir / "a-v1.json"
        corrupt.write_text("{broken", encoding="utf-8")
        attempt = "att-z-live-v2"
        nonce = "c" * 64
        now = time.time()
        record = {
            "schema_version": 2, "cycle_id": cycle_id,
            "attempt_id": attempt, "acquired_at": P._rfc3339(now - 1),
            "deadline": P._rfc3339(now + 300), "released_at": None,
            "expired": False,
            "review_governed_lease": D.REVIEW_GOVERNED_LEASE_KIND,
            "review_governed_lease_nonce": nonce,
        }
        process, identity = self.live_v2_holder(cycle_id, attempt, nonce)
        record.update(identity)
        v2 = lease_dir / f"{attempt}.json"
        v2.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(P._live_review_lease(self.root, cycle_id), v2)
        record["released_at"] = P._rfc3339(now)
        v2.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(P._live_review_lease(self.root, cycle_id), corrupt)
        record["released_at"] = None
        record["acquired_at"] = P._rfc3339(now - 10)
        record["deadline"] = P._rfc3339(now - 2)
        v2.write_text(json.dumps(record), encoding="utf-8")
        # A valid time limit never overrides an exactly live holder.
        self.assertEqual(P._live_review_lease(self.root, cycle_id), v2)
        record["deadline"] = P._rfc3339(now + 300)
        v2.write_text(json.dumps(record), encoding="utf-8")
        process.stdin.close(); process.wait(timeout=5)
        corrupt.unlink()
        self.assertIsNone(P._live_review_lease(self.root, cycle_id))

    def test_completed_finalize_is_fenced_until_governed_v2_process_exits(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        attempt = "att-finalize-race-v2"
        nonce = "d" * 64
        process, identity = self.live_v2_holder(cycle_id, attempt, nonce)
        now = time.time()
        lease_dir = P._review_lease_dir(self.root, cycle_id)
        lease_dir.mkdir(parents=True, exist_ok=True)
        (lease_dir / f"{attempt}.json").write_text(json.dumps({
            "schema_version": 2, "cycle_id": cycle_id,
            "attempt_id": attempt, "acquired_at": P._rfc3339(now - 1),
            "deadline": P._rfc3339(now + 300), "released_at": None,
            "expired": False,
            "review_governed_lease": D.REVIEW_GOVERNED_LEASE_KIND,
            "review_governed_lease_nonce": nonce,
            **identity,
        }), encoding="utf-8")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(caught.exception.code, "cycle-finalize-blocked-live-review")
        process.stdin.close(); process.wait(timeout=5)
        self.assertEqual(P.finalize(self.root, cycle_id=cycle_id)["status"], "sealed")

    def test_recovery_journal_roll_forward_is_fenced_by_live_v2_lease(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True,
                       crash_after_manifest=True)
        lease_path = P._review_lease_path(self.root, cycle_id, "att-recovery")
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
        with mock.patch.object(P, "_live_review_lease", return_value=lease_path):
            with self.assertRaises(P.ProducerError) as caught:
                P.recover(self.root)
        self.assertEqual(caught.exception.code, "cycle-finalize-blocked-live-review")
        self.assertEqual(P.read_cycle_record(self.root, cycle_id)["state"], "open")
        self.assertTrue(P.journal_path(self.root, cycle_id).exists())
        with mock.patch.object(P, "_live_review_lease", return_value=None):
            recovered = P.recover(self.root)
        self.assertIn(cycle_id, recovered["producer"]["rolled_forward"])

    def test_recovery_manifest_discovery_is_fenced_without_journal(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True,
                       crash_after_manifest=True)
        P.journal_path(self.root, cycle_id).unlink()
        lease_path = P._review_lease_path(self.root, cycle_id, "att-recovery-no-journal")
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
        with mock.patch.object(P, "_live_review_lease", return_value=lease_path):
            with self.assertRaises(P.ProducerError) as caught:
                P.recover(self.root)
        self.assertEqual(caught.exception.code, "cycle-finalize-blocked-live-review")
        self.assertEqual(P.read_cycle_record(self.root, cycle_id)["state"], "open")


class AbandonReasonTest(ProducerTestBase):
    """SD-117 §13.34.5-(2): L3 `abandon_reason` closed enum."""

    def test_zero_row_cycle_seal_stays_no_lineage_with_directory_removed(self):
        self.activate()
        route, route_file, result = self.begin()
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "no-lineage")
        self.assertFalse(Path(result["cycle_dir"]).exists())

    def test_cycle_completed_injected_into_abandoned_stream_is_refused_by_typed_conflict(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision",
                  allow_open_route=True)
        # The published cycle state is `abandoned`; injecting a `completed`
        # request against it is a typed conflict, not an idempotent
        # short-circuit that ignores `state` (D-8/D-10).
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")

    def test_every_cycle_abandoned_event_carries_closed_enum_reason_disjoint_from_review_verdicts(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        review_verdicts = {"PASS", "FAIL", "BLOCKED", "allow", "deny"}
        self.assertEqual(P.ABANDON_REASONS & review_verdicts, set())
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="FAIL")
        self.assertEqual(caught.exception.code, "abandon-reason-required")
        outcome = P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision",
                             allow_open_route=True)
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        abandoned_events = [e for e in document["events"] if e["event_type"] == "cycle.abandoned"]
        self.assertEqual(len(abandoned_events), 1)
        self.assertEqual(abandoned_events[0]["payload"]["abandon_reason"], "operator-decision")
        self.assertNotIn(abandoned_events[0]["payload"]["abandon_reason"], review_verdicts)

    def test_sealed_on_disk_write_verdicts_are_identical_to_prior_revision(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "sealed")
        target = Path(result["cycle_dir"]) / "artifacts" / "plans" / "cycle" / "extra.md"
        verdict = P.check_write(self.root, target)
        self.assertEqual(verdict["verdict"], "deny")
        self.assertEqual(verdict["reason"], "cycle-not-open")

    def test_force_abandon_ignoring_lease_requires_operator_override_live_review_reason(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.review_lease_acquire(self.root, cycle_id=cycle_id, attempt_id="att-reviewer")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(
                self.root, cycle_id=cycle_id, state="abandoned",
                abandon_reason="operator-decision", force_abandon_ignoring_lease=True,
            )
        self.assertEqual(caught.exception.code, "abandon-reason-required")
        outcome = P.finalize(
            self.root, cycle_id=cycle_id, state="abandoned",
            force_abandon_ignoring_lease=True, allow_open_route=True,
        )
        self.assertEqual(outcome["status"], "sealed")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text())
        abandoned_events = [e for e in document["events"] if e["event_type"] == "cycle.abandoned"]
        self.assertEqual(abandoned_events[0]["payload"]["abandon_reason"], "operator-override-live-review")


class FinalizeStateConflictTest(ProducerTestBase):
    """Sealed storage is not task completion: a request whose `state` does not
    match the *published* `manifest.json` cycle state is a typed conflict,
    never a silent `already-sealed`."""

    def test_active_snapshot_refuses_abandon_request_and_leaves_manifest_untouched(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        sealed = P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        self.assertEqual(sealed["cycle_state"], "active")
        R.close_route(route, route_file, commit="a" * 40, summary="fixture")
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        record_before = P.read_cycle_record(self.root, cycle_id)
        index_before = adm.load_index(self.root)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")
        self.assertIn("requested=abandoned", caught.exception.detail)
        self.assertIn("published_cycle_state=active", caught.exception.detail)
        self.assertIn("storage_state=sealed", caught.exception.detail)
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
        self.assertEqual(P.read_cycle_record(self.root, cycle_id), record_before)
        self.assertEqual(adm.load_index(self.root), index_before)

    def test_conflict_precedes_abandon_reason_validation(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")

    def test_same_terminal_state_retry_stays_idempotent(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id)
        again = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(again["status"], "already-sealed")
        self.assertEqual(again["storage_state"], "sealed")
        self.assertEqual(again["cycle_state"], "completed")

        route2, route_file2 = self.route(gate_source="fixture-2")
        result2 = P.begin(self.root, route_file=route_file2, capability="autopilot-code", intensity="direct")
        self.write_output(result2)
        R.close_route(route2, route_file2, commit="a" * 40, summary="abandoned fixture")
        cycle_id2 = result2["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id2, state="abandoned", abandon_reason="operator-decision")
        again2 = P.finalize(self.root, cycle_id=cycle_id2, state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(again2["status"], "already-sealed")
        self.assertEqual(again2["storage_state"], "sealed")
        self.assertEqual(again2["cycle_state"], "abandoned")

    def test_allow_open_first_publication_succeeds_but_identical_retry_now_conflicts(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        sealed = P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sealed["cycle_state"], "active")
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed", allow_open_route=True)
        self.assertEqual(caught.exception.code, "finalize-state-conflict")
        self.assertIn("requested=completed", caught.exception.detail)
        self.assertIn("published_cycle_state=active", caught.exception.detail)
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)

    def test_allow_open_retry_after_route_close_conflicts_and_does_not_promote(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        self.close(route, route_file)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed", allow_open_route=True)
        self.assertEqual(caught.exception.code, "finalize-state-conflict")
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
        record = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual(record["cycle_state"], "active")

    def test_completed_snapshot_refuses_abandon_and_abandoned_refuses_completed(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")

        route2, route_file2 = self.route(gate_source="fixture-2")
        result2 = P.begin(self.root, route_file=route_file2, capability="autopilot-code", intensity="direct")
        self.write_output(result2)
        R.close_route(route2, route_file2, commit="a" * 40, summary="abandoned fixture")
        cycle_id2 = result2["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id2, state="abandoned", abandon_reason="operator-decision")
        with self.assertRaises(P.ProducerError) as caught2:
            P.finalize(self.root, cycle_id=cycle_id2, state="completed")
        self.assertEqual(caught2.exception.code, "finalize-state-conflict")

    def test_sealed_cycle_state_source_is_manifest_with_absent_only_fallback(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()

        # (1) record cache dropped -> manifest (the canonical source) still judges correctly.
        record = P.read_cycle_record(self.root, cycle_id)
        del record["cycle_state"]
        P._write_cycle_record(self.root, record, exclusive=False)
        again = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(again["status"], "already-sealed")
        self.assertEqual(again["cycle_state"], "completed")

        # (2) record cache disagrees with manifest -> ambiguous, hard refusal.
        record = P.read_cycle_record(self.root, cycle_id)
        record["cycle_state"] = "abandoned"
        P._write_cycle_record(self.root, record, exclusive=False)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-ambiguous")
        self.assertIn("manifest=completed", caught.exception.detail)
        self.assertIn("record=abandoned", caught.exception.detail)

        # restore the cache before mangling the canonical source.
        record = P.read_cycle_record(self.root, cycle_id)
        record["cycle_state"] = "completed"
        P._write_cycle_record(self.root, record, exclusive=False)

        # (3) manifest absent and record cache absent -> unknown, no false success.
        record = P.read_cycle_record(self.root, cycle_id)
        del record["cycle_state"]
        P._write_cycle_record(self.root, record, exclusive=False)
        manifest_path.unlink()
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unknown")
        self.assertIn("manifest=absent", caught.exception.detail)

        # (4) manifest absent, record cache valid -> compatibility fallback judges correctly.
        record = P.read_cycle_record(self.root, cycle_id)
        record["cycle_state"] = "completed"
        P._write_cycle_record(self.root, record, exclusive=False)
        again = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(again["status"], "already-sealed")
        self.assertEqual(again["cycle_state"], "completed")

        # (5) manifest absent, record cache non-string -> unknown (not TypeError).
        record = P.read_cycle_record(self.root, cycle_id)
        record["cycle_state"] = 123
        P._write_cycle_record(self.root, record, exclusive=False)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unknown")
        self.assertIn("record=123", caught.exception.detail)

        # manifest present but record cache non-string -> the canonical source wins.
        manifest_path.write_bytes(manifest_bytes)
        again = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(again["status"], "already-sealed")
        self.assertEqual(again["cycle_state"], "completed")

    def test_unreadable_manifest_fails_closed_instead_of_trusting_record_cache(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        record = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual(record["cycle_state"], "completed")  # valid cache throughout: it must not save a success.

        # (1) unparsable JSON.
        manifest_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
        self.assertIn("unparsable", caught.exception.detail)
        manifest_path.write_bytes(manifest_bytes)

        # (2) cycle_id mismatch. The `.cycle.json` binding is removed first so
        # the locator's own global scan (a stricter, unrelated invariant that
        # cross-checks manifest cycle_id against the binding for every
        # readable-layout cycle dir) does not intercept this before
        # `_published_cycle_state` gets to judge it; `cycle_dir` still
        # resolves the path through the record's `locator` field.
        other_cycle_id = "cyc_" + "9" * 32
        binding_path = Path(result["cycle_dir"]) / ".cycle.json"
        binding_bytes = binding_path.read_bytes()
        binding_path.unlink()
        mutated = json.loads(manifest_bytes)
        mutated["cycle"]["cycle_id"] = other_cycle_id
        manifest_path.write_text(json.dumps(mutated), encoding="utf-8")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
        self.assertIn(cycle_id, caught.exception.detail)
        self.assertIn(other_cycle_id, caught.exception.detail)
        manifest_path.write_bytes(manifest_bytes)
        binding_path.write_bytes(binding_bytes)

        # (3) cycle.state outside the enum.
        mutated = json.loads(manifest_bytes)
        mutated["cycle"]["state"] = "weird"
        manifest_path.write_text(json.dumps(mutated), encoding="utf-8")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
        self.assertIn("cycle_state='weird'", caught.exception.detail)
        manifest_path.write_bytes(manifest_bytes)

        # (4) cycle.state is a non-string JSON value (list, dict) -- must not reach
        # `in _SEALED_CYCLE_STATES` unhashable.
        for bad_state in (["active"], {"value": "active"}):
            mutated = json.loads(manifest_bytes)
            mutated["cycle"]["state"] = bad_state
            manifest_path.write_text(json.dumps(mutated), encoding="utf-8")
            with self.assertRaises(P.ProducerError) as caught:
                P.finalize(self.root, cycle_id=cycle_id)
            self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
            self.assertIn(f"cycle_state={bad_state!r}", caught.exception.detail)
            manifest_path.write_bytes(manifest_bytes)

        # (5) cycle itself is not a mapping.
        for bad_cycle in ("active", ["active"]):
            mutated = json.loads(manifest_bytes)
            mutated["cycle"] = bad_cycle
            manifest_path.write_text(json.dumps(mutated), encoding="utf-8")
            with self.assertRaises(P.ProducerError) as caught:
                P.finalize(self.root, cycle_id=cycle_id)
            self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
            self.assertIn(f"cycle-structure type={type(bad_cycle).__name__}", caught.exception.detail)
            manifest_path.write_bytes(manifest_bytes)

    def test_manifest_non_regular_entries_fail_closed_instead_of_cache_fallback(self):
        import errno
        from unittest import mock

        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        record = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual(record["cycle_state"], "completed")

        # A directory is present, not absent, and must not be hidden by the
        # valid record cache.
        manifest_bytes = manifest_path.read_bytes()
        manifest_path.unlink()
        manifest_path.mkdir()
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
        self.assertIn("entry-kind=non-regular", caught.exception.detail)
        manifest_path.rmdir()
        manifest_path.write_bytes(manifest_bytes)

        # Both a live and dangling symlink are non-regular entries. The
        # lstat gate rejects them before JSON I/O can follow or classify them.
        target = manifest_path.with_name("manifest-target.json")
        target.write_bytes(manifest_bytes)
        manifest_path.unlink()
        manifest_path.symlink_to(target.name)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
        manifest_path.unlink()
        target.unlink()
        manifest_path.symlink_to("missing-manifest.json")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
        manifest_path.unlink()
        manifest_path.write_bytes(manifest_bytes)

        # FIFO is rejected from its mode without attempting a blocking read.
        fifo_supported = hasattr(os, "mkfifo")
        if fifo_supported:
            manifest_path.unlink()
            os.mkfifo(manifest_path)
            with self.assertRaises(P.ProducerError) as caught:
                P.finalize(self.root, cycle_id=cycle_id)
            self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
            manifest_path.unlink()
            manifest_path.write_bytes(manifest_bytes)

        # Lookup failures are not absence. Mocking lstat keeps this portable
        # when the test process has permission to inspect the fixture.
        with mock.patch.object(P, "cycle_dir", return_value=manifest_path.parent), \
             mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            with self.assertRaises(P.ProducerError) as caught:
                P._published_cycle_state(self.root, record)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
        self.assertIn("lookup-error=PermissionError", caught.exception.detail)
        with mock.patch.object(P, "cycle_dir", return_value=manifest_path.parent), \
             mock.patch.object(Path, "lstat", side_effect=OSError(errno.ENOTDIR, "not a directory")):
            with self.assertRaises(P.ProducerError) as caught:
                P._published_cycle_state(self.root, record)
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
        self.assertIn("lookup-error=NotADirectoryError", caught.exception.detail)

    def test_recovery_roll_forward_before_sealed_branch_is_judged_on_published_state(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True, crash_after_manifest=True)
        # before: recovery has not run yet.
        self.assertEqual(P.read_cycle_record(self.root, cycle_id)["state"], "open")
        self.assertIn(cycle_id, P.status(self.root)["pending_journals"])
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")
        # after: `_recover_locked` rolled the manifest forward before the sealed
        # branch ever ran its judgment, so the conflict is against `active`.
        record = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual(record["state"], "sealed")
        self.assertEqual(record["cycle_state"], "active")
        self.assertFalse(P.journal_path(self.root, cycle_id).exists())
        self.assertIn(cycle_id, adm.load_index(self.root).manifests)

    def test_non_sealed_record_states_still_fall_to_cycle_not_open(self):
        import shutil

        self.activate()
        # (1) zero-row cycle seals to `no-lineage`, not `sealed`.
        route, route_file, result = self.begin()
        outcome = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(outcome["status"], "no-lineage")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(caught.exception.code, "cycle-not-open")

        # (2) zero-row abandon seals record state to `abandoned`, not `sealed`.
        route2, route_file2 = self.route(gate_source="fixture-2")
        result2 = P.begin(self.root, route_file=route_file2, capability="autopilot-code", intensity="direct")
        P.finalize(self.root, cycle_id=result2["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=result2["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "cycle-not-open")

        # (3) a recovery-dropped cycle record is `dropped`, not `sealed`.
        route3, route_file3 = self.route(gate_source="fixture-3")
        result3 = P.begin(self.root, route_file=route_file3, capability="autopilot-code", intensity="direct")
        shutil.rmtree(result3["cycle_dir"])
        P.recover(self.root)
        self.assertEqual(P.read_cycle_record(self.root, result3["cycle_id"])["state"], "dropped")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=result3["cycle_id"])
        self.assertEqual(caught.exception.code, "cycle-not-open")

    def test_force_abandon_ignoring_lease_on_sealed_active_conflicts_before_lease_checks(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id, allow_open_route=True)
        P.review_lease_acquire(self.root, cycle_id=cycle_id, attempt_id="att-reviewer")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", force_abandon_ignoring_lease=True)
        self.assertEqual(caught.exception.code, "finalize-state-conflict")

    def test_unrelated_broken_locator_binding_falls_back_instead_of_leaking(self):
        # The sealed branch now reaches `cycle_dir`, whose `find_path_by_id`
        # scans the whole root -- so *another* cycle's broken `.cycle.json`
        # raises `LocatorError`, which is a `ValueError`, not a
        # `ProducerError`. An unrelated cycle's damage says nothing about this
        # cycle's work state, so it is the absent-canonical-source case: fall
        # back to the record cache and keep judging.
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        cycle_id = result["cycle_id"]
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=cycle_id)
        stranger = Path(result["cycle_dir"]).parent / "2026-01-01_stranger"
        stranger.mkdir()
        (stranger / ".cycle.json").write_text(json.dumps({
            "schema_version": 1, "kind": "artifact-cycle-binding",
            "campaign_id": "camp_" + "0" * 32, "cycle_id": "cyc_" + "0" * 32,
        }), encoding="utf-8")
        with self.assertRaises(P.artifact_locator.LocatorError):
            P.artifact_locator.scan_index(self.root)
        again = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(again["status"], "already-sealed")
        self.assertEqual(again["cycle_state"], "completed")
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "finalize-state-conflict")

    def test_current_broken_binding_does_not_mask_manifest_conflict(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id)
        cycle_dir = Path(result["cycle_dir"])
        manifest_path = cycle_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cycle"]["state"] = "abandoned"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        binding_path = cycle_dir / ".cycle.json"
        binding_path.write_text("{", encoding="utf-8")
        before = {
            "manifest": manifest_path.read_bytes(),
            "record": P.cycle_record_path(self.root, cycle_id).read_bytes(),
            "index_json": (self.root / "campaigns" / "INDEX.json").read_bytes(),
            "index_md": (self.root / "campaigns" / "INDEX.md").read_bytes(),
        }
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unknown")
        self.assertIn("locator-cycle-binding-invalid", caught.exception.detail)
        self.assertEqual(manifest_path.read_bytes(), before["manifest"])
        self.assertEqual(P.cycle_record_path(self.root, cycle_id).read_bytes(), before["record"])
        self.assertEqual((self.root / "campaigns" / "INDEX.json").read_bytes(), before["index_json"])
        self.assertEqual((self.root / "campaigns" / "INDEX.md").read_bytes(), before["index_md"])

    def test_record_locator_cannot_substitute_another_cycle_directory(self):
        import shutil

        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id)
        cycle_dir = Path(result["cycle_dir"])
        decoy = cycle_dir.parent / f"{cycle_dir.name}-decoy"
        shutil.copytree(cycle_dir, decoy)
        binding_path = decoy / P.artifact_locator.CYCLE_BINDING
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        binding["cycle_id"] = "cyc_" + "9" * 32
        binding_path.write_text(json.dumps(binding), encoding="utf-8")
        record = P.read_cycle_record(self.root, cycle_id)
        record["locator"] = decoy.name
        P._write_cycle_record(self.root, record, exclusive=False)
        with self.assertRaises(P.artifact_locator.LocatorError) as locator:
            P.artifact_locator.scan_index(self.root)
        self.assertEqual(locator.exception.code, "locator-cycle-binding-id-mismatch")
        before = {
            "manifest": (decoy / "manifest.json").read_bytes(),
            "record": P.cycle_record_path(self.root, cycle_id).read_bytes(),
            "index_json": (self.root / "campaigns" / "INDEX.json").read_bytes(),
            "index_md": (self.root / "campaigns" / "INDEX.md").read_bytes(),
        }
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unknown")
        self.assertEqual((decoy / "manifest.json").read_bytes(), before["manifest"])
        self.assertEqual(P.cycle_record_path(self.root, cycle_id).read_bytes(), before["record"])
        self.assertEqual((self.root / "campaigns" / "INDEX.json").read_bytes(), before["index_json"])
        self.assertEqual((self.root / "campaigns" / "INDEX.md").read_bytes(), before["index_md"])

    def test_unrelated_broken_binding_does_not_mask_unreadable_current_manifest(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        P.finalize(self.root, cycle_id=cycle_id)
        cycle_dir = Path(result["cycle_dir"])
        manifest_path = cycle_dir / "manifest.json"
        manifest_before = manifest_path.read_bytes()
        manifest_path.write_text("{not json", encoding="utf-8")
        stranger = cycle_dir.parent / "2026-01-01_stranger"
        stranger.mkdir()
        (stranger / ".cycle.json").write_text(json.dumps({
            "schema_version": 1, "kind": "artifact-cycle-binding",
            "campaign_id": "camp_" + "0" * 32, "cycle_id": "cyc_" + "0" * 32,
        }), encoding="utf-8")
        before = {
            "manifest": manifest_path.read_bytes(),
            "record": P.cycle_record_path(self.root, cycle_id).read_bytes(),
            "index_json": (self.root / "campaigns" / "INDEX.json").read_bytes(),
            "index_md": (self.root / "campaigns" / "INDEX.md").read_bytes(),
        }
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle_id, state="completed")
        self.assertEqual(caught.exception.code, "sealed-cycle-state-unreadable")
        self.assertEqual(manifest_path.read_bytes(), before["manifest"])
        self.assertNotEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(P.cycle_record_path(self.root, cycle_id).read_bytes(), before["record"])
        self.assertEqual((self.root / "campaigns" / "INDEX.json").read_bytes(), before["index_json"])
        self.assertEqual((self.root / "campaigns" / "INDEX.md").read_bytes(), before["index_md"])


class SharedReferencePinAndRelatedTest(ProducerTestBase):
    def _admitted_spec_pin(self):
        self.activate()
        route, route_file, source = self.begin("direct", "autopilot-spec", "update")
        self.write_output(source, "spec/prd.md", b"# PRD\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=source["cycle_id"])
        admitted = P.admit_shared(self.root, cycle_id=source["cycle_id"], kind="spec", source="spec", key="prd")
        return admitted

    def test_manifest_shape_unchanged_without_pins(self):
        self.activate()
        route, route_file, result = self.begin()
        self.write_output(result)
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(document["shared_references"], [])
        self.assertEqual(document["shared_reference_revisions"], [])
        self.assertTrue(m.validate(document).ok)
        self.assertIsNone(P.read_cycle_record(self.root, result["cycle_id"]).get("shared_reference_pins"))

    def test_shared_reference_pins_emit_valid_manifest_rows(self):
        admitted = self._admitted_spec_pin()
        pin = {
            "kind": "spec", "shared_reference_id": admitted["shared_reference_id"],
            "shared_reference_revision_id": admitted["shared_reference_revision_id"],
        }
        route, route_file, result = self.begin("direct", "autopilot-code", shared_reference_pins=[pin])
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["shared_reference_pins"], [pin])
        self.write_output(result)
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(m.validate(document).ok, m.validate(document).violations)
        self.assertEqual(document["shared_references"], [{
            "shared_reference_id": admitted["shared_reference_id"], "kind": "shared-spec",
            "title": "artifacts/spec",
        }])
        self.assertEqual(len(document["shared_reference_revisions"]), 1)
        rev_row = document["shared_reference_revisions"][0]
        self.assertEqual(rev_row["shared_reference_revision_id"], admitted["shared_reference_revision_id"])
        self.assertEqual(rev_row["shared_reference_id"], admitted["shared_reference_id"])
        self.assertEqual(rev_row["content_digest"], admitted["content_digest"])
        self.assertEqual(rev_row["provenance"]["source_manifest_id"], document["manifest_id"])

    def test_shared_reference_pin_missing_revision_is_typed_hold(self):
        admitted = self._admitted_spec_pin()
        pin = {
            "kind": "spec", "shared_reference_id": admitted["shared_reference_id"],
            "shared_reference_revision_id": "rrev_" + "9" * 32,
        }
        route, route_file, result = self.begin("direct", "autopilot-code", shared_reference_pins=[pin])
        self.write_output(result)
        self.close(route, route_file)
        with self.assertRaises(P.ProducerError) as ctx:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(ctx.exception.code, "shared-reference-pin-unresolved")
        self.assertFalse((Path(result["cycle_dir"]) / "manifest.json").exists())

    def test_shared_reference_pin_digest_mismatch_is_typed_hold(self):
        admitted = self._admitted_spec_pin()
        pin = {
            "kind": "spec", "shared_reference_id": admitted["shared_reference_id"],
            "shared_reference_revision_id": admitted["shared_reference_revision_id"],
            "content_digest": "sha256:" + "0" * 64,
        }
        route, route_file, result = self.begin("direct", "autopilot-code", shared_reference_pins=[pin])
        self.write_output(result)
        self.close(route, route_file)
        with self.assertRaises(P.ProducerError) as ctx:
            P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(ctx.exception.code, "shared-reference-pin-digest-mismatch")
        self.assertFalse((Path(result["cycle_dir"]) / "manifest.json").exists())

    def test_begin_shared_reference_cli_flag(self):
        admitted = self._admitted_spec_pin()
        route, route_file = self.route("direct", "autopilot-code")
        pin_value = f"spec:{admitted['shared_reference_id']}:{admitted['shared_reference_revision_id']}:{admitted['content_digest']}"
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = P.main([
                "begin", "--artifact-root", str(self.root), "--route", str(route_file),
                "--capability", "autopilot-code", "--intensity", "direct",
                "--shared-reference", pin_value,
            ])
        self.assertEqual(code, P.OK)
        payload = json.loads(buffer.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["status"], "begun")
        record = P.read_cycle_record(self.root, payload["cycle_id"])
        self.assertEqual(record["shared_reference_pins"], [{
            "kind": "spec", "shared_reference_id": admitted["shared_reference_id"],
            "shared_reference_revision_id": admitted["shared_reference_revision_id"],
            "content_digest": admitted["content_digest"],
        }])

    def test_related_field_validation(self):
        self.activate()
        route, route_file, first = self.begin(campaign_key="camp-a")
        self.write_output(first)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=first["cycle_id"])
        route2, route_file2, second = self.begin(intensity="quick", campaign_key="camp-b")

        with self.assertRaises(P.ProducerError) as ctx:
            P.set_campaign_related(self.root, first["campaign_id"], related=[{"kind": "bogus-kind",
                                    "campaign_id": second["campaign_id"]}])
        self.assertEqual(ctx.exception.code, "campaign-related-invalid")

        with self.assertRaises(P.ProducerError) as ctx:
            P.set_campaign_related(self.root, first["campaign_id"], related=[{"kind": "related"}])
        self.assertEqual(ctx.exception.code, "campaign-related-invalid")

        with self.assertRaises(P.ProducerError) as ctx:
            P.set_campaign_related(self.root, first["campaign_id"],
                                   related=[{"kind": "related", "campaign_id": "camp_" + "z" * 32}])
        self.assertEqual(ctx.exception.code, "campaign-related-unresolved")

        related = [{"kind": "precedes", "campaign_id": second["campaign_id"]}]
        result = P.set_campaign_related(self.root, first["campaign_id"], related=related)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(P.read_campaign(self.root, first["campaign_id"])["related"], related)
        # Not bidirectional: the related campaign's own record is untouched.
        self.assertIsNone(P.read_campaign(self.root, second["campaign_id"]).get("related"))
        self.assertEqual(
            P.check_write(self.root, self.root / "campaigns" / first["campaign_id"] / "campaign.json")["reason"],
            "campaign-record-machine-managed")

    def test_mark_campaign_superseded_refuses_live_cycles(self):
        self.activate()
        route, route_file, first = self.begin(campaign_key="camp-live")
        self.write_output(first)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=first["cycle_id"])
        route2, route_file2, second = self.begin(intensity="quick", campaign_key="camp-live")
        self.write_output(second)
        self.close(route2, route_file2)
        P.finalize(self.root, cycle_id=second["cycle_id"])

        with self.assertRaises(P.ProducerError) as ctx:
            P.mark_campaign_superseded(self.root, first["campaign_id"])
        self.assertEqual(ctx.exception.code, "campaign-has-live-cycles")

        superseded_first = P.mark_cycle_superseded(
            self.root, first["cycle_id"], superseded_by=[second["cycle_id"]], superseded_event_id="ev_" + "a" * 32)
        self.assertEqual(superseded_first["state"], "superseded")
        manifest_before = (Path(first["cycle_dir"]) / "manifest.json").read_bytes()

        with self.assertRaises(P.ProducerError) as ctx:
            P.mark_campaign_superseded(self.root, first["campaign_id"])
        self.assertEqual(ctx.exception.code, "campaign-has-live-cycles")

        P.mark_cycle_superseded(
            self.root, second["cycle_id"], superseded_by=[], superseded_event_id="ev_" + "b" * 32)
        result = P.mark_campaign_superseded(self.root, first["campaign_id"])
        self.assertEqual(result["state"], "superseded")
        self.assertEqual(P.read_campaign(self.root, first["campaign_id"])["state"], "superseded")
        # The sealed manifest's folded cycle.state is untouched by the side record.
        self.assertEqual((Path(first["cycle_dir"]) / "manifest.json").read_bytes(), manifest_before)
        document = json.loads(manifest_before.decode("utf-8"))
        self.assertEqual(document["cycle"]["state"], "completed")
        self.assertEqual(P.read_cycle_record(self.root, first["cycle_id"])["state"], "superseded")
        # A key freed only by supersession cannot be resumed through find_campaign_by_key.
        self.assertIsNone(P.find_campaign_by_key(self.root, "camp-live"))


class ComponentSetPreservation(ProducerTestBase):
    """D-87 (a)(b)(c): a partial admit must not silently delete the components it
    did not carry. On 2026-09-03 two admits one minute apart took a three-component
    reference down to one and two PRDs vanished from `latest`."""

    def _cycle(self, generations):
        """One sealed cycle holding every generation as its own source subtree.

        Generations are separate sources, not separate routes: a second compile of
        the same tuple is a duplicate runtime route, and what this suite needs to
        vary is the admitted tree, not the route.
        """
        self.activate()
        route, route_file, result = self.begin("direct", "autopilot-spec", "update")
        for index, components in enumerate(generations):
            for name in components:
                self.write_output(result, f"gen{index}/{name}/prd.md",
                                  f"# {name} g{index}\n".encode("utf-8"))
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        return result

    def _admit(self, result, generation, **kw):
        return P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec",
                              source=f"gen{generation}", key="prd", **kw)

    def _reference(self, reference_id):
        return json.loads(
            (self.root / "shared" / "spec" / reference_id / "reference.json").read_text()
        )

    def _staging_leftovers(self, reference_id):
        revisions = self.root / "shared" / "spec" / reference_id / "revisions"
        return sorted(p.name for p in revisions.iterdir() if p.name.startswith(".admitting-"))

    def _journals(self):
        directory = P.shared_journal_path(self.root, "rrev_probe").parent
        return sorted(p.name for p in directory.glob("*.json")) if directory.is_dir() else []

    def test_regressed_component_set_is_refused(self):
        """A17-1: refusal names every component that would have vanished, and
        leaves no revision, no journal and no staging behind."""
        result = self._cycle([["a", "b"], ["a"]])
        admitted = self._admit(result, 0)
        reference_id = admitted["shared_reference_id"]
        before, journals_before = self._reference(reference_id), self._journals()
        with self.assertRaises(P.ProducerError) as ctx:
            self._admit(result, 1)
        self.assertEqual(ctx.exception.code, "component-set-regressed")
        self.assertIn("b", ctx.exception.detail)
        after = self._reference(reference_id)
        self.assertEqual(after["revisions"], before["revisions"])
        self.assertEqual(after["latest_revision_id"], before["latest_revision_id"])
        self.assertEqual(self._staging_leftovers(reference_id), [])
        self.assertEqual(self._journals(), journals_before)

    def test_drop_component_admits_and_records(self):
        """A17-2."""
        result = self._cycle([["a", "b"], ["a"]])
        admitted = self._admit(result, 0)
        dropped = self._admit(result, 1, drop_components=["b"], drop_reason="superseded by a")
        self.assertEqual(dropped["status"], "admitted")
        record = json.loads((Path(dropped["revision_dir"]) / "revision.json").read_text())
        self.assertEqual(record["dropped_components"],
                         [{"name": "b", "reason": "superseded by a"}])
        reference = self._reference(admitted["shared_reference_id"])
        self.assertEqual(reference["latest_revision_id"], dropped["shared_reference_revision_id"])

    def test_drop_reason_defaults_when_absent(self):
        result = self._cycle([["a", "b"], ["a"]])
        self._admit(result, 0)
        dropped = self._admit(result, 1, drop_components=["b"])
        record = json.loads((Path(dropped["revision_dir"]) / "revision.json").read_text())
        self.assertEqual(record["dropped_components"], [{"name": "b", "reason": "unspecified"}])

    def test_drop_component_unknown_is_refused(self):
        result = self._cycle([["a", "b"], ["a", "b"]])
        self._admit(result, 0)
        with self.assertRaises(P.ProducerError) as ctx:
            self._admit(result, 1, drop_components=["zzz"])
        self.assertEqual(ctx.exception.code, "drop-component-unknown")

    def test_superset_is_not_a_regression(self):
        """A17-3: adding a component is not a regression."""
        result = self._cycle([["a", "b"], ["a", "b", "c"]])
        self._admit(result, 0)
        widened = self._admit(result, 1)
        self.assertEqual(widened["status"], "admitted")
        record = json.loads((Path(widened["revision_dir"]) / "revision.json").read_text())
        self.assertNotIn("dropped_components", record)
        self.assertEqual(P.component_set(row["path"] for row in record["files"]), {"a", "b", "c"})

    def test_single_component_reference_unchanged(self):
        """A17-4: the ordinary single-component admit behaves exactly as before and
        its revision record gains no key."""
        result = self._cycle([["a"], ["a"]])
        one = self._admit(result, 0)
        two = self._admit(result, 1)
        for admitted in (one, two):
            record = json.loads((Path(admitted["revision_dir"]) / "revision.json").read_text())
            self.assertNotIn("dropped_components", record)
        self.assertEqual(
            json.loads((Path(two["revision_dir"]) / "revision.json").read_text())["sequence"], 2
        )

    def test_first_revision_is_exempt(self):
        """A17-6: no predecessor, nothing to regress against."""
        result = self._cycle([["a", "b"]])
        admitted = self._admit(result, 0)
        self.assertEqual(admitted["status"], "admitted")
        self.assertTrue(admitted["reference_created"])

    def test_flat_top_level_files_are_components(self):
        """`rrev_15cf1d9f` admitted `prd.md` flat; defining components as top-level
        directories would read that revision as empty and miss the regression."""
        self.assertEqual(P.component_set(["prd.md", "a/b.md", "revision.json"]), {"prd.md", "a"})


class ComponentSetCheckSurface(ProducerTestBase):
    """D-87 (d): read-only adjacent-pair check, `components(new) >= components(old) - dropped(new)`."""

    def _chain(self, generations):
        """Build a reference whose history already regressed, the way the real one
        did before (a) existed: admit with an explicit drop, then erase the drop
        record so the pair reads as a silent loss."""
        self.activate()
        route, route_file, result = self.begin("direct", "autopilot-spec", "update")
        for index, components in enumerate(generations):
            for name in components:
                self.write_output(result, f"gen{index}/{name}/prd.md",
                                  f"# {name} g{index}\n".encode("utf-8"))
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        ids, admitted = [], None
        for index, components in enumerate(generations):
            previous = generations[index - 1] if index else []
            admitted = P.admit_shared(
                self.root, cycle_id=result["cycle_id"], kind="spec", source=f"gen{index}",
                key="prd",
                drop_components=[name for name in previous if name not in components],
            )
            ids.append(admitted["shared_reference_revision_id"])
            record_path = Path(admitted["revision_dir"]) / "revision.json"
            record = json.loads(record_path.read_text())
            if record.pop("dropped_components", None) is not None:
                record_path.chmod(0o600)
                record_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        return admitted["shared_reference_id"], ids

    def test_reports_regression_and_recovery(self):
        """A17-5 shape: violations while components vanish, zero once restored."""
        reference_id, ids = self._chain([["a", "b", "c"], ["a"], ["b"], ["a", "b", "c"]])
        report = P.check_component_sets(self.root, "spec", reference_id)
        self.assertEqual(report["violations"], 2)
        self.assertEqual([pair["verdict"] for pair in report["pairs"]],
                         ["regressed", "regressed", "ok"])
        recovered = P.check_component_sets(self.root, "spec", reference_id, from_revision=ids[-1])
        self.assertEqual(recovered["violations"], 0)

    def test_window_bounds_the_report(self):
        """The window is not cosmetic: unbounded, the real reference reports nine
        violations reaching back to seq 8, and A17-5 asks about two."""
        reference_id, ids = self._chain([["a", "b", "c"], ["a"], ["b"], ["a", "b", "c"]])
        window = P.check_component_sets(self.root, "spec", reference_id,
                                        from_revision=ids[1], to_revision=ids[2])
        self.assertEqual(window["violations"], 1)
        self.assertEqual(len(window["pairs"]), 1)

    def test_unknown_window_bound_is_refused(self):
        reference_id, _ids = self._chain([["a", "b"], ["a"]])
        with self.assertRaises(P.ProducerError) as ctx:
            P.check_component_sets(self.root, "spec", reference_id, from_revision="rrev_nope")
        self.assertEqual(ctx.exception.code, "revision-unknown")

    def test_explicit_drop_is_not_a_violation(self):
        self.activate()
        route, route_file, result = self.begin("direct", "autopilot-spec", "update")
        for name in ("a", "b"):
            self.write_output(result, f"gen0/{name}/prd.md", b"x\n")
        self.write_output(result, "gen1/a/prd.md", b"y\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        admitted = P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec",
                                  source="gen0", key="prd")
        P.admit_shared(self.root, cycle_id=result["cycle_id"], kind="spec", source="gen1",
                       key="prd", drop_components=["b"], drop_reason="retired")
        report = P.check_component_sets(self.root, "spec", admitted["shared_reference_id"])
        self.assertEqual(report["violations"], 0)
        self.assertEqual(report["pairs"][-1]["dropped"], ["b"])

    def test_missing_revision_record_is_reported_not_raised(self):
        """`rrev_3af0bce6` has no `revision.json`; the check must say so, not crash."""
        reference_id, ids = self._chain([["a", "b"], ["a"]])
        record = (self.root / "shared" / "spec" / reference_id / "revisions" / ids[0]
                  / "revision.json")
        record.chmod(0o600)
        record.unlink()
        report = P.check_component_sets(self.root, "spec", reference_id)
        self.assertEqual(report["unreadable"], [ids[0]])
        self.assertEqual([pair["verdict"] for pair in report["pairs"]], ["unknown"])

    def test_check_writes_nothing(self):
        reference_id, _ids = self._chain([["a", "b"], ["a"]])
        shared = self.root / "shared"
        before = {p: p.stat().st_mtime_ns for p in sorted(shared.rglob("*")) if p.is_file()}
        P.check_component_sets(self.root, "spec", reference_id)
        after = {p: p.stat().st_mtime_ns for p in sorted(shared.rglob("*")) if p.is_file()}
        self.assertEqual(before, after)



class TerminalExactRecoveryTest(ProducerTestBase):
    def test_terminal_exact_rejects_each_index_identity_drift_without_mutation(self):
        result, output, binding = self.prepared()
        cycle_id = result["cycle_id"]
        P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        original = P.artifact_index.to_payload(adm.load_index(self.root))
        manifest = Path(result["cycle_dir"]) / "manifest.json"
        record = P.cycle_record_path(self.root, cycle_id)
        stable_bytes = (manifest.read_bytes(), record.read_bytes(), output.read_bytes())
        cases = [(section, key) for section in ("manifests", "cycles")
                 for key in [*original[section][cycle_id], None]]
        cases.append(("artifact_root_id", None))
        for section, key in cases:
            with self.subTest(section=section, key=key):
                payload = json.loads(json.dumps(original))
                if section == "artifact_root_id":
                    payload[section] = "aroot_" + "f" * 32
                elif key is None:
                    del payload[section][cycle_id]
                else:
                    payload[section][cycle_id][key] += "-foreign"
                drifted = P.artifact_index.parse(payload)
                adm._write_index(self.root, drifted)
                for operation in (P.verify_finalized_cycle, P.finalize_exact_cycle):
                    with self.assertRaisesRegex(P.ProducerError, "already-sealed-mismatch"):
                        operation(self.root, cycle_id=cycle_id, expected_binding=binding)
                    self.assertEqual(P.artifact_index.to_payload(adm.load_index(self.root)), payload)
                    self.assertEqual((manifest.read_bytes(), record.read_bytes(), output.read_bytes()), stable_bytes)
                    self.assertFalse(P.journal_path(self.root, cycle_id).exists())
        adm._write_index(self.root, P.artifact_index.parse(original))
        self.assertEqual(P.verify_finalized_cycle(self.root, cycle_id=cycle_id,
                         expected_binding=binding)["status"], "already-sealed")

    def test_general_absent_cache_compatibility_never_proves_terminal_exact(self):
        result, output, binding = self.prepared()
        cycle_id = result["cycle_id"]
        P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        manifest = Path(result["cycle_dir"]) / "manifest.json"
        manifest.unlink()  # isolated relocation-compatibility fixture only
        before = P.read_cycle_record(self.root, cycle_id)
        normal = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(normal["status"], "already-sealed")
        self.assertEqual(normal["cycle_state"], "completed")
        for operation in (P.verify_finalized_cycle, P.finalize_exact_cycle):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(P.ProducerError, "already-sealed-mismatch"):
                    operation(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(P.read_cycle_record(self.root, cycle_id), before)
        self.assertFalse(manifest.exists())

    def prepared(self):
        self.activate()
        route, route_file, result = self.begin()
        output = self.write_output(result, "plans/cycle/final_report.md", b"verified report\n")
        self.close(route, route_file)
        record = P.read_cycle_record(self.root, result["cycle_id"])
        binding = {key: record[key] for key in ("campaign_id", "cycle_id", "producer_id", "route_hash")}
        binding["cycle_record_digest"] = P.dispatch_terminal_commit.cycle_identity_digest(record)
        return result, output, binding

    def test_manifest_crash_recovers_only_exact_cycle_and_replay_proves_outputs(self):
        result, output, binding = self.prepared()
        other_route, other_file = self.route(slug="other-open-cycle")
        other = P.begin(self.root, route_file=other_file, capability="autopilot-code", intensity="direct")
        other_before = P.read_cycle_record(self.root, other["cycle_id"])
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize_exact_cycle(self.root, cycle_id=result["cycle_id"], expected_binding=binding,
                                   crash_after_manifest=True)
        manifest = Path(result["cycle_dir"]) / "manifest.json"
        before = manifest.read_bytes()
        with mock.patch.object(P, "_recover_locked", side_effect=AssertionError("root recovery forbidden")):
            replay = P.finalize_exact_cycle(self.root, cycle_id=result["cycle_id"], expected_binding=binding)
            self.assertEqual(replay["status"], "already-sealed")
            P.verify_finalized_cycle(self.root, cycle_id=result["cycle_id"], expected_binding=binding)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(P.read_cycle_record(self.root, other["cycle_id"]), other_before)
        output.write_bytes(b"drift after sealing\n")
        with self.assertRaisesRegex(P.ProducerError, "already-sealed-mismatch"):
            P.verify_finalized_cycle(self.root, cycle_id=result["cycle_id"], expected_binding=binding)

    def test_completed_finalize_live_lease_and_reentry_make_no_manifest(self):
        result, output, binding = self.prepared()
        P.review_lease_acquire(self.root, cycle_id=result["cycle_id"], attempt_id="att-review")
        with self.assertRaisesRegex(P.ProducerError, "cycle-finalize-blocked-live-review"):
            P.finalize_exact_cycle(self.root, cycle_id=result["cycle_id"], expected_binding=binding)
        self.assertFalse((Path(result["cycle_dir"]) / "manifest.json").exists())
        with self.assertRaisesRegex(P.ProducerError, "finalize-reentry-forbidden"):
            P.finalize(self.root, cycle_id=result["cycle_id"], _admission_lock_fd=123)
        self.assertEqual(P.read_cycle_record(self.root, result["cycle_id"])["state"], "open")


class QuickOwnerBindingIntegrationTest(ProducerTestBase):
    def test_registered_quick_begin_crash_resumes_same_binding_without_owner_route_fields(self):
        self.activate()
        route, route_file = self.route("quick")
        node = route["nodes"][0]
        jobs = Path(self._tmp.name) / "jobs.log"
        owner = "att-quick-binding"
        meta = dict(attempt_id=owner, worker_type="owner", dispatch_depth="1", registered_worker="1",
            harness="codex", capability="autopilot-code", capability_mode="dev", intensity="quick",
            route_file=str(route_file), route_id=route["route_id"], route_hash=route["route_hash"],
            route_node=node["id"], registry_digest=route["registry_digest"],
            completion_gate=node["completion_gate"], write_scope=";".join(node["write_scope"]))
        jobs.write_text(f"2026-09-08T00:00:00Z\topen\t{R.ROOT}\t{R.ROOT}\tquick\t"
                        + ",".join(f"{k}={v}" for k,v in meta.items()) + "\n")
        kwargs = dict(route_file=route_file, capability="autopilot-code", intensity="quick",
                      jobs=jobs, owner_attempt_id=owner)
        with mock.patch.object(P.dispatch_terminal_commit, "publish_producer_binding", side_effect=RuntimeError("binding-crash")):
            with self.assertRaisesRegex(RuntimeError, "binding-crash"):
                P.begin(self.root, **kwargs)
        records = P.list_cycle_records(self.root)
        self.assertEqual(len(records), 1)
        result = P.begin(self.root, **kwargs)
        self.assertEqual(result["cycle_id"], records[0]["cycle_id"])
        binding = P.dispatch_terminal_commit.load_producer_binding(artifact_root=self.root,
                          route_id=route["route_id"], owner_attempt_id=owner)
        self.assertEqual(binding.binding["cycle_id"], result["cycle_id"])
        self.assertEqual(P.begin(self.root, **kwargs)["cycle_id"], result["cycle_id"])
        self.assertEqual(len(P.list_cycle_records(self.root)), 1)


class TerminalTransactionIntegrationTest(ProducerTestBase):
    def _prepare_fixture(self, harness="claude", capability="autopilot-code"):
        import dispatch_terminal_commit as terminal
        self.activate()
        route=R.compile_route(capability,"update" if capability=="autopilot-spec" else "dev","standard",cwd=R.ROOT,artifact_root=self.root,
            predicates=[],transport="headless",tracking="tracked",tracked_gate_evidence=gate_evidence(),
            slug="terminal-transaction-fixture",dispatch_evidence={"tuples":[nested(harness,"codex")]})
        if capability=="autopilot-spec":
            route=R.compose_route(capability=capability,capability_mode="update",shape="staged",
                graph="review,prd-transaction",slug="terminal-transaction-fixture",cwd=R.ROOT,
                artifact_root=self.root,intensity="standard",dispatch_evidence={"tuples":[nested(harness,"codex")]})
        route_file=Path(L.admit_runtime_route(self.root,route).route_file)
        jobs=Path(self._tmp.name)/"jobs.log"; owner="att-transaction-owner"; child="att-transaction-report"
        owner_meta=dict(attempt_id=owner,worker_type="owner",dispatch_depth="1",registered_worker="1",
            harness=harness,owner_route_file=str(route_file),owner_route_id=route["route_id"],
            owner_route_hash=route["route_hash"])
        def row(status,slug,metadata):
            return f"2026-09-08T00:00:00Z\t{status}\t{R.ROOT}\t{R.ROOT}\t{slug}\t"+",".join(f"{k}={v}" for k,v in metadata.items())+"\n"
        jobs.write_text(row("open","owner",owner_meta))
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}):
            result=P.begin(self.root,route_file=route_file,capability=capability,intensity="standard",
                           jobs=jobs,owner_attempt_id=owner)
            artifact=self.write_output(result,rel=("spec/_internal/reviews/verdict.md" if capability=="autopilot-spec"
                else "plans/fixture/final_report.md"),data=b"verified fixture report\n")
            node=next(node for node in route["nodes"] if (node["id"]=="review" if capability=="autopilot-spec" else node.get("terminal")))
            child_meta=dict(attempt_schema_version=2,attempt_id=child,parent_attempt_id=owner,
                dispatch_depth=2,transport="headless",execution_surface="registered-headless",registered_worker="1",
                fallback_hop="same-harness-headless",harness="codex",route_id=route["route_id"],
                route_hash=route["route_hash"],route_node=node["id"],failure_class="pass",launch_outcome="reaped-before-publish")
            subprocess.run([sys.executable,"-c","pass"],check=True)
            jobs.write_text(row("open","owner",owner_meta)+row("open","report",child_meta))
            R.complete_node(route,node,node["id"],artifact,attempt_id=child,jobs=jobs)
            request=terminal.TerminalCommitRequest(route_file,owner,jobs,self.root)
            return route,route_file,jobs,owner,result,artifact,request

    def test_spec_owner_settlement_uses_native_executor_evidence_for_all_harnesses(self):
        import dispatch_terminal_commit as terminal
        from dispatch_completion_join import exact_attempt_row, join_selected_attempts
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture=TerminalTransactionIntegrationTest(); fixture.setUp()
                try:
                    route,path,jobs,owner,cycle,review,request=fixture._prepare_fixture(harness,"autopilot-spec")
                    report=fixture.write_output(cycle,rel="spec/_internal/owner-report.md",data=b"verified transaction report\n")
                    text=f"artifact: {report}\nverdict: PASS\nblocker: none"
                    native={
                        "codex":[{"type":"item.completed","item":{"type":"agent_message","text":text}},
                                 {"type":"turn.completed"}],
                        "claude":[{"type":"result","subtype":"success","is_error":False,"result":text}],
                        "opencode":[{"type":"text","sessionID":"ses_test","part":{"type":"text","text":text}},
                                    {"type":"step_finish","sessionID":"ses_test","part":{"type":"step-finish","reason":"stop"}}],
                    }[harness]
                    log=jobs.parent/"owner.jsonl"; log.write_text("\n".join(json.dumps(r) for r in native)+"\n")
                    jobs.write_text(jobs.read_text().replace("worker_type=owner",f"attempt_schema_version=2,worker_type=owner,log_file={log},workflow_completion=runtime-v1"))
                    self.assertIsNone(terminal.owner_workflow_continuation(jobs,owner,path))
                    old=review.read_bytes(); review.unlink()
                    self.assertIn("review",terminal.owner_workflow_continuation(jobs,owner,path))
                    review.write_bytes(old)
                    fixture._closed_owner(jobs,owner); meta=exact_attempt_row(jobs,owner).metadata
                    before=jobs.read_bytes()
                    with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs),"AGENT_ARTIFACT_ROOT":str(fixture.root)}):
                        gates=R.terminal_gate_observation(route,jobs=jobs,exact_terminal=True)
                        self.assertTrue(gates["prd-transaction"]["passed"],gates)
                        snapshot={str(p):p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}
                        diagnosis=terminal.inspect_owner_completion(jobs,"done",meta)
                        self.assertEqual((diagnosis["state"],diagnosis["checkpoint"]),("closure-pending","not-claimed"))
                        self.assertEqual(snapshot,{str(p):p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()})
                        marker=R.completion_dir(route["route_id"],jobs=jobs)/"prd-transaction.json"
                        marker.write_text("{}")
                        self.assertFalse(R.terminal_gate_observation(route,jobs=jobs)["prd-transaction"]["passed"])
                        marker.unlink()
                        # An unavailable native result remains an owned obligation.
                        saved=log.read_bytes(); log.write_text("")
                        self.assertFalse(R.terminal_gate_observation(route,jobs=jobs)["prd-transaction"]["passed"])
                        log.write_bytes(saved)
                        receipt=join_selected_attempts(jobs=jobs,expected_attempts={owner},timeout=0,recover=True)
                        self.assertEqual(receipt["state"],"ready",receipt)
                        self.assertFalse(terminal.owner_completion_pending(jobs,"done",meta))
                        self.assertEqual(terminal.settle_owner_completion(jobs,"done",meta).result,"completed")
                        self.assertEqual(jobs.read_bytes(),before)
                        self.assertFalse((R.completion_dir(route["route_id"],jobs=jobs)/"prd-transaction.json").exists())
                        self.assertEqual(P.read_cycle_record(fixture.root,cycle["cycle_id"])["state"],"sealed")
                        report.write_text("changed after settlement")
                        self.assertTrue(terminal.owner_completion_pending(jobs,"done",meta))
                finally: fixture.doCleanups()

    def _closed_owner(self, jobs, owner):
        lines=jobs.read_text().splitlines()
        for i,line in enumerate(lines):
            fields=line.split("\t"); meta=D.parse_registry_metadata(fields[5])
            if meta.get("attempt_id") != owner: continue
            meta.update(attempt_schema_version="2", execution_surface="registered-headless", transport="headless",
                        fallback_hop="same-harness-headless", failure_class="pass", note="completed-supervisor",
                        workflow_completion="runtime-v1", launch_outcome="reaped-before-publish",
                        parent_sid="fixture-parent", parent_completion_delivery="codex-managed-gateway")
            fields[1]="done"; fields[5]=",".join(f"{k}={v}" for k,v in meta.items());lines[i]="\t".join(fields)
        jobs.write_text("\n".join(lines)+"\n")
        return meta

    def test_executing_owner_uses_same_terminal_proof_before_and_after_settlement(self):
        import dispatch_terminal_commit as terminal
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture=TerminalTransactionIntegrationTest(); fixture.setUp()
                try:
                    route,path,jobs,owner,result,artifact,request=fixture._prepare_fixture(harness)
                    jobs.write_text(jobs.read_text().replace("worker_type=owner", "attempt_schema_version=2,workflow_completion=runtime-v1,worker_type=owner"))
                    before=jobs.read_bytes()
                    self.assertIsNone(terminal.owner_workflow_continuation(jobs,owner,path))
                    saved=artifact.read_bytes(); artifact.unlink()
                    prompt=terminal.owner_workflow_continuation(jobs,owner,path)
                    self.assertIn("[workflow-completion-pending]",prompt)
                    self.assertIn("same owner",prompt)
                    self.assertEqual(jobs.read_bytes(),before)
                    artifact.write_bytes(saved)
                    self.assertIsNone(terminal.owner_workflow_continuation(jobs,owner,path))
                    self.assertEqual(jobs.read_bytes(),before)
                finally: fixture.doCleanups()

    def test_runtime_completion_finishes_and_replays_without_changing_pass_for_three_harnesses(self):
        import dispatch_terminal_commit as terminal
        import workflow_state
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture=TerminalTransactionIntegrationTest(); fixture.setUp()
                try:
                    route,path,jobs,owner,result,artifact,request=fixture._prepare_fixture(harness)
                    fixture._closed_owner(jobs,owner)
                    from dispatch_completion_join import exact_attempt_row, materialize_after_terminal_close
                    meta=exact_attempt_row(jobs,owner).metadata
                    before=jobs.read_bytes()
                    self.assertTrue(terminal.owner_completion_pending(jobs,"done",meta))
                    settled = terminal.settle_owner_completion(jobs,"done",meta)
                    self.assertEqual(settled.result,"completed",settled)
                    materialize_after_terminal_close(jobs,owner)
                    self.assertFalse(terminal.owner_completion_pending(jobs,"done",meta))
                    self.assertIn(str(artifact),terminal.completed_owner_handoff(jobs,"done",meta))
                    self.assertEqual(workflow_state.WorkflowLedger(route["route_id"],route["route_hash"],jobs=jobs).state()["workflow_state"],"COMPLETE")
                    manifest=Path(result["cycle_dir"])/"manifest.json"
                    sealed=manifest.read_bytes()
                    self.assertEqual(P.read_cycle_record(fixture.root,result["cycle_id"])["state"],"sealed")
                    self.assertEqual(terminal.settle_owner_completion(jobs,"done",meta).result,"completed")
                    self.assertEqual(manifest.read_bytes(),sealed)
                    self.assertEqual(jobs.read_bytes(),before)
                    artifact.write_text("changed after sealing")
                    self.assertTrue(terminal.owner_completion_pending(jobs,"done",meta))
                finally: fixture.doCleanups()

    def test_runtime_completion_failure_keeps_pass_notice_and_recovers_without_a_model(self):
        import dispatch_terminal_commit as terminal
        import dispatch_supervision as supervision
        from dispatch_completion_join import exact_attempt_row, join_selected_attempts, materialize_after_terminal_close
        route,path,jobs,owner,result,artifact,request=self._prepare_fixture()
        self._closed_owner(jobs,owner); meta=exact_attempt_row(jobs,owner).metadata; before=jobs.read_bytes()
        with mock.patch.object(P,"finalize_exact_cycle",side_effect=P.ProducerError("fixture-busy")):
            materialize_after_terminal_close(jobs,owner)
            self.assertTrue(terminal.owner_completion_pending(jobs,"done",meta))
            import workflow_state
            self.assertNotEqual(workflow_state.WorkflowLedger(route["route_id"],route["route_hash"],jobs=jobs).state()["workflow_state"],"COMPLETE")
            receipt=join_selected_attempts(jobs=jobs,expected_attempts={owner},timeout=0,recover=False)
            self.assertNotEqual(receipt["state"],"ready")
        notices=supervision.materialize(jobs,{owner},reason="workflow-completion-pending")
        self.assertTrue(supervision.notice_is_current(notices[0]))
        self.assertEqual(jobs.read_bytes(),before)

        receipt=join_selected_attempts(jobs=jobs,expected_attempts={owner},timeout=2,recover=True)
        self.assertEqual(receipt["state"],"ready",receipt)
        self.assertFalse(supervision.notice_is_current(notices[0]))
        self.assertEqual(jobs.read_bytes(),before)

    def test_completed_retry_history_does_not_block_closure_or_allow_late_start(self):
        import dispatch_terminal_commit as terminal
        route,path,jobs,owner,result,artifact,request=self._prepare_fixture()
        lines=jobs.read_text().splitlines(); fields=lines[-1].split("\t")
        meta=D.parse_registry_metadata(fields[5]); current=meta["attempt_id"]
        meta.update(attempt_id="att-prior-review", failure_class="runtime", note="dead-runtime-exit",
                    retry_attempt_id=current, retry_claimed_at="fixture-past")
        fields[5]=",".join(f"{k}={v}" for k,v in meta.items())
        lines.insert(-1,"\t".join(fields));jobs.write_text("\n".join(lines)+"\n")
        self._closed_owner(jobs,owner)
        from dispatch_completion_join import exact_attempt_row
        owner_meta=exact_attempt_row(jobs,owner).metadata; before=jobs.read_bytes()
        settled=terminal.settle_owner_completion(jobs,"done",owner_meta)
        self.assertEqual(settled.result,"completed",settled)
        self.assertEqual(jobs.read_bytes(),before)
        with self.assertRaises(D.DispatchContractError) as caught:
            D.ensure_terminal_claim_absent(jobs,route["route_id"],"att-prior-review")
        self.assertEqual(caught.exception.reason,"terminal-claim-conflict")

    def test_late_conflict_in_nonterminal_child_holds_consumption_without_rewriting_success(self):
        import dispatch_terminal_commit as terminal
        from dispatch_completion_join import exact_attempt_row
        route,path,jobs,owner,result,artifact,request=self._prepare_fixture()
        self._closed_owner(jobs,owner)
        # This is a registered advisory child, separate from the report marker.
        child=dict(attempt_id="att-advisory",parent_attempt_id=owner,worker_type="review",dispatch_depth="2",
                   attempt_schema_version="2",registered_worker="1",execution_surface="registered-headless",
                   transport="headless",fallback_hop="same-harness-headless",harness="codex",
                   note="completed-review",failure_class="pass",launch_outcome="reaped-before-publish")
        prefix=f"2026-09-08T00:00:00Z\tdone\t{R.ROOT}\t{R.ROOT}\tadvisory\t"
        original=jobs.read_text(); jobs.write_text(original+prefix+",".join(f"{k}={v}" for k,v in child.items())+"\n")
        meta=exact_attempt_row(jobs,owner).metadata
        self.assertEqual(terminal.settle_owner_completion(jobs,"done",meta).result,"completed")
        manifest=Path(result["cycle_dir"])/"manifest.json"; sealed=manifest.read_bytes()
        child.update(terminal_conflict="1",conflicting_terminal_note="dead-runtime-exit",conflicting_failure_class="runtime")
        jobs.write_text(original+prefix+",".join(f"{k}={v}" for k,v in child.items())+"\n")
        conflicted=jobs.read_bytes()
        self.assertTrue(terminal.owner_completion_pending(jobs,"done",meta))
        self.assertNotEqual(terminal.settle_owner_completion(jobs,"done",meta).result,"completed")
        self.assertEqual(jobs.read_bytes(),conflicted)
        self.assertEqual(manifest.read_bytes(),sealed)

    def test_default_services_close_finalize_envelope_and_replay(self):
        import dispatch_terminal_commit as terminal
        route,route_file,jobs,owner,result,artifact,request=self._prepare_fixture()
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}):
            proof=terminal.prove_terminal_authority(request)
            self.assertEqual(proof.status,"proved",proof)
            settled=terminal.settle_terminal_commit(request)
            self.assertEqual(settled.result,"completed",settled)
            self.assertIn(f"artifact: {artifact}",settled.envelope_text)
            manifest=Path(result["cycle_dir"])/"manifest.json"
            self.assertTrue(manifest.is_file())
            self.assertEqual(P.read_cycle_record(self.root,result["cycle_id"])["state"],"sealed")
            slot=terminal.terminal_slot(self.root,route["route_id"],owner)
            paths=[R.outcome_path(route_file),manifest,slot/"owner-envelope.txt",slot/"owner-envelope.json"]
            before={str(path):path.read_bytes() for path in paths}
            replay=terminal.settle_terminal_commit(request)
            self.assertEqual(replay.result,"completed",replay)
            self.assertEqual(before,{str(path):path.read_bytes() for path in paths})
            artifact.write_text("post-seal corruption")
            self.assertNotEqual(terminal.settle_terminal_commit(request).result,"completed")

    def test_default_services_recover_each_durable_boundary_without_duplicate_outputs(self):
        import dispatch_terminal_commit as terminal
        checkpoints=("claim-before","claim-after","close-after","manifest-after","finalize-after","envelope-after")
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                fixture=TerminalTransactionIntegrationTest()
                fixture.setUp()
                try:
                    route,route_file,jobs,owner,result,artifact,request=fixture._prepare_fixture()
                    if checkpoint=="claim-before":
                        crash=mock.patch.object(terminal.dispatch_contract,"claim_terminal_route_locked",
                                                side_effect=RuntimeError(checkpoint))
                    elif checkpoint=="claim-after":
                        original=terminal._atomic_json
                        def write(path,value,**kw):
                            if value.get("state")=="claimed":raise RuntimeError(checkpoint)
                            return original(path,value,**kw)
                        crash=mock.patch.object(terminal,"_atomic_json",side_effect=write)
                    elif checkpoint=="manifest-after":
                        crash=mock.patch.object(P,"_commit_sealed",side_effect=RuntimeError(checkpoint))
                    else:
                        next_state={"close-after":"route-closed","finalize-after":"producer-finalized",
                                    "envelope-after":"owner-envelope-sealed"}[checkpoint]
                        original=terminal._advance_state
                        def advance(path,commit_id,expected,next_value,extra):
                            if next_value==next_state:raise RuntimeError(checkpoint)
                            return original(path,commit_id,expected,next_value,extra)
                        crash=mock.patch.object(terminal,"_advance_state",side_effect=advance)
                    with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}):
                        with crash:
                            interrupted=terminal.settle_terminal_commit(request)
                        self.assertNotEqual(interrupted.result,"completed",interrupted)
                        slot=terminal.terminal_slot(fixture.root,route["route_id"],owner)
                        targets=[R.outcome_path(route_file),Path(result["cycle_dir"])/"manifest.json",slot/"owner-envelope.txt"]
                        committed={str(path):path.read_bytes() for path in targets if path.is_file()}
                        recovered=terminal.settle_terminal_commit(request)
                        self.assertEqual(recovered.result,"completed",(checkpoint,recovered))
                        for path in targets:
                            self.assertTrue(path.is_file())
                            if str(path) in committed:self.assertEqual(path.read_bytes(),committed[str(path)])
                        sealed={str(path):path.read_bytes() for path in targets}
                        self.assertEqual(terminal.settle_terminal_commit(request).result,"completed")
                        self.assertEqual(sealed,{str(path):path.read_bytes() for path in targets})
                        self.assertEqual(P.read_cycle_record(fixture.root,result["cycle_id"])["state"],"sealed")
                finally:
                    fixture.doCleanups()

    def test_review_lease_between_close_and_finalize_is_typed_and_recoverable(self):
        import dispatch_terminal_commit as terminal
        route,route_file,jobs,owner,result,artifact,request=self._prepare_fixture()
        original=terminal._advance_state
        def acquire_after_close(path,commit_id,expected,next_value,extra):
            state=original(path,commit_id,expected,next_value,extra)
            if next_value=="route-closed":
                P.review_lease_acquire(self.root,cycle_id=result["cycle_id"],attempt_id="att-late-review")
            return state
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}):
            with mock.patch.object(terminal,"_advance_state",side_effect=acquire_after_close):
                blocked=terminal.settle_terminal_commit(request)
            self.assertEqual(blocked.result,"recoverable",blocked)
            self.assertEqual(blocked.reason,"producer-finalize-failed")
            self.assertIn("cycle-finalize-blocked-live-review",blocked.detail)
            self.assertFalse((Path(result["cycle_dir"])/"manifest.json").exists())
            self.assertEqual(P.read_cycle_record(self.root,result["cycle_id"])["state"],"open")
            P.review_lease_release(self.root,cycle_id=result["cycle_id"],attempt_id="att-late-review")
            self.assertEqual(terminal.settle_terminal_commit(request).result,"completed")

    def test_real_completion_writer_observes_claim_before_marker_mutation(self):
        import dispatch_terminal_commit as terminal
        route,route_file,jobs,owner,result,artifact,request=self._prepare_fixture()
        with mock.patch.dict(os.environ,{"AGENT_DISPATCH_JOBS":str(jobs)}):
            first=terminal.settle_terminal_commit(request,
                services=terminal.TerminalCommitServices(crash_after="claim-after"))
            self.assertEqual(first.result,"recoverable",first)
            node=next(node for node in route["nodes"] if node.get("terminal"))
            directory=R.completion_dir(route["route_id"])
            before={str(path):path.read_bytes() for path in directory.rglob("*.json")}
            registry_before=jobs.read_bytes()
            with self.assertRaises(terminal.dispatch_contract.DispatchContractError) as caught:
                R._complete_node_locked(route,node,node["id"],artifact,jobs=jobs,
                    attempt_id="att-transaction-report")
            self.assertEqual(caught.exception.reason,"terminal-claim-conflict")
            self.assertEqual(before,{str(path):path.read_bytes() for path in directory.rglob("*.json")})
            self.assertEqual(registry_before,jobs.read_bytes())
            self.assertEqual(terminal.settle_terminal_commit(request).result,"completed")


class LocatorDateDuplicationTest(ProducerTestBase):
    """One date in a new work locator, two in a migration locator.

    BC_ResNet 2026-09-10 carried six campaign directories named
    ``<date>_<same date>-<slug>`` and one whose two dates disagreed, so the
    directory sorted under one day and read as another.
    """

    def test_a_route_slug_that_already_carries_a_date_does_not_get_a_second_one(self):
        self.activate()
        route = compile_for("direct", self.root, slug="2026-09-10-r5-streaming-window-sim")
        route_file = Path(L.admit_runtime_route(self.root, route).route_file)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                         intensity="direct", campaign_key="streaming-release")
        campaign = P.read_campaign(self.root, result["campaign_id"])
        locator = campaign["locator"]
        self.assertEqual(len(P.artifact_locator._DATE_PREFIX.findall(locator)), 1, locator)
        self.assertTrue(locator.endswith("_r5-streaming-window-sim"), locator)
        self.assertEqual(campaign["slug"], "r5-streaming-window-sim")
        self.assertEqual(campaign["slug_source"], "route")
        # The route sealed the normalised slug, so the cycle locator under it
        # carries one date too.
        self.assertEqual(route["slug"], "r5-streaming-window-sim")
        cycle_locator = Path(result["cycle_dir"]).name
        self.assertEqual(len(P.artifact_locator._DATE_PREFIX.findall(cycle_locator)), 1,
                         cycle_locator)
        self.assertIsNotNone(P.read_cycle_record(self.root, result["cycle_id"]))

    def test_strip_leading_date_leaves_every_other_slug_intact(self):
        cases = {
            "2026-09-10-r5-window": "r5-window",
            "2026-09-10_r5-window": "r5-window",
            # A date that disagrees with the cycle's date is still dropped: the
            # locator's own date is the authoritative one for new work.
            "2026-09-09-r4-explicit": "r4-explicit",
            "r6-endpoint-options": "r6-endpoint-options",
            # A trailing number is part of the name, never a date.
            "wwd-2026-04": "wwd-2026-04",
            # A slug that is only a date keeps its own text rather than emptying.
            "2026-09-10": "2026-09-10",
        }
        for slug, expected in cases.items():
            with self.subTest(slug=slug):
                self.assertEqual(P.artifact_locator.strip_leading_date(slug), expected)

    def test_locator_base_keeps_both_dates_so_migration_provenance_survives(self):
        """A migration locator dates the move, its slug dates the content.

        `core/CORE.md`'s W7H relocation table records exactly this shape
        (``2026-09-05_2026-08-24-artifact-knowledge-index-w7/``), and
        relayout/residue/resplit all name through `locator_base`. Normalising
        there would erase when the content was originally made.
        """

        self.assertEqual(
            P.artifact_locator.locator_base("2026-09-05T00:00:00Z",
                                            "2026-08-24-artifact-knowledge-index-w7"),
            "2026-09-05_2026-08-24-artifact-knowledge-index-w7")


class RouteLaunchContextTest(ProducerTestBase):
    def test_route_launch_prepares_exact_context_once_without_copied_env(self):
        self.activate()
        route, path = self.route()
        preview = P.prepare_route_artifact_env(path, start=False, jobs=self.jobs)
        self.assertEqual(preview["AGENT_ARTIFACT_CYCLE_ID"], "")
        self.assertEqual(list(P.list_cycle_records(self.root)), [])
        env = P.prepare_route_artifact_env(path, start=True, jobs=self.jobs)
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CYCLE_ID": "cyc_foreign",
                                        "AGENT_ARTIFACT_OUTPUT_DIR": "/foreign/artifacts"}):
            again = P.prepare_route_artifact_env(path, start=True, jobs=self.jobs)
            ready = P.prepare_route_artifact_env(path, start=False, jobs=self.jobs)
        self.assertEqual(again, env)
        self.assertEqual(ready, env)
        self.assertEqual(len(list(P.list_cycle_records(self.root))), 1)
        self.assertEqual(P.read_cycle_record(self.root, env["AGENT_ARTIFACT_CYCLE_ID"])["route_id"], route["route_id"])
        self.assertEqual(Path(env["AGENT_ARTIFACT_OUTPUT_DIR"]), Path(env["AGENT_ARTIFACT_CYCLE_DIR"]) / "artifacts")


if __name__ == "__main__":
    unittest.main()
